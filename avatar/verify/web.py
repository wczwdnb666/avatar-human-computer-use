# -*- coding: utf-8 -*-
"""Web 端验证：CDP 回读 DOM + URL 变化 + 网络响应。

Web 端比桌面端**强得多**，因为能拿到**确切的值**：

=========================  =============================================
DOM 判据                     ``Runtime.evaluate`` 读元素内容/值/可见性
URL 判据                     ``location.href`` 比较
网络判据                     ``Network`` 事件里找目标请求的响应
标题判据                     ``document.title``
=========================  =============================================

## 三条判据的强弱与选择

**网络判据最强** —— 服务端返回 200 且 body 里有成功标记，
这就是**事实**，不是推断。打榜场景必须用它：
"我提交了这一票"只有服务端确认才算数，UI 上的"提交成功"
提示可能是个前端 bug 画出来的。

**DOM 判据次强** —— 能读出确切文本/值。缺点是"DOM 说成功"
不等于"业务成功"（前端可能乐观更新）。而且 React/Vue
的 DOM 更新是异步的，要等 —— 所以 :func:`verify_dom` 内置轮询。

**URL 判据最弱但最快** —— 只能证明"导航发生了"，
不能证明"到了正确的页面"（可能 302 到登录页）。
适合做**快速预判**，不适合作为唯一判据。

## 坑

1. **``Runtime.evaluate`` 的 ``returnByValue=True`` 对不可序列化值
   返回空**。DOM 节点、函数、循环引用都拿不到值 → 会被判成"未知"。
   所以能返回字符串/数字的地方一定返回字符串/数字。

2. **跨域 iframe 读不到**。``document.querySelector`` 到 iframe 里
   会抛 SecurityError。这种情况返回"未知"，不是失败。

3. **DOM 更新是异步的**。点完按钮立刻读会读到旧值。
   :func:`verify_dom` 默认在 ``timeout`` 内轮询到值符合或超时，
   这是**必须的** —— 否则会大量误报失败。

4. **文本比较要归一化**。DOM 里的文本带大量空白和换行。
   默认 ``normalize=True``：把连续空白压成单个空格、去首尾。

5. **网络判据要用 ``Network.enable`` 之后捕获的记录**。
   动作之前就启动监视（``NetworkMonitor.start``），
   否则请求已经发完，事件捕获不到。这是最容易踩的坑。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AvatarError
from .result import VerifyResult

__all__ = [
    "WebVerifyError",
    "evaluate_value",
    "verify_dom",
    "verify_dom_text",
    "verify_dom_value",
    "verify_dom_attr",
    "verify_element_visible",
    "verify_element_exists",
    "verify_url",
    "verify_title",
    "verify_network",
    "verify_network_status",
    "verify_web",
    "selfcheck",
]


class WebVerifyError(AvatarError):
    """Web 验证过程出错。"""

    exit_code = 24


#: 默认轮询间隔。
DEFAULT_POLL = 0.1
#: 默认等待 DOM 更新时长。
DEFAULT_TIMEOUT = 5.0


def _norm(s: Any, normalize: bool = True) -> str:
    """文本归一化：压空白、去首尾。"""
    if s is None:
        return ""
    t = str(s)
    if not normalize:
        return t.strip()
    try:
        import re
        return re.sub(r"\s+", " ", t).strip()
    except Exception:
        return " ".join(t.split())


def _js_str(s: Any) -> str:
    """把 Python 值安全地嵌进 JS 字符串字面量。"""
    import json
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


#: 读元素状态并返回 JSON 的 JS 片段。
#: ``__avatarVerify`` 挂在 window 上，幂等定义（重复注入无害）。
PROBE_JS_TEMPLATE = """
(function() {
  if (!window.__avatarVerify) {
    window.__avatarVerify = function(sel) {
      var out = {found: false};
      var el = null;
      try {
        el = document.querySelector(sel);
      } catch (e) {
        out.error = 'query_failed: ' + e.message;
        return out;
      }
      if (!el) return out;
      out.found = true;
      try { out.tag = el.tagName ? el.tagName.toLowerCase() : ''; } catch (e) {}
      try { out.text = (el.innerText !== undefined && el.innerText !== null)
              ? el.innerText : (el.textContent || ''); } catch (e) {}
      try { out.value = (el.value === undefined) ? null : el.value; } catch (e) {}
      try { out.disabled = !!el.disabled; } catch (e) {}
      try { out.checked = (el.checked === undefined) ? null : !!el.checked; } catch (e) {}
      try {
        var cs = (el.ownerDocument.defaultView || window).getComputedStyle(el);
        var r = el.getBoundingClientRect();
        out.display = cs.display;
        out.visibility = cs.visibility;
        out.opacity = cs.opacity;
        out.rect = {x: r.left, y: r.top, w: r.width, h: r.height};
      } catch (e) {}
      try {
        out.attrs = {};
        if (el.attributes) {
          for (var i = 0; i < el.attributes.length; i++) {
            var a = el.attributes[i];
            out.attrs[a.name] = a.value;
          }
        }
      } catch (e) {}
      return out;
    };
  }
  return window.__avatarVerify(SEL);
})()
"""


def evaluate_value(sess: Any, expression: str, *,
                   session_id: Optional[str] = None,
                   timeout: float = 10.0,
                   await_promise: bool = False) -> Tuple[bool, Any, Optional[str]]:
    """执行 JS 并取值。返回 ``(是否拿到, 值, 错误)``。

    **不抛异常** —— 拿不到就返回 ``(False, None, 原因)``，
    由调用方转成"未知"。
    """
    if sess is None:
        return (False, None, "没有 CDP 会话")
    try:
        resp = sess.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
            },
            session_id=session_id, timeout=timeout)
    except Exception as exc:
        return (False, None, "cdp_error: %s" % (exc,))

    if not isinstance(resp, dict):
        return (False, None, "响应格式异常")
    if resp.get("exceptionDetails"):
        det = resp["exceptionDetails"]
        msg = ((det.get("exception") or {}).get("description")
               or det.get("text") or "JS 异常")
        return (False, None, "js_exception: %s" % msg)

    result = resp.get("result")
    if not isinstance(result, dict):
        return (False, None, "缺 result")
    if result.get("type") == "undefined":
        return (False, None, "值是 undefined")
    if "value" not in result:
        sub = result.get("subtype")
        return (False, None, "值不可序列化（subtype=%s）" % sub)
    return (True, result["value"], None)


def _probe(sess: Any, selector: str, *, session_id: Optional[str] = None,
           timeout: float = 10.0) -> Tuple[bool, Optional[Dict[str, Any]],
                                           Optional[str]]:
    """探测元素状态。返回 ``(是否拿到, 状态字典, 错误)``。"""
    js = PROBE_JS_TEMPLATE.replace("SEL", _js_str(selector))
    ok, val, err = evaluate_value(sess, js, session_id=session_id,
                                  timeout=timeout)
    if not ok:
        return (False, None, err)
    if not isinstance(val, dict):
        return (False, None, "探测结果不是对象")
    if val.get("error"):
        return (False, None, val["error"])
    return (True, val, None)


# --------------------------------------------------------------------------
# DOM 判据
# --------------------------------------------------------------------------

def verify_dom(
    sess: Any,
    selector: str,
    *,
    expect_text: Any = None,
    has_expect_text: bool = False,
    expect_value: Any = None,
    has_expect_value: bool = False,
    match: str = "contains",
    normalize: bool = True,
    expect_exists: Optional[bool] = None,
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
    settle: float = 0.0,
) -> VerifyResult:
    """验证 DOM 元素的存在性 / 文本 / 值。

    在 ``timeout`` 内**轮询**直到条件满足 —— DOM 更新是异步的，
    不轮询会大量误报。

    判据优先级：``expect_exists`` > ``has_expect_value`` >
    ``has_expect_text``。没给任何期望时只验证"元素存在"。
    """
    t0 = time.time()
    if settle > 0:
        time.sleep(settle)

    deadline = time.time() + max(0.0, timeout)
    last_state = None  # type: Optional[Dict[str, Any]]
    last_err = None
    ever_probed = False
    last_actual = None

    while True:
        ok, state, err = _probe(sess, selector, session_id=session_id,
                                timeout=max(1.0, min(10.0, timeout + 1.0)))
        if ok:
            ever_probed = True
            last_state = state
            found = bool(state.get("found"))

            if expect_exists is not None:
                if found == bool(expect_exists):
                    return VerifyResult.ok_(
                        method="dom", target=selector, mode="web",
                        reason="元素%s" % ("存在" if expect_exists else "已消失"),
                        evidence={"expected_exists": bool(expect_exists),
                                  "found": found},
                        elapsed=time.time() - t0)
                last_actual = found
            elif not found:
                last_actual = "不存在"
            else:
                # 值优先
                if has_expect_value:
                    actual_raw = state.get("value")
                    if actual_raw is None and state.get("checked") is not None:
                        actual_raw = "true" if state.get("checked") else "false"
                    v = _match(actual_raw, expect_value, match, normalize)
                    last_actual = actual_raw
                    if v is True:
                        return VerifyResult.ok_(
                            method="dom_value", target=selector, mode="web",
                            reason="值匹配（%s）" % match,
                            evidence={"expected": _safe(expect_value),
                                      "actual": _safe(actual_raw),
                                      "match": match},
                            elapsed=time.time() - t0)
                if has_expect_text:
                    actual_txt = state.get("text")
                    v = _match(actual_txt, expect_text, match, normalize)
                    if last_actual is None:
                        last_actual = actual_txt
                    if v is True:
                        return VerifyResult.ok_(
                            method="dom_text", target=selector, mode="web",
                            reason="文本匹配（%s）" % match,
                            evidence={"expected": _safe(expect_text),
                                      "actual": _safe(actual_txt),
                                      "match": match},
                            elapsed=time.time() - t0)
                if not has_expect_value and not has_expect_text:
                    return VerifyResult.ok_(
                        method="dom", target=selector, mode="web",
                        reason="元素存在",
                        evidence={"found": True}, elapsed=time.time() - t0)
        else:
            last_err = err

        if time.time() >= deadline:
            break
        time.sleep(max(0.01, poll))

    # 超时 —— 归纳最后的观察
    if not ever_probed:
        return VerifyResult.unknown_(
            method="dom", target=selector, mode="web",
            reason="读不到元素状态：%s" % (last_err or "未知原因"),
            elapsed=time.time() - t0,
            extra={"probe_error": last_err})

    found = bool((last_state or {}).get("found"))
    if expect_exists is not None:
        return VerifyResult.fail(
            method="dom", target=selector, mode="web",
            reason="期望元素%s，但%s（等待 %.1fs）"
                   % ("存在" if expect_exists else "消失",
                      "没找到" if expect_exists else "仍然存在",
                      timeout),
            evidence={"expected_exists": bool(expect_exists),
                      "found": found},
            elapsed=time.time() - t0)

    if not found:
        return VerifyResult.fail(
            method="dom", target=selector, mode="web",
            reason="元素不存在（等待 %.1fs）" % timeout,
            evidence={"found": False}, elapsed=time.time() - t0)

    want = expect_value if has_expect_value else expect_text
    field = "value" if has_expect_value else "text"
    return VerifyResult.fail(
        method="dom_value" if has_expect_value else "dom_text",
        target=selector, mode="web",
        reason="%s 不匹配：期望 %r，实际 %r（等待 %.1fs）"
               % (field, _safe(want), _safe(last_actual), timeout),
        evidence={"expected": _safe(want), "actual": _safe(last_actual),
                  "match": match, "field": field},
        elapsed=time.time() - t0)


def _match(actual: Any, expected: Any, match: str,
           normalize: bool = True) -> Optional[bool]:
    m = (match or "contains").lower()
    if m == "any":
        return actual is not None
    if m == "nonempty":
        return bool(_norm(actual, normalize))
    a = _norm(actual, normalize)
    e = _norm(expected, normalize)
    if m == "exact":
        return a == e
    if m == "contains":
        return e in a
    if m == "regex":
        try:
            import re
            return re.search(e, a) is not None
        except Exception:
            return None
    return None


def _safe(v: Any, limit: int = 300) -> Any:
    try:
        s = v if isinstance(v, (int, float, bool, type(None))) else str(v)
    except Exception:
        return "<unrepr>"
    if isinstance(s, str) and len(s) > limit:
        return s[:limit] + "…"
    return s


def verify_dom_text(sess: Any, selector: str, expected: Any, *,
                    match: str = "contains", **kw) -> VerifyResult:
    """验证元素文本。"""
    return verify_dom(sess, selector, expect_text=expected,
                      has_expect_text=True, match=match, **kw)


def verify_dom_value(sess: Any, selector: str, expected: Any, *,
                     match: str = "exact", **kw) -> VerifyResult:
    """验证元素值（input/textarea/select）。"""
    return verify_dom(sess, selector, expect_value=expected,
                      has_expect_value=True, match=match, **kw)


def verify_dom_attr(
    sess: Any,
    selector: str,
    attr: str,
    expected: Any = None,
    *,
    has_expected: bool = False,
    match: str = "exact",
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> VerifyResult:
    """验证元素的某个属性。

    ``has_expected=False`` 时只验证**属性存在**。
    """
    t0 = time.time()
    deadline = time.time() + max(0.0, timeout)
    last = None
    probed = False

    while True:
        ok, state, err = _probe(sess, selector, session_id=session_id,
                                timeout=max(1.0, min(10.0, timeout + 1.0)))
        if ok:
            probed = True
            attrs = state.get("attrs") or {}
            if attr in attrs:
                last = attrs[attr]
                if not has_expected:
                    return VerifyResult.ok_(
                        method="dom_attr", target=selector, mode="web",
                        reason="属性 %s 存在（值 %r）" % (attr, _safe(last)),
                        evidence={"attr": attr, "actual": _safe(last)},
                        elapsed=time.time() - t0)
                v = _match(last, expected, match)
                if v is True:
                    return VerifyResult.ok_(
                        method="dom_attr", target=selector, mode="web",
                        reason="属性 %s 匹配（%s）" % (attr, match),
                        evidence={"attr": attr, "expected": _safe(expected),
                                  "actual": _safe(last)},
                        elapsed=time.time() - t0)
            else:
                last = None
        if time.time() >= deadline:
            break
        time.sleep(DEFAULT_POLL)

    if not probed:
        return VerifyResult.unknown_(
            method="dom_attr", target=selector, mode="web",
            reason="读不到元素状态：%s" % (err or "未知"), elapsed=time.time() - t0)
    if last is None and has_expected:
        return VerifyResult.fail(
            method="dom_attr", target=selector, mode="web",
            reason="属性 %s 不存在（期望 %r）" % (attr, _safe(expected)),
            evidence={"attr": attr, "expected": _safe(expected),
                      "actual": None},
            elapsed=time.time() - t0)
    if last is None:
        return VerifyResult.fail(
            method="dom_attr", target=selector, mode="web",
            reason="属性 %s 不存在" % attr, elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="dom_attr", target=selector, mode="web",
        reason="属性 %s 不匹配：期望 %r，实际 %r"
               % (attr, _safe(expected), _safe(last)),
        evidence={"attr": attr, "expected": _safe(expected),
                  "actual": _safe(last)},
        elapsed=time.time() - t0)


def verify_element_visible(sess: Any, selector: str, *,
                           expect: bool = True,
                           session_id: Optional[str] = None,
                           timeout: float = DEFAULT_TIMEOUT) -> VerifyResult:
    """验证元素**可见 / 不可见**。

    可见判据（三个都要满足）：``display != none``、
    ``visibility != hidden``、``opacity > 0``，**且** 有非零尺寸。
    """
    t0 = time.time()
    deadline = time.time() + max(0.0, timeout)
    last_vis = None
    probed = False
    last_err = None

    while True:
        ok, state, err = _probe(sess, selector, session_id=session_id,
                                timeout=max(1.0, min(10.0, timeout + 1.0)))
        if ok:
            probed = True
            last_vis = _is_visible(state)
            if last_vis == bool(expect):
                return VerifyResult.ok_(
                    method="dom_visible", target=selector, mode="web",
                    reason="元素%s" % ("可见" if expect else "不可见"),
                    evidence={"visible": last_vis, "expected": bool(expect)},
                    elapsed=time.time() - t0)
        else:
            last_err = err
        if time.time() >= deadline:
            break
        time.sleep(DEFAULT_POLL)

    if not probed:
        return VerifyResult.unknown_(
            method="dom_visible", target=selector, mode="web",
            reason="读不到元素状态：%s" % (last_err or "未知"),
            elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="dom_visible", target=selector, mode="web",
        reason="期望元素%s，实际%s" % ("可见" if expect else "不可见",
                                      "可见" if last_vis else "不可见"),
        evidence={"visible": last_vis, "expected": bool(expect)},
        elapsed=time.time() - t0)


def _is_visible(state: Dict[str, Any]) -> bool:
    if not state.get("found"):
        return False
    if state.get("display") in ("none", None) and "display" in state:
        return False
    if state.get("visibility") == "hidden":
        return False
    try:
        if float(state.get("opacity", 1)) <= 0.0:
            return False
    except (TypeError, ValueError):
        pass
    rect = state.get("rect") or {}
    try:
        if float(rect.get("w", 0)) <= 0 or float(rect.get("h", 0)) <= 0:
            return False
    except (TypeError, ValueError):
        pass
    return True


def verify_element_exists(sess: Any, selector: str, *, expect: bool = True,
                          **kw) -> VerifyResult:
    """验证元素存在 / 消失。"""
    return verify_dom(sess, selector, expect_exists=expect, **kw)


# --------------------------------------------------------------------------
# URL / 标题判据
# --------------------------------------------------------------------------

def verify_url(
    sess: Any,
    expected: Any,
    *,
    match: str = "contains",
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> VerifyResult:
    """验证当前 URL。

    默认 ``contains`` —— 完整 URL 常带随机 query，精确比会误报。
    """
    t0 = time.time()
    deadline = time.time() + max(0.0, timeout)
    last = None
    got_any = False

    while True:
        ok, href, err = evaluate_value(
            sess, "location.href", session_id=session_id,
            timeout=max(1.0, min(10.0, timeout + 1.0)))
        if ok and href is not None:
            got_any = True
            last = href
            v = _match(href, expected, match)
            if v is True:
                return VerifyResult.ok_(
                    method="url", mode="web",
                    reason="URL 匹配（%s）" % match,
                    evidence={"expected": _safe(expected),
                              "actual": _safe(href)},
                    elapsed=time.time() - t0)
        if time.time() >= deadline:
            break
        time.sleep(max(0.01, poll))

    if not got_any:
        return VerifyResult.unknown_(
            method="url", mode="web",
            reason="读不到 location.href：%s" % (err or "未知"),
            elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="url", mode="web",
        reason="URL 不匹配：期望 %r，实际 %r"
               % (_safe(expected), _safe(last)),
        evidence={"expected": _safe(expected), "actual": _safe(last),
                  "match": match},
        elapsed=time.time() - t0)


def verify_title(
    sess: Any,
    expected: Any,
    *,
    match: str = "contains",
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> VerifyResult:
    """验证 ``document.title``。"""
    t0 = time.time()
    deadline = time.time() + max(0.0, timeout)
    last = None
    got_any = False

    while True:
        ok, title, err = evaluate_value(
            sess, "document.title", session_id=session_id,
            timeout=max(1.0, min(10.0, timeout + 1.0)))
        if ok and title is not None:
            got_any = True
            last = title
            if _match(title, expected, match) is True:
                return VerifyResult.ok_(
                    method="title", mode="web",
                    reason="标题匹配（%s）" % match,
                    evidence={"expected": _safe(expected),
                              "actual": _safe(title)},
                    elapsed=time.time() - t0)
        if time.time() >= deadline:
            break
        time.sleep(DEFAULT_POLL)

    if not got_any:
        return VerifyResult.unknown_(
            method="title", mode="web", reason="读不到标题",
            elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="title", mode="web",
        reason="标题不匹配：期望 %r，实际 %r"
               % (_safe(expected), _safe(last)),
        evidence={"expected": _safe(expected), "actual": _safe(last)},
        elapsed=time.time() - t0)


# --------------------------------------------------------------------------
# 网络判据（最强）
# --------------------------------------------------------------------------

def verify_network(
    monitor: Any,
    pattern: Any = None,
    *,
    expect_status: Any = None,
    has_expect_status: bool = False,
    url_contains: Optional[str] = None,
    body_contains: Optional[str] = None,
    require_finished: bool = True,
    timeout: float = 10.0,
    poll: float = 0.1,
) -> VerifyResult:
    """在网络记录里找目标请求，验证其状态/响应体。

    ``monitor`` 是 :class:`avatar.web_cdp.network.NetworkMonitor`
    （**必须在动作之前就 ``start()``**，否则捕获不到）。

    ``pattern`` 可以直接给一个 :class:`RequestPattern`；
    也可以只给 ``url_contains`` 由本函数构造。

    判据（全满足才算成功）：URL 命中 + 请求完成 + 状态码符合 +
    （可选）响应体包含指定文本。
    """
    t0 = time.time()
    if monitor is None:
        return VerifyResult.unknown_(
            method="network", mode="web", reason="没有网络监视器",
            elapsed=time.time() - t0)

    pat = pattern
    if pat is None and url_contains is not None:
        try:
            from ..web_cdp.network import RequestPattern
            pat = RequestPattern(url_contains)
        except Exception:
            pat = None

    if pat is None:
        return VerifyResult.unknown_(
            method="network", mode="web",
            reason="既没给 pattern 也没给 url_contains",
            elapsed=time.time() - t0)

    # 等目标请求出现（**放宽 require_finished**，这样能区分
    # "根本没这条请求" 和 "有这条请求但还没跑完" —— 两者的结论不同）
    rec = None
    pending = None
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            if hasattr(monitor, "wait_for"):
                rec = monitor.wait_for(pat, timeout=0.0,
                                       require_finished=require_finished)
        except TypeError:
            rec = None
        except Exception:
            rec = None

        if rec is None:
            # 找不到"已完成"的 —— 看看是不是"有但还没完成"
            try:
                cand = monitor.first(pat)
            except Exception:
                cand = None
            if cand is not None:
                pending = cand
                if not require_finished:
                    rec = cand
                    break
            else:
                pending = None

        if rec is not None:
            break
        if time.time() >= deadline:
            break
        time.sleep(max(0.01, poll))

    if rec is None and pending is not None:
        # 请求存在但还没结束 —— 这是"未知"，不是"没找到"，更不是"失败"
        return VerifyResult.unknown_(
            method="network", mode="web",
            reason="请求存在但 %s 内未完成（等待 %.1fs）"
                   % ("要求已完成，" if require_finished else "", timeout),
            evidence={"url": _safe(getattr(pending, "url", "")),
                      "method": getattr(pending, "method", ""),
                      "finished": False},
            elapsed=time.time() - t0,
            extra={"pending": True})

    if rec is None:
        try:
            n = len(getattr(monitor, "records", []) or [])
        except Exception:
            n = -1
        return VerifyResult.fail(
            method="network", mode="web",
            reason="在 %d 条记录里没找到目标请求（等待 %.1fs）"
                   % (n, timeout),
            evidence={"records": n, "timeout": timeout},
            elapsed=time.time() - t0)

    ev = {
        "url": _safe(getattr(rec, "url", "")),
        "method": getattr(rec, "method", ""),
        "status": getattr(rec, "status", None),
        "finished": bool(getattr(rec, "finished", False)),
        "failed": getattr(rec, "error_text", None),
    }

    if not bool(getattr(rec, "finished", False)) and require_finished:
        return VerifyResult.unknown_(
            method="network", mode="web",
            reason="请求还没结束（等待 %.1fs）" % timeout,
            evidence=ev, elapsed=time.time() - t0)

    # 失败请求
    err_text = getattr(rec, "error_text", None)
    if err_text:
        return VerifyResult.fail(
            method="network", mode="web",
            reason="请求失败：%s" % err_text, evidence=ev,
            elapsed=time.time() - t0)

    status = getattr(rec, "status", None)

    if has_expect_status:
        if status is None:
            return VerifyResult.unknown_(
                method="network", mode="web", reason="没拿到状态码",
                evidence=ev, elapsed=time.time() - t0)
        if not _status_match(status, expect_status):
            return VerifyResult.fail(
                method="network", mode="web",
                reason="状态码不符：期望 %r，实际 %s"
                       % (_safe(expect_status), status),
                evidence=dict(ev, expected_status=_safe(expect_status)),
                elapsed=time.time() - t0)

    if body_contains:
        body = None
        try:
            body = rec.body_text() if hasattr(rec, "body_text") else None
        except Exception:
            body = None
        if body is None:
            try:
                body = getattr(rec, "body", None)
            except Exception:
                body = None
        if body is None:
            return VerifyResult.unknown_(
                method="network", mode="web",
                reason="响应体拿不到（可能 304 缓存 / Service Worker 拦截）",
                evidence=ev, elapsed=time.time() - t0)
        if body_contains not in str(body):
            return VerifyResult.fail(
                method="network", mode="web",
                reason="响应体不含 %r" % _safe(body_contains),
                evidence=dict(ev, body_head=_safe(str(body)[:200])),
                elapsed=time.time() - t0)
        ev["body_head"] = _safe(str(body)[:200])

    return VerifyResult.ok_(
        method="network", mode="web",
        reason="请求成功（%s %s%s）"
               % (ev["method"] or "GET", status if status else "?",
                  "，响应体含目标文本" if body_contains else ""),
        evidence=ev, elapsed=time.time() - t0,
        extra={"confidence": 1.0})


def _status_match(status: Any, expect: Any) -> bool:
    try:
        st = int(status)
    except (TypeError, ValueError):
        return False
    if isinstance(expect, (int, float)):
        return st == int(expect)
    if isinstance(expect, str):
        e = expect.strip()
        if e.endswith("xx") or e.endswith("XX"):
            try:
                return st // 100 == int(e[0])
            except (ValueError, IndexError):
                return False
        try:
            return st == int(e)
        except ValueError:
            return False
    if isinstance(expect, (set, frozenset, list, tuple)):
        return any(_status_match(st, x) for x in expect)
    return False


def verify_network_status(
    monitor: Any,
    url_contains: str,
    expected: Any = "2xx",
    *,
    timeout: float = 10.0,
) -> VerifyResult:
    """只看状态码的便捷封装。``expected`` 支持 ``"2xx"`` 这类写法。"""
    return verify_network(monitor, url_contains=url_contains,
                          expect_status=expected, has_expect_status=True,
                          timeout=timeout)


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def verify_web(
    sess: Any = None,
    *,
    selector: Optional[str] = None,
    expect_text: Any = None,
    has_expect_text: bool = False,
    expect_value: Any = None,
    has_expect_value: bool = False,
    expect_visible: Optional[bool] = None,
    expect_exists: Optional[bool] = None,
    expect_url: Any = None,
    has_expect_url: bool = False,
    expect_title: Any = None,
    has_expect_title: bool = False,
    monitor: Any = None,
    network_url_contains: Optional[str] = None,
    expect_status: Any = None,
    has_expect_status: bool = False,
    body_contains: Optional[str] = None,
    match: str = "contains",
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    prefer: str = "auto",
) -> VerifyResult:
    """Web 端统一入口。按 ``prefer`` 决定判据顺序。

    ``prefer``：

    - ``"auto"``（默认）**网络 → DOM → URL/标题**。
      网络最强，有监视器就用它。
    - ``"network"`` / ``"dom"`` / ``"url"`` 强制某一类

    与桌面端不同：Web 端**多个判据会串行尝试**，
    第一个给出**确定结论**的就返回。因为网络判据需要监视器
    和特定的 URL，不一定适用；DOM 选择器也可能不存在。
    """
    t0 = time.time()

    # 1. 网络（最强）
    if monitor is not None and (network_url_contains is not None) \
            and prefer in ("auto", "network"):
        r = verify_network(monitor, url_contains=network_url_contains,
                           expect_status=expect_status,
                           has_expect_status=has_expect_status,
                           body_contains=body_contains,
                           timeout=timeout)
        if r.conclusive or prefer == "network":
            r.elapsed = time.time() - t0
            return r

    # 2. DOM
    if sess is not None and selector and prefer in ("auto", "dom"):
        if expect_visible is not None:
            r = verify_element_visible(sess, selector, expect=expect_visible,
                                       session_id=session_id, timeout=timeout)
        elif expect_exists is not None:
            r = verify_element_exists(sess, selector, expect=expect_exists,
                                      session_id=session_id, timeout=timeout)
        else:
            r = verify_dom(sess, selector, expect_text=expect_text,
                           has_expect_text=has_expect_text,
                           expect_value=expect_value,
                           has_expect_value=has_expect_value,
                           match=match, session_id=session_id,
                           timeout=timeout)
        if r.conclusive or prefer == "dom":
            r.elapsed = time.time() - t0
            return r

    # 3. URL
    if sess is not None and has_expect_url and prefer in ("auto", "url"):
        r = verify_url(sess, expect_url, match=match, session_id=session_id,
                       timeout=timeout)
        if r.conclusive or prefer == "url":
            r.elapsed = time.time() - t0
            return r

    # 4. 标题
    if sess is not None and has_expect_title and prefer == "auto":
        r = verify_title(sess, expect_title, match=match,
                         session_id=session_id, timeout=timeout)
        r.elapsed = time.time() - t0
        return r

    return VerifyResult.unknown_(
        method="web", mode="web",
        reason="没给可用的判据（至少要 selector / expect_url / monitor）",
        elapsed=time.time() - t0,
        extra={"no_criteria": True})


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

class _FakeSession(object):
    """假 CDP 会话：按 JS 表达式返回预置结果。"""

    def __init__(self, mapping=None, error=None):
        self.mapping = mapping or {}
        self.error = error
        self.calls = []

    def call(self, method, params=None, *, session_id=None, timeout=None,
             **kw):
        self.calls.append((method, dict(params or {})))
        if self.error:
            raise RuntimeError(self.error)
        expr = (params or {}).get("expression", "")
        for key, val in self.mapping.items():
            if key in expr:
                return val
        return {"result": {"type": "undefined"}}


def _el(found=True, text="", value=None, display="block",
        visibility="visible", opacity="1", w=100.0, h=20.0,
        attrs=None) -> Dict[str, Any]:
    return {"result": {"type": "object", "value": {
        "found": found, "tag": "div", "text": text, "value": value,
        "display": display, "visibility": visibility, "opacity": opacity,
        "rect": {"x": 0, "y": 0, "w": w, "h": h},
        "attrs": attrs if attrs is not None else {},
    }}}


class _FakeRecord(object):
    """假的 RequestRecord。

    **必须实现 ``match``** —— 真 ``RequestPattern.match(rec)`` 会回调
    ``rec.match(url)``（记录自己决定 URL 怎么比）。测试替身漏了这个，
    会让所有走 RequestPattern 的用例静默失败在 AttributeError 上。
    """

    def __init__(self, url="https://x/api", method="POST", status=200,
                 finished=True, error_text=None, body=None,
                 headers=None, resource_type="XHR"):
        self.url = url
        self.method = method
        self.status = status
        self.finished = finished
        self.error_text = error_text
        self._body = body
        self.headers = headers or {}
        self.resource_type = resource_type
        self.request_id = "r1"

    def body_text(self):
        return self._body

    def match(self, pattern):
        """与真 RequestRecord.match 语义一致：正则搜索 URL。"""
        import re
        if pattern is None:
            return True
        if hasattr(pattern, "search"):
            return pattern.search(self.url or "") is not None
        try:
            return re.search(str(pattern), self.url or "") is not None
        except Exception:
            return str(pattern) in (self.url or "")


class _FakeMonitor(object):
    """假的 NetworkMonitor。

    ``first`` / ``find`` 都按真监视器的契约走：优先 ``pattern.match(rec)``，
    失败再退回"把 pattern 当字符串看"。真监视器用的是
    ``pattern.match(rec)`` 一律，这里保持一致以免测出假绿。
    """

    def __init__(self, recs=None):
        self.records = list(recs or [])

    def _hit(self, pattern, rec):
        try:
            return bool(pattern.match(rec))
        except AttributeError:
            # pattern 没有 match —— 当成字符串处理
            try:
                return str(pattern) in (getattr(rec, "url", "") or "")
            except Exception:
                return False
        except Exception:
            return False

    def first(self, pattern):
        for r in self.records:
            if self._hit(pattern, r):
                return r
        return None

    def find(self, pattern):
        return [r for r in self.records if self._hit(pattern, r)]

    def wait_for(self, pattern, timeout=0.0, require_finished=False):
        for r in self.records:
            if not self._hit(pattern, r):
                continue
            if require_finished and not bool(getattr(r, "finished", False)):
                continue
            return r
        return None


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 归一化 ---
    add("_norm 压空白", _norm("  a \n\n b  ") == "a b", _norm("  a \n\n b  "))
    add("_norm 空 → 空", _norm(None) == "")
    add("_norm 保留模式不退空白",
        _norm("  a b  ", normalize=False) == "a b")

    # --- JS 字符串安全 ---
    add("_js_str 带引号转义",
        _js_str('a"b') == '"a\\"b"', _js_str('a"b'))
    add("_js_str 中文保留",
        "中文" in _js_str("中文"), _js_str("中文"))
    add("_js_str None → 空串", _js_str(None) == '""')

    # --- evaluate_value ---
    s = _FakeSession({"1+1": {"result": {"type": "number", "value": 2}}})
    ok, v, err = evaluate_value(s, "1+1")
    add("evaluate_value 取值", ok and v == 2, (ok, v, err))

    s = _FakeSession({"x": {"result": {"type": "undefined"}}})
    ok, v, err = evaluate_value(s, "x")
    add("undefined → 没拿到", ok is False and "undefined" in err, err)

    s = _FakeSession({"x": {"result": {"type": "object", "subtype": "node"}}})
    ok, v, err = evaluate_value(s, "x")
    add("不可序列化 → 没拿到", ok is False and "不可序列化" in err, err)

    s = _FakeSession({"x": {"exceptionDetails": {
        "text": "boom", "exception": {"description": "TypeError: boom"}}}})
    ok, v, err = evaluate_value(s, "x")
    add("JS 异常 → 没拿到", ok is False and "TypeError" in err, err)

    s = _FakeSession(error="conn reset")
    ok, v, err = evaluate_value(s, "x")
    add("CDP 异常 → 没拿到", ok is False and "cdp_error" in err, err)

    ok, v, err = evaluate_value(None, "x")
    add("无会话 → 没拿到", ok is False and "没有 CDP 会话" in err, err)

    s = _FakeSession({"x": {"result": {"type": "object"}}})
    ok, v, err = evaluate_value(s, "x")
    add("缺 value 字段 → 没拿到", ok is False, err)

    # --- verify_dom 存在性 ---
    s = _FakeSession({"__avatarVerify": _el(True)})
    r = verify_dom(s, "#a", timeout=0.0)
    add("verify_dom 存在即 ok", r.ok is True, r.describe())
    add("verify_dom method=dom", r.method == "dom", r.method)
    add("verify_dom mode=web", r.mode == "web", r.mode)

    s = _FakeSession({"__avatarVerify": _el(False)})
    r = verify_dom(s, "#a", expect_exists=True, timeout=0.0)
    add("期望存在但没找到 → fail", r.failed is True, r.describe())
    r = verify_dom(s, "#a", expect_exists=False, timeout=0.0)
    add("期望消失且真的没了 → ok", r.ok is True, r.describe())

    # --- 文本 ---
    s = _FakeSession({"__avatarVerify": _el(True, text="  提交  成功  ")})
    r = verify_dom_text(s, "#a", "提交 成功", timeout=0.0)
    add("文本归一化后 contains 命中", r.ok is True, r.describe())
    add("文本 method=dom_text", r.method == "dom_text", r.method)

    r = verify_dom_text(s, "#a", "提交失败", timeout=0.0)
    add("文本不含 → fail", r.failed is True, r.describe())
    add("文本 fail evidence 带 actual",
        "actual" in r.evidence, sorted(r.evidence))

    r = verify_dom_text(s, "#a", "提交", match="exact", timeout=0.0)
    add("文本 exact 不匹配 → fail", r.failed is True, r.describe())

    r = verify_dom_text(s, "#a", r"提交\s*成功", match="regex", timeout=0.0)
    add("文本 regex 命中", r.ok is True, r.describe())

    # --- 值 ---
    s = _FakeSession({"__avatarVerify": _el(True, value="hello")})
    r = verify_dom_value(s, "#i", "hello", timeout=0.0)
    add("值精确匹配 → ok", r.ok is True, r.describe())
    add("值 method=dom_value", r.method == "dom_value", r.method)

    r = verify_dom_value(s, "#i", "world", timeout=0.0)
    add("值不符 → fail", r.failed is True, r.describe())

    # checkbox 的 checked 转成 true/false
    s = _FakeSession({"__avatarVerify": _el(True, value=None)})
    r = verify_dom_value(s, "#c", "true", timeout=0.0)
    add("value=None 时不误判为 ok", r.failed or r.unknown, r.describe())

    # --- 元素不存在时的 fail ---
    s = _FakeSession({"__avatarVerify": _el(False)})
    r = verify_dom_text(s, "#a", "x", timeout=0.0)
    add("元素不存在 → fail 而非 unknown", r.failed is True, r.describe())
    add("fail reason 说明不存在", "不存在" in r.reason, r.reason)

    # --- 探测失败 → unknown ---
    s = _FakeSession(error="boom")
    r = verify_dom(s, "#a", timeout=0.0)
    add("探测失败 → unknown", r.unknown is True, r.describe())
    add("unknown 带 probe_error",
        "probe_error" in r.extra, r.extra)

    # --- 可见性 ---
    s = _FakeSession({"__avatarVerify": _el(True)})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("可见 → ok", r.ok is True, r.describe())

    s = _FakeSession({"__avatarVerify": _el(True, display="none")})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("display:none → 不可见", r.failed is True, r.describe())
    r = verify_element_visible(s, "#a", expect=False, timeout=0.0)
    add("期望不可见 → ok", r.ok is True, r.describe())

    s = _FakeSession({"__avatarVerify": _el(True, visibility="hidden")})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("visibility:hidden → 不可见", r.failed is True, r.describe())

    s = _FakeSession({"__avatarVerify": _el(True, opacity="0")})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("opacity:0 → 不可见", r.failed is True, r.describe())

    s = _FakeSession({"__avatarVerify": _el(True, w=0, h=0)})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("零尺寸 → 不可见", r.failed is True, r.describe())

    s = _FakeSession({"__avatarVerify": _el(False)})
    r = verify_element_visible(s, "#a", timeout=0.0)
    add("不存在 → 不可见", r.failed is True, r.describe())

    # --- 属性 ---
    s = _FakeSession({"__avatarVerify": _el(True, attrs={"data-x": "1"})})
    r = verify_dom_attr(s, "#a", "data-x", "1", has_expected=True, timeout=0.0)
    add("属性匹配 → ok", r.ok is True, r.describe())

    r = verify_dom_attr(s, "#a", "data-x", timeout=0.0)
    add("属性存在即 ok", r.ok is True, r.describe())

    r = verify_dom_attr(s, "#a", "data-y", timeout=0.0)
    add("属性不存在且无期望 → fail", r.failed is True, r.describe())

    r = verify_dom_attr(s, "#a", "data-y", "2", has_expected=True, timeout=0.0)
    add("属性不存在但期望值 → fail", r.failed is True, r.describe())

    r = verify_dom_attr(s, "#a", "data-x", "9", has_expected=True, timeout=0.0)
    add("属性值不符 → fail", r.failed is True, r.describe())

    # --- URL ---
    s = _FakeSession({"location.href": {"result": {
        "type": "string", "value": "https://x.com/ok?t=1"}}})
    r = verify_url(s, "x.com/ok", timeout=0.0)
    add("URL contains 命中", r.ok is True, r.describe())
    add("URL method=url", r.method == "url", r.method)

    r = verify_url(s, "https://x.com/ok?t=1", match="exact", timeout=0.0)
    add("URL exact 命中", r.ok is True, r.describe())

    r = verify_url(s, "y.com", timeout=0.0)
    add("URL 不符 → fail", r.failed is True, r.describe())

    s = _FakeSession(error="boom")
    r = verify_url(s, "x", timeout=0.0)
    add("URL 读不到 → unknown", r.unknown is True, r.describe())

    # --- 标题 ---
    s = _FakeSession({"document.title": {"result": {
        "type": "string", "value": "我的主页"}}})
    r = verify_title(s, "主页", timeout=0.0)
    add("标题 contains 命中", r.ok is True, r.describe())

    r = verify_title(s, "别的", timeout=0.0)
    add("标题不符 → fail", r.failed is True, r.describe())

    # --- 状态码匹配 ---
    add("状态 200 == 200", _status_match(200, 200) is True)
    add("状态 200 匹配 '200'", _status_match(200, "200") is True)
    add("状态 201 匹配 '2xx'", _status_match(201, "2xx") is True)
    add("状态 404 不匹配 '2xx'", _status_match(404, "2xx") is False)
    add("状态 200 匹配 {200,201}",
        _status_match(200, {200, 201}) is True)
    add("状态 500 不匹配 {200,201}",
        _status_match(500, {200, 201}) is False)
    add("状态非法返回 False", _status_match("x", 200) is False)
    add("状态 'abc' 期望返回 False", _status_match(200, "abc") is False)

    # --- 网络判据 ---
    rec = _FakeRecord(status=200, finished=True, body='{"ok":true}')
    mon = _FakeMonitor([rec])
    r = verify_network(mon, url_contains="x/api", timeout=0.0)
    add("网络：找到且完成 → ok", r.ok is True, r.describe())
    add("网络 method=network", r.method == "network", r.method)
    add("网络 ok 置信度 1.0", r.confidence == 1.0, r.confidence)

    r = verify_network(mon, url_contains="x/api", expect_status="2xx",
                       has_expect_status=True, timeout=0.0)
    add("网络：状态 2xx 通过", r.ok is True, r.describe())

    r = verify_network(mon, url_contains="x/api", expect_status="5xx",
                       has_expect_status=True, timeout=0.0)
    add("网络：状态不符 → fail", r.failed is True, r.describe())

    rec2 = _FakeRecord(status=200, finished=True, body='{"ok":true}')
    mon2 = _FakeMonitor([rec2])
    r = verify_network(mon2, url_contains="x/api",
                       body_contains='"ok":true', timeout=0.0)
    add("网络：响应体含目标 → ok", r.ok is True, r.describe())

    r = verify_network(mon2, url_contains="x/api",
                       body_contains='"err":1', timeout=0.0)
    add("网络：响应体不含 → fail", r.failed is True, r.describe())

    rec3 = _FakeRecord(status=500, finished=True, error_text="net::ERR")
    mon3 = _FakeMonitor([rec3])
    r = verify_network(mon3, url_contains="x/api", timeout=0.0)
    add("网络：请求失败 → fail", r.failed is True, r.describe())

    rec4 = _FakeRecord(status=None, finished=False)
    mon4 = _FakeMonitor([rec4])
    r = verify_network(mon4, url_contains="x/api", timeout=0.2)
    add("网络：未完成 → unknown", r.unknown is True, r.describe())

    mon5 = _FakeMonitor([])
    r = verify_network(mon5, url_contains="x/api", timeout=0.2)
    add("网络：找不到请求 → fail", r.failed is True, r.describe())
    add("网络：找不到带记录数",
        "records" in r.evidence, sorted(r.evidence))

    r = verify_network(None, url_contains="x", timeout=0.2)
    add("网络：无监视器 → unknown", r.unknown is True, r.describe())

    r = verify_network(mon, timeout=0.2)
    add("网络：无 pattern → unknown", r.unknown is True, r.describe())

    # 响应体拿不到
    rec6 = _FakeRecord(status=200, finished=True, body=None)
    mon6 = _FakeMonitor([rec6])
    r = verify_network(mon6, url_contains="x/api", body_contains="ok",
                       timeout=0.0)
    add("网络：拿不到响应体 → unknown", r.unknown is True, r.describe())

    # 用真 RequestPattern
    try:
        from ..web_cdp.network import RequestPattern
        p = RequestPattern("x/api")
        r = verify_network(mon, p, timeout=0.0, require_finished=False)
        add("网络：接受真 RequestPattern", r.ok is True, r.describe())
    except Exception as exc:
        add("网络：接受真 RequestPattern", False, str(exc))

    r = verify_network_status(mon, "x/api", "2xx", timeout=0.0)
    add("verify_network_status 便捷封装", r.ok is True, r.describe())

    # --- verify_web 路由 ---
    s = _FakeSession({"__avatarVerify": _el(True, text="成功")})
    r = verify_web(s, selector="#a", expect_text="成功",
                   has_expect_text=True, timeout=0.0)
    add("verify_web DOM 路径", r.ok is True, r.describe())

    r = verify_web(s, selector="#a", expect_visible=True, timeout=0.0)
    add("verify_web 可见性路径", r.ok is True, r.describe())

    r = verify_web(s, selector="#a", expect_exists=False, timeout=0.0)
    add("verify_web 存在性路径", r.failed is True, r.describe())

    s2 = _FakeSession({"location.href": {"result": {
        "type": "string", "value": "https://x/ok"}}})
    r = verify_web(s2, expect_url="x/ok", has_expect_url=True, timeout=0.0)
    add("verify_web URL 路径", r.ok is True, r.describe())

    r = verify_web(s2, expect_title="t", has_expect_title=True, timeout=0.0)
    add("verify_web 标题路径（读不到 → unknown）",
        r.unknown is True, r.describe())

    r = verify_web(s, prefer="network", monitor=mon,
                   network_url_contains="x/api", timeout=0.0)
    add("verify_web prefer=network", r.ok is True, r.describe())

    r = verify_web(s)
    add("verify_web 无判据 → unknown", r.unknown is True, r.describe())
    add("verify_web 无判据标记",
        r.extra.get("no_criteria") is True, r.extra)

    # 网络优先于 DOM
    s3 = _FakeSession({"__avatarVerify": _el(True, text="x")})
    r = verify_web(s3, selector="#a", monitor=mon,
                   network_url_contains="x/api", timeout=0.0)
    add("verify_web auto 下网络优先", r.method == "network", r.method)

    # --- 轮询真的等 ---
    class _DelayedSession(object):
        def __init__(self):
            self.n = 0

        def call(self, method, params=None, **kw):
            self.n += 1
            if self.n < 3:
                return {"result": {"type": "object", "value": {
                    "found": True, "text": "旧值"}}}
            return {"result": {"type": "object", "value": {
                "found": True, "text": "新值"}}}

    ds = _DelayedSession()
    t0 = time.time()
    r = verify_dom_text(ds, "#a", "新值", timeout=1.0, poll=0.05)
    add("DOM 轮询能等到更新", r.ok is True, r.describe())
    add("DOM 轮询确实重试了多次", ds.n >= 3, ds.n)

    # --- _match ---
    add("_match contains", _match("abc", "b", "contains") is True)
    add("_match exact 归一化",
        _match(" a  b ", "a b", "exact") is True)
    add("_match any 非 None",
        _match("x", None, "any") is True)
    add("_match any None → False", _match(None, None, "any") is False)
    add("_match nonempty 空白 → False",
        _match("   ", None, "nonempty") is False)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "verify.web"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
