# -*- coding: utf-8 -*-
"""阶段八 · 坐标校验（``vision/validator``）。

## 这个文件在守什么

**大模型会编造坐标**。四道闸门缺一不可：

1. 范围校验 —— 越界拒绝
2. 坐标换算 —— 图像坐标 → 屏幕坐标
3. 像素校验 —— 空白区拒绝
4. 置信度阈值 —— 模型没把握就拒绝

外加：**失败返回 None，不降级到盲点**。
"""
from __future__ import annotations

import pytest

from avatar.vision.validator import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_PIXEL_PATCH,
    GATE_CONFIDENCE,
    GATE_ORDER,
    GATE_PIXEL,
    GATE_RANGE,
    GATE_RECHECK,
    GATE_SCALE,
    CoordinateValidator,
    GateResult,
    ValidatedResult,
    patch_variance,
)


class Img:
    def __init__(self, width=1280, height=720, scale=1.0,
                 offset_x=0, offset_y=0):
        self.width, self.height = width, height
        self.scale = scale
        self.offset_x, self.offset_y = offset_x, offset_y

    def image_to_source(self, x, y):
        s = self.scale if self.scale > 0 else 1.0
        return (int(round(x / s)) + self.offset_x,
                int(round(y / s)) + self.offset_y)


class Res:
    def __init__(self, x=100, y=200, conf=0.9, found=True, ok=True,
                 reason=""):
        self.x, self.y = x, y
        self.confidence = conf
        self.found, self.ok = found, ok
        self.reason = reason


class Bmp:
    def __init__(self, w=1280, h=720, flat=False):
        self.width, self.height = w, h
        self.stride = w * 4
        if flat:
            self.pixels = bytes([200]) * (w * h * 4)
        else:
            self.pixels = bytes([((x * 7 + y * 13) % 200) + 20
                                 for y in range(h) for x in range(w)
                                 for _ in range(4)])


class TestContract:
    def test_docstring_has_boundary(self):
        import avatar.vision.validator as M
        assert "能力边界" in (M.__doc__ or "")

    def test_five_gates(self):
        assert len(GATE_ORDER) == 5 and GATE_ORDER[-1] == GATE_RECHECK

    def test_defaults(self):
        assert DEFAULT_MIN_CONFIDENCE == 0.5
        assert DEFAULT_PIXEL_PATCH == 50

    def test_recheck_off_by_default(self):
        assert CoordinateValidator().recheck is False

    def test_pixel_check_required_by_default(self):
        assert CoordinateValidator().require_pixel_check is True


class TestVariance:
    def test_flat_is_zero(self):
        v, n = patch_variance(Bmp(200, 200, flat=True), 100, 100, 50)
        assert v < 1.0 and n == 2500

    def test_textured_is_high(self):
        v, _ = patch_variance(Bmp(200, 200, flat=False), 100, 100, 50)
        assert v > 8.0

    def test_none_safe(self):
        assert patch_variance(None, 10, 10) == (0.0, 0)

    def test_out_of_range_safe(self):
        assert patch_variance(Bmp(50, 50), -999, -999, 50)[0] >= 0


class TestGate1Range:
    def test_out_of_range_rejected(self):
        V = CoordinateValidator()
        img, bmp = Img(), Bmp(flat=False)
        assert V.validate(Res(x=5000), img, bitmap=bmp) is None
        assert V.validate(Res(y=9999), img, bitmap=bmp) is None
        assert V.validate(Res(x=-1), img, bitmap=bmp) is None

    def test_boundaries_pass(self):
        V = CoordinateValidator()
        img, bmp = Img(), Bmp(flat=False)
        assert V.validate(Res(x=0, y=0), img, bitmap=bmp) is not None
        assert V.validate(Res(x=1279, y=719), img, bitmap=bmp) is not None

    def test_exclusive_upper(self):
        """★ 上界是开区间 —— x=width 越界。"""
        V = CoordinateValidator()
        assert V.validate(Res(x=1280), Img(), bitmap=Bmp(flat=False)) is None


class TestPreconditions:
    def test_none_result(self):
        assert CoordinateValidator().validate(None, Img()) is None

    def test_not_ok(self):
        V = CoordinateValidator()
        assert V.validate(Res(ok=False), Img(), bitmap=Bmp(flat=False)) is None

    def test_not_found(self):
        V = CoordinateValidator()
        assert V.validate(Res(found=False), Img(),
                          bitmap=Bmp(flat=False)) is None


class TestGate2Scale:
    def test_scale_applied(self):
        """★★ scale=0.5 → 图像 (100,200) 对应原图 (200,400)。"""
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200), Img(scale=0.5),
                       bitmap=Bmp(flat=False))
        assert r is not None and (r.source_x, r.source_y) == (200, 400)

    def test_offset_applied(self):
        """★★ 裁剪偏移必须加上。"""
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200),
                       Img(offset_x=50, offset_y=30),
                       bitmap=Bmp(flat=False))
        assert r is not None and (r.source_x, r.source_y) == (150, 230)

    def test_screen_rect_applied(self):
        """★★ screen_rect 偏移也要加上。"""
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200),
                       Img(offset_x=50, offset_y=30),
                       screen_rect=(1000, 500, 2280, 1220),
                       bitmap=Bmp(flat=False))
        assert r is not None and (r.screen_x, r.screen_y) == (1150, 730)

    def test_no_image_info(self):
        assert CoordinateValidator().validate(Res(), None) is None

    def test_dict_image_info(self):
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200),
                       {"width": 1280, "height": 720, "scale": 0.5,
                        "offset_x": 0, "offset_y": 0},
                       bitmap=Bmp(flat=False))
        assert r is not None


