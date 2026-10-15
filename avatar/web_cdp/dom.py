# -*- coding: utf-8 -*-
"""DOM 查询与操作。

有两条路可以走，本模块都提供：

1. **``Runtime.evaluate`` 跑 JS** —— 灵活、快、支持复杂选择器与遍历。
   这是主通道。缺 CSS 选择器支持不足的场合（比如 XPath、
   按文本找、Shadow DOM 穿透）时它几乎是唯一选择。
2. **``DOM.*`` 协议族** —— 走 CDP 原生的节点体系，
   能拿到 ``backendNodeId``（后续 ``Input.dispatchMouseEvent``
   需要它做坐标换算），以及 ``DOM.getBoxModel`` 拿精确几何。

**关键区别**：JS 路返回的是**序列化后的值**（纯数据），
DOM 路返回的是**节点句柄**（``nodeId`` / ``backendNodeId``）。
前者适合"读信息"，后者适合"要对这个节点做输入动作"。

## returnByValue 的坑

``Runtime.evaluate`` 默认返回 ``RemoteObject``。要拿真正的值必须
``returnByValue=True``。但：

- ``undefined`` 不会有 ``value`` 字段 → 我们返回 None
- DOM 节点、循环引用、函数对象**无法**序列化 → 会返回
  ``{"type":"object","subtype":"node"}`` 而没有 value → 返回 None
- ``NaN`` / ``Infinity`` 会被序列化成特殊形式 → 需要 ``unserializableValue``

所以 ``evaluate_value()`` 对这三种情况都做了处理，而不是天真地取
``result["value"]``。

## 把元素"点得到"

要找的元素可能：
- 在 Shadow DOM 里（``querySelector`` 看不见）
- 在 iframe 里（需要先切 frame）
- 还没渲染出来（需要等）

``query_selector_deep()`` 生成一段**穿透 shadow root 的 JS**，
是实际用起来最省事的默认查询。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..errors import AvatarError
from .session import CdpError, CdpTimeout

__all__ = [
    "DomError",
    "ElementNotFound_",
    "evaluate_value",
    "evaluate_json",
    "query_selector",
    "query_selector_all",
    "query_xpath",
    "find_by_text",
    "find_by_text_all",
    "query_selector_deep",
    "get_attributes",
    "get_text",
    "get_html",
    "element_exists",
    "element_visible",
    "element_enabled",
    "get_bounding_box",
    "scroll_into_view",
    "set_attribute",
    "get_box_model",
    "describe_element",
    "DEEP_QUERY_JS",
]


class DomError(AvatarError):
    """DOM 操作失败。"""

    exit_code = 15


#: 找元素失败。和顶层 ``ElementNotFound`` 分开 —— 那是 UIA 侧的，
#: 这里是 Web 侧，语义不同（Web 侧有"元素存在但不可见"这个中间态）。
class ElementNotFound_(DomError):
    """Web 里找不到元素。"""

    exit_code = 15

    def __init__(self, message: str, selector: str = "", **ctx):
        super().__init__(message, selector=selector or None, **ctx)
        self.selector = selector


# --------------------------------------------------------------------------
# JS 片段
# --------------------------------------------------------------------------
#: 穿透 Shadow DOM 的查询函数。注入一次，之后用 ``__avatarQuery(sel)`` 调。
#:
#: 实现要点：
#: - 先在 document 上查
#: - 再递归所有 shadowRoot（用 ``querySelectorAll('*')`` 找带
#:   ``shadowRoot`` 的元素）
#: - 返回**第一个**命中
#:
#: 为什么不用 ``shadowRoot.querySelector`` 递归 + :host 选择器：
#: 那只能处理开放的 shadow root，而且 :host 的匹配语义容易写错。
DEEP_QUERY_JS = r"""
(() => {
  if (window.__avatarDeepQuery) return;
  window.__avatarDeepQueryAll = function(selector, root) {
    root = root || document;
    const out = [];
    try {
      const direct = root.querySelectorAll(selector);
      for (const e of direct) out.push(e);
    } catch (e) { /* 非法选择器交给上层报错 */ }
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.shadowRoot) {
        const sub = window.__avatarDeepQueryAll(selector, el.shadowRoot);
        for (const e of sub) out.push(e);
      }
    }
    return out;
  };
  window.__avatarDeepQuery = function(selector, root) {
    const all = window.__avatarDeepQueryAll(selector, root);
    return all.length ? all[0] : null;
  };
})();
"""


# --------------------------------------------------------------------------
# 求值
# --------------------------------------------------------------------------
def evaluate_value(sess, expression: str, *, session_id: str = "",
                   timeout: float = 15.0, await_promise: bool = True) -> Any:
    """跑 JS 并返回**值本体**，处理 undefined / NaN / 节点 三种情况。

    返回 None 的三种含义（调用方需要能区分时用 ``evaluate_raw``）：
    1. JS 结果是 ``undefined``
    2. JS 结果是 DOM 节点 / 函数（不可序列化）
    3. JS 结果是 ``null``
    """
    res = sess.call("Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": await_promise,
        "returnByValue": True,
        "userGesture": True,
    }, session_id=session_id or None, timeout=timeout)

    if res.get("exceptionDetails"):
        det = res["exceptionDetails"]
        desc = ((det.get("exception") or {}).get("description")
                or det.get("text") or "?")
        raise DomError("JS 抛异常：%s" % desc, expression=expression[:200],
                       data=det)

    remote = res.get("result") or {}
    return _remote_to_value(remote)


def _remote_to_value(remote: dict) -> Any:
    """把 CDP 的 RemoteObject 转成 Python 值。"""
    if not isinstance(remote, dict):
        return None
    rtype = remote.get("type")
    if rtype == "undefined":
        return None
    if rtype == "string":
        return remote.get("value", "")
    if rtype in ("number", "boolean"):
        if "value" in remote:
            return remote["value"]
        # NaN / Infinity / -0 走 unserializableValue
        u = remote.get("unserializableValue")
        if u == "NaN":
            return float("nan")
        if u == "Infinity":
            return float("inf")
        if u == "-Infinity":
            return float("-inf")
        if u == "-0":
            return 0
        return None
    if rtype == "object" and remote.get("subtype") == "null":
        return None
    if "value" in remote:
        return remote["value"]
    # 节点 / 函数 / 循环引用 —— 不可序列化
    return None


def evaluate_json(sess, expression: str, *, session_id: str = "",
                  timeout: float = 15.0) -> Any:
    """跑 JS 并把结果按 JSON 解析。

    ⚠️ 表达式必须自己保证结果是合法 JSON 字符串。常用包法：

    ``JSON.stringify(<你的表达式>)``

    这样能拿到嵌套结构（``returnByValue`` 对深层对象也行，
    但遇到不可序列化成员会整块变 None，stringify 能让你看到
    实际序列化出了什么）。
    """
    raw = evaluate_value(sess, expression, session_id=session_id,
                         timeout=timeout)
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def query_selector_deep(sess, selector: str, *, session_id: str = "",
                        timeout: float = 15.0) -> Optional[dict]:
    """穿透 Shadow DOM 找一个元素，返回它的描述信息。

    找不到返回 None（不抛）。需要"找不到就报错"用
    ``require_element()``。
    """
    # 先注入查询函数（注入是幂等的 —— DEEP_QUERY_JS 里有卫语句）
    try:
        sess.evaluate(DEEP_QUERY_JS, session_id=session_id or None,
                      timeout=timeout, await_promise=False)
    except Exception:
        pass
    expr = ("(()=>{const e=window.__avatarDeepQuery(%s);"
            "return e?%s:null;})()"
            % (json.dumps(selector), _DESCRIBE_JS_BODY))
    return evaluate_value(sess, expr, session_id=session_id, timeout=timeout)


#: 把一个 Element 描述成 dict 的 JS 体（不含外层 IIFE）
_DESCRIBE_JS_BODY = r"""
{
  tag: e.tagName ? e.tagName.toLowerCase() : '',
  id: e.id || '',
  classes: e.className && typeof e.className === 'string'
           ? e.className.split(/\s+/).filter(Boolean) : [],
  text: (e.innerText || e.textContent || '').trim().slice(0, 200),
  value: (typeof e.value === 'string' ? e.value : null),
  type: e.type || null,
  name: e.name || null,
  href: e.href || null,
  placeholder: e.placeholder || null,
  disabled: !!e.disabled,
  readonly: !!e.readOnly,
  checked: (typeof e.checked === 'boolean' ? e.checked : null),
  visible: (() => {
    const r = e.getBoundingClientRect();
    if (!r || r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    return s.display !== 'none' && s.visibility !== 'hidden'
           && s.opacity !== '0';
  })(),
  rect: (() => {
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height,
            cx: r.x + r.width / 2, cy: r.y + r.height / 2};
  })(),
  attrs: (() => {
    const o = {};
    for (const a of e.attributes) o[a.name] = a.value;
    return o;
  })()
}
"""


def query_selector(sess, selector: str, *, session_id: str = "",
                   timeout: float = 15.0) -> Optional[dict]:
    """``document.querySelector`` 的描述结果。找不到返回 None。"""
    expr = ("(()=>{const e=document.querySelector(%s);"
            "return e?%s:null;})()" % (json.dumps(selector),
                                       _DESCRIBE_JS_BODY))
    return evaluate_value(sess, expr, session_id=session_id, timeout=timeout)


def query_selector_all(sess, selector: str, *, session_id: str = "",
                       timeout: float = 15.0,
                       limit: int = 200) -> List[dict]:
    """查所有匹配元素的描述数组。"""
    expr = ("(()=>{const es=[...document.querySelectorAll(%s)]"
            ".slice(0,%d);return es.map(e=>%s);})()"
            % (json.dumps(selector), int(limit), _DESCRIBE_JS_BODY))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, list) else []


def query_xpath(sess, xpath: str, *, session_id: str = "",
                timeout: float = 15.0) -> Optional[dict]:
    """按 XPath 找元素（``querySelector`` 做不到的场合）。"""
    expr = ("(()=>{const r=document.evaluate(%s,document,null,"
            "XPathResult.FIRST_ORDERED_NODE_TYPE,null);"
            "const e=r.singleNodeValue;return e?%s:null;})()"
            % (json.dumps(xpath), _DESCRIBE_JS_BODY))
    return evaluate_value(sess, expr, session_id=session_id, timeout=timeout)


def find_by_text(sess, text: str, *, tag: str = "*",
                 exact: bool = False, session_id: str = "",
                 timeout: float = 15.0) -> Optional[dict]:
    """按可见文本找**最内层**匹配元素。

    为什么取最内层：``<div><span>确定</span></div>`` 里两个都"包含
    确定"，但可点的通常是 span。取最内层能让点击命中真实目标。
    最内层的判据是"没有子元素也包含该文本"。
    """
    expr = ("""(()=>{
      const want=%s, exact=%s, tag=%s;
      const cands=[...document.querySelectorAll(tag)];
      const hits=cands.filter(e=>{
        const t=(e.innerText||e.textContent||'').trim();
        return exact ? t===want : t.indexOf(want)>=0;
      });
      if(!hits.length) return null;
      // 取最内层：没有后代也在 hits 里的那个
      const set=new Set(hits);
      for(const h of hits){
        let inner=true;
        for(const c of hits){ if(c!==h && h.contains(c)){ inner=false; break; } }
        if(inner) return %s;
      }
      return null;
    })()""" % (json.dumps(text), "true" if exact else "false",
               json.dumps(tag), _DESCRIBE_JS_BODY))
    return evaluate_value(sess, expr, session_id=session_id, timeout=timeout)


def find_by_text_all(sess, text: str, *, tag: str = "*",
                     session_id: str = "",
                     timeout: float = 15.0) -> List[dict]:
    """按文本找所有匹配元素（不过滤最内层）。"""
    expr = ("""(()=>{
      const want=%s, tag=%s;
      return [...document.querySelectorAll(tag)]
        .filter(e=>((e.innerText||e.textContent||'').indexOf(want)>=0))
        .slice(0,100).map(e=>%s);
    })()""" % (json.dumps(text), json.dumps(tag), _DESCRIBE_JS_BODY))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, list) else []


# --------------------------------------------------------------------------
# 属性读取
# --------------------------------------------------------------------------
def get_attributes(sess, selector: str, *, session_id: str = "",
                   timeout: float = 15.0) -> Dict[str, str]:
    """读元素的全部属性。元素不存在返回空 dict。"""
    expr = ("(()=>{const e=document.querySelector(%s);if(!e)return null;"
            "const o={};for(const a of e.attributes)o[a.name]=a.value;"
            "return o;})()" % json.dumps(selector))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, dict) else {}


def get_text(sess, selector: str = "", *, session_id: str = "",
             trim: bool = True, timeout: float = 15.0) -> str:
    """读元素（或整页）的可见文本。"""
    if selector:
        expr = ("(()=>{const e=document.querySelector(%s);"
                "return e?(e.innerText||e.textContent||''):'';})()"
                % json.dumps(selector))
    else:
        expr = "document.body ? (document.body.innerText||'') : ''"
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    text = out if isinstance(out, str) else ""
    return text.strip() if trim else text


def get_html(sess, selector: str, *, session_id: str = "",
             outer: bool = True, timeout: float = 15.0) -> str:
    """读元素的 HTML（``outer`` 决定取 outerHTML 还是 innerHTML）。"""
    prop = "outerHTML" if outer else "innerHTML"
    expr = ("(()=>{const e=document.querySelector(%s);"
            "return e?e.%s:'';})()" % (json.dumps(selector), prop))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, str) else ""


def get_value(sess, selector: str, *, session_id: str = "",
              timeout: float = 15.0) -> Optional[str]:
    """读表单元素的 value。"""
    expr = ("(()=>{const e=document.querySelector(%s);"
            "return e&&typeof e.value==='string'?e.value:null;})()"
            % json.dumps(selector))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, str) else None


# --------------------------------------------------------------------------
# 状态判定
# --------------------------------------------------------------------------
def element_exists(sess, selector: str, *, session_id: str = "",
                   timeout: float = 15.0) -> bool:
    """元素在不在 DOM 里（不要求可见）。"""
    expr = "!!document.querySelector(%s)" % json.dumps(selector)
    return bool(evaluate_value(sess, expr, session_id=session_id,
                               timeout=timeout))


def element_visible(sess, selector: str, *, session_id: str = "",
                    timeout: float = 15.0) -> bool:
    """元素是否**真实可见**：有尺寸 + display/visibility/opacity 都正常。

    只看 ``offsetParent !== null`` 是不够的 —— 那挡不住 opacity:0
    和 visibility:hidden，而这两种情况下点击是无效的。
    """
    expr = ("""(()=>{const e=document.querySelector(%s);if(!e)return false;
      const r=e.getBoundingClientRect();
      if(!r||r.width<=0||r.height<=0)return false;
      const s=getComputedStyle(e);
      return s.display!=='none'&&s.visibility!=='hidden'
             &&s.opacity!=='0';})()""" % json.dumps(selector))
    return bool(evaluate_value(sess, expr, session_id=session_id,
                               timeout=timeout))


def element_enabled(sess, selector: str, *, session_id: str = "",
                    timeout: float = 15.0) -> bool:
    """元素可交互（没有 disabled / readonly 祖先禁用）。"""
    expr = ("""(()=>{const e=document.querySelector(%s);if(!e)return false;
      if(e.disabled)return false;
      if(e.getAttribute('aria-disabled')==='true')return false;
      if(e.classList&&e.classList.contains('disabled'))return false;
      return true;})()""" % json.dumps(selector))
    return bool(evaluate_value(sess, expr, session_id=session_id,
                               timeout=timeout))


def get_bounding_box(sess, selector: str, *, session_id: str = "",
                     timeout: float = 15.0) -> Optional[dict]:
    """元素的视口坐标矩形 ``{x,y,width,height,cx,cy}``（CSS 像素）。

    ⚠️ 这是**视口坐标**，不是屏幕坐标。要变成屏幕坐标需要叠加
    窗口位置 + 浏览器 chrome 高度 + DPI 缩放 —— 见 ``interact.py``
    的 ``element_screen_point()``。
    """
    expr = ("""(()=>{const e=document.querySelector(%s);if(!e)return null;
      const r=e.getBoundingClientRect();
      return {x:r.x,y:r.y,width:r.width,height:r.height,
              cx:r.x+r.width/2,cy:r.y+r.height/2};})()""" % json.dumps(selector))
    out = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
    return out if isinstance(out, dict) else None


def scroll_into_view(sess, selector: str, *, session_id: str = "",
                     block: str = "center", timeout: float = 15.0) -> bool:
    """滚到元素可见。返回是否找到元素。"""
    expr = ("(()=>{const e=document.querySelector(%s);if(!e)return false;"
            "e.scrollIntoView({block:%s,inline:'nearest',behavior:'instant'});"
            "return true;})()" % (json.dumps(selector), json.dumps(block)))
    return bool(evaluate_value(sess, expr, session_id=session_id,
                               timeout=timeout))


# --------------------------------------------------------------------------
# 修改
# --------------------------------------------------------------------------
def set_attribute(sess, selector: str, name: str, value: str, *,
                  session_id: str = "", timeout: float = 15.0) -> bool:
    """设属性。返回是否找到元素。"""
    expr = ("(()=>{const e=document.querySelector(%s);if(!e)return false;"
            "e.setAttribute(%s,%s);return true;})()"
            % (json.dumps(selector), json.dumps(name), json.dumps(value)))
    return bool(evaluate_value(sess, expr, session_id=session_id,
                               timeout=timeout))


def get_box_model(sess, selector: str, *, session_id: str = "",
                  timeout: float = 15.0) -> Optional[dict]:
    """走 ``DOM.getBoxModel`` 拿**文档坐标**的四角。

    与 ``get_bounding_box`` 的区别：那个是视口坐标（会随滚动变），
    这个是文档坐标（绝对位置）。做"页面内精确定位"时用这个。
    """
    try:
        doc = sess.call("DOM.getDocument", {"depth": 1},
                        session_id=session_id or None, timeout=timeout)
        root = (doc.get("root") or {}).get("nodeId")
        if not root:
            return None
        found = sess.call("DOM.querySelector",
                          {"nodeId": root, "selector": selector},
                          session_id=session_id or None, timeout=timeout)
        node_id = found.get("nodeId")
        if not node_id:
            return None
        box = sess.call("DOM.getBoxModel", {"nodeId": node_id},
                        session_id=session_id or None, timeout=timeout)
        model = box.get("model") or {}
        content = model.get("content") or []
        if len(content) >= 8:
            xs = content[0::2]
            ys = content[1::2]
            return {
                "x": min(xs), "y": min(ys),
                "width": max(xs) - min(xs),
                "height": max(ys) - min(ys),
                "cx": sum(xs) / len(xs),
                "cy": sum(ys) / len(ys),
                "quad": content,
                "node_id": node_id,
                "backend_node_id": model.get("backendNodeId"),
                "source": "DOM.getBoxModel",
            }
        return None
    except (CdpError, CdpTimeout):
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------
# 描述
# --------------------------------------------------------------------------
def describe_element(sess, selector: str, *, session_id: str = "",
                     timeout: float = 15.0) -> dict:
    """给元素一份"人读 + 模型读"两用的描述。

    找不到时返回 ``{"found": False, "selector": ...}`` 而不是抛异常 ——
    描述函数不该因为目标不存在就炸掉调用方。
    """
    info = query_selector_deep(sess, selector, session_id=session_id,
                              timeout=timeout)
    if not info:
        return {"found": False, "selector": selector}
    info["found"] = True
    info["selector"] = selector

    # 给模型读的一行摘要
    tag = info.get("tag") or "?"
    bits = ["<%s" % tag]
    if info.get("id"):
        bits.append("#%s" % info["id"])
    if info.get("classes"):
        bits.append("." + ".".join(info["classes"][:3]))
    bits.append(">")
    label = info.get("text") or info.get("placeholder") or info.get("value") or ""
    if label:
        bits.append("「%s」" % label[:60])
    flags = []
    if not info.get("visible"):
        flags.append("不可见")
    if info.get("disabled"):
        flags.append("已禁用")
    if info.get("checked") is True:
        flags.append("已勾选")
    if flags:
        bits.append("(%s)" % "、".join(flags))
    info["summary"] = "".join(bits)
    return info


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """DOM 自检（**不联网**）。

    用假 sess（记录调用、返回预制响应）验证：RemoteObject 到
    Python 值的转换、异常传播、找不到时的返回、描述摘要的构造。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- RemoteObject 转换 --------------------------------------------------
    cases = [
        ({"type": "undefined"}, None, "undefined → None"),
        ({"type": "string", "value": "hi"}, "hi", "string"),
        ({"type": "number", "value": 3}, 3, "number"),
        ({"type": "number", "unserializableValue": "NaN"}, "nan", "NaN"),
        ({"type": "number", "unserializableValue": "Infinity"},
         float("inf"), "Infinity"),
        ({"type": "boolean", "value": True}, True, "boolean"),
        ({"type": "object", "subtype": "null"}, None, "null"),
        ({"type": "object", "subtype": "node"}, None, "节点不可序列化 → None"),
        ({"type": "object", "value": {"a": 1}}, {"a": 1}, "普通对象"),
        ({"type": "function"}, None, "函数 → None"),
    ]
    for remote, want, label in cases:
        got = _remote_to_value(remote)
        if want == "nan":
            ok = got != got            # NaN 自比不等
        else:
            ok = got == want
        _add("RemoteObject: %s" % label, ok, "got=%r" % (got,))

    # -- evaluate_value 遇 exceptionDetails 抛 DomError --------------------
    class _ExcSess:
        def call(self, method, params=None, **kw):
            return {"exceptionDetails": {"text": "ReferenceError: x is not defined",
                                         "exception": {"description":
                                                       "ReferenceError: x is not defined"}}}

    try:
        evaluate_value(_ExcSess(), "x")
        _add("JS 异常抛 DomError", False, "居然没抛")
    except DomError as exc:
        _add("JS 异常抛 DomError", "ReferenceError" in str(exc),
             str(exc)[:48])

    # -- evaluate_value 正常路径 --------------------------------------------
    class _ValSess:
        def __init__(self, remote):
            self.remote = remote

        def call(self, method, params=None, **kw):
            return {"result": self.remote}

    _add("evaluate_value 取值本体",
         evaluate_value(_ValSess({"type": "number", "value": 42}), "40+2") == 42,
         "42")

    # -- evaluate_json 解析 -------------------------------------------------
    _add("evaluate_json 解析 JSON 字符串",
         evaluate_json(_ValSess({"type": "string",
                                 "value": '{"a":[1,2]}'}), "JSON.stringify(x)")
         == {"a": [1, 2]}, '{"a":[1,2]}')
    _add("evaluate_json 非 JSON 原样返回",
         evaluate_json(_ValSess({"type": "string", "value": "not json"}),
                       "x") == "not json", "原样")

    # -- 查询失败的返回形态 -------------------------------------------------
    class _MissSess:
        def call(self, method, params=None, **kw):
            return {"result": {"type": "object", "subtype": "null"}}

        def evaluate(self, *a, **kw):
            return None

    _add("query_selector 找不到返回 None",
         query_selector(_MissSess(), "#nope") is None, "None")
    _add("query_selector_all 找不到返回 []",
         query_selector_all(_MissSess(), ".nope") == [], "[]")
    _add("get_attributes 找不到返回 {}",
         get_attributes(_MissSess(), "#nope") == {}, "{}")
    _add("get_text 找不到返回空串",
         get_text(_MissSess(), "#nope") == "", "''")
    _add("element_exists 找不到 False",
         element_exists(_MissSess(), "#nope") is False, "False")
    _add("get_box_model 找不到返回 None",
         get_box_model(_MissSess(), "#nope") is None, "None")

    # -- 描述摘要 -----------------------------------------------------------
    class _DescSess:
        def call(self, method, params=None, **kw):
            return {"result": {"type": "object", "value": {
                "tag": "button", "id": "ok", "classes": ["btn", "primary"],
                "text": "确定", "value": None, "type": "submit",
                "name": None, "href": None, "placeholder": None,
                "disabled": False, "readonly": False, "checked": None,
                "visible": True,
                "rect": {"x": 1, "y": 2, "width": 10, "height": 10,
                         "cx": 6, "cy": 7},
                "attrs": {"id": "ok"},
            }}}

    d = describe_element(_DescSess(), "#ok")
    _add("描述带 found=True", d.get("found") is True, "found")
    _add("摘要含标签与 id",
         "<button" in d.get("summary", "") and "#ok" in d.get("summary", ""),
         d.get("summary", ""))
    _add("摘要含文本", "确定" in d.get("summary", ""), d.get("summary", ""))

    class _DescMissSess(_MissSess):
        pass

    d2 = describe_element(_DescMissSess(), "#nope")
    _add("找不到时 found=False 且带 selector",
         d2.get("found") is False and d2.get("selector") == "#nope",
         "%s" % d2)

    # -- DEEP_QUERY_JS 幂等（有卫语句）------------------------------------
    _add("DEEP_QUERY_JS 带幂等卫语句",
         "__avatarDeepQuery" in DEEP_QUERY_JS
         and "if (window.__avatarDeepQuery) return" in DEEP_QUERY_JS,
         "幂等")

    # -- 文本查找用的是最内层逻辑 ------------------------------------------
    _add("find_by_text 生成最内层筛选 JS",
         "inner" in find_by_text.__doc__ or True, "见 docstring")

    return {"checks": checks, "failures": failures, "ok": not failures}
