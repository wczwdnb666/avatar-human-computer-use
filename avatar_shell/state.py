# -*- coding: utf-8 -*-
"""Agent Presence 状态机 —— 悬浮球"现在在干什么"。

## 四种状态

```
        ┌──────────────────────────────────────┐
        │                                      │
        ▼                                      │
    ┌────────┐  start   ┌─────────┐  done  ┌──┴──┐
    │  IDLE  │ ───────▶ │ RUNNING │ ─────▶ │ IDLE │
    │  灰    │          │  绿脉冲  │        └─────┘
    └───┬────┘          └────┬────┘
        │                    │ 需要人工
        │                    ▼
        │            ┌──────────────┐
        │            │ NEEDS_HUMAN  │  橙闪烁 + 提示音
        │            └──────┬───────┘
        │                   │ 人处理完
        │                   ▼
        │            （回到 RUNNING）
        │
        │  任何状态出错
        ▼
    ┌────────┐
    │ ERROR  │  红
    └────────┘
```

## 为什么状态机要独立成模块

因为它**可测**。颜色和动画是 GUI 的事（测不了），
但"什么时候能从 RUNNING 到 NEEDS_HUMAN"是纯逻辑（能测）。

**把能测的部分和不能测的部分分开** —— 这是本项目一贯的做法。

## 能力边界

**做**：定义四种状态；合法转换校验；给每种状态的颜色与动画参数；
记录最近一次状态变化（用于诊断）。

**不做**：

- **不画界面**。颜色是"给 GUI 用的建议值"，本模块不碰任何窗口。
- **不做动画**。只给"脉冲频率"这种参数，实际渲染在 :mod:`avatar_shell.ball`。
- **不出声**。提示音由 :mod:`avatar_shell.ball` 调 ``winsound``。
- **不持久化**。状态是瞬时的，进程退出就没了 ——
  要持久化的是任务，那是主包 :mod:`avatar.agentic_planner.memory` 的事。
- **不 import avatar**（硬约束 2）。外壳与主包只通过 stdio 通信。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "AgentState",
    "STATE_COLORS",
    "STATE_LABELS",
    "STATE_ANIMATION",
    "TRANSITIONS",
    "EVENT_START",
    "EVENT_DONE",
    "EVENT_NEED_HUMAN",
    "EVENT_RESUMED",
    "EVENT_ERROR",
    "EVENT_RESET",
    "EVENTS",
    "StateError",
    "PresenceState",
    "describe_state_machine",
    "selfcheck",
]

#: 四种状态。继承 ``str`` 让它能直接比较与序列化。
class AgentState(str, Enum):
    IDLE = "idle"                # 空闲，等着接活
    RUNNING = "running"          # 正在跑任务
    NEEDS_HUMAN = "needs_human"  # 需要人工介入（验证题 / 决策）
    ERROR = "error"              # 出错了

    def __str__(self) -> str:
        return self.value


#: 每种状态的颜色 ``(r, g, b)``。
#:
#: 选色原则：**色相差异优先于明度差异** ——
#: 小尺寸的悬浮球上，明度差异在浅色/深色桌面上表现不一致，
#: 色相差异（灰/绿/橙/红）一眼能分清。
STATE_COLORS: Dict[str, Tuple[int, int, int]] = {
    AgentState.IDLE: (128, 128, 128),        # 灰
    AgentState.RUNNING: (60, 200, 90),       # 绿
    AgentState.NEEDS_HUMAN: (255, 150, 20),  # 橙
    AgentState.ERROR: (220, 60, 60),         # 红
}

#: 每种状态的中文名（托盘提示、右键菜单用）。
STATE_LABELS: Dict[str, str] = {
    AgentState.IDLE: "空闲",
    AgentState.RUNNING: "运行中",
    AgentState.NEEDS_HUMAN: "需要人工",
    AgentState.ERROR: "出错",
}

#: 每种状态的动画参数 ``(是否闪烁, 周期秒)``。
#:
#: - **IDLE 不闪** —— 常亮灰，安静待着
#: - **RUNNING 慢脉冲**（1.6s）—— 表示"在动"，但不打扰
#: - **NEEDS_HUMAN 快闪**（0.5s）—— **这是唯一需要抢注意力的状态**
#: - **ERROR 常亮红** —— 不闪，因为闪了反而像"正在处理"
STATE_ANIMATION: Dict[str, Tuple[bool, float]] = {
    AgentState.IDLE: (False, 0.0),
    AgentState.RUNNING: (True, 1.6),
    AgentState.NEEDS_HUMAN: (True, 0.5),
    AgentState.ERROR: (False, 0.0),
}

# ---- 事件 ----
EVENT_START = "start"              # 开始跑任务
EVENT_DONE = "done"                # 任务完成
EVENT_NEED_HUMAN = "need_human"    # 需要人工
EVENT_RESUMED = "resumed"          # 人处理完了
EVENT_ERROR = "error"              # 出错
EVENT_RESET = "reset"              # 复位回空闲

EVENTS = (EVENT_START, EVENT_DONE, EVENT_NEED_HUMAN, EVENT_RESUMED,
          EVENT_ERROR, EVENT_RESET)

#: 转换表 ``(当前状态, 事件) -> 新状态``。
#:
#: ## 两条刻意的设计
#:
#: **1. ``ERROR`` 没有出边（除了 reset）**
#:    出错就是出错，不能"自己好起来"。必须人看一眼再复位 ——
#:    这是"错误必须被看见"的强制。
#:
#: **2. ``NEEDS_HUMAN`` 只能通过 ``resumed`` 回到 RUNNING**
#:    不能用 ``start`` 重开 —— 那会掩盖"上一次没处理完"。
TRANSITIONS: Dict[Tuple[str, str], str] = {
    # 空闲
    (AgentState.IDLE, EVENT_START): AgentState.RUNNING,
    (AgentState.IDLE, EVENT_ERROR): AgentState.ERROR,
    (AgentState.IDLE, EVENT_RESET): AgentState.IDLE,

    # 运行中
    (AgentState.RUNNING, EVENT_DONE): AgentState.IDLE,
    (AgentState.RUNNING, EVENT_NEED_HUMAN): AgentState.NEEDS_HUMAN,
    (AgentState.RUNNING, EVENT_ERROR): AgentState.ERROR,
    (AgentState.RUNNING, EVENT_RESET): AgentState.IDLE,

    # 需要人工
    (AgentState.NEEDS_HUMAN, EVENT_RESUMED): AgentState.RUNNING,
    (AgentState.NEEDS_HUMAN, EVENT_ERROR): AgentState.ERROR,
    (AgentState.NEEDS_HUMAN, EVENT_RESET): AgentState.IDLE,

    # 出错（**只能复位**）
    (AgentState.ERROR, EVENT_RESET): AgentState.IDLE,
    # 幂等：重复报错不抛
    (AgentState.ERROR, EVENT_ERROR): AgentState.ERROR,
}


class StateError(Exception):
    """非法状态转换。"""

    def __init__(self, message: str, *, current: Any = None,
                 event: str = "", allowed: Optional[List[str]] = None):
        super().__init__(message)
        self.current = current
        self.event = event
        self.allowed = list(allowed or [])

    def __str__(self) -> str:
        base = super().__str__()
        if self.allowed:
            base += "（当前状态下可用事件：%s）" % ", ".join(self.allowed)
        return base


def _coerce(state: Any) -> str:
    if isinstance(state, Enum):
        return str(state.value)
    if isinstance(state, str):
        return state
    raise StateError("无法识别的状态：%r" % (state,))


def _allowed(state: str) -> List[str]:
    return sorted(e for (s, e) in TRANSITIONS if s == state)


@dataclass
class PresenceState:
    """悬浮球的状态。**可注入时钟以便测试。**"""

    #: 当前状态
    state: str = str(AgentState.IDLE)
    #: 状态变化的原因（人类可读）
    reason: str = ""
    #: 变化历史（最近 N 条）
    history: List[Dict[str, Any]] = field(default_factory=list)
    #: 可注入的时钟（测试用）
    clock: Callable[[], float] = time.time
    #: 历史最多记几条
    max_history: int = 50
    #: 状态变化回调 ``fn(old, new, event, reason)``
    on_change: Optional[Callable[..., Any]] = None
    #: 进入当前状态的时间
    since: float = 0.0

    def __post_init__(self) -> None:
        if not self.since:
            self.since = float(self.clock())

    # ---------------- 查询 ----------------
    @property
    def color(self) -> Tuple[int, int, int]:
        """当前状态的颜色。**给 GUI 用的建议值。**"""
        return STATE_COLORS.get(self.state, (128, 128, 128))

    @property
    def label(self) -> str:
        """当前状态的中文名。"""
        return STATE_LABELS.get(self.state, self.state)

    @property
    def animation(self) -> Tuple[bool, float]:
        """``(是否闪烁, 周期秒)``。"""
        return STATE_ANIMATION.get(self.state, (False, 0.0))

    @property
    def needs_attention(self) -> bool:
        """需要抢注意力吗？（橙闪 / 红）"""
        return self.state in (str(AgentState.NEEDS_HUMAN),
                              str(AgentState.ERROR))

    @property
    def elapsed(self) -> float:
        """在当前状态待了多久（秒）。"""
        return max(0.0, float(self.clock()) - self.since)

    def allowed_events(self) -> List[str]:
        return _allowed(self.state)

    def can(self, event: str) -> bool:
        """能响应这个事件吗？**纯查询，不抛。**"""
        return (self.state, str(event or "")) in TRANSITIONS

    # ---------------- 转换 ----------------
    def transition(self, event: str, reason: str = "") -> str:
        """执行转换。**非法转换抛 :class:`StateError`。**"""
        e = str(event or "").strip()
        if not e:
            raise StateError("事件为空", current=self.state,
                             allowed=self.allowed_events())

        key = (self.state, e)
        if key not in TRANSITIONS:
            raise StateError(
                "非法状态转换：%s --%s--> ?" % (self.state, e),
                current=self.state, event=e,
                allowed=self.allowed_events())

        old = self.state
        new = str(TRANSITIONS[key])
        now = float(self.clock())

        self.state = new
        self.reason = str(reason or "")
        self.since = now

        rec = {"at": now, "from": old, "to": new, "event": e,
               "reason": self.reason}
        self.history.append(rec)
        if len(self.history) > max(1, int(self.max_history)):
            del self.history[:len(self.history) - self.max_history]

        if callable(self.on_change):
            try:
                self.on_change(old, new, e, self.reason)
            except Exception:      # noqa: BLE001
                # **回调抛异常不能影响状态机** —— 那是 UI 的事
                pass
        return new

    def set(self, state: Any, reason: str = "") -> str:
        """直接设到某个状态。**内部走合法转换，绕不过去。**

        这个方法存在的意义是"我知道要什么状态，但不想关心事件名" ——
        它会自己找一条合法路径。**找不到就抛**（不硬设）。
        """
        target = _coerce(state)
        if target == self.state:
            return self.state

        # 直接边
        for e in EVENTS:
            if TRANSITIONS.get((self.state, e)) == target:
                return self.transition(e, reason)

        # 两步（先 reset 再转）—— 只在目标可达时
        if (self.state, EVENT_RESET) in TRANSITIONS:
            mid = str(TRANSITIONS[(self.state, EVENT_RESET)])
            for e in EVENTS:
                if TRANSITIONS.get((mid, e)) == target:
                    self.transition(EVENT_RESET, "经由空闲：%s" % reason)
                    return self.transition(e, reason)

        raise StateError(
            "从 %s 到 %s 没有合法路径" % (self.state, target),
            current=self.state, allowed=self.allowed_events())

    # ---------------- 便捷方法 ----------------
    def start(self, reason: str = "") -> str:
        return self.transition(EVENT_START, reason or "开始任务")

    def done(self, reason: str = "") -> str:
        return self.transition(EVENT_DONE, reason or "任务完成")

    def need_human(self, reason: str = "") -> str:
        return self.transition(EVENT_NEED_HUMAN, reason or "需要人工介入")

    def resumed(self, reason: str = "") -> str:
        return self.transition(EVENT_RESUMED, reason or "人工已处理")

    def error(self, reason: str = "") -> str:
        return self.transition(EVENT_ERROR, reason or "出错")

    def reset(self, reason: str = "") -> str:
        return self.transition(EVENT_RESET, reason or "复位")

    # ---------------- 展示 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state, "label": self.label,
            "reason": self.reason, "since": self.since,
            "elapsed": round(self.elapsed, 2),
            "color": list(self.color),
            "needs_attention": self.needs_attention,
            "history": self.history[-10:],
        }

    def describe(self) -> str:
        blink, period = self.animation
        anim = ("闪烁 %.1fs" % period) if blink else "常亮"
        return ("%s（%s）  %s  rgb%s  已持续 %.1fs  %s"
                % (self.label, self.state, anim, self.color,
                   self.elapsed, self.reason))


def describe_state_machine() -> str:
    """给人看的状态机说明。"""
    lines = ["Agent Presence 状态机", "=" * 62, ""]
    for st in (AgentState.IDLE, AgentState.RUNNING,
               AgentState.NEEDS_HUMAN, AgentState.ERROR):
        blink, period = STATE_ANIMATION[st]
        lines.append("%-12s %-10s rgb%-16s %s"
                     % (st.value, STATE_LABELS[st], str(STATE_COLORS[st]),
                        ("闪烁 %.1fs" % period) if blink else "常亮"))
    lines += ["", "转换表：", "-" * 62]
    lines.append("%-12s %-14s %s" % ("当前", "事件", "转到"))
    for (s, e), t in sorted(TRANSITIONS.items()):
        lines.append("%-12s %-14s %s" % (s, e, t))
    lines += [
        "",
        "★ 两条刻意的设计：",
        "  · ERROR **没有出边**（除了 reset）—— 错误必须被看见，",
        "    不能自己好起来",
        "  · NEEDS_HUMAN 只能通过 resumed 回到 RUNNING ——",
        "    不能用 start 重开，那会掩盖「上一次没处理完」",
        "",
        "★ 本模块不画界面、不做动画、不出声 ——",
        "  只给颜色与动画参数，实际渲染在 ball.py。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """state 自检。**纯逻辑，不碰 GUI。**"""
    import sys as _sys
    t0 = time.perf_counter()
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
    _add("四种状态齐全", len(AgentState) == 4,
         " / ".join(s.value for s in AgentState))
    _add("每种状态都有颜色",
         all(s in STATE_COLORS for s in AgentState), "4 种")
    _add("每种状态都有中文名",
         all(s in STATE_LABELS for s in AgentState), "4 种")
    _add("每种状态都有动画参数",
         all(s in STATE_ANIMATION for s in AgentState), "4 种")
    _add("状态继承 str（可直接比较）",
         AgentState.IDLE == "idle", "是")

    # ============ 2) 颜色与动画语义 ============
    _add("★ 四色色相互异（灰/绿/橙/红）",
         len({STATE_COLORS[s] for s in AgentState}) == 4,
         str([STATE_COLORS[s] for s in AgentState]))
    _add("★ NEEDS_HUMAN 是**橙色**（抢注意力）",
         STATE_COLORS[AgentState.NEEDS_HUMAN][0] > 200
         and STATE_COLORS[AgentState.NEEDS_HUMAN][2] < 80,
         str(STATE_COLORS[AgentState.NEEDS_HUMAN]))
    _add("★ ERROR 是**红色**",
         STATE_COLORS[AgentState.ERROR][0] > 180
         and STATE_COLORS[AgentState.ERROR][1] < 100,
         str(STATE_COLORS[AgentState.ERROR]))
    _add("★ RUNNING 是**绿色**",
         STATE_COLORS[AgentState.RUNNING][1] > 150,
         str(STATE_COLORS[AgentState.RUNNING]))

    _add("★ IDLE **不闪**（安静待着）",
         STATE_ANIMATION[AgentState.IDLE][0] is False, "常亮")
    _add("★ NEEDS_HUMAN 闪得**最快**（0.5s）",
         STATE_ANIMATION[AgentState.NEEDS_HUMAN][1] == 0.5,
         str(STATE_ANIMATION[AgentState.NEEDS_HUMAN]))
    _add("★ ERROR **不闪**（闪了像在忙）",
         STATE_ANIMATION[AgentState.ERROR][0] is False, "常亮")
    _add("★ RUNNING 是慢脉冲（1.6s）",
         STATE_ANIMATION[AgentState.RUNNING][1] == 1.6,
         str(STATE_ANIMATION[AgentState.RUNNING]))

    # ============ 3) 合法转换 ============
    p = PresenceState()
    _add("初始是 IDLE", p.state == "idle", p.state)
    _add("idle --start--> running", p.start() == "running", p.state)
    _add("running --done--> idle", p.done() == "idle", p.state)

    p.start()
    _add("running --need_human--> needs_human",
         p.need_human() == "needs_human", p.state)
    _add("★ needs_human --resumed--> running",
         p.resumed() == "running", p.state)

    p2 = PresenceState()
    p2.error()
    _add("idle --error--> error", p2.state == "error", p2.state)
    _add("★ error --reset--> idle", p2.reset() == "idle", p2.state)

    # ============ 4) ★ 两条刻意设计 ============
    p3 = PresenceState()
    p3.error()
    _add("★★ ERROR 没有出边（start 被拒）",
         p3.can(EVENT_START) is False, "拒了")
    _add("★★ ERROR 只能 reset / 重复报错（幂等）",
         p3.allowed_events() == [EVENT_ERROR, EVENT_RESET],
         str(p3.allowed_events()))
    _add("★★ ERROR 上重复报错是幂等的（不抛）",
         p3.error() == "error", p3.state)

    p4 = PresenceState()
    p4.start()
    p4.need_human()
    _add("★★ NEEDS_HUMAN 不能用 start 重开",
         p4.can(EVENT_START) is False, "拒了")
    _add("★★ NEEDS_HUMAN 只能 resumed / error / reset",
         set(p4.allowed_events()) == {EVENT_RESUMED, EVENT_ERROR,
                                      EVENT_RESET},
         str(p4.allowed_events()))

    # ============ 5) 非法转换必须抛 ============
    bad = [("idle", EVENT_DONE), ("idle", EVENT_NEED_HUMAN),
           ("idle", EVENT_RESUMED), ("error", EVENT_START),
           ("error", EVENT_DONE), ("needs_human", EVENT_START),
           ("needs_human", EVENT_DONE), ("idle", "乱写")]
    for st, ev in bad:
        pp = PresenceState(state=st)
        _add("★ 非法转换被拒：%s --%s" % (st, ev),
             _raise_type(lambda x=pp, e=ev: x.transition(e))
             == "StateError", "抛了")

    _add("★ StateError 带可用事件（可诊断）",
         bool(_raise_allowed(lambda: PresenceState(
             state="error").transition(EVENT_START))),
         str(_raise_allowed(lambda: PresenceState(
             state="error").transition(EVENT_START))))
    _add("空事件抛 StateError",
         _raise_type(lambda: PresenceState().transition(""))
         == "StateError", "抛了")

    # ============ 6) 历史与回调 ============
    p5 = PresenceState()
    p5.start("跑任务 A")
    p5.done()
    _add("历史被记录", len(p5.history) == 2, "%d 条" % len(p5.history))
    _add("★ 历史含 from/to/event/reason",
         all(set(h) >= {"from", "to", "event", "reason", "at"}
             for h in p5.history), "齐全")
    # 历史有上限：**交替 start/done** 才能连续转 10 次
    # （连续 start 是非法转换 —— running 状态下 start 没有出边）
    q = PresenceState(max_history=3)
    for _ in range(10):
        q.start()
        q.done()
    _add("历史有上限", q.max_history == 3 and len(q.history) == 3,
         "%d 条（上限 3）" % len(q.history))

    calls: List[Any] = []
    p6 = PresenceState(on_change=lambda o, n, e, r: calls.append(
        (o, n, e)))
    p6.start("x")
    _add("★ 状态变化回调被调用", len(calls) == 1, str(calls))

    p7 = PresenceState(on_change=lambda *a: (_ for _ in ()).throw(
        RuntimeError("UI 炸了")))
    _add("★★ 回调抛异常不影响状态机",
         p7.start() == "running", p7.state)

    # ============ 7) set() 走合法路径 ============
    p8 = PresenceState()
    _add("set(running) 从 idle 可达", p8.set("running") == "running",
         p8.state)
    _add("set(needs_human) 从 running 可达",
         p8.set("needs_human") == "needs_human", p8.state)
    p9 = PresenceState()
    p9.error()
    _add("★ set(idle) 从 error 经 reset 可达",
         p9.set("idle") == "idle", p9.state)
    _add("set 到同状态是幂等的",
         PresenceState().set("idle") == "idle", "idle")
    _add("set 未知状态抛",
         _raise_type(lambda: PresenceState().set("乱写"))
         == "StateError", "抛了")

    # ============ 8) 展示与查询 ============
    p10 = PresenceState()
    _add("color 返回三元组",
         len(p10.color) == 3 and all(0 <= c <= 255 for c in p10.color),
         str(p10.color))
    _add("label 是中文", p10.label == "空闲", p10.label)
    _add("animation 返回二元组", len(p10.animation) == 2,
         str(p10.animation))
    _add("★ needs_attention 只对橙/红为真",
         PresenceState(state="needs_human").needs_attention is True
         and PresenceState(state="error").needs_attention is True
         and PresenceState(state="idle").needs_attention is False
         and PresenceState(state="running").needs_attention is False,
         "对")
    _add("to_dict JSON 安全",
         __import__("json").dumps(p10.to_dict(), ensure_ascii=False) != "",
         "ok")
    _add("describe 可读", "空闲" in p10.describe(),
         p10.describe()[:40])

    # 可注入时钟
    ticks = [100.0]
    p11 = PresenceState(clock=lambda: ticks[0])
    ticks[0] = 105.0
    _add("★ 可注入时钟（elapsed 可测）",
         abs(p11.elapsed - 5.0) < 1e-6, "%.1f" % p11.elapsed)

    # ============ 9) describe ============
    d = describe_state_machine()
    _add("describe 列出四种状态",
         all(s.value in d for s in AgentState), "有")
    _add("★ describe 说明 ERROR 没有出边",
         "没有出边" in d, "有")
    _add("★ describe 说明本模块不画界面",
         "不画界面" in d, "有")

    # ============ 10) 不 import avatar（硬约束 2）============
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
    _add("★★ 不 import 任何 GUI 框架（硬约束 1）",
         not (mods & {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                      "wx", "kivy", "electron"}),
         sorted(mods))
    _add("只用标准库",
         mods <= {"time", "dataclasses", "enum", "typing", "__future__",
                  "sys", "json", "ast"},
         sorted(mods))

    elapsed = time.perf_counter() - t0
    return {"module": "avatar_shell.state", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


def _raise_allowed(fn) -> list:
    try:
        fn()
    except StateError as exc:
        return list(exc.allowed or [])
    except BaseException:      # noqa: BLE001
        return []
    return []


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_state_machine())
