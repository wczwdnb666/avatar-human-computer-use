# -*- coding: utf-8 -*-
"""阶段五 · 外部打码（``avatar.risk_detector.solver``）的请求构造测试。

## 这个文件在守什么

打码模块**会花钱、会把截图发给第三方**。所以它的测试重点不是
"能不能打通平台"，而是：

1. **默认关闭是不是真的关死了** —— 没配置时一个字节都不许出网。
2. **请求构造对不对** —— 字段名、编码、header、timeout 全都要
   逐项断言。构造错了平台会拒，但**不会有任何报错提示**，
   只会一直返回 None，排查起来很痛苦。
3. **失败路径不抛异常** —— 网络断了、超时了、平台报错了，
   调用方拿到的必须是 None 而不是异常。

## 两层 mock，各测各的

- ``stub_http`` 夹具打桩 ``solver._http_post`` / ``_http_get``
  —— 测**上层逻辑**（闸门、解析、轮询、错误处理）。
- ``mock_urlopen`` 夹具打桩 ``urllib.request.urlopen``
  —— 测**``_default_http_post`` 自己**（Request 的 method /
  data / headers 有没有设对）。这一层不打桩的话，
  ``_default_http_post`` 就是唯一没被测到的代码。

**全程不发真实请求。**
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request

import pytest

from avatar.risk_detector import solver as S


# ==========================================================================
# 夹具
# ==========================================================================
class HttpRecorder:
    """记录所有"发出去了"的请求，并返回预设响应。"""

    def __init__(self, post_reply="", get_reply="", post_exc=None,
                 get_exc=None, get_replies=None):
        self.calls = []                 # [("POST"/"GET", url, body, headers, timeout)]
        self.post_reply = post_reply
        self.get_reply = get_reply
        self.post_exc = post_exc
        self.get_exc = get_exc
        self.get_replies = list(get_replies or [])   # 依次返回

    # ---- 打桩用的两个函数 ----
    def post(self, url, body, headers, timeout):
        self.calls.append(("POST", url, body, headers, timeout))
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_reply

    def get(self, url, headers, timeout):
        self.calls.append(("GET", url, b"", headers, timeout))
        if self.get_exc is not None:
            raise self.get_exc
        if self.get_replies:
            return self.get_replies.pop(0)
        return self.get_reply

    # ---- 断言辅助 ----
    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    @property
    def gets(self):
        return [c for c in self.calls if c[0] == "GET"]

    def last_post(self):
        assert self.posts, "一个 POST 都没发"
        return self.posts[-1]

    def body_text(self) -> str:
        return self.last_post()[2].decode("utf-8", "replace")

    def headers(self) -> dict:
        return self.last_post()[3]


@pytest.fixture
def stub_http(monkeypatch):
    """打桩 ``solver._http_post`` / ``_http_get``，返回一个记录器。"""
    rec = HttpRecorder()
    monkeypatch.setattr(S, "_http_post", rec.post)
    monkeypatch.setattr(S, "_http_get", rec.get)
    return rec


@pytest.fixture
def clean_env(monkeypatch):
    """清掉打码相关的环境变量 —— 免得开发机上的真实配置影响测试。

    这条很重要：如果开发机上设了 ``AVATAR_SOLVER_ENABLED=1``，
    下面所有"默认关闭"的测试都会假红，而假红的修法很容易
    变成"把断言改松"，那就把安全闸给拆了。
    """
    for k in (S.ENV_ENABLED, S.ENV_API_KEY, S.ENV_API_URL):
        monkeypatch.delenv(k, raising=False)
    return {}


@pytest.fixture
def on():
    """一个"配置完整"的配置对象 —— 明确表示"这条测试要发请求"。"""
    return S.SolverConfig(enabled=True, api_key="TESTKEY",
                          api_url="https://2captcha.com/in.php")


# ==========================================================================
# 1. 契约与常量
# ==========================================================================
class TestContract:
    def test_default_timeout_positive(self):
        assert S.DEFAULT_TIMEOUT > 0

    def test_user_agent_non_empty(self):
        assert S.DEFAULT_USER_AGENT.strip()

    def test_env_names(self):
        assert S.ENV_ENABLED == "AVATAR_SOLVER_ENABLED"
        assert S.ENV_API_KEY == "AVATAR_SOLVER_API_KEY"

    def test_exported_from_package(self):
        from avatar import risk_detector as pkg
        for n in ("solve_captcha_via_api", "solve_recaptcha_token",
                  "solver_enabled", "solver_config", "describe_solver"):
            assert hasattr(pkg, n), n

    def test_module_docstring_has_boundary(self):
        assert "能力边界" in (S.__doc__ or "")

    def test_no_hard_solving_disclaimer(self):
        """硬约束 #9：模块里不许出现"解滑块"之类的能力描述。

        这条用 AST 剥掉字符串常量后检查**代码**，
        防止将来有人真加了硬解逻辑还留着"我们不硬解"的文档。
        """
        import ast
        src = open(S.__file__, encoding="utf-8").read()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        for bad in ("find_gap", "detect_gap", "solve_slider",
                    "click_coordinates", "match_template"):
            assert bad not in names, "出现了硬解相关符号：%s" % bad


# ==========================================================================
# 2. 默认关闭（硬约束 #1）—— 本文件最重要的一节
# ==========================================================================
class TestDisabledByDefault:
    """打码会花钱、会把截图发出去。**没显式打开就一个字节都不发。**"""

    def test_config_defaults_off(self):
        c = S.SolverConfig()
        assert c.enabled is False
        assert c.api_key == ""
        assert c.active is False

    def test_enabled_alone_is_not_enough(self):
        assert S.SolverConfig(enabled=True).active is False

    def test_key_alone_is_not_enough(self):
        assert S.SolverConfig(api_key="k").active is False

    def test_both_needed(self):
        assert S.SolverConfig(enabled=True, api_key="k").active is True

    def test_is_enabled_matches_active(self):
        for c in (S.SolverConfig(),
                  S.SolverConfig(enabled=True),
                  S.SolverConfig(api_key="k"),
                  S.SolverConfig(enabled=True, api_key="k")):
            assert S.is_enabled(c) == c.active

    def test_is_enabled_never_raises(self):
        assert S.is_enabled(None) in (True, False)

    def test_default_off_sends_nothing(self, stub_http, clean_env):
        """★★ 核心断言：默认配置下调用，**零请求**。"""
        r = S.solve_captcha_via_api(
            api_url="https://2captcha.com/in.php",
            api_key="KEY",
            image_data=b"\x89PNG",
        )
        assert r is None
        assert stub_http.calls == [], \
            "默认关闭却发了请求：%r" % (stub_http.calls,)

    def test_enabled_true_without_key_sends_nothing(self, stub_http,
                                                    clean_env):
        r = S.solve_captcha_via_api(
            api_url="https://2captcha.com/in.php",
            api_key="",
            image_data=b"\x89PNG",
            enabled=True,
        )
        assert r is None
        assert stub_http.calls == []

    def test_key_without_enabled_sends_nothing(self, stub_http, clean_env):
        r = S.solve_captcha_via_api(
            api_url="https://2captcha.com/in.php",
            api_key="KEY",
            image_data=b"\x89PNG",
            enabled=False,
        )
        assert r is None
        assert stub_http.calls == []

    def test_missing_api_url_sends_nothing(self, stub_http, clean_env):
        r = S.solve_captcha_via_api(
            api_url="", api_key="KEY", image_data=b"\x89PNG", enabled=True)
        assert r is None
        assert stub_http.calls == []

    def test_recaptcha_default_off_sends_nothing(self, stub_http, clean_env):
        r = S.solve_recaptcha_token(
            site_key="SK", page_url="https://x.com", api_key="KEY")
        assert r is None
        assert stub_http.calls == []

    def test_detailed_reports_why_disabled(self, stub_http, clean_env):
        r = S.solve_captcha_detailed(
            api_url="https://2captcha.com/in.php", api_key="KEY",
            image_data=b"\x89PNG", enabled=False)
        assert r.ok is False
        assert r.reason == "solver_disabled"

    def test_detailed_reports_missing_key(self, stub_http, clean_env):
        r = S.solve_captcha_detailed(
            api_url="https://2captcha.com/in.php", api_key="",
            image_data=b"\x89PNG", enabled=True)
        assert r.ok is False
        assert r.reason == "solver_no_api_key"

    def test_env_can_enable(self, stub_http, clean_env):
        """环境变量配全 → 启用。"""
        r = S.solve_captcha_via_api(
            image_data=b"\x89PNG",
            env={S.ENV_ENABLED: "1", S.ENV_API_KEY: "EK",
                 S.ENV_API_URL: "https://2captcha.com/in.php"})
        assert len(stub_http.posts) == 1

    def test_env_enabled_without_key_stays_off(self, stub_http, clean_env):
        r = S.solve_captcha_via_api(
            image_data=b"\x89PNG",
            env={S.ENV_ENABLED: "1", S.ENV_API_URL: "https://x.com"})
        assert r is None
        assert stub_http.calls == []

    @pytest.mark.parametrize("val,expect", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True),
        ("on", True), ("Y", True),
        ("0", False), ("no", False), ("false", False), ("", False),
        ("random", False),
    ])
    def test_env_enabled_parsing(self, val, expect):
        c = S.SolverConfig.from_env({S.ENV_ENABLED: val,
                                     S.ENV_API_KEY: "k"})
        assert c.active is expect


# ==========================================================================
# 3. api_key 脱敏
# ==========================================================================
class TestRedaction:
    """key 绝不能进日志/报告。"""

    def test_to_dict_redacts_by_default(self):
        c = S.SolverConfig(enabled=True, api_key="abcdefgh12345678")
        assert "abcdefgh12345678" not in json.dumps(c.to_dict())

    def test_short_key_fully_masked(self):
        assert S.SolverConfig(api_key="short").to_dict()["api_key"] == "***"

    def test_describe_redacts(self):
        c = S.SolverConfig(enabled=True, api_key="abcdefgh12345678")
        assert "abcdefgh12345678" not in c.describe()

    def test_describe_solver_redacts(self):
        c = S.SolverConfig(enabled=True, api_key="abcdefgh12345678")
        assert "abcdefgh12345678" not in S.describe_solver(c)

    def test_explicit_opt_out_gives_plaintext(self):
        c = S.SolverConfig(api_key="abcdefgh12345678")
        assert c.to_dict(redact=False)["api_key"] == "abcdefgh12345678"


# ==========================================================================
# 4. multipart 构造
# ==========================================================================
class TestMultipart:
    def test_boundary_wraps_body(self):
        body, bd = S.build_multipart_body({"a": "1"}, boundary="B")
        assert body.startswith(b"--B")
        assert body.rstrip().endswith(b"--B--")

    def test_crlf_separators(self):
        body, _ = S.build_multipart_body({"a": "1"}, boundary="B")
        assert b"\r\n" in body

    def test_field_present(self):
        body, _ = S.build_multipart_body({"key": "SECRET"}, boundary="B")
        assert b'name="key"' in body
        assert b"SECRET" in body

    def test_disposition_header_format(self):
        body, _ = S.build_multipart_body({"k": "v"}, boundary="B")
        assert b'Content-Disposition: form-data; name="k"' in body

    def test_file_field_is_base64(self):
        raw = b"\x89PNG\r\n\x1a\n"
        body, _ = S.build_multipart_body({}, files={"body": raw},
                                         boundary="B")
        assert base64.b64encode(raw) in body
        assert raw not in body, "原始二进制不该直接进 body"

    def test_file_field_accepts_str(self):
        body, _ = S.build_multipart_body({}, files={"body": "abc"},
                                         boundary="B")
        assert base64.b64encode(b"abc") in body

    def test_boundary_returned_matches_body(self):
        body, bd = S.build_multipart_body({"a": "1"}, boundary="XYZ")
        assert bd == "XYZ"
        assert b"--XYZ" in body

    def test_auto_boundary_is_unique(self):
        _, a = S.build_multipart_body({"a": "1"})
        _, b = S.build_multipart_body({"a": "1"})
        assert a != b, "自动 boundary 必须唯一，否则并发会串"

    def test_unicode_value_survives(self):
        body, _ = S.build_multipart_body({"note": "验证码"}, boundary="B")
        assert "验证码".encode("utf-8") in body

    def test_empty_fields_still_valid(self):
        body, _ = S.build_multipart_body({}, boundary="B")
        assert body.rstrip().endswith(b"--B--")


# ==========================================================================
# 5. JSON 构造
# ==========================================================================
class TestJsonBody:
    def test_roundtrip(self):
        b = S.build_json_body({"key": "k", "n": 1})
        assert json.loads(b.decode("utf-8")) == {"key": "k", "n": 1}

    def test_ensure_ascii_false(self):
        b = S.build_json_body({"t": "验证"})
        assert "验证" in b.decode("utf-8")

    def test_bytes_auto_base64(self):
        b = S.build_json_body({"img": b"\x89PNG"})
        assert json.loads(b.decode("utf-8"))["img"] == \
            base64.b64encode(b"\x89PNG").decode("ascii")

    def test_nested_dict_bytes(self):
        b = S.build_json_body({"a": {"b": b"\x01"}})
        assert json.loads(b.decode("utf-8"))["a"]["b"] == \
            base64.b64encode(b"\x01").decode("ascii")

    def test_list_bytes(self):
        b = S.build_json_body({"xs": [b"\x01", "s"]})
        assert json.loads(b.decode("utf-8"))["xs"][0] == \
            base64.b64encode(b"\x01").decode("ascii")

    def test_compact_separators(self):
        b = S.build_json_body({"a": 1, "b": 2})
        assert b", " not in b, "不该有多余空格"


# ==========================================================================
# 6. 编码选择
# ==========================================================================
class TestPickMode:
    def test_capsolver_is_json(self):
        assert S.pick_mode("https://api.capsolver.com/createTask") == "json"

    def test_2captcha_is_multipart(self):
        assert S.pick_mode("https://2captcha.com/in.php") == "multipart"

    def test_explicit_wins(self):
        assert S.pick_mode("https://api.capsolver.com/x", "multipart") \
            == "multipart"
        assert S.pick_mode("https://2captcha.com/x", "json") == "json"

    def test_unknown_url_defaults_multipart(self):
        assert S.pick_mode("https://example.com/api") == "multipart"

    def test_empty_url_defaults_multipart(self):
        assert S.pick_mode("") == "multipart"

    def test_bad_mode_falls_back_to_auto(self):
        assert S.pick_mode("https://api.capsolver.com/x", "garbage") == "json"


# ==========================================================================
# 7. 响应解析
# ==========================================================================
class TestParseResponse:
    def test_2captcha_success(self):
        r = S.parse_response('{"status": 1, "request": "TOKEN"}')
        assert r.ok and r.text == "TOKEN"

    def test_2captcha_error(self):
        r = S.parse_response('{"status": 0, "request": "ERROR_WRONG_USER_KEY"}')
        assert not r.ok
        assert "ERROR_WRONG_USER_KEY" in r.reason

    def test_not_ready_is_distinguishable(self):
        """★ 关键：``CAPCHA_NOT_READY`` 不是失败，是"再等等"。"""
        r = S.parse_response('{"status": 0, "request": "CAPCHA_NOT_READY"}')
        assert not r.ok
        assert r.reason == "not_ready"

    def test_plain_ok_format(self):
        r = S.parse_response("OK|TOKEN")
        assert r.ok and r.text == "TOKEN"

    def test_plain_error_format(self):
        r = S.parse_response("ERROR_CAPTCHA_UNSOLVABLE")
        assert not r.ok

    def test_plain_not_ready(self):
        r = S.parse_response("CAPCHA_NOT_READY")
        assert r.reason == "not_ready"

    def test_capsolver_success(self):
        r = S.parse_response(
            '{"errorId": 0, "solution": {"gRecaptchaResponse": "T"}}')
        assert r.ok and r.text == "T"

    def test_capsolver_error(self):
        r = S.parse_response(
            '{"errorId": 12, "errorCode": "ERROR_ZERO_BALANCE",'
            ' "errorDescription": "no funds"}')
        assert not r.ok
        assert "ERROR_ZERO_BALANCE" in r.reason

    def test_status_success_word(self):
        assert S.parse_response('{"status": "success", "solution": "s"}').ok

    @pytest.mark.parametrize("bad", ["", "   ", "{not json", "[1,2]", "null"])
    def test_garbage_never_raises(self, bad):
        r = S.parse_response(bad)
        assert r.ok is False

    def test_raw_preserved_for_debug(self):
        r = S.parse_response('{"status": 0, "request": "ERROR_X"}')
        assert r.raw is not None


# ==========================================================================
# 8. 图片输入三种形态
# ==========================================================================
class TestImageCoercion:
    def test_bytes_passthrough(self):
        blob, bad = S._coerce_image(b"\x89PNG")
        assert blob == b"\x89PNG" and bad == ""

    def test_long_base64_string(self):
        s = base64.b64encode(b"z" * 500).decode()
        blob, bad = S._coerce_image(s)
        assert bad == ""
        assert blob == s.encode("ascii")

    def test_data_uri_prefix_stripped(self):
        s = "data:image/png;base64," + base64.b64encode(b"z" * 500).decode()
        blob, bad = S._coerce_image(s)
        assert bad == ""
        assert not blob.startswith(b"data:")

    def test_file_path(self, tmp_path):
        p = tmp_path / "cap.png"
        p.write_bytes(b"\x89PNG_FILE")
        blob, bad = S._coerce_image(str(p))
        assert bad == ""
        assert blob == b"\x89PNG_FILE"

    @pytest.mark.parametrize("bad_input,expect", [
        (b"", "empty_image"),
        ("", "empty_image"),
        (None, "empty_image"),
        (12345, "image_type_bad"),
        ([1, 2], "image_type_bad"),
    ])
    def test_bad_inputs(self, bad_input, expect):
        _, bad = S._coerce_image(bad_input)
        assert bad.startswith(expect.split(":")[0]), bad

    def test_missing_path_reported(self):
        _, bad = S._coerce_image("C:/definitely/not/here.png")
        assert bad.startswith("image_not_found")


# ==========================================================================
# 9. 请求构造（打桩 _http_post 层）
# ==========================================================================
class TestRequestConstruction:
    """字段名错一个平台就拒 —— 但**不会报错**，只会一直返回 None。

    所以这里逐项断言，别偷懒。
    """

    def test_actually_posts(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        assert len(stub_http.posts) == 1

    def test_timeout_is_passed(self, stub_http, clean_env):
        """硬约束 #2：**所有请求必须带 timeout**。"""
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            timeout=33.0, enabled=True)
        assert stub_http.last_post()[4] == 33.0

    def test_default_timeout_used(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        assert stub_http.last_post()[4] == S.DEFAULT_TIMEOUT

    def test_user_agent_header(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        ua = stub_http.headers().get("User-Agent", "")
        assert ua and "avatar" in ua.lower()

    def test_content_type_multipart_with_boundary(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        ct = stub_http.headers().get("Content-Type", "")
        assert ct.startswith("multipart/form-data; boundary=")
        # boundary 必须和 body 里的一致
        bd = ct.split("boundary=", 1)[1]
        assert ("--%s" % bd).encode() in stub_http.last_post()[2]

    def test_api_key_in_body(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "SECRETKEY", b"\x89PNG",
            enabled=True)
        assert b"SECRETKEY" in stub_http.last_post()[2]

    def test_api_key_not_in_url(self, stub_http, clean_env):
        """key 走 body，不走 query —— URL 会进各种日志。"""
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "SECRETKEY", b"\x89PNG",
            enabled=True)
        assert "SECRETKEY" not in stub_http.last_post()[1]

    def test_image_base64_in_body(self, stub_http, clean_env):
        raw = b"\x89PNG_IMAGE_DATA"
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", raw, enabled=True)
        assert base64.b64encode(raw) in stub_http.last_post()[2]

    def test_method_base64_field(self, stub_http, clean_env):
        """2Captcha 的 ``in.php`` 要求 ``method=base64``。"""
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        assert b"method" in stub_http.last_post()[2]
        assert b"base64" in stub_http.last_post()[2]

    def test_capsolver_json_content_type(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://api.capsolver.com/createTask", "CK", b"\x89PNG",
            enabled=True)
        assert stub_http.headers().get("Content-Type") == "application/json"

    def test_capsolver_json_body_parseable(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://api.capsolver.com/createTask", "CK", b"\x89PNG",
            enabled=True)
        d = json.loads(stub_http.last_post()[2].decode("utf-8"))
        assert d.get("key") == "CK"
        assert d.get("image") == base64.b64encode(b"\x89PNG").decode()

    def test_extra_fields_merged(self, stub_http, clean_env):
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, extra_fields={"soft_id": "42"})
        body = stub_http.last_post()[2]
        assert b"soft_id" in body and b"42" in body

    def test_config_object_takes_priority(self, stub_http, clean_env):
        cfg = S.SolverConfig(enabled=True, api_key="CFGKEY",
                             api_url="https://cfg.example/x")
        S.solve_captcha_via_api(
            "https://ignored.example/x", "IGNORED", b"\x89PNG",
            config=cfg)
        assert "cfg.example" in stub_http.last_post()[1]
        assert b"CFGKEY" in stub_http.last_post()[2]


# ==========================================================================
# 10. mock urllib —— 测 _default_http_post 自己
# ==========================================================================
class TestDefaultHttpPost:
    """上面那些测试打桩的是 ``_http_post``，所以 ``_default_http_post``
    本身**从没被执行过**。这一节补上 —— 它才是真正碰网络的代码。
    """

    def test_request_method_is_post(self, monkeypatch):
        seen = {}

        class _Resp:
            def read(self):
                return b'{"status": 1, "request": "T"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["data"] = req.data
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        out = S._default_http_post("https://x.com/api", b"BODY",
                                   {"User-Agent": "UA"}, 12.0)

        assert seen["method"] == "POST"
        assert seen["data"] == b"BODY"
        assert seen["url"] == "https://x.com/api"
        assert seen["timeout"] == 12.0
        assert out == '{"status": 1, "request": "T"}'

    def test_headers_are_set(self, monkeypatch):
        seen = {}

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            seen["headers"] = dict(req.header_items())
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        S._default_http_post("https://x.com", b"B", {"User-Agent": "MYUA"}, 1.0)

        # urllib 会把头名规范化成 Title-Case
        got = {k.lower(): v for k, v in seen["headers"].items()}
        assert got.get("user-agent") == "MYUA"

    def test_timeout_always_forwarded(self, monkeypatch):
        """硬约束 #2 的最底层验证 —— urlopen 必须收到 timeout。"""
        seen = {}

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: (
                                seen.__setitem__("t", timeout), _Resp())[1])
        S._default_http_post("https://x.com", b"B", {}, 7.5)
        assert seen["t"] == 7.5

    def test_default_http_get_method(self, monkeypatch):
        seen = {}

        class _Resp:
            def read(self):
                return b"OK"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["timeout"] = timeout
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        out = S._default_http_get("https://x.com/q?a=1", {"User-Agent": "UA"},
                                  5.0)
        assert seen["method"] == "GET"
        assert seen["timeout"] == 5.0
        assert out == "OK"


