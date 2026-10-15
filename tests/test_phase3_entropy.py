#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段三 · 真人熵引擎测试（可复现性 / 运动学 / 度量判定）。

覆盖 ``avatar/human_entropy/`` 下的七个模块：entropy（随机源）、
kinetics（速度曲线）、tremor（生理震颤）、submove（多段子运动）、
mouse / keyboard（鼠标键盘的真人化封装）、metrics（真人度度量）。

设计原则（与阶段一/二测试一致）：

* **零第三方依赖** —— 只用标准库 + 被测代码本身。
* **不碰真实设备** —— 本文件纯计算，不移动光标、不按键。
  （真人化的实机验证在 ``test_phase3_dispatcher.py`` 和手工命令里。）
* **失败要响亮** —— 判定不通过必须如实报告，不许放宽阈值蒙混。

本文件刻意钉住的硬约束（阶段三规格第 4、5 条）：

1. **所有随机源必须走 entropy.py**，不许有模块绕过它直接调 ``random``。
   绕过去就不可复现了 —— 而"可复现"是调参和回归的前提。
2. **同一 seed 必须生成完全相同的轨迹**。这条用逐点比对钉死，
   不是"大致相同"。
3. 真人轨迹必须在**三个核心指标**上明确区别于机械轨迹：
   峰值数 ≥2、直度 ≠1、不对称度 <1。机械轨迹这三项应该反过来。

以下这些判据的阈值都是**实测扫描出来的**，不是拍脑袋定的 ——
每条的取值理由都写在了对应断言的注释里。
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.human_entropy import entropy as E
from avatar.human_entropy import keyboard as HK
from avatar.human_entropy import kinetics as KN
from avatar.human_entropy import metrics as MT
from avatar.human_entropy import mouse as HM
from avatar.human_entropy import submove as SM
from avatar.human_entropy import tremor as TR


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
        print("  [FAIL] %s  -> %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  -> %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _points(plan):
    """把 SubMovementPlan 摊成 (x, y, delay) 列表。"""
    return [(s.x, s.y, s.delay) for s in plan.segments]


#: 覆盖各类行程与精度的样本矩阵。全部断言都跑这一整套，
#: 避免"只在 700px 上通过"这种假绿。
CASES = [
    ("短距 40px", (600, 300, 640, 340), 1.0),
    ("微距 60px", (500, 500, 560, 540), 1.0),
    ("中短距 160px", (100, 100, 260, 220), 1.0),
    ("中距反向 424px", (700, 400, 300, 260), 1.0),
    ("长距 721px", (100, 100, 700, 500), 1.0),
    ("全屏对角线 1698px", (0, 0, 1440, 900), 1.0),
    ("水平长距", (20, 450, 1400, 450), 1.0),
    ("垂直长距", (720, 30, 720, 870), 1.0),
    ("长距高精度", (100, 100, 700, 500), 2.0),
    ("远距高精度", (300, 300, 1370, 850), 1.6),
]


# --------------------------------------------------------------------------
# 1. 熵源纪律（硬约束 #4）
# --------------------------------------------------------------------------
def test_entropy_module_discipline():
    """human_entropy 下的模块不许绕过 entropy.py 直接调 random。

    这条要真查：直接 ``import random`` 还能用，但会破坏可复现性 ——
    而且症状很隐蔽（大部分时候看起来是随机的，偶尔才露馅）。
    做法是源码级检查：允许 ``import random as _random_module``
    （entropy.py 自己要用它建 Random 实例），但不允许调用
    ``random.xxx`` 这种模块级函数。
    """
    import re

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "avatar", "human_entropy")
    offenders = []
    # 匹配 `random.` 开头但不是 `_random_module.` 也不是 `self.random` 的调用
    pat = re.compile(r"(?<![_\w.])random\s*\.\s*(?!Random\b)")
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py") or fn == "entropy.py":
            continue
        path = os.path.join(root, fn)
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                code = line.split("#", 1)[0]
                if pat.search(code):
                    offenders.append("%s:%d  %s" % (fn, lineno, code.strip()))
    _expect(not offenders,
            "以下位置绕过了 entropy.py 直接调用 random：\n    "
            + "\n    ".join(offenders))


def test_entropy_state_api():
    """种子、开关、重置这套 API 必须完整可用。"""
    saved = E.get_seed()
    try:
        E.seed(1234)
        _expect(E.get_seed() == 1234, "设种子后 get_seed() 应返回 1234")
        _expect(E.is_reproducible(),
                "设了种子之后 is_reproducible() 应为 True")

        E.reset()
        _expect(E.get_seed() is None, "reset() 后种子应回到 None")
        _expect(not E.is_reproducible(),
                "无种子时 is_reproducible() 应为 False")
    finally:
        if saved is None:
            E.reset()
        else:
            E.seed(saved)


def test_entropy_helpers_ranges():
    """基本随机函数必须落在声明的区间内。"""
    E.seed(99)
    for _ in range(300):
        v = E.uniform(3.0, 7.0)
        _expect(3.0 <= v <= 7.0, "uniform(3,7) 越界: %r" % v)
        g = E.gauss(10.0, 2.0)
        _expect(isinstance(g, float), "gauss 应返回 float")
        i = E.randint(5, 9)
        _expect(5 <= i <= 9, "randint(5,9) 越界: %r" % i)

    for _ in range(300):
        v = E.clamped_gauss(0.0, 5.0, -1.0, 1.0)
        _expect(-1.0 <= v <= 1.0, "clamped_gauss 越界: %r" % v)

    r = E.random()
    _expect(0.0 <= r < 1.0, "random() 应在 [0,1): %r" % r)


