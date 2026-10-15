# -*- coding: utf-8 -*-
"""阶段七 · WinRT 地基（``_win32/winrt``）。

## 这个文件在守什么

手搓 WinRT 最容易错的三处：

1. **IID 记错** —— 没有任何可推导规律，错一位就是调别的接口
2. **vtable 槽位记错** —— 尤其 `TryCreateFromUserProfileLanguages`
   在 `[10]` 不是 `[9]`
3. **HSTRING 生命周期** —— `WindowsCreateStringReference` 不复制字符串，
   缓冲区被回收就是野指针

前两条**静态可测**（对着表断言），第三条要真跑一次。

## 无 WinRT 环境

`combase.dll` 不存在时（非 Windows），涉及真实调用的测试自动 skip。
"""
from __future__ import annotations

import ctypes

import pytest

from avatar._win32 import winrt as W

_HAS_DLL = bool(W.ro_state().get("dll_loaded"))
_needs_dll = pytest.mark.skipif(not _HAS_DLL,
                                reason="combase.dll 不可用")


class TestContract:
    def test_docstring_has_boundary(self):
        assert "能力边界" in (W.__doc__ or "")

    def test_header_is_32_bytes(self):
        """HSTRING_HEADER 的 ABI 尺寸是 32 字节（x64）。"""
        assert ctypes.sizeof(W.HSTRING_HEADER) == 32

    def test_inspectable_prefix_is_6(self):
        assert W.VTBL_IINSPECTABLE_SIZE == 6

    def test_all_ocr_slots_at_or_after_6(self):
        """★ 槽位不能低于 6 —— 那会覆盖 IInspectable 的固定布局。"""
        tables = (W.VTBL_OCR_ENGINE, W.VTBL_OCR_ENGINE_STATICS,
                  W.VTBL_OCR_RESULT, W.VTBL_OCR_LINE, W.VTBL_OCR_WORD,
                  W.VTBL_SOFTWARE_BITMAP_STATICS)
        for t in tables:
            for name, slot in t.items():
                assert slot >= 6, "%s 的 %s 在 [%d]" % (t, name, slot)

    def test_activation_factory_slot(self):
        assert W.VTBL_IActivation_FACTORY["ActivateInstance"] == 6


class TestCriticalSlots:
    """★ 这几条是"凭印象写就会错"的地方。"""

    def test_try_create_from_user_profile_is_10(self):
        """★★ 是 [10]，不是 [9]。

        写成 [9] 会调到 `TryCreateFromLanguage` ——
        它的第一个参数是 `ILanguage*`（输入），
        而这里会塞一个输出指针进去，行为未定义。
        """
        assert W.VTBL_OCR_ENGINE_STATICS[
            "TryCreateFromUserProfileLanguages"] == 10

    def test_try_create_from_language_is_9(self):
        assert W.VTBL_OCR_ENGINE_STATICS["TryCreateFromLanguage"] == 9

    def test_is_language_supported_is_8(self):
        assert W.VTBL_OCR_ENGINE_STATICS["IsLanguageSupported"] == 8

    def test_recognize_async_is_6(self):
        assert W.VTBL_OCR_ENGINE["RecognizeAsync"] == 6

    def test_create_copy_from_buffer_is_9(self):
        assert W.VTBL_SOFTWARE_BITMAP_STATICS["CreateCopyFromBuffer"] == 9

    def test_buffer_byte_access_is_3(self):
        """★ `IBufferByteAccess` 是**经典 COM**（没有 IInspectable），
        所以它的第一个方法在 [3] 而不是 [6]。"""
        assert W.VTBL_IBUFFER_BYTE_ACCESS["Buffer"] == 3

    def test_async_slots(self):
        assert (W._VTBL_ASYNC_GET_RESULTS,
                W._VTBL_ASYNC_GET_STATUS,
                W._VTBL_ASYNC_GET_ERRORCODE,
                W._VTBL_ASYNC_CANCEL,
                W._VTBL_ASYNC_CLOSE) == (8, 10, 11, 12, 13)


class TestIIDs:
    def test_all_iids_parse_and_roundtrip(self):
        from avatar._win32.guids import guid
        iids = {
            "IActivationFactory": W.IID_IActivationFactory,
            "IOcrEngine": W.IID_OCR_ENGINE,
            "IOcrEngineStatics": W.IID_OCR_ENGINE_STATICS,
            "IOcrResult": W.IID_OCR_RESULT,
            "IOcrLine": W.IID_OCR_LINE,
            "IOcrWord": W.IID_OCR_WORD,
            "ISoftwareBitmapStatics": W.IID_SOFTWARE_BITMAP_STATICS,
            "IBufferByteAccess": W.IID_IBUFFER_BYTE_ACCESS,
        }
        for name, s in iids.items():
            assert str(guid(s)).upper() == s.upper(), name

    def test_ocr_engine_statics_exact_value(self):
        """★ 这个值是查证过的（windows-rs 绑定），不是凭印象。"""
        assert W.IID_OCR_ENGINE_STATICS.upper() == \
            "{5BFFA85A-3384-3540-9940-699120D428A8}"

    def test_guids_module_has_winrt_table(self):
        from avatar._win32 import guids as G
        assert len(G.NAMED_WINRT_IIDS) >= 14
        assert G.winrt_iid_of("IOcrEngineStatics")


@_needs_dll
class TestRealCalls:
    def test_ro_initialize_returns_hresult(self):
        assert isinstance(W.ro_initialize(), int)

    def test_ro_initialize_idempotent(self):
        assert W.ro_initialize() in (0, 1)

    @pytest.mark.parametrize("text", [
        "", "abc", "记事本", "Windows.Media.Ocr.OcrEngine",
        "mixed 中英 123",
    ])
    def test_hstring_roundtrip(self, text):
        with W.HString(text) as hs:
            assert hs.raw() == text

    def test_hstring_ref_roundtrip(self):
        """★ `WindowsCreateStringReference` 不复制字符串 ——
        返回的 HStringRef 必须锚定住缓冲区。"""
        ref = W.hstring_ref("Windows.Media.Ocr.OcrEngine")
        assert ref.raw() == "Windows.Media.Ocr.OcrEngine"

    def test_hstring_duplicate_independent(self):
        with W.HString("dup") as a:
            b = a.duplicate()
            try:
                assert b.raw() == "dup"
            finally:
                b.close()

    def test_read_hstring_none_safe(self):
        assert W.read_hstring(None) == ""

    def test_release_none_safe(self):
        assert W.release(None) == 0

    def test_vtbl_none_raises_not_crash(self):
        with pytest.raises(W.WinRtError):
            W.vtbl(None, 6)

    def test_get_iids_none_returns_empty(self):
        assert W.get_iids(None) == []

    def test_activate_missing_class_raises(self):
        with pytest.raises(W.WinRtError):
            W.activation_factory("Windows.Media.Ocr.不存在的东西")

    def test_activate_empty_name_raises(self):
        with pytest.raises(W.WinRtError):
            W.activation_factory("")


class TestDiagnostics:
    def test_ro_state_never_raises(self):
        assert isinstance(W.ro_state(), dict)

    def test_describe_never_raises(self):
        assert isinstance(W.describe_winrt(), str)

    def test_describe_lists_slots(self):
        assert "TryCreateFromUserProfileLanguages" in W.describe_winrt()

    def test_winrt_ptr_falsy_when_empty(self):
        assert bool(W.WinRtPtr(None)) is False

    def test_no_third_party_imports(self):
        """★ 零 SDK —— 只用 ctypes。"""
        import ast
        tree = ast.parse(open(W.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"win32com", "clr", "winrt", "comtypes"}), mods
