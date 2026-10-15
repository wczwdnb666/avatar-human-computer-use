# -*- coding: utf-8 -*-
"""标签页 / 目标管理：``Target.*`` 域。

Chrome 里"标签页"的正式叫法是 **target**。CDP 的 target 体系：

============================  ========================================
``page``                      普通标签页
``iframe``                    页面内的 iframe（OOPIF 会独立成 target）
``worker``                    Web Worker
``service_worker``            Service Worker
``shared_worker``             Shared Worker
``browser``                   浏览器本身（唯一）
``other``                     devtools 前端、扩展后台页等
============================  ========================================

## 扁平会话（flatten）下的两种连法

**A. 浏览器端点 + attachToTarget（推荐）**
``ws://127.0.0.1:9222/devtools/browser/<id>`` 连上去，
``Target.attachToTarget {targetId, flatten:true}`` 拿到 ``sessionId``，
之后所有页面级方法都带这个 sessionId 复用同一条 WebSocket。
优点：一条连接管所有页；新增标签页用 ``Target.setAutoAttach`` 自动收。

**B. 页面端点**
``/json/list`` 拿到每个页的 ``webSocketDebuggerUrl``，一页一条连接。
简单，但页多了连接就多，且**拿不到浏览器级命令**。

本模块两条都支持：``TabManager`` 绑定浏览器级会话，
``list_tabs`` 在拿到的是页面会话时自动退化成只能看见自己（返回单条）。

## 坑

1. **``Target.getTargets`` 返回的 ``id`` 是 targetId**，不是 sessionId。
   两者不能混用 —— 传错会报 "Session with given id not found"。
2. **``Target.attachToTarget`` 对同一 target 重复调用**会返回**同一个**
   sessionId（Chrome 会去重），所以 attach 是幂等的，不用担心泄漏。
3. **关闭标签页用 ``Target.closeTarget {targetId}``**，
   不是 ``Page.close``。``Target.closeTarget`` 对浏览器自身的 target 会失败。
4. **激活标签页**（切到前台）用 ``Target.activateTarget``，
   但**在无头模式下无意义**（还会返回 "Not supported"），要能容忍失败。
5. **新开的标签页是 ``about:blank``**，需要显式导航；
   而且刚 attach 时 Page 域可能还没 enable，要先 enable 再操作。
6. **``/json/list`` 里 ``type`` 为 ``page`` 的才用户可见**，
   ``background_page`` / ``service_worker`` 别当标签页数给用户看。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..errors import AvatarError
from .session import CdpError

__all__ = [
    "TabError",
    "TargetInfo",
    "TabManager",
    "list_tabs",
    "find_tab",
    "new_tab",
    "close_tab",
    "activate_tab",
    "switch_tab",
    "get_targets",
    "attach_tab",
    "set_auto_attach",
    "wait_for_tab",
    "selfcheck",
]


class TabError(AvatarError):
    """标签页操作失败。"""

    exit_code = 19


# 用户能"看见"的 target 类型。其余是内部实现细节。
USER_VISIBLE_TYPES = ("page", "tab", "webview")


class TargetInfo(object):
    """一个 target 的快照。

    ``target_id`` 用于 ``Target.*`` 命令；
    ``session_id`` 只有在 attach 过之后才有值。
    """

    __slots__ = ("target_id", "type", "title", "url", "attached",
                 "opener_id", "browser_context_id", "session_id")

    def __init__(self, raw: Dict[str, Any]) -> None:
        raw = raw or {}
        self.target_id = raw.get("targetId") or ""
        self.type = raw.get("type") or ""
        self.title = raw.get("title") or ""
        self.url = raw.get("url") or ""
        self.attached = bool(raw.get("attached"))
        self.opener_id = raw.get("openerId") or ""
        self.browser_context_id = raw.get("browserContextId") or ""
        self.session_id = None  # type: Optional[str]

    @property
    def visible(self) -> bool:
        """是否算"一个标签页"（给用户数数的口径）。"""
        return self.type in USER_VISIBLE_TYPES and not self.is_devtools

    @property
    def is_devtools(self) -> bool:
        u = (self.url or "").lower()
        return u.startswith("devtools://")

    @property
    def blank(self) -> bool:
        return (self.url or "") in ("", "about:blank")

    def describe(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "type": self.type,
            "title": self.title[:120],
            "url": self.url[:300],
            "attached": self.attached,
            "visible": self.visible,
            "session_id": self.session_id,
        }

    def matches(self, *, url_contains: Optional[str] = None,
                title_contains: Optional[str] = None,
                type_: Optional[str] = None,
                target_id: Optional[str] = None) -> bool:
        if target_id is not None and self.target_id != target_id:
            return False
        if type_ is not None and self.type != type_:
            return False
        if url_contains is not None:
            if url_contains.lower() not in (self.url or "").lower():
                return False
        if title_contains is not None:
            if title_contains.lower() not in (self.title or "").lower():
                return False
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return "<Target %s %s %r>" % (self.type, self.target_id[:8], self.url[:60])


# --------------------------------------------------------------------------
# 底层查询
# --------------------------------------------------------------------------

def get_targets(
    sess: Any,
    *,
    session_id: Optional[str] = None,
    filter_type: Optional[Any] = None,
    timeout: float = 10.0,
) -> List[TargetInfo]:
    """列全部 target。``filter_type`` 可以是字符串或字符串集合。

    传页面级会话时会自动代理到 ``browser_session``（若挂了），
    这样 ``list_tabs(page_session)`` 也能拿到全部标签页。
    """
    bs = _browser_session(sess)
    try:
        resp = bs.call("Target.getTargets", {}, session_id=session_id,
                       timeout=timeout)
    except Exception:
        return []

    infos = []
    for raw in (resp or {}).get("targetInfos") or []:
        try:
            infos.append(TargetInfo(raw))
        except Exception:
            continue

    if filter_type:
        if isinstance(filter_type, str):
            allowed = {filter_type}
        else:
            allowed = set(filter_type)
        infos = [t for t in infos if t.type in allowed]
    return infos


def list_tabs(
    sess: Any,
    *,
    session_id: Optional[str] = None,
    visible_only: bool = True,
    timeout: float = 10.0,
) -> List[TargetInfo]:
    """列标签页。默认只给用户可见的（排除 devtools / worker）。"""
    pages = get_targets(sess, session_id=session_id,
                        filter_type=USER_VISIBLE_TYPES, timeout=timeout)
    if visible_only:
        pages = [t for t in pages if t.visible]
    return pages


def find_tab(
    sess: Any,
    *,
    url_contains: Optional[str] = None,
    title_contains: Optional[str] = None,
    session_id: Optional[str] = None,
    index: int = 0,
    timeout: float = 10.0,
) -> Optional[TargetInfo]:
    """按条件找标签页。找不到返回 None。

    ``index`` 用于多个命中时选第几个（负数表示从后往前）。
    """
    hits = [t for t in list_tabs(sess, session_id=session_id, timeout=timeout)
            if t.matches(url_contains=url_contains,
                         title_contains=title_contains)]
    if not hits:
        return None
    try:
        return hits[index]
    except IndexError:
        return None


def _browser_session(sess: Any) -> Any:
    """挑出能执行 ``Target.*`` 的会话。

    ``BrowserSession`` 本身就是浏览器级的；页面级会话需先取 ``browser_session``
    属性（``BrowserSession`` 会挂），拿不到就退回自己（多数 CDP 实现里
    ``Target.*`` 在页面会话上也能跑，只是作用域受限）。
    """
    bs = getattr(sess, "browser_session", None)
    if bs is not None:
        return bs
    return sess


def new_tab(
    sess: Any,
    url: str = "about:blank",
    *,
    timeout: float = 20.0,
    background: bool = False,
    width: int = 0,
    height: int = 0,
) -> Optional[TargetInfo]:
    """开新标签页。

    ``background=True`` 时用 ``newWindow:false`` + 不激活；
    返回 ``TargetInfo``，**已自动 attach 并 enable 了 Page/Runtime/DOM**。
    """
    bs = _browser_session(sess)
    params = {"url": url or "about:blank"}
    if background:
        params["background"] = True
    if width and height:
        params["width"] = int(width)
        params["height"] = int(height)

    try:
        resp = bs.call("Target.createTarget", params, timeout=timeout)
    except Exception as exc:
        raise TabError("创建标签页失败: %s" % (exc,))

    tid = (resp or {}).get("targetId") or ""
    if not tid:
        return None

    info = TargetInfo({"targetId": tid, "type": "page", "url": url})
    sid = attach_tab(bs, tid, timeout=timeout)
    if sid:
        info.session_id = sid
        info.attached = True
        try:
            bs.enable("Page", "Runtime", "DOM", session_id=sid)
        except Exception:
            pass
    return info


def close_tab(
    sess: Any,
    target_id: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """关标签页。成功返回 True。"""
    bs = _browser_session(sess)
    if not target_id:
        return False
    try:
        resp = bs.call("Target.closeTarget", {"targetId": target_id},
                       timeout=timeout)
        return bool((resp or {}).get("success"))
    except Exception:
        return False


def activate_tab(
    sess: Any,
    target_id: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """把标签页切到前台。

    **无头模式下会失败** —— 这不是错误，返回 False 让调用方决定。
    """
    bs = _browser_session(sess)
    if not target_id:
        return False
    try:
        bs.call("Target.activateTarget", {"targetId": target_id},
                timeout=timeout)
        return True
    except Exception:
        return False


def attach_tab(
    sess: Any,
    target_id: str,
    *,
    flatten: bool = True,
    timeout: float = 10.0,
) -> Optional[str]:
    """attach 到 target，返回 sessionId。

    重复 attach 同一 target 会拿到**同一个** sessionId（Chrome 去重），
    所以可以放心把它当幂等操作使。
    """
    bs = _browser_session(sess)
    if not target_id:
        return None
    try:
        resp = bs.call("Target.attachToTarget",
                       {"targetId": target_id, "flatten": bool(flatten)},
                       timeout=timeout)
    except Exception:
        return None
    return (resp or {}).get("sessionId") or None


def detach_tab(sess: Any, session_id: str, *, timeout: float = 10.0) -> bool:
    bs = _browser_session(sess)
    try:
        bs.call("Target.detachFromTarget", {"sessionId": session_id},
                timeout=timeout)
        return True
    except Exception:
        return False


def set_auto_attach(
    sess: Any,
    *,
    enable: bool = True,
    session_id: Optional[str] = None,
    wait_for_debugger: bool = False,
    filter_: Optional[List[Dict[str, Any]]] = None,
    timeout: float = 10.0,
) -> bool:
    """打开/关闭自动 attach。

    开了之后新开的标签页会通过 ``Target.attachedToTarget`` 事件
    自动推过来 —— 这是"点了个链接弹新窗"场景的唯一正确抓法，
    轮询 ``getTargets`` 会漏（弹窗可能瞬间关闭）。

    ``filter_`` 形如 ``[{"type": "page", "exclude": False}]``。
    """
    bs = _browser_session(sess)
    params = {
        "autoAttach": bool(enable),
        "waitForDebuggerOnStart": bool(wait_for_debugger),
        "flatten": True,
    }
    if filter_:
        params["filter"] = filter_
    try:
        bs.call("Target.setAutoAttach", params, session_id=session_id,
                timeout=timeout)
        return True
    except Exception:
        return False


def set_discover_targets(sess: Any, *, enable: bool = True,
                         session_id: Optional[str] = None,
                         timeout: float = 10.0) -> bool:
    """开关 ``Target.targetCreated`` / ``targetDestroyed`` 事件。"""
    bs = _browser_session(sess)
    try:
        bs.call("Target.setDiscoverTargets", {"discover": bool(enable)},
                session_id=session_id, timeout=timeout)
        return True
    except Exception:
        return False


def switch_tab(
    sess: Any,
    *,
    url_contains: Optional[str] = None,
    title_contains: Optional[str] = None,
    target_id: Optional[str] = None,
    index: int = 0,
    activate: bool = True,
    timeout: float = 10.0,
) -> Optional[TargetInfo]:
    """找到并切到某个标签页，返回它（已带 session_id）。

    这是日常最常用的一个：``switch_tab(url_contains="taobao")``。
    """
    bs = _browser_session(sess)

    info = None  # type: Optional[TargetInfo]
    if target_id:
        for t in list_tabs(bs, visible_only=False, timeout=timeout):
            if t.target_id == target_id:
                info = t
                break
    else:
        info = find_tab(bs, url_contains=url_contains,
                        title_contains=title_contains, index=index,
                        timeout=timeout)

    if info is None:
        return None

    sid = attach_tab(bs, info.target_id, timeout=timeout)
    if sid:
        info.session_id = sid
        info.attached = True
        try:
            bs.enable("Page", "Runtime", "DOM", session_id=sid)
        except Exception:
            pass

    if activate:
        activate_tab(bs, info.target_id, timeout=timeout)
    return info


def wait_for_tab(
    sess: Any,
    *,
    url_contains: Optional[str] = None,
    title_contains: Optional[str] = None,
    timeout: float = 10.0,
    interval: float = 0.2,
    exclude_ids: Optional[Any] = None,
) -> Optional[TargetInfo]:
    """等某个标签页出现。超时返回 None（不抛）。

    ``exclude_ids`` 用于"等**新**标签页" —— 把已知的 id 集合传进来，
    只有不在集合里的才算新。比"等 URL 变化"可靠。
    """
    ex = set(exclude_ids or ())
    deadline = time.time() + max(0.0, timeout)
    while True:
        for t in list_tabs(sess, visible_only=False, timeout=5.0):
            if t.target_id in ex:
                continue
            if t.matches(url_contains=url_contains,
                         title_contains=title_contains):
                return t
        if time.time() >= deadline:
            return None
        time.sleep(max(0.02, interval))


def tab_ids(sess: Any, **kw) -> List[str]:
    """取当前所有可见标签页 id（配合 wait_for_tab 的 exclude 用）。"""
    return [t.target_id for t in list_tabs(sess, **kw)]


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

class _FakeBrowser(object):
    """假浏览器级会话。"""

    def __init__(self) -> None:
        self.calls = []
        self.targets = []
        self.session_counter = 0
        self.attached = {}
        self.fail_methods = set()

    def call(self, method, params=None, *, session_id=None, timeout=None,
             **kw):
        self.calls.append((method, dict(params or {})))
        if method in self.fail_methods:
            raise CdpError("boom", code=-1, method=method)
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.createTarget":
            self.session_counter += 1
            tid = "T%03d" % self.session_counter
            self.targets.append({"targetId": tid, "type": "page",
                                 "url": params.get("url") or "about:blank",
                                 "title": "", "attached": False})
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            tid = params.get("targetId")
            if tid in self.attached:
                return {"sessionId": self.attached[tid]}
            sid = "S-%s" % tid
            self.attached[tid] = sid
            return {"sessionId": sid}
        if method == "Target.closeTarget":
            tid = params.get("targetId")
            before = len(self.targets)
            self.targets = [t for t in self.targets if t["targetId"] != tid]
            return {"success": len(self.targets) != before}
        if method == "Target.activateTarget":
            return {}
        if method in ("Target.setAutoAttach", "Target.setDiscoverTargets",
                      "Target.detachFromTarget"):
            return {}
        raise AssertionError("unexpected call: %s" % method)

    def enable(self, *domains, **kw):
        return {}


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- TargetInfo ---
    t = TargetInfo({"targetId": "AAA", "type": "page",
                    "title": "百度一下", "url": "https://www.baidu.com/",
                    "attached": True})
    add("target_id 解析", t.target_id == "AAA")
    add("type 解析", t.type == "page")
    add("title 解析", t.title == "百度一下")
    add("attached 解析", t.attached is True)
    add("page 算可见", t.visible is True)
    add("非 page 不算可见",
        TargetInfo({"type": "worker", "url": "x"}).visible is False)
    add("devtools 不算可见",
        TargetInfo({"type": "page", "url": "devtools://x"}).visible is False)
    add("blank 判定", TargetInfo({"type": "page",
                                 "url": "about:blank"}).blank is True)
    add("空 url 也算 blank", TargetInfo({"type": "page"}).blank is True)
    add("init 时 session_id 为 None", t.session_id is None)

    d = t.describe()
    add("describe 字段齐",
        set(d) >= {"target_id", "type", "title", "url", "attached",
                   "visible", "session_id"}, sorted(d))

    # matches
    add("matches url_contains 命中",
        t.matches(url_contains="baidu") is True)
    add("matches url_contains 大小写不敏感",
        t.matches(url_contains="BAIDU") is True)
    add("matches url_contains 未命中",
        t.matches(url_contains="google") is False)
    add("matches title_contains 命中",
        t.matches(title_contains="百度") is True)
    add("matches target_id 命中", t.matches(target_id="AAA") is True)
    add("matches target_id 未命中", t.matches(target_id="BBB") is False)
    add("matches type 命中", t.matches(type_="page") is True)
    add("matches 无条件时 True", t.matches() is True)
    add("matches 多条件与语义",
        t.matches(url_contains="baidu", title_contains="google") is False)

    # --- get_targets ---
    s = _FakeBrowser()
    s.targets = [
        {"targetId": "1", "type": "page", "url": "https://a.com", "title": "A"},
        {"targetId": "2", "type": "page", "url": "devtools://x", "title": "D"},
        {"targetId": "3", "type": "worker", "url": "w.js", "title": ""},
        {"targetId": "4", "type": "page", "url": "https://b.com", "title": "B"},
    ]
    all_t = get_targets(s)
    add("get_targets 全量 4 条", len(all_t) == 4, len(all_t))
    add("get_targets filter_type 字符串",
        [x.target_id for x in get_targets(s, filter_type="page")] == ["1", "2", "4"],
        [x.target_id for x in get_targets(s, filter_type="page")])
    add("get_targets filter_type 集合",
        len(get_targets(s, filter_type={"page", "worker"})) == 4)

    vis = list_tabs(s)
    add("list_tabs 排除 devtools 与 worker",
        [x.target_id for x in vis] == ["1", "4"], [x.target_id for x in vis])
    add("list_tabs visible_only=False 含 devtools",
        len(list_tabs(s, visible_only=False)) >= 3,
        len(list_tabs(s, visible_only=False)))

    s.fail_methods.add("Target.getTargets")
    add("getTargets 失败返回空列表", get_targets(s) == [])
    add("list_tabs 失败返回空列表", list_tabs(s) == [])
    s.fail_methods.discard("Target.getTargets")

    # --- find_tab ---
    add("find_tab url 命中", find_tab(s, url_contains="a.com").target_id == "1")
    add("find_tab title 命中",
        find_tab(s, title_contains="B").target_id == "4")
    add("find_tab 未命中返回 None",
        find_tab(s, url_contains="zzz") is None)
    add("find_tab index 越界返回 None",
        find_tab(s, url_contains=".com", index=9) is None)
    add("find_tab index=-1 取最后一个",
        find_tab(s, url_contains=".com", index=-1).target_id == "4")

    # --- new_tab ---
    s = _FakeBrowser()
    nt = new_tab(s, "https://c.com")
    add("new_tab 返回 TargetInfo", nt is not None and isinstance(nt, TargetInfo))
    add("new_tab url 正确", nt.url == "https://c.com")
    add("new_tab 有 session_id", nt.session_id == "S-T001", nt.session_id)
    add("new_tab attached True", nt.attached is True)
    add("new_tab 调了 createTarget",
        ("Target.createTarget", {"url": "https://c.com"}) == s.calls[0],
        s.calls[0])
    add("new_tab 自动 attach",
        any(c[0] == "Target.attachToTarget" for c in s.calls))

    s2 = _FakeBrowser()
    new_tab(s2, "x", background=True)
    add("new_tab background 传参",
        s2.calls[0][1].get("background") is True, s2.calls[0][1])

    s3 = _FakeBrowser()
    s3.fail_methods.add("Target.createTarget")
    try:
        new_tab(s3, "x")
        add("new_tab 失败抛 TabError", False, "没抛")
    except TabError:
        add("new_tab 失败抛 TabError", True)

    # --- attach_tab ---
    s = _FakeBrowser()
    a1 = attach_tab(s, "T9")
    a2 = attach_tab(s, "T9")
    add("attach_tab 返回 sessionId", a1 == "S-T9", a1)
    add("attach_tab 幂等（同 target 同 sid）", a1 == a2, (a1, a2))
    add("attach_tab 空 target 返回 None", attach_tab(s, "") is None)
    s.fail_methods.add("Target.attachToTarget")
    add("attach_tab 异常返回 None", attach_tab(s, "T1") is None)

    s = _FakeBrowser()
    add("detach_tab 成功 True", detach_tab(s, "S-1") is True)
    s.fail_methods.add("Target.detachFromTarget")
    add("detach_tab 失败 False", detach_tab(s, "S-1") is False)

    # --- close_tab ---
    s = _FakeBrowser()
    s.targets = [{"targetId": "1", "type": "page", "url": "a"},
                 {"targetId": "2", "type": "page", "url": "b"}]
    add("close_tab 成功", close_tab(s, "1") is True)
    add("close_tab 后少一个", len(s.targets) == 1, len(s.targets))
    add("close_tab 不存在的 target 返回 False", close_tab(s, "999") is False)
    add("close_tab 空 id 返回 False", close_tab(s, "") is False)

    # --- activate_tab ---
    s = _FakeBrowser()
    add("activate_tab 成功", activate_tab(s, "1") is True)
    s = _FakeBrowser()
    s.fail_methods.add("Target.activateTarget")
    add("activate_tab 无头失败返回 False 不抛", activate_tab(s, "1") is False)
    add("activate_tab 空 id False", activate_tab(s, "") is False)

    # --- set_auto_attach / discover ---
    s = _FakeBrowser()
    add("set_auto_attach 成功", set_auto_attach(s) is True)
    p = [c for c in s.calls if c[0] == "Target.setAutoAttach"][0][1]
    add("set_auto_attach 默认 autoAttach True", p["autoAttach"] is True)
    add("set_auto_attach flatten True", p["flatten"] is True)
    add("set_auto_attach 默认不等调试器",
        p["waitForDebuggerOnStart"] is False)

    s = _FakeBrowser()
    set_auto_attach(s, filter_=[{"type": "page", "exclude": False}])
    p = [c for c in s.calls if c[0] == "Target.setAutoAttach"][0][1]
    add("set_auto_attach filter 透传",
        p["filter"] == [{"type": "page", "exclude": False}], p.get("filter"))
    s = _FakeBrowser()
    set_auto_attach(s, enable=False)
    p = [c for c in s.calls if c[0] == "Target.setAutoAttach"][0][1]
    add("set_auto_attach 关闭传 False", p["autoAttach"] is False)

    s = _FakeBrowser()
    s.fail_methods.add("Target.setAutoAttach")
    add("set_auto_attach 失败返回 False", set_auto_attach(s) is False)

    s = _FakeBrowser()
    add("set_discover_targets 成功", set_discover_targets(s) is True)
    add("set_discover_targets 传 discover",
        [c for c in s.calls if c[0] == "Target.setDiscoverTargets"][0][1] ==
        {"discover": True})

    # --- switch_tab ---
    s = _FakeBrowser()
    s.targets = [
        {"targetId": "1", "type": "page", "url": "https://a.com", "title": "A"},
        {"targetId": "2", "type": "page", "url": "https://b.com", "title": "B"},
    ]
    sw = switch_tab(s, url_contains="b.com")
    add("switch_tab 命中并带 session", sw is not None and sw.session_id == "S-2",
        sw)
    add("switch_tab 调了 activate",
        any(c[0] == "Target.activateTarget" and c[1].get("targetId") == "2"
            for c in s.calls))
    add("switch_tab 未命中返回 None", switch_tab(s, url_contains="zzz") is None)

    s = _FakeBrowser()
    s.targets = [{"targetId": "7", "type": "page", "url": "https://x.com",
                  "title": "X"}]
    sw = switch_tab(s, target_id="7", activate=False)
    add("switch_tab 按 target_id", sw.target_id == "7")
    add("switch_tab activate=False 不调 activate",
        not any(c[0] == "Target.activateTarget" for c in s.calls))

    # --- wait_for_tab ---
    s = _FakeBrowser()
    s.targets = [{"targetId": "1", "type": "page", "url": "https://old.com",
                  "title": "old"}]
    t0 = time.time()
    w = wait_for_tab(s, url_contains="new.com", timeout=0.35, interval=0.05)
    add("wait_for_tab 超时返回 None", w is None)
    add("wait_for_tab 超时后总时长 >= timeout", time.time() - t0 >= 0.3,
        round(time.time() - t0, 3))

    s = _FakeBrowser()
    s.targets = [
        {"targetId": "1", "type": "page", "url": "https://old.com", "title": ""},
        {"targetId": "9", "type": "page", "url": "https://new.com", "title": ""},
    ]
    w = wait_for_tab(s, url_contains="new.com", timeout=1.0)
    add("wait_for_tab 命中返回", w is not None and w.target_id == "9")
    w = wait_for_tab(s, url_contains="new.com", exclude_ids={"9"}, timeout=0.3)
    add("wait_for_tab exclude 排除已知", w is None)

    add("tab_ids 列出可见 id",
        sorted(tab_ids(s)) == ["1", "9"], sorted(tab_ids(s)))

    # --- 浏览器级会话选择 ---
    class _Pg(object):
        def __init__(self):
            self.browser_session = _FakeBrowser()

        def call(self, *a, **k):
            raise AssertionError("不该走页面会话")

    pg = _Pg()
    add("_browser_session 取到 browser_session",
        _browser_session(pg) is pg.browser_session)
    add("_browser_session 无则退回自己",
        _browser_session(s) is s)

    # --- 分页会话直接调 list_tabs ---
    bs = _FakeBrowser()
    bs.targets = [{"targetId": "1", "type": "page", "url": "u"}]
    pg2 = _Pg()
    pg2.browser_session = bs
    add("页面会话可代理列标签", len(list_tabs(pg2)) == 1)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "web_cdp.tab"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
