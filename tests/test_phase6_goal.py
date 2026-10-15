# -*- coding: utf-8 -*-
"""阶段六 · 目标解析与计划校验（``goal`` / ``plan``）。

## 这个文件在守什么

规划链路的**入口**：一句话 → Goal → Plan → 校验通过。

这里测的不是"解析得多聪明"，而是**边界**：

- 解析不出来时**不抛异常**（返回 unknown 的 Goal）
- DAG 不合法时**在生成那一刻就拦住**（不是等执行器跑到一半）
- 无环检查、依赖完整性检查**真的会失败**（不是永远返回通过）

## 与自检的分工

``goal.selfcheck``（61 项）和 ``plan.selfcheck``（56 项）是**内部视角**。
这里是**外部视角** —— 从包级公开面进来，用真实调用方会走的那条路。
两边不重复：自检盯内部不变式，这里盯对外契约。
"""

from __future__ import annotations

import json

import pytest

from avatar.agentic_planner import (
    Goal,
    Plan,
    PlanError,
    RuleBasedPlanner,
    Step,
    parse_goal,
)


# ==========================================================================
# 1. Goal 解析
# ==========================================================================
class TestGoalParsing:
    @pytest.mark.parametrize("text,intent", [
        ("打开记事本", "open"),
        ("输入 hello", "type"),
        ("点击保存", "click"),
        ("关闭记事本", "close"),
        ("等待 3 秒", "wait"),
        ("打开 https://a.com/x", "navigate"),
    ])
    def test_single_action(self, text, intent):
        g = parse_goal(text)
        assert g.intent == intent, g.describe()
        assert g.ok is True

    def test_composite_without_separator(self):
        """★ 没有分隔词的连写句也要能拆。"""
        g = parse_goal("打开记事本输入hello")
        assert len(g.actions) == 2, g.actions
        assert g.actions[0]["action"] == "open"
        assert g.actions[1]["action"] == "type"
        assert g.intent == "composite"

    def test_composite_with_separator(self):
        g = parse_goal("打开记事本，然后输入 hello")
        assert len(g.actions) == 2

    def test_verb_as_object_not_split(self):
        """★ 「点击保存」里"保存"是宾语，不是第二个动作。"""
        g = parse_goal("点击保存")
        assert g.intent == "click", g.actions
        assert g.target == "保存"

    def test_quoted_text_wins(self):
        g = parse_goal("在记事本里输入「你好世界」")
        assert "你好世界" in str(g.actions[0].get("text", "")), g.actions

    def test_app_alias_canonicalized(self):
        assert parse_goal("打开记事本").target == "notepad"
        assert parse_goal("打开计算器").target == "calc"

    def test_unknown_app_kept_verbatim(self):
        """认不出的应用名原样保留 —— **不猜**。"""
        assert parse_goal("打开 Photoshop").target == "Photoshop"

    def test_url_becomes_navigate(self):
        g = parse_goal("打开 https://example.com/login")
        assert g.actions[0]["action"] == "navigate"
        assert "https://example.com/login" in g.actions[0]["target"]

    def test_wait_unit_conversion(self):
        assert parse_goal("等 2 分钟").actions[0]["seconds"] == 120.0
        assert parse_goal("wait 500 ms").actions[0]["seconds"] == 0.5

    def test_budget_defaults(self):
        g = parse_goal("打开记事本")
        assert g.budget_seconds == 1800.0      # 30 分钟
        assert g.budget_steps == 50

    def test_success_criteria_per_action(self):
        g = parse_goal("打开记事本输入hello")
        assert len(g.success) == len(g.actions)
        assert all(s.strip() for s in g.success)


class TestGoalNeverRaises:
    """解析失败是常态，不该做成异常。"""

    @pytest.mark.parametrize("bad", [
        "", "   ", None, "嗯那个东西你懂的", "???", "12345",
        "打开" * 300,
    ])
    def test_never_raises(self, bad):
        g = parse_goal(bad)
        assert isinstance(g, Goal)

    def test_unknown_keeps_instruction(self):
        """★ 解析失败仍保留原话 —— 可以交给 LLM 后端重试。"""
        g = parse_goal("嗯那个东西你懂的")
        assert g.intent == "unknown"
        assert g.instruction == "嗯那个东西你懂的"
        assert g.ok is False

    def test_from_dict_tolerates_garbage(self):
        for bad in ({}, None, {"actions": "不是列表", "budget_seconds": "abc"},
                    {"success": 123}, []):
            assert Goal.from_dict(bad) is not None

    def test_task_id_unique(self):
        ids = {parse_goal("打开记事本").task_id for _ in range(30)}
        assert len(ids) == 30

    def test_roundtrip(self):
        g = parse_goal("打开记事本输入hello")
        back = Goal.from_json(g.to_json())
        assert back.instruction == g.instruction
        assert back.intent == g.intent
        assert len(back.actions) == len(g.actions)
        assert back.task_id == g.task_id


