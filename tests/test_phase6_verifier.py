# -*- coding: utf-8 -*-
"""阶段六 · 步骤验证（``verifier``）。

## 这个文件在守什么

验证是"自动执行"的眼睛。它错了，整个自动化就是盲跑。
这里守三件事：

1. **三态语义** —— ``ok`` / ``fail`` / ``unknown`` 严格互斥，
   且 ``unknown`` **不等于** ``fail``（前者不该触发重试，后者该）
2. **三个后端各自的判据** —— 像素 / UIA / DOM
3. **验证器自己坏了返回 unknown** —— 不是 fail（会误触发重试）

## 复用阶段四的契约

返回值必须是 :class:`avatar.verify.result.VerifyResult` ——
不能自己造一套三态。这样上游的处理逻辑可以统一。
"""

from __future__ import annotations

import os

import pytest

from avatar.agentic_planner.plan import Step
from avatar.agentic_planner.verifier import (
    PIXEL_DIFF_THRESHOLD,
    DomVerifier,
    PixelDiffVerifier,
    UiaStateVerifier,
    VerifierBackend,
    get_verifier,
    verify,
)
from avatar.verify.result import VerifyResult


# ==========================================================================
# 夹具
# ==========================================================================
def _step(kind, **spec):
    s = Step(id="s1", action="click", params={"target": "x"})
    s.verify = dict(kind=kind, **spec)
    return s


class _Obs:
    """最小观测替身。"""

    def __init__(self, **kw):
        self.ok = kw.get("ok", True)
        self.title = kw.get("title", "")
        self.url = kw.get("url", "")
        self.hwnd = kw.get("hwnd", 0)
        self.screenshot_path = kw.get("screenshot_path", "")
        self.identity = kw.get("identity", "") or self.title or self.url


@pytest.fixture
def no_uia():
    """一个"找不到任何元素"的假 UIA 查找 —— 避免全树遍历。"""
    return lambda name: None


# ==========================================================================
# 1. 契约
# ==========================================================================
class TestContract:
    def test_three_backends_are_siblings(self):
        """★ 三个后端只共享接口，不共享实现。"""
        for cls in (PixelDiffVerifier, UiaStateVerifier, DomVerifier):
            assert cls.__bases__ == (VerifierBackend,), cls.__name__

    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            VerifierBackend()

    def test_returns_verify_result(self):
        """★ 复用阶段四契约 —— 不能自己造一套三态。"""
        assert isinstance(verify(_step("elapsed"), None, None),
                          VerifyResult)

    def test_threshold_positive(self):
        assert PIXEL_DIFF_THRESHOLD > 0


# ==========================================================================
# 2. 路由
# ==========================================================================
class TestRouting:
    def test_routes_to_correct_backend(self):
        assert isinstance(get_verifier("pixel"), PixelDiffVerifier)
        assert isinstance(get_verifier("uia"), UiaStateVerifier)
        assert isinstance(get_verifier("dom"), DomVerifier)

    def test_unknown_kind_returns_none(self):
        assert get_verifier("乱写") is None

    def test_unknown_kind_yields_unknown_not_fail(self):
        """★★ 拼错的 kind 要返回 unknown —— 当成 fail 会让每步都失败。"""
        r = verify(_step("pxiel"), None, None)
        assert r.verdict == "unknown", r.describe()

    def test_no_criteria_yields_unknown(self):
        r = verify(Step(id="s1", action="click"), None, None)
        assert r.verdict == "unknown"


# ==========================================================================
# 3. 三态语义
# ==========================================================================
class TestThreeState:
    def test_verdicts_are_three(self):
        seen = set()
        for kind in ("window", "window_gone", "elapsed", "file",
                     "pixel", "uia", "dom", "乱写"):
            seen.add(verify(_step(kind), None, None,
                            uia_find=lambda n: None).verdict)
        assert seen <= {"ok", "fail", "unknown"}, seen

    def test_unknown_is_not_fail(self):
        """★★ 这是整个验证层最重要的一条区分。"""
        r = verify(_step("pixel"), None, None)
        assert r.verdict == "unknown"
        assert r.verdict != "fail"

    def test_verifier_crash_yields_unknown(self):
        """★ 验证器自己坏了 → unknown，不能是 fail（会误触发重试）。"""
        class _Boom:
            @property
            def verify(self):
                raise RuntimeError("步骤对象坏了")

        assert verify(_Boom(), None, None).verdict == "unknown"

    def test_none_step_yields_unknown(self):
        assert verify(None, None, None).verdict == "unknown"


