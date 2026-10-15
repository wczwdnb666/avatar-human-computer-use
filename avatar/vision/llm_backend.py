# -*- coding: utf-8 -*-
"""视觉大模型后端 —— 五家 provider 的图片定位适配。

## 与 :mod:`agentic_planner.llm_backend` 的分工

那个管**文字**（规划、拆解），这个管**图片**（在截图里找一个元素）。

**为什么不复用同一个**：两者的请求体结构完全不同 ——
图片要 base64 编码进 `content` 数组、要算图像 token、
要处理分辨率上限。硬塞进一个类会让两边都变难读。

**共用的部分**：超时 / 重试 / 指数退避 / 脱敏 / 不可重试的错误码
—— 这些从 :mod:`agentic_planner.llm_backend` 借过来。

## 五家 provider，两套协议

```
                  ┌────────────────────────────┐
   统一请求  ────▶ │  VisionBackend（抽象）       │
   （JPEG + 目标） │   build_payload()          │
                  │   parse_response()         │
                  └───┬──────────────┬─────────┘
                      │              │
            ┌─────────▼────┐  ┌──────▼──────────┐
            │ OpenAI 兼容   │  │ Anthropic       │
            │ GPT-4o        │  │ Claude          │
            │ DeepSeek-VL   │  │                 │
            │ Kimi / Qwen-VL│  │                 │
            │ GLM-4V        │  │                 │
            └───────────────┘  └─────────────────┘
```

**图片格式**：两家都用 base64 data URL，但**字段位置不同**：

- OpenAI：`{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}`
- Anthropic：`{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"..."}}`

## 能力边界

**做**：构造两家的图片请求体与请求头；解析两家响应；
把返回的 JSON 归一化成统一的 :class:`VisionResult`；
超时 + 重试 + 脱敏；**永不抛异常**（失败返回带 reason 的结果）。

**不做**：

- **不引入任何 SDK**。只用 ``urllib.request``。
- **不做图像处理**。压缩 / 裁剪在 :mod:`avatar.vision.image_prep`。
- **不做坐标校验**。这是 :mod:`avatar.vision.validator` 的事 ——
  而且**必须**做（大模型会编造坐标）。
- **不做成本控制**。预算在 :mod:`avatar.vision.cost`。
- **不重试不可重试的错误**。401/403/404 重试只是浪费配额。
- **不缓存**。缓存要基于截图 hash，那是 :mod:`avatar.vision.cost` 的事。
"""

from __future__ import annotations

import abc
import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..agentic_planner.llm_backend import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    RETRYABLE_STATUS,
    _default_http_post,
    _redact,
)

__all__ = [
    "DEFAULT_VISION_TIMEOUT",
    "DEFAULT_VISION_MAX_RETRIES",
    "VISION_PROVIDERS",
    "VISION_PRESETS",
    "VisionError",
    "VisionResult",
    "VisionBackend",
    "OpenAICompatibleVisionBackend",
    "AnthropicVisionBackend",
    "normalize_image",
    "get_vision_backend",
    "describe_vision_backends",
    "selfcheck",
]

#: 默认超时。**比文字请求长** —— 图片上传 + 视觉推理本来就慢。
DEFAULT_VISION_TIMEOUT = 60.0

#: 默认重试次数（硬约束 3）。
DEFAULT_VISION_MAX_RETRIES = 3

# ---- provider ----
VISION_PROVIDER_OPENAI = "openai"
VISION_PROVIDER_ANTHROPIC = "anthropic"
VISION_PROVIDER_DEEPSEEK = "deepseek"
VISION_PROVIDER_KIMI = "kimi"
VISION_PROVIDER_QWEN = "qwen"
VISION_PROVIDER_GLM = "glm"

VISION_PROVIDERS = (VISION_PROVIDER_OPENAI, VISION_PROVIDER_ANTHROPIC,
                    VISION_PROVIDER_DEEPSEEK, VISION_PROVIDER_KIMI,
                    VISION_PROVIDER_QWEN, VISION_PROVIDER_GLM)

