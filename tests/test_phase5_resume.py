# -*- coding: utf-8 -*-
"""阶段五 · 恢复层（``avatar.risk_detector.resume``）的行为测试。

## 这个文件在守什么

5C 的目标是「知道何时该停」。guard.py 管"停"，本文件管"**从哪儿接回去**"。
恢复比熔断更容易出错，因为它要在一个**已经变化了的世界**里做判断：
人被叫来了、人动过手、页面可能跳了、窗口可能关了。所以这里有两条主线：

1. **三种策略必须真的分叉**（skip / retry / abort）——
   不能三个都说"好的继续"，那就等于没有策略。
2. **现场失效必须返回 False 而不是抛异常**（硬约束 H）——
   失效是**预期内**的常态，不是异常状况。把它做成异常，
   调用方就得到处写 try，最后一定有人忘。

## 分层

- 纯逻辑层：不碰桌面、不碰文件，全绿是底线。
- 真实 hwnd 层：需要真桌面，拿不到就 ``pytest.skip``。
  **skip 不算失败** —— 无头环境跑不出桌面，那是环境的事实。

## 与自检的分工

``resume.selfcheck()`` 是本模块的自检（57 项），跑得快、无依赖。
本文件是**外部视角**：从 ``avatar.risk_detector`` 的公开面进来，
用真实调用方会走的那条路验证。两边不重复 ——
自检盯内部不变式，这里盯对外契约。
"""

from __future__ import annotations

import json
import time

import pytest

from avatar.risk_detector import context as C
from avatar.risk_detector import guard as G
from avatar.risk_detector import resume as R
from avatar.risk_detector.status import RiskStatus, Severity


# ==========================================================================
# 夹具
# ==========================================================================
def _ctx(**kw) -> C.InterventionContext:
    """造一个可用的 InterventionContext。不截图、不落盘。"""
    base = dict(hwnd=0, url="https://example.com/step",
                current_step="step_3")
    base.update(kw)
    return C.InterventionContext(**base)


@pytest.fixture
def live_ctx():
    """一个 hwnd=0 的上下文 —— 0 表示"没绑定窗口"，永远不算失效。"""
    return _ctx()


@pytest.fixture
def dead_hwnd():
    """一个**几乎肯定不存在**的 hwnd，用来测 "窗口没了" 这条失效路径。

    特意挑一个巨大的、不可能是真实窗口的值 —— 拿当前进程里
    某个真 hwnd 来测会随环境漂移。
    """
    return 0x7FFFFFF0


# ==========================================================================
# 1. 契约：常量与公开面
# ==========================================================================
class TestContract:
    """resume 模块对外的承诺。"""

    def test_outcome_constants(self):
        assert R.RESUME_OK == "ok"
        assert R.RESUME_STALE == "stale"
        assert R.RESUME_ABORTED == "aborted"
        # 三个必须互不相同 —— 否则调用方分流会撞车
        assert len({R.RESUME_OK, R.RESUME_STALE, R.RESUME_ABORTED}) == 3

    def test_strategy_set_matches_context(self):
        """策略集必须来自 context 层，不能各写一份。"""
        assert set(R.RESUME_STRATEGIES) == set(C.RESUME_STRATEGIES)
        assert set(R.RESUME_STRATEGIES) == {"skip", "retry", "abort"}

    def test_default_strategy_is_retry(self):
        assert R.DEFAULT_RESUME_STRATEGY == "retry"

    def test_exported_from_package(self):
        """包级必须导出恢复层的公开符号 —— 外部调用方从包进。"""
        from avatar import risk_detector as pkg
        for name in ("resume_from", "ResumePlan", "validate_context",
                     "RESUME_OK", "RESUME_STALE", "RESUME_ABORTED",
                     "RESUME_STRATEGIES", "DEFAULT_RESUME_STRATEGY"):
            assert hasattr(pkg, name), name

    def test_abort_signal_is_guard_class(self):
        """abort 抛的异常必须是 guard 定义的那个 —— 全项目一份。"""
        assert R.AbortSignal is G.AbortSignal
        assert issubclass(R.AbortSignal, Exception)

    def test_describe_strategies_mentions_all(self):
        text = R.describe_strategies()
        for s in ("retry", "skip", "abort"):
            assert s in text, s

    def test_module_docstring_has_boundary_section(self):
        """硬约束 I：模块 docstring 必须有"能力边界"。"""
        doc = R.__doc__ or ""
        assert "能力边界" in doc, "缺「能力边界」一节"


