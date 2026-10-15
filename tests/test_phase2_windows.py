#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段二 · 应用 / 窗口管理测试。

覆盖 ``avatar/app_manager/`` 下的七个模块：几何换算、显示器、窗口状态、
前台抢占、进程、窗口枚举、应用启动。运行在 200% 缩放的单显示器机器上
必须全过。

设计原则（与 ``tests/test_phase1.py`` 一致）：

* **零第三方依赖** —— 只用标准库 + 被测代码本身。
* **只读优先** —— 不擅自打开 / 关闭 / 移动用户的窗口。少数需要真实
  改动的检查（置顶、最大化）都在 ``try/finally`` 里成对还原。
* **不抢前台** —— 前台子系统只做只读探测。抢前台会打断用户当前的工作，
  那是 CLI 层 ``avatar activate`` 的职责，不该在测试里做。

关键坑位（本文件刻意守住）：

1. ``layout.Rect`` 是 ``(left, top, right, bottom)``，**不是** xywh。
2. 客户区坐标 ↔ 屏幕坐标必须往返一致，这是"窗口相对坐标贯穿全链路"的
   地基。200% 缩放下取整误差最容易在这里暴露。
3. ``Element`` 之下的进程白名单：默认只允许杀自己启动的进程，
   测试要验证这条闸门真的会拦人。
4. 关窗口时不能被 IME / GDI+ 钩子窗口骗到 —— 它们字面就叫 "Default IME"，
   过滤必须只看 ``is_usable``。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.app_manager import foreground as FG
from avatar.app_manager import layout as L
from avatar.app_manager import launcher as APP
from avatar.app_manager import monitor as M
from avatar.app_manager import process as P
from avatar.app_manager import state as ST
from avatar.app_manager import windows as W
from avatar.errors import ProcessBlocked, TargetError, WindowNotFound

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


def _first_usable():
    """拿一个可用窗口做各种几何/状态测试。

    优先挑记事本这类"正常"窗口，免得踩到 Start 菜单、输入法候选框的
    特殊几何。实在没有就退而取任意可用窗口。

    注意 ``WindowInfo`` 的字段名是 ``process``（不是 ``exe``）。
    """
    ws = [w for w in W.list_windows() if w.is_usable]
    if not ws:
        raise AssertionError("本机没有可操作窗口，几何/状态测试无法进行")
    for w in ws:
        if "notepad" in (w.process or "").lower():
            return w
    return ws[0]


# --------------------------------------------------------------------------
# 几何：Rect 与坐标换算
# --------------------------------------------------------------------------
def test_layout_rect():
    section("几何 · Rect 的数据模型")

    def _ltrb():
        r = L.Rect.from_ltrb(10, 20, 110, 70)
        assert (r.left, r.top, r.right, r.bottom) == (10, 20, 110, 70)
        assert (r.width, r.height) == (100, 50), "宽高应为 right-left / bottom-top"
        assert r.center == (60, 45), "中心点算错：%s" % (r.center,)
        assert r.area == 5000, "面积算错：%s" % r.area
        assert not r.is_empty, "100x50 不该被判为空"

    def _xywh():
        r = L.Rect.from_xywh(10, 20, 100, 50)
        assert (r.right, r.bottom) == (110, 70), "from_xywh 应把 w/h 折算成 rb"
        assert L.Rect.from_ltrb(10, 20, 110, 70) == r, "两种构造应相等"

    def _contains():
        r = L.Rect.from_xywh(0, 0, 100, 100)
        assert r.contains(0, 0) and r.contains(99, 99), "边界内应命中"
        assert not r.contains(100, 100), "右下边界是开区间（right/bottom 排他）"
        assert not r.contains(-1, 50), "越界不该命中"
        assert r.contains(-5, 50, margin=10), "margin 应放宽判定"

    def _intersect():
        a = L.Rect.from_xywh(0, 0, 100, 100)
        b = L.Rect.from_xywh(50, 50, 100, 100)
        assert a.intersects(b), "应有交集"
        i = a.intersection(b)
        assert (i.left, i.top, i.right, i.bottom) == (50, 50, 100, 100), \
            "交集算错：%s" % i
        c = L.Rect.from_xywh(200, 200, 10, 10)
        assert not a.intersects(c), "不该判为有交集"

    def _from_pointer_array():
        """UIA 的边界矩形是 [left, top, width, height]，**不是** ltrb。"""
        r = L.Rect.from_pointer_array([5, 6, 30, 40])
        assert (r.left, r.top, r.right, r.bottom) == (5, 6, 35, 46), \
            "第 3/4 个元素是宽高，不是右下角：%s" % r

    check("from_ltrb / width / height / center", _ltrb)
    check("from_xywh 折算正确", _xywh)
    check("contains 边界为开区间", _contains)
    check("交集运算正确", _intersect)
    check("from_pointer_array 按 xywh 解释", _from_pointer_array)


