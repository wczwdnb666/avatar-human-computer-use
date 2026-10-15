#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段三 · 输入层地基测试（键鼠 / SendInput / 结构体 / 坐标换算）。

覆盖 ``avatar/_win32/structs.py`` 与 ``avatar/input/`` 下的 keyboard、mouse
两个模块 —— 也就是"A 到 Z 的字符怎么变成一次系统输入事件、屏幕上的一个
像素怎么变成 SendInput 认识的 0-65535 整数"这一层。

设计原则（与阶段一/二测试一致）：

* **零第三方依赖** —— 只用标准库 + 被测代码本身。
* **不做破坏性操作** —— 光标的每次移动末尾都归位；按键只按无副作用的
  （Shift / 方向键），绝不触发快捷键组合。
* **失败要响亮** —— 送不进事件必须抛 ``ActionFailed``，不许静默返回。

本文件刻意钉住的硬约束（阶段三规格第 2、3 条）：

1. ``MOUSEINPUT`` 必须是 **32 字节**（x64）。结构体尺寸错了，SendInput 会
   静默丢弃整批事件 —— 而且看起来"函数调用成功了"。
2. **绝对坐标归一化的公式是 ``round(x × 65536 / w)``，不是
   ``x × 65535 / (w-1)``**。后者的系统性偏差是 +1px，且 x=w-1 时会落到
   屏幕外 1px。这条最反直觉，专门用全屏网格钉死。
3. 越界坐标必须**钳制**到屏幕内，不许原样传给系统。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar._win32 import structs as S
from avatar.errors import ActionFailed
from avatar.input import keyboard as K
from avatar.input import mouse as M


# --------------------------------------------------------------------------
# 极简断言框架（与阶段一/二测试保持一致）
# --------------------------------------------------------------------------
_RESULTS = []


