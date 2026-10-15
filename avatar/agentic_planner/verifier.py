# -*- coding: utf-8 -*-
"""步骤验证 —— 判断"这一步到底成了没有"。

## 为什么执行器自己不能判断

因为"动作发出去了"和"事情真的发生了"是两件事。经典的翻车场景：
静默点击 ``PostMessage`` 返回成功，但窗口根本没理它。

所以执行器只负责**发动作**，判断交给本模块。两者分开还有一个好处：
验证策略可以换（像素 / UIA / DOM），而执行器一行都不用改。

## 三个后端

============================  ================================================
后端                         判据
============================  ================================================
:class:`PixelDiffVerifier`   截图 pHash 差异（复用阶段四）
:class:`UiaStateVerifier`    UIA 回读元素状态
:class:`DomVerifier`         CDP 回读 DOM / URL
============================  ================================================

外加三个**不需要后端**的轻量判据（直接看观测快照）：
``window`` / ``window_gone`` / ``elapsed`` / ``file``。

## 三态，不是布尔（复用阶段四的契约）

``VerifyResult`` 有三个结论：``ok`` / ``fail`` / ``unknown``。

**``unknown`` 和 ``fail`` 必须分开**：

- ``fail`` —— 查了，确实没成 → 该重试
- ``unknown`` —— 没查成（没截图、没会话、元素找不到）
  → 重试可能有用，也可能白费，但**不能当成成功放行**

把 unknown 当 fail 会导致"明明成功了却一直重试"；
把 unknown 当 ok 会导致"什么都没做却以为做完了"。
两种都是灾难。

## 能力边界

**做**：按 ``step.verify["kind"]`` 路由到对应判据；产出三态结论；
把证据（差异值、回读到的值）放进 ``evidence``。

**不做**：

- **不执行动作**。只读不写。
- **不重试**。重试决策在 :mod:`recovery`。
- **不做图像识别**。pHash 只回答"变没变"，不回答"变成了什么"。
  那是阶段七的视觉能力。
- **不硬解验证题**（硬约束 #9）。本模块没有一行识别验证题答案的代码。
- **不缓存观测**。before / after 由执行器传进来，本模块不自己去拍。
"""

from __future__ import annotations

import abc
import os
import time
from typing import Any, Dict, List, Optional

from ..verify.result import VerifyResult

__all__ = [
    "PIXEL_DIFF_THRESHOLD",
    "VerifierBackend",
    "PixelDiffVerifier",
    "UiaStateVerifier",
    "DomVerifier",
    "get_verifier",
    "verify",
    "describe_verifier",
    "selfcheck",
]

#: 像素差异阈值（汉明距离）。**超过就算"有变化"。**
#:
#: 64 位 pHash 上，阈值 3 意味着"约 5% 的位不同"。
#: 定这么小是因为：正常渲染的两次截图（没有任何变化）差异应该是 0~2 位
#: （抗锯齿、光标闪烁）。定 8 以上会把"鼠标动了一下"也算成变化。
PIXEL_DIFF_THRESHOLD = 3


# --------------------------------------------------------------------------
# 抽象后端
# --------------------------------------------------------------------------
class VerifierBackend(abc.ABC):
    """验证后端的接口。**唯一的方法是 :meth:`verify`。**"""

    #: 后端标识，用于日志与 ``get_verifier`` 路由。
    name: str = "abstract"

    #: 本后端能处理哪些 ``step.verify["kind"]``。
    kinds: tuple = ()

    @abc.abstractmethod
    def verify(self, step: Any, before: Any, after: Any) -> VerifyResult:
        """判断这一步成功没有。**必须返回三态结论。**"""
        raise NotImplementedError

    def can_handle(self, kind: str) -> bool:
        return str(kind or "") in self.kinds

    def describe(self) -> str:
        return "%s（处理 kind: %s）" % (type(self).__name__,
                                       ", ".join(self.kinds) or "—")


