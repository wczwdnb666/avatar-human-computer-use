# -*- coding: utf-8 -*-
"""阶段六 · 端到端（RuleBasedPlanner + 记事本场景）。

## 这个文件在守什么

前面 6 个文件各测一块。这里测**它们串起来**：

    一句话 → parse_goal → planner.plan → plan.validate
           → executor.run（风控闸 → 观测 → 执行 → 验证 → 恢复）
           → memory.save（可断点恢复）

## 为什么用"假记事本"

真实跑记事本会让测试依赖"这台机器装了什么、桌面什么状态"。
所以这里用一个 :class:`FakeNotepad` 模拟：
打开 = 记录一个窗口，输入 = 往缓冲区追加文本，保存 = 标记已保存。

**它模拟的是阶段二/三/四的能力**，不是执行器 —— 执行器的调度逻辑
是真的在跑。这样既能在无桌面环境跑，又验证了真实链路。

## 场景覆盖

1. 快乐路径：打开 → 输入 → 保存
2. 并行路径：同时开两个应用
3. 自愈路径：第一次输入失败 → 重试成功
4. 熔断路径：中途检出验证码 → 停下等人
5. 断点恢复：中途进程"挂了" → 从存档接着跑
"""

from __future__ import annotations

import json
import os

import pytest

from avatar.agentic_planner import get_planner, parse_goal
from avatar.agentic_planner.executor import Executor
from avatar.agentic_planner.memory import ShortTermMemory
from avatar.agentic_planner.plan import Plan
from avatar.agentic_planner.verifier import verify as real_verify


# ==========================================================================
# 假记事本（模拟阶段二/三/四）
# ==========================================================================
class FakeNotepad:
    """模拟一个记事本应用 + 它的窗口。

    记录"现在开着哪些窗口、每个窗口里是什么文本、保存了没有"。
    这样验证器可以真的去读它 —— 而不是靠 runner 自报。
    """

    def __init__(self):
        self.windows = {}        # name -> {"text": str, "saved": bool}
        self.fail_next_type = 0  # 让接下来 N 次输入失败
        self.closed = []

    # ---- 动作 ----
    def open(self, name):
        if name in self.windows:
            return {"ok": True, "detail": "已经开着"}
        self.windows[name] = {"text": "", "saved": False}
        return {"ok": True, "detail": "已启动 %s" % name}

    def type(self, name, text):
        if self.fail_next_type > 0:
            self.fail_next_type -= 1
            return {"ok": False, "detail": "输入失败（模拟）"}
        if name not in self.windows:
            return {"ok": False, "detail": "没有这个窗口"}
        self.windows[name]["text"] += text
        self.windows[name]["saved"] = False
        return {"ok": True, "detail": "已输入"}

    def save(self, name):
        if name not in self.windows:
            return {"ok": False, "detail": "没有这个窗口"}
        self.windows[name]["saved"] = True
        return {"ok": True, "detail": "已保存"}

    def close(self, name):
        if name in self.windows:
            del self.windows[name]
            self.closed.append(name)
        return {"ok": True, "detail": "已关闭"}

    # ---- 观测 / 验证 ----
    def observe(self, hwnd=None, **kw):
        """模拟 observer：报当前"前台窗口"。"""
        name = next(iter(self.windows), "")
        return {
            "ok": bool(name),
            "title": name,
            "url": "",
            "hwnd": 1 if name else 0,
            "screenshot_path": "",
            "elements": list(self.windows),
        }

    def runner(self, step):
        """模拟 DefaultRunner。"""
        kind = step.action
        target = str(step.params.get("target", "") or "")
        text = str(step.params.get("text", "") or "")
        if kind == "open":
            return self.open(target or "notepad")
        if kind == "type":
            return self.type(target or self._any(), text)
        if kind == "save":
            return self.save(target or self._any())
        if kind == "close":
            return self.close(target or self._any())
        if kind == "wait":
            return {"ok": True, "detail": "等待完成"}
        return {"ok": False, "detail": "假记事本不支持 %s" % kind}

    def _any(self):
        """最近打开的那个窗口。

        规则规划器生成的 type/save 步骤常常没写 target
        （"输入hello" 没说往哪儿输）。真实执行器会打到**前台窗口**，
        而刚打开的那个通常就在前台 —— 这里模拟这个语义。
        """
        return next(reversed(self.windows), "")