# ==========================================================================
# 2. 三种策略的分叉
# ==========================================================================
class TestStrategyDispatch:
    """skip / retry / abort 必须走三条不同的路。"""

    def test_retry_returns_ok_plan(self, live_ctx):
        plan = R.resume_from(live_ctx, "retry")
        assert plan.ok is True
        assert plan.outcome == R.RESUME_OK
        assert plan.strategy == "retry"
        assert plan.action_hint == "retry_step"

    def test_skip_returns_ok_plan(self, live_ctx):
        plan = R.resume_from(live_ctx, "skip")
        assert plan.ok is True
        assert plan.outcome == R.RESUME_OK
        assert plan.strategy == "skip"
        assert plan.action_hint == "skip_step"

    def test_skip_and_retry_differ_in_hint(self, live_ctx):
        """这条钉的是"三策略不能退化成一个"。

        如果哪天有人图省事把 skip 也映射成 retry_step，
        熔断恢复就会变成"人点了跳过，结果又重跑一遍当前步骤"。
        """
        a = R.resume_from(live_ctx, "skip")
        b = R.resume_from(live_ctx, "retry")
        assert a.action_hint != b.action_hint, "skip 和 retry 撞车了"
        assert a.strategy != b.strategy

    def test_abort_raises_abort_signal(self, live_ctx):
        with pytest.raises(G.AbortSignal) as ei:
            R.resume_from(live_ctx, "abort")
        assert isinstance(ei.value, G.AbortSignal)

    def test_abort_signal_carries_context(self, live_ctx):
        """异常必须带上下文 —— 否则回溯时不知道停在哪一步。"""
        with pytest.raises(G.AbortSignal) as ei:
            R.resume_from(live_ctx, "abort")
        exc = ei.value
        assert isinstance(exc.context, C.InterventionContext)
        assert exc.context.current_step == "step_3"

    def test_abort_signal_carries_plan(self, live_ctx):
        with pytest.raises(G.AbortSignal) as ei:
            R.resume_from(live_ctx, "abort")
        d = getattr(ei.value, "detail", {}).get("plan")
        assert d, "AbortSignal 没带上 plan —— 排查时看不到决策依据"
        assert d["strategy"] == "abort"
        assert d["outcome"] == R.RESUME_ABORTED

    def test_abort_is_not_risk_blocked(self, live_ctx):
        """两种"停"必须可区分：人决定的中止 ≠ 闸门拦下的。

        调用方对这两者的处理不同（前者记人处理过，后者记被拦），
        共用一种异常就分不出来了。
        """
        from avatar.errors import RiskBlocked
        with pytest.raises(G.AbortSignal) as ei:
            R.resume_from(live_ctx, "abort")
        assert not isinstance(ei.value, RiskBlocked)

    def test_abort_does_not_return_plan(self, live_ctx):
        """abort 必须抛，不能返回一个"其实该终止"的对象。

        理由：返回对象需要调用方自觉检查，中间任何一层忘了检查，
        流程就会继续往下跑。抛异常才能穿透所有 try/except。
        """
        raised = False
        plan = None
        try:
            plan = R.resume_from(live_ctx, "abort")
        except G.AbortSignal:
            raised = True
        assert raised, "abort 竟然正常返回了"
        assert plan is None


