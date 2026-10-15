# -*- coding: utf-8 -*-
"""pytest 全局夹具 —— 消除跨测试污染。

## 这个文件解决的问题

阶段三有两条测试**间歇性**失败：

```
tests/test_phase3_input.py::test_move_and_restore
    落点不精确：目标 (960,900)，实际 (964,902)   ← 差 4px
    落点不精确：目标 (960,900)，实际 (959,900)   ← 差 1px（另一次）
tests/test_phase3_dispatcher.py::test_silent_click_does_not_move_cursor
```

**单独跑必过，全量跑偶尔红。** 每次偏的方向和幅度都不一样 ——
这不是逻辑错误，是**跨测试污染**：前一个测试把光标留在某个位置，
或者测试期间有别的进程/系统动画动了光标。

## 两个 autouse 夹具

| 夹具 | 时机 | 作用 |
|---|---|---|
| :func:`reset_cursor` | 每个测试**前后** | 光标复位到 (0,0) |
| :func:`reset_interventions` | 每个测试**前后** | 清掉本测试产生的熔断产物 |

两个都是 ``autouse=True`` —— 不需要测试自己声明。这是刻意的：
污染是**全局性**问题，靠每个测试记得声明是治不住的。

## 能力边界

**做**：把光标挪到 (0,0)；删掉测试期间**新增**的熔断产物。

**不做**：

- **不删除测试开始前就存在的文件**。`~/.avatar/interventions/`
  可能有人工保留的真实熔断现场。夹具只删 ``after - before`` 的差集。
- **不碰 `.gitignore`**。那个文件是 :mod:`risk_detector.context`
  维护的目录标记，删了会让目录暴露。
- **不在无桌面环境里报错**。拿不到 `user32` 时静默跳过 ——
  无头环境跑不出光标，那是环境的事实，不是测试的失败。
- **不还原光标到"原位置"**。刻意复位到 (0,0) 而不是"测前的位置"：
  原位置本身就是不确定的（正是污染的来源），还原它等于把污染传下去。
"""

from __future__ import annotations

import os

import pytest

#: 光标复位目标。用 (0,0) 而不是"测前位置" —— 见模块 docstring。
RESET_POSITION = (0, 0)

#: 熔断产物目录（相对用户主目录）。
INTERVENTION_SUBDIR = os.path.join(".avatar", "interventions")

#: 这个文件名永远不删。
_KEEP = {".gitignore"}


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _intervention_dir() -> str:
    """熔断产物目录的绝对路径。**不创建目录。**"""
    try:
        from avatar.risk_detector import context as C
        return str(C.intervention_dir(create=False))
    except Exception:      # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), INTERVENTION_SUBDIR)


def _listdir(path: str) -> set:
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _set_cursor(x: int, y: int) -> bool:
    """直接把光标挪过去。**不走输入层** —— 那会触发真人化轨迹。

    返回是否成功。任何失败都吞掉 —— 无头环境下这个调用必然失败，
    那不是测试该关心的事。
    """
    try:
        from avatar._win32.api import user32
        return bool(user32.SetCursorPos(int(x), int(y)))
    except Exception:      # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_cursor():
    """每个测试前后把光标复位到 (0,0)。

    治的是阶段三那两条 flaky：

    - ``test_move_and_restore`` 断言落点**精确**等于目标坐标。
      前一个测试把光标留在别处、或者系统动画正在动光标，
      都会让它在测试开始前就处于不确定状态。
    - ``test_silent_click_does_not_move_cursor`` 断言静默点击
      **不改变**光标位置。如果测前光标正在被别的东西挪动，
      这条会误判。

    复位是**幂等**的：不管测前在哪，测后都在 (0,0)。
    """
    _set_cursor(*RESET_POSITION)
    yield
    _set_cursor(*RESET_POSITION)


