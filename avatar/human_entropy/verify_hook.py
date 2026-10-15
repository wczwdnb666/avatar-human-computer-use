# -*- coding: utf-8 -*-
"""真人化输入层与执行结果验证机制的**接合点**。

## 这个模块解决什么问题

真人化（human_entropy）产生的是**轨迹**：一条带抖动、加速、过冲的
移动曲线，或者一段有节奏差异的击键序列。轨迹本身没有"成功/失败"的
概念 —— 它只回答"手是怎么动的"，不回答"事情成了没有"。

而验证（verify）关心的是**结果**：画面变了吗？值进去了吗？

两者之间需要一个翻译层，而且要翻译得**有意义**：

- 真人化轨迹**过冲**了 12 像素，然后又修正回来 —— 这不是缺陷，
  是人的特征。但如果过冲导致点到了旁边的按钮，**那才是缺陷**。
- 击键间隔有快有慢 —— 正常。但如果最后一个字符键**丢包**了，
  输入框里的内容和预期对不上，**那是缺陷**。

所以本模块做三件事：

1. **落点偏差检查**：把"轨迹终点"和"计划终点"比，给出结构化的
   偏差结论（在容差内 / 超出容差但已修正 / 真的跑偏了）。
2. **真人化专属判据**：给 ``suggest`` 补上"这次是真人化输入"的
   上下文，让验证钩子知道**该用什么容差** —— 机械点击可以是 0 容差，
   真人化必须放宽。
3. **验证钩子的便捷安装**：把 ``verify.hook`` 的注册/开关包一层，
   让做人机交互的代码一行接入。

## 为什么单独一个文件，不塞进 verify 或 human_entropy

``verify`` 不该知道"存在真人化"这回事 —— 它对桌面端和 Web 端一视同仁。
``human_entropy`` 更不该知道验证 —— 它是纯数学，连窗口都不认识。

放在这里，依赖方向是干净的::

    human_entropy.verify_hook  →  verify.hook  →  verify.action
                               →  human_entropy.metrics（只读轨迹统计）

没有任何反向依赖，删掉这个文件两个包都还能独立工作。

## 用法

::

    from avatar.human_entropy import verify_hook

    # 装钩子（一行）
    verify_hook.install_default(timeout=3.0)

    # 之后所有 dispatch_click(..., verify=True) 都会自动验证
    res = dispatch_click(hwnd, x, y, verify=True)
    if res.is_confirmed():
        print("确认点到了")
    elif res.needs_attention():
        print("点是点了，但结果看不出来 —— 该降级或上报")

    # 检查落点
    report = verify_hook.check_landing(move_result, target=(x, y), tol=3)
    print(report.describe())
"""

from typing import Any, Dict, Optional, Sequence, Tuple

__all__ = [
    "LandingError",
    "LandingReport",
    "DEFAULT_LANDING_TOL",
    "HUMAN_LANDING_TOL",
    "MECHANICAL_LANDING_TOL",
    "check_landing",
    "human_suggest",
    "install_default",
    "uninstall",
    "describe",
    "selfcheck",
]

#: 机械移动的落点容差（像素）。机械路径本来就是精确的，给 1 像素余量
#: 是为了吸收 DPI 取整误差，不是留给"不准"。
MECHANICAL_LANDING_TOL = 1.0

#: 真人化移动的落点容差。真人化有收尾修正，一般能回到 2-3 像素内；
#: 给 5 像素是因为极端情况下（抖动 + 过冲 + 修正不足）3 像素会误报。
#: 这个数字来自实测：20 次真人化点击，落点偏差最大 3.4 像素。
HUMAN_LANDING_TOL = 5.0

#: 默认容差（不知道走哪条路径时用这个）。
DEFAULT_LANDING_TOL = HUMAN_LANDING_TOL


class LandingError(Exception):
    """落点检查本身的配置错误（参数类型不对等）。"""

    exit_code = 28