# ==========================================================================
# 11. reCAPTCHA 流程
# ==========================================================================
class TestRecaptcha:
    def test_happy_path_returns_token(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "TASK1"}'
        stub_http.get_replies = ['{"status": 0, "request": "CAPCHA_NOT_READY"}',
                                 '{"status": 1, "request": "TOKEN_XYZ"}']
        tok = S.solve_recaptcha_token(
            "SITEKEY", "https://target.example/login", "KEY",
            enabled=True, poll_interval=0.0)
        assert tok == "TOKEN_XYZ"

    def test_two_step_protocol(self, stub_http, clean_env):
        """必须先 POST 下单，再 GET 轮询 —— 顺序不能反。"""
        stub_http.post_reply = '{"status": 1, "request": "TASK1"}'
        stub_http.get_replies = ['{"status": 1, "request": "TOK"}']
        S.solve_recaptcha_token("SK", "https://x.com", "K",
                                enabled=True, poll_interval=0.0)
        kinds = [c[0] for c in stub_http.calls]
        assert kinds == ["POST", "GET"], kinds

    def test_submit_has_googlekey_and_pageurl(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_replies = ['{"status": 1, "request": "TOK"}']
        S.solve_recaptcha_token("MY_SITE_KEY", "https://target.example/p",
                                "K", enabled=True, poll_interval=0.0)
        body = stub_http.posts[0][2]
        assert b"MY_SITE_KEY" in body
        assert b"target.example" in body

    def test_submit_method_is_userrecaptcha(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_replies = ['{"status": 1, "request": "TOK"}']
        S.solve_recaptcha_token("SK", "https://x.com", "K",
                                enabled=True, poll_interval=0.0)
        assert b"userrecaptcha" in stub_http.posts[0][2]

    def test_poll_url_carries_task_id(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "TASK_ABC"}'
        stub_http.get_replies = ['{"status": 1, "request": "TOK"}']
        S.solve_recaptcha_token("SK", "https://x.com", "K",
                                enabled=True, poll_interval=0.0)
        assert "TASK_ABC" in stub_http.gets[0][1]

    def test_poll_until_ready(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_replies = [
            '{"status": 0, "request": "CAPCHA_NOT_READY"}',
            '{"status": 0, "request": "CAPCHA_NOT_READY"}',
            '{"status": 0, "request": "CAPCHA_NOT_READY"}',
            '{"status": 1, "request": "TOK"}',
        ]
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0)
        assert tok == "TOK"
        assert len(stub_http.gets) == 4

    def test_poll_exhausted_returns_none(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_reply = '{"status": 0, "request": "CAPCHA_NOT_READY"}'
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0,
                                      max_poll=3)
        assert tok is None
        assert len(stub_http.gets) == 3, "轮询次数必须恰好是 max_poll"

    def test_hard_error_stops_polling(self, stub_http, clean_env):
        """余额不足这类**真错误**必须立刻退出，别白轮询 24 次。"""
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_reply = '{"status": 0, "request": "ERROR_ZERO_BALANCE"}'
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0,
                                      max_poll=10)
        assert tok is None
        assert len(stub_http.gets) == 1, "真错误却继续轮询了"

    def test_submit_failure_returns_none(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 0, "request": "ERROR_WRONG_USER_KEY"}'
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0)
        assert tok is None
        assert stub_http.gets == [], "下单失败还去轮询了"

    def test_no_task_id_returns_none(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": ""}'
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0)
        assert tok is None

    def test_missing_site_key(self, stub_http, clean_env):
        assert S.solve_recaptcha_token("", "https://x.com", "K",
                                       enabled=True,
                                       poll_interval=0.0) is None
        assert stub_http.calls == []

    def test_missing_page_url(self, stub_http, clean_env):
        assert S.solve_recaptcha_token("SK", "", "K", enabled=True,
                                       poll_interval=0.0) is None
        assert stub_http.calls == []

    def test_poll_network_error_is_tolerated(self, clean_env, monkeypatch):
        """单次轮询失败不放弃 —— 网络抖动是常态。"""
        calls = {"n": 0}

        def _post(u, b, h, t):
            return '{"status": 1, "request": "T"}'

        def _get(u, h, t):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("抖了一下")
            return '{"status": 1, "request": "TOK"}'

        monkeypatch.setattr(S, "_http_post", _post)
        monkeypatch.setattr(S, "_http_get", _get)
        tok = S.solve_recaptcha_token("SK", "https://x.com", "K",
                                      enabled=True, poll_interval=0.0,
                                      max_poll=3)
        assert tok == "TOK", "单次网络抖动不该让整个流程失败"

    def test_poll_timeout_forwarded(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 1, "request": "T"}'
        stub_http.get_replies = ['{"status": 1, "request": "TOK"}']
        S.solve_recaptcha_token("SK", "https://x.com", "K", enabled=True,
                                timeout=9.0, poll_interval=0.0)
        assert stub_http.posts[0][4] == 9.0
        assert stub_http.gets[0][4] == 9.0


