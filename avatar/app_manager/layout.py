# -*- coding: utf-8 -*-
"""窗口几何与坐标转换 —— 全链路坐标一致性的落地点。

职责
----
· ``Rect``：不可变矩形值对象，带坐标空间转换
· 窗口三种矩形的读取：带阴影的 / DWM 可见的 / 客户区
· 客户区 ↔ 屏幕 坐标双向换算
· 边框厚度测量
· 所在显示器与缩放系数

依赖
----
· ``avatar._win32.api``（user32 / dwmapi 绑定）
· ``avatar._win32.structs``
· ``avatar._win32.dpi``（显示器信息）
· ``avatar.errors``

## 坐标空间的三个层次（全链路约定）

本项目对坐标的处理是**严格分层**的，任何跨层传递都必须显式换算：

```
   ① 屏幕物理坐标 (screen)     ← 唯一真值，阶段一确立
        ↕  layout.screen_to_client / client_to_screen
   ② 窗口客户区坐标 (client)   ← ★ 元素对象只持有这一层
        ↕  (无)  客户区内相对坐标就是它
   ③ 窗口内相对坐标 (relative) ← 部分场景（如相对于某控件）才用
```

**为什么要这么分**：屏幕坐标在窗口移动后立刻失效，而客户区坐标
不受窗口位置影响。让元素对象只持有客户区坐标，窗口一动不用重新查找元素，
只需重新取一次客户区原点。这个设计把"元素失效"的概率降到最低。

**一个必须警惕的坑**：`GrabWindow` 之类 API 的 bbox 参数是
``(left, top, right, bottom)``，而本模块一律用 ``Rect`` 值对象，
不再裸传四元组 —— 顺序错误是这类代码最常见的 bug 来源。
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Optional, Tuple

from .._win32 import api as A
from .._win32 import dpi as D
from .._win32 import structs as S
from ..errors import WindowNotFound

# --------------------------------------------------------------------------
# Rect
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """不可变矩形。坐标为 (left, top, right, bottom)，即**右下开区间**。

    与 PIL 的 bbox 语义一致（right/bottom 是右下一个像素的坐标，不含）。
    这样 ``width = right - left`` 不需要 +1，避免经典的"差一像素"问题。
    """

    left: int
    top: int
    right: int
    bottom: int

    # -- 构造 ------------------------------------------------------------
    @classmethod
    def from_xywh(cls, x: int, y: int, w: int, h: int) -> "Rect":
        """从 (x, y, 宽, 高) 构造。**注意与 from_ltrb 的区别**。"""
        return cls(int(x), int(y), int(x) + int(w), int(y) + int(h))

    @classmethod
    def from_ltrb(cls, l: int, t: int, r: int, b: int) -> "Rect":
        return cls(int(l), int(t), int(r), int(b))

    @classmethod
    def from_win32(cls, rc) -> "Rect":
        """从 ctypes RECT 结构体构造。"""
        return cls(int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))

    @classmethod
    def from_pointer_array(cls, values, offset: int = 0) -> "Rect":
        """从 ``double[4]`` 数组构造（UIA 的 GetBoundingRectangles 用）。

        UIA 返回的 double 数组是 ``[left, top, width, height]``，
        **不是** ltrb —— 这是 UIA 里少见的例外，必须显式转换。
        """
        l, t, w, h = (float(values[offset + i]) for i in range(4))
        return cls(int(round(l)), int(round(t)), int(round(l + w)), int(round(t + h)))

    # -- 属性 ------------------------------------------------------------
    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    @property
    def center(self) -> Tuple[int, int]:
        """中心点。用整数除法，奇数尺寸时偏左上 —— 对点击来说更安全
        （不会落到边界外）。"""
        return (self.left + self.width // 2, self.top + self.height // 2)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    # -- 谓词 ------------------------------------------------------------
    def contains(self, x: int, y: int, margin: int = 0) -> bool:
        return (self.left - margin <= x < self.right + margin
                and self.top - margin <= y < self.bottom + margin)

    def intersects(self, other: "Rect") -> bool:
        return not (other.left >= self.right or self.left >= other.right
                    or other.top >= self.bottom or self.top >= other.bottom)

    def intersection(self, other: "Rect") -> "Rect":
        l = max(self.left, other.left)
        t = max(self.top, other.top)
        r = min(self.right, other.right)
        b = min(self.bottom, other.bottom)
        if r <= l or b <= t:
            return Rect(0, 0, 0, 0)
        return Rect(l, t, r, b)

    # -- 变换 ------------------------------------------------------------
    def offset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.left + int(dx), self.top + int(dy),
                    self.right + int(dx), self.bottom + int(dy))

    def scaled(self, factor: float) -> "Rect":
        """按比例缩放（相对原点）。用于 DPI 换算。"""
        return Rect(int(round(self.left * factor)), int(round(self.top * factor)),
                    int(round(self.right * factor)), int(round(self.bottom * factor)))

    def inflate(self, dx: int, dy: int = None) -> "Rect":
        """向外扩张。dy 省略则与 dx 相同。"""
        if dy is None:
            dy = dx
        return Rect(self.left - int(dx), self.top - int(dy),
                    self.right + int(dx), self.bottom + int(dy))

    def clamp(self, bounds: "Rect") -> "Rect":
        """把矩形夹进 bounds 内（保持左上角坐标不动，只裁右下）。"""
        l = max(self.left, bounds.left)
        t = max(self.top, bounds.top)
        r = min(self.right, bounds.right)
        b = min(self.bottom, bounds.bottom)
        return Rect(l, t, max(l, r), max(t, b))

    # -- 坐标空间转换（需要窗口句柄）------------------------------------
    def to_client(self, hwnd: int) -> "Rect":
        """屏幕坐标 → 客户区坐标。"""
        ox, oy = client_origin(hwnd)
        return self.offset(-ox, -oy)

    def to_screen(self, hwnd: int) -> "Rect":
        """客户区坐标 → 屏幕坐标。"""
        ox, oy = client_origin(hwnd)
        return self.offset(ox, oy)

    # -- 输出 ------------------------------------------------------------
    def as_xywh(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def as_ltrb(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def to_win32(self) -> S.RECT:
        return S.RECT(self.left, self.top, self.right, self.bottom)

    def __iter__(self):
        return iter((self.left, self.top, self.right, self.bottom))

    def __str__(self):
        return "(%d,%d %dx%d)" % (self.left, self.top, self.width, self.height)


# --------------------------------------------------------------------------
# 窗口矩形
# --------------------------------------------------------------------------
def raw_window_rect(hwnd: int) -> Rect:
    """``GetWindowRect`` —— **含 Aero 阴影**，比肉眼看到的窗口大一圈。

    只在需要"系统认为的窗口边界"时用。要点准用 ``window_rect``。
    """
    _ensure_window(hwnd)
    rc = S.RECT()
    if not A.user32.GetWindowRect(int(hwnd), ctypes.byref(rc)):
        raise WindowNotFound("GetWindowRect 失败", hwnd=int(hwnd))
    return Rect.from_win32(rc)


def window_rect(hwnd: int) -> Rect:
    """窗口的**可见**边框 —— DWM 扩展边框，等于肉眼看到的。

    这是"点窗口标题栏/边框"应该用的矩形。DWM 不可用时退回 GetWindowRect。
    """
    _ensure_window(hwnd)
    if A.dwmapi is not None:
        rc = S.RECT()
        hr = A.dwmapi.DwmGetWindowAttribute(
            int(hwnd), A.DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rc), ctypes.sizeof(rc),
        )
        if A.hresult_ok(hr) and rc.width > 0 and rc.height > 0:
            return Rect.from_win32(rc)
    return raw_window_rect(hwnd)


def client_rect(hwnd: int, screen_coords: bool = True) -> Rect:
    """客户区矩形。

    screen_coords=True（默认）时返回**屏幕坐标**的客户区矩形；
    False 时返回以 (0,0) 为原点的纯尺寸矩形。
    """
    _ensure_window(hwnd)
    rc = S.RECT()
    if not A.user32.GetClientRect(int(hwnd), ctypes.byref(rc)):
        raise WindowNotFound("GetClientRect 失败", hwnd=int(hwnd))
    if not screen_coords:
        return Rect.from_win32(rc)
    ox, oy = client_origin(hwnd)
    return Rect(ox, oy, ox + rc.width, oy + rc.height)


def client_size(hwnd: int) -> Tuple[int, int]:
    """客户区尺寸 (宽, 高)。"""
    return client_rect(hwnd, screen_coords=False).size


def client_origin(hwnd: int) -> Tuple[int, int]:
    """客户区左上角的**屏幕物理坐标**，即 ``ClientToScreen(hwnd, (0,0))``。

    这是整个坐标换算的中心点。所有"客户区 ↔ 屏幕"的转换都靠它。
    """
    _ensure_window(hwnd)
    pt = S.POINT(0, 0)
    if not A.user32.ClientToScreen(int(hwnd), ctypes.byref(pt)):
        raise WindowNotFound("ClientToScreen 失败", hwnd=int(hwnd))
    return (int(pt.x), int(pt.y))


def client_to_screen(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """客户区坐标 → 屏幕物理坐标。"""
    _ensure_window(hwnd)
    pt = S.POINT(int(x), int(y))
    if not A.user32.ClientToScreen(int(hwnd), ctypes.byref(pt)):
        raise WindowNotFound("ClientToScreen 失败", hwnd=int(hwnd))
    return (int(pt.x), int(pt.y))


def screen_to_client(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """屏幕物理坐标 → 客户区坐标。"""
    _ensure_window(hwnd)
    pt = S.POINT(int(x), int(y))
    if not A.user32.ScreenToClient(int(hwnd), ctypes.byref(pt)):
        raise WindowNotFound("ScreenToClient 失败", hwnd=int(hwnd))
    return (int(pt.x), int(pt.y))


def in_client(hwnd: int, x: int, y: int, margin: int = 0) -> bool:
    """屏幕坐标是否落在客户区（客户区坐标系里判断，避免边框干扰）。"""
    cx, cy = screen_to_client(hwnd, x, y)
    w, h = client_size(hwnd)
    return (-margin <= cx < w + margin) and (-margin <= cy < h + margin)


def client_center(hwnd: int) -> Tuple[int, int]:
    """客户区中心的屏幕坐标。"""
    return client_rect(hwnd).center


# --------------------------------------------------------------------------
# 边框厚度
# --------------------------------------------------------------------------
def frame_thickness(hwnd: int) -> Tuple[int, int, int, int]:
    """测量窗口边框厚度，返回 ``(顶部, 左, 右, 底部)``。

    用途：从"可见窗口矩形"推算客户区，或在需要知道标题栏高度时使用
    （点标题栏拖动、识别"这是不是无边框窗口"）。

    算法：对比 DWM 可见边框与客户区的相对位置。
    比用 ``GetSystemMetrics(SM_CYCAPTION)`` 准 —— 后者只反映系统默认值，
    不反映应用自定义的非客户区（很多现代应用是自绘的）。
    """
    frame = window_rect(hwnd)
    cli = client_rect(hwnd)
    top = cli.top - frame.top
    left = cli.left - frame.left
    right = frame.right - cli.right
    bottom = frame.bottom - cli.bottom
    # 自绘窗口可能得到负数或荒谬值，夹到合理范围
    return (max(0, top), max(0, left), max(0, right), max(0, bottom))


def titlebar_height(hwnd: int) -> int:
    """标题栏高度（物理像素）。无边框窗口返回 0。"""
    return frame_thickness(hwnd)[0]


def has_border(hwnd: int) -> bool:
    """是否是有边框窗口。"""
    t, l, r, b = frame_thickness(hwnd)
    return (t + l + r + b) > 0


# --------------------------------------------------------------------------
# 与显示器 / 缩放的关系
# --------------------------------------------------------------------------
def monitor_of(hwnd: int) -> Optional[D.DpiInfo]:
    """窗口所在的显示器。"""
    _ensure_window(hwnd)
    hmon = A.user32.MonitorFromWindow(int(hwnd), A.MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = S.MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(S.MONITORINFOEXW)
    if not A.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    device = info.szDevice or ""
    for m in D.list_monitors():
        if m.device == device:
            return m
    # 退化：自己拼一个
    dx, dy = D._dpi_for_monitor(hmon)
    pw, ph, hz, bpp = D._display_mode(device)
    return D.DpiInfo(
        monitor_index=-1, device=device,
        rect=(info.rcMonitor.left, info.rcMonitor.top,
              info.rcMonitor.width, info.rcMonitor.height),
        work=(info.rcWork.left, info.rcWork.top,
              info.rcWork.width, info.rcWork.height),
        is_primary=bool(info.dwFlags & S.MONITORINFOF_PRIMARY),
        dpi_x=dx, dpi_y=dy, scale_x=dx / D.BASE_DPI, scale_y=dy / D.BASE_DPI,
        physical_width=pw or info.rcMonitor.width,
        physical_height=ph or info.rcMonitor.height,
        refresh_hz=hz, bits_per_pel=bpp,
    )


def window_dpi(hwnd: int) -> int:
    """窗口自身的 DPI（``GetDpiForWindow``）。取不到时返回系统 DPI。"""
    _ensure_window(hwnd)
    d = int(A.user32.GetDpiForWindow(int(hwnd)))
    if d > 0:
        return d
    d = int(A.user32.GetDpiForSystem())
    return d if d > 0 else 96


def window_scale(hwnd: int) -> float:
    """窗口的缩放系数（1.0 = 100%）。"""
    return window_dpi(hwnd) / D.BASE_DPI


def is_window_on_primary(hwnd: int) -> bool:
    m = monitor_of(hwnd)
    return bool(m and m.is_primary)


# --------------------------------------------------------------------------
# 几何诊断
# --------------------------------------------------------------------------
def describe(hwnd: int) -> str:
    """窗口几何的一页纸摘要。给 CLI 和调试用。"""
    raw = raw_window_rect(hwnd)
    frame = window_rect(hwnd)
    cli = client_rect(hwnd)
    t = frame_thickness(hwnd)
    mon = monitor_of(hwnd)
    return "\n".join([
        "带阴影矩形 : %s   ← GetWindowRect，比肉眼大" % raw,
        "可见边框   : %s   ← DWM，点这个才准" % frame,
        "客户区     : %s   ← 元素坐标的基准" % cli,
        "边框厚度   : 上%d 左%d 右%d 下%d" % t,
        "客户区中心 : %s" % (cli.center,),
        "窗口 DPI   : %d (%d%%)" % (window_dpi(hwnd), int(window_scale(hwnd) * 100)),
        "所在显示器 : %s" % (("[%d] %s %d%%" % (mon.monitor_index, mon.device, mon.scale_percent))
                            if mon else "(未知)"),
    ])


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _ensure_window(hwnd) -> None:
    if not hwnd or not A.user32.IsWindow(int(hwnd)):
        raise WindowNotFound("窗口句柄无效", hwnd=int(hwnd or 0))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """几何层的往返一致性自检 —— 在 200% 缩放下必须全过。

    核心两项：
      1. ``ClientToScreen`` 与 ``ScreenToClient`` 互为逆运算
      2. ``client_origin`` 与 ``client_rect().left/top`` 一致
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 找一个可用窗口做测试
    from .windows import list_windows
    ws = [w for w in list_windows() if w.is_usable]
    if not ws:
        _add("存在可用窗口", False, "没有可操作的窗口可供几何测试")
        return {"checks": checks, "failures": failures, "ok": False}

    hwnd = ws[0].hwnd
    try:
        ox, oy = client_origin(hwnd)
        cr = client_rect(hwnd)
        _add("client_origin == client_rect 左上角",
             (ox, oy) == (cr.left, cr.top),
             "origin=(%d,%d) rect.left/top=(%d,%d)" % (ox, oy, cr.left, cr.top))

        w, h = client_size(hwnd)
        _add("客户区尺寸为正", w > 0 and h > 0, "%dx%d" % (w, h))

        # 往返
        ok = True
        detail = ""
        for pt in [(0, 0), (1, 1), (w // 2, h // 2), (max(0, w - 1), max(0, h - 1))]:
            sx, sy = client_to_screen(hwnd, *pt)
            bx, by = screen_to_client(hwnd, sx, sy)
            if (bx, by) != pt:
                ok = False
                detail = "客户区%s → 屏幕(%d,%d) → 客户区%s 不一致" % (pt, sx, sy, (bx, by))
                break
        _add("ClientToScreen/ScreenToClient 往返一致", ok, detail or "4 个采样点全部还原")

        # Rect 往返
        r0 = Rect.from_xywh(10, 20, 100, 50)
        r1 = r0.to_screen(hwnd).to_client(hwnd)
        _add("Rect 客户区↔屏幕 往返一致", r0 == r1, "%s → %s" % (r0, r1))

        # 边框厚度合理
        t, l, rr, b = frame_thickness(hwnd)
        sane = t < 500 and l < 500 and rr < 500 and b < 500
        _add("边框厚度在合理范围", sane, "上%d 左%d 右%d 下%d" % (t, l, rr, b))

        # 可见边框应不大于带阴影矩形
        raw = raw_window_rect(hwnd)
        frame = window_rect(hwnd)
        _add("可见边框 <= 带阴影矩形",
             frame.width <= raw.width + 2 and frame.height <= raw.height + 2,
             "raw=%s frame=%s" % (raw, frame))

        # 客户区应被可见边框包含
        _add("客户区在窗口内",
             frame.contains(cr.left, cr.top) and cr.right <= frame.right + 1,
             "frame=%s client=%s" % (frame, cr))

    except Exception as e:
        _add("几何测试执行", False, "异常: %r" % (e,))

    return {"checks": checks, "failures": failures, "ok": not failures}
