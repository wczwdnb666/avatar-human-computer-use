# -*- coding: utf-8 -*-
"""COM 基础设施 —— 纯 ctypes 手搓 vtable 调用。

职责
----
· ``ComPtr``：裸 COM 接口指针的 RAII 封装，按 **vtable 索引** 调用方法
· ``init_com`` / ``uninit_com``：公寓（apartment）初始化，UIA 要求 MTA
· ``create_instance``：``CoCreateInstance`` 封装
· ``check_hresult``：HRESULT 校验，失败抛 ``ComError``
· ``ComError``：携带 hresult / 接口名 / 方法名 / 索引，便于定位

依赖
----
· ctypes（标准库）
· ``avatar._win32.api``（ole32 绑定）
· ``avatar._win32.guids``（IID / CLSID 解析）
· ``avatar._win32.variant``（VARIANT / BSTR，仅类型引用）
· ``avatar.errors``（ComError 基类）

## 为什么手搓 vtable

COM 对象的内存布局是::

    pInterface ──► [ vtable 指针 ] ──► [ fn0 | fn1 | fn2 | ... ]

所以 ``*(void***)p`` 是 vtable 基址，``vtable[i]`` 是第 i 个方法的入口。
IUnknown 固定占 vtable 前 3 个槽位::

    0 : QueryInterface
    1 : AddRef
    2 : Release

因此**业务方法的索引从 3 开始**。本模块的 ``_method(index)`` 接收的是
**绝对索引**，调用方需要自己数 —— 索引表以 ``UIAutomationClient.h``
的方法声明顺序为准，写在 ``uia/patterns.py`` 和各消费模块里。

## 三个踩过的坑

**坑 1 —— 不要用 ``ctypes.WINFUNCTYPE(RESULT)(addr)`` 直接建函数。**
``addr`` 是 ``c_void_p`` 时转换会丢值；必须先 ``int`` 化再 ``cast``。
本模块的 ``_method`` 统一处理。

**坑 2 —— ``firstarg`` 必须是 ``c_void_p``，不能用默认的变参。**
不显式声明时 ctypes 会把 this 指针当 32 位传，x64 下高 32 位丢失 → 崩溃。
所以每个方法都强制声明 ``argtypes[0] = c_void_p``。

**坑 3 —— Release 之后指针必须置 0。**
重复 Release 会造成 use-after-free。本类用 ``_ptr`` 单点持有并置空。
"""
from __future__ import annotations

import atexit
import ctypes
import threading
from ctypes import wintypes

from ..errors import AvatarError
from . import api as A
from .guids import GUID, guid, iid_of

# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class ComError(AvatarError):
    """COM 调用失败。

    携带的 ``hresult`` 是原始 HRESULT（Python int，可能是负数）；
    ``hresult_hex`` 是便于阅读的 ``0xXXXXXXXX`` 形式。
    """

    exit_code = 9

    def __init__(self, message: str, hresult: int = 0, interface: str = "",
                 method: str = "", index: int = -1, **context):
        self.hresult = int(hresult)
        self.hresult_hex = "0x%08X" % (self.hresult & 0xFFFFFFFF)
        self.interface = interface
        self.method = method
        self.index = index
        super().__init__(
            message, hresult=self.hresult_hex, interface=interface or None,
            method=method or None, index=index if index >= 0 else None, **context,
        )

    def __str__(self):
        parts = ["%s" % self.args[0] if self.args else "COM 调用失败"]
        loc = []
        if self.interface:
            loc.append(self.interface)
        if self.method:
            loc.append(self.method)
        if loc:
            parts.append("在 %s" % "::".join(loc))
        parts.append("失败")
        base = " ".join(parts) if len(parts) > 1 else parts[0]
        # 附上 HRESULT 与可读名
        try:
            from .variant import hr_describe
            base += " [%s]" % hr_describe(self.hresult)
        except Exception:
            base += " [%s]" % self.hresult_hex
        if self.index >= 0:
            base += " (vtable[%d])" % self.index
        return base