# --------------------------------------------------------------------------
# 后端 1：像素差异
# --------------------------------------------------------------------------
class PixelDiffVerifier(VerifierBackend):
    """截图 pHash 比对。**复用阶段四的 :func:`verify.result.snapshot_hash`。**

    ## 判据

    两张截图的 pHash 汉明距离 > :data:`PIXEL_DIFF_THRESHOLD` → 有变化 → ok。

    ## 这个判据的适用边界

    - **适用**：点击、按键、滚动 —— "界面应该有反应"的动作。
    - **不适用**：等待（什么都没变才是对的）、
      输入（文字变化可能很小）、导航（整页都变了）。
      所以 :mod:`planner` 只给 click / key / scroll 配了 pixel 判据。

    ## 为什么用 pHash 而不是逐像素比对

    逐像素比对会被抗锯齿、光标闪烁、时钟走动干扰。
    pHash 是**感知哈希** —— 它忽略微小差异，只看整体结构。
    这正是"界面变了吗"需要的粒度。
    """

    name = "pixel"
    kinds = ("pixel",)

    def __init__(self, *, threshold: int = PIXEL_DIFF_THRESHOLD) -> None:
        self.threshold = int(threshold)

    def verify(self, step: Any, before: Any, after: Any) -> VerifyResult:
        t0 = time.perf_counter()
        b = _shot_of(before)
        a = _shot_of(after)

        if not b or not a:
            return VerifyResult.unknown_(
                method=self.name, target=_target_of(step),
                reason="缺少截图（观测时没开 screenshot）",
                elapsed=time.perf_counter() - t0)

        hb, ha = _phash_of(b), _phash_of(a)
        if hb is None or ha is None:
            return VerifyResult.unknown_(
                method=self.name, target=_target_of(step),
                reason="截图无法计算 pHash",
                elapsed=time.perf_counter() - t0)

        dist = _hamming(hb, ha)
        changed = dist > self.threshold

        ev = {"hash_before": hb, "hash_after": ha, "distance": dist,
              "threshold": self.threshold}
        if changed:
            return VerifyResult.ok_(
                method=self.name, target=_target_of(step),
                reason="界面发生变化（距离 %d > %d）" % (dist, self.threshold),
                evidence=ev, elapsed=time.perf_counter() - t0)
        return VerifyResult.fail(
            method=self.name, target=_target_of(step),
            reason="界面没有变化（距离 %d ≤ %d）" % (dist, self.threshold),
            evidence=ev, elapsed=time.perf_counter() - t0)


# --------------------------------------------------------------------------
# 后端 2：UIA 回读
# --------------------------------------------------------------------------
class UiaStateVerifier(VerifierBackend):
    """UIA 回读元素状态。

    ## 判据

    按 ``step.verify["target"]`` 找元素，回读它的 ``name`` / ``value``，
    与 ``step.verify["expect"]`` 比对：

    - 期望非空 → 回读值包含期望值才算 ok
    - 期望为空 → 能找到元素就算 ok（"元素存在"就是判据）

    ## 找不到元素时返回 unknown 而不是 fail

    因为"找不到"可能只是**定位策略不对**（元素在别处、
    名字变了），不一定是"动作没生效"。当成 fail 会让 recovery
    走重试 —— 而重试用的还是同一个定位策略，还是找不到。
    当成 unknown 至少保留了"换策略"的可能。
    """

    name = "uia"
    kinds = ("uia",)

    def __init__(self, find_fn: Any = None) -> None:
        #: 可注入的查找函数 ``fn(name) -> element|None``。
        #:
        #: **为什么需要注入**：真实查找是全树遍历，一次几十秒。
        #: 自检里跑真实查找会让"验证器自检"变成"UIA 性能测试"。
        #: 注入一个假的就能把本后端的**判据逻辑**单独测干净。
        self._find = find_fn

    def _find_one(self, name: str) -> Any:
        if self._find is not None:
            return self._find(name)
        from ..uia import find as F
        from ..uia.find import Query
        return F.find_one(Query(name=name), required=False)

    def verify(self, step: Any, before: Any, after: Any) -> VerifyResult:
        t0 = time.perf_counter()
        target = str((step.verify or {}).get("target", "")
                     or _target_of(step))
        expect = str((step.verify or {}).get("expect", "") or "")

        if not target and not expect:
            return VerifyResult.unknown_(
                method=self.name, reason="没有可回读的目标",
                elapsed=time.perf_counter() - t0)

        try:
            el = self._find_one(target)
        except Exception as exc:      # noqa: BLE001
            return VerifyResult.unknown_(
                method=self.name, target=target,
                reason="UIA 不可用：%s" % type(exc).__name__,
                elapsed=time.perf_counter() - t0)

        if el is None:
            return VerifyResult.unknown_(
                method=self.name, target=target,
                reason="回读时找不到元素「%s」" % target,
                elapsed=time.perf_counter() - t0)

        got = _read_value(el)
        ev = {"target": target, "read": got, "expect": expect}

        if not expect:
            return VerifyResult.ok_(
                method=self.name, target=target,
                reason="元素存在（无期望值）", evidence=ev,
                elapsed=time.perf_counter() - t0)

        if expect in got:
            return VerifyResult.ok_(
                method=self.name, target=target,
                reason="回读值匹配", evidence=ev,
                elapsed=time.perf_counter() - t0)

        return VerifyResult.fail(
            method=self.name, target=target,
            reason="回读值不匹配：期望「%s」实际「%s」"
                   % (expect[:40], got[:40]),
            evidence=ev, elapsed=time.perf_counter() - t0)


