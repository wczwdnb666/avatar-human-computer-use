# -*- coding: utf-8 -*-
"""风控检测与验证题熔断（阶段五）。

## 这个包解决什么问题

产品哲学是"**除了必要，全是自动**"。前面四个阶段把"自动"做到了极致：
UIA 精准定位、真人化轨迹、执行结果验证。但有一条边界始终没处理 ——

    **遇到人机验证题时该怎么办？**

错误答案有三个，这个包是为了**同时避开这三个**：

1. **硬解**。写 OCR 识别滑块缺口、算拖动距离、点选图标坐标。
   这是明确禁止的（硬约束 #9）：技术上不稳定，法律上越界，
   而且风控厂商每天都在升级 —— 解一次能过，下周就废。
2. **死等**。检测到就 sleep / 无限轮询等人来解决。
   任务会永远卡在那儿，调用方拿不到任何信号，无法做超时决策。
3. **静默失败**。把检出当成普通失败往上抛，一路吞掉。
   人根本不知道需要自己动手，只看到一个语焉不详的错误码。

正确答案是**熔断**：检出 → 优雅暂停 → 呼叫人类 → 人类完成后从断点继续。

## 唯一合法的人工打断

`avatar/errors.py` 里对 ``RiskBlocked`` 的注释写着"风险闸门拦截
（**唯一合法的人工打断之一**）"。这个包就是那句话的实现。

## 分层

::

    status.py         RiskStatus —— 四元组结论（检出/类型/证据/处置）
    fingerprints.py   指纹库 —— "验证题长什么样"的唯一权威定义
    detector_dom.py   第一层：CDP DOM / iframe 检测
    detector_window.py 第二层：窗口标题 / 类名 / 模态遮挡
    detector_pixel.py  第三层：截图 + OCR 关键词（最贵，最后用）
    detector.py       三层汇合入口 detect()
    guard.py          HumanInterventionRequired / pause_for_human()
    context.py        InterventionContext —— 熔断时的完整上下文快照
    resume.py         人类完成后从断点恢复
    solver.py         外部打码平台（**默认关闭**）

## 三层检测的成本阶梯

====================  ==============  =====================  ==========
层                     手段             典型耗时               何时启用
====================  ==============  =====================  ==========
``dom``               CDP evaluate     ~50ms（有会话时）      有 CDP 会话就用
``window``            EnumWindows      ~20ms                  总是启用
``pixel``             截图 + OCR       0.5~2s                 **仅当上两层都没结论**
====================  ==============  =====================  ==========

第三层最贵且最容易误报（OCR 认错字、页面截图包含无关文本），
所以硬约束是：**只在 DOM 和 window 都没检出且没结论时才启用**。

## 默认值与开关

- 检测超时默认 **3 秒**，超时返回 ``RiskStatus(reason="timeout")``，
  **不是**"干净"。
- 人类介入默认限 **5 分钟**，超时抛 ``HumanInterventionTimeout``，
  任务转 Blocked，不是死等。
- 外部打码**默认关闭**，必须显式 ``enabled=True`` + ``api_key``。

## 典型用法

::

    from avatar.risk_detector import detect, guard

    st = detect(hwnd=hwnd, cdp_session=sess, timeout=3.0)
    if st.action == "pause":
        guard.pause_for_human(st, step="提交订单", timeout=300)
    elif st.action == "abort":
        raise RiskBlocked("任务终止：%s" % st.describe())
    # action == "continue" 时什么都不做，继续走

"""

from __future__ import annotations

# ---- 结论数据结构 ----
from .status import (
    ACTION_ABORT,
    ACTION_CONTINUE,
    ACTION_INTERFACE,
    ACTION_PAUSE,
    ALL_ACTIONS,
    DEFAULT_RISK_TIMEOUT,
    RiskStatus,
    RiskType,
    Severity,
    SEVERITY_ACTION,
    action_interface,
    describe_actions,
    merge_status,
)

# ---- 指纹库 ----
from .fingerprints import (
    ALL_FINGERPRINTS,
    DOM_FINGERPRINTS,
    GROUP_THRESHOLD,
    SOURCES,
    STRONG_TEXT_GROUPS,
    TEXT_FINGERPRINTS,
    WEAK_TEXT_WORDS,
    WEAK_TIER_AMBIGUOUS,
    WEAK_TIER_FOOTER,
    WEAK_TIER_OCR_NOISE,
    WEAK_TIER_PENALTY,
    WEAK_WORD_TIER,
    WINDOW_FINGERPRINTS,
    Fingerprint,
    audit_sources,
    describe_library,
    fingerprint_by_id,
    iter_fingerprints,
    match_dom,
    match_text,
    match_window,
    source_info,
    text_hit_score,
)

