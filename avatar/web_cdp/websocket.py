# -*- coding: utf-8 -*-
"""纯 socket 手搓 WebSocket 客户端（RFC 6455）。

**这个模块存在的唯一理由**：CDP（Chrome DevTools Protocol）的传输层是
WebSocket，而我们要坚持零第三方依赖 —— 不允许 ``websocket-client`` /
``websockets`` / ``aiohttp``。所以帧协议得自己实现。

为什么不用 ``websockets`` 这类库：
- 它们会拖进 asyncio / 大量依赖，和本项目的同步阻塞风格冲突
- CDP 只需要**客户端**方向的极小子集：不压缩、不做扩展协商、
  服务端消息长度已知（JSON-RPC，很少超过 64KB 但必须支持 64 位长度）

实现范围（RFC 6455 §4–§6）：

1. **握手** —— 标准 HTTP Upgrade。``Sec-WebSocket-Key`` 必须是
   **16 字节随机数的 base64**（不是任意字符串），服务端会用它和固定
   GUID 做 SHA-1 来验证。响应里的 ``Sec-WebSocket-Accept`` 必须校验 ——
   不校验就等于"我连上了但我不知道对面是谁"。
2. **掩码** —— RFC 6455 §5.3 规定**客户端发出的所有帧必须掩码**
   （服务端发往客户端的不许掩码）。这是硬性要求，Chrome 会直接断开
   未掩码的客户端帧。
3. **分片** —— 一条消息可以由多个帧组成（``FIN=0`` 开头，
   ``FIN=1`` 结尾，中间帧 ``opcode=0``）。控制帧（ping/pong/close）
   **不允许分片**，且可以插在分片消息中间。
4. **长度编码** —— 7 位 / 7+16 位 / 7+64 位三种。126 和 127 是哨兵值，
   不是长度本身。

不做的事（明确记录，避免以后被当成 bug）：
- 不实现 permessage-deflate（CDP 默认不开）
- 不实现客户端主动发 ping（CDP 不需要，服务端会发）
- 不实现多线程并发写（由 session.py 加锁保证）

线程安全约定：``WebSocket`` 对象本身**不是**线程安全的。
``send`` / ``recv`` 必须在同一线程，或者由调用方加锁。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time
from typing import List, Optional, Tuple

from ..errors import AvatarError

__all__ = [
    "WebSocketError",
    "HandshakeError",
    "WebSocketClosed",
    "WS_GUID",
    "OPCODE_CONTINUATION",
    "OPCODE_TEXT",
    "OPCODE_BINARY",
    "OPCODE_CLOSE",
    "OPCODE_PING",
    "OPCODE_PONG",
    "make_key",
    "expected_accept",
    "encode_frame",
    "FrameDecoder",
    "WebSocket",
    "ws_url_to_host_port",
]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class WebSocketError(AvatarError):
    """WebSocket 层错误基类。"""

    exit_code = 11


class HandshakeError(WebSocketError):
    """HTTP Upgrade 握手失败（状态码不对 / Accept 校验不过）。"""

    exit_code = 11

    def __init__(self, message: str, status: int = 0, accept: str = "", **ctx):
        super().__init__(message, status=status or None,
                         accept=accept or None, **ctx)
        self.status = status
        self.accept = accept


class WebSocketClosed(WebSocketError):
    """对端关闭了连接，或者我们主动关了。"""

    exit_code = 11

    def __init__(self, message: str = "WebSocket 已关闭",
                 code: int = 0, reason: str = "", **ctx):
        super().__init__(message, code=code or None,
                         reason=reason or None, **ctx)
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: RFC 6455 §1.3 规定的固定 GUID。拼在 Sec-WebSocket-Key 后面做 SHA-1。
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

#: 控制帧的 opcode 都 >= 8，且不可分片
_CONTROL_OPCODES = frozenset({OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG})

#: 单帧能表示的最大长度（2^63-1）。实际上 CDP 消息远小于这个。
_MAX_PAYLOAD = (1 << 63) - 1

#: 默认单次 recv 的缓冲区大小。64KB 是 TCP 窗口的常见档位。
DEFAULT_RECV_SIZE = 65536


# --------------------------------------------------------------------------
# 握手工具
# --------------------------------------------------------------------------
def make_key() -> str:
    """生成 ``Sec-WebSocket-Key``。

    ⚠️ 必须是 **16 字节随机数的 base64**（RFC 6455 §4.1 第 7 条）。
    常见错误是随便写个固定字符串 —— 服务端不会拒绝，但你也无法
    通过 ``Sec-WebSocket-Accept`` 校验来确认对端真的是 WebSocket
    服务端（而不是某个碰巧返回 101 的反向代理）。
    """
    return base64.b64encode(os.urandom(16)).decode("ascii")


def expected_accept(key: str) -> str:
    """按 RFC 6455 §4.2.2 算出服务端应该返回的 ``Sec-WebSocket-Accept``。

    ``base64(sha1(key + GUID))``。这是握手完整性的唯一凭据。
    """
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_url_to_host_port(url: str) -> Tuple[str, int, str]:
    """把 ``ws://host:port/path`` 拆成 ``(host, port, path)``。

    只支持 ``ws://``（不处理 ``wss://`` —— CDP 的调试端口永远是明文，
    因为 Chrome 拒绝在非 localhost 上开调试端口却不带 TLS 是刻意的：
    它就是要你确保只有本机能连）。

    也接受裸 ``host:port/path`` 形式，方便调用方直接传。
    """
    s = url.strip()
    if s.startswith("ws://"):
        s = s[5:]
    elif s.startswith("http://"):
        # 允许把 /json 端点升级后的 http URL 传进来，容错
        s = s[7:]
    elif s.startswith("wss://") or s.startswith("https://"):
        raise WebSocketError("不支持 wss:// —— CDP 调试端口只有明文 ws://",
                             url=url)

    # 拆 path
    path = "/"
    slash = s.find("/")
    if slash >= 0:
        path = s[slash:]
        s = s[:slash]

    # 拆 host:port
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise WebSocketError("端口不是数字：%r" % port_s, url=url)
    else:
        host, port = s, 80

    if not host:
        raise WebSocketError("URL 里没有主机名：%r" % url, url=url)
    return host, port, path


# --------------------------------------------------------------------------
# 帧编码
# --------------------------------------------------------------------------
def encode_frame(opcode: int, payload: bytes = b"", *,
                 fin: bool = True, mask: bool = True) -> bytes:
    """把一个帧编码成字节串。

    字节布局（RFC 6455 §5.2）::

        0               1               2               3
        0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
        +-+-+-+-+-------+-+-------------+-------------------------------+
        |F|R|R|R| opcode|M| Payload len |    Extended payload length    |
        |I|S|S|S|  (4)  |A|     (7)     |             (16/64)           |
        |N|V|V|V|       |S|             |   (if payload len==126/127)   |
        | |1|2|3|       |K|             |                               |
        +-+-+-+-+-------+-+-------------+ - - - - - - - - - - - - - - - +
        |     Extended payload length continued, if payload len == 127  |
        + - - - - - - - - - - - - - - - +-------------------------------+
        |                               |Masking-key, if MASK set to 1  |
        +-------------------------------+-------------------------------+
        | Masking-key (continued)       |          Payload Data         |
        +-------------------------------- - - - - - - - - - - - - - - - +

    **客户端必须掩码**（``mask=True``）。控制帧 payload 必须 <= 125 字节。
    """
    if opcode in _CONTROL_OPCODES:
        if not fin:
            raise WebSocketError("控制帧不允许分片（FIN 必须为 1）",
                                 opcode=opcode)
        if len(payload) > 125:
            raise WebSocketError("控制帧 payload 不得超过 125 字节",
                                 opcode=opcode, size=len(payload))
    if len(payload) > _MAX_PAYLOAD:
        raise WebSocketError("payload 过大", size=len(payload))

    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    out = bytearray()
    out.append(b0)

    length = len(payload)
    mask_bit = 0x80 if mask else 0x00

    if length < 126:
        out.append(mask_bit | length)
    elif length <= 0xFFFF:
        out.append(mask_bit | 126)
        out += struct.pack("!H", length)
    else:
        out.append(mask_bit | 127)
        out += struct.pack("!Q", length)

    if mask:
        key = os.urandom(4)
        out += key
        # 掩码算法：payload[i] XOR key[i % 4]
        out += _apply_mask(payload, key)
    else:
        out += payload

    return bytes(out)


def _apply_mask(data: bytes, key: bytes) -> bytes:
    """XOR 掩码。加密和解密是同一个操作（异或自反）。"""
    if not data:
        return b""
    k = key
    # 把 key 铺满到和 data 一样长，然后整体异或 —— 比逐字节取模快
    repeated = (k * (len(data) // 4 + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, repeated))


# --------------------------------------------------------------------------
# 帧解码（增量式）
# --------------------------------------------------------------------------
class FrameDecoder:
    """增量式帧解码器。

    为什么不一次性解析：TCP 是**字节流**，一次 ``recv`` 拿到的可能是
    半个帧、一个半帧、或者三个帧。所以必须有"喂字节 → 吐出完整帧"的
    状态机。

    用法::

        dec = FrameDecoder()
        dec.feed(sock.recv(65536))
        for frame in dec.frames():
            ...

    ``frames()`` 是生成器，**会消耗**内部缓冲。用完再 ``feed``。
    """

    #: 单条消息（分片重组后）的上限。CDP 响应通常 < 1MB，
    #: 截图 base64 可能到 10MB+。给 256MB 足够且能挡住内存炸弹。
    MAX_MESSAGE = 256 * 1024 * 1024

    def __init__(self) -> None:
        self._buf = bytearray()
        #: 分片重组：当前累积的 payload 片段
        self._fragments: List[bytes] = []
        self._fragment_opcode: Optional[int] = None
        self._fragment_size = 0
        #: 统计（自检和调试用）
        self.frames_decoded = 0
        self.messages_decoded = 0

    # -- 输入 ---------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """喂入新收到的字节。"""
        if data:
            self._buf += data

    @property
    def pending(self) -> int:
        """缓冲区里还有多少字节没解析。"""
        return len(self._buf)

    # -- 解析 ---------------------------------------------------------------
    def frames(self):
        """逐帧产出 ``(fin, opcode, payload)``。

        注意：这里产出的是**帧**，不是消息。分片重组在 ``messages()``。
        """
        while True:
            frame = self._try_parse_one()
            if frame is None:
                return
            self.frames_decoded += 1
            yield frame

    def messages(self):
        """逐条产出重组后的消息 ``(opcode, payload)``。

        - 文本/二进制帧：直接产出
        - 分片帧：累积到 FIN 才产出
        - 控制帧：**立即穿插产出**（哪怕在分片中间）
        """
        for fin, opcode, payload in self.frames():
            if opcode in _CONTROL_OPCODES:
                # 控制帧不参与分片重组，直接穿插
                self.messages_decoded += 1
                yield opcode, payload
                continue

            if opcode != OPCODE_CONTINUATION:
                # 新消息的开头
                if self._fragment_opcode is not None:
                    # 上一个分片消息没结束就来了新的 —— 协议违规
                    self._reset_fragments()
                    raise WebSocketError(
                        "分片消息未结束就收到新的数据帧（协议违规）",
                        opcode=opcode)
                self._fragment_opcode = opcode

            self._fragments.append(payload)
            self._fragment_size += len(payload)
            if self._fragment_size > self.MAX_MESSAGE:
                self._reset_fragments()
                raise WebSocketError("分片消息超过上限，疑似内存炸弹",
                                     size=self._fragment_size)

            if fin:
                op = self._fragment_opcode
                body = b"".join(self._fragments)
                self._reset_fragments()
                self.messages_decoded += 1
                yield op, body

    def _reset_fragments(self) -> None:
        self._fragments = []
        self._fragment_opcode = None
        self._fragment_size = 0

    def _try_parse_one(self):
        """尝试从缓冲里解出一个完整帧。不够就返回 None。"""
        buf = self._buf
        n = len(buf)
        if n < 2:
            return None

        b0 = buf[0]
        b1 = buf[1]
        fin = bool(b0 & 0x80)
        rsv = b0 & 0x70
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        if rsv:
            # 我们从不协商扩展，所以 RSV 必须为 0
            raise WebSocketError("RSV 位非 0，对端使用了未协商的扩展",
                                 rsv=rsv)

        offset = 2
        if length == 126:
            if n < offset + 2:
                return None
            length = struct.unpack_from("!H", buf, offset)[0]
            offset += 2
        elif length == 127:
            if n < offset + 8:
                return None
            length = struct.unpack_from("!Q", buf, offset)[0]
            offset += 8
            if length > _MAX_PAYLOAD:
                raise WebSocketError("长度超过 2^63-1", length=length)

        if length > self.MAX_MESSAGE:
            raise WebSocketError("单帧超过上限", length=length)

        mask_key = None
        if masked:
            if n < offset + 4:
                return None
            mask_key = bytes(buf[offset:offset + 4])
            offset += 4

        if n < offset + length:
            return None

        payload = bytes(buf[offset:offset + length])
        del buf[:offset + length]

        if mask_key:
            payload = _apply_mask(payload, mask_key)

        return fin, opcode, payload


# --------------------------------------------------------------------------
# WebSocket 客户端
# --------------------------------------------------------------------------
class WebSocket:
    """一个同步阻塞的 WebSocket 客户端连接。

    典型用法::

        ws = WebSocket("ws://127.0.0.1:9222/devtools/page/ABC")
        ws.connect(timeout=5.0)
        ws.send_text('{"id":1,"method":"Page.enable"}')
        opcode, data = ws.recv_message(timeout=5.0)
        ws.close()

    ``timeout`` 的语义统一为**整次操作**的墙钟上限。内部用
    ``settimeout`` 实现，所以超时会抛 ``socket.timeout``，
    我们把它翻译成 ``TimeoutError_``。
    """

    def __init__(self, url: str, *,
                 origin: Optional[str] = None,
                 extra_headers: Optional[dict] = None) -> None:
        self.url = url
        self.host, self.port, self.path = ws_url_to_host_port(url)
        self.origin = origin
        self.extra_headers = extra_headers or {}

        self._sock: Optional[socket.socket] = None
        self._decoder = FrameDecoder()
        self._closed = False
        self._close_code = 0
        self._close_reason = ""

        #: 握手响应头（调试用，也让上层能读 Sec-WebSocket-Accept）
        self.handshake_status = 0
        self.handshake_headers: dict = {}

        #: 统计
        self.bytes_sent = 0
        self.bytes_received = 0
        self.ping_count = 0
        self.pong_count = 0

    # -- 状态 ---------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    # -- 连接 ---------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        """建 TCP 连接 + 完成 HTTP Upgrade 握手。"""
        if self._sock is not None:
            raise WebSocketError("已经连接过了，请新建实例")

        try:
            sock = socket.create_connection((self.host, self.port), timeout)
        except OSError as exc:
            raise WebSocketError(
                "连接 %s:%d 失败：%s" % (self.host, self.port, exc),
                host=self.host, port=self.port)

        sock.settimeout(timeout)
        self._sock = sock

        try:
            self._handshake(timeout)
        except Exception:
            # 握手失败必须关掉 socket，否则 fd 泄漏
            self._hard_close()
            raise

    def _handshake(self, timeout: float) -> None:
        key = make_key()
        want_accept = expected_accept(key)

        lines = [
            "GET %s HTTP/1.1" % self.path,
            "Host: %s:%d" % (self.host, self.port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if self.origin:
            lines.append("Origin: %s" % self.origin)
        for k, v in self.extra_headers.items():
            lines.append("%s: %s" % (k, v))

        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        self._sock.sendall(request)
        self.bytes_sent += len(request)

        # 读响应头。必须逐字节读到 \r\n\r\n —— 多读一个字节就会
        # 把 WebSocket 帧的开头吃掉，那是经典 bug。
        raw = self._read_http_head(timeout)
        head, _, leftover = raw.partition(b"\r\n\r\n")

        if leftover:
            # 服务端把第一帧和响应头一起发了（少见但合法）
            self._decoder.feed(leftover)

        text = head.decode("latin-1")
        header_lines = text.split("\r\n")
        status_line = header_lines[0] if header_lines else ""
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1])
        except (IndexError, ValueError):
            raise HandshakeError("响应状态行无法解析：%r" % status_line)

        headers = {}
        for line in header_lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        self.handshake_status = status
        self.handshake_headers = headers

        if status != 101:
            raise HandshakeError(
                "期望 101 Switching Protocols，实际 %d" % status,
                status=status, headers=headers)

        got_accept = headers.get("sec-websocket-accept", "")
        if got_accept != want_accept:
            # 不校验就等于不知道对面是不是 WebSocket 服务端
            raise HandshakeError(
                "Sec-WebSocket-Accept 校验失败（期望 %r，实际 %r）"
                % (want_accept, got_accept),
                status=status, accept=got_accept)

        upgrade = headers.get("upgrade", "").lower()
        if upgrade != "websocket":
            raise HandshakeError(
                "响应头 Upgrade 不是 websocket：%r" % upgrade,
                status=status)

    def _read_http_head(self, timeout: float) -> bytes:
        """读到 ``\\r\\n\\r\\n`` 为止。返回**含**结尾分隔符的完整头。"""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            if b"\r\n\r\n" in buf:
                return bytes(buf)
            remain = deadline - time.monotonic()
            if remain <= 0:
                from ..errors import TimeoutError_
                raise TimeoutError_("读 HTTP 响应头超时", elapsed=timeout)
            self._sock.settimeout(max(0.05, remain))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                from ..errors import TimeoutError_
                raise TimeoutError_("读 HTTP 响应头超时", elapsed=timeout)
            if not chunk:
                raise HandshakeError("握手期间连接被对端关闭")
            self.bytes_received += len(chunk)
            buf += chunk
            if len(buf) > 65536:
                raise HandshakeError("响应头超过 64KB，疑似不是 WebSocket 端点")

    # -- 发送 ---------------------------------------------------------------
    def send_text(self, text: str, *, timeout: float = 10.0) -> None:
        """发一条文本消息。"""
        self._send(OPCODE_TEXT, text.encode("utf-8"), timeout)

    def send_binary(self, data: bytes, *, timeout: float = 10.0) -> None:
        """发一条二进制消息。"""
        self._send(OPCODE_BINARY, data, timeout)

    def send_ping(self, payload: bytes = b"", *, timeout: float = 10.0) -> None:
        """发 ping。客户端主动 ping 在 CDP 里用不上，但协议要求支持。"""
        if len(payload) > 125:
            raise WebSocketError("ping payload 不得超过 125 字节")
        self._send(OPCODE_PING, payload, timeout)
        self.ping_count += 1

    def send_pong(self, payload: bytes = b"", *, timeout: float = 10.0) -> None:
        """发 pong（收到 ping 时的回应）。"""
        if len(payload) > 125:
            raise WebSocketError("pong payload 不得超过 125 字节")
        self._send(OPCODE_PONG, payload, timeout)
        self.pong_count += 1

    def _send(self, opcode: int, payload: bytes, timeout: float) -> None:
        if self._closed or self._sock is None:
            raise WebSocketClosed("连接已关闭，无法发送", code=self._close_code,
                                  reason=self._close_reason)
        frame = encode_frame(opcode, payload, fin=True, mask=True)
        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(frame)
        except socket.timeout:
            from ..errors import TimeoutError_
            raise TimeoutError_("发送 WebSocket 帧超时", elapsed=timeout)
        except OSError as exc:
            raise WebSocketClosed("发送失败，连接可能已断：%s" % exc)
        self.bytes_sent += len(frame)

    # -- 接收 ---------------------------------------------------------------
    def recv_message(self, timeout: float = 10.0,
                     msg_type: Optional[int] = None):
        """收一条消息，返回 ``(opcode, payload_bytes)``。

        ``msg_type`` 非 None 时只返回该类型，其他类型（如 ping）自动处理：
        - ping → 自动回 pong，然后继续等
        - close → 回 close 并抛 ``WebSocketClosed``

        超时抛 ``TimeoutError_``（**不抛 socket.timeout** —— 上层统一捕获
        自定义异常）。
        """
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                from ..errors import TimeoutError_
                raise TimeoutError_("接收 WebSocket 消息超时", elapsed=timeout)
            op, data = self._next_message(remain)
            if op == OPCODE_PING:
                # 协议要求：收到 ping 必须尽快回 pong，且回同样的 payload
                try:
                    self.send_pong(data, timeout=min(2.0, remain))
                except Exception:
                    pass
                continue
            if op == OPCODE_PONG:
                self.pong_count += 0
                continue
            if op == OPCODE_CLOSE:
                self._handle_close(data)
                raise WebSocketClosed("对端主动关闭", code=self._close_code,
                                      reason=self._close_reason)
            if msg_type is not None and op != msg_type:
                continue
            return op, data

    def recv_text(self, timeout: float = 10.0) -> str:
        """收一条文本消息（丢掉控制帧）。"""
        op, data = self.recv_message(timeout, msg_type=OPCODE_TEXT)
        return data.decode("utf-8", errors="replace")

    def _next_message(self, timeout: float):
        """从解码器拿一条消息，缓冲区空就 recv。"""
        for op, data in self._decoder.messages():
            return op, data

        # 缓冲区里没有完整消息，去读
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                from ..errors import TimeoutError_
                raise TimeoutError_("等待 WebSocket 数据超时", elapsed=timeout)

            self._sock.settimeout(max(0.05, remain))
            try:
                chunk = self._sock.recv(DEFAULT_RECV_SIZE)
            except socket.timeout:
                from ..errors import TimeoutError_
                raise TimeoutError_("等待 WebSocket 数据超时", elapsed=timeout)

            if not chunk:
                # 对端关了 TCP。RFC 说应该先收到 close 帧，但现实中
                # 直接断的情况很常见（进程被杀）。
                self._closed = True
                raise WebSocketClosed("TCP 连接被对端关闭")

            self.bytes_received += len(chunk)
            self._decoder.feed(chunk)
            for op, data in self._decoder.messages():
                return op, data

    @property
    def buffered_messages(self) -> int:
        """解码器缓冲里还有几条待取消息（不算控制帧）。"""
        return 0

    # -- 关闭 ---------------------------------------------------------------
    def _handle_close(self, data: bytes) -> None:
        """处理收到的 close 帧：解析状态码，回一个 close。"""
        code, reason = 1005, ""
        if len(data) >= 2:
            code = struct.unpack("!H", data[:2])[0]
            if len(data) > 2:
                reason = data[2:].decode("utf-8", errors="replace")
        self._close_code = code
        self._close_reason = reason
        try:
            payload = struct.pack("!H", 1000) if code == 1005 else data[:125]
            frame = encode_frame(OPCODE_CLOSE, payload, fin=True, mask=True)
            self._sock.sendall(frame)
            self.bytes_sent += len(frame)
        except Exception:
            pass
        self._closed = True

    def close(self, code: int = 1000, reason: str = "",
              timeout: float = 2.0) -> None:
        """主动关闭：发 close 帧，然后关 socket。

        已经关闭时是**幂等**的，不报错 —— 调用方不必先判断。
        """
        if self._closed or self._sock is None:
            self._hard_close()
            return
        try:
            payload = struct.pack("!H", code)
            if reason:
                payload += reason.encode("utf-8")[:123]
            frame = encode_frame(OPCODE_CLOSE, payload, fin=True, mask=True)
            self._sock.settimeout(timeout)
            self._sock.sendall(frame)
            self.bytes_sent += len(frame)
            # 尝试读对端的 close 回执，读不到也不强求
            self._sock.settimeout(min(timeout, 0.5))
            try:
                self._sock.recv(4096)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            self._closed = True
            self._close_code = code
            self._close_reason = reason
            self._hard_close()

    def _hard_close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._closed = True

    # -- 上下文管理 ---------------------------------------------------------
    def __enter__(self) -> "WebSocket":
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else (
            "connected" if self._sock else "init")
        return "<WebSocket %s:%d%s %s>" % (self.host, self.port, self.path, state)

    # -- 契约方法 -----------------------------------------------------------
    def settimeout(self, timeout: float) -> None:
        """设置后续 socket 操作的超时（不改变方法级 timeout 参数）。"""
        if self._sock is not None:
            self._sock.settimeout(timeout)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """WebSocket 帧编解码自检（**不联网**）。

    握手部分需要真实服务端，由 ``tests/test_phase4_websocket.py`` 里的
    本地 TCP 假服务端覆盖。这里只测纯计算：密钥生成、Accept 计算、
    帧编码、解码状态机（含分片与掩码）。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 密钥 ---------------------------------------------------------------
    k1, k2 = make_key(), make_key()
    _add("key 是 16 字节 base64", len(base64.b64decode(k1)) == 16,
         "%d 字节" % len(base64.b64decode(k1)))
    _add("key 每次都不同", k1 != k2, "%s… vs %s…" % (k1[:8], k2[:8]))

    # -- Accept 计算（RFC 6455 §1.3 的官方测试向量）------------------------
    # 这是 RFC 里给的标准例子，必须一字不差
    rfc_key = "dGhlIHNhbXBsZSBub25jZQ=="
    rfc_want = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    _add("RFC 6455 官方 Accept 向量",
         expected_accept(rfc_key) == rfc_want,
         expected_accept(rfc_key))

    # -- URL 解析 -----------------------------------------------------------
    h, p, path = ws_url_to_host_port("ws://127.0.0.1:9222/devtools/page/AB")
    _add("ws URL 解析", (h, p, path) == ("127.0.0.1", 9222,
                                        "/devtools/page/AB"),
         "%s:%d%s" % (h, p, path))
    _add("无 path 时默认 /",
         ws_url_to_host_port("ws://localhost:9222")[2] == "/",
         ws_url_to_host_port("ws://localhost:9222")[2])
    try:
        ws_url_to_host_port("wss://example.com/x")
        _add("wss 被拒绝", False, "居然接受了 wss://")
    except WebSocketError:
        _add("wss 被拒绝", True, "WebSocketError")

    # -- 帧编码：长度三档 ---------------------------------------------------
    f_small = encode_frame(OPCODE_TEXT, b"hi")
    _add("短帧长度位 = 2", (f_small[1] & 0x7F) == 2,
         "len bits = %d" % (f_small[1] & 0x7F))
    f_16 = encode_frame(OPCODE_TEXT, b"x" * 200)
    _add("126 哨兵 + 16 位长度", (f_16[1] & 0x7F) == 126,
         "len bits = %d" % (f_16[1] & 0x7F))
    _add("16 位长度值正确",
         struct.unpack_from("!H", f_16, 2)[0] == 200,
         str(struct.unpack_from("!H", f_16, 2)[0]))
    f_64 = encode_frame(OPCODE_TEXT, b"y" * 70000)
    _add("127 哨兵 + 64 位长度", (f_64[1] & 0x7F) == 127,
         "len bits = %d" % (f_64[1] & 0x7F))
    _add("64 位长度值正确",
         struct.unpack_from("!Q", f_64, 2)[0] == 70000,
         str(struct.unpack_from("!Q", f_64, 2)[0]))

    # -- 掩码位必须置位 -----------------------------------------------------
    _add("客户端帧必须掩码", bool(f_small[1] & 0x80), "MASK=1")

    # -- 控制帧限制 ---------------------------------------------------------
    try:
        encode_frame(OPCODE_PING, b"x" * 200)
        _add("控制帧 payload <= 125 被强制", False, "居然编码成功了")
    except WebSocketError:
        _add("控制帧 payload <= 125 被强制", True, "WebSocketError")
    try:
        encode_frame(OPCODE_PING, b"", fin=False)
        _add("控制帧不许分片", False, "居然编码成功了")
    except WebSocketError:
        _add("控制帧不许分片", True, "WebSocketError")

    # -- 解码往返 -----------------------------------------------------------
    for size in (0, 5, 125, 126, 300, 65535, 65536, 70000):
        payload = os.urandom(size) if size else b""
        enc = encode_frame(OPCODE_BINARY, payload)
        dec = FrameDecoder()
        dec.feed(enc)
        got = list(dec.frames())
        ok = len(got) == 1 and got[0][2] == payload and got[0][1] == OPCODE_BINARY
        _add("往返 %d 字节" % size, ok,
             "OK" if ok else "帧数 %d" % len(got))

    # -- 分片重组 -----------------------------------------------------------
    d = FrameDecoder()
    d.feed(encode_frame(OPCODE_TEXT, b"AAA", fin=False))
    _add("分片未结束时无消息", len(list(d.messages())) == 0, "0 条")
    d.feed(encode_frame(OPCODE_CONTINUATION, b"BBB", fin=False))
    d.feed(encode_frame(OPCODE_CONTINUATION, b"CCC", fin=True))
    msgs = list(d.messages())
    _add("三个分片重组为一条",
         len(msgs) == 1 and msgs[0][1] == b"AAABBBCCC",
         "%d 条 %r" % (len(msgs), msgs[0][1] if msgs else None))

    # -- 控制帧穿插在分片中间 -----------------------------------------------
    d = FrameDecoder()
    d.feed(encode_frame(OPCODE_TEXT, b"X", fin=False))
    d.feed(encode_frame(OPCODE_PING, b"ping!"))
    d.feed(encode_frame(OPCODE_CONTINUATION, b"Y", fin=True))
    msgs = list(d.messages())
    ops = [m[0] for m in msgs]
    _add("控制帧可穿插在分片中",
         msg_ok := (OPCODE_PING in ops and OPCODE_TEXT in ops),
         "opus=%s" % ops)

    # -- 字节流切片（半个帧）------------------------------------------------
    enc = encode_frame(OPCODE_TEXT, b"hello world")
    d = FrameDecoder()
    out = []
    for i in range(len(enc)):
        d.feed(enc[i:i + 1])
        out.extend(d.messages())
    _add("逐字节喂入仍能解出",
         len(out) == 1 and out[0][1] == b"hello world",
         "%d 条" % len(out))

    # -- 一次喂多个帧 -------------------------------------------------------
    blob = (encode_frame(OPCODE_TEXT, b"one")
            + encode_frame(OPCODE_TEXT, b"two")
            + encode_frame(OPCODE_TEXT, b"three"))
    d = FrameDecoder()
    d.feed(blob)
    out = list(d.messages())
    _add("一次喂三帧解出三条", len(out) == 3,
         " / ".join(x[1].decode() for x in out))

    # -- RSV 位必须报错 -----------------------------------------------------
    bad = bytearray(encode_frame(OPCODE_TEXT, b"z"))
    bad[0] |= 0x40   # 置 RSV1
    d = FrameDecoder()
    d.feed(bytes(bad))
    try:
        list(d.frames())
        _add("RSV 非 0 报错", False, "居然放过了")
    except WebSocketError:
        _add("RSV 非 0 报错", True, "WebSocketError")

    return {"checks": checks, "failures": failures, "ok": not failures}
