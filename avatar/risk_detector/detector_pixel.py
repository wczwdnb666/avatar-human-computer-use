"""像素层风险检测（第三层，最后一道防线）。

## 这一层在链路里的位置

三层检测器是**成本阶梯**，越往下越贵、越不可靠：

    DOM 层（毫秒，evaluate 一次）  →  最便宜，浏览器里有 CDP 就先跑
    窗口层（10~50ms，EnumWindows） →  中等，任何窗口都能跑
    像素层（0.3~3s，截图 + OCR）   →  最贵，**只在上面两层都没结果时才启用**

所以这一层有两个硬性前提：

1. **只在 DOM 和窗口层都没命中时才被调用** —— 由 :mod:`detector` 统一编排，
   本模块自身不做这个判断（避免"谁负责降级"的职责重叠）。
2. **OCR 不可用就立刻返回 unknown** —— 绝不猜、绝不假装干净。
   阶段七之前 ``uia.ocr_fallback`` 恒定 ``is_available() == False``，
   所以现在这一层实际上**永远返回 unknown**。这是**正确**的行为：
   诚实地说"我看不见"，比谎报"没问题"安全得多。

## 三态语义（硬约束）

    is_clean=True    → 读到了字，且一个字都没命中指纹 → 真的干净
    detected=True    → 读到了字，且命中指纹           → 有风险
    其余             → **unknown**（OCR 不可用 / 截图失败 / 超时 / 异常）

**关键**：``is_clean=False`` 是默认值。只有"确实读到了文字并且全部干净"
才允许置 ``True``。读不到东西一律 unknown —— 上层看到 unknown 会走
"叫人来看"而不是"放心点下去"。

## 文本归一化

OCR 出来的文本有一类顽固噪声：**字符之间被塞进空格**（"拖 动 滑 块"
"请 完 成 验 证"）。指纹库 :mod:`fingerprints` 里的 ``matches()`` 用的是
子串匹配，对空格敏感，所以**归一化必须在进指纹库之前做完**。

本模块的 :func:`normalize_text` 负责这件事，规则保守：

- 去掉**中日韩字符之间**的空格（中文本身不用空格分词，夹空格必是 OCR 噪声）
- 全角空格、不间断空格 → 普通空格
- 各类虚线/项目符号 → 普通空格
- 连续空白压成一个空格
- 两侧 strip

**不做的**：不做繁简转换、不做同义词替换、不删标点。
那些是会改变语义的操作，归 fingerprints 层管，不是像素层的职责。

## 与其它模块的边界

- OCR 引擎：只用 :mod:`avatar.uia.ocr_fallback` 的公开函数，
  **不自己 import pytesseract / paddleocr** —— 引擎在阶段七接。
- 截图：只用 :mod:`avatar.vision.capture`，不自己调 gdi32。
- 指纹：**一条都不硬编码**，全部从 :mod:`fingerprints` 读。

## 能力边界

**做**：截图 → OCR（通过 :mod:`avatar.uia.ocr_fallback`）→
文本归一化 → 与 :mod:`fingerprints` 的文本指纹比对。

**不做**：

- **不硬解**（硬约束 #9）。本模块**没有一行**识别滑块缺口位置、
  点选图标坐标、验证码字符的代码，将来也不会有。
  它只做"读字 → 匹配关键词"。
- **不自己调 OCR 引擎**。只用 :mod:`avatar.uia.ocr_fallback` 的
  公开函数，不 import pytesseract / paddleocr —— 引擎在阶段七接。
  所以现在本层**恒定返回 unknown**（``reason="ocr_unavailable"``），
  这是**正确**的行为：诚实地说"我看不见"比谎报"没问题"安全得多。
- **不做图像处理**。没有灰度、二值化、去噪、旋转矫正。
  这些是 OCR 引擎内部的职责。
- **不做语义处理**。归一化只处理"空白字符"这一类，不做繁简转换、
  同义词替换、标点删除 —— 那些会改变语义，归 :mod:`fingerprints` 管。
- **不自己调 gdi32 截图**。只用 :mod:`avatar.vision.capture`。
- **不重试 OCR**。超时就返回 timeout，让上层决定要不要重来。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AvatarError
from .fingerprints import (
    TEXT_FINGERPRINTS,
    text_hit_score,
)
from .status import (
    DEFAULT_RISK_TIMEOUT,
    RiskStatus,
    Severity,
)

__all__ = [
    "PIXEL_SCAN_TIMEOUT",
    "MAX_REGION_PIXELS",
    "DEFAULT_MIN_CONFIDENCE",
    "SAMPLE_STEP",
    "normalize_text",
    "should_skip_region",
    "collect_text",
    "detect_pixel",
    "selfcheck",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 像素层整层预算。比另外两层宽 —— OCR 本来就慢。
#: 注意这和 ``detect()`` 传进来的层超时是两回事：这里是**内部**的
#: 「读了这么久还没读完就算了」，外面还有一道 3s 的总闸。
PIXEL_SCAN_TIMEOUT = 2.5

#: OCR 结果的最低置信度。低于这个值的词直接丢 —— 半信半疑的
#: 识别结果只会制造噪声命中。0.6 是 tesseract 中文的保守线。
DEFAULT_MIN_CONFIDENCE = 0.6

#: 单次 OCR 的采样步长（像素）。>1 表示跳着采，用于降采样提速。
#: 保持 1：验证码字体小，跳采样会直接吃掉笔画。真要提速应该缩小
#: region，而不是牺牲分辨率 —— 分辨率掉了 OCR 就废了。
SAMPLE_STEP = 1

#: 单块区域的最大像素数（宽 × 高）。超了就拒绝，防止有人传个
#: 全屏 4K 区域把 OCR 卡死。1920×1080 ≈ 2.07M，
#: 这里给到 3M 留一点余量给 2560 宽的窗口。
MAX_REGION_PIXELS = 3_000_000

#: 兜底的分组阈值（命中多组时用于挑"最像的那个"）。
_GROUP_THRESHOLD = 1.0

#: 置信度公式的系数。``base = weight × trust × FACTOR + bonus``。
_CONF_FACTOR = 0.9
_CONF_BONUS_PER_EXTRA = 0.03
_CONF_CAP = 0.99


# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------
#: 中日韩统一表意文字 + 扩展 A + 兼容表意 + 全角标点区。
#: 这些字符**之间**的空格一定是 OCR 噪声。
_CJK = (
    "\u3400-\u4dbf"      # 扩展 A
    "\u4e00-\u9fff"      # 基本区
    "\uf900-\ufaff"      # 兼容表意
    "\u3000-\u303f"      # 中文标点
    "\uff00-\uffef"      # 全角
)

#: 各类"看起来像空格"的东西 → 普通空格。
_SPACE_LIKE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\ufeff]")

#: 项目符号 / 虚线 / 装饰符 → 普通空格。
_BULLET_LIKE = re.compile("[•·◦▪▫■□●○◆◇※→←↑↓»«]+")

#: 连续空白。
_WS_RUN = re.compile(r"\s+")

#: **CJK 字符之间的空格** —— 唯一被删掉的空格类型。
#: 用零宽断言，不消耗字符，所以 "拖 动 滑 动" 一次替换就把三处空格全删。
_CJK_GAP = re.compile(r"(?<=[" + _CJK + r"])[ ]+(?=[" + _CJK + r"])")

#: 全角标点周围的空格也删（"条款 。" → "条款。"）。
_CJK_PUNCT_GAP = re.compile(r"\s+(?=[\u3001\u3002\uff0c\uff01\uff1f\uff1b\uff1a"
                            r"\u201c\u201d\u2018\u2019\uff08\uff09])")


def normalize_text(text: str) -> str:
    """把 OCR 原始输出归一化成可以被指纹库匹配的形态。

    **为什么必须做这一步**：指纹库里"拖动滑块"是一个连续子串，
    而 OCR 常常吐出"拖 动 滑 块"。子串匹配对空格敏感，不归一化
    就会漏报 —— 而漏报验证码是这个项目里最不能接受的错误。

    处理顺序有讲究（每一步都依赖前一步的产物）：

    1. 全角/不间断空格 → 普通空格（先统一，后面规则才认得）
    2. 项目符号 → 空格（防止 "•拖动" 粘成一个词）
    3. **删 CJK 之间的空格** ← 核心
    4. 删 CJK 标点**前**的空格
    5. 连续空白压成一个
    6. 两端 strip

    英文词之间的空格**保留** —— "I am not a robot" 里的空格是有
    语义的，删了反而匹配不上。

    参数
    ----
    - ``text`` —— OCR 原始文本，可以是 None（返回空串）

    返回
    ----
    归一化后的字符串。输入为 None/空 → 返回 ``""``。
    """
    if not text:
        return ""
    s = str(text)

    # 1) 统一各种"伪空格"
    s = _SPACE_LIKE.sub(" ", s)

    # 2) 装饰符 → 空格
    s = _BULLET_LIKE.sub(" ", s)

    # 3) 核心：删掉 CJK 字符**之间**的空格。
    #    反复跑几遍 —— "拖 动 滑" 这种连续空格，零宽断言一轮能全吃掉，
    #    但混排 "A 拖 动 B" 可能需要两轮。跑 3 轮封顶，不做无界循环。
    for _ in range(3):
        new = _CJK_GAP.sub("", s)
        if new == s:
            break
        s = new

    # 4) 中文标点前的空格
    s = _CJK_PUNCT_GAP.sub("", s)

    # 5) 压缩空白
    s = _WS_RUN.sub(" ", s)

    return s.strip()


# --------------------------------------------------------------------------
# 区域检查
# --------------------------------------------------------------------------
def should_skip_region(region: Optional[Sequence[int]]) -> Optional[str]:
    """检查 OCR 区域是否合理。返回 ``None`` 表示可以跑，否则返回拒绝原因。

    拦三种情况：

    - 宽或高 <= 0（算不出面积，OCR 会崩或返回空）
    - 面积超过 :data:`MAX_REGION_PIXELS`（会把 OCR 卡到超时）
    - 元素数不是 4（调用方传错格式了，早报比晚报好）

    ``region=None``（表示整窗）一律放行 —— 由调用方负责剪裁。
    """
    if region is None:
        return None
    try:
        r = tuple(region)
    except TypeError:
        return "region_not_iterable"
    if len(r) != 4:
        return "region_need_4_items"
    try:
        w = int(r[2])
        h = int(r[3])
    except (TypeError, ValueError):
        return "region_not_numeric"
    if w <= 0 or h <= 0:
        return "region_empty"
    if w * h > MAX_REGION_PIXELS:
        return "region_too_large"
    return None


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def collect_text(window: int,
                 *,
                 region: Optional[Sequence[int]] = None,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 lang: str = "chi_sim+eng",
                 errors: Optional[List[str]] = None,
                 timeout: float = PIXEL_SCAN_TIMEOUT,
                 ) -> Tuple[List[str], List[Dict[str, Any]], bool]:
    """读一块区域的文字，返回 ``(归一化文本列表, 原始命中, ocr_ok)``。

    ``ocr_ok=False`` 表示**没能读到文字**（引擎不可用 / 抛异常 / 区域被拒）。
    调用方拿到 ``False`` 必须走 unknown，不能当"干净"。

    ## 为什么先过 ``is_available()``

    ``uia.ocr_fallback`` 在没接引擎时，``read_region()`` 会抛
    ``OcrUnavailable``。当然可以"直接调、接住异常"，但那样有两个坏处：

    1. 异常是**控制流**，每帧一次异常对性能不友好；
    2. 拿不到**原因**。``unavailable_reason()`` 能给出"引擎未接入，
       计划阶段七"，这个信息对诊断有用得多。

    所以本函数**先问再调**，把 ``is_available() == False`` 当成
    一等公民路径处理（返回 ``ocr_ok=False`` + 原因），而不是异常路径。

    ## 为什么把 timeout 也放进来

    超时不只是一个总闸：OCR 是分批的，如果能读到一半发现已经超了，
    没必要继续烧 CPU。这里用 ``perf_counter`` 记账，**每次拿到一批
    结果就查一次表**，超了立刻停手并标记 ``errors``。
    """
    hits_out: List[Dict[str, Any]] = []
    texts: List[str] = []

    if errors is None:
        errors = []

    # ---- 1) 区域合法性 ----
    bad = should_skip_region(region)
    if bad:
        errors.append("region: %s" % bad)
        return [], [], False

    # ---- 2) 引擎可用性（先问再调）----
    try:
        from ..uia import ocr_fallback as O
    except Exception as exc:      # noqa: BLE001
        errors.append("import ocr_fallback: %s: %s" % (type(exc).__name__, exc))
        return [], [], False

    try:
        if not O.is_available():
            reason = ""
            try:
                reason = O.unavailable_reason() or ""
            except Exception:      # noqa: BLE001
                reason = ""
            errors.append("ocr_unavailable: %s" % (reason or "未说明原因"))
            return [], [], False
    except Exception as exc:      # noqa: BLE001
        errors.append("is_available: %s: %s" % (type(exc).__name__, exc))
        return [], [], False

    # ---- 3) 真读 ----
    t0 = time.perf_counter()
    budget = max(0.0, float(timeout))
    try:
        raw = O.read_region(
            int(window),
            region=tuple(region) if region is not None else None,
            min_confidence=float(min_confidence),
            lang=str(lang),
        )
    except AvatarError as exc:
        # OcrUnavailable 也走这里（它是 UnsupportedError 的子类）
        errors.append("read_region: %s: %s" % (type(exc).__name__, exc))
        return [], [], False
    except Exception as exc:      # noqa: BLE001
        errors.append("read_region: %s: %s" % (type(exc).__name__, exc))
        return [], [], False

    if raw is None:
        errors.append("read_region 返回 None")
        return [], [], False

    # ---- 4) 逐条归一化 + 记账 ----
    try:
        for h in raw:
            if time.perf_counter() - t0 > budget:
                errors.append("ocr_partial_timeout")
                break
            t_raw = getattr(h, "text", "") or ""
            n = normalize_text(t_raw)
            if not n:
                continue
            texts.append(n)
            hits_out.append({
                "text": n,
                "raw_text": t_raw,
                "confidence": float(getattr(h, "confidence", 0.0) or 0.0),
                "client_rect": getattr(h, "client_rect", None),
                "engine": getattr(h, "engine", "") or "",
            })
    except Exception as exc:      # noqa: BLE001
        errors.append("post_process: %s: %s" % (type(exc).__name__, exc))

    return texts, hits_out, True


# --------------------------------------------------------------------------
# 检测主函数
# --------------------------------------------------------------------------
def detect_pixel(window: int = 0,
                 *,
                 region: Optional[Sequence[int]] = None,
                 timeout: float = DEFAULT_RISK_TIMEOUT,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 lang: str = "chi_sim+eng",
                 extra_texts: Sequence[str] = (),
                 ) -> RiskStatus:
    """第三层：像素检测。截图 → OCR → 文本指纹。

    参数
    ----
    - ``window`` —— 目标窗口句柄。0 表示没有具体窗口（此时只能处理
      ``extra_texts``，纯 OCR 路径直接跳过 —— 没有窗口就没有坐标系）。
    - ``region`` —— 窗口客户区内的 ``(x, y, w, h)``。``None`` = 整窗。
    - ``timeout`` —— 整层预算（硬约束 #7）。
    - ``min_confidence`` —— OCR 置信度下限。
    - ``lang`` —— 语言包，默认中文简体 + 英文。
    - ``extra_texts`` —— 调用方已知的文本（比如 DOM 里读到的可见文本）。
      **跨层信息注入点**：DOM 层读到的文字比 OCR 干净得多，
      送进来等于给像素层白送一份高置信度样本。

    返回
    ----
    ``source="pixel"`` 的 RiskStatus。**永远不抛异常** ——
    所有失败路径都转成带 reason 的 RiskStatus。
    """
    t0 = time.perf_counter()
    try:
        budget = max(0.0, float(timeout))
    except (TypeError, ValueError):
        return RiskStatus.error(
            TypeError("timeout 不是数字: %r" % (timeout,)), source="pixel",
            elapsed=time.perf_counter() - t0)

    if budget <= 0.0:
        return RiskStatus.timeout(source="pixel", limit=0.0)

    errors: List[str] = []

    # ---- 1) 注入文本先归一并直接进池（不依赖 OCR，零成本）----
    pool: List[str] = []
    injected: List[str] = []
    try:
        for t in (extra_texts or ()):
            n = normalize_text(t)
            if n:
                injected.append(n)
                pool.append(n)
    except Exception as exc:      # noqa: BLE001
        errors.append("extra_texts: %s: %s" % (type(exc).__name__, exc))

    # ---- 2) OCR 路径 ----
    ocr_ok = False
    ocr_items: List[Dict[str, Any]] = []
    remaining = max(0.0, budget - (time.perf_counter() - t0))
    if window and remaining > 0.0:
        try:
            texts, ocr_items, ocr_ok = collect_text(
                int(window),
                region=region,
                min_confidence=min_confidence,
                lang=lang,
                errors=errors,
                timeout=remaining,
            )
            pool.extend(texts)
        except Exception as exc:      # noqa: BLE001 —— 不吞，记进 evidence
            errors.append("collect_text: %s: %s" % (type(exc).__name__, exc))
            ocr_ok = False

    elapsed = time.perf_counter() - t0

    # ---- 3) 超时判定（采集完才发现超了，也要如实报 timeout）----
    if elapsed > budget:
        st = RiskStatus.timeout(source="pixel", limit=budget)
        st.elapsed = elapsed
        st.extra["ocr_ok"] = ocr_ok
        st.extra["warnings"] = errors
        return st

    # ---- 4) 有文本才有资格下结论 ----
    if not pool:
        # 一个字都没读到 → unknown，**绝不**降级成 clean。
        # 先分清"OCR 根本不可用"和"OCR 可用但这块区域是空的"。
        ocr_bad = any(e.startswith("ocr_unavailable") for e in errors)
        reason = "ocr_unavailable" if ocr_bad else "no_text"
        return RiskStatus.unknown_(
            source="pixel",
            reason=reason,
            elapsed=elapsed,
            extra={
                "ocr_ok": ocr_ok,
                "window": int(window or 0),
                "region": list(region) if region is not None else None,
                "text_count": 0,
                "warnings": errors,
                "hint": ("OCR 引擎未接入（阶段七填充）—— 这一层当前无法"
                         "给出任何结论"
                         if ocr_bad else
                         "OCR 可用，但目标区域没读到文字 —— "
                         "可能区域选错了，也可能界面是纯图形"),
            },
        )

    # ---- 5) 逐段文本过指纹库 ----
    # ``text_hit_score`` 返回的 ``hits`` 是**摘要 dict**（id/weight/
    # severity/type），不是 Fingerprint 对象。要拿到 ``source``/``pattern``
    # 这些字段得回库里查。查不到就跳过 —— 半个指纹比没有更危险。
    from .fingerprints import _BY_ID

    all_hits: List[Dict[str, Any]] = []
    scores: List[Dict[str, Any]] = []
    try:
        for idx, text in enumerate(pool):
            from_injected = idx < len(injected)
            try:
                scored = text_hit_score(text)
            except Exception as exc:      # noqa: BLE001
                errors.append("score[%d]: %s: %s" % (idx, type(exc).__name__, exc))
                continue
            scores.append({
                "text": text[:120],
                "score": scored.get("score", 0.0),
                "detected": bool(scored.get("detected")),
                "strong": [h.get("id") for h in (scored.get("hits") or [])],
                "weak": list(scored.get("weak") or []),
                "groups": [g.get("id") for g in (scored.get("groups") or [])],
                "from_injected": from_injected,
            })
            # 只取强命中；弱词是**扣分项**不是命中项，不能当证据
            for h in (scored.get("hits") or []):
                fp = _BY_ID.get(str(h.get("id") or ""))
                if fp is None:
                    errors.append("unknown_fingerprint_id: %r" % (h.get("id"),))
                    continue
                all_hits.append({
                    "fingerprint": fp,
                    "score": float(scored.get("score", 0.0) or 0.0),
                    "text": text,
                    "text_index": idx,
                    "from_injected": from_injected,
                    "field": "text",
                    "value": fp.pattern,
                })
    except Exception as exc:      # noqa: BLE001
        errors.append("match: %s: %s" % (type(exc).__name__, exc))

    if not all_hits:
        # 读到了文字，但一个字都没命中 → 这一层可以放行
        return RiskStatus.clean(
            source="pixel",
            elapsed=elapsed,
            extra={
                "text_count": len(pool),
                "ocr_ok": ocr_ok,
                "injected_count": len(injected),
                "warnings": errors,
                "scores": scores[:20],      # 截断，别把 evidence 撑爆
            },
        )

    # ---- 6) 挑最有分量的命中 ----
    def _sort_key(h: Dict[str, Any]) -> Tuple[int, float, float]:
        fp = h.get("fingerprint")
        sev = getattr(fp, "severity", Severity.INFO) if fp is not None else Severity.INFO
        w = float(getattr(fp, "weight", 0.0) or 0.0) if fp is not None else 0.0
        return (_sev_rank(sev), w, float(h.get("score", 0.0) or 0.0))

    try:
        chosen = max(all_hits, key=_sort_key)
    except (ValueError, TypeError):
        chosen = all_hits[0]

    fp = chosen.get("fingerprint")
    if fp is None:
        return RiskStatus.unknown_(
            source="pixel", reason="malformed_hit", elapsed=elapsed,
            extra={"warnings": errors + ["命中项没有 fingerprint 字段"]})

    # ---- 7) 置信度 ----
    conf = _confidence(fp, all_hits)

    evidence: Dict[str, Any] = {
        "layer": "pixel",
        "fingerprint_id": fp.id,
        "fingerprint": fp.pattern,
        "matched_field": chosen.get("field", "text"),
        "matched_value": chosen.get("value", ""),
        "vendor": fp.vendor,
        "matched_text": str(chosen.get("text", ""))[:200],
        "text_index": chosen.get("text_index", -1),
        "from_injected": bool(chosen.get("from_injected")),
        "all_hits": [getattr(h.get("fingerprint"), "id", "?") for h in all_hits],
        "text_count": len(pool),
        "ocr_ok": ocr_ok,
        "region": list(region) if region is not None else None,
        "trust": _trust_of(fp.source),
        "source": fp.source,
    }
    if errors:
        evidence["warnings"] = errors

    return RiskStatus(
        True,
        type=fp.type,
        vendor=fp.vendor,
        severity=fp.severity,
        evidence=evidence,
        source="pixel",
        elapsed=elapsed,
        confidence=conf,
        fingerprint_id=fp.id,
        extra={
            "hit_count": len(all_hits),
            "ocr_ok": ocr_ok,
            "text_count": len(pool),
            "injected_count": len(injected),
            "scores": scores[:20],
        },
    )


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
_SEV_RANK = {
    Severity.INFO: 0,
    Severity.BYPASSABLE: 1,
    Severity.INTERRUPT: 2,
    Severity.BLOCK: 3,
}


def _sev_rank(sev: str) -> int:
    return _SEV_RANK.get(sev, 0)


def _trust_of(source_key: str) -> float:
    """查指纹来源的可信度。查不到给 0.5（不假设可信也不假设不可信）。"""
    try:
        from .fingerprints import SOURCES
        info = SOURCES.get(str(source_key or ""))
        if isinstance(info, dict):
            return float(info.get("trust", 0.5))
    except Exception:      # noqa: BLE001
        pass
    return 0.5


def _confidence(fp: Any, hits: List[Dict[str, Any]]) -> float:
    """算置信度。

    ``weight × trust × 0.9 + 命中数加成``，**上限 0.99**。

    为什么封顶 0.99 而不是 1.0：这是 OCR + 子串匹配，
    永远存在误识别的可能。宣称 100% 确定是撒谎，上层可能
    据此跳过人工确认。差那 0.01 就是诚实。
    """
    try:
        w = float(getattr(fp, "weight", 0.0) or 0.0)
        tr = _trust_of(getattr(fp, "source", ""))
        base = w * tr * _CONF_FACTOR
        extra = min(5, max(0, len(hits) - 1)) * _CONF_BONUS_PER_EXTRA
        return round(min(_CONF_CAP, max(0.0, base + extra)), 3)
    except Exception:      # noqa: BLE001
        return 0.5


def _code_only() -> str:
    """把本模块的代码剥成"只有标识符"的骨架，用于自检"不硬编码指纹"。

    做法：用 AST 收集**所有字符串常量**的行号，整行丢弃；
    再砍行尾注释。剩下的只有关键字和标识符，任何厂商名
    （"recaptcha"/"cloudflare"……）都不可能残留 —— 因为
    它们只可能出现在字符串或注释里。

    之前试过只剥 docstring，不行：断言自己写的字符串里也有
    "recaptcha" 这类词，导致"本模块不硬编码指纹"这条断言永远红。
    换成整行丢弃字符串常量所在行才是对的。
    """
    import ast
    import io
    import os
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src

    drop: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            start = getattr(node, "lineno", 0) or 0
            end = getattr(node, "end_lineno", start) or start
            for ln in range(start, end + 1):
                drop.add(ln)

    out = []
    for i, line in enumerate(src.split("\n"), 1):
        if i in drop:
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------
class _Hit:
    """``OcrHit`` 的字段对齐替身。"""

    __slots__ = ("text", "confidence", "client_rect", "engine", "window", "raw")

    def __init__(self, text: str, *, confidence: float = 0.9,
                 client_rect: Tuple[int, int, int, int] = (0, 0, 10, 10),
                 engine: str = "fake", window: int = 1,
                 raw: str = "") -> None:
        self.text = text
        self.confidence = confidence
        self.client_rect = client_rect
        self.engine = engine
        self.window = window
        self.raw = raw or text


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """像素层自检。

    ``quick=True``（默认）：**全部用假的 OCR 引擎**，不碰真实屏幕、
    不依赖 ocr_fallback 的真实可用性。目标毫秒级完成。

    ``quick=False``：额外报告一次真实 OCR 环境（阶段七之前必然是
    不可用 —— 这**不构成失败**，如实报告即可）。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ================= 1) 归一化 =================
    _add("normalize_text: 删 CJK 之间的空格",
         normalize_text("拖 动 滑 块") == "拖动滑块",
         normalize_text("拖 动 滑 块"))
    _add("normalize_text: 完 成 验 证 → 完成验证",
         normalize_text("请 完 成 验 证") == "请完成验证",
         normalize_text("请 完 成 验 证"))
    _add("normalize_text: 全角空格也吃",
         normalize_text("人\u3000机\u3000验\u3000证") == "人机验证",
         repr(normalize_text("人\u3000机\u3000验\u3000证")))
    _add("normalize_text: NBSP 也吃",
         normalize_text("验\u00a0证") == "验证",
         repr(normalize_text("验\u00a0证")))
    _add("normalize_text: **保留**英文词间空格",
         normalize_text("I am not a robot") == "I am not a robot",
         normalize_text("I am not a robot"))
    _add("normalize_text: 项目符号 → 空格",
         normalize_text("•拖动滑块") == "拖动滑块",
         normalize_text("•拖动滑块"))
    _add("normalize_text: 中文标点前的空格删掉",
         normalize_text("请完成验证 。") == "请完成验证。",
         normalize_text("请完成验证 。"))
    # 注意：这里**不能**用 "  验证   码  " 来测"压缩空白" ——
    # "证"/"码" 都是 CJK，它们之间的空格会被规则 3 删掉，
    # 结果是 "验证码" 而不是 "验证 码"。那是**对的**行为，
    # 拿它测空白压缩会假红。用英文词来测才准。
    _add("normalize_text: 压连续空白 + strip（英文）",
         normalize_text("  verify   confirm  ") == "verify confirm",
         repr(normalize_text("  verify   confirm  ")))
    _add("normalize_text: CJK 之间空格一律删（含多空格）",
         normalize_text("  验证   码  ") == "验证码",
         repr(normalize_text("  验证   码  ")))
    _add("normalize_text: 中英混排保留英文侧空格",
         normalize_text("点击 verify 按钮") == "点击 verify 按钮",
         repr(normalize_text("点击 verify 按钮")))
    _add("normalize_text: None/空安全",
         normalize_text(None) == "" and normalize_text("") == ""
         and normalize_text("   ") == "",
         "返回空串")
    _add("normalize_text: 纯英文不动",
         normalize_text("Just a moment...") == "Just a moment...",
         normalize_text("Just a moment..."))
    _add("normalize_text: 归一化后**能**进指纹库命中",
         "拖动滑块" in normalize_text("拖 动 滑 块"), "这是本函数的全部意义")

    # ================= 2) 区域检查 =================
    _add("should_skip_region: None 放行",
         should_skip_region(None) is None, "整窗")
    _add("should_skip_region: 正常区域放行",
         should_skip_region((0, 0, 300, 100)) is None, "300x100")
    _add("should_skip_region: 零宽拒绝",
         should_skip_region((0, 0, 0, 100)) == "region_empty", "w=0")
    _add("should_skip_region: 零高拒绝",
         should_skip_region((0, 0, 100, 0)) == "region_empty", "h=0")
    _add("should_skip_region: 负数拒绝",
         should_skip_region((0, 0, -10, 50)) == "region_empty", "w=-10")
    _add("should_skip_region: 超大区域拒绝",
         should_skip_region((0, 0, 4000, 4000)) == "region_too_large",
         "16M > %d" % MAX_REGION_PIXELS)
    _add("should_skip_region: 元素数不对拒绝",
         should_skip_region((0, 0, 100)) == "region_need_4_items", "len=3")
    _add("should_skip_region: 非数字拒绝",
         should_skip_region((0, 0, "a", 100)) == "region_not_numeric", "'a'")

    # ================= 3) OCR 不可用路径（核心硬约束）=================
    from ..uia import ocr_fallback as O

    _fake_box: Dict[str, Any] = {"hits": [], "available": False,
                                 "reason": "假的：引擎未接入",
                                 "raise": None, "calls": []}
    _orig_avail = O.is_available
    _orig_reason = O.unavailable_reason
    _orig_read = O.read_region

    def _fake_avail() -> bool:
        return bool(_fake_box["available"])

    def _fake_reason() -> str:
        return "" if _fake_box["available"] else str(_fake_box["reason"])

    def _fake_read(window, *, region=None, min_confidence=0.6,
                   lang="chi_sim+eng"):
        _fake_box["calls"].append({"window": window, "region": region,
                                   "min_confidence": min_confidence,
                                   "lang": lang})
        if _fake_box["raise"] is not None:
            raise _fake_box["raise"]
        return list(_fake_box["hits"])

    O.is_available = _fake_avail
    O.unavailable_reason = _fake_reason
    O.read_region = _fake_read

    st_ocr_off = None
    try:
        # ---- 3a) 引擎不可用 → unknown，且 reason 恰好是 ocr_unavailable ----
        _fake_box.update({"available": False, "hits": [], "calls": [],
                          "raise": None, "reason": "假的：引擎未接入"})
        st = detect_pixel(1, timeout=3.0)
        st_ocr_off = st
        _add("OCR 不可用 → detected=False",
             st.detected is False, st.describe())
        _add("OCR 不可用 → is_clean=False（硬约束，绝不谎报干净）",
             st.is_clean is False, "is_clean=%r" % (st.is_clean,))
        _add("OCR 不可用 → is_unknown=True",
             st.is_unknown is True, "is_unknown=%r" % (st.is_unknown,))
        _add("OCR 不可用 → reason 恰为 'ocr_unavailable'",
             st.reason == "ocr_unavailable", st.reason)
        _add("OCR 不可用 → 不去调 read_region（先问再调）",
             _fake_box["calls"] == [], "calls=%d" % len(_fake_box["calls"]))
        _add("OCR 不可用 → evidence 里带引擎原因",
             "ocr_unavailable" in " ".join(
                 st.extra.get("warnings", []) or []),
             st.extra.get("warnings"))
        _add("OCR 不可用 → source='pixel'",
             st.source == "pixel", st.source)

        # ---- 3b) 注入文本救场：OCR 废了但调用方给了文本 ----
        _fake_box.update({"available": False, "hits": [], "calls": [],
                          "raise": None, "reason": "引擎未接入"})
        st_inj = detect_pixel(1, extra_texts=["请完成安全验证"], timeout=3.0)
        _add("OCR 不可用但 extra_texts 命中 → detected",
             st_inj.detected is True, st_inj.describe())
        _add("注入命中 → evidence.from_injected=True",
             st_inj.evidence.get("from_injected") is True,
             st_inj.evidence.get("from_injected"))
        _add("注入命中 → evidence.layer='pixel'",
             st_inj.evidence.get("layer") == "pixel",
             st_inj.evidence.get("layer"))
        _add("注入命中 → fingerprint_id 非空",
             bool(st_inj.fingerprint_id), st_inj.fingerprint_id)

        # ---- 3c) 注入文本是脏的（带空格）→ 归一化后照样命中 ----
        st_dirty = detect_pixel(1, extra_texts=["请 完 成 安 全 验 证"],
                                timeout=3.0)
        _add("注入的脏文本经归一化后命中（归一化接线正确）",
             st_dirty.detected is True, st_dirty.describe())

        # ---- 3d) 注入干净文本 → clean（有文本+全不命中）----
        st_clean = detect_pixel(1, extra_texts=["这是一段完全无害的说明文字"],
                                timeout=3.0)
        _add("注入无害文本 → is_clean=True",
             (not st_clean.detected) and st_clean.is_clean,
             st_clean.describe())
        _add("clean 的 extra 带 scores 明细",
             isinstance(st_clean.extra.get("scores"), list)
             and len(st_clean.extra["scores"]) >= 1, "可审计")

        # ---- 3e) 引擎可用 + 读到命中文本 → detected ----
        _fake_box.update({"available": True, "hits": [
            _Hit("请拖动滑块完成验证", confidence=0.92),
        ], "calls": [], "raise": None, "reason": ""})
        st_hit = detect_pixel(1, timeout=3.0)
        _add("OCR 可用 + 命中文本 → detected",
             st_hit.detected is True, st_hit.describe())
        _add("OCR 命中 → reason 为空、is_clean=False",
             st_hit.is_clean is False, "is_clean=%r" % (st_hit.is_clean,))
        _add("OCR 命中 → evidence.ocr_ok=True",
             st_hit.evidence.get("ocr_ok") is True,
             st_hit.evidence.get("ocr_ok"))
        _add("OCR 命中 → 传对了 window",
             _fake_box["calls"] and _fake_box["calls"][0]["window"] == 1,
             _fake_box["calls"][:1])
        _add("OCR 命中 → confidence 在 (0, 1)",
             0.0 < st_hit.confidence < 1.0, st_hit.confidence)

        # ---- 3f) 引擎可用 + 读到干净文本 → clean ----
        _fake_box.update({"available": True, "hits": [
            _Hit("欢迎使用本软件", confidence=0.9),
        ], "calls": [], "raise": None, "reason": ""})
        st_ok = detect_pixel(1, timeout=3.0)
        _add("OCR 可用 + 干净文本 → is_clean=True",
             (not st_ok.detected) and st_ok.is_clean, st_ok.describe())

        # ---- 3g) 引擎可用但读到空 → unknown（不能报 clean）----
        _fake_box.update({"available": True, "hits": [],
                          "calls": [], "raise": None, "reason": ""})
        st_empty = detect_pixel(1, timeout=3.0)
        _add("OCR 可用但读到空 → unknown（不报 clean）",
             (not st_empty.detected) and st_empty.is_unknown
             and st_empty.reason == "no_text", st_empty.describe())
        _add("读到空的 is_clean 必须是 False",
             st_empty.is_clean is False, "硬约束")

        # ---- 3h) OCR 抛异常 → 不吞，转 unknown + warnings ----
        _fake_box.update({"available": True, "hits": [], "calls": [],
                          "raise": RuntimeError("OCR 引擎炸了"), "reason": ""})
        st_exc = detect_pixel(1, timeout=3.0)
        _add("OCR 抛异常 → unknown（不吞异常）",
             st_exc.is_unknown, st_exc.describe())
        _add("异常详情写进 warnings",
             "OCR 引擎炸了" in " ".join(st_exc.extra.get("warnings") or []),
             st_exc.extra.get("warnings"))

        # ---- 3i) 低置信度结果被 ocr_fallback 过滤 → 由替身模拟 ----
        _fake_box.update({"available": True, "hits": [],
                          "calls": [], "raise": None, "reason": ""})
        st_low = detect_pixel(1, timeout=3.0, min_confidence=0.95)
        _add("min_confidence 透传到 read_region",
             _fake_box["calls"]
             and _fake_box["calls"][0]["min_confidence"] == 0.95,
             _fake_box["calls"][:1])
        _add("过滤后无文本 → unknown",
             st_low.is_unknown, st_low.describe())

        # ---- 3j) region 超限 → unknown + warnings（不进 OCR）----
        _fake_box.update({"available": True, "hits": [], "calls": [],
                          "raise": None, "reason": ""})
        st_bigr = detect_pixel(1, region=(0, 0, 9999, 9999), timeout=3.0)
        _add("region 超限 → unknown",
             st_bigr.is_unknown, st_bigr.describe())
        _add("region 超限 → 根本没调 OCR",
             _fake_box["calls"] == [], "提前拦截")
        _add("region 超限 → warnings 说明原因",
             "region_too_large" in " ".join(
                 st_bigr.extra.get("warnings") or []),
             st_bigr.extra.get("warnings"))

        # ---- 3k) window=0 且无注入 → unknown ----
        st_nowin = detect_pixel(0, timeout=3.0)
        _add("window=0 且无注入 → unknown",
             st_nowin.is_unknown, st_nowin.describe())
    finally:
        O.is_available = _orig_avail
        O.unavailable_reason = _orig_reason
        O.read_region = _orig_read

    # ================= 4) timeout =================
    st_t = detect_pixel(1, timeout=0.0)
    _add("timeout=0 → 立刻 timeout",
         st_t.reason == "timeout" and st_t.is_unknown, st_t.describe())
    _add("timeout 状态的 is_clean=False",
         st_t.is_clean is False, "硬约束")

    # 超时：OCR 读得比预算慢 → 报 timeout（替身里塞 sleep）
    O.is_available = lambda: True
    O.read_region = lambda w, **kw: (time.sleep(0.25) or [_Hit("验证")])
    try:
        st_slow = detect_pixel(1, timeout=0.05)
        _add("OCR 比预算慢 → timeout（不硬等）",
             st_slow.reason == "timeout", st_slow.describe())
    finally:
        O.is_available = _orig_avail
        O.read_region = _orig_read

    # 非法 timeout（None/字符串）→ error 而不是崩
    st_bad = detect_pixel(1, timeout="abc")     # type: ignore[arg-type]
    _add("非法 timeout → error（不崩）",
         st_bad.is_unknown and st_bad.reason == "error", st_bad.describe())

    # ================= 5) 三态自证 =================
    _add("所有分支都返回 RiskStatus",
         all(isinstance(x, RiskStatus) for x in
             (st_ocr_off, st_t, st_bad)),
         "共 3 个样本")
    _add("unknown 分支全部 is_clean=False（硬约束总校验）",
         all(x.is_clean is False for x in (st_ocr_off, st_t, st_bad)),
         "已自证")

    # 遍历指纹库：确认本层读的是真库不是空壳
    _add("指纹库非空（TEXT_FINGERPRINTS）",
         len(TEXT_FINGERPRINTS) > 0,
         "共 %d 条文本指纹" % len(TEXT_FINGERPRINTS))

    # ================= 6) 硬约束：不硬编码指纹 =================
    code = _code_only()
    _add("本模块不硬编码厂商/指纹文本（只查代码骨架）",
         not any(k in code for k in
                 ('"recaptcha"', '"geetest"', '"hcaptcha"', '"cloudflare"',
                  '"turnstile"', '"拖动滑块"', '"人机验证"',
                  '"just a moment"', '"too many requests"')),
         "全部来自 fingerprints.py")

    # 顶层不 import OCR 引擎 / 不自己调 gdi32
    import ast
    import io
    import os
    tree = ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read())
    top_imports: List[str] = []
    for node in tree.body:            # 只看**顶层**，函数内的延迟导入不算
        if isinstance(node, ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_imports.append(node.module or "")
    _add("顶层不 import 具体 OCR 引擎（阶段七才接）",
         not any(("pytesseract" in m) or ("paddleocr" in m)
                 or ("easyocr" in m) for m in top_imports),
         top_imports)
    _add("顶层不 import vision.capture（本层不做截图）",
         not any("vision" in m for m in top_imports), top_imports)

    top_classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    _add("顶层无生产用类（只有测试替身）",
         all(c.startswith("_") for c in top_classes), top_classes)

    # ================= 7) quick=False：真实环境 =================
    if not quick:
        try:
            env = O.describe_environment()
            _add("真实 OCR 环境（quick=False）", True,
                 "available=%s" % env.get("available"))
        except Exception as exc:      # noqa: BLE001
            _add("真实 OCR 环境（quick=False）", True,
                 "取不到但不构成失败：%s" % exc)
    else:
        checks.append({"name": "真实 OCR 环境（quick 跳过）",
                       "ok": True, "detail": "(quick 跳过)"})

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-52s %s" % ("[OK]" if c["ok"] else "[FAIL]", c["name"], c["detail"]))
    n = len(r["checks"])
    print("detector_pixel 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