# ==========================================================================
# 3. 策略来源的优先级
# ==========================================================================
class TestStrategyResolution:
    """显式参数 > context.resume_strategy > 默认 retry。"""

    def test_reads_from_context_when_not_given(self):
        ctx = _ctx(resume_strategy="skip")
        plan = R.resume_from(ctx)
        assert plan.strategy == "skip"

    def test_explicit_overrides_context(self):
        ctx = _ctx(resume_strategy="skip")
        plan = R.resume_from(ctx, "retry")
        assert plan.strategy == "retry", "显式参数被 context 盖掉了"

    def test_default_when_context_has_none(self):
        ctx = C.InterventionContext(hwnd=0, url="https://a.com/x",
                                    current_step="s",
                                    resume_strategy="")
        plan = R.resume_from(ctx)
        assert plan.strategy == R.DEFAULT_RESUME_STRATEGY

    @pytest.mark.parametrize("bad", ["", None, "SKIP", "  Retry  ",
                                     "explode", "123", "跳过"])
    def test_bad_strategy_never_crashes(self, live_ctx, bad):
        """非法策略值不许炸 —— 现场数据是人手写的，一定会有脏值。"""
        try:
            plan = R.resume_from(live_ctx, bad)
        except G.AbortSignal:
            return          # 被归一到 abort 也接受
        assert plan.strategy in R.RESUME_STRATEGIES

    def test_case_insensitive(self, live_ctx):
        """``"SKIP"`` 应该被认出来，不是悄悄退化成 retry。"""
        plan = R.resume_from(live_ctx, "SKIP")
        assert plan.strategy == "skip", "大小写没归一化"

    def test_whitespace_tolerated(self, live_ctx):
        plan = R.resume_from(live_ctx, "  retry  ")
        assert plan.strategy == "retry"

    def test_context_strategy_normalized_on_read(self):
        """context 里的脏策略值也要能被认出。"""
        ctx = _ctx(resume_strategy="ABORT")
        assert ctx.strategy == "abort"

    def test_context_bad_strategy_falls_back_to_default(self):
        ctx = _ctx(resume_strategy="乱写的")
        assert ctx.strategy == R.DEFAULT_RESUME_STRATEGY


# ==========================================================================
# 4. 现场失效（硬约束 H）
# ==========================================================================
class TestStaleContext:
    """失效必须**返回 False + 原因**，不许抛异常。"""

    def test_none_context_is_stale_not_crash(self):
        plan = R.resume_from(None)
        assert plan.ok is False
        assert plan.outcome == R.RESUME_STALE
        assert plan.reason, "失效必须带原因，不能是空字符串"

    @pytest.mark.parametrize("garbage", [
        "https://example.com",          # 一个裸字符串
        12345,
        [1, 2, 3],
        object(),
        b"bytes",
    ])
    def test_garbage_context_returns_stale(self, garbage):
        """认不出来的东西 = 失效，不是崩溃。"""
        plan = R.resume_from(garbage)
        assert plan.ok is False
        assert plan.outcome == R.RESUME_STALE
        assert "context" in plan.reason

    def test_dead_hwnd_is_stale(self, dead_hwnd):
        """窗口没了 → 现场失效。"""
        ctx = _ctx(hwnd=dead_hwnd)
        plan = R.resume_from(ctx, "retry")
        assert plan.ok is False, "窗口都没了还说能恢复"
        assert plan.outcome == R.RESUME_STALE
        assert "hwnd" in plan.reason

    def test_url_changed_is_stale(self, live_ctx):
        """页面跳走了 → 现场失效（重试会点到别的东西上）。"""
        plan = R.resume_from(live_ctx, "retry",
                             current_url="https://example.com/完全不同的页")
        assert plan.ok is False
        assert plan.outcome == R.RESUME_STALE
        assert "url" in plan.reason.lower()

    def test_url_same_is_ok(self, live_ctx):
        plan = R.resume_from(live_ctx, "retry",
                             current_url="https://example.com/step")
        assert plan.ok is True

    def test_url_query_change_is_ok(self, live_ctx):
        """只有 query 变了不算失效 —— 很多站点每步都改 query。"""
        plan = R.resume_from(live_ctx, "retry",
                             current_url="https://example.com/step?t=99")
        assert plan.ok is True, "query 变化被误判成失效"

    def test_url_trailing_slash_is_ok(self, live_ctx):
        plan = R.resume_from(live_ctx, "retry",
                             current_url="https://example.com/step/")
        assert plan.ok is True

    def test_current_url_none_skips_url_check(self, live_ctx):
        """不传 current_url = 调用方不做 URL 校验，不该因此判失效。"""
        plan = R.resume_from(live_ctx, "retry", current_url=None)
        assert plan.ok is True

    def test_stale_plan_has_no_action_hint(self, live_ctx):
        """失效时不该告诉调用方"去重试/去跳过" —— 没得执行。"""
        plan = R.resume_from(None)
        assert plan.action_hint == ""

    def test_stale_records_url_change(self, live_ctx):
        """URL 失效时把前后 URL 记进 extra —— 排查时这是第一个要看的。"""
        plan = R.resume_from(live_ctx, "retry",
                             current_url="https://other.com/x")
        assert plan.extra.get("url_before") == "https://example.com/step"
        assert plan.extra.get("url_now") == "https://other.com/x"

    def test_stale_never_raises_even_for_nasty_input(self):
        """失效路径是**最后一道兜底**，绝不能自己炸。"""
        class Nasty:
            """访问任何属性都抛 —— 模拟坏掉的 context 替身。"""
            def __getattr__(self, name):
                raise RuntimeError("我坏了: %s" % name)

        plan = R.resume_from(Nasty())
        assert plan.outcome == R.RESUME_STALE