# ==========================================================================
# 12. 失败路径（硬约束 #2：失败返回 None，不抛）
# ==========================================================================
class TestFailureNeverRaises:
    @pytest.mark.parametrize("exc", [
        OSError("网络断了"),
        TimeoutError("超时"),
        ValueError("奇奇怪怪"),
        RuntimeError("炸了"),
        urllib.error.URLError("dns"),
    ])
    def test_network_exceptions_become_none(self, monkeypatch, clean_env, exc):
        def _boom(u, b, h, t):
            raise exc
        monkeypatch.setattr(S, "_http_post", _boom)
        got = S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        assert got is None

    def test_detailed_records_exception_reason(self, monkeypatch, clean_env):
        def _boom(u, b, h, t):
            raise OSError("网络断了")
        monkeypatch.setattr(S, "_http_post", _boom)
        r = S.solve_captcha_detailed(
            "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True)
        assert r.ok is False
        assert "request_failed" in r.reason
        assert "OSError" in r.reason

    def test_recaptcha_submit_exception(self, monkeypatch, clean_env):
        def _boom(u, b, h, t):
            raise OSError("x")
        monkeypatch.setattr(S, "_http_post", _boom)
        assert S.solve_recaptcha_token("SK", "https://x.com", "K",
                                       enabled=True,
                                       poll_interval=0.0) is None

    def test_errors_list_gets_reason(self, stub_http, clean_env):
        stub_http.post_reply = '{"status": 0, "request": "ERROR_X"}'
        errs = []
        S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, errors=errs)
        assert errs and "solver" in errs[0]

    def test_errors_hook_exception_swallowed(self, stub_http, clean_env):
        """传进来的 errors 是个坏对象也不该崩 —— 打码路径要够皮实。"""
        class _BadList:
            def append(self, x):
                raise RuntimeError("列表坏了")
        stub_http.post_reply = '{"status": 0, "request": "ERROR_X"}'
        got = S.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, errors=_BadList())
        assert got is None


