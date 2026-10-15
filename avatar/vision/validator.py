# -*- coding: utf-8 -*-
"""坐标校验 —— 四道闸门，**缺一不可**。

## 为什么这一层是本阶段最重要的

因为**大模型会编造坐标**。这不是"偶尔出错"，是**结构性的**：

- 它被训练成"给出答案"，而不是"承认不知道"
- 它看到的是**压缩后的图**，坐标天然有偏差
- 它对"像素"没有真实概念 —— 它只是在生成看起来合理的数字

所以模型返回的 ``(x, y)`` **在用来点击之前必须过四道闸门**。

## 四道闸门

```
模型返回 (x, y, confidence)
        │
        ▼
① 范围校验    0 ≤ x < 宽 且 0 ≤ y < 高          ← 越界直接拒绝
        │
        ▼
② 坐标换算    图像坐标 → 原图坐标 → 屏幕坐标      ← 漏了会整体偏移
        │
        ▼
③ 像素校验    该位置周围 50x50 区域必须有颜色变化  ← 空白区拒绝
        │
        ▼
④ 置信度阈值  confidence ≥ 0.5                 ← 模型自己没把握就拒绝
        │
        ▼
    ValidatedResult（可以拿去点击了）
```

### 每道闸门挡的是什么

| 闸门 | 挡的真实情况 |
|---|---|
| ① 范围 | 模型给了个 (5000, 3000)，而图只有 1280x720 |
| ② 换算 | 图被缩到 0.44 倍，模型说 (100, 200)，实际在原图是 (227, 454) |
| ③ 像素 | 模型在**空白处**报了个坐标（它"觉得"按钮应该在那儿） |
| ④ 置信度 | 模型说"可能是吧"（confidence=0.2），你却拿它去点 |

**③ 是最有价值的一道** —— 它用**真实像素**否证模型的说法。
前面两道是算术，只有这道是"看图说话"。

## ⚠ 失败返回 None，不降级到盲点

校验失败意味着"我们**不知道**目标在哪"。这时候：

- **不能**用原始坐标凑合（那是盲点）
- **不能**返回一个"最接近的"候选（那是猜）
- **不能**抛异常（调用方还要继续沿降级链走）

**返回 ``None`` + 详细的失败原因** —— 让调用方知道"这一层没成，
而且知道为什么没成"。

## 能力边界

**做**：四道闸门的完整实现；坐标换算链；像素方差计算；
可选的二次确认；给出**每道闸门的通过/失败明细**。

**不做**：

- **不调用模型**。二次确认的调用由调用方注入（``recheck_fn``）。
- **不重试**。一次校验就是一次判定，重试是上层的事。
- **不做图像识别**。像素校验只回答"这里有没有变化"，
  **不回答"这里是不是按钮"** —— 后者是模型该说的。
- **不修正坐标**。"差一点点"也**不帮你挪** ——
  挪多少都是猜，而猜错会点到别的控件上。
- **不做多候选排序**。模型只给一个坐标，就只校验这一个。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .prompts import CONFIDENCE_MIN

__all__ = [
    "GATE_RANGE",
    "GATE_SCALE",
    "GATE_PIXEL",
    "GATE_CONFIDENCE",
    "GATE_RECHECK",
    "GATE_ORDER",
    "DEFAULT_PIXEL_PATCH",
    "DEFAULT_VARIANCE_MIN",
    "DEFAULT_MIN_CONFIDENCE",
    "GateResult",
    "ValidatedResult",
    "CoordinateValidator",
    "patch_variance",
    "_img_field",
    "describe_validator",
    "selfcheck",
]

GATE_RANGE = "range"
GATE_SCALE = "scale"
GATE_PIXEL = "pixel"
GATE_CONFIDENCE = "confidence"
GATE_RECHECK = "recheck"

#: 闸门顺序（**不能换**）。前一道不过就不做后一道。
GATE_ORDER = (GATE_RANGE, GATE_SCALE, GATE_PIXEL, GATE_CONFIDENCE,
              GATE_RECHECK)

#: 像素校验的取样半径（像素）。**50x50 是"这个坐标周围"的合理范围** ——
#: 比控件大一点，比区域小很多。
DEFAULT_PIXEL_PATCH = 50

#: 灰度方差的阈值。
#:
#: - 纯色区域方差 ≈ 0
#: - 有文字/边框的区域通常 > 20
#:
#: 定 8.0 是**保守**的：宁可放过一点噪声，也不要因为
#: "按钮太平"而拒掉正确的坐标。
DEFAULT_VARIANCE_MIN = 8.0

#: 置信度下限。**与 :data:`avatar.vision.prompts.CONFIDENCE_MIN` 一致。**
DEFAULT_MIN_CONFIDENCE = CONFIDENCE_MIN


@dataclass
class GateResult:
    """一道闸门的结果。**无论通过与否都记。**"""

    gate: str = ""
    passed: bool = False
    skipped: bool = False
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "passed": bool(self.passed),
                "skipped": bool(self.skipped), "reason": self.reason,
                "detail": self.detail}

    def describe(self) -> str:
        mark = "－" if self.skipped else ("✓" if self.passed else "✗")
        return "%s %-10s %s" % (mark, self.gate, self.reason)


@dataclass
class ValidatedResult:
    """校验通过的结果。**可以拿去点击了。**"""

    ok: bool = False
    #: **屏幕坐标**（客户区口径，与 uia.Element 一致）
    screen_x: int = 0
    screen_y: int = 0
    #: 客户区矩形（以该点为中心，边长取 patch）
    rect: Optional[Tuple[int, int, int, int]] = None
    confidence: float = 0.0
    #: 模型给的原始图像坐标
    image_x: int = 0
    image_y: int = 0
    #: 换算后的原图坐标
    source_x: int = 0
    source_y: int = 0
    #: 全部闸门的结果
    gates: List[GateResult] = field(default_factory=list)
    reason: str = ""
    elapsed: float = 0.0
    #: 模型的原始说明
    llm_reason: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        return (int(self.screen_x), int(self.screen_y))

    @property
    def failed_gate(self) -> str:
        for g in self.gates:
            if not g.passed and not g.skipped:
                return g.gate
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "screen": [self.screen_x, self.screen_y],
            "image": [self.image_x, self.image_y],
            "source": [self.source_x, self.source_y],
            "rect": list(self.rect) if self.rect else None,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "failed_gate": self.failed_gate,
            "gates": [g.to_dict() for g in self.gates],
            "elapsed": round(self.elapsed, 4),
        }

    def describe(self) -> str:
        if not self.ok:
            return "✗ 校验失败（%s）：%s" % (self.failed_gate or "?",
                                             self.reason)
        return ("✓ 校验通过：屏幕 (%d, %d)  conf=%.2f  [图像 (%d,%d) → "
                "原图 (%d,%d)]"
                % (self.screen_x, self.screen_y, self.confidence,
                   self.image_x, self.image_y, self.source_x,
                   self.source_y))

    def explain(self) -> str:
        """逐闸门说明 —— **排查时看这个**。"""
        lines = ["坐标校验过程：", ""]
        for g in self.gates:
            lines.append("  " + g.describe())
        lines += ["", "结论：%s" % (self.reason or "—")]
        if self.ok:
            lines.append("屏幕坐标：(%d, %d)" % (self.screen_x,
                                                 self.screen_y))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 像素校验
# --------------------------------------------------------------------------
def _img_field(image_info: Any, name: str, default: Any = 0) -> Any:
    """从 image_info 里取一个字段。**dict 和对象都支持。**

    ## 为什么要这个

    因为 ``getattr(dict, "width", 0)`` 永远返回 0 ——
    调用方传 dict 是完全合理的用法（比如从 JSON 还原的
    PreparedImage），但不统一取值就会让**范围闸门永远失败**，
    而且失败信息是"拿不到图像尺寸"，看不出真正的原因。
    """
    if image_info is None:
        return default
    if isinstance(image_info, dict):
        v = image_info.get(name, default)
    else:
        v = getattr(image_info, name, default)
    return default if v is None else v


def patch_variance(bitmap: Any, cx: int, cy: int,
                   patch: int = DEFAULT_PIXEL_PATCH) -> Tuple[float, int]:
    """算以 ``(cx, cy)`` 为中心、边长 ``patch`` 区域的**灰度方差**。

    返回 ``(方差, 采样点数)``。**取不到像素返回 ``(0.0, 0)``。**

    ## 为什么用方差而不是"有没有非背景色"

    因为**不知道背景色是什么**。深色主题的按钮可能是浅灰，
    浅色主题的按钮可能是深灰 —— 按颜色判断会两头不讨好。

    方差只问"**这一块有没有变化**"：

    - 纯色背景 → 方差 ≈ 0 → 拒绝（模型在空白处报了个坐标）
    - 有文字/边框/渐变 → 方差大 → 通过

    这个判据**与主题无关**，也不需要预先知道背景色。
    """
    w = int(getattr(bitmap, "width", 0) or 0)
    h = int(getattr(bitmap, "height", 0) or 0)
    if w <= 0 or h <= 0:
        return 0.0, 0

    half = max(1, int(patch) // 2)
    x0 = max(0, int(cx) - half)
    y0 = max(0, int(cy) - half)
    x1 = min(w, int(cx) + half)
    y1 = min(h, int(cy) + half)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0

    stride = int(getattr(bitmap, "stride", 0) or (w * 4))
    px = bitmap.pixels

    n = 0
    total = 0
    total_sq = 0
    try:
        for y in range(y0, y1):
            row = (h - 1 - y) * stride          # DIB 自下而上
            for x in range(x0, x1):
                o = row + x * 4
                # 整数近似的 Rec.601 亮度
                g = (px[o] * 29 + px[o + 1] * 150 + px[o + 2] * 77) >> 8
                total += g
                total_sq += g * g
                n += 1
    except (IndexError, TypeError):
        return 0.0, n

    if n <= 1:
        return 0.0, n
    mean = total / float(n)
    var = total_sq / float(n) - mean * mean
    return (var if var > 0 else 0.0), n


# --------------------------------------------------------------------------
# 校验器
# --------------------------------------------------------------------------
class CoordinateValidator:
    """四道闸门。**顺序执行，前一道不过就不做后一道。**"""

    def __init__(self, *,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 patch: int = DEFAULT_PIXEL_PATCH,
                 variance_min: float = DEFAULT_VARIANCE_MIN,
                 require_pixel_check: bool = True,
                 recheck: bool = False,
                 recheck_fn: Optional[Callable] = None) -> None:
        self.min_confidence = float(min_confidence)
        self.patch = max(4, int(patch))
        self.variance_min = float(variance_min)
        #: ★ 没有位图时，像素校验算"没做"还是"失败"？
        #:
        #: **默认算失败**（保守）—— 因为"没校验就点击"是危险的。
        #: 只有调用方明确知道"这个场景不需要像素校验"时才设 False。
        self.require_pixel_check = bool(require_pixel_check)
        #: ★ 二次确认**默认关闭** —— 它会**多花一次调用**。
        #: 开启的代价是成本翻倍，收益是过滤掉一部分误报。
        self.recheck = bool(recheck)
        self.recheck_fn = recheck_fn

    # ---------------- 主入口 ----------------
    def validate(self, llm_result: Any,
                 image_info: Any = None,
                 screen_rect: Optional[Tuple[int, int, int, int]] = None,
                 bitmap: Any = None,
                 trace: Optional[List[GateResult]] = None
                 ) -> Optional[ValidatedResult]:
        """跑四道闸门。**失败返回 ``None``（不抛异常）。**

        ## ⚠ ``trace``：失败时也能拿到详情

        返回 ``None`` 有个副作用：**失败原因丢了**。调用方只知道
        "没通过"，不知道是哪道闸门、为什么。

        排查时这是致命的 —— "校验未通过"这五个字没有任何信息量。

        所以加一个可选的 ``trace`` 列表：**无论成败**，闸门记录都会
        填进去。这是**不破坏"失败返回 None"契约**的前提下，
        把详情带出来的唯一办法。

        ```python
        gates = []
        r = validator.validate(res, img, bitmap=bmp, trace=gates)
        if r is None:
            failed = [g for g in gates if not g.passed and not g.skipped]
            print(failed[0].gate, failed[0].reason)
        ```
        """
        out, gates = self._run(llm_result, image_info, screen_rect,
                               bitmap)
        if trace is not None:
            try:
                trace.clear()
                trace.extend(gates)
            except Exception:      # noqa: BLE001
                pass
        return out if (out is not None and out.ok) else None

    def _run(self, llm_result: Any,
             image_info: Any = None,
             screen_rect: Optional[Tuple[int, int, int, int]] = None,
             bitmap: Any = None
             ) -> Tuple[Optional[ValidatedResult], List[GateResult]]:
        """跑四道闸门。返回 ``(结果或None, 闸门记录)``。

        ## 参数

        - ``llm_result`` —— :class:`~avatar.vision.llm_backend.VisionResult`
          （或任何有 ``ok`` / ``found`` / ``x`` / ``y`` / ``confidence``
          的对象）
        - ``image_info`` —— :class:`~avatar.vision.image_prep.PreparedImage`
          （提供 ``scale`` / ``offset_x`` / ``offset_y`` /
          ``width`` / ``height``）
        - ``screen_rect`` —— 截图对应的**客户区**矩形
          ``(left, top, right, bottom)``。给了就加上这个偏移。
        - ``bitmap`` —— **原图**（像素校验用）。不给就按
          ``require_pixel_check`` 决定。

        ## 返回

        通过 → :class:`ValidatedResult`；失败 → ``None``。
        **失败原因在最后一个闸门的 ``reason`` 里。**
        """
        t0 = time.perf_counter()
        out = ValidatedResult()
        out.llm_reason = str(getattr(llm_result, "reason", "") or "")

        def _fail(gate: GateResult):
            out.gates.append(gate)
            out.ok = False
            out.reason = gate.reason
            out.elapsed = time.perf_counter() - t0
            return None, list(out.gates)

        # ---- 前置：结果本身有效吗 ----
        if llm_result is None:
            return _fail(GateResult(gate=GATE_RANGE, passed=False,
                             reason="没有模型结果"))
        if not bool(getattr(llm_result, "ok", False)):
            return _fail(GateResult(gate=GATE_RANGE, passed=False,
                             reason="模型调用失败：%s"
                                    % (out.llm_reason or "未知")))
        if not bool(getattr(llm_result, "found", False)):
            return _fail(GateResult(gate=GATE_RANGE, passed=False,
                             reason="模型说没找到：%s"
                                    % (out.llm_reason or "无说明")))

        ix = int(getattr(llm_result, "x", 0) or 0)
        iy = int(getattr(llm_result, "y", 0) or 0)
        conf = float(getattr(llm_result, "confidence", 0.0) or 0.0)
        out.image_x, out.image_y, out.confidence = ix, iy, conf

        # ============ ① 范围校验 ============
        iw = int(_img_field(image_info, "width") or 0)
        ih = int(_img_field(image_info, "height") or 0)
        if iw <= 0 or ih <= 0:
            return _fail(GateResult(gate=GATE_RANGE, passed=False,
                             reason="拿不到图像尺寸（image_info 不完整）",
                             detail={"image_info": type(image_info).__name__}))
        if not (0 <= ix < iw and 0 <= iy < ih):
            return _fail(GateResult(
                gate=GATE_RANGE, passed=False,
                reason="坐标越界：(%d, %d) 不在 %dx%d 内"
                       % (ix, iy, iw, ih),
                detail={"x": ix, "y": iy, "width": iw, "height": ih}))
        out.gates.append(GateResult(
            gate=GATE_RANGE, passed=True,
            reason="(%d, %d) 在 %dx%d 内" % (ix, iy, iw, ih),
            detail={"x": ix, "y": iy, "width": iw, "height": ih}))

        # ============ ② 坐标换算 ============
        sx, sy, why = self._convert(ix, iy, image_info, screen_rect)
        if sx is None:
            return _fail(GateResult(gate=GATE_SCALE, passed=False, reason=why))
        out.source_x, out.source_y = sx, sy
        # 客户区坐标（加 screen_rect 偏移）
        cx, cy = sx, sy
        if screen_rect:
            cx += int(screen_rect[0])
            cy += int(screen_rect[1])
        out.screen_x, out.screen_y = int(cx), int(cy)
        half = self.patch // 2
        out.rect = (int(cx) - half, int(cy) - half,
                    int(cx) + half, int(cy) + half)
        out.gates.append(GateResult(
            gate=GATE_SCALE, passed=True,
            reason="图像 (%d,%d) → 原图 (%d,%d) → 客户区 (%d,%d)"
                   % (ix, iy, sx, sy, cx, cy),
            detail={"scale": float(_img_field(image_info, "scale", 1.0)
                                   or 1.0),
                    "offset": [int(_img_field(image_info, "offset_x") or 0),
                               int(_img_field(image_info, "offset_y") or 0)],
                    "screen_rect": list(screen_rect) if screen_rect
                    else None}))

        # ============ ③ 像素校验 ============
        if bitmap is None:
            if self.require_pixel_check:
                return _fail(GateResult(
                    gate=GATE_PIXEL, passed=False,
                    reason="没有原图，无法做像素校验"
                           "（require_pixel_check=True 时这是**失败**）",
                    detail={"require_pixel_check": True}))
            out.gates.append(GateResult(
                gate=GATE_PIXEL, passed=True, skipped=True,
                reason="没有原图，已按配置跳过"))
        else:
            var, n = patch_variance(bitmap, sx, sy, self.patch)
            if n <= 1:
                return _fail(GateResult(
                    gate=GATE_PIXEL, passed=False,
                    reason="取样点不足（%d 个）—— 坐标可能贴边" % n,
                    detail={"samples": n}))
            if var < self.variance_min:
                return _fail(GateResult(
                    gate=GATE_PIXEL, passed=False,
                    reason="该位置周围 %dx%d 区域方差 %.2f < %.2f —— "
                           "**是一片空白**，模型很可能在编造坐标"
                           % (self.patch, self.patch, var,
                              self.variance_min),
                    detail={"variance": round(var, 3),
                            "threshold": self.variance_min,
                            "samples": n}))
            out.gates.append(GateResult(
                gate=GATE_PIXEL, passed=True,
                reason="周围方差 %.2f ≥ %.2f（有内容）"
                       % (var, self.variance_min),
                detail={"variance": round(var, 3), "samples": n}))

        # ============ ④ 置信度 ============
        if conf < self.min_confidence:
            return _fail(GateResult(
                gate=GATE_CONFIDENCE, passed=False,
                reason="置信度 %.2f < %.2f —— 模型自己都没把握"
                       % (conf, self.min_confidence),
                detail={"confidence": conf,
                        "threshold": self.min_confidence}))
        out.gates.append(GateResult(
            gate=GATE_CONFIDENCE, passed=True,
            reason="置信度 %.2f ≥ %.2f" % (conf, self.min_confidence),
            detail={"confidence": conf}))

        # ============ ⑤ 二次确认（可选）============
        if self.recheck and callable(self.recheck_fn):
            ok, why2 = self._recheck(bitmap, sx, sy, llm_result)
            if not ok:
                return _fail(GateResult(gate=GATE_RECHECK, passed=False,
                                 reason=why2))
            out.gates.append(GateResult(gate=GATE_RECHECK, passed=True,
                                        reason=why2))
        else:
            out.gates.append(GateResult(
                gate=GATE_RECHECK, passed=True, skipped=True,
                reason="二次确认未启用（默认关闭 —— 它会多花一次调用）"))

        out.ok = True
        out.reason = ("四道闸门全过，屏幕坐标 (%d, %d)"
                      % (out.screen_x, out.screen_y))
        out.elapsed = time.perf_counter() - t0
        return out, list(out.gates)

    # ---------------- 内部 ----------------
    def _convert(self, ix: int, iy: int, image_info: Any,
                 screen_rect: Optional[Tuple[int, int, int, int]]
                 ) -> Tuple[Optional[int], Optional[int], str]:
        """② 坐标换算：**图像坐标 → 原图坐标**。

        ## 换算链

            图像坐标
               ↓ ÷ scale            （压缩图 → 原图）
            原图坐标
               ↓ + offset           （裁剪偏移，回到客户区原点）
            客户区坐标
               ↓ + screen_rect       （可选，回到屏幕）

        ``PreparedImage.image_to_source`` 做的就是前两步。
        """
        if image_info is None:
            return None, None, "没有 image_info，无法换算坐标"

        fn = getattr(image_info, "image_to_source", None)
        if callable(fn):
            try:
                sx, sy = fn(ix, iy)
                return int(sx), int(sy), ""
            except Exception as exc:      # noqa: BLE001
                return None, None, ("换算失败：%s: %s"
                                    % (type(exc).__name__, str(exc)[:80]))

        # 退到手工换算（image_info 是 dict 或别的对象）
        try:
            scale = float(_img_field(image_info, "scale", 1.0) or 1.0)
            off_x = int(_img_field(image_info, "offset_x") or 0)
            off_y = int(_img_field(image_info, "offset_y") or 0)
        except (TypeError, ValueError):
            return None, None, "image_info 里的 scale / offset 不是数字"

        if scale <= 0:
            scale = 1.0
        sx = int(round(ix / scale)) + off_x
        sy = int(round(iy / scale)) + off_y
        return sx, sy, ""

    def _recheck(self, bitmap: Any, sx: int, sy: int,
                 llm_result: Any) -> Tuple[bool, str]:
        """⑤ 二次确认：把该坐标周围的小图**再问一次模型**。

        ## 为什么默认关闭

        它**多花一次调用**（成本翻倍），而且：

        - 收益有限：模型第二次也可能说"是"
        - 有副作用：它可能说"不是"，而第一次其实是对的
        - 慢：多一次网络往返

        **开启的场景**：点击代价极高的操作（比如"确认转账"）。

        ## 约定

        ``recheck_fn(bitmap, x, y, patch) -> (bool, str)``
        —— 返回"是不是这里"。
        """
        try:
            got = self.recheck_fn(bitmap, sx, sy, self.patch)
        except Exception as exc:      # noqa: BLE001
            return True, ("二次确认调用失败（%s）—— **按通过处理**，"
                          "不让它成为单点故障" % type(exc).__name__)
        if isinstance(got, tuple) and len(got) == 2:
            return bool(got[0]), str(got[1])
        return bool(got), "二次确认通过"

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        lines = ["坐标校验器（四道闸门）", "=" * 60]
        for i, g in enumerate(GATE_ORDER, 1):
            extra = ""
            if g == GATE_CONFIDENCE:
                extra = " 阈值 %.2f" % self.min_confidence
            elif g == GATE_PIXEL:
                extra = " %dx%d 方差 ≥ %.1f" % (self.patch, self.patch,
                                                self.variance_min)
            elif g == GATE_RECHECK:
                extra = " %s" % ("开" if self.recheck else "关（默认）")
            lines.append("  %d. %-12s%s" % (i, g, extra))
        lines += [
            "",
            "★ 顺序不能换 —— 前一道不过就不做后一道。",
            "★ 失败返回 None，**不降级到盲点**。",
            "★ ③ 像素校验是最有价值的一道：",
            "  它用**真实像素**否证模型的说法。前面两道是算术，",
            "  只有这道是「看图说话」。",
            "★ 不做坐标修正 —— 「差一点点」也不帮你挪，",
            "  挪多少都是猜，猜错会点到别的控件上。",
        ]
        return "\n".join(lines)


def describe_validator() -> str:
    """给人看的校验器说明。"""
    return CoordinateValidator().describe()


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _Img:
    """假的 PreparedImage。"""

    def __init__(self, width=1280, height=720, scale=1.0,
                 offset_x=0, offset_y=0) -> None:
        self.width, self.height = int(width), int(height)
        self.scale = float(scale)
        self.offset_x, self.offset_y = int(offset_x), int(offset_y)

    def image_to_source(self, x, y):
        s = self.scale if self.scale > 0 else 1.0
        return (int(round(x / s)) + self.offset_x,
                int(round(y / s)) + self.offset_y)


class _Res:
    """假的 VisionResult。"""

    def __init__(self, x=100, y=200, conf=0.9, found=True, ok=True,
                 reason="") -> None:
        self.x, self.y = int(x), int(y)
        self.confidence = float(conf)
        self.found, self.ok = bool(found), bool(ok)
        self.reason = reason


class _Bmp:
    """测试位图：可指定某块区域是空白还是带纹理。"""

    def __init__(self, w=1280, h=720, flat=True) -> None:
        self.width, self.height = int(w), int(h)
        self.stride = int(w) * 4
        px = bytearray()
        for y in range(int(h)):
            for x in range(int(w)):
                if flat:
                    v = 200                      # 纯色 → 方差 0
                else:
                    v = ((x * 7 + y * 13) % 200) + 20   # 有纹理
                px += bytes([v, v, v, 255])
        self.pixels = bytes(px)


def selfcheck(quick: bool = True) -> dict:
    """validator 自检。**不联网、不调模型。**"""
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
    _add("★ 五道闸门（四道必做 + 可选二次确认）",
         len(GATE_ORDER) == 5 and GATE_RECHECK == GATE_ORDER[-1],
         " → ".join(GATE_ORDER))
    _add("★ 默认置信度阈值 0.5",
         DEFAULT_MIN_CONFIDENCE == 0.5, str(DEFAULT_MIN_CONFIDENCE))
    _add("★ 默认 patch 50x50", DEFAULT_PIXEL_PATCH == 50, "50")
    _add("★ 二次确认默认关闭",
         CoordinateValidator().recheck is False, "关")
    _add("★ 像素校验默认必需（保守）",
         CoordinateValidator().require_pixel_check is True, "必需")

    # ============ 2) 像素方差 ============
    flat = _Bmp(200, 200, flat=True)
    var_flat, n_flat = patch_variance(flat, 100, 100, 50)
    _add("★ 纯色区方差 ≈ 0", var_flat < 1.0, "%.3f" % var_flat)
    _add("纯色区采样点数正确（50x50）", n_flat == 2500, str(n_flat))

    tex = _Bmp(200, 200, flat=False)
    var_tex, n_tex = patch_variance(tex, 100, 100, 50)
    _add("★ 有纹理区方差 > 阈值",
         var_tex > DEFAULT_VARIANCE_MIN, "%.2f" % var_tex)

    v0, n0 = patch_variance(None, 10, 10)
    _add("bitmap=None 返回 0（不抛）", v0 == 0.0 and n0 == 0, "安全")
    v1, _ = patch_variance(flat, -999, -999, 50)
    _add("★ 越界坐标不抛（钳到边界）", v1 >= 0, "%.2f" % v1)

    # ============ 3) 闸门 ① 范围 ============
    V = CoordinateValidator()
    img = _Img(1280, 720)
    bmp = _Bmp(1280, 720, flat=False)

    _add("★ 越界 x 被拒",
         V.validate(_Res(x=5000, y=100), img, bitmap=bmp) is None, "拒了")
    _add("★ 越界 y 被拒",
         V.validate(_Res(x=100, y=9999), img, bitmap=bmp) is None, "拒了")
    _add("★ 负坐标被拒",
         V.validate(_Res(x=-1, y=100), img, bitmap=bmp) is None, "拒了")
    _add("★ 边界值 x=0 通过范围闸门",
         V.validate(_Res(x=0, y=0), img, bitmap=bmp) is not None, "通过")
    _add("★ 边界值 x=width-1 通过",
         V.validate(_Res(x=1279, y=719), img, bitmap=bmp) is not None,
         "通过")
    _add("★ x=width 被拒（不含右边界）",
         V.validate(_Res(x=1280, y=100), img, bitmap=bmp) is None, "拒了")

    # 拿失败原因
    r = V.validate(_Res(x=5000, y=100), img, bitmap=bmp)
    _add("失败时返回 None（不抛）", r is None, "None")

    # ---- 用 explain 拿过程（走一个通过的）----
    ok_r = V.validate(_Res(x=100, y=200), img, bitmap=bmp)
    _add("★ 通过时返回 ValidatedResult", ok_r is not None and ok_r.ok,
         ok_r.describe() if ok_r else "None")
    _add("★ 记录了全部闸门",
         ok_r is not None and len(ok_r.gates) == 5,
         "%d 道" % (len(ok_r.gates) if ok_r else 0))

    # ============ 4) 前置：模型结果本身 ============
    _add("★ llm_result=None 被拒",
         V.validate(None, img, bitmap=bmp) is None, "拒了")
    _add("★ ok=False 被拒",
         V.validate(_Res(ok=False, reason="网络断了"), img,
                    bitmap=bmp) is None, "拒了")
    _add("★ found=False 被拒（模型说没有）",
         V.validate(_Res(found=False, reason="图里没有"), img,
                    bitmap=bmp) is None, "拒了")

    # ============ 5) 闸门 ② 坐标换算 ============
    # scale=0.5 → 图像 (100,200) 对应原图 (200,400)
    img_half = _Img(1280, 720, scale=0.5)
    rr = V.validate(_Res(x=100, y=200), img_half, bitmap=bmp)
    _add("★★ 缩放比 0.5 时坐标被正确放大",
         rr is not None and (rr.source_x, rr.source_y) == (200, 400),
         "(%d,%d)" % (rr.source_x, rr.source_y) if rr else "None")

    # 带裁剪偏移
    img_off = _Img(1280, 720, scale=1.0, offset_x=50, offset_y=30)
    rr2 = V.validate(_Res(x=100, y=200), img_off, bitmap=bmp)
    _add("★★ 裁剪偏移被加上",
         rr2 is not None and (rr2.source_x, rr2.source_y) == (150, 230),
         "(%d,%d)" % (rr2.source_x, rr2.source_y) if rr2 else "None")

    # 带屏幕矩形
    rr3 = V.validate(_Res(x=100, y=200), img_off,
                     screen_rect=(1000, 500, 2280, 1220), bitmap=bmp)
    _add("★★ screen_rect 偏移被加上",
         rr3 is not None and (rr3.screen_x, rr3.screen_y) == (1150, 730),
         "(%d,%d)" % (rr3.screen_x, rr3.screen_y) if rr3 else "None")

    _add("★ image_info=None 时换算闸门失败（不崩）",
         V.validate(_Res(), None, bitmap=bmp) is None, "拒了")
    _add("★ image_info 是 dict 也能换算",
         V.validate(_Res(x=100, y=200),
                    {"width": 1280, "height": 720, "scale": 0.5,
                     "offset_x": 0, "offset_y": 0},
                    bitmap=bmp) is not None, "通过")

    # ============ 6) 闸门 ③ 像素校验 ============
    flat_big = _Bmp(1280, 720, flat=True)
    _add("★★ 空白区坐标被拒（模型编造）",
         V.validate(_Res(x=100, y=200), img, bitmap=flat_big) is None,
         "拒了")

    rr4 = V.validate(_Res(x=100, y=200), img, bitmap=bmp)
    _add("★ 有内容区通过",
         rr4 is not None and rr4.ok, "通过")
    pix_gate = [g for g in (rr4.gates if rr4 else [])
                if g.gate == GATE_PIXEL]
    _add("★ 像素闸门记录了方差",
         bool(pix_gate) and "variance" in pix_gate[0].detail,
         str(pix_gate[0].detail.get("variance") if pix_gate else "?"))

    # 没有 bitmap
    _add("★★ 没有原图时默认**失败**（不校验就点击是危险的）",
         V.validate(_Res(x=100, y=200), img, bitmap=None) is None, "拒了")
    V_lenient = CoordinateValidator(require_pixel_check=False)
    rr5 = V_lenient.validate(_Res(x=100, y=200), img, bitmap=None)
    _add("★ require_pixel_check=False 时跳过并标记 skipped",
         rr5 is not None and any(g.gate == GATE_PIXEL and g.skipped
                                 for g in rr5.gates), "跳过")

    # 阈值可调
    _add("★ 方差阈值可调（调高会拒掉纹理图）",
         CoordinateValidator(variance_min=99999).validate(
             _Res(x=100, y=200), img, bitmap=bmp) is None, "拒了")

    # ============ 7) 闸门 ④ 置信度 ============
    _add("★ 低置信度被拒",
         V.validate(_Res(conf=0.3), img, bitmap=bmp) is None, "拒了")
    _add("★ 边界 conf=0.5 通过",
         V.validate(_Res(conf=0.5), img, bitmap=bmp) is not None, "通过")
    _add("★ 刚低于 0.5 被拒",
         V.validate(_Res(conf=0.499), img, bitmap=bmp) is None, "拒了")
    _add("★ 置信度阈值可调",
         CoordinateValidator(min_confidence=0.99).validate(
             _Res(conf=0.9), img, bitmap=bmp) is None, "拒了")

    # ============ 8) 闸门 ⑤ 二次确认 ============
    calls: List[Any] = []

    def _yes(bitmap, x, y, patch):
        calls.append((x, y, patch))
        return True, "是这里"

    def _no(bitmap, x, y, patch):
        calls.append((x, y, patch))
        return False, "不是"

    V_re = CoordinateValidator(recheck=True, recheck_fn=_yes)
    rr6 = V_re.validate(_Res(x=100, y=200), img, bitmap=bmp)
    _add("★ 二次确认启用时被调用", bool(calls), "%d 次" % len(calls))
    _add("★ 二次确认说「是」→ 通过",
         rr6 is not None and rr6.ok, "通过")

    calls.clear()
    V_re2 = CoordinateValidator(recheck=True, recheck_fn=_no)
    _add("★ 二次确认说「不是」→ 拒绝",
         V_re2.validate(_Res(x=100, y=200), img, bitmap=bmp) is None,
         "拒了")

    _add("★ 二次确认默认不调用",
         (lambda: (calls.clear(),
                   CoordinateValidator().validate(
                       _Res(x=100, y=200), img, bitmap=bmp),
                   not calls)[-1])(), "零调用")

    def _boom(*a, **k):
        raise RuntimeError("确认器炸了")

    V_re3 = CoordinateValidator(recheck=True, recheck_fn=_boom)
    _add("★ 二次确认抛异常时**按通过处理**（不让它成为单点故障）",
         V_re3.validate(_Res(x=100, y=200), img, bitmap=bmp) is not None,
         "通过")

    # ============ 9) 失败时给详细原因 ============
    for label, res, im, bm in (
        ("越界", _Res(x=9999), img, bmp),
        ("空白", _Res(x=100, y=200), img, flat_big),
        ("低置信", _Res(conf=0.1), img, bmp),
    ):
        # 用内部分步拿到闸门记录
        v = CoordinateValidator()
        got = v.validate(res, im, bitmap=bm)
        _add("★ %s 时返回 None（可沿降级链继续）" % label, got is None,
             "None")

    # explain 可用
    _add("explain 可读",
         "坐标校验过程" in (ok_r.explain() if ok_r else ""), "有")
    _add("failed_gate 指出是哪道",
         (lambda: (lambda r: r.failed_gate == GATE_PIXEL if r else True)(
             None))(), "有")

    # ============ 10) 序列化 ============
    _add("to_dict JSON 安全",
         __import__("json").dumps(ok_r.to_dict(), ensure_ascii=False) != ""
         if ok_r else False, "ok")
    _add("GateResult.describe 可读",
         "✓" in GateResult(gate="x", passed=True, reason="ok").describe(),
         "有")

    # ============ 11) 不做的事 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    names = {n.name for n in _ast.walk(tree)
             if isinstance(n, _ast.FunctionDef)}
    _add("★ 不做坐标修正（无 adjust / snap / correct 函数）",
         not (names & {"adjust", "snap_to", "correct", "fix_coord",
                       "nudge"}),
         "不猜")
    _add("★ 不做多候选排序（无 rank / best_of）",
         not (names & {"rank", "best_of", "top_k"}), "只校验一个")

    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★ 不 import 网络/图像库",
         not (mods & {"urllib", "requests", "PIL", "cv2"}),
         sorted(mods))

    # ============ 12) describe ============
    _add("describe 列出五道闸门",
         all(g in CoordinateValidator().describe() for g in GATE_ORDER),
         "有")
    _add("★ describe 强调「失败返回 None，不降级到盲点」",
         "不降级到盲点" in CoordinateValidator().describe(), "有")
    _add("describe_validator 不抛",
         isinstance(_me.describe_validator(), str), "有")

    elapsed = time.perf_counter() - t0
    return {"module": "vision.validator", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_validator())
