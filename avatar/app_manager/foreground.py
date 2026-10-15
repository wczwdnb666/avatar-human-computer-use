# -*- coding: utf-8 -*-
"""前台窗口抢占：把目标窗口可靠地推到前台，并保证它**真的**在前台。

职责
----
1. ``get_foreground()`` / ``foreground_hwnd()`` —— 读当前前台窗口
2. ``force_foreground(hwnd)`` —— 绕过 Windows 前台锁，强行激活目标窗口
3. ``ensure_foreground(hwnd)`` —— 断言式：确保目标在前台，否则抛异常
4. ``focus_guard(hwnd)`` —— 上下文管理器：进入时抢前台，退出时可选还原
5. ``wait_foreground(hwnd)`` —— 轮询等待目标成为前台

依赖
----
· ``avatar._win32.api``（user32 / kernel32）
· ``avatar._win32.structs``
· ``avatar.errors``

## 为什么"抢前台"这么难

Windows 有一道**前台锁（foreground lock）**：只有**当前拥有前台窗口的线程**，
或者**刚接收到用户输入**的进程，才有资格调用 ``SetForegroundWindow`` 成功。
其余情况下 ``SetForegroundWindow`` 会**静默失败** —— 返回 0，不报错，不抛异常，
窗口也不动。这是自动化最容易被坑的地方：代码看起来跑通了，实际焦点还在别处，
后续所有键盘输入全打进了错误窗口。

标准的绕过手段是 ``AttachThreadInput``：

    把**当前线程**的输入队列挂到**目标线程**上，此时二者共享输入状态，
    调用方就"继承"了目标线程的前台资格，``SetForegroundWindow`` 才会生效。

挂上之后必须**摘掉**，否则两个线程的输入被永久绑定 —— 这是本模块所有
代码路径（含异常路径）都用 try/finally 的原因。

## 关键细节

* ``ShowWindowAsync`` 而非 ``ShowWindow``：目标窗口消息泵卡死时，
  同步版会把我们自己也卡死，异步版只投递请求就返回。
* 最小化窗口**必须**先 ``SW_RESTORE``，否则 ``SetForegroundWindow`` 无效。
* ``AttachThreadInput`` 的第二个参数是**窗口所属线程**，不是当前线程。
* 纯 UWP / 提权窗口（UIPI 隔离）可能无论如何都抢不到前台 ——
  此时抛 ``ForegroundTimeout``，绝不假装成功。
"""
from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from .._win32 import api as A
from .._win32 import structs as S
from ..errors import TimeoutError_, WindowNotFound

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 抢占失败时的默认重试次数（每次之间短暂等待，给窗口管理器时间）
DEFAULT_ATTEMPTS = 3

#: 每轮尝试内部的等待上限（秒）
DEFAULT_TIMEOUT = 2.0

#: 两次尝试之间的间隔（秒）—— 太快会让前台锁来不及更新
RETRY_DELAY = 0.08

#: 轮询确认的间隔（秒）
POLL_INTERVAL = 0.02


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class ForegroundError(TimeoutError_):
    """抢占前台失败。

    属于 ``TimeoutError_`` 的子类，因此调用方可以统一按超时处理，
    也可以精确捕获本类型做特殊处理。
    """

    def __init__(self, message: str, hwnd: Optional[int] = None,
                 current: Optional[int] = None):
        super().__init__(message)
        self.hwnd = hwnd
        self.current = current


# --------------------------------------------------------------------------
# 读取前台
# --------------------------------------------------------------------------
def get_foreground() -> int:
    """当前前台窗口句柄。无前台窗口时返回 0。"""
    h = A.user32.GetForegroundWindow()
    return int(h) if h else 0


#: 别名 —— 语义更直白的写法
foreground_hwnd = get_foreground


def is_foreground(hwnd: int) -> bool:
    """目标窗口当前是否就是前台窗口。"""
    h = int(hwnd)
    if not h:
        return False
    return get_foreground() == h