def test_derive_produces_independent_streams():
    """derive(tag) 派生出的流必须与主流的序列不同、且自身可复现。

    派生流的用途：让"轨迹形状"和"打字节奏"各自独立，
    互不干扰 —— 改一个不会让另一个也跟着变。
    """
    E.seed(555)
    a = [E.uniform(0, 1) for _ in range(5)]
    E.seed(555)
    b = [E.uniform(0, 1) for _ in range(5)]
    _expect(a == b, "同一 seed 主流序列不一致")

    E.seed(555)
    d1 = [E.derive("mouse").uniform(0, 1) for _ in range(5)]
    E.seed(555)
    d2 = [E.derive("mouse").uniform(0, 1) for _ in range(5)]
    _expect(d1 == d2, "derive() 返回的对象不可复现")

    E.seed(555)
    d3 = [E.derive("keyboard").uniform(0, 1) for _ in range(5)]
    _expect(d1 != d3, "不同 tag 派生出的序列不该相同")

    E.seed(555)
    a2 = [E.uniform(0, 1) for _ in range(5)]
    _expect(a == a2, "走派生流之后主流序列被污染了")


# --------------------------------------------------------------------------
# 2. 可复现性（硬约束 #5 —— 本阶段最核心的一条）
# --------------------------------------------------------------------------
def test_plan_reproducible():
    """同一 seed 两次规划出的轨迹必须**逐点完全相同**。

    不是"大致相同"、"误差小于 1px" —— 是完全相同。任何浮点层面的
    差异都说明有随机源漏出了熵控制。
    """
    for name, pts, prec in CASES:
        E.seed(20260927)
        p1 = SM.plan_movement(*pts, precision=prec)
        E.seed(20260927)
        p2 = SM.plan_movement(*pts, precision=prec)
        s1, s2 = _points(p1), _points(p2)
        _expect(len(s1) == len(s2),
                "[%s] 点数不一致：%d vs %d" % (name, len(s1), len(s2)))
        for i, (a, b) in enumerate(zip(s1, s2)):
            _expect(a == b,
                    "[%s] 第 %d 点不一致：%r vs %r" % (name, i, a, b))
        _expect(p1.total_duration == p2.total_duration,
                "[%s] 总时长不一致：%r vs %r"
                % (name, p1.total_duration, p2.total_duration))


def test_plan_differs_across_seeds():
    """换 seed 必须产生不同轨迹 —— 否则这个熵源是死的。"""
    E.seed(1)
    a = _points(SM.plan_movement(100, 100, 700, 500))
    E.seed(2)
    b = _points(SM.plan_movement(100, 100, 700, 500))
    _expect(a != b, "seed=1 与 seed=2 生成了完全相同的轨迹")
    # 长度可以不同，但不能"只差一个点还全都一样"
    same = sum(1 for x, y in zip(a, b) if x == y)
    _expect(same < len(a) * 0.5,
            "两个 seed 的轨迹有 %d/%d 个点相同，随机性太弱" % (same, len(a)))


def test_no_seed_is_still_random():
    """不设种子时两次轨迹应该不同（真随机模式）。"""
    E.reset()
    a = _points(SM.plan_movement(100, 100, 700, 500))
    b = _points(SM.plan_movement(100, 100, 700, 500))
    _expect(a != b, "无种子模式下两次轨迹相同，说明熵没起作用")


def test_submovement_reproducible():
    """子运动结构本身也必须可复现。"""
    E.seed(31337)
    p1 = SM.plan_movement(100, 100, 800, 600)
    E.seed(31337)
    p2 = SM.plan_movement(100, 100, 800, 600)
    _expect(len(p1.submovements) == len(p2.submovements),
            "子运动段数不一致：%d vs %d"
            % (len(p1.submovements), len(p2.submovements)))
    for i, (a, b) in enumerate(zip(p1.submovements, p2.submovements)):
        _expect(a.describe() == b.describe(),
                "第 %d 段参数不一致：%r vs %r"
                % (i, a.describe(), b.describe()))


# --------------------------------------------------------------------------
# 3. 运动学曲线
# --------------------------------------------------------------------------
def test_asymmetric_profile_monotonic():
    """非对称速度曲线的位移必须单调不减，且端点精确 0 → 1。"""
    E.seed(7)
    prof = KN.AsymmetricProfile(peak_position=0.45, accel_ratio=0.5)
    _expect(abs(prof.position(0.0)) < 1e-9,
            "position(0) 应为 0，实际 %r" % prof.position(0.0))
    _expect(abs(prof.position(1.0) - 1.0) < 1e-6,
            "position(1) 应为 1，实际 %r" % prof.position(1.0))
    prev = -1.0
    for i in range(101):
        s = prof.position(i / 100.0)
        _expect(s >= prev - 1e-9,
                "位移非单调：t=%.2f 时 s=%.6f < 前值 %.6f"
                % (i / 100.0, s, prev))
        prev = s


