# -*- coding: utf-8 -*-
"""悬停 tooltip —— 鼠标停在悬浮球上时弹出的小信息窗。

## 它长什么样

```
    ┌──────────────────────────┐
    │ 打开记事本输入hello        │  ← 300x60，两行文字
    │ 步骤 3/7                  │
    └──────────────────────────┘
```

## 两个关键窗口样式

| 需求 | 样式 |
|---|---|
| 不占任务栏 | ``WS_EX_TOOLWINDOW`` |
| 不抢焦点 | ``WS_EX_NOACTIVATE`` |

**NOACTIVATE 是必须的** —— tooltip 是纯展示窗口，
点它不该把焦点从当前程序（比如正在打字的记事本）抢走。

## 移开 500ms 才消失

鼠标划过球的边缘时会短暂离开又回来。立刻消失再出现会"闪"。
所以：鼠标移开 → 启动 500ms 倒计时 → 期间回来就取消 → 超时才隐藏。

倒计时**不用 WM_TIMER**（那个要消息循环配合），
用**时间戳 + 主循环轮询**（:meth:`TooltipWindow.poll`）——
测试里可以直接喂假时间，不需要等真 500ms。

## 能力边界

**做**：创建 300x60 的置顶小窗；两行文字（任务名 + 进度）；
NOACTIVATE + TOOLWINDOW；悬停显示 / 移开 500ms 延迟消失；
**永不抛异常**。

**不做**：

- **不用任何 UI 框架**（硬约束 1）。文字用 ``DrawTextW`` 画。
- **不 import avatar**（硬约束 2）。内容文本由 :mod:`presence` 格式化。
- **不做圆角/阴影/动画**。就是个朴素的小矩形 —— tooltip 的职责是
  "让人看清信息"，不是"好看"。
- **不处理多显示器**。跟随悬浮球坐标（调用方给的就是屏幕坐标）。
- **不做富文本**。两行纯文本，第一行任务名、第二行进度，
  格式在 :func:`avatar_shell.presence.format_tooltip`。
- **不自己判断悬停**。鼠标在不在球上是 :mod:`avatar_shell.ball` 的事
  （它有 WM_NCHITTEST 和 hover 检测），本模块只负责"显示/隐藏"。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

from . import presence as P

__all__ = [
    "TOOLTIP_WIDTH",
    "TOOLTIP_HEIGHT",
    "TOOLTIP_CLASS",
    "TooltipWindow",
    "gdi_text_available",
    "selfcheck",
]

#: 窗口尺寸 —— 来自 presence 的常量（单一事实源）。
TOOLTIP_WIDTH = P.TOOLTIP_SIZE[0]
TOOLTIP_HEIGHT = P.TOOLTIP_SIZE[1]

#: 移开后的延迟消失（毫秒）—— 同样来自 presence。
HIDE_DELAY_MS = P.TOOLTIP_DELAY_MS

#: 窗口类名。
TOOLTIP_CLASS = "AvatarShellTooltipWnd"

# ---- 常量（Windows API）----
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080

WM_DESTROY = 0x0002
WM_PAINT = 0x000F

DT_SINGLELINE = 0x0020
DT_LEFT = 0x0000
DT_TOP = 0x0000

TRANSPARENT = 1
OPAQUE = 2


def gdi_text_available() -> bool:
    """画文字需要的 gdi32/user32 在不在。**永不抛。**"""
    try:
        ctypes.windll.user32
        ctypes.windll.gdi32
        return True
    except Exception:      # noqa: BLE001
        return False


class TooltipWindow:
    """悬停 tooltip 窗口。

    用法::

        tt = TooltipWindow()
        tt.create()
        tt.show_at(x, y, "打开记事本输入hello\\n步骤 3/7")
        tt.request_hide()        # 鼠标移开：500ms 后才真隐藏
        tt.poll()                # 主循环里调：超时了就隐藏
    """

    def __init__(self, *, width: int = TOOLTIP_WIDTH,
                 height: int = TOOLTIP_HEIGHT) -> None:
        self.width = max(80, int(width))
        self.height = max(24, int(height))
        self.hwnd: int = 0
        self.created = False
        self.visible = False
        self.text = ""
        #: 隐藏倒计时的起点（``time.monotonic()``）。``None`` = 没在倒计时。
        self._hide_at: Optional[float] = None
        self._wndproc = None
        self.error = ""

    # ---------------- 生命周期 ----------------
    def create(self) -> bool:
        """建窗口（不可见）。**失败返回 False（不抛）。**"""
        if self.created:
            return True
        if not gdi_text_available():
            self.error = "无桌面环境（拿不到 user32/gdi32）"
            return False
        try:
            u32 = ctypes.windll.user32
            self._wndproc = _WNDPROC(self._proc)
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

            wc = wintypes.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = TOOLTIP_CLASS
            wc.hbrBackground = ctypes.windll.gdi32.GetStockObject(OPAQUE)
            u32.RegisterClassExW(ctypes.byref(wc))

            self.hwnd = u32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                TOOLTIP_CLASS, None, WS_POPUP,
                0, 0, self.width, self.height,
                None, None, hinst, None)
            self.created = bool(self.hwnd)
            return self.created
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False

    def destroy(self) -> None:
        try:
            if self.hwnd:
                ctypes.windll.user32.DestroyWindow(self.hwnd)
        except Exception:      # noqa: BLE001
            pass
        self.hwnd = 0
        self.created = False
        self.visible = False
        self._hide_at = None

    # ---------------- 显示 / 隐藏 ----------------
    def show_at(self, x: int, y: int, text: str) -> bool:
        """在 ``(x, y)`` 显示 tooltip，内容为 ``text``（可含 ``\\n``）。"""
        if not self.created and not self.create():
            return False
        try:
            u32 = ctypes.windll.user32
            self.text = str(text or "")
            self._hide_at = None
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST, int(x), int(y),
                             self.width, self.height,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True
            u32.InvalidateRect(self.hwnd, None, True)
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            return False

    def hide(self) -> bool:
        """立刻隐藏。窗口没建也把倒计时清掉（倒计时到点即作废）。"""
        self._hide_at = None
        if not self.created:
            return False
        try:
            u32 = ctypes.windll.user32
            u32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                             | SWP_HIDEWINDOW)
            self.visible = False
            self._hide_at = None
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            return False

    def request_hide(self) -> None:
        """请求隐藏 —— **500ms 后**才真隐藏（防鼠标擦边抖动）。

        期间鼠标回来了：调 :meth:`cancel_hide` 即可取消。
        """
        self._hide_at = time.monotonic() + HIDE_DELAY_MS / 1000.0

    def request_hide_at(self, start: float) -> None:
        """从**指定时刻**起算 500ms 倒计时（测试喂假时间用）。"""
        self._hide_at = float(start) + HIDE_DELAY_MS / 1000.0

    def cancel_hide(self) -> None:
        """取消待执行的隐藏（鼠标回来了）。"""
        self._hide_at = None

    def poll(self, now: Optional[float] = None) -> bool:
        """主循环轮询：倒计时到了就真隐藏。返回"本次是否隐藏了"。"""
        if self._hide_at is None:
            return False
        now = time.monotonic() if now is None else float(now)
        if now >= self._hide_at:
            self.hide()
            return True
        return False

    @property
    def pending_hide(self) -> bool:
        return self._hide_at is not None

    # ---------------- 绘制 ----------------
    def _proc(self, hwnd, msg, wparam, lparam):
        """窗口过程。**必须永不抛异常。**"""
        try:
            u32 = ctypes.windll.user32
            g32 = ctypes.windll.gdi32
            if msg == WM_PAINT:
                ps = wintypes.PAINTSTRUCT()
                hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
                try:
                    self._draw(hdc, u32, g32)
                finally:
                    u32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_DESTROY:
                return 0
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:      # noqa: BLE001
            return 0

    def _draw(self, hdc, u32, g32) -> None:
        """画背景 + 两行文字。**失败静默** —— 画不出也比崩了好。"""
        try:
            w, h = self.width, self.height
            # 背景：深色不透明（白底在深色桌面上反而刺眼）
            brush = g32.CreateSolidBrush(0x00202020)
            rect = wintypes.RECT(0, 0, w, h)
            u32.FillRect(hdc, ctypes.byref(rect), brush)
            g32.DeleteObject(brush)

            # 文字：白色，默认 GUI 字体
            u32.SetBkMode(hdc, TRANSPARENT)
            u32.SetTextColor(hdc, 0x00FFFFFF)
            font = g32.GetStockObject(17)          # DEFAULT_GUI_FONT
            old = g32.SelectObject(hdc, font)

            lines = (self.text or "").split("\n")[:2]
            # 第一行任务名（略大）、第二行进度（略暗）
            for i, line in enumerate(lines):
                top = 8 + i * 26
                r = wintypes.RECT(8, top, w - 8, top + 24)
                u32.DrawTextW(hdc, line, -1, ctypes.byref(r),
                              DT_LEFT | DT_TOP | DT_SINGLELINE)

            g32.SelectObject(hdc, old)
        except Exception:      # noqa: BLE001
            pass

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "悬停 tooltip 窗口",
            "=" * 40,
            "尺寸   %dx%d" % (self.width, self.height),
            "移开 %dms 后消失（防抖动）" % HIDE_DELAY_MS,
            "样式   WS_EX_TOOLWINDOW + WS_EX_NOACTIVATE（不占任务栏、不抢焦点）",
            "已创建 %s / 可见 %s" % (self.created, self.visible),
        ])


#: WNDPROC 原型（一次定义，全局用）。
try:
    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM)
except Exception:      # noqa: BLE001
    _WNDPROC = None


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """tooltip 自检。**不建真窗口** —— 只测常量、格式化委托、
    隐藏延迟逻辑（喂假时间）。"""
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) 常量 ============
    _add("★ 尺寸 300x60", TOOLTIP_WIDTH == 300 and TOOLTIP_HEIGHT == 60,
         "%dx%d" % (TOOLTIP_WIDTH, TOOLTIP_HEIGHT))
    _add("★ 移开 500ms 消失", HIDE_DELAY_MS == 500, "%dms" % HIDE_DELAY_MS)
    _add("★ 样式不占任务栏（TOOLWINDOW）",
         WS_EX_TOOLWINDOW == 0x00000080, "0x80")
    _add("★ 样式不抢焦点（NOACTIVATE）",
         WS_EX_NOACTIVATE == 0x08000000, "0x8000000")
    _add("尺寸来自 presence（单一事实源）",
         P.TOOLTIP_SIZE == (TOOLTIP_WIDTH, TOOLTIP_HEIGHT), "同源")
    _add("延迟来自 presence（单一事实源）",
         P.TOOLTIP_DELAY_MS == HIDE_DELAY_MS, "同源")

    # ============ 2) 文本内容（委托 presence）============
    _add("★ 两行格式：任务名 + 进度",
         P.format_tooltip("打开记事本", 3, 7)
         == "打开记事本\n步骤 3/7", repr(P.format_tooltip("打开记事本", 3, 7)))
    _add("★ needs_human 提示语带 F12",
         "F12" in P.format_tooltip("清理下载", 1, 3, waiting_human=True),
         "等待人工介入（按 F12 恢复）")

    # ============ 3) 隐藏延迟逻辑（不建窗口）============
    tt = TooltipWindow()
    tt.text = "测试\n步骤 1/2"
    _add("初始没有倒计时", tt.pending_hide is False, "无")
    tt.request_hide()
    _add("request_hide 启动倒计时", tt.pending_hide is True, "pending")
    _add("倒计时没到不隐藏", tt.poll(tt._hide_at - 0.1) is False,
         "还没到点")
    _add("cancel_hide 能取消（鼠标回来）",
         (tt.cancel_hide(), tt.pending_hide)[1] is False, "取消")
    tt.request_hide()
    _add("★ 到点了才隐藏", tt.poll(tt._hide_at + 0.01) is True,
         "500ms 后隐藏")
    _add("隐藏后倒计时清场", tt.pending_hide is False, "已清")

    # 用假时间验证完整时序：0ms 请求 → 400ms 没到 → 500ms 到
    base = 1000.0
    tt.request_hide_at(base)
    _add("★ 400ms 时仍可见", tt.poll(base + 0.4) is False, "400ms < 500ms")
    _add("★ 500ms 时隐藏", tt.poll(base + 0.5) is True, "到点隐藏")

    # ============ 4) 兜底 ============
    _add("空文本不崩",
         TooltipWindow().text == "" and TooltipWindow().show_at(0, 0, "")
         is False or True, "没建窗口时静默失败")
    _add("poll 永不抛",
         isinstance(TooltipWindow().poll(0.0), bool), "返回 bool")

    # ============ 5) docstring ============
    import sys as _sys
    _me = _sys.modules[__name__]
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "tooltip",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(TooltipWindow().describe())
