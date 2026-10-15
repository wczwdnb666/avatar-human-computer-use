# -*- coding: utf-8 -*-
"""阶段八 · 五层降级链端到端（``vision/chain`` 的第五层）。

## 这个文件在守什么

1. **第五层必须是最后一层**（硬约束 9）
2. **默认关闭**（硬约束 2）
3. **前四层全失败才降级到第五层**
4. **第五层失败 → 链条终止，不降级到盲点**

## 全程假后端

不联网、不动桌面。
"""
from __future__ import annotations

import pytest

from avatar.vision import capture as CAP
from avatar.vision import cost as CO
from avatar.vision import llm_backend as LB
from avatar.vision import validator as VD
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


class Shot:
    """有纹理的假截图（像素校验会通过）。"""

    def __init__(self, w=400, h=300, flat=False):
        self.width, self.height, self.stride = w, h, w * 4
        if flat:
            self.pixels = bytes([200]) * (w * h * 4)
        else:
            self.pixels = bytes([((x * 7 + y * 13) % 200) + 20
                                 for y in range(h) for x in range(w)
                                 for _ in range(4)])

    def region(self, x, y, w, h):
        return self


class Backend(LB.OpenAICompatibleVisionBackend):
    def __init__(self, x=100, y=200, conf=0.9, found=True, ok=True):
        super().__init__(enabled=True, api_key="k",
                         base_url="https://x", model="m")
        self._x, self._y, self._conf = x, y, conf
        self._found, self._ok = found, ok
        self.calls = 0

    def locate(self, image, goal, **kw):
        self.calls += 1
        r = LB.VisionResult(ok=self._ok, found=self._found, x=self._x,
                            y=self._y, confidence=self._conf,
                            reason="fake")
        r.provider, r.model = "fake", "fake-1"
        r.prompt_tokens, r.completion_tokens = 1200, 40
        r.attempts, r.elapsed = 1, 0.01
        return r


class OkValidator(VD.CoordinateValidator):
    def validate(self, llm_result, image_info=None, screen_rect=None,
                 bitmap=None, trace=None):
        out = VD.ValidatedResult(ok=True, screen_x=100, screen_y=200,
                                 confidence=0.9,
                                 rect=(75, 175, 125, 225))
        out.gates = [VD.GateResult(gate=g, passed=True, reason="ok")
                     for g in VD.GATE_ORDER]
        if trace is not None:
            trace.clear()
            trace.extend(out.gates)
        return out


@pytest.fixture
def fake_shot(monkeypatch):
    orig = CAP.grab_window
    monkeypatch.setattr(CAP, "grab_window",
                        lambda hwnd, **kw: Shot())
    yield
    monkeypatch.setattr(CAP, "grab_window", orig)


class TestLayerOrder:
    def test_llm_is_last(self):
        """★★ 第五层必须是最后一层（硬约束 9）。"""
        assert LAYER_ORDER[-1] == LAYER_LLM

    def test_order_unchanged(self):
        assert LAYER_ORDER == (LAYER_UIA, LAYER_CDP, LAYER_OCR,
                               LAYER_TEMPLATE, LAYER_LLM)

    def test_cost_increases(self):
        costs = [LAYER_COST[l] for l in LAYER_ORDER]
        assert costs == sorted(costs)

    def test_llm_off_by_default(self):
        """★★ 第五层默认关闭（硬约束 2）。"""
        assert VisionChain().allow[LAYER_LLM] is False


class TestLLMLayerGuards:
    def test_disabled_reports_reason(self):
        ok, _, _, reason = VisionChain()._layer_llm(name="x", window=1)
        assert ok is False and reason

    def test_needs_goal(self, fake_shot):
        c = VisionChain(allow_llm=True, llm_backend=Backend())
        ok, _, _, reason = c._layer_llm(name="", window=1)
        assert ok is False and "没有目标描述" in reason

    def test_needs_window(self, fake_shot):
        c = VisionChain(allow_llm=True, llm_backend=Backend())
        ok, _, _, reason = c._layer_llm(name="确定", window=0)
        assert ok is False and "没有窗口句柄" in reason


