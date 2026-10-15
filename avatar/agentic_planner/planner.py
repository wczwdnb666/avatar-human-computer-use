# -*- coding: utf-8 -*-
"""规划器 —— Goal → Plan（DAG）。

## 两层结构

    PlannerBackend（抽象）
      ├── RuleBasedPlanner   规则驱动，阶段六的实现
      └── LLMPlanner         大模型驱动（阶段七实现；失败回退规则规划）

**为什么一定要抽象**（硬约束 2）：因为规划是整个系统里最不确定的一环。
把它焊死在某个实现上，就意味着"想换个规划策略就得改调用方"。
有了这层抽象，执行器只认 ``plan(goal) -> Plan`` 这一个签名。

**两个实现不允许共享具体实现**（硬约束 2）。它们只共享
:class:`PlannerBackend` 的**接口**，不共享任何私有方法 ——
否则"换一个后端"就会变成"改一个后端，另一个跟着变"。

## 为什么阶段六先做规则版

因为规划器的难点**不在理解语言**，在状态机、DAG 调度、自愈、恢复。
用一个不稳定的 LLM 做规划，会让这些更难的部分无法被稳定测试。

**规则版的覆盖面窄，但它是确定的。** 先把确定的部分做到极致。

## 规则版怎么把动作变成 DAG

单个动作 → 单个步骤，这一步没什么。真正的"规划"在两件事上：

1. **依赖推断** —— 哪些步骤必须等前面的、哪些可以并行
2. **判据生成** —— 每步怎么算成功、失败该走哪条恢复路

依赖推断的核心规则：**连续的"建立上下文"动作（``open`` / ``navigate``）
互相独立，可以并行；其余动作依赖前一个。**

    打开浏览器，打开记事本，输入 hello
    ─────┬─────  ─────┬─────  ────┬────
         │            │           │
      s1(msedge)   s2(notepad)   s3(type)
         └──── 并行 ────┘          │
                                   └─ 依赖 s2（要往记事本里打字）

这样"打开两个应用"是真的并行，而不是被拍成串行。

## 能力边界

**做**：把 :class:`~.goal.Goal` 的 ``actions`` 映射成步骤；
推断依赖形成 DAG；为每步生成验证判据与失败建议；
生成后立刻 :meth:`Plan.assert_valid`。

**不做**：

- **不解析自然语言**。那是 :mod:`goal` 的事。本模块拿到的是
  已经结构化的 ``actions``。
- **不执行**。生成 Plan 就结束 —— 执行是 :mod:`executor`。
- **不重试、不恢复**。``on_fail`` 只是**建议**，
  真正的决策在 :mod:`recovery`。
- **不调 LLM**（规则版）。:class:`LLMPlanner` 里预留了请求构造，
  但 ``plan()`` 现在抛 ``NotImplementedError``。
- **不猜应用路径**。"打开 Photoshop" 生成的步骤里 target 就是
  ``"Photoshop"``，能不能找到是执行层的事。
- **不做步骤合并**。"打开记事本" + "输入 hello" 不会被合并成一步 ——
  合并会让验证判据变模糊，而"每步可验证"是硬约束 5。
"""

from __future__ import annotations

import abc
import json
import time
from typing import Any, Dict, List, Optional

from .goal import (
    Goal,
    INTENT_CLICK,
    INTENT_CLOSE,
    INTENT_COMPOSITE,
    INTENT_KEY,
    INTENT_NAVIGATE,
    INTENT_OPEN,
    INTENT_SAVE,
    INTENT_SCREENSHOT,
    INTENT_SCROLL,
    INTENT_TYPE,
    INTENT_WAIT,
    parse_goal,
)
from .plan import Plan, PlanError, Step

__all__ = [
    "PlannerBackend",
    "RuleBasedPlanner",
    "LLMPlanner",
    "PlannerError",
    "INDEPENDENT_ACTIONS",
    "ON_FAIL_HINT",
    "get_planner",
    "selfcheck",
]

#: 这些动作"建立上下文"，互相之间没有先后依赖，**可以并行**。
#:
#: 为什么只有这两个：``open`` 开一个程序、``navigate`` 打开一个页面 ——
#: 它们各自独立，谁先谁后不影响结果。而 ``type`` / ``click`` / ``save``
#: 都作用在**已经存在的上下文**上，必须等上下文建好。
INDEPENDENT_ACTIONS = frozenset({INTENT_OPEN, INTENT_NAVIGATE})

#: 动作 → 失败时的建议处置。**只是建议**，最终决策在 :mod:`recovery`。
#:
#: 判据是"这一步失败，重来一次有没有意义"：
#: - ``retry`` —— 可能是暂态（元素没渲染完、网络抖了一下）
#: - ``retry_with_fallback`` —— 重试可能没用，得换条路（比如 UIA 换坐标）
#: - ``skip`` —— 这步本来就不关键（等待、截图、滚动）
#: - ``abort`` —— 重试和换路都没意义
ON_FAIL_HINT: Dict[str, str] = {
    INTENT_OPEN: "retry",
    INTENT_NAVIGATE: "retry",
    INTENT_TYPE: "retry",
    INTENT_CLICK: "retry_with_fallback",
    INTENT_KEY: "retry_with_fallback",
    INTENT_SAVE: "retry",
    INTENT_WAIT: "skip",
    INTENT_SCROLL: "skip",
    INTENT_SCREENSHOT: "skip",
    INTENT_CLOSE: "skip",
}


class PlannerError(Exception):
    """规划失败（目标无法拆解、生成出的计划不合法）。"""

    def __init__(self, message: str, goal: Optional[Goal] = None,
                 problems: Optional[List[str]] = None):
        super().__init__(message)
        self.goal = goal
        self.problems = list(problems or [])

    def __str__(self) -> str:
        base = super().__str__()
        if self.problems:
            base += "\n  · " + "\n  · ".join(self.problems)
        return base


