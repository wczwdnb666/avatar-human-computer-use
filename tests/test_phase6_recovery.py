# -*- coding: utf-8 -*-
"""阶段六 · 异常恢复（``recovery``）。

## 这个文件在守什么

自愈是阶段六的核心目标，也是最容易失控的地方 ——
"无限重试"和"重试不够"都是事故。这里守：

1. **五种策略都能被走到**（不是三个分支摆设）
2. **决策顺序不能换** —— 风险优先于重试计数
3. **三条上限硬约束**（硬约束 6）—— 模拟 20 次失败必须收敛
4. **风险必须叫人**（硬约束 9）—— 不允许硬闯
5. **纯函数** —— 决策不改任何东西（记账是执行器的事）

## 为什么"认不出的错误按可恢复处理"

误判成 abort 会**丢掉整个任务**，误判成 retry 只是**多花几秒**。
两害相权取其轻 —— 这个方向被测试钉住。
"""

from __future__ import annotations

import pytest

from avatar.agentic_planner.recovery import (
    MAX_RETRIES,
    MAX_STRATEGY_SWITCHES,
    RecoveryDecision,
    RecoveryStrategy,
    UNRECOVERABLE_HINTS,
    UNRECOVERABLE_TYPES,
    decide_recovery,
    is_unrecoverable,
    should_pause_for_risk,
)


class _Step:
    def __init__(self, attempts=0, switches=0):
        self.attempts = attempts
        self.strategy_switches = switches


class _Risk:
    def __init__(self, clean=False, desc="slider 检出"):
        self.is_clean = clean
        self._desc = desc

    def describe(self):
        return self._desc


# ==========================================================================
# 1. 契约
# ==========================================================================
class TestContract:
    def test_five_strategies(self):
        assert len(RecoveryStrategy) == 5
        assert {s.value for s in RecoveryStrategy} == {
            "retry", "retry_with_fallback", "skip", "abort",
            "pause_for_human"}

    def test_limits(self):
        """硬约束 6。"""
        assert MAX_RETRIES == 3
        assert MAX_STRATEGY_SWITCHES == 2

    def test_strategy_is_str_enum(self):
        assert RecoveryStrategy.RETRY == "retry"
        assert str(RecoveryStrategy.ABORT) == "abort"

    def test_decision_compares_with_string(self):
        d = decide_recovery(_Step(attempts=1))
        assert d == "retry"
        assert d != "abort"
        assert d.value == "retry"
        assert str(d) == "retry"

    def test_decision_hashable(self):
        a = decide_recovery(_Step(attempts=1))
        b = decide_recovery(_Step(attempts=9, switches=2))
        assert len({a, b}) == 2

    def test_decision_has_reason(self):
        """★ 每个决策都要能说清"为什么" —— 复盘靠它。"""
        for attempts in (0, 1, 3, 9):
            for switches in (0, 1, 2):
                d = decide_recovery(_Step(attempts, switches))
                assert d.reason, (attempts, switches)

    def test_decision_to_dict(self):
        import json
        d = decide_recovery(_Step(attempts=1))
        assert json.dumps(d.to_dict(), ensure_ascii=False)


# ==========================================================================
# 2. 五条路都走到
# ==========================================================================
class TestAllStrategies:
    def test_first_failure_retries(self):
        assert decide_recovery(_Step(attempts=1)) == "retry"

    def test_retries_exhausted_switches_strategy(self):
        assert decide_recovery(_Step(attempts=3, switches=0)) == \
            "retry_with_fallback"

    def test_switches_exhausted_pauses(self):
        assert decide_recovery(_Step(attempts=9, switches=2)) == \
            "pause_for_human"

    def test_risk_pauses(self):
        assert decide_recovery(_Step(), risk=_Risk(clean=False)) == \
            "pause_for_human"

    def test_unrecoverable_aborts(self):
        assert decide_recovery(_Step(), error=PermissionError("x")) == "abort"


# ==========================================================================
# 3. 决策顺序不能换
# ==========================================================================
class TestDecisionOrder:
    def test_risk_beats_retry_count(self):
        """★★ 第一次就撞上风险 → 必须叫人，不是 retry。

        先判重试次数的话，"第一次撞验证码"会被判 retry ——
        而重试只会再撞一次。
        """
        d = decide_recovery(_Step(attempts=0), risk=_Risk(clean=False))
        assert d == "pause_for_human", d.describe()

    def test_risk_beats_unrecoverable(self):
        """★★ 风险优先于 abort —— 风控问题是"能解决"的，该叫人。"""
        d = decide_recovery(_Step(attempts=0),
                            error=PermissionError("x"),
                            risk=_Risk(clean=False))
        assert d == "pause_for_human", d.describe()

    def test_unrecoverable_beats_retry_count(self):
        d = decide_recovery(_Step(attempts=0),
                            error=PermissionError("x"))
        assert d == "abort", d.describe()

    def test_clean_risk_does_not_interfere(self):
        assert decide_recovery(_Step(attempts=1),
                               risk=_Risk(clean=True)) == "retry"

    def test_none_risk_does_not_interfere(self):
        assert decide_recovery(_Step(attempts=1), risk=None) == "retry"


