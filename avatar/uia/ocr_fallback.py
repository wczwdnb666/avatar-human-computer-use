# -*- coding: utf-8 -*-
"""OCR 降级通道 —— 走 Windows.Media.Ocr（WinRT），纯 ctypes 手搓。

## 这一层解决什么

UIA 只能看到"暴露了无障碍信息的控件"。有一大类东西它看不到：

- 自绘界面（游戏、Electron 的 canvas、老式 MFC 程序）
- 图片里的文字（截图、扫描件、聊天记录）
- 没实现 UIA provider 的第三方程序

对这些东西，"屏幕上有字"是唯一的线索。本模块就是那条线索。

## 走的是哪条路

    GDI 抓屏（avatar.vision.capture）
        ↓  BGRA 像素（自下而上的 DIB）
    SoftwareBitmap（WinRT）
        ↓  CreateCopyWithAlphaFromBuffer(buffer, Bgra8, w, h, Ignore)
    OcrEngine.TryCreateFromUserProfileLanguages()
        ↓
    RecognizeAsync(SoftwareBitmap) → IAsyncOperation<OcrResult>
        ↓  轮询 get_Status 等完成
    OcrResult → Lines → Words → (Text, BoundingRect)

**全程 ctypes，零 SDK。** 走 WinRT 是因为 Win32 侧根本没有 OCR 能力 ——
这不是选型偏好，是"只有这一条路"。

## ⚠ 一个必须先知道的事实：OCR 可能**永远不可用**

`Windows.Media.Ocr` 在现代 Windows 上**要求应用具有包标识**
（packaged identity，即 MSIX 打包）。普通 exe（包括 Python 进程）
调用 `TryCreateFromUserProfileLanguages` 会**返回 null** ——
HRESULT 是成功的，但引擎是空的。

这不是本模块的 bug，是 Windows 的策略。所以：

- :func:`is_available` **真的去探测**，不假设
- :func:`explain_unavailable` 会**明确说出**是哪一类原因
- :func:`set_engine` 支持三种内置引擎选择（``"windows"`` / ``"tesseract"``
  / ``"auto"``），也允许注入自定义函数（PaddleOCR / 云 OCR）

**降级链的正确态度是"这一层不可用就往下走"，不是"想办法绕过系统限制"。**

## ✅ 旁路：内置 Tesseract 引擎（2026-10-16）

上面那条死路（位图通路不通）有了绕法：``uia/ocr_tesseract.py``
用 subprocess 调外部 ``tesseract.exe`` —— 不碰 WinRT，把位图存成
临时 BMP 文件，解析 TSV 输出。

默认 ``set_engine("auto")``：WinRT 不可用时**自动降级**到 Tesseract，
调用方无感。Tesseract 是**可选依赖** —— 没装只是这条旁路不通，
UIA 通道完全不受影响。

    O.set_engine("auto")        # 默认：WinRT 优先，挂了自动走 Tesseract
    O.set_engine("windows")     # 只用 WinRT（诊断 WinRT 用）
    O.set_engine("tesseract")   # 强制 Tesseract（WinRT 能跑也绕开它）

## ⚠ 已知问题（2026-10-07 实测）

本机（Windows，2880x1800）上：

- ✅ `RoInitialize` / `RoGetActivationFactory` 正常
- ✅ `OcrEngine.TryCreateFromUserProfileLanguages` **成功**，
  语言 `zh-Hans-CN` 可用
- ✅ `DataWriter` → `IBuffer` 通路正常（`Length` = 64000）
- ❌ `SoftwareBitmap.CreateCopyFromBuffer` 返回
  **`E_INVALIDARG (0x80070057)`**

已排查并**排除**的可能：IID 错（`GetIids` 验证三个接口 IID 全对）、
槽位错（`[9]` 四参数不崩、`[10]` 五参数不崩，其余槽位必崩）、
`BitmapPixelFormat` 值错（0~11 全试过）、buffer 长度错
（`capacity = length = w*h*4` 精确匹配）、图太大（64x64 同样失败）。

**未定位**。这是一条 WinRT 的深水区（`SoftwareBitmap` 的 ABI 细节），
继续挖的性价比低于"换一条路"。

**所以本模块的设计是"探测到位、如实报告、可换引擎"**：
:func:`is_available` 会一路探测到**位图通路**，不通就报不可用
（而不是"引擎能建就算可用"——那是假的可用性）。

## 能力边界

**做**：探测 OCR 可用性并给出**具体**原因；列出已安装的识别语言；
把 GDI 位图转成 WinRT 位图；识别整图或指定区域；
按文本查找（支持模糊）；把结果换算成客户区坐标；
**按引擎模式路由**（WinRT / Tesseract / 自定义，见 :func:`set_engine`）。

**不做**：

- **不做版面分析**。不识别表格结构、不合并段落、不做阅读顺序重排。
  WinRT 给的就是"行 → 词"，本模块原样透出。
- **不做图像预处理**。不二值化、不去噪、不放大。图不清楚就是不清楚 ——
  预处理是"让 OCR 猜得更准"，而猜出来的坐标拿去点击是危险的。
- **不缓存识别结果**。同一块区域重复识别会重复计算。调用方自己去重
  （界面变了结果就该变，缓存反而是错的）。
- **不做多语言混合识别**。一次调用一个引擎（一种语言）。
  中英混排请用 `chi_sim+eng` 这类引擎自己支持的组合。
- **不硬解验证题**（硬约束 #9 全项目生效）。识别出"拖动滑块"这四个字
  的目的是**叫人**，不是去算缺口在哪。
- **不改系统设置**。缺语言包就是缺，本模块只报告，不尝试安装。
"""

from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .._win32 import winrt as W
from .._win32 import guids as G
from .._win32.com import succeeded
from ..errors import UnsupportedError

__all__ = [
    "CAPABILITY",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_TIMEOUT",
    "OcrUnavailable",
    "OcrWord",
    "OcrHit",
    "set_engine",
    "engine_mode",
    "engine_name",
    "is_available",
    "winrt_available",
    "tesseract_available",
    "unavailable_reason",
    "explain_unavailable",
    "probe_bitmap_path",
    "languages",
    "recognize",
    "recognize_window",
    "find_text",
    "find_all_text",
    "read_region",
    "describe_environment",
    "selfcheck",
]

#: 本模块在降级链里的名字
CAPABILITY = "ocr"

#: 默认最低置信度。**WinRT OCR 不提供置信度**，所以本模块统一给 1.0，
#: 这个常量只在注入别的引擎时有用。
DEFAULT_MIN_CONFIDENCE = 0.6

#: 单次识别的超时（秒）。
DEFAULT_TIMEOUT = 15.0

#: ``BitmapPixelFormat`` 枚举：BGRA8
_PIXEL_BGRA8 = 2
#: ``BitmapAlphaMode`` 枚举：Ignore（不预乘、不解释 alpha）
_ALPHA_IGNORE = 2

#: 识别语言的可读名 → BCP-47 标签（给 ``Language`` 构造用）。
LANGUAGE_TAGS = {
    "中文": "zh-Hans-CN",
    "简体中文": "zh-Hans-CN",
    "繁體中文": "zh-Hant-TW",
    "英语": "en-US",
    "英文": "en-US",
    "日语": "ja-JP",
    "韩语": "ko-KR",
    "德语": "de-DE",
    "法语": "fr-FR",
    "西班牙语": "es-ES",
    "俄语": "ru-RU",
}


class OcrUnavailable(UnsupportedError):
    """OCR 引擎不可用。上层捕获它沿降级链往下走。"""

    def __init__(self, message: str = "", **context):
        super().__init__(message or "OCR 不可用", capability=CAPABILITY,
                         **context)


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------
@dataclass
class OcrWord:
    """一个识别出的词/字块 —— **WinRT 层的原始结果**。

    坐标是**相对于传给 :func:`recognize` 的那张图**（左上角原点，
    单位像素）。要换算成客户区坐标请用 :func:`recognize_window`，
    它会把偏移加回去。
    """

    text: str = ""
    #: ``(left, top, right, bottom)``，相对传入图像
    rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    #: 所属行号（同一行的词共享）
    line: int = -1
    #: 置信度。**WinRT OCR 不提供**，固定 1.0。
    confidence: float = 1.0
    #: 产生它的引擎名
    engine: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "rect": list(self.rect),
                "line": self.line, "confidence": round(self.confidence, 3),
                "engine": self.engine}

    def __str__(self) -> str:
        return "OcrWord(%r, %s)" % (self.text, self.rect)


