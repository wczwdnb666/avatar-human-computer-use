# -*- coding: utf-8 -*-
"""CDP 会话管理：连接、调用分发、事件订阅、超时、重连。

CDP（Chrome DevTools Protocol）是一个 **JSON-RPC 2.0 变体**，跑在
WebSocket 上。它的结构是：

::

    浏览器进程 (browser target)
      ├─ /json/version          HTTP 端点，报告 Browser 级 ws 地址
      ├─ /json/list             HTTP 端点，列出所有页面 target
      └─ ws://.../devtools/browser/<id>      ← browser 级连接
            │
            ├─ Target.attachToTarget {flatten: true} → sessionId
            │     │
            │     └─ 之后所有 Page./Runtime./DOM. 方法都带 sessionId，
            │        复用**同一条** WebSocket
            └─ Target.getTargets / Target.createTarget ...

两种连接方式：

1. **扁平模式（flatten）** —— 一条 WebSocket 连 browser 端点，
   用 ``Target.attachToTarget`` 拿 sessionId，后续所有页面的方法
   都带 ``sessionId`` 字段走这一条连接。**推荐**，因为只有一条连接。
2. **直连模式** —— 每个页面 target 有自己的 ``webSocketDebuggerUrl``，
   为每个页面开一条 WebSocket。简单但连接数多。

本模块两种都支持，但主推扁平模式。

## 消息形态

请求::

    {"id": 1, "method": "Page.navigate", "params": {...}, "sessionId": "..."}

响应（有 id 的是响应）::

    {"id": 1, "result": {...}, "sessionId": "..."}
    {"id": 1, "error": {"code": -32000, "message": "..."}}

事件（无 id）::

    {"method": "Page.loadEventFired", "params": {...}, "sessionId": "..."}

**关键约束（硬约束 #2）**：所有调用必须带 id，响应用 id 匹配；
事件用 method 匹配。混用会导致"把事件当响应返回"这种极难查的 bug。

## 线程模型

``CdpSession`` 不是线程安全的。它内部有一把 ``threading.Lock`` 保护
**发送**（因为帧必须整帧原子写入），但**接收**是单线程排他的 ——
调用 ``call()`` 时会一直读到自己的 id，期间收到的事件进队列。

如果调用方需要"一边等响应一边处理事件"，用 ``wait_event()`` ——
它是在同一个读循环里抽事件的（不会真并发读）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..errors import AvatarError, TimeoutError_
from .websocket import (OPCODE_TEXT, WebSocket, WebSocketClosed,
                        WebSocketError)

__all__ = [
    "CdpError",
    "CdpTimeout",
    "CdpClosed",
    "CdpSession",
    "CdpEvent",
    "BrowserSession",
    "open_session",
]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class CdpError(AvatarError):
    """CDP 调用返回了 error 对象。"""

    exit_code = 12

    def __init__(self, message: str, code: int = 0, method: str = "",
                 data: Any = None, **ctx):
        super().__init__(message, code=code or None,
                         method=method or None, **ctx)
        self.code = code
        self.method = method
        self.data = data


class CdpTimeout(TimeoutError_):
    """CDP 调用超时。"""

    exit_code = 6


class CdpClosed(AvatarError):
    """CDP 连接已断开。"""

    exit_code = 12


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
class CdpEvent:
    """一条 CDP 事件。"""

    __slots__ = ("method", "params", "session_id", "timestamp")

    def __init__(self, method: str, params: dict,
                 session_id: str = "") -> None:
        self.method = method
        self.params = params
        self.session_id = session_id
        self.timestamp = time.time()

    def __repr__(self) -> str:
        return "<CdpEvent %s%s>" % (
            self.method,
            " sid=%s" % self.session_id[:8] if self.session_id else "")

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "params": self.params,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
class CdpSession:
    """一条 CDP 连接。

    :param url: WebSocket URL。可以是 browser 端点，也可以是某个页面的
        ``webSocketDebuggerUrl``。
    :param timeout: 默认调用超时。
    :param auto_reconnect: 断线时是否自动重连（**只对**"连接是凭什么建的"
        这条信息做重连，不重放未完成的方法 —— 重放可能造成重复副作用）。
    """

    def __init__(self, url: str, *, timeout: float = 15.0,
                 auto_reconnect: bool = False) -> None:
        self.url = url
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect

        self._ws: Optional[WebSocket] = None
        self._lock = threading.RLock()

        self._next_id = 1
        #: id -> 响应（已收到但还没被 wait 取走的）
        self._pending: Dict[int, dict] = {}
        #: 事件队列（未被 wait_event 取走的）
        self._events: List[CdpEvent] = []
        #: method -> 订阅回调。有回调时事件仍会入队。
        self._listeners: Dict[str, List[Callable[[CdpEvent], None]]] = {}

        #: 已知的 sessionId → targetId 映射（扁平模式）
        self._sessions: Dict[str, str] = {}

        #: 统计
        self.calls = 0
        self.events_seen = 0
        self.errors_seen = 0

    # -- 生命周期 -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.connected

    def connect(self, timeout: Optional[float] = None) -> None:
        """建连接。幂等 —— 已连接时直接返回。"""
        if self.connected:
            return
        t = self.timeout if timeout is None else timeout
        ws = WebSocket(self.url)
        ws.connect(t)
        self._ws = ws

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self) -> "CdpSession":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 底层收发 -----------------------------------------------------------
    def _ensure(self) -> WebSocket:
        if self._ws is None or self._ws.closed:
            if self.auto_reconnect:
                self.close()
                self.connect()
            else:
                raise CdpClosed("CDP 连接已断开（auto_reconnect=False）",
                                url=self.url)
        return self._ws

    def _pump_once(self, timeout: float) -> None:
        """读一条消息并归位（响应进 _pending，事件进队列/派发）。"""
        ws = self._ensure()
        try:
            _, raw = ws.recv_message(timeout, msg_type=OPCODE_TEXT)
        except WebSocketClosed as exc:
            raise CdpClosed("CDP 连接被关闭：%s" % exc, url=self.url)
        except WebSocketError:
            raise
        except TimeoutError_:
            raise

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            msg = json.loads(text)
        except ValueError:
            # CDP 不该发非 JSON。若发生，记下来但不炸 —— 可能是分片 bug。
            self.errors_seen += 1
            return

        self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        """把一条已解析的消息归位。"""
        mid = msg.get("id")
        if mid is not None:
            # 响应
            self._pending[int(mid)] = msg
            return

        method = msg.get("method")
        if method is None:
            return

        # 事件
        self.events_seen += 1
        ev = CdpEvent(method, msg.get("params") or {},
                      msg.get("sessionId") or "")

        # 记录 sessionId ↔ targetId（Target.attachedToTarget 事件）
        if method == "Target.attachedToTarget":
            sid = ev.params.get("sessionId")
            tid = ((ev.params.get("targetInfo") or {}).get("targetId"))
            if sid and tid:
                self._sessions[sid] = tid

        callbacks = self._listeners.get(method)
        if callbacks:
            for cb in list(callbacks):
                try:
                    cb(ev)
                except Exception:
                    # 回调异常不能影响主循环
                    pass
        callbacks = self._listeners.get("*")
        if callbacks:
            for cb in list(callbacks):
                try:
                    cb(ev)
                except Exception:
                    pass

        self._events.append(ev)

    # -- 调用 ---------------------------------------------------------------
    def call(self, method: str, params: Optional[dict] = None, *,
             session_id: Optional[str] = None,
             timeout: Optional[float] = None) -> dict:
        """发一个 CDP 方法调用，等它的响应，返回 ``result``。

        抛 ``CdpError`` 当响应里带 ``error``；抛 ``CdpTimeout`` 当超时。
        """
        t = self.timeout if timeout is None else timeout
        with self._lock:
            ws = self._ensure()
            mid = self._next_id
            self._next_id += 1

            payload = {"id": mid, "method": method}
            if params is not None:
                payload["params"] = params
            if session_id:
                payload["sessionId"] = session_id

            data = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            ws.send_text(data.decode("utf-8"), timeout=t)
            self.calls += 1

            deadline = time.monotonic() + t
            while True:
                resp = self._pending.pop(mid, None)
                if resp is not None:
                    break
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise CdpTimeout("CDP 调用超时：%s" % method,
                                     elapsed=t, method=method)
                self._pump_once(remain)

        if "error" in resp:
            err = resp["error"] or {}
            self.errors_seen += 1
            raise CdpError(
                "CDP %s 失败：%s" % (method, err.get("message", "?")),
                code=int(err.get("code", 0) or 0),
                method=method, data=err.get("data"))

        return resp.get("result") or {}

    # -- 事件 ---------------------------------------------------------------
    def subscribe(self, method: str,
                  callback: Callable[[CdpEvent], None]) -> None:
        """订阅某个 method 的事件。``method="*"`` 订阅全部。

        回调在**读线程**里同步执行 —— 别在里面做耗时工作，
        否则会拖慢整个会话。
        """
        self._listeners.setdefault(method, []).append(callback)

    def unsubscribe(self, method: str,
                    callback: Optional[Callable] = None) -> None:
        """取消订阅。``callback=None`` 时清掉该 method 的全部订阅。"""
        if callback is None:
            self._listeners.pop(method, None)
            return
        lst = self._listeners.get(method)
        if not lst:
            return
        try:
            lst.remove(callback)
        except ValueError:
            pass
        if not lst:
            self._listeners.pop(method, None)

    def wait_event(self, method: str, *, timeout: Optional[float] = None,
                   predicate: Optional[Callable[[CdpEvent], bool]] = None,
                   session_id: Optional[str] = None) -> CdpEvent:
        """等一个指定 method 的事件。

        ``predicate`` 用来进一步过滤（比如等 URL 匹配某个 pattern）。
        超时抛 ``CdpTimeout``。

        ⚠️ 会**消耗**事件队列 —— 队列里已经有的匹配事件会被先用掉。
        """
        t = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + t

        # 先扫已有队列
        for i, ev in enumerate(self._events):
            if ev.method != method:
                continue
            if session_id and ev.session_id != session_id:
                continue
            if predicate and not predicate(ev):
                continue
            del self._events[i]
            return ev

        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise CdpTimeout("等待事件超时：%s" % method,
                                 elapsed=t, method=method)
            before = len(self._events)
            self._pump_once(remain)
            # 新到的事件在尾部
            for i in range(before, len(self._events)):
                ev = self._events[i]
                if ev.method != method:
                    continue
                if session_id and ev.session_id != session_id:
                    continue
                if predicate and not predicate(ev):
                    continue
                del self._events[i]
                return ev

    def drain_events(self, method: Optional[str] = None) -> List[CdpEvent]:
        """取走队列里的事件（可过滤 method）。"""
        if method is None:
            out = self._events
            self._events = []
            return out
        keep, out = [], []
        for ev in self._events:
            (out if ev.method == method else keep).append(ev)
        self._events = keep
        return out

    # -- 便利方法 -----------------------------------------------------------
    def enable(self, *domains: str, session_id: Optional[str] = None) -> Dict[str, dict]:
        """批量 enable 若干域，返回 ``{domain: result}``。

        enable 失败**不中断** —— 有些域在特定 target 上不存在
        （比如 browser target 上没有 Page 域），那不算错误。
        """
        out = {}
        for d in domains:
            try:
                out[d] = self.call("%s.enable" % d, session_id=session_id)
            except CdpError as exc:
                out[d] = {"_error": str(exc)}
        return out

    def evaluate(self, expression: str, *, session_id: Optional[str] = None,
                 await_promise: bool = True,
                 return_by_value: bool = True,
                 timeout: Optional[float] = None) -> Any:
        """``Runtime.evaluate`` 的便捷封装，直接返回值本体。

        ``returnByValue=True`` 时 CDP 会把 JS 值序列化进 ``result.value``。
        但 ``undefined`` 没有 value 字段 —— 那时返回 None。
        """
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": return_by_value,
            "userGesture": True,
        }, session_id=session_id, timeout=timeout)

        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text")
            raise CdpError("JS 执行抛异常：%s" % desc,
                           method="Runtime.evaluate", data=det)

        remote = res.get("result") or {}
        if "value" in remote:
            return remote["value"]
        # undefined / 不可序列化
        return None

    def session_for_target(self, target_id: str,
                           flatten: bool = True) -> str:
        """为某个 target 附加一个 session，返回 sessionId。"""
        params: dict = {"targetId": target_id}
        if flatten:
            params["flatten"] = True
        res = self.call("Target.attachToTarget", params)
        sid = res.get("sessionId") or ""
        if sid:
            self._sessions[sid] = target_id
        return sid

    # -- 统计 ---------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "url": self.url,
            "connected": self.connected,
            "calls": self.calls,
            "events_seen": self.events_seen,
            "errors_seen": self.errors_seen,
            "queued_events": len(self._events),
            "sessions": len(self._sessions),
            "auto_reconnect": self.auto_reconnect,
        }

    def __repr__(self) -> str:
        return "<CdpSession %s calls=%d %s>" % (
            self.url, self.calls, "connected" if self.connected else "closed")


# --------------------------------------------------------------------------
# Browser 级会话（连 browser 端点）
# --------------------------------------------------------------------------
class BrowserSession(CdpSession):
    """连到 browser 端点的会话，额外提供 target 管理。

    browser 端点的方法（``Target.*``、``Browser.*``）**不带 sessionId**；
    页面级方法（``Page.*``、``Runtime.*``）必须带。
    """

    def list_targets(self, *, filter_type: Optional[str] = None) -> List[dict]:
        """列所有 target。``filter_type="page"`` 只要页面。"""
        res = self.call("Target.getTargets")
        infos = res.get("targetInfos") or []
        if filter_type is None:
            return infos
        return [t for t in infos if t.get("type") == filter_type]

    def find_target(self, *, url_contains: Optional[str] = None,
                    type_: str = "page",
                    title_contains: Optional[str] = None) -> Optional[dict]:
        """按条件找一个 target，找不到返回 None。"""
        for t in self.list_targets(filter_type=type_):
            if url_contains and url_contains not in (t.get("url") or ""):
                continue
            if title_contains and title_contains not in (t.get("title") or ""):
                continue
            return t
        return None

    def attach_page(self, target_id: str) -> str:
        """附加到页面 target，返回 sessionId。

        附加后会自动 ``Page.enable`` + ``Runtime.enable`` ——
        不 enable 的话收不到 ``Page.loadEventFired`` 这类事件，
        会表现为"navigate 之后一直等不到加载完成"。
        """
        sid = self.session_for_target(target_id, flatten=True)
        self.enable("Page", "Runtime", "DOM", session_id=sid)
        return sid


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def open_session(url: str, *, timeout: float = 15.0,
                 reconnect: bool = False) -> CdpSession:
    """建并连接一个会话。URL 里含 ``/devtools/browser/`` 时返回
    ``BrowserSession``，否则返回普通 ``CdpSession``。"""
    cls = BrowserSession if "/devtools/browser/" in url else CdpSession
    s = cls(url, timeout=timeout, auto_reconnect=reconnect)
    s.connect(timeout)
    return s


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """会话层自检（**不联网**）。

    用喂给 ``_dispatch`` 的假消息验证：id 匹配、事件归位、
    事件队列消耗、URL 后缀判断。真实连接由
    ``tests/test_phase4_cdp.py`` 覆盖。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 类选择 -------------------------------------------------------------
    _add("browser 端点选 BrowserSession",
         ("/devtools/browser/" in "ws://x/devtools/browser/AAA"),
         "URL 判定")
    _add("page 端点不走 BrowserSession",
         ("/devtools/browser/" not in "ws://x/devtools/page/BBB"),
         "URL 判定")

    # -- 消息归位：响应 -----------------------------------------------------
    s = CdpSession("ws://127.0.0.1:1/devtools/page/x")
    s._dispatch({"id": 7, "result": {"ok": True}})
    _add("响应按 id 入 pending", 7 in s._pending, "pending=%s" % list(s._pending))

    # -- 消息归位：事件 -----------------------------------------------------
    s2 = CdpSession("ws://127.0.0.1:1/devtools/page/x")
    s2._dispatch({"method": "Page.loadEventFired",
                  "params": {"timestamp": 1.5}})
    _add("事件不入 pending", not s2._pending, "pending=%d" % len(s2._pending))
    _add("事件入队列", len(s2._events) == 1 and s2.events_seen == 1,
         "队列 %d / 累计 %d" % (len(s2._events), s2.events_seen))

    # -- 事件订阅 -----------------------------------------------------------
    seen = []
    s2.subscribe("Page.loadEventFired", lambda e: seen.append(e.method))
    s2._dispatch({"method": "Page.loadEventFired", "params": {}})
    _add("订阅回调被调用", seen == ["Page.loadEventFired"], "%s" % seen)

    star = []
    s2.subscribe("*", lambda e: star.append(e.method))
    s2._dispatch({"method": "Network.requestWillBeSent", "params": {}})
    _add("通配订阅收到所有事件", "Network.requestWillBeSent" in star, "%s" % star)

    s2.unsubscribe("Page.loadEventFired")
    n_before = len(seen)
    s2._dispatch({"method": "Page.loadEventFired", "params": {}})
    _add("退订后不再回调", len(seen) == n_before, "%d 次" % len(seen))

    # -- sessionId 记录 -----------------------------------------------------
    s3 = CdpSession("ws://127.0.0.1:1/devtools/browser/x")
    s3._dispatch({"method": "Target.attachedToTarget",
                  "params": {"sessionId": "SID1",
                             "targetInfo": {"targetId": "TID1"}}})
    _add("attachedToTarget 记进映射",
         s3._sessions.get("SID1") == "TID1", "%s" % s3._sessions)

    # -- 事件队列消耗 -------------------------------------------------------
    s4 = CdpSession("ws://127.0.0.1:1/devtools/page/x")
    for i in range(3):
        s4._dispatch({"method": "X.tick", "params": {"i": i}})
    got = s4.drain_events("X.tick")
    _add("drain_events 取走全部", len(got) == 3 and not s4._events,
         "%d 条，剩 %d" % (len(got), len(s4._events)))

    # -- 过滤式 drain 保留其他 ----------------------------------------------
    s5 = CdpSession("ws://127.0.0.1:1/devtools/page/x")
    s5._dispatch({"method": "A.one", "params": {}})
    s5._dispatch({"method": "B.two", "params": {}})
    got = s5.drain_events("A.one")
    _add("drain 过滤保留其他",
         len(got) == 1 and len(s5._events) == 1
         and s5._events[0].method == "B.two",
         "取 %d 留 %d" % (len(got), len(s5._events)))

    # -- 非 JSON 不炸 -------------------------------------------------------
    s6 = CdpSession("ws://127.0.0.1:1/devtools/page/x")
    s6._dispatch({"id": 1})       # 缺 result 也算响应
    _add("缺 result 的响应仍入 pending", 1 in s6._pending, "pending")

    # -- describe -----------------------------------------------------------
    d = s2.describe()
    need = {"url", "connected", "calls", "events_seen", "errors_seen",
            "queued_events", "sessions", "auto_reconnect"}
    _add("describe 字段完整", need <= set(d),
         "缺 %s" % sorted(need - set(d)) if need - set(d) else "8 个字段")

    # -- 未连接时 call 抛错而不是崩 ----------------------------------------
    try:
        s7 = CdpSession("ws://127.0.0.1:1/devtools/page/x")
        s7.call("Page.enable", timeout=0.1)
        _add("未连接时调用抛 CdpClosed", False, "居然没抛")
    except CdpClosed:
        _add("未连接时调用抛 CdpClosed", True, "CdpClosed")
    except Exception as exc:      # noqa: BLE001
        _add("未连接时调用抛 CdpClosed", False,
             "抛了 %s" % type(exc).__name__)

    return {"checks": checks, "failures": failures, "ok": not failures}