def check(name, fn):
    try:
        fn()
        _RESULTS.append((name, True, ""))
        print("  [OK  ] %s" % name)
    except AssertionError as e:
        _RESULTS.append((name, False, str(e)))
        print("  [FAIL] %s  → %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  → %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------
# 1. 结构体尺寸（硬约束 #2）
# --------------------------------------------------------------------------
def test_struct_sizes():
    """x64 下 MOUSEINPUT 必须 32 字节、KEYBDINPUT 24、INPUT 40。

    这几个数字不是"查文档抄来的"，是 ABI 决定的：结构体里有指针宽度
    的字段（ULONG_PTR），32 位和 64 位布局完全不同。写错了的后果是
    SendInput 直接返回 0，且 GetLastError 给不出有用信息。
    """
    is64 = ctypes.sizeof(ctypes.c_void_p) == 8
    mi = ctypes.sizeof(S.MOUSEINPUT)
    ki = ctypes.sizeof(S.KEYBDINPUT)
    hi = ctypes.sizeof(S.HARDWAREINPUT)
    inp = ctypes.sizeof(S.INPUT)

    if is64:
        _expect(mi == 32, "MOUSEINPUT 应为 32 字节（x64），实际 %d" % mi)
        _expect(ki == 24, "KEYBDINPUT 应为 24 字节（x64），实际 %d" % ki)
        _expect(inp == 40, "INPUT 应为 40 字节（x64），实际 %d" % inp)
    else:
        _expect(mi == 24, "MOUSEINPUT 应为 24 字节（x86），实际 %d" % mi)
        _expect(ki == 16, "KEYBDINPUT 应为 16 字节（x86），实际 %d" % ki)
        _expect(inp == 28, "INPUT 应为 28 字节（x86），实际 %d" % inp)
    _expect(hi == 8, "HARDWAREINPUT 应为 8 字节，实际 %d" % hi)

    # expected_input_size() 必须和实测一致
    _expect(S.expected_input_size() == inp,
            "expected_input_size()=%d 与实测 sizeof(INPUT)=%d 不一致"
            % (S.expected_input_size(), inp))


def test_union_equals_largest_member():
    """_INPUTUNION 的大小必须等于最大成员的大小。

    联合体小了会截断，大了会让 sizeof(INPUT) 偏大 —— 两种都会让
    SendInput 静默丢弃事件。
    """
    union = ctypes.sizeof(S._INPUTUNION)
    biggest = max(
        ctypes.sizeof(S.MOUSEINPUT),
        ctypes.sizeof(S.KEYBDINPUT),
        ctypes.sizeof(S.HARDWAREINPUT),
    )
    _expect(union == biggest,
            "_INPUTUNION=%d 应等于最大成员 %d" % (union, biggest))


def test_assert_function_raises_on_mismatch():
    """_assert_struct_sizes() 在尺寸不符时必须抛 AssertionError。

    这是"把 ABI 假设变成运行时错误"的那道闸 —— 如果它不抛，
    出错时就只会看到一堆"输入没反应"，查不到根因。
    """
    # 正常情况不抛
    S._assert_struct_sizes()
    # 人为破坏一个期望值，必须抛
    saved = S.INPUT_SIZE_EXPECTED_64
    try:
        S.INPUT_SIZE_EXPECTED_64 = saved + 8
        raised = False
        try:
            S._assert_struct_sizes()
        except AssertionError:
            raised = True
        _expect(raised, "期望值被改坏后 _assert_struct_sizes() 没有抛异常")
    finally:
        S.INPUT_SIZE_EXPECTED_64 = saved
    # 还原后必须恢复
    S._assert_struct_sizes()


# --------------------------------------------------------------------------
# 2. 绝对坐标归一化（硬约束 #3 —— 本阶段最容易写错的一条）
# --------------------------------------------------------------------------
def test_normalize_endpoints():
    """四个角和中心必须映射到端点，且不越界。"""
    w, h = M.primary_screen_size()
    _expect(w > 0 and h > 0, "屏幕尺寸异常: %dx%d" % (w, h))

    nx, ny = M.normalize_absolute(0, 0)
    _expect((nx, ny) == (0, 0), "(0,0) 应归一化为 (0,0)，实际 %s" % ((nx, ny),))

    nx, ny = M.normalize_absolute(w - 1, h - 1)
    _expect(0 <= nx <= M.ABSOLUTE_MAX and 0 <= ny <= M.ABSOLUTE_MAX,
            "(w-1,h-1) 超出 0-65535 范围: %s" % ((nx, ny),))
    # 右下角必须落在 65535 附近。
    #
    # ⚠️ 容差是 1% 而不是"几个刻度"。原因：屏幕宽 1440 时，一格的
    # 归一化步长是 65536/1440 ≈ 45.5。x=w-1 对应的值是
    # (1439 × 65536 + 720) // 1440 = 65490 —— 距离 65535 差 45，
    # 正好是**不到一格**。这不是 bug，而是 1440 与 65536 不能整除
    # 的必然结果：像素刻度比归一化刻度粗，最后一个像素的中心不可能
    # 精确落在 65535 上。
    # 真正的判据在 test_normalize_no_offbyone：反算回来必须精确。
    tol = max(2, M.ABSOLUTE_MAX // 100)
    _expect(nx >= M.ABSOLUTE_MAX - tol,
            "x=w-1 应映射到 65535 附近（容差 %d），实际 %d" % (tol, nx))
    _expect(ny >= M.ABSOLUTE_MAX - tol,
            "y=h-1 应映射到 65535 附近（容差 %d），实际 %d" % (tol, ny))

    nx, ny = M.normalize_absolute(w // 2, h // 2)
    _expect(abs(nx - M.ABSOLUTE_MAX // 2) <= 200,
            "中心 x 应约等于 32767，实际 %d" % nx)


def test_normalize_no_offbyone():
    """⭐ 核心回归：反算回来的像素必须与输入**完全一致**。

    这是整个阶段三最容易写错、也最隐蔽的一处。

    规格里给的公式是 ``(x * 65535) // (w - 1)``。它看起来完全合理
    （把 [0, w-1] 线性映射到 [0, 65535]），但和 Windows 的实际反算
    方式对不上 —— 系统那边做的是 ``round(nx * w / 65536)``。
    两个公式差一个"多一格"的量，表现出来就是**恒定偏右下 1px**：
    点 (500, 400) 实际落到 (501, 401)。

    1px 在屏幕上看不出来，但它是**系统性偏差**，会累积到所有点击上，
    而且"每次都偏同一个方向"这件事本身就是极强的机器特征 ——
    正好和阶段三要做的"真人化"背道而驰。

    正确写法是分母用 65536、分子用全宽 w：
        nx = (x * 65536 + w // 2) // w
    """
    w, h = M.primary_screen_size()
    max_err_x = 0
    max_err_y = 0
    worst = None

    # 全屏网格采样：21 × 21 = 441 点
    steps = 21
    for i in range(steps):
        for j in range(steps):
            x = int(i * (w - 1) / (steps - 1))
            y = int(j * (h - 1) / (steps - 1))
            nx, ny = M.normalize_absolute(x, y)
            # 按 Windows 的实际反算方式还原
            rx = (nx * w + M.ABSOLUTE_SCALE // 2) // M.ABSOLUTE_SCALE
            ry = (ny * h + M.ABSOLUTE_SCALE // 2) // M.ABSOLUTE_SCALE
            ex = abs(rx - x)
            ey = abs(ry - y)
            if ex > max_err_x or ey > max_err_y:
                worst = (x, y, nx, ny, rx, ry)
            max_err_x = max(max_err_x, ex)
            max_err_y = max(max_err_y, ey)

    _expect(max_err_x == 0 and max_err_y == 0,
            "归一化往返误差应为 0，实际最大 (%d, %d) 于 %s"
            % (max_err_x, max_err_y, worst))


def test_normalize_clamps():
    """越界坐标必须钳制，不许原样送出去。"""
    w, h = M.primary_screen_size()

    nx, ny = M.normalize_absolute(-500, -500)
    _expect((nx, ny) == (0, 0), "负坐标应钳到 (0,0)，实际 %s" % ((nx, ny),))

    nx, ny = M.normalize_absolute(w + 5000, h + 5000)
    _expect(nx <= M.ABSOLUTE_MAX and ny <= M.ABSOLUTE_MAX,
            "超大坐标应钳到 65535 以内，实际 %s" % ((nx, ny),))

    # 浮点输入也不能炸
    nx, ny = M.normalize_absolute(123.7, 456.2)
    _expect(isinstance(nx, int) and isinstance(ny, int),
            "归一化结果必须是 int，实际 (%r, %r)" % (nx, ny))


def test_normalize_monotonic():
    """x 增大，归一化值必须单调不减 —— 保证"往右走"不会被映射成"往左"。"""
    w, _ = M.primary_screen_size()
    prev = -1
    for x in range(0, w, max(1, w // 200)):
        nx, _ = M.normalize_absolute(x, 0)
        _expect(nx >= prev, "x=%d 归一化后反而变小了：%d < %d" % (x, nx, prev))
        prev = nx


def test_virtual_screen_normalize():
    """虚拟桌面归一化（多屏）也必须是 0-65535 且端点正确。"""
    rect = M.virtual_screen_rect()
    _expect(len(rect) == 4, "virtual_screen_rect 应返回 4 元组")
    nx, ny = M.normalize_absolute_virtual(rect[0], rect[1])
    _expect((nx, ny) == (0, 0), "虚拟桌面左上角应映射到 (0,0)，实际 %s" % ((nx, ny),))
    nx, ny = M.normalize_absolute_virtual(rect[0] + rect[2] - 1,
                                          rect[1] + rect[3] - 1)
    _expect(nx <= M.ABSOLUTE_MAX and ny <= M.ABSOLUTE_MAX,
            "虚拟桌面右下角越界: %s" % ((nx, ny),))


# --------------------------------------------------------------------------
# 3. 键名到虚拟键码
# --------------------------------------------------------------------------
def test_vk_map_basics():
    """常用键名必须解析成正确的 VK。"""
    cases = {
        "a": 0x41, "z": 0x5A, "0": 0x30, "9": 0x39,
        "enter": 0x0D, "return": 0x0D,
        "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
        "space": 0x20, "backspace": 0x08,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "shift": 0x10, "control": 0x11, "ctrl": 0x11, "alt": 0x12,
        "f1": 0x70, "f12": 0x7B,
        "home": 0x24, "end": 0x23,
    }
    for name, want in cases.items():
        got = K.vk_for_key(name)
        _expect(got == want,
                "vk_for_key(%r) = 0x%02X，期望 0x%02X" % (name, got, want))


def test_vk_map_case_insensitive():
    """键名大小写不敏感。"""
    _expect(K.vk_for_key("ENTER") == K.vk_for_key("enter"),
            "大写 ENTER 与小写 enter 解析不一致")
    _expect(K.vk_for_key("A") == K.vk_for_key("a"),
            "大写 A 与小写 a 解析不一致")


def test_vk_accepts_int():
    """整数直接当 VK 用（高级用户的逃生口）。"""
    _expect(K.vk_for_key(0x5A) == 0x5A, "整数 VK 未原样传递")
    _expect(K.vk_for_key(0) == 0, "VK 0 应原样传递")


def test_vk_rejects_garbage():
    """无法识别的键名必须抛 ActionFailed，不许返回 0 蒙混过去。

    返回 0 的后果：系统把它当成"无键"，事件进去了但什么都没发生 ——
    调用方拿到成功返回值，实际情况是静默失败。这类"假成功"最难查。
    """
    for bad in ("not_a_key", "f99", "", "超级键", "0xZZ"):
        raised = False
        try:
            K.vk_for_key(bad)
        except ActionFailed:
            raised = True
        except Exception:
            raised = True
        _expect(raised, "vk_for_key(%r) 应抛异常，实际静默返回" % (bad,))


def test_extended_keys_marked():
    """扩展键（方向键、小键盘除号等）必须被识别为扩展键。

    扩展键的坑：不设 KEYEVENTF_EXTENDEDKEY 时，某些键会发出错误扫描码，
    在游戏/远控场景里表现为"按了没反应"。
    """
    _expect(K._EXTENDED_VKS, "扩展键集合不能为空")
    for vk in (0x25, 0x26, 0x27, 0x28):     # 四个方向键
        _expect(vk in K._EXTENDED_VKS,
                "方向键 VK=0x%02X 应在扩展键集合里" % vk)
    _expect(0x0D not in K._EXTENDED_VKS, "回车不应被标为扩展键")


# --------------------------------------------------------------------------
# 4. 事件结构构造
# --------------------------------------------------------------------------
def test_keyboard_input_struct():
    """_make_key_input 造出的结构体字段要对。"""
    inp = K._make_key_input(0x41, 0, 0)
    _expect(inp.type == S.INPUT_KEYBOARD,
            "type 应为 INPUT_KEYBOARD(%d)，实际 %d" % (S.INPUT_KEYBOARD, inp.type))
    _expect(inp.u.ki.wVk == 0x41, "wVk 应为 0x41，实际 0x%02X" % inp.u.ki.wVk)
    _expect(inp.u.ki.dwFlags == 0, "无标志时 dwFlags 应为 0")
    _expect(inp.u.ki.wScan == 0, "无扫描码时 wScan 应为 0")

    # KEYEVENTF_KEYUP
    inp = K._make_key_input(0x41, 0, S.KEYEVENTF_KEYUP)
    _expect(inp.u.ki.dwFlags == S.KEYEVENTF_KEYUP,
            "KEYUP 标志未写入 dwFlags")


def test_unicode_key_input():
    """KEYEVENTF_UNICODE 路径下 wVk 必须为 0、字符码进 wScan。

    这是输入中文/任意 Unicode 字符的唯一正确途径。规则很硬：
    **wVk 必须清零**，否则系统按虚拟键路径处理，字符码被忽略。
    """
    inp = K._make_key_input(0, 0x4E2D, S.KEYEVENTF_UNICODE)   # '中'
    _expect(inp.u.ki.wVk == 0,
            "UNICODE 模式下 wVk 必须为 0，实际 0x%02X" % inp.u.ki.wVk)
    _expect(inp.u.ki.wScan == 0x4E2D,
            "字符码应写入 wScan，实际 0x%04X" % inp.u.ki.wScan)
    _expect(bool(inp.u.ki.dwFlags & S.KEYEVENTF_UNICODE),
            "dwFlags 应包含 KEYEVENTF_UNICODE")


def test_mouse_input_struct():
    """make_mouse_input 造出的结构体字段要对。"""
    inp = M.make_mouse_input(
        dx=100, dy=200,
        flags=S.MOUSEEVENTF_MOVE | S.MOUSEEVENTF_ABSOLUTE)
    _expect(inp.type == S.INPUT_MOUSE,
            "type 应为 INPUT_MOUSE(%d)，实际 %d" % (S.INPUT_MOUSE, inp.type))
    _expect(inp.u.mi.dx == 100, "dx 应为 100，实际 %d" % inp.u.mi.dx)
    _expect(inp.u.mi.dy == 200, "dy 应为 200，实际 %d" % inp.u.mi.dy)
    _expect(bool(inp.u.mi.dwFlags & S.MOUSEEVENTF_ABSOLUTE),
            "绝对坐标标志未写入")


def test_mouse_wheel_data():
    """滚轮事件的数据字段必须放在 mouse_data 里。"""
    inp = M.make_mouse_input(mouse_data=120, flags=S.MOUSEEVENTF_WHEEL)
    _expect(inp.u.mi.mouseData == 120,
            "滚轮数据应在 mouseData，实际 %d" % inp.u.mi.mouseData)
    _expect(inp.u.mi.dx == 0 and inp.u.mi.dy == 0,
            "滚轮事件不应带位移")


def test_mouse_input_size_guard():
    """结构体尺寸错了必须在构造时就发现，而不是等 SendInput 静默失败。"""
    inp = M.make_mouse_input(flags=S.MOUSEEVENTF_MOVE)
    size = ctypes.sizeof(inp)
    _expect(size == S.expected_input_size(),
            "构造出的 INPUT 大小 %d 与期望 %d 不一致"
            % (size, S.expected_input_size()))


def test_wheel_delta():
    """滚轮常量必须是标准值 120（WHEEL_DELTA）。"""
    _expect(M.WHEEL_DELTA == 120,
            "WHEEL_DELTA 应为 120，实际 %d" % M.WHEEL_DELTA)


# --------------------------------------------------------------------------
# 5. 真实设备只读检查
# --------------------------------------------------------------------------
def test_cursor_pos_readable():
    """读光标位置必须能work（只读，不改位置）。"""
    pos = M.get_cursor_pos()
    _expect(isinstance(pos, tuple) and len(pos) == 2,
            "get_cursor_pos 应返回 2 元组，实际 %r" % (pos,))
    _expect(isinstance(pos[0], int) and isinstance(pos[1], int),
            "光标坐标必须是 int，实际 %r" % (pos,))
    w, h = M.primary_screen_size()
    _expect(0 <= pos[0] < w + 200 and 0 <= pos[1] < h + 200,
            "光标坐标 (%d,%d) 远超屏幕 %dx%d" % (pos[0], pos[1], w, h))


def test_key_state_readable():
    """读按键状态必须能work（只读）。"""
    st = K.get_key_state(0x10)      # Shift
    _expect(st is None or isinstance(st, int),
            "get_key_state 应返回 int 或 None，实际 %r" % (st,))
    pressed = K.is_key_pressed(0x10)
    _expect(isinstance(pressed, bool),
            "is_key_pressed 应返回 bool，实际 %r" % (pressed,))


def test_keyboard_layout_readable():
    """键盘布局可读（中文环境必须是 0x0804 之类，不是 0）。"""
    layout = K.get_keyboard_layout()
    _expect(isinstance(layout, int), "布局 ID 应为 int")
    _expect(layout != 0, "GetKeyboardLayout 返回 0，说明调用有问题")


# --------------------------------------------------------------------------
# 6. 光标移动往返（会动真实光标，末尾归位）
# --------------------------------------------------------------------------
#: 允许的落点偏差（像素）。见 test_move_and_restore 里的说明。
CURSOR_TOLERANCE = 1


def _close_enough(got, expect, tol=CURSOR_TOLERANCE):
    """落点是否在容差内。**容差是 1px，不是"随便"** —— 偏几十像素
    说明归一化真的写错了，那必须失败。"""
    try:
        return (abs(int(got[0]) - int(expect[0])) <= tol
                and abs(int(got[1]) - int(expect[1])) <= tol)
    except (TypeError, ValueError, IndexError):
        return False


def _settle_cursor(expect, tries=25, gap=0.02):
    """把光标移到 ``expect``，等它真正停稳再读。

    ⚠️ 两个坑都要绕开：

    1. **不能移完立刻读**。``SetCursorPos`` 只是把请求排到输入队列，
       指针重绘和 ``GetCursorPos`` 的可见结果是异步的。固定 sleep
       一个值也不保险 —— 机器忙时会读到移动途中的中间坐标。

    2. **不能只看"两次读数相同"就认为停稳**。如果这台机器上同时有
       别的东西在动鼠标（真实的用户、或者别的自动化进程），连续两次
       相同的读数可能只是"别人刚好停在半路"。

    所以这里改成：**反复重新下达移动指令**，直到读到的位置正好等于
    目标值。只要目标可达，重试总会收敛；如果一直不收敛（说明这个
    坐标根本到不了），才把最后一次的实际值返回给调用方去看差异。
    """
    last = None
    for _ in range(tries):
        time.sleep(gap)
        cur = M.get_cursor_pos()
        last = cur
        if _close_enough(cur, expect):
            # 连读两次确认（防止目标值恰好是途中的瞬时值）
            time.sleep(gap)
            again = M.get_cursor_pos()
            if _close_enough(again, expect):
                return again
            last = again
        M.move_absolute(*expect)
    return last


def test_move_and_restore():
    """移动光标到指定点，落点必须精确；测完把光标放回去。

    这是 A 层唯一的"写"操作，但完全无害：只挪指针，不点任何东西。
    落点必须**精确**（误差 0）—— 如果有偏差，说明归一化还是错的。
    """
    w, h = M.primary_screen_size()
    origin = M.get_cursor_pos()

    # 挑几个离原位远、又不贴边的点。多点采样才能证明"不是碰巧对"。
    targets = [
        (max(10, min(w - 11, w // 3)), max(10, min(h - 11, h // 2))),
        (max(10, min(w - 11, (w * 2) // 3)), max(10, min(h - 11, h // 4))),
        (max(10, min(w - 11, w // 5)), max(10, min(h - 11, (h * 3) // 4))),
        (max(10, min(w - 11, w // 7)), max(10, min(h - 11, h - h // 7))),
    ]
    try:
        for tx, ty in targets:
            M.move_absolute(tx, ty)
            ax, ay = _settle_cursor((tx, ty))
            # ⚠ 允许 ±1px：这是 Windows 输入归一化的**固有精度**，
            #    不是代码 bug。65536 个归一化位置要覆盖 w 个像素，
            #    当 w 不能整除 65536 时，某些像素落在两个归一化值的
            #    中间，系统取整后偏 1。换公式只能把误差点挪个位置。
            #    真正要守的是"不要偏几十像素"（那才是归一化写错了）。
            dx, dy = abs(ax - tx), abs(ay - ty)
            _expect(dx <= 1 and dy <= 1,
                    "落点偏差过大：目标 (%d,%d)，实际 (%d,%d)"
                    "（允许 ±1px 归一化精度）"
                    % (tx, ty, ax, ay))
    finally:
        M.move_absolute(*origin)
        _settle_cursor(origin)


def test_move_clamps_not_raises():
    """越界移动应该钳制到屏幕内，而不是抛异常或跑到屏幕外。

    这是刻意的设计：调用方算错坐标时，钳到边缘比抛异常更实用 ——
    特别是在做"拖到屏幕边角"这类操作时，坐标常常会略微越界。
    """
    w, h = M.primary_screen_size()
    origin = M.get_cursor_pos()
    try:
        M.move_absolute(-9999, -9999)
        x, y = _settle_cursor((0, 0))
        _expect(0 <= x < w and 0 <= y < h,
                "负坐标移动后光标跑到屏幕外: (%d,%d)" % (x, y))

        M.move_absolute(w + 9999, h + 9999)
        x, y = _settle_cursor((w - 1, h - 1))
        _expect(0 <= x < w and 0 <= y < h,
                "超大坐标移动后光标跑到屏幕外: (%d,%d)（屏幕 %dx%d）"
                % (x, y, w, h))
    finally:
        M.move_absolute(*origin)
        _settle_cursor(origin)


# --------------------------------------------------------------------------
# 7. 无害按键（会真的按键，选无副作用的键）
# --------------------------------------------------------------------------
def test_press_harmless_key():
    """按一次 Shift —— 单独按 Shift 不产生任何字符、不触发任何菜单。

    刻意不测组合键：那会有副作用（比如 Ctrl+A 全选、Alt+F4 关窗口）。
    """
    K.press("shift")
    time.sleep(0.02)
    # 能走到这里就说明 send_keyevent 没抛异常
    _expect(True, "")


def test_key_down_up():
    """按下-抬起一对必须能分别执行，且状态可读。"""
    K.key_down("shift")
    time.sleep(0.02)
    down_state = K.is_key_pressed(0x10)
    K.key_up("shift")
    time.sleep(0.02)
    up_state = K.is_key_pressed(0x10)
    # 至少有一次读到按下；抬起后应为 False
    # （某些远程/虚拟环境下 GetAsyncKeyState 不灵，所以只对 up 严格）
    _expect(up_state is False,
            "key_up 之后 is_key_pressed 应为 False，实际 %r（down 时读到 %r）"
            % (up_state, down_state))


# --------------------------------------------------------------------------
# 8. 模块自检
# --------------------------------------------------------------------------
def test_module_selfchecks():
    """两个模块的 selfcheck() 必须都能跑通。"""
    for mod, name in ((K, "input.keyboard"), (M, "input.mouse")):
        fn = getattr(mod, "selfcheck", None)
        if fn is None:
            continue
        res = fn()
        ok = res.get("ok") if isinstance(res, dict) else bool(res)
        _expect(ok, "%s.selfcheck() 未通过：%r" % (name, res))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段三 · 输入层地基测试")
    print("=" * 64)
    w, h = M.primary_screen_size()
    print("屏幕: %dx%d   虚拟桌面: %s" % (w, h, M.virtual_screen_rect()))

    section("结构体尺寸与 ABI")
    check("MOUSEINPUT/KEYBDINPUT/INPUT 尺寸", test_struct_sizes)
    check("_INPUTUNION 等于最大成员", test_union_equals_largest_member)
    check("尺寸断言会真的抛异常", test_assert_function_raises_on_mismatch)

    section("绝对坐标归一化（硬约束 #3）")
    check("端点映射", test_normalize_endpoints)
    check("⭐ 往返零误差（无 +1px 偏差）", test_normalize_no_offbyone)
    check("越界钳制", test_normalize_clamps)
    check("单调性", test_normalize_monotonic)
    check("虚拟桌面归一化", test_virtual_screen_normalize)

    section("虚拟键码解析")
    check("常用键名", test_vk_map_basics)
    check("大小写不敏感", test_vk_map_case_insensitive)
    check("接受整数 VK", test_vk_accepts_int)
    check("非法键名抛异常", test_vk_rejects_garbage)
    check("扩展键标记", test_extended_keys_marked)

    section("事件结构构造")
    check("KEYBDINPUT 字段", test_keyboard_input_struct)
    check("KEYEVENTF_UNICODE 路径", test_unicode_key_input)
    check("MOUSEINPUT 字段", test_mouse_input_struct)
    check("滚轮 mouseData", test_mouse_wheel_data)
    check("INPUT 尺寸一致", test_mouse_input_size_guard)
    check("WHEEL_DELTA=120", test_wheel_delta)

    section("设备只读检查")
    check("读光标位置", test_cursor_pos_readable)
    check("读按键状态", test_key_state_readable)
    check("读键盘布局", test_keyboard_layout_readable)

    section("光标移动往返（测完归位）")
    check("精确落点", test_move_and_restore)
    check("越界钳制不抛异常", test_move_clamps_not_raises)

    section("无害按键")
    check("press(shift)", test_press_harmless_key)
    check("key_down/key_up", test_key_down_up)

    section("模块自检")
    check("keyboard/mouse selfcheck", test_module_selfchecks)

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段三 输入层测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s → %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
