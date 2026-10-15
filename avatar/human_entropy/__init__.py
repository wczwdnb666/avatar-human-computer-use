# -*- coding: utf-8 -*-
"""真人熵引擎：让 Avatar 的每次操作在行为特征上像真人。

四个独立机制
------------
``entropy``
    **熵源统一管理**。所有随机数从这里取 —— 这是"可复现"的前提。
    支持 ``entropy.seed(42)``，同一 seed + 同一路径 = 同一轨迹。
``kinetics``
    **非对称速度曲线**。快加速、慢减速。速度峰值出现在行程的
    40-55% 处，加速段短于减速段。
``tremor``
    **微震颤**。8-12 Hz 的低频**相关**噪声（不是白噪声），
    幅度随运动速度变化（运动掩盖效应）。
``submove``
    **多段子运动**。弹道冲刺 → 修正 → 微调，段间有残留误差。
    这是真人轨迹与机械轨迹最本质的区别。

上层封装
--------
``mouse``      把四个机制组合成 move / click / drag / scroll
``keyboard``   打字节奏（对数正态间隔、认知停顿、错字回改）
``metrics``    真人度度量（速度峰值数、路径直度、落点误差）

设计纪律
--------
1. **禁止直接 ``import random``**。质量硬约束 #4。检查见
   ``scripts/selfcheck.py`` 的 ``check_entropy_discipline``。
2. **四个机制必须独立可调**。想只开震颤不开分段？构造
   ``Tremor`` 手动传入，或给各函数传 ``human=`` 开关。
3. **同一 seed 必须可复现**。质量硬约束 #5。

快速上手
--------
::

    from avatar.human_entropy import entropy, mouse

    entropy.seed(42)
    mouse.click_human(800, 450)      # 同 seed 下轨迹完全可复现

    entropy.seed(None)               # 回真随机
    mouse.click_human(800, 450)
"""
from __future__ import annotations

from . import entropy, kinetics, metrics, submove, tremor, verify_hook

__all__ = [
    "entropy",
    "kinetics",
    "tremor",
    "submove",
    "metrics",
    "verify_hook",
]
