# -*- coding: utf-8 -*-
"""MCP Server —— 读 stdin、写 stdout、处理 JSON-RPC 请求。

## 启动方式

    python -m avatar.mcp

它会阻塞在 stdin 上，等客户端发请求。

## ⚠ 硬约束 8：stdout 只能出 JSON-RPC

**所有日志、进度、异常信息一律走 stderr。**

理由：MCP 客户端把 stdout 的**每一行**都当成协议消息解析。
往里打一个 ``print("启动成功")`` 就会让客户端收到一条非法消息，
轻则报错，重则直接断开连接 —— 而且这个 bug 极难查，
因为它看起来"只是多打了一行字"。

所以本模块**没有任何** ``print()``，全部走 :meth:`MCPServer._log`。

## 支持的请求

============================  ==========================================
方法                          说明
============================  ==========================================
``initialize``                握手，返回协议版本与能力
``notifications/initialized`` 客户端就绪（**通知，不回响应**）
``tools/list``                列出工具及其 JSON Schema
``tools/call``                调用工具
``ping``                      心跳
============================  ==========================================

## 错误处理策略

**任何异常都不能让 server 崩。** 理由：客户端通常只在启动时
拉起一次 server，崩了之后它不会自动重启 —— 用户看到的是
"AI 突然不能用工具了"，而没有任何提示。

所以 :meth:`handle` 外面包了一层兜底：无论发生什么，
都返回一个 JSON-RPC error 响应，然后继续服务。

## 能力边界

**做**：stdio 主循环；方法分派；工具调用；异常 → 错误响应；
日志走 stderr。

**不做**：

- **不做并发**。stdio 是串行的。同一个时刻只有一个请求在处理 ——
  这也是 ``execute_autonomous_task`` 会阻塞的原因。
- **不做断线重连**。stdin EOF 就是退出信号。
- **不做权限控制**。能拉起这个进程的人本来就能操作这台机器。
- **不做协议协商的降级**。客户端要的协议版本我们不支持时，
  如实返回我们的版本，由客户端决定要不要继续 ——
  **不假装支持**。
- **不做 progress 通知**。长任务期间客户端看不到进度。
  要这个能力得实现 MCP 的 ``notifications/progress``。
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict, List, Optional, TextIO

from . import protocol as P
from .tools import ToolError, ToolRegistry, build_default_tools

__all__ = [
    "MCPServer",
    "serve",
    "selfcheck",
]


class MCPServer:
    """一个 MCP server。**默认用真实 stdio。**"""

    def __init__(self,
                 registry: Optional[ToolRegistry] = None,
                 *,
                 stdin: Any = None,
                 stdout: Any = None,
                 stderr: Any = None,
                 verbose: bool = False) -> None:
        self.registry = registry if registry is not None \
            else build_default_tools()
        #: ⚠ **必须用二进制流** —— 文本流的换行转换会破坏
        #: ``Content-Length`` 的字节计数
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.stderr = stderr if stderr is not None else sys.stderr
        self.verbose = bool(verbose)
        self.served = 0
        self.errors = 0
        #: 客户端在 initialize 里报的协议版本
        self.client_protocol = ""
        self.client_name = ""

    # ---------------- 日志（**只走 stderr**）----------------
    def _log(self, msg: str) -> None:
        if not self.verbose:
            return
        try:
            self.stderr.write("[avatar-mcp] %s\n" % msg)
            self.stderr.flush()
        except Exception:      # noqa: BLE001
            pass

    # ---------------- 输出 ----------------
    def _write(self, obj: Dict[str, Any]) -> None:
        """写一条消息。**只写 JSON-RPC，别的什么都不写。**"""
        try:
            self.stdout.write(P.encode_message(obj))
            self.stdout.flush()
        except Exception as exc:      # noqa: BLE001
            self._log("写 stdout 失败：%s" % exc)

    # ---------------- 主循环 ----------------
    def serve_forever(self) -> int:
        """一直服务到 stdin EOF。**返回退出码。**"""
        self._log("启动，工具 %d 个" % len(self.registry))
        while True:
            try:
                msg = P.read_message(self.stdin)
            except P.ProtocolError as exc:
                # 解析失败也要回一个错误 —— 但**不能崩**
                self.errors += 1
                self._log("协议错误：%s" % exc)
                self._write(P.make_error(None, exc.code, str(exc)))
                continue
            except Exception as exc:      # noqa: BLE001
                self.errors += 1
                self._log("读 stdin 失败：%s" % exc)
                return 1

            if msg is None:
                self._log("stdin EOF，退出")
                return 0

            resp = self.handle(msg)
            if resp is not None:
                self._write(resp)
                self.served += 1

    # ---------------- 分派 ----------------
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理一条消息。**返回 ``None`` = 通知，不需要响应。**

        **永不抛异常** —— 任何问题都变成 JSON-RPC error 响应。
        """
        try:
            return self._dispatch(msg)
        except Exception as exc:      # noqa: BLE001
            self.errors += 1
            rid = msg.get("id") if isinstance(msg, dict) else None
            self._log("处理 %r 时崩了：%s"
                      % (msg.get("method") if isinstance(msg, dict)
                         else None, exc))
            self._log(traceback.format_exc()[-500:])
            return P.make_error(rid, P.INTERNAL_ERROR,
                                "服务端内部错误：%s" % str(exc)[:200])

    def _dispatch(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, dict):
            return P.make_error(None, P.INVALID_REQUEST, "消息不是对象")

        method = str(msg.get("method", "") or "")
        rid = msg.get("id")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            params = {}

        notification = P.is_notification(msg)

        # ---- 通知 ----
        if method in ("notifications/initialized", "initialized"):
            self._log("客户端就绪")
            return None
        if method.startswith("notifications/"):
            self._log("收到通知 %s（忽略）" % method)
            return None

        # ---- 请求 ----
        if method == "initialize":
            return P.make_result(rid, self._initialize(params))
        if method == "ping":
            return P.make_result(rid, {})
        if method == "tools/list":
            return P.make_result(rid, {"tools": self.registry.list_schemas()})
        if method == "tools/call":
            return self._call_tool(rid, params)

        if notification:
            return None
        return P.make_error(rid, P.METHOD_NOT_FOUND,
                            "不支持的方法：%s" % method,
                            {"supported": ["initialize", "ping",
                                           "tools/list", "tools/call"]})

    # ---------------- initialize ----------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """握手。**如实报告我们的版本，不假装支持客户端的。**"""
        self.client_protocol = str(params.get("protocolVersion", "") or "")
        info = params.get("clientInfo") or {}
        if isinstance(info, dict):
            self.client_name = str(info.get("name", "") or "")
        self._log("握手：客户端 %s（协议 %s）"
                  % (self.client_name or "?", self.client_protocol or "?"))

        if self.client_protocol and \
                self.client_protocol != P.MCP_PROTOCOL_VERSION:
            self._log("⚠ 客户端协议版本 %s 与我们支持的 %s 不同，"
                      "仍按我们的版本继续"
                      % (self.client_protocol, P.MCP_PROTOCOL_VERSION))

        return {
            "protocolVersion": P.MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": P.SERVER_NAME,
                           "version": P.SERVER_VERSION},
        }

    # ---------------- tools/call ----------------
    def _call_tool(self, rid: Any,
                   params: Dict[str, Any]) -> Dict[str, Any]:
        name = str(params.get("name", "") or "")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return P.make_error(rid, P.INVALID_PARAMS,
                                "arguments 必须是对象")

        self._log("调用工具 %s" % name)
        try:
            result = self.registry.call(name, args)
        except ToolError as exc:
            # **工具的业务失败** —— 用 isError 而不是 JSON-RPC error。
            # 区别很重要：前者是"工具跑了但没成功"（模型应该看到并调整），
            # 后者是"协议层出了问题"（客户端应该重试或报错）。
            msg = str(exc)
            if exc.hint:
                msg += "\n提示：" + exc.hint
            return P.make_result(rid, _tool_error_content(msg))
        except Exception as exc:      # noqa: BLE001
            return P.make_result(rid, _tool_error_content(
                "%s: %s" % (type(exc).__name__, str(exc)[:300])))

        return P.make_result(rid, _tool_ok_content(result))

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "MCP Server",
            "  名称    : %s %s" % (P.SERVER_NAME, P.SERVER_VERSION),
            "  协议    : %s" % P.MCP_PROTOCOL_VERSION,
            "  工具    : %d 个" % len(self.registry),
            "  日志    : stderr（stdout 只出 JSON-RPC，硬约束 8）",
        ])


