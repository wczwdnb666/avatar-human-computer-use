# -*- coding: utf-8 -*-
"""``python -m avatar_shell`` —— 启动 Agent Presence 外壳。

## 用法

.. code-block:: bash

    python -m avatar_shell                    # 启动（默认连主包）
    python -m avatar_shell --size 56          # 换个尺寸
    python -m avatar_shell --no-bridge        # 不连主包（纯展示）
    python -m avatar_shell --selftest         # 只跑自检
    python -m avatar_shell --dry-run          # 只检查环境，不建窗口
    python -m avatar_shell --no-hotkey        # 不注册全局热键

## 它做什么

1. 起一个 ``avatar.mcp`` 子进程（可关）
2. 建悬浮球（状态可视化：颜色 / 呼吸 / 闪烁 / 200ms 渐变）
3. 挂 tooltip（悬停显示任务名 + 进度，移开 500ms 消失）
4. 注册全局热键 Ctrl+Alt+A（唤起输入框）
5. 起任务状态轮询（2Hz 限频，变化时刷球 + tooltip）
6. 进消息循环 —— 拖拽移动、右键菜单、热键、输入框

## ⚠ 这个进程是独立的（硬约束 3）

**它挂了不影响主包。** 主包的任务照常跑，只是没人给你显示状态。

所以这里的异常处理原则是：**能吞就吞，尽量活着**。
真的活不下去（比如没有桌面）就打印一行原因然后退出 ——
不留下一个半死的进程。
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .state import AgentState, PresenceState


def _parse_args(argv: List[str]) -> dict:
    """手写参数解析。"""
    a: dict = {
        "size": 48, "x": 100, "y": 100,
        "selftest": False, "dry_run": False, "no_bridge": False,
        "no_hotkey": False,
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
        elif v == "--no-hotkey":
            a["no_hotkey"] = True
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
        sys.stderr.write("  能建窗口  : %s\n"
                         % ("是（未真的建）" if gdi_available()
                            else "否"))
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

    # ---- tooltip（悬停显示）----
    from .tooltip import TooltipWindow
    tooltip = TooltipWindow()

    # ---- 输入框（Ctrl+Alt+A 唤起）----
    from .input_box import InputBox

    #: 当前任务信息（tooltip 内容 + 轮询回调更新）。
    _task_info: Dict[str, Any] = {"title": "", "step": None,
                                  "total": None, "running": False}

    input_box = InputBox(
        on_submit=lambda text: _on_submit_task(text, ball, bridge),
        on_cancel=lambda: sys.stderr.write("输入取消\n"))

    # ---- 任务状态轮询（连了主包才有意义）----
    poller = None
    if bridge is not None and bridge.alive:
        from .bridge import TaskStatePoller

        def _on_state_change(state: str, task: Dict[str, Any]) -> None:
            """任务状态变化 → 刷球 + 记 tooltip 内容。

            needs_human 的颜色/闪烁/提示音由 ball.set_state 内部处理
            （它认 AgentState.NEEDS_HUMAN 就播 880Hz x3）。
            """
            _task_info["title"] = str(task.get("instruction")
                                      or task.get("title") or "")[:24]
            _task_info["step"] = task.get("step")
            _task_info["total"] = task.get("total")
            _task_info["running"] = state == "running"
            try:
                ball.set_state(state, "轮询到任务状态变化")
            except Exception:      # noqa: BLE001
                pass

        poller = TaskStatePoller(bridge)
        poller.subscribe(_on_state_change)

    # ---- 全局热键 ----
    hotkey = None
    if not args["no_hotkey"]:
        from .hotkey import HotkeyListener
        hotkey = HotkeyListener(
            on_hotkey=lambda: input_box.show(center=True))
        if not hotkey.register():
            # 冲突：**明确提示**，不静默（硬约束 7）
            sys.stderr.write(
                "⚠ 全局热键注册失败：%s\n"
                "  一键唤起不可用，仍可右键菜单「启动任务…」。\n"
                % (hotkey.error or "未知原因"))
            hotkey = None

    # ---- tick 回调：热键轮询 + tooltip 悬停/倒计时 ----
    def _tick() -> None:
        """主循环 tick：跑热键消息、管 tooltip 显隐。**不阻塞。**"""
        try:
            if hotkey is not None:
                hotkey.poll_once()
        except Exception:      # noqa: BLE001
            pass
        try:
            if ball._hover:
                if not tooltip.visible and tooltip.created:
                    from .presence import format_tooltip
                    text = format_tooltip(
                        _task_info["title"], _task_info["step"],
                        _task_info["total"],
                        running=_task_info["running"],
                        waiting_human=(
                            ball.presence.state == AgentState.NEEDS_HUMAN))
                    # tooltip 显示在球的上方（球的 y 减 tooltip 高）
                    tooltip.show_at(
                        ball.x, max(0, ball.y - 70), text)
                tooltip.cancel_hide()
            else:
                if tooltip.visible and not tooltip.pending_hide:
                    tooltip.request_hide()
            tooltip.poll()
        except Exception:      # noqa: BLE001
            pass
        try:
            if poller is not None:
                poller.poll()
        except Exception:      # noqa: BLE001
            pass

    def _on_command(cmd: str) -> None:
        """右键菜单的回调。**任何异常都吞掉**（UI 不能崩）。"""
        try:
            _handle_command(cmd, ball, bridge, input_box)
        except Exception as exc:      # noqa: BLE001
            sys.stderr.write("命令 %s 出错：%s\n" % (cmd, exc))

    ball.on_command = _on_command
    ball.set_tick_callback(_tick)

    if not ball.create():
        sys.stderr.write("[FAIL] 建窗口失败：%s\n" % (ball.error or "未知"))
        if bridge:
            bridge.stop()
        return 1

    tooltip.create()

    sys.stderr.write(
        "[OK] 悬浮球已启动（%dpx，位置 %d,%d）\n"
        "  拖拽移动 / 右键菜单 / Ctrl+Alt+A 唤起输入框\n"
        % (args["size"], args["x"], args["y"]))

    try:
        return ball.run()
    except KeyboardInterrupt:
        sys.stderr.write("\n收到 Ctrl+C，退出\n")
        return 0
    finally:
        if hotkey is not None:
            hotkey.unregister()
        tooltip.destroy()
        input_box.destroy()
        ball.destroy()
        if bridge:
            bridge.stop()


def _on_submit_task(text: str, ball, bridge) -> None:
    """输入框 Enter：发任务 + 球变 running。"""
    text = str(text or "").strip()
    if not text:
        sys.stderr.write("任务目标为空，忽略\n")
        return
    if bridge is None or not bridge.alive:
        sys.stderr.write(
            "没连上主包，发不了任务。请去掉 --no-bridge 重启。\n")
        return
    ok, msg = bridge.send_task(text)
    if ok:
        ball.set_state(AgentState.RUNNING, "新任务已发出")
        sys.stderr.write("[OK] 任务已发出：%s\n" % text[:40])
    else:
        sys.stderr.write("任务发出失败：%s\n" % (msg or "未知")[:120])


def _handle_command(cmd: str, ball, bridge, input_box) -> None:
    """处理右键菜单的选择。"""
    from .menu import (COMMAND_INTERVENE, COMMAND_PAUSE, COMMAND_QUIT,
                       COMMAND_RECENT, COMMAND_START, COMMAND_STATUS)

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

    if cmd == COMMAND_RECENT:
        # 查看最近任务：5 条任务名 + 状态（从主包拉）
        if bridge is not None and bridge.alive:
            tasks = bridge.list_tasks(5)
            if tasks:
                lines = ["最近 5 条任务："]
                for t in tasks:
                    lines.append("  · [%s] %s"
                                 % (t.get("task_state", "?"),
                                    str(t.get("instruction", ""))[:40]))
                sys.stderr.write("\n".join(lines) + "\n")
            else:
                sys.stderr.write("没有任务记录\n")
        else:
            sys.stderr.write("主包未连接，看不到任务记录\n")
        return

    if cmd == COMMAND_INTERVENE:
        ball.set_state(AgentState.NEEDS_HUMAN, "手动熔断")
        sys.stderr.write("已熔断 —— 等人工处理\n")
        return

    if cmd == COMMAND_PAUSE:
        if bridge is not None and bridge.alive:
            ok, msg = bridge.call_tool_text("tasks_pause", {})
            sys.stderr.write("暂停：%s\n" % (msg or "已发出" if ok
                                             else msg or "失败")[:80])
        else:
            sys.stderr.write("主包未连接，发不了暂停\n")
        return

    if cmd == COMMAND_START:
        # 弹输入框（Ctrl+Alt+A 同款）
        input_box.show(center=True)
        return


if __name__ == "__main__":
    sys.exit(main())
