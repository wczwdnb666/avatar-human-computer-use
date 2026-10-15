# -*- coding: utf-8 -*-
"""阶段五 · 熔断与人工介入测试（批次 5C 初版）。

## 分层

和 ``test_phase4_cdp.py`` 同构，按"离真实桌面多远"分三层：

1. **纯逻辑层** —— 动作翻译、异常携带 context、环境判断。
   零副作用，毫秒级。
2. **打桩层** —— 把 ``_user32`` / ``_sleep`` / ``env_report`` 换成
   替身，验证等待循环的**行为**：去抖、超时、should_stop、Ctrl+C。
   **不打桩全局 ``time.sleep``** —— 那会污染整个 pytest 进程。
3. **真桌面层** —— 真有桌面时才跑；没有就 ``skip``。
   **skip 不是失败**（CI 机器上通常没有交互桌面）。

## 为什么大量用 ``guard._sleep`` 而不是 ``time.sleep``

``guard`` 里所有睡眠都走模块级别名 ``_sleep``。测试只改它 ——
影响面精确到 guard 自己。改 ``time.sleep`` 是全进程操作，
pytest 自己的计时、超时、插件调度都会受影响，是那种
"跑一次之后后面全乱"的脏改动。

## 5G 会补什么

- 真实的 F9 按键注入（现在只验证轮询逻辑，不真按键）；
- 无桌面环境的真实验证（现在靠打桩 env_report）；
- 熔断截图与人眼可读性的端到端（现在只验证"文件写出且是合法 BMP"）。
"""

from __future__ import annotations

import os
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.risk_detector import context as C          # noqa: E402
from avatar.risk_detector import guard as G            # noqa: E402
from avatar.risk_detector.status import (              # noqa: E402
    RiskStatus,
    RiskType,
    Severity,
)


# ==========================================================================
# 夹具
# ==========================================================================
def _ctx(**kw) -> C.InterventionContext:
    base = dict(hwnd=0, url="https://a.com/step", current_step="step_1")
    base.update(kw)
    return C.InterventionContext(**base)


@pytest.fixture
def no_desktop(monkeypatch):
    """把环境探测打桩成"没有桌面"。"""
    monkeypatch.setattr(G, "env_report", lambda: {
        "session": "remote", "desktop": False, "beep": False,
        "hotkey": False, "notify": False, "reason": "测试打桩：无桌面",
        "platform": "win32",
    })
    return True


@pytest.fixture
def with_desktop(monkeypatch):
    """把环境探测打桩成"有桌面"，并静音所有 UI 副作用。"""
    monkeypatch.setattr(G, "env_report", lambda: {
        "session": "console", "desktop": True, "beep": False,
        "hotkey": True, "notify": False, "reason": "", "platform": "win32",
    })
    monkeypatch.setattr(G, "beep", lambda *a, **k: False)
    monkeypatch.setattr(G, "notify", lambda *a, **k: False)
    monkeypatch.setattr(G, "log_intervention", lambda text, **k: "")
    return True


class FakeKeyState:
    """``GetAsyncKeyState`` 的替身。

    可以设定"第 N 次调用时开始返回按下"，或者一直不按，
    或者抛异常（用于测 Ctrl+C 传播）。
    """

    def __init__(self, press_at: int = 1, raise_exc=None) -> None:
        self.press_at = press_at
        self.raise_exc = raise_exc
        self.calls = 0

    def GetAsyncKeyState(self, vk):      # noqa: N802 —— 对齐 Win32 名
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return -32768 if self.calls >= self.press_at else 0


@pytest.fixture
def fake_user32(monkeypatch):
    """替换 ``guard._user32``，返回一个可配置的 FakeKeyState 安装器。

    ``_install(press_at=..., raise_exc=...)`` 装好替身并返回它；
    装好之后 ``_install.last`` 指向同一个对象，
    测试可以直接读 ``last.calls`` 看轮询了几轮。
    """
    box = {"fake": FakeKeyState()}

    def _install(**kw):
        box["fake"] = FakeKeyState(**kw)
        monkeypatch.setattr(G, "_user32", lambda: box["fake"])
        _install.last = box["fake"]
        return box["fake"]

    _install.last = box["fake"]
    return _install


@pytest.fixture
def spy_sleep(monkeypatch):
    """替换 ``guard._sleep`` —— **只动 guard 的别名，不碰全局 time**。"""
    seen: list = []

    def _sleep(s):
        seen.append(round(float(s), 4))
        time.sleep(min(float(s), 0.002))     # 真睡一点点，保持时序真实
    monkeypatch.setattr(G, "_sleep", _sleep)
    return seen


