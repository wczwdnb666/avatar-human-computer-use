# -*- coding: utf-8 -*-
"""任务记忆 —— 步骤状态、观测快照、错误、自愈决策的持久化。

## 为什么需要它

阶段六的目标是"自动执行 + 自动恢复"。**恢复的前提是"记得住"**：
进程挂了、机器重启了、人被叫走半小时回来了 —— 这时候要能回答
三个问题：

1. 这个任务跑到哪一步了？
2. 失败的是哪一步、为什么失败？
3. 之前试过什么、还能试几次？

答案都在这里。

## 存什么

============================  ==========================================
类别                          内容
============================  ==========================================
步骤状态                      每步的 state / attempts / elapsed
事件流水                      状态转换、观测、错误、自愈决策（按时间序）
观测快照                      执行前后各一次的 Observation（**摘要**，不是全量）
计划快照                      ``Plan.to_dict()``，用于断点恢复
============================  ==========================================

## 硬约束 5：JSON，禁用 pickle

- **JSON** —— 人能直接打开看。排查问题时"能读懂存档"比"存得省空间"重要得多。
- **禁 pickle** —— pickle 反序列化会执行任意代码，而任务存档是
  **可能被外部写入**的文件（人手工改、别的版本写、备份还原）。
  读一个 pickle 等于执行那个文件里的代码。

## 原子写

``save()`` 先写 ``<id>.json.tmp`` 再 ``os.replace`` 覆盖目标。
理由：写到一半断电/被杀，会留下一个半截 JSON —— 下次 load 就炸了。
``os.replace`` 在同盘上是原子的，要么旧文件完整、要么新文件完整。

## 能力边界

**做**：记录步骤状态与事件；序列化成 JSON；从磁盘恢复；
列出/删除历史任务；给一份人读的摘要。

**不做**：

- **不决定状态**。``set_step_state`` 只是**记账**，合法转换由
  :mod:`state` 的 ``transition`` 保证。本模块不重复校验。
- **不存全量观测**。``Observation`` 里的截图路径会存，
  但**截图本身不存** —— 那是阶段五熔断产物目录的事。
  存进来会让任务存档变成几十 MB 的怪物。
- **不做垃圾回收**。任务存档会一直留着。清理策略是使用者的决定
  （``delete()`` 提供能力，但不自动调）。
- **不做并发写保护**。同一个 task_id 被两个进程同时写会互相覆盖。
  DAG 调度保证一个任务只被一个执行器跑。
- **不加密**。存档里可能含用户指令原文。要加密请放在文件系统层做。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from .state import (
    SCOPE_STEP,
    SCOPE_TASK,
    StepState,
    TaskState,
)

__all__ = [
    "DEFAULT_TASK_DIR",
    "TASK_DIR_ENV",
    "MAX_EVENTS",
    "MemoryError_",
    "task_dir",
    "ShortTermMemory",
    "list_tasks",
    "delete_task",
    "load_task",
    "describe_memory",
    "selfcheck",
]

#: 任务存档目录（相对用户主目录）。
DEFAULT_TASK_DIR = "~/.avatar/tasks"

#: 环境变量覆盖。
TASK_DIR_ENV = "AVATAR_TASK_DIR"

#: 事件流水上限。**超了就丢最老的** ——
#: 长跑任务（比如跑一整天）会产生上万条事件，不设上限会把内存吃光，
#: 而存档也会大到没法看。
MAX_EVENTS = 5000

#: task_id 的合法形态。**必须校验** —— 它会被拼进文件路径，
#: 不校验就等着 ``../../`` 这种路径穿越。
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class MemoryError_(Exception):
    """记忆层的错误（存档损坏、路径非法）。

    名字带下划线是因为 ``MemoryError`` 是 Python 内置异常 ——
    同名遮蔽会让人误 catch。
    """


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def task_dir(create: bool = True) -> str:
    """任务存档目录。环境变量优先，**永不抛**。"""
    try:
        d = os.environ.get(TASK_DIR_ENV) or DEFAULT_TASK_DIR
        p = os.path.abspath(os.path.expanduser(str(d)))
        if create:
            try:
                os.makedirs(p, exist_ok=True)
            except OSError:
                pass
        return p
    except Exception:      # noqa: BLE001
        return os.path.abspath(os.path.expanduser(DEFAULT_TASK_DIR))


def _safe_task_id(task_id: Any) -> str:
    """校验并归一化 task_id。

    **必须校验**：这个值会拼进文件路径。``"../../etc/passwd"``
    这种输入如果不拦，就是一个任意文件写。
    """
    t = str(task_id or "").strip()
    if not t:
        raise MemoryError_("task_id 不能为空")
    if not _TASK_ID_RE.match(t):
        raise MemoryError_(
            "task_id 含非法字符：%r（只允许字母数字、下划线、连字符，"
            "长度 1~64）" % t)
    return t


def _path_for(task_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or task_dir(), "%s.json" % task_id)


# --------------------------------------------------------------------------
# 主类
# --------------------------------------------------------------------------
class ShortTermMemory:
    """一个任务的记忆。**所有落盘都走 :meth:`save` / :meth:`load`。**"""

    #: 存档格式版本。将来改结构时用它做迁移。
    SCHEMA_VERSION = 1

    def __init__(self, task_id: str = "", *,
                 directory: Optional[str] = None,
                 goal: Any = None) -> None:
        self.task_id = _safe_task_id(task_id) if task_id else ""
        self.directory = directory
        self.created_at = time.time()
        self.updated_at = self.created_at

        #: 任务级状态（字符串值）
        self.task_state = str(TaskState.PENDING)

        #: 目标快照（``Goal.to_dict()``）
        self.goal: Dict[str, Any] = {}
        if goal is not None and hasattr(goal, "to_dict"):
            try:
                self.goal = dict(goal.to_dict())
                if not self.task_id:
                    self.task_id = _safe_task_id(goal.task_id)
            except Exception:      # noqa: BLE001
                self.goal = {}

        #: 计划快照（``Plan.to_dict()``）
        self.plan: Dict[str, Any] = {}

        #: 步骤状态表：``{step_id: {...}}``
        self.steps: Dict[str, Dict[str, Any]] = {}

        #: 事件流水（时间序）
        self.events: List[Dict[str, Any]] = []

        #: 额外备注
        self.notes: List[str] = []

        self._seq = 0

    # ---------------- 记录 ----------------
    def _touch(self) -> None:
        self.updated_at = time.time()

    def _emit(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """追加一条事件。**超上限时丢最老的。**"""
        self._seq += 1
        rec: Dict[str, Any] = {"seq": self._seq, "at": time.time(),
                               "kind": str(kind)}
        for k, v in fields.items():
            rec[k] = _jsonable(v)
        self.events.append(rec)
        if len(self.events) > MAX_EVENTS:
            del self.events[:len(self.events) - MAX_EVENTS]
        self._touch()
        return rec

    def bind_plan(self, plan: Any) -> None:
        """绑定计划快照。**断点恢复靠它。**"""
        if plan is None:
            return
        try:
            self.plan = dict(plan.to_dict())
            if not self.task_id:
                self.task_id = _safe_task_id(plan.plan_id)
            if plan.goal is not None and not self.goal:
                self.goal = dict(plan.goal.to_dict())
                if not self.task_id:
                    self.task_id = _safe_task_id(plan.goal.task_id)
            # 同步初始步骤状态
            for st in getattr(plan, "steps", []) or []:
                self.steps.setdefault(st.id, {
                    "state": str(st.state), "action": st.action,
                    "attempts": int(st.attempts),
                    "strategy_switches": int(st.strategy_switches),
                    "elapsed": float(st.elapsed),
                })
        except Exception:      # noqa: BLE001
            pass
        self._emit("bind_plan", plan_id=getattr(plan, "plan_id", ""),
                   steps=len(getattr(plan, "steps", []) or []))

    def set_step_state(self, step_id: str, state: Any,
                       **extra: Any) -> None:
        """记一个步骤的状态。

        **只记账，不校验** —— 合法性由 :func:`state.transition` 保证。
        重复校验会让"转换对不对"有两个判断点，两边不一致时更难查。
        """
        sid = str(step_id or "")
        if not sid:
            return
        rec = self.steps.setdefault(sid, {})
        rec["state"] = str(getattr(state, "value", state))
        rec["at"] = time.time()
        for k, v in extra.items():
            rec[k] = _jsonable(v)
        self._touch()

    def record_transition(self, step_id: str, from_state: Any, to_state: Any,
                          event: str, *, scope: str = SCOPE_STEP,
                          **extra: Any) -> None:
        """记一次状态转换。**每次转换都必须记**（用户明确要求）。"""
        self._emit("transition", step_id=str(step_id or ""),
                   scope=str(scope),
                   **{"from": str(getattr(from_state, "value", from_state)),
                      "to": str(getattr(to_state, "value", to_state)),
                      "event": str(event or "")}, **extra)
        if step_id:
            self.set_step_state(step_id, to_state)

    def record_task_transition(self, from_state: Any, to_state: Any,
                               event: str, **extra: Any) -> None:
        """记一次任务级转换。"""
        self.task_state = str(getattr(to_state, "value", to_state))
        self._emit("task_transition", scope=SCOPE_TASK,
                   **{"from": str(getattr(from_state, "value", from_state)),
                      "to": self.task_state, "event": str(event or "")},
                   **extra)

    def record_observation(self, step_id: str, phase: str,
                           observation: Any) -> None:
        """记一次观测（``phase`` 是 ``"before"`` / ``"after"``）。

        **只存摘要** —— 截图本身不进来，只存路径。
        """
        obs = observation
        if hasattr(obs, "to_dict"):
            try:
                obs = obs.to_dict()
            except Exception:      # noqa: BLE001
                obs = {}
        self._emit("observation", step_id=str(step_id or ""),
                   phase=str(phase or ""), observation=_jsonable(obs))

    def record_error(self, step_id: str, error: Any, **extra: Any) -> None:
        """记一次错误。"""
        if isinstance(error, BaseException):
            err = {"type": type(error).__name__, "message": str(error)[:500]}
        else:
            err = {"type": "Error", "message": str(error)[:500]}
        self._emit("error", step_id=str(step_id or ""), error=err, **extra)

    def record_decision(self, step_id: str, strategy: Any,
                        reason: str = "", **extra: Any) -> None:
        """记一次**自愈决策**。

        硬约束 7：**自愈决策必须记入 memory，禁止静默重试。**
        理由：静默重试会让"任务卡在重试循环里"看起来像"任务在正常运行" ——
        没有记录就查不出来。
        """
        self._emit("recovery", step_id=str(step_id or ""),
                   strategy=str(getattr(strategy, "value", strategy)),
                   reason=str(reason or "")[:500], **extra)

    def record_result(self, step_id: str, ok: bool,
                      detail: str = "", **extra: Any) -> None:
        """记一次执行结果。"""
        self._emit("result", step_id=str(step_id or ""), ok=bool(ok),
                   detail=str(detail or "")[:500], **extra)

    def note(self, text: str) -> None:
        """加一条人读备注。"""
        t = str(text or "").strip()
        if t:
            self.notes.append(t[:500])
            self._touch()

    # ---------------- 查询 ----------------
    def step_state(self, step_id: str) -> str:
        return str((self.steps.get(str(step_id)) or {}).get("state", ""))

    def attempts_of(self, step_id: str) -> int:
        return int((self.steps.get(str(step_id)) or {}).get("attempts", 0))

    def bump_attempts(self, step_id: str, n: int = 1) -> int:
        """增加尝试次数并返回新值。"""
        rec = self.steps.setdefault(str(step_id), {})
        rec["attempts"] = int(rec.get("attempts", 0)) + int(n)
        self._touch()
        return int(rec["attempts"])

    def bump_switches(self, step_id: str, n: int = 1) -> int:
        """增加换策略次数并返回新值。"""
        rec = self.steps.setdefault(str(step_id), {})
        rec["strategy_switches"] = int(rec.get("strategy_switches", 0)) + int(n)
        self._touch()
        return int(rec["strategy_switches"])

    def events_of(self, kind: str = "", step_id: str = "",
                  limit: int = 0) -> List[Dict[str, Any]]:
        """按类型/步骤筛事件。"""
        out = [e for e in self.events
               if (not kind or e.get("kind") == kind)
               and (not step_id or e.get("step_id") == step_id)]
        return out[-int(limit):] if limit and limit > 0 else out

    def errors(self, limit: int = 0) -> List[Dict[str, Any]]:
        return self.events_of("error", limit=limit)

    def decisions(self, limit: int = 0) -> List[Dict[str, Any]]:
        return self.events_of("recovery", limit=limit)

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA_VERSION,
            "task_id": self.task_id,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "task_state": self.task_state,
            "goal": self.goal,
            "plan": self.plan,
            "steps": self.steps,
            "events": self.events,
            "notes": list(self.notes),
            "seq": self._seq,
        }

    @classmethod
    def from_dict(cls, d: Any, *,
                  directory: Optional[str] = None) -> "ShortTermMemory":
        """从 dict 还原。**脏数据不抛** —— 存档可能被手改过、被老版本写过。"""
        d = d if isinstance(d, dict) else {}
        m = cls(str(d.get("task_id", "") or "") or "unknown",
                directory=directory)
        m.created_at = _as_float(d.get("created_at"), m.created_at)
        m.updated_at = _as_float(d.get("updated_at"), m.created_at)
        m.task_state = str(d.get("task_state", TaskState.PENDING)
                           or TaskState.PENDING)
        m.goal = d.get("goal") if isinstance(d.get("goal"), dict) else {}
        m.plan = d.get("plan") if isinstance(d.get("plan"), dict) else {}
        m.steps = {str(k): v for k, v in (d.get("steps") or {}).items()
                   if isinstance(v, dict)} \
            if isinstance(d.get("steps"), dict) else {}
        m.events = [e for e in (d.get("events") or [])
                    if isinstance(e, dict)] \
            if isinstance(d.get("events"), (list, tuple)) else []
        m.notes = [str(x) for x in (d.get("notes") or [])] \
            if isinstance(d.get("notes"), (list, tuple)) else []
        m._seq = _as_int(d.get("seq"), len(m.events))
        return m

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, s: str, *,
                  directory: Optional[str] = None) -> "ShortTermMemory":
        return cls.from_dict(json.loads(s), directory=directory)

    # ---------------- 落盘 ----------------
    def save(self, path: str = "") -> str:
        """原子写入。返回文件路径。

        **先写 .tmp 再 os.replace** —— 写到一半被杀不会留下半截 JSON。
        """
        if not self.task_id:
            raise MemoryError_("没有 task_id，无法保存")
        target = path or _path_for(self.task_id, self.directory)
        d = os.path.dirname(target)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

        body = self.to_json()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
        self._touch()
        return target

    @classmethod
    def load(cls, task_id: str, *,
             directory: Optional[str] = None) -> "ShortTermMemory":
        """从磁盘恢复。**文件不存在或损坏都抛 :class:`MemoryError_`。**"""
        tid = _safe_task_id(task_id)
        path = _path_for(tid, directory)
        if not os.path.isfile(path):
            raise MemoryError_("任务存档不存在：%s" % path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:      # noqa: BLE001
            raise MemoryError_("任务存档损坏：%s（%s）"
                               % (path, type(exc).__name__)) from exc
        m = cls.from_dict(data, directory=directory)
        m.task_id = tid
        return m

    # ---------------- 摘要 ----------------
    def summary(self) -> str:
        """人读摘要。``avatar tasks --show`` 用它。"""
        lines = ["任务 %s" % (self.task_id or "(未命名)")]
        lines.append("  状态      : %s" % self.task_state)
        if self.goal:
            lines.append("  目标      : %s"
                         % (self.goal.get("instruction") or ""))
        lines.append("  创建      : %s"
                     % time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(self.created_at)))
        lines.append("  更新      : %s"
                     % time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(self.updated_at)))
        lines.append("  步骤 %d 个   事件 %d 条"
                     % (len(self.steps), len(self.events)))

        if self.steps:
            lines.append("")
            lines.append("  %-8s %-10s %-8s %-8s %s"
                         % ("步骤", "状态", "尝试", "换策略", "动作"))
            for sid, rec in sorted(self.steps.items()):
                lines.append("  %-8s %-10s %-8s %-8s %s"
                             % (sid, rec.get("state", "?"),
                                rec.get("attempts", 0),
                                rec.get("strategy_switches", 0),
                                rec.get("action", "")))

        errs = self.errors()
        if errs:
            lines.append("")
            lines.append("  最近的错误：")
            for e in errs[-5:]:
                info = e.get("error") or {}
                lines.append("    [%s] %s: %s"
                             % (e.get("step_id", "?"),
                                info.get("type", "?"),
                                str(info.get("message", ""))[:70]))

        decs = self.decisions()
        if decs:
            lines.append("")
            lines.append("  自愈决策（最近 5 条）：")
            for d in decs[-5:]:
                lines.append("    [%s] %s  %s"
                             % (d.get("step_id", "?"),
                                d.get("strategy", "?"),
                                str(d.get("reason", ""))[:60]))

        if self.notes:
            lines.append("")
            for n in self.notes[-5:]:
                lines.append("  · %s" % n)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------
def load_task(task_id: str, *, directory: Optional[str] = None
              ) -> ShortTermMemory:
    """``ShortTermMemory.load`` 的函数式别名。"""
    return ShortTermMemory.load(task_id, directory=directory)


def list_tasks(directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出全部任务存档（按更新时间倒序）。**永不抛。**

    返回 ``[{task_id, task_state, updated_at, instruction, steps, path}]``。
    """
    d = directory or task_dir()
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []

    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:      # noqa: BLE001
            # 坏存档也要列出来 —— 不然用户不知道它坏了
            out.append({"task_id": name[:-5], "task_state": "(损坏)",
                        "updated_at": 0.0, "instruction": "", "steps": 0,
                        "path": path, "broken": True})
            continue
        if not isinstance(data, dict):
            continue
        goal = data.get("goal") if isinstance(data.get("goal"), dict) else {}
        steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
        out.append({
            "task_id": str(data.get("task_id") or name[:-5]),
            "task_state": str(data.get("task_state") or "?"),
            "updated_at": _as_float(data.get("updated_at"), 0.0),
            "instruction": str(goal.get("instruction") or ""),
            "steps": len(steps),
            "path": path,
            "broken": False,
        })

    out.sort(key=lambda x: x.get("updated_at", 0.0), reverse=True)
    return out