__all__ = [
    # status
    "RiskStatus", "RiskType", "Severity", "SEVERITY_ACTION",
    "DEFAULT_RISK_TIMEOUT", "merge_status",
    # status —— 5C 契约（三态动作接口，两边不许各定义一套）
    "ACTION_CONTINUE", "ACTION_PAUSE", "ACTION_ABORT", "ALL_ACTIONS",
    "ACTION_INTERFACE", "action_interface", "describe_actions",
    # fingerprints
    "Fingerprint", "DOM_FINGERPRINTS", "WINDOW_FINGERPRINTS",
    "TEXT_FINGERPRINTS", "STRONG_TEXT_GROUPS", "WEAK_TEXT_WORDS",
    "ALL_FINGERPRINTS", "GROUP_THRESHOLD", "WEAK_TIER_PENALTY",
    "WEAK_TIER_FOOTER", "WEAK_TIER_AMBIGUOUS", "WEAK_TIER_OCR_NOISE",
    "WEAK_WORD_TIER",
    "SOURCES", "source_info", "audit_sources",
    "fingerprint_by_id", "iter_fingerprints", "match_dom", "match_window",
    "match_text", "text_hit_score", "describe_library",
    # 能力层（延迟导入，避免与 detector/guard 的循环 import）
    "detect", "detect_quick", "describe_layers", "detect_available",
    "should_intervene", "translate_action",
    "guarded", "pause_for_human", "guard",
    "HumanInterventionRequired", "HumanInterventionTimeout", "AbortSignal",
    "InterventionContext", "build_context", "intervention_dir",
    "resume_from", "ResumePlan", "validate_context",
    "RESUME_OK", "RESUME_STALE", "RESUME_ABORTED",
    "RESUME_STRATEGIES", "DEFAULT_RESUME_STRATEGY",
    "solver",
    "solve_captcha_via_api", "solve_recaptcha_token",
    "solver_enabled", "solver_config", "describe_solver",
    "integration", "install_risk_guard", "uninstall_risk_guard",
    "risk_guard_installed", "risk_guard_events", "describe_integration",
    "quick_check", "MODE_OBSERVE", "MODE_BLOCK",
    "selfcheck",
]


#: 允许包级 ``__getattr__`` 懒加载的子模块。
_LAZY = {
    "detector", "detector_dom", "detector_window", "detector_pixel",
    "guard", "context", "resume", "solver", "integration", "errors",
}


