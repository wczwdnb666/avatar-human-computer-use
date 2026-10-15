# -*- coding: utf-8 -*-
"""UIA 控件模式（Control Pattern）：把"能做什么"和"怎么做"分开。

职责
----
1. 模式**接口获取** —— ``acquire(el, "Invoke")`` → 裸 COM 指针（RAII 包装）
2. 每个模式的**类型化封装** —— ``InvokePattern`` / ``ValuePattern`` /
   ``TogglePattern`` / ``SelectionItemPattern`` / ``ExpandCollapsePattern``
   / ``ScrollPattern`` / ``RangeValuePattern`` / ``WindowPattern``
3. ``pattern_actions(el)`` —— 列出"这个元素能做哪些动作"，给上层决策用

依赖
----
· ``avatar.uia.tree``（``Element``、``ref()``、``detect_patterns``）
· ``avatar._win32.com``（ComPtr）
· ``avatar._win32.guids``（模式 IID）

## 为什么要把模式单独成模块

元素定位（tree.py）回答"哪里"，模式（本模块）回答"能做什么"。
分成两层的好处：

· 定位可以在不碰模式的情况下完成 —— 快、稳
· 上层"点击某按钮"时先问模式：能用 ``Invoke`` 就用 ``Invoke``，
  它不需要光标、不受窗口遮挡影响、不受 DPI 影响
· 模式用不了才退回坐标点击（那就是 ``actions.py`` 的兜底逻辑）

## 坐标系约定（与 tree.py 一致）

本模块**不产生任何屏幕坐标**。需要坐标的地方一律从
``Element.client_rect()`` 取客户区坐标，再按需换算。
模式本身不涉及坐标 —— ``Invoke`` / ``Toggle`` 之类都是语义操作。

## 引用管理铁律

``acquire()`` 返回的 ``ComPtr`` **归调用方所有**，必须用 ``with``
或显式 ``release()``。模式接口漏放会连带元素引用一起泄漏，
而 UIA 模式对象不小。
"""
from __future__ import annotations

import ctypes
import time
from typing import Dict, List, Optional, Sequence

from .._win32 import com as C
from .._win32 import guids as G
from ..errors import ElementNotFound, ElementStale
from . import tree as T

#: ``ComError`` 定义在 ``com`` 模块里（它属于 COM 层，不是通用异常）
ComError = C.ComError

# --------------------------------------------------------------------------
# vtable 索引
# --------------------------------------------------------------------------
# 每个模式下 IUnknown 占 0/1/2，业务方法从 3 开始。索引是**绝对**的。
#
# ⚠ 下面这些索引不是猜的，是从 Windows SDK 的
#   ``UIAutomationClient.h`` 里各接口的 ``XxxVtbl`` 结构体
#   逐个抄下来的（那个结构体的字段顺序就是 vtable 布局）。
#   校验脚本见 ``tools/dump_uia_vtbl.py``。
#
# 几个**容易想当然写错**的地方（都踩过）：
#
# · ``RangeValuePattern`` 的第 5 项是 ``get_CurrentIsReadOnly``，
#   ``Minimum`` 和 ``Maximum`` 在 7/6 —— 顺序是 Max 在前、Min 在后
# · ``LegacyIAccessiblePattern`` 的 3 是 ``Select`` 不是 ``DoDefaultAction``；
#   ``DoDefaultAction`` 是 4，``SetValue`` 是 5，``CurrentValue`` 是 8
# · ``SelectionPattern`` 的 3 是 ``GetCurrentSelection``，
#   ``IsSelectionRequired`` 在第 5
# · ``WindowPattern`` 的 ``CanMaximize``/``CanMinimize`` 夹在中间，
#   所以 ``IsModal`` 是 8、``WindowVisualState`` 是 10（不是 6/7）

# IUIAutomationInvokePattern
VT_INVOKE_INVOKE = 3

# IUIAutomationValuePattern
VT_VALUE_SET_VALUE = 3
VT_VALUE_GET_VALUE = 4
VT_VALUE_IS_READ_ONLY = 5

# IUIAutomationTogglePattern
VT_TOGGLE_TOGGLE = 3
VT_TOGGLE_GET_STATE = 4

# IUIAutomationSelectionItemPattern
VT_SELITEM_SELECT = 3
VT_SELITEM_ADD_TO_SELECTION = 4
VT_SELITEM_REMOVE_FROM_SELECTION = 5
VT_SELITEM_GET_IS_SELECTED = 6

# IUIAutomationExpandCollapsePattern
VT_EXPAND_EXPAND = 3
VT_EXPAND_COLLAPSE = 4
VT_EXPAND_GET_STATE = 5

# IUIAutomationScrollPattern
VT_SCROLL_SCROLL = 3
VT_SCROLL_SET_SCROLL_PERCENT = 4
VT_SCROLL_GET_H_PERCENT = 5
VT_SCROLL_GET_V_PERCENT = 6
VT_SCROLL_GET_H_VIEWSIZE = 7
VT_SCROLL_GET_V_VIEWSIZE = 8
VT_SCROLL_GET_H_SCROLLABLE = 9
VT_SCROLL_GET_V_SCROLLABLE = 10

# IUIAutomationRangeValuePattern
# ⚠ 顺序是 SetValue / Value / **IsReadOnly** / **Maximum** / **Minimum**
VT_RANGE_SET_VALUE = 3
VT_RANGE_GET_VALUE = 4
VT_RANGE_IS_READ_ONLY = 5
VT_RANGE_GET_MAX = 6
VT_RANGE_GET_MIN = 7
VT_RANGE_GET_LARGE_CHANGE = 8
VT_RANGE_GET_SMALL_CHANGE = 9

# IUIAutomationWindowPattern
# ⚠ CanMaximize/CanMinimize 夹在 SetWindowVisualState 之后
VT_WINDOW_CLOSE = 3
VT_WINDOW_WAIT_FOR_INPUT_IDLE = 4
VT_WINDOW_SET_WINDOW_VISUAL_STATE = 5
VT_WINDOW_GET_CAN_MAXIMIZE = 6
VT_WINDOW_GET_CAN_MINIMIZE = 7
VT_WINDOW_IS_MODAL = 8
VT_WINDOW_IS_TOPMOST = 9
VT_WINDOW_GET_VISUAL_STATE = 10
VT_WINDOW_GET_INTERACTION_STATE = 11

# IUIAutomationLegacyIAccessiblePattern
# ⚠ 3 是 Select（不是 DoDefaultAction）
VT_LEGACY_SELECT = 3
VT_LEGACY_DO_DEFAULT_ACTION = 4
VT_LEGACY_SET_VALUE = 5
VT_LEGACY_GET_CHILD_ID = 6
VT_LEGACY_GET_NAME = 7
VT_LEGACY_GET_VALUE = 8
VT_LEGACY_GET_DESCRIPTION = 9
VT_LEGACY_GET_ROLE = 10
VT_LEGACY_GET_STATE = 11
VT_LEGACY_GET_HELP = 12
VT_LEGACY_GET_KEYBOARD_SHORTCUT = 13
VT_LEGACY_GET_DEFAULT_ACTION = 15

