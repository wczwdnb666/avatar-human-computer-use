# -*- coding: utf-8 -*-
"""页面导航：打开 / 跳转 / 前进 / 后退 / 等待加载。

## 为什么"等加载完成"是本模块的核心难点

``Page.navigate`` 的响应**只表示请求被接受**，不表示页面加载完了。
天真的做法是 sleep 两秒 —— 在网络慢或页面有大量异步资源时必然翻车。

正确做法是订阅 ``Page.loadEventFired``。但这里有三个坑：

1. **必须在 navigate 之前订阅**。事件可能在 navigate 响应回来之前
   就发出了（本地文件、缓存命中）。先 navigate 再等 = 永远等不到。
2. **老页面的 loadEventFired 会误触发**。如果你在页面 A 上订阅，
   然后在 A 还没加载完时导航到 B，A 的 load 事件会先到。
   解法：导航前先 ``drain_events`` 清空队列，并且用 ``loaderId`` /
   frameId 过滤（``Page.frameNavigated`` 里能拿到新页面的 loaderId）。
3. **有些页面永远不触发 loadEventFired**（比如 SPA 用
   history.pushState、或者有挂起的资源）。所以必须有超时兜底，
   超时后**不算失败**，而是返回 ``loaded=False`` 让调用方决定。

另外还有 ``Page.lifecycleEvent``（更细粒度：DOMContentLoaded /
load / networkIdle），但它的可用性依赖 ``Page.setLifecycleEventsEnabled``，
而且不同 Chrome 版本事件名有差异，所以本模块**只依赖 loadEventFired**，
把 lifecycle 作为可选增强。

## frameId 与 loaderId

- ``frameId``：主 frame 的 id，跨导航**不变**（tab 不变就是同一个主 frame）
- ``loaderId``：每次导航**都变**，是区分"新页面的 load"和
  "老页面的 load"的唯一可靠依据

所以等待逻辑里必须抓 ``Page.frameNavigated`` 事件取新 loaderId，
再等 ``loadEventFired`` 且其 loaderId 匹配。
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

from ..errors import AvatarError, TimeoutError_
from .session import CdpError, CdpEvent, CdpTimeout

__all__ = [
    "NavigationError",
    "NavigationResult",
    "enable_domains",
    "navigate",
    "wait_for_load",
    "wait_for_ready_state",
    "reload",
    "go_back",
    "go_forward",
    "wait_for_url",
    "wait_for_condition",
    "stop_loading",
]


class NavigationError(AvatarError):
    """导航失败（CDP 明确报错、或者页面报告了错误）。"""

    exit_code = 14


class NavigationResult:
    """一次导航的结果。

    ``ok`` 表示 navigate 调用本身成功。``loaded`` 表示观察到
    ``loadEventFired``。两者**可以不一致** —— 请求成功但资源挂起时
    ``ok=True, loaded=False``，这是真实存在且需要如实报告的状态。
    """

    __slots__ = ("ok", "url", "frame_id", "loader_id", "loaded",
                 "elapsed", "error", "ready_state")

    def __init__(self, ok: bool, url: str = "", frame_id: str = "",
                 loader_id: str = "", loaded: bool = False,
                 elapsed: float = 0.0, error: str = "",
                 ready_state: str = "") -> None:
        self.ok = ok
        self.url = url
        self.frame_id = frame_id
        self.loader_id = loader_id
        self.loaded = loaded
        self.elapsed = elapsed
        self.error = error
        self.ready_state = ready_state

    def describe(self) -> dict:
        return {
            "ok": self.ok,
            "url": self.url,
            "frame_id": self.frame_id[:12] if self.frame_id else "",
            "loader_id": self.loader_id[:12] if self.loader_id else "",
            "loaded": self.loaded,
            "elapsed": round(self.elapsed, 3),
            "ready_state": self.ready_state,
            "error": self.error,
        }

    def __repr__(self) -> str:
        return ("<NavigationResult ok=%s loaded=%s %.2fs %s>"
                % (self.ok, self.loaded, self.elapsed,
                   (self.url or "")[:48]))


# --------------------------------------------------------------------------
# 域启用
# --------------------------------------------------------------------------
def enable_domains(sess, session_id: str = "", *,
                   domains: Optional[List[str]] = None) -> dict:
    """启用导航所需的基础域。

    默认只开 ``Page`` / ``Runtime`` / ``DOM``。``Network`` 会带来
    大量事件开销，需要时自己单独开。
    """
    if domains is None:
        domains = ["Page", "Runtime", "DOM"]
    return sess.enable(*domains, session_id=session_id or None)


# --------------------------------------------------------------------------
# 导航
# --------------------------------------------------------------------------
def navigate(sess, url: str, *, session_id: str = "",
             timeout: float = 20.0,
             wait_load: bool = True,
             wait_ready_state: bool = True,
             referrer: str = "") -> NavigationResult:
    """导航到 URL 并（可选）等加载完成。

    :param wait_load: 是否等 ``loadEventFired``
    :param wait_ready_state: 加载后是否再等 ``document.readyState``
        变成 ``complete``。有些页面 load 事件到了但 DOM 还在拼 ——
        这一层是双保险。
    """
    t0 = time.monotonic()

    # ⚠️ 清空事件队列。理由见模块 docstring 的坑 2：
    # 上一页面的 loadEventFired 可能还在队列里，不清就会立刻"等到"。
    try:
        sess.drain_events("Page.loadEventFired")
        sess.drain_events("Page.frameNavigated")
    except Exception:
        pass

    params = {"url": url}
    if referrer:
        params["referrer"] = referrer

    try:
        res = sess.call("Page.navigate", params,
                        session_id=session_id or None, timeout=timeout)
    except CdpError as exc:
        return NavigationResult(
            ok=False, url=url, elapsed=time.monotonic() - t0,
            error="导航调用失败：%s" % exc)
    except (CdpTimeout, TimeoutError_) as exc:
        return NavigationResult(
            ok=False, url=url, elapsed=time.monotonic() - t0,
            error="导航调用超时：%s" % exc)

    # navigate 的响应里也带 loaderId —— 比等事件更直接
    err = res.get("errorText") or ""
    frame_id = res.get("frameId") or ""
    loader_id = res.get("loaderId") or ""

    if err:
        return NavigationResult(
            ok=False, url=url, frame_id=frame_id, loader_id=loader_id,
            elapsed=time.monotonic() - t0, error="页面报告错误：%s" % err)

    result = NavigationResult(
        ok=True, url=url, frame_id=frame_id, loader_id=loader_id,
        elapsed=time.monotonic() - t0)

    if not wait_load:
        return result

    remain = timeout - (time.monotonic() - t0)
    if remain <= 0:
        result.error = "navigate 耗时已超过总超时，跳过等待加载"
        return result

    loaded = wait_for_load(sess, timeout=remain, session_id=session_id,
                           loader_id=loader_id)
    result.loaded = loaded
    result.elapsed = time.monotonic() - t0
    if not loaded:
        result.error = "等 loadEventFired 超时（页面可能有挂起资源）"

    if wait_ready_state and loaded:
        remain2 = timeout - (time.monotonic() - t0)
        if remain2 > 0:
            result.ready_state = wait_for_ready_state(
                sess, timeout=min(remain2, 5.0), session_id=session_id)
            result.elapsed = time.monotonic() - t0

    return result


def wait_for_load(sess, *, timeout: float = 15.0, session_id: str = "",
                  loader_id: str = "") -> bool:
    """等 ``Page.loadEventFired``。

    ``loader_id`` 非空时只认匹配的 loader —— 这能挡住"老页面的 load
    提前到达"。传空串则不校验（兼容不提供 loaderId 的场景）。

    **超时返回 False 而不是抛异常** —— 调用方需要区分
    "导航失败"（抛）和"加载没等到"（超时）。
    """
    deadline = time.monotonic() + timeout

    def _match(ev: CdpEvent) -> bool:
        if not loader_id:
            return True
        return (ev.params.get("loaderId") or "") == loader_id

    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        try:
            sess.wait_event("Page.loadEventFired", timeout=remain,
                            predicate=_match if loader_id else None,
                            session_id=session_id or None)
            return True
        except CdpTimeout:
            return False
        except Exception:
            # 连接断了之类 —— 也算没等到
            return False


def wait_for_ready_state(sess, *, timeout: float = 5.0,
                         session_id: str = "",
                         target: str = "complete",
                         interval: float = 0.1) -> str:
    """轮询 ``document.readyState`` 直到达到 ``target``。

    返回最终的实际值。超时也返回实际值（不抛）—— 调用方看返回值判定。
    """
    order = {"loading": 0, "interactive": 1, "complete": 2}
    want = order.get(target, 2)
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            last = sess.evaluate("document.readyState",
                                 session_id=session_id or None,
                                 timeout=min(2.0, max(0.3, deadline - time.monotonic()))) or ""
        except Exception:
            last = last or ""
        if order.get(last, -1) >= want:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(interval)


# --------------------------------------------------------------------------
# 历史操作
# --------------------------------------------------------------------------
def reload(sess, *, session_id: str = "", ignore_cache: bool = False,
           timeout: float = 20.0, wait_load: bool = True) -> NavigationResult:
    """重新加载当前页。"""
    t0 = time.monotonic()
    try:
        sess.drain_events("Page.loadEventFired")
    except Exception:
        pass
    try:
        sess.call("Page.reload", {"ignoreCache": bool(ignore_cache)},
                  session_id=session_id or None, timeout=timeout)
    except CdpError as exc:
        return NavigationResult(ok=False, elapsed=time.monotonic() - t0,
                                error="reload 失败：%s" % exc)
    r = NavigationResult(ok=True, elapsed=time.monotonic() - t0)
    if wait_load:
        r.loaded = wait_for_load(sess, timeout=max(1.0, timeout - r.elapsed),
                                 session_id=session_id)
        r.elapsed = time.monotonic() - t0
    return r


def go_back(sess, *, session_id: str = "", timeout: float = 20.0,
            wait_load: bool = True) -> NavigationResult:
    """后退。用 ``Page.getNavigationHistory`` + ``Page.navigateToHistoryEntry``。

    ⚠️ 不能简单用 ``window.history.back()`` —— 那是异步的，
    而且历史栈为空时静默什么都不做。走 CDP 能明确知道有没有历史条目。
    """
    return _history_move(sess, -1, session_id=session_id, timeout=timeout,
                         wait_load=wait_load)


def go_forward(sess, *, session_id: str = "", timeout: float = 20.0,
               wait_load: bool = True) -> NavigationResult:
    """前进。"""
    return _history_move(sess, +1, session_id=session_id, timeout=timeout,
                         wait_load=wait_load)


def _history_move(sess, delta: int, *, session_id: str, timeout: float,
                  wait_load: bool) -> NavigationResult:
    t0 = time.monotonic()
    try:
        hist = sess.call("Page.getNavigationHistory",
                         session_id=session_id or None, timeout=timeout)
    except CdpError as exc:
        return NavigationResult(ok=False, elapsed=time.monotonic() - t0,
                                error="读导航历史失败：%s" % exc)

    entries = hist.get("entries") or []
    idx = int(hist.get("currentIndex", 0))
    nxt = idx + delta
    if nxt < 0:
        return NavigationResult(ok=False, elapsed=time.monotonic() - t0,
                                error="已经是历史最前，无法后退")
    if nxt >= len(entries):
        return NavigationResult(ok=False, elapsed=time.monotonic() - t0,
                                error="已经是历史最后，无法前进")

    entry = entries[nxt]
    try:
        sess.drain_events("Page.loadEventFired")
    except Exception:
        pass
    try:
        sess.call("Page.navigateToHistoryEntry", {"entryId": entry.get("id")},
                  session_id=session_id or None, timeout=timeout)
    except CdpError as exc:
        return NavigationResult(ok=False, elapsed=time.monotonic() - t0,
                                error="历史跳转失败：%s" % exc)

    r = NavigationResult(ok=True, url=entry.get("url") or "",
                         elapsed=time.monotonic() - t0)
    if wait_load:
        r.loaded = wait_for_load(sess, timeout=max(1.0, timeout - r.elapsed),
                                 session_id=session_id)
        r.elapsed = time.monotonic() - t0
    return r


def stop_loading(sess, *, session_id: str = "") -> None:
    """停止加载（对应浏览器地址栏的 × ）。"""
    try:
        sess.call("Page.stopLoading", session_id=session_id or None,
                  timeout=3.0)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 等待辅助
# --------------------------------------------------------------------------
def wait_for_url(sess, pattern, *, timeout: float = 15.0,
                 session_id: str = "", interval: float = 0.2) -> str:
    """等当前 URL 匹配 ``pattern``，返回命中的 URL。

    ``pattern`` 可以是字符串（子串匹配）或编译好的正则 / 可调用。
    超时抛 ``TimeoutError_`` —— 这个**是**失败，因为调用方明确要求
    等某个 URL 出现。
    """
    matcher = _make_matcher(pattern)
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            last = sess.evaluate("location.href",
                                 session_id=session_id or None,
                                 timeout=min(3.0, max(0.5, deadline - time.monotonic()))) or ""
        except Exception:
            last = ""
        if last and matcher(last):
            return last
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError_(
                "等 URL 匹配超时：%r（当前 %r）" % (pattern, last[:120]),
                elapsed=timeout)
        time.sleep(min(interval, remain))


def wait_for_condition(sess, expression: str, *, timeout: float = 15.0,
                       session_id: str = "", interval: float = 0.15,
                       poll_in_page: bool = False) -> bool:
    """轮询一段 JS 表达式直到它为真。

    ``poll_in_page=True`` 时把轮询放到页面里做（``setTimeout`` 循环），
    减少往返 —— 但会占用页面主线程，且页面若在导航中会丢失。
    默认在**客户端**轮询，更可控。

    超时返回 False（不抛）—— 这是"条件没满足"，不是错误。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            v = sess.evaluate("!!(%s)" % expression,
                              session_id=session_id or None,
                              timeout=min(3.0, max(0.5, deadline - time.monotonic())))
            if v:
                return True
        except Exception:
            pass
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        time.sleep(min(interval, remain))