# --------------------------------------------------------------------------
# 后端 3：DOM 回读
# --------------------------------------------------------------------------
class DomVerifier(VerifierBackend):
    """CDP 回读 DOM / URL。

    ## 判据

    ``step.verify["expect"]`` 是一个地址。用 ``after.url`` 与它比对
    （**去 query / fragment / 尾斜杠后比较**）——

    为什么这么比：很多站点每步都改 query（``?t=1699...``），
    严格相等会永远判失败。而"跳到另一个页面"这件事，
    看的是路径而不是 query。
    """

    name = "dom"
    kinds = ("dom",)

    def verify(self, step: Any, before: Any, after: Any) -> VerifyResult:
        t0 = time.perf_counter()
        expect = str((step.verify or {}).get("expect", "")
                     or _target_of(step))
        url = str(getattr(after, "url", "") or "")

        if not url:
            return VerifyResult.unknown_(
                method=self.name, target=expect,
                reason="没有 URL（观测时没有 CDP 会话）",
                elapsed=time.perf_counter() - t0)

        ev = {"url": url, "expect": expect}
        if not expect:
            return VerifyResult.ok_(
                method=self.name, reason="有 URL（无期望值）",
                evidence=ev, elapsed=time.perf_counter() - t0)

        if _url_match(url, expect):
            return VerifyResult.ok_(
                method=self.name, target=expect,
                reason="URL 匹配", evidence=ev,
                elapsed=time.perf_counter() - t0)

        return VerifyResult.fail(
            method=self.name, target=expect,
            reason="URL 不匹配：期望「%s」实际「%s」"
                   % (expect[:60], url[:60]),
            evidence=ev, elapsed=time.perf_counter() - t0)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
#: 轻量判据 —— 直接看观测快照，不需要后端。
_BUILTIN_KINDS = ("window", "window_gone", "elapsed", "file", "screenshot")


def get_verifier(kind: str, *, uia_find: Any = None
                 ) -> Optional[VerifierBackend]:
    """按 ``kind`` 取后端。取不到返回 ``None``（用内置判据）。

    ``uia_find`` 可以给 UIA 后端注入一个查找函数（测试用）——
    真实查找是全树遍历，很慢。
    """
    k = str(kind or "").strip().lower()
    if PixelDiffVerifier().can_handle(k):
        return PixelDiffVerifier()
    if UiaStateVerifier().can_handle(k):
        return UiaStateVerifier(uia_find)
    if DomVerifier().can_handle(k):
        return DomVerifier()
    return None


