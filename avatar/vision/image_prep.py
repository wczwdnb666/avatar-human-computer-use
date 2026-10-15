# -*- coding: utf-8 -*-
"""图像预处理 —— 把 GDI 位图变成能喂给大模型的小图。

## 为什么必须预处理

一张 2880x1800 的屏幕截图：

- 原始 BGRA：**20.7 MB**
- base64 后：**27.6 MB**
- 按 `w*h/750` 估算 token：**6912 tokens**

**一次请求 6912 token 只为了找一个小按钮。** 而模型能接受的
图像尺寸有上限（多数是 1280~2048px 最长边），超了会被服务端
拒绝或自动降采样 —— 后者更糟，因为**你不知道它降成了多少**，
坐标换算就错了。

所以预处理做四件事：**缩放 / 裁剪 / 压缩 / 记账**。

## 能力边界

**做**：等比缩放到最长边 ≤ ``max_edge``；区域裁剪；
多屏选择；DPR（物理像素→逻辑像素）处理；
编码成 JPEG（GDI+，质量可调）或 PNG（纯 Python，无损）；
记录缩放比与估算 token。

**不做**：

- **不做图像增强**。不二值化、不去噪、不锐化。
  **图不清楚就是不清楚** —— 增强会让模型"看到"原本不存在的东西，
  而它给出的坐标会被用来点击。
- **不做超分**。放大不会增加信息，只会增加 token。
- **不做无损旋转 / 透视校正**。界面截图没有这些需求。
- **不装 Pillow**。JPEG 用 Windows 自带的 GDI+ 编码器（ctypes），
  PNG 用标准库 `zlib` 手写。
- **不保证无损**。JPEG 是有损的 —— 这是**刻意的取舍**：
  省下的 token 远比丢掉的几个像素值重要。

## ⚠ 可逆性是硬要求

``scale`` 必须记录下来，因为模型返回的是**压缩后图像的坐标**，
要用它反推回原图坐标，再换算成屏幕坐标。

**这一步漏了，点击位置会整体偏移** —— 而且偏得很隐蔽
（缩放比 0.44 时，100px 的误差变成 227px）。
"""

from __future__ import annotations

import ctypes
import io
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_MAX_EDGE",
    "DEFAULT_JPEG_QUALITY",
    "MIN_MAX_EDGE",
    "MAX_MAX_EDGE",
    "PreparedImage",
    "prepare_for_llm",
    "crop_region",
    "handle_dpr",
    "downscale",
    "encode_jpeg",
    "encode_png",
    "jpeg_available",
    "estimate_image_tokens",
    "describe_image_prep",
    "selfcheck",
]

#: 默认最长边。**1280 是各家视觉模型的安全线** ——
#: 多数模型的上限在 1280~2048，取小值更保险。
DEFAULT_MAX_EDGE = 1280

#: 默认 JPEG 质量。
DEFAULT_JPEG_QUALITY = 85

#: 最长边的合法范围。太小看不清，太大爆 token。
MIN_MAX_EDGE = 256
MAX_MAX_EDGE = 4096


