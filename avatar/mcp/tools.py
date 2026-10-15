# -*- coding: utf-8 -*-
"""MCP 工具集 —— 把 avatar 的能力包装成 MCP Tools。

## 这一层解决什么

MCP 客户端（Claude Desktop / Cursor）看到的是"工具列表"。
本模块负责把 avatar 的能力**翻译**成那个列表 —— 包括
每个工具的 JSON Schema（**硬约束 9：必须完整**）。

## 工具的粒度怎么定

**粗一点，不要细。**

反面例子：把"移动鼠标""按下左键""松开左键"拆成三个工具 ——
那是在让模型**手写底层操作**，它一定会写错顺序。

正面例子：``mouse_click(x, y)`` —— 一次调用一个完整的动作。

判断标准：**"这个工具名能不能直接回答用户想干什么"**。
能，就是合适的粒度。

## 能力边界

**做**：注册工具；校验参数；调用 avatar 的能力；把结果压成
MCP 的 ``content`` 格式；把异常翻成 ``isError`` 响应。

**不做**：

- **不做鉴权**。stdio 是本机进程间通信，调用方已经是你自己了。
- **不做限流**。同一个客户端疯狂调用是本机的事，不是协议的事。
- **不做异步**。MCP 的 ``tools/call`` 是同步请求-响应；
  长任务（比如 ``execute_autonomous_task``）会**阻塞**到完成。
  要异步请用 MCP 的 progress 通知（本模块未实现）。
- **不硬解验证题**（硬约束 #9）。``detect`` 检出验证题会
  返回"需要人"，**不会**去解题。
- **不绕过风控**。``execute_autonomous_task`` 走的是阶段六执行器，
  风控闸照样生效。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolError",
    "build_default_tools",
    "describe_tools",
    "selfcheck",
]


class ToolError(Exception):
    """工具调用失败。**会被翻成 MCP 的 isError 响应。**"""

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.hint = str(hint or "")


def _schema(props: Dict[str, Any], required: Optional[List[str]] = None,
            *, desc: str = "") -> Dict[str, Any]:
    """构造一个**完整**的 JSON Schema（硬约束 9）。

    ``type`` / ``properties`` / ``required`` 三样必须都在 ——
    缺 ``required`` 时给空数组而不是省略：有些客户端会因此报错。
    """
    return {
        "type": "object",
        "properties": dict(props or {}),
        "required": list(required or []),
        **({"description": desc} if desc else {}),
    }


def _p(type_: str, desc: str = "", **extra: Any) -> Dict[str, Any]:
    """一个属性的简写。"""
    out: Dict[str, Any] = {"type": type_}
    if desc:
        out["description"] = desc
    out.update(extra)
    return out


@dataclass
class Tool:
    """一个 MCP 工具。"""

    name: str = ""
    description: str = ""
    #: JSON Schema（**必须完整**）
    input_schema: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable[..., Any]] = None
    #: 是否需要更长的超时（客户端可据此提示）
    long_running: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema or _schema({})}

    def call(self, args: Optional[Dict[str, Any]] = None) -> Any:
        """调这个工具。**参数缺失抛 :class:`ToolError`。**"""
        if self.handler is None:
            raise ToolError("工具 %s 没有实现" % self.name)
        # ⚠ 类型检查要在 dict() **之前** —— 否则传个字符串会先抛
        #   ValueError（dict("x") 失败），错误类型就不对了
        if args is None:
            params: Dict[str, Any] = {}
        elif isinstance(args, dict):
            params = dict(args)
        else:
            raise ToolError("参数必须是对象（收到 %s）"
                            % type(args).__name__)
        # 必填项检查（**在调用前**，让错误信息更清楚）
        missing = [k for k in (self.input_schema.get("required") or [])
                   if k not in params or params[k] is None]
        if missing:
            raise ToolError(
                "缺少必填参数：%s" % "、".join(missing),
                hint="看 tools/list 里的 inputSchema")
        try:
            return self.handler(**params)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError("参数不匹配：%s" % exc) from exc


class ToolRegistry:
    """工具注册表。**名字唯一。**"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("工具必须有 name")
        if tool.name in self._tools:
            raise ValueError("工具名重复：%s" % tool.name)
        self._tools[tool.name] = tool
        return tool

    def add(self, name: str, description: str,
            input_schema: Dict[str, Any],
            handler: Callable[..., Any], *,
            long_running: bool = False) -> Tool:
        return self.register(Tool(name=name, description=description,
                                  input_schema=input_schema,
                                  handler=handler,
                                  long_running=long_running))

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(str(name or ""))

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[Tool]:
        return [self._tools[n] for n in self.names()]

    def list_schemas(self) -> List[Dict[str, Any]]:
        """``tools/list`` 的返回体。"""
        return [t.to_dict() for t in self.all()]

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        t = self.get(name)
        if t is None:
            raise ToolError("没有这个工具：%s" % name,
                            hint="可用：%s" % "、".join(self.names()))
        return t.call(args)

    def __len__(self) -> int:
        return len(self._tools)