# ==========================================================================
# 2. Plan 校验
# ==========================================================================
def _plan(*specs):
    """``_plan(("a",), ("b", "a"))`` → Plan 带两个步骤。"""
    steps = []
    for spec in specs:
        sid = spec[0]
        deps = list(spec[1:])
        steps.append(Step(id=sid, action="click", depends_on=deps))
    return Plan(steps=steps)


class TestPlanValidation:
    def test_valid_dag_passes(self):
        p = _plan(("a",), ("b", "a"), ("c", "a"), ("d", "b", "c"))
        assert p.validate() == []
        assert p.is_valid is True

    def test_cycle_detected(self):
        """★★ 有环必须在生成时就被拦住。"""
        p = _plan(("a", "c"), ("b", "a"), ("c", "b"))
        problems = p.validate()
        assert any("环" in x for x in problems), problems

    def test_cycle_raises_on_topo(self):
        p = _plan(("a", "b"), ("b", "a"))
        with pytest.raises(PlanError):
            p.topological_order()

    def test_assert_valid_raises_with_details(self):
        p = _plan(("a", "b"), ("b", "a"))
        with pytest.raises(PlanError) as ei:
            p.assert_valid()
        assert ei.value.problems, "异常要带问题清单"

    def test_self_dependency(self):
        assert any("依赖自己" in x for x in _plan(("a", "a")).validate())

    def test_missing_dependency(self):
        problems = _plan(("a",), ("b", "不存在")).validate()
        assert any("不存在" in x for x in problems)

    def test_missing_dep_does_not_fake_cycle(self):
        """★ 缺依赖时不该同时报"有环" —— 一次报两个假问题会误导排查。"""
        problems = _plan(("a",), ("b", "不存在")).validate()
        assert not any("环" in x for x in problems), problems

    def test_duplicate_id(self):
        assert any("重复" in x for x in _plan(("a",), ("a",)).validate())

    def test_empty_action(self):
        p = Plan(steps=[Step(id="a", action="")])
        assert any("没有 action" in x for x in p.validate())

    def test_empty_plan(self):
        assert any("没有任何步骤" in x for x in Plan().validate())

    def test_bad_state(self):
        p = Plan(steps=[Step(id="a", action="x", state="乱写")])
        assert any("状态非法" in x for x in p.validate())

    def test_edges_must_match_depends_on(self):
        p = _plan(("a",), ("b", "a"))
        p.edges = [("a", "b"), ("x", "y")]
        assert any("edges 里有" in x for x in p.validate())
        p.sync_edges()
        assert p.validate() == []


# ==========================================================================
# 3. DAG 调度语义
# ==========================================================================
class TestPlanScheduling:
    def test_topological_order_respects_deps(self):
        p = _plan(("a",), ("b", "a"), ("c", "a"), ("d", "b", "c"))
        order = p.topological_order()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_topological_order_stable(self):
        """★ 拓扑序必须稳定 —— 否则测试和复盘都变成猜谜。"""
        p = _plan(("a",), ("b", "a"), ("c", "a"))
        orders = {tuple(p.topological_order()) for _ in range(20)}
        assert len(orders) == 1, orders

    def test_ready_steps_enables_parallelism(self):
        """★★ DAG 的价值：a 完成后 b 和 c **同时**可执行。"""
        p = _plan(("a",), ("b", "a"), ("c", "a"), ("d", "b", "c"))
        assert p.ready_steps() == ["a"]
        assert set(p.ready_steps({"a"})) == {"b", "c"}
        assert p.ready_steps({"a", "b", "c"}) == ["d"]

    def test_ready_steps_excludes_completed(self):
        """★ 调用方说"完成了"就是完成了 —— 不该再出现在候选里。"""
        p = _plan(("a",), ("b", "a"))
        assert "a" not in p.ready_steps({"a"})

    def test_blocked_forever(self):
        p = _plan(("a",), ("b", "a"), ("c", "b"))
        p.step_by_id("a").state = "failed"
        assert "b" in p.blocked_forever()
        assert "c" not in p.blocked_forever()


