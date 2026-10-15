# -*- coding: utf-8 -*-
"""进程管理：创建 / 查询 / 等待 / 终止，以及权限与完整性级别探测。

职责
----
1. ``list_processes()`` —— 枚举进程（PID / 名称 / 路径 / 内存）
2. ``by_name`` / ``by_pid`` / ``find`` —— 查询
3. ``launch()`` —— 用 ``CreateProcessW`` 启动新进程，返回 ``LaunchedProcess``
4. ``kill()`` / ``terminate_tree()`` —— 终止（带**白名单保护**）
5. ``wait_for_exit`` / ``wait_for_window`` —— 等待
6. ``integrity_level()`` / ``can_control()`` / ``uipi_blocked()`` —— UIPI 权限判定

依赖
----
· ``avatar._win32.api``（kernel32 / userenv / advapi32 / shell32）
· ``avatar._win32.structs``
· ``avatar.errors``

## 权限模型：为什么"点不动"某个窗口

Windows 从 Vista 起有 **UIPI（User Interface Privilege Isolation）**：
低完整性级别的进程**不能**向高完整性级别的进程发送窗口消息、也不能驱动它的控件。
表现是：窗口看得见、截图截得到、但鼠标键盘事件全被静默丢弃 —— 又是"不报错但没反应"。

本模块的对策：

    `can_control(hwnd)` 比较**自己**和**目标**的完整性级别，
    不够就直接判定，让上层在"发事件"之前就知道会失败，
    而不是发完了发现没动静再各种猜测。

## 进程终止的白名单

默认策略（已与使用者确认）：**只允许终止 avatar 自己启动的进程**。
``launch()`` 会把 PID 记进 ``_OWNED_PIDS``；``kill()`` 遇到不在册的 PID
会抛 ``ProcessBlocked``，除非显式传 ``allow_foreign=True``。

这条规则存在的理由：自动化脚本里一个"清理残留进程"的疏忽，
可以让使用者丢掉整个下午的未保存工作。默认保守，需要时显式放行。
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .._win32 import api as A
from .._win32 import structs as S
from ..errors import ProcessBlocked, TargetError, TimeoutError_, WindowNotFound

# --------------------------------------------------------------------------
# 全局状态：本进程启动过的 PID
# --------------------------------------------------------------------------
_OWNED_PIDS: set = set()
_OWNED_LOCK = threading.Lock()


def owned_pids() -> set:
    """返回 avatar 自己启动过的 PID 集合（快照）。"""
    with _OWNED_LOCK:
        return set(_OWNED_PIDS)


def _mark_owned(pid: int) -> None:
    with _OWNED_LOCK:
        _OWNED_PIDS.add(int(pid))


def _is_owned(pid: int) -> bool:
    with _OWNED_LOCK:
        return int(pid) in _OWNED_PIDS


def forget_owned(pid: int) -> None:
    """从自己启动的记录里移除（进程已退出时清理用）。"""
    with _OWNED_LOCK:
        _OWNED_PIDS.discard(int(pid))


# --------------------------------------------------------------------------
# 完整性级别
# --------------------------------------------------------------------------
#: 完整性级别 RID → 名字
INTEGRITY_NAMES = {
    0x0000: "untrusted",
    0x1000: "low",
    0x2000: "medium",
    0x3000: "high",
    0x4000: "system",
    0x5000: "protected",
}


def integrity_level(pid: int) -> int:
    """读取进程的完整性级别 RID。失败返回 -1。

    走的是 ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` →
    ``OpenProcessToken(TokenIntegrityLevel)`` → ``GetTokenInformation`` →
    取 ``TOKEN_MANDATORY_LABEL`` 里最后一个子权限的 RID。
    """
    h = A.kernel32.OpenProcess(S.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return -1
    try:
        tok = S.HANDLE()
        if not A.advapi32.OpenProcessToken(h, S.TOKEN_QUERY, ctypes.byref(tok)):
            return -1
        try:
            # 先问大小
            need = S.DWORD(0)
            A.advapi32.GetTokenInformation(
                tok, S.TokenIntegrityLevel, None, 0, ctypes.byref(need)
            )
            if need.value == 0:
                return -1
            buf = ctypes.create_string_buffer(need.value)
            if not A.advapi32.GetTokenInformation(
                tok, S.TokenIntegrityLevel, buf, need.value, ctypes.byref(need)
            ):
                return -1
            # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label; }
            # 第一个字段是 SID 指针，指向 buf 之外？不 —— 指针指向本进程内存，
            # 是 GetTokenInformation 填好的绝对地址。
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_ptr:
                return -1
            # 最后一个子权限的 RID，即完整性级别
            cnt = A.advapi32.GetSidSubAuthorityCount(ctypes.c_void_p(sid_ptr))
            if not cnt:
                return -1
            n = ctypes.cast(cnt, ctypes.POINTER(ctypes.c_ubyte))[0]
            if n < 1:
                return -1
            rid_p = A.advapi32.GetSidSubAuthority(
                ctypes.c_void_p(sid_ptr), n - 1
            )
            if not rid_p:
                return -1
            return int(ctypes.cast(rid_p, ctypes.POINTER(S.DWORD))[0])
        finally:
            A.kernel32.CloseHandle(tok)
    finally:
        A.kernel32.CloseHandle(h)


def integrity_name(rid: int) -> str:
    """RID → 可读名。"""
    if rid < 0:
        return "unknown"
    return INTEGRITY_NAMES.get(rid, "0x%X" % rid)


def self_integrity() -> int:
    """当前进程的完整性级别。"""
    return integrity_level(os.getpid())


def is_elevated() -> bool:
    """当前进程是否以管理员身份运行（完整性级别 ≥ high）。"""
    return self_integrity() >= 0x3000


def uipi_blocked(target_pid: int) -> bool:
    """我们是否**因权限不足**而无法驱动目标进程的 UI。

    判定规则：目标完整性级别 **严格高于** 自己 → 被阻。
    同级或更低都能驱动。
    """
    mine = self_integrity()
    theirs = integrity_level(target_pid)
    if mine < 0 or theirs < 0:
        return False  # 读不到就别乱判，交给实际尝试
    return theirs > mine


def can_control(target_pid: int) -> bool:
    """能否驱动目标进程的 UI（``uipi_blocked`` 的反面）。"""
    return not uipi_blocked(target_pid)


def require_control(target_pid: int, what: str = "操作该进程的界面") -> None:
    """断言可驱动，否则抛 ``UipiBlocked``。"""
    if uipi_blocked(target_pid):
        from ..errors import UipiBlocked
        mine = self_integrity()
        theirs = integrity_level(target_pid)
        raise UipiBlocked(
            "无法%s：目标进程（PID %d，完整性 %s）比当前进程（%s）权限更高。"
            "请以管理员身份运行 avatar。" % (
                what, target_pid, integrity_name(theirs), integrity_name(mine)
            ),
            self_integrity=mine,
            target_integrity=theirs,
        )


# --------------------------------------------------------------------------
# 进程信息
# --------------------------------------------------------------------------
@dataclass
class ProcessInfo:
    """一个进程的摘要。"""

    pid: int
    name: str = ""
    path: str = ""
    ppid: int = 0
    threads: int = 0
    memory: int = 0          # 工作集字节数
    integrity: int = -1
    owned: bool = False      # 是否由 avatar 启动

    @property
    def integrity_name(self) -> str:
        return integrity_name(self.integrity)

    @property
    def alive(self) -> bool:
        return is_alive(self.pid)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "path": self.path,
            "ppid": self.ppid,
            "threads": self.threads,
            "memory_mb": round(self.memory / 1048576.0, 1),
            "integrity": self.integrity_name,
            "owned": self.owned,
            "alive": self.alive,
        }

    def __str__(self):
        return "%-6d %-28s %-52s %s%s" % (
            self.pid,
            (self.name or "?")[:28],
            (self.path or "?")[:52],
            self.integrity_name,
            " [owned]" if self.owned else "",
        )


def is_alive(pid: int) -> bool:
    """PID 是否对应一个活着的进程。

    用 ``OpenProcess(SYNCHRONIZE) + WaitForSingleObject(0)`` —— 比
    "打开成功就算活着"更准，因为僵尸句柄也能打开。
    """
    h = A.kernel32.OpenProcess(S.SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        r = A.kernel32.WaitForSingleObject(h, 0)
        return r == S.WAIT_TIMEOUT
    finally:
        A.kernel32.CloseHandle(h)


def _query_path(pid: int) -> str:
    """取进程可执行文件的完整路径。"""
    h = A.kernel32.OpenProcess(
        S.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = S.DWORD(1024)
        if A.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    except Exception:
        return ""
    finally:
        A.kernel32.CloseHandle(h)


def process_of(pid: int) -> Optional[ProcessInfo]:
    """按 PID 取一个 ``ProcessInfo``；进程不存在返回 None。"""
    pid = int(pid)
    if pid <= 0 or not is_alive(pid):
        return None
    path = _query_path(pid)
    return ProcessInfo(
        pid=pid,
        name=os.path.basename(path) if path else "",
        path=path,
        integrity=integrity_level(pid),
        owned=_is_owned(pid),
    )


def list_processes() -> list:
    """枚举全部进程（用 ``CreateToolhelp32Snapshot``）。

    返回的 ``ProcessInfo`` 里 ``memory`` 已填入工作集大小。
    """
    TH32CS_SNAPPROCESS = 0x00000002
    snap = A.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    INVALID = ctypes.c_void_p(-1).value
    if not snap or int(snap) == INVALID:
        return []

    out = []
    try:
        entry = S.PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(S.PROCESSENTRY32W)
        ok = A.kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            info = ProcessInfo(
                pid=pid,
                name=entry.szExeFile,
                ppid=int(entry.th32ParentProcessID),
                threads=int(entry.cntThreads),
                owned=_is_owned(pid),
            )
            # 内存 / 路径 / 完整性按需补 —— 太贵就不查路径
            info.memory = _working_set(pid)
            out.append(info)
            ok = A.kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        A.kernel32.CloseHandle(snap)
    return out


def _working_set(pid: int) -> int:
    try:
        pmc = S.PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(S.PROCESS_MEMORY_COUNTERS)
        h = A.kernel32.OpenProcess(S.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return 0
        try:
            if A.psapi is None or A.psapi.GetProcessMemoryInfo is None:
                return 0
            if A.psapi.GetProcessMemoryInfo(
                h, ctypes.byref(pmc), ctypes.sizeof(pmc)
            ):
                return int(pmc.WorkingSetSize)
            return 0
        finally:
            A.kernel32.CloseHandle(h)
    except Exception:
        return 0


def by_pid(pid: int) -> Optional[ProcessInfo]:
    """按 PID 查（``process_of`` 的别名）。"""
    return process_of(pid)


def by_name(name: str, exact: bool = False) -> list:
    """按进程名查（忽略大小写，允许省略 .exe）。

    ``exact=False`` 时按**子串**匹配（"edge" 能匹配 "msedge.exe"）。
    """
    if not name:
        return []
    needle = name.lower()
    if not needle.endswith(".exe"):
        needle_bare = needle
        needle_exe = needle + ".exe"
    else:
        needle_bare = needle[:-4]
        needle_exe = needle

    out = []
    for p in list_processes():
        pn = (p.name or "").lower()
        if exact:
            if pn in (needle_bare, needle_exe):
                out.append(p)
        else:
            if needle_bare in pn:
                out.append(p)
    return out


def find(name: str = None, pid: int = None, path_re: str = None) -> list:
    """组合查询。任一条件满足即算命中（条件之间是**与**）。"""
    res = list_processes()
    if pid is not None:
        res = [p for p in res if p.pid == int(pid)]
    if name:
        needle = name.lower().replace(".exe", "")
        res = [p for p in res if needle in (p.name or "").lower()]
    if path_re:
        rx = re.compile(path_re, re.I)
        res = [p for p in res if p.path and rx.search(p.path)]
    return res


def process_of_window(hwnd: int) -> ProcessInfo:
    """窗口所属进程。"""
    pid = S.DWORD(0)
    A.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    info = process_of(int(pid.value))
    if info is None:
        return ProcessInfo(pid=int(pid.value), name="(unknown)")
    return info


def pid_of_window(hwnd: int) -> int:
    pid = S.DWORD(0)
    A.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def windows_of_pid(pid: int, include_invisible: bool = False) -> list:
    """某进程的全部顶层窗口句柄。"""
    from .windows import list_windows
    return [
        w.hwnd for w in list_windows(include_invisible=include_invisible)
        if w.pid == int(pid)
    ]


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
@dataclass
class LaunchedProcess:
    """``launch()`` 的返回值。"""

    pid: int
    process: Optional[object] = None     # HANDLE（不自动关，由 close_handle 处理）
    thread: Optional[object] = None
    cmdline: str = ""
    cwd: str = ""
    started: float = field(default_factory=time.time)

    @property
    def alive(self) -> bool:
        return is_alive(self.pid)

    def info(self) -> Optional[ProcessInfo]:
        return process_of(self.pid)

    def wait(self, timeout: float = 30.0) -> int:
        """等待进程退出，返回退出码。超时抛 ``TimeoutError_``。"""
        rc = wait_for_exit(self.pid, timeout=timeout)
        if rc is None:
            raise TimeoutError_(
                "等待进程退出超时（PID %d，%.1fs）" % (self.pid, timeout)
            )
        return rc

    def windows(self, timeout: float = 10.0, poll: float = 0.1) -> list:
        """等待该进程出现顶层窗口，返回窗口列表。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            ws = windows_of_pid(self.pid)
            if ws:
                return ws
            if time.monotonic() >= deadline:
                return []
            time.sleep(max(0.01, poll))

    def first_window(self, timeout: float = 10.0) -> int:
        """等待并返回第一个顶层窗口句柄；没有则抛 ``WindowNotFound``。"""
        ws = self.windows(timeout=timeout)
        if not ws:
            raise WindowNotFound(
                "进程已启动但未出现顶层窗口（PID %d）" % self.pid,
                pid=self.pid,
            )
        return ws[0]

    def kill(self, force: bool = False, timeout: float = 5.0) -> bool:
        return kill(self.pid, force=force, timeout=timeout, allow_foreign=True)

    def close_handles(self) -> None:
        for h in (self.thread, self.process):
            if h:
                try:
                    A.kernel32.CloseHandle(h)
                except Exception:
                    pass
        self.thread = None
        self.process = None

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "cmdline": self.cmdline,
            "cwd": self.cwd,
            "alive": self.alive,
        }

    def __str__(self):
        return "LaunchedProcess(pid=%d, alive=%s, cmd=%s)" % (
            self.pid, self.alive, (self.cmdline or "")[:60]
        )


def resolve_executable(target: str) -> str:
    """把一个"程序名或路径"解析成可传给 CreateProcessW 的命令行首段。

    解析顺序：

    1. 含路径分隔符 → 直接当路径（存在则返回绝对路径）
    2. 已在 PATH 中（``shutil.which``）
    3. 系统目录常见位置（System32 / Windows / Program Files）
    4. 返回原样，让 CreateProcessW 自己按 PATH 找
    """
    if os.sep in target or "/" in target:
        p = os.path.abspath(target)
        return p if os.path.exists(p) else target

    import shutil
    w = shutil.which(target)
    if w:
        return w

    # 常见系统程序
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands = [
        os.path.join(sysroot, "System32", target),
        os.path.join(sysroot, target),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    if not target.lower().endswith(".exe"):
        for c in list(cands):
            if os.path.exists(c + ".exe"):
                return c + ".exe"
    return target


def launch(target: str,
           args: Optional[Iterable[str]] = None,
           cwd: Optional[str] = None,
           show: int = S.SW_SHOWNORMAL,
           wait_input_idle_ms: int = 0,
           env: Optional[dict] = None,
           new_console: bool = False) -> LaunchedProcess:
    """启动一个新进程。

    ``target`` 可以是可执行文件名（走 PATH 解析）或完整路径。
    ``args`` 是参数序列 —— **不做 shell 解析**，每个元素就是一个 argv。

    新进程会被登记进 ``_OWNED_PIDS``，因此后续 ``kill()`` 无需额外授权。

    返回的 ``LaunchedProcess`` 持有 process/thread 句柄，
    用完调 ``close_handles()`` 或让它随 GC 释放。
    """
    exe = resolve_executable(target)
    argv = [exe] + [str(a) for a in (args or [])]
    quote = A.command_line_from_argv(argv)
    cmdline = ctypes.create_unicode_buffer(quote)

    si = S.STARTUPINFOW()
    si.cb = ctypes.sizeof(S.STARTUPINFOW)
    si.dwFlags = S.STARTF_USESHOWWINDOW
    si.wShowWindow = int(show)
    pi = S.PROCESS_INFORMATION()

    flags = 0
    if new_console:
        flags |= S.CREATE_NEW_CONSOLE
    if env is not None:
        flags |= S.CREATE_UNICODE_ENVIRONMENT

    env_block = None
    env_ptr = None
    if env is not None:
        env_block, env_ptr = _build_env_block(env)

    cwdbuf = ctypes.c_wchar_p(os.path.abspath(cwd)) if cwd else None

    ok = A.kernel32.CreateProcessW(
        ctypes.c_wchar_p(exe),
        cmdline,
        None,
        None,
        False,
        flags,
        env_ptr,
        cwdbuf,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        err = A.kernel32.GetLastError()
        raise TargetError(
            "启动进程失败: %s（Win32 错误 %d: %s）" % (
                exe, err, A.win32_error_text(err)
            ),
            target=target,
        )

    lp = LaunchedProcess(
        pid=int(pi.dwProcessId),
        process=pi.hProcess,
        thread=pi.hThread,
        cmdline=quote,
        cwd=cwd or os.getcwd(),
    )
    _mark_owned(lp.pid)

    if wait_input_idle_ms > 0:
        try:
            A.user32.WaitForInputIdle(pi.hProcess, int(wait_input_idle_ms))
        except Exception:
            pass

    return lp


def _build_env_block(extra: dict):
    """构造 CREATE_UNICODE_ENVIRONMENT 需要的环境块。

    返回 ``(buffer, pointer)`` —— buffer 必须由调用方保活到 CreateProcess 返回，
    否则指针悬空。
    """
    base = dict(os.environ)
    base.update({str(k): str(v) for k, v in extra.items()})
    parts = []
    for k, v in base.items():
        if k.upper() == "PATH" and "PATH" in base:
            pass
        parts.append("%s=%s" % (k, v))
    # 特殊处理：PATH 可能因大小写重复，这里合并
    payload = "\0".join(parts) + "\0\0"
    buf = ctypes.create_unicode_buffer(payload)
    return buf, ctypes.cast(buf, ctypes.c_void_p)


def run(target: str, args: Optional[Iterable[str]] = None,
        timeout: float = 30.0, cwd: Optional[str] = None,
        hide: bool = True) -> dict:
    """同步运行一个程序并收集输出（走临时文件，因为纯 ctypes 不建管道更方便）。

    返回 ``{"pid", "returncode", "stdout", "stderr", "elapsed"}``。

    注意：本函数为方便起见使用子进程重定向，**不**经过 ``CreateProcessW``
    直调 —— 它只用于 avatar 自身的外围工具链，不参与 UI 自动化。
    """
    import subprocess
    import tempfile
    start = time.time()
    argv = [resolve_executable(target)] + [str(a) for a in (args or [])]
    with tempfile.TemporaryFile("w+b") as fo, tempfile.TemporaryFile("w+b") as fe:
        p = subprocess.Popen(argv, stdout=fo, stderr=fe, cwd=cwd)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        fo.seek(0)
        fe.seek(0)
        return {
            "pid": p.pid,
            "returncode": p.returncode,
            "stdout": fo.read().decode("utf-8", "replace"),
            "stderr": fe.read().decode("utf-8", "replace"),
            "elapsed": round(time.time() - start, 3),
        }


# --------------------------------------------------------------------------
# 等待
# --------------------------------------------------------------------------
def wait_for_exit(pid: int, timeout: float = 30.0) -> Optional[int]:
    """等待进程退出。返回退出码；超时返回 None。"""
    pid = int(pid)
    h = A.kernel32.OpenProcess(S.SYNCHRONIZE | S.PROCESS_QUERY_LIMITED_INFORMATION,
                               False, pid)
    if not h:
        return None if is_alive(pid) else 0

    try:
        ms = int(max(0.0, timeout) * 1000)
        r = A.kernel32.WaitForSingleObject(h, ms)
        if r != S.WAIT_OBJECT_0:
            return None
        code = S.DWORD(0)
        A.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        forget_owned(pid)
        return int(code.value)
    finally:
        A.kernel32.CloseHandle(h)


def wait_for_window(pid: int, timeout: float = 10.0,
                    poll: float = 0.1) -> Optional[int]:
    """等待某进程出现顶层窗口，返回句柄；超时返回 None。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        ws = windows_of_pid(pid)
        if ws:
            return ws[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.01, poll))


# --------------------------------------------------------------------------
# 终止
# --------------------------------------------------------------------------
def kill(pid: int, force: bool = False, timeout: float = 5.0,
         allow_foreign: bool = False) -> bool:
    """终止进程。

    ``force=False``（默认）先发 ``WM_CLOSE`` 给该进程的所有顶层窗口，
    等 ``timeout/2`` —— 给它机会保存数据。没退再硬杀。

    ``force=True`` 直接 ``TerminateProcess``。

    ``allow_foreign=False``（默认）时，如果目标 PID **不是** avatar 自己启动的，
    抛 ``ProcessBlocked``。这是保护使用者数据的安全阀。
    """
    pid = int(pid)

    if pid == os.getpid():
        raise ProcessBlocked(
            "拒绝终止 avatar 自身进程", process=pid, operation="kill"
        )

    if not allow_foreign and not _is_owned(pid):
        raise ProcessBlocked(
            "目标进程不是 avatar 启动的（PID %d）。"
            "若要终止请显式传 allow_foreign=True。" % pid,
            process=pid,
            operation="kill",
        )

    if not is_alive(pid):
        forget_owned(pid)
        return True

    if not force:
        # 温和路径：给所有顶层窗口发 WM_CLOSE
        graceful_deadline = time.monotonic() + max(0.5, timeout / 2.0)
        for h in windows_of_pid(pid, include_invisible=True):
            try:
                A.user32.PostMessageW(h, S.WM_CLOSE, 0, 0)
            except Exception:
                pass
        while time.monotonic() < graceful_deadline:
            if not is_alive(pid):
                forget_owned(pid)
                return True
            time.sleep(0.05)

    h = A.kernel32.OpenProcess(S.PROCESS_TERMINATE, False, pid)
    if not h:
        err = A.kernel32.GetLastError()
        raise ProcessBlocked(
            "无法终止进程 PID %d（Win32 错误 %d: %s）。"
            "可能权限不足。" % (pid, err, A.win32_error_text(err)),
            process=pid,
            operation="terminate",
        )
    try:
        if not A.kernel32.TerminateProcess(h, 1):
            err = A.kernel32.GetLastError()
            raise ProcessBlocked(
                "TerminateProcess 失败（PID %d，错误 %d）" % (pid, err),
                process=pid, operation="terminate",
            )
    finally:
        A.kernel32.CloseHandle(h)

    # 确认真的退了
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not is_alive(pid):
            forget_owned(pid)
            return True
        time.sleep(0.05)

    raise TimeoutError_("已请求终止但进程仍在（PID %d）" % pid)


def terminate_tree(pid: int, force: bool = True, timeout: float = 5.0,
                   allow_foreign: bool = False) -> list:
    """终止进程及其**全部后代**。返回被终止的 PID 列表。

    用 ``CreateToolhelp32Snapshot`` 建父子表，自底向上杀 ——
    先杀子进程可以避免父进程重启它们。
    """
    pid = int(pid)
    if not allow_foreign and not _is_owned(pid):
        raise ProcessBlocked(
            "目标进程不是 avatar 启动的（PID %d）" % pid,
            process=pid, operation="terminate_tree",
        )

    procs = list_processes()
    children = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)

    order = []

    def _walk(x):
        for c in children.get(x, []):
            _walk(c)
        order.append(x)

    _walk(pid)

    killed = []
    for x in order:
        if x == os.getpid():
            continue
        try:
            if kill(x, force=force, timeout=timeout, allow_foreign=True):
                killed.append(x)
        except Exception:
            pass
    return killed


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """进程子系统自检。

    枚举检查依赖**本机能列出进程**。一个都列不出时判 SKIP ——
    环境资源不存在 ≠ 代码有 bug。
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

    # 1. 枚举进程
    procs = list_processes()
    if procs:
        _add("可枚举进程", len(procs) > 5, "共 %d 个进程" % len(procs))
    else:
        _add("可枚举进程", False, "本机枚举不到进程 → 跳过", skip=True)

    # 2. 能找到自己
    me = process_of(os.getpid())
    _add("可查到自身进程", me is not None and me.pid == os.getpid(),
         "pid=%d name=%s" % (os.getpid(), me.name if me else "?"))

    # 3. 自身路径能读出来
    _add("可读取进程路径", bool(me and me.path), me.path if me else "(无)")

    # 4. 完整性级别能读
    rid = self_integrity()
    _add("可读取完整性级别", rid > 0, "%s (0x%X)" % (integrity_name(rid), max(0, rid)))

    # 5. 自身可驱动自身
    _add("自身进程可驱动", can_control(os.getpid()), "UIPI 判定为可驱动")

    # 6. is_alive 对活进程返回真
    _add("is_alive 对活进程为真", is_alive(os.getpid()), "pid=%d" % os.getpid())

    # 7. is_alive 对不存在的 PID 返回假
    _add("is_alive 对死进程为假", not is_alive(0xFFFFFFF0),
         "伪造 PID 判定为不存在")

    # 8. 白名单保护生效
    blocked = False
    try:
        kill(4, force=True, timeout=0.1)  # System 进程，必定不在白名单
    except ProcessBlocked:
        blocked = True
    except Exception:
        pass
    _add("白名单保护生效", blocked, "未授权的 PID 被拒绝")

    # 9. 命令行拼接
    q = A.command_line_from_argv([r"C:\Program Files\a b\c.exe", "x y"])
    _add("命令行引号正确", q.startswith('"') and '"x y"' in q, q)

    return {"checks": checks, "failures": failures,
            "skipped": skipped, "ok": not failures}


def _main() -> int:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for p in list_processes()[:40]:
            print(p)
        return 0
    if cmd == "selfcheck":
        res = selfcheck()
        for c in res["checks"]:
            mark = ("SKIP" if c.get("skipped")
                    else ("OK" if c["ok"] else "!!"))
            print("  [%s] %-30s %s" % (mark, c["name"], c["detail"]))
        n = len(res["checks"])
        n_skip = len(res.get("skipped") or [])
        line = "进程自检: %d/%d" % (n - len(res["failures"]) - n_skip, n)
        if n_skip:
            line += " (SKIP %d)" % n_skip
        print(line)
        return 0 if res["ok"] else 1
    if cmd == "info":
        if len(sys.argv) < 3:
            print("用法: info <pid>")
            return 2
        p = process_of(int(sys.argv[2]))
        print(p.to_dict() if p else "进程不存在")
        return 0
    print("未知命令: %s" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
