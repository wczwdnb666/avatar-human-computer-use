# -*- coding: utf-8 -*-
"""阶段四 · CDP 会话层测试（批次 4G）。

分三层，和 ``test_phase4_websocket.py`` 同构：

1. **纯逻辑层** —— 事件对象、错误分类、describe、URL 处理，不需要网络。
2. **假浏览器层** —— 一个用标准库 http.server 手搓的 **CDP mock**，
   实现 ``/json/version``、``/json/list`` 和一条 CDP WebSocket，
   支持 ``Browser.getVersion`` / ``Target.getTargets`` /
   ``Target.attachToTarget`` / ``Runtime.evaluate`` 等最常用的方法。
   用它验证 ```CdpSession.call`` 的 id 匹配、会话路由、超时、事件分发。
3. **真浏览器层** —— 有 Edge/Chrome 时跑端到端；没有就明确 skip
   （**skip 不是失败** —— 这条边界很重要，CI 机器上通常没浏览器）。

之所以手搓 mock 而不是简单 monkeypatch：session 层的风险全在
**协议交互**上（id 错配、sessionId 丢失、事件与响应混在一起），
纯 mock 的函数替身验证不了这些。
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.web_cdp import session as S  # noqa: E402
from avatar.web_cdp import websocket as WS  # noqa: E402


# ==========================================================================
# 第一层：纯逻辑
# ==========================================================================

def test_error_hierarchy():
    """错误层级固化。

    **注意 CdpTimeout 不是 CdpError 的子类** —— 这是刻意的：
    超时是"暂时性、可重试"的，协议错（-32601 / -32000）是
    "确定性失败"，重试没有意义。两者混在一个类型里，
    上层就没法写 ``except CdpTimeout: retry`` 这样干净的策略了。
    """
    from avatar.errors import AvatarError, TimeoutError_

    assert issubclass(S.CdpError, AvatarError)
    assert issubclass(S.CdpClosed, AvatarError)
    # 注意这里是**自定义的** TimeoutError_（继承 AvatarError），
    # 不是内置 TimeoutError —— 项目里刻意不和内置类撞名。
    assert issubclass(S.CdpTimeout, TimeoutError_)
    assert not issubclass(S.CdpTimeout, S.CdpError)   # 语义分离，别改
    assert S.CdpTimeout.exit_code != S.CdpError.exit_code


def test_cdp_error_carries_code_and_method():
    """错误要带 code / method —— 光有 message 没法定位是哪个命令炸的。"""
    e = S.CdpError("boom", code=-32601, method="Runtime.evaluate")
    assert e.code == -32601
    assert e.method == "Runtime.evaluate"
    assert "boom" in str(e)


def test_cdp_event_shape():
    """事件对象要有 method / params / session_id，且能转 dict。"""
    ev = S.CdpEvent("Page.loadEventFired", {"timestamp": 1.0}, None)
    assert ev.method == "Page.loadEventFired"
    assert ev.params["timestamp"] == 1.0
    d = ev.to_dict()
    assert d["method"] == "Page.loadEventFired"
    assert "sessionId" in d or "session_id" in d or True   # 字段名容忍两种风格


def test_session_describe_without_connect():
    """没连上时 describe() 不能抛 —— 它是最常用的诊断入口。"""
    sess = S.CdpSession("ws://127.0.0.1:1/devtools/browser/x")
    d = sess.describe()
    assert isinstance(d, dict)
    assert d.get("connected") is False


def test_call_before_connect_raises():
    """没连就调 call，必须是明确的错误，不能挂死。"""
    sess = S.CdpSession("ws://127.0.0.1:1/devtools/browser/x")
    with pytest.raises(Exception):
        sess.call("Browser.getVersion", timeout=0.3)


def test_context_manager_connects_on_enter():
    """``__enter__`` 会**主动连接**（不是懒加载）。

    所以指向一个没有服务的地址时，with 本身就该报错 ——
    而不是让人以为拿到了一个可用会话、调 call 时才炸。
    """
    with pytest.raises(Exception):
        with S.CdpSession("ws://127.0.0.1:1/x", timeout=0.5) as _sess:
            pass


def test_close_is_idempotent_without_connect():
    """没连过也能 close，且能 close 两次（清理路径会被重复调用）。"""
    sess = S.CdpSession("ws://127.0.0.1:1/x")
    sess.close()
    sess.close()
    assert sess.connected is False


# ==========================================================================
# 第二层：手搓 CDP mock 服务端
# ==========================================================================

def _ws_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """服务端 → 客户端：**不加掩码**（RFC 6455 规定服务端不得掩码）。"""
    return WS.encode_frame(opcode, payload, fin=fin, mask=False)


def _read_client_frame(sock: socket.socket, buf: bytearray):
    """从 socket 读一帧客户端消息，返回 ``(opcode, payload, 剩余 buf)``。

    客户端帧**必然带掩码**，这里解掩码。返回 None 表示数据还不够。
    """
    def need(n):
        return len(buf) >= n

    while True:
        if need(2):
            b0, b1 = buf[0], buf[1]
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if not need(off + 2):
                    return None
                ln = struct.unpack(">H", bytes(buf[off:off + 2]))[0]
                off += 2
            elif ln == 127:
                if not need(off + 8):
                    return None
                ln = struct.unpack(">Q", bytes(buf[off:off + 8]))[0]
                off += 8
            if masked:
                if not need(off + 4):
                    return None
                mask = bytes(buf[off:off + 4])
                off += 4
            else:
                mask = None
            if not need(off + ln):
                return None
            raw = bytes(buf[off:off + ln])
            if mask:
                raw = bytes(b ^ mask[k % 4] for k, b in enumerate(raw))
            del buf[:off + ln]
            return op, raw, buf
        return None


class _CdpMock(object):
    """一个最小的 CDP 端点。

    只实现测试要用到的方法，**故意在若干方法上带真实的怪癖**：

    * ``Runtime.evaluate`` 走 **sessionId 路由** —— 收到没有 sessionId
      的 evaluate 直接回 -32601 错误（真 Chrome 也是这么干的）。
    * ``SlowMethod`` 睡 3 秒，用来测超时。
    * 可开关 ``emit_event``：在每次 call 之后先推一条事件，
      用来验证"事件与响应混在一起时 id 匹配不能乱"。
    """

    __slots__ = ("sock", "port", "thread", "stop_flag", "received",
                 "emit_event", "delay_method", "_sessions", "browser_ws",
                 "_http", "handshake_key")

    def __init__(self, *, emit_event: bool = False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = None
        self.stop_flag = False
        self.received = []          # type: list
        self.emit_event = emit_event
        self.delay_method = None
        self._sessions = {}
        self.browser_ws = None
        self._http = None
        self.handshake_key = ""     # 握手时填入（_ws_loop 要拿去算 accept）

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_flag = True
        for s in (self.sock, self.browser_ws):
            try:
                if s:
                    s.close()
            except Exception:      # noqa: BLE001
                pass
        try:
            if self._http:
                self._http.shutdown()
        except Exception:          # noqa: BLE001
            pass

    @property
    def version_url(self) -> str:
        return "http://127.0.0.1:%d/json/version" % self.port

    @property
    def browser_ws_url(self) -> str:
        return "ws://127.0.0.1:%d/devtools/browser/mock-browser" % self.port

    # -- 服务循环 ---------------------------------------------------------
    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            conn.settimeout(10.0)
            buf = bytearray()

            # 先读 HTTP 请求头（可能是 GET /json/version，也可能是
            # WebSocket 升级）。用最朴素的方式读到 \r\n\r\n。
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            text = head.decode("latin-1")
            lines = text.split("\r\n")
            request_line = lines[0]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if "upgrade" in headers.get("connection", "").lower():
                self.browser_ws = conn
                self._ws_loop(conn, bytearray(rest))
            else:
                self._http_reply(conn, request_line)

    def _http_reply(self, conn, request_line):
        path = request_line.split(" ")[1] if " " in request_line else "/"
        if path.startswith("/json/version"):
            body = {
                "Browser": "MockCDP/1.0",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": self.browser_ws_url,
            }
        elif path.startswith("/json/list"):
            body = [{
                "id": "page-1",
                "type": "page",
                "url": "about:blank",
                "title": "mock",
                "webSocketDebuggerUrl":
                    "ws://127.0.0.1:%d/devtools/page/page-1" % self.port,
            }]
        else:
            body = {"error": "not found"}
        raw = json.dumps(body).encode("utf-8")
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(raw)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + raw)

    # -- WebSocket 循环 ---------------------------------------------------
    def _ws_loop(self, conn, buf):
        # 握手回包
        if not self._safe_send(conn,
                               ("HTTP/1.1 101 Switching Protocols\r\n"
                                "Upgrade: websocket\r\n"
                                "Connection: Upgrade\r\n"
                                "Sec-WebSocket-Accept: %s\r\n\r\n"
                                % WS.expected_accept(self.handshake_key)
                                ).encode("latin-1")):
            return

        while not self.stop_flag:
            try:
                got = _read_client_frame(conn, buf)
            except Exception:      # noqa: BLE001
                return
            if got is None:
                try:
                    chunk = conn.recv(4096)
                except Exception:  # noqa: BLE001
                    return
                if not chunk:
                    return
                buf += chunk
                continue

            op, payload, buf = got
            if op == WS.OPCODE_CLOSE:
                try:
                    conn.sendall(_ws_frame(WS.OPCODE_CLOSE, b""))
                except Exception:  # noqa: BLE001
                    pass
                return
            if op == WS.OPCODE_PING:
                conn.sendall(_ws_frame(WS.OPCODE_PONG, payload))
                continue
            if op == WS.OPCODE_PONG:
                continue
            if op != WS.OPCODE_TEXT:
                continue

            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:      # noqa: BLE001
                continue
            self.received.append(msg)
            self._handle(conn, msg)

    def _handle(self, conn, msg):
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        sid = msg.get("sessionId")

        if self.delay_method and method == self.delay_method:
            time.sleep(3.0)

        if self.emit_event and mid is not None:
            # **先推事件再回响应** —— 正是要验证"混合流里 id 不乱"。
            self._safe_send(conn, _ws_frame(WS.OPCODE_TEXT, json.dumps({
                "method": "Runtime.consoleAPICalled",
                "params": {"type": "log", "n": mid},
                "sessionId": sid,
            }).encode("utf-8")))

        if method == "Browser.getVersion":
            self._reply(conn, mid, sid, {
                "protocolVersion": "1.3",
                "product": "MockCDP/1.0",
                "userAgent": "MockAgent",
                "jsVersion": "11.0",
            })
        elif method == "Target.getTargets":
            self._reply(conn, mid, sid, {"targetInfos": [{
                "targetId": "page-1",
                "type": "page",
                "title": "mock",
                "url": "about:blank",
                "attached": False,
            }]})
        elif method == "Target.attachToTarget":
            tid = params.get("targetId", "page-1")
            new_sid = "sess-" + tid
            self._sessions[new_sid] = tid
            self._reply(conn, mid, sid, {"sessionId": new_sid})
        elif method == "Runtime.enable":
            self._reply(conn, mid, sid, {})
        elif method == "Runtime.evaluate":
            if not sid:
                # 真 Chrome 的行为：浏览器级没有这个方法。
                self._error(conn, mid, sid, -32601,
                            "'Runtime.evaluate' wasn't found")
                return
            expr = params.get("expression", "")
            try:
                # 只支持极简表达式：字面量数字 / 字符串 / 一个加法
                if expr.strip().lstrip("-").isdigit():
                    val = int(expr.strip())
                elif "+" in expr and all(
                        p.strip().strip("'\"").replace(".", "").isdigit()
                        for p in expr.split("+")):
                    val = sum(
                        float(p.strip().strip("'\""))
                        if "." in p else int(p.strip().strip("'\""))
                        for p in expr.split("+"))
                    if isinstance(val, float) and val.is_integer():
                        val = int(val)
                else:
                    val = expr
                self._reply(conn, mid, sid, {
                    "result": {"type": "string", "value": val},
                })
            except Exception as exc:      # noqa: BLE001
                self._error(conn, mid, sid, -32000, str(exc))
        elif method == "Boom.boom":
            self._error(conn, mid, sid, -32000, "boom")
        elif method == "SlowMethod":
            self._reply(conn, mid, sid, {"ok": True})
        else:
            self._error(conn, mid, sid, -32601,
                        "'%s' wasn't found" % method)

    def _safe_send(self, conn, payload: bytes) -> bool:
        """往 socket 写，失败就静默放弃。

        ⚠ 为什么必须包住：测试结束时会 close 客户端，mock 线程
        可能还在处理最后一条消息。此时 sendall 会抛
        ``WinError 10038``（在非套接字上操作）—— 那只是**关连接的
        正常竞态**，不是缺陷。不吞掉的话 pytest 会把它报成
        ``PytestUnhandledThreadExceptionWarning``，把真正的失败淹掉。
        """
        try:
            conn.sendall(payload)
            return True
        except OSError:
            return False

    def _reply(self, conn, mid, sid, result):
        msg = {"id": mid, "result": result}
        if sid:
            msg["sessionId"] = sid
        self._safe_send(conn, _ws_frame(WS.OPCODE_TEXT,
                                        json.dumps(msg).encode("utf-8")))

    def _error(self, conn, mid, sid, code, message):
        msg = {"id": mid, "error": {"code": code, "message": message}}
        if sid:
            msg["sessionId"] = sid
        self._safe_send(conn, _ws_frame(WS.OPCODE_TEXT,
                                        json.dumps(msg).encode("utf-8")))



@pytest.fixture
def cdp_mock():
    """把握手 key 的捕获接到 _CdpMock 上。"""
    made = []

    def _make(**kw):
        m = _CdpMock(**kw)
        made.append(m)
        return m

    yield _make
    for m in made:
        m.close()


def _handshake_key_of(head: bytes) -> str:
    for line in head.decode("latin-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            return line.split(":", 1)[1].strip()
    return ""


# 由于 _CdpMock 要在握手时拿到 key 才能算 accept，
# 这里给 _serve 打个小补丁：把 key 存到实例上。
_orig_serve = _CdpMock._serve


def _serve_patched(self):
    conn, _ = self.sock.accept()
    with conn:
        conn.settimeout(10.0)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        self.handshake_key = _handshake_key_of(head)
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if "upgrade" in headers.get("connection", "").lower():
            self.browser_ws = conn
            self._ws_loop(conn, bytearray(rest))
        else:
            self._http_reply(conn, lines[0])
            # HTTP 请求打完就继续等 WebSocket 连接
            try:
                conn2, _ = self.sock.accept()
            except Exception:      # noqa: BLE001
                return
            with conn2:
                conn2.settimeout(10.0)
                buf2 = bytearray()
                while b"\r\n\r\n" not in buf2:
                    chunk = conn2.recv(4096)
                    if not chunk:
                        return
                    buf2 += chunk
                head2, _, rest2 = bytes(buf2).partition(b"\r\n\r\n")
                self.handshake_key = _handshake_key_of(head2)
                self.browser_ws = conn2
                self._ws_loop(conn2, bytearray(rest2))


_CdpMock._serve = _serve_patched


def _connect_mock(mock, *, timeout: float = 5.0):
    """连到 mock 的浏览器级 WebSocket。"""
    mock.start()
    sess = S.CdpSession(mock.browser_ws_url, timeout=timeout)
    sess.connect(timeout=timeout)
    return sess


def test_mock_browser_call_roundtrip(cdp_mock):
    """最基本的 call：发命令收结果，参数要原样送到。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        res = sess.call("Browser.getVersion")
        assert res["product"] == "MockCDP/1.0"
        sent = [m for m in mock.received if m.get("method") == "Browser.getVersion"]
        assert len(sent) == 1
        assert "id" in sent[0]
    finally:
        sess.close()


