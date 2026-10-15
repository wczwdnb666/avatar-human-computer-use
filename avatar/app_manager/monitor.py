# -*- coding: utf-8 -*-
"""多显示器拓扑与虚拟屏坐标空间。

职责
----
把阶段一 ``_win32/dpi.py`` 的底层探测包装成面向使用的 API：
· 显示器列表 / 主屏 / 虚拟屏矩形
· 查询点 / 窗口属于哪块屏
· 工作区（扣任务栏）
· 相邻屏查找、把窗口移到指定屏

依赖
----
· ``avatar._win32.dpi``
· ``avatar.app_manager.layout``（Rect）
· ``avatar.errors``

## 虚拟屏坐标空间

多显示器时 Windows 提供一个**统一坐标空间**，原点在**最左上那块屏**的
左上角。副屏在主屏左侧或上方时，坐标会是**负数**。

```
        +-----------+---------------+
        |  DISPLAY2 |               |
        | (-1920,0) |  DISPLAY1     |
        |           |  (0,0) 主屏    |
        +-----------+---------------+
```

所以：
· 永远不要假设坐标 >= 0
· 归一化 SendInput 坐标必须用虚拟屏原点，不能用主屏尺寸（阶段一已做）
· ``neighbors()`` 用矩形相邻判断，而不是假设按索引排布
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .._win32 import api as A
from .._win32 import dpi as D
from .._win32 import structs as S
from .layout import Rect


@dataclass(frozen=True)
class Monitor:
    """一块显示器。坐标全部为物理像素，处于虚拟屏坐标空间。"""

    index: int
    device: str
    rect: Rect
    work: Rect
    is_primary: bool
    dpi_x: int
    dpi_y: int
    scale_x: float
    scale_y: float
    physical_width: int
    physical_height: int
    refresh_hz: int
    bits_per_pel: int

    @property
    def scale(self) -> float:
        return self.scale_x

    @property
    def scale_percent(self) -> int:
        return int(round(self.scale_x * 100))

    @property
    def center(self) -> Tuple[int, int]:
        return self.rect.center

    @property
    def uniform_scale(self) -> bool:
        return abs(self.scale_x - self.scale_y) < 1e-6

    def contains(self, x: int, y: int) -> bool:
        return self.rect.contains(x, y)

    def __str__(self):
        return "[%d]%s %-14s %dx%d @(%d,%d) %d%% %dHz" % (
            self.index, "*" if self.is_primary else " ",
            self.device or "(未知)",
            self.physical_width, self.physical_height,
            self.rect.left, self.rect.top,
            self.scale_percent, self.refresh_hz,
        )


def _to_monitor(info: D.DpiInfo) -> Monitor:
    return Monitor(
        index=info.monitor_index,
        device=info.device,
        rect=Rect.from_xywh(*info.rect),
        work=Rect.from_xywh(*info.work),
        is_primary=info.is_primary,
        dpi_x=info.dpi_x,
        dpi_y=info.dpi_y,
        scale_x=info.scale_x,
        scale_y=info.scale_y,
        physical_width=info.physical_width,
        physical_height=info.physical_height,
        refresh_hz=info.refresh_hz,
        bits_per_pel=info.bits_per_pel,
    )


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
def monitors() -> List[Monitor]:
    """全部显示器，按阶段一的枚举顺序。每次调用重新探测（支持热插拔）。"""
    return [_to_monitor(m) for m in D.list_monitors()]


def count() -> int:
    return len(monitors())


def primary() -> Monitor:
    """主显示器。"""
    for m in monitors():
        if m.is_primary:
            return m
    ms = monitors()
    if not ms:
        raise RuntimeError("未检测到任何显示器")
    return ms[0]


def by_index(index: int) -> Monitor:
    ms = monitors()
    if not 0 <= index < len(ms):
        raise IndexError("显示器序号 %d 越界（共 %d 块）" % (index, len(ms)))
    return ms[index]


def virtual_screen() -> Rect:
    """虚拟屏矩形（所有显示器的并集），可能含负坐标。"""
    vx, vy, vw, vh = D.virtual_screen()
    return Rect.from_xywh(vx, vy, vw, vh)


def virtual_origin() -> Tuple[int, int]:
    vx, vy, _, _ = D.virtual_screen()
    return (int(vx), int(vy))


def is_multi_monitor() -> bool:
    return count() > 1


def has_mixed_scaling() -> bool:
    """是否存在混合缩放 —— 这是最容易翻车的场景。"""
    ms = monitors()
    if len(ms) < 2:
        return False
    first = round(ms[0].scale_x, 4)
    return any(abs(round(m.scale_x, 4) - first) > 1e-6 for m in ms[1:])


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def at_point(x: int, y: int,
             default_to_nearest: bool = True) -> Optional[Monitor]:
    """坐标所属显示器。``default_to_nearest`` 让屏幕外的点也返回最近的屏。"""
    idx = A.MONITOR_DEFAULTTONEAREST if default_to_nearest else A.MONITOR_DEFAULTTONULL
    hmon = A.user32.MonitorFromPoint(S.POINT(int(x), int(y)), idx)
    if not hmon:
        return None
    info = S.MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(S.MONITORINFOEXW)
    if not A.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    device = info.szDevice or ""
    for m in monitors():
        if m.device == device:
            return m
    return None


def at_window(hwnd: int) -> Optional[Monitor]:
    """窗口所在显示器。"""
    if not hwnd:
        return None
    hmon = A.user32.MonitorFromWindow(int(hwnd), A.MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = S.MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(S.MONITORINFOEXW)
    if not A.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    device = info.szDevice or ""
    for m in monitors():
        if m.device == device:
            return m
    return None


def index_at_point(x: int, y: int) -> int:
    m = at_point(x, y)
    return m.index if m else -1


def scale_at_point(x: int, y: int) -> float:
    m = at_point(x, y)
    return m.scale_x if m else 1.0


def cursor_monitor() -> Optional[Monitor]:
    """鼠标当前所在的显示器。"""
    pt = S.POINT()
    A.user32.GetCursorPos(ctypes.byref(pt))
    return at_point(pt.x, pt.y)


# --------------------------------------------------------------------------
# 工作区
# --------------------------------------------------------------------------
def work_area(index: Optional[int] = None) -> Rect:
    """工作区（扣掉任务栏）。

    ⚠️ 注意：多显示器时任务栏只在一块屏上（通常是主屏），
    所以各屏的 work 矩形扣减量不一样 —— 不能用一个全局值算。
    """
    if index is None:
        return primary().work
    return by_index(index).work


def is_in_work_area(x: int, y: int) -> bool:
    """点是否在某个工作区内（不被任务栏遮挡）。"""
    for m in monitors():
        if m.work.contains(x, y):
            return True
    return False


# --------------------------------------------------------------------------
# 邻居
# --------------------------------------------------------------------------
def neighbors(index: int, direction: str, tolerance: int = 200) -> Optional[int]:
    """找指定方向上相邻的显示器。

    ``direction``: "left" / "right" / "up" / "down"

    算法：候选屏必须在目标方向上、且在垂直/水平方向上有重叠。
    ``tolerance`` 容忍布局时的轻微错位（人手工排列显示器很难对齐）。
    """
    ms = monitors()
    if not 0 <= index < len(ms):
        return None
    me = ms[index].rect
    best = None
    best_dist = None

    for m in ms:
        if m.index == index:
            continue
        r = m.rect
        if direction in ("left", "right"):
            if direction == "left":
                # 候选屏必须在左侧（其右边界不越过我左边界太多）
                if r.right > me.left + tolerance:
                    continue
            else:
                if r.left < me.right - tolerance:
                    continue
            # 垂直方向必须有重叠，否则是"斜对面"不是"左边"
            if r.bottom <= me.top or r.top >= me.bottom:
                continue
            dist = abs(r.center[0] - me.center[0])
        elif direction in ("up", "down"):
            if direction == "up":
                if r.bottom > me.top + tolerance:
                    continue
            else:
                if r.top < me.bottom - tolerance:
                    continue
            if r.right <= me.left or r.left >= me.right:
                continue
            dist = abs(r.center[1] - me.center[1])
        else:
            raise ValueError("未知方向: %r（可选 left/right/up/down）" % direction)

        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = m.index
    return best


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def describe() -> str:
    ms = monitors()
    vs = virtual_screen()
    lines = [
        "虚拟桌面 : %s @ (%d,%d)" % (vs.size, vs.left, vs.top),
        "显示器   : %d 块%s%s" % (
            len(ms),
            "  ⚠ 混合缩放（坐标必须按屏分别换算）" if has_mixed_scaling() else "",
            "",
        ),
    ]
    for m in ms:
        lines.append("  " + str(m))
        lines.append("      工作区 %s   DPI %d/%d" % (m.work, m.dpi_x, m.dpi_y))
    return "\n".join(lines)


def to_dict() -> dict:
    return {
        "count": count(),
        "multi": is_multi_monitor(),
        "mixed_scaling": has_mixed_scaling(),
        "virtual": virtual_screen().as_ltrb(),
        "monitors": [
            {
                "index": m.index,
                "device": m.device,
                "rect": m.rect.as_ltrb(),
                "work": m.work.as_ltrb(),
                "primary": m.is_primary,
                "dpi": (m.dpi_x, m.dpi_y),
                "scale": round(m.scale_x, 4),
                "scale_percent": m.scale_percent,
                "physical": (m.physical_width, m.physical_height),
                "refresh": m.refresh_hz,
                "bpp": m.bits_per_pel,
                "uniform_scale": m.uniform_scale,
            }
            for m in monitors()
        ],
    }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """显示器自检。

    枚举检查依赖**本机接了显示器**。一块都枚举不到时判 SKIP ——
    环境资源不存在 ≠ 代码有 bug（例如无头 / 远程会话）。
    """
    checks = []
    failures = []
    skipped = []

    def _add(name, ok, detail, skip=False):
        """``skip=True`` → 本机环境缺这个资源，标 SKIP，不计入失败。"""
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": detail})
        if skip:
            skipped.append(name)
        elif not ok:
            failures.append(name)

    ms = monitors()
    if ms:
        _add("至少一块显示器", True, "%d 块" % len(ms))
    else:
        _add("至少一块显示器", False,
             "本机无显示器（无头 / 远程会话）→ 跳过", skip=True)

    if ms:
        _add("恰好一块主屏", sum(1 for m in ms if m.is_primary) == 1,
             "主屏 %d 块" % sum(1 for m in ms if m.is_primary))
        _add("虚拟屏覆盖全部显示器",
             all(virtual_screen().contains(m.rect.left, m.rect.top) and
                 m.rect.right <= virtual_screen().right + 1 and
                 m.rect.bottom <= virtual_screen().bottom + 1 for m in ms),
             "虚拟屏 %s" % virtual_screen())
        _add("工作区在显示器内",
             all(m.rect.contains(m.work.left, m.work.top) for m in ms),
             "各屏工作区均在自身矩形内")
        _add("物理尺寸可读",
             all(m.physical_width > 0 and m.physical_height > 0 for m in ms),
             ", ".join("%dx%d" % (m.physical_width, m.physical_height) for m in ms))
        # 主屏中心应归属主屏
        pm = primary()
        got = at_point(*pm.center)
        _add("MonitorFromPoint 归属正确",
             got is not None and got.device == pm.device,
             "主屏中心 → [%s]" % (got.device if got else "None"))
        # 工作区不等于显示器矩形时说明有任务栏，这是正常的；只验证不越界
        _add("工作区不越界",
             all(m.work.left >= m.rect.left and m.work.top >= m.rect.top
                 for m in ms), "ok")

    return {"checks": checks, "failures": failures,
            "skipped": skipped, "ok": not failures}