def _hr_int(hr):
    """把各种形态的 HRESULT 统一成 Python int。取不到返回 None。

    HRESULT 可能是：

    · 纯 Python int（大多数情况）
    · ``HRESULT_SAFE`` 实例 —— 它是 ``ctypes.c_long`` 子类，
      **不能**直接 ``int()``，要先取 ``.value``
    · ``ctypes.c_long`` / ``ctypes.HRESULT`` 实例
    """
    if isinstance(hr, int):
        return hr
    v = getattr(hr, "value", None)
    if isinstance(v, int):
        return v
    try:
        return int(hr)
    except (TypeError, ValueError):
        return None


def check_hresult(hr, what: str = "", interface: str = "", method: str = "",
                  index: int = -1) -> None:
    """校验 HRESULT。失败即抛 ``ComError``。

    这是**唯一**允许的 COM 错误处理方式 —— 任何 ``if hr != 0: pass``
    式的写法都会被 code review 拒绝。

    HRESULT 语义：最高位为 0 = 成功（S_OK/S_FALSE），为 1 = 失败。
    Python 里 ctypes.c_long 返回的是**有符号** int，所以负数即失败；
    但也可能收到无符号形式，故用 ``& 0x80000000`` 判断更稳。
    """
    v = _hr_int(hr)
    if v is None:
        raise ComError("返回值不是整数 HRESULT: %r" % (hr,),
                       interface=interface, method=method)
    if v & 0x80000000:
        raise ComError(
            what or "COM 调用", hresult=v, interface=interface,
            method=method, index=index,
        )


def succeeded(hr) -> bool:
    """不抛异常的版本，用于"探测型"调用。"""
    v = _hr_int(hr)
    if v is None:
        return False
    return not (v & 0x80000000)


# --------------------------------------------------------------------------
# HRESULT 出参类型
# --------------------------------------------------------------------------
class HRESULT_SAFE(ctypes.c_long):
    """**不自动抛异常**的 HRESULT 返回类型。

    ## 为什么不能用 ``ctypes.HRESULT``

    ``ctypes.HRESULT`` 带一个 ``_check_retval_`` 钩子（``_check_HRESULT``），
    ctypes 会在函数返回后**自动检查**：只要 HRESULT 是失败值就抛
    ``OSError: [WinError -2147467262] 不支持此接口``。

    这看起来很贴心，实际是个陷阱：

    · 它**绕过了**本模块的 ``check_hresult`` / ``succeeded`` 契约，
      抛的是 ``OSError`` 而不是 ``ComError``，调用方按 ``ComError``
      捕获会漏掉
    · 更糟的是**"探测型"调用全废** —— 比如问一个元素"你支持 Grid
      模式吗"，不支持时返回 ``E_NOINTERFACE``，这是**预期结果、
      不是错误**，却被变成异常

    所以凡是"可能会失败且失败是正常结果"的 COM 调用（模式探测、
    属性查询、遍历走到底），一律用本类型作 ``restype``，
    再由调用方用 ``succeeded(hr)`` 判断。

    ``ctypes.c_long`` 正是 HRESULT 的底层类型（4 字节有符号），
    换掉只去掉钩子，二进制布局完全一致。
    """

    # ctypes 的标量类型**不带** int 转换 —— 调用方拿到返回值后
    # 常写 ``hr & 0x80000000`` 或 ``int(hr)``，所以这两个必须补上，
    # 否则每处调用都要先 ``.value``，太难用。
    def __int__(self):
        return int(self.value)

    def __index__(self):
        return int(self.value)

    def __and__(self, other):
        return int(self.value) & other

    def __rand__(self, other):
        return other & int(self.value)

    def __or__(self, other):
        return int(self.value) | other

    def __ror__(self, other):
        return other | int(self.value)

    def __eq__(self, other):
        if isinstance(other, int):
            return int(self.value) == other
        return NotImplemented

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    def __hash__(self):
        return hash(int(self.value))

    def __repr__(self):
        return "HRESULT(0x%08X)" % (int(self.value) & 0xFFFFFFFF)

    def __str__(self):
        return "0x%08X" % (int(self.value) & 0xFFFFFFFF)


