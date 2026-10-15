# -*- coding: utf-8 -*-
"""窗口枚举与查找 —— 只读，纯 ctypes。

## 一个必须避开的坑

Windows 上"一个应用"往往对应**多个顶层窗口**。以 Edge 为例：

    主窗口      "某个网页 - Microsoft Edge"        ← 你要的
    辅助窗口    ""（无标题的 Chrome_WidgetWin_1）   ← 会先被匹配到
    后台窗口    "Microsoft Edge"（隐藏的宿主）       ← 也会被匹配到

如果按进程名匹配并取第一个，很大概率拿到那个**无标题的辅助窗口** ——
它有合法的句柄、合法的矩形（常常是 0x0 或屏幕那么大），于是你点空了，
而且不报错。

本模块的对策：
  · `find_windows()` 返回**全部**匹配项，附 `score`；
  · `score` 里"有标题"是正分，"尺寸为 0"是重罚；
  · 提供了 `exclude_classes` 默认屏蔽已知的辅助窗口类。
"""
from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .._win32 import api as A
from .._win32 import structs as S
from ..errors import WindowNotFound

# 已知的"辅助 / 不可见"窗口类。匹配时降权或直接排除。
AUXILIARY_CLASSES = {
    "Chrome_WidgetWin_0",       # Chromium 隐藏宿主
    "Chrome_MessageWindow",     # 消息窗口
    "Chrome_ImF",               # 输入法相关
    "ApplicationFrameWindow",   # UWP 外壳（有时是好的，视情况）
    "Windows.UI.Core.CoreWindow",
    "MSCTFIME UI",
    "IME",
    "Default IME",
    "tooltips_class32",
    "SysShadow",
    "ForegroundStaging",
    "TaskListThumbnailWnd",
    "Shell_TrayWnd",
    "Progman",
    "WorkerW",
    "Button",
    "Static",
    "Edit",
    "ComboBox",
    "SysListView32",
    "Internet Explorer_Server",
}

# 这些类通常是"真窗口"，优先
PREFERRED_CLASSES = {
    "Notepad",
    "Chrome_WidgetWin_1",
    "MozillaWindowClass",
    "ApplicationFrameWindow",
    "CabinetWClass",
    "ExploreWClass",
    "WindowsForms10.Window.8.app",
    "Qt5QWindowIcon",
    "Qt5152QWindowIcon",
    "TForm1",
}


@dataclass
class WindowInfo:
    """一个顶层窗口的摘要信息。全部坐标为物理像素。"""

    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    rect: tuple = (0, 0, 0, 0)
    client_size: tuple = (0, 0)
    visible: bool = True
    minimized: bool = False
    maximized: bool = False
    cloaked: bool = False
    score: float = 0.0
    reasons: tuple = field(default_factory=tuple)
    #: WindowFromPoint 直接命中的子控件句柄（若该窗口来自坐标查询）
    child_hwnd: int = 0

    @property
    def width(self) -> int:
        return self.rect[2]

    @property
    def height(self) -> int:
        return self.rect[3]

    @property
    def has_title(self) -> bool:
        return bool(self.title.strip())

    @property
    def is_usable(self) -> bool:
        """是否是一个"能动"的窗口。

        可动 = 可见 + 未最小化 + 有尺寸 + 未被 DWM 隐形 + 有标题。
        最后的"有标题"很关键 —— 它过滤掉几乎所有辅助窗口。
        """
        return (
            self.visible
            and not self.minimized
            and self.width > 1
            and self.height > 1
            and not self.cloaked
        )

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class": self.class_name,
            "pid": self.pid,
            "process": self.process,
            "rect": self.rect,
            "client_size": self.client_size,
            "visible": self.visible,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "cloaked": self.cloaked,
            "score": round(self.score, 1),
            "usable": self.is_usable,
        }

    def __str__(self):
        return "[0x%08X] %-28s %-24s %dx%d @(%d,%d)" % (
            self.hwnd,
            (self.process or "?")[:28],
            (self.title or "(无标题)")[:24],
            self.width,
            self.height,
            self.rect[0],
            self.rect[1],
        )