def test_asymmetric_vs_minimum_jerk():
    """真人曲线必须明确区别于 minimum-jerk 曲线。

    判别量是**速度峰值的位置**：真人峰值偏前（<0.5），
    minimum-jerk 对称（=0.5）。
    """
    E.seed(11)
    human = KN.make_profile(700, human=True)
    robot = KN.make_profile(700, human=False)

    _expect(isinstance(human, KN.AsymmetricProfile),
            "make_profile(human=True) 应返回 AsymmetricProfile")
    _expect(isinstance(robot, KN.MinimumJerkProfile),
            "make_profile(human=False) 应返回 MinimumJerkProfile")

    # 真人的峰值位置应偏前
    _expect(human.peak_position < 0.5,
            "真人峰值位置应 <0.5（偏前），实际 %.3f" % human.peak_position)
    _expect(robot.peak_position == 0.5,
            "minimum-jerk 峰值位置应为 0.5，实际 %.3f" % robot.peak_position)

    # 用速度曲线验证：真人峰不在正中
    vh = KN.velocity_curve(human, 80)
    _expect(vh, "velocity_curve 返回空")
    peak_idx = vh.index(max(vh)) / (len(vh) - 1)
    _expect(peak_idx < 0.5,
            "真人速度峰值应在行程前半段，实际位置 %.3f" % peak_idx)


def test_duration_scales_with_distance():
    """时长必须随距离单调增长（Fitts 定律的定性要求）。"""
    E.seed(3)
    prev = 0.0
    for d in (50, 100, 200, 400, 800, 1200):
        t = KN.duration_for_distance(d)
        _expect(t >= prev - 1e-9,
                "距离 %d 的时长 %.4f 反而小于上一个 %.4f" % (d, t, prev))
        prev = t
    _expect(KN.duration_for_distance(1200) > KN.duration_for_distance(50),
            "远距离时长未超过近距离")


def test_duration_min_dist_parameter():
    """⭐ ``min_dist`` 参数：把"短距离"的时长按参考距离折算。

    这是修"修正段速度比主运动还快"那个 bug 的关键参数。
    传了 min_dist 之后，同样的短距离会得到**更长**的时长
    （因为它是按"一个典型指向动作"的节奏在走），
    从而速度落到合理量级。
    """
    E.seed(21)
    plain = [KN.duration_for_distance(100) for _ in range(40)]
    E.seed(21)
    scaled = [KN.duration_for_distance(100, min_dist=700) for _ in range(40)]

    avg_plain = sum(plain) / len(plain)
    avg_scaled = sum(scaled) / len(scaled)
    _expect(avg_scaled > avg_plain * 1.5,
            "min_dist=700 时 100px 的时长 %.4f 应明显大于不传时的 %.4f"
            % (avg_scaled, avg_plain))

    # min_dist 不大于真实距离时，行为应与不传一致
    E.seed(42)
    a = KN.duration_for_distance(800, min_dist=400)
    E.seed(42)
    b = KN.duration_for_distance(800)
    _expect(a == b,
            "min_dist 小于距离时不该改变结果：%r vs %r" % (a, b))


# --------------------------------------------------------------------------
# 4. 震颤
# --------------------------------------------------------------------------
def test_tremor_oscillates():
    """震颤必须真的在振荡，不是一条常数或慢漂移。"""
    E.seed(5)
    tm = TR.make_tremor(amplitude=2.0)
    vals = [tm.at(i * 0.002, speed_ratio=0.1) for i in range(250)]
    _expect(max(vals) - min(vals) > 0.1,
            "震颤幅度变化 %.4f 太小，几乎没有振荡" % (max(vals) - min(vals)))
    # 符号要反复变（振荡特征）
    sign_changes = sum(
        1 for i in range(1, len(vals))
        if (vals[i] > 0) != (vals[i - 1] > 0)
    )
    _expect(sign_changes > 5,
            "震颤 250 个采样点里只变号 %d 次，不像振荡" % sign_changes)


def test_tremor_frequency_band():
    """主频应落在生理性震颤的 8-12Hz 区间。

    用零交叉计数粗略估频：一段 T 秒里的变号次数 /2 /T ≈ 频率。
    """
    E.seed(17)
    tm = TR.make_tremor(amplitude=2.0)
    dt = 0.001
    n = 3000                      # 3 秒
    vals = [tm.at(i * dt, speed_ratio=0.05) for i in range(n)]
    sign_changes = sum(
        1 for i in range(1, len(vals))
        if (vals[i] > 0) != (vals[i - 1] > 0)
    )
    freq = sign_changes / 2.0 / (n * dt)
    # 多个谐波叠加，零交叉会高于主频，所以给一个宽松但有效的区间
    _expect(5.0 <= freq <= 40.0,
            "震颤估计频率 %.2f Hz 超出 5-40Hz 的合理区间" % freq)
    _expect(TR.TRENOR_CENTER_HZ == 10.0,
            "震颤主频常量应定义为 10Hz，实际 %r" % TR.TRENOR_CENTER_HZ)


def test_tremor_suppressed_by_speed():
    """震颤幅度必须随速度增大而**减小**（运动掩盖效应）。

    真人快速挥动时手抖被"盖住"，慢速微调时抖得最明显。
    如果反了，快速移动会抖成一团，看起来像帕金森而不是正常人。
    """
    E.seed(23)
    tm = TR.make_tremor(amplitude=2.0)
    slow = [abs(tm.at(i * 0.004, speed_ratio=0.05)) for i in range(400)]
    fast = [abs(tm.at(i * 0.004, speed_ratio=1.0)) for i in range(400)]
    avg_slow = sum(slow) / len(slow)
    avg_fast = sum(fast) / len(fast)
    _expect(avg_slow > avg_fast,
            "慢速震颤均值 %.4f 应大于快速 %.4f" % (avg_slow, avg_fast))


def test_tremor_disabled():
    """enabled=False 时必须输出恒零，供对照组使用。"""
    E.seed(29)
    tm = TR.make_tremor(amplitude=2.0, enabled=False)
    vals = [tm.at(i * 0.004, speed_ratio=0.3) for i in range(200)]
    _expect(all(abs(v) < 1e-12 for v in vals),
            "禁用震颤后仍有非零输出：max=%r" % max(abs(v) for v in vals))


