# -*- coding: utf-8 -*-
"""环境观测 —— 执行前后各拍一张"快照"。

## 为什么需要它

验证"这一步成功了吗"的唯一可靠办法，是**比较执行前后的环境差异**。
没有 before 快照，你只能看到"现在屏幕上有个记事本"——
但那个记事本可能是十分钟前就开着的。

## 观测什么

============================  ==========================================
字段                          来源
============================  ==========
``title`` / ``class_name``    窗口标题与类名（阶段二）
``process``                   进程名
``hwnd``                      窗口句柄
``url``                       页面地址（有 CDP 会话时）
``screenshot_path``           截图路径（**默认不截**）
``elements``                  控件树摘要（可选）
``timestamp``                 观测时刻
============================  ==========================================

## ⚠ 截图默认关闭

``observe(screenshot=False)`` 是默认值。理由：执行器每步要观测**两次**，
截图一次 20~80ms —— 挂上去会让整个任务慢一个数量级。

**只有需要像素级验证时才开**（比如"点击后界面应该变化"）。
那时执行器会显式传 ``screenshot=True``。

## 能力边界

**做**：读窗口标题/类名/进程/URL；可选截图存盘；可选控件树摘要；
把结果压成可 JSON 序列化的 :class:`Observation`；**永不抛异常**。

**不做**：

- **不做判断**。"变没变、成没成"是 :mod:`verifier` 的事。
  本模块只负责"拍下来"。
- **不做图像处理**。pHash 计算在 :mod:`verifier`，本模块只给路径。
- **不清理截图**。截图落在阶段五的熔断产物目录，清理策略归它。
- **不主动开 CDP 会话**。有就用来读 URL，没有就留空 ——
  为了读个 URL 去起浏览器是不划算的。
- **不做深度遍历**。``elements`` 只是浅层摘要（前 N 个控件的名字），
  深度遍历在阶段二 :mod:`uia.tree` 里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "MAX_ELEMENTS",
    "Observation",
    "observe",
    "observe_window",
    "describe_observer",
    "selfcheck",
]

#: 控件树摘要最多取几个。
MAX_ELEMENTS = 20

#: 标题/类名等字符串的截断长度。
_MAX_STR = 200


@dataclass
class Observation:
    """一次观测的结果。**JSON 安全。**"""

    ok: bool = False
    hwnd: int = 0
    title: str = ""
    class_name: str = ""
    process: str = ""
    url: str = ""
    screenshot_path: str = ""
    elements: List[str] = field(default_factory=list)
    timestamp: float = 0.0
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def identity(self) -> str:
        """一个能代表"这是哪个窗口/页面"的短串。用于快速比较。"""
        return self.url or self.title or (
            "hwnd=%d" % self.hwnd if self.hwnd else "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "hwnd": int(self.hwnd or 0),
            "title": self.title,
            "class_name": self.class_name,
            "process": self.process,
            "url": self.url,
            "screenshot_path": self.screenshot_path,
            "elements": list(self.elements),
            "timestamp": float(self.timestamp),
            "reason": self.reason,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Observation":
        """从 dict 还原。**脏数据不抛** —— 观测记录会进 memory 存档，
        而存档可能被手改过、被老版本写过。"""
        d = d if isinstance(d, dict) else {}
        return cls(
            ok=bool(d.get("ok")),
            hwnd=_as_int(d.get("hwnd")),
            title=str(d.get("title", "") or ""),
            class_name=str(d.get("class_name", "") or ""),
            process=str(d.get("process", "") or ""),
            url=str(d.get("url", "") or ""),
            screenshot_path=str(d.get("screenshot_path", "") or ""),
            elements=[str(x) for x in (d.get("elements") or [])]
            if isinstance(d.get("elements"), (list, tuple)) else [],
            timestamp=_as_float(d.get("timestamp")),
            reason=str(d.get("reason", "") or ""),
            extra=dict(d.get("extra") or {})
            if isinstance(d.get("extra"), dict) else {},
        )

    def describe(self) -> str:
        if not self.ok:
            return "观测失败：%s" % (self.reason or "未知")
        bits = []
        if self.title:
            bits.append(self.title[:40])
        if self.url:
            bits.append(self.url[:50])
        if self.hwnd:
            bits.append("hwnd=%d" % self.hwnd)
        if self.screenshot_path:
            bits.append("shot")
        return " | ".join(bits) or "(空观测)"

    def same_identity(self, other: "Observation") -> bool:
        """两个观测看的是不是同一个东西（同一窗口/同一页面）。"""
        if not isinstance(other, Observation):
            return False
        a, b = self.identity, other.identity
        return bool(a) and a == b


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def observe(hwnd: Optional[int] = None,
            cdp_session: Any = None,
            *,
            screenshot: bool = False,
            elements: bool = False,
            directory: Optional[str] = None,
            errors: Optional[List[str]] = None,
            timeout: float = 2.0) -> Observation:
    """观测当前环境。**永不抛异常。**

    ## 参数

    - ``hwnd`` —— 目标窗口。不给就试前台窗口。
    - ``cdp_session`` —— 有的话读 URL（**不会为读 URL 去新建会话**）。
    - ``screenshot`` —— **默认 False**。开了才截图（慢）。
    - ``elements`` —— 要不要控件树摘要（也是可选，因为它要遍历 UIA）。
    - ``errors`` —— 失败原因会 append 进来。

    ## 返回值

    :class:`Observation`。**失败时 ``ok=False`` 且 ``reason`` 有值** ——
    不抛异常。理由：观测失败（窗口没了、UIA 抽风）是预期内的，
    让它抛会把整个执行循环打断，而"这一步观测不到"通常还能继续跑。
    """
    def _err(msg: str) -> None:
        if errors is not None:
            try:
                errors.append(msg)
            except Exception:      # noqa: BLE001
                pass

    obs = Observation(hwnd=int(hwnd or 0))

    # ---- 1) 窗口信息 ----
    target = int(hwnd or 0)
    if not target:
        target = _foreground()
        obs.hwnd = target

    if target:
        try:
            from ..app_manager import windows as W
            info = W.refresh(target)
            obs.title = str(getattr(info, "title", "") or "")[:_MAX_STR]
            obs.class_name = str(getattr(info, "class_name", "")
                                 or "")[:_MAX_STR]
            obs.process = str(getattr(info, "process", "") or "")[:_MAX_STR]
            obs.ok = True
        except Exception as exc:      # noqa: BLE001
            obs.reason = "window_read_failed:%s" % type(exc).__name__
            _err("observer: %s" % obs.reason)
    else:
        obs.reason = "no_target_window"
        _err("observer: no_target_window")

    # ---- 2) URL（有会话才读）----
    if cdp_session is not None:
        url, url_err = _read_url(cdp_session, timeout=timeout)
        if url:
            obs.url = str(url)[:_MAX_STR]
            obs.ok = True
        elif url_err:
            # 给了会话却读不到 URL —— 要记下来，
            # 不然"为什么 DOM 验证总是不通过"会查不到原因
            obs.extra["url_error"] = url_err

    # ---- 3) 截图（默认不做）----
    if screenshot:
        try:
            from ..risk_detector import context as C
            errs: List[str] = []
            path = C.save_screenshot(target, errors=errs,
                                     directory=directory) if target else ""
            if path:
                obs.screenshot_path = str(path)
            else:
                obs.extra["screenshot_error"] = errs[0] if errs else "unknown"
        except Exception as exc:      # noqa: BLE001
            obs.extra["screenshot_error"] = type(exc).__name__

    # ---- 4) 控件树摘要（可选）----
    if elements and target:
        try:
            obs.elements = _elements_summary(target)
        except Exception as exc:      # noqa: BLE001
            obs.extra["elements_error"] = type(exc).__name__

    return obs


def observe_window(hwnd: int, **kw: Any) -> Observation:
    """``observe`` 的显式窗口版本（可读性用）。"""
    kw.pop("cdp_session", None)
    return observe(hwnd, **kw)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _foreground() -> int:
    try:
        from .. import perception as P
        return int(P.foreground_hwnd() or 0)
    except Exception:      # noqa: BLE001
        return 0


def _read_url(session: Any, *, timeout: float = 2.0) -> tuple:
    """从一个 CDP 会话里读当前 URL。

    返回 ``(url, error)``。**认不出的会话返回 ``("", "unreadable")``**。

    为什么要返回 error 而不是只吞异常：调用方需要区分
    "这个会话本来就没有 URL"（Web 之外的目标）和
    "会话坏了读不出来"（要排查）。前者留空即可，后者要记进 extra。
    """
    if session is None:
        return "", ""

    # 有些会话对象自带 url 属性
    url = getattr(session, "url", "")
    if isinstance(url, str) and url:
        return url, ""

    tried = False
    last_err = ""
    for attr in ("evaluate", "call"):
        fn = getattr(session, attr, None)
        if not callable(fn):
            continue
        tried = True
        try:
            if attr == "evaluate":
                got = fn("location.href", timeout=timeout)
            else:
                got = fn("Runtime.evaluate",
                         {"expression": "location.href",
                          "returnByValue": True}, timeout=timeout)
            v = _dig_url(got)
            if v:
                return v, ""
        except Exception as exc:      # noqa: BLE001
            last_err = "%s: %s" % (type(exc).__name__, str(exc)[:80])
            continue

    if not tried:
        # 既没有 url 属性也没有 evaluate/call —— 认不出来
        return "", "unreadable_session:%s" % type(session).__name__
    return "", last_err or "no_url_in_response"


def _dig_url(got: Any) -> str:
    """从 CDP 的各种返回形态里挖出 URL。"""
    if isinstance(got, str):
        return got
    if isinstance(got, dict):
        for k in ("result", "value", "url"):
            if k in got:
                v = _dig_url(got[k])
                if v:
                    return v
    return ""


def _as_int(v: Any, default: int = 0) -> int:
    """安全转 int。**脏值退默认，不抛。**"""
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return int(default)


def _as_float(v: Any, default: float = 0.0) -> float:
    """安全转 float。**脏值退默认，不抛。**"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _elements_summary(hwnd: int) -> List[str]:
    """浅层控件树摘要：前 N 个控件的「类型:名字」。"""
    from ..uia import tree as T
    root = T.root_of(hwnd) if hasattr(T, "root_of") else None
    if root is None:
        return []
    out: List[str] = []
    try:
        for el in T.walk(root, max_nodes=MAX_ELEMENTS * 3):
            name = str(getattr(el, "name", "") or "").strip()
            ctype = str(getattr(el, "control_type", "") or "")
            if not (name or ctype):
                continue
            out.append(("%s:%s" % (ctype, name))[:120])
            if len(out) >= MAX_ELEMENTS:
                break
    except Exception:      # noqa: BLE001
        return out
    return out


