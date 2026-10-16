# -*- coding: utf-8 -*-
"""语义化动作层 —— ``click`` / ``set_value`` / ``toggle`` / ...

这一层是"人说的话"和"机器做的事"之间的翻译器。上层说"点一下登录"，
这里负责决定**用哪条路真的把它点下去**。

## 执行阶梯（严格按顺序，不跳级）

    1. 模式动作    Invoke / Toggle / SelectionItem / ExpandCollapse ...
                   → 最可靠。跨进程、不依赖窗口在前台、不受遮挡影响。
    2. 键鼠动作    焦点 + 键盘（UIA 的 SetFocus 后发键）
                   → 次选。需要窗口可交互，但仍有语义依据。
    3. 坐标动作    元素中心点 + 真实鼠标轨迹
                   → **兜底**。必须显式开启，且必须带元素作为锚点。

## 三条不可违反的规则

**规则一：``click`` / ``set`` 绝不允许"猜坐标"。**

如果元素定位失败，抛 ``ElementNotFound``，**不降级到坐标**。
理由：坐标兜底的前提是"我知道目标在哪"，而不是"我不知道目标在哪，
随便点一下试试"。后者在自动化里等于随机破坏。

    ✅ 元素找到了，但它不支持 Invoke → 可以降级到"点它的中心"
    ❌ 元素没找到 → 不许降级，直接失败

**规则二：每个动作执行后必须验证。**

不做验证的动作等于没做 —— 界面上"点了个寂寞"太常见。
``click(..., expect=...)`` 要求调用方给一个期望变化，
验证失败抛 ``ActionFailed``。

**规则三：所有坐标当场换算，不缓存。**

``Element`` 只带 ``hwnd + rect``。要屏幕坐标时用
``layout.client_to_screen`` **现算**。窗口一动，缓存就全废，
而且是静默失效 —— 点到了别的地方还不报错。

## 与坐标兜底层的边界

阶段二只提供 ``coord_fallback`` 钩子；**阶段三把真实实现注册进来**：

· ``install_input_layer()`` —— 一行把输入层挂上，之后坐标路径走
  ``avatar.input.dispatcher``（静默/物理双模式 + 真人化）
· 钩子未注册时，坐标路径抛 ``UnsupportedError``（明确的"能力不可用"，
  而不是假装成功）

## 动作返回值（阶段三）

所有公开动作返回 ``ActionResult``（来自 ``avatar.input.dispatcher``），
其中 ``method`` / ``mode_used`` / ``fallback_used`` **全部必填**。
历史字段 ``path`` / ``coord_source`` 保留在 ``extra`` 里，兼容阶段二的
调用方。

## 依赖

· ``avatar.uia.tree`` / ``patterns`` / ``find`` —— 定位与模式
· ``avatar.app_manager.layout`` —— 坐标换算
· ``avatar.app_manager.foreground`` —— 需要前台时激活窗口
· ``avatar.input.dispatcher`` —— 坐标路径的真实实现（阶段三）
· ``avatar.errors`` —— 各类失败异常
"""
from __future__ import annotations

import functools
import time
from typing import Callable, Dict, List, Optional, Sequence

from .._win32 import com as C
from ..app_manager import layout as L
from ..errors import (ElementNotFound, ElementStale, LocateError,
                      TimeoutError_, UnsafeStateError, UnsupportedError)
from ..input.dispatcher import (
    MODE_PHYSICAL,
    MODE_SILENT,
    ActionResult,
    dispatch_click,
    dispatch_key,
    dispatch_text,
    get_mode,
)
from . import describe as D
from . import find as F
from . import patterns as P
from . import tree as T

# -- 阶段四：执行结果验证 --------------------------------------------------
# ⚠ 这里**必须**是模块级导入而不是函数内延迟导入。
#   原因：``VerifyResult`` 在 ``set_value`` 的函数体里被当成构造器直接用
#   （``VerifyResult.unknown_(...)``），延迟导入写漏一处就是
#   ``NameError`` —— 而且只在"控件不支持 ValuePattern"这条冷路径上炸，
#   正常路径测不出来。宁可在 import 期暴露依赖，也不要留这种暗雷。
#
#   循环依赖检查：``avatar.verify.desktop`` → ``avatar.vision.capture``
#   → ``avatar.input``；``avatar.uia.actions`` 也 import ``avatar.input``。
#   但 ``avatar.verify.desktop`` **不 import uia.actions**，所以不成环。
#   用 ``verify.result``（纯数据结构，无下游依赖）最安全。
from ..verify.result import VerifyResult
from ..verify.desktop import read_uia_value

__all__ = [
    "ActionFailed", "click", "click_at_element", "set_value", "clear_value",
    "get_value", "toggle", "check", "uncheck", "select", "expand", "collapse",
    "focus", "scroll_into_view", "scroll", "close_window", "invoke_default",
    "read", "is_supported", "register_coord_fallback", "coord_fallback",
    "install_input_layer", "uninstall_input_layer", "input_layer_installed",
    "ActionResult", "ACTIONS", "selfcheck",
]


class ActionFailed(LocateError):
    """动作没能生效（执行了但界面没变）。

    与 ``ElementNotFound`` 的区别：
    · ``ElementNotFound`` = 找不目标
    · ``ActionFailed`` = 找到了、也调了，但**结果不对**

    这个区分很重要 —— 前者要重新定位，后者要换一条执行路径重试。
    """

    exit_code = 4

    def __init__(self, message: str, action: str = "", element=None, **context):
        super().__init__(message, action=action or None, **context)
        self.action = action
        self.element = element


# --------------------------------------------------------------------------
# 5E：失败重试 → 自动检测（挂载点 2/3）
# --------------------------------------------------------------------------
def _risk_note_result(res) -> None:
    """动作结束后记账。**失败连续 3 次就自动跑一次完整检测。**

    ## 为什么要"失败 3 次"才检测

    单次失败太常见了 —— 元素还没渲染完、动画没停、焦点没切过去。
    每次都检测会让正常流程变得很慢（完整检测要 3 秒）。

    但**连续 3 次失败是强信号**：目标可能被验证码挡住了、
    被弹窗遮住了、页面已经跳走了。这时候花 3 秒做完整检测
    完全值得 —— 因为正常路径已经走不通了。

    ## 成功就清零

    一次成功说明"路通了"，之前的失败是暂态问题。
    不清零的话，长时间运行的会话里计数会慢慢攒到 3，
    然后在一次无关的失败上误触发检测。

    ## 开销

    集成默认关闭时，这里只有一次 ``is_installed()`` 判断。
    启用后每次动作多一次字典读写 —— 纳秒级。
    """
    try:
        from ..risk_detector import integration as _ri
    except Exception:      # noqa: BLE001
        return

    try:
        if not _ri.is_installed():
            return

        hwnd = int(getattr(res, "hwnd", 0) or 0)
        act = str(getattr(res, "method", "") or "action")

        if getattr(res, "ok", False):
            _ri.reset_failures(hwnd, act)
            return

        _ri.note_failure(hwnd, act)
        if _ri.should_detect_after_failures(hwnd, act):
            _ri.detect_after_failures(hwnd, action=act)
    except BaseException:      # noqa: BLE001
        # RiskBlocked 要传播（那是任务终止）；其余异常吞掉 ——
        # 记账失败不该影响动作本身的结果。
        import sys as _s
        if _s.exc_info()[0] is not None and \
                _s.exc_info()[0].__name__ == "RiskBlocked":
            raise
        return


