#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Avatar × QQ 自动化 Showcase —— 五层降级链 + 真人熵 + 结果验证。

## 这个 Demo 演示什么

用 Avatar 自动操控 **PC 版 QQ**：搜索好友 → 发一条测试消息。
重点不是"能发出去"，而是展示 Avatar 的三个核心机制：

```
┌──────────────────────────────────────────────────────────────┐
│ 1. 五层降级链                                                │
│    UIA → CDP → OCR → 模板匹配 → 视觉大模型                   │
│    QQ（NT 版）是 Electron，UIA 基本被封死 —— 正好用它演示     │
│    "第一层失败后自动往下走"，并打印每层的 fallback_used/reason │
├──────────────────────────────────────────────────────────────┤
│ 2. 真人熵引擎（human_entropy）                                │
│    鼠标：贝塞尔 + 多段子运动 + 8~12Hz 手抖 + 反应延迟         │
│    键盘：高斯分布按键间隔 + 词间停顿 + 结尾确认停顿            │
│    目的：避免被 QQ 客户端的风控判成脚本                       │
├──────────────────────────────────────────────────────────────┤
│ 3. 执行结果验证                                               │
│    发送后重新截取输入框区域，比较"墨迹占比"是否显著下降       │
│    （输入框清空 = 发送成功），不靠"看起来像成功了"           │
└──────────────────────────────────────────────────────────────┘
```

## 怎么跑

```bash
# 1) 先干跑：只定位、不点、不打字（默认就是这个模式）
python examples/qq_auto.py --friend "文件传输助手"

# 2) 确认定位到的位置没问题，再真发
python examples/qq_auto.py --friend "文件传输助手" --yes

# 3) 指定消息内容（默认是安全测试串）
python examples/qq_auto.py --friend "xxx" --message "Hello from Avatar AI!" --yes
```

**前置条件**：

- Windows 桌面（非锁屏、非远程会话断开）
- QQ 已登录（Demo 会找 `QQ.exe` 窗口；找不到会尝试启动）
- 建议先用「文件传输助手」当收件人 —— 发错了也只是发给自己

## 安全边界（这个 Demo 刻意做的）

- **默认 dry-run**：不加 `--yes` 只走到"准备发送"就停，一个字符都不会发出去。
- **消息内容固定为安全串**：`Hello from Avatar AI!`，可改但不建议发业务内容。
- **只做三件事**：搜索、输入、回车。不读聊天记录、不碰文件、不改设置。
- **收件人由你指定**，脚本不会猜"最近聊过的人"。

## 给二次开发者的注释

代码里凡是 `★` 开头的注释，都是"踩过坑才这么写"的地方，
值得抄到自己项目里；普通注释只是解释逻辑。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------
# 让脚本能直接 `python examples/qq_auto.py` 跑（不必先 pip install -e .）
# ★ 这一行必须在 import avatar 之前 —— 否则从 examples/ 目录跑会 ImportError
# --------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from avatar.app_manager import foreground as FG          # noqa: E402
from avatar.app_manager import launcher as LAUNCH        # noqa: E402
from avatar.app_manager import windows as W              # noqa: E402
from avatar.human_entropy import keyboard as HK          # noqa: E402
from avatar.human_entropy import mouse as HM             # noqa: E402
from avatar.vision import capture as C                   # noqa: E402
from avatar.vision.chain import (LAYER_CDP, LAYER_LLM,   # noqa: E402
                                 LAYER_OCR, LAYER_TEMPLATE,
                                 LAYER_UIA, VisionChain)

#: 默认消息。**故意选一句无害的测试串** —— 这个 Demo 是给别人抄的，
#: 不该出现"发送真实业务内容"的示例。
DEFAULT_MESSAGE = "Hello from Avatar AI!"

#: QQ 主窗口可能的进程名（不同版本不一样，全都试一遍）。
QQ_PROCESS_NAMES = ("QQ", "qq", "QQNT", "QQScLauncher")

#: 层名 → 中文，日志里用。
LAYER_LABEL = {
    LAYER_UIA: "UIA（控件树）",
    LAYER_CDP: "CDP（Chromium 调试）",
    LAYER_OCR: "OCR（文字识别）",
    LAYER_TEMPLATE: "模板匹配",
    LAYER_LLM: "视觉大模型",
}


# ==========================================================================
# 日志 —— 让用户看到"思考 → 决策 → 执行 → 验证"
# ==========================================================================
def log(tag: str, msg: str) -> None:
    """带时间戳的单行日志。

    ★ 为什么要自己写而不用 logging：
      Demo 的读者只关心"现在到哪一步了"，logging 的级别/格式是噪音。
      统一前缀（``[定位]`` / ``[降级]`` / ``[执行]`` / ``[验证]``）比
      时间戳更有用 —— 用户扫一眼就知道卡在哪个阶段。
    """
    print("  [%s] %s" % (tag, msg), flush=True)


def banner(title: str) -> None:
    print()
    print("=" * 66)
    print("  " + title)
    print("=" * 66)