def __getattr__(name: str):
    """懒加载能力层模块。

    理由和 ``avatar.web_cdp.__init__`` 一样：能力层模块都要
    ``from .status import ...``，而本 ``__init__`` 又要导出它们 ——
    顶层直接 import 会循环。用 ``__getattr__`` 做成按需加载：
    ``import avatar.risk_detector`` 时不加载，访问 ``.guard`` 时才加载。

    另外还有两个便捷**函数**的直接导出：

    - ``detect``       —— ``detector.detect``
    - ``detect_quick`` —— ``detector.detect_quick``
    - ``pause_for_human`` —— ``guard.pause_for_human``
    - ``resume_from``  —— ``resume.resume_from``

    这样调用方可以 ``from avatar.risk_detector import detect``，
    不用记得它在哪个子模块里。
    """
    import importlib
    if name in _LAZY:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod

    _FUNCS = {
        "detect": ("detector", "detect"),
        "detect_quick": ("detector", "detect_quick"),
        "describe_layers": ("detector", "describe_layers"),
        "detect_available": ("detector", "LAYERS"),
        # ---- 5C：熔断与恢复 ----
        "should_intervene": ("guard", "should_intervene"),
        "translate_action": ("guard", "translate_action"),
        "pause_for_human": ("guard", "pause_for_human"),
        "guard": ("guard", "guard"),
        "guarded": ("guard", "guarded"),
        "HumanInterventionRequired": ("guard", "HumanInterventionRequired"),
        "HumanInterventionTimeout": ("guard", "HumanInterventionTimeout"),
        "AbortSignal": ("guard", "AbortSignal"),
        "InterventionContext": ("context", "InterventionContext"),
        "build_context": ("context", "build_context"),
        "intervention_dir": ("context", "intervention_dir"),
        "resume_from": ("resume", "resume_from"),
        "ResumePlan": ("resume", "ResumePlan"),
        "validate_context": ("resume", "validate_context"),
        "RESUME_OK": ("resume", "RESUME_OK"),
        "RESUME_STALE": ("resume", "RESUME_STALE"),
        "RESUME_ABORTED": ("resume", "RESUME_ABORTED"),
        "RESUME_STRATEGIES": ("context", "RESUME_STRATEGIES"),
        "DEFAULT_RESUME_STRATEGY": ("context", "DEFAULT_RESUME_STRATEGY"),
        # ---- 5D：外部打码（默认关闭）----
        "solve_captcha_via_api": ("solver", "solve_captcha_via_api"),
        "solve_recaptcha_token": ("solver", "solve_recaptcha_token"),
        "solver_enabled": ("solver", "is_enabled"),
        "solver_config": ("solver", "SolverConfig"),
        "describe_solver": ("solver", "describe_solver"),
        # ---- 5E：与主流程集成 ----
        "install_risk_guard": ("integration", "install"),
        "uninstall_risk_guard": ("integration", "uninstall"),
        "risk_guard_installed": ("integration", "is_installed"),
        "risk_guard_events": ("integration", "events"),
        "describe_integration": ("integration", "describe_integration"),
        "quick_check": ("integration", "quick_check"),
        "MODE_OBSERVE": ("integration", "MODE_OBSERVE"),
        "MODE_BLOCK": ("integration", "MODE_BLOCK"),
    }
    if name in _FUNCS:
        mod_name, attr = _FUNCS[name]
        try:
            mod = importlib.import_module("." + mod_name, __name__)
        except ImportError as exc:
            raise AttributeError(
                "%s 尚未实现（导入 %s 失败：%s）" % (name, mod_name, exc))
        obj = getattr(mod, attr, None)
        if obj is None:
            raise AttributeError("%s.%s 不存在" % (mod_name, attr))
        globals()[name] = obj
        return obj

    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def available() -> dict:
    """报告风控检测能力当前可用性。**永不抛异常。**

    返回::

        {"ok": bool, "layers": {"dom": ..., "window": ..., "pixel": ...},
         "ocr": bool, "beep": bool, "hotkey": bool, "solver": bool,
         "reason": str}

    可用性探测本身不该失败 —— 它存在的意义就是让上层据此
    决定走不走降级链。
    """
    out = {"ok": False, "layers": {}, "ocr": False, "beep": False,
           "hotkey": False, "solver": False, "reason": ""}

    # 第一层：DOM（需要 CDP 会话才算可用，这里是静态能力探测）
    try:
        import importlib
        importlib.import_module(".detector_dom", __name__)
        out["layers"]["dom"] = "ready"
    except Exception as exc:      # noqa: BLE001
        out["layers"]["dom"] = "unavailable: %s" % exc

    # 第二层：窗口枚举（纯 ctypes，本机必然可用）
    try:
        from ..app_manager import windows as W
        n = len(W.list_windows())
        out["layers"]["window"] = "ready(%d 窗口)" % n
    except Exception as exc:      # noqa: BLE001
        out["layers"]["window"] = "unavailable: %s" % exc

    # 第三层：像素 + OCR
    try:
        from ..uia import ocr_fallback as O
        out["ocr"] = bool(getattr(O, "is_available", lambda: False)())
        out["layers"]["pixel"] = "ready" if out["ocr"] else "no-ocr"
    except Exception as exc:      # noqa: BLE001
        out["layers"]["pixel"] = "unavailable: %s" % exc

    # 提示音（winsound 是内置模块，Windows 上必然有）
    try:
        import winsound      # noqa: F401
        out["beep"] = True
    except Exception:      # noqa: BLE001
        out["beep"] = False

    # 热键轮询（GetAsyncKeyState 在 user32 里）
    try:
        from .._win32 import api as A
        out["hotkey"] = hasattr(A.user32, "GetAsyncKeyState")
    except Exception:      # noqa: BLE001
        out["hotkey"] = False

    # 外部打码（默认关闭）
    try:
        import importlib
        S = importlib.import_module(".solver", __name__)
        cfg = S.load_config()
        out["solver"] = bool(getattr(cfg, "enabled", False))
    except Exception:      # noqa: BLE001
        out["solver"] = False

    ok = out["layers"].get("window", "").startswith("ready")
    out["ok"] = ok
    parts = []
    if not ok:
        parts.append("窗口层不可用 —— 检测降级到仅 DOM/像素")
    if not out["ocr"]:
        parts.append("OCR 不可用 —— 像素层失效")
    if not out["beep"]:
        parts.append("无提示音 —— 熔断时无法出声提醒")
    if not out["hotkey"]:
        parts.append("热键轮询不可用 —— 只能靠超时退出")
    out["reason"] = "；".join(parts) if parts else "三层检测全部就绪"
    return out