@pytest.fixture(autouse=True)
def reset_interventions():
    """清掉本测试在 ``~/.avatar/interventions/`` 里产生的文件。

    为什么需要：阶段五的熔断会**自动截图存盘**。测试跑多了，
    这个目录会攒下几十个 BMP（每个几百 KB），而且会让
    "目录里有几个文件"这类断言变得不可靠。

    **只删差集** —— 测试开始前就存在的文件一个不动。
    有人工保留的熔断现场时，这个夹具不会误伤。
    """
    d = _intervention_dir()
    before = _listdir(d)
    yield
    after = _listdir(d)

    for name in sorted(after - before):
        if name in _KEEP:
            continue
        path = os.path.join(d, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            # 删不掉就算了（可能被占用 / 权限）——
            # 清理失败不该让测试变红
            pass


# --------------------------------------------------------------------------
# 可选：给需要"确定性光标"的测试用
# --------------------------------------------------------------------------
@pytest.fixture
def clean_cursor():
    """显式声明"我要一个干净的光标起点"。

    ``reset_cursor`` 已经 autouse 了，这个夹具存在的意义是
    **在测试中间**也能复位 —— 比如一条测试里要先做几个
    会动光标的动作，再断言某个精确落点。
    """
    def _reset():
        return _set_cursor(*RESET_POSITION)
    _reset()
    return _reset


# --------------------------------------------------------------------------
# DPI 感知：进程级一次性状态，必须在会话开始时固定
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def _pin_dpi_awareness():
    """把进程的 DPI 感知固定下来，让所有测试看到同一个坐标系。

    ## 为什么这个夹具是必须的（不是可选优化）

    **DPI 感知是进程级、一次性、不可逆的全局状态**：
    Windows 只允许设一次，设完不能改。

    后果是：**谁先调它，决定了后面所有测试看到的屏幕尺寸**。

    - DPI unaware → 本机报 1440x900（逻辑分辨率）
    - DPI aware   → 本机报 2880x1800（物理分辨率）

    阶段三的 ``test_move_and_restore`` 会按屏幕宽算采样点：
    1920/2880 宽的屏幕上，某些坐标在"归一化 → 反算"往返中会偏 1px。
    所以**单独跑必过、连跑必挂** —— 差别只在于前面的测试有没有设过 DPI。

    ## 为什么是 session 级

    因为它是"一次性"状态，不能每个测试"重置"。只能在整个会话
    开始时确定它，让所有测试站在同一个地基上。

    ## 能力边界

    只**确保已设置**，不尝试改回 unaware —— Windows 不允许。
    拿不到 API 时静默跳过（非 Windows / 沙箱环境）。
    """
    try:
        from avatar._win32 import dpi as D
        D.set_process_dpi_awareness()
    except Exception:      # noqa: BLE001
        pass
    yield


# --------------------------------------------------------------------------
# 全局状态隔离（阶段九 9.0-A）
# --------------------------------------------------------------------------
#: 会话开始时的全局状态快照。由 :func:`_isolate_global_state` 填。
_GLOBAL_BASELINE: dict = {}

#: 被检测到"被前序测试改过"的记录（诊断用）。
_GLOBAL_DRIFT: list = []


def _snapshot_global_state() -> dict:
    """拍一张**轻量**的全局状态快照。

    ## 为什么只要这几项

    因为这个函数**每个测试都会调**（autouse）。快照必须便宜 ——
    读几个属性、查一次环境变量，微秒级。

    重量级的东西（枚举窗口、抓屏）不能进来。

    ## 监控哪五项

    ====================  ==========  ====================================
    项                    能否复位    为什么
    ====================  ==========  ====================================
    DPI 感知              ✗ 不能      Windows 只允许设一次，**不可逆**
    COM 单元模型          ✗ 不能      线程级，设了不能改
    locale                ✓ 能        ``setlocale`` 可改回
    工作目录              ✓ 能        ``os.chdir`` 可改回
    ``AVATAR_*`` 环境变量  ✓ 能        ``os.environ`` 可改回
    ====================  ==========  ====================================

    **不能复位的那两项只做"检测并报告"** —— 检测到漂移说明
    有测试在改全局状态，那是它的问题，不是这个夹具能修的。
    """
    s: dict = {}

    # ---- DPI（只读，不可逆）----
    # ``set_process_dpi_awareness`` 是幂等的：已经设过就返回当前状态，
    # 不会真的重设。所以用它当 getter 是安全的。
    try:
        from avatar._win32 import dpi as D
        s["dpi"] = str(D.set_process_dpi_awareness())
    except Exception:      # noqa: BLE001
        s["dpi"] = ""

    # ---- COM 单元模型（只读）----
    try:
        from avatar._win32 import com as C
        s["com"] = str(C.apartment_type())
    except Exception:      # noqa: BLE001
        s["com"] = ""

    # ---- locale（可复位）----
    try:
        import locale as _loc
        s["locale"] = str(_loc.setlocale(_loc.LC_ALL))
    except Exception:      # noqa: BLE001
        s["locale"] = ""

    # ---- 工作目录（可复位）----
    try:
        import os as _os
        s["cwd"] = _os.getcwd()
    except Exception:      # noqa: BLE001
        s["cwd"] = ""

    # ---- AVATAR_* 环境变量（可复位）----
    try:
        import os as _os
        s["env"] = {k: v for k, v in _os.environ.items()
                    if k.startswith("AVATAR_")}
    except Exception:      # noqa: BLE001
        s["env"] = {}

    return s


def _restore_global_state(base: dict, cur: dict) -> list:
    """把**能复位**的项改回基线。返回被复位了哪些项的名字。

    **不能复位的（DPI / COM）只报告，不动。**
    """
    restored: list = []

    # ---- locale ----
    if cur.get("locale") and base.get("locale") \
            and cur["locale"] != base["locale"]:
        try:
            import locale as _loc
            _loc.setlocale(_loc.LC_ALL, base["locale"])
            restored.append("locale")
        except Exception:      # noqa: BLE001
            pass

    # ---- 工作目录 ----
    if cur.get("cwd") and base.get("cwd") and cur["cwd"] != base["cwd"]:
        try:
            import os as _os
            _os.chdir(base["cwd"])
            restored.append("cwd")
        except Exception:      # noqa: BLE001
            pass

    # ---- AVATAR_* 环境变量 ----
    if cur.get("env") != base.get("env"):
        try:
            import os as _os
            # 删掉多出来的
            for k in set(cur.get("env", {})) - set(base.get("env", {})):
                _os.environ.pop(k, None)
            # 恢复改过的
            for k, v in (base.get("env") or {}).items():
                if _os.environ.get(k) != v:
                    _os.environ[k] = v
            restored.append("env")
        except Exception:      # noqa: BLE001
            pass

    return restored


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """每个测试前后对比全局状态，**能复位的先复位**（阶段九 9.0-A）。

    ## 为什么需要它

    全量 pytest 偶发失败（单独跑必过），根因是**进程级全局状态
    被前序测试改变**：

    - DPI 感知 → 改变屏幕尺寸 → 坐标换算跟着变
    - locale → 改变 ``str.lower()`` 的行为（土耳其语 i/İ）
    - 工作目录 → 改变相对路径的解析
    - ``AVATAR_*`` 环境变量 → 改变默认配置

    这些都不是"被测对象的状态"，而是**进程的地基**。
    地基被动过，后面的测试就站在了别的地面上。

    ## 复位策略

    **能复位的先复位，不能复位的记录并报告**：

    - locale / cwd / ``AVATAR_*`` → **真的改回去**
    - DPI / COM 单元模型 → 只能检测（Windows 不允许改）

    检测到不可复位的漂移时，会写进 ``_GLOBAL_DRIFT`` 并
    **打印一次警告**（每个测试只警告一次，不刷屏）。

    ## 成本

    快照只读 5 个属性，**微秒级**。比它挡住的那类 bug 便宜得多。
    """
    cur_before = _snapshot_global_state()

    # ---- 会话基线：第一个测试时记录 ----
    if not _GLOBAL_BASELINE:
        _GLOBAL_BASELINE.update(cur_before)

    # ---- 不可复位的漂移：报告 ----
    for key in ("dpi", "com"):
        b = _GLOBAL_BASELINE.get(key)
        c = cur_before.get(key)
        if b and c and b != c:
            sig = (key, b, c)
            if sig not in _GLOBAL_DRIFT:
                _GLOBAL_DRIFT.append(sig)
                print("\n[conftest] ⚠ 全局状态漂移（不可复位）：%s "
                      "%r → %r" % (key, b, c))

    # ---- 可复位的：开测前先复位 ----
    _restore_global_state(_GLOBAL_BASELINE, cur_before)

    yield

    # ---- 测完再复位一次，别把状态漏给下一个 ----
    _restore_global_state(_GLOBAL_BASELINE, _snapshot_global_state())


@pytest.fixture(scope="session")
def global_state_report():
    """会话结束后拿漂移报告（诊断用）。

    用法::

        def test_x(global_state_report):
            ...
        # 或者跑完在 pytest_terminal_summary 里读 _GLOBAL_DRIFT
    """
    yield _GLOBAL_DRIFT


# --------------------------------------------------------------------------
# 阶段六：任务记忆夹具
# --------------------------------------------------------------------------
@pytest.fixture
def tmpmem(tmp_path):
    """造一个**用临时目录**的 :class:`ShortTermMemory` 工厂。

    ```python
    def test_x(tmpmem):
        mem = tmpmem()          # 自动生成 task_id
        mem = tmpmem("my-task") # 指定 task_id
    ```

    ## 为什么必须用它

    阶段六的执行器会往 ``~/.avatar/tasks/`` 写任务存档。
    测试里不隔离的话，跑一次测试就往用户的真实目录里塞几十个
    垃圾任务 —— 而且自检会因此变慢、`avatar tasks` 会变吵。

    **用 tmp_path 的目录，跑完自动清理。**
    """
    from avatar.agentic_planner.memory import ShortTermMemory

    counter = {"i": 0}

    def _mk(task_id: str = ""):
        counter["i"] += 1
        return ShortTermMemory(task_id or "t%d" % counter["i"],
                               directory=str(tmp_path))

    _mk.dir = str(tmp_path)
    return _mk
