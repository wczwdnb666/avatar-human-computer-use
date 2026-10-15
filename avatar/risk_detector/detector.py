"""风险检测统一入口 —— 三层降级编排。

## 唯一入口

    st = detect(hwnd=None, cdp_session=None)   # → RiskStatus

调用方（5C 的 guard、5E 的主流程）**只认这一个函数**。
三层的存在是实现细节：今天可能是 DOM→窗口→像素，明天加一层
"网络特征识别"，调用方的代码一行都不用改。

## 降级规则（硬约束，逐条可测）

1. **任一层命中就立刻返回** —— 不再往下跑。像素层是 OCR，
   几百毫秒起步；DOM 已经抓到滑块了就没必要再去认字。
2. **未命中才降级到下一层**。"未命中"包括两种情况：
   ``is_clean``（真的干净）和 ``is_unknown``（看不清）。
   —— 等等，这里有个反直觉的点，单独说，见第 3 条。
3. **unknown 也要继续往下跑**。如果 DOM 层说"我读不到页面"，
   不代表窗口层也读不到。每一层是**独立的观测手段**，
   上一层看不清 ≠ 这一层也看不清。
4. **异常绝不吞**。任何层抛异常 → 记进 ``evidence["errors"]``
   → **继续尝试下一层**。吞掉异常等于把"检测器坏了"伪装成
   "检测通过了"，这是最危险的失败模式。
5. **三层都没命中时，最终必须是 ``is_clean=False``**。
   全部 unknown 时返回 unknown；哪怕三层都报 clean，
   也**允许**返回 clean（那说明三路独立观测一致，可信）。
   —— 但只要有**任何一层**是 unknown，最终就是 unknown。
   不允许"两个不知道 + 一个干净 = 干净"，那是拿无知当证据。
6. **每层都有 timeout**，默认 3s。超时的层返回
   ``RiskStatus.timeout()``，**当作 unknown 处理**（继续降级）。
7. **层自身超时会挤压后面的层**：总预算不是"每层 3s × 3 = 9s"，
   而是**共享一个总预算**。DOM 层烧掉 2.5s，窗口层就只剩 0.5s。
   这是刻意的 —— 上层动作的体感延迟不该随检测层数线性增长。

## 顺序为什么是这个顺序

    DOM（毫秒）  →  窗口（10~50ms）  →  像素（0.3~3s）

按**成本**排，不按"准确率"排。贵的层先跑等于每次动作都付最贵的代价。
代价是像素层很少有机会跑到 —— 那正是目的。

## 返回值

``RiskStatus``，``source`` 标明"最终结论来自哪一层"（可能是
``"dom"`` / ``"window"`` / ``"pixel"`` / ``"merge"``）。
``evidence`` 里**一定**有：

    evidence["layers"]   → 每层跑了没跑、结论如何、花了多久
    evidence["errors"]   → 所有层抛的异常（没有则不存在）

这是"可解释"的底线：拿到一个 unknown，必须能看出是哪一层卡住了。

## 能力边界

**做**：三层检测的**编排** —— 分配预算、按序降级、合并结论、
把每层的原始结论挂进 ``evidence["layers"]``。

**不做**：

- **不自己检测**。三层各自在 :mod:`detector_dom` /
  :mod:`detector_window` / :mod:`detector_pixel` 里，本模块
  **按层名延迟 import**，未交付的层不阻断本模块的 import。
- **不硬解**（硬约束 #9）。本模块里没有一行识别验证题答案的代码。
- **不处置**。产出 ``RiskStatus`` 就结束 —— 暂停/终止是 :mod:`guard`。
- **不缓存**。同一秒内调两次 ``detect()`` 会真的跑两遍。
  缓存策略该由调用方按场景决定（动作前后对时效的要求不同）。
- **不重试**。某层失败就降级到下一层，不会重跑同一层。
- **不决定 severity**。那是 :mod:`fingerprints` 里逐条配的，
  本模块只搬运。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .status import (
    DEFAULT_RISK_TIMEOUT,
    RiskStatus,
    Severity,
    merge_status,
)

__all__ = [
    "LAYERS",
    "LAYER_ORDER",
    "DETECT_TOTAL_BUDGET",
    "MIN_LAYER_BUDGET",
    "detect",
    "detect_quick",
    "describe_layers",
    "selfcheck",
]


#: 层名 → (是否已实现)。检测器分批交付，未交付的层**跳过但不报错**。
LAYERS: Dict[str, bool] = {
    "dom": True,
    "window": True,
    "pixel": True,
}

#: 降级顺序。改这里就能改链路，不用碰别的地方。
LAYER_ORDER: Tuple[str, ...] = ("dom", "window", "pixel")

#: 总预算。**不是**每层预算 —— 三层共享这一个池子。
DETECT_TOTAL_BUDGET = 3.0

#: 单层最少也要给的预算。低于这个值就别跑了 ——
#: 给 5ms 让 OCR 跑，纯属浪费时间还制造假 timeout。
MIN_LAYER_BUDGET = 0.05

#: 各层的"期望耗时"，用于按比例分配总预算。
#: 数值来自实测：DOM 一次 evaluate 约 3~15ms，窗口枚举约 10~40ms，
#: OCR 一次约 300ms 起步。给像素层留大头，因为它最贵也最容易超。
_LAYER_EXPECT = {
    "dom": 0.15,
    "window": 0.10,
    "pixel": 2.00,
}


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def detect(hwnd: Optional[int] = None,
           cdp_session: Any = None,
           *,
           timeout: float = DETECT_TOTAL_BUDGET,
           session_id: Optional[str] = None,
           url: str = "",
           region: Optional[Sequence[int]] = None,
           extra_titles: Sequence[str] = (),
           extra_texts: Sequence[str] = (),
           only: Optional[Iterable[str]] = None,
           skip: Iterable[str] = (),
           include_invisible: bool = False,
           ) -> RiskStatus:
    """三层风险检测。**唯一入口**。

    参数
    ----
    - ``hwnd`` —— 当前操作的目标窗口句柄。可以不传（纯浏览器场景
      只给 ``cdp_session`` 也行，那时窗口层退化成全桌面扫描）。
    - ``cdp_session`` —— 阶段四的 ``CdpSession``。**鸭子类型**，
      不 import :mod:`avatar.web_cdp` —— 那会让本模块在没装
      CDP 依赖的环境里直接 import 失败。只需要它有
      ``evaluate(expression, ...)`` 方法。
    - ``timeout`` —— **总预算**（硬约束 #7 默认 3s）。三层共享。
    - ``session_id`` —— CDP 的 sessionId（若 session 已经 attach 过页面）。
    - ``url`` —— 已知的页面 URL。DOM 层拿它做路径判定，
      **比从页面里现读更可靠**（现读时页面可能还在跳转）。
    - ``region`` —— 像素层的目标区域 ``(x, y, w, h)``。不给就整窗。
    - ``extra_titles`` —— 注入窗口层的标题（如 CDP 读到的 document.title）。
    - ``extra_texts`` —— 注入像素层的文本（如 DOM 读到的可见文本）。
    - ``only`` —— **只跑**这些层（调试/单测用）。给了就忽略 ``skip``。
    - ``skip`` —— 跳过这些层。

    返回
    ----
    合并后的 ``RiskStatus``。

    ## 合并规则（在 :func:`status.merge_status` 基础上加了层预算逻辑）

    - 任一层 ``detected=True`` → **立刻返回**，不再往下跑
    - 全部跑完仍无命中 → ``merge_status()`` 合并，**有 unknown 就是 unknown**
    """
    t0 = time.perf_counter()

    # ---- 0) 预算与层选择 ----
    try:
        total = max(0.0, float(timeout))
    except (TypeError, ValueError):
        st = RiskStatus.error(
            TypeError("timeout 不是数字: %r" % (timeout,)), source="merge")
        st.extra["layers"] = {}
        return st

    if total <= 0.0:
        st = RiskStatus.timeout(source="merge", limit=0.0)
        st.extra["layers"] = {}
        st.extra["hint"] = "总预算为 0，一层都没跑"
        return st

    names = _select_layers(only=only, skip=skip)
    if not names:
        st = RiskStatus.unknown_(source="merge", reason="no_layer")
        st.extra["layers"] = {}
        st.extra["hint"] = "所有层都被 only/skip 排除了"
        return st

    # ---- 1) 按期望耗时分配预算 ----
    budgets = _split_budget(names, total)

    # ---- 2) 逐层跑 ----
    results: List[RiskStatus] = []
    layer_report: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    for name in names:
        spent = time.perf_counter() - t0
        left = total - spent
        if left <= 0.0:
            # 总预算用尽 —— 剩下的层标记为"没跑"，不是"干净"
            rep = {"ran": False, "reason": "total_budget_exhausted"}
            layer_report[name] = rep
            errors.append("%s: 总预算耗尽，未执行" % name)
            continue

        # 给这一层的用量 = min(分配值, 剩余总量)
        give = min(budgets.get(name, left), left)
        # 低于地板值就别跑 —— 5ms 的 OCR 只会制造假 timeout
        if give < MIN_LAYER_BUDGET:
            layer_report[name] = {
                "ran": False, "reason": "insufficient_budget",
                "budget": round(give, 4)}
            errors.append("%s: 剩余预算 %.3fs 低于地板值，未执行" % (name, give))
            continue

        lt0 = time.perf_counter()
        try:
            st = _run_layer(name, hwnd=hwnd, cdp_session=cdp_session,
                            session_id=session_id, url=url, region=region,
                            extra_titles=extra_titles, extra_texts=extra_texts,
                            include_invisible=include_invisible,
                            timeout=give)
        except Exception as exc:      # noqa: BLE001 —— 硬约束：绝不吞
            lt = time.perf_counter() - lt0
            msg = "%s: %s: %s" % (name, type(exc).__name__, exc)
            errors.append(msg)
            layer_report[name] = {
                "ran": True, "raised": "%s: %s" % (type(exc).__name__, exc),
                "elapsed": round(lt, 4)}
            # 转成 error 状态参与合并 —— 让"这一层坏了"进入最终结论
            st = RiskStatus.error(exc, source=name,
                                  elapsed=lt)
        else:
            lt = time.perf_counter() - lt0
            layer_report[name] = {
                "ran": True,
                "detected": bool(st.detected),
                "is_clean": bool(st.is_clean),
                "is_unknown": bool(st.is_unknown),
                "reason": st.reason,
                "severity": st.severity,
                "fingerprint_id": st.fingerprint_id,
                "elapsed": round(lt, 4),
                "budget": round(give, 4),
            }

        results.append(st)

        # ---- 硬约束 1：命中立刻返回 ----
        if st.detected:
            st.elapsed = time.perf_counter() - t0
            _attach_layers(st, layer_report, errors, names, hwnd, cdp_session)
            st.extra["stopped_at"] = name
            st.extra["remaining_layers"] = list(
                names[names.index(name) + 1:]) if name in names else []
            return st

    # ---- 3) 全部跑完仍无命中 → 合并 ----
    elapsed = time.perf_counter() - t0

    if not results:
        st = RiskStatus.unknown_(source="merge", reason="no_layer_ran",
                                 elapsed=elapsed)
        _attach_layers(st, layer_report, errors, names, hwnd, cdp_session)
        return st

    merged = merge_status(results, source="merge")
    merged.elapsed = elapsed

    # 合并可能返回 clean（全部层都报干净）—— 那是合法的，
    # 但只要有 unknown 参与，merge_status 已经返回 unknown 了。
    _attach_layers(merged, layer_report, errors, names, hwnd, cdp_session)

    # 硬约束 5 的自证：不确定时绝不 clean
    if merged.is_clean and any(r.is_unknown for r in results):
        # 理论上 merge_status 已经处理了。留这道闸是防它将来被改坏。
        merged.detected = False
        merged.reason = "inconsistent"
        merged.extra["gate"] = ("merge_status 返回 clean 但有 unknown 参与 —— "
                                "已强制降级为 unknown")

    merged.extra["stopped_at"] = ""
    return merged


def _run_layer(name: str,
               *,
               hwnd: Optional[int],
               cdp_session: Any,
               session_id: Optional[str],
               url: str,
               region: Optional[Sequence[int]],
               extra_titles: Sequence[str],
               extra_texts: Sequence[str],
               include_invisible: bool,
               timeout: float) -> RiskStatus:
    """跑单层。**延迟导入** —— 未交付的层不阻断整个模块的 import。"""
    if name == "dom":
        from .detector_dom import detect_dom
        if cdp_session is None:
            return RiskStatus.unknown_(source="dom", reason="no_session")
        return detect_dom(cdp_session, session_id=session_id, url=url,
                          timeout=timeout)

    if name == "window":
        from .detector_window import detect_window
        return detect_window(hwnd or 0, timeout=timeout,
                             include_invisible=include_invisible,
                             extra_titles=extra_titles)

    if name == "pixel":
        from .detector_pixel import detect_pixel
        return detect_pixel(hwnd or 0, region=region, timeout=timeout,
                            extra_texts=extra_texts)

    return RiskStatus.unknown_(source=name, reason="unknown_layer")


def detect_quick(hwnd: Optional[int] = None,
                 cdp_session: Any = None,
                 **kwargs: Any) -> RiskStatus:
    """只要一个"现在能不能动手"的快速回答。

    与 :func:`detect` 的区别：

    - **只跑便宜的两层**（DOM + 窗口），跳过像素层。
    - 总预算压到 1s。

    用途：主循环里**每次动作前**的例行检查。像素层留给
    "我已经觉得不对劲了，再仔细看一眼"的场景 ——
    每点一次鼠标都跑 OCR，机器受得了人也受不了。

    返回 ``is_clean=True`` 表示"便宜的两层没发现问题"，
    **不代表绝对安全** —— 像素层没跑。要看全量结果用 :func:`detect`。
    """
    kw = dict(kwargs)
    kw.setdefault("timeout", 1.0)
    kw["skip"] = tuple(kw.get("skip") or ()) + ("pixel",)
    st = detect(hwnd=hwnd, cdp_session=cdp_session, **kw)
    st.extra["quick"] = True
    return st


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _select_layers(*, only: Optional[Iterable[str]],
                   skip: Iterable[str]) -> List[str]:
    """决定跑哪些层。``only`` 优先于 ``skip``。"""
    if only is not None:
        want = [str(x) for x in only]
        return [n for n in LAYER_ORDER if n in want and LAYERS.get(n)]
    sk = {str(x) for x in (skip or ())}
    return [n for n in LAYER_ORDER if n not in sk and LAYERS.get(n)]


def _split_budget(names: Sequence[str], total: float) -> Dict[str, float]:
    """按**期望耗时比例**切总预算。

    为什么按比例而不是平均分：DOM 只要 15ms，给它 1s 是浪费；
    OCR 要 300ms+，给它 1s 经常不够。按 15:10:200 分配，
    DOM 拿到的富余预算就当"随机波动"的缓冲，像素层拿到大头。
    """
    weights = {n: float(_LAYER_EXPECT.get(n, 0.2)) for n in names}
    s = sum(weights.values()) or 1.0
    return {n: total * (w / s) for n, w in weights.items()}


def _attach_layers(st: RiskStatus,
                   layer_report: Dict[str, Dict[str, Any]],
                   errors: List[str],
                   names: Sequence[str],
                   hwnd: Optional[int],
                   cdp_session: Any) -> None:
    """把层执行明细挂到 ``extra`` 上 —— 可解释性的底线。"""
    st.extra["layers"] = layer_report
    st.extra["layer_order"] = list(names)
    st.extra["target_hwnd"] = int(hwnd or 0)
    st.extra["has_session"] = cdp_session is not None
    if errors:
        st.extra["errors"] = list(errors)


def describe_layers() -> str:
    """给人看的链路说明。"""
    lines = ["三层检测链路（按成本递增）："]
    exp = {"dom": "CDP evaluate，毫秒级",
           "window": "EnumWindows + 指纹，10~50ms",
           "pixel": "截图 + OCR + 文本指纹，300ms~3s"}
    for i, n in enumerate(LAYER_ORDER, 1):
        flag = "已实现" if LAYERS.get(n) else "待交付"
        lines.append("  %d. %-7s %-28s [%s]" % (i, n + "：", exp.get(n, ""), flag))
    lines.append("总预算 %.1fs 由三层共享（按期望耗时 15:10:200 分配）"
                 % DETECT_TOTAL_BUDGET)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------
class _FakeLayer:
    """可以冒充任意一层的假检测器，用来测编排逻辑。

    ``_run_layer`` 是按**层名**延迟 import 再调的，所以要打桩
    得换掉 :func:`_run_layer` 本身 —— 见 :func:`selfcheck`。

    这个类只是提供一个"行为可编程"的替代品：
    第 N 次调用返回什么、抛不抛异常，全由 ``plan`` 决定。
    """

    __slots__ = ("plan", "calls")

    def __init__(self, plan: Dict[str, List[Any]]) -> None:
        # 不使用 __slots__ 之外的东西：calls 记录调用顺序
        self.plan = plan
        self.calls: List[str] = []

    def __call__(self, name: str, **kw: Any) -> RiskStatus:
        self.calls.append(name)
        seq = self.plan.get(name)
        if not seq:
            return RiskStatus.unknown_(source=name, reason="no_plan")
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """编排层自检。

    ``quick=True``（默认）：**全部用假层**，不碰真实桌面也不碰
    CDP，验证的是编排逻辑本身。毫秒级完成。

    ``quick=False``：额外跑一次真实的三层（会碰桌面）。真实
    OCR 不可用是**正常的**，不构成失败。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) 层选择 ============
    _add("默认跑全部三层",
         _select_layers(only=None, skip=()) == list(LAYER_ORDER),
         _select_layers(only=None, skip=()))
    _add("skip 生效",
         _select_layers(only=None, skip=("pixel",)) == ["dom", "window"],
         _select_layers(only=None, skip=("pixel",)))
    _add("only 生效",
         _select_layers(only=("pixel",), skip=()) == ["pixel"],
         _select_layers(only=("pixel",), skip=()))
    _add("only 优先于 skip",
         _select_layers(only=("dom", "pixel"), skip=("dom",)) == ["dom", "pixel"],
         "only 说了算")
    _add("only 里未知层名被忽略",
         _select_layers(only=("dom", "nonexistent"), skip=()) == ["dom"],
         "不炸")
    _add("全部排除 → 空列表",
         _select_layers(only=None, skip=("dom", "window", "pixel")) == [],
         [])

    # ============ 2) 预算切分 ============
    b = _split_budget(["dom", "window", "pixel"], 3.0)
    _add("预算总和 ≈ 总额",
         abs(sum(b.values()) - 3.0) < 1e-6, {k: round(v, 3) for k, v in b.items()})
    _add("像素层分到的预算是 DOM 的 10 倍以上",
         b["pixel"] > b["dom"] * 10,
         "pixel=%.2f dom=%.3f" % (b["pixel"], b["dom"]))
    _add("单层预算不超过总额",
         all(v <= 3.0 + 1e-9 for v in b.values()),
         {k: round(v, 3) for k, v in b.items()})
    b1 = _split_budget(["dom"], 1.0)
    _add("只有一层时独占全部预算",
         abs(b1["dom"] - 1.0) < 1e-9, b1)

    # ============ 3) 假层编排 ============
    import avatar.risk_detector.detector as _me
    _orig_run = _me._run_layer

    def _install(plan):
        fake = _FakeLayer(plan)

        def _run(name, **kw):
            return fake(name, **kw)
        _me._run_layer = _run
        return fake

    def _det(name, sev=Severity.INTERRUPT, fp_id="fp.x"):
        return RiskStatus(
            True, type="captcha", severity=sev, source=name,
            fingerprint_id=fp_id,
            evidence={"layer": name, "fingerprint_id": fp_id})

    try:
        # ---- 3a) 第一层命中 → 立刻返回，后面的层不跑 ----
        fake = _install({
            "dom": [_det("dom")],
            "window": [_det("window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect(only=("dom", "window", "pixel"))
        _add("第一层命中 → 立刻返回",
             st.detected and st.source == "dom", st.describe())
        _add("第一层命中 → window/pixel 根本没跑",
             fake.calls == ["dom"], fake.calls)
        _add("立刻返回 → extra.stopped_at 记录层名",
             st.extra.get("stopped_at") == "dom", st.extra.get("stopped_at"))
        _add("立刻返回 → remaining_layers 列出跳过的层",
             st.extra.get("remaining_layers") == ["window", "pixel"],
             st.extra.get("remaining_layers"))
        _add("立刻返回 → evidence.layers 只含跑过的",
             set(st.extra.get("layers", {})) == {"dom"},
             sorted(st.extra.get("layers", {})))

        # ---- 3b) 前两层 clean，第三层命中 ----
        fake = _install({
            "dom": [RiskStatus.clean(source="dom")],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect(only=("dom", "window", "pixel"))
        _add("前两层 clean + 第三层命中 → detected",
             st.detected and st.source == "pixel", st.describe())
        _add("三层都跑到（命中在最后一层）",
             fake.calls == ["dom", "window", "pixel"], fake.calls)

        # ---- 3c) unknown 要继续降级（关键）----
        fake = _install({
            "dom": [RiskStatus.unknown_(source="dom", reason="no_session")],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect()
        _add("DOM unknown **不阻断**降级 → 继续跑到像素层",
             st.detected and st.source == "pixel", st.describe())
        _add("unknown 之后的层确实跑了",
             fake.calls == ["dom", "window", "pixel"], fake.calls)

        # ---- 3d) 全部 clean → 允许 clean ----
        fake = _install({
            "dom": [RiskStatus.clean(source="dom")],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [RiskStatus.clean(source="pixel")],
        })
        st = _me.detect()
        _add("三层全 clean → 最终 is_clean=True（三路一致可信）",
             st.is_clean is True and st.detected is False, st.describe())
        _add("全 clean 时 evidence.layers 有三条记录",
             len(st.extra.get("layers", {})) == 3,
             sorted(st.extra.get("layers", {})))

        # ---- 3e) 两个 unknown + 一个 clean → **必须** unknown ----
        fake = _install({
            "dom": [RiskStatus.unknown_(source="dom", reason="a")],
            "window": [RiskStatus.unknown_(source="window", reason="b")],
            "pixel": [RiskStatus.clean(source="pixel")],
        })
        st = _me.detect()
        _add("2 unknown + 1 clean → 最终 unknown（不拿无知当证据）",
             st.is_unknown and st.is_clean is False, st.describe())
        _add("unknow 合并后 source='merge'",
             st.source == "merge", st.source)

        # ---- 3f) 三个 unknown → unknown ----
        fake = _install({
            "dom": [RiskStatus.unknown_(source="dom", reason="a")],
            "window": [RiskStatus.unknown_(source="window", reason="b")],
            "pixel": [RiskStatus.unknown_(source="pixel", reason="c")],
        })
        st = _me.detect()
        _add("三层全 unknown → 最终 is_clean=False（硬约束）",
             st.is_clean is False and st.is_unknown, st.describe())

        # ---- 3g) 异常绝不吞：抛→记→继续 ----
        fake = _install({
            "dom": [RuntimeError("DOM 炸了")],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [RiskStatus.clean(source="pixel")],
        })
        st = _me.detect()
        errs = st.extra.get("errors") or []
        _add("层抛异常 → 不中断，继续跑后续层",
             fake.calls == ["dom", "window", "pixel"], fake.calls)
        _add("异常写进 extra.errors",
             any("DOM 炸了" in e for e in errs), errs)
        _add("异常的层在 layers 里标 raised",
             "raised" in st.extra.get("layers", {}).get("dom", {}),
             st.extra.get("layers", {}).get("dom"))
        _add("有异常 → 最终不会 clean",
             st.is_clean is False, "error 参与合并")

        # ---- 3h) 异常之后有命中 → 仍然返回命中 ----
        fake = _install({
            "dom": [RuntimeError("DOM 炸了")],
            "window": [_det("window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect()
        _add("异常层之后的层命中 → 正常返回 detected",
             st.detected and st.source == "window", st.describe())

        # ---- 3i) 全部抛异常 → unknown，不是 clean ----
        fake = _install({
            "dom": [OSError("a")],
            "window": [OSError("b")],
            "pixel": [OSError("c")],
        })
        st = _me.detect()
        _add("三层全抛异常 → unknown（绝不 clean）",
             st.is_unknown and st.is_clean is False, st.describe())

        # ---- 3j) timeout=0 → 立刻 timeout，一层不跑 ----
        fake = _install({"dom": [_det("dom")]})
        st = _me.detect(timeout=0.0)
        _add("timeout=0 → timeout 状态", st.reason == "timeout", st.describe())
        _add("timeout=0 → 一层都没跑", fake.calls == [], fake.calls)

        # ---- 3k) 非法 timeout → error 不崩 ----
        st = _me.detect(timeout="abc")     # type: ignore[arg-type]
        _add("非法 timeout → error（不崩）",
             st.is_unknown and st.reason == "error", st.describe())

        # ---- 3l) 单层超时 → 当 unknown，继续降级 ----
        fake = _install({
            "dom": [RiskStatus.timeout(source="dom", limit=0.01)],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect()
        _add("单层 timeout → 不阻断降级",
             st.detected and st.source == "pixel", st.describe())
        _add("单层 timeout 继续跑完三层",
             fake.calls == ["dom", "window", "pixel"], fake.calls)

        # ---- 3m) only/skip 全排除 → unknown ----
        st = _me.detect(only=("nonexistent",))
        _add("无层可跑 → unknown（不是 clean）",
             st.is_unknown and st.reason == "no_layer", st.describe())

        # ---- 3n) cdp_session=None 时 dom 层返回 no_session ----
        fake = _install({"dom": [RiskStatus.unknown_(source="dom",
                                                      reason="no_session")]})
        st = _me.detect(cdp_session=None, only=("dom",))
        _add("无 session → dom 层 unknown/no_session",
             st.is_unknown and st.reason == "no_session", st.describe())
        _add("无 session 不抛异常", fake.calls == ["dom"], fake.calls)

        # ---- 3o) detect_quick 跳过像素层 ----
        fake = _install({
            "dom": [RiskStatus.clean(source="dom")],
            "window": [RiskStatus.clean(source="window")],
            "pixel": [_det("pixel")],
        })
        st = _me.detect_quick()
        _add("detect_quick 跳过像素层",
             "pixel" not in fake.calls, fake.calls)
        _add("detect_quick 打上标记",
             st.extra.get("quick") is True, st.extra.get("quick"))
        _add("detect_quick 预算默认 1s",
             st.extra.get("layers", {}).get("dom", {}).get("budget", 99) <= 1.0,
             st.extra.get("layers", {}).get("dom", {}).get("budget"))

        # ---- 3p) 命中即返回，不看严重度（同层内不涉及"谁更严重"）----
        fake = _install({
            "dom": [_det("dom", sev=Severity.INFO), _det("dom",
                                                         sev=Severity.INFO)],
            "window": [_det("window", sev=Severity.BLOCK)],
        })
        st = _me.detect()
        _add("第一层命中即返回（哪怕它严重度更低）",
             st.source == "dom" and st.severity == Severity.INFO,
             "%s/%s" % (st.source, st.severity))
        st2 = _me.detect_quick()
        # 上面那个 fake 的 dom 队列留了第二份样本 —— 正好用来验证
        # "短路的不是第一次调用，而是每一次调用"。之前这条断言写成
        # 复用同一个空队列，假红了。
        _add("重复调用同样是短路行为（不是一次性）",
             st2.detected and st2.source == "dom", st2.describe())
    finally:
        _me._run_layer = _orig_run

    # ============ 4) 真实层在（未打桩时）能 import ============
    for mod in ("detector_dom", "detector_window", "detector_pixel"):
        try:
            __import__("avatar.risk_detector." + mod, fromlist=["x"])
            _add("能 import %s" % mod, True, "OK")
        except Exception as exc:      # noqa: BLE001
            _add("能 import %s" % mod, False, "%s: %s" % (type(exc).__name__, exc))

    # ============ 5) 硬约束：顶层不自造 Session / 不硬编码指纹 ============
    import ast
    import io
    import os
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    top_imports: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_imports.append(node.module or "")
    _add("顶层不 import web_cdp（禁自造 session 类型）",
         not any("web_cdp" in m for m in top_imports), top_imports)
    _add("顶层不 import 任何 detector_*（延迟导入）",
         not any("detector_" in m for m in top_imports), top_imports)
    top_classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    _add("顶层无生产用类（只有测试替身）",
         all(c.startswith("_") for c in top_classes), top_classes)

    # 不硬编码指纹（编排队不该认识任何指纹）
    def _code_only(s: str) -> str:
        tr = ast.parse(s)
        drop = set()
        for node in ast.walk(tr):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ln in range(getattr(node, "lineno", 0) or 0,
                                (getattr(node, "end_lineno", 0) or 0) + 1):
                    drop.add(ln)
        return "\n".join(l.split("#", 1)[0] for i, l in enumerate(
            s.split("\n"), 1) if i not in drop)

    code = _code_only(src)
    _add("本模块不硬编码指纹/厂商名",
         not any(k in code for k in
                 ('"recaptcha"', '"cloudflare"', '"拖动滑块"',
                  '"人机验证"', '"just a moment"')),
         "全部来自 fingerprints.py")

    # ============ 6) 描述函数不炸 ============
    try:
        d = describe_layers()
        _add("describe_layers 可用", "三层" in d, d.split("\n")[0])
    except Exception as exc:      # noqa: BLE001
        _add("describe_layers 可用", False, str(exc))

    # ============ 7) quick=False：真跑一遍 ============
    if not quick:
        try:
            st = detect(hwnd=0, timeout=1.0)
            _add("真实三层（无 session）返回 RiskStatus", True, st.describe())
        except Exception as exc:      # noqa: BLE001
            _add("真实三层（无 session）返回 RiskStatus", False, str(exc))
    else:
        checks.append({"name": "真实三层（quick 跳过）",
                       "ok": True, "detail": "(quick 跳过)"})

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
        print("  %s %-52s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
    n = len(r["checks"])
    print("detector 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