# ==========================================================================
# 1. 常量与契约（纯逻辑）
# ==========================================================================
class TestContract:
    """硬约束里的常量必须在，且必须是我们承诺的值。"""

    def test_vk_f9(self):
        assert G.VK_F9 == 0x78

    def test_poll_interval_is_50ms(self):
        """硬约束 A：轮询间隔必须是 0.05s。"""
        assert G.DEFAULT_POLL_INTERVAL == 0.05

    def test_debounce_is_300ms(self):
        """硬约束 A：去抖必须是 0.3s。"""
        assert G.DEBOUNCE_SECONDS == 0.3

    def test_default_timeout_5min(self):
        """硬约束 #7：默认等人 300s。"""
        assert G.DEFAULT_INTERVENTION_TIMEOUT == 300.0

    def test_sm_remotesession_value(self):
        assert G.SM_REMOTESESSION == 0x1000

    def test_guard_entry_matches_action_interface(self):
        """guard 的入口名必须与 status.ACTION_INTERFACE 里写的一致 ——
        这是"两边不许各定义一套"的机器化检查。"""
        from avatar.risk_detector.status import ACTION_INTERFACE, ACTION_PAUSE
        assert ACTION_INTERFACE[ACTION_PAUSE]["guard_entry"] == "pause_for_human"
        assert callable(getattr(G, "pause_for_human"))

    def test_timeout_exception_name_matches_contract(self):
        """ACTION_INTERFACE 里写的异常名必须真的存在。"""
        from avatar.risk_detector.status import ACTION_INTERFACE, ACTION_PAUSE
        name = ACTION_INTERFACE[ACTION_PAUSE]["raises"]
        assert name == "HumanInterventionTimeout"
        assert hasattr(G, name)


# ==========================================================================
# 2. 动作翻译（硬约束 E / F）
# ==========================================================================
class TestTranslateAction:
    """guard 只从 ACTION_INTERFACE 翻译，不自造判定逻辑。"""

    def test_none_becomes_pause(self):
        """没有状态 = 不知道 = 停下来看。"""
        assert G.translate_action(None) == "pause"

    def test_clean_becomes_continue(self):
        assert G.translate_action(RiskStatus.clean()) == "continue"

    def test_interrupt_becomes_pause(self):
        st = RiskStatus.hit("slider", severity=Severity.INTERRUPT)
        assert G.translate_action(st) == "pause"

    def test_block_becomes_abort(self):
        st = RiskStatus.hit("rate_limit", severity=Severity.BLOCK)
        assert G.translate_action(st) == "abort"

    def test_any_hit_is_not_clean(self):
        """钉住 ``detected=True ⇒ is_clean=False`` 这个不变式。

        注意这条**不能**再推出"命中就一定熔断"了 ——
        那是初版的错误推论，见下一条。
        """
        for sev in (Severity.INFO, Severity.BYPASSABLE,
                    Severity.INTERRUPT, Severity.BLOCK):
            st = RiskStatus.hit("challenge_page", severity=sev)
            assert st.detected is True, sev
            assert st.is_clean is False, \
                "%s 的命中竟然被当成干净 —— 不变式破了" % sev

    def test_bypassable_passes_because_action_continue(self):
        """★★ 2026-10-01 修正：``bypassable`` **放行**，不再熔断。

        ## 为什么改

        初版规定"``is_clean=False`` 一律熔断"，于是 ``bypassable``
        （``SEVERITY_ACTION`` 映射是 ``continue``，"人不该被打断"）
        在闸口被**升级**成 pause。那是 guard 用"事实核对"
        覆盖了 5B 的"产品判断"—— 职责越界。

        ## 修正后的规则

        把 ``is_clean=False`` 拆成两种**完全不同**的情况：

        - ``detected=False``（未知）→ 5B **没做成判断**
          → guard **必须**保守熔断。放行等于盲开车。
        - ``detected=True``（查出来了）→ 5B **做了判断**，
          只是结论是"不必打断" → guard **尊重它**，放行。

        一句话：**guard 只在"5B 没判断"时越权；判断过了就听 5B 的。**

        ## 代价（写清楚，别当没看见）

        放行意味着：如果 5B 把某个该拦的东西错标成 ``bypassable``，
        guard 不会再兜住它。这是**刻意接受**的 —— 因为兜住的代价是
        每次 Cloudflare 无感盾都白停 5 分钟，而那是常态。
        防线在 5B 的 severity 配置上，不在这里。
        """
        st = RiskStatus.hit("challenge_page", severity=Severity.BYPASSABLE)

        # 前提：这三个值的关系是这条测试的地基，地基动了要立刻知道
        assert st.is_clean is False, "前提变了：bypassable 现在是 clean 了"
        assert st.detected is True, "前提变了：bypassable 现在 detected=False"
        assert st.action == "continue", "severity 映射没变"

        assert G.should_intervene(st)[0] is False, \
            "detected=True + action=continue → 放行（信任 5B）"
        assert G.translate_action(st) == "continue"

    def test_info_also_passes(self):
        """``info`` 同理 —— detected=True + action=continue → 放行。"""
        st = RiskStatus.hit("challenge_page", severity=Severity.INFO)
        assert st.detected is True and st.action == "continue"
        assert G.should_intervene(st)[0] is False
        assert G.translate_action(st) == "continue"

    def test_bypassable_was_the_only_case_that_changed(self):
        """钉住"这次修正只动了 bypassable/info，没动别的"。

        改判据最容易出的事故是**顺手把未知也放行了** ——
        那是最危险的回归。这条把四个 severity 的结果全部锁死。
        """
        expect = {
            Severity.INFO: False,          # 放行
            Severity.BYPASSABLE: False,    # 放行 ← 本次修正
            Severity.INTERRUPT: True,      # 熔断
            Severity.BLOCK: True,          # 熔断
        }
        for sev, want in expect.items():
            st = RiskStatus.hit("challenge_page", severity=sev)
            got = G.should_intervene(st)[0]
            assert got is want, "%s 的介入判定变成了 %s" % (sev, got)

    @pytest.mark.parametrize("st,label", [
        (RiskStatus.unknown_(reason="ocr_unavailable"), "ocr 不可用"),
        (RiskStatus.unknown_(reason="no_window"), "枚举不到窗口"),
        (RiskStatus.timeout(), "超时"),
        (RiskStatus.error(OSError("boom")), "检测器抛异常"),
    ])
    def test_unknown_also_pauses(self, st, label):
        """★ ``is_clean=False`` 且 ``detected=False`` → 必须熔断。

        这是修正后**唯一**还强制熔断的 is_clean=False 分支。
        如果只认 ``detected=True``，那么"读不出来页面"会被当成
        "没问题"放行 —— 那是盲开车。修正**没有**放松这一条。
        """
        assert st.is_clean is False, "%s 的 is_clean 应为 False" % label
        assert st.detected is False, "%s 应该是 detected=False" % label
        assert G.should_intervene(st)[0] is True, "%s 必须熔断" % label
        assert G.translate_action(st) == "pause", "%s 必须触发熔断" % label

    def test_unknown_own_action_is_continue_hence_unusable(self):
        """反证：为什么不能直接拿 ``status.action`` 当判据。

        未知状态的 ``action`` 是 ``continue``（RiskStatus 不越权替
        调用方做终止决定）。所以 guard 必须先看 ``detected`` 这道闸 ——
        **不是** is_clean（is_clean=False 有两种情况）。
        """
        st = RiskStatus.unknown_(reason="ocr_unavailable")
        assert st.action == "continue", "未知状态自称 continue 是本系列的设计"
        assert G.should_intervene(st)[0] is True, \
            "guard 必须覆盖这个'自称 continue'的陷阱"
        assert G.translate_action(st) == "pause"

    def test_result_always_in_contract(self):
        """翻译结果必须落在 ACTION_INTERFACE 的三个键里。"""
        from avatar.risk_detector.status import ACTION_INTERFACE
        samples = [
            None,
            RiskStatus.clean(),
            RiskStatus.hit("slider", severity=Severity.INTERRUPT),
            RiskStatus.hit("x", severity=Severity.BLOCK),
            RiskStatus.hit("y", severity=Severity.INFO),
            RiskStatus.unknown_(reason="a"),
            RiskStatus.timeout(),
            RiskStatus.error(ValueError("v")),
        ]
        for st in samples:
            assert G.translate_action(st) in ACTION_INTERFACE