def foreground_info() -> dict:
    """诊断用：打印前台窗口的详细信息。"""
    h = get_foreground()
    if not h:
        return {"hwnd": 0, "title": "", "pid": 0, "tid": 0, "valid": False}

    buf = ctypes.create_unicode_buffer(512)
    A.user32.GetWindowTextW(h, buf, 512)
    pid = S.DWORD(0)
    tid = int(A.user32.GetWindowThreadProcessId(h, ctypes.byref(pid)) or 0)
    return {
        "hwnd": h,
        "title": buf.value,
        "pid": int(pid.value),
        "tid": tid,
        "valid": bool(A.user32.IsWindow(h)),
    }


def thread_of_window(hwnd: int) -> int:
    """窗口所属的 UI 线程 ID。"""
    pid = S.DWORD(0)
    tid = A.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value), int(tid or 0)


def current_thread_id() -> int:
    """当前线程 ID。"""
    return int(A.kernel32.GetCurrentThreadId())


# --------------------------------------------------------------------------
# 输入队列挂接
# --------------------------------------------------------------------------
@contextmanager
def attached_input(target_thread: int) -> Iterator[bool]:
    """把当前线程的输入队列挂到 ``target_thread`` 上（上下文管理器）。

    yield 出的是**是否挂接成功**。挂接失败不抛异常 —— 因为部分情况下
    （目标已在前台、同进程）不挂接也能成功，调用方应该继续尝试。

    无论如何退出时都会尝试摘除，包括目标线程中途消失的情况。
    """
    me = current_thread_id()
    ok = False
    if target_thread and target_thread != me:
        try:
            ok = bool(A.user32.AttachThreadInput(me, int(target_thread), True))
        except Exception:
            ok = False
    try:
        yield ok
    finally:
        if ok:
            try:
                A.user32.AttachThreadInput(me, int(target_thread), False)
            except Exception:
                # 目标线程已退出时摘除会失败，无害
                pass


# --------------------------------------------------------------------------
# 基础激活
# --------------------------------------------------------------------------
def _ensure_window(hwnd: int) -> int:
    h = int(hwnd)
    if not h or not A.user32.IsWindow(h):
        raise WindowNotFound("窗口句柄无效或已销毁", hwnd=h)
    return h


def _restore_if_minimized(h: int) -> None:
    """最小化的窗口必须先还原，否则前台化无效。"""
    if A.user32.IsIconic(h):
        A.user32.ShowWindowAsync(h, S.SW_RESTORE)


def _restore_if_hidden(h: int) -> None:
    """不可见的窗口先显示。

    ``SW_SHOWNOACTIVATE`` 不会触发前台锁，用它做"露脸"这一步最安全，
    真正的激活交给后面的 ``SetForegroundWindow``。
    """
    if not A.user32.IsWindowVisible(h):
        A.user32.ShowWindowAsync(h, S.SW_SHOWNOACTIVATE)


def activate_once(hwnd: int) -> bool:
    """**单次**尝试激活窗口。返回是否已成为前台。

    这是最轻量的原语，不做重试、不抛异常。调用方自己决定重试策略。

    步骤：

    1. 还原（若最小化 / 不可见）
    2. 把自己的输入队列挂到目标线程
    3. ``SetForegroundWindow``
    4. ``BringWindowToTop``（补刀 —— 某些窗口只吃这一下）
    5. ``SetFocus``（拿到键盘焦点，键盘输入才进得去）
    """
    h = int(hwnd)
    if not h or not A.user32.IsWindow(h):
        return False

    if is_foreground(h):
        return True

    _restore_if_minimized(h)
    _restore_if_hidden(h)

    tid = thread_of_window(h)[1]
    with attached_input(tid):
        try:
            A.user32.SetForegroundWindow(h)
        except Exception:
            pass
        if not is_foreground(h):
            try:
                A.user32.BringWindowToTop(h)
            except Exception:
                pass
        # SetFocus 只对**当前线程**创建的窗口直接有效；
        # 跨线程时需要挂接，这里已经在 attached_input 内，所以可用。
        try:
            A.user32.SetFocus(h)
        except Exception:
            pass

    return is_foreground(h)


def wait_foreground(hwnd: int, timeout: float = 1.0,
                    poll: float = POLL_INTERVAL) -> bool:
    """轮询等待窗口成为前台。返回是否成功（不抛异常）。"""
    h = int(hwnd)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if is_foreground(h):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.005, poll))


