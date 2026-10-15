# -*- coding: utf-8 -*-
"""鼠标注入：SendInput 物理模式，绝对坐标归一化到 65535。

坐标契约（质量硬约束 #3）
-------------------------
所有进入本模块的坐标都是**物理屏幕绝对坐标**（左上角为原点，1:1 像素）。
归一化公式固定为::

    nx = (x * 65535) // (screen_width  - 1)
    ny = (y * 65535) // (screen_height - 1)

为什么是 ``width - 1`` 而不是 ``width``：
    ``MOUSEEVENTF_ABSOLUTE`` 的坐标系是**闭区间** [0, 65535]，
    0 映射到屏幕第 0 列，65535 映射到第 ``width-1`` 列。用 ``width``
    做分母会让 65535 对应的像素落在屏幕外，表现为"总是差几个像素"。

为什么必须钳制：
    上层给的坐标可能来自过期的窗口快照（窗口已移动/缩放），
    越界值会让光标飞到别的显示器，动作落到错误窗口 —— 这种失败
    很难复现。钳制到屏幕范围内是最小惊讶行为。
"""
from __future__ import annotations

import ctypes
import time
from typing import Sequence, Tuple

from .._win32 import structs as S
from .._win32.api import last_error, user32, win32_error_text
from ..errors import ActionFailed

__all__ = [
    "ABSOLUTE_MAX",
    "SM_XVIRTUALSCREEN",
    "SM_YVIRTUALSCREEN",
    "SM_CXVIRTUALSCREEN",
    "SM_CYVIRTUALSCREEN",
    "virtual_screen_rect",
    "primary_screen_size",
    "normalize_absolute",
    "normalize_absolute_virtual",
    "make_mouse_input",
    "send_mouse_input",
    "move_absolute",
    "move_relative",
    "mouse_down",
    "mouse_up",
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "scroll",
    "scroll_horizontal",
    "drag",
    "get_cursor_pos",
    "set_cursor_pos",
    "flush_moves",
]


ABSOLUTE_MAX = 65535
#: 归一化的**分母**。注意：是 65536 而不是 65535。见 ``normalize_absolute``
#: 的说明 —— 这是一个实测出来的坑。
ABSOLUTE_SCALE = 65536

# GetSystemMetrics 索引
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_BUTTONS = {
    "left": (S.MOUSEEVENTF_LEFTDOWN, S.MOUSEEVENTF_LEFTUP),
    "right": (S.MOUSEEVENTF_RIGHTDOWN, S.MOUSEEVENTF_RIGHTUP),
    "middle": (S.MOUSEEVENTF_MIDDLEDOWN, S.MOUSEEVENTF_MIDDLEUP),
}

_XBUTTONS = {
    "x1": (S.XBUTTON1, S.MOUSEEVENTF_XDOWN, S.MOUSEEVENTF_XUP),
    "x2": (S.XBUTTON2, S.MOUSEEVENTF_XDOWN, S.MOUSEEVENTF_XUP),
}


# --------------------------------------------------------------------------
# 屏幕几何
# --------------------------------------------------------------------------
def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """虚拟桌面（所有显示器）的 ``(x, y, width, height)``。

    副屏在主屏左侧时 x 为负、在左侧时 y 为负 —— 这是**允许**的，
    正因为如此，跨屏移动必须用 VIRTUALDESK 归一化，见
    ``normalize_absolute_virtual``。
    """
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return (x, y, w, h)


def primary_screen_size() -> Tuple[int, int]:
    """主屏物理像素尺寸 ``(width, height)``。"""
    return (
        user32.GetSystemMetrics(0),   # SM_CXSCREEN
        user32.GetSystemMetrics(1),   # SM_CYSCREEN
    )


