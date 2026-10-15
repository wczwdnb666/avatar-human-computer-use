# -*- coding: utf-8 -*-
"""阶段八 · 图像预处理（``vision/image_prep``）。

## 这个文件在守什么

1. **可逆性** —— scale 必须记录（漏了坐标会整体偏移）
2. **顺序** —— 裁剪 → DPR → 缩放 → 编码
3. **两种编码器** —— JPEG（GDI+）优先，PNG（zlib）兜底
"""
from __future__ import annotations

import struct

import pytest

from avatar.vision.image_prep import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MAX_EDGE,
    MAX_MAX_EDGE,
    MIN_MAX_EDGE,
    PreparedImage,
    crop_region,
    downscale,
    encode_jpeg,
    encode_png,
    estimate_image_tokens,
    handle_dpr,
    jpeg_available,
    prepare_for_llm,
)


class Bmp:
    def __init__(self, w, h, flat=False):
        self.width, self.height = int(w), int(h)
        self.stride = int(w) * 4
        if flat:
            self.pixels = bytes([128]) * (w * h * 4)
        else:
            self.pixels = bytes([((x * 7 + y * 13) % 200) + 20
                                 for y in range(h) for x in range(w)
                                 for _ in range(4)])

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
        o = Bmp.__new__(Bmp)
        o.width, o.height, o.stride, o.pixels = w, h, w * 4, bytes(out)
        return o


class TestContract:
    def test_docstring_has_boundary(self):
        import avatar.vision.image_prep as M
        assert "能力边界" in (M.__doc__ or "")

    def test_defaults(self):
        assert DEFAULT_MAX_EDGE == 1280
        assert DEFAULT_JPEG_QUALITY == 85

    def test_edge_range(self):
        assert MIN_MAX_EDGE < DEFAULT_MAX_EDGE < MAX_MAX_EDGE


class TestTokenEstimate:
    def test_1280x720(self):
        assert 1200 < estimate_image_tokens(1280, 720) < 1300

    def test_full_screen_is_huge(self):
        """★ 2880x1800 约 6912 tokens —— 这就是为什么要压缩。"""
        assert 6800 < estimate_image_tokens(2880, 1800) < 7000

    def test_zero_and_negative(self):
        assert estimate_image_tokens(0, 0) == 0
        assert estimate_image_tokens(-5, -5) == 0

    def test_never_raises(self):
        for a, b in ((0, 1), (1, 0), (99999, 99999)):
            assert estimate_image_tokens(a, b) >= 0


class TestDownscale:
    def test_half(self):
        s = downscale(Bmp(400, 200), 0.5)
        assert (s.width, s.height) == (200, 100)

    def test_no_upscale(self):
        b = Bmp(100, 100)
        assert downscale(b, 1.0) is b
        assert downscale(b, 2.0) is b

    def test_bad_factor_safe(self):
        b = Bmp(100, 100)
        assert downscale(b, 0).width == 100

    def test_aspect_preserved(self):
        s = downscale(Bmp(400, 200), 0.5)
        assert abs(s.width / s.height - 2.0) < 0.02

    def test_flat_stays_flat(self):
        """★ 面积平均：纯色图缩完还是纯色。"""
        s = downscale(Bmp(100, 100, flat=True), 0.5)
        assert all(v == 128 for v in s.pixels[0::4])


class TestCrop:
    def test_size_and_offset(self):
        got, ox, oy = crop_region(Bmp(400, 200), (10, 20, 110, 70))
        assert (got.width, got.height) == (100, 50)
        assert (ox, oy) == (10, 20)

    def test_none_returns_original(self):
        b = Bmp(100, 100)
        assert crop_region(b, None)[0] is b

    def test_garbage_safe(self):
        b = Bmp(100, 100)
        assert crop_region(b, "乱写")[0] is b

    def test_clamped(self):
        got, _, _ = crop_region(Bmp(400, 200), (-10, -10, 99999, 99999))
        assert got.width == 400

    def test_empty_region(self):
        b = Bmp(100, 100)
        assert crop_region(b, (50, 50, 50, 50))[0] is b


class TestDPR:
    def test_one(self):
        b = Bmp(10, 10)
        assert handle_dpr(b, 1.0)[1] == 1.0

    def test_two_keeps_bitmap(self):
        """★ DPR 只记因子，**不动图**（动图会丢清晰度）。"""
        b = Bmp(10, 10)
        got, f = handle_dpr(b, 2.0)
        assert f == 2.0 and got is b

    def test_bad_safe(self):
        assert handle_dpr(Bmp(10, 10), 0)[1] == 1.0


