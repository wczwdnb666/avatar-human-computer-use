# -*- coding: utf-8 -*-
"""阶段七 · LLM 后端（``agentic_planner/llm_backend`` + ``LLMPlanner``）。

## 这个文件在守什么

调外部大模型有三件事做错代价最大：

1. **没超时** —— HTTP 会永久挂住
2. **没预算** —— 账单失控
3. **重试了不该重试的错误** —— 401 重试一万次还是 401

## 全程 mock

所有 HTTP 都走注入的假函数。**一个字节都不出网。**
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from avatar.agentic_planner.llm_backend import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    RETRYABLE_STATUS,
    AnthropicBackend,
    OpenAICompatibleBackend,
    TokenBudget,
    estimate_tokens,
    get_backend,
    normalize_tools,
)


class FakeHTTP:
    def __init__(self, reply="", exc=None, replies=None):
        self.calls = []
        self.reply = reply
        self.exc = exc
        self.replies = list(replies or [])

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        if self.exc is not None:
            raise self.exc
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        return self.reply


NOSLEEP = lambda s: None
_OK = json.dumps({"choices": [{"message": {"content": "hi"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def _bk(http, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("api_key", "k")
    kw.setdefault("base_url", "https://api.example/v1/chat")
    kw.setdefault("model", "m")
    kw.setdefault("http_post", http)
    kw.setdefault("sleep", NOSLEEP)
    return OpenAICompatibleBackend(**kw)


class TestTokenBudget:
    def test_per_request_limit(self):
        b = TokenBudget(per_request=100, total=1000)
        assert b.check(101)[0] is False
        assert b.check(100)[0] is True

    def test_total_limit(self):
        b = TokenBudget(per_request=100, total=150)
        b.consume(100)
        assert b.check(100)[0] is False

    def test_remaining(self):
        b = TokenBudget(total=100)
        b.consume(30)
        assert b.remaining == 70

    def test_rejected_counter(self):
        b = TokenBudget(per_request=10, total=10)
        b.check(999)
        b.check(999)
        assert b.rejected == 2

    def test_estimate_cjk_more_expensive(self):
        assert estimate_tokens("中文中文中文中文") > \
            estimate_tokens("abcdefghijklmnop")

    def test_estimate_never_raises(self):
        for x in (None, 1, [], {}, object(), "x"):
            assert estimate_tokens(x) >= 0


class TestDisabledByDefault:
    def test_default_off(self):
        assert OpenAICompatibleBackend().active is False

    def test_enabled_without_key_off(self):
        assert OpenAICompatibleBackend(enabled=True).active is False

    def test_no_request_when_disabled(self):
        """★★ 未启用时**零请求**。"""
        f = FakeHTTP(reply=_OK)
        r = _bk(f, enabled=False).complete([{"role": "user",
                                             "content": "x"}])
        assert r.ok is False and f.calls == []

    def test_reason_lists_missing(self):
        b = OpenAICompatibleBackend()
        assert "未启用" in b.unavailable_reason()
        assert "api_key" in OpenAICompatibleBackend(
            enabled=True).unavailable_reason()


class TestRequestShape:
    def test_timeout_forwarded(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).complete([{"role": "user", "content": "x"}])
        assert f.calls[0][3] == DEFAULT_TIMEOUT

    def test_user_agent_present(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).complete([{"role": "user", "content": "x"}])
        assert f.calls[0][2].get("User-Agent")

    def test_key_not_in_url(self):
        f = FakeHTTP(reply=_OK)
        _bk(f, api_key="SECRET").complete([{"role": "user", "content": "x"}])
        assert "SECRET" not in f.calls[0][0]

    def test_temperature_zero(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).complete([{"role": "user", "content": "x"}])
        assert json.loads(f.calls[0][1].decode())["temperature"] == 0.0

    def test_budget_blocks_before_sending(self):
        """★★ 超预算时**不发请求**（硬约束 5）。"""
        f = FakeHTTP(reply=_OK)
        b = _bk(f, budget=TokenBudget(per_request=5, total=5))
        r = b.complete([{"role": "user", "content": "很长" * 200}])
        assert r.ok is False and f.calls == []
        assert "预算" in r.reason


class TestRetry:
    def test_retries_on_503_then_succeeds(self):
        f = FakeHTTP(replies=[
            urllib.error.HTTPError("u", 503, "busy", {}, None),
            urllib.error.HTTPError("u", 429, "slow", {}, None),
            _OK])
        r = _bk(f).complete([{"role": "user", "content": "x"}])
        assert r.ok and r.attempts == 3

    def test_does_not_retry_401(self):
        """★★ 401 重试没用 —— 只调一次。"""
        f = FakeHTTP(exc=urllib.error.HTTPError("u", 401, "bad", {}, None))
        r = _bk(f).complete([{"role": "user", "content": "x"}])
        assert r.ok is False and len(f.calls) == 1

    def test_network_error_retries_to_limit(self):
        f = FakeHTTP(exc=urllib.error.URLError("down"))
        r = _bk(f).complete([{"role": "user", "content": "x"}])
        assert r.ok is False and len(f.calls) == DEFAULT_MAX_RETRIES

    def test_backoff_increases(self):
        slept = []
        f = FakeHTTP(exc=urllib.error.URLError("x"))
        _bk(f, sleep=lambda s: slept.append(s)).complete(
            [{"role": "user", "content": "x"}])
        assert len(slept) == 2 and slept[1] > slept[0]
        assert all(s <= 8.0 for s in slept)

    def test_4xx_not_retryable(self):
        for code in (400, 401, 403, 404, 422):
            assert code not in RETRYABLE_STATUS
        for code in (429, 500, 502, 503, 504):
            assert code in RETRYABLE_STATUS


class TestResponseParsing:
    def test_openai_tool_calls(self):
        b = _bk(FakeHTTP())
        r = b.parse_response(json.dumps({"choices": [{"message": {
            "tool_calls": [{"id": "c1", "function": {
                "name": "click", "arguments": '{"x": 1}'}}]}}]}))
        assert r.ok and r.tool_calls[0]["arguments"] == {"x": 1}

    def test_anthropic_content_blocks(self):
        ab = AnthropicBackend(enabled=True, api_key="k",
                              base_url="https://x", model="c")
        r = ab.parse_response(json.dumps({
            "content": [{"type": "text", "text": "好的"},
                        {"type": "tool_use", "id": "t", "name": "click",
                         "input": {"x": 2}}],
            "usage": {"input_tokens": 3, "output_tokens": 7}}))
        assert r.ok and r.text == "好的"
        assert r.tool_calls[0]["arguments"] == {"x": 2}
        assert (r.prompt_tokens, r.completion_tokens) == (3, 7)

    @pytest.mark.parametrize("bad", ["", "{坏的", "[1,2]", '{"choices":[]}',
                                     '{"error":{"message":"x"}}'])
    def test_garbage_never_raises(self, bad):
        assert _bk(FakeHTTP()).parse_response(bad).ok is False


class TestToolNormalization:
    TOOLS = [{"name": "click", "description": "点",
              "parameters": {"type": "object", "properties": {}}}]

    def test_openai_wraps_in_function(self):
        out = normalize_tools(self.TOOLS, protocol="openai")
        assert out[0]["type"] == "function"
        assert out[0]["function"]["name"] == "click"

    def test_anthropic_uses_input_schema(self):
        out = normalize_tools(self.TOOLS, protocol="anthropic")
        assert "input_schema" in out[0] and "function" not in out[0]

    def test_anthropic_system_hoisted(self):
        ab = AnthropicBackend(enabled=True, api_key="k", base_url="https://x",
                              model="c")
        p = ab.build_payload([{"role": "system", "content": "S"},
                              {"role": "user", "content": "U"}])
        assert p["system"] == "S"
        assert all(m["role"] != "system" for m in p["messages"])

    def test_anthropic_headers(self):
        ab = AnthropicBackend(enabled=True, api_key="K",
                              base_url="https://x", model="c")
        h = ab.build_headers()
        assert h["x-api-key"] == "K" and h["anthropic-version"]


class TestFactory:
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            get_backend("gpt5")

    def test_deepseek_is_openai_compatible(self):
        assert isinstance(get_backend("deepseek"),
                          OpenAICompatibleBackend)

    def test_anthropic_backend(self):
        assert isinstance(get_backend("anthropic"), AnthropicBackend)

    def test_env_key(self):
        b = get_backend("openai", env={"AVATAR_LLM_API_KEY": "EK"})
        assert b.api_key == "EK"

    def test_still_disabled_by_default(self):
        assert get_backend("openai", env={"AVATAR_LLM_API_KEY": "EK"}).active \
            is False

    def test_key_redacted(self):
        b = get_backend("openai", api_key="sk-abcdefgh12345678",
                        enabled=True)
        assert "sk-abcdefgh12345678" not in b.describe()


class TestLLMPlanner:
    def _planner(self, text="", ok=True, **kw):
        from avatar.agentic_planner.planner import LLMPlanner

        class _Fake:
            def __init__(self):
                self.calls = []

            def complete(self, messages, **k):
                self.calls.append((messages, k))

                class R:
                    pass
                r = R()
                r.ok = ok
                r.text = text
                r.reason = "测试" if not ok else ""
                return r

        return LLMPlanner(enabled=True, backend=_Fake(), **kw)

    GOOD = json.dumps({"steps": [
        {"id": "s1", "action": "open", "params": {"target": "notepad"}},
        {"id": "s2", "action": "type", "params": {"text": "hi"},
         "depends_on": ["s1"]}]})

    def test_plans_from_llm(self):
        from avatar.agentic_planner import parse_goal
        p = self._planner(self.GOOD).plan(parse_goal("打开记事本输入hi"))
        assert len(p.steps) == 2 and p.validate() == []
        assert p.planner == "llm"

    def test_deps_preserved(self):
        from avatar.agentic_planner import parse_goal
        p = self._planner(self.GOOD).plan(parse_goal("打开记事本输入hi"))
        assert p.step_by_id("s2").depends_on == ["s1"]

    def test_verify_filled(self):
        from avatar.agentic_planner import parse_goal
        p = self._planner(self.GOOD).plan(parse_goal("打开记事本输入hi"))
        assert all(st.verify.get("kind") for st in p.steps)

    def test_fallback_on_failure(self):
        """★★ 硬约束 7：LLM 失败必须回退规则版。"""
        from avatar.agentic_planner import parse_goal
        pl = self._planner(ok=False)
        p = pl.plan(parse_goal("打开记事本输入hello"))
        assert "llm-fallback" in p.planner
        assert p.validate() == []
        assert pl.last_fallback_reason

    def test_fallback_on_junk(self):
        from avatar.agentic_planner import parse_goal
        p = self._planner("这不是 JSON").plan(parse_goal("打开记事本"))
        assert "llm-fallback" in p.planner

    def test_fallback_on_cycle(self):
        """★★ 模型给出有环的计划 → 回退（硬约束 3）。"""
        from avatar.agentic_planner import parse_goal
        cyc = json.dumps({"steps": [
            {"id": "a", "action": "click", "depends_on": ["b"]},
            {"id": "b", "action": "click", "depends_on": ["a"]}]})
        p = self._planner(cyc).plan(parse_goal("打开记事本"))
        assert "llm-fallback" in p.planner

    def test_no_fallback_raises(self):
        from avatar.agentic_planner import PlannerError, parse_goal
        pl = self._planner(ok=False, fallback_to_rule=False)
        with pytest.raises(PlannerError):
            pl.plan(parse_goal("打开记事本"))

    def test_parse_tolerates_markdown(self):
        from avatar.agentic_planner import parse_goal
        pl = self._planner()
        got, err = pl.parse_plan("```json\n" + self.GOOD + "\n```",
                                 parse_goal("x"))
        assert got is not None, err

    def test_parse_tolerates_preamble(self):
        from avatar.agentic_planner import parse_goal
        pl = self._planner()
        got, err = pl.parse_plan("好的：\n" + self.GOOD, parse_goal("x"))
        assert got is not None, err

    @pytest.mark.parametrize("bad", ["", "非 JSON", '{"steps":[]}',
                                     '{"steps":[{"id":"a"}]}'])
    def test_parse_rejects_with_reason(self, bad):
        from avatar.agentic_planner import parse_goal
        got, err = self._planner().parse_plan(bad, parse_goal("x"))
        assert got is None and err

    def test_disabled_falls_back(self):
        from avatar.agentic_planner import parse_goal
        from avatar.agentic_planner.planner import LLMPlanner
        p = LLMPlanner(enabled=False).plan(parse_goal("打开记事本"))
        assert "llm-fallback" in p.planner


class TestNoSDK:
    def test_llm_backend_no_sdk(self):
        import ast
        from avatar.agentic_planner import llm_backend as LB
        tree = ast.parse(open(LB.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert not (mods & {"openai", "anthropic", "requests", "httpx",
                            "aiohttp", "litellm"}), mods
        assert "urllib" in mods