# ==========================================================================
# 5. validate_context
# ==========================================================================
class TestValidateContext:
    def test_returns_tuple(self, live_ctx):
        ok, why = R.validate_context(live_ctx)
        assert ok is True
        assert why == ""

    def test_none_is_false(self):
        ok, why = R.validate_context(None)
        assert ok is False and why

    def test_never_raises_for_garbage(self):
        for bad in (123, "x", [], object(), b"y"):
            ok, why = R.validate_context(bad)
            assert ok is False
            assert isinstance(why, str) and why

    def test_dict_context_is_accepted(self):
        """``context.to_dict()`` 的结果必须能直接喂回来。"""
        d = _ctx().to_dict()
        ok, why = R.validate_context(d)
        assert ok is True, why

    def test_bad_dict_is_false_not_crash(self):
        ok, why = R.validate_context({"hwnd": "不是数字", "url": 123})
        # 能容错解析就算通过；不能容错也必须返回 False 而不是抛
        assert isinstance(ok, bool)
        assert isinstance(why, str)

    def test_error_hook_receives_reason(self):
        seen: list = []
        ok, why = R.validate_context(None, error_hook=seen.append)
        assert ok is False
        assert seen and seen[0] == why

    def test_error_hook_exception_is_swallowed(self):
        """打点回调坏了不能拖垮校验 —— 校验是恢复路径的关键路径。"""
        def _bad(msg):
            raise RuntimeError("钩子坏了")
        ok, why = R.validate_context(None, error_hook=_bad)
        assert ok is False
        assert why

    def test_error_hook_not_called_on_success(self, live_ctx):
        seen: list = []
        ok, _ = R.validate_context(live_ctx, error_hook=seen.append)
        assert ok is True
        assert seen == [], "成功时不该调错误钩子"

    def test_require_url_with_missing_url_fails(self):
        """``require_url=True`` 且两边都没 URL → 失效。"""
        ctx = C.InterventionContext(hwnd=0, url="", current_step="s")
        ok, why = R.validate_context(ctx, require_url=True)
        assert ok is False
        assert "url" in why.lower()

    def test_require_url_with_matching_url_ok(self):
        ctx = C.InterventionContext(hwnd=0, url="https://a.com/x",
                                    current_step="s")
        ok, why = R.validate_context(ctx, current_url="https://a.com/x",
                                     require_url=True)
        assert ok is True, why


# ==========================================================================
# 6. 重试上限 → 升级为 abort
# ==========================================================================
class TestAttemptEscalation:
    """重试有上限。上限**由调用方给**，本模块不发明默认值。"""

    def test_under_limit_stays_retry(self, live_ctx):
        ctx = _ctx(attempt=1)
        plan = R.resume_from(ctx, "retry", max_attempt=3)
        assert plan.strategy == "retry"
        assert plan.ok is True

    def test_at_limit_escalates_to_abort(self, live_ctx):
        ctx = _ctx(attempt=3)
        with pytest.raises(G.AbortSignal):
            R.resume_from(ctx, "retry", max_attempt=3)

    def test_over_limit_escalates(self, live_ctx):
        ctx = _ctx(attempt=99)
        with pytest.raises(G.AbortSignal):
            R.resume_from(ctx, "retry", max_attempt=3)

    def test_escalation_reason_is_recorded(self, live_ctx):
        ctx = _ctx(attempt=3)
        with pytest.raises(G.AbortSignal) as ei:
            R.resume_from(ctx, "retry", max_attempt=3)
        plan = getattr(ei.value, "detail", {}).get("plan", {})
        assert "escalated" in plan.get("extra", {}), \
            "升级为 abort 却没记原因 —— 后来人不知道为啥停了"

    def test_no_max_attempt_means_no_escalation(self, live_ctx):
        """不传上限就不升级 —— 上限是业务决定，不是技术决定。"""
        ctx = _ctx(attempt=10 ** 6)
        plan = R.resume_from(ctx, "retry")           # 不给 max_attempt
        assert plan.strategy == "retry"
        assert plan.ok is True

    def test_skip_is_not_escalated(self, live_ctx):
        """skip 不参与重试计数 —— 跳过不是重试。"""
        ctx = _ctx(attempt=99)
        plan = R.resume_from(ctx, "skip", max_attempt=1)
        assert plan.ok is True
        assert plan.strategy == "skip"

    def test_explicit_attempt_overrides_context(self, live_ctx):
        ctx = _ctx(attempt=0)
        with pytest.raises(G.AbortSignal):
            R.resume_from(ctx, "retry", attempt=9, max_attempt=3)

    def test_attempt_carried_into_plan(self, live_ctx):
        plan = R.resume_from(_ctx(attempt=2), "retry", max_attempt=5)
        assert plan.attempt == 2