# ==========================================================================
# 2b. 判定入口 should_intervene（2026-10-01 新增）
# ==========================================================================
class TestShouldIntervene:
    """判定表必须逐格对上 —— 这是 5B 与 5C 的职责边界。"""

    def test_returns_tuple_of_bool_str(self):
        for st in (None, RiskStatus.clean(), RiskStatus.unknown_(reason="x")):
            got = G.should_intervene(st)
            assert isinstance(got, tuple) and len(got) == 2
            assert isinstance(got[0], bool)
            assert isinstance(got[1], str)

    def test_none_intervenes(self):
        ok, why = G.should_intervene(None)
        assert ok is True
        assert why == "status_missing"

    def test_clean_passes(self):
        ok, why = G.should_intervene(RiskStatus.clean())
        assert ok is False
        assert why == "clean"

    def test_unknown_intervenes_with_reason_unknown(self):
        ok, why = G.should_intervene(
            RiskStatus.unknown_(reason="ocr_unavailable"))
        assert ok is True
        assert why == "unknown"

    def test_reasons_are_distinct_per_branch(self):
        """每个分支的 reason 必须不同 —— 日志靠它排查。

        如果两个不同分支返回同一个 reason，说明判定被合并了，
        而那正是这次修正要拆开的东西。
        """
        reasons = {
            "missing": G.should_intervene(None)[1],
            "clean": G.should_intervene(RiskStatus.clean())[1],
            "unknown": G.should_intervene(
                RiskStatus.unknown_(reason="x"))[1],
            "continue": G.should_intervene(
                RiskStatus.hit("a", severity=Severity.BYPASSABLE))[1],
            "pause": G.should_intervene(
                RiskStatus.hit("b", severity=Severity.INTERRUPT))[1],
            "abort": G.should_intervene(
                RiskStatus.hit("c", severity=Severity.BLOCK))[1],
        }
        assert len(set(reasons.values())) == len(reasons), reasons

    def test_unreadable_status_is_conservative(self):
        """读不出属性 → 保守介入，不崩。"""
        class _Bad:
            @property
            def is_clean(self):
                raise RuntimeError("坏了")

        ok, why = G.should_intervene(_Bad())
        assert ok is True
        assert "unreadable" in why

    def test_off_contract_action_is_conservative(self):
        """``status.action`` 返回契约外的值 → 保守介入，不放行。"""
        class _Weird:
            is_clean = False
            detected = True
            action = "explode"

        ok, why = G.should_intervene(_Weird())
        assert ok is True, "契约外的动作值不许放行"
        assert "unknown" in why

    def test_does_not_mutate_status(self):
        """纯查询 —— 不许改 status。"""
        st = RiskStatus.hit("slider", severity=Severity.INTERRUPT)
        before = (st.is_clean, st.detected, st.action)
        G.should_intervene(st)
        assert (st.is_clean, st.detected, st.action) == before

    def test_consistent_with_translate_action(self):
        """两个入口**不许分叉** —— 一个判断，两种粒度。"""
        samples = [
            None,
            RiskStatus.clean(),
            RiskStatus.hit("a", severity=Severity.INFO),
            RiskStatus.hit("b", severity=Severity.BYPASSABLE),
            RiskStatus.hit("c", severity=Severity.INTERRUPT),
            RiskStatus.hit("d", severity=Severity.BLOCK),
            RiskStatus.unknown_(reason="x"),
            RiskStatus.timeout(),
            RiskStatus.error(ValueError("v")),
        ]
        for st in samples:
            intervene = G.should_intervene(st)[0]
            act = G.translate_action(st)
            assert (act == "continue") == (not intervene), \
                "分叉了：intervene=%s 但 action=%s" % (intervene, act)

    def test_exported_from_package(self):
        from avatar import risk_detector as pkg
        assert hasattr(pkg, "should_intervene")
        assert pkg.should_intervene(None)[0] is True


