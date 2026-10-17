# -*- coding: utf-8 -*-
"""Agent Presence 可视化逻辑 —— 颜色、动画、过渡、tooltip、节流、提示音。

## 这一层解决什么

GUI（:mod:`avatar_shell.ball`）要画的东西，拆成**纯函数**放这里：

```
presence.color(state)          → (r, g, b)          颜色
presence.alpha_at(state, t)    → 0~255              呼吸/闪烁的透明度
presence.lerp_color(a, b, t)   → (r, g, b)          渐变中间色
presence.format_tooltip(...)   → str                tooltip 两行文字
presence.RateLimiter           → 类                  每秒最多 2 次刷新
presence.ALERT_SOUND           → (freq, ms, times)   needs_human 提示音节奏
```

**为什么拆出来**：颜色对不对、渐变够不够 200ms、tooltip 格式对不对，
这些全是**纯逻辑**，能直接测。窗口渲染要真桌面，测不了。
把能测的和不能测的分开 —— 项目一贯的做法。

## 性能约束（硬要求）

动画**不能定时重绘整个窗口**。悬浮球是 48x48 的 UpdateLayeredWindow
逐像素图，每帧重绘要几十万次像素写。所以：

- **画圆只画一次**（初始化时）
- 动画只改 **alpha 通道**（`SetLayeredWindowAttributes`，一次系统调用）
- 状态切换的 200ms 颜色渐变才重绘（约 12 帧，有限次数）

## 能力边界

**做**：状态→颜色映射；alpha 呼吸/闪烁计算（正弦）；
颜色线性插值；过渡进度（200ms）；tooltip 文本格式化；
刷新频率限制（默认 2Hz）；提示音节奏参数（频率/时长/次数）；
状态标签。

**不做**：

- **不碰任何 Windows API**。本模块纯计算，零 ctypes。
  渲染在 :mod:`avatar_shell.ball`，声音在 ball 里调 ``winsound``。
- **不做缓动曲线**。呼吸是正弦，渐变是线性 —— 够用，
  且这两个都有明确周期（0.8s / 0.4s / 200ms），缓动会让周期不可控。
- **不管字体、字号、布局**。tooltip 只给**文本**，
  怎么画是 tooltip 窗口的事。
- **不持久化任何状态**。
- **不 import avatar**（硬约束 2）。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .state import (
    STATE_ANIMATION,
    STATE_COLORS,
    STATE_LABELS,
    AgentState,
)

__all__ = [
    "ALERT_SOUND",
    "TRANSITION_MS",
    "IDLE_ALPHA",
    "PULSE_MIN_ALPHA",
    "PULSE_MAX_ALPHA",
    "FLASH_ALPHA",
    "TOOLTIP_SIZE",
    "TOOLTIP_DELAY_MS",
    "HOTKEY_COMBO",
    "color",
    "label",
    "is_animated",
    "animation_period",
    "alpha_at",
    "lerp_color",
    "transition_progress",
    "transition_color",
    "format_tooltip",
    "format_progress",
    "RateLimiter",
    "describe",
    "selfcheck",
]

# ======================================================================
# 常量
# ======================================================================

#: needs_human 提示音：``(频率 Hz, 时长 ms, 次数)``。
#:
#: 880Hz 是 A5 —— 清晰但不刺耳。连续 3 次短音是"有人找你"的通用节奏
#:（和手机的提醒通知同思路）。**只响 needs_human**，别的状态不响 ——
#: 声音是最强的打扰，滥用会让人关掉整个提示。
ALERT_SOUND: Tuple[int, int, int] = (880, 200, 3)

#: 状态切换的颜色渐变时长（毫秒）。
#:
#: 200ms 的来历：短于 100ms 感知不到渐变（像突变），
#: 长于 300ms 显得拖沓（尤其是 running→needs_human 这种紧急切换）。
TRANSITION_MS = 200

#: 静止状态（idle / error）的 alpha —— 满不透明。
IDLE_ALPHA = 255

#: running 呼吸的 alpha 范围（0~255）。
#: 0.45~1.0：呼吸要"看得出来"但不能暗到像熄灭了。
PULSE_MIN_ALPHA = 115
PULSE_MAX_ALPHA = 255

#: needs_human 闪烁的 alpha 值（在 0 和 255 之间硬切）。
#: 0.4s 周期 + 满幅切换 = 最显眼。**这是唯一允许硬切的状态** ——
#: 渐变会削弱"紧急"感。
FLASH_ALPHA = 255

#: tooltip 窗口尺寸（像素）。
TOOLTIP_SIZE: Tuple[int, int] = (300, 60)

#: 鼠标移开后 tooltip 延迟消失（毫秒）。
#:
#: 500ms 的来历：鼠标划过球时可能短暂离开又回来（比如从边缘擦过），
#: 立刻消失再出现会闪。留 500ms 容错，又不至于"移开了还赖着"。
TOOLTIP_DELAY_MS = 500

#: 全局唤起热键：``(修饰键, 虚拟键码)``。
#:
#: Ctrl+Alt+A —— 不撞 Windows 自带组合（Win+* 和 Ctrl+Alt+Del 除外），
#: A 是左手小指+拇指最顺的位置。
HOTKEY_COMBO: Tuple[int, int] = (0x0002 | 0x0001, 0x41)

#: 进度刷新的最小间隔（秒）—— 每秒最多 2 次。
#:
#: 为什么限频：任务事件可能密集（每步 3~4 个事件），
#: 不节流的话悬浮球会高频重画 → 视觉上"发抖"，
#: 而且白白占 CPU。2Hz 对人眼足够（状态变化本来就是秒级的事）。
MIN_REFRESH_INTERVAL = 0.5


# ======================================================================
# 颜色与动画
# ======================================================================
def color(state: str) -> Tuple[int, int, int]:
    """状态 → (r, g, b)。不认识的状态回灰色（= idle 的颜色）。

    ⚠ 不抛异常 —— 本模块所有函数都不抛，调用方（GUI 线程）不能被
    一个坏状态值打死。
    """
    try:
        return STATE_COLORS.get(str(state), STATE_COLORS[AgentState.IDLE])
    except Exception:      # noqa: BLE001
        return (128, 128, 128)


def label(state: str) -> str:
    """状态 → 中文标签。"""
    try:
        return STATE_LABELS.get(str(state), str(state))
    except Exception:      # noqa: BLE001
        return str(state)


def is_animated(state: str) -> bool:
    """该状态是否有动画。"""
    try:
        return bool(STATE_ANIMATION.get(str(state), (False, 0.0))[0])
    except Exception:      # noqa: BLE001
        return False


def animation_period(state: str) -> float:
    """该状态的动画周期（秒）。静止状态返回 0。"""
    try:
        return float(STATE_ANIMATION.get(str(state), (False, 0.0))[1])
    except Exception:      # noqa: BLE001
        return 0.0


def alpha_at(state: str, t: float) -> int:
    """时刻 ``t``（``time.monotonic()`` 的秒）时该状态的 alpha（0~255）。

    ## 三种节奏

    - **idle / error** —— 恒定 255。**不闪**：
      error 闪了像"正在处理"，idle 闪了像"有事"。
      静止是这两种状态的语义。
    - **running** —— 正弦呼吸，周期 0.8s，alpha 在
      ``[PULSE_MIN_ALPHA, PULSE_MAX_ALPHA]`` 之间平滑起伏。
      "活着，但不催你"。
    - **needs_human** —— 周期 0.4s 的满幅闪烁。**唯一抢注意力的状态**。
      在 0 和 255 之间硬切（正弦取符号），闪得干脆。
    """
    try:
        st = str(state)
        if not is_animated(st):
            return IDLE_ALPHA
        period = animation_period(st)
        if period <= 0:
            return IDLE_ALPHA
        phase = (t % period) / period          # 0~1
        if st == AgentState.NEEDS_HUMAN:
            # 闪烁：前半周期亮、后半周期暗（硬切，不要渐变）
            return FLASH_ALPHA if phase < 0.5 else 0
        # running：余弦呼吸。``(1 - cos(2π·phase)) / 2`` —— 每个周期内
        # 从最暗(phase=0)平滑升到最亮(phase=0.5)再落回最暗(phase=1)，
        # 对称、无跳变，是标准的"呼吸"曲线。
        wave = (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0
        lo, hi = PULSE_MIN_ALPHA, PULSE_MAX_ALPHA
        return int(round(lo + (hi - lo) * wave))
    except Exception:      # noqa: BLE001
        return IDLE_ALPHA


def lerp_color(a: Tuple[int, int, int],
               b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    """两个颜色之间线性插值。``t`` 钳制到 [0, 1]。"""
    try:
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else float(t))
        return (int(round(a[0] + (b[0] - a[0]) * t)),
                int(round(a[1] + (b[1] - a[1]) * t)),
                int(round(a[2] + (b[2] - a[2]) * t)))
    except Exception:      # noqa: BLE001
        return tuple(b)     # type: ignore[return-value]


def transition_progress(started_at: float,
                        now: Optional[float] = None) -> float:
    """状态切换的渐变进度（0~1）。超过 200ms 恒为 1（渐变结束）。

    ``started_at`` 是 ``time.monotonic()`` 时间戳。
    """
    try:
        now = time.monotonic() if now is None else float(now)
        elapsed_ms = (now - float(started_at)) * 1000.0
        if elapsed_ms <= 0:
            return 0.0
        return 1.0 if elapsed_ms >= TRANSITION_MS else elapsed_ms / TRANSITION_MS
    except Exception:      # noqa: BLE001
        return 1.0


def transition_color(old: str, new: str, started_at: float,
                     now: Optional[float] = None) -> Tuple[int, int, int]:
    """状态切换**当前该画什么颜色** —— 渐变 200ms 后到终点色。"""
    try:
        t = transition_progress(started_at, now)
        if t >= 1.0:
            return color(new)
        return lerp_color(color(old), color(new), t)
    except Exception:      # noqa: BLE001
        return color(new)


# ======================================================================
# tooltip 文本
# ======================================================================
def format_progress(step: Optional[int], total: Optional[int],
                    running: bool = True) -> str:
    """进度行文本。

    - 在跑且有步数 → ``步骤 3/7``
    - 在跑但没步数 → ``进行中``
    - 不在跑 → ``空闲``
    """
    try:
        if not running:
            return "空闲"
        if step is not None and total:
            return "步骤 %d/%d" % (int(step), int(total))
        return "进行中"
    except Exception:      # noqa: BLE001
        return "空闲"


def format_tooltip(task_name: str, step: Optional[int], total: Optional[int],
                   *, running: bool = True,
                   waiting_human: bool = False) -> str:
    """tooltip 的两行文本（``\\n`` 分隔）。

    第一行：任务名（截断到 24 字）。
    第二行：进度；needs_human 时换成等人提示。

    ⚠ needs_human 的提示语写在这里而不是 ball 里 ——
    它是**内容**，不是渲染。
    """
    try:
        name = (str(task_name or "").strip() or "未命名任务")[:24]
        if waiting_human:
            return "%s\n等待人工介入（按 F12 恢复）" % name
        return "%s\n%s" % (name, format_progress(step, total, running))
    except Exception:      # noqa: BLE001
        return "未命名任务\n空闲"


# ======================================================================
# 频率限制
# ======================================================================
class RateLimiter:
    """刷新频率限制器。默认每秒最多 2 次。

    用法::

        rl = RateLimiter()
        if rl.allow():        # True 才真正重画
            ball.repaint()

    **为什么需要**：任务事件密集时（每步 3~4 个），
    不节流悬浮球会高频重画 —— 视觉上发抖，CPU 也白烧。
    """

    def __init__(self, min_interval: float = MIN_REFRESH_INTERVAL) -> None:
        self.min_interval = max(0.01, float(min_interval))
        self._last = 0.0
        self._dropped = 0
        self._allowed = 0

    def allow(self, now: Optional[float] = None) -> bool:
        """现在允许刷新吗？**消耗一次额度**（不允许时也计入丢弃）。"""
        try:
            now = time.monotonic() if now is None else float(now)
            if now - self._last >= self.min_interval:
                self._last = now
                self._allowed += 1
                return True
            self._dropped += 1
            return False
        except Exception:      # noqa: BLE001
            return False

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def allowed(self) -> int:
        return self._allowed

    def stats(self) -> Dict[str, int]:
        return {"allowed": self._allowed, "dropped": self._dropped}

    def reset(self) -> None:
        self._last = 0.0
        self._dropped = 0
        self._allowed = 0


# ======================================================================
# 描述
# ======================================================================
def describe() -> str:
    """可视化配置的说明（自检和文档用）。"""
    lines = [
        "Agent Presence 可视化配置",
        "=" * 56,
        "颜色：",
    ]
    for st in (AgentState.IDLE, AgentState.RUNNING,
               AgentState.NEEDS_HUMAN, AgentState.ERROR):
        r, g, b = color(st)
        lines.append("  %-12s #%02X%02X%02X  动画=%s  周期=%.1fs"
                     % (st, r, g, b,
                        "呼吸" if st == AgentState.RUNNING
                        else ("闪烁" if st == AgentState.NEEDS_HUMAN
                              else "静止"),
                        animation_period(st)))
    lines += [
        "",
        "渐变：%dms（颜色过渡）" % TRANSITION_MS,
        "提示音：%dHz / %dms x %d（仅 needs_human）" % ALERT_SOUND,
        "tooltip：%dx%d，移开 %dms 消失"
        % (TOOLTIP_SIZE[0], TOOLTIP_SIZE[1], TOOLTIP_DELAY_MS),
        "刷新限频：每 %.1fs 最多 1 次（2Hz）" % MIN_REFRESH_INTERVAL,
        "热键：Ctrl+Alt+A",
    ]
    return "\n".join(lines)


# ======================================================================
# 自检
# ======================================================================
def selfcheck(quick: bool = True) -> dict:
    """presence 可视化逻辑的自检。**纯计算，quick 档 < 0.1s。**

    覆盖：颜色映射、alpha 呼吸/闪烁的数值特征、渐变时长、
    tooltip 格式、限频器行为、提示音节奏。
    **不测窗口渲染** —— 那要真桌面。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) 颜色映射（用户给的精确值）============
    _add("★ idle 是灰色 0x808080", color("idle") == (128, 128, 128),
         "#%02X%02X%02X" % color("idle"))
    _add("★ running 是绿色 0x22C55E", color("running") == (34, 197, 94),
         "#%02X%02X%02X" % color("running"))
    _add("★ needs_human 是橙色 0xF97316", color("needs_human") == (249, 115, 22),
         "#%02X%02X%02X" % color("needs_human"))
    _add("★ error 是红色 0xEF4444", color("error") == (239, 68, 68),
         "#%02X%02X%02X" % color("error"))
    _add("不认识的状态回灰色", color("不存在的状态") == (128, 128, 128),
         "不抛")
    _add("颜色永不抛", color(None) == (128, 128, 128), "None 也不抛")

    # ============ 2) 动画周期 ============
    _add("★ running 周期 0.8s", animation_period("running") == 0.8,
         "%.1fs" % animation_period("running"))
    _add("★ needs_human 周期 0.4s", animation_period("needs_human") == 0.4,
         "%.1fs" % animation_period("needs_human"))
    _add("idle 不闪", is_animated("idle") is False, "静止")
    _add("error 不闪（闪了像在处理）", is_animated("error") is False, "静止")

    # ============ 3) alpha 呼吸/闪烁 ============
    # running：正弦呼吸，值域 [PULSE_MIN, PULSE_MAX]，且周期正确
    lo, hi = PULSE_MIN_ALPHA, PULSE_MAX_ALPHA
    run_samples = [alpha_at("running", i * 0.8 / 24) for i in range(24)]
    _add("★ running 呼吸值在范围内",
         all(lo <= v <= hi for v in run_samples),
         "%d~%d" % (min(run_samples), max(run_samples)))
    _add("★ running 呼吸确实有起伏（非恒定）",
         max(run_samples) - min(run_samples) > 30,
         "起伏 %d" % (max(run_samples) - min(run_samples)))
    _add("★ running 呼吸周期是 0.8s",
         alpha_at("running", 0.0) == alpha_at("running", 0.8)
         and alpha_at("running", 0.4) == alpha_at("running", 1.2),
         "0.0/0.8 同相，0.4/1.2 同相")
    _add("★ 呼吸起点是最暗、中点是最亮",
         alpha_at("running", 0.0) == lo
         and alpha_at("running", 0.4) == hi,
         "t=0 暗 %d，t=0.4 亮 %d" % (lo, hi))
    _add("呼吸四分之一处在中点",
         abs(alpha_at("running", 0.2) - (lo + hi) // 2) <= 1,
         "t=0.2 ≈ 中点")
    # needs_human：0.4s 硬切闪烁
    flash = [alpha_at("needs_human", i * 0.4 / 8) for i in range(8)]
    _add("★ needs_human 闪烁只取 0 或 255",
         set(flash) <= {0, FLASH_ALPHA}, str(sorted(set(flash))))
    _add("★ needs_human 周期 0.4s",
         alpha_at("needs_human", 0.0) == alpha_at("needs_human", 0.4)
         and alpha_at("needs_human", 0.2) != alpha_at("needs_human", 0.0),
         "0.0/0.4 同相，0.0/0.2 反相")
    # 静止状态
    _add("★ idle alpha 恒 255", alpha_at("idle", 0.0) == 255
         and alpha_at("idle", 123.0) == 255, "静止")
    _add("★ error alpha 恒 255", alpha_at("error", 0.0) == 255
         and alpha_at("error", 123.0) == 255, "静止")
    _add("alpha 永不抛", isinstance(alpha_at("乱写", 1.0), int), "坏状态也返回 int")

    # ============ 4) 渐变 ============
    _add("★ 渐变时长 200ms", TRANSITION_MS == 200, "%dms" % TRANSITION_MS)
    t_start = 1000.0
    _add("渐变起点是旧色", transition_color("running", "idle", t_start,
                                            t_start) == color("running"),
         "t=0 → 旧色")
    _add("渐变终点是新色", transition_color("running", "idle", t_start,
                                            t_start + 0.3) == color("idle"),
         "t>200ms → 新色")
    _add("渐变中途在两者之间",
         lerp_color(color("running"), color("idle"), 0.5)
         == transition_color("running", "idle", t_start, t_start + 0.1),
         "t=100ms ≈ 中点")
    _add("★ 过渡进度钳制到 [0,1]",
         transition_progress(t_start, t_start - 5) == 0.0
         and transition_progress(t_start, t_start + 99) == 1.0, "钳制")
    _add("lerp 两端精确",
         lerp_color((0, 0, 0), (255, 255, 255), 0.0) == (0, 0, 0)
         and lerp_color((0, 0, 0), (255, 255, 255), 1.0) == (255, 255, 255),
         "t=0/1 不动点")
    _add("lerp 钳制越界 t",
         lerp_color((0, 0, 0), (255, 255, 255), 2.0) == (255, 255, 255),
         "t=2 → 终点")

    # ============ 5) tooltip 格式 ============
    _add("★ tooltip 第一行任务名第二行进度",
         format_tooltip("打开记事本输入hello", 3, 7)
         == "打开记事本输入hello\n步骤 3/7", repr(format_tooltip("打开记事本输入hello", 3, 7)))
    _add("空闲时第二行是「空闲」",
         format_tooltip("打开记事本", None, None, running=False)
         == "打开记事本\n空闲", "空闲")
    _add("在跑但没步数 → 进行中",
         format_tooltip("整理桌面", None, None, running=True)
         == "整理桌面\n进行中", "进行中")
    _add("★ needs_human 的提示语",
         "等待人工介入" in format_tooltip("清理下载", 1, 3,
                                          waiting_human=True),
         "带 F12 提示")
    _add("空任务名有兜底",
         format_tooltip("", 1, 2) == "未命名任务\n步骤 1/2", "未命名任务")
    _add("超长任务名截断到 24 字",
         len(format_tooltip("超" * 40, 1, 2).splitlines()[0]) == 24,
         "24 字")
    _add("tooltip 尺寸 300x60", TOOLTIP_SIZE == (300, 60),
         "%dx%d" % TOOLTIP_SIZE)
    _add("tooltip 移开 500ms 消失", TOOLTIP_DELAY_MS == 500,
         "%dms" % TOOLTIP_DELAY_MS)

    # ============ 6) 频率限制 ============
    rl = RateLimiter(min_interval=0.5)
    now = 1000.0
    _add("★ 第一次允许", rl.allow(now) is True, "allowed=1")
    _add("★ 间隔内拒绝", rl.allow(now + 0.1) is False, "dropped=1")
    _add("★ 间隔后允许", rl.allow(now + 0.5) is True, "allowed=2")
    _add("默认 2Hz（间隔 0.5s）", MIN_REFRESH_INTERVAL == 0.5,
         "%.1fs" % MIN_REFRESH_INTERVAL)
    _add("统计口径正确",
         rl.stats() == {"allowed": 2, "dropped": 1}, str(rl.stats()))
    rl2 = RateLimiter()
    _add("reset 清空", (rl2.allow(100.0), rl2.allow(100.1), rl2.reset(),
                        rl2.allow(100.0))[3] is True, "reset 后立刻允许")
    _add("RateLimiter 永不抛",
         RateLimiter(min_interval=-1).min_interval >= 0.01, "坏参数钳制")

    # ============ 7) 提示音 ============
    _add("★ 提示音 880Hz / 200ms / 3 次", ALERT_SOUND == (880, 200, 3),
         str(ALERT_SOUND))
    _add("★ 热键是 Ctrl+Alt+A",
         HOTKEY_COMBO == (0x0003, 0x41), "MOD_CONTROL|MOD_ALT=0x3, VK_A=0x41")

    # ============ 8) docstring ============
    import sys as _sys
    _me = _sys.modules[__name__]
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "presence",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe())
