# -*- coding: utf-8 -*-
"""应用启动器：从"应用名"到"可操作窗口"的一站式解析。

职责
----
把使用者的意图（"打开记事本"、"打开这台电脑"、"打开这个网址"）翻译成
一个**已经就绪、可以立刻操作**的窗口，并处理好中间的所有差异。

依赖
----
· ``avatar.app_manager.process``（CreateProcessW 启动）
· ``avatar.app_manager.windows``（窗口等待与定位）
· ``avatar._win32.api``（注册表 / ShellExecuteW / IApplicationActivationManager）
· ``avatar.errors``

## 为什么需要一个启动器模块

Windows 上"启动一个应用"至少有**五条互不相通**的路径：

    1. 可执行文件          notepad.exe / "C:\\Program Files\\X\\x.exe"
    2. App Paths 注册表    Win+R 里输入 "chrome" 能起来，靠的就是它
    3. Shell 关联          "打开 C:\\a.docx" → 由 Word 认领
    4. URI 协议            "ms-settings:" / "https://..." / "mailto:"
    5. UWP / Store 应用    Explorer 之外的宿主，CreateProcess 起不来

走错路径的表现是"没反应"或"起来了但是另一个东西"。本模块的做法是按顺序
**逐个试**，每一步都验证"窗口真的出现了"，失败才降级到下一步。

## UWP 的特殊处理

UWP 应用**不能**用 ``CreateProcessW`` 启动 —— 会返回
``ERROR_ELEVATION_REQUIRED`` 或直接找不到文件。必须走
``IApplicationActivationManager::ActivateApplication``，
参数是 **AUMID**（Application User Model ID），形如::

    Microsoft.WindowsCalculator_8wekyb3d8bbwe!App

AUMID 从注册表 ``HKCU\\Software\\Classes\\ActivatableClasses\\Package`` 下
遍历得到。本模块的 ``find_aumid()`` 做这件事。
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from .._win32 import api as A
from .._win32 import com as C
from .._win32 import guids as G
from .._win32 import structs as S
from ..errors import (ProcessBlocked, TargetError, TimeoutError_,
                      WindowNotFound)
from . import process as P
from . import windows as W

# --------------------------------------------------------------------------
# 常用别名表
# --------------------------------------------------------------------------
#: 用户口语 → 可执行文件名（或 URI）
#: 只收"用别的名字更常被叫到"的那些，不是穷举。
APP_ALIASES = {
    "记事本": "notepad.exe", "notepad": "notepad.exe",
    "计算器": "calc.exe", "calc": "calc.exe", "calculator": "calc.exe",
    "画图": "mspaint.exe", "paint": "mspaint.exe", "mspaint": "mspaint.exe",
    "命令提示符": "cmd.exe", "cmd": "cmd.exe",
    "终端": "wt.exe", "terminal": "wt.exe", "wt": "wt.exe",
    "资源管理器": "explorer.exe", "explorer": "explorer.exe",
    "文件资源管理器": "explorer.exe",
    "任务管理器": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "控制面板": "control.exe", "control": "control.exe",
    "注册表": "regedit.exe", "regedit": "regedit.exe",
    "写字板": "write.exe", "wordpad": "write.exe",
    "截图": "snippingtool.exe", "snippingtool": "snippingtool.exe",
    "edge": "msedge.exe", "浏览器": "msedge.exe",
    "chrome": "chrome.exe", "谷歌浏览器": "chrome.exe",
    "firefox": "firefox.exe", "火狐": "firefox.exe",
    "vscode": "code.exe", "code": "code.exe", "vs code": "code.exe",
    "设置": "ms-settings:", "windows设置": "ms-settings:",
}

#: 这些 URI / 协议前缀直接交给 ShellExecuteW
SHELL_PREFIXES = (
    "ms-settings:", "ms-", "http://", "https://", "file://",
    "mailto:", "shell:", "microsoft-edge:", "bingmaps:", "search-ms:",
)

#: 启动后等待窗口出现的默认超时（秒）
DEFAULT_WINDOW_TIMEOUT = 15.0

#: 部分应用启动慢（Office、IDE），给单独一档
SLOW_APPS = {
    "winword.exe": 40.0, "excel.exe": 40.0, "powerpnt.exe": 40.0,
    "outlook.exe": 40.0, "acrord32.exe": 30.0, "photoshop.exe": 60.0,
    "code.exe": 25.0, "devenv.exe": 60.0,
}


# --------------------------------------------------------------------------
# 返回类型
# --------------------------------------------------------------------------
@dataclass
class LaunchedApp:
    """``open_app()`` 的返回值 —— 一个"已经就绪的窗口"。"""

    #: 目标窗口句柄（0 表示没等到窗口）
    hwnd: int = 0
    #: 进程 PID（0 表示未取得，比如 URI 走 Shell 的情况）
    pid: int = 0
    #: 实际用的启动方式：exe / app_paths / shell / uri / aumid
    method: str = ""
    #: 实际执行的命令或 URI
    command: str = ""
    #: 窗口信息（等到了才有）
    window: Optional[object] = None
    #: 底层进程对象（若有）
    process: Optional[object] = None
    #: 尝试过程中的备注
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.hwnd != 0

    @property
    def alive(self) -> bool:
        return self.pid > 0 and P.is_alive(self.pid)

    def require_window(self) -> int:
        """要求必须有窗口，否则抛 ``WindowNotFound``。"""
        if not self.hwnd:
            raise WindowNotFound(
                "应用已启动但未出现可操作窗口（method=%s, command=%s）"
                % (self.method, self.command),
                pid=self.pid or None,
            )
        return self.hwnd

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "method": self.method,
            "command": self.command,
            "title": self.window.title if self.window else "",
            "process": self.window.process if self.window else "",
            "notes": list(self.notes),
        }

    def __str__(self):
        return "LaunchedApp(ok=%s, method=%s, hwnd=0x%X, pid=%d, cmd=%s)" % (
            self.ok, self.method, self.hwnd, self.pid, (self.command or "")[:60]
        )


# --------------------------------------------------------------------------
# 名字解析
# --------------------------------------------------------------------------
def alias_of(name: str) -> str:
    """把口语名翻译成可执行名 / URI。查不到则原样返回。"""
    key = (name or "").strip()
    if not key:
        return key
    low = key.lower()
    if low in APP_ALIASES:
        return APP_ALIASES[low]
    if key in APP_ALIASES:
        return APP_ALIASES[key]
    # 去 .exe 再查一次
    if low.endswith(".exe") and low[:-4] in APP_ALIASES:
        return APP_ALIASES[low[:-4]]
    return key


def is_uri(target: str) -> bool:
    """是否应交给 ShellExecuteW 处理的 URI / 协议。"""
    t = (target or "").strip().lower()
    return any(t.startswith(p) for p in SHELL_PREFIXES)


def resolve_app_paths(name: str) -> str:
    """查 ``App Paths`` 注册表 —— Win+R 里输入短名能起来就靠这个。

    两个位置，先查用户级：
      · ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\<name>``
      · ``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\<name>``
    """
    if not name:
        return ""
    key = name if name.lower().endswith(".exe") else name + ".exe"
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + key

    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub) as k:
                val, _ = winreg.QueryValueEx(k, "")
                val = os.path.expandvars(str(val).strip('"'))
                if val and os.path.exists(val):
                    return val
        except OSError:
            continue
    return ""


#: 中文名 → UWP 包名关键字。
#: UWP 的包名是英文的（``Microsoft.WindowsCalculator``），中文显示名要从
#: ``AppxManifest`` 的 ``ms-resource:`` 里解析 —— 成本高且不稳定。
#: 对本机自带的那几个系统应用，直接查表更可靠。
UWP_HINTS = {
    "计算器": "WindowsCalculator", "calculator": "WindowsCalculator",
    "日历": "WindowsCommunicationsApps", "calendar": "WindowsCommunicationsApps",
    "相机": "WindowsCamera", "camera": "WindowsCamera",
    "时钟": "WindowsAlarms", "闹钟": "WindowsAlarms", "clock": "WindowsAlarms",
    "邮件": "windowscommunicationsapps", "mail": "windowscommunicationsapps",
    "地图": "WindowsMaps", "maps": "WindowsMaps",
    "音乐": "ZuneMusic", "groove": "ZuneMusic",
    "电影和电视": "ZuneVideo", "movies": "ZuneVideo",
    "照片": "Windows.Photos", "photos": "Windows.Photos",
    "录音机": "WindowsSoundRecorder", "soundrecorder": "WindowsSoundRecorder",
    "天气": "BingWeather", "weather": "BingWeather",
    "资讯": "BingNews", "news": "BingNews",
    "商店": "WindowsStore", "store": "WindowsStore",
    "便笺": "MicrosoftStickyNotes", "sticky": "MicrosoftStickyNotes",
    "截图工具": "ScreenSketch", "snip": "ScreenSketch",
    "终端": "WindowsTerminal", "terminal": "WindowsTerminal",
    "记事本": "WindowsNotepad", "notepad": "WindowsNotepad",
    "画图": "Paint", "paint": "Paint",
    "任务管理器": "TaskManager", "taskmgr": "TaskManager",
    "设置": "immersivecontrolpanel", "settings": "immersivecontrolpanel",
    "白板": "Whiteboard", "whiteboard": "Whiteboard",
    "剪贴板": "Windows.Clipchamp",
    "phone": "YourPhone", "手机连接": "YourPhone",
}


def resolve_uwp(name: str) -> str:
    """把一个名字解析成 AUMID，找不到返回空串。

    解析顺序（前一步命中就返回）：

    1. 本身就是 AUMID 形式 → 原样返回
    2. 中文名 / 英文别名查 ``UWP_HINTS`` → 按关键字搜包名
    3. 直接按包名子串匹配（``Calculator`` → ``...WindowsCalculator...``）
    4. 按 manifest 里的显示名匹配（可能命中 ``ms-resource:`` 间接，命中率低）
    """
    if not name:
        return ""
    s = name.strip()
    if _looks_like_aumid(s):
        return s

    # 2. 查表
    hint = UWP_HINTS.get(s.lower()) or UWP_HINTS.get(s)
    if hint:
        got = find_aumid(hint)
        if got:
            return got

    # 3. 包名子串
    got = find_aumid(s)
    if got:
        return got

    # 去 .exe 再试（"calc.exe" → "calc"）
    stem = os.path.splitext(s)[0]
    if stem != s:
        got = find_aumid(stem)
        if got:
            return got
    return ""


def find_aumid(name: str) -> str:
    """按名字找 UWP 应用的 AUMID（显示名或包名，忽略大小写，子串匹配）。

    匹配顺序：包名 → 显示名。命中多个时取第一个。
    """
    if not name:
        return ""
    needle = name.lower()
    for display, aumid, _root in _iter_uwp():
        if needle in aumid.split("!")[0].lower():
            return aumid
    for display, aumid, _root in _iter_uwp():
        if display and needle in display.lower():
            return aumid
    return ""


#: UWP 包注册表位置（用户级，含全部已安装包）
_UWP_BASE = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppModel\Repository\Packages"
)


def _package_family(name: str) -> str:
    """从包全名提取 PackageFamilyName。

    ``Name_Version_Arch_Resource_PublisherId`` → ``Name_PublisherId``

    AUMID 用的是 **PackageFamilyName**，不是 PackageFullName ——
    带版本号和架构的写法 ``..._11.2606.0.0_x64__8wekyb3d8bbwe``
    传给 ActivateApplication 会失败。
    """
    parts = (name or "").split("_")
    if len(parts) < 2:
        return name or ""
    return "%s_%s" % (parts[0], parts[-1])


def _iter_uwp():
    """遍历已安装的 UWP 应用，yield ``(显示名, aumid, 安装目录)``。

    数据来源（实测验证过的组合）：

    1. 注册表 ``AppModel\\Repository\\Packages`` 的**子键名**是包全名
    2. 该键的 ``PackageRootFolder`` 给出安装目录
    3. 该键的 ``DisplayName`` 给出显示名（常是 ``ms-resource:...`` 间接）
    4. 安装目录下的 ``AppxManifest.xml`` 里 ``<Application Id="...">``
       给出 AppId
    5. ``AUMID = PackageFamilyName + "!" + AppId``

    为什么不用 ``HKCU\\Software\\Classes\\ActivatableClasses\\Package``：
    实测该键在本机只剩 2017 年的 70 个空壳（无子键无值），
    现代 Windows 上已经不承载应用注册了。**不要**再走那条路。
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UWP_BASE) as k:
            count = winreg.QueryInfoKey(k)[0]
            for i in range(count):
                try:
                    pkg = winreg.EnumKey(k, i)
                except OSError:
                    break
                with winreg.OpenKey(k, pkg) as pk:
                    try:
                        root, _ = winreg.QueryValueEx(pk, "PackageRootFolder")
                    except OSError:
                        continue
                    try:
                        display, _ = winreg.QueryValueEx(pk, "DisplayName")
                        display = str(display)
                    except OSError:
                        display = ""

                apps = _manifest_app_ids(root)
                if not apps:
                    continue
                pfn = _package_family(pkg)
                for app_id in apps:
                    yield display, "%s!%s" % (pfn, app_id), root
    except OSError:
        return