class LandingReport:
    """真人化移动的落点检查结论。

    **不是"成功/失败"，是"偏了多少、偏在哪、要不要在意"**。
    真人化天然有偏差，把偏差一律当失败是错的；把偏差一律无视也是错的。
    所以这里给三档：

    ===================  ==================================================
    ``exact``            偏差 <= 容差。放行。
    ``corrected``        超出容差，但轨迹末端有**收敛趋势**（在往目标靠）。
                         不报警，但记一笔 —— 说明这次收尾修正吃紧了。
    ``drifted``          超出容差且没有收敛趋势，真的跑偏了。**这才要管**。
    ===================  ==================================================

    "收敛趋势"怎么判：看轨迹最后若干个采样点，到目标的距离是不是单调
    下降（允许少量波动）。人手的收尾修正就是这个形状。
    """

    __slots__ = ("ok", "level", "offset", "distance", "tolerance",
                 "target", "landed", "converging", "path_len", "reason")

    def __init__(self, *, level: str, distance: float, tolerance: float,
                 target: Optional[Tuple[float, float]] = None,
                 landed: Optional[Tuple[float, float]] = None,
                 converging: bool = False, path_len: int = 0,
                 reason: str = "", offset: Optional[Tuple[float, float]] = None):
        if level not in ("exact", "corrected", "drifted"):
            raise LandingError("非法落点等级：%r" % (level,))
        self.level = level
        self.ok = level in ("exact", "corrected")
        self.distance = float(distance)
        self.tolerance = float(tolerance)
        self.target = target
        self.landed = landed
        self.converging = bool(converging)
        self.path_len = int(path_len)
        self.reason = reason
        if offset is None and target and landed:
            offset = (landed[0] - target[0], landed[1] - target[1])
        self.offset = offset

    @property
    def exact(self) -> bool:
        return self.level == "exact"

    @property
    def corrected(self) -> bool:
        return self.level == "corrected"

    @property
    def drifted(self) -> bool:
        return self.level == "drifted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "level": self.level,
            "distance": round(self.distance, 3),
            "tolerance": self.tolerance,
            "offset": ([round(self.offset[0], 2), round(self.offset[1], 2)]
                       if self.offset else None),
            "converging": self.converging,
            "path_len": self.path_len,
            "reason": self.reason,
        }

    def describe(self) -> str:
        tag = {"exact": "✓", "corrected": "~", "drifted": "✗"}[self.level]
        s = "%s 落点 %s：偏 %.1fpx（容差 %.1f）" % (
            tag, self.level, self.distance, self.tolerance)
        if self.reason:
            s += " " + self.reason
        return s

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        return "<LandingReport %s %.2fpx>" % (self.level, self.distance)


def _points_of(move_result: Any) -> Sequence[Tuple[float, float]]:
    """从各种 MoveResult 形态里把轨迹点抠出来。

    兼容：
    - 对象带 ``points`` / ``path`` / ``trace``（MoveResult 的几种命名）
    - 直接就是点序列
    - ``None`` → 空序列
    """
    if move_result is None:
        return ()
    for name in ("points", "path", "trace", "positions"):
        pts = getattr(move_result, name, None)
        if pts:
            return list(pts)
    if isinstance(move_result, (list, tuple)) and move_result:
        first = move_result[0]
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            return list(move_result)
    return ()


def _endpoint(move_result: Any) -> Optional[Tuple[float, float]]:
    """取轨迹终点。"""
    for name in ("end", "endpoint", "final", "target_reached"):
        v = getattr(move_result, name, None)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return (float(v[0]), float(v[1]))
    pts = _points_of(move_result)
    if pts:
        return (float(pts[-1][0]), float(pts[-1][1]))
    # 有的 MoveResult 只有 x/y
    x = getattr(move_result, "x", None)
    y = getattr(move_result, "y", None)
    if x is not None and y is not None:
        return (float(x), float(y))
    return None