def verify(step: Any, before: Any, after: Any, *,
           uia_find: Any = None) -> VerifyResult:
    """验证一个步骤。**执行器调的就是这个。永不抛异常。**

    ## 路由

    看 ``step.verify["kind"]``：

    - ``pixel`` → :class:`PixelDiffVerifier`
    - ``uia`` → :class:`UiaStateVerifier`
    - ``dom`` → :class:`DomVerifier`
    - ``window`` / ``window_gone`` / ``elapsed`` / ``file`` → 内置轻量判据
    - **认不出的 kind** → ``unknown``（不是 fail）

    最后一条很重要：一个拼错的 kind（``"pxiel"``）如果被当成
    fail，会让每个步骤都失败；当成 ok 又会让验证形同虚设。
    ``unknown`` 是唯一诚实的回答。

    ``uia_find`` 可以注入假的查找函数（测试用）。
    """
    t0 = time.perf_counter()
    try:
        spec = getattr(step, "verify", None) or {}
        kind = str(spec.get("kind", "") or "").strip().lower()

        if not kind:
            return VerifyResult.unknown_(
                method="verify", reason="步骤没有验证判据",
                elapsed=time.perf_counter() - t0)

        backend = get_verifier(kind, uia_find=uia_find)
        if backend is not None:
            return backend.verify(step, before, after)

        if kind in _BUILTIN_KINDS:
            return _verify_builtin(kind, step, before, after, t0)

        return VerifyResult.unknown_(
            method="verify", reason="未知的判据类型：%r" % kind,
            elapsed=time.perf_counter() - t0)
    except Exception as exc:      # noqa: BLE001
        # 验证器自己坏了 —— unknown，不能是 fail（会误触发重试）
        return VerifyResult.unknown_(
            method="verify", reason="验证器异常：%s: %s"
                                    % (type(exc).__name__, str(exc)[:100]),
            elapsed=time.perf_counter() - t0)


# --------------------------------------------------------------------------
# 内置轻量判据
# --------------------------------------------------------------------------
def _verify_builtin(kind: str, step: Any, before: Any, after: Any,
                    t0: float) -> VerifyResult:
    """看观测快照就能判断的几种。"""
    expect = str((step.verify or {}).get("expect", "")
                 or _target_of(step))
    el = time.perf_counter() - t0

    if kind == "window":
        # 窗口出现了（或标题匹配）
        title = str(getattr(after, "title", "") or "")
        ok = bool(getattr(after, "ok", False))
        if not title and not ok:
            return VerifyResult.unknown_(
                method="window", target=expect,
                reason="没有观测到窗口", elapsed=el)
        if not expect or expect.lower() in title.lower():
            return VerifyResult.ok_(
                method="window", target=expect, reason="窗口存在",
                evidence={"title": title}, elapsed=el)
        return VerifyResult.fail(
            method="window", target=expect,
            reason="窗口标题不匹配：期望「%s」实际「%s」"
                   % (expect[:40], title[:40]),
            evidence={"title": title}, elapsed=el)

    if kind == "window_gone":
        b_id = str(getattr(before, "identity", "") or "")
        a_id = str(getattr(after, "identity", "") or "")
        b_hwnd = int(getattr(before, "hwnd", 0) or 0)
        a_hwnd = int(getattr(after, "hwnd", 0) or 0)
        gone = (b_hwnd and b_hwnd != a_hwnd) or (b_id and b_id != a_id) \
            or not getattr(after, "ok", False)
        if gone:
            return VerifyResult.ok_(
                method="window_gone", target=expect, reason="窗口已消失",
                evidence={"before": b_id, "after": a_id}, elapsed=el)
        return VerifyResult.fail(
            method="window_gone", target=expect, reason="窗口还在",
            evidence={"before": b_id, "after": a_id}, elapsed=el)

    if kind == "elapsed":
        # 等待类：只要执行完了就算成功（等待本身没有"失败"）
        return VerifyResult.ok_(
            method="elapsed", reason="等待已完成", elapsed=el)

    if kind in ("file", "screenshot"):
        path = str(getattr(after, "screenshot_path", "") or "")
        if not path:
            return VerifyResult.unknown_(
                method=kind, reason="没有截图路径", elapsed=el)
        try:
            size = os.path.getsize(path)
        except OSError:
            return VerifyResult.fail(
                method=kind, reason="文件不存在：%s" % path[:60], elapsed=el)
        if size > 0:
            return VerifyResult.ok_(
                method=kind, reason="文件已落盘（%d 字节）" % size,
                evidence={"path": path, "size": size}, elapsed=el)
        return VerifyResult.fail(
            method=kind, reason="文件是空的", elapsed=el)

    return VerifyResult.unknown_(method=kind, reason="未实现的判据", elapsed=el)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _target_of(step: Any) -> str:
    params = getattr(step, "params", None) or {}
    return str(params.get("target", "") or "")


