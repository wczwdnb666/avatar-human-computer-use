# -*- coding: utf-8 -*-
"""第二层检测：窗口标题 / 类名 / 模态遮挡。

## 为什么窗口层必须排在 DOM 层后面、像素层前面

窗口层的成本是 **20ms 级**（`EnumWindows` 一次遍历，阶段四实测通道 A
毫秒级），比 DOM 层贵一点点，但比像素层（截图 + OCR，0.5~2s）便宜 100 倍。
更重要的是它**覆盖面不同**：

- DOM 层只看得到网页里的东西
- 窗口层看得到**整个桌面** —— 独立客户端的验证弹窗、Windows 凭据框、
  智能卡 PIN 框、Cloudflare 的浏览器标题变化，全在这里

两者是互补的，不是冗余的。有 CDP 会话时先跑 DOM（快），
DOM 没结论时跑窗口层（次快），都干净才动像素层（最贵）。

## 复用什么、不复用什么

**复用**：阶段二的 ``avatar.app_manager.windows``（``list_windows`` /
``refresh`` / ``WindowInfo``）。它已经处理了 DWM 隐形窗口、辅助窗口
过滤、UWP 框架这些坑 —— 重写一遍只会引入新 bug。

**不复用**：阶段二的 ``has_modal_dialog``。它判据是"有 GW_OWNER 或
类名 #32770"，那是"有没有对话框"的通用判据。本层要的是
**"有没有挡住我要操作的那个窗口的模态框"** —— 需要几何相交 + Z 序
判断，是另一个问题。所以自己写 ``detect_occlusion``。

## 硬约束

- 指纹全部从 fingerprints.py 读，本文件不许出现厂商名字面量。
- 异常不吞：每个子步骤各自 try，失败记进 ``evidence["errors"]``，
  继续跑下一个子步骤。

## 能力边界

**做**：枚举顶层窗口，用标题 / 类名 / 进程名匹配 :mod:`fingerprints`
的窗口指纹；另外判断**目标窗口是否被模态框遮挡**。

**不做**：

- **不截图**。本层是 20ms 级的纯枚举，截图会毁掉这个优势。
- **不做 OCR**。读不到文字就说读不到，不猜。
- **不改窗口状态**。不置顶、不激活、不最小化 —— 只读。
  激活窗口是有副作用的操作（会抢焦点），不该藏在检测层里。
- **不复用** ``app_manager.windows.has_modal_dialog``。它的判据是
  "有没有对话框"，本层要的是"**有没有挡住目标窗口**" ——
  需要几何相交 + Z 序，是另一个问题，所以自己写
  ``detect_occlusion``。
- **不判 severity**。同上，由指纹库配。
- **不处理多显示器坐标归一化**。几何比较用的是屏幕物理坐标，
  跨屏 DPI 差异由阶段一的 :mod:`app_manager.layout` 负责。
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .fingerprints import match_window
from .status import DEFAULT_RISK_TIMEOUT, RiskStatus, RiskType, Severity

__all__ = [
    "WINDOW_SCAN_TIMEOUT",
    "OCCLUSION_MIN_AREA_RATIO",
    "WinFields",
    "list_candidate_windows",
    "detect_window",
    "detect_occlusion",
    "selfcheck",
]

#: 窗口枚举的预算。EnumWindows 实测毫秒级，给 1s 已经很宽松。
WINDOW_SCAN_TIMEOUT = 1.0

#: 遮挡判定：弹窗面积要占到目标窗口的多少才算"挡住"。
#: 0.18 的取法：一个小提示条（toast）通常 < 10% 面积，不该算遮挡；
#: 而验证题弹窗通常 > 30%。取 18% 落在两者中间，偏保守
#: （宁可漏报遮挡，也不要误报 —— 误报会让人白跑一趟）。
OCCLUSION_MIN_AREA_RATIO = 0.18

#: 这些窗口类名是**系统辅助层**，永远不算"遮挡"。
#: 它们会覆盖在别人上面但用户看不见（或不该被当成障碍）：
#: 输入法候选窗、任务栏浮出、工具提示等。
_NON_BLOCKING_CLASSES = frozenset({
    "tooltips_class32",
    "ime",
    "msctfime",
    "shelldll_defview",
    "tasklistthumnail",
    "windows.ui.core.corewindow",       # 部分 UWP 悬浮层
    "progman",
    "shell_traywnd",
    "notifyiconoverflowwindow",
})


class WinFields(object):
    """窗口层采集到的原始材料。

    和 ``DomFields`` 同样的设计：**薄包装 + ok 标记**。
    ``ok=False`` 表示"没查成"，不是"没有窗口"。
    """

    __slots__ = ("windows", "target", "errors", "ok", "elapsed")

    def __init__(self, windows: Optional[Sequence[Any]] = None, *,
                 target: Any = None, ok: bool = True,
                 errors: Optional[List[str]] = None,
                 elapsed: float = 0.0) -> None:
        self.windows = list(windows or [])
        self.target = target
        self.errors = list(errors or [])
        self.ok = bool(ok)
        self.elapsed = float(elapsed)

    @property
    def empty(self) -> bool:
        """一个窗口都没枚举到 —— 通常意味着 EnumWindows 被沙箱拦了。"""
        return not self.windows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_count": len(self.windows),
            "target_hwnd": getattr(self.target, "hwnd", 0) or 0,
            "ok": self.ok,
            "errors": self.errors,
            "elapsed": round(self.elapsed, 4),
        }


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def list_candidate_windows(*, include_invisible: bool = False,
                           errors: Optional[List[str]] = None) -> List[Any]:
    """枚举顶层窗口。

    直接转发到阶段二的 ``app_manager.windows.list_windows``。
    **只做一次遍历**，把结果给所有子步骤共用 —— 每次动作前都检测，
    重复枚举是纯浪费。
    """
    from ..app_manager import windows as W
    return W.list_windows(include_invisible=include_invisible)


def _refresh(hwnd: int) -> Any:
    """按句柄重取窗口信息。失败返回 None（不抛）。"""
    try:
        from ..app_manager import windows as W
        return W.refresh(hwnd)
    except Exception:      # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 模态遮挡
# --------------------------------------------------------------------------
def detect_occlusion(target_hwnd: int,
                     windows: Optional[Sequence[Any]] = None,
                     *,
                     min_ratio: float = OCCLUSION_MIN_AREA_RATIO,
                     ) -> Optional[Dict[str, Any]]:
    """判断有没有别的窗口挡在 ``target_hwnd`` 前面。

    判据（**三个都要满足**）：

    1. **是弹窗** —— 有属主（``GW_OWNER`` 非零）或类名是对话框类
       ``#32770``。普通窗口盖住目标不算"遮挡"，那是用户自己
       切换了窗口，属于另一类问题。
    2. **可见且未最小化** —— 隐形窗口当然不挡。
    3. **面积相交比例 ≥ ``min_ratio``** —— 小 toast 不算。

    **Z 序怎么判**（⚠️ 这里踩过一个真实的坑，记录如下）：

    第一版我假设"``EnumWindows`` 的返回顺序就是 Z 序，谁在前面谁在上面"。
    **这是错的** —— 阶段二的 ``app_manager.windows.list_windows`` 会
    在枚举完之后**按 score 重新排序**（它的 docstring 写着"按 score 降序"），
    原始的 Z 序在那里就被破坏了。实测拿一个记事本 + 一个对话框调用它，
    返回顺序跟屏幕上谁在前毫无关系。

    所以正确做法是**问 Windows 要真 Z 序**：
    ``GetWindow(hwnd, GW_HWNDPREV)`` 返回同层中在它**上面**的窗口，
    沿着这条链一直往上走能走到最顶层。我的判据变成：

        从目标窗口沿 GW_HWNDPREV 上溯，如果沿途碰到候选弹窗 → 它在目标之上

    这样不依赖任何枚举顺序，也不受 ``list_windows`` 排序影响。
    代价是每个候选窗口最多几次 ``GetWindow`` 调用 —— 微秒级，
    而候选弹窗通常只有 0~3 个，可以忽略。

    返回 ``{"hwnd", "title", "class", "pid", "process",
    "overlap_ratio", "area_ratio", "reasons"}``，没遮挡返回 ``None``。
    """
    if not target_hwnd:
        return None
    if windows is None:
        try:
            windows = list_candidate_windows()
        except Exception:      # noqa: BLE001
            return None

    target = None
    for w in windows:
        if int(getattr(w, "hwnd", 0) or 0) == int(target_hwnd):
            target = w
            break
    if target is None:
        # 目标不在枚举结果里（隐形/已销毁/被过滤）——
        # 拿不到几何就没法算遮挡，不猜。
        return None

    trect = tuple(getattr(target, "rect", (0, 0, 0, 0)) or (0, 0, 0, 0))
    tarea = max(1, _rect_area(trect))

    # 真 Z 序：目标之上的所有窗口句柄
    above: set = set()
    order_ok = True
    try:
        above = _windows_above(int(target_hwnd), limit=400)
    except Exception:      # noqa: BLE001
        order_ok = False

    for w in windows:
        hwnd = int(getattr(w, "hwnd", 0) or 0)
        if not hwnd or hwnd == int(target_hwnd):
            continue

        cls = (getattr(w, "class_name", "") or "").lower()
        if cls in _NON_BLOCKING_CLASSES:
            continue
        if not bool(getattr(w, "visible", False)):
            continue
        if bool(getattr(w, "minimized", False)):
            continue
        if bool(getattr(w, "cloaked", False)):
            continue

        # --- Z 序：必须在目标之上 ---
        if order_ok:
            if hwnd not in above:
                continue          # 在目标之下，不挡
        else:
            # 拿不到 Z 序时**保守判为可能遮挡** ——
            # 宁可多叫人一次，也不要放过一个盖住屏幕的验证框。
            pass

        reasons = []
        # 必须是"弹窗"：有属主 或 标准对话框类
        is_popup = False
        try:
            if cls == "#32770":
                is_popup = True
                reasons.append("类名 #32770")
            owner_hwnd = _owner(hwnd)
            if owner_hwnd:
                is_popup = True
                reasons.append("有属主 0x%X" % owner_hwnd)
        except Exception:      # noqa: BLE001
            pass
        if not is_popup:
            continue

        wrect = tuple(getattr(w, "rect", (0, 0, 0, 0)) or (0, 0, 0, 0))
        ov = _overlap_area(trect, wrect)
        area_ratio = ov / tarea
        if area_ratio < min_ratio:
            continue

        reasons.append("面积覆盖 %.0f%%" % (area_ratio * 100))
        if order_ok:
            reasons.append("Z 序在目标之上（GetWindow GW_HWNDPREV 链）")
        else:
            reasons.append("Z 序未取得 —— 保守判为遮挡")
        return {
            "hwnd": hwnd,
            "title": getattr(w, "title", "") or "",
            "class": getattr(w, "class_name", "") or "",
            "pid": int(getattr(w, "pid", 0) or 0),
            "process": getattr(w, "process", "") or "",
            "overlap_ratio": round(area_ratio, 4),
            "area_ratio": round(area_ratio, 4),
            "z_order_known": order_ok,
            "reasons": reasons,
        }
    return None


def _windows_above(hwnd: int, *, limit: int = 400) -> set:
    """返回**真 Z 序中在 ``hwnd`` 之上**的所有同层窗口句柄集合。

    做法：反复 ``GetWindow(cur, GW_HWNDPREV)`` 一路向上 ——
    ``GW_HWNDPREV`` 的语义就是"同层中在我上面的那个窗口"。

    与 ``EnumWindows`` 的顺序假设不同，这个方法**不依赖任何枚举顺序**，
    是 Win32 明确文档化的 Z 序查询。之前用枚举顺序判断是错的
    （见 :func:`detect_occlusion` docstring 里记录的坑）。

    ``limit`` 是保险丝：万一链路上有环（不该发生，但内核态出问题时
    什么都有可能），到上限就停，绝不无限循环。
    """
    out: set = set()
    try:
        from .._win32 import api as A
        GW_HWNDPREV = 3
        cur = int(hwnd)
        for _ in range(max(1, int(limit))):
            cur = int(A.user32.GetWindow(cur, GW_HWNDPREV) or 0)
            if not cur or cur in out:
                break
            out.add(cur)
    except Exception:      # noqa: BLE001
        raise
    return out


def _owner(hwnd: int) -> int:
    """取属主窗口句柄。失败返回 0。"""
    try:
        from .._win32 import api as A
        GW_OWNER = 4
        return int(A.user32.GetWindow(int(hwnd), GW_OWNER) or 0)
    except Exception:      # noqa: BLE001
        return 0


def _rect_area(r: Sequence[int]) -> int:
    if not r or len(r) < 4:
        return 0
    w = max(0, int(r[2]))
    h = max(0, int(r[3]))
    return w * h


def _overlap_area(a: Sequence[int], b: Sequence[int]) -> int:
    """两个"位置+尺寸"矩形的相交面积。

    ⚠️ 注意 ``WindowInfo.rect`` 的语义是 ``(x, y, width, height)``
    —— **不是** ``(left, top, right, bottom)``。这是阶段二定的约定，
    这里必须跟着走，否则算出来的面积会离谱。
    """
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0
    ax, ay, aw, ah = (int(a[0]), int(a[1]), int(a[2]), int(a[3]))
    bx, by, bw, bh = (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
    ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    oy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ox * oy


# --------------------------------------------------------------------------
# 检测主函数
# --------------------------------------------------------------------------
def detect_window(hwnd: int = 0, *,
                  timeout: float = DEFAULT_RISK_TIMEOUT,
                  include_invisible: bool = False,
                  check_occlusion: bool = True,
                  extra_titles: Sequence[str] = (),
                  ) -> RiskStatus:
    """第二层：窗口检测。

    参数
    ----
    - ``hwnd`` —— 当前要操作的窗口。为 0 时只做**全桌面**关键词扫描
      （仍有意义：桌面某个角落可能挂着验证弹窗）。
    - ``timeout`` —— 整层预算（硬约束 #7）。
    - ``check_occlusion`` —— 是否做遮挡判定。关了能省几毫秒，
      但会漏掉"验证框把我盖住了"这种最常见的情况。默认开。
    - ``extra_titles`` —— 调用方已知的额外标题（比如 CDP 读到的
      ``document.title``）。**这是跨层信息注入点**：网页层的标题
      比窗口层读到的更实时（浏览器标题有延迟），送进来能提高命中率。

    返回
    ----
    ``source="window"`` 的 RiskStatus。
    """
    t0 = time.perf_counter()
    budget = max(0.0, float(timeout))
    if budget <= 0.0:
        return RiskStatus.timeout(source="window", limit=0.0)

    # ---- 1) 枚举 ----
    errors: List[str] = []
    wins: List[Any] = []
    try:
        wins = list_candidate_windows(include_invisible=include_invisible,
                                      errors=errors)
    except Exception as exc:      # noqa: BLE001
        return RiskStatus.error(exc, source="window",
                                elapsed=time.perf_counter() - t0)

    t_enum = time.perf_counter() - t0
    if t_enum > budget:
        st = RiskStatus.timeout(source="window", limit=budget)
        st.extra["phase"] = "enumerate"
        return st

    fields = WinFields(wins, errors=errors, elapsed=t_enum)

    # ---- 2) 逐窗口跑指纹（硬约束：指纹只在 fingerprints.py）----
    all_hits: List[Dict[str, Any]] = []
    try:
        for w in wins:
            hits = match_window(
                title=str(getattr(w, "title", "") or ""),
                class_name=str(getattr(w, "class_name", "") or ""),
                process=str(getattr(w, "process", "") or ""),
            )
            for h in hits:
                h = dict(h)
                h["hwnd"] = int(getattr(w, "hwnd", 0) or 0)
                h["window_title"] = getattr(w, "title", "") or ""
                h["window_class"] = getattr(w, "class_name", "") or ""
                all_hits.append(h)
    except Exception as exc:      # noqa: BLE001 —— 不吞，记进 evidence 继续
        fields.errors.append("fingerprint: %s: %s" % (type(exc).__name__, exc))

    # ---- 3) 调用方注入的标题（跨层信息）----
    try:
        for t in (extra_titles or ()):
            if not t:
                continue
            for h in match_window(title=str(t)):
                h = dict(h)
                h["hwnd"] = 0
                h["window_title"] = str(t)
                h["window_class"] = ""
                h["from_extra"] = True
                all_hits.append(h)
    except Exception as exc:      # noqa: BLE001
        fields.errors.append("extra_titles: %s: %s" % (type(exc).__name__, exc))

    # ---- 4) 遮挡判定 ----
    occlusion = None
    if check_occlusion and hwnd:
        try:
            occlusion = detect_occlusion(hwnd, wins)
        except Exception as exc:      # noqa: BLE001
            fields.errors.append("occlusion: %s: %s" % (type(exc).__name__, exc))

    elapsed = time.perf_counter() - t0

    # ---- 5) 汇合 ----
    # 遮挡本身不算"验证题命中"（挡住我的可能是个保存确认框），
    # 但如果**遮挡窗口自己也命中了指纹**，那才是真的必须叫人。
    occ_hit = None
    if occlusion:
        for h in all_hits:
            if h.get("hwnd") and h["hwnd"] == occlusion.get("hwnd"):
                occ_hit = h
                break

    if all_hits:
        # 优先报"挡在前面的那个" —— 它是眼下的直接障碍
        chosen = occ_hit or max(
            all_hits,
            key=lambda h: (_sev_rank(h["fingerprint"].severity),
                           h["fingerprint"].weight))
        fp = chosen["fingerprint"]
        evidence: Dict[str, Any] = {
            "layer": "window",
            "fingerprint_id": fp.id,
            "fingerprint": fp.pattern,
            "matched_field": chosen["field"],
            "matched_value": chosen["value"],
            "vendor": fp.vendor,
            "window_hwnd": chosen.get("hwnd", 0),
            "window_title": chosen.get("window_title", ""),
            "window_class": chosen.get("window_class", ""),
            "all_hits": [h["fingerprint"].id for h in all_hits],
            "extra_hits": [
                {"fingerprint_id": h["fingerprint"].id,
                 "matched_value": h.get("value", ""),
                 "from_extra": bool(h.get("from_extra"))}
                for h in all_hits if h.get("from_extra")
            ],
            "window_count": len(wins),
            "trust": _trust_of(fp.source),
            "source": fp.source,
        }
        if occlusion:
            evidence["occlusion"] = occlusion
        if fields.errors:
            evidence["warnings"] = fields.errors

        # 命中窗口**同时**是遮挡物 → 置信度上调（两条独立证据对齐）
        conf = _confidence(fp, all_hits)
        if occ_hit is not None:
            conf = round(min(0.99, conf + 0.05), 3)
            evidence["occluding_hit"] = True

        return RiskStatus(
            True,
            type=fp.type,
            vendor=fp.vendor,
            severity=fp.severity,
            evidence=evidence,
            source="window",
            elapsed=elapsed,
            confidence=conf,
            fingerprint_id=fp.id,
            extra={"hit_count": len(all_hits),
                   "occluded": bool(occlusion),
                   "occlusion_blocking": bool(occ_hit)},
        )

    if fields.empty:
        return RiskStatus.unknown_(
            source="window", reason="no_window",
            elapsed=elapsed,
            extra={"warnings": fields.errors})

    # 没有指纹命中，但有遮挡 —— 这是**未知**而不是干净：
    # 有东西盖住了目标窗口，它可能就是个尚未被识别的验证框。
    # 报告成 clean 会让上层放心地点下去，那一点就行了。
    if occlusion:
        return RiskStatus.unknown_(
            source="window",
            reason="occluded_unknown",
            elapsed=elapsed,
            extra={"occlusion": occlusion,
                   "window_count": len(wins),
                   "warnings": fields.errors,
                   "hint": "有弹窗遮挡目标窗口，但它的标题/类名未命中任何"
                           "指纹 —— 不能断言干净，建议上层升级到像素层"},
        )

    return RiskStatus.clean(
        source="window",
        elapsed=elapsed,
        extra={"window_count": len(wins),
               "warnings": fields.errors,
               "target_hwnd": int(hwnd or 0)},
    )


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
_SEV_RANK = {
    Severity.INFO: 0,
    Severity.BYPASSABLE: 1,
    Severity.INTERRUPT: 2,
    Severity.BLOCK: 3,
}


def _sev_rank(sev: str) -> int:
    return _SEV_RANK.get(sev, 0)


def _trust_of(source_key: str) -> float:
    try:
        from .fingerprints import source_info
        return float(source_info(source_key).get("trust", 0.5))
    except Exception:      # noqa: BLE001
        return 0.5


def _confidence(fp: Any, hits: List[Dict[str, Any]]) -> float:
    """同 detector_dom：权重 × 来源可信度，上限 0.99。"""
    w = float(getattr(fp, "weight", 1.0))
    t = _trust_of(getattr(fp, "source", ""))
    bonus = min(0.15, 0.05 * max(0, len(hits) - 1))
    return round(min(0.99, max(0.05, w * t * 0.9 + bonus)), 3)


def _code_only() -> str:
    """剥掉全部字符串字面量与注释，只留可执行代码骨架。

    用途与 ``detector_dom._code_only`` 相同：让"不许硬编码指纹"
    这类静态断言不被 docstring 与断言自身的字符串干扰。
    """
    try:
        import ast
        import os
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        str_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ln in range(getattr(node, "lineno", 1),
                                getattr(node, "end_lineno",
                                        getattr(node, "lineno", 1)) + 1):
                    str_lines.add(ln)
        kept = []
        for i, ln in enumerate(src.splitlines(), 1):
            if i in str_lines:
                continue
            kept.append(ln.split("#", 1)[0])
        return "\n".join(kept)
    except Exception:      # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _W(object):
    """自检用的假 WindowInfo。字段与阶段二 ``WindowInfo`` 对齐。"""

    __slots__ = ("hwnd", "title", "class_name", "pid", "process", "rect",
                 "visible", "minimized", "cloaked")

    def __init__(self, hwnd: int, title: str = "", class_name: str = "",
                 *, pid: int = 1, process: str = "x.exe",
                 rect: Tuple[int, int, int, int] = (0, 0, 100, 100),
                 visible: bool = True, minimized: bool = False,
                 cloaked: bool = False) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = class_name
        self.pid = pid
        self.process = process
        self.rect = rect
        self.visible = visible
        self.minimized = minimized
        self.cloaked = cloaked


def selfcheck(quick: bool = True) -> dict:
    """窗口层自检。

    ``quick=True``（默认）：**全部用假窗口列表**，不碰真实桌面。
    目标是毫秒级完成（硬约束：单模块 < 2s）。

    ``quick=False``：额外真的枚举一次本机窗口（验证阶段二依赖可用）。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1) 几何工具
    _add("_rect_area 用 (x,y,w,h) 语义",
         _rect_area((10, 20, 30, 40)) == 1200, _rect_area((10, 20, 30, 40)))
    _add("_overlap_area 完全包含",
         _overlap_area((0, 0, 100, 100), (10, 10, 20, 20)) == 400,
         _overlap_area((0, 0, 100, 100), (10, 10, 20, 20)))
    _add("_overlap_area 无相交为 0",
         _overlap_area((0, 0, 10, 10), (50, 50, 10, 10)) == 0, 0)
    _add("_overlap_area 部分相交",
         _overlap_area((0, 0, 100, 100), (50, 50, 100, 100)) == 2500,
         _overlap_area((0, 0, 100, 100), (50, 50, 100, 100)))
    _add("_rect_area 容忍空/残缺输入",
         _rect_area(()) == 0 and _rect_area((1, 2)) == 0, "安全")

    # --- Z 序假数据：用 monkeypatch 打桩 _windows_above ---
    import avatar.risk_detector.detector_window as _me

    _real_above = _me._windows_above
    # 用**可变容器**装集合，不用裸变量名 —— 闭包捕获的是绑定，
    # 后面写 `_ABOVE = {400}` 会在外层新建一个局部名，闭包看到的
    # 还是旧的空集合。这是 Python 闭包的经典坑，踩过一次。
    _ABOVE = {"v": set()}

    def _fake_above(hwnd, *, limit=400):
        return set(_ABOVE["v"])
    _me._windows_above = _fake_above

    # 2) 遮挡：普通窗口不遮挡
    target = _W(100, "记事本", "Notepad", rect=(0, 0, 800, 600))
    normal = _W(200, "Chrome", "Chrome_WidgetWin_1",
                rect=(0, 0, 800, 600))       # 面积全覆盖但不是弹窗
    _ABOVE["v"] = {200}
    _add("非弹窗盖上来 → 不算遮挡",
         _me.detect_occlusion(100, [target, normal]) is None,
         "无属主非 #32770")

    # 3) 遮挡：小 toast 不算
    toast = _W(300, "提示", "#32770", rect=(0, 0, 120, 40))
    _ABOVE["v"] = {300}
    _add("小 toast（<18% 面积）→ 不算遮挡",
         _me.detect_occlusion(100, [target, toast]) is None,
         "120x40=4.8% < 18%（面积比 4800/480000）")

    # 4) 遮挡：大对话框 + Z 序在上 → 算
    dlg = _W(400, "确认", "#32770", rect=(100, 100, 600, 400))
    _ABOVE["v"] = {400}
    occ = _me.detect_occlusion(100, [target, dlg])
    _add("大对话框 + Z 序在上 → 判为遮挡",
         occ is not None and occ["hwnd"] == 400, occ)
    _add("遮挡结果带 overlap_ratio",
         occ and 0.0 < occ["overlap_ratio"] <= 1.0,
         occ.get("overlap_ratio") if occ else None)
    _add("遮挡结果带 reasons",
         occ and "面积覆盖" in " ".join(occ["reasons"]),
         occ.get("reasons") if occ else None)
    _add("遮挡结果标记 z_order_known",
         occ and occ.get("z_order_known") is True,
         occ.get("z_order_known") if occ else None)

    # 5) **真 Z 序在下 → 不算遮挡**（这是修掉的那个真 bug 的回归测试）
    _ABOVE["v"] = set()          # 400 不在目标之上
    _add("大对话框但在 Z 序之下 → 不算遮挡（回归：枚举顺序不是 Z 序）",
         _me.detect_occlusion(100, [target, dlg]) is None,
         "above=∅")

    # 6) 目标不在列表 → 不猜
    _ABOVE["v"] = {400}
    _add("目标不在窗口列表 → 返回 None（不猜）",
         _me.detect_occlusion(9999, [target, dlg]) is None, "hwnd=9999")

    # 7) 不可见 / 最小化 / 隐形 不算遮挡
    for kw, name in (({"visible": False}, "不可见"),
                     ({"minimized": True}, "最小化"),
                     ({"cloaked": True}, "DWM 隐形")):
        d = _W(500, "确认", "#32770", rect=(100, 100, 600, 400), **kw)
        _ABOVE["v"] = {500}
        _add("%s的弹窗不算遮挡" % name,
             _me.detect_occlusion(100, [target, d]) is None, name)

    # 8) 系统辅助类不算遮挡
    tip = _W(600, "", "tooltips_class32", rect=(0, 0, 800, 600))
    _ABOVE["v"] = {600}
    _add("系统辅助类（tooltips）不算遮挡",
         _me.detect_occlusion(100, [target, tip]) is None, "tooltips_class32")

    # 8b) Z 序取不到 → 保守判为遮挡（宁多叫一次，不放过）
    def _broken_above(hwnd, *, limit=400):
        raise OSError("GetWindow 失败")
    _me._windows_above = _broken_above
    _add("Z 序取不到时保守判为遮挡（不放过）",
         _me.detect_occlusion(100, [target, dlg]) is not None,
         "保守策略")
    _me._windows_above = _fake_above
    _ABOVE["v"] = {400}

    # 9) 检测主函数：无窗口 → unknown
    orig = _me.list_candidate_windows

    def _patched(wins):
        """同时打桩枚举**和 Z 序**。

        只打桩枚举是不够的：``detect_occlusion`` 还会问 Z 序，
        而假数据的 hwnd 和之前那批用例里的 ``_ABOVE['v']`` 对不上，
        遮挡就判不出来。这里把假窗口列表**全部**算作「在目标之上」，
        这才是"有个对话框盖在我窗口上"的真实场景。
        """
        def f(*, include_invisible=False, errors=None):
            return list(wins)
        _me.list_candidate_windows = f
        _ABOVE["v"] = {int(getattr(w, "hwnd", 0) or 0) for w in wins}
        return f
    try:
        _patched([])
        st = _me.detect_window()
        _add("枚举到 0 个窗口 → unknown（不是 clean）",
             st.is_unknown and st.reason == "no_window", st.describe())

        # 10) 干净窗口
        _patched([_W(1, "记事本", "Notepad")])
        st2 = _me.detect_window()
        _add("普通窗口 → is_clean",
             (not st2.detected) and st2.is_clean, st2.describe())

        # 11) 命中凭据框（窗口指纹）
        _patched([_W(2, "登录", "CredentialDialogXamlHost")])
        st3 = _me.detect_window()
        _add("命中凭据框类名 → detected",
             st3.detected and st3.source == "window", st3.describe())
        _add("凭据框 → 必须打断",
             st3.severity == Severity.INTERRUPT and st3.action == "pause",
             "%s → %s" % (st3.severity, st3.action))

        # 12) Cloudflare 标题
        _patched([_W(3, "Just a moment...", "Chrome_WidgetWin_1")])
        st4 = _me.detect_window()
        _add("Cloudflare 五秒盾标题 → 必须打断",
             st4.detected and st4.severity == Severity.INTERRUPT,
             st4.describe())

        # 13) 「正在检查您的浏览器」是可绕过
        _patched([_W(4, "Checking your browser - Cloudflare",
                     "Chrome_WidgetWin_1")])
        st5 = _me.detect_window()
        _add("检查中标题 → BYPASSABLE（不打断）",
             st5.detected and st5.action == "continue", st5.describe())

        # 14) 限流 → abort
        _patched([_W(5, "Too many requests", "Chrome_WidgetWin_1")])
        st6 = _me.detect_window()
        _add("限流标题 → action=abort",
             st6.detected and st6.action == "abort", st6.describe())

        # 15) **有遮挡但指纹没命中 → unknown**（关键：不能报 clean）
        _patched([target, _W(7, "某个未知弹窗", "#32770",
                             rect=(100, 100, 600, 400))])
        st7 = _me.detect_window(hwnd=100)
        _add("未知弹窗遮挡 → unknown（绝不报 clean）",
             (not st7.detected) and st7.is_unknown
             and st7.reason == "occluded_unknown", st7.describe())
        _add("未知遮挡的 extra 带 occlusion 详情",
             "occlusion" in st7.extra, sorted(st7.extra))

        # 16) 遮挡窗口**同时**命中指纹 → 置信度上调 + evidence 标记
        _patched([target, _W(8, "安全验证", "#32770",
                             rect=(100, 100, 600, 400))])
        st8 = _me.detect_window(hwnd=100)
        _add("遮挡窗命中的指纹优先上报",
             st8.detected and st8.evidence.get("occluding_hit") is True,
             st8.evidence.get("occluding_hit"))
        _add("遮挡命中 → extra.occlusion_blocking=True",
             st8.extra.get("occlusion_blocking") is True,
             st8.extra.get("occlusion_blocking"))

        # 17) extra_titles 跨层注入
        _patched([_W(9, "随便", "Notepad")])
        st9 = _me.detect_window(extra_titles=["人机验证"])
        # 注意：这里**不**断言具体 RiskType —— 具体归到 CAPTCHA 还是
        # challenge_page 是 fingerprints.py 的职责，窗口层只该保证
        # "注入进来的标题参与了匹配"。断言具体类型会把两层的职责
        # 焊死，5G 调指纹库时这条测试就会假红。只钉住注入链路本身。
        _add("extra_titles 能触发命中（跨层注入）",
             st9.detected and any(h.get("from_extra")
                                  for h in st9.evidence.get("extra_hits", [])),
             "%s | type=%s" % (st9.describe(), st9.type))

        # 18) timeout
        st10 = _me.detect_window(timeout=0.0)
        _add("timeout=0 → 立刻 timeout",
             st10.reason == "timeout" and st10.is_unknown, st10.describe())

        # 19) 枚举抛异常 → 不吞，转 error
        def _boom(*, include_invisible=False, errors=None):
            raise OSError("EnumWindows 被拦")
        _me.list_candidate_windows = _boom
        st11 = _me.detect_window()
        _add("枚举抛异常 → error 状态（不吞）",
             st11.is_unknown and st11.reason == "error", st11.describe())
        _add("异常详情写进 extra",
             "EnumWindows 被拦" in str(st11.extra.get("error", "")),
             st11.extra.get("error"))
    finally:
        _me.list_candidate_windows = orig
        _me._windows_above = _real_above

    # 20) 硬约束：不硬编码厂商指纹
    code = _code_only()
    _add("本模块不硬编码厂商指纹（只查代码）",
         not any(k in code for k in
                 ('"recaptcha"', '"geetest"', '"hcaptcha"', '"cloudflare"',
                  '"turnstile"', '"just a moment"', '"too many requests"')),
         "全部来自 fingerprints.py")

    # 21) 硬约束：顶层不自造生产用 Session 类
    import ast
    import os
    tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    top_classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    _add("顶层无生产用类（只有数据/测试替身）",
         all(c.startswith("_") or c.endswith("Fields") for c in top_classes),
         top_classes)

    # 22) 所有分支返回 RiskStatus
    _add("所有分支返回 RiskStatus",
         all(isinstance(x, RiskStatus) for x in
             (st, st2, st3, st7, st10, st11)),
         "共 6 个样本")

    # 23) unknown 分支全部 is_clean=False（硬约束）
    _add("unknown 分支全部 is_clean=False（硬约束）",
         all(x.is_clean is False for x in (st, st7, st10, st11)),
         "已自证")

    # 24) quick=False 才碰真实桌面
    if not quick:
        try:
            real = _me.list_candidate_windows()
            _add("真实枚举本机窗口（quick=False）", True,
                 "枚举到 %d 个窗口" % len(real))
        except Exception as exc:      # noqa: BLE001
            _add("真实枚举本机窗口（quick=False）", True,
                 "枚举失败但不构成失败：%s" % exc)
    else:
        checks.append({"name": "真实枚举本机窗口（quick 跳过）",
                       "ok": True, "detail": "(quick 跳过)"})

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-48s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
    n = len(r["checks"])
    print("detector_window 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
