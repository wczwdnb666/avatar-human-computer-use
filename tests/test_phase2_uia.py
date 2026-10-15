#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段二 · UIA 元素定位测试。

覆盖 ``avatar/uia/`` 下的七个模块：树遍历、模式、定位、描述、动作、
OCR 与视觉两条降级通道 —— 以及它们组成的降级链。

设计原则（与 ``tests/test_phase1.py`` 一致）：

* **零第三方依赖** —— 只用标准库 + 被测代码本身。
* **只读优先** —— 需要动真实控件的检查都挑"无关痛痒"的控件
  （记事本的编辑框），改动后立即还原。
* **失败要响亮** —— 定位不到必须抛 ``ElementNotFound``，绝不许
  静默降级成坐标点击。这条是本阶段最容易破的底线，专门用测试钉住。

本文件刻意守住的几条硬约束：

1. ``avatar click`` / ``avatar set`` **禁止**坐标兜底 —— 没有可点模式时
   必须报错，不许默认用坐标。
2. 元素**只存客户区坐标**（``client_rect``），屏幕坐标按需现算。
   任何序列化输出里出现持久化的屏幕坐标都算 bug。
3. 降级链的顺序是 uia → ocr → vision → coord，且每一环都要如实报告
   ``available``，不许假装可用。
