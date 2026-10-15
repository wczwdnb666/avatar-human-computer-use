#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段四 · 手搓 WebSocket 层测试（RFC 6455）。

被测对象是 ``avatar/web_cdp/websocket.py`` —— 一个**完全用标准库**
实现的 WebSocket 客户端（``socket`` + ``hashlib`` + ``base64``）。
禁用了 ``websocket-client`` / ``websockets``，因为：

1. 依赖越少，越不容易在某台干净机器上装不上；
2. 帧编码/解码是本项目的核心能力之一，黑盒用库等于把它外包出去；
3. RFC 6455 的细节（掩码、分片、控制帧不可分片）是必须理解的东西，
   不能"能跑就行"。

测试分三层
----------

**第一层：纯计算**（握手 key、帧编解码）
    完全离线。给字节、要字节。这层最该厚 —— 帧格式错了，
    连接阶段就会挂，但错误信息会指向网络层，非常难查。

**第二层：与真实服务端对话**
    自己起一个最小 WebSocket 服务端（也是标准库手搓的），
    验证握手往返、掩码识别、分片重组、控制帧。
    **不依赖浏览器**，所以在任何机器上都能跑。

**第三层：真浏览器端到端**（可选）
    起 Edge/Chrome 无头实例，真实走一次 CDP。
    没有浏览器时自动 skip（``pytest.mark.skipif``）。

为什么要自己写服务端而不 mock
----------------------------
mock 只能验证"我以为的协议"。RFC 6455 有一堆**只在真握手时才暴露**
的细节：``Sec-WebSocket-Accept`` 的计算、客户端必须掩码、服务端
不能掩码、``Connection: Upgrade`` 大小写。自己写服务端就是把这些
细节实现第二遍 —— 两边对得上，才说明真的对了。
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.web_cdp import websocket as WS  # noqa: E402

#: RFC 6455 规定的握手 GUID。写死在这里当"标准答案"用 ——
#: 从被测代码里 import 这个常量会让测试变成同义反复。
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ==========================================================================
# 第一层：握手 key 计算
# ==========================================================================

def test_make_key_is_16_bytes_base64():
    """客户端 key 必须是 16 字节随机数的 base64（24 字符带 ``==``）。"""
    k = WS.make_key()
    assert isinstance(k, str)
    assert len(k) == 24
    assert k.endswith("==")
    raw = base64.b64decode(k)
    assert len(raw) == 16


def test_make_key_is_random():
    """两次调用必须不同 —— 复用 key 是安全缺陷（可预测的握手）。"""
    keys = {WS.make_key() for _ in range(50)}
    assert len(keys) == 50