#: 厂商预设。**只有 base_url 和默认模型不同** ——
#: 六家里五家都是 OpenAI 兼容协议。
VISION_PRESETS: Dict[str, Dict[str, str]] = {
    VISION_PROVIDER_OPENAI: {
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "protocol": "openai",
    },
    VISION_PROVIDER_DEEPSEEK: {
        # DeepSeek 的视觉模型走 OpenAI 兼容格式
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-vl2",
        "protocol": "openai",
    },
    VISION_PROVIDER_KIMI: {
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k-vision-preview",
        "protocol": "openai",
    },
    VISION_PROVIDER_QWEN: {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/"
                    "chat/completions",
        "model": "qwen-vl-plus",
        "protocol": "openai",
    },
    VISION_PROVIDER_GLM: {
        "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4v",
        "protocol": "openai",
    },
    VISION_PROVIDER_ANTHROPIC: {
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-20241022",
        "protocol": "anthropic",
    },
}

#: 支持哪些 MIME。**JPEG 优先**（体积最小），PNG 兜底（无损）。
SUPPORTED_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif")


class VisionError(Exception):
    """视觉后端调用失败。**带是否可重试。**"""

    def __init__(self, message: str, *, status: int = 0,
                 retryable: bool = False, provider: str = ""):
        super().__init__(message)
        self.status = int(status)
        self.retryable = bool(retryable)
        self.provider = str(provider or "")


@dataclass
class VisionResult:
    """一次视觉定位的**原始**结果（还没经过坐标校验）。

    ⚠ **拿到它不能直接用来点击** —— 必须过
    :class:`avatar.vision.validator.CoordinateValidator`。
    大模型会编造坐标，这是已知事实。
    """

    ok: bool = False
    #: 图像**像素**坐标（不是屏幕坐标！）
    x: int = 0
    y: int = 0
    found: bool = False
    confidence: float = 0.0
    reason: str = ""
    #: 原始文本（排查用）
    text: str = ""
    #: 用量
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 第几次尝试才成功
    attempts: int = 0
    elapsed: float = 0.0
    provider: str = ""
    model: str = ""
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok), "found": bool(self.found),
            "x": int(self.x), "y": int(self.y),
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason[:200], "provider": self.provider,
            "model": self.model, "tokens": self.total_tokens,
            "attempts": self.attempts, "elapsed": round(self.elapsed, 3),
        }

    def describe(self) -> str:
        if not self.ok:
            return "✗ 视觉调用失败：%s" % (self.reason or "未知")
        if not self.found:
            return "· 模型说没找到：%s" % (self.reason or "无说明")
        return ("✓ 模型给出 (%d, %d) conf=%.2f（%d tokens，%.1fs）"
                % (self.x, self.y, self.confidence, self.total_tokens,
                   self.elapsed))


# --------------------------------------------------------------------------
# 图片规范化
# --------------------------------------------------------------------------
def normalize_image(data: Any, mime: str = "image/jpeg",
                    *, protocol: str = "openai") -> Dict[str, Any]:
    """把图片字节规范化成指定协议要求的格式。

    ## 两家格式

    OpenAI（``image_url``）::

        {"type": "image_url",
         "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}

    Anthropic（``image`` + ``source``）::

        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": "/9j/4AAQ..."}}

    差异是"base64 包不包在 data URL 里"。**写错了模型会看不到图，
    而且不报错** —— 它只会说"图里没找到"。所以转换集中在这里做。
    """
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise VisionError("图片数据必须是 bytes 或 str（base64），收到 %s"
                          % type(data).__name__)

    m = str(mime or "image/jpeg").strip().lower()
    if m not in SUPPORTED_MIME:
        raise VisionError("不支持的图片格式：%r（支持 %s）"
                          % (mime, " / ".join(SUPPORTED_MIME)))

    # 已经是 base64 就直接用；否则编码
    try:
        text = raw.decode("ascii")
        base64.b64decode(text, validate=True)
        b64 = text
    except Exception:      # noqa: BLE001
        b64 = base64.b64encode(raw).decode("ascii")

    if protocol == "anthropic":
        return {"type": "image",
                "source": {"type": "base64", "media_type": m,
                           "data": b64}}
    return {"type": "image_url",
            "image_url": {"url": "data:%s;base64,%s" % (m, b64)}}


def _image_bytes_len(image_part: Dict[str, Any]) -> int:
    """估一下 base64 的长度（诊断用）。"""
    try:
        if "image_url" in image_part:
            url = image_part["image_url"]["url"]
            return len(url.split(",", 1)[-1])
        return len(image_part["source"]["data"])
    except Exception:      # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
