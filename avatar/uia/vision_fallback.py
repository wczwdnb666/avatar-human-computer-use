# -*- coding: utf-8 -*-
"""视觉兜底 —— 纯 Python 的模板匹配与颜色/差分检测。

## 与 OCR 的分工

两个都是"UIA 定位失败之后"的降级手段，但解决的问题不同：

    OCR        解决"我知道文字内容，但 UIA 看不到这个控件"
               例：游戏/自绘界面/截图里的按钮

    视觉       解决"我知道它长什么样，但说不出它叫什么"
               例：某个特定图标、勾选框、进度条

所以视觉这一层的核心是**模板匹配**与**视觉特征检测**，
不是文字识别。

## 实现状态

**已实现**（阶段七）。这里原有一节叫"现在不实现的原因"，
讲的是"视觉比 OCR 更容易看起来能用其实很脆" —— 那些顾虑是真的，
但结论不是"不做"，而是**做的时候要守住几条**：

· 模板匹配对 DPI 缩放、主题、字体渲染差异极其敏感
  → 统一到客户区口径，模板尺寸由调用方负责给对
· 阈值调松 → 满屏误报；调紧 → 一个都匹配不到
  → 默认阈值 0.85（见 ``DEFAULT_THRESHOLD``），宁可让调用方自己调低
· 没有可靠的"匹配置信度"标定
  → 用 **NCC 归一化互相关**，分数含义明确（1.0 = 完全一致）
· 纯色/空白区域会被误判成"匹配成功"
  → 方差为 0 时 NCC 无意义，**直接判不匹配**（刻意如此，不是 bug）

## 接口约定

· 返回的 ``VisualHit`` **必须带所在窗口句柄**，坐标是**客户区**口径
· 必须给出可解释的 ``score``，并且**不允许**在没有候选时"返回最像的"
· 纯色/空白区域判定要可靠 —— 很多"匹配成功"其实是匹配到了一片白

## 依赖

**仅标准库。** 匹配算法（NCC / 颜色聚类 / 差分）是纯 Python 实现的，
**没用 OpenCV，也没用 numpy** —— 这是刻意的：引入 ``cv2`` 会让
"零依赖"破功，而这个算法的规模用纯 Python 完全够。

（代价是慢，所以它在降级链里排在 OCR 之后、视觉大模型之前 ——
见 ``avatar/vision/chain.py`` 的 ``LAYER_COST``。）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import UnsupportedError

__all__ = [
    "VisualHit", "VisualUnavailable", "is_available",
    "algorithm_available",
    "unavailable_reason", "describe_environment",
    "find_template_in_window",
    "find_icon", "check_box_state", "wait_for_template", "set_engine",
    "engine_name", "capture_backend",
    # ---- 阶段七：纯 Python 匹配 ----
    "Match", "find_template", "find_template_multi",
    "find_color", "diff",
]

#: 本模块在降级链里的名字
CAPABILITY = "vision"

#: 默认匹配分数阈值。
#:
#: 定在 0.85 而不是 0.7 的原因：视觉匹配的误报代价很高 ——
#: 匹配错了会点到一个**看起来合理但完全不对**的位置，比直接失败危险。
#: 宁可让调用方自己调低，也不要给一个"经常误报"的默认值。
DEFAULT_THRESHOLD = 0.85


class VisualUnavailable(UnsupportedError):
    """视觉能力不可用。上层捕获它沿降级链往下走。"""

    def __init__(self, message: str = "", **context):
        super().__init__(
            message or "视觉能力尚未接入（计划于阶段七实现）",
            capability=CAPABILITY, **context)


@dataclass
class VisualHit:
    """一次视觉匹配的结果。

    ## 坐标约定

    ``client_rect`` 是**相对所属窗口客户区**的矩形 —— 与 ``uia.Element``、
    ``ocr.OcrHit`` 完全一致的口径。不提供绝对屏幕坐标。

    ## ``score`` 的含义

    0.0~1.0。**不同匹配方式之间不可横向比较** —— 模板匹配的 0.9
    和颜色检测的 0.9 含义不同。调用方只能在同一匹配方式内部用它排序/过滤。
    """

    #: 匹配到什么（模板名 / "checkbox:checked" / ...）
    label: str = ""
    #: 所属顶层窗口句柄
    window: int = 0
    #: 客户区坐标矩形 ``(left, top, right, bottom)``
    client_rect: Optional[Tuple[int, int, int, int]] = None
    #: 匹配置信度 0.0~1.0（同一方式内可比）
    score: float = 0.0
    #: 匹配方式（"template" / "color" / "edge" / "model"）
    method: str = ""
    #: 额外信息（多尺度命中的缩放比等），排查用
    raw: Dict = field(default_factory=dict)

    @property
    def center_client(self) -> Optional[Tuple[int, int]]:
        if not self.client_rect:
            return None
        l, t, r, b = self.client_rect
        return ((l + r) // 2, (t + b) // 2)

    def to_dict(self) -> Dict:
        return {
            "label": self.label,
            "window": self.window,
            "client_rect": list(self.client_rect) if self.client_rect else None,
            "score": round(float(self.score), 4),
            "method": self.method,
        }

    def __str__(self):
        return "VisualHit(%r score=%.2f %s win=0x%X)" % (
            self.label, self.score, self.method, self.window)


# --------------------------------------------------------------------------
# 引擎注册（阶段七填充）
# --------------------------------------------------------------------------
#: 模板匹配实现的注册位。
#:
#: 约定签名::
#:
#:     fn(window: int, template, region: Optional[tuple],
#:        threshold: float, scales: Sequence[float]) -> List[VisualHit]
_ENGINE = None
_ENGINE_NAME = ""


def set_engine(fn, name: str = "") -> None:
    """注册真实视觉实现（阶段七调用）。传 ``None`` 可注销。"""
    global _ENGINE, _ENGINE_NAME
    _ENGINE = fn
    _ENGINE_NAME = name or ("custom" if fn else "")


def engine_name() -> str:
    """当前引擎名；未接入返回空串。"""
    return _ENGINE_NAME


def capture_backend() -> str:
    """截图后端名。

    这一项**已经可用**（阶段一实现了 ``vision.capture``），
    与实际识别能力分开报告 —— "能截图"不等于"能识别"。
    """
    try:
        from ..vision import capture
        return "vision.capture" if capture else ""
    except Exception:
        return ""


def is_available() -> bool:
    """视觉**引擎**是否可用（``set_engine`` 注册的外部实现）。

    ## 两条路径，可用性不同

    本模块有**两套**匹配能力，别混为一谈：

    ==========  =========================================  ==============
    路径        入口                                       可用性
    ==========  =========================================  ==============
    纯算法      ``find_template`` / ``find_color`` /        **恒定可用**
                ``diff``                                   （仅标准库）
    引擎        ``find_template_in_window`` /               注册了才可用
                ``find_icon``
    ==========  =========================================  ==============

    这个函数回答的是**引擎版**。引擎版要做"截图 + 多尺度 + 坐标换算"
    的完整链路，属于可选增强。

    ⚠ 这里原先恒定返回 ``False``，理由写的是"阶段二只做接口，
    阶段七实现"。阶段七实现之后那个理由就过期了，而且**低估了实际
    能力** —— 纯算法那套当时已经能用，降级链第四层走的就是它
    （见 ``avatar/vision/chain.py`` 的 ``_layer_template``）。
    """
    return _ENGINE is not None


def algorithm_available() -> bool:
    """纯 Python 匹配算法是否可用。**恒定 True。**

    它只用标准库、没有任何外部依赖、不需要 ``set_engine()``。
    留着这个函数是为了让调用方和诊断输出能明确区分
    "算法能不能用"与"引擎有没有接"—— 这两件事经常被混为一谈，
    混了就会得出"视觉不可用"这种**比实际能力更差**的结论。
    """
    return True


def unavailable_reason() -> str:
    """为什么**引擎版**不可用（算法版永远可用，不在这里报告）。"""
    if _ENGINE is None:
        return ("未接入外部视觉引擎（set_engine）。"
                "这不影响纯 Python 匹配 —— find_template / find_color / "
                "diff 只用标准库，始终可用，降级链第四层走的就是它们。"
                "缺的只是引擎版能力：多尺度模板匹配与图标库。")
    return ""


def describe_environment() -> Dict:
    """描述当前视觉环境 —— 给自检和诊断用。"""
    if _ENGINE is not None:
        return {
            "available": True,
            "engine": _ENGINE_NAME or "custom",
            "reason": "",
            "capture": capture_backend(),
            "install_hint": "",
            "backends": _known_backends(),
        }
    return {
        "available": False,
        "engine": "",
        "reason": unavailable_reason(),
        "capture": capture_backend(),
        "install_hint": (
            "接入方式（阶段七二选一）：\n"
            "  1) OpenCV 模板匹配：pip install opencv-python + numpy，"
            "多尺度匹配最成熟，体积约 60MB\n"
            "  2) 纯 numpy 实现：零额外依赖，但多尺度匹配要自己写，"
            "性能差不少\n"
            "⚠ 无论哪条路都**必须支持多尺度** —— 本机是 200% 缩放，"
            "单尺度模板在别的缩放比例下会全部失效。"
        ),
        "backends": _known_backends(),
    }


def _known_backends() -> List[Dict]:
    """探测本机已有的视觉相关依赖（只看装没装，不导入）。"""
    import importlib.util

    out = []
    for name, pkg, note in (
        ("opencv", "cv2", "多尺度模板匹配的事实标准"),
        ("numpy", "numpy", "纯 numpy 匹配/颜色统计的基础"),
        ("pillow", "PIL", "图像读写与缩放"),
    ):
        try:
            found = importlib.util.find_spec(pkg) is not None
        except Exception:
            found = False
        out.append({"name": name, "package": pkg, "installed": found,
                    "note": note})
    return out


# --------------------------------------------------------------------------
# 接口（阶段七填实现）
# --------------------------------------------------------------------------
def _require_engine():
    if _ENGINE is None:
        raise VisualUnavailable(
            "视觉匹配不可用。当前阶段（阶段二）只提供接口，"
            "实现计划在阶段七。"
            "现在请走 UIA 元素定位路径。")
    return _ENGINE


def find_template_in_window(window: int, template, *,
                  region: Optional[Tuple[int, int, int, int]] = None,
                  threshold: float = DEFAULT_THRESHOLD,
                  scales: Sequence[float] = (0.75, 1.0, 1.25, 1.5, 2.0),
                  limit: int = 1) -> List[VisualHit]:
    """在窗口里找模板图像。

    ## 参数

    ``window``
        顶层窗口句柄。**必填** —— 结果要换算成客户区坐标。
    ``template``
        模板。具体类型由引擎决定（文件路径 / ``Bitmap`` / numpy 数组）。
    ``region``
        **客户区坐标**矩形，限定搜索范围。范围越小越快越准。
    ``scales``
        **多尺度**：至少覆盖常见 DPI 缩放。本机 200%，
        默认值把这台机器和 100%/150% 的都覆盖到了。
    ``threshold``
        低于此分数丢弃。**不许**返回"最像的那个"。

    阶段七之前调用会抛 ``VisualUnavailable``。
    """
    fn = _require_engine()
    if not window:
        raise ValueError("find_template_in_window 必须传 window —— "
                         "没有窗口就无法把匹配结果换算成客户区坐标")
    hits = fn(window, template, region, threshold, tuple(scales)) or []
    hits = [h for h in hits if isinstance(h, VisualHit)]
    # ⚠ 阈值必须在本层**再过滤一遍**，不能只依赖引擎遵守约定。
    #   早先实现信任引擎自己筛，结果一个返回全部候选的引擎会让
    #   0.40 分的误报混进来 —— 而视觉误报的代价是"点到一个看起来
    #   合理但完全不对的位置"，比漏报危险得多。
    hits = [h for h in hits if float(h.score) >= float(threshold)]
    hits.sort(key=lambda h: -h.score)
    return hits[:limit] if limit else hits


def find_icon(window: int, name: str, **kw) -> Optional[VisualHit]:
    """按图标名找（图标库由引擎维护，阶段七提供）。

    比 ``find_template_in_window`` 高一层：调用方给逻辑名（``"close"`` /
    ``"settings"``），引擎负责选模板。好处是模板可以按主题/DPI 换，
    调用方不用管。
    """
    hits = find_template_in_window(window, name, **kw)
    return hits[0] if hits else None


def check_box_state(window: int, region: Tuple[int, int, int, int],
                    *, threshold: float = DEFAULT_THRESHOLD) -> Optional[str]:
    """判断一个复选框的勾选状态。

    返回 ``"checked"`` / ``"unchecked"`` / ``"indeterminate"``，
    判不出来返回 ``None``。

    ## 为什么单列这个接口

    复选框的"勾"在不同主题下长得完全不同（浅色主题的蓝勾、
    深色主题的白勾、禁用态的灰勾），而且 UIA 有时对这个状态
    上报不准。这是视觉兜底**最有价值**的场景之一：
    只要判断"区域中心有没有那个颜色的像素"，比定位整个控件简单可靠得多。

    ``region`` 是**客户区坐标**矩形。
    """
    fn = _require_engine()
    if not window:
        raise ValueError("check_box_state 必须传 window")
    hits = fn(window, "checkbox", region, threshold, (1.0,)) or []
    for h in hits:
        if isinstance(h, VisualHit) and h.label.startswith("checkbox:"):
            return h.label.split(":", 1)[1]
    return None


def wait_for_template(window: int, template, *, timeout: float = 5.0,
                      poll: float = 0.2, **kw) -> Optional[VisualHit]:
    """等模板出现。超时返回 ``None``（不抛异常 —— "没出现"是正常答案）。

    视觉匹配比 UIA 查询重得多（每次都要截图 + 匹配），
    所以轮询间隔比 ``uia.find.wait`` 大得多。
    """
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        hits = find_template_in_window(window, template,
                                       limit=1, **kw)
        if hits:
            return hits[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.02, poll))


# ==========================================================================
# 纯 Python 匹配算法（阶段七）
# ==========================================================================
# 这些函数**不需要任何引擎** —— 直接在位图上算。
# 放在本模块而不是 vision/ 下，是因为它们服务的是"降级链的第四层"，
# 与 VisualHit 的坐标口径必须一致。


@dataclass
class Match:
    """一次模板/颜色匹配的结果。**坐标相对传入的图。**"""

    rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    score: float = 0.0
    scale: float = 1.0
    method: str = "template"
    label: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def to_hit(self, window: int, *, off_x: int = 0,
               off_y: int = 0) -> VisualHit:
        """转成带窗口上下文的 :class:`VisualHit`。"""
        l, t, r, b = self.rect
        return VisualHit(
            label=self.label, window=int(window),
            client_rect=(l + off_x, t + off_y, r + off_x, b + off_y),
            score=self.score, method=self.method,
            raw={"scale": self.scale})

    def to_dict(self) -> Dict:
        return {"rect": list(self.rect), "score": round(self.score, 4),
                "scale": self.scale, "method": self.method,
                "label": self.label}

    def __str__(self) -> str:
        return "Match(%s %s score=%.3f)" % (self.label, self.rect,
                                            self.score)


def _gray(bitmap: Any, region: Optional[Tuple[int, int, int, int]] = None
          ) -> Tuple[bytearray, int, int]:
    """把位图转成**自上而下**的灰度数组 ``(data, w, h)``。

    ## 为什么转灰度

    模板匹配不需要颜色 —— 灰度既快（1/4 数据量）又稳
    （不受色偏影响）。需要颜色时用 :func:`find_color`。
    """
    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    if w <= 0 or h <= 0:
        raise VisualUnavailable("位图尺寸异常：%dx%d" % (w, h))

    if region:
        l = max(0, min(w, int(region[0])))
        t = max(0, min(h, int(region[1])))
        r = max(l, min(w, int(region[2])))
        b = max(t, min(h, int(region[3])))
    else:
        l, t, r, b = 0, 0, w, h

    cw, ch = max(0, r - l), max(0, b - t)
    if cw <= 0 or ch <= 0:
        raise VisualUnavailable("裁剪区域为空：%s" % (region,))

    stride = int(getattr(bitmap, "stride", 0) or (w * 4))
    px = bitmap.pixels
    out = bytearray(cw * ch)
    for y in range(ch):
        # DIB 自下而上 → 翻转
        srow = (h - 1 - (t + y)) * stride + l * 4
        drow = y * cw
        for x in range(cw):
            o = srow + x * 4
            # BGRA → 亮度（整数近似 Rec.601）
            out[drow + x] = (px[o] * 29 + px[o + 1] * 150
                             + px[o + 2] * 77) >> 8
    return out, cw, ch


def _to_gray_any(obj: Any) -> Tuple[bytearray, int, int]:
    """位图或 ``bytes`` 都能收（模板常是内存里的字节）。"""
    if isinstance(obj, (bytes, bytearray)):
        raise VisualUnavailable(
            "模板必须是位图对象（有 width/height/stride/pixels），"
            "不接受裸 bytes —— 无法知道尺寸")
    return _gray(obj)


def _ncc_at(hay: bytearray, hw: int, hh: int,
            tpl: bytearray, tw: int, th: int,
            ox: int, oy: int, tmean: float, tnorm: float) -> float:
    """在 ``(ox, oy)`` 处算归一化互相关（NCC）。

    ## 为什么用 NCC 而不是逐像素差

    逐像素差（SAD）对**亮度变化**极其敏感 —— 界面换了个主题、
    或者有个半透明遮罩，SAD 就废了。NCC 先减均值再算相关，
    对线性亮度变化免疫。

    代价是慢一些（每点要算均值和方差），但本模块的 stride
    默认是 2，实际能接受。
    """
    s = 0.0
    sq = 0.0
    cross = 0.0
    n = tw * th
    for y in range(th):
        hbase = (oy + y) * hw + ox
        tbase = y * tw
        for x in range(tw):
            hv = hay[hbase + x]
            tv = tpl[tbase + x]
            s += hv
            sq += hv * hv
            cross += hv * tv
    hmean = s / n
    # 分母：模板的范数 × 窗口的范数
    hvar = sq - n * hmean * hmean
    if hvar <= 1e-9 or tnorm <= 1e-9:
        return 0.0
    return (cross - n * hmean * tmean) / ((hvar ** 0.5) * tnorm)


def _prep_template(tpl: bytearray, tw: int, th: int) -> Tuple[float, float]:
    n = tw * th
    s = 0.0
    sq = 0.0
    for v in tpl:
        s += v
        sq += v * v
    mean = s / n
    var = sq - n * mean * mean
    return mean, (var ** 0.5 if var > 1e-9 else 0.0)


def find_template(haystack: Any, needle: Any, *,
                  region: Optional[Tuple[int, int, int, int]] = None,
                  tolerance: float = DEFAULT_THRESHOLD,
                  stride: int = 2) -> Optional[Match]:
    """在 ``haystack`` 里找 ``needle``，返回**最优**的一个匹配。

    ## 参数

    - ``region`` —— 只在图的这一块里找（**坐标相对 haystack**）
    - ``tolerance`` —— 低于这个 NCC 分数就返回 ``None``。
      **不返回"最像的"** —— 错的位置比没位置危险。
    - ``stride`` —— 滑动步长。默认 2（四倍加速，代价是可能差 1px）。
      要精确定位就设 1。

    ## 不做的事

    不做多尺度、不做旋转。界面缩放请用 ``scales`` 参数版的
    :func:`find_template_multi`，或者先把模板缩放到目标尺寸。
    """
    hits = find_template_multi(haystack, needle, region=region,
                               tolerance=tolerance, stride=stride, limit=1)
    return hits[0] if hits else None


def find_template_multi(haystack: Any, needle: Any, *,
                        region: Optional[Tuple[int, int, int, int]] = None,
                        tolerance: float = DEFAULT_THRESHOLD,
                        stride: int = 2,
                        limit: int = 0,
                        suppress: int = 0,
                        scales: Sequence[float] = (1.0,),
                        label: str = "") -> List[Match]:
    """找**所有**超过阈值的匹配，按分数降序。

    ``suppress`` 是非极大值抑制半径（像素）。不抑制的话，
    同一个目标会在邻近位置产生一串高分 —— 那对"点哪个"没用。

    ``scales`` 是缩放比序列。**注意代价**：每个 scale 都要跑一遍完整搜索。
    """
    hay, hw, hh = _gray(haystack, region)
    out: List[Match] = []

    for sc in (scales or (1.0,)):
        sc = float(sc)
        tpl_bmp = _scale_bitmap(needle, sc)
        tpl, tw, th = _to_gray_any(tpl_bmp)
        if tw <= 0 or th <= 0 or tw > hw or th > hh:
            continue
        tmean, tnorm = _prep_template(tpl, tw, th)

        step = max(1, int(stride))
        found: List[Match] = []
        for oy in range(0, hh - th + 1, step):
            for ox in range(0, hw - tw + 1, step):
                sc_val = _ncc_at(hay, hw, hh, tpl, tw, th, ox, oy,
                                 tmean, tnorm)
                if sc_val >= tolerance:
                    found.append(Match(
                        rect=(ox, oy, ox + tw, oy + th), score=sc_val,
                        scale=sc, method="template", label=label))
        out.extend(_suppress(found, max(0, int(suppress))))

    out.sort(key=lambda m: -m.score)
    return out[:limit] if limit else out


def _suppress(matches: List[Match], radius: int) -> List[Match]:
    """非极大值抑制。``radius=0`` 时只去完全重复的。"""
    if not matches:
        return []
    matches = sorted(matches, key=lambda m: -m.score)
    kept: List[Match] = []
    for m in matches:
        cx, cy = m.center
        dup = False
        for k in kept:
            kx, ky = k.center
            if abs(cx - kx) <= radius and abs(cy - ky) <= radius:
                dup = True
                break
        if not dup:
            kept.append(m)
    return kept


def _scale_bitmap(bitmap: Any, scale: float) -> Any:
    """缩放位图。``scale == 1.0`` 时原样返回。

    **只支持整数倍缩小**（用现成的 ``downsample``）——
    任意比例的插值会引入模糊，反而降低匹配率。
    """
    if abs(scale - 1.0) < 1e-9:
        return bitmap
    if scale < 1.0:
        factor = max(1, int(round(1.0 / scale)))
        fn = getattr(bitmap, "downsample", None)
        if callable(fn):
            try:
                return fn(factor)
            except Exception:      # noqa: BLE001
                return bitmap
    return bitmap


def find_color(bitmap: Any, rgb: Tuple[int, int, int], *,
               tolerance: int = 12,
               region: Optional[Tuple[int, int, int, int]] = None,
               min_pixels: int = 1,
               cluster: int = 8) -> List[Tuple[int, int]]:
    """找指定颜色的像素**簇中心**。

    ## 返回什么

    簇的中心点列表 ``[(x, y), ...]``，坐标**相对传入的图**。

    ## 为什么要聚类

    一个纯色按钮有几千个同色像素。直接返回全部，调用方没法用。
    按 ``cluster`` 半径合并成簇，返回每簇的中心 —— 那才是"按钮在哪"。

    ## 参数

    - ``tolerance`` —— 每通道允许的偏差（0~255）
    - ``min_pixels`` —— 少于这么多像素的簇丢弃（滤掉噪点）
    - ``cluster`` —— 聚类半径（像素）
    """
    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    if w <= 0 or h <= 0:
        raise VisualUnavailable("位图尺寸异常：%dx%d" % (w, h))

    if region:
        l = max(0, min(w, int(region[0])))
        t = max(0, min(h, int(region[1])))
        r = max(l, min(w, int(region[2])))
        b = max(t, min(h, int(region[3])))
    else:
        l, t, r, b = 0, 0, w, h

    want_r, want_g, want_b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    tol = max(0, int(tolerance))
    stride = int(getattr(bitmap, "stride", 0) or (w * 4))
    px = bitmap.pixels

    buckets: Dict[Tuple[int, int], List[int]] = {}
    cell = max(1, int(cluster))
    for y in range(t, b):
        srow = (h - 1 - y) * stride
        for x in range(l, r):
            o = srow + x * 4
            if (abs(px[o] - want_b) <= tol
                    and abs(px[o + 1] - want_g) <= tol
                    and abs(px[o + 2] - want_r) <= tol):
                buckets.setdefault((x // cell, y // cell), []).append(
                    (x << 16) | y)

    out: List[Tuple[int, int]] = []
    for pts in buckets.values():
        if len(pts) < max(1, int(min_pixels)):
            continue
        sx = sy = 0
        for p in pts:
            sx += (p >> 16) & 0xFFFF
            sy += p & 0xFFFF
        out.append((sx // len(pts), sy // len(pts)))
    out.sort()
    return out


def diff(a: Any, b: Any, *,
         region: Optional[Tuple[int, int, int, int]] = None) -> float:
    """两图的**平均绝对差**，0.0~1.0。

    - ``0.0`` —— 完全一样
    - ``1.0`` —— 完全相反

    ## 与阶段四 pHash 的分工

    pHash 回答"**结构**变了吗"（抗噪、抗亮度）；本函数回答
    "**像素**变了多少"（敏感、精确）。

    做"这一步有没有生效"的判断用 pHash（阶段四 verify）；
    做"变化幅度有多大"的度量用本函数。
    """
    ga, aw, ah = _gray(a, region)
    gb, bw, bh = _gray(b, region)
    if (aw, ah) != (bw, bh):
        raise VisualUnavailable(
            "两图尺寸不一致：%dx%d vs %dx%d" % (aw, ah, bw, bh))
    if not ga:
        return 0.0
    total = 0
    for i in range(len(ga)):
        d = ga[i] - gb[i]
        total += d if d >= 0 else -d
    return total / (len(ga) * 255.0)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    checks, failures = [], []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. 可用性状态自洽
    #    ⚠ 这里原本硬断言 ``is_available() is False`` —— 那是把
    #    **本机当前状态**当成代码契约。哪天注册了引擎（set_engine）
    #    或换个环境，它就假红了。
    #    改成契约式：**可用 → 无原因；不可用 → 必须给出原因**。
    #    同时 detail 里"接口占位，实现属阶段七"是过期文案 ——
    #    引擎注册在阶段八已经落地。
    _avail = is_available()
    _reason = unavailable_reason()
    _add("可用性状态自洽",
         (_avail and not _reason) or (not _avail and bool(_reason)),
         "is_available()=%s，原因%s"
         % (_avail, ("已给出" if _reason else "为空")))

    # 2. 截图能力与识别能力分开报告
    cap = capture_backend()
    _add("截图 / 引擎 / 算法三者分开报告",
         bool(cap) and isinstance(is_available(), bool)
         and algorithm_available() is True,
         "截图=%s，引擎=%s，算法=可用（三个独立维度）"
         % (cap, "已接入" if is_available() else "未接入"))

    # 3. 原因说明
    reason = unavailable_reason()
    _add("引擎不可用时有原因说明", bool(reason), reason[:56])
    _add("★ 原因里说清「算法版仍然可用」",
         ("算法" in reason) or ("find_template" in reason),
         "否则调用方会以为匹配完全不能用")

    # 4. 环境描述完整
    env = describe_environment()
    _add("环境描述字段完整",
         all(k in env for k in ("available", "reason", "install_hint",
                                "capture", "backends")),
         "%d 个候选后端已探测" % len(env.get("backends") or []))

    # 5. 提示里强调多尺度（本机 200% 缩放的硬要求）
    hint = env.get("install_hint") or ""
    _add("安装建议强调多尺度", "多尺度" in hint and "200%" in hint,
         "明确指出本机 200% 缩放，单尺度模板会失效")

    # 6. 接口抛 VisualUnavailable
    for label, fn in (("find_template_in_window",
                       lambda: find_template_in_window(1, object())),
                      ("find_icon", lambda: find_icon(1, "close")),
                      ("check_box_state",
                       lambda: check_box_state(1, (0, 0, 10, 10)))):
        try:
            fn()
            _add("%s 抛 VisualUnavailable" % label, False, "没有抛异常")
        except VisualUnavailable:
            _add("%s 抛 VisualUnavailable" % label, True,
                 "正确抛异常（可被上层捕获做降级）")
        except Exception as exc:      # noqa: BLE001
            _add("%s 抛 VisualUnavailable" % label, False,
                 "抛了 %s" % type(exc).__name__)

    # 7. 是 UnsupportedError 子类
    _add("VisualUnavailable 可被 UnsupportedError 捕获",
         issubclass(VisualUnavailable, UnsupportedError),
         "MRO 正确 → 上层通用降级 except 能抓到")

    # 8. 缺 window 时优先报能力不可用
    try:
        find_template_in_window(0, object())
    except VisualUnavailable:
        _add("window 校验顺序合理", True,
             "先报能力不可用（更根本的问题优先）")
    except ValueError:
        _add("window 校验顺序合理", False, "先报了 ValueError")
    except Exception as exc:      # noqa: BLE001
        _add("window 校验顺序合理", False, "抛了 %s" % type(exc).__name__)

    # 9. 注册引擎后可用，且 threshold 会**丢弃**低分结果
    def _fake(window, template, region, threshold, scales):
        return [
            VisualHit(label="close", window=window,
                      client_rect=(1, 2, 20, 20), score=0.95,
                      method="template"),
            VisualHit(label="close", window=window,
                      client_rect=(50, 60, 70, 80), score=0.40,
                      method="template"),
        ]

    set_engine(_fake, "fake")
    try:
        _add("注册引擎后报告可用",
             is_available() is True and engine_name() == "fake",
             "engine=%s" % engine_name())
        hits = find_template_in_window(12345, "tpl",
                                       threshold=0.8, limit=0)
        _add("低于阈值的结果被丢弃",
             len(hits) == 1 and hits[0].score == 0.95,
             "2 个候选 → 保留 %d 个（0.40 被丢）" % len(hits))
        top = find_template_in_window(12345, "tpl",
                                      threshold=0.8, limit=1)
        _add("结果按分数降序", top and top[0].score == 0.95,
             "最高分 %.2f" % (top[0].score if top else 0))
        st = check_box_state(12345, (0, 0, 20, 20))
        _add("check_box_state 无对应标签返回 None", st is None,
             "没有 checkbox:* 标签 → None（不硬猜）")
    except Exception as exc:      # noqa: BLE001
        _add("注册引擎后报告可用", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        set_engine(None)
        _add("注销引擎后恢复不可用", is_available() is False,
             "engine_name=%r" % engine_name())

    # 10. VisualHit 契约
    hit = VisualHit(label="ok", window=777, client_rect=(4, 5, 24, 25),
                    score=0.9, method="template")
    d = hit.to_dict()
    _add("VisualHit 契约完整",
         d["window"] == 777 and d["client_rect"] == [4, 5, 24, 25]
         and d["method"] == "template",
         "window + 客户区坐标 + method 都在")
    _add("VisualHit 有客户区中心点", hit.center_client == (14, 15),
         "center_client=%s" % (hit.center_client,))
    _add("VisualHit 无绝对屏幕坐标字段",
         not any("screen" in k for k in d),
         "字段: %s" % ", ".join(sorted(d)))

    # 11. 阈值默认值偏保守
    _add("默认阈值偏保守（宁缺毋滥）",
         DEFAULT_THRESHOLD >= 0.8,
         "DEFAULT_THRESHOLD=%.2f —— 视觉误报代价高于漏报" % DEFAULT_THRESHOLD)

    return {"checks": checks, "failures": failures, "ok": not failures}


def _main() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("视觉兜底（接口占位）自检:")
    res = selfcheck()
    for c in res["checks"]:
        print("  [%s] %-38s %s"
              % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
    total = len(res["checks"])
    print("vision_fallback 自检: %d/%d"
          % (total - len(res["failures"]), total))
    return 1 if res["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