#: 有符号的 HRESULT 值（= ``ctypes.c_long`` 的别名，语义更清楚）
HRESULT = HRESULT_SAFE


# --------------------------------------------------------------------------
# 公寓初始化
# --------------------------------------------------------------------------
_com_state = {"init": False, "mode": None, "hr": 0, "thread_id": None}
_com_lock = threading.RLock()
_atexit_registered = False

#: 已初始化状态
CO_ALREADY_INITIALIZED = 0x80010106  # RPC_E_CHANGED_MODE 的常见近亲


def init_com(apartment: str = "MTA", allow_reinit: bool = True) -> dict:
    """初始化 COM。

    **UI Automation 要求 MTA**（``COINIT_MULTITHREADED``），
    因为它内部的跨进程调用不适合 STA 的消息泵模型。
    传 "STA" 只在特殊场景（如需要 OLE 拖放）时用。

    返回状态字典，便于诊断。重复调用安全。

    ⚠️ ``CoInitializeEx`` 在**同一线程**用不同模式重复调用会返回
    ``RPC_E_CHANGED_MODE`` (0x80010106)。这不是"失败"，而是"已经以
    别的模式初始化过了"。本函数把它视作**可接受**并记录实际模式。
    """
    global _atexit_registered
    with _com_lock:
        if _com_state["init"]:
            return dict(_com_state)

        flag = A.COINIT_MULTITHREADED if apartment.upper() == "MTA" else A.COINIT_APARTMENTTHREADED
        hr = A.ole32.CoInitializeEx(None, flag)
        hr_u = int(hr) & 0xFFFFFFFF

        if hr_u == 0x00000000:          # S_OK
            _com_state.update(init=True, mode=apartment.upper(), hr=hr, thread_id=threading.get_ident())
        elif hr_u == 0x00000001:        # S_FALSE（本线程已初始化过同一模式）
            _com_state.update(init=True, mode=apartment.upper(), hr=hr, thread_id=threading.get_ident())
        elif hr_u == 0x80010106:        # RPC_E_CHANGED_MODE
            if not allow_reinit:
                raise ComError(
                    "COM 已以不同公寓模式初始化，无法改为 %s" % apartment,
                    hresult=hr, interface="ole32", method="CoInitializeEx",
                )
            # 探测当前实际模式
            apt_type = ctypes.c_int(0)
            apt_qual = ctypes.c_int(0)
            A.ole32.CoGetApartmentType(ctypes.byref(apt_type), ctypes.byref(apt_qual))
            # APTTYPE: 0=STA 1=MTA 2=NA 3=MAIN_STA
            actual = {0: "STA", 1: "MTA"}.get(apt_type.value, "UNKNOWN")
            _com_state.update(init=True, mode=actual, hr=hr,
                              thread_id=threading.get_ident(), note="RPC_E_CHANGED_MODE")
        else:
            raise ComError(
                "CoInitializeEx 失败", hresult=hr, interface="ole32", method="CoInitializeEx",
            )

        if not _atexit_registered:
            atexit.register(uninit_com)
            _atexit_registered = True
        return dict(_com_state)


def uninit_com() -> None:
    """反初始化。进程退出时自动调用，通常不需要手动调。"""
    with _com_lock:
        if _com_state["init"]:
            A.ole32.CoUninitialize()
            _com_state.update(init=False, mode=None, hr=0, thread_id=None)


def com_state() -> dict:
    return dict(_com_state)


def apartment_type() -> str:
    """当前线程的公寓类型名。"""
    t = ctypes.c_int(-1)
    q = ctypes.c_int(-1)
    A.ole32.CoGetApartmentType(ctypes.byref(t), ctypes.byref(q))
    return {0: "STA", 1: "MTA", 2: "NA", 3: "MAIN_STA"}.get(t.value, "UNKNOWN(%d)" % t.value)


