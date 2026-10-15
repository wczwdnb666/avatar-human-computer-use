# -*- coding: utf-8 -*-
"""风控与主流程的接缝 —— 5E。

## 这个模块解决的问题

5B 会检测、5C 会熔断，但**没人去调它们**。本模块是那条"调用线"：
把风险检测挂到动作层的关键路径上，让"检出 → 停"自动发生，
而不是等调用方自己记得检查。

## 挂在哪三个位置（对应 5E 的三条要求）

=====================  =========================================  ============
位置                    时机                                        开销
=====================  =========================================  ============
``input/dispatcher``    物理点击**前**                               只读窗口标题
``uia/actions``         动作失败**连续 3 次后**                        跑完整检测
``agentic_planner``     每个计划步骤**前**                            跑完整检测
=====================  =========================================  ============

前两个位置在**热路径**上（每次点击、每次重试都要过），所以第一个
只查窗口标题 —— **绝不截图**。截图一次要几十毫秒，挂在每次点击
前面会让整个动作层慢到不能用。

## ⚠ 默认关闭（和 solver 同一个理由）

本模块**默认不启用**。``install()`` 之后才生效。

为什么：

- 现有动作层的测试跑在真桌面上。如果默认开启，某个测试窗口的
  标题恰好命中指纹，就会**真的停下来等人按 F9** ——
  测试挂死 300 秒，而且看起来像"代码卡住了"。
- "自动停下来"是一个**改变程序行为**的决定，该由人显式打开。

打开方式::

    from avatar.risk_detector import integration
    integration.install(enabled=True)          # 只观察，不阻断
    integration.install(enabled=True, mode="block")   # 检出就熔断

## 两种模式

- ``observe``（默认）—— 检测照跑，结果记进 ``integration.events()``，
  **但从不阻断**。用于调参、观察误报。
- ``block`` —— 检出就调 :func:`guard.guard`，该停就停。

## 能力边界

**做**：

- ``quick_check`` —— 只读窗口标题/类名/进程名做匹配，**不截图**。
- ``guard_before_action`` —— 动作前的一道闸（受 mode 控制）。
- ``note_failure`` / ``should_detect_after_failures`` —— 失败计数，
  连续 3 次后触发完整检测。
- ``install`` / ``uninstall`` —— 显式开关，线程内全局。

**不做**：

- **不改动作层的返回值语义**。熔断时返回一个"失败的 ActionResult"，
  不是抛异常 —— 动作层的调用方本来就处理失败返回值。
- **不在热路径上截图**。``quick_check`` 只看文本，要看图请显式调
  ``risk_detector.detect()``。
- **不做全局单例以外的状态管理**。没有配置持久化、没有跨进程共享。
- **不自动重试**。失败 3 次后触发的是**检测**，不是重试 ——
  重试是动作层自己的事。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "MODE_OBSERVE",
    "MODE_BLOCK",
    "MODES",
    "FAILURE_THRESHOLD",
    "MAX_EVENTS",
    "install",
    "uninstall",
    "is_installed",
    "mode",
    "set_mode",
    "quick_check",
    "guard_before_action",
    "note_failure",
    "reset_failures",
    "failure_count",
    "should_detect_after_failures",
    "detect_after_failures",
    "events",
    "clear_events",
    "status",
    "describe_integration",
    "selfcheck",
]

#: 只观察，不阻断（默认）。
MODE_OBSERVE = "observe"
#: 检出就熔断。
MODE_BLOCK = "block"
MODES = (MODE_OBSERVE, MODE_BLOCK)

#: 连续失败几次后触发完整检测。
FAILURE_THRESHOLD = 3

#: 事件环形缓冲的上限 —— 不能让长期运行的任务把内存吃光。
MAX_EVENTS = 200


# --------------------------------------------------------------------------
# 全局状态（进程内）
# --------------------------------------------------------------------------
_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {
    "installed": False,
    "mode": MODE_OBSERVE,
    "events": [],          # [{at, kind, hwnd, action, ok, reason, ...}]
    "failures": {},        # key -> 连续失败次数
    "checks": 0,           # quick_check 调用次数（诊断用）
    "blocks": 0,           # 实际阻断次数
}


def install(*, enabled: bool = True, mode_: str = MODE_OBSERVE) -> Dict[str, Any]:
    """打开集成。**必须显式调用** —— 不调就是关着的。

    ``mode_="observe"``（默认）只观察不阻断；``"block"`` 才会真的停下来。
    参数名带下划线是为了不和模块级 :func:`mode` 撞名。
    """
    m = str(mode_ or MODE_OBSERVE).strip().lower()
    if m not in MODES:
        m = MODE_OBSERVE
    with _LOCK:
        _STATE["installed"] = bool(enabled)
        _STATE["mode"] = m
    return status()


def uninstall() -> Dict[str, Any]:
    """关掉集成。**默认状态。**"""
    with _LOCK:
        _STATE["installed"] = False
    return status()


def is_installed() -> bool:
    with _LOCK:
        return bool(_STATE["installed"])


def mode() -> str:
    with _LOCK:
        return str(_STATE["mode"])


def set_mode(m: str) -> str:
    """切换 observe / block。非法值退回 observe（安全侧）。"""
    v = str(m or "").strip().lower()
    if v not in MODES:
        v = MODE_OBSERVE
    with _LOCK:
        _STATE["mode"] = v
    return v


# --------------------------------------------------------------------------
# 事件记录
# --------------------------------------------------------------------------
def _record(kind: str, **fields: Any) -> Dict[str, Any]:
    """记一条事件。**永不抛** —— 记录不该拖垮动作。"""
    rec = {"at": time.time(), "kind": str(kind)}
    rec.update(fields)
    try:
        with _LOCK:
            buf = _STATE["events"]
            buf.append(rec)
            if len(buf) > MAX_EVENTS:
                del buf[:len(buf) - MAX_EVENTS]
    except Exception:      # noqa: BLE001
        pass
    return rec


def events(limit: int = 0) -> List[Dict[str, Any]]:
    """读事件。``limit>0`` 时返回最近 N 条。"""
    with _LOCK:
        buf = list(_STATE["events"])
    if limit and limit > 0:
        return buf[-int(limit):]
    return buf


def clear_events() -> None:
    with _LOCK:
        _STATE["events"] = []


def status() -> Dict[str, Any]:
    """当前集成状态。**永不抛。**"""
    try:
        with _LOCK:
            return {
                "installed": bool(_STATE["installed"]),
                "mode": str(_STATE["mode"]),
                "events": len(_STATE["events"]),
                "checks": int(_STATE["checks"]),
                "blocks": int(_STATE["blocks"]),
                "failure_keys": len(_STATE["failures"]),
            }
    except Exception:      # noqa: BLE001
        return {"installed": False, "mode": MODE_OBSERVE, "events": 0,
                "checks": 0, "blocks": 0, "failure_keys": 0}


# --------------------------------------------------------------------------
# 轻量检测（热路径 —— 绝不截图）
# --------------------------------------------------------------------------
def quick_check(hwnd: int = 0,
                *,
                titles: Optional[List[str]] = None,
                class_name: str = "",
                process: str = "",
                ) -> Any:
    """**只读文本**的轻量检测。返回 :class:`RiskStatus`。

    ## 为什么只查窗口标题

    这个函数挂在**每次物理点击**前面。截图一次 20~80ms，
    挂在点击前会让动作层慢一个数量级 —— 那不是"安全检查"，
    那是"把工具变废"。

    窗口标题和类名是**零成本**的：``GetWindowText`` 是纯内存操作。
    而标题恰恰是最有效的强指纹来源之一（``"安全验证"``、
    ``"人机验证"``、``"Just a moment..."``、``CredentialDialogXamlHost``
    这个类名）。

    ## 返回三态

    - 命中 → ``RiskStatus.hit(...)``（``is_clean=False``）
    - 没命中 → ``RiskStatus.clean(reason="quick_clean")``
    - 读不到窗口 → ``RiskStatus.unknown_(reason="quick_no_window")``
      —— **未知也算可疑**，交给上层的 :func:`guard.should_intervene`
      去决定（它会把未知判成熔断）。

    ## 参数

    ``titles`` 给了就直接用（测试用），不去枚举真实窗口。
    """
    from .status import RiskStatus, RiskType, Severity
    from .fingerprints import match_window

    with _LOCK:
        _STATE["checks"] += 1

    # ---- 拿到窗口信息 ----
    title = ""
    cls = str(class_name or "")
    proc = str(process or "")

    if titles is not None:
        title = " ".join(str(t) for t in titles if t)
    elif hwnd:
        try:
            from ..app_manager import windows as W
            info = W.refresh(int(hwnd))
            title = str(getattr(info, "title", "") or "")
            if not cls:
                cls = str(getattr(info, "class_name", "") or "")
            if not proc:
                proc = str(getattr(info, "process", "") or "")
        except Exception as exc:      # noqa: BLE001
            return RiskStatus.unknown_(
                source="integration.quick",
                reason="quick_read_failed:%s" % type(exc).__name__)

    if not (title or cls or proc):
        # 没有可查的文本 —— 不是"干净"，是"没查成"
        return RiskStatus.unknown_(source="integration.quick",
                                   reason="quick_no_window")

    # ---- 匹配 ----
    try:
        hits = match_window(title=title, class_name=cls, process=proc)
    except Exception as exc:      # noqa: BLE001
        return RiskStatus.unknown_(
            source="integration.quick",
            reason="quick_match_failed:%s" % type(exc).__name__)

    if not hits:
        # ⚠ reason 必须用 "clean"，**不能自造**。
        # RiskStatus.is_clean 的白名单是 ("clean", "no_fingerprint",
        # "below_threshold")。自造一个 "quick_clean" 会让 is_clean
        # 变成 False → 被当成 unknown → 每次都熔断。
        # "这是轻量检测查的"这个信息放 source 和 extra 里。
        return RiskStatus.clean(
            source="integration.quick",
            reason="clean",
            extra={"via": "quick_check",
                   "title": title[:120],
                   "class_name": cls[:80]})

    best = hits[0]
    fp = best.get("fingerprint")
    severity = getattr(fp, "severity", Severity.INTERRUPT)
    rtype = getattr(fp, "type", RiskType.CHALLENGE_PAGE)
    vendor = getattr(fp, "vendor", "")

    return RiskStatus.hit(
        rtype,
        vendor=vendor,
        severity=severity,
        source="integration.quick",
        fingerprint_id=getattr(fp, "id", ""),
        # ⚠ evidence 是 **dict** 不是 list（RiskStatus.__init__ 里
        #    走 dict(evidence or {})）。传 list 会在构造时炸。
        evidence={
            "fingerprint_id": getattr(fp, "id", ""),
            "title": title[:200],
            "class_name": cls[:120],
            "process": proc[:80],
            "weight": getattr(fp, "weight", 0.0),
            "where": getattr(fp, "where", ""),
            "via": "quick_check(窗口标题)",
        },
        confidence=min(0.95, float(getattr(fp, "weight", 1.0) or 1.0)),
    )


# --------------------------------------------------------------------------
# 动作前的闸
# --------------------------------------------------------------------------
def guard_before_action(hwnd: int = 0,
                        *,
                        action: str = "",
                        current_step: str = "",
                        url: str = "",
                        titles: Optional[List[str]] = None,
                        **guard_kw: Any) -> Optional[Dict[str, Any]]:
    """动作**前**的一道闸。返回 ``None`` = 放行。

    ## 返回

    - ``None`` —— 可以继续（集成没开 / 没检出 / observe 模式）
    - ``dict`` —— 被拦下了，内容来自 :func:`guard.guard`

    ## 为什么不抛异常

    动作层的调用方（``dispatcher.dispatch_click`` 等）返回的是
    ``ActionResult``，不是异常。在这里抛会穿透它们的错误处理，
    让"被风控拦下"看起来像"代码崩了"。

    ## observe 模式

    检测照跑、事件照记，但**返回 None** —— 不阻断。
    这样可以在真实任务里观察误报率，而不用先赌一把。
    """
    if not is_installed():
        return None

    st = quick_check(hwnd, titles=titles)

    try:
        from .guard import should_intervene
        intervene, why = should_intervene(st)
    except Exception as exc:      # noqa: BLE001
        # 判定本身坏了 —— 保守：记下来，但 observe 模式不阻断
        _record("guard_error", hwnd=int(hwnd or 0), action=action,
                error="%s: %s" % (type(exc).__name__, exc))
        return None

    if not intervene:
        _record("pass", hwnd=int(hwnd or 0), action=action,
                reason=why, risk=str(getattr(st, "reason", "")))
        return None

    # ---- 检出风险 ----
    m = mode()
    _record("detected", hwnd=int(hwnd or 0), action=action,
            reason=why, risk=st.describe(),
            mode=m, blocked=(m == MODE_BLOCK))

    if m != MODE_BLOCK:
        return None       # observe —— 只看不动

    with _LOCK:
        _STATE["blocks"] += 1

    from .guard import guard as _guard
    try:
        out = _guard(st, hwnd=int(hwnd or 0), url=url,
                     current_step=current_step, **guard_kw)
    except Exception as exc:      # noqa: BLE001
        _record("guard_raised", hwnd=int(hwnd or 0), action=action,
                error="%s: %s" % (type(exc).__name__, exc))
        raise
    return out


# --------------------------------------------------------------------------
# 失败计数（动作层重试用）
# --------------------------------------------------------------------------
def _key(hwnd: Any, action: str) -> str:
    return "%s:%s" % (int(hwnd or 0), str(action or ""))


def note_failure(hwnd: int = 0, action: str = "") -> int:
    """记一次失败，返回**连续**失败次数。"""
    k = _key(hwnd, action)
    with _LOCK:
        n = int(_STATE["failures"].get(k, 0)) + 1
        _STATE["failures"][k] = n
    return n


def reset_failures(hwnd: int = 0, action: str = "") -> None:
    """清掉某个动作的失败计数（成功一次就清）。"""
    with _LOCK:
        _STATE["failures"].pop(_key(hwnd, action), None)


def failure_count(hwnd: int = 0, action: str = "") -> int:
    with _LOCK:
        return int(_STATE["failures"].get(_key(hwnd, action), 0))


def should_detect_after_failures(hwnd: int = 0, action: str = "",
                                 threshold: int = FAILURE_THRESHOLD) -> bool:
    """连续失败够次数了吗？够就该跑一次完整检测了。"""
    return failure_count(hwnd, action) >= max(1, int(threshold))


def detect_after_failures(hwnd: int = 0,
                          *,
                          action: str = "",
                          threshold: int = FAILURE_THRESHOLD,
                          url: str = "",
                          current_step: str = "",
                          full: bool = True,
                          **kw: Any) -> Optional[Dict[str, Any]]:
    """失败 N 次后触发**完整**检测。返回 ``None`` = 没到阈值 / 放行。

    和 :func:`guard_before_action` 的区别：这个跑的是
    :func:`detector.detect`（三层全跑，含截图和 OCR），
    所以**只在失败之后**调 —— 那时候慢一点没关系，
    因为正常路径已经走不通了。

    "动作失败 3 次"是**很强的信号**：目标元素可能被验证码挡了、
    被弹窗遮了、或者页面已经变了。这时候花 3 秒做完整检测是值得的。
    """
    if not is_installed():
        return None

    n = failure_count(hwnd, action)
    if n < max(1, int(threshold)):
        return None

    _record("detect_after_failures", hwnd=int(hwnd or 0), action=action,
            failures=n, threshold=int(threshold))

    try:
        if full:
            from .detector import detect as _detect
            st = _detect(hwnd=int(hwnd or 0) or None,
                         timeout=kw.pop("timeout", 3.0))
        else:
            st = quick_check(hwnd)
    except Exception as exc:      # noqa: BLE001
        _record("detect_error", hwnd=int(hwnd or 0), action=action,
                error="%s: %s" % (type(exc).__name__, exc))
        return None

    try:
        from .guard import should_intervene
        intervene, why = should_intervene(st)
    except Exception:      # noqa: BLE001
        return None

    if not intervene:
        return None

    if mode() != MODE_BLOCK:
        _record("detected", hwnd=int(hwnd or 0), action=action,
                reason=why, risk=st.describe(), mode=mode(), blocked=False)
        return None

    with _LOCK:
        _STATE["blocks"] += 1

    from .guard import guard as _guard
    return _guard(st, hwnd=int(hwnd or 0), url=url,
                  current_step=current_step, **kw)


# --------------------------------------------------------------------------
# 人看的说明
# --------------------------------------------------------------------------
def describe_integration() -> str:
    s = status()
    state = "已启用" if s["installed"] else "未启用（默认）"
    lines = [
        "风控集成：%s（模式 %s）" % (state, s["mode"]),
        "  轻量检测次数：%d" % s["checks"],
        "  实际阻断次数：%d" % s["blocks"],
        "  事件数        ：%d" % s["events"],
        "  失败计数键    ：%d" % s["failure_keys"],
        "",
        "挂载点：",
        "  input/dispatcher  物理点击前   → quick_check（只读标题）",
        "  uia/actions       失败 3 次后  → detect（完整三层）",
        "  agentic_planner   每步执行前   → detect（完整三层）",
        "",
        "启用：",
        "  from avatar.risk_detector import integration",
        "  integration.install(enabled=True)              # observe",
        "  integration.install(enabled=True, mode_='block')  # 真阻断",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """integration 自检。**不碰真实桌面、不阻断、不等待。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ⚠ 走外部调用方路径
    _me = _sys.modules[__name__]

    saved_installed = _me.is_installed()
    saved_mode = _me.mode()
    saved_events = _me.events()

    try:
        # ============ 1) 常量 ============
        _add("模式只有两种", set(_me.MODES) == {"observe", "block"},
             str(_me.MODES))
        _add("失败阈值是 3（用户要求）", _me.FAILURE_THRESHOLD == 3,
             str(_me.FAILURE_THRESHOLD))
        _add("事件缓冲有上限", _me.MAX_EVENTS > 0, str(_me.MAX_EVENTS))
        _add("模块 docstring 含「能力边界」",
             "能力边界" in (_me.__doc__ or ""), "有")

        # ============ 2) 默认关闭 ============
        _me.uninstall()
        _add("★ 默认未启用", _me.is_installed() is False, "关着")
        _add("★ 未启用时 guard_before_action 直接返回 None",
             _me.guard_before_action(0, action="click") is None, "None")
        _add("★ 未启用时 detect_after_failures 直接返回 None",
             _me.detect_after_failures(0, action="click") is None, "None")

        # ============ 3) 安装 / 卸载 / 模式 ============
        s = _me.install(enabled=True)
        _add("install(enabled=True) 生效", _me.is_installed() is True, "开")
        _add("install 返回状态字典", isinstance(s, dict) and "mode" in s,
             str(s))
        _add("默认模式是 observe", _me.mode() == _me.MODE_OBSERVE,
             _me.mode())
        _add("set_mode 能切到 block",
             _me.set_mode("block") == "block" and _me.mode() == "block",
             "block")
        _add("★ set_mode 非法值退回 observe（安全侧）",
             _me.set_mode("乱写的") == "observe", "退回")
        _me.set_mode("observe")
        _add("uninstall 关掉",
             _me.uninstall() is not None and _me.is_installed() is False,
             "关")

        # ============ 4) quick_check 的三态 ============
        from .status import RiskStatus

        st = _me.quick_check(0, titles=["记事本 - 未命名"])
        _add("普通标题 → clean",
             st.is_clean is True and st.reason == "clean",
             st.describe())
        _add("★ clean 的 reason 必须在 RiskStatus 的白名单里",
             st.reason in ("clean", "no_fingerprint", "below_threshold"),
             "reason=%s is_clean=%s" % (st.reason, st.is_clean))

        st = _me.quick_check(0, titles=[])
        _add("★ 没有可查文本 → unknown（不是 clean）",
             st.is_clean is False and st.detected is False
             and "no_window" in st.reason, st.describe())

        st = _me.quick_check(0)
        _add("★ hwnd=0 且没给 titles → unknown",
             st.is_clean is False and st.detected is False, st.describe())

        st = _me.quick_check(0, titles=["安全验证 - Windows"])
        _add("★ 命中窗口指纹（安全验证）→ detected",
             st.detected is True, st.describe())
        _add("命中证据里带 via 标记（说明是轻量检测来的）",
             bool(st.evidence) and str(
                 st.evidence.get("via", "")).startswith("quick_check"),
             str(st.evidence.get("via", "")))
        _add("命中带 fingerprint_id（可追溯）",
             bool(st.fingerprint_id), st.fingerprint_id)

        st = _me.quick_check(0, titles=["人机验证"])
        _add("★ 中文人机验证标题也能命中",
             st.detected is True, st.describe())

        st = _me.quick_check(0, titles=["请登录"])
        _add("★ 登录类标题命中", st.detected is True, st.describe())

        st = _me.quick_check(0, titles=["Just a moment..."])
        _add("★ 英文 Cloudflare 标题命中", st.detected is True, st.describe())

        # 类名 / 进程名也能匹配
        st = _me.quick_check(0, class_name="CredentialDialogXamlHost")
        _add("★ 类名能命中（凭据对话框）", st.detected is True, st.describe())

        # ============ 5) quick_check 绝不截图 ============
        import ast as _ast
        src = open(_me.__file__, encoding="utf-8").read()
        tree = _ast.parse(src)
        quick_fn = None
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and \
                    node.name == "quick_check":
                quick_fn = node
        _add("能找到 quick_check 定义", quick_fn is not None, "有")

        if quick_fn is not None:
            called = set()
            for node in _ast.walk(quick_fn):
                if isinstance(node, _ast.Attribute):
                    called.add(node.attr)
                elif isinstance(node, _ast.Name):
                    called.add(node.id)
            _add("★★ quick_check 里不出现任何截图调用",
                 not (called & {"grab_screen", "grab_window", "save_bitmap",
                                "save_screenshot", "capture", "ImageGrab"}),
                 "无截图")
            _add("★★ quick_check 里不出现 OCR 调用",
                 not (called & {"read_region", "pytesseract", "ocr"}),
                 "无 OCR")
            _add("★ quick_check 里不调用完整 detect（避免热路径变慢）",
                 "detect" not in called, "只走 match_window")

        # ============ 6) observe 模式不阻断 ============
        _me.clear_events()
        _me.install(enabled=True, mode_=_me.MODE_OBSERVE)
        got = _me.guard_before_action(0, action="click",
                                      titles=["安全验证"])
        _add("★★ observe 模式：检出但返回 None（不阻断）", got is None,
             repr(got))
        evs = _me.events()
        _add("★ observe 模式：事件仍然记下来了",
             any(e["kind"] == "detected" for e in evs),
             "events=%d" % len(evs))
        _add("★ 事件里标明 blocked=False",
             any(e.get("blocked") is False for e in evs if
                 e["kind"] == "detected"), "observe")

        # 放行路径也记事件
        _me.clear_events()
        _me.guard_before_action(0, action="click", titles=["记事本"])
        _add("放行也记 pass 事件",
             any(e["kind"] == "pass" for e in _me.events()), "pass")

        # 未安装时不记事件
        _me.uninstall()
        _me.clear_events()
        _me.guard_before_action(0, action="click", titles=["安全验证"])
        _add("★ 未启用时不产生任何事件（零开销）",
             _me.events() == [], "空")

        # ============ 7) 失败计数 ============
        _me.install(enabled=True, mode_=_me.MODE_OBSERVE)
        _me.reset_failures(1, "click")
        _add("初始失败计数为 0", _me.failure_count(1, "click") == 0, "0")
        n1 = _me.note_failure(1, "click")
        n2 = _me.note_failure(1, "click")
        n3 = _me.note_failure(1, "click")
        _add("note_failure 递增", (n1, n2, n3) == (1, 2, 3),
             "%s" % [n1, n2, n3])
        _add("★ 到 3 次 → should_detect_after_failures=True",
             _me.should_detect_after_failures(1, "click") is True, "3 次")
        _add("reset 后归零",
             (_me.reset_failures(1, "click"),
              _me.failure_count(1, "click"))[1] == 0, "0")
        _add("★ 不同 (hwnd, action) 互不干扰",
             (_me.note_failure(2, "click"),
              _me.failure_count(1, "click"),
              _me.failure_count(2, "click"))[1:] == (0, 1),
             "隔离")

        _add("没到阈值时 should_detect=False",
             (_me.reset_failures(9, "type"),
              _me.note_failure(9, "type"),
              _me.should_detect_after_failures(9, "type"))[2] is False,
             "1 次")
        _add("自定义阈值生效",
             (_me.reset_failures(8, "x"),
              _me.note_failure(8, "x"),
              _me.should_detect_after_failures(8, "x", threshold=1))[2]
             is True, "threshold=1")

        # 未到阈值时 detect_after_failures 不动
        _me.reset_failures(7, "click")
        _me.note_failure(7, "click")
        _add("★ 未到阈值 → detect_after_failures 返回 None",
             _me.detect_after_failures(7, action="click") is None, "None")

        # ============ 8) 事件缓冲不无限增长 ============
        _me.clear_events()
        for i in range(_me.MAX_EVENTS + 50):
            _me._record("test", i=i)
        _add("★ 事件缓冲被截断在 MAX_EVENTS",
             len(_me.events()) == _me.MAX_EVENTS,
             "%d" % len(_me.events()))

        # ============ 9) status / describe ============
        st_d = _me.status()
        _add("status 返回全部字段",
             set(st_d) >= {"installed", "mode", "events", "checks",
                           "blocks", "failure_keys"}, str(sorted(st_d)))
        _add("describe_integration 不抛",
             isinstance(_me.describe_integration(), str), "有文本")
        _add("describe 提到三个挂载点",
             all(k in _me.describe_integration()
                 for k in ("dispatcher", "uia/actions", "agentic_planner")),
             "三个都在")

        # ============ 10) 坏输入不崩 ============
        _add("quick_check(hwnd=None) 不崩",
             _me.quick_check(None) is not None, "容错")
        _add("quick_check(titles=None, hwnd=巨大值) 不崩",
             _me.quick_check(0x7FFFFFF0) is not None, "容错")
        _add("guard_before_action 坏参数不崩",
             _me.guard_before_action(0, titles=[]) in (None, {}) or True,
             "容错")
        _add("note_failure(0, '') 不崩",
             isinstance(_me.note_failure(0, ""), int), "容错")
        _add("status() 永不抛", isinstance(_me.status(), dict), "dict")

    finally:
        _me.clear_events()
        _me.set_mode(saved_mode)
        _me.install(enabled=saved_installed, mode_=saved_mode)
        for e in saved_events:
            _me._record(e.get("kind", "restored"), **{
                k: v for k, v in e.items() if k != "kind"})

    elapsed = time.perf_counter() - t0
    return {
        "module": "integration",
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
    print(describe_integration())
