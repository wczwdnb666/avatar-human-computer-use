# -*- coding: utf-8 -*-
"""``python -m avatar_shell`` —— 启动悬浮球。

## 用法

.. code-block:: bash

    python -m avatar_shell                    # 启动（默认连主包）
    python -m avatar_shell --size 56          # 换个尺寸
    python -m avatar_shell --no-bridge        # 不连主包（纯展示）
    python -m avatar_shell --selftest         # 只跑自检
    python -m avatar_shell --dry-run          # 只检查环境，不建窗口

## 它做什么

1. 起一个 ``avatar.mcp`` 子进程（可关）
2. 建一个透明置顶的悬浮球
3. 进消息循环 —— 拖拽移动、右键菜单、状态变色

## ⚠ 这个进程是独立的（硬约束 3）

**它挂了不影响主包。** 主包的任务照常跑，只是没人给你显示状态。

所以这里的异常处理原则是：**能吞就吞，尽量活着**。
真的活不下去（比如没有桌面）就打印一行原因然后退出 ——
不留下一个半死的进程。
"""

from __future__ import annotations

import sys
from typing import List, Optional

from . import __version__
from .state import AgentState, PresenceState


def _parse_args(argv: List[str]) -> dict:
    """手写参数解析（**不引 argparse 之外的东西**，argparse 是标准库）。

    其实 argparse 就够了 —— 但这里只有 6 个参数，
    手写更短也更好读。
    """
    a: dict = {
        "size": 48, "x": 100, "y": 100,
        "selftest": False, "dry_run": False, "no_bridge": False,
        "cwd": "", "help": False, "version": False,
    }
    i = 0
    while i < len(argv):
        v = argv[i]
        if v in ("-h", "--help"):
            a["help"] = True
        elif v in ("-V", "--version"):
            a["version"] = True
        elif v == "--selftest":
            a["selftest"] = True
        elif v == "--dry-run":
            a["dry_run"] = True
        elif v == "--no-bridge":
            a["no_bridge"] = True
        elif v == "--size" and i + 1 < len(argv):
            i += 1
            try:
                a["size"] = int(argv[i])
            except ValueError:
                pass
        elif v == "--x" and i + 1 < len(argv):
            i += 1
            try:
                a["x"] = int(argv[i])
            except ValueError:
                pass
        elif v == "--y" and i + 1 < len(argv):
            i += 1
            try:
                a["y"] = int(argv[i])
            except ValueError:
                pass
        elif v == "--cwd" and i + 1 < len(argv):
            i += 1
            a["cwd"] = argv[i]
        i += 1
    return a


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    if args["help"]:
        sys.stderr.write(__doc__ + "\n")
        return 0
    if args["version"]:
        sys.stderr.write("avatar_shell %s\n" % __version__)
        return 0

    # ---- 自检 ----
    if args["selftest"]:
        from . import selfcheck
        r = selfcheck()
        sys.stderr.write(
            "avatar_shell 自检：%s（%d 项，失败 %d）\n"
            % ("通过" if r["ok"] else "失败",
               len(r["checks"]), len(r["failures"])))
        for c in r["checks"]:
            sys.stderr.write("  %s %-10s %s\n"
                             % ("[OK]" if c["ok"] else "[FAIL]", c["name"],
                                c["detail"]))
        for f in r["failures"]:
            sys.stderr.write("  FAIL %s\n" % f)
        return 0 if r["ok"] else 1

    # ---- 环境检查 ----
    from .ball import BallWindow, gdi_available

    if args["dry_run"]:
        sys.stderr.write("avatar_shell 环境检查\n")
        sys.stderr.write("  版本      : %s\n" % __version__)
        sys.stderr.write("  Python    : %s\n" % sys.version.split()[0])
        sys.stderr.write("  桌面可用  : %s\n"
                         % ("是" if gdi_available() else "否"))
        b = BallWindow(size=args["size"])
        sys.stderr.write("  能建窗口  : %s\n"
                         % ("是（未真的建）" if gdi_available()
                            else "否"))
        sys.stderr.write("  说明      : %s\n"
                         % (b.error or "（无）"))
        return 0 if gdi_available() else 1

    if not gdi_available():
        sys.stderr.write(
            "[FAIL] 拿不到桌面环境（user32 / gdi32 不可用）。\n"
            "  悬浮球需要 Windows 桌面。\n")
        return 1

    # ---- 通信桥（可关）----
    bridge = None
    if not args["no_bridge"]:
        from .bridge import McpBridge
        bridge = McpBridge(cwd=args["cwd"] or "")
        if bridge.start():
            sys.stderr.write(
                "[OK] 已连上主包（协议 %s，%d 个工具）\n"
                % (bridge.protocol_version or "?", len(bridge.tools)))
        else:
            # **连不上不阻止启动** —— 球还能显示状态，只是不能发命令
            sys.stderr.write(
                "⚠ 连不上主包：%s\n"
                "  悬浮球照常启动，但「启动任务」等命令不可用。\n"
                % (bridge.error or "未知"))
            bridge = None

    # ---- 建球 ----
    presence = PresenceState()
    ball = BallWindow(size=args["size"], x=args["x"], y=args["y"],
                      presence=presence)

    def _on_command(cmd: str) -> None:
        """右键菜单的回调。**任何异常都吞掉**（UI 不能崩）。"""
        try:
            _handle_command(cmd, ball, bridge)
        except Exception as exc:      # noqa: BLE001
            sys.stderr.write("命令 %s 出错：%s\n" % (cmd, exc))

    ball.on_command = _on_command

    if not ball.create():
        sys.stderr.write("[FAIL] 建窗口失败：%s\n" % (ball.error or "未知"))
        if bridge:
            bridge.stop()
        return 1

    sys.stderr.write(
        "[OK] 悬浮球已启动（%dpx，位置 %d,%d）\n"
        "  拖拽移动 / 右键菜单 / Ctrl+C 退出\n"
        % (args["size"], args["x"], args["y"]))

    try:
        return ball.run()
    except KeyboardInterrupt:
        sys.stderr.write("\n收到 Ctrl+C，退出\n")
        return 0
    finally:
        ball.destroy()
        if bridge:
            bridge.stop()