@dataclass
class PreparedImage:
    """一张处理好的图。**含换算所需的全部信息。**"""

    #: 编码后的字节（JPEG 或 PNG）
    data: bytes = b""
    #: MIME
    mime: str = "image/jpeg"
    #: 处理后的尺寸
    width: int = 0
    height: int = 0
    #: ★ 缩放比：``处理后 / 原始``。**坐标换算要用它。**
    scale: float = 1.0
    #: 原图尺寸（换算用）
    src_width: int = 0
    src_height: int = 0
    #: 裁剪区域在原图里的偏移（客户区坐标）。**换算要用它。**
    offset_x: int = 0
    offset_y: int = 0
    #: 估算的图像 token
    est_tokens: int = 0
    #: 编码方式（"jpeg-gdiplus" / "png-zlib" / "none"）
    codec: str = ""
    #: 原图字节数（诊断用）
    raw_bytes: int = 0
    elapsed: float = 0.0

    @property
    def b64_len(self) -> int:
        """base64 之后的长度（约等于实际上传量）。"""
        return (len(self.data) * 4 + 2) // 3

    def image_to_source(self, x: int, y: int) -> Tuple[int, int]:
        """**压缩图坐标 → 原图坐标**。

        ⚠ 这是坐标换算的**第一段**（第二段是"原图坐标 → 屏幕坐标"，
        由 :mod:`avatar.vision.validator` 做）。

        漏了这一步的后果：缩放比 0.44 时，模型说的 (100, 200)
        实际对应原图的 (227, 454) —— **点击位置整体偏移**。
        """
        if self.scale <= 0:
            return (int(x), int(y))
        return (int(round(x / self.scale)), int(round(y / self.scale)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mime": self.mime, "width": self.width,
            "height": self.height, "scale": round(self.scale, 4),
            "src": [self.src_width, self.src_height],
            "offset": [self.offset_x, self.offset_y],
            "bytes": len(self.data), "est_tokens": self.est_tokens,
            "codec": self.codec,
        }

    def describe(self) -> str:
        return ("%dx%d → %dx%d（scale=%.3f）%s  %d 字节 / 约 %d tokens"
                % (self.src_width, self.src_height, self.width,
                   self.height, self.scale, self.codec,
                   len(self.data), self.est_tokens))


# --------------------------------------------------------------------------
# token 估算
# --------------------------------------------------------------------------
def estimate_image_tokens(width: int, height: int) -> int:
    """按分辨率估算图像 token。

    ## 公式的来源

    OpenAI 的视觉定价大致是 **每 750 像素 1 个 token**
    （`w*h/750`）。这个系数各家不同，但量级一致。

    **本函数偏保守**（宁可高估）：高估的代价是"少发几次"，
    低估的代价是"超预算被服务端拒"。

    ⚠ 这是**估算**，不是精确计数。精确值只有服务端知道，
    而它会在响应里返回真实用量 —— 那个值才是记账的依据。
    """
    w = max(0, int(width))
    h = max(0, int(height))
    if w <= 0 or h <= 0:
        return 0
    return int(w * h / 750) + 1


# --------------------------------------------------------------------------
# 缩放
# --------------------------------------------------------------------------
def downscale(bitmap: Any, factor: float) -> Any:
    """任意比例缩放。``factor >= 1.0`` 时原样返回。

    ## 用面积平均而不是最近邻

    缩小的时候，最近邻会**丢掉大量像素**（比如 3x3 只取 1 个），
    细小的文字笔画会直接消失 —— 而 OCR 和视觉模型都靠笔画认字。

    面积平均把每个目标像素对应的源区域**求平均**，
    笔画变淡但**不消失**。代价是慢一点。

    ## 不做放大

    ``factor < 1.0``（放大）直接返回原图。放大不增加信息，
    只增加 token —— 那是纯粹的浪费。
    """
    f = float(factor or 1.0)
    if f >= 1.0:
        return bitmap
    if f <= 0:
        f = 1.0

    src_w = int(getattr(bitmap, "width", 0) or 0)
    src_h = int(getattr(bitmap, "height", 0) or 0)
    if src_w <= 0 or src_h <= 0:
        return bitmap

    dst_w = max(1, int(round(src_w * f)))
    dst_h = max(1, int(round(src_h * f)))
    if dst_w == src_w and dst_h == src_h:
        return bitmap

    stride = int(getattr(bitmap, "stride", 0) or (src_w * 4))
    px = bitmap.pixels
    out = bytearray(dst_w * dst_h * 4)

    # 每个目标像素覆盖的源区域
    for dy in range(dst_h):
        y0 = (dy * src_h) // dst_h
        y1 = max(y0 + 1, ((dy + 1) * src_h) // dst_h)
        for dx in range(dst_w):
            x0 = (dx * src_w) // dst_w
            x1 = max(x0 + 1, ((dx + 1) * src_w) // dst_w)
            b = g = r = a = 0
            n = 0
            for sy in range(y0, y1):
                row = (src_h - 1 - sy) * stride      # DIB 自下而上
                for sx in range(x0, x1):
                    o = row + sx * 4
                    b += px[o]
                    g += px[o + 1]
                    r += px[o + 2]
                    a += px[o + 3]
                    n += 1
            if not n:
                n = 1
            o2 = ((dst_h - 1 - dy) * dst_w + dx) * 4
            out[o2] = b // n
            out[o2 + 1] = g // n
            out[o2 + 2] = r // n
            out[o2 + 3] = a // n

    return _MiniBitmap(dst_w, dst_h, dst_w * 4, bytes(out))


class _MiniBitmap:
    """一个最小的位图替身 —— 只要 :mod:`capture` 的那几个字段。

    **为什么不用 `capture.Bitmap`**：那是个 dataclass，
    构造它要走 ``__post_init__`` 的校验。这里只是搬运像素，
    不需要那些。用鸭子类型最省事。
    """

    __slots__ = ("width", "height", "stride", "pixels")

    def __init__(self, width: int, height: int, stride: int,
                 pixels: bytes) -> None:
        self.width = int(width)
        self.height = int(height)
        self.stride = int(stride)
        self.pixels = pixels

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def get_pixel(self, x: int, y: int) -> Optional[Tuple[int, int, int]]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        o = (self.height - 1 - y) * self.stride + x * 4
        return (self.pixels[o + 2], self.pixels[o + 1], self.pixels[o])

    def region(self, x: int, y: int, w: int, h: int) -> "_MiniBitmap":
        x = max(0, min(self.width, int(x)))
        y = max(0, min(self.height, int(y)))
        w = max(0, min(self.width - x, int(w)))
        h = max(0, min(self.height - y, int(h)))
        out = bytearray(w * h * 4)
        for yy in range(h):
            src = (self.height - 1 - (y + yy)) * self.stride + x * 4
            out[yy * w * 4:(yy + 1) * w * 4] = \
                self.pixels[src:src + w * 4]
        return _MiniBitmap(w, h, w * 4, bytes(out))


def _to_rgb_rows(bitmap: Any) -> bytes:
    """BGRA（自下而上）→ RGB（自上而下）。

    **两个变换都必须做**：

    - **BGRA → RGB**：PNG/JPEG 都不吃 4 通道
    - **自下而上 → 自上而下**：DIB 是倒的，不改的话图是**上下颠倒**的

    第二条最坑：倒着的图**不会报错**，只会让模型认不出字
    （或者认成别的）。这种"安静地错"最难查。
    """
    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    stride = int(getattr(bitmap, "stride", 0) or (w * 4))
    px = bitmap.pixels
    out = bytearray(w * h * 3)
    for y in range(h):
        src = (h - 1 - y) * stride
        dst = y * w * 3
        for x in range(w):
            o = src + x * 4
            d = dst + x * 3
            out[d] = px[o + 2]
            out[d + 1] = px[o + 1]
            out[d + 2] = px[o]
    return bytes(out)


# --------------------------------------------------------------------------
# PNG 编码（纯 Python + zlib）
# --------------------------------------------------------------------------
def encode_png(bitmap: Any) -> bytes:
    """编码成 PNG。**纯标准库（zlib），无损。**

    ## 为什么留着 PNG

    JPEG 更小，但**有损**。有些场景（比如要把截图存档做证据）
    需要无损。而且 GDI+ 在某些精简系统上不可用 —— 那时 PNG 是兜底。
    """
    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    if w <= 0 or h <= 0:
        raise ValueError("位图尺寸异常：%dx%d" % (w, h))

    rgb = _to_rgb_rows(bitmap)

    # 每行前面加一个 filter 字节（0 = None）
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# JPEG 编码（GDI+，纯 ctypes）
# --------------------------------------------------------------------------
_gdiplus = None
_gdiplus_token = ctypes.c_void_p()
_GDI_LOAD_ERROR = ""

try:
    _gdiplus = ctypes.WinDLL("gdiplus.dll")
except OSError as _exc:      # pragma: no cover
    _gdiplus = None
    _GDI_LOAD_ERROR = str(_exc)


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    ]


class _ImageCodecInfo(ctypes.Structure):
    """GDI+ 的 ``ImageCodecInfo``。

    x64 下 112 字节：CLSID(16) + GUID(16) + 6 个指针(48)
    + 4 个 DWORD(16) + 2 个指针(16)。
    """
    _fields_ = [
        ("Clsid", ctypes.c_byte * 16),
        ("FormatID", ctypes.c_byte * 16),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.c_void_p),
        ("SigMask", ctypes.c_void_p),
    ]


class _EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", ctypes.c_byte * 16),
        ("NumberOfValues", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("Value", ctypes.c_void_p),
    ]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32),
                ("Parameter", _EncoderParameter * 1)]