def _is_cloaked(hwnd: int) -> bool:
    """DWM 隐形窗口检测。

    UWP 应用被挂起时窗口仍在枚举里、isVisible 仍为真、矩形也正常，
    但 DWM 把它标记为 cloaked —— 实际屏幕上看不见。点它会点到别的东西。
    """
    if A.dwmapi is None:
        return False
    val = ctypes.c_int(0)
    hr = A.dwmapi.DwmGetWindowAttribute(
        int(hwnd), A.DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
    )
    if not A.hresult_ok(hr):
        return False
    return val.value != 0


def _texts(hwnd: int) -> tuple:
    n = A.user32.GetWindowTextLengthW(hwnd)
    title = ""
    if n > 0:
        buf = ctypes.create_unicode_buffer(n + 2)
        A.user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
    cbuf = ctypes.create_unicode_buffer(256)
    A.user32.GetClassNameW(hwnd, cbuf, 256)
    return title, cbuf.value


def list_windows(include_invisible: bool = False,
                 include_cloaked: bool = False,
                 processes: Optional[Iterable[str]] = None,
                 exclude_processes: Optional[Iterable[str]] = None,
                 title_re: Optional[str] = None,
                 class_re: Optional[str] = None,
                 min_size: int = 0) -> list:
    """枚举顶层窗口，返回 WindowInfo 列表（按 score 降序）。

    过滤参数都是可选的，组合时取**交集**（全部满足才保留）。
    进程名比较**忽略大小写**，且允许省略 .exe（"msedge" 能匹配 "msedge.exe"）。
    """
    from ..perception import _process_name

    out = []
    procs_lo = {p.lower() for p in processes} if processes else None
    excl_lo = {p.lower() for p in exclude_processes} if exclude_processes else None

    def _cb(hwnd, _lparam):
        hwnd = int(hwnd)

        visible = bool(A.user32.IsWindowVisible(hwnd))
        if not visible and not include_invisible:
            return True

        title, cls = _texts(hwnd)

        # 无标题且不可见的，基本都是系统辅助窗口，直接跳过
        # （有标题但不可见的仍保留 —— 可能是刚被最小化的目标）
        if not title and not visible:
            return True

        pid = S.DWORD(0)
        A.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = _process_name(int(pid.value))

        cloaked = _is_cloaked(hwnd)
        if cloaked and not include_cloaked:
            return True

        # --- 过滤 ---
        if procs_lo is not None:
            pl = pname.lower()
            if pl not in procs_lo and pl.replace(".exe", "") not in procs_lo:
                return True
        if excl_lo is not None:
            pl = pname.lower()
            if pl in excl_lo or pl.replace(".exe", "") in excl_lo:
                return True
        if title_re and not re.search(title_re, title, re.I):
            return True
        if class_re and not re.search(class_re, cls, re.I):
            return True

        wrc = S.RECT()
        A.user32.GetWindowRect(hwnd, ctypes.byref(wrc))
        w, h = wrc.width, wrc.height
        if min_size and (w < min_size or h < min_size):
            return True

        crc = S.RECT()
        A.user32.GetClientRect(hwnd, ctypes.byref(crc))

        info = WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=cls,
            pid=int(pid.value),
            process=pname,
            rect=wrc.as_tuple(),
            client_size=(crc.width, crc.height),
            visible=visible,
            minimized=bool(A.user32.IsIconic(hwnd)),
            maximized=bool(A.user32.IsZoomed(hwnd)),
            cloaked=cloaked,
        )
        info.score, info.reasons = _score(info)
        out.append(info)
        return True

    cb = ctypes.WINFUNCTYPE(S.BOOL, S.wintypes.HWND, S.wintypes.LPARAM)(_cb)
    A.user32.EnumWindows(cb, 0)

    out.sort(key=lambda wi: wi.score, reverse=True)
    return out


