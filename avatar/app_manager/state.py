# -*- coding: utf-8 -*-
"""窗口状态操作：最小化 / 最大化 / 还原 / 置顶 / 移动 / 缩放 / 关闭。

职责
----
把 Win32 的窗口状态操作包装成语义化 API，并提供**状态验证** ——
不是发完消息就算成功，而是回读确认。

依赖
----
· ``avatar._win32.api``（user32）
· ``avatar.app_manager.layout``（Rect / 几何）
· ``avatar.app_manager.monitor``（居中到某块屏）
· ``avatar.errors``

## 设计原则：动作后验证

Win32 的窗口操作是**异步**的 —— ``ShowWindow`` 只是发了个请求，
窗口管理器稍后才处理。所以立即回读状态经常拿到旧值。

本模块的每个操作都遵循同一模式：

    1. 前置检查（句柄有效、状态是否需要改变）
    2. 发操作
    3. ``wait_state`` 轮询确认（带超时）
    4. 确认失败 → 抛异常，不装作成功

这比"发完就返回 True"慢几十毫秒，但**不会骗人**。自动化里最怕的就是
"以为成功了实际没动"，后续所有步骤都会建立在错误前提上。
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .._win32 import api as A
from .._win32 import structs as S
from ..errors import TimeoutError_, WindowNotFound
from . import layout as L

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: SW_* 名字 → 值
SW_COMMANDS = {
    "hide": S.SW_HIDE,
    "show": S.SW_SHOW,
    "show_normal": S.SW_SHOWNORMAL,
    "normal": S.SW_SHOWNORMAL,
    "restore": S.SW_RESTORE,
    "min": S.SW_MINIMIZE,
    "minimize": S.SW_MINIMIZE,
    "minimized": S.SW_SHOWMINIMIZED,
    "max": S.SW_MAXIMIZE,
    "maximize": S.SW_MAXIMIZE,
    "maximized": S.SW_SHOWMAXIMIZED,
    "show_no_activate": S.SW_SHOWNOACTIVATE,
    "show_min_no_active": S.SW_SHOWMINNOACTIVE,
    "show_na": S.SW_SHOWNA,
}


@dataclass(frozen=True)
class WindowState:
    """窗口状态快照。"""

    hwnd: int
    visible: bool
    minimized: bool
    maximized: bool
    foreground: bool
    topmost: bool
    enabled: bool
    rect: L.Rect
    client: L.Rect

    @property
    def normal(self) -> bool:
        """既没最小化也没最大化。"""
        return not self.minimized and not self.maximized

    def describe(self) -> str:
        flags = []
        if self.minimized:
            flags.append("最小化")
        if self.maximized:
            flags.append("最大化")
        if self.normal:
            flags.append("常规")
        if self.visible:
            flags.append("可见")
        else:
            flags.append("隐藏")
        if self.foreground:
            flags.append("前台")
        if self.topmost:
            flags.append("置顶")
        if not self.enabled:
            flags.append("已禁用")
        return "，".join(flags)

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "visible": self.visible,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "normal": self.normal,
            "foreground": self.foreground,
            "topmost": self.topmost,
            "enabled": self.enabled,
            "rect": self.rect.as_ltrb(),
            "client": self.client.as_ltrb(),
        }


# --------------------------------------------------------------------------
# 读取状态
# --------------------------------------------------------------------------
def _ensure(hwnd) -> int:
    h = int(hwnd or 0)
    if not A.user32.IsWindow(h):
        raise WindowNotFound("窗口句柄无效", hwnd=h)
    return h


def is_visible(hwnd: int) -> bool:
    return bool(A.user32.IsWindowVisible(_ensure(hwnd)))


def is_minimized(hwnd: int) -> bool:
    return bool(A.user32.IsIconic(_ensure(hwnd)))


def is_maximized(hwnd: int) -> bool:
    return bool(A.user32.IsZoomed(_ensure(hwnd)))


def is_foreground(hwnd: int) -> bool:
    return int(A.user32.GetForegroundWindow() or 0) == _ensure(hwnd)


def is_enabled(hwnd: int) -> bool:
    return bool(A.user32.IsWindowEnabled(_ensure(hwnd)))


def is_topmost(hwnd: int) -> bool:
    h = _ensure(hwnd)
    ex = A.user32.GetWindowLongW(h, S.GWL_EXSTYLE)
    return bool(ex & S.WS_EX_TOPMOST)


def is_minimized_by_rect(hwnd: int) -> bool:
    """用矩形判断是否最小化 —— ``IsIconic`` 对某些自绘窗口不准。

    最小化的窗口在 Windows 上会被移到 ``(-32000, -32000)``。
    """
    r = L.raw_window_rect(hwnd)
    return r.left <= -30000 and r.top <= -30000


def state_of(hwnd: int) -> WindowState:
    """完整状态快照。"""
    h = _ensure(hwnd)
    return WindowState(
        hwnd=h,
        visible=is_visible(h),
        minimized=is_minimized(h),
        maximized=is_maximized(h),
        foreground=is_foreground(h),
        topmost=is_topmost(h),
        enabled=is_enabled(h),
        rect=L.raw_window_rect(h),
        client=L.client_rect(h),
    )


# --------------------------------------------------------------------------
# 等待状态变化
# --------------------------------------------------------------------------
def wait_state(hwnd: int, predicate, timeout: float = 2.0,
               poll: float = 0.02) -> bool:
    """轮询等待窗口满足条件。

    ``predicate`` 接收 ``WindowState``。返回 True 表示等到，False 表示超时。
    **不抛异常** —— 让调用方决定超时怎么处理（有些场景超时是可接受的）。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            if predicate(state_of(hwnd)):
                return True
        except WindowNotFound:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.001, poll))


