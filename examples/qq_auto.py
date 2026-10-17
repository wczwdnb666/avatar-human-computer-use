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
def find_qq_window() -> Optional[W.WindowInfo]:
    """找 QQ 主窗口。找不到返回 None（**不抛**，让上层决定要不要启动）。

    ★ 为什么按进程名而不是标题找：
      QQ 的窗口标题会随会话变（"QQ" / "张三" / "群聊(3)"），
      按标题匹配是自找麻烦；进程名稳定得多。
    """
    for proc in QQ_PROCESS_NAMES:
        try:
            wins = W.find_windows(process=proc, usable_only=False)
        except Exception:      # noqa: BLE001
            continue
        # 过滤掉 0x0 的隐藏窗口（QQ 有一堆不可见的辅助窗口）
        for w in wins:
            if w.rect[2] > 200 and w.rect[3] > 200 and not w.minimized:
                return w
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
# 三、相对坐标兜底（QQ 封死 UIA 时的现实方案）
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
    sx, sy = client_to_screen(hwnd, cx, cy)
    log("执行", "点击%s → 屏幕 (%d, %d)" % (("（" + why + "）") if why else "",
                                            sx, sy))
    # ★ click_human 内部：贝塞尔轨迹 → 反应延迟 → 按下 → 按住 → 抬起
    HM.click_human(sx, sy, human=True, precision=1.2)
    time.sleep(0.25)


def human_type(text: str) -> None:
    """真人化打字。

    ★ typo_rate=0 是刻意的：
      Demo 发的是固定测试串，打错再回改会往输入框里塞退格，
      万一 QQ 把退格当成快捷键（有的版本会），消息就乱了。
      "真人感"由按键间隔和停顿提供，不需要真的打错。
    """
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
    if not activate(win.hwnd):
        log("窗口", "⚠ 激活失败（可能被前台锁挡住），继续尝试…")

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
    if pt is None:
        # 兜底：相对坐标。QQ NT 的搜索框固定在左上区域。
        pt = relative_point((w, h), 0.26, 0.030)
        method = "relative(0.26, 0.030)"
        log("降级", "fallback_used=True  reason=五层未命中，改用相对坐标")
        log("降级", "若位置不准，请手动截图该区域做模板，见文件末尾说明")
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
    human_type(friend)
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
    input_pt = relative_point((w, h), 0.52, 0.88)
    log("定位", "消息输入框 → 客户区 %s（相对坐标：底部中间）" % (input_pt,))
    human_click(win.hwnd, *input_pt, why="输入框")

    # 发送前先记一个"墨迹基线"，用于发送后对比
    try:
        before = _ink_ratio(grab_input_region(win.hwnd, (w, h)))
        log("验证", "发送前墨迹基线：%.3f" % before)
    except Exception:      # noqa: BLE001
        before = 0.0

    human_type(message)

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
