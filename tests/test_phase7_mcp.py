# -*- coding: utf-8 -*-
"""阶段七 · MCP Server（``avatar.mcp``）。

## 这个文件在守什么

MCP 是"外部客户端能调我们"的接口，出问题的代价最直接：

1. **线格式搞错** —— 客户端连不上（MCP 是换行分隔 JSON，不是 LSP）
2. **stdout 被污染** —— 客户端收到非法消息后断开
3. **schema 不完整** —— 客户端不知道参数怎么填

## 全程内存流

不碰真实 stdio，不起服务。
"""
from __future__ import annotations

import io
import json

import pytest

from avatar.mcp import protocol as P
from avatar.mcp.server import MCPServer
from avatar.mcp.tools import ToolError, ToolRegistry, build_default_tools


class MemStream:
    def __init__(self, data: bytes = b""):
        self._buf = bytearray(data)
        self.written = bytearray()

    def readline(self):
        i = self._buf.find(b"\n")
        if i < 0:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:i + 1])
        del self._buf[:i + 1]
        return out

    def read(self, n=-1):
        out = bytes(self._buf if n < 0 else self._buf[:n])
        del self._buf[:len(out)]
        return out

    def write(self, d):
        b = d if isinstance(d, (bytes, bytearray)) else str(d).encode()
        self.written.extend(b)
        return len(b)

    def flush(self):
        pass

    def lines(self):
        return [l for l in self.written.decode("utf-8", "replace")
                .splitlines() if l.strip()]


class ErrStream:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += str(s)
        return len(s)

    def flush(self):
        pass


def _srv(**kw):
    out = kw.pop("stdout", None) or MemStream()
    err = ErrStream()
    return MCPServer(stdout=out, stderr=err, verbose=True, **kw), out, err


class TestWireFormat:
    """★ MCP 是换行分隔 JSON，**不是 LSP 的 Content-Length**。"""

    def test_encode_uses_newline(self):
        b = P.encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"})
        assert b.endswith(b"\n")
        assert b"Content-Length" not in b

    def test_encode_lsp_style_available(self):
        b = P.encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"},
                             style="lsp")
        assert b.startswith(b"Content-Length: ")

    def test_decode_handles_both(self):
        m = {"jsonrpc": "2.0", "id": 1, "method": "x"}
        assert P.decode_message(P.encode_message(m))["method"] == "x"
        assert P.decode_message(P.encode_message(m, style="lsp"))["method"] \
            == "x"

    def test_read_message_mcp_style(self):
        s = io.BytesIO(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "a"}).encode() + b"\n")
        assert P.read_message(s)["method"] == "a"

    def test_read_message_lsp_style(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 2,
                           "method": "b"}).encode()
        s = io.BytesIO(("Content-Length: %d\r\n\r\n" % len(body)).encode()
                       + body)
        assert P.read_message(s)["method"] == "b"

    def test_read_eof_returns_none(self):
        assert P.read_message(io.BytesIO(b"")) is None

    def test_skips_blank_lines(self):
        s = io.BytesIO(b"\n\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "c"}).encode() + b"\n")
        assert P.read_message(s)["method"] == "c"

    @pytest.mark.parametrize("bad", ["", "  ", "不是 json", "[1,2]"])
    def test_decode_rejects(self, bad):
        with pytest.raises(P.ProtocolError):
            P.decode_message(bad)

    def test_error_codes_are_standard(self):
        assert (P.PARSE_ERROR, P.INVALID_REQUEST, P.METHOD_NOT_FOUND,
                P.INVALID_PARAMS, P.INTERNAL_ERROR) == \
            (-32700, -32600, -32601, -32602, -32603)


class TestNotifications:
    def test_no_id_is_notification(self):
        assert P.is_notification({"method": "x"}) is True

    def test_with_id_is_request(self):
        assert P.is_notification({"method": "x", "id": 1}) is False

    def test_null_id_still_request(self):
        """★ JSON-RPC 2.0 规定 `id: null` 是请求，不是通知。"""
        assert P.is_notification({"method": "x", "id": None}) is False

    def test_server_does_not_reply_to_notification(self):
        srv, _, _ = _srv()
        assert srv.handle({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None


class TestToolsSchema:
    def test_at_least_seven_tools(self):
        assert len(build_default_tools()) >= 7

    @pytest.mark.parametrize("name", [
        "launch_app", "mouse_click", "cdp_execute_js", "vision_locate",
        "execute_autonomous_task", "detect", "pause_for_human",
    ])
    def test_required_tools_exist(self, name):
        assert build_default_tools().get(name) is not None

    def test_all_schemas_complete(self):
        """★★ 硬约束 9：type / properties / required 三样都要在。"""
        for t in build_default_tools().all():
            s = t.input_schema
            assert s.get("type") == "object", t.name
            assert "properties" in s, t.name
            assert isinstance(s.get("required"), list), t.name

    def test_required_fields_exist_in_properties(self):
        """★ required 里的名字必须在 properties 里 —— 否则客户端会懵。"""
        for t in build_default_tools().all():
            props = set(t.input_schema.get("properties") or {})
            for r in (t.input_schema.get("required") or []):
                assert r in props, "%s.%s" % (t.name, r)

    def test_all_have_description(self):
        for t in build_default_tools().all():
            assert t.description, t.name

    def test_schemas_json_safe(self):
        assert json.dumps(build_default_tools().list_schemas(),
                          ensure_ascii=False)

    def test_long_running_marked(self):
        reg = build_default_tools()
        assert reg.get("execute_autonomous_task").long_running is True
        assert reg.get("list_windows").long_running is False


class TestRegistry:
    def _reg(self):
        r = ToolRegistry()
        r.add("echo", "回显", {"type": "object",
                               "properties": {"x": {"type": "integer"}},
                               "required": ["x"]}, lambda x: x)
        return r

    def test_call(self):
        assert self._reg().call("echo", {"x": 5}) == 5

    def test_missing_required(self):
        with pytest.raises(ToolError):
            self._reg().call("echo", {})

    def test_unknown_tool_lists_available(self):
        with pytest.raises(ToolError) as ei:
            self._reg().call("nope", {})
        assert "可用" in ei.value.hint

    def test_args_not_dict(self):
        with pytest.raises(ToolError):
            self._reg().call("echo", "不是对象")

    def test_duplicate_name_rejected(self):
        r = self._reg()
        with pytest.raises(ValueError):
            r.add("echo", "x", {"type": "object", "properties": {},
                                "required": []}, lambda: None)


class TestServerProtocol:
    def test_initialize(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion":
                                   P.MCP_PROTOCOL_VERSION,
                                   "clientInfo": {"name": "test"}}})
        assert r["result"]["protocolVersion"] == P.MCP_PROTOCOL_VERSION
        assert "tools" in r["result"]["capabilities"]
        assert srv.client_name == "test"

    def test_initialize_reports_our_version_not_clients(self):
        """★ 客户端版本不认识时**如实报我们的**，不假装。"""
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "1999-01-01"}})
        assert r["result"]["protocolVersion"] == P.MCP_PROTOCOL_VERSION

    def test_tools_list(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = r["result"]["tools"]
        assert tools and all(
            set(t) >= {"name", "description", "inputSchema"} for t in tools)

    def test_ping(self):
        srv, _, _ = _srv()
        assert srv.handle({"jsonrpc": "2.0", "id": 3,
                           "method": "ping"})["result"] == {}

    def test_unknown_method(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "不存在"})
        assert r["error"]["code"] == P.METHOD_NOT_FOUND
        assert "supported" in r["error"]["data"]