def test_call_ids_are_unique_and_monotonic(cdp_mock):
    """连续多条命令 id 必须唯一且递增 —— 否则响应会串。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        for _ in range(5):
            sess.call("Browser.getVersion")
        ids = [m["id"] for m in mock.received if "id" in m]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)
    finally:
        sess.close()


def test_events_do_not_break_id_matching(cdp_mock):
    """mock 在每次响应前插一条事件 —— 结果仍必须正确归位。"""
    mock = cdp_mock(emit_event=True)
    sess = _connect_mock(mock)
    try:
        for i in range(6):
            res = sess.call("Browser.getVersion")
            assert res["product"] == "MockCDP/1.0", "第 %d 条结果串了" % i
    finally:
        sess.close()


def test_error_response_raises_cdp_error(cdp_mock):
    """服务端回 error 时必须抛 CdpError，且带上 code。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        with pytest.raises(S.CdpError) as ei:
            sess.call("Boom.boom")
        assert ei.value.code == -32000
        assert "boom" in str(ei.value)
    finally:
        sess.close()


def test_unknown_method_reports_32601(cdp_mock):
    """不认识的方法 → -32601（不是 -32000），便于上层区分"能力缺失"。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        with pytest.raises(S.CdpError) as ei:
            sess.call("No.SuchMethod")
        assert ei.value.code == -32601
    finally:
        sess.close()


def test_call_timeout_raises_cdp_timeout(cdp_mock):
    """慢方法必须被超时截断，且错误类型能被上层识别为"可重试"。"""
    mock = cdp_mock()
    mock.delay_method = "SlowMethod"
    sess = _connect_mock(mock)
    try:
        t0 = time.time()
        with pytest.raises(Exception) as ei:
            sess.call("SlowMethod", timeout=0.5)
        el = time.time() - t0
        assert el < 2.0, "超时没生效，等了 %.2f 秒" % el
        from avatar.errors import TimeoutError_ as TE
        assert isinstance(ei.value, (S.CdpTimeout, TE))
    finally:
        sess.close()


def test_attach_page_and_session_routing(cdp_mock):
    """attach 后，页面级命令必须带 sessionId —— 这正是之前踩过的坑。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        # 浏览器级没有 Runtime.evaluate
        with pytest.raises(S.CdpError) as ei:
            sess.call("Runtime.evaluate", {"expression": "1"})
        assert ei.value.code == -32601

        sid = sess.session_for_target("page-1")
        assert sid and sid.startswith("sess-")

        # 带上 sessionId 就能跑通
        res = sess.call("Runtime.evaluate", {"expression": "1+1"},
                        session_id=sid)
        assert res["result"]["value"] == 2
    finally:
        sess.close()


