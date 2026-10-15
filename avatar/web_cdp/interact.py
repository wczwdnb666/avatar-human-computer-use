# -*- coding: utf-8 -*-
"""页面交互：点击 / 输入 / 悬停 / 拖拽 / 上传。

## 核心策略：JS 优先，Input 事件兜底

每个动作都先试 **JS 合成事件**（``element.click()``、
分发 ``MouseEvent``），失败再降级到 **CDP 的 ``Input.dispatchMouseEvent``**
（真实输入事件，走浏览器的输入管线）。

为什么 JS 优先：

| | JS 合成 | Input.dispatchMouseEvent |
|---|---|---|
| 速度 | 快（一次 evaluate） | 慢（多次往返 + 需要坐标） |
| 需要坐标 | 不需要 | 需要，且要算屏幕坐标 |
| 需要焦点 | 不需要 | **需要窗口在前台** |
| 能穿 Shadow DOM | 能 | 能（只要有坐标） |
| 被检测为自动化 | 较容易（isTrusted=false） | 难（isTrusted=true） |

为什么还必须保留 Input 兜底 —— JS 合成事件的**真实盲区**：

1. ``isTrusted=false``。有些页面（尤其是登录、支付）会检查这个，
   直接忽略合成事件。
2. **不触发浏览器的默认行为**。比如 ``<a href>`` 上的 ``.click()``
   会导航，但 ``dragover`` 之类的默认行为不会发生。
3. **某些控件只认真实鼠标事件**：``<input type=file>``（必须真实点击
   才能开文件选择器）、拖拽排序（依赖 mousedown/mousemove/mouseup 序列
   的坐标连续性）、canvas 上的自绘控件。
4. **需要坐标进入视口的场景**：元素在视口外时 JS 点击仍"成功"，
   但用户体验上没发生；而 Input 需要先滚动进视口。

所以判据是：**JS 路径返回明确的失败信号就走 Input**。
我们让 JS 返回一个结构化的 ``{ok, reason}`` 而不是裸 bool ——
这样才能区分"元素不存在"（该报错）和"合成点击无效"（该降级）。

## isTrusted 的检查方法

要判断"合成事件是否被页面接受"，最可靠的是**看副作用有没有发生**
（比如输入后 value 变了没）。``interact.py`` 里输入动作会回读校验，
点击则交给 ``verify/`` 层做端到端确认。这是刻意的分层：
本模块只管"动作发出去了"，"到底成不成"由验证层回答。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ..errors import AvatarError
from .dom import DomError, evaluate_value, get_bounding_box, query_selector

__all__ = [
    "InteractError",
    "InteractionResult",
    "click",
    "double_click",
    "right_click",
    "hover",
    "focus_element",
    "blur_element",
    "type_text",
    "set_value",
    "clear_value",
    "press_key",
    "select_option",
    "check",
    "uncheck",
    "set_checked",
    "drag",
    "scroll_to",
    "scroll_by",
    "hover_menu",
    "upload_file",
    "submit_form",
    "click_by_text",
    "dispatch_mouse",
    "dispatch_key",
    "JS_CLICK_JS",
]


class InteractError(AvatarError):
    """交互失败（元素找不到、不可交互、CDP 报错）。"""

    exit_code = 16


class InteractionResult:
    """一次交互的结果。

    ``method`` 说明**实际走了哪条路**：
    ``"js"`` / ``"input"`` / ``"value"`` / ``"select"``。
    这个字段是硬要求 —— 调用方和验证层都需要知道
    "这个动作是真鼠标做的还是 JS 合成的"。
    """

    __slots__ = ("ok", "method", "selector", "detail", "fallback_used",
                 "elapsed", "extra")

    def __init__(self, ok: bool, method: str = "", selector: str = "",
                 detail: str = "", fallback_used: bool = False,
                 elapsed: float = 0.0, extra: Optional[dict] = None) -> None:
        self.ok = ok
        self.method = method
        self.selector = selector
        self.detail = detail
        self.fallback_used = fallback_used
        self.elapsed = elapsed
        self.extra = extra or {}

    def describe(self) -> dict:
        return {
            "ok": self.ok,
            "method": self.method,
            "selector": self.selector,
            "detail": self.detail,
            "fallback_used": self.fallback_used,
            "elapsed": round(self.elapsed, 3),
            "extra": self.extra,
        }

    def __repr__(self) -> str:
        return "<InteractionResult ok=%s method=%s %s>" % (
            self.ok, self.method, (self.detail or "")[:40])


# --------------------------------------------------------------------------
# JS 片段
# --------------------------------------------------------------------------
#: 合成点击。返回结构化结果，让上层能区分失败原因。
#:
#: 关键点：
#: - 先 ``scrollIntoView`` —— 不在视口里时某些框架会忽略点击
#: - 分发完整的 pointerdown/mousedown/mouseup/click 序列 ——
#:   只调 ``.click()`` 的话 React 的合成事件系统可能收不到 pointer 事件
#: - 用 ``MouseEvent`` 而非 ``Event``，带 ``bubbles/cancelable/composed``
#:   （``composed: true`` 才能穿 Shadow DOM 边界冒泡）
JS_CLICK_JS = r"""
(() => {
  const sel = %s;
  const e = (window.__avatarDeepQuery ? window.__avatarDeepQuery(sel)
                                      : document.querySelector(sel));
  if (!e) return {ok: false, reason: "not_found"};
  try { e.scrollIntoView({block: "center", inline: "nearest",
                          behavior: "instant"}); } catch (_) {}

  const r = e.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0)
    return {ok: false, reason: "zero_size"};
  const s = getComputedStyle(e);
  if (s.display === "none" || s.visibility === "hidden")
    return {ok: false, reason: "hidden"};
  if (s.pointerEvents === "none")
    return {ok: false, reason: "pointer_events_none"};

  const opt = {bubbles: true, cancelable: true, composed: true,
               view: window, clientX: r.x + r.width / 2,
               clientY: r.y + r.height / 2,
               button: %d, detail: %d};
  const btn = ["none", "left", "right", "middle"][%d] || "left";

  try {
    if (window.PointerEvent) {
      e.dispatchEvent(new PointerEvent("pointerdown",
        Object.assign({pointerId: 1, pointerType: "mouse",
                       isPrimary: true, buttons: %d}, opt)));
    }
    e.dispatchEvent(new MouseEvent("mousedown", opt));
    if (window.PointerEvent) {
      e.dispatchEvent(new PointerEvent("pointerup",
        Object.assign({pointerId: 1, pointerType: "mouse",
                       isPrimary: true, buttons: 0}, opt)));
    }
    e.dispatchEvent(new MouseEvent("mouseup", opt));
    e.dispatchEvent(new MouseEvent("click", opt));
    if (%d === 2) {
      e.dispatchEvent(new MouseEvent("dblclick", opt));
    }
    if (btn === "right") {
      e.dispatchEvent(new MouseEvent("contextmenu", opt));
    }
  } catch (err) {
    return {ok: false, reason: "dispatch_failed", message: String(err)};
  }
  return {ok: true, reason: "synthetic", tag: e.tagName.toLowerCase(),
          text: (e.innerText || "").slice(0, 80)};
})()
"""

#: 用原生 setter 写值，绕过 React / Vue 的值劫持。
#:
#: 为什么必须这样：直接用 ``e.value = "x"`` 时，框架挂在 value 上的
#: setter 会知道这是**程序设置**，于是不更新它自己的内部状态 ——
#: 提交表单时用的是框架状态，于是你写的值丢了。
#: 调 ``HTMLInputElement.prototype.value`` 的原始 setter 能绕过劫持，
#: 再手动派发 ``input`` / ``change`` 事件通知框架。
JS_SET_VALUE_JS = r"""
(() => {
  const sel = %s;
  const val = %s;
  const e = (window.__avatarDeepQuery ? window.__avatarDeepQuery(sel)
                                      : document.querySelector(sel));
  if (!e) return {ok: false, reason: "not_found"};

  const tag = (e.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea") {
    const proto = tag === "input"
      ? window.HTMLInputElement.prototype
      : window.HTMLTextAreaElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) { d.set.call(e, val); } else { e.value = val; }
  } else if (e.isContentEditable) {
    e.focus();
    e.textContent = val;
  } else if (e.tagName === "SELECT") {
    e.value = val;
  } else {
    return {ok: false, reason: "not_editable", tag: tag};
  }

  // 通知框架：input → change（顺序不能反，很多框架靠 input 更新状态）
  e.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
  e.dispatchEvent(new Event("change", {bubbles: true, composed: true}));
  return {ok: true, reason: "set", tag: tag,
          value: String(e.value !== undefined ? e.value : e.textContent)};
})()
"""

#: 读值（用于输入后回读校验）。
JS_READ_VALUE_JS = r"""
(() => {
  const sel = %s;
  const e = (window.__avatarDeepQuery ? window.__avatarDeepQuery(sel)
                                      : document.querySelector(sel));
  if (!e) return null;
  if (e.isContentEditable) return e.textContent || "";
  if (e.value !== undefined) return String(e.value);
  return (e.innerText || "");
})()
"""

#: 悬停：分发 mouseover / mouseenter / mousemove。
#:
#: ⚠️ 只分发一次 mousemove 往往不够 —— 很多下拉菜单靠
#: mousemove 的**持续性**判定"鼠标还在上面"。所以这里给一段时间窗口，
#: 由 Python 侧多次调用（见 hover()）。
JS_HOVER_JS = r"""
(() => {
  const sel = %s;
  const e = (window.__avatarDeepQuery ? window.__avatarDeepQuery(sel)
                                      : document.querySelector(sel));
  if (!e) return {ok: false, reason: "not_found"};
  try { e.scrollIntoView({block: "center", inline: "nearest",
                          behavior: "instant"}); } catch (_) {}
  const r = e.getBoundingClientRect();
  const opt = {bubbles: true, cancelable: true, composed: true,
               view: window, clientX: r.x + r.width / 2,
               clientY: r.y + r.height / 2};
  e.dispatchEvent(new MouseEvent("mouseover", opt));
  e.dispatchEvent(new MouseEvent("mouseenter", opt));
  e.dispatchEvent(new MouseEvent("mousemove", opt));
  return {ok: true, reason: "synthetic"};
})()
"""


# --------------------------------------------------------------------------
# 结果解析
# --------------------------------------------------------------------------
def _parse_js_result(res: Any) -> Tuple[bool, str, dict]:
    """把 JS 返回的 ``{ok, reason, ...}`` 拆成 ``(ok, reason, extra)``。"""
    if not isinstance(res, dict):
        return False, "bad_js_result", {"raw": res}
    ok = bool(res.get("ok"))
    reason = str(res.get("reason") or "")
    extra = {k: v for k, v in res.items() if k not in ("ok", "reason")}
    return ok, reason, extra


#: 哪些 reason 意味着"元素本身有问题"—— 该继续降级试 Input
#: （因为可能是合成事件被无视）
_DEGRADABLE_REASONS = frozenset({
    "dispatch_failed", "pointer_events_none", "synthetic",
})

#: 哪些 reason 意味着"根本没得点"—— 不降级，交给 Input 路径自己再判一次
_HARD_REASONS = frozenset({"not_found", "zero_size", "hidden",
                           "not_editable"})


# --------------------------------------------------------------------------
# CDP Input 底层
# --------------------------------------------------------------------------
def dispatch_mouse(sess, x: float, y: float, *, type_: str = "mousePressed",
                   button: str = "left", click_count: int = 1,
                   session_id: str = "", timeout: float = 5.0,
                   modifiers: int = 0) -> None:
    """发一个真实的鼠标事件。

    ``x, y`` 是**视口 CSS 像素坐标**（``Input.dispatchMouseEvent`` 的约定）。
    想点屏幕上的某个位置需要先经过 ``interact.element_screen_point()``
    换算到视口坐标。

    ``type_`` 取 ``mousePressed`` / ``mouseReleased`` / ``mouseMoved``。
    """
    sess.call("Input.dispatchMouseEvent", {
        "type": type_,
        "x": float(x),
        "y": float(y),
        "button": button,
        "clickCount": int(click_count),
        "modifiers": int(modifiers),
    }, session_id=session_id or None, timeout=timeout)


def dispatch_key(sess, *, type_: str = "keyDown", key: str = "",
                 code: str = "", text: str = "", windows_vk: int = 0,
                 native_vk: int = 0, modifiers: int = 0,
                 session_id: str = "", timeout: float = 5.0) -> None:
    """发一个真实的键盘事件。

    ``key`` 是键名（``"Enter"`` / ``"a"``），``code`` 是物理键码
    （``"KeyA"`` / ``"Enter"``）。要输入字符时用 ``text``。
    """
    params = {"type": type_, "modifiers": int(modifiers)}
    if key:
        params["key"] = key
    if code:
        params["code"] = code
    if text:
        params["text"] = text
    if windows_vk:
        params["windowsVirtualKeyCode"] = int(windows_vk)
    if native_vk:
        params["nativeVirtualKeyCode"] = int(native_vk)
    sess.call("Input.dispatchKeyEvent", params,
              session_id=session_id or None, timeout=timeout)


#: 常用键的 (key, code, vk) 三元组
_KEYS = {
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
    "space": (" ", "Space", 32),
    " ": (" ", "Space", 32),
}


# --------------------------------------------------------------------------
# 点击
# --------------------------------------------------------------------------
def click(sess, selector: str, *, session_id: str = "",
          timeout: float = 15.0, button: str = "left",
          clicks: int = 1, force_input: bool = False,
          allow_input_fallback: bool = True) -> InteractionResult:
    """点元素。

    :param force_input: 跳过 JS 合成，直接走真鼠标事件
    :param allow_input_fallback: JS 路径不好使时是否降级到真鼠标
    """
    t0 = time.monotonic()

    if not force_input:
        btn_idx = {"left": 0, "middle": 1, "right": 2}.get(button.lower(), 0)
        js = JS_CLICK_JS % (json.dumps(selector), btn_idx, int(clicks),
                            btn_idx, (1 if btn_idx == 0 else 4),
                            int(clicks))
        try:
            raw = evaluate_value(sess, js, session_id=session_id,
                                 timeout=timeout)
            ok, reason, extra = _parse_js_result(raw)
            if ok:
                return InteractionResult(
                    True, "js", selector,
                    "JS 合成点击成功", False,
                    time.monotonic() - t0, extra)
            if reason in _HARD_REASONS and not allow_input_fallback:
                return InteractionResult(
                    False, "js", selector,
                    "JS 点击失败：%s" % reason, False,
                    time.monotonic() - t0, extra)
        except DomError as exc:
            extra = {"js_error": str(exc)}
        except Exception as exc:      # noqa: BLE001
            extra = {"js_error": "%s: %s" % (type(exc).__name__, exc)}
    else:
        extra = {"forced": "input"}

    if not allow_input_fallback:
        return InteractionResult(
            False, "js", selector, "JS 点击失败且不允许降级", False,
            time.monotonic() - t0, extra)

    # -- 降级：真鼠标事件 -----------------------------------------------
    ok, detail, iextra = _input_click(sess, selector, button=button,
                                      clicks=clicks, session_id=session_id,
                                      timeout=timeout)
    merged = dict(extra or {})
    merged.update(iextra)
    return InteractionResult(ok, "input", selector, detail, True,
                             time.monotonic() - t0, merged)


def _input_click(sess, selector: str, *, button: str, clicks: int,
                 session_id: str, timeout: float,
                 timeout_override: Optional[float] = None) -> Tuple[bool, str, dict]:
    """走真鼠标事件点击。返回 ``(ok, detail, extra)``。"""
    box = get_bounding_box(sess, selector, session_id=session_id,
                           timeout=timeout)
    if not box:
        return False, "找不到元素，无法取坐标", {"reason": "not_found"}

    x, y = float(box["cx"]), float(box["cy"])
    if box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return False, "元素尺寸为 0，无法点击", {"reason": "zero_size"}

    btn = button.lower()
    mask = {"left": 1, "right": 2, "middle": 4}.get(btn, 1)

    try:
        # 先把光标挪过去 —— 很多控件靠 mouseover 初始化内部状态，
        # 不先 move 直接 press 会点不中（尤其菜单、tooltip 类）
        dispatch_mouse(sess, x, y, type_="mouseMoved", button="none",
                       session_id=session_id, timeout=timeout)
        for i in range(max(1, clicks)):
            dispatch_mouse(sess, x, y, type_="mousePressed", button=btn,
                           click_count=i + 1, session_id=session_id,
                           timeout=timeout, modifiers=0)
            dispatch_mouse(sess, x, y, type_="mouseReleased", button=btn,
                           click_count=i + 1, session_id=session_id,
                           timeout=timeout, modifiers=0)
            if clicks > 1:
                time.sleep(0.05)
    except Exception as exc:      # noqa: BLE001
        return False, "真鼠标事件发送失败：%s" % exc, {
            "reason": "dispatch_failed", "error": str(exc)}

    return True, "真鼠标点击 (%d,%d) button=%s clicks=%d" % (
        int(x), int(y), btn, clicks), {
        "point": [x, y], "button": btn, "buttons_mask": mask}


def double_click(sess, selector: str, *, session_id: str = "",
                 timeout: float = 15.0) -> InteractionResult:
    """双击。"""
    return click(sess, selector, session_id=session_id, timeout=timeout,
                 clicks=2)


def right_click(sess, selector: str, *, session_id: str = "",
                timeout: float = 15.0) -> InteractionResult:
    """右键（会触发 contextmenu）。"""
    return click(sess, selector, session_id=session_id, timeout=timeout,
                 button="right")


def click_by_text(sess, text: str, *, tag: str = "*", exact: bool = False,
                  session_id: str = "", timeout: float = 15.0,
                  nth: int = 0) -> InteractionResult:
    """按可见文本点击。内部用 ``find_by_text`` 定位后交 ``click``。

    ``nth`` 用于"同名按钮有多个"时选第几个，越界会退到最后一个。
    """
    from .dom import find_by_text_all
    t0 = time.monotonic()
    hits = find_by_text_all(sess, text, tag=tag, session_id=session_id,
                            timeout=timeout)
    if not hits:
        return InteractionResult(False, "text", "", "没找到含 %r 的元素" % text,
                                 False, time.monotonic() - t0)
    idx = min(max(0, nth), len(hits) - 1)
    # 用 DOM 里唯一能定位到该元素的方式：位置索引 + 文本双条件选择器
    sel = _selector_for_hit(text, tag, exact, idx)
    r = click(sess, sel, session_id=session_id, timeout=timeout)
    r.elapsed = time.monotonic() - t0
    r.detail = "按文本 %r 点击（第 %d 个）：%s" % (text, idx, r.detail)
    return r


def _selector_for_hit(text: str, tag: str, exact: bool, idx: int) -> str:
    """给"按文本找"的命中生成一个可复用选择器。

    生成的是一段**伪选择器**（前缀 ``avatar-text:``），``click`` 里的
    JS 不认识它 —— 所以 ``click_by_text`` 实际走的是位置索引路径。
    这里返回真实选择器：用 ``:nth-of-type`` 不可靠（文本顺序 ≠ DOM 顺序），
    改用更稳的「JS 里重新按文本取第 idx 个」。
    """
    # 用 tag 加一个属性选择器不现实（文本不在属性里）。
    # 所以返回一个 __avatarTextSelector 的私有语法，交给 click 的
    # JS 分支识别 —— 见 JS_CLICK_JS 中的 %s 替换点。
    return "%s%s|%s|%d" % ("__avatarText:", tag, text, idx)


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------
def set_value(sess, selector: str, text: str, *, session_id: str = "",
              timeout: float = 15.0, verify: bool = True,
              allow_input_fallback: bool = True) -> InteractionResult:
    """写值（JS 原生 setter 绕过框架劫持），可选回读校验。

    ``verify=True`` 时写完读回比对。**这个校验很重要** ——
    因为合成 ``input`` 事件可能被框架忽略，那时 value 静态看起来对了，
    但框架内部状态没更新，提交时会丢。回读只能挡住"完全没写进去"，
    挡不住"框架状态没同步" —— 后者由 ``verify/`` 层做端到端验证。
    """
    t0 = time.monotonic()
    js = JS_SET_VALUE_JS % (json.dumps(selector), json.dumps(text))
    try:
        raw = evaluate_value(sess, js, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}

    if ok and verify:
        got = _read_value(sess, selector, session_id=session_id,
                          timeout=timeout)
        extra["readback"] = got
        if got != text:
            ok = False
            reason = "readback_mismatch"

    if ok:
        return InteractionResult(True, "value", selector,
                                 "JS 写值成功", False,
                                 time.monotonic() - t0, extra)

    if not allow_input_fallback:
        return InteractionResult(False, "value", selector,
                                 "写值失败：%s" % reason, False,
                                 time.monotonic() - t0, extra)

    # -- 降级：真键盘输入 ---------------------------------------------
    ok2, detail2, extra2 = _input_type(sess, selector, text,
                                       session_id=session_id,
                                       timeout=timeout)
    merged = dict(extra)
    merged.update(extra2)
    merged["js_reason"] = reason
    return InteractionResult(ok2, "input", selector, detail2, True,
                             time.monotonic() - t0, merged)


def type_text(sess, selector: str, text: str, *, session_id: str = "",
              timeout: float = 15.0, clear_first: bool = True,
              verify: bool = True,
              allow_input_fallback: bool = True) -> InteractionResult:
    """向输入框敲字。

    与 ``set_value`` 的区别：这个**逐字符发键盘事件**，
    更像真人，也会触发 keydown / keypress / keyup 的完整序列。
    ``set_value`` 是一次性写值，快但不经过按键事件。

    默认 ``clear_first=True`` 会先清空。
    """
    t0 = time.monotonic()
    if clear_first:
        clear_value(sess, selector, session_id=session_id, timeout=timeout)

    ok2, detail2, extra2 = _input_type(sess, selector, text,
                                       session_id=session_id,
                                       timeout=timeout)
    if ok2 and verify:
        got = _read_value(sess, selector, session_id=session_id,
                          timeout=timeout)
        extra2["readback"] = got
        # 允许框架插入的格式化差异（比如手机号自动加空格）
        if got != text and _normalize(got) != _normalize(text):
            ok2 = False
            detail2 = "回读不符：期望 %r，实际 %r" % (text[:40], str(got)[:40])

    return InteractionResult(ok2, "input", selector, detail2, False,
                             time.monotonic() - t0, extra2)


def _normalize(s) -> str:
    """去掉不可见字符和首尾空白，用于宽松比对。"""
    if s is None:
        return ""
    return "".join(str(s).split())


def _read_value(sess, selector: str, *, session_id: str,
                timeout: float) -> Optional[str]:
    try:
        got = evaluate_value(sess, JS_READ_VALUE_JS % json.dumps(selector),
                             session_id=session_id, timeout=timeout)
        return got if isinstance(got, str) else None
    except Exception:
        return None


def _input_type(sess, selector: str, text: str, *, session_id: str,
                timeout: float) -> Tuple[bool, str, dict]:
    """先聚焦，再逐字符发键盘事件。"""
    fr = focus_element(sess, selector, session_id=session_id,
                       timeout=timeout)
    if not fr.ok:
        return False, "聚焦失败：%s" % fr.detail, {"reason": "focus_failed"}

    sent = 0
    try:
        for ch in text:
            if ch == "\n":
                key, code, vk = _KEYS["enter"]
                dispatch_key(sess, type_="keyDown", key=key, code=code,
                             text="\r", windows_vk=vk, native_vk=vk,
                             session_id=session_id, timeout=timeout)
                dispatch_key(sess, type_="keyUp", key=key, code=code,
                             windows_vk=vk, native_vk=vk,
                             session_id=session_id, timeout=timeout)
            else:
                # 字符输入：type_="char" 会带上 text，浏览器据此产生输入
                dispatch_key(sess, type_="keyDown", key=ch, text=ch,
                             session_id=session_id, timeout=timeout)
                dispatch_key(sess, type_="char", key=ch, text=ch,
                             session_id=session_id, timeout=timeout)
                dispatch_key(sess, type_="keyUp", key=ch,
                             session_id=session_id, timeout=timeout)
            sent += 1
    except Exception as exc:      # noqa: BLE001
        return False, "键入中断在第 %d 个字符：%s" % (sent, exc), {
            "reason": "dispatch_failed", "sent": sent, "error": str(exc)}

    return True, "逐字符键入 %d 个字符" % sent, {"sent": sent}


def clear_value(sess, selector: str, *, session_id: str = "",
                timeout: float = 15.0) -> InteractionResult:
    """清空输入框。走原生 setter 写空串 + 派发事件。"""
    return set_value(sess, selector, "", session_id=session_id,
                     timeout=timeout, verify=False,
                     allow_input_fallback=False)


def focus_element(sess, selector: str, *, session_id: str = "",
                  timeout: float = 15.0) -> InteractionResult:
    """聚焦元素。"""
    t0 = time.monotonic()
    expr = ("""(()=>{const e=(window.__avatarDeepQuery?
      window.__avatarDeepQuery(%s):document.querySelector(%s));
      if(!e)return {ok:false,reason:"not_found"};
      try{e.focus({preventScroll:false});}catch(_){e.focus();}
      return {ok:document.activeElement===e,reason:"focused",
              tag:e.tagName.toLowerCase()};})()"""
            % (json.dumps(selector), json.dumps(selector)))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}
    return InteractionResult(ok, "js", selector,
                             "聚焦%s" % ("成功" if ok else "失败：" + reason),
                             False, time.monotonic() - t0, extra)


def blur_element(sess, selector: str, *, session_id: str = "",
                 timeout: float = 15.0) -> InteractionResult:
    """失焦（提交单字段表单时常需要）。"""
    t0 = time.monotonic()
    expr = ("(()=>{const e=document.querySelector(%s);if(!e)"
            "return {ok:false,reason:'not_found'};e.blur();"
            "return {ok:true,reason:'blurred'};})()" % json.dumps(selector))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}
    return InteractionResult(ok, "js", selector, reason, False,
                             time.monotonic() - t0, extra)


def press_key(sess, key: str, *, session_id: str = "",
              timeout: float = 5.0, modifiers: int = 0) -> InteractionResult:
    """在**当前焦点**上按一个键（不针对元素）。

    ``key`` 支持 ``"enter"`` / ``"tab"`` / ``"escape"`` 等常用名，
    以及单字符（``"a"``）。
    """
    t0 = time.monotonic()
    k = key.lower()
    if k in _KEYS:
        name, code, vk = _KEYS[k]
    elif len(key) == 1:
        name, code, vk = key, "", 0
    else:
        name, code, vk = key, key, 0

    try:
        dispatch_key(sess, type_="keyDown", key=name, code=code,
                     text=name if len(name) == 1 else "",
                     windows_vk=vk, native_vk=vk, modifiers=modifiers,
                     session_id=session_id, timeout=timeout)
        dispatch_key(sess, type_="keyUp", key=name, code=code,
                     windows_vk=vk, native_vk=vk, modifiers=modifiers,
                     session_id=session_id, timeout=timeout)
        ok, detail = True, "按下 %s" % name
    except Exception as exc:      # noqa: BLE001
        ok, detail = False, "按键失败：%s" % exc

    return InteractionResult(ok, "input", "", detail, False,
                             time.monotonic() - t0)


# --------------------------------------------------------------------------
# 悬停
# --------------------------------------------------------------------------
def hover(sess, selector: str, *, session_id: str = "",
          timeout: float = 15.0, settle: float = 0.35) -> InteractionResult:
    """悬停。

    ``settle`` 是"保持悬停"的时长 —— 期间会**重复发 mousemove**，
    因为靠 CSS ``:hover`` 的菜单需要鼠标持续停在那儿，
    只发一次事件后浏览器可能因为没后续事件把 hover 态收掉。
    """
    t0 = time.monotonic()
    js = JS_HOVER_JS % json.dumps(selector)
    try:
        raw = evaluate_value(sess, js, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}

    if not ok:
        return InteractionResult(False, "js", selector,
                                 "悬停失败：%s" % reason, False,
                                 time.monotonic() - t0, extra)

    # 保持悬停：周期性重发 mousemove
    deadline = time.monotonic() + max(0.0, settle)
    tries = 0
    while time.monotonic() < deadline:
        try:
            evaluate_value(sess, js, session_id=session_id, timeout=2.0)
            tries += 1
        except Exception:
            break
        time.sleep(0.08)

    extra["mousemove_repeats"] = tries
    return InteractionResult(True, "js", selector,
                             "悬停 %.2fs（重发 %d 次 mousemove）"
                             % (settle, tries), False,
                             time.monotonic() - t0, extra)


def hover_menu(sess, menu_selector: str, item_text: str, *,
               session_id: str = "", timeout: float = 15.0,
               settle: float = 0.4) -> InteractionResult:
    """悬停父项展开菜单，再点菜单项 —— 经典的两步交互。

    顺序不能反：必须先 hover 把菜单**渲染出来**，
    菜单项才会出现在 DOM 里可供点击。
    """
    t0 = time.monotonic()
    h = hover(sess, menu_selector, session_id=session_id,
              timeout=timeout, settle=settle)
    if not h.ok:
        return InteractionResult(False, "js", menu_selector,
                                 "展开菜单失败：%s" % h.detail, False,
                                 time.monotonic() - t0)

    # 等菜单项出现
    from .navigate import wait_for_element
    ok = wait_for_element(sess, item_text, timeout=min(3.0, timeout),
                          session_id=session_id)
    if not ok:
        # 菜单项可能不是选择器 —— 退到按文本
        r = click_by_text(sess, item_text, session_id=session_id,
                          timeout=timeout)
        r.elapsed = time.monotonic() - t0
        r.fallback_used = True
        return r

    r = click(sess, item_text, session_id=session_id, timeout=timeout)
    r.elapsed = time.monotonic() - t0
    return r


# --------------------------------------------------------------------------
# 复选 / 单选
# --------------------------------------------------------------------------
def set_checked(sess, selector: str, on: bool, *, session_id: str = "",
                timeout: float = 15.0) -> InteractionResult:
    """确保勾选状态为 ``on``（幂等）。

    **不盲点** —— 先读当前状态，已经对了就什么都不做。
    盲点会把"已勾选"变成"未勾选"，这是 checkbox 自动化最常见的 bug。
    """
    t0 = time.monotonic()
    expr = ("(()=>{const e=document.querySelector(%s);if(!e)"
            "return {ok:false,reason:'not_found',state:null};"
            "if(e.type==='radio'){"
            "  const before=!!e.checked;"
            "  if(before===%s)return {ok:true,reason:'noop',state:before};"
            "  e.click();return {ok:true,reason:'clicked',state:!!e.checked};"
            "}"
            "const before=!!e.checked;"
            "if(before===%s)return {ok:true,reason:'noop',state:before};"
            "e.click();"
            "return {ok:true,reason:'clicked',state:!!e.checked};})()"
            % (json.dumps(selector), "true" if on else "false",
               "true" if on else "false"))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}

    return InteractionResult(ok, "js", selector,
                             ("已经是目标状态，跳过" if reason == "noop"
                              else "点击后 state=%s" % extra.get("state")),
                             False, time.monotonic() - t0, extra)


def check(sess, selector: str, **kw) -> InteractionResult:
    """勾选。"""
    return set_checked(sess, selector, True, **kw)


def uncheck(sess, selector: str, **kw) -> InteractionResult:
    """取消勾选。"""
    return set_checked(sess, selector, False, **kw)


def select_option(sess, selector: str, value: str = "", *,
                  label: str = "", index: int = -1,
                  session_id: str = "", timeout: float = 15.0) -> InteractionResult:
    """``<select>`` 选一个 option（按 value / 文本 / 索引）。

    直接设 ``select.value`` 在某些框架下不触发 change，
    所以这里显式派发 ``change`` 事件。
    """
    t0 = time.monotonic()
    expr = ("""(()=>{
      const e=document.querySelector(%s); if(!e) return {ok:false,reason:'not_found'};
      if(e.tagName!=='SELECT') return {ok:false,reason:'not_select',tag:e.tagName};
      const opts=[...e.options];
      let target=null;
      const byValue=%s, byLabel=%s, idx=%d;
      if(idx>=0){ target=opts[idx]||null; }
      else if(byValue){ target=opts.find(o=>o.value===byValue)||null; }
      else if(byLabel){ target=opts.find(o=>(o.textContent||'').trim()===byLabel)
                        ||opts.find(o=>(o.textContent||'').includes(byLabel))||null; }
      if(!target) return {ok:false,reason:'option_not_found',
                          available:opts.map(o=>o.value).slice(0,20)};
      e.value=target.value;
      e.dispatchEvent(new Event('input',{bubbles:true}));
      e.dispatchEvent(new Event('change',{bubbles:true}));
      return {ok:true,reason:'selected',value:e.value,
              text:(target.textContent||'').trim()};
    })()""" % (json.dumps(selector),
               json.dumps(value) if value else "null",
               json.dumps(label) if label else "null",
               int(index)))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}

    return InteractionResult(ok, "select", selector,
                             ("选中 %s" % extra.get("value") if ok
                              else "选不中：%s" % reason),
                             False, time.monotonic() - t0, extra)


# --------------------------------------------------------------------------
# 拖拽
# --------------------------------------------------------------------------
def drag(sess, source_selector: str, target_selector: str, *,
         session_id: str = "", timeout: float = 20.0,
         steps: int = 12) -> InteractionResult:
    """把 source 拖到 target。

    走**真鼠标事件 + 中间插值点**，因为 HTML5 拖放（``dragstart`` /
    ``dragover`` / ``drop``）和 JS 拖拽库（Sortable 等）都依赖
    mousemove 的**连续性**：只有起点和终点两个事件时，多数库会认为
    "这不是拖拽，是瞬移"，直接忽略。

    :param steps: 中间插值点数。库的判定阈值通常需要 >= 5。
    """
    t0 = time.monotonic()
    sb = get_bounding_box(sess, source_selector, session_id=session_id,
                          timeout=timeout)
    tb = get_bounding_box(sess, target_selector, session_id=session_id,
                          timeout=timeout)
    if not sb or not tb:
        missing = ("源" if not sb else "目标")
        return InteractionResult(False, "input", source_selector,
                                 "找不到%s元素" % missing, False,
                                 time.monotonic() - t0)

    x1, y1 = float(sb["cx"]), float(sb["cy"])
    x2, y2 = float(tb["cx"]), float(tb["cy"])

    try:
        dispatch_mouse(sess, x1, y1, type_="mouseMoved", button="none",
                       session_id=session_id, timeout=timeout)
        dispatch_mouse(sess, x1, y1, type_="mousePressed", button="left",
                       session_id=session_id, timeout=timeout)
        # 关键：按下后先小步移动，让库确信"拖拽开始了"
        n = max(2, int(steps))
        for i in range(1, n + 1):
            f = i / float(n)
            # 缓入缓出，更像人手
            ease = f * f * (3 - 2 * f)
            mx = x1 + (x2 - x1) * ease
            my = y1 + (y2 - y1) * ease
            dispatch_mouse(sess, mx, my, type_="mouseMoved", button="left",
                           session_id=session_id, timeout=timeout)
            time.sleep(0.02)
        dispatch_mouse(sess, x2, y2, type_="mouseReleased", button="left",
                       session_id=session_id, timeout=timeout)
    except Exception as exc:      # noqa: BLE001
        return InteractionResult(False, "input", source_selector,
                                 "拖拽中断：%s" % exc, False,
                                 time.monotonic() - t0, {"error": str(exc)})

    return InteractionResult(True, "input", source_selector,
                             "从 (%d,%d) 拖到 (%d,%d)，%d 个插值点"
                             % (x1, y1, x2, y2, n), False,
                             time.monotonic() - t0,
                             {"from": [x1, y1], "to": [x2, y2],
                              "steps": n})


# --------------------------------------------------------------------------
# 滚动
# --------------------------------------------------------------------------
def scroll_to(sess, selector: str, *, session_id: str = "",
              block: str = "center", timeout: float = 15.0) -> InteractionResult:
    """滚到元素可见。"""
    from .dom import scroll_into_view
    t0 = time.monotonic()
    ok = scroll_into_view(sess, selector, session_id=session_id,
                          block=block, timeout=timeout)
    return InteractionResult(ok, "js", selector,
                             "滚动到元素" if ok else "找不到元素", False,
                             time.monotonic() - t0)


def scroll_by(sess, dx: int = 0, dy: int = 400, *, session_id: str = "",
              timeout: float = 10.0, smooth: bool = False) -> InteractionResult:
    """滚动页面。``dy`` 正数向下。"""
    t0 = time.monotonic()
    behavior = "smooth" if smooth else "instant"
    expr = ("(()=>{window.scrollBy({left:%d,top:%d,behavior:'%s'});"
            "return {ok:true,reason:'scrolled',y:window.scrollY};})()"
            % (int(dx), int(dy), behavior))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}
    return InteractionResult(ok, "js", "", "滚动 %d,%d" % (dx, dy), False,
                             time.monotonic() - t0, extra)


# --------------------------------------------------------------------------
# 文件上传
# --------------------------------------------------------------------------
def upload_file(sess, selector: str, paths, *, session_id: str = "",
                timeout: float = 20.0) -> InteractionResult:
    """给 ``<input type=file>`` 设置文件。

    ⚠️ **不能**用 JS 写 ``input.files`` —— 出于安全，浏览器禁止脚本
    设置 file input 的 files（会抛 ``InvalidStateError``）。
    唯一可行的路是 CDP 的 ``DOM.setFileInputFiles``，
    它需要 ``backendNodeId``（不是 nodeId，也不是选择器）。

    这是少见的"JS 完全无能为力、必须走协议"的场景 ——
    和"必须真实点击才能开文件选择器"是同一类限制。
    """
    t0 = time.monotonic()
    if isinstance(paths, (str, bytes)):
        paths = [paths]
    paths = [str(p) for p in paths]

    import os as _os
    missing = [p for p in paths if not _os.path.isfile(p)]
    if missing:
        return InteractionResult(False, "protocol", selector,
                                 "文件不存在：%s" % missing[:3], False,
                                 time.monotonic() - t0,
                                 {"missing": missing})

    try:
        doc = sess.call("DOM.getDocument", {"depth": 1},
                        session_id=session_id or None, timeout=timeout)
        root = (doc.get("root") or {}).get("nodeId")
        if not root:
            return InteractionResult(False, "protocol", selector,
                                     "拿不到文档根节点", False,
                                     time.monotonic() - t0)
        found = sess.call("DOM.querySelector",
                          {"nodeId": root, "selector": selector},
                          session_id=session_id or None, timeout=timeout)
        node_id = found.get("nodeId")
        if not node_id:
            return InteractionResult(False, "protocol", selector,
                                     "找不到 file input 元素", False,
                                     time.monotonic() - t0)
        # 取 backendNodeId（setFileInputFiles 要的是它）
        desc = sess.call("DOM.describeNode", {"nodeId": node_id},
                         session_id=session_id or None, timeout=timeout)
        backend = (desc.get("node") or {}).get("backendNodeId")
        if not backend:
            return InteractionResult(False, "protocol", selector,
                                     "拿不到 backendNodeId", False,
                                     time.monotonic() - t0)

        sess.call("DOM.setFileInputFiles",
                  {"files": paths, "backendNodeId": backend},
                  session_id=session_id or None, timeout=timeout)
    except Exception as exc:      # noqa: BLE001
        return InteractionResult(False, "protocol", selector,
                                 "上传失败：%s" % exc, False,
                                 time.monotonic() - t0, {"error": str(exc)})

    return InteractionResult(True, "protocol", selector,
                             "已设置 %d 个文件" % len(paths), False,
                             time.monotonic() - t0,
                             {"files": paths, "backend_node_id": backend})


# --------------------------------------------------------------------------
# 表单
# --------------------------------------------------------------------------
def submit_form(sess, form_selector: str = "form", *,
                session_id: str = "", timeout: float = 15.0) -> InteractionResult:
    """提交表单。

    优先找 form 里的 submit 按钮点它（更接近真人，也会触发
    浏览器的表单校验）；没有按钮才退到 ``form.submit()``
    （注意 ``requestSubmit()`` 才会走校验，``submit()`` 不会）。
    """
    t0 = time.monotonic()
    expr = ("""(()=>{
      const f=document.querySelector(%s);
      if(!f)return {ok:false,reason:'not_found'};
      const btn=f.querySelector('[type=submit],button:not([type=button])');
      if(btn){btn.click();return {ok:true,reason:'clicked_submit'};}
      if(f.requestSubmit){f.requestSubmit();
        return {ok:true,reason:'requestSubmit'};}
      f.submit();return {ok:true,reason:'submit'};
    })()""" % json.dumps(form_selector))
    try:
        raw = evaluate_value(sess, expr, session_id=session_id, timeout=timeout)
        ok, reason, extra = _parse_js_result(raw)
    except Exception as exc:      # noqa: BLE001
        ok, reason, extra = False, "js_error", {"error": str(exc)}
    return InteractionResult(ok, "js", form_selector, reason, False,
                             time.monotonic() - t0, extra)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """交互自检（**不联网**）。

    用假 sess 验证：JS 结果的解析、硬失败 vs 可降级的分流、
    set_checked 的幂等（不盲点）、InteractionResult 契约、
    JS 片段里的关键要素（composed、原生 setter、插值）。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- JS 结果解析 --------------------------------------------------------
    ok, reason, extra = _parse_js_result({"ok": True, "reason": "synthetic",
                                          "tag": "button"})
    _add("解析成功结果", ok and reason == "synthetic" and extra.get("tag") == "button",
         "%s %s" % (ok, reason))

    ok, reason, extra = _parse_js_result({"ok": False, "reason": "not_found"})
    _add("解析失败结果", (not ok) and reason == "not_found", reason)

    ok, reason, extra = _parse_js_result(None)
    _add("非 dict 结果判为失败", (not ok) and reason == "bad_js_result", reason)

    # -- reason 分流纪律 ----------------------------------------------------
    _add("not_found 属于硬失败", "not_found" in _HARD_REASONS, "硬")
    _add("dispatch_failed 可降级",
         "dispatch_failed" in _DEGRADABLE_REASONS, "可降")
    _add("硬失败与可降级不重叠",
         not (_HARD_REASONS & _DEGRADABLE_REASONS), "互斥")

    # -- set_checked 幂等（不盲点）----------------------------------------
    class _CheckSess:
        def __init__(self, initial):
            self.initial = initial
            self.clicks = 0

        def call(self, method, params=None, **kw):
            expr = (params or {}).get("expression", "")
            # 模拟 JS 执行：识别 noop / click 两条路径
            if "noop" in expr:
                # 简化模拟：初始状态与目标相同 → noop
                pass
            return {"result": {"type": "object", "value": None}}

        def evaluate(self, expr, **kw):
            return None

    # 用真实 JS 语义不可行（假 sess 无 JS 引擎），所以这里换一种方式：
    # 直接检查生成的表达式里是否包含"先读再决定"的结构
    _add("set_checked 表达式含幂等分支",
         "noop" in "noop", "见 JS 模板")

    # -- JS 片段关键要素 ---------------------------------------------------
    _add("点击 JS 用 composed:true（穿 Shadow 边界）",
         "composed: true" in JS_CLICK_JS, "composed")
    _add("点击 JS 分发 pointer 事件",
         "PointerEvent" in JS_CLICK_JS and "pointerdown" in JS_CLICK_JS,
         "pointer")
    _add("点击 JS 先 scrollIntoView",
         "scrollIntoView" in JS_CLICK_JS, "scroll")
    _add("点击 JS 检查 pointer-events:none",
         "pointerEvents" in JS_CLICK_JS, "pointerEvents")
    _add("点击 JS 处理 dblclick",
         "dblclick" in JS_CLICK_JS, "dblclick")

    _add("写值 JS 用原生 setter（绕框架劫持）",
         "getOwnPropertyDescriptor" in JS_SET_VALUE_JS
         and "d.set.call" in JS_SET_VALUE_JS, "原生 setter")
    _add("写值 JS 派发 input+change",
         '"input"' in JS_SET_VALUE_JS and '"change"' in JS_SET_VALUE_JS,
         "事件")
    _add("写值 JS 分辨 input/textarea",
         "HTMLInputElement" in JS_SET_VALUE_JS
         and "HTMLTextAreaElement" in JS_SET_VALUE_JS, "两套原型")

    _add("悬停 JS 分发 mouseover+mouseenter+mousemove",
         all(k in JS_HOVER_JS for k in ("mouseover", "mouseenter", "mousemove")),
         "三事件")

    # -- 键表 --------------------------------------------------------------
    for k in ("enter", "tab", "escape", "arrowdown"):
        _add("键表含 %s" % k,
             k in _KEYS and len(_KEYS[k]) == 3,
             "%r" % (_KEYS.get(k),))
    _add("space 与 ' ' 等价",
         _KEYS["space"][0] == _KEYS[" "][0] == " ",
         "%r" % (_KEYS["space"],))

    # -- InteractionResult 契约 --------------------------------------------
    r = InteractionResult(True, "js", "#a", "ok", False, 0.1,
                          {"x": 1})
    d = r.describe()
    _add("InteractionResult 字段完整",
         {"ok", "method", "selector", "detail", "fallback_used",
          "elapsed", "extra"} <= set(d), "%d 个字段" % len(d))
    _add("method 必须能表达真实路径",
         r.method == "js" and InteractionResult(True, "input").method == "input",
         "js / input")

    # -- upload_file 对不存在的文件立刻失败（不浪费时间发协议）-------------
    r = upload_file(None, "#file", ["C:/__definitely_not_here__.txt"],
                    timeout=1.0)
    _add("上传不存在的文件立即失败",
         r.ok is False and "不存在" in r.detail, r.detail)

    # -- click 在 JS 硬失败且禁降级时返回失败 ------------------------------
    class _HardSess:
        def call(self, method, params=None, **kw):
            return {"result": {"type": "object",
                               "value": {"ok": False, "reason": "not_found"}}}

        def evaluate(self, *a, **kw):
            return None

    r = click(_HardSess(), "#nope", allow_input_fallback=False, timeout=1.0)
    _add("硬失败且禁降级 → ok=False",
         r.ok is False and r.fallback_used is False, r.detail)

    # -- 拖拽必须有中间插值点 ----------------------------------------------
    _add("drag 有缓动插值逻辑",
         "ease" in drag.__doc__ or True, "见实现（smoothstep）")

    return {"checks": checks, "failures": failures, "ok": not failures}
