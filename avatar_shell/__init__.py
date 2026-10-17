# -*- coding: utf-8 -*-
"""Avatar Shell —— ORB 式悬浮球交互外壳。

## 这是什么

一个**独立于主包**的桌面悬浮球，用来：

1. **看** —— Avatar 现在在干什么（空闲 / 运行中 / 需要人工 / 出错）
2. **控** —— 右键菜单启动任务、暂停、退出
3. **喊** —— 需要人工介入时闪橙 + 出声

## ⚠ 三个硬约束

### 1. 不引入任何第三方 UI 框架（硬约束 1）

**不用 Electron / Tauri / PyQt。** 全部用纯 ctypes 调 Windows 原生 API：

| 需求 | 用什么 |
|---|---|
| 透明窗口 | `WS_EX_LAYERED` + `UpdateLayeredWindow` |
| 置顶 | `SetWindowPos(HWND_TOPMOST)` |
| 点击穿透 | `WS_EX_TRANSPARENT`（鼠标悬停时切换） |
| 拖拽移动 | `WM_NCHITTEST` 返回 `HTCAPTION` |
| 右键菜单 | `TrackPopupMenu`（原生菜单） |
| 提示音 | `winsound.Beep`（标准库） |

**为什么不用框架**：一个 40x40 的悬浮球，用 Electron 装 200MB 运行时 ——
那是拿大炮打蚊子。而且框架会拖一堆依赖进来，
**违背整个项目"零依赖"的底线**。

### 2. 不直接 import avatar（硬约束 2）

外壳与主包**只通过 stdio / MCP 通信**。理由：

- 外壳可以**独立替换**（想用别的 UI 就换，主包不动）
- 主包保持**零依赖**（它不需要知道外壳存在）
- 外壳崩了**不影响主包**（独立进程）

### 3. 故障隔离（硬约束 3）

外壳是**独立进程**。它挂了，Avatar 照常跑任务 ——
只是没人给你显示状态了。

## 模块分工

============================  ==========================================
模块                          职责
============================  ==========================================
:mod:`avatar_shell.state`     Agent Presence 状态机（四种状态）
:mod:`avatar_shell.ball`      悬浮球窗口（创建 / 绘制 / 拖拽 / 穿透）
:mod:`avatar_shell.menu`      右键菜单（原生 ``TrackPopupMenu``）
:mod:`avatar_shell.bridge`    与 ``avatar.mcp`` 的 stdio 通信桥
:mod:`avatar_shell.__main__`  ``python -m avatar_shell`` 入口
============================  ==========================================

## 快速开始

.. code-block:: bash

    python -m avatar_shell                  # 启动悬浮球
    python -m avatar_shell --selftest       # 只跑自检

## 能力边界（整个包）

**做**：状态可视化；原生悬浮球窗口；原生右键菜单；
通过 stdio 与主包通信；出错时如实显示。

**不做**：

- **不用任何 UI 框架**（硬约束 1）。
- **不 import avatar**（硬约束 2）—— 只走 stdio。
- **不做任务逻辑**。它只是"显示 + 转发命令"。
- **不持久化状态**。进程退出就没了 ——
  要持久化的是任务，那是主包的事。
- **不硬解验证题**（硬约束 #9 全项目生效）。
  它只会显示"需要人工"，然后等人。
"""

from .state import (
    AgentState,
    PresenceState,
    StateError,
    describe_state_machine,
)

__all__ = [
    "__version__",
    "AgentState",
    "PresenceState",
    "StateError",
    "describe_state_machine",
    "selfcheck",
]

#: 外壳版本。**与主包独立** —— 它有自己的发布节奏。
__version__ = "0.1.0"


def selfcheck(quick: bool = True) -> dict:
    """包级自检 —— 汇总子模块。

    **不启动 GUI**（那需要桌面环境，且会阻塞）。
    """
    import time as _time

    t0 = _time.perf_counter()
    checks = []
    failures = []

    # ---- state（纯逻辑，总能跑）----
    try:
        from . import state as _st
        r = _st.selfcheck(quick=quick)
        c = len(r.get("checks", []))
        f = len(r.get("failures", []))
        checks.append({"name": "state", "ok": f == 0,
                       "detail": "%d/%d" % (c - f, c)})
        failures.extend("state:%s" % x for x in r.get("failures", []))
    except Exception as exc:      # noqa: BLE001
        checks.append({"name": "state", "ok": False,
                       "detail": str(exc)[:100]})
        failures.append("state")

    # ---- ball / menu / bridge / presence / tooltip / hotkey / input_box ----
    # （state 已在上面单独跑过，不重复）
    for name in ("ball", "menu", "bridge",
                 "presence", "tooltip", "hotkey", "input_box"):
        try:
            mod = __import__("avatar_shell.%s" % name, fromlist=["selfcheck"])
            fn = getattr(mod, "selfcheck", None)
            if not callable(fn):
                checks.append({"name": name, "ok": True,
                               "detail": "（无自检）"})
                continue
            r = fn(quick=quick)
            c = len(r.get("checks", []))
            f = len(r.get("failures", []))
            checks.append({"name": name, "ok": f == 0,
                           "detail": "%d/%d" % (c - f, c)})
            failures.extend("%s:%s" % (name, x)
                            for x in r.get("failures", []))
        except ImportError as exc:
            # 缺模块不算失败 —— 可能只装了部分
            checks.append({"name": name, "ok": True,
                           "detail": "（未实现：%s）" % str(exc)[:40]})
        except Exception as exc:      # noqa: BLE001
            checks.append({"name": name, "ok": False,
                           "detail": str(exc)[:100]})
            failures.append(name)

    return {"module": "avatar_shell", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures,
            "elapsed": round(_time.perf_counter() - t0, 4)}


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(r, ensure_ascii=False, indent=2))
