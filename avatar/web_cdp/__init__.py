# -*- coding: utf-8 -*-
"""Web / Electron 控制层（纯 ctypes + 标准库手搓 CDP）。

**为什么手搓 CDP**：本项目坚持零第三方依赖。WebSocket 用标准库 socket
自己实现（RFC 6455），CDP 的 HTTP 端点用 ``http.client``，
JSON-RPC 用 ``json``。禁用 ``selenium`` / ``playwright`` /
``websocket-client`` / ``websockets`` —— 见 `docs/web_cdp.md`。

分层::

    websocket.py   帧协议（握手 / 掩码 / 分片）—— 最底层，最容易错
    session.py     JSON-RPC 会话（id 匹配 / 事件订阅 / 会话扁平化）
    launcher.py    启动浏览器 + 调试端口探测（重点：不要连错浏览器）
    navigate.py    导航（Page.navigate + loadEventFired 等待）
    dom.py         DOM 查询（Runtime.evaluate + Node 查询）
    interact.py    点击 / 输入 / 悬停 / 拖拽 / 上传
    network.py     请求监听（打榜取证用）
    screenshot.py  截图（全页 / 元素 / 区域）
    tab.py         标签页管理
    electron.py    Electron 应用适配（端口扫描 + 附加）
    fallback.py    CDP 不可用时降级到 UIA / 视觉

典型用法::

    from avatar.web_cdp import launcher as L, session as S

    info, proc = L.ensure_debug_browser(prefer="edge", headless=True)
    sess = S.open_session(info.websocket_url)
    page = sess.list_targets(filter_type="page")[0]
    sid = sess.attach_page(page["targetId"])
    sess.call("Page.navigate", {"url": "https://example.com"}, session_id=sid)
    print(sess.evaluate("document.title", session_id=sid))
"""

from __future__ import annotations

# 帧层
from .websocket import (OPCODE_BINARY, OPCODE_CLOSE, OPCODE_CONTINUATION,
                        OPCODE_PING, OPCODE_PONG, OPCODE_TEXT, WS_GUID,
                        FrameDecoder, HandshakeError, WebSocket,
                        WebSocketClosed, WebSocketError, encode_frame,
                        expected_accept, make_key, ws_url_to_host_port)

# 会话层
from .session import (BrowserSession, CdpClosed, CdpError, CdpEvent,
                      CdpSession, CdpTimeout, open_session)

# 启动层
from .launcher import (DEFAULT_DEBUG_PORT, DEBUG_PORT_RANGE, BrowserInfo,
                       BrowserLaunchError, close_browser, ensure_debug_browser,
                       find_browser, find_chrome, find_edge, http_json,
                       is_port_free, launch_browser, list_debug_instances,
                       pick_free_port, probe_debug_port, wait_for_debug_port)

__all__ = [
    # websocket
    "WebSocket", "FrameDecoder", "WebSocketError", "HandshakeError",
    "WebSocketClosed", "encode_frame", "make_key", "expected_accept",
    "ws_url_to_host_port", "WS_GUID",
    "OPCODE_CONTINUATION", "OPCODE_TEXT", "OPCODE_BINARY",
    "OPCODE_CLOSE", "OPCODE_PING", "OPCODE_PONG",
    # session
    "CdpSession", "BrowserSession", "CdpEvent", "CdpError", "CdpTimeout",
    "CdpClosed", "open_session",
    # launcher
    "BrowserInfo", "BrowserLaunchError", "find_browser", "find_chrome",
    "find_edge", "probe_debug_port", "is_port_free", "pick_free_port",
    "list_debug_instances", "launch_browser", "ensure_debug_browser",
    "wait_for_debug_port", "close_browser", "http_json",
    "DEFAULT_DEBUG_PORT", "DEBUG_PORT_RANGE",
    # 能力层（延迟导入，避免与 session 循环 import）
    "navigate", "dom", "interact", "network", "screenshot", "tab",
    "electron", "fallback",
]


def __getattr__(name: str):
    """延迟导入能力层模块。

    能力层模块都 ``from . import session``，而本 ``__init__`` 又要导出
    它们 —— 顶层直接 import 会循环。用 ``__getattr__`` 做成懒加载：
    ``import avatar.web_cdp`` 时不加载，访问 ``web_cdp.dom`` 时才加载。
    """
    if name in ("navigate", "dom", "interact", "network", "screenshot",
                "tab", "electron", "fallback"):
        import importlib
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


#: 可用性探测：Chrome/Edge 在不在、有没有可复用的调试实例。
def available() -> dict:
    """报告 Web 控制能力当前可用性。

    返回 ``{"ok": bool, "browser": ..., "kind": ..., "instances": [...],
    "reason": str}``。**永不抛异常** —— 可用性探测本身不该失败，
    它存在的意义就是让上层据此决定走不走降级链。
    """
    try:
        exe = find_browser("any")
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "browser": None, "kind": "unknown",
                "instances": [], "reason": "探测可执行文件失败：%s" % exc}

    instances = []
    try:
        instances = [i.describe() for i in list_debug_instances()]
    except Exception:
        pass

    if instances:
        return {"ok": True, "browser": instances[0]["browser"],
                "kind": instances[0]["kind"], "instances": instances,
                "reason": "已有调试实例 %d 个，可直接复用" % len(instances)}
    if exe:
        return {"ok": True, "browser": exe, "kind": "installed",
                "instances": [],
                "reason": "本机已安装浏览器，可按需启动带调试端口的新实例"}
    return {"ok": False, "browser": None, "kind": "unknown", "instances": [],
            "reason": "本机未安装 Chrome / Edge，且没有可复用的调试实例"}


def selfcheck() -> dict:
    """web_cdp 包级自检：汇总子模块 + 报告可用性。"""
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    import importlib
    _SUBS = ("websocket", "session", "launcher",
             "navigate", "dom", "interact", "network", "screenshot", "tab",
             "fallback", "electron")
    for mod_name in _SUBS:
        mod = globals().get(mod_name)
        if mod is None or not hasattr(mod, "selfcheck"):
            try:
                mod = importlib.import_module("." + mod_name, __name__)
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
             "%d/%d 通过" % (total - bad, total))

    avail = available()
    _add("可用性探测不抛异常", "ok" in avail and "reason" in avail,
         avail.get("reason", "")[:56])

    return {"checks": checks, "failures": failures, "ok": not failures}