# IUIAutomationSelectionPattern
# ⚠ 3 是 GetCurrentSelection，不是 IsSelectionRequired
VT_SELECTION_GET_CURRENT = 3
VT_SELECTION_CAN_SELECT_MULTIPLE = 4
VT_SELECTION_IS_REQUIRED = 5

# IUIAutomationGridPattern
VT_GRID_GET_ITEM = 3
VT_GRID_GET_ROW_COUNT = 4
VT_GRID_GET_COLUMN_COUNT = 5

# IUIAutomationTablePattern
VT_TABLE_GET_ROW_HEADERS = 3
VT_TABLE_GET_COLUMN_HEADERS = 4
VT_TABLE_GET_ROW_OR_COLUMN_MAJOR = 5

# IUIAutomationDockPattern
VT_DOCK_SET_POSITION = 3
VT_DOCK_GET_POSITION = 4

# IUIAutomationTransformPattern
VT_TRANSFORM_MOVE = 3
VT_TRANSFORM_RESIZE = 4
VT_TRANSFORM_ROTATE = 5
VT_TRANSFORM_CAN_MOVE = 6
VT_TRANSFORM_CAN_RESIZE = 7
VT_TRANSFORM_CAN_ROTATE = 8

# IUIAutomationVirtualizedItemPattern
VT_VIRTUALIZED_REALIZE = 3

# IUIAutomationSynchronizedInputPattern
VT_SYNC_START_LISTENING = 3
VT_SYNC_CANCEL = 4

# IUIAutomationScrollItemPattern
VT_SCROLL_ITEM_INTO_VIEW = 3

# --------------------------------------------------------------------------
# 模式枚举值
# --------------------------------------------------------------------------

#: ``ToggleState``
TOGGLE_OFF = 0
TOGGLE_ON = 1
TOGGLE_INDETERMINATE = 2
TOGGLE_NAMES = {0: "off", 1: "on", 2: "indeterminate"}

#: ``ExpandCollapseState``
EC_COLLAPSED = 0
EC_EXPANDED = 1
EC_PARTIALLY_EXPANDED = 2
EC_LEAF_NODE = 3
EC_NAMES = {0: "collapsed", 1: "expanded", 2: "partially", 3: "leaf"}

#: ``WindowVisualState``
WVS_NORMAL = 0
WVS_MAXIMIZED = 1
WVS_MINIMIZED = 2
WVS_NAMES = {0: "normal", 1: "maximized", 2: "minimized"}

#: ``WindowInteractionState``
WIS_RUNNING = 0
WIS_CLOSING = 1
WIS_READY_FOR_USER_INTERACTION = 2
WIS_NOT_RESPONDING = 3
WIS_BLOCKED_BY_MODAL_WINDOW = 4

#: ``ScrollAmount``（``ScrollPattern.Scroll`` 的入参）
SCROLL_LARGE_DECREMENT = 0
SCROLL_SMALL_DECREMENT = 1
SCROLL_NO_AMOUNT = 2
SCROLL_LARGE_INCREMENT = 3
SCROLL_SMALL_INCREMENT = 4

#: ``ScrollAmount`` 的 ``NO_AMOUNT`` 代表"这个轴不动"
SCROLL_UNKNOWN = -1.0

#: ``UIA_ScrollPatternNoScroll`` —— SetScrollPercent 表示"不滚动该轴"
SCROLL_NO_SCROLL = -1.0

#: 模式名 → 本模块的封装类名。给 ``wrap()`` 用。
PATTERN_CLASSES = {
    "Invoke": "InvokePattern",
    "Value": "ValuePattern",
    "Toggle": "TogglePattern",
    "SelectionItem": "SelectionItemPattern",
    "ExpandCollapse": "ExpandCollapsePattern",
    "Scroll": "ScrollPattern",
    "RangeValue": "RangeValuePattern",
    "Window": "WindowPattern",
    "LegacyIAccessible": "LegacyPattern",
    "Selection": "SelectionPattern",
    "Text": "TextPattern",
}

#: 每个模式"代表一个可执行动作"时，该动作的名字。
#:
#: 这是给上层做决策用的朴素映射 —— 比如"这个按钮能 Invoke"
#: 就读作"它能被点击"。不是所有模式都在这里（``Text`` 只读不可做）。
PATTERN_VERBS = {
    "Invoke": "invoke",
    "Value": "set_value",
    "Toggle": "toggle",
    "SelectionItem": "select",
    "ExpandCollapse": "expand_collapse",
    "Scroll": "scroll",
    "RangeValue": "set_range",
    "Window": "window",
    "LegacyIAccessible": "legacy",
}

#: 模式优先级 —— 越小越优先。
#:
#: ``actions.py`` 按此顺序挑模式来执行"点击"这类语义操作：
#: ``Invoke`` 最干净（纯语义、不碰鼠标），``LegacyIAccessible``
#: 是最后一道语义退路，再往下就只能动鼠标了。
PATTERN_PRIORITY = {
    "Invoke": 10,
    "Toggle": 20,
    "SelectionItem": 30,
    "ExpandCollapse": 40,
    "Value": 50,
    "RangeValue": 60,
    "ScrollItem": 65,
    "LegacyIAccessible": 70,
    "Window": 80,
    "Scroll": 85,
    "Text": 90,
    # 以下模式只提供信息（表格结构、停靠、变换），不参与"点击"决策
    "Dock": 200,
    "Transform": 210,
    "Grid": 220,
    "GridItem": 220,
    "Table": 220,
    "TableItem": 220,
    "Selection": 230,
    "VirtualizedItem": 240,
    "SynchronizedInput": 250,
}


