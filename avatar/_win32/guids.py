# -*- coding: utf-8 -*-
"""GUID / IID / CLSID 常量中心。

职责
----
把项目用到的所有 COM 标识符集中在这里，避免散落在各处手写字符串导致
抄错一位十六进制。所有 GUID 都通过 `guid()` 解析成 `GUID` 结构体并缓存。

依赖
----
· ctypes（标准库）
· 无内部依赖 —— 本模块处于依赖链最底层

设计说明
--------
GUID 字符串格式为 ``{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`` 或省略花括号。
解析时用 ``uuid.UUID``（标准库）校验合法性，再按 Win32 的 GUID 内存布局
（Data1: uint32, Data2: uint16, Data3: uint16, Data4: 8 bytes）填入。

**不要用 ctypes 直接读字符串** —— GUID 是二进制结构，不是 char 数组。
"""
from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes

# --------------------------------------------------------------------------
# GUID 结构体
# --------------------------------------------------------------------------


class GUID(ctypes.Structure):
    """Win32 GUID / IID / CLSID，三者内存布局相同。

    Data4 是 8 字节数组 —— 注意它**不是** uint64。Win32 的 GUID 把最后
    8 字节按字节序原样存储，按 uint64 读会在小端机上得到错误的值。
    """

    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        d4 = "".join("%02X" % b for b in self.Data4)
        return "{%08X-%04X-%04X-%s-%s}" % (
            self.Data1,
            self.Data2,
            self.Data3,
            d4[:4],
            d4[4:],
        )

    def __repr__(self) -> str:
        return "GUID(%s)" % self

    def __eq__(self, other):
        if isinstance(other, GUID):
            return bytes(self) == bytes(other)
        return NotImplemented

    def __hash__(self):
        return hash(bytes(self))

    @classmethod
    def from_string(cls, s: str) -> "GUID":
        return guid(s)

    def as_void_p(self) -> ctypes.c_void_p:
        """转成 ctypes.c_void_p 便于传参（传 byref 更常见）。"""
        return ctypes.cast(ctypes.byref(self), ctypes.c_void_p)


#: IID 与 CLSID 在 Win32 里都是 GUID
IID = GUID
CLSID = GUID

# --------------------------------------------------------------------------
# 解析与缓存
# --------------------------------------------------------------------------
_GUID_CACHE: dict = {}


def guid(s: str) -> GUID:
    """把 ``"{XXXXXXXX-...}"`` 解析成 GUID 结构体（带缓存）。

    ⚠️ 实现要点，两个坑都踩过：

    **坑 1 —— 不要用 ``uuid.UUID.fields`` 手工拼 D3/D4。**
    ``fields`` 的语义是::

        (time_low, time_mid, time_hi_version,
         clock_seq_hi_variant, clock_seq_low, node)

    它与 Win32 GUID 的 ``(Data1, Data2, Data3, Data4[8])`` 布局**不同**::

        uuid.fields[2]                          → GUID.Data3
        uuid.fields[3], fields[4],
        fields[5] 的低 6 字节                    → GUID.Data4[0..7]

    曾按 ``(fields[3] >> 32) & 0xFFFF`` 取 Data3，得到的永远是 0。

    **坑 2 —— 不要用 ``GUID.from_buffer_copy(u.bytes)``。**
    ``uuid.UUID.bytes`` 是**大端**序的 16 字节，而 ctypes 在 x86/x64 小端机上
    按字段类型解释多字节整数，于是 D1/D2/D3 的字节顺序被反转::

        {FF48DBA4-60EF-4201-...}  →  解析成 {A4DB48FF-EF60-0142-...}

    两种错法都只会换来一个笼统的 ``REGDB_E_CLASSNOTREG``，完全看不出是
    解析问题 —— 所以必须把这段逻辑单测钉死。

    正确做法：用 ``uuid.UUID.int``（128 位大端序整数）显式移位取值。
    """
    key = s.strip().strip("{}").replace(" ", "").lower()
    tpl = _GUID_CACHE.get(key)
    if tpl is None:
        try:
            u = uuid.UUID(key)
        except ValueError as e:
            raise ValueError("非法 GUID 字符串: %r (%s)" % (s, e)) from e
        # int 是 128 位大端表示，按 Win32 字段宽度切
        n = u.int
        d1 = (n >> 96) & 0xFFFFFFFF
        d2 = (n >> 80) & 0xFFFF
        d3 = (n >> 64) & 0xFFFF
        d4 = [(n >> (56 - i * 8)) & 0xFF for i in range(8)]
        g = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
        tpl = bytes(g)
        _GUID_CACHE[key] = tpl
    return GUID.from_buffer_copy(tpl)


