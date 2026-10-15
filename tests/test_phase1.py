# -*- coding: utf-8 -*-
"""阶段一测试：DPI / 坐标 / 抓屏 / 窗口。

不依赖 pytest（保持零依赖），可以直接 `python tests/test_phase1.py` 跑。
如果装了 pytest 也能被收集。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar import perception as P
from avatar._win32 import api as A
from avatar._win32 import dpi as D
from avatar._win32 import structs as S
from avatar.app_manager import windows as W
from avatar.vision import capture as C


# --------------------------------------------------------------------------
# 极简断言框架
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


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------
def test_struct_sizes():
    section("结构体布局（静默失败的头号来源）")

    def _input_size():
        n = ctypes.sizeof(S.INPUT)
        assert n == S.expected_input_size(), \
            "sizeof(INPUT)=%d，期望 %d —— SendInput 会静默失败" % (n, S.expected_input_size())

    def _mouse_input():
        # MOUSEINPUT: dx,dy,mouseData,dwFlags (4×4=16) + 4 字节对齐填充
        #             + dwExtraInfo 指针 (8) + 4 字节尾部填充 = 32
        n = ctypes.sizeof(S.MOUSEINPUT)
        assert n == 32, "sizeof(MOUSEINPUT)=%d，期望 32" % n

    def _keybd_input():
        # KEYBDINPUT: wVk,wScan (2+2) + dwFlags,time (4+4) = 12
        #             + 4 字节对齐填充 + dwExtraInfo 指针 (8) = 24
        n = ctypes.sizeof(S.KEYBDINPUT)
        assert n == 24, "sizeof(KEYBDINPUT)=%d，期望 24" % n

    def _union_size():
        # union 取最大成员：MOUSEINPUT(32) 胜出。若这条错了，
        # 说明某个成员的结构体声明有误，SendInput 会静默失败。
        n = ctypes.sizeof(S._INPUTUNION)
        assert n == 32, "sizeof(_INPUTUNION)=%d，期望 32（MOUSEINPUT 应最大）" % n

    def _rect():
        assert ctypes.sizeof(S.RECT) == 16, "sizeof(RECT)=%d" % ctypes.sizeof(S.RECT)
        r = S.RECT(10, 20, 110, 220)
        assert r.as_tuple() == (10, 20, 100, 200), r.as_tuple()
        assert r.width == 100 and r.height == 200

    def _point():
        assert ctypes.sizeof(S.POINT) == 8

    def _devmode():
        # DEVMODEW 必须足够大，否则 EnumDisplaySettingsW 会写越界
        n = ctypes.sizeof(S.DEVMODEW)
        assert n >= 200, "sizeof(DEVMODEW)=%d 偏小，EnumDisplaySettingsW 可能写越界" % n

    check("sizeof(INPUT) 正确（x64=40）", _input_size)
    check("sizeof(MOUSEINPUT) 正确（32）", _mouse_input)
    check("sizeof(KEYBDINPUT) 正确（24）", _keybd_input)
    check("INPUT union 尺寸正确（32）", _union_size)
    check("RECT 布局与属性", _rect)
    check("POINT 布局", _point)
    check("DEVMODEW 尺寸充足", _devmode)


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------
def test_dpi():
    section("DPI 适配")

    def _aware():
        st = D.set_process_dpi_awareness()
        assert st.ok, "DPI 感知未生效: %s" % st

    def _verify():
        assert D.verify(), "交叉验证失败 —— 坐标会偏移"

    def _metrics_match():
        pm = D.primary_monitor()
        assert pm is not None, "取不到主显示器"
        gw = A.user32.GetSystemMetrics(S.SM_CXSCREEN)
        assert abs(gw - pm.physical_width) <= 1, \
            "GetSystemMetrics=%d 与 EnumDisplaySettings=%d 不一致 —— 仍在逻辑坐标系" \
            % (gw, pm.physical_width)

    def _scale_sane():
        pm = D.primary_monitor()
        assert 0.5 <= pm.scale_x <= 5.0, "缩放系数异常: %s" % pm.scale_x

    def _monitor_props():
        pm = D.primary_monitor()
        assert pm.refresh_hz > 0, "刷新率取不到: %d" % pm.refresh_hz
        assert pm.bits_per_pel in (16, 24, 32), "色深异常: %d" % pm.bits_per_pel
        assert pm.physical_width > 0 and pm.physical_height > 0

    check("进程为 Per-Monitor DPI Aware", _aware)
    check("DPI 交叉验证通过", _verify)
    check("GetSystemMetrics 与 EnumDisplaySettings 一致", _metrics_match)
    check("缩放系数在合理区间", _scale_sane)
    check("显示器刷新率/色深可读", _monitor_props)


# --------------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------------
def test_coordinates():
    section("坐标换算（全部物理像素）")

    def _roundtrip():
        """物理 → 逻辑 → 物理 应能还原（200% 缩放下无损）。"""
        pm = D.primary_monitor()
        for pt in [(0, 0), (100, 100), (1439, 899), (pm.physical_width - 1, pm.physical_height - 1)]:
            lx, ly = D.physical_to_logical(*pt)
            bx, by = D.logical_to_physical(lx, ly)
            # 缩放一定是整数比时无损；非整数比允许 1px 误差
            assert abs(bx - pt[0]) <= max(1, int(pm.scale_x)), \
                "%s → 逻辑(%d,%d) → 物理(%d,%d) 偏差过大" % (pt, lx, ly, bx, by)

    def _normalize_range():
        pm = D.primary_monitor()
        for pt in [(0, 0), pm.center,
                   (pm.physical_width - 1, pm.physical_height - 1)]:
            nx, ny = D.normalize_absolute(*pt)
            assert 0 <= nx <= 65535 and 0 <= ny <= 65535, \
                "%s 归一化越界: (%d,%d)" % (pt, nx, ny)

    def _normalize_endpoints():
        """原点 → 0，右下角 → 65535（单屏、原点在 (0,0) 时）。"""
        vx, vy, vw, vh = D.virtual_screen()
        nx0, ny0 = D.normalize_absolute(vx, vy)
        nx1, ny1 = D.normalize_absolute(vx + vw - 1, vy + vh - 1)
        assert (nx0, ny0) == (0, 0), "原点归一化应为 (0,0)，实际 (%d,%d)" % (nx0, ny0)
        assert (nx1, ny1) == (65535, 65535), \
            "右下角归一化应为 (65535,65535)，实际 (%d,%d)" % (nx1, ny1)

    def _clamp():
        vx, vy, vw, vh = D.virtual_screen()
        assert D.clamp_to_screen(vx - 999, vy - 999) == (vx, vy)
        assert D.clamp_to_screen(vx + vw + 999, 0) == (vx + vw - 1, vy)

    def _monitor_at():
        pm = D.primary_monitor()
        m = D.monitor_at_point(*pm.center)
        assert m is not None, "主屏中心点应属于某块显示器"
        assert m.device == pm.device

    def _scale_at():
        pm = D.primary_monitor()
        assert abs(P.layout().scale_at(*pm.center) - pm.scale_x) < 1e-6

    check("物理↔逻辑 往返一致", _roundtrip)
    check("归一化值在 0..65535 内", _normalize_range)
    check("归一化端点映射正确", _normalize_endpoints)
    check("坐标夹紧到屏幕内", _clamp)
    check("MonitorFromPoint 归属正确", _monitor_at)
    check("按点取缩放系数", _scale_at)


# --------------------------------------------------------------------------
# 虚拟桌面
# --------------------------------------------------------------------------
def test_virtual_screen():
    section("虚拟桌面（多屏布局）")

    def _nonzero():
        vx, vy, vw, vh = D.virtual_screen()
        assert vw > 0 and vh > 0, "虚拟桌面尺寸非法: %dx%d" % (vw, vh)

    def _cover_all():
        vx, vy, vw, vh = D.virtual_screen()
        for m in D.list_monitors():
            mx, my, mw, mh = m.rect
            assert mx >= vx and my >= vy, "显示器 [%d] 超出虚拟桌面左上" % m.monitor_index
            assert mx + mw <= vx + vw and my + mh <= vy + vh, \
                "显示器 [%d] 超出虚拟桌面右下" % m.monitor_index

    def _no_overlap():
        """显示器矩形之间不应重叠（镜像模式除外，此时系统只报一块）。"""
        mons = D.list_monitors()
        for i in range(len(mons)):
            for j in range(i + 1, len(mons)):
                ax, ay, aw, ah = mons[i].rect
                bx, by, bw, bh = mons[j].rect
                overlap = not (ax + aw <= bx or bx + bw <= ax or
                               ay + ah <= by or by + bh <= ay)
                assert not overlap, \
                    "显示器 [%d] 与 [%d] 矩形重叠（镜像模式应只报一块）" % (i, j)

    def _count():
        assert D.monitor_count() == len(D.list_monitors()), \
            "SM_CMONITORS=%d 但枚举到 %d 块" % (D.monitor_count(), len(D.list_monitors()))

    check("虚拟桌面尺寸非零", _nonzero)
    check("虚拟桌面覆盖全部显示器", _cover_all)
    check("显示器矩形互不重叠", _no_overlap)
    check("显示器计数与枚举一致", _count)


# --------------------------------------------------------------------------
# 抓屏
# --------------------------------------------------------------------------
def test_capture():
    section("抓屏与像素")

    def _full():
        bmp = C.grab_screen(None)
        vx, vy, vw, vh = D.virtual_screen()
        assert bmp.size == (vw, vh), \
            "全屏抓图 %s 与虚拟桌面 %dx%d 不符" % (bmp.size, vw, vh)
        assert bmp.origin == (vx, vy)

    def _region():
        bmp = C.grab_screen((100, 100, 300, 200))
        assert bmp.size == (300, 200), bmp.size
        assert bmp.origin == (100, 100), bmp.origin
        assert len(bmp.pixels) == 300 * 200 * 4

    def _region_clamped_negative():
        """负 origin 的区域（副屏在主屏左侧时常见）不应崩。"""
        bmp = C.grab_screen((-100, -50, 200, 200))
        assert bmp.size == (200, 200)
        assert bmp.origin == (-100, -50)

    def _pixel_consistency():
        """关键一致性测试：抓屏得到的像素 == GetPixel 独立读取的像素。

        这两条路径完全不同（BitBlt 整屏 vs GDI GetPixel 单点）。
        如果 DPI 或 origin 有偏差，这里第一个就会炸。
        """
        pm = D.primary_monitor()
        cx, cy = pm.center
        bmp = C.grab_screen((cx - 50, cy - 50, 100, 100))
        bad = 0
        checked = 0
        for dy in range(0, 100, 17):
            for dx in range(0, 100, 17):
                sx, sy = cx - 50 + dx, cy - 50 + dy
                a = bmp.get_pixel(dx, dy)
                b = C.pixel_at(sx, sy)
                checked += 1
                if a != b:
                    bad += 1
        assert bad == 0, "抓屏与 GetPixel 有 %d/%d 个像素不一致 —— origin/DPC 换算有问题" % (bad, checked)
        assert checked >= 30, "采样点太少"

    def _window_capture():
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return  # 无窗口可测，跳过（不算失败）
        w = ws[0]
        bmp = C.grab_window(w.hwnd)
        assert bmp.width > 0 and bmp.height > 0
        # DWM 边框通常比 GetWindowRect 略小（阴影）
        assert bmp.width <= w.width + 2, "窗口抓图比 GetWindowRect 还大: %d > %d" % (bmp.width, w.width)
        assert bmp.height <= w.height + 2

    def _window_client_origin():
        """客户区抓图的 origin 必须等于 ClientToScreen(0,0) —— 否则窗口内坐标全错。"""
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return
        w = ws[0]
        bmp = C.grab_window(w.hwnd, client_only=True)
        g = P.geometry(w.hwnd)
        assert bmp.origin == (g.client[0], g.client[1]), \
            "客户区图 origin=%s 但 ClientToScreen=%s" % (bmp.origin, (g.client[0], g.client[1]))
        assert bmp.size == g.client_size, \
            "客户区图尺寸 %s != GetClientRect %s" % (bmp.size, g.client_size)

    def _invalid_window():
        try:
            C.grab_window(0xDEADBEEF)
        except Exception:
            return
        raise AssertionError("无效句柄应抛异常，而不是返回图")

    def _region_clamped_helpers():
        bmp = C.grab_screen((0, 0, 100, 100))
        sub = bmp.region(10, 10, 50, 50)
        assert sub.size == (50, 50)
        assert sub.origin == (10, 10)
        # 越界裁剪
        sub2 = bmp.region(80, 80, 100, 100)
        assert sub2.size == (20, 20), sub2.size
        assert sub2.origin == (80, 80)

    def _downsample():
        bmp = C.grab_screen((0, 0, 200, 200))
        small = bmp.downsample(4)
        assert small.size == (50, 50), small.size
        assert len(small.pixels) == 50 * 50 * 4

    def _batch_pixels():
        pts = [(10, 10), (20, 20), (30, 30)]
        vals = C.pixel_at_batch(pts)
        assert len(vals) == 3
        for p, v in zip(pts, vals):
            assert v == C.pixel_at(*p), "批量读 %s 与单点读不一致: %s vs %s" % (p, v, C.pixel_at(*p))

    check("全屏抓图尺寸/原点正确", _full)
    check("区域抓图", _region)
    check("负坐标区域抓图不崩", _region_clamped_negative)
    check("★ 抓屏像素与 GetPixel 逐点一致", _pixel_consistency)
    check("窗口抓图", _window_capture)
    check("★ 客户区图 origin 对齐 ClientToScreen", _window_client_origin)
    check("无效窗口句柄抛异常", _invalid_window)
    check("位图 region 裁剪", _region_clamped_helpers)
    check("降采样", _downsample)
    check("批量读像素", _batch_pixels)


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------
def test_windows():
    section("窗口枚举与查找")

    def _enum():
        ws = W.list_windows()
        assert len(ws) >= 1, "一个窗口都没枚举到"

    def _sorted():
        ws = W.list_windows()
        scores = [w.score for w in ws]
        assert scores == sorted(scores, reverse=True), "窗口未按 score 降序"

    def _titles_preferred():
        """同进程多窗口时，有标题的必须排在无标题的前面。"""
        ws = W.list_windows(include_invisible=True)
        by_proc = {}
        for w in ws:
            by_proc.setdefault(w.process, []).append(w)
        for proc, group in by_proc.items():
            if len(group) < 2:
                continue
            titled = [i for i, w in enumerate(group) if w.has_title]
            untitled = [i for i, w in enumerate(group) if not w.has_title]
            if titled and untitled:
                assert min(titled) < max(untitled), \
                    "%s 的无标题窗口排到了有标题窗口前面（会点错窗口）" % proc

    def _geometry_matches():
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return
        w = ws[0]
        g = P.geometry(w.hwnd)
        assert g.hwnd == w.hwnd
        assert g.title == w.title
        assert 0 <= g.scale <= 10

    def _client_to_screen():
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return
        g = P.geometry(ws[0].hwnd)
        cx, cy = 10, 10
        sx, sy = g.client_to_screen(cx, cy)
        assert sx == g.client[0] + 10 and sy == g.client[1] + 10
        assert g.screen_to_client(sx, sy) == (cx, cy)

    def _contains():
        ws = [w for w in W.list_windows() if w.is_usable]
        if not ws:
            return
        g = P.geometry(ws[0].hwnd)
        assert g.contains_screen_point(*g.center), "窗口中心应被判为在窗口内"

    def _not_found_raises():
        try:
            W.find_window("这个标题绝对不存在_zzz_%d" % int(time.time()))
        except Exception as e:
            from avatar.errors import WindowNotFound
            assert isinstance(e, WindowNotFound), "应抛 WindowNotFound，实际 %r" % e
            return
        raise AssertionError("找不到窗口时必须抛异常，不许返回猜的结果")

    def _filter_process():
        ws = W.list_windows()
        if not ws:
            return
        target = ws[0].process
        got = W.list_windows(processes=[target])
        assert all(g.process.lower() == target.lower() for g in got)
        assert len(got) >= 1

    def _window_at_center():
        ws = [w for w in W.list_windows() if w.is_usable and w.maximized]
        if not ws:
            # 没有最大化的窗口就跳过
            return
        w = ws[0]
        g = P.geometry(w.hwnd)
        cx, cy = g.center
        found = W.window_at(cx, cy)
        assert found is not None, "屏幕坐标处应能找到窗口"
        assert found.hwnd == w.hwnd, \
            "中心点属于 0x%08X 但 WindowFromPoint 给出 0x%08X" % (w.hwnd, found.hwnd)

    check("窗口枚举非空", _enum)
    check("按 score 降序", _sorted)
    check("★ 有标题窗口优先于无标题", _titles_preferred)
    check("窗口几何与前台字段一致", _geometry_matches)
    check("客户区↔屏幕坐标往返", _client_to_screen)
    check("点包含判定", _contains)
    check("★ 找不到窗口必须抛异常", _not_found_raises)
    check("按进程名过滤", _filter_process)
    check("WindowFromPoint 归属正确", _window_at_center)


# --------------------------------------------------------------------------
# 感知快照
# --------------------------------------------------------------------------
def test_snapshot():
    section("感知快照")

    def _basic():
        snap = P.snapshot(with_screen=False)
        assert snap.layout.count >= 1
        assert len(snap.cursor) == 2
        assert snap.age >= 0

    def _with_screen():
        snap = P.snapshot(with_screen=True, region=(0, 0, 200, 200))
        assert snap.screen is not None
        assert snap.screen.size == (200, 200)
        px = snap.pixel(*snap.screen.origin)
        assert px is not None, "快照内应能读到像素"

    def _consistency():
        """快照内像素 == 直接读屏像素。"""
        region = (0, 0, 120, 120)
        snap = P.snapshot(with_screen=True, region=region)
        ox, oy = snap.screen.origin
        bad = 0
        for dy in range(0, 120, 23):
            for dx in range(0, 120, 23):
                if snap.pixel(ox + dx, oy + dy) != C.pixel_at(ox + dx, oy + dy):
                    bad += 1
        assert bad == 0, "快照与实时读屏有 %d 处不一致" % bad

    def _crop():
        snap = P.snapshot(with_screen=True, region=(0, 0, 200, 200))
        c = snap.crop(50, 50, 60, 60)
        assert c is not None and c.size == (60, 60)
        assert c.origin == (50, 50)

    def _json():
        import json
        snap = P.snapshot(with_screen=False)
        s = json.dumps(snap.to_dict(), ensure_ascii=False)
        assert "layout" in s and "cursor" in s

    check("快照基础字段", _basic)
    check("带图快照", _with_screen)
    check("★ 快照像素与实时读屏一致", _consistency)
    check("快照裁剪", _crop)
    check("快照可 JSON 序列化", _json)


# --------------------------------------------------------------------------
# 端到端自检
# --------------------------------------------------------------------------
def test_selfcheck():
    section("自检汇总")

    def _all_ok():
        r = P.selfcheck(verbose=False)
        assert r["ok"], "自检失败项: %s" % r["failures"]

    check("阶段一自检全部通过", _all_ok)


# --------------------------------------------------------------------------
def main():
    t0 = time.time()
    print("Avatar 阶段一测试")
    print("=" * 62)

    test_struct_sizes()
    test_dpi()
    test_coordinates()
    test_virtual_screen()
    test_capture()
    test_windows()
    test_snapshot()
    test_selfcheck()

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = [n for n, ok, _ in _RESULTS if not ok]

    print()
    print("=" * 62)
    print("结果: %d/%d 通过，耗时 %.2fs" % (passed, total, time.time() - t0))
    if failed:
        print("失败:")
        for n in failed:
            print("  · %s" % n)
    else:
        print("全部通过。")
    print()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