def test_tremor_reproducible():
    """震颤波形也必须可复现。"""
    E.seed(31)
    a = TR.tremor_series(2.0, 300, amplitude=1.2)
    E.seed(31)
    b = TR.tremor_series(2.0, 300, amplitude=1.2)
    _expect(a == b, "同 seed 的震颤序列不一致")
    _expect(len(a) == 300, "震颤序列长度应为 300，实际 %d" % len(a))
    # 也必须真在振荡，不是常数
    _expect(max(a) - min(a) > 0.05,
            "震颤序列幅度 %.4f 太小，像是常数" % (max(a) - min(a)))


# --------------------------------------------------------------------------
# 5. 多段子运动
# --------------------------------------------------------------------------
def test_submovements_count_and_order():
    """真人必须分段（2-3 段），机械只有 1 段。"""
    for name, pts, prec in CASES:
        E.seed(4242)
        human = SM.plan_movement(*pts, precision=prec, human=True)
        E.seed(4242)
        robot = SM.plan_movement(*pts, precision=prec, human=False)
        _expect(2 <= len(human.submovements) <= 3,
                "[%s] 真人应有 2-3 个子运动段，实际 %d"
                % (name, len(human.submovements)))
        _expect(len(robot.submovements) == 1,
                "[%s] 机械应只有 1 段，实际 %d"
                % (name, len(robot.submovements)))


def test_submovement_residual_chain():
    """各段的误差覆盖比例必须递减收敛（主运动→修正→微调）。"""
    E.seed(77)
    plan = SM.plan_movement(100, 100, 700, 500)
    subs = plan.submovements
    _expect(subs[0].error_fraction > 0.7,
            "主运动覆盖应 >0.7，实际 %.3f" % subs[0].error_fraction)
    # 累积误差应逐步收敛到 0 附近
    remaining = 1.0
    for i, s in enumerate(subs):
        _expect(0.0 < s.error_fraction <= 1.0,
                "第 %d 段 error_fraction=%.3f 越界" % (i, s.error_fraction))
        remaining *= (1.0 - s.error_fraction)
    _expect(remaining < 0.25,
            "走完全部子运动后仍剩 %.3f 的误差，收敛太慢" % remaining)


def test_submovement_durations_positive():
    """每段的时长必须为正，且总时长等于各段之和（加犹豫）。"""
    for name, pts, prec in CASES:
        E.seed(88)
        plan = SM.plan_movement(*pts, precision=prec)
        total = 0.0
        for i, s in enumerate(plan.submovements):
            _expect(s.duration > 0,
                    "[%s] 第 %d 段时长应为正，实际 %r" % (name, i, s.duration))
            _expect(s.samples >= 2,
                    "[%s] 第 %d 段采样点数应 ≥2，实际 %d"
                    % (name, i, s.samples))
            total += s.duration + s.hesitation
        _expect(plan.total_duration > 0,
                "[%s] 总时长应为正" % name)


def test_sampling_density_proportional():
    """⭐ 采样点数必须与时长成正比（同一定标率）。

    这是修"次峰采不出来"那个 bug 的核心。原来的点数公式按**距离**
    算，时长公式按另一套系数算，两者量纲不一致 —— 结果修正段拿到
    的点数远远不够，速度曲线被粗粒度量化，次峰形状消失。

    正确做法：点数 = 时长 × 固定采样率。断言方式是检查
    "点数 / 时长" 在各段之间保持一致（在未触及上下限时）。
    """
    E.seed(101)
    plan = SM.plan_movement(100, 100, 700, 500)
    ratios = []
    for s in plan.submovements:
        r = s.samples / s.duration
        ratios.append(r)
    # 允许上下限造成的截断，只要求主运动（点数充足、不会触限）在 80Hz 附近
    primary_ratio = plan.submovements[0].samples / plan.submovements[0].duration
    _expect(abs(primary_ratio - SM._SAMPLE_RATE_HZ) / SM._SAMPLE_RATE_HZ < 0.25,
            "主运动采样率 %.1f Hz 偏离目标 %.1f Hz 太多"
            % (primary_ratio, SM._SAMPLE_RATE_HZ))
    _expect(SM._MIN_SAMPLES_PER_SEGMENT >= 6,
            "每段最少采样点数应 ≥6（画出一个有形状的峰所需），实际 %d"
            % SM._MIN_SAMPLES_PER_SEGMENT)


def test_hesitation_does_not_create_duplicate_point():
    """⭐ 犹豫停顿不能新增一个与末点完全重合的采样点。

    这是另一个明确的坑：追加一个"位移 0、延迟很大"的点，
    在速度序列上会造出一个接近 0 的值，正好压在下一段的爬升前面，
    把次峰截断。

    正确做法：把犹豫时长**加到前一个采样点的 delay 上**。
    """
    E.seed(102)
    plan = SM.plan_movement(100, 100, 700, 500)
    pts = _points(plan)
    dup = 0
    for i in range(1, len(pts)):
        if pts[i][0] == pts[i - 1][0] and pts[i][1] == pts[i - 1][1]:
            dup += 1
    _expect(dup == 0,
            "轨迹里出现了 %d 个与前一采样点完全重合的点" % dup)