# ==========================================================================
# 3. 异常携带 context（硬约束 D）
# ==========================================================================
class TestExceptions:
    """两个异常都必须携带完整 InterventionContext，不允许只传字符串。"""

    def test_required_carries_context_object(self):
        ctx = _ctx(hwnd=7, current_step="s1")
        exc = G.HumanInterventionRequired("测试", context=ctx)
        assert isinstance(exc.context, C.InterventionContext)
        assert exc.context.hwnd == 7
        assert exc.context.current_step == "s1"

    def test_required_accepts_dict(self):
        ctx = _ctx(current_step="from_dict")
        exc = G.HumanInterventionRequired("x", context=ctx.to_dict())
        assert exc.context.current_step == "from_dict", "dict 也要能还原"

    def test_required_without_context_still_has_one(self):
        """没有任何 context 时也给一个空的 —— 永不缺字段。"""
        exc = G.HumanInterventionRequired("裸异常")
        assert isinstance(exc.context, C.InterventionContext)

    def test_required_keeps_raw_context_like_object(self):
        exc = G.HumanInterventionRequired("x", context=object())
        assert "raw_context" in exc.detail, "认不出的东西要留个脚印"

    def test_required_exit_code(self):
        assert G.HumanInterventionRequired("x").exit_code == 5

    def test_required_is_avatar_error(self):
        from avatar.errors import AvatarError
        assert issubclass(G.HumanInterventionRequired, AvatarError)

    def test_required_describe_mentions_step(self):
        exc = G.HumanInterventionRequired("原因", context=_ctx(current_step="提交订单"))
        assert "提交订单" in exc.describe()

    def test_timeout_subclasses_required(self):
        """超时是"需要人"的子类 —— 调用方一个 except 能兜住两种情况。"""
        assert issubclass(G.HumanInterventionTimeout, G.HumanInterventionRequired)

    def test_timeout_carries_waited_and_limit(self):
        exc = G.HumanInterventionTimeout("超时", context=_ctx(),
                                         waited=12.5, limit=30.0)
        assert exc.waited == 12.5
        assert exc.limit == 30.0
        assert "12.5" in str(exc)

    def test_timeout_degraded_flag(self):
        normal = G.HumanInterventionTimeout("t", context=_ctx())
        bad = G.HumanInterventionTimeout("t", context=_ctx(), degraded=True)
        assert normal.is_degraded is False
        assert bad.is_degraded is True

    def test_exceptions_survive_base_class_context_collision(self):
        """⛔ 回归测试：``AvatarError.__init__`` 会设 ``self.context = {}``。

        我们的子类也用一个叫 ``context`` 的属性装 InterventionContext。
        如果 ``super().__init__()`` 排在我们赋值之后，它会把我们的
        对象覆盖成一个空 dict —— 而且**不会报错**，
        只有断言类型才发现。这个坑已经踩过一次，钉死它。
        """
        ctx = _ctx(hwnd=1234)
        for cls, kw in ((G.HumanInterventionRequired, {}),
                        (G.HumanInterventionTimeout,
                         {"waited": 1.0, "limit": 2.0}),
                        (G.AbortSignal, {})):
            exc = cls("x", context=ctx, **kw)
            assert isinstance(exc.context, C.InterventionContext), \
                "%s.context 被基类覆盖成了 %r" % (cls.__name__, type(exc.context))
            assert exc.context.hwnd == 1234, cls.__name__

    def test_abort_signal_is_not_risk_blocked(self):
        """区分「人决定中止」和「闸门拦截」—— exit_code 同是 5，类型不同。"""
        from avatar.errors import RiskBlocked
        assert not issubclass(G.AbortSignal, RiskBlocked)
        assert G.AbortSignal("x").exit_code == RiskBlocked.exit_code

    def test_exception_to_dict(self):
        exc = G.HumanInterventionRequired("原因", context=_ctx(current_step="z"))
        d = exc.to_dict()
        import json
        assert json.dumps(d)                 # JSON 安全
        assert d["type"] == "HumanInterventionRequired"
        assert d["context"]["current_step"] == "z"


# ==========================================================================
# 4. 环境探测
# ==========================================================================
class TestEnvReport:
    def test_returns_all_fields(self):
        env = G.env_report()
        assert set(env) >= {"session", "desktop", "beep", "hotkey",
                            "notify", "reason", "platform"}

    def test_never_raises(self):
        env = G.env_report()          # 重复调用也不该抛
        assert isinstance(env["desktop"], bool)
        assert isinstance(env["reason"], str)

    def test_has_desktop_matches_env_report(self):
        assert G.has_desktop() == bool(G.env_report().get("desktop"))


