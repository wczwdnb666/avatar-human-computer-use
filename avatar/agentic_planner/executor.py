# -*- coding: utf-8 -*-
"""执行器 —— 按 DAG 调度，把规划、观测、验证、恢复串起来。

## 一次执行的完整流程

    1. 取当前可执行集（plan.ready_steps）
    2. 对每个步骤：
       a. 检查全局超时 / 熔断标志
       b. 风控闸（阶段五 detect → 命中则 pause_for_human）
       c. observer 观测 before
       d. transition(pending, start) → running，记入 memory
       e. runner 执行
       f. observer 观测 after
       g. verifier 判定成功没有
       h. 成功 → transition(succeed)；失败 → recovery 决策
          → retry / retry_with_fallback / skip / abort / pause_for_human
    3. 回到 1，直到没有可执行步骤或任务终止

## 并行

无依赖的步骤**可以并行**（用 ``threading``，**不用 asyncio**）。

为什么不用 asyncio：整条链路（UIA COM 调用、``SendInput``、
``GetAsyncKeyState``）都是**阻塞**的 C 调用，asyncio 包不住它们。
用线程反而更简单：一个线程一个步骤，天然对应"同时开两个程序"。

## 与阶段五的集成（硬约束 9）

**任何步骤遇到风险都要停下来叫人，不允许硬闯。**

    每步执行前 → risk_detector.integration.guard_before_action()
    命中 → guard() → pause_for_human() → 人按 F9 → 继续

熔断时**整个任务暂停**（不只是当前步骤）—— 因为风控问题是全局的，
一个步骤检出验证码，说明整个会话都处于风险中。

## 三条上限（硬约束 6）

============================  ==========  ==================================
上限                          默认值      作用
============================  ==========  ==================================
单步重试                     3 次        同一策略最多试几次
单步换策略                   2 次        换了几条路之后就不再换了
全局超时                     30 分钟     整任务的时间预算
============================  ==========  ==================================

这三条**硬编码为常量**，且测试断言不能超限。

## 能力边界

**做**：DAG 调度（含并行）；调用阶段五风控闸；调 observer / verifier /
recovery；维护状态机与 memory；全局超时与熔断传播；断点恢复。

**不做**：

- **不规划**。Plan 是 :mod:`planner` 给的，执行器不改结构。
- **不决定重试策略**。``decide_recovery`` 在 :mod:`recovery`，
  执行器只执行它的决定。
- **不自己写状态**。所有状态变更过 :func:`state.transition`（硬约束 4）。
- **不绕过风控**。``guard_enabled=False`` 只给单元测试用，
  不是生产后门。
- **不硬解验证题**（硬约束 #9）。遇到验证题一律叫人。
- **不清理任务存档**。``resume`` 只读，删除要显式调
  :func:`memory.delete_task`。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .memory import ShortTermMemory
from .plan import Plan, Step
from .state import (
    SCOPE_STEP,
    SCOPE_TASK,
    StepState,
    TaskState,
    StateError,
    transition,
)

__all__ = [
    "EXECUTOR_READY",
    "MAX_RETRIES",
    "MAX_STRATEGY_SWITCHES",
    "TASK_TIMEOUT",
    "MAX_WORKERS",
    "StepResult",
    "ExecutionResult",
    "DefaultRunner",
    "Executor",
    "describe_planner",
    "selfcheck",
]

#: 阶段六是否已交付。**现在是 True** —— 执行器真的能跑了。
EXECUTOR_READY = True

#: 单步重试上限（硬约束 6）。
MAX_RETRIES = 3

#: 单步换策略上限（硬约束 6）。
MAX_STRATEGY_SWITCHES = 2

#: 全局超时，秒（硬约束 6）。
TASK_TIMEOUT = 30 * 60.0

#: 并行度上限。开太多线程去抢 UIA 反而更慢。
MAX_WORKERS = 4


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------
@dataclass
class StepResult:
    """单个步骤的执行结果。"""

    step_id: str = ""
    ok: bool = False
    state: str = "pending"
    attempts: int = 0
    switches: int = 0
    detail: str = ""
    strategy: str = ""          # 最后一次 recovery 决策
    blocked: bool = False       # 被风控拦下 / 转人工
    skipped: bool = False
    guard_result: Optional[Dict[str, Any]] = None
    verify: Optional[Any] = None
    error: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id, "ok": bool(self.ok),
            "state": self.state, "attempts": int(self.attempts),
            "switches": int(self.switches), "detail": self.detail,
            "strategy": self.strategy, "blocked": bool(self.blocked),
            "skipped": bool(self.skipped), "error": self.error,
            "elapsed": round(self.elapsed, 3),
        }

    def describe(self) -> str:
        if self.blocked:
            head = "⊘ 被风控拦截"
        elif self.skipped:
            head = "↷ 已跳过"
        elif self.ok:
            head = "✓ 成功"
        else:
            head = "✗ 失败"
        bits = [head, self.step_id]
        if self.attempts > 1:
            bits.append("试 %d 次" % self.attempts)
        if self.switches:
            bits.append("换策略 %d 次" % self.switches)
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)


@dataclass
class ExecutionResult:
    """整个任务的执行结果。"""

    ok: bool = False
    task_state: str = str(TaskState.PENDING)
    plan_id: str = ""
    task_id: str = ""
    steps: List[StepResult] = field(default_factory=list)
    elapsed: float = 0.0
    reason: str = ""
    blocked: bool = False
    memory: Optional[Any] = None

    @property
    def succeeded(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps
                   if not s.ok and not s.skipped and not s.blocked)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok), "task_state": self.task_state,
            "plan_id": self.plan_id, "task_id": self.task_id,
            "elapsed": round(self.elapsed, 3), "reason": self.reason,
            "blocked": bool(self.blocked),
            "succeeded": self.succeeded, "failed": self.failed,
            "steps": [s.to_dict() for s in self.steps],
        }

    def describe(self) -> str:
        head = "✓ 任务完成" if self.ok else (
            "⊘ 任务被风控拦下" if self.blocked else "✗ 任务失败")
        lines = ["%s  %s" % (head, self.plan_id),
                 "  状态 %s   耗时 %.1fs   %s"
                 % (self.task_state, self.elapsed, self.reason)]
        for s in self.steps:
            lines.append("    %s" % s.describe())
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 默认执行器（对接阶段二/三/四）
# --------------------------------------------------------------------------
class DefaultRunner:
    """把步骤映射到阶段二/三/四的真实能力。

    ## 为什么是一个类而不是一个函数

    因为它需要**保存上下文**：打开的程序要在后续步骤里被找到，
    CDP 会话要在 navigate 之后继续用。函数没有地方放这些。

    ## 优雅降级

    每一类动作都包在 try/except 里。环境不具备（没装记事本、
    没起浏览器）时返回失败，**不抛异常** —— 异常会穿透调度循环，
    让"某个应用没装"看起来像"执行器坏了"。
    """

    def __init__(self) -> None:
        self._apps: Dict[str, Any] = {}      # target -> 启动信息
        self._sessions: Dict[str, Any] = {}  # url -> CDP 会话
        self.last_error = ""

    # ---------------- 分派 ----------------
    def __call__(self, step: Step) -> Any:
        kind = str(step.action or "")
        params = step.params or {}
        target = str(params.get("target", "") or "")
        text = str(params.get("text", "") or "")

        handler = {
            "open": self._open,
            "close": self._close,
            "navigate": self._navigate,
            "type": self._type,
            "click": self._click,
            "key": self._key,
            "wait": self._wait,
            "scroll": self._scroll,
            "screenshot": self._screenshot,
            "save": self._save,
        }.get(kind)

        if handler is None:
            return {"ok": False,
                    "detail": "未知动作 %r（执行器没有对应实现）" % kind}

        try:
            return handler(target, text, params, step)
        except Exception as exc:      # noqa: BLE001
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return {"ok": False,
                    "detail": "执行 %s 时出错：%s" % (kind, self.last_error)}

    # ---------------- 各类动作 ----------------
    def _open(self, target: str, text: str, params: Dict, step: Step) -> Dict:
        if not target:
            return {"ok": False, "detail": "没有目标程序名"}
        from ..app_manager import launcher as APP
        info = APP.launch(target)
        self._apps[target] = info
        hwnd = int(getattr(info, "hwnd", 0) or 0)
        if hwnd:
            step.hwnd = hwnd
        return {"ok": True, "detail": "已启动 %s" % target, "hwnd": hwnd}

    def _close(self, target: str, text: str, params: Dict,
               step: Step) -> Dict:
        from ..app_manager import windows as W
        info = self._apps.get(target)
        hwnd = int(getattr(info, "hwnd", 0) or 0) or int(step.hwnd or 0)
        if not hwnd and target:
            wins = W.list_windows(title_re=target)
            hwnd = int(wins[0].hwnd) if wins else 0
        if not hwnd:
            return {"ok": False, "detail": "找不到要关闭的窗口：%s" % target}
        from ..app_manager import state as ST
        ST.close(hwnd)
        return {"ok": True, "detail": "已请求关闭 %s" % target}

    def _navigate(self, target: str, text: str, params: Dict,
                  step: Step) -> Dict:
        if not target:
            return {"ok": False, "detail": "没有目标地址"}
        from ..web_cdp import launcher as L
        sess = L.open_url(target)
        self._sessions[target] = sess
        return {"ok": True, "detail": "已打开 %s" % target,
                "url": getattr(sess, "url", target)}

    def _type(self, target: str, text: str, params: Dict,
              step: Step) -> Dict:
        if not text:
            return {"ok": False, "detail": "没有要输入的文本"}
        from ..input import dispatcher as D
        hwnd = self._resolve_hwnd(target, step)
        if not hwnd:
            return {"ok": False,
                    "detail": "找不到输入目标：%s" % (target or "?")}
        res = D.dispatch_text(hwnd, text, human=True)
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": getattr(res, "detail", "") or "已输入"}

    def _click(self, target: str, text: str, params: Dict,
               step: Step) -> Dict:
        from ..uia import actions as A
        from ..uia import find as F
        from ..uia.find import Query
        el = F.find_one(Query(name=target), required=False)
        if el is None:
            return {"ok": False, "detail": "找不到元素：%s" % target}
        res = A.click(el)
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": getattr(res, "detail", "") or "已点击"}

    def _key(self, target: str, text: str, params: Dict,
             step: Step) -> Dict:
        key = (target or "enter").lower().replace(" ", "")
        combo = [k for k in key.split("+") if k] or ["enter"]
        from ..input import dispatcher as D
        hwnd = self._resolve_hwnd("", step)
        if not hwnd:
            return {"ok": False, "detail": "没有可发送按键的目标窗口"}
        res = D.dispatch_key(hwnd, combo, human=True)
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": "已发送 %s" % key}

    def _wait(self, target: str, text: str, params: Dict,
              step: Step) -> Dict:
        secs = float(params.get("seconds", 1.0) or 0.0)
        secs = max(0.0, min(secs, 600.0))   # 别让人传个 1e9 把任务挂死
        time.sleep(secs)
        return {"ok": True, "detail": "已等待 %.1fs" % secs}

    def _scroll(self, target: str, text: str, params: Dict,
                step: Step) -> Dict:
        from ..input import dispatcher as D
        hwnd = self._resolve_hwnd("", step)
        if not hwnd:
            return {"ok": False, "detail": "没有可滚动的目标窗口"}
        res = D.dispatch_scroll(hwnd, -3)
        return {"ok": bool(getattr(res, "ok", False)), "detail": "已滚动"}

    def _screenshot(self, target: str, text: str, params: Dict,
                    step: Step) -> Dict:
        from ..risk_detector import context as C
        hwnd = self._resolve_hwnd(target, step)
        errs: List[str] = []
        path = C.save_screenshot(hwnd, errors=errs) if hwnd else ""
        if not path:
            return {"ok": False,
                    "detail": "截图失败：%s" % (errs[0] if errs else "未知")}
        return {"ok": True, "detail": "已保存 %s" % path, "path": path}

    def _save(self, target: str, text: str, params: Dict,
              step: Step) -> Dict:
        """保存 = 发 Ctrl+S。**不处理"另存为"对话框** —— 那需要人。"""
        from ..input import dispatcher as D
        hwnd = self._resolve_hwnd(target, step)
        if not hwnd:
            return {"ok": False, "detail": "没有可保存的目标窗口"}
        res = D.dispatch_key(hwnd, ["ctrl", "s"], human=True)
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": "已发送 Ctrl+S"}

    # ---------------- 辅助 ----------------
    def _resolve_hwnd(self, target: str, step: Step) -> int:
        """找一个能用的 hwnd：记过的 > 步骤上带的 > 按标题找 > 前台。"""
        info = self._apps.get(target)
        hwnd = int(getattr(info, "hwnd", 0) or 0)
        if hwnd:
            return hwnd
        if step.hwnd:
            return int(step.hwnd)
        if target:
            from ..app_manager import windows as W
            wins = W.list_windows(title_re=target)
            if wins:
                return int(wins[0].hwnd)
        try:
            from .. import perception as P
            return int(P.foreground_hwnd() or 0)
        except Exception:      # noqa: BLE001
            return 0


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------
class Executor:
    """DAG 执行器。**阶段六的主入口。**

    ## 依赖注入

    ``runner`` / ``observer`` / ``verifier`` / ``recovery`` 都可以替换。
    这不是为了"灵活"，是为了**能测**：真实执行要动桌面、
    真实验证要读 UIA，把它们换成假实现就能在无桌面环境里
    把调度逻辑测干净。
    """

    def __init__(self,
                 runner: Optional[Callable[[Step], Any]] = None,
                 *,
                 observer: Optional[Callable[..., Any]] = None,
                 verifier: Optional[Callable[..., Any]] = None,
                 recovery: Optional[Callable[..., Any]] = None,
                 guard_enabled: bool = True,
                 detect_timeout: float = 3.0,
                 task_timeout: float = TASK_TIMEOUT,
                 max_retries: int = MAX_RETRIES,
                 max_switches: int = MAX_STRATEGY_SWITCHES,
                 parallel: bool = True,
                 max_workers: int = MAX_WORKERS,
                 memory: Optional[ShortTermMemory] = None,
                 ) -> None:
        self.runner = runner
        self._observer = observer
        self._verifier = verifier
        self._recovery = recovery
        self.guard_enabled = bool(guard_enabled)
        self.detect_timeout = float(detect_timeout)
        self.task_timeout = float(task_timeout)
        self.max_retries = int(max_retries)
        self.max_switches = int(max_switches)
        self.parallel = bool(parallel)
        self.max_workers = max(1, int(max_workers))
        self.memory = memory

        self._lock = threading.RLock()
        self._blocked = threading.Event()
        self._started = 0.0

    # ---------------- 延迟拿默认实现 ----------------
    def _obs(self) -> Optional[Callable]:
        if self._observer is not None:
            return self._observer
        try:
            from .observer import observe
            return observe
        except Exception:      # noqa: BLE001
            return None

    def _ver(self) -> Optional[Callable]:
        if self._verifier is not None:
            return self._verifier
        try:
            from .verifier import verify
            return verify
        except Exception:      # noqa: BLE001
            return None

    def _rec(self) -> Optional[Callable]:
        if self._recovery is not None:
            return self._recovery
        try:
            from .recovery import decide_recovery
            return decide_recovery
        except Exception:      # noqa: BLE001
            return None

    # ---------------- 风控闸（5E 挂载点 3/3）----------------
    def before_step(self, step: Step) -> Optional[Dict[str, Any]]:
        """这一步该不该执行？``None`` = 放行。

        走 :func:`risk_detector.integration.guard_before_action` ——
        轻量检测（只读窗口标题），**不截图**。因为规划器可能有很多步骤，
        每步截图 3 秒是灾难。
        """
        if not self.guard_enabled:
            return None
        try:
            from ..risk_detector import integration as _ri
        except Exception:      # noqa: BLE001
            return None
        try:
            if not _ri.is_installed():
                return None
            return _ri.guard_before_action(
                int(getattr(step, "hwnd", 0) or 0),
                action=str(getattr(step, "action", "") or "step"),
                current_step=str(getattr(step, "id", "") or ""))
        except BaseException:      # noqa: BLE001
            # ⚠ 风控**自己抛的异常必须传播**，不能吞。
            #    吞掉 HumanInterventionTimeout 等于把"等人超时"伪装成
            #    "检测通过" —— 那是静默硬闯，正是硬约束 9 禁止的。
            import sys as _s
            exc_type = _s.exc_info()[0]
            name = getattr(exc_type, "__name__", "")
            if name in ("RiskBlocked", "HumanInterventionTimeout",
                        "HumanInterventionRequired", "AbortSignal"):
                raise
            # 其余（集成层坏掉、导入失败）吞掉 —— 检测器坏了不该
            # 让整个执行器不能用
            return None

    # ---------------- 单步执行 ----------------
    def execute_step(self, step: Step,
                     mem: Optional[ShortTermMemory] = None) -> StepResult:
        """执行一个步骤。**含完整的风控 → 观测 → 执行 → 验证 → 恢复。**"""
        t0 = time.perf_counter()
        mem = mem or self.memory
        res = StepResult(step_id=step.id, state=str(step.state))

        # ---- 0) 全局超时 ----
        if self._timed_out():
            res.detail = "全局超时（%.0fs）" % self.task_timeout
            return res

        # ---- 1) 熔断标志 ----
        if self._blocked.is_set():
            res.blocked = True
            res.detail = "同批步骤已触发熔断，本步未执行"
            return res

        # ---- 2) 风控闸 ----
        gate = self.before_step(step)
        if gate is not None:
            res.blocked = True
            res.guard_result = gate
            res.detail = "风控拦截，步骤未执行"
            self._blocked.set()
            if mem is not None:
                mem.record_result(step.id, False, res.detail, blocked=True)
            return res

        # ---- 3) 观测 before（硬约束 8：禁止跳过）----
        before = self._observe(step, "before", mem)

        # ---- 4) 状态 → running ----
        self._set_state(step, "start", mem, res)

        # ---- 5) 执行 + 验证 + 恢复（循环）----
        while True:
            if self._timed_out():
                res.detail = "执行中超时"
                self._set_state(step, "fail", mem, res)
                break

            step.attempts += 1
            res.attempts = step.attempts
            if mem is not None:
                mem.bump_attempts(step.id)

            ok, detail, raw = self._run(step)
            if not ok and mem is not None:
                # 执行失败（含 runner 抛异常）都要记进 memory ——
                # 不然"为什么失败"在复盘时查不到
                mem.record_error(step.id, detail or "执行失败")
            after = self._observe(step, "after", mem)

            vres = self._verify(step, before, after, mem)
            verified = _verify_ok(vres, fallback=ok)

            if mem is not None:
                mem.record_result(step.id, verified, detail, raw=_brief(raw))

            if verified:
                res.ok = True
                res.detail = detail or "成功"
                res.verify = _brief(vres)
                self._set_state(step, "succeed", mem, res)
                break

            # ---- 失败 → 恢复决策 ----
            strategy = self._decide(step, detail, mem)
            res.strategy = strategy

            if strategy == "retry":
                if step.attempts >= self.max_retries:
                    # 重试用完 → 升级为换策略
                    strategy = "retry_with_fallback"
                    res.strategy = strategy
                else:
                    self._set_state(step, "fail", mem, res)
                    self._set_state(step, "retry", mem, res)
                    continue

            if strategy == "retry_with_fallback":
                if step.strategy_switches >= self.max_switches:
                    strategy = "pause_for_human"
                    res.strategy = strategy
                else:
                    step.strategy_switches += 1
                    res.switches = step.strategy_switches
                    if mem is not None:
                        mem.bump_switches(step.id)
                    self._set_state(step, "fail", mem, res)
                    self._set_state(step, "retry", mem, res)
                    continue

            if strategy == "skip":
                res.skipped = True
                res.detail = detail or "已跳过"
                self._set_state(step, "skip", mem, res)
                break

            if strategy == "pause_for_human":
                res.blocked = True
                res.detail = "自愈上限用尽，转人工"
                self._set_state(step, "block", mem, res)
                self._blocked.set()
                break

            # abort（默认兜底）
            res.detail = detail or "放弃"
            self._set_state(step, "abort", mem, res)
            break

        res.elapsed = time.perf_counter() - t0
        step.elapsed += res.elapsed
        res.state = str(step.state)
        return res

    # ---------------- 调度 ----------------
    def run(self, plan: Plan,
            mem: Optional[ShortTermMemory] = None) -> ExecutionResult:
        """按 DAG 跑完整个计划。**被拦截即停止后续步骤。**"""
        t_start = time.perf_counter()
        self._started = t_start
        self._blocked.clear()

        mem = mem or self.memory
        if mem is None:
            mem = ShortTermMemory(getattr(plan, "plan_id", "task"))
            self.memory = mem
        mem.bind_plan(plan)

        out = ExecutionResult(plan_id=str(getattr(plan, "plan_id", "")),
                              task_id=mem.task_id, memory=mem)

        # 任务级：pending → running
        self._task_state(mem, TaskState.RUNNING, "start")

        try:
            plan.assert_valid()
        except Exception as exc:      # noqa: BLE001
            out.task_state = str(TaskState.FAILED)
            out.reason = "计划不合法：%s" % exc
            out.elapsed = time.perf_counter() - t_start
            return out

        all_results: List[StepResult] = []

        while True:
            if self._timed_out():
                out.reason = "全局超时（%.0fs）" % self.task_timeout
                break

            completed = plan.completed_ids()
            ready = plan.ready_steps(completed)
            if not ready:
                break

            if self.parallel and len(ready) > 1:
                batch = self._run_parallel(plan, ready, mem)
            else:
                batch = []
                for sid in ready:
                    st = plan.step_by_id(sid)
                    if st is not None:
                        batch.append(self.execute_step(st, mem))

            all_results.extend(batch)

            # 熔断 → 整个任务停下
            if self._blocked.is_set():
                out.blocked = True
                out.reason = out.reason or "风控拦截或自愈上限用尽，任务暂停"
                break

            # 有失败且没有可恢复路径 → 停下
            if plan.blocked_forever():
                out.reason = "存在永远无法执行的步骤（前置已失败）"
                break

        out.steps = all_results
        out.elapsed = time.perf_counter() - t_start

        # ---- 定任务级终态 ----
        # ⚠ 判据用 `all(st.done for st in plan.steps)` 而不是只看本轮结果 ——
        # 因为 resume 时可能有步骤在**上一轮就已经成功**（本轮没跑它们），
        # 只看 all_results 会把这些算成"未执行"从而误判失败。
        if out.blocked:
            self._task_state(mem, TaskState.BLOCKED, "block")
            out.task_state = str(TaskState.BLOCKED)
        elif all(st.done for st in plan.steps):
            self._task_state(mem, TaskState.SUCCESS, "succeed")
            out.task_state = str(TaskState.SUCCESS)
            out.ok = True
            out.reason = out.reason or "全部步骤完成"
        else:
            self._task_state(mem, TaskState.FAILED, "fail")
            out.task_state = str(TaskState.FAILED)
            if not out.reason:
                left = sum(1 for st in plan.steps if not st.done)
                out.reason = ("有 %d 个步骤未完成" % left) if left else \
                    "有步骤失败"

        try:
            mem.note("执行结束：%s（%.1fs）" % (out.task_state, out.elapsed))
            mem.save()
        except Exception:      # noqa: BLE001
            pass
        return out

    def _run_parallel(self, plan: Plan, ready: List[str],
                      mem: ShortTermMemory) -> List[StepResult]:
        """并行跑一批。**任何一个触发熔断，其他的会看到标志。**"""
        steps = [plan.step_by_id(sid) for sid in ready]
        steps = [s for s in steps if s is not None]
        if not steps:
            return []

        out: List[StepResult] = []
        workers = min(self.max_workers, len(steps))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self.execute_step, s, mem): s
                        for s in steps}
                for fut in as_completed(futs):
                    st = futs[fut]
                    try:
                        out.append(fut.result())
                    except BaseException as exc:      # noqa: BLE001
                        out.append(StepResult(
                            step_id=st.id, ok=False,
                            error="%s: %s" % (type(exc).__name__, exc)))
        except Exception:      # noqa: BLE001
            # 线程池都起不来 —— 退回串行，别让任务卡死
            out = [self.execute_step(s, mem) for s in steps]
        return out

    # ---------------- 内部 ----------------
    def _timed_out(self) -> bool:
        return (time.perf_counter() - self._started) > self.task_timeout

    def _set_state(self, step: Step, event: str,
                   mem: Optional[ShortTermMemory],
                   res: StepResult) -> None:
        """状态转换 + 记账。**所有变更都走这里**（硬约束 4）。"""
        old = str(step.state)
        try:
            new = transition(old, event, scope=SCOPE_STEP)
        except StateError:
            # 非法转换（比如终态上再来一次）—— 保持原状，记一笔
            if mem is not None:
                mem.record_error(step.id,
                                 "非法状态转换 %s --%s" % (old, event))
            return
        step.state = new
        res.state = new
        if mem is not None:
            mem.record_transition(step.id, old, new, event, scope=SCOPE_STEP)

    def _task_state(self, mem: ShortTermMemory, target: TaskState,
                    event: str) -> None:
        old = str(getattr(mem, "task_state", TaskState.PENDING))
        try:
            new = transition(old, event, scope=SCOPE_TASK)
        except StateError:
            return
        try:
            mem.record_task_transition(old, new, event)
        except Exception:      # noqa: BLE001
            pass

    def _observe(self, step: Step, phase: str,
                 mem: Optional[ShortTermMemory]) -> Any:
        """观测一次。**硬约束 8：禁止跳过。**"""
        fn = self._obs()
        obs = None
        if fn is not None:
            try:
                obs = fn(hwnd=int(step.hwnd or 0) or None)
            except Exception as exc:      # noqa: BLE001
                obs = {"error": "%s: %s" % (type(exc).__name__, exc)}
        if mem is not None:
            try:
                mem.record_observation(step.id, phase, obs)
            except Exception:      # noqa: BLE001
                pass
        return obs

    def _verify(self, step: Step, before: Any, after: Any,
                mem: Optional[ShortTermMemory]) -> Any:
        fn = self._ver()
        if fn is None:
            return None
        try:
            return fn(step, before, after)
        except Exception as exc:      # noqa: BLE001
            return {"ok": False, "reason": "verifier_error",
                    "detail": "%s: %s" % (type(exc).__name__, exc)}

    def _decide(self, step: Step, detail: str,
                mem: Optional[ShortTermMemory]) -> str:
        """问 recovery 该走哪条路。**决策必须记入 memory**（硬约束 7）。"""
        fn = self._rec()
        strategy = "abort"
        reason = ""
        if fn is not None:
            try:
                got = fn(step, state=str(step.state), error=detail)
                strategy = str(getattr(got, "value", got) or "abort")
                reason = str(getattr(got, "reason", "") or "")
            except Exception as exc:      # noqa: BLE001
                reason = "recovery_error: %s" % type(exc).__name__
        else:
            # 没有 recovery 模块时用内置的保守策略
            if step.attempts < self.max_retries:
                strategy = "retry"
            elif step.strategy_switches < self.max_switches:
                strategy = "retry_with_fallback"
            else:
                strategy = "abort"

        if mem is not None:
            try:
                mem.record_decision(step.id, strategy,
                                    reason or ("第 %d 次失败：%s"
                                               % (step.attempts,
                                                  (detail or "")[:80])))
            except Exception:      # noqa: BLE001
                pass
        return strategy

    def _run(self, step: Step):
        """执行一步。返回 ``(ok, detail, raw)``。**不抛异常。**"""
        if self.runner is None:
            return False, "executor_not_implemented（没有注入 runner）", None
        try:
            raw = self.runner(step)
        except BaseException as exc:      # noqa: BLE001
            return False, "%s: %s" % (type(exc).__name__, exc), None

        if isinstance(raw, dict):
            return bool(raw.get("ok")), str(raw.get("detail", "")), raw
        if isinstance(raw, bool):
            return raw, "" if raw else "runner 返回 False", raw
        ok = bool(getattr(raw, "ok", True))
        return ok, str(getattr(raw, "detail", "") or ""), raw

    # ---------------- 断点恢复 ----------------
    def resume(self, task_id: str, *,
               directory: Optional[str] = None) -> ExecutionResult:
        """从磁盘恢复一个任务继续跑。

        ## 恢复什么

        - **计划结构**（步骤 + 依赖）—— 从存档里的 ``plan`` 快照还原
        - **步骤状态** —— 已成功的不会重跑，失败的重新进入调度
        - **attempts / strategy_switches** —— 自愈上限**跨恢复累加**

        最后一点很重要：不累加的话，"重启一次就重置重试次数"
        会变成一个无限重试的后门。
        """
        try:
            mem = ShortTermMemory.load(task_id, directory=directory)
        except Exception as exc:      # noqa: BLE001
            return ExecutionResult(task_id=task_id, ok=False,
                                   reason="无法读取任务存档：%s" % exc)

        plan = _plan_from_memory(mem)
        if plan is None:
            return ExecutionResult(task_id=task_id, ok=False,
                                   reason="存档里没有可还原的计划")

        # 把状态与计数搬回 Plan
        for st in plan.steps:
            rec = mem.steps.get(st.id) or {}
            if rec:
                st.state = str(rec.get("state", st.state) or st.state)
                st.attempts = int(rec.get("attempts", 0) or 0)
                st.strategy_switches = int(
                    rec.get("strategy_switches", 0) or 0)

        # 终态步骤不重跑：failed / blocked / running 回到 pending
        for st in plan.steps:
            if st.state in (str(StepState.FAILED), str(StepState.BLOCKED),
                            str(StepState.RUNNING)):
                st.state = str(StepState.PENDING)

        self.memory = mem
        return self.run(plan, mem)

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        lines = [
            "执行器",
            "  风控闸    : %s" % ("开" if self.guard_enabled
                                  else "关（仅测试）"),
            "  并行      : %s（最多 %d 线程）"
            % ("开" if self.parallel else "关", self.max_workers),
            "  单步重试  : %d 次" % self.max_retries,
            "  换策略    : %d 次" % self.max_switches,
            "  全局超时  : %.0f 分钟" % (self.task_timeout / 60.0),
            "  runner    : %s" % (type(self.runner).__name__
                                  if self.runner else "(未注入)"),
            "  observer  : %s" % ("已注入" if self._obs() else "缺失"),
            "  verifier  : %s" % ("已注入" if self._ver() else "缺失"),
            "  recovery  : %s" % ("已注入" if self._rec() else "缺失"),
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _plan_from_memory(mem: ShortTermMemory) -> Optional[Plan]:
    try:
        d = mem.plan
        if not d:
            return None
        return Plan.from_dict(d)
    except Exception:      # noqa: BLE001
        return None


def _verify_ok(vres: Any, *, fallback: bool) -> bool:
    """把验证结论压成 bool。

    ``vres is None``（没有 verifier）时退到 runner 的返回值 ——
    这是刻意的降级：没有验证器总比"所有步骤都算失败"好。
    """
    if vres is None:
        return bool(fallback)
    if isinstance(vres, bool):
        return vres
    if isinstance(vres, dict):
        return bool(vres.get("ok", False))
    return bool(getattr(vres, "ok", fallback))


def _brief(v: Any) -> Any:
    """把任意对象压成可序列化的小摘要。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _brief(x) for k, x in list(v.items())[:10]}
    for attr in ("to_dict", "describe"):
        fn = getattr(v, attr, None)
        if callable(fn):
            try:
                return _brief(fn())
            except Exception:      # noqa: BLE001
                pass
    return str(v)[:200]