def guid_ptr(g: GUID):
    return ctypes.byref(g)


# --------------------------------------------------------------------------
# IUnknown 及基础接口
# --------------------------------------------------------------------------
IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"

# --------------------------------------------------------------------------
# UI Automation
# --------------------------------------------------------------------------
CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

IID_IUIAUTOMATION_ELEMENT = "{D22108AA-8AC5-49A5-837B-37BBB3D7591E}"
IID_IUIAUTOMATION_ELEMENT_ARRAY = "{14314595-B4BC-4055-95F2-58F92F391ADB}"
IID_IUIAUTOMATION_TREE_WALKER = "{4042C624-389C-4AFC-A630-9DF854A541FC}"
IID_IUIAUTOMATION_CACHE_REQUEST = "{B09E5E20-96DD-44C9-B7DA-4C1B0BE3E2B3}"
IID_IUIAUTOMATION_CONDITION = "{352FFBA8-0973-437C-A61F-F64CAFD81DF9}"
IID_IUIAUTOMATION_CONDITION_TRUE = "{DFB581A8-4F3D-4F6C-A4D1-3A5DA1A0A2CD}"
IID_IUIAUTOMATION_CONDITION_FALSE = "{B7B6C4A4-8E7B-4B3D-9E1D-F9B0E51E3E8B}"

