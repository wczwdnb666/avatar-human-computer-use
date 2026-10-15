# -*- coding: utf-8 -*-
"""Win32 API 绑定：显式声明签名，杜绝 ctypes 默认 c_int 截断指针。

**为什么要一个个写 argtypes**：ctypes 默认把返回值当 c_int。在 x64 上句柄是
64 位指针，不声明 restype 就会被截断成 32 位 —— 句柄高位丢失，后续调用
全部失败或操作到错误对象。这类 bug 不抛异常，只是"行为不对"。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import structs as S
from .structs import BOOL, DWORD, LONG, POINT, RECT, UINT, ULONG_PTR

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

try:
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
except OSError:  # pragma: no cover
    userenv = None

try:
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
except OSError:  # pragma: no cover
    psapi = None

# shcore 在 Win8.1+ 才有；Win7 上加载失败要能优雅降级
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - 老旧系统
    shcore = None

# dwmapi 用于取窗口真实边框（Aero 阴影外扩），缺失时降级到 GetWindowRect
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:  # pragma: no cover
    dwmapi = None


# ==========================================================================
# user32 —— 屏幕与虚拟桌面
# ==========================================================================
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int

user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
user32.GetCursorPos.restype = BOOL

user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = BOOL

# ==========================================================================
# user32 —— 显示器
# ==========================================================================
MonitorEnumProc = ctypes.WINFUNCTYPE(
    BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(RECT),
    wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = (
    wintypes.HDC,
    ctypes.POINTER(RECT),
    MonitorEnumProc,
    wintypes.LPARAM,
)
user32.EnumDisplayMonitors.restype = BOOL

user32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.c_void_p)
user32.GetMonitorInfoW.restype = BOOL

user32.MonitorFromPoint.argtypes = (POINT, DWORD)
user32.MonitorFromPoint.restype = wintypes.HMONITOR

user32.MonitorFromWindow.argtypes = (wintypes.HWND, DWORD)
user32.MonitorFromWindow.restype = wintypes.HMONITOR

MONITOR_DEFAULTTONULL = 0x00000000
MONITOR_DEFAULTTOPRIMARY = 0x00000001
MONITOR_DEFAULTTONEAREST = 0x00000002

user32.EnumDisplaySettingsW.argtypes = (wintypes.LPCWSTR, DWORD, ctypes.POINTER(S.DEVMODEW))
user32.EnumDisplaySettingsW.restype = BOOL

# ==========================================================================
# user32 —— DPI
# ==========================================================================
user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
user32.GetDpiForWindow.restype = UINT

user32.GetDpiForSystem.argtypes = ()
user32.GetDpiForSystem.restype = UINT

user32.SetProcessDPIAware.argtypes = ()
user32.SetProcessDPIAware.restype = BOOL

user32.GetAwarenessFromDpiAwarenessContext.argtypes = (S.DPI_AWARENESS_CONTEXT,)
user32.GetAwarenessFromDpiAwarenessContext.restype = BOOL

user32.SetThreadDpiAwarenessContext.argtypes = (S.DPI_AWARENESS_CONTEXT,)
user32.SetThreadDpiAwarenessContext.restype = S.DPI_AWARENESS_CONTEXT

user32.GetThreadDpiAwarenessContext.argtypes = ()
user32.GetThreadDpiAwarenessContext.restype = S.DPI_AWARENESS_CONTEXT

user32.AreDpiAwarenessContextsEqual.argtypes = (
    S.DPI_AWARENESS_CONTEXT,
    S.DPI_AWARENESS_CONTEXT,
)
user32.AreDpiAwarenessContextsEqual.restype = BOOL

if shcore is not None:
    shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
    shcore.SetProcessDpiAwareness.restype = ctypes.HRESULT

    shcore.GetProcessDpiAwareness.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_int),
    )
    shcore.GetProcessDpiAwareness.restype = ctypes.HRESULT

    shcore.GetDpiForMonitor.argtypes = (
        wintypes.HMONITOR,
        ctypes.c_int,
        ctypes.POINTER(UINT),
        ctypes.POINTER(UINT),
    )
    shcore.GetDpiForMonitor.restype = ctypes.HRESULT

# ==========================================================================
# user32 —— 窗口
# ==========================================================================
user32.EnumWindows.argtypes = (
    ctypes.WINFUNCTYPE(BOOL, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
)
user32.EnumWindows.restype = BOOL

user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = BOOL

user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = BOOL

user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = BOOL

user32.IsZoomed.argtypes = (wintypes.HWND,)
user32.IsZoomed.restype = BOOL

user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int

user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = BOOL

user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetClientRect.restype = BOOL

user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(POINT))
user32.ClientToScreen.restype = BOOL

user32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(POINT))
user32.ScreenToClient.restype = BOOL

user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.restype = LONG

user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongPtrW.restype = ULONG_PTR

user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ULONG_PTR)
user32.SetWindowLongPtrW.restype = ULONG_PTR

user32.GetWindow.argtypes = (wintypes.HWND, UINT)
user32.GetWindow.restype = wintypes.HWND

user32.EnumChildWindows.argtypes = (
    wintypes.HWND,
    ctypes.WINFUNCTYPE(BOOL, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
)
user32.EnumChildWindows.restype = BOOL

user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetParent.restype = wintypes.HWND

user32.GetAncestor.argtypes = (wintypes.HWND, UINT)
user32.GetAncestor.restype = wintypes.HWND

user32.GetDesktopWindow.argtypes = ()
user32.GetDesktopWindow.restype = wintypes.HWND

user32.GetShellWindow.argtypes = ()
user32.GetShellWindow.restype = wintypes.HWND

user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND

user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = BOOL

user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = BOOL

user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = BOOL

user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindowAsync.restype = BOOL

user32.SetWindowPos.argtypes = (
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    UINT,
)
user32.SetWindowPos.restype = BOOL

user32.MoveWindow.argtypes = (
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    BOOL,
)
user32.MoveWindow.restype = BOOL

user32.PostMessageW.argtypes = (wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = BOOL

user32.SendMessageW.argtypes = (wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.SendMessageW.restype = wintypes.LPARAM

user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND,
    ctypes.POINTER(DWORD),
)
user32.GetWindowThreadProcessId.restype = DWORD

user32.AttachThreadInput.argtypes = (DWORD, DWORD, BOOL)
user32.AttachThreadInput.restype = BOOL

user32.SetFocus.argtypes = (wintypes.HWND,)
user32.SetFocus.restype = wintypes.HWND

user32.GetFocus.argtypes = ()
user32.GetFocus.restype = wintypes.HWND

user32.SetActiveWindow.argtypes = (wintypes.HWND,)
user32.SetActiveWindow.restype = wintypes.HWND

user32.GetActiveWindow.argtypes = ()
user32.GetActiveWindow.restype = wintypes.HWND

user32.GetWindowDC.argtypes = (wintypes.HWND,)
user32.GetWindowDC.restype = wintypes.HDC

user32.GetDC.argtypes = (wintypes.HWND,)
user32.GetDC.restype = wintypes.HDC

user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
user32.ReleaseDC.restype = ctypes.c_int

# ==========================================================================
# user32 —— 输入
# ==========================================================================
user32.SendInput.argtypes = (UINT, ctypes.POINTER(S.INPUT), ctypes.c_int)
user32.SendInput.restype = UINT

user32.MapVirtualKeyW.argtypes = (UINT, UINT)
user32.MapVirtualKeyW.restype = UINT

user32.GetKeyState.argtypes = (ctypes.c_int,)
user32.GetKeyState.restype = ctypes.c_short

user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

user32.GetKeyboardLayout.argtypes = (DWORD,)
user32.GetKeyboardLayout.restype = wintypes.HKL

user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short

user32.ToUnicodeEx.argtypes = (
    UINT,
    UINT,
    ctypes.POINTER(ctypes.c_byte),
    ctypes.POINTER(wintypes.WCHAR),
    ctypes.c_int,
    UINT,
    wintypes.HKL,
)
user32.ToUnicodeEx.restype = ctypes.c_int

user32.PtInRect.argtypes = (ctypes.POINTER(RECT), POINT)
user32.PtInRect.restype = BOOL

user32.WindowFromPoint.argtypes = (POINT,)
user32.WindowFromPoint.restype = wintypes.HWND

# ==========================================================================
# gdi32 —— 截屏 / 像素
# ==========================================================================
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.SelectObject.restype = wintypes.HGDIOBJ

gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.DeleteObject.restype = BOOL

gdi32.DeleteDC.argtypes = (wintypes.HDC,)
gdi32.DeleteDC.restype = BOOL

gdi32.BitBlt.argtypes = (
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    DWORD,
)
gdi32.BitBlt.restype = BOOL

gdi32.StretchBlt.argtypes = (
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    DWORD,
)
gdi32.StretchBlt.restype = BOOL

gdi32.GetDIBits.argtypes = (
    wintypes.HDC,
    wintypes.HBITMAP,
    UINT,
    UINT,
    ctypes.c_void_p,
    ctypes.POINTER(S.BITMAPINFO),
    UINT,
)
gdi32.GetDIBits.restype = ctypes.c_int

gdi32.GetPixel.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
gdi32.GetPixel.restype = DWORD

gdi32.CreateDIBSection.argtypes = (
    wintypes.HDC,
    ctypes.POINTER(S.BITMAPINFO),
    UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    DWORD,
)
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

gdi32.GetDeviceCaps.argtypes = (wintypes.HDC, ctypes.c_int)
gdi32.GetDeviceCaps.restype = ctypes.c_int

# ==========================================================================
# kernel32 —— 进程
# ==========================================================================
kernel32.GetCurrentProcessId.argtypes = ()
kernel32.GetCurrentProcessId.restype = DWORD

kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = DWORD

kernel32.OpenProcess.argtypes = (DWORD, BOOL, DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = BOOL

kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ==========================================================================
# dwmapi
# ==========================================================================
if dwmapi is not None:
    dwmapi.DwmGetWindowAttribute.argtypes = (
        wintypes.HWND,
        DWORD,
        ctypes.c_void_p,
        DWORD,
    )
    dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DWM_CLOAKED_APP = 0x00000001
DWM_CLOAKED_SHELL = 0x00000002
DWM_CLOAKED_INHERITED = 0x00000004


# ==========================================================================
# ole32 —— COM 基础设施
# ==========================================================================
COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0
COINIT_DISABLE_OLE1DDE = 0x4
COINIT_SPEED_OVER_MEMORY = 0x8

CLSCTX_INPROC_SERVER = 0x1
CLSCTX_INPROC_HANDLER = 0x2
CLSCTX_LOCAL_SERVER = 0x4
CLSCTX_INPROC_SERVER16 = 0x8
CLSCTX_REMOTE_SERVER = 0x10
CLSCTX_INPROC_HANDLER16 = 0x20
CLSCTX_RESERVED1 = 0x40
CLSCTX_RESERVED2 = 0x80
CLSCTX_RESERVED3 = 0x100
CLSCTX_RESERVED4 = 0x200
CLSCTX_NO_CODE_DOWNLOAD = 0x400
CLSCTX_RESERVED5 = 0x800
CLSCTX_NO_CUSTOM_MARSHAL = 0x1000
CLSCTX_ENABLE_CODE_DOWNLOAD = 0x2000
CLSCTX_NO_FAILURE_LOG = 0x4000
CLSCTX_DISABLE_AAA = 0x8000
CLSCTX_ENABLE_AAA = 0x10000
CLSCTX_FROM_DEFAULT_CONTEXT = 0x20000
CLSCTX_ACTIVATE_X86_SERVER = 0x40000
CLSCTX_ACTIVATE_32_BIT_SERVER = CLSCTX_ACTIVATE_X86_SERVER
CLSCTX_ACTIVATE_64_BIT_SERVER = 0x80000
CLSCTX_ENABLE_CLOAKING = 0x100000
CLSCTX_APPCONTAINER = 0x400000
CLSCTX_ACTIVATE_AAA_AS_IU = 0x800000
CLSCTX_PS_DLL = 0x80000000
CLSCTX_ALL = (
    CLSCTX_INPROC_SERVER | CLSCTX_INPROC_HANDLER | CLSCTX_LOCAL_SERVER | CLSCTX_REMOTE_SERVER
)
CLSCTX_SERVER = CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER | CLSCTX_REMOTE_SERVER

#: VARIANT 相关常量（供 variant.py 引用但不重复定义）
VT_EMPTY = 0
VT_NULL = 1
VT_I4 = 3
VT_BSTR = 8
VT_BOOL = 11
VT_UNKNOWN = 13
VT_DISPATCH = 9
VT_R8 = 5

ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, DWORD)
ole32.CoInitializeEx.restype = ctypes.c_long

ole32.CoInitialize.argtypes = (ctypes.c_void_p,)
ole32.CoInitialize.restype = ctypes.c_long

ole32.CoUninitialize.argtypes = ()
ole32.CoUninitialize.restype = None

ole32.CoCreateInstance.argtypes = (
    ctypes.c_void_p,     # REFCLSID
    ctypes.c_void_p,     # LPUNKNOWN pUnkOuter
    DWORD,               # DWORD dwClsContext
    ctypes.c_void_p,     # REFIID
    ctypes.POINTER(ctypes.c_void_p),   # LPVOID *ppv
)
ole32.CoCreateInstance.restype = ctypes.c_long

ole32.CoGetApartmentType.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
ole32.CoGetApartmentType.restype = None

ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
ole32.CoTaskMemFree.restype = None

ole32.CoTaskMemAlloc.argtypes = (ctypes.c_size_t,)
ole32.CoTaskMemAlloc.restype = ctypes.c_void_p

ole32.CoInitializeSecurity.argtypes = (
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
    ctypes.c_void_p, DWORD, DWORD, ctypes.c_void_p,
    DWORD, ctypes.c_void_p,
)
ole32.CoInitializeSecurity.restype = ctypes.c_long

ole32.StringFromCLSID.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
ole32.StringFromCLSID.restype = ctypes.c_long

ole32.CLSIDFromString.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p)
ole32.CLSIDFromString.restype = ctypes.c_long

ole32.PropVariantClear.argtypes = (ctypes.c_void_p,)
ole32.PropVariantClear.restype = ctypes.c_long


# ==========================================================================
# oleaut32 —— VARIANT / BSTR
# ==========================================================================
oleaut32.SysAllocString.argtypes = (ctypes.c_wchar_p,)
oleaut32.SysAllocString.restype = ctypes.c_void_p

oleaut32.SysAllocStringLen.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
oleaut32.SysAllocStringLen.restype = ctypes.c_void_p

oleaut32.SysFreeString.argtypes = (ctypes.c_void_p,)
oleaut32.SysFreeString.restype = None

oleaut32.SysStringLen.argtypes = (ctypes.c_void_p,)
oleaut32.SysStringLen.restype = ctypes.c_uint

oleaut32.SysStringByteLen.argtypes = (ctypes.c_void_p,)
oleaut32.SysStringByteLen.restype = ctypes.c_uint

oleaut32.VariantInit.argtypes = (ctypes.c_void_p,)
oleaut32.VariantInit.restype = None

oleaut32.VariantClear.argtypes = (ctypes.c_void_p,)
oleaut32.VariantClear.restype = ctypes.c_long

oleaut32.VariantCopy.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
oleaut32.VariantCopy.restype = ctypes.c_long

oleaut32.VariantChangeType.argtypes = (
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ushort, ctypes.c_ushort,
)
oleaut32.VariantChangeType.restype = ctypes.c_long

oleaut32.SafeArrayDestroy.argtypes = (ctypes.c_void_p,)
oleaut32.SafeArrayDestroy.restype = ctypes.c_long


# ==========================================================================
# advapi32 —— 令牌 / 权限
# ==========================================================================
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_READ = 0x00020008
TOKEN_ALL_ACCESS = 0x000F01FF
TOKEN_DUPLICATE = 0x0002
TOKEN_IMPERSONATE = 0x0004
MAXIMUM_ALLOWED = 0x02000000

TokenUser = 1
TokenGroups = 2
TokenPrivileges = 3
TokenOwner = 4
TokenPrimaryGroup = 5
TokenDefaultDacl = 6
TokenSource = 7
TokenType = 8
TokenImpersonationLevel = 9
TokenStatistics = 10
TokenRestrictedSids = 11
TokenSessionId = 12
TokenGroupsAndPrivileges = 13
TokenSessionReference = 14
TokenSandBoxInert = 15
TokenAuditPolicy = 16
TokenOrigin = 17
TokenElevationType = 18
TokenLinkedToken = 19
TokenElevation = 20
TokenHasRestrictions = 21
TokenAccessInformation = 22
TokenVirtualizationAllowed = 23
TokenVirtualizationEnabled = 24
TokenIntegrityLevel = 25
TokenUIAccess = 26
TokenMandatoryPolicy = 27
TokenLogonSid = 28

SECURITY_MAX_SID_SIZE = 68
SECURITY_BUILTIN_DOMAIN_RID = 0x00000020
DOMAIN_ALIAS_RID_ADMINS = 0x00000220
DOMAIN_ALIAS_RID_USERS = 0x00000221
DOMAIN_ALIAS_RID_GUESTS = 0x00000222
DOMAIN_ALIAS_RID_POWER_USERS = 0x00000223

WinBuiltinAnyPackageSid = 15
WinUntrustedLabelSid = 16
WinLowLabelSid = 17
WinMediumLabelSid = 18
WinHighLabelSid = 19
WinSystemLabelSid = 20

advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, DWORD, ctypes.POINTER(wintypes.HANDLE))
advapi32.OpenProcessToken.restype = BOOL

advapi32.OpenThreadToken.argtypes = (
    wintypes.HANDLE, DWORD, BOOL, ctypes.POINTER(wintypes.HANDLE),
)
advapi32.OpenThreadToken.restype = BOOL

advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
)
advapi32.GetTokenInformation.restype = BOOL

advapi32.SetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
)
advapi32.SetTokenInformation.restype = BOOL

advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
advapi32.ConvertSidToStringSidW.restype = BOOL

advapi32.ConvertStringSidToSidW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))
advapi32.ConvertStringSidToSidW.restype = BOOL

advapi32.CreateWellKnownSid.argtypes = (
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(DWORD),
)
advapi32.CreateWellKnownSid.restype = BOOL

advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    ctypes.c_wchar_p, DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(DWORD),
)
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL

advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)

advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, DWORD)
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(DWORD)

advapi32.CheckTokenMembership.argtypes = (
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(BOOL),
)
advapi32.CheckTokenMembership.restype = BOOL

advapi32.AdjustTokenPrivileges.argtypes = (
    wintypes.HANDLE, BOOL, ctypes.c_void_p, DWORD, ctypes.c_void_p, ctypes.c_void_p,
)
advapi32.AdjustTokenPrivileges.restype = BOOL

advapi32.LookupPrivilegeValueW.argtypes = (
    ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
)
advapi32.LookupPrivilegeValueW.restype = BOOL

advapi32.RegOpenKeyExW.argtypes = (
    wintypes.HKEY, ctypes.c_wchar_p, DWORD, DWORD, ctypes.POINTER(wintypes.HKEY),
)
advapi32.RegOpenKeyExW.restype = ctypes.c_long

advapi32.RegQueryValueExW.argtypes = (
    wintypes.HKEY, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(DWORD),
    ctypes.c_void_p, ctypes.POINTER(DWORD),
)
advapi32.RegQueryValueExW.restype = ctypes.c_long

advapi32.RegEnumKeyExW.argtypes = (
    wintypes.HKEY, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD),
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
)
advapi32.RegEnumKeyExW.restype = ctypes.c_long

advapi32.RegCloseKey.argtypes = (wintypes.HKEY,)
advapi32.RegCloseKey.restype = ctypes.c_long

HKEY_LOCAL_MACHINE = 0x80000002
HKEY_CURRENT_USER = 0x80000001
HKEY_CLASSES_ROOT = 0x80000000
KEY_READ = 0x20019
KEY_WOW64_64KEY = 0x0100
KEY_WOW64_32KEY = 0x0200


# ==========================================================================
# userenv —— 完整性级别的 SDDL 转换
# ==========================================================================
if userenv is not None:
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = (
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p),
    )
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long


# ==========================================================================
# shell32 —— ShellExecute / 图标
# ==========================================================================
shell32.ShellExecuteW.argtypes = (
    wintypes.HWND, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
    ctypes.c_wchar_p, ctypes.c_int,
)
shell32.ShellExecuteW.restype = wintypes.HINSTANCE

shell32.ShellExecuteExW.argtypes = (ctypes.c_void_p,)
shell32.ShellExecuteExW.restype = BOOL

shell32.SHGetFileInfoW.argtypes = (
    ctypes.c_wchar_p, DWORD, ctypes.c_void_p, DWORD, ctypes.c_uint,
)
shell32.SHGetFileInfoW.restype = ctypes.c_void_p

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
SHGFI_EXETYPE = 0x000002000
SHGFI_TYPENAME = 0x000000400

SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3


# ==========================================================================
# kernel32 —— 进程创建
# ==========================================================================
if psapi is not None:
    psapi.GetModuleBaseNameW.argtypes = (
        wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, DWORD,
    )
    psapi.GetModuleBaseNameW.restype = DWORD

    psapi.EnumProcesses.argtypes = (ctypes.POINTER(DWORD), DWORD, ctypes.POINTER(DWORD))
    psapi.EnumProcesses.restype = BOOL

    psapi.GetProcessImageFileNameW.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, DWORD,
    )
    psapi.GetProcessImageFileNameW.restype = DWORD

    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = BOOL
else:
    psapi.GetProcessMemoryInfo = None


# STARTUPINFOW / PROCESS_INFORMATION / PROCESSENTRY32W /
# PROCESS_MEMORY_COUNTERS 的**唯一定义**在 structs.py。
# 这里只做别名，**不要**用 class X(S.X) 继承 —— 子类是一个新的
# ctypes 类型，byref() 出来的指针类型对不上 argtypes，会报
# "expected LP_STARTUPINFOW instance instead of pointer to STARTUPINFOW"。
STARTUPINFOW = S.STARTUPINFOW
PROCESS_INFORMATION = S.PROCESS_INFORMATION


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

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259

kernel32.CreateProcessW.argtypes = (
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    BOOL, DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p,
)
kernel32.CreateProcessW.restype = BOOL

kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, DWORD)
kernel32.WaitForSingleObject.restype = DWORD

kernel32.WaitForMultipleObjects.argtypes = (DWORD, ctypes.POINTER(wintypes.HANDLE), BOOL, DWORD)
kernel32.WaitForMultipleObjects.restype = DWORD

kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(DWORD))
kernel32.GetExitCodeProcess.restype = BOOL

kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, ctypes.c_uint)
kernel32.TerminateProcess.restype = BOOL

kernel32.CreateToolhelp32Snapshot.argtypes = (DWORD, DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.Process32FirstW.restype = BOOL

kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.Process32NextW.restype = BOOL

user32.WaitForInputIdle.argtypes = (wintypes.HANDLE, DWORD)
user32.WaitForInputIdle.restype = DWORD

kernel32.GetLastError.argtypes = ()
kernel32.GetLastError.restype = DWORD

kernel32.GetProcessId.argtypes = (wintypes.HANDLE,)
kernel32.GetProcessId.restype = DWORD

kernel32.GetModuleFileNameExW = None  # 走 psapi

# --------------------------------------------------------------------------
# kernel32 —— 错误消息格式化
# --------------------------------------------------------------------------
FORMAT_MESSAGE_ALLOCATE_BUFFER = 0x00000100
FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200
FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
FORMAT_MESSAGE_FROM_HMODULE = 0x00000800
LANG_NEUTRAL = 0x00
SUBLANG_DEFAULT = 0x01


def MAKELANGID(primary: int, sub: int) -> int:
    """语言 ID 合成：低 10 位主语言，高 6 位子语言。"""
    return int(primary) | (int(sub) << 10)


kernel32.FormatMessageW.argtypes = (
    DWORD,
    ctypes.c_void_p,
    DWORD,
    DWORD,
    ctypes.c_void_p,
    DWORD,
    ctypes.c_void_p,
)
kernel32.FormatMessageW.restype = DWORD

kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
kernel32.LocalFree.restype = ctypes.c_void_p

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
MAX_PATH = 260

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_DUP_HANDLE = 0x0040
PROCESS_CREATE_PROCESS = 0x0080
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_ALL_ACCESS = 0x001F0FFF


class PROCESSENTRY32W(S.PROCESSENTRY32W):
    pass


PROCESS_MEMORY_COUNTERS = S.PROCESS_MEMORY_COUNTERS

# ==========================================================================
# 错误处理
# ==========================================================================
def last_error() -> int:
    return ctypes.get_last_error()


def win_error(code: int | None = None) -> OSError:
    return ctypes.WinError(ctypes.get_last_error() if code is None else code)


def win32_error_text(code: int) -> str:
    """Win32 错误码 → 系统消息文本（走 FormatMessageW）。

    拿不到时返回空串，**不抛异常** —— 它只用于给异常信息补充可读说明。
    """
    if not code:
        return ""
    try:
        buf = ctypes.c_void_p()
        n = kernel32.FormatMessageW(
            FORMAT_MESSAGE_ALLOCATE_BUFFER
            | FORMAT_MESSAGE_FROM_SYSTEM
            | FORMAT_MESSAGE_IGNORE_INSERTS,
            None,
            int(code),
            MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
            ctypes.byref(buf),
            0,
            None,
        )
        if not n or not buf.value:
            return ""
        try:
            text = ctypes.wstring_at(buf.value)
        finally:
            kernel32.LocalFree(buf)
        return " ".join(text.split())
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 命令行拼接
# --------------------------------------------------------------------------
def _quote_arg(a: str) -> str:
    """按 Windows ``CommandLineToArgvW`` 的反向规则给单个参数加引号。

    规则（微软官方文档 "Parsing C Command-Line Arguments"）：

    · 无空格 / 制表符 / 双引号 且非空 → 原样返回
    · 否则用双引号包裹，内部的双引号加倍，且**反斜杠要按后续是否为引号
      决定是否加倍**（连续反斜杠后跟引号时，反斜杠数翻倍；
      连续反斜杠在末尾时也要翻倍）
    """
    if a and not any(c in a for c in ' \t"'):
        return a

    out = ['"']
    i = 0
    n = len(a)
    while i < n:
        c = a[i]
        if c == "\\":
            # 数连续反斜杠
            j = i
            while j < n and a[j] == "\\":
                j += 1
            cnt = j - i
            if j >= n:
                # 末尾反斜杠：翻倍，避免转义收尾引号
                out.append("\\" * (cnt * 2))
            elif a[j] == '"':
                # 后跟引号：翻倍 + 转义该引号
                out.append("\\" * (cnt * 2 + 1))
                out.append('"')
                j += 1
            else:
                out.append("\\" * cnt)
            i = j
            continue
        if c == '"':
            out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    out.append('"')
    return "".join(out)


def command_line_from_argv(argv) -> str:
    """argv 序列 → Windows 命令行字符串。

    **不做 shell 解释**，纯粹按 ``CreateProcessW`` 的约定拼装。
    """
    return " ".join(_quote_arg(str(a)) for a in argv)


def hresult_ok(hr: int) -> bool:
    """HRESULT 成功判定：高位为 0 即成功。"""
    return hr >= 0