# ==========================================================================
# 4. 上限硬约束（硬约束 6）
# ==========================================================================
class TestLimitsEnforced:
    def test_converges(self):
        """★★ 模拟 20 次失败，一定要收敛 —— 不允许无限重试。"""
        st = _Step()
        seen = []
        for _ in range(20):
            d = decide_recovery(st)
            seen.append(str(d))
            if d == "retry":
                st.attempts += 1
            elif d == "retry_with_fallback":
                st.strategy_switches += 1
            else:
                break
        assert seen[-1] in ("pause_for_human", "abort"), seen

    def test_retry_count_within_limit(self):
        st = _Step()
        retries = 0
        for _ in range(20):
            d = decide_recovery(st)
            if d == "retry":
                retries += 1
                st.attempts += 1
            elif d == "retry_with_fallback":
                st.strategy_switches += 1
            else:
                break
        assert retries <= MAX_RETRIES, retries

    def test_switch_count_within_limit(self):
        st = _Step()
        switches = 0
        for _ in range(20):
            d = decide_recovery(st)
            if d == "retry":
                st.attempts += 1
            elif d == "retry_with_fallback":
                switches += 1
                st.strategy_switches += 1
            else:
                break
        assert switches <= MAX_STRATEGY_SWITCHES, switches

    def test_custom_limits_respected(self):
        assert decide_recovery(_Step(attempts=1),
                               max_retries=1) == "retry_with_fallback"
        assert decide_recovery(_Step(attempts=9, switches=0),
                               max_switches=0) == "pause_for_human"

    def test_limits_in_decision(self):
        d = decide_recovery(_Step(attempts=2, switches=1))
        assert d.limits["attempts"] == 2
        assert d.limits["max_retries"] == MAX_RETRIES


# ==========================================================================
# 5. 不可恢复错误的识别
# ==========================================================================
class TestUnrecoverable:
    def test_known_types(self):
        assert is_unrecoverable(PermissionError("x")) is True

    @pytest.mark.parametrize("msg", [
        "Access Denied",
        "permission denied",
        "权限不足",
        "拒绝访问",
        "进程已退出",
        "process terminated",
        "not supported",
        "账户已被封禁",
        "account suspended",
    ])
    def test_keywords(self, msg):
        assert is_unrecoverable(msg) is True, msg

    def test_case_insensitive(self):
        assert is_unrecoverable("PERMISSION DENIED") is True

    @pytest.mark.parametrize("err", [
        ValueError("元素没找到"), OSError("网络超时"),
        RuntimeError("界面没反应"), "某个奇怪的中文错误", "", None,
    ])
    def test_recoverable_default(self, err):
        """★★ 认不出的按**可恢复**处理 —— 误 abort 丢任务，误 retry 只多花几秒。"""
        assert is_unrecoverable(err) is False, err

    def test_registry_non_empty(self):
        assert len(UNRECOVERABLE_TYPES) >= 3
        assert len(UNRECOVERABLE_HINTS) >= 5


# ==========================================================================
# 6. 风险判据（硬约束 9）
# ==========================================================================
class TestRiskGate:
    def test_clean_passes(self):
        assert should_pause_for_risk(_Risk(clean=True)) is False

    def test_dirty_pauses(self):
        assert should_pause_for_risk(_Risk(clean=False)) is True

    def test_none_passes(self):
        assert should_pause_for_risk(None) is False

    def test_unknown_object_is_conservative(self):
        """★ 认不出的风险对象按"有风险"处理 —— 与 guard 判据一致。"""
        assert should_pause_for_risk(object()) is True

    def test_unknown_status_triggers_pause(self):
        """★ 未知状态（is_clean=False, detected=False）也要叫人。"""
        class _Unknown:
            is_clean = False
            detected = False

        assert should_pause_for_risk(_Unknown()) is True


# ==========================================================================
# 7. 纯函数
# ==========================================================================
class TestPurity:
    def test_does_not_mutate_step(self):
        """★★ 决策是纯函数 —— 记账是执行器的事（硬约束 7 集中在一处）。"""
        st = _Step(attempts=2, switches=1)
        before = (st.attempts, st.strategy_switches)
        for _ in range(5):
            decide_recovery(st)
        assert (st.attempts, st.strategy_switches) == before

    def test_does_not_write_memory(self):
        """★ 不写 memory —— 否则"谁记的"会有两个答案。"""
        calls = []

        class _Mem:
            def record_decision(self, *a, **k):
                calls.append(1)

        decide_recovery(_Step(attempts=1), memory=_Mem())
        assert calls == []


# ==========================================================================
# 8. 容错
# ==========================================================================
class TestRobustness:
    def test_none_step(self):
        assert decide_recovery(None) is not None

    def test_bad_step_attrs(self):
        class _Bad:
            attempts = "abc"
            strategy_switches = None

        assert decide_recovery(_Bad()) is not None

    def test_arbitrary_error_object(self):
        assert decide_recovery(_Step(), error=object()) is not None

    def test_arbitrary_risk_object(self):
        assert decide_recovery(_Step(), risk=object()) is not None

    def test_empty_step_uses_defaults(self):
        class _Empty:
            pass

        d = decide_recovery(_Empty())
        assert d.limits["attempts"] == 0


# ==========================================================================
# 9. 不依赖阶段五
# ==========================================================================
class TestIsolation:
    def test_no_risk_detector_import(self):
        """★ recovery 不该 import 阶段五 —— 会成环。"""
        import ast
        import avatar.agentic_planner.recovery as R
        tree = ast.parse(open(R.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not any("risk_detector" in m for m in mods), sorted(mods)

    def test_no_upward_import(self):
        import ast
        import avatar.agentic_planner.recovery as R
        tree = ast.parse(open(R.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"executor", "observer", "verifier", "planner"})
