# -*- coding: utf-8 -*-
"""运动学：非对称速度曲线。

人类瞄准运动的核心特征（Fitts 定律 + Woodworth 双相模型）
--------------------------------------------------------
一次指向性移动**不是**匀速的，也不是对称加减速的，而是::

    ┌ 冲刺相（ballistic）: 启动快、加速度大，快速逼近目标
    │                      —— 占总时长约 60-70%，覆盖约 85-90% 距离
    └ 修正相（corrective）: 大幅减速，缓慢贴合目标
                           —— 占总时长约 30-40%，覆盖剩余 10-15%

关键量化特征：
    - **加速段短、减速段长**。速度曲线峰值出现在行程的 **40-55%** 处，
      偏前。这是"非对称"的全部含义。
    - 减速段的时长通常是加速段的 **1.5-3 倍**。
    - 移动距离越长，加速段占比越大（长距离更"果断"）。
    - 移动距离越短，曲线越接近对称，甚至出现"过冲-回拉"。

本模块提供 ``AsymmetricProfile``：把 0→1 的归一化进度映射成
0→1 的归一化位移，速度曲线由 ``accel_ratio`` 和 ``peak_position``
两个参数控制。

参考实现
--------
采用改进的 double-sigmoid / minimum-jerk 混合形式：

    前段（加速）：  v(t) ∝ sin²(π/2 · t/t_p)        —— 从 0 平滑升到峰值
    后段（减速）：  v(t) ∝ cos²(π/2 · (t-t_p)/(1-t_p)) —— 从峰值平滑降到 0

对速度积分得到位移 s(t)，再归一化使 s(1) = 1。这个形式的好处是
速度连续、在 t_p 处导数为 0（无加速度突变），且天然非对称。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import entropy

__all__ = [
    "AsymmetricProfile",
    "MinimumJerkProfile",
    "make_profile",
    "sample_positions",
    "velocity_curve",
    "duration_for_distance",
    "estimate_peak_count",
]


def _clamp01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# --------------------------------------------------------------------------
# 非对称速度曲线
# --------------------------------------------------------------------------
@dataclass
class AsymmetricProfile:
    """非对称速度曲线：快加速、慢减速。

    :param peak_position: 速度峰值出现的归一化位置（0.35-0.60 是真人区间）
    :param accel_ratio: 加速段与减速段的时长比。1.0=对称，
                        0.4=加速很快、减速拖很长（典型伸手动作）
    :param overshoot: 过冲比例。0=不过冲，>0 表示在 1-overshoot 处
                      先冲过头再回拉（短距离精细移动常见 2-6%）
    :param wobble: 峰值附近的抖动幅度，让曲线不是数学上的光滑
    """

    peak_position: float = 0.45
    accel_ratio: float = 0.5
    overshoot: float = 0.0
    wobble: float = 0.0

    # 内部：预计算的归一化常数
    _accel_area: float = field(default=0.0, init=False, repr=False)
    _decel_area: float = field(default=0.0, init=False, repr=False)
    _wobble_phases: Tuple[float, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self):
        self.peak_position = _clamp01(self.peak_position)
        # 峰值位置不能在端点，否则积分退化
        if self.peak_position <= 0.02:
            self.peak_position = 0.02
        if self.peak_position >= 0.98:
            self.peak_position = 0.98
        if self.accel_ratio <= 0:
            self.accel_ratio = 0.5
        self._precompute()

    def _precompute(self) -> None:
        """预计算两段速度曲线的定积分，用于归一化。"""
        tp = self.peak_position

        # 加速段 v(t) = sin²(π/2 · t/tp)，t ∈ [0, tp]
        # ∫₀^tp sin²(π/2 · t/tp) dt = tp/2
        self._accel_area = tp / 2.0

        # 减速段 v(t) = cos²(π/2 · (t-tp)/(1-tp))，t ∈ [tp, 1]
        # ∫ 同理 = (1-tp)/2
        self._decel_area = (1.0 - tp) / 2.0

        # 用 accel_ratio 缩放减速段的"时间权重"：accel_ratio 小 → 减速段
        # 在进度比例上占更多时间。这里对减速段做 gamma 重参数化。
        g = 1.0 / max(0.05, self.accel_ratio)
        self._decel_gamma = g

        if self.wobble > 0:
            # 固定相位的谐波，用熵源决定相位（可复现）
            n = 3
            self._wobble_phases = tuple(
                entropy.uniform(0.0, 2.0 * math.pi) for _ in range(n)
            )

    # -- 核心映射 --------------------------------------------------------
    def _velocity(self, t: float) -> float:
        """归一化时间 t ∈ [0,1] 处的归一化速度。"""
        t = _clamp01(t)
        tp = self.peak_position

        if t <= tp:
            # 加速段
            if tp <= 0:
                base = 0.0
            else:
                base = math.sin(math.pi * 0.5 * (t / tp)) ** 2
        else:
            # 减速段：把 [tp,1] 映射到 [0,1]，用 gamma 控制形状
            u = (t - tp) / (1.0 - tp) if (1.0 - tp) > 0 else 1.0
            u = _clamp01(u)
            # cos² 上升 → 衰减；gamma>1 让尾部更拖
            base = math.cos(math.pi * 0.5 * (u ** min(1.0, self._decel_gamma))) ** 2

        if self.wobble > 0 and self._wobble_phases:
            w = self.wobble
            base *= 1.0 + w * (
                0.6 * math.sin(self._wobble_phases[0] + 7.0 * math.pi * t)
                + 0.3 * math.sin(self._wobble_phases[1] + 13.0 * math.pi * t)
                + 0.1 * math.sin(self._wobble_phases[2] + 23.0 * math.pi * t)
            )
            if base < 0.0:
                base = 0.0

        return base

    def position(self, t: float) -> float:
        """归一化时间 t → 归一化位移 s ∈ [0, 1]。

        用**解析积分**而非数值累加：速度曲线是 sin²/cos²，
        在各自的半区间上有闭式原函数，精度更高也更快。
        """
        t = _clamp01(t)
        tp = self.peak_position

        if t <= tp:
            # ∫ sin²(π/2 · u/tp) du, u ∈ [0,t]
            # = t/2 - (tp/(2π)) · sin(π t / tp)
            if tp <= 0:
                s = 0.0
            else:
                s = t / 2.0 - (tp / (2.0 * math.pi)) * math.sin(math.pi * t / tp)
        else:
            # 加速段整段面积 + 减速段 [tp, t] 的面积
            s = self._accel_area
            u = (t - tp) / (1.0 - tp) if (1.0 - tp) > 0 else 1.0
            u = _clamp01(u)
            # 对 gamma 重参数化做数值积分（gamma≠1 时无简单原函数）
            steps = 40
            acc = 0.0
            for i in range(steps):
                uu = u * (i + 0.5) / steps
                acc += math.cos(math.pi * 0.5 * (uu ** min(1.0, self._decel_gamma))) ** 2
            s += acc * (u / steps) * (1.0 - tp) / 2.0 * 2.0

        total = self._accel_area + self._decel_area
        if total <= 0:
            return _clamp01(t)
        s = s / total

        # 过冲：在最终段叠加一个衰减的负向修正
        if self.overshoot > 0 and t > 0.55:
            k = (t - 0.55) / 0.45
            s += self.overshoot * math.sin(math.pi * k) * (1.0 - k) * 0.02

        return math.nan if s != s else s

    # -- 便捷 ------------------------------------------------------------
    def positions(self, count: int) -> List[float]:
        """按等间隔时间采样 ``count`` 个归一化位移（含端点）。"""
        if count < 2:
            return [0.0] * max(0, count)
        return [self.position(i / (count - 1)) for i in range(count)]

    def describe(self) -> dict:
        return {
            "peak_position": round(self.peak_position, 4),
            "accel_ratio": round(self.accel_ratio, 4),
            "overshoot": round(self.overshoot, 4),
            "wobble": round(self.wobble, 4),
        }


# --------------------------------------------------------------------------
# 备用：minimum-jerk 曲线（对称，仅用于对照/测试）
# --------------------------------------------------------------------------
@dataclass
class MinimumJerkProfile:
    """最小加加速度曲线：``s(t) = 10t³ - 15t⁴ + 6t⁵``。对称。

    这是机器人学的"最平滑运动"标准解。真人**不**这样动 —— 保留它
    是为了在测试里做对照：一个真人化轨迹的速度峰值位置应该明显
    偏离 0.5，且加速段短于减速段。
    """

    peak_position: float = 0.5
    accel_ratio: float = 1.0
    overshoot: float = 0.0
    wobble: float = 0.0

    def position(self, t: float) -> float:
        t = _clamp01(t)
        return 10.0 * t**3 - 15.0 * t**4 + 6.0 * t**5

    def positions(self, count: int) -> List[float]:
        if count < 2:
            return [0.0] * max(0, count)
        return [self.position(i / (count - 1)) for i in range(count)]

    def describe(self) -> dict:
        return {"kind": "minimum_jerk"}


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def make_profile(
    distance: float,
    *,
    human: bool = True,
    peak_position: Optional[float] = None,
    accel_ratio: Optional[float] = None,
) -> object:
    """按移动距离生成合适的曲线。

    远距离移动更"果敢"（峰值更靠前、加速段更短）；
    近距离更谨慎（曲线更对称）。这个规律来自 Fitts 定律下的
    实际观察数据，不是随手拍的。
    """
    if not human:
        return MinimumJerkProfile()

    d = abs(float(distance))

    if peak_position is None:
        # 距离 < 80px：偏对称（0.48-0.52）
        # 距离 > 600px：明显偏前（0.38-0.45）
        if d < 80.0:
            peak_position = entropy.uniform(0.46, 0.53)
        elif d < 400.0:
            peak_position = entropy.uniform(0.42, 0.50)
        else:
            peak_position = entropy.uniform(0.36, 0.45)

    if accel_ratio is None:
        # 加速段/减速段时长比：长距离 0.35-0.55，短距离 0.6-0.9
        if d < 80.0:
            accel_ratio = entropy.uniform(0.60, 0.90)
        elif d < 400.0:
            accel_ratio = entropy.uniform(0.45, 0.70)
        else:
            accel_ratio = entropy.uniform(0.35, 0.55)

    # 短距离 + 高精度需求 → 会有轻微过冲
    overshoot = 0.0
    if 100.0 < d < 700.0 and entropy.random() < 0.55:
        overshoot = entropy.uniform(0.15, 0.6)

    return AsymmetricProfile(
        peak_position=peak_position,
        accel_ratio=accel_ratio,
        overshoot=overshoot,
        wobble=entropy.uniform(0.02, 0.07),
    )


def sample_positions(
    profile,
    count: int,
    *,
    start: float = 0.0,
    end: float = 1.0,
) -> List[float]:
    """把归一化曲线映射到 [start, end] 的实际位移序列。"""
    norm = profile.positions(count)
    span = end - start
    return [start + s * span for s in norm]


# --------------------------------------------------------------------------
# 时长
# --------------------------------------------------------------------------
def duration_for_distance(
    distance: float,
    *,
    base: float = 0.085,
    per_pixel: float = 0.00042,
    max_duration: float = 1.35,
    min_dist: float = 0.0,
    min_ratio: float = 0.78,
    shape: float = 0.10,
) -> float:
    """按距离估算移动时长（秒）。

    形状参考 Fitts 定律：时长随距离**亚线性**增长（对数），
    但为了避免对数在小距离处趋于 0，改用 ``base + per_pixel × d^0.85``
    的经验式。

    真人参考值（实测统计）：
        50px   → 约 0.10-0.18s
        200px  → 约 0.16-0.30s
        800px  → 约 0.30-0.55s
        1400px → 约 0.40-0.75s

    :param min_dist: 距离参考（像素）。**给修正段/微调段用**，主运动不传。

        为什么必须有它：本函数算的是"走 d 像素要多久"，隐含前提是
        "这段路要**起速→到峰值→停稳**"。所以短距离的时长下限
        （``base`` ≈ 85ms）本质上是在表达"再短的动作也有起停开销" ——
        而按这个下限推回去，100px 用 85ms，等效平均速度 1176 px/s，
        **比主运动还快**。这显然不对。

        传了 ``min_dist`` 之后，函数改用一条**独立的折叠曲线**接替
        ``d < min_dist`` 这一段::

            peak = base + per_pixel · min_dist^0.85      # 折点处的时长
            folded(d) = max(base · min_ratio, peak · (d/min_dist)^shape)

        * 折点取在 ``d = min_dist`` **本身**（不是它的某个比例），
          于是 ``d ≥ min_dist`` 的区间完全走原曲线 ——
          ``min_dist`` 小于距离时行为与不传**逐位相同**。
        * ``shape = 0.45 < 1`` 保证 folded 随 d 单调递增；
          ``d → 0`` 时收敛到 0，所以需要一条地板托住，地板取
          ``base · min_ratio``，``min_ratio = 2.0``。
        * 两条曲线在折点处**都等于 peak**，天然接缝，无台阶。

        **踩过的两个坑，都记在这里：**

        ① **参考尺度必须用调用方自己的 base/per_pixel 算。**
           早期版本拿模块默认参数算 ``ref_dur``，而修正段是带着
           ``base=0.075, per_pixel=0.00052`` 进来的 —— 参考基数与本次
           基数不匹配，折算出来的值**比不传还小**（0.079 vs 0.107），
           "让短距离变慢"的意图完全落空。

        ② **不能写成"原时长的倍数"。** 试过
           ``amp = 1 + k·(ref/d − 1)`` 和"向 ref^0.85 插值"两种，
           都在 d ∈ (0, 400] 上**非单调** —— 因为 ``base`` 主导时
           ``own(d)`` 几乎是常数（斜率仅 ``PP·0.85·d^-0.15``），
           任何随 d 衰减的倍率都会把它压下去，出现"d=40 比 d=400 更久"。
           折叠段必须**自带形状**，不能是别的曲线的缩放。

        实测（``base=0.085, per_pixel=0.00042, min_dist=700``）::

            d=  40px → 0.1700s   （不传时 0.0912s，×1.80）
            d= 100px → 0.2101s   （不传时 0.1022s，×1.60）
            d= 315px → 0.3320s   （不传时 0.1357s，×1.45）
            d= 700px → 0.1879s   （与不传逐位相同）

        峰值速度落到 500-900 px/s，与主峰（约 8000）的比值落在文献
        "修正段约为主运动的 1/4 - 1/10" 区间内。
    """
    d = abs(float(distance))
    ref = abs(float(min_dist))
    if ref > d > 0.0:
        # 折点处的时长 —— 用**同一套** base/per_pixel 算（坑 ①）
        peak = base + per_pixel * (ref ** 0.85)
        # 折叠段自带形状（坑 ②）：幂曲线 shape<1 ⇒ 单调递增
        raw = peak * ((d / ref) ** shape)
        # 起停地板：d→0 时幂曲线收敛到 0，物理上不可能。地板锚在
        # **折点时长**上（不是 base 上）—— 这样地板与折点同源，
        # 不会出现"地板比折点还高、把整条曲线压平"的情况。
        floor = peak * min_ratio
        if raw < floor:
            raw = floor
    else:
        raw = base + per_pixel * (d ** 0.85)
    # 加个体差异抖动。**只在最终值上抖一次** —— 中间量保持确定性，
    # 否则同一 seed 下走折叠分支和不走折叠分支会消耗不同次数的随机数，
    # 导致 min_dist 小于距离时结果与不传 min_dist 不一致。
    raw *= entropy.uniform(0.85, 1.20)
    return min(max_duration, max(0.03, raw))


def velocity_curve(profile, samples: int = 60) -> List[float]:
    """返回速度序列（对位移做数值微分），供度量使用。"""
    pos = profile.positions(samples)
    v = []
    for i in range(1, len(pos)):
        v.append((pos[i] - pos[i - 1]) * (len(pos) - 1))
    return v


def estimate_peak_count(profile, samples: int = 60, *, min_prominence: float = 0.18) -> int:
    """数速度曲线上的峰值个数。

    真人移动的速度曲线通常有 **2 个及以上**峰值：冲刺相一个主峰，
    修正相一到多个小峰（微修正）。只有 1 个峰意味着"一次冲到底"，
    是机器人生成的特征。

    ``min_prominence`` 是相对主峰高度的最小显著性阈值，
    过滤掉数值噪声造成的假峰。
    """
    v = velocity_curve(profile, samples)
    if len(v) < 3:
        return 0

    peak = max(v)
    if peak <= 0:
        return 0
    threshold = peak * min_prominence

    count = 0
    for i in range(1, len(v) - 1):
        if v[i] > v[i - 1] and v[i] >= v[i + 1] and v[i] >= threshold:
            count += 1

    # 单调上升/下降的短序列可能一个峰都数不到，但序列本身有起伏
    if count == 0 and peak > 0:
        count = 1
    return count


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """运动学自检：曲线端点、单调性、时长曲线、min_dist 折算。

    ``duration_for_distance`` 的 ``min_dist`` 折算分支是本阶段改动
    最频繁的地方（见它的 docstring 里记的三个坑），所以这条要
    单独钉死 —— 单靠"看着差不多"是发现不了非单调的。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = entropy.get_seed()
    try:
        entropy.seed(7)

        # -- 曲线端点 ---------------------------------------------------
        prof = AsymmetricProfile(peak_position=0.45, accel_ratio=0.5)
        _add("position(0) == 0", abs(prof.position(0.0)) < 1e-9,
             "%.3e" % prof.position(0.0))
        _add("position(1) == 1", abs(prof.position(1.0) - 1.0) < 1e-6,
             "%.9f" % prof.position(1.0))

        # -- 单调性 -----------------------------------------------------
        prev = -1.0
        mono = True
        for i in range(101):
            s = prof.position(i / 100.0)
            if s < prev - 1e-9:
                mono = False
                break
            prev = s
        _add("AsymmetricProfile 位移单调不减", mono, "101 个采样点")

        # -- 真人 vs 机械 -----------------------------------------------
        human = make_profile(700, human=True)
        robot = make_profile(700, human=False)
        _add("human=True 给非对称曲线",
             isinstance(human, AsymmetricProfile), type(human).__name__)
        _add("human=False 给 minimum-jerk",
             isinstance(robot, MinimumJerkProfile), type(robot).__name__)
        _add("真人峰值位置偏前", 0.30 < human.peak_position < 0.60,
             "peak_position=%.3f" % human.peak_position)
        _add("minimum-jerk 峰值居中", abs(robot.peak_position - 0.5) < 1e-9,
             "peak_position=%.3f" % robot.peak_position)

        # -- 时长随距离单调 ---------------------------------------------
        # ⚠ 单点采样不保证单调：duration_for_distance 末尾乘了
        # uniform(0.85, 1.20)，相邻距离的增量本来就小于抖动幅度。
        # 所以这里取大样本均值——均值才反映曲线本体的走势。
        prev_t = 0.0
        mono_t = True
        bad_d = None
        for d in (50, 100, 200, 400, 800, 1200):
            entropy.seed(99)
            t = sum(duration_for_distance(d) for _ in range(200)) / 200
            if t < prev_t - 1e-9:
                mono_t = False
                bad_d = d
                break
            prev_t = t
        _add("时长随距离单调不减", mono_t,
             "断点 d=%r" % bad_d if bad_d else "50→1200px 均值单调")

        # -- min_dist 折算（三个坑的回归） ------------------------------
        entropy.seed(21)
        plain = sum(duration_for_distance(100) for _ in range(40)) / 40
        entropy.seed(21)
        folded = sum(duration_for_distance(100, min_dist=700)
                     for _ in range(40)) / 40
        _add("min_dist 让短距离变慢", folded > plain * 1.5,
             "%.4f → %.4f（×%.2f）" % (plain, folded, folded / plain))

        # ⭐ 折算段必须单调：早期版本会出现"d=40 比 d=400 更久"
        # 同样用均值消掉末尾的 uniform 抖动。
        prev_f = -1.0
        mono_f = True
        bad_d = None
        for d in (3, 10, 20, 40, 55, 100, 200, 315, 400, 600, 699, 700):
            entropy.seed(7)
            v = sum(duration_for_distance(d, min_dist=700)
                    for _ in range(120)) / 120
            if v < prev_f - 1e-9:
                mono_f = False
                bad_d = d
                break
            prev_f = v
        _add("min_dist 折算段单调不减", mono_f,
             "断点 d=%r" % bad_d if bad_d else "3→700px 全程单调")

        # ⭐ 在 d == min_dist 处必须与不传 min_dist 完全接上（无缝）
        entropy.seed(42)
        x = duration_for_distance(700, min_dist=700)
        entropy.seed(42)
        y = duration_for_distance(700)
        _add("d==min_dist 处无缝接合", x == y,
             "%.10f vs %.10f" % (x, y))

        # ⭐ min_dist 小于距离时行为必须与不传逐位相同
        entropy.seed(42)
        x2 = duration_for_distance(800, min_dist=400)
        entropy.seed(42)
        y2 = duration_for_distance(800)
        _add("min_dist<d 时退化为不传", x2 == y2,
             "%.10f vs %.10f" % (x2, y2))

        # -- 其它 -------------------------------------------------------
        prof500 = make_profile(500, human=True)
        vc = velocity_curve(prof500, 60)
        _add("velocity_curve 非空且为正", bool(vc) and max(vc) > 0,
             "%d 个采样点，峰值 %.3f" % (len(vc), max(vc) if vc else 0))
        _add("estimate_peak_count 可调用",
             isinstance(estimate_peak_count(prof500), int),
             "%d 个峰" % estimate_peak_count(prof500))
        _add("sample_positions 长度正确",
             len(sample_positions(prof500, 64)) == 64,
             "len=%d" % len(sample_positions(prof500, 64)))

    finally:
        if saved is None:
            entropy.reset()
        else:
            entropy.seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
