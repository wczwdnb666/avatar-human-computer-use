# -*- coding: utf-8 -*-
"""屏幕捕获 —— 纯 ctypes + GDI，不依赖 Pillow。

## 为什么不用 PIL.ImageGrab

ImageGrab 内部也是 GDI，但它：
  · 是第三方依赖（本项目零 pip 依赖）；
  · 在 DPI 不感知的进程里会悄悄返回逻辑尺寸的图，而坐标是物理的；
  · 不支持 BitBlt 从窗口 DC 取图。

自己走 GDI 反而更短、更可控。

## 两个 DC 源

  · **屏幕 DC**（`GetDC(None)`）—— 抓屏上可见内容，包括其他窗口的遮挡。
    适合"我看到的就是这些"的语义。
  · **窗口 DC**（`GetWindowDC(hwnd)`）—— 抓窗口自身绘制内容。窗口被遮挡时
    仍能抓到自己的内容（部分场景），但有些用 DirectComposition 的应用会抓到黑的。

## 坐标系

所有 `grab_*` 的输入输出**都是物理像素**。返回值统一是 `Bitmap`：
自带 `width/height/origin`，`pixels` 是 BGRA 的 `bytearray`。

**为什么是 BGRA**：GDI 的 32 位 DIB 就是 BGRA 内存序，
少一次通道交换就少一次全图遍历 —— 4K 图一次遍历约 10ms，肉眼可感。
需要 RGB 时用 `.to_rgb_bytes()` 显式转换，让代价可见。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from .._win32 import api as A
from .._win32 import structs as S
from ..errors import UnsupportedError

# DIB 用的 32bpp 常量
BI_RGB = 0
DIB_RGB_COLORS = 0

# BitBlt 光栅操作码
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000  # 把分层窗口（半透明）也纳入捕获


@dataclass
class Bitmap:
    """一块 32 位 BGRA 位图。

    attributes:
        width, height: 像素尺寸
        origin: (x, y) 该位图左上角对应的**屏幕物理坐标**
        pixels: bytearray，长度 = width * height * 4，内存序 BGRA
        stride: 每行字节数（DIB 自下而上存储，由调用方按需翻转）
    """

    width: int
    height: int
    origin: tuple
    pixels: bytearray
    stride: int = 0

    def __post_init__(self):
        if not self.stride:
            self.stride = self.width * 4

    @property
    def size(self) -> tuple:
        return (self.width, self.height)

    # -- 像素访问 ---------------------------------------------------------
    def _offset(self, x: int, y: int) -> int:
        """DIB 是自下而上存储：第 0 行在缓冲区末尾。"""
        return ((self.height - 1 - y) * self.stride) + x * 4

    def get_pixel(self, x: int, y: int) -> Optional[tuple]:
        """返回 (r, g, b)。越界返回 None。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        o = self._offset(x, y)
        b = self.pixels[o]
        g = self.pixels[o + 1]
        r = self.pixels[o + 2]
        return (r, g, b)

    def get_pixel_bgra(self, x: int, y: int) -> Optional[tuple]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        o = self._offset(x, y)
        return (self.pixels[o], self.pixels[o + 1], self.pixels[o + 2], self.pixels[o + 3])

    def to_rgb_bytes(self) -> bytes:
        """转成 RGB 顺序的 bytes（去掉 alpha）。给需要 RGB 的下游用。"""
        out = bytearray(self.width * self.height * 3)
        src = self.pixels
        stride = self.stride
        h = self.height
        w = self.width
        j = 0
        for y in range(h):
            row = (h - 1 - y) * stride
            for x in range(w):
                o = row + x * 4
                out[j] = src[o + 2]
                out[j + 1] = src[o + 1]
                out[j + 2] = src[o]
                j += 3
        return bytes(out)

    def region(self, x: int, y: int, w: int, h: int) -> "Bitmap":
        """按位图内坐标裁剪出一块新位图。"""
        x = max(0, min(self.width, x))
        y = max(0, min(self.height, y))
        w = max(0, min(self.width - x, w))
        h = max(0, min(self.height - y, h))
        buf = bytearray(w * h * 4)
        src = self.pixels
        stride = self.stride
        H = self.height
        for yy in range(h):
            s = (H - 1 - (y + yy)) * stride + x * 4
            d = (h - 1 - yy) * w * 4
            buf[d:d + w * 4] = src[s:s + w * 4]
        return Bitmap(
            width=w,
            height=h,
            origin=(self.origin[0] + x, self.origin[1] + y),
            pixels=buf,
        )

    def downsample(self, factor: int) -> "Bitmap":
        """整数倍降采样（取块均值）。用于快速比对 / 生成缩略图。

        不做插值 —— 目的是降低数据量，不是美化图像。
        """
        if factor <= 1:
            return self
        nw = max(1, self.width // factor)
        nh = max(1, self.height // factor)
        buf = bytearray(nw * nh * 4)
        src = self.pixels
        stride = self.stride
        H = self.height
        f = factor
        area = f * f
        for yy in range(nh):
            for xx in range(nw):
                r = g = b = 0
                for dy in range(f):
                    row = (H - 1 - (yy * f + dy)) * stride
                    for dx in range(f):
                        o = row + (xx * f + dx) * 4
                        b += src[o]
                        g += src[o + 1]
                        r += src[o + 2]
                d = (nh - 1 - yy) * nw * 4 + xx * 4
                buf[d] = b // area
                buf[d + 1] = g // area
                buf[d + 2] = r // area
                buf[d + 3] = 255
        return Bitmap(width=nw, height=nh, origin=self.origin, pixels=buf)

    def as_rgb_rows(self):
        """逐行产出 RGB bytes（自上而下）。给模板匹配、编码器用。"""
        for y in range(self.height):
            row = (self.height - 1 - y) * self.stride
            line = bytearray(self.width * 3)
            j = 0
            for x in range(self.width):
                o = row + x * 4
                line[j] = self.pixels[o + 2]
                line[j + 1] = self.pixels[o + 1]
                line[j + 2] = self.pixels[o]
                j += 3
            yield bytes(line)


# --------------------------------------------------------------------------
# 内部：建一个可读写的 DIB section 并绑到内存
# --------------------------------------------------------------------------
class _Dib:
    """RAII 风格的 DIB 缓冲区。with 块结束后自动释放 GDI 对象。"""

    def __init__(self, width: int, height: int):
        self.width = int(width)
        self.height = int(height)
        self.hdc = None
        self.hbmp = None
        self.old = None
        self.bits = ctypes.c_void_p()
        self._info = S.BITMAPINFO()
        self._info.bmiHeader.biSize = ctypes.sizeof(S.BITMAPINFOHEADER)
        self._info.bmiHeader.biWidth = self.width
        # 负高度 = 自上而下存储。但为了和 GetDIBits 的默认行为一致、
        # 避免不同驱动差异，这里用正高度（自下而上），由 Bitmap 负责翻转。
        self._info.bmiHeader.biHeight = self.height
        self._info.bmiHeader.biPlanes = 1
        self._info.bmiHeader.biBitCount = 32
        self._info.bmiHeader.biCompression = BI_RGB

    def __enter__(self):
        screen_dc = A.user32.GetDC(None)
        if not screen_dc:
            raise UnsupportedError("GetDC 失败，无法建 DIB", capability="capture")
        self.hdc = A.gdi32.CreateCompatibleDC(screen_dc)
        A.user32.ReleaseDC(None, screen_dc)
        if not self.hdc:
            raise UnsupportedError("CreateCompatibleDC 失败", capability="capture")

        self.hbmp = A.gdi32.CreateDIBSection(
            self.hdc,
            ctypes.byref(self._info),
            DIB_RGB_COLORS,
            ctypes.byref(self.bits),
            None,
            0,
        )
        if not self.hbmp:
            A.gdi32.DeleteDC(self.hdc)
            self.hdc = None
            raise UnsupportedError("CreateDIBSection 失败", capability="capture")

        self.old = A.gdi32.SelectObject(self.hdc, self.hbmp)
        return self

    def __exit__(self, *exc):
        if self.hdc and self.old:
            A.gdi32.SelectObject(self.hdc, self.old)
        if self.hbmp:
            A.gdi32.DeleteObject(self.hbmp)
        if self.hdc:
            A.gdi32.DeleteDC(self.hdc)
        return False

    def read(self) -> bytearray:
        """把 DIB 内存拷成 bytearray。"""
        n = self.width * self.height * 4
        if not self.bits:
            return bytearray(n)
        return bytearray(ctypes.string_at(self.bits, n))


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
def grab_screen(region: Optional[tuple] = None, include_layered: bool = True) -> Bitmap:
    """抓取屏幕。

    region: (left, top, width, height)，**物理像素**，相对虚拟桌面原点。
            省略则抓整个虚拟桌面（多屏并集）。

    ⚠️ 参数顺序是 (x, y, w, h) —— 不是 PIL 的 (left, top, right, bottom)。
    这个差异曾导致过一次坐标反了的崩溃，特此标注。
    """
    if region is None:
        left, top, width, height = A.user32.GetSystemMetrics(S.SM_XVIRTUALSCREEN), \
            A.user32.GetSystemMetrics(S.SM_YVIRTUALSCREEN), \
            A.user32.GetSystemMetrics(S.SM_CXVIRTUALSCREEN), \
            A.user32.GetSystemMetrics(S.SM_CYVIRTUALSCREEN)
    else:
        left, top, width, height = (int(v) for v in region)

    if width <= 0 or height <= 0:
        raise UnsupportedError(
            "抓屏区域尺寸非法: %dx%d" % (width, height), capability="capture"
        )

    flags = SRCCOPY | (CAPTUREBLT if include_layered else 0)

    with _Dib(width, height) as dib:
        screen_dc = A.user32.GetDC(None)
        if not screen_dc:
            raise UnsupportedError("GetDC(None) 失败", capability="capture")
        try:
            ok = A.gdi32.BitBlt(
                dib.hdc, 0, 0, width, height, screen_dc, left, top, flags
            )
            if not ok:
                raise UnsupportedError(
                    "BitBlt 失败 (err=%d)" % A.last_error(), capability="capture"
                )
        finally:
            A.user32.ReleaseDC(None, screen_dc)
        px = dib.read()

    return Bitmap(width=width, height=height, origin=(left, top), pixels=px)


def grab_window(hwnd: int, client_only: bool = False, include_layered: bool = True) -> Bitmap:
    """抓取指定窗口。

    client_only=True 时只抓客户区（不含标题栏和边框），
    并且 origin 会对齐到客户区左上角的屏幕坐标 —— 这样从这张图里
    量出来的坐标可以直接喂给鼠标动作，不需要再加偏移。
    """
    hwnd = int(hwnd)
    if not A.user32.IsWindow(hwnd):
        raise UnsupportedError("无效窗口句柄 %r" % hwnd, capability="capture")

    if client_only:
        rc = S.RECT()
        if not A.user32.GetClientRect(hwnd, ctypes.byref(rc)):
            raise UnsupportedError("GetClientRect 失败", capability="capture")
        w, h = rc.width, rc.height
        pt = S.POINT(0, 0)
        A.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        left, top = pt.x, pt.y
        frame = None
    else:
        rc = S.RECT()
        if not A.user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            raise UnsupportedError("GetWindowRect 失败", capability="capture")
        # DWM 扩展边框更贴近肉眼看到的窗口边界（Aero 阴影会被 GetWindowRect 多算）
        frame = _dwm_frame(hwnd)
        left, top, w, h = frame or (rc.left, rc.top, rc.width, rc.height)

    if w <= 0 or h <= 0:
        raise UnsupportedError("窗口尺寸非法: %dx%d" % (w, h), capability="capture")

    flags = SRCCOPY | (CAPTUREBLT if include_layered else 0)

    with _Dib(w, h) as dib:
        if client_only:
            # 客户区不在窗口 DC 的原点（左边有边框、上边有标题栏），
            # 用 GetDC(hwnd) 的客户区 DC 更省事 —— 它的原点是客户区左上角。
            hdc = A.user32.GetDC(hwnd)
            if not hdc:
                raise UnsupportedError("GetDC(hwnd) 失败", capability="capture")
            try:
                ok = A.gdi32.BitBlt(dib.hdc, 0, 0, w, h, hdc, 0, 0, flags)
            finally:
                A.user32.ReleaseDC(hwnd, hdc)
        else:
            hdc = A.user32.GetWindowDC(hwnd)
            if not hdc:
                raise UnsupportedError("GetWindowDC 失败", capability="capture")
            if frame is not None:
                # 用 DWM 边框时，GetWindowDC 的原点（含阴影的窗口左上角）
                # 与 DWM 边框坐标对不上，所以改从屏幕 DC 按屏幕坐标拷，
                # 保证 origin 与像素一一对应。
                A.user32.ReleaseDC(hwnd, hdc)
                screen_dc = A.user32.GetDC(None)
                if not screen_dc:
                    raise UnsupportedError("GetDC(None) 失败", capability="capture")
                try:
                    ok = A.gdi32.BitBlt(
                        dib.hdc, 0, 0, w, h, screen_dc, left, top, flags
                    )
                finally:
                    A.user32.ReleaseDC(None, screen_dc)
            else:
                try:
                    ok = A.gdi32.BitBlt(dib.hdc, 0, 0, w, h, hdc, 0, 0, flags)
                finally:
                    A.user32.ReleaseDC(hwnd, hdc)

        if not ok:
            raise UnsupportedError("BitBlt(window) 失败", capability="capture")
        px = dib.read()

    return Bitmap(width=w, height=h, origin=(left, top), pixels=px)


def grab_monitor(index: int = 0, work_area_only: bool = False) -> Bitmap:
    """抓某块显示器。index 按 list_monitors() 的顺序。"""
    from .._win32.dpi import list_monitors

    mons = list_monitors()
    if not mons:
        raise UnsupportedError("未检测到显示器", capability="capture")
    if not (0 <= index < len(mons)):
        raise UnsupportedError(
            "显示器序号 %d 越界（共 %d 块）" % (index, len(mons)), capability="capture"
        )
    m = mons[index]
    rect = m.work if work_area_only else m.rect
    return grab_screen(rect)


def pixel_at(x: int, y: int) -> Optional[tuple]:
    """读单个物理像素，返回 (r, g, b)。

    GDI GetPixel 是最便宜的单点读取 —— 不建位图、不拷贝整屏。
    适合做轮询判断（例如"进度条变绿了没"）。
    返回 None 表示读不到（坐标越界或 DC 获取失败）。
    """
    hdc = A.user32.GetDC(None)
    if not hdc:
        return None
    try:
        v = A.gdi32.GetPixel(hdc, int(x), int(y))
        if v == 0xFFFFFFFF or v == -1:
            return None
        return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
    finally:
        A.user32.ReleaseDC(None, hdc)


def pixel_at_batch(points) -> list:
    """批量读像素。共用一次 DC 获取，比逐个调用省 N-1 次 GetDC。

    points: 可迭代的 (x, y)。返回同样顺序的 [(r,g,b) | None]。
    """
    hdc = A.user32.GetDC(None)
    if not hdc:
        return [None] * len(list(points)) if not isinstance(points, (list, tuple)) else [None] * len(points)
    out = []
    try:
        for x, y in points:
            v = A.gdi32.GetPixel(hdc, int(x), int(y))
            if v == 0xFFFFFFFF or v == -1:
                out.append(None)
            else:
                out.append((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
    finally:
        A.user32.ReleaseDC(None, hdc)
    return out


def average_color(bmp: Bitmap) -> tuple:
    """整图平均色 (r, g, b)。用于粗判"这块区域是不是黑了/白了"。"""
    px = bmp.pixels
    n = len(px) // 4
    if n == 0:
        return (0, 0, 0)
    r = g = b = 0
    # 步进采样加速：整图逐像素在 4K 上要 800 万次循环，太慢
    step = 4
    for i in range(0, len(px) - 4, 4 * step):
        b += px[i]
        g += px[i + 1]
        r += px[i + 2]
    cnt = max(1, len(range(0, len(px) - 4, 4 * step)))
    return (r // cnt, g // cnt, b // cnt)


def is_blank(bmp: Bitmap, tolerance: int = 6) -> bool:
    """判断图是否纯色（全黑 / 全白）。

    典型用途：抓一个窗口，发现全黑 → 该窗口用了 DirectComposition 或
    正在最小化，BitBlt 抓不到，应触发降级。
    """
    px = bmp.pixels
    if len(px) < 8:
        return True
    b0, g0, r0 = px[0], px[1], px[2]
    step = 4
    for i in range(0, len(px) - 4, 4 * step):
        if (abs(px[i] - b0) > tolerance
                or abs(px[i + 1] - g0) > tolerance
                or abs(px[i + 2] - r0) > tolerance):
            return False
    return True


def _dwm_frame(hwnd: int) -> Optional[tuple]:
    """取 DWM 认为的窗口实际可见边框 (left, top, w, h)。

    GetWindowRect 会把 Aero 的不可见阴影算进去 —— 视觉上窗口比它小一圈。
    要点的准确，就得用 DWMWA_EXTENDED_FRAME_BOUNDS。
    """
    if A.dwmapi is None:
        return None
    rc = S.RECT()
    hr = A.dwmapi.DwmGetWindowAttribute(
        int(hwnd),
        A.DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rc),
        ctypes.sizeof(rc),
    )
    if not A.hresult_ok(hr):
        return None
    w = rc.width
    h = rc.height
    if w <= 0 or h <= 0:
        return None
    return (rc.left, rc.top, w, h)