# --------------------------------------------------------------------------
# ComPtr
# --------------------------------------------------------------------------
_FUNC_CACHE = {}
_FUNC_CACHE_LOCK = threading.RLock()

_CACHE_ADDREF = 3
_CACHE_RELEASE = 4


class ComPtr:
    """COM 接口指针的 RAII 封装。

    用法::

        with create_instance(CLSID_CUIAUTOMATION, IID_IUIAUTOMATION) as uia:
            # vtable[6] 是 GetRootElement（IUIAutomation 的方法索引，含 IUnknown 3 个）
            get_root = uia.method(6, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
            root = ctypes.c_void_p()
            check_hresult(get_root(uia.ptr, ctypes.byref(root)),
                          "获取根元素", "IUIAutomation", "GetRootElement", 6)

    ``method(index, restype, *argtypes)`` 里的 ``index`` 是**绝对 vtable 索引**，
    含 IUnknown 占的 0/1/2。

    方法函数对象会按 ``(index, restype, argtypes)`` 缓存 —— 因为 COM 对象的
    vtable 地址在对象生命周期内不变，缓存是安全的。缓存键包含接口名与指针，
    避免不同接口的同索引方法互相污染。
    """

    __slots__ = ("_ptr", "_name", "_freed", "_iid", "_own")

    def __init__(self, ptr, name: str = "IUnknown", iid: str = "", own: bool = True):
        p = int(ptr) if ptr else 0
        self._ptr = p
        self._name = name
        self._freed = p == 0
        self._iid = iid
        self._own = own  # own=False 表示不负责 Release（借用外部指针）

    # -- 基础 ------------------------------------------------------------
    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def name(self) -> str:
        return self._name

    @property
    def valid(self) -> bool:
        return self._ptr != 0 and not self._freed

    def __bool__(self) -> bool:
        return self.valid

    def __int__(self) -> int:
        return self._ptr

    def as_void_p(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(self._ptr)

    def __repr__(self):
        return "<ComPtr %s 0x%X%s>" % (self._name, self._ptr, " FREED" if self._freed else "")

    def _require(self) -> None:
        if self._freed or not self._ptr:
            raise ComError(
                "接口指针已释放或为空（%s）" % self._name,
                hresult=0x80070006, interface=self._name, method="(use after release)",
            )

    # -- vtable ----------------------------------------------------------
    def vtable_ptr(self) -> int:
        """取 vtable 基址。"""
        self._require()
        vt = ctypes.cast(self._ptr, ctypes.POINTER(ctypes.c_void_p))
        return int(vt[0])

    def method(self, index: int, restype, *argtypes):
        """取 vtable[index] 的方法，返回可调用的 ctypes 函数对象。

        ``index`` 是绝对索引，含 IUnknown 的 0/1/2。
        ``argtypes`` 会原样使用；**调用方必须自己把 this 指针作为第一个参数传**。

        返回的函数第一个参数类型强制为 ``c_void_p``（this 指针），
        避免 ctypes 默认把指针当 32 位截断。

        ## ``restype`` 是 ``ctypes.HRESULT`` 时会被自动换成 ``HRESULT_SAFE``

        原因见 ``HRESULT_SAFE`` 的文档：``ctypes.HRESULT`` 的
        ``_check_retval_`` 钩子会在失败时抛 ``OSError``，绕过本模块的
        ``check_hresult`` 契约，还会把"探测失败"这种正常结果变成异常。

        这里做一次静默替换，让**既有的全部调用点**自动获得正确行为，
        不必逐个改 ``restype``。想拿原始 HRESULT 自己判断的调用方
        也就能正常用 ``succeeded(hr)`` 了。
        """
        if restype is ctypes.HRESULT:
            restype = HRESULT_SAFE
        if index < 0:
            raise ValueError("vtable 索引不能为负: %d" % index)
        key = (self._ptr, self._name, index, restype, argtypes)
        with _FUNC_CACHE_LOCK:
            fn = _FUNC_CACHE.get(key)
            if fn is not None:
                return fn
        vt = self.vtable_ptr()
        slots = ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))
        addr = int(slots[index])
        if not addr:
            raise ComError(
                "vtable[%d] 为空 —— 该接口不支持此方法" % index,
                hresult=0x80004001, interface=self._name, method="(null slot)", index=index,
            )
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        fn = proto(ctypes.cast(addr, ctypes.c_void_p).value)
        with _FUNC_CACHE_LOCK:
            _FUNC_CACHE[key] = fn
        return fn

    # 兼容别名
    vcall = method

    def call(self, index: int, restype, *argtypes, **kw):
        """取方法并**立即调用**（自动补 this）。``args`` 关键字传实参。"""
        fn = self.method(index, restype, *argtypes)
        return fn(self._ptr, *kw.get("args", ()))

    # -- IUnknown --------------------------------------------------------
    def query_interface(self, iid) -> "ComPtr":
        """QueryInterface。``iid`` 可以是接口名字符串或 GUID 字符串。"""
        self._require()
        g = iid_of(iid) if isinstance(iid, str) else iid
        out = ctypes.c_void_p()
        fn = self.method(0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        hr = fn(self._ptr, ctypes.byref(g), ctypes.byref(out))
        if not succeeded(hr):
            raise ComError(
                "QueryInterface 失败", hresult=hr, interface=self._name,
                method="QueryInterface(%s)" % (iid if isinstance(iid, str) else str(g)),
                index=0,
            )
        return ComPtr(out.value, name=(iid if isinstance(iid, str) else str(g)))

    def supports(self, iid) -> bool:
        """探测是否支持某接口。支持则返回 True 并**立即释放**探测得到的指针。

        用于能力检测 —— 探测不该产生泄漏。
        """
        try:
            q = self.query_interface(iid)
        except ComError:
            return False
        q.release()
        return True

    def add_ref(self) -> int:
        self._require()
        fn = self.method(1, ctypes.c_ulong)
        return int(fn(self._ptr))

    def release(self) -> int:
        """Release 并置空。幂等 —— 重复调用返回 0，不会 use-after-free。"""
        if self._freed or not self._ptr:
            self._freed = True
            return 0
        if not self._own:
            self._freed = True
            self._ptr = 0
            return 0
        fn = self.method(2, ctypes.c_ulong)
        n = int(fn(self._ptr))
        self._ptr = 0
        self._freed = True
        self._clear_cache()
        return n

    def _clear_cache(self):
        with _FUNC_CACHE_LOCK:
            dead = [k for k in _FUNC_CACHE if k[0] == self._ptr]
            for k in dead:
                _FUNC_CACHE.pop(k, None)

    # -- 上下文管理器 ----------------------------------------------------
    def __enter__(self) -> "ComPtr":
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def __del__(self):
        try:
            if getattr(self, "_own", True) and not getattr(self, "_freed", True):
                self.release()
        except Exception:
            pass


def com_ptr(ptr, name="IUnknown", own=True) -> ComPtr:
    """从裸指针构造 ComPtr。``own=False`` 表示借用（不 Release）。"""
    return ComPtr(ptr, name=name, own=own)


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def create_instance(clsid: str, iid: str = "", context: int = A.CLSCTX_ALL,
                    name: str = "") -> ComPtr:
    """``CoCreateInstance`` 封装，返回 ComPtr（调用方负责 release / with）。

    失败时抛 ComError，附带 CLSID 与常见原因提示
    （``REGDB_E_CLASSNOTREG`` 通常是 GUID 写错或组件未安装）。
    """
    init_com()
    c = guid(clsid) if isinstance(clsid, str) else clsid
    i = guid(iid) if isinstance(iid, str) and iid else (
        guid("{00000000-0000-0000-C000-000000000046}") if not iid else iid)
    if isinstance(i, str):
        i = guid(i)

    label = name or (iid if isinstance(iid, str) and iid else "") or str(clsid)

    out = ctypes.c_void_p()
    hr = A.ole32.CoCreateInstance(
        ctypes.byref(c), None, context, ctypes.byref(i), ctypes.byref(out)
    )
    if not succeeded(hr):
        hint = ""
        if (int(hr) & 0xFFFFFFFF) == 0x80040154:
            hint = "（类未注册：GUID 可能写错，或组件未安装）"
        raise ComError(
            "CoCreateInstance 失败%s" % hint, hresult=hr, interface=label,
            method="CoCreateInstance",
        )
    return ComPtr(out.value, name=label)


def create_instance_direct(clsid: str, iid: str = "", name: str = "") -> ComPtr:
    """先取 IUnknown，再 QueryInterface 到目标接口。

    某些组件的 IID 直接 CoCreateInstance 会失败，但 QI 能成功。
    两条路都试，成功率更高。
    """
    init_com()
    c = guid(clsid) if isinstance(clsid, str) else clsid
    unknown = guid("{00000000-0000-0000-C000-000000000046}")
    out = ctypes.c_void_p()
    hr = A.ole32.CoCreateInstance(
        ctypes.byref(c), None, A.CLSCTX_ALL, ctypes.byref(unknown), ctypes.byref(out)
    )
    if not succeeded(hr):
        raise ComError(
            "CoCreateInstance(IUnknown) 失败", hresult=hr,
            interface=name or str(clsid), method="CoCreateInstance",
        )
    base = ComPtr(out.value, name="IUnknown")
    if not iid:
        return base
    try:
        target = base.query_interface(iid)
    finally:
        base.release()
    if name:
        target._name = name
    return target


# --------------------------------------------------------------------------
# 全局内存
# --------------------------------------------------------------------------
def task_mem_free(ptr) -> None:
    """释放 CoTaskMemAlloc 分配的内存。

    COM 出参里的**数组**（如 SafeArrayToRectNativeArray 得到的 RECT 数组、
    GetRuntimeId 的 SAFEARRAY）必须用它释放，不能用 free()。
    """
    if ptr:
        A.ole32.CoTaskMemFree(ctypes.c_void_p(int(ptr)))


def com_string(ptr, free: bool = True) -> str:
    """读取 UIA / COM 返回的**字符串**并释放。

    ⚠️ 关键：UIA 的 ``get_CurrentName`` / ``get_CurrentAutomationId`` /
    ``get_CurrentClassName`` 等返回的是 **BSTR**，不是 CoTaskMem 分配的
    普通宽字符串。**必须用 SysFreeString 释放**。

    踩过的坑：曾用 ``CoTaskMemFree`` 释放它，立刻触发
    ``0xc0000374``（堆损坏）—— 而且是在读取成功、打印正确之后才崩，
    极具迷惑性。实测 ``SysStringLen`` 返回正数、``SysFreeString``
    正常工作，可反证它是 BSTR。

    判断依据：这些属性在 UIAutomationClient.idl 里声明为 ``BSTR``，
    COM 的 BSTR 一律走 SysFreeString；只有显式标注
    ``[size_is]``/``CoTaskMemAlloc`` 的出参才用 CoTaskMemFree。
    """
    if not ptr:
        return ""
    p = int(ptr)
    try:
        return ctypes.wstring_at(p)
    finally:
        if free:
            A.oleaut32.SysFreeString(ctypes.c_void_p(p))


#: 兼容旧名（阶段一曾用 read_com_string 表示 CoTaskMem 版本，此处纠正语义）
read_com_string = com_string


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------
def diagnostics() -> dict:
    """COM 环境诊断信息。给 selfcheck 用。"""
    init_com()
    return {
        "initialized": _com_state["init"],
        "mode": _com_state["mode"],
        "apartment": apartment_type(),
        "thread_id": _com_state["thread_id"],
        "hresult_init": "0x%08X" % (_com_state["hr"] & 0xFFFFFFFF),
        "cached_methods": len(_FUNC_CACHE),
    }


def clear_method_cache() -> int:
    """清空方法缓存。仅测试用。"""
    with _FUNC_CACHE_LOCK:
        n = len(_FUNC_CACHE)
        _FUNC_CACHE.clear()
    return n