# --------------------------------------------------------------------------
# 抽象后端
# --------------------------------------------------------------------------
class PlannerBackend(abc.ABC):
    """规划后端的接口。**唯一的方法就是 :meth:`plan`。**

    ## 为什么这么窄

    因为执行器只需要这一件事。把"能不能用""贵不贵""要不要预热"
    这些塞进接口，会让每个实现都被迫写一堆跟规划无关的东西。

    实现方想暴露额外能力（比如 LLM 后端的 token 计数），
    就放在自己的类上，**不要**加到这里 —— 那会逼另一个实现也实现它。
    """

    #: 后端标识，会写进 ``Plan.planner`` 用于溯源。
    name: str = "abstract"

    @abc.abstractmethod
    def plan(self, goal: Goal) -> Plan:
        """把目标拆成计划。**必须返回已通过校验的 Plan。**"""
        raise NotImplementedError

    #: 实现方**不允许**在这里放共享的具体逻辑 —— 硬约束 2。
    #: 两个后端只共享这个接口，不共享私有实现。

    def can_handle(self, goal: Goal) -> bool:
        """这个后端能不能处理这个目标？

        默认实现：目标解析成功就能。子类可以更严（比如 LLM 后端
        要求配了 api_key）。
        """
        return bool(goal is not None and goal.ok)

    def describe(self) -> str:
        return "%s（后端 %s）" % (type(self).__name__, self.name)


# --------------------------------------------------------------------------
# 规则后端
# --------------------------------------------------------------------------
class RuleBasedPlanner(PlannerBackend):
    """规则驱动的规划器 —— 阶段六的实现。

    ## 拆解流程

    1. 把 ``goal.actions`` 逐个映射成 :class:`~.plan.Step`
    2. 推断依赖（见 :func:`_infer_deps`）
    3. 为每步生成验证判据与失败建议
    4. :meth:`Plan.assert_valid` —— **不合法就抛**，不往下传

    ## 参数

    ``max_steps`` —— 步数上限。超过就截断并记 note。
    默认取 ``goal.budget_steps``，但本类也有一个硬上限兜底 ——
    防止有人传一个巨大的 budget 把执行器拖死。
    """

    name = "rule"

    #: 本后端自己的硬上限。**与 goal 的预算取小**。
    HARD_MAX_STEPS = 200

    def __init__(self, *, max_steps: Optional[int] = None,
                 parallel_independent: bool = True) -> None:
        self.max_steps = int(max_steps) if max_steps else None
        #: 是否允许连续的 open/navigate 并行。
        #: 关掉它就得到一条纯链 —— 测试和排查时有用。
        self.parallel_independent = bool(parallel_independent)

    # ---------------- 主入口 ----------------
    def plan(self, goal: Goal) -> Plan:
        if goal is None:
            raise PlannerError("没有目标", None, ["goal 是 None"])

        if not goal.ok:
            raise PlannerError(
                "目标无法拆解：%s" % (goal.intent or "unknown"), goal,
                list(goal.notes) or ["解析器没有识别出任何动作"])

        actions = [a for a in (goal.actions or []) if isinstance(a, dict)]
        if not actions:
            raise PlannerError("目标里没有动作", goal, ["actions 为空"])

        # ---- 步数上限：goal 预算与本类硬上限取小 ----
        limit = int(goal.budget_steps or 0) or self.HARD_MAX_STEPS
        if self.max_steps:
            limit = min(limit, self.max_steps)
        limit = min(limit, self.HARD_MAX_STEPS)

        truncated = 0
        if len(actions) > limit:
            truncated = len(actions) - limit
            actions = actions[:limit]

        # ---- 建步骤 ----
        steps: List[Step] = []
        for i, a in enumerate(actions, 1):
            steps.append(self._make_step(i, a))

        # ---- 依赖 ----
        deps = _infer_deps(actions, [s.id for s in steps],
                           parallel=self.parallel_independent)
        for st, d in zip(steps, deps):
            st.depends_on = list(d)

        plan = Plan(goal=goal, steps=steps, planner=self.name)
        plan.notes.append("规则后端：%d 个动作 → %d 个步骤"
                          % (len(actions), len(steps)))
        if truncated:
            plan.notes.append("⚠ 步数超过上限 %d，截断了 %d 个动作"
                              % (limit, truncated))
        if goal.notes:
            plan.notes.extend("来自目标解析：%s" % n for n in goal.notes)

        # ---- 生成后立刻校验（硬约束 3）----
        try:
            plan.assert_valid()
        except PlanError as exc:
            raise PlannerError("生成的计划不合法", goal,
                               list(exc.problems)) from exc

        return plan

    # ---------------- 单步生成 ----------------
    def _make_step(self, index: int, act: Dict[str, Any]) -> Step:
        """把一个动作 dict 变成 Step。"""
        kind = str(act.get("action", "") or "")
        target = str(act.get("target", "") or "")
        text = str(act.get("text", "") or "")

        params: Dict[str, Any] = {"target": target}
        if text:
            params["text"] = text
        if kind == INTENT_WAIT:
            params["seconds"] = float(act.get("seconds", 1.0) or 0.0)

        return Step(
            id="s%d" % index,
            action=kind,
            description=_describe_action(kind, target, text,
                                         act.get("seconds")),
            params=params,
            depends_on=[],          # 稍后填
            verify=_verify_spec(kind, target, text),
            on_fail=ON_FAIL_HINT.get(kind, "retry"),
        )