def selfcheck() -> dict:
    """包级自检：汇总已实现的子模块。

    **只跑已存在的子模块** —— 阶段五分批次交付，5A 完成时
    detector/guard 还不存在，硬跑会红。用 ``_SUBS`` 里实际能
    导入的模块做汇总，并对缺失模块报 ``"(未实现)"`` 而不是失败，
    这样自检在开发中途也是绿的，不会淹没真正的错误。
    """
    import time as _t
    import importlib
    t0 = _t.perf_counter()
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1) 核心两层必须先能用（它们在 5A 就该完成）
    for mod_name in ("status", "fingerprints"):
        try:
            mod = importlib.import_module("." + mod_name, __name__)
        except Exception as exc:      # noqa: BLE001
            _add("%s 可导入" % mod_name, False,
                 "%s: %s" % (type(exc).__name__, exc))
            continue
        try:
            sub = mod.selfcheck()
        except Exception as exc:      # noqa: BLE001
            _add("%s.selfcheck 可跑" % mod_name, False,
                 "%s: %s" % (type(exc).__name__, exc))
            continue
        total = len(sub.get("checks", []))
        bad = len(sub.get("failures", []))
        _add("%s.selfcheck" % mod_name, bool(sub.get("ok")),
             "%d/%d 通过，%.3fs" % (total - bad, total,
                                    sub.get("elapsed", 0.0)))

    # 2) 后续批次的模块：存在则跑，不存在只记录不失败
    for mod_name in ("context", "detector_dom", "detector_window",
                     "detector_pixel", "detector", "guard", "resume",
                     "solver"):
        try:
            mod = importlib.import_module("." + mod_name, __name__)
        except ImportError:
            _add("%s（待交付）" % mod_name, True, "未实现 —— 不构成失败")
            continue
        except Exception as exc:      # noqa: BLE001
            _add("%s 可导入" % mod_name, False,
                 "%s: %s" % (type(exc).__name__, exc))
            continue
        fn = getattr(mod, "selfcheck", None)
        if fn is None:
            _add("%s.selfcheck 存在" % mod_name, False, "模块没有 selfcheck()")
            continue
        try:
            sub = fn()
        except Exception as exc:      # noqa: BLE001
            _add("%s.selfcheck 可跑" % mod_name, False,
                 "%s: %s" % (type(exc).__name__, exc))
            continue
        total = len(sub.get("checks", []))
        bad = len(sub.get("failures", []))
        _add("%s.selfcheck" % mod_name, bool(sub.get("ok")),
             "%d/%d 通过，%.3fs" % (total - bad, total,
                                    sub.get("elapsed", 0.0)))

    # 3) 指纹库规模（阶段五的核心资产，掉了要立刻知道）
    try:
        n_dom = len(DOM_FINGERPRINTS)
        n_win = len(WINDOW_FINGERPRINTS)
        n_txt = len(TEXT_FINGERPRINTS)
        _add("指纹库规模达标", n_dom >= 20 and n_win >= 8 and n_txt >= 30,
             "dom=%d window=%d text=%d" % (n_dom, n_win, n_txt))
    except Exception as exc:      # noqa: BLE001
        _add("指纹库规模达标", False, "%s: %s" % (type(exc).__name__, exc))

    # 4) 硬约束 #9 的可执行自证（滑块/点选恒为 INTERRUPT）
    try:
        bad = [f.id for f in ALL_FINGERPRINTS
               if f.type in (RiskType.SLIDER, RiskType.CLICK_POINT)
               and f.severity != Severity.INTERRUPT]
        _add("硬约束#9：滑块/点选恒为 INTERRUPT", not bad,
             bad or "已自证")
    except Exception as exc:      # noqa: BLE001
        _add("硬约束#9：滑块/点选恒为 INTERRUPT", False,
             "%s: %s" % (type(exc).__name__, exc))

    # 5) 可用性探测本身不抛
    try:
        av = available()
        _add("available() 不抛异常", "layers" in av and "reason" in av,
             av.get("reason", "")[:56])
    except Exception as exc:      # noqa: BLE001
        _add("available() 不抛异常", False, "%s: %s" % (type(exc).__name__, exc))

    # 6) **5B 端到端**：从包入口真的能跑通 detect()
    #
    #    这一条是阶段五最容易悄悄坏掉的地方 —— 各模块自检全绿，
    #    但包级 `detect` 的 `__getattr__` 懒加载链断了，调用方
    #    一 import 就炸。所以包自检必须自己走一遍真实入口。
    #
    #    ⚠ 注意这里**必须**用 `_pkg.detect` / `_pkg.WEAK_TIER_FOOTER`
    #    这种属性访问，不能写裸名 `detect`。裸名查的是本函数的
    #    globals()，而 `__getattr__` 只在**模块外部访问属性**时触发 ——
    #    在模块内部裸名引用根本不会走到 __getattr__，只会 NameError。
    #    （同一个坑在 detector_window 的自检里也踩过：模块内部
    #    裸名调用拿不到被 monkeypatch 的模块全局。教训一致：
    #    自检要显式走"外部调用方会走的路径"，否则测的不是真实入口。）
    import sys as _sys
    _pkg = _sys.modules[__name__]
    try:
        d = _pkg.detect(hwnd=0, timeout=0.8)    # 不传 session → DOM 会 unknown
        _add("包入口 detect() 可调用", isinstance(d, RiskStatus), d.describe())
        _add("包入口 detect() 的 is_clean 类型正确",
             isinstance(d.is_clean, bool), repr(d.is_clean))
        # 无 session + 本机无验证题 + OCR 未接入 → 三条 unknown，
        # 最终**必须**不是 clean。这是硬约束的端到端自证。
        _add("包入口：三层全 unknown 时 is_clean=False（硬约束端到端自证）",
             d.is_clean is False,
             "layers=%s" % sorted(d.extra.get("layers", {})))
        _add("包入口：extra.layers 记录了三层",
             set(d.extra.get("layers", {})) >= {"dom", "window", "pixel"},
             sorted(d.extra.get("layers", {})))
    except Exception as exc:      # noqa: BLE001
        _add("包入口 detect() 可调用", False,
             "%s: %s" % (type(exc).__name__, exc))

    # 7) 5C 契约出口通着（5C 会 `from avatar.risk_detector import ACTION_INTERFACE`）
    try:
        iface = action_interface(ACTION_PAUSE)
        _add("ACTION_INTERFACE 可供 5C 消费",
             bool(iface) and iface.get("guard_entry") == "pause_for_human",
             iface.get("guard_entry"))
        _add("pause 契约点名 HumanInterventionTimeout",
             iface.get("raises") == "HumanInterventionTimeout",
             iface.get("raises"))
    except Exception as exc:      # noqa: BLE001
        _add("ACTION_INTERFACE 可供 5C 消费", False,
             "%s: %s" % (type(exc).__name__, exc))

    # 8) 5G 误报率测试要用的消融开关通着
    try:
        base = text_hit_score("请完成验证")["score"]
        off = text_hit_score("请完成验证",
                             penalty_scale={_pkg.WEAK_TIER_FOOTER: 0.0})["score"]
        _add("penalty_scale 消融开关可供 5G 使用",
             isinstance(base, float) and isinstance(off, float),
             "base=%.2f off=%.2f" % (base, off))
    except Exception as exc:      # noqa: BLE001
        _add("penalty_scale 消融开关可供 5G 使用", False,
             "%s: %s" % (type(exc).__name__, exc))

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "elapsed": round(_t.perf_counter() - t0, 3),
    }


def _main() -> int:
    r = selfcheck()
    for c in r["checks"]:
        print("  %s %-44s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
    n = len(r["checks"])
    print("risk_detector 包自检：%d/%d 通过，%.3fs"
          % (n - len(r["failures"]), n, r["elapsed"]))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