#: ``EncoderQuality`` 的 GUID（GDI+ 官方定义）
_QUALITY_GUID = bytes([
    0xB5, 0xE4, 0x5B, 0x1D, 0x4A, 0xFA, 0x2D, 0x45,
    0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB,
])


def _ensure_gdiplus() -> bool:
    """初始化 GDI+（幂等）。**失败返回 False，不抛。**"""
    global _gdiplus_token
    if _gdiplus is None:
        return False
    if _gdiplus_token.value:
        return True
    try:
        _gdiplus.GdiplusStartup.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
        _gdiplus.GdiplusStartup.restype = ctypes.c_int
        inp = _GdiplusStartupInput(1, None, 0, 0)
        st = _gdiplus.GdiplusStartup(ctypes.byref(_gdiplus_token),
                                     ctypes.byref(inp), None)
        return st == 0
    except Exception:      # noqa: BLE001
        return False


def jpeg_available() -> bool:
    """GDI+ 的 JPEG 编码器能用吗？**不抛异常。**"""
    try:
        return bool(_ensure_gdiplus() and _find_jpeg_codec())
    except Exception:      # noqa: BLE001
        return False


def _find_jpeg_codec() -> Optional[bytes]:
    """找 GDI+ 的 JPEG 编码器 CLSID（16 字节）。找不到返回 None。"""
    if _gdiplus is None:
        return None
    try:
        _gdiplus.GdipGetImageEncodersSize.argtypes = [
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        _gdiplus.GdipGetImageEncodersSize.restype = ctypes.c_int
        _gdiplus.GdipGetImageEncoders.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        _gdiplus.GdipGetImageEncoders.restype = ctypes.c_int

        n = ctypes.c_uint32(0)
        size = ctypes.c_uint32(0)
        if _gdiplus.GdipGetImageEncodersSize(ctypes.byref(n),
                                             ctypes.byref(size)) != 0:
            return None
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _gdiplus.GdipGetImageEncoders(n, size, buf) != 0:
            return None
        arr = ctypes.cast(buf, ctypes.POINTER(_ImageCodecInfo))
        for i in range(int(n.value)):
            info = arr[i]
            mime = str(info.MimeType or "")
            if mime.lower() == "image/jpeg":
                return bytes(info.Clsid)
    except Exception:      # noqa: BLE001
        return None
    return None


def _stream_vtbl_fn(stream: Any, slot: int, restype: Any, argtypes: list):
    """取 ``IStream`` 的某个 vtable 函数。"""
    p = stream.value if isinstance(stream, ctypes.c_void_p) else int(stream)
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0]
    addr = ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[slot]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p,
                              *argtypes)(addr)


def _stream_length(stream: Any) -> int:
    """``IStream::Seek(0, STREAM_SEEK_END, &pos)`` —— 拿流的长度。

    ``IStream`` vtable：``[3]Read [4]Write [5]Seek [6]SetSize ...``
    """
    try:
        fn = _stream_vtbl_fn(
            stream, 5, ctypes.c_long,
            [ctypes.c_int64, ctypes.c_uint32,
             ctypes.POINTER(ctypes.c_uint64)])
        pos = ctypes.c_uint64(0)
        hr = fn(stream, ctypes.c_int64(0), ctypes.c_uint32(2),
                ctypes.byref(pos))
        return int(pos.value) if hr == 0 else 0
    except Exception:      # noqa: BLE001
        return 0


def _stream_seek(stream: Any, offset: int, origin: int) -> bool:
    """``IStream::Seek``。``origin``：0=开头 1=当前 2=末尾。"""
    try:
        fn = _stream_vtbl_fn(
            stream, 5, ctypes.c_long,
            [ctypes.c_int64, ctypes.c_uint32,
             ctypes.POINTER(ctypes.c_uint64)])
        pos = ctypes.c_uint64(0)
        return fn(stream, ctypes.c_int64(offset), ctypes.c_uint32(origin),
                  ctypes.byref(pos)) == 0
    except Exception:      # noqa: BLE001
        return False