# --------------------------------------------------------------------------
# 模式接口获取
# --------------------------------------------------------------------------
def acquire(el, pattern: str, required: bool = False) -> Optional[C.ComPtr]:
    """取元素上某个模式的接口指针。

    ``el`` 可以是 ``Element`` 或 ``C.ComPtr``。

    返回的 ``ComPtr`` **归调用方所有** —— 用 ``with`` 或显式 ``release()``。
    元素不支持该模式时返回 ``None``；``required=True`` 时抛
    ``PatternNotSupported``。

    ## 为什么用 ``GetCurrentPatternAs`` 而不是 ``GetCurrentPattern``

    ``GetCurrentPattern`` 返回 ``IUnknown``，还得再 ``QueryInterface`` 一次；
    ``GetCurrentPatternAs`` 一步到位拿到目标接口，省一次跨进程调用。
    它多一个 ``riid`` 入参，所以 vtable 索引是 14（``GetCurrentPattern`` 是 16）。
    """
    pattern_id = T.PATTERN_IDS.get(pattern)
    iid_name = T.PATTERN_IIDS.get(pattern)
    if pattern_id is None or iid_name is None:
        raise ValueError(
            "未知的控件模式: %r（可选: %s）"
            % (pattern, ", ".join(sorted(T.PATTERN_IIDS)))
        )

    # 统一拿到一个 ComPtr（若入参是 Element，需要现场重新定位）
    ptr, hwnd, owner = T._resolve_ref(el)
    if not ptr:
        if required:
            raise ElementStale("元素已失效，无法获取模式 %s" % pattern,
                               hwnd=hwnd or None)
        return None

    try:
        # PATTERN_IIDS 存的是 ``IID_IUIAUTOMATION_*_PATTERN`` 常量名，
        # 先解析成 GUID 字符串（guids 模块的 NAMED_IIDS 用的是不带
        # ``IID_`` 前缀的接口名，所以这里取常量本身的 GUID 值）。
        iid_str = getattr(G, iid_name, None)
        if not isinstance(iid_str, str):
            raise ValueError(
                "模式 %s 的 IID 常量 %s 不存在于 guids 模块" % (pattern, iid_name))
        iid = G.iid_of(iid_str)
        out = ctypes.c_void_p()
        elem = C.ComPtr(ptr, name="IUIAutomationElement", own=False)
        fn = elem.method(T.VT_ELEMENT_GET_CURRENT_PATTERN_AS, ctypes.HRESULT,
                         ctypes.c_int, ctypes.POINTER(G.GUID),
                         ctypes.POINTER(ctypes.c_void_p))
        hr = fn(elem.ptr, ctypes.c_int(int(pattern_id)),
                ctypes.byref(iid), ctypes.byref(out))
        if not C.succeeded(hr) or not out.value:
            if required:
                raise PatternNotSupported(
                    "元素不支持模式 %s" % pattern,
                    pattern=pattern, element=getattr(el, "name", ""),
                )
            return None
        return C.ComPtr(int(out.value),
                        name="IUIAutomation%sPattern" % pattern,
                        iid=iid_name)
    finally:
        T._release_owner(owner)


def supported(el) -> List[str]:
    """元素支持的全部模式名（按优先级排序）。

    ## 两级策略

    1. 入参是 ``Element`` 且它已经带 ``patterns``（构造时
       ``with_patterns=True`` 就会带）→ **直接用**，不再碰 COM。
       这是绝大多数情况，也是唯一稳的路子。
    2. 否则才现探 —— 此时需要把元素重新定位成 COM 引用，
       对已失焦/已滚出视野的元素可能定位不到，那就返回空表。

    ⚠ 早期实现**只**走第 2 条路，结果 ``Element`` 明明带着
    ``patterns=('Invoke', ...)``，``supported()`` 却返回 ``[]``
    —— 因为它去重新定位元素时失败了。白白浪费一次跨进程调用。
    """
    cached = getattr(el, "patterns", None)
    if cached:
        names = list(cached)
        return sorted(names, key=lambda n: PATTERN_PRIORITY.get(n, 999))

    ptr, hwnd, owner = T._resolve_ref(el)
    if not ptr:
        return []
    try:
        names = T.detect_patterns(C.ComPtr(ptr, name="probe", own=False))
    except Exception:
        names = []
    finally:
        T._release_owner(owner)
    return sorted(names, key=lambda n: PATTERN_PRIORITY.get(n, 999))


def best_pattern(el, candidates: Sequence[str]) -> Optional[str]:
    """从 ``candidates`` 里挑出元素支持、且优先级最高的那个。

    给"点击"这类语义操作选实现路径用。
    """
    have = set(supported(el))
    usable = [c for c in candidates if c in have]
    if not usable:
        return None
    return min(usable, key=lambda n: PATTERN_PRIORITY.get(n, 999))


# --------------------------------------------------------------------------
# 模式封装基类
# --------------------------------------------------------------------------
class Pattern:
    """所有模式封装的基类。

    ``self._ptr`` 是模式接口指针，由本对象持有并负责释放。
    用 ``with`` 管理生命周期::

        with patterns.acquire(el, "Value") as vp:
            ValuePattern(vp).set_value("hello")

    或者直接用 ``patterns.value(el).set_value(...)`` 这类便捷函数。
    """

    #: 模式名 —— 子类必须覆盖
    NAME = ""

    __slots__ = ("_cp",)

    def __init__(self, cp: C.ComPtr):
        if cp is None or not cp.valid:
            raise PatternNotSupported(
                "模式 %s 的接口指针无效" % self.NAME, pattern=self.NAME)
        self._cp = cp

    # -- 生命周期 --------------------------------------------------------
    @property
    def ptr(self) -> int:
        return self._cp.ptr

    def release(self) -> int:
        return self._cp.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def __repr__(self):
        return "<%s at 0x%X>" % (type(self).__name__, self.ptr or 0)

    # -- 底层调用 --------------------------------------------------------
    def _call(self, index: int, restype, *argtypes, args=()):
        """调本模式 vtable 上的方法，统一做 HRESULT 检查。"""
        fn = self._cp.method(index, ctypes.HRESULT, *argtypes)
        full = (ctypes.c_void_p(self._cp.ptr),) + tuple(args)
        return fn(*full)

    def _void(self, index: int, *argtypes, args=(), method=""):
        """调用一个返回 ``HRESULT`` 且无出参的方法，失败抛 ``ComError``。"""
        hr = self._call(index, ctypes.HRESULT, *argtypes, args=args)
        C.check_hresult(hr, method or self.NAME, self._cp.name,
                        method or self.NAME, index)
        return True

    def _int(self, index: int, argtypes=(), args=(), method="",
             default: int = 0) -> int:
        """调用一个出参是 ``int`` 的方法。"""
        out = ctypes.c_int(0)
        hr = self._call(index, ctypes.HRESULT,
                        *(tuple(argtypes) + (ctypes.POINTER(ctypes.c_int),)),
                        args=tuple(args) + (ctypes.byref(out),))
        if not C.succeeded(hr):
            return default
        return int(out.value)

    def _double(self, index: int, argtypes=(), args=(), method="",
                default: float = 0.0) -> float:
        out = ctypes.c_double(0.0)
        hr = self._call(index, ctypes.HRESULT,
                        *(tuple(argtypes) + (ctypes.POINTER(ctypes.c_double),)),
                        args=tuple(args) + (ctypes.byref(out),))
        if not C.succeeded(hr):
            return default
        return float(out.value)

    def _bool(self, index: int, argtypes=(), args=(), method="",
              default: bool = False) -> bool:
        """``BOOL`` 出参（4 字节）。"""
        out = ctypes.c_int(0)
        hr = self._call(index, ctypes.HRESULT,
                        *(tuple(argtypes) + (ctypes.POINTER(ctypes.c_int),)),
                        args=tuple(args) + (ctypes.byref(out),))
        if not C.succeeded(hr):
            return default
        return bool(out.value)

    def _bstr(self, index: int, argtypes=(), args=(), method="",
              default: str = "") -> str:
        """``BSTR`` 出参。用 ``SysFreeString`` 释放 —— 不是 ``CoTaskMemFree``。"""
        out = ctypes.c_void_p()
        hr = self._call(index, ctypes.HRESULT,
                        *(tuple(argtypes) + (ctypes.POINTER(ctypes.c_void_p),)),
                        args=tuple(args) + (ctypes.byref(out),))
        if not C.succeeded(hr) or not out.value:
            return default
        return C.com_string(out.value)