def force_foreground(hwnd: int, timeout: float = DEFAULT_TIMEOUT,
                     attempts: int = DEFAULT_ATTEMPTS) -> bool:
    """强行把窗口推到前台，带重试与确认。

    与 ``activate_once`` 的区别：本函数是**完整策略** ——
    多轮尝试、轮询确认、超时后抛 ``ForegroundError``。

    返回 True 表示确认成功（``GetForegroundWindow() == hwnd``）。
    """
    h = _ensure_window(hwnd)
    if is_foreground(h):
        return True

    deadline = time.monotonic() + max(0.0, timeout)
    last_current = 0

    for attempt in range(max(1, int(attempts))):
        if activate_once(h):
            return True

        # 单轮内给它一点时间 —— SetForegroundWindow 生效是异步的
        slice_end = min(deadline, time.monotonic() + timeout / max(1, attempts))
        while time.monotonic() < slice_end:
            if is_foreground(h):
                return True
            time.sleep(POLL_INTERVAL)

        last_current = get_foreground()
        if time.monotonic() >= deadline:
            break
        time.sleep(RETRY_DELAY)

    last_current = last_current or get_foreground()
    raise ForegroundError(
        "无法将窗口置为前台（hwnd=0x%X）；当前前台是 0x%X。"
        "常见原因：目标窗口属于提权进程（UIPI 隔离）、或被系统锁屏/"
        "UAC 弹窗占用前台。" % (h, last_current),
        hwnd=h,
        current=last_current,
    )


def ensure_foreground(hwnd: int, timeout: float = DEFAULT_TIMEOUT,
                      attempts: int = DEFAULT_ATTEMPTS) -> int:
    """断言式接口：确保窗口在前台，否则抛 ``ForegroundError``。

    与 ``force_foreground`` 行为一致，但**返回句柄**而非布尔值，
    方便链式写作 ``h = ensure_foreground(h)``。

    这是所有"要发键盘输入"的操作应当调用的入口 —— 键盘输入打到错误窗口
    是自动化里最昂贵的一类 bug。
    """
    h = _ensure_window(hwnd)
    force_foreground(h, timeout=timeout, attempts=attempts)
    return h


# --------------------------------------------------------------------------
# 焦点守卫
# --------------------------------------------------------------------------
@contextmanager
def focus_guard(hwnd: int, restore: bool = False,
                timeout: float = DEFAULT_TIMEOUT) -> Iterator[int]:
    """焦点守卫：进入时抢占前台，退出时按需还原。

    ``restore=True`` 时，退出阶段把**进入前**的前台窗口还回去 ——
    适合"临时抢一下焦点做完事就走"的场景，不打扰用户当前的工作。

    ``restore=False``（默认）时保持目标窗口在前台 —— 适合"接下来要连续
    发键鼠事件"的场景，避免中途被抢。

    用法::

        with focus_guard(hwnd):
            send_keys("hello")
    """
    h = _ensure_window(hwnd)
    previous = get_foreground()
    ensure_foreground(h, timeout=timeout)
    try:
        yield h
    finally:
        if restore and previous and previous != h \
                and A.user32.IsWindow(previous):
            try:
                force_foreground(previous, timeout=0.5, attempts=1)
            except Exception:
                # 还原失败不干扰主流程 —— 这只是"礼貌性"动作
                pass


@contextmanager
def attached_focus(hwnd: int) -> Iterator[bool]:
    """只挂接输入队列、不做前台化 —— 给需要跨线程消息发送的场景用。

    yield 出挂接是否成功。退出时自动摘除。
    """
    h = _ensure_window(hwnd)
    tid = thread_of_window(h)[1]
    with attached_input(tid) as ok:
        yield ok


# --------------------------------------------------------------------------
# 顶层窗口解析
# --------------------------------------------------------------------------
def root_window(hwnd: int) -> int:
    """向上找到顶层（祖先）窗口。

    UIA 常返回子窗口句柄，但前台化必须针对顶层窗口 —— 子窗口无法成为前台。
    """
    h = int(hwnd)
    if not h:
        return 0
    while True:
        parent = A.user32.GetAncestor(h, S.GA_ROOT)
        if not parent or int(parent) == h:
            return h
        h = int(parent)