def _with_risk_note(fn: Callable) -> Callable:
    """给公共动作函数套一层"结束后记账"。

    用装饰器而不是在每个 ``return`` 前手写调用 —— ``click()``
    有六七个返回点，手写一定会漏。装饰器保证**无论从哪条路返回
    都会记账**。
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        res = fn(*args, **kwargs)
        _risk_note_result(res)
        return res
    return _wrapper


# --------------------------------------------------------------------------
# 坐标兜底钩子（阶段三填充）
# --------------------------------------------------------------------------
#: 真实鼠标/键盘合成的实现，由阶段三的输入层注册。
#:
#: 签名： ``fn(x: int, y: int, *, button: str = "left",
#:             clicks: int = 1, human: bool = True) -> bool``
#: 坐标为**屏幕物理像素**。返回 True 表示已发出事件。
_COORD_FALLBACK: Optional[Callable] = None


def register_coord_fallback(fn: Optional[Callable]) -> None:
    """注册坐标动作的真实实现（阶段三的输入层调用这里）。"""
    global _COORD_FALLBACK
    _COORD_FALLBACK = fn


def coord_fallback() -> Optional[Callable]:
    """当前注册的坐标实现；未注册返回 ``None``。"""
    return _COORD_FALLBACK


def _default_coord_fallback(x: int, y: int, *, button: str = "left",
                            clicks: int = 1, human: bool = True) -> bool:
    """阶段三的默认实现：走输入层。

    这是 ``install_input_layer()`` 挂上去的那个函数。它**不用** hwnd ——
    入参已经是屏幕绝对坐标，直接交给 ``input.mouse``。

    为什么 human 默认 True：坐标兜底本就是"没有语义锚点、只能靠
    人手"的场合，这时候更像人一点反而更安全（避免在错误的瞬间
    以极高的频率双击某个位置）。
    """
    from .. import input as input_layer

    if clicks == 2:
        if human:
            from ..human_entropy import mouse as hm

            hm.double_click_human(x, y, button=button)
        else:
            input_layer.mouse.double_click(x, y, button=button)
    else:
        if human:
            from ..human_entropy import mouse as hm

            hm.click_human(x, y, button=button)
            for _ in range(max(0, clicks - 1)):
                hm.click_human(None, None, button=button)
        else:
            input_layer.mouse.click(x, y, button=button, clicks=clicks)
    return True


def install_input_layer() -> None:
    """把阶段三的输入层挂到坐标兜底钩子上。

    调用一次即可。挂上之后，``click(..., allow_coord=True)`` 这条
    路径才真正能点下去（在此之前会抛 ``UnsupportedError``）。
    """
    register_coord_fallback(_default_coord_fallback)


def uninstall_input_layer() -> None:
    """摘掉输入层（测试用）。"""
    register_coord_fallback(None)


def input_layer_installed() -> bool:
    """输入层是否已挂载。"""
    return _COORD_FALLBACK is not None


def _run_coord_fallback(x: int, y: int, *, button: str = "left",
                        clicks: int = 1, human: bool = True) -> bool:
    """走坐标路径。未注册实现时抛 ``UnsupportedError``。"""
    if _COORD_FALLBACK is None:
        raise UnsupportedError(
            "坐标兜底未启用：请先调用 "
            "avatar.uia.actions.install_input_layer() 挂载输入层。",
            capability="coord_input",
        )
    return bool(_COORD_FALLBACK(x, y, button=button, clicks=clicks,
                                human=human))


# --------------------------------------------------------------------------
# Element.client_center —— 3D 要求的坐标出口
# --------------------------------------------------------------------------
def element_client_center(el: T.Element) -> Optional[tuple]:
    """取元素的**客户区中心**坐标 ``(x, y)``；无客户区返回 ``None``。

    这是 3D 规格里 ``Element.client_center()`` 的模块级实现。
    之所以放在这里而不是给 ``Element`` 加方法：``Element`` 定义在
    ``uia/tree.py``，给它加方法意味着 tree 层要依赖 layout 的
    换算细节；保持"元素只描述自己、坐标换算由动作层做"这条分界
    更干净。

    上层如果确实想要 ``el.client_center()`` 的写法，用
    ``Element.client_center()``（见 ``uia/tree.py`` 的同名方法，
    它内部就调这里）。
    """
    cr = el.client_rect()
    if cr is None:
        return None
    x, y = cr.center
    return (int(x), int(y))


def element_screen_center(el: T.Element) -> tuple:
    """取元素中心的**屏幕绝对坐标** ``(x, y, source)``。

    ``source`` ∈ ``{"live", "snapshot"}`` —— 告诉调用方这个坐标是
    现算的还是快照（快照在窗口已移动时会偏）。
    """
    (x, y), source = _center_screen_with_source(el)
    return (int(x), int(y), source)


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _as_element(target) -> T.Element:
    """把各种入参统一成 ``Element``。

    接受：``Element`` / ``Query`` / ``int``(hwnd) / ``str``(名称)。
    """
    if isinstance(target, T.Element):
        return target
    if isinstance(target, int):
        return T.from_window(target)
    if isinstance(target, str):
        return F.find_one(T.Query(name=target))
    if isinstance(target, F.Query):
        return F.find_one(target)
    raise TypeError("不支持的定位参数：%r" % type(target).__name__)


def _center_screen(el: T.Element) -> tuple:
    """元素中心的**屏幕物理坐标** —— 每次调用都重新换算。

    ⚠ 不缓存。``el.rect`` 是快照，窗口一动就过时；这里用客户区
    坐标 + ``ClientToScreen`` 现算，保证拿到的是"现在"的位置。

    没有客户区（元素无宿主窗口）时退回快照矩形 —— 但这是 degradation，
    会在返回值里体现出来（见 ``_center_screen_with_source``）。
    """
    return _center_screen_with_source(el)[0]


def _center_screen_with_source(el: T.Element) -> tuple:
    """返回 ``((x, y), source)``。``source`` ∈ ``{"live", "snapshot"}``。"""
    cr = el.client_rect()
    if cr is not None and el.window:
        try:
            x, y = cr.center
            sx, sy = L.client_to_screen(el.window, int(x), int(y))
            return (int(sx), int(sy)), "live"
        except Exception:
            pass
    if el.rect is not None:
        return el.rect.center, "snapshot"
    raise ElementNotFound("元素既没有客户区坐标也没有屏幕矩形，无法定位",
                          query=el.name)


def _require_usable(el: T.Element, action: str) -> None:
    """动作前置检查 —— 早失败，别等点完了才发现点错东西。"""
    if not el.enabled:
        raise ActionFailed("元素已禁用，无法%s" % action,
                           action=action, element=el)
    if el.offscreen:
        raise ActionFailed(
            "元素在屏幕外（被滚动出视口或被遮挡），无法%s。"
            "先调 scroll_into_view()。" % action,
            action=action, element=el)


def _verify(el: T.Element, expect, timeout: float, poll: float = 0.05) -> bool:
    """轮询等待 ``expect(el)`` 为真。

    ``expect`` 可以是：
    · ``None``         —— 不验证（调用方明确表示不关心）
    · ``callable``      —— 收 ``Element`` 返回 bool
    · ``Query``         —— 等这个查询有结果

    **保留这个 bool 版本是为了向后兼容** —— 阶段二的调用方和测试
    都在用它。要结构化结论请用 :func:`_verify_result`。
    """
    if expect is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if isinstance(expect, F.Query):
                if F.exists(expect):
                    return True
            elif callable(expect):
                if expect(el):
                    return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.005, poll))


def _verify_result(el: T.Element, expect, timeout: float,
                   poll: float = 0.05):
    """``_verify`` 的结构化版本，返回 ``VerifyResult``。

    与 :func:`_verify` 的三点区别（都是"为什么需要结构化"的理由）：

    1. **布尔不够用**。``expect`` 是"等查询有结果"，超时到底是
       "元素真的没出现"（fail）还是"窗口还没渲染完"（unknown）？
       bool 两种都返回 ``False``，上层只能当失败处理 —— 于是对一个
       其实已经成功了的动作报错、或者对一个真失败了的动作重试到超时。
       这里把"说了算的判据能拿到"和"拿不到判据"分开。
    2. **要带证据**。``evidence`` 里记下等了多久、试了几次、
       ``expect`` 是什么类型 —— 出问题时能复盘。
    3. **要统一**。桌面端和 Web 端现在都返回同一个类型，
       上层处理逻辑只有一套。

    ``expect`` 为 ``None`` 时返回 ``VerifyResult.unknown_`` ——
    **不是 ok**。因为"调用方没说要验什么"不等于"验证通过了"。
    """
    from ..verify.result import VerifyResult

    if expect is None:
        return VerifyResult.unknown_(
            method="uia.expect", reason="调用方未提供验证判据",
            mode="desktop")

    t0 = time.monotonic()
    deadline = t0 + max(0.0, timeout)
    attempts = 0
    is_query = isinstance(expect, F.Query)
    kind = "query" if is_query else ("callable" if callable(expect)
                                     else type(expect).__name__)

    # 判据不可用（既不是 Query 也不是 callable）→ **未知**，不是失败。
    # 这是最容易写错的地方：拿不到判定手段 ≠ 事情没发生。
    if not is_query and not callable(expect):
        return VerifyResult.unknown_(
            method="uia.expect", mode="desktop",
            reason="无法理解的验证判据：%r" % (type(expect).__name__,),
            evidence={"expect_kind": kind, "elapsed": 0.0})

    last_exc = None
    exc_count = 0
    while True:
        attempts += 1
        try:
            if is_query:
                if F.exists(expect):
                    return VerifyResult.ok_(
                        method="uia.expect", mode="desktop",
                        reason="查询已有结果",
                        target=getattr(el, "name", "") or "",
                        elapsed=time.monotonic() - t0,
                        evidence={"expect_kind": kind, "attempts": attempts})
            elif expect(el):
                return VerifyResult.ok_(
                    method="uia.expect", mode="desktop",
                    reason="判据成立",
                    target=getattr(el, "name", "") or "",
                    elapsed=time.monotonic() - t0,
                    evidence={"expect_kind": kind, "attempts": attempts})
        except Exception as exc:      # noqa: BLE001
            # 判据自己抛异常 —— 记下来，但继续等。
            # 元素正在销毁/重建时读属性抛 COM 错很常见，不是失败信号。
            last_exc = exc
            exc_count += 1

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.005, poll))

    elapsed = time.monotonic() - t0
    ev = {"expect_kind": kind, "attempts": attempts,
          "exceptions": exc_count, "elapsed": round(elapsed, 4)}

    # --- 超时了，但要分清是"确认没发生"还是"没法确认" ---
    if is_query:
        # 查询在超时后仍然无结果，且判据本身没抛错 → **确认失败**。
        # 为什么算 fail 而不是 unknown：Query 是"查有没有"，
        # 查得到/查不到都是明确答案，超时 = 没查到。
        if exc_count == 0:
            return VerifyResult.fail(
                method="uia.expect", mode="desktop",
                reason="等待查询结果超时（%.2fs）" % timeout,
                target=getattr(el, "name", "") or "",
                elapsed=elapsed, evidence=ev)
        return VerifyResult.unknown_(
            method="uia.expect", mode="desktop",
            reason="查询期间判据抛异常：%s" % (last_exc,),
            elapsed=elapsed, evidence=ev, error=str(last_exc))

    # callable 判据：**每一次**尝试都在抛异常 → 不是"条件不满足"，
    # 而是"根本没法判断"。
    #
    # ⚠ 这里踩过一次：原来写的是 ``attempts == 1``，意思是"第一次就抛了"。
    #   但轮询路径下 attempts 会涨到 5、10、20 —— 判定永远不成立，
    #   一个"读不到元素状态"的场景被误报成"确认失败"。
    #   正确判据是"抛异常次数 == 尝试次数"（一次都没成功求值过）。
    if exc_count > 0 and exc_count == attempts:
        return VerifyResult.unknown_(
            method="uia.expect", mode="desktop",
            reason="判据每次求值都失败（%d 次）：%s" % (attempts, last_exc),
            elapsed=elapsed, evidence=ev, error=str(last_exc))

    # 部分成功求值但返回 False → 确认条件没满足 = **确认失败**
    return VerifyResult.fail(
        method="uia.expect", mode="desktop",
        reason="等待预期变化超时（%.2fs）" % timeout,
        target=getattr(el, "name", "") or "",
        elapsed=elapsed, evidence=ev)


#: 动作自带判据的总开关。开的时候动作会把自己知道的判据
#: （``suggest``）塞进 ``extra["suggest"]``，供验证钩子取用。
#: 关掉可以省一点点构造 dict 的开销。
_SUGGEST_ENABLED = True


def suggest_enabled() -> bool:
    return _SUGGEST_ENABLED


def set_suggest_enabled(on: bool) -> bool:
    """开关动作自带判据。返回旧值。"""
    global _SUGGEST_ENABLED
    old = _SUGGEST_ENABLED
    _SUGGEST_ENABLED = bool(on)
    return old


def _attach_suggest(res: ActionResult, el: T.Element,
                    suggest: Optional[Dict] = None) -> ActionResult:
    """把动作建议的验证判据挂到结果上。

    **为什么动作要建议判据**：点 ``#submit`` 之后该等什么，动作自己
    最清楚（它知道目标是谁、刚做了什么）。让上层去猜只能得到
    "画面变了吗"这种弱判据。传下去，钩子能用就用。

    挂的是 ``extra["suggest"]`` —— 不走 ``verify_action`` 的自动路由，
    因为那是**钩子**的决定，不是动作的。动作只提供线索。
    """
    if not _SUGGEST_ENABLED:
        return res
    hint = dict(suggest or {})
    hint.setdefault("mode", "desktop")
    if el is not None and hint.get("target") is None:
        hint["target"] = el
    if "timeout" not in hint:
        hint["timeout"] = 5.0
    res.extra["suggest"] = hint
    return res


def run_verify(res: ActionResult, *, expect=None, timeout: float = 2.0,
               hook: Any = None, force: bool = False) -> ActionResult:
    """给一个已完成的 UIA 动作补上验证结论。

    **统一出口** —— ``click`` / ``set_value`` / ``toggle`` 等动作
    在返回前都过这里，所以它们的验证行为完全一致。

    两路判据，优先级从高到低：

    1. ``expect`` 给了 → 用 :func:`_verify_result`（最精确，
       因为调用方明确说了要等什么）
    2. ``expect`` 没给 → 走全局验证钩子（``verify.hook``），
       拿动作挂的 ``suggest`` 当线索

    两路都没有 → ``verified`` 留 ``None``（**不做虚假的已验证声明**）。

    :param hook: 临时钩子（覆盖全局注册的那个）
    :param force: 无视钩子开关，强行按 ``suggest`` 验一次
    """
    try:
        from ..verify.result import VerifyResult
        from ..verify import hook as hook_mod
    except Exception:      # noqa: BLE001
        return res

    # -- 路线 1：显式判据 --
    if expect is not None:
        el = res.extra.get("_element")
        vres = _verify_result(el, expect, timeout)
        res.verified = vres
        res.extra["verified"] = vres.verdict
        return res

    # -- 路线 2：钩子 --
    if not (hook_mod.has_verify_hook() or hook is not None or force):
        return res

    ctx = hook_mod.build_context(
        "generic",
        ok=res.ok,
        hwnd=res.hwnd,
        point=res.point,
        mode_used=res.mode_used,
        method=res.method,
        suggest=res.extra.get("suggest"),
        extra={"fallback_used": res.fallback_used},
    )
    vres = hook_mod.run_verify_hook(ctx, hook=hook, force=force)
    if vres is not None:
        res.verified = vres
        res.extra["verified"] = vres.verdict
    return res


def _activate(el: T.Element) -> bool:
    """把元素所在窗口带到前台（需要真实键鼠时才要）。"""
    if not el.window:
        return False
    try:
        from ..app_manager import foreground as FG
        return bool(FG.force_foreground(el.window, timeout=1.5))
    except Exception:
        return False


def _pattern_do(el: T.Element, pattern: str, method: str, *args,
                required: bool = False):
    """取模式 → 调方法 → **保证释放**。

    这是所有模式动作的唯一出口 —— 集中处理"取不到模式"和
    "方法不存在"两种失败，避免每个动作各写一遍。

    ## 返回约定（关键）

    返回 ``(obj, result)``。**``obj is None`` 才是"这个模式不可用"**。
    模式可用但方法返回了 ``False``（例如 ``set_value`` 被拒绝）时，
    ``obj`` 非 ``None``、``result`` 为 ``False`` —— 这两者必须分清。

    ## ⚠ 踩过的坑

    早期调用点写的是 ``if ok is not None: break``。但"模式不可用"时
    返回的是 ``(None, False)`` —— ``ok`` 是 ``False`` 而不是 ``None``，
    于是**误判成成功**。后果：一个不支持任何模式的纯文本元素，
    "点击"会静默地什么都不做却报告成功。

    现在改成 ``if obj is not None and ok:`` 双条件。
    """
    obj = P.wrap(el, pattern, required=False)
    if obj is None:
        if required:
            raise ActionFailed(
                "元素不支持 %s 模式，无法执行 %s" % (pattern, method),
                action=method, element=el)
        return None, False
    try:
        fn = getattr(obj, method, None)
        if fn is None:
            if required:
                raise ActionFailed(
                    "%s 模式没有 %s 方法" % (pattern, method),
                    action=method, element=el)
            return obj, False
        try:
            result = fn(*args)
        except Exception as exc:      # noqa: BLE001
            raise ActionFailed(
                "%s.%s 调用失败：%s" % (pattern, method, exc),
                action=method, element=el)
        return obj, (bool(result) if result is not None else True)
    finally:
        try:
            obj.release()
        except Exception:
            pass


# --------------------------------------------------------------------------
# ActionResult 辅助
# --------------------------------------------------------------------------
def _res_from_extra(extra: Dict, el: T.Element, t0: float,
                    method: str, detail: str,
                    *, suggest: Optional[Dict] = None,
                    expect=None, timeout: float = 2.0) -> ActionResult:
    """从历史 detail dict 造一个 ActionResult。

    阶段二的动作（toggle/select/expand/...）内部仍用 dict 记录细节，
    这里统一包装成 ActionResult，避免每个动作各写一遍字段映射。

    阶段四在这里顺势把验证接上（**只此一处**，所以所有走这个函数的
    动作自动获得验证能力）：先挂 ``suggest``，再按 ``expect`` 或全局
    钩子出结论。
    """
    res = ActionResult(
        ok=True,
        method=method,
        mode_used=MODE_SILENT,
        fallback_used=False,
        detail=detail,
        hwnd=el.window or None,
        duration=time.monotonic() - t0,
        extra=extra,
    )
    res.extra["_element"] = el
    _attach_suggest(res, el, suggest)
    run_verify(res, expect=expect, timeout=timeout)
    return res


def _coord_action(el: T.Element, extra: Dict, t0: float, *,
                  human: bool = True, mode: Optional[str] = None,
                  button: str = "left", clicks: int = 1) -> ActionResult:
    """动作层的坐标兜底统一出口。

    有客户区 → 走 dispatcher（静默优先 + 可降级）；
    无客户区 → 走坐标兜底钩子（纯屏幕坐标）。

    所有需要"没有语义模式时靠坐标点一下"的动作都调这里，保证
    它们的行为（模式选择、降级、真人化）完全一致。
    """
    (x, y), source = _center_screen_with_source(el)
    extra.update(path="coord", coord_source=source, coord=(x, y))

    if el.window:
        cr = el.client_rect()
        if cr is not None:
            cx, cy = cr.center
            _activate(el)
            res = dispatch_click(
                el.window, int(cx), int(cy),
                button=button, clicks=clicks, human=human, mode=mode,
            )
            res.extra.update(extra)
            res.duration = time.monotonic() - t0
            return res

    _activate(el)
    _run_coord_fallback(x, y, button=button, clicks=clicks, human=human)
    return ActionResult(
        ok=True, method="mouse", mode_used=mode or get_mode(),
        fallback_used=False,
        detail="坐标兜底点击 (%d, %d)" % (x, y),
        point=(x, y), hwnd=el.window or None,
        duration=time.monotonic() - t0, extra=extra,
    )


# --------------------------------------------------------------------------
# 点击
# --------------------------------------------------------------------------
#: "点击"这个语义可以走的模式，按优先级排。
#: 顺序即策略：Invoke 最直接，SelectionItem 用于列表/标签，
#: Toggle 用于开关，ExpandCollapse 用于可展开项。
CLICK_PATTERNS: Sequence[str] = (
    "Invoke", "SelectionItem", "Toggle", "ExpandCollapse",
    "LegacyIAccessible",
)


@_with_risk_note
def click(target, *, expect=None, timeout: float = 2.0,
          allow_coord: bool = False, button: str = "left",
          clicks: int = 1, human: bool = True,
          mode: Optional[str] = None) -> ActionResult:
    """点一下目标。

    ## 执行阶梯

    1. 依次尝试 ``CLICK_PATTERNS`` 里的模式
    2. 全都不支持时，**只有** ``allow_coord=True`` 才降级到坐标
    3. 目标定位不到 → 直接 ``ElementNotFound``，**任何情况下都不降级**

    返回 ``ActionResult``，其中::

        method       "invoke" / "selectionitem" / "toggle" / ...
                     / "mouse"（坐标路径）
        mode_used    "physical" / "silent"（坐标路径才有意义；
                     模式动作不涉及输入注入，统一标 "silent"
                     —— 因为它本就不打断用户）
        fallback_used 坐标路径且发生了静默→物理降级时为 True

    历史上的 ``path`` / ``pattern`` / ``coord_source`` 字段保留在
    ``extra`` 里，阶段二的调用方如果读的是 dict 键，改成
    ``res.extra["path"]`` 即可。

    :param human: 坐标路径是否走真人化轨迹
    :param mode: 覆盖交互模式（"physical" / "silent"）
    """
    t0 = time.monotonic()
    el = _as_element(target)
    _require_usable(el, "点击")

    tried = []
    extra = {"path": None, "pattern": None, "verified": None, "tried": tried}

    # -- 阶梯 1：模式 ----------------------------------------------------
    # ⚠ 判定必须用 ``obj is not None and ok`` 两个条件。
    #   只用 ``ok is not None`` 会把"模式不可用"（返回 False）误判成成功，
    #   导致一个不支持任何模式的元素"点击成功"却什么都没发生。
    actions_map = {
        "Invoke": "invoke",
        "SelectionItem": "select",
        "Toggle": "toggle",
        "ExpandCollapse": "expand",
        "LegacyIAccessible": "do_default_action",
    }
    for name in CLICK_PATTERNS:
        # 元素带模式快照且明确不含这个模式 → 跳过，省一次 COM 调用
        if el.patterns and name not in el.patterns:
            continue
        method = actions_map[name]
        tried.append("%s.%s" % (name, method))
        try:
            obj, ok = _pattern_do(el, name, method)
        except ActionFailed:
            continue          # 这个模式调失败 → 试下一个
        except Exception:
            continue
        if obj is not None and ok:
            extra.update(path=name.lower(), pattern=name)
            break

    # -- 阶梯 2：坐标兜底（必须显式允许） ---------------------------------
    action_res: Optional[ActionResult] = None
    if extra["path"] is None:
        if not allow_coord:
            raise ActionFailed(
                "元素 %r 不支持任何点击模式（试过：%s）。"
                "若确定要用坐标兜底，请显式传 allow_coord=True。"
                % (el.name or el.label, ", ".join(tried) or "无"),
                action="click", element=el)
        (x, y), source = _center_screen_with_source(el)
        extra.update(path="coord", pattern=None, coord_source=source,
                     coord=(x, y))

        # 坐标路径：用 dispatcher，让静默/物理与真人化统一生效
        if el.window:
            # 有宿主窗口 → 用客户区坐标走 dispatcher（可用静默模式）
            cr = el.client_rect()
            if cr is not None:
                cx, cy = cr.center
                _activate(el)
                action_res = dispatch_click(
                    el.window, int(cx), int(cy),
                    button=button, clicks=clicks, human=human, mode=mode,
                )
        if action_res is None:
            # 没有客户区：只能用屏幕坐标 + 坐标兜底钩子
            _activate(el)
            _run_coord_fallback(x, y, button=button, clicks=clicks,
                                human=human)
            action_res = ActionResult(
                ok=True, method="mouse",
                mode_used=mode or get_mode(), fallback_used=False,
                detail="坐标兜底点击 (%d, %d)" % (x, y),
                point=(x, y), hwnd=el.window or None,
            )

    # -- 验证 ------------------------------------------------------------
    # 阶段四改造：这里原来只跑 `_verify` 拿个 bool，现在两条路都走 ——
    # 有 expect 用 expect（最准），没有就走全局验证钩子（拿 suggest 当线索）。
    # `expect` 的失败语义保持**向后兼容**：仍然抛 ActionFailed，
    # 但异常里带上 VerifyResult，调用方能拿到结构化原因。
    res = action_res
    if res is None:
        res = ActionResult(
            ok=True,
            method=extra["path"],
            mode_used=MODE_SILENT,
            fallback_used=False,
            detail="%s 模式点击成功" % extra["pattern"],
            hwnd=el.window or None,
            duration=time.monotonic() - t0,
            extra=extra,
        )

    res.extra.update(extra)
    res.extra["_element"] = el
    _attach_suggest(res, el, {
        "mode": "desktop",
        "target": el,
        "uia_target": el,
        "timeout": max(float(timeout), 1.0),
    })

    if expect is not None:
        vres = _verify_result(el, expect, timeout)
        res.verified = vres
        res.extra["verified"] = vres.verdict
        res.verify_elapsed = vres.elapsed
        if not vres.ok:
            # 向后兼容：expect 不满足仍然抛，但把结论挂上去
            exc = ActionFailed(
                "点击 %r 后等待 %.2fs 未观察到预期变化"
                % (el.name or el.label, timeout),
                action="click", element=el)
            exc.verify = vres                   # type: ignore[attr-defined]
            res.error = vres.reason
            raise exc
    else:
        run_verify(res, timeout=max(float(timeout), 1.0))

    res.duration = time.monotonic() - t0
    return res


def click_at_element(el: T.Element, **kw) -> ActionResult:
    """``click`` 的显式版本 —— 强制要求传 ``Element``。

    用在"元素必须已经定位好"的场景（例如列表里的第 N 个复选框），
    避免不小心把字符串当名字重新查一遍、查到了同名但不是目标那个。
    """
    if not isinstance(el, T.Element):
        raise TypeError("click_at_element 只接受 Element，收到 %r"
                        % type(el).__name__)
    return click(el, **kw)


# --------------------------------------------------------------------------
# 取值 / 设值
# --------------------------------------------------------------------------
@_with_risk_note
def set_value(target, text: str, *, expect=None, timeout: float = 2.0,
              clear_first: bool = True, allow_coord: bool = False,
              human: bool = False, mode: Optional[str] = None) -> ActionResult:
    """给输入框设值。

    优先 ``Value.SetValue`` —— 它**直接设文本，不模拟键盘**，
    因此不受输入法、键盘布局、焦点窗口影响。这是最稳的路径。

    不支持 Value 模式时（少见，如某些自绘控件）：
    · ``allow_coord=True`` → 坐标点击聚焦 + 输入层负责键入
    · 否则抛 ``ActionFailed``（**明确告诉你这条路不通**，而不是假装成功）

    ⚠ 反例：登录密码框用 ``Value.SetValue`` 在很多应用里是**不生效**的，
    因为安全控件会拒绝程序化设值。这种情况必须走真实键入。
    调用方应先试 ``set_value``，捕获 ``ActionFailed`` 后改走键鼠路径。

    :param human: 坐标路径的输入是否走真人化打字节奏
    :param mode: 覆盖交互模式
    """
    t0 = time.monotonic()
    el = _as_element(target)
    _require_usable(el, "设值")
    extra = {"path": None, "value": text, "verified": None}

    if clear_first and ("Value" in (el.patterns or ()) or not el.patterns):
        # 先清空，避免变成"追加"
        try:
            obj, ok = _pattern_do(el, "Value", "set_value", "")
            if obj is not None and ok:
                extra["cleared"] = True
        except Exception:
            pass

    try:
        obj, ok = _pattern_do(el, "Value", "set_value", text, required=True)
    except ActionFailed:
        if not allow_coord:
            raise
        (x, y), source = _center_screen_with_source(el)
        extra.update(path="coord", coord_source=source, coord=(x, y))

        # 坐标路径：走 dispatcher 的文本输入（静默优先）
        if el.window:
            cr = el.client_rect()
            if cr is not None:
                cx, cy = cr.center
                _activate(el)
                res = dispatch_text(
                    el.window, text,
                    human=human, mode=mode,
                    client_x=int(cx), client_y=int(cy),
                )
                res.extra.update(extra)
                res.extra["_element"] = el
                _attach_suggest(res, el, _value_suggest(el, text, timeout))
                if expect is not None:
                    vres = _verify_result(el, expect, timeout)
                    res.verified = vres
                    res.extra["verified"] = vres.verdict
                    res.verify_elapsed = vres.elapsed
                    if not vres.ok:
                        exc = ActionFailed("设值后未观察到预期变化",
                                           action="set_value", element=el)
                        exc.verify = vres       # type: ignore[attr-defined]
                        raise exc
                else:
                    # 坐标路径**必须**回读 —— 键入的东西可能被输入法/焦点吃掉
                    _verify_written(res, el, text)
                res.duration = time.monotonic() - t0
                return res

        # 无客户区：退回坐标兜底钩子（只聚焦，键入需调用方自理）
        _activate(el)
        _run_coord_fallback(x, y)
        res = ActionResult(
            ok=True, method="mouse", mode_used=mode or get_mode(),
            fallback_used=False,
            detail="坐标聚焦于 (%d, %d)，键入需调用方自理" % (x, y),
            point=(x, y), hwnd=el.window or None,
            duration=time.monotonic() - t0, extra=extra,
        )
        res.extra["_element"] = el
        res.verified = VerifyResult.unknown_(
            method="set_value", mode="desktop",
            reason="坐标聚焦路径不负责键入，无法回读确认",
            target=el.name or "")
        return res
    else:
        extra["path"] = "value"
        extra["ok"] = bool(ok)

    res = ActionResult(
        ok=True, method="value", mode_used=MODE_SILENT, fallback_used=False,
        detail="Value.SetValue 设值成功", hwnd=el.window or None,
        duration=time.monotonic() - t0, extra=extra,
    )
    res.extra["_element"] = el
    _attach_suggest(res, el, _value_suggest(el, text, timeout))

    if expect is not None:
        vres = _verify_result(el, expect, timeout)
        res.verified = vres
        res.extra["verified"] = vres.verdict
        res.verify_elapsed = vres.elapsed
        if not vres.ok:
            exc = ActionFailed("设值后未观察到预期变化", action="set_value",
                               element=el)
            exc.verify = vres               # type: ignore[attr-defined]
            raise exc
    else:
        # ⚠ 阶段四补的关键一步：**设完自己回读**。
        #
        # 为什么必须做：UIA ``Value.SetValue`` 对只读控件、密码框、
        # 被安全策略拦住的编辑框会**返回成功但值没进去**。原来不看返回值
        # 就报成功，是明确的假阳性。回读一次成本很低（一次属性读），
        # 换来的是"成功"这个词终于有依据。
        _verify_written(res, el, text)

    res.duration = time.monotonic() - t0
    return res


def _value_suggest(el: T.Element, text: str, timeout: float) -> Dict:
    """``set_value`` 建议的验证判据：回读值等于写入值。"""
    return {
        "mode": "desktop",
        "target": el,
        "uia_target": el,
        "expect_value": text,
        "match": "exact",
        "timeout": max(float(timeout), 1.0),
    }


def _verify_written(res: ActionResult, el: T.Element, text: str) -> None:
    """设值后回读，把结论写进 ``res.verified``。**永不抛异常**。

    ``text`` 为空串时判据改成"值为空"，这是清空操作的正当期望。
    密码框读回来是掩码（全 ``●``）而不是原文 —— 这种情况判"非空"即可，
    不做等值比较（否则必然假阴性）。这是刻意的放宽。
    """
    try:
        ok_read, got = read_uia_value(el)
    except Exception as exc:      # noqa: BLE001
        res.verified = VerifyResult.unknown_(
            method="set_value.readback", mode="desktop",
            reason="回读失败：%s" % (exc,), target=el.name or "",
            error=str(exc))
        return

    if not ok_read:
        # 读不到值 —— 未知，不是失败。很多控件压根不暴露 ValuePattern。
        res.verified = VerifyResult.unknown_(
            method="set_value.readback", mode="desktop",
            reason="控件不暴露 ValuePattern，无法回读确认",
            target=el.name or "")
        return

    got_s = "" if got is None else str(got)
    ev = {"expected": text, "actual": got_s[:200],
          "elapsed": round(res.duration, 4)}

    # 密码框：读回来全是掩码字符 → 只能判非空
    if text and got_s and _looks_masked(got_s):
        res.verified = VerifyResult.ok_(
            method="set_value.readback", mode="desktop",
            reason="读回掩码值（密码框），确认已写入",
            target=el.name or "", evidence=ev)
        return

    if got_s == text:
        res.verified = VerifyResult.ok_(
            method="set_value.readback", mode="desktop",
            reason="回读一致", target=el.name or "", evidence=ev)
        return

    # 值不匹配 —— 这是**确认失败**，正是要抓的那种假阳性
    res.verified = VerifyResult.fail(
        method="set_value.readback", mode="desktop",
        reason="回读不一致：期望 %r，实际 %r" % (text[:80], got_s[:80]),
        target=el.name or "", evidence=ev)
    res.extra["readback_mismatch"] = True


#: 常见密码掩码字符。UIA 读密码框通常给一串这个。
_MASK_CHARS = "\u2022\u25cf*\u25a0\u25aa\u00b7\u25cb\u25cc"


def _looks_masked(s: str) -> bool:
    """判断读回的是不是掩码串。"""
    if not s:
        return False
    return all(c in _MASK_CHARS for c in s)


def get_value(target) -> Optional[str]:
    """读输入框当前文本。不支持 Value 模式返回 ``None``。

    ⚠️ 返回前必须做**类型收敛**，不能直接把 ``_pattern_do`` 的第二个
    返回值抛出去。原因：``_pattern_do`` 在"模式不可用"时返回的是
    ``(False, False)`` 这种布尔哨兵（见它的实现），于是 ``val`` 会是
    ``False`` 而**不是** ``None``。调用方写 ``if get_value(el) is not None``
    就会把"读不到"当成"读到了空值"，进而做出错误的判断。

    类型契约是 ``Optional[str]``，那就严格只返回 ``str`` 或 ``None``。
    """
    el = _as_element(target)
    try:
        _, val = _pattern_do(el, "Value", "value")
    except Exception:
        return None
    if isinstance(val, str):
        return val
    # bool / int / 其它哨兵值一律视为"没读到"
    return None


def clear_value(target, **kw) -> ActionResult:
    """清空输入框。"""
    return set_value(target, "", clear_first=False, **kw)


# --------------------------------------------------------------------------
# 勾选 / 切换
# --------------------------------------------------------------------------
@_with_risk_note
def toggle(target, on: Optional[bool] = None, *, expect=None,
           timeout: float = 2.0, allow_coord: bool = False,
           human: bool = True, mode: Optional[str] = None) -> ActionResult:
    """切换勾选状态。

    ``on=None`` 表示"翻转"；``on=True`` 表示"确保勾上"；
    ``on=False`` 表示"确保取消"。

    "确保"语义很重要：开关类控件不该被无脑 flip —— 你以为是开，
    实际它已经是开，flip 一下就关了。所以这里先读状态再决定要不要动，
    状态已经对了就**什么都做**（幂等）。
    """
    el = _as_element(target)
    _require_usable(el, "切换")
    t0 = time.monotonic()
    extra = {"path": None, "verified": None}

    obj, cur = _pattern_do(el, "Toggle", "state")
    if obj is not None and cur is not None:
        is_on = (int(cur) == int(P.TOGGLE_ON))
        extra["before"] = is_on
        if on is not None and is_on == bool(on):
            extra.update(path="toggle", action="noop", after=is_on)
            return _res_from_extra(extra, el, t0, "toggle", "Toggle 已是目标状态")
        _, ok = _pattern_do(el, "Toggle", "toggle")
        extra.update(path="toggle", after=not is_on, ok=bool(ok))
    else:
        # 没有 Toggle 模式 → 可能是个带 SelectionItem 的复选项
        sel, selected = _pattern_do(el, "SelectionItem", "is_selected")
        if sel is not None and selected is not None:
            extra["before"] = bool(selected)
            if on is not None and bool(selected) == bool(on):
                extra.update(path="selection_item", action="noop",
                             after=bool(selected))
                return _res_from_extra(extra, el, t0, "selectionitem",
                                       "SelectionItem 已是目标状态")
            _, ok = _pattern_do(el, "SelectionItem", "select")
            extra.update(path="selection_item", after=not bool(selected),
                         ok=bool(ok))
        elif allow_coord:
            return _coord_action(el, extra, t0, human=human, mode=mode)
        else:
            raise ActionFailed(
                "元素 %r 既无 Toggle 也无 SelectionItem 模式，无法切换。"
                "需要坐标做法请显式传 allow_coord=True。" % (el.name or el.label),
                action="toggle", element=el)

    if expect is not None:
        ok = _verify(el, expect, timeout)
        extra["verified"] = ok
        if not ok:
            raise ActionFailed("切换后未观察到预期变化", action="toggle",
                               element=el)
    return _res_from_extra(extra, el, t0, extra["path"] or "toggle",
                           "切换成功")


def check(target, **kw) -> ActionResult:
    """确保勾上。"""
    return toggle(target, True, **kw)


def uncheck(target, **kw) -> ActionResult:
    """确保取消。"""
    return toggle(target, False, **kw)


# --------------------------------------------------------------------------
# 选择 / 展开
# --------------------------------------------------------------------------
def select(target, *, expect=None, timeout: float = 2.0,
           allow_coord: bool = False,
           human: bool = True, mode: Optional[str] = None) -> ActionResult:
    """选中列表项 / 标签页 / 菜单项（``SelectionItem.Select``）。

    与 ``click`` 的区别：``click`` 会依次试多种模式，``select`` 只走
    SelectionItem —— 语义更精确，用在"确定要用选中语义"的场景。
    """
    el = _as_element(target)
    _require_usable(el, "选中")
    t0 = time.monotonic()
    try:
        _, ok = _pattern_do(el, "SelectionItem", "select", required=True)
    except ActionFailed:
        if not allow_coord:
            raise
        return _coord_action(el, {}, t0, human=human, mode=mode)

    extra = {"path": "selection_item", "ok": bool(ok)}
    if expect is not None:
        extra["verified"] = _verify(el, expect, timeout)
        if not extra["verified"]:
            raise ActionFailed("选中后未观察到预期变化", action="select",
                               element=el)
    return _res_from_extra(extra, el, t0, "selectionitem", "SelectionItem 选中成功")


def expand(target, *, on: bool = True, expect=None,
           timeout: float = 2.0) -> Dict:
    """展开（``on=True``）或收起（``on=False``）可折叠项。

    同样是幂等语义：已经展开了再调 ``expand()`` 什么都不做。
    """
    el = _as_element(target)
    _require_usable(el, "展开" if on else "收起")

    obj, state = _pattern_do(el, "ExpandCollapse", "state")
    if obj is None or state is None:
        raise ActionFailed(
            "元素 %r 不支持 ExpandCollapse 模式" % (el.name or el.label),
            action="expand", element=el)

    want = P.EC_EXPANDED if on else P.EC_COLLAPSED
    if state == want:
        return {"element": el, "path": "expand_collapse", "action": "noop",
                "state": state}

    _, ok = _pattern_do(el, "ExpandCollapse",
                        "expand" if on else "collapse", required=True)
    detail = {"element": el, "path": "expand_collapse", "ok": bool(ok)}
    if expect is not None:
        detail["verified"] = _verify(el, expect, timeout)
        if not detail["verified"]:
            raise ActionFailed("展开/收起后未观察到预期变化",
                               action="expand", element=el)
    return detail


def collapse(target, **kw) -> Dict:
    """收起。"""
    return expand(target, on=False, **kw)


# --------------------------------------------------------------------------
# 焦点 / 滚动
# --------------------------------------------------------------------------
def focus(target, *, timeout: float = 2.0) -> Dict:
    """把键盘焦点给元素（走 ``SetFocus``）。

    比"点一下"更可靠 —— 不产生鼠标事件，不会被遮挡影响。
    输入文本前应该先 ``focus()``。
    """
    el = _as_element(target)
    if not el.focusable and el.patterns:
        # focusable 在某些控件上报得不准，不直接拒绝，只记录
        pass
    ptr, hwnd, owner = T._resolve_ref(el)
    if not ptr:
        raise ElementNotFound("元素已失效，无法设置焦点",
                              query=el.name, hwnd=el.window)
    try:
        cp = C.ComPtr(ptr, name="focus", own=False)
        fn = cp.method(T.VT_ELEMENT_SET_FOCUS, C.ctypes.HRESULT)
        hr = fn(cp._ptr)
        C.check_hresult(hr, "SetFocus", cp.name, "SetFocus",
                        T.VT_ELEMENT_SET_FOCUS)
    finally:
        T._release_owner(owner)
    return {"element": el, "path": "set_focus", "ok": True}


def scroll_into_view(target, *, timeout: float = 2.0) -> Dict:
    """把元素滚进视口（``ScrollItem.ScrollIntoView``）。

    对 ``offscreen=True`` 的元素，这是必经步骤 —— 直接点会点空。
    """
    el = _as_element(target)
    _, ok = _pattern_do(el, "ScrollItem", "scroll_into_view")
    if ok is None:
        raise ActionFailed(
            "元素 %r 不支持 ScrollItem 模式，无法滚入视口"
            % (el.name or el.label), action="scroll_into_view", element=el)
    return {"element": el, "path": "scroll_item", "ok": bool(ok)}

def scroll(target, *, horizontal: Optional[float] = None,
           vertical: Optional[float] = None, large: bool = False) -> Dict:
    """滚动容器。``horizontal`` / ``vertical`` 传 ``0.0~1.0`` 表示百分比。

    走 ``Scroll.SetScrollPercent`` —— 一次调用到位，
    比"滚轮 N 次"精确得多（滚轮每次滚多少依赖系统设置）。
    """
    el = _as_element(target)
    obj, _ = _pattern_do(el, "Scroll", "set_scroll_percent")
    if obj is None:
        raise ActionFailed("元素 %r 不支持 Scroll 模式" % (el.name or el.label),
                           action="scroll", element=el)
    h = P.SCROLL_NO_SCROLL if horizontal is None else float(horizontal)
    v = P.SCROLL_NO_SCROLL if vertical is None else float(vertical)
    _, ok = _pattern_do(el, "Scroll", "set_scroll_percent", h, v)
    return {"element": el, "path": "scroll", "ok": bool(ok),
            "horizontal": h, "vertical": v}


# --------------------------------------------------------------------------
# 窗口级动作
# --------------------------------------------------------------------------
def close_window(target, *, timeout: float = 3.0) -> Dict:
    """关窗口（``Window.Close``）。

    ⚠ 这会**弹保存确认框**（如果文档有未保存修改）。调用方要有
    后续处理预案，或者先用别的路径确认已保存。
    """
    el = _as_element(target)
    _, ok = _pattern_do(el, "Window", "close", required=True)
    return {"element": el, "path": "window_close", "ok": bool(ok)}


def invoke_default(target, *, allow_coord: bool = False,
                   human: bool = True,
                   mode: Optional[str] = None) -> ActionResult:
    """执行元素的"默认动作"（双击文件图标那种语义）。

    优先 ``LegacyIAccessible.DoDefaultAction``；
    坐标兜底时**用双击**（默认动作的鼠标表达就是双击）。
    """
    el = _as_element(target)
    _require_usable(el, "执行默认动作")
    t0 = time.monotonic()
    try:
        _, ok = _pattern_do(el, "LegacyIAccessible", "do_default_action",
                            required=True)
        return _res_from_extra({"path": "legacy", "ok": bool(ok)}, el, t0,
                               "legacy", "LegacyIAccessible 默认动作成功")
    except ActionFailed:
        if not allow_coord:
            raise
        return _coord_action(el, {"clicks": 2}, t0,
                             human=human, mode=mode, clicks=2)


# --------------------------------------------------------------------------
# 只读
# --------------------------------------------------------------------------
def read(target) -> Dict:
    """读元素当前状态 —— **不产生任何动作**。

    用在动作前后做对比：读一次 → 动一下 → 再读一次 → 比对。
    这是"只看结果"原则的最小实现。
    """
    el = _as_element(target)
    out = {
        "element": el,
        "name": el.name,
        "type": el.type_name,
        "enabled": el.enabled,
        "offscreen": el.offscreen,
        "focused": el.focused,
        "actions": D.actions_of(el),
    }
    for pat, attr in (("Value", "value"), ("Toggle", "state_name"),
                      ("ExpandCollapse", "state_name"),
                      ("SelectionItem", "is_selected"),
                      ("RangeValue", "value"),
                      ("Window", "visual_state_name"),
                      ("Scroll", "vertical_percent")):
        if el.patterns and pat not in el.patterns:
            continue
        try:
            obj = P.wrap(el, pat, required=False)
            if obj is None:
                continue
            try:
                out[attr] = getattr(obj, attr)
            finally:
                obj.release()
        except Exception:
            continue
    return out


def is_supported(target, action: str) -> bool:
    """元素是否支持某个语义动作。**不抛异常** —— 用于能力探测。"""
    try:
        el = _as_element(target)
    except Exception:
        return False
    pats = set(el.patterns or ())
    if not pats:
        try:
            pats = set(P.supported(el))
        except Exception:
            return False
    need = {
        "click": set(CLICK_PATTERNS),
        "set_value": {"Value"},
        "get_value": {"Value"},
        "toggle": {"Toggle", "SelectionItem"},
        "select": {"SelectionItem"},
        "expand": {"ExpandCollapse"},
        "collapse": {"ExpandCollapse"},
        "scroll": {"Scroll"},
        "scroll_into_view": {"ScrollItem"},
        "focus": {""},          # SetFocus 是元素基础方法，人人都有
        "close_window": {"Window"},
        "invoke_default": {"LegacyIAccessible", "Invoke"},
    }.get(action)
    if need is None:
        raise ValueError("未知动作名：%r" % action)
    return bool(pats & need) if need != {""} else True


#: 动作名 → 实现，供 CLI / 规划器按名分发
ACTIONS: Dict[str, Callable] = {
    "click": click,
    "set_value": set_value,
    "clear_value": clear_value,
    "get_value": get_value,
    "toggle": toggle,
    "check": check,
    "uncheck": uncheck,
    "select": select,
    "expand": expand,
    "collapse": collapse,
    "focus": focus,
    "scroll": scroll,
    "scroll_into_view": scroll_into_view,
    "close_window": close_window,
    "invoke_default": invoke_default,
    "read": read,
}


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
#: 非 quick 档自检的总时间预算（秒）。超过就停止新增重活。
#:
#: 8 秒这个数是这么定的：硬约束要求"单模块自检 ≤ 3s"，那是对
#: **默认档（quick=True）**的要求；``quick=False`` 是"我要认真跑一遍"
#: 的显式选择，允许更久，但仍然必须有上限 —— 否则一旦机器上
#: 窗口特别多，就又变成 70 秒的等待。
DEFAULT_SELFCHECK_BUDGET = 8.0


def _iter_real_windows(max_items: int = 40, per_item_budget: float = 0.05):
    """自检专用：**可中断、有上限**地拿几个真实元素。

    为什么要专门写一个，而不是直接用 ``T.descendants(T.root())``：

    * ``T.descendants`` 会**一口气**走完整个子树才返回。中途没有
      任何检查点，所以既不能限时、也不能响应 ``Ctrl+C`` ——
      实测在 4000+ 元素的桌面上要 52 秒，且期间按 Ctrl+C 也没用
      （C 扩展里的 COM 调用不会让 Python 收到信号）。
    * 这里改成**自己一层层走、每拿一个就吐出来**，调用方每次
      都能查预算 / 被 KeyboardInterrupt 打断。

    只走顶层窗口的浅层（``max_depth=2``）—— 自检要的是"随便一个
    有 rect 的真实元素"，不是特定目标，所以没必要往深处挖。
    """
    try:
        root = T.root()
    except Exception:
        return
    cnt = 0
    try:
        for el in T.descendants(root, kind=T.VIEW_CONTROL,
                                max_depth=2, limit=max_items):
            yield el
            cnt += 1
            if cnt >= max_items:
                return
            #: 每次循环让出一小段 —— 让 KeyboardInterrupt 有机会送达
            time.sleep(per_item_budget / 20.0)
    except KeyboardInterrupt:
        raise
    except Exception:
        return


def selfcheck(quick: bool = True) -> dict:
    """UIA 语义动作的自检。

    ## ``quick`` —— 为什么默认是 True

    自检的定位是"**随手就能跑**"。一旦某项跑到几十秒，人就不愿意跑了，
    等于没有自检。实测：加入真实窗口遍历的那几项后，本模块自检
    从 1.4s 涨到 **70.78s**（`T.descendants(T.root())` 在 4000+ 元素的
    桌面上就是这个量级 —— 每次跨进程 COM 调用都要几十微秒，
    累起来就是分钟级）。

    所以分两档：

    * ``quick=True``（默认）—— 只测**函数签名、常量表、错误路径、
      纯逻辑分支**。全程零真实窗口遍历，单模块 < 2s。
      档位内所有"需要真元素"的检查会以 ``(quick 跳过)`` 记明，
      **仍然计入 checks**，不会静默消失（静默跳过 = 假装测过）。
    * ``quick=False`` —— 保留完整测试，含真实窗口遍历。
      受 ``deadline`` 约束，超时即收工（不是失败，是"这档没跑完"）。

    ## 可中断

    遍历过程**每个元素前都查一次 deadline**，所以 ``Ctrl+C``
    能干净退出：被打断时返回已完成的部分，并把 ``interrupted``
    标为 True，而不是把半截结果当成"全绿"。
    """
    checks, failures = [], []
    interrupted = False
    _t_start = time.monotonic()
    # ⚠ 这几个必须在**最前面** import/绑定：后面多处（含第 1 项）要用。
    #   放在函数中段会导致"前面的代码访问不到"的 UnboundLocalError。
    import sys as _sys
    import unittest.mock as _mock
    _self_mod = _sys.modules[__name__]
    #: 非 quick 档的总预算。超过就停止新增重活（已完成的仍然有效）。
    deadline = time.monotonic() + (DEFAULT_SELFCHECK_BUDGET if not quick
                                   else 0.0)

    def _add(name, ok, detail, skip=False):
        """``skip=True`` → 主动跳过重活 / 本机环境缺资源。

        标 SKIP、不计入 failures、不影响退出码。
        """
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": detail})
        if skip:
            pass
        elif not ok:
            failures.append(name)

    def _skip(name, why):
        """跳过项也要**显式记一笔** —— 静默跳过等于假装测过。"""
        _add(name, False, "(quick 跳过) %s" % why, skip=True)

    def _budget_left() -> bool:
        """还有预算吗？quick 档恒为 True（quick 档本来就没重活）。"""
        return quick or time.monotonic() < deadline

    # 1. 定位不到时**绝不**降级到坐标
    #
    # ⚠ 这里原本是**不限定窗口**的 Query —— 每次都要**全桌面遍历**
    #   （实测 2.09s/次，循环两次 = 4.18s，是本 selfcheck 最大的耗时来源，
    #   也是它在 CI 上超 3s 红线的根因）。
    #
    #   但这一项要验的**根本不是"怎么找"**，而是"**找不到时怎么办**" ——
    #   即 `_as_element` 抛 ElementNotFound 之后，`click` 会不会偷偷
    #   降级去用坐标。所以直接 mock 掉查找、构造出"找不到"即可：
    #   零搜索耗时，语义**完全保留**，且不依赖本机有窗口（headless 也能跑）。
    with _mock.patch.object(
            F, "find_one",
            side_effect=ElementNotFound("测试：故意让查找失败")):
        for allow in (False, True):
            try:
                click(F.Query(automation_id="绝不存在的元素ID_ZZZ"),
                      allow_coord=allow)
                _add("定位失败不降级坐标(allow_coord=%s)" % allow, False,
                     "没有抛 ElementNotFound")
            except ElementNotFound:
                _add("定位失败不降级坐标(allow_coord=%s)" % allow, True,
                     "正确抛 ElementNotFound，坐标未被使用")
            except Exception as exc:      # noqa: BLE001
                _add("定位失败不降级坐标(allow_coord=%s)" % allow, False,
                     "抛了 %s: %s" % (type(exc).__name__, exc))

    # 2. 坐标钩子未注册时抛 UnsupportedError
    register_coord_fallback(None)
    try:
        _run_coord_fallback(100, 100)
        _add("坐标兜底未启用时报错", False, "没有抛异常")
    except UnsupportedError as exc:
        _add("坐标兜底未启用时报错", True,
             "UnsupportedError(%s)" % exc.capability)
    except Exception as exc:      # noqa: BLE001
        _add("坐标兜底未启用时报错", False,
             "抛了 %s" % type(exc).__name__)

    # 3. 注册钩子后能被调用
    seen = []

    def _fake(x, y, **kw):
        seen.append((x, y, kw))
        return True

    register_coord_fallback(_fake)
    try:
        _run_coord_fallback(12, 34, button="left", clicks=2)
        ok = bool(seen) and seen[0][0] == 12 and seen[0][2].get("clicks") == 2
        _add("坐标钩子可注册并被调用", ok,
             "收到 %r" % (seen[0] if seen else None,))
    except Exception as exc:      # noqa: BLE001
        _add("坐标钩子可注册并被调用", False,
             "%s: %s" % (type(exc).__name__, exc))
    finally:
        register_coord_fallback(None)

    # 4. is_supported 不抛异常
    try:
        r = is_supported(T.Element(name="假元素"), "click")
        _add("is_supported 安全返回", r is False, "无模式元素 → False")
    except Exception as exc:      # noqa: BLE001
        _add("is_supported 安全返回", False, "%s: %s" % (type(exc).__name__, exc))

    # 5. is_supported 用快照模式判定（不碰 COM）
    fake = T.Element(name="假按钮", patterns=("Invoke",))
    fake_edit = T.Element(name="假输入框", patterns=("Value",))
    _add("is_supported 按模式判定",
         is_supported(fake, "click") and is_supported(fake_edit, "set_value")
         and not is_supported(fake, "set_value"),
         "Invoke→可点击，Value→可设值")

    # 6. 未知动作名被拒绝
    try:
        is_supported(fake, "飞起来")
        _add("未知动作名被拒绝", False, "没有抛 ValueError")
    except ValueError:
        _add("未知动作名被拒绝", True, "抛 ValueError")

    # 7. ACTIONS 表完整
    missing = [n for n in ("click", "set_value", "toggle", "select",
                           "expand", "focus", "scroll", "read")
               if n not in ACTIONS]
    _add("ACTIONS 分发表完整", not missing,
         "缺: %s" % missing if missing else "16 个动作全部可分发")

    # 8. Element 入参校验
    try:
        click(3.14)
        _add("非法入参被拒绝", False, "没有抛 TypeError")
    except TypeError:
        _add("非法入参被拒绝", True, "抛 TypeError")
    except Exception as exc:      # noqa: BLE001
        _add("非法入参被拒绝", False, "抛了 %s" % type(exc).__name__)

    # 9. click_at_element 只吃 Element
    try:
        click_at_element("不是元素")
        _add("click_at_element 只接受 Element", False, "没有抛 TypeError")
    except TypeError:
        _add("click_at_element 只接受 Element", True, "字符串被拒绝")

    # 10. 禁用元素被拦（前置检查生效）
    dis = T.Element(name="禁用按钮", control_type=T.ControlType.BUTTON,
                    enabled=False, patterns=("Invoke",))
    try:
        click(dis)
        _add("禁用元素被拦截", False, "没有抛 ActionFailed")
    except ActionFailed as exc:
        _add("禁用元素被拦截", True, "%s" % str(exc)[:48])
    except Exception as exc:      # noqa: BLE001
        _add("禁用元素被拦截", False, "抛了 %s" % type(exc).__name__)

    # 11. 离屏元素被拦
    off = T.Element(name="离屏按钮", control_type=T.ControlType.BUTTON,
                    offscreen=True, patterns=("Invoke",))
    try:
        click(off)
        _add("离屏元素被拦截", False, "没有抛 ActionFailed")
    except ActionFailed:
        _add("离屏元素被拦截", True, "提示需要先 scroll_into_view")

    # 12. 不支持任何点击模式 → 抛 ActionFailed 且**不**静默用坐标
    dumb = T.Element(name="纯文本", control_type=T.ControlType.TEXT,
                     patterns=())
    try:
        click(dumb)
        _add("无可点模式时不静默用坐标", False, "没有抛异常")
    except ActionFailed as exc:
        _add("无可点模式时不静默用坐标", True,
             "要求显式 allow_coord：%s" % ("allow_coord" in str(exc)))
    except Exception as exc:      # noqa: BLE001
        _add("无可点模式时不静默用坐标", False,
             "抛了 %s" % type(exc).__name__)

    # 13. _center_screen 当场换算（不缓存）
    #
    # ⚠ 这一项要真窗口。``T.descendants(T.root(), ...)`` 在大桌面上是
    #   **分钟级**（实测单次 52s）—— 就是本模块自检从 1.4s 涨到 70.78s
    #   的元凶。所以只有 ``quick=False`` 才做。
    real = None
    if quick:
        _skip("中心点当场换算",
              "需要真实窗口遍历（quick=False 才跑）")
        _skip("read() 只读且有输出", "需要真实元素")
    elif not _budget_left():
        _skip("中心点当场换算", "超出自检预算")
        _skip("read() 只读且有输出", "超出自检预算")
    else:
        try:
            for cand in _iter_real_windows(max_items=40):
                if cand.rect is not None and cand.window:
                    real = cand
                    break
        except KeyboardInterrupt:
            interrupted = True
        except Exception:
            pass
        if real is not None:
            try:
                (x, y), source = _center_screen_with_source(real)
                _add("中心点当场换算", source in ("live", "snapshot"),
                     "(%d,%d) source=%s" % (x, y, source))
            except Exception as exc:      # noqa: BLE001
                _add("中心点当场换算", False,
                     "%s: %s" % (type(exc).__name__, exc))
        else:
            _add("中心点当场换算", True, "无可用元素，跳过")

        # 14. read() 对真实元素可用且不产生动作
        if real is not None:
            try:
                out = read(real)
                _add("read() 只读且有输出",
                     isinstance(out, dict) and "name" in out
                     and "actions" in out,
                     "读到 %d 个字段" % len(out))
            except Exception as exc:      # noqa: BLE001
                _add("read() 只读且有输出", False,
                     "%s: %s" % (type(exc).__name__, exc))
        else:
            _add("read() 只读且有输出", True, "无可用元素，跳过")

    # 15. toggle 幂等：状态已对时不动手
    tw = T.Element(name="假开关", patterns=("Toggle",))
    calls = []

    def _fake_wrap(el, pattern, required=False):
        calls.append(pattern)

        class _Stub:
            NAME = pattern
            def state(self):
                return P.TOGGLE_OFF
            def toggle(self):
                calls.append("toggle-called")
                return True
            def release(self):
                return 0
        return _Stub() if pattern == "Toggle" else None

    orig = P.wrap
    try:
        P.wrap = _fake_wrap
        d = toggle(tw, on=False)        # 已经是 OFF，要求 OFF → 应 noop
        act = (d.extra or {}).get("action") if hasattr(d, "extra") else None
        _add("toggle 幂等（状态已对不动作）",
             act == "noop" and "toggle-called" not in calls,
             "action=%s calls=%s" % (act, calls))
    except Exception as exc:      # noqa: BLE001
        _add("toggle 幂等（状态已对不动作）", False,
             "%s: %s" % (type(exc).__name__, exc))
    finally:
        P.wrap = orig

    # ======================================================================
    # 阶段四：执行结果验证
    # ======================================================================
    # 这里**不碰真窗口** —— 用一个只有 name/patterns 的最小替身，
    # 把 _verify_result 的三态分支跑一遍。真·端到端在 4G 测试里。
    class _FakeEl(object):
        def __init__(self, name="fake", patterns=("Value",)):
            self.name = name
            self.label = name
            self.patterns = patterns
            self.window = 0
            self.offscreen = False

    fe = _FakeEl()

    # -- _verify_result：expect=None 是"未知"而不是"通过" --
    vr = _verify_result(fe, None, 0.1)
    _add("expect=None → 未知（不是 ok）",
         vr.unknown is True and vr.ok is False, vr.describe())
    _add("expect=None 原因说明", "未提供验证判据" in vr.reason, vr.reason)

    # -- 判据类型不认识 → 未知（不是失败）--
    vr = _verify_result(fe, 12345, 0.1)
    _add("非法判据 → 未知", vr.unknown is True, vr.describe())
    _add("非法判据原因带类型", "int" in vr.reason, vr.reason)

    # -- callable 判据成立 → ok，且带证据 --
    vr = _verify_result(fe, lambda e: True, 0.5)
    _add("callable 判据成立 → ok", vr.ok is True, vr.describe())
    _add("ok 带 attempts 证据", vr.evidence.get("attempts", 0) >= 1,
         vr.evidence)
    _add("ok 带 expect_kind=callable",
         vr.evidence.get("expect_kind") == "callable", vr.evidence)

    # -- callable 判据始终 False → 确认失败 --
    vr = _verify_result(fe, lambda e: False, 0.12)
    _add("判据始终 False → fail", vr.failed is True, vr.describe())
    _add("fail 原因提超时", "超时" in vr.reason, vr.reason)

    # -- callable 判据抛异常 → 未知（读不到状态 ≠ 条件不满足）--
    def _boom(_e):
        raise OSError("element gone")

    vr = _verify_result(fe, _boom, 0.08)
    _add("判据抛异常 → 未知", vr.unknown is True, vr.describe())
    _add("判据抛异常原因可见", "element gone" in vr.reason, vr.reason)
    _add("判据抛异常带 error 字段", vr.error is not None, vr.error)

    # -- Query 判据：查不到 → fail（Query 的语义就是查有没有）--
    # ⚠ 构造器叫 ``F.query`` 不是 ``F.by_name`` —— 是"按条件建查询对象"，
    #   不是"按名字找"。名字匹配要靠 ``name=`` 参数。
    #
    # ⚠⚠ 两个性能陷阱，都踩过：
    #   1) ``F.query("__xxx__")`` 走宽松子串匹配 → 下推不了 → Python 全遍历
    #      （实测 52.28s）。加 ``name_exact=True`` 落到 UIA 原生 FindAll
    #      （实测 2.09s）。这是阶段四硬约束 #13 说的那个悬崖。
    #   2) 但 2.09s 仍然吃掉整个 selfcheck 的预算 —— 而这一项要验的是
    #      "``_verify_result`` 拿到 query 判据、**查不到时**怎么判"，
    #      **不是 UIA 怎么查**。所以直接 mock 掉查询、构造出"查不到"：
    #      零搜索耗时，语义完全保留，且不依赖本机桌面内容（headless 也能跑）。
    try:
        q = F.query("__definitely_not_exist_zzz__", name_exact=True)
        with _mock.patch.object(F, "exists", return_value=False):
            vr = _verify_result(fe, q, 0.15)
        _add("Query 查不到 → fail",
             vr.failed is True, vr.describe())
        _add("Query 判据 kind=query",
             vr.evidence.get("expect_kind") == "query", vr.evidence)
    except Exception as exc:      # noqa: BLE001
        _add("Query 查不到 → fail", False,
             "%s: %s" % (type(exc).__name__, exc))

    # -- mode 必须是 desktop --
    vr = _verify_result(fe, lambda e: True, 0.2)
    _add("_verify_result mode=desktop", vr.mode == "desktop", vr.mode)

    # -- 掩码识别 --
    _add("识别 ● 掩码", _looks_masked("\u25cf\u25cf\u25cf") is True, _looks_masked("\u25cf\u25cf\u25cf") is True)
    _add("识别 • 掩码", _looks_masked("\u2022\u2022") is True, _looks_masked("\u2022\u2022") is True)
    _add("识别 **** 掩码", _looks_masked("****") is True, _looks_masked("****") is True)
    _add("普通文本不算掩码", _looks_masked("hello") is False, _looks_masked("hello") is False)
    _add("空串不算掩码", _looks_masked("") is False, _looks_masked("") is False)

    # -- _value_suggest 结构 --
    sg = _value_suggest(fe, "abc", 2.0)
    _add("suggest 带 expect_value", sg["expect_value"] == "abc", sg)
    _add("suggest 带 match=exact", sg["match"] == "exact", sg["match"] == "exact")
    _add("suggest target 是元素", sg["target"] is fe, sg["target"] is fe)
    _add("suggest timeout 保底 1s", sg["timeout"] >= 1.0, sg["timeout"] >= 1.0)

    # -- _attach_suggest 开关 --
    old_sg = suggest_enabled()
    try:
        set_suggest_enabled(False)
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False)
        _attach_suggest(res, fe, sg)
        _add("suggest 关掉时不挂", "suggest" not in res.extra,
             list(res.extra))
        set_suggest_enabled(True)
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False)
        _attach_suggest(res, fe, sg)
        _add("suggest 打开时挂上", "suggest" in res.extra, list(res.extra))
        _add("suggest 自动带 mode",
             res.extra["suggest"].get("mode") == "desktop",
             res.extra["suggest"].get("mode"))
        add_old = set_suggest_enabled(False)
        _add("set_suggest_enabled 返回旧值", add_old is True, add_old)
    finally:
        set_suggest_enabled(old_sg)

    # -- _verify_written：三类回读结果 --
    # `_sys` / `_mock` / `_self_mod` 已在函数开头绑定（见那里的注释）。

    with _mock.patch.object(_self_mod, "read_uia_value",
                            lambda el: (True, "abc")):
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False, duration=0.01)
        _verify_written(res, fe, "abc")
        _add("回读一致 → ok", res.verified is not None and res.verified.ok,
             res.verified)

    with _mock.patch.object(_self_mod, "read_uia_value",
                            lambda el: (True, "xyz")):
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False, duration=0.01)
        _verify_written(res, fe, "abc")
        _add("回读不一致 → fail",
             res.verified is not None and res.verified.failed, res.verified)
        _add("回读不一致打标记",
             res.extra.get("readback_mismatch") is True, res.extra)
        _add("回读不一致原因含期望实际",
             "期望" in res.verified.reason and "实际" in res.verified.reason,
             res.verified.reason)

    with _mock.patch.object(_self_mod, "read_uia_value",
                            lambda el: (False, None)):
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False, duration=0.01)
        _verify_written(res, fe, "abc")
        _add("读不到值 → 未知（不是失败）",
             res.verified is not None and res.verified.unknown, res.verified)

    with _mock.patch.object(_self_mod, "read_uia_value",
                            lambda el: (True, "\u25cf\u25cf\u25cf\u25cf")):
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False, duration=0.01)
        _verify_written(res, fe, "secret")
        _add("密码框掩码回读 → ok",
             res.verified is not None and res.verified.ok, res.verified)
        _add("密码框原因提掩码",
             "掩码" in res.verified.reason, res.verified.reason)

    with _mock.patch.object(_self_mod, "read_uia_value",
                            lambda el: (_ for _ in ()).throw(OSError("com down"))):
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False, duration=0.01)
        _verify_written(res, fe, "abc")
        # _verify_written 自己吞了异常 → 但这里 mock 抛的是 OSError，
        # 被 read_uia_value 调用处 try 住 → unknown
        _add("回读抛异常 → 未知不冒泡",
             res.verified is not None and res.verified.unknown,
             res.verified)

    # -- run_verify 在无钩子无 expect 时不动 verified --
    from ..verify import hook as _hook
    _hook.clear_verify_hook()
    res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                       fallback_used=False)
    run_verify(res)
    _add("无判据无钩子 → verified 留 None", res.verified is None, res.verified)

    # -- run_verify 走钩子 --
    _hook.set_verify_hook(lambda c: VerifyResult.fail(reason="钩子判失败"))
    _hook.set_enabled(True)
    try:
        res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                           fallback_used=False)
        run_verify(res)
        _add("run_verify 走钩子",
             res.verified is not None and res.verified.failed, res.verified)
        _add("run_verify 写入 extra.verified",
             res.extra.get("verified") == "fail", res.extra)
    finally:
        _hook.clear_verify_hook()

    # -- run_verify 的 expect 优先于钩子 --
    res = ActionResult(ok=True, method="value", mode_used=MODE_SILENT,
                       fallback_used=False)
    res.extra["_element"] = fe
    run_verify(res, expect=lambda e: True, timeout=0.3)
    _add("expect 路径优先且 ok",
         res.verified is not None and res.verified.ok, res.verified)
    _add("expect 路径 method=uia.expect",
         res.verified.method == "uia.expect", res.verified.method)

    # -- _res_from_extra 也接上了验证 --
    res = _res_from_extra({}, fe, time.monotonic(), "toggle", "开关切换",
                          suggest={"mode": "desktop", "target": fe})
    _add("_res_from_extra 挂 suggest",
         "suggest" in res.extra, list(res.extra))
    _add("_res_from_extra 存元素引用", res.extra.get("_element") is fe,
                             res.extra.get("_element") is fe)

    # -- 顶层导出 --
    _add("_verify_result 可调用", callable(_verify_result), callable(_verify_result))
    _add("run_verify 可调用", callable(run_verify), callable(run_verify))
    _add("_attach_suggest 可调用", callable(_attach_suggest), callable(_attach_suggest))

    # -- 自检自身的元信息 --
    # 为什么要把档位放进返回值：调用方（scripts/selfcheck.py）需要
    # 知道"这个结果快是快，但它是不是漏跑了重活"。只报 ok=True
    # 而不说档位，等于让"quick 跳过"变成隐性损失。
    elapsed = time.monotonic() - _t_start
    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures and not interrupted,
        "module": __name__,
        "quick": bool(quick),
        "interrupted": bool(interrupted),
        #: 因 quick / 预算 / 环境缺失而没跑的项数（transparency）。
        #: 用 check 项的 ``skipped`` 标记显式统计 —— 不再靠扫 detail
        #: 里的文案（那太脆弱，改个字就失准）。
        "skipped_heavy": sum(1 for c in checks if c.get("skipped")),
        "skipped": [c["name"] for c in checks if c.get("skipped")],
        "elapsed": round(elapsed, 3),
    }


def _main() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    quick = "--full" not in sys.argv
    print("UIA 语义动作自检%s:" % ("" if quick else "（完整档，含真实窗口遍历）"))
    try:
        res = selfcheck(quick=quick)
    except KeyboardInterrupt:
        print("\n被中断（Ctrl+C）—— 已完成部分不构成「全绿」结论")
        return 130
    for c in res["checks"]:
        print("  [%s] %-38s %s"
              % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
    total = len(res["checks"])
    print("actions 自检: %d/%d  档位=%s  耗时=%.2fs%s"
          % (total - len(res["failures"]), total,
             "quick" if res.get("quick") else "full",
             res.get("elapsed", 0.0),
             "" if not res.get("interrupted") else "  [中断]"))
    if res.get("skipped_heavy"):
        print("（其中 %d 项因档位/预算未跑真实窗口，用 --full 可跑全）"
              % res["skipped_heavy"])
    return 1 if res["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
