# -*- coding: utf-8 -*-
"""阶段八 · 视觉后端（``vision/llm_backend``）。

## 这个文件在守什么

1. **默认关闭**（硬约束 2）—— 必须 enabled + api_key
2. **超时 + User-Agent + 重试**（硬约束 3）
3. **两家格式不能写混** —— OpenAI 用 image_url，Anthropic 用 source

## 全程 mock

所有 HTTP 走注入的假函数。**一个字节都不出网。**
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from avatar.vision.llm_backend import (
    DEFAULT_VISION_MAX_RETRIES,
    DEFAULT_VISION_TIMEOUT,
    VISION_PROVIDERS,
    AnthropicVisionBackend,
    OpenAICompatibleVisionBackend,
    VisionError,
    VisionResult,
    get_vision_backend,
    normalize_image,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
_OK = json.dumps({
    "choices": [{"message": {"content": json.dumps({
        "found": True, "x": 10, "y": 20, "confidence": 0.9,
        "reason": "ok"})}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
_NOSLEEP = lambda s: None


class FakeHTTP:
    def __init__(self, reply="", exc=None, replies=None):
        self.calls = []
        self.reply, self.exc = reply, exc
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


def _bk(http, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("api_key", "k")
    kw.setdefault("base_url", "https://x/v1/chat")
    kw.setdefault("model", "m")
    kw.setdefault("http_post", http)
    kw.setdefault("sleep", _NOSLEEP)
    return OpenAICompatibleVisionBackend(**kw)


class TestContract:
    def test_docstring_has_boundary(self):
        import avatar.vision.llm_backend as M
        assert "能力边界" in (M.__doc__ or "")

    def test_six_providers(self):
        assert len(VISION_PROVIDERS) == 6

    def test_timeout_and_retries(self):
        assert DEFAULT_VISION_TIMEOUT == 60.0
        assert DEFAULT_VISION_MAX_RETRIES == 3


class TestDisabledByDefault:
    def test_off_by_default(self):
        assert OpenAICompatibleVisionBackend().active is False

    def test_needs_key(self):
        assert OpenAICompatibleVisionBackend(
            enabled=True, base_url="https://x", model="m").active is False

    def test_active_when_complete(self):
        assert OpenAICompatibleVisionBackend(
            enabled=True, api_key="k", base_url="https://x",
            model="m").active is True

    def test_zero_request_when_disabled(self):
        """★★ 未启用时**零请求**。"""
        f = FakeHTTP(reply=_OK)
        b = _bk(f, enabled=False)
        r = b.locate(_JPEG, "x")
        assert r.ok is False and f.calls == []


class TestImageNormalization:
    def test_openai_format(self):
        p = normalize_image(_JPEG, "image/jpeg", protocol="openai")
        assert p["type"] == "image_url"
        assert p["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_anthropic_format(self):
        p = normalize_image(_JPEG, "image/jpeg", protocol="anthropic")
        assert p["type"] == "image"
        assert p["source"]["type"] == "base64"
        assert not p["source"]["data"].startswith("data:")

    def test_no_double_encode(self):
        import base64
        pre = base64.b64encode(_JPEG).decode()
        p = normalize_image(pre, "image/jpeg", protocol="openai")
        assert p["image_url"]["url"].endswith(pre)

    def test_bad_mime_raises(self):
        with pytest.raises(VisionError):
            normalize_image(_JPEG, "image/tiff")

    def test_bad_type_raises(self):
        with pytest.raises(VisionError):
            normalize_image(12345)


class TestRequestShape:
    def test_success(self):
        f = FakeHTTP(reply=_OK)
        r = _bk(f).locate(_JPEG, "x", system="S", user="U")
        assert r.ok and r.found and r.x == 10 and r.y == 20

    def test_temperature_zero(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).locate(_JPEG, "x")
        body = json.loads(f.calls[0][1].decode())
        assert body["temperature"] == 0.0

    def test_image_before_text(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).locate(_JPEG, "x", system="S", user="U")
        body = json.loads(f.calls[0][1].decode())
        content = body["messages"][-1]["content"]
        assert content[0]["type"] == "image_url"

    def test_timeout_forwarded(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).locate(_JPEG, "x")
        assert f.calls[0][3] == DEFAULT_VISION_TIMEOUT

    def test_user_agent(self):
        f = FakeHTTP(reply=_OK)
        _bk(f).locate(_JPEG, "x")
        assert f.calls[0][2].get("User-Agent")

    def test_key_not_in_url(self):
        f = FakeHTTP(reply=_OK)
        _bk(f, api_key="SECRET").locate(_JPEG, "x")
        assert "SECRET" not in f.calls[0][0]

    def test_anthropic_system_hoisted(self):
        fa = FakeHTTP(reply=json.dumps({
            "content": [{"type": "text", "text": '{"found":false}'}]}))
        ab = AnthropicVisionBackend(
            enabled=True, api_key="K", base_url="https://x", model="c",
            http_post=fa, sleep=_NOSLEEP)
        ab.locate(_JPEG, "x", system="SYS", user="U")
        body = json.loads(fa.calls[0][1].decode())
        assert body["system"] == "SYS"
        assert all(m["role"] != "system" for m in body["messages"])
        assert body["messages"][0]["content"][0]["type"] == "image"

    def test_anthropic_headers(self):
        ab = AnthropicVisionBackend(enabled=True, api_key="K",
                                    base_url="https://x", model="c")
        h = ab.build_headers()
        assert h["x-api-key"] == "K" and h["anthropic-version"]


class TestParsing:
    @pytest.mark.parametrize("text", [
        '{"found":true,"x":1,"y":2,"confidence":0.9}',
        '```json\n{"found":true,"x":1,"y":2,"confidence":0.9}\n```',
        '好的：{"found":true,"x":1,"y":2,"confidence":0.9}',
        '{"found":true,"x":"3","y":"4","confidence":"0.7"}',
    ])
    def test_tolerant(self, text):
        b = _bk(FakeHTTP())
        got = b.parse_response(json.dumps(
            {"choices": [{"message": {"content": text}}]}))
        assert got.ok, got.reason

    def test_found_false_is_ok(self):
        """★ found=false 时 ok 仍为 True —— 那是**一次成功的调用**。"""
        b = _bk(FakeHTTP())
        got = b.parse_response(json.dumps({"choices": [
            {"message": {"content": '{"found":false,"reason":"没有"}'}}]}))
        assert got.ok is True and got.found is False

    @pytest.mark.parametrize("bad", ["", "非 JSON",
                                     '{"found":true,"x":"abc","y":1}'])
    def test_rejects_with_reason(self, bad):
        b = _bk(FakeHTTP())
        got = b.parse_response(json.dumps(
            {"choices": [{"message": {"content": bad}}]}))
        assert got.ok is False and got.reason

    def test_garbage_never_raises(self):
        b = _bk(FakeHTTP())
        for bad in ("{坏的", '{"choices":[]}',
                    '{"error":{"message":"x"}}'):
            assert b.parse_response(bad).ok is False


class TestRetry:
    def test_retries_then_succeeds(self):
        f = FakeHTTP(replies=[
            urllib.error.HTTPError("u", 503, "busy", {}, None),
            urllib.error.HTTPError("u", 429, "slow", {}, None),
            _OK])
        r = _bk(f).locate(_JPEG, "x")
        assert r.ok and r.attempts == 3

    def test_no_retry_on_401(self):
        f = FakeHTTP(exc=urllib.error.HTTPError("u", 401, "bad", {}, None))
        r = _bk(f).locate(_JPEG, "x")
        assert r.ok is False and len(f.calls) == 1

    def test_backoff_bounded(self):
        slept = []
        f = FakeHTTP(exc=urllib.error.URLError("down"))
        _bk(f, sleep=lambda s: slept.append(s)).locate(_JPEG, "x")
        assert len(slept) == 2 and slept[1] > slept[0]
        assert all(s <= 8.0 for s in slept)


class TestFactory:
    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            get_vision_backend("gpt5")

    @pytest.mark.parametrize("p", VISION_PROVIDERS)
    def test_all_disabled_by_default(self, p):
        """★★ 六家**全部**默认关闭（硬约束 2）。"""
        assert get_vision_backend(p).active is False

    def test_anthropic_class(self):
        assert isinstance(get_vision_backend("anthropic"),
                          AnthropicVisionBackend)

    def test_key_redacted(self):
        b = get_vision_backend("openai", api_key="sk-abcdefgh12345678",
                               enabled=True)
        assert "sk-abcdefgh12345678" not in b.describe()


class TestNoSDK:
    def test_no_sdk(self):
        import ast
        import avatar.vision.llm_backend as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & {"openai", "anthropic", "requests", "httpx",
                            "PIL"}), mods
        assert "urllib" in mods
