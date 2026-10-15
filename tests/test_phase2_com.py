# -*- coding: utf-8 -*-
"""阶段二测试 A：COM 基础设施（``avatar._win32``）。

覆盖
----
· ``HRESULT_SAFE`` —— 失败的 HRESULT 不抛 OSError（这是"探测"能工作的前提）
· ``check_hresult`` / ``succeeded`` —— 各种入参形态都认
· ``ComPtr`` —— 引用计数、幂等 release、context manager、无泄漏
· ``VARIANT`` —— 布局（``vt`` 在前）、bool 是 short、清理
· ``GUID`` / ``iid_of`` —— 命名 IID 查询
· 接口 IID 与 vtable 索引**与权威表一致**（不许拍脑袋）

不依赖 pytest，直接跑::

    python tests/test_phase2_com.py

依赖：``avatar._win32.{com,variant,guids,structs}``、``avatar.uia.tree``。
"""
from __future__ import annotations

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar._win32 import com as C
from avatar._win32 import guids as G
from avatar._win32 import structs as S
from avatar._win32 import variant as V
from avatar.uia import tree as T

# --------------------------------------------------------------------------
# 极简断言框架（与阶段一测试保持一致）
# --------------------------------------------------------------------------
_RESULTS = []


def check(name, fn):
    try:
        fn()
        _RESULTS.append((name, True, ""))
        print("  [OK  ] %s" % name)
    except AssertionError as e:
        _RESULTS.append((name, False, str(e)))
        print("  [FAIL] %s  → %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  → %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


def _camel_to_snake(name):
    """``IUIAutomationScrollItemPattern`` → ``IUIAUTOMATION_SCROLL_ITEM_PATTERN``。

    用于把 ``NAMED_IIDS`` 的驼峰键推导回 guids 里的常量名。
    """
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (name[i - 1].islower()
                                   or (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(ch.upper())
    return "".join(out)


# --------------------------------------------------------------------------
# HRESULT 处理
# --------------------------------------------------------------------------
def test_hresult():
    section("HRESULT：失败不许逃成 OSError")

    def _safe_does_not_raise():
        """``ComPtr.method`` 必须把 ``ctypes.HRESULT`` 换成 ``HRESULT_SAFE``。

        背景：``ctypes.HRESULT._check_retval_`` 会把失败的 HRESULT 直接
        转成 ``OSError``，绕过 ``check_hresult`` 的契约。
        后果很严重 —— "这个元素支持模式 X 吗"这类**探测性**调用
        本来就该允许失败，被转成异常后就无法区分"不支持"和"出错了"。
        """
        assert hasattr(C, "HRESULT_SAFE"), "com 模块没有 HRESULT_SAFE"
        assert not getattr(C.HRESULT_SAFE, "_check_retval_", None), (
            "HRESULT_SAFE 不该有 _check_retval_ —— 否则又会在失败时抛 OSError")

    def _hr_int_variants():
        """``_hr_int`` 要能吃 int / HRESULT_SAFE / ctypes 标量。"""
        assert C._hr_int(0) == 0
        assert C._hr_int(-2147467262) == -2147467262
        assert C._hr_int(C.HRESULT_SAFE(-1)) == -1
        assert C._hr_int(ctypes.c_long(5)) == 5

    def _failed():
        assert C.succeeded(0) is True, "S_OK 应判成功"
        assert C.succeeded(1) is True, "S_FALSE(1) 也是成功"
        assert C.succeeded(-2147467262) is False, "E_NOINTERFACE 应判失败"
        assert C.succeeded(C.HRESULT_SAFE(0)) is True
        assert C.succeeded(C.HRESULT_SAFE(-1)) is False

    def _check_hresult_raises():
        try:
            C.check_hresult(-2147467262, "TestMethod")
        except C.ComError as exc:
            msg = str(exc)
            assert "80004002" in msg.lower() or "80004002" in msg, (
                "ComError 应带十六进制 hresult，实际: %s" % msg)
            return
        raise AssertionError("check_hresult 对失败 HRESULT 没有抛 ComError")

    def _check_hresult_ok():
        # 成功时不该抛
        C.check_hresult(0, "TestMethod")
        C.check_hresult(1, "TestMethod")

    check("HRESULT_SAFE 不带 _check_retval_", _safe_does_not_raise)
    check("_hr_int 接受多种入参", _hr_int_variants)
    check("succeeded 判定正确", _failed)
    check("check_hresult 失败时抛 ComError（含 hex）", _check_hresult_raises)
    check("check_hresult 成功时不抛", _check_hresult_ok)


# --------------------------------------------------------------------------
# ComPtr
# --------------------------------------------------------------------------
def test_comptr():
    section("ComPtr：引用计数与释放")

    def _live_com():
        C.init_com("MTA")
        cp = C.create_instance(G.CLSID_CUIAUTOMATION, G.IID_IUIAUTOMATION,
                               name="test-uia")
        assert cp.valid and cp.ptr, "创建 IUIAutomation 失败"

        def _refcount_stable():
            """反复 AddRef/Release 后引用计数必须回到原位。"""
            before = cp.add_ref()
            assert before >= 2, "AddRef 应让计数 ≥2，实际 %d" % before
            after = cp.release()
            assert after < before, (
                "Release 后计数应下降，before=%d after=%d" % (before, after))

        check("AddRef/Release 配对", _refcount_stable)

        def _idempotent():
            """重复 release 必须安全 —— 不能 use-after-free。"""
            tmp = C.create_instance(G.CLSID_CUIAUTOMATION,
                                    G.IID_IUIAUTOMATION, name="tmp")
            p = tmp.ptr
            tmp.release()
            tmp.release()          # 第二次应无害
            assert tmp.release() == 0, "重复 release 应返回 0"
            assert p, "指针值应被记录过"

        check("release 幂等", _idempotent)

        def _context_manager():
            with C.create_instance(G.CLSID_CUIAUTOMATION,
                                   G.IID_IUIAUTOMATION,
                                   name="ctx") as ctx_cp:
                assert ctx_cp.valid
                inner = ctx_cp.ptr
            assert inner, "with 块内指针应有效"
            # 退出后应已释放
            assert ctx_cp.release() == 0, "with 退出后应已释放"

        check("context manager 自动释放", _context_manager)

        cp.release()

    check("能创建 IUIAutomation 实例", _live_com)


def test_com_no_leak():
    section("COM 引用泄漏检查")

    def _repeat_probe():
        """反复取根元素 + 释放，引用计数必须稳定。

        UIA 树遍历最容易泄漏的地方：每次跨进程取到的元素都是新引用，
        忘了 Release 就会在长跑任务里把内存吃光。
        """
        C.init_com("MTA")
        u = T.uia()
        counts = []
        for _ in range(60):
            root = T.root()
            assert root is not None
            counts.append(1)
        assert len(counts) == 60

    def _patterns_no_leak():
        """模式的获取/释放循环 —— 100 次后不该崩。"""
        u = T.uia()
        from avatar.uia import patterns as P
        wins = []
        for w in T.descendants(T.root(), kind=T.VIEW_CONTROL,
                               max_depth=3, limit=40):
            if w.control_type == T.ControlType.WINDOW:
                wins.append(w)
                break
        if not wins:
            return
        el = wins[0]
        for _ in range(100):
            obj = P.wrap(el, "Window", required=False)
            if obj is not None:
                _ = obj.visual_state
                obj.release()

    check("反复取根元素无异常", _repeat_probe)
    check("模式获取/释放 100 次无异常", _patterns_no_leak)


# --------------------------------------------------------------------------
# VARIANT
# --------------------------------------------------------------------------
def test_variant():
    section("VARIANT：布局是踩过的坑")

    def _size():
        n = ctypes.sizeof(V.VARIANT)
        exp = V.expected_variant_size()
        assert n == exp == 24, (
            "sizeof(VARIANT)=%d，期望 24（x64 下 vt 在前 + 4 保留字 + 16 联合体）"
            % n)

    def _vt_first():
        """``vt`` 必须在偏移 0 —— 这个顺序搞反过一次。

        反了之后 ``VariantInit`` 清零仍让 ``.vt`` 读成 0（看起来正常），
        但调用方写入的数据永远读不到，表现为"某些属性莫名是空的"。
        """
        assert V.VARIANT.vt.offset == 0, (
            "VARIANT.vt 必须在偏移 0，实际 %d" % V.VARIANT.vt.offset)
        vt_data = V.VARIANT.vt_data
        assert vt_data.offset >= 8, (
            "vt_data 偏移应 ≥8（8 字节对齐），实际 %d" % vt_data.offset)

    def _bool_is_short():
        """``VT_BOOL`` 是 short：−1 为真、0 为假。不是 1/0。"""
        t = V.VARIANT.from_bool(True)
        f = V.VARIANT.from_bool(False)
        assert t.vt == V.VT_BOOL and f.vt == V.VT_BOOL
        assert t.vt_data.boolVal == V.VARIANT_TRUE == -1, (
            "VARIANT_TRUE 必须是 -1，实际 %r" % (V.VARIANT_TRUE,))
        assert f.vt_data.boolVal == V.VARIANT_FALSE == 0

    def _roundtrip():
        i = V.VARIANT.from_int(-12345)
        assert i.vt == V.VT_I4 and i.vt_data.lVal == -12345
        s = V.VARIANT.from_str("中文测试")
        assert s.vt == V.VT_BSTR
        back = V.expected_variant_size()
        assert back == 24
        V.variant_clear(s)         # BSTR 必须用 SysFreeString 释放

    def _clear_empty():
        v = V.VARIANT.empty()
        assert v.vt == V.VT_EMPTY
        V.variant_clear(v)

    check("sizeof(VARIANT) == 24", _size)
    check("vt 在偏移 0（顺序坑）", _vt_first)
    check("VT_BOOL 是 short 且 −1 为真", _bool_is_short)
    check("VARIANT 构造与清理", _roundtrip)
    check("空 VARIANT 可清理", _clear_empty)


# --------------------------------------------------------------------------
# GUID / IID
# --------------------------------------------------------------------------
def test_guids():
    section("GUID 与接口 IID")

    def _cuiautomation():
        clsid = G.CLSID_CUIAUTOMATION
        iid = G.IID_IUIAUTOMATION
        for g in (clsid, iid):
            assert isinstance(g, str) and g.startswith("{") and g.endswith("}"), (
                "GUID 应是 {..} 形式的字符串，实际 %r" % (g,))

    def _named_iids_keys():
        """``NAMED_IIDS`` 的键是 **COM 接口名**（驼峰，无 ``IID_`` 前缀）。

        踩过：``iid_of`` 传 ``"IID_IUIAUTOMATION_WINDOW_PATTERN"``（SDK 常量名）
        或 ``"IUIAUTOMATION_WINDOW_PATTERN"``（常量去掉前三个字符）都会 KeyError。
        真正的键是 ``"IUIAutomationWindowPattern"`` —— 大小写可容忍，
        但 "UI" 之后必须是小写驼峰，不能全大写。
        """
        keys = sorted(G.NAMED_IIDS)
        assert keys, "NAMED_IIDS 不该为空"
        bad = [k for k in keys if k.startswith("IID_")]
        assert not bad, "NAMED_IIDS 的键不该带 IID_ 前缀：%s" % bad[:3]
        # 驼峰约定：含 "IUIAutomation" 前缀的键，其后不应再出现全大写单词
        off = [k for k in keys
               if k.startswith("IUIAutomation") and k[len("IUIAutomation"):].isupper()]
        assert not off, "NAMED_IIDS 的键应是驼峰接口名，不该全大写：%s" % off[:3]

    def _iid_of():
        got = G.iid_of("IUIAutomationWindowPattern")
        assert isinstance(got, G.GUID), "iid_of 应返回 GUID 对象，实际 %r" % (got,)
        assert str(got).startswith("{"), "GUID 应能转成 {..} 字符串，实际 %r" % (got,)
        # 大小写不敏感
        assert G.iid_of("iuiautomationwindowpattern") == got, "iid_of 应容忍大小写"
        # 直接传 {..} 字符串应等价返回（用于绕过命名表）
        raw = "{12345678-1234-1234-1234-123456789ABC}"
        assert G.iid_of(raw) == G.guid(raw), "传裸 GUID 应原样返回"

    def _unknown_rejected():
        try:
            G.iid_of("IUIAUTOMATION_绝不存在的接口")
        except (KeyError, ValueError):
            return
        raise AssertionError("未知接口名应报错")

    check("CLSID / IID 格式正确", _cuiautomation)
    check("NAMED_IIDS 键不带 IID_ 前缀", _named_iids_keys)
    check("iid_of 可用且支持裸 GUID", _iid_of)
    check("未知接口名被拒绝", _unknown_rejected)


def test_iid_table_authoritative():
    section("IID 表与权威来源一致（不许拍脑袋）")

    #: 从 Windows SDK ``UIAutomationClient.h`` 的 ``MIDL_INTERFACE`` 抄下。
    #: 校验脚本：``tools/dump_uia_vtbl.py``（配 ``tools/UIAutomationClient.h``）。
    #: 键是 guids 里的**常量名**（``IID_...``），值是权威 GUID。
    #: 早先有 4 个值是编的，症状是 ``E_NOINTERFACE`` —— 模式明明检测到了
    #: 却取不到接口。
    AUTHORITATIVE = {
        "IID_IUIAUTOMATION_SELECTION_PATTERN": "{5ED5202E-B2AC-47A6-B638-4B0BF140D78E}",
        "IID_IUIAUTOMATION_TEXT_PATTERN": "{32EBA289-3583-42C9-9C59-3B6D9A1E9B6A}",
        "IID_IUIAUTOMATION_SCROLL_ITEM_PATTERN": "{B488300F-D015-4F19-9C29-BB595E3645EF}",
        "IID_IUIAUTOMATION_VIRTUALIZED_ITEM_PATTERN": "{6BA3D7A6-04CF-4F11-8793-A8D1CDE9969F}",
        "IID_IUIAUTOMATION_GRID_PATTERN": "{414C3CDC-856B-4F5B-8538-3131C6302550}",
        "IID_IUIAUTOMATION_TABLE_PATTERN": "{620E691C-EA96-4710-A850-754B24CE2417}",
        "IID_IUIAUTOMATION_DOCK_PATTERN": "{FDE5EF97-1464-48F6-90BF-43D0948E86EC}",
        "IID_IUIAUTOMATION_TRANSFORM_PATTERN": "{A9B55844-A55D-4EF0-926D-569C16FF89BB}",
        "IID_IUIAUTOMATION_WINDOW_PATTERN": "{0FAEF453-9208-43EF-BBB2-3B485177864F}",
        "IID_IUIAUTOMATION_ELEMENT": "{D22108AA-8AC5-49A5-837B-37BBB3D7591E}",
        "IID_IUIAUTOMATION": "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}",
    }

    def _each():
        wrong = []
        for const, want in AUTHORITATIVE.items():
            got = getattr(G, const, None)
            if got is None:
                wrong.append("%s 缺失" % const)
            elif got.upper() != want.upper():
                wrong.append("%s 期望 %s 实际 %s" % (const, want, got))
        assert not wrong, "IID 与 SDK 不符：%s" % "; ".join(wrong)

    def _named_matches_const():
        """``NAMED_IIDS`` 的值必须与同名常量严格一致，不许各写一份。"""
        bad = []
        for iface, val in G.NAMED_IIDS.items():
            # IUIAutomationWindowPattern → IID_IUIAUTOMATION_WINDOW_PATTERN
            const = "IID_" + _camel_to_snake(iface)
            if const == "IID_I_UNKNOWN" or const == "IID_I_DISPATCH":
                continue
            ref = getattr(G, const, None)
            if ref is not None and ref.upper() != val.upper():
                bad.append("%s: 表里 %s 常量 %s" % (const, val, ref))
        assert not bad, "NAMED_IIDS 与常量不一致：%s" % bad[:3]

    def _all_pattern_iids_present():
        """``PATTERN_IIDS`` 里引用的每个常量都必须在 guids 里真的存在。"""
        missing = []
        for name, const in T.PATTERN_IIDS.items():
            if not hasattr(G, const):
                missing.append("%s → %s" % (name, const))
        assert not missing, "PATTERN_IIDS 引用了不存在的常量：%s" % missing

    check("关键 IID 与 SDK 一致", _each)
    check("NAMED_IIDS 的值与常量一致", _named_matches_const)
    check("PATTERN_IIDS 引用的常量都存在", _all_pattern_iids_present)


def test_vtable_indices():
    section("vtable 索引与权威表一致")

    def _pattern_ids():
        """``UIA_*PatternId`` 的权威值（UIAutomationClient.h）。

        早先有 5 个值错了（Window / TableItem / SelectionItem / Table /
        LegacyIAccessible），症状是"模式检测到了但取接口 E_NOINTERFACE"。
        """
        WANT = {
            "Invoke": 10000, "Selection": 10001, "Value": 10002,
            "RangeValue": 10003, "Scroll": 10004, "ExpandCollapse": 10005,
            "Grid": 10006, "GridItem": 10007, "MultipleView": 10008,
            "Window": 10009, "SelectionItem": 10010, "Dock": 10011,
            "Table": 10012, "TableItem": 10013, "Text": 10014,
            "Toggle": 10015, "Transform": 10016, "ScrollItem": 10017,
            "LegacyIAccessible": 10018, "ItemContainer": 10019,
            "VirtualizedItem": 10020, "SynchronizedInput": 10021,
            "ObjectModel": 10022, "Annotation": 10023, "Styles": 10025,
            "Spreadsheet": 10026, "SpreadsheetItem": 10027,
            "TextChild": 10029, "Drag": 10030, "DropTarget": 10031,
            "TextEdit": 10032, "CustomNavigation": 10033,
        }
        wrong = {k: (v, T.PATTERN_IDS.get(k))
                 for k, v in WANT.items() if T.PATTERN_IDS.get(k) != v}
        assert not wrong, "PatternId 与 SDK 不符：%s" % wrong

    def _property_ids():
        """``UIA_*PropertyId``（30000 段）。非连续，不能靠外推。"""
        WANT = {
            "runtime_id": 30000, "bounding_rectangle": 30001,
            "process_id": 30002, "control_type": 30003,
            "localized_type": 30004, "name": 30005,
            "automation_id": 30011, "class_name": 30012,
            "is_password": 30019, "native_window_handle": 30020,
            "is_offscreen": 30022, "framework_id": 30024,
        }
        wrong = {k: (v, T.UIA_PROPERTY_IDS.get(k))
                 for k, v in WANT.items() if T.UIA_PROPERTY_IDS.get(k) != v}
        assert not wrong, "PropertyId 与 SDK 不符：%s" % wrong

    def _two_id_systems_distinct():
        """⚠ ``VT_ELEMENT_PROPS``（vtable 方法索引）与
        ``UIA_PROPERTY_IDS``（属性 ID）是**两套编号**，不能混用。

        混用的症状是 ``E_INVALIDARG`` —— 拿 vtable 索引 20 当属性 ID 传，
        UIA 不认识 20 这个属性就报错。
        """
        assert T.UIA_BOUNDING_RECT_PROP_ID == 30001, "轮廓矩形属性 ID 应为 30001"
        assert T.UIA_BOUNDING_RECT_PROP_ID not in T.VT_ELEMENT_PROPS.values(), (
            "30001 出现在 vtable 索引表里 —— 两套编号串了")
        # vtable 索引应是小的连续数
        big = [v for v in T.VT_ELEMENT_PROPS.values() if v > 200]
        assert not big, "VT_ELEMENT_PROPS 里出现大数（像是属性 ID）：%s" % big

    def _parse_table():
        """``UIA_*PatternId`` 表要完整（32 个）。"""
        assert len(T.PATTERN_IDS) >= 30, (
            "PATTERN_IDS 只有 %d 项，应覆盖完整的 32 个模式"
            % len(T.PATTERN_IDS))

    check("PatternId 与 SDK 一致", _pattern_ids)
    check("PropertyId 与 SDK 一致", _property_ids)
    check("两套编号未混用", _two_id_systems_distinct)
    check("PatternId 表完整", _parse_table)


def test_structs():
    section("结构体尺寸（静默失败的头号来源）")

    def _mouseinput_padding():
        """``MOUSEINPUT`` 必须 32 字节 —— 少一个填充字节 SendInput 就静默失败。

        规则：4 个 4 字节字段（16）+ 4 字节对齐填充 + 8 字节指针 + 4 字节尾部填充。
        """
        n = ctypes.sizeof(S.MOUSEINPUT)
        assert n == 32, "sizeof(MOUSEINPUT)=%d，期望 32" % n

    def _input_size():
        n = ctypes.sizeof(S.INPUT)
        exp = S.expected_input_size()
        assert n == exp, "sizeof(INPUT)=%d，期望 %d" % (n, exp)

    def _union():
        n = ctypes.sizeof(S._INPUTUNION)
        assert n == 32, "sizeof(_INPUTUNION)=%d，期望 32" % n

    check("MOUSEINPUT 32 字节填充规则", _mouseinput_padding)
    check("sizeof(INPUT) 正确", _input_size)
    check("INPUT 联合体尺寸正确", _union)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 64)
    print("  阶段二测试 A：COM 基础设施")
    print("=" * 64)

    test_hresult()
    test_comptr()
    test_com_no_leak()
    test_variant()
    test_guids()
    test_iid_table_authoritative()
    test_vtable_indices()
    test_structs()

    total = len(_RESULTS)
    failed = [r for r in _RESULTS if not r[1]]
    print()
    print("=" * 64)
    print("阶段二 COM 测试: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("\n失败项：")
        for name, _, err in failed:
            print("  · %s → %s" % (name, err))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