# --- Element 级 Pattern 接口 ---
#: 全部从 Windows SDK 的 ``UIAutomationClient.h`` 里各接口
#: ``MIDL_INTERFACE("...")`` 宏逐个抄下，不是猜的。
#:
#: ⚠ 早先有三个值是**编的**（``SELECTION_PATTERN`` / ``TEXT_PATTERN``
#: 的 GUID 完全是假的，另外几个干脆缺失），已按 SDK 头文件订正。
#: 校验脚本：``tools/dump_uia_vtbl.py``。
IID_IUIAUTOMATION_INVOKE_PATTERN = "{FB377FBE-8EA6-46D5-9C73-6499642D3059}"
IID_IUIAUTOMATION_VALUE_PATTERN = "{A94CD8B1-0844-4CD6-9D2D-640537AB39E9}"
IID_IUIAUTOMATION_TOGGLE_PATTERN = "{94CF8058-9B8D-4AB9-8BFD-4CD0A33C8C70}"
IID_IUIAUTOMATION_EXPAND_COLLAPSE_PATTERN = "{619BE086-1F4E-4EE4-BAFA-210128738730}"
IID_IUIAUTOMATION_SCROLL_PATTERN = "{88F4D42A-E881-459D-A77C-73BBBB7E02DC}"
IID_IUIAUTOMATION_SELECTION_ITEM_PATTERN = "{A8EFA66A-0FDA-421A-9194-38021F3578EA}"
IID_IUIAUTOMATION_RANGE_VALUE_PATTERN = "{59213F4F-7346-49E5-B120-80555987A148}"
IID_IUIAUTOMATION_WINDOW_PATTERN = "{0FAEF453-9208-43EF-BBB2-3B485177864F}"
IID_IUIAUTOMATION_LEGACY_ACCESSIBLE_PATTERN = "{828055AD-355B-4435-86D5-3B51C14A9B1B}"
IID_IUIAUTOMATION_TEXT_RANGE = "{A543CC6A-F4AE-494B-8239-C814481187A8}"
IID_IUIAUTOMATION_SELECTION_PATTERN = "{5ED5202E-B2AC-47A6-B638-4B0BF140D78E}"
IID_IUIAUTOMATION_SCROLL_ITEM_PATTERN = "{B488300F-D015-4F19-9C29-BB595E3645EF}"
IID_IUIAUTOMATION_VIRTUALIZED_ITEM_PATTERN = "{6BA3D7A6-04CF-4F11-8793-A8D1CDE9969F}"
IID_IUIAUTOMATION_TEXT_PATTERN = "{32EBA289-3583-42C9-9C59-3B6D9A1E9B6A}"
#: 以下四个是补全的（原文件缺）
IID_IUIAUTOMATION_GRID_PATTERN = "{414C3CDC-856B-4F5B-8538-3131C6302550}"
IID_IUIAUTOMATION_GRID_ITEM_PATTERN = "{78F8EF57-66C3-4E09-BD7C-E79B2004894D}"
IID_IUIAUTOMATION_TABLE_PATTERN = "{620E691C-EA96-4710-A850-754B24CE2417}"
IID_IUIAUTOMATION_TABLE_ITEM_PATTERN = "{0B964EB3-EF2E-4464-9C79-61D61737A27E}"
IID_IUIAUTOMATION_DOCK_PATTERN = "{FDE5EF97-1464-48F6-90BF-43D0948E86EC}"
IID_IUIAUTOMATION_TRANSFORM_PATTERN = "{A9B55844-A55D-4EF0-926D-569C16FF89BB}"
IID_IUIAUTOMATION_SYNCHRONIZED_INPUT_PATTERN = "{2233BE0B-AFB7-448B-9FDA-3B378AA5EAE1}"
#: 以下是"冷门"模式 —— 本项目实现了取接口的通道，但暂不做类型化封装。
#: 保留 IID 是为了让 ``patterns.acquire()`` 对所有模式都能工作。
IID_IUIAUTOMATION_MULTIPLE_VIEW_PATTERN = "{8D253C91-1DC5-4BB5-B18F-ADE16FA495E8}"
IID_IUIAUTOMATION_ITEM_CONTAINER_PATTERN = "{C690FDB2-27A8-423C-812D-429773C9084E}"
IID_IUIAUTOMATION_OBJECT_MODEL_PATTERN = "{71C284B3-C14D-4D14-981E-19751B0D756D}"
IID_IUIAUTOMATION_ANNOTATION_PATTERN = "{9A175B21-339E-41B1-8E8B-623F6B681098}"
IID_IUIAUTOMATION_STYLES_PATTERN = "{85B5F0A2-BD79-484A-AD2B-388C9838D5FB}"
IID_IUIAUTOMATION_SPREADSHEET_PATTERN = "{7517A7C8-FAAE-4DE9-9F08-29B91E8595C1}"
IID_IUIAUTOMATION_SPREADSHEET_ITEM_PATTERN = "{7D4FB86C-8D34-40E1-8E83-62C15204E335}"
IID_IUIAUTOMATION_TEXT_CHILD_PATTERN = "{6552B038-AE05-40C8-ABFD-AA08352AAB86}"
IID_IUIAUTOMATION_DRAG_PATTERN = "{1DC7B570-1F54-4BAD-BCDA-D36A722FB7BD}"
IID_IUIAUTOMATION_DROP_TARGET_PATTERN = "{69A095F7-EEE4-430E-A46B-FB73B1AE39A5}"
IID_IUIAUTOMATION_TEXT_EDIT_PATTERN = "{17E21576-996C-4870-99D9-BFF323380C06}"
IID_IUIAUTOMATION_CUSTOM_NAVIGATION_PATTERN = "{01EA217A-1766-47ED-A6CC-ACF492854B1F}"

# --- Element 的扩展接口（取更多属性时用）---
IID_IUIAUTOMATION_ELEMENT2 = "{6749C683-F70D-4487-A698-5FD4E98E352F}"
IID_IUIAUTOMATION_ELEMENT3 = "{8471DF34-AEE0-4A01-A7DE-7DB9AF12C296}"
IID_IUIAUTOMATION_ELEMENT4 = "{3B6B0F9C-FA1C-4C36-B7A9-6C9B0E6B0A31}"

