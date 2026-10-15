# -*- coding: utf-8 -*-
"""元素转可读文本 —— 给人和给模型的两套形态。

## 为什么需要两套

**给人看的**（``describe`` / ``table`` / ``tree``）：
要紧凑、能扫、能一眼定位。列对齐，颜色标记状态，一行一个元素。

**给模型看的**（``to_prompt`` / ``to_json`` / ``to_click_target``）：
要有稳定的字段名、明确的类型、足够的上下文（在哪、能不能点、怎么点）。
模型不需要对齐漂亮，但需要**信息完整且无歧义** —— 比如"这个元素能点"
必须显式给出，不能让模型从控件类型去猜。

## 核心设计：``to_click_target``

这是整个视觉/动作层与模型之间的**契约**。一个可点击目标的完整描述：

    {
      "index": 3,                    # 本轮的句柄号，后续动作用它引用
      "name": "登录",
      "type": "button",
      "automation_id": "loginBtn",
      "window": 201234,              # 顶层窗口句柄
      "client_rect": [10, 200, 80, 32],   # **窗口客户区相对坐标**
      "screen_rect": [..],           # 仅作参考，模型不该依赖它
      "patterns": ["Invoke"],        # 可用模式 —— 决定"怎么点"
      "preferred_action": "invoke",  # 推荐动作
      "enabled": true,
      "offscreen": false
    }

模型拿到的 ``screen_rect`` 只是**参考信息**。真正执行时必须用
``window + client_rect`` 重新换算 —— 因为模型从"看到"到"决定点击"
可能隔了几百毫秒，窗口可能已经移动了。

## 反模式（明确不做）

· ❌ 输出完整 DOM 式嵌套树给模型 —— token 爆炸且模型抓不住重点
· ❌ 省略 ``enabled`` / ``offscreen`` —— 模型会去点禁用按钮
· ❌ 只给坐标不给语义 —— 无法做重试和验证

## 依赖

· ``avatar.uia.tree`` —— ``Element`` 快照
· ``avatar.uia.patterns`` —— 判断可用动作
· ``avatar.app_manager.layout`` —— 客户区 ↔ 屏幕坐标换算
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence

from . import patterns as P
from . import tree as T

__all__ = [
    "describe", "one_line", "table", "tree", "markup",
    "to_dict", "to_json", "to_prompt", "to_click_target", "actions_of",
    "summarize", "compare",
]

#: 一行里名称列的最大宽度（超出截断，避免一个长标题把表格撑爆）
NAME_WIDTH = 34

#: ``control_type`` → 中文短名。给"给人看"的形态用。
TYPE_ZH = {
    T.ControlType.BUTTON: "按钮",
    T.ControlType.EDIT: "编辑框",
    T.ControlType.DOCUMENT: "文档",
    T.ControlType.LIST: "列表",
    T.ControlType.LIST_ITEM: "列表项",
    T.ControlType.MENU: "菜单",
    T.ControlType.MENU_BAR: "菜单栏",
    T.ControlType.MENU_ITEM: "菜单项",
    T.ControlType.TEXT: "文本",
    T.ControlType.IMAGE: "图片",
    T.ControlType.CHECK_BOX: "复选框",
    T.ControlType.RADIO_BUTTON: "单选框",
    T.ControlType.COMBO_BOX: "下拉框",
    T.ControlType.TAB: "选项卡",
    T.ControlType.TAB_ITEM: "标签页",
    T.ControlType.TREE: "树",
    T.ControlType.TREE_ITEM: "树节点",
    T.ControlType.TOOL_BAR: "工具栏",
    T.ControlType.STATUS_BAR: "状态栏",
    T.ControlType.WINDOW: "窗口",
    T.ControlType.PANE: "面板",
    T.ControlType.HYPERLINK: "链接",
    T.ControlType.SLIDER: "滑块",
    T.ControlType.SCROLL_BAR: "滚动条",
    T.ControlType.PROGRESS_BAR: "进度条",
    T.ControlType.SPINNER: "微调框",
    T.ControlType.DATA_GRID: "数据表格",
    T.ControlType.TABLE: "表格",
    T.ControlType.DATA_ITEM: "数据项",
    T.ControlType.HEADER: "表头",
    T.ControlType.HEADER_ITEM: "表头项",
    T.ControlType.TOOL_TIP: "提示",
    T.ControlType.SPLIT_BUTTON: "分裂按钮",
    T.ControlType.THUMB: "滚动块",
    T.ControlType.TITLE_BAR: "标题栏",
    T.ControlType.SEPARATOR: "分隔符",
    T.ControlType.GROUP: "分组",
    T.ControlType.CUSTOM: "自定义",
    T.ControlType.CALENDAR: "日历",
    T.ControlType.APPBAR: "应用栏",
    T.ControlType.SEMANTIC_ZOOM: "缩放视图",
}


def _type_label(el: T.Element, zh: bool = False) -> str:
    if zh:
        return TYPE_ZH.get(el.control_type) or el.localized_type or el.type_name
    return el.type_name


def _clip(s: str, width: int) -> str:
    s = s or ""
    if len(s) <= width:
        return s
    return s[:max(1, width - 1)] + "…"


# --------------------------------------------------------------------------
# 给人看
# --------------------------------------------------------------------------
def one_line(el: T.Element, index: Optional[int] = None,
             width: int = NAME_WIDTH, zh: bool = True) -> str:
    """单个元素一行。``index`` 非空时行首带句柄号（用于后续引用）。"""
    flags = []
    if not el.enabled:
        flags.append("禁用")
    if el.offscreen:
        flags.append("离屏")
    if el.focused:
        flags.append("焦点")
    if el.password:
        flags.append("密码")
    tail = (" [" + ",".join(flags) + "]") if flags else ""

    head = ("%3d  " % index) if index is not None else ""
    return "%s%-6s %-*s %s%s" % (
        head, _type_label(el, zh), width, _clip(el.name or "(无名)", width),
        el.automation_id or "-", tail,
    )


def describe(el: T.Element, index: Optional[int] = None) -> str:
    """单个元素的**完整**细节 —— 当一个元素需要被仔细看时用。

    与 ``one_line`` 的区别：这里把 rect / 客户区 / 模式 / 层级都摊开，
    适合"为什么点错了"这类排查场景。
    """
    lines = []
    lines.append("元素 #%s" % (index if index is not None else "-"))
    lines.append("  类型      %s (%s)" % (_type_label(el, True),
                                        el.localized_type or el.type_name))
    lines.append("  名称      %r" % (el.name or ""))
    if el.automation_id:
        lines.append("  ID        %s" % el.automation_id)
    if el.class_name:
        lines.append("  类名      %s" % el.class_name)
    if el.framework:
        lines.append("  框架      %s" % el.framework)
    lines.append("  进程      pid=%d" % el.process_id)
    lines.append("  窗口      0x%X" % (el.window or 0))
    if el.native_hwnd:
        lines.append("  自身句柄  0x%X" % el.native_hwnd)

    rect = el.rect
    lines.append("  屏幕矩形  %s" % (rect if rect is not None else "(无)"))
    cr = el.client_rect()
    lines.append("  客户区矩形 %s" % (cr if cr is not None else "(无)"))

    state = []
    state.append("可用" if el.enabled else "**禁用**")
    if el.focused:
        state.append("有焦点")
    if el.focusable:
        state.append("可聚焦")
    if el.offscreen:
        state.append("**离屏**")
    if el.password:
        state.append("**密码框**")
    lines.append("  状态      %s" % ", ".join(state))

    pats = list(el.patterns or ())
    if pats:
        lines.append("  模式      %s" % ", ".join(sorted(pats)))
    acts = P.pattern_actions(el) if pats else []
    if acts:
        lines.append("  可做      %s" % ", ".join(acts))
        best = P.best_pattern(el, list(P.PATTERN_PRIORITY))
        lines.append("  首选动作  %s" % (best or "(无，需坐标兜底)"))

    if el.help_text:
        lines.append("  帮助      %s" % el.help_text)
    if el.access_key:
        lines.append("  访问键    %s" % el.access_key)
    if el.accelerator:
        lines.append("  加速键    %s" % el.accelerator)

    lines.append("  层级      深度 %d%s" % (
        el.depth, "（占位元素）" if el.synthetic else ""))
    return "\n".join(lines)


def table(elements: Sequence[T.Element], *, indexed: bool = True,
          zh: bool = True, title: str = "") -> str:
    """列表形式 —— 最常用的"给人看"形态。

    列：``#  类型  名称  AutomationId  状态``
    """
    if not elements:
        return "（没有元素）"
    head = ("%3s  " % "#") if indexed else ""
    head += "%-6s %-*s %s" % ("类型" if zh else "type",
                              NAME_WIDTH, "名称" if zh else "name",
                              "AutomationId")
    lines = []
    if title:
        lines.append(title)
    lines.append(head)
    lines.append("-" * (len(head) + 12))
    for i, el in enumerate(elements):
        lines.append(one_line(el, index=i if indexed else None,
                              zh=zh))
    return "\n".join(lines)


def tree(elements: Sequence[T.Element], *, zh: bool = True,
         max_lines: int = 0) -> str:
    """缩进树形态 —— 看层级关系时用。

    ``Element`` 快照里带 ``depth``，直接用它做缩进。
    注意这**不是**从 root 开始的完整树，只是给定列表按深度画的形状。
    """
    if not elements:
        return "（没有元素）"
    out = []
    for el in elements:
        out.append("  " * max(0, el.depth) + one_line(el, zh=zh))
        if max_lines and len(out) >= max_lines:
            out.append("  ...（已截断）")
            break
    return "\n".join(out)


def markup(elements: Sequence[T.Element], *, limit: int = 60) -> str:
    """带编号的紧凑清单，**给模型定位用**（但用 XML 风格包裹）。

    比 ``table`` 更省 token，因为去掉了对齐填充和分隔线；
    编号是后续动作的引用句柄。
    """
    out = ["<elements count=\"%d\">" % min(len(elements), limit)]
    for i, el in enumerate(elements[:limit]):
        attrs = [
            'i="%d"' % i,
            'type="%s"' % _type_label(el, False),
            'name="%s"' % _esc(el.name or ""),
        ]
        if el.automation_id:
            attrs.append('id="%s"' % _esc(el.automation_id))
        if not el.enabled:
            attrs.append('enabled="false"')
        if el.offscreen:
            attrs.append('offscreen="true"')
        acts = P.pattern_actions(el) if el.patterns else []
        if acts:
            attrs.append('actions="%s"' % ",".join(acts))
        out.append("  <e %s/>" % " ".join(attrs))
    out.append("</elements>")
    return "\n".join(out)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------
# 给模型看
# --------------------------------------------------------------------------
def actions_of(el: T.Element) -> List[str]:
    """元素可执行的语义动作名（``click`` / ``set_value`` / ...）。

    ## 快照没带模式时会现探

    ``Element.patterns`` 只有在 ``descendants(..., with_patterns=True)``
    时才会被填充。用默认参数拿到的快照 ``patterns=()`` —— 这**不表示
    "不支持任何模式"**，只表示"没探测过"。

    所以这里要区分两种情况：

    · ``with_patterns=True`` 过，且探到 0 个模式 → 真的什么都不支持
    · 从没探测过 → 现场探一次（多一次跨进程调用，可接受）

    遗憾的是 ``Element`` 上目前没有"是否探测过"的标记位，
    而探测本身对**已经失效的元素**会失败。所以：

    · 元素还能定位到 → 现探，拿到准确结果
    · 元素已失效（窗口关了 / 被重建）→ 返回空表，**不抛异常**
      （``actions_of`` 是展示用途，不该因为探不到就炸掉整个描述流程）
    """
    try:
        return sorted(set(P.PATTERN_VERBS.get(p, "") for p in _patterns_of(el))
                      - {""})
    except Exception:
        return []


def _patterns_of(el: T.Element) -> List[str]:
    """取元素的模式列表：优先快照，快照为空则现探。"""
    if el.patterns:
        return list(el.patterns)
    try:
        return list(P.supported(el))
    except Exception:
        return []


def to_dict(el: T.Element, *, index: Optional[int] = None,
            include_screen: bool = True) -> Dict:
    """结构化描述。

    ``include_screen=False`` 时**不输出**屏幕坐标 —— 强制调用方
    走 ``window + client_rect`` 的换算路径，避免缓存失效的坐标流出去。
    """
    cr = el.client_rect()
    d = {
        "name": el.name or "",
        "type": _type_label(el, False),
        "localized_type": el.localized_type or "",
        "control_type": el.control_type,
        "automation_id": el.automation_id or "",
        "class_name": el.class_name or "",
        "framework": el.framework or "",
        "process_id": el.process_id,
        "window": el.window or 0,
        "hwnd": el.hwnd or 0,
        "enabled": bool(el.enabled),
        "offscreen": bool(el.offscreen),
        "focused": bool(el.focused),
        "focusable": bool(el.focusable),
        "password": bool(el.password),
        "depth": el.depth,
        "runtime_id": list(el.runtime_id or ()),
        "patterns": sorted(el.patterns or ()),
        "actions": actions_of(el),
        "preferred_action": _preferred(el),
        "help_text": el.help_text or "",
    }
    if index is not None:
        d["index"] = index
    if cr is not None:
        d["client_rect"] = list(cr.as_xywh())
    else:
        d["client_rect"] = None
    if include_screen:
        d["screen_rect"] = (list(el.rect.as_xywh())
                            if el.rect is not None else None)
    return d


def _preferred(el: T.Element) -> Optional[str]:
    pats = _patterns_of(el)
    if not pats:
        return None
    best = P.best_pattern(el, list(P.PATTERN_PRIORITY))
    if not best:
        return None
    # 模式名 → 动作名。Window/Scroll/Text 这些是"描述性"模式，
    # 映射不到单一动作 —— 用模式名的小写形式更诚实。
    return P.PATTERN_VERBS.get(best) or best.lower()


def to_click_target(el: T.Element, index: Optional[int] = None) -> Dict:
    """**动作层与模型之间的契约** —— 见模块文档。

    只给模型执行所需的最小充分信息。
    ``screen_rect`` 保留但明确标为 reference，真正的执行路径是
    ``window + client_rect``。
    """
    cr = el.client_rect()
    target = {
        "index": index,
        "name": el.name or "",
        "type": _type_label(el, False),
        "automation_id": el.automation_id or "",
        "window": el.window or 0,
        "client_rect": list(cr.as_xywh()) if cr is not None else None,
        "screen_rect_reference": (list(el.rect.as_xywh())
                                  if el.rect is not None else None),
        "patterns": sorted(el.patterns or ()),
        "preferred_action": _preferred(el),
        "enabled": bool(el.enabled),
        "offscreen": bool(el.offscreen),
    }
    if index is not None:
        target["index"] = index
    return target


def to_json(obj, *, indent: int = 2) -> str:
    """JSON 序列化。``obj`` 可以是 ``Element`` / 列表 / dict。"""
    if isinstance(obj, T.Element):
        obj = to_dict(obj)
    elif isinstance(obj, (list, tuple)):
        obj = [to_dict(e) if isinstance(e, T.Element) else e for e in obj]
    return json.dumps(obj, ensure_ascii=False, indent=indent,
                      default=_json_default)


def _json_default(o):
    """Rect 之类的对象兜底。"""
    for attr in ("as_xywh", "as_ltrb"):
        fn = getattr(o, attr, None)
        if callable(fn):
            try:
                return list(fn())
            except Exception:
                pass
    return repr(o)


def to_prompt(elements: Sequence[T.Element], *, window_name: str = "",
              limit: int = 60, include_screen: bool = False) -> str:
    """把一批元素渲染成**给模型的一段完整上下文**。

    结构：

        <window name="...">
          <element i="0" type="button" name="登录" id="loginBtn"
                   actions="click" rect="10,200,80,32" />
          ...
        </window>

    ``rect`` 用**客户区坐标**（``x,y,w,h``）。默认不输出屏幕坐标 ——
    模型不该依赖它，见 ``to_click_target`` 的说明。
    """
    out = ['<window name="%s" count="%d">'
           % (_esc(window_name), min(len(elements), limit))]
    for i, el in enumerate(elements[:limit]):
        attrs = ['i="%d"' % i,
                 'type="%s"' % _type_label(el, False),
                 'name="%s"' % _esc(_clip(el.name or "", 60))]
        if el.automation_id:
            attrs.append('id="%s"' % _esc(el.automation_id))
        cr = el.client_rect()
        if cr is not None:
            try:
                x, y, w, h = cr.as_xywh()
                attrs.append('rect="%d,%d,%d,%d"' % (x, y, w, h))
            except Exception:
                pass
        acts = actions_of(el)
        if acts:
            attrs.append('actions="%s"' % ",".join(acts))
        if not el.enabled:
            attrs.append('enabled="false"')
        if el.offscreen:
            attrs.append('offscreen="true"')
        if el.password:
            attrs.append('password="true"')
        out.append("  <element %s />" % " ".join(attrs))
    out.append("</window>")
    return "\n".join(out)


def summarize(elements: Sequence[T.Element]) -> str:
    """一句统计 —— 开场的"这一屏有什么"。"""
    if not elements:
        return "没有可交互元素"
    by_type: Dict[str, int] = {}
    for el in elements:
        key = _type_label(el, True)
        by_type[key] = by_type.get(key, 0) + 1
    parts = ["%s×%d" % (k, v) for k, v in
             sorted(by_type.items(), key=lambda kv: -kv[1])]
    n_dis = sum(1 for e in elements if not e.enabled)
    n_off = sum(1 for e in elements if e.offscreen)
    extra = []
    if n_dis:
        extra.append("禁用 %d" % n_dis)
    if n_off:
        extra.append("离屏 %d" % n_off)
    tail = ("（%s）" % "，".join(extra)) if extra else ""
    return "共 %d 个元素：%s%s" % (len(elements), "，".join(parts[:8]), tail)


def compare(before: Sequence[T.Element],
            after: Sequence[T.Element]) -> Dict[str, List[T.Element]]:
    """对比两次快照，给出"新增 / 消失 / 保留"。

    用途：点一下之后**验证界面真的变了**。这是"只看结果不看动作"
    这一原则的落地手段 —— 不做验证的动作等于没做。
    """
    bmap = {e.key: e for e in before}
    amap = {e.key: e for e in after}
    added = [amap[k] for k in amap if k not in bmap]
    gone = [bmap[k] for k in bmap if k not in amap]
    same = [amap[k] for k in amap if k in bmap]
    return {"added": added, "gone": gone, "same": same}


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    checks, failures = [], []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1. 类型中文名映射有缺漏吗
    #    只看 50000 段（UIA_ControlTypeIds 的真实取值区间）——
    #    类体里还有别的整数常量（例如某些内部标记 246），
    #    不筛就会误报"缺 unknown(246)"。
    missing = [T.control_name(k)
               for k, v in vars(T.ControlType).items()
               if isinstance(v, int) and 50000 <= v < 50050
               and v not in TYPE_ZH]
    _add("中文类型名覆盖完整", not missing,
         "缺: %s" % ", ".join(missing) if missing else "全部 ControlType 都有中文名")

    # 2. 需要一个真实元素来做后续检查
    el = None
    try:
        got = T.descendants(T.root(), kind=T.VIEW_CONTROL, max_depth=3,
                            limit=40)
        for cand in got:
            if cand.control_type == T.ControlType.BUTTON and cand.rect is not None:
                el = cand
                break
        if el is None and got:
            el = got[0]
    except Exception as exc:      # noqa: BLE001
        _add("能取到真实元素", False, "%s: %s" % (type(exc).__name__, exc))

    if el is None:
        _add("能取到真实元素", False, "没取到任何元素")
        return {"checks": checks, "failures": failures, "ok": not failures}

    _add("能取到真实元素", True,
         "%s %r rect=%s" % (_type_label(el), (el.name or "")[:20], el.rect))

    # 3. one_line 一行且不换行
    s = one_line(el, index=7)
    _add("one_line 单行输出", "\n" not in s and s.strip().startswith("7"),
         s[:60])

    # 4. describe 覆盖关键字段
    d = describe(el)
    _add("describe 含关键字段",
         all(k in d for k in ("类型", "名称", "窗口", "屏幕矩形", "客户区矩形")),
         "%d 行" % len(d.splitlines()))

    # 5. describe 无屏幕坐标时也不炸
    _add("describe 对无矩形元素安全",
         isinstance(describe(T.Element(name="空元素")), str),
         "空元素也能描述")

    # 6. table 对齐
    tbl = table([el], indexed=True)
    _add("table 有表头与行", "#" in tbl and "类型" in tbl,
         "%d 行" % len(tbl.splitlines()))

    # 7. tree 用 depth 缩进
    tl = tree([el])
    _add("tree 通过 depth 缩进", isinstance(tl, str) and tl.strip(),
         "深度 %d → 缩进 %d 空格" % (el.depth, el.depth * 2))

    # 8. markup 是 XML 风格且带编号
    mk = markup([el])
    _add("markup 带编号与标签",
         'i="0"' in mk and mk.startswith("<elements"),
         mk.splitlines()[1].strip()[:60])

    # 9. to_dict 客户区坐标存在
    dd = to_dict(el, index=0)
    _add("to_dict 带 client_rect",
         "client_rect" in dd and "index" in dd and dd["index"] == 0,
         "client_rect=%s" % (dd.get("client_rect"),))

    # 10. include_screen=False 时确实没有屏幕坐标
    dd2 = to_dict(el, include_screen=False)
    _add("include_screen=False 时不输出屏幕坐标",
         "screen_rect" not in dd2, "已省略 screen_rect")

    # 11. to_click_target 是契约形态
    ct = to_click_target(el, index=3)
    need = {"index", "window", "client_rect", "patterns",
            "preferred_action", "enabled", "offscreen"}
    _add("to_click_target 字段齐全", need <= set(ct),
         "缺: %s" % (need - set(ct)) if need - set(ct) else "契约字段全在")

    # 12. JSON 可序列化且中文不转义
    js = to_json([el])
    _add("to_json 可序列化", js.strip().startswith("[") and "\\u" not in js,
         "%d 字节，中文原样输出" % len(js))

    # 13. to_prompt 结构正确
    pr = to_prompt([el], window_name="测试窗")
    _add("to_prompt 结构正确",
         pr.startswith("<window") and pr.rstrip().endswith("</window>"),
         pr.splitlines()[0][:60])

    # 14. to_prompt 里坐标是客户区口径
    _add("to_prompt 用客户区坐标",
         "rect=" in pr or el.client_rect() is None,
         "元素客户区 %s" % (el.client_rect(),))

    # 15. summarize 有统计
    sm = summarize([el])
    _add("summarize 有统计", "共" in sm and "元素" in sm, sm[:60])

    # 16. actions_of 至少能对窗口给出结果
    try:
        win = T.from_window(el.window) if el.window else None
        acts = actions_of(win) if win is not None else []
        _add("actions_of 对真实元素可用", isinstance(acts, list),
             "窗口动作: %s" % (", ".join(acts) or "(无)"))
    except Exception as exc:      # noqa: BLE001
        _add("actions_of 对真实元素可用", False, "%s: %s" % (type(exc).__name__, exc))

    # 17. compare 能识别相同/差异
    got = T.descendants(T.root(), kind=T.VIEW_CONTROL, max_depth=3, limit=20)
    diff = compare(got, got)
    _add("compare 相同快照无差异",
         not diff["added"] and not diff["gone"],
         "added=%d gone=%d same=%d" % (len(diff["added"]),
                                       len(diff["gone"]), len(diff["same"])))
    diff2 = compare(got, got[:max(1, len(got) // 2)])
    _add("compare 能识别消失", len(diff2["gone"]) > 0,
         "gone=%d" % len(diff2["gone"]))

    # 18. 名称截断
    long_el = T.Element(name="很长" * 40, control_type=T.ControlType.BUTTON)
    line = one_line(long_el, width=20)
    _add("过长名称被截断", "…" in line and len(line) < 120,
         "长度 %d" % len(line))

    return {"checks": checks, "failures": failures, "ok": not failures}


def _main() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("UIA 元素描述自检:")
    res = selfcheck()
    for c in res["checks"]:
        print("  [%s] %-34s %s"
              % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
    total = len(res["checks"])
    print("describe 自检: %d/%d" % (total - len(res["failures"]), total))
    return 1 if res["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
