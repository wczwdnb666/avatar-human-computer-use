# -*- coding: utf-8 -*-
"""Agent 规划与执行层 —— 阶段六。

## 这一层解决什么

产品哲学是「**除了必要全是自动**」。前五个阶段：
感知 → 操作 → 真人化 → 验证 → 风控。阶段六是"自动"的核心实现层：

    一句话目标 → 自动拆解 → 自动执行 → 自动验证 → 自动恢复

## 模块分工

============================  ==========================================
模块                          职责
============================  ==========================================
:mod:`goal`                   自然语言 → 结构化 Goal（规则解析）
:mod:`plan`                   步骤 + 依赖 → DAG，校验、拓扑排序
:mod:`planner`                Goal → Plan（Backend 可插拔）
:mod:`state`                  状态机（合法转换的唯一入口）
:mod:`memory`                 任务状态持久化（JSON，可断点恢复）
:mod:`observer`               执行前后各观测一次环境
:mod:`verifier`               判断这一步成功没有
:mod:`recovery`               失败时决定：重试 / 换策略 / 上抛
:mod:`executor`               按 DAG 调度，把上面这些串起来
============================  ==========================================

## 依赖方向（严格单向，不成环）

    goal → plan → planner → executor
                    ↑          ↑
                 state ────→ memory
                              ↑
            observer → verifier → recovery

``executor`` 是最上层，它 import 其余全部；其余模块**互不 import 上层**。
这样每一层都能被单独测试。

## 能力边界（整个包）

**做**：目标解析、DAG 规划、状态机、持久化、观测、验证、恢复决策、
DAG 调度执行。

**不做**：

- **不硬解验证题**（硬约束 #9 全项目生效）。遇到验证题一律交给
  阶段五的 :mod:`avatar.risk_detector.guard` 叫人。
- **不做 OCR / 视觉识别**。那是阶段七。
- **不自己实现 LLM 调用**。:class:`~.planner.LLMPlanner` 只负责把模型
  输出转成 Plan（阶段七实现，解析或校验失败时**自动回退规则规划**）；
  HTTP 协议、token 预算、重试都在 :mod:`~.llm_backend` 里。
- **不直接操作文件**。所有持久化走 :class:`~.memory.ShortTermMemory`
  的 ``save()`` / ``load()``。
- **不绕过状态机**。任何状态变更必须过 :func:`~.state.transition`。
"""

from .goal import Goal, parse_goal
from .plan import Plan, PlanError, Step
from .planner import (
    LLMPlanner,
    PlannerBackend,
    PlannerError,
    RuleBasedPlanner,
    get_planner,
)

__all__ = [
    # goal
    "Goal", "parse_goal",
    # plan
    "Plan", "Step", "PlanError",
    # planner
    "PlannerBackend", "RuleBasedPlanner", "LLMPlanner", "PlannerError",
    "get_planner",
]


#: 允许包级 ``__getattr__`` 懒加载的子模块。
_LAZY = {"state", "memory", "observer", "verifier", "recovery", "executor"}

#: 便捷函数/类的来源表。
_FUNCS = {
    # state
    "TaskState": ("state", "TaskState"),
    "StepState": ("state", "StepState"),
    "transition": ("state", "transition"),
    "can_transition": ("state", "can_transition"),
    "describe_state_machine": ("state", "describe_state_machine"),
    # memory
    "ShortTermMemory": ("memory", "ShortTermMemory"),
    "task_dir": ("memory", "task_dir"),
    "list_tasks": ("memory", "list_tasks"),
    # observer
    "observe": ("observer", "observe"),
    "Observation": ("observer", "Observation"),
    # verifier
    "verify": ("verifier", "verify"),
    "VerifierBackend": ("verifier", "VerifierBackend"),
    # recovery
    "decide_recovery": ("recovery", "decide_recovery"),
    "RecoveryStrategy": ("recovery", "RecoveryStrategy"),
    # executor
    "Executor": ("executor", "Executor"),
    "ExecutionResult": ("executor", "ExecutionResult"),
    "StepResult": ("executor", "StepResult"),
    "EXECUTOR_READY": ("executor", "EXECUTOR_READY"),
    "describe_planner": ("executor", "describe_planner"),
}


def __getattr__(name: str):
    """懒加载其余子模块。

    理由与 :mod:`avatar.risk_detector` 一样：``executor`` 会 import
    前面所有模块，顶层直接 import 容易成环，也会让
    ``import avatar.agentic_planner`` 变得很重。
    """
    import importlib

    if name in _LAZY:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod

    if name in _FUNCS:
        mod_name, attr = _FUNCS[name]
        try:
            mod = importlib.import_module("." + mod_name, __name__)
        except ImportError as exc:
            raise AttributeError(
                "%s 尚未实现（导入 %s 失败：%s）" % (name, mod_name, exc))
        obj = getattr(mod, attr, None)
        if obj is None:
            raise AttributeError("%s.%s 不存在" % (mod_name, attr))
        globals()[name] = obj
        return obj

    raise AttributeError("module %r has no attribute %r" % (__name__, name))
