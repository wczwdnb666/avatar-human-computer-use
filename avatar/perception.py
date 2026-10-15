# -*- coding: utf-8 -*-
"""底层感知 —— 把 DPI / 显示器 / 窗口 / 坐标收敛成**一个一致视图**。

## 这个模块解决的问题

在写字号化的自动化代码时，最容易出的错不是"API 调不通"，而是
**同一个屏幕在不同地方被理解成不同的尺寸**：

    GetSystemMetrics 说 1440x900
    ImageGrab 出来的图是 2880x1800
    GetCursorPos 返回 800（在主屏中间还是 1/4 处？）
    UIA 的 BoundingRectangle 给的是 1200

四个数字，四套坐标系，混起来点，结果全错。而且不报错。

本模块的规矩只有一条：**物理像素是唯一真值，其他都是派生量。**
所有对外接口（`Screen`、`Snapshot`）都以物理像素为单位，
换算只在 `to_physical` / `to_logical` 两个函数里发生，且有明确命名。
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import Optional

from ._win32 import api as A
from ._win32 import dpi as D
from ._win32 import structs as S
from .errors import DpiSetupError, WindowNotFound
from .vision.capture import Bitmap, average_color, grab_screen, is_blank, pixel_at


# --------------------------------------------------------------------------
# 显示器布局
# --------------------------------------------------------------------------
@dataclass
class MonitorLayout:
    """全部显示器的空间布局快照。

    这是"全域分辨率"的落地：不是"我的屏幕是 2K"，
    而是"我有 3 块屏，坐标空间是这些矩形的并集，各自缩放比不同"。
    """

    monitors: tuple
    virtual_origin: tuple
    virtual_size: tuple
    primary: Optional[D.DpiInfo]

    @property
    def count(self) -> int:
        return len(self.monitors)

    @property
    def is_multi(self) -> bool:
        return len(self.monitors) > 1

    @property
    def mixed_scaling(self) -> bool:
        """各屏缩放比是否不一致 —— 混合 DPI 是最容易翻车的场景。"""
        if len(self.monitors) < 2:
            return False
        first = round(self.monitors[0].scale_x, 4)
        return any(abs(round(m.scale_x, 4) - first) > 1e-6 for m in self.monitors[1:])

    def containing(self, x: int, y: int) -> Optional[D.DpiInfo]:
        for m in self.monitors:
            mx, my, mw, mh = m.rect
            if mx <= x < mx + mw and my <= y < my + mh:
                return m
        return None

    def index_of(self, x: int, y: int) -> int:
        m = self.containing(x, y)
        return m.monitor_index if m else -1

    def scale_at(self, x: int, y: int) -> float:
        m = self.containing(x, y)
        return m.scale_x if m else 1.0

    @property
    def max_scale(self) -> float:
        return max((m.scale_x for m in self.monitors), default=1.0)

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "multi": self.is_multi,
            "mixed_scaling": self.mixed_scaling,
            "virtual_origin": self.virtual_origin,
            "virtual_size": self.virtual_size,
            "monitors": [
                {
                    "index": m.monitor_index,
                    "device": m.device,
                    "rect": m.rect,
                    "work": m.work,
                    "primary": m.is_primary,
                    "dpi": (m.dpi_x, m.dpi_y),
                    "scale_percent": m.scale_percent,
                    "physical": (m.physical_width, m.physical_height),
                    "refresh": m.refresh_hz,
                    "uniform": m.is_uniform_scale,
                }
                for m in self.monitors
            ],
        }

    def describe(self) -> str:
        lines = [
            "虚拟桌面: origin=(%d,%d) size=%dx%d  显示器 %d 块%s"
            % (
                self.virtual_origin[0],
                self.virtual_origin[1],
                self.virtual_size[0],
                self.virtual_size[1],
                self.count,
                "  ⚠ 混合缩放" if self.mixed_scaling else "",
            )
        ]
        for m in self.monitors:
            x, y, w, h = m.rect
            wx, wy, ww, wh = m.work
            lines.append(
                "  [%d]%s %-14s %dx%d @(%d,%d)  %d%%  %dHz  %dbpp  工作区 %dx%d"
                % (
                    m.monitor_index,
                    "*" if m.is_primary else " ",
                    m.device or "(未知)",
                    m.physical_width,
                    m.physical_height,
                    x,
                    y,
                    m.scale_percent,
                    m.refresh_hz,
                    m.bits_per_pel,
                    ww,
                    wh,
                )
            )
        return "\n".join(lines)


def layout() -> MonitorLayout:
    """当前显示器布局（每次都重新探测 —— 支持热插拔）。"""
    mons = D.list_monitors()
    vx, vy, vw, vh = D.virtual_screen()
    primary = next((m for m in mons if m.is_primary), mons[0] if mons else None)
    return MonitorLayout(
        monitors=tuple(mons),
        virtual_origin=(vx, vy),
        virtual_size=(vw, vh),
        primary=primary,
    )


# --------------------------------------------------------------------------
# 窗口几何
# --------------------------------------------------------------------------
@dataclass
class WindowGeometry:
    """一个窗口的全部几何信息，物理像素。

    三种矩形各有用途，别混：
      · rect     —— GetWindowRect，**含 Aero 阴影**，比肉眼看到的大一圈。
      · frame    —— DWM 扩展边框，**等于肉眼看到的窗口边界**。要"点准"就用它。
      · client   —— 客户区（不含标题栏/边框），**屏内内容坐标的原点**。
    """

    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    rect: tuple          # (left, top, w, h) 含阴影
    frame: tuple         # (left, top, w, h) DWM 可见边框
    client: tuple        # (left, top, w, h) 客户区，屏幕坐标
    client_size: tuple   # (w, h) 纯客户区尺寸
    dpi: int
    scale: float
    monitor_index: int
    visible: bool
    minimized: bool
    maximized: bool

    @property
    def center(self) -> tuple:
        x, y, w, h = self.frame
        return (x + w // 2, y + h // 2)

    def client_to_screen(self, cx: int, cy: int) -> tuple:
        """客户区内坐标 → 屏幕物理坐标。

        这一步是必须的：UIA 报的元素坐标是屏幕坐标，但如果是从
        截图里量出来的、或者用窗口相对坐标指定的，就落在客户坐标系里。
        """
        return (self.client[0] + int(cx), self.client[1] + int(cy))

    def screen_to_client(self, sx: int, sy: int) -> tuple:
        return (int(sx) - self.client[0], int(sy) - self.client[1])

    def contains_screen_point(self, sx: int, sy: int) -> bool:
        x, y, w, h = self.frame
        return x <= sx < x + w and y <= sy < y + h

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class": self.class_name,
            "pid": self.pid,
            "process": self.process,
            "rect": self.rect,
            "frame": self.frame,
            "client": self.client,
            "client_size": self.client_size,
            "dpi": self.dpi,
            "scale": round(self.scale, 4),
            "scale_percent": int(round(self.scale * 100)),
            "monitor_index": self.monitor_index,
            "visible": self.visible,
            "minimized": self.minimized,
            "maximized": self.maximized,
        }


def _process_name(pid: int) -> str:
    """pid → 可执行文件名（如 msedge.exe）。取不到返回空串。

    不导入 psutil（零依赖），走 kernel32 + QueryFullProcessImageNameW。
    """
    if pid <= 0:
        return ""
    h = A.kernel32.OpenProcess(A.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint(1024)
        if A.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            import os
            return os.path.basename(buf.value)
        return ""
    finally:
        A.kernel32.CloseHandle(h)


def geometry(hwnd: int) -> WindowGeometry:
    """取窗口完整几何。所有值为物理像素。"""
    hwnd = int(hwnd)
    if not A.user32.IsWindow(hwnd):
        raise WindowNotFound("窗口句柄无效", hwnd=hwnd)

    def _text():
        n = A.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        A.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _cls():
        buf = ctypes.create_unicode_buffer(256)
        A.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    wrc = S.RECT()
    if not A.user32.GetWindowRect(hwnd, ctypes.byref(wrc)):
        raise WindowNotFound("GetWindowRect 失败", hwnd=hwnd)

    from .vision.capture import _dwm_frame
    frame = _dwm_frame(hwnd) or wrc.as_tuple()

    crc = S.RECT()
    A.user32.GetClientRect(hwnd, ctypes.byref(crc))
    origin = S.POINT(0, 0)
    A.user32.ClientToScreen(hwnd, ctypes.byref(origin))
    client = (origin.x, origin.y, crc.width, crc.height)

    pid = S.DWORD(0)
    A.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    wdpi = int(A.user32.GetDpiForWindow(hwnd)) or 96
    hmon = A.user32.MonitorFromWindow(hwnd, A.MONITOR_DEFAULTTONEAREST)
    mon_idx = -1
    mon_scale = wdpi / D.BASE_DPI
    if hmon:
        for i, m in enumerate(D.list_monitors()):
            mx, my, mw, mh = m.rect
            cx, cy, cw, ch = client
            if mx <= cx < mx + mw and my <= cy < my + mh:
                mon_idx = i
                break

    return WindowGeometry(
        hwnd=hwnd,
        title=_text(),
        class_name=_cls(),
        pid=int(pid.value),
        process=_process_name(int(pid.value)),
        rect=wrc.as_tuple(),
        frame=frame,
        client=client,
        client_size=(crc.width, crc.height),
        dpi=wdpi,
        scale=wdpi / D.BASE_DPI,
        monitor_index=mon_idx,
        visible=bool(A.user32.IsWindowVisible(hwnd)),
        minimized=bool(A.user32.IsIconic(hwnd)),
        maximized=bool(A.user32.IsZoomed(hwnd)),
    )


def foreground_hwnd() -> int:
    return int(A.user32.GetForegroundWindow() or 0)


def foreground_geometry() -> Optional[WindowGeometry]:
    h = foreground_hwnd()
    if not h:
        return None
    try:
        return geometry(h)
    except WindowNotFound:
        return None


def cursor_position() -> tuple:
    """当前鼠标位置的**物理**坐标。"""
    p = S.POINT()
    A.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


# --------------------------------------------------------------------------
# 感知快照
# --------------------------------------------------------------------------
@dataclass
class Snapshot:
    """某一时刻的完整感知状态。

    `stamp` 是抓取时刻的 monotonic 时间。做状态比对时**必须**用它判断新旧 ——
    不能用两次独立抓取的结果比较，中间可能已经变了。
    """

    stamp: float
    layout: MonitorLayout
    cursor: tuple
    foreground: Optional[WindowGeometry]
    screen: Optional[Bitmap] = None
    note: str = ""

    @property
    def age(self) -> float:
        return time.monotonic() - self.stamp

    def pixel(self, x: int, y: int) -> Optional[tuple]:
        """读快照内的像素（物理坐标）。有图就用图，没图就现读。"""
        if self.screen is not None:
            ox, oy = self.screen.origin
            return self.screen.get_pixel(int(x) - ox, int(y) - oy)
        return pixel_at(x, y)

    def crop(self, x: int, y: int, w: int, h: int) -> Optional[Bitmap]:
        if self.screen is None:
            return None
        ox, oy = self.screen.origin
        return self.screen.region(int(x) - ox, int(y) - oy, w, h)

    def to_dict(self, with_screen_info: bool = True) -> dict:
        d = {
            "stamp": round(self.stamp, 3),
            "age_ms": round(self.age * 1000, 1),
            "cursor": self.cursor,
            "layout": self.layout.to_dict(),
            "foreground": self.foreground.to_dict() if self.foreground else None,
            "note": self.note,
        }
        if with_screen_info and self.screen is not None:
            bmp = self.screen
            d["screen"] = {
                "origin": bmp.origin,
                "size": bmp.size,
                "avg_color": average_color(bmp),
                "blank": is_blank(bmp),
            }
        return d


def snapshot(with_screen: bool = False, downsample: int = 1,
             region: Optional[tuple] = None) -> Snapshot:
    """抓一次完整感知。

    with_screen=True 时会把整屏（或 region）抓进内存 —— 4K 屏约 33 MB，
    不要在高频循环里调用。
    """
    st = time.monotonic()
    lay = layout()
    cur = cursor_position()
    fg = foreground_geometry()
    bmp = None
    note = ""
    if with_screen:
        bmp = grab_screen(region)
        if downsample > 1:
            bmp = bmp.downsample(downsample)
        if is_blank(bmp):
            note = "屏幕内容为纯色 —— 可能锁屏、显示器休眠，或 BitBlt 抓不到（DirectComposition）"
    return Snapshot(stamp=st, layout=lay, cursor=cur, foreground=fg, screen=bmp, note=note)


# --------------------------------------------------------------------------
# 坐标换算 —— 唯一允许换算发生的地方
# --------------------------------------------------------------------------
def to_physical(x: int, y: int) -> tuple:
    """逻辑像素 → 物理像素。只在对接非 DPI 感知的外部系统时用。"""
    return D.logical_to_physical(x, y)


def to_logical(x: int, y: int) -> tuple:
    """物理像素 → 逻辑像素。"""
    return D.physical_to_logical(x, y)


def normalize(x: int, y: int) -> tuple:
    """物理坐标 → SendInput 绝对坐标的 0..65535 归一化值。"""
    return D.normalize_absolute(x, y)


def clamp(x: int, y: int) -> tuple:
    return D.clamp_to_screen(x, y)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(verbose: bool = True) -> dict:
    """阶段一自检：把"底层感知"是否可信逐条验一遍。

    每一条都独立于其它条 —— 一条失败不会掩盖另一条。
    返回值里 `failures` 非空即表示感知层不可信，后续所有动作都不该执行。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. INPUT 结构体尺寸 —— SendInput 静默失败的头号原因
    actual = ctypes.sizeof(S.INPUT)
    expect = S.expected_input_size()
    _add("INPUT 结构体尺寸", actual == expect,
         "sizeof=%d 期望=%d %s" % (actual, expect, "(正确)" if actual == expect else "→ SendInput 会静默失败！"))

    # 2. 进程 DPI 感知
    st = D.set_process_dpi_awareness()
    _add("进程 DPI 感知", st.ok, str(st))

    # 3. 交叉验证（GetSystemMetrics vs EnumDisplaySettings）
    ok3 = D.verify()
    pm = D.primary_monitor()
    _add("DPI 交叉验证", ok3,
         "GetSystemMetrics=%dx%d / EnumDisplaySettings=%s"
         % (
             A.user32.GetSystemMetrics(S.SM_CXSCREEN),
             A.user32.GetSystemMetrics(S.SM_CYSCREEN),
             pm.physical_width if pm else "?",
         ) + ("x%d" % pm.physical_height if pm else ""))

    # 4. 显示器枚举
    lay = layout()
    _add("显示器枚举", lay.count >= 1,
         "%d 块%s" % (lay.count, "（混合缩放）" if lay.mixed_scaling else ""))

    # 5. 虚拟桌面覆盖所有显示器矩形（多屏时容易算错）
    vx, vy = lay.virtual_origin
    vw, vh = lay.virtual_size
    cover = True
    for m in lay.monitors:
        mx, my, mw, mh = m.rect
        if mx < vx or my < vy or mx + mw > vx + vw or my + mh > vy + vh:
            cover = False
    _add("虚拟桌面覆盖", cover, "origin=%s size=%s" % (lay.virtual_origin, lay.virtual_size))

    # 6. 坐标归一化在范围内
    nx, ny = normalize(*cursor_position())
    _add("坐标归一化", 0 <= nx <= 65535 and 0 <= ny <= 65535, "norm=(%d,%d)" % (nx, ny))

    # 7. 光标位置在某个显示器内
    cx, cy = cursor_position()
    _add("光标在屏内", lay.containing(cx, cy) is not None, "cursor=(%d,%d)" % (cx, cy))

    # 8. 抓屏可用且尺寸与预期一致
    try:
        bmp = grab_screen(lay.monitors[0].rect)
        size_ok = bmp.size == (lay.monitors[0].rect[2], lay.monitors[0].rect[3])
        _add("抓屏尺寸一致", size_ok,
             "抓到 %dx%d / 期望 %dx%d" % (bmp.width, bmp.height,
                                         lay.monitors[0].rect[2], lay.monitors[0].rect[3]))
        _add("抓屏非纯色", not is_blank(bmp),
             "均色=%s%s" % (average_color(bmp), "（纯色，可能锁屏）" if is_blank(bmp) else ""))
    except Exception as e:
        _add("抓屏可用", False, "异常: %r" % (e,))

    # 9. 单点像素读取
    try:
        px = pixel_at(cx, cy)
        _add("单点像素读取", px is not None, "cursor 处像素=%s" % (px,))
    except Exception as e:
        _add("单点像素读取", False, "异常: %r" % (e,))

    result = {
        "version": __import__("avatar").__version__,
        "checks": checks,
        "failures": failures,
        "ok": not failures,
    }

    if verbose:
        print(_format_selfcheck(result))
    return result


def _format_selfcheck(r: dict) -> str:
    lines = ["", "Avatar 阶段一自检", "=" * 62]
    for c in r["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        lines.append("[%s] %-16s %s" % (mark, c["name"], c["detail"]))
    lines.append("-" * 62)
    if r["ok"]:
        lines.append("全部通过。感知层可信，坐标可安全使用。")
    else:
        lines.append("失败项: %s" % ", ".join(r["failures"]))
        lines.append("⚠ 感知层不可信 —— 后续所有坐标动作都不应执行。")
    lines.append("")
    return "\n".join(lines)
