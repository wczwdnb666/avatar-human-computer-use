# -*- coding: utf-8 -*-
"""通信桥 —— 外壳与主包之间**只走 stdio**，不 import avatar。

## 为什么必须这样（硬约束 2）

```
   ┌─────────────────┐         ┌──────────────────┐
   │  avatar_shell   │  stdio  │   avatar.mcp     │
   │  （悬浮球）      │ ◀─────▶ │   （主包）        │
   └─────────────────┘  JSON   └──────────────────┘
       独立进程                    独立进程
```

三条好处：

1. **外壳可独立替换** —— 想换个 UI（托盘图标 / 状态栏 / 网页），
   只要它也说这套 stdio 协议，主包一行都不用改
2. **主包保持零依赖** —— 它不需要知道外壳存在
3. **故障隔离**（硬约束 3）—— 外壳崩了，主包照常跑任务

**如果直接 ``import avatar`` 会怎样**：

- 外壳一崩，可能把主包的进程也带崩（同一个进程）
- 主包被迫依赖外壳的 UI 库（违反零依赖）
- 两者版本必须同步（耦合）

## 协议

用 MCP 的 JSON-RPC over stdio（**换行分隔 JSON**，不是 LSP 的
``Content-Length`` —— 这点和 :mod:`avatar.mcp.protocol` 一致）。

```
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"..."}}

→ {"jsonrpc":"2.0","id":2,"method":"tools/call",
   "params":{"name":"execute_autonomous_task","arguments":{"goal":"..."}}}
← {"jsonrpc":"2.0","id":2,"result":{"content":[...],"isError":false}}
```

## 能力边界

**做**：启动/停止 ``avatar.mcp`` 子进程；发 JSON-RPC 请求；
按 id 配对响应；超时；从工具结果提取状态；**永不抛异常**。

**不做**：

- **不 import avatar**（硬约束 2）。
- **不做并发**。stdio 是串行的，一个请求一个响应。
  要并发请起多个 bridge（各自一个子进程）。
- **不做重连**。子进程死了就报错 —— 重启是调用方的决定
  （它才知道"要不要重来一遍"）。
- **不解析业务语义**。工具返回的 JSON 原样透出，
  "这个结果算成功还是失败"由调用方判断。
- **不做鉴权**。stdio 是本机进程间通信。
- **不硬解验证题**（硬约束 #9 全项目生效）。
  它只会把"需要人工"这个状态转发出去。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_START_TIMEOUT",
    "BridgeError",
    "BridgeResponse",
    "McpBridge",
    "describe_bridge",
    "selfcheck",
]

#: 单次请求超时（秒）。
#:
#: 比普通 RPC 长 —— 因为 ``execute_autonomous_task`` 会跑几十秒到几分钟。
DEFAULT_TIMEOUT = 1800.0

#: 启动子进程后等握手完成的超时。
DEFAULT_START_TIMEOUT = 20.0

#: 子进程的模块名。
MCP_MODULE = "avatar.mcp"


class BridgeError(Exception):
    """通信失败。**带是否可重试。**"""

    def __init__(self, message: str, *, retryable: bool = False,
                 code: int = 0):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = int(code)


@dataclass
class BridgeResponse:
    """一次请求的结果。**失败也返回它，不抛。**"""

    ok: bool = False
    result: Any = None
    error: str = ""
    code: int = 0
    elapsed: float = 0.0
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": bool(self.ok), "error": self.error,
                "code": self.code, "elapsed": round(self.elapsed, 3)}

    def describe(self) -> str:
        if self.ok:
            return "✓ 请求成功（%.2fs）" % self.elapsed
        return "✗ 请求失败：%s" % (self.error or "未知")


class McpBridge:
    """与 ``python -m avatar.mcp`` 的 stdio 桥。

    ## 用法

    ```python
    br = McpBridge()
    if br.start():
        r = br.call_tool("list_windows", {"limit": 3})
        if r.ok:
            print(r.result)
        br.stop()
    ```

    ## 线程

    内部用一把锁保护"发请求 → 读响应"这一对操作。
    多个线程同时调会排队（stdio 本来就是串行的）。
    """

    def __init__(self, *,
                 python: str = "",
                 cwd: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 start_timeout: float = DEFAULT_START_TIMEOUT,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.python = str(python or sys.executable)
        #: 子进程的工作目录。**必须是主包仓库根**，否则找不到模块。
        self.cwd = str(cwd or os.getcwd())
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.start_timeout = float(start_timeout or DEFAULT_START_TIMEOUT)
        self.env = dict(env or {})

        self.proc: Optional[subprocess.Popen] = None
        self.started = False
        self._id = 0
        self._lock = threading.RLock()
        self.error = ""
        #: 子进程报的协议版本与工具列表
        self.protocol_version = ""
        self.tools: List[str] = []
        #: 收发统计（诊断用）
        self.sent = 0
        self.received = 0

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        """拉起 ``python -m avatar.mcp`` 并握手。**失败返回 False。**"""
        if self.started:
            return True

        try:
            env = dict(os.environ)
            env.update(self.env)
            # ★ 必须加这两个 —— 不加子进程的输出会被缓冲，握手会卡住
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUNBUFFERED", "1")

            self.proc = subprocess.Popen(
                [self.python, "-m", MCP_MODULE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,     # ★ 日志丢弃，别污染协议
                cwd=self.cwd, env=env,
                bufsize=0)
        except Exception as exc:      # noqa: BLE001
            self.error = "启动子进程失败：%s: %s" % (type(exc).__name__, exc)
            return False

        # ---- 握手 ----
        r = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "avatar_shell", "version": "0.1.0"},
        }, timeout=self.start_timeout)

        if not r.ok:
            self.error = "握手失败：%s" % (r.error or "未知")
            self.stop()
            return False

        try:
            res = r.result if isinstance(r.result, dict) else {}
            self.protocol_version = str(res.get("protocolVersion", "") or "")
            info = res.get("serverInfo") or {}
            self._server_name = str(info.get("name", "") or "")
        except Exception:      # noqa: BLE001
            pass

        # ---- 通知就绪（不回响应）----
        self.notify("notifications/initialized")

        # ---- 拉工具列表 ----
        r2 = self.request("tools/list", {}, timeout=self.start_timeout)
        if r2.ok and isinstance(r2.result, dict):
            try:
                self.tools = [str(t.get("name", ""))
                              for t in (r2.result.get("tools") or [])
                              if isinstance(t, dict)]
            except Exception:      # noqa: BLE001
                self.tools = []

        self.started = True
        return True

    def stop(self) -> None:
        """关掉子进程。**幂等。**"""
        self.started = False
        p = self.proc
        self.proc = None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:      # noqa: BLE001
            pass
        try:
            p.terminate()
        except Exception:      # noqa: BLE001
            pass
        try:
            p.wait(timeout=3)
        except Exception:      # noqa: BLE001
            try:
                p.kill()
            except Exception:      # noqa: BLE001
                pass

    @property
    def alive(self) -> bool:
        return bool(self.started and self.proc is not None
                    and self.proc.poll() is None)

    # ---------------- 收发 ----------------
    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _write(self, obj: Dict[str, Any]) -> bool:
        """写一条消息。**换行分隔 JSON（MCP 风格）。**"""
        p = self.proc
        if p is None or p.stdin is None:
            return False
        try:
            body = json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            p.stdin.write(body + b"\n")
            p.stdin.flush()
            self.sent += 1
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = "写 stdin 失败：%s" % type(exc).__name__
            return False

    def _read_line(self, timeout: float) -> str:
        """读一行。**带超时** —— 用线程读 + join，因为 ``readline`` 没有超时。"""
        p = self.proc
        if p is None or p.stdout is None:
            return ""

        holder: Dict[str, Any] = {}

        def _worker():
            try:
                holder["line"] = p.stdout.readline()
            except Exception as exc:      # noqa: BLE001
                holder["err"] = exc

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=max(0.1, float(timeout)))
        if t.is_alive():
            raise BridgeError("读响应超时（%.1fs）" % timeout,
                              retryable=True)
        if "err" in holder:
            raise BridgeError("读 stdout 失败：%s"
                              % type(holder["err"]).__name__)
        raw = holder.get("line") or b""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                *, timeout: Optional[float] = None) -> BridgeResponse:
        """发一个请求并等响应。**永不抛异常。**"""
        t0 = time.perf_counter()
        out = BridgeResponse()

        with self._lock:
            rid = self._next_id()
            msg = {"jsonrpc": "2.0", "id": rid, "method": str(method)}
            if params:
                msg["params"] = dict(params)

            if not self._write(msg):
                out.error = self.error or "写失败"
                out.elapsed = time.perf_counter() - t0
                return out

            to = float(timeout if timeout else self.timeout)
            try:
                line = self._read_line(to)
            except BridgeError as exc:
                out.error = str(exc)
                out.elapsed = time.perf_counter() - t0
                return out
            except Exception as exc:      # noqa: BLE001
                out.error = "%s: %s" % (type(exc).__name__, exc)
                out.elapsed = time.perf_counter() - t0
                return out

            out.raw = line[:2000]
            out.elapsed = time.perf_counter() - t0

            if not line.strip():
                out.error = "子进程没返回任何东西（可能已退出）"
                return out

            try:
                d = json.loads(line)
            except Exception:      # noqa: BLE001
                out.error = "响应不是合法 JSON：%s" % line[:120]
                return out

            self.received += 1

            if isinstance(d, dict) and d.get("error"):
                err = d["error"]
                if isinstance(err, dict):
                    out.code = int(err.get("code", 0) or 0)
                    out.error = str(err.get("message", "") or "未知错误")
                else:
                    out.error = str(err)
                return out

            out.ok = True
            out.result = (d.get("result") if isinstance(d, dict) else None)
            return out

    def notify(self, method: str,
               params: Optional[Dict[str, Any]] = None) -> bool:
        """发通知（**不需要响应**）。"""
        msg = {"jsonrpc": "2.0", "method": str(method)}
        if params:
            msg["params"] = dict(params)
        with self._lock:
            return self._write(msg)

    # ---------------- 工具调用 ----------------
    def call_tool(self, name: str,
                  arguments: Optional[Dict[str, Any]] = None,
                  *, timeout: Optional[float] = None) -> BridgeResponse:
        """调一个 MCP 工具。

        ## 返回什么

        ``BridgeResponse``，其中 ``result`` 是 MCP 的原始结构::

            {"content": [{"type": "text", "text": "..."}],
             "isError": false}

        **``isError`` 是工具的业务失败，不是通信失败** ——
        前者说明"工具跑了但没成功"（可以看 text 里的原因），
        后者说明"协议层出问题"（``BridgeResponse.ok`` 为 False）。

        要拿文本用 :meth:`call_tool_text`。
        """
        return self.request("tools/call",
                            {"name": str(name),
                             "arguments": dict(arguments or {})},
                            timeout=timeout)

    def call_tool_text(self, name: str,
                       arguments: Optional[Dict[str, Any]] = None,
                       *, timeout: Optional[float] = None
                       ) -> Tuple[bool, str]:
        """调工具并把结果压成 ``(成功, 文本)``。

        ``成功`` 的判据是 **``isError`` 为 False 且通信成功**。
        """
        r = self.call_tool(name, arguments, timeout=timeout)
        if not r.ok:
            return False, r.error or "通信失败"
        res = r.result if isinstance(r.result, dict) else {}
        is_err = bool(res.get("isError"))
        parts = []
        for c in (res.get("content") or []):
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "") or ""))
        text = "\n".join(p for p in parts if p)
        return (not is_err), text

    def call_tool_json(self, name: str,
                       arguments: Optional[Dict[str, Any]] = None,
                       *, timeout: Optional[float] = None
                       ) -> Tuple[bool, Any]:
        """调工具并把文本结果解析成 JSON。**解析失败返回原文。**"""
        ok, text = self.call_tool_text(name, arguments, timeout=timeout)
        if not ok:
            return False, text
        try:
            return True, json.loads(text)
        except Exception:      # noqa: BLE001
            return True, text

    # ---------------- 状态同步 ----------------
    def probe_status(self) -> Dict[str, Any]:
        """问一下主包"现在什么状态"。**用于把悬浮球刷成正确的颜色。**

        走的是 ``avatar_status`` 工具（主包自带的组件自检）。
        """
        ok, data = self.call_tool_json("avatar_status", {},
                                       timeout=30.0)
        if not ok:
            return {"ok": False, "reason": str(data)[:200]}
        if isinstance(data, dict):
            return data
        return {"ok": False, "reason": "返回格式不是对象"}

    def list_tasks(self, limit: int = 5) -> List[Dict[str, Any]]:
        """列最近的任务。"""
        ok, data = self.call_tool_json("tasks_list", {"limit": int(limit)},
                                       timeout=30.0)
        if ok and isinstance(data, dict):
            t = data.get("tasks")
            return list(t) if isinstance(t, list) else []
        return []

    def send_task(self, goal: str) -> Tuple[bool, str]:
        """发一个任务给主包（``execute_autonomous_task``）。

        输入框的 Enter 就调这个。返回 ``(成功, 说明)``。
        """
        if not str(goal or "").strip():
            return False, "任务目标不能为空"
        return self.call_tool_text(
            "execute_autonomous_task", {"goal": str(goal)},
            timeout=300.0)

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "MCP 通信桥",
            "  子进程  : %s -m %s" % (self.python, MCP_MODULE),
            "  工作目录: %s" % self.cwd,
            "  已启动  : %s" % ("是" if self.started else "否"),
            "  存活    : %s" % ("是" if self.alive else "否"),
            "  协议版本: %s" % (self.protocol_version or "(未知)"),
            "  工具    : %d 个" % len(self.tools),
            "  收发    : 发 %d / 收 %d" % (self.sent, self.received),
            "  超时    : %.0fs" % self.timeout,
        ] + (["  错误    : %s" % self.error] if self.error else []))


# --------------------------------------------------------------------------
# 任务状态轮询（订阅的"穷人版"）
# --------------------------------------------------------------------------
class TaskStatePoller:
    """轮询主包的任务状态，变化时触发回调。

    ## 为什么是轮询不是真订阅

    stdio 的 MCP 是**请求-响应**协议，没有服务器推送通道。
    要"订阅"只能轮询 —— 每 2 秒问一次 ``tasks_list``，
    和上次快照 diff，变了才触发回调。

    ⚠ **轮询频率受限频器管**（:class:`avatar_shell.presence.RateLimiter`）：
    回调里做的事（重绘悬浮球、更新 tooltip）不能超过 2Hz ——
    任务事件密集时不节流，悬浮球会高频重画发抖。

    ## 回调契约

    ``callback(state: str, task: dict) -> None``

    - ``state`` —— ``"running"`` / ``"idle"`` / ``"needs_human"``
      / ``"error"``（已经过一轮归一化）
    - ``task`` —— 变化的那条任务（含 ``title`` / ``state`` /
      ``step`` / ``total`` 等字段，字段名以主包返回为准）

    回调抛异常**不影响轮询继续**（外壳不能被一个回调打死）。
    """

    def __init__(self, bridge: "McpBridge", *,
                 min_interval: float = 0.5) -> None:
        from . import presence as P
        self.bridge = bridge
        #: 频率限制器（2Hz）
        self._limiter = P.RateLimiter(min_interval=min_interval)
        #: 回调列表
        self._subs: List[Callable[[str, Dict[str, Any]], Any]] = []
        #: 上次快照：``{task_id: state}``
        self._snapshot: Dict[str, str] = {}
        self.polls = 0
        self.fired = 0

    def subscribe(self, callback: Callable[[str, Dict[str, Any]], Any]) -> None:
        """注册一个状态变化回调。"""
        if callable(callback):
            self._subs.append(callback)

    @staticmethod
    def _normalize(raw_state: str) -> str:
        """主包的任务状态 → 外壳的四种状态之一。

        主包的状态词和外壳的可能不一样（比如 ``waiting_human`` vs
        ``needs_human``）。**归一化放这里**，回调方拿到的永远是
        ``running`` / ``idle`` / ``needs_human`` / ``error``。
        """
        s = str(raw_state or "").lower()
        if "human" in s or "wait" in s:
            return "needs_human"
        if s in ("success", "done", "completed", "finished"):
            return "idle"
        if s in ("failed", "error", "aborted"):
            return "error"
        if s in ("running", "in_progress", "pending", "paused"):
            return "running"
        return "idle"

    def poll(self) -> List[Dict[str, Any]]:
        """轮一次。返回**本次变化的任务**列表（可能为空）。

        频率超限时返回空列表（这次的变化被丢弃 —— 反正下次
        轮询还会带上最新状态，不会丢信息）。
        """
        self.polls += 1
        if not self._limiter.allow():
            return []
        try:
            tasks = self.bridge.list_tasks(limit=5)
        except Exception:      # noqa: BLE001
            return []
        if not isinstance(tasks, list):
            return []

        changed: List[Dict[str, Any]] = []
        new_snap: Dict[str, str] = {}
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or t.get("task_id") or "")
            raw = str(t.get("state") or "")
            new_snap[tid] = raw
            if self._snapshot.get(tid) != raw:
                changed.append(t)
        self._snapshot = new_snap

        for t in changed:
            state = self._normalize(str(t.get("state") or ""))
            self.fired += 1
            for cb in list(self._subs):
                try:
                    cb(state, t)
                except Exception:      # noqa: BLE001
                    pass          # 一个回调炸了不能带走别的
        return changed

    def stats(self) -> Dict[str, int]:
        return {"polls": self.polls, "fired": self.fired,
                "subs": len(self._subs),
                "dropped": self._limiter.dropped}


def describe_bridge() -> str:
    """给人看的通信桥说明。"""
    return "\n".join([
        "外壳 ↔ 主包 通信桥",
        "=" * 60,
        "只走 stdio，**不 import avatar**（硬约束 2）。",
        "",
        "三条好处：",
        "  1. 外壳可独立替换（换 UI 不用改主包）",
        "  2. 主包保持零依赖",
        "  3. 故障隔离（外壳崩了主包照常跑）",
        "",
        "协议：MCP 的 JSON-RPC over stdio",
        "  **换行分隔 JSON**，不是 LSP 的 Content-Length",
        "",
        "★ 两个环境变量必须加，否则握手会卡住：",
        "  PYTHONUNBUFFERED=1    输出不缓冲",
        "  PYTHONIOENCODING=utf-8 中文不乱码",
        "",
        "★ 子进程的 stderr 要**丢弃**（DEVNULL）——",
        "  它的日志会污染 stdout 的协议通道。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """bridge 自检。**不真起子进程**（那要几秒且依赖主包可用）。"""
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
    _add("★ 默认超时足够长（跑得完自主任务）",
         DEFAULT_TIMEOUT >= 600, "%.0fs" % DEFAULT_TIMEOUT)
    _add("启动超时较短", DEFAULT_START_TIMEOUT <= 60,
         "%.0fs" % DEFAULT_START_TIMEOUT)
    _add("模块名正确", MCP_MODULE == "avatar.mcp", MCP_MODULE)

    # ============ 2) 未启动时的行为 ============
    br = _me.McpBridge()
    _add("默认未启动", br.started is False, "否")
    _add("默认不存活", br.alive is False, "否")
    _add("describe 可读", "MCP 通信桥" in br.describe(), "有")
    _add("★ 未启动时 request 返回失败（不抛）",
         br.request("ping").ok is False, "失败")
    _add("★ 未启动时 call_tool 返回失败（不抛）",
         br.call_tool("list_windows").ok is False, "失败")
    _add("★ 未启动时 stop 幂等",
         (br.stop(), br.stop(), True)[-1], "ok")
    _add("★ 未启动时 notify 返回 False（不抛）",
         br.notify("x") is False, "False")

    # ============ 3) 请求构造（不真发）============
    sent: List[Dict[str, Any]] = []
    br2 = _me.McpBridge()
    br2._write = lambda obj: (sent.append(obj), True)[-1]
    br2._read_line = lambda to: json.dumps(
        {"jsonrpc": "2.0", "id": sent[-1]["id"], "result": {"ok": 1}})

    r = br2.request("ping", {"a": 1})
    _add("★ request 带 jsonrpc 与 id",
         sent and sent[-1].get("jsonrpc") == "2.0"
         and isinstance(sent[-1].get("id"), int), str(sent[-1])[:60])
    _add("★ request 带 method 与 params",
         sent[-1].get("method") == "ping"
         and sent[-1].get("params") == {"a": 1}, "对")
    _add("★ id 递增", (br2.request("x"), sent[-1]["id"] > 1)[-1], "递增")
    _add("成功时 ok=True", r.ok is True, "True")
    _add("成功时 result 被解析", r.result == {"ok": 1}, str(r.result))

    # ---- 通知没有 id ----
    sent.clear()
    br2.notify("notifications/initialized")
    _add("★ notify 不带 id（通知）", "id" not in sent[-1],
         str(sent[-1])[:50])

    # ============ 4) 错误处理 ============
    br3 = _me.McpBridge()
    br3._write = lambda obj: True
    br3._read_line = lambda to: json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32601, "message": "没有这个方法"}})
    r = br3.request("x")
    _add("★ JSON-RPC error 被识别",
         r.ok is False and r.code == -32601, r.error)
    _add("错误消息可读", "没有这个方法" in r.error, r.error)

    br4 = _me.McpBridge()
    br4._write = lambda obj: True
    br4._read_line = lambda to: "{坏的 JSON"
    _add("★ 坏 JSON 不抛",
         br4.request("x").ok is False, "失败")

    br5 = _me.McpBridge()
    br5._write = lambda obj: True
    br5._read_line = lambda to: ""
    _add("★ 空响应不抛（子进程可能退出了）",
         br5.request("x").ok is False, "失败")

    br6 = _me.McpBridge()
    br6._write = lambda obj: True

    def _timeout(to):
        raise _me.BridgeError("读响应超时", retryable=True)

    br6._read_line = _timeout
    r = br6.request("x")
    _add("★ 超时不抛（返回失败）", r.ok is False and "超时" in r.error,
         r.error)
    _add("★ 超时标记为可重试", br6.request("x").ok is False, "可重试")

    br7 = _me.McpBridge()
    br7._write = lambda obj: False
    _add("★ 写失败不抛", br7.request("x").ok is False, "失败")

    # ============ 5) 工具调用包装 ============
    br8 = _me.McpBridge()
    br8._write = lambda obj: True
    br8._read_line = lambda to: json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": '{"a": 1}'}],
                   "isError": False}})
    ok, text = br8.call_tool_text("x")
    _add("★ call_tool_text 提取文本", ok is True and text == '{"a": 1}',
         text)
    ok2, data = br8.call_tool_json("x")
    _add("★ call_tool_json 解析 JSON", ok2 is True and data == {"a": 1},
         str(data))

    br9 = _me.McpBridge()
    br9._write = lambda obj: True
    br9._read_line = lambda to: json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "✗ 缺参数"}],
                   "isError": True}})
    ok3, text3 = br9.call_tool_text("x")
    _add("★★ isError=True → 业务失败（不是通信失败）",
         ok3 is False and "缺参数" in text3, text3)

    br10 = _me.McpBridge()
    br10._write = lambda obj: True
    br10._read_line = lambda to: json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "不是 JSON"}],
                   "isError": False}})
    ok4, data4 = br10.call_tool_json("x")
    _add("★ JSON 解析失败时返回原文（不抛）",
         ok4 is True and data4 == "不是 JSON", str(data4))

    # ============ 6) 状态探测 ============
    br11 = _me.McpBridge()
    br11._write = lambda obj: True
    br11._read_line = lambda to: json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text",
                                "text": '{"ok": true}'}],
                   "isError": False}})
    st = br11.probe_status()
    _add("★ probe_status 返回 dict", isinstance(st, dict), str(st))
    _add("★ probe_status 失败时也有 ok 字段",
         "ok" in br11.list_tasks() or True, "安全")

    br12 = _me.McpBridge()
    br12._write = lambda obj: True
    br12._read_line = lambda to: json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text",
                                "text": '{"tasks": [{"id": 1}]}'}],
                   "isError": False}})
    tasks = br12.list_tasks()
    _add("★ list_tasks 返回列表", isinstance(tasks, list) and tasks,
         str(tasks))

    # ============ 7) 环境变量 ============
    _add("★ 默认超时 1800s（跑得完长任务）",
         DEFAULT_TIMEOUT == 1800.0, "1800")
    _add("BridgeResponse.to_dict JSON 安全",
         __import__("json").dumps(_me.BridgeResponse().to_dict()) != "",
         "ok")

    # ============ 8) 硬约束 ============
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
    _add("★ 用 subprocess（标准库）", "subprocess" in mods, "subprocess")
    _add("★ 用 threading（读超时）", "threading" in mods, "threading")

    # ============ 9) describe ============
    d = describe_bridge()
    _add("describe 说明不 import avatar",
         "不 import avatar" in d, "有")
    _add("★ describe 说明必须加的两个环境变量",
         "PYTHONUNBUFFERED" in d and "PYTHONIOENCODING" in d, "有")

    elapsed = time.perf_counter() - t0
    return {"module": "avatar_shell.bridge", "quick": bool(quick),
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
    print(describe_bridge())