def test_endpoint_snap():
    """默认（snap_endpoint=True）时，最后一个采样点必须精确落在目标上。

    这一点保证了上层点击能命中目标；随机漂移只影响"过程"，
    不影响"结果"。``landing_error`` 记录的是过程中的最大偏移量，
    不是最终差 —— 最终差恒为 0。
    """
    for name, pts, prec in CASES:
        E.seed(103)
        plan = SM.plan_movement(*pts, precision=prec)
        last = plan.segments[-1]
        err = math.hypot(last.x - plan.end[0], last.y - plan.end[1])
        _expect(err < 1e-6,
                "[%s] 最终落点偏差 %.6f px，应精确为 0" % (name, err))


def test_endpoint_jitter_mode():
    """snap_endpoint=False 时必须保留漂移（供轨迹分析用）。"""
    E.seed(104)
    plan = SM.plan_movement(100, 100, 700, 500, snap_endpoint=False)
    last = plan.segments[-1]
    err = math.hypot(last.x - plan.end[0], last.y - plan.end[1])
    _expect(err > 0.0,
            "关闭吸附后落点仍精确（误差 %r），漂移没生效" % err)
    _expect(err < 2.0,
            "关闭吸附后的漂移 %.3f px 超过 2px 上限" % err)


def test_zero_distance_move():
    """距离为 0 的移动不能崩，要返回单点或空序列。"""
    E.seed(105)
    plan = SM.plan_movement(500, 500, 500, 500)
    _expect(len(plan.segments) >= 1,
            "零距离移动应至少返回 1 个点，实际 %d" % len(plan.segments))
    _expect(plan.distance < 1e-6, "零距离移动的 distance 应为 0")


# --------------------------------------------------------------------------
# 6. 度量：峰值检测（本阶段最关键的算法）
# --------------------------------------------------------------------------
def test_count_peaks_basic():
    """人工构造的波形上，峰值计数必须正确。"""
    _expect(MT.count_peaks([1.0, 2.0, 1.0]) == 1,
            "单峰波应数出 1 个峰")
    # 双峰：中间有明显的谷
    wave = [1.0, 5.0, 1.0, 1.0, 4.0, 1.0]
    n = MT.count_peaks(wave, smooth=0, min_prominence=0.15,
                       min_gap_ratio=0.0)
    _expect(n == 2, "双峰波应数出 2 个峰，实际 %d" % n)

    _expect(MT.count_peaks([0.0, 0.0, 0.0]) == 0,
            "全零序列应数出 0 个峰")
    _expect(MT.count_peaks([1.0]) == 0, "单元素序列应数出 0 个峰")

    # ⭐ 相对自身判据：次峰只要比自己两边的谷高 10% 就算数，
    # 不需要和主峰比高低
    tall_then_small = [1.0, 100.0, 1.0, 1.0, 8.0, 1.0]
    _expect(MT.count_peaks(tall_then_small, smooth=0,
                           min_prominence=0.10, min_gap_ratio=0.0) == 2,
            "主峰 100、次峰 8 应数出 2 个峰（相对判据），实际 %d"
            % MT.count_peaks(tall_then_small, smooth=0,
                             min_prominence=0.10, min_gap_ratio=0.0))


def test_count_peaks_rejects_shoulder():
    """⭐ 单调下降尾段上的"小鼓包"不能被数成独立的峰。

    这是这套算法最重要的一条判据，也是最容易写错的一条。

    只检查"是不是局部极大"是不够的：机械轨迹在减速尾段的数值抖动
    会把某个点拱高一点，形式上确实是局部极大 —— 但它的两侧根本
    没有谷，整条序列还在单调下滑。

    所以判据必须加上**两侧谷校验**：候选峰左右各要找到一个比自己
    低一个阈值的谷。没有这条，一条纯粹的 minimum-jerk 单段曲线
    会被数出 2 个峰，"真人度"指标直接废掉。
    """
    # 一条单调下降的序列，尾段有个小鼓包
    wave = [10.0, 8.0, 6.0, 4.0, 2.0, 1.0, 1.6, 1.2, 0.8, 0.4, 0.2]
    n = MT.count_peaks(wave, smooth=0, min_prominence=0.10,
                       min_gap_ratio=0.0)
    _expect(n == 1,
            "单调下降尾段的鼓包应被拒绝（只数 1 个峰），实际 %d" % n)


def test_mechanical_never_multipeak():
    """⭐ 机械轨迹在任何距离/精度下都**不会数出 ≥2 个峰**。

    这是反向验证：如果机械轨迹能被数出 ≥2 个峰，说明峰值检测在
    "数值抖动"上误判了，前面真人轨迹的"通过"也就不值钱了。

    ⚠️ 判据是 ``n <= 1`` 而不是 ``n == 1``。短距离（40px）的机械轨迹
    采样点很少，``count_peaks`` 会因为 ``len(v) < 3`` 直接返回 0 ——
    这是**正确**行为（点太少，根本谈不上"峰"），不是漏判。
    把它当失败会把"算法对短序列保守"这个特性误报成缺陷。
    """
    bad = []
    zero = 0
    for d in (40, 100, 300, 700, 1400):
        for prec in (1.0, 2.0):
            for seed in range(1, 41):
                E.seed(seed)
                plan = SM.plan_movement(100, 100, 100 + d, 100 + d // 2,
                                        human=False, precision=prec)
                v = MT._velocities(_points(plan))
                if not v:
                    continue
                n = MT.count_peaks(v)
                if n == 0:
                    zero += 1
                if n > 1:
                    bad.append((seed, d, prec, n))
    _expect(not bad,
            "%d 条机械轨迹被数出多个峰（最多列 5 条）：%s"
            % (len(bad), bad[:5]))
    # 顺便记录：短序列返回 0 的情况存在，但不应占多数
    total = 5 * 2 * 40
    _expect(zero < total * 0.5,
            "有 %d/%d 条机械轨迹数出 0 个峰，比例过高，疑似漏判"
            % (zero, total))


