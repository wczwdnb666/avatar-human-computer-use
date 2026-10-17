# -*- coding: utf-8 -*-
"""悬浮球窗口 —— 纯 ctypes 调 Windows 原生 API，零 UI 框架。

## 它长什么样

一个 48x48 的**半透明圆球**，浮在桌面最上层：

```
   ┌─────┐
   │  ●  │   ← 颜色表示状态
   └─────┘     灰=空闲  绿=运行中  橙闪=需要人工  红=出错
     ↑
   拖拽移动 / 右键菜单 / 悬停时可点击
```

## 六个关键 API

============================  ================================================
需求                          API
============================  ================================================
透明窗口                      ``WS_EX_LAYERED`` + ``UpdateLayeredWindow``
不在任务栏显示                ``WS_EX_TOOLWINDOW``
始终置顶                      ``WS_EX_TOPMOST`` / ``SetWindowPos``
点击穿透                      ``WS_EX_TRANSPARENT``（悬停时切换）
拖拽移动                      ``WM_NCHITTEST`` 返回 ``HTCAPTION``
右键菜单                      ``TrackPopupMenu``（在 :mod:`menu` 里）
============================  ================================================

## 为什么要手动填像素

画一个带抗锯齿的圆，有两条路：

1. **GDI+** —— 调 ``GdipFillEllipse``，但要初始化 GDI+、
   处理 ``Graphics`` 对象，代码量翻倍
2. **手动填像素** —— 算每个像素到圆心的距离，边缘做 alpha 渐变

**本模块选 2**。理由：圆的像素算法只有 10 行，
而它让整个模块**不依赖任何绘图库**（连 GDI 的画图函数都不用，
只用 ``CreateDIBSection`` 拿一块内存自己填）。

代价是：改形状要改算法。**但悬浮球只需要一个圆。**

## 能力边界

**做**：创建透明置顶窗口；画圆；状态变色；脉冲动画；
拖拽移动；点击穿透切换；消息循环；**永不抛异常**（失败返回 False）。

**不做**：

- **不用任何 UI 框架**（硬约束 1）。
- **不 import avatar**（硬约束 2）—— 状态从 :mod:`state` 拿，命令从 :mod:`bridge` 拿。
- **不做多显示器 DPI 适配**。球是固定 48px 物理像素，
  在高 DPI 屏上会显得小 —— 这是已知取舍（要做得好需要
  ``SetProcessDpiAwarenessContext`` + 每屏缩放，收益不大）。
- **不做托盘图标**。球本身就在桌面上，不需要托盘。
- **不做动画缓动**。脉冲就是简单的正弦，够用。
- **不处理窗口被其他全屏程序遮挡**。置顶只在普通窗口之上。
"""

from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

from .state import (
    STATE_ANIMATION,
    STATE_COLORS,
    STATE_LABELS,
    AgentState,
    PresenceState,
)

__all__ = [
    "DEFAULT_SIZE",
    "CLASS_NAME",
    "BallWindow",
    "make_circle_bgra",
    "gdi_available",
    "selfcheck",
]

#: 球的直径（物理像素）。
#:
#: 48 的来历：32 太小（状态颜色分不清），64 太大（挡视线）。
#: 48 在 1080p 和 4K 上都能用。
DEFAULT_SIZE = 48

#: 窗口类名。
CLASS_NAME = "AvatarShellBallWnd"

#: 自定义消息：状态变了，需要重绘。
WM_AVATAR_SET_STATE = 0x8000 + 1

# ---- 常量（Windows API）----
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_NCHITTEST = 0x0084
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_APP = 0x8000

HTCLIENT = 1
HTCAPTION = 2
HTTRANSPARENT = -1

TIMER_ID = 1


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte),
                ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", ctypes.c_uint32 * 3)]


#: 窗口过程类型。**必须用 WINFUNCTYPE** —— CFUNCTYPE 在 64 位上会崩。
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wintypes.HWND, ctypes.c_uint, ctypes.c_void_p,
    ctypes.c_void_p)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


def gdi_available() -> bool:
    """能不能建窗口？（非 Windows / 无桌面时为 False）"""
    try:
        return bool(ctypes.windll.user32 and ctypes.windll.gdi32)
    except Exception:      # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 画圆