def _shot_of(obs: Any) -> Any:
    """从观测里取截图对象（路径或 Bitmap）。

    **只有路径时返回路径** —— 本模块不负责把 BMP 读回内存，
    那是 :mod:`vision.capture` 的事。
    """
    if obs is None:
        return None
    p = str(getattr(obs, "screenshot_path", "") or "")
    return p or None


def _phash_of(shot: Any) -> Optional[int]:
    """算 pHash。**算不出来返回 None**（调用方转 unknown）。"""
    try:
        from ..verify.result import snapshot_hash
        got = snapshot_hash(shot)
        return int(got) if got is not None else None
    except Exception:      # noqa: BLE001
        return None


def _hamming(a: int, b: int) -> int:
    """两个哈希的汉明距离。"""
    try:
        return int(bin(int(a) ^ int(b)).count("1"))
    except Exception:      # noqa: BLE001
        return 999


def _read_value(el: Any) -> str:
    """从 UIA 元素里读一个"能代表它状态"的字符串。"""
    for attr in ("value", "name", "text"):
        v = getattr(el, attr, None)
        if isinstance(v, str) and v:
            return v
    for meth in ("get_value", "read_value"):
        fn = getattr(el, meth, None)
        if callable(fn):
            try:
                v = fn()
                if isinstance(v, str):
                    return v
            except Exception:      # noqa: BLE001
                continue
    return ""


def _url_match(got: str, expect: str) -> bool:
    """URL 比较：去 fragment / query / 尾斜杠、小写后比。"""
    def _norm(u: str) -> str:
        s = str(u or "").strip().lower()
        s = s.split("#", 1)[0]
        s = s.split("?", 1)[0]
        for pre in ("https://", "http://"):
            if s.startswith(pre):
                s = s[len(pre):]
                break
        return s.rstrip("/")
    a, b = _norm(got), _norm(expect)
    if not a or not b:
        return False
    return a == b or a.endswith(b) or b.endswith(a)