# --- 主接口的扩展版本 ---
IID_IUIAUTOMATION2 = "{34723AFF-0C9D-49D0-9896-7AB52DF8CD8A}"
IID_IUIAUTOMATION3 = "{73C2FB66-046D-490A-8A65-1C8F24DF0E8C}"
IID_IUIAUTOMATION4 = "{1189C02A-05F8-4319-8E21-E817E3BE0BF5}"
IID_IUIAUTOMATION5 = "{25F700C8-D816-4057-A56B-9D2B0AADD9FB}"
IID_IUIAUTOMATION6 = "{AAE6825B-09C6-4A2E-B1DF-A5CAE4D4F3A9}"

# --------------------------------------------------------------------------
# UWP 应用激活
# --------------------------------------------------------------------------
CLSID_APPLICATION_ACTIVATION_MANAGER = "{45BA127D-10A8-46EA-8AB7-56EA9078943C}"
IID_IAPPLICATION_ACTIVATION_MANAGER = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"

# --------------------------------------------------------------------------
# 其他
# --------------------------------------------------------------------------
CLSID_SHELL_WINDOWS = "{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"

#: 常用接口名 → IID 的映射，供 ComPtr.query_interface 用字符串调用
NAMED_IIDS = {
    "IUnknown": IID_IUNKNOWN,
    "IDispatch": IID_IDISPATCH,
    "IUIAutomation": IID_IUIAUTOMATION,
    "IUIAutomationElement": IID_IUIAUTOMATION_ELEMENT,
    "IUIAutomationElementArray": IID_IUIAUTOMATION_ELEMENT_ARRAY,
    "IUIAutomationTreeWalker": IID_IUIAUTOMATION_TREE_WALKER,
    "IUIAutomationCondition": IID_IUIAUTOMATION_CONDITION,
    "IUIAutomationCacheRequest": IID_IUIAUTOMATION_CACHE_REQUEST,
    "IUIAutomationInvokePattern": IID_IUIAUTOMATION_INVOKE_PATTERN,
    "IUIAutomationValuePattern": IID_IUIAUTOMATION_VALUE_PATTERN,
    "IUIAutomationTogglePattern": IID_IUIAUTOMATION_TOGGLE_PATTERN,
    "IUIAutomationExpandCollapsePattern": IID_IUIAUTOMATION_EXPAND_COLLAPSE_PATTERN,
    "IUIAutomationScrollPattern": IID_IUIAUTOMATION_SCROLL_PATTERN,
    "IUIAutomationSelectionItemPattern": IID_IUIAUTOMATION_SELECTION_ITEM_PATTERN,
    "IUIAutomationRangeValuePattern": IID_IUIAUTOMATION_RANGE_VALUE_PATTERN,
    "IUIAutomationTextPattern": IID_IUIAUTOMATION_TEXT_PATTERN,
    "IUIAutomationTextRange": IID_IUIAUTOMATION_TEXT_RANGE,
    "IUIAutomationWindowPattern": IID_IUIAUTOMATION_WINDOW_PATTERN,
    "IUIAutomationLegacyIAccessiblePattern": IID_IUIAUTOMATION_LEGACY_ACCESSIBLE_PATTERN,
    "IUIAutomationScrollItemPattern": IID_IUIAUTOMATION_SCROLL_ITEM_PATTERN,
    "IUIAutomationGridPattern": IID_IUIAUTOMATION_GRID_PATTERN,
    "IUIAutomationGridItemPattern": IID_IUIAUTOMATION_GRID_ITEM_PATTERN,
    "IUIAutomationTablePattern": IID_IUIAUTOMATION_TABLE_PATTERN,
    "IUIAutomationTableItemPattern": IID_IUIAUTOMATION_TABLE_ITEM_PATTERN,
    "IUIAutomationDockPattern": IID_IUIAUTOMATION_DOCK_PATTERN,
    "IUIAutomationTransformPattern": IID_IUIAUTOMATION_TRANSFORM_PATTERN,
    "IUIAutomationSynchronizedInputPattern":
        IID_IUIAUTOMATION_SYNCHRONIZED_INPUT_PATTERN,
    "IUIAutomationVirtualizedItemPattern":
        IID_IUIAUTOMATION_VIRTUALIZED_ITEM_PATTERN,
    "IUIAutomationSelectionPattern": IID_IUIAUTOMATION_SELECTION_PATTERN,
    "IApplicationActivationManager": IID_IAPPLICATION_ACTIVATION_MANAGER,
}