# ==========================================================================
# 5. 提示音与通知（不阻塞、不抛）
# ==========================================================================
class TestNotifyAndBeep:
    def test_beep_returns_bool(self):
        assert isinstance(G.beep(times=1, freq=880, dur_ms=10), bool)

    def test_beep_never_raises_on_bad_args(self):
        # 极端参数也不该崩 —— 熔断路径上不能因为出不了声而挂
        assert isinstance(G.beep(times=0, freq=0, dur_ms=0), bool)

    def test_notify_returns_bool(self):
        assert isinstance(G.notify("t", "m", fallback_print=False), bool)

    def test_log_intervention_writes_file(self, tmp_path):
        p = G.log_intervention("测试事件", directory=str(tmp_path))
        assert p and os.path.isfile(p)
        body = open(p, encoding="utf-8").read()
        assert "测试事件" in body

    def test_log_intervention_newline_is_flattened(self, tmp_path):
        """多行消息压成一行 —— 日志要能一行一条地 grep。"""
        p = G.log_intervention("a\nb\nc", directory=str(tmp_path))
        body = open(p, encoding="utf-8").read()
        assert "a\nb" not in body
        assert "a b c" in body

    def test_log_intervention_appends(self, tmp_path):
        G.log_intervention("first", directory=str(tmp_path))
        G.log_intervention("second", directory=str(tmp_path))
        body = open(os.path.join(str(tmp_path), "interventions.log"),
                    encoding="utf-8").read()
        assert "first" in body and "second" in body

    def test_log_intervention_never_raises(self):
        # 不可写路径也不该抛
        assert isinstance(G.log_intervention("x", directory=""), str)


# ==========================================================================
# 6. 热键等待循环（硬约束 A）
# ==========================================================================
class TestWaitForHotkey:
    def test_returns_true_when_pressed(self, fake_user32):
        fake_user32(press_at=1)
        ok, waited = G.wait_for_hotkey(timeout=1.0, poll=0.01, debounce=0.0)
        assert ok is True
        assert waited >= 0.0
        assert fake_user32.last.calls >= 1

    def test_returns_false_on_timeout_without_raising(self, fake_user32):
        fake_user32(press_at=10 ** 9)
        ok, waited = G.wait_for_hotkey(timeout=0.15, poll=0.02, debounce=0.0)
        assert ok is False, "超时要返回 False，抛不抛是调用方的决定"
        assert 0.1 <= waited <= 0.9

    def test_timeout_zero_returns_immediately(self, fake_user32):
        fake_user32(press_at=1)
        t0 = time.perf_counter()
        ok, waited = G.wait_for_hotkey(timeout=0.0, poll=0.01, debounce=0.0)
        assert ok is False
        assert time.perf_counter() - t0 < 0.5

    def test_poll_interval_is_actually_used(self, fake_user32, spy_sleep):
        """硬约束 A 的执行验证：循环里真的 sleep(0.05)。"""
        fake_user32(press_at=3)
        G.wait_for_hotkey(timeout=2.0, poll=0.05, debounce=0.0)
        assert any(abs(s - 0.05) < 1e-6 for s in spy_sleep), spy_sleep

    def test_debounce_is_actually_used(self, fake_user32, spy_sleep):
        """硬约束 A 的执行验证：按下后真的 sleep(0.3)。"""
        fake_user32(press_at=1)
        G.wait_for_hotkey(timeout=2.0, poll=0.05, debounce=0.3)
        assert any(abs(s - 0.3) < 1e-6 for s in spy_sleep), spy_sleep

    def test_debounce_happens_before_returning(self, fake_user32, spy_sleep):
        """顺序：先睡满去抖，再返回 —— 不能反。"""
        fake_user32(press_at=1)
        ok, _ = G.wait_for_hotkey(timeout=2.0, poll=0.05, debounce=0.3)
        assert ok is True
        assert abs(spy_sleep[-1] - 0.3) < 1e-6, "最后一次 sleep 应是去抖"

    def test_does_not_touch_global_time_sleep(self, fake_user32, spy_sleep):
        """打桩的是 guard._sleep，全局 time.sleep 必须是原装的。

        这条守的是"测试不许污染进程"—— 如果哪天有人图省事把
        guard 里的 _sleep 别名去掉、直接写 time.sleep，
        这条会立刻发现"spy 没被调用"（而不是测试莫名其妙变慢）。

        ``spy_sleep`` 是个 list 夹具，打桩时**不真的睡**，
        所以这里可以放心用默认 poll（0.05）走一轮。
        """
        fake_user32(press_at=2)
        real = time.sleep
        G.wait_for_hotkey(timeout=1.0, poll=0.05, debounce=0.0)
        assert time.sleep is real, "全局 time.sleep 被改动了"
        assert spy_sleep, "guard._sleep 没被调用 —— 别名可能被去掉了"

    def test_should_stop_ends_wait_early(self, fake_user32):
        fake_user32(press_at=10 ** 9)
        calls = {"n": 0}

        def _stop():
            calls["n"] += 1
            return calls["n"] > 3
        t0 = time.perf_counter()
        ok, _ = G.wait_for_hotkey(timeout=5.0, poll=0.01, debounce=0.0,
                                  should_stop=_stop)
        assert ok is False
        assert time.perf_counter() - t0 < 2.0, "should_stop 应立即叫停"

    def test_should_stop_true_from_start(self, fake_user32):
        fake_user32(press_at=10 ** 9)
        t0 = time.perf_counter()
        ok, _ = G.wait_for_hotkey(timeout=5.0, poll=0.01, debounce=0.0,
                                  should_stop=lambda: True)
        assert ok is False
        assert time.perf_counter() - t0 < 0.5

    def test_on_tick_called_per_cycle(self, fake_user32):
        fake_user32(press_at=10 ** 9)
        ticks: list = []
        G.wait_for_hotkey(timeout=0.15, poll=0.02, debounce=0.0,
                          on_tick=lambda el: ticks.append(el))
        assert len(ticks) >= 1, "进度回调没被调用"

    def test_on_tick_exception_does_not_break_wait(self, fake_user32):
        fake_user32(press_at=2)
        def _bad(el):
            raise RuntimeError("回调坏了")
        ok, _ = G.wait_for_hotkey(timeout=1.0, poll=0.02, debounce=0.0,
                                  on_tick=_bad)
        assert ok is True, "回调抛异常不该拖垮等待"

    def test_no_user32_gives_timeout(self, monkeypatch):
        """拿不到 user32（沙箱/非 Windows）→ 等满超时返回 False，不崩。"""
        monkeypatch.setattr(G, "_user32", lambda: None)
        ok, waited = G.wait_for_hotkey(timeout=0.1, poll=0.01, debounce=0.0)
        assert ok is False
        assert waited >= 0.05

    def test_keydown_bit_not_just_nonzero(self, monkeypatch):
        """必须判 ``& 0x8000``，不能判 ``!= 0``。

        ``GetAsyncKeyState`` 的低位字节编码"本线程启动后是否被按过"——
        那是历史信息。测试用一个只有低位为 1 的返回值（0x0001，
        表示"曾经按过但**现在没按**"），等待应当**超时**。
        """
        class _Stale:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x0001        # 历史位，不是当前按下
        monkeypatch.setattr(G, "_user32", lambda: _Stale)
        ok, _ = G.wait_for_hotkey(timeout=0.12, poll=0.02, debounce=0.0)
        assert ok is False, "把历史位当成当前按下 = 无缘无故恢复"