@dataclass
class OcrHit:
    """一次文字识别的结果 —— **带窗口上下文**。

    ## 坐标约定（重要）

    ``client_rect`` 是**相对所属窗口客户区**的矩形 —— 与 ``uia.Element``
    完全一致的口径。不提供"绝对屏幕坐标"字段，需要时由调用方用
    ``layout.client_to_screen`` 当场换算。

    理由与 ``uia`` 层相同：屏幕坐标会因窗口移动而静默失效。
    """

    text: str = ""
    #: 所属顶层窗口句柄（必须非零 —— 否则无法换算坐标）
    window: int = 0
    #: 客户区坐标矩形 ``(left, top, right, bottom)``
    client_rect: Optional[Tuple[int, int, int, int]] = None
    #: 置信度 0.0~1.0
    confidence: float = 0.0
    #: 产生这个结果的引擎名
    engine: str = ""
    #: 引擎的原始输出，排查用
    raw: Dict = field(default_factory=dict)

    @property
    def center_client(self) -> Optional[Tuple[int, int]]:
        """客户区坐标下的中心点。"""
        if not self.client_rect:
            return None
        l, t, r, b = self.client_rect
        return ((l + r) // 2, (t + b) // 2)

    def to_dict(self) -> Dict:
        return {
            "text": self.text,
            "window": self.window,
            "client_rect": list(self.client_rect) if self.client_rect else None,
            "confidence": round(float(self.confidence), 4),
            "engine": self.engine,
        }

    def __str__(self):
        return "OcrHit(%r conf=%.2f win=0x%X)" % (
            self.text, self.confidence, self.window)


# --------------------------------------------------------------------------
# 引擎注册与路由
# --------------------------------------------------------------------------
#: 真实识别函数的注册位。约定签名::
#:
#:     fn(window: int, region: Optional[tuple], min_confidence: float,
#:        lang: str) -> List[OcrHit]
#:
#: ``region`` 是**客户区坐标**矩形，None 表示整窗。
_ENGINE: Optional[Callable] = None
_ENGINE_NAME = ""

#: 引擎模式：``"auto"``（WinRT 优先，挂了降级 Tesseract）/
#: ``"windows"``（只用 WinRT）/ ``"tesseract"``（强制 Tesseract）/
#: ``"custom"``（注入的可调用对象）。
_ENGINE_MODE = "auto"

#: 探测结果的缓存。``None`` = 还没探测过。
_PROBE: Dict[str, Any] = {"done": False, "ok": False, "reason": "",
                          "engine": "", "langs": []}
_PROBE_LOCK = threading.Lock()


def set_engine(fn, name: str = "") -> None:
    """选择或注册 OCR 引擎。

    ## 三种用法

    1. **字符串选内置引擎**（推荐）::

           set_engine("auto")        # WinRT 优先，不可用自动降级 Tesseract
           set_engine("windows")     # 只用 Windows.Media.Ocr
           set_engine("tesseract")   # 强制走 Tesseract（旁路）

    2. **注入自定义函数**（旧协议，测试与第三方引擎用）::

           def my_engine(window, region, min_conf, lang) -> [OcrHit]: ...
           set_engine(my_engine, "paddleocr")

    3. ``set_engine(None)`` —— 回到默认的 auto。

    ## 为什么要这个口子

    因为 Windows.Media.Ocr 在很多环境里用不了（位图通路不通、
    缺包标识、缺语言包），而"OCR 不可用"不该让整个降级链失效。

    这也是**测试隔离**的手段：塞一个假引擎就能测上层逻辑，
    不必依赖真机上有 OCR。
    """
    global _ENGINE, _ENGINE_NAME, _ENGINE_MODE

    if callable(fn):
        _ENGINE = fn
        _ENGINE_NAME = str(name or "custom")
        _ENGINE_MODE = "custom"
    elif isinstance(fn, str):
        mode = fn.strip().lower()
        if mode in ("auto",):
            _ENGINE, _ENGINE_NAME, _ENGINE_MODE = None, "", "auto"
        elif mode in ("windows", "winrt", "native"):
            _ENGINE, _ENGINE_NAME, _ENGINE_MODE = None, "", "windows"
        elif mode in ("tesseract",):
            _ENGINE, _ENGINE_NAME, _ENGINE_MODE = None, "", "tesseract"
        else:
            raise ValueError(
                "set_engine 的名字只能是 'auto' / 'windows' / 'tesseract'，"
                "或传一个可调用对象，收到：%r" % fn)
    elif fn is None:
        _ENGINE, _ENGINE_NAME, _ENGINE_MODE = None, "", "auto"
    else:
        raise ValueError(
            "set_engine 只接受字符串（引擎名）或可调用对象，收到：%r"
            % type(fn).__name__)

    with _PROBE_LOCK:
        _PROBE["done"] = False


def engine_mode() -> str:
    """当前引擎模式：``auto`` / ``windows`` / ``tesseract`` / ``custom``。"""
    return _ENGINE_MODE


def _tesseract_mod():
    """拿 Tesseract 引擎模块。**延迟 import** —— 它顶部 import 了本模块，
    顶部反向 import 会成环。"""
    from . import ocr_tesseract as OT
    return OT


def winrt_available() -> bool:
    """只查 Windows 原生 OCR（不走 Tesseract 降级）。"""
    return bool(_probe()["ok"])


def tesseract_available() -> bool:
    """只查 Tesseract 旁路。"""
    try:
        return _tesseract_mod().is_available()
    except Exception:      # noqa: BLE001
        return False


def engine_name() -> str:
    """当前**实际生效**的引擎名（不是模式名）。"""
    if _ENGINE is not None:
        return _ENGINE_NAME or "custom"
    if _ENGINE_MODE == "windows":
        return "windows.media.ocr" if winrt_available() else ""
    if _ENGINE_MODE == "tesseract":
        return "tesseract" if tesseract_available() else ""
    # auto：报告真正会用的那个
    if winrt_available():
        return "windows.media.ocr"
    if tesseract_available():
        return "tesseract"
    return ""


# --------------------------------------------------------------------------
# GDI 位图 → SoftwareBitmap
# --------------------------------------------------------------------------
def _bgra_top_down(bmp: Any) -> bytes:
    """把 ``vision.capture.Bitmap`` 的像素转成**自上而下**的 BGRA。

    ## 为什么必须翻转

    GDI 的 DIB 是**自下而上**存的（第一行是屏幕最下面那行），
    而 WinRT 的 SoftwareBitmap 期望**自上而下**。

    不翻转的后果不是"崩"，是**图倒过来** —— OCR 对倒着的字识别率
    极低，但不会报错，只会返回乱码或空。这种"安静地错"最难查。
    """
    w = int(getattr(bmp, "width", 0) or 0)
    h = int(getattr(bmp, "height", 0) or 0)
    if w <= 0 or h <= 0:
        raise OcrUnavailable("位图尺寸异常：%dx%d" % (w, h))

    stride = int(getattr(bmp, "stride", 0) or (w * 4))
    src = bmp.pixels
    row_bytes = w * 4
    out = bytearray(w * h * 4)

    for y in range(h):
        s = (h - 1 - y) * stride
        out[y * row_bytes:(y + 1) * row_bytes] = src[s:s + row_bytes]
    return bytes(out)


def _make_buffer_from_bytes(data: bytes) -> W.WinRtPtr:
    """把一段字节塞进 WinRT ``IBuffer``。**调用方负责 close。**

    ## 为什么不走 IBufferByteAccess

    最直接的办法是 ``IBufferByteAccess::Buffer()`` 拿裸指针直接 memmove。
    但那个接口是**经典 COM**，它的 IID 与 ``IBuffer`` 只差最后一位
    （``...0FE0`` vs ``...0FE1``），QI 错了不会报错 ——
    会在 ``IBuffer`` 的 vtable[3]（``GetIids``）上按 ``Buffer(byte**)``
    的签名调用，**直接访问违例**。本模块实测崩过一次。

    ``DataWriter`` 这条路全是 WinRT 接口，IID 不用猜：

        ActivateInstance("...DataWriter") → IDataWriter
        WriteBytes(length, ptr)           → 拷进去
        DetachBuffer()                    → 交出 IBuffer

    ## 槽位换算

    ``IDataWriter`` 的第一个自定义方法（``UnstoredBufferLength``）
    落在 vtable[6]（紧接 IInspectable 的 6 个固定槽），
    所以 ``slot = 6 + (声明序号 - 1)``：

        WriteBytes   （第 7 个）→ [12]
        DetachBuffer （第 26 个）→ [31]
    """
    if not data:
        raise OcrUnavailable("要写入的字节为空")

    fac = W.activation_factory(G.WINRT_CLASS_DATA_WRITER)
    try:
        dw = W.activate_instance(fac)
    finally:
        fac.close()

    # ⚠ 这个缓冲区必须活到 WriteBytes 返回 —— 它是同步调用，函数内安全
    payload = ctypes.create_string_buffer(bytes(data), len(data))
    try:
        hr = W.vtbl_call(
            dw, W.VTBL_DATA_WRITER["WriteBytes"], ctypes.c_long,
            [ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_uint32(len(data)),
            ctypes.cast(payload, ctypes.c_void_p))
        if not succeeded(hr):
            raise OcrUnavailable(
                "DataWriter.WriteBytes 失败（HRESULT 0x%08X）"
                % (hr & 0xFFFFFFFF), method="IDataWriter::WriteBytes")

        out = ctypes.c_void_p()
        hr = W.vtbl_call(
            dw, W.VTBL_DATA_WRITER["DetachBuffer"], ctypes.c_long,
            [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
        if not succeeded(hr) or not out.value:
            raise OcrUnavailable(
                "DataWriter.DetachBuffer 失败（HRESULT 0x%08X）"
                % (hr & 0xFFFFFFFF), method="IDataWriter::DetachBuffer")
    finally:
        dw.close()

    return W.WinRtPtr(out, "IBuffer")


def bitmap_to_software_bitmap(bmp: Any) -> W.WinRtPtr:
    """``vision.capture.Bitmap`` → WinRT ``SoftwareBitmap``。

    调用方**负责 close** 返回的对象。

    ## 三步

    1. 造一个 ``w*h*4`` 的 WinRT ``Buffer``
    2. 通过 ``IBufferByteAccess`` 拿到裸指针，把 BGRA 数据 memmove 进去
    3. ``SoftwareBitmap.CreateCopyWithAlphaFromBuffer`` 深拷贝成位图

    第 3 步用**带 alpha 参数**的那个重载（vtable[10]）而不是简版（[9]）——
    简版默认 ``Premultiplied``，而 GDI 抓来的图 alpha 是 0 或 255
    且**没有预乘**。用 ``Ignore`` 让 WinRT 不解释 alpha，避免
    "整张图变黑"这类安静的错误。
    """
    w = int(getattr(bmp, "width", 0) or 0)
    h = int(getattr(bmp, "height", 0) or 0)
    data = _bgra_top_down(bmp)
    need = w * h * 4
    if len(data) < need:
        raise OcrUnavailable(
            "像素数据不足：需要 %d 字节，只有 %d" % (need, len(data)))

    buf = _make_buffer_from_bytes(data[:need])

    statics = W.activation_factory(G.WINRT_CLASS_SOFTWARE_BITMAP,
                                   G.IID_ISoftwareBitmapStatics)
    out = ctypes.c_void_p()
    hr = W.vtbl_call(
        statics,
        W.VTBL_SOFTWARE_BITMAP_STATICS["CreateCopyWithAlphaFromBuffer"],
        ctypes.c_long,
        # source(IBuffer*) format(int) width(int) height(int) alpha(int) out**
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
        buf.ptr, ctypes.c_int(_PIXEL_BGRA8), ctypes.c_int(w),
        ctypes.c_int(h), ctypes.c_int(_ALPHA_IGNORE), ctypes.byref(out))
    statics.close()
    buf.close()

    if not succeeded(hr) or not out.value:
        raise OcrUnavailable(
            "SoftwareBitmap 创建失败（HRESULT 0x%08X）" % (hr & 0xFFFFFFFF),
            method="CreateCopyWithAlphaFromBuffer")
    return W.WinRtPtr(out, "SoftwareBitmap")


# --------------------------------------------------------------------------
# WinRT OCR 引擎
# --------------------------------------------------------------------------
class _Rect(ctypes.Structure):
    """``Windows.Foundation.Rect`` —— 4 个 FLOAT。"""
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float),
                ("Width", ctypes.c_float), ("Height", ctypes.c_float)]


def _create_engine(language: str = "") -> W.WinRtPtr:
    """创建一个 ``OcrEngine``。

    ``language`` 为空时走 ``TryCreateFromUserProfileLanguages``（vtable[10]），
    给了就用 ``TryCreateFromLanguage``（vtable[9]）。

    ## ⚠ 返回空指针不是异常

    这是 Windows 的策略：没有包标识时 ``TryCreate*`` **成功返回 null**。
    本函数把"null"翻成 :class:`OcrUnavailable` 并带上诊断，
    让上层能沿降级链往下走。
    """
    statics = W.activation_factory(G.WINRT_CLASS_OCR_ENGINE,
                                   G.IID_IOcrEngineStatics)
    eng = ctypes.c_void_p()
    try:
        if language:
            lang = _create_language(language)
            try:
                hr = W.vtbl_call(
                    statics,
                    W.VTBL_OCR_ENGINE_STATICS["TryCreateFromLanguage"],
                    ctypes.c_long,
                    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                    lang.ptr, ctypes.byref(eng))
            finally:
                lang.close()
        else:
            # ⚠ 槽位是 [10]，不是 [9] —— 写成 [9] 会调到
            #    TryCreateFromLanguage，把 ILanguage* 参数位置塞进
            #    一个 out 指针，行为未定义
            hr = W.vtbl_call(
                statics,
                W.VTBL_OCR_ENGINE_STATICS[
                    "TryCreateFromUserProfileLanguages"],
                ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(eng))
    finally:
        statics.close()

    if not succeeded(hr):
        raise OcrUnavailable(
            "TryCreateFromUserProfileLanguages 失败"
            "（HRESULT 0x%08X）" % (hr & 0xFFFFFFFF),
            method="IOcrEngineStatics", hint=_HR_HINT.get(hr & 0xFFFFFFFF, ""))

    if not eng.value:
        raise OcrUnavailable(
            "OCR 引擎创建返回 null。**HRESULT 是成功的，引擎是空的。**",
            method="IOcrEngineStatics",
            hint=explain_unavailable())

    return W.WinRtPtr(eng, "OcrEngine")


def _create_language(tag: str) -> W.WinRtPtr:
    """``Windows.Globalization.Language`` 实例。"""
    tag = LANGUAGE_TAGS.get(str(tag), str(tag))
    fac = W.activation_factory(G.WINRT_CLASS_LANGUAGE)
    inst = W.activate_instance(fac)
    fac.close()
    # Language 的构造是 Language(string)，没有无参构造 ——
    # 这里退一步：用工厂拿到的默认实例改不了 tag，直接报错更诚实
    inst.close()
    raise OcrUnavailable(
        "指定语言需要构造 Windows.Globalization.Language(%r)，"
        "而该类只有带参构造（本模块未实现）。"
        "请留空 language 参数使用系统默认语言。" % tag,
        method="Language")


#: HRESULT → 提示（OCR 相关）
_HR_HINT = {
    0x80070005: "访问被拒 —— Windows.Media.Ocr 要求应用具有**包标识**"
                "（MSIX 打包）。普通 exe 调不了。",
    0x80040154: "找不到 Windows.Media.Ocr 类 —— 系统版本过低"
                "（需要 Windows 10 及以上）。",
    0x8007007E: "缺少 OCR 组件（Windows.Media.Ocr.dll）。",
}


def _read_vector_view(vec: Any, count: int,
                      item_iid: str) -> List[W.WinRtPtr]:
    """读 ``IVectorView<T>`` 的前 ``count`` 个元素。

    槽位布局对所有 ``IVectorView<T>`` 都一样（[6] GetAt / [7] get_Size），
    所以**不需要 QI 到具体的泛型实例** —— 返回的指针已经是它了。
    """
    out: List[W.WinRtPtr] = []
    for i in range(max(0, int(count))):
        p = ctypes.c_void_p()
        hr = W.vtbl_call(vec, 6, ctypes.c_long,
                         [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                         ctypes.c_uint32(i), ctypes.byref(p))
        if not succeeded(hr) or not p.value:
            continue
        out.append(W.WinRtPtr(p, item_iid))
    return out


def _vector_size(vec: Any) -> int:
    n = ctypes.c_uint32(0)
    hr = W.vtbl_call(vec, 7, ctypes.c_long,
                     [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
    return int(n.value) if succeeded(hr) else 0


def _winrt_recognize(bitmap: Any, *, language: str = "",
                     timeout: float = DEFAULT_TIMEOUT,
                     region: Optional[Tuple[int, int, int, int]] = None,
                     ) -> List[OcrWord]:
    """**真正的 WinRT OCR 调用。** 返回相对传入图像的词列表。

    ``region`` 给了就先裁剪再识别（省时间也更准）。
    """
    if bitmap is None:
        raise OcrUnavailable("没有位图可识别")

    bmp = _crop(bitmap, region)
    off_x, off_y = (region[0], region[1]) if region else (0, 0)

    sbs = bitmap_to_software_bitmap(bmp)
    engine = None
    op = None
    result = None
    try:
        engine = _create_engine(language)

        # RecognizeAsync(SoftwareBitmap*, IAsyncOperation<OcrResult>**)
        op_p = ctypes.c_void_p()
        hr = W.vtbl_call(
            engine, W.VTBL_OCR_ENGINE["RecognizeAsync"], ctypes.c_long,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
            sbs.ptr, ctypes.byref(op_p))
        if not succeeded(hr) or not op_p.value:
            raise OcrUnavailable(
                "RecognizeAsync 调用失败（HRESULT 0x%08X）"
                % (hr & 0xFFFFFFFF), method="IOcrEngine::RecognizeAsync")

        op = W.WinRtPtr(op_p, "IAsyncOperation")
        result = W.await_async(op.ptr, timeout=timeout, close=True)
        op = None
        if not result.ok:
            # 有些实现允许返回 null 结果（图里没字）
            return []

        return _collect_words(result, off_x, off_y, language)
    finally:
        for obj in (result, op, engine, sbs):
            if obj is not None:
                try:
                    obj.close()
                except Exception:      # noqa: BLE001
                    pass


def _collect_words(result: Any, off_x: int, off_y: int,
                   language: str) -> List[OcrWord]:
    """``OcrResult`` → ``[OcrWord]``。"""
    words: List[OcrWord] = []
    lines_vec = ctypes.c_void_p()
    hr = W.vtbl_call(result.ptr, W.VTBL_OCR_RESULT["Lines"], ctypes.c_long,
                     [ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(lines_vec))
    if not succeeded(hr) or not lines_vec.value:
        return words

    vec = W.WinRtPtr(lines_vec, "IVectorView<OcrLine>")
    try:
        n_lines = _vector_size(vec)
        for li, line in enumerate(_read_vector_view(vec, n_lines, "OcrLine")):
            try:
                words.extend(_collect_line(line, li, off_x, off_y, language))
            finally:
                line.close()
    finally:
        vec.close()
    return words


def _collect_line(line: Any, line_index: int, off_x: int, off_y: int,
                  language: str) -> List[OcrWord]:
    out: List[OcrWord] = []
    words_vec = ctypes.c_void_p()
    hr = W.vtbl_call(line.ptr, W.VTBL_OCR_LINE["Words"], ctypes.c_long,
                     [ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(words_vec))
    if not succeeded(hr) or not words_vec.value:
        return out

    vec = W.WinRtPtr(words_vec, "IVectorView<OcrWord>")
    try:
        n = _vector_size(vec)
        for w in _read_vector_view(vec, n, "OcrWord"):
            try:
                text = _word_text(w)
                rect = _word_rect(w)
                if not text:
                    continue
                out.append(OcrWord(
                    text=text,
                    rect=(rect[0] + off_x, rect[1] + off_y,
                          rect[2] + off_x, rect[3] + off_y),
                    line=line_index,
                    confidence=1.0,          # WinRT OCR 不提供置信度
                    engine="windows.media.ocr"))
            finally:
                w.close()
    finally:
        vec.close()
    return out


def _word_text(w: Any) -> str:
    hs = ctypes.c_void_p()
    hr = W.vtbl_call(w.ptr, W.VTBL_OCR_WORD["Text"], ctypes.c_long,
                     [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(hs))
    if not succeeded(hr) or not hs.value:
        return ""
    try:
        return W.read_hstring(hs)
    finally:
        try:
            ctypes.windll.combase.WindowsDeleteString(hs)
        except Exception:      # noqa: BLE001
            pass


def _word_rect(w: Any) -> Tuple[int, int, int, int]:
    r = _Rect()
    hr = W.vtbl_call(w.ptr, W.VTBL_OCR_WORD["BoundingRect"], ctypes.c_long,
                     [ctypes.POINTER(_Rect)], ctypes.byref(r))
    if not succeeded(hr):
        return (0, 0, 0, 0)
    return (int(r.X), int(r.Y), int(r.X + r.Width), int(r.Y + r.Height))


def _crop(bitmap: Any, region: Optional[Tuple[int, int, int, int]]) -> Any:
    """裁剪位图。**没有 crop 方法就原样返回**（不报错）。"""
    if not region:
        return bitmap
    fn = getattr(bitmap, "crop", None)
    if callable(fn):
        try:
            l, t, r, b = region
            return fn(l, t, max(1, r - l), max(1, b - t))
        except Exception:      # noqa: BLE001
            return bitmap
    return bitmap


# --------------------------------------------------------------------------
# 可用性探测
# --------------------------------------------------------------------------
def probe_bitmap_path() -> str:
    """自测"GDI 位图 → SoftwareBitmap"这条通路。**返回空串表示通。**

    ## 为什么要单独测这一步

    因为"OCR 引擎能创建"和"能真的识别"是**两件事**。
    实测本机：`TryCreateFromUserProfileLanguages` 成功（引擎非空），
    但 `SoftwareBitmap.CreateCopyFromBuffer` 返回
    ``E_INVALIDARG (0x80070057)`` —— 图根本喂不进去。

    如果 :func:`is_available` 只看引擎，就会报"可用"，
    然后每次识别都失败 —— 那是**假的可用性**，比"不可用"更糟。

    所以探测必须一路走到位图为止。
    """
    try:
        w = h = 4
        data = bytes([0x80]) * (w * h * 4)
        buf = _make_buffer_from_bytes(data)
        try:
            statics = W.activation_factory(G.WINRT_CLASS_SOFTWARE_BITMAP,
                                           G.IID_ISoftwareBitmapStatics)
            out = ctypes.c_void_p()
            hr = W.vtbl_call(
                statics,
                W.VTBL_SOFTWARE_BITMAP_STATICS["CreateCopyFromBuffer"],
                ctypes.c_long,
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                 ctypes.POINTER(ctypes.c_void_p)],
                buf.ptr, ctypes.c_int(_PIXEL_BGRA8), ctypes.c_int(w),
                ctypes.c_int(h), ctypes.byref(out))
            statics.close()
            if not succeeded(hr):
                return ("SoftwareBitmap.CreateCopyFromBuffer 失败"
                        "（HRESULT 0x%08X）" % (hr & 0xFFFFFFFF))
            if not out.value:
                return "SoftwareBitmap 创建返回空指针"
            W.release(out)
            return ""
        finally:
            buf.close()
    except OcrUnavailable as exc:
        return str(exc)
    except W.WinRtError as exc:
        return str(exc)
    except Exception as exc:      # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, exc)


def _probe(force: bool = False) -> Dict[str, Any]:
    """探测 WinRT OCR 是否可用。**结果缓存**（探测一次约几十毫秒）。"""
    with _PROBE_LOCK:
        if _PROBE["done"] and not force:
            return dict(_PROBE)

        res: Dict[str, Any] = {"done": True, "ok": False, "reason": "",
                               "engine": "", "langs": []}
        try:
            st = W.ro_state()
            if not st["dll_loaded"]:
                res["reason"] = "combase.dll 不可用：%s" % st["dll_error"]
                _PROBE.update(res)
                return dict(_PROBE)

            eng = _create_engine("")
            eng.close()
            # ★ 引擎能建还不够 —— 位图通路也必须通
            bmp_err = probe_bitmap_path()
            if bmp_err:
                res["reason"] = (
                    "OCR 引擎可以创建，但位图转换不通：%s\n"
                    "  → 这是 WinRT 的 SoftwareBitmap 创建环节失败，"
                    "不是引擎本身的问题。可用 set_engine() 换引擎。"
                    % bmp_err)
                res["engine"] = "windows.media.ocr(位图通路不通)"
                _PROBE.update(res)
                return dict(_PROBE)
            res["ok"] = True
            res["engine"] = "windows.media.ocr"
            try:
                res["langs"] = _list_languages()
            except Exception:      # noqa: BLE001
                res["langs"] = []
        except OcrUnavailable as exc:
            res["reason"] = str(exc)
        except W.WinRtError as exc:
            res["reason"] = str(exc)
        except Exception as exc:      # noqa: BLE001
            res["reason"] = "%s: %s" % (type(exc).__name__, exc)

        _PROBE.update(res)
        return dict(_PROBE)


def is_available() -> bool:
    """OCR 现在能用吗？**真的去探测，不假设。**

    按当前引擎模式回答：

    - ``custom`` —— 注入了自定义引擎，直接 True（那个引擎自己负责可用性）
    - ``windows`` —— 只问 WinRT
    - ``tesseract`` —— 只问 Tesseract
    - ``auto``（默认）—— 两个里**有一个**能用就是 True

    ⚠ auto 档的识别入口会先调本函数再分派引擎 —— 这是**故意的**：
    阶段五的自检靠 monkeypatch 本函数模拟"引擎不可用"，
    入口必须尊重这个 patch，否则那个测试会在能跑 WinRT 的机器上假红。
    """
    if _ENGINE is not None:
        return True
    if _ENGINE_MODE == "windows":
        return winrt_available()
    if _ENGINE_MODE == "tesseract":
        return tesseract_available()
    return winrt_available() or tesseract_available()


def unavailable_reason() -> str:
    """不可用的原因（一句话）。可用时返回空串。

    auto 档且**两个引擎都不可用**时，组合两边的诊断 ——
    只报一边会让人以为另一边也没试。
    """
    if _ENGINE is not None:
        return ""
    if _ENGINE_MODE == "windows":
        return str(_probe()["reason"] or "")
    if _ENGINE_MODE == "tesseract":
        try:
            return str(_tesseract_mod().unavailable_reason() or "")
        except Exception:      # noqa: BLE001
            return "Tesseract 探测失败"
    # auto
    w_reason = str(_probe()["reason"] or "")
    t_reason = ""
    try:
        t_reason = str(_tesseract_mod().unavailable_reason() or "")
    except Exception:      # noqa: BLE001
        pass
    if w_reason and t_reason:
        return "Windows OCR：%s ｜ Tesseract：%s" % (
            w_reason.splitlines()[0][:80], t_reason)
    return w_reason or t_reason


def explain_unavailable() -> str:
    """**诊断为什么不可用**，给出可操作的建议。**永不抛。**

    这是本模块最有价值的部分之一 —— "OCR 用不了"这句话没有信息量，
    "因为你的程序没有包标识"才让人知道下一步该干什么。
    """
    if _ENGINE is not None:
        return "当前用的是自定义引擎「%s」，不涉及 WinRT OCR。" % _ENGINE_NAME

    tess_ok = tesseract_available()

    # 强制 tesseract 模式：只诊断 tesseract
    if _ENGINE_MODE == "tesseract":
        if tess_ok:
            try:
                return "Tesseract 可用（%s）。" % _tesseract_mod().version()
            except Exception:      # noqa: BLE001
                return "Tesseract 可用。"
        return _tesseract_diagnosis()

    # 强制 windows 模式：只诊断 WinRT
    if _ENGINE_MODE == "windows":
        if winrt_available():
            return "Windows 原生 OCR 可用。"
        return _winrt_diagnosis()

    # auto
    if winrt_available():
        return "Windows 原生 OCR 可用（auto 模式当前走它）。"
    if tess_ok:
        try:
            return ("Windows 原生 OCR 不可用，但 auto 模式已降级到 "
                    "Tesseract（%s）。" % _tesseract_mod().version())
        except Exception:      # noqa: BLE001
            return ("Windows 原生 OCR 不可用，但 auto 模式已降级到 "
                    "Tesseract。")
    # 两个都不可用：两边都报
    return _winrt_diagnosis() + "\n\n" + _tesseract_diagnosis()


def _tesseract_diagnosis() -> str:
    """Tesseract 旁路的诊断。**永不抛**。"""
    try:
        OT = _tesseract_mod()
        env = OT.describe_environment()
    except Exception as exc:      # noqa: BLE001
        return "Tesseract 模块加载或探测失败：%s" % exc

    lines = ["Tesseract 旁路：%s"
             % ("可用" if env.get("available") else "不可用"), ""]
    if env.get("available"):
        lines.append("  exe    : %s" % env.get("exe", ""))
        lines.append("  版本   : %s" % env.get("version", ""))
        langs = env.get("languages") or []
        lines.append("  语言包 : %s"
                     % (" / ".join(langs) if langs else "(没探到)"))
        if langs and "chi_sim" not in langs:
            lines.append("  ⚠ 没有 chi_sim —— 中文识别会失败。")
            lines.append("    下 chi_sim.traineddata 放进 tessdata 目录：")
            lines.append("    https://github.com/tesseract-ocr/tessdata_fast")
    else:
        lines.append("  原因   : %s" % env.get("reason", "(未说明)"))
        lines.append("")
        lines.append("  安装（二选一）：")
        lines.append("    a) 官方安装包（UB-Mannheim 维护，推荐）：")
        lines.append("       https://github.com/UB-Mannheim/tesseract/wiki")
        lines.append("       装完把安装目录加进 PATH"
                     "（默认 " + r"C:\Program Files\Tesseract-OCR" + "）")
        lines.append("    b) winget：winget install UB-Mannheim.TesseractOCR")
        lines.append("  中文语言包（chi_sim.traineddata）：")
        lines.append("    https://github.com/tesseract-ocr/tessdata_fast")
    return "\n".join(lines)


def _winrt_diagnosis() -> str:
    """WinRT OCR 的逐项排查（原 explain_unavailable 的走查体）。"""
    lines = ["OCR 不可用。逐项排查：", ""]

    # 1) 库层
    st = W.ro_state()
    lines.append("① WinRT 运行时")
    if not st["dll_loaded"]:
        lines.append("   ✗ combase.dll 加载失败：%s" % st["dll_error"])
        lines.append("   → 需要 Windows 8 及以上。")
        return "\n".join(lines)
    lines.append("   ✓ combase.dll 已加载（单元模型 %s）"
                 % (st["apartment"] or "未初始化"))
    if st["first_hr_hex"] and st["first_hr_hex"] not in ("0x00000000",
                                                         "0x00000001"):
        lines.append("   ⚠ 首次初始化 HRESULT %s：%s"
                     % (st["first_hr_hex"], st["first_error"]))

    # 2) 类是否可激活
    lines += ["", "② OCR 类是否可激活"]
    try:
        W.activation_factory(G.WINRT_CLASS_OCR_ENGINE,
                             G.IID_IOcrEngineStatics).close()
        lines.append("   ✓ Windows.Media.Ocr.OcrEngine 可激活")
    except W.WinRtError as exc:
        lines.append("   ✗ 激活失败：%s" % exc)
        lines.append("   → 系统版本过低或组件缺失。")
        return "\n".join(lines)

    # 3) 引擎能否创建（最常见的一关）
    lines += ["", "③ 引擎能否创建"]
    try:
        eng = _create_engine("")
        eng.close()
        lines.append("   ✓ 引擎创建成功")

        # 3b) 位图通路 —— **引擎能建不等于能识别**
        bmp_err = probe_bitmap_path()
        if bmp_err:
            lines += [
                "",
                "④ 位图通路（GDI 位图 → SoftwareBitmap）",
                "   ✗ %s" % bmp_err,
                "",
                "   这一步失败意味着：**图喂不进 OCR 引擎**。",
                "   引擎是好的，语言包也在，问题在 WinRT 的",
                "   SoftwareBitmap 创建环节（ABI 层）。",
                "",
                "   可操作的做法：",
                "     a) 本模块**内置** Tesseract 旁路（推荐，零代码）：",
                "        # 装了 Tesseract 后 auto 模式自动降级；或强制：",
                "        from avatar.uia import ocr_fallback as O",
                "        O.set_engine('tesseract')",
                "     b) 依赖 UIA 通道（不走 OCR）",
                "     c) 用视觉模板匹配（uia/vision_fallback）",
            ]
            return "\n".join(lines)
        lines += ["", "④ 已安装的识别语言"]
        langs = _list_languages()
        if langs:
            for l in langs:
                lines.append("   · %s（%s）" % (l.get("display", "?"),
                                               l.get("tag", "?")))
        else:
            lines.append("   ⚠ 一个都没有 —— 需要装语言包：")
            lines.append("     设置 → 时间和语言 → 语言 → 添加语言")
            lines.append("     → 勾选「可选语言功能 → 光学字符识别」")
        return "\n".join(lines)
    except OcrUnavailable as exc:
        lines.append("   ✗ 引擎创建返回 null")
        lines.append("   → **最可能的原因：本进程没有包标识（MSIX 打包）**")
        lines.append("     Windows.Media.Ocr 要求打包应用才能用。")
        lines.append("     可选做法：")
        lines.append("       a) 本模块**内置** Tesseract 旁路（装了 Tesseract 后")
        lines.append("          auto 模式自动降级，或 O.set_engine('tesseract')）")
        lines.append("       b) 把本工具打成 MSIX（改动大）")
        lines.append("       c) 依赖 UIA 通道（不走 OCR）")
        lines.append("   原始错误：%s" % exc)
        return "\n".join(lines)


def _list_languages() -> List[Dict[str, str]]:
    """列出已安装的 OCR 语言。失败返回空列表。"""
    statics = W.activation_factory(G.WINRT_CLASS_OCR_ENGINE,
                                   G.IID_IOcrEngineStatics)
    vec_p = ctypes.c_void_p()
    try:
        hr = W.vtbl_call(
            statics,
            W.VTBL_OCR_ENGINE_STATICS["AvailableRecognizerLanguages"],
            ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(vec_p))
        if not succeeded(hr) or not vec_p.value:
            return []
    finally:
        statics.close()

    out: List[Dict[str, str]] = []
    vec = W.WinRtPtr(vec_p, "IVectorView<Language>")
    try:
        n = _vector_size(vec)
        for lang in _read_vector_view(vec, n, "Language"):
            try:
                tag = _language_tag(lang)
                if tag:
                    out.append({"tag": tag, "display": _display_name(tag)})
            finally:
                lang.close()
    finally:
        vec.close()
    return out


def _language_tag(lang: Any) -> str:
    """``ILanguage::get_LanguageTag`` —— 槽位 [6]。"""
    hs = ctypes.c_void_p()
    try:
        hr = W.vtbl_call(lang.ptr, 6, ctypes.c_long,
                         [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(hs))
        if not succeeded(hr) or not hs.value:
            return ""
        try:
            return W.read_hstring(hs)
        finally:
            try:
                ctypes.windll.combase.WindowsDeleteString(hs)
            except Exception:      # noqa: BLE001
                pass
    except Exception:      # noqa: BLE001
        return ""


def _display_name(tag: str) -> str:
    for cn, t in LANGUAGE_TAGS.items():
        if t.lower() == str(tag).lower():
            return cn
    return tag


def languages() -> List[Dict[str, str]]:
    """已安装的 OCR 语言列表。**不可用时返回空列表（不抛）。**

    按引擎模式返回对应引擎的语言：

    - WinRT —— ``[{"tag": "zh-Hans-CN", "display": "中文"}, ...]``
    - Tesseract —— ``[{"tag": "chi_sim", "display": "chi_sim"}, ...]``
    """
    if _ENGINE is not None:
        return []
    try:
        if _ENGINE_MODE == "tesseract":
            return [{"tag": t, "display": t}
                    for t in _tesseract_mod().languages()]
        if _ENGINE_MODE == "windows":
            if not winrt_available():
                return []
            return _list_languages()
        # auto：优先 native，降级则 tesseract
        if winrt_available():
            return _list_languages()
        if tesseract_available():
            return [{"tag": t, "display": t}
                    for t in _tesseract_mod().languages()]
        return []
    except Exception:      # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# 识别入口
# --------------------------------------------------------------------------
def recognize(bitmap: Any, *,
              region: Optional[Tuple[int, int, int, int]] = None,
              language: str = "",
              timeout: float = DEFAULT_TIMEOUT) -> List[OcrWord]:
    """识别一张位图。返回**相对该图**的词列表。

    ## 参数

    - ``bitmap`` —— ``vision.capture.Bitmap``（或任何有
      ``width`` / ``height`` / ``stride`` / ``pixels`` 的对象）
    - ``region`` —— 先裁剪再识别，``(left, top, right, bottom)``
      相对传入图像。**限定范围既提速也提准**。
    - ``language`` —— BCP-47 标签或中文名。留空用系统默认。
    - ``timeout`` —— 单次识别超时（秒）。

    ## 不可用时

    抛 :class:`OcrUnavailable`，消息里带**具体原因**。
    上层捕获它沿降级链往下走。

    ## 引擎路由

    - ``tesseract`` 模式 —— 走 :mod:`avatar.uia.ocr_tesseract`
    - ``windows`` 模式 —— 只走 WinRT，不可用就抛
    - ``auto``（默认）—— 先问 :func:`is_available`（这一步尊重外部
      monkeypatch，阶段五自检靠它模拟"引擎不可用"），再分派：
      WinRT 可用走 WinRT，否则走 Tesseract
    """
    if _ENGINE is not None:
        # 自定义引擎走 OcrHit 协议，这里转成 OcrWord
        hits = _ENGINE(0, region, 0.0, language) or []
        out: List[OcrWord] = []
        for h in hits:
            if not isinstance(h, OcrHit):
                continue
            out.append(OcrWord(
                text=h.text, rect=h.client_rect or (0, 0, 0, 0),
                line=-1, confidence=h.confidence,
                engine=h.engine or _ENGINE_NAME))
        return out

    if _ENGINE_MODE == "tesseract":
        return _tesseract_recognize(bitmap, region=region,
                                    language=language, timeout=timeout)

    if _ENGINE_MODE == "windows":
        if not winrt_available():
            raise OcrUnavailable(unavailable_reason() or "OCR 不可用",
                                 hint=explain_unavailable())
        return _winrt_recognize(bitmap, language=language, timeout=timeout,
                                region=region)

    # auto
    if not is_available():
        raise OcrUnavailable(unavailable_reason() or "OCR 不可用",
                             hint=explain_unavailable())
    if winrt_available():
        return _winrt_recognize(bitmap, language=language, timeout=timeout,
                                region=region)
    return _tesseract_recognize(bitmap, region=region,
                                language=language, timeout=timeout)


def _tesseract_recognize(bitmap: Any, *,
                         region: Optional[Tuple[int, int, int, int]] = None,
                         language: str = "",
                         timeout: float = DEFAULT_TIMEOUT) -> List[OcrWord]:
    """走 Tesseract 旁路识别一张位图。坐标加回 region 原点，
    与 WinRT 路径的口径一致（相对**传入的原图**）。"""
    OT = _tesseract_mod()
    if not OT.is_available():
        raise OcrUnavailable(
            OT.unavailable_reason() or "Tesseract 不可用",
            hint=_tesseract_diagnosis())
    words = OT.recognize(bitmap, region=region, language=language,
                         timeout=timeout)
    if region and (region[0] or region[1]):
        off_x, off_y = int(region[0]), int(region[1])
        for w in words:
            l, t, r, b = w.rect
            w.rect = (l + off_x, t + off_y, r + off_x, b + off_y)
    return words


def recognize_window(window: int, *,
                     region: Optional[Tuple[int, int, int, int]] = None,
                     language: str = "",
                     timeout: float = DEFAULT_TIMEOUT,
                     ) -> List[OcrHit]:
    """抓窗口客户区 → 识别 → 换算成**客户区坐标**的 ``OcrHit`` 列表。

    ``region`` 是**客户区坐标**矩形，None 表示整窗。

    ## 引擎路由

    与 :func:`recognize` 同一套规则（tesseract / windows / auto）。
    """
    if not window:
        raise ValueError("recognize_window 必须传 window —— "
                         "没有窗口就无法把识别结果换算成客户区坐标")

    if _ENGINE is not None:
        return [h for h in (_ENGINE(window, region, 0.0, language) or [])
                if isinstance(h, OcrHit)]

    if _ENGINE_MODE == "tesseract":
        return _tesseract_recognize_window(window, region=region,
                                           language=language,
                                           timeout=timeout)

    if _ENGINE_MODE == "windows":
        if not winrt_available():
            raise OcrUnavailable(unavailable_reason() or "OCR 不可用",
                                 hint=explain_unavailable())
        return _winrt_recognize_window(window, region=region,
                                       language=language, timeout=timeout)

    # auto
    if not is_available():
        raise OcrUnavailable(unavailable_reason() or "OCR 不可用",
                             hint=explain_unavailable())
    if winrt_available():
        return _winrt_recognize_window(window, region=region,
                                       language=language, timeout=timeout)
    return _tesseract_recognize_window(window, region=region,
                                       language=language, timeout=timeout)


def _grab_client(window: int,
                 region: Optional[Tuple[int, int, int, int]]):
    """抓窗口客户区（region 给了就只抓那一块）。

    返回 ``(shot, off_x, off_y)`` —— off 是 region 左上角在客户区的
    偏移，识别结果的坐标要加回去。抓不到抛 :class:`OcrUnavailable`。

    ## ★ 为什么是 grab_window + 自己裁剪

    这里曾经调 ``C.grab_client(window, region=...)`` —— 而
    ``avatar.vision.capture`` **根本没有 grab_client**（它只有
    ``grab_screen`` / ``grab_window`` / ``grab_monitor``）。

    因为本机 WinRT 位图通路坏着、也没装 Tesseract，OCR 路径一直
    走不到抓屏这一步，这个 AttributeError 就潜伏了很久。
    **一旦 OCR 真正可用，它立刻炸。**

    现在的做法：抓整个客户区（``client_only=True`` 排除标题栏），
    再用 ``Bitmap.region()`` 按 region 裁。多一次内存拷贝，
    但换来的是"只依赖真实存在的 API"。

    ``region`` 是**客户区坐标** ``(left, top, right, bottom)``。
    """
    from ..vision import capture as C

    shot = C.grab_window(window, client_only=True)
    if shot is None:
        raise OcrUnavailable("抓取窗口客户区失败（hwnd=0x%X）" % window)

    if region:
        l, t, r, b = (int(v) for v in region)
        shot = shot.region(l, t, max(1, r - l), max(1, b - t))
        off_x, off_y = l, t
    else:
        off_x = off_y = 0
    return shot, off_x, off_y


def _words_to_hits(words: List[OcrWord], window: int,
                   off_x: int, off_y: int) -> List[OcrHit]:
    """``[OcrWord]``（相对图）→ ``[OcrHit]``（客户区坐标）。"""
    hits: List[OcrHit] = []
    for w in words:
        l, t, r, b = w.rect
        hits.append(OcrHit(
            text=w.text, window=int(window),
            client_rect=(l + off_x, t + off_y, r + off_x, b + off_y),
            confidence=w.confidence, engine=w.engine,
            raw={"line": w.line}))
    return hits


def _winrt_recognize_window(window: int, *,
                            region: Optional[Tuple[int, int, int, int]] = None,
                            language: str = "",
                            timeout: float = DEFAULT_TIMEOUT
                            ) -> List[OcrHit]:
    """WinRT 路径的 recognize_window（原实现，抽出来供路由复用）。"""
    shot, off_x, off_y = _grab_client(window, region)
    words = _winrt_recognize(shot, language=language, timeout=timeout)
    return _words_to_hits(words, window, off_x, off_y)


def _tesseract_recognize_window(window: int, *,
                                region: Optional[Tuple[int, int, int, int]] = None,
                                language: str = "",
                                timeout: float = DEFAULT_TIMEOUT
                                ) -> List[OcrHit]:
    """Tesseract 路径的 recognize_window。"""
    shot, off_x, off_y = _grab_client(window, region)
    words = _tesseract_recognize(shot, region=None, language=language,
                                 timeout=timeout)
    return _words_to_hits(words, window, off_x, off_y)


# --------------------------------------------------------------------------
# 查找（对外主接口）
# --------------------------------------------------------------------------
def _require_engine():
    """确认能用，否则抛。**保留旧签名**（阶段二的调用方在用）。"""
    if _ENGINE is None and not is_available():
        raise OcrUnavailable(
            unavailable_reason() or "OCR 不可用",
            hint=explain_unavailable())
    return _ENGINE


def _call_engine(window: int, region, min_confidence: float,
                 lang: str) -> List[OcrHit]:
    """统一的"拿结果"入口 —— 自定义引擎或 WinRT 引擎。"""
    if _ENGINE is not None:
        return [h for h in (_ENGINE(window, region, min_confidence, lang)
                            or []) if isinstance(h, OcrHit)]
    return recognize_window(window, region=region, language=lang)


def find_text(text: str, *, window: int, exact: bool = False,
              min_confidence: float = DEFAULT_MIN_CONFIDENCE,
              region: Optional[Tuple[int, int, int, int]] = None,
              lang: str = "",
              fuzzy: bool = False) -> Optional[OcrHit]:
    """在窗口里找一段文字，返回**最可信**的一个。

    ## 参数

    - ``window`` —— 顶层窗口句柄。**必填** —— OCR 结果要能换算成
      客户区坐标，没有窗口就无从换算。
    - ``text`` —— 要找的文字。``exact=False`` 时按包含匹配。
    - ``region`` —— **客户区坐标**矩形，限定搜索范围。
    - ``min_confidence`` —— 低于这个置信度的结果直接丢弃，
      **不返回"最像的"**（错的位置比没位置危险）。
    - ``fuzzy`` —— 容忍 OCR 的常见误认（``0``↔``O``、``1``↔``l``
      等）。默认关 —— 开了会提高误匹配率。
    """
    hits = find_all_text(text, window=window, exact=exact,
                         min_confidence=min_confidence, region=region,
                         lang=lang, fuzzy=fuzzy, limit=1)
    return hits[0] if hits else None


def find_all_text(text: str, *, window: int, exact: bool = False,
                  min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                  region: Optional[Tuple[int, int, int, int]] = None,
                  lang: str = "", fuzzy: bool = False,
                  limit: int = 0) -> List[OcrHit]:
    """在窗口里找**所有**匹配的文字，按置信度降序。"""
    if not window:
        raise ValueError("find_all_text 必须传 window —— "
                         "没有窗口就无法把识别结果换算成客户区坐标")

    hits = _call_engine(window, region, min_confidence, lang) or []
    hits = [h for h in hits if isinstance(h, OcrHit)]

    if exact:
        hits = [h for h in hits if h.text.strip() == str(text).strip()]
    elif fuzzy:
        want = _fold(text)
        hits = [h for h in hits if want in _fold(h.text)
                or _fold(h.text) in want]
    else:
        want = str(text)
        hits = [h for h in hits if want in h.text or h.text in want]

    hits.sort(key=lambda h: -h.confidence)
    return hits[:limit] if limit else hits


def read_region(window: int, *,
                region: Optional[Tuple[int, int, int, int]] = None,
                min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                lang: str = "") -> List[OcrHit]:
    """把一块区域的文字**全读出来**（不做匹配）。

    用途：给模型看一眼"这块显示的是什么"，或者做界面状态校验。
    """
    if not window:
        raise ValueError("read_region 必须传 window")
    return _call_engine(window, region, min_confidence, lang) or []


#: OCR 常见误认的折叠表。**只折叠视觉上真的像的** ——
#: 折叠太激进会让"12"和"l2"混为一谈。
#: ``str.maketrans`` 的键**必须长度 1** —— 多字符替换只能单独做。
_FOLD_MAP = str.maketrans({
    "0": "o", "o": "o",
    "1": "l", "l": "l", "|": "l",
    "5": "s",
    "8": "b",
    "2": "z",
})

#: 多字符替换表（先做，再做单字符折叠）。
#: 只放**视觉上真的会混**的组合 —— 放多了会把不同的字认成同一个。
_FOLD_MULTI = (("rn", "m"), ("vv", "w"), ("cl", "d"))


def _fold(s: str) -> str:
    """模糊匹配用的归一化：小写 + 去空白 + 折叠易混字符。"""
    t = str(s or "").lower()
    t = "".join(ch for ch in t if not ch.isspace())
    for a, b in _FOLD_MULTI:
        t = t.replace(a, b)
    return t.translate(_FOLD_MAP)


# --------------------------------------------------------------------------
# 环境描述
# --------------------------------------------------------------------------
def describe_environment() -> Dict[str, Any]:
    """OCR 环境快照。**永不抛** —— 诊断函数不该因为诊断失败而失败。

    快照里**两个引擎都探**：``winrt_available`` / ``tesseract`` 各一份，
    外加当前生效的 ``engine`` 与 ``engine_mode``。诊断"为什么走了
    旁路"要看两份，只看一份会漏。
    """
    try:
        env: Dict[str, Any] = {
            "capability": CAPABILITY,
            "engine": engine_name(),
            "engine_mode": _ENGINE_MODE,
            "custom_engine": _ENGINE is not None,
            "available": is_available(),
            "reason": unavailable_reason(),
            "languages": languages(),
            "winrt": W.ro_state(),
            "winrt_available": winrt_available(),
            "backend": "windows.media.ocr (WinRT, ctypes)",
        }
        try:
            tess = _tesseract_mod().describe_environment()
            env["tesseract"] = tess
            env["tesseract_available"] = bool(tess.get("available"))
        except Exception as exc:      # noqa: BLE001
            env["tesseract"] = {"available": False,
                                "reason": "%s: %s" % (type(exc).__name__, exc)}
            env["tesseract_available"] = False
        return env
    except Exception as exc:      # noqa: BLE001
        return {"capability": CAPABILITY, "available": False,
                "engine": "", "engine_mode": _ENGINE_MODE,
                "custom_engine": _ENGINE is not None,
                "reason": "%s: %s" % (type(exc).__name__, exc),
                "languages": [], "winrt": {}, "backend": "",
                "tesseract": {"available": False, "reason": str(exc)},
                "tesseract_available": False}


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """ocr 自检。**不假设 OCR 可用** —— 不可用时验证"诊断是否正确"。"""
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
    _add("OcrUnavailable 是 UnsupportedError 的子类",
         issubclass(_me.OcrUnavailable, UnsupportedError), "可沿链降级")
    _add("OcrWord 有坐标与文本",
         _me.OcrWord(text="x", rect=(1, 2, 3, 4)).text == "x", "有")
    _add("OcrWord.center 计算正确",
         _me.OcrWord(rect=(0, 0, 10, 20)).center == (5, 10), "(5,10)")
    _add("OcrHit 保留阶段二的字段",
         all(hasattr(_me.OcrHit(), f)
             for f in ("text", "window", "client_rect", "confidence",
                       "engine", "raw")),
         "兼容")

    # ============ 2) 探测不抛 ============
    _add("is_available 永不抛", _me.is_available() in (True, False), "bool")
    _add("unavailable_reason 永不抛",
         isinstance(_me.unavailable_reason(), str), "str")
    _add("explain_unavailable 永不抛",
         isinstance(_me.explain_unavailable(), str), "str")
    _add("languages 永不抛（不可用时返回空）",
         isinstance(_me.languages(), list), "list")
    _add("describe_environment 永不抛",
         isinstance(_me.describe_environment(), dict), "dict")

    avail = _me.is_available()
    _add("★ 可用性有明确结论（不静默）",
         avail or bool(_me.unavailable_reason()),
         "可用" if avail else "不可用，有 reason")

    if not avail:
        exp = _me.explain_unavailable()
        _add("★ 不可用时给出可操作诊断",
             len(exp) > 40 and "排查" in exp, exp.splitlines()[0][:40])
        _add("★ 诊断点名了具体环节（不是一句「不可用」）",
             any(k in exp for k in ("包标识", "语言包", "位图通路",
                                    "引擎能否创建")),
             "有具体方向")
        _add("★ 诊断建议了替代方案（set_engine）",
             "set_engine" in exp, "有出路")

    # ============ 3) 自定义引擎注入 ============
    saved = _me._ENGINE, _me._ENGINE_NAME, _me._ENGINE_MODE
    try:
        calls = []

        def _fake(window, region, min_conf, lang):
            calls.append((window, region, lang))
            return [
                _me.OcrHit(text="确定", window=window,
                           client_rect=(10, 20, 60, 40),
                           confidence=0.95, engine="fake"),
                _me.OcrHit(text="取消", window=window,
                           client_rect=(80, 20, 130, 40),
                           confidence=0.90, engine="fake"),
            ]

        _me.set_engine(_fake, "fake")
        _add("★ set_engine 后 is_available 为 True",
             _me.is_available() is True, "True")
        _add("engine_name 报告自定义引擎",
             _me.engine_name() == "fake", _me.engine_name())

        hits = _me.find_all_text("确定", window=0x1234)
        _add("★ find_all_text 用上了注入的引擎",
             len(hits) == 1 and hits[0].text == "确定", str(hits))
        _add("注入引擎收到了 window 参数",
             calls and calls[0][0] == 0x1234, str(calls[:1]))

        one = _me.find_text("取消", window=1)
        _add("find_text 返回最可信的一个",
             one is not None and one.text == "取消", str(one))

        _add("★ 匹配不上时返回 None（不返回最像的）",
             _me.find_text("不存在的文字", window=1) is None, "None")

        exact_hit = _me.find_text("确定", window=1, exact=True)
        _add("exact=True 走精确匹配",
             exact_hit is not None and exact_hit.text == "确定", "有")

        reg = _me.read_region(1)
        _add("read_region 返回全部", len(reg) == 2, "%d 条" % len(reg))

        # 模糊匹配
        def _fake2(window, region, min_conf, lang):
            return [_me.OcrHit(text="S0S", window=window,
                               client_rect=(0, 0, 10, 10),
                               confidence=0.9, engine="fake2")]

        _me.set_engine(_fake2, "fake2")
        _add("★ fuzzy=True 容忍 OCR 误认（S0S ↔ SOS）",
             _me.find_text("SOS", window=1, fuzzy=True) is not None,
             "匹配上了")
        _add("★ fuzzy=False 时严格匹配（不误伤）",
             _me.find_text("SOS", window=1, fuzzy=False) is None,
             "没匹配")

        # window 必填
        _add("★ 不传 window 抛 ValueError",
             _raise_type(lambda: _me.find_all_text("x", window=0))
             == "ValueError", "抛了")

        # 引擎抛异常不吞
        def _boom(window, region, min_conf, lang):
            raise RuntimeError("引擎炸了")

        _me.set_engine(_boom, "boom")
        _add("★ 引擎异常向外传播（不吞）",
             _raise_type(lambda: _me.find_all_text("x", window=1))
             == "RuntimeError", "传播")
    finally:
        _me._ENGINE, _me._ENGINE_NAME, _me._ENGINE_MODE = saved
        _me._PROBE["done"] = False

    # ============ 3b) 引擎模式路由（字符串选引擎）============
    # 给**真** tesseract 模块打临时补丁验证路由，不依赖本机装没装。
    try:
        OT = _me._tesseract_mod()
        _saved_t = (OT.is_available, OT.unavailable_reason, OT.version,
                    OT.languages, OT.describe_environment, OT.recognize)
        _fake_calls = []

        def _t_avail():
            return True

        def _t_reason():
            return "假的：没装"

        def _t_version():
            return "tesseract 0.0.0-fake"

        def _t_langs():
            return ["eng"]

        def _t_env():
            return {"available": True, "version": "tesseract 0.0.0-fake",
                    "exe": "fake.exe", "languages": ["eng"], "reason": ""}

        def _t_rec(bitmap, region=None, language="", timeout=10.0):
            _fake_calls.append({"region": region, "lang": language})
            return [_me.OcrWord(text="假词", rect=(1, 2, 3, 4),
                                confidence=0.5, engine="tesseract")]

        OT.is_available = _t_avail
        OT.unavailable_reason = _t_reason
        OT.version = _t_version
        OT.languages = _t_langs
        OT.describe_environment = _t_env
        OT.recognize = _t_rec
        try:
            _me.set_engine("tesseract")
            _add("★ set_engine('tesseract') 切模式",
                 _me.engine_mode() == "tesseract", _me.engine_mode())
            _add("★ tesseract 模式下 is_available 走旁路",
                 _me.is_available() is True, "True")
            _add("tesseract 模式 engine_name 是 tesseract",
                 _me.engine_name() == "tesseract", _me.engine_name())
            _add("tesseract 模式 languages 来自旁路",
                 _me.languages() == [{"tag": "eng", "display": "eng"}],
                 str(_me.languages()))

            _me.set_engine("windows")
            _add("★ set_engine('windows') 切回原生",
                 _me.engine_mode() == "windows", _me.engine_mode())

            _me.set_engine("auto")
            _add("★ set_engine('auto') 恢复默认",
                 _me.engine_mode() == "auto", _me.engine_mode())

            _add("★ 非法引擎名抛 ValueError",
                 _raise_type(lambda: _me.set_engine("不存在"))
                 == "ValueError", "抛了")

            # 路由真的走到了假 tesseract（位图级 recognize）
            class _Bmp2:
                width, height, stride = 2, 2, 8
                pixels = bytes(16)

            _me.set_engine("tesseract")
            got_words = _me.recognize(_Bmp2())
            _add("★ recognize 路由到 tesseract 旁路",
                 len(got_words) == 1 and got_words[0].text == "假词",
                 str([w.text for w in got_words]))
            _add("tesseract 旁路的词带 engine 标记",
                 bool(got_words) and got_words[0].engine == "tesseract",
                 "tesseract")
            _add("tesseract 旁路收到了语言参数",
                 _fake_calls and _fake_calls[0]["lang"] in ("chi_sim+eng", ""),
                 str(_fake_calls[:1]))
        finally:
            (OT.is_available, OT.unavailable_reason, OT.version,
             OT.languages, OT.describe_environment, OT.recognize) = _saved_t
            _me.set_engine(None)
    except Exception as exc:      # noqa: BLE001
        _add("引擎模式路由", False, "%s: %s" % (type(exc).__name__, exc))
        try:
            _me.set_engine(None)
        except Exception:      # noqa: BLE001
            pass

    # ============ 4) 像素转换 ============
    class _Bmp:
        def __init__(self, w, h):
            self.width, self.height = w, h
            self.stride = w * 4
            # DIB 自下而上：第 0 行是最下面
            self.pixels = bytes(
                [(y * 10 + x) % 256 for y in range(h)
                 for x in range(w * 4)])

    try:
        b = _Bmp(4, 3)
        top = _me._bgra_top_down(b)
        _add("★ BGRA 行序翻转（DIB 自下而上 → 自上而下）",
             len(top) == 4 * 3 * 4
             and top[0:4] == b.pixels[2 * 16:2 * 16 + 4],
             "首行 = 原图末行")
    except Exception as exc:      # noqa: BLE001
        _add("BGRA 翻转", False, str(exc))

    _add("★ 非法尺寸抛 OcrUnavailable",
         _raise_type(lambda: _me._bgra_top_down(_Bmp(0, 0)))
         == "OcrUnavailable", "抛了")

    # ============ 5) 不碰真实桌面 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top_mods = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top_mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top_mods.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import 桌面/抓屏（延迟 import）",
         not (top_mods & {"capture", "layout", "actions", "dispatcher"}),
         sorted(top_mods))
    _add("★ 不 import 任何 OCR 第三方库",
         not (top_mods & {"pytesseract", "paddleocr", "easyocr", "cv2"}),
         "零第三方")

    # ============ 6) 不硬解 ============
    # ⚠ 查**符号**，不查文本 —— 文本搜索会命中本文件里
    #   "不做缺口识别"这类说明文字（这个坑已经踩了五次）
    BAD = {"find_gap", "detect_gap", "solve_slider", "match_template",
           "gap_position", "locate_gap", "calc_offset"}
    names = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, _ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, _ast.Name):
            names.add(node.id)
    hit = names & BAD
    _add("★ 不硬解（无缺口识别/坐标推算符号）", not hit,
         "只识别文字，不解题" if not hit else str(sorted(hit)))

    elapsed = time.perf_counter() - t0
    return {
        "module": "ocr_fallback",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(explain_unavailable())