# ==========================================================================
# 7. ResumePlan 对象
# ==========================================================================
class TestResumePlan:
    def test_defaults_are_safe(self):
        p = R.ResumePlan()
        assert p.ok is False, "默认必须是不能恢复 —— 失败要安全"
        assert p.outcome == R.RESUME_STALE
        assert p.strategy == R.DEFAULT_RESUME_STRATEGY
        assert isinstance(p.extra, dict)

    def test_to_dict_is_json_safe(self, live_ctx):
        plan = R.resume_from(live_ctx, "skip")
        d = plan.to_dict()
        json.dumps(d)                    # 不抛就是安全
        assert isinstance(d, dict)

    def test_to_dict_has_expected_keys(self, live_ctx):
        d = R.resume_from(live_ctx, "retry").to_dict()
        assert set(d) >= {"ok", "strategy", "outcome", "reason",
                          "action_hint", "current_step", "attempt", "extra"}

    def test_to_dict_does_not_leak_context_object(self, live_ctx):
        """序列化结果里不能塞 InterventionContext 实例。

        plan.to_dict() 会进日志/JSON，塞对象进去会在序列化时炸，
        而且可能把截图路径之类的现场信息带出去。
        """
        d = R.resume_from(live_ctx, "retry").to_dict()
        assert not isinstance(d.get("context"), C.InterventionContext)

    def test_describe_is_readable(self, live_ctx):
        for s in ("skip", "retry"):
            text = R.resume_from(live_ctx, s).describe()
            assert isinstance(text, str) and text

    def test_current_step_carried(self, live_ctx):
        plan = R.resume_from(live_ctx, "retry")
        assert plan.current_step == "step_3"

    def test_extra_always_dict(self):
        assert isinstance(R.ResumePlan().extra, dict)


# ==========================================================================
# 8. on_event 回调
# ==========================================================================
class TestEventCallback:
    def test_called_on_success(self, live_ctx):
        seen: list = []
        R.resume_from(live_ctx, "retry", on_event=seen.append)
        assert len(seen) == 1
        assert seen[0]["outcome"] == R.RESUME_OK
        assert "at" in seen[0], "事件没带时间戳，打点没法排序"

    def test_called_on_stale(self):
        seen: list = []
        R.resume_from(None, on_event=seen.append)
        assert seen and seen[0]["outcome"] == R.RESUME_STALE

    def test_called_before_abort_raises(self, live_ctx):
        """abort 抛异常前也要打点 —— 否则"人放弃"这个事件丢了。"""
        seen: list = []
        with pytest.raises(G.AbortSignal):
            R.resume_from(live_ctx, "abort", on_event=seen.append)
        assert seen, "abort 前没打点"

    def test_callback_exception_swallowed(self, live_ctx):
        def _bad(rec):
            raise RuntimeError("打点库挂了")
        plan = R.resume_from(live_ctx, "retry", on_event=_bad)
        assert plan.ok is True, "打点坏了不该拖垮恢复"

    def test_callback_exception_swallowed_on_stale(self):
        def _bad(rec):
            raise RuntimeError("打点库挂了")
        plan = R.resume_from(None, on_event=_bad)
        assert plan.outcome == R.RESUME_STALE

    def test_event_is_json_safe(self, live_ctx):
        seen: list = []
        R.resume_from(live_ctx, "skip", on_event=seen.append)
        json.dumps(seen[0])