def test_expected_accept_matches_rfc():
    """``Accept`` 必须是 ``base64(sha1(key + GUID))``。用 RFC 的官方样例。"""
    # RFC 6455 §1.3 的样例
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    assert WS.expected_accept(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_expected_accept_requires_str_key():
    """``expected_accept`` 的签名是 ``key: str``，不收 bytes。

    这里固化契约：传真值 key 必须拿到 RFC 样例结果；传 bytes 直接报错
    （而不是静默算出一个别的值）。调用方负责在 HTTP 解析层就 decode 好。
    """
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    assert WS.expected_accept(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    with pytest.raises(Exception):
        WS.expected_accept(key.encode("ascii"))


def test_expected_accept_against_manual_sha1():
    """自己手算一遍对照 —— 防止实现里 GUID 写错但自测通过。"""
    key = WS.make_key()
    manual = base64.b64encode(
        hashlib.sha1((key + GUID).encode()).digest()).decode()
    assert WS.expected_accept(key) == manual


# ==========================================================================
# 第一层：帧编码
# ==========================================================================

def test_encode_small_text_frame():
    """最短文本帧：1 字节头 + 1 字节长度+掩码 + 4 字节掩码 + 负载。"""
    frame = WS.encode_frame(0x1, b"hi")
    assert isinstance(frame, (bytes, bytearray))
    # FIN + opcode=1
    assert frame[0] == 0x81
    # MASK=1，长度 2
    assert frame[1] == 0x80 | 2
    assert len(frame) == 2 + 4 + 2


def test_encode_applies_mask():
    """客户端发的帧**必须**掩码（RFC 6455 §5.3）。"""
    payload = b"hello"
    frame = WS.encode_frame(0x1, payload)
    assert frame[1] & 0x80, "MASK 位必须置 1"
    mask = frame[2:6]
    body = frame[6:]
    assert len(body) == len(payload)
    # 用掩码解回来应该还原原文
    unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
    assert unmasked == payload
    # 而且不能和原文一样（除非极小概率掩码全 0）
    assert body != payload


def test_encode_length_16bit():
    """负载 126..65535 用 2 字节长度。"""
    payload = b"x" * 200
    frame = WS.encode_frame(0x1, payload)
    assert frame[1] & 0x7F == 126
    assert struct.unpack(">H", frame[2:4])[0] == 200
    assert frame[4:8] != b""      # 掩码


def test_encode_length_64bit():
    """超过 65535 用 8 字节长度。"""
    payload = b"y" * 70000
    frame = WS.encode_frame(0x1, payload)
    assert frame[1] & 0x7F == 127
    assert struct.unpack(">Q", frame[2:10])[0] == 70000


def test_encode_length_boundaries():
    """长度三档的边界值（125 / 126 / 65535 / 65536）。"""
    for n, expect_field in ((0, 0), (125, 125), (126, 126),
                            (65535, 126), (65536, 127)):
        frame = WS.encode_frame(0x1, b"z" * n)
        assert frame[1] & 0x7F == expect_field, "长度 %d 用了 %d 档" % (
            n, frame[1] & 0x7F)


def test_encode_fin_false():
    """``fin=False`` 表示这是分片的第一段，FIN 位要为 0。"""
    frame = WS.encode_frame(0x1, b"part", fin=False)
    assert frame[0] & 0x80 == 0


def test_encode_opcodes():
    """非文本 opcode（continuation / close / ping / pong / binary）。"""
    for op in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
        frame = WS.encode_frame(op, b"")
        assert frame[0] & 0x0F == op


def test_encode_no_mask_option():
    """``mask=False`` 时不带掩码 —— 服务端才这么发，测试服务端要用。"""
    frame = WS.encode_frame(0x1, b"hi", mask=False)
    assert frame[1] & 0x80 == 0
    assert len(frame) == 2 + 2


def test_encode_rejects_control_frame_fragmentation():
    """控制帧不允许分片（RFC 6455 §5.5）—— 必须报错。"""
    with pytest.raises(Exception):
        WS.encode_frame(WS.OPCODE_PING, b"x", fin=False)


def test_encode_rejects_oversized_control_frame():
    """控制帧 payload 不得超过 125 字节。"""
    with pytest.raises(Exception):
        WS.encode_frame(WS.OPCODE_CLOSE, b"x" * 126)


def test_encode_passes_through_reserved_opcode():
    """保留 opcode 实现层不拦 —— 由上层协议保证不用。

    这里把行为**记录下来**而不是断言报错：帧编码层是"字节打包器"，
    不该承担协议语义校验（校验在解码侧和会话侧）。
    """
    frame = WS.encode_frame(0x3, b"x")
    assert frame[0] & 0x0F == 0x3


def test_encode_rejects_str_payload():
    """负载必须是 bytes —— 传 str 应报错，不做隐式编码。

    这是**刻意**的严格：帧层只管字节，编码是调用方的决定。
    隐式转 UTF-8 会让"我想发 Latin-1 字节"这种需求无从表达，
    而且出错时很难定位。
    """
    with pytest.raises(TypeError):
        WS.encode_frame(WS.OPCODE_TEXT, "中文")


def test_encode_empty_payload():
    """空负载是合法的（close 帧经常没有 body）。"""
    frame = WS.encode_frame(0x8, b"")
    assert len(frame) == 2 + 4


# ==========================================================================
# 第一层：帧解码
# ==========================================================================

def _server_frame(opcode, payload, *, fin=True):
    """造一个**服务端**帧（不带掩码），喂给解码器。"""
    return WS.encode_frame(opcode, payload, fin=fin, mask=False)


def _decode_all(data):
    """喂数据、取出所有**帧**，返回 ``[(fin, opcode, payload), ...]``。"""
    dec = WS.FrameDecoder()
    dec.feed(data)
    return list(dec.frames())


def _decode_messages(data):
    """喂数据、取出所有**消息**（分片已重组），返回 ``[(opcode, payload)]``。"""
    dec = WS.FrameDecoder()
    dec.feed(data)
    return list(dec.messages())


def test_feed_returns_none():
    """``feed`` 是纯投喂，**不返回**解析结果。

    这是刻意的 API 设计：解析结果要能被多次取（``frames`` 取帧、
    ``messages`` 取消息），如果 feed 直接返回，就得在内部缓存两份。
    """
    dec = WS.FrameDecoder()
    assert dec.feed(_server_frame(0x1, b"x")) is None


def test_decode_simple_text():
    """解码服务端发来的最短文本帧。"""
    frames = _decode_all(_server_frame(0x1, b"hello"))
    assert len(frames) == 1
    fin, opcode, payload = frames[0]
    assert opcode == 0x1
    assert payload == b"hello"
    assert fin is True


def test_decode_16bit_length():
    payload = b"a" * 300
    frames = _decode_all(_server_frame(0x1, payload))
    assert frames[0][2] == payload


def test_decode_64bit_length():
    payload = b"b" * 70000
    frames = _decode_all(_server_frame(0x1, payload))
    assert len(frames[0][2]) == 70000


def test_decode_incremental_byte_by_byte():
    """**增量式**：一次喂一个字节也要能正确重组。

    这是真实网络的行为 —— ``recv(4096)`` 拿到的可能只有 1 字节。
    解码器必须是有状态的，不能假设"一次 feed 就是完整帧"。
    """
    dec = WS.FrameDecoder()
    frame = _server_frame(0x1, b"incremental")
    out = []
    for i in range(len(frame)):
        dec.feed(frame[i:i + 1])
        out.extend(dec.frames())
    assert len(out) == 1
    assert out[0][2] == b"incremental"


def test_decode_multiple_frames_one_feed():
    """一次 feed 里含多帧（服务端可能合并发送）。"""
    data = _server_frame(0x1, b"one") + _server_frame(0x1, b"two")
    frames = _decode_all(data)
    assert [f[2] for f in frames] == [b"one", b"two"]


def test_decode_multiple_frames_incremental():
    """多帧 + 分批喂，组合起来是最难的情况。"""
    dec = WS.FrameDecoder()
    data = _server_frame(0x1, b"aaa") + _server_frame(0x1, b"bbb")
    out = []
    for i in range(0, len(data), 3):
        dec.feed(data[i:i + 3])
        out.extend(dec.frames())
    assert [f[2] for f in out] == [b"aaa", b"bbb"]


def test_pending_tracks_buffer():
    """``pending`` 反映还没解析掉的字节数。"""
    dec = WS.FrameDecoder()
    frame = _server_frame(0x1, b"abc")
    dec.feed(frame[:4])
    assert dec.pending == 4
    dec.feed(frame[4:])
    list(dec.frames())
    assert dec.pending == 0


def test_decode_fragmented_message():
    """分片消息：opcode=1 的 fin=0 段 + continuation + 末段 fin=1。

    **帧层**应产出 3 帧；**消息层**应重组成 1 条。
    """
    data = (_server_frame(0x1, b"Hello", fin=False)
            + _server_frame(0x0, b", ", fin=False)
            + _server_frame(0x0, b"World", fin=True))

    frames = _decode_all(data)
    assert len(frames) == 3
    assert [f[0] for f in frames] == [False, False, True]

    msgs = _decode_messages(data)
    assert len(msgs) == 1
    assert msgs[0][0] == 0x1
    assert msgs[0][1] == b"Hello, World"


def test_fragment_with_control_frame_in_between():
    """分片中间穿插控制帧 —— 控制帧必须**立即产出**，不参与重组。"""
    data = (_server_frame(0x1, b"part1", fin=False)
            + _server_frame(0x9, b"ping!")           # 控制帧
            + _server_frame(0x0, b"part2", fin=True))
    msgs = _decode_messages(data)
    assert len(msgs) == 2
    assert msgs[0] == (0x9, b"ping!")                # 先出 ping
    assert msgs[1] == (0x1, b"part1part2")           # 再出重组结果


def test_new_data_frame_during_fragment_raises():
    """分片没结束就来了新的数据帧 → 协议违规。

    报错时机在 ``messages()`` 重组阶段（``frames()`` 只做拆帧，
    不管分片语义），所以段内 feed 后统一在 messages() 上断言。
    """
    dec = WS.FrameDecoder()
    dec.feed(_server_frame(0x1, b"first", fin=False))
    dec.feed(_server_frame(0x1, b"second", fin=True))
    with pytest.raises(Exception):
        list(dec.messages())


def test_decode_close_frame():
    """close 帧（opcode 0x8）带 2 字节状态码。"""
    payload = struct.pack(">H", 1000) + b"bye"
    msgs = _decode_messages(_server_frame(0x8, payload))
    assert msgs[0][0] == 0x8
    assert msgs[0][1][:2] == struct.pack(">H", 1000)


def test_decode_ping_pong_opcodes():
    """ping(0x9) / pong(0xA) 要能正确识别。"""
    data = _server_frame(0x9, b"ping") + _server_frame(0xA, b"pong")
    msgs = _decode_messages(data)
    assert [m[0] for m in msgs] == [0x9, 0xA]


def test_decode_utf8_payload():
    """UTF-8 多字节字符按字节传输，解码层不负责转码。"""
    msgs = _decode_messages(_server_frame(0x1, "中文测试".encode("utf-8")))
    assert msgs[0][1].decode("utf-8") == "中文测试"


def test_decode_masked_frame_is_unmasked():
    """带掩码的帧：解码层会**解掩码**而不是拒绝。

    ⚠ 注意语义：RFC 说服务端**不该**发掩码帧，但实现层选择
    "能解就解"而不是硬报错 —— 因为某些代理/中间件会加掩码。
    这里把实际行为记录下来，避免以后误以为"会抛错"。
    """
    xs = b"masked"
    frame = WS.encode_frame(0x1, xs, mask=True)     # 带掩码
    frames = _decode_all(frame)
    assert frames[0][2] == xs


def test_decode_rejects_rsv_bits():
    """RSV 位非 0（未协商扩展）应报协议错。"""
    dec = WS.FrameDecoder()
    frame = bytes([0x81 | 0x40, 2]) + b"hi"
    dec.feed(frame)
    with pytest.raises(Exception):
        list(dec.frames())


def test_decoder_state_persists_between_feeds():
    """同一个 decoder 连续喂两个帧，状态不能串。"""
    dec = WS.FrameDecoder()
    dec.feed(_server_frame(0x1, b"first"))
    f1 = list(dec.frames())
    dec.feed(_server_frame(0x1, b"second"))
    f2 = list(dec.frames())
    assert f1[0][2] == b"first"
    assert f2[0][2] == b"second"


def test_decoder_stats():
    """解码统计：帧数 / 消息数要分开记。"""
    dec = WS.FrameDecoder()
    dec.feed(_server_frame(0x1, b"a") + _server_frame(0x1, b"b"))
    list(dec.frames())
    assert dec.frames_decoded == 2
    dec.feed(_server_frame(0x1, b"c"))
    list(dec.messages())
    assert dec.messages_decoded == 1


# ==========================================================================
# 第二层：与真实（手搓）服务端对话
# ==========================================================================

class _MiniServer:
    """最小 WebSocket 服务端，**只用标准库**。

    存在的意义：给客户端一个真实的对端。这样握手、掩码、分片这些
    只有在"两个实现对话"时才暴露的问题才能被测出来。
    """

    def __init__(self, *, echo=True, fragment=False, ping_first=False):
        self.echo = echo
        self.fragment = fragment
        self.ping_first = ping_first
        self.received: list = []          # 收到的消息负载
        self.handshake_key = None
        self.client_masked = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.error: Exception | None = None
        self._t = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._t.start()
        return self

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(10.0)
            with conn:
                self._handshake(conn)
                if self.ping_first:
                    conn.sendall(WS.encode_frame(0x9, b"ping!", mask=False))
                buf = b""      # ⚠ 跨 recv 粘包缓冲：大帧会被 TCP 切成多段
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    buf += data
                    frames, buf = self._decode_incoming(buf)
                    for op, payload in frames:
                        if op == 0x8:      # close
                            conn.sendall(WS.encode_frame(0x8, b"", mask=False))
                            return
                        if op == 0x9:      # ping → pong
                            conn.sendall(WS.encode_frame(0xA, payload, mask=False))
                            continue
                        if op == 0xA:      # pong → 忽略
                            continue
                        self.received.append(payload)
                        if self.echo:
                            self._send_reply(conn, payload)
        except Exception as exc:      # noqa: BLE001
            self.error = exc

    def _send_reply(self, conn, payload):
        if self.fragment:
            # 故意分成三段发，测客户端重组
            third = max(1, len(payload) // 3)
            conn.sendall(WS.encode_frame(0x1, payload[:third], fin=False,
                                         mask=False))
            conn.sendall(WS.encode_frame(0x0, payload[third:2 * third],
                                         fin=False, mask=False))
            conn.sendall(WS.encode_frame(0x0, payload[2 * third:],
                                         fin=True, mask=False))
        else:
            conn.sendall(WS.encode_frame(0x1, payload, mask=False))

    def _handshake(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                raise RuntimeError("客户端在握手前断开")
            buf += chunk
        text = buf.decode("latin-1")
        headers = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            raise RuntimeError("客户端没发 Sec-WebSocket-Key")
        self.handshake_key = key
        accept = WS.expected_accept(key)
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        )
        conn.sendall(resp.encode("latin-1"))

    def _decode_incoming(self, data):
        """解客户端帧（带掩码）。返回 ``(frames, 未消费的尾部字节)``。

        尾部字节是**半截帧** —— TCP 不保证一次 recv 收到完整帧，
        调用方必须把它拼到下一轮 recv 的前面。
        """
        out = []
        i = 0
        n = len(data)
        while i + 2 <= n:
            b0, b1 = data[i], data[i + 1]
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            self.client_masked = masked
            ln = b1 & 0x7F
            j = i + 2
            if ln == 126:
                if j + 2 > n:
                    break                       # 长度字段还没收全
                ln = struct.unpack(">H", data[j:j + 2])[0]
                j += 2
            elif ln == 127:
                if j + 8 > n:
                    break
                ln = struct.unpack(">Q", data[j:j + 8])[0]
                j += 8
            if masked:
                if j + 4 > n:
                    break
                mask = data[j:j + 4]
                j += 4
            else:
                mask = None
            if j + ln > n:
                break                           # payload 还没收全 → 等下一段
            if mask is not None:
                payload = bytes(b ^ mask[k % 4]
                                for k, b in enumerate(data[j:j + ln]))
            else:
                payload = data[j:j + ln]
            j += ln
            out.append((op, payload))
            i = j
        return out, data[i:]

    def stop(self):
        try:
            self.sock.close()
        except Exception:      # noqa: BLE001
            pass


@pytest.fixture
def mini_server():
    servers = []

    def _make(**kw):
        s = _MiniServer(**kw).start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


def _connect(port, **kw):
    ws = WS.WebSocket("ws://127.0.0.1:%d/" % port, **kw)
    ws.connect(timeout=10.0)
    return ws


def test_handshake_to_real_server(mini_server):
    """握手能对上真实服务端（Accept 校验通过）。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        assert ws.connected is True
    finally:
        ws.close()


def test_client_frames_are_masked(mini_server):
    """客户端发的帧必须带掩码 —— 由**服务端**视角确认。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        ws.send_text("mask check")
        for _ in range(50):
            if srv.received:
                break
            time.sleep(0.02)
        assert srv.client_masked is True
        assert srv.received == [b"mask check"]
    finally:
        ws.close()


def test_echo_roundtrip(mini_server):
    """发出去的消息能收回来。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        ws.send_text("echo me")
        opcode, payload = ws.recv_message(timeout=5.0)
        assert opcode == WS.OPCODE_TEXT
        assert payload == b"echo me"
    finally:
        ws.close()


def test_roundtrip_multiple_messages(mini_server):
    """连续多条消息，顺序不能乱。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        payloads = ["m%d" % i for i in range(10)]
        for p in payloads:
            ws.send_text(p)
        got = []
        for _ in range(10):
            _op, _pl = ws.recv_message(timeout=5.0)
            got.append(_pl.decode())
        assert got == payloads
    finally:
        ws.close()


def test_roundtrip_utf8(mini_server):
    """中文/emoji 往返不乱码。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        text = "中文 · 🪞 · ünïcödé"
        ws.send_text(text)
        opcode, payload = ws.recv_message(timeout=5.0)
        assert payload.decode("utf-8") == text
    finally:
        ws.close()


def test_roundtrip_large_payload(mini_server):
    """大负载（触发 64 位长度档）往返完整。"""
    srv = mini_server()
    ws = _connect(srv.port)
    try:
        big = "A" * 80000
        ws.send_text(big)
        opcode, payload = ws.recv_message(timeout=10.0)
        assert len(payload) == 80000
    finally:
        ws.close()


def test_fragmented_server_message_reassembled(mini_server):
    """服务端分三片发，客户端应重组成一条完整消息。"""
    srv = mini_server(fragment=True)
    ws = _connect(srv.port)
    try:
        ws.send_text("X" * 300)
        opcode, payload = ws.recv_message(timeout=5.0)
        assert payload == b"X" * 300
    finally:
        ws.close()


def test_ping_from_server_gets_pong(mini_server):
    """服务端主动 ping，客户端应自动回 pong（否则连接会被判死）。"""
    srv = mini_server(ping_first=True)
    ws = _connect(srv.port)
    try:
        # 服务端 ping 之后我们发一条，触发它继续处理
        ws.send_text("after ping")
        for _ in range(100):
            if srv.received:
                break
            time.sleep(0.02)
        assert srv.received, "服务端没收到消息（可能 pong 没回导致断连）"
    finally:
        ws.close()


def test_close_handshake(mini_server):
    """close 之后 ``connected`` 应变 False。"""
    srv = mini_server()
    ws = _connect(srv.port)
    ws.close()
    assert ws.connected is False


def test_recv_timeout_raises(mini_server):
    """没消息时 ``recv_message`` 应超时抛错，不能永远挂着。"""
    srv = mini_server(echo=False)
    ws = _connect(srv.port)
    try:
        t0 = time.monotonic()
        with pytest.raises(Exception):
            ws.recv_message(timeout=0.3)
        assert time.monotonic() - t0 < 5.0
    finally:
        ws.close()


def test_connect_refused_raises():
    """连一个没人监听的端口应报错而不是卡住。"""
    # 先占一个端口再关掉，拿到一个几乎肯定空闲的号
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ws = WS.WebSocket("ws://127.0.0.1:%d/" % port)
    t0 = time.monotonic()
    with pytest.raises(Exception):
        ws.connect(timeout=3.0)
    assert time.monotonic() - t0 < 10.0


def test_bad_accept_rejected():
    """服务端返回错误的 Accept → 客户端必须拒绝（防中间人）。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _bad_handshake():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: WRONGACCEPTVALUE=\r\n\r\n")
        except Exception:      # noqa: BLE001
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_bad_handshake, daemon=True)
    t.start()
    ws = WS.WebSocket("ws://127.0.0.1:%d/" % port)
    with pytest.raises(Exception):
        ws.connect(timeout=5.0)
    t.join(timeout=5)


def test_http_error_status_rejected():
    """服务端回 404 而不是 101 → 握手失败。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _404():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        except Exception:      # noqa: BLE001
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_404, daemon=True)
    t.start()
    ws = WS.WebSocket("ws://127.0.0.1:%d/" % port)
    with pytest.raises(Exception):
        ws.connect(timeout=5.0)
    t.join(timeout=5)


def test_url_parsing_ws_scheme():
    """``ws://host:port/path`` 要正确拆出 host/port/path。"""
    ws = WS.WebSocket("ws://127.0.0.1:9222/devtools/browser/abc")
    assert ws.host == "127.0.0.1"
    assert ws.port == 9222
    assert ws.path.startswith("/devtools/browser/")


def test_url_parsing_default_port():
    """没写端口时用 80（ws）。"""
    ws = WS.WebSocket("ws://example.com/x")
    assert ws.port == 80


def test_url_parsing_rejects_bad_scheme():
    """完全不认识的 scheme 应报错。"""
    with pytest.raises(Exception):
        WS.ws_url_to_host_port("ftp://example.com/x")


def test_url_parsing_rejects_wss_scheme():
    """``wss://`` / ``https://`` **明确拒绝** —— 不是漏实现，是设计决定。

    CDP 调试端口只提供明文 ``ws://``（它绑定在 localhost 上，
    加 TLS 没有意义）。允许 wss 会让人以为可以连远程调试端口 ——
    那是个安全陷阱（相当于把浏览器控制权暴露到网络）。

    拒绝行为本身是**安全特性**，所以要断言它。
    """
    for url in ("wss://example.com/x", "https://example.com/x"):
        with pytest.raises(Exception):
            WS.ws_url_to_host_port(url)


def test_url_parsing_accepts_http_for_tolerance():
    """``http://`` 被**容忍**并当成 ws 处理（``/json`` 端点升级场景）。"""
    host, port, path = WS.ws_url_to_host_port(
        "http://127.0.0.1:9222/devtools/page/A")
    assert host == "127.0.0.1"
    assert port == 9222


def test_url_parsing_helper_is_pure():
    """``ws_url_to_host_port`` 返回 ``(host, port, path)`` 三元组。"""
    host, port, path = WS.ws_url_to_host_port("ws://example.com:9222/a/b")
    assert (host, port, path) == ("example.com", 9222, "/a/b")


# ==========================================================================
# 第三层：真浏览器端到端（没浏览器就跳过）
# ==========================================================================

def _find_browser():
    from avatar.web_cdp import launcher as LB
    return LB.find_browser("any")


_HAS_BROWSER = _find_browser() is not None


@pytest.mark.skipif(not _HAS_BROWSER, reason="本机没有 Chrome/Edge")
def test_real_browser_handshake():
    """连真浏览器：握手成功，且能收到第一帧。

    这是**唯一**能验证"和真实实现对话"的用例。跳过它意味着
    整个 WebSocket 层其实没被真正验证过。
    """
    from avatar.web_cdp import launcher as LB
    from avatar.web_cdp import session as SS

    port = LB.pick_free_port()
    proc, info = LB.launch_browser(_find_browser(), port=port,
                                   headless=True, wait_ready=True,
                                   ready_timeout=30.0)
    try:
        vinfo = LB.probe_debug_port(port)
        assert vinfo, "没探测到调试端口"
        ws_url = vinfo["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
        sess = SS.open_session(ws_url, timeout=15.0)
        try:
            # Browser.getVersion 是最廉价的浏览器级方法
            res = sess.call("Browser.getVersion")
            assert "product" in res
            assert "Browser/" in res["product"] or res["product"]
        finally:
            sess.close()
    finally:
        try:
            proc.terminate()
        except Exception:      # noqa: BLE001
            pass


@pytest.mark.skipif(not _HAS_BROWSER, reason="本机没有 Chrome/Edge")
def test_real_browser_large_message():
    """真浏览器返回大消息（target 列表很长时）不被截断。"""
    from avatar.web_cdp import launcher as LB
    from avatar.web_cdp import session as SS

    port = LB.pick_free_port()
    proc, info = LB.launch_browser(_find_browser(), port=port,
                                   headless=True, wait_ready=True,
                                   ready_timeout=30.0)
    try:
        vinfo = LB.probe_debug_port(port)
        ws_url = vinfo["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
        sess = SS.open_session(ws_url, timeout=15.0)
        try:
            for _ in range(5):
                sess.call("Target.createTarget",
                          {"url": "data:text/html,<h1>x</h1>"})
            res = sess.call("Target.getTargets")
            assert len(res.get("targetInfos") or []) >= 5
        finally:
            sess.close()
    finally:
        try:
            proc.terminate()
        except Exception:      # noqa: BLE001
            pass