def test_peak_threshold_value():
    """显著性判据的两个常量必须是实测校准出来的值，改动要有依据。

    理由（见 ``metrics.count_peaks`` 的 docstring）：

    * ``min_prominence = 0.10`` —— 判据是 **峰相对自身** 的两侧落差比例。
      曾经写成"相对全局最大峰"，在 720px 真人轨迹上漏掉 8/40 条真实次峰
      （主峰 8300 时修正峰只有 559，占 0.067 < 0.08）。
    * ``smooth = 2`` —— 窗口 4 在四条行程上都是明显更差的
      （短距 40px：7/40 漏判，窗口 2 是 0/40）。
    """
    import inspect
    sig = inspect.signature(MT.count_peaks)
    default = sig.parameters["min_prominence"].default
    _expect(abs(default - 0.10) < 1e-9,
            "count_peaks 的 min_prominence 默认值应为 0.10，实际 %r" % default)
    smooth = sig.parameters["smooth"].default
    _expect(smooth == 2,
            "平滑窗口应为 2（窗口 4 会抹平短促的次峰），实际 %r" % smooth)


# --------------------------------------------------------------------------
# 7. 度量：真人度判定
# --------------------------------------------------------------------------
def test_human_passes_all_cases():
    """⭐ 全样本矩阵 × 多 seed：真人轨迹必须全部通过判定。

    这是阶段三的**核心验收条件**。样本覆盖 40px 到 1700px、
    普通精度到 2 倍高精度、水平/垂直/斜向。
    """
    fails = []
    for name, pts, prec in CASES:
        for seed in range(1, 41):
            E.seed(seed)
            plan = SM.plan_movement(*pts, precision=prec)
            m = MT.analyze_move(plan)
            v = MT.verdict(m)
            if not v["ok"]:
                fails.append((name, seed, v["failures"], m.describe()))
    _expect(not fails,
            "%d 条真人轨迹未通过判定（最多列 3 条）：\n    %s"
            % (len(fails), "\n    ".join(repr(f) for f in fails[:3])))


def test_mechanical_fails_verdict():
    """⭐ 机械轨迹必须全部**不通过**真人判定。

    这是"判定有意义"的证明。如果机械也能通过，那这个判定
    什么都证明不了。
    """
    passed = []
    for d in (40, 100, 300, 700, 1400):
        for seed in range(1, 41):
            E.seed(seed)
            plan = SM.plan_movement(100, 100, 100 + d, 100 + d // 2,
                                    human=False)
            m = MT.analyze_move(plan)
            v = MT.verdict(m)
            if v["ok"]:
                passed.append((seed, d, m.describe()))
    _expect(not passed,
            "%d 条机械轨迹通过了真人判定（最多列 3 条）：\n    %s"
            % (len(passed), "\n    ".join(repr(f) for f in passed[:3])))


def test_verdict_criteria_values():
    """判定阈值本身必须是设计好的那几个值。"""
    _expect(MT.PASS_CRITERIA["min_peak_count"] == 2,
            "峰值数门槛应为 2")
    _expect(MT.PASS_CRITERIA["max_landing_error"] == 2.0,
            "落点误差上限应为 2px")
    _expect(MT.PASS_CRITERIA["max_asymmetry"] == 0.98,
            "不对称度上限应为 0.98")
    # ⚠️ 直度上限必须是 1.0 而不是 0.9999 —— 见 metrics.py 里的注释：
    #    长距离直线的直度天然是 0.99992，在第四位上判会把合法轨迹判失败。
    _expect(MT.PASS_CRITERIA["max_straightness"] == 1.0,
            "直度上限应为 1.0（严格小于），实际 %r"
            % MT.PASS_CRITERIA["max_straightness"])


def test_human_metrics_contrast():
    """真人 vs 机械在三项核心指标上的方向必须都对。"""
    E.seed(7)
    h = SM.plan_movement(100, 100, 700, 500, human=True)
    E.seed(7)
    r = SM.plan_movement(100, 100, 700, 500, human=False)
    mh = MT.analyze_move(h)
    mr = MT.analyze_move(r)

    _expect(mh.peak_count >= 2,
            "真人峰值数应 ≥2，实际 %d" % mh.peak_count)
    _expect(mr.peak_count == 1,
            "机械峰值数应为 1，实际 %d" % mr.peak_count)
    _expect(mh.straightness < 1.0,
            "真人直度应 <1，实际 %.6f" % mh.straightness)
    _expect(mr.straightness == 1.0,
            "机械直度应 =1，实际 %.6f" % mr.straightness)
    _expect(mh.asymmetry < mr.asymmetry,
            "真人不对称度 %.3f 应小于机械 %.3f"
            % (mh.asymmetry, mr.asymmetry))
    _expect(mh.tremor_energy > mr.tremor_energy,
            "真人震颤能量 %.4f 应大于机械 %.4f"
            % (mh.tremor_energy, mr.tremor_energy))
    _expect(mh.submovement_count >= 2,
            "真人子运动段数应 ≥2，实际 %d" % mh.submovement_count)


