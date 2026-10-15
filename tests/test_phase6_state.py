# -*- coding: utf-8 -*-
"""阶段六 · 状态机（``state``）。

## 这个文件在守什么

状态机是"自动恢复"的地基。它错了，恢复就无从谈起。
这里守三件事：

1. **非法转换必须抛** —— 直接赋值不会报错，只会在某天让任务莫名重跑
2. **`failed` 在两级下结论相反** —— 任务级是终态，步骤级不是（自愈要拉回来）
3. **`scope` 必须显式** —— 五个状态值在两个枚举里都有，光看字符串分不清

## 硬约束 4

所有状态转换必须通过 ``transition()``，禁止直接赋值。
本文件用"非法转换抛异常"来间接证明这条被强制了。
"""

from __future__ import annotations

import pytest

from avatar.agentic_planner.state import (
    EVENT_ABORT,
    EVENT_BLOCK,
    EVENT_FAIL,
    EVENT_RESUME,
    EVENT_RETRY,
    EVENT_SKIP,
    EVENT_START,
    EVENT_SUCCEED,
    EVENTS,
    SCOPE_STEP,
    SCOPE_TASK,
    StepState,
    StateError,
    TaskState,
    can_transition,
    is_terminal,
    transition,
)


# ==========================================================================
# 1. 枚举契约
# ==========================================================================
class TestEnums:
    def test_task_states(self):
        assert {str(s.value) for s in TaskState} == {
            "pending", "running", "success", "failed", "blocked"}

    def test_step_states(self):
        assert {str(s.value) for s in StepState} == {
            "pending", "running", "success", "failed", "skipped", "blocked"}

    def test_str_enum(self):
        """继承 str 让 JSON 序列化和直接比较都能用。"""
        assert TaskState.PENDING == "pending"
        assert isinstance(TaskState.PENDING, str)
        assert str(StepState.SUCCESS) == "success"

    def test_events_registered(self):
        assert len(EVENTS) == 8
        assert EVENT_RETRY in EVENTS and EVENT_RESUME in EVENTS


# ==========================================================================
# 2. 合法转换
# ==========================================================================
class TestLegalTransitions:
    def test_task_happy_path(self):
        assert transition("pending", EVENT_START,
                          scope=SCOPE_TASK) == "running"
        assert transition("running", EVENT_SUCCEED,
                          scope=SCOPE_TASK) == "success"

    def test_task_block_and_resume(self):
        assert transition("running", EVENT_BLOCK,
                          scope=SCOPE_TASK) == "blocked"
        assert transition("blocked", EVENT_RESUME,
                          scope=SCOPE_TASK) == "running"

    def test_step_skip(self):
        assert transition("running", EVENT_SKIP,
                          scope=SCOPE_STEP) == "skipped"
        assert transition("pending", EVENT_SKIP,
                          scope=SCOPE_STEP) == "skipped"

    def test_returns_plain_string(self):
        got = transition("pending", EVENT_START)
        assert isinstance(got, str) and not isinstance(got, TaskState)


# ==========================================================================
# 3. 自愈的关键两条
# ==========================================================================
class TestSelfHealingTransitions:
    def test_failed_can_retry(self):
        """★★ 步骤 failed 能回到 running —— 自愈的基础。"""
        assert transition("failed", EVENT_RETRY,
                          scope=SCOPE_STEP) == "running"

    def test_blocked_can_resume_and_retry(self):
        assert transition("blocked", EVENT_RESUME,
                          scope=SCOPE_STEP) == "running"
        assert transition("blocked", EVENT_RETRY,
                          scope=SCOPE_STEP) == "running"

    def test_task_failed_cannot_retry(self):
        """★ 任务级没有 retry —— 整个任务失败了就是失败了。"""
        assert can_transition("failed", EVENT_RETRY,
                              scope=SCOPE_TASK) is False


# ==========================================================================
# 4. 非法转换必须抛（硬约束 4 的执行证明）
# ==========================================================================
class TestIllegalTransitions:
    @pytest.mark.parametrize("state,event,scope,why", [
        ("pending", EVENT_SUCCEED, SCOPE_TASK, "没开始就成功"),
        ("pending", EVENT_FAIL, SCOPE_TASK, "没开始就失败"),
        ("pending", EVENT_RESUME, SCOPE_TASK, "没卡住就恢复"),
        ("success", EVENT_START, SCOPE_TASK, "成功后重开"),
        ("failed", EVENT_START, SCOPE_TASK, "失败后重开"),
        ("success", EVENT_RETRY, SCOPE_STEP, "成功后重试"),
        ("skipped", EVENT_START, SCOPE_STEP, "跳过后重开"),
    ])
    def test_illegal_raises(self, state, event, scope, why):
        with pytest.raises(StateError):
            transition(state, event, scope=scope)

    def test_error_carries_context(self):
        """异常要能自解释 —— 不然排查时只知道"错了"。"""
        with pytest.raises(StateError) as ei:
            transition("success", EVENT_START, scope=SCOPE_TASK)
        exc = ei.value
        assert exc.current == "success"
        assert exc.event == EVENT_START

    def test_empty_event_raises(self):
        with pytest.raises(StateError):
            transition("pending", "")
        with pytest.raises(StateError):
            transition("pending", None)

    def test_unknown_state_raises(self):
        """拼错的状态名要能发现 —— 静默接受会让所有判断走错分支。"""
        with pytest.raises(StateError):
            transition("runing", EVENT_START)
        with pytest.raises(StateError):
            transition(123, EVENT_START)


