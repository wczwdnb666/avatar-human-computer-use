# -*- coding: utf-8 -*-
"""MCP Server —— 让 Claude Desktop / Cursor 直接调用 Avatar。

## 这是什么

`MCP <https://modelcontextprotocol.io>`_（Model Context Protocol）是
"把工具接给大模型"的标准协议。本包把 avatar 的能力做成一个
**零依赖的 MCP server** —— 不装官方 SDK，用 ``http.server`` 之外的
纯标准库手搓 JSON-RPC over stdio。

## 一条命令启动

.. code-block:: bash

    python -m avatar.mcp

它阻塞在 stdin 上，等客户端发请求。

## 接进 Claude Desktop

编辑 ``%APPDATA%\\Claude\\claude_desktop_config.json``：

.. code-block:: json

    {
      "mcpServers": {
        "avatar": {
          "command": "python",
          "args": ["-m", "avatar.mcp"],
          "cwd": "C:/github/avatar-human-computer-use",
          "env": { "PYTHONPATH": "C:/github/avatar-human-computer-use" }
        }
      }
    }

重启 Claude Desktop，对话里会出现一个工具图标。

## 能力边界（整个包）

**做**：JSON-RPC 编解码；MCP 握手；工具注册与调用；
把 avatar 的九项能力（启动应用 / 点击 / 输入 / UIA 查找 /
五层视觉查找 / CDP 执行 JS / 风控检测 / 熔断 / 自主任务）暴露给客户端。

**不做**：

- **不装官方 mcp SDK**。零依赖是这个项目从头到尾的约束。
- **不做鉴权与限流**。stdio 是本机进程间通信。
- **不做并发**。stdio 串行，一个请求一个响应。
- **不硬解验证题**（硬约束 #9）。``detect`` 检出就返回"需要人"。
- **不绕过风控**。``execute_autonomous_task`` 走完整执行器，风控闸照生效。
"""

from .protocol import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
    make_error,
    make_result,
)
from .server import MCPServer, serve
from .tools import Tool, ToolError, ToolRegistry, build_default_tools

__all__ = [
    # protocol
    "JSONRPC_VERSION", "MCP_PROTOCOL_VERSION", "SERVER_NAME",
    "SERVER_VERSION", "ProtocolError", "encode_message",
    "decode_message", "make_result", "make_error",
    # server
    "MCPServer", "serve",
    # tools
    "Tool", "ToolRegistry", "ToolError", "build_default_tools",
]


def selfcheck(quick: bool = True) -> dict:
    """包级自检 —— 汇总三个子模块。"""
    import time
    from . import protocol, server, tools

    t0 = time.perf_counter()
    checks = []
    failures = []
    for name, mod in (("protocol", protocol), ("tools", tools),
                      ("server", server)):
        try:
            r = mod.selfcheck(quick=quick)
            c = len(r.get("checks", []))
            f = len(r.get("failures", []))
            checks.append({"name": "子模块 %s" % name, "ok": f == 0,
                           "detail": "%d/%d" % (c - f, c)})
            failures.extend("%s:%s" % (name, x)
                            for x in r.get("failures", []))
        except Exception as exc:      # noqa: BLE001
            checks.append({"name": "子模块 %s" % name, "ok": False,
                           "detail": str(exc)[:120]})
            failures.append(name)

    return {"module": "mcp", "quick": bool(quick), "checks": checks,
            "failures": failures, "ok": not failures,
            "elapsed": round(time.perf_counter() - t0, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "checks": r["checks"], "failures": r["failures"],
         "elapsed": r["elapsed"]}, ensure_ascii=False, indent=2))
