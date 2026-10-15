# -*- coding: utf-8 -*-
"""VARIANT / BSTR 支持。

职责
----
· 完整的 ``VARIANT`` 结构体布局（16 字节数据 + 4 字节类型 = 24 字节，x64 对齐）
· 从 Python 值构造 VARIANT，从 VARIANT 提取 Python 值
· ``BSTR`` 的分配 / 读取 / 释放（BSTR 不是普通字符串，必须走 SysAllocString）
· HRESULT 格式化工具

依赖
----
· ctypes（标准库）
· ``avatar._win32.api``（oleaut32 绑定）
· 无其他内部依赖

为什么要自己写
--------------
VARIANT 是一个 **union + 类型标签** 的结构。纯 ctypes 下手写它有两个必须
注意的点：

1. **尺寸**：x64 上 ``sizeof(VARIANT) == 24``。数据 union 占 16 字节
   （含一个指针，8 字节对齐），加上 ``VARTYPE``（2 字节）+ 6 字节填充。
   少算填充会让后续字段错位，而 COM 调用**不会报错**，只会拿到垃圾值。

2. **BSTR 的生命周期**：BSTR 是 ``SysAllocString`` 分配的，必须
   ``SysFreeString`` 释放。VARIANT 里的 BSTR 由 ``VariantClear`` 负责释放。
   混用 Python 字符串和 BSTR 是内存泄漏的常见来源。

⚠️ 阶段二实际只用到 **整数 / 布尔 / 字符串** 三种变体。其余类型保留
完整定义以备后用，但转换函数只实现这三种 —— 未实现的显式抛异常，
不做静默的"差不多"转换。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import api as A

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
VT_EMPTY = 0
VT_NULL = 1
VT_I2 = 2
VT_I4 = 3
VT_R4 = 4
VT_R8 = 5
VT_CY = 6
VT_DATE = 7
VT_BSTR = 8
VT_DISPATCH = 9
VT_ERROR = 10
VT_BOOL = 11
VT_VARIANT = 12
VT_UNKNOWN = 13
VT_DECIMAL = 14
VT_I1 = 16
VT_UI1 = 17
VT_UI2 = 18
VT_UI4 = 19
VT_I8 = 20
VT_UI8 = 21
VT_INT = 22
VT_UINT = 23
VT_VOID = 24
VT_HRESULT = 25
VT_PTR = 26
VT_SAFEARRAY = 27
VT_CARRAY = 28
VT_USERDEFINED = 29
VT_LPSTR = 30
VT_LPWSTR = 31
VT_RECORD = 36
VT_INT_PTR = 37
VT_UINT_PTR = 38
VT_ARRAY = 0x2000
VT_BYREF = 0x4000

VARIANT_TRUE = -1
VARIANT_FALSE = 0

#: x64 上 VARIANT 必须正好 24 字节
VARIANT_SIZE_EXPECTED_64 = 24


# --------------------------------------------------------------------------
# 结构体定义
# --------------------------------------------------------------------------
class _VARIANT_UNION(ctypes.Union):
    """VARIANT 的数据联合体。

    必须显式声明 ``c_void_p`` 成员 —— 它决定了 union 的**对齐**为 8 字节，
    从而让整个 VARIANT 达到 24 字节。只写 ``c_longlong`` 在某些平台
    对齐不同。
    """

    _fields_ = [
        ("llVal", ctypes.c_longlong),
        ("lVal", ctypes.c_long),
        ("wVal", ctypes.c_short),
        ("bVal", ctypes.c_char),
        ("fltVal", ctypes.c_float),
        ("dblVal", ctypes.c_double),
        ("boolVal", ctypes.c_short),
        ("scode", ctypes.c_long),
        ("cyVal", ctypes.c_longlong),
        ("date", ctypes.c_double),
        ("bstrVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("pdispVal", ctypes.c_void_p),
        ("parray", ctypes.c_void_p),
        ("piVal", ctypes.c_void_p),
        ("plVal", ctypes.c_void_p),
        ("pllVal", ctypes.c_void_p),
        ("pfltVal", ctypes.c_void_p),
        ("pdblVal", ctypes.c_void_p),
        ("pboolVal", ctypes.c_void_p),
        ("pdecVal", ctypes.c_void_p),
        ("pbstrVal", ctypes.c_void_p),
        ("ppunkVal", ctypes.c_void_p),
        ("ppdispVal", ctypes.c_void_p),
        ("pparray", ctypes.c_void_p),
        ("pvarVal", ctypes.c_void_p),
        # 补齐到 16 字节
        ("_pad", ctypes.c_byte * 16),
    ]


class VARIANT(ctypes.Structure):
    """OLE VARIANT。

    x64 布局（**``vt`` 在前**，这是关键）::

        offset  0 : VARTYPE vt        (2 字节)
        offset  2 : wReserved1        (2 字节)
        offset  4 : wReserved2        (2 字节)
        offset  6 : wReserved3        (2 字节)
        offset  8 : union data        (16 字节，含指针 → 8 字节对齐)
        total     : 24 字节

    ## ⚠ 这个顺序搞反过一次

    最初的版本把 union 放在前面、``vt`` 放在偏移 16 —— **完全反了**。
    Windows 的 ``tagVARIANT`` 定义是 ``VARTYPE vt`` 打头，
    紧随三个保留字，然后才是数据联合体。

    反了之后的现象极具迷惑性：

    · ``VariantInit`` 把 24 字节清零 → 读 ``.vt`` 得到 0（= ``VT_EMPTY``），
      看起来"正常"
    · 但调用方**写入**的实际上是数据区，``vt`` 永远读不到真实类型
    · 最终表现为"所有 VARIANT 出参都是空的"，COM 调用返回 ``S_OK``
      却什么都拿不到

    实测证据：``GetCurrentPropertyValue(30001)`` 返回的原始字节开头是
    ``05 20`` = ``0x2005``（``VT_ARRAY | VT_R8``），而按旧布局读
    ``.vt`` 得到 0。

    结构体尺寸对（24 字节）**不代表字段顺序对**，这是本次踩坑的核心教训。
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("vt_data", _VARIANT_UNION),
    ]

    # -- 构造 ------------------------------------------------------------
    @classmethod
    def empty(cls) -> "VARIANT":
        v = cls()
        ctypes.memset(ctypes.byref(v), 0, ctypes.sizeof(cls))
        v.vt = VT_EMPTY
        return v

    @classmethod
    def from_int(cls, value: int) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_I4
        v.vt_data.lVal = int(value)
        return v

    @classmethod
    def from_int64(cls, value: int) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_I8
        v.vt_data.llVal = int(value)
        return v

    @classmethod
    def from_bool(cls, value: bool) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_BOOL
        v.vt_data.boolVal = VARIANT_TRUE if value else VARIANT_FALSE
        return v

    @classmethod
    def from_double(cls, value: float) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_R8
        v.vt_data.dblVal = float(value)
        return v

    @classmethod
    def from_str(cls, value: str) -> "VARIANT":
        """构造 VT_BSTR 变体。

        ⚠️ 分配出的 BSTR 必须由调用方负责释放。用 ``with`` 或手动
        ``VariantClear``。这里提供了一个上下文管理器 ``variant_scope()``。
        """
        v = cls.empty()
        v.vt = VT_BSTR
        v.vt_data.bstrVal = bstr(value)
        return v

    @classmethod
    def from_unknown(cls, ptr: int) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_UNKNOWN
        v.vt_data.punkVal = int(ptr) if ptr else None
        return v

    @classmethod
    def from_dispatch(cls, ptr: int) -> "VARIANT":
        v = cls.empty()
        v.vt = VT_DISPATCH
        v.vt_data.pdispVal = int(ptr) if ptr else None
        return v

    # -- 提取 ------------------------------------------------------------
    def as_int(self) -> int:
        """提取整数。支持 I1/I2/I4/I8/UI1/UI2/UI4/UI8/INT/UINT/HRESULT。"""
        t = self.vt & ~VT_BYREF
        d = self.vt_data
        if t in (VT_I1, VT_UI1):
            return int(d.bVal[0]) if isinstance(d.bVal, bytes) else int.from_bytes(
                ctypes.string_at(ctypes.byref(d.bVal), 1), "little", signed=(t == VT_I1)
            )
        if t in (VT_I2, VT_UI2, VT_BOOL):
            return int(d.wVal)
        if t in (VT_I4, VT_UI4, VT_INT, VT_UINT, VT_ERROR, VT_HRESULT):
            return int(d.lVal)
        if t in (VT_I8, VT_UI8, VT_CY):
            return int(d.llVal)
        if t == VT_BOOL:
            return int(d.boolVal)
        raise TypeError("VARIANT 类型 %d 不是整数（vt=0x%04X）" % (t, self.vt))

    def as_bool(self) -> bool:
        t = self.vt & ~VT_BYREF
        if t == VT_BOOL:
            return self.vt_data.boolVal != VARIANT_FALSE
        return bool(self.as_int())

    def as_double(self) -> float:
        t = self.vt & ~VT_BYREF
        if t == VT_R4:
            return float(self.vt_data.fltVal)
        if t == VT_R8:
            return float(self.vt_data.dblVal)
        if t in (VT_I4, VT_I8, VT_I2):
            return float(self.as_int())
        raise TypeError("VARIANT 类型 %d 不是浮点（vt=0x%04X）" % (t, self.vt))

    def as_str(self) -> str:
        """提取字符串。

        VT_BSTR 直接读；其他类型尝试 ``VariantChangeType`` 转成 BSTR。
        不做隐式 ``str()`` —— 那会把结构体地址输出成字符串，静默出错。
        """
        t = self.vt & ~VT_BYREF
        if t == VT_EMPTY or t == VT_NULL:
            return ""
        if t == VT_BSTR:
            p = self.vt_data.bstrVal
            return bstr_to_str(p) if p else ""
        if t == VT_LPWSTR:
            p = self.vt_data.bstrVal
            return ctypes.wstring_at(p) if p else ""
        if t == VT_LPSTR:
            p = self.vt_data.bstrVal
            return ctypes.string_at(p).decode("utf-8", "replace") if p else ""
        # 其他类型走系统转换
        return variant_to_str(self)

    def as_ptr(self) -> int:
        t = self.vt & ~VT_BYREF
        if t in (VT_UNKNOWN, VT_DISPATCH):
            return int(self.vt_data.punkVal or 0) if t == VT_UNKNOWN else int(self.vt_data.pdispVal or 0)
        return int(self.vt_data.llVal or 0)

    def is_empty(self) -> bool:
        return (self.vt & ~VT_BYREF) in (VT_EMPTY, VT_NULL)

    @property
    def type_name(self) -> str:
        names = {
            VT_EMPTY: "EMPTY", VT_NULL: "NULL", VT_I2: "I2", VT_I4: "I4",
            VT_R4: "R4", VT_R8: "R8", VT_BSTR: "BSTR", VT_DISPATCH: "DISPATCH",
            VT_ERROR: "ERROR", VT_BOOL: "BOOL", VT_VARIANT: "VARIANT",
            VT_UNKNOWN: "UNKNOWN", VT_I1: "I1", VT_UI1: "UI1", VT_UI2: "UI2",
            VT_UI4: "UI4", VT_I8: "I8", VT_UI8: "UI8", VT_INT: "INT",
            VT_UINT: "UINT", VT_HRESULT: "HRESULT", VT_LPSTR: "LPSTR",
            VT_LPWSTR: "LPWSTR", VT_ARRAY: "ARRAY",
        }
        t = self.vt & ~VT_BYREF
        base = names.get(t, "VT_%d" % t)
        if self.vt & VT_BYREF:
            base += "|BYREF"
        if self.vt & VT_ARRAY:
            base += "|ARRAY"
        return base

    def __str__(self):
        try:
            if (self.vt & ~VT_BYREF) in (VT_EMPTY, VT_NULL):
                return "VARIANT(%s)" % self.type_name
            if self.vt & ~VT_BYREF in (VT_BSTR, VT_LPWSTR):
                return "VARIANT(%s)=%r" % (self.type_name, self.as_str())
            if self.vt & ~VT_BYREF in (VT_BOOL,):
                return "VARIANT(%s)=%s" % (self.type_name, self.as_bool())
            if self.vt & ~VT_BYREF in (VT_I4, VT_I2, VT_I8, VT_UI4, VT_UI8):
                return "VARIANT(%s)=%d" % (self.type_name, self.as_int())
            if self.vt & ~VT_BYREF in (VT_R4, VT_R8):
                return "VARIANT(%s)=%g" % (self.type_name, self.as_double())
            return "VARIANT(%s)=0x%X" % (self.type_name, self.vt_data.llVal or 0)
        except Exception as e:
            return "VARIANT(%s, 读取失败: %s)" % (self.type_name, e)