def describe_planner() -> str:
    """给人看的规划与执行状态。"""
    try:
        from .planner import RuleBasedPlanner
        rp_desc = RuleBasedPlanner().describe()
    except Exception as exc:      # noqa: BLE001
        rp_desc = "规划器不可用：%s" % exc

    return "\n".join([
        "规划与执行（agentic_planner）：阶段六",
        "  执行器就绪：%s（EXECUTOR_READY=%s）" % (EXECUTOR_READY,
                                                  EXECUTOR_READY),
        "  %s" % rp_desc,
        "",
        "链路：parse_goal → planner.plan → plan.validate",
        "      → executor.run（风控闸 → 观测 → 执行 → 验证 → 恢复）",
        "      → memory.save（可断点恢复）",
        "",
        "三条上限（硬约束 6）：",
        "  单步重试 %d 次 / 换策略 %d 次 / 全局 %.0f 分钟"
        % (MAX_RETRIES, MAX_STRATEGY_SWITCHES, TASK_TIMEOUT / 60.0),
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
class _FakeRunner:
    """测试用 runner：按脚本返回结果。"""

    def __init__(self, script: Optional[Dict[str, Any]] = None,
                 default: Any = None) -> None:
        self.script = dict(script or {})
        self.default = default if default is not None else {"ok": True,
                                                            "detail": "ok"}
        self.calls: List[str] = []
        self.lock = threading.Lock()

    def __call__(self, step: Step) -> Any:
        with self.lock:
            self.calls.append(step.id)
            n = self.calls.count(step.id) - 1
        v = self.script.get(step.id, self.default)
        if isinstance(v, list):
            v = v[min(n, len(v) - 1)]
        if isinstance(v, BaseException):
            raise v
        if callable(v):
            return v(step)
        return v


def selfcheck(quick: bool = True) -> dict:
    """executor 自检。**用假 runner / 假 observer，不碰真实桌面。**"""
    import os
    import shutil
    import sys as _sys
    import tempfile
    import uuid

    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]
    from .goal import parse_goal
    from .plan import Plan, Step as PStep
    from .planner import RuleBasedPlanner

    tmp = tempfile.mkdtemp(prefix="avatar_exec_")

    def _mk_plan(txt="打开记事本输入hello", **kw):
        return RuleBasedPlanner(**kw).plan(parse_goal(txt))

    def _exec(runner, **kw):
        kw.setdefault("guard_enabled", False)      # 测试里关掉风控闸
        # ⚠ 必须注入一个临时目录的 memory —— 否则 Executor 会往
        #    ~/.avatar/tasks/ 里写一堆测试任务，污染用户的真实存档
        kw.setdefault("memory", ShortTermMemory(
            "chk%s" % uuid.uuid4().hex[:8], directory=tmp))
        # ⚠ 必须注入假的 observer / verifier —— 否则会去读**真实桌面**、
        #    用**真实判据**去验一个假 runner 的结果，结论必然是"失败"。
        #    要测真实 verifier 的地方会显式传（setdefault 不覆盖）。
        kw.setdefault("observer", lambda **k: {"title": "记事本"})
        kw.setdefault("verifier", lambda *a, **k: {"ok": True})
        return _me.Executor(runner, **kw)

    def _fail_ver(step, before, after):
        """恒判失败的假 verifier —— 用来测"失败 → 重试 → 恢复"那条路。"""
        return {"ok": False, "reason": "测试：判为失败"}

    def _s2_fail_ver(step, before, after):
        """只让 s2 失败 —— 用来测"部分成功后断点恢复"。"""
        return {"ok": step.id != "s2"}

    _counts: Dict[str, int] = {}

    def _follow_ver(step, before, after):
        """s2 第 1 次判失败、之后判成功 —— 测"重试后成功"。"""
        n = _counts.get(step.id, 0) + 1
        _counts[step.id] = n
        return {"ok": not (step.id == "s2" and n == 1)}

    try:
        # ============ 1) 常量（硬约束 6）============
        _add("★ 单步重试上限 3", _me.MAX_RETRIES == 3, str(_me.MAX_RETRIES))
        _add("★ 换策略上限 2", _me.MAX_STRATEGY_SWITCHES == 2,
             str(_me.MAX_STRATEGY_SWITCHES))
        _add("★ 全局超时 30 分钟", _me.TASK_TIMEOUT == 1800.0,
             "%.0fs" % _me.TASK_TIMEOUT)
        _add("EXECUTOR_READY 现在是 True", _me.EXECUTOR_READY is True, "True")
        _add("模块 docstring 含「能力边界」",
             "能力边界" in (_me.__doc__ or ""), "有")

        # ============ 2) 顺序执行 ============
        plan = _mk_plan("打开记事本输入hello")
        r = _FakeRunner()
        out = _exec(r).run(plan)
        _add("★ 两步全部成功 → 任务成功",
             out.ok is True and out.task_state == "success", out.describe())
        _add("★ runner 被调用了两次", len(r.calls) == 2, str(r.calls))
        _add("★ 执行顺序符合 DAG", r.calls == ["s1", "s2"], str(r.calls))
        _add("每步都记了结果",
             all(s.ok for s in out.steps), str([s.ok for s in out.steps]))

        # ============ 3) 状态机接入（硬约束 4）============
        _add("★ 步骤终态是 success",
             all(str(s.state) == "success" for s in plan.steps),
             str([s.state for s in plan.steps]))
        _add("★ memory 记了状态转换",
             len(out.memory.events_of("transition")) >= 2,
             "%d 条" % len(out.memory.events_of("transition")))
        _add("★ memory 里没有非法转换的错误记录",
             not any("非法状态转换" in str(e.get("error", {}))
                     for e in out.memory.errors()),
             "%d 条错误" % len(out.memory.errors()))

        # ============ 4) 并行（DAG 的价值）============
        plan_p = _mk_plan("打开浏览器，打开记事本")
        rp = _FakeRunner()
        _add("★ 两个 open 都无依赖（可并行）",
             plan_p.steps[0].depends_on == []
             and plan_p.steps[1].depends_on == [], "并行")
        out_p = _exec(rp).run(plan_p)
        _add("★ 并行批次都执行了",
             set(rp.calls) == {"s1", "s2"} and out_p.ok, str(rp.calls))

        rp2 = _FakeRunner()
        out_p2 = _exec(rp2, parallel=False).run(
            _mk_plan("打开浏览器，打开记事本"))
        _add("★ 关掉并行也能跑完",
             out_p2.ok and set(rp2.calls) == {"s1", "s2"}, str(rp2.calls))

        # ============ 5) 失败 → 重试 ============
        plan_f = _mk_plan("打开记事本输入hello")
        rf = _FakeRunner({"s2": [{"ok": False, "detail": "第一次失败"},
                                 {"ok": True, "detail": "第二次成功"}]})
        out_f = _exec(rf, verifier=_follow_ver).run(plan_f)
        _add("★ 第一次失败、第二次成功 → 任务成功",
             out_f.ok is True, out_f.describe())
        _add("★ s2 被调用了 2 次（重试了）",
             rf.calls.count("s2") == 2, str(rf.calls))
        _add("★ s2 的 attempts 记到 2",
             plan_f.step_by_id("s2").attempts == 2,
             str(plan_f.step_by_id("s2").attempts))

        # ============ 6) 重试上限（硬约束 6）============
        plan_lim = _mk_plan("打开记事本输入hello")
        rl = _FakeRunner({"s2": {"ok": False, "detail": "一直失败"}})
        out_lim = _exec(rl, verifier=_s2_fail_ver).run(plan_lim)
        _add("★ 一直失败 → 任务失败", out_lim.ok is False, out_lim.describe())
        _add("★★ 调用次数不超过「重试上限 + 换策略上限 + 1」",
             rl.calls.count("s2") <= _me.MAX_RETRIES
             + _me.MAX_STRATEGY_SWITCHES + 1,
             "s2 调了 %d 次" % rl.calls.count("s2"))
        _add("★ s2 最终状态是 failed",
             str(plan_lim.step_by_id("s2").state) in ("failed", "blocked"),
             str(plan_lim.step_by_id("s2").state))

        # ============ 7) 自愈决策记入 memory（硬约束 7）============
        _add("★★ 自愈决策被记入 memory（禁止静默重试）",
             len(out_lim.memory.decisions()) >= 1,
             "%d 条决策" % len(out_lim.memory.decisions()))
        _add("★ 决策里有策略名",
             all(d.get("strategy") for d in out_lim.memory.decisions()),
             str([d.get("strategy") for d in out_lim.memory.decisions()]))

        # ============ 8) 观测（硬约束 8）============
        seen_obs: List[str] = []

        def _fake_obs(hwnd=None):
            seen_obs.append("x")
            return {"title": "记事本", "url": ""}

        plan_o = _mk_plan("打开记事本输入hello")
        out_o = _exec(_FakeRunner(), observer=_fake_obs).run(plan_o)
        _add("★★ 每步执行前后各观测一次（2 步 × 2 = 4 次）",
             len(seen_obs) == 4, "%d 次" % len(seen_obs))
        _add("★ 观测被记入 memory",
             len(out_o.memory.events_of("observation")) == 4,
             "%d 条" % len(out_o.memory.events_of("observation")))
        _add("观测记录带 phase",
             {e["phase"] for e in out_o.memory.events_of("observation")}
             == {"before", "after"}, "before/after")

        # ============ 9) 验证 ============
        _ver_fail = _fail_ver

        plan_v = _mk_plan("打开记事本")
        out_v = _exec(_FakeRunner(), verifier=_ver_fail).run(plan_v)
        _add("★ verifier 说失败 → 步骤失败（runner 成功也没用）",
             out_v.ok is False, out_v.describe())

        def _ver_ok(step, before, after):
            return {"ok": True}

        plan_v2 = _mk_plan("打开记事本")
        out_v2 = _exec(_FakeRunner({"s1": {"ok": False,
                                           "detail": "runner 说失败"}}),
                       verifier=_ver_ok).run(plan_v2)
        _add("★ verifier 说成功 → 步骤成功（runner 失败也放过）",
             out_v2.ok is True, out_v2.describe())

        def _ver_boom(step, before, after):
            raise RuntimeError("验证器炸了")

        plan_v3 = _mk_plan("打开记事本")
        out_v3 = _exec(_FakeRunner(), verifier=_ver_boom).run(plan_v3)
        _add("★ verifier 抛异常不拖垮任务",
             isinstance(out_v3, _me.ExecutionResult), "没崩")

        # ============ 10) recovery 决策 ============
        def _rec_skip(step, state="", error=""):
            return "skip"

        plan_r = _mk_plan("打开记事本")
        out_r = _exec(_FakeRunner({"s1": {"ok": False}}),
                      verifier=_ver_fail, recovery=_rec_skip).run(plan_r)
        _add("★ recovery 说 skip → 步骤被跳过",
             out_r.steps[0].skipped is True, out_r.describe())
        _add("★ 跳过的步骤算完成任务", out_r.ok is True, out_r.task_state)

        def _rec_abort(step, state="", error=""):
            return "abort"

        plan_a = _mk_plan("打开记事本")
        out_a = _exec(_FakeRunner({"s1": {"ok": False}}),
                      verifier=_ver_fail, recovery=_rec_abort).run(plan_a)
        _add("★ recovery 说 abort → 任务失败",
             out_a.ok is False, out_a.task_state)

        def _rec_boom(step, state="", error=""):
            raise RuntimeError("恢复器炸了")

        plan_rb = _mk_plan("打开记事本")
        out_rb = _exec(_FakeRunner({"s1": {"ok": False}}),
                       verifier=_ver_fail, recovery=_rec_boom).run(plan_rb)
        _add("★ recovery 抛异常不拖垮任务",
             isinstance(out_rb, _me.ExecutionResult), "没崩")

        # ============ 11) runner 抛异常 ============
        plan_x = _mk_plan("打开记事本")
        rx = _FakeRunner({"s1": OSError("runner 炸了")})
        out_x = _exec(rx, verifier=_fail_ver,
                      recovery=lambda *a, **k: "abort").run(plan_x)
        _add("★ runner 抛异常被收敛成失败（不外泄）",
             out_x.ok is False and isinstance(out_x, _me.ExecutionResult),
             out_x.describe())
        _add("异常信息记进了 memory",
             any("OSError" in str(e.get("error", {}))
                 for e in out_x.memory.errors()),
             "%d 条错误" % len(out_x.memory.errors()))

        # ============ 12) 全局超时 ============
        plan_t = _mk_plan("打开记事本输入hello")

        def _slow(step):
            time.sleep(0.2)
            return {"ok": True}

        out_t = _exec(_slow, task_timeout=0.05, parallel=False).run(plan_t)
        _add("★ 全局超时会中断执行",
             out_t.elapsed < 3.0, "%.2fs" % out_t.elapsed)
        _add("★ 超时后任务不算成功", out_t.ok is False, out_t.task_state)

        # ============ 13) 风控闸 ============
        from ..risk_detector import integration as RI
        was = RI.is_installed()
        try:
            RI.uninstall()
            plan_g = _mk_plan("打开记事本")
            out_g = _exec(_FakeRunner(), guard_enabled=True).run(plan_g)
            _add("★ 风控集成未启用时闸门放行",
                 out_g.ok is True, out_g.task_state)

            RI.install(enabled=True, mode_=RI.MODE_OBSERVE)
            plan_g2 = _mk_plan("打开记事本")
            out_g2 = _exec(_FakeRunner(), guard_enabled=True).run(plan_g2)
            _add("★ observe 模式下也不阻断",
                 out_g2.ok is True, out_g2.task_state)
        finally:
            RI.uninstall()
            RI.install(enabled=was)

        # ============ 14) 没有 runner ============
        plan_nr = _mk_plan("打开记事本")
        out_nr = _exec(None, verifier=_fail_ver,
                       recovery=lambda *a, **k: "abort").run(plan_nr)
        _add("★ 没注入 runner 时返回失败（不崩）",
             out_nr.ok is False
             and any("not_implemented" in s.detail for s in out_nr.steps),
             out_nr.describe())

        # ============ 15) memory 落盘与断点恢复 ============
        plan_m = _mk_plan("打开记事本输入hello")
        mem = ShortTermMemory("exectest", directory=tmp)
        _exec(_FakeRunner()).run(plan_m, mem)
        _add("★ 执行后 memory 自动落盘",
             os.path.isfile(os.path.join(tmp, "exectest.json")), "有")
        loaded = ShortTermMemory.load("exectest", directory=tmp)
        _add("★★ 断点恢复：能读出步骤状态",
             loaded.step_state("s1") == "success", loaded.step_state("s1"))
        _add("★★ 断点恢复：能还原计划",
             _me._plan_from_memory(loaded) is not None, "能")

        # 恢复执行：已成功的不重跑
        plan_m2 = _mk_plan("打开记事本输入hello")
        mem2 = ShortTermMemory("exectest2", directory=tmp)
        r_first = _FakeRunner({"s2": {"ok": False, "detail": "第一次失败"}})
        _exec(r_first, verifier=_s2_fail_ver,
              recovery=lambda *a, **k: "abort").run(plan_m2, mem2)

        ex_resume = _exec(_FakeRunner())
        out_res = ex_resume.resume("exectest2", directory=tmp)
        _add("★★ resume 能把任务接着跑完",
             out_res.ok is True, out_res.describe())
        _add("★★ resume 不重跑已成功的步骤（s1 一次都没跑）",
             ex_resume.runner.calls.count("s1") == 0,
             "calls=%s" % ex_resume.runner.calls)
        _add("★★ resume 只补跑失败的 s2",
             ex_resume.runner.calls.count("s2") == 1,
             "calls=%s" % ex_resume.runner.calls)

        out_bad = _exec(_FakeRunner()).resume("不存在", directory=tmp)
        _add("★ resume 不存在的任务不崩",
             isinstance(out_bad, _me.ExecutionResult), "没崩")

        # ============ 16) 非法计划 ============
        bad_plan = Plan(steps=[PStep(id="a", action="click",
                                     depends_on=["b"]),
                               PStep(id="b", action="click",
                                     depends_on=["a"])])
        out_bad2 = _exec(_FakeRunner()).run(bad_plan)
        _add("★ 非法计划被拒（有环）",
             out_bad2.ok is False and "不合法" in out_bad2.reason,
             out_bad2.reason)

        # ============ 17) 全部已完成 ============
        done_plan = Plan(steps=[PStep(id="a", action="wait")])
        done_plan.steps[0].state = "success"
        out_e = _exec(_FakeRunner()).run(done_plan)
        _add("★ 全部已完成的计划直接成功",
             out_e.ok is True and out_e.steps == [], out_e.task_state)

        # ============ 18) 展示 ============
        _add("Executor.describe 不抛",
             isinstance(_exec(_FakeRunner()).describe(), str), "有")
        _add("describe 列出三条上限",
             all(k in _exec(_FakeRunner()).describe()
                 for k in ("重试", "换策略", "超时")), "有")
        _add("describe_planner 不抛",
             isinstance(_me.describe_planner(), str), "有")
        _add("StepResult.describe 可读",
             isinstance(_me.StepResult(step_id="s1", ok=True).describe(),
                        str), "有")
        _add("ExecutionResult.to_dict JSON 安全",
             __import__("json").dumps(out.to_dict()) != "", "ok")

        # ============ 19) 与 5E 接口兼容 ============
        _add("★ 仍有 before_step（5E 挂载点 3/3）",
             callable(getattr(_me.Executor, "before_step", None)), "有")
        _add("★ 仍有 describe_planner（selfcheck 脚本依赖）",
             callable(_me.describe_planner), "有")
        _add("★ Step 与 plan.Step 是同一个类",
             _me.Step is PStep, "同一个")

        # ============ 20) 不 import asyncio（硬约束）============
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
        _add("★★ 不 import asyncio（用 threading）",
             "asyncio" not in mods, sorted(mods))
        _add("★ 用 concurrent.futures（基于 threading）",
             "concurrent" in mods, "有")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    return {
        "module": "executor",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_planner())