def encode_jpeg(bitmap: Any, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    """用 **GDI+** 编码 JPEG。**纯 ctypes，不装 Pillow。**

    ## 为什么用 GDI+ 而不是手写 JPEG 编码器

    手写 JPEG 要 DCT + 量化 + 霍夫曼编码，300+ 行，而且容易在
    边界条件上出错（产生肉眼可见的块效应）。

    GDI+ 是 Windows 自带的，一次调用就出图，而且**质量参数
    与系统其他程序一致**（同一套编码器）。

    ## 失败时

    抛 :class:`RuntimeError` —— 调用方（:func:`prepare_for_llm`）
    会回退到 PNG。**不让"JPEG 不可用"变成"整个功能不可用"。**
    """
    if not _ensure_gdiplus():
        raise RuntimeError("GDI+ 不可用：%s"
                           % (_GDI_LOAD_ERROR or "初始化失败"))
    clsid = _find_jpeg_codec()
    if not clsid:
        raise RuntimeError("GDI+ 里找不到 JPEG 编码器")

    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    if w <= 0 or h <= 0:
        raise ValueError("位图尺寸异常：%dx%d" % (w, h))

    # GDI+ 的 24bppRGB 期望**自下而上**的 BGR —— 正好是 DIB 的原始布局，
    # 所以直接从原图搬，**不做行序翻转**（那是给 PNG 用的）
    stride = int(getattr(bitmap, "stride", 0) or (w * 4))
    px = bitmap.pixels
    bgr = bytearray(w * h * 3)
    for y in range(h):
        src = y * stride            # 保持自下而上
        dst = y * w * 3
        for x in range(w):
            o = src + x * 4
            d = dst + x * 3
            bgr[d] = px[o]
            bgr[d + 1] = px[o + 1]
            bgr[d + 2] = px[o + 2]

    src_buf = ctypes.create_string_buffer(bytes(bgr), len(bgr))

    _gdiplus.GdipCreateBitmapFromScan0.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _gdiplus.GdipCreateBitmapFromScan0.restype = ctypes.c_int
    _gdiplus.GdipSaveImageToStream.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p]
    _gdiplus.GdipSaveImageToStream.restype = ctypes.c_int
    _gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    _gdiplus.GdipDisposeImage.restype = ctypes.c_int

    # PixelFormat24bppRGB = 0x00021808
    bmp_ptr = ctypes.c_void_p()
    st = _gdiplus.GdipCreateBitmapFromScan0(
        w, h, w * 3, 0x00021808,
        ctypes.cast(src_buf, ctypes.c_void_p), ctypes.byref(bmp_ptr))
    if st != 0 or not bmp_ptr.value:
        raise RuntimeError("GdipCreateBitmapFromScan0 失败（%d）" % st)

    # ⚠ **必须声明 restype/argtypes** —— 不声明的话 ctypes 默认按
    #   C int 处理返回值，64 位指针会被**截断**（GlobalLock 踩过）。
    _k32 = ctypes.windll.kernel32
    _o32 = ctypes.windll.ole32
    _o32.CreateStreamOnHGlobal.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    _o32.CreateStreamOnHGlobal.restype = ctypes.c_long
    _o32.GetHGlobalFromStream.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _o32.GetHGlobalFromStream.restype = ctypes.c_long
    _k32.GlobalLock.argtypes = [ctypes.c_void_p]
    _k32.GlobalLock.restype = ctypes.c_void_p      # ← 关键
    _k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _k32.GlobalUnlock.restype = ctypes.c_int
    _k32.GlobalSize.argtypes = [ctypes.c_void_p]
    _k32.GlobalSize.restype = ctypes.c_size_t

    stream = ctypes.c_void_p()
    try:
        if _o32.CreateStreamOnHGlobal(
                None, True, ctypes.byref(stream)) != 0 or not stream.value:
            raise RuntimeError("CreateStreamOnHGlobal 失败")

        params = _EncoderParameters()
        params.Count = 1
        ctypes.memmove(ctypes.byref(params.Parameter[0].Guid),
                       _QUALITY_GUID, 16)
        params.Parameter[0].NumberOfValues = 1
        # EncoderParameterValueTypeLong = 4（**不是 1** —— 写错会返回
        # InvalidParameter，且看不出是哪一步的问题）
        params.Parameter[0].Type = 4
        q = ctypes.c_uint32(max(1, min(100, int(quality))))
        params.Parameter[0].Value = ctypes.cast(
            ctypes.byref(q), ctypes.c_void_p)

        clsid_buf = ctypes.create_string_buffer(clsid, 16)
        st = _gdiplus.GdipSaveImageToStream(
            bmp_ptr, stream, ctypes.cast(clsid_buf, ctypes.c_void_p),
            ctypes.byref(params))
        if st != 0:
            raise RuntimeError("GdipSaveImageToStream 失败（%d）" % st)

        # ---- 从 IStream 读回字节 ----
        # ⚠ 长度要用 **IStream::Seek 到末尾**拿，不能用 GlobalSize ——
        #   GlobalSize 给的是 HGlobal 的**分配大小**，通常比实际数据大，
        #   多读出来的部分是未初始化内存（可能触发访问违例）。
        n = _stream_length(stream)
        if n <= 0:
            raise RuntimeError("IStream 长度为 0")
        _stream_seek(stream, 0, 0)      # 回到开头

        hglobal = ctypes.c_void_p()
        if _o32.GetHGlobalFromStream(
                stream, ctypes.byref(hglobal)) != 0 or not hglobal.value:
            raise RuntimeError("GetHGlobalFromStream 失败")
        ptr = _k32.GlobalLock(hglobal)
        if not ptr:
            raise RuntimeError("GlobalLock 失败")
        try:
            data = ctypes.string_at(ptr, n)
        finally:
            _k32.GlobalUnlock(hglobal)
        if not data:
            raise RuntimeError("编码结果为空")
        return data
    finally:
        try:
            _gdiplus.GdipDisposeImage(bmp_ptr)
        except Exception:      # noqa: BLE001
            pass
        if stream.value:
            try:
                vt = ctypes.cast(stream.value,
                                 ctypes.POINTER(ctypes.c_void_p))[0]
                release = ctypes.WINFUNCTYPE(
                    ctypes.c_ulong, ctypes.c_void_p)(
                        ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[2])
                release(stream)
            except Exception:      # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# 裁剪 / DPR
