# -*- coding: utf-8 -*-
"""阶段六 · 执行器（``executor``）。

## 这个文件在守什么

执行器是把所有零件串起来的地方。这里守：

1. **DAG 调度正确** —— 依赖顺序、可并行批次
2. **三条上限硬约束**（硬约束 6）—— 重试 3 / 换策略 2 / 全局超时
3. **每步前后都观测**（硬约束 8）—— 禁止跳过
4. **自愈决策必须记入 memory**（硬约束 7）—— 禁止静默重试
5. **风控闸不可绕过**（硬约束 9）—— 但集成默认关闭
6. **断点恢复**：已成功的不重跑，自愈计数跨恢复累加

## 完全隔离真实环境

所有测试注入假 runner / observer / verifier，并用 ``tmp_path`` 做存档目录。
**不碰真实桌面，不碰 ``~/.avatar/tasks/``。**
"""

from __future__ import annotations

import os
import time

import pytest

from avatar.agentic_planner import parse_goal
from avatar.agentic_planner.executor import (
    MAX_RETRIES,
    MAX_STRATEGY_SWITCHES,
    TASK_TIMEOUT,
    ExecutionResult,
    Executor,
)
from avatar.agentic_planner.memory import ShortTermMemory
from avatar.agentic_planner.planner import RuleBasedPlanner


# ==========================================================================
# 夹具
# ==========================================================================
class FakeRunner:
    """按脚本返回结果的假执行器。"""

    def __init__(self, script=None, default=None):
        self.script = dict(script or {})
        self.default = default if default is not None else {
            "ok": True, "detail": "ok"}
        self.calls = []

    def __call__(self, step):
        self.calls.append(step.id)
        n = self.calls.count(step.id) - 1
        v = self.script.get(step.id, self.default)
        if isinstance(v, list):
            v = v[min(n, len(v) - 1)]
        if isinstance(v, BaseException):
            raise v
        return v


@pytest.fixture
def tmpmem(tmp_path):
    """一个用临时目录的 memory 工厂。"""
    n = {"i": 0}

    def _mk(tid=""):
        n["i"] += 1
        return ShortTermMemory(tid or "t%d" % n["i"],
                               directory=str(tmp_path))
    return _mk


def _mk_exec(runner, tmpmem, **kw):
    """造一个完全隔离的执行器。"""
    kw.setdefault("guard_enabled", False)
    kw.setdefault("memory", tmpmem())
    kw.setdefault("observer", lambda **k: {"title": "记事本"})
    kw.setdefault("verifier", lambda *a, **k: {"ok": True})
    return Executor(runner, **kw)


def _ok_ver(step, before, after):
    return {"ok": True}


def _fail_ver(step, before, after):
    return {"ok": False, "reason": "测试：判失败"}


def _only_s2_fail_ver(step, before, after):
    return {"ok": step.id != "s2"}


# ==========================================================================
# 1. 常量（硬约束 6）
# ==========================================================================
class TestLimits:
    def test_retry_limit(self):
        assert MAX_RETRIES == 3

    def test_switch_limit(self):
        assert MAX_STRATEGY_SWITCHES == 2

    def test_task_timeout(self):
        assert TASK_TIMEOUT == 1800.0     # 30 分钟

    def test_limits_are_constants_not_params(self):
        """★ 上限是常量而非实例默认值 —— 防止有人"这次赶时间"调大。"""
        import inspect
        sig = inspect.signature(Executor.__init__)
        for name in ("max_retries", "max_switches", "task_timeout"):
            assert sig.parameters[name].default in (
                MAX_RETRIES, MAX_STRATEGY_SWITCHES, TASK_TIMEOUT)