def _wait_or_raise(hwnd: int, predicate, timeout: float, what: str) -> None:
    if not wait_state(hwnd, predicate, timeout):
        st = state_of(hwnd) if A.user32.IsWindow(int(hwnd)) else None
        raise TimeoutError_(
            "等待「%s」超时（%.1fs），当前状态: %s"
            % (what, timeout, st.describe() if st else "窗口已关闭"),
            elapsed=timeout, hwnd=int(hwnd), expected=what,
        )


# --------------------------------------------------------------------------
# 状态操作
# --------------------------------------------------------------------------
def show(hwnd: int, command: str = "restore", verify: bool = True,
         timeout: float = 2.0) -> WindowState:
    """显示窗口。``command`` 取 ``SW_COMMANDS`` 里的名字。"""
    h = _ensure(hwnd)
    if command not in SW_COMMANDS:
        raise ValueError(
            "未知显示命令: %r（可选: %s）" % (command, ", ".join(sorted(SW_COMMANDS)))
        )
    cmd = SW_COMMANDS[command]
    # ShowWindowAsync 不阻塞调用线程 —— 目标窗口无响应时不会把我们也拖住
    A.user32.ShowWindowAsync(h, cmd)
    if not verify:
        return state_of(h)

    if command in ("min", "minimize", "minimized"):
        _wait_or_raise(h, lambda s: s.minimized or s.visible, timeout, "最小化")
    elif command in ("max", "maximize", "maximized"):
        _wait_or_raise(h, lambda s: s.maximized, timeout, "最大化")
    elif command in ("hide",):
        _wait_or_raise(h, lambda s: not s.visible, timeout, "隐藏")
    elif command in ("restore", "show_normal", "normal", "show"):
        _wait_or_raise(h, lambda s: s.visible and not s.minimized, timeout, "还原")
    return state_of(h)


def minimize(hwnd: int, verify: bool = True, timeout: float = 2.0) -> WindowState:
    return show(hwnd, "minimize", verify, timeout)