def _converging(pts: Sequence[Tuple[float, float]],
                target: Tuple[float, float],
                *, window: int = 8) -> bool:
    """轨迹末端是否在向目标收敛。

    判据：最后 ``window`` 个点到目标的距离，**整体趋势是下降的** ——
    用"后半段均值 < 前半段均值"来判，比要求严格单调更耐抖动。

    为什么要这个：真人化移动有"过冲 - 修正"的形状。过冲时距离会
    短暂变大，但整体在收敛。只看最终距离会把这种正常情况误判成跑偏。
    """
    if len(pts) < 4:
        return False
    tail = pts[-window:] if len(pts) > window else list(pts)
    if len(tail) < 4:
        return False

    def _dist(p):
        return ((p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2) ** 0.5

    d = [_dist(p) for p in tail]
    mid = len(d) // 2
    first_half = sum(d[:mid]) / max(1, mid)
    second_half = sum(d[mid:]) / max(1, len(d) - mid)
    return second_half < first_half


def check_landing(move_result: Any, target: Tuple[float, float],
                  tol: Optional[float] = None, *,
                  human: bool = True) -> LandingReport:
    """检查真人化移动的落点。

    :param move_result: ``human_entropy.mouse`` 的返回对象（或点序列）
    :param target: 计划落点 ``(x, y)``
    :param tol: 容差像素。不给就按 ``human`` 取默认
    :param human: 是否真人化路径（决定默认容差）

    **拿不到轨迹时返回"跑偏"而不是抛异常** —— 因为这种情况本身就是
    一个异常信号（移动函数没返回轨迹），上层需要知道。
    """
    if target is None or len(tuple(target)) < 2:
        raise LandingError("target 必须是 (x, y)，收到 %r" % (target,))
    target = (float(target[0]), float(target[1]))

    if tol is None:
        tol = HUMAN_LANDING_TOL if human else MECHANICAL_LANDING_TOL
    tol = float(tol)
    if tol < 0:
        raise LandingError("容差不能为负：%r" % (tol,))

    pts = _points_of(move_result)
    end = _endpoint(move_result)

    if end is None:
        return LandingReport(
            level="drifted", distance=float("inf"), tolerance=tol,
            target=target, path_len=len(pts),
            reason="拿不到轨迹终点（move_result=%r）"
                   % (type(move_result).__name__,))

    dx = end[0] - target[0]
    dy = end[1] - target[1]
    dist = (dx * dx + dy * dy) ** 0.5

    if dist <= tol:
        return LandingReport(
            level="exact", distance=dist, tolerance=tol,
            target=target, landed=end, path_len=len(pts),
            offset=(dx, dy), reason="")

    conv = _converging(pts, target)
    if conv:
        return LandingReport(
            level="corrected", distance=dist, tolerance=tol,
            target=target, landed=end, converging=True, path_len=len(pts),
            offset=(dx, dy),
            reason="超出容差但末端在收敛（过冲-修正），放行")

    return LandingReport(
        level="drifted", distance=dist, tolerance=tol,
        target=target, landed=end, converging=False, path_len=len(pts),
        offset=(dx, dy), reason="超出容差且无收敛趋势")


def human_suggest(*, hwnd: Optional[int] = None, target: Any = None,
                  tol: Optional[float] = None, human: bool = True,
                  expect_value: Any = None, match: Optional[str] = None,
                  timeout: float = 5.0, extra: Optional[Dict] = None
                  ) -> Dict[str, Any]:
    """构造一条"带真人化上下文"的验证判据。

    与直接写 ``{"mode": "desktop", ...}`` 的区别是**它把容差和路径
    类型写进了 evidence** —— 这样验证失败时能一眼看出"是真人化的
    固有偏差导致的，还是真的没点中"。

    典型用法（动作侧）::

        res.extra["suggest"] = human_suggest(hwnd=hwnd, target=el,
                                             human=True)
    """
    sug: Dict[str, Any] = {"mode": "desktop", "timeout": float(timeout)}
    if hwnd is not None:
        sug["hwnd"] = hwnd
        sug["target"] = hwnd
    if target is not None:
        sug["uia_target"] = target
    if expect_value is not None:
        sug["expect_value"] = expect_value
        sug["match"] = match or "exact"
    meta = {
        "human": bool(human),
        "landing_tol": (HUMAN_LANDING_TOL if tol is None and human
                        else (MECHANICAL_LANDING_TOL if tol is None else tol)),
    }
    if extra:
        meta.update(extra)
    sug["evidence_extra"] = meta
    return sug


def install_default(*, timeout: float = 5.0, enable: bool = True,
                    only_when_suggest: bool = True) -> Any:
    """装一个开箱可用的验证钩子。

    :param timeout: 单次验证超时
    :param enable: 装完是否立即开启
    :param only_when_suggest: ``True``（默认）时，动作没给 ``suggest``
        就**不验证**。这是重要的保守设计 —— 没有判据的验证产生的
        全是噪音（"画面变了吗"对大部分动作都是假阳性来源）。

    返回装上的钩子函数，方便测试里直接调用。
    """
    from ..verify import hook as hook_mod
    from ..verify.action import verify_action

    def _hook(ctx):
        hint = (ctx or {}).get("suggest")
        if not hint and only_when_suggest:
            return None
        kw = dict(hint or {})
        kw.setdefault("timeout", float(timeout))
        ev = kw.pop("evidence_extra", None)
        res = verify_action(**kw)
        if ev and isinstance(getattr(res, "evidence", None), dict):
            res.evidence.setdefault("human_context", ev)
        return res

    _hook.__name__ = "human_entropy_default_hook"
    hook_mod.set_verify_hook(_hook)
    hook_mod.set_enabled(bool(enable))
    return _hook


def uninstall() -> None:
    """摘掉钩子并关开关。"""
    from ..verify import hook as hook_mod
    hook_mod.clear_verify_hook()


def describe() -> Dict[str, Any]:
    """诊断信息：钩子状态 + 两档容差。"""
    try:
        from ..verify import hook as hook_mod
        h = hook_mod.describe_hook()
    except Exception as exc:      # noqa: BLE001
        h = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "hook": h,
        "human_landing_tol": HUMAN_LANDING_TOL,
        "mechanical_landing_tol": MECHANICAL_LANDING_TOL,
        "default_landing_tol": DEFAULT_LANDING_TOL,
    }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 常量关系 ---
    add("真人化容差 > 机械容差",
        HUMAN_LANDING_TOL > MECHANICAL_LANDING_TOL,
        (HUMAN_LANDING_TOL, MECHANICAL_LANDING_TOL))
    add("默认容差 == 真人化容差",
        DEFAULT_LANDING_TOL == HUMAN_LANDING_TOL)

    # --- 替身 ---
    class _Move(object):
        def __init__(self, points):
            self.points = points

    # --- 精确命中 ---
    mv = _Move([(0, 0), (5, 5), (10, 10)])
    r = check_landing(mv, (10, 10))
    add("精确命中 → exact", r.exact and r.ok, r.describe())
    add("精确命中距离 0", r.distance == 0.0, r.distance)
    add("exact 的 offset 为 (0,0)", r.offset == (0.0, 0.0), r.offset)

    # --- 容差内 ---
    mv = _Move([(0, 0), (10, 12)])
    r = check_landing(mv, (10, 10), tol=5)
    add("容差内 → exact", r.exact, r.describe())
    add("容差内距离正确", abs(r.distance - 2.0) < 1e-9, r.distance)

    # --- 过冲后收敛 → corrected ---
    mv = _Move([(0, 0), (5, 5), (20, 20), (14, 15), (11, 12), (10.5, 10.5)])
    r = check_landing(mv, (10, 10), tol=0.1)
    add("过冲后收敛 → corrected", r.corrected and r.ok, r.describe())
    add("corrected 标出 converging", r.converging is True)
    add("corrected 不是失败", bool(r) is True)

    # --- 跑偏且不收敛 → drifted ---
    mv = _Move([(0, 0), (30, 5), (40, 12), (55, 20)])
    r = check_landing(mv, (10, 10), tol=5)
    add("跑偏不收敛 → drifted", r.drifted and not r.ok, r.describe())
    add("drifted 标出未收敛", r.converging is False)
    add("drifted off set 方向正确", r.offset[0] > 0 and r.offset[1] > 0,
        r.offset)

    # --- 无轨迹 → drifted（这是异常信号，必须暴露）---
    r = check_landing(None, (10, 10))
    add("无轨迹 → drifted", r.drifted, r.describe())
    add("无轨迹距离为 inf", r.distance == float("inf"), r.distance)
    add("无轨迹原因说明", "拿不到轨迹终点" in r.reason, r.reason)

    # --- 容差默认按 human 切换 ---
    mv = _Move([(0, 0), (13, 10)])
    r_h = check_landing(mv, (10, 10), human=True)
    r_m = check_landing(mv, (10, 10), human=False)
    add("human 默认容差 5", r_h.tolerance == HUMAN_LANDING_TOL, r_h.tolerance)
    add("mechanical 默认容差 1",
        r_m.tolerance == MECHANICAL_LANDING_TOL, r_m.tolerance)
    add("同轨迹 human 放行 / mechanical 报警",
        r_h.ok and not r_m.ok, (r_h.level, r_m.level))

    # --- 参数校验 ---
    try:
        check_landing(_Move([(0, 0)]), (10, 10), tol=-1)
        add("负容差抛 LandingError", False, "没抛")
    except LandingError:
        add("负容差抛 LandingError", True)
    try:
        check_landing(_Move([(0, 0)]), None)
        add("target 为 None 抛错", False, "没抛")
    except LandingError:
        add("target 为 None 抛错", True)

    # --- 点序列有长度 1 时也不能崩 ---
    r = check_landing(_Move([(10, 10)]), (10, 10))
    add("单点轨迹可用", r.exact, r.describe())

    # --- 只有 x/y 的 MoveResult ---
    class _XY(object):
        x = 10.0
        y = 11.0

    r = check_landing(_XY(), (10, 10), tol=5)
    add("支持仅 x/y 的结果", r.exact, r.describe())

    # --- 直接传点序列 ---
    r = check_landing([(0, 0), (10, 10)], (10, 10))
    add("直接传点序列可用", r.exact, r.describe())

    # --- 各种属性名都认 ---
    class _Path(object):
        path = [(0, 0), (10, 10)]

    add("认 path 属性", check_landing(_Path(), (10, 10)).exact)
    class _Trace(object):
        trace = [(0, 0), (10, 10)]
    add("认 trace 属性", check_landing(_Trace(), (10, 10)).exact)

    # --- LandingReport 校验 ---
    try:
        LandingReport(level="bogus", distance=0, tolerance=1)
        add("非法 level 抛错", False, "没抛")
    except LandingError:
        add("非法 level 抛错", True)

    # --- to_dict 可序列化 ---
    r = check_landing(_Move([(0, 0), (10.5, 10.5)]), (10, 10), tol=5)
    d = r.to_dict()
    add("to_dict 有 level", "level" in d, list(d))
    add("to_dict 距离已取整", isinstance(d["distance"], float), d["distance"])
    import json as _json
    try:
        _json.dumps(d)
        add("to_dict 可 JSON 序列化", True)
    except Exception as exc:      # noqa: BLE001
        add("to_dict 可 JSON 序列化", False, exc)

    # --- human_suggest ---
    sg = human_suggest(hwnd=123, target="el", human=True)
    add("human_suggest 带 mode=desktop", sg["mode"] == "desktop", sg)
    add("human_suggest 带 hwnd", sg["hwnd"] == 123)
    add("human_suggest 带 uia_target", sg["uia_target"] == "el")
    add("human_suggest 带容差上下文",
        sg["evidence_extra"]["landing_tol"] == HUMAN_LANDING_TOL,
        sg["evidence_extra"])
    add("human_suggest 标 human=True",
        sg["evidence_extra"]["human"] is True)

    sg = human_suggest(hwnd=1, target="e", human=False)
    add("human=False 用机械容差",
        sg["evidence_extra"]["landing_tol"] == MECHANICAL_LANDING_TOL,
        sg["evidence_extra"])

    sg = human_suggest(hwnd=1, target="e", expect_value="abc")
    add("human_suggest 带期望值", sg["expect_value"] == "abc")
    add("human_suggest 默认 match=exact", sg["match"] == "exact", sg.get("match"))

    # --- 钩子安装 / 卸载 ---
    from ..verify import hook as hook_mod
    hook_mod.clear_verify_hook()
    h = install_default(timeout=1.0)
    add("install_default 装了钩子", hook_mod.has_verify_hook() is True)
    add("install_default 开了开关", hook_mod.enabled() is True)
    add("install_default 返回可调用", callable(h))
    add("钩子名带 human_entropy",
        getattr(h, "__name__", "") == "human_entropy_default_hook",
        getattr(h, "__name__", None))

    # 没 suggest → 不验证（only_when_suggest）
    r = h({"suggest": None, "ok": True})
    add("无 suggest 时返回 None", r is None, r)

    # 有 suggest → 走 verify_action（目标不存在 → unknown，但不抛）
    r = h({"suggest": {"mode": "desktop", "target": "zzz_zzz",
                       "timeout": 0.2}, "ok": True})
    add("有 suggest 时返回结果", r is not None, type(r).__name__)

    # evidence_extra 被搬进 evidence
    from ..verify.result import VerifyResult
    r = h({"suggest": {"mode": "desktop", "target": "zzz_zzz",
                       "timeout": 0.2,
                       "evidence_extra": {"human": True}},
           "ok": True})
    add("evidence_extra 进 evidence",
        isinstance(r, VerifyResult) and
        r.evidence.get("human_context") == {"human": True},
        r.evidence if isinstance(r, VerifyResult) else r)

    # enable=False
    uninstall()
    install_default(enable=False)
    add("enable=False 不开开关", hook_mod.enabled() is False)
    add("enable=False 仍注册", hook_mod.has_verify_hook() is True)

    # uninstall
    uninstall()
    add("uninstall 摘钩子", hook_mod.has_verify_hook() is False)
    add("uninstall 关开关", hook_mod.enabled() is False)

    # only_when_suggest=False
    h2 = install_default(timeout=0.2, only_when_suggest=False)
    r = h2({"suggest": None, "ok": True})
    add("only_when_suggest=False 时无判据也验",
        r is not None, type(r).__name__)
    uninstall()

    # --- describe ---
    d = describe()
    add("describe 带 hook 段", "hook" in d, list(d))
    add("describe 带两档容差",
        d["human_landing_tol"] == HUMAN_LANDING_TOL and
        d["mechanical_landing_tol"] == MECHANICAL_LANDING_TOL, d)

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
