# -*- coding: utf-8 -*-
"""动作重试：``@with_retry`` 装饰器。

## 为什么"重试 + 验证"必须绑在一起

单独的 retry 是**危险的**：如果动作其实成功了、只是返回值不可靠，
盲重试会让动作执行两次 —— 提交两次订单、发两条消息、
投两票。这是真实事故，不是假想。

所以本模块的 retry **默认带验证**：每次尝试后调 ``verify``，
**验证确认成功才停**。这样：

- 第一次就成功的，不会被重试（不像"按返回值重试"那样可能重复执行）
- 第一次执行了但结果延迟到达的，验证会等到它出现
- 真正失败的才重试

## 退避策略

默认**指数退避 + 抖动**：

::

    第 1 次失败 → 等 base * 1 = 0.5s
    第 2 次失败 → 等 base * 1.5 ≈ 0.75s
    第 3 次失败 → 等 base * 2.25 ≈ 1.1s

抖动（±25%）是必须的：多个并发任务如果退避时间一致，
会在同一时刻一起重试，形成冲击波。这个是工程常识但很容易忘。

参数 ``backoff=1.5`` 是**倍数**而不是固定增量，
``backoff=1.0`` 就是固定间隔。

## 什么算"该重试"

- 抛异常 → 重试（除非在 ``no_retry_on`` 里）
- ``ok=False``（动作自己说失败） → 重试
- 验证 ``unknown`` → 重试（**关键**：这是"不知道"，可能只是还没生效）
- 验证 ``fail`` → 默认**不重试**（确认失败了，重试也是白搭），
  除非 ``retry_on_verify_fail=True`
- 验证 ``ok`` → 成功，立即返回

## 返回值

:class:`RetryOutcome` 而不是裸结果 —— 它带**尝试历史**。
出问题时能一眼看到"试了三次，每次验证都是 unknown，
因为截图一直失败"，这比"失败了"有用一万倍。
"""

from __future__ import annotations

import functools
import random
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..errors import AvatarError
from .result import VerifyResult, result_of

__all__ = [
    "RetryError",
    "RetryPolicy",
    "RetryOutcome",
    "with_retry",
    "retry_call",
    "selfcheck",
]


class RetryError(AvatarError):
    """重试参数非法。"""

    exit_code = 26