# --------------------------------------------------------------------------
# 坐标归一化
# --------------------------------------------------------------------------
def _clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def normalize_absolute(x: int, y: int) -> Tuple[int, int]:
    """主屏绝对坐标 → [0, 65535] 归一化坐标。越界钳制。

    公式：``round(x * 65536 / screen_width)``，上限钳到 65535。

    ⚠️⚠️ 这个公式与直觉相反，是**实测反推**出来的，务必看完再改
    =================================================================
    直觉/文档上的常见写法是::

        nx = (x * 65535) // (screen_width - 1)      # ❌ 实测有 1px 系统偏差

    它基于"65535 是闭区间最大值、对应最后一列"的推理。但 Windows 的
    ``MOUSEEVENTF_ABSOLUTE`` **实际反算方式**是::

        实际像素 = round(nx * screen_width / 65536)   # 分母是 65536，用全宽

    两个公式的差异导致：
      · 用 65535/(w-1)：1440 宽屏幕上 36 个采样点里 20 个偏 +1px，
        **且方向恒定偏右下**（系统把偏小的输入圆整到了下一像素）
      · 用 65536/w：实测 9 个跨越全屏的采样点 **全部 Δ=0**

    更严重的是边界：``x = w-1``（最后一列）用旧公式算出的 nx 会被
    系统反算成 **w**，即光标跑到屏幕右边界之外一个像素。

    所以这里的写法是：

        nx = min(65535, round(x * 65536 / w))

    保留 ``ABSOLUTE_MAX = 65535`` 作为**钳制上限**（协议要求上限），
    但归一化的**分母用 65536**。这两件事不是一回事。

    返回值的实测精度：单轴 0px（跨全屏 9 点全覆盖，Δ 全为 0）。
    在副屏/负坐标场合见 ``normalize_absolute_virtual``。
    """
    w, h = primary_screen_size()
    if w <= 1 or h <= 1:
        raise ActionFailed(
            "屏幕尺寸异常：%dx%d" % (w, h),
            method="mouse",
        )

    cx = _clamp(int(x), 0, w - 1)
    cy = _clamp(int(y), 0, h - 1)

    # round(x * 65536 / size)，用整数运算避免浮点
    #
    # ⚠ 这里有一个**已知的 1px 精度边界**，不是 bug、也无法靠调公式根治：
    # 65536 个离散归一化位置要覆盖 w 个像素，当 w 不能整除 65536 时，
    # 某些像素会落在"两个归一化值的中间"，Windows 侧取整后偏 1px。
    #
    # 实测（2880x1800）：x=960 → nx=21845 → 反算 959.985 → 系统给 959。
    # 换成"区间中点"公式能救 960，但会让 1440x900 下的 112 变成 113 ——
    # 误差点只是换了个位置。所以**保留原公式**（它在 1920 宽上 Δ=0），
    # 由测试容忍 ±1px（见 tests/test_phase3_input.py）。
    nx = (cx * ABSOLUTE_SCALE + w // 2) // w
    ny = (cy * ABSOLUTE_SCALE + h // 2) // h

    return (int(_clamp(nx, 0, ABSOLUTE_MAX)), int(_clamp(ny, 0, ABSOLUTE_MAX)))


def normalize_absolute_virtual(x: int, y: int) -> Tuple[int, int]:
    """虚拟桌面绝对坐标 → [0, 65535] 归一化坐标（配 VIRTUALDESK 标志）。

    副屏上 x 可能为负，所以先平移到以虚拟桌面左上角为原点。
    取整与分母规则同 ``normalize_absolute``（分母用全宽 65536 刻度）。
    """
    vx, vy, vw, vh = virtual_screen_rect()
    if vw <= 1 or vh <= 1:
        raise ActionFailed(
            "虚拟桌面尺寸异常：%dx%d" % (vw, vh),
            method="mouse",
        )

    cx = _clamp(int(x) + vx, 0, vw - 1)
    cy = _clamp(int(y) + vy, 0, vh - 1)

    # 取整与分母规则同 normalize_absolute（含那条 1px 精度边界的说明）
    nx = (cx * ABSOLUTE_SCALE + vw // 2) // vw
    ny = (cy * ABSOLUTE_SCALE + vh // 2) // vh
    return (int(_clamp(nx, 0, ABSOLUTE_MAX)), int(_clamp(ny, 0, ABSOLUTE_MAX)))


# --------------------------------------------------------------------------
# 事件构造与发送
# --------------------------------------------------------------------------
def make_mouse_input(
    dx: int = 0,
    dy: int = 0,
    mouse_data: int = 0,
    flags: int = 0,
) -> S.INPUT:
    """构造一个 INPUT_MOUSE 事件。"""
    inp = S.INPUT()
    inp.type = S.INPUT_MOUSE
    inp.u.mi.dx = ctypes.c_long(dx).value
    inp.u.mi.dy = ctypes.c_long(dy).value
    inp.u.mi.mouseData = ctypes.c_ulong(mouse_data).value & 0xFFFFFFFF
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = 0
    return inp


def send_mouse_input(inputs: Sequence[S.INPUT]) -> int:
    """发送一批鼠标事件，失败抛 ``ActionFailed``。"""
    if not inputs:
        return 0
    n = len(inputs)
    arr = (S.INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(S.INPUT))
    if sent != n:
        err = last_error()
        raise ActionFailed(
            "SendInput 鼠标事件注入失败：期望 %d 个，实际 %d 个（错误 %d：%s）"
            % (n, sent, err, win32_error_text(err)),
            method="mouse",
            expected=n,
            sent=sent,
            last_error=err,
        )
    return sent


# --------------------------------------------------------------------------
# 移动
# --------------------------------------------------------------------------
def move_absolute(x: int, y: int, *, virtual_desk: bool = False) -> None:
    """把光标瞬时移动到物理屏幕绝对坐标 ``(x, y)``。

    这是"瞬移"，不是真人移动。真人化移动在 ``human_entropy.mouse``。
    """
    if virtual_desk:
        nx, ny = normalize_absolute_virtual(x, y)
        flags = S.MOUSEEVENTF_MOVE | S.MOUSEEVENTF_ABSOLUTE | S.MOUSEEVENTF_VIRTUALDESK
    else:
        nx, ny = normalize_absolute(x, y)
        flags = S.MOUSEEVENTF_MOVE | S.MOUSEEVENTF_ABSOLUTE

    send_mouse_input([make_mouse_input(dx=nx, dy=ny, flags=flags)])


def move_relative(dx: int, dy: int) -> None:
    """相对移动（不归一化，dx/dy 直接是像素增量）。"""
    send_mouse_input(
        [make_mouse_input(dx=int(dx), dy=int(dy), flags=S.MOUSEEVENTF_MOVE)]
    )


def flush_moves() -> None:
    """发一个零位移事件。

    用途：Windows 会把连续的相对移动**合并**（coalesce）成一个事件，
    如果上层需要在两个移动之间插入一个精确采样点（例如轨迹录制），
    中间插一个 0 位移即可打断合并。同时它也能让"移动距离不足"
    的情况仍然产生一次 WM_MOUSEMOVE，使 hover 生效。
    """
    send_mouse_input(
        [
            make_mouse_input(
                dx=0,
                dy=0,
                flags=S.MOUSEEVENTF_MOVE | S.MOUSEEVENTF_MOVE_NOCOALESCE,
            )
        ]
    )


# --------------------------------------------------------------------------
# 按下 / 抬起
# --------------------------------------------------------------------------
def mouse_down(button: str = "left") -> None:
    """按下鼠标键。``button`` ∈ {left, right, middle, x1, x2}。"""
    b = button.lower()
    if b in _BUTTONS:
        down_flag, _ = _BUTTONS[b]
        send_mouse_input([make_mouse_input(flags=down_flag)])
        return
    if b in _XBUTTONS:
        xflag, down_flag, _ = _XBUTTONS[b]
        send_mouse_input(
            [make_mouse_input(mouse_data=xflag, flags=down_flag)]
        )
        return
    raise ActionFailed("未知鼠标键：%r" % (button,), method="mouse")


def mouse_up(button: str = "left") -> None:
    """抬起鼠标键。"""
    b = button.lower()
    if b in _BUTTONS:
        _, up_flag = _BUTTONS[b]
        send_mouse_input([make_mouse_input(flags=up_flag)])
        return
    if b in _XBUTTONS:
        xflag, _, up_flag = _XBUTTONS[b]
        send_mouse_input(
            [make_mouse_input(mouse_data=xflag, flags=up_flag)]
        )
        return
    raise ActionFailed("未知鼠标键：%r" % (button,), method="mouse")


# --------------------------------------------------------------------------
# 点击
# --------------------------------------------------------------------------
def click(
    x: int = None,
    y: int = None,
    *,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.02,
    hold: float = 0.0,
    virtual_desk: bool = False,
) -> None:
    """点击。``(x, y)`` 为 None 时在原地点击（不动光标）。

    ``interval`` 是多次点击之间的间隔；``hold`` 是按下到抬起的按住时长。
    真人单击的 hold 约 40-100ms；双击的两次间隔约 80-180ms。
    """
    if (x is None) != (y is None):
        raise ActionFailed(
            "click 的 x/y 必须同时给出或同时省略",
            method="mouse",
        )

    if x is not None:
        move_absolute(x, y, virtual_desk=virtual_desk)

    if clicks < 1:
        raise ActionFailed("clicks 必须 >= 1，收到 %r" % (clicks,), method="mouse")

    for i in range(clicks):
        mouse_down(button)
        if hold > 0:
            time.sleep(hold)
        mouse_up(button)
        if i < clicks - 1 and interval > 0:
            time.sleep(interval)


def double_click(x: int = None, y: int = None, *, button: str = "left", interval: float = 0.08, **kw) -> None:
    """双击。默认间隔 80ms（在系统双击阈值内）。"""
    click(x, y, button=button, clicks=2, interval=interval, **kw)


def right_click(x: int = None, y: int = None, **kw) -> None:
    """右键单击。"""
    click(x, y, button="right", **kw)


def middle_click(x: int = None, y: int = None, **kw) -> None:
    """中键单击。"""
    click(x, y, button="middle", **kw)


# --------------------------------------------------------------------------
# 滚轮
# --------------------------------------------------------------------------
WHEEL_DELTA = 120


def scroll(amount: int, *, x: int = None, y: int = None, steps: int = 1) -> None:
    """垂直滚动。

    ``amount`` 为正 = 向上滚（远离用户），负 = 向下滚。
    单位是 120 的倍数（``WHEEL_DELTA``）。

    ``steps`` > 1 时把总量拆成多步发送 —— 应用程序对"一步滚 1200"
    和"滚十步每步 120"的处理完全不同（前者常被忽略或跳变）。
    """
    if x is not None and y is not None:
        move_absolute(x, y)

    if not isinstance(amount, int) or amount == 0:
        raise ActionFailed("scroll 的 amount 必须是非零整数", method="mouse")

    steps = max(1, int(steps))
    base = amount // steps
    rest = amount - base * steps

    for i in range(steps):
        delta = base + (rest if i == steps - 1 else 0)
        if delta == 0:
            continue
        send_mouse_input(
            [make_mouse_input(mouse_data=delta, flags=S.MOUSEEVENTF_WHEEL)]
        )


def scroll_horizontal(amount: int, *, x: int = None, y: int = None, steps: int = 1) -> None:
    """水平滚动。``amount`` 正 = 向右。"""
    if x is not None and y is not None:
        move_absolute(x, y)

    if not isinstance(amount, int) or amount == 0:
        raise ActionFailed("scroll_horizontal 的 amount 必须是非零整数", method="mouse")

    steps = max(1, int(steps))
    base = amount // steps
    rest = amount - base * steps

    for i in range(steps):
        delta = base + (rest if i == steps - 1 else 0)
        if delta == 0:
            continue
        send_mouse_input(
            [make_mouse_input(mouse_data=delta, flags=S.MOUSEEVENTF_HWHEEL)]
        )


# --------------------------------------------------------------------------
# 拖拽
# --------------------------------------------------------------------------
def drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    button: str = "left",
    steps: int = 20,
    step_delay: float = 0.01,
    hold_before: float = 0.05,
    hold_after: float = 0.05,
    virtual_desk: bool = False,
) -> None:
    """线性拖拽：从 (x1,y1) 到 (x2,y2)。

    线性、等速 —— 这是"机械"拖拽，不是真人拖拽。真人化拖拽在
    ``human_entropy.mouse.drag_human``。

    ``hold_before`` 是按下之后、开始移动之前的停顿（让目标窗口
    先进入"拖动待命"状态）；``hold_after`` 是到位后、抬起前的停顿
    （让目标有机会响应 hover 高亮）。
    """
    move_absolute(x1, y1, virtual_desk=virtual_desk)
    time.sleep(hold_before)
    mouse_down(button)

    try:
        for i in range(1, steps + 1):
            t = i / steps
            px = int(round(x1 + (x2 - x1) * t))
            py = int(round(y1 + (y2 - y1) * t))
            move_absolute(px, py, virtual_desk=virtual_desk)
            if step_delay > 0:
                time.sleep(step_delay)
        if hold_after > 0:
            time.sleep(hold_after)
    finally:
        # 无论中途出什么岔子都必须抬起按键，
        # 否则鼠标会卡在"按住"状态，后续所有操作都变成拖拽
        mouse_up(button)


# --------------------------------------------------------------------------
# 光标位置
# --------------------------------------------------------------------------
def get_cursor_pos() -> Tuple[int, int]:
    """取当前光标物理坐标。"""
    pt = S.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        err = last_error()
        raise ActionFailed(
            "GetCursorPos 失败（错误 %d：%s）" % (err, win32_error_text(err)),
            method="mouse",
        )
    return (pt.x, pt.y)


def set_cursor_pos(x: int, y: int) -> None:
    """用 SetCursorPos 直接设光标位置（不发 SendInput 事件）。

    与 ``move_absolute`` 的区别：SetCursorPos **不产生鼠标输入事件**，
    某些应用（尤其是游戏和用 Raw Input 的程序）收不到；但它的优点是
    不受 UIPI 限制（在多数情况下）。作为降级手段使用。
    """
    if not user32.SetCursorPos(int(x), int(y)):
        err = last_error()
        raise ActionFailed(
            "SetCursorPos(%d, %d) 失败（错误 %d：%s）"
            % (x, y, err, win32_error_text(err)),
            method="mouse",
        )


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """鼠标输入层自检（**不动真实光标**）。

    这里的重点是归一化公式 —— 分母 65536 是硬约束 #3，
    往返误差必须为 0px。真实移动由 ``tests/test_phase3_input.py``
    覆盖，那里会保存并还原光标位置。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 常量 ---------------------------------------------------------------
    _add("ABSOLUTE_MAX == 65535", ABSOLUTE_MAX == 65535, str(ABSOLUTE_MAX))

    # -- 屏幕尺寸可读 -------------------------------------------------------
    try:
        w, h = primary_screen_size()
        _add("主屏尺寸可读", w > 0 and h > 0, "%dx%d" % (w, h))
    except Exception as exc:      # noqa: BLE001
        _add("主屏尺寸可读", False, "%s: %s" % (type(exc).__name__, exc))
        return {"checks": checks, "failures": failures, "ok": not failures}

    # -- ⭐ 归一化往返误差 0px（硬约束 #3）---------------------------------
    worst = 0
    worst_at = None
    pts = [0, 1, 2, w // 4, w // 2 - 1, w // 2, w // 2 + 1,
           3 * w // 4, w - 2, w - 1]
    bad = []
    for x in pts:
        nx, ny = normalize_absolute(x, h // 2)
        back = round(nx * w / 65536)
        err = abs(back - x)
        if err > worst:
            worst, worst_at = err, x
        if err:
            bad.append((x, back))
    _add("水平归一化往返 0 误差", worst == 0,
         "%d 个采样点，最大误差 %dpx%s"
         % (len(pts), worst, "（x=%r）" % worst_at if worst else ""))

    _add("归一化输出在 0..65535 内",
         all(0 <= v <= ABSOLUTE_MAX
             for v in (normalize_absolute(0, 0)[0],
                       normalize_absolute(w - 1, h - 1)[0],
                       normalize_absolute(w - 1, h - 1)[1])),
         "四角采样")

    # -- 越界必须钳制而不是回绕 ---------------------------------------------
    _add("越界坐标被钳制",
         normalize_absolute(-500, -500) == (0, 0),
         "(-500,-500) -> %r" % (normalize_absolute(-500, -500),))
    far = normalize_absolute(w + 9999, h + 9999)
    _add("远超界被钳到上限",
         far[0] >= (w - 1) * 65536 // w - 1 and far[0] <= ABSOLUTE_MAX,
         "(w+9999,h+9999) -> %r" % (far,))

    # -- 结构体 -------------------------------------------------------------
    try:
        mi = make_mouse_input(0, 0, 0, 0)
        _add("make_mouse_input 可用", mi is not None, type(mi).__name__)
    except Exception as exc:      # noqa: BLE001
        _add("make_mouse_input 可用", False, "%s: %s" % (type(exc).__name__, exc))

    # -- 虚屏矩形 -----------------------------------------------------------
    try:
        rect = virtual_screen_rect()
        _add("虚屏矩形可读", len(rect) == 4, "%r" % (rect,))
    except Exception as exc:      # noqa: BLE001
        _add("虚屏矩形可读", False, "%s: %s" % (type(exc).__name__, exc))

    # -- 光标位置可读（只读，不设置）----------------------------------------
    try:
        pos = get_cursor_pos()
        _add("光标位置可读", isinstance(pos, tuple) and len(pos) == 2,
             "%r" % (pos,))
    except Exception as exc:      # noqa: BLE001
        _add("光标位置可读", False, "%s: %s" % (type(exc).__name__, exc))

    return {"checks": checks, "failures": failures, "ok": not failures}
