# -*- coding: utf-8 -*-
"""阶段七 · OCR（``uia/ocr_fallback``）。

## 这个文件在守什么

OCR 的**可用性是环境决定的**（缺语言包 / 缺包标识 / WinRT 位图通路不通）。
所以这里守的不是"能不能识别"，而是：

1. **不可用时给出可操作的诊断**（不是一句"不可用"）
2. **接口在两种状态下都不崩**（可用 / 不可用）
3. **注入自定义引擎后行为正确**（这是唯一的可控路径）

## 无 OCR 环境

`is_available()` 为 False 时，涉及真实识别的测试自动 skip。
"""
from __future__ import annotations

import pytest

from avatar.uia import ocr_fallback as O

_HAS_OCR = O.is_available()
_needs_ocr = pytest.mark.skipif(not _HAS_OCR,
                                reason="本机 OCR 不可用：%s"
                                       % O.unavailable_reason()[:60])


@pytest.fixture
def fake_engine():
    """注入一个假引擎，用完恢复。"""
    saved = O._ENGINE, O._ENGINE_NAME

    def _fake(window, region, min_conf, lang):
        return [
            O.OcrHit(text="确定", window=window,
                     client_rect=(10, 20, 60, 40), confidence=0.95,
                     engine="fake"),
            O.OcrHit(text="取消", window=window,
                     client_rect=(80, 20, 130, 40), confidence=0.90,
                     engine="fake"),
            O.OcrHit(text="S0S", window=window,
                     client_rect=(200, 20, 240, 40), confidence=0.88,
                     engine="fake"),
        ]

    O.set_engine(_fake, "fake")
    yield _fake
    O._ENGINE, O._ENGINE_NAME = saved
    O._PROBE["done"] = False


class TestContract:
    def test_docstring_has_boundary(self):
        assert "能力边界" in (O.__doc__ or "")

    def test_docstring_documents_known_issue(self):
        """★ 已知问题必须写在文档里 —— 不能只留在聊天记录里。"""
        assert "已知问题" in (O.__doc__ or "")

    def test_unavailable_is_unsupported_error(self):
        from avatar.errors import UnsupportedError
        assert issubclass(O.OcrUnavailable, UnsupportedError)

    def test_ocr_word_center(self):
        assert O.OcrWord(rect=(0, 0, 10, 20)).center == (5, 10)

    def test_ocr_hit_keeps_stage2_fields(self):
        h = O.OcrHit()
        for f in ("text", "window", "client_rect", "confidence",
                  "engine", "raw"):
            assert hasattr(h, f)

    def test_default_min_confidence_sane(self):
        assert 0.0 < O.DEFAULT_MIN_CONFIDENCE <= 1.0


class TestAvailabilityNeverRaises:
    def test_is_available(self):
        assert O.is_available() in (True, False)

    def test_unavailable_reason(self):
        assert isinstance(O.unavailable_reason(), str)

    def test_explain_unavailable(self):
        assert isinstance(O.explain_unavailable(), str)

    def test_languages(self):
        assert isinstance(O.languages(), list)

    def test_describe_environment(self):
        assert isinstance(O.describe_environment(), dict)

    def test_probe_bitmap_path(self):
        assert isinstance(O.probe_bitmap_path(), str)

    def test_availability_is_honest(self):
        """★ 可用就必须有结论；不可用就必须有原因。"""
        assert O.is_available() or bool(O.unavailable_reason())


class TestExplainWhenUnavailable:
    @pytest.mark.skipif(_HAS_OCR, reason="本机 OCR 可用，跳过诊断测试")
    def test_explain_mentions_specific_stage(self):
        exp = O.explain_unavailable()
        assert any(k in exp for k in
                   ("包标识", "语言包", "位图通路", "引擎能否创建")), exp[:200]

    @pytest.mark.skipif(_HAS_OCR, reason="本机 OCR 可用，跳过诊断测试")
    def test_explain_suggests_alternative(self):
        assert "set_engine" in O.explain_unavailable()

    def test_explain_never_raises_even_when_available(self):
        assert isinstance(O.explain_unavailable(), str)


class TestCustomEngine:
    def test_set_engine_makes_available(self, fake_engine):
        assert O.is_available() is True

    def test_engine_name(self, fake_engine):
        assert O.engine_name() == "fake"

    def test_find_text(self, fake_engine):
        h = O.find_text("确定", window=0x1234)
        assert h is not None and h.text == "确定"

    def test_find_text_miss_returns_none(self, fake_engine):
        """★ 匹配不上返回 None —— **不返回"最像的"**。"""
        assert O.find_text("不存在的文字", window=1) is None

    def test_find_all_text_sorted_by_confidence(self, fake_engine):
        hits = O.find_all_text("", window=1)
        assert [h.confidence for h in hits] == \
            sorted((h.confidence for h in hits), reverse=True)

    def test_exact_match(self, fake_engine):
        assert O.find_text("确定", window=1, exact=True) is not None

    def test_fuzzy_tolerates_ocr_confusion(self, fake_engine):
        """★ fuzzy 容忍 0↔O / S↔5 这类误认。"""
        assert O.find_text("SOS", window=1, fuzzy=True) is not None

    def test_strict_by_default(self, fake_engine):
        assert O.find_text("SOS", window=1, fuzzy=False) is None

    def test_read_region(self, fake_engine):
        assert len(O.read_region(1)) == 3

    def test_window_required(self, fake_engine):
        with pytest.raises(ValueError):
            O.find_all_text("x", window=0)

    def test_engine_exception_propagates(self, fake_engine):
        """★ 引擎自己的异常**不吞** —— 那是它的问题，要暴露。"""
        def _boom(window, region, min_conf, lang):
            raise RuntimeError("引擎炸了")

        O.set_engine(_boom, "boom")
        with pytest.raises(RuntimeError):
            O.find_all_text("x", window=1)


class TestBitmapConversion:
    class _Bmp:
        def __init__(self, w, h):
            self.width, self.height, self.stride = w, h, w * 4
            self.pixels = bytes([(y * 10 + x) % 256
                                 for y in range(h) for x in range(w * 4)])

    def test_bgra_flip(self):
        """★ DIB 自下而上 → SoftwareBitmap 自上而下，必须翻转。"""
        b = self._Bmp(4, 3)
        top = O._bgra_top_down(b)
        assert len(top) == 4 * 3 * 4
        assert top[0:4] == b.pixels[2 * 16:2 * 16 + 4]

    def test_bad_size_raises(self):
        with pytest.raises(O.OcrUnavailable):
            O._bgra_top_down(self._Bmp(0, 0))

    def test_no_pil_dependency(self):
        import ast
        tree = ast.parse(open(O.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"pytesseract", "paddleocr", "easyocr",
                            "cv2", "PIL"}), mods


class TestNoHardSolving:
    def test_no_gap_symbols(self):
        """★ 硬约束 #9：只识别文字，不解题。"""
        import ast
        tree = ast.parse(open(O.__file__, encoding="utf-8").read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names.add(node.name)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        assert not (names & {"find_gap", "detect_gap", "solve_slider",
                             "gap_position"})


@_needs_ocr
class TestRealRecognition:
    def test_recognize_screen(self):
        """真的抓屏识别一次。**本机 OCR 可用时才跑。**"""
        from avatar.vision import capture as C
        shot = C.grab_screen()
        assert shot is not None
        words = O.recognize(shot, region=(0, 0, shot.width, 200),
                            timeout=20)
        assert isinstance(words, list)

    def test_languages_non_empty(self):
        assert O.languages()