def _tool_ok_content(result: Any) -> Dict[str, Any]:
    """把工具返回值包成 MCP 的 content 格式。

    非字符串的结果序列化成 JSON —— 模型读 JSON 比读 Python repr 容易。
    """
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2,
                              default=str)
        except Exception:      # noqa: BLE001
            text = str(result)
    return {"content": [{"type": "text", "text": text}],
            "isError": False}


def _tool_error_content(message: str) -> Dict[str, Any]:
    """工具的**业务失败** —— 用 isError，不是 JSON-RPC error。

    这样模型能看到失败原因并调整策略；而协议层的 error
    会让客户端直接报错给用户。
    """
    return {"content": [{"type": "text", "text": "✗ " + str(message)}],
            "isError": True}


def serve(argv: Optional[List[str]] = None) -> int:
    """``python -m avatar.mcp`` 的入口。"""
    args = list(argv or [])
    verbose = "-v" in args or "--verbose" in args
    return MCPServer(verbose=verbose).serve_forever()


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _MemStream:
    """内存流，用来在自检里假装 stdio。"""

    def __init__(self, data: bytes = b"") -> None:
        self._buf = bytearray(data)
        self.written = bytearray()

    def readline(self) -> bytes:
        i = self._buf.find(b"\n")
        if i < 0:
            if not self._buf:
                return b""
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:i + 1])
        del self._buf[:i + 1]
        return out

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def write(self, data: Any) -> int:
        b = data if isinstance(data, (bytes, bytearray)) \
            else str(data).encode("utf-8")
        self.written.extend(b)
        return len(b)

    def flush(self) -> None:
        pass

    def lines(self) -> List[str]:
        return [l for l in self.written.decode(
            "utf-8", "replace").splitlines() if l.strip()]