# ==========================================================================
# 4. 内置判据
# ==========================================================================
class TestBuiltinCriteria:
    def test_window_match(self):
        r = verify(_step("window", expect="记事本"), None,
                   _Obs(title="记事本 - 未命名"))
        assert r.verdict == "ok"

    def test_window_mismatch(self):
        r = verify(_step("window", expect="计算器"), None,
                   _Obs(title="记事本"))
        assert r.verdict == "fail"

    def test_window_no_observation(self):
        """★ 没观测到窗口是 unknown（没查成），不是 fail。"""
        r = verify(_step("window", expect="记事本"), None, _Obs(ok=False))
        assert r.verdict == "unknown"

    def test_window_gone(self):
        r = verify(_step("window_gone"), _Obs(hwnd=100, title="记事本"),
                   _Obs(hwnd=200, title="桌面"))
        assert r.verdict == "ok"

    def test_window_still_there(self):
        r = verify(_step("window_gone"), _Obs(hwnd=100, title="记事本"),
                   _Obs(hwnd=100, title="记事本"))
        assert r.verdict == "fail"

    def test_elapsed_always_ok(self):
        """等待类没有"失败"这个概念。"""
        assert verify(_step("elapsed"), None, None).verdict == "ok"

    def test_file_exists(self, tmp_path):
        p = tmp_path / "a.bmp"
        p.write_bytes(b"\x00" * 50)
        r = verify(_step("file"), None, _Obs(screenshot_path=str(p)))
        assert r.verdict == "ok"

    def test_file_missing(self, tmp_path):
        r = verify(_step("file"), None,
                   _Obs(screenshot_path=str(tmp_path / "nope")))
        assert r.verdict == "fail"

    def test_file_empty(self, tmp_path):
        p = tmp_path / "e.bmp"
        p.write_bytes(b"")
        r = verify(_step("file"), None, _Obs(screenshot_path=str(p)))
        assert r.verdict == "fail"

    def test_file_no_path_is_unknown(self):
        assert verify(_step("file"), None, _Obs()).verdict == "unknown"


# ==========================================================================
# 5. 像素后端
# ==========================================================================
class TestPixelBackend:
    def test_no_screenshot_is_unknown(self):
        """★ 没截图是"没查成"，不是"没变化"。"""
        r = verify(_step("pixel"), _Obs(), _Obs())
        assert r.verdict == "unknown"

    def test_hamming_distance(self):
        from avatar.agentic_planner.verifier import _hamming
        assert _hamming(0, 0) == 0
        assert _hamming(0b1011, 0b1001) == 1
        assert _hamming(0b1111, 0) == 4

    def test_threshold_configurable(self):
        assert PixelDiffVerifier(threshold=42).threshold == 42

    def test_identical_hashes_fail(self, tmp_path, monkeypatch):
        """★ 两张一样的截图 = 界面没变 = fail（该重试）。"""
        v = PixelDiffVerifier(threshold=3)
        monkeypatch.setattr(
            "avatar.agentic_planner.verifier._phash_of",
            lambda shot: 12345)
        b = _Obs(screenshot_path=str(tmp_path / "a.bmp"))
        a = _Obs(screenshot_path=str(tmp_path / "b.bmp"))
        assert v.verify(_step("pixel"), b, a).verdict == "fail"

    def test_different_hashes_ok(self, tmp_path, monkeypatch):
        v = PixelDiffVerifier(threshold=3)
        seq = iter([0b0, 0b1111111])
        monkeypatch.setattr(
            "avatar.agentic_planner.verifier._phash_of",
            lambda shot: next(seq))
        b = _Obs(screenshot_path=str(tmp_path / "a.bmp"))
        a = _Obs(screenshot_path=str(tmp_path / "b.bmp"))
        assert v.verify(_step("pixel"), b, a).verdict == "ok"


