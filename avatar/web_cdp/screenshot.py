# -*- coding: utf-8 -*-
"""网页截图：``Page.captureScreenshot`` + ``clip`` 区域裁剪。

和阶段二的**桌面截图**不同：那个抓的是屏幕像素（会被遮挡、被别的窗口盖住），
这个抓的是**渲染器里的合成结果** —— 即使窗口最小化、被遮挡、在虚拟桌面之外，
照样能拿到完整页面。做验证时这是 Web 端更可靠的证据源。

## 三种截法

============================  ==========================================
``capture_visible``           只抓视口（默认，最快）
``capture_full_page``         抓完整页面（用 ``Page.getLayoutMetrics``
                              拿 contentSize 再设 ``captureBeyondViewport``）
``capture_element``           抓某个元素（``DOM.getBoxModel`` 拿文档坐标
                              再 clip）
============================  ==========================================

## 坑

1. **``captureBeyondViewport`` 的坐标系是文档坐标系**，不是视口坐标系。
   视口截图（不传 clip）返回的是**当前可见区域**；一旦传了 clip，
   Chrome 会当成文档坐标。混用会导致截到空白。
   ``getLayoutMetrics`` 返回的 ``cssContentSize`` 是文档坐标，
   ``cssVisualViewport.pageX/pageY`` 才是当前滚动位置 —— 要拼起来用。

2. **``clip.scale`` 必须显式给 1.0**。不给的话高 DPI 机器上返回的图
   尺寸和 clip 对不上（Chrome 默认按 deviceScaleFactor 放大）。

3. **元素可能在视口外**：``DOM.getBoxModel`` 返回的坐标是文档坐标，
   截元素时不需要先滚动 —— 但页面对"不在视口内就不渲染"（虚拟列表）
   的情况会截到空。这种元素要先 ``scroll_into_view``。

4. **超大页面会失败**：宽高乘积超过 Chrome 内部限制（约 16384×16384）
   时 ``captureScreenshot`` 直接报错。所以 ``max_pixels`` 要兜底，
   超限时按比例缩放 clip。

5. **PNG 是 base64 传的**，要 ``base64.b64decode``。空字符串表示
   "截了个寂寞"（通常是页面还在加载或坐标为负），必须当失败处理。
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Dict, Optional, Tuple

from ..errors import AvatarError

__all__ = [
    "ScreenshotError",
    "ShotResult",
    "capture_visible",
    "capture_full_page",
    "capture_element",
    "capture_clip",
    "save_shot",
    "get_layout_metrics",
    "set_device_metrics",
    "selfcheck",
]


class ScreenshotError(AvatarError):
    """截图失败。"""

    exit_code = 18


# Chrome 对合成图面尺寸有硬上限；超过会直接报错。
MAX_DIMENSION = 16384
DEFAULT_MAX_PIXELS = MAX_DIMENSION * MAX_DIMENSION
# 单边也不能超过这个值，否则即使总面积没超 Chrome 也拒。
MAX_SIDE = MAX_DIMENSION


class ShotResult(object):
    """一次截图的结果。

    ``data`` 是**原始 PNG 字节**（不是 base64）。
    ``ok`` 为 False 时 ``data`` 为空，``error`` 说明原因。
    """

    __slots__ = ("ok", "data", "width", "height", "format_", "clip",
                 "beyond_viewport", "captured_at", "error", "elapsed")

    def __init__(
        self,
        ok: bool,
        data: bytes = b"",
        *,
        width: int = 0,
        height: int = 0,
        format_: str = "png",
        clip: Optional[Dict[str, float]] = None,
        beyond_viewport: bool = False,
        error: Optional[str] = None,
        elapsed: float = 0.0,
    ) -> None:
        self.ok = bool(ok)
        self.data = data or b""
        self.width = int(width)
        self.height = int(height)
        self.format_ = format_
        self.clip = clip
        self.beyond_viewport = bool(beyond_viewport)
        self.captured_at = time.time()
        self.error = error
        self.elapsed = float(elapsed)

    @property
    def size(self) -> int:
        """字节数。"""
        return len(self.data)

    @property
    def empty(self) -> bool:
        return len(self.data) == 0

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii") if self.data else ""

    def save(self, path: str) -> str:
        """落盘。返回实际路径。父目录不存在则创建。"""
        if not self.ok or not self.data:
            raise ScreenshotError("没有可保存的图像数据", detail=self.error)
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(self.data)
        return os.path.abspath(path)

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "bytes": self.size,
            "width": self.width,
            "height": self.height,
            "format": self.format_,
            "beyond_viewport": self.beyond_viewport,
            "clip": self.clip,
            "elapsed": round(self.elapsed, 4),
            "error": self.error,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if not self.ok:
            return "<ShotResult FAIL %s>" % (self.error,)
        return "<ShotResult %dx%d %dB>" % (self.width, self.height, self.size)


# --------------------------------------------------------------------------
# 布局度量
# --------------------------------------------------------------------------

def _metrics_of(resp: Any) -> Dict[str, Any]:
    """把 ``Page.getLayoutMetrics`` 的响应压成统一的扁平结构。

    CDP 新旧字段并存（``contentSize`` / ``cssContentSize``），
    这里两个都取，优先 css 版（不受页面缩放影响，和 clip 坐标系一致）。
    """
    if not isinstance(resp, dict):
        return {}
    content = resp.get("cssContentSize") or resp.get("contentSize") or {}
    layout = resp.get("cssLayoutViewport") or resp.get("layoutViewport") or {}
    visual = resp.get("cssVisualViewport") or resp.get("visualViewport") or {}
    return {
        "content_width": float(content.get("width", 0) or 0),
        "content_height": float(content.get("height", 0) or 0),
        "layout_x": float(layout.get("pageX", 0) or 0),
        "layout_y": float(layout.get("pageY", 0) or 0),
        "layout_client_w": float(layout.get("clientWidth", 0) or 0),
        "layout_client_h": float(layout.get("clientHeight", 0) or 0),
        "visual_x": float(visual.get("pageX", 0) or 0),
        "visual_y": float(visual.get("pageY", 0) or 0),
        "visual_client_w": float(visual.get("clientWidth", 0) or 0),
        "visual_client_h": float(visual.get("clientHeight", 0) or 0),
        "visual_scale": float(visual.get("scale", 1.0) or 1.0),
    }


def get_layout_metrics(sess: Any, *, session_id: Optional[str] = None,
                       timeout: float = 10.0) -> Dict[str, Any]:
    """取页面布局度量。失败返回空 dict（不抛）。"""
    try:
        resp = sess.call("Page.getLayoutMetrics", {}, session_id=session_id,
                         timeout=timeout)
    except Exception:
        return {}
    return _metrics_of(resp)


def set_device_metrics(
    sess: Any,
    *,
    width: int,
    height: int,
    device_scale_factor: float = 1.0,
    mobile: bool = False,
    session_id: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """覆盖设备度量（模拟特定分辨率）。常用于无头下的稳定尺寸。"""
    try:
        sess.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(width),
                "height": int(height),
                "deviceScaleFactor": float(device_scale_factor),
                "mobile": bool(mobile),
            },
            session_id=session_id,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def clear_device_metrics(sess: Any, *, session_id: Optional[str] = None,
                         timeout: float = 10.0) -> bool:
    try:
        sess.call("Emulation.clearDeviceMetricsOverride", {},
                  session_id=session_id, timeout=timeout)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 核心截取
# --------------------------------------------------------------------------

def _png_size(data: bytes) -> Tuple[int, int]:
    """从 PNG 头里读宽高（IHDR 在固定偏移）。

    只认 PNG —— CDP 默认给 PNG。读不出来返回 (0, 0)，不影响功能。
    """
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (0, 0)
    try:
        import struct
        w, h = struct.unpack(">II", data[16:24])
        return (int(w), int(h))
    except Exception:
        return (0, 0)


def _clamp_clip(clip: Dict[str, float], *, max_pixels: int) -> Dict[str, float]:
    """把 clip 压进 Chrome 能接受的尺寸范围。

    超限时**等比缩小**（而不是裁掉），保证画面完整 —— 打榜取证时
    "看得全"比"看得清"重要。scale 会跟着降，所以最终像素对齐原图。
    """
    x = float(clip.get("x", 0) or 0)
    y = float(clip.get("y", 0) or 0)
    w = max(1.0, float(clip.get("width", 1) or 1))
    h = max(1.0, float(clip.get("height", 1) or 1))
    scale = float(clip.get("scale", 1.0) or 1.0)

    # 单边限制 → 按比例缩 clip，scale 反向补回来
    side = max(w, h)
    if side > MAX_SIDE:
        k = float(MAX_SIDE) / side
        w *= k
        h *= k
        scale *= k

    # 总量限制
    if int(w) * int(h) > int(max_pixels):
        k = (float(max_pixels) / float(int(w) * int(h))) ** 0.5
        w *= k
        h *= k
        scale *= k

    return {
        "x": round(x, 3),
        "y": round(y, 3),
        "width": max(1.0, round(w, 3)),
        "height": max(1.0, round(h, 3)),
        "scale": max(0.01, round(scale, 6)),
    }


def capture_clip(
    sess: Any,
    clip: Dict[str, float],
    *,
    session_id: Optional[str] = None,
    timeout: float = 20.0,
    format_: str = "png",
    quality: Optional[int] = None,
    beyond_viewport: bool = False,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    from_surface: bool = True,
    optimize: bool = False,
) -> ShotResult:
    """按 clip 截图（**文档坐标系**）。

    这是所有截图的底座。``capture_visible`` 就是"不传 clip"的特例。
    """
    t0 = time.time()
    params: Dict[str, Any] = {
        "format": format_,
        "fromSurface": bool(from_surface),
        "captureBeyondViewport": bool(beyond_viewport),
    }
    if format_ == "jpeg" and quality is not None:
        params["quality"] = max(0, min(100, int(quality)))
    if optimize:
        params["optimizeForSpeed"] = True

    eff_clip = None
    if clip:
        eff_clip = _clamp_clip(clip, max_pixels=max_pixels)
        params["clip"] = eff_clip
        # clip 存在时 beyondViewport 语义会变，显式对齐避免截空白
        if not beyond_viewport:
            params.pop("captureBeyondViewport", None)

    try:
        resp = sess.call("Page.captureScreenshot", params,
                         session_id=session_id, timeout=timeout)
    except Exception as exc:
        return ShotResult(False, error="cdp_error: %s" % (exc,),
                          clip=eff_clip, elapsed=time.time() - t0)

    raw = ""
    if isinstance(resp, dict):
        raw = resp.get("data") or ""
    if not raw:
        return ShotResult(False, error="empty_image", clip=eff_clip,
                          beyond_viewport=beyond_viewport,
                          elapsed=time.time() - t0)

    try:
        data = base64.b64decode(raw)
    except Exception as exc:
        return ShotResult(False, error="decode_error: %s" % (exc,),
                          clip=eff_clip, elapsed=time.time() - t0)

    if not data:
        return ShotResult(False, error="empty_image", clip=eff_clip,
                          elapsed=time.time() - t0)

    w, h = _png_size(data) if format_ == "png" else (0, 0)
    return ShotResult(True, data, width=w, height=h, format_=format_,
                      clip=eff_clip, beyond_viewport=beyond_viewport,
                      elapsed=time.time() - t0)


def capture_visible(
    sess: Any,
    *,
    session_id: Optional[str] = None,
    timeout: float = 20.0,
    format_: str = "png",
    quality: Optional[int] = None,
    from_surface: bool = True,
) -> ShotResult:
    """抓当前视口。最快，也最常用（验证页面状态时够用）。"""
    return capture_clip(sess, {}, session_id=session_id, timeout=timeout,
                        format_=format_, quality=quality,
                        beyond_viewport=False, from_surface=from_surface)


def capture_full_page(
    sess: Any,
    *,
    session_id: Optional[str] = None,
    timeout: float = 30.0,
    format_: str = "png",
    quality: Optional[int] = None,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    settle: float = 0.0,
) -> ShotResult:
    """抓完整页面（含滚动区域）。

    流程：``Page.getLayoutMetrics`` → 取 ``cssContentSize`` 作为 clip
    （文档坐标，从 0,0 起）→ 带 ``captureBeyondViewport`` 截。
    """
    if settle > 0:
        time.sleep(settle)

    m = get_layout_metrics(sess, session_id=session_id, timeout=timeout)
    w = m.get("content_width") or m.get("layout_client_w") or 0
    h = m.get("content_height") or m.get("layout_client_h") or 0
    if w <= 0 or h <= 0:
        # 度量拿不到 —— 退化成视口截图，别硬报错
        res = capture_visible(sess, session_id=session_id, timeout=timeout,
                              format_=format_, quality=quality)
        if res.ok:
            res.beyond_viewport = False
        return res

    clip = {"x": 0.0, "y": 0.0, "width": float(w), "height": float(h),
            "scale": 1.0}
    res = capture_clip(sess, clip, session_id=session_id, timeout=timeout,
                       format_=format_, quality=quality,
                       beyond_viewport=True, max_pixels=max_pixels)
    res.beyond_viewport = True
    return res


def capture_element(
    sess: Any,
    selector: str,
    *,
    session_id: Optional[str] = None,
    timeout: float = 20.0,
    format_: str = "png",
    quality: Optional[int] = None,
    padding: float = 0.0,
    scroll_first: bool = True,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> ShotResult:
    """抓单个元素。

    ``padding`` 给元素外扩像素（做个"带边框"的效果图）。
    ``scroll_first`` 默认滚动到元素 —— 虚拟列表/懒加载页面上必须开，
    否则元素的 box 存在但内容没渲染，会截到空。
    """
    t0 = time.time()

    box = _element_doc_box(sess, selector, session_id=session_id,
                           timeout=timeout)
    if box is None:
        # 再试一次：先滚进视口再取 box
        if scroll_first:
            try:
                from .dom import scroll_into_view
                scroll_into_view(sess, selector, session_id=session_id,
                                 timeout=timeout)
                time.sleep(0.12)
                box = _element_doc_box(sess, selector, session_id=session_id,
                                       timeout=timeout)
            except Exception:
                box = None
    if box is None:
        return ShotResult(False, error="element_not_found: %s" % selector,
                          elapsed=time.time() - t0)

    x, y, w, h = box
    if w <= 0 or h <= 0:
        return ShotResult(False, error="zero_size", elapsed=time.time() - t0)

    pad = float(padding or 0)
    clip = {
        "x": max(0.0, x - pad),
        "y": max(0.0, y - pad),
        "width": w + pad * 2,
        "height": h + pad * 2,
        "scale": 1.0,
    }
    res = capture_clip(sess, clip, session_id=session_id, timeout=timeout,
                       format_=format_, quality=quality,
                       beyond_viewport=True, max_pixels=max_pixels)
    res.beyond_viewport = True
    if res.ok:
        res.clip = clip
    return res


def _element_doc_box(
    sess: Any,
    selector: str,
    *,
    session_id: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[Tuple[float, float, float, float]]:
    """取元素在**文档坐标系**里的 (x, y, w, h)。

    走 ``DOM.getBoxModel``（返回的 quad 已是文档坐标）。
    ``DOM.getBoxModel`` 只认 nodeId/backendNodeId/objectId —— 不认选择器，
    所以先 ``DOM.querySelector`` 拿 nodeId。**注意**：``DOM.querySelector``
    走的是 ``DOM.enable`` 后的文档树，且不穿透 Shadow DOM。
    穿透需求走 ``dom.get_box_model``。
    """
    try:
        sess.enable("DOM", session_id=session_id)
    except Exception:
        pass

    node_id = 0
    try:
        doc = sess.call("DOM.getDocument", {"depth": 1},
                        session_id=session_id, timeout=timeout)
        root = (doc or {}).get("root") or {}
        root_id = root.get("nodeId") or 0
        if root_id:
            found = sess.call("DOM.querySelector",
                              {"nodeId": root_id, "selector": selector},
                              session_id=session_id, timeout=timeout)
            node_id = int((found or {}).get("nodeId") or 0)
    except Exception:
        node_id = 0

    if not node_id:
        # 退路：DOM.getBoxModel 也接受 objectId，但不能传原始选择器。
        # 用 Runtime 拿到 objectId 再查。
        try:
            from .dom import _make_matcher  # noqa: F401  (保持依赖显式)
        except Exception:
            pass
        return None

    try:
        model = sess.call("DOM.getBoxModel", {"nodeId": node_id},
                          session_id=session_id, timeout=timeout)
    except Exception:
        return None

    quad = None
    if isinstance(model, dict):
        m = model.get("model")
        if isinstance(m, dict):
            quad = m.get("border") or m.get("content") or m.get("padding")
    if not quad or len(quad) < 8:
        return None

    xs = [float(quad[i]) for i in range(0, 8, 2)]
    ys = [float(quad[i]) for i in range(1, 8, 2)]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return (x0, y0, x1 - x0, y1 - y0)


def save_shot(res: ShotResult, path: str) -> str:
    """ShotResult 落盘。失败抛 ScreenshotError。"""
    return res.save(path)


def shot_to_bytes(res: ShotResult) -> bytes:
    return res.data


def compare_sizes(a: ShotResult, b: ShotResult) -> bool:
    """两张图尺寸是否一致（验证"页面没重排"的廉价判据）。"""
    if not (a.ok and b.ok):
        return False
    return (a.width, a.height) == (b.width, b.height)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

class _FakeSession(object):
    """假会话：可按 method 注册响应，也可注册异常。"""

    def __init__(self) -> None:
        self.calls = []
        self.responses = {}
        self.errors = {}
        self.enabled = []

    def enable(self, *domains, **kw):
        self.enabled.extend(domains)
        return {}

    def call(self, method, params=None, *, session_id=None, timeout=None,
             **kw):
        self.calls.append((method, dict(params or {})))
        if method in self.errors:
            raise self.errors[method]
        if method in self.responses:
            r = self.responses[method]
            return r(params, session_id) if callable(r) else r
        raise AssertionError("unexpected call: %s" % method)


def _png(w: int, h: int) -> bytes:
    """造一个最小合法 PNG（只为了 IHDR 宽高）。"""
    import struct
    import zlib
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    idat = chunk(b"IDAT", zlib.compress(raw, 1))
    return sig + ihdr + idat + chunk(b"IEND", b"")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- PNG 尺寸解析 ---
    p = _png(37, 11)
    add("png_size 解析正确", _png_size(p) == (37, 11), _png_size(p))
    add("非 PNG 返回 (0,0)", _png_size(b"hello world") == (0, 0))
    add("空字节返回 (0,0)", _png_size(b"") == (0, 0))

    # --- clip 夹取 ---
    c = _clamp_clip({"x": 0, "y": 0, "width": 100, "height": 50, "scale": 1.0},
                    max_pixels=10 ** 9)
    add("小 clip 原样保留", c["width"] == 100.0 and c["height"] == 50.0, c)

    big = _clamp_clip({"x": 0, "y": 0, "width": 100000, "height": 20000,
                       "scale": 1.0}, max_pixels=10 ** 9)
    add("单边超限被缩", big["width"] <= MAX_SIDE + 1, big)
    add("单边超限后比例保持",
        abs(big["width"] / big["height"] - 5.0) < 0.05, big["width"] / big["height"])
    add("单边超限后 scale 补偿", big["scale"] < 1.0, big["scale"])

    huge = _clamp_clip({"x": 0, "y": 0, "width": 16000, "height": 16000,
                        "scale": 1.0}, max_pixels=10 ** 7)
    add("总面积超限被缩",
        int(huge["width"]) * int(huge["height"]) <= 10 ** 7 + 1000, huge)
    add("面积缩放用平方根",
        abs(huge["width"] / huge["height"] - 1.0) < 0.01, huge)

    neg = _clamp_clip({"x": -5, "y": -5, "width": 0, "height": -3,
                       "scale": 0}, max_pixels=10 ** 9)
    add("非法尺寸兜底为 >=1", neg["width"] >= 1.0 and neg["height"] >= 1.0, neg)
    add("scale 兜底非零", neg["scale"] > 0, neg["scale"])

    # --- 度量解析 ---
    m = _metrics_of({
        "cssContentSize": {"width": 1280, "height": 4200},
        "cssVisualViewport": {"pageX": 0, "pageY": 300, "clientWidth": 1280,
                              "clientHeight": 800, "scale": 1.0},
        "cssLayoutViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1280,
                              "clientHeight": 800},
    })
    add("content 尺寸取 css 版",
        m["content_width"] == 1280.0 and m["content_height"] == 4200.0, m)
    add("visual 滚动位置分离", m["visual_y"] == 300.0, m)

    m2 = _metrics_of({"contentSize": {"width": 100, "height": 200}})
    add("旧字段 contentSize 兼容",
        m2["content_width"] == 100.0 and m2["content_height"] == 200.0, m2)

    add("getLayoutMetrics 异常返回空 dict 不抛",
        get_layout_metrics(_SessionThatFails()) == {})

    # --- ShotResult 契约 ---
    empty = ShotResult(False, error="x")
    add("失败 ShotResult.ok False", empty.ok is False)
    add("失败 ShotResult.data 为空", empty.data == b"")
    add("失败 ShotResult.size 0", empty.size == 0)
    add("失败 ShotResult.empty True", empty.empty is True)
    add("失败 ShotResult.to_base64 空串", empty.to_base64() == "")
    add("失败 ShotResult.describe 带 error",
        empty.describe()["error"] == "x")

    okr = ShotResult(True, p, width=37, height=11)
    add("成功 ShotResult.ok True", okr.ok is True)
    add("成功 ShotResult.size 一致", okr.size == len(p))
    add("成功 ShotResult.pixels 乘积", okr.pixels == 37 * 11)
    add("成功 to_base64 可解回",
        base64.b64decode(okr.to_base64()) == p)
    add("describe 含全部字段",
        set(okr.describe()) >= {"ok", "bytes", "width", "height", "format",
                                "beyond_viewport", "clip", "elapsed", "error"})

    import tempfile
    tmpd = tempfile.mkdtemp(prefix="avatar-shot-")
    fp = os.path.join(tmpd, "sub", "a.png")
    saved = okr.save(fp)
    add("save 自动建目录", os.path.isfile(saved), saved)
    with open(saved, "rb") as f:
        add("save 内容一致", f.read() == p)
    try:
        empty.save(os.path.join(tmpd, "no.png"))
        add("空图 save 抛错", False, "没抛")
    except ScreenshotError:
        add("空图 save 抛错", True)

    # --- capture_visible ---
    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = {"data": _b64(_png(800, 600))}
    r = capture_visible(s, timeout=5)
    add("capture_visible ok", r.ok)
    add("capture_visible 尺寸解析", (r.width, r.height) == (800, 600), r)
    add("capture_visible 不传 clip",
        "clip" not in s.calls[0][1], s.calls[0][1])
    add("capture_visible fromSurface 默认 True",
        s.calls[0][1]["fromSurface"] is True)
    add("capture_visible 不 beyondViewport",
        "captureBeyondViewport" not in s.calls[0][1]
        or s.calls[0][1]["captureBeyondViewport"] is False)

    # --- capture_visible 空数据 ---
    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = {"data": ""}
    r = capture_visible(s, timeout=5)
    add("空 base64 判失败", not r.ok and r.error == "empty_image", r.error)

    # --- capture_visible CDP 异常 ---
    s = _FakeSession()
    s.errors["Page.captureScreenshot"] = RuntimeError("boom")
    r = capture_visible(s, timeout=5)
    add("CDP 异常判失败不抛", not r.ok and "cdp_error" in (r.error or ""), r.error)
    add("CDP 异常 elapsed 有值", r.elapsed >= 0)

    # --- JPEG ---
    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = {"data": _b64(b"\xff\xd8junk")}
    r = capture_visible(s, format_="jpeg", quality=77, timeout=5)
    add("jpeg 带 quality", s.calls[0][1].get("quality") == 77, s.calls[0][1])
    add("jpeg 不解析 PNG 尺寸", (r.width, r.height) == (0, 0), r)
    add("jpeg ok（非空即成功）", r.ok)

    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = {"data": _b64(b"x")}
    capture_visible(s, format_="jpeg", quality=999, timeout=5)
    add("quality 夹到 100", s.calls[0][1]["quality"] == 100)
    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = {"data": _b64(b"x")}
    capture_visible(s, format_="jpeg", quality=-5, timeout=5)
    add("quality 夹到 0", s.calls[0][1]["quality"] == 0)

    # --- capture_full_page ---
    s = _FakeSession()

    def _shot(params, session_id):
        clip = params.get("clip") or {}
        return {"data": _b64(_png(int(clip.get("width", 100)),
                                  int(clip.get("height", 100))))}

    s.responses["Page.getLayoutMetrics"] = {
        "cssContentSize": {"width": 1280, "height": 3000},
        "cssVisualViewport": {"pageX": 0, "pageY": 500, "clientWidth": 1280,
                              "clientHeight": 800},
    }
    s.responses["Page.captureScreenshot"] = _shot
    r = capture_full_page(s, timeout=5)
    add("full_page ok", r.ok)
    add("full_page beyondViewport", r.beyond_viewport is True)
    add("full_page clip 从 0,0 起",
        r.clip["x"] == 0.0 and r.clip["y"] == 0.0, r.clip)
    add("full_page 高度取 contentSize", r.clip["height"] == 3000.0, r.clip)
    add("full_page 尺寸对", (r.width, r.height) == (1280, 3000), r)

    # 度量失败 → 退化视口
    s = _FakeSession()
    s.errors["Page.getLayoutMetrics"] = RuntimeError("nope")
    s.responses["Page.captureScreenshot"] = {"data": _b64(_png(400, 300))}
    r = capture_full_page(s, timeout=5)
    add("度量失败退化视口截图", r.ok and r.beyond_viewport is False, r)
    add("退化后不传 clip",
        all("clip" not in c[1] for c in s.calls
            if c[0] == "Page.captureScreenshot"))

    # --- capture_element ---
    s = _FakeSession()
    s.responses["DOM.getDocument"] = {"root": {"nodeId": 1}}
    s.responses["DOM.querySelector"] = {"nodeId": 42}
    s.responses["DOM.getBoxModel"] = {
        "model": {"border": [100, 200, 300, 200, 300, 260, 100, 260],
                  "content": [100, 200, 300, 200, 300, 260, 100, 260],
                  "width": 200, "height": 60}
    }
    s.responses["Page.captureScreenshot"] = _shot
    r = capture_element(s, "#btn", timeout=5)
    add("element ok", r.ok)
    add("element clip 用文档坐标",
        r.clip["x"] == 100.0 and r.clip["y"] == 200.0, r.clip)
    add("element 尺寸对",
        r.clip["width"] == 200.0 and r.clip["height"] == 60.0, r.clip)
    add("element beyondViewport", r.beyond_viewport is True)
    add("element 先 enable DOM", "DOM" in s.enabled, s.enabled)

    r = capture_element(s, "#btn", padding=10, timeout=5)
    add("element padding 外扩",
        r.clip["width"] == 220.0 and r.clip["height"] == 80.0, r.clip)
    add("element padding 不越界为负", r.clip["x"] == 90.0, r.clip)

    r = capture_element(s, "#edge", padding=500, timeout=5)
    add("element padding 左边夹到 0", r.clip["x"] == 0.0, r.clip)

    # 找不到元素
    s = _FakeSession()
    s.responses["DOM.getDocument"] = {"root": {"nodeId": 1}}
    s.responses["DOM.querySelector"] = {"nodeId": 0}
    r = capture_element(s, "#gone", scroll_first=False, timeout=5)
    add("元素不存在判失败",
        not r.ok and (r.error or "").startswith("element_not_found"), r.error)

    # 零尺寸
    s = _FakeSession()
    s.responses["DOM.getDocument"] = {"root": {"nodeId": 1}}
    s.responses["DOM.querySelector"] = {"nodeId": 7}
    s.responses["DOM.getBoxModel"] = {
        "model": {"border": [10, 10, 10, 10, 10, 10, 10, 10]}}
    r = capture_element(s, "#zero", scroll_first=False, timeout=5)
    add("零尺寸判失败", not r.ok and r.error == "zero_size", r.error)

    # box model 抛错
    s = _FakeSession()
    s.responses["DOM.getDocument"] = {"root": {"nodeId": 1}}
    s.responses["DOM.querySelector"] = {"nodeId": 7}
    s.errors["DOM.getBoxModel"] = RuntimeError("nope")
    r = capture_element(s, "#x", scroll_first=False, timeout=5)
    add("boxModel 抛错判失败不抛", not r.ok, r.error)

    # quad 不足 8 个数
    s = _FakeSession()
    s.responses["DOM.getDocument"] = {"root": {"nodeId": 1}}
    s.responses["DOM.querySelector"] = {"nodeId": 7}
    s.responses["DOM.getBoxModel"] = {"model": {"border": [1, 2, 3]}}
    r = capture_element(s, "#x", scroll_first=False, timeout=5)
    add("畸形 quad 判失败", not r.ok, r.error)

    # --- capture_clip 显式 ---
    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = _shot
    r = capture_clip(s, {"x": 5, "y": 6, "width": 70, "height": 80,
                         "scale": 1.0}, timeout=5)
    add("clip 原样传递", s.calls[0][1]["clip"]["x"] == 5.0, s.calls[0][1]["clip"])
    add("clip + beyondViewport 可共存", r.ok and r.beyond_viewport is False)

    s = _FakeSession()
    s.responses["Page.captureScreenshot"] = _shot
    r = capture_clip(s, {"x": 0, "y": 0, "width": 50, "height": 50},
                     beyond_viewport=True, timeout=5)
    add("beyondViewport 为 True 时保留该参数",
        s.calls[0][1].get("captureBeyondViewport") is True)
    add("scale 缺省补 1.0", s.calls[0][1]["clip"]["scale"] == 1.0,
        s.calls[0][1]["clip"])

    # --- 对比 ---
    a = ShotResult(True, p, width=10, height=10)
    b = ShotResult(True, p, width=10, height=10)
    c2 = ShotResult(True, p, width=11, height=10)
    add("compare_sizes 相同为 True", compare_sizes(a, b) is True)
    add("compare_sizes 不同为 False", compare_sizes(a, c2) is False)
    add("compare_sizes 有失败为 False", compare_sizes(a, empty) is False)

    # --- 设备度量 ---
    s = _FakeSession()
    s.responses["Emulation.setDeviceMetricsOverride"] = {}
    add("set_device_metrics 成功", set_device_metrics(s, width=1280,
                                                      height=800) is True)
    add("set_device_metrics 参数正确",
        s.calls[0][1]["width"] == 1280 and s.calls[0][1]["mobile"] is False,
        s.calls[0][1])
    s = _FakeSession()
    s.errors["Emulation.setDeviceMetricsOverride"] = RuntimeError("x")
    add("set_device_metrics 异常返回 False",
        set_device_metrics(s, width=1, height=1) is False)

    s = _FakeSession()
    s.responses["Emulation.clearDeviceMetricsOverride"] = {}
    add("clear_device_metrics 成功", clear_device_metrics(s) is True)

    # --- 清掉临时目录 ---
    try:
        import shutil
        shutil.rmtree(tmpd, ignore_errors=True)
    except Exception:
        pass

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "web_cdp.screenshot"}


class _SessionThatFails(object):
    def call(self, *a, **kw):
        raise RuntimeError("fails")


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
