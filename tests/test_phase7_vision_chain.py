# -*- coding: utf-8 -*-
"""阶段七 · 五层视觉降级链（``vision/chain``）。

## 这个文件在守什么

降级链的价值全在**顺序**和**可解释性**上：

1. **必须按成本从低到高**（UIA → CDP → OCR → 模板 → 大模型）
2. **每层都要记** `fallback_used` / `reason` / `elapsed`
3. **某层失败才降级**，不是并行赛跑

这里用**注入的假层**测这些 —— 不碰真实桌面。
"""
from __future__ import annotations

import pytest

from avatar.vision.chain import (
    LAYER_CDP,
    LAYER_COST,
    LAYER_LLM,
    LAYER_OCR,
    LAYER_ORDER,
    LAYER_TEMPLATE,
    LAYER_UIA,
    VisionChain,
)


def _chain_with(succeed_at: str, *, seen=None):
    """造一个链：``succeed_at`` 那层成功，之前的都失败。"""
    c = VisionChain(allow_llm=True)
    log = seen if seen is not None else []

    def _fail(layer):
        def _f(**kw):
            log.append(layer)
            return False, None, 0.0, "%s 故意失败" % layer
        return _f

    def _ok(layer):
        def _f(**kw):
            log.append(layer)
            return True, (10, 20, 30, 40), 0.9, "%s 命中" % layer
        return _f

    for layer, attr in ((LAYER_UIA, "_layer_uia"), (LAYER_CDP, "_layer_cdp"),
                        (LAYER_OCR, "_layer_ocr"),
                        (LAYER_TEMPLATE, "_layer_template"),
                        (LAYER_LLM, "_layer_llm")):
        setattr(c, attr, _ok(layer) if layer == succeed_at else _fail(layer))
    return c, log


class TestContract:
    def test_docstring_has_boundary(self):
        assert "能力边界" in (VisionChain.__module__ and
                              __import__("avatar.vision.chain",
                                         fromlist=["x"]).__doc__ or "")

    def test_five_layers(self):
        assert len(LAYER_ORDER) == 5

    def test_order_is_by_cost(self):
        """★ 顺序按**成本**排，不按准确率。"""
        costs = [LAYER_COST[l] for l in LAYER_ORDER]
        assert costs == sorted(costs), costs

    def test_order_exact(self):
        assert LAYER_ORDER == (LAYER_UIA, LAYER_CDP, LAYER_OCR,
                               LAYER_TEMPLATE, LAYER_LLM)

    def test_llm_default_off(self):
        """★ 第五层最贵，默认关。"""
        assert VisionChain().allow[LAYER_LLM] is False


class TestDowngradeOrder:
    """★ 每层都测一遍：只走到该层，不往下多走。"""

    @pytest.mark.parametrize("idx,layer", list(enumerate(LAYER_ORDER)))
    def test_stops_at_success_layer(self, idx, layer):
        c, seen = _chain_with(layer)
        r = c.locate("确定", window=1)
        assert r.ok and r.method == layer
        assert seen == list(LAYER_ORDER[:idx + 1]), seen

    def test_first_layer_no_fallback(self):
        c, _ = _chain_with(LAYER_UIA)
        assert c.locate("x", window=1).fallback_used is False

    def test_later_layer_sets_fallback(self):
        c, _ = _chain_with(LAYER_OCR)
        assert c.locate("x", window=1).fallback_used is True

    def test_all_fail(self):
        c = VisionChain(allow_llm=True)
        for attr in ("_layer_uia", "_layer_cdp", "_layer_ocr",
                     "_layer_template", "_layer_llm"):
            setattr(c, attr, lambda **kw: (False, None, 0.0, "故意失败"))
        r = c.locate("找不到的东西", window=1)
        assert r.ok is False
        assert "都试过了" in r.reason
        assert len(r.attempts) == 5


class TestAttemptRecording:
    def test_every_attempt_has_reason(self):
        c, _ = _chain_with(LAYER_TEMPLATE)
        r = c.locate("x", window=1)
        assert all(a.reason for a in r.attempts)

    def test_every_attempt_has_elapsed(self):
        c, _ = _chain_with(LAYER_TEMPLATE)
        r = c.locate("x", window=1)
        assert all(a.elapsed >= 0 for a in r.attempts)

    def test_total_elapsed_at_least_sum(self):
        c, _ = _chain_with(LAYER_OCR)
        r = c.locate("x", window=1)
        assert r.elapsed >= sum(a.elapsed for a in r.attempts) - 1e-6

    def test_skipped_layers_marked(self):
        c = VisionChain(allow_uia=True, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=False)
        r = c.locate("x", window=1)
        skipped = [a for a in r.attempts if a.skipped]
        assert len(skipped) == 4

    def test_layer_exception_downgrades(self):
        """★ 某层抛异常要降级到下一层，不是中断。"""
        c = VisionChain(allow_llm=False)

        def _boom(**kw):
            raise RuntimeError("这层炸了")

        c._layer_uia = _boom
        c._layer_cdp = lambda **kw: (True, (1, 2, 3, 4), 0.9, "第二层救场")
        r = c.locate("x", window=1)
        assert r.ok and r.method == LAYER_CDP
        assert "RuntimeError" in r.attempts[0].reason


