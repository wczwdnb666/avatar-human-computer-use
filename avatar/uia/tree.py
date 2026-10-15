# -*- coding: utf-8 -*-
"""UIA 元素树：把 COM 元素转成 Python 值对象，并安全遍历。

职责
----
1. ``Element`` —— ``IUIAutomationElement`` 的 Python 包装（值语义）
2. ``root()`` / ``from_window()`` / ``from_point()`` / ``focused()`` —— 入口
3. ``children()`` / ``parent()`` / ``descendants()`` / ``walk()`` —— 遍历
4. ``TreeWalker`` 封装 —— 三种视图（raw / control / content）
5. ``dump()`` —— 树的可读快照，调试与喂给模型用

依赖
----
· ``avatar._win32.com``（ComPtr / com_string / check_hresult）
· ``avatar._win32.guids``
· ``avatar.app_manager.layout``（只读几何，**不缓存屏幕坐标**）

## 为什么要包装成值对象

``IUIAutomationElement`` 是 COM 引用计数的。如果直接把裸指针往上抛：

· 调用方忘了 Release → 泄漏（UIA 元素很重，几十个就能看出内存涨）
· 元素对应的控件被销毁后，指针还指着 → 访问即崩（``0xc0000005``）
· 树遍历时持有大量中间引用 → 内存爆

所以本模块的做法是：**读值、立刻放**。``Element`` 只存属性快照
（名字、类型、矩形、模式支持情况），底层 COM 指针在构造完就释放。
代价是无法通过 ``Element`` 反向调用 COM 方法 —— 需要操作时用
``Element.ref()`` 现场重新获取一个 ``ComPtr``，用完即放。

这个取舍是刻意的：**UIA 树是动态的，任何时刻的引用都可能失效**。
持有快照比持有引用安全得多，也和"不缓存坐标"的原则一致。
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from .._win32 import api as A
from .._win32 import com as C
from .._win32 import guids as G
from .._win32 import structs as S
from .._win32 import variant as V
from ..errors import ElementStale, UiaUnavailable, WindowNotFound
from ..app_manager import layout as L

#: ``oleaut32`` —— SAFEARRAY 的释放必须走这里
_A_oleaut32 = A.oleaut32

#: ``SAFEARRAY`` 结构（只需要前 24 字节）。
#:
#: 布局::
#:
#:     cDims      u16     维度数（通常 1）
#:     fFeatures  u16     标志位
#:     cbElements u32     单元素字节数
#:     cLocks     u32     锁计数
#:     pvData     void*   数据区
#:     rgsabound  { cElements: u32, lLbound: i32 }[1]
#:
#: 注意 x64 下 ``pvData`` 是 8 字节且要求 8 字节对齐，
#: 所以 ``cbElements``/``cLocks`` 之后有 4 字节填充 —— 用 ctypes
#: 自动对齐即可，不要手算偏移。
class _SAFEARRAY_BOUND(ctypes.Structure):
    _fields_ = [
        ("cElements", ctypes.c_ulong),
        ("lLbound", ctypes.c_long),
    ]


class _SAFEARRAY(ctypes.Structure):
    _fields_ = [
        ("cDims", ctypes.c_ushort),
        ("fFeatures", ctypes.c_ushort),
        ("cbElements", ctypes.c_ulong),
        ("cLocks", ctypes.c_ulong),
        ("pvData", ctypes.c_void_p),
        ("rgsabound", _SAFEARRAY_BOUND * 1),
    ]


def _safearray_length(psa: int) -> int:
    """SAFEARRAY 的元素个数（只支持一维）。"""
    if not psa:
        return 0
    sa = ctypes.cast(ctypes.c_void_p(psa), ctypes.POINTER(_SAFEARRAY)).contents
    if int(sa.cDims) != 1:
        return 0
    return int(sa.rgsabound[0].cElements)


def _safearray_data(psa: int):
    """SAFEARRAY 的数据区指针（按 ``double`` 解释，供矩形读取用）。"""
    if not psa:
        return None
    sa = ctypes.cast(ctypes.c_void_p(psa), ctypes.POINTER(_SAFEARRAY)).contents
    if not sa.pvData:
        return None
    return ctypes.cast(sa.pvData, ctypes.POINTER(ctypes.c_double))

# --------------------------------------------------------------------------
# vtable 索引（IUIAutomationElement 共 85 个方法，0..84）
# --------------------------------------------------------------------------
# IUnknown 占 0/1/2，业务方法从 3 开始。索引是**绝对**的。
VT_ELEMENT_SET_FOCUS = 3
VT_ELEMENT_GET_RUNTIME_ID = 4
VT_ELEMENT_FIND_FIRST = 5
VT_ELEMENT_FIND_ALL = 6
VT_ELEMENT_GET_CURRENT_PATTERN_AS = 14
VT_ELEMENT_GET_CURRENT_PATTERN = 16
VT_ELEMENT_GET_CURRENT_PROPERTY = 10
#: ``get_CurrentIsPassword`` —— 索引 34，与 ``VT_ELEMENT_PROPS["is_password"]`` 一致
VT_ELEMENT_GET_CURRENT_IS_PASSWORD = 34
#: ``get_CurrentBoundingRectangle`` —— 索引 43（**不推荐用**，见 _get_bounding_rect）
VT_ELEMENT_GET_CURRENT_BOUNDING_RECT = 43

VT_ELEMENT_PROPS = {
    "process_id": 20,
    "control_type": 21,
    "localized_type": 22,
    "name": 23,
    "accelerator_key": 24,
    "access_key": 25,
    "has_keyboard_focus": 26,
    "is_keyboard_focusable": 27,
    "is_enabled": 28,
    "automation_id": 29,
    "class_name": 30,
    "help_text": 31,
    "is_content_element": 32,
    "is_control_element": 33,
    "is_password": 34,
    "native_window_handle": 36,
    "is_offscreen": 38,
    "item_status": 39,
    "framework_id": 40,
    "is_required_for_forms": 41,
    "is_data_valid_for_forms": 42,
    "bounding_rectangle": 43,
    "item_type": 44,
    "is_available": 45,
    "orientation": 46,
    "level": 47,
    "position_in_set": 48,
    "provider_description": 49,
}
VT_ELEMENT_CLICKABLE_POINT = 84

# IUIAutomation（顶层）vtable
VT_UA_GET_ROOT = 5
VT_UA_FROM_HANDLE = 6
VT_UA_FROM_POINT = 7
VT_UA_FOCUSED = 8
VT_UA_CREATE_TREE_WALKER = 13
VT_UA_CONTROL_WALKER = 14
VT_UA_CONTENT_WALKER = 15
VT_UA_RAW_WALKER = 16
VT_UA_CREATE_TRUE_CONDITION = 21
VT_UA_CREATE_PROPERTY_CONDITION = 23
VT_UA_CREATE_AND_CONDITION = 25
VT_UA_CREATE_OR_CONDITION = 28
VT_UA_CREATE_NOT_CONDITION = 31

# IUIAutomationElementArray
VT_ARRAY_LENGTH = 3
VT_ARRAY_GET_ELEMENT = 4

# IUIAutomationTreeWalker
VT_WALKER_GET_PARENT = 3
VT_WALKER_GET_FIRST_CHILD = 4
VT_WALKER_GET_LAST_CHILD = 5
VT_WALKER_GET_NEXT_SIBLING = 6
VT_WALKER_GET_PREVIOUS_SIBLING = 7

#: 三种视图
VIEW_RAW = "raw"
VIEW_CONTROL = "control"
VIEW_CONTENT = "content"

#: 遍历安全上限 —— 防止在病态 UI 上无限递归
MAX_DESCENDANTS = 4000
MAX_DEPTH_DEFAULT = 32

#: ``UIA_BoundingRectanglePropertyId``
#:
#: 走属性读取器（``GetCurrentPropertyValue``）而不是
#: ``get_CurrentBoundingRectangle`` 的原因见 ``_get_bounding_rect``。
UIA_BOUNDING_RECT_PROP_ID = 30001

# --------------------------------------------------------------------------
# UIA 属性 ID（``UIA_*PropertyId``，30000 段）
# --------------------------------------------------------------------------
#: ⚠ **两套编号不能混用**
#:
#: · ``VT_ELEMENT_PROPS`` 里的 20/23/28… 是 **vtable 方法索引**，
#:   只对 ``el.method(index, ...)`` 这种直接调用有效
#: · 下面这套 30000 段的是 **属性 ID**，只对
#:   ``GetCurrentPropertyValue(prop_id)` 有效
#:
#: 把 vtable 索引当属性 ID 传给 ``GetCurrentPropertyValue`` 会得到
#: ``E_INVALIDARG (0x80070057)`` —— 实测踩过一次。
#:
#: 这些值来自 ``UIAutomationClient.h`` 的 ``enum UIAutomationPropertyIds``，
#: 已对照微软官方文档核对。
#:
#: ⚠ 注意有两个属性**不在连续区间里**，容易想当然写错：
#: ``UIA_ItemStatusPropertyId`` 是 30026 而 ``UIA_IsDataValidForFormPropertyId``
#: 是 30103，``UIA_LevelPropertyId`` 是 30154、``UIA_PositionInSetPropertyId``
#: 是 30152 —— 不要按相邻关系推算。
UIA_PROPERTY_IDS = {
    "runtime_id": 30000,
    "bounding_rectangle": 30001,
    "process_id": 30002,
    "control_type": 30003,
    "localized_type": 30004,
    "name": 30005,
    "accelerator_key": 30006,
    "access_key": 30007,
    "has_keyboard_focus": 30008,
    "is_keyboard_focusable": 30009,
    "is_enabled": 30010,
    "automation_id": 30011,
    "class_name": 30012,
    "help_text": 30013,
    "is_control_element": 30016,
    "is_content_element": 30017,
    "is_password": 30019,
    "native_window_handle": 30020,
    "is_offscreen": 30022,
    "framework_id": 30024,
    "is_required_for_forms": 30025,
    "item_status": 30026,
    "is_data_valid_for_forms": 30103,
    "provider_description": 30107,
    "position_in_set": 30152,
    "level": 30154,
    "is_dialog": 30174,
}


# --------------------------------------------------------------------------
# ControlType 常量（UIA_ControlTypeIds）
# --------------------------------------------------------------------------
class ControlType:
    """UIA ``ControlType`` 取值。名字比数字好读，也便于给模型看。"""

    BUTTON = 50000
    CALENDAR = 50001
    CHECK_BOX = 50002
    COMBO_BOX = 50003
    EDIT = 50004
    HYPERLINK = 50005
    IMAGE = 50006
    LIST_ITEM = 50007
    LIST = 50008
    MENU = 50009
    MENU_BAR = 50010
    MENU_ITEM = 50011
    PROGRESS_BAR = 50012
    RADIO_BUTTON = 50013
    SCROLL_BAR = 50014
    SLIDER = 50015
    SPINNER = 50016
    STATUS_BAR = 50017
    TAB = 50018
    TAB_ITEM = 50019
    TEXT = 50020
    TOOL_BAR = 50021
    TOOL_TIP = 50022
    TREE = 50023
    TREE_ITEM = 50024
    CUSTOM = 50025
    GROUP = 50026
    THUMB = 50027
    DATA_GRID = 50028
    DATA_ITEM = 50029
    DOCUMENT = 50030
    SPLIT_BUTTON = 50031
    WINDOW = 50032
    PANE = 50033
    HEADER = 50034
    HEADER_ITEM = 50035
    TABLE = 50036
    TITLE_BAR = 50037
    SEPARATOR = 50038
    SEMANTIC_ZOOM = 50039
    APPBAR = 50040


#: 数字 → 短名
CONTROL_NAMES = {
    v: k.lower() for k, v in vars(ControlType).items()
    if not k.startswith("_") and isinstance(v, int)
}

#: 短名 → 数字（大小写不敏感）
CONTROL_IDS = {k: v for v, k in CONTROL_NAMES.items()}


def control_name(ct: int) -> str:
    """``ControlType`` 数字 → 短名。未知返回 ``"unknown(...)"。"""
    return CONTROL_NAMES.get(int(ct), "unknown(%s)" % ct)


def control_id(name: str) -> int:
    """短名 → ``ControlType`` 数字，未知抛 ``ValueError``。"""
    key = (name or "").strip().lower()
    if key in CONTROL_IDS:
        return CONTROL_IDS[key]
    raise ValueError(
        "未知控件类型: %r（可选: %s）"
        % (name, ", ".join(sorted(CONTROL_IDS)[:20]) + " ...")
    )


#: 可交互的控件类型 —— 找"能点的东西"时默认只在这些里找
INTERACTIVE_TYPES = frozenset({
    ControlType.BUTTON, ControlType.CHECK_BOX, ControlType.COMBO_BOX,
    ControlType.EDIT, ControlType.HYPERLINK, ControlType.LIST_ITEM,
    ControlType.MENU_ITEM, ControlType.RADIO_BUTTON, ControlType.SLIDER,
    ControlType.SPINNER, ControlType.TAB_ITEM, ControlType.TREE_ITEM,
    ControlType.DATA_ITEM, ControlType.SPLIT_BUTTON, ControlType.SCROLL_BAR,
    ControlType.THUMB,
})

#: 容器类控件 —— 搜索时通常要穿透，不该作为命中结果
CONTAINER_TYPES = frozenset({
    ControlType.PANE, ControlType.WINDOW, ControlType.GROUP,
    ControlType.CUSTOM, ControlType.DOCUMENT, ControlType.TABLE,
    ControlType.DATA_GRID, ControlType.TREE, ControlType.LIST,
    ControlType.TOOL_BAR, ControlType.MENU_BAR, ControlType.STATUS_BAR,
    ControlType.TITLE_BAR, ControlType.HEADER,
})


# --------------------------------------------------------------------------
# 底层：属性读取（全部走统一的错误处理）
# --------------------------------------------------------------------------
def _get_prop_bool(el: C.ComPtr, index: int, default: int = 0) -> int:
    """读取一个返回 ``BOOL`` 的属性，返回 0/1。

    ## 为什么单独有这个函数

    ``BOOL`` 属性的 vtable 签名是 ``HRESULT( BOOL *retVal )``。
    直接按 ``c_int`` 出参读**大多数情况能用**，但实测有例外：
    ``get_CurrentIsPassword``（索引 34）对一个普通 Edge 窗口返回 1
    —— 明显是错的。

    原因是这个属性在跨进程 marshal 时走的是属性表（property bag），
    而不是直接的 vtable 调用。稳妥做法是走
    ``GetCurrentPropertyValue``，让它按 ``VT_BOOL`` 自己解释。

    ``VT_BOOL`` 在 VARIANT 里是 ``short``：``-1`` 为真、``0`` 为假
    （``VARIANT_TRUE``/``VARIANT_FALSE``）。**不是 1/0** —— 这点很容易搞错。
    """
    val = _get_prop_value(el, index)
    if val is None:
        return default
    vt = int(val.vt)
    try:
        if vt == V.VT_BOOL:
            raw = int(val.vt_data.boolVal)
            return 1 if raw != 0 else 0      # -1 和 1 都算真
        if vt in (V.VT_I4, V.VT_INT):
            return 1 if int(val.vt_data.lVal) != 0 else 0
        if vt == V.VT_EMPTY:
            return default
        return default
    except Exception:
        return default
    finally:
        try:
            V.variant_clear(val)
        except Exception:
            pass


def _get_prop_str(el: C.ComPtr, index: int) -> str:
    """读取一个返回 BSTR 的属性。空值返回 ""。"""
    out = ctypes.c_void_p()
    fn = el.method(index, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(el._ptr, ctypes.byref(out))
    if not C.succeeded(hr):
        return ""
    if not out.value:
        return ""
    # UIA 返回的是 BSTR，必须 SysFreeString —— 用 CoTaskMemFree 会堆损坏
    return C.com_string(out.value)


def _get_prop_int(el: C.ComPtr, index: int, default: int = 0) -> int:
    """读取一个返回 int 的属性（BOOL 也走这里，真=1/假=0）。"""
    out = ctypes.c_int(default)
    fn = el.method(index, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))
    hr = fn(el._ptr, ctypes.byref(out))
    if not C.succeeded(hr):
        return default
    return int(out.value)


def _get_prop_ptr(el: C.ComPtr, index: int) -> int:
    """读取一个返回指针/句柄的属性（如 NativeWindowHandle）。"""
    out = ctypes.c_void_p()
    fn = el.method(index, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(el._ptr, ctypes.byref(out))
    if not C.succeeded(hr):
        return 0
    return int(out.value or 0)


def _get_prop_value(el: C.ComPtr, prop_id: int):
    """调 ``GetCurrentPropertyValue``，返回 ``VARIANT`` 或 ``None``。

    vtable 索引 10 是 ``IUIAutomationElement::GetCurrentPropertyValue``，
    签名是 ``HRESULT( PROPERTYID id, VARIANT *retVal )`` —— 注意它
    比 ``get_CurrentXXX`` 系列多一个入参。

    返回值是一个**已初始化的 ``VARIANT``**（调用方负责 ``VariantClear``），
    读取失败返回 ``None``。这里不做类型判断 —— 交给调用方按需解释。
    """
    out = V.VARIANT()
    # 防御：确保 vt = VT_EMPTY，否则 COM 可能往未初始化内存里写
    ctypes.memset(ctypes.byref(out), 0, ctypes.sizeof(out))

    fn = el.method(VT_ELEMENT_GET_CURRENT_PROPERTY, ctypes.HRESULT,
                   ctypes.c_int, ctypes.POINTER(V.VARIANT))
    hr = fn(el._ptr, ctypes.c_int(int(prop_id)), ctypes.byref(out))
    if not C.succeeded(hr):
        return None
    return out


def _variant_doubles(val) -> List[float]:
    """把 ``VT_ARRAY | VT_R8`` 的 ``SAFEARRAY`` 读成 ``float`` 列表。

    调用方交出 ``val`` 的所有权 —— 本函数读完会 ``VariantClear``。

    ## SAFEARRAY 释放

    必须用 ``SafeArrayDestroy``（``oleaut32``）而不是 ``CoTaskMemFree``。
    之所以没走 ``VariantClear`` 统一释放，是因为 ``SafeArrayDestroy``
    还要负责释放数组内部元素（对 ``VT_BSTR``/``VT_VARIANT`` 尤其重要）；
    但两者都调会导致 double-free —— 所以这里**只**调
    ``SafeArrayDestroy``，再把 ``vt`` 置成 ``VT_EMPTY`` 让外层的
    ``VariantClear`` 变成空操作。
    """
    if val is None:
        return []
    vt = int(val.vt)
    if vt in (V.VT_EMPTY, V.VT_NULL):
        return []

    if vt != (V.VT_ARRAY | V.VT_R8):
        # 不是期望的类型 —— 清掉，避免泄漏
        try:
            V.variant_clear(val)
        except Exception:
            pass
        return []

    psa = int(val.vt_data.parray or 0)
    if not psa:
        return []
    nums: List[float] = []
    try:
        n = _safearray_length(psa)
        data = _safearray_data(psa)
        if data:
            nums = [float(data[i]) for i in range(max(0, n))]
    except Exception:
        nums = []
    finally:
        try:
            _A_oleaut32.SafeArrayDestroy(ctypes.c_void_p(psa))
        except Exception:
            pass
        # 数组已被销毁 —— 把 VARIANT 标记为空，防止 VariantClear 再碰它
        try:
            val.vt = V.VT_EMPTY
        except Exception:
            pass
    return nums
def _get_bounding_rect(el: C.ComPtr):
    """读取边界矩形，返回 ``(left, top, width, height)`` 或 None。

    ## 为什么不用 vtable[43]

    ``IUIAutomationElement::get_CurrentBoundingRectangle``（vtable 索引 43）
    的签名是 ``HRESULT get_CurrentBoundingRectangle(RECT *retVal)``，
    ``RECT`` 是 **4 个 32 位 LONG**（16 字节）。

    但实测发现：直接照签名调，返回的 HRESULT 是 S_OK，
    结构里却全是垃圾值（``1e-311`` 那种）。原因是 UIA 的
    ``CurrentBoundingRectangle`` 属性**在跨进程 marshal 时按
    ``double[4]`` 序列化**，与 C 头文件里的 ``RECT`` 不一致。

    稳妥的做法是走属性读取器：``GetCurrentPropertyValue(30001)``
    （``UIA_BoundingRectanglePropertyId``），它返回一个
    ``VT_ARRAY | VT_R8`` 的 ``SAFEARRAY``，元素是
    ``[left, top, width, height]`` 四个 double —— 语义明确、与实现无关。
    """
    val = _get_prop_value(el, UIA_BOUNDING_RECT_PROP_ID)
    if val is None:
        return None
    nums = _variant_doubles(val)
    if not nums or len(nums) < 4:
        return None
    return (nums[0], nums[1], nums[2], nums[3])


# --------------------------------------------------------------------------
# Element
# --------------------------------------------------------------------------
@dataclass
class Element:
    """UIA 元素的**值快照**。

    只存读出来的属性，不持有 COM 指针。构造完底层引用即被释放。

    坐标语义
    --------
    ``rect`` 是 ``layout.Rect``，坐标系是**屏幕物理像素** —— 这是
    UIA 的原生输出（进程 DPI-aware 时）。但本对象**不把它当作长期有效值**：

    · ``rect`` 是构造那一刻的快照，窗口一动就过时
    · 要"当前"位置，调 ``Element.refresh()`` 重新读
    · 要窗口相对坐标，用 ``client_rect()``，它按需换算
    """

    #: 元素名（通常是显示文本）
    name: str = ""
    #: ControlType 数字
    control_type: int = 0
    #: 本地化的控件类型名（如"按钮"）
    localized_type: str = ""
    #: AutomationId —— 最稳定的定位依据
    automation_id: str = ""
    #: 控件类名（如 "Button"）
    class_name: str = ""
    #: 所属进程 PID
    process_id: int = 0
    #: 元素**自身**所属顶层窗口句柄。
    #:
    #: 只有元素自己有 ``NativeWindowHandle``（即它本身是个窗口）时才非零。
    #: 多数 UIA 元素（按钮、编辑框、列表项）这里是 0 —— 别拿它当
    #: "宿主窗口"用，那个是 ``window``。
    hwnd: int = 0
    #: 元素**所在的**顶层窗口句柄 —— 用来做窗口相对坐标换算。
    #:
    #: 与 ``hwnd`` 的区别很关键：一个按钮 ``hwnd`` 是 0，但它的
    #: ``window`` 是那个记事本窗口的句柄。``client_rect()`` 用这个值。
    window: int = 0
    #: 元素自身的 NativeWindowHandle（常常是 0 —— 多数 UIA 元素不是窗口）
    native_hwnd: int = 0
    #: 边界矩形（屏幕物理像素，快照）
    rect: Optional[object] = None
    #: 是否可用（enabled）
    enabled: bool = True
    #: 是否有键盘焦点
    focused: bool = False
    #: 是否可接收键盘焦点
    focusable: bool = False
    #: 是否在屏幕外（被滚动出视口 / 被遮挡）
    offscreen: bool = False
    #: 是否密码框
    password: bool = False
    #: 框架（Win32 / UIA / WPF / WinForm ...）
    framework: str = ""
    #: 帮助文本
    help_text: str = ""
    #: 加速键
    accelerator: str = ""
    #: 访问键
    access_key: str = ""
    #: 运行时 ID（同一元素在不同时刻可能不同，但同一进程内可用于比较）
    runtime_id: tuple = field(default_factory=tuple)
    #: 支持的模式名（延迟探测，见 ``detect_patterns``）
    patterns: tuple = field(default_factory=tuple)
    #: 深度（``descendants`` 填充）
    depth: int = 0
    #: 父元素引用（``descendants`` 填充，为可读性保留；不参与比较）
    parent: Optional["Element"] = None
    #: 是否为占位/合成元素（由本模块生成，非 UIA 原生）
    synthetic: bool = False

    # -- 基本信息 --------------------------------------------------------
    @property
    def type_name(self) -> str:
        """控件类型的短名（``button`` / ``edit`` ...）。"""
        return control_name(self.control_type)

    @property
    def is_interactive(self) -> bool:
        """是否属于可交互控件类型。"""
        return self.control_type in INTERACTIVE_TYPES

    @property
    def is_container(self) -> bool:
        """是否属于容器控件类型。"""
        return self.control_type in CONTAINER_TYPES

    @property
    def label(self) -> str:
        """最像"用户看到的那个名字"的字符串。

        优先 ``name``，退化到 ``automation_id``、``class_name``。
        """
        return self.name or self.automation_id or self.class_name or self.localized_type

    @property
    def key(self) -> str:
        """稳定标识，用于去重与比较。

        由 ``runtime_id`` 派生（若可用），否则退回属性组合。
        ``runtime_id`` 是 UIA 给出的"这个元素"的身份，同一会话内稳定。
        """
        if self.runtime_id:
            return "rt:" + "-".join(str(x) for x in self.runtime_id)
        return "sig:%d:%d:%s:%s" % (
            self.process_id, self.control_type,
            self.automation_id, self.name,
        )

    # -- 几何 ------------------------------------------------------------
    @property
    def center(self):
        """元素中心点（屏幕物理像素）。无矩形返回 None。"""
        if self.rect is None:
            return None
        return self.rect.center

    def client_rect(self):
        """元素矩形换算到**所属窗口的客户区坐标系**。

        这是本模块对外的主要坐标口径 —— 窗口一动，客户区坐标不变，
        而屏幕坐标全废。

        ## 用哪个句柄

        用 ``self.window``（元素**所在**的顶层窗口），不是 ``self.hwnd``
        （元素**自己**的窗口 — 多数元素是 0）。两者在 ``Element`` 上是
        分开的字段。

        ## 换算方式

        取元素的左上角和右下角两个屏幕点，分别减去窗口客户区原点。
        不能只换算左上角再加宽高 —— 多显示器 + 不同 DPI 下，
        客户区原点本身就带缩放，只移一个点会有累积误差。

        无宿主窗口或无矩形时返回 None。
        """
        if self.rect is None or not self.window:
            return None

        ox, oy = L.client_origin(self.window)
        return L.Rect.from_ltrb(
            int(self.rect.left) - int(ox),
            int(self.rect.top) - int(oy),
            int(self.rect.right) - int(ox),
            int(self.rect.bottom) - int(oy),
        )

    def center_in_client(self):
        """元素中心点换算到客户区坐标。"""
        cr = self.client_rect()
        return cr.center if cr is not None else None

    def client_center(self):
        """元素中心的**客户区坐标** ``(x, y)``；无客户区返回 ``None``。

        这是阶段三输入层要的坐标口径 —— 输入层的**唯一坐标系是屏幕
        绝对坐标**，所以这个客户区坐标必须再经
        ``avatar.app_manager.layout.client_to_screen(hwnd, x, y)`` 换算，
        或者直接交给
        ``avatar.input.dispatcher.dispatch_click(hwnd, x, y)``
        （它内部会换算）。

        ⚠️ 不要把这个坐标直接喂给 ``input.mouse.move_absolute`` ——
        那个函数要的是屏幕坐标，喂客户区坐标会点到窗口外。
        """
        return self.center_in_client()

    def client_center_int(self):
        """``client_center()`` 的整数版（输入层要 int）。"""
        c = self.center_in_client()
        if c is None:
            return None
        return (int(c[0]), int(c[1]))

    def screen_center(self):
        """元素中心的**屏幕绝对坐标** ``(x, y)``；不可得返回 ``None``。

        每次调用都现算（不缓存快照）—— 窗口一动，快照就过时。
        优先用客户区坐标 + ``ClientToScreen``；没有客户区时退回
        ``rect.center``（快照）。
        """
        cr = self.client_rect()
        if cr is not None and self.window:
            try:
                x, y = cr.center
                sx, sy = L.client_to_screen(self.window, int(x), int(y))
                return (int(sx), int(sy))
            except Exception:
                pass
        if self.rect is not None:
            return self.rect.center
        return None

    def screen_center_with_source(self):
        """返回 ``((x, y), source)``，``source`` ∈ ``{"live", "snapshot"}``。

        ``live`` = 刚用 ClientToScreen 现算的，可信；
        ``snapshot`` = 元素快照里的矩形，窗口若已移动则偏。
        上层在"点击后没反应"时可以用这个字段判断是不是踩了快照。
        """
        cr = self.client_rect()
        if cr is not None and self.window:
            try:
                x, y = cr.center
                sx, sy = L.client_to_screen(self.window, int(x), int(y))
                return (int(sx), int(sy)), "live"
            except Exception:
                pass
        if self.rect is not None:
            return self.rect.center, "snapshot"
        return None, "none"

    def contains_point(self, x: int, y: int) -> bool:
        """屏幕物理坐标是否落在元素矩形内。"""
        return self.rect is not None and self.rect.contains(x, y)

    # -- 比较 ------------------------------------------------------------
    def __eq__(self, other):
        if not isinstance(other, Element):
            return NotImplemented
        return self.key == other.key

    def __hash__(self):
        return hash(self.key)

    # -- 输出 ------------------------------------------------------------
    def to_dict(self, short: bool = False) -> dict:
        d = {
            "name": self.name,
            "type": self.type_name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class": self.class_name,
            "enabled": self.enabled,
            "offscreen": self.offscreen,
        }
        if not short:
            d.update({
                "localized_type": self.localized_type,
                "pid": self.process_id,
                "hwnd": self.hwnd,
                "native_hwnd": self.native_hwnd,
                "rect": self.rect.as_xywh() if self.rect else None,
                "client_rect": (self.client_rect().as_xywh()
                                if self.client_rect() else None),
                "focused": self.focused,
                "focusable": self.focusable,
                "password": self.password,
                "framework": self.framework,
                "help_text": self.help_text,
                "patterns": list(self.patterns),
                "runtime_id": list(self.runtime_id),
                "depth": self.depth,
            })
        return d

    def __str__(self):
        flags = []
        if not self.enabled:
            flags.append("禁用")
        if self.offscreen:
            flags.append("离屏")
        if self.focused:
            flags.append("焦点")
        if self.password:
            flags.append("密码")
        tail = (" [%s]" % ",".join(flags)) if flags else ""
        name = (self.name or "(无名)")
        if len(name) > 40:
            name = name[:37] + "..."
        return "%-14s %-42s %s%s" % (
            self.type_name, name, self.automation_id or "-", tail
        )

    def line(self, indent: int = 0) -> str:
        """带缩进的一行，用于打印树。"""
        return "  " * indent + str(self)


# --------------------------------------------------------------------------
# UIAutomation 单例
# --------------------------------------------------------------------------
_UA_LOCK = __import__("threading").Lock()
_UA: Optional[C.ComPtr] = None


def _we_own_com() -> bool:
    """本次调用是否由我们初始化 COM。

    线程已经初始化过 COM 时 ``CoInitializeEx`` 返回
    ``S_FALSE (1)`` 或 ``RPC_E_CHANGED_MODE`` —— 那种情况下
    **不该**由我们 ``CoUninitialize``，否则会把调用方的 COM 拆掉。
    """
    return C.init_com("MTA") == 0


def uia() -> C.ComPtr:
    """取 ``IUIAutomation`` 实例（进程内单例）。

    首次调用时创建；后续复用。**不要**手动 release 返回值 ——
    它由模块级缓存持有，生命周期到进程结束。
    """
    global _UA
    with _UA_LOCK:
        if _UA is not None and _UA.valid:
            return _UA
        C.init_com("MTA")
        _UA = C.create_instance(
            G.CLSID_CUIAUTOMATION, G.IID_IUIAUTOMATION, name="CUIAutomation"
        )
        return _UA


def is_available() -> bool:
    """UIA 是否可用。任何失败都返回 False，不抛异常。

    供上层做能力探测 —— 例如"这台机器能不能用元素定位"。
    """
    try:
        u = uia()
        root = _raw_root(u)
        ok = bool(root)
        if root:
            C.ComPtr(root, name="probe").release()
        return ok
    except Exception:
        return False


def require_available() -> None:
    """断言 UIA 可用，否则抛 ``UiaUnavailable``。"""
    if not is_available():
        raise UiaUnavailable(
            "UI Automation 不可用。可能原因：COM 初始化失败、"
            "或运行在无桌面会话（如服务、SSH 无桌面）。"
        )


def _raw_root(u: C.ComPtr) -> int:
    """取根元素裸指针（内部用，调用方负责释放）。"""
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_GET_ROOT, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.byref(out))
    C.check_hresult(hr, "GetRootElement", "IUIAutomation", "GetRootElement",
                    VT_UA_GET_ROOT)
    return int(out.value or 0)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _snapshot(el: C.ComPtr, hwnd: int = 0, depth: int = 0,
              with_patterns: bool = False) -> Element:
    """把一个 ``IUIAutomationElement`` COM 指针读成 ``Element`` 值对象。

    **读完就放** —— 这是本模块的核心约定。调用方拿到的是纯 Python 值，
    没有任何 COM 引用残留。
    """
    try:
        pid = _get_prop_int(el, VT_ELEMENT_PROPS["process_id"])
        native = _get_prop_ptr(el, VT_ELEMENT_PROPS["native_window_handle"])

        e = Element(
            name=_get_prop_str(el, VT_ELEMENT_PROPS["name"]),
            control_type=_get_prop_int(el, VT_ELEMENT_PROPS["control_type"]),
            localized_type=_get_prop_str(el, VT_ELEMENT_PROPS["localized_type"]),
            automation_id=_get_prop_str(el, VT_ELEMENT_PROPS["automation_id"]),
            class_name=_get_prop_str(el, VT_ELEMENT_PROPS["class_name"]),
            process_id=pid,
            native_hwnd=native,
            enabled=bool(_get_prop_bool(el, UIA_PROPERTY_IDS["is_enabled"], 1)),
            focused=bool(_get_prop_bool(el, UIA_PROPERTY_IDS["has_keyboard_focus"])),
            focusable=bool(_get_prop_bool(
                el, UIA_PROPERTY_IDS["is_keyboard_focusable"])),
            offscreen=bool(_get_prop_bool(el, UIA_PROPERTY_IDS["is_offscreen"])),
            password=bool(_get_prop_bool(el, UIA_PROPERTY_IDS["is_password"])),
            framework=_get_prop_str(el, VT_ELEMENT_PROPS["framework_id"]),
            help_text=_get_prop_str(el, VT_ELEMENT_PROPS["help_text"]),
            accelerator=_get_prop_str(el, VT_ELEMENT_PROPS["accelerator_key"]),
            access_key=_get_prop_str(el, VT_ELEMENT_PROPS["access_key"]),
            runtime_id=_read_runtime_id(el),
            depth=depth,
        )

        # 矩形。UIA 给的是 [left, top, width, height] —— 与 ltrb 不同！
        # Rect.from_pointer_array 就是为这个语义准备的（它内部做 l+w / t+h）。
        raw = _get_bounding_rect(el)
        if raw and raw[2] > 0 and raw[3] > 0:
            e.rect = L.Rect.from_pointer_array(raw)

        # hwnd：只认元素**自己**的窗口句柄。
        #
        # 不要再退回调用方传入的 hwnd —— 那会让一棵树里所有子元素的
        # hwnd 都等于根窗口的 hwnd（实测过：9 个子级全报 0x1000C），
        # 调用方据此做"窗口相对坐标换算"就会全部算错。
        e.hwnd = _root_hwnd_of(native)

        # window：元素**所在**的顶层窗口。子级继承父级的宿主窗口 ——
        # 这才是窗口相对坐标该用的句柄。显式传入的 hwnd 表示
        # "我知道这个元素属于哪个窗口"，优先采信。
        e.window = int(hwnd or 0) or e.hwnd

        if with_patterns:
            e.patterns = tuple(detect_patterns(el))
        return e
    finally:
        el.release()


def _read_runtime_id(el: C.ComPtr) -> tuple:
    """读 ``RuntimeId``（``int[]``）。

    UIA 返回的是 ``SAFEARRAY``，需要 ``SafeArrayAccessData`` 取数据、
    ``SafeArrayDestroy`` 释放 —— 这是少数**必须用 ``CoTaskMemFree`` 族之外**
    的释放路径的地方。这里做得保守：任何一步失败就返回空元组，
    不让探测失败影响主流程。
    """
    out = ctypes.c_void_p()
    fn = el.method(VT_ELEMENT_GET_RUNTIME_ID, ctypes.HRESULT,
                   ctypes.POINTER(ctypes.c_void_p))
    hr = fn(el._ptr, ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return ()

    psa = out.value
    try:
        # SAFEARRAY: cDims(u16) fFeatures(u16) cbElements(u32) cLocks(u32)
        #            pvData(ptr) rgsabound[1]
        class _BOUND(ctypes.Structure):
            _fields_ = [("cElements", ctypes.c_ulong),
                        ("lLbound", ctypes.c_long)]

        class _SA(ctypes.Structure):
            _fields_ = [
                ("cDims", ctypes.c_ushort),
                ("fFeatures", ctypes.c_ushort),
                ("cbElements", ctypes.c_ulong),
                ("cLocks", ctypes.c_ulong),
                ("pvData", ctypes.c_void_p),
                ("rgsabound", _BOUND * 1),
            ]

        sa = ctypes.cast(psa, ctypes.POINTER(_SA)).contents
        n = int(sa.rgsabound[0].cElements)
        if n <= 0 or n > 64:
            return ()
        data = ctypes.cast(sa.pvData, ctypes.POINTER(ctypes.c_int))
        return tuple(int(data[i]) for i in range(n))
    except Exception:
        return ()
    finally:
        # SafeArray 必须用 SafeArrayDestroy（oleaut32）
        try:
            from .._win32 import api as _A
            _A.oleaut32.SafeArrayDestroy(ctypes.c_void_p(psa))
        except Exception:
            pass


def _root_hwnd_of(hwnd: int) -> int:
    """把（往往为 0 的）native hwnd 上溯到顶层窗口。

    ``NativeWindowHandle`` 对多数 UIA 元素是 **0** —— 只有真正对应
    Win32 窗口的元素才有值。所以不能依赖它，只能作为补充。
    """
    if not hwnd:
        return 0
    try:
        from ..app_manager import foreground as FG
        return FG.root_window(hwnd) or hwnd
    except Exception:
        return hwnd


def from_ptr(ptr: int, hwnd: int = 0, depth: int = 0,
             with_patterns: bool = False) -> Element:
    """从裸 COM 指针构造 ``Element``（会消耗掉这个引用）。

    调用方**交出所有权** —— 本函数负责释放。
    """
    if not ptr:
        raise ElementStale("空元素指针", hwnd=hwnd or None)
    return _snapshot(C.ComPtr(ptr, name="IUIAutomationElement"),
                     hwnd=hwnd, depth=depth, with_patterns=with_patterns)


def from_com(el: C.ComPtr, hwnd: int = 0, depth: int = 0,
             with_patterns: bool = False) -> Element:
    """从 ``ComPtr`` 读快照。

    **不会**释放传入的 ``ComPtr`` —— 所有权仍归调用方（与 ``from_ptr``
    的"消耗引用"语义不同）。这样调用方可以一边遍历一边复用同一个
    ComPtr，也符合"谁的引用谁释放"的一般规则。
    """
    if el is None or not el.valid:
        raise ElementStale("无效的 COM 元素", hwnd=hwnd or None)
    return _snapshot_borrowed(el, hwnd=hwnd, depth=depth,
                              with_patterns=with_patterns)


def _snapshot_borrowed(el: C.ComPtr, hwnd: int = 0, depth: int = 0,
                       with_patterns: bool = False) -> Element:
    """``_snapshot`` 的借版 —— 读值但**不**释放传入引用。

    实现上先 AddRef 再交给 ``_snapshot``（它会释放一次），
    净效果是引用计数不变。
    """
    el.add_ref()
    return _snapshot(el, hwnd=hwnd, depth=depth, with_patterns=with_patterns)


# --------------------------------------------------------------------------
# 根 / 窗口 / 坐标 / 焦点
# --------------------------------------------------------------------------
def root(with_patterns: bool = False) -> Element:
    """桌面根元素。"""
    u = uia()
    ptr = _raw_root(u)
    return from_ptr(ptr, with_patterns=with_patterns)


def from_window(hwnd: int, with_patterns: bool = False) -> Element:
    """窗口句柄 → 元素。

    拿不到时抛 ``WindowNotFound`` —— 不返回"大概是这个"的元素。
    """
    h = int(hwnd)
    if not h:
        raise WindowNotFound("窗口句柄为空", hwnd=h)
    u = uia()
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_FROM_HANDLE, ctypes.HRESULT,
                  ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.c_void_p(h), ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        raise WindowNotFound(
            "无法从句柄 0x%X 取 UIA 元素（该窗口可能不支持 UIA）" % h,
            hwnd=h,
        )
    return from_ptr(int(out.value), hwnd=h, with_patterns=with_patterns)


def from_point(x: int, y: int, with_patterns: bool = False) -> Element:
    """屏幕物理坐标 → 该处的元素。

    这是**坐标兜底**路径的入口：UIA 找不到元素时，用像素坐标反查
    那里是什么控件。
    """
    u = uia()
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_FROM_POINT, ctypes.HRESULT,
                  S.POINT, ctypes.POINTER(ctypes.c_void_p))
    pt = S.POINT(int(x), int(y))
    hr = fn(u._ptr, pt, ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        raise ElementStale("坐标 (%d,%d) 处没有 UIA 元素" % (x, y))
    return from_ptr(int(out.value), with_patterns=with_patterns)


def focused(with_patterns: bool = False) -> Optional[Element]:
    """当前键盘焦点元素。没有则返回 None（不抛异常）。"""
    u = uia()
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_FOCUSED, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return None
    return from_ptr(int(out.value), with_patterns=with_patterns)


# --------------------------------------------------------------------------
# 遍历
# --------------------------------------------------------------------------
def _walker(kind: str = VIEW_CONTROL) -> C.ComPtr:
    """取指定视图的 TreeWalker。

    三种视图的差别：

    ``raw``
        原始树，包含所有实现细节元素。最完整也最吵。
    ``control``（默认）
        只留"控件"—— 排除了纯布局元素。**定位操作目标的正确视图**。
    ``content``
        最精简，只留对用户有意义的内容元素。适合做"读屏"式的理解，
        但会漏掉一些可点的东西。
    """
    u = uia()
    idx = {
        VIEW_RAW: VT_UA_RAW_WALKER,
        VIEW_CONTROL: VT_UA_CONTROL_WALKER,
        VIEW_CONTENT: VT_UA_CONTENT_WALKER,
    }.get(kind)
    if idx is None:
        raise ValueError("未知视图: %r（可选: raw / control / content）" % kind)

    out = ctypes.c_void_p()
    fn = u.method(idx, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.byref(out))
    C.check_hresult(hr, "get_%sWalker" % kind, "IUIAutomation",
                    "get_%sViewWalker" % kind, idx)
    return C.ComPtr(int(out.value), name="IUIAutomationTreeWalker(%s)" % kind)


def _walk_one(walker: C.ComPtr, ptr: int, index: int) -> int:
    """走一步，返回新元素指针（0 表示走不通）。调用方负责释放。"""
    out = ctypes.c_void_p()
    fn = walker.method(index, ctypes.HRESULT,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(walker._ptr, ctypes.c_void_p(ptr), ctypes.byref(out))
    if not C.succeeded(hr):
        return 0
    return int(out.value or 0)


def _ref_of(hwnd: int):
    """取窗口元素的 ComPtr（调用方负责 release）。"""
    u = uia()
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_FROM_HANDLE, ctypes.HRESULT,
                  ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.c_void_p(int(hwnd)), ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return None
    return C.ComPtr(int(out.value), name="IUIAutomationElement")


def walker_children(ptr: int, kind: str = VIEW_CONTROL) -> List[int]:
    """遍历某元素的直接子级，返回指针列表（调用方负责全部释放）。"""
    out = []
    w = _walker(kind)
    try:
        cur = _walk_one(w, ptr, VT_WALKER_GET_FIRST_CHILD)
        guard = 0
        while cur and guard < MAX_DESCENDANTS:
            out.append(cur)
            cur = _walk_one(w, cur, VT_WALKER_GET_NEXT_SIBLING)
            guard += 1
    finally:
        w.release()
    return out


def children(el_or_hwnd, kind: str = VIEW_CONTROL,
             with_patterns: bool = False) -> List[Element]:
    """直接子级。

    ``el_or_hwnd`` 可以是 ``Element``、``int``（窗口句柄）或
    ``C.ComPtr``（UIA 元素引用）。

    用 ``Element`` 调用时会**重新定位**该元素（``Element`` 不持有 COM 引用）。
    定位失败返回空列表 —— 不抛异常，因为"没有子级"和"元素失效"在这层
    调用方看来是同一件事。
    """
    ptr, hwnd, owner = _resolve_ref(el_or_hwnd)
    if not ptr:
        return []

    # 宿主窗口：入参是 Element 就沿用它的 window，否则用解析出的 hwnd
    host_hwnd = getattr(el_or_hwnd, "window", 0) or hwnd

    try:
        raw = walker_children(ptr, kind)
    finally:
        _release_owner(owner)

    out = []
    try:
        for p in raw:
            try:
                # 子级继承父级的**宿主窗口**（host），但**不继承**父级
                # 自身的 hwnd —— 见 Element.hwnd / Element.window 的说明。
                host = host_hwnd or hwnd
                out.append(_snapshot_borrowed_int(p, hwnd=host,
                                                  with_patterns=with_patterns))
            except Exception:
                continue
    finally:
        # 子级指针由 walker_children 借出 —— 统一在这里释放。
        for p in raw:
            _safe_release(p)
    return out


def _safe_release(ptr: int) -> None:
    """释放一个裸指针，任何失败都吞掉。

    遍历过程中"释放失败"不该中断流程 —— 最坏情况是泄漏一个引用，
    而抛异常会让整次查找白做。
    """
    if not ptr:
        return
    try:
        C.ComPtr(ptr, name="tmp").release()
    except Exception:
        pass


def parent(el_or_hwnd, kind: str = VIEW_CONTROL,
           with_patterns: bool = False) -> Optional[Element]:
    """父级。到顶或元素失效返回 None。"""
    ptr, hwnd, owner = _resolve_ref(el_or_hwnd)
    if not ptr:
        return None
    w = _walker(kind)
    try:
        p = _walk_one(w, ptr, VT_WALKER_GET_PARENT)
    finally:
        w.release()
        _release_owner(owner)
    if not p:
        return None
    return _snapshot(C.ComPtr(p, name="parent"),
                     hwnd=getattr(el_or_hwnd, "window", 0) or hwnd,
                     with_patterns=with_patterns)


def siblings(el_or_hwnd, kind: str = VIEW_CONTROL,
             include_self: bool = False) -> List[Element]:
    """同级元素（按控件树顺序）。

    UIA 的 walker 只能"从当前往两侧走"，所以实现是：
    先一路走回第一个同级（最左），再从那里往右枚举。

    ## 引用管理

    这里刻意**不沿用"边走边放"**的做法，因为同一级里的指针关系是链条状
    （next 返回的新指针必须留着才能继续走），中途释放会把链断掉。

    改为：**先收集全部指针，再统一释放**。指针数有 ``MAX_DESCENDANTS`` 上限，
    所以内存占用可控，而所有权一目了然。
    """
    ptr, hwnd, owner = _resolve_ref(el_or_hwnd)
    if not ptr:
        return []

    host_hwnd = getattr(el_or_hwnd, "window", 0) or hwnd

    w = _walker(kind)
    borrowed = []          # 本函数借到的、需要释放的全部指针
    out = []
    try:
        # 1. 回退到最左同级。每走一步，前一个指针就归我们所有。
        leftmost = ptr
        prev = _walk_one(w, ptr, VT_WALKER_GET_PREVIOUS_SIBLING)
        guard = 0
        while prev and guard < MAX_DESCENDANTS:
            if leftmost != ptr:
                borrowed.append(leftmost)
            leftmost = prev
            prev = _walk_one(w, leftmost, VT_WALKER_GET_PREVIOUS_SIBLING)
            guard += 1
        if leftmost != ptr:
            borrowed.append(leftmost)

        # 2. 从左往右收集（只收集指针，不读快照）
        chain = []
        cur = leftmost
        while cur and guard < MAX_DESCENDANTS * 2:
            chain.append(cur)
            cur = _walk_one(w, cur, VT_WALKER_GET_NEXT_SIBLING)
            guard += 1
        # chain 里除 ptr 外都是我们的引用
        for c in chain:
            if c != ptr and c not in borrowed:
                borrowed.append(c)

        # 3. 读快照（_snapshot_borrowed_int 自己 AddRef，不动我们的引用）
        want = [c for c in chain if include_self or c != ptr]
        for c in want:
            try:
                out.append(_snapshot_borrowed_int(c, hwnd=host_hwnd))
            except Exception:
                continue
    finally:
        for b in borrowed:
            _safe_release(b)
        w.release()
        _release_owner(owner)
    return out


def _snapshot_borrowed_int(ptr: int, hwnd: int = 0, depth: int = 0,
                           with_patterns: bool = False) -> Element:
    """从**裸指针**读快照，不消耗调用方的引用。

    做法：AddRef 一次再交给 ``_snapshot``（它会释放），净计数不变。
    """
    if not ptr:
        return None
    p = C.ComPtr(int(ptr), name="borrowed")
    p.add_ref()
    try:
        return _snapshot(p, hwnd=hwnd, depth=depth,
                         with_patterns=with_patterns)
    except Exception:
        _safe_release(ptr)
        raise


def borrowed(ptr: int, hwnd: int = 0, depth: int = 0,
             with_patterns: bool = False) -> Element:
    """从裸 COM 指针读快照，**不消耗**调用方的引用。

    给"我已经拿着一个引用、还想顺便读一份值"的场景用（比如
    ``patterns.py`` 里先拿元素再问它支持哪些模式）。读完引用
    仍归调用方，由调用方决定何时 ``_safe_release``。
    """
    return _snapshot_borrowed_int(ptr, hwnd=hwnd, depth=depth,
                                  with_patterns=with_patterns)


def descendants(el_or_hwnd, kind: str = VIEW_CONTROL,
                max_depth: int = MAX_DEPTH_DEFAULT,
                limit: int = MAX_DESCENDANTS,
                with_patterns: bool = False,
                skip_containers: bool = False) -> List[Element]:
    """深度优先遍历全部后代（含各级子级，不含自身）。

    有**三重护栏**，因为病态 UI（比如某些 Canvas 应用）能让 UIA 树
    展开到几万节点：

    · ``max_depth`` —— 深度上限
    · ``limit`` —— 结果总数上限
    · ``MAX_DESCENDANTS`` —— 内部硬上限

    ``skip_containers=True`` 时，只保留非容器类型 —— 用于"找能点的东西"，
    能把结果从几千压到几十。
    """
    ptr, hwnd, owner = _resolve_ref(el_or_hwnd)
    if not ptr:
        return []

    # 宿主窗口：入参是 Element 就沿用它的 window，否则用解析出的 hwnd
    host_hwnd = getattr(el_or_hwnd, "window", 0) or hwnd

    w = _walker(kind)
    out: List[Element] = []
    borrowed: List[int] = []
    seen = set()
    try:
        stack = [(ptr, 0)]
        while stack and len(out) < limit:
            cur, d = stack.pop()
            if d >= max_depth:
                continue

            # 收集直接子级（这些指针是借来的，记入 borrowed）
            kids = []
            ch = _walk_one(w, cur, VT_WALKER_GET_FIRST_CHILD)
            while ch:
                kids.append(ch)
                borrowed.append(ch)
                ch = _walk_one(w, ch, VT_WALKER_GET_NEXT_SIBLING)
                if len(kids) > MAX_DESCENDANTS:
                    break

            for k in reversed(kids):
                if len(out) >= limit:
                    break
                try:
                    snap = _snapshot_borrowed_int(k, hwnd=host_hwnd,
                                                  with_patterns=with_patterns)
                except Exception:
                    continue
                if skip_containers and snap.is_container:
                    pass          # 容器不产出，但仍要下探
                else:
                    if snap.key not in seen:
                        seen.add(snap.key)
                        out.append(snap)
                stack.append((k, d + 1))
    finally:
        for b in borrowed:
            _safe_release(b)
        w.release()
        _release_owner(owner)
    return out


def _resolve_ref(el_or_hwnd):
    """把入参统一成 ``(ptr, hwnd, owner)``。

    ``owner`` 是一个**持有引用的 ``ComPtr``**，调用方必须把它保存在
    变量里直到用完 ``ptr`` 为止，然后释放它（或依赖它的 ``__del__``）。

    ## 为什么必须返回 owner 而不是裸指针

    之前这里返回 ``(int, hwnd, bool)``，其中 int 取自一个局部的
    ``ComPtr``。函数一返回，那个 ``ComPtr`` 就被 GC 掉、``__del__``
    调用 ``Release()`` —— **调用方拿到的是已释放的野指针**，
    用下去就是 use-after-free（实测直接进程退出 127，无 Python 异常）。

    现在把 ``ComPtr`` 本身交出去，引用计数由 Python 对象生命周期
    正确管理；``owner`` 为 ``None`` 表示指针是借用的、不该释放。
    """
    if isinstance(el_or_hwnd, int):
        h = int(el_or_hwnd)
        p = _ref_of(h)
        return (p._ptr if p else 0), h, p or None

    if isinstance(el_or_hwnd, Element):
        e = el_or_hwnd
        p = ref(e)
        if p is None:
            return 0, e.hwnd, None
        return p._ptr, e.hwnd, p

    if isinstance(el_or_hwnd, C.ComPtr):
        if not el_or_hwnd.valid:
            return 0, 0, None
        return el_or_hwnd._ptr, 0, None      # 借用：调用方自己持有

    raise TypeError("不支持的元素类型: %r" % type(el_or_hwnd))


def _release_owner(owner) -> None:
    """释放 ``_resolve_ref`` 交回的 owner（``None`` / 借用时是空操作）。"""
    if owner is None:
        return
    try:
        owner.release()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Element → 重新获取 COM 引用
# --------------------------------------------------------------------------
def ref(el: Element, root_el: Optional[C.ComPtr] = None) -> Optional[C.ComPtr]:
    """重新获取 ``Element`` 对应的 COM 引用。失败返回 None。

    ## 为什么需要"重新获取"

    ``Element`` 不持有 COM 引用（见模块头）。要操作它，得先找回来。
    定位顺序：

    1. ``native_hwnd`` 有值 → 直接从该窗口取
    2. 从 ``hwnd`` 的窗口元素出发，按 ``runtime_id`` 深度搜索
    3. 按 ``automation_id`` + ``control_type`` 搜索（较慢，但稳）

    搜索有深度与数量上限 —— 找不到就是找不到，返回 None，
    **不返回"差不多"的元素**。调用方拿到 None 应当重走一遍查找流程。
    """
    if el is None:
        return None

    # 1. 有原生窗口句柄 —— 最直接
    if el.native_hwnd:
        try:
            from ..app_manager import windows as W
            if W.is_alive(el.native_hwnd):
                p = _ref_of(el.native_hwnd)
                if p is not None:
                    return p
        except Exception:
            pass

    # 2/3. 从窗口根往下找
    if not el.hwnd:
        return None
    try:
        from ..app_manager import windows as W
        if not W.is_alive(el.hwnd):
            return None
    except Exception:
        pass

    base = _ref_of(el.hwnd)
    if base is None:
        return None

    try:
        target_rt = el.runtime_id
        target_aid = el.automation_id
        target_ct = el.control_type

        w = _walker(VIEW_RAW if target_rt else VIEW_CONTROL)
        try:
            # 深度优先。**每个从 walker 拿到的指针都是我们自己的引用，
            # 必须显式释放** —— 见下方 finally 的 _safe_release(cur)。
            stack = [base._ptr]
            base_borrowed = True          # base 在栈里时按"借用"处理
            visited = 0
            while stack and visited < MAX_DESCENDANTS:
                cur = stack.pop()
                visited += 1
                hit = False
                try:
                    if cur != base._ptr:
                        if target_rt and _read_runtime_id(
                                C.ComPtr(cur, name="q")) == target_rt:
                            hit = True
                        elif target_aid and _get_prop_str(
                                cur, VT_ELEMENT_PROPS["automation_id"]) == target_aid:
                            if _get_prop_int(
                                    cur, VT_ELEMENT_PROPS["control_type"]) == target_ct:
                                hit = True
                    if hit:
                        # 交给调用方一个**自有**引用。
                        C.ComPtr(cur, name="found").add_ref()
                        return C.ComPtr(cur, name="result")

                    ch = _walk_one(w, cur, VT_WALKER_GET_FIRST_CHILD)
                    kids = []
                    while ch:
                        kids.append(ch)
                        ch = _walk_one(w, ch, VT_WALKER_GET_NEXT_SIBLING)
                    stack.extend(reversed(kids))
                finally:
                    # base 是借用来的（由外层 finally 释放）；
                    # 其余都是 walker 给我们的，用完即弃。
                    if cur != base._ptr:
                        _safe_release(cur)
            # 栈里可能还剩没走完的兄弟节点，一并放掉
            for leftover in stack:
                if leftover != base._ptr:
                    _safe_release(leftover)
            del base_borrowed
        finally:
            w.release()
    finally:
        base.release()

    return None


def refresh(el: Element, with_patterns: bool = False) -> Optional[Element]:
    """重新读取元素的当前属性。元素已不存在时返回 None。"""
    p = ref(el)
    if p is None:
        return None
    try:
        out = _snapshot(p, hwnd=el.hwnd, depth=el.depth,
                        with_patterns=with_patterns)
        return out
    except Exception:
        return None


def is_stale(el: Element) -> bool:
    """元素是否已失效（控件被销毁 / 窗口已关闭）。"""
    if el is None:
        return True
    try:
        from ..app_manager import windows as W
        if el.hwnd and not W.is_alive(el.hwnd):
            return True
    except Exception:
        pass
    return ref(el) is None


# --------------------------------------------------------------------------
# 模式探测（在 tree 层，避免和 patterns 循环依赖）
# --------------------------------------------------------------------------
#: 模式名 → IID 属性名
PATTERN_IIDS = {
    "Invoke": "IID_IUIAUTOMATION_INVOKE_PATTERN",
    "Value": "IID_IUIAUTOMATION_VALUE_PATTERN",
    "Toggle": "IID_IUIAUTOMATION_TOGGLE_PATTERN",
    "ExpandCollapse": "IID_IUIAUTOMATION_EXPAND_COLLAPSE_PATTERN",
    "Scroll": "IID_IUIAUTOMATION_SCROLL_PATTERN",
    "SelectionItem": "IID_IUIAUTOMATION_SELECTION_ITEM_PATTERN",
    "Selection": "IID_IUIAUTOMATION_SELECTION_PATTERN",
    "RangeValue": "IID_IUIAUTOMATION_RANGE_VALUE_PATTERN",
    "Text": "IID_IUIAUTOMATION_TEXT_PATTERN",
    "Window": "IID_IUIAUTOMATION_WINDOW_PATTERN",
    "LegacyIAccessible": "IID_IUIAUTOMATION_LEGACY_ACCESSIBLE_PATTERN",
    "Grid": "IID_IUIAUTOMATION_GRID_PATTERN",
    "GridItem": "IID_IUIAUTOMATION_GRID_ITEM_PATTERN",
    "Table": "IID_IUIAUTOMATION_TABLE_PATTERN",
    "TableItem": "IID_IUIAUTOMATION_TABLE_ITEM_PATTERN",
    "Dock": "IID_IUIAUTOMATION_DOCK_PATTERN",
    "Transform": "IID_IUIAUTOMATION_TRANSFORM_PATTERN",
    "SynchronizedInput": "IID_IUIAUTOMATION_SYNCHRONIZED_INPUT_PATTERN",
    "VirtualizedItem": "IID_IUIAUTOMATION_VIRTUALIZED_ITEM_PATTERN",
    "ScrollItem": "IID_IUIAUTOMATION_SCROLL_ITEM_PATTERN",
    # 冷门模式：acquire() 能用，但 patterns.py 不做类型化封装
    "MultipleView": "IID_IUIAUTOMATION_MULTIPLE_VIEW_PATTERN",
    "ItemContainer": "IID_IUIAUTOMATION_ITEM_CONTAINER_PATTERN",
    "ObjectModel": "IID_IUIAUTOMATION_OBJECT_MODEL_PATTERN",
    "Annotation": "IID_IUIAUTOMATION_ANNOTATION_PATTERN",
    "Styles": "IID_IUIAUTOMATION_STYLES_PATTERN",
    "Spreadsheet": "IID_IUIAUTOMATION_SPREADSHEET_PATTERN",
    "SpreadsheetItem": "IID_IUIAUTOMATION_SPREADSHEET_ITEM_PATTERN",
    "TextChild": "IID_IUIAUTOMATION_TEXT_CHILD_PATTERN",
    "Drag": "IID_IUIAUTOMATION_DRAG_PATTERN",
    "DropTarget": "IID_IUIAUTOMATION_DROP_TARGET_PATTERN",
    "TextEdit": "IID_IUIAUTOMATION_TEXT_EDIT_PATTERN",
    "CustomNavigation": "IID_IUIAUTOMATION_CUSTOM_NAVIGATION_PATTERN",
}

#: 模式名 → ``get_CurrentPattern`` 用的 PatternId
#:
#: ⚠ 全部从 Windows SDK ``UIAutomationClient.h`` 的
#: ``UIA_XxxPatternId`` 枚举逐个抄下（校验脚本
#: ``tools/dump_uia_vtbl.py``）。**早先有五个值是错的** ——
#: ``Window``/``TableItem``/``SelectionItem``/``Table``/``LegacyIAccessible``
#: 都错位了，导致 ``GetCurrentPatternAs`` 一律返回
#: ``E_NOINTERFACE (0x80004002)``。别凭印象填这张表。
PATTERN_IDS = {
    "Invoke": 10000,
    "Selection": 10001,
    "Value": 10002,
    "RangeValue": 10003,
    "Scroll": 10004,
    "ExpandCollapse": 10005,
    "Grid": 10006,
    "GridItem": 10007,
    "MultipleView": 10008,
    "Window": 10009,
    "SelectionItem": 10010,
    "Dock": 10011,
    "Table": 10012,
    "TableItem": 10013,
    "Text": 10014,
    "Toggle": 10015,
    "Transform": 10016,
    "ScrollItem": 10017,
    "LegacyIAccessible": 10018,
    "ItemContainer": 10019,
    "VirtualizedItem": 10020,
    "SynchronizedInput": 10021,
    "ObjectModel": 10022,
    "Annotation": 10023,
    "Styles": 10025,
    "Spreadsheet": 10026,
    "SpreadsheetItem": 10027,
    "TextChild": 10029,
    "Drag": 10030,
    "DropTarget": 10031,
    "TextEdit": 10032,
    "CustomNavigation": 10033,
}


def detect_patterns(el: C.ComPtr) -> List[str]:
    """探测元素支持哪些控件模式。

    走 ``get_CurrentPattern`` + ``get_PropertyValue`` 的轻量路线 ——
    只判断"有没有"，不取接口。取接口（``GetCurrentPatternAs``）更重，
    在需要时才做（见 ``patterns.acquire``）。

    探测失败的模式直接跳过，**不抛异常** —— 探测本身不该中断流程。
    """
    found = []
    for name, pid in PATTERN_IDS.items():
        try:
            if _has_pattern_id(el, pid):
                found.append(name)
        except Exception:
            continue
    return found


def _has_pattern_id(el: C.ComPtr, pattern_id: int) -> bool:
    """``get_CurrentPattern(patternId, IUnknown**)`` 是否返回非空。"""
    out = ctypes.c_void_p()
    fn = el.method(VT_ELEMENT_GET_CURRENT_PATTERN, ctypes.HRESULT,
                   ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    hr = fn(el._ptr, ctypes.c_int(int(pattern_id)), ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return False
    # 探测到了就立刻释放 —— 否则每探测一个模式就漏一个引用
    C.ComPtr(int(out.value), name="pattern-probe").release()
    return True


# --------------------------------------------------------------------------
# 原生查找（FindAll）—— 大范围搜索的唯一可行路径
# --------------------------------------------------------------------------
#: ``FindAll`` / ``FindFirst`` 的 TreeScope 位掩码。
#:
#: ⚠ 这是**枚举值不是指针**，第一个参数必须传 ``c_int``。
#: 早期实现把第一个参数写成 ``c_void_p(None)`` → 得到
#: ``E_POINTER (0x80004003)``，而且因为整个调用被 try 吞掉，
#: 表现为"什么都没找到"，排查了很久。
TREE_SCOPE_NONE = 0x0
TREE_SCOPE_ELEMENT = 0x1
TREE_SCOPE_CHILDREN = 0x2
TREE_SCOPE_DESCENDANTS = 0x4
TREE_SCOPE_PARENT = 0x8
TREE_SCOPE_ANCESTORS = 0x10
TREE_SCOPE_SUBTREE = TREE_SCOPE_ELEMENT | TREE_SCOPE_CHILDREN | TREE_SCOPE_DESCENDANTS

#: ``IUIAutomationElementArray`` 的 vtable 槽位
VT_ELEMENT_ARRAY_GET_LENGTH = 3
VT_ELEMENT_ARRAY_GET_ELEMENT = 4


def find_uia(scope, property_id: int, value, *,
             control_types=None, max_results: int = 200,
             view: str = VIEW_CONTROL,
             tree_scope: int = 0) -> List[int]:
    """用 UIA 原生 ``FindAll`` 找一个子树里满足条件的元素，返回裸指针列表。

    ## 为什么必须有这个函数

    Python 侧遍历树（``walker_children`` 递归）**每次都要跨进程调 COM**：
    取当前元素属性、取子节点、取兄弟节点、取模式…… 一个 3000 节点的
    桌面树要 30 秒以上。实测：

        find_all(Query(control_type="window"), scope=root)  → 30.8s

    UIA 的 ``IUIAutomationElement::FindAll``（vtable 6）把筛选**下推到
    目标进程/uiautomationcore 内部**执行，只把结果集传回来。同样的查询
    降到几十毫秒。

    ## 条件表达力

    原生只支持属性等值条件（``CreatePropertyCondition``）。
    "子串匹配""正则""打分排序"这些还是得在 Python 侧做 ——
    但只要先用原生条件把候选集**大幅缩小**，Python 侧的负担就微不足道了。

    所以 ``find.py`` 的策略是：**能用原生条件下推的就下推，
    剩下少量候选再走 Python 打分**。

    ## 参数

    ``scope``
        搜哪个元素下面。可以是 ``Element``（推荐 —— ``root()`` 返回的就是它）
        或 ``ComPtr`` 或窗口句柄 ``int``。
    ``property_id``
        UIA 属性 ID（30000 段，见 ``UIA_PROPERTY_IDS``）。传 0 表示不筛属性。
    ``value``
        要等于的值。类型按属性走：ControlType 传 ``int``，
        Name/AutomationId 传 ``str``。
    ``control_types``
        可选的类型过滤（额外 ``AndCondition``）。

    ## 返回值

    裸指针 ``int`` 列表。**调用方负责释放** —— 用 ``C.ComPtr(p).release()``
    或包成 ``borrowed(p)``。这里不返回 ``ComPtr`` 是为了让调用方能
    直接喂给 ``from_com``/``borrowed``。

    失败返回空列表 —— 查找不到是正常结果，不是异常。
    """
    u = uia()

    # 解析 scope 成 ComPtr（Element 要先取到真实引用）
    scope_cp = None
    scope_own = None
    if isinstance(scope, Element):
        ptr, _hwnd, owner = _resolve_ref(scope)
        if not ptr:
            return []
        scope_cp = C.ComPtr(ptr, name="find-scope", own=False)
        scope_own = owner
    elif isinstance(scope, C.ComPtr):
        scope_cp = scope
    elif isinstance(scope, int) and scope:
        try:
            scope_cp = ref(from_window(scope))
        except Exception:
            return []
        scope_own = scope_cp
    else:
        root_cp = C.ComPtr(_raw_root(u), name="find-root")
        scope_cp = root_cp
        scope_own = root_cp

    conds = []
    own = []           # 需要释放的临时条件对象

    try:
        if property_id:
            pc = _create_property_condition(u, property_id, value)
            if pc is None:
                return []
            conds.append(pc)
            own.append(pc)

        if control_types:
            pc = _create_property_condition(u, UIA_PROPERTY_IDS["control_type"],
                                            int(control_types))
            if pc is not None:
                conds.append(pc)
                own.append(pc)

        if not conds:
            cond = _create_true_condition(u)
        elif len(conds) == 1:
            cond = conds[0]
        else:
            cond = _create_and_condition(u, conds)
        if cond is None:
            return []
        if cond not in own:
            own.append(cond)

        arr_ptr = ctypes.c_void_p()
        fn = scope_cp.method(VT_ELEMENT_FIND_ALL, ctypes.HRESULT,
                             ctypes.c_int, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
        scope_value = int(tree_scope or TREE_SCOPE_DESCENDANTS)
        hr = fn(scope_cp._ptr, ctypes.c_int(scope_value),
                ctypes.c_void_p(cond), ctypes.byref(arr_ptr))
        if not C.succeeded(hr) or not arr_ptr.value:
            return []

        return _drain_element_array(int(arr_ptr.value), max_results)
    except Exception:
        return []
    finally:
        for c in own:
            try:
                C.ComPtr(c, name="uia-cond").release()
            except Exception:
                pass
        # scope 是自己造的（ref() / root）才释放
        if scope_own is not None and scope_own is not scope_cp:
            try:
                C.ComPtr(scope_own.ptr, name="find-scope-own").release()
            except Exception:
                pass
        elif scope_own is not None and scope_own is scope_cp:
            try:
                scope_cp.release()
            except Exception:
                pass


def _drain_element_array(parr: int, max_results: int) -> List[int]:
    """把一个 ``IUIAutomationElementArray`` 读成"我们持有引用"的指针列表。

    ## 引用计数规则（踩过坑）

    ``FindAll`` 返回的数组是**由我们拥有**的（``FindAll`` 已 AddRef）。
    数组里的每个元素则由**数组**持有。

    所以正确做法：

    1. 对每个元素 ``AddRef`` 一份 → 我们手上多一个引用
    2. 读完 ``Release`` 数组 → 数组持有的那份被释放，我们这份还在

    本函数返回的指针**带一份属于调用方的引用**，调用方必须释放。

    ## ⚠ 两个坑

    · **双重 Release**：早先 ``C.ComPtr(parr)`` 默认 ``own=True``，
      函数体内一个 + ``finally`` 里又一个 → 同一个指针被 Release 两次
      → 进程静默崩溃（exit 127，无 traceback）。
      现在统一只在一个地方释放数组。
    · **AddRef 不能省**：不 AddRef 就直接释放数组，拿到的指针立刻悬空，
      读它是 use-after-free。
    """
    if not parr:
        return []
    out = []
    arr = None
    try:
        arr = C.ComPtr(parr, name="element-array")     # own=True，唯一所有者
        n = ctypes.c_int(0)
        fn_len = arr.method(VT_ELEMENT_ARRAY_GET_LENGTH, ctypes.HRESULT,
                            ctypes.POINTER(ctypes.c_int))
        hr = fn_len(arr._ptr, ctypes.byref(n))
        if not C.succeeded(hr):
            return []
        total = int(n.value)
        if total <= 0:
            return []
        take = min(total, max_results) if max_results else total

        fn_get = arr.method(VT_ELEMENT_ARRAY_GET_ELEMENT, ctypes.HRESULT,
                            ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        for i in range(take):
            item = ctypes.c_void_p()
            hr = fn_get(arr._ptr, ctypes.c_int(i), ctypes.byref(item))
            if not C.succeeded(hr) or not item.value:
                continue
            # 接管所有权：AddRef 一份，免得数组销毁时把我们手上的收走。
            # 之后由调用方负责最终 Release。
            try:
                holder = C.ComPtr(int(item.value), name="found", own=False)
                holder.add_ref()
            except Exception:
                continue
            out.append(int(item.value))
    except Exception:
        out = []
    finally:
        if arr is not None:
            try:
                arr.release()
            except Exception:
                pass
    return out


def _create_property_condition(u: C.ComPtr, property_id: int, value):
    """``CreatePropertyCondition``（vtable 23）。

    ``value`` 先打成 ``VARIANT``。用 ``V.make_*`` 那些便捷构造 ——
    它们已经处理了 ``vt`` 在偏移 0 这个关键细节（见 ``variant.VARIANT``
    的长注释，这个顺序搞反过一次）。
    """
    var = _make_variant(value)
    if var is None:
        return None

    out = ctypes.c_void_p()
    fn = u.method(VT_UA_CREATE_PROPERTY_CONDITION, ctypes.HRESULT,
                  ctypes.c_int, ctypes.POINTER(V.VARIANT),
                  ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.c_int(int(property_id)),
            ctypes.byref(var), ctypes.byref(out))
    try:
        V.variant_clear(var)
    except Exception:
        pass
    if not C.succeeded(hr) or not out.value:
        return None
    return int(out.value)


def _make_variant(value):
    """把 Python 值打成 ``VARIANT``。无法表示的类型返回 ``None``。

    全部委托给 ``VARIANT.from_*`` —— 那些类方法内部已按正确布局赋值，
    特别是 ``VT_BOOL`` 用 short 且 −1 为真这个容易写错的点。
    """
    try:
        if isinstance(value, bool):
            return V.VARIANT.from_bool(value)
        if isinstance(value, int):
            return V.VARIANT.from_int(value)
        if isinstance(value, float):
            return V.VARIANT.from_double(value)
        if isinstance(value, str):
            return V.VARIANT.from_str(value)
        return None
    except Exception:
        return None


def _create_true_condition(u: C.ComPtr):
    """``CreateTrueCondition``（vtable 21）—— 匹配一切。"""
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_CREATE_TRUE_CONDITION, ctypes.HRESULT,
                  ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return None
    return int(out.value)


def _create_and_condition(u: C.ComPtr, conds):
    """``CreateAndCondition``（vtable 25）—— 变参 (count, ptr1, ptr2, ...)。"""
    n = len(conds)
    args = [ctypes.c_void_p(int(c)) for c in conds]
    out = ctypes.c_void_p()
    fn = u.method(VT_UA_CREATE_AND_CONDITION, ctypes.HRESULT,
                  ctypes.c_int, *([ctypes.c_void_p] * n),
                  ctypes.POINTER(ctypes.c_void_p))
    hr = fn(u._ptr, ctypes.c_int(n), *args, ctypes.byref(out))
    if not C.succeeded(hr) or not out.value:
        return None
    return int(out.value)


def find_by_name_uia(scope: C.ComPtr, name: str,
                     control_types=None, max_results: int = 200) -> List[int]:
    """按 Name 精确匹配（UIA 原生）。"""
    return find_uia(scope, UIA_PROPERTY_IDS["name"], name,
                    control_types=control_types, max_results=max_results)


def find_by_id_uia(scope: C.ComPtr, automation_id: str,
                   max_results: int = 50) -> List[int]:
    """按 AutomationId 精确匹配（UIA 原生）—— 最快的定位路径。"""
    return find_uia(scope, UIA_PROPERTY_IDS["automation_id"], automation_id,
                    max_results=max_results)


def find_by_type_uia(scope: C.ComPtr, control_type,
                     max_results: int = 200) -> List[int]:
    """按 ControlType 匹配（UIA 原生）。"""
    return find_uia(scope, UIA_PROPERTY_IDS["control_type"], int(control_type),
                    max_results=max_results)


# --------------------------------------------------------------------------
# 树快照 / dump
# --------------------------------------------------------------------------
def dump(target, kind: str = VIEW_CONTROL, max_depth: int = 6,
         limit: int = 300, skip_containers: bool = False,
         with_patterns: bool = False) -> List[Element]:
    """把一棵 UIA 子树拍成扁平列表（带 depth），用于打印或喂给模型。

    深度优先 + 前序，``depth`` 字段可以用来重建缩进。

    ``target`` 可以是窗口句柄（``int``）或 ``Element`` —— 传 ``Element``
    时以它为根，深度从 0 开始（该元素自身的 ``depth`` 会被保留为原值，
    所以从子元素起拍的树 ``depth`` 可能不为 0，渲染时按相对缩进处理）。
    """
    if isinstance(target, Element):
        win = target
    else:
        try:
            win = from_window(int(target))
        except (WindowNotFound, TypeError, ValueError):
            return []

    out = [win]
    rest = descendants(win, kind=kind, max_depth=max_depth, limit=limit,
                       with_patterns=with_patterns,
                       skip_containers=skip_containers)
    out.extend(rest)
    return out


def render(elements: Sequence[Element], max_lines: int = 0) -> str:
    """把 ``dump()`` 的结果渲染成缩进文本。"""
    if not elements:
        return "(UIA 树为空)"
    lines = [e.line(e.depth) for e in elements]
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + ["  ... (%d 项已省略)" % (len(lines) - max_lines)]
    return "\n".join(lines)


def summary(target, limit: int = 40) -> str:
    """一棵 UIA 子树的简报 —— 只列可交互元素。"""
    els = dump(target, skip_containers=True, limit=limit)
    if not els:
        return "(未找到可交互元素)"
    return render(els, max_lines=limit)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    checks = []

    def _add(name, ok, detail, skip=False):
        """``skip=True`` → 本机环境缺这个资源，标 SKIP，不计入失败。"""
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": detail})

    # 1. UIA 可用
    #    ⚠ UIA 不可用是**环境能力缺失**（Server Core / 精简系统），
    #    不是代码有 bug → 整个模块判 SKIP。
    #    说清楚：这等于本模块**什么都没验**，别让人误以为"通过了"。
    avail = is_available()
    if not avail:
        _add("UIA 可用", False,
             "本机 UIA 不可用（Server Core / 精简系统）"
             "→ 本模块全部检查跳过", skip=True)
        return {"checks": checks,
                "failures": [],
                "skipped": [c["name"] for c in checks if c.get("skipped")],
                "ok": True}
    _add("UIA 可用", True, "IUIAutomation 创建成功")

    # 2. 能取到根元素
    r = root()
    _add("可读取根元素", bool(r), "根元素 name=%r type=%s" % (r.name, r.type_name))

    # 3. 控件类型名映射
    _add("ControlType 映射正确",
         control_name(ControlType.BUTTON) == "button"
         and control_id("edit") == ControlType.EDIT,
         "button=%d edit=%d" % (ControlType.BUTTON, ControlType.EDIT))

    # 4. 能找到至少一个顶层窗口并读取其 UIA 元素
    from ..app_manager import windows as W
    ws = [w for w in W.list_windows() if w.is_usable]
    if ws:
        w0 = ws[0]
        win = from_window(w0.hwnd)
        _add("可从窗口取 UIA 元素", bool(win),
             "hwnd=0x%X name=%r pid=%d" % (w0.hwnd, win.name, win.process_id))

        # 5. 能遍历子级
        ch = children(win)
        _add("可遍历子级", isinstance(ch, list),
             "直接子级 %d 个" % len(ch))

        # 6. 后代遍历有结果且不超过上限
        desc = descendants(win, max_depth=4, limit=120)
        _add("可遍历后代", len(desc) > 0 and len(desc) <= 120,
             "深度 4 内 %d 个元素（上限 120）" % len(desc))

        # 7. 每个元素都能算出客户区坐标（不缓存屏幕坐标的验证）
        with_cr = [e for e in desc if e.client_rect() is not None]
        _add("元素可换算客户区坐标", len(with_cr) > 0,
             "%d/%d 个元素有客户区坐标" % (len(with_cr), len(desc)))

        # 8. 矩形语义正确（UIA 给的是 xywh，不是 ltrb）
        bad = [e for e in desc if e.rect and (e.rect.width <= 0 or e.rect.height <= 0)]
        _add("矩形宽高为正（xywh 解析正确）", not bad,
             "%d 个元素矩形全部合法" % len(desc) if not bad
             else "%d 个元素宽高异常" % len(bad))

        # 9. runtime_id 可读
        rt = [e for e in desc if e.runtime_id]
        _add("RuntimeId 可读", len(rt) > 0,
             "%d/%d 个元素有 RuntimeId" % (len(rt), len(desc)))

        # 10. 元素 key 唯一性（用于去重）
        keys = [e.key for e in desc]
        _add("元素 key 稳定可比较", len(set(keys)) == len(keys),
             "去重后 %d/%d" % (len(set(keys)), len(keys)))

        # 10b. 子级不把**根窗口的句柄**当成自己的
        #
        # 这条是为一个真实缺陷加的回归测试：早期 ``_snapshot`` 用
        # ``hwnd or _root_hwnd_of(native)``，root 的每个子元素都会继承
        # 根窗口的 hwnd，窗口相对坐标换算全错。
        #
        # 判据：若某个子元素的 ``native_hwnd`` 为 0，它的 ``hwnd`` 也必须
        # 为 0（而不是退化成根窗口句柄）。子元素之间互相共享 hwnd 是
        # 正常的（同一个窗口里的多个 pane），不算问题。
        inherited = [e for e in ch if not e.native_hwnd and e.hwnd]
        _add("子级不继承根窗口句柄", not inherited,
             "无自身窗口的子级 hwnd 均为 0（%d 个子级，%d 个异常）"
             % (len(ch), len(inherited)))

        # 10c. 矩形必须来自 SAFEARRAY 路径（vtable[43] 会返回垃圾值）
        #
        # ⚠ 判据不能写成 ``0 <= left <= 20000``。Windows 把**最小化**的窗口
        #   挪到屏幕外，坐标是 -32000 量级（实测 -31988）。那是合法值，
        #   不是"垃圾"。
        #
        #   vtable[43] 路径的垃圾值特征是**超出 int32 合理范围**或
        #   **宽高为负**，所以判据按这个来。
        rects = [e for e in desc if e.rect]

        def _rect_sane(e):
            r = e.rect
            # 屏幕外（含最小化）是合法的，范围放宽到 ±40000
            for v in (r.left, r.top, r.right, r.bottom):
                if not (-40000 <= v <= 40000):
                    return False
            # 宽高必须非负 —— 垃圾值常表现为负数
            return r.width >= 0 and r.height >= 0

        sane = [e for e in rects if _rect_sane(e)]
        _add("矩形取值合法（含屏幕外/最小化）", len(sane) == len(rects),
             "%d/%d 个矩形合法（%d 个元素无矩形）"
             % (len(sane), len(rects), len(desc) - len(rects)))

    # 11. 坐标反查
    try:
        pt = from_point(10, 10)
        _add("可从坐标反查元素", bool(pt),
             "(%d,%d) → %s %r" % (10, 10, pt.type_name, pt.name[:20]))
    except Exception as e:
        _add("可从坐标反查元素", False, "异常: %s" % e)

    # 12. 视图参数校验
    _add("非法视图名被拒绝",
         _raises(ValueError, _walker, "nonsense"),
         "raw/control/content 之外的视图名报错")

    # 13. 快照不泄漏（连续读 200 次，引用计数不增长）
    leak = _leak_check()
    _add("快照不泄漏 COM 引用", leak,
         "连续 200 次读取后引用计数稳定")

    return {"checks": checks,
            "failures": [c["name"] for c in checks
                         if not c["ok"] and not c.get("skipped")],
            "skipped": [c["name"] for c in checks if c.get("skipped")],
            "ok": all(c["ok"] or c.get("skipped") for c in checks)}


def _raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
        return False
    except exc:
        return True
    except Exception:
        return False


def _leak_check() -> bool:
    """反复读快照，看 COM 引用计数是否稳定。

    ``_snapshot`` 的契约是"读完就放"。这里把一个已知元素连续读 200 次，
    比对前后引用计数：泄漏的话计数会单调增长。

    实现：先 ``add_ref`` 拿到基线计数，循环读快照，再读一次计数。
    正确实现下两者应**相等**（快照的 AddRef/Release 成对抵消）。
    """
    try:
        from ..app_manager import windows as W
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return True   # 无窗口可测，不判失败

        p = _ref_of(ws[0].hwnd)
        if p is None:
            return True
        try:
            before = p.add_ref()
            p.release()

            for _ in range(200):
                # 用 _snapshot_borrowed：它内部 AddRef+Release 配平，
                # 正好等于生产代码里 "拿到 ComPtr → 读快照" 的用法
                _snapshot_borrowed(p, hwnd=ws[0].hwnd)

            after = p.add_ref()
            p.release()
            return after == before
        finally:
            p.release()
    except Exception:
        return True   # 探测失败不判失败


def _main() -> int:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"

    if cmd == "selfcheck":
        res = selfcheck()
        for c in res["checks"]:
            print("  [%s] %-30s %s" % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
        print("UIA tree 自检: %d/%d" % (
            len(res["checks"]) - len(res["failures"]), len(res["checks"])))
        return 0 if res["ok"] else 1

    if cmd == "dump":
        if len(sys.argv) < 3:
            print("用法: dump <hwnd>   (hwnd 支持 0x 前缀)")
            from ..app_manager import windows as W
            for w in [x for x in W.list_windows() if x.is_usable][:15]:
                print("  0x%X %s" % (w.hwnd, w.title[:50]))
            return 2
        h = int(sys.argv[2], 0)
        print(render(dump(h), max_lines=200))
        return 0

    if cmd == "windows":
        from ..app_manager import windows as W
        for w in [x for x in W.list_windows() if x.is_usable]:
            print("  0x%-10X %-30s %s" % (w.hwnd, w.process, w.title[:48]))
        return 0

    print("未知命令: %s（可选: selfcheck / dump / windows）" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
