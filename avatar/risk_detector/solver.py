# -*- coding: utf-8 -*-
"""外部打码平台对接 —— 5D。

## 这个模块是什么

把验证题**外包给人**的一条通道：调用第三方打码平台（2Captcha /
CapSolver 等），把图或 site_key 发过去，拿回答案。

它和 5C 的 :mod:`guard` 是**两条并行的路**，不是替代关系：

    guard.pause_for_human   叫**本机前面坐着的那个人**
    solver.solve_*          叫**平台背后的人**

选哪条由调用方决定。本模块只提供能力，不替调用方做选择。

## ⚠ 默认关闭（硬约束 #1）

**本模块默认不启用，也不会自动启用。** 必须同时满足：

1. 显式传 ``enabled=True``（或环境变量 ``AVATAR_SOLVER_ENABLED=1``）
2. ``api_key`` 非空（或环境变量 ``AVATAR_SOLVER_API_KEY``）

否则所有 ``solve_*`` **直接返回 None，连一个字节都不发出去**。

为什么定这么死：

- **打码要花钱**。误触发就是真金白银往外流。
- **会把图发到第三方**。截图里可能有账号、订单、聊天内容。
  这是一个**隐私外发**动作，必须由人显式打开。
- **很多平台的服务条款禁止自动化打码**。默认关闭让"开了"成为
  一个明确的人类决定，而不是一个库的默认行为。

## 能力边界

**做**：

- ``solve_captcha_via_api`` —— 发一张图（base64 或 multipart），
  取回识别结果字符串。
- ``solve_recaptcha_token`` —— 走 2Captcha 的 reCAPTCHA 流程
  （``in.php`` 下单 → ``res.php`` 轮询），取回 token。
- 兼容 2Captcha / CapSolver 的通用响应格式
  （``{"status": ..., "solution": ...}`` / ``{"errorId": 0, ...}``）。
- **失败一律返回 None**，不抛异常（硬约束 #2）——
  打码失败是**预期内的常态**（余额不足、图太糊、平台超时），
  不该让调用方到处写 try。

**不做**：

- **不硬解**。本模块里没有一行识别滑块缺口、点选坐标、
  OCR 验证码的代码（硬约束 #9）。它只是个 HTTP 客户端，
  把问题**转交出去**。真正的解法是平台背后的人。
- **不猜验证码内容**。短信/邮件 OTP 不在本模块范围内 ——
  那是人的凭证，机器不该碰。
- **不重试**。一次调用一次请求（reCAPTCHA 的轮询除外）。
  重试策略是调用方的决定，本模块不发明默认值。
- **不缓存**。同一个图重复调用会重复计费 —— 调用方自己去重。
- **不在没配置时"顺手用个免费接口"**。没有默认 api_url，
  没配就是没配。
- **不记日志到磁盘**。``api_key`` 绝不能出现在任何持久化产物里。

## 测试约定

**所有请求构造都有独立测试，且不发真实请求**（硬约束）。
为此本模块把两件事分开：

- ``build_multipart_body`` / ``build_json_body`` —— **纯函数**，
  直接测字节输出。
- ``_http_post`` / ``_http_get`` —— 唯一碰网络的地方，
  测试里替换掉它就能断言"发了什么"。

:func:`selfcheck` 全程用假 http 函数，**一个字节都不出网**。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "DEFAULT_2CAPTCHA_IN",
    "DEFAULT_2CAPTCHA_RES",
    "ENV_ENABLED",
    "ENV_API_KEY",
    "ENV_API_URL",
    "MODE_AUTO",
    "MODE_MULTIPART",
    "MODE_JSON",
    "SolverConfig",
    "SolveResult",
    "is_enabled",
    "solve_captcha_via_api",
    "solve_captcha_detailed",
    "solve_recaptcha_token",
    "build_multipart_body",
    "build_json_body",
    "pick_mode",
    "parse_response",
    "describe_solver",
    "selfcheck",
]

#: 默认超时（秒）。**所有请求都必须带**（硬约束 #2）。
DEFAULT_TIMEOUT = 60.0

#: 默认 User-Agent。有些平台会按 UA 做风控，给个明确的。
DEFAULT_USER_AGENT = "avatar-human-computer-use/1.0 (+risk_detector.solver)"

#: 2Captcha 的两个端点。写死是因为它们的协议是固定的。
DEFAULT_2CAPTCHA_IN = "https://2captcha.com/in.php"
DEFAULT_2CAPTCHA_RES = "https://2captcha.com/res.php"

#: 环境变量名。**默认关闭**，这两个都不设时模块是死的。
ENV_ENABLED = "AVATAR_SOLVER_ENABLED"
ENV_API_KEY = "AVATAR_SOLVER_API_KEY"
ENV_API_URL = "AVATAR_SOLVER_API_URL"

#: 请求体编码方式。
MODE_AUTO = "auto"          # 按 api_url 猜（默认）
MODE_MULTIPART = "multipart"
MODE_JSON = "json"

#: 轮询间隔与上限（仅 reCAPTCHA 流程用）。
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_MAX_POLL = 24       # 24 × 5s = 120s，比 timeout 略短的兜底

#: 模块级别名 —— 测试只改这个，不碰全局 time.sleep（5C 的教训）。
_sleep = time.sleep

#: 唯一碰网络的两个函数。测试替换它们即可断言"发了什么"。
_http_post: Optional[Callable[..., str]] = None
_http_get: Optional[Callable[..., str]] = None


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class SolverConfig:
    """打码平台配置。**默认全关。**

    ## 为什么做成对象而不是一堆参数

    因为"启用"是一个**需要同时满足两个条件**的判断
    （``enabled`` 且 ``api_key``）。散成参数时，调用方很容易
    只传了一个就以为开了。做成对象后，判断集中在
    :meth:`active` 一处，别的代码不需要重新推一遍。
    """

    __slots__ = ("enabled", "api_url", "api_key", "timeout", "mode",
                 "user_agent", "poll_interval", "max_poll", "extra")

    def __init__(self,
                 *,
                 enabled: bool = False,
                 api_url: str = "",
                 api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 mode: str = MODE_AUTO,
                 user_agent: str = DEFAULT_USER_AGENT,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 max_poll: int = DEFAULT_MAX_POLL,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        self.enabled = bool(enabled)
        self.api_url = str(api_url or "")
        self.api_key = str(api_key or "")
        self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
        self.mode = str(mode or MODE_AUTO)
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.poll_interval = float(poll_interval)
        self.max_poll = int(max_poll)
        self.extra = dict(extra or {})

    @property
    def active(self) -> bool:
        """真正启用了吗？**必须 enabled 且 api_key 非空。**

        这个属性是"要不要发请求"的唯一判据。别的地方
        不要自己写 ``if cfg.enabled`` —— 那只检查了一半。
        """
        return bool(self.enabled and self.api_key)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "SolverConfig":
        """从环境变量读配置。**读不到就是关闭状态，永不抛。**"""
        e = os.environ if env is None else env
        raw_on = str(e.get(ENV_ENABLED, "") or "").strip().lower()
        enabled = raw_on in ("1", "true", "yes", "on", "y")
        return cls(
            enabled=enabled,
            api_url=str(e.get(ENV_API_URL, "") or ""),
            api_key=str(e.get(ENV_API_KEY, "") or ""),
        )

    def to_dict(self, *, redact: bool = True) -> Dict[str, Any]:
        """序列化。**默认脱敏** —— api_key 绝不能进日志。"""
        key = self.api_key
        if redact and key:
            key = (key[:4] + "…" + key[-2:]) if len(key) > 8 else "***"
        return {
            "enabled": self.enabled,
            "active": self.active,
            "api_url": self.api_url,
            "api_key": key,
            "timeout": self.timeout,
            "mode": self.mode,
            "poll_interval": self.poll_interval,
            "max_poll": self.max_poll,
        }

    def describe(self) -> str:
        d = self.to_dict()
        state = "已启用" if d["active"] else (
            "配置不完整（缺 api_key）" if d["enabled"] else "关闭（默认）")
        return "\n".join([
            "打码平台配置：%s" % state,
            "  api_url : %s" % (d["api_url"] or "(未设置)"),
            "  api_key : %s" % (d["api_key"] or "(未设置)"),
            "  timeout : %.1fs" % d["timeout"],
            "  编码    : %s" % d["mode"],
        ])


class SolveResult:
    """一次打码尝试的完整结果。

    ``solve_captcha_via_api`` 只返回 ``solution`` 字符串（用起来方便），
    但排查问题时需要知道**为什么失败**。这个对象承载那些细节，
    由 ``solve_captcha_detailed`` 返回，或者通过 ``errors`` 列表带出。
    """

    __slots__ = ("ok", "solution", "reason", "raw", "elapsed",
                 "status_code", "attempts")

    def __init__(self,
                 *,
                 ok: bool = False,
                 solution: Any = None,
                 reason: str = "",
                 raw: Any = None,
                 elapsed: float = 0.0,
                 status_code: int = 0,
                 attempts: int = 0) -> None:
        self.ok = bool(ok)
        self.solution = solution
        self.reason = str(reason or "")
        self.raw = raw
        self.elapsed = float(elapsed)
        self.status_code = int(status_code)
        self.attempts = int(attempts)

    @property
    def text(self) -> str:
        """solution 的字符串形式（``solution`` 可能是 dict）。"""
        s = self.solution
        if s is None:
            return ""
        if isinstance(s, str):
            return s
        if isinstance(s, dict):
            # CapSolver 的 solution 是 {"gRecaptchaResponse": "..."} 这种
            for k in ("gRecaptchaResponse", "token", "text", "answer",
                      "code", "result"):
                v = s.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in s.values():
                if isinstance(v, str) and v:
                    return v
            return json.dumps(s, ensure_ascii=False)
        return str(s)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "solution": self.solution if isinstance(
                self.solution, (str, int, float, bool, type(None))) else str(
                    self.solution)[:200],
            "reason": self.reason,
            "elapsed": round(self.elapsed, 3),
            "status_code": self.status_code,
            "attempts": self.attempts,
        }

    def describe(self) -> str:
        if self.ok:
            return "✓ 打码成功（%.1fs，%d 次请求）：%s" % (
                self.elapsed, self.attempts, self.text[:60])
        return "✗ 打码失败：%s（%.1fs，%d 次请求）" % (
            self.reason or "未知原因", self.elapsed, self.attempts)


# --------------------------------------------------------------------------
# 启用判断
# --------------------------------------------------------------------------
def is_enabled(config: Optional[SolverConfig] = None,
               env: Optional[Dict[str, str]] = None) -> bool:
    """打码平台现在启用了吗？**永不抛。**

    不传 ``config`` 时从环境变量读。这是"能不能发请求"的唯一入口，
    CLI 和 selfcheck 都该调它，而不是自己检查 ``enabled``。
    """
    try:
        cfg = config if config is not None else SolverConfig.from_env(env)
        return bool(cfg.active)
    except Exception:      # noqa: BLE001
        # 配置读坏了 = 没配置好 = 关着
        return False


# --------------------------------------------------------------------------
# 请求体构造（纯函数 —— 独立可测，不碰网络）
# --------------------------------------------------------------------------
def build_multipart_body(fields: Dict[str, Any],
                         *,
                         files: Optional[Dict[str, bytes]] = None,
                         boundary: str = "") -> Tuple[bytes, str]:
    """构造 ``multipart/form-data`` 请求体。

    返回 ``(body, boundary)``。boundary 要写进 ``Content-Type`` 头，
    所以必须返回给调用方。

    ``fields`` 是普通表单字段（值是 str）；``files`` 是文件字段
    （值是 bytes，会走 base64 编码 —— 2Captcha 的 ``file`` 字段
    收的就是 base64 文本，不是二进制）。

    ## 为什么要手写而不是用 ``email`` 模块

    因为要**能测**。手写的版本给定 boundary 就产出确定的字节，
    测试可以逐字节断言。用标准库的 ``MIMEMultipart`` 会引入
    随机 boundary 和头部顺序的不确定性。
    """
    bd = boundary or ("----avatar%s" % uuid.uuid4().hex)
    out: List[bytes] = []
    nl = b"\r\n"

    for k, v in (fields or {}).items():
        out.append(("--%s" % bd).encode("ascii"))
        out.append(('Content-Disposition: form-data; name="%s"' % k)
                   .encode("utf-8"))
        out.append(b"")
        out.append(str(v).encode("utf-8"))

    for k, blob in (files or {}).items():
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        b64 = base64.b64encode(bytes(blob)).decode("ascii")
        out.append(("--%s" % bd).encode("ascii"))
        out.append(('Content-Disposition: form-data; name="%s"' % k)
                   .encode("utf-8"))
        out.append(b"")
        out.append(b64.encode("ascii"))

    out.append(("--%s--" % bd).encode("ascii"))

    body = nl.join(out) + nl
    return body, bd


def build_json_body(payload: Dict[str, Any]) -> bytes:
    """构造 JSON 请求体。``ensure_ascii=False`` 保留可读性。

    图（bytes）会被自动 base64 —— 调用方传原始字节即可，
    不用自己编码（少一处写错的机会）。
    """
    def _enc(v: Any) -> Any:
        if isinstance(v, (bytes, bytearray)):
            return base64.b64encode(bytes(v)).decode("ascii")
        if isinstance(v, dict):
            return {k: _enc(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_enc(x) for x in v]
        return v

    return json.dumps(_enc(payload or {}), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def pick_mode(api_url: str, mode: str = MODE_AUTO) -> str:
    """决定用哪种编码。

    显式给了 ``multipart`` / ``json`` 就用给的；``auto`` 时按
    api_url 猜：

    - 含 ``capsolver`` → JSON（CapSolver 的 API 只收 JSON）
    - 其余 → multipart（2Captcha 的 ``in.php`` 收表单）

    猜错的代价是请求被拒，不是崩溃 —— 平台会回一个错误，
    我们原样返回 None。所以这里不需要猜得多准。
    """
    m = str(mode or MODE_AUTO).strip().lower()
    if m in (MODE_MULTIPART, MODE_JSON):
        return m
    u = str(api_url or "").lower()
    if "capsolver" in u or "cap-solver" in u:
        return MODE_JSON
    return MODE_MULTIPART


# --------------------------------------------------------------------------
# 响应解析（纯函数）
# --------------------------------------------------------------------------
def parse_response(text: str) -> SolveResult:
    """解析平台响应。**兼容 2Captcha / CapSolver 两家的格式。**

    见过的形状（都是真实存在的，不是编的）::

        2Captcha 成功   {"status": 1, "request": "03AGdBq26..."}
        2Captcha 失败   {"status": 0, "request": "ERROR_WRONG_USER_KEY"}
        2Captcha 等待中 {"status": 0, "request": "CAPCHA_NOT_READY"}
        纯文本成功      "OK|03AGdBq26..."
        纯文本失败      "ERROR_CAPTCHA_UNSOLVABLE"
        CapSolver 成功  {"errorId": 0, "solution": {"gRecaptchaResponse": "..."}}
        CapSolver 失败  {"errorId": 1, "errorCode": "ERROR_...", "errorDescription": "..."}

    ## 关键区分：``CAPCHA_NOT_READY`` 不是失败

    2Captcha 的轮询接口在"还没算完"时返回 ``status: 0``，
    但那**不是**错误 —— 该继续等。所以这个函数把它标成
    ``reason="not_ready"`` 且 ``ok=False``，由轮询循环识别。
    把它当失败会让轮询第一次就退出，永远拿不到结果。
    """
    if not text or not str(text).strip():
        return SolveResult(ok=False, reason="empty_response")

    s = str(text).strip()

    # ---- 纯文本 "OK|xxx" / "ERROR_xxx" ----
    if not s.startswith("{"):
        if s.startswith("OK|"):
            return SolveResult(ok=True, solution=s[3:], raw=s)
        if "NOT_READY" in s.upper():
            return SolveResult(ok=False, reason="not_ready", raw=s)
        return SolveResult(ok=False, reason="platform_error:%s" % s[:80],
                           raw=s)

    # ---- JSON ----
    try:
        d = json.loads(s)
    except Exception:      # noqa: BLE001
        return SolveResult(ok=False, reason="bad_json", raw=s[:200])

    if not isinstance(d, dict):
        return SolveResult(ok=False, reason="json_not_object", raw=s[:200])

    # CapSolver 风格：errorId
    if "errorId" in d:
        eid = d.get("errorId")
        if eid in (0, "0", None):
            return SolveResult(ok=True, solution=d.get("solution"), raw=d)
        return SolveResult(
            ok=False,
            reason="capsolver_error:%s %s" % (
                d.get("errorCode", ""), d.get("errorDescription", "")),
            raw=d)

    # 2Captcha / 通用风格：status + request|solution
    status = d.get("status", d.get("code"))
    body = d.get("request", d.get("solution", d.get("result")))

    ok_status = status in (1, "1", True, "ok", "OK", "success", "SUCCESS")
    if ok_status:
        # 注意：status=1 且 request 是 task id 时（in.php 的下单响应），
        # 这也算成功 —— 解析层不区分"下单成功"和"算完了"，
        # 那个区分由调用方按端点决定。
        return SolveResult(ok=True, solution=body, raw=d)

    msg = str(body or d.get("error", d.get("message", "")) or "")
    if "NOT_READY" in msg.upper():
        return SolveResult(ok=False, reason="not_ready", raw=d)
    return SolveResult(ok=False, reason="platform_error:%s" % (msg or "unknown"),
                       raw=d)


# --------------------------------------------------------------------------
# 网络（唯一碰 IO 的地方 —— 测试替换这里）
# --------------------------------------------------------------------------
def _default_http_post(url: str, body: bytes, headers: Dict[str, str],
                       timeout: float) -> str:
    """真的发一个 POST。**只有这里出网。**"""
    req = urllib.request.Request(url, data=body, headers=dict(headers),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _default_http_get(url: str, headers: Dict[str, str],
                      timeout: float) -> str:
    """真的发一个 GET。**只有这里出网。**"""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _post(url: str, body: bytes, headers: Dict[str, str],
          timeout: float) -> str:
    """走可替换的 ``_http_post``。"""
    fn = _http_post or _default_http_post
    return fn(url, body, headers, timeout)


def _get(url: str, headers: Dict[str, str], timeout: float) -> str:
    """走可替换的 ``_http_get``。"""
    fn = _http_get or _default_http_get
    return fn(url, headers, timeout)


def _headers(cfg: SolverConfig, content_type: str) -> Dict[str, str]:
    """构造请求头。**User-Agent 必带**（有些平台按 UA 风控）。"""
    h = {
        "User-Agent": cfg.user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


# --------------------------------------------------------------------------
# 主入口 1：图片打码
# --------------------------------------------------------------------------
def solve_captcha_via_api(api_url: str = "",
                          api_key: str = "",
                          image_data: Any = b"",
                          timeout: float = DEFAULT_TIMEOUT,
                          *,
                          enabled: bool = False,
                          mode: str = MODE_AUTO,
                          config: Optional[SolverConfig] = None,
                          extra_fields: Optional[Dict[str, Any]] = None,
                          errors: Optional[List[str]] = None,
                          env: Optional[Dict[str, str]] = None,
                          ) -> Optional[str]:
    """把一张图发给打码平台，拿回识别结果。**失败返回 None。**

    ## 参数

    - ``image_data`` —— 图片字节（``bytes``）、base64 字符串、
      或本地文件路径（``str``）。三种都收 —— 调用方手上是什么就传什么，
      免得为了这个函数多写一次编码。
    - ``enabled`` —— **默认 False**。不显式打开就什么都不发。
    - ``api_url`` / ``api_key`` —— 不传时从环境变量读
      （``AVATAR_SOLVER_API_URL`` / ``AVATAR_SOLVER_API_KEY``）。
    - ``config`` —— 给了就以它为准，忽略上面几个参数。
    - ``errors`` —— 失败原因会 append 进来（对齐
      :func:`context.save_screenshot` 的风格）。不传就只返回 None。

    ## 返回

    成功 → 识别结果字符串（``str``）
    失败 → ``None``（**不抛异常**，硬约束 #2）

    ## 为什么"不抛异常"

    打码失败是**预期内的常态**：余额不足、图太糊、平台抽风、
    网络抖动。这些都不该让调用方的动作链崩掉 —— 它应该
    拿到 ``None`` 然后走"叫本机的人"那条路（5C 的 guard）。
    把失败做成异常，会逼调用方到处写 try，最后一定有人漏写。
    """
    def _err(msg: str) -> None:
        if errors is not None:
            try:
                errors.append(msg)
            except Exception:      # noqa: BLE001
                pass

    r = solve_captcha_detailed(
        api_url, api_key, image_data, timeout,
        enabled=enabled, mode=mode, config=config,
        extra_fields=extra_fields, env=env)

    if not r.ok:
        _err("solver: %s" % (r.reason or "unknown"))
        return None
    return r.text


def solve_captcha_detailed(api_url: str = "",
                           api_key: str = "",
                           image_data: Any = b"",
                           timeout: float = DEFAULT_TIMEOUT,
                           *,
                           enabled: bool = False,
                           mode: str = MODE_AUTO,
                           config: Optional[SolverConfig] = None,
                           extra_fields: Optional[Dict[str, Any]] = None,
                           env: Optional[Dict[str, str]] = None,
                           ) -> SolveResult:
    """同 :func:`solve_captcha_via_api`，但返回完整的 :class:`SolveResult`。

    排查"为什么没打出来"时用这个 —— 它带 reason / raw / 耗时 / 请求次数。
    """
    t0 = time.perf_counter()
    cfg = _resolve_config(config, enabled=enabled, api_url=api_url,
                          api_key=api_key, timeout=timeout, mode=mode, env=env)

    # ---- 闸门：没配好就一个字节都不发 ----
    if not cfg.active:
        why = ("solver_disabled" if not cfg.enabled
               else "solver_no_api_key")
        return SolveResult(ok=False, reason=why,
                           elapsed=time.perf_counter() - t0)
    if not cfg.api_url:
        return SolveResult(ok=False, reason="solver_no_api_url",
                           elapsed=time.perf_counter() - t0)

    blob, bad = _coerce_image(image_data)
    if bad:
        return SolveResult(ok=False, reason=bad,
                           elapsed=time.perf_counter() - t0)

    m = pick_mode(cfg.api_url, cfg.mode)
    fields: Dict[str, Any] = {"key": cfg.api_key}
    fields.update(extra_fields or {})

    try:
        if m == MODE_JSON:
            payload = dict(fields)
            payload.setdefault("image", blob)
            body = build_json_body(payload)
            headers = _headers(cfg, "application/json")
        else:
            fields.setdefault("method", "base64")
            body, bd = build_multipart_body(fields, files={"body": blob})
            headers = _headers(
                cfg, "multipart/form-data; boundary=%s" % bd)

        text = _post(cfg.api_url, body, headers, cfg.timeout)
    except Exception as exc:      # noqa: BLE001
        # 网络/超时/被拒 —— 全部收敛成失败，不外抛
        return SolveResult(ok=False,
                           reason="request_failed:%s: %s"
                                  % (type(exc).__name__, str(exc)[:120]),
                           elapsed=time.perf_counter() - t0, attempts=1)

    r = parse_response(text)
    r.elapsed = time.perf_counter() - t0
    r.attempts = 1
    return r


# --------------------------------------------------------------------------
# 主入口 2：reCAPTCHA token
# --------------------------------------------------------------------------
def solve_recaptcha_token(site_key: str,
                          page_url: str,
                          api_key: str = "",
                          *,
                          timeout: float = DEFAULT_TIMEOUT,
                          enabled: bool = False,
                          config: Optional[SolverConfig] = None,
                          in_url: str = DEFAULT_2CAPTCHA_IN,
                          res_url: str = DEFAULT_2CAPTCHA_RES,
                          poll_interval: float = DEFAULT_POLL_INTERVAL,
                          max_poll: int = DEFAULT_MAX_POLL,
                          errors: Optional[List[str]] = None,
                          env: Optional[Dict[str, str]] = None,
                          ) -> Optional[str]:
    """走 2Captcha 的 reCAPTCHA 流程，拿回 token。**失败返回 None。**

    ## 流程（两步，这是 2Captcha 的固定协议）

    1. ``POST in.php`` —— 下单。带 ``method=userrecaptcha``、
       ``googlekey=site_key``、``pageurl=page_url``。返回 task id。
    2. ``GET res.php`` —— 轮询。带 ``action=get&id=<task_id>``。
       没算完时返回 ``CAPCHA_NOT_READY``，**这不是错误，要继续等**。

    ## 为什么要轮询而不是长连接

    2Captcha 没有长连接接口。它的协议就是"下单 → 隔几秒问一次"。
    轮询间隔默认 5s，最多 24 次（约 120s）。

    ## 为什么 ``site_key`` 而不是截图

    reCAPTCHA v2/v3 的解法需要 ``site_key`` + ``pageurl``，
    平台用它去官方接口换 token。**截图在这里没用** ——
    这是 reCAPTCHA 和其他验证码最大的区别。

    ## 返回

    成功 → token 字符串；失败 → ``None``（不抛）。
    """
    def _err(msg: str) -> None:
        if errors is not None:
            try:
                errors.append(msg)
            except Exception:      # noqa: BLE001
                pass

    r = solve_recaptcha_detailed(
        site_key, page_url, api_key, timeout=timeout, enabled=enabled,
        config=config, in_url=in_url, res_url=res_url,
        poll_interval=poll_interval, max_poll=max_poll, env=env)

    if not r.ok:
        _err("solver.recaptcha: %s" % (r.reason or "unknown"))
        return None
    return r.text


def solve_recaptcha_detailed(site_key: str,
                             page_url: str,
                             api_key: str = "",
                             *,
                             timeout: float = DEFAULT_TIMEOUT,
                             enabled: bool = False,
                             config: Optional[SolverConfig] = None,
                             in_url: str = DEFAULT_2CAPTCHA_IN,
                             res_url: str = DEFAULT_2CAPTCHA_RES,
                             poll_interval: float = DEFAULT_POLL_INTERVAL,
                             max_poll: int = DEFAULT_MAX_POLL,
                             env: Optional[Dict[str, str]] = None,
                             ) -> SolveResult:
    """同 :func:`solve_recaptcha_token`，返回完整结果。"""
    t0 = time.perf_counter()
    cfg = _resolve_config(config, enabled=enabled, api_url=in_url,
                          api_key=api_key, timeout=timeout, mode=MODE_MULTIPART,
                          env=env)
    cfg.poll_interval = float(poll_interval)
    cfg.max_poll = int(max_poll)

    if not cfg.active:
        return SolveResult(
            ok=False,
            reason="solver_disabled" if not cfg.enabled else "solver_no_api_key",
            elapsed=time.perf_counter() - t0)
    if not str(site_key or "").strip():
        return SolveResult(ok=False, reason="missing_site_key",
                           elapsed=time.perf_counter() - t0)
    if not str(page_url or "").strip():
        return SolveResult(ok=False, reason="missing_page_url",
                           elapsed=time.perf_counter() - t0)

    attempts = 0

    # ---- 第 1 步：下单 ----
    try:
        fields = {
            "key": cfg.api_key,
            "method": "userrecaptcha",
            "googlekey": str(site_key),
            "pageurl": str(page_url),
            "json": 1,
        }
        body, bd = build_multipart_body(fields)
        headers = _headers(cfg, "multipart/form-data; boundary=%s" % bd)
        text = _post(in_url, body, headers, cfg.timeout)
        attempts += 1
    except Exception as exc:      # noqa: BLE001
        return SolveResult(ok=False,
                           reason="submit_failed:%s: %s"
                                  % (type(exc).__name__, str(exc)[:120]),
                           elapsed=time.perf_counter() - t0, attempts=attempts)

    first = parse_response(text)
    if not first.ok:
        first.elapsed = time.perf_counter() - t0
        first.attempts = attempts
        return first

    task_id = first.text
    if not task_id:
        return SolveResult(ok=False, reason="no_task_id",
                           elapsed=time.perf_counter() - t0, attempts=attempts)

    # ---- 第 2 步：轮询 ----
    poll = max(1, int(cfg.max_poll))
    interval = max(0.0, float(cfg.poll_interval))
    last_reason = "timeout"

    for i in range(poll):
        if interval:
            _sleep(interval)
        try:
            qs = urllib.parse.urlencode({
                "key": cfg.api_key, "action": "get", "id": task_id, "json": 1,
            })
            url = "%s?%s" % (res_url, qs)
            text = _get(url, _headers(cfg, ""), cfg.timeout)
            attempts += 1
        except Exception as exc:      # noqa: BLE001
            last_reason = "poll_failed:%s: %s" % (type(exc).__name__,
                                                  str(exc)[:100])
            continue      # 单次轮询失败不放弃 —— 网络抖动是常态

        r = parse_response(text)
        if r.ok:
            r.elapsed = time.perf_counter() - t0
            r.attempts = attempts
            return r
        if r.reason == "not_ready":
            last_reason = "not_ready"
            continue
        # 真错误（余额不足、key 错）—— 立刻退出，别白轮询
        r.elapsed = time.perf_counter() - t0
        r.attempts = attempts
        return r

    return SolveResult(ok=False,
                       reason="poll_exhausted:%s" % last_reason,
                       elapsed=time.perf_counter() - t0, attempts=attempts)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _resolve_config(config: Optional[SolverConfig], *,
                    enabled: bool, api_url: str, api_key: str,
                    timeout: float, mode: str,
                    env: Optional[Dict[str, str]] = None) -> SolverConfig:
    """把参数和环境变量合成一个配置。**显式参数 > 环境变量。**

    显式传了 ``config`` 就直接用它（调用方已经想清楚了）。
    """
    if config is not None:
        return config
    base = SolverConfig.from_env(env)
    # 显式参数覆盖环境变量（传了才算显式）
    if enabled:
        base.enabled = True
    if api_url:
        base.api_url = str(api_url)
    if api_key:
        base.api_key = str(api_key)
    if timeout:
        base.timeout = float(timeout)
    if mode:
        base.mode = str(mode)
    return base


def _coerce_image(image_data: Any) -> Tuple[bytes, str]:
    """把三种输入统一成 bytes。返回 ``(blob, 错误原因)``。"""
    d = image_data
    if d is None or d == b"" or d == "":
        return b"", "empty_image"

    if isinstance(d, (bytes, bytearray)):
        return bytes(d), ""

    if isinstance(d, str):
        s = d.strip()
        # 看起来像 base64？
        if s.startswith("data:"):
            _, _, s = s.partition(",")
        if len(s) > 256 and _looks_like_b64(s):
            return s.encode("ascii"), ""
        # 当作文件路径
        try:
            if os.path.isfile(s):
                with open(s, "rb") as f:
                    return f.read(), ""
        except Exception as exc:      # noqa: BLE001
            return b"", "image_read_failed:%s" % type(exc).__name__
        # 短字符串 —— 可能是小的 base64，也可能是路径写错
        if _looks_like_b64(s):
            return s.encode("ascii"), ""
        return b"", "image_not_found:%s" % s[:80]

    return b"", "image_type_bad:%s" % type(d).__name__


def _looks_like_b64(s: str) -> bool:
    """粗略判断是不是 base64。**只用来分流，不用来做校验。**"""
    if not s or len(s) < 8:
        return False
    ok = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz0123456789+/=\r\n")
    sample = s[:512]
    return all(c in ok for c in sample)


def describe_solver(config: Optional[SolverConfig] = None) -> str:
    """给人看的打码平台配置说明 + 怎么打开它。"""
    cfg = config if config is not None else SolverConfig.from_env()
    lines = [cfg.describe(), ""]
    if not cfg.active:
        lines += [
            "启用方式（三选一）：",
            "",
            "  1) 代码：",
            "       from avatar.risk_detector.solver import solve_captcha_via_api",
            "       solve_captcha_via_api(api_url=..., api_key=...,",
            "                             image_data=png, enabled=True)",
            "",
            "  2) 环境变量（推荐，避免 key 进代码）：",
            "       set %s=1" % ENV_ENABLED,
            "       set %s=your_key" % ENV_API_KEY,
            "       set %s=https://api.2captcha.com/in.php" % ENV_API_URL,
            "",
            "  3) 自检：python -m avatar.risk_detector.solver",
            "",
            "⚠ 默认关闭是**刻意**的：打码要花钱，且会把截图发到第三方。",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """solver 自检。**全程用假 http 函数，一个字节都不出网。**

    ``quick=True``（默认）：无网络、无文件、无等待。
    """
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ⚠ 走外部调用方路径（python -m 时模块有两份，裸名会绕过打桩）
    _me = _sys.modules[__name__]

    # ============ 1) 常量与契约 ============
    _add("DEFAULT_TIMEOUT 是正数", _me.DEFAULT_TIMEOUT > 0,
         "%.1fs" % _me.DEFAULT_TIMEOUT)
    _add("三种编码模式齐全",
         {_me.MODE_AUTO, _me.MODE_MULTIPART, _me.MODE_JSON}
         == {"auto", "multipart", "json"}, "auto/multipart/json")
    _add("环境变量名固定",
         _me.ENV_ENABLED == "AVATAR_SOLVER_ENABLED"
         and _me.ENV_API_KEY == "AVATAR_SOLVER_API_KEY",
         "%s / %s" % (_me.ENV_ENABLED, _me.ENV_API_KEY))
    _add("默认 User-Agent 非空", bool(_me.DEFAULT_USER_AGENT),
         _me.DEFAULT_USER_AGENT[:40])
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("顶层不 import 任何打码 SDK",
         not any(m in _sys.modules for m in
                 ("twocaptcha", "capsolver", "anticaptcha")),
         "只用标准库 urllib")

    # ============ 2) 默认关闭（硬约束 #1）============
    _add("★ 空配置 → is_enabled=False",
         _me.is_enabled(_me.SolverConfig()) is False, "默认关")
    _add("★ enabled=True 但没 api_key → 仍然 False",
         _me.is_enabled(_me.SolverConfig(enabled=True)) is False,
         "必须两个条件都满足")
    _add("★ 有 api_key 但 enabled=False → 仍然 False",
         _me.is_enabled(_me.SolverConfig(api_key="k")) is False,
         "必须显式打开")
    _add("★ enabled=True + api_key → True",
         _me.is_enabled(_me.SolverConfig(enabled=True, api_key="k")) is True,
         "配置完整")
    _add("SolverConfig.active 与 is_enabled 一致",
         all(_me.is_enabled(c) == c.active for c in (
             _me.SolverConfig(),
             _me.SolverConfig(enabled=True),
             _me.SolverConfig(api_key="k"),
             _me.SolverConfig(enabled=True, api_key="k"))),
         "两个入口不许分叉")

    # 环境变量
    _add("环境变量不设 → 关闭",
         _me.SolverConfig.from_env({}).active is False, "{}")
    _add("环境变量 AVATAR_SOLVER_ENABLED=1 + key → 启用",
         _me.SolverConfig.from_env({
             _me.ENV_ENABLED: "1", _me.ENV_API_KEY: "k"}).active is True,
         "1 + k")
    _add("enabled=yes/on/true 都认",
         all(_me.SolverConfig.from_env(
             {_me.ENV_ENABLED: v, _me.ENV_API_KEY: "k"}).active
             for v in ("yes", "on", "true", "TRUE", "Y")),
         "宽松解析")
    _add("enabled=0/no 不认",
         _me.SolverConfig.from_env(
             {_me.ENV_ENABLED: "0", _me.ENV_API_KEY: "k"}).active is False,
         "0")
    _add("from_env 不抛异常",
         isinstance(_me.SolverConfig.from_env({"x": "y"}), _me.SolverConfig),
         "宽容")

    # ============ 3) 脱敏 ============
    cfg_full = _me.SolverConfig(enabled=True, api_key="abcdefgh12345678")
    d = cfg_full.to_dict()
    _add("★ to_dict 默认脱敏 api_key",
         "abcdefgh12345678" not in json.dumps(d), d["api_key"])
    _add("脱敏后仍能看出前后缀",
         d["api_key"].startswith("abcd") and d["api_key"].endswith("78"),
         d["api_key"])
    _add("短 key 全打码",
         _me.SolverConfig(api_key="short").to_dict()["api_key"] == "***",
         "***")
    _add("redact=False 才给明文",
         _me.SolverConfig(api_key="abcdefgh12345678")
         .to_dict(redact=False)["api_key"] == "abcdefgh12345678",
         "显式要求")
    _add("describe 不含明文 key",
         "abcdefgh12345678" not in cfg_full.describe(), "已脱敏")

    # ============ 4) 请求体构造（纯函数）============
    body, bd = _me.build_multipart_body({"a": "1", "b": "2"},
                                        boundary="XBOUND")
    _add("multipart：boundary 出现在头和尾",
         body.startswith(b"--XBOUND") and body.rstrip().endswith(b"--XBOUND--"),
         "首尾都有")
    _add("multipart：字段名和值都在",
         b'name="a"' in body and b"1" in body
         and b'name="b"' in body and b"2" in body, "两个字段")
    _add("multipart：用 CRLF 分隔",
         b"\r\n" in body, "标准要求")
    _add("multipart：返回的 boundary 与内容一致",
         bd == "XBOUND", bd)
    _add("multipart：不给 boundary 时自动生成",
         len(_me.build_multipart_body({"a": "1"})[1]) > 8, "自动")

    body2, _ = _me.build_multipart_body(
        {}, files={"body": b"\x89PNG\r\n"})
    _add("★ multipart：文件字段走 base64",
         base64.b64encode(b"\x89PNG\r\n") in body2, "iVBORw0K...")

    jb = _me.build_json_body({"key": "k", "n": 1})
    _add("json：可解析", json.loads(jb.decode("utf-8")) == {"key": "k", "n": 1},
         jb[:40].decode("utf-8", "replace"))
    _add("json：ensure_ascii=False（中文不转义）",
         "验证" in _me.build_json_body({"t": "验证"}).decode("utf-8"),
         "中文保留")
    _add("★ json：bytes 自动 base64",
         json.loads(_me.build_json_body({"img": b"\x89PNG"}
                                        ).decode("utf-8"))["img"]
         == base64.b64encode(b"\x89PNG").decode("ascii"),
         "自动编码")
    _add("json：嵌套结构也处理",
         json.loads(_me.build_json_body(
             {"a": {"b": b"\x01"}}).decode("utf-8"))["a"]["b"]
         == base64.b64encode(b"\x01").decode("ascii"), "递归")

    # ============ 5) 编码选择 ============
    _add("pick_mode：capsolver → json",
         _me.pick_mode("https://api.capsolver.com/createTask") == "json",
         "capsolver")
    _add("pick_mode：2captcha → multipart",
         _me.pick_mode("https://2captcha.com/in.php") == "multipart",
         "2captcha")
    _add("pick_mode：显式覆盖优先",
         _me.pick_mode("https://api.capsolver.com/x", "multipart")
         == "multipart", "显式赢")
    _add("pick_mode：空 url → multipart（保守默认）",
         _me.pick_mode("") == "multipart", "默认")

    # ============ 6) 响应解析（两家格式）============
    r = _me.parse_response('{"status": 1, "request": "03AGdBq26abc"}')
    _add("2Captcha 成功", r.ok and r.text == "03AGdBq26abc", r.describe())

    r = _me.parse_response('{"status": 0, "request": "ERROR_WRONG_USER_KEY"}')
    _add("2Captcha 失败（key 错）",
         (not r.ok) and "ERROR_WRONG_USER_KEY" in r.reason, r.reason)

    r = _me.parse_response('{"status": 0, "request": "CAPCHA_NOT_READY"}')
    _add("★ CAPCHA_NOT_READY 归为 not_ready（不是错误）",
         (not r.ok) and r.reason == "not_ready", r.reason)

    r = _me.parse_response("OK|token123")
    _add("纯文本 OK| 形式", r.ok and r.text == "token123", r.text)

    r = _me.parse_response("ERROR_CAPTCHA_UNSOLVABLE")
    _add("纯文本 ERROR_ 形式",
         (not r.ok) and "ERROR_CAPTCHA_UNSOLVABLE" in r.reason, r.reason)

    r = _me.parse_response(
        '{"errorId": 0, "solution": {"gRecaptchaResponse": "TOK"}}')
    _add("CapSolver 成功", r.ok and r.text == "TOK", r.text)

    r = _me.parse_response(
        '{"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE",'
        ' "errorDescription": "no money"}')
    _add("CapSolver 失败",
         (not r.ok) and "ERROR_ZERO_BALANCE" in r.reason, r.reason)

    _add("空响应 → 失败不崩",
         _me.parse_response("").ok is False, "empty")
    _add("坏 JSON → 失败不崩",
         _me.parse_response("{not json").ok is False, "bad_json")
    _add("JSON 数组（非对象）→ 失败不崩",
         _me.parse_response("[1,2]").ok is False, "json_not_object")
    _add("status=success 也认",
         _me.parse_response('{"status": "success", "solution": "s"}').ok,
         "宽松")

    # ============ 7) SolveResult ============
    sr = _me.SolveResult(ok=True, solution={"gRecaptchaResponse": "T"})
    _add("SolveResult.text 能从 dict 取 token", sr.text == "T", sr.text)
    _add("SolveResult 序列化 JSON 安全",
         json.dumps(sr.to_dict()) != "", "ok")
    _add("SolveResult 失败也有可读 describe",
         "失败" in _me.SolveResult(reason="x").describe(), "失败")

    # ============ 8) 图片输入三种形态 ============
    blob, bad = _me._coerce_image(b"\x89PNG")
    _add("bytes 直传", blob == b"\x89PNG" and not bad, "bytes")
    blob, bad = _me._coerce_image(base64.b64encode(b"x" * 400).decode())
    _add("长 base64 字符串识别为 base64", not bad and len(blob) > 100, "b64")
    blob, bad = _me._coerce_image("data:image/png;base64,"
                                  + base64.b64encode(b"y" * 400).decode())
    _add("data URI 前缀被剥掉", not bad, "data:")
    blob, bad = _me._coerce_image(b"")
    _add("空输入 → empty_image", bad == "empty_image", bad)
    blob, bad = _me._coerce_image(None)
    _add("None → empty_image", bad == "empty_image", bad)
    blob, bad = _me._coerce_image(12345)
    _add("类型不对 → image_type_bad",
         bad.startswith("image_type_bad"), bad)

    # ============ 9) 不配置就绝不发请求（硬约束 #1）============
    sent: List[Any] = []

    def _spy_post(url, body, headers, timeout):
        sent.append(("POST", url, body, headers, timeout))
        return '{"status": 1, "request": "SHOULD_NOT_HAPPEN"}'

    def _spy_get(url, headers, timeout):
        sent.append(("GET", url, headers, timeout))
        return '{"status": 1, "request": "SHOULD_NOT_HAPPEN"}'

    saved_post, saved_get = _me._http_post, _me._http_get
    _me._http_post = _spy_post
    _me._http_get = _spy_get
    try:
        # --- 默认关闭 ---
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "KEY", b"\x89PNG",
            enabled=False, env={})
        _add("★ 默认关闭：solve_captcha_via_api 返回 None",
             r is None, repr(r))
        _add("★★ 默认关闭：一个请求都没发出去",
             sent == [], "sent=%r" % (sent,))

        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "KEY", b"\x89PNG",
            enabled=True, env={})       # enabled 开了但没 key？
        _add("★ enabled=True + api_key 传了 → 会发（这条应该发）",
             r is not None and len(sent) == 1, "sent=%d" % len(sent))

        # 只有 enabled 没有 key
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "", b"\x89PNG",
            enabled=True, env={})
        _add("★★ 只有 enabled、没 key → 返回 None 且不发",
             r is None and sent == [], "sent=%r" % (sent,))

        # 只有 key 没有 enabled
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "KEY", b"\x89PNG",
            enabled=False, env={})
        _add("★★ 只有 key、没 enabled → 返回 None 且不发",
             r is None and sent == [], "sent=%r" % (sent,))

        # 没 api_url
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "", "KEY", b"\x89PNG", enabled=True, env={})
        _add("★ 没 api_url → 返回 None 且不发",
             r is None and sent == [], "sent=%r" % (sent,))

        # --- 环境变量打开 ---
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "", "", b"\x89PNG", enabled=False, env={
                _me.ENV_ENABLED: "1",
                _me.ENV_API_KEY: "ENVKEY",
                _me.ENV_API_URL: "https://2captcha.com/in.php"})
        _add("★ 环境变量配全 → 启用并发请求",
             len(sent) == 1, "sent=%d" % len(sent))

        # ============ 10) 请求构造（硬约束：必须有独立测试）============
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "MYKEY", b"\x89PNG_DATA",
            timeout=33.0, enabled=True, env={})
        _add("请求真的发出去了", len(sent) == 1 and sent[0][0] == "POST",
             "POST")
        _add("★ 请求带 timeout（硬约束 #2）",
             len(sent) == 1 and sent[0][4] == 33.0,
             "timeout=%s" % (sent[0][4] if sent else None))
        hdrs = sent[0][3] if sent else {}
        _add("★ 请求带 User-Agent",
             "User-Agent" in hdrs and hdrs["User-Agent"], 
             hdrs.get("User-Agent", "")[:30])
        _add("★ Content-Type 是 multipart 且 boundary 一致",
             hdrs.get("Content-Type", "").startswith(
                 "multipart/form-data; boundary="), hdrs.get("Content-Type", ""))
        body = sent[0][2] if sent else b""
        _add("★ 请求体带 api_key", b"MYKEY" in body, "key 在")
        _add("★ 请求体带 base64 的图",
             base64.b64encode(b"\x89PNG_DATA") in body, "图在")
        _add("请求体带 method=base64（2Captcha 要求）",
             b"method" in body and b"base64" in body, "method")
        _add("api_key 只在请求体，不在 URL",
             "MYKEY" not in sent[0][1], sent[0][1])

        # JSON 模式（capsolver）
        sent[:] = []
        r = _me.solve_captcha_via_api(
            "https://api.capsolver.com/createTask", "CK", b"\x89PNG",
            enabled=True, env={})
        hdrs = sent[0][3] if sent else {}
        _add("★ capsolver 走 JSON",
             hdrs.get("Content-Type") == "application/json",
             hdrs.get("Content-Type", ""))
        _add("JSON 体可解析且含 key",
             json.loads(sent[0][2].decode("utf-8")).get("key") == "CK",
             "key 在")

        # extra_fields 合并
        sent[:] = []
        _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, extra_fields={"soft_id": "42"}, env={})
        _add("extra_fields 合并进请求体",
             b"soft_id" in sent[0][2] and b"42" in sent[0][2], "合并")

        # ============ 11) reCAPTCHA 流程 ============
        sent[:] = []
        calls = {"n": 0}

        def _recaptcha_post(url, body, headers, timeout):
            sent.append(("POST", url, body))
            return '{"status": 1, "request": "TASKID123"}'

        def _recaptcha_get(url, headers, timeout):
            calls["n"] += 1
            sent.append(("GET", url, b""))
            if calls["n"] < 3:
                return '{"status": 0, "request": "CAPCHA_NOT_READY"}'
            return '{"status": 1, "request": "TOKEN_ABC"}'

        _me._http_post = _recaptcha_post
        _me._http_get = _recaptcha_get
        tok = _me.solve_recaptcha_token(
            "SITEKEY6Lc", "https://target.example/login", "RKEY",
            enabled=True, poll_interval=0.0, env={})
        _add("★ reCAPTCHA 成功拿到 token", tok == "TOKEN_ABC", repr(tok))
        _add("★ reCAPTCHA 轮询了 3 次（前两次 not_ready）",
             calls["n"] == 3, "n=%d" % calls["n"])
        _add("★ reCAPTCHA 请求带 User-Agent 和 timeout",
             len(sent) >= 1, "%d 个请求" % len(sent))
        _add("★ reCAPTCHA 下单带 googlekey 和 pageurl",
             b"SITEKEY6Lc" in sent[0][2] and b"target.example" in sent[0][2],
             "sitekey+pageurl")

        # 轮询耗尽
        calls["n"] = 0
        sent[:] = []
        _me._http_get = lambda u, h, t: (
            calls.__setitem__("n", calls["n"] + 1)
            or '{"status": 0, "request": "CAPCHA_NOT_READY"}')
        tok = _me.solve_recaptcha_token(
            "SK", "https://x.com", "K", enabled=True,
            poll_interval=0.0, max_poll=3, env={})
        _add("★ 轮询耗尽 → 返回 None（不抛）", tok is None, repr(tok))
        _add("轮询次数不超过 max_poll", calls["n"] == 3, "n=%d" % calls["n"])

        # 真错误立刻退出
        calls["n"] = 0
        _me._http_get = lambda u, h, t: (
            calls.__setitem__("n", calls["n"] + 1)
            or '{"status": 0, "request": "ERROR_ZERO_BALANCE"}')
        tok = _me.solve_recaptcha_token(
            "SK", "https://x.com", "K", enabled=True,
            poll_interval=0.0, max_poll=10, env={})
        _add("★ 真错误（余额不足）立刻退出，不白轮询",
             tok is None and calls["n"] == 1, "n=%d" % calls["n"])

        # 默认关闭
        sent[:] = []
        tok = _me.solve_recaptcha_token("SK", "https://x.com", "K",
                                        enabled=False, env={})
        _add("★★ reCAPTCHA 默认关闭 → None 且不发请求",
             tok is None and sent == [], "sent=%r" % (sent,))

        _add("reCAPTCHA 缺 site_key → None",
             _me.solve_recaptcha_token("", "https://x.com", "K",
                                       enabled=True, poll_interval=0.0,
                                       env={}) is None, "site_key")
        _add("reCAPTCHA 缺 page_url → None",
             _me.solve_recaptcha_token("SK", "", "K", enabled=True,
                                       poll_interval=0.0, env={}) is None,
             "page_url")

        # ============ 12) 失败路径不抛异常（硬约束 #2）============
        def _boom(url, body, headers, timeout):
            raise OSError("网络断了")

        _me._http_post = _boom
        got = None
        try:
            got = _me.solve_captcha_via_api(
                "https://2captcha.com/in.php", "K", b"\x89PNG",
                enabled=True, env={})
            raised = False
        except BaseException:      # noqa: BLE001
            raised = True
        _add("★★ 网络异常 → 返回 None，不抛", (not raised) and got is None,
             "None")

        # 超时异常
        def _timeout(url, body, headers, timeout):
            raise TimeoutError("超时了")

        _me._http_post = _timeout
        got = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, env={})
        _add("★★ 超时 → 返回 None，不抛", got is None, "None")

        # 平台返回错误
        _me._http_post = lambda u, b, h, t: '{"status": 0, "request": "ERROR_X"}'
        got = _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, env={})
        _add("平台报错 → 返回 None", got is None, "None")

        # errors 列表收集原因
        errs: List[str] = []
        _me.solve_captcha_via_api(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, errors=errs, env={})
        _add("errors 列表收到失败原因",
             bool(errs) and "solver" in errs[0], errs[:1])

        # ============ 13) 详情结果 ============
        _me._http_post = lambda u, b, h, t: '{"status": 1, "request": "OKSOL"}'
        det = _me.solve_captcha_detailed(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=True, env={})
        _add("detailed 成功：ok/attempts/elapsed 都有",
             det.ok and det.attempts == 1 and det.elapsed >= 0,
             det.describe())
        _add("detailed 结果 JSON 安全", json.dumps(det.to_dict()) != "", "ok")

        det = _me.solve_captcha_detailed(
            "https://2captcha.com/in.php", "K", b"\x89PNG",
            enabled=False, env={})
        _add("detailed 关闭时 reason=solver_disabled",
             (not det.ok) and det.reason == "solver_disabled", det.reason)

        det = _me.solve_captcha_detailed(
            "https://2captcha.com/in.php", "", b"\x89PNG",
            enabled=True, env={})
        _add("detailed 缺 key 时 reason=solver_no_api_key",
             (not det.ok) and det.reason == "solver_no_api_key", det.reason)

        # ============ 14) 不碰全局 time.sleep ============
        real_sleep = time.sleep
        _me._http_get = lambda u, h, t: '{"status": 1, "request": "T"}'
        _me.solve_recaptcha_token("SK", "https://x.com", "K", enabled=True,
                                  poll_interval=0.0, env={})
        _add("★ 全局 time.sleep 未被改动（打桩的是 _sleep 别名）",
             time.sleep is real_sleep, "原装")

    finally:
        _me._http_post, _me._http_get = saved_post, saved_get

    # ============ 15) 顶层不 import 重模块 ============
    import ast as _ast
    try:
        src = open(_me.__file__, encoding="utf-8").read()
        tree = _ast.parse(src)
    except Exception as exc:      # noqa: BLE001
        _add("能读源码", False, str(exc))
        tree = None

    if tree is not None:
        top_mods = set()
        for node in tree.body:      # 只看顶层
            if isinstance(node, _ast.Import):
                for a in node.names:
                    top_mods.add((a.name or "").split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                top_mods.add((node.module or "").split(".")[0])
        _add("★ 顶层不 import PIL/cv2/numpy",
             not (top_mods & {"PIL", "cv2", "numpy"}), sorted(top_mods))
        _add("顶层不 import 本包其他模块（零依赖）",
             not (top_mods & {"status", "fingerprints", "guard",
                              "context", "resume", "detector"}), 
             "solver 可独立使用")
        _add("只用标准库", top_mods <= {
            "base64", "json", "os", "time", "urllib", "uuid", "typing",
            "__future__", "sys"}, sorted(top_mods))

    # ============ 16) describe ============
    _add("describe_solver 不抛",
         isinstance(_me.describe_solver(_me.SolverConfig()), str), "有文本")
    _add("未启用时 describe 给出启用步骤",
         "启用方式" in _me.describe_solver(_me.SolverConfig()), "有指引")
    _add("已启用时 describe 不再提示启用步骤",
         "启用方式" not in _me.describe_solver(
             _me.SolverConfig(enabled=True, api_key="k")), "已启用")

    elapsed = time.perf_counter() - t0
    return {
        "module": "solver",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_solver())