# ==========================================================================
# 7. Ctrl+C 干净打断（硬约束 G）
# ==========================================================================
class TestKeyboardInterrupt:
    """中断必须**传播出去**，不许被吞，也不许留后台线程。"""

    def test_propagates_from_polling(self, fake_user32):
        fake_user32(raise_exc=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            G.wait_for_hotkey(timeout=1.0, poll=0.01, debounce=0.0)

    def test_propagates_from_sleep(self, monkeypatch):
        """最真实的中断点：人按 Ctrl+C 时进程多半正在 sleep。"""
        class _Idle:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0
        monkeypatch.setattr(G, "_user32", lambda: _Idle)

        def _kbd_sleep(_s):
            raise KeyboardInterrupt()
        monkeypatch.setattr(G, "_sleep", _kbd_sleep)
        with pytest.raises(KeyboardInterrupt):
            G.wait_for_hotkey(timeout=5.0, poll=0.05, debounce=0.0)

    def test_pause_for_human_propagates(self, with_desktop, monkeypatch):
        def _raise(vk, **k):
            raise KeyboardInterrupt()
        monkeypatch.setattr(G, "wait_for_hotkey", _raise)
        with pytest.raises(KeyboardInterrupt):
            G.pause_for_human("中断", _ctx(), timeout=10.0, capture=False)

    def test_no_background_thread_left(self, with_desktop, monkeypatch):
        """等待期间不新起线程 —— Ctrl+C 之后不该有线程挂在那里。"""
        import threading
        before = threading.active_count()
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 0.1))
        G.pause_for_human("x", _ctx(), timeout=1.0, capture=False)
        assert threading.active_count() == before, \
            "await 用了后台线程 —— 退出时会留下卡死的非守护线程"


