# -*- coding: utf-8 -*-
"""元素查找：条件构造 + 多级回退策略。

这个模块回答一个问题：**"用户说的那个控件，到底是屏幕上的哪一个？"**

## 为什么不能只靠一个条件

人描述控件的方式和 UIA 的属性不是一一对应的。
"点登录" 里的"登录"可能是 ``Name``、可能是 ``AutomationId``、
可能是 ``LegacyIAccessible.Name``、也可能只在旁边的文本里。

所以查找不是"匹配一次"，而是**按可信度分级的回退链**：

    1. RuntimeId           —— 精确身份，最可靠（同一会话内）
    2. AutomationId        —— 开发者写死，跨会话稳定 → 首选
    3. Name（精确）         —— 用户可见文本
    4. Name（子串/正则）
    5. 复合条件（类型 + 文本）
    6. 相对定位（在某控件附近）
    7. LegacyIAccessible 属性
    8. → 全部失败，抛 ElementNotFound，**拒绝执行**

第 8 步是这个模块存在的意义。产品哲学里"元素定位优先、坐标兜底"，
但**兜底不是猜** —— 找不到就是找不到，交给上层决定要不要降级到视觉，
而不是在这里瞎选一个"最像的"。

## 打分而非过滤

一旦候选多于一个，就要选"最可能是用户想要的那个"。
打分维度（见 ``_score``）：

· 匹配强度（精确 > 前缀 > 子串）
· 属性来源（AutomationId > Name）
· 控件类型是否符合预期
· 是否可交互（enabled / 非 offscreen / focusable）
· 是否在指定窗口内
· 面积小者优先（更"具体"的控件，避免匹配到包住一切的 Panel）

## 坐标系约定

本模块返回的 ``Element`` 只带 ``hwnd + rect``，**不带屏幕坐标缓存**。
需要坐标时调 ``Element.client_rect()`` 当场换算（见 ``tree`` 的说明）。

## 依赖

· ``avatar.uia.tree`` —— 树遍历与元素快照
· ``avatar.uia.patterns`` —— 判断元素支持哪些模式（用于可交互性加权）
· ``avatar.errors`` —— ``ElementNotFound`` / ``LocateError``
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .._win32 import com as C
from ..errors import ElementNotFound, UiaUnavailable
from . import patterns as P
from . import tree as T

__all__ = [
    "Query", "query", "find", "find_all", "find_one", "exists", "count",
    "wait", "wait_gone", "closest", "normalize",
]

#: 找不到元素时的默认等待上限（秒）。超时后抛 ``ElementNotFound``。
DEFAULT_TIMEOUT = 5.0

#: 轮询间隔。UIA 查询本身有开销，50ms 是实测下来不浪费 CPU 又不迟钝的值。
POLL_INTERVAL = 0.05

#: 名称归一化时要去掉的字符（空白、全角空格、不间断空格）
_WS_RE = re.compile(r"[\s\u00a0\u3000]+")


# --------------------------------------------------------------------------
# 查询描述
# --------------------------------------------------------------------------
class Query:
    """一次元素查找的完整描述。

    用关键字参数描述"要找什么"，所有条件默认是**与**关系
    （全部满足才保留）。需要"或"用 ``alternatives``。

    例子::

        # 找记事本里的"文件"菜单
        q = Query(name="文件", control_type="menu_item")

        # 找任意一个确定/取消按钮
        q = Query.any_of(
            Query(name="确定", control_type="button"),
            Query(name="OK",   control_type="button"),
        )

        # 按 AutomationId 精确定位（最推荐）
        q = Query(automation_id="AddButton")

    ## 相对定位

    ``near`` 传一个元素或查询，表示"在这个元素附近找"。
    ``near_radius`` 限制距离（像素，按屏幕坐标算）。
    这对"同一行里的那个图标按钮"这类场景很有用 ——
    因为它的 Name 往往是空的，但位置有信息量。
    """

    __slots__ = (
        "name", "name_exact", "name_regex", "automation_id", "control_type",
        "class_name", "framework", "runtime_id", "window", "process_id",
        "text", "enabled", "focusable", "offscreen", "password",
        "interactive_only", "has_pattern", "predicate", "alternatives",
        "near", "near_radius", "max_depth", "scope",
    )

    def __init__(
        self,
        name: Optional[str] = None,
        *,
        name_exact: bool = False,
        name_regex: Optional[str] = None,
        automation_id: Optional[str] = None,
        control_type=None,
        class_name: Optional[str] = None,
        framework: Optional[str] = None,
        runtime_id: Optional[Sequence[int]] = None,
        window: int = 0,
        process_id: int = 0,
        text: Optional[str] = None,
        enabled: Optional[bool] = None,
        focusable: Optional[bool] = None,
        offscreen: Optional[bool] = None,
        password: Optional[bool] = None,
        interactive_only: bool = False,
        has_pattern: Optional[str] = None,
        predicate: Optional[Callable[[T.Element], bool]] = None,
        alternatives: Optional[Sequence["Query"]] = None,
        near=None,
        near_radius: int = 0,
        max_depth: int = T.MAX_DEPTH_DEFAULT,
        scope=None,
    ):
        self.name = name
        self.name_exact = name_exact
        self.name_regex = name_regex
        self.automation_id = automation_id
        #: 允许传 ``"button"`` 短名或 ``ControlType.BUTTON`` 数字或列表
        self.control_type = _norm_types(control_type)
        self.class_name = class_name
        self.framework = framework
        self.runtime_id = tuple(runtime_id) if runtime_id else None
        self.window = int(window or 0)
        self.process_id = int(process_id or 0)
        #: 同时匹配 name/value/help_text（"这个控件上写着 XX" 的宽松解释）
        self.text = text
        self.enabled = enabled
        self.focusable = focusable
        self.offscreen = offscreen
        self.password = password
        self.interactive_only = interactive_only
        self.has_pattern = has_pattern
        self.predicate = predicate
        self.alternatives = list(alternatives) if alternatives else None
        self.near = near
        self.near_radius = int(near_radius or 0)
        self.max_depth = int(max_depth)
        self.scope = scope

    # -- 便捷构造 --------------------------------------------------------
    @classmethod
    def any_of(cls, *queries: "Query") -> "Query":
        """多个候选条件，任一命中即可。按给定顺序优先。

        ⚠ 方法名不能叫 ``alternatives`` —— 和 ``__slots__`` 里的
        同名字段冲突（Python 会报 "conflicts with class variable"）。
        """
        return cls(alternatives=queries)

    @classmethod
    def by_name(cls, name: str, **kw) -> "Query":
        return cls(name=name, **kw)

    @classmethod
    def by_id(cls, automation_id: str, **kw) -> "Query":
        """按 AutomationId 查找 —— **最稳的定位方式**。"""
        return cls(automation_id=automation_id, **kw)

    @classmethod
    def by_type(cls, control_type, **kw) -> "Query":
        return cls(control_type=control_type, **kw)

    @classmethod
    def by_text(cls, text: str, **kw) -> "Query":
        """宽松匹配：名字、值、帮助文本任一包含即可。"""
        return cls(text=text, **kw)

    # -- 展示 ------------------------------------------------------------
    def describe(self) -> str:
        """给人和模型看的一句话。"""
        if self.alternatives:
            return " 或 ".join(q.describe() for q in self.alternatives)
        bits = []
        if self.runtime_id:
            bits.append("runtime_id=%s" % "-".join(str(x) for x in self.runtime_id))
        if self.automation_id:
            bits.append("id=%r" % self.automation_id)
        if self.name_regex:
            bits.append("name~/%s/" % self.name_regex)
        elif self.name is not None:
            bits.append("name%s=%r" % ("=" if self.name_exact else "≈", self.name))
        if self.text:
            bits.append("text≈%r" % self.text)
        if self.control_type:
            names = [T.control_name(c) for c in sorted(self.control_type)]
            bits.append("type∈{%s}" % ",".join(names))
        if self.class_name:
            bits.append("class≈%r" % self.class_name)
        if self.window:
            bits.append("window=0x%X" % self.window)
        if self.process_id:
            bits.append("pid=%d" % self.process_id)
        for flag, label in ((self.enabled, "enabled"),
                            (self.focusable, "focusable"),
                            (self.offscreen, "offscreen"),
                            (self.password, "password")):
            if flag is not None:
                bits.append("%s=%s" % (label, flag))
        if self.interactive_only:
            bits.append("可交互")
        if self.has_pattern:
            bits.append("支持%s" % self.has_pattern)
        if self.near is not None:
            bits.append("靠近%s%s" % (
                "元素" if not isinstance(self.near, Query) else self.near.describe(),
                "@%dpx" % self.near_radius if self.near_radius else "",
            ))
        if self.predicate is not None:
            bits.append("自定义谓词")
        return " ".join(bits) or "(无条件)"

    def __repr__(self):
        return "<Query %s>" % self.describe()


def _norm_types(control_type) -> set:
    """把 ``"button"`` / ``50000`` / ``[..]`` 统一成 ``set[int]``。"""
    if control_type is None:
        return set()
    if isinstance(control_type, (str, int)):
        control_type = [control_type]
    out = set()
    for item in control_type:
        if isinstance(item, int):
            out.add(item)
        else:
            out.add(T.control_id(str(item)))
    return out


def query(text: str = "", control_type=None, *, name_exact: bool = False,
          **kw) -> Query:
    """快捷构造。

    ``query("登录", "button")`` 等价于 ``Query(name="登录",
    control_type="button")``。
    """
    if name_exact:
        kw["name_exact"] = True
    return Query(name=text or None, control_type=control_type, **kw)


def normalize(s: str) -> str:
    """归一化名称：压掉所有空白、去掉首尾。

    UIA 的 Name 里经常混着多余空格 / 全角空格 / 不间断空格，
    而用户输入的是干净文本。不归一化就会"看起来一样却匹配不上"。
    """
    return _WS_RE.sub("", (s or "").strip())


# --------------------------------------------------------------------------
# 单元素匹配（内部）
# --------------------------------------------------------------------------
def _matches_one(el: T.Element, q: Query) -> Optional[int]:
    """检查单个 ``Element`` 是否满足 ``q``。

    返回匹配强度分数（越高越强）；不匹配返回 ``None``。

    强度分级：
        100  精确（runtime_id / automation_id / name 完全一致）
         80  前缀匹配
         60  子串匹配
         50  正则匹配
         40  仅文本字段命中
    """
    # 1) runtime_id —— 身份级，最强
    if q.runtime_id is not None:
        if tuple(el.runtime_id) != q.runtime_id:
            return None
        return 100

    score = 0

    # 2) automation_id —— 开发者定义的稳定 id
    if q.automation_id is not None:
        if normalize(el.automation_id) != normalize(q.automation_id):
            return None
        score = max(score, 100)

    # 3) 类型
    if q.control_type:
        if el.control_type not in q.control_type:
            return None
        score = max(score, 70)

    # 4) class / framework
    if q.class_name is not None:
        if normalize(q.class_name).lower() not in normalize(el.class_name).lower():
            return None
        score = max(score, 70)

    if q.framework is not None:
        if normalize(el.framework).lower() != normalize(q.framework).lower():
            return None

    # 5) name / 正则
    if q.name_regex:
        try:
            if not re.search(q.name_regex, el.name or "", re.I):
                return None
        except re.error:
            return None
        score = max(score, 50)
    elif q.name is not None:
        got, want = normalize(el.name), normalize(q.name)
        if q.name_exact:
            if got != want:
                return None
            score = max(score, 100)
        else:
            if not got or not want:
                return None
            if got == want:
                score = max(score, 100)
            elif got.startswith(want):
                score = max(score, 80)
            elif want in got:
                score = max(score, 60)
            else:
                return None

    # 6) 宽松文本：name / value / help_text 任一命中
    if q.text is not None:
        want = normalize(q.text)
        if not want:
            return None
        hay = (normalize(el.name) + normalize(el.help_text)).lower()
        hit = want.lower() in hay
        if not hit and el.patterns and "Value" in el.patterns:
            # 值在 Value 模式里，读起来有成本 —— 只在前面都没命中时才试
            try:
                vp = P.wrap(el, "Value", required=False)
                if vp is not None:
                    try:
                        hit = want.lower() in normalize(vp.value or "").lower()
                    finally:
                        vp.release()
            except Exception:
                hit = False
        if not hit:
            return None
        score = max(score, 40)

    # 7) 状态位
    if q.enabled is not None and bool(el.enabled) != bool(q.enabled):
        return None
    if q.focusable is not None and bool(el.focusable) != bool(q.focusable):
        return None
    if q.offscreen is not None and bool(el.offscreen) != bool(q.offscreen):
        return None
    if q.password is not None and bool(el.password) != bool(q.password):
        return None

    # 8) 范围与可交互
    if q.window and el.window != q.window:
        return None
    if q.process_id and el.process_id != q.process_id:
        return None
    if q.interactive_only and not el.is_interactive:
        return None

    # 9) 模式要求
    if q.has_pattern:
        pats = el.patterns or ()
        if pats:
            if q.has_pattern not in pats:
                return None
        else:
            # 元素没带模式快照 —— 现场探一次
            try:
                if q.has_pattern not in P.supported(el):
                    return None
            except Exception:
                return None

    # 10) 自定义谓词
    if q.predicate is not None:
        try:
            if not q.predicate(el):
                return None
        except Exception:
            return None

    return score if score else 55     # 纯状态条件也算命中


# --------------------------------------------------------------------------
# 打分（候选排序）
# --------------------------------------------------------------------------
def _area(el: T.Element) -> int:
    if el.rect is None:
        return 1 << 30
    try:
        return max(0, int(el.rect.width)) * max(0, int(el.rect.height))
    except Exception:
        return 1 << 30


def _score(el: T.Element, q: Query, base: int) -> float:
    """在匹配强度之上叠加"更像用户想要的那个"的加权。

    这些权重不是拍脑袋来的，每一条都对应一个实测踩过的坑：

    · **面积小者优先**：搜 "设置" 在 Edge 里会命中一个覆盖整窗的
      Pane 和一个 32px 的齿轮按钮。人指的是按钮。不惩罚大面积，
      就会选中那个 Pane，然后"点击"落在窗口正中。
    · **可交互者优先**：同一段文本常同时存在于 Text 控件和
      Button 上。人想点的通常是能点那个。
    · **enabled 优先**：禁用按钮点了没反应，不该被选中。
    · **非 offscreen 优先**：离屏元素点不到（需要先滚动）。
    · **靠前深度优先**：浅层元素通常是"主干"，深层多是包装。
    """
    s = float(base)

    # 精确 id 命中就基本定了，不再让几何因素干扰
    if base >= 100:
        s += 20.0

    if el.is_interactive:
        s += 12.0
    if el.enabled:
        s += 8.0
    else:
        s -= 25.0
    if el.offscreen:
        s -= 30.0
    if el.focusable:
        s += 3.0
    if el.patterns:
        # 有可执行模式的元素更可能是目标
        if any(p in P.PATTERN_VERBS for p in el.patterns):
            s += 6.0

    # 面积惩罚：对数化，避免超大容器把分数打到负无穷
    import math
    area = _area(el)
    if area < (1 << 30):
        s -= min(18.0, math.log10(max(1, area)) * 1.6)

    # 深度惩罚：浅层优先
    s -= min(6.0, max(0, el.depth) * 0.4)

    return s


# --------------------------------------------------------------------------
# 候选获取：三条通道，从快到慢
# --------------------------------------------------------------------------
def _enumerate_windows(process_id: int = 0) -> List[T.Element]:
    """通道 A：用 Win32 枚举顶层窗口（**最快**，毫秒级）。

    为什么单列一条通道：UIA 的 ``FindAll`` 从桌面根往下搜**不会跨进程
    进入别的应用窗口**（实测：全桌面搜 Window 只返回 1 个）。
    而 Win32 的 ``EnumWindows`` 天然就是全桌面的，而且极快。

    所以"找窗口"这类查询走这条路；"找窗口里的控件"再进通道 B。
    """
    from ..app_manager import windows as W
    out = []
    for info in W.list_windows(include_invisible=True):
        if process_id and info.pid != process_id:
            continue
        try:
            out.append(T.from_window(int(info.hwnd)))
        except Exception:
            continue
    return out


def _candidates_via_uia(root_el, q: Query, match_q: Query):
    """通道 B：UIA 原生 ``FindAll``（快，秒级以内）。

    把能表达成"属性等值"的条件**下推**到 UIA 内部执行。
    实测收益：全桌面搜 button 从 30 秒级降到 1 秒级。

    ## 返回约定（关键）

    返回 ``None`` 表示**"这条通道表达不了这个条件，请走通道 C"**；
    返回 ``[]`` 表示**"下推执行成功，确实是零结果"**。

    这两个必须区分开！早期实现把两者都返回 ``[]``，
    于是"找不到"会被误判成"下推不了"，接着去跑几十秒的全遍历 ——
    一次失败查询要 25 秒。现在用 ``None`` 明确表达"不适用"。
    """
    prop_id = 0
    value = None
    control_types = None

    # runtime_id 无法用属性条件下推
    if match_q.runtime_id is not None:
        return None

    if match_q.automation_id is not None:
        prop_id = T.UIA_PROPERTY_IDS["automation_id"]
        value = match_q.automation_id
    elif match_q.name is not None and match_q.name_exact:
        prop_id = T.UIA_PROPERTY_IDS["name"]
        value = match_q.name
    elif match_q.name or match_q.name_regex or match_q.text \
            or match_q.class_name or match_q.framework \
            or match_q.predicate:
        # 有"难以下推"的条件（子串 / 正则 / 谓词）→ 交给全遍历
        return None

    if match_q.control_type and len(match_q.control_type) == 1:
        control_types = next(iter(match_q.control_type))

    if not prop_id and not control_types:
        return None        # 纯状态条件（enabled/offscreen...）下推不了

    try:
        ptrs = T.find_uia(root_el, prop_id, value,
                          control_types=control_types,
                          max_results=T.MAX_DESCENDANTS)
    except Exception:
        return None

    out = []
    host_hwnd = getattr(root_el, "window", 0) or getattr(root_el, "hwnd", 0)
    for p in ptrs:
        try:
            # borrowed() 复制属性后不持有引用
            el = T.borrowed(p, hwnd=int(host_hwnd))
            out.append(el)
        except Exception:
            pass
        finally:
            # find_uia 返回的指针带一份属于我们的引用 —— 用完还掉。
            # 只在这里释放一次（之前写成两处 → 双重 Release → 崩溃）
            _release_found(p)
    # 注意：这里返回 [] 是**有意义的零结果**，不是"不适用"
    return out


def _release_found(ptr: int) -> None:
    """释放 ``find_uia`` 返回指针上属于我们的那份引用。"""
    try:
        C.ComPtr(ptr, name="found-owner").release()
    except Exception:
        pass


def _candidates_via_walk(root_el, q: Query, match_q: Query) -> List[T.Element]:
    """通道 C：Python 侧全遍历（慢，但表达力最强）。

    只有通道 A/B 搞不定时才走这里。对单窗口（几百个元素）是可接受的；
    对全桌面（几千个）会到几十秒 —— 所以上层要先缩小 scope。
    """
    try:
        return T.descendants(root_el, kind=T.VIEW_CONTROL,
                             max_depth=match_q.max_depth,
                             limit=T.MAX_DESCENDANTS,
                             with_patterns=bool(match_q.has_pattern))
    except Exception:
        return []


# --------------------------------------------------------------------------
# 作用域
# --------------------------------------------------------------------------
def _resolve_scope(scope, window: int, process_id: int):
    """把 ``scope`` 参数解析成一个 (元素, hwnd) 对。"""
    if scope is not None:
        if isinstance(scope, T.Element):
            return scope, (scope.window or scope.hwnd or window)
        if isinstance(scope, int):
            return T.from_window(scope), scope
        # 当作 Query：只允许单个结果，否则用第一个窗口兜底
        found = find_all(scope, window=window, process_id=process_id, limit=1)
        if found:
            return found[0], (found[0].window or found[0].hwnd)
        raise ElementNotFound("作用域查询没有匹配到任何元素",
                              query=scope.describe())

    if window:
        return T.from_window(window), window
    return T.root(), 0


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
def find_all(q, *, window: int = 0, process_id: int = 0, scope=None,
             limit: int = 0) -> List[T.Element]:
    """找出所有匹配元素，按"最像目标"降序返回。

    ``q`` 可以是 ``Query``，也可以直接给字符串（按 name 子串找）。

    ## 三条候选通道，从快到慢

    | 通道 | 手段 | 耗时量级 | 适用 |
    |------|------|----------|------|
    | A | Win32 ``EnumWindows`` | 毫秒 | 找窗口 |
    | B | UIA 原生 ``FindAll`` | 1 秒内 | 找某窗口内的控件（条件可下推） |
    | C | Python 全遍历 | 数十秒 | 条件复杂（正则/谓词）或需模式探测 |

    选路规则：**能用快通道就不用慢通道**。
    早期实现只有通道 C，结果"全桌面找按钮"要 30 秒以上 —— 不可用。
    """
    if isinstance(q, str):
        q = Query(name=q)
    if not isinstance(q, Query):
        raise TypeError("find_all 需要 Query 或 str，收到 %r" % type(q).__name__)

    # 合并调用方传入的作用域限制
    if window and not q.window:
        q = _derive(q, window=window)
    if process_id and not q.process_id:
        q = _derive(q, process_id=process_id)

    try:
        root_el, host = _resolve_scope(scope, q.window, q.process_id)
    except Exception:
        return []

    if root_el is None:
        return []

    # 相对定位：先把参照物找出来，再用它的矩形做距离筛选
    near_rect = None
    if q.near is not None:
        near_el = q.near
        if isinstance(near_el, Query):
            got = find_all(near_el, window=q.window, process_id=q.process_id,
                           scope=root_el, limit=1)
            near_el = got[0] if got else None
        if isinstance(near_el, T.Element) and near_el.rect is not None:
            near_rect = near_el.rect
        else:
            return []

    # 给匹配用的条件副本：去掉 near（它不参与单元素判定）
    match_q = _derive(q, near=None) if q.near is not None else q

    alts = q.alternatives or [match_q]
    cands = []

    for one in alts:
        pool = _gather_candidates(root_el, q, one)
        for el in pool:
            base = _matches_one(el, one)
            if base is None:
                continue
            if near_rect is not None and not _near_enough(el, near_rect,
                                                          q.near_radius):
                continue
            cands.append((_score(el, one, base), el))

    if not cands:
        return []

    # 去重（同一元素可能被多个 alternatives 命中），保留最高分
    best: Dict[str, Tuple[float, T.Element]] = {}
    for s, el in cands:
        k = el.key
        if k not in best or s > best[k][0]:
            best[k] = (s, el)

    ordered = [el for _, el in sorted(best.values(),
                                      key=lambda t: -t[0])]
    return ordered[:limit] if limit else ordered


def _gather_candidates(root_el, q: Query, one: Query) -> List[T.Element]:
    """按"从快到慢"选通道拿候选集。"""
    # 通道 A：找窗口类目标时，Win32 枚举远比 UIA 快且覆盖面全
    if one.control_type == {T.ControlType.WINDOW}:
        wins = _enumerate_windows(one.process_id)
        if wins:
            # 已经按窗口找过了，再按窗口过滤一次
            if q.window:
                wins = [w for w in wins if w.hwnd == q.window
                        or w.window == q.window]
            return wins

    # 通道 B：UIA 原生下推。None = 不适用；[] = 真的没找到
    got = _candidates_via_uia(root_el, q, one)
    if got is not None:
        return got

    # 通道 C：全遍历兜底（只有下推不了时才来）
    return _candidates_via_walk(root_el, q, one)


def _near_enough(el: T.Element, near_rect, radius: int) -> bool:
    """元素是否落在参照物附近。``radius=0`` 表示不限距离（只要同一个窗口）。"""
    if el.rect is None:
        return False
    if radius <= 0:
        return True
    try:
        # 用中心点距离 —— 比矩形相交更符合"旁边那个"的直觉
        cx, cy = el.rect.center
        nx, ny = near_rect.center
        dx = abs(cx - nx) - (near_rect.width + el.rect.width) / 2.0
        dy = abs(cy - ny) - (near_rect.height + el.rect.height) / 2.0
        dx = max(0.0, dx)
        dy = max(0.0, dy)
        import math
        return math.hypot(dx, dy) <= radius
    except Exception:
        return True


def _derive(q: Query, **overrides) -> Query:
    """基于已有 Query 造一个改了若干字段的副本。"""
    kw = {slot: getattr(q, slot, None) for slot in Query.__slots__}
    kw.update(overrides)
    return Query(**kw)


def find_one(q, *, window: int = 0, process_id: int = 0, scope=None,
             required: bool = True) -> Optional[T.Element]:
    """找"最像目标"的那一个。

    ``required=False`` 时找不到返回 ``None``；默认抛 ``ElementNotFound``
    —— 这是"定位不到就不许动"的执行点。
    """
    got = find_all(q, window=window, process_id=process_id, scope=scope,
                   limit=1)
    if got:
        return got[0]
    if not required:
        return None
    desc = q.describe() if isinstance(q, Query) else repr(q)
    raise ElementNotFound("没有找到匹配的元素：%s" % desc, query=desc,
                          hwnd=window)


#: ``find`` 是 ``find_one`` 的短名（对外主入口）
find = find_one


def exists(q, *, window: int = 0, process_id: int = 0, scope=None) -> bool:
    """存在性判断 —— 不该抛异常，用这个。"""
    try:
        return find_all(q, window=window, process_id=process_id, scope=scope,
                        limit=1) != []
    except Exception:
        return False


def count(q, *, window: int = 0, process_id: int = 0, scope=None) -> int:
    """匹配到的元素个数。"""
    try:
        return len(find_all(q, window=window, process_id=process_id,
                            scope=scope))
    except Exception:
        return 0


def wait(q, *, window: int = 0, process_id: int = 0, scope=None,
         timeout: float = DEFAULT_TIMEOUT,
         poll: float = POLL_INTERVAL) -> Optional[T.Element]:
    """等元素出现。超时抛 ``ElementNotFound``（附已等待时长）。

    ## 为什么必须"轮询 + 超时"

    现代 UI 是异步渲染的。点完一个按钮后立刻找结果控件，
    十有八九还没创建出来。固定 ``sleep`` 要么不够（偶发失败）
    要么太长（白等）。轮询是唯一稳的做法。

    注意 UIA 每次查询都要跨进程，所以轮询间隔不能太小 ——
    50ms 是实测的平衡点。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    started = time.monotonic()
    last_err = None
    while True:
        try:
            got = find_all(q, window=window, process_id=process_id,
                           scope=scope, limit=1)
            if got:
                return got[0]
        except UiaUnavailable:
            raise
        except Exception as exc:      # noqa: BLE001
            last_err = exc
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.005, poll))

    desc = q.describe() if isinstance(q, Query) else repr(q)
    detail = "没有找到匹配的元素：%s" % desc
    if last_err is not None:
        detail += "（最后一次错误：%s）" % last_err
    raise ElementNotFound(detail, query=desc, hwnd=window)


