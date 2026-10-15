# -*- coding: utf-8 -*-
"""状态机 —— 任务与步骤的状态流转。

## 为什么要有状态机

因为"自动恢复"的前提是**知道现在在哪一步、哪一步失败了**。
散落的布尔标志（``is_running`` / ``has_failed`` / ``is_done``）会
组合出几十种互相矛盾的状态，而排查问题时你需要的是**一个**确定的答案。

## 两条状态线

    TaskState   PENDING → RUNNING → (SUCCESS | FAILED | BLOCKED)
    StepState   PENDING → RUNNING → (SUCCESS | FAILED | SKIPPED | BLOCKED)

区别在于步骤多了个 ``SKIPPED``（人工选择跳过），
而任务没有 —— "整个任务被跳过"没有意义。

## 关键语义：BLOCKED 与 FAILED 的区别

- **BLOCKED** —— 卡住了，**等外部条件变化**（人处理完验证码、
  依赖的步骤重跑成功）。可以回到 RUNNING。
- **FAILED** —— 这次尝试失败了。**自愈可以把它拉回 RUNNING**
  （这是"再试一次"，是新的一次尝试，``attempts`` 会 +1）。

⚠ 注意这里和直觉不同：``StepState.FAILED`` **不是终态**。
如果它是终态，自愈就没法工作了 —— 而自愈是阶段六的核心目标。
真正"不可再试"的判断在 :mod:`recovery`（它看的是 attempts 上限），
不是状态机。

``TaskState.FAILED`` 才是终态 —— 整个任务放弃了，不会再有下一次。

## 硬约束 4：禁止直接赋值

任何状态变更**必须**过 :func:`transition`。原因：
直接赋值不会检查合法性，``SUCCESS → RUNNING`` 这种回退
在代码里不会报错，只会在某天让任务莫名其妙重跑一遍。

## 能力边界

**做**：定义状态集合；校验并执行状态转换；给"能不能转"的纯查询；
画状态机说明。

**不做**：

- **不持久化**。状态存盘是 :mod:`memory` 的事。
- **不决定该转到哪**。本模块只回答"能不能转"，
  "该不该重试"是 :mod:`recovery` 的判断。
- **不计数**。``attempts`` / ``strategy_switches`` 由调用方维护 ——
  状态机不管业务计数。
- **不监控超时**。全局超时是 :mod:`executor` 的事。
- **不做并发控制**。多个线程同时 transition 同一个状态不在保护范围内
  （DAG 调度保证同一步骤不会被并行执行两次）。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "TaskState",
    "StepState",
    "StateError",
    "EVENT_START",
    "EVENT_SUCCEED",
    "EVENT_FAIL",
    "EVENT_SKIP",
    "EVENT_BLOCK",
    "EVENT_RESUME",
    "EVENT_RETRY",
    "EVENT_ABORT",
    "EVENTS",
    "TASK_TRANSITIONS",
    "STEP_TRANSITIONS",
    "transition",
    "can_transition",
    "transition_or_raise",
    "is_terminal",
    "describe_state_machine",
    "selfcheck",
]


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
class TaskState(str, Enum):
    """任务级状态。**FAILED 是终态。**

    继承 ``str`` 是为了让 ``TaskState.PENDING == "pending"`` 成立、
    且 ``json.dumps`` 直接可用 —— 否则每个序列化点都要手写转换。
    """

    PENDING = "pending"       # 已创建，还没开始
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 全部完成
    FAILED = "failed"         # 放弃（终态）
    BLOCKED = "blocked"       # 卡住等人/等条件（可恢复）

    def __str__(self) -> str:
        return self.value


class StepState(str, Enum):
    """步骤级状态。

    ``FAILED`` **不是终态** —— 自愈可以把它拉回 RUNNING。见模块文档。
    """

    PENDING = "pending"       # 还没轮到我
    RUNNING = "running"       # 正在执行
    SUCCESS = "success"       # 成功
    FAILED = "failed"         # 这次失败了（可重试）
    SKIPPED = "skipped"       # 人工选择跳过
    BLOCKED = "blocked"       # 卡住（可恢复）

    def __str__(self) -> str:
        return self.value


class StateError(Exception):
    """非法状态转换。**带完整上下文**，方便定位是哪一步转错了。"""

    def __init__(self, message: str, *, current: Any = None,
                 event: str = "", allowed: Optional[List[str]] = None):
        super().__init__(message)
        self.current = current
        self.event = event
        self.allowed = list(allowed or [])

    def __str__(self) -> str:
        base = super().__str__()
        if self.allowed:
            base += "（当前状态下可用事件：%s）" % ", ".join(self.allowed)
        return base


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
EVENT_START = "start"        # 开始执行
EVENT_SUCCEED = "succeed"    # 执行成功
EVENT_FAIL = "fail"          # 执行失败
EVENT_SKIP = "skip"          # 跳过
EVENT_BLOCK = "block"        # 卡住
EVENT_RESUME = "resume"      # 恢复（人处理完了 / 条件满足了）
EVENT_RETRY = "retry"        # 自愈：再试一次
EVENT_ABORT = "abort"        # 放弃

EVENTS = (EVENT_START, EVENT_SUCCEED, EVENT_FAIL, EVENT_SKIP, EVENT_BLOCK,
          EVENT_RESUME, EVENT_RETRY, EVENT_ABORT)


# --------------------------------------------------------------------------
# 转换表
# --------------------------------------------------------------------------
#: 任务级转换表。``(当前状态, 事件) -> 新状态``
TASK_TRANSITIONS: Dict[Tuple[str, str], str] = {
    (TaskState.PENDING, EVENT_START): TaskState.RUNNING,

    (TaskState.RUNNING, EVENT_SUCCEED): TaskState.SUCCESS,
    (TaskState.RUNNING, EVENT_FAIL): TaskState.FAILED,
    (TaskState.RUNNING, EVENT_BLOCK): TaskState.BLOCKED,
    (TaskState.RUNNING, EVENT_ABORT): TaskState.FAILED,

    # BLOCKED 可恢复 —— 这是"熔断后继续"的落点
    (TaskState.BLOCKED, EVENT_RESUME): TaskState.RUNNING,
    (TaskState.BLOCKED, EVENT_SUCCEED): TaskState.SUCCESS,
    (TaskState.BLOCKED, EVENT_FAIL): TaskState.FAILED,
    (TaskState.BLOCKED, EVENT_ABORT): TaskState.FAILED,
}

#: 步骤级转换表。
STEP_TRANSITIONS: Dict[Tuple[str, str], str] = {
    (StepState.PENDING, EVENT_START): StepState.RUNNING,
    (StepState.PENDING, EVENT_SKIP): StepState.SKIPPED,
    (StepState.PENDING, EVENT_ABORT): StepState.FAILED,

    (StepState.RUNNING, EVENT_SUCCEED): StepState.SUCCESS,
    (StepState.RUNNING, EVENT_FAIL): StepState.FAILED,
    (StepState.RUNNING, EVENT_SKIP): StepState.SKIPPED,
    (StepState.RUNNING, EVENT_BLOCK): StepState.BLOCKED,
    (StepState.RUNNING, EVENT_ABORT): StepState.FAILED,

    # ★ 自愈的关键两条：FAILED 和 BLOCKED 都能回到 RUNNING
    (StepState.FAILED, EVENT_RETRY): StepState.RUNNING,
    (StepState.FAILED, EVENT_ABORT): StepState.FAILED,   # 幂等，便于重复调用

    (StepState.BLOCKED, EVENT_RESUME): StepState.RUNNING,
    (StepState.BLOCKED, EVENT_RETRY): StepState.RUNNING,
    (StepState.BLOCKED, EVENT_SUCCEED): StepState.SUCCESS,
    (StepState.BLOCKED, EVENT_FAIL): StepState.FAILED,
    (StepState.BLOCKED, EVENT_SKIP): StepState.SKIPPED,
    (StepState.BLOCKED, EVENT_ABORT): StepState.FAILED,
}

#: 作用域常量。**必须显式指定**，因为 ``pending`` / ``running`` /
#: ``failed`` 这些值在任务级和步骤级**都存在** —— 光看字符串
#: 分不清是哪一个。这是设计上真实存在的歧义，不能靠猜。
SCOPE_TASK = "task"
SCOPE_STEP = "step"
SCOPES = (SCOPE_TASK, SCOPE_STEP)

#: 默认作用域。步骤级转换是绝大多数调用点（执行器每步都要转），
#: 任务级只有执行器顶层用 —— 让它显式传 ``scope="task"`` 不麻烦。
SCOPE_DEFAULT = SCOPE_STEP

#: 终态。**注意步骤级和任务级不同** —— 见 :func:`is_terminal`。
_TERMINAL_TASK = (str(TaskState.SUCCESS), str(TaskState.FAILED))
_TERMINAL_STEP = (str(StepState.SUCCESS), str(StepState.SKIPPED))


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _coerce(state: Any) -> str:
    """把状态规整成字符串值。**认不出来就抛。**

    为什么要抛：一个拼错的状态名（``"runing"``）如果被静默接受，
    后面所有基于状态的判断都会走错分支，而且**不会报错**。
    """
    if isinstance(state, Enum):
        return str(state.value)
    if isinstance(state, str):
        return state
    raise StateError("无法识别的状态：%r（类型 %s）"
                     % (state, type(state).__name__), current=state)


def _norm_scope(scope: Any) -> str:
    s = str(scope or SCOPE_DEFAULT).strip().lower()
    if s in ("task", "tasks"):
        return SCOPE_TASK
    if s in ("step", "steps"):
        return SCOPE_STEP
    raise StateError("未知作用域：%r（可用：task / step）" % (scope,))


def _table_for(scope: str) -> Dict[Tuple[str, str], str]:
    return TASK_TRANSITIONS if scope == SCOPE_TASK else STEP_TRANSITIONS


def _valid_states(scope: str) -> set:
    cls = TaskState if scope == SCOPE_TASK else StepState
    return {str(x.value) for x in cls}


def _allowed_events(state: str, scope: str) -> List[str]:
    table = _table_for(scope)
    return sorted(e for (s, e) in table if s == state)


# --------------------------------------------------------------------------
# 主接口
# --------------------------------------------------------------------------
def can_transition(state: Any, event: str, *,
                   scope: str = SCOPE_DEFAULT) -> bool:
    """``state`` 下能响应 ``event`` 吗？**纯查询，不抛异常。**

    认不出的状态/事件/作用域返回 ``False``（而不是抛）——
    这个函数常被用在"先问问能不能"的场景里，抛异常不好用。
    """
    try:
        s = _coerce(state)
        sc = _norm_scope(scope)
    except StateError:
        return False
    if s not in _valid_states(sc):
        return False
    return (s, str(event or "")) in _table_for(sc)


def transition(state: Any, event: str,
               *, scope: str = SCOPE_DEFAULT) -> str:
    """执行状态转换。**非法转换抛 :class:`StateError`。**

    ## 参数

    - ``state`` —— 当前状态（:class:`TaskState` / :class:`StepState` /
      对应的字符串都收）
    - ``event`` —— 事件名（``start`` / ``succeed`` / ``fail`` /
      ``skip`` / ``block`` / ``resume`` / ``retry`` / ``abort``）
    - ``scope`` —— ``"task"`` 或 ``"step"``，**默认 ``"step"``**。

    ## ⚠ 为什么必须有 scope

    因为 ``pending`` / ``running`` / ``success`` / ``failed`` /
    ``blocked`` 这五个值**在两个枚举里都有**。光看字符串分不清
    是"任务的 running"还是"步骤的 running" —— 而它们的转换表**不同**
    （比如 ``running --skip--> skipped`` 只在步骤级合法）。

    不加 scope 就只能猜，猜错的后果是：任务级的非法转换被放行，
    或者步骤级的合法转换被拒。

    ## 返回

    **新状态的字符串值**（不是 Enum）。这样调用方可以直接塞进
    ``Step.state``，不用到处 ``.value``。

    ## 幂等性

    在终态上调 ``abort`` 会返回它自己（不抛）。理由：
    执行器可能在"已经失败"之后又收到一次放弃请求，
    那不该是错误。
    """
    s = _coerce(state)
    sc = _norm_scope(scope)
    e = str(event or "").strip()

    if s not in _valid_states(sc):
        raise StateError(
            "状态 %r 不属于 %s 作用域（%s 可用：%s）"
            % (s, sc, sc, sorted(_valid_states(sc))),
            current=s, event=e, allowed=_allowed_events(s, sc)
            if s in _valid_states(sc) else [])

    if not e:
        raise StateError("事件为空", current=s, allowed=_allowed_events(s, sc))

    table = _table_for(sc)
    key = (s, e)

    if key in table:
        return str(table[key])

    # 幂等：终态上重复 abort 不算错
    terminal = _TERMINAL_TASK if sc == SCOPE_TASK else _TERMINAL_STEP
    if e == EVENT_ABORT and s in terminal:
        return s

    raise StateError(
        "非法状态转换：%s --%s--> ?" % (s, e), current=s, event=e,
        allowed=_allowed_events(s, sc))


def transition_or_raise(state: Any, event: str, *,
                        scope: str = SCOPE_DEFAULT) -> str:
    """:func:`transition` 的别名 —— 让调用点的意图更明显。

    存在的意义是**可读性**：在"这里必须成功"的地方写
    ``transition_or_raise``，读者一眼就知道失败会抛，
    不用去翻 ``transition`` 的文档确认。
    """
    return transition(state, event, scope=scope)


def is_terminal(state: Any, *, scope: str = SCOPE_DEFAULT) -> bool:
    """是终态吗？

    - **任务级**：``SUCCESS`` / ``FAILED``
    - **步骤级**：``SUCCESS`` / ``SKIPPED``

    ⚠ **同一个 ``"failed"``，在两级下结论相反**：

    - 任务 ``failed`` → 终态（整个任务放弃了）
    - 步骤 ``failed`` → **不是**终态（自愈要能把它拉回 RUNNING）

    所以这个函数也**必须**知道 scope。
    """
    try:
        s = _coerce(state)
        sc = _norm_scope(scope)
    except StateError:
        return False
    if s not in _valid_states(sc):
        return False
    return s in (_TERMINAL_TASK if sc == SCOPE_TASK else _TERMINAL_STEP)


def describe_state_machine() -> str:
    """给人看的状态机说明。"""
    lines = ["状态机", "=" * 64, ""]

    lines.append("任务级（TaskState）：PENDING → RUNNING → "
                 "SUCCESS / FAILED / BLOCKED")
    lines.append("-" * 64)
    lines.append("%-12s %-12s %s" % ("当前", "事件", "转到"))
    for (s, e), t in sorted(TASK_TRANSITIONS.items()):
        lines.append("%-12s %-12s %s" % (s, e, t))

    lines += ["", "步骤级（StepState）：PENDING → RUNNING → "
              "SUCCESS / FAILED / SKIPPED / BLOCKED", "-" * 64]
    lines.append("%-12s %-12s %s" % ("当前", "事件", "转到"))
    for (s, e), t in sorted(STEP_TRANSITIONS.items()):
        lines.append("%-12s %-12s %s" % (s, e, t))

    lines += [
        "",
        "关键语义：",
        "  · BLOCKED 可恢复（resume / retry → RUNNING）",
        "  · 步骤级 FAILED **不是终态** —— 自愈靠 retry 拉回 RUNNING",
        "  · 任务级 FAILED **是终态** —— 整个任务放弃了",
        "  · 终态上重复 abort 幂等（不抛）",
        "",
        "上限（在 recovery 里，不在状态机里）：",
        "  · 单步重试 3 次  · 单步换策略 2 次  · 全局 30 分钟",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """state 自检。**纯逻辑，不碰外部。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 枚举 ============
    _add("TaskState 五个值",
         {str(s.value) for s in _me.TaskState}
         == {"pending", "running", "success", "failed", "blocked"},
         str(sorted(s.value for s in _me.TaskState)))
    _add("StepState 六个值",
         {str(s.value) for s in _me.StepState}
         == {"pending", "running", "success", "failed", "skipped",
             "blocked"},
         str(sorted(s.value for s in _me.StepState)))
    _add("★ Enum 继承 str（json 安全、可直接比较）",
         _me.TaskState.PENDING == "pending"
         and isinstance(_me.TaskState.PENDING, str), "是")
    _add("str(枚举) 返回值",
         str(_me.TaskState.PENDING) == "pending", str(_me.TaskState.PENDING))
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    # ============ 2) 任务级合法转换 ============
    _add("pending --start--> running",
         _me.transition(_me.TaskState.PENDING, _me.EVENT_START) == "running",
         "running")
    _add("running --succeed--> success",
         _me.transition("running", _me.EVENT_SUCCEED) == "success", "success")
    _add("running --fail--> failed",
         _me.transition("running", _me.EVENT_FAIL) == "failed", "failed")
    _add("running --block--> blocked",
         _me.transition("running", _me.EVENT_BLOCK) == "blocked", "blocked")
    _add("★ blocked --resume--> running（可恢复）",
         _me.transition("blocked", _me.EVENT_RESUME) == "running", "running")
    _add("blocked --fail--> failed",
         _me.transition("blocked", _me.EVENT_FAIL) == "failed", "failed")

    _add("★ 转换返回字符串而不是 Enum",
         isinstance(_me.transition("pending", _me.EVENT_START), str)
         and not isinstance(_me.transition("pending", _me.EVENT_START),
                            _me.TaskState),
         "str")

    # ============ 3) 步骤级合法转换 ============
    _add("pending --start--> running",
         _me.transition("pending", _me.EVENT_START) == "running", "running")
    _add("running --skip--> skipped",
         _me.transition("running", _me.EVENT_SKIP) == "skipped", "skipped")
    _add("pending --skip--> skipped（还没开始就能跳）",
         _me.transition("pending", _me.EVENT_SKIP) == "skipped", "skipped")

    # ============ 4) 自愈的关键转换 ============
    _add("★★ failed --retry--> running（自愈的基础）",
         _me.transition("failed", _me.EVENT_RETRY) == "running", "running")
    _add("★★ blocked --retry--> running",
         _me.transition("blocked", _me.EVENT_RETRY) == "running", "running")
    _add("★★ blocked --resume--> running",
         _me.transition("blocked", _me.EVENT_RESUME) == "running", "running")

    # ============ 5) 非法转换必须抛 ============
    bad_cases = [
        ("pending", _me.EVENT_SUCCEED, "没开始就成功"),
        ("pending", _me.EVENT_FAIL, "没开始就失败"),
        ("pending", _me.EVENT_RESUME, "没卡住就恢复"),
        ("success", _me.EVENT_START, "成功后重开"),
        ("success", _me.EVENT_RETRY, "成功后重试"),
        ("skipped", _me.EVENT_START, "跳过后重开"),
        ("pending", "乱写的事件", "未知事件"),
    ]
    for s, e, why in bad_cases:
        _add("★ 非法转换被拒：%s（%s）" % (s + " --" + e, why),
             _raise_type(lambda s=s, e=e: _me.transition(s, e))
             == "StateError", "抛了")

    _add("★ StateError 带当前状态",
         _raise_attr(lambda: _me.transition("success", _me.EVENT_START),
                     "current") == "success", "有")
    # 用 pending（有可用事件）来测 —— success 是终态，可用事件本来就是空的
    _add("★ StateError 带可用事件列表（可诊断）",
         "start" in (_raise_attr(
             lambda: _me.transition("pending", _me.EVENT_SUCCEED),
             "allowed") or []),
         str(_raise_attr(
             lambda: _me.transition("pending", _me.EVENT_SUCCEED),
             "allowed")))
    _add("★ 终态的可用事件列表为空（不能再转出去）",
         _raise_attr(lambda: _me.transition("success", _me.EVENT_START),
                     "allowed") == [],
         "空")

    # ============ 6) 幂等 ============
    _add("★ 终态上重复 abort 幂等（不抛）",
         _me.transition("failed", _me.EVENT_ABORT) == "failed"
         and _me.transition("success", _me.EVENT_ABORT) == "success"
         and _me.transition("skipped", _me.EVENT_ABORT) == "skipped",
         "幂等")
    _add("failed --abort--> failed",
         _me.transition("failed", _me.EVENT_ABORT) == "failed", "failed")

    # ============ 7) 空事件 ============
    _add("★ 空事件抛 StateError",
         _raise_type(lambda: _me.transition("pending", "")) == "StateError",
         "抛了")
    _add("★ None 事件抛 StateError",
         _raise_type(lambda: _me.transition("pending", None))
         == "StateError", "抛了")

    # ============ 8) 未知状态 ============
    _add("★ 未知状态抛 StateError（不静默）",
         _raise_type(lambda: _me.transition("runing", "start"))
         == "StateError", "抛了（拼错要能发现）")
    _add("★ 未知状态类型抛 StateError",
         _raise_type(lambda: _me.transition(123, "start")) == "StateError",
         "抛了")
    _add("未知状态的 can_transition 返回 False（不抛）",
         _me.can_transition("runing", "start") is False, "False")

    # ============ 9) can_transition ============
    _add("can_transition 对合法转换返回 True",
         _me.can_transition("pending", "start") is True, "True")
    _add("can_transition 对非法转换返回 False",
         _me.can_transition("success", "start") is False, "False")
    _add("★ can_transition 永不抛",
         all(_me.can_transition(s, e) in (True, False)
             for s in ("pending", "running", "success", "failed",
                       "skipped", "blocked", "乱写")
             for e in list(_me.EVENTS) + ["乱写"]),
         "全部返回 bool")

    # ============ 10) 终态判定（注意 scope）============
    _add("任务 SUCCESS 是终态",
         _me.is_terminal("success", scope="task") is True, "终态")
    _add("任务 FAILED 是终态",
         _me.is_terminal("failed", scope="task") is True, "终态")
    _add("★ 任务 BLOCKED **不是**终态（可恢复）",
         _me.is_terminal("blocked", scope="task") is False, "非终态")
    _add("步骤 SUCCESS 是终态",
         _me.is_terminal("success", scope="step") is True, "终态")
    _add("步骤 SKIPPED 是终态",
         _me.is_terminal("skipped", scope="step") is True, "终态")
    _add("★★ 同一个 failed：任务级是终态，步骤级**不是**",
         _me.is_terminal("failed", scope="task") is True
         and _me.is_terminal("failed", scope="step") is False,
         "task=True step=False")
    _add("★ 步骤 failed 能 retry 回 running（自愈的基础）",
         _me.transition("failed", "retry", scope="step") == "running",
         "failed --retry--> running")
    _add("★ 任务级没有 retry（任务失败就是失败）",
         _me.can_transition("failed", "retry", scope="task") is False,
         "False")
    _add("★ 步骤级有 skip，任务级没有",
         _me.can_transition("running", "skip", scope="step") is True
         and _me.can_transition("running", "skip", scope="task") is False,
         "step=True task=False")
    _add("未知状态 is_terminal 返回 False",
         _me.is_terminal("乱写") is False, "False")

    # ============ 10b) scope 参数本身 ============
    _add("★ 默认 scope 是 step", _me.SCOPE_DEFAULT == "step", "step")
    _add("scope 常量齐全",
         set(_me.SCOPES) == {"task", "step"}, str(_me.SCOPES))
    _add("★ 非法 scope 抛 StateError",
         _raise_type(lambda: _me.transition("pending", "start",
                                            scope="乱写"))
         == "StateError", "抛了")
    _add("★ 非法 scope 的 can_transition 返回 False（不抛）",
         _me.can_transition("pending", "start", scope="乱写") is False,
         "False")
    _add("★ 状态不属于该 scope 时抛（'skipped' 不是任务级状态）",
         _raise_type(lambda: _me.transition("skipped", "start",
                                            scope="task"))
         == "StateError", "抛了")
    _add("★ scope 别名可用（tasks / steps）",
         _me.transition("pending", "start", scope="tasks") == "running"
         and _me.transition("pending", "start", scope="steps") == "running",
         "别名")
    _add("★ transition_or_raise 与 transition 一致",
         _me.transition_or_raise("pending", "start")
         == _me.transition("pending", "start"), "一致")

    # ============ 11) 转换表自身的一致性 ============
    _add("★ 转换表里没有自环（除了幂等 abort）",
         all(s != t or e == _me.EVENT_ABORT
             for (s, e), t in list(_me.TASK_TRANSITIONS.items())
             + list(_me.STEP_TRANSITIONS.items())),
         "无意外自环")
    _add("★ 每个事件都在 EVENTS 里",
         all(e in _me.EVENTS
             for (_s, e) in list(_me.TASK_TRANSITIONS)
             + list(_me.STEP_TRANSITIONS)),
         "全部登记")
    _add("★ 每个目标状态都是合法状态值",
         all(t in {str(x.value) for x in _me.TaskState}
             or t in {str(x.value) for x in _me.StepState}
             for t in list(_me.TASK_TRANSITIONS.values())
             + list(_me.STEP_TRANSITIONS.values())),
         "全部合法")

    # ============ 12) 所有状态都可达 ============
    reachable_task = {t for t in _me.TASK_TRANSITIONS.values()}
    _add("★ 任务级所有状态都能被转到",
         reachable_task == {"running", "success", "failed", "blocked"},
         str(sorted(reachable_task)))
    reachable_step = {t for t in _me.STEP_TRANSITIONS.values()}
    _add("★ 步骤级所有状态都能被转到",
         reachable_step == {"running", "success", "failed", "skipped",
                            "blocked"},
         str(sorted(reachable_step)))

    # ============ 13) 随机游走：不会卡死 ============
    stuck = []
    for start in ("pending", "running"):
        for e1 in _me.EVENTS:
            if not _me.can_transition(start, e1):
                continue
            s1 = _me.transition(start, e1)
            for e2 in _me.EVENTS:
                if not _me.can_transition(s1, e2):
                    continue
                s2 = _me.transition(s1, e2)
                if s2 not in {str(x.value) for x in _me.TaskState} | \
                        {str(x.value) for x in _me.StepState}:
                    stuck.append((start, e1, e2, s2))
    _add("★ 两步随机游走不会产生非法状态", not stuck, str(stuck[:3]))

    # ============ 14) describe ============
    txt = _me.describe_state_machine()
    _add("describe 列出任务级转换", "TaskState" in txt, "有")
    _add("describe 列出步骤级转换", "StepState" in txt, "有")
    _add("★ describe 说明 FAILED 不是终态",
         "不是终态" in txt, "有")
    _add("describe 列出三条上限",
         all(k in txt for k in ("3 次", "2 次", "30 分钟")), "有")

    # ============ 15) 无外部依赖 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import 本包其他模块（状态机是最底层）",
         not (top & {"goal", "plan", "planner", "memory", "executor",
                     "observer", "verifier", "recovery"}),
         sorted(top))
    _add("顶层只依赖标准库",
         top <= {"time", "enum", "typing", "__future__", "sys"},
         sorted(top))

    elapsed = time.perf_counter() - t0
    return {
        "module": "state",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


# ---- 自检辅助 ----
def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


def _raise_attr(fn, attr: str):
    try:
        fn()
    except StateError as exc:
        return getattr(exc, attr, None)
    except BaseException:      # noqa: BLE001
        return None
    return None


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_state_machine())