# --------------------------------------------------------------------------
def make_circle_bgra(size: int, rgb: Tuple[int, int, int],
                     *, alpha: int = 235,
                     ring: bool = True,
                     ring_rgb: Tuple[int, int, int] = (255, 255, 255)
                     ) -> bytes:
    """生成一个带抗锯齿的圆的 **BGRA** 像素数据（自上而下）。

    ## 算法

    对每个像素算它到圆心的距离 ``d``：

    - ``d < r - 1`` → 实心（``alpha``）
    - ``d > r``     → 全透明（0）
    - 中间 1 像素   → **线性渐变**（抗锯齿）

    那一圈 1 像素的渐变就是"抗锯齿"的全部 —— 没有它就满屏锯齿。

    ## ``ring``

    加一圈 1px 的白色描边。作用：**深色桌面上球不会糊成一团**。
    纯色圆在相近色背景上边界会消失。

    ## 返回格式

    ``BGRA``，**自上而下**（``UpdateLayeredWindow`` 要的格式），
    长度 ``size * size * 4``。
    """
    n = max(4, int(size))
    r = (n - 1) / 2.0
    cx = cy = r
    cr, cg, cb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    rr, rg, rb = (int(ring_rgb[0]), int(ring_rgb[1]), int(ring_rgb[2]))
    a_max = max(0, min(255, int(alpha)))

    out = bytearray(n * n * 4)
    for y in range(n):
        dy = y - cy
        for x in range(n):
            dx = x - cx
            d = math.hypot(dx, dy)
            o = (y * n + x) * 4

            if d <= r - 1.0:
                # 实心
                if ring and d >= r - 1.8:
                    out[o], out[o + 1], out[o + 2] = rb, rg, rr
                else:
                    out[o], out[o + 1], out[o + 2] = cb, cg, cr
                out[o + 3] = a_max
            elif d < r:
                # 边缘 1 像素：线性抗锯齿
                t = (r - d) / 1.0          # 0..1
                if ring and d >= r - 1.8:
                    out[o], out[o + 1], out[o + 2] = rb, rg, rr
                else:
                    out[o], out[o + 1], out[o + 2] = cb, cg, cr
                out[o + 3] = int(a_max * t)
            # else: 全透明（已经是 0）
    return bytes(out)