# ==========================================================================
# 8. pause_for_human：F9 恢复路径
# ==========================================================================
class TestPauseResume:
    def test_resumed_returns_dict(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 3.5))
        out = G.pause_for_human("原因", _ctx(), timeout=30.0, capture=False)
        assert out["resumed"] is True
        assert out["waited"] == 3.5
        assert out["degraded"] is False

    def test_context_passed_through(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        ctx = _ctx(current_step="关键步骤")
        out = G.pause_for_human("x", ctx, timeout=10.0, capture=False)
        assert out["context"] is ctx

    def test_auto_builds_context_from_status(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        st = RiskStatus.hit("slider", severity=Severity.INTERRUPT,
                            fingerprint_id="txt.zh.drag_slider")
        out = G.pause_for_human("", None, timeout=10.0, capture=False,
                                hwnd=0, url="https://a.com", current_step="s9")
        ctx = out["context"]
        assert isinstance(ctx, C.InterventionContext)
        assert ctx.current_step == "s9"

    def test_writes_intervention_log(self, with_desktop, monkeypatch):
        logs: list = []
        monkeypatch.setattr(G, "log_intervention",
                            lambda text, **k: (logs.append(text), "")[1])
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        G.pause_for_human("原因A", _ctx(), timeout=10.0, capture=False)
        assert len(logs) == 1
        assert "原因A" in logs[0]

    def test_env_attached_to_context(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        ctx = _ctx()
        G.pause_for_human("x", ctx, timeout=10.0, capture=False)
        assert ctx.extra.get("env", {}).get("desktop") is True
        assert ctx.extra.get("env", {}).get("degraded") is False

    def test_on_wait_callback_gets_progress(self, with_desktop, monkeypatch):
        def _fake_wait(vk, **k):
            tick = k.get("on_tick")
            if tick:
                tick(0.0)
                tick(1.0)
            return (True, 2.0)
        monkeypatch.setattr(G, "wait_for_hotkey", _fake_wait)
        seen: list = []
        G.pause_for_human("x", _ctx(), timeout=10.0, capture=False,
                          on_wait=lambda el, lim: seen.append((el, lim)))
        assert seen, "on_wait 没被调用"
        assert seen[0][1] == 10.0, "第二个参数应是上限"


# ==========================================================================
# 9. pause_for_human：超时路径
# ==========================================================================
class TestPauseTimeout:
    def test_raises_timeout(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (False, 30.0))
        with pytest.raises(G.HumanInterventionTimeout) as ei:
            G.pause_for_human("超时了", _ctx(), timeout=30.0, capture=False)
        exc = ei.value
        assert exc.context is not None
        assert exc.waited == 30.0
        assert exc.limit == 30.0
        assert exc.degraded is False

    def test_timeout_is_also_required(self, with_desktop, monkeypatch):
        """一个 ``except HumanInterventionRequired`` 能兜住超时。"""
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (False, 5.0))
        with pytest.raises(G.HumanInterventionRequired):
            G.pause_for_human("x", _ctx(), timeout=5.0, capture=False)

    def test_stopped_by_caller_marks_flag(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (False, 1.0))
        with pytest.raises(G.HumanInterventionTimeout) as ei:
            G.pause_for_human("x", _ctx(), timeout=10.0, capture=False,
                              should_stop=lambda: True)
        assert ei.value.detail.get("stopped_by_caller") is True


# ==========================================================================
# 10. 环境退化路径（硬约束 B）
# ==========================================================================
class TestDegradedPath:
    def test_no_desktop_raises_timeout(self, no_desktop):
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("退化", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.01)

    def test_degraded_flag_is_true(self, no_desktop):
        with pytest.raises(G.HumanInterventionTimeout) as ei:
            G.pause_for_human("退化", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.01)
        assert ei.value.degraded is True
        assert ei.value.is_degraded is True

    def test_does_not_poll_hotkey_when_degraded(self, no_desktop, monkeypatch):
        """无桌面时**不轮询热键** —— 那个轮询是假的，做了只会
        让人以为"系统在等我按 F9"。"""
        calls = {"n": 0}

        def _must_not_call(vk, **k):
            calls["n"] += 1
            return (True, 0.0)
        monkeypatch.setattr(G, "wait_for_hotkey", _must_not_call)
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("x", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.01)
        assert calls["n"] == 0

    def test_does_not_wait_full_timeout(self, no_desktop):
        """等的是固定的 degraded_wait，不是 300s。"""
        t0 = time.perf_counter()
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("x", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.05)
        assert time.perf_counter() - t0 < 5.0

    def test_writes_degraded_log(self, no_desktop, monkeypatch):
        logs: list = []
        monkeypatch.setattr(G, "log_intervention",
                            lambda text, **k: (logs.append(text), "")[1])
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("x", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.01)
        assert logs and "DEGRADED" in logs[0]

    def test_message_explains_why(self, no_desktop):
        with pytest.raises(G.HumanInterventionTimeout) as ei:
            G.pause_for_human("x", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=0.01)
        assert "无桌面" in str(ei.value)

    def test_env_flag_recorded_in_context(self, no_desktop):
        ctx = _ctx()
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("x", ctx, timeout=300.0, capture=False,
                              degraded_wait=0.01)
        assert ctx.extra.get("env", {}).get("degraded") is True

    def test_should_stop_can_end_degraded_wait(self, no_desktop):
        t0 = time.perf_counter()
        with pytest.raises(G.HumanInterventionTimeout):
            G.pause_for_human("x", _ctx(), timeout=300.0, capture=False,
                              degraded_wait=10.0,
                              should_stop=lambda: True)
        assert time.perf_counter() - t0 < 2.0


# ==========================================================================
# 11. guard() 三态分派
# ==========================================================================
class TestGuardDispatch:
    def test_continue_is_noop(self):
        out = G.guard(RiskStatus.clean(), capture=False)
        assert out["action"] == "continue"
        assert out["resumed"] is True

    def test_abort_raises_risk_blocked(self):
        from avatar.errors import RiskBlocked
        st = RiskStatus.hit("rate_limit", severity=Severity.BLOCK)
        with pytest.raises(RiskBlocked):
            G.guard(st, capture=False)

    def test_abort_does_not_go_through_pause(self, monkeypatch):
        """abort 不能走 pause_for_human —— 那只会白等 5 分钟。"""
        called = {"n": 0}
        monkeypatch.setattr(G, "pause_for_human",
                            lambda *a, **k: called.__setitem__("n", 1))
        st = RiskStatus.hit("x", severity=Severity.BLOCK)
        from avatar.errors import RiskBlocked
        with pytest.raises(RiskBlocked):
            G.guard(st, capture=False)
        assert called["n"] == 0

    def test_pause_calls_pause_for_human(self, with_desktop, monkeypatch):
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        st = RiskStatus.hit("slider", severity=Severity.INTERRUPT)
        out = G.guard(st, capture=False, timeout=10.0)
        assert out["action"] == "pause"
        assert out["resumed"] is True

    def test_unknown_triggers_pause(self, with_desktop, monkeypatch):
        """硬约束 F 在 guard() 这一层的落点。"""
        monkeypatch.setattr(G, "wait_for_hotkey", lambda vk, **k: (True, 1.0))
        st = RiskStatus.unknown_(reason="ocr_unavailable")
        out = G.guard(st, capture=False, timeout=10.0)
        assert out["action"] == "pause", "unknown 必须熔断"

    def test_raise_on_pause_does_not_block(self, monkeypatch):
        monkeypatch.setattr(G, "pause_for_human",
                            lambda *a, **k: pytest.fail("不该走到阻塞等待"))
        st = RiskStatus.unknown_(reason="x")
        with pytest.raises(G.HumanInterventionRequired):
            G.guard(st, capture=False, raise_on_pause=True)

    def test_guard_result_names_its_consumer(self):
        out = G.guard(RiskStatus.clean(), capture=False)
        assert out["consumed_by"], "要标明是谁消费的（可审计）"


# ==========================================================================
# 12. guarded() 上下文管理器
# ==========================================================================
class TestGuarded:
    def test_clean_passes_through(self):
        with G.guarded(RiskStatus.clean(), capture=False) as g:
            assert g.result["action"] == "continue"

    def test_abort_raises_inside_with(self):
        from avatar.errors import RiskBlocked
        st = RiskStatus.hit("x", severity=Severity.BLOCK)
        with pytest.raises(RiskBlocked):
            with G.guarded(st, capture=False):
                pass

    def test_does_not_swallow_user_exception(self):
        """上下文管理器不许吞掉业务代码自己的异常。"""
        with pytest.raises(ValueError):
            with G.guarded(RiskStatus.clean(), capture=False):
                raise ValueError("业务异常")


# ==========================================================================
# 13. 截图与上下文（与 context 模块的接缝）
# ==========================================================================
class TestInterventionArtifacts:
    def test_dir_created_with_gitignore(self, tmp_path):
        d = str(tmp_path / "interventions")
        assert C.ensure_dir(d)
        assert os.path.isdir(d)
        gi = os.path.join(d, ".gitignore")
        assert os.path.isfile(gi), "硬约束 C：必须写 .gitignore"
        body = open(gi, encoding="utf-8").read()
        assert "!.gitignore" in body

    def test_screenshot_path_shape(self, tmp_path):
        p = C.screenshot_path_for(4321, directory=str(tmp_path))
        assert os.path.dirname(p) == os.path.abspath(str(tmp_path))
        assert "_4321." in os.path.basename(p)

    def test_save_bitmap_writes_valid_bmp(self, tmp_path):
        """存出来的必须是**合法** BMP —— 人要看，看图软件要能开。"""
        class _Bmp:
            def __init__(self, w, h):
                self.width, self.height = w, h
                self.origin = (0, 0)
                self.stride = w * 4
                self.pixels = bytearray(b"\x10\x20\x30\xff" * (w * h))
        p = str(tmp_path / "x.bmp")
        assert C.save_bitmap(_Bmp(4, 3), p) is True
        raw = open(p, "rb").read()
        assert raw[:2] == b"BM"
        assert struct.unpack("<I", raw[2:6])[0] == len(raw)
        assert struct.unpack("<I", raw[10:14])[0] == 54
        assert struct.unpack("<H", raw[28:30])[0] == 32
        assert struct.unpack("<i", raw[22:26])[0] == -3, "负高度=自上而下"

    def test_build_context_without_capture(self):
        ctx = C.build_context(RiskStatus.hit("slider"), hwnd=0,
                              current_step="s", capture=False)
        assert ctx.screenshot_path == ""

    def test_build_context_records_capture_failure_as_warning(self):
        """截图失败不能阻断熔断 —— 但要留下警告。"""
        ctx = C.build_context(RiskStatus.hit("slider"), hwnd=0,
                              current_step="s", capture=True,
                              extra={"warnings": []})
        # hwnd=0 且无 region → 抓不到，应记录警告
        assert ctx.extra.get("warnings"), "抓不到图要留警告"


# ==========================================================================
# 14. 真桌面层（没有就 skip —— skip 不是失败）
# ==========================================================================
class TestRealDesktop:
    """真环境验证。CI / 无头机器上 skip。"""

    def test_env_report_is_usable(self):
        env = G.env_report()
        if not env.get("platform") == "win32":
            pytest.skip("非 Windows，无 Win32 桌面概念")
        if env.get("session") == "unknown":
            pytest.skip("拿不到 user32（可能是沙箱限制）")
        assert env["session"] in ("console", "remote")

    def test_desktop_detection_is_consistent(self):
        """同一次进程内重复探测应给出一致结论。"""
        a = G.env_report().get("desktop")
        b = G.env_report().get("desktop")
        assert a == b, "桌面探测不稳定：%r vs %r" % (a, b)

    def test_can_produce_real_context(self, tmp_path):
        """真抓一次屏（有桌面时）—— 验证 capture 链路通。"""
        if not G.has_desktop():
            pytest.skip("无交互桌面，跳过真实截图")
        errors: list = []
        shot = C.save_screenshot(0, region=(0, 0, 32, 32),
                                 directory=str(tmp_path), errors=errors)
        # 抓到就验证文件；抓不到也算"环境限制"，只要不抛就行
        if shot:
            assert os.path.isfile(shot)
            assert os.path.getsize(shot) > 54
        else:
            assert errors, "失败必须给原因"