def expected_variant_size() -> int:
    return VARIANT_SIZE_EXPECTED_64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16


# --------------------------------------------------------------------------
# BSTR
# --------------------------------------------------------------------------
def bstr(text: str) -> int:
    """Python str → BSTR（SysAllocString）。返回指针值。

    ⚠️ 返回值所有权归调用方，**必须** ``SysFreeString``。
    BSTR 不是普通字符串：

    · 前面有 4 字节的长度前缀
    · 以 NUL 结尾（但长度前缀才是权威长度，字符串里可以有 NUL）
    · 必须用 SysAllocString / SysFreeString 配对，不能用 free()
    """
    if text is None:
        return 0
    p = A.oleaut32.SysAllocString(ctypes.c_wchar_p(str(text)))
    return int(p) if p else 0


def bstr_to_str(p: int) -> str:
    """BSTR 指针 → Python str。不释放。"""
    if not p:
        return ""
    return ctypes.wstring_at(p)


def bstr_free(p: int) -> None:
    """释放 BSTR。传 0 安全。"""
    if p:
        A.oleaut32.SysFreeString(ctypes.c_void_p(p))


def bstr_len(p: int) -> int:
    """BSTR 的**字节**长度（不含结尾 NUL）。SysStringByteLen。"""
    if not p:
        return 0
    return int(A.oleaut32.SysStringByteLen(ctypes.c_void_p(p)))


