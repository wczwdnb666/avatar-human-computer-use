# -*- coding: utf-8 -*-
"""UIA（UI Automation）元素定位层。

这是 Avatar 的**主定位通道** —— 坐标只在前两层（屏幕 / 客户区）兜底。

模块划分
--------
``tree``
    树的遍历与快照。把 COM 的 ``IUIAutomationElement`` 转成 Python 值对象，
    并保证**及时释放** COM 引用。
``patterns``
    控件模式（Pattern）的读写。点击、设值、勾选都走这里，而不是发鼠标事件。
``find``
    元素查找：条件构造 + 多级回退策略。
``describe``
    元素转可读文本，给人和给模型看的两种形态。
``actions``
    语义化动作（click / set_value / toggle ...），Pattern 优先、坐标兜底。
``ocr_fallback`` / ``vision_fallback``
    UIA 之外的两条降级通道：WinRT OCR 与纯 Python 模板匹配。

依赖
----
· ``avatar._win32.{com,guids,variant}``
· ``avatar.app_manager``（窗口句柄与几何）

## 一条硬规则

元素对象**永远不持有屏幕绝对坐标**。只持有 ``hwnd + client_rect``。
需要屏幕坐标时用 ``layout`` 的换算函数**当场算**。

理由：窗口可能被移动、缩放、多屏间拖动。缓存下来的屏幕坐标会在
窗口一动之后全部失效，而且是**静默失效** —— 点下去点到了别的地方，
不报错。按需换算最多多花几十微秒，换的是正确性。
"""
from __future__ import annotations

from . import actions, describe, find, ocr_fallback, patterns, tree
from . import vision_fallback

__all__ = [
    "tree", "patterns", "find", "describe", "actions",
    "ocr_fallback", "vision_fallback",
]

#: 降级链的**顺序** —— 越靠前越可靠。
#:
#: 这个元组不只是文档，``fallback_chain()`` 会用它决定
#: "当前环境实际能走哪几步"。顺序的意义：
#:
#:   1. ``uia``    跨进程、不受遮挡影响、不依赖窗口在前台 —— 最稳
#:   2. ``ocr``    需要截图 + 识别，慢但能覆盖自绘界面
#:   3. ``vision`` 模板匹配，对 DPI/主题最敏感
#:   4. ``coord``  纯坐标，没有任何语义校验 —— 最脆弱
FALLBACK_ORDER = ("uia", "ocr", "vision", "coord")


def fallback_chain() -> list:
    """当前环境**实际可用**的降级链。

    返回 ``[{"name": ..., "available": bool, "reason": ...}, ...]``。

    用途：动作发起前就能知道"如果 UIA 失败，还有没有后手"，
    而不是走到一半才发现所有兜底都没接。
    """
    out = []
    for name in FALLBACK_ORDER:
        if name == "uia":
            try:
                ok = tree.is_available()
                reason = "" if ok else "UIA 不可用（无桌面会话或 COM 初始化失败）"
            except Exception as exc:      # noqa: BLE001
                ok, reason = False, "探测失败：%s" % exc
        elif name == "ocr":
            ok = ocr_fallback.is_available()
            reason = "" if ok else ocr_fallback.unavailable_reason()
        elif name == "vision":
            ok = vision_fallback.is_available()
            reason = "" if ok else vision_fallback.unavailable_reason()
        else:      # coord
            ok = actions.coord_fallback() is not None
            reason = "" if ok else ("坐标输入属输入层（阶段三），"
                                    "当前仅提供钩子未接入实现")
        out.append({"name": name, "available": bool(ok), "reason": reason})
    return out


def has_fallback() -> bool:
    """UIA 之后还有没有可用的兜底手段。

    ``False`` 意味着：UIA 一旦定位失败，这个任务就只能放弃。
    上层应该在规划阶段就据此决定要不要执行 —— 而不是失败了才发现。
    """
    chain = fallback_chain()
    return any(item["available"] for item in chain if item["name"] != "uia")


def describe_capabilities() -> str:
    """一段人类可读的能力说明 —— CLI 的 ``avatar capabilities`` 用。"""
    lines = ["元素定位能力："]
    for item in fallback_chain():
        mark = "可用  " if item["available"] else "不可用"
        lines.append("  [%s] %-6s %s" % (mark, item["name"],
                                         item["reason"] or "就绪"))
    if not has_fallback():
        lines.append("")
        lines.append("  ⚠ UIA 之后没有可用的兜底手段 —— "
                     "定位失败即任务失败，不会瞎点坐标。")
    return "\n".join(lines)