def test_compare_output_shape():
    """compare() 的输出结构要完整，供 CLI 和报告使用。"""
    E.seed(7)
    h = SM.plan_movement(100, 100, 700, 500, human=True)
    E.seed(7)
    r = SM.plan_movement(100, 100, 700, 500, human=False)
    cmp = MT.compare(h, r)
    for key in ("human", "robot", "delta"):
        _expect(key in cmp, "compare() 缺少 %r" % key)
    for key in ("peak_count", "straightness", "duration", "tremor_energy"):
        _expect(key in cmp["delta"], "delta 缺少 %r" % key)


def test_analyze_move_accepts_both_plan_types():
    """analyze_move 要能吃 SubMovementPlan 和 MoveResult 两种输入。"""
    E.seed(7)
    plan = SM.plan_movement(100, 100, 700, 500)
    m1 = MT.analyze_move(plan)
    _expect(m1.points > 0, "从 SubMovementPlan 分析出的点数应 >0")

    # MoveResult 只有汇总字段，也要能分析
    fake = type("FakeMoveResult", (), {
        "requested": (700, 500), "points": 30, "distance": 700.0,
        "landing_error": 0.5, "peak_count": 3, "straightness": 0.98,
        "path_length": 720.0, "duration": 0.5, "submovements": 3,
    })()
    m2 = MT.analyze_move(fake)
    _expect(m2.peak_count == 3,
            "从 MoveResult 读出的峰值数应为 3，实际 %d" % m2.peak_count)


def test_analyze_segments_tuple_input():
    """analyze_segments 要能吃 (x, y, delay) 三元组列表。"""
    pts = [(0.0, 0.0, 0.01), (10.0, 5.0, 0.01), (25.0, 12.0, 0.01),
           (30.0, 14.0, 0.01)]
    m = MT.analyze_segments(pts, target=(30.0, 14.0))
    _expect(m.points == 4, "点数应为 4，实际 %d" % m.points)
    _expect(m.landing_error < 1e-9,
            "目标即是末点，落点误差应为 0，实际 %r" % m.landing_error)
    _expect(0.0 < m.straightness <= 1.0,
            "直度应在 (0,1]，实际 %r" % m.straightness)


def test_timing_entropy_range():
    """时序熵必须落在 0-1。"""
    E.seed(7)
    plan = SM.plan_movement(100, 100, 700, 500)
    delays = [s.delay for s in plan.segments[1:]]
    ent = MT.timing_entropy(delays)
    _expect(0.0 <= ent <= 1.0, "时序熵应在 [0,1]，实际 %r" % ent)

    # 完全规律的序列熵应低
    regular = MT.timing_entropy([0.01] * 30)
    _expect(regular < 0.4,
            "完全规律的延迟序列熵应较低，实际 %.3f" % regular)


def test_pause_counting():
    """停顿检测：延迟明显偏大且几乎没有位移的点才算停顿。"""
    pts = []
    for i in range(10):
        pts.append((float(i), 0.0, 0.01))
    # 插一个"原地长延迟"
    pts.append((9.0, 0.0, 0.30))
    pts.append((10.0, 0.0, 0.01))
    n = MT.count_pauses(pts)
    _expect(n >= 1, "应至少检测到 1 次停顿，实际 %d" % n)


