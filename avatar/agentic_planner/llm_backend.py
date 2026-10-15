# -*- coding: utf-8 -*-
"""LLM 后端 —— 多模型适配 + token 预算 + 重试。

## 这一层解决什么

阶段六的规划器是**规则驱动**的：能处理"打开记事本输入hello"，
处理不了"把这周的订单导出成表格"。后者需要真正的语言理解。

本模块提供"调大模型"的能力，把各家 API 的差异**收在一处**：

```
                    ┌──────────────────────────┐
   统一请求  ──────▶ │  LLMBackend（抽象）       │
   （messages +     │   build_payload()        │  各家格式不同
     tools）         │   parse_response()       │
                    └───┬──────────────┬───────┘
                        │              │
              ┌─────────▼────┐  ┌──────▼──────────┐
              │ OpenAI 兼容   │  │ Anthropic       │
              │ GPT/DeepSeek  │  │ Claude          │
              │ Kimi/Qwen     │  │                 │
              └───────────────┘  └─────────────────┘
```

调用方只说"给我补全这段对话"，不用知道对面是哪家。

## 三件事必须做对（硬约束 5 / 6）

1. **token 预算** —— 单次请求上限 + 单任务累计上限。
   超了**返回错误，不发请求**。不做这件事的后果是"账单失控"。
2. **超时** —— 默认 60 秒。不设超时的 HTTP 调用会**永久挂住**。
3. **重试** —— 最多 3 次，指数退避。但**只重试可重试的错误**
   （429 / 5xx / 网络抖动），4xx 参数错误重试一万次也没用。

## 能力边界

**做**：构造各家的请求体与请求头；解析各家的响应；
把工具 Schema 规范化成各家格式；token 预算控制；
超时与指数退避重试；**永不抛异常**（失败返回带 reason 的响应）。

**不做**：

- **不引入任何 SDK**。只用 ``urllib.request``。理由不只是"零依赖"——
  各家 SDK 的版本兼容是长期的维护负担，而 HTTP 协议十年没变过。
- **不做流式**。流式要处理 SSE 分片、增量解析、中途断流 ——
  而规划场景要的是"一次拿到完整 JSON"，流式没有收益。
- **不做多轮对话管理**。本模块收 messages、发 messages，
  不维护会话历史。历史是调用方的事（它才知道该记多少）。
- **不做成本估算的"智能"**。token 计数用**粗略估算**
  （字符数 / 4），因为精确计数要各家自己的 tokenizer ——
  那是另一个 SDK。估算偏保守（宁可少发，不可超支）。
- **不重试不可重试的错误**。401/403/404 重试只是浪费时间和配额。
- **不落盘任何 api_key**。日志、异常、``describe()`` 全部脱敏。
"""

from __future__ import annotations

import abc
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PER_REQUEST_TOKENS",
    "DEFAULT_TOTAL_TOKENS",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "PROVIDER_OPENAI",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_DEEPSEEK",
    "PROVIDER_KIMI",
    "PROVIDER_QWEN",
    "PROVIDERS",
    "PROVIDER_PRESETS",
    "LLMError",
    "TokenBudget",
    "LLMResponse",
    "ToolSpec",
    "LLMBackend",
    "OpenAICompatibleBackend",
    "AnthropicBackend",
    "get_backend",
    "estimate_tokens",
    "normalize_tools",
    "describe_llm",
    "selfcheck",
]

#: 默认超时（秒）。**必须设** —— 不设超时的 HTTP 会永久挂住。
DEFAULT_TIMEOUT = 60.0

#: 默认最大重试次数（硬约束 6）。
DEFAULT_MAX_RETRIES = 3

#: 单次请求的 token 上限（输入+输出）。超了**不发请求**。
DEFAULT_PER_REQUEST_TOKENS = 8000

#: 单任务的累计 token 上限。超了拒绝后续请求。
DEFAULT_TOTAL_TOKENS = 200_000

#: 环境变量名。
ENV_API_KEY = "AVATAR_LLM_API_KEY"
ENV_BASE_URL = "AVATAR_LLM_BASE_URL"
ENV_MODEL = "AVATAR_LLM_MODEL"
ENV_PROVIDER = "AVATAR_LLM_PROVIDER"

# ---- 厂商 ----
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_KIMI = "kimi"
PROVIDER_QWEN = "qwen"

PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_DEEPSEEK,
             PROVIDER_KIMI, PROVIDER_QWEN)

#: 厂商预设。**只有 base_url 和默认模型不同** ——
#: DeepSeek / Kimi / Qwen 都是 OpenAI 兼容协议，
#: 换一家只需要换这两项。这是"不做四套适配"的原因。
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    PROVIDER_OPENAI: {
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "protocol": "openai",
    },
    PROVIDER_DEEPSEEK: {
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "protocol": "openai",
    },
    PROVIDER_KIMI: {
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "protocol": "openai",
    },
    PROVIDER_QWEN: {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/"
                    "chat/completions",
        "model": "qwen-plus",
        "protocol": "openai",
    },
    PROVIDER_ANTHROPIC: {
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-20241022",
        "protocol": "anthropic",
    },
}

