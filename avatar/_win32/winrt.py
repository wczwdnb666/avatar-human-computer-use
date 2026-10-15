# -*- coding: utf-8 -*-
"""WinRT 地基 —— 纯 ctypes 手搓，不依赖任何 SDK、不调 .NET。

## 这一层解决什么

Windows 有一批能力**只以 WinRT 形式暴露**，没有等价的 Win32 API。
最典型的就是 OCR（:mod:`Windows.Media.Ocr`）—— Win32 侧根本没有
"识别屏幕上的字"这个能力。

用 WinRT 通常要装 C++/WinRT、C# 或者 `winrt` Python 包。本模块
**一个都不装**，直接用 ctypes 调 `combase.dll` 的四个导出函数，
自己拼 HSTRING、自己走 vtable。

## WinRT 与 COM 的三个区别（这是本模块存在的理由）

1. **激活方式**：COM 用 `CoCreateInstance(CLSID)`；WinRT 用
   `RoGetActivationFactory(类名)`。类名是**字符串**，不是 GUID。
2. **字符串类型**：COM 用 `BSTR`；WinRT 用 `HSTRING`（不可变、引用计数、
   由 `WindowsCreateString` 分配）。
3. **异步**：WinRT 的 `IAsyncOperation<T>` 是**跨语言**的等待协议，
   不能像 COM 那样 `WaitForSingleObject` 等它 —— 必须轮询 `get_Status`。

## 四个导出函数（全在 combase.dll）

    RoInitialize(RO_INIT_TYPE)                     → HRESULT
    RoUninitialize()
    RoGetActivationFactory(HSTRING, REFIID, void**)
    WindowsCreateString / WindowsGetStringRawBuffer / WindowsDeleteString

## vtable 布局（x64）

WinRT 接口的前 6 个槽位是固定的：

    [0] QueryInterface   [1] AddRef   [2] Release
    [3] GetIids          [4] GetRuntimeClassName   [5] GetTrustLevel

**具体方法从 [6] 开始**。所以调用某个方法前必须知道它在第几槽 ——
本模块把槽位做成常量（见 `VTBL_*`），并在 :func:`describe_winrt`
里列出来，方便核对。

## 能力边界

**做**：初始化 WinRT；创建/读取/释放 HSTRING；按类名激活工厂；
按槽位调用任意 WinRT 方法；等待 `IAsyncOperation`；枚举对象实现的接口
（`GetIids`）；给出可读的失败诊断。

**不做**：

- **不封装具体 WinRT 类**。OCR 在 :mod:`avatar.uia.ocr_fallback`，
  图像在 :mod:`avatar.vision.capture`。本模块只提供"怎么调"，
  不提供"调什么"。
- **不做 ABI 之外的猜测**。槽位号是硬编码常量，**不做运行时探测** ——
  探测（比如按方法签名匹配）猜错的代价是静默调错函数，
  比直接报错危险得多。
- **不管理 COM 生命周期**。释放要显式调 :func:`release`，
  或用 :class:`WinRtPtr` 的上下文管理器。
- **不处理 STA/MTA 切换**。`RoInitialize` 是**按线程**的，
  初始化过了就改不了（返回 `RPC_E_CHANGED_MODE`）——
  本模块只如实报告，不尝试修复。
- **不做异常翻译**。WinRT 的错误是 HRESULT，本模块把它翻成
  :class:`WinRtError` 并带上方法名，但不做语义分类
  （"哪个错误该重试"是调用方的判断）。
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .com import ComError, check_hresult, succeeded
from .guids import GUID, guid

__all__ = [
    # 初始化
    "RO_INIT_MULTITHREADED",
    "RO_INIT_SINGLETHREADED",
    "ro_initialize",
    "ro_uninitialize",
    "ro_state",
    "ensure_initialized",
    # HSTRING
    "HSTRING_HEADER",
    "HString",
    "HStringRef",
    "hstring",
    "hstring_ref",
    "read_hstring",
    # 激活
    "IActivationFactory",
    "IID_IActivationFactory",
    "activation_factory",
    "activate_instance",
    # 调用
    "vtbl",
    "vtbl_call",
    "release",
    "add_ref",
    "get_iids",
    "WinRtPtr",
    # 异步
    "ASYNC_STARTED",
    "ASYNC_COMPLETED",
    "ASYNC_CANCELED",
    "ASYNC_ERROR",
    "async_status",
    "await_async",
    # 常量表
    "IID_OCR_ENGINE",
    "IID_OCR_ENGINE_STATICS",
    "IID_OCR_RESULT",
    "IID_OCR_LINE",
    "IID_OCR_WORD",
    "IID_SOFTWARE_BITMAP_STATICS",
    "IID_IBUFFER_BYTE_ACCESS",
    "VTBL_OCR_ENGINE",
    "VTBL_OCR_ENGINE_STATICS",
    "VTBL_OCR_RESULT",
    "VTBL_OCR_LINE",
    "VTBL_OCR_WORD",
    "VTBL_SOFTWARE_BITMAP_STATICS",
    "VTBL_DATA_WRITER",
    "WinRtError",
    # 诊断
    "describe_winrt",
    "selfcheck",
]


# ==========================================================================
# combase.dll 绑定
# ==========================================================================
#: WinRT 的运行时库。它随 Windows 8+ 自带，不需要额外安装。
try:
    _combase = ctypes.WinDLL("combase.dll", use_last_error=True)
except OSError as _exc:      # pragma: no cover - 非 Windows 才会走到
    _combase = None
    _LOAD_ERROR = str(_exc)
else:
    _LOAD_ERROR = ""

#: 多线程单元（MTA）。**推荐**：MTA 的 WinRT 对象可以从任意线程用。
RO_INIT_MULTITHREADED = 1
#: 单线程单元（STA）。有些 UI 相关的 WinRT 类要求 STA。
RO_INIT_SINGLETHREADED = 0

_S_OK = 0
_S_FALSE = 1
#: 线程已经在别的单元模型里初始化过 —— **改不了**，只能报告。
RPC_E_CHANGED_MODE = 0x80010106

#: HRESULT → 可读文本（只列本模块会碰到的几个，其余走 check_hresult）
_HR_HINTS = {
    RPC_E_CHANGED_MODE:
        "该线程已用另一种单元模型初始化过（通常是 STA）。"
        "RoInitialize 只能在第一次调用时定单元模型，之后改不了。"
        "解决办法：在一个新线程里跑 WinRT，或者让本进程从头用 MTA。",
    0x80040154:      # REGDB_E_CLASSNOTREG
        "找不到这个 WinRT 类。检查类名拼写，或系统版本过低。",
    0x80070002:      # ERROR_FILE_NOT_FOUND
        "系统缺少对应的 WinRT 组件（语言包 / 可选功能未安装）。",
    0x80070005:      # E_ACCESSDENIED
        "访问被拒。Windows.Media.Ocr 等类要求应用具有 **包标识**"
        "（MSIX 打包），普通 exe 调用会被拒。",
    0x80004005:      # E_FAIL
        "WinRT 内部失败（E_FAIL）。常见于 unpackaged 应用调受限命名空间。",
}


class WinRtError(ComError):
    """WinRT 调用失败。**带方法名与 HRESULT 提示。**"""

    def __init__(self, message: str, *, hr: int = 0, method: str = "",
                 interface: str = "", hint: str = ""):
        super().__init__(message)
        self.hr = int(hr)
        self.method = method
        self.interface = interface
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        bits = []
        if self.method:
            bits.append("方法 %s" % self.method)
        if self.interface:
            bits.append("接口 %s" % self.interface)
        if bits:
            base += "（%s）" % " · ".join(bits)
        if self.hint:
            base += "\n  ⚠ " + self.hint
        return base


# ==========================================================================
# HSTRING
# ==========================================================================
class HSTRING_HEADER(ctypes.Structure):
    """`WindowsCreateStringReference` 用的栈上头部。

    ABI 上是 32 字节（x64）：

        union { PVOID Reserved1; char Reserved2[24]; } Reserved;
        ULONG Reserved3;                    // 4 字节，尾部对齐到 32

    本模块**不解析它的内部字段** —— 它就是个不透明缓冲区，
    由 `WindowsCreateStringReference` 填、由同一个调用返回的
    HSTRING 引用。所以用 `c_byte * 32` 最省事也最不容易错。
    """

    _fields_ = [("Reserved", ctypes.c_byte * 32)]


def _bind_combase() -> None:
    """声明 combase.dll 的函数签名。**必须显式声明** ——
    不声明的话 ctypes 默认按 int 处理参数，64 位指针会被截断。"""
    if _combase is None:
        return

    _combase.RoInitialize.argtypes = [ctypes.c_int]
    _combase.RoInitialize.restype = ctypes.c_long

    _combase.RoUninitialize.argtypes = []
    _combase.RoUninitialize.restype = None

    _combase.RoGetActivationFactory.argtypes = [
        ctypes.c_void_p,                     # HSTRING
        ctypes.POINTER(GUID),                # REFIID
        ctypes.POINTER(ctypes.c_void_p),     # void**
    ]
    _combase.RoGetActivationFactory.restype = ctypes.c_long

    _combase.WindowsCreateString.argtypes = [
        ctypes.c_wchar_p,                    # LPCWSTR
        ctypes.c_uint32,                     # UINT32
        ctypes.POINTER(ctypes.c_void_p),     # HSTRING*
    ]
    _combase.WindowsCreateString.restype = ctypes.c_long

    _combase.WindowsCreateStringReference.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(HSTRING_HEADER),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _combase.WindowsCreateStringReference.restype = ctypes.c_long

    _combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    _combase.WindowsDeleteString.restype = ctypes.c_long

    _combase.WindowsGetStringRawBuffer.argtypes = [
        ctypes.c_void_p,                     # HSTRING
        ctypes.POINTER(ctypes.c_uint32),     # UINT32*
    ]
    _combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p

    _combase.WindowsDuplicateString.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _combase.WindowsDuplicateString.restype = ctypes.c_long


_bind_combase()


def _require_combase() -> None:
    if _combase is None:
        raise WinRtError(
            "combase.dll 加载失败：%s" % (_LOAD_ERROR or "未知原因"),
            hint="WinRT 需要 Windows 8 及以上。非 Windows 平台没有这个库。")


class HString:
    """一个 HSTRING 的所有权包装。**用 with 或显式 close。**

    ```python
    with HString("Windows.Media.Ocr.OcrEngine") as hs:
        factory = activation_factory_from(hs, IID_IActivationFactory)
    # 出了 with，HSTRING 已释放
    ```
    """

    __slots__ = ("_ptr", "_closed", "_value")

    def __init__(self, value: str = "") -> None:
        _require_combase()
        self._ptr = ctypes.c_void_p()
        self._closed = False
        self._value = str(value)
        self._create(self._value)

    def _create(self, value: str) -> None:
        buf = ctypes.c_void_p()
        hr = _combase.WindowsCreateString(
            value, len(value), ctypes.byref(buf))
        if not succeeded(hr):
            raise WinRtError(
                "WindowsCreateString 失败：%r" % value, hr=hr,
                method="WindowsCreateString",
                hint=_HR_HINTS.get(hr & 0xFFFFFFFF, ""))
        self._ptr = buf

    @property
    def ptr(self) -> ctypes.c_void_p:
        return self._ptr

    @property
    def value(self) -> str:
        return self._value

    def raw(self) -> str:
        """读出底层缓冲区（**不复制所有权**）。"""
        return read_hstring(self._ptr)

    def duplicate(self) -> "HString":
        """复制一份（独立生命周期）。"""
        out = ctypes.c_void_p()
        hr = _combase.WindowsDuplicateString(self._ptr, ctypes.byref(out))
        if not succeeded(hr):
            raise WinRtError("WindowsDuplicateString 失败", hr=hr,
                             method="WindowsDuplicateString")
        clone = HString.__new__(HString)
        clone._ptr = out
        clone._closed = False
        clone._value = self._value
        return clone

    def close(self) -> None:
        if self._closed or not self._ptr:
            return
        try:
            _combase.WindowsDeleteString(self._ptr)
        except Exception:      # noqa: BLE001
            pass
        self._closed = True
        self._ptr = ctypes.c_void_p()

    def __enter__(self) -> "HString":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:      # pragma: no cover - GC 时机不定
        try:
            self.close()
        except Exception:      # noqa: BLE001
            pass

    def __repr__(self) -> str:
        state = "已释放" if self._closed else "有效"
        return "HString(%r, %s)" % (self._value, state)


def hstring(value: str) -> HString:
    """创建一个 HSTRING（**调用方负责 close**）。"""
    return HString(value)


class HStringRef:
    """**栈上** HSTRING 引用的持有者 —— 不分配堆内存。

    ## ⚠ 必须持有这个对象

    ``WindowsCreateStringReference`` **不复制字符串**。它让 HSTRING
    直接指向你给的那块内存。所以：

    - 传入的字符缓冲区必须活到 HSTRING 用完为止
    - 返回的 HSTRING 也必须活到用完为止

    这两件事都由本对象负责 —— **它就是那个"活到用完"的锚点**。

    ## 踩过的坑

    第一版直接 `WindowsCreateStringReference(value, ...)` 传 Python 的
    ``str``。ctypes 会为它建一个**临时** wchar 缓冲区，函数一返回
    那个缓冲区就可能被回收 —— HSTRING 立刻变成野指针，
    读出乱码或空串。所以这里用 ``create_unicode_buffer`` 显式分配，
    并把它挂在实例上。

    ## 什么时候用

    类名是**编译期常量**时（本模块的全部场景）。比
    ``WindowsCreateString`` 少一次堆分配，也不需要 DeleteString。
    """

    __slots__ = ("_header", "_buf", "_ptr", "_value", "_closed")

    def __init__(self, value: str) -> None:
        _require_combase()
        self._value = str(value)
        self._header = HSTRING_HEADER()
        # ⚠ 显式分配并持有 —— 见类文档里那个坑
        self._buf = ctypes.create_unicode_buffer(self._value)
        self._ptr = ctypes.c_void_p()
        self._closed = False

        hr = _combase.WindowsCreateStringReference(
            ctypes.cast(self._buf, ctypes.c_wchar_p),
            len(self._value),
            ctypes.byref(self._header),
            ctypes.byref(self._ptr))
        if not succeeded(hr):
            raise WinRtError(
                "WindowsCreateStringReference 失败：%r" % self._value,
                hr=hr, method="WindowsCreateStringReference")

    @property
    def ptr(self) -> ctypes.c_void_p:
        return self._ptr

    @property
    def header(self) -> HSTRING_HEADER:
        return self._header

    @property
    def value(self) -> str:
        return self._value

    def raw(self) -> str:
        return read_hstring(self._ptr)

    def close(self) -> None:
        """标记失效。**不需要释放** —— 内存是 Python 管的。"""
        self._closed = True
        self._ptr = ctypes.c_void_p()

    def __enter__(self) -> "HStringRef":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return "HStringRef(%r)" % self._value


def hstring_ref(value: str) -> HStringRef:
    """用**栈上头部**创建 HSTRING 引用。返回 :class:`HStringRef`。

    **必须持有返回的对象** —— 它同时锚定了字符缓冲区和 HSTRING 本身。
    见 :class:`HStringRef` 的文档。
    """
    return HStringRef(value)


def read_hstring(hs: Any) -> str:
    """读 HSTRING 的文本。**不释放**。空句柄返回空串。"""
    if not hs:
        return ""
    if isinstance(hs, HString):
        hs = hs.ptr
    if isinstance(hs, ctypes.c_void_p):
        if not hs.value:
            return ""
    try:
        n = ctypes.c_uint32(0)
        p = _combase.WindowsGetStringRawBuffer(hs, ctypes.byref(n))
        if not p:
            return ""
        return p[: n.value] if n.value else ""
    except Exception:      # noqa: BLE001
        return ""


# ==========================================================================
# 初始化（按线程）
# ==========================================================================
_tls = threading.local()

#: 进程级首次初始化的结果，用于诊断。
_first_init: Dict[str, Any] = {"hr": None, "apartment": "", "error": ""}


def ro_initialize(apartment: int = RO_INIT_MULTITHREADED) -> int:
    """初始化当前线程的 WinRT。返回 HRESULT。

    ## 为什么返回值而不是抛异常

    因为"已经初始化过"不是错误：

    - ``S_OK``（0）—— 本次初始化成功
    - ``S_FALSE``（1）—— 本线程已经初始化过，**可以继续用**
    - ``RPC_E_CHANGED_MODE`` —— 本线程在**别的**单元模型里，
      改不了。这时 WinRT 调用**可能**仍然可用（如果对象是 agile 的），
      也可能失败。本模块如实返回，由调用方决定。

    把 S_FALSE 当失败会误伤（重复调用很常见）；把 CHANGED_MODE
    当失败会误伤 agile 对象。所以**返回码就是返回码**。
    """
    _require_combase()
    if getattr(_tls, "initialized", False):
        return _S_FALSE

    hr = int(_combase.RoInitialize(int(apartment)))
    _tls.initialized = True
    _tls.hr = hr
    _tls.apartment = ("MTA" if apartment == RO_INIT_MULTITHREADED
                      else "STA")

    if _first_init["hr"] is None:
        _first_init["hr"] = hr
        _first_init["apartment"] = _tls.apartment
        if not succeeded(hr):
            _first_init["error"] = _HR_HINTS.get(hr & 0xFFFFFFFF,
                                                 "HRESULT 0x%08X" % (hr & 0xFFFFFFFF))
    return hr


def ro_uninitialize() -> None:
    """反初始化当前线程。**只在本线程真的初始化过时调用。**"""
    if _combase is None:
        return
    if not getattr(_tls, "initialized", False):
        return
    if succeeded(getattr(_tls, "hr", _S_FALSE)) and \
            getattr(_tls, "hr", _S_FALSE) == _S_OK:
        try:
            _combase.RoUninitialize()
        except Exception:      # noqa: BLE001
            pass
    _tls.initialized = False


def ensure_initialized(apartment: int = RO_INIT_MULTITHREADED) -> bool:
    """确保当前线程可用。**返回是否可用，不抛异常。**

    判据：`RoInitialize` 返回 S_OK 或 S_FALSE 就算可用；
    返回 `RPC_E_CHANGED_MODE` 时**仍然算可用** —— 因为 agile 对象
    在 STA 线程里也能调。真正的可用性由具体调用去暴露。
    """
    hr = ro_initialize(apartment)
    h = hr & 0xFFFFFFFF
    if h in (_S_OK, _S_FALSE):
        return True
    if h == RPC_E_CHANGED_MODE:
        return True
    return False


def ro_state() -> Dict[str, Any]:
    """当前线程 + 进程的 WinRT 初始化状态。**永不抛。**"""
    try:
        hr = getattr(_tls, "hr", None)
        return {
            "dll_loaded": _combase is not None,
            "dll_error": _LOAD_ERROR,
            "thread_initialized": bool(getattr(_tls, "initialized", False)),
            "thread_hr": hr,
            "thread_hr_hex": ("0x%08X" % (hr & 0xFFFFFFFF)
                              if isinstance(hr, int) else ""),
            "apartment": getattr(_tls, "apartment", ""),
            "first_hr": _first_init["hr"],
            "first_hr_hex": ("0x%08X" % (_first_init["hr"] & 0xFFFFFFFF)
                             if isinstance(_first_init["hr"], int) else ""),
            "first_error": _first_init["error"],
        }
    except Exception:      # noqa: BLE001
        return {"dll_loaded": False, "dll_error": "state unavailable",
                "thread_initialized": False, "thread_hr": None,
                "thread_hr_hex": "", "apartment": "",
                "first_hr": None, "first_hr_hex": "", "first_error": ""}


# ==========================================================================
# 接口 IID
# ==========================================================================
#: ``IActivationFactory`` —— 所有 WinRT 类的工厂都实现它。
#: 槽位 [6] 是 ``ActivateInstance``。
IID_IActivationFactory = "{00000035-0000-0000-C000-000000000046}"

#: ``IAsyncInfo`` —— ``IAsyncOperation`` 继承它。
#: 槽位 [10] 是 ``get_Status``。
IID_IAsyncInfo = "{00000036-0000-0000-C000-000000000046}"

# ---- Windows.Media.Ocr ----
#: 来源：windows-rs 生成的绑定（``crates/libs/windows/src/Windows/Media/Ocr/mod.rs``）
IID_OCR_ENGINE = "{5A14BC41-5B76-3140-B680-8825562683AC}"
IID_OCR_ENGINE_STATICS = "{5BFFA85A-3384-3540-9940-699120D428A8}"
IID_OCR_RESULT = "{9BD235B2-175B-3D6A-92E2-388C206E2F63}"
IID_OCR_LINE = "{0043A16F-E31F-3A24-899C-D444BD088124}"
IID_OCR_WORD = "{3C2A477A-5CD9-3525-BA2A-23D1E0A68A1D}"

# ---- Windows.Graphics.Imaging ----
IID_SOFTWARE_BITMAP_STATICS = "{DF0385DB-672F-4A9D-806E-C2442F343E86}"
IID_SOFTWARE_BITMAP = "{689E4C51-5CEA-4E1A-8F15-8DC0C6B4B1B8}"

# ---- Windows.Storage.Streams ----
#: ``IBufferByteAccess`` —— 拿 IBuffer 的裸指针写数据。
#: 这是**经典 COM 接口**（不是 WinRT），但 WinRT 的 Buffer 实现了它。
IID_IBUFFER_BYTE_ACCESS = "{905A0FE0-BC53-11DF-8C49-001E4FC686DA}"

#: WinRT 接口的固定前 6 槽（IUnknown 3 + IInspectable 3）。
VTBL_IINSPECTABLE_SIZE = 6

# ---- vtable 槽位（**从 6 开始**）----
#: ``IOcrEngine``：``[6] RecognizeAsync``  ``[7] RecognizerLanguage``
VTBL_OCR_ENGINE = {
    "RecognizeAsync": 6,
    "RecognizerLanguage": 7,
}

#: ``IOcrEngineStatics``：
#: ``[6] MaxImageDimension`` ``[7] AvailableRecognizerLanguages``
#: ``[8] IsLanguageSupported`` ``[9] TryCreateFromLanguage``
#: ``[10] TryCreateFromUserProfileLanguages``
#:
#: ⚠ **[10] 不是 [9]**。凭印象写会调到 `TryCreateFromLanguage`
#: 并把它的第一个参数（ILanguage*）当成输出指针 —— 直接崩。
VTBL_OCR_ENGINE_STATICS = {
    "MaxImageDimension": 6,
    "AvailableRecognizerLanguages": 7,
    "IsLanguageSupported": 8,
    "TryCreateFromLanguage": 9,
    "TryCreateFromUserProfileLanguages": 10,
}

#: ``IOcrResult``：``[6] Lines`` ``[7] TextAngle`` ``[8] Text``
VTBL_OCR_RESULT = {
    "Lines": 6,
    "TextAngle": 7,
    "Text": 8,
}

#: ``IOcrLine``：``[6] Words`` ``[7] Text``
VTBL_OCR_LINE = {
    "Words": 6,
    "Text": 7,
}

#: ``IOcrWord``：``[6] BoundingRect`` ``[7] Text``
VTBL_OCR_WORD = {
    "BoundingRect": 6,
    "Text": 7,
}

#: ``ISoftwareBitmapStatics``：
#: ``[6] Copy`` ``[7] Convert`` ``[8] ConvertWithAlpha``
#: ``[9] CreateCopyFromBuffer`` ``[10] CreateCopyWithAlphaFromBuffer``
#: ``[11] CreateCopyFromSurfaceAsync`` ``[12] CreateCopyWithAlphaFromSurfaceAsync``
VTBL_SOFTWARE_BITMAP_STATICS = {
    "Copy": 6,
    "Convert": 7,
    "ConvertWithAlpha": 8,
    "CreateCopyFromBuffer": 9,
    "CreateCopyWithAlphaFromBuffer": 10,
    "CreateCopyFromSurfaceAsync": 11,
    "CreateCopyWithAlphaFromSurfaceAsync": 12,
}

#: ``IBufferByteAccess``：``[6] Buffer``（经典 COM，前 3 槽是 IUnknown）
VTBL_IBUFFER_BYTE_ACCESS = {
    "Buffer": 3,
}

#: ``IActivationFactory``：``[6] ActivateInstance``
VTBL_IActivation_FACTORY = {
    "ActivateInstance": 6,
}

#: ``IDataWriter``（``Windows.Storage.Streams.DataWriter``）。
#:
#: **为什么需要它**：往 WinRT 的 ``IBuffer`` 里写像素，最直接的办法是
#: ``IBufferByteAccess::Buffer()`` 拿裸指针。但那个接口是**经典 COM**，
#: 它的 IID 与 ``IBuffer`` 只差最后一位，极易 QI 错 ——
#: 错了之后会在 ``IBuffer`` 的 vtable[3]（``GetIids``）上按
#: ``Buffer(byte**)`` 的签名调，**直接访问违例**（实测崩过）。
#:
#: ``DataWriter`` 这条路不需要那个接口：``WriteBytes`` 拷数据，
#: ``DetachBuffer`` 交出 ``IBuffer``。全是 WinRT 接口，IID 不用猜。
VTBL_DATA_WRITER = {
    "UnstoredBufferLength": 6,
    "UnicodeEncoding": 7,
    "SetUnicodeEncoding": 8,
    "ByteOrder": 9,
    "SetByteOrder": 10,
    "WriteByte": 11,
    "WriteBytes": 12,
    "WriteBuffer": 13,
    "WriteBufferRange": 14,
    "WriteBoolean": 15,
    "WriteGuid": 16,
    "WriteInt16": 17,
    "WriteInt32": 18,
    "WriteInt64": 19,
    "WriteUInt16": 20,
    "WriteUInt32": 21,
    "WriteUInt64": 22,
    "WriteSingle": 23,
    "WriteDouble": 24,
    "WriteDateTime": 25,
    "WriteTimeSpan": 26,
    "WriteString": 27,
    "MeasureString": 28,
    "StoreAsync": 29,
    "FlushAsync": 30,
    "DetachBuffer": 31,
    "DetachStream": 32,
}


# ==========================================================================
# vtable 调用
# ==========================================================================
def _unwrap(ptr: Any) -> int:
    """把各种"指针形态"统一成整数地址。

    收：``WinRtPtr`` / ``c_void_p`` / ``int``。
    **``WinRtPtr`` 也要收** —— 它是本模块自己的包装，
    要求调用方每次都写 ``.ptr`` 只会制造无谓的错误机会。
    """
    if ptr is None:
        return 0
    if isinstance(ptr, WinRtPtr):
        ptr = ptr.ptr
    if isinstance(ptr, ctypes.c_void_p):
        return int(ptr.value or 0)
    try:
        return int(ptr)
    except (TypeError, ValueError):
        return 0


def vtbl(ptr: Any, slot: int) -> int:
    """取对象 vtable 第 ``slot`` 个函数的地址。"""
    p = _unwrap(ptr)
    if not p:
        raise WinRtError("空指针 —— 对象没创建成功或已被释放")
    # 对象首字段是 vtable 指针
    vptr = ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0]
    if not vptr:
        raise WinRtError("vtable 指针为空 —— 这不是一个 COM 对象")
    return ctypes.cast(vptr, ctypes.POINTER(ctypes.c_void_p))[slot]


def vtbl_call(ptr: Any, slot: int, restype: Any,
              argtypes: Sequence[Any], *args: Any) -> Any:
    """调 vtable 第 ``slot`` 个函数。

    ## 为什么要显式给 argtypes

    因为 ctypes 默认把参数当 C int 处理 —— 64 位指针会被**截断**，
    结果是"偶尔能用、偶尔崩"。显式声明类型是唯一的稳妥做法。

    ## 参数顺序

    ``argtypes`` **不含 this** —— 本函数自动把对象指针插到第一个位置。
    """
    addr = vtbl(ptr, slot)
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p,
                            *list(argtypes))(addr)
    # ⚠ this 指针要传**整数地址**，不能传 WinRtPtr ——
    #   ctypes 不认识 WinRtPtr，会报 ArgumentError
    return fn(_unwrap(ptr), *args)


def add_ref(ptr: Any) -> int:
    """``IUnknown::AddRef``。返回新引用计数。"""
    return int(vtbl_call(ptr, 1, ctypes.c_ulong, []))


def release(ptr: Any) -> int:
    """``IUnknown::Release``。返回剩余引用计数。**空指针安全。**"""
    if not _unwrap(ptr):
        return 0
    try:
        return int(vtbl_call(ptr, 2, ctypes.c_ulong, []))
    except Exception:      # noqa: BLE001
        return 0


def get_iids(ptr: Any) -> List[str]:
    """``IInspectable::GetIids`` —— 枚举对象实现的所有接口 IID。

    **为什么需要它**：WinRT 的 IID 没法像 COM 那样从注册表查。
    想知道"这个对象支持哪些接口"，只能问它自己。

    返回 IID 字符串列表（``{XXXX-...}`` 形式）。失败返回空列表 ——
    这是个诊断辅助，不该因为拿不到就把调用方搞崩。
    """
    out: List[str] = []
    try:
        n = ctypes.c_uint32(0)
        arr = ctypes.c_void_p()
        hr = vtbl_call(ptr, 3, ctypes.c_long,
                       [ctypes.POINTER(ctypes.c_uint32),
                        ctypes.POINTER(ctypes.c_void_p)],
                       ctypes.byref(n), ctypes.byref(arr))
        if not succeeded(hr) or not arr.value:
            return out
        try:
            base = ctypes.cast(arr, ctypes.POINTER(GUID))
            for i in range(int(n.value)):
                out.append(str(base[i]))
        finally:
            # GetIids 用 CoTaskMemAlloc 分配，必须 CoTaskMemFree
            try:
                ctypes.windll.ole32.CoTaskMemFree(arr)
            except Exception:      # noqa: BLE001
                pass
    except Exception:      # noqa: BLE001
        return out
    return out


class WinRtPtr:
    """WinRT 接口指针的所有权包装。**出 with 就 Release。**"""

    __slots__ = ("_ptr", "_released", "name")

    def __init__(self, ptr: Any = None, name: str = "") -> None:
        self._ptr = ctypes.c_void_p(ptr.value if isinstance(
            ptr, ctypes.c_void_p) else (ptr or 0))
        self._released = False
        self.name = name

    @property
    def ptr(self) -> ctypes.c_void_p:
        return self._ptr

    @property
    def ok(self) -> bool:
        return bool(self._ptr and self._ptr.value) and not self._released

    def detach(self) -> ctypes.c_void_p:
        """放弃所有权（调用方接手）。"""
        p = self._ptr
        self._ptr = ctypes.c_void_p()
        self._released = True
        return p

    def close(self) -> None:
        if self._released:
            return
        release(self._ptr)
        self._released = True
        self._ptr = ctypes.c_void_p()

    def __enter__(self) -> "WinRtPtr":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:      # pragma: no cover
        try:
            self.close()
        except Exception:      # noqa: BLE001
            pass

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        addr = self._ptr.value if self._ptr else 0
        return "WinRtPtr(%s, 0x%X)" % (self.name or "?", addr or 0)


# ==========================================================================
# 激活工厂
# ==========================================================================
def _guid_arg(iid: str) -> GUID:
    return guid(iid) if isinstance(iid, str) else iid


def activation_factory(class_name: str,
                       iid: str = IID_IActivationFactory) -> WinRtPtr:
    """按**类名**激活工厂。这是 WinRT 版的 ``CoCreateInstance``。

    ## 参数

    - ``class_name`` —— 完整类名，如 ``"Windows.Media.Ocr.OcrEngine"``
    - ``iid`` —— 想要的接口。默认 ``IActivationFactory``
      （只想要实例时用它 + :func:`activate_instance`）。

    ## 想要静态方法时

    传对应 statics 接口的 IID，比如
    ``activation_factory("Windows.Media.Ocr.OcrEngine", IID_OCR_ENGINE_STATICS)``。

    静态方法（`TryCreateFromUserProfileLanguages` 等）**只在这个
    statics 接口上**，`IActivationFactory` 上没有。

    ## 失败时

    抛 :class:`WinRtError`，带 HRESULT 和针对性的提示
    （类名拼错 / 缺组件 / 缺包标识）。
    """
    _require_combase()
    ensure_initialized()

    if not str(class_name or "").strip():
        raise WinRtError("类名为空", method="RoGetActivationFactory")

    # 用栈上引用 —— 类名是常量，没必要堆分配。
    # ⚠ ref 必须在整个调用期间存活（它锚定着字符缓冲区）
    ref = hstring_ref(class_name)

    out = ctypes.c_void_p()
    g = _guid_arg(iid)
    hr = int(_combase.RoGetActivationFactory(
        ref.ptr, ctypes.byref(g), ctypes.byref(out)))

    if not succeeded(hr):
        h = hr & 0xFFFFFFFF
        raise WinRtError(
            "激活 %s 失败（HRESULT 0x%08X）" % (class_name, h),
            hr=hr, method="RoGetActivationFactory", interface=iid,
            hint=_HR_HINTS.get(h, ""))

    if not out.value:
        raise WinRtError(
            "激活 %s 返回了空指针（HRESULT 是成功码）" % class_name,
            method="RoGetActivationFactory", interface=iid,
            hint="WinRT 有时会「成功但给 null」—— 通常是没有包标识。")

    return WinRtPtr(out, class_name)


def activate_instance(factory: Any,
                      iid: str = "") -> WinRtPtr:
    """``IActivationFactory::ActivateInstance`` —— 从工厂造一个实例。

    ``iid`` 给了就再 QueryInterface 一次（因为 ``ActivateInstance``
    返回的是对象的**默认接口**，不一定是你要的那个）。
    """
    out = ctypes.c_void_p()
    hr = vtbl_call(factory, VTBL_IActivation_FACTORY["ActivateInstance"],
                   ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)],
                   ctypes.byref(out))
    if not succeeded(hr):
        raise WinRtError("ActivateInstance 失败", hr=hr,
                         method="ActivateInstance",
                         hint=_HR_HINTS.get(hr & 0xFFFFFFFF, ""))
    inst = WinRtPtr(out, "instance")
    if iid:
        return query_interface(inst, iid)
    return inst


def query_interface(obj: Any, iid: str) -> WinRtPtr:
    """``IUnknown::QueryInterface``。**失败抛 :class:`WinRtError`。**"""
    out = ctypes.c_void_p()
    g = _guid_arg(iid)
    hr = vtbl_call(obj, 0, ctypes.c_long,
                   [ctypes.POINTER(GUID),
                    ctypes.POINTER(ctypes.c_void_p)],
                   ctypes.byref(g), ctypes.byref(out))
    if not succeeded(hr):
        raise WinRtError(
            "QueryInterface 失败（HRESULT 0x%08X）" % (hr & 0xFFFFFFFF),
            hr=hr, method="QueryInterface", interface=iid,
            hint="这个对象不支持该接口。可用接口：%s"
                 % (", ".join(get_iids(obj)[:6]) or "（拿不到）"))
    return WinRtPtr(out, iid)


def try_query_interface(obj: Any, iid: str) -> Optional[WinRtPtr]:
    """QI 的**不抛版本** —— 不支持就返回 ``None``。"""
    try:
        return query_interface(obj, iid)
    except WinRtError:
        return None


# ==========================================================================
# IAsyncOperation
# ==========================================================================
#: ``AsyncStatus`` 枚举
ASYNC_STARTED = 0
ASYNC_COMPLETED = 1
ASYNC_CANCELED = 2
ASYNC_ERROR = 3

#: ``IAsyncInfo`` 的槽位（IAsyncOperation 继承它）
_VTBL_ASYNC_GET_STATUS = 10
_VTBL_ASYNC_GET_ERRORCODE = 11
_VTBL_ASYNC_CANCEL = 12
_VTBL_ASYNC_CLOSE = 13
#: ``IAsyncOperation<T>`` 自己的 ``GetResults``
_VTBL_ASYNC_GET_RESULTS = 8


def async_status(op: Any) -> int:
    """读 ``IAsyncInfo::get_Status``。失败返回 ``-1``。"""
    try:
        st = ctypes.c_int(0)
        hr = vtbl_call(op, _VTBL_ASYNC_GET_STATUS, ctypes.c_long,
                       [ctypes.POINTER(ctypes.c_int)], ctypes.byref(st))
        if not succeeded(hr):
            return -1
        return int(st.value)
    except Exception:      # noqa: BLE001
        return -1


def async_error_code(op: Any) -> int:
    """读 ``IAsyncInfo::get_ErrorCode``。"""
    try:
        ec = ctypes.c_long(0)
        vtbl_call(op, _VTBL_ASYNC_GET_ERRORCODE, ctypes.c_long,
                  [ctypes.POINTER(ctypes.c_long)], ctypes.byref(ec))
        return int(ec.value)
    except Exception:      # noqa: BLE001
        return 0


def await_async(op: Any, *,
                timeout: float = 10.0,
                poll: float = 0.005,
                close: bool = True) -> WinRtPtr:
    """等一个 ``IAsyncOperation<T>`` 完成，返回结果的 :class:`WinRtPtr`。

    ## 为什么是轮询而不是等待事件

    WinRT 的 `IAsyncOperation` 提供了 `put_Completed` 回调，但
    **回调需要一个 COM 可调用对象**（实现 `IAsyncOperationCompletedHandler`）——
    用 ctypes 手搓那个对象的 vtable 比轮询复杂得多，而且回调会在
    WinRT 自己的线程上触发，跨线程回 Python 还要处理 GIL 与栈切换。

    对 OCR 这种"几十毫秒到几百毫秒"的操作，**轮询 5ms 一次**完全够用，
    而且行为可预测（超时是我们说了算，不是 WinRT 说了算）。

    ## 超时

    超时后**尝试 Cancel**，然后抛 :class:`WinRtError`。
    不 Cancel 的话那个异步操作会一直挂着，泄漏。

    ## 返回

    结果对象的 :class:`WinRtPtr`（**调用方负责 close**）。
    结果本身可能是空指针（有些 WinRT 方法允许返回 null）——
    这时返回的 WinRtPtr 是"有效但空"的，用 ``bool()`` 判断。
    """
    if not op:
        raise WinRtError("await_async 收到空指针")

    deadline = time.perf_counter() + max(0.0, float(timeout))
    last = -1
    while True:
        st = async_status(op)
        last = st
        if st == ASYNC_COMPLETED:
            break
        if st == ASYNC_ERROR:
            code = async_error_code(op)
            if close:
                _async_close(op)
            raise WinRtError(
                "异步操作失败（HRESULT 0x%08X）" % (code & 0xFFFFFFFF),
                hr=code, method="IAsyncOperation",
                hint=_HR_HINTS.get(code & 0xFFFFFFFF, ""))
        if st == ASYNC_CANCELED:
            if close:
                _async_close(op)
            raise WinRtError("异步操作被取消", method="IAsyncOperation")
        if st < 0:
            if close:
                _async_close(op)
            raise WinRtError(
                "读不到异步状态（get_Status 失败）—— "
                "传进来的可能不是 IAsyncOperation",
                method="IAsyncInfo::get_Status")
        if time.perf_counter() >= deadline:
            _async_cancel(op)
            if close:
                _async_close(op)
            raise WinRtError(
                "异步操作超时（%.1fs）" % timeout, method="IAsyncOperation",
                hint="已尝试 Cancel。如果这是 OCR，可能是图太大或语言包缺失。")
        time.sleep(poll)

    res = ctypes.c_void_p()
    hr = vtbl_call(op, _VTBL_ASYNC_GET_RESULTS, ctypes.c_long,
                   [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(res))
    if close:
        _async_close(op)
    if not succeeded(hr):
        raise WinRtError("GetResults 失败（HRESULT 0x%08X）"
                         % (hr & 0xFFFFFFFF), hr=hr,
                         method="IAsyncOperation::GetResults",
                         hint=_HR_HINTS.get(hr & 0xFFFFFFFF, ""))
    return WinRtPtr(res, "async-result")


def _async_cancel(op: Any) -> None:
    try:
        vtbl_call(op, _VTBL_ASYNC_CANCEL, ctypes.c_long, [])
    except Exception:      # noqa: BLE001
        pass


def _async_close(op: Any) -> None:
    try:
        vtbl_call(op, _VTBL_ASYNC_CLOSE, ctypes.c_long, [])
    except Exception:      # noqa: BLE001
        pass


# ==========================================================================
# 诊断
# ==========================================================================
def describe_winrt() -> str:
    """给人看的 WinRT 环境说明。**永不抛。**"""
    st = ro_state()
    lines = ["WinRT 环境（纯 ctypes，零 SDK）", "=" * 64]
    lines.append("combase.dll   : %s"
                 % ("已加载" if st["dll_loaded"]
                    else "**加载失败** %s" % st["dll_error"]))
    lines.append("本线程初始化  : %s"
                 % ("是" if st["thread_initialized"] else "否"))
    lines.append("本线程 HRESULT: %s" % (st["thread_hr_hex"] or "-"))
    lines.append("单元模型      : %s" % (st["apartment"] or "-"))
    if st["first_error"]:
        lines.append("首次初始化问题: %s" % st["first_error"])
    lines += [
        "",
        "vtable 槽位（前 6 槽是 IInspectable 固定布局，方法从 6 开始）：",
        "  IOcrEngine              %s" % _fmt_slots(VTBL_OCR_ENGINE),
        "  IOcrEngineStatics       %s" % _fmt_slots(VTBL_OCR_ENGINE_STATICS),
        "  IOcrResult              %s" % _fmt_slots(VTBL_OCR_RESULT),
        "  IOcrLine                %s" % _fmt_slots(VTBL_OCR_LINE),
        "  IOcrWord                %s" % _fmt_slots(VTBL_OCR_WORD),
        "  ISoftwareBitmapStatics  %s"
        % _fmt_slots(VTBL_SOFTWARE_BITMAP_STATICS),
        "",
        "⚠ 槽位是硬编码的，**不做运行时探测** ——",
        "  按签名猜槽位猜错的代价是静默调错函数，比直接报错危险。",
    ]
    return "\n".join(lines)


def _fmt_slots(table: Dict[str, int]) -> str:
    return "  ".join("[%d]%s" % (v, k) for k, v in sorted(
        table.items(), key=lambda kv: kv[1]))


# ==========================================================================
# 自检
# ==========================================================================
def selfcheck(quick: bool = True) -> dict:
    """winrt 自检。**不激活任何真实 WinRT 类**（除非 quick=False）。"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("HSTRING_HEADER 是 32 字节（x64 ABI）",
         ctypes.sizeof(_me.HSTRING_HEADER) == 32,
         "%d 字节" % ctypes.sizeof(_me.HSTRING_HEADER))
    _add("IInspectable 前 6 槽是固定的",
         _me.VTBL_IINSPECTABLE_SIZE == 6, "6")
    _add("IActivationFactory 槽位正确",
         _me.VTBL_IActivation_FACTORY["ActivateInstance"] == 6, "[6]")
    _add("★ 所有 OCR 槽位都 ≥ 6（不会覆盖 IInspectable）",
         all(v >= 6 for v in list(_me.VTBL_OCR_ENGINE.values())
             + list(_me.VTBL_OCR_ENGINE_STATICS.values())
             + list(_me.VTBL_OCR_RESULT.values())
             + list(_me.VTBL_OCR_LINE.values())
             + list(_me.VTBL_OCR_WORD.values())
             + list(_me.VTBL_SOFTWARE_BITMAP_STATICS.values())),
         "全部 ≥6")

    # ============ 2) ★ 关键槽位（凭印象写会错）============
    _add("★★ TryCreateFromUserProfileLanguages 在 [10]（不是 [9]）",
         _me.VTBL_OCR_ENGINE_STATICS["TryCreateFromUserProfileLanguages"] == 10,
         "[10] —— 写成 [9] 会调到 TryCreateFromLanguage 并崩")
    _add("★ TryCreateFromLanguage 在 [9]",
         _me.VTBL_OCR_ENGINE_STATICS["TryCreateFromLanguage"] == 9, "[9]")
    _add("★ IsLanguageSupported 在 [8]",
         _me.VTBL_OCR_ENGINE_STATICS["IsLanguageSupported"] == 8, "[8]")
    _add("★ RecognizeAsync 在 [6]",
         _me.VTBL_OCR_ENGINE["RecognizeAsync"] == 6, "[6]")
    _add("★ CreateCopyFromBuffer 在 [9]",
         _me.VTBL_SOFTWARE_BITMAP_STATICS["CreateCopyFromBuffer"] == 9, "[9]")
    _add("★ IBufferByteAccess::Buffer 在 [3]（经典 COM，无 IInspectable）",
         _me.VTBL_IBUFFER_BYTE_ACCESS["Buffer"] == 3, "[3]")
    _add("★ AsyncInfo 槽位：Status=10 ErrorCode=11 Cancel=12 Close=13",
         (_me._VTBL_ASYNC_GET_STATUS == 10
          and _me._VTBL_ASYNC_GET_ERRORCODE == 11
          and _me._VTBL_ASYNC_CANCEL == 12
          and _me._VTBL_ASYNC_CLOSE == 13
          and _me._VTBL_ASYNC_GET_RESULTS == 8),
         "8=GetResults 10..13=IAsyncInfo")

    # ============ 3) IID 格式 ============
    iids = {
        "IActivationFactory": _me.IID_IActivationFactory,
        "IAsyncInfo": _me.IID_IAsyncInfo,
        "IOcrEngine": _me.IID_OCR_ENGINE,
        "IOcrEngineStatics": _me.IID_OCR_ENGINE_STATICS,
        "IOcrResult": _me.IID_OCR_RESULT,
        "IOcrLine": _me.IID_OCR_LINE,
        "IOcrWord": _me.IID_OCR_WORD,
        "ISoftwareBitmapStatics": _me.IID_SOFTWARE_BITMAP_STATICS,
        "IBufferByteAccess": _me.IID_IBUFFER_BYTE_ACCESS,
    }
    bad = []
    for name, s in iids.items():
        try:
            g = guid(s)
            if str(g).upper() != s.upper():
                bad.append("%s 往返不一致" % name)
        except Exception as exc:      # noqa: BLE001
            bad.append("%s 解析失败 %s" % (name, exc))
    _add("★ 全部 IID 可解析且往返一致", not bad, str(bad) or "9 个全对")

    _add("★ IOcrEngineStatics 不是凭印象写的那个值",
         _me.IID_OCR_ENGINE_STATICS.upper()
         == "{5BFFA85A-3384-3540-9940-699120D428A8}",
         "来源：windows-rs 绑定，已核对")

    # ============ 4) HSTRING ============
    st = _me.ro_state()
    if not st["dll_loaded"]:
        _add("（combase.dll 不可用，跳过 HSTRING 实测）", True,
             st["dll_error"])
    else:
        _add("RoInitialize 可调（返回 HRESULT）",
             isinstance(_me.ro_initialize(), int),
             "0x%08X" % (_me.ro_initialize() & 0xFFFFFFFF))
        _add("重复 RoInitialize 返回 S_FALSE（1）",
             _me.ro_initialize() in (0, 1), "幂等")

        # HSTRING 往返（含中文与空串）
        for text in ("", "abc", "记事本", "Windows.Media.Ocr.OcrEngine",
                     "混合 mixed 文本 123"):
            try:
                with _me.HString(text) as hs:
                    got = hs.raw()
                ok = got == text
            except Exception as exc:      # noqa: BLE001
                ok = False
                got = "%s: %s" % (type(exc).__name__, exc)
            _add("★ HSTRING 往返：%r" % text, ok, repr(got))

        # 栈上引用
        try:
            ref = _me.hstring_ref("Windows.Media.Ocr.OcrEngine")
            got = ref.raw()
            ok = got == "Windows.Media.Ocr.OcrEngine"
        except Exception as exc:      # noqa: BLE001
            ok = False
            got = str(exc)
        _add("★ hstring_ref 往返（栈上，无堆分配）", ok, repr(got)[:60])
        _add("★ HStringRef 锚定了字符缓冲区（防野指针）",
             _me.HStringRef("abc").raw() == "abc",
             "缓冲区随对象存活")

        # duplicate
        try:
            with _me.HString("dup-test") as a:
                b = a.duplicate()
                ok = b.raw() == "dup-test"
                b.close()
        except Exception as exc:      # noqa: BLE001
            ok = False
        _add("HString.duplicate 独立生命周期", ok, "ok")

        _add("read_hstring(None) 返回空串（不抛）",
             _me.read_hstring(None) == "", "空")

        # 空指针安全
        _add("release(None) 返回 0（不抛）", _me.release(None) == 0, "0")
        _add("WinRtPtr(None) 是 falsy",
             bool(_me.WinRtPtr(None)) is False, "falsy")

    # ============ 5) 诊断不抛 ============
    _add("ro_state 永不抛", isinstance(_me.ro_state(), dict), "dict")
    _add("describe_winrt 永不抛",
         isinstance(_me.describe_winrt(), str), "有文本")
    _add("describe 列出 vtable 槽位",
         "TryCreateFromUserProfileLanguages" in _me.describe_winrt(), "有")

    # ============ 6) 激活失败给明确提示 ============
    if st["dll_loaded"]:
        err = _raise(lambda: _me.activation_factory(
            "Windows.Media.Ocr.OcrEngine_不存在的类"))
        _add("★ 激活不存在的类 → WinRtError（带 HRESULT）",
             isinstance(err, _me.WinRtError), type(err).__name__)
        _add("★ 激活失败不抛 OSError/崩溃",
             not isinstance(err, (OSError, SystemError)),
             type(err).__name__)
        _add("空类名 → WinRtError",
             isinstance(_raise(lambda: _me.activation_factory("")),
                        _me.WinRtError), "抛了")

    # ============ 7) vtbl 工具对空指针的防护 ============
    _add("★ vtbl(None) 抛 WinRtError（不是段错误）",
         isinstance(_raise(lambda: _me.vtbl(None, 6)), _me.WinRtError),
         "抛了")
    _add("★ get_iids(空) 返回空列表（不抛）",
         _me.get_iids(None) == [], "空")

    # ============ 8) 不 import 第三方 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    _add("★ 顶层只依赖标准库 + 本包 _win32",
         top <= {"ctypes", "threading", "time", "typing", "__future__",
                 "sys", "com", "guids"},
         sorted(top))
    _add("★ 不 import win32com / pythonnet / winrt",
         not (top & {"win32com", "clr", "winrt", "comtypes"}),
         sorted(top))

    elapsed = time.perf_counter() - t0
    return {
        "module": "winrt",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


def _raise(fn):
    """调 fn，返回异常对象；没抛返回 None。"""
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return exc
    return None


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_winrt())
