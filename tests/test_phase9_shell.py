# -*- coding: utf-8 -*-
"""阶段九 · ORB 式交互外壳（``avatar_shell``）。

## 这个文件在守什么

1. **状态机** —— 四种状态 + 转换合法性（可测的纯逻辑）
2. **菜单构造** —— 排序、id、按状态变灰（可测的纯逻辑）
3. **画圆算法** —— 抗锯齿、透明边界（可测的纯计算）
4. **通信桥** —— 请求构造、错误处理（mock 掉 stdin/stdout）
5. **三条硬约束** —— 不用 UI 框架 / 不 import avatar / 故障隔离

## ⚠ 不测 GUI 渲染

**窗口创建、消息循环、实际绘制都不测** —— 那需要真实桌面，
而且在 CI 里跑不了（无头环境）。

**能测的是纯逻辑，测干净的也是纯逻辑。** 这是本项目一贯的分工：
把可测的部分和不可测的部分**在代码结构上分开**。

## 无桌面环境

`gdi_available()` 为 False 时，涉及窗口的测试自动 skip。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 外壳是独立目录，不在 avatar 包内 —— 要手动加进 sys.path
_SHELL_ROOT = Path(__file__).resolve().parent.parent
if str(_SHELL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHELL_ROOT))

import avatar_shell                        # noqa: E402
from avatar_shell import ball as BALL      # noqa: E402
from avatar_shell import bridge as BR      # noqa: E402
from avatar_shell import menu as MENU      # noqa: E402
from avatar_shell.state import (           # noqa: E402
    STATE_ANIMATION,
    STATE_COLORS,
    STATE_LABELS,
    AgentState,
    PresenceState,
    StateError,
)

_HAS_DESKTOP = BALL.gdi_available()
_needs_desktop = pytest.mark.skipif(not _HAS_DESKTOP, reason="无桌面环境")


# ==========================================================================
# 1. 包契约
# ==========================================================================
class TestPackage:
    def test_version(self):
        assert avatar_shell.__version__ == "0.1.0"

    def test_docstring_has_boundary(self):
        assert "能力边界" in (avatar_shell.__doc__ or "")

    def test_exports(self):
        for n in ("AgentState", "PresenceState", "StateError"):
            assert hasattr(avatar_shell, n), n

    def test_package_selfcheck(self):
        r = avatar_shell.selfcheck()
        assert r["ok"] is True, r["failures"]


# ==========================================================================
# 2. 状态机
# ==========================================================================
class TestStateMachine:
    def test_four_states(self):
        assert len(AgentState) == 4
        assert {s.value for s in AgentState} == {
            "idle", "running", "needs_human", "error"}

    def test_every_state_has_color_label_animation(self):
        for s in AgentState:
            assert s in STATE_COLORS, s
            assert s in STATE_LABELS, s
            assert s in STATE_ANIMATION, s

    def test_colors_distinct(self):
        assert len({STATE_COLORS[s] for s in AgentState}) == 4

    def test_semantic_colors(self):
        """★ 颜色语义：灰/绿/橙/红。"""
        assert STATE_COLORS[AgentState.RUNNING][1] > 150       # 绿
        assert STATE_COLORS[AgentState.NEEDS_HUMAN][0] > 200   # 橙
        assert STATE_COLORS[AgentState.ERROR][0] > 180         # 红

    def test_animation_semantics(self):
        """★ NEEDS_HUMAN 闪得最快；ERROR 不闪。"""
        assert STATE_ANIMATION[AgentState.NEEDS_HUMAN][1] == 0.5
        assert STATE_ANIMATION[AgentState.ERROR][0] is False
        assert STATE_ANIMATION[AgentState.IDLE][0] is False

    def test_happy_path(self):
        p = PresenceState()
        assert p.state == "idle"
        p.start()
        assert p.state == "running"
        p.done()
        assert p.state == "idle"

    def test_need_human_roundtrip(self):
        p = PresenceState()
        p.start()
        p.need_human()
        assert p.state == "needs_human"
        assert p.needs_attention is True
        p.resumed()
        assert p.state == "running"

    def test_error_only_reset(self):
        """★★ ERROR 没有出边（除了 reset）—— 错误必须被看见。"""
        p = PresenceState()
        p.error()
        assert p.can("start") is False
        assert p.can("done") is False
        assert set(p.allowed_events()) == {"error", "reset"}

    def test_needs_human_cannot_restart(self):
        """★★ NEEDS_HUMAN 不能用 start 重开 —— 会掩盖"上次没处理完"。"""
        p = PresenceState()
        p.start()
        p.need_human()
        assert p.can("start") is False

    @pytest.mark.parametrize("state,event", [
        ("idle", "done"), ("idle", "need_human"), ("idle", "resumed"),
        ("error", "start"), ("error", "done"),
        ("needs_human", "start"), ("needs_human", "done"),
        ("idle", "乱写"),
    ])
    def test_illegal_raises(self, state, event):
        with pytest.raises(StateError):
            PresenceState(state=state).transition(event)

    def test_history(self):
        p = PresenceState()
        p.start("跑 A")
        p.done()
        assert len(p.history) == 2
        assert p.history[0]["reason"] == "跑 A"

    def test_history_capped(self):
        p = PresenceState(max_history=3)
        for _ in range(10):
            p.start()
            p.done()
        assert len(p.history) == 3

    def test_callback(self):
        calls = []
        p = PresenceState(on_change=lambda o, n, e, r: calls.append(
            (o, n, e)))
        p.start("x")
        assert calls == [("idle", "running", "start")]

    def test_callback_exception_swallowed(self):
        """★ UI 回调抛异常不能影响状态机。"""
        p = PresenceState(on_change=lambda *a: 1 / 0)
        assert p.start() == "running"

    def test_set_finds_path(self):
        p = PresenceState()
        assert p.set("running") == "running"
        assert p.set("needs_human") == "needs_human"

    def test_set_from_error_via_reset(self):
        """★ set 会自己找合法路径（error → idle 要经 reset）。"""
        p = PresenceState()
        p.error()
        assert p.set("idle") == "idle"

    def test_injectable_clock(self):
        ticks = [100.0]
        p = PresenceState(clock=lambda: ticks[0])
        ticks[0] = 105.0
        assert abs(p.elapsed - 5.0) < 1e-6

    def test_to_dict_json_safe(self):
        assert json.dumps(PresenceState().to_dict(), ensure_ascii=False)


# ==========================================================================
# 3. 菜单构造
# ==========================================================================
class TestMenu:
    def test_five_items_two_separators(self):
        real = [i for i in MENU.MENU_ITEMS if not i.separator]
        seps = [i for i in MENU.MENU_ITEMS if i.separator]
        assert len(real) == 5 and len(seps) == 2

    def test_commands_present(self):
        cmds = {i.cmd for i in MENU.MENU_ITEMS if not i.separator}
        assert cmds == {"start", "status", "pause", "intervene", "quit"}

    def test_quit_last(self):
        """★ 危险操作放最后 —— 防误点。"""
        real = [i for i in MENU.MENU_ITEMS if not i.separator]
        assert real[-1].cmd == "quit"

    def test_start_first(self):
        real = [i for i in MENU.MENU_ITEMS if not i.separator]
        assert real[0].cmd == "start"

    def test_ids_unique_nonzero(self):
        ids = [i.id for i in MENU.MENU_ITEMS if not i.separator]
        assert all(i > 1000 for i in ids)
        assert len(set(ids)) == len(ids)

    def test_separator_has_no_id(self):
        assert all(i.id == 0 for i in MENU.MENU_ITEMS if i.separator)

    def test_pause_grayed_when_idle(self):
        """★ 没在跑时"暂停"应该是灰的。"""
        items = MENU.build_menu_items(running=False)
        pause = [i for i in items if i.cmd == "pause"][0]
        assert pause.enabled is False

    def test_pause_enabled_when_running(self):
        items = MENU.build_menu_items(running=True)
        pause = [i for i in items if i.cmd == "pause"][0]
        assert pause.enabled is True

    def test_paused_label(self):
        items = MENU.build_menu_items(running=True, paused=True)
        pause = [i for i in items if i.cmd == "pause"][0]
        assert pause.label == "已暂停" and pause.enabled is False

    def test_build_is_pure(self):
        """★★ build_menu_items 不能改全局 MENU_ITEMS。"""
        before = [(i.cmd, i.label, i.enabled) for i in MENU.MENU_ITEMS]
        MENU.build_menu_items(running=True, paused=True)
        after = [(i.cmd, i.label, i.enabled) for i in MENU.MENU_ITEMS]
        assert before == after

    def test_flags(self):
        assert MENU.TPM_RETURNCMD == 0x0100
        assert MENU.MF_SEPARATOR == 0x00000800

    def test_to_dict(self):
        assert json.dumps(MENU.MENU_ITEMS[0].to_dict(),
                          ensure_ascii=False)


# ==========================================================================
# 4. 画圆
# ==========================================================================
class TestCircle:
    def test_length(self):
        assert len(BALL.make_circle_bgra(32, (1, 2, 3))) == 32 * 32 * 4

    def test_center_opaque(self):
        px = BALL.make_circle_bgra(32, (60, 200, 90))
        c = (16 * 32 + 16) * 4
        assert px[c + 3] > 200

    def test_center_bgra_order(self):
        """★ BGRA：蓝在前。"""
        px = BALL.make_circle_bgra(32, (60, 200, 90))
        c = (16 * 32 + 16) * 4
        assert (px[c], px[c + 1], px[c + 2]) == (90, 200, 60)

    def test_corners_transparent(self):
        px = BALL.make_circle_bgra(32, (60, 200, 90))
        assert px[3] == 0
        assert px[(31 * 32 + 31) * 4 + 3] == 0

    def test_antialiasing(self):
        """★ 边缘有 alpha 渐变（抗锯齿）。

        ⚠ 用 list 不用 set —— set 会把左右两侧相同值的像素去重。
        """
        px = BALL.make_circle_bgra(32, (60, 200, 90))
        alphas = [px[(16 * 32 + x) * 4 + 3] for x in range(32)]
        mid = [a for a in alphas if 0 < a < 235]
        assert len(mid) >= 2, alphas

    def test_ring_toggle(self):
        a = BALL.make_circle_bgra(32, (60, 200, 90), ring=True)
        b = BALL.make_circle_bgra(32, (60, 200, 90), ring=False)
        assert a != b

    def test_min_size_clamped(self):
        assert len(BALL.make_circle_bgra(1, (1, 2, 3))) == 4 * 4 * 4

    def test_alpha_clamped(self):
        assert len(BALL.make_circle_bgra(16, (1, 2, 3), alpha=999)) \
            == 16 * 16 * 4


# ==========================================================================
# 5. 窗口对象（不真建）
# ==========================================================================
class TestBallWindow:
    def test_not_created_by_default(self):
        assert BALL.BallWindow().created is False

    def test_show_without_create(self):
        assert BALL.BallWindow().show() is False

    def test_move_without_create(self):
        assert BALL.BallWindow().move(1, 2) is False

    def test_click_through_without_create(self):
        assert BALL.BallWindow().set_click_through(True) is False

    def test_destroy_idempotent(self):
        b = BALL.BallWindow()
        b.destroy()
        b.destroy()

    def test_color_follows_state(self):
        b = BALL.BallWindow()
        assert b.color == STATE_COLORS[AgentState.IDLE]
        b.presence.start()
        assert b.color == STATE_COLORS[AgentState.RUNNING]
        b.presence.need_human()
        assert b.color == STATE_COLORS[AgentState.NEEDS_HUMAN]

    def test_set_state_illegal_returns_false(self):
        """★ UI 不能因为状态机拒绝就崩。"""
        b = BALL.BallWindow()
        b.set_state("running")
        assert b.set_state("乱写") is False
        assert b.presence.state == "running"
        assert b.error

    def test_nchittest_returns_caption(self):
        """★ 拖拽靠 WM_NCHITTEST 返回 HTCAPTION。"""
        b = BALL.BallWindow()
        assert b._wnd_proc(0, BALL.WM_NCHITTEST, 0, 0) == BALL.HTCAPTION

    def test_unknown_message_safe(self):
        assert BALL.BallWindow()._wnd_proc(0, 0x9999, 0, 0) is not None \
            or True

    def test_alpha_constant_when_idle(self):
        b = BALL.BallWindow()
        assert b._current_alpha() == 235

    def test_constants(self):
        assert BALL.WS_EX_LAYERED == 0x00080000
        assert BALL.WS_EX_TRANSPARENT == 0x00000020
        assert BALL.WS_EX_TOOLWINDOW == 0x00000080
        assert BALL.HWND_TOPMOST == -1
        assert BALL.ULW_ALPHA == 0x2


# ==========================================================================
# 6. 通信桥
# ==========================================================================
def _mk_bridge(reply: str = ""):
    """造一个"不真起子进程"的桥。"""
    br = BR.McpBridge()
    br.sent_msgs = []
    br._write = lambda obj: (br.sent_msgs.append(obj), True)[-1]
    br._read_line = lambda to: reply
    return br


class TestBridge:
    def test_not_started(self):
        br = BR.McpBridge()
        assert br.started is False and br.alive is False

    def test_request_without_start_fails(self):
        assert BR.McpBridge().request("ping").ok is False

    def test_stop_idempotent(self):
        br = BR.McpBridge()
        br.stop()
        br.stop()

    def test_request_shape(self):
        br = _mk_bridge(json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "result": {"a": 1}}))
        r = br.request("ping", {"x": 1})
        msg = br.sent_msgs[-1]
        assert msg["jsonrpc"] == "2.0"
        assert msg["method"] == "ping"
        assert msg["params"] == {"x": 1}
        assert isinstance(msg["id"], int)
        assert r.ok and r.result == {"a": 1}

    def test_notify_has_no_id(self):
        br = _mk_bridge()
        br.notify("notifications/initialized")
        assert "id" not in br.sent_msgs[-1]

    def test_error_response(self):
        br = _mk_bridge(json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "error": {"code": -32601,
                                              "message": "没有"}}))
        r = br.request("x")
        assert r.ok is False and r.code == -32601

    def test_bad_json(self):
        assert _mk_bridge("{坏的").request("x").ok is False

    def test_empty_response(self):
        assert _mk_bridge("").request("x").ok is False

    def test_write_failure(self):
        br = BR.McpBridge()
        br._write = lambda obj: False
        assert br.request("x").ok is False

    def test_timeout(self):
        br = BR.McpBridge()

        def _boom(to):
            raise BR.BridgeError("读响应超时", retryable=True)

        br._write = lambda obj: True
        br._read_line = _boom
        r = br.request("x")
        assert r.ok is False and "超时" in r.error

    def test_call_tool_text(self):
        br = _mk_bridge(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": '{"a":1}'}],
                       "isError": False}}))
        ok, text = br.call_tool_text("x")
        assert ok is True and text == '{"a":1}'

    def test_call_tool_json(self):
        br = _mk_bridge(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": '{"a":1}'}],
                       "isError": False}}))
        ok, data = br.call_tool_json("x")
        assert ok and data == {"a": 1}

    def test_iserror_is_business_failure(self):
        """★★ isError=True 是**业务失败**，不是通信失败。"""
        br = _mk_bridge(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "✗ 缺参数"}],
                       "isError": True}}))
        ok, text = br.call_tool_text("x")
        assert ok is False and "缺参数" in text

    def test_json_parse_fallback(self):
        br = _mk_bridge(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "非 JSON"}],
                       "isError": False}}))
        ok, data = br.call_tool_json("x")
        assert ok is True and data == "非 JSON"

    def test_default_timeout_long(self):
        assert BR.DEFAULT_TIMEOUT >= 600


# ==========================================================================
# 7. 三条硬约束
# ==========================================================================
class TestHardConstraints:
    """★★ 阶段九的硬约束 1 / 2 / 3。"""

    @pytest.mark.parametrize("mod", ["state", "ball", "menu", "bridge",
                                     "__init__", "__main__"])
    def test_no_avatar_import(self, mod):
        """★★ 硬约束 2：不 import avatar（只走 stdio）。"""
        import ast
        p = _SHELL_ROOT / "avatar_shell" / ("%s.py" % mod)
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                if n.level == 0:      # 只查绝对导入
                    mods.add((n.module or "").split(".")[0])
        assert "avatar" not in mods, (mod, sorted(mods))

    @pytest.mark.parametrize("mod", ["state", "ball", "menu", "bridge"])
    def test_no_gui_framework(self, mod):
        """★★ 硬约束 1：不用任何第三方 UI 框架。"""
        import ast
        BANNED = {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                  "wx", "kivy", "pygame", "customtkinter"}
        p = _SHELL_ROOT / "avatar_shell" / ("%s.py" % mod)
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & BANNED), (mod, sorted(mods & BANNED))

    def test_bridge_uses_subprocess(self):
        """★★ 硬约束 3：独立进程 —— 靠 subprocess 拉起主包。"""
        import ast
        p = _SHELL_ROOT / "avatar_shell" / "bridge.py"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
        assert "subprocess" in mods

    def test_all_modules_have_docstring(self):
        """★ 硬约束 9：每个 .py 都有模块级 docstring。"""
        import ast
        for p in sorted((_SHELL_ROOT / "avatar_shell").glob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            doc = ast.get_docstring(tree) or ""
            assert len(doc) > 50, p.name