#: 可重试的 HTTP 状态码。
#:
#: - ``429`` 限流 —— 等一会儿再来
#: - ``5xx`` 服务端问题 —— 不是我们的错
#:
#: **4xx 不在这里** —— 参数错误重试一万次也还是错，
#: 只是浪费时间和配额。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(Exception):
    """LLM 调用失败。**带可读原因与是否可重试。**"""

    def __init__(self, message: str, *, status: int = 0,
                 retryable: bool = False, provider: str = ""):
        super().__init__(message)
        self.status = int(status)
        self.retryable = bool(retryable)
        self.provider = str(provider or "")


# --------------------------------------------------------------------------
# token 预算
# --------------------------------------------------------------------------
def estimate_tokens(text: Any) -> int:
    """**粗略**估算 token 数。

    ## 为什么是估算

    精确计数要各家自己的 tokenizer —— 那是另一个 SDK，违背"零依赖"。
    经验值：英文约 4 字符/token，中文约 1.5 字符/token。

    **估算偏保守**（宁可高估）：高估的代价是"少发几次"，
    低估的代价是"超支被服务端拒"或"账单超预期"。
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:      # noqa: BLE001
            text = str(text)

    cjk = 0
    other = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        else:
            other += 1
    # 中文 1.5 字符/token，其余 4 字符/token，各留一点余量
    return int(cjk / 1.4 + other / 3.6) + 1


class TokenBudget:
    """token 预算。**超预算直接拒绝，不发请求**（硬约束 5）。

    ## 为什么要两个上限

    - **单次上限**防"一次塞进去一本书"
    - **累计上限**防"一个循环调了一万次"

    只有单次上限的话，循环调用照样能把账单刷爆；
    只有累计上限的话，单次超大请求会在第一次就把预算耗尽。
    """

    def __init__(self, *, per_request: int = DEFAULT_PER_REQUEST_TOKENS,
                 total: int = DEFAULT_TOTAL_TOKENS) -> None:
        self.per_request = max(1, int(per_request))
        self.total = max(1, int(total))
        self.used = 0
        self.requests = 0
        self.rejected = 0

    def check(self, estimated: int) -> Tuple[bool, str]:
        """能不能发？返回 ``(可以, 原因)``。"""
        est = max(0, int(estimated))
        if est > self.per_request:
            self.rejected += 1
            return False, ("单次请求估算 %d tokens，超过上限 %d"
                           % (est, self.per_request))
        if self.used + est > self.total:
            self.rejected += 1
            return False, ("累计已用 %d + 本次 %d 超过总预算 %d"
                           % (self.used, est, self.total))
        return True, ""

    def consume(self, actual: int) -> int:
        """记账。返回累计用量。"""
        n = max(0, int(actual))
        self.used += n
        self.requests += 1
        return self.used

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def reset(self) -> None:
        self.used = 0
        self.requests = 0
        self.rejected = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"per_request": self.per_request, "total": self.total,
                "used": self.used, "remaining": self.remaining,
                "requests": self.requests, "rejected": self.rejected}


# --------------------------------------------------------------------------
# 响应
# --------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """一次补全的结果。**失败也返回它，不抛异常。**"""

    ok: bool = False
    text: str = ""
    #: 结构化的工具调用（如果模型返回了）
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    #: 用量
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 失败原因（ok=False 时有值）
    reason: str = ""
    #: 是否可重试
    retryable: bool = False
    #: 第几次尝试才成功（1 = 一次就成）
    attempts: int = 0
    elapsed: float = 0.0
    #: 原始响应，排查用
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok), "text": self.text[:500],
            "tool_calls": self.tool_calls[:5],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reason": self.reason, "attempts": self.attempts,
            "elapsed": round(self.elapsed, 3),
        }

    def describe(self) -> str:
        if self.ok:
            return ("✓ 补全成功（%d tokens，%.1fs，第 %d 次尝试）"
                    % (self.total_tokens, self.elapsed, self.attempts))
        return "✗ 补全失败：%s（%.1fs）" % (self.reason or "未知", self.elapsed)


@dataclass
class ToolSpec:
    """一个工具的声明。**规范化后的统一形态。**"""

    name: str = ""
    description: str = ""
    #: JSON Schema
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters or {"type": "object",
                                                  "properties": {}}}


def normalize_tools(tools: Sequence[Any], *,
                    protocol: str = "openai") -> List[Dict[str, Any]]:
    """把工具声明规范化成**指定协议**的格式。

    ## 两家格式的差异

    OpenAI 是"函数"包一层：

        {"type": "function", "function": {"name": ..., "parameters": ...}}

    Anthropic 直接就是工具本身，且要显式 ``input_schema``：

        {"name": ..., "description": ..., "input_schema": {...}}

    差异很小，但**写错了模型会完全忽略工具** —— 而且不报错。
    所以这个转换必须集中做，不能各家散着写。
    """
    out: List[Dict[str, Any]] = []
    for t in (tools or []):
        spec = _as_tool(t)
        if not spec.name:
            continue
        if protocol == "anthropic":
            out.append({
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters
                or {"type": "object", "properties": {}},
            })
        else:
            out.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters
                    or {"type": "object", "properties": {}},
                },
            })
    return out


def _as_tool(t: Any) -> ToolSpec:
    if isinstance(t, ToolSpec):
        return t
    if isinstance(t, dict):
        # 容忍已经是 OpenAI 格式的输入
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        return ToolSpec(
            name=str(fn.get("name", "") or ""),
            description=str(fn.get("description", "") or ""),
            parameters=dict(fn.get("parameters")
                            or fn.get("input_schema") or {}))
    return ToolSpec(
        name=str(getattr(t, "name", "") or ""),
        description=str(getattr(t, "description", "") or ""),
        parameters=dict(getattr(t, "parameters", None) or {}))


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------
class LLMBackend(abc.ABC):
    """LLM 后端的接口。**两个抽象方法：构造请求 / 解析响应。**"""

    #: 后端标识
    name: str = "abstract"
    #: 协议（"openai" / "anthropic"）
    protocol: str = "openai"

    def __init__(self, *,
                 api_key: str = "",
                 base_url: str = "",
                 model: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 budget: Optional[TokenBudget] = None,
                 enabled: bool = False,
                 http_post: Optional[Callable] = None,
                 sleep: Optional[Callable] = None) -> None:
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "")
        self.model = str(model or "")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.max_retries = max(1, int(max_retries))
        self.budget = budget if budget is not None else TokenBudget()
        #: **默认关闭** —— 与 solver / LLMPlanner 同一个理由：
        #: 调外部服务要花钱，必须显式打开。
        self.enabled = bool(enabled)
        #: 可注入的 HTTP 与 sleep —— 测试用（不发真实请求、不真等）
        self._http_post = http_post
        self._sleep = sleep or time.sleep

    @property
    def active(self) -> bool:
        """真正可用吗？**必须 enabled + api_key + base_url + model。**"""
        return bool(self.enabled and self.api_key and self.base_url
                    and self.model)

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "后端未启用（enabled=False）"
        if not self.api_key:
            return "没有 api_key"
        if not self.base_url:
            return "没有 base_url"
        if not self.model:
            return "没有 model"
        return ""

    # ---------------- 抽象 ----------------
    @abc.abstractmethod
    def build_payload(self, messages: Sequence[Dict[str, Any]],
                      *, tools: Optional[Sequence[Any]] = None,
                      temperature: float = 0.0,
                      **kw: Any) -> Dict[str, Any]:
        """构造请求体。**各家格式不同。**"""
        raise NotImplementedError

    @abc.abstractmethod
    def build_headers(self) -> Dict[str, str]:
        """构造请求头。**必须带 User-Agent（硬约束 6）。**"""
        raise NotImplementedError

    @abc.abstractmethod
    def parse_response(self, raw: Any) -> LLMResponse:
        """解析响应。**归一化成统一形态。**"""
        raise NotImplementedError

    # ---------------- 主入口 ----------------
    def complete(self, messages: Sequence[Dict[str, Any]],
                 *, tools: Optional[Sequence[Any]] = None,
                 temperature: float = 0.0,
                 timeout: Optional[float] = None,
                 **kw: Any) -> LLMResponse:
        """发一次补全。**永不抛异常。**

        ## 流程

        1. 检查是否启用 → 没启用直接返回失败
        2. **估算 token 并检查预算** → 超了直接拒绝，不发请求
        3. 构造请求
        4. 发（带超时）
        5. 失败且可重试 → 指数退避后重试（最多 ``max_retries`` 次）
        6. 解析响应，记账
        """
        t0 = time.perf_counter()
        res = LLMResponse()

        if not self.active:
            res.reason = self.unavailable_reason()
            res.elapsed = time.perf_counter() - t0
            return res

        payload = self.build_payload(messages, tools=tools,
                                     temperature=temperature, **kw)
        headers = self.build_headers()

        # ---- token 预算（硬约束 5）----
        est = estimate_tokens(json.dumps(payload, ensure_ascii=False))
        ok, why = self.budget.check(est)
        if not ok:
            res.reason = "token 预算拒绝：%s" % why
            res.elapsed = time.perf_counter() - t0
            return res

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        to = float(timeout if timeout else self.timeout)

        last_reason = ""
        for attempt in range(1, self.max_retries + 1):
            res.attempts = attempt
            try:
                raw = self._post(self.base_url, body, headers, to)
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                detail = _read_err_body(exc)
                last_reason = "HTTP %d：%s" % (status, detail[:160])
                if status not in RETRYABLE_STATUS:
                    res.reason = last_reason
                    res.retryable = False
                    res.elapsed = time.perf_counter() - t0
                    return res
                res.retryable = True
            except Exception as exc:      # noqa: BLE001
                # 网络层错误（超时、DNS、连接重置）—— 都算可重试
                last_reason = "%s: %s" % (type(exc).__name__,
                                          str(exc)[:160])
                res.retryable = True
            else:
                parsed = self.parse_response(raw)
                parsed.attempts = attempt
                parsed.elapsed = time.perf_counter() - t0
                self.budget.consume(parsed.total_tokens or est)
                return parsed

            # 可重试 —— 指数退避
            if attempt < self.max_retries:
                self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))

        res.reason = "重试 %d 次仍失败：%s" % (self.max_retries, last_reason)
        res.elapsed = time.perf_counter() - t0
        return res

    def _post(self, url: str, body: bytes, headers: Dict[str, str],
              timeout: float) -> str:
        fn = self._http_post or _default_http_post
        return fn(url, body, headers, timeout)

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        state = "已启用" if self.active else "未启用（默认）"
        lines = [
            "%s：%s" % (type(self).__name__, state),
            "  protocol : %s" % self.protocol,
            "  base_url : %s" % (self.base_url or "(未设置)"),
            "  model    : %s" % (self.model or "(未设置)"),
            "  api_key  : %s" % _redact(self.api_key),
            "  timeout  : %.1fs  重试 %d 次" % (self.timeout,
                                               self.max_retries),
            "  预算     : 单次 %d / 累计 %d（已用 %d）"
            % (self.budget.per_request, self.budget.total,
               self.budget.used),
        ]
        if not self.active:
            lines.append("  原因     : %s" % self.unavailable_reason())
        return "\n".join(lines)


def _redact(key: str) -> str:
    """api_key 脱敏。**任何输出路径都必须过它。**"""
    k = str(key or "")
    if not k:
        return "(未设置)"
    if len(k) <= 8:
        return "***"
    return k[:4] + "…" + k[-2:]


def _read_err_body(exc: Any) -> str:
    try:
        raw = exc.read()
        return raw.decode("utf-8", "replace") if raw else ""
    except Exception:      # noqa: BLE001
        return ""


def _default_http_post(url: str, body: bytes, headers: Dict[str, str],
                       timeout: float) -> str:
    """**唯一碰网络的地方。** 测试替换它即可断言"发了什么"。"""
    req = urllib.request.Request(url, data=body, headers=dict(headers),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# OpenAI 兼容（GPT / DeepSeek / Kimi / Qwen）
# --------------------------------------------------------------------------
class OpenAICompatibleBackend(LLMBackend):
    """OpenAI 的 ``chat/completions`` 协议。

    **DeepSeek / Kimi / Qwen 都用这套** —— 它们把自己做成了
    "换个 base_url 就能用"的兼容层。所以本模块不为它们各写一套。
    """

    name = "openai"
    protocol = "openai"

    def build_payload(self, messages: Sequence[Dict[str, Any]],
                      *, tools: Optional[Sequence[Any]] = None,
                      temperature: float = 0.0, **kw: Any
                      ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in (messages or [])],
            "temperature": float(temperature),
        }
        nt = normalize_tools(tools, protocol="openai") if tools else []
        if nt:
            payload["tools"] = nt
            payload["tool_choice"] = kw.pop("tool_choice", "auto")
        # 要求 JSON 输出 —— 规划场景要的是结构，不是散文
        if kw.pop("json_mode", False):
            payload["response_format"] = {"type": "json_object"}
        for k in ("max_tokens", "top_p", "stop"):
            if k in kw and kw[k] is not None:
                payload[k] = kw[k]
        return payload

    def build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.api_key,
            # 硬约束 6：必须带 User-Agent
            "User-Agent": "avatar-human-computer-use/1.0 "
                          "(+agentic_planner.llm_backend)",
        }

    def parse_response(self, raw: Any) -> LLMResponse:
        res = LLMResponse(raw=raw)
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:      # noqa: BLE001
            res.reason = "响应不是合法 JSON：%s" % type(exc).__name__
            return res
        if not isinstance(d, dict):
            res.reason = "响应不是对象"
            return res

        # 有些兼容层把错误放在 body 里（HTTP 200）
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
        for tc in (msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:      # noqa: BLE001
                    args = {"_raw": args}
            res.tool_calls.append({
                "id": str((tc or {}).get("id", "") or ""),
                "name": str(fn.get("name", "") or ""),
                "arguments": args if isinstance(args, dict) else {},
            })

        res.ok = bool(res.text or res.tool_calls)
        if not res.ok:
            res.reason = "响应既没有文本也没有工具调用"
        return res


# --------------------------------------------------------------------------
# Anthropic（Claude）
# --------------------------------------------------------------------------
class AnthropicBackend(LLMBackend):
    """Claude 的 ``messages`` 协议。

    ## 与 OpenAI 的三处差异

    1. **系统提示是顶层字段** ``system``，不在 ``messages`` 里
    2. **工具用 ``input_schema``**，且不包 ``function``
    3. **认证头是 ``x-api-key``**，外加 ``anthropic-version``
    """

    name = "anthropic"
    protocol = "anthropic"

    #: Anthropic 要求显式声明 API 版本。
    API_VERSION = "2023-06-01"

    def build_payload(self, messages: Sequence[Dict[str, Any]],
                      *, tools: Optional[Sequence[Any]] = None,
                      temperature: float = 0.0, **kw: Any
                      ) -> Dict[str, Any]:
        # 把 system 消息抽出来放到顶层
        sys_parts: List[str] = []
        convo: List[Dict[str, Any]] = []
        for m in (messages or []):
            role = str(m.get("role", "") or "")
            if role == "system":
                sys_parts.append(str(m.get("content", "") or ""))
                continue
            convo.append({"role": role or "user",
                          "content": m.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": convo,
            "temperature": float(temperature),
            "max_tokens": int(kw.pop("max_tokens", 4096)),
        }
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)

        nt = normalize_tools(tools, protocol="anthropic") if tools else []
        if nt:
            payload["tools"] = nt
        for k in ("top_p", "stop_sequences"):
            if k in kw and kw[k] is not None:
                payload[k] = kw[k]
        return payload

    def build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "User-Agent": "avatar-human-computer-use/1.0 "
                          "(+agentic_planner.llm_backend)",
        }

    def parse_response(self, raw: Any) -> LLMResponse:
        res = LLMResponse(raw=raw)
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

        texts: List[str] = []
        for block in (d.get("content") or []):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", "") or "")
            if btype == "text":
                texts.append(str(block.get("text", "") or ""))
            elif btype == "tool_use":
                res.tool_calls.append({
                    "id": str(block.get("id", "") or ""),
                    "name": str(block.get("name", "") or ""),
                    "arguments": dict(block.get("input") or {})
                    if isinstance(block.get("input"), dict) else {},
                })

        res.text = "\n".join(t for t in texts if t)
        res.ok = bool(res.text or res.tool_calls)
        if not res.ok:
            res.reason = "响应既没有文本也没有工具调用"
        return res


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def get_backend(provider: str = PROVIDER_OPENAI, *,
                api_key: str = "",
                base_url: str = "",
                model: str = "",
                enabled: bool = False,
                timeout: float = DEFAULT_TIMEOUT,
                max_retries: int = DEFAULT_MAX_RETRIES,
                budget: Optional[TokenBudget] = None,
                env: Optional[Dict[str, str]] = None,
                **kw: Any) -> LLMBackend:
    """按厂商名造一个后端。**未知厂商抛 ValueError（不静默降级）。**

    ``api_key`` / ``base_url`` / ``model`` 不传时从环境变量读，
    再退到厂商预设。
    """
    p = str(provider or PROVIDER_OPENAI).strip().lower()
    if p not in PROVIDER_PRESETS:
        raise ValueError("未知的 LLM 厂商：%r（可用：%s）"
                         % (provider, " / ".join(PROVIDERS)))

    preset = PROVIDER_PRESETS[p]
    e = os.environ if env is None else env
    key = api_key or str(e.get(ENV_API_KEY, "") or "")
    url = base_url or str(e.get(ENV_BASE_URL, "") or "") \
        or preset["base_url"]
    mdl = model or str(e.get(ENV_MODEL, "") or "") or preset["model"]

    cls = (AnthropicBackend if preset["protocol"] == "anthropic"
           else OpenAICompatibleBackend)
    return cls(api_key=key, base_url=url, model=mdl, enabled=enabled,
               timeout=timeout, max_retries=max_retries, budget=budget,
               **kw)


def describe_llm(backend: Optional[LLMBackend] = None) -> str:
    """给人看的 LLM 配置说明。"""
    lines = ["LLM 后端（零 SDK，纯 urllib）", "=" * 64]
    if backend is not None:
        lines.append(backend.describe())
    else:
        lines.append("未指定后端。支持的厂商与预设：")
        for p in PROVIDERS:
            pre = PROVIDER_PRESETS[p]
            lines.append("  %-10s %-28s %s" % (p, pre["model"],
                                               pre["protocol"]))
    lines += [
        "",
        "启用方式：",
        "  set %s=sk-..." % ENV_API_KEY,
        "  set %s=deepseek" % ENV_PROVIDER,
        "  from avatar.agentic_planner.llm_backend import get_backend",
        "  b = get_backend('deepseek', enabled=True)",
        "",
        "三条硬约束：",
        "  · 超时 %.0fs（不设超时的 HTTP 会永久挂住）" % DEFAULT_TIMEOUT,
        "  · 重试 %d 次，指数退避，**只重试可重试的错误**"
        % DEFAULT_MAX_RETRIES,
        "  · token 预算：单次 %d / 累计 %d，超了不发请求"
        % (DEFAULT_PER_REQUEST_TOKENS, DEFAULT_TOTAL_TOKENS),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _FakeHTTP:
    """记录"发了什么"，返回预设响应。"""

    def __init__(self, reply: str = "", exc: Any = None,
                 replies: Optional[List[str]] = None) -> None:
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
    _nosleep = lambda s: None

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("五个厂商预设齐全", len(_me.PROVIDER_PRESETS) == 5,
         " / ".join(_me.PROVIDERS))
    _add("★ 四个走 OpenAI 兼容协议",
         sum(1 for p in _me.PROVIDER_PRESETS.values()
             if p["protocol"] == "openai") == 4, "openai ×4")
    _add("★ Anthropic 单独一套协议",
         _me.PROVIDER_PRESETS[_me.PROVIDER_ANTHROPIC]["protocol"]
         == "anthropic", "anthropic")
    _add("★ 4xx 不在可重试列表里",
         400 not in _me.RETRYABLE_STATUS
         and 401 not in _me.RETRYABLE_STATUS
         and 403 not in _me.RETRYABLE_STATUS, "参数错误不重试")
    _add("★ 429 / 5xx 可重试",
         429 in _me.RETRYABLE_STATUS and 500 in _me.RETRYABLE_STATUS
         and 503 in _me.RETRYABLE_STATUS, "限流/服务端可重试")

    # ============ 2) token 估算 ============
    _add("空串估算为 0 或 1", _me.estimate_tokens("") <= 1, "0~1")
    _add("★ 中文比英文「贵」（同样字符数估算更多）",
         _me.estimate_tokens("中文中文中文中文") >
         _me.estimate_tokens("abcdefghijklmnop"), "中文更贵")
    _add("dict 也能估算",
         _me.estimate_tokens({"a": "b"}) > 0, "有值")
    _add("None 返回 0", _me.estimate_tokens(None) == 0, "0")
    _add("估算永不抛", all(_me.estimate_tokens(x) >= 0
                         for x in (None, 1, [], {}, object())), "全部有值")

    # ============ 3) token 预算（硬约束 5）============
    b = _me.TokenBudget(per_request=100, total=250)
    _add("单次超限被拒", b.check(101)[0] is False, b.check(101)[1])
    _add("单次未超限通过", b.check(100)[0] is True, "通过")
    b.consume(100)
    _add("累计记账", b.used == 100, str(b.used))
    _add("累计超限被拒", b.check(200)[0] is False, b.check(200)[1])
    _add("剩余额度正确", b.remaining == 150, str(b.remaining))
    _add("拒绝次数被记", b.rejected >= 2, str(b.rejected))
    b.reset()
    _add("reset 清零", b.used == 0 and b.rejected == 0, "清零")
    _add("to_dict JSON 安全",
         __import__("json").dumps(b.to_dict()) != "", "ok")

    # ============ 4) 默认关闭 ============
    bk = _me.OpenAICompatibleBackend()
    _add("★ 默认未启用", bk.active is False, "关")
    _add("★ 缺 key 时不可用",
         _me.OpenAICompatibleBackend(enabled=True).active is False,
         "不可用")
    _add("★ 缺 base_url 时不可用",
         _me.OpenAICompatibleBackend(enabled=True, api_key="k").active
         is False, "不可用")
    _add("四个都齐才可用",
         _me.OpenAICompatibleBackend(
             enabled=True, api_key="k", base_url="https://x",
             model="m").active is True, "可用")
    _add("unavailable_reason 说明缺什么（逐项）",
         "未启用" in bk.unavailable_reason()
         and "api_key" in _me.OpenAICompatibleBackend(
             enabled=True).unavailable_reason()
         and "base_url" in _me.OpenAICompatibleBackend(
             enabled=True, api_key="k").unavailable_reason(),
         "逐项说明")

    # ---- 未启用时不发请求 ----
    fake = _FakeHTTP(reply="{}")
    bk2 = _me.OpenAICompatibleBackend(enabled=False, api_key="k",
                                      base_url="https://x", model="m",
                                      http_post=fake)
    r = bk2.complete([{"role": "user", "content": "hi"}])
    _add("★★ 未启用时 complete 返回失败且**不发请求**",
         r.ok is False and fake.calls == [], "零请求")

    # ============ 5) 请求构造 ============
    fake2 = _FakeHTTP(reply=json.dumps({
        "choices": [{"message": {"content": "你好"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
    bk3 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="SECRET", base_url="https://api.x/v1/chat",
        model="m1", http_post=fake2, sleep=_nosleep)
    r = bk3.complete([{"role": "user", "content": "hi"}])
    _add("★ 成功返回文本", r.ok and r.text == "你好", r.describe())
    _add("★ 请求带 timeout（硬约束 6）",
         fake2.calls[0][3] == _me.DEFAULT_TIMEOUT, str(fake2.calls[0][3]))
    _add("★ 请求带 User-Agent（硬约束 6）",
         bool(fake2.calls[0][2].get("User-Agent")), "有")
    _add("请求带 Authorization",
         fake2.calls[0][2].get("Authorization", "").startswith("Bearer "),
         "有")
    _add("★ api_key 不在 URL 里",
         "SECRET" not in fake2.calls[0][0], "URL 干净")
    _add("body 是 JSON",
         json.loads(fake2.calls[0][1].decode("utf-8"))["model"] == "m1",
         "model 对")
    _add("temperature 默认 0（规划要确定性）",
         json.loads(fake2.calls[0][1].decode("utf-8"))["temperature"] == 0.0,
         "0.0")
    _add("usage 被记下", r.prompt_tokens == 10 and r.completion_tokens == 5,
         "%d+%d" % (r.prompt_tokens, r.completion_tokens))
    _add("预算被记账", bk3.budget.used > 0, str(bk3.budget.used))

    # ---- 工具规范化 ----
    payload = bk3.build_payload(
        [{"role": "user", "content": "x"}],
        tools=[{"name": "click", "description": "点",
                "parameters": {"type": "object",
                               "properties": {"x": {"type": "integer"}},
                               "required": ["x"]}}])
    _add("★ OpenAI 工具格式：包一层 function",
         payload["tools"][0]["type"] == "function"
         and payload["tools"][0]["function"]["name"] == "click", "对")
    _add("工具 parameters 原样带过去",
         "required" in payload["tools"][0]["function"]["parameters"], "有")

    ab = _me.AnthropicBackend(enabled=True, api_key="k",
                              base_url="https://api.anthropic.com/v1/messages",
                              model="claude")
    ap = ab.build_payload(
        [{"role": "system", "content": "你是助手"},
         {"role": "user", "content": "x"}],
        tools=[{"name": "click", "description": "点", "parameters": {}}])
    _add("★ Anthropic：system 提到顶层",
         ap.get("system") == "你是助手"
         and all(m["role"] != "system" for m in ap["messages"]), "对")
    _add("★ Anthropic：工具用 input_schema",
         "input_schema" in ap["tools"][0]
         and "function" not in ap["tools"][0], "对")
    _add("Anthropic 头用 x-api-key",
         ab.build_headers().get("x-api-key") == "k", "对")
    _add("Anthropic 头带 anthropic-version",
         bool(ab.build_headers().get("anthropic-version")), "有")
    _add("Anthropic 头带 User-Agent",
         bool(ab.build_headers().get("User-Agent")), "有")

    # ============ 6) 响应解析 ============
    res = bk3.parse_response(json.dumps({
        "choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "click",
                                      "arguments": '{"x": 10}'}}]}}],
        "usage": {}}))
    _add("★ 解析 OpenAI 工具调用",
         res.ok and res.tool_calls
         and res.tool_calls[0]["name"] == "click"
         and res.tool_calls[0]["arguments"] == {"x": 10}, "对")

    res = ab.parse_response(json.dumps({
        "content": [{"type": "text", "text": "好的"},
                    {"type": "tool_use", "id": "t1", "name": "click",
                     "input": {"x": 5}}],
        "usage": {"input_tokens": 3, "output_tokens": 7}}))
    _add("★ 解析 Anthropic 内容块",
         res.ok and res.text == "好的"
         and res.tool_calls[0]["arguments"] == {"x": 5}, "对")
    _add("Anthropic usage 字段名不同（input/output）",
         res.prompt_tokens == 3 and res.completion_tokens == 7, "对")

    _add("坏 JSON 不抛",
         bk3.parse_response("{坏的").ok is False, "失败")
    _add("非对象不抛",
         bk3.parse_response("[1,2]").ok is False, "失败")
    _add("没有 choices 不抛",
         bk3.parse_response('{"choices": []}').ok is False, "失败")
    _add("服务端错误放在 200 里也能识别",
         bk3.parse_response(
             '{"error": {"message": "rate limited"}}').ok is False, "失败")
    _add("Anthropic error 也能识别",
         ab.parse_response('{"type":"error","error":{"message":"x"}}').ok
         is False, "失败")

    # ============ 7) 重试（硬约束 6）============
    fake3 = _FakeHTTP(replies=[
        urllib.error.HTTPError("u", 503, "busy", {}, None),
        urllib.error.HTTPError("u", 429, "slow down", {}, None),
        json.dumps({"choices": [{"message": {"content": "终于好了"}}],
                    "usage": {}}),
    ])
    bk4 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=fake3, sleep=_nosleep, max_retries=3)
    r = bk4.complete([{"role": "user", "content": "x"}])
    _add("★★ 503→429→成功：重试 3 次后成功",
         r.ok and r.attempts == 3 and r.text == "终于好了", r.describe())
    _add("★ 重试次数不超上限", len(fake3.calls) == 3, "%d 次" % len(fake3.calls))

    fake4 = _FakeHTTP(exc=urllib.error.HTTPError("u", 401, "bad key", {},
                                                 None))
    bk5 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=fake4, sleep=_nosleep, max_retries=3)
    r = bk5.complete([{"role": "user", "content": "x"}])
    _add("★★ 401 **不重试**（参数错误重试没用）",
         r.ok is False and len(fake4.calls) == 1, "%d 次" % len(fake4.calls))

    fake5 = _FakeHTTP(replies=[urllib.error.URLError("网络断了")] * 5)
    bk6 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=fake5, sleep=_nosleep, max_retries=3)
    r = bk6.complete([{"role": "user", "content": "x"}])
    _add("★★ 网络错误重试到上限后放弃",
         r.ok is False and len(fake5.calls) == 3, "%d 次" % len(fake5.calls))
    _add("失败也带 reason",
         bool(r.reason) and "重试" in r.reason, r.reason[:60])

    # ---- 指数退避真的调了 sleep ----
    slept: List[float] = []
    fake6 = _FakeHTTP(replies=[urllib.error.URLError("x")] * 5)
    bk7 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        http_post=fake6, sleep=lambda s: slept.append(s), max_retries=3)
    bk7.complete([{"role": "user", "content": "x"}])
    _add("★ 退避是递增的（0.5 → 1.0）",
         len(slept) == 2 and slept[1] > slept[0], str(slept))
    _add("★ 退避有上限（不超过 8s）", all(s <= 8.0 for s in slept),
         str(slept))

    # ============ 8) 预算在发请求前拦住 ============
    tight = _me.TokenBudget(per_request=10, total=10)
    fake7 = _FakeHTTP(reply="{}")
    bk8 = _me.OpenAICompatibleBackend(
        enabled=True, api_key="k", base_url="https://x", model="m",
        budget=tight, http_post=fake7, sleep=_nosleep)
    r = bk8.complete([{"role": "user", "content": "很长的一段话" * 100}])
    _add("★★ 超预算时**不发请求**（硬约束 5）",
         r.ok is False and fake7.calls == [] and "预算" in r.reason,
         r.reason[:60])

    # ============ 9) 工厂 ============
    _add("★ get_backend 未知厂商抛 ValueError",
         _raise_type(lambda: _me.get_backend("gpt5")) == "ValueError",
         "抛了")
    b1 = _me.get_backend("deepseek")
    _add("deepseek 走 OpenAI 兼容",
         isinstance(b1, _me.OpenAICompatibleBackend), type(b1).__name__)
    _add("deepseek 预设 base_url 正确",
         "deepseek.com" in b1.base_url, b1.base_url)
    b2 = _me.get_backend("anthropic")
    _add("anthropic 走 AnthropicBackend",
         isinstance(b2, _me.AnthropicBackend), type(b2).__name__)
    b3 = _me.get_backend("kimi", api_key="K", enabled=True)
    _add("显式参数覆盖环境",
         b3.api_key == "K", "K")
    b4 = _me.get_backend("openai", env={_me.ENV_API_KEY: "ENVKEY"})
    _add("环境变量能提供 key",
         b4.api_key == "ENVKEY", "ENVKEY")
    _add("★ 默认关闭（即使配了 key）", b4.active is False, "关")

    # ============ 10) 脱敏 ============
    bk9 = _me.OpenAICompatibleBackend(enabled=True,
                                      api_key="sk-abcdefgh12345678",
                                      base_url="https://x", model="m")
    _add("★ describe 脱敏 api_key",
         "sk-abcdefgh12345678" not in bk9.describe(), "已脱敏")
    _add("短 key 全打码", _me._redact("short") == "***", "***")
    _add("空 key 显示未设置", _me._redact("") == "(未设置)", "未设置")

    # ============ 11) 展示 ============
    _add("describe_llm 不抛",
         isinstance(_me.describe_llm(), str), "有")
    _add("describe_llm 列出五个厂商",
         all(p in _me.describe_llm() for p in _me.PROVIDERS), "有")
    _add("LLMResponse.describe 可读",
         "成功" in _me.LLMResponse(ok=True).describe(), "有")

    # ============ 12) 零 SDK ============
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
    _add("★★ 不 import 任何 LLM SDK",
         not (mods & {"openai", "anthropic", "requests", "httpx",
                      "aiohttp", "litellm", "dashscope"}),
         sorted(mods))
    _add("★ 只用 urllib（标准库）", "urllib" in mods, "urllib")

    elapsed = time.perf_counter() - t0
    return {
        "module": "llm_backend",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


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
    print(describe_llm())
