# -*- coding: utf-8 -*-
"""计划与 DAG —— 步骤、依赖、校验、拓扑调度。

## 为什么是 DAG 而不是列表

线性列表表达不了"这两步可以同时做"。而真实任务里这种情况很多：

    打开浏览器 ─┬─→ 打开标签页 A ─┐
                └─→ 打开标签页 B ─┴─→ 对比结果

用列表写，就只能拍个顺序，白白串行化。用 DAG 写，
执行器可以直接问"现在哪些步骤的前置都完成了"，然后**并行跑**。

## 三个校验（:meth:`Plan.validate`）

1. **id 唯一** —— 重名会让依赖引用产生歧义
2. **依赖完整** —— ``depends_on`` 里引用的 id 必须真的存在
3. **无环** —— 有环就没有合法的执行顺序，必须在这里拦住

这三条在**生成时**就要跑（:meth:`Plan.assert_valid`），
而不是等执行器跑到一半才发现。规划错误越早暴露越便宜。

## 能力边界

**做**：描述步骤与依赖；校验 DAG 合法性；算拓扑序与"当前可执行集"；
序列化成 JSON；画一张给人看的依赖图。

**不做**：

- **不执行**。本模块不知道"点击"怎么点 —— 那是 :mod:`executor`。
- **不做调度决策**。:meth:`Plan.ready_steps` 只回答"现在**可以**跑哪些"，
  "**要不要**并行跑"是执行器的事（受资源、风控、顺序语义影响）。
- **不管状态**。``Step.state`` 只是个字符串字段，合法转换由
  :mod:`state` 的 ``transition()`` 管，本模块不校验它。
- **不重试、不恢复**。那是 :mod:`recovery` 的事。
- **不调 LLM**。生成计划是 :mod:`planner` 的事，本模块只承载产物。
- **不做依赖推断**。``depends_on`` 是生成时就写好的，
  本模块不会"猜"两步之间该不该有边。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .goal import Goal

__all__ = [
    "Step",
    "Plan",
    "PlanError",
    "STEP_PENDING",
    "STEP_RUNNING",
    "STEP_SUCCESS",
    "STEP_FAILED",
    "STEP_SKIPPED",
    "STEP_BLOCKED",
    "STEP_STATES",
    "selfcheck",
]

#: 步骤状态（与 :mod:`state` 的 ``StepState`` 保持一致）。
#:
#: 这里用字符串而不是 Enum，是为了让 ``Plan.to_dict()`` 天然 JSON 安全。
#: :mod:`state` 那边会做字符串 ↔ 枚举的转换，两边不许各定义一套语义。
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCESS = "success"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_BLOCKED = "blocked"

STEP_STATES = (STEP_PENDING, STEP_RUNNING, STEP_SUCCESS, STEP_FAILED,
               STEP_SKIPPED, STEP_BLOCKED)


class PlanError(Exception):
    """计划本身有问题（重名 / 依赖缺失 / 有环）。

    单独一个异常类型，是为了让调用方能把"计划写错了"和
    "执行时出错了"分开处理 —— 前者要重新规划，后者要恢复。
    """

    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = list(problems or [])

    def __str__(self) -> str:
        if self.problems:
            return "%s\n  · %s" % (super().__str__(),
                                   "\n  · ".join(self.problems))
        return super().__str__()


# --------------------------------------------------------------------------
# Step
# --------------------------------------------------------------------------
@dataclass
class Step:
    """一个步骤 —— "做什么" + "依赖谁" + "怎么算成功" + "失败怎么办"。"""

    #: 步骤标识，**在 Plan 内唯一**。
    id: str = ""

    #: 动作类型（``open`` / ``type`` / ``click`` / ``navigate`` / ``wait`` …）。
    action: str = ""

    #: 人类可读的说明（日志和 DAG 图用）。
    description: str = ""

    #: 动作参数（``{"target": "notepad", "text": "hello"}``）。
    params: Dict[str, Any] = field(default_factory=dict)

    #: 前置步骤的 id 列表。**空 = 可以立即执行**。
    depends_on: List[str] = field(default_factory=list)

    #: 验证判据（交给 :mod:`verifier`）。
    #: 形如 ``{"kind": "uia", "target": "notepad", "expect": "..."}``。
    verify: Dict[str, Any] = field(default_factory=dict)

    #: 失败时的建议处置（``retry`` / ``skip`` / ``abort`` / ``pause``）。
    #: **只是建议** —— 真正的决策在 :mod:`recovery`。
    on_fail: str = ""

    #: 当前状态。**只能通过 :func:`state.transition` 改**（硬约束 4）。
    state: str = STEP_PENDING

    #: 已尝试次数。
    attempts: int = 0

    #: 已换策略次数（自愈上限用）。
    strategy_switches: int = 0

    #: 目标窗口句柄（执行时可能被填充）。
    hwnd: int = 0

    #: 执行结果摘要（最后一次）。
    result: Dict[str, Any] = field(default_factory=dict)

    #: 累计耗时。
    elapsed: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "s%s" % uuid.uuid4().hex[:8]

    @property
    def terminal(self) -> bool:
        """终态了吗？终态步骤不会再被执行器调度。"""
        return self.state in (STEP_SUCCESS, STEP_FAILED, STEP_SKIPPED)

    @property
    def done(self) -> bool:
        """算"完成"吗？（成功或跳过都算，失败不算）"""
        return self.state in (STEP_SUCCESS, STEP_SKIPPED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "description": self.description,
            "params": dict(self.params or {}),
            "depends_on": list(self.depends_on or []),
            "verify": dict(self.verify or {}),
            "on_fail": self.on_fail,
            "state": self.state,
            "attempts": int(self.attempts),
            "strategy_switches": int(self.strategy_switches),
            "hwnd": int(self.hwnd or 0),
            "result": dict(self.result or {}),
            "elapsed": float(self.elapsed),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        d = d if isinstance(d, dict) else {}
        return cls(
            id=str(d.get("id", "") or ""),
            action=str(d.get("action", "") or ""),
            description=str(d.get("description", "") or ""),
            params=dict(d.get("params") or {})
            if isinstance(d.get("params"), dict) else {},
            depends_on=[str(x) for x in (d.get("depends_on") or [])
                        if isinstance(d.get("depends_on"), (list, tuple))],
            verify=dict(d.get("verify") or {})
            if isinstance(d.get("verify"), dict) else {},
            on_fail=str(d.get("on_fail", "") or ""),
            state=str(d.get("state", STEP_PENDING) or STEP_PENDING),
            attempts=int(d.get("attempts") or 0),
            strategy_switches=int(d.get("strategy_switches") or 0),
            hwnd=int(d.get("hwnd") or 0),
            result=dict(d.get("result") or {})
            if isinstance(d.get("result"), dict) else {},
            elapsed=float(d.get("elapsed") or 0.0),
        )

    def describe(self) -> str:
        bits = ["[%s] %s" % (self.id, self.action)]
        if self.params.get("target"):
            bits.append(str(self.params["target"]))
        if self.params.get("text"):
            bits.append("「%s」" % self.params["text"])
        if self.depends_on:
            bits.append("← %s" % ",".join(self.depends_on))
        bits.append("(%s)" % self.state)
        return " ".join(bits)


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------
@dataclass
class Plan:
    """一张 DAG。步骤 + 依赖边 + 它服务于哪个目标。"""

    #: 这个计划服务于什么目标。
    goal: Optional[Goal] = None

    #: 全部步骤。**顺序不代表执行顺序** —— 执行顺序由 DAG 决定。
    steps: List[Step] = field(default_factory=list)

    #: 依赖边 ``[(from_id, to_id)]``，语义是 "from 是 to 的前置"。
    #: **冗余存储** —— ``Step.depends_on`` 才是真值来源，
    #: 这个字段是为了让 DAG 能被单独读取/可视化。
    #: :meth:`validate` 会检查两者一致。
    edges: List[Tuple[str, str]] = field(default_factory=list)

    #: 计划标识。
    plan_id: str = ""

    #: 生成时间。
    created_at: float = 0.0

    #: 生成器标识（``"rule"`` / ``"llm"``）。
    planner: str = "rule"

    #: 生成说明（给人复核）。
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.plan_id:
            self.plan_id = "p%s" % uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()
        if not self.edges and self.steps:
            self.edges = self._derive_edges()

    # ---------------- 索引 ----------------
    def _derive_edges(self) -> List[Tuple[str, str]]:
        """从 ``depends_on`` 推出边。"""
        out: List[Tuple[str, str]] = []
        for st in self.steps:
            for dep in (st.depends_on or []):
                out.append((dep, st.id))
        return out

    def sync_edges(self) -> List[Tuple[str, str]]:
        """把 ``edges`` 与 ``depends_on`` 对齐。返回新的边表。"""
        self.edges = self._derive_edges()
        return self.edges

    def step_by_id(self, sid: str) -> Optional[Step]:
        for st in self.steps:
            if st.id == sid:
                return st
        return None

    def index(self) -> Dict[str, Step]:
        return {st.id: st for st in self.steps}

    @property
    def ids(self) -> List[str]:
        return [st.id for st in self.steps]

    def __len__(self) -> int:
        return len(self.steps)

    # ---------------- 校验 ----------------
    def validate(self) -> List[str]:
        """检查 DAG 合法性。**返回问题列表，空列表 = 通过。**

        ## 检查项

        1. 计划非空
        2. 步骤 id 唯一
        3. 每个步骤有 action
        4. ``depends_on`` 引用的 id 存在（**依赖完整性**）
        5. 没有自依赖
        6. **无环**（用 Kahn 算法）
        7. ``edges`` 与 ``depends_on`` 一致
        8. ``state`` 是合法值

        ## 为什么返回列表而不是抛

        因为规划器需要**一次性拿到全部问题**，而不是修一个跑一次。
        真正要抛的场景（生成后立刻校验）用 :meth:`assert_valid`。
        """
        problems: List[str] = []

        if not self.steps:
            problems.append("计划里没有任何步骤")
            return problems

        # ---- 1) id 唯一 ----
        seen: Set[str] = set()
        dup: Set[str] = set()
        for st in self.steps:
            if st.id in seen:
                dup.add(st.id)
            seen.add(st.id)
        for d in sorted(dup):
            problems.append("步骤 id 重复：%s" % d)

        # ---- 2) action 非空 ----
        for st in self.steps:
            if not str(st.action or "").strip():
                problems.append("步骤 %s 没有 action" % st.id)

        # ---- 3) 依赖完整 + 自依赖 ----
        for st in self.steps:
            for dep in (st.depends_on or []):
                if dep == st.id:
                    problems.append("步骤 %s 依赖自己" % st.id)
                elif dep not in seen:
                    problems.append("步骤 %s 依赖了不存在的 %s"
                                    % (st.id, dep))

        # ---- 4) 状态合法 ----
        for st in self.steps:
            if st.state not in STEP_STATES:
                problems.append("步骤 %s 的状态非法：%s" % (st.id, st.state))

        # ---- 5) edges 与 depends_on 一致 ----
        derived = set(self._derive_edges())
        declared = set(tuple(e) for e in (self.edges or []))
        only_declared = declared - derived
        only_derived = derived - declared
        if only_declared:
            problems.append("edges 里有 depends_on 没写的边：%s"
                            % sorted(only_declared)[:5])
        if only_derived:
            problems.append("depends_on 里有 edges 没写的边：%s"
                            % sorted(only_derived)[:5])

        # ---- 6) 无环（Kahn）----
        # 只有在依赖都完整时才有意义 —— 缺依赖的图算不出拓扑序，
        # 硬算会把"缺依赖"误报成"有环"（一次报两个假问题）
        if not any("依赖了不存在" in p for p in problems):
            try:
                self.topological_order()
            except PlanError as exc:
                # 把异常消息也带上 —— 否则 problems 里只有"涉及的步骤：..."
                # 而看不出**是什么问题**（消息本身才有"有环"两个字）
                first = str(exc).splitlines()[0]
                if first:
                    problems.append(first)
                problems.extend(exc.problems or [])

        return problems

    def assert_valid(self) -> None:
        """校验并**抛异常**。规划器生成计划后立刻调这个。

        为什么要抛：一个不合法的 DAG 交到执行器手里，
        最可能的结果是"跑到一半卡住"或者"顺序错乱" ——
        两种都很难排查。在**生成的那一刻**就炸掉，代价最小。
        """
        problems = self.validate()
        if problems:
            raise PlanError("计划不合法（%d 个问题）" % len(problems),
                            problems)

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ---------------- 拓扑 ----------------
    def topological_order(self) -> List[str]:
        """Kahn 算法算拓扑序。**有环抛 :class:`PlanError`。**

        ## 平局怎么破

        同一层里按 ``steps`` 的**原始顺序**排。这很重要：
        Kahn 的结果不唯一，如果不定个平局规则，同一份计划
        每次跑出来的顺序都可能不同 —— 那会让测试和复盘都变成猜谜。
        按原始顺序排，让"生成时怎么写的"决定"执行时怎么排"。
        """
        idx = self.index()
        indeg: Dict[str, int] = {sid: 0 for sid in idx}
        order_hint: Dict[str, int] = {sid: i for i, sid in
                                      enumerate(self.ids)}

        for st in self.steps:
            for dep in (st.depends_on or []):
                if dep in indeg:
                    indeg[st.id] += 1

        # 初始零入度集，按原始顺序
        ready = sorted([s for s, d in indeg.items() if d == 0],
                       key=lambda s: order_hint.get(s, 0))
        out: List[str] = []

        while ready:
            sid = ready.pop(0)
            out.append(sid)
            for st in self.steps:
                if sid in (st.depends_on or []):
                    indeg[st.id] -= 1
                    if indeg[st.id] == 0:
                        ready.append(st.id)
            ready.sort(key=lambda s: order_hint.get(s, 0))

        if len(out) != len(idx):
            stuck = sorted(set(idx) - set(out))
            raise PlanError(
                "计划有环，无法确定执行顺序",
                ["涉及的步骤：%s" % ", ".join(stuck[:8])])

        return out

    def ready_steps(self, completed: Optional[Set[str]] = None) -> List[str]:
        """现在**可以**执行哪些步骤？

        "可以" = 状态是 pending **且** 不在 ``completed`` 里
        **且** ``depends_on`` 全部在 ``completed`` 里。

        返回的列表**可以并行跑** —— 但要不要并行是执行器的决定
        （受风控、资源、顺序语义影响），本方法只给候选。

        ## ⚠ ``completed`` 是权威，不是 ``state`` 的附庸

        ``completed`` 里的 id 会被**无条件排除**，即使它的 ``state``
        还写着 ``pending``。为什么：调用方传 ``completed={"a"}``
        就是在说"a 已经做完了"，这个声明比计划对象里的状态字段更可信 ——
        状态字段可能还没来得及同步（执行器刚做完、正要去更新）。

        反过来，如果某个步骤的 ``state`` 已经是 ``success``，
        那它也不会出现在结果里 —— 两条路都能排除，不会漏。
        """
        done = set(completed or set())
        out: List[str] = []
        for st in self.steps:
            if st.state != STEP_PENDING:
                continue
            if st.id in done:
                # 调用方说它完成了 —— 以调用方为准
                continue
            if all(d in done for d in (st.depends_on or [])):
                out.append(st.id)
        return out

    def completed_ids(self) -> Set[str]:
        """当前算"已完成"的 id 集合（成功或跳过）。"""
        return {st.id for st in self.steps if st.done}

    def blocked_forever(self) -> List[str]:
        """找出"永远不可能执行"的步骤。

        判据：还是 pending，但依赖里有一个**已失败**且不会恢复的步骤。
        执行器用这个来决定"是不是该整体放弃了"。
        """
        dead = {st.id for st in self.steps if st.state == STEP_FAILED}
        if not dead:
            return []
        out: List[str] = []
        for st in self.steps:
            if st.state != STEP_PENDING:
                continue
            if any(d in dead for d in (st.depends_on or [])):
                out.append(st.id)
        return out

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": float(self.created_at),
            "planner": self.planner,
            "notes": list(self.notes),
            "goal": self.goal.to_dict() if self.goal is not None else None,
            "steps": [st.to_dict() for st in self.steps],
            "edges": [[a, b] for a, b in (self.edges or [])],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Plan":
        """从 dict 还原。**脏数据不抛**（同 :meth:`Goal.from_dict` 的理由）。"""
        d = d if isinstance(d, dict) else {}
        goal = None
        gd = d.get("goal")
        if isinstance(gd, dict):
            goal = Goal.from_dict(gd)

        steps = [Step.from_dict(x) for x in (d.get("steps") or [])
                 if isinstance(x, dict)]

        edges: List[Tuple[str, str]] = []
        for e in (d.get("edges") or []):
            if isinstance(e, (list, tuple)) and len(e) == 2:
                edges.append((str(e[0]), str(e[1])))

        return cls(
            goal=goal,
            steps=steps,
            edges=edges,
            plan_id=str(d.get("plan_id", "") or ""),
            created_at=float(d.get("created_at") or 0.0),
            planner=str(d.get("planner", "rule") or "rule"),
            notes=[str(n) for n in (d.get("notes") or [])],
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, s: str) -> "Plan":
        return cls.from_dict(json.loads(s))

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        """画一张给人看的 DAG。"""
        lines: List[str] = []
        goal_txt = self.goal.instruction if self.goal else "(无目标)"
        lines.append("计划 %s  ←  %s" % (self.plan_id, goal_txt))
        lines.append("生成器 %s   步骤 %d   边 %d"
                     % (self.planner, len(self.steps), len(self.edges)))
        lines.append("-" * 60)

        try:
            order = self.topological_order()
        except PlanError:
            lines.append("⚠ 计划有环，无法排序 —— 下面是原始顺序")
            order = self.ids

        idx = self.index()
        # 按"第几层"缩进，让依赖关系一眼可见
        depth = self._depths()
        for sid in order:
            st = idx.get(sid)
            if st is None:
                continue
            pad = "  " * depth.get(sid, 0)
            lines.append("%s%s" % (pad, st.describe()))

        if self.notes:
            lines.append("-" * 60)
            for n in self.notes:
                lines.append("· %s" % n)
        return "\n".join(lines)

    def _depths(self) -> Dict[str, int]:
        """每个步骤的"层深" = 最长依赖链长度。用于缩进展示。

        **有环时全部返回 0** —— 展示函数不该因为图坏了就炸掉，
        能让人看到"步骤有哪些、状态如何"比"缩进对不对"重要得多。
        """
        try:
            order = self.topological_order()
        except PlanError:
            return {sid: 0 for sid in self.ids}

        depth: Dict[str, int] = {}
        for sid in order:
            st = self.step_by_id(sid)
            if st is None:
                continue
            deps = [d for d in (st.depends_on or []) if d in depth]
            depth[sid] = (max(depth[d] for d in deps) + 1) if deps else 0
        return depth


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def _mk(sid: str, action: str = "click", deps=None, **params) -> Step:
    return Step(id=sid, action=action, depends_on=list(deps or []),
                params=dict(params))


def selfcheck(quick: bool = True) -> dict:
    """plan 自检。**纯逻辑，不碰桌面。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 常量 ============
    _add("六个步骤状态齐全", len(_me.STEP_STATES) == 6,
         "%d 个" % len(_me.STEP_STATES))
    _add("pending 是默认状态", _me.STEP_PENDING == "pending", "pending")
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    # ============ 2) Step 基础 ============
    st = _me.Step()
    _add("Step 自动生成 id", bool(st.id) and st.id.startswith("s"), st.id)
    _add("Step 默认状态 pending", st.state == _me.STEP_PENDING, st.state)
    _add("pending 不是终态", st.terminal is False, "非终态")
    _add("pending 不算完成", st.done is False, "未完成")

    st2 = _me.Step(state=_me.STEP_SUCCESS)
    _add("success 是终态且算完成",
         st2.terminal is True and st2.done is True, "完成")
    st3 = _me.Step(state=_me.STEP_SKIPPED)
    _add("skipped 是终态且算完成（跳过也算走过）",
         st3.terminal is True and st3.done is True, "完成")
    st4 = _me.Step(state=_me.STEP_FAILED)
    _add("★ failed 是终态但**不算完成**",
         st4.terminal is True and st4.done is False, "未完成")
    st5 = _me.Step(state=_me.STEP_BLOCKED)
    _add("★ blocked **不是**终态（可恢复）",
         st5.terminal is False, "非终态")

    # ============ 3) 合法 DAG ============
    p = _me.Plan(steps=[_mk("a"), _mk("b", deps=["a"]),
                        _mk("c", deps=["a"]), _mk("d", deps=["b", "c"])])
    _add("★ 合法 DAG 校验通过", p.validate() == [], str(p.validate()))
    _add("is_valid 属性一致", p.is_valid is True, "True")
    _add("edges 自动从 depends_on 推出", len(p.edges) == 4,
         "%d 条边" % len(p.edges))

    # ============ 4) 无环检查 ============
    cyc = _me.Plan(steps=[_mk("a", deps=["c"]), _mk("b", deps=["a"]),
                          _mk("c", deps=["b"])])
    probs = cyc.validate()
    _add("★ 有环被检出",
         any("环" in x for x in probs), str(probs))
    _add("★ 有环时 topological_order 抛 PlanError",
         _raise_type(cyc.topological_order) == "PlanError",
         "抛了")
    _add("★ assert_valid 对有环计划抛异常",
         _raise_type(cyc.assert_valid) == "PlanError", "抛了")
    _add("PlanError 带 problems 列表",
         _raise_problems(cyc.assert_valid) != [], "有明细")

    # 自环
    self_cyc = _me.Plan(steps=[_mk("a", deps=["a"])])
    _add("★ 自依赖被检出",
         any("依赖自己" in x for x in self_cyc.validate()),
         str(self_cyc.validate()))

    # ============ 5) 依赖完整性 ============
    bad = _me.Plan(steps=[_mk("a"), _mk("b", deps=["不存在"])])
    probs = bad.validate()
    _add("★ 依赖不存在的 id 被检出",
         any("不存在" in x for x in probs), str(probs))
    _add("★ 缺依赖时不误报「有环」（否则一次报两个假问题）",
         not any("环" in x for x in probs), str(probs))

    # ============ 6) id 唯一 ============
    dup = _me.Plan(steps=[_mk("a"), _mk("a")])
    _add("★ id 重复被检出",
         any("重复" in x for x in dup.validate()), str(dup.validate()))

    # ============ 7) action 非空 ============
    noact = _me.Plan(steps=[_me.Step(id="a", action="")])
    _add("★ 空 action 被检出",
         any("没有 action" in x for x in noact.validate()),
         str(noact.validate()))

    # ============ 8) 空计划 ============
    _add("★ 空计划被检出",
         any("没有任何步骤" in x for x in _me.Plan().validate()), "空")

    # ============ 9) 状态非法 ============
    badst = _me.Plan(steps=[_me.Step(id="a", action="x", state="乱写的")])
    _add("★ 非法状态被检出",
         any("状态非法" in x for x in badst.validate()),
         str(badst.validate()))

    # ============ 10) edges 一致性 ============
    p2 = _me.Plan(steps=[_mk("a"), _mk("b", deps=["a"])])
    p2.edges = [("a", "b"), ("x", "y")]
    _add("★ edges 多出的边被检出",
         any("edges 里有" in x for x in p2.validate()),
         str(p2.validate()))
    p3 = _me.Plan(steps=[_mk("a"), _mk("b", deps=["a"])])
    p3.edges = []
    _add("★ edges 缺失的边被检出",
         any("depends_on 里有" in x for x in p3.validate()),
         str(p3.validate()))
    _add("sync_edges 能修好",
         (p3.sync_edges(), p3.validate())[1] == [], "修好了")

    # ============ 11) 拓扑序 ============
    order = p.topological_order()
    _add("★ 拓扑序长度正确", len(order) == 4, str(order))
    _add("★ 依赖在前（b 在 a 之后）",
         order.index("a") < order.index("b"), str(order))
    _add("★ 汇点 d 在最后", order[-1] == "d", str(order))
    _add("★ 拓扑序稳定（跑 20 次一致）",
         len({tuple(p.topological_order()) for _ in range(20)}) == 1,
         str(order))

    # ============ 12) ready_steps（可并行）============
    p4 = _me.Plan(steps=[_mk("a"), _mk("b", deps=["a"]),
                         _mk("c", deps=["a"]), _mk("d", deps=["b", "c"])])
    _add("★ 初始只有无依赖的步骤可执行",
         p4.ready_steps() == ["a"], str(p4.ready_steps()))
    _add("★★ a 完成后 b 和 c **同时**可执行（这就是 DAG 的价值）",
         set(p4.ready_steps({"a"})) == {"b", "c"},
         str(p4.ready_steps({"a"})))
    _add("b、c 都完成后 d 可执行",
         p4.ready_steps({"a", "b", "c"}) == ["d"],
         str(p4.ready_steps({"a", "b", "c"})))
    _add("全完成后没有可执行步骤",
         p4.ready_steps({"a", "b", "c", "d"}) == [], "空")

    p4.step_by_id("a").state = _me.STEP_SUCCESS
    _add("★ 已非 pending 的步骤不再出现在 ready 里",
         p4.ready_steps({"a"}) == ["b", "c"] or
         set(p4.ready_steps({"a"})) == {"b", "c"},
         str(p4.ready_steps({"a"})))

    _add("completed_ids 只含 success/skipped",
         p4.completed_ids() == {"a"}, str(p4.completed_ids()))

    # ============ 13) blocked_forever ============
    p5 = _me.Plan(steps=[_mk("a"), _mk("b", deps=["a"]),
                         _mk("c", deps=["b"])])
    p5.step_by_id("a").state = _me.STEP_FAILED
    _add("★ a 失败后 b 被标记为永远不可执行",
         "b" in p5.blocked_forever(), str(p5.blocked_forever()))
    _add("★ 但 c 不在列表里（它的直接依赖 b 还没失败）",
         "c" not in p5.blocked_forever(), str(p5.blocked_forever()))
    _add("没有失败时返回空",
         _me.Plan(steps=[_mk("a")]).blocked_forever() == [], "空")

    # ============ 14) 序列化 ============
    g = _goal()
    p6 = _me.Plan(goal=g, steps=[_mk("a", "open", target="notepad"),
                                 _mk("b", "type", deps=["a"], text="hi")])
    d = p6.to_dict()
    _add("to_dict JSON 安全",
         json.dumps(d, ensure_ascii=False) != "", "ok")
    p7 = _me.Plan.from_dict(d)
    _add("★ 往返后步骤数与依赖一致",
         len(p7.steps) == 2
         and p7.step_by_id("b").depends_on == ["a"], "一致")
    _add("往返后 goal 也回来了",
         p7.goal is not None
         and p7.goal.instruction == g.instruction, "goal 一致")
    _add("往返后 plan_id 不变", p7.plan_id == p6.plan_id, "一致")
    _add("往返后仍通过校验", p7.validate() == [], str(p7.validate()))
    _add("from_json 可用",
         _me.Plan.from_json(p6.to_json()).plan_id == p6.plan_id, "一致")
    _add("★ from_dict(空) 不抛",
         _me.Plan.from_dict({}).steps == [], "空")
    _add("★ from_dict(None) 不抛",
         _me.Plan.from_dict(None).steps == [], "None")
    _add("★ from_dict 容错脏步骤",
         len(_me.Plan.from_dict({"steps": ["不是字典", 123,
                                          {"id": "a"}]}).steps) == 1,
         "只留合法项")

    # ============ 15) 状态持久化往返 ============
    p6.step_by_id("a").state = _me.STEP_SUCCESS
    p6.step_by_id("a").attempts = 2
    p8 = _me.Plan.from_dict(p6.to_dict())
    _add("★ 步骤状态能往返（断点恢复的基础）",
         p8.step_by_id("a").state == _me.STEP_SUCCESS
         and p8.step_by_id("a").attempts == 2,
         p8.step_by_id("a").describe())

    # ============ 16) describe ============
    txt = p6.describe()
    _add("describe 含计划 id", p6.plan_id in txt, "有")
    _add("describe 含全部步骤 id",
         all(s in txt for s in ("a", "b")), "有")
    _add("describe 对坏计划不抛",
         isinstance(cyc.describe(), str), "有")

    # ============ 17) 不碰外部 ============
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
    _add("★ 顶层不 import 执行/网络/桌面相关模块",
         not (top & {"urllib", "socket", "threading", "subprocess",
                     "executor", "observer", "recovery"}),
         sorted(top))
    _add("顶层只依赖标准库 + goal",
         top <= {"json", "time", "uuid", "dataclasses", "typing",
                 "__future__", "sys", "goal"},
         sorted(top))

    elapsed = time.perf_counter() - t0
    return {
        "module": "plan",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


# ---- 自检辅助 ----
def _goal():
    from .goal import parse_goal
    return parse_goal("打开记事本输入hi")


def _raise_type(fn) -> str:
    """调 fn，返回抛出的异常类名；没抛返回空串。"""
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


def _raise_problems(fn) -> list:
    try:
        fn()
    except PlanError as exc:
        return list(exc.problems or [])
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
    print(_goal() and Plan(
        goal=_goal(),
        steps=[_mk("s1", "open", target="notepad"),
               _mk("s2", "type", deps=["s1"], text="hello"),
               _mk("s3", "save", deps=["s2"])],
    ).describe())
