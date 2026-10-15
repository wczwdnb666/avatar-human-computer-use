# -*- coding: utf-8 -*-
"""真人化鼠标：把 kinetics + tremor + submove 组合成可执行的移动。

分工
----
``avatar.input.mouse``
    只管"发事件"。瞬移、机械点击。
``本模块``
    只管"像人一样动"。决定轨迹、时序、节奏。
``avatar.input.dispatcher``
    决定走物理模式还是静默模式。

三层分离的好处：换真人化算法不动输入原语；换输入模式不动真人化。

公共 API
--------
``move_human(x, y)``        真人化移动（不点击）
``click_human(x, y)``       移动到目标 + 停顿 + 按下 + 抬起（含 hold 时长）
``double_click_human(x, y)`` 双击（含双击间隔）
``right_click_human(x, y)`` 右键
``drag_human(...)``         真人化拖拽（途中有速度变化与微停顿）
``scroll_human(...)``       真人化滚轮（分多次、间隔不均）
``hover_human(x, y)``       移动过去并停留（用于触发 hover 效果）

参数
----
所有 ``*_human`` 函数都有 ``human=False`` 开关，返回机械轨迹
用于对照实验（质量硬约束里要求"真人熵四个机制必须独立可调"）。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .. import input as input_layer
from ..errors import ActionFailed
from . import entropy, kinetics, submove, tremor, metrics

__all__ = [
    "MoveResult",
    "move_human",
    "hover_human",
    "click_human",
    "double_click_human",
    "right_click_human",
    "middle_click_human",
    "drag_human",
    "scroll_human",
    "sleep_human",
    "reaction_pause",
    "settle_time",
]

# 鼠标左键按下到抬起的真人时长分布（秒）
_CLICK_HOLD = (0.042, 0.115)
# 双击两次之间的间隔（秒）
_DOUBLE_GAP = (0.075, 0.165)


@dataclass
class MoveResult:
    """一次真人化移动的结果，供 ActionLog / 测试断言使用。"""

    start: Tuple[float, float]
    end: Tuple[float, float]
    requested: Tuple[float, float]
    distance: float
    duration: float
    points: int
    submovements: int
    landing_error: float
    peak_count: int = 0
    path_length: float = 0.0
    straightness: float = 0.0
    human: bool = True

    def describe(self) -> dict:
        return {
            "start": [round(self.start[0], 2), round(self.start[1], 2)],
            "end": [round(self.end[0], 2), round(self.end[1], 2)],
            "requested": [round(self.requested[0], 2), round(self.requested[1], 2)],
            "distance": round(self.distance, 2),
            "duration": round(self.duration, 4),
            "points": self.points,
            "submovements": self.submovements,
            "landing_error": round(self.landing_error, 4),
            "peak_count": self.peak_count,
            "straightness": round(self.straightness, 4),
            "human": self.human,
        }


# --------------------------------------------------------------------------
# 度量工具（内联，避免循环导入 metrics）
# --------------------------------------------------------------------------
def _path_metrics(segments) -> Tuple[float, float]:
    """返回 (路径总长, 直度)。

    直度 = 直线距离 / 路径长度。1.0 = 完全直线；
    真人指向动作通常在 0.92-0.995 之间。
    """
    if len(segments) < 2:
        return 0.0, 1.0

    total = 0.0
    for i in range(1, len(segments)):
        a = segments[i - 1]
        b = segments[i]
        total += math.hypot(b.x - a.x, b.y - a.y)

    straight = math.hypot(
        segments[-1].x - segments[0].x,
        segments[-1].y - segments[0].y,
    )
    if total <= 1e-9:
        return 0.0, 1.0
    return total, min(1.0, straight / total)


def _peak_count(segments) -> int:
    """数速度曲线峰值（按延迟归一化后的速度）。

    直接复用 ``metrics.count_peaks``，保证 ``MoveResult.peak_count``
    与 ``metrics.analyze_move().peak_count`` **永远一致** ——
    曾经两处各写一份，阈值和平滑窗口不同，同一个 plan 一个算出 1
    一个算出 4，导致"真人化判定失败"的假警报。
    """
    from . import metrics as _m

    if len(segments) < 4:
        return 0
    v = []
    for i in range(1, len(segments)):
        a = segments[i - 1]
        b = segments[i]
        d = math.hypot(b.x - a.x, b.y - a.y)
        dt = max(1e-4, b.delay)
        v.append(d / dt)
    return _m.count_peaks(v)


# --------------------------------------------------------------------------
# 移动
# --------------------------------------------------------------------------
def move_human(
    x: int,
    y: int,
    *,
    human: bool = True,
    precision: float = 1.0,
    from_pos: Optional[Tuple[int, int]] = None,
    max_segments: int = 3,
    dry_run: bool = False,
) -> MoveResult:
    """真人化移动到物理屏幕绝对坐标 ``(x, y)``。

    :param human: False → 走最小加加速度曲线、无震颤、无分段（对照组）
    :param precision: >1 表示高精度（小目标），会更慢、更多修正段
    :param from_pos: 起点。None = 用当前光标位置
    :param dry_run: True 时只规划不发送事件（用于测试与预览）
    """
    if from_pos is None:
        from_pos = input_layer.mouse.get_cursor_pos()

    x1, y1 = float(from_pos[0]), float(from_pos[1])
    x2, y2 = float(x), float(y)

    plan = submove.plan_movement(
        x1, y1, x2, y2,
        human=human, precision=precision, max_segments=max_segments,
    )

    # ---- 发送事件 ------------------------------------------------------
    #
    # ⚠️ 实测定下来的策略：**全程用绝对坐标**。
    #
    # 曾试过"首点绝对 + 其余相对"（相对移动能保留亚像素震颤细节），
    # 但相对移动的每次 dx/dy 都要取整，**误差会逐点累积** ——
    # 实测 61 个点累积出 3px 偏差，落点明显偏离目标。
    #
    # 绝对坐标每次都是"目标位置 vs 屏幕归一化"，不累积误差。
    # 代价是震颤细节会被 int() 量化掉一部分 —— 但 0.5-2px 的震颤
    # 在量化后仍保留大部分幅度，而 3px 的落点偏差是不可接受的。
    # 精度优先。
    if not dry_run:
        for seg in plan.segments:
            if seg.delay > 0:
                time.sleep(seg.delay)
            input_layer.mouse.move_absolute(int(round(seg.x)), int(round(seg.y)))

    path_len, straightness = _path_metrics(plan.segments)
    peaks = _peak_count(plan.segments)

    # 落点误差按**实际光标位置**算，不是按计划终点算 ——
    # 调用方关心的是"手真的停在哪"，计划值只是意图。
    if not dry_run:
        try:
            fx, fy = input_layer.mouse.get_cursor_pos()
        except Exception:
            fx, fy = (
                float(plan.segments[-1].x) if plan.segments else x2,
                float(plan.segments[-1].y) if plan.segments else y2,
            )
    else:
        fx, fy = (
            float(plan.segments[-1].x) if plan.segments else x2,
            float(plan.segments[-1].y) if plan.segments else y2,
        )
    landing = math.hypot(fx - x2, fy - y2)

    return MoveResult(
        start=(x1, y1),
        end=(fx, fy),
        requested=(x2, y2),
        distance=plan.distance,
        duration=plan.total_duration,
        points=len(plan.segments),
        submovements=len(plan.submovements),
        landing_error=landing,
        peak_count=peaks,
        path_length=path_len,
        straightness=straightness,
        human=human,
    )


def hover_human(
    x: int,
    y: int,
    *,
    dwell: Optional[float] = None,
    **kw,
) -> MoveResult:
    """移动过去并停留一会儿（触发 hover 提示）。"""
    res = move_human(x, y, **kw)
    if dwell is None:
        dwell = entropy.uniform(0.10, 0.42)
    if dwell > 0:
        time.sleep(dwell)
    return res


# --------------------------------------------------------------------------
# 点击
# --------------------------------------------------------------------------
def reaction_pause(*, base: float = 0.11, scale: float = 1.0) -> float:
    """按下之前的"反应"停顿。

    真人在"决定点这里"到"手真的按下"之间有一个约 80-200ms 的延迟。
    这遵循一个右偏（对数正态）分布 —— 偶尔会有很长的犹豫，
    但大多数时候很快。返回实际睡眠的秒数。
    """
    # 对数正态：median ≈ base，长尾在右侧
    mu = math.log(max(0.01, base))
    sigma = 0.42
    t = entropy.lognormvariate(mu, sigma) * scale
    t = min(0.9, max(0.03, t))
    time.sleep(t)
    return t


def settle_time(*, base: float = 0.06) -> float:
    """抬起鼠标键之后的"稳定"时间（防止连击被吃）。"""
    t = entropy.clamped_gauss(base, base * 0.4, 0.02, 0.35)
    time.sleep(t)
    return t


def click_human(
    x: int = None,
    y: int = None,
    *,
    button: str = "left",
    human: bool = True,
    precision: float = 1.0,
    pause: float = 0.0,
    hold: Optional[float] = None,
    from_pos: Optional[Tuple[int, int]] = None,
    dry_run: bool = False,
) -> MoveResult:
    """真人化点击：移动 → 反应停顿 → 按下 → 按住 → 抬起。

    :param pause: 到位后、按下之前的额外停顿（None 表示用反应停顿）
    :param hold: 按住时长；None 时从 ``_CLICK_HOLD`` 随机取
    """
    if (x is None) != (y is None):
        raise ActionFailed("click_human 的 x/y 必须同时给出或同时省略", method="mouse")

    if x is not None:
        res = move_human(
            x, y, human=human, precision=precision,
            from_pos=from_pos, dry_run=dry_run,
        )
        # 落点用实际终点（含 0.3-1.5px 漂移）
        tx = int(round(res.end[0]))
        ty = int(round(res.end[1]))
    else:
        cur = input_layer.mouse.get_cursor_pos()
        res = MoveResult(
            start=cur, end=cur, requested=cur,
            distance=0.0, duration=0.0, points=0,
            submovements=0, landing_error=0.0, human=human,
        )
        tx = ty = None

    if dry_run:
        return res

    # 反应停顿
    if pause > 0:
        time.sleep(pause)
    elif human:
        reaction_pause()

    # 按下
    input_layer.mouse.mouse_down(button)

    # 按住时长
    if hold is None:
        if human:
            hold = entropy.uniform(*_CLICK_HOLD)
        else:
            hold = 0.02
    if hold > 0:
        time.sleep(hold)

    input_layer.mouse.mouse_up(button)

    if human:
        settle_time()

    return res


def double_click_human(
    x: int = None,
    y: int = None,
    *,
    button: str = "left",
    human: bool = True,
    precision: float = 1.0,
    gap: Optional[float] = None,
    from_pos: Optional[Tuple[int, int]] = None,
    dry_run: bool = False,
) -> MoveResult:
    """真人化双击。

    关键细节：**两次点击之间光标几乎不动**（真人第二次点击的落点通常
    与第一次相差 0-2 px）。如果这里重新跑一次 ``move_human``，第二次
    会带新的落点漂移，可能落到相邻元素上 —— 所以第二次是原地点击。
    """
    res = click_human(
        x, y, button=button, human=human, precision=precision,
        from_pos=from_pos, dry_run=dry_run,
    )
    if dry_run:
        return res

    if gap is None:
        gap = entropy.uniform(*_DOUBLE_GAP) if human else 0.08
    time.sleep(gap)

    # 第二次：原地（微小 0-2px 重新定位，模拟真人的第二次修正）
    if human:
        cur = input_layer.mouse.get_cursor_pos()
        jx = int(round(cur[0] + entropy.uniform(-1.2, 1.2)))
        jy = int(round(cur[1] + entropy.uniform(-1.2, 1.2)))
        input_layer.mouse.move_absolute(jx, jy)
        time.sleep(entropy.uniform(0.008, 0.030))

    input_layer.mouse.mouse_down(button)
    time.sleep(entropy.uniform(*_CLICK_HOLD) if human else 0.02)
    input_layer.mouse.mouse_up(button)

    if human:
        settle_time()

    return res


def right_click_human(x: int = None, y: int = None, **kw) -> MoveResult:
    """真人化右键单击。"""
    kw.setdefault("button", "right")
    return click_human(x, y, **kw)


def middle_click_human(x: int = None, y: int = None, **kw) -> MoveResult:
    """真人化中键单击。"""
    kw.setdefault("button", "middle")
    return click_human(x, y, **kw)


# --------------------------------------------------------------------------
# 拖拽
# --------------------------------------------------------------------------
def drag_human(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    button: str = "left",
    human: bool = True,
    precision: float = 1.0,
    hold_before: Optional[float] = None,
    hold_after: Optional[float] = None,
    mid_pauses: int = 0,
    dry_run: bool = False,
) -> List[MoveResult]:
    """真人化拖拽：移动到位 → 按住 → 分段移动（带速度变化）→ 抬起。

    与 ``input.mouse.drag`` 的区别：那里是等速直线；这里是分段非对称
    曲线 + 震颤 + 中途可能的停顿（"拖到一半检查一下"）。

    返回各段移动的结果（第一段是到起点的移动）。
    """
    results: List[MoveResult] = []

    # 1. 移动到起点
    r0 = move_human(x1, y1, human=human, precision=precision, dry_run=dry_run)
    results.append(r0)
    if dry_run:
        return results

    cur_x = int(round(r0.end[0]))
    cur_y = int(round(r0.end[1]))

    # 2. 按住前的停顿（真人会先按住、确认"抓住"了再拖）
    if hold_before is None:
        hold_before = entropy.uniform(0.055, 0.155) if human else 0.05
    if human:
        reaction_pause(base=0.09)
    time.sleep(hold_before)
    input_layer.mouse.mouse_down(button)

    try:
        # 3. 分段移动：把整段拖拽拆成 1-3 个子段
        n_sub = 1 if not human else entropy.randint(1, 3)
        if n_sub <= 0:
            n_sub = 1

        pts: List[Tuple[int, int]] = []
        for i in range(1, n_sub + 1):
            t = i / n_sub
            # 在中途加横向摆动
            sway = 0.0
            if human and i < n_sub:
                sway = entropy.gauss(0.0, min(6.0, abs(x2 - x1) * 0.008 + 2.0))
            px = int(round(x1 + (x2 - x1) * t + sway))
            py = int(round(y1 + (y2 - y1) * t))
            pts.append((px, py))

        for i, (px, py) in enumerate(pts):
            seg2 = move_human(
                px, py, human=human, precision=precision,
                from_pos=(cur_x, cur_y),
                max_segments=2 if human else 1,
                dry_run=False,
            )
            results.append(seg2)
            cur_x, cur_y = int(round(seg2.end[0])), int(round(seg2.end[1]))

            # 中途微停顿
            if human and i < len(pts) - 1 and entropy.random() < 0.30:
                time.sleep(entropy.uniform(0.04, 0.18))

        for _ in range(int(mid_pauses)):
            time.sleep(entropy.uniform(0.08, 0.25))

        # 4. 到位后的停顿（让目标响应 hover 高亮）
        if hold_after is None:
            hold_after = entropy.uniform(0.06, 0.20) if human else 0.05
        time.sleep(hold_after)
    finally:
        # 无论中途出什么岔子都必须抬起按键，
        # 否则鼠标卡在"按住"状态，后续所有操作都变成拖拽
        input_layer.mouse.mouse_up(button)

    if human:
        settle_time(base=0.08)

    return results


# --------------------------------------------------------------------------
# 滚轮
# --------------------------------------------------------------------------
def scroll_human(
    amount: int,
    *,
    x: int = None,
    y: int = None,
    human: bool = True,
    precision: float = 1.0,
    dry_run: bool = False,
) -> int:
    """真人化滚轮。

    真人滚动特征：
        - 分多次，每次 1-3 个 WHEEL_DELTA（不是一次性滚到底）
        - 每次间隔 25-140ms，间隔不均匀
        - 偶尔反向修正（滚过头了往回一点）—— 概率 12%
        - 总量会有 ±8% 的偏差（真人不会精确滚 N 格）

    返回实际发出的 WHEEL_DELTA 总量。
    """
    if x is not None and y is not None:
        move_human(x, y, human=human, precision=precision, dry_run=dry_run)

    if dry_run:
        return amount

    if not human:
        input_layer.mouse.scroll(amount, steps=max(1, abs(amount) // 120))
        return amount

    direction = 1 if amount > 0 else -1
    target = abs(amount)

    # 总量偏差 ±8%
    target = int(round(target * entropy.uniform(0.92, 1.08)))

    step_unit = 120
    emitted = 0
    steps_taken = 0
    max_steps = 60

    while emitted < target and steps_taken < max_steps:
        # 每次滚 1-3 格
        n = entropy.choices([1, 2, 3], weights=[0.35, 0.42, 0.23], k=1)[0]
        delta = direction * n * step_unit
        if emitted + abs(delta) > target:
            delta = direction * max(step_unit, target - emitted)

        input_layer.mouse.scroll(delta, steps=1)
        emitted += abs(delta)
        steps_taken += 1

        # 不均匀间隔
        gap = entropy.clamped_gauss(0.075, 0.035, 0.022, 0.30)
        time.sleep(gap)

        # 12% 概率滚过头往回一点
        if entropy.random() < 0.12 and steps_taken < max_steps:
            back = -direction * step_unit
            input_layer.mouse.scroll(back, steps=1)
            emitted -= step_unit
            time.sleep(entropy.uniform(0.05, 0.16))
            steps_taken += 1

    if human:
        settle_time(base=0.05)

    return emitted


# --------------------------------------------------------------------------
# 暂停
# --------------------------------------------------------------------------
def sleep_human(
    *,
    base: float = 0.25,
    sigma_ratio: float = 0.35,
    low: float = 0.03,
    high: float = 2.0,
) -> float:
    """人类化的"思考"停顿。截断高斯，避免出现极长或极短的值。"""
    t = entropy.clamped_gauss(base, base * sigma_ratio, low, high)
    time.sleep(t)
    return t


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """鼠标真人化自检（**只做规划，不动真实光标**）。

    这个模块的公开接口大多会真的操作鼠标，所以自检里刻意只调
    **纯计算**的部分：轨迹规划与指标。实机操作由
    ``tests/test_phase3_dispatcher.py`` 覆盖，那里会做光标还原。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = entropy.get_seed()
    try:
        # -- 路径规划（不落盘、不动光标） -------------------------------
        entropy.seed(7)
        plan = submove.plan_movement(100, 100, 700, 500)
        _add("路径规划产出采样点", len(plan.segments) > 10,
             "%d 个点" % len(plan.segments))
        _add("路径距离为正", plan.distance > 0, "%.2f px" % plan.distance)
        _add("总时长为正", plan.total_duration > 0,
             "%.4f s" % plan.total_duration)

        # -- 指标方向：真人 vs 机械 -------------------------------------
        hm = metrics.analyze_move(plan)
        entropy.seed(7)
        rp = submove.plan_movement(100, 100, 700, 500, human=False)
        rm = metrics.analyze_move(rp)
        _add("真人峰值数 >= 2", hm.peak_count >= 2, "%d" % hm.peak_count)
        _add("机械峰值数 == 1", rm.peak_count == 1, "%d" % rm.peak_count)
        _add("真人直度 < 1", hm.straightness < 1.0, "%.5f" % hm.straightness)
        _add("机械直度 == 1", rm.straightness == 1.0, "%.5f" % rm.straightness)

        # -- 模块公开接口存在 -------------------------------------------
        missing = [n for n in ("move_human", "click_human", "drag_human",
                               "scroll_human")
                   if not callable(globals().get(n))]
        _add("公开接口齐全", not missing, "缺 %s" % missing if missing else "4 个入口")
        _add("MoveResult 可构造",
             isinstance(MoveResult, type), "dataclass")

        # -- 可复现 -----------------------------------------------------
        entropy.seed(31337)
        a = [(s.x, s.y) for s in submove.plan_movement(100, 100, 800, 600).segments]
        entropy.seed(31337)
        b = [(s.x, s.y) for s in submove.plan_movement(100, 100, 800, 600).segments]
        _add("规划可复现", a == b, "%d 个点" % len(a))

    finally:
        if saved is None:
            entropy.reset()
        else:
            entropy.seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