class _ErrStream:
    def __init__(self) -> None:
        self.text = ""

    def write(self, s: str) -> int:
        self.text += str(s)
        return len(s)

    def flush(self) -> None:
        pass


def selfcheck(quick: bool = True) -> dict:
    """server 自检。**用内存流，不碰真实 stdio、不动桌面。**"""
    import time
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    def _mk():
        out = _MemStream()
        err = _ErrStream()
        srv = _me.MCPServer(stdout=out, stderr=err, verbose=True)
        return srv, out, err

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("★ docstring 强调 stdout 只出 JSON-RPC",
         "stdout" in (_me.__doc__ or ""), "已说明")

    # ============ 2) initialize ============
    srv, out, err = _mk()
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": P.MCP_PROTOCOL_VERSION,
                               "clientInfo": {"name": "claude-desktop"}}})
    _add("★ initialize 返回协议版本",
         r["result"]["protocolVersion"] == P.MCP_PROTOCOL_VERSION, "对")
    _add("★ initialize 声明 tools 能力",
         "tools" in r["result"]["capabilities"], "对")
    _add("initialize 带 serverInfo",
         r["result"]["serverInfo"]["name"] == P.SERVER_NAME, "对")
    _add("★ 客户端信息被记下",
         srv.client_name == "claude-desktop", srv.client_name)

    # 协议版本不匹配时不假装
    srv2, _, _ = _mk()
    r2 = srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}})
    _add("★ 协议版本不匹配时如实返回我们的版本",
         r2["result"]["protocolVersion"] == P.MCP_PROTOCOL_VERSION,
         "不假装")

    # ============ 3) 通知不回响应 ============
    _add("★ notifications/initialized 返回 None（不回）",
         srv.handle({"jsonrpc": "2.0",
                     "method": "notifications/initialized"}) is None,
         "None")
    _add("★ 其他 notifications/* 也不回",
         srv.handle({"jsonrpc": "2.0",
                     "method": "notifications/cancelled"}) is None, "None")

    # ============ 4) tools/list ============
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    _add("★ tools/list 返回工具数组", isinstance(tools, list) and tools,
         "%d 个" % len(tools))
    _add("★ 每个工具有 name/description/inputSchema",
         all(set(t) >= {"name", "description", "inputSchema"}
             for t in tools), "齐全")
    _add("★ inputSchema 完整（硬约束 9）",
         all(t["inputSchema"].get("type") == "object"
             and "properties" in t["inputSchema"]
             and "required" in t["inputSchema"] for t in tools),
         "全部完整")

    # ============ 5) tools/call ============
    r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "list_windows",
                               "arguments": {"limit": 1}}})
    res = r["result"]
    _add("★ tools/call 返回 content 数组",
         isinstance(res.get("content"), list) and res["content"], "对")
    _add("★ content[0].type == text",
         res["content"][0]["type"] == "text", "对")
    _add("★ isError 字段存在",
         "isError" in res, str(res.get("isError")))

    # ---- 业务失败用 isError，不是 JSON-RPC error ----
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "不存在的工具", "arguments": {}}})
    _add("★★ 工具不存在 → isError（不是 JSON-RPC error）",
         "result" in r and r["result"].get("isError") is True,
         "isError")
    _add("★★ 错误信息里有可用工具列表（可自愈）",
         "可用" in r["result"]["content"][0]["text"], "有提示")

    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "mouse_click", "arguments": {}}})
    _add("★ 缺必填参数 → isError 且说明缺什么",
         r["result"].get("isError") is True
         and "必填" in r["result"]["content"][0]["text"], "有说明")

    r = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "list_windows",
                               "arguments": "不是对象"}})
    _add("★ arguments 不是对象 → JSON-RPC error（协议层）",
         "error" in r and r["error"]["code"] == P.INVALID_PARAMS,
         "error")

    # ============ 6) 未知方法 ============
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "不存在的"})
    _add("★ 未知方法 → METHOD_NOT_FOUND",
         r["error"]["code"] == P.METHOD_NOT_FOUND, "对")
    _add("★ 错误里列出支持的方法（可自愈）",
         "supported" in r["error"]["data"], "有")

    # ============ 7) ping ============
    _add("ping 返回空结果",
         srv.handle({"jsonrpc": "2.0", "id": 8,
                     "method": "ping"})["result"] == {}, "空对象")

    # ============ 8) 永不崩 ============
    class _Boom:
        def get(self, k, d=None):
            raise RuntimeError("坏消息")

    r = srv.handle(_Boom())
    _add("★★ 坏消息对象 → INTERNAL_ERROR（不崩）",
         isinstance(r, dict) and "error" in r, "有响应")

    class _BadReg:
        def call(self, *a, **k):
            raise RuntimeError("工具炸了")

        def list_schemas(self):
            return []

        def __len__(self):
            return 0

    srv_bad = _me.MCPServer(registry=_BadReg(), stdout=_MemStream(),
                            stderr=_ErrStream())
    r = srv_bad.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": "x"}})
    _add("★★ 工具内部抛异常 → isError（不崩）",
         r["result"].get("isError") is True, "isError")

    # ============ 9) 日志走 stderr（硬约束 8）============
    srv3, out3, err3 = _mk()
    srv3.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "test"}}})
    srv3.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    _add("★★ 日志写在 stderr", "avatar-mcp" in err3.text, "有日志")
    _add("★★ stdout 里没有任何非 JSON 内容",
         all(_is_json_line(l) for l in out3.lines()),
         str(out3.lines()[:2]))

    # ============ 10) serve_forever 全流程 ============
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {}}),
        json.dumps({"jsonrpc": "2.0",
                    "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
    ]
    inp = _MemStream(("\n".join(lines) + "\n").encode("utf-8"))
    out4 = _MemStream()
    srv4 = _me.MCPServer(stdin=inp, stdout=out4, stderr=_ErrStream())
    code = srv4.serve_forever()
    _add("★ serve_forever 在 EOF 时正常退出", code == 0, "退出码 %d" % code)
    _add("★★ 4 条消息 → 3 条响应（通知不回）",
         len(out4.lines()) == 3, "%d 条" % len(out4.lines()))
    _add("★ 响应全是合法 JSON-RPC",
         all(_is_json_line(l) for l in out4.lines()), "全部合法")
    _add("served 计数正确", srv4.served == 3, str(srv4.served))

    # ---- 坏行不中断 ----
    # ⚠ bytes 字面量**只能含 ASCII** —— 中文要先 encode
    bad_in = _MemStream("这不是 json\n".encode("utf-8") + json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n")
    out5 = _MemStream()
    srv5 = _me.MCPServer(stdin=bad_in, stdout=out5, stderr=_ErrStream())
    code5 = srv5.serve_forever()
    _add("★★ 遇到坏消息不崩，继续服务后续请求",
         code5 == 0 and len(out5.lines()) == 2, "%d 条" % len(out5.lines()))
    _add("坏消息返回 PARSE_ERROR",
         json.loads(out5.lines()[0])["error"]["code"] == P.PARSE_ERROR,
         "对")

    # ============ 11) describe ============
    _add("describe 不抛", isinstance(srv.describe(), str), "有")
    _add("★ describe 说明 stdout 策略",
         "stderr" in srv.describe(), "有")

    # ============ 12) 不 import 官方 SDK ============
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
    _add("★★ 不 import 官方 mcp SDK",
         "mcp" not in {m for m in mods if m != "protocol"}, sorted(mods))
    _add("只用标准库 + 本包（无官方 SDK）",
         not (mods & {"mcp", "requests", "httpx", "aiohttp"}),
         sorted(mods))

    elapsed = time.perf_counter() - t0
    return {"module": "mcp.server", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _is_json_line(line: str) -> bool:
    try:
        d = json.loads(line)
        return isinstance(d, dict) and "jsonrpc" in d
    except Exception:      # noqa: BLE001
        return False


if __name__ == "__main__":      # pragma: no cover
    # ⚠ **连自检输出也走 stderr** —— 这个模块里一个 print 都不能有。
    #    服务路径上的 print 会污染协议通道，而自检路径上的 print
    #    会让"模块里没有 print"这条断言失效 —— 那条断言的价值
    #    就在于它是**机械可查**的，不该为自检开例外。
    import json as _json
    import sys as _sys
    _r = selfcheck()
    _sys.stderr.write(_json.dumps(
        {"ok": _r["ok"],
         "passed": len(_r["checks"]) - len(_r["failures"]),
         "total": len(_r["checks"]), "elapsed": _r["elapsed"],
         "failures": _r["failures"]},
        ensure_ascii=False, indent=2) + "\n")
