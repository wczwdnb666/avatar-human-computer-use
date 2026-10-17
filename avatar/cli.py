# -*- coding: utf-8 -*-
"""Avatar 命令行入口。

阶段一只实现"感知"相关子命令 —— 全部是**只读**操作，不动鼠标键盘。
阶段三加上"输入"子命令 —— 会真的动鼠标键盘，务必看清参数。

    avatar selfcheck                     阶段自检（最重要）
    avatar dpi                           DPI 与显示器详情
    avatar layout                        多屏布局一页纸
    avatar windows [--process P]         列出窗口
    avatar geom --hwnd 0x1234            某个窗口的几何
    avatar cursor                        光标位置（物理 / 归一化）
    avatar pixel --x 100 --y 200         读一个像素
    avatar shot --out a.bmp              抓屏存 BMP（纯 ctypes 写文件）
    avatar pick --x 500 --y 300          坐标诊断：这个点在哪块屏、属于哪个窗口

    ── 阶段三：输入层 ──────────────────────────────────────────────
    avatar move --x N --y N [--human]    移动光标到屏幕绝对坐标
    avatar type --text "内容" [--human]  输入文本（打字节奏可真人化）
    avatar mode --set silent|physical    查看/切换交互模式
    avatar entropy --seed 42 --demo      真人熵演示与可复现性验证

    ── 阶段四：Web CDP ─────────────────────────────────────────────
    avatar web-opts                      诊断：浏览器 / 调试端口 / Electron 窗口
    avatar web-open --url U              打开页面（自动复用已有调试实例）
    avatar web-click -s "#submit"        按选择器点击（可带期望文本验证）
    avatar web-eval --js "document.title" 执行 JS

    ── 阶段四：Electron ────────────────────────────────────────────
    avatar electron-windows              列出 Electron 窗口（标出哪些能走 CDP）
    avatar electron-attach -p Code.exe   附加到 Electron 应用

    ── 阶段四：执行结果验证 ────────────────────────────────────────
    avatar verify --mode desktop --hwnd 0x1234 --expect-title "保存"
    avatar verify --mode web -s "#result" --expect-text "成功"
    avatar verify-hook --on              开启动作后的自动验证

    退出码：0=确认成功  5=确认失败  11=无法判定（未知）
    三态必须分开 —— "不知道"和"没成功"的处置方式完全不同。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
import time

# 保证以脚本方式运行时也能 import avatar 包
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from avatar import __version__
from avatar import perception as P
from avatar.app_manager import windows as W
from avatar.errors import AvatarError
from avatar._win32 import dpi as D

MASK = 0xFFFFFFFF


def _hr(title: str = ""):
    if title:
        print()
        print(title)
    print("=" * 66)


def cmd_selfcheck(a):
    r = P.selfcheck(verbose=True)
    return 0 if r["ok"] else 1


def cmd_dpi(a):
    _hr("DPI 感知")
    st = D.set_process_dpi_awareness()
    print("状态      : %s" % st)
    print("设置方式  : %s" % st.method)
    for at in st.attempts:
        print("  尝试    : %s" % at)
    print("交叉验证  : %s" % ("通过" if D.verify() else "失败（坐标会偏移！）"))

    _hr("显示器")
    lay = P.layout()
    print(lay.describe())

    _hr("尺寸对照（判断 DPI 是否真的生效）")
    import ctypes as _c
    from avatar._win32 import api as A
    from avatar._win32 import structs as S

    print("GetSystemMetrics 主屏 : %dx%d"
          % (A.user32.GetSystemMetrics(S.SM_CXSCREEN),
             A.user32.GetSystemMetrics(S.SM_CYSCREEN)))
    print("虚拟桌面              : %dx%d @ (%d,%d)"
          % (lay.virtual_size[0], lay.virtual_size[1],
             lay.virtual_origin[0], lay.virtual_origin[1]))
    for m in lay.monitors:
        print("[%d] %-14s 物理 %dx%d / 缩放 %d%% -> 逻辑 %dx%d"
              % (m.monitor_index, m.device or "?",
                 m.physical_width, m.physical_height, m.scale_percent,
                 int(round(m.physical_width / m.scale_x)),
                 int(round(m.physical_height / m.scale_y))))
    print()
    print("说明：物理 ≠ 逻辑 时若 GetSystemMetrics 报的是「逻辑」值，")
    print("     说明 DPI 感知未生效 —— 所有坐标会偏移。")
    return 0


def cmd_layout(a):
    lay = P.layout()
    if a.json:
        import json
        print(json.dumps(lay.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(lay.describe())
        print()
        print("混合缩放: %s" % ("是（点击必须按屏分别换算）" if lay.mixed_scaling else "否"))
    return 0


def cmd_windows(a):
    ws = W.list_windows(
        include_invisible=a.all,
        processes=[a.process] if a.process else None,
        title_re=a.title,
        min_size=a.min_size or 0,
    )
    if a.usable:
        ws = [w for w in ws if w.is_usable]

    if a.json:
        import json
        print(json.dumps([w.to_dict() for w in ws], ensure_ascii=False, indent=2))
        return 0

    _hr("窗口 %d 个" % len(ws))
    print("%-12s %-22s %-26s %s" % ("HWND", "进程", "标题", "矩形"))
    print("-" * 66)
    for w in (ws[: a.limit] if a.limit else ws):
        print("0x%08X  %-22s %-26s %dx%d @(%d,%d)%s"
              % (w.hwnd,
                 (w.process or "?")[:22],
                 (w.title or "(无标题)")[:26],
                 w.width, w.height, w.rect[0], w.rect[1],
                 "  [最小化]" if w.minimized else ("  [隐形]" if w.cloaked else "")))
    if a.limit and len(ws) > a.limit:
        print("... 还有 %d 个（用 --limit 调大）" % (len(ws) - a.limit))
    return 0


def cmd_geom(a):
    hwnd = _parse_hwnd(a)
    g = P.geometry(hwnd)
    if a.json:
        import json
        print(json.dumps(g.to_dict(), ensure_ascii=False, indent=2))
        return 0

    _hr("窗口几何 (全部为物理像素)")
    print("HWND        : 0x%08X" % g.hwnd)
    print("标题        : %s" % (g.title or "(无标题)"))
    print("类名        : %s" % g.class_name)
    print("进程        : %s (pid=%d)" % (g.process or "?", g.pid))
    print("带阴影矩形  : %dx%d @ (%d,%d)   <- GetWindowRect，比肉眼大一圈"
          % (g.rect[2], g.rect[3], g.rect[0], g.rect[1]))
    print("可见边框    : %dx%d @ (%d,%d)   <- DWM，肉眼看到的就是它"
          % (g.frame[2], g.frame[3], g.frame[0], g.frame[1]))
    print("客户区      : %dx%d @ (%d,%d)"
          % (g.client_size[0], g.client_size[1], g.client[0], g.client[1]))
    print("窗口中心    : %s" % (g.center,))
    print("DPI / 缩放  : %d / %d%%" % (g.dpi, int(round(g.scale * 100))))
    print("所在显示器  : [%d]" % g.monitor_index)
    print("状态        : visible=%s minimized=%s maximized=%s"
          % (g.visible, g.minimized, g.maximized))
    return 0


def cmd_cursor(a):
    x, y = P.cursor_position()
    nx, ny = P.normalize(x, y)
    lay = P.layout()
    m = lay.containing(x, y)
    print("物理坐标    : (%d, %d)" % (x, y))
    print("逻辑坐标    : %s" % (P.to_logical(x, y),))
    print("归一化坐标  : (%d, %d)   <- SendInput 绝对坐标用这个" % (nx, ny))
    print("所在显示器  : %s" % ("[%d] %d%%" % (m.monitor_index, m.scale_percent) if m else "屏幕外"))
    print("虚拟桌面    : origin=%s size=%s" % (lay.virtual_origin, lay.virtual_size))
    wi = W.window_at(x, y)
    print("所在窗口    : %s" % (wi if wi else "(桌面)"))
    return 0


def cmd_pixel(a):
    px = P.pixel_at(a.x, a.y)
    if px is None:
        print("读不到像素（坐标越界或 DC 失败）: (%d, %d)" % (a.x, a.y))
        return 1
    r, g, b = px
    print("(%d, %d) -> RGB(%d, %d, %d)  #%02X%02X%02X" % (a.x, a.y, r, g, b, r, g, b))
    print("灰度        : %d" % int(0.299 * r + 0.587 * g + 0.114 * b))
    return 0


def cmd_shot(a):
    from avatar.vision.capture import grab_screen, grab_window, is_blank, average_color

    t0 = time.time()
    if a.hwnd:
        bmp = grab_window(_parse_hwnd(a), client_only=a.client)
    elif a.region:
        rx, ry, rw, rh = (int(v) for v in a.region.split(","))
        bmp = grab_screen((rx, ry, rw, rh))
    else:
        bmp = grab_screen(None)
    dt = (time.time() - t0) * 1000

    if a.downsample > 1:
        bmp = bmp.downsample(a.downsample)

    out = a.out or ("shot_%s.bmp" % time.strftime("%H%M%S"))
    _write_bmp(out, bmp)
    print("已保存  : %s" % os.path.abspath(out))
    print("尺寸    : %dx%d  原点 %s" % (bmp.width, bmp.height, bmp.origin))
    print("耗时    : %.1f ms" % dt)
    print("均色    : %s" % (average_color(bmp),))
    if is_blank(bmp):
        print("⚠ 纯色图 —— 可能锁屏 / 显示器休眠 / 该窗口用 DirectComposition 抓不到")
    return 0


def cmd_pick(a):
    """坐标诊断：输入一个点，输出关于它的全部认知。"""
    x, y = int(a.x), int(a.y)
    lay = P.layout()
    m = lay.containing(x, y)
    _hr("点 (%d, %d) 的诊断" % (x, y))
    print("在虚拟桌面内    : %s" % ("是" if m else "否（屏幕外）"))
    if m:
        mx, my, mw, mh = m.rect
        print("所属显示器      : [%d] %s  缩放 %d%%"
              % (m.monitor_index, m.device or "?", m.scale_percent))
        print("屏内相对坐标    : (%d, %d)  (屏尺寸 %dx%d)"
              % (x - mx, y - my, m.physical_width, m.physical_height))
        print("换算逻辑坐标    : %s" % (P.to_logical(x, y),))
    print("归一化坐标      : %s" % (P.normalize(x, y),))
    px = P.pixel_at(x, y)
    print("该点像素        : %s" % (("RGB%s" % (px,)) if px else "读不到",))
    wi = W.window_at(x, y)
    if wi:
        print("该点窗口        : 0x%08X %s" % (wi.hwnd, wi.title or "(无标题)"))
        print("                : %s (pid=%d)" % (wi.process, wi.pid))
        try:
            g = P.geometry(wi.hwnd)
            cx, cy = g.screen_to_client(x, y)
            print("窗口客户区坐标  : (%d, %d)  <- 喂给窗口内相对坐标动作" % (cx, cy))
            print("窗口 DPI        : %d (%d%%)" % (g.dpi, int(round(g.scale * 100))))
        except AvatarError as e:
            print("窗口几何取不到  : %s" % e)
    else:
        print("该点窗口        : (桌面)")
    return 0


# --------------------------------------------------------------------------
# 纯 ctypes 写 BMP —— 不引入 Pillow
# --------------------------------------------------------------------------
def _write_bmp(path: str, bmp) -> None:
    """把 Bitmap 写成图片。**按扩展名决定格式**：``.png`` 走 PNG，
    其余走 24 位 BMP。

    ## 为什么加 PNG

    BMP 只有本项目的工具能读，`Read` 之类的图片查看器打不开 ——
    每次抓完图都要手动转一道，很烦。PNG 直接用
    :func:`avatar.vision.image_prep.encode_png`（项目自带的纯 Python
    编码器，零依赖），一行代码的事。

    ## BMP 为什么是 24 位

    BMP 是自下而上存储的，而我们的 Bitmap 已经是 DIB 内存序
    （也是自下而上），所以**逐行直接拷贝即可，不需要翻转**。
    每行按 4 字节对齐补零。
    """
    if str(path).lower().endswith(".png"):
        from avatar.vision.image_prep import encode_png
        with open(path, "wb") as f:
            f.write(encode_png(bmp))
        return
    w, h = bmp.width, bmp.height
    row_raw = w * 3
    pad = (4 - (row_raw % 4)) % 4
    row_out = row_raw + pad
    pixel_bytes = row_out * h
    file_size = 14 + 40 + pixel_bytes

    with open(path, "wb") as f:
        # BITMAPFILEHEADER
        f.write(b"BM")
        f.write(struct.pack("<IHHI", file_size, 0, 0, 54))
        # BITMAPINFOHEADER
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, pixel_bytes, 2835, 2835, 0, 0))
        # 像素数据：Bitmap 是 BGRA，BMP 要 BGR
        src = bmp.pixels
        stride = bmp.stride
        padz = b"\x00" * pad
        for y in range(h):
            base = y * stride
            line = bytearray(row_raw)
            for x in range(w):
                o = base + x * 4
                d = x * 3
                line[d] = src[o]
                line[d + 1] = src[o + 1]
                line[d + 2] = src[o + 2]
            f.write(bytes(line))
            if pad:
                f.write(padz)


def _parse_hwnd(a, *, fallback: bool = True) -> int:
    """解析窗口句柄。

    ## 兼容三种入参

    - argparse 的 Namespace（自动取 ``.hwnd``）
    - ``int``（原样返回）
    - 字符串（十进制或 ``0x`` 前缀十六进制）

    ## 为什么要兼容

    历史上有两份同名实现，一份收 Namespace、一份收裸值，
    后定义的那份**覆盖**了前一份 —— 结果所有传 Namespace 的
    调用点全部拿到 ``None``，``shot --hwnd`` 直接崩。
    合并成一份，两种用法都认，这类"静默覆盖"就不会再发生。

    ``fallback=True`` 且值为 None 时回退到前台窗口。
    """
    v = a.hwnd if hasattr(a, "hwnd") else a
    if isinstance(v, int):
        return v
    if v is None:
        if not fallback:
            return 0
        h = P.foreground_hwnd()
        if not h:
            raise SystemExit("没有前台窗口，且未指定 --hwnd")
        return h
    s = str(v).strip()
    if not s:
        # ⚠ 空串**不**回退前台 —— 那是"用户给了 --hwnd 但值是空的"，
        #   属于输入错误。回退前台会把笔误变成"悄悄抓了别的窗口"，
        #   比报错危险得多。没给参数（None）才走前台回退。
        raise SystemExit("无法解析 hwnd: %r（空值；省略 --hwnd 才会用前台窗口）"
                         % v)
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        raise SystemExit("无法解析 hwnd: %r（用十进制或 0x 前缀十六进制）" % v)


# ==========================================================================
# 阶段三：输入层命令
# ==========================================================================
def cmd_move(a):
    """移动光标到屏幕绝对坐标。"""
    from avatar.input import mouse as M
    from avatar.human_entropy import entropy, mouse as HM
    from avatar.human_entropy import metrics as MT
    from avatar.human_entropy import submove as SM

    if a.seed is not None:
        entropy.seed(a.seed)

    before = M.get_cursor_pos()
    start = (a.from_x, a.from_y) if a.from_x is not None else before

    # ---- 试运行：只规划轨迹、算指标，不碰真实光标 -----------------------
    if a.dry_run:
        plan = SM.plan_movement(
            start[0], start[1], a.x, a.y,
            human=True, precision=a.precision,
        )
        m = MT.analyze_move(plan)
        v = MT.verdict(m)
        if a.json:
            import json as _j
            print(_j.dumps(
                {"requested": [a.x, a.y], "from": list(start),
                 "plan": plan.describe(), "metrics": m.describe(),
                 "verdict": v["ok"], "failures": v["failures"]},
                ensure_ascii=False, indent=2))
            return 0
        _hr("轨迹规划（试运行，未移动光标）")
        print("起点      : (%d, %d)" % tuple(start))
        print("目标      : (%d, %d)" % (a.x, a.y))
        print("距离      : %.1f px" % plan.distance)
        print("采样点数  : %d" % len(plan.segments))
        print("子运动段  : %d" % len(plan.submovements))
        print("规划时长  : %.3f s" % plan.total_duration)
        _hr("真人化指标")
        print("速度峰值  : %d  %s" % (
            m.peak_count, "[OK] ≥2（像人）" if m.peak_count >= 2 else "[FAIL] 只有 1 个（像机器）"))
        print("路径直度  : %.4f  %s" % (
            m.straightness, "[OK] ≠1（像人）" if m.straightness < 1.0 else "[FAIL] 完全直线"))
        print("不对称度  : %.3f  %s" % (
            m.asymmetry, "[OK] <1（加速短）" if m.asymmetry < 0.98 else "[FAIL] 对称"))
        print("震颤能量  : %.4f" % m.tremor_energy)
        print("峰值速度  : %.0f px/s" % m.max_speed)
        print()
        print("判定      : %s" % ("[OK] 通过" if v["ok"] else "[FAIL] %s" % v["failures"]))
        return 0

    if a.human:
        res = HM.move_human(
            a.x, a.y,
            human=True,
            precision=a.precision,
            from_pos=start,
        )
        actual = M.get_cursor_pos()
    else:
        M.move_absolute(a.x, a.y)
        res = None
        actual = M.get_cursor_pos()

    if a.json:
        import json as _j
        out = {"requested": [a.x, a.y], "before": list(before), "actual": list(actual)}
        if res is not None:
            out["move"] = res.describe()
        print(_j.dumps(out, ensure_ascii=False, indent=2))
        return 0

    _hr("移动%s" % ("（真人化）" if a.human else "（机械）"))
    print("目标坐标  : (%d, %d)" % (a.x, a.y))
    print("起始坐标  : (%d, %d)" % before)
    print("实际落点  : (%d, %d)" % actual)
    err = ((actual[0] - a.x) ** 2 + (actual[1] - a.y) ** 2) ** 0.5
    print("落点误差  : %.2f px  %s" % (err, "[OK]" if err <= 2.0 else "[FAIL] 超 2px"))

    if res is not None:
        _hr("真人化指标")
        print("距离      : %.0f px" % res.distance)
        print("时长      : %.3f s" % res.duration)
        print("采样点数  : %d" % res.points)
        print("子运动段  : %d" % res.submovements)
        print("速度峰值  : %d  %s" % (res.peak_count,
                                      "[OK] ≥2（像人）" if res.peak_count >= 2 else "[FAIL] 只有 1 个（像机器）"))
        print("路径直度  : %.4f  %s" % (res.straightness,
                                        "[OK] ≠1（像人）" if res.straightness < 1.0 else "[FAIL] 完全直线"))
        print()
        print("说明：峰值数 ≥2、直度 <1、有子运动段，是真人轨迹的三个特征。")
        print("      想看对比：把 --human 去掉再跑一次。")
    return 0


def cmd_type(a):
    """输入文本。"""
    from avatar.input import keyboard as K
    from avatar.human_entropy import entropy, keyboard as HK

    if a.seed is not None:
        entropy.seed(a.seed)

    text = a.text
    if a.file:
        with open(a.file, "r", encoding="utf-8") as f:
            text = f.read()

    if text is None:
        raise SystemExit("需要 --text 或 --file")

    if a.dry_run:
        est = HK.estimate_duration(text, wpm=a.wpm, typo_rate=a.typo_rate)
        if a.json:
            import json as _j
            print(_j.dumps({"chars": len(text), "estimated_seconds": round(est, 3),
                            "wpm": a.wpm, "human": a.human}, ensure_ascii=False, indent=2))
            return 0
        _hr("打字预估（未真的输入）")
        print("字符数    : %d" % len(text))
        print("目标速度  : %.0f WPM" % a.wpm)
        print("预计时长  : %.2f s" % est)
        print("真人化    : %s" % ("是" if a.human else "否"))
        return 0

    if a.human:
        r = HK.type_text_human(
            text, wpm=a.wpm, skill=a.skill,
            typo_rate=a.typo_rate, correct_typos=not a.no_correct,
            pause_at_end=not a.no_pause,
        )
        stats = r.describe()
    else:
        t0 = time.monotonic()
        n = K.type_text(text)
        stats = {"chars": n, "duration": round(time.monotonic() - t0, 3)}

    if a.json:
        import json as _j
        print(_j.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    _hr("输入%s" % ("（真人化）" if a.human else "（机械）"))
    print("字符数    : %s" % stats.get("chars"))
    print("时长      : %.2f s" % stats.get("duration", 0))
    if a.human:
        print("实际 WPM  : %.0f" % stats.get("wpm", 0))
        print("按键次数  : %s" % stats.get("keystrokes"))
        print("错字/回改 : %s / %s" % (stats.get("typos"), stats.get("corrections")))
        print("停顿次数  : %s" % stats.get("pauses"))
    print()
    print("⚠️ 输入是发给**前台窗口**的。若目标窗口不在前台，")
    print("   先用 `avatar windows` 找到它，再用系统手段激活。")
    return 0


def cmd_mode(a):
    """查看/切换交互模式。"""
    from avatar.input import dispatcher as DP

    if a.set:
        old = DP.set_interaction_mode(
            a.set,
            allow_fallback=None if a.fallback is None else a.fallback,
        )
        changed = old != a.set
    else:
        old = changed = None

    info = DP.describe_modes()

    if a.json:
        import json as _j
        print(_j.dumps({"before": old, "changed": changed, **info},
                       ensure_ascii=False, indent=2))
        return 0

    _hr("交互模式")
    if changed:
        print("已切换    : %s -> %s" % (old, info["mode"]))
    print("当前模式  : %s" % info["mode"])
    print("允许降级  : %s" % ("是" if info["allow_fallback"] else "否"))
    _hr("两种模式的差异")
    print("silent    静默：PostMessage 直接进目标消息队列")
    print("          · 不动真实光标、不抢焦点，可在后台跑不影响用户")
    print("          · 只对 Win32 传统控件可靠")
    print("          · 不支持：Electron/自绘 UI、修饰键组合、拖拽")
    print("          · 写入后必须回读校验（不处理 WM_CHAR 的控件会静默忽略）")
    print()
    print("physical  物理：SendInput 走系统输入队列")
    print("          · 兼容性最好，任何程序都认；支持真人化轨迹")
    print("          · 会动真实光标、抢焦点")
    print("          · 受 UIPI 限制：目标提权时事件被静默丢弃")
    print()
    print("默认是 silent，失败自动降级到 physical（--fallback 控制）。")
    return 0


def cmd_entropy(a):
    """真人熵演示与可复现性验证。"""
    from avatar.human_entropy import entropy, kinetics, metrics, submove, tremor
    from avatar.input import mouse as M

    _hr("熵源状态")
    if a.seed is not None:
        entropy.seed(a.seed)
    print("当前种子  : %s" % (entropy.get_seed() if entropy.get_seed() is not None else "None（真随机）"))
    print("可复现    : %s" % ("是" if entropy.is_reproducible() else "否"))

    _hr("可复现性验证（同 seed 两次生成同一轨迹）")
    seed = a.seed if a.seed is not None else 42
    entropy.seed(seed)
    p1 = submove.plan_movement(100, 100, a.to_x, a.to_y, human=True, precision=1.2)
    s1 = [(round(s.x, 6), round(s.y, 6), round(s.delay, 6)) for s in p1.segments]
    entropy.seed(seed)
    p2 = submove.plan_movement(100, 100, a.to_x, a.to_y, human=True, precision=1.2)
    s2 = [(round(s.x, 6), round(s.y, 6), round(s.delay, 6)) for s in p2.segments]
    same = s1 == s2
    print("种子      : %d" % seed)
    print("采样点    : %d" % len(s1))
    print("两次一致  : %s  %s" % (same, "[OK]" if same else "[FAIL] 违反可复现性！"))

    entropy.seed(seed + 1)
    p3 = submove.plan_movement(100, 100, a.to_x, a.to_y, human=True, precision=1.2)
    s3 = [(round(s.x, 6), round(s.y, 6), round(s.delay, 6)) for s in p3.segments]
    print("换种子变  : %s  %s" % (s1 != s3, "[OK]" if s1 != s3 else "[FAIL] 熵源没起作用"))

    _hr("真人轨迹 vs 机械轨迹")
    entropy.seed(seed)
    plan_h = submove.plan_movement(100, 100, a.to_x, a.to_y, human=True, precision=1.2)
    plan_r = submove.plan_movement(100, 100, a.to_x, a.to_y, human=False)
    mh = metrics.analyze_move(plan_h)
    mr = metrics.analyze_move(plan_r)

    print("%-18s %-14s %-14s %s" % ("指标", "真人化", "机械", "判据"))
    print("-" * 66)
    print("%-18s %-14s %-14s %s" % ("速度峰值数", mh.peak_count, mr.peak_count, "真人 ≥2"))
    print("%-18s %-14s %-14s %s" % ("路径直度", round(mh.straightness, 4),
                                    round(mr.straightness, 4), "真人 <1"))
    print("%-18s %-14s %-14s %s" % ("速度不对称度", round(mh.asymmetry, 3),
                                    round(mr.asymmetry, 3), "真人 <1（加速短）"))
    print("%-18s %-14s %-14s %s" % ("子运动段数", mh.submovement_count,
                                    mr.submovement_count, "真人 2-3"))
    print("%-18s %-14s %-14s %s" % ("停顿次数", mh.pause_count, mr.pause_count, "真人 ≥1"))
    print("%-18s %-14s %-14s %s" % ("震颤能量", round(mh.tremor_energy, 4),
                                    round(mr.tremor_energy, 4), "8-12Hz 占比"))
    print("%-18s %-14s %-14s %s" % ("时长(s)", round(mh.duration, 3),
                                    round(mr.duration, 3), "真人更慢"))

    v = metrics.verdict(mh)
    print()
    print("真人化判定: %s" % ("[OK] 通过" if v["ok"] else "[FAIL] 失败: %s" % v["failures"]))
    for k, ok in v["checks"].items():
        print("  %s %s" % ("[OK]" if ok else "[FAIL]", k))

    _hr("四个独立机制")
    print("1. entropy.py   熵源统一管理（本文件）—— 种子、可复现开关")
    print("2. kinetics.py  非对称速度曲线 —— 快加速、慢减速")
    print("3. tremor.py    8-12Hz 低频相关噪声 —— 不是白噪声")
    print("4. submove.py   多段子运动 —— 冲刺 -> 修正 -> 微调")
    print()
    print("每个机制都可以单独关闭看效果：")
    print("  · 关分段：submove.plan_movement(..., human=False)")
    print("  · 关震颤：tremor.make_tremor(enabled=False)")
    print("  · 关真人化：move_human(..., human=False)")

    if a.demo:
        _hr("实机演示（真的移动光标，2 秒后归位）")
        try:
            from_x, from_y = M.get_cursor_pos()
            entropy.seed(seed)
            res = __import__("avatar.human_entropy.mouse", fromlist=["x"]).move_human(
                a.to_x, a.to_y)
            time.sleep(0.4)
            actual = M.get_cursor_pos()
            print("从 (%d,%d) 到 (%d,%d)" % (from_x, from_y, a.to_x, a.to_y))
            print("实际光标  : %s" % (actual,))
            print("峰值 %d | 子运动 %d | 直度 %.4f | 耗时 %.3fs"
                  % (res.peak_count, res.submovements, res.straightness, res.duration))
            time.sleep(1.0)
            M.move_absolute(from_x, from_y)
            print("已归位到 (%d,%d)" % (from_x, from_y))
        except Exception as exc:      # noqa: BLE001
            print("演示失败（可能没有可用的桌面会话）：%s" % exc)

    if entropy.get_seed() is None:
        entropy.seed(None)
    return 0


# ==========================================================================
# 阶段四：Web CDP + 执行结果验证
# ==========================================================================
#
# 设计取舍说明（为什么这几个命令长这样）：
#
# 1. ``web open`` 默认**复用已有调试实例**。很多用户的浏览器是常年开着的，
#    每次都新起一个会：a) 慢，b) 把用户的会话丢掉，c) 起一堆僵尸进程。
#    想强制新实例要显式 ``--new``。
# 2. ``web`` 子命令之间**不共享会话** —— 每条命令独立起会话、独立收尾。
#    因为 CLI 是一次性进程。要连续操作请用 ``web eval`` 跑一段脚本，
#    或者用 Python API。
# 3. ``verify`` 命令**没有默认判据**。必须显式给 ``--target`` 之类，
#    因为"验证什么"是调用方才知道的事。给个默认判据只会造噪音。


def cmd_web_opts(a):
    """诊断：列出可用的浏览器、探测已有调试端口。

    单列一个命令是因为"为什么连不上"是最高频的问题。
    把所有相关事实一次打全 —— 找到了几个浏览器、端口开没开、
    开的那个是不是 CDP —— 省得反复试。
    """
    from avatar.web_cdp import launcher as LB
    from avatar.web_cdp import electron as EL

    chrome = LB.find_chrome()
    edge = LB.find_edge()
    found = {"chrome": chrome, "edge": edge}
    instances = []
    try:
        instances = LB.list_debug_instances()
    except Exception as exc:      # noqa: BLE001
        instances = []
        _fmt_err = "%s: %s" % (type(exc).__name__, exc)

    if a.json:
        import json as _j
        print(_j.dumps({
            "browsers": found,
            "instances": [
                {"port": i.port, "product": getattr(i, "product", ""),
                 "ws": getattr(i, "websocket_url", "")}
                for i in instances],
            "known_ports": list(LB.DEBUG_PORT_RANGE),
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    _hr("浏览器")
    for kind, path in found.items():
        print("  %-8s %s" % (kind, path or "(未找到)"))

    _hr("调试端口")
    if instances:
        for i in instances:
            print("  端口 %-6s %s" % (i.port, getattr(i, "product", "") or ""))
            ws = getattr(i, "websocket_url", "")
            if ws:
                print("           %s" % ws)
    else:
        print("  %s 都没有 CDP 实例"
              % "/".join(str(p) for p in LB.DEBUG_PORT_RANGE))
        print("  提示：先跑  avatar web-open --headless --url https://example.com")

    _hr("本机 Electron 窗口")
    wins = EL.find_electron_windows()
    if wins:
        for w in wins[:10]:
            cdp = "CDP✓" if w.get("has_cdp") else "CDP✗"
            print("  [%s] hwnd=%-10s %-20s %s"
                  % (cdp, w.get("hwnd"), (w.get("process") or "")[:20],
                     (w.get("title") or "")[:44]))
    else:
        print("  没检测到运行中的 Electron 应用")
    return 0


def _connect_web(port, *, timeout=15.0):
    """按端口建 CDP 会话，返回 ``(browser_session, page_session)``。

    ## 为什么需要这个 helper

    ``open_session(url)`` 吃的是 **WebSocket URL**
    （``ws://127.0.0.1:9222/devtools/browser/<id>``），**不是端口号**。
    而 ``/json/version`` 给的 ``webSocketDebuggerUrl`` 是**浏览器级**端点 ——
    浏览器级会话不能直接 ``Runtime.evaluate``（那是页面级方法），
    必须先 ``attach_page`` 拿到页面级 sessionId。

    所以从端口到"能用的页面会话"要走三步：探测 → 连浏览器级 → attach 页面。
    三条 web 命令都要做，抽出来保证行为一致 —— 尤其
    **``attach_page`` 会自动 ``Page.enable + Runtime.enable + DOM.enable``**，
    漏任何一条都会表现为"命令报成功但什么都读不到"。

    ⚠ ``/json/version`` 返回的 ``webSocketDebuggerUrl`` 里 host 有时是
    ``localhost``。新版 Chrome/Edge 对**非 IP 字面量**的 Host 头做校验，
    用 ``localhost`` 连会被拒（"Host header is specified and is not an IP
    address or localhost" 的变体）。这里统一改写成 ``127.0.0.1``。

    返回的 ``page`` 是一个 ``CdpSession``，通过 ``browser_session`` 与
    ``page_session_id`` 两个属性挂回浏览器级连接 —— 这样上层既能用页面级
    方法（``Runtime.evaluate``），也能用浏览器级方法（``Target.getTargets``），
    而底层只有一条 WebSocket。
    """
    from avatar.web_cdp import launcher as LB
    from avatar.web_cdp import session as SS

    info = LB.probe_debug_port(port, timeout=timeout)
    if not info:
        raise RuntimeError("端口 %d 上没有 CDP 实例" % port)

    ws = info.get("webSocketDebuggerUrl") or ""
    if not ws:
        raise RuntimeError(
            "端口 %d 的 /json/version 没有 webSocketDebuggerUrl" % port)
    ws = ws.replace("localhost", "127.0.0.1")

    browser = SS.open_session(ws, timeout=timeout)

    try:
        targets = browser.list_targets(filter_type="page")
        if not targets:
            return browser, browser
        sid = browser.attach_page(targets[0]["targetId"])
    except Exception:      # noqa: BLE001
        return browser, browser

    # 造一个"看着像页面级"的会话对象 —— 但**不新开 socket**。
    #
    # 为什么这样写：手搓的 CdpSession 之上只有一条 WebSocket，
    # 页面级方法靠 ``sessionId`` 字段区分。所以最省的做法是让
    # 页面级对象**借用**浏览器级对象的那条连接，只换个默认 sessionId。
    # 两条 socket 会出现事件分流的麻烦（页面事件进 browser 的队列，
    # browser 方法又不在 page 上），一致性问题比省的那点代码麻烦得多。
    page = _PageFacade(browser, sid)
    return page, browser


class _PageFacade(object):
    """把 ``(browser_session, session_id)`` 包成一个"页面级会话"。

    只转发 ``CdpSession`` 上与页面操作相关的成员，其它属性原样透传。
    用组合而不是继承，是因为继承会让人以为它有独立的连接 ——
    它没有，它和 browser 共用同一条 WebSocket。
    """

    __slots__ = ("browser_session", "session_id", "url")

    def __init__(self, browser_session, session_id):
        self.browser_session = browser_session
        self.session_id = session_id
        self.url = getattr(browser_session, "url", "")

    # -- 核心：所有调用都带上本页的 sessionId --
    #
    # ⚠ 必须用"是 None 就替换"而不是 ``setdefault``。
    #   ``CdpSession.evaluate`` 内部会显式传 ``session_id=None``，
    #   ``setdefault`` 见到 key 已存在就不动 → sessionId 丢了 →
    #   CDP 报 ``'Runtime.evaluate' wasn't found``（浏览器级没有这个方法）。
    #   这个坑很隐蔽：错误信息完全不提 sessionId。
    def _sid(self, kw):
        if kw.get("session_id") is None:
            kw["session_id"] = self.session_id
        return kw

    def call(self, method, params=None, **kw):
        self._sid(kw)
        if params is None:
            return self.browser_session.call(method, **kw)
        return self.browser_session.call(method, params, **kw)

    # 这些方法本来就是页面级语义，转发时补 sessionId
    def evaluate(self, expression, **kw):
        self._sid(kw)
        return self.browser_session.evaluate(expression, **kw)

    def enable(self, *domains, **kw):
        self._sid(kw)
        return self.browser_session.enable(*domains, **kw)

    def subscribe(self, method, **kw):
        self._sid(kw)
        return self.browser_session.subscribe(method, **kw)

    # -- 其它一律透传（connected / close / describe / ...）--
    def __getattr__(self, name):
        return getattr(self.browser_session, name)

    def close(self):
        """只 detach 本页，**不关底层连接**。"""
        try:
            self.browser_session.call(
                "Target.detachFromTarget", {"sessionId": self.session_id})
        except Exception:      # noqa: BLE001
            pass

    def describe(self):
        d = dict(self.browser_session.describe())
        d["page_session_id"] = self.session_id
        d["facade"] = True
        return d

    def __repr__(self):      # pragma: no cover
        return "<PageFacade sid=%s>" % (self.session_id[:8] if self.session_id
                                        else "")


def _open_web(args):
    """起/复用调试浏览器，返回 ``(page_session, browser_session, proc)``。

    三条路径按优先级：
    1. ``--port`` 指定了 → 直接连
    2. 没指定 → 扫常用端口，找到就复用
    3. 都没有 → 起新实例（``--headless`` 控制有头无头）
    """
    from avatar.web_cdp import launcher as LB

    explicit = getattr(args, "port", None)
    profile = getattr(args, "profile", None)
    timeout = float(getattr(args, "timeout", 10.0) or 10.0)

    if explicit:
        page, browser = _connect_web(explicit, timeout=timeout)
        return page, browser, None

    insts = []
    try:
        insts = LB.list_debug_instances()
    except Exception:      # noqa: BLE001
        insts = []
    if insts:
        page, browser = _connect_web(insts[0].port, timeout=timeout)
        return page, browser, None

    # 起新的。用 pick_free_port + launch_browser（而不是 ensure_debug_browser）
    # 是为了**拿到 Popen**，这样 --close-browser 才能真的收尾。
    #
    # ⚠ 参数名是 ``user_data_dir`` 不是 ``profile``，返回是
    #   ``(Popen, BrowserInfo)`` 元组不是 ``(port, Popen)``。
    #   launch_browser 已经内部 wait_ready 了，不用再自己等端口。
    # ⚠ ``find_browser()`` 默认 ``prefer="chrome"`` → **只**找 Chrome，
    #   本机没装 Chrome 就会返回 None。要退到 Edge 必须显式传 "any"。
    exe = getattr(args, "exe", None) or LB.find_browser("any")
    if not exe:
        from avatar.errors import AvatarError as _AE

        class _NoBrowser(_AE):
            exit_code = 12

        raise _NoBrowser(
            "没找到可用的浏览器。装一个 Chrome/Edge，或用 --exe 指定路径。")
    headless = getattr(args, "headless", False)
    port = LB.pick_free_port()
    proc, info = LB.launch_browser(
        exe, port=port, headless=headless, url="about:blank",
        user_data_dir=profile, wait_ready=True,
        ready_timeout=max(timeout, 20.0))
    page, browser = _connect_web(info.port, timeout=timeout)
    return page, browser, proc


def cmd_web_open(a):
    """打开页面（或复用已有调试实例）。"""
    _hr("Web 会话")
    try:
        sess, browser, proc = _open_web(a)
    except Exception as exc:      # noqa: BLE001
        print("[FAIL] 建立会话失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 12

    try:
        url = a.url or "about:blank"
        if url != "about:blank":
            sess.call("Page.enable")
            sess.call("Page.navigate", {"url": url})

        pages = []
        try:
            pages = browser.list_targets()
        except Exception:      # noqa: BLE001
            pass

        if a.json:
            import json as _j
            print(_j.dumps({
                "url": url,
                "page_count": len(pages),
                "pages": [p.to_dict() if hasattr(p, "to_dict") else str(p)
                          for p in pages],
            }, ensure_ascii=False, indent=2, default=str))
            return 0

        print("会话已建立")
        print("已导航到  : %s" % url)
        print("页面数    : %d" % len(pages))
        for i, pg in enumerate(pages[:10]):
            title = getattr(pg, "title", "") or ""
            purl = getattr(pg, "url", "") or ""
            print("  [%d] %s" % (i, (title or "(无标题)")[:50]))
            if purl:
                print("      %s" % purl[:90])
        if a.title:
            v = None
            try:
                from avatar.verify.web import verify_title
                v = verify_title(sess, a.title, timeout=a.timeout)
            except Exception as exc:      # noqa: BLE001
                print("验证失败：%s" % exc)
            if v is not None:
                print("标题验证  : %s" % v.describe())
        return 0
    finally:
        _quiet_close(sess)
        if proc is not None and getattr(a, "close_browser", False):
            try:
                proc.terminate()
            except Exception:      # noqa: BLE001
                pass


def cmd_web_click(a):
    """按选择器点击元素。"""
    from avatar.web_cdp import interact as I
    from avatar.verify.web import verify_dom, verify_element_visible

    _hr("Web 点击")
    try:
        sess, browser, proc = _open_web(a)
    except Exception as exc:      # noqa: BLE001
        print("[FAIL] 建立会话失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 12

    try:
        # 点之前先确认元素在 —— 不然报错信息会很难懂
        pre = verify_element_visible(sess, a.selector, timeout=a.timeout)
        if pre.unknown:
            print("⚠ 元素可见性未知：%s" % pre.reason)
        elif pre.failed:
            print("[FAIL] 元素不可见或不存在：%s" % pre.reason, file=sys.stderr)
            return 4

        res = I.click(sess, a.selector, button=a.button, clicks=a.clicks)

        vres = None
        if a.expect_text is not None:
            vres = verify_dom(sess, a.expect_selector or a.selector,
                              expect_text=a.expect_text,
                              has_expect_text=True, timeout=a.timeout)
        elif a.verify:
            vres = verify_element_visible(sess, a.verify, timeout=a.timeout)

        if a.json:
            import json as _j
            out = {"click": res.to_dict() if hasattr(res, "to_dict") else dict(res)}
            if vres is not None:
                out["verify"] = vres.to_dict()
            print(_j.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0

        print("点击已发出: %s" % (res.describe() if hasattr(res, "describe")
                                else res))
        if vres is not None:
            print("验证      : %s" % vres.describe())
            if vres.failed:
                return 5
        return 0
    except Exception as exc:      # noqa: BLE001
        print("[FAIL] 点击失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 4
    finally:
        _quiet_close(sess)


def cmd_web_eval(a):
    """在页面里执行 JS。"""
    from avatar.verify.web import evaluate_value

    _hr("Web eval")
    script = a.js
    if not script and a.file:
        with open(a.file, "r", encoding="utf-8") as f:
            script = f.read()
    if not script:
        print("[FAIL] 需要 --js 或 --file", file=sys.stderr)
        return 2

    try:
        sess, browser, proc = _open_web(a)
    except Exception as exc:      # noqa: BLE001
        print("[FAIL] 建立会话失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 12

    try:
        got, value, err = evaluate_value(sess, script, timeout=a.timeout)

        if a.json:
            import json as _j
            print(_j.dumps({"ok": got, "value": value, "error": err},
                           ensure_ascii=False, indent=2, default=str))
            return 0 if got else 10

        if not got:
            print("[FAIL] 求值失败：%s" % err, file=sys.stderr)
            return 10
        if isinstance(value, (dict, list)):
            import json as _j
            print(_j.dumps(value, ensure_ascii=False, indent=2, default=str))
        else:
            print(value)
        return 0
    finally:
        _quiet_close(sess)


def cmd_electron_attach(a):
    """把 Electron 应用接到 CDP（需要它带着 --remote-debugging-port 起的）。"""
    from avatar.web_cdp import electron as EL
    from avatar.web_cdp import session as SS

    _hr("Electron 附加")

    target = None
    if a.hwnd:
        hwnd = _parse_hwnd(a.hwnd)
        target = {"hwnd": hwnd}
    elif a.process:
        wins = EL.find_electron_windows(process_names=[a.process])
        if not wins:
            print("[FAIL] 没找到进程 %s 的窗口" % a.process, file=sys.stderr)
            print("  提示：跑 avatar web opts 看有哪些 Electron 窗口")
            return 4
        target = wins[0]
        print("找到窗口  : hwnd=%s %s" % (target.get("hwnd"),
                                          target.get("title") or ""))
    else:
        wins = EL.find_electron_windows()
        if not wins:
            print("[FAIL] 没检测到 Electron 窗口", file=sys.stderr)
            return 4
        target = wins[0]
        print("自动选中  : hwnd=%s %s" % (target.get("hwnd"),
                                          target.get("title") or ""))

    try:
        sess = EL.attach_electron(target.get("hwnd"),
                                  pid=target.get("pid"),
                                  process_name=target.get("process"))
    except Exception as exc:      # noqa: BLE001
        print("[FAIL] 附加失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 12

    if sess is None:
        print("[FAIL] 目标没开调试端口，无法用 CDP 附加")
        print("  原因：Electron 应用默认不开 --remote-debugging-port")
        print("  办法一：启动时加参数")
        print("      YourApp.exe --remote-debugging-port=9222")
        print("  办法二：设置环境变量后重启该应用")
        print("      set ELECTRON_ENABLE_LOGGING=1")
        print("  办法三：**降级到 UIA 路径**（阶段二的能力，同样可用）")
        print("      avatar windows --process %s" % (a.process or "YourApp"))
        return 12

    try:
        pages = []
        try:
            pages = sess.browser_session.list_targets()
        except Exception:      # noqa: BLE001
            pass

        if a.json:
            import json as _j
            print(_j.dumps({
                "attached": True,
                "window": target,
                "page_count": len(pages),
            }, ensure_ascii=False, indent=2, default=str))
            return 0

        print("[OK] 已通过 CDP 附加")
        print("窗口      : hwnd=%s" % target.get("hwnd"))
        print("页面数    : %d" % len(pages))
        if a.eval:
            from avatar.verify.web import evaluate_value
            got, value, err = evaluate_value(sess, a.eval, timeout=a.timeout)
            print("求值      : %s" % (value if got else "失败 %s" % err))
        return 0
    finally:
        _quiet_close(sess)


def cmd_verify(a):
    """统一的验证命令：桌面 / Web 都走这里。"""
    from avatar.verify.action import verify_action
    from avatar.verify.result import VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN

    # ⚠ ``--json`` 是给程序消费的，标题栏/分隔线混进 stdout 会让
    #   ``json.loads(stdout)`` 直接炸。所以机器可读模式下不打印任何装饰。
    if not a.json:
        _hr("执行结果验证")

    kw = {"mode": a.mode, "timeout": a.timeout}
    if a.settle is not None:
        kw["settle"] = a.settle

    # --- Web 判据 ---
    if a.selector:
        kw["selector"] = a.selector
    if a.url_contains:
        kw["url_contains"] = a.url_contains
    if a.expect_text is not None:
        kw["expect_text"] = a.expect_text
        kw["has_expect_text"] = True
    if a.expect_value is not None:
        kw["expect_value"] = a.expect_value
        kw["has_expect_value"] = True

    # --- 桌面判据 ---
    # ⚠ 顺序有讲究：``target`` 与 ``uia_target`` 是两个不同的键。
    #   ``verify_uia_exists`` 收 ``target``；``verify_uia_value`` 收
    #   ``uia_target``。给错键的表现是"没给判据"，而不是报错 ——
    #   所以这里两个都填，让 verify_action 路由能挑到合适的那个。
    if a.hwnd:
        kw["hwnd"] = _parse_hwnd(a.hwnd)
        kw["target"] = kw["hwnd"]
    elif a.target:
        kw["target"] = a.target
        kw["uia_target"] = a.target
    if a.expect_title:
        kw["verify_window_title"] = a.expect_title
        # 标题判据必须配一个 hwnd，否则没法读标题
        if not a.hwnd and a.target:
            kw.setdefault("hwnd", None)
    if a.expect_exists:
        kw["expect_exists"] = True
    # 收紧旋钮 —— 必须透传，否则 verify_uia_exists 只能吃默认值，
    # 一旦目标给的是字符串就走全遍历（52.8 秒级）。
    if getattr(a, "name_exact", False):
        kw["name_exact"] = True
    if getattr(a, "control_type", None):
        kw["control_type"] = a.control_type

    sess = None
    if a.mode == "web" or (a.mode == "auto" and (a.selector or a.url_contains)):
        try:
            sess, browser, proc = _open_web(a)
            kw["session"] = sess
        except Exception as exc:      # noqa: BLE001
            print("[FAIL] 建立 Web 会话失败：%s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
            return 12

    # 只给标题判据、没给窗口 → 用前台窗口。
    # 这是最自然的默认（"我刚让它干活的窗口对不对"），
    # 也避免为了验个标题先让人去查 hwnd。
    if a.expect_title and not a.hwnd and not a.target:
        try:
            from avatar.perception import foreground_hwnd

            fg = foreground_hwnd()
            if fg:
                kw["hwnd"] = fg
                kw["target"] = fg
                kw.setdefault("verify_window_title", a.expect_title)
        except Exception:      # noqa: BLE001
            pass

    try:
        # 什么都没给 → 明确告诉调用方，而不是给个假结论。
        # 判据键要看**构造出来的 kw**，不能只看原始参数名 ——
        # ``--expect-title`` 会变成 ``verify_window_title``，
        # 按参数名检查会漏判（踩过：命令直接报"没给判据"）。
        judge_keys = ("selector", "url_contains", "expect_text",
                      "expect_value", "verify_window_title", "expect_exists")
        if not any(k in kw for k in judge_keys):
            print("[FAIL] 没给任何验证判据。至少要给一样：", file=sys.stderr)
            for k in judge_keys:
                print("    --%s" % k.replace("_", "-"), file=sys.stderr)
            return 2

        res = verify_action(**kw)

        if a.json:
            import json as _j
            print(_j.dumps(res.to_dict(), ensure_ascii=False, indent=2,
                           default=str))
        else:
            print("结论      : %s" % res.verdict)
            print("手段      : %s" % (res.method or "(未走到底)"))
            print("原因      : %s" % (res.reason or "(无)"))
            print("耗时      : %.3fs" % res.elapsed)
            if res.evidence:
                print("证据      :")
                for k, v in list(res.evidence.items())[:12]:
                    print("    %-16s %s" % (k, str(v)[:70]))
            print()
            print(res.describe())

        # 退出码：ok=0 / fail=5 / unknown=11（三态必须区分，
        # 不然脚本里没法用 —— 这正是本机制存在的意义）
        if res.verdict == VERIFY_OK:
            return 0
        if res.verdict == VERIFY_FAIL:
            return 5
        return 11
    finally:
        if sess is not None:
            _quiet_close(sess)


def cmd_electron_windows(a):
    """列出运行中的 Electron 窗口，标出哪些能走 CDP。

    ``--cdp-only`` 是给"我要用 CDP 控制它"这个场景的：直接把
    不可用的筛掉，省得看完一堆又发现一个都用不了。
    """
    from avatar.web_cdp import electron as EL

    wins = EL.find_electron_windows(process_names=a.process or None,
                                    with_cdp_only=a.cdp_only)

    if a.json:
        import json as _j
        print(_j.dumps({"count": len(wins), "windows": wins},
                       ensure_ascii=False, indent=2, default=str))
        return 0

    _hr("Electron 窗口")
    if not wins:
        print("没有匹配的窗口")
        if a.cdp_only:
            print()
            print("注意：--cdp-only 会过滤掉**所有**没开调试端口的应用。")
            print("      而 Electron 应用默认不开 —— 所以空的概率很高。")
            print("      去掉 --cdp-only 看全部，或启动应用时加")
            print("      --remote-debugging-port=9222")
        return 0

    for w in wins:
        cdp = "CDP✓" if w.get("has_cdp") else "CDP✗"
        print("  [%s] hwnd=%-10s %-22s %s"
              % (cdp, w.get("hwnd"), (w.get("process") or "")[:22],
                 (w.get("title") or "")[:50]))
        if w.get("port"):
            print("        调试端口 %s" % w["port"])
    print()
    print("共 %d 个（CDP[OK] 的可用 web 通道控制，其余走 UIA）" % len(wins))
    return 0


def _quiet_close(sess) -> None:
    """静默关会话 —— 收尾失败不该覆盖主流程的结果。"""
    if sess is None:
        return
    try:
        sess.close()
    except Exception:      # noqa: BLE001
        pass


def cmd_verify_hook(a):
    """查看/设置验证钩子的状态与容差。

    这个命令存在的理由：勾了 ``--verify`` 但没生效，是最高频的困惑。
    把开关状态、注册的钩子名、调用/跳过计数一次打全，
    不用去猜"到底为什么没验"。
    """
    from avatar.verify import hook as HK
    from avatar.human_entropy import verify_hook as VH

    if a.on:
        VH.install_default(timeout=a.timeout)
    elif a.off:
        VH.uninstall()

    info = VH.describe()

    if a.json:
        import json as _j
        print(_j.dumps(info, ensure_ascii=False, indent=2))
        return 0

    _hr("验证钩子")
    h = info["hook"]
    if h.get("error"):
        print("状态未知  : %s" % h["error"])
    else:
        print("已注册    : %s" % ("是" if h["registered"] else "否"))
        print("钩子      : %s" % (h["hook"] or "(无)"))
        print("开关      : %s" % ("开" if h["enabled"] else "关"))
        print("已执行    : %d 次" % h["calls"])
        print("已跳过    : %d 次" % h["skipped"])

    _hr("落点容差")
    print("真人化    : %.1f px   （轨迹收尾有修正，允许偏差）"
          % info["human_landing_tol"])
    print("机械      : %.1f px   （路径精确，只留取整余量）"
          % info["mechanical_landing_tol"])
    print()
    print("开关用法：")
    print("  avatar verify-hook --on    装上默认钩子并开启")
    print("  avatar verify-hook --off   摘掉")
    print()
    print("注意：钩子只在**动作给了判据**时才验证。没判据就没验证 ——")
    print("      这是刻意的，因为无判据的验证产生的全是噪音。")
    return 0


# ==========================================================================
# 阶段五：风控命令（检测 / 熔断 / 打码）
# ==========================================================================
def _risk_summary(st) -> str:
    """把 RiskStatus 压成一行给人看的摘要。"""
    if st is None:
        return "? 无状态"
    return st.describe()


def cmd_detect(a):
    """单次检测，或 ``--watch`` 持续监听。"""
    import time as _t
    from avatar.risk_detector import detector as DET

    hwnd = 0
    if getattr(a, "hwnd", None) is not None:
        hwnd = _parse_hwnd(a)
    elif not getattr(a, "cdp_port", None):
        # 没给 hwnd 也没给端口 —— 用前台窗口
        hwnd = P.foreground_hwnd() or 0

    session = None
    url = ""
    if getattr(a, "cdp_port", None):
        try:
            session = _connect_web(a.cdp_port)
            url = getattr(session, "url", "") or ""
        except Exception as exc:      # noqa: BLE001
            print("[FAIL] 连不上 CDP 端口 %s：%s" % (a.cdp_port, exc),
                  file=sys.stderr)
            return 2

    interval = max(0.5, float(getattr(a, "interval", 2.0) or 2.0))
    watch = bool(getattr(a, "watch", False))
    timeout = float(getattr(a, "timeout", 3.0) or 3.0)
    rounds = 0
    hits = 0

    try:
        while True:
            rounds += 1
            t0 = _t.perf_counter()
            try:
                st = DET.detect(hwnd=hwnd or None, cdp_session=session,
                                timeout=timeout, url=url)
            except Exception as exc:      # noqa: BLE001
                print("[FAIL] 检测失败：%s: %s" % (type(exc).__name__, exc),
                      file=sys.stderr)
                return 2
            el = _t.perf_counter() - t0

            if getattr(a, "json", False):
                import json
                print(json.dumps({
                    "round": rounds,
                    "elapsed": round(el, 3),
                    "status": st.to_dict(),
                    "describe": st.describe(),
                }, ensure_ascii=False))
            else:
                if rounds == 1:
                    _hr("风控检测（hwnd=%s%s）"
                        % (hwnd or "-",
                           " cdp=%s" % a.cdp_port if session else ""))
                print("[%d] %.2fs  %s" % (rounds, el, _risk_summary(st)))

            if not st.is_clean:
                hits += 1
                if not getattr(a, "json", False):
                    print()
                    print("  ⚠ 需要人工介入")
                    print("  " + st.explain().replace("\n", "\n  "))
                    print()
                if not watch:
                    return 1
            elif not watch:
                if not getattr(a, "json", False):
                    print()
                    print("  [OK] 干净，可以放心动作")
                return 0

            if watch:
                try:
                    _t.sleep(interval)
                except KeyboardInterrupt:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if session is not None:
            _quiet_close(session)

    if watch:
        print()
        print("监听结束：%d 轮，%d 次需要介入" % (rounds, hits))
    return 1 if hits else 0


def cmd_pause_test(a):
    """演示熔断：手动触发 pause_for_human，按 F9 继续。"""
    from avatar.risk_detector import guard as G
    from avatar.risk_detector import context as C

    timeout = float(getattr(a, "timeout", 30.0) or 30.0)
    env = G.env_report()

    _hr("熔断演示（pause-test）")
    print("环境探测：")
    print("  会话      : %s" % env.get("session"))
    print("  有桌面    : %s" % ("是" if env.get("desktop") else "否"))
    print("  能出声    : %s" % ("是" if env.get("beep") else "否"))
    print("  热键可用  : %s" % ("是" if env.get("hotkey") else "否"))
    print("  桌面通知  : %s" % ("是" if env.get("notify") else "否"))
    if env.get("reason"):
        print("  说明      : %s" % env["reason"])
    print()
    print("截图目录：%s" % C.intervention_dir())
    print()

    if not env.get("desktop"):
        print("⚠ 当前没有可用桌面 —— 会走**退化路径**：")
        print("  写日志 -> 等固定超时 -> 抛 HumanInterventionTimeout")
        print("  不会响铃、不会轮询热键（那在没有桌面的环境里不可靠）")
    else:
        print("请听提示音，然后**按 F9** 表示'我处理完了'。")
        print("超时 %.0f 秒后会抛 HumanInterventionTimeout。" % timeout)
    print()

    try:
        out = G.pause_for_human(
            "pause-test：这是一次演示，按 F9 继续",
            hwnd=P.foreground_hwnd() or 0,
            current_step="pause-test",
            timeout=timeout,
            degraded_wait=min(3.0, timeout),
            on_wait=(lambda el, lim: print(
                "  等待中… %.0f/%.0fs" % (el, lim)) if getattr(a, "verbose",
                                                               False) else None),
        )
    except G.HumanInterventionTimeout as exc:
        print()
        print("[FAIL] 超时：%s" % exc)
        print("  等了 %.1fs（上限 %.1fs），退化路径=%s"
              % (exc.waited, exc.limit, exc.is_degraded))
        ctx = getattr(exc, "context", None)
        if ctx is not None:
            print("  现场已存：%s" % (ctx.screenshot_path or "(未截图)"))
            print("  日志：%s" % (C.intervention_dir() or ""))
            try:
                plan = ctx and __import__(
                    "avatar.risk_detector.resume", fromlist=["x"]
                ).resume_from(ctx, "retry")
                if plan:
                    print("  恢复建议：%s" % plan.describe())
            except Exception:      # noqa: BLE001
                pass
        return 1

    print()
    print("[OK] 收到 F9 —— 模拟'人处理完了'。")
    print("  等了 %.1fs，退化路径=%s" % (out["waited"], out["degraded"]))
    print("  现场截图：%s" % (out.get("screenshot") or "(未截图)"))
    print()
    print("接下来真实流程会调 resume_from(context, strategy) 从断点接回去。")
    print("  试一下：python -m avatar.risk_detector.resume")
    return 0


def cmd_solver(a):
    """查看/校验打码平台配置。**--test 不发真实请求。**"""
    from avatar.risk_detector import solver as SV

    cfg = SV.SolverConfig.from_env()
    enabled = SV.is_enabled(cfg)

    if getattr(a, "json", False):
        import json
        print(json.dumps({
            "enabled": enabled,
            "config": cfg.to_dict(),
            "checks": _solver_checks(cfg, SV),
        }, ensure_ascii=False, indent=2))
        return 0 if enabled else 1

    _hr("打码平台配置")
    print(SV.describe_solver(cfg))
    print()

    checks = _solver_checks(cfg, SV)
    print("配置校验（**不发任何请求**）：")
    print("%-34s %s" % ("检查项", "结果"))
    print("-" * 66)
    for name, ok, detail in checks:
        print("%-34s %s%s" % (name, "[OK]" if ok else "[FAIL]",
                              ("  " + detail) if detail else ""))

    if not enabled:
        print()
        print("当前**未启用**（这是默认状态）。")
        print("要启用，按上面'启用方式'里的任一种配置。")
        return 1

    print()
    print("[OK] 配置看起来是完整的。")
    print("  注意：这里**没有真的发请求**（--test 只做本地校验）。")
    print("  要真实验证，跑一次带 enabled=True 的 solve_captcha_via_api。")
    return 0


def _solver_checks(cfg, SV):
    """本地校验打码配置。返回 ``[(名称, 是否通过, 说明)]``。"""
    out = []

    out.append(("开关已打开（enabled）", bool(cfg.enabled),
                "enabled=%s" % cfg.enabled))

    has_key = bool(cfg.api_key)
    key_hint = ""
    if has_key:
        k = cfg.api_key
        key_hint = (k[:4] + "…" + k[-2:]) if len(k) > 8 else "***"
    out.append(("API key 已配置", has_key, key_hint or "缺失"))

    url = cfg.api_url or ""
    looks_url = url.startswith("http://") or url.startswith("https://")
    out.append(("API URL 形如 http(s)://", bool(url) and looks_url,
                url or "缺失"))

    mode = SV.pick_mode(url, cfg.mode)
    out.append(("编码方式可判定", mode in (SV.MODE_JSON, SV.MODE_MULTIPART),
                "→ %s" % mode))

    out.append(("超时已设置", float(cfg.timeout or 0) > 0,
                "%.1fs" % cfg.timeout))

    out.append(("User-Agent 非空", bool(cfg.user_agent),
                cfg.user_agent[:36]))

    # 平台识别（只是提示，不影响通过）
    u = url.lower()
    if "2captcha" in u:
        platform = "2Captcha"
    elif "capsolver" in u:
        platform = "CapSolver"
    else:
        platform = "未知（按通用格式处理）"
    out.append(("平台识别（仅提示）", True, platform))

    out.append(("默认关闭策略生效",
                not (has_key and not cfg.enabled),
                "只给 key 不给 enabled 时仍然关着"))

    return out


# ==========================================================================
# 阶段六：规划与执行命令
# ==========================================================================
def _build_plan(args, *, quiet: bool = False):
    """按参数造一个 Plan。返回 ``(plan, error_msg)``。"""
    from avatar.agentic_planner import get_planner, parse_goal
    from avatar.agentic_planner.plan import Plan

    # ---- 从文件加载 ----
    if getattr(args, "plan_file", None):
        import json
        path = args.plan_file
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:      # noqa: BLE001
            return None, "读不了计划文件 %s：%s" % (path, exc)
        try:
            plan = Plan.from_dict(data)
        except Exception as exc:      # noqa: BLE001
            return None, "计划文件格式不对：%s" % exc
        problems = plan.validate()
        if problems:
            return None, "计划不合法：\n  · " + "\n  · ".join(problems)
        return plan, ""

    # ---- 从目标生成 ----
    goal_txt = getattr(args, "goal", "") or ""
    if not goal_txt:
        return None, "必须给 --goal 或 --plan-file"

    goal = parse_goal(goal_txt)
    if not quiet:
        print("目标解析：%s" % goal.intent)
        for a in goal.actions:
            print("  · %s %s%s" % (a.get("action", "?"), a.get("target", ""),
                                   (" 「%s」" % a["text"]) if a.get("text")
                                   else ""))
        print()

    if not goal.ok:
        return None, ("目标无法拆解：%s\n  解析说明：%s"
                      % (goal.intent, "；".join(goal.notes) or "（无）"))

    planner_name = getattr(args, "planner", "rule") or "rule"
    try:
        planner = get_planner(planner_name)
    except ValueError as exc:
        return None, str(exc)

    try:
        plan = planner.plan(goal)
    except Exception as exc:      # noqa: BLE001
        return None, "规划失败：%s" % exc
    return plan, ""


def cmd_plan(a):
    """生成 Plan 并打印 DAG。**不执行任何动作。**"""
    plan, err = _build_plan(a)
    if plan is None:
        print("[FAIL] %s" % err, file=sys.stderr)
        return 2

    if getattr(a, "json", False):
        import json
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    _hr("执行计划（DAG）")
    print(plan.describe())
    print()
    print("拓扑序：%s" % " -> ".join(plan.topological_order()))
    ready = plan.ready_steps()
    print("初始可执行（可并行）：%s" % (", ".join(ready) or "（无）"))
    print()
    print("提示：用 `avatar run --goal \"...\"` 真的执行它。")
    return 0


def cmd_run(a):
    """执行一个计划。"""
    import time as _t

    plan, err = _build_plan(a)
    if plan is None:
        print("[FAIL] %s" % err, file=sys.stderr)
        return 2

    if getattr(a, "dry_run", False):
        _hr("试运行（--dry-run：只规划，不执行）")
        print(plan.describe())
        return 0

    from avatar.agentic_planner.executor import DefaultRunner, Executor

    _hr("执行计划 %s" % plan.plan_id)
    print(plan.describe())
    print()

    runner = None if getattr(a, "no_runner", False) else DefaultRunner()
    ex = Executor(
        runner,
        parallel=not getattr(a, "serial", False),
        task_timeout=float(getattr(a, "timeout", 1800.0) or 1800.0),
        max_retries=int(getattr(a, "max_retries", 3) or 3),
        max_switches=int(getattr(a, "max_switches", 2) or 2),
    )
    print(ex.describe())
    print()

    t0 = _t.perf_counter()
    try:
        out = ex.run(plan)
    except KeyboardInterrupt:
        print("\n已中断（任务存档已保存，可用 `avatar tasks --resume` 接着跑）",
              file=sys.stderr)
        return 130

    print()
    print(out.describe())
    print()
    print("任务存档：%s" % (out.task_id or "(无)"))
    if not out.ok:
        print("看详情：avatar tasks --show %s" % out.task_id)
        print("接着跑：avatar tasks --resume %s" % out.task_id)
    return 0 if out.ok else 1


def cmd_tasks(a):
    """列出 / 查看 / 恢复历史任务。"""
    from avatar.agentic_planner import memory as MEM

    # ---- 恢复 ----
    if getattr(a, "resume", None):
        from avatar.agentic_planner.executor import DefaultRunner, Executor

        tid = str(a.resume)
        _hr("恢复任务 %s" % tid)
        try:
            mem = MEM.ShortTermMemory.load(tid)
        except Exception as exc:      # noqa: BLE001
            print("[FAIL] %s" % exc, file=sys.stderr)
            return 2
        print(mem.summary())
        print()
        print("已成功的步骤不会重跑；失败/卡住的会重新进入调度。")
        print("注意：自愈计数**跨恢复累加**（不会因为重启就重置）。")
        print()

        ex = Executor(DefaultRunner(),
                      parallel=not getattr(a, "serial", False))
        out = ex.resume(tid)
        print(out.describe())
        return 0 if out.ok else 1

    # ---- 查看详情 ----
    if getattr(a, "show", None):
        tid = str(a.show)
        try:
            mem = MEM.ShortTermMemory.load(tid)
        except Exception as exc:      # noqa: BLE001
            print("[FAIL] %s" % exc, file=sys.stderr)
            return 2
        if getattr(a, "json", False):
            import json
            print(json.dumps(mem.to_dict(), ensure_ascii=False, indent=2))
            return 0
        _hr("任务详情")
        print(mem.summary())
        return 0

    # ---- 列出 ----
    tasks = MEM.list_tasks()
    if getattr(a, "json", False):
        import json
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return 0

    _hr("历史任务 %d 个" % len(tasks))
    if not tasks:
        print("（还没有任务。跑一个：avatar run --goal \"打开记事本输入hello\"）")
        print()
        print("存档目录：%s" % MEM.task_dir(create=False))
        return 0

    print("%-18s %-10s %-19s %-6s %s"
          % ("任务 ID", "状态", "更新时间", "步骤", "目标"))
    print("-" * 90)
    for t in tasks[:int(getattr(a, "limit", 20) or 20)]:
        mark = "  ⚠ 损坏" if t.get("broken") else ""
        ts = (time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(t["updated_at"]))
              if t.get("updated_at") else "-")
        print("%-18s %-10s %-19s %-6s %s%s"
              % (t["task_id"], t["task_state"], ts, t["steps"],
                 (t["instruction"] or "")[:40], mark))

    if len(tasks) > 20:
        print("... 还有 %d 个（用 --limit 调大）" % (len(tasks) - 20))
    print()
    print("看详情：avatar tasks --show <任务ID>")
    print("接着跑：avatar tasks --resume <任务ID>")
    return 0


# ==========================================================================
# 阶段七：视觉链 / LLM / MCP
# ==========================================================================
def cmd_vision_chain(a):
    """演示五层视觉降级链。"""
    from avatar.vision.chain import VisionChain

    text = str(getattr(a, "text", "") or "")
    if not text:
        print("[FAIL] 必须给 --text", file=sys.stderr)
        return 2

    hwnd = _parse_hwnd(a) if getattr(a, "hwnd", None) else 0
    chain = VisionChain(
        allow_uia=not getattr(a, "no_uia", False),
        allow_cdp=not getattr(a, "no_cdp", False),
        allow_ocr=not getattr(a, "no_ocr", False),
        allow_template=not getattr(a, "no_template", False),
        allow_llm=bool(getattr(a, "allow_llm", False)),
    )

    _hr("五层视觉降级链")
    print(chain.describe())
    print()
    print("查找目标：%r   窗口：%s" % (text, ("0x%X" % hwnd) if hwnd else "(自动)"))
    print()

    r = chain.locate(text, window=hwnd)
    if getattr(a, "json", False):
        import json
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
        return 0 if r.ok else 1

    print(r.explain())
    return 0 if r.ok else 1


def cmd_vision_llm(a):
    """视觉大模型定位 —— 单图测试 / 实时调用 / 成本 / 缓存。

    ## 四种模式

    - ``--image x.png --goal "..."`` —— 对单张图测试（**默认不真发请求**）
    - ``--live --goal "..."`` —— 截当前屏幕 + **真实调用**
    - ``--cost`` —— 显示预算
    - ``--clear-cache`` —— 清空缓存

    ⚠ 默认**不真发请求** —— 只验证"请求构造对不对"。
    真调用要 ``--live`` 且配了 key。
    """
    import json as _json
    from avatar.vision import cost as CO
    from avatar.vision import image_prep as IP
    from avatar.vision import llm_backend as LB
    from avatar.vision import prompts as PR
    from avatar.vision import validator as VD

    # ---- 清缓存 ----
    if getattr(a, "clear_cache", False):
        cache = CO.ResultCache()
        n = cache.clear()
        print("已清空缓存（%d 条）" % n)
        print("注意：缓存是**进程内**的，换个进程就是新的 —— "
              "这条命令主要用于同进程内的调试。")
        return 0

    # ---- 成本 ----
    if getattr(a, "cost", False):
        _hr("视觉调用成本")
        print(CO.describe_cost())
        print()
        b = CO.CostBudget()
        print(b.describe())
        return 0

    goal = str(getattr(a, "goal", "") or "")
    if not goal:
        print("[FAIL] 必须给 --goal", file=sys.stderr)
        return 2

    # ---- 造后端 ----
    provider = str(getattr(a, "provider", "openai") or "openai")
    try:
        backend = LB.get_vision_backend(
            provider,
            api_key=str(getattr(a, "api_key", "") or ""),
            base_url=str(getattr(a, "base_url", "") or ""),
            model=str(getattr(a, "model", "") or ""),
            enabled=bool(getattr(a, "enabled", False)))
    except ValueError as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        return 2

    _hr("视觉大模型定位")
    print(backend.describe())
    print()

    # ---- 取图 ----
    img_bytes = b""
    mime = "image/png"
    source = ""
    img_path = str(getattr(a, "image", "") or "")
    if img_path:
        import os as _os
        if not _os.path.isfile(img_path):
            print("[FAIL] 图片不存在：%s" % img_path, file=sys.stderr)
            return 2
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        mime = ("image/jpeg" if img_path.lower().endswith((".jpg", ".jpeg"))
                else "image/png")
        source = "文件 %s（%d 字节）" % (img_path, len(img_bytes))
    elif getattr(a, "live", False):
        from avatar.vision import capture as C
        try:
            shot = C.grab_screen()
        except Exception as exc:      # noqa: BLE001
            print("[FAIL] 抓屏失败：%s" % exc, file=sys.stderr)
            return 2
        prepared = IP.prepare_for_llm(shot, max_edge=int(
            getattr(a, "max_edge", 1280) or 1280))
        img_bytes, mime = prepared.data, prepared.mime
        source = ("当前屏幕（原图 %dx%d → 压缩 %dx%d，scale=%.3f，"
                  "约 %d tokens）"
                  % (prepared.src_width, prepared.src_height,
                     prepared.width, prepared.height, prepared.scale,
                     prepared.est_tokens))
    else:
        print("[FAIL] 必须给 --image 或 --live", file=sys.stderr)
        return 2

    print("目标：%r" % goal)
    print("来源：%s" % source)
    print()

    # ---- 只验证请求构造（默认）----
    if not getattr(a, "enabled", False):
        _hr("请求构造预览（**未真实发送**）")
        system, user = PR.build_prompt(goal, None)
        part = LB.normalize_image(img_bytes, mime,
                                  protocol=backend.protocol)
        payload = backend.build_payload(part, system, user)
        headers = backend.build_headers()

        print("URL     ：%s" % backend.base_url)
        print("方法    ：POST")
        print("请求头  ：")
        for k, v in headers.items():
            print("    %s: %s" % (k, v[:40] + ("…" if len(v) > 40 else "")))
        print()
        print("请求体  ：")
        body = _json.dumps(payload, ensure_ascii=False, indent=2)
        # base64 太长，截断显示
        if len(body) > 1200:
            body = body[:1200] + "\n  …（省略 %d 字符）" % (
                len(_json.dumps(payload, ensure_ascii=False)) - 1200)
        print(body)
        print()
        print("图片格式：%s（%d 字节 -> base64 约 %d 字节）"
              % (mime, len(img_bytes), (len(img_bytes) * 4 + 2) // 3))
        print()
        print("★ 上面是**完整请求构造**，一个字节都没发出去。")
        print("  要真调用：加 --enabled（且配好 api_key）")
        print("  要看成本：--cost")
        return 0

    # ---- 真实调用 ----
    if not backend.active:
        print("[FAIL] 后端不可用：%s" % backend.unavailable_reason(),
              file=sys.stderr)
        print("  提示：set AVATAR_LLM_API_KEY=sk-... 然后加 --enabled",
              file=sys.stderr)
        return 2

    budget = CO.CostBudget(
        per_request=int(getattr(a, "per_request_tokens", 2000) or 2000),
        total=int(getattr(a, "total_tokens", 20000) or 20000))
    cache = CO.ResultCache()
    validator = VD.CoordinateValidator(
        recheck=bool(getattr(a, "recheck", False)))

    _hr("真实调用")
    est = CO.estimate_image_tokens(1280, 720)
    print("预算：单次 %d / 累计 %d" % (budget.per_request, budget.total))
    print()

    system, user = PR.build_prompt(goal, None)
    try:
        budget.require(est + 500)
    except CO.CostExceeded as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        return 2

    res = backend.locate(img_bytes, goal, system=system, user=user,
                         mime=mime)
    budget.consume(res.total_tokens or est,
                   provider=res.provider, model=res.model,
                   completion_tokens=res.completion_tokens,
                   est_tokens=est, elapsed=res.elapsed, ok=res.ok)

    print(res.describe())
    print()
    if not res.ok:
        print("[FAIL] 调用失败：%s" % res.reason, file=sys.stderr)
        return 1
    if not res.found:
        print("· 模型说没找到：%s" % (res.reason or "无说明"))
        return 1

    print("模型给的图像坐标：(%d, %d)  conf=%.2f"
          % (res.x, res.y, res.confidence))
    print()

    # ---- 校验 ----
    gates = []
    validated = validator.validate(res, None, bitmap=None, trace=gates)
    print(validator.describe())
    print()
    for g in gates:
        print("  %s" % g.describe())
    print()
    if validated is None:
        print("[FAIL] 校验未通过 —— **不降级到盲点**")
        return 1
    print("[OK] 校验通过：屏幕坐标 (%d, %d)" % (validated.screen_x,
                                             validated.screen_y))
    print()
    print(budget.describe())
    return 0


def cmd_llm_plan(a):
    """用 LLMPlanner 生成 Plan（**没配 key 会回退规则版**）。"""
    import json as _json
    from avatar.agentic_planner import parse_goal
    from avatar.agentic_planner.planner import LLMPlanner

    goal_txt = str(getattr(a, "goal", "") or "")
    if not goal_txt:
        print("[FAIL] 必须给 --goal", file=sys.stderr)
        return 2

    provider = str(getattr(a, "provider", "openai") or "openai")
    planner = LLMPlanner(
        provider=provider,
        api_key=str(getattr(a, "api_key", "") or ""),
        base_url=str(getattr(a, "base_url", "") or ""),
        model=str(getattr(a, "model", "") or ""),
        enabled=bool(getattr(a, "enabled", False)),
    )

    _hr("LLM 规划")
    print(planner.describe())
    print()

    g = parse_goal(goal_txt)
    if not g.ok:
        print("[FAIL] 目标无法解析：%s" % g.intent, file=sys.stderr)
        return 2

    plan = planner.plan(g)

    if getattr(a, "json", False):
        print(_json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(plan.describe())
    print()
    if planner.last_fallback_reason:
        print("⚠ 回退到规则版：%s" % planner.last_fallback_reason)
    else:
        print("[OK] LLM 规划成功（规划器：%s）" % plan.planner)
    return 0


def cmd_mcp_serve(a):
    """启动 MCP Server（stdio）。"""
    from avatar.mcp.server import serve

    # ⚠ 日志一律走 stderr —— stdout 是协议通道（硬约束 8）
    args = []
    if getattr(a, "verbose", False):
        args.append("-v")
    return serve(args)


def cmd_mcp_test(a):
    """验证 MCP 工具注册正确（**不起服务**）。"""
    import json as _json
    from avatar.mcp import MCP_PROTOCOL_VERSION, SERVER_NAME
    from avatar.mcp.server import MCPServer
    from avatar.mcp.tools import build_default_tools

    reg = build_default_tools()
    srv = MCPServer(registry=reg)

    _hr("MCP 工具注册检查")
    print("服务标识：%s" % SERVER_NAME)
    print("协议版本：%s" % MCP_PROTOCOL_VERSION)
    print("工具数量：%d" % len(reg))
    print()

    bad = []
    for t in reg.all():
        sch = t.input_schema or {}
        problems = []
        if sch.get("type") != "object":
            problems.append("type 不是 object")
        if "properties" not in sch:
            problems.append("缺 properties")
        if "required" not in sch:
            problems.append("缺 required")
        for r in (sch.get("required") or []):
            if r not in (sch.get("properties") or {}):
                problems.append("required 里的 %s 不在 properties" % r)
        if not t.description:
            problems.append("没有 description")
        mark = "✓" if not problems else "✗"
        print("  %s %-26s %s" % (mark, t.name,
                                 "；".join(problems) or "schema 完整"))
        if problems:
            bad.append(t.name)

    print()
    # 走一遍协议层（用内存流，不碰真实 stdio）
    class _Sink:
        def write(self, d):
            return len(d) if isinstance(d, (bytes, bytearray)) else 0

        def flush(self):
            pass

    srv.stdout = _Sink()
    r_init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": MCP_PROTOCOL_VERSION}})
    ok_init = (r_init or {}).get("result", {}).get("protocolVersion") == \
        MCP_PROTOCOL_VERSION
    r_list = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    n_list = len(((r_list or {}).get("result") or {}).get("tools") or [])

    print("协议自测：")
    print("  %s initialize 握手" % ("[OK]" if ok_init else "[FAIL]"))
    print("  %s tools/list 返回 %d 个工具" % ("[OK]" if n_list == len(reg) else "[FAIL]",
                                              n_list))
    print()

    if getattr(a, "json", False):
        print(_json.dumps({"tools": reg.names(), "bad": bad,
                           "initialize_ok": ok_init,
                           "tools_listed": n_list},
                          ensure_ascii=False, indent=2))

    ok = (not bad) and ok_init and n_list == len(reg)
    print("结果：%s" % ("全部通过" if ok else "有问题（见上）"))
    if ok:
        print()
        print("接入 Claude Desktop：把 mcp_config.json 里的内容合并进")
        print("  %APPDATA%\\Claude\\claude_desktop_config.json")
        print("然后重启 Claude Desktop。")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="avatar",
        description="Avatar %s —— 让 AI 以人类形式操纵电脑。阶段一：底层感知。" % __version__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出（供程序消费）")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("selfcheck", help="阶段一自检（先跑这个）")
    sp.set_defaults(func=cmd_selfcheck)

    sp = sub.add_parser("dpi", help="DPI 感知与显示器详情")
    sp.set_defaults(func=cmd_dpi)

    sp = sub.add_parser("layout", help="多屏布局")
    sp.set_defaults(func=cmd_layout)

    sp = sub.add_parser("windows", help="列出窗口")
    sp.add_argument("--process", "-p", help="按进程名过滤，如 msedge")
    sp.add_argument("--title", "-t", help="标题子串（正则）")
    sp.add_argument("--all", action="store_true", help="包含不可见窗口")
    sp.add_argument("--usable", action="store_true", help="只留可操作窗口")
    sp.add_argument("--min-size", type=int, default=0, help="最小边长")
    sp.add_argument("--limit", "-n", type=int, default=0, help="最多列几个")
    sp.set_defaults(func=cmd_windows)

    sp = sub.add_parser("geom", help="窗口几何")
    sp.add_argument("--hwnd", help="窗口句柄（十进制或 0x 十六进制），省略=前台窗口")
    sp.set_defaults(func=cmd_geom)

    sp = sub.add_parser("cursor", help="光标位置与坐标换算")
    sp.set_defaults(func=cmd_cursor)

    sp = sub.add_parser("pixel", help="读单个像素")
    sp.add_argument("--x", type=int, required=True)
    sp.add_argument("--y", type=int, required=True)
    sp.set_defaults(func=cmd_pixel)

    sp = sub.add_parser("shot", help="抓屏存 BMP")
    sp.add_argument("--out", "-o", help="输出路径（默认 shot_HHMMSS.bmp）")
    sp.add_argument("--hwnd", help="只抓某个窗口")
    sp.add_argument("--client", action="store_true", help="只抓客户区")
    sp.add_argument("--region", help="区域 x,y,w,h（物理像素）")
    sp.add_argument("--downsample", type=int, default=1, help="降采样倍率")
    sp.set_defaults(func=cmd_shot)

    sp = sub.add_parser("pick", help="坐标诊断：这个点在哪、属于谁")
    sp.add_argument("--x", type=int, required=True)
    sp.add_argument("--y", type=int, required=True)
    sp.set_defaults(func=cmd_pick)

    # ---- 阶段三：输入层 -------------------------------------------------
    sp = sub.add_parser("move", help="移动光标（--human 走真人化轨迹）")
    sp.add_argument("--x", type=int, required=True, help="屏幕绝对 X")
    sp.add_argument("--y", type=int, required=True, help="屏幕绝对 Y")
    sp.add_argument("--human", action="store_true",
                    help="走真人化轨迹（贝塞尔+非对称速度+震颤+多段子运动）")
    sp.add_argument("--precision", type=float, default=1.0,
                    help="精度要求，>1 更谨慎（小目标）")
    sp.add_argument("--from-x", type=int, help="指定起点 X（默认用当前光标）")
    sp.add_argument("--from-y", type=int, help="指定起点 Y")
    sp.add_argument("--dry-run", action="store_true",
                    help="只规划轨迹并打印指标，不动真实光标")
    sp.add_argument("--seed", type=int, help="随机种子（同 seed 轨迹可复现）")
    sp.set_defaults(func=cmd_move)

    sp = sub.add_parser("type", help="输入文本（--human 走真人打字节奏）")
    sp.add_argument("--text", help="要输入的文本")
    sp.add_argument("--file", help="从文件读取文本（UTF-8）")
    sp.add_argument("--human", action="store_true", help="真人化打字节奏")
    sp.add_argument("--wpm", type=float, default=320.0,
                    help="目标速度（词/分钟，默认 320；中文按字符折算）")
    sp.add_argument("--skill", type=float, default=1.0, help="熟练度倍数，>1 更快")
    sp.add_argument("--typo-rate", type=float, default=0.0,
                    help="错字率 0-0.08（默认 0，不打错）")
    sp.add_argument("--no-correct", action="store_true", help="打错后不回改")
    sp.add_argument("--no-pause", action="store_true", help="结尾不停顿")
    sp.add_argument("--dry-run", action="store_true", help="只估算时长，不真的输入")
    sp.add_argument("--seed", type=int, help="随机种子")
    sp.set_defaults(func=cmd_type)

    sp = sub.add_parser("mode", help="查看/切换交互模式（silent / physical）")
    sp.add_argument("--set", choices=["silent", "physical"],
                    help="切换到指定模式")
    sp.add_argument("--fallback", action="store_true", default=None,
                    help="允许静默失败后降级到物理")
    sp.add_argument("--no-fallback", dest="fallback", action="store_false",
                    help="禁止降级（静默失败就直接报错）")
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser("entropy", help="真人熵演示与可复现性验证")
    sp.add_argument("--seed", type=int, help="随机种子（默认 42）")
    sp.add_argument("--to-x", type=int, default=1100, help="演示目标 X")
    sp.add_argument("--to-y", type=int, default=640, help="演示目标 Y")
    sp.add_argument("--demo", action="store_true", help="真的移动光标演示（结束后归位）")
    sp.set_defaults(func=cmd_entropy)

    # ---- 阶段四：Web CDP ------------------------------------------------
    def _add_web_common(p_, *, selector=False, eval_=False):
        """web 子命令共享的连接参数。

        抽出来是为了**保证三条命令的连接语义完全一致** ——
        手抄一遍参数很容易漏 ``--port`` 之类，然后某个命令就连不上了。
        """
        p_.add_argument("--url", help="目标 URL")
        p_.add_argument("--port", type=int, help="调试端口（省略则自动探测/新起）")
        p_.add_argument("--exe", help="浏览器可执行文件路径")
        p_.add_argument("--headless", action="store_true", help="无头运行")
        p_.add_argument("--profile", help="用户数据目录（隔离会话用）")
        p_.add_argument("--timeout", type=float, default=5.0, help="超时秒数")
        p_.add_argument("--close-browser", action="store_true",
                        help="结束后关掉自己起的浏览器实例")
        if selector:
            p_.add_argument("--selector", "-s", help="CSS 选择器")
        if eval_:
            p_.add_argument("--js", help="要执行的 JS 表达式")
            p_.add_argument("--file", help="从文件读 JS")
        return p_

    sp = _add_web_common(sub.add_parser(
        "web-opts", help="诊断：可用浏览器 + 已有调试端口 + Electron 窗口"))
    sp.set_defaults(func=cmd_web_opts)

    sp = _add_web_common(sub.add_parser("web-open", help="打开页面 / 复用调试实例"))
    sp.add_argument("--title", help="顺带验证页面标题包含此串")
    sp.set_defaults(func=cmd_web_open)

    sp = _add_web_common(sub.add_parser("web-click", help="按选择器点击"),
                         selector=True)
    sp.add_argument("--button", default="left", choices=["left", "right", "middle"])
    sp.add_argument("--clicks", type=int, default=1)
    sp.add_argument("--expect-text", help="点击后某元素的文本应包含此串")
    sp.add_argument("--expect-selector", help="期望出现的元素选择器（默认同 selector）")
    sp.add_argument("--verify", help="点击后应变为可见的元素选择器")
    sp.set_defaults(func=cmd_web_click)

    sp = _add_web_common(sub.add_parser("web-eval", help="执行 JS"),
                         eval_=True)
    sp.set_defaults(func=cmd_web_eval)

    # ---- 阶段四：Electron -----------------------------------------------
    sp = sub.add_parser("electron-attach",
                        help="把 Electron 应用接到 CDP（需带调试端口启动）")
    sp.add_argument("--hwnd", help="窗口句柄（十进制或 0x 十六进制）")
    sp.add_argument("--process", "-p", help="进程名，如 Code.exe / DingTalk.exe")
    sp.add_argument("--eval", help="附加成功后顺带执行一段 JS")
    sp.add_argument("--timeout", type=float, default=5.0)
    sp.set_defaults(func=cmd_electron_attach)

    sp = sub.add_parser("electron-windows", help="列出运行中的 Electron 窗口")
    sp.add_argument("--process", "-p", action="append",
                    help="进程名过滤（可重复）")
    sp.add_argument("--cdp-only", action="store_true",
                    help="只列已经开着调试端口的（能用 CDP 的）")
    sp.set_defaults(func=cmd_electron_windows)

    # ---- 阶段四：统一验证 -----------------------------------------------
    sp = sub.add_parser("verify", help="验证执行结果（桌面 / Web 统一入口）")
    sp.add_argument("--mode", default="auto",
                    choices=["auto", "desktop", "web"],
                    help="验证通道（auto 优先走 web）")
    sp.add_argument("--timeout", type=float, default=5.0)
    sp.add_argument("--settle", type=float, help="判定前先等这么久")
    # ⚠ 不要在子命令里再定义 ``dest="json"`` —— argparse 会用子命令的
    #   默认值（False）覆盖全局 ``--json`` 的 True，表现为
    #   ``avatar --json verify ...`` 完全被忽略。子命令这边不定义即可，
    #   全局的 ``args.json`` 自然可见。
    g_w = sp.add_argument_group("Web 判据")
    g_w.add_argument("--selector", "-s", help="元素选择器")
    g_w.add_argument("--expect-text", help="元素文本应包含此串")
    g_w.add_argument("--expect-value", help="元素 value 应等于此串")
    g_w.add_argument("--url-contains", help="URL 应包含此串")
    g_w.add_argument("--port", type=int, help="调试端口")
    g_w.add_argument("--exe", help="浏览器路径")
    g_w.add_argument("--headless", action="store_true")
    g_w.add_argument("--profile", help="用户数据目录")
    g_w.add_argument("--url", help="先导航到这个 URL")
    g_w.add_argument("--close-browser", action="store_true")
    g_d = sp.add_argument_group("桌面判据")
    g_d.add_argument("--hwnd", help="窗口句柄")
    g_d.add_argument("--target", help="UIA 元素名/查询表达式")
    g_d.add_argument("--expect-title", help="窗口标题应包含此串")
    g_d.add_argument("--expect-exists", action="store_true",
                     help="目标元素应存在")
    # 这两个旋钮是把查询"收紧成可下推"的关键：
    # 不做收紧时，字符串目标会走 Python 全遍历（实测全桌面 52.8 秒），
    # 而且 --timeout 完全管不住。给了就能落到 UIA 原生快通道（秒级以内）。
    g_d.add_argument("--name-exact", action="store_true",
                     help="元素名按完全相等匹配（可下推，快）")
    g_d.add_argument("--control-type",
                     help="限定控件类型，如 button/edit/text（可下推，快）")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("verify-hook",
                        help="查看/开关动作后的自动验证钩子")
    sp.add_argument("--on", action="store_true", help="装上默认钩子并开启")
    sp.add_argument("--off", action="store_true", help="摘掉钩子")
    sp.add_argument("--timeout", type=float, default=5.0,
                    help="单次验证超时")
    sp.set_defaults(func=cmd_verify_hook)

    # ---------------- 阶段五：风控 ----------------
    sp = sub.add_parser("detect",
                        help="风控检测：单次，或 --watch 持续监听")
    sp.add_argument("--hwnd", default=None,
                    help="目标窗口句柄（十进制或 0x 十六进制）；不传用前台窗口")
    sp.add_argument("--cdp-port", type=int, default=None,
                    help="Chrome 调试端口（走 Web 层检测）")
    sp.add_argument("--watch", action="store_true",
                    help="持续监听（默认每 2s 一次），检测到就报警")
    sp.add_argument("--interval", type=float, default=2.0,
                    help="--watch 的间隔秒数（默认 2.0）")
    sp.add_argument("--timeout", type=float, default=3.0,
                    help="单次检测的时间预算（默认 3.0）")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("pause-test",
                        help="演示熔断：手动触发 pause_for_human，按 F9 继续")
    sp.add_argument("--timeout", type=float, default=30.0,
                    help="等待人工的秒数（默认 30，演示用）")
    sp.add_argument("--verbose", action="store_true",
                    help="打印等待进度")
    sp.set_defaults(func=cmd_pause_test)

    sp = sub.add_parser("solver",
                        help="打码平台配置查看与校验（默认关闭）")
    sp.add_argument("--test", action="store_true",
                    help="校验配置是否正确（**不发真实请求**）")
    sp.set_defaults(func=cmd_solver)

    # ---------------- 阶段六：规划与执行 ----------------
    sp = sub.add_parser("plan",
                        help="生成执行计划并打印 DAG（不执行）")
    sp.add_argument("--goal", default="", help="自然语言目标")
    sp.add_argument("--plan-file", default="", help="从 JSON 文件加载计划")
    sp.add_argument("--planner", default="rule",
                    choices=["rule", "llm"], help="规划后端（默认 rule）")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("run",
                        help="执行计划（风控闸 → 观测 → 执行 → 验证 → 恢复）")
    sp.add_argument("--goal", default="", help="自然语言目标")
    sp.add_argument("--plan-file", default="", help="从 JSON 文件加载计划")
    sp.add_argument("--planner", default="rule",
                    choices=["rule", "llm"], help="规划后端（默认 rule）")
    sp.add_argument("--dry-run", action="store_true",
                    help="只规划不执行")
    sp.add_argument("--serial", action="store_true",
                    help="禁用并行（DAG 里的无依赖步骤也串行跑）")
    sp.add_argument("--no-runner", action="store_true",
                    help="不注入真实执行器（只跑调度骨架，用于排查）")
    sp.add_argument("--timeout", type=float, default=1800.0,
                    help="全局超时秒数（默认 1800 = 30 分钟）")
    sp.add_argument("--max-retries", type=int, default=3,
                    help="单步重试上限（默认 3）")
    sp.add_argument("--max-switches", type=int, default=2,
                    help="单步换策略上限（默认 2）")
    sp.set_defaults(func=cmd_run)

    # ---------------- 阶段七：视觉链 / LLM / MCP ----------------
    sp = sub.add_parser("vision",
                        help="视觉降级链（UIA → CDP → OCR → 模板 → 大模型）")
    vsub = sp.add_subparsers(dest="vision_cmd")
    vc = vsub.add_parser("chain", help="演示五层降级查找")
    vc.add_argument("--text", required=True, help="要找的文字/控件名")
    vc.add_argument("--hwnd", default=None, help="目标窗口句柄")
    vc.add_argument("--no-uia", action="store_true", help="关掉 UIA 层")
    vc.add_argument("--no-cdp", action="store_true", help="关掉 CDP 层")
    vc.add_argument("--no-ocr", action="store_true", help="关掉 OCR 层")
    vc.add_argument("--no-template", action="store_true",
                    help="关掉模板匹配层")
    vc.add_argument("--allow-llm", action="store_true",
                    help="打开视觉大模型层（阶段八）")
    vc.set_defaults(func=cmd_vision_chain)

    vl = vsub.add_parser("llm", help="视觉大模型定位（单图 / 实时 / 成本）")
    vl.add_argument("--goal", default="", help="要找什么（如「确定按钮」）")
    vl.add_argument("--image", default="", help="对单张图测试（png/jpg）")
    vl.add_argument("--live", action="store_true",
                    help="截当前屏幕（配合 --enabled 会真调用）")
    vl.add_argument("--provider", default="openai",
                    choices=["openai", "anthropic", "deepseek", "kimi",
                             "qwen", "glm"], help="厂商")
    vl.add_argument("--api-key", default="", help="API key")
    vl.add_argument("--base-url", default="", help="自定义端点")
    vl.add_argument("--model", default="", help="模型名")
    vl.add_argument("--enabled", action="store_true",
                    help="**真的发请求**（默认只预览请求构造）")
    vl.add_argument("--max-edge", type=int, default=1280,
                    help="缩放后最长边（默认 1280）")
    vl.add_argument("--recheck", action="store_true",
                    help="开启二次确认（会多一次调用）")
    vl.add_argument("--per-request-tokens", type=int, default=2000,
                    help="单次 token 上限")
    vl.add_argument("--total-tokens", type=int, default=20000,
                    help="累计 token 上限")
    vl.add_argument("--cost", action="store_true", help="显示成本预算")
    vl.add_argument("--clear-cache", action="store_true", help="清空结果缓存")
    vl.set_defaults(func=cmd_vision_llm)

    sp = sub.add_parser("llm", help="LLM 规划（没配 key 会回退规则版）")
    lsub = sp.add_subparsers(dest="llm_cmd")
    lp = lsub.add_parser("plan", help="用 LLMPlanner 生成 Plan")
    lp.add_argument("--goal", required=True, help="自然语言目标")
    lp.add_argument("--provider", default="openai",
                    choices=["openai", "anthropic", "deepseek", "kimi",
                             "qwen"], help="厂商（默认 openai）")
    lp.add_argument("--api-key", default="", help="API key（也可用环境变量）")
    lp.add_argument("--base-url", default="", help="自定义端点")
    lp.add_argument("--model", default="", help="模型名")
    lp.add_argument("--enabled", action="store_true",
                    help="真的调用（默认只回退规则版）")
    lp.set_defaults(func=cmd_llm_plan)

    sp = sub.add_parser("mcp", help="MCP Server（接 Claude Desktop / Cursor）")
    msub = sp.add_subparsers(dest="mcp_cmd")
    ms = msub.add_parser("serve", help="启动 MCP Server（stdio）")
    ms.add_argument("-v", "--verbose", action="store_true",
                    help="日志走 stderr")
    ms.set_defaults(func=cmd_mcp_serve)
    mt = msub.add_parser("test", help="验证 MCP 工具注册（不起服务）")
    mt.set_defaults(func=cmd_mcp_test)

    sp = sub.add_parser("tasks", help="历史任务：列出 / 查看 / 恢复")
    sp.add_argument("--show", default=None, help="查看某个任务的详情")
    sp.add_argument("--resume", default=None, help="从磁盘恢复并继续执行")
    sp.add_argument("--limit", type=int, default=20, help="列出多少条")
    sp.add_argument("--serial", action="store_true",
                    help="--resume 时禁用并行")
    sp.set_defaults(func=cmd_tasks)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except AvatarError as e:
        print("[FAIL] %s" % e, file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