# ==========================================================================
# 默认工具集 —— 把 avatar 的能力接出来
# ==========================================================================
def build_default_tools(registry: Optional[ToolRegistry] = None
                        ) -> ToolRegistry:
    """注册全部默认工具。**每个 handler 都自己吞异常并翻成 ToolError。**"""
    reg = registry or ToolRegistry()

    # ---------------- 1) 窗口与应用 ----------------
    def _launch_app(name: str, wait: float = 3.0) -> Any:
        try:
            from ..app_manager import launcher as APP
        except Exception as exc:      # noqa: BLE001
            raise ToolError("应用启动模块不可用：%s" % exc) from exc
        try:
            info = APP.launch(str(name))
        except Exception as exc:      # noqa: BLE001
            raise ToolError("启动 %s 失败：%s" % (name, exc)) from exc
        return {"ok": True, "hwnd": int(getattr(info, "hwnd", 0) or 0),
                "name": str(name)}

    reg.add(
        "launch_app",
        "启动一个桌面应用并返回它的窗口句柄。"
        "支持的别名：notepad / calc / mspaint / explorer / cmd / msedge。",
        _schema({
            "name": _p("string", "应用名或别名，如 notepad"),
            "wait": _p("number", "启动后等待秒数（默认 3）"),
        }, ["name"]),
        _launch_app)

    def _list_windows(title_contains: str = "", limit: int = 20) -> Any:
        try:
            from ..app_manager import windows as W
        except Exception as exc:      # noqa: BLE001
            raise ToolError("窗口模块不可用：%s" % exc) from exc
        try:
            wins = W.list_windows()
        except Exception as exc:      # noqa: BLE001
            raise ToolError("枚举窗口失败：%s" % exc) from exc
        out = []
        for w in wins:
            title = str(getattr(w, "title", "") or "")
            if title_contains and str(title_contains) not in title:
                continue
            out.append({"hwnd": int(getattr(w, "hwnd", 0) or 0),
                        "title": title[:120],
                        "process": str(getattr(w, "process", "") or "")})
            if len(out) >= int(limit or 20):
                break
        return {"ok": True, "windows": out, "count": len(out)}

    reg.add(
        "list_windows",
        "列出当前打开的窗口（可按标题筛选）。用来找 hwnd。",
        _schema({
            "title_contains": _p("string", "标题包含这个子串才返回"),
            "limit": _p("integer", "最多返回几个（默认 20）"),
        }),
        _list_windows)

    # ---------------- 2) 输入 ----------------
    def _mouse_click(x: int, y: int, button: str = "left",
                     clicks: int = 1, hwnd: int = 0) -> Any:
        try:
            from ..input import dispatcher as D
        except Exception as exc:      # noqa: BLE001
            raise ToolError("输入层不可用：%s" % exc) from exc
        try:
            if hwnd:
                res = D.dispatch_click(int(hwnd), int(x), int(y),
                                       button=str(button),
                                       clicks=int(clicks))
            else:
                from ..input import mouse as M
                M.click(int(x), int(y), button=str(button),
                        clicks=int(clicks))
                return {"ok": True, "detail": "已点击 (%d,%d)" % (x, y)}
        except Exception as exc:      # noqa: BLE001
            raise ToolError("点击失败：%s" % exc) from exc
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": str(getattr(res, "detail", "") or "")}

    reg.add(
        "mouse_click",
        "在屏幕坐标 (x, y) 点击。给了 hwnd 就按窗口客户区坐标算。",
        _schema({
            "x": _p("integer", "X 坐标"),
            "y": _p("integer", "Y 坐标"),
            "button": _p("string", "left / right / middle",
                         enum=["left", "right", "middle"]),
            "clicks": _p("integer", "点击次数（1=单击，2=双击）"),
            "hwnd": _p("integer", "目标窗口句柄（0=屏幕坐标）"),
        }, ["x", "y"]),
        _mouse_click)

    def _type_text(text: str, hwnd: int = 0, human: bool = True) -> Any:
        try:
            from ..input import dispatcher as D
        except Exception as exc:      # noqa: BLE001
            raise ToolError("输入层不可用：%s" % exc) from exc
        if not hwnd:
            raise ToolError("必须给 hwnd —— 没有窗口就不知道往哪儿打字")
        try:
            res = D.dispatch_text(int(hwnd), str(text), human=bool(human))
        except Exception as exc:      # noqa: BLE001
            raise ToolError("输入失败：%s" % exc) from exc
        return {"ok": bool(getattr(res, "ok", False)),
                "detail": str(getattr(res, "detail", "") or "")}

    reg.add(
        "type_text",
        "往指定窗口输入文本（默认走真人化打字节奏）。",
        _schema({
            "text": _p("string", "要输入的文本"),
            "hwnd": _p("integer", "目标窗口句柄"),
            "human": _p("boolean", "是否用真人化节奏（默认 true）"),
        }, ["text", "hwnd"]),
        _type_text)

    # ---------------- 3) 定位 ----------------
    def _uia_find(name: str, hwnd: int = 0, limit: int = 5) -> Any:
        try:
            from ..uia import find as F
            from ..uia.find import Query
        except Exception as exc:      # noqa: BLE001
            raise ToolError("UIA 不可用：%s" % exc) from exc
        q = Query(name=str(name), window=int(hwnd)) if hwnd \
            else Query(name=str(name))
        try:
            els = F.find_all(q, limit=int(limit or 5))
        except Exception as exc:      # noqa: BLE001
            raise ToolError("查找失败：%s" % exc) from exc
        out = []
        for el in (els or []):
            r = getattr(el, "client_rect", None) or getattr(el, "rect", None)
            out.append({"name": str(getattr(el, "name", "") or "")[:80],
                        "type": str(getattr(el, "control_type", "") or ""),
                        "rect": list(r) if r else None})
        return {"ok": True, "found": out, "count": len(out)}

    reg.add(
        "uia_find",
        "用 UIA 语义查找控件（最快的一层，毫秒级）。"
        "返回控件名、类型、客户区矩形。",
        _schema({
            "name": _p("string", "控件名（支持部分匹配）"),
            "hwnd": _p("integer", "限定窗口（0=全桌面）"),
            "limit": _p("integer", "最多返回几个（默认 5）"),
        }, ["name"]),
        _uia_find)

    def _vision_locate(text: str, hwnd: int = 0,
                       click: bool = False) -> Any:
        try:
            from ..vision.chain import VisionChain
        except Exception as exc:      # noqa: BLE001
            raise ToolError("视觉降级链不可用：%s" % exc) from exc
        chain = VisionChain(allow_llm=False)
        r = chain.locate(str(text), window=int(hwnd or 0))
        out = r.to_dict()
        if r.ok and click and r.center:
            try:
                from ..input import dispatcher as D
                D.dispatch_click(int(hwnd), r.center[0], r.center[1])
                out["clicked"] = True
            except Exception as exc:      # noqa: BLE001
                out["click_error"] = str(exc)[:120]
        return out

    reg.add(
        "vision_locate",
        "五层降级查找一个目标（UIA → CDP → OCR → 模板 → 大模型）。"
        "按成本从低到高依次试，返回用了哪一层与完整尝试记录。"
        "可选 click=true 直接点它。",
        _schema({
            "text": _p("string", "要找的文字/控件名"),
            "hwnd": _p("integer", "目标窗口句柄"),
            "click": _p("boolean", "找到后是否直接点击（默认 false）"),
        }, ["text"]),
        _vision_locate)

    # ---------------- 4) 浏览器 ----------------
    def _cdp_execute_js(js: str, port: int = 9222,
                        session_id: str = "") -> Any:
        try:
            from ..web_cdp import session as SESS
        except Exception as exc:      # noqa: BLE001
            raise ToolError("CDP 模块不可用：%s" % exc) from exc
        try:
            sess = SESS.connect(port=int(port))
        except Exception as exc:      # noqa: BLE001
            raise ToolError(
                "连不上调试端口 %d：%s" % (port, exc),
                hint="先启动带 --remote-debugging-port=%d 的浏览器"
                     % port) from exc
        try:
            got = sess.evaluate(str(js), session_id=session_id or None)
        except Exception as exc:      # noqa: BLE001
            raise ToolError("执行 JS 失败：%s" % exc) from exc
        finally:
            try:
                sess.close()
            except Exception:      # noqa: BLE001
                pass
        return {"ok": True, "result": got}

    reg.add(
        "cdp_execute_js",
        "在浏览器里执行一段 JavaScript 并返回结果（走 CDP）。"
        "需要浏览器以 --remote-debugging-port 启动。",
        _schema({
            "js": _p("string", "要执行的 JS 表达式"),
            "port": _p("integer", "调试端口（默认 9222）"),
            "session_id": _p("string", "标签页 session id（可选）"),
        }, ["js"]),
        _cdp_execute_js)

    # ---------------- 5) 风控 ----------------
    def _detect(hwnd: int = 0, timeout: float = 3.0) -> Any:
        try:
            from ..risk_detector import detect as _d
        except Exception as exc:      # noqa: BLE001
            raise ToolError("风控模块不可用：%s" % exc) from exc
        try:
            st = _d(hwnd=int(hwnd) or None, timeout=float(timeout or 3.0))
        except Exception as exc:      # noqa: BLE001
            raise ToolError("检测失败：%s" % exc) from exc
        return {"ok": True, "is_clean": bool(st.is_clean),
                "detected": bool(st.detected),
                "severity": str(getattr(st, "severity", "")),
                "action": str(getattr(st, "action", "")),
                "describe": st.describe()}

    reg.add(
        "detect",
        "检测当前屏幕是否有验证题/风控页（三层：DOM → 窗口 → 像素）。"
        "**只检测不解题** —— 检出就说明该叫人了。",
        _schema({
            "hwnd": _p("integer", "目标窗口（0=自动）"),
            "timeout": _p("number", "检测时间预算秒数（默认 3）"),
        }),
        _detect)

    def _pause_for_human(reason: str = "", timeout: float = 300.0) -> Any:
        try:
            from ..risk_detector import guard as G
        except Exception as exc:      # noqa: BLE001
            raise ToolError("熔断模块不可用：%s" % exc) from exc
        try:
            out = G.pause_for_human(str(reason or "需要人工处理"),
                                    timeout=float(timeout or 300.0),
                                    degraded_wait=2.0)
        except G.HumanInterventionTimeout as exc:
            return {"ok": False, "timeout": True,
                    "detail": "等人超时：%s" % exc}
        except Exception as exc:      # noqa: BLE001
            raise ToolError("熔断失败：%s" % exc) from exc
        return {"ok": True, "resumed": True,
                "waited": float(out.get("waited", 0.0)),
                "screenshot": str(out.get("screenshot", "") or "")}

    reg.add(
        "pause_for_human",
        "**熔断**：停下来等人处理（响铃 + 桌面通知 + 等 F9）。"
        "遇到验证题、需要人工决策、或者机器搞不定时调它。",
        _schema({
            "reason": _p("string", "为什么要人处理（会显示给人看）"),
            "timeout": _p("number", "等待秒数（默认 300）"),
        }, ["reason"]),
        _pause_for_human, long_running=True)

    # ---------------- 6) 自主任务 ----------------
    def _execute_autonomous_task(goal: str, dry_run: bool = False,
                                 timeout: float = 1800.0) -> Any:
        try:
            from ..agentic_planner import get_planner, parse_goal
            from ..agentic_planner.executor import DefaultRunner, Executor
        except Exception as exc:      # noqa: BLE001
            raise ToolError("规划层不可用：%s" % exc) from exc

        g = parse_goal(str(goal))
        if not g.ok:
            raise ToolError(
                "这句话我没解析出动作：%s" % g.intent,
                hint="试试「打开记事本输入hello」这种带动作词的句子")

        try:
            plan = get_planner().plan(g)
        except Exception as exc:      # noqa: BLE001
            raise ToolError("规划失败：%s" % exc) from exc

        if dry_run:
            return {"ok": True, "dry_run": True,
                    "plan": plan.describe(),
                    "steps": len(plan.steps)}

        ex = Executor(DefaultRunner(),
                      task_timeout=float(timeout or 1800.0))
        try:
            out = ex.run(plan)
        except Exception as exc:      # noqa: BLE001
            raise ToolError("执行失败：%s" % exc) from exc
        return out.to_dict()

    reg.add(
        "execute_autonomous_task",
        "★ **一句话目标 → 自动拆解 → 执行 → 验证 → 自愈**。"
        "例：「打开记事本输入hello然后保存」。"
        "走完整链路：规划 → 风控闸 → 观测 → 执行 → 验证 → 恢复。"
        "**长任务会阻塞**，风控检出会中途停下叫人。",
        _schema({
            "goal": _p("string", "自然语言目标"),
            "dry_run": _p("boolean", "只规划不执行（默认 false）"),
            "timeout": _p("number", "全局超时秒数（默认 1800）"),
        }, ["goal"]),
        _execute_autonomous_task, long_running=True)

    def _tasks_list(limit: int = 10) -> Any:
        try:
            from ..agentic_planner import memory as MEM
        except Exception as exc:      # noqa: BLE001
            raise ToolError("记忆层不可用：%s" % exc) from exc
        return {"ok": True, "tasks": MEM.list_tasks()[:int(limit or 10)]}

    reg.add(
        "tasks_list",
        "列出历史自主任务（含状态与目标）。用来查「上次那个跑完没有」。",
        _schema({"limit": _p("integer", "最多几条（默认 10）")}),
        _tasks_list)

    # ---------------- 7) 诊断 ----------------
    def _avatar_status() -> Any:
        out: Dict[str, Any] = {"ok": True, "components": {}}
        for key, mod_path, fn in (
            ("risk_detector", "avatar.risk_detector", "selfcheck"),
            ("ocr", "avatar.uia.ocr_fallback", "selfcheck"),
            ("vision_chain", "avatar.vision.chain", "selfcheck"),
            ("planner", "avatar.agentic_planner.planner", "selfcheck"),
        ):
            try:
                mod = __import__(mod_path, fromlist=["selfcheck"])
                r = getattr(mod, fn)()
                out["components"][key] = {
                    "ok": bool(r.get("ok")),
                    "passed": len(r.get("checks", []))
                    - len(r.get("failures", [])),
                    "total": len(r.get("checks", [])),
                }
            except Exception as exc:      # noqa: BLE001
                out["components"][key] = {"ok": False,
                                          "error": str(exc)[:120]}
        out["ok"] = all(c.get("ok") for c in out["components"].values())
        return out

    reg.add(
        "avatar_status",
        "自检各组件（风控 / OCR / 视觉链 / 规划器）是否正常。"
        "出问题先调这个。",
        _schema({}),
        _avatar_status)

    return reg