# --------------------------------------------------------------------------
# VariantClear / VariantChangeType
# --------------------------------------------------------------------------
def variant_clear(v: VARIANT) -> None:
    """释放 VARIANT 持有的资源（主要是 BSTR / 接口指针）。

    幂等：清空后 vt 变成 VT_EMPTY，再调无事。
    """
    A.oleaut32.VariantClear(ctypes.byref(v))


def variant_to_str(v: VARIANT) -> str:
    """用系统转换把任意 VARIANT 转成字符串。

    注意：会**修改传入的 v**（VariantChangeType 原地转换）。
    需要保留原值时先 ``copy_variant``。
    """
    out = VARIANT.empty()
    hr = A.oleaut32.VariantChangeType(
        ctypes.byref(out), ctypes.byref(v), 0, VT_BSTR
    )
    if hr < 0:
        return ""
    try:
        return out.as_str()
    finally:
        variant_clear(out)


def copy_variant(v: VARIANT) -> VARIANT:
    """深拷贝一个 VARIANT（含 BSTR 重新分配）。"""
    out = VARIANT.empty()
    hr = A.oleaut32.VariantCopy(ctypes.byref(out), ctypes.byref(v))
    if hr < 0:
        raise OSError("VariantCopy 失败: 0x%08X" % (hr & 0xFFFFFFFF))
    return out


