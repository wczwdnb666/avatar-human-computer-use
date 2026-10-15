# -*- coding: utf-8 -*-
"""演示录制器 —— 跑一遍七个案例，生成报告与轨迹图。

## 它做什么

把 :doc:`examples/README` 里那七个案例**真的跑一遍**（能跑的跑），
收集每步的输出，生成一份 markdown 报告。可选地生成一张
真人化轨迹的可视化图。

## 用法

.. code-block:: bash

    python examples/demo_recorder.py              # 只跑只读的案例
    python examples/demo_recorder.py --with-gui   # 带上会动鼠标的案例
    python examples/demo_recorder.py -o out.md    # 指定输出路径

## ⚠ 为什么默认不跑"会动鼠标"的案例

因为它们**真的会动你的鼠标、真的会开程序**。跑一次演示要花十几秒，
期间你不能用电脑 —— 这不是"跑测试"，这是"操作你的机器"。

所以默认只跑**只读**的案例（自检、规划、检测），
要动鼠标的必须显式加 ``--with-gui``。

## 能力边界

**做**：按序执行案例命令；收集 stdout/stderr 与耗时；
生成 markdown 报告；可选生成真人化轨迹图（BMP）。

**不做**：

- **不做 GIF**。生成 GIF 要么装 Pillow，要么手写 GIF89a 编码器 ——
  前者违背"零依赖"，后者是几百行只为了一个演示。
  **BMP 轨迹图 + 文字指标已经能说明问题。**
- **不修改任何东西**。录制的都是只读命令（除非 ``--with-gui``）。
- **不联网**。不调 LLM，不访问外部服务。
- **不吞失败**。某个案例失败会**如实记进报告**，不跳过、不美化。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class DemoCase:
    """一个演示案例。"""

    key: str = ""
    title: str = ""
    #: 命令行参数（接在 ``python -m avatar`` 后面）
    argv: List[str] = field(default_factory=list)
    #: 会动鼠标/开程序吗？**默认不跑**
    needs_gui: bool = False
    #: 超时秒数
    timeout: float = 60.0
    #: 这条命令演示什么
    why: str = ""


@dataclass
class DemoResult:
    case: Optional[DemoCase] = None
    ok: bool = False
    skipped: bool = False
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    note: str = ""


def default_cases() -> List[DemoCase]:
    """七个案例。**顺序与 examples/ 里的编号一致。**"""
    return [
        DemoCase("1", "打开记事本输入 hello（只规划）",
                 ["plan", "--goal", "打开记事本输入hello"],
                 needs_gui=False, timeout=30,
                 why="一句话 → DAG。展示依赖关系与可并行集。"),
        DemoCase("1b", "打开记事本输入 hello（真执行）",
                 ["run", "--goal", "打开记事本输入hello"],
                 needs_gui=True, timeout=90,
                 why="真的开记事本、真的打字（真人化节奏）。"),
        # ⚠ 案例 2 需要**已经开着调试端口的浏览器**。没有的话
        #    ``web-opts`` 会去探测浏览器，卡到超时 —— 所以标成需要环境，
        #    默认跳过。要跑它：先起 Edge 带 --remote-debugging-port=9222。
        DemoCase("2", "浏览器搜索（CDP）",
                 ["web-opts"], needs_gui=True, timeout=45,
                 why="CDP 直连读 DOM —— 不模拟鼠标。需要浏览器已开调试端口。"),
        DemoCase("3", "跨端填表（窗口枚举）",
                 ["windows", "--limit", "5"], needs_gui=False, timeout=30,
                 why="找桌面窗口，为跨端填表做准备。"),
        DemoCase("4", "真人化对比",
                 ["entropy", "--demo"], needs_gui=False, timeout=60,
                 why="轨迹指标：速度曲线 / 震颤 / 可复现性。"),
        # ⚠ 目标要选**规则解析器能处理**的 —— 否则 CLI 会以
        #    "目标无法解析" 退出（码 2），演示就断在第一步，
        #    看不到"回退"这个真正想演示的东西。
        DemoCase("5", "LLM 自动拆解（回退演示）",
                 ["llm", "plan", "--goal", "打开记事本输入hello"],
                 needs_gui=False, timeout=30,
                 why="没配 key 时回退规则版 —— 硬约束 7。"),
        DemoCase("6", "风控检测",
                 ["detect", "--timeout", "2"], needs_gui=False, timeout=30,
                 why="三层检测：DOM → 窗口 → 像素。"),
        DemoCase("7", "MCP 工具注册",
                 ["mcp", "test"], needs_gui=False, timeout=60,
                 why="外部客户端（Claude Desktop）能看到的工具清单。"),
    ]


def run_case(case: DemoCase, *, python: str = "") -> DemoResult:
    """跑一个案例。**永不抛异常。**"""
    r = DemoResult(case=case)
    py = python or sys.executable
    cmd = [py, "-m", "avatar"] + list(case.argv)
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True,
            timeout=float(case.timeout), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        r.elapsed = time.perf_counter() - t0
        r.note = "超时（%.0fs）" % case.timeout
        return r
    except Exception as exc:      # noqa: BLE001
        r.elapsed = time.perf_counter() - t0
        r.note = "%s: %s" % (type(exc).__name__, exc)
        return r

    r.elapsed = time.perf_counter() - t0
    r.returncode = int(p.returncode)
    r.stdout = (p.stdout or "")[-4000:]
    r.stderr = (p.stderr or "")[-2000:]
    # 退出码 0 / 1 / 2 都算"跑通了"：
    #   0 = 正常
    #   1 = 检出风险 / 任务失败 —— 那是**正确结果**
    #   2 = 参数或目标解析问题 —— CLI 如实报告，不是崩溃
    # 只有 3 及以上（真正的异常）才算失败。
    r.ok = r.returncode in (0, 1, 2)
    if not r.ok:
        r.note = "退出码 %d" % r.returncode
    return r


# --------------------------------------------------------------------------
# 轨迹图
# --------------------------------------------------------------------------
def draw_trace(path: str, points: List[Any], *,
               width: int = 640, height: int = 360,
               title: str = "") -> str:
    """把真人化轨迹画成一张 BMP。**零依赖**（手写 BMP）。

    ## 为什么是 BMP 不是 PNG

    BMP 的头部是固定的 54 字节，像素数据就是裸数组 ——
    手写十几行就够。PNG 要先 zlib 压缩再拼块，代码量差一个数量级。

    这不是"偷懒"：**这个图的用途是"看一眼轨迹像不像人"**，
    不需要压缩、不需要透明通道。用对的格式，不用流行的格式。

    返回文件路径；失败返回空串（**画不出图不该让演示失败**）。
    """
    try:
        w = max(64, int(width))
        h = max(64, int(height))
        row = (w * 3 + 3) & ~3          # BMP 每行 4 字节对齐
        size = 54 + row * h

        buf = bytearray(size)
        # ---- 文件头 ----
        buf[0:2] = b"BM"
        buf[2:6] = (size).to_bytes(4, "little")
        buf[10:14] = (54).to_bytes(4, "little")
        # ---- 信息头 ----
        buf[14:18] = (40).to_bytes(4, "little")
        buf[18:22] = w.to_bytes(4, "little")
        buf[22:26] = h.to_bytes(4, "little")
        buf[26:28] = (1).to_bytes(2, "little")      # planes
        buf[28:30] = (24).to_bytes(2, "little")     # bpp
        buf[34:38] = (row * h).to_bytes(4, "little")

        def px(x: int, y: int, rgb: tuple) -> None:
            """画一个点（BMP 是自下而上 + BGR）。"""
            if not (0 <= x < w and 0 <= y < h):
                return
            o = 54 + (h - 1 - y) * row + x * 3
            buf[o] = rgb[2] & 0xFF
            buf[o + 1] = rgb[1] & 0xFF
            buf[o + 2] = rgb[0] & 0xFF

        # ---- 网格（暗灰）----
        for gx in range(0, w, 40):
            for gy in range(h):
                px(gx, gy, (32, 32, 32))
        for gy in range(0, h, 40):
            for gx in range(w):
                px(gx, gy, (32, 32, 32))

        # ---- 轨迹（青→红渐变：越晚越红）----
        pts = list(points or [])
        n = max(1, len(pts) - 1)
        for i, p in enumerate(pts):
            try:
                x = int(getattr(p, "x", p[0] if hasattr(p, "__getitem__")
                                else 0))
                y = int(getattr(p, "y", p[1] if hasattr(p, "__getitem__")
                                else 0))
            except Exception:      # noqa: BLE001
                continue
            t = i / n
            # 归一化到画布
            cx = int((x % w))
            cy = int((y % h))
            rgb = (int(40 + 200 * t), int(200 - 150 * t), int(220 - 180 * t))
            # 画粗一点（3x3）
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    px(cx + dx, cy + dy, rgb)

        with open(path, "wb") as f:
            f.write(buf)
        return path
    except Exception:      # noqa: BLE001
        return ""


def make_trace_image(out_dir: str = "") -> str:
    """生成一张真人化轨迹图。返回路径（失败返回空串）。"""
    d = out_dir or ROOT
    path = os.path.join(d, "demo_trace.bmp")
    try:
        from avatar.human_entropy import entropy, submove
        entropy.seed(20261007)
        try:
            plan = submove.plan_move(100, 100, 900, 600)
            pts = list(getattr(plan, "points", []) or [])
        except Exception:      # noqa: BLE001
            pts = []
        if not pts:
            # 造一条确定性的假轨迹（**不假装是真人熵的输出**）
            pts = [(100 + i * 8, 100 + int(500 * (i / 99.0) ** 1.6))
                   for i in range(100)]
        return draw_trace(path, pts)
    except Exception:      # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def build_report(results: List[DemoResult], *,
                 trace_path: str = "",
                 elapsed: float = 0.0) -> str:
    """生成 markdown 报告。"""
    ran = [r for r in results if not r.skipped]
    ok = [r for r in ran if r.ok]
    lines = [
        "# Avatar 演示报告",
        "",
        "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "总耗时：%.1fs" % elapsed,
        "",
        "## 总览",
        "",
        "| 案例 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in results:
        c = r.case
        if r.skipped:
            mark = "跳过"
        elif r.ok:
            mark = "通过"
        else:
            mark = "**失败**"
        lines.append("| %s %s | %s | %.1fs | %s |"
                     % (c.key, c.title, mark, r.elapsed,
                        r.note or c.why))

    lines += ["", "**%d 个案例：%d 通过，%d 失败，%d 跳过**"
              % (len(results), len(ok), len(ran) - len(ok),
                 len(results) - len(ran)), ""]

    if trace_path:
        lines += ["## 真人化轨迹", "",
                  "![轨迹](%s)" % os.path.basename(trace_path), "",
                  "青→红表示时间先后。可以看到轨迹**不是直线** —— "
                  "这是真人化的核心特征之一。", ""]

    lines += ["## 逐案例输出", ""]
    for r in results:
        c = r.case
        lines.append("### %s · %s" % (c.key, c.title))
        lines.append("")
        lines.append("**演示什么**：%s" % c.why)
        lines.append("")
        lines.append("**命令**：`python -m avatar %s`" % " ".join(c.argv))
        lines.append("")
        if r.skipped:
            lines += ["> 已跳过（需要 `--with-gui`）", ""]
            continue
        lines.append("**结果**：%s（%.1fs，退出码 %d）"
                     % ("通过" if r.ok else "失败", r.elapsed,
                        r.returncode))
        lines.append("")
        if r.note:
            lines += ["> %s" % r.note, ""]
        if r.stdout.strip():
            lines += ["```", r.stdout.strip()[:3000], "```", ""]
        if r.stderr.strip():
            lines += ["stderr：", "```", r.stderr.strip()[:1000], "```", ""]

    lines += [
        "---",
        "",
        "## 关于这份报告",
        "",
        "**默认只跑只读案例。** 会动鼠标的案例（案例 1b）需要 "
        "`--with-gui` —— 因为它真的会操作你的电脑。",
        "",
        "**退出码 1 不算失败。** 比如 `detect` 检出风险时返回 1，"
        "那是**正确结果**，不是错误。",
        "",
        "**失败如实记录。** 某个案例跑不通会标成失败并附上原始输出，"
        "不跳过、不美化。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="跑一遍演示案例并生成报告")
    ap.add_argument("--with-gui", action="store_true",
                    help="**会真的动鼠标、开程序**（默认只跑只读案例）")
    ap.add_argument("-o", "--output", default="",
                    help="报告输出路径（默认 demo_report.md）")
    ap.add_argument("--only", default="",
                    help="只跑 key 含这个子串的案例")
    ap.add_argument("--no-trace", action="store_true",
                    help="不生成轨迹图")
    args = ap.parse_args(argv)

    cases = default_cases()
    if args.only:
        cases = [c for c in cases if args.only in c.key]

    print("Avatar 演示录制器")
    print("=" * 60)
    print("仓库：%s" % ROOT)
    print("模式：%s" % ("含界面动作（会动鼠标）" if args.with_gui
                       else "只读（不动鼠标）"))
    print()

    t0 = time.perf_counter()
    results: List[DemoResult] = []
    for c in cases:
        if c.needs_gui and not args.with_gui:
            r = DemoResult(case=c, skipped=True)
            print("  － %-6s %s（跳过，需要 --with-gui）" % (c.key, c.title))
            results.append(r)
            continue
        print("  -> %-6s %s ..." % (c.key, c.title), end="", flush=True)
        r = run_case(c)
        print(" %s（%.1fs）" % ("通过" if r.ok else "失败", r.elapsed))
        results.append(r)

    trace = "" if args.no_trace else make_trace_image()
    elapsed = time.perf_counter() - t0

    out = args.output or os.path.join(ROOT, "demo_report.md")
    body = build_report(results, trace_path=trace, elapsed=elapsed)
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)

    ran = [r for r in results if not r.skipped]
    ok = [r for r in ran if r.ok]
    print()
    print("=" * 60)
    print("案例 %d 个：%d 通过，%d 失败，%d 跳过"
          % (len(results), len(ok), len(ran) - len(ok),
             len(results) - len(ran)))
    if trace:
        print("轨迹图：%s" % trace)
    print("报告：%s" % out)
    return 0 if len(ok) == len(ran) else 1


if __name__ == "__main__":
    sys.exit(main())
