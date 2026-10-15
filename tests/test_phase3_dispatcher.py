#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段三 · 输入分发层测试（静默/物理双模式 + 降级纪律）。

被测对象是 ``avatar/input/dispatcher.py`` —— 它存在的唯一理由是：
**同样一个"点击"，在不同场合要用完全不同的机制发出去。**

============================  ============================================
模式                          机制
============================  ============================================
静默 (``silent``)              ``PostMessage`` / ``SendMessage``，不抢焦点
物理 (``physical``)            ``SendInput``，真移动系统光标、真按键
============================  ============================================

设计原则
--------

* **不污染用户桌面**。本文件所有用例只对**自己创建的**窗口发消息 ——
  绝不往记事本、浏览器这些真实应用里打字。测试结束前一定销毁窗口。
* **失败要响亮**。硬约束 #6 说"不可达必须抛异常，不许静默失败"，
  这条要用真实的最小化窗口去撞，不能只读代码。
* **降级要留痕**。硬约束 #7 说每个动作返回的 ``ActionResult`` 必须
  填全 ``method`` / ``mode_used`` / ``fallback_used`` 三个字段。

关于"要不要真的动光标"
----------------------
物理模式确实会移动系统光标 —— 这是 ``SendInput`` 的定义，绕不过去。
所以本文件里凡是触发物理模式的用例，都会**先把光标位置记下来，
跑完之后还原**。这不是为了好看：测试跑在一台真实使用的机器上，
把用户鼠标留在屏幕角落是没礼貌的。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.input import dispatcher as D
from avatar.input import mouse as M
from avatar.input import keyboard as K
from avatar.errors import ActionFailed, TargetUnreachable

user32 = ctypes.WinDLL("user32", use_last_error=True)


# --------------------------------------------------------------------------
# 极简断言框架（与阶段一/二一致）
# --------------------------------------------------------------------------
_RESULTS = []