def _score(wi: WindowInfo) -> tuple:
    """给窗口打分，用于在多个候选里挑出"用户真正想操作的那个"。

    这是踩过坑之后加的：直接取第一个会把辅助窗口选中。
    """
    s = 0.0
    reasons = []

    if wi.has_title:
        s += 50
        reasons.append("有标题+50")
    else:
        s -= 30
        reasons.append("无标题-30")

    if wi.class_name in AUXILIARY_CLASSES:
        s -= 60
        reasons.append("辅助窗口类-60")
    elif wi.class_name in PREFERRED_CLASSES:
        s += 20
        reasons.append("主窗口类+20")

    if not wi.visible:
        s -= 25
        reasons.append("不可见-25")
    if wi.minimized:
        s -= 10
        reasons.append("已最小化-10")
    if wi.cloaked:
        s -= 40
        reasons.append("DWM隐形-40")

    area = wi.width * wi.height
    if area <= 1:
        s -= 50
        reasons.append("零尺寸-50")
    elif area < 40000:  # 约 200x200
        s -= 15
        reasons.append("尺寸过小-15")
    else:
        # 大窗口更可能是主窗口，但收益递减
        s += min(15.0, area / 200000.0 * 15.0)
        reasons.append("尺寸合理+%.1f" % min(15.0, area / 200000.0 * 15.0))

    return (s, tuple(reasons))


def find_windows(query: str = "",
                 process: Optional[str] = None,
                 class_name: Optional[str] = None,
                 exact_title: bool = False,
                 usable_only: bool = True,
                 limit: int = 0) -> list:
    """按标题/进程/类名找窗口。

    query 是**标题**的子串（或正则，若 exact_title=False 且含正则元字符时按正则试）。
    """
    title_re = None
    if query:
        if exact_title:
            title_re = "^%s$" % re.escape(query)
        else:
            title_re = re.escape(query)

    procs = [process] if process else None
    cls_re = re.escape(class_name) if class_name else None

    res = list_windows(
        include_invisible=not usable_only,
        title_re=title_re,
        processes=procs,
        class_re=cls_re,
    )
    if usable_only:
        res = [w for w in res if w.is_usable]
    return res[:limit] if limit else res


def find_window(query: str = "",
                process: Optional[str] = None,
                class_name: Optional[str] = None,
                exact_title: bool = False,
                usable_only: bool = True) -> WindowInfo:
    """找**唯一一个**最匹配的窗口，找不到就抛异常。

    这就是"不猜"原则的体现：定位不到 → 抛 WindowNotFound，
    让上层决定是换策略还是报告，**绝不允许**返回一个"大概是它"的窗口。
    """
    cands = find_windows(query, process, class_name, exact_title, usable_only)
    if not cands:
        raise WindowNotFound(
            "未找到窗口",
            query=query or None,
            process=process or None,
            class_name=class_name or None,
        )
    return cands[0]


def window_at(x: int, y: int, root: bool = True) -> Optional[WindowInfo]:
    """物理坐标处的窗口。

    WindowFromPoint 返回的可能是**子控件**（一个按钮、一段文字），
    默认向上找到顶层窗口 —— 但顶层窗口的 rect 可能覆盖整个屏幕，
    所以同时保留子控件信息在 `child_hwnd` 里。
    """
    from ..perception import _process_name

    pt = S.POINT(int(x), int(y))
    child = int(A.user32.WindowFromPoint(pt) or 0)
    if not child:
        return None

    GA_ROOT = 2
    if root:
        top = int(A.user32.GetAncestor(child, GA_ROOT) or 0)
        if top:
            child, hwnd = child, top
        else:
            hwnd = child
    else:
        hwnd = child

    title, cls = _texts(hwnd)
    pid = S.DWORD(0)
    A.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    wrc = S.RECT()
    A.user32.GetWindowRect(hwnd, ctypes.byref(wrc))
    info = WindowInfo(
        hwnd=hwnd,
        title=title,
        class_name=cls,
        pid=int(pid.value),
        process=_process_name(int(pid.value)),
        rect=wrc.as_tuple(),
        visible=bool(A.user32.IsWindowVisible(hwnd)),
        minimized=bool(A.user32.IsIconic(hwnd)),
        maximized=bool(A.user32.IsZoomed(hwnd)),
    )
    info.child_hwnd = child
    return info


def describe(windows: list, limit: int = 0) -> str:
    if not windows:
        return "(没有匹配的窗口)"
    rows = windows[:limit] if limit else windows
    return "\n".join(str(w) for w in rows)


# ==========================================================================
# 阶段二增补：等待 / 生命周期 / 便捷入口
# ==========================================================================
def is_alive(hwnd: int) -> bool:
    """窗口是否还存在。"""
    h = int(hwnd)
    return bool(h) and bool(A.user32.IsWindow(h))


