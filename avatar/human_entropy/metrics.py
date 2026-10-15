# -*- coding: utf-8 -*-
"""真人度度量：怎么判定一条轨迹"像人"。

三个核心指标
------------
1. **速度峰值数 (peak_count)**
   真人指向动作的速度曲线有 2 个及以上峰值（主运动 + 修正运动）。
   机器人生成的单段曲线只有 1 个。阈值：≥2 判为"像人"。

2. **路径直度 (straightness)**
   = 直线距离 / 实际路径长度。真人 0.92-0.995 —— **不是 1.0**。
   完全直线（=1.0）是机器特征；但太低（<0.85）也不像人（那是不受控的抖动）。

3. **落点误差 (landing_error)**
   真人点击不会落在同一像素。误差应 < 2px（否则说明瞄不准），
   但 > 0（否则说明是机械定位）。

辅助指标
--------
- **速度不对称度 (asymmetry)**：加速段时长 / 减速段时长。< 1 才像人。
- **震颤能量 (tremor_energy)**：8-12 Hz 频带内的能量占比。
- **时序熵 (timing_entropy)**：延迟序列的熵。均匀分布熵值最高但不自然；
  真人介于"完全规律"和"完全随机"之间（对数正态）。
- **暂停次数 (pause_count)**：整段动作中的停顿次数（犹豫）。

用法
----
::

    from avatar.human_entropy import metrics

    report = metrics.analyze_move(segments)     # 从 PlanSegment 列表
    report = metrics.analyze_segments(...)      # 从 (x,y,delay) 三元组
    print(report.describe())

    # 对比
    metrics.compare(human_plan, robot_plan)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import entropy

__all__ = [
    "MoveMetrics",
    "analyze_move",
    "analyze_segments",
    "analyze_timing",
    "tremor_energy",
    "timing_entropy",
    "compare",
    "PASS_CRITERIA",
    "verdict",
]


#: 判定"像人"的阈值。测试直接引用这些常量，避免两处硬编码。
PASS_CRITERIA = {
    "min_peak_count": 2,          # 速度峰值数 ≥ 2
    "max_landing_error": 2.0,     # 落点误差 < 2px
    "min_straightness": 0.85,     # 路径直度 ≥ 0.85
    #: 直度上限。= 1.0 是完全笔直，属于机器特征。
    #:
    #: ⚠️ 这里用**严格小于**而不是"小于 0.9999"，是踩过坑之后改的。
    #: 原来的阈值 0.9999 在小数第四位上做判断，而长距离的直线移动
    #: 天然就能做到 0.99992 —— 仅仅因为"路径绕了 0.14px"就判失败。
    #: 真正的分界线只有一条：路径长度是否**恰好等于**直线距离。
    #: 用 1.0 做上限，误差留给了浮点本身，语义也干净：
    #: "只要路径不是数学意义上的直线，就算过关"。
    "max_straightness": 1.0,
    "max_asymmetry": 0.98,        # 加速段不能长于减速段
    "min_landing_error": 0.0,     # 允许 0（原地点击）
}


@dataclass
class MoveMetrics:
    """一条轨迹的真人度指标。"""

    points: int = 0
    distance: float = 0.0
    path_length: float = 0.0
    duration: float = 0.0

    # 核心三指标
    peak_count: int = 0
    straightness: float = 1.0
    landing_error: float = 0.0

    # 辅助
    asymmetry: float = 1.0
    tremor_energy: float = 0.0
    timing_entropy: float = 0.0
    pause_count: int = 0
    max_speed: float = 0.0
    mean_speed: float = 0.0
    speed_cv: float = 0.0
    submovement_count: int = 0

    def describe(self) -> Dict[str, Any]:
        return {
            "points": self.points,
            "distance": round(self.distance, 2),
            "path_length": round(self.path_length, 2),
            "duration": round(self.duration, 4),
            "peak_count": self.peak_count,
            "straightness": round(self.straightness, 4),
            "landing_error": round(self.landing_error, 4),
            "asymmetry": round(self.asymmetry, 4),
            "tremor_energy": round(self.tremor_energy, 4),
            "timing_entropy": round(self.timing_entropy, 4),
            "pause_count": self.pause_count,
            "max_speed": round(self.max_speed, 2),
            "mean_speed": round(self.mean_speed, 2),
            "speed_cv": round(self.speed_cv, 4),
            "submovement_count": self.submovement_count,
        }


# --------------------------------------------------------------------------
# 基础序列工具
# --------------------------------------------------------------------------
def _as_points(segments) -> List[Tuple[float, float, float]]:
    """把各种输入统一成 (x, y, delay) 列表。"""
    out: List[Tuple[float, float, float]] = []
    for s in segments:
        if isinstance(s, (list, tuple)) and len(s) >= 2:
            x = float(s[0])
            y = float(s[1])
            d = float(s[2]) if len(s) >= 3 else 0.0
            out.append((x, y, d))
        else:
            x = float(getattr(s, "x"))
            y = float(getattr(s, "y"))
            d = float(getattr(s, "delay", 0.0))
            out.append((x, y, d))
    return out


def _velocities(pts: Sequence[Tuple[float, float, float]]) -> List[float]:
    v = []
    for i in range(1, len(pts)):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        dist = math.hypot(dx, dy)
        dt = max(1e-4, pts[i][2])
        v.append(dist / dt)
    return v


def _savgol_smooth(values: Sequence[float], window: int = 5) -> List[float]:
    """简单的移动平均平滑（去数值噪声，避免数出假峰）。

    窗口自动收缩到奇数且不超过序列长度。
    """
    n = len(values)
    if n < 3:
        return list(values)
    w = min(window, n if n % 2 == 1 else n - 1)
    if w < 3:
        return list(values)
    if w % 2 == 0:
        w -= 1
    half = w // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


# --------------------------------------------------------------------------
# 峰值
# --------------------------------------------------------------------------
def count_peaks(
    values: Sequence[float],
    *,
    min_prominence: float = 0.10,
    smooth: int = 2,
    min_gap_ratio: float = 0.04,
) -> int:
    """数序列中的峰值个数。

    判定标准是**真正的"突起"**：一个候选峰必须在两侧各找到一个
    比它低 ``min_prominence``（相对**它自己**）的谷，才算一个峰。

    :param min_prominence: 显著性阈值，**相对于候选峰自身的高度**。

        ⚠️ 这一点改了两轮，两个坑都记在这里：

        **坑一：光看"是不是局部极大"不够，必须看两侧的谷。**
        机械轨迹在**减速尾段**的数值抖动会把某个点拱高一点，形式上
        确实是局部极大 —— 但两侧根本没有谷，序列整体还在单调下滑。
        只看局部极大，一条纯粹的 minimum-jerk 单段曲线会被数出 2 个峰，
        "真人度"指标直接废掉。

        **坑二：阈值不能相对"全局最大峰"。** 原来写的是
        ``thr = max(v) * 0.08`` —— 这在长距离上会**漏掉真实的次峰**。
        实测 720px 真人轨迹：主峰 8300 px/s，修正峰 559 px/s，
        两侧谷分别是 1 和 0。559 是个**货真价实**的峰（两侧落差 558），
        但它相对主峰只有 0.067 < 0.08，被绝对门槛筛掉了 ——
        40 个种子里漏 8 个。

        正确判据是**峰相对自身的落差**：``(val − 谷) >= val * 0.10``。
        这样 559 的峰要求落差 ≥ 55.9，实际落差 558，轻松通过；
        而机械尾段的假鼓包（例如 1.6 相对邻值 1.0/1.2）落差太小，
        照样被拒。换成这个判据后四种行程 160 条真人轨迹零漏判。

    :param smooth: 平滑窗口（0 = 不平滑）。默认 **2**，不是 4 ——
        窗口越大，短促的次峰越容易被邻域平均抹平。实测（40 seed/行程）:

        ========== ====== ====== ======
        行程        s=0    s=2    s=4
        ========== ====== ====== ======
        短距 40px   0/40   0/40   7/40
        中短 160px  3/40   3/40  13/40
        长距 721px  8/40   8/40  12/40
        对角 1698px 10/40  10/40  11/40
        ========== ====== ====== ======

        （表中数字是"漏判条数"，越小越好；8/40 与 10/40 那两列
        的问题由上面的坑二解决，换成相对判据后全部归零。）
        窗口 4 在 4 条行程上都明显更差，所以取 2。

    :param min_gap_ratio: 两个峰之间至少相隔序列长度的这个比例，
        用来滤掉同一个峰上的相邻抖动
    """
    v = list(values)
    if smooth and len(v) >= 3:
        v = _savgol_smooth(v, smooth)
    if len(v) < 3:
        return 0

    peak = max(v)
    if peak <= 0:
        return 0

    # 候选峰：局部极大（**不**在这里用全局相对阈值筛 —— 见"坑二"，那样
    # 会把长距离上的真实次峰一并筛掉，改到下面按各自高度判显著性）
    cands = []
    for i in range(1, len(v) - 1):
        if v[i] > v[i - 1] and v[i] >= v[i + 1]:
            cands.append((i, v[i]))

    if not cands:
        return 1 if peak > 0 else 0

    # ---- 校验：每个候选峰两侧都必须有"够低的谷" --------------------------
    # 左侧只看**滑动谷**：从峰值向左走，记录最低点，但一旦遇到
    # 比当前峰还高的点就停下（说明那不是谷，是上一座山的坡）。
    valid = []
    for idx, val in cands:
        left_low = val
        for j in range(idx - 1, -1, -1):
            if v[j] > val:
                break
            left_low = min(left_low, v[j])
        right_low = val
        for j in range(idx + 1, len(v)):
            if v[j] > val:
                break
            right_low = min(right_low, v[j])
        # 关键：显著性按**该峰自身高度**的比例判（见"坑二"）
        keep = val * min_prominence
        if (val - left_low) >= keep and (val - right_low) >= keep:
            valid.append((idx, val))

    if not valid:
        # 没有任何候选峰有实质性的突起 —— 退化为单峰
        return 1

    # 合并过近的峰：同簇里取最高的那个
    gap = max(1, int(len(v) * min_gap_ratio))
    merged = []
    for idx, val in valid:
        if merged and idx - merged[-1][0] < gap:
            if val > merged[-1][1]:
                merged[-1] = (idx, val)
        else:
            merged.append((idx, val))

    return max(1, len(merged))


def count_pauses(
    pts: Sequence[Tuple[float, float, float]],
    *,
    threshold: float = 0.04,
    min_pause: int = 1,
) -> int:
    """数停顿次数：延迟显著大于中位数、且位移接近 0。"""
    delays = [p[2] for p in pts if p[2] > 0]
    if not delays:
        return 0
    delays_sorted = sorted(delays)
    median = delays_sorted[len(delays_sorted) // 2]
    limit = max(threshold, median * 2.6)

    n = 0
    for i in range(1, len(pts)):
        d = pts[i][2]
        move = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        if d >= limit and move < 1.2:
            n += 1
    return n


# --------------------------------------------------------------------------
# 不对称度
# --------------------------------------------------------------------------
def compute_asymmetry(values: Sequence[float]) -> float:
    """计算速度曲线的加速段/减速段时长比。

    以速度峰值为分割点：峰值之前的采样点数 / 峰值之后的采样点数。
    < 1 表示加速段短于减速段（真人特征）。
    """
    v = _savgol_smooth(values, 5) if len(values) >= 5 else list(values)
    if len(v) < 3:
        return 1.0
    peak = max(v)
    if peak <= 0:
        return 1.0
    idx = v.index(peak)
    before = max(1, idx)
    after = max(1, len(v) - 1 - idx)
    return before / after


# --------------------------------------------------------------------------
# 震颤能量
# --------------------------------------------------------------------------
def tremor_energy(
    pts: Sequence[Tuple[float, float, float]],
    *,
    band: Tuple[float, float] = (7.0, 13.0),
) -> float:
    """估算 8-12 Hz 频带的能量占比。

    做法：把轨迹到拟合直线的**法向残差**看成时间序列，做简化的
    离散傅里叶（只算目标频带），用频带能量 / 总能量作为指标。

    为什么用残差而不是原始坐标：原始坐标的主要成分是"慢速位移"
    （低频、大能量），会淹没震颤。法向残差把位移趋势剔除了。
    """
    if len(pts) < 8:
        return 0.0

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]

    # 最小二乘拟合直线 y = a x + b，取法向残差
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    if abs(sxx) > 1e-9:
        a = sxy / sxx
        b = mean_y - a * mean_x
        resid = [ys[i] - (a * xs[i] + b) for i in range(n)]
    else:
        resid = [y - mean_y for y in ys]

    # 去均值
    mr = sum(resid) / n
    resid = [r - mr for r in resid]

    total = sum(r * r for r in resid)
    if total <= 1e-12:
        return 0.0

    # 采样率估计：用平均延迟
    delays = [p[2] for p in pts[1:] if p[2] > 1e-6]
    if not delays:
        return 0.0
    mean_dt = sum(delays) / len(delays)
    if mean_dt <= 1e-6:
        return 0.0
    fs = 1.0 / mean_dt

    # 只算目标频带内的频率分量
    band_lo, band_hi = band
    band_power = 0.0
    all_power = 0.0
    max_k = n // 2
    for k in range(1, max_k + 1):
        f = k * fs / n
        re = sum(resid[i] * math.cos(2.0 * math.pi * k * i / n) for i in range(n))
        im = sum(resid[i] * math.sin(2.0 * math.pi * k * i / n) for i in range(n))
        p = (re * re + im * im) / n
        all_power += p
        if band_lo <= f <= band_hi:
            band_power += p

    if all_power <= 1e-12:
        return 0.0
    return min(1.0, band_power / all_power)


# --------------------------------------------------------------------------
# 时序熵
# --------------------------------------------------------------------------
def timing_entropy(delays: Sequence[float], *, bins: int = 8) -> float:
    """延迟序列的归一化熵（0-1）。

    0 = 所有延迟完全一样（机械）
    1 = 完全随机（白噪声）
    真人在 0.55-0.85 之间：有节奏但不规律。
    """
    vals = [d for d in delays if d is not None]
    if len(vals) < 4:
        return 0.0

    lo = min(vals)
    hi = max(vals)
    if hi - lo < 1e-9:
        return 0.0

    counts = [0] * bins
    for v in vals:
        idx = int((v - lo) / (hi - lo) * bins)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1

    n = len(vals)
    h = 0.0
    for c in counts:
        if c > 0:
            p = c / n
            h -= p * math.log(p, 2)

    return h / math.log(bins, 2) if bins > 1 else 0.0


# --------------------------------------------------------------------------
# 主分析入口
# --------------------------------------------------------------------------
def analyze_segments(
    segments,
    *,
    start: Optional[Tuple[float, float]] = None,
    target: Optional[Tuple[float, float]] = None,
    submovement_count: int = 0,
) -> MoveMetrics:
    """分析一组采样点。

    :param segments: 任意带 ``x``/``y``/``delay`` 属性的对象列表，
                     或 ``(x, y, delay)`` 三元组列表
    :param target: 理论目标点，用于算落点误差
    """
    pts = _as_points(segments)
    m = MoveMetrics(points=len(pts))
    if len(pts) < 2:
        return m

    # 路径长度 / 直度
    total = 0.0
    for i in range(1, len(pts)):
        total += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
    straight_dist = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
    m.path_length = total
    m.distance = straight_dist
    m.straightness = min(1.0, straight_dist / total) if total > 1e-9 else 1.0

    m.duration = sum(p[2] for p in pts)

    v = _velocities(pts)
    if v:
        m.peak_count = count_peaks(v)
        m.max_speed = max(v)
        m.mean_speed = sum(v) / len(v)
        if m.mean_speed > 1e-9:
            var = sum((x - m.mean_speed) ** 2 for x in v) / len(v)
            m.speed_cv = math.sqrt(var) / m.mean_speed
        m.asymmetry = compute_asymmetry(v)

    m.tremor_energy = tremor_energy(pts)
    m.timing_entropy = timing_entropy([p[2] for p in pts[1:]])
    m.pause_count = count_pauses(pts)
    m.submovement_count = submovement_count

    if target is not None:
        m.landing_error = math.hypot(
            pts[-1][0] - float(target[0]),
            pts[-1][1] - float(target[1]),
        )
    return m


def analyze_move(plan) -> MoveMetrics:
    """从 ``SubMovementPlan`` 或 ``MoveResult`` 生成指标。"""
    if hasattr(plan, "segments") and not hasattr(plan, "requested"):
        # SubMovementPlan
        return analyze_segments(
            plan.segments,
            target=getattr(plan, "end", None),
            submovement_count=len(getattr(plan, "submovements", []) or []),
        )
    if hasattr(plan, "requested"):
        # MoveResult：没有原始点序列，只能用已有字段
        m = MoveMetrics(
            points=plan.points,
            distance=plan.distance,
            landing_error=plan.landing_error,
            peak_count=plan.peak_count,
            straightness=plan.straightness,
            path_length=plan.path_length,
            duration=plan.duration,
            submovement_count=plan.submovements,
        )
        return m
    return analyze_segments(plan)


def analyze_timing(delays: Sequence[float]) -> Dict[str, float]:
    """只分析时间序列。"""
    vals = [float(d) for d in delays]
    if not vals:
        return {"count": 0}
    vals_sorted = sorted(vals)
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return {
        "count": n,
        "mean": round(mean, 5),
        "median": round(vals_sorted[n // 2], 5),
        "std": round(math.sqrt(var), 5),
        "min": round(min(vals), 5),
        "max": round(max(vals), 5),
        "cv": round(math.sqrt(var) / mean, 4) if mean > 0 else 0.0,
        "entropy": round(timing_entropy(vals), 4),
    }


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def verdict(m: MoveMetrics, *, allow_no_landing_error: bool = True) -> Dict[str, Any]:
    """按 ``PASS_CRITERIA`` 判定一条轨迹是否"像人"。"""
    checks: Dict[str, bool] = {}

    checks["peak_count>=2"] = m.peak_count >= PASS_CRITERIA["min_peak_count"]
    checks["landing_error<2px"] = m.landing_error <= PASS_CRITERIA["max_landing_error"]
    if not allow_no_landing_error:
        checks["landing_error>0"] = m.landing_error > PASS_CRITERIA["min_landing_error"]
    checks["straightness>=0.85"] = m.straightness >= PASS_CRITERIA["min_straightness"]
    checks["straightness<1.0"] = m.straightness < PASS_CRITERIA["max_straightness"]
    checks["asymmetry<0.98"] = m.asymmetry < PASS_CRITERIA["max_asymmetry"]

    failures = [k for k, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "metrics": m.describe(),
    }


def compare(human_plan, robot_plan) -> Dict[str, Any]:
    """对比真人化轨迹与机械轨迹的各指标。"""
    mh = analyze_move(human_plan)
    mr = analyze_move(robot_plan)
    return {
        "human": mh.describe(),
        "robot": mr.describe(),
        "delta": {
            "peak_count": mh.peak_count - mr.peak_count,
            "straightness": round(mh.straightness - mr.straightness, 4),
            "duration": round(mh.duration - mr.duration, 4),
            "tremor_energy": round(mh.tremor_energy - mr.tremor_energy, 4),
            "timing_entropy": round(mh.timing_entropy - mr.timing_entropy, 4),
        },
    }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """度量自检：峰值检测算法 + 真人/机械判定分界。

    这里是"真人度"这个词的定义所在。它错了，前面所有生成端的
    努力都无法被验证 —— 所以正反两个方向都要查：

    * **正向**：真人轨迹必须通过判定
    * **反向**：机械轨迹必须**不**通过

    只查一个方向是没有意义的（全判通过或全判不通过都能"自洽"）。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = entropy.get_seed()
    try:
        from . import submove as _sm

        # -- 峰值检测的人工用例 -----------------------------------------
        _add("单峰波数出 1 个峰",
             count_peaks([1.0, 2.0, 1.0]) == 1,
             "%d" % count_peaks([1.0, 2.0, 1.0]))
        _add("双峰波数出 2 个峰",
             count_peaks([1.0, 5.0, 1.0, 1.0, 4.0, 1.0], smooth=0,
                         min_prominence=0.15, min_gap_ratio=0.0) == 2, "")
        _add("全零序列数出 0 个峰", count_peaks([0.0, 0.0, 0.0]) == 0, "")
        _add("单元素数出 0 个峰", count_peaks([1.0]) == 0, "")

        # ⭐ 两侧谷校验：单调下降尾段的鼓包不算峰
        shoulder = [10.0, 8.0, 6.0, 4.0, 2.0, 1.0, 1.6, 1.2, 0.8, 0.4, 0.2]
        _add("单调尾段鼓包被拒绝",
             count_peaks(shoulder, smooth=0, min_prominence=0.10,
                         min_gap_ratio=0.0) == 1,
             "%d 个峰" % count_peaks(shoulder, smooth=0,
                                     min_prominence=0.10, min_gap_ratio=0.0))

        # ⭐ 相对自身判据：主峰 100 + 次峰 8 也要数出 2 个
        tall_small = [1.0, 100.0, 1.0, 1.0, 8.0, 1.0]
        _add("次峰远低于主峰仍被计入",
             count_peaks(tall_small, smooth=0, min_prominence=0.10,
                         min_gap_ratio=0.0) == 2,
             "%d 个峰（全局相对判据会漏掉）"
             % count_peaks(tall_small, smooth=0, min_prominence=0.10,
                           min_gap_ratio=0.0))

        # -- 判据常量 ---------------------------------------------------
        _add("min_peak_count == 2",
             PASS_CRITERIA["min_peak_count"] == 2, "")
        _add("max_landing_error == 2.0",
             PASS_CRITERIA["max_landing_error"] == 2.0, "")
        _add("max_straightness == 1.0",
             PASS_CRITERIA["max_straightness"] == 1.0,
             "严格小于 1，用 0.9999 会把合法长直线判失败")

        # -- 正向：真人必须通过（多行程抽样） ---------------------------
        cases = [(600, 300, 640, 340), (100, 100, 260, 220),
                 (100, 100, 700, 500), (0, 0, 1440, 900),
                 (20, 450, 1400, 450)]
        bad = []
        for (x1, y1, x2, y2) in cases:
            for seed in range(1, 21):
                entropy.seed(seed)
                p = _sm.plan_movement(x1, y1, x2, y2, precision=1.0)
                v = verdict(analyze_move(p))
                if not v["ok"]:
                    bad.append((x1, y1, x2, y2, seed, v["failures"]))
                    break
        _add("真人轨迹全部通过（5 行程 × 20 seed）", not bad,
             "%d 条未通过" % len(bad) if bad else "100 条全过")

        # -- 反向：机械必须不通过 ---------------------------------------
        passed_list = []
        for d in (40, 100, 300, 700, 1400):
            for seed in range(1, 21):
                entropy.seed(seed)
                p = _sm.plan_movement(100, 100, 100 + d, 100 + d // 2,
                                      human=False)
                v = verdict(analyze_move(p))
                if v["ok"]:
                    passed_list.append((d, seed))
                    break
        _add("机械轨迹全部不通过", not passed_list,
             "%d 条误判为真人" % len(passed_list) if passed_list else "100 条全拒")

        # -- 指标方向 ---------------------------------------------------
        entropy.seed(7)
        h = analyze_move(_sm.plan_movement(100, 100, 700, 500, human=True))
        entropy.seed(7)
        r = analyze_move(_sm.plan_movement(100, 100, 700, 500, human=False))
        _add("真人直度 < 机械直度", h.straightness < r.straightness,
             "%.5f < %.5f" % (h.straightness, r.straightness))
        _add("真人不对称 < 机械不对称", h.asymmetry < r.asymmetry,
             "%.4f < %.4f" % (h.asymmetry, r.asymmetry))
        _add("真人震颤 > 机械震颤", h.tremor_energy > r.tremor_energy,
             "%.4f > %.4f" % (h.tremor_energy, r.tremor_energy))

        # -- 其它接口 ---------------------------------------------------
        cmp_ = compare(_sm.plan_movement(100, 100, 700, 500, human=True),
                       _sm.plan_movement(100, 100, 700, 500, human=False))
        _add("compare 输出三个顶层键",
             all(k in cmp_ for k in ("human", "robot", "delta")),
             ", ".join(sorted(cmp_.keys())))
        ent = timing_entropy([0.01] * 30)
        _add("规律延迟的时序熵偏低", 0.0 <= ent < 0.4, "%.4f" % ent)

    finally:
        if saved is None:
            entropy.reset()
        else:
            entropy.seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
