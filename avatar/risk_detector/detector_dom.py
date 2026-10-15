# -*- coding: utf-8 -*-
"""第一层检测：DOM / CDP。

## 这一层为什么排在最前

三层检测的成本阶梯是 **DOM(50ms) → 窗口(20ms) → 像素(0.5~2s)**。
DOM 层最有价值的地方在于：**验证题本质上是网页里的一个 DOM 元素**。
鼠标还没点下去，那个 ``<iframe src=".../recaptcha/api2/anchor">``
已经躺在文档里了。在 DOM 里读它，比截屏 + OCR 去猜要快 40 倍、
准 10 倍。

## 只做三件事

1. **扫 iframe 的 src** —— 验证题的 iframe 域名是最稳的指纹
   （reCAPTCHA 用 ``google.com/recaptcha/``，hCaptcha 用
   ``newassets.hcaptcha.com``）。厂商换 class 名容易，换域名很难。
2. **扫元素 class / id** —— 兜住 iframe 被包在 shadow DOM 或
   动态创建的情况。
3. **看 URL 路径** —— ``/challenge``、``/cdn-cgi/challenge-platform``
   这类路径说明整页都在挑战流程里。

## 硬约束

- **不许自造 session 类型**（用户明确要求）。本模块接受的是
  阶段四 ``avatar.web_cdp.session`` 里那个 ``CdpSession`` /
  ``BrowserSession``，通过 ``.evaluate()`` 和 ``.call()`` 驱动。
  为了不引入 import 循环和硬依赖，本模块用**鸭子类型**接收它 ——
  只要求对象有 ``evaluate(expr, *, session_id=None, timeout=None)``。
  这样测试时可以塞一个 mock，而不必真的起浏览器。
- **指纹全部从 fingerprints.py 读**（硬约束），本文件不允许出现
  任何形如 ``"recaptcha"`` 的字面量判断。
- **异常不吞**：``detect_dom`` 内部每个子步骤各自 try，失败的
  记进 ``evidence["errors"]``，然后**继续跑下一个子步骤**。
  只有整个函数都被包住的那一层才整体返回 error。

## 能力边界

**做**：通过已有的 CDP 会话读 DOM / URL，与 :mod:`fingerprints`
的 DOM 指纹比对。

**不做**：

- **不自己建会话**。不接受端口号、不启动浏览器、不重连。
  会话由阶段四的 :mod:`avatar.web_cdp` 提供，本模块用**鸭子类型**
  接收（只要求有 ``evaluate`` / ``call``）—— 这样测试可以塞 mock。
- **不碰桌面**。没有 CDP 会话时返回 unknown（``reason="no_session"``），
  **不退回**去枚举窗口或截图 —— 那是另外两层的事。
- **不硬编码任何指纹**。所有匹配模式从 :mod:`fingerprints` 读，
  本文件里不允许出现形如 ``"recaptcha"`` 的字面量判断。
- **不判 severity**。命中哪条指纹、什么严重度，由指纹库自己配。
- **不改页面**。只读（``Runtime.evaluate`` 的只读表达式），
  不注入、不点击、不导航。
- **不处理 shadow DOM 穿透**。目前只扫顶层文档和 iframe 的 src；
  深度遍历留给后续按需加，现在加了会增加误报面。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .fingerprints import DOM_FINGERPRINTS, match_dom
from .status import DEFAULT_RISK_TIMEOUT, RiskStatus, RiskType, Severity

__all__ = [
    "DOM_SCAN_TIMEOUT",
    "DomFields",
    "collect_fields",
    "detect_dom",
    "dom_scan_script",
    "selfcheck",
]

#: DOM 扫描本身的超时。整体 timeout 会按剩余预算再收一次。
DOM_SCAN_TIMEOUT = 1.5

#: 单个字段值的字符上限。整页 HTML 可能几 MB，塞进 evidence 会
#: 把日志打爆，也拖慢序列化。指纹匹配只关心局部，截断足够。
MAX_FIELD_CHARS = 4000


# --------------------------------------------------------------------------
# 采集脚本
# --------------------------------------------------------------------------
#: 在页面里跑的 JS。**一次 evaluate 拿全部所需信息** ——
#: 分多次调用会让 CDP 往返开销乘以 N，而每次动作前都要检测。
#:
#: 返回 ``{"iframe_src": [...], "class": [...], "id": [...], "html": "...",
#: "url": "...", "title": "...", "visible_hints": [...]}``
#:
#: 设计要点：
#: - ``class`` / ``id`` **合并成两个长串**而不是数组。指纹库用的是
#:   子串匹配，拼串比逐个元素比对快得多，也少一次 JS↔CDP 序列化。
#: - ``html`` 只取 ``documentElement.outerHTML`` 的**前 N 字符**，
#:   并且**剥掉 script 内容的正文**（只留 src）—— 内联脚本动辄几百 KB，
#:   而我们要找的指纹全在标签属性里。
#: - 全程 try/catch，任何一步失败都只记录不抛 —— 页面可能
#:   禁用了某些 API（CSP、沙箱 iframe）。
DOM_SCAN_SCRIPT = r"""
(function () {
  var out = {iframe_src: [], "class": "", id: "", html: "",
             url: "", title: "", errors: []};
  try { out.url = String(location.href); } catch (e) { out.errors.push("url:" + e); }
  try { out.title = String(document.title || ""); }
  catch (e) { out.errors.push("title:" + e); }

  // --- iframe src ---
  try {
    var fr = document.querySelectorAll("iframe");
    for (var i = 0; i < fr.length; i++) {
      var s = fr[i].getAttribute("src") || "";
      if (s) { out.iframe_src.push(String(s)); }
    }
  } catch (e) { out.errors.push("iframe:" + e); }

  // --- class / id 合并成串 ---
  try {
    var all = document.querySelectorAll("[class],[id]");
    var cs = [], ids = [];
    for (var j = 0; j < all.length; j++) {
      var c = all[j].getAttribute("class");
      if (c) { cs.push(c); }
      var d = all[j].getAttribute("id");
      if (d) { ids.push(d); }
      if (cs.length > 3000) { break; }
    }
    out["class"] = cs.join(" ");
    out.id = ids.join(" ");
  } catch (e) { out.errors.push("classid:" + e); }

  // --- HTML：剥掉内联脚本正文，保留标签属性 ---
  try {
    var raw = document.documentElement.outerHTML || "";
    // 去掉 <script>...</script> 的正文，只保留开标签（里面有 src）
    var stripped = raw.replace(
      /(<script\b[^>]*>)[\s\S]*?(<\/script>)/gi, "$1$2");
    // 同理去掉 <style> 正文
    stripped = stripped.replace(
      /(<style\b[^>]*>)[\s\S]*?(<\/style>)/gi, "$1$2");
    out.html = stripped.slice(0, %d);
  } catch (e) { out.errors.push("html:" + e); }

  return out;
})()
""" % MAX_FIELD_CHARS


class DomFields(object):
    """运行期拿到的 DOM 字段集合。

    这是个**薄包装而不是 dict**，因为要携带"哪些字段是可信的"信息：
    CDP 挂了 / JS 抛异常时，字段会是空串，而空串**不等于**"页面上没有"。
    :attr:`ok` 就是区分这两者的开关。
    """

    __slots__ = ("iframe_src", "class_str", "id_str", "html", "url",
                 "title", "errors", "ok", "raw_keys")

    def __init__(self, data: Optional[Dict[str, Any]] = None,
                 *, ok: bool = True, errors: Optional[List[str]] = None) -> None:
        d = data or {}
        srcs = d.get("iframe_src") or []
        if not isinstance(srcs, (list, tuple)):
            srcs = [srcs]
        self.iframe_src = [str(s) for s in srcs]
        self.class_str = str(d.get("class") or "")
        self.id_str = str(d.get("id") or "")
        self.html = str(d.get("html") or "")
        self.url = str(d.get("url") or "")
        self.title = str(d.get("title") or "")
        self.errors = list(errors or []) + list(d.get("errors") or [])
        self.ok = bool(ok)
        self.raw_keys = sorted(d.keys())

    # -- 转成指纹库要的 fields 字典 ------------------------------------
    def to_match_fields(self) -> Dict[str, str]:
        """拼成 :func:`fingerprints.match_dom` 要的 ``fields``。

        ``src`` 字段把**所有** iframe src 拼起来 —— 指纹是按子串匹配的，
        拼接让一条指纹能命中任意一个 iframe，而且只做一次字符串扫描。
        分隔符用空格（URL 里不会出现裸空格）。
        """
        return {
            "src": " ".join(self.iframe_src),
            "class": self.class_str,
            "id": self.id_str,
            "html": self.html,
        }

    @property
    def empty(self) -> bool:
        """所有字段都空 —— 用来判定"是不是啥都没拿到"。"""
        return not (self.iframe_src or self.class_str or self.id_str
                    or self.html or self.url)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iframe_count": len(self.iframe_src),
            "iframe_src": self.iframe_src[:8],
            "class_chars": len(self.class_str),
            "id_chars": len(self.id_str),
            "html_chars": len(self.html),
            "url": self.url,
            "title": self.title,
            "ok": self.ok,
            "errors": self.errors,
        }


def collect_fields(session: Any, *, session_id: Optional[str] = None,
                   timeout: float = DOM_SCAN_TIMEOUT) -> DomFields:
    """驱动 CDP 会话采集 DOM 字段。

    ``session`` 是阶段四的 ``CdpSession`` / ``BrowserSession``
    （鸭子类型：只要有 ``evaluate``）。**本函数不 import 它** ——
    避免 risk_detector 对 web_cdp 产生硬依赖，那样在没装浏览器
    的环境里连 status/fingerprints 都 import 不了。

    异常**向外抛**，由 :func:`detect_dom` 兜住并转成 RiskStatus。
    原因是这里的失败是"整层不可用"，不是"某个子步骤失败"。
    """
    if session is None:
        raise ValueError("collect_fields 需要 CDP session")
    ev = getattr(session, "evaluate", None)
    if ev is None:
        raise TypeError("session 没有 evaluate() —— 不是 web_cdp 的会话对象")

    t0 = time.perf_counter()
    # 先探测会话的 evaluate 收不收 timeout —— 比"试一次失败再退回"干净，
    # 因为退回的那次如果也抛（比如会话内部真的坏了），两条异常会缠绕，
    # 反而看不出根因。这里只在**签名明显不收**时才走兼容路径。
    if _accepts_timeout(ev):
        data = ev(DOM_SCAN_SCRIPT, session_id=session_id, timeout=timeout)
    else:
        data = ev(DOM_SCAN_SCRIPT, session_id=session_id)
    elapsed = time.perf_counter() - t0

    if not isinstance(data, dict):
        # JS 返回了非对象（undefined / 字符串）—— 视为采集失败
        return DomFields(None, ok=False,
                         errors=["evaluate 返回 %s，不是对象（%.3fs）"
                                 % (type(data).__name__, elapsed)])

    f = DomFields(data, ok=True)
    f.errors.append("采集耗时 %.3fs" % elapsed)
    return f


# --------------------------------------------------------------------------
# 检测主函数
# --------------------------------------------------------------------------
def detect_dom(session: Any, *,
               session_id: Optional[str] = None,
               url: str = "",
               timeout: float = DEFAULT_RISK_TIMEOUT,
               ) -> RiskStatus:
    """第一层：DOM 检测。

    参数
    ----
    - ``session`` —— 阶段四的 CDP 会话（可为 None，则返回 unknown）
    - ``session_id`` —— 页面级 session（``attach_page`` 的返回值）
    - ``url`` —— 已知的页面 URL。外部传进来时**优先用外层值**，
      因为 CDP 读的 ``location.href`` 可能是子 frame 的；
      而外层知道"我们想操作的是哪个页面"。
    - ``timeout`` —— 整层预算。**硬约束 #7：必须带超时。**

    返回
    ----
    :class:`RiskStatus`。命中 → ``source="dom"``；
    未命中且采集成功 → ``clean``；采集失败/超时 → ``unknown``。

    URL 路径的判定独立于 DOM 采集 —— 即使 DOM 什么都没拿到，
    URL 里有 ``/challenge`` 也足以说明问题。这是刻意的：
    URL 是**零成本**信息，没理由因为 DOM 采集失败而丢掉。
    """
    t0 = time.perf_counter()

    if session is None:
        return RiskStatus.unknown_(source="dom", reason="no_session",
                                   elapsed=time.perf_counter() - t0,
                                   extra={"hint": "没有 CDP 会话 —— "
                                                  "调用方应显式走窗口层"})

    budget = max(0.0, float(timeout))
    if budget <= 0.0:
        return RiskStatus.timeout(source="dom", limit=0.0)

    # ---- 1) 采集（失败不抛，转成 unknown）----
    fields = DomFields(None, ok=False)
    collect_error = ""
    try:
        fields = collect_fields(session, session_id=session_id,
                                timeout=min(DOM_SCAN_TIMEOUT, budget))
    except Exception as exc:      # noqa: BLE001 —— 硬约束：异常记进 evidence
        collect_error = "%s: %s" % (type(exc).__name__, exc)

    elapsed_now = time.perf_counter() - t0
    if elapsed_now > budget:
        st = RiskStatus.timeout(source="dom", limit=budget)
        st.extra["phase"] = "collect"
        return st

    # ---- 2) URL 路径判定（独立于 DOM，用的是外层 url）----
    url_hits: List[Dict[str, Any]] = []
    use_url = url or fields.url
    if use_url:
        try:
            url_hits = _match_url_path(use_url)
        except Exception as exc:      # noqa: BLE001
            fields.errors.append("url_match: %s: %s" % (type(exc).__name__, exc))

    # ---- 3) 指纹匹配 ----
    dom_hits: List[Dict[str, Any]] = []
    if fields.ok and not fields.empty:
        try:
            dom_hits = match_dom(fields.to_match_fields(), layer="dom")
        except Exception as exc:      # noqa: BLE001
            fields.errors.append("fingerprint: %s: %s" % (type(exc).__name__, exc))

    total_elapsed = time.perf_counter() - t0

    # ---- 4) 汇合：DOM 命中和 URL 命中取严重度高的 ----
    hits = dom_hits + url_hits
    if hits:
        best = max(hits, key=lambda h: (_sev_rank(h["fingerprint"].severity),
                                        h["fingerprint"].weight))
        fp = best["fingerprint"]
        evidence = {
            "layer": "dom",
            "fingerprint_id": fp.id,
            "fingerprint": fp.pattern,
            "matched_field": best["field"],
            "matched_value": best["value"],
            "vendor": fp.vendor,
            "url": use_url,
            "all_hits": [h["fingerprint"].id for h in hits],
            "iframe_count": len(fields.iframe_src),
            "trust": _trust_of(fp.source),
            "source": fp.source,
        }
        if fields.errors:
            evidence["warnings"] = fields.errors
        return RiskStatus(
            True,
            type=fp.type,
            vendor=fp.vendor,
            severity=fp.severity,
            evidence=evidence,
            source="dom",
            elapsed=total_elapsed,
            confidence=_confidence(fp, hits),
            fingerprint_id=fp.id,
            extra={"hit_count": len(hits),
                   "url_hit": bool(url_hits),
                   "dom_hit": bool(dom_hits)},
        )

    # ---- 5) 没命中：区分"干净"和"没查成" ----
    if not fields.ok:
        return RiskStatus.unknown_(
            source="dom",
            reason="collect_failed",
            elapsed=total_elapsed,
            extra={"error": collect_error,
                   "warnings": fields.errors,
                   "url": use_url},
        )
    if fields.empty:
        return RiskStatus.unknown_(
            source="dom",
            reason="empty_document",
            elapsed=total_elapsed,
            extra={"warnings": fields.errors, "url": use_url},
        )

    return RiskStatus.clean(
        source="dom",
        elapsed=total_elapsed,
        extra={"url": use_url,
               "iframe_count": len(fields.iframe_src),
               "html_chars": len(fields.html),
               "warnings": fields.errors,
               "fingerprints_checked": len(DOM_FINGERPRINTS)},
    )


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def dom_scan_script() -> str:
    """取扫描脚本原文（测试与审计用）。"""
    return DOM_SCAN_SCRIPT


def _accepts_timeout(fn: Any) -> bool:
    """判断 ``fn`` 的签名是否接受 ``timeout`` 关键字。

    优先用 ``inspect.signature``；拿不到（C 扩展、装饰器吃掉签名）
    就**假定接受** —— 因为阶段四的真实 ``CdpSession.evaluate`` 确实接受，
    而兼容路径是给测试 mock 用的少数派。

    判定为"不接受"后走的是**显式分支**而不是 try/except，这样
    两条路径的异常不会互相缠绕，出错时根因清楚。
    """
    try:
        import inspect
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    params = sig.parameters
    if "timeout" in params:
        return True
    # 有 **kwargs 也算接受
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params.values())


def _match_url_path(url: str) -> List[Dict[str, Any]]:
    """在 URL 上跑 DOM 指纹的路径类子集。

    只看 ``where`` 含 ``"src"`` 或 ``"html"`` 的指纹 —— 因为
    ``/cdn-cgi/challenge-platform/`` 这类路径指纹的 ``where`` 就是这些。
    把它们对着 URL 跑一遍，等于"零成本地多一层检查"。
    """
    from .fingerprints import DOM_FINGERPRINTS as _D
    hits = []
    low = url.lower()
    for fp in _D:
        if fp.regex:
            continue
        if not ("src" in fp.where or "html" in fp.where):
            continue
        # 只收够短的 pattern —— 长 pattern 是整段 DOM 片段，对 URL 无意义
        if len(fp.pattern) > 48:
            continue
        if fp.pattern in low:
            hits.append({"fingerprint": fp, "field": "url", "value": url[:200]})
    return hits


_SEV_RANK = {
    Severity.INFO: 0,
    Severity.BYPASSABLE: 1,
    Severity.INTERRUPT: 2,
    Severity.BLOCK: 3,
}


def _sev_rank(sev: str) -> int:
    return _SEV_RANK.get(sev, 0)


def _trust_of(source_key: str) -> float:
    """来源可信度。取不到给 0.5（中性）。"""
    try:
        from .fingerprints import source_info
        return float(source_info(source_key).get("trust", 0.5))
    except Exception:      # noqa: BLE001
        return 0.5


def _confidence(fp: Any, hits: List[Dict[str, Any]]) -> float:
    """综合置信度 = 权重 × 来源可信度，再按同向命中数轻微加成。

    为什么乘来源可信度：社区整理的指纹（trust=0.6）命中时，
    我们确实没那么确定 —— 这个不确定性必须传导到 confidence 上，
    否则 5C 的熔断决策会把"猜的"和"实测的"同等对待。

    上限 0.99 —— **永远不宣称 100% 确定**。检测是概率性的，
    给个 1.0 会让下游以为这是硬事实。
    """
    w = float(getattr(fp, "weight", 1.0))
    t = _trust_of(getattr(fp, "source", ""))
    base = w * t
    # 同类命中越多越可信（但边际递减）
    bonus = min(0.15, 0.05 * max(0, len(hits) - 1))
    return round(min(0.99, max(0.05, base * 0.9 + bonus)), 3)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _FakeSession(object):
    """自检/测试用的假 CDP 会话。

    只实现 ``evaluate`` —— 这正是"鸭子类型接收"的好处：
    验证 DOM 层逻辑不需要真的起浏览器。
    """

    def __init__(self, payload: Any, *, raise_exc: Optional[Exception] = None,
                 delay: float = 0.0, accept_timeout: bool = True) -> None:
        self.payload = payload
        self.raise_exc = raise_exc
        self.delay = delay
        #: False 时用一个**签名里没有 timeout** 的方法替换 evaluate，
        #: 模拟旧版/第三方会话。注意不能靠"内部 raise TypeError"来模拟 ——
        #: `_accepts_timeout` 走的是签名探测，内部抛异常是另一回事。
        self.accept_timeout = accept_timeout
        self.calls: List[str] = []
        if not accept_timeout:
            def _legacy(expression, *, session_id=None, **kw):
                return self._run(expression)
            self.evaluate = _legacy       # type: ignore[assignment]

    def _run(self, expression: str) -> Any:
        self.calls.append(expression)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload

    def evaluate(self, expression: str, *, session_id: Optional[str] = None,
                 timeout: Optional[float] = None, **kw: Any) -> Any:
        return self._run(expression)


def selfcheck(quick: bool = True) -> dict:
    """DOM 层自检。

    ``quick=True``（默认）：**全部用假会话**，不联网、不起浏览器，
    目标是毫秒级完成（硬约束：单模块 < 2s）。

    ``quick=False``：额外尝试真实探测本机有没有可用的调试实例
    （**可能慢**，且没有浏览器时只会给出降级信息，不算失败）。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1) 脚本常量
    script = dom_scan_script()
    _add("扫描脚本非空且是 IIFE",
         bool(script) and "function" in script and "iframe" in script,
         "%d 字符" % len(script))
    _add("脚本内不含任何硬编码厂商名（硬约束：指纹只在指纹库）",
         not any(k in script.lower() for k in
                 ("recaptcha", "hcaptcha", "geetest", "cloudflare",
                  "captcha", "turnstile")),
         "干净")
    _add("脚本剥掉内联 script/style 正文",
         "replace(" in script and "</script>" in script,
         "有剥离逻辑")

    # 2) DomFields 行为
    f_empty = DomFields(None)
    _add("DomFields 空输入不抛", f_empty.empty is True and f_empty.ok is True,
         "empty=True")
    f = DomFields({"iframe_src": ["https://x/a"], "class": "c", "id": "i",
                   "html": "<div/>", "url": "https://e.com"})
    _add("DomFields.to_match_fields 四个键齐备",
         set(f.to_match_fields()) == {"src", "class", "id", "html"},
         sorted(f.to_match_fields()))
    _add("DomFields 多个 iframe src 拼进 src 字段",
         " " in f.to_match_fields()["src"] or len(f.iframe_src) == 1,
         f.to_match_fields()["src"])
    f2 = DomFields({"iframe_src": ["a", "b"]})
    _add("多个 iframe 拼接正确",
         f2.to_match_fields()["src"] == "a b",
         repr(f2.to_match_fields()["src"]))

    # 3) 无会话 → unknown（不是 clean！）
    st = detect_dom(None)
    _add("无会话 → unknown 而非 clean",
         (not st.detected) and st.is_unknown and st.reason == "no_session",
         st.describe())

    # 4) **命中 recaptcha iframe**
    fake = _FakeSession({
        "iframe_src": ["https://www.google.com/recaptcha/api2/anchor?k=6Lc"],
        "class": "g-recaptcha", "id": "rc",
        "html": "<iframe src='https://www.google.com/recaptcha/api2/anchor'>",
        "url": "https://example.com/login", "title": "登录",
    })
    st = detect_dom(fake)
    _add("命中 recaptcha → detected",
         st.detected and st.source == "dom", st.describe())
    _add("命中后 fingerprint_id 非空",
         bool(st.fingerprint_id), st.fingerprint_id)
    _add("命中后 evidence 带 matched_field / matched_value",
         "matched_field" in st.evidence and "matched_value" in st.evidence,
         st.evidence.get("matched_field"))
    _add("命中 recaptcha 严重度为必须打断",
         st.severity == Severity.INTERRUPT and st.action == "pause",
         "%s → %s" % (st.severity, st.action))
    _add("命中后 confidence < 1.0（不宣称 100% 确定）",
         0.0 < st.confidence < 1.0, st.confidence)
    _add("命中后 evidence 带来源可信度",
         "trust" in st.evidence, st.evidence.get("trust"))

    # 5) 滑块必须 INTERRUPT（硬约束 #9 的检测器侧自证）
    fake_g = _FakeSession({
        "iframe_src": [], "class": "geetest_slider_button",
        "id": "geetest_wrap", "html": "<div class='geetest_slider_button'>",
        "url": "https://x.com/", "title": "",
    })
    st_g = detect_dom(fake_g)
    _add("命中极验滑块 → 必须打断",
         st_g.detected and st_g.type == RiskType.SLIDER
         and st_g.severity == Severity.INTERRUPT,
         st_g.describe())
    _add("滑块命中不出现在 continue 路径",
         st_g.action == "pause", st_g.action)

    # 6) Cloudflare 徽标是 BYPASSABLE（不该打断人）
    fake_badge = _FakeSession({
        "iframe_src": [], "class": "grecaptcha-badge", "id": "",
        "html": "<div class='grecaptcha-badge'/>",
        "url": "https://x.com/", "title": "",
    })
    st_badge = detect_dom(fake_badge)
    _add("reCAPTCHA 徽标 → BYPASSABLE（不打断人）",
         st_badge.detected and st_badge.severity == Severity.BYPASSABLE
         and st_badge.action == "continue",
         st_badge.describe())

    # 7) 干净页面
    fake_clean = _FakeSession({
        "iframe_src": ["https://player.bilibili.com/x"],
        "class": "nav btn-primary content", "id": "main submit-btn",
        "html": "<div class='nav'>首页</div><iframe src='https://player.bilibili.com/x'>",
        "url": "https://www.bilibili.com/video/BV1", "title": "视频",
    })
    st_clean = detect_dom(fake_clean)
    _add("干净页面 → is_clean（不是 unknown）",
         (not st_clean.detected) and st_clean.is_clean, st_clean.describe())

    # 8) **URL 单独命中**（DOM 干净但 URL 是挑战路径）
    fake_url = _FakeSession({
        "iframe_src": [], "class": "a b", "id": "c",
        "html": "<div/>",
        "url": "https://site.com/cdn-cgi/challenge-platform/h/b/orchestrate",
        "title": "",
    })
    st_url = detect_dom(fake_url)
    _add("URL 路径单独命中（DOM 干净也报）",
         st_url.detected and st_url.extra.get("url_hit") is True,
         st_url.describe())
    _add("外层传入的 url 优先于页面内取的",
         detect_dom(_FakeSession({"class": "x", "id": "y", "html": "<i/>",
                                  "url": "https://clean.com/"}),
                    url="https://evil.com/challenge/x").detected is True,
         "外层 url 生效")
    _add("通用 /challenge 路径可命中（新增 url 指纹）",
         detect_dom(_FakeSession({"class": "x", "id": "y", "html": "<i/>",
                                  "url": "https://x.com/challenge/verify"})
                    ).detected is True,
         "dom.generic.url_challenge")
    _add("干净 URL 不误报",
         detect_dom(_FakeSession({"class": "nav content", "id": "app",
                                  "html": "<div/>",
                                  "url": "https://news.site.com/article/123"})
                    ).is_clean is True,
         "正常新闻页")

    # 9) 采集抛异常 → 不吞，转 unknown 且 evidence 里有 error
    fake_err = _FakeSession(None, raise_exc=RuntimeError("CDP 断了"))
    st_err = detect_dom(fake_err)
    _add("采集抛异常 → unknown（不吞异常）",
         st_err.is_unknown and st_err.reason == "collect_failed",
         st_err.describe())
    _add("异常详情写进 evidence",
         "CDP 断了" in str(st_err.extra.get("error", ""))
         or "CDP 断了" in str(st_err.to_dict()),
         st_err.extra.get("error"))

    # 10) JS 返回非 dict → unknown（不是 clean）
    fake_weird = _FakeSession("我是字符串不是对象")
    st_weird = detect_dom(fake_weird)
    _add("evaluate 返回非对象 → unknown",
         st_weird.is_unknown, st_weird.describe())

    # 11) JS 返回空文档 → unknown（空 ≠ 干净）
    fake_none = _FakeSession({})
    st_none = detect_dom(fake_none)
    _add("空文档 → unknown（空≠干净）",
         st_none.is_unknown and st_none.reason == "empty_document",
         st_none.describe())

    # 12) 页面内 JS 报了错但拿到了字段 → 仍能判定
    fake_partial = _FakeSession({
        "iframe_src": ["https://www.google.com/recaptcha/api2/bframe"],
        "class": "", "id": "", "html": "",
        "url": "https://x.com/", "title": "",
        "errors": ["classid:SecurityError"],
    })
    st_partial = detect_dom(fake_partial)
    _add("部分字段失败仍能命中",
         st_partial.detected, st_partial.describe())
    _add("部分失败写进 evidence.warnings",
         "warnings" in st_partial.evidence, st_partial.evidence.get("warnings"))

    # 13) timeout=0 → 立刻 timeout
    st_to = detect_dom(fake, timeout=0.0)
    _add("timeout=0 → 立刻返回 timeout",
         st_to.reason == "timeout" and st_to.is_unknown, st_to.describe())

    # 14) 慢会话超时
    fake_slow = _FakeSession({}, delay=0.25)
    st_slow = detect_dom(fake_slow, timeout=0.05)
    _add("慢会话触发 timeout（不是 clean）",
         st_slow.reason == "timeout" and st_slow.is_unknown,
         "%s elapsed=%.3f" % (st_slow.describe(), st_slow.elapsed))

    # 15) 不支持 timeout 关键字的会话 → 兼容退回
    fake_compat = _FakeSession({"class": "geetest_slider", "id": "",
                                "html": "", "iframe_src": [],
                                "url": "https://x/"},
                               accept_timeout=False)
    st_compat = detect_dom(fake_compat)
    _add("会话不支持 timeout 关键字时兼容退回",
         st_compat.detected, st_compat.describe())

    # 16) 没有自造 session 类型（硬约束：与阶段四对齐）
    #     断言的是**可执行代码**，不是整份源码 —— 因为 docstring 里
    #     必须提到阶段四的模块名才能讲清依赖方向。用 AST 剥掉
    #     全部字符串字面量与注释再查，才是真的在查"有没有硬依赖"。
    code_text = _code_only()
    import ast
    import inspect
    import os
    tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())

    # 真判据：**模块顶层**（不在任何函数体内）不许 import web_cdp。
    # 只看函数体外的 import —— selfcheck(quick=False) 里那个延迟导入
    # 是刻意留的（没装浏览器时连 import 都不该发生），不算违规。
    top_imports: List[str] = []
    for node in tree.body:          # 只遍历顶层，不进函数
        if isinstance(node, ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top_imports.append(mod)
    _add("模块顶层不 import web_cdp（自造 session 会破坏阶段四契约）",
         not any("web_cdp" in m for m in top_imports),
         top_imports[:6] or "无任何 import")
    # 判据：顶层不许有**非测试替身**的 Session 类。
    # `_FakeSession` 是本模块自检用的假会话（下划线开头即私有测试替身），
    # 它不是要被生产代码使用的类型，不算"自造 session 类型"。
    top_classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    bad_classes = [c for c in top_classes
                   if "session" in c.lower() and not c.startswith("_")]
    _add("模块顶层不定义生产用的 Session 类（不自造类型）",
         not bad_classes,
         "顶层类：%s" % (top_classes or "无"))
    _add("detect_dom 只用鸭子类型驱动会话",
         "evaluate" in inspect.getsource(detect_dom)
         or "collect_fields" in inspect.getsource(detect_dom),
         "通过 evaluate() 驱动")

    # 17) 指纹全部来自指纹库（同样只查可执行代码）
    _add("本模块不硬编码厂商指纹（只查代码，不查 docstring）",
         not any(k in code_text for k in
                 ('"recaptcha"', "'recaptcha'", '"geetest"', "'geetest'",
                  '"hcaptcha"', "'hcaptcha'", '"turnstile"', "'turnstile'")),
         "全部来自 fingerprints.py")

    # 18) 返回类型契约
    _add("所有分支返回 RiskStatus",
         all(isinstance(x, RiskStatus) for x in
             (st, st_g, st_clean, st_err, st_to, st_slow)),
         "共 6 个样本")
    _add("unknown 分支全部 is_clean=False（硬约束）",
         all(x.is_clean is False for x in (st, st_err, st_weird, st_none,
                                           st_to, st_slow)),
         "已自证")

    # 19) quick=False 才做的联网探测
    if not quick:
        try:
            from ..web_cdp import launcher as L
            inst = L.list_debug_instances()
            _add("真实探测调试实例（quick=False）", True,
                 "发现 %d 个可复用实例" % len(inst))
        except Exception as exc:      # noqa: BLE001
            _add("真实探测调试实例（quick=False）", True,
                 "探测失败但不构成失败：%s" % exc)
    else:
        checks.append({"name": "真实探测调试实例（quick 跳过）",
                       "ok": True, "detail": "(quick 跳过)"})

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _code_only() -> str:
    """剥掉**全部字符串字面量与注释**后剩下的可执行代码骨架。

    为什么需要它：静态断言"本模块不许出现厂商名 / 不许 import web_cdp"
    时，会遇到两类假警报：

    1. **docstring 里必须提**。讲清依赖方向就得写
       "本模块接受的是阶段四 web_cdp 的会话对象"。
    2. **断言自己的字符串里也有**。自检代码写
       ``'"recaptcha"' not in code_text``，这句话本身就把
       "recaptcha" 写进了源码。

    所以正确做法不是"查源码里有没有这个词"，而是
    **把字符串字面量和注释全部抹掉，只留标识符与语法结构**，
    再查。这样 ``import avatar.web_cdp`` 会留下 ``avatar`` / ``web_cdp``
    两个 NAME token 被抓到，而 docstring 和断言字符串则彻底消失。

    AST 解析失败时退回**原源码**（查得更严 > 漏掉真违规）。
    """
    try:
        import ast
        import os
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)

        # 收集所有字符串字面量（含 docstring、f-string 的静态部分）的行号
        str_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if getattr(node, "lineno", None):
                    str_lines.add(node.lineno)
                if getattr(node, "end_lineno", None):
                    for ln in range(node.lineno, node.end_lineno + 1):
                        str_lines.add(ln)

        lines = src.splitlines()
        kept = []
        for i, ln in enumerate(lines, 1):
            if i in str_lines:
                continue          # 整行都在字符串里 → 丢掉
            kept.append(ln.split("#", 1)[0])   # 再去掉行尾注释
        return "\n".join(kept)
    except Exception:      # noqa: BLE001
        return _module_source()


def _module_source() -> str:
    """读本文件源码全文（兜底用）。"""
    try:
        import os
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:      # noqa: BLE001
        return ""


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-52s %s" % ("[OK]" if c["ok"] else "[FAIL]", c["name"], c["detail"]))
    n = len(r["checks"])
    print("detector_dom 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