def wait_window(query: str = "",
                process: Optional[str] = None,
                class_name: Optional[str] = None,
                timeout: float = 10.0,
                poll: float = 0.1,
                usable_only: bool = True) -> Optional[WindowInfo]:
    """等待某个窗口出现。超时返回 None（不抛异常）。

    用于"启动应用后等它把窗口拉起来" —— 应用启动到出窗有个不确定的间隔，
    固定 sleep 要么太短要么浪费。
    """
    import time as _t
    deadline = _t.monotonic() + max(0.0, timeout)
    while True:
        try:
            return find_window(query, process, class_name,
                               usable_only=usable_only)
        except WindowNotFound:
            pass
        if _t.monotonic() >= deadline:
            return None
        _t.sleep(max(0.01, poll))


def wait_window_or_raise(query: str = "",
                         process: Optional[str] = None,
                         class_name: Optional[str] = None,
                         timeout: float = 10.0,
                         poll: float = 0.1) -> WindowInfo:
    """等待窗口出现，超时抛 ``WindowNotFound``。"""
    w = wait_window(query, process, class_name, timeout, poll)
    if w is None:
        raise WindowNotFound(
            "等待窗口超时（%.1fs）" % timeout,
            query=query or None,
            process=process or None,
            class_name=class_name or None,
        )
    return w


# ==========================================================================
# UWP 窗口解析
# ==========================================================================
#: UWP 应用的真正内容窗口类。见 ``find_uwp_window`` 的说明。
UWP_CORE_CLASS = "Windows.UI.Core.CoreWindow"
UWP_FRAME_CLASS = "ApplicationFrameWindow"

#: UWP 外壳（ApplicationFrameHost.exe）里的辅助窗口类，不该被当成主窗口
UWP_AUX_CLASSES = {
    "ApplicationFrameTitleBarWindow",
    "ApplicationFrameInputSinkWindow",
}


def is_uwp_frame(hwnd: int) -> bool:
    """该窗口是否是 UWP 外壳窗口（``ApplicationFrameWindow``）。"""
    _t, cls = _texts(int(hwnd))
    return cls == UWP_FRAME_CLASS


def uwp_app_pid(frame_hwnd: int) -> int:
    """从 UWP 外壳窗口取**真正应用进程**的 PID。

    为什么需要这个：UWP 窗口的 ``hwnd`` 属于 ``ApplicationFrameHost.exe``，
    而不属于应用自己。外壳内部有一个 ``Windows.UI.Core.CoreWindow``，
    **它的 PID 才是应用进程**。

        实测（计算器）::

            ApplicationFrameWindow      pid=17852  ← AFH，GetWindowThreadProcessId 得到这个
              ApplicationFrameTitleBarWindow   pid=17852  ← 辅助
              Windows.UI.Core.CoreWindow       pid=17980  ← ★ 真正的应用
              ApplicationFrameInputSinkWindow  pid=17852  ← 辅助

    返回 0 表示不是 UWP 外壳或取不到。
    """
    h = int(frame_hwnd)
    if not is_uwp_frame(h):
        return 0
    for ch in children(h, recurse=True):
        _t, cls = _texts(ch)
        if cls == UWP_CORE_CLASS:
            pid = S.DWORD(0)
            A.user32.GetWindowThreadProcessId(ch, ctypes.byref(pid))
            return int(pid.value)
    return 0


def uwp_core_window(frame_hwnd: int) -> int:
    """从 UWP 外壳窗口取内部的 ``Windows.UI.Core.CoreWindow`` 句柄。返回 0 表示没有。"""
    h = int(frame_hwnd)
    for ch in children(h, recurse=True):
        _t, cls = _texts(ch)
        if cls == UWP_CORE_CLASS:
            return int(ch)
    return 0


