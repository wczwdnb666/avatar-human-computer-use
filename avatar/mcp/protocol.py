# -*- coding: utf-8 -*-
"""MCP 协议编解码 —— JSON-RPC over stdio。

## ⚠ 先说一个容易搞错的事实

**MCP 用的是「换行分隔的 JSON」（newline-delimited JSON），不是
LSP 的 ``Content-Length`` 头。**

两种都叫"JSON-RPC over stdio"，但线格式不同：

    LSP 风格（**不是 MCP**）::

        Content-Length: 87\r\n
        \r\n
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}

    MCP 风格（**是这个**）::

        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n

按 LSP 风格写出来的 server，Claude Desktop / Cursor **连不上** ——
它们发的是裸 JSON 行，而你的 server 在等 ``Content-Length``，
双方都在等对方，握手永远不完成。

**本模块两种都收**（读取时自动识别），但**写出时用 MCP 风格**。
这样既不会连不上，也方便用 LSP 风格的工具调试。

## 能力边界

**做**：编码一条消息；解析一行/一段消息（自动识别两种线格式）；
构造标准响应（result / error）；识别通知（无 id，不需要响应）。

**不做**：

- **不做传输层**。不碰 socket、不碰 HTTP。本模块只管字节 ↔ 对象。
- **不做并发**。stdio 是串行的，一个请求一个响应。
- **不做请求路由**。方法名怎么处理是 :mod:`avatar.mcp.server` 的事。
- **不校验 params 的业务语义**。JSON-RPC 层的校验（是不是对象、
  有没有 method）做，工具参数的业务校验在工具自己那里。
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "JSONRPC_VERSION",
    "MCP_PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "ProtocolError",
    "encode_message",
    "decode_message",
    "read_message",
    "make_result",
    "make_error",
    "make_notification",
    "is_notification",
    "describe_protocol",
    "selfcheck",
]

#: JSON-RPC 版本。**固定 2.0。**
JSONRPC_VERSION = "2.0"

#: 我们支持的 MCP 协议版本。
MCP_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "avatar-human-computer-use"
SERVER_VERSION = "0.7.0"

# ---- JSON-RPC 标准错误码 ----
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """协议层错误（消息不是合法 JSON-RPC）。"""

    def __init__(self, message: str, *, code: int = INVALID_REQUEST,
                 data: Any = None):
        super().__init__(message)
        self.code = int(code)
        self.data = data


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------
def encode_message(obj: Dict[str, Any], *, style: str = "mcp") -> bytes:
    """把消息编成字节。**默认 MCP 风格（换行结尾）。**

    ``style="lsp"`` 会加 ``Content-Length`` 头 —— 用于对接 LSP 系工具。
    """
    body = json.dumps(obj, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    if style == "lsp":
        header = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        return header + body
    return body + b"\n"


def decode_message(text: Any) -> Dict[str, Any]:
    """解析一条消息。**收 str 或 bytes。失败抛 :class:`ProtocolError`。**

    会自动剥掉 ``Content-Length`` 头（如果存在）。
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("消息不是合法 UTF-8：%s" % exc,
                                code=PARSE_ERROR) from exc
    s = str(text or "").strip()
    if not s:
        raise ProtocolError("消息为空", code=PARSE_ERROR)

    # 剥 Content-Length 头（LSP 风格）
    if s.lower().startswith("content-length:"):
        _, _, s = s.partition("\r\n\r\n")
        if not s:
            _, _, s = text.partition("\n\n")
        s = s.strip()

    try:
        d = json.loads(s)
    except Exception as exc:      # noqa: BLE001
        raise ProtocolError("不是合法 JSON：%s" % str(exc)[:80],
                            code=PARSE_ERROR) from exc
    if not isinstance(d, dict):
        raise ProtocolError("JSON-RPC 消息必须是对象", code=INVALID_REQUEST)

    if d.get("jsonrpc") != JSONRPC_VERSION and "jsonrpc" in d:
        raise ProtocolError("jsonrpc 版本必须是 \"2.0\"（收到 %r）"
                            % d.get("jsonrpc"), code=INVALID_REQUEST)
    if "method" not in d and "result" not in d and "error" not in d:
        raise ProtocolError("既不是请求也不是响应", code=INVALID_REQUEST)
    return d