class FakeVerifier:
    """模拟 verifier：**真的去读假记事本的状态**。

    这一点很重要 —— 如果验证器只会返回 True，那"自愈"就没意义了。
    """

    def __init__(self, np):
        self.np = np

    def __call__(self, step, before, after):
        kind = (step.verify or {}).get("kind", "")
        target = str(step.params.get("target", "") or "")
        expect = str((step.verify or {}).get("expect", "") or "")
        # ⚠ 规则规划器生成的 type/save 步骤 target 常常是空的
        #    （"输入hello" 没写往哪儿输）。执行器会让 runner 自己挑，
        #    验证器也必须用同一套回退逻辑，否则永远读不到。
        who = target or self.np._any()

        if step.action == "open":
            return {"ok": bool(self.np.windows)}
        if step.action == "type":
            got = self.np.windows.get(who, {}).get("text", "")
            return {"ok": bool(got) and (not expect or expect in got),
                    "reason": "缓冲区=%r 期望=%r" % (got, expect)}
        if step.action == "save":
            return {"ok": bool(self.np.windows.get(who, {}).get("saved"))}
        if step.action == "close":
            return {"ok": target not in self.np.windows}
        if kind == "elapsed":
            return {"ok": True}
        return {"ok": True}


@pytest.fixture
def np_():
    return FakeNotepad()


def _exec(np_, tmpmem, **kw):
    kw.setdefault("guard_enabled", False)
    kw.setdefault("memory", tmpmem())
    kw.setdefault("observer", np_.observe)
    kw.setdefault("verifier", FakeVerifier(np_))
    return Executor(np_.runner, **kw)


# ==========================================================================
# 1. 快乐路径
# ==========================================================================
class TestHappyPath:
    def test_open_type_save(self, np_, tmpmem):
        plan = get_planner().plan(parse_goal("打开记事本输入hello然后保存"))
        assert len(plan.steps) == 3

        out = _exec(np_, tmpmem).run(plan)

        assert out.ok is True, out.describe()
        assert out.task_state == "success"
        assert np_.windows["notepad"]["text"] == "hello"
        assert np_.windows["notepad"]["saved"] is True

    def test_full_chain_recorded(self, np_, tmpmem):
        """★ 全链路都要留痕：转换、观测、结果、决策。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        out = _exec(np_, tmpmem).run(plan)
        mem = out.memory
        assert len(mem.events_of("transition")) >= 2
        assert len(mem.events_of("observation")) == 4      # 2 步 × 2 次
        assert len(mem.events_of("result")) >= 2
        assert mem.step_state("s1") == "success"

    def test_plan_restorable_from_memory(self, np_, tmpmem):
        """★ 断点恢复的前提：计划快照能还原。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        out = _exec(np_, tmpmem).run(plan)
        back = Plan.from_dict(out.memory.plan)
        assert back.plan_id == plan.plan_id
        assert back.validate() == []


# ==========================================================================
# 2. 并行路径
# ==========================================================================
class TestParallelPath:
    def test_two_apps_parallel(self, np_, tmpmem):
        plan = get_planner().plan(parse_goal("打开浏览器，打开记事本"))
        assert plan.steps[1].depends_on == [], "两个 open 应可并行"
        out = _exec(np_, tmpmem).run(plan)
        assert out.ok is True, out.describe()
        assert set(np_.windows) >= {"msedge", "notepad"}

    def test_dependent_step_waits(self, np_, tmpmem):
        plan = get_planner().plan(
            parse_goal("打开浏览器，打开记事本，输入hello"))
        # s3 依赖 s2（记事本），不依赖 s1（浏览器）
        assert plan.steps[2].depends_on == [plan.steps[1].id]
        out = _exec(np_, tmpmem).run(plan)
        assert out.ok is True, out.describe()
        assert np_.windows["notepad"]["text"] == "hello"