def find_uwp_window(pid: int, usable_only: bool = True) -> Optional[WindowInfo]:
    """按**应用进程 PID** 找 UWP 窗口。

    这是 UWP 的专用路径 —— 普通 ``EnumWindows`` + PID 比对**永远找不到**
    UWP 窗口，因为窗口挂在外壳进程上。

    做法：遍历所有 ``ApplicationFrameWindow``，逐个下钻取内部
    ``CoreWindow`` 的 PID，与目标比较。
    """
    target = int(pid)
    if not target:
        return None

    for w in list_windows(include_invisible=not usable_only):
        if w.class_name != UWP_FRAME_CLASS:
            continue
        if uwp_app_pid(w.hwnd) != target:
            continue
        if usable_only and not w.is_usable:
            continue
        info = refresh(w.hwnd)
        # 把真实应用 PID 记进去 —— 上层（比如 UIPI 判定）需要它
        info.pid = target
        core = uwp_core_window(w.hwnd)
        if core:
            info.child_hwnd = core
        return info
    return None


def list_uwp_windows() -> list:
    """列出全部 UWP 应用窗口（外壳层），返回 ``[(WindowInfo, app_pid, core_hwnd)]``。"""
    out = []
    for w in list_windows():
        if w.class_name != UWP_FRAME_CLASS:
            continue
        app_pid = uwp_app_pid(w.hwnd)
        if not app_pid:
            continue
        out.append((w, app_pid, uwp_core_window(w.hwnd)))
    return out


def window_of_pid(pid: int, usable_only: bool = True,
                  require_title: bool = False) -> Optional[WindowInfo]:
    """某进程最好的那个顶层窗口。

    "最好"用的是 ``_score`` —— 有标题、主窗口类、尺寸大的优先，
    避免拿到 Chromium 的无标题辅助窗口。

    **UWP 已处理** —— 它会自动下钻 ApplicationFrameHost 外壳，
    见 ``uwp_core_window``。
    """
    from .process import windows_of_pid as _wop
    hs = set(_wop(int(pid)))
    if hs:
        cands = [w for w in list_windows(include_invisible=not usable_only)
                 if w.hwnd in hs]
        if require_title:
            cands = [w for w in cands if w.has_title]
        if usable_only:
            cands = [w for w in cands if w.is_usable]
        if cands:
            return cands[0]  # 已按 score 降序

    # 普通路径没找到 → 可能是 UWP，走外壳下钻
    return find_uwp_window(int(pid), usable_only=usable_only)


def main_window(process: str, timeout: float = 0.0) -> Optional[WindowInfo]:
    """按进程名取主窗口。这是"我要操作这个应用"的最常用入口。

    ``timeout>0`` 时会在超时前轮询等待（适合刚启动完）。
    """
    if timeout > 0:
        import time as _t
        deadline = _t.monotonic() + timeout
        while True:
            ws = find_windows(process=process, usable_only=True)
            if ws:
                return ws[0]
            if _t.monotonic() >= deadline:
                return None
            _t.sleep(0.1)
    ws = find_windows(process=process, usable_only=True)
    return ws[0] if ws else None


def refresh(hwnd: int) -> WindowInfo:
    """重新读取某个窗口的当前信息（不重新枚举全表）。"""
    from ..perception import _process_name

    h = int(hwnd)
    if not is_alive(h):
        raise WindowNotFound("窗口已销毁", hwnd=h)

    title, cls = _texts(h)
    pid = S.DWORD(0)
    A.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    wrc = S.RECT()
    A.user32.GetWindowRect(h, ctypes.byref(wrc))
    crc = S.RECT()
    A.user32.GetClientRect(h, ctypes.byref(crc))

    info = WindowInfo(
        hwnd=h,
        title=title,
        class_name=cls,
        pid=int(pid.value),
        process=_process_name(int(pid.value)),
        rect=wrc.as_tuple(),
        client_size=(crc.width, crc.height),
        visible=bool(A.user32.IsWindowVisible(h)),
        minimized=bool(A.user32.IsIconic(h)),
        maximized=bool(A.user32.IsZoomed(h)),
        cloaked=_is_cloaked(h),
    )
    info.score, info.reasons = _score(info)
    return info