# ==========================================================================
# 2. 顺序执行
# ==========================================================================
class TestSequentialExecution:
    def test_two_steps_succeed(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        r = FakeRunner()
        out = _mk_exec(r, tmpmem).run(plan)
        assert out.ok is True
        assert out.task_state == "success"
        assert r.calls == ["s1", "s2"]

    def test_dag_order_respected(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        r = FakeRunner()
        _mk_exec(r, tmpmem).run(plan)
        assert r.calls.index("s1") < r.calls.index("s2")

    def test_steps_reach_terminal_state(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        _mk_exec(FakeRunner(), tmpmem).run(plan)
        assert all(str(s.state) == "success" for s in plan.steps)

    def test_all_events_recorded(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        out = _mk_exec(FakeRunner(), tmpmem).run(plan)
        assert len(out.memory.events_of("transition")) >= 2
        assert not any("非法状态转换" in str(e.get("error", {}))
                       for e in out.memory.errors())


# ==========================================================================
# 3. 并行（DAG 的价值）
# ==========================================================================
class TestParallel:
    def test_independent_steps_run_parallel(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开浏览器，打开记事本"))
        assert plan.steps[1].depends_on == []
        r = FakeRunner()
        out = _mk_exec(r, tmpmem).run(plan)
        assert out.ok is True
        assert set(r.calls) == {"s1", "s2"}

    def test_serial_mode_also_works(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开浏览器，打开记事本"))
        r = FakeRunner()
        out = _mk_exec(r, tmpmem, parallel=False).run(plan)
        assert out.ok is True
        assert set(r.calls) == {"s1", "s2"}

    def test_dependent_steps_not_parallel(self, tmpmem):
        """★ 有依赖的步骤不该被并行 —— 顺序语义要保住。"""
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        r = FakeRunner()
        _mk_exec(r, tmpmem).run(plan)
        assert r.calls == ["s1", "s2"]


# ==========================================================================
# 4. 失败 → 重试 → 上限
# ==========================================================================
class TestRetryAndLimits:
    def test_retry_then_succeed(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        r = FakeRunner({"s2": [{"ok": False}, {"ok": True}]})
        counts = {}

        def _follow(step, before, after):
            n = counts.get(step.id, 0) + 1
            counts[step.id] = n
            return {"ok": not (step.id == "s2" and n == 1)}

        out = _mk_exec(r, tmpmem, verifier=_follow).run(plan)
        assert out.ok is True, out.describe()
        assert r.calls.count("s2") == 2

    def test_attempts_recorded(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        counts = {}

        def _follow(step, before, after):
            n = counts.get(step.id, 0) + 1
            counts[step.id] = n
            return {"ok": not (step.id == "s2" and n == 1)}

        _mk_exec(FakeRunner(), tmpmem, verifier=_follow).run(plan)
        assert plan.step_by_id("s2").attempts == 2

    def test_never_exceeds_limits(self, tmpmem):
        """★★ 一直失败时，调用次数不能超过「重试 + 换策略」上限。"""
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        r = FakeRunner()
        out = _mk_exec(r, tmpmem, verifier=_only_s2_fail_ver).run(plan)
        assert out.ok is False
        assert r.calls.count("s2") <= MAX_RETRIES + MAX_STRATEGY_SWITCHES + 1, \
            "s2 被调了 %d 次" % r.calls.count("s2")

    def test_switches_recorded(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        _mk_exec(FakeRunner(), tmpmem,
                 verifier=_only_s2_fail_ver).run(plan)
        assert plan.step_by_id("s2").strategy_switches <= MAX_STRATEGY_SWITCHES

    def test_task_fails_when_step_fails(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem,
                       verifier=_fail_ver,
                       recovery=lambda *a, **k: "abort").run(plan)
        assert out.ok is False
        assert out.task_state == "failed"


# ==========================================================================
# 5. 观测（硬约束 8）
# ==========================================================================
class TestObservation:
    def test_two_observations_per_step(self, tmpmem):
        """★★ 每步前后各观测一次，禁止跳过。"""
        seen = []

        def _obs(**k):
            seen.append(1)
            return {"title": "记事本"}

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        out = _mk_exec(FakeRunner(), tmpmem, observer=_obs).run(plan)
        assert len(seen) == 4, "2 步 × 2 次 = 4 次，实际 %d" % len(seen)
        assert len(out.memory.events_of("observation")) == 4

    def test_observation_has_phase(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem,
                       observer=lambda **k: {"title": "x"}).run(plan)
        phases = {e["phase"] for e in out.memory.events_of("observation")}
        assert phases == {"before", "after"}

    def test_observer_exception_does_not_break(self, tmpmem):
        def _boom(**k):
            raise RuntimeError("观测器炸了")

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem, observer=_boom).run(plan)
        assert isinstance(out, ExecutionResult)


# ==========================================================================
# 6. 自愈决策记录（硬约束 7）
# ==========================================================================
class TestDecisionRecording:
    def test_decisions_recorded(self, tmpmem):
        """★★ 禁止静默重试 —— 每次决策都要留痕。"""
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        out = _mk_exec(FakeRunner(), tmpmem,
                       verifier=_only_s2_fail_ver).run(plan)
        decs = out.memory.decisions()
        assert len(decs) >= 1
        assert all(d.get("strategy") for d in decs)

    def test_decision_has_reason(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem, verifier=_fail_ver,
                       recovery=lambda *a, **k: "retry").run(plan)
        assert all(d.get("reason") for d in out.memory.decisions())


# ==========================================================================
# 7. 风控闸（硬约束 9）
# ==========================================================================
class TestGuardGate:
    def test_disabled_gate_passes(self, tmpmem):
        from avatar.risk_detector import integration as RI
        was = RI.is_installed()
        try:
            RI.uninstall()
            plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
            out = _mk_exec(FakeRunner(), tmpmem, guard_enabled=True).run(plan)
            assert out.ok is True
        finally:
            RI.install(enabled=was)

    def test_observe_mode_does_not_block(self, tmpmem):
        from avatar.risk_detector import integration as RI
        was = RI.is_installed()
        try:
            RI.install(enabled=True, mode_=RI.MODE_OBSERVE)
            plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
            out = _mk_exec(FakeRunner(), tmpmem, guard_enabled=True).run(plan)
            assert out.ok is True
        finally:
            RI.uninstall()
            RI.install(enabled=was)

    def test_before_step_exists(self):
        """★ 5E 的挂载点 3/3 必须还在。"""
        assert callable(getattr(Executor, "before_step", None))


# ==========================================================================
# 8. 全局超时
# ==========================================================================
class TestTimeout:
    def test_timeout_interrupts(self, tmpmem):
        def _slow(step):
            time.sleep(0.2)
            return {"ok": True}

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        out = _mk_exec(_slow, tmpmem, task_timeout=0.05,
                       parallel=False).run(plan)
        assert out.elapsed < 3.0
        assert out.ok is False


# ==========================================================================
# 9. 异常收敛
# ==========================================================================
class TestExceptionHandling:
    def test_runner_exception_becomes_failure(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        r = FakeRunner({"s1": OSError("runner 炸了")})
        out = _mk_exec(r, tmpmem, verifier=_fail_ver,
                       recovery=lambda *a, **k: "abort").run(plan)
        assert out.ok is False
        assert any("OSError" in str(e.get("error", {}))
                   for e in out.memory.errors())

    def test_verifier_exception_does_not_break(self, tmpmem):
        def _boom(step, before, after):
            raise RuntimeError("验证器炸了")

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem, verifier=_boom).run(plan)
        assert isinstance(out, ExecutionResult)

    def test_recovery_exception_does_not_break(self, tmpmem):
        def _boom(step, **k):
            raise RuntimeError("恢复器炸了")

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(FakeRunner(), tmpmem, verifier=_fail_ver,
                       recovery=_boom).run(plan)
        assert isinstance(out, ExecutionResult)

    def test_no_runner_returns_failure(self, tmpmem):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        out = _mk_exec(None, tmpmem, verifier=_fail_ver,
                       recovery=lambda *a, **k: "abort").run(plan)
        assert out.ok is False
        assert any("not_implemented" in s.detail for s in out.steps)


# ==========================================================================
# 10. 非法计划
# ==========================================================================
class TestInvalidPlan:
    def test_cyclic_plan_rejected(self, tmpmem):
        from avatar.agentic_planner.plan import Plan, Step
        bad = Plan(steps=[Step(id="a", action="x", depends_on=["b"]),
                          Step(id="b", action="x", depends_on=["a"])])
        out = _mk_exec(FakeRunner(), tmpmem).run(bad)
        assert out.ok is False
        assert "不合法" in out.reason

    def test_already_done_plan_succeeds(self, tmpmem):
        from avatar.agentic_planner.plan import Plan, Step
        p = Plan(steps=[Step(id="a", action="wait", state="success")])
        out = _mk_exec(FakeRunner(), tmpmem).run(p)
        assert out.ok is True
        assert out.steps == []


# ==========================================================================
# 11. 断点恢复
# ==========================================================================
class TestResume:
    def test_resume_finishes_task(self, tmpmem, tmp_path):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        mem = ShortTermMemory("res1", directory=str(tmp_path))
        _mk_exec(FakeRunner(), tmpmem, memory=mem,
                 verifier=_only_s2_fail_ver,
                 recovery=lambda *a, **k: "abort").run(plan, mem)
        assert mem.step_state("s1") == "success"
        assert mem.step_state("s2") == "failed"

        ex = _mk_exec(FakeRunner(), tmpmem)
        out = ex.resume("res1", directory=str(tmp_path))
        assert out.ok is True, out.describe()

    def test_resume_skips_succeeded(self, tmpmem, tmp_path):
        """★★ 已成功的步骤不重跑 —— 这是断点恢复的意义。"""
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        mem = ShortTermMemory("res2", directory=str(tmp_path))
        _mk_exec(FakeRunner(), tmpmem, memory=mem,
                 verifier=_only_s2_fail_ver,
                 recovery=lambda *a, **k: "abort").run(plan, mem)

        ex = _mk_exec(FakeRunner(), tmpmem)
        ex.resume("res2", directory=str(tmp_path))
        assert ex.runner.calls.count("s1") == 0, \
            "s1 已成功，不该重跑：%s" % ex.runner.calls
        assert ex.runner.calls.count("s2") == 1

    def test_resume_missing_task_no_crash(self, tmpmem, tmp_path):
        ex = _mk_exec(FakeRunner(), tmpmem)
        out = ex.resume("nope", directory=str(tmp_path))
        assert isinstance(out, ExecutionResult)
        assert out.ok is False


# ==========================================================================
# 12. 不污染真实目录
# ==========================================================================
class TestIsolation:
    def test_selfcheck_does_not_write_real_task_dir(self, tmp_path):
        """★★ 自检绝不能往 ``~/.avatar/tasks/`` 写东西。"""
        import avatar.agentic_planner.executor as E
        real = os.path.join(os.path.expanduser("~"), ".avatar", "tasks")
        before = set(os.listdir(real)) if os.path.isdir(real) else set()
        E.selfcheck()
        after = set(os.listdir(real)) if os.path.isdir(real) else set()
        assert after == before, \
            "自检污染了真实任务目录：%s" % sorted(after - before)

    def test_uses_threading_not_asyncio(self):
        """★ 整条链路是阻塞的 C 调用，asyncio 包不住。"""
        import ast
        import avatar.agentic_planner.executor as E
        tree = ast.parse(open(E.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert "asyncio" not in mods
        assert "concurrent" in mods