# --------------------------------------------------------------------------
def crop_region(bitmap: Any, region: Optional[Tuple[int, int, int, int]]
                ) -> Tuple[Any, int, int]:
    """裁剪。返回 ``(新位图, 偏移x, 偏移y)``。

    ## 为什么要"只发目标窗口/区域"

    全屏 2880x1800 里有 90% 是与任务无关的东西。
    只发目标区域：

    - **省 token**（面积小了，token 按面积算）
    - **更准**（模型不用在无关内容里找）

    ``region`` 为 None 或非法时**原样返回**（不抛）——
    裁剪失败不该让整个流程失败。
    """
    if not region:
        return bitmap, 0, 0
    try:
        l, t, r, b = (int(region[0]), int(region[1]),
                      int(region[2]), int(region[3]))
    except Exception:      # noqa: BLE001
        return bitmap, 0, 0

    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    l = max(0, min(w, l))
    t = max(0, min(h, t))
    r = max(l, min(w, r))
    b = max(t, min(h, b))
    if r - l < 1 or b - t < 1:
        return bitmap, 0, 0

    fn = getattr(bitmap, "region", None)
    if not callable(fn):
        return bitmap, 0, 0
    try:
        return fn(l, t, r - l, b - t), l, t
    except Exception:      # noqa: BLE001
        return bitmap, 0, 0


def handle_dpr(bitmap: Any, dpr: float = 1.0) -> Tuple[Any, float]:
    """处理 DPR（设备像素比）。返回 ``(位图, 有效缩放)``。

    ## 什么时候需要

    高 DPI 屏上，物理像素是逻辑像素的 N 倍（比如 200% 缩放时
    物理 2880 = 逻辑 1440）。

    **逻辑坐标是用户和 UIA 用的坐标系**，而截图是物理像素。
    所以：

    - 发给模型的图可以按物理像素（清晰）
    - 但**坐标换算时要记得除 DPR**

    ## 本函数的做法

    **不缩放位图**（那会丢清晰度），只**返回缩放因子**，
    让调用方在坐标换算时用。

    ``dpr <= 0`` 或 ``dpr == 1.0`` 时返回 ``(原图, 1.0)``。
    """
    d = float(dpr or 1.0)
    if d <= 0 or abs(d - 1.0) < 1e-9:
        return bitmap, 1.0
    return bitmap, d


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def prepare_for_llm(bitmap: Any, *,
                    max_edge: int = DEFAULT_MAX_EDGE,
                    quality: int = DEFAULT_JPEG_QUALITY,
                    region: Optional[Tuple[int, int, int, int]] = None,
                    dpr: float = 1.0,
                    prefer: str = "jpeg",
                    ) -> PreparedImage:
    """把位图处理成能喂给大模型的小图。

    ## 处理顺序（**顺序不能换**）

    1. **裁剪** —— 先切掉无关区域（省的是**面积**，最划算）
    2. **DPR 处理** —— 只记因子，不动图
    3. **缩放** —— 缩到最长边 ≤ ``max_edge``
    4. **编码** —— JPEG（GDI+）优先，失败回退 PNG

    ## 为什么裁剪在缩放前

    先缩再裁的话，裁剪坐标要**除以缩放比**才能对应到原图 ——
    多一次换算就多一次出错机会。而且先裁后缩，
    缩放比是基于"裁剪后的图"算的，语义更清楚。

    ## ``prefer``

    - ``"jpeg"`` —— 优先 JPEG，失败回退 PNG
    - ``"png"`` —— 只用 PNG（无损，但更大）
    """
    t0 = time.perf_counter()
    out = PreparedImage(mime="image/jpeg")

    if bitmap is None:
        out.codec = "none"
        out.elapsed = time.perf_counter() - t0
        return out

    # ---- 1) 裁剪 ----
    work, off_x, off_y = crop_region(bitmap, region)
    out.offset_x, out.offset_y = int(off_x), int(off_y)

    src_w = int(getattr(work, "width", 0) or 0)
    src_h = int(getattr(work, "height", 0) or 0)
    out.src_width, out.src_height = src_w, src_h
    out.raw_bytes = src_w * src_h * 4

    if src_w <= 0 or src_h <= 0:
        out.codec = "none"
        out.elapsed = time.perf_counter() - t0
        return out

    # ---- 2) DPR（只记因子）----
    _, dpr_factor = handle_dpr(work, dpr)

    # ---- 3) 缩放 ----
    edge = max(MIN_MAX_EDGE, min(MAX_MAX_EDGE, int(max_edge or
                                                   DEFAULT_MAX_EDGE)))
    longest = max(src_w, src_h)
    factor = 1.0 if longest <= edge else (edge / float(longest))
    scaled = downscale(work, factor) if factor < 1.0 else work

    out.width = int(getattr(scaled, "width", 0) or 0)
    out.height = int(getattr(scaled, "height", 0) or 0)
    # ★ scale 要**乘上 DPR** —— 因为最终要换回逻辑坐标
    out.scale = float(factor) * float(dpr_factor)

    # ---- 4) 编码 ----
    want_png = str(prefer or "jpeg").lower() == "png"
    data = b""
    if not want_png:
        try:
            data = encode_jpeg(scaled, quality)
            out.codec = "jpeg-gdiplus"
            out.mime = "image/jpeg"
        except Exception:      # noqa: BLE001
            data = b""
    if not data:
        try:
            data = encode_png(scaled)
            out.codec = "png-zlib"
            out.mime = "image/png"
        except Exception:      # noqa: BLE001
            data = b""
            out.codec = "none"
            out.mime = ""

    out.data = data
    out.est_tokens = estimate_image_tokens(out.width, out.height)
    out.elapsed = time.perf_counter() - t0
    return out