class RetryPolicy(object):
    """重试策略参数。

    ``max_retries`` 是**重试次数**（不含首次尝试）。
    所以总尝试次数 = ``max_retries + 1``。
    """

    __slots__ = ("max_retries", "base_delay", "backoff", "max_delay",
                 "jitter", "retry_on_exception", "retry_on_fail",
                 "retry_on_unknown", "no_retry_on", "deadline")

    def __init__(
        self,
        max_retries: int = 3,
        *,
        base_delay: float = 0.5,
        backoff: float = 1.5,
        max_delay: float = 8.0,
        jitter: float = 0.25,
        retry_on_exception: bool = True,
        retry_on_fail: bool = False,
        retry_on_unknown: bool = True,
        no_retry_on: Optional[Sequence[type]] = None,
        deadline: Optional[float] = None,
    ) -> None:
        if max_retries < 0:
            raise RetryError("max_retries 不能为负")
        if base_delay < 0:
            raise RetryError("base_delay 不能为负")
        if backoff < 0:
            raise RetryError("backoff 不能为负")
        if not (0.0 <= jitter <= 1.0):
            raise RetryError("jitter 必须在 0~1 之间")
        self.max_retries = int(max_retries)
        self.base_delay = float(base_delay)
        self.backoff = float(backoff)
        self.max_delay = float(max_delay)
        self.jitter = float(jitter)
        self.retry_on_exception = bool(retry_on_exception)
        self.retry_on_fail = bool(retry_on_fail)
        self.retry_on_unknown = bool(retry_on_unknown)
        self.no_retry_on = tuple(no_retry_on or ())
        self.deadline = deadline

    @property
    def total_attempts(self) -> int:
        """最多尝试几次（含首次）。"""
        return self.max_retries + 1

    def delay_for(self, attempt: int,
                  *, rand: Optional[Callable[[], float]] = None) -> float:
        """第 ``attempt`` 次**重试前**该等多久（``attempt`` 从 1 开始）。

        含抖动。``rand`` 可注入以便测试确定性。
        """
        if attempt < 1:
            return 0.0
        raw = self.base_delay * (self.backoff ** (attempt - 1))
        raw = min(raw, self.max_delay)
        if self.jitter > 0:
            r = (rand or random.random)()
            # 抖动范围 [1-j, 1+j]
            factor = 1.0 + (r * 2.0 - 1.0) * self.jitter
            raw = raw * max(0.0, factor)
        return max(0.0, raw)

    def should_retry(self, *, attempt: int, exc: Optional[BaseException],
                     verdict: Optional[str], elapsed: float = 0.0) -> bool:
        """判断是否还该继续重试。``attempt`` 是**已完成的**尝试次数。"""
        if attempt > self.max_retries:
            return False
        if self.deadline is not None and elapsed >= self.deadline:
            return False

        if exc is not None:
            if not self.retry_on_exception:
                return False
            for t in self.no_retry_on:
                if isinstance(exc, t):
                    return False
            return True

        if verdict == "ok":
            return False
        if verdict == "fail":
            return self.retry_on_fail
        if verdict == "unknown":
            return self.retry_on_unknown
        return False

    def describe(self) -> Dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "total_attempts": self.total_attempts,
            "base_delay": self.base_delay,
            "backoff": self.backoff,
            "max_delay": self.max_delay,
            "jitter": self.jitter,
            "retry_on_exception": self.retry_on_exception,
            "retry_on_fail": self.retry_on_fail,
            "retry_on_unknown": self.retry_on_unknown,
            "no_retry_on": [t.__name__ for t in self.no_retry_on],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return "<RetryPolicy x%d base=%.2f backoff=%.2f>" % (
            self.max_retries, self.base_delay, self.backoff)


#: 默认策略单例（不可变用；实际都是值拷贝语义）。
DEFAULT_POLICY = RetryPolicy()

#: 明确**不该重试**的异常 —— 重试也还是这个结果。
_NEVER_RETRY = (KeyboardInterrupt, SystemExit, GeneratorExit)


class RetryOutcome(object):
    """重试的完整结果 + 尝试历史。"""

    __slots__ = ("ok", "value", "verify", "attempts", "elapsed",
                 "attempt_log", "policy", "gave_up_reason")

    def __init__(
        self,
        ok: bool,
        *,
        value: Any = None,
        verify: Optional[VerifyResult] = None,
        attempts: int = 0,
        elapsed: float = 0.0,
        attempt_log: Optional[List[Dict[str, Any]]] = None,
        policy: Optional[RetryPolicy] = None,
        gave_up_reason: str = "",
    ) -> None:
        self.ok = bool(ok)
        self.value = value
        self.verify = verify
        self.attempts = int(attempts)
        self.elapsed = float(elapsed)
        self.attempt_log = list(attempt_log or [])
        self.policy = policy
        self.gave_up_reason = gave_up_reason

    @property
    def verified(self) -> Optional[bool]:
        """验证结论：True/False/None（没验证或验证未知）。"""
        if self.verify is None:
            return None
        if self.verify.ok:
            return True
        if self.verify.failed:
            return False
        return None

    @property
    def verdict(self) -> str:
        if self.verify is not None:
            return self.verify.verdict
        return "ok" if self.ok else "fail"

    @property
    def unknown(self) -> bool:
        return self.verify is not None and self.verify.unknown

    @property
    def retried(self) -> bool:
        return self.attempts > 1

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "attempts": self.attempts,
            "retried": self.retried,
            "elapsed": round(self.elapsed, 4),
            "gave_up_reason": self.gave_up_reason,
            "verify": self.verify.to_dict() if self.verify else None,
            "attempt_log": self.attempt_log,
        }

    def summary(self) -> str:
        """一行人类可读说明。"""
        tag = "✓" if self.ok else ("?" if self.unknown else "✗")
        parts = ["%s 尝试 %d 次" % (tag, self.attempts)]
        if self.retried:
            parts.append("（重试 %d 次）" % (self.attempts - 1))
        if self.verify is not None:
            parts.append("验证=%s" % self.verify.verdict)
            if self.verify.reason:
                parts.append(self.verify.reason)
        if self.gave_up_reason:
            parts.append("放弃原因：%s" % self.gave_up_reason)
        return " ".join(parts)

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        return "<RetryOutcome ok=%s attempts=%d>" % (self.ok, self.attempts)


