# -*- coding: utf-8 -*-
"""``python -m avatar.mcp`` —— 启动 MCP Server（stdio）。

## ⚠ 这个文件里不能有任何 print()

stdout 是 MCP 的协议通道。往它写一个字节的非 JSON-RPC 内容，
客户端就会报"非法消息"。

所有人类可读的信息走 **stderr**（``-v`` 打开）。

## 用法

.. code-block:: bash

    python -m avatar.mcp            # 静默启动（stdio 模式）
    python -m avatar.mcp -v         # 日志走 stderr
    python -m avatar.mcp --selftest # 跑自检然后退出
    python -m avatar.mcp --list     # 列出工具然后退出
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        from . import selfcheck
        r = selfcheck()
        sys.stderr.write(
            "MCP 自检：%s（%d 项，失败 %d）\n"
            % ("通过" if r["ok"] else "失败",
               len(r["checks"]), len(r["failures"])))
        for c in r["checks"]:
            sys.stderr.write("  %s %-20s %s\n"
                             % ("[OK]" if c["ok"] else "[FAIL]", c["name"],
                                c["detail"]))
        for f in r["failures"]:
            sys.stderr.write("  FAIL %s\n" % f)
        return 0 if r["ok"] else 1

    if "--list" in args:
        from .tools import describe_tools
        sys.stderr.write(describe_tools() + "\n")
        return 0

    if "-h" in args or "--help" in args:
        sys.stderr.write(__doc__ + "\n")
        return 0

    from .server import serve
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