# 抽象后端
# --------------------------------------------------------------------------
class VisionBackend(abc.ABC):
    """视觉后端的接口。**两个抽象方法：构造请求 / 解析响应。**"""

    name: str = "abstract"
    protocol: str = "openai"

    def __init__(self, *,
                 api_key: str = "",
                 base_url: str = "",
                 model: str = "",
                 timeout: float = DEFAULT_VISION_TIMEOUT,
                 max_retries: int = DEFAULT_VISION_MAX_RETRIES,
                 enabled: bool = False,
                 http_post: Optional[Callable] = None,
                 sleep: Optional[Callable] = None) -> None:
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "")
        self.model = str(model or "")
        self.timeout = float(timeout or DEFAULT_VISION_TIMEOUT)
        self.max_retries = max(1, int(max_retries))
        #: ★ 硬约束 2：**默认关闭**，必须显式 enabled=True + api_key
        self.enabled = bool(enabled)
        self._http_post = http_post
        self._sleep = sleep or time.sleep

    @property
    def active(self) -> bool:
        """真正可用吗？**必须 enabled + api_key + base_url + model。**"""
        return bool(self.enabled and self.api_key and self.base_url
                    and self.model)

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "视觉后端未启用（需要 enabled=True）"
        if not self.api_key:
            return "没有 api_key"
        if not self.base_url:
            return "没有 base_url"
        if not self.model:
            return "没有 model"
        return ""

    # ---------------- 抽象 ----------------
    @abc.abstractmethod
    def build_payload(self, image_part: Dict[str, Any],
                      system: str, user: str, **kw: Any) -> Dict[str, Any]:
        """构造请求体。"""
        raise NotImplementedError

    @abc.abstractmethod
    def build_headers(self) -> Dict[str, str]:
        """构造请求头。**必须带 User-Agent（硬约束 3）。**"""
        raise NotImplementedError

    @abc.abstractmethod
    def parse_response(self, raw: Any) -> VisionResult:
        """解析响应，归一化成 :class:`VisionResult`。"""
        raise NotImplementedError

    # ---------------- 主入口 ----------------
    def locate(self, image: Any, goal: str, *,
               system: str = "", user: str = "",
               mime: str = "image/jpeg",
               timeout: Optional[float] = None,
               **kw: Any) -> VisionResult:
        """在图片里找 ``goal`` 描述的元素。**永不抛异常。**

        ``image`` 可以是 bytes / base64 字符串。

        ## 流程

        1. 检查是否启用 → 没启用直接返回失败
        2. 规范化图片
        3. 构造请求
        4. 发（带超时）
        5. 失败且可重试 → 指数退避后重试
        6. 解析响应
        """
        t0 = time.perf_counter()
        res = VisionResult(provider=self.name, model=self.model)

        if not self.active:
            res.reason = self.unavailable_reason()
            res.elapsed = time.perf_counter() - t0
            return res

        try:
            img = normalize_image(image, mime, protocol=self.protocol)
        except VisionError as exc:
            res.reason = str(exc)
            res.elapsed = time.perf_counter() - t0
            return res

        payload = self.build_payload(img, system, user, **kw)
        headers = self.build_headers()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        to = float(timeout if timeout else self.timeout)

        last = ""
        for attempt in range(1, self.max_retries + 1):
            res.attempts = attempt
            try:
                raw = self._post(self.base_url, body, headers, to)
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                detail = _read_err_body(exc)
                last = "HTTP %d：%s" % (status, detail[:160])
                if status not in RETRYABLE_STATUS:
                    res.reason = last
                    res.elapsed = time.perf_counter() - t0
                    return res
            except Exception as exc:      # noqa: BLE001
                last = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            else:
                parsed = self.parse_response(raw)
                parsed.provider = self.name
                parsed.model = self.model
                parsed.attempts = attempt
                parsed.elapsed = time.perf_counter() - t0
                return parsed

            if attempt < self.max_retries:
                self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))

        res.reason = "重试 %d 次仍失败：%s" % (self.max_retries, last)
        res.elapsed = time.perf_counter() - t0
        return res

    def _post(self, url: str, body: bytes, headers: Dict[str, str],
              timeout: float) -> str:
        fn = self._http_post or _default_http_post
        return fn(url, body, headers, timeout)

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        state = "已启用" if self.active else "未启用（默认）"
        return "\n".join([
            "%s：%s" % (type(self).__name__, state),
            "  protocol : %s" % self.protocol,
            "  base_url : %s" % (self.base_url or "(未设置)"),
            "  model    : %s" % (self.model or "(未设置)"),
            "  api_key  : %s" % _redact(self.api_key),
            "  timeout  : %.1fs  重试 %d 次" % (self.timeout,
                                               self.max_retries),
        ] + (["  原因     : %s" % self.unavailable_reason()]
             if not self.active else []))