# ==========================================================================
# 5. scope 参数（五个值在两个枚举里都有）
# ==========================================================================
class TestScope:
    def test_default_is_step(self):
        assert transition("pending", EVENT_START) == "running"

    def test_same_value_different_scope(self):
        """★★ 同一个 `failed`，两级下终态结论相反。"""
        assert is_terminal("failed", scope=SCOPE_TASK) is True
        assert is_terminal("failed", scope=SCOPE_STEP) is False

    def test_skip_exists_only_in_step(self):
        """★ `running --skip--> skipped` 只在步骤级合法。"""
        assert can_transition("running", EVENT_SKIP,
                              scope=SCOPE_STEP) is True
        assert can_transition("running", EVENT_SKIP,
                              scope=SCOPE_TASK) is False

    def test_cross_scope_state_rejected(self):
        """`skipped` 不是任务级状态。"""
        with pytest.raises(StateError):
            transition("skipped", EVENT_START, scope=SCOPE_TASK)

    def test_bad_scope_raises(self):
        with pytest.raises(StateError):
            transition("pending", EVENT_START, scope="乱写")

    def test_scope_aliases(self):
        assert transition("pending", EVENT_START, scope="tasks") == "running"
        assert transition("pending", EVENT_START, scope="steps") == "running"


# ==========================================================================
# 6. can_transition 永不抛
# ==========================================================================
class TestCanTransition:
    def test_returns_bool(self):
        assert can_transition("pending", EVENT_START) is True
        assert can_transition("success", EVENT_START) is False

    def test_never_raises(self):
        for s in ("pending", "running", "success", "failed", "skipped",
                  "blocked", "乱写"):
            for e in list(EVENTS) + ["乱写", "", None]:
                got = can_transition(s, e)
                assert got in (True, False), (s, e, got)

    def test_bad_scope_returns_false(self):
        assert can_transition("pending", EVENT_START, scope="乱写") is False


# ==========================================================================
# 7. 终态与幂等
# ==========================================================================
class TestTerminal:
    def test_task_terminals(self):
        assert is_terminal("success", scope=SCOPE_TASK) is True
        assert is_terminal("failed", scope=SCOPE_TASK) is True
        assert is_terminal("blocked", scope=SCOPE_TASK) is False
        assert is_terminal("running", scope=SCOPE_TASK) is False

    def test_step_terminals(self):
        assert is_terminal("success", scope=SCOPE_STEP) is True
        assert is_terminal("skipped", scope=SCOPE_STEP) is True
        assert is_terminal("failed", scope=SCOPE_STEP) is False
        assert is_terminal("blocked", scope=SCOPE_STEP) is False

    def test_abort_idempotent_on_terminal(self):
        """★ 已经失败了又来一次 abort 不该报错。"""
        assert transition("failed", EVENT_ABORT,
                          scope=SCOPE_TASK) == "failed"
        assert transition("success", EVENT_ABORT,
                          scope=SCOPE_STEP) == "success"

    def test_unknown_state_is_terminal_false(self):
        assert is_terminal("乱写") is False


# ==========================================================================
# 8. 转换表自身的一致性
# ==========================================================================
class TestTransitionTable:
    def test_no_unexpected_self_loops(self):
        from avatar.agentic_planner.state import (STEP_TRANSITIONS,
                                                  TASK_TRANSITIONS)
        for (s, e), t in list(TASK_TRANSITIONS.items()) + \
                list(STEP_TRANSITIONS.items()):
            assert s != t or e == EVENT_ABORT, (s, e, t)

    def test_all_targets_are_valid_states(self):
        from avatar.agentic_planner.state import (STEP_TRANSITIONS,
                                                  TASK_TRANSITIONS)
        valid = {str(x.value) for x in TaskState} | \
                {str(x.value) for x in StepState}
        for t in list(TASK_TRANSITIONS.values()) + \
                list(STEP_TRANSITIONS.values()):
            assert t in valid, t

    def test_all_states_reachable(self):
        from avatar.agentic_planner.state import (STEP_TRANSITIONS,
                                                  TASK_TRANSITIONS)
        assert {t for t in TASK_TRANSITIONS.values()} == {
            "running", "success", "failed", "blocked"}
        assert {t for t in STEP_TRANSITIONS.values()} == {
            "running", "success", "failed", "skipped", "blocked"}

    def test_two_step_walk_never_produces_invalid_state(self):
        """随机游走两步，不该产生表外状态。"""
        valid = {str(x.value) for x in TaskState} | \
                {str(x.value) for x in StepState}
        for start in ("pending", "running"):
            for e1 in EVENTS:
                if not can_transition(start, e1):
                    continue
                s1 = transition(start, e1)
                for e2 in EVENTS:
                    if not can_transition(s1, e2):
                        continue
                    assert transition(s1, e2) in valid, (start, e1, e2)