#: ``AppxManifest.xml`` 的 AppId 提取。用正则而非 XML 解析 ——
#: manifest 里有未声明前缀的 XML 命名空间，标准解析器会直接报错。
_MANIFEST_APP_RE = re.compile(r'<Application\b[^>]*?\bId\s*=\s*"([^"]+)"',
                              re.I | re.S)


def _manifest_app_ids(root: str) -> List[str]:
    """从包的安装目录读 ``AppxManifest.xml``，取出全部 ``<Application Id>``。"""
    if not root:
        return []
    mf = os.path.join(root, "AppxManifest.xml")
    if not os.path.exists(mf):
        return []
    try:
        with open(mf, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return []
    return _MANIFEST_APP_RE.findall(txt)


def list_aumids(limit: int = 0) -> List[Tuple[str, str]]:
    """列出所有 UWP 应用的 ``[(显示名, aumid)]``。

    显示名拿到 ``ms-resource:`` 间接引用时留空字符串 —— 那种名字
    需要 MRT 资源解析，阶段二不做（阶段七的 OCR 层也不该做）。
    """
    out = []
    for display, aumid, _root in _iter_uwp():
        if display.startswith("ms-resource:"):
            display = ""
        out.append((display, aumid))
        if limit and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# UWP 激活（纯 ctypes COM）
# --------------------------------------------------------------------------
def activate_aumid(aumid: str, timeout: float = 3.0) -> int:
    """用 ``IApplicationActivationManager`` 启动 UWP 应用，返回 PID。

    这是 UWP 的**唯一**正确启动方式 —— ``CreateProcessW`` 对它无效。

    vtable 布局（IApplicationActivationManager）::

        0 QueryInterface  1 AddRef  2 Release
        3 ActivateApplication    ← 用这个
        4 ActivateForFile   5 ActivateForProtocol

    ``ActivateApplication(appUserModelId, arguments, options, processId)``
    返回的 HRESULT 若失败，通常是因为 AUMID 写错或该应用被策略禁用。
    """
    if not aumid or "!" not in aumid:
        raise TargetError("AUMID 格式非法: %r" % aumid)

    C.init_com("MTA")
    mgr = C.create_instance(
        G.CLSID_APPLICATION_ACTIVATION_MANAGER,
        G.IID_IAPPLICATION_ACTIVATION_MANAGER,
        name="ApplicationActivationManager",
    )
    with mgr:
        pid_out = S.DWORD(0)
        # ActivateApplication(LPCWSTR, LPCWSTR, ACTIVATEOPTIONS, DWORD*)
        #   原型（服务端实际签名）：
        #     HRESULT ActivateApplication(IApplicationActivationManager* this,
        #                                 LPCWSTR appUserModelId,
        #                                 LPCWSTR arguments,
        #                                 ACTIVATEOPTIONS options,
        #                                 DWORD* processId)
        #   对应 Python 侧：restype + 4 个参数类型（this 由 call 自动补）。
        AO_NONE = 0
        hr = mgr.call(
            3, ctypes.HRESULT,
            ctypes.c_wchar_p,       # appUserModelId
            ctypes.c_wchar_p,       # arguments（None 即可）
            ctypes.c_uint,          # ACTIVATEOPTIONS
            ctypes.POINTER(S.DWORD),  # processId 出参
            args=(aumid, None, AO_NONE, ctypes.byref(pid_out)),
        )
        C.check_hresult(hr, "ActivateApplication",
                        "IApplicationActivationManager", "ActivateApplication", 3)
        return int(pid_out.value)


# --------------------------------------------------------------------------
# ShellExecute
# --------------------------------------------------------------------------
def shell_execute(target: str, params: str = "", cwd: str = "",
                  show: int = S.SW_SHOWNORMAL, wait: bool = False,
                  no_ui: bool = True) -> dict:
    """走 Shell 打开任意对象（文件 / URI / 协议 / 未在 PATH 里的程序）。

    **用 ``ShellExecuteExW`` 而不是 ``ShellExecuteW``** —— 这是个关键选择：

    ``ShellExecuteW`` 的返回值语义是"<=32 表示错误"，但实测在 Windows 11 上
    打开一个不存在的协议仍会返回 42（成功区间）。它**无法可靠报告失败**。
    ``ShellExecuteExW`` 返回 BOOL，失败时 ``GetLastError()`` 给出真实原因。

    返回 ``{"hinst", "hprocess", "pid"}``。``hprocess`` 仅当
    ``SEE_MASK_NOCLOSEPROCESS`` 生效时非零，调用方用完需 ``CloseHandle``
    （本函数在不需要时已经关掉了）。
    """
    sei = S.SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(S.SHELLEXECUTEINFOW)
    mask = S.SEE_MASK_NOCLOSEPROCESS | S.SEE_MASK_FLAG_NO_UI
    if wait:
        mask |= S.SEE_MASK_WAITFORINPUTIDLE
    sei.fMask = mask
    sei.hwnd = None
    sei.lpVerb = "open"
    sei.lpFile = target
    sei.lpParameters = params or None
    sei.lpDirectory = cwd or None
    sei.nShow = int(show)

    ok = A.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err = A.kernel32.GetLastError()
        raise TargetError(
            "ShellExecuteEx 失败: %s %s（Win32 错误 %d: %s）" % (
                target, params, err, A.win32_error_text(err)
            ),
            target=target,
        )

    hproc = sei.hProcess
    pid = 0
    if hproc:
        pid = int(A.kernel32.GetProcessId(hproc) or 0)
        if wait and pid:
            A.kernel32.WaitForSingleObject(hproc, S.INFINITE)
        A.kernel32.CloseHandle(hproc)

    return {
        "hinst": int(sei.hInstApp or 0),
        "hprocess": 0,   # 已关闭，不对外暴露悬空句柄
        "pid": pid,
    }


def shell_execute_legacy(target: str, params: str = "", cwd: str = "",
                         show: int = S.SW_SHOWNORMAL) -> int:
    """旧接口（``ShellExecuteW``），保留用于兼容。

    ⚠ **不要**用它判断成功与否 —— 它报告不了失败。需要错误信息请用
    ``shell_execute()``。
    """
    r = A.shell32.ShellExecuteW(
        None,
        ctypes.c_wchar_p("open"),
        ctypes.c_wchar_p(target),
        ctypes.c_wchar_p(params) if params else None,
        ctypes.c_wchar_p(cwd) if cwd else None,
        int(show),
    )
    return int(r) if r else 0


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def open_app(target: str,
             args: Optional[Iterable[str]] = None,
             wait_window: bool = True,
             timeout: float = 0.0,
             process: Optional[str] = None,
             title: Optional[str] = None,
             reuse: bool = True,
             exclusive: bool = False) -> LaunchedApp:
    """打开一个应用并返回**就绪的窗口**。

    参数
    ----
    target
        应用名（"记事本" / "notepad" / "msedge"）、可执行文件路径、
        文件路径（"C:\\\\a.docx"）、或 URI（"ms-settings:" / "https://..."）
    args
        传给可执行文件的参数
    wait_window
        是否等待窗口出现（默认 True）。False 时只负责启动。
    timeout
        等窗口的超时。0 表示按应用自动选（``DEFAULT_WINDOW_TIMEOUT``
        或 ``SLOW_APPS`` 里的档位）
    process
        用于定位窗口的进程名（默认从 target 推断）
    title
        用于定位窗口的标题子串（比进程名更精确，能避开辅助窗口）
    reuse
        若该应用**已在运行**，直接复用它已有的窗口，不重复启动。
        （默认 True —— 重复开记事本会得到一堆窗口，很烦人）
    exclusive
        True 时若已有窗口则抛异常而不复用。用于"必须全新实例"的场景。

    返回
    ----
    ``LaunchedApp``。``wait_window=True`` 时 ``.hwnd`` 保证非 0
    （否则抛 ``WindowNotFound``）。
    """
    original = (target or "").strip()
    if not original:
        raise TargetError("target 不能为空")

    la = LaunchedApp(command=original)
    exe = alias_of(original)
    to = timeout if timeout > 0 else _timeout_for(exe)

    # --- 0. 复用已有窗口 ---
    if reuse or exclusive:
        existing = _find_existing(original, exe, process, title)
        if existing is not None:
            if exclusive:
                raise TargetError(
                    "应用已在运行（hwnd=0x%X），exclusive=True 拒绝复用"
                    % existing.hwnd,
                    target=original,
                )
            la.hwnd = existing.hwnd
            la.pid = existing.pid
            la.window = existing
            la.method = "reuse"
            la.command = original
            la.notes.append("复用已运行窗口")
            return la

    # --- 1. URI / 协议 ---
    if is_uri(exe):
        la.notes.append("识别为 URI/协议，走 ShellExecute")
        try:
            shell_execute(exe)
            la.method = "uri"
            la.command = exe
        except Exception as e:
            la.notes.append("ShellExecute 失败: %s" % e)
            la.method = "uri"
            la.command = exe
        if wait_window:
            _await_window(la, original, exe, process, title, to)
        return la

    # --- 2. 文件路径（交给 Shell 用关联程序打开）---
    if _looks_like_path(exe) and os.path.exists(exe) \
            and not exe.lower().endswith((".exe", ".com", ".bat", ".cmd")):
        la.notes.append("识别为文件路径，走 Shell 关联")
        shell_execute(exe)
        la.method = "shell"
        if wait_window:
            _await_window(la, original, exe, process, title, None, to)
        return la

    # --- 3. UWP ---
    #   放在 exe 路径之前判断。理由：UWP 应用的窗口挂在
    #   ApplicationFrameHost.exe 上，走 CreateProcess 那套既起不来也找不到窗口，
    #   拖到最后只会白等一轮超时。
    #
    #   "计算器" 这种中文名会经 UWP_HINTS 映射到包名关键字 ——
    #   不能只靠包名子串匹配，因为本机包名是英文的（Microsoft.WindowsCalculator）。
    aumid = resolve_uwp(original)
    if not aumid and exe != original:
        aumid = resolve_uwp(exe)

    if aumid:
        la.notes.append("识别为 UWP 应用，走 IApplicationActivationManager")
        pid = activate_aumid(aumid)
        la.pid = pid
        la.method = "aumid"
        la.command = aumid
        if pid:
            P._mark_owned(pid)
        la.notes.append("已激活 AUMID: %s（pid=%d）" % (aumid, pid))
        if wait_window:
            _await_window(la, original, exe, process, title, pid or None, to)
        return la

    # --- 4. 可执行文件：先找 App Paths，再让 CreateProcess 自己找 ---
    resolved = ""
    ap = resolve_app_paths(exe)
    if ap:
        resolved = ap
        la.method = "app_paths"
        la.notes.append("App Paths 命中: %s" % ap)
    else:
        resolved = P.resolve_executable(exe)
        la.method = "exe"

    try:
        proc = P.launch(resolved, args=args)
        la.process = proc
        la.pid = proc.pid
        la.command = proc.cmdline
        la.notes.append("已启动 PID %d" % proc.pid)
    except Exception as e:
        # CreateProcess 失败。UWP 已经在第 3 步拦掉了，走到这里说明
        # 既不是 UWP 也不是有效路径 —— 最后的兜底是 ShellExecute，
        # 它能处理 UAC 提权、文件关联、以及注册表里古怪的启动命令。
        la.notes.append("CreateProcess 失败（%s），降级 ShellExecute" % e)
        try:
            shell_execute(resolved,
                          params=" ".join(str(a) for a in (args or [])))
            la.method = "shell"
            la.command = resolved
        except Exception as e2:
            la.notes.append("ShellExecute 也失败: %s" % e2)
            raise TargetError(
                "无法启动 %r：%s；ShellExecute 兜底也失败：%s"
                % (original, e, e2),
                target=original,
            )

    if wait_window:
        _await_window(la, original, exe, process, title, la.pid or None, to)
    return la


def _timeout_for(exe: str) -> float:
    base = os.path.basename((exe or "").lower())
    return SLOW_APPS.get(base, DEFAULT_WINDOW_TIMEOUT)


def _looks_like_path(s: str) -> bool:
    return (os.sep in s) or ("/" in s) or (len(s) > 1 and s[1] == ":")


def _looks_like_aumid(s: str) -> bool:
    return "!" in s and "_" in s and "\\" not in s and "/" not in s


def _find_existing(original: str, exe: str,
                   process: Optional[str], title: Optional[str]):
    """找该应用已存在的可用窗口。"""
    proc_name = process or _infer_process(exe)
    try:
        if title:
            return W.find_window(title, process=proc_name, usable_only=True)

        # UWP：按 AUMID 反查运行中的应用进程
        aumid = resolve_uwp(original) or resolve_uwp(exe)
        if aumid:
            w = _find_running_uwp(aumid)
            if w is not None:
                return w
            return None   # 是 UWP 但没在跑，交给主流程去激活

        if proc_name and not is_uri(exe):
            ws = W.find_windows(process=proc_name, usable_only=True)
            if ws:
                return ws[0]
    except WindowNotFound:
        return None
    except Exception:
        return None
    return None


def _find_running_uwp(aumid: str):
    """若某个 UWP 应用已在运行，返回它的窗口。

    判定方式：遍历所有 ``ApplicationFrameWindow``，用
    ``Windows.UI.Core.CoreWindow`` 的 PID 反查进程名/路径，
    看是否属于该 AUMID 对应的包。

    ``ApplicationFrameHost`` 的外壳窗口标题就是应用显示名，
    所以这里用"标题匹配 AUMID 的短名"作为快速启发式，
    避免为每个外壳都去解析包清单。
    """
    if not aumid or "!" not in aumid:
        return None
    pkg_short = aumid.split("!")[0].split("_")[0]
    # 去掉常见前缀，留最后的可读段（Microsoft.WindowsCalculator → WindowsCalculator）
    tokens = [t for t in re.split(r"[.\-_]", pkg_short) if len(t) > 3]
    if not tokens:
        return None
    key = tokens[-1].lower()

    for w in W.list_windows():
        if w.class_name != W.UWP_FRAME_CLASS or not w.is_usable:
            continue
        app_pid = W.uwp_app_pid(w.hwnd)
        if not app_pid:
            continue
        try:
            path = P.process_of(app_pid)
        except Exception:
            path = None
        hay = ((w.title or "") + " " + ((path.path or "") if path else "")).lower()
        if key in hay:
            # 修正 PID —— 外壳窗口的 w.pid 是 ApplicationFrameHost，
            # 上层做 UIPI 判定、进程查询时要用真实的应用进程 PID。
            info = W.refresh(w.hwnd)
            info.pid = app_pid
            core = W.uwp_core_window(w.hwnd)
            if core:
                info.child_hwnd = core
            return info
    return None


def _infer_process(exe: str) -> str:
    """从 target 推断用于窗口匹配的进程名。"""
    if not exe or is_uri(exe):
        return ""
    base = os.path.basename(exe)
    if base and base.lower().endswith(".exe"):
        return base
    if base and "." not in base:
        return base + ".exe"
    return ""


def _await_window(la: LaunchedApp, original: str, exe: str,
                  process: Optional[str], title: Optional[str],
                  pid: Optional[int], timeout: float) -> None:
    """等待窗口出现并填入 ``la``。找不到抛 ``WindowNotFound``。

    ## UWP 的坑

    ``pid`` 对 UWP 是**应用进程**，但窗口 ``hwnd`` 挂在
    ``ApplicationFrameHost.exe`` 上。所以普通 PID 比对找不到它 ——
    必须走 ``W.window_of_pid``，它内部会下钻外壳取 ``CoreWindow`` 的 PID。
    """
    proc_name = process or _infer_process(exe)
    deadline = time.monotonic() + max(0.1, timeout)
    poll = 0.15

    while True:
        w = None
        # 优先：精确标题（UWP 的标题在外壳窗口上，能直接命中）
        if title:
            try:
                w = W.find_window(title, process=proc_name or None,
                                  usable_only=True)
            except WindowNotFound:
                w = None
        # 其次：PID（含 UWP 外壳下钻）
        if w is None and pid:
            try:
                w = W.window_of_pid(pid, usable_only=True)
            except Exception:
                w = None
        # 再次：进程名
        if w is None and proc_name and not _looks_like_aumid(exe):
            try:
                ws = W.find_windows(process=proc_name, usable_only=True)
                w = ws[0] if ws else None
            except Exception:
                w = None

        if w is not None:
            la.window = w
            la.hwnd = w.hwnd
            if not la.pid:
                la.pid = w.pid
            return

        if time.monotonic() >= deadline:
            raise WindowNotFound(
                "等待应用窗口超时（%.1fs，target=%r，process=%r，pid=%s）"
                % (timeout, original, proc_name, pid or "-"),
                pid=pid,
                process=proc_name or None,
            )
        time.sleep(poll)


# --------------------------------------------------------------------------
# 便捷封装
# --------------------------------------------------------------------------
def open_and_focus(target: str, **kw) -> LaunchedApp:
    """打开并**确保前台**。返回的窗口已可直接接收键鼠事件。"""
    from . import foreground as FG
    la = open_app(target, **kw)
    if la.hwnd:
        FG.force_foreground(la.hwnd)
    return la


def close_app(target: str, process: Optional[str] = None,
              force: bool = False, timeout: float = 5.0) -> int:
    """关闭某个应用的全部窗口。返回处理的窗口数。

    走 ``WM_CLOSE``（优雅）—— 应用可以弹保存确认框。
    ``force=True`` 时若优雅关闭无效会强杀进程。

    ## UWP 的坑（实测踩过）

    记事本在 Win11 上是 **Store 版 UWP**，它的窗口句柄属于
    ``ApplicationFrameHost.exe``，真实进程在
    ``Windows.UI.Core.CoreWindow`` 里，进程名是 ``WindowsNotepad``
    而不是 ``notepad.exe``。

    早期实现只按 ``notepad.exe`` 找窗口 → 找到 0 个 → 返回 0，
    **静默地什么都没干**。现在按 ``_find_existing`` 同一套逻辑解析：
    先试 UWP（AUMID → CoreWindow PID），再退回普通进程窗口。
    """
    exe = alias_of(target)
    proc_name = process or _infer_process(exe)

    ws = []
    # 1) UWP 路径：和 _find_existing 保持一致。
    #    注意：resolve_uwp() 对"记事本"这类**有 Win32 实现也有 Store 版**
    #    的应用也会返回 AUMID，但实际跑的可能就是 Win32 版。
    #    所以这里 UWP 找不到**不能提前返回**，必须继续走第 2 步。
    try:
        aumid = resolve_uwp(target) or resolve_uwp(exe)
        if aumid:
            w = _find_running_uwp(aumid)
            if w is not None:
                ws = [w]
    except Exception:
        ws = []

    # 2) 普通进程窗口（Win32 应用，以及上面 UWP 没命中的情况）
    if not ws and proc_name:
        ws = W.find_windows(process=proc_name, usable_only=False)

    # 3) 兜底：按 target 当标题找
    if not ws:
        try:
            ws = [W.find_window(target, usable_only=False)]
        except WindowNotFound:
            return 0

    from . import state as ST
    n = 0
    killed_pids = set()
    for w in ws:
        # 只要"真正的窗口" —— 用 is_usable 判定。
        #
        # ⚠ 不能用"有没有标题"来判定：IME 窗口就叫 "Default IME"、
        # GDI+ 钩子窗口叫 "GDI+ Window (Notepad.exe)"，**都有标题**。
        # 它们 is_usable=False（不可见 / 不是普通顶部窗口）。
        #
        # 为什么必须排除：它们和被关的主窗口同属一个 PID，
        # 全算进去会让 close_app 的返回值虚高
        # （实测：记事本 1 个真窗口 + 6 个辅助窗口 → 返回 7）。
        if not getattr(w, "is_usable", True):
            continue

        try:
            ST.close(w.hwnd, force=force, wait=False)
            n += 1
        except ProcessBlocked:
            # 进程白名单拦截：不是自己启动的进程。
            # 这类进程**只关窗口、不强杀** —— 符合"默认只允许关闭
            # 自己启动的进程"的既有约定。
            try:
                ST.close(w.hwnd, force=False, wait=False)
                n += 1
            except Exception:
                continue
        except Exception:
            continue
        pid = int(getattr(w, "pid", 0) or 0)
        if pid:
            killed_pids.add(pid)

    # 优雅关闭发完后，给应用一点时间自己退出；不退的再按需强杀。
    if force and killed_pids:
        deadline = time.monotonic() + max(1.0, min(timeout, 5.0))
        while time.monotonic() < deadline:
            if not any(P.by_pid(p) for p in killed_pids):
                break
            time.sleep(0.1)
        for pid in killed_pids:
            if P.by_pid(pid):
                try:
                    from .process import kill
                    kill(pid, force=True, allow_foreign=True,
                         timeout=timeout)
                except Exception:
                    pass
    return n


def is_running(target: str, process: Optional[str] = None) -> bool:
    """该应用是否在运行（有进程或有窗口都算）。"""
    exe = alias_of(target)
    proc_name = process or _infer_process(exe)
    if proc_name:
        if P.by_name(proc_name):
            return True
    if is_uri(exe) or _looks_like_aumid(exe):
        return False
    try:
        W.find_window(exe.replace(".exe", ""), usable_only=False)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. 别名解析
    _add("别名解析正确", alias_of("记事本") == "notepad.exe"
         and alias_of("notepad") == "notepad.exe",
         "记事本 → notepad.exe")

    # 2. URI 识别
    _add("URI 识别正确",
         is_uri("https://example.com") and is_uri("ms-settings:")
         and not is_uri("notepad.exe"),
         "https/ms-settings 命中，notepad 不命中")

    # 3. AUMID 识别
    _add("AUMID 识别正确",
         _looks_like_aumid("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
         and not _looks_like_aumid("C:\\a\\b.exe"),
         "形如 Pkg_xxx!App 的识别为 AUMID")

    # 4. App Paths 注册表可读（找一个一定存在的）
    ap = resolve_app_paths("notepad.exe") or resolve_app_paths("mspaint.exe")
    _add("App Paths 可读", True,
         ap if ap else "(本机无 notepad/mspaint 的 App Paths 条目，跳过)")

    # 5. 能列出 UWP 应用
    aumids = list_aumids()
    _add("可枚举 UWP AUMID", len(aumids) > 0,
         "发现 %d 个，例如 %s" % (
             len(aumids), aumids[0][1][:60] if aumids else "(无)"))

    # 5b. 按名字能找到 AUMID（计算器是系统自带，必定存在）
    calc = find_aumid("Calculator")
    _add("可按名字定位 UWP", bool(calc) and "!" in calc,
         "Calculator → %s" % (calc or "(未找到)"))

    # 5c. PackageFamilyName 提取正确
    _add("PackageFamilyName 提取正确",
         _package_family("Microsoft.WindowsCalculator_11.2606.0.0_x64__8wekyb3d8bbwe")
         == "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
         "去掉版本号与架构")

    # 5d. AUMID 里不含版本号（含版本号会激活失败）
    bad = [a for _d, a in aumids if re.search(r"_\d+\.\d+\.\d+", a.split("!")[0])]
    _add("AUMID 不含版本号", not bad,
         "共 %d 个，格式全部合法" % len(aumids) if not bad
         else "%d 个含版本号，例如 %s" % (len(bad), bad[0]))

    # 6. 超时档位
    _add("慢启动应用有独立超时", _timeout_for("winword.exe") > DEFAULT_WINDOW_TIMEOUT,
         "winword → %.0fs" % _timeout_for("winword.exe"))

    # 7. 进程名推断
    _add("进程名推断正确",
         _infer_process("notepad.exe") == "notepad.exe"
         and _infer_process("msedge") == "msedge.exe",
         "notepad.exe / msedge → .exe")

    # 8. is_running 不抛异常
    try:
        r = is_running("不存在的应用XYZ")
        _add("is_running 安全返回", r is False, "未知应用 → False")
    except Exception as e:
        _add("is_running 安全返回", False, "异常: %s" % e)

    # 9. ShellExecuteEx 能正确报告失败
    _add("ShellExecuteEx 失败可检出",
         _get_shell_rc_ok(),
         "不存在的文件 → TargetError + 错误 2（ShellExecuteW 报告不出）")

    return {"checks": checks, "failures": failures, "ok": not failures}


def _get_shell_rc_ok() -> bool:
    """验证 ShellExecuteEx 的错误路径会抛异常。

    用**不存在的文件路径**做探针 —— 实测三种非法目标的行为：

        C:\\definitely\\not\\here\\xyz123.txt   → False + 错误 2（可检出）
        not-a-real-app-xyz123                  → False + 错误 2（可检出）
        zzz-nonexistent-proto-xyz://abc        → True  + 错误 0（**检不出**）

    最后一种是个**真实的 Windows 行为**，不是我们的 bug：未知协议
    会被 Shell 视为"可交给'选择一个应用'对话框处理"，因此返回成功。
    对照之下旧的 ``ShellExecuteW`` 对前两种也返回 42（看似成功），
    所以本模块没有用它。
    """
    try:
        shell_execute(r"C:\definitely\not\here\xyz123.txt")
        return False
    except TargetError:
        return True
    except Exception:
        return False


def _main() -> int:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"

    if cmd == "selfcheck":
        res = selfcheck()
        for c in res["checks"]:
            print("  [%s] %-28s %s" % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
        print("启动器自检: %d/%d" % (
            len(res["checks"]) - len(res["failures"]), len(res["checks"])))
        return 0 if res["ok"] else 1

    if cmd == "aumids":
        rows = list_aumids(limit=int(sys.argv[2]) if len(sys.argv) > 2 else 40)
        for name, aumid in rows:
            print("%-40s %s" % (name, aumid))
        print("共 %d 个" % len(rows))
        return 0

    if cmd == "open":
        if len(sys.argv) < 3:
            print("用法: open <应用名|路径|URI> [--no-wait]")
            return 2
        wait = "--no-wait" not in sys.argv
        la = open_app(sys.argv[2], wait_window=wait)
        print(la.to_dict())
        return 0

    print("未知命令: %s（可选: selfcheck / aumids / open）" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