def read_message(stream: Any) -> Optional[Dict[str, Any]]:
    """从流里读一条消息。**EOF 返回 ``None``。**

    自动识别两种线格式：

    - 以 ``Content-Length:`` 开头 → 按 LSP 风格读够字节数
    - 其余 → 读一行（MCP 风格）

    用二进制流（``sys.stdin.buffer``）—— 文本流的换行转换会破坏
    ``Content-Length`` 的字节计数。
    """
    if stream is None:
        return None

    try:
        first = stream.readline()
    except Exception:      # noqa: BLE001
        return None
    if not first:
        return None

    raw = first if isinstance(first, bytes) else str(first).encode("utf-8")

    # ---- LSP 风格 ----
    if raw.lower().startswith(b"content-length:"):
        try:
            n = int(raw.split(b":", 1)[1].strip())
        except Exception:      # noqa: BLE001
            raise ProtocolError("Content-Length 不是数字",
                                code=PARSE_ERROR)
        # 吃掉空行
        blank = stream.readline()
        _ = blank
        body = stream.read(n)
        return decode_message(body)

    # ---- MCP 风格（一行一个 JSON）----
    line = raw
    while not line.strip():
        nxt = stream.readline()
        if not nxt:
            return None
        line = nxt if isinstance(nxt, bytes) else str(nxt).encode("utf-8")
    return decode_message(line)