# ==========================================================================
# 4. 端到端：目标 → 计划
# ==========================================================================
class TestGoalToPlan:
    def test_simple_goal(self):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本"))
        assert len(plan.steps) == 1
        assert plan.validate() == []

    def test_composite_goal(self):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        assert len(plan.steps) == 2
        assert plan.steps[1].depends_on == [plan.steps[0].id]

    def test_independent_actions_run_parallel(self):
        """★★ 连续 open 之间没有依赖 —— 这是 DAG 而不是列表的实证。"""
        plan = RuleBasedPlanner().plan(parse_goal("打开浏览器，打开记事本"))
        assert plan.steps[0].depends_on == []
        assert plan.steps[1].depends_on == []
        assert set(plan.ready_steps()) == {"s1", "s2"}

    def test_parallel_can_be_disabled(self):
        plan = RuleBasedPlanner(parallel_independent=False).plan(
            parse_goal("打开浏览器，打开记事本"))
        assert plan.steps[1].depends_on == [plan.steps[0].id]

    def test_every_step_has_verify(self):
        """硬约束 5：每步可验证。"""
        for text in ("打开记事本", "点击保存", "等待 2 秒",
                     "打开 https://a.com", "关闭记事本"):
            plan = RuleBasedPlanner().plan(parse_goal(text))
            for st in plan.steps:
                assert st.verify.get("kind"), (text, st.id, st.verify)

    def test_every_step_has_on_fail(self):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        assert all(st.on_fail for st in plan.steps)

    def test_undecomposable_goal_raises(self):
        from avatar.agentic_planner import PlannerError
        with pytest.raises(PlannerError):
            RuleBasedPlanner().plan(parse_goal("嗯那个东西你懂的"))

    def test_plan_is_json_serializable(self):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        blob = json.dumps(plan.to_dict(), ensure_ascii=False)
        assert blob
        back = Plan.from_dict(json.loads(blob))
        assert len(back.steps) == len(plan.steps)
        assert back.validate() == []

    def test_describe_draws_dag(self):
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        text = plan.describe()
        assert "s1" in text and "s2" in text


# ==========================================================================
# 5. 可插拔后端（硬约束 2）
# ==========================================================================
class TestPlannerBackend:
    def test_backends_do_not_share_implementation(self):
        """★★ 两个后端只共享接口，不共享实现。"""
        from avatar.agentic_planner import (LLMPlanner, PlannerBackend,
                                            RuleBasedPlanner as RBP)
        assert RBP.__bases__ == (PlannerBackend,)
        assert LLMPlanner.__bases__ == (PlannerBackend,)
        assert not issubclass(LLMPlanner, RBP)
        assert not issubclass(RBP, LLMPlanner)

    def test_abstract_cannot_instantiate(self):
        from avatar.agentic_planner import PlannerBackend
        with pytest.raises(TypeError):
            PlannerBackend()

    def test_llm_planner_falls_back_when_disabled(self):
        """★ 阶段七把 LLMPlanner 实现了。

        未启用时**回退规则版**（硬约束 7），不是抛异常。
        阶段六这条原本断言"抛 NotImplementedError" ——
        实现之后就改成断言"回退"。
        """
        from avatar.agentic_planner import LLMPlanner
        lp = LLMPlanner(enabled=False)
        p = lp.plan(parse_goal("打开记事本"))
        assert "llm-fallback" in p.planner
        assert p.validate() == []

    def test_llm_disabled_by_default(self):
        from avatar.agentic_planner import LLMPlanner
        assert LLMPlanner().active is False
        assert LLMPlanner(enabled=True).active is False   # 还缺 key

    def test_llm_request_construction_is_testable(self):
        """★ 请求构造是纯函数 —— 阶段七接网络时只需补 plan()。"""
        from avatar.agentic_planner import LLMPlanner
        lp = LLMPlanner(enabled=True, api_key="sk-test-1234567890")
        payload = lp.build_payload(parse_goal("打开记事本"))
        assert json.dumps(payload, ensure_ascii=False)
        assert payload["temperature"] == 0.0
        assert len(payload["messages"]) == 2
        assert lp.build_headers()["Authorization"].startswith("Bearer ")

    def test_llm_describe_redacts_key(self):
        from avatar.agentic_planner import LLMPlanner
        lp = LLMPlanner(enabled=True, api_key="sk-test-1234567890")
        assert "sk-test-1234567890" not in lp.describe()

    def test_unknown_backend_raises(self):
        from avatar.agentic_planner import get_planner
        with pytest.raises(ValueError):
            get_planner("gpt5")