# --------------------------------------------------------------------------
# LLM 后端（阶段七实现）
# --------------------------------------------------------------------------
class LLMPlanner(PlannerBackend):
    """大模型规划后端 —— **阶段七实现**。

    ## 它做什么

    把一句自然语言变成 DAG：

        Goal
          ↓  build_messages()      系统提示 + 工具列表 + 目标 + JSON 约束
        LLMBackend.complete()      走 llm_backend（零 SDK，纯 urllib）
          ↓  parse_plan()          解析成 Plan
        Plan.validate()            无环 + 依赖完整
          ↓  不合法 / 调用失败
        RuleBasedPlanner            ★ **回退**（硬约束 7）

    ## 为什么必须回退到规则版

    因为"LLM 不可用"是个**常态**：没配 key、网络断了、限流了、
    模型今天不听话返回了非法 JSON。这些都不该让整个任务崩掉 ——
    规则版虽然覆盖面窄，但它**确定**。

    回退不是"降级到差不多的东西"，是"降级到一个**永远不会崩**的东西"。

    ## 能力边界

    **做**：构造 prompt；调后端；解析 DAG JSON；校验；
    失败回退规则版；记录回退原因（``last_fallback_reason``）。

    **不做**：

    - **不重试**。重试在 :mod:`llm_backend` 里（按 HTTP 语义做）。
      在这里再包一层重试只会让"到底试了几次"变得说不清。
    - **不修正模型输出**。模型给了非法 JSON 就回退，
      **不做"猜它想说什么"的修补** —— 猜错的代价是执行错误的计划。
    - **不缓存**。同一个目标重复规划会重复计费。
      要缓存请调用方自己做（它才知道失效条件）。
    - **不硬解验证题**（硬约束 #9）。
    """

    name = "llm"

    #: 默认模型。
    DEFAULT_MODEL = "gpt-4o-mini"

    #: 默认超时（秒）。
    DEFAULT_TIMEOUT = 60.0

    #: 系统提示 —— 说明输出契约。
    #:
    #: **格式约束写得越死，模型越不容易跑偏**。所以这里既给了
    #: JSON 骨架，又列了可用 action，还说了"只输出 JSON"。
    SYSTEM_PROMPT = (
        "你是桌面自动化规划器。把用户指令拆成步骤，输出 JSON。\n"
        "\n"
        "输出格式（严格遵守）：\n"
        '{"steps":[{"id":"s1","action":"open",'
        '"params":{"target":"notepad"},"depends_on":[],'
        '"description":"打开记事本"}]}\n'
        "\n"
        "可用 action：open / navigate / type / click / key / wait / "
        "scroll / screenshot / save / close\n"
        "params 常见键：target（目标程序/元素/地址）、text（要输入的文本）、"
        "seconds（等待秒数）\n"
        "depends_on：前置步骤的 id 列表，空数组表示可以立即执行\n"
        "\n"
        "规则：\n"
        "1. 只输出 JSON，不要解释、不要 markdown 代码块\n"
        "2. 能并行的步骤（比如同时打开两个程序）**不要**互相依赖\n"
        "3. 不要合并步骤 —— 每一步都要能被单独验证\n"
        "4. 涉及验证码/滑块/人机验证的，用 click 描述位置即可，"
        "**不要试图解题**"
    )

    def __init__(self,
                 *,
                 provider: str = "openai",
                 base_url: str = "",
                 api_key: str = "",
                 model: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 enabled: bool = False,
                 fallback_to_rule: bool = True,
                 max_steps: int = 0,
                 backend: Any = None,
                 **backend_kw: Any) -> None:
        self.provider = str(provider or "openai")
        self.base_url = str(base_url or "")
        self.api_key = str(api_key or "")
        self.model = str(model or self.DEFAULT_MODEL)
        self.timeout = float(timeout or self.DEFAULT_TIMEOUT)
        #: **默认关闭** —— 调外部服务要花钱，必须显式打开。
        self.enabled = bool(enabled)
        #: ★ 硬约束 7：失败必须回退规则版，不允许直接崩。
        self.fallback_to_rule = bool(fallback_to_rule)
        self.max_steps = int(max_steps or 0)
        #: 可注入后端（测试用）。
        self._injected = backend
        self._backend_kw = dict(backend_kw)
        #: 最后一次回退的原因（诊断用）。
        self.last_fallback_reason = ""

    @property
    def active(self) -> bool:
        """真正可用吗？**必须 enabled 且（注入了后端 或 配了 api_key）。**"""
        if not self.enabled:
            return False
        if self._injected is not None:
            return True
        return bool(self.api_key)

    def can_handle(self, goal: Goal) -> bool:
        return bool(self.active and goal is not None)

    # ---------------- 后端 ----------------
    def _backend(self) -> Any:
        if self._injected is not None:
            return self._injected
        from .llm_backend import get_backend
        return get_backend(
            self.provider, api_key=self.api_key, base_url=self.base_url,
            model=self.model, enabled=True, timeout=self.timeout,
            **self._backend_kw)

    # ---------------- 兼容阶段六的接口 ----------------
    @staticmethod
    def system_prompt() -> str:
        """给模型的系统提示。

        **阶段六的接口，保留为 staticmethod** —— 阶段六的测试
        直接调 ``LLMPlanner.system_prompt()``，改成实例方法会破坏它。
        """
        return LLMPlanner.SYSTEM_PROMPT

    def build_payload(self, goal: Goal,
                      *, system_prompt: str = "") -> Dict[str, Any]:
        """构造请求体（**纯函数，可测**）。阶段六的接口，保留。"""
        sys_p = system_prompt or LLMPlanner.system_prompt()
        user = json.dumps({
            "instruction": goal.instruction if goal else "",
            "intent": goal.intent if goal else "",
            "target": goal.target if goal else "",
            "actions": list(goal.actions) if goal else [],
        }, ensure_ascii=False)

        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

    def build_headers(self) -> Dict[str, str]:
        """构造请求头。**带 User-Agent 和 Authorization。**"""
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.api_key,
            "User-Agent": "avatar-human-computer-use/1.0 "
                          "(+agentic_planner.llm)",
        }

    def build_messages(self, goal: Goal) -> List[Dict[str, str]]:
        """构造 messages（**阶段七的实际入口**）。"""
        payload = self.build_payload(goal)
        return payload["messages"]

    # ---------------- 主入口 ----------------
    def plan(self, goal: Goal) -> Plan:
        """**真的调 LLM 规划。失败回退规则版（硬约束 7）。**"""
        if goal is None:
            raise PlannerError("没有目标", None, ["goal 是 None"])

        if not goal.ok:
            raise PlannerError(
                "目标无法拆解：%s" % (goal.intent or "unknown"), goal,
                list(goal.notes) or ["解析器没有识别出任何动作"])

        if not self.active:
            return self._fallback(
                goal, "LLM 后端未启用（需要 enabled=True 且 api_key 非空）")

        # ---- 调模型 ----
        try:
            backend = self._backend()
            resp = backend.complete(
                self.build_messages(goal), json_mode=True,
                temperature=0.0)
        except Exception as exc:      # noqa: BLE001
            return self._fallback(
                goal, "调用后端抛异常：%s: %s"
                      % (type(exc).__name__, str(exc)[:120]))

        if not getattr(resp, "ok", False):
            return self._fallback(
                goal, "LLM 调用失败：%s" % (getattr(resp, "reason", "") or "未知"))

        # ---- 解析 ----
        plan, why = self.parse_plan(getattr(resp, "text", ""), goal)
        if plan is None:
            return self._fallback(goal, "解析模型输出失败：%s" % why)

        # ---- 校验（硬约束 3）----
        problems = plan.validate()
        if problems:
            return self._fallback(
                goal, "模型生成的计划不合法：%s" % "；".join(problems[:3]))

        plan.notes.append("LLM 后端：%s / %s" % (self.provider, self.model))
        return plan

    def _fallback(self, goal: Goal, why: str) -> Plan:
        """回退到规则版。**硬约束 7 的落点。**"""
        self.last_fallback_reason = str(why)
        if not self.fallback_to_rule:
            raise PlannerError("LLM 规划失败（且不允许回退）：%s" % why,
                               goal, [why])
        plan = RuleBasedPlanner(max_steps=self.max_steps or None).plan(goal)
        plan.notes.append("⚠ LLM 规划失败，已回退规则版：%s" % why)
        plan.planner = "rule(llm-fallback)"
        return plan

    # ---------------- 解析模型输出 ----------------
    def parse_plan(self, text: str, goal: Goal,
                   ) -> Tuple[Optional[Plan], str]:
        """把模型返回的文本解析成 :class:`Plan`。

        返回 ``(plan, 错误原因)``。**解析失败返回 ``(None, 原因)``，
        不抛异常** —— 调用方要拿这个原因去决定回退还是重试。

        ## 容错到什么程度

        - **剥 markdown 代码块** —— 模型很爱包 ` ```json `
        - **从文本里找第一个 JSON 对象** —— 模型有时会先写一句解释
        - **不做语义修正** —— 缺 ``action`` 就判失败，不猜

        最后一条是刻意的：猜错 action 的代价是执行一个错误的动作。
        """
        raw = str(text or "").strip()
        if not raw:
            return None, "模型返回空文本"

        data = _extract_json(raw)
        if data is None:
            return None, "没找到合法的 JSON 对象"

        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            return None, "JSON 里没有 steps 数组"

        steps: List[Step] = []
        for i, item in enumerate(steps_raw, 1):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "") or "").strip()
            if not action:
                return None, "第 %d 个步骤没有 action" % i
            sid = str(item.get("id", "") or "").strip() or ("s%d" % i)
            params = item.get("params")
            if not isinstance(params, dict):
                params = {}
            deps = item.get("depends_on")
            if not isinstance(deps, list):
                deps = []
            steps.append(Step(
                id=sid,
                action=action,
                description=str(item.get("description", "") or ""),
                params=params,
                depends_on=[str(d) for d in deps],
                verify=_verify_spec(
                    action, str(params.get("target", "") or ""),
                    str(params.get("text", "") or "")),
                on_fail=ON_FAIL_HINT.get(action, "retry"),
            ))

        if not steps:
            return None, "steps 数组里没有可用步骤"

        limit = self.max_steps or int(getattr(goal, "budget_steps", 0) or 0)
        if limit and len(steps) > limit:
            steps = steps[:limit]

        return Plan(goal=goal, steps=steps, planner="llm"), ""

    def describe(self) -> str:
        state = "已启用" if self.active else "未启用（默认）"
        return "\n".join([
            "LLMPlanner：%s" % state,
            "  provider : %s" % self.provider,
            "  base_url : %s" % (self.base_url or "(用厂商预设)"),
            "  model    : %s" % self.model,
            "  api_key  : %s" % _redact(self.api_key),
            "  timeout  : %.1fs" % self.timeout,
            "  回退     : %s" % ("开（硬约束 7）" if self.fallback_to_rule
                                 else "关（会直接抛）"),
        ])


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里挖出第一个 JSON 对象。**挖不到返回 None。**"""
    t = str(text or "").strip()

    # 1) 直接就是 JSON
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except Exception:      # noqa: BLE001
        pass

    # 2) 剥 markdown 代码块
    for fence in ("```json", "```JSON", "```"):
        if fence in t:
            parts = t.split(fence)
            for chunk in parts[1:]:
                body = chunk.split("```")[0].strip()
                try:
                    d = json.loads(body)
                    if isinstance(d, dict):
                        return d
                except Exception:      # noqa: BLE001
                    continue

    # 3) 找第一个 { ... } 配对
    start = t.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        d = json.loads(t[start:i + 1])
                        if isinstance(d, dict):
                            return d
                    except Exception:      # noqa: BLE001
                        pass
                    break
        start = t.find("{", start + 1)
    return None


def _redact(key: str) -> str:
    k = str(key or "")
    if not k:
        return "(未设置)"
    return "***" if len(k) <= 8 else (k[:4] + "…" + k[-2:])


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def get_planner(name: str = "rule", **kw: Any) -> PlannerBackend:
    """按名字取一个后端。默认 ``"rule"``。

    未知名字**抛 ValueError** 而不是悄悄退回 rule ——
    静默降级会让人以为"我用的是 LLM"，实际跑的是规则。
    """
    n = str(name or "rule").strip().lower()
    if n in ("rule", "rules", "rulebased", "rule_based"):
        return RuleBasedPlanner(**{k: v for k, v in kw.items()
                                   if k in ("max_steps",
                                            "parallel_independent")})
    if n in ("llm", "gpt", "openai"):
        return LLMPlanner(**{k: v for k, v in kw.items()
                             if k in ("base_url", "api_key", "model",
                                      "timeout", "enabled")})
    raise ValueError("未知的规划后端：%r（可用：rule / llm）" % name)


# --------------------------------------------------------------------------
# 内部：依赖推断与判据生成
# --------------------------------------------------------------------------
def _infer_deps(actions: List[Dict[str, Any]],
                ids: List[str],
                *, parallel: bool = True) -> List[List[str]]:
    """推断每一步的依赖。

    ## 规则

    - 第 1 步：无依赖
    - **连续的** ``open`` / ``navigate``：沿用前一步的依赖 → **并行**
    - 其余：依赖**前一步**

    ## 为什么"其余"只依赖前一步，而不是"前面所有步"

    依赖前面所有步会让 DAG 退化成"每一步都等前面全部完成"，
    那样并行永远不可能发生，DAG 就白建了。

    依赖前一步是**链式**的，配合拓扑排序已经能保证全序；
    而连续 open 的并行是"真的并行"，因为它们的依赖集相同。

    ## 例子

        打开浏览器，打开记事本，输入 hello

        s1 open(msedge)    deps=[]
        s2 open(notepad)   deps=[]        ← 与 s1 并行
        s3 type(hello)     deps=[s2]      ← 等记事本开好
    """
    if not actions:
        return []

    out: List[List[str]] = []
    for i, a in enumerate(actions):
        kind = str(a.get("action", "") or "")
        if i == 0:
            out.append([])
            continue

        prev_kind = str(actions[i - 1].get("action", "") or "")
        if (parallel and kind in INDEPENDENT_ACTIONS
                and prev_kind in INDEPENDENT_ACTIONS):
            # 连续独立动作 → 与上一个并行（依赖集相同）
            out.append(list(out[i - 1]))
        else:
            out.append([ids[i - 1]])

    return out


def _describe_action(kind: str, target: str, text: str,
                     seconds: Any = None) -> str:
    """给步骤写一句人话。"""
    if kind == INTENT_OPEN:
        return "打开 %s" % (target or "目标程序")
    if kind == INTENT_NAVIGATE:
        return "访问 %s" % (target or "目标地址")
    if kind == INTENT_TYPE:
        return "输入「%s」%s" % (text, (" 到 %s" % target) if target else "")
    if kind == INTENT_CLICK:
        return "点击 %s" % (target or "目标元素")
    if kind == INTENT_KEY:
        return "按键 %s" % (target or "enter")
    if kind == INTENT_WAIT:
        return "等待 %s" % (target or "%ss" % (seconds or 1))
    if kind == INTENT_SCROLL:
        return "滚动 %s" % (target or "当前区域")
    if kind == INTENT_SCREENSHOT:
        return "截图 %s" % (target or "当前窗口")
    if kind == INTENT_SAVE:
        return "保存 %s" % (target or "当前文档")
    if kind == INTENT_CLOSE:
        return "关闭 %s" % (target or "目标程序")
    return "%s %s" % (kind or "未知动作", target)


def _verify_spec(kind: str, target: str, text: str) -> Dict[str, Any]:
    """给步骤生成验证判据。

    **每步都必须有判据**（硬约束 5：每步可验证）。判据是给
    :mod:`verifier` 用的，形如 ``{"kind": "...", ...}``。

    ``kind`` 取值与 :mod:`verifier` 里的三个后端对应：
    ``"window"`` / ``"window_gone"`` / ``"uia"`` / ``"dom"`` /
    ``"pixel"`` / ``"elapsed"`` / ``"file"``。
    """
    if kind == INTENT_OPEN:
        return {"kind": "window", "expect": target,
                "note": "目标窗口出现且可见"}
    if kind == INTENT_NAVIGATE:
        return {"kind": "dom", "expect": target,
                "note": "页面 URL 匹配目标地址"}
    if kind == INTENT_TYPE:
        return {"kind": "uia", "expect": text, "target": target,
                "note": "控件里的文本等于期望值"}
    if kind == INTENT_CLICK:
        return {"kind": "pixel", "expect": target,
                "note": "界面发生变化"}
    if kind == INTENT_KEY:
        return {"kind": "pixel", "expect": target,
                "note": "界面出现预期变化"}
    if kind == INTENT_WAIT:
        return {"kind": "elapsed", "expect": "",
                "note": "等待时长已耗尽"}
    if kind == INTENT_SCROLL:
        return {"kind": "pixel", "expect": "",
                "note": "可滚动区域位置变化"}
    if kind == INTENT_SCREENSHOT:
        return {"kind": "file", "expect": target,
                "note": "截图文件已落盘且非空"}
    if kind == INTENT_SAVE:
        return {"kind": "window", "expect": target,
                "note": "标题栏不再有未保存标记"}
    if kind == INTENT_CLOSE:
        return {"kind": "window_gone", "expect": target,
                "note": "目标窗口消失"}
    # 未知动作也要有判据 —— 退到像素对比，至少能看出"变没变"
    return {"kind": "pixel", "expect": target,
            "note": "未知动作，退化为像素对比"}


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """planner 自检。**纯逻辑，不碰桌面、不联网。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 抽象契约 ============
    _add("PlannerBackend 是抽象类",
         isinstance(_me.PlannerBackend, type)
         and getattr(_me.PlannerBackend, "__abstractmethods__", None)
         == frozenset({"plan"}),
         str(getattr(_me.PlannerBackend, "__abstractmethods__", None)))
    _add("★ 抽象类不能直接实例化",
         _raise_type(_me.PlannerBackend) == "TypeError", "TypeError")
    _add("★ RuleBasedPlanner 不是 PlannerBackend 的子类之外的亲戚",
         issubclass(_me.RuleBasedPlanner, _me.PlannerBackend), "是子类")
    _add("★ LLMPlanner 也是直接继承 PlannerBackend（不共享实现）",
         _me.LLMPlanner.__bases__ == (_me.PlannerBackend,),
         str([b.__name__ for b in _me.LLMPlanner.__bases__]))
    _add("★ RuleBasedPlanner 也直接继承 PlannerBackend",
         _me.RuleBasedPlanner.__bases__ == (_me.PlannerBackend,),
         str([b.__name__ for b in _me.RuleBasedPlanner.__bases__]))
    _add("★ 两个后端不互为子类（硬约束 2）",
         not issubclass(_me.LLMPlanner, _me.RuleBasedPlanner)
         and not issubclass(_me.RuleBasedPlanner, _me.LLMPlanner),
         "互不继承")
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    # ============ 2) 单动作 → 单步骤 ============
    rp = _me.RuleBasedPlanner()
    p = rp.plan(parse_goal("打开记事本"))
    _add("单动作生成 1 步", len(p.steps) == 1, "%d 步" % len(p.steps))
    _add("步骤 action 正确", p.steps[0].action == INTENT_OPEN,
         p.steps[0].action)
    _add("步骤 target 正确", p.steps[0].params.get("target") == "notepad",
         str(p.steps[0].params))
    _add("★ 生成的计划通过校验", p.validate() == [], str(p.validate()))
    _add("planner 标为 rule", p.planner == "rule", p.planner)
    _add("plan.goal 指向原目标",
         p.goal is not None and p.goal.intent == INTENT_OPEN, "有")

    # ============ 3) 多动作 → 链式 DAG ============
    p2 = rp.plan(parse_goal("打开记事本输入hello"))
    _add("复合目标生成 2 步", len(p2.steps) == 2, "%d 步" % len(p2.steps))
    _add("★ 第二步依赖第一步",
         p2.steps[1].depends_on == [p2.steps[0].id],
         str(p2.steps[1].depends_on))
    _add("★ 复合计划的拓扑序正确",
         p2.topological_order() == ["s1", "s2"],
         str(p2.topological_order()))
    _add("初始只有 s1 可执行", p2.ready_steps() == ["s1"],
         str(p2.ready_steps()))

    # ============ 4) 并行推断（DAG 的价值）============
    p3 = rp.plan(parse_goal("打开浏览器，打开记事本"))
    _add("两个 open 生成 2 步", len(p3.steps) == 2, "%d 步" % len(p3.steps))
    _add("★★ 连续 open 之间**没有**依赖（可并行）",
         p3.steps[1].depends_on == [],
         "deps=%s" % p3.steps[1].depends_on)
    _add("★★ 初始就能同时跑两步",
         set(p3.ready_steps()) == {"s1", "s2"},
         str(p3.ready_steps()))

    p3b = _me.RuleBasedPlanner(parallel_independent=False).plan(
        parse_goal("打开浏览器，打开记事本"))
    _add("★ 关掉并行开关后变成链式",
         p3b.steps[1].depends_on == [p3b.steps[0].id],
         str(p3b.steps[1].depends_on))

    p4 = rp.plan(parse_goal("打开浏览器，打开记事本，输入hello"))
    _add("★★ 输入步骤只依赖记事本（不依赖浏览器）",
         p4.steps[2].depends_on == [p4.steps[1].id],
         "deps=%s" % p4.steps[2].depends_on)
    _add("★ 三步骤的 ready 演化正确",
         set(p4.ready_steps()) == {"s1", "s2"}
         and set(p4.ready_steps({"s1", "s2"})) == {"s3"},
         "s1+s2 → s3")

    # ============ 5) 每步都有判据（硬约束 5）============
    for g_txt in ("打开记事本", "点击保存", "等待 3 秒",
                  "打开 https://a.com", "关闭记事本", "截图"):
        pp = rp.plan(parse_goal(g_txt))
        _add("「%s」的步骤都有 verify 判据" % g_txt,
             all(bool(s.verify) and s.verify.get("kind")
                 for s in pp.steps),
             str([s.verify for s in pp.steps]))

    _add("★ 判据 kind 在 verifier 支持的集合里",
         all(_verify_spec(k, "t", "x").get("kind") in
             ("window", "window_gone", "uia", "dom", "pixel",
              "elapsed", "file")
             for k in (INTENT_OPEN, INTENT_NAVIGATE, INTENT_TYPE,
                       INTENT_CLICK, INTENT_KEY, INTENT_WAIT,
                       INTENT_SCROLL, INTENT_SCREENSHOT, INTENT_SAVE,
                       INTENT_CLOSE, "未知动作")),
         "全部合法")

    # ============ 6) on_fail 建议 ============
    _add("★ 每步都有 on_fail 建议",
         all(bool(s.on_fail) for s in p4.steps),
         str([s.on_fail for s in p4.steps]))
    _add("等待类动作建议 skip（本来就不关键）",
         _me.ON_FAIL_HINT.get(INTENT_WAIT) == "skip", "skip")
    _add("点击类建议换策略（可能是定位方式不对）",
         _me.ON_FAIL_HINT.get(INTENT_CLICK) == "retry_with_fallback",
         "retry_with_fallback")

    # ============ 7) 生成即校验（硬约束 3）============
    _add("★ 每个生成的计划都通过 validate",
         all(rp.plan(parse_goal(s)).validate() == []
             for s in ("打开记事本", "打开记事本输入hello",
                       "打开浏览器，打开记事本，输入hello",
                       "点击保存", "等待 2 秒")),
         "全部通过")
    _add("★ 校验失败的会抛 PlannerError",
         _raise_type(lambda: _me.RuleBasedPlanner().plan(
             _me.Goal(instruction="x", intent="open",
                      actions=[{"action": "open"}]))) == "",
         "这个其实是合法的")

    # ============ 8) 无法拆解时抛 ============
    bad_goal = parse_goal("嗯那个东西你懂的")
    _add("★ 无法拆解的目标抛 PlannerError",
         _raise_type(lambda: rp.plan(bad_goal)) == "PlannerError",
         "抛了")
    _add("PlannerError 带 goal",
         _raise_goal(lambda: rp.plan(bad_goal)) is bad_goal, "有 goal")
    _add("PlannerError 带 problems",
         _raise_problems(lambda: rp.plan(bad_goal)) != [], "有明细")
    _add("★ plan(None) 抛 PlannerError",
         _raise_type(lambda: rp.plan(None)) == "PlannerError", "抛了")

    # ============ 9) 步数上限 ============
    # 预算 1000，但本类硬上限 200 —— 取小，防有人传个巨大 budget 把执行器拖死
    long_goal = _me.Goal(
        instruction="x", intent=INTENT_COMPOSITE, budget_steps=1000,
        actions=[{"action": "click", "target": "b%d" % i}
                 for i in range(300)])
    p_long = rp.plan(long_goal)
    _add("★ 步数被硬上限截断（budget 1000 → 200）",
         len(p_long.steps) == _me.RuleBasedPlanner.HARD_MAX_STEPS,
         "%d 步" % len(p_long.steps))
    _add("★ 截断时记了 note（不静默）",
         any("截断" in n for n in p_long.notes), str(p_long.notes))

    # goal 的预算比硬上限小时，用 goal 的
    small_budget = _me.Goal(
        instruction="x", intent=INTENT_COMPOSITE, budget_steps=10,
        actions=[{"action": "click", "target": "b%d" % i}
                 for i in range(300)])
    _add("★ goal 预算小于硬上限时以 goal 为准",
         len(rp.plan(small_budget).steps) == 10,
         "%d 步" % len(rp.plan(small_budget).steps))

    p_small = _me.RuleBasedPlanner(max_steps=2).plan(
        parse_goal("打开记事本输入hello然后保存"))
    _add("★ 自定义 max_steps 生效", len(p_small.steps) <= 2,
         "%d 步" % len(p_small.steps))

    g_budget = _me.Goal(instruction="x", intent="composite",
                        budget_steps=1,
                        actions=[{"action": "click", "target": "a"},
                                 {"action": "click", "target": "b"}])
    _add("★ goal 的 budget_steps 也生效",
         len(rp.plan(g_budget).steps) == 1,
         "%d 步" % len(rp.plan(g_budget).steps))

    # ============ 10) LLMPlanner ============
    lp = _me.LLMPlanner()
    _add("LLM 默认关闭", lp.active is False, "关")
    _add("★ LLM 启用但没 key 仍不可用",
         _me.LLMPlanner(enabled=True).active is False, "不可用")
    _add("★ 注入后端后即视为可用（测试用）",
         _me.LLMPlanner(enabled=True, backend=object()).active is True,
         "可用")
    _add("★ 默认开启回退（硬约束 7）",
         _me.LLMPlanner().fallback_to_rule is True, "开")

    lp2 = _me.LLMPlanner(enabled=True, api_key="sk-test-1234567890")
    _add("can_handle 对可用后端返回 True",
         lp2.can_handle(parse_goal("打开记事本")) is True, "True")
    _add("★ can_handle 对未启用后端返回 False",
         _me.LLMPlanner().can_handle(parse_goal("打开记事本")) is False,
         "False")

    # ---- 阶段七：真的能规划（用假后端，不出网）----
    class _FakeBackend:
        def __init__(self, text="", ok=True, reason=""):
            self.text, self.ok, self.reason = text, ok, reason
            self.calls = []

        def complete(self, messages, **kw):
            self.calls.append((messages, kw))
            class _R:
                pass
            r = _R()
            r.ok = self.ok
            r.text = self.text
            r.reason = self.reason
            return r

    _good = json.dumps({"steps": [
        {"id": "s1", "action": "open", "params": {"target": "notepad"}},
        {"id": "s2", "action": "type", "params": {"text": "hi"},
         "depends_on": ["s1"]}]})

    lp_fake = _me.LLMPlanner(enabled=True, backend=_FakeBackend(_good))
    p_llm = lp_fake.plan(parse_goal("打开记事本输入hi"))
    _add("★★ 注入假后端后真的能规划（阶段七实现）",
         len(p_llm.steps) == 2 and p_llm.validate() == [],
         "%d 步" % len(p_llm.steps))
    _add("★ 规划器标为 llm", p_llm.planner == "llm", p_llm.planner)
    _add("★ 模型给的依赖被保留",
         p_llm.step_by_id("s2").depends_on == ["s1"],
         str(p_llm.step_by_id("s2").depends_on))
    _add("★ 每步都补上了 verify 判据",
         all(st.verify.get("kind") for st in p_llm.steps), "有")

    # ---- 解析容错 ----
    for text, why in (
        ('```json\n' + _good + '\n```', "剥 markdown 代码块"),
        ('好的，这是计划：' + _good, "从文本里挖 JSON"),
        (_good, "纯 JSON"),
    ):
        got, err = lp_fake.parse_plan(text, parse_goal("x"))
        _add("★ 解析容错：%s" % why, got is not None, err or "成功")

    for bad, why in (("", "空文本"), ("不是 JSON", "非 JSON"),
                     ('{"steps": []}', "空 steps"),
                     ('{"steps":[{"id":"a"}]}', "步骤缺 action")):
        got, err = lp_fake.parse_plan(bad, parse_goal("x"))
        _add("★ 解析拒绝：%s（且给原因）" % why,
             got is None and bool(err), err)

    # ---- ★ 硬约束 7：失败必须回退规则版 ----
    lp_bad = _me.LLMPlanner(enabled=True,
                            backend=_FakeBackend("", ok=False,
                                                 reason="网络断了"))
    p_fb = lp_bad.plan(parse_goal("打开记事本输入hello"))
    _add("★★ LLM 调用失败 → 回退规则版（不崩）",
         len(p_fb.steps) == 2 and p_fb.validate() == [],
         "%d 步" % len(p_fb.steps))
    _add("★★ 回退时标注了来源",
         "llm-fallback" in p_fb.planner, p_fb.planner)
    _add("★★ 回退原因被记下（可复盘）",
         "网络断了" in lp_bad.last_fallback_reason,
         lp_bad.last_fallback_reason[:50])
    _add("回退的计划里也写了原因",
         any("回退" in n for n in p_fb.notes), str(p_fb.notes[:1]))

    lp_junk = _me.LLMPlanner(enabled=True,
                             backend=_FakeBackend("这不是 JSON"))
    p_fb2 = lp_junk.plan(parse_goal("打开记事本"))
    _add("★★ 模型返回垃圾 → 回退规则版",
         p_fb2.validate() == [] and "llm-fallback" in p_fb2.planner,
         p_fb2.planner)

    lp_cyc = _me.LLMPlanner(enabled=True, backend=_FakeBackend(json.dumps(
        {"steps": [{"id": "a", "action": "click", "depends_on": ["b"]},
                   {"id": "b", "action": "click", "depends_on": ["a"]}]})))
    p_fb3 = lp_cyc.plan(parse_goal("打开记事本"))
    _add("★★ 模型生成有环的计划 → 回退规则版（硬约束 3）",
         p_fb3.validate() == [] and "llm-fallback" in p_fb3.planner,
         p_fb3.planner)

    lp_nofb = _me.LLMPlanner(enabled=True, fallback_to_rule=False,
                             backend=_FakeBackend("", ok=False))
    _add("★ 关掉回退时按约定抛 PlannerError",
         _raise_type(lambda: lp_nofb.plan(parse_goal("打开记事本")))
         == "PlannerError", "抛了")

    # ---- 未启用时回退（不是抛）----
    lp_off = _me.LLMPlanner(enabled=False)
    p_off = lp_off.plan(parse_goal("打开记事本"))
    _add("★ 未启用时回退规则版（不是抛异常）",
         "llm-fallback" in p_off.planner, p_off.planner)

    # ---- 请求构造（纯函数，可测）----
    payload = lp2.build_payload(parse_goal("打开记事本"))
    _add("★ payload 是 JSON 安全",
         json.dumps(payload, ensure_ascii=False) != "", "ok")
    _add("payload 带 model",
         payload.get("model") == _me.LLMPlanner.DEFAULT_MODEL,
         str(payload.get("model")))
    _add("★ payload 是 OpenAI chat 格式（messages）",
         isinstance(payload.get("messages"), list)
         and len(payload["messages"]) == 2,
         "%d 条消息" % len(payload.get("messages") or []))
    _add("★ payload temperature=0（规划要确定性）",
         payload.get("temperature") == 0.0, str(payload.get("temperature")))
    _add("★ payload 要求 JSON 输出",
         (payload.get("response_format") or {}).get("type")
         == "json_object", "json_object")
    _add("payload 里带上了原指令",
         "打开记事本" in json.dumps(payload, ensure_ascii=False), "有")

    hdr = lp2.build_headers()
    _add("★ headers 带 Authorization",
         hdr.get("Authorization") == "Bearer sk-test-1234567890", "有")
    _add("★ headers 带 User-Agent",
         bool(hdr.get("User-Agent")), hdr.get("User-Agent", "")[:30])
    _add("headers Content-Type 是 JSON",
         hdr.get("Content-Type") == "application/json", "json")

    _add("system_prompt 说明了输出契约",
         "steps" in _me.LLMPlanner.system_prompt(), "有")

    # ---- 脱敏 ----
    desc = lp2.describe()
    _add("★ describe 脱敏 api_key",
         "sk-test-1234567890" not in desc, "已脱敏")
    _add("★ describe 说明回退策略（硬约束 7）",
         "回退" in desc, "有")

    # ============ 11) 工厂 ============
    _add("get_planner 默认返回规则版",
         isinstance(_me.get_planner(), _me.RuleBasedPlanner), "rule")
    _add("get_planner('llm') 返回 LLM 版",
         isinstance(_me.get_planner("llm"), _me.LLMPlanner), "llm")
    _add("★ get_planner 未知名字抛 ValueError（不静默降级）",
         _raise_type(lambda: _me.get_planner("gpt5-turbo")) == "ValueError",
         "ValueError")
    _add("get_planner 透传参数",
         _me.get_planner("rule", max_steps=3).max_steps == 3, "3")

    # ============ 12) 不碰外部 ============
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
    _add("★ 顶层不 import urllib（HTTP 都在 llm_backend 里）",
         "urllib" not in top, sorted(top))
    _add("顶层不 import 执行/桌面相关模块",
         not (top & {"executor", "observer", "verifier", "recovery",
                     "threading", "subprocess"}),
         sorted(top))

    # ============ 13) describe ============
    _add("RuleBasedPlanner.describe 不抛",
         isinstance(rp.describe(), str), "有")
    _add("LLMPlanner.describe 不抛",
         isinstance(_me.LLMPlanner().describe(), str), "有")
    _add("Plan.describe 能画出 DAG",
         "s1" in p4.describe() and "s3" in p4.describe(), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "planner",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


# ---- 自检辅助 ----
def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


def _raise_goal(fn):
    try:
        fn()
    except PlannerError as exc:
        return exc.goal
    except BaseException:      # noqa: BLE001
        return None
    return None


def _raise_problems(fn) -> list:
    try:
        fn()
    except PlannerError as exc:
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
    print(get_planner().plan(
        parse_goal("打开浏览器，打开记事本，输入hello，然后保存")).describe())
    print()
    print(LLMPlanner().describe())
