# -*- coding: utf-8 -*-
"""Prompt 模板 —— 让模型"找元素"而不是"聊天"。

## 为什么 prompt 要单独一个模块

因为它是**契约**。模型返回的 JSON 结构决定了后面三道校验怎么写，
而校验写错了会**点错地方**。所以：

- 输出格式必须**只有一处定义**（:data:`OUTPUT_SCHEMA`）
- 坐标系声明必须**明确**（图像像素 ≠ 屏幕坐标）
- 禁止编造必须**写死**（找不到就返回 found=false）

散在各处写 prompt 的结果是：改了格式忘了改校验，
然后模型返回的坐标被当成屏幕坐标直接点 —— **偏几百像素**。

## 四个必须写进 prompt 的约束

1. **坐标系**：坐标是**图像像素**，原点在左上角
2. **不许编造**：找不到必须返回 ``found=false``
3. **不许 Markdown**：只返回裸 JSON
4. **不许模糊**：confidence 是 0~1 的数，不是 "high" / "probably"

## 能力边界

**做**：定义系统提示 / 用户提示模板 / 输出 JSON Schema；
把目标描述与图像信息填进模板；给一份人读的 prompt 预览。

**不做**：

- **不调用模型**。构造 prompt 是纯函数，调用在
  :mod:`avatar.vision.llm_backend`。
- **不解析响应**。解析在 backend 的 ``parse_response``。
- **不校验坐标**。校验在 :mod:`avatar.vision.validator`。
- **不做多语言 prompt**。**统一用中文** ——
  这是给中文用户的项目，而且实测中文 prompt 在国产模型上更稳。
  需要英文请直接传 ``system=`` / ``user=`` 覆盖。
- **不做 few-shot 示例**。示例会让 prompt 变长（每个示例几百 token），
  而本层的定位任务**结构简单**，一个 JSON Schema 就够了。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "OUTPUT_SCHEMA",
    "CONFIDENCE_MIN",
    "build_prompt",
    "build_user_prompt",
    "describe_prompts",
    "validate_output",
    "selfcheck",
]

#: 系统提示词。**固定不变** —— 改了要同步改 OUTPUT_SCHEMA 与 validator。
#:
#: ## 写法上的取舍
#:
#: - **用中文**：国产模型（DeepSeek / Kimi / Qwen / GLM）上更稳，
#:   GPT/Claude 也能正确处理
#: - **明确坐标系**：不说清楚的话模型会按"屏幕坐标"理解，
#:   而截图可能被缩放过 —— 那就偏了
#: - **明确禁止编造**：这是视觉定位最大的风险。
#:   模型倾向于"给个答案"，哪怕它其实没看见
#: - **强调 found=false 是合法答案**：不这么说的话，
#:   模型会把"没找到"也硬报一个坐标
SYSTEM_PROMPT = """你是一个界面元素定位器。用户会给你一张屏幕截图和一个目标描述，\
你要在图中找到该元素并返回它的中心坐标。

## 输出格式

只返回一个 JSON 对象，不要任何其他内容：

{
  "found": true 或 false,
  "x": 整数,
  "y": 整数,
  "confidence": 0.0 到 1.0 的小数,
  "reason": "一句话说明你看到了什么"
}

## 坐标系（重要）

- x 和 y 是**这张图片的像素坐标**，原点在**左上角**
- x 向右增大，y 向下增大
- **不是屏幕坐标，也不是缩放前的坐标** —— 就是你现在看到的这张图的坐标
- 坐标必须落在图片范围内：0 ≤ x < 图片宽度，0 ≤ y < 图片高度

## 硬性要求

1. **找不到就返回 found=false**。这是完全合法的答案，不会被认为失败。
2. **绝对不要编造坐标**。如果你不确定，就把 confidence 调低或返回 found=false。
   一个错误的坐标会导致点击到错误的位置，比"没找到"危险得多。