class TestGate3Pixel:
    def test_flat_rejected(self):
        """★★ 空白区坐标被拒 —— 模型在编造。"""
        V = CoordinateValidator()
        assert V.validate(Res(x=100, y=200), Img(),
                          bitmap=Bmp(flat=True)) is None

    def test_textured_passes(self):
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200), Img(),
                       bitmap=Bmp(flat=False))
        assert r is not None and r.ok

    def test_variance_recorded(self):
        V = CoordinateValidator()
        gates = []
        V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False),
                   trace=gates)
        pix = [g for g in gates if g.gate == GATE_PIXEL]
        assert pix and "variance" in pix[0].detail

    def test_no_bitmap_fails_by_default(self):
        """★★ 没有原图时**默认失败** —— 不校验就点击是危险的。"""
        V = CoordinateValidator()
        assert V.validate(Res(), Img(), bitmap=None) is None

    def test_no_bitmap_skippable(self):
        V = CoordinateValidator(require_pixel_check=False)
        r = V.validate(Res(x=100, y=200), Img(), bitmap=None)
        assert r is not None and any(g.gate == GATE_PIXEL and g.skipped
                                     for g in r.gates)

    def test_threshold_configurable(self):
        V = CoordinateValidator(variance_min=99999)
        assert V.validate(Res(x=100, y=200), Img(),
                          bitmap=Bmp(flat=False)) is None


class TestGate4Confidence:
    def test_low_rejected(self):
        V = CoordinateValidator()
        assert V.validate(Res(conf=0.3), Img(),
                          bitmap=Bmp(flat=False)) is None

    def test_boundary_passes(self):
        V = CoordinateValidator()
        assert V.validate(Res(conf=0.5), Img(),
                          bitmap=Bmp(flat=False)) is not None

    def test_just_below_rejected(self):
        V = CoordinateValidator()
        assert V.validate(Res(conf=0.499), Img(),
                          bitmap=Bmp(flat=False)) is None

    def test_threshold_configurable(self):
        V = CoordinateValidator(min_confidence=0.99)
        assert V.validate(Res(conf=0.9), Img(),
                          bitmap=Bmp(flat=False)) is None


class TestGate5Recheck:
    def test_called_when_enabled(self):
        calls = []

        def _yes(bmp, x, y, patch):
            calls.append((x, y, patch))
            return True, "是"

        V = CoordinateValidator(recheck=True, recheck_fn=_yes)
        r = V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False))
        assert r is not None and calls

    def test_reject(self):
        V = CoordinateValidator(recheck=True,
                                recheck_fn=lambda *a: (False, "不是"))
        assert V.validate(Res(x=100, y=200), Img(),
                          bitmap=Bmp(flat=False)) is None

    def test_not_called_by_default(self):
        calls = []
        V = CoordinateValidator(recheck_fn=lambda *a: (calls.append(1),
                                                       True)[1])
        V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False))
        assert not calls

    def test_exception_treated_as_pass(self):
        """★ 二次确认抛异常时**按通过处理** —— 不让它成为单点故障。"""
        def _boom(*a):
            raise RuntimeError("炸了")

        V = CoordinateValidator(recheck=True, recheck_fn=_boom)
        assert V.validate(Res(x=100, y=200), Img(),
                          bitmap=Bmp(flat=False)) is not None


class TestTrace:
    def test_trace_on_success(self):
        V = CoordinateValidator()
        gates = []
        r = V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False),
                       trace=gates)
        assert r is not None and len(gates) == 5

    def test_trace_on_failure(self):
        """★★ 失败时也要能拿到详情 —— 否则只知道「没通过」。"""
        V = CoordinateValidator()
        gates = []
        r = V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=True),
                       trace=gates)
        assert r is None
        failed = [g for g in gates if not g.passed and not g.skipped]
        assert failed and failed[0].gate == GATE_PIXEL
        assert failed[0].reason

    def test_failed_gate_identifiable(self):
        V = CoordinateValidator()
        gates = []
        V.validate(Res(conf=0.1), Img(), bitmap=Bmp(flat=False),
                   trace=gates)
        failed = [g for g in gates if not g.passed and not g.skipped]
        assert failed[0].gate == GATE_CONFIDENCE


class TestNoShortcuts:
    def test_no_coordinate_adjustment(self):
        """★ 不做坐标修正 —— 「差一点点」也不挪，挪多少都是猜。"""
        import ast
        import avatar.vision.validator as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert not (names & {"adjust", "snap_to", "correct", "fix_coord",
                             "nudge", "rank", "best_of"})

    def test_no_network(self):
        import ast
        import avatar.vision.validator as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & {"urllib", "requests", "PIL", "cv2"}), mods


class TestSerialization:
    def test_to_dict(self):
        import json
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False))
        assert r is not None
        assert json.dumps(r.to_dict(), ensure_ascii=False)

    def test_explain(self):
        V = CoordinateValidator()
        r = V.validate(Res(x=100, y=200), Img(), bitmap=Bmp(flat=False))
        assert r is not None and "坐标校验过程" in r.explain()

    def test_gate_describe(self):
        assert "✓" in GateResult(gate="x", passed=True,
                                 reason="ok").describe()