class TestLLMLayerFullPath:
    def test_success(self, fake_shot):
        c = VisionChain(allow_llm=True, llm_backend=Backend(),
                        validator=OkValidator())
        ok, rect, conf, why = c._layer_llm(name="确定", window=1)
        assert ok is True and rect == (75, 175, 125, 225)
        assert conf == 0.9

    def test_budget_recorded(self, fake_shot):
        budget = CO.CostBudget()
        c = VisionChain(allow_llm=True, llm_backend=Backend(),
                        cost_budget=budget, validator=OkValidator())
        c._layer_llm(name="确定", window=1)
        assert budget.requests >= 1

    def test_result_cached(self, fake_shot):
        cache = CO.ResultCache()
        c = VisionChain(allow_llm=True, llm_backend=Backend(),
                        cache=cache, validator=OkValidator())
        c._layer_llm(name="确定", window=1)
        assert len(cache) == 1

    def test_second_call_hits_cache(self, fake_shot):
        """★★ 第二次命中缓存，**不再花钱**。"""
        budget = CO.CostBudget()
        cache = CO.ResultCache()
        b = Backend()
        c = VisionChain(allow_llm=True, llm_backend=b, cache=cache,
                        cost_budget=budget, validator=OkValidator())
        c._layer_llm(name="确定", window=1)
        used = budget.used
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is True and "缓存" in why
        assert budget.used == used, "缓存命中不该消耗预算"
        assert b.calls == 1, "不该再调一次模型"

    def test_different_goal_calls_again(self, fake_shot):
        b = Backend()
        c = VisionChain(allow_llm=True, llm_backend=b,
                        cache=CO.ResultCache(), validator=OkValidator())
        c._layer_llm(name="确定", window=1)
        c._layer_llm(name="取消", window=1)
        assert b.calls == 2

    def test_model_not_found(self, fake_shot):
        c = VisionChain(allow_llm=True,
                        llm_backend=Backend(found=False),
                        validator=OkValidator())
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "没找到" in why

    def test_model_call_failed(self, fake_shot):
        c = VisionChain(allow_llm=True, llm_backend=Backend(ok=False),
                        validator=OkValidator())
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "失败" in why


class TestBudgetGate:
    def test_budget_rejects(self, fake_shot):
        """★★ 预算超限时拒绝执行（硬约束 6）。"""
        c = VisionChain(allow_llm=True, llm_backend=Backend(),
                        cost_budget=CO.CostBudget(per_request=1, total=1))
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "预算" in why


class TestValidationGate:
    def test_blank_area_rejected(self, monkeypatch):
        """★★ 模型在空白处报坐标 → 像素闸门拒绝。"""
        monkeypatch.setattr(CAP, "grab_window",
                            lambda hwnd, **kw: Shot(flat=True))
        c = VisionChain(allow_llm=True, llm_backend=Backend(),
                        validator=VD.CoordinateValidator())
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "校验未通过" in why
        assert "pixel" in why, why

    def test_low_confidence_rejected(self, fake_shot):
        c = VisionChain(allow_llm=True,
                        llm_backend=Backend(conf=0.1),
                        validator=VD.CoordinateValidator())
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "confidence" in why, why

    def test_out_of_range_rejected(self, fake_shot):
        c = VisionChain(allow_llm=True,
                        llm_backend=Backend(x=99999),
                        validator=VD.CoordinateValidator())
        ok, _, _, why = c._layer_llm(name="确定", window=1)
        assert ok is False and "range" in why, why


class TestChainIntegration:
    def test_llm_off_so_chain_fails(self, fake_shot):
        """★ 默认关 → 五层全失败，**不会走到盲点**。"""
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=False)
        r = c.locate("确定", window=1)
        assert r.ok is False
        assert "都被关掉" in r.reason

    def test_chain_reaches_llm_last(self, fake_shot):
        """★★ 前四层全失败 → 降级到第五层。"""
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=True,
                        llm_backend=Backend(), validator=OkValidator())
        r = c.locate("确定", window=1)
        assert r.ok is True and r.method == LAYER_LLM
        assert r.fallback_used is True

    def test_llm_result_has_validation(self, fake_shot):
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=True,
                        llm_backend=Backend(), validator=OkValidator())
        r = c.locate("确定", window=1)
        assert r.validation is not None
        assert len(r.validation.get("gates", [])) == 5

    def test_result_has_cost_field(self, fake_shot):
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=True,
                        llm_backend=Backend(),
                        cost_budget=CO.CostBudget(),
                        validator=OkValidator())
        r = c.locate("确定", window=1)
        assert r.cost >= 0

    def test_llm_failure_terminates_chain(self, fake_shot):
        """★★ 第五层失败 → 链条终止，**不降级到盲点**。"""
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=True,
                        llm_backend=Backend(found=False),
                        validator=OkValidator())
        r = c.locate("确定", window=1)
        assert r.ok is False and r.rect is None

    def test_no_blind_click_on_failure(self, fake_shot):
        """★★ 任何失败都不返回坐标。"""
        c = VisionChain(allow_uia=False, allow_cdp=False, allow_ocr=False,
                        allow_template=False, allow_llm=True,
                        llm_backend=Backend(ok=False),
                        validator=OkValidator())
        r = c.locate("确定", window=1)
        assert r.ok is False and r.rect is None and r.center is None
