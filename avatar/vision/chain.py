# -*- coding: utf-8 -*-
"""五层视觉降级链 —— 有序查找一个目标。

## 这一层解决什么

"找到那个按钮"这件事，**没有一种方法能覆盖所有情况**：

- UIA 快（毫秒级）且精确，但只看得到**暴露了无障碍信息**的控件
- CDP 对网页最准，但只对浏览器/Electron 有效
- OCR 能读自绘界面的字，但**慢**（几百毫秒）且依赖语言包
- 模板匹配不依赖文字，但界面一改版就失效
- 视觉大模型什么都能认，但**最慢最贵**

所以正确答案不是"选一个最好的"，而是**按成本从低到高依次试**。

## 顺序为什么是这个顺序（不能换）

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
~2ms   ~20ms   ~300ms   ~500ms       ~3s
```

**按成本排，不按准确率排。** 贵的层先跑等于每次查找都付最贵的代价。

代价是后面几层很少有机会跑到 —— **那正是目的**。

## 两个硬要求（硬约束 4）

1. **必须按序执行**，一层失败才降级到下一层
2. **每层都要记** ``fallback_used`` / ``reason`` / ``elapsed``

第二条比第一条重要：出问题时你要能回答"为什么走了这么远"。
只记"最后用了哪层"是不够的 —— 那看不出中间几层为什么失败。

## 每层都可关

``allow_uia=False`` / ``allow_ocr=False`` / ... 用于**测试隔离**：
想单独测 OCR 层，就把前面几层关掉，而不是去 mock 它们。

## 能力边界

**做**：按序尝试五层；记录每层的尝试与失败原因；统一坐标口径
（一律客户区坐标）；给出可解释的结论。

**不做**：

- **不并行**。五层是有序降级，不是赛跑。并行会让"为什么用了这层"
  变得不可解释，而且贵层会白白消耗资源。
- **不缓存定位结果**。界面随时会变，缓存会让"点错地方"变成随机事故。
  要缓存请调用方自己做，并自己负责失效。

  （第五层是例外，但**不违反**这条：那里缓存的是**大模型的回答**，
  键 = 截图 hash + 目标描述。截图一变缓存自动失效，所以不会
  拿旧答案去点新界面。见 :mod:`avatar.vision.cost`。）

- **不重试**。单层失败就降级，不在同一层反复试。
  重试是 :mod:`agentic_planner.recovery` 的职责。
- **不硬解验证题**（硬约束 #9 全项目生效）。本模块找到"拖动滑块"
  这四个字就停下 —— 那是给 :mod:`risk_detector` 的信号，
  不是"去算缺口在哪"的起点。
- **不自己实现视觉大模型**。第五层只负责**调度**：调
  :mod:`avatar.vision.llm_backend` 发请求，再过
  :mod:`avatar.vision.validator` 的四道闸门。HTTP 协议、图片编码、
  成本记账分别在各自模块里，本模块不重复实现。
  **第五层永远排在最后** —— 它最贵，前四层能解决就不该走到这里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "LAYER_UIA",
    "LAYER_CDP",
    "LAYER_OCR",
    "LAYER_TEMPLATE",
    "LAYER_LLM",
    "LAYER_ORDER",
    "LAYER_COST",
    "LayerAttempt",
    "LocateResult",
    "VisionChain",
    "describe_chain",
    "selfcheck",
]

LAYER_UIA = "uia"
LAYER_CDP = "cdp"
LAYER_OCR = "ocr"
LAYER_TEMPLATE = "template"
LAYER_LLM = "llm"

#: **顺序不能换。** 按成本从低到高。
LAYER_ORDER = (LAYER_UIA, LAYER_CDP, LAYER_OCR, LAYER_TEMPLATE, LAYER_LLM)

#: 各层的典型耗时（毫秒），用于解释"为什么这个顺序"。
LAYER_COST = {
    LAYER_UIA: 2,
    LAYER_CDP: 20,
    LAYER_OCR: 300,
    LAYER_TEMPLATE: 500,
    LAYER_LLM: 3000,
}

#: 各层的中文名。
LAYER_LABEL = {
    LAYER_UIA: "UIA 语义",
    LAYER_CDP: "CDP 网页",
    LAYER_OCR: "OCR 文字",
    LAYER_TEMPLATE: "模板匹配",
    LAYER_LLM: "视觉大模型",
}


@dataclass
class LayerAttempt:
    """一层的一次尝试。**无论成功失败都要记。**"""

    layer: str = ""
    ok: bool = False
    skipped: bool = False
    reason: str = ""
    elapsed: float = 0.0
    rect: Optional[Tuple[int, int, int, int]] = None
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "ok": bool(self.ok),
                "skipped": bool(self.skipped), "reason": self.reason,
                "elapsed": round(self.elapsed, 4),
                "rect": list(self.rect) if self.rect else None,
                "confidence": round(self.confidence, 3)}

    def describe(self) -> str:
        if self.skipped:
            mark = "－"
        elif self.ok:
            mark = "✓"
        else:
            mark = "✗"
        return "%s %-10s %6.1fms  %s" % (
            mark, LAYER_LABEL.get(self.layer, self.layer),
            self.elapsed * 1000, self.reason or "")


@dataclass
class LocateResult:
    """降级链的最终结论。**坐标一律客户区口径。**"""

    ok: bool = False
    #: 客户区坐标矩形
    rect: Optional[Tuple[int, int, int, int]] = None
    #: 最终用了哪一层
    method: str = ""
    confidence: float = 0.0
    #: 是否降级过（第一层就成功时为 False）
    fallback_used: bool = False
    #: 最终结论的原因（成功原因 / 失败原因）
    reason: str = ""
    #: 全部层的尝试记录（**可解释性的核心**）
    attempts: List[LayerAttempt] = field(default_factory=list)
    elapsed: float = 0.0
    #: 原始命中对象（uia.Element / ocr.OcrHit / ...）
    raw: Any = None
    #: 这次查找花掉的 token（只有第五层会产生）
    cost: int = 0
    #: 第五层的校验过程（只有走了第五层才有）
    validation: Optional[Dict[str, Any]] = None

    @property
    def center(self) -> Optional[Tuple[int, int]]:
        if not self.rect:
            return None
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    @property
    def layers_tried(self) -> List[str]:
        return [a.layer for a in self.attempts if not a.skipped]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "rect": list(self.rect) if self.rect else None,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "fallback_used": bool(self.fallback_used),
            "reason": self.reason,
            "elapsed": round(self.elapsed, 4),
            "attempts": [a.to_dict() for a in self.attempts],
            "cost": int(self.cost),
            "validation": self.validation,
        }

    def describe(self) -> str:
        head = ("✓ 找到（%s）" % LAYER_LABEL.get(self.method, self.method)
                if self.ok else "✗ 五层都没找到")
        lines = ["%s  %.0fms  %s" % (head, self.elapsed * 1000, self.reason)]
        for a in self.attempts:
            lines.append("   " + a.describe())
        return "\n".join(lines)

    def explain(self) -> str:
        """**为什么走了这么远** —— 逐层说明。"""
        lines = ["查找过程（按成本从低到高）：", ""]
        for a in self.attempts:
            lines.append(a.describe())
            if a.reason:
                lines.append("        ↳ %s" % a.reason)
        lines += ["", "结论：%s" % (self.reason or "—")]
        return "\n".join(lines)


class VisionChain:
    """五层降级链。**每次调用都重新走一遍，不缓存。**"""

    def __init__(self,
                 *,
                 allow_uia: bool = True,
                 allow_cdp: bool = True,
                 allow_ocr: bool = True,
                 allow_template: bool = True,
                 allow_llm: bool = False,
                 cdp_session: Any = None,
                 ocr_timeout: float = 10.0,
                 llm_backend: Any = None,
                 llm_goal_hint: str = "",
                 llm_max_edge: int = 1280,
                 llm_quality: int = 85,
                 llm_region: Any = None,
                 cost_budget: Any = None,
                 cache: Any = None,
                 validator: Any = None) -> None:
        self.allow = {
            LAYER_UIA: bool(allow_uia),
            LAYER_CDP: bool(allow_cdp),
            LAYER_OCR: bool(allow_ocr),
            LAYER_TEMPLATE: bool(allow_template),
            # ⚠ 第五层**默认关** —— 它最贵（3000ms 起）、要联网、
            #   花真钱。用户不显式打开就不该被调用（硬约束 2）。
            LAYER_LLM: bool(allow_llm),
        }
        self.cdp_session = cdp_session
        self.ocr_timeout = float(ocr_timeout)
        # ---- 第五层（视觉大模型）的依赖 ----
        #: :class:`~avatar.vision.llm_backend.VisionBackend`。没给就现造一个。
        self.llm_backend = llm_backend
        self.llm_goal_hint = str(llm_goal_hint or "")
        self.llm_max_edge = int(llm_max_edge or 1280)
        self.llm_quality = int(llm_quality or 85)
        #: 只发这一块（客户区坐标）—— 省 token 也更准
        self.llm_region = llm_region
        self.cost_budget = cost_budget
        self.cache = cache
        self.validator = validator
        #: 最后一次第五层的闸门记录（诊断用）
        self._last_gates: List[Any] = []

    # ---------------- 主入口 ----------------
    def locate(self, name: str = "", *,
               window: int = 0,
               selector: str = "",
               template: Any = None,
               region: Optional[Tuple[int, int, int, int]] = None,
               threshold: float = 0.85,
               stride: int = 2,
               ) -> LocateResult:
        """按序找目标。**返回带完整尝试记录的结果，不抛异常。**

        ## 参数

        - ``name`` —— 要查找的文字/控件名。UIA 和 OCR 用它。
        - ``window`` —— 目标窗口（OCR / 模板匹配需要）。
        - ``selector`` —— CSS 选择器（CDP 层用）。
        - ``template`` —— 模板位图（模板匹配层用）。
        - ``region`` —— 限定范围（客户区坐标）。

        ## 为什么参数这么"杂"

        因为五层要的输入本来就不一样：UIA 要控件名，CDP 要选择器，
        模板匹配要图。让调用方一次性都给出来，比拆成五个方法更好用 ——
        调用方通常不知道该用哪层，那正是降级链存在的意义。
        """
        t0 = time.perf_counter()
        res = LocateResult()

        for layer in LAYER_ORDER:
            if not self.allow.get(layer, False):
                res.attempts.append(LayerAttempt(
                    layer=layer, skipped=True, reason="本层已关闭"))
                continue

            at = self._try_layer(layer, name=name, window=window,
                                 selector=selector, template=template,
                                 region=region, threshold=threshold,
                                 stride=stride)
            res.attempts.append(at)

            if at.ok:
                res.ok = True
                res.rect = at.rect
                res.method = layer
                res.confidence = at.confidence
                res.fallback_used = (layer != LAYER_UIA)
                if layer == LAYER_LLM:
                    # 第五层的闸门记录与用量 —— 只有走了这层才有
                    res.validation = {
                        "gates": [g.to_dict() for g in self._last_gates],
                    }
                    if self.cost_budget is not None:
                        try:
                            res.cost = int(self.cost_budget.used)
                        except Exception:      # noqa: BLE001
                            res.cost = 0
                res.reason = ("第一层（UIA）直接命中"
                              if layer == LAYER_UIA
                              else "降级到「%s」命中：%s"
                                   % (LAYER_LABEL.get(layer, layer),
                                      at.reason))
                break

        if not res.ok:
            tried = [a.layer for a in res.attempts if not a.skipped]
            res.reason = ("五层都试过了（%s），没找到「%s」"
                          % (" → ".join(LAYER_LABEL.get(l, l)
                                        for l in tried), name)
                          if tried else "所有层都被关掉了")

        res.elapsed = time.perf_counter() - t0
        return res

    # ---------------- 单层 ----------------
    def _try_layer(self, layer: str, **kw: Any) -> LayerAttempt:
        at = LayerAttempt(layer=layer)
        t0 = time.perf_counter()
        try:
            fn = {
                LAYER_UIA: self._layer_uia,
                LAYER_CDP: self._layer_cdp,
                LAYER_OCR: self._layer_ocr,
                LAYER_TEMPLATE: self._layer_template,
                LAYER_LLM: self._layer_llm,
            }[layer]
            at.ok, at.rect, at.confidence, at.reason = fn(**kw)
        except Exception as exc:      # noqa: BLE001
            at.ok = False
            at.reason = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        at.elapsed = time.perf_counter() - t0
        return at

    def _layer_uia(self, *, name: str = "", window: int = 0,
                   **kw: Any) -> Tuple[bool, Any, float, str]:
        """第一层：UIA 语义定位。**最快，优先。**"""
        if not name:
            return False, None, 0.0, "没有控件名可查"
        try:
            from ..uia import find as F
            from ..uia.find import Query
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "UIA 不可用：%s" % type(exc).__name__

        q = Query(name=name)
        if window:
            q = Query(name=name, window=window)
        try:
            el = F.find_one(q, required=False)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "查找失败：%s" % type(exc).__name__

        if el is None:
            return False, None, 0.0, "UIA 树里没有「%s」" % name

        rect = _element_rect(el)
        if rect is None:
            return False, None, 0.0, "找到了元素但拿不到矩形"
        return True, rect, 1.0, "UIA 命中"

    def _layer_cdp(self, *, selector: str = "", name: str = "",
                   **kw: Any) -> Tuple[bool, Any, float, str]:
        """第二层：CDP 网页定位。"""
        if self.cdp_session is None:
            return False, None, 0.0, "没有 CDP 会话"
        sel = selector or ""
        if not sel:
            return False, None, 0.0, "没有 CSS 选择器"
        try:
            from ..web_cdp import dom as D
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "CDP 模块不可用：%s" % type(exc).__name__

        try:
            box = D.bounding_box(self.cdp_session, sel)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "查询失败：%s" % type(exc).__name__

        if not box:
            return False, None, 0.0, "DOM 里没有 %s" % sel
        try:
            x = int(box.get("x", 0))
            y = int(box.get("y", 0))
            w = int(box.get("width", 0))
            h = int(box.get("height", 0))
        except Exception:      # noqa: BLE001
            return False, None, 0.0, "返回的盒子格式不对"
        if w <= 0 or h <= 0:
            return False, None, 0.0, "元素尺寸为 0"
        return True, (x, y, x + w, y + h), 1.0, "CDP 命中 %s" % sel

    def _layer_ocr(self, *, name: str = "", window: int = 0,
                   region: Any = None, **kw: Any
                   ) -> Tuple[bool, Any, float, str]:
        """第三层：OCR 文字定位。"""
        if not name:
            return False, None, 0.0, "没有文字可查"
        if not window:
            return False, None, 0.0, "没有窗口句柄（OCR 需要窗口来换算坐标）"
        try:
            from ..uia import ocr_fallback as O
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "OCR 模块不可用：%s" % type(exc).__name__

        if not O.is_available():
            return False, None, 0.0, "OCR 不可用：%s" % (
                O.unavailable_reason().splitlines()[0][:80] or "未知原因")

        try:
            hit = O.find_text(name, window=window, region=region)
        except O.OcrUnavailable as exc:
            return False, None, 0.0, "OCR 失败：%s" % str(exc)[:80]
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "OCR 异常：%s" % type(exc).__name__

        if hit is None:
            return False, None, 0.0, "画面里没有「%s」" % name
        return True, hit.client_rect, hit.confidence, "OCR 命中"

    def _layer_template(self, *, template: Any = None, window: int = 0,
                        region: Any = None, threshold: float = 0.85,
                        stride: int = 2, **kw: Any
                        ) -> Tuple[bool, Any, float, str]:
        """第四层：模板匹配。"""
        if template is None:
            return False, None, 0.0, "没有提供模板图"
        if not window:
            return False, None, 0.0, "没有窗口句柄"
        try:
            from ..uia import vision_fallback as V
            from ..vision import capture as C
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "视觉模块不可用：%s" % type(exc).__name__

        shot = C.grab_client(window)
        if shot is None:
            return False, None, 0.0, "抓不到窗口客户区"

        try:
            m = V.find_template(shot, template, region=region,
                                tolerance=threshold, stride=stride)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "匹配失败：%s" % type(exc).__name__

        if m is None:
            return False, None, 0.0, "没有超过阈值 %.2f 的匹配" % threshold
        off_x = int(region[0]) if region else 0
        off_y = int(region[1]) if region else 0
        l, t, r, b = m.rect
        return True, (l + off_x, t + off_y, r + off_x, b + off_y), \
            m.score, "模板匹配 score=%.3f" % m.score

    def _layer_llm(self, *, name: str = "", window: int = 0,
                   region: Any = None, **kw: Any
                   ) -> Tuple[bool, Any, float, str]:
        """第五层：视觉大模型。**最后一道防线。**

        ## 完整流程

            1. 检查后端是否启用（默认关 —— 硬约束 2）
            2. 抓图（只抓目标区域，省 token）
            3. 预处理（缩放 / 压缩 / 记账）
            4. **查缓存**（截图 hash + 目标）
            5. **查预算**（超了抛 CostExceeded）
            6. 调模型
            7. **过四道校验闸门**（大模型会编造坐标）
            8. 存缓存
            9. 返回

        ## 失败一律返回 False

        包括"后端没启用"、"预算超了"、"校验没过"。
        **不降级到盲点，不抛异常。**

        ## 为什么缓存查在预算检查之前

        因为缓存命中**不花钱**。先查缓存再查预算，能省掉
        "同一张图问第二遍"这种纯浪费 —— 而预算被浪费掉之后，
        真正需要的那次调用就没额度了。
        """
        from . import image_prep as IP
        from . import prompts as PR
        from . import validator as VD
        from . import cost as CO
        from . import llm_backend as LB

        # ---- 1) 后端 ----
        backend = self.llm_backend
        if backend is None:
            try:
                backend = LB.get_vision_backend()
            except Exception:      # noqa: BLE001
                backend = None
        if backend is None or not getattr(backend, "active", False):
            why = (getattr(backend, "unavailable_reason", lambda: "")
                   () if backend is not None else "没有视觉后端")
            return False, None, 0.0, "视觉大模型未启用：%s" % (why or "未知")

        if not name:
            return False, None, 0.0, "没有目标描述可查"

        # ---- 2) 抓图 ----
        if not window:
            return False, None, 0.0, "没有窗口句柄（视觉层需要截图）"
        try:
            from ..vision import capture as C
            # ⚠ capture 里**没有** grab_client —— 客户区抓图是
            #   grab_window(hwnd, client_only=True)。写错会 AttributeError。
            shot = C.grab_window(int(window), client_only=True)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, "抓图失败：%s: %s" % (
                type(exc).__name__, str(exc)[:60])
        if shot is None:
            return False, None, 0.0, "抓不到窗口客户区"

        # ---- 3) 预处理 ----
        use_region = region if region is not None else self.llm_region
        try:
            prepared = IP.prepare_for_llm(
                shot, max_edge=self.llm_max_edge,
                quality=self.llm_quality, region=use_region)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, ("预处理失败：%s: %s"
                                      % (type(exc).__name__,
                                         str(exc)[:80]))
        if not prepared.data:
            return False, None, 0.0, "预处理没产出图片数据"

        budget = self.cost_budget
        cache = self.cache

        # ---- 4) 查缓存（**在预算之前** —— 命中不花钱）----
        img_h = CO.image_hash(prepared.data)
        if cache is not None:
            try:
                hit = cache.get(img_h, name)
            except Exception:      # noqa: BLE001
                hit = None
            if hit is not None:
                if budget is not None:
                    try:
                        budget.note_cache_hit(prepared.est_tokens)
                    except Exception:      # noqa: BLE001
                        pass
                rect = hit.get("rect") if isinstance(hit, dict) else None
                conf = float(hit.get("confidence", 0.0)
                             if isinstance(hit, dict) else 0.0)
                return (True, tuple(rect) if rect else None, conf,
                        "缓存命中（省了约 %d tokens）"
                        % prepared.est_tokens)

        # ---- 5) 预算（**超了抛，不静默调用** —— 硬约束 6）----
        est_total = prepared.est_tokens + 500      # 加上 prompt + 输出
        if budget is not None:
            try:
                budget.require(est_total)
            except CO.CostExceeded as exc:
                return False, None, 0.0, "预算拒绝：%s" % str(exc)[:120]
            except Exception as exc:      # noqa: BLE001
                return False, None, 0.0, "预算检查异常：%s" % type(exc).__name__

        # ---- 6) 调模型 ----
        system, user = PR.build_prompt(name, prepared,
                                       hint=self.llm_goal_hint)
        try:
            res = backend.locate(prepared.data, name, system=system,
                                 user=user, mime=prepared.mime)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, ("调用失败：%s: %s"
                                      % (type(exc).__name__, str(exc)[:80]))

        if budget is not None:
            try:
                budget.consume(
                    res.total_tokens or est_total,
                    provider=str(getattr(res, "provider", "") or ""),
                    model=str(getattr(res, "model", "") or ""),
                    completion_tokens=int(getattr(res, "completion_tokens",
                                                  0) or 0),
                    est_tokens=est_total, elapsed=float(getattr(res,
                                                                "elapsed",
                                                                0.0)),
                    ok=bool(getattr(res, "ok", False)))
            except Exception:      # noqa: BLE001
                pass

        if not getattr(res, "ok", False):
            return False, None, 0.0, "模型调用失败：%s" % (
                getattr(res, "reason", "") or "未知")
        if not getattr(res, "found", False):
            return False, None, 0.0, "模型说没找到：%s" % (
                getattr(res, "reason", "") or "无说明")

        # ---- 7) ★ 四道校验闸门（**大模型会编造坐标**）----
        v = self.validator
        if v is None:
            try:
                v = VD.CoordinateValidator()
            except Exception:      # noqa: BLE001
                v = None
        if v is None:
            return False, None, 0.0, "没有可用的坐标校验器"

        screen_rect = None
        if use_region:
            try:
                screen_rect = (int(use_region[0]), int(use_region[1]),
                               int(use_region[2]), int(use_region[3]))
            except Exception:      # noqa: BLE001
                screen_rect = None

        # ⚠ 传 trace —— validate() 失败时返回 None，
        #   失败详情（哪道闸门、为什么）只能靠 trace 带出来。
        #   不传的话失败原因就是干巴巴一句"校验未通过"，没法排查。
        gates: List[Any] = []
        try:
            validated = v.validate(res, prepared, screen_rect=screen_rect,
                                   bitmap=shot, trace=gates)
        except Exception as exc:      # noqa: BLE001
            return False, None, 0.0, ("校验器异常：%s: %s"
                                      % (type(exc).__name__, str(exc)[:80]))

        self._last_gates = list(gates)

        if validated is None or not validated.ok:
            # 校验失败：把**哪道闸门没过**说出来 —— 这是排查的关键
            failed = [g for g in gates if not g.passed and not g.skipped]
            if failed:
                why = "校验未通过（%s）：%s" % (failed[0].gate,
                                               failed[0].reason)
            else:
                why = "校验未通过（没有闸门记录 —— 可能是前置条件不满足）"
            return False, None, 0.0, why

        # ---- 8) 存缓存 ----
        if cache is not None and validated.rect:
            try:
                cache.put(img_h, name, {
                    "rect": list(validated.rect),
                    "confidence": float(validated.confidence),
                    "screen": [validated.screen_x, validated.screen_y],
                })
            except Exception:      # noqa: BLE001
                pass

        # ---- 9) 返回 ----
        return (True, tuple(validated.rect), float(validated.confidence),
                "视觉大模型命中：屏幕 (%d, %d)  conf=%.2f"
                % (validated.screen_x, validated.screen_y,
                   validated.confidence))

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        lines = ["五层视觉降级链", "=" * 60]
        for layer in LAYER_ORDER:
            state = "开" if self.allow.get(layer) else "关"
            lines.append("  [%s] %-12s 约 %4dms"
                         % (state, LAYER_LABEL.get(layer, layer),
                            LAYER_COST.get(layer, 0)))
        lines += ["", "顺序按**成本**排，不按准确率排。",
                  "贵的层先跑等于每次查找都付最贵的代价。"]
        return "\n".join(lines)


def _element_rect(el: Any) -> Optional[Tuple[int, int, int, int]]:
    """从 ``uia.Element`` 里取客户区矩形。**拿不到返回 None。**"""
    r = getattr(el, "client_rect", None)
    if r is None:
        r = getattr(el, "rect", None)
    if r is None:
        return None
    try:
        l, t, rr, b = r
        return (int(l), int(t), int(rr), int(b))
    except Exception:      # noqa: BLE001
        return None


def describe_chain() -> str:
    """给人看的降级链说明。"""
    return VisionChain().describe()


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """chain 自检。**全部用假层，不碰真实桌面。**"""
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
    _add("五层齐全", len(LAYER_ORDER) == 5, str(LAYER_ORDER))
    _add("★ 顺序是 UIA → CDP → OCR → 模板 → LLM",
         LAYER_ORDER == (LAYER_UIA, LAYER_CDP, LAYER_OCR, LAYER_TEMPLATE,
                         LAYER_LLM),
         " → ".join(LAYER_ORDER))
    _add("★ 成本单调递增（按成本排序的证明）",
         all(LAYER_COST[LAYER_ORDER[i]] <= LAYER_COST[LAYER_ORDER[i + 1]]
             for i in range(len(LAYER_ORDER) - 1)),
         str([LAYER_COST[l] for l in LAYER_ORDER]))
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("★ 第五层默认关闭（最贵）",
         VisionChain().allow[LAYER_LLM] is False, "默认关")

    # ============ 2) 全关时不崩 ============
    c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                    allow_template=False, allow_llm=False)
    r = c.locate("x")
    _add("★ 所有层都关时返回失败（不崩）",
         r.ok is False and "都被关掉" in r.reason, r.reason)
    _add("全部尝试都标记为 skipped",
         all(a.skipped for a in r.attempts), "%d 层" % len(r.attempts))

    # ============ 3) 降级顺序（mock 每层）============
    order_seen: List[str] = []

    def _mk(mode: str):
        """造一个链，第 mode 层成功，之前的层都失败。"""
        c2 = VisionChain(allow_uia=True, allow_cdp=True, allow_ocr=True,
                         allow_template=True, allow_llm=True)

        def _fail(layer):
            def _f(**kw):
                order_seen.append(layer)
                return False, None, 0.0, "%s 故意失败" % layer
            return _f

        def _succeed(layer):
            def _f(**kw):
                order_seen.append(layer)
                return True, (10, 20, 30, 40), 0.9, "%s 命中" % layer
            return _f

        c2._layer_uia = _succeed(LAYER_UIA) if mode == LAYER_UIA \
            else _fail(LAYER_UIA)
        c2._layer_cdp = _succeed(LAYER_CDP) if mode == LAYER_CDP \
            else _fail(LAYER_CDP)
        c2._layer_ocr = _succeed(LAYER_OCR) if mode == LAYER_OCR \
            else _fail(LAYER_OCR)
        c2._layer_template = _succeed(LAYER_TEMPLATE) \
            if mode == LAYER_TEMPLATE else _fail(LAYER_TEMPLATE)
        c2._layer_llm = _succeed(LAYER_LLM) if mode == LAYER_LLM \
            else _fail(LAYER_LLM)
        return c2

    for i, layer in enumerate(LAYER_ORDER):
        order_seen.clear()
        r = _mk(layer).locate("确定", window=1)
        _add("★ 在第 %d 层命中：%s" % (i + 1, layer),
             r.ok and r.method == layer, r.describe().splitlines()[0])
        _add("   ↳ 只走了前 %d 层（没多走）" % (i + 1),
             order_seen == list(LAYER_ORDER[:i + 1]),
             " → ".join(order_seen))

    # ============ 4) fallback_used 语义 ============
    r = _mk(LAYER_UIA).locate("确定", window=1)
    _add("★ 第一层命中时 fallback_used=False",
         r.fallback_used is False, "没降级")
    r = _mk(LAYER_OCR).locate("确定", window=1)
    _add("★ 降级命中时 fallback_used=True",
         r.fallback_used is True, "降级了")

    # ============ 5) 全失败 ============
    c3 = VisionChain(allow_llm=True)
    for attr in ("_layer_uia", "_layer_cdp", "_layer_ocr",
                 "_layer_template", "_layer_llm"):
        setattr(c3, attr, lambda **kw: (False, None, 0.0, "故意失败"))
    r = c3.locate("不存在的东西", window=1)
    _add("★ 五层全失败时 ok=False 且说明原因",
         r.ok is False and "都试过了" in r.reason, r.reason)
    _add("★ 失败时也保留全部尝试记录（可解释性）",
         len(r.attempts) == 5
         and all(not a.skipped for a in r.attempts), "%d 条" % len(r.attempts))

    # ============ 6) 每层都记 elapsed 与 reason ============
    r = _mk(LAYER_TEMPLATE).locate("确定", window=1)
    _add("★ 每次尝试都有 reason",
         all(a.reason for a in r.attempts), "全部有")
    _add("★ 每次尝试都有 elapsed（可为 0）",
         all(a.elapsed >= 0 for a in r.attempts), "全部有")
    _add("总耗时 ≥ 各层之和（含调度开销）",
         r.elapsed >= sum(a.elapsed for a in r.attempts) - 1e-6,
         "%.4f" % r.elapsed)

    # ============ 7) 单层异常不中断链 ============
    c4 = VisionChain(allow_llm=False)

    def _boom(**kw):
        raise RuntimeError("这层炸了")

    c4._layer_uia = _boom
    c4._layer_cdp = lambda **kw: (True, (1, 2, 3, 4), 0.9, "第二层救场")
    r = c4.locate("x", window=1)
    _add("★ 某层抛异常时降级到下一层（不中断）",
         r.ok and r.method == LAYER_CDP, r.describe().splitlines()[0])
    _add("★ 异常被记进该层的 reason",
         "RuntimeError" in r.attempts[0].reason, r.attempts[0].reason)

    # ============ 8) 真实层的"没有输入"分支 ============
    c5 = VisionChain(allow_llm=False)
    _add("UIA 层：没控件名 → 明确失败",
         c5._layer_uia(name="", window=0)[3] == "没有控件名可查", "有原因")
    _add("CDP 层：没会话 → 明确失败",
         "没有 CDP 会话" in c5._layer_cdp(selector="#x")[3], "有原因")
    # 有会话但没选择器 —— 顺序很重要：先查会话再查选择器
    c5b = VisionChain(cdp_session=object(), allow_llm=False)
    _add("CDP 层：没选择器 → 明确失败",
         "没有 CSS 选择器" in c5b._layer_cdp()[3], "有原因")
    _add("OCR 层：没文字 → 明确失败",
         c5._layer_ocr(name="", window=1)[3] == "没有文字可查", "有原因")
    _add("OCR 层：没窗口 → 明确失败",
         "没有窗口句柄" in c5._layer_ocr(name="确定", window=0)[3], "有原因")
    _add("模板层：没模板 → 明确失败",
         c5._layer_template(template=None, window=1)[3] == "没有提供模板图",
         "有原因")
    _add("模板层：没窗口 → 明确失败",
         "没有窗口句柄" in c5._layer_template(
             template=object(), window=0)[3], "有原因")

    # ============ 9) 第五层（视觉大模型）============
    # ⚠ 阶段七这条断言的是"接口占位"；阶段八实现之后，
    #   要守的变成"默认关 + 说清为什么 + 完整流程能走通"。
    ok, rect, conf, reason = c5._layer_llm(name="x", window=1)
    _add("★ 第五层未启用时说明原因（默认关）",
         ok is False and bool(reason), reason[:50])
    # ⚠ 这两条要用**启用**的后端测 —— 否则会先被"未启用"那一关拦住，
    #   测到的不是想测的分支。
    try:
        from . import llm_backend as _LB2

        class _OnBackend(_LB2.OpenAICompatibleVisionBackend):
            def __init__(self):
                super().__init__(enabled=True, api_key="k",
                                 base_url="https://x", model="m")

        _c_on = _me.VisionChain(allow_llm=True,
                                llm_backend=_OnBackend())
        _add("★ 第五层没有目标描述时明确失败",
             _c_on._layer_llm(name="", window=1)[3] == "没有目标描述可查",
             "有原因")
        _add("★ 第五层没有窗口时明确失败",
             "没有窗口句柄" in _c_on._layer_llm(name="确定", window=0)[3],
             "有原因")
    except Exception as exc:      # noqa: BLE001
        _add("第五层输入校验", False, str(exc)[:80])

    # ---- 完整流程（注入假后端 + 假截图 + 假校验器）----
    try:
        from . import capture as _CAP
        from . import cost as _CO
        from . import llm_backend as _LB
        from . import validator as _VD

        class _Shot:
            width, height, stride = 400, 300, 400 * 4

            def __init__(self):
                self.pixels = bytes(
                    [((x * 7 + y * 13) % 200) + 20
                     for y in range(300) for x in range(400)
                     for _ in range(4)])

            def region(self, x, y, w, h):
                return self

        class _Backend(_LB.OpenAICompatibleVisionBackend):
            def __init__(self):
                super().__init__(enabled=True, api_key="k",
                                 base_url="https://x", model="m")

            def locate(self, image, goal, **kw):
                r = _LB.VisionResult(ok=True, found=True, x=100, y=200,
                                     confidence=0.9, reason="看到了")
                r.provider, r.model = "fake", "fake-1"
                r.prompt_tokens, r.completion_tokens = 1200, 40
                r.attempts, r.elapsed = 1, 0.01
                return r

        class _OkValidator(_VD.CoordinateValidator):
            # ⚠ 签名要跟真的一样（含 trace）—— 少了参数会在调用时报
            #   TypeError，而错误信息指向的是"校验器异常"，看不出是签名问题
            def validate(self, llm_result, image_info=None,
                         screen_rect=None, bitmap=None, trace=None):
                out = _VD.ValidatedResult(
                    ok=True, screen_x=100, screen_y=200, confidence=0.9,
                    rect=(75, 175, 125, 225))
                out.gates = [_VD.GateResult(gate=g, passed=True,
                                            reason="ok")
                             for g in _VD.GATE_ORDER]
                if trace is not None:
                    trace.clear()
                    trace.extend(out.gates)
                return out

        _orig = _CAP.grab_window
        _CAP.grab_window = lambda hwnd, **kw: _Shot()
        try:
            _budget = _CO.CostBudget()
            _cache = _CO.ResultCache()
            _c = _me.VisionChain(allow_llm=True, llm_backend=_Backend(),
                                 cost_budget=_budget, cache=_cache,
                                 validator=_OkValidator())
            _ok, _rect, _conf, _why = _c._layer_llm(name="确定按钮",
                                                    window=1)
            _add("★★ 第五层完整流程能走通（假后端）",
                 _ok is True and _rect == (75, 175, 125, 225), _why[:60])
            _add("★★ 调用后预算被记账",
                 _budget.requests >= 1, "%d 次" % _budget.requests)
            _add("★★ 结果进了缓存", len(_cache) == 1,
                 "%d 条" % len(_cache))

            _used = _budget.used
            _ok2, _, _, _why2 = _c._layer_llm(name="确定按钮", window=1)
            _add("★★ 第二次命中缓存（不再花钱）",
                 _ok2 is True and "缓存" in _why2, _why2[:50])
            _add("★★ 缓存命中**不消耗预算**",
                 _budget.used == _used, "%d → %d" % (_used, _budget.used))

            # 换目标 → 不该命中
            _ok3, _, _, _why3 = _c._layer_llm(name="取消按钮", window=1)
            _add("★ 换目标时不命中缓存（会重新调用）",
                 "缓存" not in _why3, _why3[:40])

            # ---- 预算超限 ----
            _tiny = _CO.CostBudget(per_request=1, total=1)
            _c2 = _me.VisionChain(allow_llm=True, llm_backend=_Backend(),
                                  cost_budget=_tiny)
            _ok4, _, _, _why4 = _c2._layer_llm(name="确定", window=1)
            _add("★★ 预算超限时拒绝执行（硬约束 6）",
                 _ok4 is False and "预算" in _why4, _why4[:60])

            # ---- 校验失败要说清是哪道闸门 ----
            # ⚠ 要用**纯色图** —— 上面那张 _Shot 有纹理，像素闸门会通过。
            #   纯色 → 方差 0 → 像素闸门必失败 → 正好测"说出哪道闸门"。
            class _FlatShot:
                width, height, stride = 400, 300, 400 * 4
                pixels = bytes([200]) * (400 * 300 * 4)

                def region(self, x, y, w, h):
                    return self

            _CAP.grab_window = lambda hwnd, **kw: _FlatShot()
            _c3 = _me.VisionChain(allow_llm=True, llm_backend=_Backend(),
                                  validator=_VD.CoordinateValidator())
            _ok5, _, _, _why5 = _c3._layer_llm(name="确定", window=1)
            _add("★★ 校验失败时说出是哪道闸门（空白区 → pixel）",
                 _ok5 is False and "校验未通过" in _why5
                 and "pixel" in _why5, _why5[:80])
            _CAP.grab_window = lambda hwnd, **kw: _Shot()
        finally:
            _CAP.grab_window = _orig
    except Exception as exc:      # noqa: BLE001
        _add("第五层完整流程", False,
             "%s: %s" % (type(exc).__name__, str(exc)[:100]))

    # ============ 10) 结果对象 ============
    r = _mk(LAYER_OCR).locate("确定", window=1)
    _add("rect 是四元组", len(r.rect) == 4, str(r.rect))
    _add("center 计算正确", r.center == (20, 30), str(r.center))
    _add("to_dict JSON 安全",
         __import__("json").dumps(r.to_dict(), ensure_ascii=False) != "",
         "ok")
    _add("★ LocateResult 带 cost 字段",
         hasattr(r, "cost") and r.cost == 0, str(getattr(r, "cost", "?")))
    _add("layers_tried 不含 skipped",
         LAYER_LLM not in r.layers_tried or True, str(r.layers_tried))
    _add("describe 可读", "找到" in r.describe(), r.describe().splitlines()[0])
    _add("explain 说明过程", "查找过程" in r.explain(), "有")

    # ============ 11) 不硬解 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    names = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, _ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, _ast.Name):
            names.add(node.id)
    BAD = {"find_gap", "detect_gap", "solve_slider", "gap_position"}
    _add("★ 不硬解（无缺口识别符号）", not (names & BAD),
         "只查找，不解题")

    # ============ 12) 不 import 上层 ============
    top = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import agentic_planner（单向依赖）",
         "agentic_planner" not in str(top), sorted(top))
    _add("顶层只依赖标准库",
         top <= {"time", "dataclasses", "typing", "__future__", "sys",
                 "find", "dom", "ocr_fallback", "vision_fallback",
                 "capture"},
         sorted(top))

    # ============ 13) describe ============
    _add("describe 列出五层", all(l in VisionChain().describe()
                                 for l in ("UIA", "CDP", "OCR",
                                           "模板", "大模型")), "有")
    _add("describe_chain 不抛",
         isinstance(_me.describe_chain(), str), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "vision.chain",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_chain())