def _normalize_verify(verify: Any) -> Optional[Callable[[Any], Any]]:
    """把各种 verify 参数形态归一成可调用对象。

    - ``None`` → 不验证
    - 可调用 → 直接用
    - :class:`VerifyRequest` → 包成 ``lambda r: verify_action(request=req)``
    - dict → 包成 ``verify_action(**dict)``
    """
    if verify is None:
        return None
    if callable(verify):
        return verify

    from .action import VerifyRequest, verify_action

    if isinstance(verify, VerifyRequest):
        return lambda _r, _req=verify: verify_action(request=_req)
    if isinstance(verify, dict):
        kw = dict(verify)
        return lambda _r, _kw=kw: verify_action(**_kw)
    raise RetryError("verify 参数无法识别：%r" % (verify,))


def retry_call(
    func: Callable[..., Any],
    *args: Any,
    verify: Any = None,
    policy: Optional[RetryPolicy] = None,
    max_retries: Optional[int] = None,
    backoff: Optional[float] = None,
    base_delay: Optional[float] = None,
    on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    **kwargs: Any,
) -> RetryOutcome:
    """执行 ``func``，按策略重试，可选验证。

    返回值是 :class:`RetryOutcome`（**不是** func 的原始返回值）——
    调用方需要原始值就取 ``.value``。

    ``sleep`` 可注入（测试用），默认 ``time.sleep``。
    ``on_attempt`` 每次尝试后回调，便于把尝试写进日志/证据链。
    """
    pol = policy or RetryPolicy(
        max_retries if max_retries is not None else DEFAULT_POLICY.max_retries,
        backoff=backoff if backoff is not None else DEFAULT_POLICY.backoff,
        base_delay=(base_delay if base_delay is not None
                    else DEFAULT_POLICY.base_delay))

    vfn = _normalize_verify(verify)
    sleeper = sleep or time.sleep

    t0 = time.time()
    log = []  # type: List[Dict[str, Any]]
    attempts = 0
    last_value = None
    last_verify = None

    while True:
        attempts += 1
        entry = {"attempt": attempts, "started": time.time(),
                 "error": None, "verdict": None}

        exc = None
        value = None
        try:
            value = func(*args, **kwargs)
            entry["ok"] = True
        except _NEVER_RETRY:
            raise
        except BaseException as e:      # noqa: BLE001
            exc = e
            entry["ok"] = False
            entry["error"] = "%s: %s" % (type(e).__name__, e)

        last_value = value

        vres = None  # type: Optional[VerifyResult]
        if exc is None and vfn is not None:
            try:
                raw = vfn(value)
                vres = result_of(raw)
            except Exception as ve:      # noqa: BLE001
                vres = VerifyResult.unknown_(
                    method="retry", reason="验证函数抛异常：%s" % (ve,),
                    error=str(ve))
        if vres is not None:
            last_verify = vres
            entry["verify"] = vres.to_dict()
            entry["verdict"] = vres.verdict

        # 判定
        if exc is None and (vfn is None or (vres is not None and vres.ok)):
            entry["elapsed"] = time.time() - entry["started"]
            log.append(entry)
            if on_attempt:
                try:
                    on_attempt(entry)
                except Exception:
                    pass
            out = RetryOutcome(
                True, value=value, verify=vres, attempts=attempts,
                elapsed=time.time() - t0, attempt_log=log, policy=pol)
            return out

        entry["elapsed"] = time.time() - entry["started"]
        log.append(entry)
        if on_attempt:
            try:
                on_attempt(entry)
            except Exception:
                pass

        verdict = entry.get("verdict")
        if exc is not None:
            verdict = None
        elapsed = time.time() - t0

        if not pol.should_retry(attempt=attempts, exc=exc, verdict=verdict,
                                elapsed=elapsed):
            reason = _give_up_reason(pol, attempts, exc, verdict, elapsed)
            return RetryOutcome(
                False, value=last_value, verify=last_verify,
                attempts=attempts, elapsed=elapsed, attempt_log=log,
                policy=pol, gave_up_reason=reason)

        delay = pol.delay_for(attempts)
        if pol.deadline is not None:
            delay = min(delay, max(0.0, pol.deadline - elapsed))
        if delay > 0:
            sleeper(delay)