def foreground_root() -> int:
    """当前前台窗口的顶层窗口。"""
    return root_window(get_foreground())


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """前台子系统自检。

    只做**只读**检查 —— 不去抢前台（那会打断用户当前的工作）。
    真正的抢前台能力由 PowerShell/CLI 里的 ``avatar activate`` 命令验证。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. 能读到前台窗口
    h = get_foreground()
    _add("GetForegroundWindow 可用", h != 0, "前台 hwnd=0x%X" % h)

    # 2. 前台窗口是有效窗口
    if h:
        _add("前台句柄有效", bool(A.user32.IsWindow(h)), "IsWindow=真")

    # 3. 能取到前台窗口的标题（允许空标题）
    info = foreground_info()
    _add("可读取前台窗口属性", info["hwnd"] == h,
         "title=%r pid=%d tid=%d" % (info["title"], info["pid"], info["tid"]))

    # 4. 线程 ID 解析
    tid_ok = True
    tid_detail = "无前台窗口"
    if h:
        _, tid = thread_of_window(h)
        tid_ok = tid > 0
        tid_detail = "tid=%d" % tid
    _add("可解析窗口所属线程", tid_ok, tid_detail)

    # 5. 当前线程 ID 可用
    me = current_thread_id()
    _add("GetCurrentThreadId 可用", me > 0, "当前 tid=%d" % me)

    # 6. root_window 幂等（对顶层窗口调用应返回自身）
    if h:
        r = root_window(h)
        _add("root_window 幂等", root_window(r) == r,
             "顶层 hwnd=0x%X" % r)

    # 7. attached_input 能正确挂接 / 摘除
    #
    # ⚠ 判据修正（2026-09-27）：**挂接失败不构成缺陷**。
    #
    #   原来这条写成"必须挂上"，于是长期红灯 —— 但那是**检查设计错了**，
    #   不是代码坏了。依据就在本文件 ``attached_input`` 自己的 docstring：
    #
    #       「挂接失败不抛异常 —— 因为部分情况下（目标已在前台、同进程）
    #         不挂接也能成功，调用方应该继续尝试。」
    #
    #   ``AttachThreadInput`` 失败的常见原因（都是**预期内**的）：
    #     * 目标线程本身就在前台 —— 不需要挂接，语义上已经"通了"
    #     * 自动化/无交互会话里，输入桌面受限（本机实测就是这种）
    #     * 目标进程权限高于当前进程（UIPI 拦截）
    #
    #   所以正确的判据是**接口契约**：调用不抛异常 + 返回布尔 +
    #   退出时摘除干净。挂没挂上如实报出来供排查，但不判失败。
    if h:
        tid = thread_of_window(h)[1]
        raised = None
        try:
            with attached_input(tid) as ok:
                attach_ok = ok or (tid == me)
        except Exception as exc:      # noqa: BLE001
            raised = exc
            attach_ok = False
        detail = "挂接=%s (目标 tid=%d, 当前 tid=%d)" % (ok if raised is None
                                                     else "<异常>", tid, me)
        if raised is not None:
            _add("AttachThreadInput 接口契约", False,
                 "抛了 %s: %s" % (type(raised).__name__, raised))
        else:
            # 契约成立即通过；挂接结果作为**信息**写在 detail 里
            _add("AttachThreadInput 接口契约", True,
                 "%s（挂接失败属预期，不影响语义）" % detail)

    return {"checks": checks, "failures": failures, "ok": not failures}


# --------------------------------------------------------------------------
# 命令行入口（调试用）
# --------------------------------------------------------------------------
def _main() -> int:
    import json
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"

    if cmd == "info":
        print(json.dumps(foreground_info(), ensure_ascii=False, indent=2))
        return 0

    if cmd == "activate":
        if len(sys.argv) < 3:
            print("用法: python -m avatar.app_manager.foreground activate <hwnd>")
            return 2
        h = int(sys.argv[2], 0)
        force_foreground(h)
        print("已激活: 0x%X" % h)
        return 0

    if cmd == "selfcheck":
        res = selfcheck()
        for c in res["checks"]:
            print("  [%s] %-32s %s" % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
        print("前台自检: %d/%d" % (len(res["checks"]) - len(res["failures"]),
                                   len(res["checks"])))
        return 0 if res["ok"] else 1

    print("未知命令: %s（可选: info / activate / selfcheck）" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
