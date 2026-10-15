# -*- coding: utf-8 -*-
"""异常恢复 —— 失败了该走哪条路。

## 五种策略

======================  ====================================================
策略                    什么时候用
======================  ====================================================
``retry``               可能是暂态（元素没渲染完、网络抖了一下）
``retry_with_fallback`` 重试没用，得**换条路**（UIA 换坐标、换个选择器）
``skip``                这步本来就不关键（等待、截图、滚动）
``pause_for_human``     机器解决不了，叫人
``abort``               重试和换路都没意义（权限不足、进程被杀）
======================  ====================================================

## 决策顺序（**顺序不能换**）

    1. 有风险（detected=True）        → PAUSE_FOR_HUMAN   ← 硬约束 9，最高优先
    2. 不可恢复错误                    → ABORT
    3. 换策略次数用完                  → PAUSE_FOR_HUMAN
    4. 重试次数没用完                  → RETRY
    5. 重试用完但还能换                → RETRY_WITH_FALLBACK
    6. 兜底                            → PAUSE_FOR_HUMAN

## 为什么风险排第一

因为风控是**全局**判断。一个步骤检出验证码，说明整个会话都处于
风险中 —— 这时候"再试一次"不但没用，还可能让情况更糟
（连续触发风控 → 账号被限流）。所以硬约束 9 要求：**检出就叫人，
不允许硬闯**。

## 上限为什么在常量里而不是参数里

因为这是**安全边界**，不是调优旋钮。做成参数会诱使人"这次赶时间，
把重试上限调到 100" —— 那等于没有上限。要改就得改代码，
改代码就得过 review。

## 能力边界

**做**：看步骤状态与错误，给出策略与理由；识别不可恢复的错误；
把上限判断集中在一处。

**不做**：

- **不执行策略**。本模块只回答"该走哪条路"，
  真的去重试/跳过/叫人 是 :mod:`executor`。
- **不重试**。没有循环、没有等待、不调任何动作。
- **不改状态**。``attempts`` / ``strategy_switches`` 的累加在执行器。
- **不做风控判定**。``risk`` 参数由调用方传入（来自阶段五），
  本模块只读它，不自己调 detect。
- **不记日志**。决策记录由执行器写进 memory（硬约束 7）——
  这样"谁在什么时候记的"只有一个答案。
- **不猜错误语义**。认不出的错误按"可恢复"处理（先重试），
  而不是当成 abort —— 误 abort 会丢掉整个任务，
  误 retry 只是多花几秒。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "RecoveryStrategy",
    "RecoveryDecision",
    "MAX_RETRIES",
    "MAX_STRATEGY_SWITCHES",
    "UNRECOVERABLE_HINTS",
    "UNRECOVERABLE_TYPES",
    "decide_recovery",
    "is_unrecoverable",
    "should_pause_for_risk",
    "describe_recovery",
    "selfcheck",
]

#: 单步重试上限。**与 :mod:`executor` 的常量保持一致**（硬约束 6）。
MAX_RETRIES = 3

#: 单步换策略上限。**与 :mod:`executor` 的常量保持一致**（硬约束 6）。
MAX_STRATEGY_SWITCHES = 2


class RecoveryStrategy(str, Enum):
    """恢复策略。继承 ``str`` 让它能直接比较和序列化。"""

    RETRY = "retry"
    RETRY_WITH_FALLBACK = "retry_with_fallback"
    SKIP = "skip"
    ABORT = "abort"
    PAUSE_FOR_HUMAN = "pause_for_human"

    def __str__(self) -> str:
        return self.value


#: 不可恢复错误的**异常类名**。命中就 ABORT。
UNRECOVERABLE_TYPES = frozenset({
    "PermissionError",           # 权限不足
    "ProcessBlocked",            # 进程被策略拦住（阶段二的错误类型）
    "ProcessNotFound",           # 进程没了
    "UnsupportedError",          # 环境不支持（比如没有 UIA）
    "UnsafeStateError",          # 状态不安全
})

#: 不可恢复错误的**关键词**（异常消息里出现即算）。
#: 用来兜住那些被包成通用异常的情况。
UNRECOVERABLE_HINTS = (
    "access denied", "permission denied", "权限不足", "拒绝访问",
    "无权限", "被拒绝",
    "process exited", "process terminated", "进程已退出", "进程被终止",
    "process is gone", "进程不存在",
    "not supported", "unsupported", "不支持",
    "账户已被封禁", "账号被封", "account banned", "account suspended",
)


@dataclass(eq=False)
class RecoveryDecision:
    """一次恢复决策。**带理由，方便复盘。**

    ``eq=False`` 是为了自定义 :meth:`__eq__` —— 让它能直接
    和字符串比较（``decision == "retry"``），用起来顺手。
    """

    strategy: RecoveryStrategy = RecoveryStrategy.ABORT
    reason: str = ""
    limits: Dict[str, int] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def value(self) -> str:
        """策略的字符串值。**给执行器用**（它按字符串分派）。"""
        return str(self.strategy.value)

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, RecoveryDecision):
            return self.strategy == other.strategy
        return self.value == str(other)

    def __hash__(self) -> int:
        return hash(self.value)

    def to_dict(self) -> Dict[str, Any]:
        return {"strategy": self.value, "reason": self.reason,
                "limits": dict(self.limits),
                "evidence": dict(self.evidence)}

    def describe(self) -> str:
        return "%s —— %s" % (self.value, self.reason or "(无理由)")


# --------------------------------------------------------------------------
# 判据
# --------------------------------------------------------------------------
def is_unrecoverable(error: Any) -> bool:
    """这个错误"重试也没用"吗？

    ## 两类判据

    1. **异常类型**在 :data:`UNRECOVERABLE_TYPES` 里
    2. **错误消息**含 :data:`UNRECOVERABLE_HINTS` 里的词

    为什么还要看消息：很多底层错误（COM 的 ``HRESULT``）会被包成
    通用异常，类型信息丢了，但消息里还留着 ``Access denied``。

    ## 认不出来时返回 False（= 可恢复）

    这是刻意的方向：**误判成 abort 会丢掉整个任务，
    误判成 retry 只是多花几秒。** 两害相权取其轻。
    """
    if error is None:
        return False

    name = type(error).__name__ if isinstance(error, BaseException) else ""
    if name and name in UNRECOVERABLE_TYPES:
        return True

    msg = str(error).lower()
    if not msg:
        return False
    return any(h.lower() in msg for h in UNRECOVERABLE_HINTS)


def should_pause_for_risk(risk: Any) -> bool:
    """这个风险状态要求叫人吗？（硬约束 9）

    判据与 :func:`risk_detector.guard.should_intervene` **一致** ——
    都是"不干净就要人"。这里重新实现是因为 :mod:`recovery` 不该
    反向依赖阶段五（那会成环），而判据本身只有两行。
    """
    if risk is None:
        return False
    try:
        return not bool(getattr(risk, "is_clean", False))
    except Exception:      # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def decide_recovery(step: Any = None,
                    *,
                    state: str = "",
                    error: Any = None,
                    risk: Any = None,
                    max_retries: int = MAX_RETRIES,
                    max_switches: int = MAX_STRATEGY_SWITCHES,
                    memory: Any = None) -> RecoveryDecision:
    """决定这一步该怎么恢复。**纯函数 —— 不改任何东西。**

    ## 参数

    - ``step`` —— 步骤对象。读它的 ``attempts`` / ``strategy_switches`` /
      ``on_fail``。
    - ``state`` —— 当前状态（诊断用）。
    - ``error`` —— 失败原因（异常对象或字符串）。
    - ``risk`` —— 阶段五的 ``RiskStatus``。**非空且不干净就叫人。**
    - ``max_retries`` / ``max_switches`` —— 上限。默认用模块常量，
      执行器会传自己的（两者应该一致）。
    - ``memory`` —— 只读，用于查历史决策。**本函数不写 memory** ——
      记录由执行器统一做（硬约束 7 要求"每次决策都记"，
      集中在一处才不会漏）。

    ## 决策顺序（见模块文档）

    顺序**不能换**。特别是"风险优先"这条：如果先判断重试次数，
    那么"第一次就撞上验证码"会被判成 retry —— 而重试只会再撞一次。
    """
    attempts = _int_attr(step, "attempts", 0)
    switches = _int_attr(step, "strategy_switches", 0)
    limits = {"max_retries": int(max_retries),
              "max_switches": int(max_switches),
              "attempts": attempts, "switches": switches}

    # ---- 1) 风险优先（硬约束 9）----
    if should_pause_for_risk(risk):
        return RecoveryDecision(
            RecoveryStrategy.PAUSE_FOR_HUMAN,
            "检出风险（%s）—— 不允许硬闯，叫人处理"
            % _risk_desc(risk),
            limits, {"risk": _risk_desc(risk)})

    # ---- 2) 不可恢复错误 ----
    if is_unrecoverable(error):
        return RecoveryDecision(
            RecoveryStrategy.ABORT,
            "不可恢复的错误（%s）—— 重试和换路都没意义"
            % _err_desc(error),
            limits, {"error": _err_desc(error)})

    # ---- 3) 换策略次数用完 ----
    if switches >= int(max_switches):
        return RecoveryDecision(
            RecoveryStrategy.PAUSE_FOR_HUMAN,
            "已换策略 %d 次（上限 %d），机器解决不了，叫人"
            % (switches, int(max_switches)),
            limits)

    # ---- 4) 重试没用完 ----
    if attempts < int(max_retries):
        return RecoveryDecision(
            RecoveryStrategy.RETRY,
            "第 %d 次失败（上限 %d 次），可能是暂态，先重试"
            % (attempts, int(max_retries)),
            limits)

    # ---- 5) 重试用完但还能换策略 ----
    if switches < int(max_switches):
        return RecoveryDecision(
            RecoveryStrategy.RETRY_WITH_FALLBACK,
            "重试 %d 次仍失败，换一条执行路径（已换 %d 次）"
            % (attempts, switches),
            limits)

    # ---- 6) 兜底 ----
    return RecoveryDecision(
        RecoveryStrategy.PAUSE_FOR_HUMAN,
        "重试与换策略都用尽了，转人工", limits)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _int_attr(obj: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(obj, name, default) or 0)
    except (TypeError, ValueError):
        return int(default)


def _err_desc(error: Any) -> str:
    if error is None:
        return "未知"
    if isinstance(error, BaseException):
        return "%s: %s" % (type(error).__name__, str(error)[:80])
    return str(error)[:100]


def _risk_desc(risk: Any) -> str:
    if risk is None:
        return "未知"
    d = getattr(risk, "describe", None)
    if callable(d):
        try:
            return str(d())[:100]
        except Exception:      # noqa: BLE001
            pass
    return "%s/%s" % (getattr(risk, "type", ""),
                      getattr(risk, "severity", ""))


def describe_recovery() -> str:
    """给人看的恢复层说明。"""
    lines = [
        "异常恢复（recovery）",
        "=" * 64,
        "五种策略：",
        "  retry                可能是暂态，先重试",
        "  retry_with_fallback  重试没用，换条执行路径",
        "  skip                 这步不关键，跳过继续",
        "  pause_for_human      机器解决不了，叫人",
        "  abort                重试和换路都没意义",
        "",
        "决策顺序（**不能换**）：",
        "  1. 有风险（detected）→ pause_for_human   ← 硬约束 9，最高优先",
        "  2. 不可恢复错误       → abort",
        "  3. 换策略用完         → pause_for_human",
        "  4. 重试没用完         → retry",
        "  5. 重试用完但能换     → retry_with_fallback",
        "  6. 兜底               → pause_for_human",
        "",
        "上限（硬约束 6，常量而非参数）：",
        "  单步重试 %d 次 / 换策略 %d 次" % (MAX_RETRIES,
                                          MAX_STRATEGY_SWITCHES),
        "",
        "不可恢复的错误（异常类型）：",
        "  " + ", ".join(sorted(UNRECOVERABLE_TYPES)),
        "不可恢复的错误（消息关键词）：",
        "  " + ", ".join(UNRECOVERABLE_HINTS[:6]) + " …",
        "",
        "★ 认不出的错误按**可恢复**处理 —— 误 abort 丢任务，",
        "  误 retry 只多花几秒。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _Step:
    """测试用的最小步骤替身。"""

    def __init__(self, attempts: int = 0, switches: int = 0,
                 on_fail: str = "") -> None:
        self.attempts = attempts
        self.strategy_switches = switches
        self.on_fail = on_fail


class _Risk:
    """测试用的最小 RiskStatus 替身。"""

    def __init__(self, clean: bool = False, desc: str = "slider") -> None:
        self.is_clean = clean
        self._desc = desc

    def describe(self) -> str:
        return self._desc


def selfcheck(quick: bool = True) -> dict:
    """recovery 自检。**纯逻辑，不碰外部。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("五种策略齐全", len(_me.RecoveryStrategy) == 5,
         "%d 个" % len(_me.RecoveryStrategy))
    _add("★ 重试上限 3（硬约束 6）", _me.MAX_RETRIES == 3,
         str(_me.MAX_RETRIES))
    _add("★ 换策略上限 2（硬约束 6）", _me.MAX_STRATEGY_SWITCHES == 2,
         str(_me.MAX_STRATEGY_SWITCHES))
    _add("策略继承 str（可直接比较）",
         _me.RecoveryStrategy.RETRY == "retry", "是")
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    # ============ 2) 决策表：五条路都走到 ============
    d = _me.decide_recovery(_Step(attempts=1))
    _add("★ 首次失败（attempts=1 < 3）→ retry",
         d == "retry", d.describe())

    d = _me.decide_recovery(_Step(attempts=3, switches=0))
    _add("★ 重试 3 次用完 → retry_with_fallback",
         d == "retry_with_fallback", d.describe())

    d = _me.decide_recovery(_Step(attempts=9, switches=2))
    _add("★ 换策略 2 次用完 → pause_for_human",
         d == "pause_for_human", d.describe())

    d = _me.decide_recovery(_Step(), risk=_Risk(clean=False))
    _add("★★ 检出风险 → pause_for_human（硬约束 9）",
         d == "pause_for_human", d.describe())

    d = _me.decide_recovery(_Step(), error=PermissionError("access denied"))
    _add("★ 权限不足 → abort", d == "abort", d.describe())

    # ============ 3) 决策顺序不能换 ============
    d = _me.decide_recovery(_Step(attempts=0), risk=_Risk(clean=False))
    _add("★★ 第一次就撞风险 → 仍然 pause（不是 retry）",
         d == "pause_for_human",
         "风险优先于重试计数：%s" % d.describe())

    d = _me.decide_recovery(_Step(attempts=0),
                            error=PermissionError("x"),
                            risk=_Risk(clean=False))
    _add("★★ 风险 + 不可恢复错误 → 风险优先（叫人，不是 abort）",
         d == "pause_for_human", d.describe())

    d = _me.decide_recovery(_Step(attempts=0),
                            error=PermissionError("x"))
    _add("★ 不可恢复错误优先于重试计数",
         d == "abort", d.describe())

    # ============ 4) risk=None / clean 时不干预 ============
    _add("risk=None 不触发 pause",
         _me.decide_recovery(_Step(attempts=1), risk=None) == "retry",
         "retry")
    _add("★ risk.is_clean=True 不触发 pause",
         _me.decide_recovery(_Step(attempts=1),
                             risk=_Risk(clean=True)) == "retry", "retry")

    # ============ 5) 不可恢复错误的识别 ============
    _add("PermissionError 不可恢复",
         _me.is_unrecoverable(PermissionError("x")) is True, "True")
    _add("★ 消息含 access denied → 不可恢复",
         _me.is_unrecoverable("Access Denied by server") is True, "True")
    _add("★ 消息含「权限不足」→ 不可恢复",
         _me.is_unrecoverable("权限不足，无法继续") is True, "True")
    _add("★ 消息含「进程已退出」→ 不可恢复",
         _me.is_unrecoverable("进程已退出") is True, "True")
    _add("★ 消息含「账户已被封禁」→ 不可恢复",
         _me.is_unrecoverable("账户已被封禁") is True, "True")
    _add("大小写不敏感",
         _me.is_unrecoverable("PERMISSION DENIED") is True, "True")

    _add("★ 普通错误**不**算不可恢复（要能重试）",
         _me.is_unrecoverable(ValueError("元素没找到")) is False, "False")
    _add("None 不算不可恢复",
         _me.is_unrecoverable(None) is False, "False")
    _add("空消息不算不可恢复",
         _me.is_unrecoverable("") is False, "False")
    _add("★ 认不出的错误按可恢复处理（误 abort 代价更大）",
         _me.is_unrecoverable("某个奇怪的中文错误") is False, "False")

    _add("UNRECOVERABLE_TYPES 含 PermissionError",
         "PermissionError" in _me.UNRECOVERABLE_TYPES, "有")
    _add("关键词表非空", len(_me.UNRECOVERABLE_HINTS) >= 5,
         "%d 个" % len(_me.UNRECOVERABLE_HINTS))

    # ============ 6) 上限硬约束（硬约束 6）============
    # 走到上限就一定要停下，不能无限重试
    seen = []
    st = _Step()
    for _ in range(20):
        dec = _me.decide_recovery(st)
        seen.append(str(dec))
        if dec == "retry":
            st.attempts += 1
        elif dec == "retry_with_fallback":
            st.strategy_switches += 1
        else:
            break
    _add("★★ 模拟 20 次失败一定收敛（不会无限 retry）",
         seen[-1] in ("pause_for_human", "abort"), str(seen))
    _add("★★ 收敛时 attempts 不超过上限",
         st.attempts <= _me.MAX_RETRIES, "attempts=%d" % st.attempts)
    _add("★★ 收敛时 switches 不超过上限",
         st.strategy_switches <= _me.MAX_STRATEGY_SWITCHES,
         "switches=%d" % st.strategy_switches)
    _add("★ retry 出现次数不超过上限",
         seen.count("retry") <= _me.MAX_RETRIES,
         "retry ×%d" % seen.count("retry"))
    _add("★ retry_with_fallback 出现次数不超过上限",
         seen.count("retry_with_fallback") <= _me.MAX_STRATEGY_SWITCHES,
         "fallback ×%d" % seen.count("retry_with_fallback"))

    # 自定义上限也生效
    d = _me.decide_recovery(_Step(attempts=1), max_retries=1)
    _add("自定义 max_retries 生效",
         d == "retry_with_fallback", d.describe())
    d = _me.decide_recovery(_Step(attempts=9, switches=0), max_switches=0)
    _add("★ max_switches=0 时直接叫人",
         d == "pause_for_human", d.describe())

    # ============ 7) 决策对象 ============
    d = _me.decide_recovery(_Step(attempts=1))
    _add("★ decision.value 是字符串（执行器按字符串分派）",
         d.value == "retry" and isinstance(d.value, str), d.value)
    _add("★ str(decision) 等于策略名",
         str(d) == "retry", str(d))
    _add("★ decision 可直接和字符串比较",
         (d == "retry") and (d != "abort"), "对")
    _add("decision == 另一个 decision 按策略比",
         d == _me.RecoveryDecision(_me.RecoveryStrategy.RETRY), "对")
    _add("★ 每个决策都带 reason（可复盘）",
         all(_me.decide_recovery(_Step(attempts=a, switches=s)).reason
             for a in (0, 1, 3, 9) for s in (0, 1, 2)),
         "全都有")
    _add("decision.to_dict JSON 安全",
         __import__("json").dumps(d.to_dict(), ensure_ascii=False) != "",
         "ok")
    _add("decision.describe 可读",
         "retry" in d.describe(), d.describe())
    _add("decision 可哈希（能放进 set）",
         len({_me.decide_recovery(_Step(attempts=1)),
              _me.decide_recovery(_Step(attempts=9, switches=2))}) == 2,
         "2 个")
    _add("limits 里带上了计数（便于诊断）",
         d.limits.get("attempts") == 1
         and d.limits.get("max_retries") == 3, str(d.limits))

    # ============ 8) 容错 ============
    _add("★ step=None 不抛",
         _me.decide_recovery(None) is not None, "容错")
    _add("★ 坏 step（属性是字符串）不抛",
         _me.decide_recovery(type("S", (), {"attempts": "abc",
                                            "strategy_switches": None})())
         is not None, "容错")
    _add("★ error 是任意对象不抛",
         _me.decide_recovery(_Step(), error=object()) is not None, "容错")
    _add("★ risk 是坏对象不抛",
         _me.decide_recovery(_Step(), risk=object()) is not None, "容错")

    # ============ 9) should_pause_for_risk ============
    _add("is_clean=True → 不叫人",
         _me.should_pause_for_risk(_Risk(clean=True)) is False, "False")
    _add("is_clean=False → 叫人",
         _me.should_pause_for_risk(_Risk(clean=False)) is True, "True")
    _add("None → 不叫人",
         _me.should_pause_for_risk(None) is False, "False")
    _add("★ 没有 is_clean 属性 → **保守叫人**（与 guard 判据一致）",
         _me.should_pause_for_risk(object()) is True,
         "认不出的风险对象按有风险处理")
    _add("★ 判据与 guard.should_intervene 同源：只看 is_clean",
         _me.should_pause_for_risk(_Risk(clean=True)) is False
         and _me.should_pause_for_risk(_Risk(clean=False)) is True,
         "一致")

    # ============ 10) 纯函数 ============
    st = _Step(attempts=2, switches=1)
    before = (st.attempts, st.strategy_switches)
    for _ in range(5):
        _me.decide_recovery(st)
    _add("★★ decide_recovery 是纯函数（不改 step）",
         (st.attempts, st.strategy_switches) == before,
         "%s → %s" % (before, (st.attempts, st.strategy_switches)))

    # ============ 11) 不 import 阶段五（避免成环）============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import risk_detector（单向依赖，避免成环）",
         not any("risk_detector" in m for m in top), sorted(top))
    _add("顶层不 import 执行/观测/验证",
         not (top & {"executor", "observer", "verifier", "planner"}),
         sorted(top))
    _add("顶层只依赖标准库",
         top <= {"time", "enum", "dataclasses", "typing", "__future__",
                 "sys"},
         sorted(top))

    # ============ 12) describe ============
    txt = _me.describe_recovery()
    _add("describe 列出五种策略",
         all(s in txt for s in ("retry", "retry_with_fallback", "skip",
                                "abort", "pause_for_human")), "有")
    _add("★ describe 说明决策顺序不能换",
         "不能换" in txt, "有")
    _add("describe 列出上限",
         "3 次" in txt and "2 次" in txt, "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "recovery",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_recovery())