# --------------------------------------------------------------------------
# WinRT IID / CLSID（阶段七）
# --------------------------------------------------------------------------
# 来源：windows-rs 生成的绑定（crates/libs/windows/src/Windows/**/mod.rs）。
# **不是凭印象写的** —— WinRT 的 IID 没有任何"可推导的规律"，
# 记错一个数字就是调用到别的接口上。
#
# 这些值在 avatar/_win32/winrt.py 里被引用；放在这里是为了让
# "GUID 常量中心"这个职责完整 —— 想知道某个 GUID 是什么，
# 只看这一个文件就够了。

#: ``IActivationFactory`` —— 所有 WinRT 类的工厂都实现它（槽位 [6] = ActivateInstance）
IID_IActivationFactory = "{00000035-0000-0000-C000-000000000046}"

#: ``IAsyncInfo`` —— ``IAsyncOperation`` 继承它（槽位 [10..13]）
IID_IAsyncInfo = "{00000036-0000-0000-C000-000000000046}"

# ---- Windows.Media.Ocr ----
#: ``IOcrEngine``（实例接口）：[6] RecognizeAsync  [7] RecognizerLanguage
IID_IOcrEngine = "{5A14BC41-5B76-3140-B680-8825562683AC}"
#: ``IOcrEngineStatics``（静态接口）：[6..10]，
#: **[10] 是 TryCreateFromUserProfileLanguages**
IID_IOcrEngineStatics = "{5BFFA85A-3384-3540-9940-699120D428A8}"
#: ``IOcrResult``：[6] Lines  [7] TextAngle  [8] Text
IID_IOcrResult = "{9BD235B2-175B-3D6A-92E2-388C206E2F63}"
#: ``IOcrLine``：[6] Words  [7] Text
IID_IOcrLine = "{0043A16F-E31F-3A24-899C-D444BD088124}"
#: ``IOcrWord``：[6] BoundingRect  [7] Text
IID_IOcrWord = "{3C2A477A-5CD9-3525-BA2A-23D1E0A68A1D}"

# ---- Windows.Graphics.Imaging ----
#: ``ISoftwareBitmapStatics``：[9] CreateCopyFromBuffer
IID_ISoftwareBitmapStatics = "{DF0385DB-672F-4A9D-806E-C2442F343E86}"
#: ``ISoftwareBitmap``（实例接口，默认接口）
IID_ISoftwareBitmap = "{689E4C51-5CEA-4E1A-8F15-8DC0C6B4B1B8}"
#: ``ISoftwareBitmapFactory``：[6] Create  [7] CreateWithAlpha
IID_ISoftwareBitmapFactory = "{C99FEB69-2D62-4D47-A6B3-4FDB6A07FDF8}"
#: ``IBitmapDecoder``：[6] get_FrameCount … [10] GetSoftwareBitmapAsync
IID_IBitmapDecoder = "{ACEF22BA-2AF9-4E3A-9E18-CC0D3F3E2E1A}"
#: ``IBitmapFrame``：[6] GetThumbnailAsync … [9] GetSoftwareBitmapAsync
IID_IBitmapFrame = "{72A49A1C-8081-438D-91BC-94ECFC8185C6}"
#: ``IBitmapBuffer``：[6] GetPlaneCount … [7] GetPlaneDescription
IID_IBitmapBuffer = "{A53E04C4-399C-438C-B28F-A63A6B83D1A1}"

# ---- Windows.Storage.Streams ----
#: ``IBufferFactory`` —— ``Buffer`` 的构造要带 capacity，所以不能用
#: ``ActivateInstance``，必须走这个工厂。槽位 [6] = ``Create(capacity, out)``
IID_IBufferFactory = "{71AF914D-C10F-484B-BC50-14BC623B3A27}"

