# -*- coding: utf-8 -*-
"""微震颤：8-12 Hz 的低频**相关**噪声。

为什么不能用白噪声
------------------
白噪声每个采样点独立，功率谱平坦。它的视觉表现是"锯齿状的高频抖动"，
一看就是程序加的。

真人手部震颤的生理特征（生理性震颤，physiological tremor）
--------------------------------------------------------
- **主频 8-12 Hz**（这是神经-肌肉环路的不自主振荡频率，人类共性）
- **功率谱集中在窄带** —— 不是平坦的，是有一个明显的主峰
- **时间上有相关性** —— 相邻采样点的值相近（correlated noise），
  所以轨迹看起来是"波浪"而不是"毛刺"
- **幅度随运动速度变化** —— 快速移动时震颤被运动掩盖，慢速逼近
  时震颤显现（生理原因：肌肉共激活增加会放大震颤）
- **幅度 0.5-3 px**（在 100% DPI 下），精细动作时可达 4-5 px

实现方法
--------
用**叠加的正弦振荡**近似窄带相关噪声::

    tremor(t) = Σᵏ Aₖ · sin(2π fₖ t + φₖ)

- 中心频率 10 Hz，几个谐波围绕它分布（8.5 / 10.2 / 11.6 Hz）
- 每路有自己的相位 φₖ（由熵源决定，可复现）
- 幅度 Aₖ 按 1/f 衰减（低频分量更强）
- 加入**极低频漂移**（0.3-0.8 Hz）模拟手臂的姿态保持摆动

这样得到的信号：主频在 10 Hz 附近、相邻点相关、且完全可复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import entropy

__all__ = [
    "TRENOR_CENTER_HZ",
    "Tremor",
    "make_tremor",
    "tremor_series",
    "amplitude_for_speed",
    "pinkish_noise",
]


# 生理性震颤主频中心
TRENOR_CENTER_HZ = 10.0

# 默认谐波构成：(相对频率倍率, 相对幅度权重)
_HARMONICS: Tuple[Tuple[float, float], ...] = (
    (0.85, 1.00),   # 8.5 Hz  —— 主峰
    (1.00, 0.75),   # 10.0 Hz —— 主峰
    (1.16, 0.45),   # 11.6 Hz —— 主峰
    (0.62, 0.30),   # 6.2 Hz  —— 次低频
    (1.55, 0.18),   # 15.5 Hz —— 高频尾
)

# 极低频姿态漂移（手臂保持姿势时的缓慢摆动）
_DRIFT_HZ = (0.32, 0.58, 0.81)


@dataclass
class Tremor:
    """一次震颤生成器的实例。

    每个实例的相位由熵源决定；同一 seed 下相位一致 → 波形可复现。

    :param amplitude: 基础幅度（像素）。0.5-3.0 是真人区间。
    :param center_hz: 主频中心，默认 10 Hz
    :param speed_suppression: 速度抑制系数。速度越大震颤越被掩盖。
                              0 = 不抑制（快速移动时也抖），
                              1 = 完全按速度归一化抑制
    :param drift: 极低频漂移幅度（相对 amplitude 的比例）
    """

    amplitude: float = 1.2
    center_hz: float = TRENOR_CENTER_HZ
    speed_suppression: float = 0.85
    drift: float = 0.55

    _phases: Tuple[float, ...] = field(default=(), init=False, repr=False)
    _drift_phases: Tuple[float, ...] = field(default=(), init=False, repr=False)
    _amp_scale: float = field(default=1.0, init=False, repr=False)

    def __post_init__(self):
        if self.amplitude < 0:
            self.amplitude = 0.0
        # 相位随机（可复现）
        self._phases = tuple(entropy.uniform(0.0, 2.0 * math.pi) for _ in _HARMONICS)
        self._drift_phases = tuple(
            entropy.uniform(0.0, 2.0 * math.pi) for _ in _DRIFT_HZ
        )
        # 个体差异：每个人的震颤强度不一样，给个 ±30% 的乘性因子
        self._amp_scale = entropy.uniform(0.70, 1.30)

    # -- 核心 ------------------------------------------------------------
    def at(self, t: float, *, speed_ratio: float = 0.0) -> float:
        """取时间 ``t``（秒，相对本次移动起点）处的震颤偏移（像素）。

        :param speed_ratio: 当前速度 / 峰值速度，∈[0,1]。
                            用于运动掩盖效应：越快震颤越弱。
        """
        if self.amplitude <= 0:
            return 0.0

        amp = self.amplitude * self._amp_scale

        # 速度抑制：慢速（speed_ratio→0）时保留 100%，快速时压到 (1-suppression)
        if self.speed_suppression > 0:
            k = max(0.0, min(1.0, speed_ratio))
            amp *= 1.0 - self.speed_suppression * k

        # 窄带振荡
        acc = 0.0
        for (mult, weight), phase in zip(_HARMONICS, self._phases):
            f = self.center_hz * mult
            acc += weight * math.sin(2.0 * math.pi * f * t + phase)

        # 归一化：Σ weights ≈ 2.68
        norm = sum(w for _, w in _HARMONICS)
        acc /= norm

        # 极低频漂移
        if self.drift > 0:
            d = 0.0
            for f, phase in zip(_DRIFT_HZ, self._drift_phases):
                d += math.sin(2.0 * math.pi * f * t + phase)
            d /= len(_DRIFT_HZ)
            acc += self.drift * d

        return acc * amp

    def series(
        self,
        duration: float,
        samples: int,
        *,
        speed_ratios: Optional[Sequence[float]] = None,
    ) -> List[float]:
        """生成一段时间的震颤偏移序列。"""
        if samples <= 0:
            return []
        out = []
        for i in range(samples):
            t = duration * (i / max(1, samples - 1))
            sr = 0.0
            if speed_ratios is not None and i < len(speed_ratios):
                sr = speed_ratios[i]
            out.append(self.at(t, speed_ratio=sr))
        return out

    def describe(self) -> dict:
        return {
            "amplitude": round(self.amplitude, 4),
            "center_hz": self.center_hz,
            "speed_suppression": self.speed_suppression,
            "drift": self.drift,
            "amp_scale": round(self._amp_scale, 4),
        }


def make_tremor(
    *,
    amplitude: Optional[float] = None,
    detailed: bool = True,
    enabled: bool = True,
) -> Tremor:
    """按动作类型生成一个震颤实例。

    :param amplitude: 显式幅度；None 时由熵源随机决定
    :param detailed: 是否为"精细动作"（如点小按钮、拖拽对齐）。
                     精细动作的震颤更大、速度抑制更弱 —— 因为人在这
                     种场合会更慢、更用力，共激活增加。
    :param enabled: False 时返回零幅度的 Tremor（用于对照实验）
    """
    if not enabled:
        return Tremor(amplitude=0.0)

    if amplitude is None:
        if detailed:
            # 精细动作：幅度大、抑制弱（慢速时抖动明显）
            amplitude = entropy.uniform(1.6, 3.2)
        else:
            # 普通动作
            amplitude = entropy.uniform(0.5, 1.8)

    return Tremor(
        amplitude=amplitude,
        center_hz=entropy.uniform(8.8, 11.4),
        speed_suppression=entropy.uniform(0.55, 0.80) if detailed
        else entropy.uniform(0.80, 0.95),
        drift=entropy.uniform(0.35, 0.75),
    )


def tremor_series(
    duration: float,
    samples: int,
    *,
    amplitude: float = 1.2,
    speed_ratios: Optional[Sequence[float]] = None,
) -> List[float]:
    """一次性生成震颤序列的便捷函数（会消耗熵源）。"""
    return make_tremor(amplitude=amplitude).series(
        duration, samples, speed_ratios=speed_ratios
    )


def amplitude_for_speed(
    speed: float,
    *,
    peak_speed: float,
    base_amplitude: float = 1.2,
    exponent: float = 1.6,
) -> float:
    """按即时速度缩放震颤幅度。

    速度越接近峰值，震颤越被"运动掩盖"（实测：快速移动时手抖幅度
    下降到 30-40%）。用幂律而非线性，因为抑制在高速端更陡。
    """
    if peak_speed <= 0:
        return base_amplitude
    r = max(0.0, min(1.0, speed / peak_speed))
    return base_amplitude * (1.0 - 0.65 * (r ** exponent))


def pinkish_noise(count: int, *, alpha: float = 1.0) -> List[float]:
    """1/f^α 噪声（粉红噪声）序列。

    用途：给"认知停顿"的分布、或者路径上很慢的漂移提供更自然的
    长相关性。实现用最简单的 **Voss-McCartney** 算法：
    多个不同更新频率的随机游走叠加，天然产生 1/f 谱。
    """
    if count <= 0:
        return []

    # 8 个八度的行波
    octaves = 8
    values = [0.0] * octaves
    counters = [0] * octaves
    out: List[float] = []

    for i in range(count):
        for k in range(octaves):
            if i % (1 << k) == 0:
                values[k] = entropy.uniform(-1.0, 1.0)
        s = sum(values) / octaves
        out.append(s)
        for k in range(octaves):
            counters[k] += 1

    return out


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """震颤自检：振荡性、频率带、运动掩盖、可禁用、可复现。"""
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = entropy.get_seed()
    try:
        entropy.seed(5)

        # -- 真的在振荡 -------------------------------------------------
        tm = make_tremor(amplitude=2.0)
        vals = [tm.at(i * 0.002, speed_ratio=0.1) for i in range(250)]
        span = max(vals) - min(vals)
        _add("幅度有变化（非恒定）", span > 0.1, "跨度 %.4f" % span)
        sign_changes = sum(1 for i in range(1, len(vals))
                           if (vals[i] > 0) != (vals[i - 1] > 0))
        _add("符号反复翻转（是振荡）", sign_changes > 5,
             "250 点变号 %d 次" % sign_changes)

        # -- 频率落在生理带 ---------------------------------------------
        entropy.seed(17)
        tm2 = make_tremor(amplitude=2.0)
        dt = 0.001
        n = 3000
        v2 = [tm2.at(i * dt, speed_ratio=0.05) for i in range(n)]
        sc = sum(1 for i in range(1, len(v2))
                 if (v2[i] > 0) != (v2[i - 1] > 0))
        freq = sc / 2.0 / (n * dt)
        _add("估计频率落在 5-40Hz", 5.0 <= freq <= 40.0, "%.2f Hz" % freq)
        _add("主频常量 == 10Hz", TRENOR_CENTER_HZ == 10.0,
             "TRENOR_CENTER_HZ=%r" % TRENOR_CENTER_HZ)

        # -- 运动掩盖（速度越大抖得越轻） -------------------------------
        entropy.seed(23)
        tm3 = make_tremor(amplitude=2.0)
        slow = [abs(tm3.at(i * 0.004, speed_ratio=0.05)) for i in range(400)]
        fast = [abs(tm3.at(i * 0.004, speed_ratio=1.0)) for i in range(400)]
        avg_s = sum(slow) / len(slow)
        avg_f = sum(fast) / len(fast)
        _add("慢速抖动大于快速（运动掩盖）", avg_s > avg_f,
             "慢 %.4f > 快 %.4f" % (avg_s, avg_f))

        # -- 可禁用 -----------------------------------------------------
        entropy.seed(29)
        off = make_tremor(amplitude=2.0, enabled=False)
        zs = [off.at(i * 0.004, speed_ratio=0.3) for i in range(200)]
        _add("enabled=False 输出恒零",
             all(abs(z) < 1e-12 for z in zs),
             "max=%.3e" % max(abs(z) for z in zs))

        # -- 可复现 -----------------------------------------------------
        entropy.seed(31)
        s1 = tremor_series(2.0, 300, amplitude=1.2)
        entropy.seed(31)
        s2 = tremor_series(2.0, 300, amplitude=1.2)
        _add("tremor_series 可复现", s1 == s2, "%d 个采样点" % len(s1))
        _add("tremor_series 长度正确", len(s1) == 300, "len=%d" % len(s1))
        _add("tremor_series 真的在起伏",
             max(s1) - min(s1) > 0.05,
             "跨度 %.4f" % (max(s1) - min(s1)))

        # -- 幅度随速度的函数 -------------------------------------------
        # 规则：速度越快，震颤被运动掩盖，幅度越小。
        # peak_speed 是本次运动的峰值速度，作为归一化基准。
        amps = [amplitude_for_speed(r, peak_speed=1.0)
                for r in (0.0, 0.25, 0.5, 1.0)]
        _add("amplitude_for_speed 单调递减",
             all(amps[i] >= amps[i + 1] for i in range(len(amps) - 1)),
             " / ".join("%.3f" % a for a in amps))
        _add("低速震颤幅度 > 高速",
             amps[0] > amps[-1], "%.3f > %.3f" % (amps[0], amps[-1]))

    finally:
        if saved is None:
            entropy.reset()
        else:
            entropy.seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
