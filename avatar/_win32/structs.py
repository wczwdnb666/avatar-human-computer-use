# -*- coding: utf-8 -*-
"""Win32 结构体与常量：纯 ctypes 定义，逐字节对齐。

坐标约定（**全局唯一真值**）：
    物理像素 = 真值。逻辑像素只允许在本模块的换算函数里短暂存在。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# 64 位下指针宽度；Win32 的 ULONG_PTR 必须按指针宽度声明，
# 否则 SendInput 结构体尺寸不符 → 静默失败。
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

LONG = ctypes.c_long
DWORD = wintypes.DWORD
WORD = wintypes.WORD
BOOL = wintypes.BOOL
UINT = wintypes.UINT


# --------------------------------------------------------------------------
# 矩形 / 点
# --------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", LONG),
        ("top", LONG),
        ("right", LONG),
        ("bottom", LONG),
    ]

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def as_tuple(self):
        """(left, top, width, height)"""
        return (self.left, self.top, self.width, self.height)

    def __repr__(self):
        return "RECT(left=%d, top=%d, right=%d, bottom=%d)" % (
            self.left, self.top, self.right, self.bottom,
        )


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]

    def __iter__(self):
        return iter((self.x, self.y))

    def __repr__(self):
        return "POINT(x=%d, y=%d)" % (self.x, self.y)


# --------------------------------------------------------------------------
# 显示器
# --------------------------------------------------------------------------
class MONITORINFOEXW(ctypes.Structure):
    """EnumDisplayMonitors 回调里拿显示器全名（\\\\.\\DISPLAY1）靠它。

    注意 dwFlags 与 rcMonitor 顺序不能错；cbSize 必须在调用前填好。
    """

    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


# MONITORINFO 与 MONITORINFOEXW 前三个字段布局一致，DWORD + RECT*2 即 40 字节
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", DWORD),
    ]


MONITORINFOF_PRIMARY = 0x00000001

# GetSystemMetrics 索引
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_CMONITORS = 80
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_XVIRTUALSCREEN = 76  # 虚拟桌面左上角 X（多屏时可负）
SM_YVIRTUALSCREEN = 77  # 虚拟桌面左上角 Y

# EnumDisplaySettings
ENUM_CURRENT_SETTINGS = -1
ENUM_REGISTRY_SETTINGS = -2

# DEVMODE.dmFields
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000

CCHDEVICENAME = 32
CCHFORMNAME = 32


class DEVMODEW(ctypes.Structure):
    """EnumDisplaySettingsW 用。字段顺序严格按 wingdi.h。

    前后各有一段 union，这里用等长字节数组占位即可，因为我们只读
    dmPelsWidth / dmPelsHeight / dmDisplayFrequency / dmBitsPerPel。
    """

    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * CCHDEVICENAME),
        ("dmSpecVersion", WORD),
        ("dmDriverVersion", WORD),
        ("dmSize", WORD),
        ("dmDriverExtra", WORD),
        ("dmFields", DWORD),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * CCHFORMNAME),
        ("dmLogPixels", WORD),
        ("dmBitsPerPel", DWORD),
        ("dmPelsWidth", DWORD),
        ("dmPelsHeight", DWORD),
        ("dmDisplayFlags", DWORD),
        ("dmDisplayFrequency", DWORD),
        ("dmICMMethod", DWORD),
        ("dmICMIntent", DWORD),
        ("dmMediaType", DWORD),
        ("dmDitherType", DWORD),
        ("dmReserved1", DWORD),
        ("dmReserved2", DWORD),
        ("dmPanningWidth", DWORD),
        ("dmPanningHeight", DWORD),
    ]


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------
PROCESS_DPI_UNAWARE = 0
PROCESS_SYSTEM_DPI_AWARE = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2

DPI_AWARENESS_CONTEXT_UNAWARE = ctypes.c_void_p(-1)
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED = ctypes.c_void_p(-5)

MDT_EFFECTIVE_DPI = 0
MDT_ANGULAR_DPI = 1
MDT_RAW_DPI = 2

# 本进程 DPI 感知状态的查询结果
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2

# 逻辑单位 → 物理像素的缩放，SystemParametersInfo 的索引
SPI_GETLOGICALDPIOVERRIDE = 0x009E

# SetThreadDpiAwarenessContext 需要的类型
DPI_AWARENESS_CONTEXT = ctypes.c_void_p


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """SendInput 的输入事件。

    ⚠️ x64 下 sizeof(INPUT) 必须 == 40。自检脚本会在导入时验证。
    """

    _fields_ = [
        ("type", DWORD),
        ("u", _INPUTUNION),
    ]


INPUT_SIZE_EXPECTED_64 = 40
INPUT_SIZE_EXPECTED_32 = 28

# --------------------------------------------------------------------------
# 结构体尺寸断言（模块导入即校验）
#
# SendInput 是"按字节偏移解读"的 API：结构体尺寸差一个字节，系统会
# 静默丢弃整个输入批次，GetLastError 也只给 ERROR_INVALID_PARAMETER。
# 因此尺寸错误必须在导入期就炸，绝不能留到运行时。
#
# MOUSEINPUT  = 4(dx) + 4(dy) + 4(mouseData) + 4(dwFlags) + 4(time)
#               + 8(dwExtraInfo, x64)                          = 28，对齐到 8 → 32
# KEYBDINPUT  = 2(wVk) + 2(wScan) + 4(dwFlags) + 4(time)
#               + 8(dwExtraInfo, x64)                          = 20，对齐到 8 → 24
# HARDWAREINPUT = 4(uMsg) + 2(wParamL) + 2(wParamH)             = 8
# --------------------------------------------------------------------------
MOUSEINPUT_SIZE_EXPECTED_64 = 32
MOUSEINPUT_SIZE_EXPECTED_32 = 24

KEYBDINPUT_SIZE_EXPECTED_64 = 24
KEYBDINPUT_SIZE_EXPECTED_32 = 16

HARDWAREINPUT_SIZE_EXPECTED = 8


def expected_input_size() -> int:
    return INPUT_SIZE_EXPECTED_64 if ctypes.sizeof(ctypes.c_void_p) == 8 else INPUT_SIZE_EXPECTED_32


def _assert_struct_sizes() -> None:
    """导入期校验所有输入结构体尺寸，不符立即抛 AssertionError。

    这是质量硬约束 #2 的落地点：尺寸断言必须是**模块级常量 + 模块级校验**，
    不允许藏在函数里等着被调用。
    """
    is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
    want_mi = MOUSEINPUT_SIZE_EXPECTED_64 if is_64 else MOUSEINPUT_SIZE_EXPECTED_32
    want_ki = KEYBDINPUT_SIZE_EXPECTED_64 if is_64 else KEYBDINPUT_SIZE_EXPECTED_32

    got_mi = ctypes.sizeof(MOUSEINPUT)
    got_ki = ctypes.sizeof(KEYBDINPUT)
    got_hi = ctypes.sizeof(HARDWAREINPUT)
    got_in = ctypes.sizeof(INPUT)

    if got_mi != want_mi:
        raise AssertionError(
            "MOUSEINPUT 尺寸错误：期望 %d 字节，实际 %d 字节（%s）"
            % (want_mi, got_mi, "x64" if is_64 else "x86")
        )
    if got_ki != want_ki:
        raise AssertionError(
            "KEYBDINPUT 尺寸错误：期望 %d 字节，实际 %d 字节（%s）"
            % (want_ki, got_ki, "x64" if is_64 else "x86")
        )
    if got_hi != HARDWAREINPUT_SIZE_EXPECTED:
        raise AssertionError(
            "HARDWAREINPUT 尺寸错误：期望 %d 字节，实际 %d 字节"
            % (HARDWAREINPUT_SIZE_EXPECTED, got_hi)
        )
    if got_in != expected_input_size():
        raise AssertionError(
            "INPUT 尺寸错误：期望 %d 字节，实际 %d 字节（%s）"
            % (expected_input_size(), got_in, "x64" if is_64 else "x86")
        )
    # 联合体取最大成员
    want_union = max(got_mi, got_ki, got_hi)
    got_union = ctypes.sizeof(_INPUTUNION)
    if got_union != want_union:
        raise AssertionError(
            "_INPUTUNION 尺寸错误：期望 %d 字节（最大成员），实际 %d 字节"
            % (want_union, got_union)
        )


_assert_struct_sizes()


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------
GWL_WNDPROC = -4
GWL_HINSTANCE = -6
GWL_HWNDPARENT = -8
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWL_USERDATA = -21
GWL_ID = -12

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

# GetAncestor
GA_PARENT = 1
GA_ROOT = 2
GA_ROOTOWNER = 3

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_NORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_SHOWNA = 8
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_ACTIVATE = 0x0006
WM_SETTINGCHANGE = 0x001A
WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
WM_GETMINMAXINFO = 0x0024

PW_RENDERFULLCONTENT = 0x00000002

# SetWindowPos
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
HWND_TOP = 0
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOREDRAW = 0x0008
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOOWNERZORDER = 0x0200
SWP_NOSENDCHANGING = 0x0400
SWP_ASYNCWINDOWPOS = 0x4000


# --------------------------------------------------------------------------
# 位图 / 绘制
# --------------------------------------------------------------------------
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_byte),
        ("rgbGreen", ctypes.c_byte),
        ("rgbRed", ctypes.c_byte),
        ("rgbReserved", ctypes.c_byte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


DIB_RGB_COLORS_FLAG = 0


# --------------------------------------------------------------------------
# 进程内存信息（GetProcessMemoryInfo）
# --------------------------------------------------------------------------
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """x64 下 40 字节；``cb`` 必须由调用方填自身大小。"""

    _fields_ = [
        ("cb", DWORD),
        ("PageFaultCount", DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class STARTUPINFOW(ctypes.Structure):
    """``CreateProcessW`` 的启动信息（x64 下 104 字节）。"""

    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    """``CreateProcessW`` 的输出：新进程与主线程的句柄。"""

    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    """``Process32FirstW`` / ``Process32NextW`` 的条目结构。

    ``dwSize`` 必须由调用方填 ``sizeof(PROCESSENTRY32W)`` ——
    不填的话 Process32FirstW 直接返回 FALSE。
    """

    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


#: ``GetTokenInformation`` / ``OpenProcessToken`` 用到的令牌句柄类型别名
HANDLE = wintypes.HANDLE


# --------------------------------------------------------------------------
# 进程访问权限 / 等待
# --------------------------------------------------------------------------
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_CREATE_THREAD = 0x0002
PROCESS_SET_SESSIONID = 0x0004
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_DUP_HANDLE = 0x0040
PROCESS_CREATE_PROCESS = 0x0080
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_ALL_ACCESS = 0x001F0FFF

#: ``WaitForSingleObject`` / ``WaitForMultipleObjects`` 的返回码
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF

# --------------------------------------------------------------------------
# 令牌 / 完整性级别
# --------------------------------------------------------------------------
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_DUPLICATE = 0x0002
TokenUser = 1
TokenGroups = 2
TokenPrivileges = 3
TokenOwner = 4
TokenPrimaryGroup = 5
TokenStatistics = 10
TokenIntegrityLevel = 25
TokenElevation = 20

#: ``CreateProcessW`` 标志
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SEPARATE_WOW_VDM = 0x00000800
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
NORMAL_PRIORITY_CLASS = 0x00000020
IDLE_PRIORITY_CLASS = 0x00000040
HIGH_PRIORITY_CLASS = 0x00000080
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESIZE = 0x00000002
STARTF_USEPOSITION = 0x00000004

#: 进程快照标志
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPALL = 0x0000000F
INVALID_HANDLE_VALUE = -1

#: ``GetStdHandle`` 标准设备
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12


# --------------------------------------------------------------------------
# ShellExecuteEx
# --------------------------------------------------------------------------
SEE_MASK_DEFAULT = 0x00000000
SEE_MASK_CLASSNAME = 0x00000001
SEE_MASK_CLASSKEY = 0x00000003
SEE_MASK_IDLIST = 0x00000004
SEE_MASK_INVOKEIDLIST = 0x0000000C
SEE_MASK_ICON = 0x00000010
SEE_MASK_HOTKEY = 0x00000020
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_CONNECTNETDRV = 0x00000080
SEE_MASK_FLAG_DDEWAIT = 0x00000100
SEE_MASK_DOENVSUBST = 0x00000200
SEE_MASK_FLAG_NO_UI = 0x00000400
SEE_MASK_UNICODE = 0x00004000
SEE_MASK_NO_CONSOLE = 0x00008000
SEE_MASK_ASYNCOK = 0x00100000
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_LOG_USAGE = 0x04000000
SEE_MASK_NOZONECHECKS = 0x00800000
SEE_MASK_WAITFORINPUTIDLE = 0x02000000

#: ShellExecute 传统返回码（<= 32 为错误）
SE_ERR_FNF = 2
SE_ERR_PNF = 3
SE_ERR_ACCESSDENIED = 5
SE_ERR_OOM = 8
SE_ERR_NOASSOC = 31

#: ShellExecuteEx 失败时常见的 Win32 错误
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_CANCELLED = 1223


class SHELLEXECUTEINFOW(ctypes.Structure):
    """``ShellExecuteExW`` 的入参结构（x64 下 112 字节）。

    字段顺序严格按 shellapi.h。``cbSize`` 必须填 ``sizeof``。
    ``fMask`` 里带 ``SEE_MASK_NOCLOSEPROCESS`` 时 ``hProcess``
    才会被填写 —— 拿到后必须 ``CloseHandle``。
    """

    _fields_ = [
        ("cbSize", DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]