class TestEncoding:
    def test_png_magic(self):
        png = encode_png(Bmp(100, 50))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert b"IHDR" in png[:64] and png[-8:-4] == b"IEND"

    def test_png_dimensions(self):
        png = encode_png(Bmp(100, 50))
        assert struct.unpack(">II", png[16:24]) == (100, 50)

    def test_png_zero_raises(self):
        with pytest.raises(ValueError):
            encode_png(Bmp(0, 0))

    def test_jpeg_probe(self):
        assert jpeg_available() in (True, False)

    @pytest.mark.skipif(not jpeg_available(), reason="GDI+ 不可用")
    def test_jpeg_magic(self):
        jp = encode_jpeg(Bmp(100, 50), 85)
        assert jp[:2] == b"\xff\xd8" and jp[-2:] == b"\xff\xd9"

    @pytest.mark.skipif(not jpeg_available(), reason="GDI+ 不可用")
    def test_jpeg_quality_matters(self):
        b = Bmp(200, 150)
        assert len(encode_jpeg(b, 50)) < len(encode_jpeg(b, 95))


class TestPrepare:
    def test_downscales_long_edge(self):
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280)
        assert max(p.width, p.height) == 1280

    def test_records_scale(self):
        """★★ scale 必须记录 —— 坐标换算要用它。"""
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280)
        assert 0 < p.scale < 1.0

    def test_records_source_size(self):
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280)
        assert (p.src_width, p.src_height) == (1440, 900)

    def test_encodes(self):
        p = prepare_for_llm(Bmp(400, 300))
        assert p.data and p.codec != "none"

    def test_mime_matches_codec(self):
        p = prepare_for_llm(Bmp(400, 300))
        assert (p.codec.startswith("jpeg") and p.mime == "image/jpeg") or \
               (p.codec.startswith("png") and p.mime == "image/png")

    def test_compressed_tokens_far_less(self):
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280)
        assert p.est_tokens < estimate_image_tokens(1440, 900)

    def test_coordinate_reversible(self):
        """★★ 压缩图坐标能反推回原图。"""
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280)
        sx, sy = p.image_to_source(int(p.width * 0.5), int(p.height * 0.5))
        assert abs(sx - 720) <= 2 and abs(sy - 450) <= 2

    def test_small_not_upscaled(self):
        p = prepare_for_llm(Bmp(200, 100), max_edge=1280)
        assert p.scale == 1.0 and (p.width, p.height) == (200, 100)

    def test_region_offset_recorded(self):
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280,
                            region=(100, 200, 500, 400))
        assert (p.offset_x, p.offset_y) == (100, 200)

    def test_dpr_in_scale(self):
        p = prepare_for_llm(Bmp(1440, 900), max_edge=1280, dpr=2.0)
        assert abs(p.scale - (1280 / 1440.0) * 2.0) < 0.01

    def test_prefer_png(self):
        p = prepare_for_llm(Bmp(100, 100), prefer="png")
        assert p.codec == "png-zlib"

    def test_none_safe(self):
        assert prepare_for_llm(None).codec == "none"

    def test_zero_size_safe(self):
        assert prepare_for_llm(Bmp(0, 0)).codec == "none"

    def test_max_edge_clamped(self):
        assert max(prepare_for_llm(Bmp(1440, 900),
                                   max_edge=10).width,
                   prepare_for_llm(Bmp(1440, 900),
                                   max_edge=10).height) >= MIN_MAX_EDGE

    def test_to_dict_json_safe(self):
        import json
        p = prepare_for_llm(Bmp(200, 100))
        assert json.dumps(p.to_dict(), ensure_ascii=False)


class TestNoEnhancement:
    def test_no_enhancement_functions(self):
        """★ 不做图像增强 —— 增强会让模型「看到」不存在的东西。"""
        import ast
        import avatar.vision.image_prep as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert not (names & {"sharpen", "denoise", "threshold", "equalize",
                             "enhance", "upscale"})

    def test_no_pil(self):
        import ast
        import avatar.vision.image_prep as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & {"PIL", "cv2", "numpy"})
        assert "zlib" in mods