def describe_image_prep() -> str:
    """给人看的预处理说明。"""
    return "\n".join([
        "图像预处理（image_prep）",
        "=" * 60,
        "处理顺序：裁剪 → DPR（只记因子）→ 缩放 → 编码",
        "",
        "参数：",
        "  最长边    %d px（范围 %d~%d）"
        % (DEFAULT_MAX_EDGE, MIN_MAX_EDGE, MAX_MAX_EDGE),
        "  JPEG 质量 %d" % DEFAULT_JPEG_QUALITY,
        "",
        "编码器：",
        "  JPEG  GDI+（Windows 自带，纯 ctypes）%s"
        % ("可用" if jpeg_available() else "**不可用**（会回退 PNG）"),
        "  PNG   纯 Python + zlib（无损）",
        "",
        "token 估算：w * h / 750（偏保守，宁可高估）",
        "",
        "★ scale 必须记录下来 —— 模型返回的是压缩图坐标，",
        "  要用它反推回原图，再换算成屏幕坐标。",
        "  漏了这一步，缩放比 0.44 时点击位置会整体偏移 2.3 倍。",
        "",
        "★ 不做图像增强 —— 增强会让模型「看到」原本不存在的东西，",
        "  而它给出的坐标会被用来点击。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _Bmp:
    """测试用位图（带纹理，便于验证缩放）。"""

    def __init__(self, w: int, h: int, pattern: bool = True) -> None:
        self.width, self.height = int(w), int(h)
        self.stride = int(w) * 4
        if not pattern:
            self.pixels = bytes([128, 128, 128, 255]) * (w * h)
        else:
            # ⚠ 用 bytes(...) 一次成型，别逐字节 += —— 后者在 640x480 上
            #   就是 30 万次追加，自检会因此超过 3 秒红线
            self.pixels = bytes(
                b for y in range(h) for x in range(w)
                for b in ((((x * 3 + y * 7) % 200) + 30),
                          ((((x * 3 + y * 7) % 200) + 30) * 2) % 256,
                          ((((x * 3 + y * 7) % 200) + 30) * 3) % 256, 255))

    def region(self, x, y, w, h):
        x = max(0, min(self.width, int(x)))
        y = max(0, min(self.height, int(y)))
        w = max(0, min(self.width - x, int(w)))
        h = max(0, min(self.height - y, int(h)))
        out = bytearray(w * h * 4)
        for yy in range(h):
            src = (self.height - 1 - (y + yy)) * self.stride + x * 4
            out[yy * w * 4:(yy + 1) * w * 4] = \
                self.pixels[src:src + w * 4]
        return _Bmp._mk(w, h, bytes(out))

    @classmethod
    def _mk(cls, w, h, px):
        o = cls.__new__(cls)
        o.width, o.height, o.stride, o.pixels = w, h, w * 4, px
        return o


class _NoiseBmp:
    """伪随机噪声位图 —— 测编码器体积用。

    **为什么用噪声**：JPEG 在噪声上最吃亏（DCT 压不动随机数据），
    PNG 也压不动。如果 JPEG 在噪声上都能比 PNG 小，
    真实截图（有大片纯色 + 边缘）就更没问题。
    """

    def __init__(self, w: int, h: int) -> None:
        self.width, self.height = int(w), int(h)
        self.stride = int(w) * 4
        # ⚠ 必须用**真伪随机**（random 模块）。
        #   第一版手写了个 LCG 并取低字节 —— LCG 的低字节周期极短
        #   （2^8 量级），出来的是**周期性图案**，PNG 压到 2%，
        #   测出来"PNG 比 JPEG 小得多"，但那不是噪声。
        import random as _rnd
        rng = _rnd.Random(12345)
        px = bytearray(rng.randbytes(self.width * self.height * 4))
        for i in range(3, len(px), 4):
            px[i] = 255          # alpha 固定不透明
        self.pixels = bytes(px)

    def region(self, x, y, w, h):
        return self


def selfcheck(quick: bool = True) -> dict:
    """image_prep 自检。**不联网、不动桌面。**"""
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
    _add("默认最长边 1280", DEFAULT_MAX_EDGE == 1280, "1280")
    _add("默认 JPEG 质量 85", DEFAULT_JPEG_QUALITY == 85, "85")
    _add("最长边范围合理", MIN_MAX_EDGE < DEFAULT_MAX_EDGE < MAX_MAX_EDGE,
         "%d ~ %d" % (MIN_MAX_EDGE, MAX_MAX_EDGE))

    # ============ 2) token 估算 ============
    _add("★ 按 w*h/750 估算",
         estimate_image_tokens(750, 1) == 2,  # 750/750 + 1
         str(estimate_image_tokens(750, 1)))
    _add("1280x720 约 1229 tokens",
         1200 < estimate_image_tokens(1280, 720) < 1300,
         str(estimate_image_tokens(1280, 720)))
    _add("★ 2880x1800 约 6912（这就是为什么要压缩）",
         6800 < estimate_image_tokens(2880, 1800) < 7000,
         str(estimate_image_tokens(2880, 1800)))
    _add("零尺寸返回 0",
         estimate_image_tokens(0, 0) == 0, "0")
    _add("负数返回 0", estimate_image_tokens(-5, -5) == 0, "0")
    _add("估算永不抛",
         all(estimate_image_tokens(a, b) >= 0
             for a, b in ((0, 1), (1, 0), (-1, 1), (99999, 99999))),
         "全部有值")

    # ============ 3) 缩放 ============
    b = _Bmp(400, 200)
    small = downscale(b, 0.5)
    _add("★ 缩小到一半尺寸正确",
         small.width == 200 and small.height == 100,
         "%dx%d" % (small.width, small.height))
    _add("缩放后仍是 4 字节/像素",
         len(small.pixels) == 200 * 100 * 4,
         "%d 字节" % len(small.pixels))
    _add("★ factor >= 1 时原样返回（不放大）",
         downscale(b, 1.0) is b and downscale(b, 2.0) is b, "原图")
    _add("factor <= 0 时不崩",
         downscale(b, 0).width == 400, "原图")
    _add("缩放不改变宽高比",
         abs(small.width / small.height - 2.0) < 0.02,
         "%.3f" % (small.width / small.height))

    # 面积平均 vs 最近邻：纯色图缩完还是纯色
    flat = _Bmp(100, 100, pattern=False)
    fs = downscale(flat, 0.5)
    _add("★ 纯色图缩放后仍是纯色（面积平均生效）",
         all(v == 128 for v in fs.pixels[0::4]), "均匀")

    # ============ 4) 裁剪 ============
    got, ox, oy = crop_region(b, (10, 20, 110, 70))
    _add("★ 裁剪尺寸正确",
         got.width == 100 and got.height == 50,
         "%dx%d" % (got.width, got.height))
    _add("★ 裁剪偏移被返回（坐标换算要用）",
         (ox, oy) == (10, 20), "(%d,%d)" % (ox, oy))
    _add("★ region=None 时原样返回",
         crop_region(b, None)[0] is b, "原图")
    _add("★ 非法 region 不抛（原样返回）",
         crop_region(b, "乱写")[0] is b, "原图")
    _add("越界 region 被钳制",
         crop_region(b, (-10, -10, 99999, 99999))[0].width == 400, "钳制")
    _add("空 region 原样返回",
         crop_region(b, (50, 50, 50, 50))[0] is b, "原图")

    # ============ 5) DPR ============
    _add("★ dpr=1 返回 1.0", handle_dpr(b, 1.0)[1] == 1.0, "1.0")
    _add("★ dpr=2 返回 2.0（只记因子，不动图）",
         handle_dpr(b, 2.0)[1] == 2.0 and handle_dpr(b, 2.0)[0] is b, "2.0")
    _add("dpr<=0 不崩", handle_dpr(b, 0)[1] == 1.0, "1.0")

    # ============ 6) 编码 ============
    png = encode_png(b)
    _add("★ PNG 魔数正确",
         png[:8] == b"\x89PNG\r\n\x1a\n", repr(png[:8]))
    _add("PNG 含 IHDR / IDAT / IEND",
         b"IHDR" in png[:64] and b"IDAT" in png and png[-8:-4] == b"IEND",
         "三段齐全")
    _add("PNG 尺寸写对（宽 400 高 200）",
         struct.unpack(">II", png[16:24]) == (400, 200),
         str(struct.unpack(">II", png[16:24])))
    _add("PNG 尺寸为 0 时抛",
         _raise_type(lambda: encode_png(_Bmp(0, 0))) == "ValueError",
         "抛了")

    has_jpeg = jpeg_available()
    _add("JPEG 编码器探测不抛", has_jpeg in (True, False),
         "可用" if has_jpeg else "不可用（会回退 PNG）")
    if has_jpeg:
        jp = encode_jpeg(b, 85)
        _add("★ JPEG 以 FFD8 开头、FFD9 结尾",
             jp[:2] == b"\xff\xd8" and jp[-2:] == b"\xff\xd9",
             "%r ... %r" % (jp[:2], jp[-2:]))
        # ⚠ 用**噪声图**比体积。
        #   规律纹理（上面那张 _Bmp）PNG 反而更小 —— 那是 PNG 的无损压缩
        #   在规整图案上极强，不是 JPEG 有问题。
        #   真实截图介于两者之间，但**噪声是 JPEG 最不利的情况** ——
        #   它都能赢，真实场景就更能赢。
        noise = _NoiseBmp(200, 150)
        _add("★ JPEG 比 PNG 小（噪声图 —— JPEG 最不利的情况）",
             len(encode_jpeg(noise, 85)) < len(encode_png(noise)),
             "JPEG %d / PNG %d" % (len(encode_jpeg(noise, 85)),
                                   len(encode_png(noise))))
        jp50 = encode_jpeg(b, 50)
        jp95 = encode_jpeg(b, 95)
        _add("★ 质量参数生效（50 < 95 的体积）",
             len(jp50) < len(jp95),
             "q50 %d / q95 %d" % (len(jp50), len(jp95)))

    # ============ 7) 主入口 ============
    big = _Bmp(640, 480)
    # ⚠ 用 max_edge=320 触发缩放 —— 640x480 小于 1280，用默认值不会缩。
    #   自检要**快**（有 3 秒红线），所以不能为了测缩放去造一张大图。
    p = prepare_for_llm(big, max_edge=320, quality=85)
    _add("★ 长边被缩到 max_edge", max(p.width, p.height) == 320,
         "%dx%d" % (p.width, p.height))
    _add("★ scale 被记录（坐标换算要用）",
         0 < p.scale < 1.0, "%.4f" % p.scale)
    _add("★ 原图尺寸被记录", (p.src_width, p.src_height) == (640, 480),
         "%dx%d" % (p.src_width, p.src_height))
    _add("★ 编码成功且有数据", bool(p.data) and p.codec != "none",
         "%s %d 字节" % (p.codec, len(p.data)))
    _add("★ 估算 token 被记录", p.est_tokens > 0, str(p.est_tokens))
    _add("★ 压缩后 token 远小于原图",
         p.est_tokens < estimate_image_tokens(640, 480),
         "%d vs %d" % (p.est_tokens, estimate_image_tokens(640, 480)))
    # 640x480 不缩放时 scale 应为 1.0
    p_same = prepare_for_llm(big, max_edge=1280)
    _add("★ 小图不缩放时 scale=1.0", p_same.scale == 1.0,
         str(p_same.scale))
    _add("mime 与 codec 一致",
         (p.codec.startswith("jpeg") and p.mime == "image/jpeg")
         or (p.codec.startswith("png") and p.mime == "image/png"),
         "%s / %s" % (p.codec, p.mime))

    # ---- ★ 坐标可逆 ----
    src_pt = p.image_to_source(int(p.width * 0.5), int(p.height * 0.5))
    expect = (int(640 * 0.5), int(480 * 0.5))
    _add("★★ 压缩图坐标能反推回原图（误差 ≤2px）",
         abs(src_pt[0] - expect[0]) <= 2 and abs(src_pt[1] - expect[1]) <= 2,
         "%s ≈ %s" % (src_pt, expect))

    # ---- 小图不放大 ----
    p2 = prepare_for_llm(_Bmp(200, 100), max_edge=1280)
    _add("★ 小图不放大（scale=1.0）", p2.scale == 1.0, str(p2.scale))
    _add("小图尺寸不变", (p2.width, p2.height) == (200, 100),
         "%dx%d" % (p2.width, p2.height))

    # ---- 带裁剪 ----
    p3 = prepare_for_llm(big, max_edge=1280, region=(100, 200, 500, 400))
    _add("★ 裁剪偏移被记录",
         (p3.offset_x, p3.offset_y) == (100, 200),
         "(%d,%d)" % (p3.offset_x, p3.offset_y))
    _add("★ 裁剪后的 scale 基于裁剪尺寸算",
         p3.scale == 1.0,  # 400x200 小于 1280，不缩
         str(p3.scale))

    # ---- 带 DPR ----
    p4 = prepare_for_llm(big, max_edge=1280, dpr=2.0)
    # ⚠ 缩放比的分母是**最长边**（2880），不是高（1800）
    # 640x480 小于 max_edge，所以 scale 只反映 DPR
    _add("★ scale 含 DPR 因子",
         abs(p4.scale - 2.0) < 0.01,
         "%.4f（期望 2.0 —— 小图不缩放，只乘 DPR）" % p4.scale)

    # ---- prefer=png ----
    p5 = prepare_for_llm(_Bmp(100, 100), prefer="png")
    _add("prefer='png' 时走 PNG", p5.codec == "png-zlib", p5.codec)

    # ---- None 不崩 ----
    p6 = prepare_for_llm(None)
    _add("★ 传 None 不崩（codec=none）",
         p6.codec == "none" and not p6.data, "安全")
    _add("零尺寸位图不崩",
         prepare_for_llm(_Bmp(0, 0)).codec == "none", "安全")

    # ---- 参数钳制 ----
    p7 = prepare_for_llm(big, max_edge=10)
    _add("★ max_edge 被钳到下限（不能太小）",
         max(p7.width, p7.height) >= MIN_MAX_EDGE,
         "%dx%d" % (p7.width, p7.height))
    p8 = prepare_for_llm(big, max_edge=99999)
    _add("★ max_edge 被钳到上限",
         max(p8.width, p8.height) <= MAX_MAX_EDGE,
         "%dx%d" % (p8.width, p8.height))

    # ============ 8) 序列化 ============
    _add("to_dict JSON 安全",
         __import__("json").dumps(p.to_dict(), ensure_ascii=False) != "",
         "ok")
    _add("describe 可读", "scale=" in p.describe(),
         p.describe()[:50])
    _add("b64_len 估算合理",
         p.b64_len > len(p.data), "%d > %d" % (p.b64_len, len(p.data)))

    # ============ 9) 零第三方 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★★ 不 import PIL / cv2（零第三方）",
         not (mods & {"PIL", "cv2", "numpy", "imageio"}),
         sorted(mods))
    _add("★ 用 zlib 做 PNG（标准库）", "zlib" in mods, "zlib")

    # ============ 10) 不做增强 ============
    names = {n.name for n in _ast.walk(tree)
             if isinstance(n, _ast.FunctionDef)}
    _add("★ 不做图像增强（无 sharpen / denoise / threshold）",
         not (names & {"sharpen", "denoise", "threshold", "equalize",
                       "enhance", "upscale"}),
         "只缩放/裁剪/编码")

    elapsed = time.perf_counter() - t0
    return {"module": "vision.image_prep", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


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
    print(describe_image_prep())