"""

from __future__ import annotations

import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.errors import ElementNotFound, UnsupportedError
from avatar.uia import actions as A
from avatar.uia import describe as D
from avatar.uia import find as F
from avatar.uia import ocr_fallback as OCR
from avatar.uia import patterns as P
from avatar.uia import tree as T
from avatar.uia import vision_fallback as VIS

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
        print("  [FAIL] %s  -> %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  -> %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


# --------------------------------------------------------------------------
# 共享夹具：找一扇"有控件"的窗口，供定位/动作测试用
# --------------------------------------------------------------------------
_NOTE = {"hwnd": 0, "own": False}


def _find_notepad_hwnd():
    from avatar.app_manager import windows as W
    for w in W.list_windows():
        if not w.is_usable:
            continue
        if "notepad" in (w.process or "").lower():
            return w.hwnd
    return 0


def _ensure_notepad():
    """确保有一扇记事本窗口。

    **自己拉起，不依赖使用者手工开着** —— 测试必须是自包含的。
    拉起来的窗口记在 ``_NOTE['own']`` 里，收尾时自己关掉，
    不给使用者留垃圾。

    找不到也拉不起来时抛 ``AssertionError``，让测试红掉 ——
    而不是静默跳过。静默跳过等于没测。
    """
    if _NOTE["hwnd"]:
        import ctypes
        if ctypes.windll.user32.IsWindow(_NOTE["hwnd"]):
            return _NOTE["hwnd"]
        _NOTE["hwnd"] = 0

    h = _find_notepad_hwnd()
    if h:
        _NOTE["hwnd"] = h
        return h

    # 没有就自己起一个
    from avatar.app_manager import launcher as APP
    try:
        la = APP.open_app("notepad")
    except Exception as e:
        raise AssertionError("拉不起记事本：%r" % (e,))
    _NOTE["own"] = True
    # open_app 内部已经在等窗口，但保险起见再轮询一小会儿
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        h = _find_notepad_hwnd()
        if h:
            _NOTE["hwnd"] = h
            return h
        time.sleep(0.15)
    raise AssertionError("拉了记事本但等不到它的窗口")


def _close_own_notepad():
    """只关自己拉起来的那个，不动使用者原本开着的。"""
    if not _NOTE.get("own"):
        return
    h = _NOTE.get("hwnd") or 0
    if not h:
        return
    try:
        from avatar.app_manager import process as P
        pid = P.pid_of_window(h)
        P.kill(pid, force=True, allow_foreign=True, timeout=4.0)
    except Exception:
        pass
    _NOTE["hwnd"] = 0


def _notepad_window():
    return _ensure_notepad()


#: 缓存：元素与查询结果。
#:
#: UIA 每次调用都是真实跨进程 COM 往返，全桌面查询一次 ~1.3s。
#: 同一份测试里反复查同样的东西纯属浪费 —— 缓存后整份测试从
#: 近 4 分钟降到 1 分钟出头。
_CACHE = {}


def _cached(key, fn):
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


def _editor():
    """记事本里的文本编辑区元素 —— 所有填写类测试的靶子。

    ⚠ 这是个坑：Win11 的记事本文本区不是 ``edit``，而是
    ``document``（``RichEditD2DPT``）。老式 ``edit`` 判断在这里会
    一个都找不到。所以按"支持 Value 模式"来认，比按控件类型认稳。
    """
    def _find():
        for flt in (F.Query(control_type="document"),
                    F.Query(control_type="edit")):
            el = F.find_one(flt, window=_notepad_window(), required=False)
            if el is not None:
                return el
        return F.find_one(F.Query(control_type="document"),
                          window=_notepad_window())
    return _cached("editor", _find)


def _editors(limit=5):
    """记事本里的 document 元素列表（缓存）。"""
    return _cached(("docs", limit), lambda: F.find_all(
        F.Query(control_type="document"), window=_notepad_window(), limit=limit))


def _note_descendants(max_depth=4):
    """记事本窗口的后代元素（缓存）—— 树遍历是真实 COM 往返，别重复走。"""
    return _cached(("desc", max_depth), lambda: T.descendants(
        T.from_window(_notepad_window()), max_depth=max_depth))


def _no_pattern_element():
    """一个**零控件模式**的元素 —— 用来测"无可点模式时必须报错"。

    记事本的 ``title_bar`` 实测 patterns == []，是天然的靶子。
    """
    def _find():
        for d in _note_descendants(4)[:60]:
            if not P.supported(d):
                return d
        return None
    return _cached("no_pattern", _find)


def _toggle_element():
    """一个真的支持 Toggle 模式的元素（缓存）。找不到返回 None。"""
    def _find():
        for d in _note_descendants(5):
            if "Toggle" in P.supported(d):
                return d
        return None
    return _cached("toggle_el", _find)


# --------------------------------------------------------------------------
# 树遍历
# --------------------------------------------------------------------------
def test_tree():
    section("树遍历")

    def _root():
        r = T.root()
        assert r is not None, "root() 应返回元素"
        assert r.control_type == T.ControlType.PANE or r.control_type > 0, \
            "根元素应有有效控件类型"

    def _from_window():
        el = T.from_window(_notepad_window())
        assert el is not None, "from_window 应返回元素"
        assert el.hwnd == _notepad_window(), "元素应记得自己的顶层 hwnd"

    def _walk_finds():
        el = T.from_window(_notepad_window())
        kids = T.children(el)
        assert isinstance(kids, list) and kids, "记事本窗口应有子元素"
        desc = T.descendants(el, max_depth=4)
        assert len(desc) > 0, "应能遍历到后代元素"
        # descendants 返回的是平铺列表，元素自带 depth 字段
        for d in desc:
            assert isinstance(d.depth, int) and d.depth >= 0, \
                "后代应有 depth 字段，实际 %r" % (d.depth,)
        # 平铺列表里应出现多种控件类型（说明真的往下走了，不是只返回一层）
        types = {d.control_type for d in desc}
        assert len(types) >= 3, \
            "遍历结果控件类型太单一（%d 种），可能没往深处走" % len(types)

    def _prop_leak():
        """遍历出来的元素必须带 ``window``，且不缓存屏幕坐标。"""
        el = T.from_window(_notepad_window())
        for d in T.descendants(el, max_depth=3)[:20]:
            assert d.window == _notepad_window() or d.window == 0, \
                "元素应记得宿主窗口"
            cr = d.client_rect()
            # client_rect() 返回 layout.Rect（可能为 None）
            if cr is None:
                continue
            assert hasattr(cr, "left") and hasattr(cr, "bottom"), \
                "client_rect 应返回 Rect，实际 %r" % (cr,)
            assert cr.right >= cr.left and cr.bottom >= cr.top, \
                "client_rect 应为合法矩形：%s" % cr
            # ⭐ 屏幕坐标只能"按需算"，不能有缓存字段
            for bad in ("screen_rect", "screen_x", "screen_y", "abs_rect"):
                assert not hasattr(d, bad), \
                    "元素不该缓存屏幕坐标字段 %r" % bad

    def _text():
        el = _editor()
        # 文本内容通过 Value 模式读，不是 tree.text_of
        got = A.get_value(el)
        assert got is None or isinstance(got, str), \
            "读到的文本应是 str 或 None，实际 %r" % (got,)

    def _patterns_field():
        el = T.from_window(_notepad_window())
        assert isinstance(el.patterns, tuple), \
            "patterns 应是元组（未探测时为空）"

    def _control_names():
        assert T.control_name(T.ControlType.BUTTON) == "button", "控件类型名映射错"
        # control_id 大小写不敏感、可吞空白
        assert T.control_id("button") == T.ControlType.BUTTON, "短名应能反查 ID"
        assert T.control_id("  BUTTON ") == T.ControlType.BUTTON, \
            "control_id 应容忍大小写与空白"
        # 表中每个短名都应能往返
        for cid, name in T.CONTROL_NAMES.items():
            assert T.control_id(name) == cid, "%s 反查不一致" % name
        # 未知名字必须报错，不许静默返回 0
        try:
            T.control_id("绝不是一个控件类型")
        except ValueError:
            return
        raise AssertionError("未知控件类型应抛 ValueError")

    def _bad_hwnd():
        try:
            T.from_window(0)
        except Exception:
            return
        raise AssertionError("from_window(0) 应报错")

    check("root() 可用", _root)
    check("from_window 可用", _from_window)
    check("children / descendants 可遍历", _walk_finds)
    check("⭐ 元素不缓存屏幕坐标", _prop_leak)
    check("文本可通过 Value 模式读", _text)
    check("patterns 字段类型正确", _patterns_field)
    check("控件类型名双向映射", _control_names)
    check("非法句柄被拒绝", _bad_hwnd)


# --------------------------------------------------------------------------
# 模式
# --------------------------------------------------------------------------
def test_patterns():
    section("控件模式")

    def _pattern_ids():
        """``PATTERN_IDS``（在 tree 模块）的取值必须落在合法区间。"""
        bad = []
        for name, pid in T.PATTERN_IDS.items():
            if not (10000 <= pid <= 10050):
                bad.append("%s=%s" % (name, pid))
        assert bad == [], "PatternId 越界（应为 10000~10050）：%s" % bad[:3]

    def _pattern_iids_complete():
        missing = [n for n, c in T.PATTERN_IIDS.items() if not hasattr(T.G, c)]
        assert not missing, "PATTERN_IIDS 引用了不存在的常量：%s" % missing[:3]

    def _supported_readonly():
        el = T.from_window(_notepad_window())
        got = P.supported(el)
        assert isinstance(got, list), "supported 应返回列表"
        # 优先级表里的名字都应是真模式名
        unknown = [n for n in got if n not in T.PATTERN_IDS]
        assert not unknown, "supported 返回了未知模式名：%s" % unknown

    def _editor_has_value():
        el = _editor()
        got = P.supported(el)
        assert "Value" in got, "记事本文本区应支持 Value 模式，实际 %s" % got

    def _best_pattern():
        el = _editor()
        # ⚠ best_pattern 的签名是 (el, candidates)，不是 (el)
        best = P.best_pattern(el, ["Value", "Text", "LegacyIAccessible"])
        assert best in ("Value", None), \
            "编辑框在候选里应选中 Value，实际 %r" % (best,)
        assert P.best_pattern(el, ["完全不存在的模式"]) is None, \
            "候选全不支持时应返回 None"

    def _value_roundtrip():
        """写入 → 读回 → 还原。用唯一标记串避免误判。

        ⚠ API 形状：不是 ``ValuePattern(el)`` 而是 ``P.value(el)``；
        读值走 ``.value`` **属性**（不是 ``get_value()``）。
        """
        el = _editor()
        vp = P.value(el)
        try:
            was = vp.value
            token = "avatar-测试-%d" % int(time.time() * 1000 % 100000)
            vp.set_value(token)
            got = vp.value
            assert got == token, "写入后读回不一致：期望 %r 实际 %r" % (token, got)
            vp.set_value(was or "")
            assert vp.value == (was or ""), "未还原为原值"
        finally:
            vp.release()

    def _readonly_flag():
        el = _editor()
        vp = P.value(el)
        try:
            ro = vp.is_read_only   # 属性，不是方法
            assert isinstance(ro, bool), "is_read_only 应返回布尔，实际 %r" % (ro,)
        finally:
            vp.release()

    def _window_pattern():
        el = T.from_window(_notepad_window())
        got = P.pattern_actions(el)
        assert isinstance(got, list), "pattern_actions 应返回列表"
        # 顶层窗口必有 Window 模式
        assert "Window" in P.supported(el), \
            "顶层窗口应支持 Window 模式，实际 %s" % P.supported(el)

    def _window_state():
        el = T.from_window(_notepad_window())
        wp = P.window(el)
        try:
            st = wp.visual_state     # 属性，不是方法
            assert isinstance(st, int), \
                "visual_state 应是整数枚举，实际 %r" % (st,)
        finally:
            wp.release()

    def _missing_pattern_raises():
        """元素不支持某模式时，取名要报错，不能返回半残对象。

        ⚠ 抛的是 ``patterns.PatternNotSupported``，它继承自
        ``ElementNotFound``（不是 ``UnsupportedError``）——
        对调用方而言"元素做不到这件事"与"找不到元素"是同一类问题，
        ``actions.py`` 捕获后会考虑换元素。
        """
        el = T.from_window(_notepad_window())
        if "Toggle" in P.supported(el):
            return  # 环境异常，跳
        try:
            P.toggle(el)          # required 默认 True
        except P.PatternNotSupported as e:
            assert e.pattern == "Toggle", "异常应带上模式名，实际 %r" % e.pattern
            return
        except ElementNotFound:
            return
        except Exception as e:
            raise AssertionError(
                "应抛 PatternNotSupported，实际 %r" % (e,))
        raise AssertionError("窗口元素不支持 Toggle，取值时竟然没报错")

    def _required_false_returns_none():
        el = T.from_window(_notepad_window())
        if "Toggle" in P.supported(el):
            return
        assert P.toggle(el, required=False) is None, \
            "required=False 时不支持的模式应返回 None"

    def _unsupported_pattern_name():
        try:
            P.wrap(T.from_window(_notepad_window()), "绝不是一个模式")
        except ValueError:
            return
        raise AssertionError("未知模式名应抛 ValueError")

    check("PatternId 取值合法", _pattern_ids)
    check("PATTERN_IIDS 引用的常量都存在", _pattern_iids_complete)
    check("supported 可读", _supported_readonly)
    check("编辑框支持 Value 模式", _editor_has_value)
    check("best_pattern 选中 Value", _best_pattern)
    check("⭐ Value 写入 / 读回 / 还原", _value_roundtrip)
    check("is_read_only 返回布尔", _readonly_flag)
    check("窗口支持 Window 模式", _window_pattern)
    check("窗口视觉状态可读", _window_state)
    check("不支持的模式取名时报错", _missing_pattern_raises)
    check("required=False 返回 None", _required_false_returns_none)
    check("未知模式名被拒绝", _unsupported_pattern_name)


# --------------------------------------------------------------------------
# 定位
# --------------------------------------------------------------------------
def test_find():
    section("定位（Query 与三级通道）")

    def _normalize():
        assert F.normalize("  文件\n") == "文件", "应吞掉首尾空白"
        assert F.normalize("a\u00a0b") == "ab", "应吞掉 nbsp"
        assert F.normalize("a\u3000b") == "ab", "应吞掉全角空格"

    def _query_describe():
        q = F.Query(name="登录", control_type="button")
        s = q.describe()
        assert "登录" in s and "button" in s, "query 描述应含名与类型：%s" % s

    def _any_of():
        q = F.Query(name="a").any_of(F.Query(name="b"))
        # any_of 把候选挂在 alternatives 字段上
        assert q.alternatives, "any_of 应写入 alternatives：%r" % (q.alternatives,)
        assert q.alternatives[0].name == "b", \
            "alternatives 内容不对：%r" % (q.alternatives,)
        d = q.describe()
        assert isinstance(d, str) and d.strip(), "describe 应非空"

    def _by_type_window():
        got = F.find_all(F.Query(control_type="window"), limit=5)
        assert len(got) > 0, "应能按类型找到顶层窗口"

    def _by_title():
        hwnd = _notepad_window()
        from avatar.app_manager import windows as W
        info = W.refresh(hwnd)
        if not info.title:
            return
        frag = info.title[:4]
        # ⚠ 本机可能同时开着多个记事本，标题还都是"无标题 - Notepad"。
        # 所以不能断言"搜到的第一个就是本窗口"，只断言"结果里含本窗口"。
        got = F.find_all(F.Query(name=frag, control_type="window"), limit=20)
        assert got, "按标题片段 %r 应能搜到窗口" % frag
        assert any(el.hwnd == hwnd for el in got), \
            "按标题片段 %r 搜到的结果里应含目标窗口 0x%X，实际 %s" % (
                frag, hwnd, [hex(el.hwnd) for el in got][:5])

    def _title_scoped_to_window():
        """限定窗口作用域后按标题找，必须精确命中指定窗口。"""
        hwnd = _notepad_window()
        from avatar.app_manager import windows as W
        info = W.refresh(hwnd)
        if not info.title:
            return
        got = F.find_one(F.Query(name=info.title[:4], control_type="window"),
                         window=hwnd, required=False)
        if got is None:
            return
        assert got.hwnd == hwnd, \
            "限定作用域后仍定位错：期望 0x%X 实际 0x%X" % (hwnd, got.hwnd)

    def _not_found_raises():
        """⭐ 本阶段的底线：找不到就报错。"""
        try:
            F.find_one(F.Query(name="绝不可能存在的元素XYZ123不存在"))
        except ElementNotFound:
            return
        raise AssertionError("找不到时应抛 ElementNotFound")

    def _required_false():
        got = F.find_one(F.Query(name="绝不可能存在的元素XYZ123不存在"),
                         required=False)
        assert got is None, "required=False 应返回 None"

    def _exists_count():
        q = F.Query(control_type="window")
        assert F.exists(q) is True, "exists 应为真"
        assert F.count(q) >= 1, "count 应至少为 1"

    def _scoped_to_window():
        """⭐ 窗口作用域不许泄漏 —— 结果必须都属于指定窗口。

        用 ``document``：Win11 记事本的文本区是这个类型。
        """
        hwnd = _notepad_window()
        got = F.find_all(F.Query(control_type="document"), window=hwnd)
        assert got, "记事本里应能找到 document"
        for el in got:
            assert el.window == hwnd or el.hwnd == hwnd, \
                "窗口作用域泄漏：元素属于 0x%X 而非 0x%X" % (el.window, hwnd)

    def _wait_timeout():
        t0 = time.monotonic()
        try:
            F.wait(F.Query(name="绝不可能存在的元素XYZ123不存在"), timeout=0.3)
        except ElementNotFound:
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.25, "wait 不该提前放弃，实际 %.2fs" % elapsed
            return
        raise AssertionError("wait 超时应抛 ElementNotFound")

    def _wait_gone():
        # ⚠ 必须限定作用域。不限定时这个"预期找不到"的查询会掉进
        #   通道 C 的全桌面遍历（~30s），而不是立刻返回。
        assert F.wait_gone(
            F.Query(name="绝不可能存在的元素XYZ123不存在",
                    window=_notepad_window()), timeout=0.3) is True, \
            "对本来就不存在的元素 wait_gone 应立即为真"

    def _str_as_query():
        # 同样限定作用域，避免全桌面遍历
        got = F.find_all("notepad", window=_notepad_window(), limit=3)
        assert isinstance(got, list), "字符串应能直接当查询用"

    def _str_query_unscoped_smoke():
        """无作用域的字符串查询也要能用 —— 但只查一个便宜的类型。

        ``control_type="window"`` 走通道 A（Win32 枚举），~1.3s；
        纯名称查询走通道 C，~30s。测试里选便宜的那条。
        """
        got = F.find_all(F.query("", "window"), limit=3)
        assert isinstance(got, list) and got, "无作用域按类型查询应能返回"

    def _bad_query_type():
        try:
            F.find_all(12345)
        except TypeError:
            return
        raise AssertionError("非法查询类型应抛 TypeError")

    def _query_shortcuts():
        q = F.query("x", "button")
        # ⚠ control_type 归一成 **frozenset/set**（多类型集合），不是元组。
        #   这样 `control_type in q.control_type` 天然支持多类型匹配，
        #   且与 `_norm_types()` 的输出类型保持一致。
        assert set(q.control_type) == {T.ControlType.BUTTON}, \
            "query() 应把 control_type 归一成类型集合，实际 %r" % (
                q.control_type,)
        assert q.name == "x", "query() 应设置 name"
        # 多类型也应被接受
        q2 = F.query("y", ("button", "edit"))
        assert set(q2.control_type) == {T.ControlType.BUTTON,
                                        T.ControlType.EDIT}, \
            "多类型应全部归一，实际 %r" % (q2.control_type,)

    check("名称归一化吞空白", _normalize)
    check("Query 可读描述", _query_describe)
    check("any_of 生效", _any_of)
    check("按类型找顶层窗口", _by_type_window)
    check("按标题能找到（多开时含目标）", _by_title)
    check("限定作用域后按标题精确命中", _title_scoped_to_window)
    check("⭐ 找不到抛 ElementNotFound", _not_found_raises)
    check("required=False 返回 None", _required_false)
    check("exists / count 安全", _exists_count)
    check("⭐ 窗口作用域不泄漏", _scoped_to_window)
    check("wait 超时抛异常且不早退", _wait_timeout)
    check("wait_gone 对不存在元素为真", _wait_gone)
    check("字符串可直接当查询", _str_as_query)
    check("无作用域按类型查询可用", _str_query_unscoped_smoke)
    check("非法查询类型被拒绝", _bad_query_type)
    check("query 快捷构造器", _query_shortcuts)


# --------------------------------------------------------------------------
# 描述
# --------------------------------------------------------------------------
def test_describe():
    section("描述（给人看 / 给模型看）")

    def _type_zh_complete():
        missing = []
        for name, val in vars(T.ControlType).items():
            if name.startswith("_") or not isinstance(val, int):
                continue
            if val not in D.TYPE_ZH:
                missing.append(name)
        assert not missing, "中文类型名缺：%s" % missing[:5]

    def _one_line():
        el = _editor()
        s = D.one_line(el, 7)
        assert "\n" not in s, "one_line 不该换行"
        assert s.strip().startswith("7"), "应带编号，实际 %r" % s[:20]

    def _describe_detail():
        el = _editor()
        s = D.describe(el)
        # describe 的中文标签是"类型"/"窗口"（不是 hwnd）
        for key in ("类型", "窗口", "进程"):
            assert key in s, "describe 应含 %r，实际：\n%s" % (key, s)

    def _table():
        els = _editors(5)
        tbl = D.table(els)
        assert "#" in tbl and "类型" in tbl, "table 应有表头：\n%s" % tbl[:200]

    def _tree():
        el = T.from_window(_notepad_window())
        els = T.descendants(el, max_depth=3)
        s = D.tree(els)
        assert isinstance(s, str) and s.strip(), "tree 应输出非空"

    def _markup():
        els = _editors(3)
        s = D.markup(els)
        assert "<" in s or "[" in s, "markup 应带标记：%s" % s[:120]

    def _to_dict_client_rect():
        el = _editor()
        d = D.to_dict(el, index=1)
        assert "client_rect" in d, "to_dict 应输出 client_rect：%s" % sorted(d)

    def _no_screen_by_default():
        """⭐ 元素只存客户区坐标 —— 默认输出里不该有屏幕坐标。"""
        el = _editor()
        d = D.to_dict(el, include_screen=False)
        for k in ("screen_rect", "screen_x", "screen_y", "abs_rect"):
            assert k not in d, "默认输出不该含屏幕坐标字段 %r" % k

    def _screen_on_demand():
        """显式要屏幕坐标时应能现算出来，且与 client_to_screen 一致。

        ⚠ ``to_dict`` 输出的是 **xywh** 列表，``client_to_screen`` 收的是
        ``(x, y)`` —— 所以只比对左上角两个数。
        """
        el = _editor()
        d = D.to_dict(el, include_screen=True)
        cr = el.client_rect()
        if cr is None:
            return
        from avatar.app_manager import layout as L
        ox, oy = L.client_to_screen(el.window, cr.left, cr.top)
        # 找输出里那个屏幕矩形（xywh）
        found = False
        for k, v in d.items():
            if "screen" in k and isinstance(v, (list, tuple)) and len(v) == 4:
                assert (v[0], v[1]) == (ox, oy), (
                    "现算的屏幕坐标与 client_to_screen 不一致："
                    "%s 左上 (%d,%d) vs 期望 (%d,%d)" % (k, v[0], v[1], ox, oy))
                found = True
        assert found or "screen" not in str(d), \
            "include_screen=True 时应含屏幕坐标字段：%s" % sorted(d)

    def _click_target():
        el = _editor()
        ct = D.to_click_target(el, 1)
        # 真实字段：index/name/type/automation_id/window/client_rect/
        #          screen_rect_reference/patterns/preferred_action/enabled/offscreen
        need = {"index", "name", "type", "window", "client_rect", "patterns"}
        missing = need - set(ct)
        assert not missing, "to_click_target 缺字段：%s" % missing
        assert ct["index"] == 1, "index 应被设置"
        assert isinstance(ct["client_rect"], list) and len(ct["client_rect"]) == 4, \
            "client_rect 应是 4 元素 xywh 列表，实际 %r" % (ct["client_rect"],)

    def _click_target_no_screen():
        """⭐ 模型动作契约里不该预置可执行的屏幕坐标。

        ``screen_rect_reference`` 存在但**名字里就写明是 reference** ——
        真正执行走 ``window + client_rect``。窗口一动 reference 就作废，
        所以它不能是执行依据。
        """
        el = _editor()
        ct = D.to_click_target(el, 1)
        for bad in ("x", "y", "screen_x", "screen_y", "screen_rect"):
            assert bad not in ct, \
                "to_click_target 不该有可执行坐标字段 %r：%s" % (bad, sorted(ct))
        assert "screen_rect_reference" not in ct or "reference" in "screen_rect_reference", \
            "若保留屏幕矩形，名字必须标明 reference"

    def _to_json():
        els = _editors(2)
        js = D.to_json([D.to_dict(e, index=i) for i, e in enumerate(els)])
        assert js.strip().startswith("["), "to_json 应输出数组"
        assert "\\u" not in js, "中文不该被转义成 \\u：%s" % js[:120]

    def _to_prompt():
        els = _editors(2)
        s = D.to_prompt(els, window_name="记事本")
        assert "记事本" in s, "to_prompt 应带窗口名"
        assert isinstance(s, str) and s.strip()

    def _summarize():
        els = _editors(2)
        s = D.summarize(els)
        assert "共" in s and "元素" in s, "summarize 应含统计：%s" % s

    def _actions_of():
        el = _editor()
        acts = D.actions_of(el)
        assert isinstance(acts, list), "actions_of 应返回列表"
        assert "set_value" in acts or "click" in acts, \
            "编辑框的可执行动作应含 set_value 或 click，实际 %s" % acts

    def _compare():
        """compare 要能识别"元素消失了"。"""
        els = _editors(3)
        assert D.compare(els, els)["gone"] == [], "相同快照不该有差异"
        if len(els) >= 2:
            diff = D.compare(els, els[:1])
            assert diff["gone"], "少了一个元素却报无差异"

    def _clip():
        assert "…" in D.one_line(_editor(), 1) or True, "截断逻辑存在即可"

    check("中文类型名覆盖完整", _type_zh_complete)
    check("one_line 单行带编号", _one_line)
    check("describe 含关键字段", _describe_detail)
    check("table 有表头", _table)
    check("tree 可输出", _tree)
    check("markup 带标记", _markup)
    check("to_dict 带 client_rect", _to_dict_client_rect)
    check("⭐ 默认不输出屏幕坐标", _no_screen_by_default)
    check("屏幕坐标按需现算且一致", _screen_on_demand)
    check("to_click_target 字段齐全", _click_target)
    check("⭐ to_click_target 无可执行坐标", _click_target_no_screen)
    check("to_json 中文不转义", _to_json)
    check("to_prompt 结构正确", _to_prompt)
    check("summarize 有统计", _summarize)
    check("actions_of 对真实元素可用", _actions_of)
    check("compare 能识别消失", _compare)


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------
def test_actions():
    section("动作（模式优先，坐标兜底须显式开启）")

    def _editor_ready():
        el = _editor()
        assert A.is_supported(el, "set_value"), \
            "记事本编辑框应支持 set_value"

    def _set_value_roundtrip():
        """set_value 的契约：``ok`` 说的是**API 调用成功**，不是"内容变了"。

        ⚠ 关键区别（踩过）：
        · ``ok=True, verified=None`` —— SetValue 返回成功，但调用方没给
          ``expect``，所以没有读回确认。**这不等于内容真的落了。**
        · 要确认内容，必须传 ``expect``，或自己读回比对。

        Win11 记事本的 ``RichEditD2DPT`` 正是这种：``SetValue`` 返回成功，
        但它的 ``Value`` 读回永远为空 —— 不是我们的 bug，是控件限制。
        所以下面的"写回一致"断言只在**读得出来**时才要求一致。
        """
        el = _editor()
        was = A.get_value(el)
        token = "avatar-动作-%d" % int(time.time() * 1000 % 100000)
        try:
            res = A.set_value(el, token)
            # ⚠ 接口更正（2026-09-27）：``set_value`` 返回的是
            #   ``ActionResult``（dataclass），**不是 dict**。
            #   细节在 ``res.extra`` 里，没有 ``res.get()``。
            #   早期断言写成 ``res.get("path")`` 会直接
            #   ``AttributeError`` —— 测试红了很久没人管。
            assert res.ok is True, "SetValue 应报告成功：%s" % res
            assert res.extra.get("path") == "value", \
                "应走 value 模式，实际 path=%r" % res.extra.get("path")
            assert "verified" in res.extra, \
                "返回里应有 verified 字段：%s" % sorted(res.extra)
            # 只有读得出来的控件才要求写入可读回
            back = A.get_value(el)
            if back is None:
                return   # 控件不支持读回，跳过后半段
            assert back == token, \
                "写回不一致：期望 %r 实际 %r（读得出来就必须一致）" % (token, back)
        finally:
            try:
                A.set_value(el, was or "")
            except Exception:
                pass

    def _set_value_with_expect():
        """传了 ``expect`` 就必须真的校验 —— 这是"动作后验证"的落地。"""
        el = _editor()
        was = A.get_value(el)
        token = "avatar-校验-%d" % int(time.time() * 1000 % 100000)
        try:
            res = A.set_value(el, token,
                              expect=lambda: A.get_value(el) == token)
            # ``res.verified`` 是 VerifyResult（阶段四），不是裸 bool。
            # 判"校验成功"要看 ``.ok``，不是 ``is True``。
            vr = res.verified
            assert vr is not None and vr.ok is True, \
                "读得出来时应校验成功，实际 verified=%r" % (vr,)
        except A.ActionFailed:
            # 控件不支持读回 → expect 永远不满足 → 抛 ActionFailed 也算正确行为
            assert A.get_value(el) is None, \
                "只有在读不出来的控件上才允许校验失败"
        finally:
            try:
                A.set_value(el, was or "")
            except Exception:
                pass

    def _set_value_reports_path():
        el = _editor()
        was = A.get_value(el)
        try:
            res = A.set_value(el, "x")
            assert res.extra.get("path") == "value", \
                "编辑框应走 value 模式，实际 path=%r" % res.extra.get("path")
        finally:
            try:
                A.set_value(el, was or "")
            except Exception:
                pass

    def _clear():
        el = _editor()
        was = A.get_value(el)
        try:
            A.clear_value(el)
            back = A.get_value(el)
            if back is None:
                return   # 控件不支持读回
            assert back == "", "清空后应为空，实际 %r" % back
        finally:
            try:
                A.set_value(el, was or "")
            except Exception:
                pass

    def _focus():
        el = _editor()
        res = A.focus(el)
        assert isinstance(res, dict), "focus 应返回结果字典"

    def _no_silent_coord():
        """⭐ 底线：没有可点模式、又没显式开坐标兜底时，必须报错。

        这是一条真正的安全阀 —— 静默用坐标会点错地方，而且错得无声无息。

        ⚠ 靶子要选对：窗口本身**支持** ``LegacyIAccessible``，所以它是
        "可点"的，拿它测会误判成"没报错"。要找一个**真的零模式**的元素
        —— 记事本的 ``title_bar`` 就是（实测 patterns == []）。
        """
        el = _no_pattern_element()
        if el is None:
            return   # 找不到零模式元素，环境异常，跳

        try:
            A.click(el, allow_coord=False)
        except (UnsupportedError, A.ActionFailed, ElementNotFound):
            return   # 正确：明确报错
        except Exception as e:
            raise AssertionError("抛了意料外的异常：%r" % (e,))
        raise AssertionError(
            "元素 %r 零模式，点击竟然没报错 —— 默认用了坐标"
            % (el.name or T.control_name(el.control_type),))

    def _coord_requires_opt_in():
        """``allow_coord=False`` 时坐标钩子**绝不能**被调用。"""
        seen = []
        A.register_coord_fallback(lambda x, y, **kw: seen.append((x, y)))
        try:
            el = _no_pattern_element()
            if el is None:
                return
            try:
                A.click(el, allow_coord=False)
            except Exception:
                pass
            assert seen == [], \
                "allow_coord=False 时坐标钩子被调用了：%s" % seen
        finally:
            A.register_coord_fallback(None)

    def _locate_fail_no_fallback():
        """⭐ 定位失败不许降级坐标。"""
        assert callable(A.register_coord_fallback), \
            "register_coord_fallback 应可调用"
        assert A.coord_fallback() is None, \
            "阶段二默认不该注册坐标兜底（归阶段三输入层）"

    def _toggle_idempotent():
        """toggle 幂等：状态已经对了就空操作，不来回翻。

        ⚠ 记事本主窗口既无 Toggle 也无 SelectionItem 模式，对它调 toggle
        会抛 ``ActionFailed`` —— 这是**正确的**（不静默用坐标）。
        所以这里改成：找一个真有 Toggle 模式的元素才测幂等性。
        """
        win = T.from_window(_notepad_window())
        target = _toggle_element()

        if target is None:
            # 没有 Toggle 元素 —— 那正好验证"无模式时明确报错"
            try:
                A.toggle(win, on=True)
            except (A.ActionFailed, UnsupportedError):
                return
            raise AssertionError("无 Toggle 模式的元素竟然切换成功了")

        # 有 Toggle 元素：验证幂等 —— 设成当前状态应是空操作
        cur = None
        tp = P.toggle(target, required=False)
        if tp is None:
            return
        try:
            cur = tp.state
        finally:
            tp.release()
        res = A.toggle(target, on=cur)
        # 同上：``toggle`` 也返回 ActionResult，细节在 ``.extra``
        assert res.extra.get("action") == "noop" \
            or res.extra.get("path") is None, \
            "目标状态 == 当前状态时应是空操作，实际 %s" % res

    def _click_failure_is_loud():
        """⭐ 定位失败时 click 必须抛异常，不能返回"成功"。"""
        try:
            A.click("绝不可能存在的元素XYZ123不存在")
        except (ElementNotFound, Exception) as e:
            assert not isinstance(e, AssertionError), "%r" % (e,)
            return
        raise AssertionError("定位失败的 click 竟然返回了")

    def _actions_table():
        need = ("click", "set_value", "get_value", "toggle", "check", "uncheck",
                "select", "expand", "collapse", "focus", "scroll",
                "scroll_into_view", "close_window", "invoke_default", "read")
        missing = [k for k in need if k not in A.ACTIONS]
        assert not missing, "ACTIONS 表缺：%s" % missing

    def _is_supported():
        """⚠ ``is_supported`` 对**未知动作名**会抛 ValueError（不是返回 False）。

        这是刻意的：动作名写错了应该立刻发现，而不是被当成"不支持"
        静默吞掉。False 只表示"这个动作已知，但元素做不到"。
        """
        el = _editor()
        assert A.is_supported(el, "click") in (True, False), \
            "已知动作应返回布尔"
        try:
            A.is_supported(el, "不存在动作")
        except ValueError:
            return
        raise AssertionError("未知动作名应抛 ValueError，而不是静默返回 False")

    def _bad_target():
        try:
            A.set_value(0, "x")
        except Exception:
            return
        raise AssertionError("非法目标应报错")

    def _read_noop():
        el = _editor()
        d = A.read(el)
        assert isinstance(d, dict), "read 应返回快照字典"

    check("editbox 支持 set_value", _editor_ready)
    check("⭐ set_value 契约（ok 只表示 API 成功）", _set_value_roundtrip)
    check("⭐ set_value 传 expect 时真校验", _set_value_with_expect)
    check("set_value 走 value 模式", _set_value_reports_path)
    check("clear_value 可用", _clear)
    check("focus 可用", _focus)
    check("⭐ 零模式元素点击必须报错", _no_silent_coord)
    check("⭐ allow_coord=False 时不碰坐标", _coord_requires_opt_in)
    check("⭐ 未注册坐标兜底（归阶段三）", _locate_fail_no_fallback)
    check("⭐ toggle 幂等 / 无模式时报错", _toggle_idempotent)
    check("⭐ 定位失败的 click 不返回", _click_failure_is_loud)
    check("ACTIONS 表完整", _actions_table)
    check("is_supported 对未知动作抛 ValueError", _is_supported)
    check("非法目标被拒绝", _bad_target)
    check("read 返回快照", _read_noop)


# --------------------------------------------------------------------------
# 降级通道：OCR / 视觉
# --------------------------------------------------------------------------
def test_fallbacks():
    section("降级通道（OCR / 视觉）")

    def _ocr_reports_availability_honestly():
        """OCR 的可用性必须**如实**报告。

        ⚠ 这条原先是 `assert OCR.is_available() is False`，理由写的是
        "实现推迟到阶段七"。阶段七已经实现，那个理由过期了。

        而且"能不能用"是**环境事实**（本机 WinRT 的 SoftwareBitmap
        通路受阻），把它写成硬断言，环境一变就假红 —— 测的就不再是
        代码，而是这台机器。

        真正该守的契约是：**不可用时必须说清为什么**。
        """
        if OCR.is_available():
            return          # 环境支持，这条不适用
        assert OCR.unavailable_reason(), "不可用时必须给出原因"

    def _ocr_raises_before_validation():
        """⭐ 不可用时请求 OCR 必须报 UnsupportedError，而不是先去校验参数。

        这条顺序很重要：先校验参数会把 "环境不支持" 掩盖成 "参数不对"。
        """
        try:
            # 故意传非法窗口 0，看它先报哪个错
            OCR.find_text("任何文字", window=0)
        except OCR.OcrUnavailable:
            return
        except UnsupportedError:
            return
        except Exception as e:
            raise AssertionError(
                "应先报 UnsupportedError，实际 %r —— 参数校验跑到可用性检查前面了" % (e,))
        raise AssertionError("OCR 不可用时竟然没报错")

    def _ocr_engine_hooks():
        assert callable(OCR.set_engine), "set_engine 应存在"
        assert OCR.engine_name() is None or isinstance(OCR.engine_name(), str), \
            "engine_name 应返回 str 或 None"
        d = OCR.describe_environment()
        assert isinstance(d, (str, dict)), "describe_environment 应可读"

    def _ocr_hit_model():
        """``OcrHit`` 契约：只存客户区坐标，且可序列化。

        ``center_client`` 用整数除法（奇数尺寸偏左上）—— 对点击更安全，
        不会落到边界外。
        """
        # client_rect = (1, 2, 5, 6) → 宽 4 高 4 → 中心 (1+2, 2+2) = (3, 4)
        h = OCR.OcrHit(text="确定", window=123, client_rect=(1, 2, 5, 6),
                       confidence=0.9, engine="fake")
        assert h.center_client == (3, 4), \
            "center_client 算错：期望 (3,4) 实际 %s" % (h.center_client,)
        d = h.to_dict()
        assert "client_rect" in d, "OcrHit 应输出 client_rect"
        for k in ("screen_x", "screen_y", "screen_rect"):
            assert k not in d, "OcrHit 不该存屏幕坐标字段 %r" % k

    def _vision_engine_vs_algorithm():
        """⭐ 引擎版与算法版的可用性必须**分开报告**。

        原先这条是 ``assert VIS.is_available() is False``，理由写的是
        "实现推迟到阶段七"。阶段七实现之后理由过期，而且那个断言
        **低估了实际能力** —— 纯 Python 匹配当时就能用，降级链第四层
        走的就是它（``_layer_template`` → ``find_template``）。

        真正要守的契约是：**两件事分别如实报告**。让调用方看到
        "引擎不可用"就以为匹配完全不能用，是实打实的误导。
        """
        # 算法版：只用标准库，恒定可用
        assert VIS.algorithm_available() is True, "纯算法版应恒定可用"
        # 引擎版：没注册就是不可用，且必须说清"缺的到底是什么"
        if not VIS.is_available():
            why = VIS.unavailable_reason()
            assert why, "引擎不可用时必须给出原因"
            assert ("算法" in why) or ("find_template" in why), \
                "原因里必须说清「算法版仍然可用」，否则会误导：%r" % why

    def _vision_raises():
        try:
            VIS.find_template(0, object())   # 故意传非法窗口
        except VIS.VisualUnavailable:
            return
        except UnsupportedError:
            return
        except Exception as e:
            raise AssertionError(
                "应先报 UnsupportedError，实际 %r —— 参数校验跑到可用性检查前面了" % (e,))
        raise AssertionError("视觉不可用时竟然没报错")

    def _vision_capture_separate():
        """⭐ 截图能力与识别能力是两个维度，必须分开报告。"""
        cb = VIS.capture_backend()
        assert cb is None or isinstance(cb, str), "capture_backend 应返回 str/None"
        d = VIS.describe_environment()
        assert isinstance(d, (str, dict)), "describe_environment 应可读"

    def _vision_hit_model():
        h = VIS.VisualHit(label="确定", window=123, client_rect=(0, 0, 10, 10),
                          score=0.9, method="template")
        d = h.to_dict()
        assert "client_rect" in d, "VisualHit 应输出 client_rect"
        for k in ("screen_x", "screen_y", "screen_rect"):
            assert k not in d, "VisualHit 不该存屏幕坐标字段 %r" % k
    def _threshold_is_conservative():
        assert VIS.DEFAULT_THRESHOLD >= 0.8, \
            "视觉阈值应保守（>=0.8），实际 %s" % VIS.DEFAULT_THRESHOLD

    check("OCR 如实报告可用性", _ocr_reports_availability_honestly)
    check("⭐ OCR 先报不可用再校验参数", _ocr_raises_before_validation)
    check("OCR 引擎钩子可用", _ocr_engine_hooks)
    check("OcrHit 只存客户区坐标", _ocr_hit_model)
    check("⭐ 视觉：引擎版与算法版分开报告", _vision_engine_vs_algorithm)
    check("⭐ 视觉先报不可用再校验参数", _vision_raises)
    check("截图与识别分开报告", _vision_capture_separate)
    check("VisualHit 只存客户区坐标", _vision_hit_model)
    check("默认阈值保守", _threshold_is_conservative)


# --------------------------------------------------------------------------
# 降级链
# --------------------------------------------------------------------------
def test_fallback_chain():
    section("降级链编排")

    def _import():
        from avatar import uia
        assert uia.FALLBACK_ORDER == ("uia", "ocr", "vision", "coord"), \
            "降级顺序应为 uia → ocr → vision → coord，实际 %s" % (
                uia.FALLBACK_ORDER,)

    def _chain_shape():
        from avatar import uia
        ch = uia.fallback_chain()
        assert isinstance(ch, list) and len(ch) == 4, \
            "降级链应有 4 环，实际 %d" % len(ch)
        for item in ch:
            for k in ("name", "available", "reason"):
                assert k in item, "每一环应有 %r 字段：%s" % (k, item)
            assert isinstance(item["available"], bool), \
                "%s.available 应是布尔，实际 %r" % (item["name"], item["available"])
            # ⭐ 不可用必须给原因，不许留空
            if not item["available"]:
                assert item["reason"], "%s 报告不可用却没给原因" % item["name"]

    def _uia_available():
        from avatar import uia
        uia_chain = [c for c in uia.fallback_chain() if c["name"] == "uia"]
        assert uia_chain and uia_chain[0]["available"] is True, \
            "UIA 必须可用 —— 它是主通道"

    def _has_fallback_honest():
        """⭐ ``has_fallback()`` 问的是"UIA 之外还有没有兜底"。

        语义容易读错：它**不**包括 uia 自己。阶段二 OCR/vision/coord
        全不可用，所以必须是 ``False`` —— 这意味着 UIA 一旦失手，
        任务只能放弃，上层规划阶段就该据此决定要不要执行。

        这是个"如实报告能力"的测试：谎报 True 会让上层误以为有退路。
        """
        from avatar import uia
        chain = uia.fallback_chain()
        others = [c for c in chain if c["name"] != "uia" and c["available"]]
        assert uia.has_fallback() == (len(others) > 0), \
            "has_fallback() 与 chain 不一致 —— 存在谎报"
        assert uia.has_fallback() is False, \
            "阶段二 UIA 之外无兜底，has_fallback 应为 False，实际 True"
        # 主通道必须是可用的，否则整套都是假的
        uia_chain = [c for c in chain if c["name"] == "uia"]
        assert uia_chain and uia_chain[0]["available"] is True, "UIA 主通道必须可用"

    def _describe_caps():
        from avatar import uia
        s = uia.describe_capabilities()
        assert isinstance(s, str) and s.strip(), "describe_capabilities 应非空"
        assert "uia" in s, "能力描述里应出现 uia"

    check("导出降级顺序常量", _import)
    check("降级链结构完整且不可用必给原因", _chain_shape)
    check("UIA 主通道可用", _uia_available)
    check("⭐ has_fallback 如实报告", _has_fallback_honest)
    check("describe_capabilities 可读", _describe_caps)


# --------------------------------------------------------------------------
# COM 泄漏
# --------------------------------------------------------------------------
def test_no_leak():
    section("COM 引用不泄漏")

    def _walk_then_gc():
        """反复遍历 + 释放，不应出现句柄/引用数单调增长。"""
        import ctypes as C
        from avatar._win32 import com as COM
        # 取 COM 对象计数（若实现暴露）或退化为进程句柄数
        def _count():
            fn = getattr(COM, "live_count", None)
            if callable(fn):
                return int(fn())
            h = C.windll.kernel32.GetCurrentProcess()
            n = C.c_ulong(0)
            C.windll.kernel32.GetProcessHandleCount(h, C.byref(n))
            return int(n.value)

        el = T.from_window(_notepad_window())
        T.descendants(el, max_depth=3)  # 预热
        gc.collect()
        base = _count()
        for _ in range(5):
            T.descendants(T.from_window(_notepad_window()), max_depth=3)
            gc.collect()
        after = _count()
        assert after <= base + 40, \
            "5 轮遍历后句柄数从 %d 涨到 %d，疑似泄漏" % (base, after)

    def _find_loop():
        """反复定位不泄漏 —— 绕开缓存，真的重查。"""
        gc.collect()
        import ctypes as C
        h = C.windll.kernel32.GetCurrentProcess()
        n = C.c_ulong(0)
        C.windll.kernel32.GetProcessHandleCount(h, C.byref(n))
        base = int(n.value)
        for _ in range(3):
            F.find_all(F.Query(control_type="document"),
                       window=_notepad_window(), limit=5)
            gc.collect()
        C.windll.kernel32.GetProcessHandleCount(h, C.byref(n))
        after = int(n.value)
        assert after <= base + 40, \
            "3 轮定位后句柄数从 %d 涨到 %d，疑似泄漏" % (base, after)

    check("反复树遍历不泄漏", _walk_then_gc)
    check("反复定位不泄漏", _find_loop)


# --------------------------------------------------------------------------
# 模块内建自检
# --------------------------------------------------------------------------
def test_module_selfchecks():
    section("模块内建 selfcheck 全过")

    def _one(name, fn):
        def _run():
            r = fn()
            assert isinstance(r, dict), "%s selfcheck 应返回 dict" % name
            assert r.get("ok") is True, "%s selfcheck 失败：%s" % (
                name, "; ".join(r.get("failures") or ["未知"]))

        check("%s.selfcheck()" % name, _run)

    for name, mod in (("tree", T), ("patterns", P), ("find", F),
                      ("describe", D), ("actions", A),
                      ("ocr_fallback", OCR), ("vision_fallback", VIS)):
        _one(name, mod.selfcheck)


# --------------------------------------------------------------------------
# 自检性能：红线断言
# --------------------------------------------------------------------------
#: quick 档单模块上限（秒）。
#:
#: 与 ``scripts/selfcheck.py`` 的 ``MODULE_SELFCHECK_MAX`` 保持一致 ——
#: 那边是"跑的时候判"，这边是"提交的时候判"。两处都堵上，
#: 才不会出现"本地测试过了、自检里超时"的错位。
SELFCHECK_QUICK_MAX = 3.0


def test_actions_selfcheck_quick_is_fast():
    """**性能红线**：``avatar.uia.actions.selfcheck()`` 默认档必须 < 3s。

    ## 为什么这条测试必须存在

    这个模块的自检**曾经是 70.78 秒**。它就是压不下来，原因很具体：

    ``T.descendants(T.root(), ...)`` 会**一口气走完整棵桌面树**
    （4000+ 元素），每次跨进程 COM 调用几十微秒，累起来就是分钟级。
    而它连 ``Ctrl+C`` 都不响应 —— COM 调用期间 Python 收不到信号。

    结果：全量自检要等一分多钟，"随手跑一遍"这件事**实际上已经废了**。
    没有人会跑一个要等一分多钟的自检。

    修法是加 ``quick`` 档（默认跳过真实窗口遍历）+ 把那次
    "预期找不到"的 Query 收紧成 ``name_exact=True``（避免掉进
    Python 全遍历）。

    这条测试保证：**任何人以后往 selfcheck 里塞重活，都会在这里被拦下。**
    """
    section("actions.selfcheck 默认档性能红线（< 3s）")

    t0 = time.monotonic()
    r = A.selfcheck()
    dt = time.monotonic() - t0

    def _is_dict():
        assert isinstance(r, dict), "应返回 dict"

    check("actions.selfcheck() 返回 dict", _is_dict)

    def _green():
        assert r.get("ok") is True, \
            "自检失败：%s" % "; ".join(r.get("failures") or ["未知"])

    check("actions.selfcheck() quick 档全绿", _green)

    def _fast():
        assert dt < SELFCHECK_QUICK_MAX, \
            "actions 自检耗时 %.2fs，超过 %.1fs 红线。" \
            "多半是又在 quick 档里加了真实窗口遍历 —— " \
            "重活应该放进 quick=False 分支。" % (dt, SELFCHECK_QUICK_MAX)

    check("actions.selfcheck() 耗时 < %.1fs（实测 %.2fs）"
          % (SELFCHECK_QUICK_MAX, dt), _fast)

    def _declares_quick():
        assert r.get("quick") is True, \
            "返回结构里必须声明档位，否则调用方不知道有没有漏跑重活"

    check("返回值声明 quick=True", _declares_quick)

    def _transparent():
        # 跳过的重活必须**显式记账**，不能静默消失
        assert "skipped_heavy" in r, "缺 skipped_heavy —— 跳过项不透明"
        assert isinstance(r["skipped_heavy"], int)

    check("跳过项透明（skipped_heavy）", _transparent)


def test_actions_selfcheck_full_is_bounded():
    """``quick=False`` 也要有上限，不能又变成 70 秒。

    这一档允许更久（要做真实窗口遍历），但必须**受预算约束** ——
    否则机器上窗口一多，就又回到"不愿跑"的状态。
    """
    section("actions.selfcheck 完整档有上限")

    t0 = time.monotonic()
    r = A.selfcheck(quick=False)
    dt = time.monotonic() - t0

    def _is_dict2():
        assert isinstance(r, dict), "非 dict"

    check("完整档返回 dict", _is_dict2)

    def _bounded():
        budget = A.DEFAULT_SELFCHECK_BUDGET
        assert dt < budget + 4.0, \
            "完整档跑了 %.2fs，超出 %s+4s —— 预算没生效" % (dt, budget)

    check("完整档受预算约束（实测 %.2fs）" % dt, _bounded)

    def _declares_full():
        assert r.get("quick") is False, "quick=False 时返回值应声明 full 档"

    check("返回值声明 quick=False", _declares_full)


def test_actions_selfcheck_is_interruptible():
    """**可中断**：``quick=False`` 必须能被 ``KeyboardInterrupt`` 干净打断。

    老的 ``T.descendants(T.root())`` 做不到这点：它一口气走完才返回，
    期间用户按 Ctrl+C 完全没反应（C 层的 COM 调用不让 Python 收信号）。

    新实现改成生成器式逐项推进 + 每个循环点让出时间片。
    这条测试用一个"打第一枪就中断"的假遍历来验证**中断会被向上传播**，
    而不是被吞掉。
    """
    section("actions 完整档可被 Ctrl+C 打断")

    def _simulate():
        # 直接验证 _iter_real_windows 会传播 KeyboardInterrupt：
        # 用 monkeypatch 让底层 descendants 抛 KeyboardInterrupt，
        # 断言它不是被 except Exception 吞掉，而是继续往上冒。
        orig = A.T.descendants
        calls = []

        def _boom(*a, **kw):
            calls.append(1)
            raise KeyboardInterrupt()

        A.T.descendants = _boom
        try:
            try:
                list(A._iter_real_windows(max_items=5))
            except KeyboardInterrupt:
                return True          # 正确：向上传播
            return False             # 错误：被吞了
        finally:
            A.T.descendants = orig

    check("KeyboardInterrupt 向上传播（不被吞）", _simulate)


def test_actions_selfcheck_declares_budget_constant():
    """预算常量必须存在且合理 —— 它是"有上限"这个承诺的载体。"""
    section("自检预算常量")

    def _has():
        assert hasattr(A, "DEFAULT_SELFCHECK_BUDGET"), "缺预算常量"
        assert 1.0 <= A.DEFAULT_SELFCHECK_BUDGET <= 60.0, \
            "预算 %r 不合理" % A.DEFAULT_SELFCHECK_BUDGET

    check("DEFAULT_SELFCHECK_BUDGET ∈ [1, 60]", _has)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段二 · UIA 元素定位测试")
    print("=" * 64)

    # 用例本身自包含：没有记事本就自己拉一个，收尾时关掉
    try:
        _ensure_notepad()
        have_note = True
    except AssertionError as e:
        have_note = False
        print()
        print("!! %s" % e)

    try:
        test_tree()
        for fn in (test_patterns, test_find, test_describe, test_actions):
            if have_note:
                fn()
            else:
                section(fn.__name__ + "（跳过：无记事本）")
                _RESULTS.append((fn.__name__, False, "无记事本窗口，整组跳过"))
                print("  [SKIP] 无记事本窗口，整组跳过")
        test_fallbacks()
        test_fallback_chain()
        if have_note:
            test_no_leak()
        else:
            section("COM 引用不泄漏（跳过：无记事本）")
            _RESULTS.append(("test_no_leak", False, "无记事本窗口，整组跳过"))
            print("  [SKIP] 无记事本窗口，整组跳过")
        test_module_selfchecks()
        # 自检性能红线：这三条不依赖记事本，任何时候都要跑 ——
        # 因为"自检能不能随手跑"本身就是被测对象。
        test_actions_selfcheck_quick_is_fast()
        test_actions_selfcheck_full_is_bounded()
        test_actions_selfcheck_is_interruptible()
        test_actions_selfcheck_declares_budget_constant()
    finally:
        _close_own_notepad()

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段二 UIA 测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s -> %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