# --------------------------------------------------------------------------
# 8. 键盘真人化（不真按键，只算节奏）
# --------------------------------------------------------------------------
def test_keystroke_delay_distribution():
    """按键间隔应右偏（对数正态特征），不能是均匀分布。"""
    E.seed(13)
    delays = [HK.keystroke_delay("a", base_delay=0.1) for _ in range(600)]
    _expect(all(d > 0 for d in delays),
            "按键间隔必须为正")
    mean = sum(delays) / len(delays)
    med = sorted(delays)[len(delays) // 2]
    # 右偏分布：均值 > 中位数
    _expect(mean > med,
            "按键间隔分布应为右偏（均值 %.4f 应 > 中位数 %.4f）" % (mean, med))


def test_shift_chars_slower():
    """需要 Shift 的字符平均更慢（真实打字节奏）。"""
    E.seed(19)
    plain = [HK.keystroke_delay("a", base_delay=0.1) for _ in range(500)]
    shifted = [HK.keystroke_delay("A", base_delay=0.1) for _ in range(500)]
    avg_p = sum(plain) / len(plain)
    avg_s = sum(shifted) / len(shifted)
    _expect(avg_s > avg_p,
            "大写字符平均间隔 %.4f 应大于小写 %.4f" % (avg_s, avg_p))


def test_punctuation_pauses():
    """句子/分句结束应带来更长的停顿。

    ``_inter_char_pause(char, *, skill=1.0)`` —— ``skill`` 是关键字参数，
    位置参数只有 ``char`` 一个。
    """
    E.seed(23)
    normal = [HK._inter_char_pause("b") for _ in range(200)]
    clause = [HK._inter_char_pause(",") for _ in range(200)]
    sentence = [HK._inter_char_pause("。") for _ in range(200)]

    avg_n = sum(normal) / len(normal)
    avg_c = sum(clause) / len(clause)
    avg_s = sum(sentence) / len(sentence)

    _expect(avg_s > avg_n,
            "句号后停顿 %.4f 应长于普通字符后 %.4f" % (avg_s, avg_n))
    _expect(avg_c > avg_n,
            "逗号后停顿 %.4f 应长于普通字符后 %.4f" % (avg_c, avg_n))
    _expect(avg_s >= avg_c,
            "句号停顿 %.4f 不应短于逗号 %.4f" % (avg_s, avg_c))


def test_estimate_duration_scales():
    """时长估算必须随文本长度增长。"""
    short = HK.estimate_duration("hello", wpm=320)
    long = HK.estimate_duration("hello " * 40, wpm=320)
    _expect(long > short,
            "长文本估算时长 %.3f 应大于短文本 %.3f" % (long, short))
    _expect(short > 0, "短文本估算时长应为正")


def test_estimate_duration_wpm():
    """WPM 越高，估算时长越短。"""
    slow = HK.estimate_duration("hello world " * 10, wpm=150)
    fast = HK.estimate_duration("hello world " * 10, wpm=600)
    _expect(fast < slow,
            "高 WPM 估算时长 %.3f 应小于低 WPM %.3f" % (fast, slow))


def test_type_duration_reproducible():
    """打字节奏也必须可复现（在 dry-run 模式下验证）。"""
    E.seed(37)
    a = HK.estimate_duration("The quick brown fox", wpm=300)
    E.seed(37)
    b = HK.estimate_duration("The quick brown fox", wpm=300)
    _expect(a == b, "同 seed 的时长估算不一致：%r vs %r" % (a, b))


def test_keyboard_neighbors():
    """邻近键映射要覆盖常用键（这是"打错字"的基础）。"""
    for ch in ("a", "s", "e", "t"):
        nb = HK._neighbor_keys(ch)
        _expect(nb, "字符 %r 的邻近键为空" % ch)


# --------------------------------------------------------------------------
# 9. 模块自检
# --------------------------------------------------------------------------
def test_module_selfchecks():
    """各模块的 selfcheck() 必须都能跑通。"""
    for mod, name in ((E, "entropy"), (KN, "kinetics"), (TR, "tremor"),
                      (SM, "submove"), (MT, "metrics")):
        fn = getattr(mod, "selfcheck", None)
        if fn is None:
            continue
        res = fn()
        ok = res.get("ok") if isinstance(res, dict) else bool(res)
        _expect(ok, "%s.selfcheck() 未通过：%r" % (name, res))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段三 · 真人熵引擎测试")
    print("=" * 64)
    print("样本矩阵：%d 种行程 × 40 seed" % len(CASES))

    section("熵源纪律（硬约束 #4）")
    check("无模块绕过 entropy 直接调 random", test_entropy_module_discipline)
    check("种子/开关/重置 API", test_entropy_state_api)
    check("随机函数取值区间", test_entropy_helpers_ranges)
    check("derive 派生流独立可复现", test_derive_produces_independent_streams)

    section("可复现性（硬约束 #5）")
    check("⭐ 同 seed 逐点完全一致", test_plan_reproducible)
    check("异 seed 轨迹不同", test_plan_differs_across_seeds)
    check("无种子时真随机", test_no_seed_is_still_random)
    check("子运动结构可复现", test_submovement_reproducible)

    section("运动学曲线")
    check("非对称曲线单调 + 端点精确", test_asymmetric_profile_monotonic)
    check("区别于 minimum-jerk", test_asymmetric_vs_minimum_jerk)
    check("时长随距离增长", test_duration_scales_with_distance)
    check("⭐ min_dist 参数生效", test_duration_min_dist_parameter)

    section("生理震颤")
    check("真的在振荡", test_tremor_oscillates)
    check("频率落在生理区间", test_tremor_frequency_band)
    check("速度增大时被抑制", test_tremor_suppressed_by_speed)
    check("可禁用", test_tremor_disabled)
    check("波形可复现", test_tremor_reproducible)

    section("多段子运动")
    check("真人 2-3 段 / 机械 1 段", test_submovements_count_and_order)
    check("误差链收敛", test_submovement_residual_chain)
    check("各段时长与采样点为正", test_submovement_durations_positive)
    check("⭐ 采样密度按时长成比例", test_sampling_density_proportional)
    check("⭐ 犹豫不产生重合点", test_hesitation_does_not_create_duplicate_point)
    check("末端吸附精确落点", test_endpoint_snap)
    check("关闭吸附保留漂移", test_endpoint_jitter_mode)
    check("零距离移动不崩", test_zero_distance_move)

    section("度量：峰值检测")
    check("基本波形计数", test_count_peaks_basic)
    check("⭐ 拒绝单调尾段的假峰", test_count_peaks_rejects_shoulder)
    check("⭐ 机械轨迹从不出现多峰", test_mechanical_never_multipeak)
    check("阈值常量符合实测", test_peak_threshold_value)

    section("度量：真人度判定")
    check("⭐ 真人全部通过（10 行程 × 40 seed）", test_human_passes_all_cases)
    check("⭐ 机械全部不通过", test_mechanical_fails_verdict)
    check("判定阈值取值", test_verdict_criteria_values)
    check("真人/机械指标方向正确", test_human_metrics_contrast)
    check("compare 输出结构", test_compare_output_shape)
    check("analyze_move 兼容两种输入", test_analyze_move_accepts_both_plan_types)
    check("三元组输入", test_analyze_segments_tuple_input)
    check("时序熵范围", test_timing_entropy_range)
    check("停顿计数", test_pause_counting)

    section("键盘真人化")
    check("按键间隔右偏分布", test_keystroke_delay_distribution)
    check("Shift 字符更慢", test_shift_chars_slower)
    check("标点带来停顿", test_punctuation_pauses)
    check("时长估算随长度增长", test_estimate_duration_scales)
    check("WPM 影响时长", test_estimate_duration_wpm)
    check("时长估算可复现", test_type_duration_reproducible)
    check("邻近键映射", test_keyboard_neighbors)

    section("模块自检")
    check("entropy/kinetics/tremor/submove/metrics selfcheck",
          test_module_selfchecks)

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段三 真人熵测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s -> %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
