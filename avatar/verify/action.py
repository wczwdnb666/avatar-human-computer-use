# -*- coding: utf-8 -*-
"""统一验证入口：``verify_action`` 自动路由桌面 / Web。

## 为什么要"统一入口"

阶段二/三/四各层都有自己的验证需求，如果每层自己判断
"我该用 pHash 还是 DOM"，很快就会长出十套互相矛盾的逻辑。
统一入口把**路由决策**收拢到一处，各层只管传"我要验证什么"。

## 路由规则

::

    mode="auto"（默认）
      ├─ 有 cdp / session / selector / monitor / expect_url
      │   → web 验证（DOM / URL / 网络）
      └─ 否则（有 hwnd / region / uia_target）
          → desktop 验证（pHash / UIA）

    mode="desktop"   强制桌面
    mode="web"       强制 Web（没有 CDP 会话则返回 unknown）

## 判据清单（按强度排序）

================  ================================================
``network``        服务端响应 —— **事实级别**
``dom``            DOM 确切的文本/值/可见性
``uia``            UIA 确切的属性/值
``url`` / ``title``  导航发生（弱，易被重定向骗）
``pixel``         只是"画面变了"，说不出对错 —— **最弱**
================  ================================================

## 返回什么

永远返回 :class:`~avatar.verify.result.VerifyResult`。
**绝不返回裸 bool，绝不抛异常**（除非参数本身非法）。
这样调用方可以写：

::

    r = verify_action(mode="web", session=sess, selector="#ok",
                      expect_text="提交成功")
    if r.ok:      ...   # 确认成功
    elif r.failed: ...  # 确认失败，该报错
    else:         ...   # **未知，该重试或降级**

第三态一定要处理。只写 ``if r.ok: ... else: 失败`` 是**错的**，
它把"我不知道"当成了"失败"，会导致无谓的失败上报和错误的回滚。

## 批量验证

:func:`verify_any` 做"任一满足即可"（OR），
用于"两种成功路径都算成功"的场景（比如提交后可能跳转也可能弹窗）。
需要 AND 时直接对多个结果调 :func:`~avatar.verify.result.summarize`。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from ..errors import AvatarError
from .result import VerifyResult, summarize

__all__ = [
    "RouteError",
    "VerifyRequest",
    "verify_action",
    "verify_any",
    "verify_all",
    "describe_verify",
    "selfcheck",
]


class RouteError(AvatarError):
    """路由参数非法。**只有这个才会抛** —— 验证失败不抛。"""

    exit_code = 25


#: Web 判据的触发键。任一存在就认为"这是 Web 场景"。
_WEB_KEYS = ("session", "sess", "cdp", "monitor", "selector",
             "expect_url", "has_expect_url", "expect_title",
             "has_expect_title", "page", "session_id")

#: 桌面判据的触发键。
_DESKTOP_KEYS = ("hwnd", "region", "uia_target", "expect_value",
                 "uia_state", "expect_exists", "before_hash",
                 "before_shot", "after_shot", "name_exact",
                 "control_type",
                 # 标题判据。漏了这个会导致"给了 --expect-title 却报
                 # 没给判据"—— 因为 is_desktop 判定失败，请求被当成
                 # "既不是 web 也不是 desktop"的空请求。
                 "verify_window_title")


class VerifyRequest(object):
    """一次验证请求的完整参数容器。

    用对象而不是几十个 kwargs 传参，是为了让调用方
    可以**拼装/复用/序列化**验证请求 —— 这在 recipe（食谱）
    和重试逻辑里都要用。
    """

    __slots__ = ("mode", "timeout", "settle", "prefer", "_kw")

    def __init__(self, mode: str = "auto", *, timeout: float = 5.0,
                 settle: float = 0.25, prefer: str = "auto",
                 **kw: Any) -> None:
        if mode not in ("auto", "desktop", "web"):
            raise RouteError("非法 mode：%r（只能是 auto/desktop/web）"
                             % (mode,))
        self.mode = mode
        self.timeout = float(timeout)
        self.settle = float(settle)
        self.prefer = prefer
        self._kw = dict(kw)

    def get(self, key: str, default: Any = None) -> Any:
        return self._kw.get(key, default)

    def has(self, key: str) -> bool:
        v = self._kw.get(key)
        return v is not None and v is not False

    def with_(self, **kw: Any) -> "VerifyRequest":
        """派生一个新请求（覆盖部分参数）。原对象不动。"""
        merged = dict(self._kw)
        merged.update(kw)
        return VerifyRequest(self.mode, timeout=self.timeout,
                             settle=self.settle, prefer=self.prefer, **merged)

    def keys(self) -> List[str]:
        return sorted(self._kw)

    def to_kwargs(self) -> Dict[str, Any]:
        return dict(self._kw)

    @property
    def is_web(self) -> bool:
        return any(self.has(k) for k in _WEB_KEYS)

    @property
    def is_desktop(self) -> bool:
        return any(self.has(k) for k in _DESKTOP_KEYS)

    def describe(self) -> Dict[str, Any]:
        return {"mode": self.mode, "timeout": self.timeout,
                "settle": self.settle, "prefer": self.prefer,
                "is_web": self.is_web, "is_desktop": self.is_desktop,
                "params": self.keys()}

    def __repr__(self) -> str:  # pragma: no cover
        return "<VerifyRequest %s web=%s desktop=%s>" % (
            self.mode, self.is_web, self.is_desktop)


def _pick(kw: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in kw and kw[n] is not None:
            return kw[n]
    return None


def _route(req: VerifyRequest) -> str:
    """决定走哪条路。返回 ``"web"`` 或 ``"desktop"``。"""
    if req.mode == "web":
        return "web"
    if req.mode == "desktop":
        return "desktop"
    # auto
    if req.is_web and not req.is_desktop:
        return "web"
    if req.is_desktop and not req.is_web:
        return "desktop"
    if req.is_web and req.is_desktop:
        # 两边都有 —— 优先 Web（判据更强）
        return "web"
    return "desktop"      # 都没有，交给桌面端报"没判据"


def verify_action(
    mode: str = "auto",
    *,
    timeout: float = 5.0,
    settle: float = 0.25,
    prefer: str = "auto",
    request: Optional[VerifyRequest] = None,
    **kw: Any,
) -> VerifyResult:
    """统一验证入口。见模块文档。

    ``request`` 给了就以它为主（``mode``/``timeout`` 等被它覆盖），
    此时其它 kwargs 作为**补充**合并进去。
    """
    t0 = time.time()
    try:
        req = request if request is not None else VerifyRequest(
            mode, timeout=timeout, settle=settle, prefer=prefer, **kw)
        if request is not None and kw:
            req = req.with_(**kw)
    except RouteError:
        raise
    except Exception as exc:
        return VerifyResult.unknown_(
            method="route", reason="构造验证请求失败：%s" % (exc,),
            elapsed=time.time() - t0)

    route = _route(req)
    params = req.to_kwargs()

    try:
        if route == "web":
            r = _do_web(req, params)
        else:
            r = _do_desktop(req, params)
    except Exception as exc:
        return VerifyResult.unknown_(
            method=route, mode=route,
            reason="验证过程异常：%s: %s" % (type(exc).__name__, exc),
            elapsed=time.time() - t0, error=str(exc))

    if r is None:
        return VerifyResult.unknown_(
            method=route, mode=route,
            reason="没能确定判据（缺少必要参数）",
            elapsed=time.time() - t0,
            extra={"params": sorted(params)})

    r.mode = route
    r.elapsed = time.time() - t0
    return r


def _do_web(req: VerifyRequest, p: Dict[str, Any]) -> Optional[VerifyResult]:
    sess = _pick(p, "session", "sess", "cdp", "page")
    if sess is None:
        # 没有 CDP 会话 —— 明确报"未知"，而不是静默转桌面
        return VerifyResult.unknown_(
            method="web",
            reason="指定了 web 模式但没有 CDP 会话（session/sess/cdp）",
            extra={"no_session": True})

    from .web import verify_web
    return verify_web(
        sess,
        selector=_pick(p, "selector", "element"),
        expect_text=_pick(p, "expect_text"),
        has_expect_text=bool(p.get("has_expect_text")),
        expect_value=_pick(p, "expect_value_web", "dom_value"),
        has_expect_value=bool(p.get("has_expect_value_web")),
        expect_visible=_pick(p, "expect_visible"),
        expect_exists=_pick(p, "expect_element_exists"),
        expect_url=_pick(p, "expect_url"),
        has_expect_url=bool(p.get("has_expect_url") or p.get("expect_url")),
        expect_title=_pick(p, "expect_title"),
        has_expect_title=bool(p.get("has_expect_title") or p.get("expect_title")),
        monitor=_pick(p, "monitor"),
        network_url_contains=_pick(p, "network_url_contains", "request_url"),
        expect_status=_pick(p, "expect_status"),
        has_expect_status=bool(p.get("has_expect_status")
                               or p.get("expect_status") is not None),
        body_contains=_pick(p, "body_contains"),
        match=str(p.get("match") or "contains"),
        session_id=_pick(p, "session_id"),
        timeout=float(p.get("timeout") or req.timeout),
        prefer=req.prefer,
    )


def _do_desktop(req: VerifyRequest,
                p: Dict[str, Any]) -> Optional[VerifyResult]:
    from .desktop import verify_desktop

    # ⚠ 这个门禁必须包含**所有**桌面判据的键。漏一个的表现是
    #   "给了判据但报'没能确定判据（缺少必要参数）'"—— 因为请求在这里
    #   被判成 None，上层只能给个含糊的 unknown。踩过：加了
    #   verify_window_title 却忘了加进这里，标题判据静默失效。
    has_any = any(p.get(k) is not None for k in
                  ("hwnd", "region", "uia_target", "before_hash",
                   "before_shot", "after_shot", "uia_state",
                   "verify_window_title"))
    if not has_any:
        return None

    uia_state = _pick(p, "uia_state")
    expect_exists = _pick(p, "expect_exists", "expect_uia_exists")

    return verify_desktop(
        hwnd=int(p.get("hwnd") or 0),
        region=_pick(p, "region"),
        before_hash=_pick(p, "before_hash"),
        before_shot=_pick(p, "before_shot"),
        after_shot=_pick(p, "after_shot"),
        threshold=int(p.get("threshold") or 6),
        unchanged_max=int(p.get("unchanged_max") or 5),
        expect_change=bool(p.get("expect_change", True)),
        uia_target=_pick(p, "uia_target", "element", "target"),
        expect_value=_pick(p, "expect_value"),
        has_expect_value=bool(p.get("has_expect_value")
                              or p.get("expect_value") is not None),
        match=str(p.get("match") or "exact"),
        uia_state=uia_state,
        expect_exists=(None if expect_exists is None
                       else bool(expect_exists)),
        # 收紧旋钮。传给 verify_uia_exists 后才可能走 UIA 原生下推；
        # 不传 = 宽松子串 = Python 全遍历（实测全桌面 52.8 秒/次，
        # 此时 timeout 形同虚设）。
        uia_name_exact=bool(p.get("name_exact") or p.get("uia_name_exact")),
        uia_control_type=_pick(p, "control_type", "uia_control_type"),
        verify_window_title=_pick(p, "verify_window_title",
                                  "expect_window_title", "expect_title"),
        title_match=str(p.get("title_match") or "contains"),
        settle=float(p.get("settle") or req.settle),
        prefer=req.prefer if req.prefer != "auto" else "auto",
    )


def verify_any(
    requests: Sequence[Any],
    *,
    mode: str = "auto",
    timeout: float = 5.0,
) -> VerifyResult:
    """**任一成功即成功**（OR 语义）。

    用于"多条成功路径"的场景：提交后可能跳转到成功页，
    也可能原地弹个 toast。两种都算成功。

    注意：只有当**所有**分支都确认失败时才返回 fail。
    有一个分支未知、其它都失败 → 返回"未知"（因为那个未知的
    分支可能就是成功路径，只是没测出来）。
    """
    t0 = time.time()
    if not requests:
        return VerifyResult.unknown_(
            method="any", reason="没有给出任何验证分支",
            elapsed=time.time() - t0)

    results = []  # type: List[VerifyResult]
    for item in requests:
        if isinstance(item, VerifyRequest):
            r = verify_action(request=item)
        elif isinstance(item, dict):
            # dict 里的 timeout 优先 —— 每个分支可能有自己的合理超时
            kw = dict(item)
            kw.setdefault('timeout', timeout)
            kw.setdefault('mode', mode)
            r = verify_action(**kw)
        elif isinstance(item, VerifyResult):
            r = item
        else:
            r = VerifyResult.unknown_(method="any",
                                     reason="无法识别的分支：%r" % (item,))
        results.append(r)
        if r.ok:
            return VerifyResult.ok_(
                method="any", mode=r.mode,
                reason="第 %d 个分支确认成功：%s"
                       % (results.index(r) + 1, r.reason),
                evidence={"branches": [x.to_dict() for x in results]},
                elapsed=time.time() - t0,
                extra={"hit_index": results.index(r),
                       "confidence": r.confidence})

    unknown_n = sum(1 for r in results if r.unknown)
    if unknown_n:
        return VerifyResult.unknown_(
            method="any",
            reason="全部 %d 个分支都未确认成功（其中 %d 个无法判定）"
                   % (len(results), unknown_n),
            evidence={"branches": [r.to_dict() for r in results]},
            elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="any",
        reason="全部 %d 个分支都确认失败" % len(results),
        evidence={"branches": [r.to_dict() for r in results]},
        elapsed=time.time() - t0)


def verify_all(
    requests: Sequence[Any],
    *,
    mode: str = "auto",
    timeout: float = 5.0,
) -> VerifyResult:
    """**全部成功才成功**（AND 语义）。"""
    t0 = time.time()
    if not requests:
        return VerifyResult.unknown_(
            method="all", reason="没有给出任何验证分支",
            elapsed=time.time() - t0)

    results = []  # type: List[VerifyResult]
    for item in requests:
        if isinstance(item, VerifyRequest):
            r = verify_action(request=item)
        elif isinstance(item, dict):
            # dict 里的 timeout 优先 —— 每个分支可能有自己的合理超时
            kw = dict(item)
            kw.setdefault('timeout', timeout)
            kw.setdefault('mode', mode)
            r = verify_action(**kw)
        elif isinstance(item, VerifyResult):
            r = item
        else:
            r = VerifyResult.unknown_(method="all",
                                     reason="无法识别的分支：%r" % (item,))
        results.append(r)

    bad = [i for i, r in enumerate(results) if r.failed]
    unk = [i for i, r in enumerate(results) if r.unknown]

    if bad:
        return VerifyResult.fail(
            method="all",
            reason="%d/%d 个分支确认失败（索引 %s）"
                   % (len(bad), len(results), bad),
            evidence={"branches": [r.to_dict() for r in results],
                      "failed_indexes": bad},
            elapsed=time.time() - t0)
    if unk:
        return VerifyResult.unknown_(
            method="all",
            reason="%d/%d 个分支无法判定（索引 %s），其余均成功"
                   % (len(unk), len(results), unk),
            evidence={"branches": [r.to_dict() for r in results],
                      "unknown_indexes": unk},
            elapsed=time.time() - t0)
    return VerifyResult.ok_(
        method="all",
        reason="全部 %d 个分支都确认成功" % len(results),
        evidence={"branches": [r.to_dict() for r in results]},
        elapsed=time.time() - t0)


def describe_verify(r: Any) -> str:
    """人读的一行说明。容错：任何输入都不抛。"""
    try:
        rr = VerifyResult if False else None  # noqa: F841  保持 import 可见
        if isinstance(r, VerifyResult):
            return r.describe()
        if isinstance(r, dict):
            v = r.get("verdict", "?")
            m = r.get("method", "")
            rs = r.get("reason", "")
            return "[%s%s] %s" % (v, ("/" + m) if m else "", rs)
        return str(r)
    except Exception as exc:
        return "<describe 失败: %s>" % (exc,)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

class _FakeSession(object):
    def __init__(self, payload=None):
        self.payload = payload or {"result": {"type": "undefined"}}
        self.calls = []

    def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        return self.payload


def _obj_el(found=True, text="", value=None):
    return {"result": {"type": "object", "value": {
        "found": found, "text": text, "value": value,
        "display": "block", "visibility": "visible", "opacity": "1",
        "rect": {"x": 0, "y": 0, "w": 10, "h": 10}, "attrs": {},
    }}}


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- VerifyRequest ---
    rq = VerifyRequest("web", selector="#a")
    add("VerifyRequest 记录 mode", rq.mode == "web")
    add("VerifyRequest is_web", rq.is_web is True)
    add("VerifyRequest is_desktop False", rq.is_desktop is False)
    add("VerifyRequest get 取值", rq.get("selector") == "#a")
    add("VerifyRequest get 默认值", rq.get("nope", 7) == 7)
    add("VerifyRequest has 真", rq.has("selector") is True)
    add("VerifyRequest has 假", rq.has("nope") is False)
    # has() 的语义是"给了且不是 False" —— 空串**算给了**
    # （空串可能是合法的期望值，比如"验证文本框被清空"）
    add("VerifyRequest has 空串算真", rq.with_(x="").has("x") is True)
    add("VerifyRequest has None 算假",
        rq.with_(x=None).has("x") is False)
    add("VerifyRequest has False 算假",
        rq.with_(x=False).has("x") is False)

    rq2 = rq.with_(selector="#b", extra=1)
    add("with_ 覆盖参数", rq2.get("selector") == "#b")
    add("with_ 保留原参数（原有被覆盖）",
        rq.get("selector") == "#a" and rq2.get("extra") == 1)
    add("with_ 返回新对象", rq2 is not rq)
    add("keys 排序", rq2.keys() == ["extra", "selector"], rq2.keys())
    add("to_kwargs 是拷贝",
        rq2.to_kwargs() == {"selector": "#b", "extra": 1}, rq2.to_kwargs())

    add("describe 字段齐",
        set(rq.describe()) >= {"mode", "timeout", "settle", "prefer",
                               "is_web", "is_desktop", "params"},
        sorted(rq.describe()))

    try:
        VerifyRequest("bogus")
        add("非法 mode 抛 RouteError", False, "没抛")
    except RouteError:
        add("非法 mode 抛 RouteError", True)

    # 桌面请求
    rq3 = VerifyRequest(hwnd=123)
    add("桌面请求 is_desktop", rq3.is_desktop is True)
    add("桌面请求 is_web False", rq3.is_web is False)
    rq4 = VerifyRequest(hwnd=1, selector="#a")
    add("混合请求两边都 True", rq4.is_web and rq4.is_desktop)

    # --- 路由 ---
    add("auto 纯 web → web", _route(rq) == "web")
    add("auto 纯 desktop → desktop", _route(rq3) == "desktop")
    add("auto 混合 → web（更强）", _route(rq4) == "web")
    add("强制 desktop",
        _route(VerifyRequest("desktop", selector="#a")) == "desktop")
    add("强制 web",
        _route(VerifyRequest("web", hwnd=1)) == "web")
    add("空请求 → desktop", _route(VerifyRequest()) == "desktop")

    # --- verify_action: web 路径 ---
    s = _FakeSession(_obj_el(text="成功"))
    r = verify_action("web", session=s, selector="#a", expect_text="成功",
                      has_expect_text=True, timeout=0.3)
    add("verify_action web 成功", r.ok is True, r.describe())
    add("verify_action 填 mode", r.mode == "web", r.mode)
    add("verify_action 填 elapsed", r.elapsed >= 0)
    add("verify_action method 是 dom_text",
        r.method == "dom_text", r.method)

    r = verify_action("web", selector="#a")
    add("web 无 session → unknown", r.unknown is True, r.describe())
    add("web 无 session 标记",
        r.extra.get("no_session") is True, r.extra)

    # --- verify_action: desktop 路径（像素，用假 bitmap） ---
    from .desktop import _FakeBitmap, bitmap_hash
    img_a = _FakeBitmap(64, 64, lambda x, y: (255, 255, 255) if x < 32
                        else (0, 0, 0))
    # 局部结构变化 —— 不用整体镜像，pHash 对镜像不敏感（见 result.py 自检）
    img_b = _FakeBitmap(64, 64, lambda x, y: (0, 0, 0)
                        if (12 <= x < 30 and 12 <= y < 30) else (255, 255, 255))
    hb = bitmap_hash(img_a)

    r = verify_action("desktop", before_hash=hb, after_shot=img_b,
                      settle=0)
    add("verify_action desktop 像素变化 → ok", r.ok is True, r.describe())
    add("verify_action desktop mode", r.mode == "desktop", r.mode)

    r = verify_action("auto", before_hash=hb, after_shot=img_a, settle=0)
    add("verify_action auto 走 desktop", r.mode == "desktop", r.mode)

    r = verify_action("auto", session=s, selector="#a", expect_text="成功",
                      has_expect_text=True, timeout=0.3)
    add("verify_action auto 走 web", r.mode == "web", r.mode)

    r = verify_action("auto", session=s, selector="#a", expect_text="成功",
                      has_expect_text=True, before_hash=hb, after_shot=img_b,
                      timeout=0.3, settle=0)
    add("verify_action auto 混合走 web", r.mode == "web", r.mode)
    add("混合时结果仍是 ok", r.ok is True, r.describe())

    # --- desktop 无判据 → unknown ---
    r = verify_action("desktop")
    add("desktop 无判据 → unknown", r.unknown is True, r.describe())
    add("desktop 无判据 reason 提到判据", "参数" in r.reason, r.reason)

    # --- 传 request 对象 ---
    rq5 = VerifyRequest("web", timeout=0.3, session=s, selector="#a",
                        expect_text="成功", has_expect_text=True)
    r = verify_action(request=rq5)
    add("传 request 生效", r.ok is True, r.describe())

    r = verify_action(request=rq5, selector="#nope")
    add("request + kwargs 合并不崩", r.ok or r.failed or r.unknown)

    # --- 异常不抛 ---
    class _Boom(object):
        def call(self, *a, **k):
            raise RuntimeError("kaboom")

    r = verify_action("web", session=_Boom(), selector="#a", timeout=0.2)
    add("内部异常 → unknown 不抛", r.unknown is True, r.describe())
    add("内部异常 method=web", r.method == "web", r.method)

    # 参数对象本身错 → 抛
    try:
        verify_action("bogus_mode")
        add("非法 mode 抛 RouteError", False, "没抛")
    except RouteError:
        add("非法 mode 抛 RouteError", True)

    # --- verify_any ---
    r = verify_any([
        {"session": _FakeSession(_obj_el(False)), "selector": "#a",
         "expect_exists": True, "timeout": 0.2},
        {"session": _FakeSession(_obj_el(True, text="ok")), "selector": "#a",
         "expect_text": "ok", "has_expect_text": True, "timeout": 0.2},
    ])
    add("verify_any 第二个成功 → ok", r.ok is True, r.describe())
    add("verify_any 记录 hit_index", r.extra.get("hit_index") == 1, r.extra)
    add("verify_any 带 branches 证据",
        len(r.evidence.get("branches", [])) >= 2, r.evidence.keys())

    r = verify_any([
        {"session": _FakeSession(_obj_el(False)), "selector": "#a",
         "expect_exists": True, "timeout": 0.2},
    ])
    add("verify_any 全失败 → fail", r.failed is True, r.describe())

    r = verify_any([])
    add("verify_any 空 → unknown", r.unknown is True, r.describe())
    add("verify_any 空 reason 可读", "分支" in r.reason, r.reason)

    r = verify_any([VerifyResult.ok_(), VerifyResult.fail()])
    add("verify_any 接受 VerifyResult", r.ok is True, r.describe())

    r = verify_any([VerifyResult.fail(), "垃圾"])
    add("verify_any 全 fail + 垃圾 → unknown", r.unknown is True, r.describe())

    r = verify_any([VerifyResult.fail(), VerifyResult.fail()])
    add("verify_any 两个 fail → fail", r.failed is True, r.describe())

    # --- verify_all ---
    r = verify_all([VerifyResult.ok_(), VerifyResult.ok_()])
    add("verify_all 全 ok → ok", r.ok is True, r.describe())

    r = verify_all([VerifyResult.ok_(), VerifyResult.fail()])
    add("verify_all 有 fail → fail", r.failed is True, r.describe())
    add("verify_all 记录 fail 索引",
        r.evidence.get("failed_indexes") == [1], r.evidence)

    r = verify_all([VerifyResult.ok_(), VerifyResult.unknown_()])
    add("verify_all 有 unknown → unknown", r.unknown is True, r.describe())
    add("verify_all 记录 unknown 索引",
        r.evidence.get("unknown_indexes") == [1], r.evidence)

    r = verify_all([VerifyResult.fail(), VerifyResult.unknown_()])
    add("verify_all fail 优先于 unknown", r.failed is True, r.describe())

    r = verify_all([])
    add("verify_all 空 → unknown", r.unknown is True, r.describe())

    # 用 request 对象
    reqs = [VerifyRequest("web", timeout=0.3, session=_FakeSession(
        _obj_el(True, text="ok")), selector="#a", expect_text="ok",
        has_expect_text=True)]
    r = verify_all(reqs)
    add("verify_all 接受 VerifyRequest", r.ok is True, r.describe())
    r = verify_any(reqs)
    add("verify_any 接受 VerifyRequest", r.ok is True, r.describe())

    # --- describe_verify ---
    add("describe_verify VerifyResult",
        "ok" in describe_verify(VerifyResult.ok_()).lower(),
        describe_verify(VerifyResult.ok_()))
    add("describe_verify dict",
        "ok" in describe_verify({"verdict": "ok", "method": "m",
                                 "reason": "r"}), 
        describe_verify({"verdict": "ok", "method": "m", "reason": "r"}))
    add("describe_verify 垃圾输入不抛",
        isinstance(describe_verify(12345), str),
        describe_verify(12345))

    # --- summarize 配合 ---
    s2 = summarize([VerifyResult.ok_(), VerifyResult.ok_()])
    add("summarize 与 action 协同", s2["all_ok"] is True, s2)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "verify.action"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