#: ``IBufferByteAccess`` —— **经典 COM 接口**（前 3 槽是 IUnknown，
#: **没有** IInspectable）。WinRT 的 ``Buffer`` 实现了它，
#: 用它拿裸指针直接写像素。槽位 [3] = ``Buffer()``
#:
#: ⚠ 这个值来自 ``robuffer.h`` 的 ``MIDL_INTERFACE``。
#: 它与 ``IBuffer`` 的 IID 只差最后一位（``0FE0`` vs ``0FE1``），
#: 极易混淆 —— 所以 :mod:`avatar.uia.ocr_fallback` 里的解析逻辑
#: **不靠猜**：QI 成功后会再验证 ``vtable[3]`` 真能给出非空指针。
IID_IBufferByteAccess = "{905A0FE0-BC53-11DF-8C49-001E4FC686DA}"

#: ``IBuffer``（WinRT 接口）。**本模块不主动用它** ——
#: ``IBufferFactory::Create`` 返回的就是它，直接持有指针即可。
#: 登记在这里是为了对照，避免和上面那个经典 COM 接口搞混。
IID_IBuffer = "{905A0FE1-BC53-11DF-8C49-001E4FC686DA}"

# ---- WinRT 类名（激活用字符串，不是 CLSID）----
#: ``RoGetActivationFactory`` 收的是**类名字符串**，不是 CLSID。
#: 这里登记常用类名，避免各模块各写一遍字符串。
WINRT_CLASS_OCR_ENGINE = "Windows.Media.Ocr.OcrEngine"
WINRT_CLASS_SOFTWARE_BITMAP = "Windows.Graphics.Imaging.SoftwareBitmap"
WINRT_CLASS_BITMAP_DECODER = "Windows.Graphics.Imaging.BitmapDecoder"
WINRT_CLASS_BUFFER = "Windows.Storage.Streams.Buffer"
WINRT_CLASS_DATA_WRITER = "Windows.Storage.Streams.DataWriter"
WINRT_CLASS_LANGUAGE = "Windows.Globalization.Language"


#: WinRT IID 的名字表（供 ``iid_of`` 查询）。
NAMED_WINRT_IIDS = {
    "IActivationFactory": IID_IActivationFactory,
    "IAsyncInfo": IID_IAsyncInfo,
    "IOcrEngine": IID_IOcrEngine,
    "IOcrEngineStatics": IID_IOcrEngineStatics,
    "IOcrResult": IID_IOcrResult,
    "IOcrLine": IID_IOcrLine,
    "IOcrWord": IID_IOcrWord,
    "ISoftwareBitmapStatics": IID_ISoftwareBitmapStatics,
    "ISoftwareBitmap": IID_ISoftwareBitmap,
    "ISoftwareBitmapFactory": IID_ISoftwareBitmapFactory,
    "IBitmapDecoder": IID_IBitmapDecoder,
    "IBitmapFrame": IID_IBitmapFrame,
    "IBitmapBuffer": IID_IBitmapBuffer,
    "IBufferFactory": IID_IBufferFactory,
    "IBufferByteAccess": IID_IBufferByteAccess,
    "IBuffer": IID_IBuffer,
}


def winrt_iid_of(name: str) -> GUID:
    """WinRT 接口名 → GUID。**找不到就抛 KeyError**（不猜）。"""
    key = str(name or "").strip()
    if key.startswith("{"):
        return guid(key)
    if key in NAMED_WINRT_IIDS:
        return guid(NAMED_WINRT_IIDS[key])
    for k, v in NAMED_WINRT_IIDS.items():
        if k.lower() == key.lower():
            return guid(v)
    raise KeyError("未知 WinRT 接口名: %r" % name)


def iid_of(name_or_guid: str) -> GUID:
    """接口名或 GUID 字符串 → GUID。名字查 NAMED_IIDS。"""
    if name_or_guid.startswith("{"):
        return guid(name_or_guid)
    key = name_or_guid.strip()
    if key in NAMED_IIDS:
        return guid(NAMED_IIDS[key])
    # 容忍大小写
    for k, v in NAMED_IIDS.items():
        if k.lower() == key.lower():
            return guid(v)
    raise KeyError("未知接口名: %r（请显式传 GUID）" % name_or_guid)