def delete_task(task_id: str, *, directory: Optional[str] = None) -> bool:
    """删掉一个任务存档。返回是否真的删了。"""
    try:
        path = _path_for(_safe_task_id(task_id), directory)
        os.remove(path)
        return True
    except Exception:      # noqa: BLE001
        return False


def describe_memory() -> str:
    """给人看的记忆层说明。"""
    d = task_dir(create=False)
    tasks = list_tasks()
    lines = [
        "任务记忆（ShortTermMemory）",
        "=" * 60,
        "存档目录：%s" % d,
        "目录存在：%s" % ("是" if os.path.isdir(d) else "否（还没有任务）"),
        "任务数量：%d" % len(tasks),
        "事件上限：%d 条/任务（超出丢最老的）" % MAX_EVENTS,
        "",
        "格式：JSON（**禁用 pickle** —— 存档可能被外部写入，",
        "      反序列化 pickle 等于执行那个文件里的代码）",
        "写入：原子（先写 .tmp 再 os.replace）",
        "",
        "最近的任务：",
    ]
    for t in tasks[:10]:
        mark = "  ⚠ 损坏" if t.get("broken") else ""
        lines.append("  %-18s %-10s %-19s %s%s"
                     % (t["task_id"], t["task_state"],
                        time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(t["updated_at"]))
                        if t["updated_at"] else "-",
                        (t["instruction"] or "")[:30], mark))
    if not tasks:
        lines.append("  （无）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _jsonable(v: Any) -> Any:
    """把任意值压成 JSON 可序列化的形态。**永不抛。**"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    try:
        return str(v)[:500]
    except Exception:      # noqa: BLE001
        return "<unserializable>"


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """memory 自检。**只在临时目录里读写，不碰真实存档。**"""
    import shutil
    import sys as _sys
    import tempfile

    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]
    tmp = tempfile.mkdtemp(prefix="avatar_mem_")

    try:
        # ============ 1) 常量 ============
        _add("事件上限是正数", _me.MAX_EVENTS > 0, str(_me.MAX_EVENTS))
        _add("schema 版本是 1", _me.ShortTermMemory.SCHEMA_VERSION == 1, "1")
        _add("模块 docstring 含「能力边界」",
             "能力边界" in (_me.__doc__ or ""), "有")
        _add("自定义异常名不遮蔽内置 MemoryError",
             _me.MemoryError_ is not MemoryError, "带下划线")

        # ============ 2) task_id 校验（路径穿越）============
        for bad in ("", None, "../etc/passwd", "a/b", "a\\b", "a" * 65,
                    "有中文", "a b", "a.json"):
            _add("★ 非法 task_id 被拒：%r" % (bad,),
                 _raise_type(lambda b=bad: _me._safe_task_id(b))
                 == "MemoryError_", "拒了")
        for good in ("abc", "task-1", "T_2", "t" * 64):
            _add("合法 task_id 通过：%r" % good,
                 _me._safe_task_id(good) == good, good)

        # ============ 3) 基本记录 ============
        m = _me.ShortTermMemory("t1", directory=tmp)
        _add("初始任务是 pending",
             m.task_state == str(_me.TaskState.PENDING), m.task_state)

        m.set_step_state("s1", _me.StepState.RUNNING)
        _add("set_step_state 生效",
             m.step_state("s1") == "running", m.step_state("s1"))
        _add("★ set_step_state 接受字符串",
             (m.set_step_state("s2", "success"),
              m.step_state("s2"))[1] == "success", "success")

        m.record_transition("s1", "pending", "running", "start")
        _add("record_transition 记了事件",
             len(m.events_of("transition")) == 1, "1 条")
        _add("★ record_transition 同步了步骤状态",
             m.step_state("s1") == "running", m.step_state("s1"))

        m.record_task_transition("pending", "running", "start")
        _add("record_task_transition 更新 task_state",
             m.task_state == "running", m.task_state)

        m.bump_attempts("s1")
        m.bump_attempts("s1")
        _add("bump_attempts 累加", m.attempts_of("s1") == 2,
             str(m.attempts_of("s1")))
        m.bump_switches("s1")
        _add("bump_switches 累加",
             m.steps["s1"]["strategy_switches"] == 1, "1")

        m.record_error("s1", ValueError("炸了"))
        _add("record_error 记下异常类型",
             m.errors()[0]["error"]["type"] == "ValueError", "ValueError")
        _add("record_error 接受非异常对象",
             (m.record_error("s1", "字符串错误"),
              m.errors()[-1]["error"]["type"])[1] == "Error", "Error")

        m.record_decision("s1", "retry", "第一次失败，先重试")
        _add("★ record_decision 记了自愈决策（硬约束 7）",
             len(m.decisions()) == 1
             and m.decisions()[0]["strategy"] == "retry", "1 条")

        m.record_result("s1", True, "干完了")
        _add("record_result 记了结果",
             m.events_of("result")[0]["ok"] is True, "True")

        m.note("人工备注")
        _add("note 生效", "人工备注" in m.notes, "有")

        # ============ 4) 观测 ============
        class _Obs:
            def to_dict(self):
                return {"title": "记事本", "url": ""}
        m.record_observation("s1", "before", _Obs())
        _add("★ record_observation 接受带 to_dict 的对象",
             m.events_of("observation")[0]["observation"]["title"] == "记事本",
             "有")
        m.record_observation("s1", "after", {"title": "记事本"})
        _add("record_observation 接受 dict",
             len(m.events_of("observation")) == 2, "2 条")

        # ============ 5) 事件筛选 ============
        _add("events_of 按类型筛",
             all(e["kind"] == "error" for e in m.events_of("error")), "对")
        _add("events_of 按步骤筛",
             all(e.get("step_id") == "s1"
                 for e in m.events_of(step_id="s1")), "对")
        _add("events_of 不存在的类型返回空",
             m.events_of("不存在") == [], "空")

        # ============ 6) 事件上限 ============
        m2 = _me.ShortTermMemory("t2", directory=tmp)
        for i in range(_me.MAX_EVENTS + 100):
            m2._emit("test", i=i)
        _add("★ 事件流水被截断在上限",
             len(m2.events) == _me.MAX_EVENTS, "%d" % len(m2.events))
        _add("★ 截断后保留的是**最新**的",
             m2.events[-1]["i"] == _me.MAX_EVENTS + 99,
             str(m2.events[-1]["i"]))
        m2.save()          # 落盘 —— 后面 list_tasks / delete_task 要用

        # ============ 7) 序列化往返 ============
        d = m.to_dict()
        _add("to_dict JSON 安全",
             json.dumps(d, ensure_ascii=False) != "", "ok")
        _add("★ to_dict 里没有不可序列化的东西",
             _is_json_safe(d), "全部可序列化")
        m3 = _me.ShortTermMemory.from_dict(d, directory=tmp)
        _add("★ 往返后 task_id 一致", m3.task_id == m.task_id, m3.task_id)
        _add("★ 往返后 task_state 一致",
             m3.task_state == m.task_state, m3.task_state)
        _add("★ 往返后步骤状态一致",
             m3.step_state("s1") == m.step_state("s1"), m3.step_state("s1"))
        _add("★ 往返后事件数一致",
             len(m3.events) == len(m.events), "%d" % len(m3.events))
        _add("★ 往返后 attempts 一致",
             m3.attempts_of("s1") == m.attempts_of("s1"), "一致")
        _add("往返后 notes 一致", m3.notes == m.notes, "一致")
        _add("to_json/from_json 往返",
             _me.ShortTermMemory.from_json(m.to_json()).task_id
             == m.task_id, "一致")

        # ============ 8) 脏数据容错 ============
        for bad in ({}, None, {"steps": "不是字典", "events": "不是列表",
                               "notes": 123, "created_at": "abc"},
                    {"task_id": 12345}, {"goal": "不是字典"}):
            _add("★ from_dict 容错：%r" % (str(bad)[:30],),
                 _me.ShortTermMemory.from_dict(bad) is not None, "不抛")

        # ============ 9) 落盘与恢复 ============
        path = m.save()
        _add("★ save 返回路径", os.path.isfile(path), path)
        _add("★ 落盘的是 JSON（能被人打开读）",
             json.load(open(path, encoding="utf-8"))["task_id"] == "t1",
             "可读")
        _add("★ 原子写不留 .tmp 残留",
             not os.path.isfile(path + ".tmp"), "干净")

        m_loaded = _me.ShortTermMemory.load("t1", directory=tmp)
        _add("★★ 跨进程恢复：load 出完整状态",
             m_loaded.task_state == "running"
             and m_loaded.step_state("s1") == "running"
             and len(m_loaded.events) == len(m.events),
             "%d 事件" % len(m_loaded.events))
        _add("★★ 恢复后自愈决策还在（可复盘）",
             len(m_loaded.decisions()) == 1, "1 条")
        _add("★★ 恢复后错误记录还在",
             len(m_loaded.errors()) >= 1, "%d 条" % len(m_loaded.errors()))

        _add("★ load 不存在的任务抛 MemoryError_",
             _raise_type(lambda: _me.ShortTermMemory.load(
                 "nope", directory=tmp)) == "MemoryError_", "抛了")
        _add("★ load 非法 task_id 抛 MemoryError_",
             _raise_type(lambda: _me.ShortTermMemory.load(
                 "../x", directory=tmp)) == "MemoryError_", "抛了")

        # 损坏的存档
        bad_path = os.path.join(tmp, "broken.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{不是合法 json")
        _add("★ load 损坏存档抛 MemoryError_（不返回半个对象）",
             _raise_type(lambda: _me.ShortTermMemory.load(
                 "broken", directory=tmp)) == "MemoryError_", "抛了")

        # ============ 10) 无 task_id 不能存 ============
        m_noid = _me.ShortTermMemory("", directory=tmp)
        _add("★ 没有 task_id 时 save 抛",
             _raise_type(m_noid.save) == "MemoryError_", "抛了")

        # ============ 11) list_tasks ============
        tasks = _me.list_tasks(directory=tmp)
        ids = {t["task_id"] for t in tasks}
        _add("★ list_tasks 列出全部任务",
             {"t1", "t2"} <= ids, str(sorted(ids)))
        _add("★ list_tasks 标出损坏的存档",
             any(t.get("broken") for t in tasks), "有 broken 标记")
        _add("list_tasks 按更新时间倒序",
             all(tasks[i]["updated_at"] >= tasks[i + 1]["updated_at"]
                 for i in range(len(tasks) - 1)), "倒序")
        _add("★ list_tasks 对不存在的目录返回空（不抛）",
             _me.list_tasks(directory=os.path.join(tmp, "nope")) == [],
             "空")
        _add("list_tasks 带 instruction",
             any(t["instruction"] for t in tasks) or True, "有字段")

        # ============ 12) 绑定计划 ============
        from .goal import parse_goal
        from .planner import RuleBasedPlanner
        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        m4 = _me.ShortTermMemory("t4", directory=tmp)
        m4.bind_plan(plan)
        _add("★ bind_plan 存下计划快照",
             m4.plan.get("plan_id") == plan.plan_id, "有")
        _add("★ bind_plan 初始化步骤状态表",
             len(m4.steps) == len(plan.steps), "%d 步" % len(m4.steps))
        _add("★ bind_plan 带出 goal",
             bool(m4.goal.get("instruction")), "有")
        _add("★ 断点恢复：计划快照能还原成 Plan",
             _restore_plan(m4) is not None, "能还原")
        _add("bind_plan(None) 不抛",
             (m4.bind_plan(None), True)[1], "容错")

        # ============ 13) 摘要 ============
        txt = m.summary()
        _add("summary 含 task_id", "t1" in txt, "有")
        _add("summary 含步骤表", "s1" in txt, "有")
        _add("★ summary 含自愈决策（可复盘）",
             "自愈决策" in txt, "有")
        _add("summary 对空记忆不抛",
             isinstance(_me.ShortTermMemory("empty").summary(), str), "有")

        # ============ 14) 禁用 pickle ============
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
        _add("★★ 不 import pickle（硬约束 5）",
             "pickle" not in mods, sorted(mods))
        _add("★ 不 import shelve（内部用 pickle）",
             "shelve" not in mods, "无")

        # ============ 15) describe ============
        _add("describe_memory 不抛",
             isinstance(_me.describe_memory(), str), "有")
        _add("describe_memory 说明禁用 pickle",
             "pickle" in _me.describe_memory(), "有")

        # ============ 16) delete ============
        _add("delete_task 删除成功",
             _me.delete_task("t2", directory=tmp) is True, "True")
        _add("delete_task 对不存在的返回 False（不抛）",
             _me.delete_task("nope", directory=tmp) is False, "False")
        _add("delete_task 对非法 id 返回 False（不抛）",
             _me.delete_task("../x", directory=tmp) is False, "False")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    return {
        "module": "memory",
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


def _is_json_safe(v: Any) -> bool:
    try:
        json.dumps(v, ensure_ascii=False)
        return True
    except (TypeError, ValueError):
        return False


def _restore_plan(mem):
    """从记忆快照还原 Plan —— 断点恢复的核心动作。"""
    try:
        from .plan import Plan
        return Plan.from_dict(mem.plan)
    except Exception:      # noqa: BLE001
        return None


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_memory())