class TestToolCall:
    def test_success_returns_content(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "list_windows",
                                   "arguments": {"limit": 1}}})
        res = r["result"]
        assert res["content"][0]["type"] == "text"
        assert "isError" in res

    def test_business_failure_uses_iserror(self):
        """★★ 工具的业务失败用 isError，**不是** JSON-RPC error。

        区别很重要：前者是"工具跑了但没成功"（模型能看到并调整），
        后者是"协议层出了问题"（客户端直接报错）。
        """
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "不存在的工具",
                                   "arguments": {}}})
        assert "result" in r and r["result"]["isError"] is True

    def test_protocol_error_for_bad_arguments(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "list_windows",
                                   "arguments": "不是对象"}})
        assert r["error"]["code"] == P.INVALID_PARAMS

    def test_missing_required_gives_hint(self):
        srv, _, _ = _srv()
        r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "mouse_click",
                                   "arguments": {}}})
        assert r["result"]["isError"] is True
        assert "必填" in r["result"]["content"][0]["text"]


class TestNeverCrashes:
    def test_bad_message_object(self):
        srv, _, _ = _srv()

        class Boom:
            def get(self, k, d=None):
                raise RuntimeError("坏消息")

        assert "error" in srv.handle(Boom())

    def test_tool_raises(self):
        class BadReg:
            def call(self, *a, **k):
                raise RuntimeError("工具炸了")

            def list_schemas(self):
                return []

            def __len__(self):
                return 0

        srv = MCPServer(registry=BadReg(), stdout=MemStream(),
                        stderr=ErrStream())
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "x"}})
        assert r["result"]["isError"] is True

    def test_bad_line_does_not_stop_server(self):
        """★ 遇到坏消息要继续服务后续请求。"""
        good = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        inp = MemStream("这不是 json\n".encode("utf-8")
                        + good.encode() + b"\n")
        out = MemStream()
        srv = MCPServer(stdin=inp, stdout=out, stderr=ErrStream())
        assert srv.serve_forever() == 0
        assert len(out.lines()) == 2
        assert json.loads(out.lines()[0])["error"]["code"] == P.PARSE_ERROR


class TestStdoutHygiene:
    """★★ 硬约束 8：stdout 只出 JSON-RPC，日志走 stderr。"""

    def test_logs_go_to_stderr(self):
        srv, _, err = _srv()
        srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "t"}}})
        assert "avatar-mcp" in err.text

    def test_stdout_only_json(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        inp = MemStream(("\n".join(lines) + "\n").encode())
        out = MemStream()
        MCPServer(stdin=inp, stdout=out,
                  stderr=ErrStream()).serve_forever()
        for line in out.lines():
            d = json.loads(line)
            assert isinstance(d, dict) and d.get("jsonrpc") == "2.0"

    def test_notification_not_replied(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        inp = MemStream(("\n".join(lines) + "\n").encode())
        out = MemStream()
        MCPServer(stdin=inp, stdout=out,
                  stderr=ErrStream()).serve_forever()
        assert len(out.lines()) == 2


class TestNoSDK:
    def test_no_official_mcp_sdk(self):
        import ast
        for mod in (P, __import__("avatar.mcp.server", fromlist=["x"]),
                    __import__("avatar.mcp.tools", fromlist=["x"])):
            tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        mods.add((a.name or "").split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    mods.add((node.module or "").split(".")[0])
            assert not (mods & {"requests", "httpx", "aiohttp", "anyio",
                                "pydantic"}), (mod.__name__, mods)