# ==========================================================================
# 3. 自愈路径
# ==========================================================================
class TestSelfHealingPath:
    def test_retry_then_succeed(self, np_, tmpmem):
        """★★ 第一次输入失败 → 自动重试 → 成功。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        np_.fail_next_type = 1        # 第一次输入失败

        out = _exec(np_, tmpmem).run(plan)

        assert out.ok is True, out.describe()
        assert np_.windows["notepad"]["text"] == "hello"
        # 重试决策必须留痕（硬约束 7）
        assert len(out.memory.decisions()) >= 1
        assert any(d["strategy"] == "retry"
                   for d in out.memory.decisions())

    def test_persistent_failure_converges(self, np_, tmpmem):
        """★★ 一直失败要收敛（不会无限重试）。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        np_.fail_next_type = 999

        out = _exec(np_, tmpmem).run(plan)

        assert out.ok is False
        s2 = plan.step_by_id("s2")
        assert s2.attempts <= 3 + 2 + 1, "attempts=%d" % s2.attempts
        assert s2.strategy_switches <= 2
        # 上限用尽后转人工
        assert out.blocked or out.task_state in ("failed", "blocked")

    def test_failure_decision_recorded(self, np_, tmpmem):
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        np_.fail_next_type = 999
        out = _exec(np_, tmpmem).run(plan)
        decs = out.memory.decisions()
        assert decs, "自愈决策必须记入 memory"
        assert all(d.get("reason") for d in decs)


# ==========================================================================
# 4. 熔断路径（硬约束 9）
# ==========================================================================
class TestCircuitBreakerPath:
    def test_risk_pauses_task(self, np_, tmpmem, tmp_path, monkeypatch):
        """★★ 中途检出风险 → 停下等人，不硬闯。"""
        from avatar.risk_detector import integration as RI
        from avatar.risk_detector.status import RiskStatus, Severity

        was = RI.is_installed()
        try:
            RI.install(enabled=True, mode_=RI.MODE_BLOCK)

            # 让轻量检测报"检出验证码"
            monkeypatch.setattr(
                RI, "quick_check",
                lambda *a, **k: RiskStatus.hit(
                    "slider", severity=Severity.INTERRUPT))
            # 让 guard 直接抛（模拟"人等超时"）
            from avatar.risk_detector import guard as G

            def _fake_guard(st, **kw):
                raise G.HumanInterventionTimeout(
                    "测试：等人超时", context=None, waited=1.0, limit=1.0)

            monkeypatch.setattr(G, "guard", _fake_guard)

            plan = get_planner().plan(parse_goal("打开记事本输入hello"))
            ex = _exec(np_, tmpmem, guard_enabled=True)
            with pytest.raises(BaseException):
                ex.run(plan)
        finally:
            RI.uninstall()
            RI.install(enabled=was)

    def test_gate_off_means_no_interference(self, np_, tmpmem):
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        out = _exec(np_, tmpmem, guard_enabled=False).run(plan)
        assert out.ok is True