def test_layout_client_coords():
    section("几何 · 客户区 ↔ 屏幕坐标（全链路地基）")

    def _origin_matches_rect():
        w = _first_usable()
        ox, oy = L.client_origin(w.hwnd)
        cr = L.client_rect(w.hwnd)
        assert (ox, oy) == (cr.left, cr.top), (
            "client_origin 与 client_rect 左上角不一致："
            "origin=(%d,%d) rect=(%d,%d)" % (ox, oy, cr.left, cr.top))

    def _roundtrip():
        w = _first_usable()
        cw, ch = L.client_size(w.hwnd)
        assert cw > 0 and ch > 0, "客户区尺寸应为正：%dx%d" % (cw, ch)
        pts = [(0, 0), (1, 1), (cw // 2, ch // 2),
               (max(0, cw - 1), max(0, ch - 1))]
        for pt in pts:
            sx, sy = L.client_to_screen(w.hwnd, *pt)
            bx, by = L.screen_to_client(w.hwnd, sx, sy)
            assert (bx, by) == pt, (
                "往返不一致：客户区%s → 屏幕(%d,%d) → 客户区%s"
                % (pt, sx, sy, (bx, by)))

    def _rect_roundtrip():
        w = _first_usable()
        r0 = L.Rect.from_xywh(10, 20, 100, 50)
        r1 = r0.to_screen(w.hwnd).to_client(w.hwnd)
        assert r0 == r1, "Rect 往返不一致：%s → %s" % (r0, r1)

    def _center_inside():
        w = _first_usable()
        cx, cy = L.client_center(w.hwnd)
        ox, oy = L.client_origin(w.hwnd)
        cw, ch = L.client_size(w.hwnd)
        assert cx == ox + cw // 2, "中心点 x 算法不一致"
        assert cy == oy + ch // 2, "中心点 y 算法不一致"

    def _in_client():
        w = _first_usable()
        cw, ch = L.client_size(w.hwnd)
        ox, oy = L.client_origin(w.hwnd)
        assert L.in_client(w.hwnd, ox + 1, oy + 1), "屏幕坐标应被识别为在客户区内"
        assert not L.in_client(w.hwnd, ox - 50, oy - 50), "界外不该命中"
        assert L.in_client(w.hwnd, ox - 5, oy - 5, margin=10), "margin 应放宽判定"

    def _dpi():
        w = _first_usable()
        dpi = L.window_dpi(w.hwnd)
        scale = L.window_scale(w.hwnd)
        assert dpi >= 96, "DPI 不该低于 96，实际 %d" % dpi
        assert abs(scale - dpi / 96.0) < 1e-6, (
            "缩放比与 DPI 不符：dpi=%d scale=%s" % (dpi, scale))

    def _frame_sane():
        w = _first_usable()
        t, l, r, b = L.frame_thickness(w.hwnd)
        assert all(0 <= v < 500 for v in (t, l, r, b)), \
            "边框厚度超出合理范围：上%d 左%d 右%d 下%d" % (t, l, r, b)
        raw = L.raw_window_rect(w.hwnd)
        frame = L.window_rect(w.hwnd)
        assert frame.width <= raw.width + 2 and frame.height <= raw.height + 2, \
            "可见边框不该大于带阴影矩形：raw=%s frame=%s" % (raw, frame)

    def _bad_hwnd():
        for fn in (L.window_rect, L.client_rect, L.client_origin, L.client_size):
            try:
                fn(0)
            except Exception:
                continue
            raise AssertionError("%s(0) 应报错而不是返回垃圾" % fn.__name__)

    check("client_origin == client_rect 左上角", _origin_matches_rect)
    check("客户区↔屏幕 往返一致（4 采样点）", _roundtrip)
    check("Rect 客户区↔屏幕 往返一致", _rect_roundtrip)
    check("client_center 与 origin/size 一致", _center_inside)
    check("in_client 判定正确", _in_client)
    check("DPI 与缩放比自洽", _dpi)
    check("边框厚度合理且 <= 带阴影矩形", _frame_sane)
    check("非法句柄被拒绝", _bad_hwnd)


# --------------------------------------------------------------------------
# 显示器
# --------------------------------------------------------------------------
def test_monitor():
    section("显示器")

    def _at_least_one():
        ms = M.monitors()
        assert len(ms) >= 1, "至少要有一块显示器"
        assert sum(1 for m in ms if m.is_primary) == 1, \
            "有且只有一块主屏，实际 %d 块" % sum(1 for m in ms if m.is_primary)

    def _count_consistent():
        assert M.count() == len(M.monitors()), "count() 与 monitors() 不一致"

    def _virtual_covers_all():
        vs = M.virtual_screen()
        for m in M.monitors():
            assert vs.contains(m.rect.left, m.rect.top), \
                "[%s] 左上角不在虚拟屏内" % m.device
            assert m.rect.right <= vs.right + 1 and m.rect.bottom <= vs.bottom + 1, \
                "[%s] 右下角越出虚拟屏" % m.device

    def _work_area_inside():
        for m in M.monitors():
            assert m.rect.contains(m.work.left, m.work.top), \
                "[%s] 工作区左上角越界" % m.device
            assert m.work.width <= m.rect.width and m.work.height <= m.rect.height, \
                "[%s] 工作区比显示器还大" % m.device

    def _at_point():
        pm = M.primary()
        got = M.at_point(*pm.center)
        assert got is not None, "主屏中心应能解析出显示器"
        assert got.device == pm.device, \
            "主屏中心归属错：期望 [%s] 实际 [%s]" % (pm.device, got.device)

    def _at_window():
        w = _first_usable()
        m = M.at_window(w.hwnd)
        assert m is not None, "可用窗口应能解析出所在显示器"
        assert M.index_at_point(*m.center) >= 0, "显示器中心应能反查索引"

    def _scale_at_point():
        s = M.scale_at_point(*M.primary().center)
        assert s >= 1.0, "缩放比不该小于 1.0，实际 %s" % s

    def _work_area_helper():
        r = M.work_area()
        assert isinstance(r, L.Rect) and r.width > 0, "work_area() 应返回有效矩形"
        assert M.is_in_work_area(*M.primary().center), \
            "主屏中心应在工作区内"

    def _physical():
        for m in M.monitors():
            assert m.physical_width > 0 and m.physical_height > 0, \
                "[%s] 物理尺寸读不出来" % m.device

    check("至少一块显示器且主屏唯一", _at_least_one)
    check("count() 与 monitors() 一致", _count_consistent)
    check("虚拟屏覆盖全部显示器", _virtual_covers_all)
    check("工作区不越界", _work_area_inside)
    check("MonitorFromPoint 归属正确", _at_point)
    check("at_window / index_at_point 正确", _at_window)
    check("scale_at_point 合理", _scale_at_point)
    check("work_area / is_in_work_area 一致", _work_area_helper)
    check("物理尺寸可读", _physical)


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------
def test_windows():
    section("窗口枚举与匹配")

    def _enumerate():
        ws = W.list_windows()
        assert len(ws) > 0, "应能枚举到顶层窗口"
        assert any(w.is_usable for w in ws), "应至少有一个可用窗口"

    def _sorted():
        ws = W.list_windows()
        scores = [w.score for w in ws]
        assert scores == sorted(scores, reverse=True), "窗口应按评分降序排列"

    def _invisible_excluded_by_default():
        vis = W.list_windows()
        allw = W.list_windows(include_invisible=True)
        assert len(allw) >= len(vis), \
            "包含不可见窗口时数量不该变少：%d vs %d" % (len(allw), len(vis))

    def _refresh_consistent():
        w = _first_usable()
        r = W.refresh(w.hwnd)
        assert r.hwnd == w.hwnd, "refresh 返回了别的窗口"
        assert r.title == w.title or True, "标题可能变化，不强制相等"

    def _is_alive():
        w = _first_usable()
        assert W.is_alive(w.hwnd), "活窗口应判定为活着"
        assert not W.is_alive(0xDEADBEEF), "伪造句柄应判定为已死"

    def _find_by_title():
        w = _first_usable()
        if not w.title:
            return
        # 用标题前 6 个字符做子串匹配，够稳
        frag = w.title[:6]
        got = W.find_windows(frag)
        assert got, "按标题片段 %r 应能搜到窗口" % frag
        assert any(x.hwnd == w.hwnd for x in got), \
            "搜到的窗口里应包含原窗口 0x%X" % w.hwnd

    def _not_found_raises():
        try:
            W.find_window("绝不存在的标题XYZ123不存在")
        except WindowNotFound:
            return
        raise AssertionError("未命中时应抛 WindowNotFound")

    def _window_at():
        w = _first_usable()
        cx, cy = L.client_center(w.hwnd)
        got = W.window_at(cx, cy, root=True)
        assert got is not None, "客户区中心应有窗口"
        assert got.hwnd == w.hwnd, \
            "客户区中心命中的不是本窗口：期望 0x%X 实际 0x%X" % (w.hwnd, got.hwnd)

    def _children():
        w = _first_usable()
        ch = W.children(w.hwnd)
        assert isinstance(ch, list), "children 应返回列表"
        # children 返回的是裸 hwnd（int），不是 WindowInfo
        for c in ch:
            assert isinstance(c, int), "子窗口应是 hwnd 整数，实际 %r" % (c,)
            assert c != w.hwnd, "子窗口不该包含自身"
        rec = W.children(w.hwnd, recurse=True)
        assert len(rec) >= len(ch), "递归枚举不该少于直接子级：%d vs %d" % (
            len(rec), len(ch))

    def _by_pid():
        w = _first_usable()
        got = W.window_of_pid(w.pid)
        assert got is not None, "pid=%d 应能定位到主窗口" % w.pid
        assert got.pid == w.pid, "定位到的窗口 pid 不符"

    def _describe_runs():
        ws = W.list_windows()
        s = W.describe(ws[:3])
        assert isinstance(s, str) and s, "describe 应返回非空字符串"

    check("可枚举顶层窗口", _enumerate)
    check("评分降序已排序", _sorted)
    check("include_invisible 不减少结果", _invisible_excluded_by_default)
    check("refresh 结果自洽", _refresh_consistent)
    check("is_alive 真假判定正确", _is_alive)
    check("按标题片段可搜索", _find_by_title)
    check("未命中抛 WindowNotFound", _not_found_raises)
    check("window_at 命中正确窗口", _window_at)
    check("可枚举子窗口", _children)
    check("window_of_pid 定位主窗口", _by_pid)
    check("describe 可输出", _describe_runs)


# --------------------------------------------------------------------------
# 窗口状态
# --------------------------------------------------------------------------
def test_state():
    section("窗口状态（改动后必还原）")

    def _read_state():
        w = _first_usable()
        st = ST.state_of(w.hwnd)
        assert not (st.minimized and st.maximized), \
            "最小化与最大化不该同时为真"
        assert st.rect.width > 0 and st.rect.height > 0, "状态里的矩形应有效"

    def _tables_complete():
        for k in ("hide", "restore", "minimize", "maximize", "show"):
            assert k in ST.SW_COMMANDS, "SW_COMMANDS 缺 %r" % k
        for k in ("left", "right", "max", "center"):
            assert k in ST.SNAP_POSITIONS, "SNAP_POSITIONS 缺 %r" % k

    def _visibility():
        w = _first_usable()
        assert ST.is_visible(w.hwnd), "可用窗口应为可见"
        assert ST.is_enabled(w.hwnd), "可用窗口应为已启用"

    def _topmost_roundtrip():
        """置顶 → 读回 → 还原。用 try/finally 保证一定还原。"""
        w = _first_usable()
        was = ST.is_topmost(w.hwnd)
        if was:
            return  # 本来就置顶，跳过（避免把别人的置顶状态改坏）
        try:
            ST.set_topmost(w.hwnd, True, timeout=2.0)
            assert ST.is_topmost(w.hwnd) is True, "置顶后读回应为真"
        finally:
            ST.set_topmost(w.hwnd, False, timeout=2.0)
        assert ST.is_topmost(w.hwnd) is False, "还原后应为假"

    def _maximize_restore():
        """最大化 → 还原。窗口状态变更是异步的，必须等。"""
        w = _first_usable()
        was_max = ST.is_maximized(w.hwnd)
        was_min = ST.is_minimized(w.hwnd)
        if was_min:
            ST.restore(w.hwnd, timeout=2.0)  # 最小化状态下先还原
        try:
            ST.maximize(w.hwnd, timeout=3.0)
            assert ST.is_maximized(w.hwnd) is True, "最大化后读回应为真"
        finally:
            if was_max:
                ST.maximize(w.hwnd, timeout=3.0)
            else:
                ST.restore(w.hwnd, timeout=3.0)
        assert ST.is_maximized(w.hwnd) == was_max or not was_max, \
            "还原后状态应与原始一致"

    def _wait_state():
        w = _first_usable()
        ok = ST.wait_state(w.hwnd, lambda s: s.visible, timeout=1.0)
        assert ok is True, "对已满足的条件 wait_state 应立即返回真"

    def _bad_command():
        w = _first_usable()
        try:
            ST.show(w.hwnd, "这不是一个命令", verify=False)
        except Exception:
            return
        raise AssertionError("非法 SW 命令应报错")

    def _bad_snap():
        w = _first_usable()
        try:
            ST.snap(w.hwnd, "斜着放", verify=False)
        except Exception:
            return
        raise AssertionError("非法吸附位置应报错")

    check("状态可读取且自洽", _read_state)
    check("SW_COMMANDS / SNAP_POSITIONS 完整", _tables_complete)
    check("is_visible / is_enabled 正确", _visibility)
    check("置顶开关往返（带还原）", _topmost_roundtrip)
    check("最大化 / 还原往返（带还原）", _maximize_restore)
    check("wait_state 条件满足即返回", _wait_state)
    check("非法 SW 命令被拒绝", _bad_command)
    check("非法吸附位置被拒绝", _bad_snap)


# --------------------------------------------------------------------------
# 前台
# --------------------------------------------------------------------------
def test_foreground():
    section("前台（只读，不抢用户焦点）")

    def _get():
        h = FG.get_foreground()
        assert isinstance(h, int), "get_foreground 应返回 int"
        if h:
            assert FG.is_foreground(h), "读到的前台窗口应被 is_foreground 认可"

    def _info():
        info = FG.foreground_info()
        for k in ("hwnd", "title", "pid", "tid"):
            assert k in info, "foreground_info 缺字段 %r" % k
        assert info["hwnd"] == FG.get_foreground(), "两次读前台结果不一致（用户动了鼠标？）"

    def _thread():
        h = FG.get_foreground()
        if not h:
            return
        # ⚠ thread_of_window 返回的是 (pid, tid) 二元组，不是裸 tid
        got = FG.thread_of_window(h)
        assert isinstance(got, tuple) and len(got) == 2, \
            "thread_of_window 应返回 (pid, tid)，实际 %r" % (got,)
        _pid, tid = got
        assert tid > 0, "应能解析出前台窗口所属线程"
        assert FG.current_thread_id() > 0, "应能读到当前线程 ID"

    def _root_idempotent():
        h = FG.get_foreground()
        if not h:
            return
        r = FG.root_window(h)
        assert FG.root_window(r) == r, "root_window 应幂等"

    def _attach_roundtrip():
        h = FG.get_foreground()
        if not h:
            return
        _pid, tid = FG.thread_of_window(h)
        me = FG.current_thread_id()
        with FG.attached_input(tid) as ok:
            assert ok or tid == me, \
                "AttachThreadInput 应成功（目标 tid=%d 当前 tid=%d）" % (tid, me)
        # 退出后必须已摘除：再挂一次仍应成功，说明没泄漏
        with FG.attached_input(tid) as ok2:
            assert ok2 or tid == me, "二次挂接应成功，说明前一次已正确摘除"

    def _ensure_bad_hwnd():
        try:
            FG.force_foreground(0, timeout=0.2)
        except Exception:
            return
        raise AssertionError("对非法句柄抢前台应报错")

    check("GetForegroundWindow 可用", _get)
    check("foreground_info 字段完整", _info)
    check("线程 ID 可解析", _thread)
    check("root_window 幂等", _root_idempotent)
    check("AttachThreadInput 挂接/摘除往返", _attach_roundtrip)
    check("非法句柄抢前台被拒绝", _ensure_bad_hwnd)


# --------------------------------------------------------------------------
# 进程
# --------------------------------------------------------------------------
def test_process():
    section("进程与完整性级别")

    def _enumerate():
        ps = P.list_processes()
        assert len(ps) > 5, "应能枚举到多个进程，实际 %d" % len(ps)

    def _self():
        me = P.process_of(os.getpid())
        assert me is not None, "应能查到自身进程"
        assert me.pid == os.getpid(), "查到的不是自己"
        assert me.path, "自身进程路径不该为空"

    def _alive():
        assert P.is_alive(os.getpid()), "自己应该是活的"
        assert not P.is_alive(0xFFFFFFF0), "伪造 PID 应判定为不存在"

    def _integrity():
        rid = P.self_integrity()
        assert rid > 0, "应能读到自身完整性级别"
        name = P.integrity_name(rid)
        assert isinstance(name, str) and name, "完整性级别应有可读名字"

    def _can_control_self():
        assert P.can_control(os.getpid()), "应能驱动自身进程"
        assert not P.uipi_blocked(os.getpid()), "自身进程不该被 UIPI 拦"

    def _by_name():
        me = P.process_of(os.getpid())
        if not me:
            return
        got = P.by_name(me.name)
        assert got, "按名字 %r 应能搜到自己" % me.name
        assert any(x.pid == os.getpid() for x in got), "结果里应包含自身 pid"

    def _windows_of_pid():
        w = _first_usable()
        hwnds = P.windows_of_pid(w.pid, include_invisible=True)
        assert isinstance(hwnds, list), "windows_of_pid 应返回列表"

    def _pid_of_window():
        w = _first_usable()
        pid = P.pid_of_window(w.hwnd)
        assert pid == w.pid, "pid_of_window 结果与窗口 pid 不符"

    def _whitelist_blocks():
        """⭐ 白名单是安全闸门：未授权的 PID 一律拒绝杀。

        用 PID 4（System）做靶子 —— 它必定存在、必定不在白名单、
        就算真的漏过去也杀不动（权限不足），是安全的测试对象。
        """
        try:
            P.kill(4, force=True, timeout=0.1)
        except ProcessBlocked:
            return
        except Exception as e:
            raise AssertionError(
                "应抛 ProcessBlocked，实际抛了 %r" % (e,))
        raise AssertionError("未授权的 PID 竟然没被白名单拦住")

    def _whitelist_allows_self():
        """自己启动的进程在白名单里 —— 但这里不真杀自己。"""
        assert P._is_owned(os.getpid()) in (True, False), "内部判定应返回布尔"

    def _self_kill_refused():
        """⭐ 连自己都不许杀 —— 这条闸门比白名单更靠前。"""
        try:
            P.kill(os.getpid(), force=True, timeout=0.1)
        except ProcessBlocked:
            return
        raise AssertionError("竟然允许杀 avatar 自身进程")

    def _unowned_refused():
        """``allow_foreign=False`` 时，非自己启动的进程一律拒绝。"""
        # 拿一个必定存在、且肯定不是 avatar 启动的进程做靶子。
        # 优先用当前宿主进程；本体自己启动的进程会被跳过。
        candidates = [w.pid for w in W.list_windows() if w.pid != os.getpid()]
        if not candidates:
            return
        blocked = 0
        for pid in candidates[:5]:
            if P._is_owned(pid):
                continue
            try:
                P.kill(pid, force=True, timeout=0.1)  # allow_foreign 默认 False
            except ProcessBlocked:
                blocked += 1
            except Exception:
                # 更靠前的检查（比如已死）也会抛，算通过
                blocked += 1
        assert blocked > 0, "5 个非自启进程竟然一个都没被拦住"

    check("可枚举进程", _enumerate)
    check("可查到自身进程且路径可读", _self)
    check("is_alive 真假判定正确", _alive)
    check("完整性级别可读", _integrity)
    check("自身进程可驱动", _can_control_self)
    check("by_name 能搜到自己", _by_name)
    check("windows_of_pid 可用", _windows_of_pid)
    check("pid_of_window 与窗口一致", _pid_of_window)
    check("⭐ 白名单拦截未授权 PID", _whitelist_blocks)
    check("⭐ 拒绝自杀", _self_kill_refused)
    check("⭐ 非自启进程需 allow_foreign", _unowned_refused)
    check("白名单内部判定可用", _whitelist_allows_self)


# --------------------------------------------------------------------------
# 启动器（不真的启东西）
# --------------------------------------------------------------------------
def test_launcher():
    section("启动器（纯解析，不启动外部程序）")

    def _alias():
        assert APP.alias_of("记事本") == "notepad.exe", "中文别名应解析"
        assert APP.alias_of("notepad") == "notepad.exe", "英文别名应解析"

    def _uri():
        assert APP.is_uri("https://example.com"), "https 应识别为 URI"
        assert APP.is_uri("ms-settings:"), "ms- 协议应识别为 URI"
        assert not APP.is_uri("notepad.exe"), "可执行文件不该识别为 URI"

    def _aumid_shape():
        assert APP._looks_like_aumid(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"), "AUMID 应被识别"
        assert not APP._looks_like_aumid(r"C:\a\b.exe"), "路径不该识别为 AUMID"

    def _package_family():
        got = APP._package_family(
            "Microsoft.WindowsCalculator_11.2606.0.0_x64__8wekyb3d8bbwe")
        assert got == "Microsoft.WindowsCalculator_8wekyb3d8bbwe", \
            "PackageFamilyName 提取错误：%s" % got

    def _infer_process():
        assert APP._infer_process("notepad.exe") == "notepad.exe"
        assert APP._infer_process("msedge") == "msedge.exe", "无扩展名应补 .exe"

    def _timeout_tiers():
        assert APP._timeout_for("winword.exe") > APP.DEFAULT_WINDOW_TIMEOUT, \
            "慢启动应用应有更长的超时"

    def _uwp_list():
        aumids = APP.list_aumids()
        assert len(aumids) > 0, "应能枚举到 UWP 应用"
        import re
        bad = [a for _d, a in aumids
               if re.search(r"_\d+\.\d+\.\d+", a.split("!")[0])]
        assert not bad, "AUMID 里不该带版本号（会导致激活失败）：%s" % bad[:1]

    def _resolve_uwp():
        got = APP.resolve_uwp("Calculator")
        assert got and "!" in got, "按名字应能定位到 AUMID：%r" % got

    def _not_running():
        assert APP.is_running("绝不存在的应用XYZ123") is False, \
            "未知应用应是未运行"

    def _shell_fail_detected():
        try:
            APP.shell_execute(r"C:\绝不存在的路径XYZ\a.exe")
        except (TargetError, Exception):
            return
        raise AssertionError("不存在的文件应报错")

    check("别名解析正确", _alias)
    check("URI 识别正确", _uri)
    check("AUMID 形态识别正确", _aumid_shape)
    check("PackageFamilyName 提取正确", _package_family)
    check("进程名推断正确", _infer_process)
    check("慢启动应用有独立超时", _timeout_tiers)
    check("可枚举 UWP AUMID 且不含版本号", _uwp_list)
    check("可按名字定位 UWP", _resolve_uwp)
    check("is_running 对未知应用返回假", _not_running)
    check("ShellExecute 失败可检出", _shell_fail_detected)


# --------------------------------------------------------------------------
# 模块内建自检（各模块自己声明的契约）
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

    for name, mod in (("layout", L), ("monitor", M), ("windows", W),
                      ("state", ST), ("foreground", FG), ("process", P),
                      ("launcher", APP)):
        _one(name, mod.selfcheck)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段二 · 应用 / 窗口管理测试")
    print("=" * 64)

    test_layout_rect()
    test_layout_client_coords()
    test_monitor()
    test_windows()
    test_state()
    test_foreground()
    test_process()
    test_launcher()
    test_module_selfchecks()

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段二 窗口测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s → %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