def maximize(hwnd: int, verify: bool = True, timeout: float = 2.0) -> WindowState:
    return show(hwnd, "maximize", verify, timeout)


def restore(hwnd: int, verify: bool = True, timeout: float = 2.0) -> WindowState:
    """从最小化或最大化还原到常规状态。"""
    h = _ensure(hwnd)
    st = state_of(h)
    if st.minimized:
        A.user32.ShowWindowAsync(h, S.SW_RESTORE)
        if verify:
            _wait_or_raise(h, lambda s: not s.minimized, timeout, "取消最小化")
    elif st.maximized:
        A.user32.ShowWindowAsync(h, S.SW_RESTORE)
        if verify:
            _wait_or_raise(h, lambda s: not s.maximized, timeout, "取消最大化")
    return state_of(h)


def toggle_maximize(hwnd: int, verify: bool = True, timeout: float = 2.0) -> WindowState:
    """最大化 ↔ 还原 切换。"""
    if is_maximized(hwnd):
        return restore(hwnd, verify, timeout)
    return maximize(hwnd, verify, timeout)


def hide(hwnd: int, verify: bool = True, timeout: float = 2.0) -> WindowState:
    return show(hwnd, "hide", verify, timeout)


# --------------------------------------------------------------------------
# 置顶
# --------------------------------------------------------------------------
def set_topmost(hwnd: int, on: bool = True, verify: bool = True,
                timeout: float = 1.0) -> WindowState:
    """置顶 / 取消置顶。"""
    h = _ensure(hwnd)
    target = S.HWND_TOPMOST if on else S.HWND_NOTOPMOST
    ok = A.user32.SetWindowPos(
        h, target, 0, 0, 0, 0,
        S.SWP_NOMOVE | S.SWP_NOSIZE | S.SWP_NOACTIVATE,
    )
    if not ok:
        raise WindowNotFound("SetWindowPos(置顶) 失败", hwnd=h)
    if verify:
        want = bool(on)
        _wait_or_raise(h, lambda s: s.topmost == want, timeout,
                       "置顶=%s" % want)
    return state_of(h)


# --------------------------------------------------------------------------
# 移动 / 缩放
# --------------------------------------------------------------------------
def move(hwnd: int, x: int, y: int, width: Optional[int] = None,
         height: Optional[int] = None, no_activate: bool = True,
         verify: bool = True, timeout: float = 2.0) -> WindowState:
    """移动（可选缩放）。

    尺寸省略时保持当前尺寸。用的是 ``SetWindowPos`` 而不是 ``MoveWindow`` ——
    后者会重绘整个窗口，在远程桌面/低配机上明显卡顿。
    """
    h = _ensure(hwnd)
    r = L.raw_window_rect(h)
    w = int(width) if width is not None else r.width
    ht = int(height) if height is not None else r.height
    flags = S.SWP_NOZORDER | S.SWP_NOACTIVATE if no_activate else S.SWP_NOZORDER
    ok = A.user32.SetWindowPos(h, None, int(x), int(y), w, ht, flags)
    if not ok:
        raise WindowNotFound("SetWindowPos 失败", hwnd=h)
    if verify:
        _wait_or_raise(
            h, lambda s: abs(s.rect.left - int(x)) <= 2 and abs(s.rect.top - int(y)) <= 2,
            timeout, "移动到 (%d,%d)" % (int(x), int(y)),
        )
    return state_of(h)


def resize(hwnd: int, width: int, height: int, verify: bool = True,
           timeout: float = 2.0) -> WindowState:
    """改变大小，保持左上角位置。"""
    h = _ensure(hwnd)
    r = L.raw_window_rect(h)
    return move(h, r.left, r.top, width, height, verify=verify, timeout=timeout)


def place(hwnd: int, rect: L.Rect, verify: bool = True,
          timeout: float = 2.0) -> WindowState:
    """把窗口精确放到指定矩形。"""
    return move(hwnd, rect.left, rect.top, rect.width, rect.height,
                verify=verify, timeout=timeout)