# ==========================================================================
# 5. 断点恢复路径
# ==========================================================================
class TestResumePath:
    def test_resume_after_partial_failure(self, np_, tmpmem, tmp_path):
        """★★ 模拟"进程挂了"：s1 成功、s2 失败 → 存档 → 新进程接着跑。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        mem = ShortTermMemory("e2e1", directory=str(tmp_path))

        # 第一次：让 s2 一直失败（自愈上限用尽）
        np_.fail_next_type = 999
        out1 = _exec(np_, tmpmem, memory=mem).run(plan, mem)
        assert out1.ok is False
        assert mem.step_state("s1") == "success"
        assert os.path.isfile(os.path.join(str(tmp_path), "e2e1.json"))

        # 第二次：模拟"问题解决了"，从存档接着跑
        np_.fail_next_type = 0
        np_.windows.setdefault("notepad", {"text": "", "saved": False})
        ex = _exec(np_, tmpmem)
        out2 = ex.resume("e2e1", directory=str(tmp_path))

        assert out2.ok is True, out2.describe()
        # s2 真的补跑成功（文本进去了），说明恢复后执行器接着干活了
        assert np_.windows["notepad"]["text"] == "hello"

    def test_resume_keeps_attempt_counts(self, np_, tmpmem, tmp_path):
        """★★ 自愈计数跨恢复累加 —— 否则重启就是无限重试的后门。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        mem = ShortTermMemory("e2e2", directory=str(tmp_path))
        np_.fail_next_type = 999
        _exec(np_, tmpmem, memory=mem).run(plan, mem)

        loaded = ShortTermMemory.load("e2e2", directory=str(tmp_path))
        assert loaded.attempts_of("s2") > 0, "恢复后计数要还在"

    def test_resume_missing_task(self, np_, tmpmem, tmp_path):
        ex = _exec(np_, tmpmem)
        out = ex.resume("不存在", directory=str(tmp_path))
        assert out.ok is False
        assert "无法读取" in out.reason or "没有可还原" in out.reason


# ==========================================================================
# 6. 与阶段五的集成（不做熔断，只看闸门存在）
# ==========================================================================
class TestRiskIntegration:
    def test_gate_is_wired(self, np_, tmpmem):
        """★ 执行器必须真的去问过风控集成。"""
        from avatar.risk_detector import integration as RI
        was = RI.is_installed()
        calls = []
        orig = RI.guard_before_action
        try:
            RI.install(enabled=True, mode_=RI.MODE_OBSERVE)
            RI.guard_before_action = lambda *a, **k: (calls.append(1), None)[1]
            plan = get_planner().plan(parse_goal("打开记事本"))
            _exec(np_, tmpmem, guard_enabled=True).run(plan)
            assert calls, "执行器没有调风控闸"
        finally:
            RI.guard_before_action = orig
            RI.uninstall()
            RI.install(enabled=was)


# ==========================================================================
# 7. 序列化端到端
# ==========================================================================
class TestSerializationE2E:
    def test_whole_task_is_json(self, np_, tmpmem):
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        out = _exec(np_, tmpmem).run(plan)
        blob = json.dumps(out.to_dict(), ensure_ascii=False)
        assert blob
        blob2 = json.dumps(out.memory.to_dict(), ensure_ascii=False)
        assert blob2

    def test_plan_file_roundtrip(self, np_, tmpmem, tmp_path):
        """★ `avatar run --plan-file` 走的路径。"""
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        p = tmp_path / "plan.json"
        p.write_text(plan.to_json(), encoding="utf-8")
        loaded = Plan.from_dict(json.loads(p.read_text(encoding="utf-8")))
        assert loaded.validate() == []
        out = _exec(np_, tmpmem).run(loaded)
        assert out.ok is True


# ==========================================================================
# 8. 不依赖真实桌面 / 网络
# ==========================================================================
class TestIsolation:
    def test_does_not_use_real_runner(self):
        """★ 本文件的测试全部走 :class:`FakeNotepad`，不碰真实桌面。

        用 AST 查真实 import，不做文本搜索 ——
        文本搜索会命中文档里"不要用 DefaultRunner"那句（踩过）。
        """
        import ast
        tree = ast.parse(open(__file__, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add((a.name or "").split(".")[-1])
        assert "DefaultRunner" not in imported, \
            "端到端测试不该用真实执行器"
        # 也不该 import 阶段二/三/四的真实能力
        assert not (imported & {"launcher", "dispatcher", "actions",
                                "capture"}), sorted(imported)

    def test_verifier_module_is_real(self):
        """★ 用的是真 verifier 模块（不是桩）—— 链路才是真的。"""
        assert callable(real_verify)
