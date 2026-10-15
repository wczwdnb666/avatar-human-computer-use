# -*- coding: utf-8 -*-
"""动作后自动验证的**钩子注册表**。

## 为什么需要一层钩子，而不是直接在动作里调 verify_action

``input`` 层和 ``uia`` 层都不该硬依赖 ``verify`` 层：验证要截图、要读
UIA、要 CDP 会话，这些东西是**上层策略**，不是输入层该知道的事。
硬 import 会成环（``verify.desktop`` → ``vision`` → ``input``），
也会让"只想发个点击、不想验证"的调用方被迫付出截图成本。

所以采用**注册表 + 默认关闭**：

- 动作层在结束前调 :func:`run_verify_hook`，传进去"我刚刚干了什么"；
- 没人注册过钩子 → 立刻返回 ``None``，**零开销**（只有一次 dict 查空）；
- 上层（CLI / 编排器 / 测试）用 :func:`set_verify_hook` 挂上真验证。

## 三种注册方式

::

    from avatar.verify import hook

    # 1) 直接给函数
    hook.set_verify_hook(lambda ctx: verify_action(**ctx))

    # 2) 给一个"永远用同一套判据"的 VerifyRequest
    hook.set_verify_hook(VerifyRequest("desktop", uia_target=..., ...))

    # 3) 给 dict（会被当 kwargs 传给 verify_action）
    hook.set_verify_hook({"mode": "desktop", "uia_target": ...})

## 默认判据从哪来

动作自己最清楚该验证什么 —— 点了 ``#submit`` 就该等 ``#result`` 出现。
所以动作会把**建议判据**放进上下文（``suggest`` 字段）。
钩子可以选择用、可以忽略、可以覆盖：

::

    def my_hook(ctx):
        # ctx["suggest"] 里是动作给的线索
        hint = ctx.get("suggest") or {}
        if not hint:
            return None              # 动作没说该验什么 → 不验
        return verify_action(**hint)

:func:`auto_verify_hook` 就是这样一个实现，直接拿来用即可。

## 开关

:func:`enabled` / :func:`set_enabled` 控制全局开关。**默认关闭** ——
原因见 ``docs/verify.md``：验证要截图要读 DOM，有成本；而且
"每个点击都自动验证"在批量操作（填 50 个格子）时是灾难。由上层决定。
"""

import threading
from typing import Any, Callable, Dict, Optional

__all__ = [
    "HookError",
    "ACTION_CLICK",
    "ACTION_SET_VALUE",
    "ACTION_NAVIGATE",
    "ACTION_KEY",
    "ACTION_SCROLL",
    "ACTION_GENERIC",
    "build_context",
    "set_verify_hook",
    "get_verify_hook",
    "clear_verify_hook",
    "has_verify_hook",
    "set_enabled",
    "enabled",
    "auto_verify_hook",
    "run_verify_hook",
    "describe_hook",
]


class HookError(Exception):
    """钩子配置错误（判据类型不认识等）。"""

    exit_code = 27


#: 动作类型常量。用字符串而不是 Enum —— 要能原样进 JSON 日志。
ACTION_CLICK = "click"
ACTION_SET_VALUE = "set_value"
ACTION_NAVIGATE = "navigate"
ACTION_KEY = "key"
ACTION_SCROLL = "scroll"
ACTION_GENERIC = "generic"

_VALID_ACTIONS = (
    ACTION_CLICK, ACTION_SET_VALUE, ACTION_NAVIGATE, ACTION_KEY,
    ACTION_SCROLL, ACTION_GENERIC,
)

#: 全局状态。加锁是因为 CLI 可能多线程跑任务（``avatar batch``）。
#: 用类持有而不是模块级 ``global``，测试才能构造隔离实例。
class _HookState(object):
    __slots__ = ("hook", "enabled", "lock", "calls", "skipped")

    def __init__(self):
        self.hook: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.enabled: bool = False
        self.lock = threading.RLock()
        #: 统计：实际跑了多少次验证
        self.calls: int = 0
        #: 统计：因为没钩子/没开开关/动作没给判据而跳过了多少次
        self.skipped: int = 0


_STATE = _HookState()