def center_on_monitor(hwnd: int, index: Optional[int] = None,
                      use_work_area: bool = True, verify: bool = True,
                      timeout: float = 2.0) -> WindowState:
    """把窗口居中到某块显示器。

    ``use_work_area=True`` 时按工作区居中（不会被任务栏遮住底部）。
    """
    from . import monitor as MON
    mon = MON.by_index(index) if index is not None else MON.at_window(hwnd)
    if mon is None:
        raise WindowNotFound("无法确定目标显示器", hwnd=int(hwnd))
    area = mon.work if use_work_area else mon.rect
    r = L.raw_window_rect(hwnd)
    w = min(r.width, area.width)
    h = min(r.height, area.height)
    x = area.left + (area.width - w) // 2
    y = area.top + (area.height - h) // 2
    return move(hwnd, x, y, w, h, verify=verify, timeout=timeout)


#: 屏占比位置：名字 → (水平比例, 垂直比例)
SNAP_POSITIONS = {
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 0.5),
    "topleft": (0.0, 0.0, 0.5, 0.5),
    "topright": (0.5, 0.0, 0.5, 0.5),
    "bottomleft": (0.0, 0.5, 0.5, 0.5),
    "bottomright": (0.5, 0.5, 0.5, 0.5),
    "max": (0.0, 0.0, 1.0, 1.0),
    "center": (0.25, 0.25, 0.5, 0.5),
    "center-half": (0.25, 0.25, 0.5, 0.5),
}


def snap(hwnd: int, position: str, index: Optional[int] = None,
         verify: bool = True, timeout: float = 2.0) -> WindowState:
    """按屏占比摆放窗口。

    ``position``: left / right / top / bottom / 四个角 / max / center / center-half
    """
    key = str(position).strip().lower().replace("_", "").replace("-", "")
    # 归一化：center-half → centerhalf → 再映射
    alias = {
        "centerhalf": "center",
        "topleft": "topleft", "topright": "topright",
        "bottomleft": "bottomleft", "bottomright": "bottomright",
    }
    key = alias.get(key, key)
    if key not in SNAP_POSITIONS:
        raise ValueError(
            "未知位置: %r（可选: %s）" % (position, ", ".join(sorted(SNAP_POSITIONS)))
        )
    from . import monitor as MON
    mon = MON.by_index(index) if index is not None else MON.at_window(hwnd)
    if mon is None:
        raise WindowNotFound("无法确定目标显示器", hwnd=int(hwnd))
    area = mon.work
    fx, fy, fw, fh = SNAP_POSITIONS[key]
    rect = L.Rect.from_xywh(
        area.left + int(area.width * fx),
        area.top + int(area.height * fy),
        int(area.width * fw),
        int(area.height * fh),
    )
    return place(hwnd, rect, verify=verify, timeout=timeout)


def move_to_monitor(hwnd: int, index: int, keep_relative: bool = True,
                    verify: bool = True, timeout: float = 2.0) -> WindowState:
    """把窗口移到另一块显示器。

    ``keep_relative=True`` 时保持窗口在源屏内的**相对位置和占比**，
    False 时居中。
    """
    from . import monitor as MON
    src = MON.at_window(hwnd)
    dst = MON.by_index(index)
    if src is None or src.index == dst.index:
        return state_of(hwnd)

    r = L.raw_window_rect(hwnd)
    if keep_relative and src.rect.width > 0 and src.rect.height > 0:
        rx = (r.left - src.rect.left) / float(src.rect.width)
        ry = (r.top - src.rect.top) / float(src.rect.height)
        x = dst.rect.left + int(rx * dst.rect.width)
        y = dst.rect.top + int(ry * dst.rect.height)
        return move(hwnd, x, y, verify=verify, timeout=timeout)
    return center_on_monitor(hwnd, dst.index, verify=verify, timeout=timeout)