def _give_up_reason(pol: RetryPolicy, attempts: int,
                    exc: Optional[BaseException], verdict: Optional[str],
                    elapsed: float) -> str:
    if pol.deadline is not None and elapsed >= pol.deadline:
        return "超过总时限 %.1fs" % pol.deadline
    if attempts > pol.max_retries:
        if exc is not None:
            return "已达最大重试次数 %d（最后一次异常：%s）" % (
                pol.max_retries, type(exc).__name__)
        return "已达最大重试次数 %d" % pol.max_retries
    if exc is not None:
        if not pol.retry_on_exception:
            return "策略不允许对异常重试"
        for t in pol.no_retry_on:
            if isinstance(exc, t):
                return "异常 %s 在 no_retry_on 名单里" % t.__name__
        return "策略不予重试"
    if verdict == "fail":
        return "验证确认失败，且 retry_on_fail=False"
    if verdict == "unknown":
        return "验证未知，且 retry_on_unknown=False"
    return "策略不予重试"


def with_retry(
    _func: Optional[Callable[..., Any]] = None,
    *,
    max_retries: int = 3,
    verify: Any = None,
    policy: Optional[RetryPolicy] = None,
    backoff: float = 1.5,
    base_delay: float = 0.5,
    retry_on_fail: bool = False,
    retry_on_unknown: bool = True,
    no_retry_on: Optional[Sequence[type]] = None,
    on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    return_outcome: bool = True,
):
    """装饰器：给动作函数加上"重试 + 验证"。

    **``verify`` 默认是 ``None``（不验证）** —— 保持向后兼容。
    但强烈建议给上：没有验证的重试会重复执行可能已成功的动作。

    用法::

        @with_retry(max_retries=3, verify=VerifyRequest(
            "web", selector="#ok", expect_text="成功",
            has_expect_text=True))
        def submit(sess):
            return interact.click(sess, "#submit")

        out = submit(sess)
        if out.ok:      ...   # 确认成功
        elif out.unknown: ...  # 未知，需要人工/降级
        else:           ...   # 确认失败

    ``return_outcome=False`` 时返回原始值（成功）或抛最后一次异常，
    用于需要兼容旧调用约定的地方。**不推荐**，因为会丢掉尝试历史。

    可以不带括号直接用（``@with_retry``），此时用全部默认参数。
    """
    def _make(fn: Callable[..., Any]) -> Callable[..., Any]:
        pol = policy or RetryPolicy(
            max_retries, base_delay=base_delay, backoff=backoff,
            retry_on_fail=retry_on_fail, retry_on_unknown=retry_on_unknown,
            no_retry_on=no_retry_on)
        vfn = _normalize_verify(verify)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            out = retry_call(
                fn, *args, verify=vfn, policy=pol,
                on_attempt=on_attempt, sleep=sleep, **kwargs)
            if return_outcome:
                return out
            # 兼容模式
            if out.ok:
                return out.value
            if out.attempt_log:
                err = out.attempt_log[-1].get("error")
                if err:
                    raise AvatarError("重试 %d 次后仍失败：%s"
                                      % (out.attempts, err))
            raise AvatarError("重试 %d 次后仍未确认成功：%s"
                              % (out.attempts, out.gave_up_reason or ""))
        wrapper.policy = pol            # type: ignore[attr-defined]
        wrapper.verify_fn = vfn         # type: ignore[attr-defined]
        return wrapper

    if _func is not None and callable(_func):
        return _make(_func)
    return _make


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 策略参数校验 ---
    p = RetryPolicy(3)
    add("总数 = max_retries + 1", p.total_attempts == 4, p.total_attempts)
    for bad, label in (((-1,), "负数"),
                       ({"base_delay": -1}, "base_delay 负"),
                       ({"backoff": -1}, "backoff 负"),
                       ({"jitter": 2}, "jitter>1"),
                       ({"jitter": -1}, "jitter<0")):
        try:
            if isinstance(bad, tuple):
                RetryPolicy(*bad)
            else:
                RetryPolicy(**bad)
            add("非法参数抛错(%s)" % label, False, "没抛")
        except RetryError:
            add("非法参数抛错(%s)" % label, True)

    # --- 退避计算 ---
    p = RetryPolicy(3, base_delay=1.0, backoff=2.0, jitter=0)
    add("第 1 次退避 = base", p.delay_for(1) == 1.0, p.delay_for(1))
    add("第 2 次退避 = base*2", p.delay_for(2) == 2.0, p.delay_for(2))
    add("第 3 次退避 = base*4", p.delay_for(3) == 4.0, p.delay_for(3))
    add("第 0 次退避 = 0", p.delay_for(0) == 0.0)
    add("负数 attempt = 0", p.delay_for(-5) == 0.0)

    p = RetryPolicy(5, base_delay=1.0, backoff=2.0, max_delay=3.0, jitter=0)
    add("超过 max_delay 被夹", p.delay_for(9) == 3.0, p.delay_for(9))

    p = RetryPolicy(3, base_delay=1.0, backoff=1.0, jitter=0)
    add("backoff=1 固定间隔", p.delay_for(1) == p.delay_for(3) == 1.0,
        (p.delay_for(1), p.delay_for(3)))

    # 抖动
    p = RetryPolicy(3, base_delay=1.0, backoff=1.0, jitter=0.25)
    lo = p.delay_for(1, rand=lambda: 0.0)
    hi = p.delay_for(1, rand=lambda: 1.0)
    add("抖动下界 = 1-j", abs(lo - 0.75) < 1e-9, lo)
    add("抖动上界 = 1+j", abs(hi - 1.25) < 1e-9, hi)
    mid = p.delay_for(1, rand=lambda: 0.5)
    add("抖动机中 = base", abs(mid - 1.0) < 1e-9, mid)

    # --- should_retry ---
    p = RetryPolicy(2)
    add("超次数不重试", p.should_retry(attempt=3, exc=None, verdict="fail")
        is False)
    add("首次异常可重试",
        p.should_retry(attempt=1, exc=RuntimeError("x"), verdict=None)
        is True)
    add("verdict=ok 不重试",
        p.should_retry(attempt=1, exc=None, verdict="ok") is False)
    add("verdict=fail 默认不重试",
        p.should_retry(attempt=1, exc=None, verdict="fail") is False)
    add("verdict=unknown 默认重试",
        p.should_retry(attempt=1, exc=None, verdict="unknown") is True)

    p2 = RetryPolicy(2, retry_on_fail=True)
    add("retry_on_fail=True 时重试",
        p2.should_retry(attempt=1, exc=None, verdict="fail") is True)

    p3 = RetryPolicy(2, retry_on_exception=False)
    add("retry_on_exception=False 不重试",
        p3.should_retry(attempt=1, exc=RuntimeError(), verdict=None) is False)

    p4 = RetryPolicy(2, no_retry_on=(ValueError,))
    add("no_retry_on 命中不重试",
        p4.should_retry(attempt=1, exc=ValueError(), verdict=None) is False)
    add("no_retry_on 未命中仍重试",
        p4.should_retry(attempt=1, exc=TypeError(), verdict=None) is True)

    p5 = RetryPolicy(5, deadline=1.0)
    add("超 deadline 不重试",
        p5.should_retry(attempt=1, exc=RuntimeError(), verdict=None,
                        elapsed=1.5) is False)
    add("未超 deadline 重试",
        p5.should_retry(attempt=1, exc=RuntimeError(), verdict=None,
                        elapsed=0.3) is True)

    p6 = RetryPolicy(2, retry_on_unknown=False)
    add("retry_on_unknown=False 不重试",
        p6.should_retry(attempt=1, exc=None, verdict="unknown") is False)

    add("describe 字段齐",
        set(p.describe()) >= {"max_retries", "total_attempts", "base_delay",
                              "backoff", "max_delay", "jitter",
                              "retry_on_exception", "retry_on_fail",
                              "retry_on_unknown", "no_retry_on"},
        sorted(p.describe()))
    add("describe no_retry_on 转名字",
        RetryPolicy(1, no_retry_on=(ValueError,)).describe()["no_retry_on"] ==
        ["ValueError"])

    # --- retry_call：成功不重试 ---
    calls = []

    def ok_fn():
        calls.append(1)
        return "v"

    out = retry_call(ok_fn, policy=RetryPolicy(3), sleep=lambda s: None,
                     verify=lambda v: VerifyResult.ok_())
    add("成功即停", out.ok is True and out.attempts == 1, out.summary())
    add("成功只调一次", len(calls) == 1, len(calls))
    add("成功 value 正确", out.value == "v", out.value)
    add("成功 verdict ok", out.verdict == "ok", out.verdict)
    add("成功 retried False", out.retried is False)
    add("成功 verified True", out.verified is True, out.verified)

    # --- 异常重试 ---
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 3:
            raise RuntimeError("boom %d" % n["i"])
        return "finally"

    out = retry_call(flaky, policy=RetryPolicy(3, base_delay=0),
                     sleep=lambda s: None)
    add("异常重试到成功", out.ok is True and out.attempts == 3, out.summary())
    add("异常重试记录日志", len(out.attempt_log) == 3, len(out.attempt_log))
    add("日志记录异常信息",
        out.attempt_log[0]["error"].startswith("RuntimeError"),
        out.attempt_log[0]["error"])
    add("最后成功 value", out.value == "finally")
    add("out.retried True", out.retried is True)

    # --- 一直失败 ---
    n2 = {"i": 0}

    def always_fail():
        n2["i"] += 1
        raise ValueError("nope")

    out = retry_call(always_fail, policy=RetryPolicy(2, base_delay=0),
                     sleep=lambda s: None)
    add("全失败 ok False", out.ok is False)
    add("全失败尝试 max+1 次", out.attempts == 3, out.attempts)
    add("全失败 gave_up_reason 提到次数",
        "最大重试次数" in out.gave_up_reason, out.gave_up_reason)
    add("全失败 verdict fail", out.verdict == "fail", out.verdict)

    # --- no_retry_on 生效 ---
    out = retry_call(always_fail,
                     policy=RetryPolicy(5, base_delay=0,
                                        no_retry_on=(ValueError,)),
                     sleep=lambda s: None)
    add("no_retry_on 立即放弃", out.attempts == 1, out.attempts)
    add("no_retry_on reason 说明", "no_retry_on" in out.gave_up_reason,
        out.gave_up_reason)

    # --- 验证 unknown 会重试 ---
    n3 = {"i": 0}

    def unknown_then_ok():
        n3["i"] += 1
        return n3["i"]

    def v_unknown_then_ok(v):
        if v < 3:
            return VerifyResult.unknown_(reason="还没生效")
        return VerifyResult.ok_(reason="好了")

    out = retry_call(unknown_then_ok, policy=RetryPolicy(4, base_delay=0),
                     verify=v_unknown_then_ok, sleep=lambda s: None)
    add("验证 unknown 会重试", out.ok is True and out.attempts == 3,
        out.summary())
    add("unknown 尝试日志带 verify",
        out.attempt_log[0].get("verdict") == "unknown",
        out.attempt_log[0].get("verdict"))

    # --- 验证 fail 默认不重试 ---
    n4 = {"i": 0}

    def never_ok():
        n4["i"] += 1
        return n4["i"]

    out = retry_call(never_ok, policy=RetryPolicy(5, base_delay=0),
                     verify=lambda v: VerifyResult.fail(reason="没变"),
                     sleep=lambda s: None)
    add("验证 fail 默认不重试", out.attempts == 1, out.attempts)
    add("验证 fail 原因说明",
        "retry_on_fail=False" in out.gave_up_reason, out.gave_up_reason)
    add("验证 fail verified False", out.verified is False, out.verified)

    out = retry_call(never_ok,
                     policy=RetryPolicy(2, base_delay=0, retry_on_fail=True),
                     verify=lambda v: VerifyResult.fail(reason="没变"),
                     sleep=lambda s: None)
    add("retry_on_fail=True 会重试", out.attempts == 3, out.attempts)

    # --- 验证函数抛异常 → 当 unknown ---
    def bad_verify(v):
        raise RuntimeError("verify boom")

    out = retry_call(ok_fn, policy=RetryPolicy(1, base_delay=0),
                     verify=bad_verify, sleep=lambda s: None)
    add("验证抛异常 → unknown",
        out.unknown is True, out.summary())
    add("验证异常记录在 verify.reason",
        out.verify is not None and "verify boom" in out.verify.reason,
        out.verify.reason if out.verify is not None else None)

    # --- 不验证 ---
    out = retry_call(ok_fn, policy=RetryPolicy(1), sleep=lambda s: None)
    add("不验证时 ok 为 True", out.ok is True, out.summary())
    add("不验证时 verified 为 None", out.verified is None, out.verified)

    # --- 验证返回裸 bool ---
    out = retry_call(ok_fn, policy=RetryPolicy(1), verify=lambda v: True,
                     sleep=lambda s: None)
    add("verify 返回 True 可识别", out.ok is True and out.verified is True,
        out.summary())
    out = retry_call(ok_fn, policy=RetryPolicy(1), verify=lambda v: False,
                     sleep=lambda s: None)
    add("verify 返回 False 可识别",
        out.verified is False, out.summary())

    # --- verify 传 dict / VerifyRequest ---
    out = retry_call(ok_fn, policy=RetryPolicy(1), sleep=lambda s: None,
                     verify={"hwnd": 0})
    add("verify 传 dict 可用（无 hwnd → unknown）",
        out.unknown is True, out.summary())

    from .action import VerifyRequest
    out = retry_call(ok_fn, policy=RetryPolicy(1), sleep=lambda s: None,
                     verify=VerifyRequest("desktop"))
    add("verify 传 VerifyRequest 可用", out.unknown is True, out.summary())

    try:
        retry_call(ok_fn, verify=12345, policy=RetryPolicy(1),
                   sleep=lambda s: None)
        add("非法 verify 抛 RetryError", False, "没抛")
    except RetryError:
        add("非法 verify 抛 RetryError", True)

    # --- on_attempt 回调 ---
    # **注意**：这里必须新建一个独立的 flaky 实例。
    # 前面那个 flaky 的状态已经被消耗到成功态，复用它只会跑 1 次。
    n_cb = {"i": 0}

    def flaky_cb():
        n_cb["i"] += 1
        if n_cb["i"] < 3:
            raise RuntimeError("cb boom %d" % n_cb["i"])
        return "done"

    seen = []
    retry_call(flaky_cb, policy=RetryPolicy(3, base_delay=0),
               sleep=lambda s: None, on_attempt=seen.append)
    add("on_attempt 被逐次调用", len(seen) == 3, len(seen))
    add("on_attempt 收到 attempt 号",
        [e["attempt"] for e in seen] == [1, 2, 3],
        [e["attempt"] for e in seen])

    def bad_cb(e):
        raise RuntimeError("cb boom")

    out = retry_call(ok_fn, policy=RetryPolicy(1), sleep=lambda s: None,
                     on_attempt=bad_cb)
    add("on_attempt 抛异常不影响结果", out.ok is True, out.summary())

    # --- sleep 真的被调用且次数正确 ---
    slept = []
    retry_call(always_fail, policy=RetryPolicy(2, base_delay=0.1,
                                               jitter=0),
               sleep=slept.append)
    add("重试前睡 2 次（3 次尝试）", len(slept) == 2, slept)
    add("睡眠时长符合退避",
        abs(slept[0] - 0.1) < 1e-9 and abs(slept[1] - 0.15) < 1e-9, slept)

    # --- deadline 限制总时长 ---
    # 用一个"假时钟"来验证 deadline：sleep 只推进虚拟时间，
    # 不真的睡。否则这个 selfcheck 会耗掉真实的时间预算。
    n_dl = {"i": 0}
    virt = {"t": 0.0}

    def slow_fail():
        n_dl["i"] += 1
        virt["t"] += 0.06      # 每次尝试"消耗" 60ms
        raise RuntimeError("slow")

    class _FakeClock:
        """假的 time.time：把虚拟推进量叠加到真实时间上。"""

        def __init__(self, base):
            self.base = base
            self.offset = 0.0

        def __call__(self):
            return self.base + self.offset

    import time as _t
    _real = _t.time
    _t.time = _FakeClock(_real())       # type: ignore[assignment]
    try:
        def _sleeper(d):
            virt["t"] += d
            _t.time.offset = virt["t"]   # type: ignore[attr-defined]
        out = retry_call(slow_fail,
                         policy=RetryPolicy(100, base_delay=0.5, jitter=0,
                                            deadline=0.2),
                         sleep=_sleeper)
    finally:
        _t.time = _real                 # type: ignore[assignment]
    add("deadline 限制尝试次数", out.attempts < 10, out.attempts)
    add("deadline reason 可读",
        "时限" in out.gave_up_reason or "次数" in out.gave_up_reason,
        out.gave_up_reason)

    # --- 装饰器 ---
    @with_retry(max_retries=2, verify=lambda v: VerifyResult.ok_(),
                sleep=lambda s: None)
    def decorated():
        return 42

    res = decorated()
    add("装饰器返回 RetryOutcome", isinstance(res, RetryOutcome), type(res))
    add("装饰器结果 ok", res.ok is True and res.value == 42, res.summary())
    add("装饰器保留 __name__", decorated.__name__ == "decorated",
        decorated.__name__)
    add("装饰器带 policy 属性",
        isinstance(getattr(decorated, "policy", None), RetryPolicy))

    @with_retry
    def bare():
        return 7

    add("无括号装饰器可用", bare().ok is True, bare().summary())

    @with_retry(max_retries=1, return_outcome=False,
                verify=lambda v: VerifyResult.ok_(), sleep=lambda s: None)
    def plain():
        return "plain"

    add("return_outcome=False 返回原值", plain() == "plain", plain())

    @with_retry(max_retries=0, return_outcome=False, sleep=lambda s: None)
    def boom():
        raise RuntimeError("x")

    try:
        boom()
        add("return_outcome=False 失败抛 AvatarError", False, "没抛")
    except AvatarError as e:
        add("return_outcome=False 失败抛 AvatarError",
            "重试" in str(e), str(e)[:60])

    add("默认策略存在", isinstance(DEFAULT_POLICY, RetryPolicy))
    add("默认 max_retries=3", DEFAULT_POLICY.max_retries == 3)
    add("默认 backoff=1.5", abs(DEFAULT_POLICY.backoff - 1.5) < 1e-9)

    # --- RetryOutcome 契约 ---
    out = RetryOutcome(True, value=1, attempts=2)
    add("RetryOutcome describe 字段齐",
        set(out.describe()) >= {"ok", "verdict", "attempts", "retried",
                                "elapsed", "gave_up_reason", "verify",
                                "attempt_log"},
        sorted(out.describe()))
    add("RetryOutcome bool True", bool(out) is True)
    add("RetryOutcome bool False", bool(RetryOutcome(False)) is False)
    add("summary 提到次数", "尝试 2 次" in out.summary(), out.summary())

    # --- KeyboardInterrupt 不吞 ---
    def interrupt():
        raise KeyboardInterrupt()

    try:
        retry_call(interrupt, policy=RetryPolicy(3, base_delay=0),
                   sleep=lambda s: None)
        add("KeyboardInterrupt 不被吞", False, "被吞了")
    except KeyboardInterrupt:
        add("KeyboardInterrupt 不被吞", True)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "verify.retry"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