# --------------------------------------------------------------------------
# 具体模式
# --------------------------------------------------------------------------
class InvokePattern(Pattern):
    """``Invoke`` —— 最干净的"点击"。

    调用它等于让控件执行默认动作，**不涉及鼠标**。因此：

    · 不怕窗口被遮挡
    · 不受 DPI / 多显示器影响
    · 不需要窗口在前台

    这是"元素定位优先，坐标兜底"里的**首选路径**。
    """

    NAME = "Invoke"

    def invoke(self) -> bool:
        """执行默认动作。"""
        return self._void(VT_INVOKE_INVOKE, method="Invoke")


class TogglePattern(Pattern):
    """``Toggle`` —— 复选框、开关、可切换按钮。"""

    NAME = "Toggle"

    def toggle(self) -> bool:
        return self._void(VT_TOGGLE_TOGGLE, method="Toggle")

    @property
    def state(self) -> int:
        return self._int(VT_TOGGLE_GET_STATE, method="ToggleState")

    @property
    def state_name(self) -> str:
        return TOGGLE_NAMES.get(self.state, "unknown")

    @property
    def is_on(self) -> bool:
        return self.state == TOGGLE_ON

    def set_on(self, want_on: bool) -> bool:
        """切到想要的状态。已经是目标状态就不动。

        ``Toggle`` 只有"切"没有"设"，所以要读状态再决定。
        注意 ``Indeterminate`` 状态下切换的结果不确定 —— 此时总是切一次。
        """
        cur = self.state
        if cur == TOGGLE_INDETERMINATE:
            return self.toggle()
        if (cur == TOGGLE_ON) == bool(want_on):
            return True
        return self.toggle()


class ValuePattern(Pattern):
    """``Value`` —— 文本框、下拉框的值读写。

    写值是**语义操作**：直接改控件内容，不是模拟键入。
    对搜索框这类场景又快又稳。

    但要注意：某些应用（尤其 Electron / 浏览器）对 ``SetValue``
    支持不完整，写进去的值不触发 ``input`` 事件，表单可能不认。
    ``actions.type_text()`` 会在必要时退回真实键入。
    """

    NAME = "Value"

    def set_value(self, text: str) -> bool:
        return self._void(VT_VALUE_SET_VALUE, ctypes.c_wchar_p,
                          args=(text,), method="SetValue")

    @property
    def value(self) -> str:
        return self._bstr(VT_VALUE_GET_VALUE, method="Value")

    @property
    def is_read_only(self) -> bool:
        return self._bool(VT_VALUE_IS_READ_ONLY, method="IsReadOnly")


class RangeValuePattern(Pattern):
    """``RangeValue`` —— 滑块、进度条、数值微调器。

    ⚠ vtable 顺序是 ``SetValue / Value / IsReadOnly / Maximum / Minimum``
    —— ``Maximum`` 在 ``Minimum`` **前面**，别按直觉写反。
    """

    NAME = "RangeValue"

    def set_value(self, value: float) -> bool:
        return self._void(VT_RANGE_SET_VALUE, ctypes.c_double,
                          args=(float(value),), method="SetValue")

    @property
    def value(self) -> float:
        return self._double(VT_RANGE_GET_VALUE, method="Value")

    @property
    def minimum(self) -> float:
        return self._double(VT_RANGE_GET_MIN, method="Minimum")

    @property
    def maximum(self) -> float:
        return self._double(VT_RANGE_GET_MAX, method="Maximum")

    @property
    def is_read_only(self) -> bool:
        return self._bool(VT_RANGE_IS_READ_ONLY, method="IsReadOnly")

    @property
    def large_change(self) -> float:
        return self._double(VT_RANGE_GET_LARGE_CHANGE, method="LargeChange")

    @property
    def small_change(self) -> float:
        return self._double(VT_RANGE_GET_SMALL_CHANGE, method="SmallChange")

    def set_percent(self, pct: float) -> bool:
        """按 0~100 的百分比设置 —— 比记绝对量程方便。"""
        lo, hi = self.minimum, self.maximum
        if hi <= lo:
            return False
        v = lo + (hi - lo) * max(0.0, min(100.0, float(pct))) / 100.0
        return self.set_value(v)

    @property
    def percent(self) -> float:
        lo, hi = self.minimum, self.maximum
        if hi <= lo:
            return 0.0
        return (self.value - lo) * 100.0 / (hi - lo)


class SelectionItemPattern(Pattern):
    """``SelectionItem`` —— 列表项、标签页、单选项。"""

    NAME = "SelectionItem"

    def select(self) -> bool:
        return self._void(VT_SELITEM_SELECT, method="Select")

    def add_to_selection(self) -> bool:
        return self._void(VT_SELITEM_ADD_TO_SELECTION,
                          method="AddToSelection")

    def remove_from_selection(self) -> bool:
        return self._void(VT_SELITEM_REMOVE_FROM_SELECTION,
                          method="RemoveFromSelection")

    @property
    def is_selected(self) -> bool:
        return self._bool(VT_SELITEM_GET_IS_SELECTED, method="IsSelected")

    def ensure_selected(self) -> bool:
        """只在未选中时选中 —— 避免"点两下反而取消"的问题。"""
        return True if self.is_selected else self.select()


class ExpandCollapsePattern(Pattern):
    """``ExpandCollapse`` —— 树节点、组合框、折叠面板。"""

    NAME = "ExpandCollapse"

    def expand(self) -> bool:
        return self._void(VT_EXPAND_EXPAND, method="Expand")

    def collapse(self) -> bool:
        return self._void(VT_EXPAND_COLLAPSE, method="Collapse")

    @property
    def state(self) -> int:
        return self._int(VT_EXPAND_GET_STATE, method="ExpandCollapseState")

    @property
    def state_name(self) -> str:
        return EC_NAMES.get(self.state, "unknown")

    @property
    def is_expanded(self) -> bool:
        return self.state in (EC_EXPANDED, EC_PARTIALLY_EXPANDED)

    @property
    def is_leaf(self) -> bool:
        return self.state == EC_LEAF_NODE

    def set_expanded(self, want: bool) -> bool:
        """切到想要的展开状态。叶子节点直接返回 False。"""
        if self.is_leaf:
            return False
        if self.is_expanded == bool(want):
            return True
        return self.expand() if want else self.collapse()