def build_context(
    action: str,
    *,
    ok: bool = True,
    target: Any = None,
    hwnd: Optional[int] = None,
    point: Any = None,
    mode_used: str = "",
    method: str = "",
    value: Any = None,
    suggest: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造传给钩子的上下文。

    只放**验证真的会用到的**东西。不放整个 ActionResult —— 那会让钩子
    和 ActionResult 的字段演进绑死，加个字段就得改所有钩子。

    :param suggest: 动作建议的验证判据（会被直接传给 ``verify_action``）。
        这是本机制的核心：**该验什么，动作比钩子清楚**。
        例如 ``set_value`` 会建议 ``{"uia_target": el, "expect_value": text}``。
    """
    ctx = {
        "action": action,
        "ok": bool(ok),
        "target": target,
        "hwnd": hwnd,
        "point": point,
        "mode_used": mode_used,
        "method": method,
        "value": value,
        "suggest": dict(suggest) if suggest else None,
        "extra": dict(extra) if extra else {},
    }
    return ctx


def set_verify_hook(hook: Any) -> Any:
    """注册验证钩子。返回归一化后的可调用对象。

    接受三种形态（见模块文档）。传 ``None`` 等价于 :func:`clear_verify_hook`。

    **注册不会自动打开开关** —— 还得 :func:`set_enabled` (True)。
    这是刻意的：注册是"我知道怎么验"，开关是"我现在要验"，
    两件事。批量操作时临时关掉、事后打开，不用重注册。
    """
    if hook is None:
        clear_verify_hook()
        return None

    norm = _normalize_hook(hook)
    with _STATE.lock:
        _STATE.hook = norm
    return norm


def _normalize_hook(hook: Any) -> Callable[[Dict[str, Any]], Any]:
    """把各种钩子形态归一成 ``fn(ctx) -> Optional[VerifyResult]``。"""
    if callable(hook):
        return hook

    # 延迟导入：避免 verify.hook ←→ verify.action 的循环
    from .action import VerifyRequest, verify_action

    if isinstance(hook, VerifyRequest):
        req = hook
        return lambda _ctx, _r=req: verify_action(request=_r)

    if isinstance(hook, dict):
        kw = dict(hook)
        return lambda _ctx, _kw=kw: verify_action(**_kw)

    raise HookError(
        "钩子类型无法识别：%r（支持：可调用 / VerifyRequest / dict）"
        % (type(hook).__name__,))


def get_verify_hook() -> Optional[Callable[[Dict[str, Any]], Any]]:
    """取当前钩子（没注册返回 ``None``）。"""
    with _STATE.lock:
        return _STATE.hook


def clear_verify_hook() -> None:
    """摘掉钩子并关掉开关。"""
    with _STATE.lock:
        _STATE.hook = None
        _STATE.enabled = False


def has_verify_hook() -> bool:
    """是否注册了钩子（**不代表开关是开的**）。"""
    with _STATE.lock:
        return _STATE.hook is not None


def set_enabled(on: bool) -> bool:
    """开关自动验证。返回旧值。

    没注册钩子时也能打开 —— 打开后 :func:`run_verify_hook` 会走
    "没钩子"分支返回 ``None``，不报错。这样调用方可以先开开关、
    后注册钩子（CLI 里就是先解析 ``--verify`` 再建会话）。
    """
    with _STATE.lock:
        old = _STATE.enabled
        _STATE.enabled = bool(on)
        return old


def enabled() -> bool:
    """开关当前状态。"""
    with _STATE.lock:
        return _STATE.enabled


def auto_verify_hook(ctx: Dict[str, Any]) -> Any:
    """默认钩子实现：**只用动作给的判据**。

    动作没给 ``suggest`` → 返回 ``None``（不验证）。
    这是保守但正确的默认 —— 强行给个通用判据（比如"窗口标题变了"）
    只会产生一堆假阳性，比不验还坏。

    动作给的 ``suggest`` 里如果有 ``mode``，尊重它；没有就交给
    ``verify_action`` 自己路由（auto）。
    """
    from .action import verify_action

    hint = (ctx or {}).get("suggest")
    if not hint:
        return None
    kw = dict(hint)
    kw.setdefault("timeout", 5.0)
    try:
        return verify_action(**kw)
    except Exception as exc:      # noqa: BLE001
        # 钩子**永远不能**让动作失败。验证是附加信息，不是前置条件。
        from .result import VerifyResult
        return VerifyResult.unknown_(
            method="hook", reason="自动验证执行失败：%s" % (exc,),
            error=str(exc))


def run_verify_hook(
    ctx: Dict[str, Any],
    *,
    hook: Any = None,
    force: bool = False,
) -> Any:
    """动作层调用入口。返回 ``VerifyResult`` 或 ``None``。

    **本函数永不抛异常**（除了 ``KeyboardInterrupt``）—— 动作已经
    做完了，不能因为验证环节炸掉就把成功说成失败。

    跳过条件（任一命中即返回 ``None`` 并计入 ``skipped``）：

    1. 动作本身 ``ok=False``（失败的动作该走错误路径，不是验证路径）
    2. 没注册钩子且 ``hook`` 参数也没给且 ``force=False``
    3. 开关关着且 ``force=False``

    :param hook: 临时覆盖已注册的钩子（不改全局状态）
    :param force: 无视开关与"必须注册过"的限制，强行跑
        （``force=True`` 且 hook 为 None 时用 :func:`auto_verify_hook`）
    """
    try:
        if not (ctx or {}).get("ok", True):
            with _STATE.lock:
                _STATE.skipped += 1
            return None

        fn = hook
        if fn is None:
            with _STATE.lock:
                if not _STATE.enabled:
                    _STATE.skipped += 1
                    return None
                fn = _STATE.hook

        if fn is None:
            if not force:
                with _STATE.lock:
                    _STATE.skipped += 1
                return None
            fn = auto_verify_hook

        with _STATE.lock:
            _STATE.calls += 1

        return fn(ctx)

    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:      # noqa: BLE001
        from .result import VerifyResult
        return VerifyResult.unknown_(
            method="hook", reason="自动验证执行失败：%s" % (exc,),
            error=str(exc))


def describe_hook() -> Dict[str, Any]:
    """给 CLI / 诊断用的钩子状态。"""
    with _STATE.lock:
        fn = _STATE.hook
        return {
            "registered": fn is not None,
            "hook": getattr(fn, "__name__", type(fn).__name__) if fn else None,
            "enabled": _STATE.enabled,
            "calls": _STATE.calls,
            "skipped": _STATE.skipped,
        }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    from .result import VerifyResult

    # --- 初始状态 ---
    clear_verify_hook()
    add("初始无钩子", has_verify_hook() is False)
    add("初始开关关", enabled() is False)
    add("初始 describe registered=False",
        describe_hook()["registered"] is False)

    # --- build_context ---
    ctx = build_context(ACTION_CLICK, target="x", hwnd=1, point=(3, 4),
                        mode_used="physical", method="mouse",
                        suggest={"mode": "desktop"})
    add("上下文动作名", ctx["action"] == ACTION_CLICK, ctx["action"])
    add("上下文 ok 默认 True", ctx["ok"] is True)
    add("上下文 suggest 拷贝",
        ctx["suggest"] == {"mode": "desktop"} and
        ctx["suggest"] is not None)
    add("上下文无 suggest 时为 None",
        build_context(ACTION_CLICK)["suggest"] is None)
    add("上下文 extra 默认空 dict",
        build_context("x")["extra"] == {})
    add("build_context ok=False 可传",
        build_context("x", ok=False)["ok"] is False)

    # --- 跳过条件 1：动作失败 ---
    calls = []

    def rec_hook(c):
        calls.append(c)
        return VerifyResult.ok_(reason="记录")

    set_verify_hook(rec_hook)
    set_enabled(True)
    r = run_verify_hook(build_context(ACTION_CLICK, ok=False))
    add("动作失败不验证", r is None and not calls, r)

    # --- 跳过条件 2：开关关着 ---
    set_enabled(False)
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("开关关着不验证", r is None and not calls, r)

    # --- 正常路径 ---
    set_enabled(True)
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("开关开着会验证", isinstance(r, VerifyResult) and len(calls) == 1,
        (type(r).__name__, len(calls)))
    add("钩子收到完整上下文", calls[0]["action"] == ACTION_CLICK)

    # --- 没注册钩子但开关开着 → 跳过（不是 force）---
    saved = get_verify_hook()
    clear_verify_hook()
    set_enabled(True)
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("无钩子不验证", r is None, r)
    add("无钩子时 has_verify_hook False", has_verify_hook() is False)

    # --- force 用 auto_verify_hook ---
    r = run_verify_hook(
        build_context(ACTION_CLICK, suggest=None), force=True)
    add("force 且无判据 → None", r is None, r)
    r = run_verify_hook(
        build_context(ACTION_CLICK,
                      suggest={"mode": "desktop", "target": "zzz",
                               "timeout": 0.2}),
        force=True)
    add("force 且有判据 → VerifyResult", isinstance(r, VerifyResult),
        type(r).__name__)

    # --- 钩子抛异常 → unknown，不冒泡 ---
    set_verify_hook(lambda c: (_ for _ in ()).throw(RuntimeError("hook boom")))
    set_enabled(True)
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("钩子抛异常 → unknown",
        isinstance(r, VerifyResult) and r.unknown, r)
    add("钩子抛异常原因可见",
        r is not None and "hook boom" in r.reason,
        r.reason if r is not None else None)

    # --- KeyboardInterrupt 必须冒泡 ---
    set_verify_hook(lambda c: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        run_verify_hook(build_context(ACTION_CLICK))
        add("KeyboardInterrupt 冒泡", False, "被吞了")
    except KeyboardInterrupt:
        add("KeyboardInterrupt 冒泡", True)

    # --- 注册形态：VerifyRequest ---
    from .action import VerifyRequest
    set_verify_hook(VerifyRequest("desktop", target="zzz", timeout=0.2))
    add("注册 VerifyRequest 可用", callable(get_verify_hook()))
    set_enabled(True)
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("VerifyRequest 钩子返回结果", isinstance(r, VerifyResult),
        type(r).__name__)

    # --- 注册形态：dict ---
    set_verify_hook({"mode": "desktop", "target": "zzz", "timeout": 0.2})
    add("注册 dict 可用", callable(get_verify_hook()))
    r = run_verify_hook(build_context(ACTION_CLICK))
    add("dict 钩子返回结果", isinstance(r, VerifyResult), type(r).__name__)

    # --- 注册形态：非法 ---
    try:
        set_verify_hook(12345)
        add("非法钩子抛 HookError", False, "没抛")
    except HookError:
        add("非法钩子抛 HookError", True)

    # --- set_verify_hook(None) 等价 clear ---
    set_verify_hook(None)
    add("set None 等价清空", has_verify_hook() is False)

    # --- 临时 hook 参数覆盖全局 ---
    set_verify_hook(rec_hook)
    set_enabled(True)
    n = len(calls)
    tmp = lambda c: VerifyResult.fail(reason="临时")     # noqa: E731
    r = run_verify_hook(build_context(ACTION_CLICK), hook=tmp)
    add("临时钩子生效", r is not None and r.failed, r)
    add("临时钩子不写入全局记录", len(calls) == n, (len(calls), n))
    add("全局钩子没被换掉", get_verify_hook() is not None)

    # --- set_enabled 返回旧值 ---
    set_enabled(False)
    old = set_enabled(True)
    add("set_enabled 返回旧值", old is False, old)

    # --- clear 同时关开关 ---
    set_enabled(True)
    clear_verify_hook()
    add("clear 关掉开关", enabled() is False and has_verify_hook() is False)

    # --- 统计计数 ---
    clear_verify_hook()
    set_verify_hook(rec_hook)
    set_enabled(True)
    before = describe_hook()
    run_verify_hook(build_context(ACTION_CLICK))
    run_verify_hook(build_context(ACTION_CLICK, ok=False))
    after = describe_hook()
    add("calls 计数增加", after["calls"] == before["calls"] + 1,
        (before["calls"], after["calls"]))
    add("skipped 计数增加", after["skipped"] == before["skipped"] + 1,
        (before["skipped"], after["skipped"]))
    add("describe 带钩子名", after["hook"] == "rec_hook", after["hook"])

    # --- auto_verify_hook 直接用 ---
    r = auto_verify_hook({"suggest": None})
    add("auto_verify_hook 无判据 None", r is None, r)
    r = auto_verify_hook({"suggest": {"mode": "desktop", "target": "zzz",
                                      "timeout": 0.2}})
    add("auto_verify_hook 有判据返回结果",
        isinstance(r, VerifyResult), type(r).__name__)

    # 收尾，别把状态留给后面的用例
    clear_verify_hook()

    failed = [c for c in checks if not c["ok"]]
    return {
        "checks": checks,
        "failures": [c["name"] for c in failed],
        "ok": not failed,
        "total": len(checks),
        "passed": len(checks) - len(failed),
    }


def _main() -> int:
    r = selfcheck()
    for c in r["checks"]:
        mark = "OK  " if c["ok"] else "[!!]"
        print("%s %s | %s" % (mark, c["name"], c["detail"]))
    print("\n%d/%d 通过" % (r["passed"], r["total"]))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