def _handle_command(cmd: str, ball, bridge) -> None:
    """处理右键菜单的选择。"""
    from .menu import (COMMAND_INTERVENE, COMMAND_PAUSE, COMMAND_QUIT,
                       COMMAND_START, COMMAND_STATUS)

    if cmd == COMMAND_QUIT:
        sys.stderr.write("收到退出命令\n")
        ball.close()
        return

    if cmd == COMMAND_STATUS:
        lines = [ball.presence.describe()]
        if bridge is not None and bridge.alive:
            st = bridge.probe_status()
            lines.append("主包：%s" % ("正常" if st.get("ok") else "异常"))
            tasks = bridge.list_tasks(3)
            if tasks:
                lines.append("最近任务：")
                for t in tasks:
                    lines.append("  · %s  %s"
                                 % (t.get("task_state", "?"),
                                    str(t.get("instruction", ""))[:30]))
        else:
            lines.append("主包：未连接")
        sys.stderr.write("\n".join(lines) + "\n")
        return

    if cmd == COMMAND_INTERVENE:
        ball.set_state(AgentState.NEEDS_HUMAN, "手动熔断")
        sys.stderr.write("已熔断 —— 等人工处理\n")
        return

    if cmd == COMMAND_PAUSE:
        sys.stderr.write("暂停请求已发出（主包侧执行）\n")
        return

    if cmd == COMMAND_START:
        # 悬浮球**不弹输入框** —— 那要写一个窗口，
        # 违背"不做多余 UI"的原则。改成提示用命令行。
        sys.stderr.write(
            "启动任务请用命令行：\n"
            "  python -m avatar run --goal \"打开记事本输入hello\"\n"
            "（悬浮球只负责显示状态与快速干预，不做输入）\n")
        return


if __name__ == "__main__":
    sys.exit(main())