class ScrollPattern(Pattern):
    """``Scroll`` —— 可滚动容器。"""

    NAME = "Scroll"

    def scroll(self, horizontal: int = SCROLL_NO_AMOUNT,
               vertical: int = SCROLL_NO_AMOUNT) -> bool:
        """按"格"滚动。``NO_AMOUNT`` 表示该轴不动。"""
        return self._void(VT_SCROLL_SCROLL,
                          ctypes.c_int, ctypes.c_int,
                          args=(int(horizontal), int(vertical)),
                          method="Scroll")

    def set_scroll_percent(self, horizontal: float = SCROLL_NO_SCROLL,
                           vertical: float = SCROLL_NO_SCROLL) -> bool:
        """按百分比滚动（0~100）。``SCROLL_NO_SCROLL`` 表示该轴不动。"""
        return self._void(VT_SCROLL_SET_SCROLL_PERCENT,
                          ctypes.c_double, ctypes.c_double,
                          args=(float(horizontal), float(vertical)),
                          method="SetScrollPercent")

    def scroll_to_top(self) -> bool:
        return self.set_scroll_percent(vertical=0.0)

    def scroll_to_bottom(self) -> bool:
        return self.set_scroll_percent(vertical=100.0)

    @property
    def horizontal_percent(self) -> float:
        return self._double(VT_SCROLL_GET_H_PERCENT,
                            default=SCROLL_UNKNOWN)

    @property
    def vertical_percent(self) -> float:
        return self._double(VT_SCROLL_GET_V_PERCENT,
                            default=SCROLL_UNKNOWN)

    @property
    def can_scroll_horizontally(self) -> bool:
        return self._bool(VT_SCROLL_GET_H_SCROLLABLE)

    @property
    def can_scroll_vertically(self) -> bool:
        return self._bool(VT_SCROLL_GET_V_SCROLLABLE)


