# -*- coding: utf-8 -*-
"""执行结果验证机制（双端统一）。

## 为什么需要它

阶段二/三的所有动作都返回 ``ok=True`` —— 但那个 ``ok`` 的含义是
**"我把指令送出去了"**，不是 **"事情真的发生了"**。典型翻车场景：

- UIA ``SetValue`` 成功返回，但目标控件是只读的，值没进去。
- 点击坐标算对了，但弹出个模态框把点击吞了。
- ``Page.navigate`` 返回成功，但服务器 302 到了登录页。
- 表单提交了，接口返回 500 —— UI 上什么都没变，但业务失败了。

**"没报错" ≠ "成功了"**。这个包存在的唯一目的，就是把两者分开。

## 核心契约

所有判断都返回 :class:`VerifyResult`，**绝不返回裸 bool**。
理由是：验证是**证据**行为，必须能回答"凭什么说成功了"。
裸 bool 抹掉了证据，出问题时完全无法回溯。

``VerifyResult`` 的四个状态（不是两个）：

====================  ==========================================
``ok=True``           确认成功，``evidence`` 里有依据
``ok=False``          确认失败，``reason`` 说明为什么
``ok=False, unknown`` 无法判定（拿不到证据）—— **不等于失败**
====================  ==========================================

第三态很重要：截图失败、元素不在可视区、DOM 读不到，
这些是"我不知道"，不是"没成功"。上层应该**重试或降级**，
而不是当成失败去回滚。

## 双端统一

同一个入口 :func:`verify_action`，按 ``mode`` 路由：

============  ========================================================
``desktop``   截图 pHash 比对 + UIA 状态回读
``web``       CDP 回读 DOM + URL 变化 + 网络响应
``auto``      有 CDP 会话走 web，否则走 desktop
============  ========================================================

## 分层

::

    result.py     VerifyResult 数据结构 + 比对工具（pHash / 相似度）
    desktop.py    桌面端：像素 + UIA
    web.py        Web 端：DOM + URL + 网络
    action.py     统一入口 verify_action（自动路由）
    retry.py      @with_retry 装饰器（动作 + 自动验证 + 退避重试）
"""

from .result import (
    VerifyResult,
    VERIFY_OK,
    VERIFY_FAIL,
    VERIFY_UNKNOWN,
    VerificationError,
    phash,
    hamming_distance,
    image_similarity,
    images_differ,
    snapshot_hash,
    result_of,
)

from .desktop import (
    verify_pixel,
    verify_uia_state,
    verify_uia_exists,
    verify_uia_value,
    verify_window_title,
    verify_desktop,
)

from .web import (
    verify_dom,
    verify_dom_text,
    verify_dom_value,
    verify_element_visible,
    verify_url,
    verify_network,
    verify_title,
    verify_web,
)

from .action import (
    VerifyRequest,
    verify_action,
    verify_any,
    describe_verify,
)

from .retry import (
    RetryPolicy,
    RetryOutcome,
    with_retry,
    retry_call,
)

from . import hook

__all__ = [
    # result
    "VerifyResult", "VERIFY_OK", "VERIFY_FAIL", "VERIFY_UNKNOWN",
    "VerificationError", "phash", "hamming_distance", "image_similarity",
    "images_differ", "snapshot_hash", "result_of",
    # desktop
    "verify_pixel", "verify_uia_state", "verify_uia_exists",
    "verify_uia_value", "verify_window_title", "verify_desktop",
    # web
    "verify_dom", "verify_dom_text", "verify_dom_value",
    "verify_element_visible", "verify_url", "verify_network",
    "verify_title", "verify_web",
    # action
    "VerifyRequest", "verify_action", "verify_any", "describe_verify",
    # retry
    "RetryPolicy", "RetryOutcome", "with_retry", "retry_call",
    # hook
    "hook",
]


def selfcheck() -> dict:
    """包级自检：汇总各子模块。"""
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        if not ok:
            failures.append(name)

    import importlib
    _SUBS = ("result", "desktop", "web", "action", "retry", "hook")
    for mod_name in _SUBS:
        mod = globals().get(mod_name)
        if mod is None or not hasattr(mod, "selfcheck"):
            try:
                mod = importlib.import_module("." + mod_name, __name__)
            except Exception as exc:      # noqa: BLE001
                _add("%s 可导入" % mod_name, False,
                     "%s: %s" % (type(exc).__name__, exc))
                continue
        fn = getattr(mod, "selfcheck", None)
        if fn is None:
            _add("%s.selfcheck 存在" % mod_name, False, "模块没有 selfcheck()")
            continue
        try:
            sub = fn()
        except Exception as exc:      # noqa: BLE001
            _add("%s.selfcheck 可跑" % mod_name, False,
                 "%s: %s" % (type(exc).__name__, exc))
            continue
        total = len(sub.get("checks", []))
        bad = len(sub.get("failures", []))
        _add("%s.selfcheck" % mod_name, bool(sub.get("ok")),
             "%d/%d 通过" % (total - bad, total))

    # 契约检查：verify_action 必须返回 VerifyResult
    # 注意 ``verify_action(mode, *, ...)`` —— mode 之后全是 kw-only，
    # 目标必须用关键字传（踩过一次：写成了位置参数直接 TypeError）。
    try:
        r = verify_action("desktop", target="nonexistent_target_zzz",
                          timeout=0.2)
        _add("verify_action 返回 VerifyResult", isinstance(r, VerifyResult),
             type(r).__name__)
        _add("verify_action 不抛异常（目标不存在）", True, "ok")
        _add("边界目标 → 未知而非失败", r.unknown, r.describe())
    except Exception as exc:      # noqa: BLE001
        _add("verify_action 不抛异常", False, "%s: %s" % (type(exc).__name__, exc))

    # 契约检查：禁止裸 bool
    _add("VERIFY_UNKNOWN 与 FAIL 可区分",
         VERIFY_UNKNOWN != VERIFY_FAIL, (VERIFY_UNKNOWN, VERIFY_FAIL))

    return {"checks": checks, "failures": failures, "ok": not failures}