def wait_for_element(sess, selector: str, *, timeout: float = 15.0,
                     session_id: str = "", visible: bool = False) -> bool:
    """等元素出现（或可见）。超时返回 False。"""
    if visible:
        expr = ("(()=>{const e=document.querySelector(%r);"
                "if(!e)return false;"
                "const r=e.getBoundingClientRect();"
                "const s=getComputedStyle(e);"
                "return r.width>0&&r.height>0&&s.visibility!=='hidden'"
                "&&s.display!=='none'&&s.opacity!=='0';})()" % selector)
    else:
        expr = "!!document.querySelector(%r)" % selector
    return wait_for_condition(sess, expr, timeout=timeout,
                              session_id=session_id)


def wait_for_text(sess, text: str, *, timeout: float = 15.0,
                  session_id: str = "") -> bool:
    """等页面文本里出现指定内容。"""
    expr = "document.body && document.body.innerText.includes(%r)" % text
    return wait_for_condition(sess, expr, timeout=timeout,
                              session_id=session_id)


def _make_matcher(pattern) -> Callable[[str], bool]:
    """把字符串 / 正则 / 可调用统一成 ``str -> bool``。"""
    if callable(pattern):
        return pattern
    if hasattr(pattern, "search"):     # 编译好的正则
        return lambda s: bool(pattern.search(s))
    p = str(pattern)
    return lambda s: p in s


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """导航自检（**不联网**）。

    用假的 sess 对象验证：loaderId 过滤、老事件不误触发、
    历史边界、matcher 归一化、NavigationResult 契约。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- matcher 归一化 -----------------------------------------------------
    m = _make_matcher("example")
    _add("字符串 pattern 做子串匹配",
         m("https://example.com/x") and not m("https://other.com"),
         "子串")
    import re as _re
    m2 = _make_matcher(_re.compile(r"\d{3}"))
    _add("正则 pattern", m2("abc123") and not m2("abc"), "正则")
    m3 = _make_matcher(lambda s: s.endswith(".png"))
    _add("可调用 pattern", m3("a.png") and not m3("a.jpg"), "callable")

    # -- loaderId 过滤：老页面的 load 不许误触发 ---------------------------
    class _FakeSess:
        def __init__(self, events):
            self._events = list(events)
            self.drained = []

        def drain_events(self, method=None):
            self.drained.append(method)
            if method is None:
                out, self._events = self._events, []
                return out
            keep, out = [], []
            for ev in self._events:
                (out if ev.method == method else keep).append(ev)
            self._events = keep
            return out

        def wait_event(self, method, *, timeout=1.0, predicate=None,
                       session_id=None):
            for i, ev in enumerate(self._events):
                if ev.method != method:
                    continue
                if predicate and not predicate(ev):
                    continue
                del self._events[i]
                return ev
            raise CdpTimeout("超时", elapsed=timeout, method=method)

        def call(self, method, params=None, **kw):
            return {}

        def evaluate(self, expr, **kw):
            return ""

    # 队列里塞一个"老页面"的 load（loaderId=OLD），
    # 等的是 NEW —— 必须等不到，证明过滤生效
    old_load = CdpEvent("Page.loadEventFired", {"loaderId": "OLD"})
    s = _FakeSess([old_load])
    got = wait_for_load(s, timeout=0.2, loader_id="NEW")
    _add("loaderId 不匹配不误触发", got is False, "got=%s" % got)

    # 匹配时能拿到
    new_load = CdpEvent("Page.loadEventFired", {"loaderId": "NEW"})
    s2 = _FakeSess([new_load])
    got = wait_for_load(s2, timeout=0.5, loader_id="NEW")
    _add("loaderId 匹配即返回", got is True, "got=%s" % got)

    # 不传 loaderId 时兼容（任意 load 都算）
    s3 = _FakeSess([CdpEvent("Page.loadEventFired", {})])
    got = wait_for_load(s3, timeout=0.5)
    _add("不传 loaderId 时任意 load 都算", got is True, "got=%s" % got)

    # -- 超时返回 False 不抛 -----------------------------------------------
    s4 = _FakeSess([])
    try:
        got = wait_for_load(s4, timeout=0.15)
        _add("无事件时超时返回 False", got is False, "got=%s" % got)
    except Exception as exc:      # noqa: BLE001
        _add("无事件时超时返回 False", False,
             "抛了 %s" % type(exc).__name__)

    # -- 历史边界 -----------------------------------------------------------
    class _HistSess:
        def __init__(self, entries, idx):
            self._hist = {"entries": entries, "currentIndex": idx}
            self.history_calls = []

        def call(self, method, params=None, **kw):
            if method == "Page.getNavigationHistory":
                return self._hist
            if method == "Page.navigateToHistoryEntry":
                self.history_calls.append(params)
                return {}
            return {}

        def drain_events(self, method=None):
            return []

    entries = [{"id": 1, "url": "https://a"},
               {"id": 2, "url": "https://b"}]

    hs = _HistSess(entries, 0)
    r = go_back(hs, wait_load=False, timeout=1.0)
    _add("已在最前时后退被拒绝",
         r.ok is False and "最前" in r.error, r.error)

    hs2 = _HistSess(entries, 0)
    r = go_forward(hs2, wait_load=False, timeout=1.0)
    _add("前进到第二条成功",
         r.ok and hs2.history_calls
         and hs2.history_calls[0].get("entryId") == 2,
         "calls=%s" % hs2.history_calls)
    _add("前进结果带目标 URL", r.url == "https://b", r.url)

    hs3 = _HistSess(entries, 1)
    r = go_forward(hs3, wait_load=False, timeout=1.0)
    _add("已在最后时前进被拒绝",
         r.ok is False and "最后" in r.error, r.error)

    # -- NavigationResult 契约 ---------------------------------------------
    nr = NavigationResult(ok=True, url="https://x", loaded=False,
                          elapsed=1.234, error="等 load 超时")
    d = nr.describe()
    _add("NavigationResult 字段完整",
         {"ok", "url", "loaded", "elapsed", "error"} <= set(d),
         "%d 个字段" % len(d))
    _add("ok 与 loaded 可分离",
         nr.ok is True and nr.loaded is False,
         "请求成功但加载未完成 —— 合法状态")

    # -- navigate 在 CDP 报错时返回 ok=False 而不是抛 ----------------------
    class _ErrSess:
        def drain_events(self, method=None):
            return []

        def call(self, method, params=None, **kw):
            raise CdpError("boom", code=-32000, method=method)

    r = navigate(_ErrSess(), "https://x", timeout=1.0, wait_load=False)
    _add("navigate 遇 CDP 错误返回 ok=False",
         r.ok is False and "失败" in r.error, r.error)

    # -- navigate 时清空旧事件（防误触发）----------------------------------
    class _DrainSess:
        def __init__(self):
            self.drained = []

        def drain_events(self, method=None):
            self.drained.append(method)
            return []

        def call(self, method, params=None, **kw):
            return {"frameId": "F1", "loaderId": "L1"}

        def wait_event(self, method, **kw):
            raise CdpTimeout("x", elapsed=0.1, method=method)

        def evaluate(self, e, **kw):
            return "complete"

    ds = _DrainSess()
    navigate(ds, "https://y", timeout=1.0, wait_load=False)
    _add("navigate 前清空旧 load 事件",
         "Page.loadEventFired" in ds.drained,
         "%s" % ds.drained)

    # -- 页面报错时 ok=False -----------------------------------------------
    class _TextSess:
        def drain_events(self, method=None):
            return []

        def call(self, method, params=None, **kw):
            return {"frameId": "F", "loaderId": "L",
                    "errorText": "net::ERR_NAME_NOT_RESOLVED"}

    r = navigate(_TextSess(), "https://nope.invalid", timeout=1.0,
                 wait_load=False)
    _add("errorText 非空时 ok=False",
         r.ok is False and "ERR_NAME" in r.error, r.error)

    return {"checks": checks, "failures": failures, "ok": not failures}