# --------------------------------------------------------------------------
# 构造
# --------------------------------------------------------------------------
def make_result(req_id: Any, result: Any) -> Dict[str, Any]:
    """成功的响应。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def make_error(req_id: Any, code: int, message: str,
               data: Any = None) -> Dict[str, Any]:
    """失败的响应。**``id`` 原样带回** —— 客户端靠它对上号。"""
    err: Dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}


def make_notification(method: str, params: Any = None) -> Dict[str, Any]:
    """通知（**没有 id，不需要响应**）。"""
    out: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION,
                           "method": str(method)}
    if params is not None:
        out["params"] = params
    return out


def is_notification(msg: Dict[str, Any]) -> bool:
    """是通知吗？判据：有 ``method`` 且**没有** ``id``。

    ⚠ ``id`` 存在但为 ``None`` **不算**通知 —— JSON-RPC 2.0 明确
    规定 ``id: null`` 是合法请求（虽然不推荐），要回响应。
    """
    return "method" in msg and "id" not in msg


def describe_protocol() -> str:
    """给人看的协议说明。"""
    return "\n".join([
        "MCP 协议编解码（JSON-RPC 2.0 over stdio）",
        "=" * 60,
        "协议版本：%s" % MCP_PROTOCOL_VERSION,
        "服务标识：%s %s" % (SERVER_NAME, SERVER_VERSION),
        "",
        "⚠ **线格式是「换行分隔的 JSON」，不是 LSP 的 Content-Length。**",
        "",
        "  MCP（本模块写出用这个）：",
        '    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\\n',
        "",
        "  LSP（本模块**读**得懂，但不写出）：",
        "    Content-Length: 58\\r\\n\\r\\n{...}",
        "",
        "按 LSP 风格写出会让 Claude Desktop 连不上 ——",
        "它在等裸 JSON 行，而你在等 Content-Length。",
        "",
        "支持的方法：",
        "  initialize              握手",
        "  notifications/initialized  客户端就绪通知（无响应）",
        "  tools/list              列出工具",
        "  tools/call              调用工具",
        "  ping                    心跳",
        "",
        "错误码：",
        "  %d 解析失败   %d 非法请求   %d 方法不存在"
        % (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND),
        "  %d 参数非法   %d 内部错误"
        % (INVALID_PARAMS, INTERNAL_ERROR),
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """protocol 自检。**纯内存，不碰 stdio。**"""
    import sys as _sys
    t0 = __import__("time").perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("JSON-RPC 版本是 2.0", JSONRPC_VERSION == "2.0", "2.0")
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("★ docstring 说明了 MCP 不是 Content-Length 风格",
         "换行分隔" in (_me.__doc__ or ""), "已说明")

    # ============ 2) 编码 ============
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    b = encode_message(msg)
    _add("★ MCP 风格：换行结尾", b.endswith(b"\n"), repr(b[-3:]))
    _add("★ MCP 风格：没有 Content-Length",
         b"Content-Length" not in b, "干净")
    b2 = encode_message(msg, style="lsp")
    _add("LSP 风格：有 Content-Length 头",
         b2.startswith(b"Content-Length: "), repr(b2[:22]))
    _add("★ 中文不被转义（ensure_ascii=False）",
         "记事本" in encode_message({"t": "记事本"}).decode("utf-8"), "对")

    # ============ 3) 解码 ============
    _add("解码 MCP 风格",
         _me.decode_message(b)["method"] == "ping", "对")
    _add("★ 解码 LSP 风格（自动剥头）",
         _me.decode_message(b2)["method"] == "ping", "对")
    _add("解码 str 和 bytes 都行",
         _me.decode_message(b.decode("utf-8"))["method"] == "ping", "对")

    for bad, why in (("", "空"), ("  ", "空白"), ("不是 json", "非 JSON"),
                     ("[1,2]", "数组不是对象")):
        _add("★ 拒绝：%s（抛 ProtocolError）" % why,
             _raise_type(lambda x=bad: _me.decode_message(x))
             == "ProtocolError", "抛了")
    _add("jsonrpc 版本错也拒绝",
         _raise_type(lambda: _me.decode_message(
             '{"jsonrpc":"1.0","method":"x"}')) == "ProtocolError", "抛了")
    _add("既无 method 也无 result 也拒绝",
         _raise_type(lambda: _me.decode_message(
             '{"jsonrpc":"2.0","id":1}')) == "ProtocolError", "抛了")

    # ============ 4) read_message（两种风格）============
    stream = io.BytesIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "a"}
                   ).encode("utf-8") + b"\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "b"}
                     ).encode("utf-8") + b"\n")
    m1 = _me.read_message(stream)
    m2 = _me.read_message(stream)
    m3 = _me.read_message(stream)
    _add("★ 连续读两条 MCP 消息",
         m1["method"] == "a" and m2["method"] == "b", "对")
    _add("★ EOF 返回 None（不抛）", m3 is None, "None")

    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "c"}
                      ).encode("utf-8")
    lsp_stream = io.BytesIO(
        ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii") + body)
    _add("★ read_message 能读 LSP 风格",
         _me.read_message(lsp_stream)["method"] == "c", "对")

    blank_stream = io.BytesIO(b"\n\n\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 9, "method": "d"}).encode("utf-8") + b"\n")
    _add("★ 跳过空行",
         _me.read_message(blank_stream)["method"] == "d", "对")
    _add("read_message(None) 返回 None",
         _me.read_message(None) is None, "None")

    # ============ 5) 构造响应 ============
    r = _me.make_result(7, {"ok": True})
    _add("make_result 带 id 与 result",
         r["id"] == 7 and r["result"] == {"ok": True}
         and r["jsonrpc"] == "2.0", "对")
    e = _me.make_error(8, _me.METHOD_NOT_FOUND, "没有这个方法")
    _add("make_error 带 code/message",
         e["id"] == 8 and e["error"]["code"] == -32601, "对")
    _add("make_error 可带 data",
         _me.make_error(1, -1, "x", {"hint": "y"})["error"]["data"]
         == {"hint": "y"}, "对")
    n = _me.make_notification("notifications/initialized")
    _add("★ 通知没有 id", "id" not in n, "对")

    # ============ 6) 通知识别 ============
    _add("★ 有 method 无 id → 是通知",
         _me.is_notification({"method": "x"}) is True, "对")
    _add("★ 有 method 有 id → 不是通知",
         _me.is_notification({"method": "x", "id": 1}) is False, "对")
    _add("★ id 为 None 仍算请求（JSON-RPC 2.0 规定）",
         _me.is_notification({"method": "x", "id": None}) is False,
         "不是通知")

    # ============ 7) 错误码 ============
    _add("★ 错误码符合 JSON-RPC 标准",
         (_me.PARSE_ERROR, _me.INVALID_REQUEST, _me.METHOD_NOT_FOUND,
          _me.INVALID_PARAMS, _me.INTERNAL_ERROR)
         == (-32700, -32600, -32601, -32602, -32603), "标准值")

    # ============ 8) describe ============
    d = _me.describe_protocol()
    _add("describe 说明线格式差异", "换行分隔" in d, "有")
    _add("describe 列出方法", "tools/list" in d, "有")

    # ============ 9) 只用标准库 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★ 只依赖标准库（无官方 mcp SDK）",
         not (mods & {"mcp", "requests", "httpx", "aiohttp", "anyio"}),
         sorted(mods))

    elapsed = __import__("time").perf_counter() - t0
    return {"module": "mcp.protocol", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_protocol())