def describe_observer() -> str:
    """给人看的观测层说明。"""
    return "\n".join([
        "环境观测（observer）",
        "=" * 60,
        "观测字段：hwnd / title / class_name / process / url /",
        "          screenshot_path / elements / timestamp",
        "",
        "⚠ 截图默认**关闭**：执行器每步观测两次，",
        "   截图一次 20~80ms，挂上去会让任务慢一个数量级。",
        "   只有需要像素级验证时才显式开。",
        "",
        "控件树摘要也是可选的（要遍历 UIA，同样有成本）。",
        "最多取 %d 个控件。" % MAX_ELEMENTS,
        "",
        "永不抛异常 —— 观测失败返回 ok=False + reason。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """observer 自检。**默认路径不截图、不遍历控件树。**"""
    import shutil
    import sys as _sys
    import tempfile

    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]
    tmp = tempfile.mkdtemp(prefix="avatar_obs_")

    try:
        # ============ 1) 常量与契约 ============
        _add("控件摘要上限是正数", _me.MAX_ELEMENTS > 0,
             str(_me.MAX_ELEMENTS))
        _add("模块 docstring 含「能力边界」",
             "能力边界" in (_me.__doc__ or ""), "有")
        _add("★ 默认不截图（签名里 screenshot=False）",
             _default_screenshot_is_false(), "默认关")

        # ============ 2) 基本观测 ============
        # ⚠ 用一个几乎肯定不存在的 hwnd 来测"失败路径"。
        # 不能传 0 —— 0 表示"用前台窗口"，而测试机上通常有前台窗口，
        # 那样测的是成功路径。
        DEAD = 0x7FFFFFF0

        obs = _me.observe(DEAD)
        _add("★ observe(死 hwnd) 不抛异常",
             isinstance(obs, _me.Observation), "是 Observation")
        _add("obs 有 timestamp",
             obs.timestamp > 0, "%.0f" % obs.timestamp)
        _add("★ 窗口读不到时 ok=False 且有 reason（不抛）",
             obs.ok is False and bool(obs.reason), obs.reason)
        _add("★ 默认不截图（screenshot_path 为空）",
             obs.screenshot_path == "", "空")

        errs0: List[str] = []
        _me.observe(DEAD, errors=errs0)
        _add("★ errors 收到失败原因",
             bool(errs0) and "observer" in errs0[0], str(errs0[:1]))

        # ============ 3) 真实窗口（拿不到就跳过相关断言）============
        fg = _me._foreground()
        if fg:
            obs2 = _me.observe(fg)
            _add("★ 对真实窗口能读出信息",
                 obs2.ok is True, obs2.describe())
            _add("hwnd 被记录", obs2.hwnd == fg, str(obs2.hwnd))
            _add("★ title 被截断到 200 字以内",
                 len(obs2.title) <= 200, "%d 字" % len(obs2.title))
            _add("identity 非空", bool(obs2.identity), obs2.identity)
        else:
            _add("（无前台窗口，跳过真实窗口检查）", True, "skip")

        # ============ 4) URL 读取 ============
        class _SessUrlAttr:
            url = "https://example.com/a"

        o = _me.observe(0, _SessUrlAttr())
        _add("★ 会话自带 url 属性时直接读",
             o.url == "https://example.com/a", o.url)

        class _SessEval:
            def evaluate(self, expr, **kw):
                return "https://example.com/b"

        o = _me.observe(0, _SessEval())
        _add("★ 通过 evaluate 读 location.href",
             o.url == "https://example.com/b", o.url)

        class _SessCall:
            def call(self, method, params=None, **kw):
                return {"result": {"value": "https://example.com/c"}}

        o = _me.observe(0, _SessCall())
        _add("★ 通过 call 读 URL（嵌套 result.value）",
             o.url == "https://example.com/c", o.url)

        class _SessBad:
            def evaluate(self, expr, **kw):
                raise RuntimeError("会话坏了")

        o = _me.observe(0, _SessBad())
        _add("★ 会话坏了不抛，只记 extra（带异常类型）",
             isinstance(o, _me.Observation)
             and str(o.extra.get("url_error", "")).startswith("RuntimeError"),
             str(o.extra.get("url_error", "")))

        class _SessNone:
            pass

        o = _me.observe(0, _SessNone())
        _add("★ 认不出的会话返回空 URL 且记下原因（不抛）",
             o.url == "" and "unreadable_session" in str(
                 o.extra.get("url_error", "")),
             str(o.extra.get("url_error", "")))

        # ============ 5) 截图开关 ============
        o_noshot = _me.observe(DEAD, screenshot=False)
        _add("screenshot=False 时不产生路径",
             o_noshot.screenshot_path == "", "空")
        _add("★ screenshot=True 且窗口不存在时也不抛",
             _me.observe(DEAD, screenshot=True).screenshot_path == "", "容错")

        if fg:
            o_shot = _me.observe(fg, screenshot=True, directory=tmp)
            _add("★ screenshot=True 时产生路径（或明确失败原因）",
                 bool(o_shot.screenshot_path)
                 or "screenshot_error" in o_shot.extra,
                 o_shot.screenshot_path or str(o_shot.extra))

        # ============ 6) 序列化 ============
        d = obs2.to_dict() if fg else obs.to_dict()
        _add("to_dict JSON 安全",
             __import__("json").dumps(d, ensure_ascii=False) != "", "ok")
        back = _me.Observation.from_dict(d)
        _add("★ from_dict 往返一致",
             back.hwnd == d["hwnd"] and back.title == d["title"], "一致")
        _add("★ from_dict 脏数据不抛",
             _me.Observation.from_dict({"elements": "不是列表",
                                        "hwnd": "abc"}).hwnd == 0,
             "容错")
        _add("from_dict(None) 不抛",
             _me.Observation.from_dict(None) is not None, "容错")

        # ============ 7) 比较 ============
        a = _me.Observation(ok=True, title="记事本", hwnd=1)
        b = _me.Observation(ok=True, title="记事本", hwnd=1)
        c = _me.Observation(ok=True, title="计算器", hwnd=2)
        _add("★ same_identity 对同窗口返回 True",
             a.same_identity(b) is True, "True")
        _add("★ same_identity 对不同窗口返回 False",
             a.same_identity(c) is False, "False")
        _add("★ same_identity 对非 Observation 返回 False（不抛）",
             a.same_identity("字符串") is False, "False")
        _add("空 identity 不相等",
             _me.Observation().same_identity(_me.Observation()) is False,
             "False")

        # ============ 8) describe ============
        _add("describe 失败时有原因",
             "观测失败" in _me.Observation(reason="x").describe(), "有")
        _add("describe 成功时可读",
             "记事本" in _me.Observation(ok=True, title="记事本").describe(),
             "有")
        _add("describe_observer 不抛",
             isinstance(_me.describe_observer(), str), "有")
        _add("★ describe_observer 说明默认不截图",
             "默认" in _me.describe_observer(), "有")

        # ============ 9) observe_window 别名 ============
        _add("observe_window 不抛",
             isinstance(_me.observe_window(0), _me.Observation), "有")

        # ============ 10) errors 列表（坏对象容错）============
        class _BadList:
            def append(self, x):
                raise RuntimeError("列表坏了")

        _add("★ errors 是坏对象也不抛",
             isinstance(_me.observe(DEAD, errors=_BadList()),
                        _me.Observation),
             "容错")

        # ============ 11) 不 import 图像库 ============
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
        _add("★ 顶层不 import PIL / cv2 / numpy（不做图像处理）",
             not (top & {"PIL", "cv2", "numpy", "imagehash"}),
             sorted(top))
        _add("顶层不 import 执行/验证/恢复（单向依赖）",
             not (top & {"executor", "verifier", "recovery"}),
             sorted(top))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    return {
        "module": "observer",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


def _default_screenshot_is_false() -> bool:
    """检查 ``observe`` 的 screenshot 参数默认是 False。"""
    import inspect
    try:
        sig = inspect.signature(observe)
        return sig.parameters["screenshot"].default is False
    except Exception:      # noqa: BLE001
        return False


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_observer())