# ==========================================================================
# 6. UIA 后端
# ==========================================================================
class TestUiaBackend:
    def test_no_target_is_unknown(self, no_uia):
        s = Step(id="s1", action="click", params={})
        s.verify = {"kind": "uia"}
        assert verify(s, None, None, uia_find=no_uia).verdict == "unknown"

    def test_element_not_found_is_unknown(self, no_uia):
        """★★ 找不到元素是 unknown 不是 fail ——
        可能只是定位策略不对，当成 fail 会让重试用同一个策略。"""
        r = verify(_step("uia", target="不存在"), None, None,
                   uia_find=no_uia)
        assert r.verdict == "unknown"

    def test_value_matches(self):
        class _El:
            name = "保存按钮"
            value = "已保存"

        r = verify(_step("uia", target="保存按钮", expect="已保存"),
                   None, None, uia_find=lambda n: _El())
        assert r.verdict == "ok"

    def test_value_mismatch_is_fail(self):
        class _El:
            name = "保存按钮"
            value = "未保存"

        r = verify(_step("uia", target="保存按钮", expect="已保存"),
                   None, None, uia_find=lambda n: _El())
        assert r.verdict == "fail"

    def test_no_expect_means_existence(self):
        class _El:
            name = "按钮"
            value = ""

        r = verify(_step("uia", target="按钮"), None, None,
                   uia_find=lambda n: _El())
        assert r.verdict == "ok"

    def test_lookup_exception_is_unknown(self):
        def _boom(name):
            raise RuntimeError("UIA 挂了")

        r = verify(_step("uia", target="x"), None, None, uia_find=_boom)
        assert r.verdict == "unknown"


# ==========================================================================
# 7. DOM 后端
# ==========================================================================
class TestDomBackend:
    def test_no_url_is_unknown(self):
        assert verify(_step("dom", expect="https://a.com"),
                      None, _Obs()).verdict == "unknown"

    def test_url_match(self):
        r = verify(_step("dom", expect="https://a.com/x"), None,
                   _Obs(url="https://a.com/x"))
        assert r.verdict == "ok"

    def test_url_mismatch_is_fail(self):
        r = verify(_step("dom", expect="https://a.com/x"), None,
                   _Obs(url="https://b.com/y"))
        assert r.verdict == "fail"

    @pytest.mark.parametrize("got,expect", [
        ("https://a.com/x?t=1", "https://a.com/x"),
        ("https://a.com/x#top", "https://a.com/x"),
        ("https://a.com/x/", "https://a.com/x"),
        ("http://a.com/x", "https://a.com/x"),
        ("https://A.COM/X", "https://a.com/x"),
    ])
    def test_url_normalization(self, got, expect):
        """★★ 忽略 query/fragment/尾斜杠/协议/大小写 ——
        否则很多站点每步都判失败（它们每步都改 query）。"""
        from avatar.agentic_planner.verifier import _url_match
        assert _url_match(got, expect) is True, (got, expect)

    def test_url_clearly_different(self):
        from avatar.agentic_planner.verifier import _url_match
        assert _url_match("https://a.com/x", "https://b.com/y") is False

    def test_empty_url_no_crash(self):
        from avatar.agentic_planner.verifier import _url_match
        assert _url_match("", "https://a.com") is False
        assert _url_match("https://a.com", "") is False


# ==========================================================================
# 8. 证据
# ==========================================================================
class TestEvidence:
    def test_success_carries_evidence(self):
        r = verify(_step("dom", expect="https://a.com/x"), None,
                   _Obs(url="https://a.com/x"))
        assert r.evidence

    def test_failure_carries_evidence(self):
        r = verify(_step("dom", expect="https://a.com/x"), None,
                   _Obs(url="https://b.com/y"))
        assert r.evidence and "url" in r.evidence

    def test_to_dict_works(self):
        r = verify(_step("elapsed"), None, None)
        assert isinstance(r.to_dict(), dict)


# ==========================================================================
# 9. 不碰外部
# ==========================================================================
class TestIsolation:
    def test_no_image_library(self):
        """★ pHash 复用阶段四 —— 不自己 import 图像库。"""
        import ast
        import avatar.agentic_planner.verifier as V
        tree = ast.parse(open(V.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"PIL", "cv2", "numpy"}), sorted(mods)

    def test_no_reverse_dependency(self):
        """★ verifier 不该 import 执行器 / 恢复器（单向依赖）。"""
        import ast
        import avatar.agentic_planner.verifier as V
        tree = ast.parse(open(V.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"executor", "recovery", "planner", "memory"})

    def test_uia_lookup_is_injectable(self):
        """★ 真实 UIA 查找是全树遍历（几十秒）—— 必须可注入。"""
        import inspect
        assert "find_fn" in inspect.signature(
            UiaStateVerifier.__init__).parameters