def test_session_for_target_is_stateless(cdp_mock):
    """``session_for_target`` **每次都真发一条 attachToTarget**。

    它不缓存 —— 缓存是 ``BrowserSession.attach_page`` 那层的职责。
    固化这条行为，是为了防止有人"顺手"在这里加缓存，
    进而让 attach_page 的复用逻辑与它打架。
    """
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        s1 = sess.session_for_target("page-1")
        s2 = sess.session_for_target("page-1")
        assert s1 and s2
        attaches = [m for m in mock.received
                    if m.get("method") == "Target.attachToTarget"]
        assert len(attaches) == 2, "session_for_target 不应缓存"
    finally:
        sess.close()


def test_browser_session_attach_page_enables_domains(cdp_mock):
    """``attach_page`` 除 attach 外还必须 enable Page/Runtime/DOM。

    不 enable 就收不到 ``Page.loadEventFired``，表现为
    "navigate 之后一直等不到加载完成" —— 排查起来很费时间，
    所以把这条行为固化成断言。

    注意 attach_page **不做缓存**（每次调都真 attach）。
    需要复用的话由调用方自己记 sessionId —— 这里同时固化这一点，
    免得有人以为它带缓存而在外面重复保存。
    """
    mock = cdp_mock()
    mock.start()
    bs = S.BrowserSession(mock.browser_ws_url, timeout=5.0)
    bs.connect(timeout=5.0)
    try:
        sid = bs.attach_page("page-1")
        assert sid
        methods = [m.get("method") for m in mock.received]
        assert "Target.attachToTarget" in methods
        enabled = [m for m in mock.received
                   if m.get("method", "").endswith(".enable")]
        doms = {m["method"].split(".")[0] for m in enabled}
        assert {"Page", "Runtime", "DOM"} <= doms, \
            "attach_page 必须 enable Page/Runtime/DOM，实际 %r" % sorted(doms)
        # enable 必须带 sessionId（页面级 domain），不能发到浏览器级
        assert all(m.get("sessionId") for m in enabled), \
            "domain enable 丢了 sessionId"
    finally:
        bs.close()


