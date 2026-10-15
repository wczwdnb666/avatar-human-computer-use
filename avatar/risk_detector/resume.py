# -*- coding: utf-8 -*-
"""断点恢复 —— 人解决完验证题之后，从哪儿接回去。

## 这个模块要回答的问题

熔断期间人做了什么，机器**不知道也不该猜**。可能情况：

1. 人把滑块拖过了，页面自己往下走了 → 当前步骤**已经完成**
2. 人解了验证码但页面停在原地 → 当前步骤**还得重做**
3. 人看了一眼说"这个过不去，算了" → 当前步骤**该放弃**
4. 人压根没管，页面超时跳走了 → 现场已经没了

本模块把前三种变成显式策略，第四种变成"校验发现失效"。

## 三种策略

============  ==========================================================
``retry``     **从当前步骤重试**（默认）。人的典型动作是"解掉验证题"，
              页面通常停在原地等下一步，所以当前步骤要重走。
``skip``      跳过当前步骤，标记 skipped，继续下一跳。适用于
              "人在别的窗口里把事办了，这一步已经不需要了"。
``abort``     任务转入 Blocked，向上抛 :class:`AbortSignal`。
              适用于"人判断过不去"。
============  ==========================================================

## 能力边界

**做**：

- 校验 context 是否还有效（hwnd 在不在、URL 变没变）——
  **失效返回 False 并给原因，不抛异常**（硬约束 H）；
- 按策略产出下一步该干什么（一个显式的 :class:`ResumePlan`）；
- 记录恢复事件（谁、什么时候、用了什么策略）。

**不做**：

- **不重放动作**。本模块只产出"该干什么"的**计划**，
  真正的执行归阶段三的动作层。恢复层去调鼠标键盘，
  等于把动作层的真人化轨迹、坐标校验全绕过去了。
- **不猜测人的意图**。页面变了、内容变了，本模块**不去分析**
  "看起来像是成功了" —— 那是猜测。猜测错了就是静默点错地方。
  拿不准就 abort，让人再来看一眼。
- **不自己决定重试上限**。次数由调用方传（``max_attempt``）；
  超了就转 abort。本模块不发明"重试三次就放弃"这类魔法数字。
- **不用抛异常表达"失效"**。``is_stale`` 返回 ``(bool, reason)`` 的
  元组。失效是**预期结果**之一，不是异常情况 —— 用异常表达会让
  调用方被迫写 try/except，而忘了 catch 的结果就是崩溃。

## 为什么 ``abort`` 抛 AbortSignal 而不是 RiskBlocked

``RiskBlocked`` 的语义是"**检测到风险**，被闸门拦下"。
本模块的 abort 是"**人判断过不去**，主动放弃" —— 两件事。
虽然 exit_code 都是 5，但调用方按类型区分时能看出是谁决定的。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .context import (
    DEFAULT_RESUME_STRATEGY,
    RESUME_STRATEGIES,
    InterventionContext,
)
from .guard import AbortSignal

__all__ = [
    "RESUME_OK",
    "RESUME_STALE",
    "RESUME_ABORTED",
    "ResumePlan",
    "validate_context",
    "resume_from",
    "describe_strategies",
    "selfcheck",
]


#: 恢复判定结果（放在 plan 的 ``outcome`` 字段里）。
RESUME_OK = "ok"              # 现场有效，可以按策略继续
RESUME_STALE = "stale"        # 现场失效 —— 不要恢复
RESUME_ABORTED = "aborted"    # 策略是 abort —— 主动终止


# --------------------------------------------------------------------------
# 计划对象
# --------------------------------------------------------------------------
@dataclass
class ResumePlan:
    """恢复计划 —— "下一步该干什么"的显式表达。

    ## 为什么要有这个对象

    让 ``resume_from`` 直接返回 ``True/False`` 看着简单，但会把
    三个不同的信息压成一个布尔值：**现场还有效吗、该干什么、
    为什么**。调用方拿到 ``False`` 时根本分不清是"页面跑了"
    还是"人选择放弃" —— 而这两种情况该做的事完全不同。

    这个对象把三件事分开放在 ``outcome`` / ``strategy`` / ``reason``
    三个字段里，调用方一眼就能分流。
    """

    #: 是否可以继续（``outcome == RESUME_OK`` 时为 True）。
    ok: bool = False

    #: 具体动作：``"skip"`` / ``"retry"`` / ``"abort"``。
    strategy: str = DEFAULT_RESUME_STRATEGY

    #: 结果分类：见 :data:`RESUME_OK` / :data:`RESUME_STALE` /
    #: :data:`RESUME_ABORTED`。
    outcome: str = RESUME_STALE

    #: 人类可读的原因（失效时是诊断信息，成功时是"从哪接回去"）。
    reason: str = ""

    #: 该怎么执行 —— 给调用方的操作提示（不是本模块去执行）。
    #: ``"retry_step"`` / ``"skip_step"`` / ``"raise_abort"`` / ``""``
    action_hint: str = ""

    #: 当前步骤名（从 context 带出）。
    current_step: str = ""

    #: 重试计数（从 context 带出，供调用方判断是否升级为 abort）。
    attempt: int = 0

    #: 原始上下文（没有则 None）。
    context: Optional[InterventionContext] = None

    #: 补充信息（失效原因细节、URL 变化前后等）。
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "strategy": self.strategy,
            "outcome": self.outcome,
            "reason": self.reason,
            "action_hint": self.action_hint,
            "current_step": self.current_step,
            "attempt": int(self.attempt or 0),
            "extra": self.extra if isinstance(self.extra, dict) else {},
        }

    def describe(self) -> str:
        head = {"ok": "✓ 可恢复", "stale": "✗ 现场失效",
                "aborted": "■ 已中止"}.get(self.outcome, "? " + self.outcome)
        bits = [head, self.strategy]
        if self.current_step:
            bits.append("@%s" % self.current_step)
        if self.attempt:
            bits.append("第%d次" % self.attempt)
        if self.action_hint:
            bits.append("→ %s" % self.action_hint)
        return " ".join(bits) + (("  " + self.reason) if self.reason else "")


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def validate_context(context: Any,
                     *,
                     current_url: Optional[str] = None,
                     require_url: bool = False,
                     error_hook: Optional[Callable[[str], None]] = None,
                     ) -> Tuple[bool, str]:
    """校验现场是否还能恢复。返回 ``(有效?, 原因)``。

    **不抛异常**（硬约束 H）。所有失败都变成 ``(False, 原因)``。

    校验三件事：

    1. ``context`` 本身能不能认出来（None / 垃圾 dict / 类型不对 → 失效）
    2. hwnd 还在不在（委托给 :meth:`InterventionContext.is_stale`）
    3. URL 还匹不匹配（同上）

    ``current_url`` 是"**现在**的 URL"，由调用方重新读一次传进来。
    本模块**不自己去读** —— 那需要 CDP 会话或 UIA，属于阶段四/二
    的能力，恢复层不该反向依赖它们。

    ## 为什么要把 error_hook 做成参数

    校验失败的原因有时候很重要（比如"探测本身抛异常了"），
    但返回值只能带一个字符串。调用方想记日志的话，
    通过这些 hook 拿到细节 —— 本模块不打日志，保持纯函数。
    """
    def _fail(msg: str) -> Tuple[bool, str]:
        if error_hook is not None:
            try:
                error_hook(msg)
            except Exception:      # noqa: BLE001
                pass
        return False, msg

    # ---- 1) context 本身 ----
    ctx = context
    if ctx is None:
        return _fail("context_missing")

    if isinstance(ctx, dict):
        try:
            ctx = InterventionContext.from_dict(ctx)
        except Exception as exc:      # noqa: BLE001
            return _fail("context_dict_bad: %s" % type(exc).__name__)

    if not isinstance(ctx, InterventionContext):
        return _fail("context_type_bad: %s" % type(ctx).__name__)

    # ---- 2) + 3) 委托给 is_stale ----
    try:
        stale, why = ctx.is_stale(url=current_url, require_url=require_url)
    except Exception as exc:      # noqa: BLE001
        return _fail("stale_check_failed: %s: %s" % (type(exc).__name__, exc))

    if stale:
        return _fail(why or "stale")

    return True, ""


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def resume_from(context: Any = None,
                strategy: Optional[str] = None,
                *,
                current_url: Optional[str] = None,
                require_url: bool = False,
                attempt: Optional[int] = None,
                max_attempt: Optional[int] = None,
                on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                ) -> ResumePlan:
    """从熔断点恢复。返回 :class:`ResumePlan`。

    ## 参数

    - ``context`` —— :class:`InterventionContext` 或它的 ``to_dict()``。
    - ``strategy`` —— ``"skip"`` / ``"retry"`` / ``"abort"``。
      **不传则读 ``context.resume_strategy``**，它也没设时用
      :data:`context.DEFAULT_RESUME_STRATEGY`（``"retry"``）。
    - ``current_url`` —— 现在的 URL，用于校验现场。不传就跳过 URL 校验。
    - ``max_attempt`` —— 重试上限。**调用方决定**；给了的话，
      ``context.attempt >= max_attempt`` 时把 retry 升级为 abort。
      本模块不发明默认上限（那是个业务决定，不是技术决定）。
    - ``on_event`` —— 恢复事件回调（用于打点/日志）。
      回调抛异常会被吞掉 —— 打点不该拖垮恢复。

    ## 返回 vs 抛异常

    - 现场失效 → 返回 ``outcome=RESUME_STALE`` 的 plan，**不抛**（硬约束 H）
    - 策略是 abort → **抛** :class:`AbortSignal`
      （这是"任务终止"的正确表达 —— 让调用栈自然展开，
      而不是返回一个"其实该终止"的对象等调用方自觉处理）

    ## 为什么 abort 要抛而不是返回

    因为"任务转入 Blocked 状态"必须**穿透所有中间的 try/except**。
    如果返回一个 plan 让调用方自己检查，中间任何一层忘了检查，
    流程就会继续往下跑到不该跑的地方。抛异常是唯一可靠的表达。
    """
    plan = ResumePlan()
    plan.strategy = _norm_strategy(strategy, context)
    ctx: Optional[InterventionContext] = None

    # ---- 1) 规整 context ----
    try:
        if isinstance(context, dict):
            ctx = InterventionContext.from_dict(context)
        elif isinstance(context, InterventionContext):
            ctx = context
        elif context is None:
            ctx = None
        else:
            # 认不出来 —— 当作失效
            plan.outcome = RESUME_STALE
            plan.reason = "context_type_bad: %s" % type(context).__name__
            plan.extra["raw_context"] = repr(context)[:200]
            _emit(on_event, plan)
            return plan
    except Exception as exc:      # noqa: BLE001
        plan.outcome = RESUME_STALE
        plan.reason = "context_parse_failed: %s: %s" % (type(exc).__name__, exc)
        _emit(on_event, plan)
        return plan

    plan.context = ctx
    if ctx is not None:
        plan.current_step = ctx.current_step
        plan.attempt = int(ctx.attempt or 0) if attempt is None else int(attempt)
        # strategy 未显式指定时，用 context 里的
        if strategy is None:
            plan.strategy = ctx.strategy

    # ---- 2) 校验现场 ----
    valid, why = validate_context(ctx, current_url=current_url,
                                  require_url=require_url)
    if not valid:
        plan.ok = False
        plan.outcome = RESUME_STALE
        plan.reason = why
        plan.action_hint = ""
        if ctx is not None and ctx.url and current_url is not None:
            plan.extra["url_before"] = ctx.url
            plan.extra["url_now"] = str(current_url)
        _emit(on_event, plan)
        return plan

    # ---- 3) 重试上限 → 升级为 abort ----
    if (plan.strategy == "retry" and max_attempt is not None
            and plan.attempt >= int(max_attempt)):
        plan.strategy = "abort"
        plan.extra["escalated"] = (
            "attempt=%d 已达上限 max_attempt=%d，retry 升级为 abort"
            % (plan.attempt, int(max_attempt)))

    # ---- 4) 分派 ----
    if plan.strategy == "abort":
        plan.ok = False
        plan.outcome = RESUME_ABORTED
        plan.action_hint = "raise_abort"
        plan.reason = plan.extra.get("escalated") or "策略指定中止"
        _emit(on_event, plan)
        raise AbortSignal("恢复策略中止：%s" % plan.reason, context=ctx,
                          plan=plan.to_dict())

    if plan.strategy == "skip":
        plan.ok = True
        plan.outcome = RESUME_OK
        plan.action_hint = "skip_step"
        plan.reason = "跳过当前步骤，继续下一跳"
        _emit(on_event, plan)
        return plan

    # retry（默认）
    plan.ok = True
    plan.outcome = RESUME_OK
    plan.action_hint = "retry_step"
    plan.reason = "从当前步骤重试"
    _emit(on_event, plan)
    return plan


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _norm_strategy(strategy: Any, context: Any) -> str:
    """归一化策略：显式参数 > context > 默认。非法值退回默认。"""
    cand = strategy
    if cand is None:
        if isinstance(context, InterventionContext):
            cand = context.resume_strategy
        elif isinstance(context, dict):
            cand = context.get("resume_strategy")
    s = str(cand or "").strip().lower()
    return s if s in RESUME_STRATEGIES else DEFAULT_RESUME_STRATEGY


def _emit(on_event: Optional[Callable[[Dict[str, Any]], None]],
          plan: ResumePlan) -> None:
    """触发事件回调。**回调抛异常会被吞掉** —— 打点不该拖垮恢复。"""
    if on_event is None:
        return
    try:
        rec = plan.to_dict()
        rec["at"] = time.time()
        on_event(rec)
    except Exception:      # noqa: BLE001
        pass


def describe_strategies() -> str:
    """给人看的策略说明。"""
    return "\n".join([
        "恢复策略（context.resume_strategy）：",
        "  retry  从当前步骤重试（默认）—— 人解掉验证题后，这一步要重走",
        "  skip   跳过当前步骤，标记 skipped —— 人已在别处把事办了",
        "  abort  任务转 Blocked，抛 AbortSignal —— 人判断过不去",
        "",
        "校验（validate_context）：hwnd 是否存活、URL 是否匹配；",
        "失效时返回 (False, 原因)，不抛异常。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """恢复层自检。必须覆盖三种策略 + context 失效。

    ``quick=True``（默认）：纯逻辑，不碰真实桌面（
    只有一条"真实 hwnd"用例会枚举窗口，失败也不算错）。
    """
    import os
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ⚠ 走外部调用方路径：python -m 时模块有两份，裸名会绕过打桩/看到
    #    __main__ 的副本。全程用 _me.
    _me = _sys.modules[__name__]

    from .status import RiskStatus, Severity

    def _ctx(**kw) -> InterventionContext:
        base = dict(hwnd=0, url="https://a.com/step", current_step="step_1",
                    risk_status=RiskStatus.hit("slider",
                                               severity=Severity.INTERRUPT))
        base.update(kw)
        return InterventionContext(**base)

    # ============ 1) 默认策略 ============
    _add("默认策略是 retry",
         DEFAULT_RESUME_STRATEGY == "retry", DEFAULT_RESUME_STRATEGY)
    _add("三种合法策略齐备",
         set(RESUME_STRATEGIES) == {"skip", "retry", "abort"},
         RESUME_STRATEGIES)

    # ============ 2) retry ============
    p = _me.resume_from(_ctx(), "retry")
    _add("retry → ok=True, outcome=ok",
         p.ok is True and p.outcome == RESUME_OK, p.describe())
    _add("retry → action_hint='retry_step'",
         p.action_hint == "retry_step", p.action_hint)
    _add("retry → 带出 current_step",
         p.current_step == "step_1", p.current_step)
    _add("retry → 带出 context 引用", p.context is not None, "有")

    # ============ 3) skip ============
    p = _me.resume_from(_ctx(), "skip")
    _add("skip → ok=True, outcome=ok",
         p.ok is True and p.outcome == RESUME_OK, p.describe())
    _add("skip → action_hint='skip_step'",
         p.action_hint == "skip_step", p.action_hint)
    _add("skip 与 retry 的 hint 不同（可区分）",
         p.action_hint != "retry_step", "区分开了")

    # ============ 4) abort（抛 AbortSignal）============
    got = None
    try:
        _me.resume_from(_ctx(), "abort")
    except _me.AbortSignal as exc:
        got = exc
    except Exception as exc:      # noqa: BLE001
        _add("abort 抛 AbortSignal", False,
             "抛了别的：%s: %s" % (type(exc).__name__, exc))
    _add("abort → 抛 AbortSignal",
         got is not None, type(got).__name__ if got else "None")
    _add("...异常带 context",
         got is not None and isinstance(got.context, InterventionContext),
         "硬约束")
    _add("...异常 exit_code=5",
         got is not None and got.exit_code == 5,
         getattr(got, "exit_code", None))
    _add("...异常里带了 plan 快照",
         got is not None and "plan" in got.detail, sorted(got.detail) if got else [])
    _add("AbortSignal 不是 RiskBlocked（区分「人决定」与「闸门拦」）",
         not isinstance(got, __import__(
             "avatar.errors", fromlist=["RiskBlocked"]).RiskBlocked),
         "类型区分")

    # ============ 5) 策略来源优先级 ============
    p = _me.resume_from(_ctx(resume_strategy="skip"))
    _add("不传 strategy → 读 context.resume_strategy",
         p.strategy == "skip", p.strategy)
    p = _me.resume_from(_ctx(resume_strategy="skip"), "retry")
    _add("显式 strategy 覆盖 context",
         p.strategy == "retry", p.strategy)
    p = _me.resume_from(_ctx(resume_strategy="nonsense"))
    _add("context 里策略非法 → 退回默认 retry",
         p.strategy == "retry", p.strategy)
    p = _me.resume_from(_ctx(), "RETRY")
    _add("策略大小写不敏感", p.strategy == "retry", p.strategy)
    p = _me.resume_from(_ctx(), "  skip  ")
    _add("策略两侧空白无害", p.strategy == "skip", repr(p.strategy))
    p = _me.resume_from(_ctx(resume_strategy=""))
    _add("context 策略为空 → 默认 retry", p.strategy == "retry", p.strategy)

    # ============ 6) context 失效场景（硬约束 H）============
    # 6a) context 为 None
    p = _me.resume_from(None)
    _add("context=None → stale，不抛",
         p.ok is False and p.outcome == RESUME_STALE
         and p.reason == "context_missing", p.describe())

    # 6b) context 类型不对
    p = _me.resume_from("我不是 context")
    _add("context 类型不对 → stale，不抛",
         p.ok is False and p.outcome == RESUME_STALE,
         p.reason)
    _add("...原因里说明了实际类型",
         "str" in p.reason, p.reason)

    # 6c) hwnd 已不在
    p = _me.resume_from(_ctx(hwnd=0xDEADBEEF))
    _add("hwnd 已销毁 → stale/hwnd_gone（不抛，硬约束 H）",
         p.ok is False and p.outcome == RESUME_STALE
         and p.reason == "hwnd_gone", p.describe())
    _add("...action_hint 为空（失效时不给动作）",
         p.action_hint == "", repr(p.action_hint))

    # 6d) URL 变了
    p = _me.resume_from(_ctx(url="https://a.com/step"),
                        "retry", current_url="https://a.com/other")
    _add("URL 变化 → stale/url_changed",
         p.ok is False and p.reason == "url_changed", p.describe())
    _add("...extra 里带出前后 URL（诊断用）",
         p.extra.get("url_before") == "https://a.com/step"
         and p.extra.get("url_now") == "https://a.com/other",
         (p.extra.get("url_before"), p.extra.get("url_now")))

    # 6e) URL 只是 query 变了 → 仍有效
    p = _me.resume_from(_ctx(url="https://a.com/step?t=1"),
                        "retry", current_url="https://a.com/step?t=2")
    _add("仅 query 变化不算失效（一次性 token）",
         p.ok is True, p.describe())

    # 6f) require_url 但没有 url
    p = _me.resume_from(_ctx(url=""), "retry", require_url=True)
    _add("require_url 且 url 为空 → stale",
         p.ok is False and p.reason == "url_missing", p.describe())

    # 6g) 垃圾 dict
    p = _me.resume_from({"hwnd": "not-an-int", "url": 12345})
    _add("垃圾 dict 不抛，给 stale 或 ok",
         isinstance(p, _me.ResumePlan), p.describe())

    # 6h) 失效时**不抛异常**（硬约束 H 的核心）
    no_raise = True
    for bad in (None, "x", 42, [], {}, {"hwnd": None}):
        try:
            _me.resume_from(bad)
        except _me.AbortSignal:
            pass          # abort 是策略，不是失效
        except Exception as exc:      # noqa: BLE001
            no_raise = False
            _add("失效场景不抛异常", False,
                 "%r → %s: %s" % (bad, type(exc).__name__, exc))
            break
    if no_raise:
        _add("★ 六种垃圾 context 全部不抛（硬约束 H）",
             True, "None/str/int/list/空dict/None字段")

    # ============ 7) validate_context 直接调用 ============
    ok, why = _me.validate_context(_ctx())
    _add("validate_context 有效 → (True, '')",
         ok is True and why == "", (ok, why))
    ok, why = _me.validate_context(None)
    _add("validate_context(None) → (False, 'context_missing')",
         ok is False and why == "context_missing", why)
    seen: List[str] = []
    ok, why = _me.validate_context(None, error_hook=seen.append)
    _add("error_hook 收到原因", seen == ["context_missing"], seen)

    def _boom_hook(msg):
        raise RuntimeError("hook 坏了")
    ok, why = _me.validate_context(None, error_hook=_boom_hook)
    _add("error_hook 自己抛异常也不影响校验（不拖垮）",
         ok is False and why == "context_missing", "隔离")

    # ============ 8) 重试上限升级 ============
    esc_got = None
    try:
        _me.resume_from(_ctx(attempt=3), "retry", max_attempt=3)
    except _me.AbortSignal as exc:
        esc_got = exc
    _add("attempt 达上限 → retry 升级为 abort",
         esc_got is not None, type(esc_got).__name__ if esc_got else "None")
    _add("...升级原因写进 plan.extra",
         esc_got is not None and "escalated" in (
             esc_got.detail.get("plan", {}).get("extra", {})),
         (esc_got.detail.get("plan", {}).get("extra", {})
          .get("escalated", "")[:56]) if esc_got else "")

    p = _me.resume_from(_ctx(attempt=1), "retry", max_attempt=3)
    _add("未达上限 → 正常 retry 不升级",
         p.ok is True and p.strategy == "retry", p.describe())
    p = _me.resume_from(_ctx(attempt=9), "retry")
    _add("不传 max_attempt → 不升级（上限是调用方的决定）",
         p.ok is True and p.strategy == "retry", p.describe())
    p = _me.resume_from(_ctx(attempt=9), "skip", max_attempt=1)
    _add("skip 不受 max_attempt 影响（只约束 retry）",
         p.ok is True and p.strategy == "skip", p.describe())
    p = _me.resume_from(_ctx(attempt=2), "retry", attempt=0, max_attempt=3)
    _add("显式 attempt 参数覆盖 context.attempt",
         p.ok is True and p.attempt == 0, p.attempt)

    # ============ 9) 事件回调 ============
    events: List[Dict[str, Any]] = []
    _me.resume_from(_ctx(), "retry", on_event=events.append)
    _add("on_event 收到一条事件",
         len(events) == 1 and events[0].get("outcome") == RESUME_OK,
         events[:1])
    _add("事件里带时间戳", "at" in events[0], "可打点")

    def _bad_event(_rec):
        raise RuntimeError("打点坏了")
    ok_after = False
    try:
        p = _me.resume_from(_ctx(), "retry", on_event=_bad_event)
        ok_after = p.ok
    except Exception as exc:      # noqa: BLE001
        _add("on_event 抛异常不拖垮恢复", False, str(exc))
    _add("on_event 抛异常不拖垮恢复（打点不该拖垮业务）",
         ok_after is True, "隔离")

    events2: List[Dict[str, Any]] = []
    try:
        _me.resume_from(_ctx(), "abort", on_event=events2.append)
    except _me.AbortSignal:
        pass
    _add("abort 路径也发事件（可审计）",
         len(events2) == 1 and events2[0].get("outcome") == RESUME_ABORTED,
         events2[:1])

    # ============ 10) plan 序列化 ============
    p = _me.resume_from(_ctx(), "skip")
    d = p.to_dict()
    _add("ResumePlan.to_dict 字段齐",
         all(k in d for k in ("ok", "strategy", "outcome", "reason",
                              "action_hint", "current_step", "attempt")),
         sorted(d))
    _add("to_dict 可 json.dumps",
         isinstance(__import__("json").dumps(d), str), "JSON 安全")
    _add("describe 输出含结果分类与策略",
         "可恢复" in p.describe() and "skip" in p.describe(),
         p.describe())
    _add("空 plan 的 describe 不炸",
         isinstance(_me.ResumePlan().describe(), str), "安全")

    # ============ 11) 校验真的走了 is_stale（接口没被绕过）============
    real_hwnds: List[int] = []
    try:
        from ..app_manager import windows as W
        real_hwnds = [int(w.hwnd) for w in W.list_windows()][:1]
    except Exception:      # noqa: BLE001
        pass
    if real_hwnds:
        p = _me.resume_from(_ctx(hwnd=real_hwnds[0]), "retry")
        _add("真实存活 hwnd → 可恢复",
             p.ok is True, "hwnd=%d" % real_hwnds[0])
    else:
        checks.append({"name": "真实存活 hwnd → 可恢复", "ok": True,
                       "detail": "(本机无窗口，跳过)"})

    # ============ 12) 硬约束：不重放动作 / 不猜意图 ============
    import ast as _ast
    import io as _io
    src = _io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    top_imports: List[str] = []
    for node in tree.body:
        if isinstance(node, _ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            top_imports.append(node.module or "")
    _add("顶层不 import 动作层（不重放鼠标键盘）",
         not any(("actions" in m) or ("input" in m) or ("dispatcher" in m)
                 for m in top_imports), top_imports)
    _add("顶层不 import 视觉/OCR（不猜页面内容）",
         not any(("vision" in m) or ("ocr" in m) for m in top_imports),
         top_imports)
    _add("顶层不 import web_cdp / uia（不反读页面）",
         not any(("web_cdp" in m) or ("uia" in m) for m in top_imports),
         top_imports)
    top_classes = [n.name for n in tree.body
                   if isinstance(n, _ast.ClassDef)]
    _add("顶层只有计划对象（无生产用执行类）",
         all(c.startswith("_") or c == "ResumePlan" for c in top_classes),
         top_classes)
    _add("模块 docstring 含'能力边界'一节",
         "能力边界" in (_me.__doc__ or ""), "硬约束 I")

    # 不硬编码重试上限之类魔法数字。
    # ⚠ 必须**先剥掉字符串常量再查** —— 上面这条断言的文字里就写着
    #   "MAX_RETRY"，不剥的话永远红。（5B 里踩过同一个坑。）
    def _code_only(s: str) -> str:
        tr = _ast.parse(s)
        drop = set()
        for node in _ast.walk(tr):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                for ln in range(getattr(node, "lineno", 0) or 0,
                                (getattr(node, "end_lineno", 0) or 0) + 1):
                    drop.add(ln)
        return "\n".join(l.split("#", 1)[0] for i, l in enumerate(
            s.split("\n"), 1) if i not in drop)

    code = _code_only(src)
    _add("模块内没有硬编码的重试上限常量",
         not any(("MAX_RETRY" in ln) or ("DEFAULT_MAX_ATTEMPT" in ln)
                 for ln in code.split("\n")), "上限由调用方传")

    # ============ 13) describe_strategies 不炸 ============
    try:
        txt = _me.describe_strategies()
        _add("describe_strategies 列出三种策略",
             all(s in txt for s in ("retry", "skip", "abort")),
             txt.split("\n")[0])
    except Exception as exc:      # noqa: BLE001
        _add("describe_strategies", False, str(exc))

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-52s %s" % ("[OK]" if c["ok"] else "[FAIL]", c["name"], c["detail"]))
    n = len(r["checks"])
    print("resume 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
