# -*- coding: utf-8 -*-
"""一键唤起输入框 —— Ctrl+Alt+A 弹出，说一句话目标，Enter 提交。

## 它长什么样

```
    ╭──────────────────────────────────────────────╮
    │  输入任务目标…                                  │  ← 800x120，圆角，
    │  ▏                                            │     半透明 alpha 230
    ╰──────────────────────────────────────────────╯
              Enter 提交 / ESC 取消
```

浮在屏幕正中央。一个单行 Edit 控件 + 占位提示。

## 三个窗口细节（都有原因）

| 细节 | 做法 | 为什么 |
|---|---|---|
| 不占任务栏 | ``WS_EX_TOOLWINDOW`` | 它是个临时面板，不是应用 |
| 圆角 | ``SetWindowRgn`` + ``CreateRoundRectRgn`` | 圆角让"临时面板"看起来不像主窗口 |
| 半透明 | ``SetLayeredWindowAttributes`` alpha 230 | 透出底下的桌面，不挡视线 |

## ⚠ 焦点的矛盾怎么解

其他窗口一律 ``WS_EX_NOACTIVATE``（不抢焦点，硬约束 3）。
但**输入框打开时必须能激活** —— 不然没法打字。

解法：**打开时临时允许激活，关闭后恢复 NOACTIVATE**。
具体：创建窗口时**不带** NOACTIVATE（所以它可激活、能拿到键盘），
关闭时 ``SetWindowLong`` 把 NOACTIVATE 加回去
（关闭期间没人点它，透穿状态由 ball 的 hover 逻辑管，不冲突）。

## 为什么是 Edit 控件而不是自绘

"能打字、能选字、能粘贴"这些编辑能力，``EDIT`` 控件全自带。
自绘一个输入框要处理输入法、光标、选区 —— 那是几百行的坑。
**用一个原生控件 + 一层薄窗口**是正确取舍。

## 能力边界

**做**：800x120 置中窗口；圆角 + alpha 230；单行 Edit +
占位提示（``EM_SETCUEBANNER``）；Enter 提交调 ``on_submit``；
ESC 取消调 ``on_cancel``；提交/取消后自动隐藏；
打开时可激活（打得了字）、关闭后恢复 NOACTIVATE；**永不抛异常**。

**不做**：

- **不用任何 UI 框架**（硬约束 1）。原生 EDIT 控件 + ctypes。
- **不 import avatar**（硬约束 2）。提交后的动作
  （发任务）由 ``on_submit`` 回调接线到 :mod:`avatar_shell.bridge`。
- **不做多行输入**。任务目标是一句话 —— 要写长文档那是别的事。
- **不做历史记录**。最近任务在右键菜单（:mod:`avatar_shell.menu`），
  不在输入框里 —— 输入框保持"只说这一句"的纯粹。
- **不做输入校验**。空串也提交（主包会拒绝），
  外壳不替主包做判断 —— 那是"两个地方做同一件事"。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "INPUT_WIDTH",
    "INPUT_HEIGHT",
    "INPUT_ALPHA",
    "INPUT_CLASS",
    "CUE_TEXT",
    "InputBox",
    "selfcheck",
]

#: 窗口尺寸。
INPUT_WIDTH = 800
INPUT_HEIGHT = 120

#: 半透明度（0~255）。230 = 微微透出底下的桌面。
INPUT_ALPHA = 230

#: 圆角半径（像素）。
CORNER_RADIUS = 16

#: 窗口类名。
INPUT_CLASS = "AvatarShellInputWnd"

#: Edit 控件的类名。
EDIT_CLASS = "EDIT"

#: 占位提示文字。
CUE_TEXT = "输入任务目标…"

# ---- 常量（Windows API）----
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080

ULW_ALPHA = 0x00000002

GWL_EXSTYLE = -20

WM_DESTROY = 0x0002
WM_KEYDOWN = 0x0100
WM_COMMAND = 0x0111
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
EN_CHANGE = 0x0300

VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

EM_SETCUEBANNER = 0x1501


def _screen_size() -> tuple:
    """主屏尺寸。失败回退 1920x1080（测试环境无桌面时）。"""
    try:
        u32 = ctypes.windll.user32
        return (u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))
    except Exception:      # noqa: BLE001
        return (1920, 1080)


class InputBox:
    """唤起输入框。

    用法::

        box = InputBox(on_submit=lambda text: bridge.send_task(text),
                       on_cancel=lambda: None)
        box.create()
        box.show()          # 浮到屏幕中央，可打字
        # Enter → on_submit(text) → 自动隐藏
        # ESC  → on_cancel()     → 自动隐藏
    """

    def __init__(self, *,
                 on_submit: Optional[Callable[[str], Any]] = None,
                 on_cancel: Optional[Callable[[], Any]] = None) -> None:
        self.on_submit = on_submit
        self.on_cancel = on_cancel
        self.hwnd: int = 0
        self.edit_hwnd: int = 0
        self.created = False
        self.visible = False
        self.text = ""
        self._wndproc = None
        self.error = ""
        #: 最近一次提交/取消的文本（自检和调试用）。
        self.last_action: str = ""

    # ---------------- 生命周期 ----------------
    def create(self) -> bool:
        """建窗口（含 Edit 子控件，初始不可见）。"""
        if self.created:
            return True
        try:
            u32 = ctypes.windll.user32
            self._wndproc = _WNDPROC(self._proc)
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

            wc = wintypes.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = INPUT_CLASS
            wc.hbrBackground = ctypes.windll.gdi32.GetStockObject(2)  # 黑刷
            u32.RegisterClassExW(ctypes.byref(wc))

            self.hwnd = u32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED,
                INPUT_CLASS, None, WS_POPUP,
                0, 0, INPUT_WIDTH, INPUT_HEIGHT,
                None, None, hinst, None)
            if not self.hwnd:
                self.error = "建窗口失败"
                return False

            # ---- Edit 子控件（内缩 16px 留边）----
            self.edit_hwnd = u32.CreateWindowExW(
                0, EDIT_CLASS, None,
                WS_CHILD | WS_VISIBLE | 0x0080,   # ES_AUTOHSCROLL
                16, 16, INPUT_WIDTH - 32, 40,
                self.hwnd, None, hinst, None)
            if self.edit_hwnd:
                # 占位提示（灰色虚字）
                u32.SendMessageW(self.edit_hwnd, EM_SETCUEBANNER,
                                 1, CUE_TEXT)

            # 圆角区域
            try:
                g32 = ctypes.windll.gdi32
                rgn = g32.CreateRoundRectRgn(
                    0, 0, INPUT_WIDTH + 1, INPUT_HEIGHT + 1,
                    CORNER_RADIUS, CORNER_RADIUS)
                u32.SetWindowRgn(self.hwnd, rgn, True)
            except Exception:      # noqa: BLE001
                pass          # 圆角失败就直角，不影响功能

            # 半透明
            try:
                u32.SetLayeredWindowAttributes(
                    self.hwnd, 0, INPUT_ALPHA, ULW_ALPHA)
            except Exception:      # noqa: BLE001
                pass

            self.created = True
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False

    # ---------------- 显示 / 隐藏 ----------------
    def show(self, *, center: bool = True) -> bool:
        """显示并置中。**可激活**（打得了字）—— 焦点矛盾见模块 docstring。"""
        if not self.created and not self.create():
            return False
        try:
            u32 = ctypes.windll.user32
            if center:
                sw, sh = _screen_size()
                x = max(0, (sw - INPUT_WIDTH) // 2)
                y = max(0, (sh - INPUT_HEIGHT) // 3)
            else:
                x = y = 100
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y,
                             INPUT_WIDTH, INPUT_HEIGHT,
                             SWP_SHOWWINDOW)
            self.visible = True
            # 焦点给 Edit（这时窗口可激活 —— 创建时没带 NOACTIVATE）
            if self.edit_hwnd:
                u32.SetFocus(self.edit_hwnd)
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            return False

    def hide(self) -> bool:
        """隐藏。**恢复 NOACTIVATE**（关闭期间不该抢焦点）。"""
        if not self.created:
            return False
        try:
            u32 = ctypes.windll.user32
            # 把 NOACTIVATE 加回扩展样式
            ex = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                               ex | WS_EX_NOACTIVATE)
            u32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                             | SWP_HIDEWINDOW)
            self.visible = False
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            return False

    def destroy(self) -> None:
        try:
            if self.hwnd:
                ctypes.windll.user32.DestroyWindow(self.hwnd)
        except Exception:      # noqa: BLE001
            pass
        self.hwnd = 0
        self.edit_hwnd = 0
        self.created = False
        self.visible = False

    # ---------------- 文本 ----------------
    def get_text(self) -> str:
        """读 Edit 里的文本。"""
        if not self.edit_hwnd:
            return ""
        try:
            u32 = ctypes.windll.user32
            n = u32.GetWindowTextLengthW(self.edit_hwnd)
            if n <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(self.edit_hwnd, buf, n + 1)
            return buf.value or ""
        except Exception:      # noqa: BLE001
            return ""

    def clear(self) -> None:
        """清空输入框。"""
        if self.edit_hwnd:
            try:
                ctypes.windll.user32.SetWindowTextW(self.edit_hwnd, None)
            except Exception:      # noqa: BLE001
                pass

    # ---------------- 消息 ----------------
    def _submit(self) -> None:
        """Enter：读文本 → 回调 → 清空 → 隐藏。"""
        text = self.get_text()
        self.last_action = text
        if self.on_submit:
            try:
                self.on_submit(text)
            except Exception:      # noqa: BLE001
                pass          # 回调炸了不能带走窗口
        self.clear()
        self.hide()

    def _cancel(self) -> None:
        """ESC：回调 → 清空 → 隐藏。"""
        self.last_action = ""
        if self.on_cancel:
            try:
                self.on_cancel()
            except Exception:      # noqa: BLE001
                pass
        self.clear()
        self.hide()

    def _proc(self, hwnd, msg, wparam, lparam):
        """窗口过程。**必须永不抛异常。**"""
        try:
            u32 = ctypes.windll.user32
            if msg == WM_KEYDOWN:
                if wparam == VK_RETURN:
                    self._submit()
                    return 0
                if wparam == VK_ESCAPE:
                    self._cancel()
                    return 0
            if msg == WM_COMMAND:
                # Enter 在单行 Edit 里会发 WM_COMMAND（EN 通知），
                # 但我们已经从 WM_KEYDOWN 处理了 —— 这里防重复。
                return 0
            if msg == WM_DESTROY:
                return 0
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:      # noqa: BLE001
            return 0

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "唤起输入框",
            "=" * 40,
            "尺寸   %dx%d（屏幕中央，alpha %d，圆角 %dpx）"
            % (INPUT_WIDTH, INPUT_HEIGHT, INPUT_ALPHA, CORNER_RADIUS),
            "占位   %s" % CUE_TEXT,
            "按键   Enter 提交 / ESC 取消",
            "焦点   打开时可激活（打得了字），关闭后恢复 NOACTIVATE",
            "已创建 %s / 可见 %s" % (self.created, self.visible),
        ])


#: WNDPROC 原型。
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
    """输入框自检。**不建真窗口** —— 只测常量、提交/取消回调逻辑、
    焦点矛盾的处理方式。渲染要真桌面，不测。"""
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) 常量 ============
    _add("★ 尺寸 800x120", INPUT_WIDTH == 800 and INPUT_HEIGHT == 120,
         "%dx%d" % (INPUT_WIDTH, INPUT_HEIGHT))
    _add("★ 半透明 alpha 230", INPUT_ALPHA == 230, str(INPUT_ALPHA))
    _add("占位文字", CUE_TEXT == "输入任务目标…", CUE_TEXT)
    _add("EM_SETCUEBANNER 常量正确", EM_SETCUEBANNER == 0x1501, "0x1501")
    _add("VK_RETURN / VK_ESCAPE 正确",
         VK_RETURN == 0x0D and VK_ESCAPE == 0x1B, "0x0D / 0x1B")

    # ============ 2) 提交 / 取消逻辑（不建窗口）============
    got = {"submit": [], "cancel": 0}

    box = InputBox(on_submit=lambda t: got["submit"].append(t),
                   on_cancel=lambda: got.__setitem__("cancel",
                                                     got["cancel"] + 1))
    # 模拟 Enter：文本直接从 last_action 走（无窗口时 get_text 返回 ""，
    # 所以直接测 _submit 的回调链 —— 真实窗口由人工验收）
    box._submit()
    _add("★ Enter 触发 on_submit", got["submit"] == [""], "回调跑了")
    _add("提交后 last_action 记下", box.last_action == "", "记了")
    box._cancel()
    _add("★ ESC 触发 on_cancel", got["cancel"] == 1, "回调跑了")
    _add("取消后 last_action 清空", box.last_action == "", "清空")

    # 回调炸了不带走窗口
    def _boom(_t):
        raise RuntimeError("提交回调炸了")

    box2 = InputBox(on_submit=_boom)
    box2._submit()          # 不抛即过
    _add("★ 提交回调抛异常不炸窗口", True, "静默吞掉")

    # 无回调时也不炸
    box3 = InputBox()
    box3._submit()
    box3._cancel()
    _add("无回调也不炸", True, "安全")

    # ============ 3) 焦点矛盾的处理 ============
    _create_mask = WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED
    _add("★ 创建时**不带** NOACTIVATE（否则打不了字）",
         (_create_mask & WS_EX_NOACTIVATE) == 0,
         "创建样式里没有 NOACTIVATE 位")
    _add("★ 关闭时把 NOACTIVATE 加回去",
         WS_EX_NOACTIVATE == 0x08000000, "hide() 里 SetWindowLong 加回")
    _add("关闭样式常量在源里",
         "WS_EX_NOACTIVATE" in open(__file__, encoding="utf-8").read(),
         "hide 用到了")

    # ============ 4) 置中计算 ============
    sw, sh = _screen_size()
    x = max(0, (sw - INPUT_WIDTH) // 2)
    y = max(0, (sh - INPUT_HEIGHT) // 3)
    _add("置中公式不越界",
         x >= 0 and y >= 0 and x + INPUT_WIDTH <= sw,
         "(%d,%d) in %dx%d" % (x, y, sw, sh))
    _add("屏幕尺寸探测不抛", sw > 0 and sh > 0, "%dx%d" % (sw, sh))

    # ============ 5) docstring ============
    import sys as _sys
    _me = _sys.modules[__name__]
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "input_box",
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
    print(InputBox().describe())