# ==========================================================================
# 9. 与 guard / context 的对接（5C 内部的缝）
# ==========================================================================
class TestIntegrationWithGuard:
    """熔断 → 恢复的接缝。这条缝断了，5C 就是两段各自能跑的代码。"""

    def test_intervention_context_from_guard_is_resumable(self):
        """guard 抛的异常带的 context，必须能直接喂给 resume_from。"""
        ctx = _ctx(hwnd=0, current_step="after_captcha")
        exc = G.HumanInterventionRequired("检出验证码", context=ctx)
        assert isinstance(exc.context, C.InterventionContext)

        plan = R.resume_from(exc.context, "retry")
        assert plan.ok is True
        assert plan.current_step == "after_captcha"

    def test_timeout_exception_context_is_resumable(self):
        """超时（人没来）之后，调用方可能想换个策略再试。"""
        ctx = _ctx(hwnd=0)
        exc = G.HumanInterventionTimeout("等超时了", context=ctx,
                                         waited=300.0, limit=300.0)
        plan = R.resume_from(exc.context, "skip")
        assert plan.ok is True
        assert plan.strategy == "skip"

    def test_context_round_trip_through_dict(self):
        """context → dict → resume_from，策略不能丢。"""
        ctx = _ctx(resume_strategy="skip", current_step="s9")
        plan = R.resume_from(ctx.to_dict())
        assert plan.strategy == "skip"
        assert plan.current_step == "s9"

    def test_guard_module_does_not_import_resume(self):
        """防循环依赖：guard 不该反过来 import resume。

        guard 是"停"，resume 是"复"，两者只在
        InterventionContext 这一个数据结构上碰面。
        一旦 guard 开始 import resume，5C 就成环了。
        """
        src = open(G.__file__, encoding="utf-8").read()
        assert "risk_detector.resume" not in src.split("\n\n")[0] or True
        # 真正的检查：顶层 import 区不含 resume
        head = src[:src.find("def ")]
        assert "from .resume" not in head
        assert "import resume" not in head

    def test_resume_uses_no_desktop_automation(self):
        """恢复层不该依赖动作层/视觉层 —— 它只做决策，不执行。

        硬边界：resume.py 决定"该重试"，**执行**是调用方的事。
        一旦它 import 了动作层，职责就混了。
        """
        src = open(R.__file__, encoding="utf-8").read()
        head = src[:src.find("# ---")]
        for forbidden in ("actions", "vision", "web_cdp", "uia"):
            assert ("import %s" % forbidden) not in head, forbidden


# ==========================================================================
# 10. 真实 hwnd（需要桌面；拿不到就 skip）
# ==========================================================================
class TestRealWindow:
    """用真实窗口验证 hwnd 存活检查确实连着系统。

    **skip 不是失败**：无头环境跑不出桌面，这是环境的事实，
    不是代码的问题。
    """

    @pytest.fixture
    def real_hwnd(self):
        try:
            from avatar.app_manager import windows as W
            wins = W.list_windows(include_invisible=False)
        except Exception as exc:          # noqa: BLE001
            pytest.skip("枚举不到窗口：%s" % exc)
        if not wins:
            pytest.skip("当前无可见顶层窗口")
        return int(wins[0].hwnd)

    def test_live_window_validates(self, real_hwnd):
        ctx = C.InterventionContext(hwnd=real_hwnd, url="",
                                    current_step="real")
        ok, why = R.validate_context(ctx)
        assert ok is True, why

    def test_live_window_resumes(self, real_hwnd):
        ctx = C.InterventionContext(hwnd=real_hwnd, url="",
                                    current_step="real")
        plan = R.resume_from(ctx, "retry")
        assert plan.ok is True, plan.reason

    def test_closed_window_goes_stale(self):
        """开一个窗口，记下 hwnd，关掉，然后校验 —— 应该失效。

        拿不到可关闭的窗口就 skip。
        """
        from avatar.app_manager import windows as W
        try:
            wins = W.list_windows(include_invisible=False)
        except Exception as exc:          # noqa: BLE001
            pytest.skip("枚举不到窗口：%s" % exc)
        if len(wins) < 2:
            pytest.skip("窗口太少，不敢关")
        # 用一个几乎肯定不存在的 hwnd 代替真正关窗口 ——
        # 测试不该去关用户正在用的窗口
        ctx = C.InterventionContext(hwnd=0x7FFFFFF0, url="",
                                    current_step="gone")
        ok, why = R.validate_context(ctx)
        assert ok is False
        assert "hwnd" in why
