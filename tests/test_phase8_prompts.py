# -*- coding: utf-8 -*-
"""阶段八 · Prompt 模板（``vision/prompts``）。

## 这个文件在守什么

prompt 是**契约** —— 输出格式只有一处定义，
改格式忘了改校验会让模型返回的坐标被当成屏幕坐标直接点。

所以这里逐条钉住 prompt 里的**四条硬约束**。
"""
from __future__ import annotations

import json

import pytest

from avatar.vision.prompts import (
    CONFIDENCE_MIN,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_prompt,
    build_user_prompt,
    validate_output,
)


class TestContract:
    def test_docstring_has_boundary(self):
        import avatar.vision.prompts as M
        assert "能力边界" in (M.__doc__ or "")

    def test_schema_five_fields(self):
        assert set(OUTPUT_SCHEMA["properties"]) == {
            "found", "x", "y", "confidence", "reason"}

    def test_schema_required_complete(self):
        assert set(OUTPUT_SCHEMA["required"]) == set(
            OUTPUT_SCHEMA["properties"])

    def test_confidence_min(self):
        assert CONFIDENCE_MIN == 0.5


class TestPromptConstraints:
    """★ 四条必须写进 prompt 的约束。"""

    def test_coordinate_system_declared(self):
        assert "像素坐标" in SYSTEM_PROMPT and "左上角" in SYSTEM_PROMPT

    def test_explicitly_not_screen_coords(self):
        """★★ 必须说清「不是屏幕坐标」—— 不然模型会按屏幕理解。"""
        assert "不是屏幕坐标" in SYSTEM_PROMPT

    def test_no_fabrication(self):
        assert "不要编造" in SYSTEM_PROMPT

    def test_found_false_is_legal(self):
        assert "found=false" in SYSTEM_PROMPT and "合法" in SYSTEM_PROMPT

    def test_no_markdown(self):
        assert "Markdown" in SYSTEM_PROMPT

    def test_confidence_must_be_number(self):
        assert "必须是数字" in SYSTEM_PROMPT and "high" in SYSTEM_PROMPT

    def test_range_declared(self):
        assert "0 ≤ x <" in SYSTEM_PROMPT

    def test_refuses_captcha(self):
        assert "验证码" in SYSTEM_PROMPT and "滑块" in SYSTEM_PROMPT

    def test_has_not_found_example(self):
        assert '"found": false' in SYSTEM_PROMPT

    def test_prompt_length_bounded(self):
        """★ 不做 few-shot —— prompt 太长会白烧 token。"""
        assert len(SYSTEM_PROMPT) < 2000


class TestUserTemplate:
    def test_placeholders(self):
        assert "{goal}" in USER_PROMPT_TEMPLATE
        assert "{width}" in USER_PROMPT_TEMPLATE
        assert "{height}" in USER_PROMPT_TEMPLATE

    def test_repeats_constraints(self):
        assert "像素坐标" in USER_PROMPT_TEMPLATE
        assert "found=false" in USER_PROMPT_TEMPLATE

    def test_build(self):
        got = build_user_prompt("确定", width=100, height=50)
        assert "确定" in got and "100" in got and "50" in got

    def test_empty_goal_placeholder(self):
        assert "（未说明目标）" in build_user_prompt("")

    def test_hint_appended(self):
        assert "右上角" in build_user_prompt("x", hint="它在右上角")

    def test_no_dangling_hint(self):
        assert not build_user_prompt("x").endswith("- ")


class TestBuildPrompt:
    class Img:
        width, height = 800, 600

    def test_returns_tuple(self):
        s, u = build_prompt("确定", self.Img())
        assert isinstance(s, str) and isinstance(u, str)

    def test_size_injected(self):
        """★ 尺寸写进 prompt 能减少越界坐标。"""
        _, u = build_prompt("确定", self.Img())
        assert "800" in u and "600" in u

    def test_default_system(self):
        s, _ = build_prompt("x", self.Img())
        assert s == SYSTEM_PROMPT

    def test_dict_info(self):
        _, u = build_prompt("x", {"width": 100, "height": 50})
        assert "100" in u

    def test_none_info(self):
        assert isinstance(build_prompt("x", None)[1], str)

    def test_custom_system(self):
        s, _ = build_prompt("x", None, system="自定义")
        assert s == "自定义"


class TestValidateOutput:
    GOOD = {"found": True, "x": 100, "y": 200, "confidence": 0.9,
            "reason": "按钮"}

    def test_good(self):
        assert validate_output(self.GOOD)[0] is True

    def test_not_found_passes(self):
        """★ found=false 时不校验坐标。"""
        assert validate_output({"found": False, "x": 0, "y": 0,
                                "confidence": 0.0,
                                "reason": "没有"})[0] is True

    @pytest.mark.parametrize("bad,why", [
        (None, "None"),
        ("str", "字符串"),
        ({}, "空"),
        ({"found": True}, "缺字段"),
        ({"found": "true", "x": 1, "y": 2, "confidence": 0.5,
          "reason": ""}, "found 非 bool"),
        ({"found": True, "x": "1", "y": 2, "confidence": 0.5,
          "reason": ""}, "x 非数字"),
        ({"found": True, "x": 1.5, "y": 2, "confidence": 0.5,
          "reason": ""}, "x 非整数"),
        ({"found": True, "x": 1, "y": 2, "confidence": 1.5,
          "reason": ""}, "conf 越界"),
        ({"found": True, "x": 1, "y": 2, "confidence": "high",
          "reason": ""}, "conf 是形容词"),
        ({"found": True, "x": 1, "y": 2, "confidence": 0.5,
          "reason": 1}, "reason 非字符串"),
    ])
    def test_rejects(self, bad, why):
        ok, reason = validate_output(bad)
        assert ok is False and reason, why

    def test_bool_not_number(self):
        """★ True 不能当 x 用。"""
        assert validate_output({"found": True, "x": True, "y": 2,
                                "confidence": 0.5,
                                "reason": ""})[0] is False


class TestSchemaConsistency:
    def test_schema_fields_in_prompt(self):
        """★★ Schema 与 prompt 不能分叉。"""
        for k in OUTPUT_SCHEMA["properties"]:
            assert k in SYSTEM_PROMPT, k

    def test_types(self):
        assert OUTPUT_SCHEMA["properties"]["x"]["type"] == "integer"
        assert OUTPUT_SCHEMA["properties"]["confidence"]["type"] == "number"
        assert OUTPUT_SCHEMA["properties"]["found"]["type"] == "boolean"

    def test_json_safe(self):
        assert json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)


class TestNoNetwork:
    def test_no_network_import(self):
        import ast
        import avatar.vision.prompts as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & {"urllib", "requests", "PIL", "cv2"}), mods