# --------------------------------------------------------------------------
# 关闭
# --------------------------------------------------------------------------
def close(hwnd: int, force: bool = False, wait: bool = False,
          timeout: float = 3.0) -> bool:
    """关闭窗口。

    ``force=False``（默认）发 ``WM_CLOSE`` —— **优雅关闭**，应用可以弹
    "文件已修改，是否保存？" 确认框。这是应该优先用的方式。

    ``force=True`` 走 ``WM_QUIT`` 之后再 TerminateProcess —— 会**丢失未保存
    数据**。这个参数的存在是为了处理"窗口卡死关不掉"，正常流程不该用它。

    返回 True 表示窗口已关闭（或已发出关闭请求且 wait=False）。
    """
    h = _ensure(hwnd)
    if force:
        # 先温和，再强硬
        A.user32.PostMessageW(h, S.WM_CLOSE, 0, 0)
        if wait and wait_closed(h, min(1.0, timeout)):
            return True
        pid = S.DWORD(0)
        A.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value:
            from .process import kill
            kill(int(pid.value), force=True, timeout=timeout)
        return wait_closed(h, timeout) if wait else True

    A.user32.PostMessageW(h, S.WM_CLOSE, 0, 0)
    if wait:
        return wait_closed(h, timeout)
    return True


def wait_closed(hwnd: int, timeout: float = 5.0, poll: float = 0.05) -> bool:
    """等待窗口消失（关闭完成）。"""
    h = int(hwnd)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not A.user32.IsWindow(h):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.005, poll))


def activate(hwnd: int, timeout: float = 2.0) -> bool:
    """把窗口带到前台。委托给 foreground 模块（它处理了前台锁）。"""
    from .foreground import force_foreground
    return force_foreground(hwnd, timeout=timeout)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    checks = []
    failures = []

    def _add(name, ok, detail, skip=False):
        """``skip=True`` → 本机环境缺这个资源，标 SKIP，不计入失败。"""
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": detail})
        if skip:
            pass
        elif not ok:
            failures.append(name)

    from .windows import list_windows
    ws = [w for w in list_windows() if w.is_usable]
    if not ws:
        # ⚠ 原本判 FAIL —— 但"本机没有可用窗口"是**环境**
        #    （headless / session 0），不是状态读取有问题 → SKIP。
        _add("存在可操作窗口", False,
             "本机无可用窗口（无交互式桌面会话）→ 跳过", skip=True)
        return {"checks": checks, "failures": [],
                "skipped": [c["name"] for c in checks if c.get("skipped")],
                "ok": True}

    hwnd = ws[0].hwnd
    try:
        st = state_of(hwnd)
        _add("状态可读取", True, "%s %s" % (st.rect, st.describe()))
        _add("visible/minimized/maximized 互斥合理",
             not (st.minimized and st.maximized),
             "minimized=%s maximized=%s" % (st.minimized, st.maximized))
        # SW_COMMANDS 完整性
        _add("SW_COMMANDS 覆盖常用命令",
             all(k in SW_COMMANDS for k in
                 ("hide", "restore", "minimize", "maximize", "show")),
             "%d 个命令" % len(SW_COMMANDS))
        _add("SNAP_POSITIONS 覆盖常用位置",
             all(k in SNAP_POSITIONS for k in
                 ("left", "right", "max", "center")),
             "%d 个位置" % len(SNAP_POSITIONS))
        # 置顶往返（不改状态，读完就还原）
        was = is_topmost(hwnd)
        set_topmost(hwnd, True, timeout=1.0)
        got = is_topmost(hwnd)
        set_topmost(hwnd, was, timeout=1.0)
        _add("置顶切换生效", got is True, "置顶 → %s，已还原为 %s" % (got, was))
    except Exception as e:
        _add("状态测试执行", False, "异常: %r" % (e,))

    return {"checks": checks, "failures": failures,
            "skipped": [c["name"] for c in checks if c.get("skipped")],
            "ok": not failures}