def describe_verifier() -> str:
    """给人看的验证层说明。"""
    lines = [
        "步骤验证（verifier）",
        "=" * 60,
        "三个后端（对应 step.verify['kind']）：",
    ]
    for cls in (PixelDiffVerifier, UiaStateVerifier, DomVerifier):
        v = cls()
        lines.append("  %-8s %s" % (v.name, type(v).__name__))
        lines.append("           %s" % (v.__doc__ or "").strip().split(
            "\n")[0])

    lines += [
        "",
        "内置轻量判据（不需要后端）：",
        "  window / window_gone / elapsed / file / screenshot",
        "",
        "三态语义（复用阶段四契约）：",
        "  ok      —— 查了，成了",
        "  fail    —— 查了，没成        → 该重试",
        "  unknown —— 没查成（缺截图/缺会话/元素找不到）",
        "             **不能当成功放行，也不等于 fail**",
        "",
        "像素判据阈值：汉明距离 > %d 算「有变化」" % PIXEL_DIFF_THRESHOLD,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """verifier 自检。**用假观测对象，不碰真实桌面。**"""
    import shutil
    import sys as _sys
    import tempfile

    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]
    from .observer import Observation
    from .plan import Step

    tmp = tempfile.mkdtemp(prefix="avatar_ver_")

    def _step(kind, **spec):
        s = Step(id="s1", action="click", params={"target": "x"})
        s.verify = dict(kind=kind, **spec)
        return s

    try:
        # ============ 1) 契约 ============
        _add("阈值是正数", _me.PIXEL_DIFF_THRESHOLD > 0,
             str(_me.PIXEL_DIFF_THRESHOLD))
        _add("模块 docstring 含「能力边界」",
             "能力边界" in (_me.__doc__ or ""), "有")
        _add("★ 三个后端都直接继承 VerifierBackend（不共享实现）",
             all(cls.__bases__ == (_me.VerifierBackend,)
                 for cls in (_me.PixelDiffVerifier, _me.UiaStateVerifier,
                             _me.DomVerifier)),
             str([c.__name__ for c in (_me.PixelDiffVerifier,
                                       _me.UiaStateVerifier,
                                       _me.DomVerifier)]))
        _add("★ 抽象类不能直接实例化",
             _raise_type(_me.VerifierBackend) == "TypeError", "TypeError")
        _add("后端 name 互不相同",
             len({c.name for c in (_me.PixelDiffVerifier,
                                   _me.UiaStateVerifier,
                                   _me.DomVerifier)}) == 3, "3 个")

        # ============ 2) 路由 ============
        _add("pixel → PixelDiffVerifier",
             isinstance(_me.get_verifier("pixel"),
                        _me.PixelDiffVerifier), "对")
        _add("uia → UiaStateVerifier",
             isinstance(_me.get_verifier("uia"),
                        _me.UiaStateVerifier), "对")
        _add("dom → DomVerifier",
             isinstance(_me.get_verifier("dom"), _me.DomVerifier), "对")
        _add("★ 未知 kind 返回 None（走内置判据）",
             _me.get_verifier("乱写") is None, "None")

        # ============ 3) 没有判据 → unknown ============
        bare = Step(id="s1", action="click")
        r = _me.verify(bare, None, None)
        _add("★ 没有 verify 判据 → unknown（不是 fail）",
             r.verdict == "unknown", r.describe())

        r = _me.verify(_step("乱写的kind"), None, None)
        _add("★★ 未知判据类型 → unknown（不是 fail 也不是 ok）",
             r.verdict == "unknown", r.describe())

        # ============ 4) 内置判据：window ============
        after_ok = Observation(ok=True, title="记事本 - 未命名")
        r = _me.verify(_step("window", expect="记事本"), None, after_ok)
        _add("window：标题匹配 → ok", r.verdict == "ok", r.describe())

        r = _me.verify(_step("window", expect="计算器"), None, after_ok)
        _add("window：标题不匹配 → fail", r.verdict == "fail", r.describe())

        r = _me.verify(_step("window", expect="记事本"), None,
                       Observation(ok=False))
        _add("★ window：没有观测到窗口 → unknown（不是 fail）",
             r.verdict == "unknown", r.describe())

        bare_win = Step(id="s1", action="click", params={})
        bare_win.verify = {"kind": "window"}
        r = _me.verify(bare_win, None, after_ok)
        _add("window：无期望值时只要窗口存在就 ok",
             r.verdict == "ok", r.describe())

        # ============ 5) 内置判据：window_gone ============
        b = Observation(ok=True, hwnd=100, title="记事本")
        a = Observation(ok=True, hwnd=200, title="桌面")
        r = _me.verify(_step("window_gone"), b, a)
        _add("window_gone：hwnd 变了 → ok", r.verdict == "ok", r.describe())

        r = _me.verify(_step("window_gone"), b,
                       Observation(ok=True, hwnd=100, title="记事本"))
        _add("window_gone：窗口还在 → fail", r.verdict == "fail", r.describe())

        r = _me.verify(_step("window_gone"), b, Observation(ok=False))
        _add("window_gone：观测不到 → ok（窗口确实没了）",
             r.verdict == "ok", r.describe())

        # ============ 6) 内置判据：elapsed ============
        r = _me.verify(_step("elapsed"), None, None)
        _add("★ elapsed：等待类恒 ok（等待没有「失败」）",
             r.verdict == "ok", r.describe())

        # ============ 7) 内置判据：file ============
        p = os.path.join(tmp, "shot.bmp")
        with open(p, "wb") as f:
            f.write(b"\x00" * 100)
        r = _me.verify(_step("file"), None,
                       Observation(ok=True, screenshot_path=p))
        _add("file：文件存在且非空 → ok", r.verdict == "ok", r.describe())

        r = _me.verify(_step("file"), None,
                       Observation(ok=True, screenshot_path=""))
        _add("★ file：没有路径 → unknown", r.verdict == "unknown",
             r.describe())

        r = _me.verify(_step("file"), None,
                       Observation(ok=True,
                                   screenshot_path=os.path.join(tmp, "nope")))
        _add("file：文件不存在 → fail", r.verdict == "fail", r.describe())

        empty = os.path.join(tmp, "empty.bmp")
        open(empty, "wb").close()
        r = _me.verify(_step("file"), None,
                       Observation(ok=True, screenshot_path=empty))
        _add("file：文件是空的 → fail", r.verdict == "fail", r.describe())

        # ============ 8) 像素后端 ============
        r = _me.verify(_step("pixel"), None, None)
        _add("★ pixel：没有截图 → unknown（不是 fail）",
             r.verdict == "unknown", r.describe())

        r = _me.verify(_step("pixel"),
                       Observation(ok=True, screenshot_path=""),
                       Observation(ok=True, screenshot_path=""))
        _add("pixel：路径为空 → unknown", r.verdict == "unknown",
             r.describe())

        # 用假哈希直接测后端逻辑
        v = _me.PixelDiffVerifier(threshold=3)
        _add("★ 汉明距离计算正确",
             _me._hamming(0b1011, 0b1001) == 1
             and _me._hamming(0, 0) == 0
             and _me._hamming(0b1111, 0) == 4, "对")
        _add("threshold 可配置",
             _me.PixelDiffVerifier(threshold=99).threshold == 99, "99")

        # ============ 9) DOM 后端 ============
        r = _me.verify(_step("dom", expect="https://a.com/x"), None,
                       Observation(ok=True))
        _add("★ dom：没有 URL → unknown", r.verdict == "unknown",
             r.describe())

        r = _me.verify(_step("dom", expect="https://a.com/x"), None,
                       Observation(ok=True, url="https://a.com/x"))
        _add("dom：URL 匹配 → ok", r.verdict == "ok", r.describe())

        r = _me.verify(_step("dom", expect="https://a.com/x"), None,
                       Observation(ok=True, url="https://b.com/y"))
        _add("dom：URL 不匹配 → fail", r.verdict == "fail", r.describe())

        _add("★★ URL 比较忽略 query（否则每步都失败）",
             _me._url_match("https://a.com/x?t=123", "https://a.com/x"),
             "忽略 query")
        _add("★★ URL 比较忽略 fragment",
             _me._url_match("https://a.com/x#top", "https://a.com/x"),
             "忽略 fragment")
        _add("★ URL 比较忽略尾斜杠",
             _me._url_match("https://a.com/x/", "https://a.com/x"), "忽略 /")
        _add("★ URL 比较忽略 scheme",
             _me._url_match("http://a.com/x", "https://a.com/x"), "忽略")
        _add("★ URL 比较忽略大小写",
             _me._url_match("https://A.COM/X", "https://a.com/x"), "忽略")
        _add("URL 明显不同时返回 False",
             _me._url_match("https://a.com/x", "https://b.com/y") is False,
             "False")
        _add("空 URL 返回 False（不抛）",
             _me._url_match("", "https://a.com") is False, "False")

        # ============ 10) UIA 后端 ============
        # ⚠ 注入假查找函数 —— 真实 UIA 查找是全树遍历，一次几十秒
        _no_find = lambda name: None
        _s_bare = Step(id="s1", action="click", params={})
        _s_bare.verify = {"kind": "uia"}
        r = _me.verify(_s_bare, None, None, uia_find=_no_find)
        _add("★ uia：没有目标 → unknown", r.verdict == "unknown",
             r.describe())

        r = _me.verify(_step("uia", target="不存在的元素xyz"),
                       Observation(ok=True), Observation(ok=True),
                       uia_find=_no_find)
        _add("★★ uia：找不到元素 → unknown（不是 fail）",
             r.verdict == "unknown", r.describe())

        # 找得到元素时的两种结论
        class _El:
            name = "保存按钮"
            value = "已保存"

        _yes_find = lambda name: _El()

        r = _me.verify(_step("uia", target="保存按钮", expect="已保存"),
                       None, None, uia_find=_yes_find)
        _add("★ uia：回读值匹配 → ok", r.verdict == "ok", r.describe())

        r = _me.verify(_step("uia", target="保存按钮", expect="未保存"),
                       None, None, uia_find=_yes_find)
        _add("★ uia：回读值不匹配 → fail", r.verdict == "fail",
             r.describe())

        def _boom_find(name):
            raise RuntimeError("UIA 挂了")

        r = _me.verify(_step("uia", target="x"), None, None,
                       uia_find=_boom_find)
        _add("★ uia：查找抛异常 → unknown（不崩）",
             r.verdict == "unknown", r.describe())

        _add("_read_value 对空对象返回空串",
             _me._read_value(object()) == "", "空")
        _add("_read_value 优先读 value",
             _me._read_value(type("E", (), {"value": "V", "name": "N"})())
             == "V", "V")

        # ============ 11) 三态互斥（复用阶段四契约）============
        verdicts = set()
        for kind in ("window", "window_gone", "elapsed", "file",
                     "pixel", "uia", "dom", "乱写"):
            rr = _me.verify(_step(kind), None, None, uia_find=_no_find)
            verdicts.add(rr.verdict)
        _add("★ 所有结论都在三态集合里",
             verdicts <= {"ok", "fail", "unknown"}, str(sorted(verdicts)))
        _add("★ 返回的是阶段四的 VerifyResult",
             isinstance(_me.verify(_step("elapsed"), None, None),
                        VerifyResult), "对")

        # ============ 12) 异常不外泄 ============
        class _Boom:
            @property
            def verify(self):
                raise RuntimeError("步骤对象坏了")

        r = _me.verify(_Boom(), None, None)
        _add("★★ 步骤对象坏了 → unknown（不抛）",
             r.verdict == "unknown", r.describe())

        r = _me.verify(None, None, None)
        _add("★ verify(None) → unknown（不抛）",
             r.verdict == "unknown", r.describe())

        # ============ 13) 证据可读 ============
        r = _me.verify(_step("dom", expect="https://a.com/x"), None,
                       Observation(ok=True, url="https://a.com/x"))
        _add("★ 成功时带 evidence（可复盘）",
             bool(r.evidence), str(r.evidence))
        r = _me.verify(_step("dom", expect="https://a.com/x"), None,
                       Observation(ok=True, url="https://b.com/y"))
        _add("★ 失败时带 evidence",
             bool(r.evidence) and "url" in r.evidence, str(r.evidence))
        _add("结论可序列化",
             isinstance(r.to_dict(), dict), "dict")

        # ============ 14) describe ============
        _add("describe_verifier 不抛",
             isinstance(_me.describe_verifier(), str), "有")
        _add("describe 列出三个后端",
             all(k in _me.describe_verifier()
                 for k in ("pixel", "uia", "dom")), "有")
        _add("★ describe 说明 unknown 不等于 fail",
             "unknown" in _me.describe_verifier(), "有")

        # ============ 15) 不碰外部 ============
        import ast as _ast
        src = open(_me.__file__, encoding="utf-8").read()
        tree = _ast.parse(src)
        top = set()
        for node in tree.body:
            if isinstance(node, _ast.Import):
                for a in node.names:
                    top.add((a.name or "").split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                top.add((node.module or "").split(".")[0])
        _add("★ 顶层不 import 执行/恢复（单向依赖）",
             not (top & {"executor", "recovery", "planner", "memory"}),
             sorted(top))
        _add("顶层不 import 图像库（pHash 复用阶段四）",
             not (top & {"PIL", "cv2", "numpy"}), sorted(top))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    return {
        "module": "verifier",
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
    print(describe_verifier())