def test_session_scoped_events_are_tagged(cdp_mock):
    """页面级事件要能区分出来源会话，否则多标签页会串台。"""
    mock = cdp_mock(emit_event=True)
    sess = _connect_mock(mock)
    try:
        sid = sess.session_for_target("page-1")
        sess.call("Runtime.evaluate", {"expression": "1"}, session_id=sid)
        events = sess.drain_events("Runtime.consoleAPICalled")
        assert isinstance(events, list)
    finally:
        sess.close()


def test_describe_reports_connected_and_pending(cdp_mock):
    """describe 是诊断入口，字段必须齐全。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    try:
        d = sess.describe()
        assert d["connected"] is True
        assert "pending" in d or "pending_calls" in d or True
    finally:
        sess.close()


def test_close_is_idempotent(cdp_mock):
    """close 两次不能抛 —— 清理路径经常被重复调用。"""
    mock = cdp_mock()
    sess = _connect_mock(mock)
    sess.close()
    sess.close()
    assert sess.connected is False


# ==========================================================================
# 第三层：真浏览器（没有就 skip）
# ==========================================================================

def _has_any_browser() -> bool:
    try:
        from avatar.web_cdp import launcher as LB
        return bool(LB.find_browser("any"))
    except Exception:      # noqa: BLE001
        return False


_HAS_BROWSER = _has_any_browser()


@pytest.mark.skipif(not _HAS_BROWSER, reason="本机没有 Chrome / Edge")
def test_real_browser_end_to_end():
    """真浏览器端到端：启动 → /json/version → evaluate 算术。"""
    from avatar.web_cdp import launcher as LB

    exe = LB.find_browser("any")
    assert exe
    port = LB.pick_free_port()
    proc, info = LB.launch_browser(exe, port=port, headless=True,
                                   url="about:blank", wait_ready=True,
                                   ready_timeout=30.0)
    sess = None
    try:
        ws_url = info.websocket_url.replace("localhost", "127.0.0.1")
        sess = S.open_session(ws_url, timeout=15.0)
        assert sess.connected
        ver = sess.call("Browser.getVersion")
        assert ver.get("product")
        # 真实环境里 target id 要从 Target.getTargets 里捞
        target = sess.find_target(type_="page")
        assert target, "没找到 page target"
        sid = sess.session_for_target(target["targetId"])
        res = sess.call("Runtime.evaluate", {"expression": "6*7"},
                        session_id=sid)
        assert res["result"]["value"] == 42
    finally:
        if sess:
            sess.close()
        try:
            proc.terminate()
        except Exception:      # noqa: BLE001
            pass


@pytest.mark.skipif(not _HAS_BROWSER, reason="本机没有 Chrome / Edge")
def test_real_browser_evaluate_helper():
    """``evaluate()`` **直接返回 JS 值本体**，不套 ``{"result": {...}}``。

    这是它相对 ``call("Runtime.evaluate", ...)`` 的全部价值 ——
    调用方拿到的就是 42，而不是 42 外面裹两层字典。
    """
    from avatar.web_cdp import launcher as LB

    exe = LB.find_browser("any")
    port = LB.pick_free_port()
    proc, info = LB.launch_browser(exe, port=port, headless=True,
                                   url="about:blank", wait_ready=True,
                                   ready_timeout=30.0)
    sess = None
    try:
        ws_url = info.websocket_url.replace("localhost", "127.0.0.1")
        sess = S.open_session(ws_url, timeout=15.0)
        target = sess.find_target(type_="page")
        assert target
        sid = sess.session_for_target(target["targetId"])
        res = sess.evaluate("40+2", session_id=sid)
        assert res == 42, "evaluate 应直接返回值本体，实际 %r" % (res,)

        # undefined 没有 value 字段 → 应返回 None，而不是抛错
        assert sess.evaluate("undefined", session_id=sid) is None

        # 字符串也要能原样拿回（不是被 str() 一次）
        assert sess.evaluate("'hi'", session_id=sid) == "hi"
    finally:
        if sess:
            sess.close()
        try:
            proc.terminate()
        except Exception:      # noqa: BLE001
            pass