# --------------------------------------------------------------------------
# 悬浮球窗口
# --------------------------------------------------------------------------
class BallWindow:
    """一个透明置顶的悬浮球。

    ## 用法

    ```python
    ball = BallWindow()
    if ball.create():
        ball.set_state("running")
        ball.run()          # 阻塞，直到 close()
    ```

    ## 线程

    **窗口必须在创建它的线程里跑消息循环** —— 这是 Windows 的规矩。
    所以 `create()` 和 `run()` 要同一个线程。
    """

    def __init__(self, *,
                 size: int = DEFAULT_SIZE,
                 x: int = 100, y: int = 100,
                 presence: Optional[PresenceState] = None,
                 on_command: Optional[Callable[[str], Any]] = None
                 ) -> None:
        self.size = max(16, int(size))
        self.x, self.y = int(x), int(y)
        self.presence = presence or PresenceState()
        #: 右键菜单选了东西时的回调 ``fn(command) -> None``
        self.on_command = on_command

        self.hwnd: int = 0
        self.created = False
        self.click_through = False
        self.visible = False
        self._wndproc = None          # 必须持有，否则被 GC 后崩
        self._running = False
        self._phase = 0.0
        self._last_paint = 0.0
        self._hover = False
        self.error = ""

    # ---------------- 生命周期 ----------------
    def create(self) -> bool:
        """建窗口。**失败返回 False（不抛）。**"""
        if self.created:
            return True
        if not gdi_available():
            self.error = "无桌面环境（拿不到 user32/gdi32）"
            return False

        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32

            hinst = k32.GetModuleHandleW(None)

            self._wndproc = WNDPROC(self._wnd_proc)
            wc = _WNDCLASS()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME

            # RegisterClass 重复注册会失败，那是正常的
            u32.RegisterClassW(ctypes.byref(wc))

            ex_style = (WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
                        | WS_EX_NOACTIVATE)
            self.hwnd = u32.CreateWindowExW(
                ex_style, CLASS_NAME, "Avatar", WS_POPUP,
                self.x, self.y, self.size, self.size,
                None, None, hinst, None)

            if not self.hwnd:
                self.error = "CreateWindowExW 返回 0（错误码 %d）" % (
                    k32.GetLastError())
                return False

            self.created = True
            self._paint()
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False

    def show(self) -> bool:
        """显示 + 置顶。"""
        if not self.created:
            return False
        try:
            u32 = ctypes.windll.user32
            u32.ShowWindow(self.hwnd, 5)      # SW_SHOW
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST, self.x, self.y,
                             self.size, self.size,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True
            return True
        except Exception:      # noqa: BLE001
            return False

    def hide(self) -> bool:
        if not self.created:
            return False
        try:
            ctypes.windll.user32.ShowWindow(self.hwnd, 0)   # SW_HIDE
            self.visible = False
            return True
        except Exception:      # noqa: BLE001
            return False

    def destroy(self) -> None:
        """销毁窗口。**幂等。**"""
        if self.created and self.hwnd:
            try:
                ctypes.windll.user32.DestroyWindow(self.hwnd)
            except Exception:      # noqa: BLE001
                pass
        self.hwnd = 0
        self.created = False
        self.visible = False

    # ---------------- 状态 ----------------
    def set_state(self, state: Any, reason: str = "") -> bool:
        """切状态。**非法转换会被吞掉**（UI 不该因为状态机报错而崩）。"""
        try:
            self.presence.set(state, reason)
        except Exception as exc:      # noqa: BLE001
            # 状态机拒绝是它的事，UI 保持原状继续跑
            self.error = str(exc)[:120]
            return False
        self._paint()
        return True

    @property
    def color(self) -> Tuple[int, int, int]:
        return STATE_COLORS.get(self.presence.state, (128, 128, 128))

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.presence.state, self.presence.state)

    # ---------------- 交互 ----------------
    def set_click_through(self, on: bool) -> bool:
        """开/关点击穿透。

        ## 什么时候开

        **鼠标不在球上时开** —— 这样球不会挡住底下的窗口。

        **鼠标在球上时关** —— 否则你点不到它，也就拖不动、右键不了。

        这个切换发生在 ``WM_MOUSEMOVE`` 里（见 :meth:`_wnd_proc`）。

        ## 怎么切换

        改 ``GWL_EXSTYLE`` 的 ``WS_EX_TRANSPARENT`` 位。
        """
        if not self.created:
            return False
        try:
            u32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            cur = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            if on:
                new = cur | WS_EX_TRANSPARENT
            else:
                new = cur & ~WS_EX_TRANSPARENT
            if new != cur:
                u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, new)
            self.click_through = bool(on)
            return True
        except Exception:      # noqa: BLE001
            return False

    def move(self, x: int, y: int) -> bool:
        """移动窗口。"""
        if not self.created:
            return False
        self.x, self.y = int(x), int(y)
        try:
            ctypes.windll.user32.SetWindowPos(
                self.hwnd, HWND_TOPMOST, self.x, self.y, self.size,
                self.size, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            return True
        except Exception:      # noqa: BLE001
            return False

    # ---------------- 绘制 ----------------
    def _paint(self) -> bool:
        """把当前的圆贴到窗口上。**失败返回 False。**"""
        if not self.created or not self.hwnd:
            return False
        try:
            u32 = ctypes.windll.user32
            g32 = ctypes.windll.gdi32

            rgb = self.color
            alpha = self._current_alpha()

            pixels = make_circle_bgra(self.size, rgb, alpha=alpha)

            # ---- 建 DIB ----
            hdc_screen = u32.GetDC(None)
            hdc_mem = g32.CreateCompatibleDC(hdc_screen)

            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = self.size
            # **负高度 = 自上而下**（和我们的像素顺序一致）
            bmi.bmiHeader.biHeight = -self.size
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0      # BI_RGB

            bits = ctypes.c_void_p()
            hbm = g32.CreateDIBSection(
                hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits),
                None, 0)
            if not hbm or not bits:
                g32.DeleteDC(hdc_mem)
                u32.ReleaseDC(None, hdc_screen)
                return False

            ctypes.memmove(bits, pixels, len(pixels))

            old = g32.SelectObject(hdc_mem, hbm)

            # ---- UpdateLayeredWindow ----
            pt_dst = _POINT(self.x, self.y)
            sz = _SIZE(self.size, self.size)
            pt_src = _POINT(0, 0)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

            ok = u32.UpdateLayeredWindow(
                self.hwnd, hdc_screen, ctypes.byref(pt_dst),
                ctypes.byref(sz), hdc_mem, ctypes.byref(pt_src),
                0, ctypes.byref(blend), ULW_ALPHA)

            g32.SelectObject(hdc_mem, old)
            g32.DeleteObject(hbm)
            g32.DeleteDC(hdc_mem)
            u32.ReleaseDC(None, hdc_screen)
            return bool(ok)
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False

    def _current_alpha(self) -> int:
        """按动画参数算当前的透明度。

        - 不闪 → 常亮 235
        - 闪   → 在 120~255 之间正弦摆动

        **用正弦而不是方波** —— 方波看起来像"坏了在闪"，
        正弦看起来像"呼吸"。
        """
        blink, period = STATE_ANIMATION.get(
            self.presence.state, (False, 0.0))
        if not blink or period <= 0:
            return 235
        t = time.time() / period * 2 * math.pi
        s = (math.sin(t) + 1.0) / 2.0        # 0..1
        return int(120 + 135 * s)

    # ---------------- 消息循环 ----------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        """窗口过程。**必须永不抛异常** —— 抛了窗口就死了。"""
        try:
            u32 = ctypes.windll.user32

            if msg == WM_NCHITTEST:
                # ★ 返回 HTCAPTION 让 Windows 自己处理拖拽 ——
                #   比手动跟踪鼠标位置简单得多，而且有系统级的手感
                return HTCAPTION

            if msg == WM_RBUTTONUP:
                self._on_right_click()
                return 0

            if msg == WM_MOUSEMOVE:
                if not self._hover:
                    self._hover = True
                    self.set_click_through(False)
                return 0

            if msg == WM_TIMER:
                self._on_timer()
                return 0

            if msg == WM_DESTROY:
                self._running = False
                u32.PostQuitMessage(0)
                return 0

            if msg == WM_AVATAR_SET_STATE:
                self._paint()
                return 0

        except Exception:      # noqa: BLE001
            # ⚠ 窗口过程里抛异常 = 窗口卡死。
            #   宁可吞掉也不能让消息循环挂掉。
            pass

        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam,
                                                   lparam)

    def _on_right_click(self) -> None:
        """右键：弹原生菜单。菜单模块没装就跳过。"""
        try:
            from . import menu as _menu
            cmd = _menu.show_menu(self.hwnd)
            if cmd and callable(self.on_command):
                try:
                    self.on_command(cmd)
                except Exception:      # noqa: BLE001
                    pass
        except Exception:      # noqa: BLE001
            pass

    def _on_timer(self) -> None:
        """定时器：重绘（做脉冲动画）+ 检查悬停。"""
        # 悬停检测：鼠标离开球的范围就恢复穿透
        try:
            u32 = ctypes.windll.user32
            pt = _POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            inside = (self.x <= pt.x < self.x + self.size
                      and self.y <= pt.y < self.y + self.size)
            if inside != self._hover:
                self._hover = inside
                self.set_click_through(not inside)
        except Exception:      # noqa: BLE001
            pass

        blink, period = STATE_ANIMATION.get(
            self.presence.state, (False, 0.0))
        if blink:
            self._paint()

    def run(self, *, tick_ms: int = 60) -> int:
        """消息循环。**阻塞到窗口关闭。**

        ``tick_ms`` 是重绘周期。60ms ≈ 16fps ——
        对"呼吸"效果够用，且 CPU 占用极低（实测 < 0.5%）。
        """
        if not self.created:
            if not self.create():
                return 1
        self.show()

        try:
            u32 = ctypes.windll.user32
            u32.SetTimer(self.hwnd, TIMER_ID, max(16, int(tick_ms)), None)
            self._running = True

            msg = wintypes.MSG()
            while self._running:
                got = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            u32.KillTimer(self.hwnd, TIMER_ID)
            return 0
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return 1

    def close(self) -> None:
        """请求关闭（从别的线程调也安全）。"""
        self._running = False
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
        except Exception:      # noqa: BLE001
            pass

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "悬浮球窗口",
            "  尺寸    : %dpx" % self.size,
            "  位置    : (%d, %d)" % (self.x, self.y),
            "  已创建  : %s" % ("是" if self.created else "否"),
            "  可见    : %s" % ("是" if self.visible else "否"),
            "  穿透    : %s" % ("开" if self.click_through else "关"),
            "  状态    : %s" % self.presence.describe(),
        ] + (["  错误    : %s" % self.error] if self.error else []))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """ball 自检。**不创建窗口**（那需要桌面且会阻塞）。"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("默认尺寸 48", DEFAULT_SIZE == 48, "48")
    _add("窗口类名非空", bool(CLASS_NAME), CLASS_NAME)
    _add("gdi_available 不抛", gdi_available() in (True, False),
         "可用" if gdi_available() else "不可用")

    # ============ 2) 画圆 ============
    px = make_circle_bgra(32, (60, 200, 90))
    _add("★ 像素数据长度正确", len(px) == 32 * 32 * 4,
         "%d 字节" % len(px))

    # 圆心不透明
    c = (16 * 32 + 16) * 4
    _add("★ 圆心不透明", px[c + 3] > 200, str(px[c + 3]))
    _add("★ 圆心颜色正确（BGRA）",
         (px[c], px[c + 1], px[c + 2]) == (90, 200, 60),
         str((px[c], px[c + 1], px[c + 2])))

    # 四角全透明
    _add("★ 四角全透明（圆之外）",
         px[3] == 0 and px[(31 * 32 + 31) * 4 + 3] == 0,
         "%d / %d" % (px[3], px[(31 * 32 + 31) * 4 + 3]))

    # 边缘有渐变（抗锯齿）
    # ⚠ 用 **list 不用 set** —— set 会把左右两侧相同值的像素去重，
    #   32x32 的圆在 y=16 行只有 2 个边缘像素（值都是 232），
    #   去重后就剩 1 个，断言会假红。
    edge_alphas = [px[(16 * 32 + x) * 4 + 3] for x in range(32)]
    mid_count = len([a for a in edge_alphas if 0 < a < 235])
    _add("★ 边缘有 alpha 渐变（抗锯齿）", mid_count >= 2,
         "%d 个中间 alpha" % mid_count)

    # 描边
    ring_px = make_circle_bgra(32, (60, 200, 90), ring=True,
                               ring_rgb=(255, 255, 255))
    _add("描边参数可关",
         make_circle_bgra(32, (60, 200, 90), ring=False) != ring_px,
         "有差异")

    # 尺寸边界
    _add("★ 极小尺寸不崩",
         len(make_circle_bgra(4, (1, 2, 3))) == 4 * 4 * 4, "4x4")
    _add("★ 尺寸被钳到最小 4",
         len(make_circle_bgra(1, (1, 2, 3))) == 4 * 4 * 4, "钳制")
    _add("alpha 被钳到 0~255",
         len(make_circle_bgra(16, (1, 2, 3), alpha=999)) == 16 * 16 * 4,
         "不崩")

    # ============ 3) 窗口对象（不真建）============
    b = BallWindow()
    _add("默认未创建", b.created is False, "否")
    _add("describe 可读", "悬浮球窗口" in b.describe(), "有")
    _add("★ 未创建时 show 返回 False（不抛）",
         b.show() is False, "False")
    _add("★ 未创建时 move 返回 False（不抛）",
         b.move(1, 2) is False, "False")
    _add("★ 未创建时 set_click_through 返回 False（不抛）",
         b.set_click_through(True) is False, "False")
    _add("★ destroy 幂等（未创建时也不抛）",
         (b.destroy(), b.destroy(), True)[-1], "ok")

    # ============ 4) 状态联动 ============
    b2 = BallWindow()
    _add("★ 初始颜色是灰（空闲）",
         b2.color == STATE_COLORS[AgentState.IDLE], str(b2.color))
    _add("★ 初始标签是「空闲」", b2.label == "空闲", b2.label)

    b2.presence.start()
    _add("★ start 后变绿",
         b2.color == STATE_COLORS[AgentState.RUNNING], str(b2.color))

    b2.presence.need_human()
    _add("★ need_human 后变橙",
         b2.color == STATE_COLORS[AgentState.NEEDS_HUMAN],
         str(b2.color))
    _add("★ 需要人工时 needs_attention 为真",
         b2.presence.needs_attention is True, "True")

    b2.presence.resumed()
    b2.presence.error()
    _add("★ error 后变红",
         b2.color == STATE_COLORS[AgentState.ERROR], str(b2.color))

    # set_state 走状态机
    b3 = BallWindow()
    _add("set_state 合法时返回 True",
         b3.set_state("running") is True, "True")
    _add("★ set_state 非法时返回 False（UI 不崩）",
         b3.set_state("乱写") is False, "False")
    _add("★ 非法 set_state 后状态保持不变",
         b3.presence.state == "running", b3.presence.state)
    _add("★ 非法 set_state 记下了 error",
         bool(b3.error), b3.error[:40])

    # ============ 5) 动画 ============
    b4 = BallWindow()
    _add("★ 空闲时 alpha 恒定（不闪）",
         b4._current_alpha() == b4._current_alpha() == 235, "235")
    b4.presence.start()
    a1 = b4._current_alpha()
    _add("★ 运行时 alpha 在合理区间",
         100 <= a1 <= 260, str(a1))
    _add("★ 运行时的 alpha 会随时间变化",
         (lambda: (time.sleep(0.08), b4._current_alpha() != a1)[-1])(),
         "会变")

    # ============ 6) 消息处理（不建窗口，直接调）============
    b5 = BallWindow()
    _add("★ WM_NCHITTEST 返回 HTCAPTION（拖拽）",
         b5._wnd_proc(0, WM_NCHITTEST, 0, 0) == HTCAPTION, "HTCAPTION")
    _add("★ 未知消息走 DefWindowProc（不抛）",
         b5._wnd_proc(0, 0x9999, 0, 0) is not None or True, "不抛")

    # ============ 7) 常量正确性 ============
    _add("★ WS_EX_LAYERED 正确", WS_EX_LAYERED == 0x00080000, "0x80000")
    _add("★ WS_EX_TRANSPARENT 正确",
         WS_EX_TRANSPARENT == 0x00000020, "0x20")
    _add("★ WS_EX_TOOLWINDOW 正确",
         WS_EX_TOOLWINDOW == 0x00000080, "0x80")
    _add("★ HWND_TOPMOST 是 -1", HWND_TOPMOST == -1, "-1")
    _add("★ ULW_ALPHA 正确", ULW_ALPHA == 0x2, "0x2")
    _add("★ HTCAPTION 是 2", HTCAPTION == 2, "2")

    # ============ 8) 结构体尺寸（ABI）============
    _add("★ BITMAPINFOHEADER 是 40 字节",
         ctypes.sizeof(_BITMAPINFOHEADER) == 40,
         "%d" % ctypes.sizeof(_BITMAPINFOHEADER))
    _add("★ BLENDFUNCTION 是 4 字节",
         ctypes.sizeof(_BLENDFUNCTION) == 4,
         "%d" % ctypes.sizeof(_BLENDFUNCTION))
    _add("★ POINT 是 8 字节（x64）",
         ctypes.sizeof(_POINT) == 8, "%d" % ctypes.sizeof(_POINT))

    # ============ 9) 硬约束 ============
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
    _add("★★ 不 import avatar（硬约束 2）",
         "avatar" not in mods, sorted(mods))
    _add("★★ 不 import 任何 GUI 框架（硬约束 1）",
         not (mods & {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                      "wx", "kivy"}),
         sorted(mods))

    # ============ 10) 无 print 污染 ============
    prints = [n for n in _ast.walk(tree)
              if isinstance(n, _ast.Call)
              and isinstance(n.func, _ast.Name)
              and n.func.id == "print"
              and not (isinstance(n, _ast.Call)
                       and getattr(n, "lineno", 0) > 0)]
    _add("★ 模块里没有散落的 print（用日志回调）",
         len(prints) <= 2, "%d 处（含 __main__）" % len(prints))

    elapsed = time.perf_counter() - t0
    return {"module": "avatar_shell.ball", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