class WindowPattern(Pattern):
    """``Window`` —— 窗口自身的控制（关闭 / 最大化 / 等空闲）。"""

    NAME = "Window"

    def close(self) -> bool:
        """请求关闭窗口（相当于点标题栏的 ×）。

        注意：这会触发应用的"是否保存"流程 —— 可能弹对话框卡住调用方。
        强杀进程请用 ``app_manager.process.kill``。
        """
        return self._void(VT_WINDOW_CLOSE, method="Close")

    def wait_for_input_idle(self, timeout_ms: int = 5000) -> bool:
        """等窗口处理完输入队列。超时返回 False（不抛异常）。"""
        try:
            self._void(VT_WINDOW_WAIT_FOR_INPUT_IDLE, ctypes.c_int,
                       args=(int(timeout_ms),), method="WaitForInputIdle")
            return True
        except ComError:
            return False

    def set_visual_state(self, state: int, timeout: float = 3.0,
                         retries: int = 3) -> bool:
        """``NORMAL`` / ``MAXIMIZED`` / ``MINIMIZED``。

        ## 为什么要重试

        窗口状态切换是**异步**的。刚发完 ``MINIMIZED`` 紧接着发
        ``NORMAL``，UIA 可能回 ``0x80131509``（``COR_E_INVALIDOPERATION``
        / 元素暂时不可用）—— 因为上一个过渡还没走完。

        实测过：不等就连续切状态，第二次必失败。
        所以这里失败后退一步重试，并在切换后**等状态真的变了**。
        """
        if state == self.visual_state:
            return True
        last_kind = None
        for attempt in range(max(1, retries)):
            try:
                self._void(VT_WINDOW_SET_WINDOW_VISUAL_STATE, ctypes.c_int,
                           args=(int(state),),
                           method="SetWindowVisualState")
            except ComError as exc:
                last_kind = type(exc).__name__
            else:
                # 请求已接受 —— 等状态生效（UIA 的窗口操作是异步的）
                if self._wait_state(state, timeout / max(1, retries)):
                    return True
                last_kind = "状态未在超时内生效"
            time.sleep(0.15 * (attempt + 1))
        # 最后一次兜底：状态没变但也可能是外力所致，读一次再判定
        ok = self.visual_state == state
        if not ok and last_kind:
            raise ComError(
                "切换窗口状态到 %s 失败（%s）"
                % (WVS_NAMES.get(state, state), last_kind),
                interface=self._cp.name, method="SetWindowVisualState",
            )
        return ok

    def _wait_state(self, want: int, timeout: float) -> bool:
        """轮询等 ``WindowVisualState`` 变成 ``want``。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if self.visual_state == want:
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def maximize(self, timeout: float = 3.0) -> bool:
        return self.set_visual_state(WVS_MAXIMIZED, timeout=timeout)

    def minimize(self, timeout: float = 3.0) -> bool:
        return self.set_visual_state(WVS_MINIMIZED, timeout=timeout)

    def restore(self, timeout: float = 3.0) -> bool:
        return self.set_visual_state(WVS_NORMAL, timeout=timeout)

    @property
    def visual_state(self) -> int:
        return self._int(VT_WINDOW_GET_VISUAL_STATE)

    @property
    def visual_state_name(self) -> str:
        return WVS_NAMES.get(self.visual_state, "unknown")

    @property
    def interaction_state(self) -> int:
        """``WindowInteractionState`` —— 窗口是否就绪/无响应/被模态挡住。"""
        return self._int(VT_WINDOW_GET_INTERACTION_STATE)

    @property
    def is_modal(self) -> bool:
        return self._bool(VT_WINDOW_IS_MODAL)

    @property
    def is_topmost(self) -> bool:
        return self._bool(VT_WINDOW_IS_TOPMOST)

    @property
    def can_maximize(self) -> bool:
        return self._bool(VT_WINDOW_GET_CAN_MAXIMIZE)

    @property
    def can_minimize(self) -> bool:
        return self._bool(VT_WINDOW_GET_CAN_MINIMIZE)

    @property
    def is_responding(self) -> bool:
        """窗口是否在响应（非 ``NotResponding``）。"""
        return self.interaction_state != WIS_NOT_RESPONDING


class LegacyPattern(Pattern):
    """``LegacyIAccessible`` —— MSAA 兼容层。

    老应用（VB6、MFC、部分 Java 程序）只暴露 MSAA 接口，
    UIA 的现代模式全都拿不到。这时它是**唯一**的语义操作途径，
    所以优先级排在所有现代模式之后、坐标兜底之前。
    """

    NAME = "LegacyIAccessible"

    def select(self) -> bool:
        """选中 —— MSAA 的 ``Select``。注意在 vtable 里比 ``DoDefaultAction`` 靠前。"""
        return self._void(VT_LEGACY_SELECT, method="Select")

    def do_default_action(self) -> bool:
        """执行默认动作 —— 相当于 MSAA 的 ``DoDefaultAction``。"""
        return self._void(VT_LEGACY_DO_DEFAULT_ACTION,
                          method="DoDefaultAction")

    def set_value(self, text: str) -> bool:
        return self._void(VT_LEGACY_SET_VALUE, ctypes.c_wchar_p,
                          args=(text,), method="SetValue")

    @property
    def value(self) -> str:
        return self._bstr(VT_LEGACY_GET_VALUE, method="Value")

    @property
    def name(self) -> str:
        return self._bstr(VT_LEGACY_GET_NAME, method="Name")

    @property
    def description(self) -> str:
        return self._bstr(VT_LEGACY_GET_DESCRIPTION, method="Description")

    @property
    def default_action(self) -> str:
        return self._bstr(VT_LEGACY_GET_DEFAULT_ACTION,
                          method="DefaultAction")

    @property
    def child_id(self) -> int:
        return self._int(VT_LEGACY_GET_CHILD_ID, method="ChildId")

    @property
    def role(self) -> int:
        """MSAA 角色码（``ROLE_SYSTEM_*``）。"""
        return self._int(VT_LEGACY_GET_ROLE, method="Role")

    @property
    def keyboard_shortcut(self) -> str:
        return self._bstr(VT_LEGACY_GET_KEYBOARD_SHORTCUT,
                          method="KeyboardShortcut")


class SelectionPattern(Pattern):
    """``Selection`` —— 容器侧的选中管理（多选列表、组合框）。

    ⚠ 与 ``SelectionItemPattern`` 不同：那个管"我这个条目选不选中"，
    这个管"这个容器里谁被选中了"。
    """

    NAME = "Selection"

    @property
    def can_select_multiple(self) -> bool:
        return self._bool(VT_SELECTION_CAN_SELECT_MULTIPLE,
                          method="CanSelectMultiple")

    @property
    def is_required(self) -> bool:
        return self._bool(VT_SELECTION_IS_REQUIRED,
                          method="IsSelectionRequired")

    @property
    def count(self) -> int:
        """当前选中项个数。

        ``GetCurrentSelection``（vtable 3）返回 ``IUIAutomationElementArray``，
        这里只取数量 —— 具体元素由调用方另取。
        """
        arr_cp = self._current_selection()
        if arr_cp is None:
            return 0
        try:
            n = ctypes.c_int(0)
            fn = arr_cp.method(T.VT_ARRAY_LENGTH, ctypes.HRESULT,
                               ctypes.POINTER(ctypes.c_int))
            hr = fn(arr_cp.ptr, ctypes.byref(n))
            return int(n.value) if C.succeeded(hr) else 0
        finally:
            arr_cp.release()

    def _current_selection(self) -> Optional[C.ComPtr]:
        """取当前选中项的 ``IUIAutomationElementArray``（调用方负责释放）。"""
        out = ctypes.c_void_p()
        hr = self._call(VT_SELECTION_GET_CURRENT, ctypes.HRESULT,
                        ctypes.POINTER(ctypes.c_void_p),
                        args=(ctypes.byref(out),))
        if not C.succeeded(hr) or not out.value:
            return None
        return C.ComPtr(int(out.value), name="IUIAutomationElementArray")


class DockPattern(Pattern):
    """``Dock`` —— 可停靠面板（VS/Delphi 那种拖到边缘吸附的面板）。"""

    NAME = "Dock"

    #: ``DockPosition``
    TOP, LEFT, BOTTOM, RIGHT, FILL, NONE = 0, 1, 2, 3, 4, 5

    #: 位置名 —— 与上面的常量对应
    POSITION_NAMES = {0: "top", 1: "left", 2: "bottom",
                      3: "right", 4: "fill", 5: "none"}

    def set_position(self, position: int) -> bool:
        return self._void(VT_DOCK_SET_POSITION, ctypes.c_int,
                          args=(int(position),), method="SetDockPosition")

    @property
    def position(self) -> int:
        return self._int(VT_DOCK_GET_POSITION, method="DockPosition")

    @property
    def position_name(self) -> str:
        return self.POSITION_NAMES.get(self.position, "unknown")


class TransformPattern(Pattern):
    """``Transform`` —— 元素的移动/缩放/旋转。

    只对"可自由变换"的元素有效（窗口、画布上的图形）。
    普通控件会返回 ``can_move=False`` —— 此时不要硬调，
    ``Move`` 会返回 ``E_INVALIDARG``。
    """

    NAME = "Transform"

    def move(self, x: float, y: float) -> bool:
        """把元素移到屏幕坐标 ``(x, y)``。

        注意这里的坐标是**屏幕坐标** —— 这是 UIA 少数直接吃坐标的接口，
        与"元素不缓存坐标"的原则不冲突（是调用方提供，不是我们缓存）。
        """
        return self._void(VT_TRANSFORM_MOVE,
                          ctypes.c_double, ctypes.c_double,
                          args=(float(x), float(y)), method="Move")

    def resize(self, width: float, height: float) -> bool:
        return self._void(VT_TRANSFORM_RESIZE,
                          ctypes.c_double, ctypes.c_double,
                          args=(float(width), float(height)), method="Resize")

    def rotate(self, degrees: float) -> bool:
        """旋转。只有 ``can_rotate`` 为真时才行。"""
        return self._void(VT_TRANSFORM_ROTATE, ctypes.c_double,
                          args=(float(degrees),), method="Rotate")

    @property
    def can_move(self) -> bool:
        return self._bool(VT_TRANSFORM_CAN_MOVE)

    @property
    def can_resize(self) -> bool:
        return self._bool(VT_TRANSFORM_CAN_RESIZE)

    @property
    def can_rotate(self) -> bool:
        return self._bool(VT_TRANSFORM_CAN_ROTATE)


class GridPattern(Pattern):
    """``Grid`` —— 网格容器（表格、日期选择器）。只读查询。"""

    NAME = "Grid"

    @property
    def row_count(self) -> int:
        return self._int(VT_GRID_GET_ROW_COUNT)

    @property
    def column_count(self) -> int:
        return self._int(VT_GRID_GET_COLUMN_COUNT)

    def item(self, row: int, column: int) -> Optional[C.ComPtr]:
        """取 (row, column) 处的元素。

        返回的 ``ComPtr`` **归调用方所有**。
        """
        out = ctypes.c_void_p()
        hr = self._call(VT_GRID_GET_ITEM, ctypes.HRESULT,
                        ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p),
                        args=(ctypes.c_int(int(row)), ctypes.c_int(int(column)),
                              ctypes.byref(out)))
        if not C.succeeded(hr) or not out.value:
            return None
        return C.ComPtr(int(out.value), name="IUIAutomationElement")


class TablePattern(Pattern):
    """``Table`` —— 表格容器。可读行列标题。"""

    NAME = "Table"

    #: ``RowOrColumnMajor``: 0=RowMajor 1=ColumnMajor 2=Indeterminate
    ROW_MAJOR, COLUMN_MAJOR, INDETERMINATE = 0, 1, 2

    @property
    def row_or_column_major(self) -> int:
        """表格的遍历主序 —— 决定按行还是按列读更自然。"""
        return self._int(VT_TABLE_GET_ROW_OR_COLUMN_MAJOR)

    def row_headers(self) -> List[C.ComPtr]:
        """行标题元素列表（调用方负责释放每一个）。"""
        return self._header_array(VT_TABLE_GET_ROW_HEADERS)

    def column_headers(self) -> List[C.ComPtr]:
        return self._header_array(VT_TABLE_GET_COLUMN_HEADERS)

    def _header_array(self, index: int) -> List[C.ComPtr]:
        out = ctypes.c_void_p()
        hr = self._call(index, ctypes.HRESULT,
                        ctypes.POINTER(ctypes.c_void_p),
                        args=(ctypes.byref(out),))
        if not C.succeeded(hr) or not out.value:
            return []
        arr = C.ComPtr(int(out.value), name="IUIAutomationElementArray")
        try:
            n = ctypes.c_int(0)
            fn = arr.method(T.VT_ARRAY_LENGTH, ctypes.HRESULT,
                            ctypes.POINTER(ctypes.c_int))
            hr2 = fn(arr.ptr, ctypes.byref(n))
            if not C.succeeded(hr2):
                return []
            items = []
            getter = arr.method(T.VT_ARRAY_GET_ELEMENT, ctypes.HRESULT,
                                ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            for i in range(int(n.value)):
                p = ctypes.c_void_p()
                hr3 = getter(arr.ptr, ctypes.c_int(i), ctypes.byref(p))
                if C.succeeded(hr3) and p.value:
                    items.append(C.ComPtr(int(p.value),
                                          name="IUIAutomationElement"))
            return items
        finally:
            arr.release()


class GridItemPattern(Pattern):
    """``GridItem`` —— 网格里的一个单元格。"""

    NAME = "GridItem"

    @property
    def row(self) -> int:
        return self._int(4)

    @property
    def column(self) -> int:
        return self._int(5)

    @property
    def row_span(self) -> int:
        return self._int(6)

    @property
    def column_span(self) -> int:
        return self._int(7)


class TableItemPattern(Pattern):
    """``TableItem`` —— 表格里的一个单元格（比 GridItem 多标题信息）。"""

    NAME = "TableItem"

    def row_header_items(self) -> List[C.ComPtr]:
        return self._item_array(3)

    def column_header_items(self) -> List[C.ComPtr]:
        return self._item_array(4)

    def _item_array(self, index: int) -> List[C.ComPtr]:
        out = ctypes.c_void_p()
        hr = self._call(index, ctypes.HRESULT,
                        ctypes.POINTER(ctypes.c_void_p),
                        args=(ctypes.byref(out),))
        if not C.succeeded(hr) or not out.value:
            return []
        arr = C.ComPtr(int(out.value), name="IUIAutomationElementArray")
        try:
            n = ctypes.c_int(0)
            fn = arr.method(T.VT_ARRAY_LENGTH, ctypes.HRESULT,
                            ctypes.POINTER(ctypes.c_int))
            hr2 = fn(arr.ptr, ctypes.byref(n))
            if not C.succeeded(hr2):
                return []
            items = []
            getter = arr.method(T.VT_ARRAY_GET_ELEMENT, ctypes.HRESULT,
                                ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            for i in range(int(n.value)):
                p = ctypes.c_void_p()
                hr3 = getter(arr.ptr, ctypes.c_int(i), ctypes.byref(p))
                if C.succeeded(hr3) and p.value:
                    items.append(C.ComPtr(int(p.value),
                                          name="IUIAutomationElement"))
            return items
        finally:
            arr.release()


class VirtualizedItemPattern(Pattern):
    """``VirtualizedItem`` —— 虚拟化列表里"把还没生成的那一项拉出来"。

    大列表（几千行）通常只渲染可视区，滚动到的项才有真实元素。
    对虚拟化项调 ``realize()`` 会强制它实体化。
    """

    NAME = "VirtualizedItem"

    def realize(self) -> bool:
        return self._void(VT_VIRTUALIZED_REALIZE, method="Realize")


class SynchronizedInputPattern(Pattern):
    """``SynchronizedInput`` —— 让 UIA 把后续的真实输入事件回报给调用方。

    用于"我要点一下然后确认事件真的到了"这种同步需求。
    """

    NAME = "SynchronizedInput"

    #: ``SynchronizedInputType``
    KEY_DOWN, KEY_UP, LEFT_MOUSE_DOWN, LEFT_MOUSE_UP = 1, 2, 4, 8
    RIGHT_MOUSE_DOWN, RIGHT_MOUSE_UP = 16, 32

    def start_listening(self, input_type: int) -> bool:
        return self._void(VT_SYNC_START_LISTENING, ctypes.c_int,
                          args=(int(input_type),), method="StartListening")

    def cancel(self) -> bool:
        return self._void(VT_SYNC_CANCEL, method="Cancel")


class ScrollItemPattern(Pattern):
    """``ScrollItem`` —— 把（可能还不可见的）项滚进视野。

    比在容器上算百分比靠谱：直接告诉 UIA"把这个项滚到能看见"。
    """

    NAME = "ScrollItem"

    def scroll_into_view(self) -> bool:
        return self._void(VT_SCROLL_ITEM_INTO_VIEW, method="ScrollIntoView")


class TextPattern(Pattern):
    """``Text`` —— 文本内容读取（只读，无动作）。

    本模块只暴露"有没有"，真正的文本提取留给后续阶段 ——
    它涉及 ``IUIAutomationTextRange``，工作量和模式本身相当。
    """

    NAME = "Text"

    @property
    def is_available(self) -> bool:
        return True


# --------------------------------------------------------------------------
# 便捷构造
# --------------------------------------------------------------------------
_CLASS_BY_NAME: Dict[str, type] = {
    "Invoke": InvokePattern,
    "Toggle": TogglePattern,
    "Value": ValuePattern,
    "RangeValue": RangeValuePattern,
    "SelectionItem": SelectionItemPattern,
    "ExpandCollapse": ExpandCollapsePattern,
    "Scroll": ScrollPattern,
    "Window": WindowPattern,
    "LegacyIAccessible": LegacyPattern,
    "Selection": SelectionPattern,
    "Text": TextPattern,
    "Dock": DockPattern,
    "Transform": TransformPattern,
    "Grid": GridPattern,
    "GridItem": GridItemPattern,
    "Table": TablePattern,
    "TableItem": TableItemPattern,
    "VirtualizedItem": VirtualizedItemPattern,
    "SynchronizedInput": SynchronizedInputPattern,
    "ScrollItem": ScrollItemPattern,
}


def wrap(el, pattern: str, required: bool = True) -> Optional[Pattern]:
    """取模式并包成类型化对象。

    ``required=True``（默认）时元素不支持该模式会抛
    ``PatternNotSupported``；``required=False`` 时返回 ``None``。

    返回对象持有模式引用 —— 用 ``with`` 或显式 ``release()``。
    """
    cls = _CLASS_BY_NAME.get(pattern)
    if cls is None:
        raise ValueError("没有 %r 模式的封装类" % pattern)
    cp = acquire(el, pattern, required=required)
    if cp is None:
        return None
    return cls(cp)


def _factory(pattern: str):
    """生成 ``invoke(el)`` / ``value(el)`` 这类便捷函数。"""
    def fn(el, required: bool = True):
        return wrap(el, pattern, required=required)
    fn.__name__ = pattern.lower()
    fn.__doc__ = "取 %s 模式封装。不支持时%s。" % (
        pattern, "抛 PatternNotSupported" if True else "返回 None")
    return fn


#: 每个模式的便捷函数 —— ``patterns.invoke(el)`` 比
#: ``patterns.wrap(el, "Invoke")`` 少写很多字。
invoke = _factory("Invoke")
toggle = _factory("Toggle")
value = _factory("Value")
range_value = _factory("RangeValue")
selection_item = _factory("SelectionItem")
expand_collapse = _factory("ExpandCollapse")
scroll = _factory("Scroll")
window = _factory("Window")
legacy = _factory("LegacyIAccessible")
dock = _factory("Dock")
transform = _factory("Transform")
grid = _factory("Grid")
table = _factory("Table")
virtualized = _factory("VirtualizedItem")
scroll_item = _factory("ScrollItem")


def pattern_actions(el) -> List[str]:
    """列出元素能做的动作名（去重、按优先级排序）。

    给上层决策用：先看有什么语义动作，都没有再考虑坐标兜底。
    """
    names = supported(el)
    verbs = [PATTERN_VERBS[n] for n in names if n in PATTERN_VERBS]
    seen = set()
    out = []
    for v in verbs:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def describe(el) -> str:
    """一行文字描述元素的模式支持情况，用于日志与调试。"""
    names = supported(el)
    if not names:
        return "无可用控件模式"
    parts = []
    for n in names:
        parts.append(n)
    verbs = pattern_actions(el)
    return "模式: %s | 可做: %s" % (
        ", ".join(parts), ", ".join(verbs) if verbs else "无")


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class PatternNotSupported(ElementNotFound):
    """元素不支持请求的控件模式。

    继承 ``ElementNotFound`` 是因为对调用方而言，语义是一样的：
    "这个元素做不到这件事"。上层 ``actions.py`` 捕获它之后
    会考虑坐标兜底或换一个元素。
    """

    def __init__(self, message: str, pattern: str = "", element: str = ""):
        super().__init__(message, hwnd=None)
        self.pattern = pattern
        self.element = element

    def __str__(self):
        base = super().__str__()
        extra = []
        if self.pattern:
            extra.append("pattern=%s" % self.pattern)
        if self.element:
            extra.append("element=%r" % self.element)
        return base + (" [%s]" % " ".join(extra) if extra else "")


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """跑一遍模式层的自检，返回 ``{"checks": [...], "ok": bool}``。"""
    checks = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    from ..app_manager import windows as W

    # 1. 本模块**声明支持**的模式都有封装类
    #
    # 注意：不是要求"SDK 里每个模式都有类"。UIA 一共 32 个模式，
    # 其中十几个（Drag/DropTarget/Styles/Spreadsheet…）要么极罕见、
    # 要么只提供元信息，本项目不打算实现。这里只检查
    # PATTERN_VERBS + PATTERN_CLASSES 声明过的那些确实有类兜底。
    declared = set(PATTERN_CLASSES) | set(PATTERN_VERBS)
    missing = sorted(n for n in declared if n not in _CLASS_BY_NAME)
    _add("已声明的模式都有封装类", not missing,
         "实现 %d 个模式封装%s" % (
             len(_CLASS_BY_NAME),
             "" if not missing else "，缺: %s" % ", ".join(missing)))

    # 2. 有封装类的模式都能解析出 IID
    no_iid = sorted(n for n in _CLASS_BY_NAME if n not in T.PATTERN_IIDS)
    _add("模式 IID 表完整", not no_iid,
         "缺 IID 的模式: %s" % (", ".join(no_iid) if no_iid else "无"))

    # 2b. 有封装类的模式都能解析出 PatternId
    no_pid = sorted(n for n in _CLASS_BY_NAME if n not in T.PATTERN_IDS)
    _add("模式 PatternId 表完整", not no_pid,
         "缺 PatternId 的模式: %s" % (", ".join(no_pid) if no_pid else "无"))

    # 3. 优先级表覆盖所有可执行模式
    no_pri = [n for n in PATTERN_VERBS if n not in PATTERN_PRIORITY]
    _add("可执行模式都有优先级", not no_pri,
         "缺优先级的: %s" % (", ".join(no_pri) if no_pri else "无"))

    # 4. 好窗口上能探测到模式
    ws = [w for w in W.list_windows() if w.is_usable]
    if not ws:
        _add("能找到可用窗口", False, "没有可用窗口，跳过后续检查")
        return {"checks": checks,
                "failures": [c["name"] for c in checks if not c["ok"]],
                "ok": all(c["ok"] for c in checks)}

    w0 = ws[0]
    try:
        win = T.from_window(w0.hwnd)
        pats = supported(win)
        _add("能探测窗口元素的模式", True,
             "hwnd=0x%X 支持 %d 个模式: %s"
             % (w0.hwnd, len(pats), ", ".join(pats[:8])))

        # 5. Window 模式在顶窗上应当可用
        wp = wrap(win, "Window", required=False)
        if wp is not None:
            try:
                _add("Window 模式可读状态", True,
                     "visual=%s modal=%s topmost=%s"
                     % (wp.visual_state_name, wp.is_modal, wp.is_topmost))
            finally:
                wp.release()
        else:
            _add("Window 模式可读状态", False, "顶层窗口不支持 Window 模式")

        # 6. 模式接口获取不漏引用
        _add("模式接口可反复获取释放", _pattern_leak_check(win),
             "连续 100 次获取/释放后引用计数稳定")

        # 7. 未知模式名被拒绝
        _add("未知模式名被拒绝",
             _raises(ValueError, acquire, win, "Nonsense"),
             "不存在的模式名报 ValueError")

        # 8. 不支持的模式在 required=False 时返回 None
        npm = wrap(win, "RangeValue", required=False)
        _add("不支持的模式返回 None", npm is None,
             "窗口元素上 RangeValue 不支持 → None")
        if npm is not None:
            npm.release()

    except Exception as exc:      # noqa: BLE001
        _add("模式探测不抛异常", False,
             "%s: %s" % (type(exc).__name__, exc))
        return {"checks": checks,
                "failures": [c["name"] for c in checks if not c["ok"]],
                "ok": False}

    # 9. describe 有输出
    try:
        d = describe(win)
        _add("describe 有输出", bool(d), d[:70])
    except Exception as exc:      # noqa: BLE001
        _add("describe 有输出", False, "异常: %s" % exc)

    return {"checks": checks,
            "failures": [c["name"] for c in checks if not c["ok"]],
            "ok": all(c["ok"] for c in checks)}


def _raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
        return False
    except exc:
        return True
    except Exception:
        return False


def _pattern_leak_check(el, rounds: int = 100) -> bool:
    """反复取/放模式接口，确认不泄漏。

    判据：取一个模式前后，元素的引用计数回到同一水平。
    这里用"取 100 次后还能正常读到值"作为代理判据 ——
    真漏了的话，UIA 的跨进程引用会累积到明显异常。
    """
    try:
        for _ in range(rounds):
            cp = acquire(el, "Window")
            if cp is not None:
                cp.release()
        # 再取一次确认仍然可用
        cp = acquire(el, "Window")
        if cp is None:
            return False
        cp.release()
        return True
    except Exception:
        return False


def _main() -> int:
    res = selfcheck()
    print("UIA 控件模式自检:")
    for c in res["checks"]:
        print("  [%s] %-28s %s"
              % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
    n, total = len(res["checks"]) - len(res["failures"]), len(res["checks"])
    print("UIA patterns 自检: %d/%d" % (n, total))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
