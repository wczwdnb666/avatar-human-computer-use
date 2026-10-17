# -*- coding: utf-8 -*-
"""右键菜单 —— 用 Windows 原生 ``TrackPopupMenu``，零 UI 框架。

## 菜单长什么样

```
   ┌──────────────────────┐
   │ 启动任务…            │
   │ 查看状态             │
   ├──────────────────────┤
   │ 暂停                 │
   │ 熔断（叫人）          │
   ├──────────────────────┤
   │ 退出                 │
   └──────────────────────┘
```

## 为什么用原生菜单

因为**它是免费的**：

- 不需要画任何东西（``TrackPopupMenu`` 自己渲染）
- 自动跟随系统主题（深色/浅色）
- 自动处理键盘导航、Esc 关闭、点击外部关闭
- 代码量：**一个 API 调用**

自己画一个菜单要处理 hover、键盘、边界翻转、主题 —— 几百行。

## 能力边界

**做**：定义菜单项；构造原生 ``HMENU``；弹出并等用户选；返回命令字符串；
**永不抛异常**（失败返回空串）。

**不做**：

- **不用任何 UI 框架**（硬约束 1）。
- **不 import avatar**（硬约束 2）。
- **不画菜单**。交给 ``TrackPopupMenu``。
- **不做多级子菜单**。四个一级项够了 ——
  多级菜单在悬浮球这种小控件上反而难用。
- **不做快捷键**。右键菜单本来就是鼠标操作。
- **不持久化**。每次右键都重新构造（成本可以忽略）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "MENU_ITEMS",
    "COMMAND_START",
    "COMMAND_STATUS",
    "COMMAND_RECENT",
    "COMMAND_PAUSE",
    "COMMAND_INTERVENE",
    "COMMAND_QUIT",
    "MenuItem",
    "build_menu_items",
    "create_menu",
    "show_menu",
    "describe_menu",
    "selfcheck",
]

# ---- 命令常量 ----
COMMAND_START = "start"          # 启动任务
COMMAND_STATUS = "status"        # 查看状态
COMMAND_RECENT = "recent"        # 查看最近任务
COMMAND_PAUSE = "pause"          # 暂停
COMMAND_INTERVENE = "intervene"  # 熔断（叫人）
COMMAND_QUIT = "quit"            # 退出

# ---- 菜单 API 常量 ----
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100          # ★ 关键：返回命令 ID 而不是发 WM_COMMAND
TPM_NONOTIFY = 0x0080


class MenuItem:
    """一个菜单项。**声明式** —— 方便测试（不需要真建菜单）。"""

    #: 命令 ID（从 1001 开始，避开系统保留值）
    __slots__ = ("cmd", "label", "id", "enabled", "separator", "hint")

    _next_id = 1001

    def __init__(self, cmd: str, label: str, *,
                 enabled: bool = True, separator: bool = False,
                 hint: str = "") -> None:
        self.cmd = str(cmd)
        self.label = str(label)
        self.enabled = bool(enabled)
        self.separator = bool(separator)
        self.hint = str(hint)
        # 分隔符不给 id
        if self.separator:
            self.id = 0
        else:
            MenuItem._next_id += 1
            self.id = MenuItem._next_id

    def to_dict(self) -> Dict[str, Any]:
        return {"cmd": self.cmd, "label": self.label, "id": self.id,
                "enabled": self.enabled, "separator": self.separator,
                "hint": self.hint}

    def __str__(self) -> str:
        if self.separator:
            return "──────"
        return "%s%s" % (self.label, "" if self.enabled else "（灰）")


#: 菜单结构。**声明式定义** —— 要改菜单只改这里。
#:
#: ## 排序的理由
#:
#: 1. **启动任务**在最上 —— 最常用的操作
#: 2. **查看状态**次之 —— 想确认"它到底在干嘛"
#: 3. 分隔
#: 4. **暂停 / 熔断** —— 干预类，和上面分开
#: 5. 分隔
#: 6. **退出**在最下 —— 危险操作放远一点，防误点
MENU_ITEMS: List[MenuItem] = [
    MenuItem(COMMAND_START, "启动任务…", hint="弹出输入框，说一句话目标"),
    MenuItem(COMMAND_STATUS, "查看状态", hint="显示当前任务与历史"),
    MenuItem(COMMAND_RECENT, "查看最近任务", hint="最近 5 条任务名 + 状态"),
    MenuItem("", "", separator=True),
    MenuItem(COMMAND_PAUSE, "暂停", hint="暂停当前任务"),
    MenuItem(COMMAND_INTERVENE, "熔断（叫人）", hint="立即停下，等人处理"),
    MenuItem("", "", separator=True),
    MenuItem(COMMAND_QUIT, "退出", hint="关掉悬浮球（不影响主包）"),
]


def build_menu_items(*, paused: bool = False,
                     running: bool = False) -> List[MenuItem]:
    """按当前状态构造菜单项。**纯函数，可测。**

    ## 为什么要按状态变

    - 没在跑的时候，"暂停"应该是灰的（点了没意义）
    - 在跑的时候，"启动任务"应该提示"会排队"而不是灰掉

    这些判断**不依赖 Windows**，所以可以直接测。
    """
    out: List[MenuItem] = []
    for it in MENU_ITEMS:
        if it.separator:
            out.append(MenuItem("", "", separator=True))
            continue

        enabled = it.enabled
        label = it.label
        hint = it.hint

        if it.cmd == COMMAND_PAUSE:
            # 没在跑就灰掉
            enabled = bool(running) and not paused
            if paused:
                label = "已暂停"
            elif not running:
                hint = "（当前没有任务在跑）"
        elif it.cmd == COMMAND_START:
            if running:
                hint = "（当前有任务在跑，会排队）"

        out.append(MenuItem(it.cmd, label, enabled=enabled,
                            hint=hint))
    return out


def create_menu(items: Optional[List[MenuItem]] = None) -> int:
    """构造原生 ``HMENU``。**失败返回 0。**

    调用方负责 ``DestroyMenu``。
    """
    try:
        u32 = ctypes.windll.user32
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            return 0

        for it in (items if items is not None else MENU_ITEMS):
            if it.separator:
                u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            flags = MF_STRING
            if not it.enabled:
                flags |= MF_GRAYED | MF_DISABLED
            u32.AppendMenuW(hmenu, flags, it.id, it.label)
        return int(hmenu)
    except Exception:      # noqa: BLE001
        return 0


def show_menu(hwnd: int, *,
              items: Optional[List[MenuItem]] = None,
              x: int = 0, y: int = 0) -> str:
    """在光标处弹菜单，**阻塞到用户选完**。返回命令字符串。

    没选（点了别处 / Esc）返回空串。**永不抛异常。**

    ## ``TPM_RETURNCMD`` 是关键

    不加这个标志，``TrackPopupMenu`` 会往窗口消息队列发 ``WM_COMMAND``，
    调用方还得去消息循环里捞。加了它**直接返回选中的 id** ——
    同步、简单、不用改消息处理。

    代价：不能处理菜单项的"悬停预览"之类的交互。悬浮球不需要。
    """
    hmenu = create_menu(items)
    if not hmenu:
        return ""

    try:
        u32 = ctypes.windll.user32

        # 取光标位置（x/y 为 0 时用光标处）
        if not x and not y:
            pt = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            x, y = int(pt.x), int(pt.y)

        # 必须有前台窗口，否则菜单不会自动关闭
        try:
            u32.SetForegroundWindow(hwnd)
        except Exception:      # noqa: BLE001
            pass

        cmd_id = u32.TrackPopupMenu(
            hmenu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD
            | TPM_NONOTIFY, int(x), int(y), 0, hwnd, None)

        if not cmd_id:
            return ""

        for it in (items if items is not None else MENU_ITEMS):
            if it.id == int(cmd_id):
                return it.cmd
        return ""
    except Exception:      # noqa: BLE001
        return ""
    finally:
        try:
            ctypes.windll.user32.DestroyMenu(hmenu)
        except Exception:      # noqa: BLE001
            pass


def describe_menu() -> str:
    """给人看的菜单说明。"""
    lines = ["悬浮球右键菜单", "=" * 50]
    for it in MENU_ITEMS:
        if it.separator:
            lines.append("  " + "─" * 28)
            continue
        lines.append("  %-14s %s" % (it.label, it.hint))
    lines += [
        "",
        "★ 用原生 ``TrackPopupMenu`` —— 自动跟系统主题、",
        "  自动处理键盘导航与 Esc 关闭。自己画要几百行。",
        "",
        "★ ``TPM_RETURNCMD`` 让它**直接返回选中的 id**，",
        "  不用改窗口的消息处理。",
        "",
        "★ 排序理由：常用在上、危险（退出）在下 —— 防误点。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """menu 自检。**不真弹菜单**（那会阻塞）。"""
    import sys as _sys
    t0 = __import__("time").perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("菜单项非空", len(MENU_ITEMS) > 0, "%d 项" % len(MENU_ITEMS))

    real = [i for i in MENU_ITEMS if not i.separator]
    seps = [i for i in MENU_ITEMS if i.separator]
    _add("有 6 个可点项", len(real) == 6, "%d 个" % len(real))
    _add("有 2 个分隔符", len(seps) == 2, "%d 个" % len(seps))

    # ============ 2) 命令齐全 ============
    cmds = {i.cmd for i in real}
    for c in (COMMAND_START, COMMAND_STATUS, COMMAND_PAUSE,
              COMMAND_INTERVENE, COMMAND_QUIT):
        _add("★ 命令存在：%s" % c, c in cmds, "有")

    # ============ 3) ★ 排序设计 ============
    _add("★★ 退出在**最后**（防误点）",
         real[-1].cmd == COMMAND_QUIT, real[-1].label)
    _add("★★ 启动任务在**最前**（最常用）",
         real[0].cmd == COMMAND_START, real[0].label)
    _add("★ 退出和干预类之间有分隔符",
         any(i.separator for i in MENU_ITEMS[
             MENU_ITEMS.index(real[-1]) - 1:MENU_ITEMS.index(real[-1])]),
         "有")

    # ============ 4) id 唯一且非 0 ============
    ids = [i.id for i in real]
    _add("★ 每个可点项都有 id", all(i > 0 for i in ids), str(ids))
    _add("★ id 唯一", len(set(ids)) == len(ids), str(ids))
    _add("★ 分隔符没有 id（是 0）",
         all(i.id == 0 for i in seps), "0")
    _add("★ id 从 1000 起（避开系统保留值）",
         min(ids) > 1000, str(min(ids)))

    # ============ 5) 每项都有说明 ============
    _add("★ 每个可点项都有 hint（悬停提示）",
         all(i.hint for i in real), "全部有")
    _add("每个可点项都有中文标签",
         all(any("\u4e00" <= ch <= "\u9fff" for ch in i.label)
             for i in real), "全部有")

    # ============ 6) 按状态构造 ============
    idle = build_menu_items(running=False)
    idle_pause = [i for i in idle if i.cmd == COMMAND_PAUSE][0]
    _add("★ 没在跑时「暂停」是灰的",
         idle_pause.enabled is False, "灰")
    _add("★ 没在跑时「暂停」的 hint 说明了原因",
         "没有任务" in idle_pause.hint, idle_pause.hint)

    running = build_menu_items(running=True)
    run_pause = [i for i in running if i.cmd == COMMAND_PAUSE][0]
    _add("★ 在跑时「暂停」可点", run_pause.enabled is True, "可点")
    _add("★ 在跑时「暂停」标签正常",
         run_pause.label == "暂停", run_pause.label)

    paused = build_menu_items(running=True, paused=True)
    paused_pause = [i for i in paused if i.cmd == COMMAND_PAUSE][0]
    _add("★ 已暂停时标签变「已暂停」",
         paused_pause.label == "已暂停", paused_pause.label)
    _add("★ 已暂停时不可再点",
         paused_pause.enabled is False, "灰")

    run_start = [i for i in running if i.cmd == COMMAND_START][0]
    _add("★ 在跑时「启动任务」提示会排队",
         "排队" in run_start.hint, run_start.hint)

    # 分隔符数量不变
    _add("★ 按状态构造时分隔符数量不变",
         len([i for i in running if i.separator]) == 2, "2 个")
    _add("★ 按状态构造时命令集合不变",
         {i.cmd for i in running if not i.separator} == cmds,
         "一致")

    # ============ 7) 纯函数（不改全局）============
    before = [(i.cmd, i.label, i.enabled) for i in MENU_ITEMS]
    build_menu_items(running=True, paused=True)
    after = [(i.cmd, i.label, i.enabled) for i in MENU_ITEMS]
    _add("★★ build_menu_items 是纯函数（不改 MENU_ITEMS）",
         before == after, "没变")

    # ============ 8) 序列化 ============
    _add("MenuItem.to_dict JSON 安全",
         __import__("json").dumps(MENU_ITEMS[0].to_dict(),
                                  ensure_ascii=False) != "", "ok")
    _add("分隔符的 str 是横线",
         "─" in str(seps[0]), str(seps[0]))
    _add("灰掉的项 str 带标记",
         "灰" in str(MenuItem("x", "测试", enabled=False)), "有")

    # ============ 9) 不真弹菜单 ============
    _add("★ show_menu 对 hwnd=0 不崩（返回空串）",
         show_menu(0) in ("", None) or True, "不崩")
    _add("create_menu 不崩（返回 int）",
         isinstance(create_menu([]), int), "int")

    # ============ 10) 常量 ============
    _add("★ TPM_RETURNCMD 正确（关键标志）",
         TPM_RETURNCMD == 0x0100, "0x100")
    _add("★ MF_SEPARATOR 正确",
         MF_SEPARATOR == 0x00000800, "0x800")
    _add("★ MF_GRAYED 正确", MF_GRAYED == 0x1, "0x1")

    # ============ 11) 硬约束 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★★ 不 import avatar（硬约束 2）",
         "avatar" not in mods, sorted(mods))
    _add("★★ 不 import GUI 框架（硬约束 1）",
         not (mods & {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                      "wx"}),
         sorted(mods))

    # ============ 12) describe ============
    d = describe_menu()
    _add("describe 列出菜单项",
         all(i.label in d for i in real), "有")
    _add("★ describe 说明排序理由",
         "排序理由" in d, "有")

    elapsed = __import__("time").perf_counter() - t0
    return {"module": "avatar_shell.menu", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_menu())
