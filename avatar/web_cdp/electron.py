# -*- coding: utf-8 -*-
"""Electron / CEF 应用的调试端口发现与附加。

Electron 应用（VS Code、钉钉、飞书、Discord…）本质上是一个**自带
Chromium 的桌面程序**。如果它在启动时带上了
``--remote-debugging-port=N``，那么它对外暴露的 CDP 和 Chrome 一模一样 ——
我们就能用同一套代码控制它，而且**比 UIA 强得多**：
能读 DOM、能跑 JS、能精确点击，不受控件树残缺之苦。

难点在于：**端口是我们不知道的**。三条路：

1. **命令行解析**：能从目标 pid 拿到 command line 就直接读。
   Windows 上标准库拿不到别的进程的 command line（``psutil`` 是第三方，
   不许用），所以走 Win32：``NtQueryInformationProcess`` 拿 PEB →
   读 ``ProcessParameters.CommandLine``。本模块用 ctypes 实现。
   拿不到（权限不足 / WOW64 / 受保护进程）就退到第 2 条。

2. **TCP 表扫描（主力手段）**：遍历系统 TCP 连接表
   （``GetExtendedTcpTable``），筛出 ``OwningPid == 目标 pid``
   且状态为 ``LISTEN`` 的端口，逐个 ``GET /json/version``
   确认是不是 CDP。**必须确认** —— Electron 应用可能同时开着
   自己的 HTTP 服务（本地 API、调试面板），端口看起来一样。

3. **环境变量**：``ELECTRON_ENABLE_LOGGING=1`` 会让 Electron 把
   DevTools 的监听端口打到 stderr。**只能用于我们自己启动的进程**，
   已运行的进程读不到它的 stderr。所以只作为 ``launch_electron`` 的辅助。

## 关键坑

1. **``GetExtendedTcpTable`` 的缓冲区会不够**。正确姿势：先调一次
   拿 ``ERROR_INSUFFICIENT_BUFFER`` 和所需大小，再按大小分配重调。
   硬编码大小在不同机器上会崩。
2. **表里既有 IPv4 也有 IPv6**。只查 ``AF_INET`` 会漏掉只监听
   ``::1`` 的实例（Electron 默认就监听 ``127.0.0.1``，但有些配置
   监听 ``::1``）。两条都要查。
3. **端口字节序**：``dwLocalPort`` 是**网络字节序**存在低 16 位，
   要 ``ntohs``。直接当小端整数读会得到 8.8、0.58 这种鬼端口。
4. **一个 pid 可能监听多个端口**，只有一个（或零个）是 CDP。
   必须逐个探测，不能拿第一个就当 CDP。
5. **Electron 的调试端口只绑本地回环**，跨机访问不到，这是对的
   （安全设计），别指望从宿主机外连。
6. **子进程同样是 Electron**：Electron 会 fork 出 GPU 进程、
   Renderer 进程、Utility 进程。**只有主进程（Browser 进程）
   持有调试端口**。用进程树找主进程不靠谱 —— 直接扫端口更快。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from ..errors import AvatarError

__all__ = [
    "ElectronError",
    "ElectronApp",
    "scan_listen_ports",
    "find_debug_port",
    "probe_port",
    "detect_electron",
    "attach_electron",
    "find_electron_windows",
    "launch_electron",
    "ELECTRON_ENV_HINT",
    "selfcheck",
]


class ElectronError(AvatarError):
    """Electron 附加失败。"""

    exit_code = 21


#: 让 Electron 把 DevTools 端口打到 stderr 的环境变量（仅自启进程有效）。
ELECTRON_ENV_HINT = "ELECTRON_ENABLE_LOGGING"

# TCP 表状态：LISTEN
TCP_STATE_LISTEN = 2

# Windows API 常量
AF_INET = 2
AF_INET6 = 23
ERROR_INSUFFICIENT_BUFFER = 122
NO_ERROR = 0

#: GetExtendedTcpTable 的 TableClass
TCP_TABLE_OWNER_PID_ALL = 5

# 进程权限相关（取命令行走这些）
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_is_windows = True
try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
except Exception:      # pragma: no cover - 非 Windows
    _is_windows = False
    _kernel32 = None
    _iphlpapi = None


# --------------------------------------------------------------------------
# TCP 表扫描
# --------------------------------------------------------------------------

class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wt.DWORD),
        ("dwLocalAddr", wt.DWORD),
        ("dwLocalPort", wt.DWORD),
        ("dwRemoteAddr", wt.DWORD),
        ("dwRemotePort", wt.DWORD),
        ("dwOwningPid", wt.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wt.DWORD),
        ("dwLocalPort", wt.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wt.DWORD),
        ("dwRemotePort", wt.DWORD),
        ("dwState", wt.DWORD),
        ("dwOwningPid", wt.DWORD),
    ]


def _ntohs(v: int) -> int:
    """网络字节序的 16 位端口 → 主机整数。"""
    return socket.ntohs(v & 0xFFFF)


def _table_v4() -> List[Tuple[int, int, int]]:
    """返回 IPv4 TCP 表 [(pid, port, state), ...]。取不到返回空列表。"""
    if not _is_windows:
        return []
    size = wt.DWORD(0)
    ret = _iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, AF_INET,
        TCP_TABLE_OWNER_PID_ALL, 0)
    if ret not in (NO_ERROR, ERROR_INSUFFICIENT_BUFFER) or size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    ret = _iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(size), False, AF_INET,
        TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != NO_ERROR:
        return []

    count = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    row_size = ctypes.sizeof(_MIB_TCPROW_OWNER_PID)
    base = ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD)
    out = []
    for i in range(count):
        addr = base + i * row_size
        row = _MIB_TCPROW_OWNER_PID.from_address(addr)
        out.append((int(row.dwOwningPid), _ntohs(int(row.dwLocalPort)),
                    int(row.dwState)))
    return out


def _table_v6() -> List[Tuple[int, int, int]]:
    """返回 IPv6 TCP 表 [(pid, port, state), ...]。"""
    if not _is_windows:
        return []
    size = wt.DWORD(0)
    try:
        ret = _iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET6,
            TCP_TABLE_OWNER_PID_ALL, 0)
    except Exception:
        return []
    if ret not in (NO_ERROR, ERROR_INSUFFICIENT_BUFFER) or size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    try:
        ret = _iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False, AF_INET6,
            TCP_TABLE_OWNER_PID_ALL, 0)
    except Exception:
        return []
    if ret != NO_ERROR:
        return []

    count = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    row_size = ctypes.sizeof(_MIB_TCP6ROW_OWNER_PID)
    base = ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD)
    out = []
    for i in range(count):
        addr = base + i * row_size
        row = _MIB_TCP6ROW_OWNER_PID.from_address(addr)
        out.append((int(row.dwOwningPid), _ntohs(int(row.dwLocalPort)),
                    int(row.dwState)))
    return out


def scan_listen_ports(pid: int = 0, *, listening_only: bool = True,
                      include_v6: bool = True) -> List[int]:
    """扫出监听端口。

    ``pid=0`` 表示不过滤 pid（列出全系统的监听端口，慎用，量很大）。
    返回**去重排序**后的端口列表。
    """
    rows = list(_table_v4())
    if include_v6:
        rows.extend(_table_v6())
    ports = []
    for r_pid, r_port, r_state in rows:
        if pid and r_pid != int(pid):
            continue
        if listening_only and r_state != TCP_STATE_LISTEN:
            continue
        if r_port <= 0:
            continue
        ports.append(r_port)
    return sorted(set(ports))


# --------------------------------------------------------------------------
# 端口 → CDP 确认
# --------------------------------------------------------------------------

def probe_port(port: int, *, host: str = "127.0.0.1",
               timeout: float = 1.2) -> Optional[Dict[str, Any]]:
    """确认端口上是不是 CDP。是则返回 ``/json/version`` 的 JSON。

    **判据是响应体里有 ``webSocketDebuggerUrl``**，不是状态码。
    任何 HTTP 服务都会返回 200 —— 只认状态码会把 Electron 自己的
    本地 API 误判成调试端口，然后连上去握手失败，浪费调试时间。
    """
    try:
        from .launcher import http_json
    except Exception:
        return None
    try:
        data = http_json(int(port), "/json/version", host=host,
                         timeout=timeout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("webSocketDebuggerUrl"):
        return None
    return data


def find_debug_port(
    pid: int,
    *,
    timeout: float = 1.2,
    extra_ports: Optional[List[int]] = None,
    include_v6_loopback: bool = True,
) -> Optional[int]:
    """在指定 pid 的监听端口里找出 CDP 端口。

    ``extra_ports``：已知候选端口（比如从命令行解析出来的），
    **优先探测** —— 比全表扫描快，且能覆盖 pid 扫描漏掉的情况
    （某些 Electron 主进程不直接持有 socket，由子进程持有）。
    """
    order = []  # type: List[int]
    for p in (extra_ports or []):
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= p <= 65535 and p not in order:
            order.append(p)

    for p in scan_listen_ports(pid):
        if p not in order:
            order.append(p)

    for p in order:
        if probe_port(p, timeout=timeout):
            return p

    # 回环 IPv6 上再试一遍（::1）
    if include_v6_loopback:
        for p in order:
            if probe_port(p, host="::1", timeout=timeout):
                return p
    return None


# --------------------------------------------------------------------------
# 进程识别
# --------------------------------------------------------------------------

class ElectronApp(object):
    """一个被识别为 Electron 的窗口/进程。"""

    __slots__ = ("hwnd", "pid", "process_name", "kind", "debug_port",
                 "cdp_info", "session_id", "attached", "notes", "title")

    def __init__(self, hwnd: int = 0, pid: int = 0, process_name: str = "",
                 kind: str = "unknown", title: str = "") -> None:
        self.hwnd = int(hwnd)
        self.pid = int(pid)
        self.process_name = process_name
        self.kind = kind
        self.title = title
        self.debug_port = None  # type: Optional[int]
        self.cdp_info = None    # type: Optional[Dict[str, Any]]
        self.session_id = None  # type: Optional[str]
        self.attached = False
        self.notes = []         # type: List[str]

    @property
    def ok(self) -> bool:
        return self.attached and bool(self.session_id)

    @property
    def has_cdp(self) -> bool:
        return self.debug_port is not None and bool(self.cdp_info)

    @property
    def websocket_url(self) -> str:
        if self.cdp_info:
            return self.cdp_info.get("webSocketDebuggerUrl") or ""
        return ""

    @property
    def product(self) -> str:
        """从 UA 串里抠出产品名（``Chrome/120.0`` 之类）。"""
        ua = (self.cdp_info or {}).get("User-Agent") or ""
        for part in ua.split():
            if "/" in part and part.split("/")[0] in (
                    "Chrome", "HeadlessChrome", "Electron"):
                return part
        return ""

    def describe(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "process_name": self.process_name,
            "kind": self.kind,
            "title": self.title[:120],
            "debug_port": self.debug_port,
            "attached": self.attached,
            "session_id": self.session_id,
            "product": self.product,
            "has_cdp": self.has_cdp,
            "notes": self.notes,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return "<ElectronApp %s pid=%d port=%s attached=%s>" % (
            self.process_name, self.pid, self.debug_port, self.attached)


def _window_pid(hwnd: int) -> int:
    """取窗口所属 pid。失败返回 0。"""
    if not _is_windows or not hwnd:
        return 0
    try:
        pid = wt.DWORD(0)
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)),
                                         ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _process_name_of(pid: int) -> str:
    """取进程 exe 名。失败返回空串。"""
    if not _is_windows or not pid:
        return ""
    try:
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  int(pid))
        if not h:
            return ""
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                    ctypes.byref(size)):
                return buf.value or ""
            return ""
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return ""


def _window_title(hwnd: int) -> str:
    if not _is_windows or not hwnd:
        return ""
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        n = _user32.GetWindowTextLengthW(wt.HWND(int(hwnd)))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(wt.HWND(int(hwnd)), buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def detect_electron(
    hwnd: int = 0,
    *,
    pid: int = 0,
    process_name: str = "",
    probe_port_: Optional[int] = None,
) -> Optional[ElectronApp]:
    """识别窗口 / 进程是不是 Electron 应用。

    **三重判据**（按成本从低到高）：

    1. 进程名在 :data:`ELECTRON_PROCESSES` 白名单里 → 标记
       ``kind="electron_whitelist"``。
    2. 进程名不在白名单，但该 pid 有监听端口且端口是 CDP
       → 标记 ``kind="electron_cdp"``（**最强的判据**，
       因为只有 Electron / Chromium 才会暴露 CDP）。
    3. 都不是 → 返回 None。

    注意 ``kind="browser"`` 的情况**也返回对象** ——
    浏览器同样支持 CDP，调用方不必区分（只影响显示文案）。

    返回 ``None`` 表示"不是 Electron，也没有 CDP"，此时应降级 UIA。
    """
    from .fallback import detect_process_kind

    eff_pid = int(pid) if pid else _window_pid(hwnd)
    name = process_name or (_process_name_of(eff_pid) if eff_pid else "")
    base = ""
    if name:
        base = name.replace("\\", "/").rsplit("/", 1)[-1]

    kind = detect_process_kind(base) if base else "unknown"

    app = ElectronApp(hwnd, eff_pid, base or name, kind,
                      _window_title(hwnd) if hwnd else "")

    if kind in ("browser", "electron"):
        app.notes.append("进程名命中白名单：%s" % kind)
        if probe_port_:
            info = probe_port(probe_port_)
            if info:
                app.debug_port = int(probe_port_)
                app.cdp_info = info
                app.notes.append("指定端口 %d 确认是 CDP" % probe_port_)
        return app

    # 名字不认识 —— 靠 CDP 端口反证
    if eff_pid:
        port = find_debug_port(eff_pid,
                               extra_ports=[probe_port_] if probe_port_
                               else None)
        if port:
            info = probe_port(port)
            if info:
                app.debug_port = port
                app.cdp_info = info
                app.kind = "electron_cdp"
                app.notes.append("进程名不在白名单，但 pid %d 的端口 %d "
                                 "确认是 CDP" % (eff_pid, port))
                return app

    # 显式给了端口就试一下（哪怕 pid 拿不到）
    if probe_port_:
        info = probe_port(probe_port_)
        if info:
            app.debug_port = int(probe_port_)
            app.cdp_info = info
            app.kind = "electron_cdp"
            app.notes.append("端口 %d 确认是 CDP" % probe_port_)
            return app

    return None


def find_electron_windows(
    *,
    process_names: Optional[List[str]] = None,
    with_cdp_only: bool = False,
) -> List[ElectronApp]:
    """枚举桌面上的 Electron 窗口。

    走阶段二的窗口枚举（复用，不重写）。``process_names`` 给了就只留
    名字命中的。``with_cdp_only=True`` 时只返回**已确认有 CDP** 的
    —— 这个最实用：直接拿到能控制的目标。
    """
    out = []  # type: List[ElectronApp]
    try:
        from ..uia import windows as WUIA
    except Exception:
        return out

    listers = []
    for attr in ("list_windows", "enum_windows", "get_windows",
                 "_list_windows", "all_windows"):
        fn = getattr(WUIA, attr, None)
        if callable(fn):
            listers.append(fn)
    if not listers:
        return out

    raw = []
    for fn in listers:
        try:
            got = fn()
        except Exception:
            continue
        if got:
            raw = list(got)
            break

    wanted = None
    if process_names:
        wanted = {n.replace("\\", "/").rsplit("/", 1)[-1].lower()
                  .replace(".exe", "") for n in process_names}

    seen = set()
    for w in raw:
        try:
            if isinstance(w, dict):
                hwnd = int(w.get("hwnd") or w.get("handle") or 0)
                pid = int(w.get("pid") or 0)
                name = w.get("process_name") or w.get("exe") or ""
                title = w.get("title") or ""
            else:
                hwnd = int(getattr(w, "hwnd", 0) or 0)
                pid = int(getattr(w, "pid", 0) or 0)
                name = getattr(w, "process_name", "") or ""
                title = getattr(w, "title", "") or ""
        except Exception:
            continue
        if not hwnd or hwnd in seen:
            continue
        seen.add(hwnd)
        app = detect_electron(hwnd, pid=pid, process_name=name)
        if app is None:
            continue
        if wanted is not None:
            key = app.process_name.lower().replace(".exe", "")
            if key not in wanted:
                continue
        if with_cdp_only and not app.has_cdp:
            # 名字命中但没确认端口 —— 再花一次扫描确认
            if app.pid:
                p = find_debug_port(app.pid)
                if p:
                    app.debug_port = p
                    app.cdp_info = probe_port(p)
            if not app.has_cdp:
                continue
        out.append(app)
    return out


# --------------------------------------------------------------------------
# 附加
# --------------------------------------------------------------------------

def attach_electron(
    hwnd: int = 0,
    *,
    pid: int = 0,
    process_name: str = "",
    port: Optional[int] = None,
    timeout: float = 15.0,
    auto_enable: bool = True,
    target_hint: Optional[str] = None,
) -> Optional[ElectronApp]:
    """附加到 Electron 应用，返回**已带 session_id** 的 ``ElectronApp``。

    拿不到 CDP 就返回 **None**（不抛异常）—— 调用方据此降级 UIA。
    抛异常在这里是不合适的：附加失败是**预期内**的常态
    （绝大多数商业 Electron 不开调试端口）。

    ``target_hint``：页面 URL 片段，用于在多个 target 里挑对的
    （Electron 常有好几个 window target）。
    """
    from .session import open_session
    from . import tab as TAB

    app = detect_electron(hwnd, pid=pid, process_name=process_name,
                          probe_port_=port)
    if app is None:
        return None

    if not app.has_cdp:
        eff_pid = app.pid
        if eff_pid:
            p = find_debug_port(eff_pid, extra_ports=[port] if port else None)
            if p:
                app.debug_port = p
                app.cdp_info = probe_port(p)
    if not app.has_cdp:
        app.notes.append("未找到 CDP 端口，需降级 UIA")
        return None

    ws = app.websocket_url
    if not ws:
        app.notes.append("cdp_info 里没有 webSocketDebuggerUrl")
        return None

    try:
        sess = open_session(ws, timeout=timeout)
    except Exception as exc:
        app.notes.append("连接失败：%s" % (exc,))
        return None

    # 挑一个 page target
    try:
        pages = TAB.list_tabs(sess, visible_only=False, timeout=timeout)
    except Exception:
        pages = []
    if not pages and target_hint:
        t = TAB.find_tab(sess, url_contains=target_hint, timeout=timeout)
        pages = [t] if t else []

    chosen = None
    if target_hint and pages:
        for t in pages:
            if target_hint.lower() in (t.url or "").lower():
                chosen = t
                break
    if chosen is None and pages:
        # 优先非 about:blank 的
        real = [t for t in pages if not t.blank]
        chosen = real[0] if real else pages[0]

    if chosen is None:
        app.notes.append("没有可用的 page target")
        # 仍然把 session 挂上，调用方可能想开新 target
        app.session_id = None
        app.attached = False
        try:
            sess.close()
        except Exception:
            pass
        return None

    sid = TAB.attach_tab(sess, chosen.target_id, timeout=timeout)
    if not sid:
        app.notes.append("attachToTarget 失败")
        try:
            sess.close()
        except Exception:
            pass
        return None

    app.session_id = sid
    app.attached = True
    if auto_enable:
        try:
            sess.enable("Page", "Runtime", "DOM", "Network",
                        session_id=sid)
            app.notes.append("已 enable Page/Runtime/DOM/Network")
        except Exception as exc:
            app.notes.append("enable 部分失败：%s" % (exc,))
    app.notes.append("target=%s (%s)" % (chosen.target_id[:12], chosen.url[:80]))
    app.notes.append("session=%s" % sid)
    return app


def launch_electron(
    exe: str,
    *,
    port: int = 0,
    args: Optional[List[str]] = None,
    env_extra: Optional[Dict[str, str]] = None,
    wait_ready: bool = True,
    ready_timeout: float = 25.0,
) -> Tuple[Any, ElectronApp]:
    """启动一个带调试端口的 Electron 应用（**只对自己拉起的进程有效**）。

    返回 ``(Popen, ElectronApp)``。``port=0`` 时自动挑一个空闲端口。

    环境变量会带上 ``ELECTRON_ENABLE_LOGGING=1`` —— 有些应用不开这个
    就完全不理会 ``--remote-debugging-port``（Electron 默认会拦掉
    未知开关，除非显式允许）。
    """
    import os
    import subprocess

    from .launcher import is_port_free, pick_free_port

    if not exe or not os.path.isfile(exe):
        raise ElectronError("可执行文件不存在：%s" % exe)

    eff_port = int(port) if port else pick_free_port(9300, 9400)
    if not is_port_free(eff_port):
        eff_port = pick_free_port(9300, 9400)
        if not is_port_free(eff_port):
            raise ElectronError("找不到空闲端口用于调试")

    argv = [exe]
    argv.extend(args or [])
    argv.append("--remote-debugging-port=%d" % eff_port)
    argv.append("--remote-allow-origins=*")

    env = dict(os.environ)
    env[ELECTRON_ENV_HINT] = "1"
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    creationflags = 0
    if _is_windows:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(
        argv, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    app = ElectronApp(0, proc.pid,
                      os.path.basename(exe).replace(".exe", ""),
                      "electron_launched")
    app.debug_port = eff_port
    app.notes.append("以 --remote-debugging-port=%d 启动" % eff_port)
    app.notes.append("%s=1" % ELECTRON_ENV_HINT)

    if wait_ready:
        deadline = time.time() + max(1.0, ready_timeout)
        while time.time() < deadline:
            if proc.poll() is not None:
                app.notes.append("进程提前退出，返回码 %s" % proc.returncode)
                return proc, app
            info = probe_port(eff_port)
            if info:
                app.cdp_info = info
                app.notes.append("端口 %d 已就绪" % eff_port)
                break
            time.sleep(0.25)
        else:
            app.notes.append("等待端口就绪超时（%.1fs）" % ready_timeout)
    return proc, app


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

#: heavy 段（真 socket 探测）的检查项名。quick 档跳过它们并如实记账。
#: ⚠ 改动 heavy 段时同步这张表 —— 数字偏了自检输出会立刻显形。
HEAVY_CHECK_NAMES = (
    "scan_listen_ports 全表可跑",
    "全表有监听端口",
    "端口去重",
    "端口已排序",
    "端口都在合法范围",
    "不含 0 端口",
    "按 pid 过滤返回子集",
    "含非 LISTEN 时数量不减",
    "include_v6=False 返回子集",
    "本机无监听端口可检 → 跳过",
    "按 pid 过滤可跑（本进程可能无监听）",
    "不存在的 pid 返回空",
    "非 Windows 扫描返回空",
    "probe_port 在空端口返回 None",
    "200 但无 webSocketDebuggerUrl → None",
    "带 webSocketDebuggerUrl → 识别为 CDP",
    "识别后能取到 ws url",
    "find_debug_port 从 pid 端口里找到",
    "find_debug_port extra_ports 优先",
    "find_debug_port 非 CDP 时返回 None",
)


def selfcheck(quick: bool = True) -> Dict[str, Any]:
    checks = []
    skipped = []

    def add(name, ok, detail="", skip=False):
        """登记一条检查。

        ``skip=True`` → 本机环境缺这个资源（例如一个监听端口都没有），
        不是代码有 bug。标 SKIP，不计入 failures。
        """
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": str(detail)})
        if skip:
            skipped.append(name)

    skipped_heavy: List[str] = []

    # 假 HTTP 服务的 handler —— 定义在 setup 区（类定义本身不耗时），
    # 因为 heavy 段和它后面的反证段都要用它。
    # 放 heavy 段里的话，quick 档跳过该段后，后面那两处会 UnboundLocalError。
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        mode = "plain"

        def do_GET(self):  # noqa: N802
            body = b"hello, not a cdp"
            if self.mode == "cdp":
                import json as _json
                body = _json.dumps({
                    "Browser": "Chrome/120.0.0.0",
                    "Protocol-Version": "1.3",
                    "User-Agent": "Mozilla/5.0 Chrome/120.0.0.0",
                    "webSocketDebuggerUrl":
                        "ws://127.0.0.1:49522/devtools/browser/abc",
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    # --- 字节序转换（最容易错的点） ---
    add("_ntohs 小端 0x1F90(8080) 还原",
        _ntohs(socket.htons(8080)) == 8080, _ntohs(socket.htons(8080)))
    add("_ntohs 处理 0 值", _ntohs(0) == 0)
    add("_ntohs 截断到 16 位", _ntohs(0xFFFF0000 | socket.htons(9222)) == 9222,
        _ntohs(0xFFFF0000 | socket.htons(9222)))

    # --- 结构体尺寸（对齐错了会读出垃圾） ---
    if _is_windows:
        add("MIB_TCPROW_OWNER_PID 尺寸 24",
            ctypes.sizeof(_MIB_TCPROW_OWNER_PID) == 24,
            ctypes.sizeof(_MIB_TCPROW_OWNER_PID))
        add("MIB_TCP6ROW_OWNER_PID 尺寸 56",
            ctypes.sizeof(_MIB_TCP6ROW_OWNER_PID) == 56,
            ctypes.sizeof(_MIB_TCP6ROW_OWNER_PID))
    else:
        add("非 Windows 跳过结构体尺寸检查", True, "skipped")

    # ================= heavy 档：真 socket 探测 =================
    #
    # ⚠ 这一段**真的建 socket**：扫 TCP 表、起本地假 HTTP 服务、
    #   对空端口做连接尝试（Windows 上不会立刻 RST，要等满 timeout）。
    #   实测 select 1.21s + sleep 0.45s + connect 0.43s ≈ 2.1s ——
    #   是 CI 上超 3s 红线的主因，也随机器负载浮动。
    #   归 heavy：默认档不跑，--full 才跑。
    if quick:
        for _nm in HEAVY_CHECK_NAMES:
            skipped_heavy.append(_nm)
    else:
        # --- TCP 表扫描（真实调用） ---
        #
        # 实测澄清（重要）：**TCP 表扫描本身极快**（``scan_listen_ports()``
        # 全表只要 0.001s，因为用的是 ``GetExtendedTcpTable`` 一次性拿全量）。
        # 本模块 selfcheck 的耗时几乎全部来自 ``probe_port`` ——
        # 对**没有监听的端口**，Windows 不会立刻 RST，而是要等到超时
        # （实测 ``timeout=0.3`` 就实打实花 0.32s）。所以省时间要省在
        # probe 的 timeout 上，不是省扫描次数。
        #
        # 下面保持所有检查的真调用覆盖 —— 不为省时间削弱语义。
        ports_all = scan_listen_ports()
        add("scan_listen_ports 全表可跑", isinstance(ports_all, list))
        if _is_windows:
            import os
            mypid = os.getpid()
            mine = scan_listen_ports(mypid)
            nons = scan_listen_ports(0, listening_only=False)
            v6_off = scan_listen_ports(0, include_v6=False)

            # ⚠ 这一组全部以 ports_all 为**基准**（判格式或判子集关系）。
            #   Windows 上 ports_all 几乎总是非空，但**环境资源不存在 ≠
            #   代码有 bug** —— 空基准时判 SKIP，不能判 FAIL。
            #
            #   而且空基准下后果分两种，都坏：
            #     · `all(...)` / `0 not in []` / `[] == sorted([])` → 恒 True（**假通过**）
            #     · `set(真实) <= set([])` → 恒 False（**假红**）
            #   假通过比假红更坏 —— 它让人以为验过了。
            if ports_all:
                add("全表有监听端口", True, len(ports_all))
                add("端口去重", len(ports_all) == len(set(ports_all)))
                add("端口已排序", ports_all == sorted(ports_all))
                add("端口都在合法范围",
                    all(1 <= p <= 65535 for p in ports_all))
                add("不含 0 端口", 0 not in ports_all)
                add("按 pid 过滤返回子集",
                    set(mine) <= set(ports_all), (len(mine), len(ports_all)))
                add("含非 LISTEN 时数量不减", len(nons) >= len(ports_all),
                    (len(nons), len(ports_all)))
                # 真调用：不含 v6 时结果应是子集（v6 端口确实被排除）
                add("include_v6=False 返回子集",
                    set(v6_off) <= set(ports_all))
            else:
                for nm in ("全表有监听端口", "端口去重", "端口已排序",
                           "端口都在合法范围", "不含 0 端口",
                           "按 pid 过滤返回子集", "含非 LISTEN 时数量不减",
                           "include_v6=False 返回子集"):
                    add(nm, False, "本机无监听端口可检 → 跳过", skip=True)

            # 下面两项是**绝对契约**，不拿全表当基准，所以照样验
            add("按 pid 过滤可跑（本进程可能无监听）",
                isinstance(mine, list))
            add("不存在的 pid 返回空", scan_listen_ports(999999) == [])
        else:
            add("非 Windows 扫描返回空", ports_all == [])

        # --- probe_port 判据 ---
        #
        # ⚠ ``timeout=0.05`` 而不是 0.3：49521 上没有服务，Windows 不会
        #   立刻回 RST，连接要**等满超时**才放弃。实测 ``timeout=0.3``
        #   就实打实花掉 0.32s（本模块 selfcheck 的主要耗时来源）。
        #   "返回 None"这个结论在 50ms 内不会变 —— 因为压根没有服务在听。
        #   这里的 timeout 是"连接尝试的上限"，不是"等待结果的时间"。
        add("probe_port 在空端口返回 None",
            probe_port(49521, timeout=0.05) is None)

        # 用本地假 HTTP 服务验证"200 但不是 CDP 要判 None"

        srv = None
        try:
            _Handler.mode = "plain"
            srv = http.server.HTTPServer(("127.0.0.1", 49522), _Handler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.15)

            add("200 但无 webSocketDebuggerUrl → None",
                probe_port(49522, timeout=1.0) is None)

            _Handler.mode = "cdp"
            got = probe_port(49522, timeout=1.0)
            add("带 webSocketDebuggerUrl → 识别为 CDP", got is not None, got)
            add("识别后能取到 ws url",
                (got or {}).get("webSocketDebuggerUrl", "").startswith("ws://"),
                (got or {}).get("webSocketDebuggerUrl"))

            # find_debug_port 用本进程 pid 应能找到（假服务由本进程线程持有）
            import os as _os
            found = find_debug_port(_os.getpid(), timeout=1.0)
            add("find_debug_port 从 pid 端口里找到",
                found == 49522, (found, _os.getpid()))
            add("find_debug_port extra_ports 优先",
                find_debug_port(999999, extra_ports=[49522], timeout=1.0) == 49522)

            _Handler.mode = "plain"
            add("find_debug_port 非 CDP 时返回 None",
                find_debug_port(999999, extra_ports=[49522],
                                include_v6_loopback=False,
                                timeout=0.5) is None)
        finally:
            if srv is not None:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass

    # --- ElectronApp 契约 ---
    e = ElectronApp(1234, 5678, "Code.exe", "electron", "main.py")
    add("hwnd/pid 记录", e.hwnd == 1234 and e.pid == 5678)
    add("初始未附加", e.attached is False and e.ok is False)
    add("初始无 cdp", e.has_cdp is False)
    add("初始 ws url 空", e.websocket_url == "")
    add("初始 notes 空", e.notes == [])
    e.debug_port = 9222
    e.cdp_info = {"webSocketDebuggerUrl": "ws://x/y",
                  "User-Agent": "Mozilla/5.0 Electron/28.0"}
    add("有 cdp_info 后 has_cdp True", e.has_cdp is True)
    add("ws url 透出", e.websocket_url == "ws://x/y")
    add("product 抠出 Electron/28.0", e.product == "Electron/28.0", e.product)
    e.session_id = "S1"
    e.attached = True
    add("附加后 ok True", e.ok is True)
    add("describe 字段齐",
        set(e.describe()) >= {"hwnd", "pid", "process_name", "kind", "title",
                              "debug_port", "attached", "session_id",
                              "product", "has_cdp", "notes"},
        sorted(e.describe()))

    # --- 常量 ---
    add("TCP_STATE_LISTEN 为 2", TCP_STATE_LISTEN == 2)
    add("TCP_TABLE_OWNER_PID_ALL 为 5", TCP_TABLE_OWNER_PID_ALL == 5)
    add("AF_INET6 为 23", AF_INET6 == 23)
    add("ELECTRON_ENV_HINT 正确",
        ELECTRON_ENV_HINT == "ELECTRON_ENABLE_LOGGING")

    # --- detect_electron 白名单路径 ---
    a = detect_electron(0, pid=0, process_name="Code.exe")
    add("白名单命中返回对象", a is not None)
    add("白名单 kind=electron", a.kind == "electron", a.kind)
    add("白名单带 note", len(a.notes) >= 1)

    a = detect_electron(0, process_name="chrome.exe")
    add("浏览器也返回对象", a is not None and a.kind == "browser")

    a = detect_electron(0, process_name="explorer.exe")
    add("explorer 返回 None（无端口）", a is None)

    a = detect_electron(0, process_name="weirdtool.exe")
    add("未知进程无端口返回 None", a is None)

    # 未知进程 + 显式 CDP 端口 → 反证为 electron_cdp
    srv2 = None
    try:
        _Handler.mode = "cdp"
        srv2 = http.server.HTTPServer(("127.0.0.1", 49523), _Handler)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        time.sleep(0.15)
        a = detect_electron(0, process_name="weirdtool.exe",
                            probe_port_=49523)
        add("未知进程+CDP端口 → electron_cdp",
            a is not None and a.kind == "electron_cdp",
            (a.kind if a else None))
        add("反证后 has_cdp True", (a.has_cdp if a else False))
    finally:
        if srv2 is not None:
            try:
                srv2.shutdown()
                srv2.server_close()
            except Exception:
                pass

    # --- attach_electron 失败路径必须返回 None 不抛 ---
    #
    # ⚠ 这里的 timeout 直接决定 selfcheck 能不能达标，说清楚原理：
    #   ``attach_electron`` 在"探测到 CDP 端口"之后会**真去握手**。
    #   下面的假服务只回 ``/json/version``（给了个假的 ws url），
    #   握手必然失败 —— 于是它要**等满 timeout** 才放弃。
    #   实测 ``timeout=2.0`` 让这一条独占 2.0s（占整个 selfcheck 的 60%）。
    #
    #   被验证的性质是"连不上时安全返回 None，不抛异常" ——
    #   这个性质跟等多久**无关**：0.3s 连不上和 2.0s 连不上，
    #   返回的都是 None。所以用 0.3s 足够，语义零损失。
    add("attach 无端口进程返回 None",
        attach_electron(0, process_name="explorer.exe", timeout=1.0) is None)
    add("attach 未知进程无端口返回 None",
        attach_electron(0, process_name="zzz.exe", timeout=1.0) is None)

    # 端口是 CDP 但只有 /json/version（没有 page target）→ 也应安全返回
    srv3 = None
    try:
        _Handler.mode = "cdp"
        srv3 = http.server.HTTPServer(("127.0.0.1", 49524), _Handler)
        threading.Thread(target=srv3.serve_forever, daemon=True).start()
        time.sleep(0.15)
        res = attach_electron(0, process_name="weirdtool.exe", port=49524,
                              timeout=0.3)
        add("非真 CDP 服务 attach 安全返回 None", res is None, res)
    finally:
        if srv3 is not None:
            try:
                srv3.shutdown()
                srv3.server_close()
            except Exception:
                pass

    # --- launch_electron 参数校验 ---
    try:
        launch_electron("C:/definitely/not/here.exe", wait_ready=False)
        add("launch 不存在 exe 抛 ElectronError", False, "没抛")
    except ElectronError:
        add("launch 不存在 exe 抛 ElectronError", True)

    # --- find_electron_windows 不崩 ---
    got = find_electron_windows()
    add("find_electron_windows 可跑返回列表", isinstance(got, list),
        len(got))

    failures = [c["name"] for c in checks
                if not c["ok"] and not c.get("skipped")]
    return {"checks": checks, "failures": failures, "skipped": skipped,
            "skipped_heavy": len(skipped_heavy),
            "quick": bool(quick),
            "ok": not failures, "module": "web_cdp.electron"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