def describe_tools(registry: Optional[ToolRegistry] = None) -> str:
    """给人看的工具清单。"""
    reg = registry or build_default_tools()
    lines = ["MCP 工具 %d 个" % len(reg), "=" * 60]
    for t in reg.all():
        mark = "⏳" if t.long_running else "  "
        lines.append("%s %-26s %s" % (mark, t.name,
                                      t.description.split("。")[0][:40]))
        props = list((t.input_schema.get("properties") or {}))
        req = t.input_schema.get("required") or []
        if props:
            lines.append("    参数：%s" % ", ".join(
                ("%s*" % p if p in req else p) for p in props))
    lines += ["", "（⏳ = 长任务，可能阻塞几十秒到几分钟）"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """tools 自检。**只注册不调用**（避免真的动桌面）。"""
    import time
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 注册表 ============
    reg = _me.ToolRegistry()
    reg.add("a", "测试", _schema({"x": _p("integer")}), lambda x: x)
    _add("注册成功", len(reg) == 1, "1 个")
    _add("★ 重名被拒（不静默覆盖）",
         _raise_type(lambda: reg.add("a", "x", _schema({}),
                                     lambda: None)) == "ValueError",
         "抛了")
    _add("没有 name 被拒",
         _raise_type(lambda: reg.register(_me.Tool())) == "ValueError",
         "抛了")
    _add("get 拿得到", reg.get("a") is not None, "有")
    _add("get 不存在返回 None", reg.get("nope") is None, "None")
    _add("names 排序", reg.names() == ["a"], str(reg.names()))

    # ============ 2) 调用与必填校验 ============
    _add("正常调用", reg.call("a", {"x": 5}) == 5, "5")
    _add("★ 缺必填参数 → ToolError（调之前就拦）",
         _raise_type(lambda: reg.call("a", {})) == "ToolError", "抛了")
    _err = _raise_err(lambda: reg.call("zzz", {}))
    _add("★ 调不存在的工具 → ToolError 且列出可用（在 hint 里）",
         isinstance(_err, _me.ToolError) and "可用" in _err.hint,
         _err.hint[:40] if isinstance(_err, _me.ToolError) else str(_err))
    _add("参数不是对象 → ToolError",
         _raise_type(lambda: reg.call("a", "x")) == "ToolError", "抛了")

    # ============ 3) 默认工具集 ============
    d = _me.build_default_tools()
    names = d.names()
    _add("★ 注册了 7 个以上工具", len(d) >= 7, "%d 个" % len(d))
    for required in ("launch_app", "mouse_click", "cdp_execute_js",
                     "vision_locate", "execute_autonomous_task",
                     "detect", "pause_for_human"):
        _add("★ 必需工具存在：%s" % required, required in names, "有")

    # ============ 4) Schema 完整性（硬约束 9）============
    bad = []
    for t in d.all():
        s = t.input_schema
        if s.get("type") != "object":
            bad.append("%s: type" % t.name)
        if "properties" not in s:
            bad.append("%s: properties" % t.name)
        if "required" not in s:
            bad.append("%s: required" % t.name)
        if not isinstance(s.get("required"), list):
            bad.append("%s: required 不是数组" % t.name)
    _add("★★ 所有 inputSchema 完整（type/properties/required）",
         not bad, str(bad[:4]) if bad else "%d 个全完整" % len(d))

    _add("★ 每个工具都有 description",
         all(t.description for t in d.all()), "全部有")
    _add("★ 每个工具都有 handler",
         all(callable(t.handler) for t in d.all()), "全部有")

    # required 里的名字必须在 properties 里
    bad2 = []
    for t in d.all():
        props = set(t.input_schema.get("properties") or {})
        for r in (t.input_schema.get("required") or []):
            if r not in props:
                bad2.append("%s.%s" % (t.name, r))
    _add("★★ required 的字段都在 properties 里（否则客户端会懵）",
         not bad2, str(bad2) if bad2 else "全部一致")

    # ============ 5) to_dict 格式 ============
    schemas = d.list_schemas()
    _add("list_schemas 返回列表", isinstance(schemas, list), "list")
    _add("★ 每个 schema 有 name/description/inputSchema 三键",
         all(set(s) >= {"name", "description", "inputSchema"}
             for s in schemas), "三键齐全")
    _add("to_dict JSON 安全",
         json.dumps(schemas, ensure_ascii=False) != "", "ok")

    # ============ 6) 长任务标记 ============
    _add("★ 长任务被标记（execute_autonomous_task）",
         d.get("execute_autonomous_task").long_running is True, "标记了")
    _add("★ pause_for_human 也标记为长任务",
         d.get("pause_for_human").long_running is True, "标记了")
    _add("短工具不标记",
         d.get("list_windows").long_running is False, "没标记")

    # ============ 7) 参数类型正确 ============
    click = d.get("mouse_click").input_schema
    _add("mouse_click 的 x/y 是 integer",
         click["properties"]["x"]["type"] == "integer"
         and click["properties"]["y"]["type"] == "integer", "对")
    _add("mouse_click 的 x/y 是必填",
         set(click["required"]) == {"x", "y"}, str(click["required"]))
    _add("button 有 enum（客户端能做下拉）",
         "enum" in click["properties"]["button"], "有")

    # ============ 8) 不硬解 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    fn_names = {n.name for n in _ast.walk(tree)
                if isinstance(n, _ast.FunctionDef)}
    BAD = {"find_gap", "solve_slider", "detect_gap", "gap_position"}
    _add("★ 不硬解（无缺口识别函数）", not (fn_names & BAD), "干净")

    # ============ 9) describe ============
    _add("describe_tools 不抛",
         isinstance(_me.describe_tools(d), str), "有")
    _add("describe 列出工具名",
         "execute_autonomous_task" in _me.describe_tools(d), "有")

    # ============ 10) ToolError 带 hint ============
    e = _me.ToolError("x", hint="y")
    _add("ToolError 带 hint", e.hint == "y", "有")

    elapsed = time.perf_counter() - t0
    return {"module": "mcp.tools", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


def _raise_err(fn):
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return exc
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
    print(describe_tools())