# ==========================================================================
# 13. SolveResult
# ==========================================================================
class TestSolveResult:
    def test_text_from_string(self):
        assert S.SolveResult(ok=True, solution="T").text == "T"

    def test_text_from_dict_grecaptcha(self):
        r = S.SolveResult(ok=True, solution={"gRecaptchaResponse": "T"})
        assert r.text == "T"

    def test_text_from_other_dict_key(self):
        r = S.SolveResult(ok=True, solution={"answer": "A"})
        assert r.text == "A"

    def test_text_empty_when_none(self):
        assert S.SolveResult().text == ""

    def test_to_dict_json_safe(self):
        r = S.SolveResult(ok=True, solution={"a": "b"})
        json.dumps(r.to_dict())

    def test_describe_ok_and_fail(self):
        assert "成功" in S.SolveResult(ok=True, solution="x").describe()
        assert "失败" in S.SolveResult(reason="y").describe()


# ==========================================================================
# 14. 不污染进程
# ==========================================================================
class TestProcessHygiene:
    def test_does_not_touch_global_sleep(self, monkeypatch, clean_env):
        """打桩的是 ``solver._sleep``，全局 ``time.sleep`` 必须原装。"""
        monkeypatch.setattr(S, "_http_post",
                            lambda u, b, h, t: '{"status": 1, "request": "T"}')
        monkeypatch.setattr(S, "_http_get",
                            lambda u, h, t: '{"status": 1, "request": "TOK"}')
        real = time.sleep
        S.solve_recaptcha_token("SK", "https://x.com", "K", enabled=True,
                                poll_interval=0.0)
        assert time.sleep is real

    def test_poll_interval_uses_module_alias(self, monkeypatch, clean_env):
        """轮询确实走了 ``_sleep``（这样测试才能不打桩全局）。"""
        seen = []
        monkeypatch.setattr(S, "_sleep", lambda s: seen.append(s))
        monkeypatch.setattr(S, "_http_post",
                            lambda u, b, h, t: '{"status": 1, "request": "T"}')
        monkeypatch.setattr(S, "_http_get",
                            lambda u, h, t: '{"status": 1, "request": "TOK"}')
        S.solve_recaptcha_token("SK", "https://x.com", "K", enabled=True,
                                poll_interval=0.25)
        assert seen and abs(seen[0] - 0.25) < 1e-9, seen

    def test_import_does_not_create_network_client(self):
        """导入本模块不该产生任何连接/线程 —— 它必须是个哑模块。"""
        import threading
        before = threading.active_count()
        import importlib
        importlib.reload(S)
        assert threading.active_count() == before