def _read_err_body(exc: Any) -> str:
    try:
        raw = exc.read()
        return raw.decode("utf-8", "replace") if raw else ""
    except Exception:      # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# OpenAI 兼容（GPT-4o / DeepSeek-VL / Kimi / Qwen-VL / GLM-4V）
# --------------------------------------------------------------------------
class OpenAICompatibleVisionBackend(VisionBackend):
    """OpenAI 的 ``chat/completions`` 协议（带图片）。

    **六家里五家用这套** —— 图片放在 ``content`` 数组里的
    ``image_url`` 项，URL 是 base64 data URL。
    """

    name = "openai"
    protocol = "openai"

    def build_payload(self, image_part: Dict[str, Any],
                      system: str, user: str, **kw: Any
                      ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        # ★ 图片在前、文字在后 —— 两家的示例都这么写，
        #   而且有些兼容层对顺序敏感
        messages.append({
            "role": "user",
            "content": [image_part,
                        {"type": "text", "text": user or ""}],
        })

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # ★ 视觉定位要确定性 —— 温度必须 0
            "temperature": 0.0,
        }
        # 要求 JSON 输出（有些兼容层支持，不支持会被忽略）
        if kw.pop("json_mode", True):
            payload["response_format"] = {"type": "json_object"}
        if kw.get("max_tokens"):
            payload["max_tokens"] = int(kw["max_tokens"])
        return payload

    def build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.api_key,
            # ★ 硬约束 3：必须带 User-Agent
            "User-Agent": "avatar-human-computer-use/1.0 "
                          "(+vision.llm_backend)",
        }

    def parse_response(self, raw: Any) -> VisionResult:
        res = VisionResult(raw=raw)
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:      # noqa: BLE001
            res.reason = "响应不是合法 JSON：%s" % type(exc).__name__
            return res
        if not isinstance(d, dict):
            res.reason = "响应不是对象"
            return res

        if d.get("error"):
            err = d["error"]
            res.reason = "服务端错误：%s" % (
                err.get("message") if isinstance(err, dict) else str(err))
            return res

        usage = d.get("usage") or {}
        res.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        res.completion_tokens = int(usage.get("completion_tokens") or 0)

        choices = d.get("choices") or []
        if not choices:
            res.reason = "响应里没有 choices"
            return res
        msg = (choices[0] or {}).get("message") or {}
        res.text = str(msg.get("content") or "")
        if not res.text:
            res.reason = "响应里没有文本内容"
            return res

        return _parse_vision_json(res.text, res)


# --------------------------------------------------------------------------
# Anthropic（Claude）
# --------------------------------------------------------------------------
class AnthropicVisionBackend(VisionBackend):
    """Claude 的 ``messages`` 协议（带图片）。

    ## 与 OpenAI 的三处差异

    1. **系统提示是顶层字段** ``system``，不在 ``messages`` 里
    2. **图片用 ``source``** 而不是 ``image_url``（base64 不包 data URL）
    3. **认证头是 ``x-api-key``**，外加 ``anthropic-version``
    """

    name = "anthropic"
    protocol = "anthropic"

    #: Anthropic 要求显式声明 API 版本
    API_VERSION = "2023-06-01"

    def build_payload(self, image_part: Dict[str, Any],
                      system: str, user: str, **kw: Any
                      ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [image_part,
                            {"type": "text", "text": user or ""}],
            }],
            "temperature": 0.0,
            "max_tokens": int(kw.get("max_tokens") or 512),
        }
        if system:
            payload["system"] = system
        return payload

    def build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "User-Agent": "avatar-human-computer-use/1.0 "
                          "(+vision.llm_backend)",
        }

    def parse_response(self, raw: Any) -> VisionResult:
        res = VisionResult(raw=raw)
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:      # noqa: BLE001
            res.reason = "响应不是合法 JSON：%s" % type(exc).__name__
            return res
        if not isinstance(d, dict):
            res.reason = "响应不是对象"
            return res

        if d.get("type") == "error" or d.get("error"):
            err = d.get("error") or {}
            res.reason = "服务端错误：%s" % (
                err.get("message") if isinstance(err, dict) else str(err))
            return res

        usage = d.get("usage") or {}
        res.prompt_tokens = int(usage.get("input_tokens") or 0)
        res.completion_tokens = int(usage.get("output_tokens") or 0)

        texts = []
        for block in (d.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "") or ""))
        res.text = "\n".join(t for t in texts if t)
        if not res.text:
            res.reason = "响应里没有文本内容"
            return res

        return _parse_vision_json(res.text, res)