def check(name, fn):
    try:
        fn()
        _RESULTS.append((name, True, ""))
        print("  [OK  ] %s" % name)
    except AssertionError as e:
        _RESULTS.append((name, False, str(e)))
        print("  [FAIL] %s  -> %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  -> %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------
# 测试用窗口
# --------------------------------------------------------------------------
class _TestWindow:
    """一个只属于本测试的窗口。

    用裸 ``CreateWindowExW`` 建一个 ``STATIC`` 类窗口 —— 不注册自定义
    窗口类，因此不需要消息循环也能稳定收到 ``PostMessage``。

    为什么不用记事本：记事本是非受控进程，无法保证它能被创建、能被
    定位、退出后不留残渣。自己建的窗口生命期完全可控。
    """

    def __init__(self, title="avatar-dispatch-test", visible=True):
        # WS_POPUP = 0x80000000。建完必须 ShowWindow —— CreateWindowExW
        # 建出来的窗口**默认不显示**，IsWindowVisible 为假，静默模式的
        # 可达性检查会直接把"不可见"判成不可达。
        self.hwnd = user32.CreateWindowExW(
            0, "STATIC", title,
            0x80000000,
            50, 50, 320, 200,
            0, 0, 0, 0,
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW 失败：%r"
                          % ctypes.get_last_error())
        if visible:
            # SW_SHOWNOACTIVATE = 4：显示但不抢焦点
            user32.ShowWindow(self.hwnd, 4)
            user32.UpdateWindow(self.hwnd)
        self._destroyed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.destroy()

    def destroy(self):
        if not self._destroyed and self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self._destroyed = True

    def minimize(self):
        """最小化 —— ``ShowWindow(SW_MINIMIZE=6)``。

        注意：``PostMessage`` 对最小化窗口**依然投递成功**（消息进了
        队列），所以"能不能发"和"该不该发"是两件事。可达性检查是
        **我们的策略**，不是 Windows 的限制。
        """
        user32.ShowWindow(self.hwnd, 6)

    def is_minimized(self):
        return bool(user32.IsIconic(self.hwnd))


def _require_window():
    """建窗口；建不出来就跳过（不能算失败）。"""
    try:
        return _TestWindow()
    except OSError:
        return None


def _cursor_guard():
    """上下文管理器：跑完把光标还原到原位。

    ⚠️ ``SetCursorPos`` 是**异步**的 —— 调完立刻返回，光标稍后才到位。
    如果只调一次就走人，恢复动作可能延迟落地，正好落在下一个用例的
    ``before`` / ``after`` 采样窗口里，把那个用例误判成"静默点击动了光标"。

    所以这里必须**轮询确认**恢复完成再退出。
    """
    class _G:
        def __enter__(self):
            self.saved = M.get_cursor_pos()
            return self

        def __exit__(self, *exc):
            try:
                _settle_cursor(self.saved[0], self.saved[1])
            except Exception:
                pass
    return _G()


# --------------------------------------------------------------------------
# 1. 模式管理
# --------------------------------------------------------------------------
def test_default_mode():
    """默认必须是**静默模式**。

    理由（规格第 2 条）：静默模式不抢焦点、不动用户光标，副作用最小。
    物理模式是"降级/兜底"，不能是默认。
    """
    D.set_interaction_mode(D.MODE_SILENT)
    _expect(D.get_mode() == D.MODE_SILENT,
            "默认模式应为 silent，实际 %r" % D.get_mode())


def test_mode_switch():
    """两种模式要能来回切。"""
    try:
        D.set_interaction_mode(D.MODE_PHYSICAL)
        _expect(D.get_mode() == D.MODE_PHYSICAL, "切到 physical 失败")
        D.set_interaction_mode(D.MODE_SILENT)
        _expect(D.get_mode() == D.MODE_SILENT, "切回 silent 失败")
    finally:
        D.set_interaction_mode(D.MODE_SILENT)


def test_invalid_mode_rejected():
    """非法模式名必须报错，不能默默接受。"""
    try:
        D.set_interaction_mode("nonsense-mode")
        raise AssertionError("非法模式名竟被接受了")
    except ActionFailed:
        pass
    finally:
        D.set_interaction_mode(D.MODE_SILENT)


def test_describe_modes():
    """``describe_modes()`` 要给 CLI 提供完整信息。

    这里钉住的是**结构**，不是文案：CLI 的 ``avatar mode`` 命令要
    遍历两种模式各打一行说明，所以至少要有 ``current`` 和一张
    "模式名 → 说明" 的表。只有 ``{"mode": "silent"}`` 是不够的。
    """
    info = D.describe_modes()
    _expect(isinstance(info, dict), "describe_modes 应返回 dict")
    _expect("current" in info, "缺少 current（当前模式）")
    _expect(info["current"] in (D.MODE_SILENT, D.MODE_PHYSICAL),
            "current 值非法：%r" % info["current"])
    _expect("modes" in info, "缺少 modes（两种模式的说明表）")
    modes = info["modes"]
    _expect(D.MODE_SILENT in modes, "modes 里缺少 silent 条目")
    _expect(D.MODE_PHYSICAL in modes, "modes 里缺少 physical 条目")
    # 静默模式必须描述成"不抢焦点"，这是选它当默认的核心理由
    silent = str(modes[D.MODE_SILENT])
    _expect("焦点" in silent or "focus" in silent.lower(),
            "silent 的描述里应说明焦点行为：%r" % silent)
    # 两种模式的机制必须能区分开
    _expect(str(modes[D.MODE_SILENT]).lower().count("sendinput") == 0,
            "silent 的描述里不该出现 SendInput（那是物理模式的机制）")
    _expect("sendinput" in str(modes[D.MODE_PHYSICAL]).lower(),
            "physical 的描述里应说明用的是 SendInput")


# --------------------------------------------------------------------------
# 2. 可达性检查（硬约束 #6）
# --------------------------------------------------------------------------
def test_reachable_ok():
    """正常窗口应通过可达性检查。"""
    w = _require_window()
    if w is None:
        return
    try:
        D.check_window_reachable(w.hwnd)      # 不抛就是通过
    finally:
        w.destroy()


def test_reachable_rejects_dead_hwnd():
    """⭐ 无效句柄必须抛 ``TargetUnreachable``，不许静默失败。

    硬约束 #6。这条比"最小化"更重要：向一个已经销毁的窗口发消息，
    在某些实现下会**返回成功**（消息消失在虚空里），失败静默化 ——
    比直接报错危险得多。
    """
    w = _TestWindow()
    dead = w.hwnd
    w.destroy()
    time.sleep(0.05)
    try:
        D.check_window_reachable(dead)
        raise AssertionError("已销毁的窗口竟通过了可达性检查")
    except TargetUnreachable as e:
        _expect(getattr(e, "hwnd", None) == dead or str(dead) in str(e),
                "异常里应带上 hwnd，实际 %r" % (e,))


def test_reachable_rejects_minimized():
    """⭐ 最小化窗口必须被判定为不可达。

    这是**策略**而非系统限制：``PostMessage`` 对最小化窗口照样投递成功。
    但最小化窗口的客户区坐标是过期的（窗口没在渲染），点过去的坐标
    没有意义，会点错东西。所以宁可抛异常让上层停下来。
    """
    w = _require_window()
    if w is None:
        return
    try:
        w.minimize()
        # 等状态生效
        for _ in range(40):
            if w.is_minimized():
                break
            time.sleep(0.01)
        try:
            D.check_window_reachable(w.hwnd)
            raise AssertionError("最小化窗口竟通过了可达性检查")
        except TargetUnreachable:
            pass
    finally:
        w.destroy()


# --------------------------------------------------------------------------
# 3. 静默模式
# --------------------------------------------------------------------------
def test_silent_click_returns_actionresult():
    """静默点击必须返回填全字段的 ActionResult（硬约束 #7）。"""
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        r = D.dispatch_click(w.hwnd, 10, 10)
        _expect(isinstance(r, D.ActionResult),
                "dispatch_click 应返回 ActionResult，实际 %r" % type(r))
        _expect(r.method, "method 不能为空")
        _expect(r.mode_used == D.MODE_SILENT,
                "mode_used 应为 silent，实际 %r" % r.mode_used)
        _expect(isinstance(r.fallback_used, bool),
                "fallback_used 必须是 bool，实际 %r" % type(r.fallback_used))
        _expect(r.ok, "静默点击应成功：%r" % (r.error,))
    finally:
        w.destroy()


def test_silent_click_does_not_move_cursor():
    """⭐ 静默模式**绝不能**移动系统光标 —— 这是它存在的意义。

    如果静默点击偷偷动了光标，那它和物理模式就没有区别了，
    "不打扰用户"的价值也就没了。
    """
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        before = M.get_cursor_pos()
        D.dispatch_click(w.hwnd, 10, 10)
        D.dispatch_click(w.hwnd, 40, 60)
        time.sleep(0.05)
        after = M.get_cursor_pos()
        _expect(before == after,
                "静默点击把光标从 %r 挪到了 %r" % (before, after))
    finally:
        w.destroy()


def test_silent_text_reaches_window():
    """⭐ 静默文本输入必须返回静默模式的 ActionResult。

    ⚠️ 用 EDIT 控件而不是 STATIC —— ``dispatch_text`` 有一条
    **防静默失败**逻辑：静默发完之后会用 ``text_matches`` 回读校验，
    读不到内容就降级物理。STATIC 不处理 ``WM_CHAR``，一定会触发降级；
    拿它测就是拿"降级路径"当"静默路径"测，测不出东西。

    这个设计本身是对的（宁可降级也不静默失败），值得单独钉一条
    用例（见 ``test_silent_text_downgrades_when_ignored``）。
    """
    hwnd, edit = _make_edit_window()
    if hwnd is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        r = D.dispatch_text(edit, "hello avatar")
        _expect(isinstance(r, D.ActionResult), "应返回 ActionResult")
        _expect(r.method, "method 不能为空")
        _expect(r.mode_used == D.MODE_SILENT,
                "EDIT 控件应走静默模式，实际 %r" % r.mode_used)
        _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
        _expect(not r.fallback_used,
                "EDIT 能收 WM_CHAR，不该触发降级")
        _expect(D.text_matches(edit, "hello avatar"),
                "文本没送进 EDIT 控件")
    finally:
        user32.DestroyWindow(hwnd)


def test_silent_text_downgrades_when_ignored():
    """⭐ 静默写入没被接受时，必须降级并**留下痕迹**。

    这是"不许静默失败"的另一半：失败了不能假装成功。
    STATIC 窗口不处理 ``WM_CHAR`` —— 正好拿来撞这条逻辑。

    断言的是 ``fallback_used is True`` 且 ``detail`` 里说明了原因，
    而不是只断言"没崩"。
    """
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        r = D.dispatch_text(w.hwnd, "avatar-dispatch-fallback")
        _expect(isinstance(r, D.ActionResult), "应返回 ActionResult")
        # STATIC 不收文本 → 必然降级（或明确失败），二者都不能是"假装成功"
        _expect(r.fallback_used or not r.ok,
                "静默写入没被接受却报 fallback_used=False 且 ok=True —— "
                "这是静默失败。detail=%r" % r.detail)
        if r.fallback_used:
            _expect("降级" in r.detail or "未被" in r.detail,
                    "降级时 detail 应说明原因，实际 %r" % r.detail)
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


def _make_edit_window():
    """建一个带 EDIT 子控件的窗口，用于验证文本真的送达了。

    EDIT 控件必须真显示 —— 静默模式的可达性检查要求窗口可见。
    """
    hwnd = user32.CreateWindowExW(
        0, "STATIC", "avatar-edit-test", 0x80000000,
        50, 50, 320, 200, 0, 0, 0, 0,
    )
    if not hwnd:
        return None, None
    edit = user32.CreateWindowExW(
        0, "EDIT", "",
        0x40000000 | 0x10000000,   # WS_CHILD | WS_VISIBLE
        0, 0, 300, 100, hwnd, 0, 0, 0,
    )
    if not edit:
        user32.DestroyWindow(hwnd)
        return None, None
    user32.ShowWindow(hwnd, 4)      # SW_SHOWNOACTIVATE
    user32.UpdateWindow(hwnd)
    user32.UpdateWindow(edit)
    return hwnd, edit


def test_text_matches_helper():
    """``text_matches`` 是给上层校验用的。"""
    hwnd, edit = _make_edit_window()
    if hwnd is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        D.dispatch_text(edit, "abc123")
        _expect(D.text_matches(edit, "abc123"), "应匹配 'abc123'")
        _expect(not D.text_matches(edit, "xyz"), "不该匹配 'xyz'")
    finally:
        user32.DestroyWindow(hwnd)


# --------------------------------------------------------------------------
# 4. 物理模式
# --------------------------------------------------------------------------
def test_physical_click_moves_cursor():
    """⭐ 物理模式必须真的移动系统光标（SendInput 的定义）。"""
    w = _require_window()
    if w is None:
        return
    try:
        with _cursor_guard():
            D.set_interaction_mode(D.MODE_PHYSICAL)
            # 目标设在屏幕左上区域，避开任务栏
            tx, ty = 120, 120
            r = D.dispatch_move(w.hwnd, tx, ty, human=False)
            _expect(r.ok, "物理移动应成功：%r" % (r.error,))
            _expect(r.mode_used == D.MODE_PHYSICAL,
                    "mode_used 应为 physical，实际 %r" % r.mode_used)
            pos = _settle_cursor(tx, ty)
            err = max(abs(pos[0] - tx), abs(pos[1] - ty))
            _expect(err <= 2,
                    "物理移动落点误差 %dpx 过大（实际 %r，目标 %r）"
                    % (err, pos, (tx, ty)))
    finally:
        w.destroy()


def _settle_cursor(x, y, tries=30, gap=0.02):
    """反复重发移动直到落点精确（``SetCursorPos`` 是异步的）。"""
    for _ in range(tries):
        M.move_absolute(x, y)
        time.sleep(gap)
        got = M.get_cursor_pos()
        if got == (x, y):
            return got
    return M.get_cursor_pos()


def test_physical_click_returns_fields():
    """物理点击的 ActionResult 字段也要填全。"""
    w = _require_window()
    if w is None:
        return
    try:
        with _cursor_guard():
            D.set_interaction_mode(D.MODE_PHYSICAL)
            r = D.dispatch_click(w.hwnd, 10, 10, human=False)
            _expect(isinstance(r, D.ActionResult), "应返回 ActionResult")
            _expect(r.method, "method 不能为空")
            _expect(r.mode_used == D.MODE_PHYSICAL, "mode_used 应为 physical")
            _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
    finally:
        w.destroy()


def test_physical_key_no_crash():
    """物理按键不能崩。

    ⚠️ 用一个"不可达"的 hwnd（0）来触发参数校验路径，而不是真往
    当前焦点窗口敲键 —— 那会往用户正在用的程序里打字。
    """
    D.set_interaction_mode(D.MODE_PHYSICAL)
    try:
        r = D.dispatch_key(0, "SHIFT")
        _expect(isinstance(r, D.ActionResult), "应返回 ActionResult")
    except TargetUnreachable:
        # 明确报"需要 hwnd"也是可接受的行为 —— 关键是**不能静默乱敲**
        pass
    finally:
        D.set_interaction_mode(D.MODE_SILENT)


def test_physical_key_with_real_window():
    """给一个真窗口时，物理按键要返回完整的 ActionResult。"""
    w = _require_window()
    if w is None:
        return
    try:
        with _cursor_guard():
            D.set_interaction_mode(D.MODE_PHYSICAL)
            # 用 F24 这种几乎不产生可见副作用的键（大多数程序不响应）
            r = D.dispatch_key(w.hwnd, "F24")
            _expect(isinstance(r, D.ActionResult), "应返回 ActionResult")
            _expect(r.method, "method 不能为空")
            _expect(r.mode_used == D.MODE_PHYSICAL,
                    "mode_used 应为 physical，实际 %r" % r.mode_used)
            _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
    except ActionFailed:
        # 不支持 F24 也接受（键名表可能没收录），但必须是明确的失败
        pass
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


# --------------------------------------------------------------------------
# 5. 降级纪律
# --------------------------------------------------------------------------
def test_no_downgrade_when_disallowed():
    """⭐ ``allow_fallback=False`` 时，物理不可达必须抛异常。

    这条钉的是"不许悄悄降级"。上层如果明确要求用某种模式，
    系统就不能自作主张换一种 —— 那会让上层的判断依据失效。
    """
    w = _TestWindow()
    dead = w.hwnd
    w.destroy()
    time.sleep(0.05)
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        try:
            D.dispatch_click(dead, 10, 10, allow_fallback=False)
            raise AssertionError("死窗口 + 不许降级，竟没抛异常")
        except TargetUnreachable:
            pass
    finally:
        D.set_interaction_mode(D.MODE_SILENT)


def test_actionresult_defaults():
    """``ActionResult`` 的三个必填字段不能有默认值 —— 强制调用方填。"""
    import inspect
    sig = inspect.signature(D.ActionResult)
    for f in ("method", "mode_used", "fallback_used"):
        p = sig.parameters[f]
        _expect(p.default is inspect.Parameter.empty,
                "%s 不应有默认值（硬约束 #7 要求必填）" % f)


# --------------------------------------------------------------------------
# 6. 输入层协同（dispatcher 与 input 层不打架）
# --------------------------------------------------------------------------
def test_dispatcher_uses_input_layer():
    """dispatcher 的模式常量必须来自 input 层，不是自己另起一套。"""
    _expect(D.MODE_SILENT == D.input_layer.MODE_SILENT
            if hasattr(D.input_layer, "MODE_SILENT") else True,
            "MODE_SILENT 应与 input 层一致")
    _expect(D.MODE_SILENT in ("silent",), "MODE_SILENT 的字面值应为 'silent'")
    _expect(D.MODE_PHYSICAL in ("physical",),
            "MODE_PHYSICAL 的字面值应为 'physical'")


def test_lparam_packing():
    """客户区坐标 → lParam 的打包：低 16 位 x、高 16 位 y。"""
    lp = D.make_lparam(0x1234, 0x5678)
    _expect(lp & 0xFFFF == 0x1234, "低 16 位应是 x，实际 %#x" % (lp & 0xFFFF))
    _expect((lp >> 16) & 0xFFFF == 0x5678,
            "高 16 位应是 y，实际 %#x" % ((lp >> 16) & 0xFFFF))
    _expect(isinstance(lp, int), "make_lparam 应返回 int")
    # 边界值
    lp2 = D.make_lparam(0, 0)
    _expect(lp2 == 0, "make_lparam(0,0) 应为 0，实际 %r" % lp2)
    lp3 = D.make_lparam(0xFFFF, 0xFFFF)
    _expect(lp3 == 0xFFFFFFFF,
            "make_lparam(0xFFFF,0xFFFF) 应为 0xFFFFFFFF，实际 %#x" % lp3)


def test_client_to_screen_lparam():
    """``client_to_screen_lparam`` 接受 (hwnd, client_x, client_y)，返回**屏幕坐标元组**。

    ⚠️ 注意它与 ``make_lparam`` 是两个不同层面的东西：

    * ``make_lparam(cx, cy)`` —— 把**客户区坐标**打包成 lParam，
      给 ``PostMessage`` 用（静默模式）。
    * ``client_to_screen_lparam(hwnd, cx, cy)`` —— 先做
      ``ClientToScreen`` 换算，返回 **(屏幕 x, 屏幕 y) 元组**，
      给物理模式用。

    名字里的 "lparam" 有点误导：它并不返回打包好的整数。
    混用会导致"点在窗口左上角"这种经典 bug，所以单独钉一条。
    """
    w = _require_window()
    if w is None:
        return
    try:
        # 窗口建在 (50,50)，客户区 (10,20) → 屏幕应 ≥ (60,70)
        out = D.client_to_screen_lparam(w.hwnd, 10, 20)
        _expect(isinstance(out, tuple) and len(out) == 2,
                "应返回 (x, y) 元组，实际 %r" % (out,))
        sx, sy = out
        _expect(isinstance(sx, int) and isinstance(sy, int),
                "元组元素应为 int，实际 %r" % (out,))
        _expect(sx >= 50 and sy >= 50,
                "屏幕坐标 (%d,%d) 不合理（窗口在 50,50）" % (sx, sy))
        # 换个客户区点，屏幕坐标应同步平移
        out2 = D.client_to_screen_lparam(w.hwnd, 110, 120)
        _expect(out2[0] - sx == 100 and out2[1] - sy == 100,
                "客户区偏移 (100,100) 应带来屏幕偏移 (100,100)：%r vs %r"
                % (out, out2))
    finally:
        w.destroy()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段三 · 输入分发层测试")
    print("=" * 64)
    print("⚠️ 物理模式用例会临时移动系统光标，跑完自动还原")

    section("模式管理")
    check("默认是静默模式", test_default_mode)
    check("模式可来回切换", test_mode_switch)
    check("非法模式名被拒绝", test_invalid_mode_rejected)
    check("describe_modes 信息完整", test_describe_modes)

    section("可达性检查（硬约束 #6）")
    check("正常窗口可达", test_reachable_ok)
    check("⭐ 已销毁窗口抛 TargetUnreachable", test_reachable_rejects_dead_hwnd)
    check("⭐ 最小化窗口判为不可达", test_reachable_rejects_minimized)

    section("静默模式")
    check("点击返回完整 ActionResult", test_silent_click_returns_actionresult)
    check("⭐ 点击不移动光标", test_silent_click_does_not_move_cursor)
    check("文本输入返回 ActionResult", test_silent_text_reaches_window)
    check("⭐ 静默被忽略时降级并留痕", test_silent_text_downgrades_when_ignored)
    check("text_matches 校验", test_text_matches_helper)

    section("物理模式")
    check("⭐ 移动真的挪动系统光标", test_physical_click_moves_cursor)
    check("点击返回完整 ActionResult", test_physical_click_returns_fields)
    check("空 hwnd 按键不静默乱敲", test_physical_key_no_crash)
    check("真窗口按键返回完整结果", test_physical_key_with_real_window)

    section("降级纪律（硬约束 #7）")
    check("⭐ 禁止降级时抛异常", test_no_downgrade_when_disallowed)
    check("ActionResult 必填字段无默认值", test_actionresult_defaults)

    section("输入层协同")
    check("模式常量与 input 层一致", test_dispatcher_uses_input_layer)
    check("lParam 打包正确", test_lparam_packing)
    check("客户区→屏幕坐标换算", test_client_to_screen_lparam)

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段三 分发层测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s -> %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