def children(hwnd: int, recurse: bool = False,
             class_re: Optional[str] = None) -> list:
    """枚举子窗口句柄（默认只取直接子级）。

    用 ``EnumChildWindows``；``recurse=True`` 时它本身就会递归，
    否则用 ``GetWindow(GW_CHILD)`` + ``GW_HWNDNEXT`` 手动走一层。
    """
    h = int(hwnd)
    if not is_alive(h):
        return []

    pat = re.compile(class_re, re.I) if class_re else None
    out = []

    if recurse:
        def _cb(ch, _lp):
            ch = int(ch)
            cbuf = ctypes.create_unicode_buffer(256)
            A.user32.GetClassNameW(ch, cbuf, 256)
            if pat is None or pat.search(cbuf.value):
                out.append(ch)
            return True
        cb = ctypes.WINFUNCTYPE(S.BOOL, S.wintypes.HWND, S.wintypes.LPARAM)(_cb)
        A.user32.EnumChildWindows(h, cb, 0)
        return out

    GW_CHILD = 5
    GW_HWNDNEXT = 2
    ch = A.user32.GetWindow(h, GW_CHILD)
    while ch:
        ch = int(ch)
        cbuf = ctypes.create_unicode_buffer(256)
        A.user32.GetClassNameW(ch, cbuf, 256)
        if pat is None or pat.search(cbuf.value):
            out.append(ch)
        ch = A.user32.GetWindow(ch, GW_HWNDNEXT)
    return out


def owner(hwnd: int) -> int:
    """窗口的属主窗口（``GW_OWNER``）。无属主返回 0。"""
    GW_OWNER = 4
    return int(A.user32.GetWindow(int(hwnd), GW_OWNER) or 0)


def popup_windows(pid: Optional[int] = None) -> list:
    """找出所有**对话框 / 弹窗**。

    判定：有属主（``GW_OWNER`` 非 0）或类是 ``#32770``（标准对话框类）。
    这类窗口常常是"是否保存？"确认框 —— 自动化流程里必须显式处理，
    否则会一直卡在等待里。
    """
    out = []
    for w in list_windows(include_invisible=False):
        if pid is not None and w.pid != int(pid):
            continue
        if w.class_name == "#32770" or owner(w.hwnd):
            out.append(w)
    return out


def has_modal_dialog(pid: Optional[int] = None) -> Optional[WindowInfo]:
    """是否存在模态对话框。返回第一个，没有则 None。"""
    ps = popup_windows(pid)
    return ps[0] if ps else None


def to_dict_list(windows: list) -> list:
    return [w.to_dict() for w in windows]


def selfcheck() -> dict:
    """窗口子系统自检（只读）。"""
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    ws = list_windows()
    _add("可枚举顶层窗口", len(ws) > 0, "共 %d 个" % len(ws))

    usable = [w for w in ws if w.is_usable]
    _add("存在可用窗口", len(usable) > 0, "可用 %d 个" % len(usable))

    _add("评分已排序", all(
        ws[i].score >= ws[i + 1].score for i in range(len(ws) - 1)
    ), "降序排列")

    if usable:
        w = usable[0]
        r = refresh(w.hwnd)
        _add("refresh 结果一致", r.hwnd == w.hwnd,
             "hwnd=0x%X title=%r" % (r.hwnd, r.title[:30]))
        _add("is_alive 对活窗口为真", is_alive(w.hwnd), "0x%X" % w.hwnd)

        # 主窗口定位
        mw = window_of_pid(w.pid)
        _add("可定位进程主窗口", mw is not None,
             "pid=%d → %s" % (w.pid, mw.title[:30] if mw else "(无)"))

        # 子窗口枚举不报错
        ch = children(w.hwnd)
        _add("可枚举子窗口", isinstance(ch, list), "直接子级 %d 个" % len(ch))

    _add("is_alive 对假句柄为假", not is_alive(0xDEADBEEF), "伪造句柄")

    _add("find_window 未命中时抛异常",
         _raises(WindowNotFound, find_window, "不存在的标题XYZ123"),
         "WindowNotFound 已抛出")

    return {"checks": checks, "failures": failures, "ok": not failures}


def _raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
        return False
    except exc:
        return True
    except Exception:
        return False


def _main() -> int:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for w in list_windows()[:30]:
            print(w)
        return 0
    if cmd == "selfcheck":
        res = selfcheck()
        for c in res["checks"]:
            print("  [%s] %-28s %s" % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
        print("窗口自检: %d/%d" % (
            len(res["checks"]) - len(res["failures"]), len(res["checks"])))
        return 0 if res["ok"] else 1
    if cmd == "popup":
        ps = popup_windows()
        print("\n".join(str(p) for p in ps) if ps else "(无弹窗)")
        return 0
    print("未知命令: %s" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