class variant_scope:
    """上下文管理器：保证 VARIANT 被 VariantClear。

    用法::

        with variant_scope(VARIANT.from_str("hello")) as v:
            some_com_call(ctypes.byref(v))
        # 退出时自动 VariantClear，BSTR 被释放
    """

    __slots__ = ("v",)

    def __init__(self, v: VARIANT):
        self.v = v

    def __enter__(self) -> VARIANT:
        return self.v

    def __exit__(self, *exc):
        try:
            variant_clear(self.v)
        except Exception:
            pass
        return False


class bstr_scope:
    """上下文管理器：保证 BSTR 被 SysFreeString。"""

    __slots__ = ("p",)

    def __init__(self, text: str):
        self.p = bstr(text)

    def __enter__(self) -> int:
        return self.p

    def __exit__(self, *exc):
        bstr_free(self.p)
        return False


# --------------------------------------------------------------------------
# HRESULT 工具
# --------------------------------------------------------------------------
def hr_hex(hr: int) -> str:
    """HRESULT → ``0xXXXXXXXX`` 字符串。"""
    return "0x%08X" % (int(hr) & 0xFFFFFFFF)


def hr_ok(hr: int) -> bool:
    """SUCCEEDED(hr)：最高位为 0。"""
    return int(hr) >= 0 if int(hr) < 0x80000000 else False


def hr_name(hr: int) -> str:
    """常见 HRESULT 的可读名。查不到返回空串。"""
    table = {
        0x00000000: "S_OK",
        0x00000001: "S_FALSE",
        0x80004001: "E_NOTIMPL",
        0x80004002: "E_NOINTERFACE",
        0x80004003: "E_POINTER",
        0x80004004: "E_ABORT",
        0x80004005: "E_FAIL",
        0x80070005: "E_ACCESSDENIED",
        0x80070006: "E_HANDLE",
        0x8007000E: "E_OUTOFMEMORY",
        0x80070057: "E_INVALIDARG",
        0x80040154: "REGDB_E_CLASSNOTREG",
        0x800401F0: "CO_E_NOTINITIALIZED",
        0x80010105: "RPC_E_SERVERFAULT",
        0x80010108: "RPC_E_DISCONNECTED",
        0x8001010A: "RPC_E_SERVERCALL_RETRYLATER",
        0x8001010D: "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",
        0x8007007E: "ERROR_MOD_NOT_FOUND",
        0x80070490: "ERROR_NOT_FOUND",
        0x80131500: "COR_E_EXCEPTION",
    }
    return table.get(int(hr) & 0xFFFFFFFF, "")


def hr_describe(hr: int) -> str:
    """格式化 HRESULT。例：``0x80004005 (E_FAIL)``"""
    h = hr_hex(hr)
    n = hr_name(hr)
    return "%s (%s)" % (h, n) if n else h


# --------------------------------------------------------------------------
# 模块级自检（被 tests 调用）
# --------------------------------------------------------------------------
def _selfcheck_struct_sizes() -> dict:
    return {
        "VARIANT": ctypes.sizeof(VARIANT),
        "VARIANT.expected": expected_variant_size(),
        "_VARIANT_UNION": ctypes.sizeof(_VARIANT_UNION),
        "BSTR_ptr": ctypes.sizeof(ctypes.c_void_p),
    }
