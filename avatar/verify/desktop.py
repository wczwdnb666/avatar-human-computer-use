# -*- coding: utf-8 -*-
"""桌面端验证：截图 pHash 比对 + UIA 状态回读。

## 两条判据，各有各的适用场景

**像素判据（pHash）** —— 万能但不精确。

- 优点：**不依赖任何控件信息**。画布、游戏、自绘 UI、DirectX 渲染，
  这些 UIA 完全看不见的东西，像素变化是唯一的证据。
- 缺点：**说不出"变成了什么"**。只知道变了，不知道变得对不对。
  而且如果动作刚好没引起视觉变化（比如点了个透明热区、
  填了个被遮挡的输入框），会把成功判成失败。

**UIA 判据** —— 精确但脆弱。

- 优点：能给出**确切的值**（"值是 '2024' 而不是 '2023'"），
  证据链完整，人能看懂，模型也能看懂。
- 缺点：控件不一定暴露值（密码框、自绘控件、Electron 内部
  全是 `Pane`）；跨进程读值可能慢；控件可能已失效（stale）。

## 所以：两个一起用

:func:`verify_desktop` 是统一入口，按"有没有可用的 UIA 目标"自动选：

===================  ===============================================
给了 UIA 查询         以 UIA 为主判据，像素作为**佐证**（可选）
只给了区域            以像素为主判据
两个都给了            UIA 判定结果可信（拿到确切值）时**采信 UIA**；
                      UIA 判"未知"时**回退对比像素**
===================  ===============================================

## 关于"先截图还是先验证"

**正确顺序：动作前截图 → 动作 → 动作后截图 → 比对。**

本模块的 ``verify_pixel`` 支持两种调用方式：

1. 传 ``before_hash``（推荐）—— 调用方在动作前算好，最省时间。
2. 传 ``before_shot`` —— 本模块现算。

**不支持"只传一个 after 截图就判断有没有变"** —— 那不是验证，
那是猜。没有基线就没有变化，这是硬逻辑。

## 坑

1. **``grab_window`` 抓的是窗口像素**，窗口被遮挡时拿到的是**被遮挡后
   的画面**（GDI 截图）。桌面合成下更可靠的是 ``PrintWindow``，
   但那个在部分硬件加速窗口上返回黑图。所以：**抓之前把窗口置前**，
   或者用 ``grab_screen(region=窗口矩形)`` 抓屏幕对应区域。
   两者在窗口被遮挡时结果不同，本模块优先窗口直抓，失败回退屏幕区域。

2. **空白图（全黑/全白）要当"未知"** —— 硬件加速窗口、
   最小化窗口、权限不足都会返回全黑。全黑图的 pHash 是 0，
   如果基线也是 0 会被判成"没变"（假阴性）；如果基线不是 0
   会被判成"变了"（假阳性）。两种都错。所以先 ``is_blank`` 检查。

3. **UIA 读值要在动作后等一下**。很多控件（尤其 .NET/WPF 自绘）
   的值更新是异步的，动作返回后立刻读会读到旧值。
   默认 ``settle=0.25`` 秒。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AvatarError
from .result import (
    DEFAULT_CHANGED_MIN,
    DEFAULT_UNCHANGED_MAX,
    VerifyResult,
    image_similarity,
    snapshot_hash,
)

__all__ = [
    "DesktopVerifyError",
    "grab_bitmap",
    "bitmap_hash",
    "shot_hash",
    "verify_pixel",
    "verify_region_changed",
    "verify_uia_exists",
    "verify_uia_value",
    "verify_uia_state",
    "verify_window_title",
    "verify_desktop",
    "selfcheck",
]


class DesktopVerifyError(AvatarError):
    """桌面验证过程出错。"""

    exit_code = 23


#: 动作后等待值更新的默认时长（秒）。异步控件需要它。
DEFAULT_SETTLE = 0.25


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------

def grab_bitmap(
    *,
    hwnd: int = 0,
    region: Optional[Tuple[int, int, int, int]] = None,
    client_only: bool = False,
    fallback_screen: bool = True,
) -> Optional[Any]:
    """抓一张 Bitmap。拿不到返回 None（**不抛**）。

    ``hwnd`` 给了就抓窗口；``region`` 给了就抓屏幕区域。
    两个都给时以 hwnd 为主，失败且 ``fallback_screen=True`` 时
    用 region 抓屏幕。
    """
    try:
        from ..vision import capture as C
    except Exception:
        return None

    bmp = None
    if hwnd:
        try:
            bmp = C.grab_window(int(hwnd), client_only=client_only)
        except Exception:
            bmp = None
    if bmp is None and region is not None:
        try:
            bmp = C.grab_screen(tuple(int(v) for v in region))
        except Exception:
            bmp = None
    elif bmp is None and fallback_screen and hwnd:
        # 窗口直抓失败 → 用窗口矩形抓屏幕
        try:
            rect = window_rect(hwnd)
            if rect:
                bmp = C.grab_screen(rect)
        except Exception:
            bmp = None
    return bmp


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """取窗口矩形 ``(left, top, right, bottom)``。失败返回 None。"""
    if not hwnd:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        r = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def bitmap_hash(bmp: Any) -> Optional[int]:
    """对 Bitmap 算 pHash。失败返回 None。"""
    if bmp is None:
        return None
    try:
        from .result import _to_gray, phash
        w = int(getattr(bmp, "width", 0) or 0)
        h = int(getattr(bmp, "height", 0) or 0)
        if w <= 0 or h <= 0:
            return None
        px = getattr(bmp, "pixels", None)
        if not px:
            return None
        # Bitmap.pixels 可能是 bytes（BGRA）或二维序列
        if isinstance(px, (bytes, bytearray)):
            stride = int(getattr(bmp, "stride", 0) or (w * 4))
            bpp = max(1, stride // w) if w else 4
            rows = []  # type: List[List[Any]]
            for y in range(h):
                base = y * stride
                if base + w * bpp > len(px):
                    return None
                row = []
                for x in range(w):
                    i = base + x * bpp
                    if bpp >= 3:
                        b, g, r = px[i], px[i + 1], px[i + 2]
                        row.append((r, g, b))
                    else:
                        row.append(px[i])
                rows.append(row)
            return phash(rows, w, h)
        return phash(px, w, h)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 像素判据
# --------------------------------------------------------------------------

def shot_hash(shot: Any) -> Optional[int]:
    """从**任意**截图对象算 pHash。

    依次尝试三条路：

    1. 是 ``vision.capture.Bitmap``（有 ``pixels``/``width``）→ 直接算。
    2. 有 ``data`` / ``png`` / ``raw`` 字节 → 按 PNG 解析。
    3. 有 ``path`` → 读文件按 PNG 解析。

    都失败返回 ``None``。**不要**让调用方区分"是 Bitmap 还是 PNG 字节" ——
    那是本函数的职责，不是调用方的。
    """
    if shot is None:
        return None
    # 1) Bitmap（阶段二的截图对象）
    if hasattr(shot, "pixels") and hasattr(shot, "width"):
        h = bitmap_hash(shot)
        if h is not None:
            return h
    # 2) / 3) PNG 字节或路径
    return snapshot_hash(shot)


def verify_pixel(
    *,
    hwnd: int = 0,
    region: Optional[Tuple[int, int, int, int]] = None,
    before_hash: Optional[int] = None,
    before_shot: Any = None,
    after_shot: Any = None,
    after_hash: Optional[int] = None,
    threshold: int = DEFAULT_CHANGED_MIN,
    unchanged_max: int = DEFAULT_UNCHANGED_MAX,
    expect_change: bool = True,
    settle: float = 0.0,
    detect_blank: bool = True,
) -> VerifyResult:
    """用像素判断屏幕**是否发生了变化**。

    ``expect_change=True``（默认）：期望变化，变了才成功。
    ``expect_change=False``：期望**不变**（如"点了取消，窗口还在"），
    没变才成功。

    ``before_hash`` / ``before_shot`` 至少要给一个 ——
    没有基线无法判断。都没有则返回**未知**，不是失败。

    ``detect_blank=True`` 时会检查空白图并升级为"未知"（见模块文档）。
    """
    t0 = time.time()
    method = "phash"

    # 基线
    base = before_hash if before_hash is not None else shot_hash(before_shot)
    if base is None:
        return VerifyResult.unknown_(
            method=method, mode="desktop",
            reason="没有基线（before_hash / before_shot 都没给或都算不出）",
            elapsed=time.time() - t0,
            extra={"need_baseline": True})

    if settle > 0:
        time.sleep(settle)

    # 当前
    cur = after_hash
    blank = False
    if cur is None:
        bmp = after_shot if after_shot is not None else grab_bitmap(
            hwnd=hwnd, region=region)
        if bmp is None:
            return VerifyResult.unknown_(
                method=method, mode="desktop", reason="动作后截图失败",
                elapsed=time.time() - t0)
        if detect_blank:
            try:
                from ..vision import capture as C
                blank = bool(C.is_blank(bmp))
            except Exception:
                blank = False
        cur = bitmap_hash(bmp)
        if cur is None:
            return VerifyResult.unknown_(
                method=method, mode="desktop", reason="动作后截图无法计算哈希",
                elapsed=time.time() - t0)

    if detect_blank and blank:
        return VerifyResult.unknown_(
            method=method, mode="desktop",
            reason="动作后截图为空白（窗口被遮挡/最小化/硬件加速）",
            evidence={"blank": True},
            elapsed=time.time() - t0,
            extra={"blank": True})

    # 比对
    from .result import hamming_distance
    dist = hamming_distance(base, cur)
    changed = dist >= threshold
    unchanged = dist <= unchanged_max
    ev = {
        "distance": dist,
        "threshold": threshold,
        "unchanged_max": unchanged_max,
        "similarity": round(image_similarity(base, cur), 4),
        "before": hex(base),
        "after": hex(cur),
        "expect_change": bool(expect_change),
    }

    if expect_change:
        if changed:
            return VerifyResult.ok_(
                method=method, mode="desktop",
                reason="像素变化 %d 位（阈值 %d）" % (dist, threshold),
                evidence=ev, elapsed=time.time() - t0,
                extra={"confidence": min(1.0, 0.6 + dist / 40.0)})
        if unchanged:
            return VerifyResult.fail(
                method=method, mode="desktop",
                reason="期望有变化，但画面几乎没变（距离 %d ≤ %d）"
                       % (dist, unchanged_max),
                evidence=ev, elapsed=time.time() - t0,
                extra={"confidence": 0.85})
        return VerifyResult.unknown_(
            method=method, mode="desktop",
            reason="像素变化 %d 位，落在灰区（%d~%d），无法判定"
                   % (dist, unchanged_max + 1, threshold - 1),
            evidence=ev, elapsed=time.time() - t0, extra={"gray_zone": True})

    # 期望不变
    if unchanged:
        return VerifyResult.ok_(
            method=method, mode="desktop",
            reason="画面保持不变（距离 %d ≤ %d）" % (dist, unchanged_max),
            evidence=ev, elapsed=time.time() - t0,
            extra={"confidence": 0.9})
    if changed:
        return VerifyResult.fail(
            method=method, mode="desktop",
            reason="期望不变，但画面变了 %d 位（阈值 %d）" % (dist, threshold),
            evidence=ev, elapsed=time.time() - t0,
            extra={"confidence": 0.85})
    return VerifyResult.unknown_(
        method=method, mode="desktop",
        reason="像素变化 %d 位，落在灰区，无法判定" % dist,
        evidence=ev, elapsed=time.time() - t0, extra={"gray_zone": True})


def verify_region_changed(
    region: Tuple[int, int, int, int],
    before_hash: int,
    *,
    threshold: int = DEFAULT_CHANGED_MIN,
    unchanged_max: int = DEFAULT_UNCHANGED_MAX,
    settle: float = 0.0,
) -> VerifyResult:
    """抓屏幕指定区域，判断是否变了。区域用 ``(l, t, r, b)``。"""
    return verify_pixel(region=region, before_hash=before_hash,
                        threshold=threshold, unchanged_max=unchanged_max,
                        settle=settle)


# --------------------------------------------------------------------------
# UIA 判据
# --------------------------------------------------------------------------

def _settle(seconds: float) -> None:
    if seconds and seconds > 0:
        time.sleep(seconds)


def _element_of(target: Any, *, name_exact: bool = False,
                control_type: Any = None, window: int = 0) -> Optional[Any]:
    """把 target 规整成 UIA Element。

    接受：Element 本身 / Query 对象 / (name, control_type) 元组 /
    字符串（按名字找）。

    ## 为什么要 ``name_exact`` / ``control_type`` / ``window``

    ``find`` 的通道选路规则是：**条件能下推到 UIA 原生 ``FindAll``
    就走快通道（秒级以内），下推不了就走 Python 全遍历（实测全桌面
    52 秒）**。而"字符串按名字子串找"恰好是**下推不了**的那一类
    —— 于是随手传个字符串就会卡近一分钟。

    给这三个旋钮，是为了让调用方能主动把条件收紧成**可下推**的形状：

    * ``name_exact=True`` → 走 Name 属性等值下推（快）
    * ``control_type="button"`` → 按类型下推（快），再在结果里子串过滤
    * ``window=<hwnd>`` → 把 scope 缩到单个窗口（几十个元素 vs 全桌面几千个）

    三个都不给时保持旧行为（宽松子串 + 全桌面）——向后兼容，
    但慢，且容易误匹配。**验证场景建议至少给 ``window``。**
    """
    if target is None:
        return None
    if hasattr(target, "rect") and hasattr(target, "control_type"):
        return target      # 已经是 Element

    try:
        from ..uia import find as F
    except Exception:
        return None

    def _query(name, ctype=None):
        """构造收紧后的 Query。"""
        kw = {}
        if name_exact:
            kw["name_exact"] = True
        if ctype is not None:
            kw["control_type"] = ctype
        elif control_type is not None:
            kw["control_type"] = control_type
        if window:
            kw["window"] = int(window)
        return F.Query(name, **kw)

    if isinstance(target, str):
        try:
            return F.find_one(_query(target), required=False)
        except Exception:
            return None
    if isinstance(target, tuple) and len(target) == 2:
        name, ctype = target
        try:
            return F.find_one(_query(name, ctype), required=False)
        except Exception:
            return None
    # 当作 Query 用（调用方自己造好了 Query，不再改它的字段）
    try:
        return F.find_one(target, required=False)
    except Exception:
        return None


def verify_uia_exists(
    target: Any,
    *,
    expect: bool = True,
    window: int = 0,
    timeout: float = 2.0,
    poll: float = 0.05,
    name_exact: bool = False,
    control_type: Any = None,
) -> VerifyResult:
    """验证控件**存在 / 不存在**。

    ``expect=False`` 时验证"消失"（比如点了关闭按钮，对话框该没了）。
    ``timeout`` 内有轮询 —— 消失/出现都是异步的。

    ## ⚠ ``timeout`` 只在"查询本身够快"时才兑现

    轮询的外层预算拦不住**单次查询自己就超预算**的情况。
    实测：``find`` 走 Python 全遍历时单次查询 **52.8 秒**，
    此时 ``timeout=2.0`` 完全没有意义 —— 一次尝试都跑不完，
    循环根本没机会判断。

    所以：

    * 查询慢的时候，``evidence["probe_seconds"]`` 会把真实单次耗时
      记下来。**看到这个数字远大于 ``timeout``，就是慢在这**。
    * 想让 ``timeout`` 真的管用，用 ``name_exact`` /
      ``control_type`` / ``window`` 把查询收紧成可下推的（见
      ``_element_of`` 的说明）。
    """
    t0 = time.time()
    eff = target
    # 窗口号要透传给 Query（Query 自己带 window 参数）
    if window and hasattr(eff, "window"):
        try:
            eff = type(eff)(**{**{k: getattr(eff, k) for k in
                                  ("name", "automation_id", "class_name")},
                             "window": int(window)})
        except Exception:
            pass

    deadline = time.time() + max(0.0, timeout)
    slowest = 0.0
    probes = 0
    while True:
        p0 = time.time()
        found = _element_of(eff, name_exact=name_exact,
                            control_type=control_type,
                            window=int(window or 0)) is not None
        probes += 1
        slowest = max(slowest, time.time() - p0)
        if found == bool(expect):
            return VerifyResult.ok_(
                method="uia_exists",
                reason="控件%s" % ("存在" if expect else "已消失"),
                evidence={"expected": bool(expect), "found": found,
                          "probes": probes,
                          "probe_seconds": round(slowest, 3)},
                elapsed=time.time() - t0)
        if time.time() >= deadline:
            break
        time.sleep(max(0.01, poll))

    return VerifyResult.fail(
        method="uia_exists",
        reason="期望控件%s，但%s" % (
            "存在" if expect else "消失",
            "没找到" if expect else "仍然存在"),
        evidence={"expected": bool(expect), "found": not expect,
                  "probes": probes,
                  "probe_seconds": round(slowest, 3),
                  # 单次查询就吃掉整个预算 → 这不是"等不够"，是"查不动"。
                  #  把话说清楚，免得调用方以为是 timeout 设小了。
                  **({"probe_note": "单次查询耗时超过 timeout，"
                       "轮询预算未生效；请用 name_exact/control_type/window "
                       "收紧条件"}
                     if slowest > max(0.0, timeout) and probes <= 1 else {})},
        elapsed=time.time() - t0)


#: UIA 里可能存"值"的属性名，按优先级。
_VALUE_PROPS = ("Value.Value", "Value", "value", "Text", "Name")


def read_uia_value(element: Any) -> Tuple[bool, Any]:
    """从 Element 读值。返回 ``(是否拿到, 值)``。

    "没拿到"和"值是空字符串"是两回事 —— 后者是有效值。
    所以返回值里必须带一个"拿到了吗"的标志。
    """
    if element is None:
        return (False, None)

    # 先试 patterns（RangeValue / Value 等）
    try:
        pats = getattr(element, "patterns", None) or {}
        for key in ("ValuePattern", "RangeValuePattern"):
            p = pats.get(key) if hasattr(pats, "get") else None
            if p is None:
                continue
            for attr in ("value", "Value", "current_value"):
                v = getattr(p, attr, None)
                if v is None:
                    continue
                try:
                    v = v() if callable(v) else v
                except Exception:
                    continue
                if v is not None:
                    return (True, v)
    except Exception:
        pass

    # 再试属性
    for prop in ("value", "Value"):
        try:
            v = getattr(element, prop, None)
        except Exception:
            v = None
        if v is None:
            continue
        try:
            v = v() if callable(v) else v
        except Exception:
            continue
        if v is not None:
            return (True, v)

    # 退到 name（Text 控件常用 name 承载文本）
    nm = getattr(element, "name", None)
    if nm is not None:
        return (True, nm)
    return (False, None)


def verify_uia_value(
    target: Any,
    expected: Any,
    *,
    window: int = 0,
    settle: float = DEFAULT_SETTLE,
    match: str = "exact",
    timeout: float = 2.0,
) -> VerifyResult:
    """验证控件的值。

    ``match``：
    - ``"exact"`` 完全相等（转成字符串比，去首尾空白）
    - ``"contains"`` 包含
    - ``"regex"`` 正则匹配
    - ``"nonempty"`` 只要非空（``expected`` 被忽略）
    - ``"any"`` 只要求读得到值（``expected`` 被忽略）
    """
    t0 = time.time()
    el = _element_of(target)
    if el is None:
        return VerifyResult.unknown_(
            method="uia_value", reason="控件没找到", elapsed=time.time() - t0)

    deadline = time.time() + max(0.0, timeout)
    last_actual = None
    got_any = False

    while True:
        _settle(settle if got_any is False else 0.05)
        ok_read, actual = read_uia_value(el)
        if ok_read:
            got_any = True
            last_actual = actual
            verdict = _match_value(actual, expected, match)
            if verdict is True:
                return VerifyResult.ok_(
                    method="uia_value",
                    reason="值匹配（%s）" % match,
                    evidence={"expected": _safe(expected),
                              "actual": _safe(actual), "match": match},
                    elapsed=time.time() - t0)
            if verdict is False:
                # 再等一会儿，可能只是还没更新
                if time.time() >= deadline:
                    return VerifyResult.fail(
                        method="uia_value",
                        reason="值不匹配：期望 %r，实际 %r"
                               % (_safe(expected), _safe(actual)),
                        evidence={"expected": _safe(expected),
                                  "actual": _safe(actual), "match": match},
                        elapsed=time.time() - t0)
                time.sleep(0.05)
                continue

        if time.time() >= deadline:
            break
        time.sleep(0.05)

    if got_any:
        return VerifyResult.fail(
            method="uia_value",
            reason="值不匹配：期望 %r，实际 %r"
                   % (_safe(expected), _safe(last_actual)),
            evidence={"expected": _safe(expected),
                      "actual": _safe(last_actual), "match": match},
            elapsed=time.time() - t0)
    return VerifyResult.unknown_(
        method="uia_value",
        reason="控件读不到值（不支持 ValuePattern / 密码框 / 自绘控件）",
        elapsed=time.time() - t0,
        extra={"no_readable_value": True})


def _safe(v: Any, limit: int = 300) -> Any:
    try:
        s = v if isinstance(v, (int, float, bool, type(None))) else str(v)
    except Exception:
        return "<unrepr>"
    if isinstance(s, str) and len(s) > limit:
        return s[:limit] + "…"
    return s


def _match_value(actual: Any, expected: Any, match: str) -> Optional[bool]:
    """返回 True/False，``None`` 表示无法判断。"""
    m = (match or "exact").lower()
    if m == "any":
        return True
    if m == "nonempty":
        s = "" if actual is None else str(actual)
        return bool(s.strip())
    a = "" if actual is None else str(actual).strip()
    e = "" if expected is None else str(expected).strip()
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


def verify_uia_state(
    target: Any,
    *,
    enabled: Optional[bool] = None,
    visible: Optional[bool] = None,
    focused: Optional[bool] = None,
    toggled: Optional[bool] = None,
    expanded: Optional[bool] = None,
    settle: float = DEFAULT_SETTLE,
    timeout: float = 2.0,
    poll: float = 0.05,
) -> VerifyResult:
    """验证控件的**属性状态**。

    状态变化也是异步的（点完开关，``IsEnabled`` 可能过几十毫秒才更新），
    所以在 ``timeout`` 内**轮询**直到全部符合或超时。

    ``visible`` 用的是 ``not offscreen`` —— UIA 里没有直接的
    "visible"，``IsOffscreen`` 是唯一可用的近似。滚动出视野的控件
    会被判成不可见，注意这个语义。
    """
    t0 = time.time()
    _settle(settle)

    deadline = time.time() + max(0.0, timeout)
    last_checks = []  # type: List[Tuple[str, bool, Any, None]]
    proberr = None

    while True:
        el = _element_of(target)
        if el is None:
            if time.time() >= deadline:
                return VerifyResult.unknown_(
                    method="uia_state", reason="控件没找到",
                    elapsed=time.time() - t0)
            time.sleep(max(0.01, poll))
            continue

        checks = []  # type: List[Tuple[str, bool, Any, None]]
        if enabled is not None:
            checks.append(("enabled", bool(enabled), _attr(el, "enabled"),
                           None))
        if visible is not None:
            off = _attr(el, "offscreen")
            if off is None:
                checks.append(("visible", bool(visible), None, None))
            else:
                checks.append(("visible", bool(visible), not bool(off), None))
        if focused is not None:
            checks.append(("focused", bool(focused), _attr(el, "focused"),
                           None))
        if toggled is not None:
            checks.append(("toggled", bool(toggled), _read_toggle(el), None))
        if expanded is not None:
            checks.append(("expanded", bool(expanded), _read_expand(el), None))

        if not checks:
            return VerifyResult.unknown_(
                method="uia_state", reason="没有指定要验证的状态",
                elapsed=time.time() - t0)

        last_checks = checks
        mismatched = [f for f, want, got, _ in checks
                      if got is not None and bool(got) != want]
        unknown_fields = [f for f, _w, got, _ in checks if got is None]

        if not mismatched and not unknown_fields:
            return VerifyResult.ok_(
                method="uia_state",
                reason="状态全部符合：%s" % "、".join(c[0] for c in checks),
                evidence={f: {"expected": w, "actual": g}
                          for f, w, g, _ in checks},
                elapsed=time.time() - t0)

        if time.time() >= deadline:
            break
        time.sleep(max(0.01, poll))

    if not last_checks:
        return VerifyResult.unknown_(
            method="uia_state", reason="控件没找到（等待 %.1fs）" % timeout,
            elapsed=time.time() - t0)

    evidence = {f: {"expected": w, "actual": g}
                for f, w, g, _ in last_checks}
    mismatched = [f for f, want, got, _ in last_checks
                  if got is not None and bool(got) != want]
    unknown_fields = [f for f, _w, got, _ in last_checks if got is None]

    if mismatched:
        return VerifyResult.fail(
            method="uia_state",
            reason="状态不符：%s" % "、".join(mismatched),
            evidence=evidence, elapsed=time.time() - t0)
    if len(unknown_fields) == len(last_checks):
        return VerifyResult.unknown_(
            method="uia_state",
            reason="读不到任何指定状态：%s" % "、".join(unknown_fields),
            evidence=evidence, elapsed=time.time() - t0,
            extra={"unknown_fields": unknown_fields})
    return VerifyResult.unknown_(
        method="uia_state",
        reason="部分状态读不到（%s），已读到的都符合"
               % "、".join(unknown_fields),
        evidence=evidence, elapsed=time.time() - t0,
        extra={"unknown_fields": unknown_fields})


def _attr(el: Any, name: str) -> Optional[Any]:
    try:
        v = getattr(el, name, None)
    except Exception:
        return None
    return None if v is None else v


def _read_toggle(el: Any) -> Optional[bool]:
    try:
        pats = getattr(el, "patterns", None) or {}
        p = pats.get("TogglePattern") if hasattr(pats, "get") else None
        if p is None:
            return None
        for attr in ("toggle_state", "ToggleState", "state"):
            v = getattr(p, attr, None)
            if v is None:
                continue
            v = v() if callable(v) else v
            if v is None:
                continue
            # 1 = On, 0 = Off, 2 = Indeterminate
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return bool(v)
            if iv == 2:
                return None
            return iv == 1
    except Exception:
        pass
    return None


def _read_expand(el: Any) -> Optional[bool]:
    try:
        pats = getattr(el, "patterns", None) or {}
        p = pats.get("ExpandCollapsePattern") if hasattr(pats, "get") else None
        if p is None:
            return None
        for attr in ("expand_collapse_state", "ExpandCollapseState", "state"):
            v = getattr(p, attr, None)
            if v is None:
                continue
            v = v() if callable(v) else v
            if v is None:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return bool(v)
            # 0=Collapsed 1=Expanded 2=Partially 3=Leaf
            if iv == 3:
                return None
            return iv in (1, 2)
    except Exception:
        pass
    return None


def verify_window_title(
    expected: Any,
    *,
    hwnd: int = 0,
    match: str = "contains",
    timeout: float = 2.0,
) -> VerifyResult:
    """验证窗口标题。默认用 ``contains``（标题常带文件名等动态部分）。"""
    t0 = time.time()
    deadline = time.time() + max(0.0, timeout)
    last = None
    while True:
        title = _window_title(hwnd)
        last = title
        if title is not None:
            v = _match_value(title, expected, match)
            if v is True:
                return VerifyResult.ok_(
                    method="window_title",
                    reason="标题匹配（%s）" % match,
                    evidence={"expected": _safe(expected),
                              "actual": _safe(title)},
                    elapsed=time.time() - t0)
        if time.time() >= deadline:
            break
        time.sleep(0.05)
    if last is None:
        return VerifyResult.unknown_(
            method="window_title", reason="读不到窗口标题",
            elapsed=time.time() - t0)
    return VerifyResult.fail(
        method="window_title",
        reason="标题不匹配：期望 %r，实际 %r"
               % (_safe(expected), _safe(last)),
        evidence={"expected": _safe(expected), "actual": _safe(last)},
        elapsed=time.time() - t0)


def _window_title(hwnd: int) -> Optional[str]:
    if not hwnd:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        n = user32.GetWindowTextLengthW(wt.HWND(int(hwnd)))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(wt.HWND(int(hwnd)), buf, n + 1)
        return buf.value or ""
    except Exception:
        return None


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def verify_desktop(
    *,
    # 像素路径
    hwnd: int = 0,
    region: Optional[Tuple[int, int, int, int]] = None,
    before_hash: Optional[int] = None,
    before_shot: Any = None,
    after_shot: Any = None,
    threshold: int = DEFAULT_CHANGED_MIN,
    unchanged_max: int = DEFAULT_UNCHANGED_MAX,
    expect_change: bool = True,
    # UIA 路径
    uia_target: Any = None,
    expect_value: Any = None,
    has_expect_value: bool = False,
    match: str = "exact",
    uia_state: Optional[Dict[str, Any]] = None,
    expect_exists: Optional[bool] = None,
    uia_name_exact: bool = False,
    uia_control_type: Any = None,
    verify_window_title: Optional[str] = None,
    title_match: str = "contains",
    # 通用
    settle: float = DEFAULT_SETTLE,
    prefer: str = "auto",
    timeout: float = 2.0,
) -> VerifyResult:
    """桌面端统一验证入口，自动选判据。

    ``prefer``：

    - ``"auto"``（默认）给了 UIA 目标就先用 UIA；UIA 判"未知"再回退像素
    - ``"uia"`` 只用 UIA
    - ``"pixel"`` 只用像素

    **UIA 判"失败"时不回退像素** —— 因为 UIA 给出了确切值，
    那是强证据；此时像素无论变没变都改变不了"值不对"这个事实。
    只有 UIA **拿不到证据**（unknown）时才该看像素。

    标题判据（``verify_window_title``）**优先级最高**，且独立于
    UIA 目标 —— 窗口标题是最廉价、最稳的判据：不需要遍历控件树、
    不需要截图。所以一旦给了就先用它，有结论就返回。
    """
    t0 = time.time()

    # -- 标题判据优先（最廉价且最稳）--
    #
    # ⚠⚠ 这里有个**必然踩一次**的坑：形参名 ``verify_window_title``
    #   与模块级函数 ``verify_window_title`` **同名**，函数体内的名字
    #   解析会先找到形参（那个字符串），于是变成
    #   ``'Program Manager'(...)`` → ``TypeError: 'str' object is not callable``。
    #
    #   解法：用模块字典直接取函数本体，绕开局部作用域。
    #   不改成别名导入是为了让外部 API 名字保持直观（用户看到的就是
    #   ``verify_window_title="标题"`` 这个参数名）。
    if verify_window_title is not None:
        _fn = globals()["verify_window_title"]
        tres = _fn(verify_window_title, hwnd=hwnd, match=title_match,
                   timeout=timeout)
        tres.elapsed = time.time() - t0
        if tres.conclusive or prefer == "uia":
            return tres
        # 标题判据拿不到（hwnd 无效等）→ 继续走别的判据，别浪费
        _title_fallback = tres
    else:
        _title_fallback = None

    if prefer == "pixel":
        res = verify_pixel(
            hwnd=hwnd, region=region, before_hash=before_hash,
            before_shot=before_shot, after_shot=after_shot,
            threshold=threshold, unchanged_max=unchanged_max,
            expect_change=expect_change, settle=settle)
        res.mode = "desktop"
        res.elapsed = time.time() - t0
        return res

    uia_res = None  # type: Optional[VerifyResult]

    if prefer in ("auto", "uia") and (uia_target is not None
                                      or uia_state is not None):
        if expect_exists is not None:
            uia_res = verify_uia_exists(uia_target, expect=expect_exists,
                                        # ⚠ 收紧旋钮必须一路透传到这里。
                                        #   漏传的后果：调用方在 CLI 上加了
                                        #   ``--name-exact`` 以为快了，实际
                                        #   查询仍按宽松子串走 Python 全遍历
                                        #   （实测 52.8 秒 / 次）。
                                        name_exact=uia_name_exact,
                                        control_type=uia_control_type,
                                        window=hwnd or 0,
                                        timeout=timeout)
        elif uia_state:
            uia_res = verify_uia_state(uia_target, settle=settle,
                                       timeout=timeout, **uia_state)
        elif has_expect_value:
            uia_res = verify_uia_value(uia_target, expect_value, match=match,
                                       settle=settle, timeout=timeout)

        if uia_res is not None:
            if prefer == "uia":
                uia_res.mode = "desktop"
                uia_res.elapsed = time.time() - t0
                return uia_res
            if uia_res.conclusive:
                # UIA 有确定结论 —— 采信它，像素只作为补充证据
                if before_hash is not None or before_shot is not None:
                    pr = verify_pixel(
                        hwnd=hwnd, region=region, before_hash=before_hash,
                        before_shot=before_shot, after_shot=after_shot,
                        threshold=threshold, unchanged_max=unchanged_max,
                        expect_change=expect_change, settle=0.0)
                    uia_res.evidence["pixel"] = pr.to_dict()
                    uia_res.extra["pixel_agree"] = (pr.verdict == uia_res.verdict)
                uia_res.mode = "desktop"
                uia_res.elapsed = time.time() - t0
                return uia_res
            # UIA 未知 → 落下去试像素

    pixel_res = verify_pixel(
        hwnd=hwnd, region=region, before_hash=before_hash,
        before_shot=before_shot, after_shot=after_shot,
        threshold=threshold, unchanged_max=unchanged_max,
        expect_change=expect_change, settle=settle)

    if uia_res is not None and pixel_res.unknown:
        # 两条路都没证据 —— 合成一个更完整的"未知"
        return VerifyResult.unknown_(
            method="desktop",
            reason="UIA 与像素均无法判定（UIA: %s / 像素: %s）"
                   % (uia_res.reason, pixel_res.reason),
            evidence={"uia": uia_res.to_dict(), "pixel": pixel_res.to_dict()},
            target=str(uia_target)[:80] if uia_target is not None else "",
            mode="desktop", elapsed=time.time() - t0,
            extra={"both_unknown": True})

    if uia_res is not None:
        pixel_res.evidence["uia"] = uia_res.to_dict()
        pixel_res.extra["uia_unknown"] = True
    pixel_res.mode = "desktop"
    pixel_res.elapsed = time.time() - t0
    return pixel_res


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

class _FakeBitmap(object):
    """假 Bitmap：直接给像素二维数组。"""

    def __init__(self, w, h, fn, bpp=0):
        self.width = w
        self.height = h
        self.stride = w * bpp if bpp else 0
        if bpp:
            buf = bytearray()
            for y in range(h):
                for x in range(w):
                    r, g, b = fn(x, y)
                    buf.extend((b, g, r, 255))
            self.pixels = bytes(buf)
        else:
            self.pixels = [[fn(x, y) for x in range(w)] for y in range(h)]


class _FakeElement(object):
    def __init__(self, **kw):
        self.name = kw.get("name")
        self.control_type = kw.get("control_type", "Button")
        self.enabled = kw.get("enabled", True)
        self.focused = kw.get("focused", False)
        self.offscreen = kw.get("offscreen", False)
        self.rect = kw.get("rect", (0, 0, 10, 10))
        self.patterns = kw.get("patterns", {}) or {}
        self._value = kw.get("value")


class _FakeValuePattern(object):
    def __init__(self, value):
        self.value = value


class _FakeTogglePattern(object):
    def __init__(self, state):
        self.toggle_state = state


class _FakeExpandPattern(object):
    def __init__(self, state):
        self.expand_collapse_state = state


def _pat(kind, val):
    if kind == "value":
        return _FakeValuePattern(val)
    if kind == "toggle":
        return _FakeTogglePattern(val)
    return _FakeExpandPattern(val)


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 像素：无基线必须未知 ---
    r = verify_pixel(hwnd=0, region=None)
    add("无基线 → unknown", r.unknown is True, r.describe())
    add("无基线 reason 说明原因", "基线" in r.reason, r.reason)

    # --- 像素：真截图路径（本机有屏幕，能抓） ---
    import os
    same = _FakeBitmap(64, 64, lambda x, y: (255, 255, 255) if x < 32
                       else (0, 0, 0))
    hb = bitmap_hash(same)
    add("bitmap_hash 可算", hb is not None, hb)
    add("bitmap_hash 同图一致", bitmap_hash(same) == hb)

    r = verify_pixel(after_shot=same, before_hash=hb)
    add("同图 + 期望变化 → fail", r.failed is True, r.describe())
    add("同图 fail evidence 有距离", r.evidence.get("distance") == 0, r.evidence)

    r = verify_pixel(after_shot=same, before_hash=hb, expect_change=False)
    add("同图 + 期望不变 → ok", r.ok is True, r.describe())

    # **用局部结构变化，不用整体镜像** —— pHash 对左右镜像天生不敏感
    # （DCT 看低频能量分布，镜像不改变它）。整体镜像实测距离只有 4，
    # 落在"几乎没变"区间，用它做用例会测出"算法不该有的行为"。
    # 真实场景是"页面多了一块内容"，所以用局部黑块。
    inv = _FakeBitmap(64, 64, lambda x, y: (0, 0, 0)
                      if (10 <= x < 26 and 10 <= y < 26) else (255, 255, 255))
    r = verify_pixel(after_shot=inv, before_hash=hb)
    add("局部变化 + 期望变化 → ok", r.ok is True, r.describe())
    add("局部变化 evidence 距离 > 阈值", r.evidence.get("distance", 0) > 6,
        r.evidence.get("distance"))

    r = verify_pixel(after_shot=inv, before_hash=hb, expect_change=False)
    add("局部变化 + 期望不变 → fail", r.failed is True, r.describe())

    # 灰度区
    r = verify_pixel(after_shot=same, before_hash=hb, threshold=20,
                     unchanged_max=0)
    add("阈值拉大后同图仍 fail（距离 0 ≤ 0）", r.failed is True, r.describe())

    # before_shot 也能当基线
    r = verify_pixel(before_shot=same, after_shot=inv)
    add("before_shot 当基线可用", r.ok is True, r.describe())

    # 无 after 且抓不到 → 未知
    r = verify_pixel(before_hash=hb, hwnd=0, region=None)
    add("无 after 且抓不到 → unknown", r.unknown is True, r.describe())

    # --- bitmap_hash 从 bytes(BGRA) ---
    bmp_bytes = _FakeBitmap(64, 64, lambda x, y: (255, 255, 255) if x < 32
                            else (0, 0, 0), bpp=4)
    hb2 = bitmap_hash(bmp_bytes)
    add("bytes 像素可算 hash", hb2 is not None, hb2)
    add("bytes 与二维像素 hash 一致", hb2 == hb, (hb2, hb))

    add("bitmap_hash None → None", bitmap_hash(None) is None)

    class _Zero(object):
        width = 0
        height = 0
        pixels = []
        stride = 0

    add("零尺寸 bitmap → None", bitmap_hash(_Zero()) is None)

    # --- UIA 读值 ---
    ok_read, val = read_uia_value(_FakeElement(
        patterns={"ValuePattern": _pat("value", "2024")}))
    add("read_uia_value 从 ValuePattern 读", ok_read and val == "2024",
        (ok_read, val))

    ok_read, val = read_uia_value(_FakeElement(value=None, name="按钮"))
    add("read_uia_value 退到 name", ok_read and val == "按钮", (ok_read, val))

    ok_read, val = read_uia_value(None)
    add("read_uia_value None → 没拿到", ok_read is False and val is None)

    ok_read, val = read_uia_value(_FakeElement(
        name=None, value=None, patterns={}))
    add("read_uia_value 全空 → 没拿到", ok_read is False)

    ok_read, val = read_uia_value(_FakeElement(
        patterns={"ValuePattern": _pat("value", "")}))
    add("空字符串是有效值", ok_read is True and val == "", (ok_read, val))

    # --- UIA 值匹配 ---
    el = _FakeElement(patterns={"ValuePattern": _pat("value", "hello 世界")})
    r = verify_uia_value(el, "hello 世界", settle=0, timeout=0.0)
    add("值精确匹配 → ok", r.ok is True, r.describe())

    r = verify_uia_value(el, "hello", match="contains", settle=0, timeout=0.0)
    add("值 contains 匹配 → ok", r.ok is True, r.describe())

    r = verify_uia_value(el, r"hel+o", match="regex", settle=0, timeout=0.0)
    add("值 regex 匹配 → ok", r.ok is True, r.describe())

    r = verify_uia_value(el, r"^世界$", match="regex", settle=0, timeout=0.0)
    add("值 regex 锚定不匹配 → fail", r.failed is True, r.describe())

    r = verify_uia_value(el, r"\d{4}", match="regex", settle=0, timeout=0.0)
    add("值 regex 无数字不匹配 → fail", r.failed is True, r.describe())

    r = verify_uia_value(el, "xxxx", settle=0, timeout=0.0)
    add("值不匹配 → fail", r.failed is True, r.describe())
    add("fail evidence 带实际值",
        r.evidence.get("actual") == "hello 世界", r.evidence)

    r = verify_uia_value(el, None, match="nonempty", settle=0, timeout=0.0)
    add("nonempty 命中", r.ok is True, r.describe())

    r = verify_uia_value(_FakeElement(patterns={"ValuePattern":
                                                _pat("value", "")}),
                         None, match="nonempty", settle=0, timeout=0.0)
    add("nonempty 空值 → fail", r.failed is True, r.describe())

    r = verify_uia_value(el, None, match="any", settle=0, timeout=0.0)
    add("any 只要求读得到", r.ok is True, r.describe())

    r = verify_uia_value(_FakeElement(name=None, patterns={}), None,
                         match="any", settle=0, timeout=0.0)
    add("读不到值 → unknown 而非 fail", r.unknown is True, r.describe())
    add("unknown 标记 no_readable_value",
        r.extra.get("no_readable_value") is True, r.extra)

    r = verify_uia_value(None, "x", settle=0, timeout=0.0)
    add("控件不存在 → unknown", r.unknown is True, r.describe())

    # 前后空白会被 strip
    r = verify_uia_value(_FakeElement(
        patterns={"ValuePattern": _pat("value", "  abc  ")}),
        "abc", settle=0)
    add("exact 匹配会 strip 空白", r.ok is True, r.describe())

    # 数字与字符串互通
    r = verify_uia_value(_FakeElement(
        patterns={"ValuePattern": _pat("value", 42)}), "42", settle=0)
    add("数字与字符串可比", r.ok is True, r.describe())

    # --- UIA 状态 ---
    el = _FakeElement(enabled=True, focused=False, offscreen=False)
    r = verify_uia_state(el, enabled=True, focused=False, visible=True,
                         settle=0, timeout=0.0)
    add("状态全符合 → ok", r.ok is True, r.describe())
    add("状态 evidence 逐项",
        set(r.evidence) == {"enabled", "focused", "visible"}, sorted(r.evidence))

    r = verify_uia_state(el, enabled=False, settle=0, timeout=0)
    add("状态不符 → fail", r.failed is True, r.describe())
    add("fail reason 点名不符字段", "enabled" in r.reason, r.reason)

    r = verify_uia_state(_FakeElement(offscreen=True), visible=True, settle=0, timeout=0.0)
    add("offscreen=True 判为不可见", r.failed is True, r.describe())

    r = verify_uia_state(el, settle=0, timeout=0.0)
    add("没指定状态 → unknown", r.unknown is True, r.describe())

    r = verify_uia_state(_FakeElement(patterns={}), toggled=True, settle=0, timeout=0.0)
    add("toggle 读不到 → unknown", r.unknown is True, r.describe())

    el_t = _FakeElement(patterns={"TogglePattern": _pat("toggle", 1)})
    r = verify_uia_state(el_t, toggled=True, settle=0, timeout=0.0)
    add("toggle_state=1 → 开", r.ok is True, r.describe())

    el_t2 = _FakeElement(patterns={"TogglePattern": _pat("toggle", 0)})
    r = verify_uia_state(el_t2, toggled=False, settle=0, timeout=0.0)
    add("toggle_state=0 → 关", r.ok is True, r.describe())

    el_t3 = _FakeElement(patterns={"TogglePattern": _pat("toggle", 2)})
    r = verify_uia_state(el_t3, toggled=True, settle=0, timeout=0.0)
    add("toggle_state=2 不确定 → unknown", r.unknown is True, r.describe())

    el_e = _FakeElement(patterns={"ExpandCollapsePattern":
                                  _pat("expand", 1)})
    r = verify_uia_state(el_e, expanded=True, settle=0, timeout=0.0)
    add("expand_state=1 → 展开", r.ok is True, r.describe())

    el_e2 = _FakeElement(patterns={"ExpandCollapsePattern":
                                   _pat("expand", 0)})
    r = verify_uia_state(el_e2, expanded=False, settle=0, timeout=0.0)
    add("expand_state=0 → 折叠", r.ok is True, r.describe())

    el_e3 = _FakeElement(patterns={"ExpandCollapsePattern":
                                   _pat("expand", 3)})
    r = verify_uia_state(el_e3, expanded=True, settle=0, timeout=0.0)
    add("expand_state=3 叶子 → unknown", r.unknown is True, r.describe())

    # 部分状态读不到
    r = verify_uia_state(_FakeElement(enabled=True, patterns={}),
                         enabled=True, toggled=True, settle=0, timeout=0.0)
    add("部分状态未知 → unknown",
        r.unknown is True and "toggled" in r.reason, r.reason)

    r = verify_uia_state(None, enabled=True, settle=0, timeout=0.0)
    add("状态：控件不存在 → unknown", r.unknown is True, r.describe())

    # --- UIA 存在性 ---
    r = verify_uia_exists(_FakeElement(name="x"), expect=True, timeout=0.0)
    add("存在性：Element 直接给 → ok", r.ok is True, r.describe())

    r = verify_uia_exists(None, expect=False, timeout=0.0)
    add("存在性：None 且期望不存在 → ok", r.ok is True, r.describe())

    r = verify_uia_exists(None, expect=True, timeout=0.0)
    add("存在性：None 且期望存在 → fail", r.failed is True, r.describe())

    r = verify_uia_exists(_FakeElement(name="x"), expect=False, timeout=0.0)
    add("存在性：给了 Element 却期望消失 → fail",
        r.failed is True, r.describe())

    add("存在性 reason 可读", "控件" in r.reason, r.reason)

    # --- 窗口标题 ---
    r = verify_window_title("", hwnd=0, timeout=0.0)
    add("标题：hwnd=0 → unknown", r.unknown is True, r.describe())

    # --- _match_value ---
    add("match exact 相符", _match_value("a", "a", "exact") is True)
    add("match exact 不符", _match_value("a", "b", "exact") is False)
    add("match contains", _match_value("abc", "b", "contains") is True)
    add("match regex", _match_value("abc123", r"\d+", "regex") is True)
    add("match regex 非法返回 None",
        _match_value("a", "[", "regex") is None)
    add("match nonempty 空为 False",
        _match_value("  ", None, "nonempty") is False)
    add("match any 恒 True", _match_value(None, None, "any") is True)
    add("match 未知模式返回 None",
        _match_value("a", "a", "zzz") is None)
    add("match 默认 exact", _match_value("a", "a", "") is True)

    # --- _safe ---
    add("_safe 数字直通", _safe(42) == 42)
    add("_safe None 直通", _safe(None) is None)
    add("_safe 长串截断", _safe("x" * 500).endswith("…"),
        _safe("x" * 500)[-3:])
    add("_safe 短串不变", _safe("abc") == "abc")

    # --- verify_desktop 路由 ---
    same2 = _FakeBitmap(64, 64, lambda x, y: (255, 255, 255) if x < 32
                        else (0, 0, 0))
    hb3 = bitmap_hash(same2)

    r = verify_desktop(prefer="pixel", after_shot=inv, before_hash=hb3)
    add("verify_desktop prefer=pixel 走像素", r.ok is True, r.describe())
    add("verify_desktop mode=desktop", r.mode == "desktop", r.mode)

    r = verify_desktop(prefer="uia",
                       uia_target=_FakeElement(patterns={
                           "ValuePattern": _pat("value", "yes")}),
                       expect_value="yes", has_expect_value=True,
                       settle=0)
    add("verify_desktop prefer=uia 走 UIA", r.ok is True, r.describe())

    r = verify_desktop(uia_target=_FakeElement(patterns={
        "ValuePattern": _pat("value", "yes")}),
        expect_value="yes", has_expect_value=True, settle=0,
        before_hash=hb3, after_shot=inv)
    add("auto：UIA 成功即采信 UIA", r.ok is True, r.describe())
    add("auto：附带像素佐证",
        "pixel" in r.evidence, sorted(r.evidence))
    add("auto：pixel_agree 记录",
        r.extra.get("pixel_agree") is True, r.extra)

    # UIA fail 时也应采信 UIA（不回退）
    r = verify_desktop(uia_target=_FakeElement(patterns={
        "ValuePattern": _pat("value", "no")}),
        expect_value="yes", has_expect_value=True, settle=0, timeout=0.0,
        before_hash=hb3, after_shot=inv)
    add("auto：UIA 失败即采信（不回退像素）", r.failed is True, r.describe())
    add("auto：UIA 失败时 method 是 uia_value",
        r.method == "uia_value", r.method)

    # UIA unknown → 回退像素
    r = verify_desktop(uia_target=_FakeElement(name=None, patterns={}),
                       expect_value="yes", has_expect_value=True, settle=0,
                       timeout=0.0,
                       before_hash=hb3, after_shot=inv)
    add("auto：UIA 未知时回退像素", r.ok is True and r.method == "phash",
        r.describe())
    add("auto：回退时记录 uia_unknown",
        r.extra.get("uia_unknown") is True, r.extra)
    add("auto：回退时附 UIA 证据",
        "uia" in r.evidence, sorted(r.evidence))

    # 两条都未知
    r = verify_desktop(uia_target=_FakeElement(name=None, patterns={}),
                       expect_value="yes", has_expect_value=True, settle=0,
                       timeout=0.0)
    add("auto：两条都未知 → 合成 unknown", r.unknown is True, r.describe())
    add("auto：合成 unknown method=desktop", r.method == "desktop", r.method)
    add("auto：合成 unknown 标注 both_unknown",
        r.extra.get("both_unknown") is True, r.extra)

    # expect_exists 路径
    r = verify_desktop(uia_target=_FakeElement(name="x"), expect_exists=True,
                       settle=0)
    add("auto：expect_exists 路径", r.ok is True, r.describe())

    # uia_state 路径
    r = verify_desktop(uia_target=_FakeElement(enabled=True),
                       uia_state={"enabled": True}, settle=0)
    add("auto：uia_state 路径", r.ok is True, r.describe())

    # 没有任何 UIA 目标时纯像素
    r = verify_desktop(after_shot=inv, before_hash=hb3, settle=0)
    add("auto：无 UIA 目标时纯像素",
        r.ok is True and r.method == "phash", r.describe())

    # --- 真实屏幕截图（本机必有屏幕） ---
    if os.name == "nt":
        bmp = grab_bitmap(region=(0, 0, 200, 150))
        add("grab_bitmap 抓屏幕区域可用", bmp is not None)
        if bmp is not None:
            add("抓到的 bitmap 有尺寸",
                int(bmp.width) > 0 and int(bmp.height) > 0,
                (bmp.width, bmp.height))
            h_real = bitmap_hash(bmp)
            add("真实截图可算 hash", h_real is not None, h_real)
            r = verify_pixel(after_shot=bmp, before_hash=h_real,
                             settle=0)
            add("真实截图自比 → fail（没变）", r.failed is True, r.describe())
    else:
        add("非 Windows 跳过真实截图", True, "skipped")

    add("window_rect(0) → None", window_rect(0) is None)
    add("_window_title(0) → None", _window_title(0) is None)
    add("_attr 缺失属性 → None", _attr(_FakeElement(), "nope") is None)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "verify.desktop"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