3. **不要返回 Markdown 代码块**。不要用 ``` 包裹。直接输出裸 JSON。
4. **不要输出解释性文字**。除了那个 JSON 对象，什么都不要写。
5. **confidence 必须是数字**（0.0~1.0），不是 "high"、"可能"、"应该" 这类词。
   如果完全确定，用 0.95；如果只是猜测，用 0.3 以下。
6. **不要试图解题**。如果看到验证码、滑块、人机验证，
   直接返回 found=false —— 这类东西**不该由你处理**。

## 找不到时的输出

{
  "found": false,
  "x": 0,
  "y": 0,
  "confidence": 0.0,
  "reason": "图中没有看到目标元素"
}
"""

#: 用户提示词模板。``{goal}`` / ``{width}`` / ``{height}`` / ``{hint}``
USER_PROMPT_TEMPLATE = """在这张 {width}x{height} 的截图中找到：{goal}

注意：
- 坐标是**这张图**的像素坐标（原点左上角），范围 0≤x<{width}，0≤y<{height}
- 只返回 JSON，不要 Markdown 代码块，不要解释
- 找不到就返回 found=false{hint}"""

#: 输出 JSON Schema。**与 :data:`SYSTEM_PROMPT` 里的格式一一对应。**
#:
#: 这个 schema 有两个用途：
#: 1. 给人看"模型应该返回什么"
#: 2. :func:`validate_output` 用它做**结构校验**
OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {
            "type": "boolean",
            "description": "是否找到了目标元素",
        },
        "x": {
            "type": "integer",
            "description": "目标中心的 x 坐标（图像像素）",
        },
        "y": {
            "type": "integer",
            "description": "目标中心的 y 坐标（图像像素）",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "置信度 0.0~1.0",
        },
        "reason": {
            "type": "string",
            "description": "一句话说明",
        },
    },
    "required": ["found", "x", "y", "confidence", "reason"],
}

#: 置信度下限。**与 validator 的闸门 4 保持一致。**
#:
#: 0.5 的含义：模型自己说"不到一半的把握"就不要用。
#: 定得更低会让误报变多（错的位置比没位置危险）；
#: 定得更高会让这一层几乎永远失败（那还不如不接）。
CONFIDENCE_MIN = 0.5


def build_user_prompt(goal: str, *,
                      width: int = 0, height: int = 0,
                      hint: str = "") -> str:
    """填用户提示词。"""
    h = str(hint or "").strip()
    return USER_PROMPT_TEMPLATE.format(
        goal=str(goal or "").strip() or "（未说明目标）",
        width=int(width or 0),
        height=int(height or 0),
        hint=("\n- " + h) if h else "")


def build_prompt(goal: str, image_info: Any = None, *,
                 system: str = "", hint: str = "") -> Tuple[str, str]:
    """构造 ``(system, user)``。

    ``image_info`` 可以是 :class:`~avatar.vision.image_prep.PreparedImage`、
    dict，或任何有 ``width`` / ``height`` 的对象。

    **把图片尺寸写进 prompt 很重要** —— 模型知道边界后，
    越界坐标会明显减少（它自己会检查）。
    """
    w = h = 0
    if image_info is not None:
        if isinstance(image_info, dict):
            w = int(image_info.get("width", 0) or 0)
            h = int(image_info.get("height", 0) or 0)
        else:
            w = int(getattr(image_info, "width", 0) or 0)
            h = int(getattr(image_info, "height", 0) or 0)

    return (str(system) if system else SYSTEM_PROMPT,
            build_user_prompt(goal, width=w, height=h, hint=hint))


def validate_output(d: Any) -> Tuple[bool, str]:
    """按 :data:`OUTPUT_SCHEMA` 校验一个 dict。

    返回 ``(通过, 原因)``。

    **只做结构校验，不做坐标校验** —— 后者在
    :mod:`avatar.vision.validator`（那里要拿屏幕尺寸比对）。
    """
    if not isinstance(d, dict):
        return False, "不是对象（收到 %s）" % type(d).__name__

    missing = [k for k in OUTPUT_SCHEMA["required"] if k not in d]
    if missing:
        return False, "缺字段：%s" % "、".join(missing)

    if not isinstance(d.get("found"), bool):
        return False, "found 必须是布尔值（收到 %r）" % (d.get("found"),)

    if not d["found"]:
        # 没找到时坐标没有意义，不校验
        return True, ""

    for k in ("x", "y"):
        v = d.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, "%s 必须是数字（收到 %r）" % (k, v)
        if int(v) != v:
            return False, "%s 必须是整数（收到 %r）" % (k, v)

    c = d.get("confidence")
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        return False, "confidence 必须是数字（收到 %r）" % (c,)
    if not (0.0 <= float(c) <= 1.0):
        return False, "confidence 必须在 0~1（收到 %r）" % (c,)

    if not isinstance(d.get("reason"), str):
        return False, "reason 必须是字符串"

    return True, ""


def describe_prompts() -> str:
    """给人看的 prompt 说明。"""
    return "\n".join([
        "视觉定位 Prompt",
        "=" * 60,
        "系统提示 %d 字符，用户模板 %d 字符，Schema %d 个字段"
        % (len(SYSTEM_PROMPT), len(USER_PROMPT_TEMPLATE),
           len(OUTPUT_SCHEMA["properties"])),
        "",
        "四个必须写进 prompt 的约束：",
        "  1. 坐标系 —— 图像像素，原点左上角（**不是屏幕坐标**）",
        "  2. 不许编造 —— 找不到必须返回 found=false",
        "  3. 不许 Markdown —— 只返回裸 JSON",
        "  4. 不许模糊 —— confidence 是 0~1 的数，不是 high/probably",
        "",
        "置信度下限：%.2f（与 validator 的闸门 4 一致）" % CONFIDENCE_MIN,
        "",
        "★ 输出格式只有一处定义（OUTPUT_SCHEMA）——",
        "  改格式忘了改校验，会让模型返回的坐标被当成屏幕坐标直接点。",
        "",
        "★ 统一用中文 —— 国产模型上更稳，GPT/Claude 也能正确处理。",
        "  要英文请直接传 system= / user= 覆盖。",
        "",
        "★ 不做 few-shot 示例 —— 示例会让 prompt 变长几百 token，",
        "  而本层的任务结构简单，一个 Schema 就够了。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """prompts 自检。**纯字符串操作，不联网。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("置信度下限 0.5", CONFIDENCE_MIN == 0.5, "0.5")
    _add("Schema 五个字段", len(OUTPUT_SCHEMA["properties"]) == 5,
         " / ".join(OUTPUT_SCHEMA["properties"]))
    _add("Schema required 齐全",
         set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"]),
         str(OUTPUT_SCHEMA["required"]))

    # ============ 2) ★ prompt 里的四条约束 ============
    sp = SYSTEM_PROMPT
    _add("★★ 声明了坐标系（图像像素 + 左上角）",
         "像素坐标" in sp and "左上角" in sp, "有")
    _add("★★ 明确说「不是屏幕坐标」",
         "不是屏幕坐标" in sp, "有")
    _add("★★ 明确禁止编造",
         "不要编造" in sp, "有")
    _add("★★ 强调 found=false 是合法答案",
         "found=false" in sp and "合法" in sp, "有")
    _add("★★ 禁止 Markdown 代码块",
         "Markdown" in sp and "```" in sp, "有")
    _add("★ 要求 confidence 是数字不是形容词",
         "必须是数字" in sp and "high" in sp, "有")
    _add("★ 声明坐标范围 0≤x<宽",
         "0 ≤ x <" in sp, "有")
    _add("★ 声明不处理验证题",
         "验证码" in sp and "滑块" in sp, "有")
    _add("★ 给了找不到时的输出示例",
         '"found": false' in sp, "有")

    # ---- 用户模板 ----
    up = USER_PROMPT_TEMPLATE
    _add("用户模板含目标占位符", "{goal}" in up, "有")
    _add("用户模板含尺寸占位符",
         "{width}" in up and "{height}" in up, "有")
    _add("用户模板重复了坐标约束",
         "像素坐标" in up and "found=false" in up, "有")
    _add("用户模板也禁 Markdown", "Markdown" in up, "有")

    # ============ 3) build_user_prompt ============
    got = build_user_prompt("确定按钮", width=1280, height=720)
    _add("★ 目标被填进 prompt", "确定按钮" in got, got[:60])
    _add("★ 尺寸被填进 prompt",
         "1280" in got and "720" in got, "有")
    _add("无 hint 时不出现空行尾巴",
         not got.endswith("- "), repr(got[-20:]))
    got2 = build_user_prompt("x", width=10, height=10, hint="它在右上角")
    _add("hint 被追加", "它在右上角" in got2, "有")
    _add("★ 空目标不崩（用占位）",
         "（未说明目标）" in build_user_prompt(""), "占位")

    # ============ 4) build_prompt ============
    class _Img:
        width, height = 800, 600

    sysp, userp = build_prompt("确定", _Img())
    _add("★ build_prompt 返回二元组",
         isinstance(sysp, str) and isinstance(userp, str), "对")
    _add("★ 图片尺寸被写进 prompt（减少越界）",
         "800" in userp and "600" in userp, "有")
    _add("system 默认是 SYSTEM_PROMPT",
         sysp == SYSTEM_PROMPT, "对")

    _, u_dict = build_prompt("x", {"width": 100, "height": 50})
    _add("★ image_info 收 dict", "100" in u_dict, "有")
    _, u_none = build_prompt("x", None)
    _add("image_info=None 不崩", isinstance(u_none, str), "安全")
    _, u_custom = build_prompt("x", None, system="自定义")
    _add("system 可覆盖", u_custom is not None
         and build_prompt("x", None, system="自定义")[0] == "自定义", "对")

    # ============ 5) validate_output ============
    good = {"found": True, "x": 100, "y": 200, "confidence": 0.9,
            "reason": "蓝色按钮"}
    _add("★ 合法输出通过", validate_output(good)[0] is True, "通过")

    not_found = {"found": False, "x": 0, "y": 0, "confidence": 0.0,
                 "reason": "没有"}
    _add("★ found=false 也通过（坐标不校验）",
         validate_output(not_found)[0] is True, "通过")

    bad_cases = [
        (None, "None"),
        ("字符串", "字符串"),
        ({}, "空对象"),
        ({"found": True}, "缺字段"),
        ({"found": "true", "x": 1, "y": 2, "confidence": 0.5,
          "reason": ""}, "found 是字符串"),
        ({"found": True, "x": "100", "y": 2, "confidence": 0.5,
          "reason": ""}, "x 是字符串"),
        ({"found": True, "x": 1.5, "y": 2, "confidence": 0.5,
          "reason": ""}, "x 不是整数"),
        ({"found": True, "x": 1, "y": 2, "confidence": 1.5,
          "reason": ""}, "confidence 越界"),
        ({"found": True, "x": 1, "y": 2, "confidence": "high",
          "reason": ""}, "confidence 是形容词"),
        ({"found": True, "x": 1, "y": 2, "confidence": 0.5,
          "reason": 123}, "reason 不是字符串"),
    ]
    for bad, why in bad_cases:
        ok, why_not = validate_output(bad)
        _add("★ 拒绝：%s" % why, ok is False and bool(why_not),
             why_not[:40])

    _add("★ bool 不算数字（True 不能当 x）",
         validate_output({"found": True, "x": True, "y": 2,
                          "confidence": 0.5, "reason": ""})[0] is False,
         "拒了")

    # ============ 6) Schema 与 prompt 一致 ============
    _add("★★ Schema 的字段都在 prompt 的 JSON 示例里",
         all(k in SYSTEM_PROMPT for k in OUTPUT_SCHEMA["properties"]),
         "一致")
    _add("★ Schema 说 confidence 是 number",
         OUTPUT_SCHEMA["properties"]["confidence"]["type"] == "number",
         "number")
    _add("★ Schema 说 x/y 是 integer",
         OUTPUT_SCHEMA["properties"]["x"]["type"] == "integer"
         and OUTPUT_SCHEMA["properties"]["y"]["type"] == "integer",
         "integer")
    _add("Schema 可序列化",
         json.dumps(OUTPUT_SCHEMA, ensure_ascii=False) != "", "ok")

    # ============ 7) describe ============
    _add("describe_prompts 不抛",
         isinstance(_me.describe_prompts(), str), "有")
    _add("★ describe 说明「四条约束」",
         "四个必须写进" in _me.describe_prompts(), "有")

    # ============ 8) 不做多余的事 ============
    _add("★ prompt 里没有 few-shot 示例（省 token）",
         SYSTEM_PROMPT.count('"found"') <= 3,
         "%d 处 found" % SYSTEM_PROMPT.count('"found"'))
    _add("prompt 长度可控（< 2000 字符）",
         len(SYSTEM_PROMPT) < 2000, "%d 字符" % len(SYSTEM_PROMPT))

    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★ 不 import 任何网络/图像库（纯字符串）",
         not (mods & {"urllib", "requests", "PIL", "cv2"}),
         sorted(mods))

    elapsed = time.perf_counter() - t0
    return {"module": "vision.prompts", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_prompts())