# ==========================================================================
# 一、找到 QQ 窗口
# ==========================================================================
def _window_has_content(hwnd: int) -> bool:
    """激活窗口并抓图，检查界面是否"真的有内容"。

    ★ 为什么需要这道检查 —— 实测踩过的坑：
      QQ 会留下**空白残留窗**（同进程、大尺寸、整片纯背景色）。
      2026-10-18 连续两次抓到它：抓图 1600x1200 全屏灰度 20，
      后续所有校准算法自然全部失效。按面积选窗根本防不住，
      唯一可靠的办法是抓图抽样验证"非背景像素占比"。
    """
    import collections
    try:
        FG.activate_once(hwnd)
        time.sleep(0.8)
        shot, _origin = _grab_client_screen(hwnd)
        cnt = collections.Counter()
        n = 0
        for y in range(0, shot.height, 16):
            for x in range(0, shot.width, 16):
                px = shot.get_pixel(x, y)
                cnt[sum(px) // len(px)] += 1
                n += 1
        _, mode_n = cnt.most_common(1)[0]
        rich = (n - mode_n) / n > 0.02      # 超过 2% 像素偏离背景 = 有内容
        if not rich:
            log("窗口", "hwnd=0x%X 抓图是纯色（空白残留窗或被遮挡）" % hwnd)
        return rich
    except Exception as exc:      # noqa: BLE001
        log("窗口", "hwnd=0x%X 验证失败：%s" % (hwnd, exc))
        return False


def find_qq_window() -> Optional[W.WindowInfo]:
    """找 QQ 主窗口。找不到返回 None（**不抛**，让上层决定要不要启动）。

    ★ 为什么按进程名而不是标题找：
      QQ 的窗口标题会随会话变（"QQ" / "张三" / "群聊(3)"），
      按标题匹配是自找麻烦；进程名稳定得多。

    ★ 为什么不能只挑面积最大的窗口：
      见 :func:`_window_has_content` —— QQ 的空白残留窗尺寸比主窗口
      还大。所以这里按面积降序逐个"激活 → 抓图 → 内容验证"，
      第一个通过的才返回；全部失败则退回面积最大的（并大声警告）。
    """
    for proc in QQ_PROCESS_NAMES:
        try:
            wins = W.find_windows(process=proc, usable_only=False)
        except Exception:      # noqa: BLE001
            continue
        cand = [w for w in wins
                if w.rect[2] > 600 and w.rect[3] > 500 and not w.minimized]
        if not cand:
            continue
        cand.sort(key=lambda w: w.rect[2] * w.rect[3], reverse=True)
        fallback = cand[0]
        for w in cand[:3]:          # 最多试 3 个，别在辅助窗口上耗太久
            log("窗口", "候选 hwnd=0x%X %dx%d → 激活并验证内容…"
                % (w.hwnd, w.rect[2], w.rect[3]))
            if _window_has_content(w.hwnd):
                return w
        log("窗口", "⚠⚠ 没有窗口通过内容验证，退回面积最大的 0x%X —— "
                    "校准可能拿到空白图！" % fallback.hwnd)
        return fallback
    return None


def ensure_qq_window(auto_launch: bool = True) -> Optional[W.WindowInfo]:
    """确保 QQ 有可用窗口：先找，找不到再尝试启动。"""
    w = find_qq_window()
    if w:
        log("窗口", "已附着到 QQ：hwnd=0x%X  %dx%d" % (w.hwnd, w.rect[2], w.rect[3]))
        return w

    if not auto_launch:
        log("窗口", "没找到 QQ 窗口，且 --no-launch 已禁止启动")
        return None

    log("窗口", "没找到 QQ，尝试启动…")
    try:
        # ★ 用 open_app 而不是 subprocess：它会等窗口真正出现，
        #   并且处理"已有一个实例"的情况（reuse=True）。
        app = LAUNCH.open_app("QQ", timeout=40, reuse=True)
        log("窗口", "启动成功：hwnd=0x%X" % app.hwnd)
        return find_qq_window()
    except Exception as exc:      # noqa: BLE001
        log("窗口", "启动失败：%s" % exc)
        return None


def activate(hwnd: int) -> bool:
    """把窗口切到前台。

    ★ 为什么要显式激活：
      点击/打字都发给"前台窗口"。如果 QQ 在后台，鼠标坐标点下去
      可能落到别的窗口上 —— 这是自动化脚本最经典的事故。
    """
    ok = FG.activate_once(hwnd)
    if ok:
        time.sleep(0.35)      # 等窗口动画结束，不然点击可能被吞
    return ok


def _foreground_is(hwnd: int) -> bool:
    """前台窗口是否就是 hwnd（拿不到前台句柄时按失败处理）。"""
    return (FG.foreground_hwnd() or 0) == hwnd


def focus_qq(hwnd: int, retries: int = 4) -> bool:
    """激活 QQ 并**验证**前台真的是它；失败就重试。

    ★ activate_once 返回 True ≠ 焦点真的到手 —— 实测：activate_once
      报成功，0.8 秒后前台却是一个 Edge 窗口（网页周期性抢焦点 /
      Windows 前台锁）。点错窗口还是小事，打错窗口（比如往浏览器
      地址栏里打字再回车）就是事故了。所以**必须验证 + 重试**：
      激活 → sleep → 核对 foreground_hwnd()，不匹配再来。
    """
    for i in range(retries):
        activate(hwnd)
        time.sleep(0.45)
        if _foreground_is(hwnd):
            if i:
                log("窗口", "第 %d 次重试后拿到前台焦点" % (i + 1))
            return True
        log("窗口", "激活后前台不是 QQ（0x%X），重试 %d/%d"
            % (FG.foreground_hwnd() or 0, i + 1, retries))
    return False


# ==========================================================================
# 二、五层降级链定位
# ==========================================================================
def build_chain() -> VisionChain:
    """构造五层链。

    ★ 默认配置就够了：
      - UIA / CDP / OCR / 模板 四层开着
      - LLM 层**默认关闭**（最贵，且要配 key）—— 这里也不开，
        因为 Demo 要保证"零配置可跑"
    """
    return VisionChain(allow_uia=True, allow_cdp=True,
                       allow_ocr=True, allow_template=True,
                       allow_llm=False)


def locate_with_chain(chain: VisionChain, name: str, hwnd: int,
                      client_size: Tuple[int, int],
                      region: Optional[Tuple[int, int, int, int]] = None,
                      template: Optional[object] = None
                      ) -> Tuple[Optional[Tuple[int, int]], str]:
    """用五层链找目标，返回 ``(客户区坐标 或 None, 方法名)``。

    ★ 这是整个 Demo 的核心：**把每一层的尝试都打出来**。
      用户能看到"UIA 为什么失败、最后靠哪层成功"，
      而不是只看到一个"找不到"的结论。

    ``template`` 给了模板图，模板匹配层才可能命中；
    不给的话那一层会明确报"未提供模板"，而不是默默跳过。
    """
    res = chain.locate(name=name, window=hwnd, region=region,
                       template=template, threshold=0.80, stride=2)

    for at in res.attempts:
        label = LAYER_LABEL.get(at.layer, at.layer)
        if at.skipped:
            log("定位", "%-18s — 跳过：%s" % (label, at.reason or "本层关闭"))
        elif at.ok:
            log("定位", "%-18s ✓ 命中 conf=%.2f  用时 %.0fms"
                % (label, at.confidence, at.elapsed * 1000))
        else:
            log("定位", "%-18s ✗ %s" % (label, (at.reason or "未命中")[:56]))

    if res.ok and res.rect:
        # ★ rect 是**客户区坐标**（左上原点）。要变成屏幕坐标给鼠标用，
        #   必须加客户区在屏幕上的原点 —— 直接拿 rect 当屏幕坐标点下去
        #   会偏出去一个标题栏 + 边框的距离。
        cx = res.rect[0] + res.rect[2] // 2
        cy = res.rect[1] + res.rect[3] // 2
        log("降级", "fallback_used=%s  method=%s"
            % (res.fallback_used, res.method))
        return (cx, cy), str(res.method)

    log("降级", "五层全部未命中 → 交给调用方的相对坐标兜底")
    return None, ""


def client_to_screen(hwnd: int, cx: int, cy: int) -> Tuple[int, int]:
    """客户区坐标 → 屏幕物理坐标。

    ★ 这个换算必须做对：
      客户区原点是 (边框, 标题栏高度)，不是窗口左上角。
      直接用窗口 rect 加偏移，会差出标题栏那 30 多个像素 ——
      点击落在搜索框上边缘甚至标题栏上，表现就是"点了没反应"。
    """
    from avatar import perception as P

    g = P.geometry(hwnd)
    # g.client 是 (left, top, w, h)，前两个就是客户区在屏幕上的原点
    ox, oy = g.client[0], g.client[1]
    return int(ox + cx), int(oy + cy)


# ==========================================================================
# 三、自动校准：在"大致区域"里找真正的控件矩形
# ==========================================================================
def _grab_client_screen(hwnd: int):
    """抓客户区的**屏幕区域**，返回 ``(bitmap, 客户区屏幕原点)``。

    ★ 为什么不用 ``grab_window``：
      QQ 是 Electron（Chromium GPU 合成），``PrintWindow`` / ``BitBlt``
      抓出来是**全黑**。必须先激活窗口，再抓它在屏幕上的那块区域。

    ★ 为什么抓"屏幕"而不是"窗口"：
      校准用的是**客户区坐标**，而 ``grab_screen`` 给的是屏幕坐标图；
      两者只差一个原点 ``g.client[:2]``，换算成本最低。
    """
    from avatar import perception as P

    g = P.geometry(hwnd)
    cl, ct, cw, ch = g.client
    shot = C.grab_screen((cl, ct, cw, ch))
    return shot, (cl, ct)


def _gray_rows(bmp, x0: int, y0: int, w: int, h: int, step: int = 2):
    """把位图的一块区域转成**灰度二维列表**（``y`` 从顶部算）。

    ★ 两个坑：
      1. ``Bitmap.pixels`` 是 DIB **自下而上**序 —— 第 0 行在缓冲区末尾，
         所以取第 ``y`` 行要写 ``(H - 1 - y)``。忘了这一步，检测出来的
         矩形会**上下颠倒**，而且看起来"有结果"，很难发现。
      2. ``step`` 隔点采样：300x100 的区域全采样是 3 万次像素读，
         step=2 降到 7500 次，纯 Python 也只要几毫秒。
    """
    px, stride, H = bmp.pixels, bmp.stride, bmp.height
    rows = []
    for y in range(0, h, step):
        row = []
        base = (H - 1 - (y0 + y)) * stride
        for x in range(0, w, step):
            o = base + (x0 + x) * 4
            b, g, r = px[o], px[o + 1], px[o + 2]
            # 整数版 Rec.601 灰度，比浮点快 3 倍且够用
            row.append((r * 299 + g * 587 + b * 114) // 1000)
        rows.append(row)
    return rows


# ★ 墨迹检测常量（2026-10 实测 QQ NT 深色主题 1640x1160 得出）
#   搜索框背景和页面背景灰度**完全相同**（32 vs 32）—— 色块法必失效；
#   但框内"搜索"占位文字 + 放大镜图标是亮墨迹（46~63，比背景亮 14+）。
#   按墨迹密度定位，背景同色也无所谓。标题栏文字同样有墨迹，
#   靠"带上方必须有足量空白行"排除（标题栏上面贴着窗口边框，没有空白）。
CALIB_INK_DELTA = 14                            # 墨迹判定：灰度 ≥ 背景众数 + 14
# ★ x 上限 0.30W 而不是 0.40W —— 实测 0.40W 会把"左栏右上角的 + 按钮"
#   和聊天区标题一起扫进来，列投影连成一条假带，点击点落在两个元素之间。
#   搜索框的占位文字/图标在左栏左侧 0~0.15W，0.30W 足够且更安全。
CALIB_SEARCH_SCAN = (0.00, 0.00, 0.30, 0.30)    # 搜索框扫描区：左栏顶部
CALIB_INPUT_SCAN = (0.35, 0.60, 1.00, 0.98)     # 输入框扫描区：右下角


def _find_ink_band(bmp, region, *, step: int = 4,
                   blank_ratio: float = 0.08, gap_rows: int = 2,
                   min_h: int = 24, max_h: int = 96,
                   above_blank_rows: int = 5, pick: str = "first"):
    """在 ``region`` 里找一条"墨迹带"，返回 ``(x, y, w, h)`` 或 None。

    ## 算法（纯 Python，步长采样，毫秒级）

    1. 灰度光栅扫描（:func:`_gray_rows`，step=4）
    2. 逐行算"墨迹率"：灰度 ≥ 背景众数+14 的像素占比；≥8% 视为内容行
    3. 内容行合并成带（相邻内容行允许 ≤2 行空白间隔）
    4. 带过滤：
       · 高度 min_h~max_h（搜索框 ≈56px，标题栏 ≈40px 靠下一条过滤）
       · **带上方必须有 ≥above_blank_rows 行连续空白** ——
         实测数据：搜索框带上方有 40px 空白 ✓，
         标题栏带上方只有 8~12px 空白（贴着窗口边框）✗
    5. 对带做列投影（该列墨迹行数 ≥2），取最长连续列段
       （允许 12px 小间断），段外接矩形即返回值

    ``pick="first"`` 取最上面的带（搜索框），
    ``pick="last"`` 取最下面的带（输入框工具栏图标行）。
    """
    x0, y0, rw, rh = region
    rows = _gray_rows(bmp, x0, y0, rw, rh, step=step)
    if not rows or not rows[0]:
        return None
    n_cols = len(rows[0])

    # ---- 1) 背景众数 + 墨迹阈值 ----
    counts = {}
    for row in rows:
        for v in row:
            counts[v] = counts.get(v, 0) + 1
    bg = max(counts.items(), key=lambda kv: kv[1])[0]
    ink_th = bg + CALIB_INK_DELTA

    # ---- 2) 行墨迹剖面 ----
    prof = []
    for row in rows:
        xs = [i for i, v in enumerate(row) if v >= ink_th]
        prof.append((len(xs) / n_cols >= blank_ratio, xs))

    # ---- 3) 内容行合并成带 ----
    bands, cur, gap = [], None, 0
    for yi, (is_ink, _xs) in enumerate(prof):
        if is_ink:
            if cur is None:
                cur = [yi, yi]
            else:
                cur[1] = yi
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > gap_rows:
                bands.append(cur)
                cur, gap = None, 0
    if cur is not None:
        bands.append(cur)

    def _band_ok(b):
        h_px = (b[1] - b[0] + 1) * step
        if h_px < min_h or h_px > max_h:
            return False
        blank = 0
        for yi in range(b[0] - 1, -1, -1):     # 向上数连续空白行
            if prof[yi][0]:
                break
            blank += 1
        return blank >= above_blank_rows

    ok = [b for b in bands if _band_ok(b)]
    if not ok:
        return None
    b = ok[0] if pick == "first" else ok[-1]

    # ---- 5) 列投影 → 最长连续墨迹列段 ----
    best_seg, seg, gapc = None, None, 0
    for xi in range(n_cols):
        n = sum(1 for yi in range(b[0], b[1] + 1) if rows[yi][xi] >= ink_th)
        if n >= 2:
            if seg is None:
                seg = [xi, xi]
            else:
                seg[1] = xi
            gapc = 0
        elif seg is not None:
            gapc += 1
            if gapc > 3:
                if best_seg is None or seg[1] - seg[0] > best_seg[1] - best_seg[0]:
                    best_seg = seg
                seg, gapc = None, 0
    if seg is not None and (best_seg is None or
                            seg[1] - seg[0] > best_seg[1] - best_seg[0]):
        best_seg = seg
    if not best_seg or best_seg[1] - best_seg[0] < 8:
        return None

    return (x0 + best_seg[0] * step, y0 + b[0] * step,
            (best_seg[1] - best_seg[0] + 1) * step,
            (b[1] - b[0] + 1) * step)


def _save_template(shot, rect, path: str) -> bool:
    """把检测到的矩形裁下来存成 PNG（供 ``--template-search`` 复用）。"""
    try:
        from avatar.vision.image_prep import encode_png
        x, y, w, h = rect
        tpl = shot.region(x, y, w, h)
        with open(path, "wb") as f:
            f.write(encode_png(tpl))
        return True
    except Exception:      # noqa: BLE001
        return False


def calibrate_search_box(hwnd: int, client_size: Tuple[int, int],
                         save_path: str = "") -> Optional[Tuple[int, int]]:
    """自动找搜索框中心（客户区坐标）。**失败返回 None，不抛。**

    流程：抓客户区 → 左栏顶部扫描 → 找"墨迹带" → 打印坐标。
    墨迹带 = 搜索框内的"搜索"占位文字 + 放大镜图标（亮像素团）。
    """
    w, h = client_size
    ax0, ay0, ax1, ay1 = CALIB_SEARCH_SCAN
    region = (max(0, int(w * ax0)), max(16, int(h * ay0)),
              int(w * ax1) - max(0, int(w * ax0)),
              int(h * ay1) - max(16, int(h * ay0)))
    log("校准", "搜索框扫描区：客户区 %s（左栏顶部 30%%x30%%）" % (region,))

    try:
        shot, _origin = _grab_client_screen(hwnd)
    except Exception as exc:      # noqa: BLE001
        log("校准", "截图失败：%s" % exc)
        return None

    rect = _find_ink_band(shot, region, pick="first")
    if not rect:
        log("校准", "未找到搜索框墨迹带（占位文字/图标缺失？或不在主面板）")
        return None

    x, y, bw, bh = rect
    # ★ 点击点取带内偏左：占位文字/图标在框内左侧，点文字上也能聚焦输入框，
    #   无需精确命中框边缘（深色主题下框边缘肉眼都难分辨）
    center = (x + int(bw * 0.45), y + bh // 2)
    log("校准", "墨迹带：x=%d y=%d w=%d h=%d" % (x, y, bw, bh))
    log("校准", "★ 自动找到的搜索框位置：(x, y) = (%d, %d)" % center)

    if save_path and _save_template(shot, rect, save_path):
        log("校准", "模板已存：%s（下次可 --template-search 复用）" % save_path)
    return center


def calibrate_input_box(hwnd: int, client_size: Tuple[int, int],
                        save_path: str = "") -> Optional[Tuple[int, int]]:
    """自动找消息输入框点击点（客户区坐标）。**失败返回 None，不抛。**

    思路：空输入框没有文字（没墨迹），但输入框**底部工具栏**的表情/
    截图/发送图标是亮墨迹团 —— 在右下角找**最下面**的墨迹带（=工具栏），
    点击它上方约 5%% 窗口高处 = 输入框编辑区中心。
    """
    w, h = client_size
    ax0, ay0, ax1, ay1 = CALIB_INPUT_SCAN
    region = (max(0, int(w * ax0)), max(0, int(h * ay0)),
              int(w * ax1) - max(0, int(w * ax0)),
              int(h * ay1) - max(0, int(h * ay0)))
    log("校准", "输入框扫描区：客户区 %s（右下角）" % (region,))

    try:
        shot, _origin = _grab_client_screen(hwnd)
    except Exception as exc:      # noqa: BLE001
        log("校准", "截图失败：%s" % exc)
        return None

    # 工具栏图标带：矮（12~60px）、上方空白要求放宽（编辑区本来就空）
    rect = _find_ink_band(shot, region, pick="last",
                          min_h=12, max_h=60, above_blank_rows=3)
    if not rect:
        log("校准", "未找到输入框工具栏图标带（会话可能没打开？）")
        return None

    x, y, bw, bh = rect
    # ★ 图标带贴着输入框底部：点击点 = 图标带上方 5% 窗口高 → 编辑区中心
    center = (max(region[0], x + bw // 2), max(region[1], y - int(h * 0.05)))
    log("校准", "工具栏图标带：x=%d y=%d w=%d h=%d" % (x, y, bw, bh))
    log("校准", "★ 自动找到的消息输入框位置：(x, y) = (%d, %d)" % center)

    if save_path and _save_template(shot, rect, save_path):
        log("校准", "模板已存：%s" % save_path)
    return center


# ==========================================================================
# 四、相对坐标兜底（自动校准也失败时的最后手段）
# ==========================================================================
def _load_bmp(path: str):
    """读一个 24 位 BMP 成 ``Bitmap``（零依赖，30 行）。

    ## 为什么需要这个

    模板匹配层要的是 **Bitmap 对象**，而项目只有"写 BMP"没有"读 BMP"。
    Demo 想展示模板层，就得自己读 —— 好在 BMP 是纯字节结构，
    解析比 PNG 简单一个数量级。

    ## 只支持本项目的产物

    本项目 ``avatar shot --out x.bmp`` 写的是 **24 位、自下而上、
    BI_RGB** 的 BMP。这里按这个格式读，够用。别的工具生成的
    32 位/压缩 BMP 不在支持范围（会抛 ValueError，不静默出错图）。

    ★ 关键点：BMP 像素是**自下而上**存的，而 Bitmap 要**按行正序**，
      所以读的时候要逐行倒着拷 —— 这一步忘了，模板图就是上下颠倒的，
      匹配永远失败，而且现象很迷惑（"图明明对，就是匹配不上"）。
    """
    import struct
    from avatar.vision.capture import Bitmap

    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("不是 BMP 文件：%s" % path)

    offset = struct.unpack_from("<I", data, 10)[0]
    w, h = struct.unpack_from("<ii", data, 18)
    bit_count = struct.unpack_from("<H", data, 28)[0]
    if bit_count != 24:
        raise ValueError("只支持 24 位 BMP，这个文件是 %d 位" % bit_count)
    if h < 0:      # 负高度 = 自上而下，直接按行正序读
        h = -h
        bottom_up = False
    else:
        bottom_up = True

    row_raw = w * 3
    pad = (4 - (row_raw % 4)) % 4
    stride = row_raw + pad

    pixels = bytearray(w * h * 4)
    for y in range(h):
        src_y = (h - 1 - y) if bottom_up else y
        s = offset + src_y * stride
        d = y * w * 4
        for x in range(w):
            b, g, r = data[s + x * 3], data[s + x * 3 + 1], data[s + x * 3 + 2]
            d2 = d + x * 4
            pixels[d2] = b
            pixels[d2 + 1] = g
            pixels[d2 + 2] = r
            pixels[d2 + 3] = 255
    return Bitmap(width=w, height=h, origin=(0, 0), pixels=pixels,
                  stride=w * 4)


def relative_point(client_size: Tuple[int, int],
                   rx: float, ry: float) -> Tuple[int, int]:
    """按客户区比例算点。

    ★ 为什么用比例而不是绝对坐标：
      绝对坐标在换分辨率、改窗口大小、换 DPI 后全废。
      比例坐标至少能跟着窗口走 —— 这是"没有模板图时"最稳的兜底。

    ``rx/ry`` 是 0~1 的比例。例如搜索框大约在客户区顶部
    横向 30%、纵向 38px 处。
    """
    w, h = client_size
    return int(w * rx), int(ry * h if ry <= 1.0 else ry)


# ==========================================================================
# 四、真人化输入
# ==========================================================================
def human_click(hwnd: int, cx: int, cy: int, *, why: str = "") -> None:
    """真人化点击一个**客户区**坐标。"""
    # ★ 点击前必须确认前台是 QQ —— 点错窗口比点不准更糟
    if not _foreground_is(hwnd):
        focus_qq(hwnd)
    sx, sy = client_to_screen(hwnd, cx, cy)
    log("执行", "点击%s → 屏幕 (%d, %d)" % (("（" + why + "）") if why else "",
                                            sx, sy))
    # ★ click_human 内部：贝塞尔轨迹 → 反应延迟 → 按下 → 按住 → 抬起
    HM.click_human(sx, sy, human=True, precision=1.2)
    time.sleep(0.25)


def human_type(text: str, hwnd: int = 0) -> None:
    """真人化打字。

    ★ typo_rate=0 是刻意的：
      Demo 发的是固定测试串，打错再回改会往输入框里塞退格，
      万一 QQ 把退格当成快捷键（有的版本会），消息就乱了。
      "真人感"由按键间隔和停顿提供，不需要真的打错。
    """
    # ★ 打字前确认前台是 QQ —— 往别的窗口打字是自动化最危险的事故
    #   （往浏览器地址栏打字再被回车，就跳到乱七八糟的网站去了）
    if hwnd and not _foreground_is(hwnd):
        focus_qq(hwnd)
    log("执行", "打字：%r（高斯间隔 + 词间停顿）" % text)
    HK.type_text_human(text, wpm=180, skill=1.0, typo_rate=0.0,
                       pause_at_end=True)
    time.sleep(0.3)


# ==========================================================================
# 五、发送结果验证
# ==========================================================================
def _ink_ratio(bmp) -> float:
    """估算一张图里"有内容的像素"占比。

    ★ 为什么不用 OCR 直接读文字：
      验证只需要回答"输入框空没空"，不需要知道里面写了什么。
      像素统计是零依赖、零误判（不会把"没识别出来"当成"发送成功"）的做法。

    算法：取左上角 4x4 的均色当背景，统计与背景差 > 28 的像素比例。
    """
    try:
        px = bmp.pixels
        w, h, stride = bmp.width, bmp.height, bmp.stride
        if not w or not h:
            return 0.0
        # 背景色：左上角采样
        rs = gs = bs = n = 0
        for y in range(0, min(4, h)):
            for x in range(0, min(4, w)):
                off = y * stride + x * 4
                b, g, r = px[off], px[off + 1], px[off + 2]
                rs += r; gs += g; bs += b; n += 1
        br, bg, bb = rs / n, gs / n, bs / n

        ink = 0
        total = 0
        step = 2          # 隔点采样，快 4 倍，精度足够
        for y in range(0, h, step):
            row = y * stride
            for x in range(0, w, step):
                off = row + x * 4
                b, g, r = px[off], px[off + 1], px[off + 2]
                if abs(r - br) + abs(g - bg) + abs(b - bb) > 84:
                    ink += 1
                total += 1
        return ink / max(1, total)
    except Exception:      # noqa: BLE001
        return 0.0


def grab_input_region(hwnd: int, client_size: Tuple[int, int]):
    """截取输入框区域（客户区下半部分）。

    ★ 用 grab_window(client_only=True) 而不是抓全屏：
      抓全屏会把别的窗口（包括 WorkBuddy 自己）一起抓进来，
      验证时被干扰。客户区截图干净得多。
    """
    w, h = client_size
    region = (0, int(h * 0.62), w, int(h * 0.36))
    return C.grab_window(hwnd, client_only=True).region(*region)


def verify_sent(hwnd: int, client_size: Tuple[int, int],
                before: float) -> bool:
    """验证"输入框被清空 = 发送成功"。

    ★ 判据设计：
      发送前输入框里有文字 → 墨迹占比高；
      发送后清空 → 墨迹占比显著下降。
      阈值取"下降到原来的 60% 以下"，容忍占位符文字和光标闪烁。
    """
    time.sleep(0.8)      # 等界面把消息渲染进聊天区、清空输入框
    try:
        after_bmp = grab_input_region(hwnd, client_size)
        after = _ink_ratio(after_bmp)
    except Exception as exc:      # noqa: BLE001
        log("验证", "截图失败，无法验证：%s" % exc)
        return False

    log("验证", "输入框墨迹占比：发送前 %.3f → 发送后 %.3f"
        % (before, after))
    if before > 0.02 and after < before * 0.6:
        log("验证", "✓ 输入框已清空 —— 消息应当已发出")
        return True
    if before <= 0.02:
        log("验证", "⚠ 发送前输入框几乎是空的 —— 可能没点中输入框")
        return False
    log("验证", "✗ 墨迹没有明显下降 —— 可能没发出去（回车没生效？）")
    return False


# ==========================================================================
# 六、主流程
# ==========================================================================
def run(friend: str, message: str, *, send: bool,
        auto_launch: bool = True,
        template_search: str = "") -> int:
    banner("Avatar × QQ 自动化 Showcase")
    log("模式", "dry-run（只定位不发送）" if not send else "真实发送")
    log("目标", "好友=%r  消息=%r" % (friend, message))

    # ---- 1) 找窗口 ----
    banner("① 附着窗口")
    win = ensure_qq_window(auto_launch=auto_launch)
    if not win:
        log("窗口", "失败：找不到 QQ。请先手动打开并登录 QQ，再重跑。")
        return 2
    if not focus_qq(win.hwnd):
        log("窗口", "⚠⚠ 多次尝试后前台仍不是 QQ —— 点击/打字可能落到别的窗口！")
        log("窗口", "⚠⚠ 建议手动点一下 QQ 窗口再跑，或关掉抢焦点的应用")

    w, h = win.client_size if win.client_size[0] > 0 else (win.rect[2], win.rect[3])
    log("窗口", "客户区 %dx%d" % (w, h))

    chain = build_chain()

    # ---- 2) 定位搜索框（五层链）----
    banner("② 定位搜索框（五层降级链）")
    # ★ region 限定在窗口顶部 —— 缩小搜索范围能显著提高 OCR/模板的命中率，
    #   也避免把聊天记录里的"搜索"两个字误当成搜索框。
    tmpl = None
    if template_search:
        try:
            tmpl = _load_bmp(template_search)
            log("定位", "已加载模板 %s（%dx%d）"
                % (template_search, tmpl.width, tmpl.height))
        except Exception as exc:      # noqa: BLE001
            log("定位", "模板加载失败（继续走其余层）：%s" % exc)

    pt, method = locate_with_chain(chain, "搜索", win.hwnd, (w, h),
                                   region=(0, 0, w, min(140, h // 5)),
                                   template=tmpl)

    # ★ 第二道：自动校准（在相对坐标附近"找矩形"）
    #   为什么加这道：五层链在 QQ NT 上基本全灭（UIA 封死、没模板），
    #   而固定相对坐标会随版本改布局而点偏（实测点到过"个性标签"）。
    #   自动校准不依赖任何配置：截一块区域，按"扁长纯色块"找出来。
    if pt is None:
        banner("②b 自动校准搜索框")
        tpl_path = os.path.join(tempfile.gettempdir(), "qq_search_auto.png")
        pt = calibrate_search_box(win.hwnd, (w, h), save_path=tpl_path)
        if pt is not None:
            method = "auto-calibrate"
        else:
            # ★ 第三道：固定相对坐标（最后手段，**必须大声警告**）
            pt = relative_point((w, h), 0.26, 0.030)
            method = "relative(fallback)"
            log("校准", "⚠⚠ 自动校准失败 → 回退固定坐标 (0.26, 0.030)")
            log("校准", "⚠⚠ 这个坐标历史上点偏过（点到个性标签）。"
                        "请用 avatar shot 截图人工核对！")
    sx, sy = client_to_screen(win.hwnd, *pt)
    log("定位", "搜索框 → 客户区 %s（%s）→ 屏幕 (%d, %d)"
        % (pt, method, sx, sy))

    if not send:
        # dry-run 只到"准备点击"，**一个字符都不发**
        log("执行", "dry-run：将点击搜索框并输入 %r" % friend)
        log("执行", "确认无误后加 --yes 真发")
        return 0

    # ---- 3) 搜索好友 ----
    banner("③ 搜索好友")
    human_click(win.hwnd, *pt, why="搜索框")
    human_type(friend, win.hwnd)
    log("执行", "回车确认搜索")
    from avatar.input import keyboard as K
    K.press("enter")
    time.sleep(1.6)          # 等搜索结果列表刷新

    # ---- 4) 点第一个搜索结果 ----
    banner("④ 打开会话")
    # ★ 这里也用相对坐标：搜索结果的第一个条目在列表顶部往下一点。
    #   为什么不五层链？因为"第一个结果"没有稳定的文字特征 ——
    #   它取决于你搜谁，模板匹配和 OCR 都没法给出通用答案。
    res_pt = relative_point((w, h), 0.30, 0.16)
    log("定位", "第一个搜索结果 → 客户区 %s（相对坐标，无通用文字特征）"
        % (res_pt,))
    human_click(win.hwnd, *res_pt, why="搜索结果")
    time.sleep(1.2)

    # ---- 5) 点输入框、打字 ----
    banner("⑤ 输入消息")
    # ★ 自动校准输入框：右下角候选区里找最大的扁长色块。
    #   原来写死 (0.52, 0.88)，窗口一改布局就点空。
    input_pt = calibrate_input_box(
        win.hwnd, (w, h),
        save_path=os.path.join(tempfile.gettempdir(), "qq_input_auto.png"))
    if input_pt is None:
        input_pt = relative_point((w, h), 0.52, 0.88)
        log("校准", "⚠⚠ 输入框自动校准失败 → 回退固定坐标 (0.52, 0.88)")
        log("校准", "⚠⚠ 可能点不中输入框，消息会打不进去。请人工核对！")
    log("定位", "消息输入框 → 客户区 %s" % (input_pt,))
    human_click(win.hwnd, *input_pt, why="输入框")

    # 发送前先记一个"墨迹基线"，用于发送后对比
    try:
        before = _ink_ratio(grab_input_region(win.hwnd, (w, h)))
        log("验证", "发送前墨迹基线：%.3f" % before)
    except Exception:      # noqa: BLE001
        before = 0.0

    human_type(message, win.hwnd)

    # ---- 6) 发送 + 验证 ----
    banner("⑥ 发送并验证")
    log("执行", "回车发送")
    K.press("enter")
    ok = verify_sent(win.hwnd, (w, h), before)

    banner("结果")
    if ok:
        log("结果", "✓ 完成 —— 消息已发出（输入框清空验证通过）")
        return 0
    log("结果", "⚠ 发送状态不确定 —— 请人工看一眼 QQ 窗口")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Avatar × QQ 自动化 Showcase（五层降级链 + 真人熵）")
    ap.add_argument("--friend", default="文件传输助手",
                    help="要搜索的好友名（默认：文件传输助手，发错也只是发给自己）")
    ap.add_argument("--message", default=DEFAULT_MESSAGE,
                    help="消息内容（默认：%s）" % DEFAULT_MESSAGE)
    ap.add_argument("--yes", action="store_true",
                    help="确认发送。不加这个参数只做 dry-run")
    ap.add_argument("--no-launch", action="store_true",
                    help="QQ 没开时不要尝试启动")
    ap.add_argument("--template-search", default="",
                    help="搜索框模板图（24 位 BMP）。给了才会启用模板匹配层")
    a = ap.parse_args(argv)

    try:
        return run(a.friend, a.message, send=bool(a.yes),
                   auto_launch=not a.no_launch,
                   template_search=a.template_search)
    except KeyboardInterrupt:
        print("\n中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