class TestResultShape:
    def test_rect_and_center(self):
        c, _ = _chain_with(LAYER_OCR)
        r = c.locate("x", window=1)
        assert r.rect == (10, 20, 30, 40)
        assert r.center == (20, 30)

    def test_to_dict_json_safe(self):
        import json
        c, _ = _chain_with(LAYER_OCR)
        r = c.locate("x", window=1)
        assert json.dumps(r.to_dict(), ensure_ascii=False)

    def test_describe_and_explain(self):
        c, _ = _chain_with(LAYER_OCR)
        r = c.locate("x", window=1)
        assert "找到" in r.describe()
        assert "查找过程" in r.explain()


class TestLayerGuards:
    """每层对"没有输入"要有明确失败原因。"""

    def test_uia_no_name(self):
        assert VisionChain()._layer_uia(name="")[3] == "没有控件名可查"

    def test_cdp_no_session(self):
        assert "没有 CDP 会话" in VisionChain()._layer_cdp(selector="#x")[3]

    def test_cdp_no_selector(self):
        c = VisionChain(cdp_session=object())
        assert "没有 CSS 选择器" in c._layer_cdp()[3]

    def test_ocr_no_name(self):
        assert VisionChain()._layer_ocr(name="", window=1)[3] == "没有文字可查"

    def test_ocr_no_window(self):
        c = VisionChain()
        assert "没有窗口句柄" in c._layer_ocr(name="确定", window=0)[3]

    def test_template_no_template(self):
        c = VisionChain()
        assert c._layer_template(template=None, window=1)[3] == "没有提供模板图"

    def test_template_no_window(self):
        c = VisionChain()
        assert "没有窗口句柄" in c._layer_template(
            template=object(), window=0)[3]

    def test_llm_disabled_by_default(self):
        """第五层**默认关**，且失败必须给原因。

        ⚠ 这条原先是 ``assert ok is False and "占位" in reason`` ——
        阶段七第五层只有接口占位，文案里带"占位"二字。阶段八把它
        真正实现之后，文案变成"视觉大模型未启用：视觉后端未启用
        （需要 enabled=True）"，**"占位"两个字就没了**。

        于是这条从"每次必过"变成"每次必挂"，而且是**确定性**的：
        单独跑也挂，不是跨测试污染。已核实。

        契约改成"默认关 + 说清为什么" —— 那才是真正要守的东西：
        LLM 层最贵（3000ms 起、要联网、花真钱），用户没显式打开时
        绝不能被悄悄调用。
        """
        ok, rect, conf, reason = VisionChain()._layer_llm()
        assert ok is False, "第五层必须默认关"
        assert reason, "失败必须给出原因，不能只回 False"
        assert "未启用" in reason, "原因要说清是没启用：%r" % reason


class TestPureMatching:
    """纯 Python 匹配算法（不需要引擎）。"""

    class _Bmp:
        def __init__(self, w, h, fill=128, pattern=False):
            self.width, self.height, self.stride = w, h, w * 4
            if pattern:
                # ⚠ **必须有纹理**。NCC 靠方差算相关 ——
                #   纯色图方差为 0，永远匹配不上（那是正确行为，
                #   不是 bug：一块纯色没有"特征"可匹配）。
                px = bytearray()
                for y in range(h):
                    for x in range(w):
                        v = ((x * 7 + y * 13) % 200) + 30
                        px += bytes([v, v, v, 255])
                self.pixels = bytes(px)
            else:
                self.pixels = bytes([fill]) * (w * h * 4)

    def test_template_match_identical(self):
        from avatar.uia import vision_fallback as V
        b = self._Bmp(64, 64, pattern=True)
        m = V.find_template(b, self._Bmp(16, 16, pattern=True),
                            tolerance=0.9, stride=1)
        assert m is not None and m.score > 0.9

    def test_flat_image_never_matches(self):
        """★ 纯色图**永远匹配不上** —— 方差为 0 时 NCC 无意义。

        这条是**刻意的**：一块纯色没有特征，说它"匹配"是胡说。
        """
        from avatar.uia import vision_fallback as V
        assert V.find_template(self._Bmp(64, 64), self._Bmp(16, 16),
                               tolerance=0.1, stride=2) is None

    def test_template_no_match_returns_none(self):
        """★ 低于阈值返回 None —— **不返回"最像的"**。"""
        from avatar.uia import vision_fallback as V
        assert V.find_template(self._Bmp(32, 32, pattern=True),
                               self._Bmp(16, 16, pattern=True),
                               tolerance=2.0) is None

    def test_diff_identical_is_zero(self):
        from avatar.uia import vision_fallback as V
        assert V.diff(self._Bmp(16, 16), self._Bmp(16, 16)) == 0.0

    def test_diff_different(self):
        from avatar.uia import vision_fallback as V
        assert V.diff(self._Bmp(16, 16, 0), self._Bmp(16, 16, 255)) > 0.9

    def test_find_color(self):
        from avatar.uia import vision_fallback as V
        pts = V.find_color(self._Bmp(32, 32, 128), (128, 128, 128),
                           tolerance=2)
        assert pts

    def test_find_color_no_match(self):
        from avatar.uia import vision_fallback as V
        assert V.find_color(self._Bmp(16, 16, 0), (255, 255, 255),
                            tolerance=2) == []
