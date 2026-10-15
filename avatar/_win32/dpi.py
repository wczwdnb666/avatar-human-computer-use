# -*- coding: utf-8 -*-
"""DPI 适配 —— 阶段一的核心。

## 为什么这是阶段一

整个 Avatar 的所有动作最终都要落到"屏幕上某个物理像素"。只要 DPI 处理错一步，
后面所有层的坐标全部偏移，而且**不会报错**，只是点不准。表现形式还很迷惑：

    · 单屏 100% 缩放时一切正常 → 你以为是别处的 bug
    · 4K 屏 150% 缩放时点击落在左上方向偏移 1/3 → 你以为是窗口坐标算错了

所以它必须是最先立起来、且唯一真值的一层。

## 三个坐标系

1. **物理像素（physical）** —— 真值。`SetProcessDpiAwareness(2)` 之后
   `GetSystemMetrics` / `GetCursorPos` / `GetWindowRect` / UIA 的
   `BoundingRectangle` / `ImageGrab` 全部返回物理像素，互相之间可直接比较。

2. **逻辑像素（logical）** —— 进程 DPI 不感知时 API 返回的值。等于
   `物理 / scale`。**只在换算函数内部存在**，不允许流到外面。

3. **DIP（device-independent pixel）** —— WinUI / WPF 用的 1/96 英寸单位，
   数值上等于逻辑像素，但语义是"与缩放无关的设计单位"。做模板匹配时要区分。

## 关键陷阱

`SetProcessDpiAwareness(2)` 必须在**任何窗口/屏幕 API 调用之前**执行，
且**一个进程只能设置一次**。如果先建了窗口或先读了 `GetSystemMetrics`，
调用会返回 `E_ACCESSDENIED`，DPI 感知状态不生效 —— 而很多代码把这个
返回值丢掉了，于是后面全是逻辑坐标，还不自知。

本模块的做法：
  · 导入即调用（见模块底部），并**记录结果**到 `AWARENESS.result`；
  · 提供 `verify()` 让自检脚本能明确验证，而不是假设成功。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from ..errors import DpiSetupError
from . import api as A
from . import structs as S

# 标准 DPI 基准：96 DPI = 100% 缩放
BASE_DPI = 96.0


@dataclass(frozen=True)
class DpiInfo:
    """一次 DPI 探测的完整结果。所有坐标均为物理像素。"""

    monitor_index: int
    device: str
    rect: tuple  # (left, top, width, height) 物理像素
    work: tuple  # (left, top, width, height) 工作区（扣掉任务栏）物理像素
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
        """缩放系数。横向优先，绝大多数场景横纵一致。"""
        return self.scale_x

    @property
    def scale_percent(self) -> int:
        return int(round(self.scale_x * 100))

    @property
    def is_uniform_scale(self) -> bool:
        return abs(self.scale_x - self.scale_y) < 1e-6

    @property
    def center(self) -> tuple:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)

    def __str__(self):
        x, y, w, h = self.rect
        return "%s%dx%d@%dHz @ (%d,%d) %d%%" % (
            self.device,
            self.physical_width,
            self.physical_height,
            self.refresh_hz,
            x,
            y,
            self.scale_percent,
        )


def _query_process_awareness() -> int:
    """返回本进程当前的 DPI 感知级别常量。

    优先 GetProcessDpiAwareness（shcore），失败则用
    GetThreadDpiAwarenessContext（Win10 1607+）翻译。
    """
    if A.shcore is not None:
        val = ctypes.c_int(-1)
        hr = A.shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
        if A.hresult_ok(hr):
            return val.value
    # 退化路径：从线程上下文反推
    try:
        ctx = A.user32.GetThreadDpiAwarenessContext()
        aw = A.user32.GetAwarenessFromDpiAwarenessContext(ctx)
        return aw
    except Exception:
        return -1


@dataclass
class AwarenessState:
    """进程 DPI 感知的初始化记录。"""

    result: int = -1  # 成功设置到的级别；-1 表示未知
    method: str = "none"  # 实际生效的手段
    attempts: tuple = ()

    @property
    def ok(self) -> bool:
        return self.result >= S.PROCESS_SYSTEM_DPI_AWARE

    @property
    def per_monitor(self) -> bool:
        return self.result == S.PROCESS_PER_MONITOR_DPI_AWARE

    def __str__(self):
        names = {
            S.PROCESS_DPI_UNAWARE: "UNAWARE (会拿到逻辑坐标，危险)",
            S.PROCESS_SYSTEM_DPI_AWARE: "SYSTEM_AWARE",
            S.PROCESS_PER_MONITOR_DPI_AWARE: "PER_MONITOR_AWARE (推荐)",
        }
        return names.get(self.result, "UNKNOWN(%d) via %s" % (self.result, self.method))


AWARENESS = AwarenessState()


def set_process_dpi_awareness(force: bool = False) -> AwarenessState:
    """把当前进程设为 Per-Monitor DPI Aware。

    顺序很重要：**先试最精确的，再逐级降级**。
      1. shcore.SetProcessDpiAwareness(2)  —— Win8.1+，进程级，可验证返回值
      2. user32.SetProcessDPIAware()       —— Vista+，只到 system aware
      3. 什么都不做                        —— 记录后果，让调用方知道自己在裸奔

    重复调用是安全的：会直接返回已记录的状态。
    """
    global AWARENESS
    if AWARENESS.result != -1 and not force:
        return AWARENESS

    attempts = []
    result = -1
    method = "none"

    # --- 尝试 1：shcore，最精确 ---
    if A.shcore is not None:
        try:
            hr = A.shcore.SetProcessDpiAwareness(S.PROCESS_PER_MONITOR_DPI_AWARE)
            # S_OK=0；E_ACCESSDENIED=0x80070005 说明已经设过了
            if A.hresult_ok(hr):
                attempts.append("shcore.SetProcessDpiAwareness(PER_MONITOR)=OK")
                result = S.PROCESS_PER_MONITOR_DPI_AWARE
                method = "shcore"
            else:
                attempts.append("shcore.SetProcessDpiAwareness(PER_MONITOR)=0x%08X" % (hr & 0xFFFFFFFF))
        except Exception as e:  # pragma: no cover
            attempts.append("shcore 异常: %r" % (e,))
    else:
        attempts.append("shcore 不可用（系统低于 Win8.1）")

    # --- 尝试 2：老 API ---
    if result == -1:
        try:
            if A.user32.SetProcessDPIAware():
                attempts.append("user32.SetProcessDPIAware()=OK")
                result = S.PROCESS_SYSTEM_DPI_AWARE
                method = "user32"
            else:
                attempts.append("user32.SetProcessDPIAware()=FAIL")
        except Exception as e:  # pragma: no cover
            attempts.append("user32 异常: %r" % (e,))

    # --- 兜底：查真实状态 ---
    actual = _query_process_awareness()
    if result == -1:
        attempts.append("查询当前状态=%d" % actual)
        result = actual
        method = "query-only"

    AWARENESS = AwarenessState(result=result, method=method, attempts=tuple(attempts))
    return AWARENESS


def verify(raise_on_fail: bool = False) -> bool:
    """验证 DPI 感知是否真的生效。

    不能只看 SetProcessDpiAwareness 的返回值 —— 必须**交叉验证**：
    比较 GetSystemMetrics 和 EnumDisplaySettings 报的物理分辨率。
    如果两者不等，说明我们拿到的还是逻辑坐标。

    验证方式用的是"屏幕 vs 显示器真实模式"这条独立路径，
    不依赖任何 API 的返回值，所以能戳穿静默失败。
    """
    state = set_process_dpi_awareness()
    if not state.ok:
        if raise_on_fail:
            raise DpiSetupError(
                "进程 DPI 感知未生效（result=%d，尝试：%s）。"
                "后果：所有坐标会按缩放比例偏移，且不会有任何报错。"
                % (state.result, " / ".join(state.attempts))
            )
        return False

    metrics_w = A.user32.GetSystemMetrics(S.SM_CXSCREEN)
    primary = primary_monitor()
    if primary is not None and primary.physical_width:
        # 允许 1px 的差异（某些驱动会报 -1）
        if abs(metrics_w - primary.physical_width) > 1:
            if raise_on_fail:
                raise DpiSetupError(
                    "GetSystemMetrics 报 %d，但显示器真实宽度为 %d。"
                    "说明进程仍在逻辑坐标系下（缩放 %d%%）。"
                    % (metrics_w, primary.physical_width, primary.scale_percent)
                )
            return False
    return True


# --------------------------------------------------------------------------
# 显示器枚举
# --------------------------------------------------------------------------
def _monitor_proc():
    """构造 EnumDisplayMonitors 回调。返回 (callback, results_list)。"""
    results = []

    def _cb(hmon, hdc, lprc, lparam):
        info = S.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(S.MONITORINFOEXW)
        if A.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            results.append((hmon, info))
        else:
            # 拿不到扩展信息也要保留物理矩形，否则会漏掉显示器
            r = lprc.contents
            info.rcMonitor = S.RECT(r.left, r.top, r.right, r.bottom)
            info.rcWork = S.RECT(r.left, r.top, r.right, r.bottom)
            info.dwFlags = 0
            info.szDevice = ""
            results.append((hmon, info))
        return True

    return A.MonitorEnumProc(_cb), results


def _dpi_for_monitor(hmon) -> tuple:
    """取某个显示器的有效 DPI。返回 (dpi_x, dpi_y)。

    GetDpiForMonitor 在 Win8.1+ 可用。若 shcore 缺失，退回到用
    该显示器的物理宽度反推 —— 但那条路需要知道逻辑宽度，不可靠，
    故最终兜底返回 (96, 96) 并让上层知道这是猜测值。
    """
    if A.shcore is not None:
        dx = ctypes.c_uint(0)
        dy = ctypes.c_uint(0)
        hr = A.shcore.GetDpiForMonitor(
            hmon, S.MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)
        )
        if A.hresult_ok(hr) and dx.value > 0:
            return int(dx.value), int(dy.value)
    return 96, 96


def _display_mode(device: str) -> tuple:
    """EnumDisplaySettings 取真实物理模式。返回 (w, h, hz, bpp)。"""
    dm = S.DEVMODEW()
    dm.dmSize = ctypes.sizeof(S.DEVMODEW)
    ok = A.user32.EnumDisplaySettingsW(
        device if device else None, S.ENUM_CURRENT_SETTINGS, ctypes.byref(dm)
    )
    if not ok:
        return 0, 0, 0, 0
    return int(dm.dmPelsWidth), int(dm.dmPelsHeight), int(dm.dmDisplayFrequency), int(dm.dmBitsPerPel)


def list_monitors(refresh: bool = True) -> list:
    """枚举所有显示器，返回 DpiInfo 列表。全部坐标为物理像素。

    每个显示器的 DPI 是**独立查询**的 —— 这就是"全域"的含义：
    笔记本 150% + 外接 4K 100% 的混合场景下，两张屏的缩放系数不同，
    用一个全局 scale 是错的。
    """
    cb, results = _monitor_proc()
    A.user32.EnumDisplayMonitors(None, None, cb, 0)

    out = []
    for idx, (hmon, info) in enumerate(results):
        device = info.szDevice or ""
        mx, my = info.rcMonitor.left, info.rcMonitor.top
        mw = info.rcMonitor.width
        mh = info.rcMonitor.height
        wx, wy = info.rcWork.left, info.rcWork.top
        ww = info.rcWork.width
        wh = info.rcWork.height

        dpi_x, dpi_y = _dpi_for_monitor(hmon)

        # 物理分辨率：EnumDisplaySettings 是独立真值来源，
        # 用它交叉校验 EnumDisplayMonitors 报的矩形
        pw, ph, hz, bpp = _display_mode(device)

        # 若显示器矩形与真实模式不符（旋转 / 克隆模式），以真实模式为准
        if pw and ph:
            if (mw, mh) != (pw, ph) and (mw, mh) == (ph, pw):
                # 竖屏：矩形已按旋转后的宽高给出，保留矩形，不覆盖
                pass
            elif (mw, mh) != (pw, ph):
                # 克隆模式：多显示器共享同一输出，矩形是逻辑布局，真实模式是物理面板
                # 这种情况保留矩形（布局才是点击要用的），只记录真实模式供参考
                pass

        out.append(
            DpiInfo(
                monitor_index=idx,
                device=device,
                rect=(mx, my, mw, mh),
                work=(wx, wy, ww, wh),
                is_primary=bool(info.dwFlags & S.MONITORINFOF_PRIMARY),
                dpi_x=dpi_x,
                dpi_y=dpi_y,
                scale_x=dpi_x / BASE_DPI,
                scale_y=dpi_y / BASE_DPI,
                physical_width=pw or mw,
                physical_height=ph or mh,
                refresh_hz=hz,
                bits_per_pel=bpp,
            )
        )

    # 没有主屏标记时，把第一个当主屏（VirtualBox 等环境会这样）
    if out and not any(m.is_primary for m in out):
        first = out[0]
        out[0] = DpiInfo(
            monitor_index=first.monitor_index,
            device=first.device,
            rect=first.rect,
            work=first.work,
            is_primary=True,
            dpi_x=first.dpi_x,
            dpi_y=first.dpi_y,
            scale_x=first.scale_x,
            scale_y=first.scale_y,
            physical_width=first.physical_width,
            physical_height=first.physical_height,
            refresh_hz=first.refresh_hz,
            bits_per_pel=first.bits_per_pel,
        )
    return out


def primary_monitor() -> Optional[DpiInfo]:
    for m in list_monitors():
        if m.is_primary:
            return m
    ms = list_monitors()
    return ms[0] if ms else None


def virtual_screen() -> tuple:
    """虚拟桌面（全部显示器的并集），物理像素。

    返回 (left, top, width, height)。

    多屏且主屏不在最左时 left/top 可能为**负数** —— 这是正确的，
    绝对坐标定位（SendInput 的 MOUSEEVENTF_ABSOLUTE）必须用虚拟桌面原点
    做归一化，直接用主屏尺寸归一化会导致副屏点击全部落在主屏上。
    """
    return (
        A.user32.GetSystemMetrics(S.SM_XVIRTUALSCREEN),
        A.user32.GetSystemMetrics(S.SM_YVIRTUALSCREEN),
        A.user32.GetSystemMetrics(S.SM_CXVIRTUALSCREEN),
        A.user32.GetSystemMetrics(S.SM_CYVIRTUALSCREEN),
    )


def monitor_count() -> int:
    return A.user32.GetSystemMetrics(S.SM_CMONITORS)


def monitor_at_point(x: int, y: int) -> Optional[DpiInfo]:
    """给定物理坐标，返回它所在的显示器。用于"这个点在哪个屏缩放比下"。"""
    pt = S.POINT(int(x), int(y))
    hmon = A.user32.MonitorFromPoint(pt, A.MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = S.MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(S.MONITORINFOEXW)
    if not A.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    device = info.szDevice or ""
    dpi_x, dpi_y = _dpi_for_monitor(hmon)
    pw, ph, hz, bpp = _display_mode(device)
    return DpiInfo(
        monitor_index=-1,
        device=device,
        rect=(info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.width, info.rcMonitor.height),
        work=(info.rcWork.left, info.rcWork.top, info.rcWork.width, info.rcWork.height),
        is_primary=bool(info.dwFlags & S.MONITORINFOF_PRIMARY),
        dpi_x=dpi_x,
        dpi_y=dpi_y,
        scale_x=dpi_x / BASE_DPI,
        scale_y=dpi_y / BASE_DPI,
        physical_width=pw or info.rcMonitor.width,
        physical_height=ph or info.rcMonitor.height,
        refresh_hz=hz,
        bits_per_pel=bpp,
    )


def scale_at_point(x: int, y: int) -> float:
    """某物理坐标处的缩放系数。取不到时返回 1.0（保守）。"""
    m = monitor_at_point(x, y)
    return m.scale if m else 1.0


# --------------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------------
def physical_to_logical(x: int, y: int, scale: Optional[float] = None) -> tuple:
    """物理像素 → 逻辑像素。

    ⚠️ 只在需要和**非 DPI 感知的外部程序**（如某些老工具的配置、
    浏览器 window.screenX）交互时使用。Avatar 内部一律用物理像素。
    """
    if scale is None:
        scale = scale_at_point(x, y)
    if scale <= 0:
        scale = 1.0
    return (int(round(x / scale)), int(round(y / scale)))


def logical_to_physical(x: int, y: int, scale: Optional[float] = None) -> tuple:
    """逻辑像素 → 物理像素。"""
    if scale is None:
        scale = scale_at_point(x, y)
    if scale <= 0:
        scale = 1.0
    return (int(round(x * scale)), int(round(y * scale)))


def normalize_absolute(x: int, y: int) -> tuple:
    """物理坐标 → SendInput 绝对坐标所需的 0..65535 归一化值。

    关键点：**必须用虚拟桌面**做归一化基准，不是主屏尺寸。

    SendInput 的绝对坐标是相对于「虚拟屏幕」的，映射公式为::

        norm = (coord - virtual_origin) * 65535 / (virtual_size - 1)

    常见错误是直接用主屏的 0..width，导致：
      · 副屏在主屏右边 → 点击永远到不了副屏
      · 主屏不在原点（left<0）→ 整体偏移
    """
    vx, vy, vw, vh = virtual_screen()
    if vw <= 1 or vh <= 1:
        return 0, 0
    nx = int(round((int(x) - vx) * 65535.0 / (vw - 1)))
    ny = int(round((int(y) - vy) * 65535.0 / (vh - 1)))
    # 必须夹紧：越界值会被系统丢弃或产生回绕
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    return nx, ny


def clamp_to_screen(x: int, y: int) -> tuple:
    """把坐标夹进虚拟桌面范围，避免点到屏幕外（会有奇怪副作用）。"""
    vx, vy, vw, vh = virtual_screen()
    cx = max(vx, min(vx + vw - 1, int(x)))
    cy = max(vy, min(vy + vh - 1, int(y)))
    return cx, cy


def point_in_any_monitor(x: int, y: int) -> bool:
    for m in list_monitors():
        mx, my, mw, mh = m.rect
        if mx <= x < mx + mw and my <= y < my + mh:
            return True
    return False


def summary() -> dict:
    """阶段一自检摘要：给 CLI 和最终用户看的一页纸。"""
    state = set_process_dpi_awareness()
    monitors = list_monitors()
    vx, vy, vw, vh = virtual_screen()
    return {
        "awareness": {
            "result": state.result,
            "ok": state.ok,
            "per_monitor": state.per_monitor,
            "method": state.method,
            "attempts": list(state.attempts),
            "text": str(state),
        },
        "monitor_count": monitor_count(),
        "virtual_screen": {"left": vx, "top": vy, "width": vw, "height": vh},
        "primary_size": (
            A.user32.GetSystemMetrics(S.SM_CXSCREEN),
            A.user32.GetSystemMetrics(S.SM_CYSCREEN),
        ),
        "monitors": [
            {
                "index": m.monitor_index,
                "device": m.device,
                "rect": m.rect,
                "work": m.work,
                "primary": m.is_primary,
                "dpi": (m.dpi_x, m.dpi_y),
                "scale": m.scale_x,
                "scale_percent": m.scale_percent,
                "uniform_scale": m.is_uniform_scale,
                "physical": (m.physical_width, m.physical_height),
                "refresh": m.refresh_hz,
                "bpp": m.bits_per_pel,
            }
            for m in monitors
        ],
        "verified": verify(),
    }


# 导入即初始化：必须在任何窗口 API 被调用之前完成
set_process_dpi_awareness()