def wait_gone(q, *, window: int = 0, process_id: int = 0, scope=None,
              timeout: float = DEFAULT_TIMEOUT,
              poll: float = POLL_INTERVAL) -> bool:
    """等元素消失（对话框关闭、加载提示消失）。

    超时返回 ``False``（不抛异常 —— "还在这儿"是个正常答案，
    调用方可能只是想知道）。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if not find_all(q, window=window, process_id=process_id,
                            scope=scope, limit=1):
                return True
        except Exception:
            return True       # 查不动了（窗口没了）也算消失
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.005, poll))


def closest(el: T.Element, *, control_type=None, name: Optional[str] = None,
            direction: str = "any", limit: int = 1) -> List[T.Element]:
    """在同一窗口里找离 ``el`` 最近的元素。

    ``direction``：``any`` / ``right`` / ``left`` / ``above`` / ``below``。
    "同一行"取 ``right`` 或 ``left``。
    """
    if el.rect is None:
        return []
    q = Query(control_type=control_type, name=name) if (control_type or name) \
        else Query()
    pool = find_all(q, window=el.window, limit=0)
    if not pool:
        return []

    x0, y0 = el.rect.center
    scored = []
    for other in pool:
        if other.key == el.key or other.rect is None:
            continue
        x1, y1 = other.rect.center
        dx, dy = x1 - x0, y1 - y0
        if direction == "right" and dx <= 0:
            continue
        if direction == "left" and dx >= 0:
            continue
        if direction == "below" and dy <= 0:
            continue
        if direction == "above" and dy >= 0:
            continue
        import math
        dist = math.hypot(dx, dy)
        # 同方向的横向/纵向排布优先：垂直偏差大的"同行"元素往后排
        bias = 0.0
        if direction in ("right", "left"):
            bias = abs(dy) * 1.5
        elif direction in ("above", "below"):
            bias = abs(dx) * 1.5
        scored.append((dist + bias, other))
    scored.sort(key=lambda t: t[0])
    return [e for _, e in scored[:limit]] if limit else [e for _, e in scored]


def selfcheck() -> dict:
    """不依赖具体应用的自检。"""
    checks, failures = [], []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. 名称归一化
    _add("名称归一化吞掉空白", normalize("  文件\n") == "文件"
         and normalize("a\u00a0 b\u3000c") == "abc", "空白/全角/不间断全压掉")

    # 2. 类型名解析
    _add("类型短名可解析", T.control_id("button") == T.ControlType.BUTTON
         and T.control_id("menu_item") == T.ControlType.MENU_ITEM,
         "button/menu_item → 数字")
    # 3. Query.describe
    q = Query(name="登录", control_type="button", enabled=True)
    _add("Query 可读描述", "登录" in q.describe() and "button" in q.describe(),
         q.describe())

    # 4. alternatives
    qa = Query.any_of(Query(name="确定"), Query(name="OK"))
    _add("any_of 生效", "或" in qa.describe(), qa.describe())

    # ---- 实机部分 ----------------------------------------------------------
    # ⚠ 性能红线：selfcheck 必须能"随手跑"。
    #
    # 早期这里所有查询都不限定作用域，于是每个"找不到"的查询都会掉进
    # 通道 C 的全桌面遍历（桌面树 30 万节点量级），实测整项 51.7 秒 ——
    # 而其余 13 个模块 selfcheck 加起来才 8.8 秒。selfcheck 慢到没人愿意用，
    # 等于没有。
    #
    # 修法：下面凡"预期找不到"的查询一律**限定到一个具体窗口**。
    # 语义不变（仍然是"找不到要抛 ElementNotFound"），但搜索空间
    # 从整棵桌面树缩到一个窗口子树。
    #
    # 全桌面路径的覆盖交给 tests/test_phase2_uia.py —— 那儿有全套
    # 跨窗口、多通道的验证，慢一点无所谓。
    probe = _pick_probe_window()

    if probe is None:
        # 没有可用窗口：跳过实机项，但如实记录"跳过"而不是谎报通过
        _add("实机检查", True, "无可用窗口，实机项跳过（不计入失败）")
        return {"checks": checks, "failures": failures, "ok": not failures}

    probe_hwnd = probe.hwnd

    # 5. 按类型找顶层窗口（实机，走窗口枚举通道）
    try:
        got = find_all(Query(control_type="window"), limit=3)
        _add("能按类型找顶层窗口", len(got) > 0, "找到 %d 个 window" % len(got))
    except Exception as exc:      # noqa: BLE001
        _add("能按类型找顶层窗口", False, "%s: %s" % (type(exc).__name__, exc))

    # 6. 找不到时抛 ElementNotFound（而不是返回 None）—— 限定窗口内
    try:
        find_one(Query(automation_id="绝不存在的控件ID_XYZ_12345",
                       window=probe_hwnd))
        _add("找不到抛 ElementNotFound", False, "没有抛异常")
    except ElementNotFound:
        _add("找不到抛 ElementNotFound", True, "正确抛出")
    except Exception as exc:      # noqa: BLE001
        _add("找不到抛 ElementNotFound", False,
             "抛了别的：%s" % type(exc).__name__)

    # 7. required=False 返回 None
    _add("required=False 返回 None",
         find_one(Query(automation_id="绝不存在的控件ID_XYZ_12345",
                        window=probe_hwnd), required=False) is None,
         "不抛异常")

    # 8. exists / count 不抛 —— 同样限定窗口
    _add("exists/count 安全返回",
         exists(Query(automation_id="绝不存在的控件ID_XYZ",
                      window=probe_hwnd)) is False
         and count(Query(automation_id="绝不存在的控件ID_XYZ",
                         window=probe_hwnd)) == 0,
         "未知条件 → False / 0")

    # 9. wait 超时抛 ElementNotFound 且带耗时语义
    try:
        t0 = time.monotonic()
        wait(Query(automation_id="绝不存在的控件ID_XYZ",
                   window=probe_hwnd), timeout=0.3)
        _add("wait 超时抛异常", False, "没有抛")
    except ElementNotFound:
        elapsed = time.monotonic() - t0
        _add("wait 超时抛异常", elapsed >= 0.25,
             "等待 %.2fs 后抛出" % elapsed)
    except Exception as exc:      # noqa: BLE001
        _add("wait 超时抛异常", False, "抛了别的：%s" % type(exc).__name__)

    # 10. wait_gone 立即返回（元素本来就不存在）
    _add("wait_gone 对不存在元素返回 True",
         wait_gone(Query(automation_id="绝不存在的控件ID_XYZ",
                         window=probe_hwnd), timeout=0.3) is True,
         "立即判定已消失")

    # 11. str 直接当 name 查询
    try:
        find_all("绝不存在的标题XYZ", window=probe_hwnd, limit=1)
        _add("字符串可直接当查询", True, "str → Query(name=...)")
    except ElementNotFound:
        # 限定窗口后确实找不到，这也算"查询被正确构造并执行了"
        _add("字符串可直接当查询", True, "str → Query(name=...)，窗口内未命中")
    except Exception as exc:      # noqa: BLE001
        _add("字符串可直接当查询", False, "%s: %s" % (type(exc).__name__, exc))

    # 12. 非法入参类型被拒绝（纯参数检查，不涉及搜索）
    try:
        find_all(12345)
        _add("非法查询类型被拒绝", False, "没有抛 TypeError")
    except TypeError:
        _add("非法查询类型被拒绝", True, "抛 TypeError")

    # 13. 实机：窗口内能找到控件（走通道 B 原生下推）
    try:
        inner = find_all(Query(control_type="button"), window=probe_hwnd,
                         limit=50)
        _add("窗口内可查控件", True,
             "0x%X 内 %d 个按钮" % (probe_hwnd, len(inner)))
    except Exception as exc:      # noqa: BLE001
        _add("窗口内可查控件", False, "%s: %s" % (type(exc).__name__, exc))

    # 14. 只查窗口元素时不跨窗口串味
    try:
        in_win = find_all(Query(control_type="button"), window=probe_hwnd,
                          limit=50)
        leaked = [e for e in in_win if e.window != probe_hwnd]
        _add("窗口作用域不泄漏", not leaked,
             "%d 个按钮，越界 %d 个" % (len(in_win), len(leaked)))
    except Exception as exc:      # noqa: BLE001
        _add("窗口作用域不泄漏", False, "%s: %s" % (type(exc).__name__, exc))

    return {"checks": checks, "failures": failures, "ok": not failures}


def _pick_probe_window():
    """给 selfcheck 挑一个"便宜"的实机靶子窗口。

    ⚠ 挑**控件少**的窗口。曾经直接取第一个可用窗口，结果偶尔挑到浏览器
    —— 那里面几百个控件，光遍历就 8 秒，整项从 6s 涨到 14s。
    记事本这类小窗口是理想靶子：有按钮、有输入区，但只有几十个控件。

    用 ``windows.list_windows()`` 而不是全桌面 UIA 遍历，后者太慢。
    """
    try:
        from ..app_manager import windows as W
        candidates = [w for w in W.list_windows() if w.is_usable]
    except Exception:      # noqa: BLE001
        return None
    if not candidates:
        return None

    # 优先记事本（小、稳定、控件类型齐全）
    for w in candidates:
        if "notepad" in (w.process or "").lower():
            return w

    # 否则挑**面积最小**的 —— 小窗口通常控件少
    def _area(w):
        return max(0, w.width) * max(0, w.height)

    return min(candidates, key=_area)


def _main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("UIA 元素查找自检:")
    res = selfcheck()
    for c in res["checks"]:
        print("  [%s] %-32s %s"
              % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
    total = len(res["checks"])
    print("find 自检: %d/%d" % (total - len(res["failures"]), total))
    return 1 if res["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