# --------------------------------------------------------------------------
# 解析模型返回的 JSON
# --------------------------------------------------------------------------
def _parse_vision_json(text: str, res: VisionResult) -> VisionResult:
    """从模型输出里挖出 ``{found, x, y, confidence, reason}``。

    ## 容错到什么程度

    - **剥 markdown 代码块** —— 即使 prompt 里禁止了，模型有时还是包
    - **从文本里找第一个 JSON 对象**
    - **字段类型不对就转换**（模型可能返回 ``"x": "120"``）
    - **缺 ``found`` 时按"没找到"处理**，不猜

    最后一条是刻意的：**猜"它可能是想说要"的代价是点错地方。**
    """
    d = _extract_json(text)
    if d is None:
        res.reason = "模型输出里没有合法 JSON"
        return res

    found = d.get("found")
    if isinstance(found, str):
        found = found.strip().lower() in ("true", "1", "yes", "是")
    res.found = bool(found)
    res.reason = str(d.get("reason", "") or "")[:300]

    if not res.found:
        # 没找到也是**成功的一次调用** —— 只是结论是"没有"
        res.ok = True
        return res

    try:
        res.x = int(float(d.get("x", 0)))
        res.y = int(float(d.get("y", 0)))
    except (TypeError, ValueError):
        res.reason = "坐标不是数字：x=%r y=%r" % (d.get("x"), d.get("y"))
        return res

    try:
        res.confidence = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        res.confidence = 0.0

    res.ok = True
    return res


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从文本里挖出第一个 JSON 对象。**挖不到返回 None。**"""
    t = str(text or "").strip()
    if not t:
        return None

    # 1) 直接就是 JSON
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except Exception:      # noqa: BLE001
        pass

    # 2) 剥 markdown 代码块
    for fence in ("```json", "```JSON", "```"):
        if fence in t:
            for chunk in t.split(fence)[1:]:
                body = chunk.split("```")[0].strip()
                try:
                    d = json.loads(body)
                    if isinstance(d, dict):
                        return d
                except Exception:      # noqa: BLE001
                    continue

    # 3) 找第一个 { ... } 配对
    start = t.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        d = json.loads(t[start:i + 1])
                        if isinstance(d, dict):
                            return d
                    except Exception:      # noqa: BLE001
                        pass
                    break
        start = t.find("{", start + 1)
    return None


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def get_vision_backend(provider: str = VISION_PROVIDER_OPENAI, *,
                       api_key: str = "",
                       base_url: str = "",
                       model: str = "",
                       enabled: bool = False,
                       timeout: float = DEFAULT_VISION_TIMEOUT,
                       max_retries: int = DEFAULT_VISION_MAX_RETRIES,
                       **kw: Any) -> VisionBackend:
    """按厂商名造一个视觉后端。

    **未知厂商抛 ValueError（不静默降级）。**
    """
    p = str(provider or VISION_PROVIDER_OPENAI).strip().lower()
    if p not in VISION_PRESETS:
        raise ValueError("未知的视觉厂商：%r（可用：%s）"
                         % (provider, " / ".join(VISION_PROVIDERS)))

    preset = VISION_PRESETS[p]
    cls = (AnthropicVisionBackend if preset["protocol"] == "anthropic"
           else OpenAICompatibleVisionBackend)
    return cls(api_key=api_key,
               base_url=base_url or preset["base_url"],
               model=model or preset["model"],
               enabled=enabled, timeout=timeout, max_retries=max_retries,
               **kw)


def describe_vision_backends() -> str:
    """给人看的视觉后端说明。"""
    lines = ["视觉大模型后端（零 SDK，纯 urllib）", "=" * 64, ""]
    lines.append("%-10s %-32s %s" % ("厂商", "默认模型", "协议"))
    lines.append("-" * 64)
    for p in VISION_PROVIDERS:
        pre = VISION_PRESETS[p]
        lines.append("%-10s %-32s %s"
                     % (p, pre["model"], pre["protocol"]))
    lines += [
        "",
        "★ 默认关闭 —— 必须 enabled=True 且 api_key 非空才启用。",
        "  理由：视觉调用**贵**（一张图几千 token），且是隐私外发",
        "  （会把你的屏幕截图发出去）。",
        "",
        "三条硬约束：",
        "  · 超时 %.0fs（图片上传 + 视觉推理本来就慢）"
        % DEFAULT_VISION_TIMEOUT,
        "  · 重试 %d 次，指数退避，只重试 429/5xx"
        % DEFAULT_VISION_MAX_RETRIES,
        "  · 请求必带 User-Agent",
        "",
        "⚠ 拿到 VisionResult **不能直接点击** ——",
        "  必须过 CoordinateValidator 的四道闸门（大模型会编造坐标）。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _FakeHTTP:
    def __init__(self, reply: str = "", exc: Any = None,
                 replies: Optional[List[Any]] = None) -> None:
        self.calls: List[Tuple[str, bytes, Dict[str, str], float]] = []
        self.reply = reply
        self.exc = exc
        self.replies = list(replies or [])

    def __call__(self, url: str, body: bytes, headers: Dict[str, str],
                 timeout: float) -> str:
        self.calls.append((url, body, headers, timeout))
        if self.exc is not None:
            raise self.exc
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        return self.reply


_OK_JSON = json.dumps({
    "choices": [{"message": {"content": json.dumps({
        "found": True, "x": 100, "y": 200, "confidence": 0.92,
        "reason": "蓝色的确定按钮"})}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 30}})

_NOSLEEP = lambda s: None
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"


def selfcheck(quick: bool = True) -> dict:
    """llm_backend 自检。**全程假 HTTP，一个字节都不出网。**"""
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
    _add("★ 六家厂商预设齐全", len(VISION_PRESETS) == 6,
         " / ".join(VISION_PROVIDERS))
    _add("★ 五家走 OpenAI 兼容",
         sum(1 for p in VISION_PRESETS.values()
             if p["protocol"] == "openai") == 5, "openai ×5")
    _add("★ Anthropic 单独一套协议",
         VISION_PRESETS[VISION_PROVIDER_ANTHROPIC]["protocol"]
         == "anthropic", "anthropic")
    _add("默认超时 60s", DEFAULT_VISION_TIMEOUT == 60.0, "60s")
    _add("默认重试 3 次", DEFAULT_VISION_MAX_RETRIES == 3, "3")

    # ============ 2) ★ 默认关闭（硬约束 2）============
    b = OpenAICompatibleVisionBackend()
    _add("★★ 默认未启用", b.active is False, "关")
    _add("★★ 只有 enabled 没有 key → 不可用",
         OpenAICompatibleVisionBackend(
             enabled=True, base_url="https://x", model="m").active is False,
         "不可用")
    _add("★ 四个都齐才可用",
         OpenAICompatibleVisionBackend(
             enabled=True, api_key="k", base_url="https://x",
             model="m").active is True, "可用")
    _add("unavailable_reason 逐项说明",
         "未启用" in b.unavailable_reason()
         and "api_key" in OpenAICompatibleVisionBackend(
             enabled=True).unavailable_reason(), "逐项")

    # ---- 未启用时零请求 ----
    f0 = _FakeHTTP(reply=_OK_JSON)
    r = OpenAICompatibleVisionBackend(
        enabled=False, api_key="k", base_url="https://x", model="m",
        http_post=f0).locate(_JPEG, "确定按钮")
    _add("★★ 未启用时**零请求**", r.ok is False and f0.calls == [], "零请求")

    # ============ 3) 图片规范化 ============
    oa = normalize_image(_JPEG, "image/jpeg", protocol="openai")
    _add("★ OpenAI 格式：image_url + data URL",
         oa["type"] == "image_url"
         and oa["image_url"]["url"].startswith(
             "data:image/jpeg;base64,"), "对")
    an = normalize_image(_JPEG, "image/jpeg", protocol="anthropic")
    _add("★ Anthropic 格式：image + source（无 data URL）",
         an["type"] == "image"
         and an["source"]["type"] == "base64"
         and an["source"]["media_type"] == "image/jpeg"
         and not an["source"]["data"].startswith("data:"), "对")

    # 已 base64 的输入不重复编码
    import base64 as _b64
    pre = _b64.b64encode(_JPEG).decode()
    oa2 = normalize_image(pre, "image/jpeg", protocol="openai")
    _add("★ 已 base64 的输入不重复编码",
         oa2["image_url"]["url"].endswith(pre), "对")

    _add("不支持的格式抛 VisionError",
         _raise_type(lambda: normalize_image(_JPEG, "image/tiff"))
         == "VisionError", "抛了")
    _add("非法类型抛 VisionError",
         _raise_type(lambda: normalize_image(12345)) == "VisionError",
         "抛了")
    for m in SUPPORTED_MIME:
        _add("支持格式：%s" % m,
             normalize_image(_JPEG, m)["type"] == "image_url", "对")

    # ============ 4) 请求构造 ============
    f = _FakeHTTP(reply=_OK_JSON)
    bk = OpenAICompatibleVisionBackend(
        enabled=True, api_key="SECRET", base_url="https://api.x/v1/chat",
        model="m1", http_post=f, sleep=_NOSLEEP)
    r = bk.locate(_JPEG, "确定按钮", system="SYS", user="USER")
    _add("★ 成功返回坐标", r.ok and r.found and r.x == 100 and r.y == 200,
         r.describe())
    _add("★ 置信度被解析", abs(r.confidence - 0.92) < 1e-6,
         str(r.confidence))
    _add("usage 被记下", r.prompt_tokens == 1200, str(r.prompt_tokens))

    body = json.loads(f.calls[0][1].decode("utf-8"))
    _add("★ temperature=0（视觉定位要确定性）",
         body["temperature"] == 0.0, "0.0")
    _add("★ 消息里有图片项",
         any(c.get("type") == "image_url"
             for c in body["messages"][-1]["content"]), "有")
    _add("★ 图片在前、文字在后",
         body["messages"][-1]["content"][0]["type"] == "image_url",
         "图在前")
    _add("system 单独一条消息",
         body["messages"][0]["role"] == "system", "对")
    _add("★ 请求带 timeout（硬约束 3）",
         f.calls[0][3] == DEFAULT_VISION_TIMEOUT, str(f.calls[0][3]))
    _add("★ 请求带 User-Agent（硬约束 3）",
         bool(f.calls[0][2].get("User-Agent")), "有")
    _add("请求带 Authorization",
         f.calls[0][2].get("Authorization", "").startswith("Bearer "), "有")
    _add("★ api_key 不在 URL 里",
         "SECRET" not in f.calls[0][0], "URL 干净")

    # ---- Anthropic ----
    fa = _FakeHTTP(reply=json.dumps({
        "content": [{"type": "text", "text": json.dumps({
            "found": True, "x": 5, "y": 6, "confidence": 0.8,
            "reason": "ok"})}],
        "usage": {"input_tokens": 900, "output_tokens": 20}}))
    ab = AnthropicVisionBackend(
        enabled=True, api_key="K", base_url="https://api.anthropic.com/v1/"
        "messages", model="claude", http_post=fa, sleep=_NOSLEEP)
    ra = ab.locate(_JPEG, "确定", system="SYS", user="U")
    _add("★ Anthropic 响应能解析",
         ra.ok and ra.x == 5 and ra.prompt_tokens == 900, ra.describe())
    ab_body = json.loads(fa.calls[0][1].decode("utf-8"))
    _add("★ Anthropic：system 提到顶层",
         ab_body.get("system") == "SYS"
         and all(m["role"] != "system" for m in ab_body["messages"]), "对")
    _add("★ Anthropic：图片用 source 格式",
         ab_body["messages"][0]["content"][0]["type"] == "image", "对")
    _add("Anthropic 头用 x-api-key",
         fa.calls[0][2].get("x-api-key") == "K", "对")
    _add("Anthropic 头带 anthropic-version",
         bool(fa.calls[0][2].get("anthropic-version")), "有")

    # ============ 5) 响应解析容错 ============
    for text, why in (
        ('```json\n{"found":true,"x":1,"y":2,"confidence":0.9}\n```',
         "剥 markdown"),
        ('好的：{"found":true,"x":1,"y":2,"confidence":0.9}',
         "从文本挖 JSON"),
        ('{"found":true,"x":"3","y":"4","confidence":"0.7"}',
         "字符串数字"),
        ('{"found":false,"reason":"图里没有"}', "found=false"),
    ):
        got = bk.parse_response(json.dumps(
            {"choices": [{"message": {"content": text}}]}))
        _add("★ 解析容错：%s" % why, got.ok, got.reason or "成功")

    not_found = bk.parse_response(json.dumps({"choices": [
        {"message": {"content": '{"found":false,"reason":"没有"}'}}]}))
    # ⚠ found=False 时 ok 仍为 True —— "没找到"是**一次成功的调用**，
    #   只是结论是"没有"。两者必须分开，否则调用方分不清
    #   "模型说没有"和"调用失败了"。
    _add("★ found=false 也算调用成功（结论是「没有」）",
         not_found.ok is True and not_found.found is False,
         "ok=True found=False")

    for bad, why in (("", "空"), ("不是 JSON", "非 JSON"),
                     ('{"found":true,"x":"abc","y":1}', "坐标非数字")):
        got = bk.parse_response(json.dumps(
            {"choices": [{"message": {"content": bad}}]}))
        _add("★ 解析拒绝：%s（且给原因）" % why,
             got.ok is False and bool(got.reason), got.reason[:40])

    _add("坏 JSON 不抛",
         bk.parse_response("{坏的").ok is False, "失败")
    _add("没有 choices 不抛",
         bk.parse_response('{"choices":[]}').ok is False, "失败")
    _add("服务端错误放 200 里也能识别",
         bk.parse_response('{"error":{"message":"x"}}').ok is False, "失败")

    # ============ 6) 重试（硬约束 3）============
    f3 = _FakeHTTP(replies=[
        urllib.error.HTTPError("u", 503, "busy", {}, None),
        urllib.error.HTTPError("u", 429, "slow", {}, None),
        _OK_JSON])
    b3 = OpenAICompatibleVisionBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=f3, sleep=_NOSLEEP, max_retries=3)
    r3 = b3.locate(_JPEG, "x")
    _add("★★ 503→429→成功：重试 3 次", r3.ok and r3.attempts == 3,
         r3.describe())

    f4 = _FakeHTTP(exc=urllib.error.HTTPError("u", 401, "bad", {}, None))
    b4 = OpenAICompatibleVisionBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=f4, sleep=_NOSLEEP, max_retries=3)
    _add("★★ 401 **不重试**",
         b4.locate(_JPEG, "x").ok is False and len(f4.calls) == 1,
         "%d 次" % len(f4.calls))

    slept: List[float] = []
    f5 = _FakeHTTP(exc=urllib.error.URLError("down"))
    b5 = OpenAICompatibleVisionBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=f5, sleep=lambda s: slept.append(s), max_retries=3)
    b5.locate(_JPEG, "x")
    _add("★ 指数退避递增且封顶 8s",
         len(slept) == 2 and slept[1] > slept[0]
         and all(s <= 8.0 for s in slept), str(slept))

    # ============ 7) 工厂 ============
    _add("★ 未知厂商抛 ValueError",
         _raise_type(lambda: get_vision_backend("gpt5")) == "ValueError",
         "抛了")
    for p in VISION_PROVIDERS:
        bk2 = get_vision_backend(p)
        _add("工厂能造：%s" % p, bk2 is not None and bk2.active is False,
             type(bk2).__name__)
    _add("★ 工厂造出来的默认关闭（硬约束 2）",
         all(get_vision_backend(p).active is False
             for p in VISION_PROVIDERS), "全部关")
    _add("anthropic 走 AnthropicVisionBackend",
         isinstance(get_vision_backend("anthropic"),
                    AnthropicVisionBackend), "对")

    # ============ 8) 脱敏 ============
    bk6 = get_vision_backend("openai", api_key="sk-abcdefgh12345678",
                             enabled=True)
    _add("★ describe 脱敏 api_key",
         "sk-abcdefgh12345678" not in bk6.describe(), "已脱敏")

    # ============ 9) 展示 ============
    _add("describe_vision_backends 不抛",
         isinstance(_me.describe_vision_backends(), str), "有")
    _add("describe 列出六家",
         all(p in _me.describe_vision_backends()
             for p in VISION_PROVIDERS), "有")
    _add("VisionResult.describe 可读",
         "模型给出" in VisionResult(ok=True, found=True, x=1, y=2,
                                    confidence=0.9).describe(), "有")

    # ============ 10) 零 SDK ============
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
    _add("★★ 不 import 任何 SDK（含 openai / anthropic）",
         not (mods & {"openai", "anthropic", "requests", "httpx",
                      "aiohttp", "PIL"}),
         sorted(mods))
    _add("★ 只用 urllib（标准库）", "urllib" in mods, "urllib")

    elapsed = time.perf_counter() - t0
    return {"module": "vision.llm_backend", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_vision_backends())
