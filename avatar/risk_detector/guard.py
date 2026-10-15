# -*- coding: utf-8 -*-
"""熔断与人工介入 —— 5C 的执行面。

## 这个模块在链条里的位置

5B 负责**发现**验证题（三层检测 → RiskStatus），本模块负责**处置**。
两者之间的唯一接口是 :data:`status.ACTION_INTERFACE` ——
本模块**不许自造判定逻辑**，只做翻译：

====================  ==========================  ====================
action                本模块的动作                 失败时
====================  ==========================  ====================
``continue``          什么都不做，直接返回           —
``pause``             ``pause_for_human()``         ``HumanInterventionTimeout``
``abort``             抛 ``RiskBlocked``             —
====================  ==========================  ====================

## 为什么判据是 ``is_clean=False`` 而不是 ``detected=True``

这是产品哲学里最要紧的一条缝。``RiskStatus`` 有三态：

    is_clean=True    → 查过了，干净        → 可以放心动手
    detected=True    → 查出问题了          → 熔断
    is_unknown=True  → **没查成**           → **也要熔断**

第三条容易被漏掉。如果只认 ``detected=True``，那么"OCR 不可用、
检测器自己抛了异常、页面读不出来"这些情况全部会被当成"没问题"
放行 —— 而机器根本不知道屏幕上是什么。那不是自动化，
那是盲开车。所以本模块的守门条件是 ``not status.is_clean``。

## 能力边界

**做**：

- ``pause_for_human`` —— 截图存证 → 出声 → 通知 → 轮询 F9 → 返回或超时；
- 无桌面环境下**退化为**"写日志 + 等固定超时 + 抛超时异常"，不崩；
- ``HumanInterventionRequired`` / ``HumanInterventionTimeout``
  —— **两个异常都必须携带完整的 :class:`InterventionContext`**。

**不做**：

- **不硬解任何验证题**（硬约束 #9）。本模块里没有一行识别滑块缺口、
  点选坐标、OCR 验证码的代码，将来也不会有。人的手才是解法。
- **不接管鼠标键盘**。熔断期间机器**完全不动作** —— 一碰就可能
  把人正在操作的验证码点乱。检测靠只读的 ``GetAsyncKeyState``。
- **不自己写 RiskStatus 判定**。所有处置决策走
  ``status.action`` / ``ACTION_INTERFACE``，本模块只翻译。
- **不在没有桌面的环境里硬试**。远程会话/无桌面时，
  所有 UI 手段（Beep / 通知 / 热键）都不可靠，直接走退化路径。
- **不留后台线程**。等待循环是**同步**的，Ctrl+C 能干净打断。
  起个线程去轮询热键看着"优雅"，实则会在解释器退出时
  留下卡死的非守护线程 —— 那比不优雅严重得多。

## 提示音为什么用 winsound

硬约束：**必须用 Python 内置的 winsound**。理由不是"方便"，
是零依赖 —— 熔断发生在最不该失败的路径上（任务卡住了、
人在等），这时候因为缺个第三方音频库而崩掉，是最荒谬的失败。

## 5C 三个文件的接力关系

    context.py   造现场（截图存盘 + InterventionContext）
    guard.py     停（pause_for_human / 人按 F9 / 超时 / 退化）   ← 本文件
    resume.py    复（resume_from 读上下文，决定 skip/retry/abort）

## 判定入口：``should_intervene``

本模块**唯一**的判定入口是 :func:`should_intervene`。它只做三件事，
判据全部来自 :data:`status.ACTION_INTERFACE`，不自造逻辑：

=======================  ==========  =========================================
``is_clean``             ``detected``  处置
=======================  ==========  =========================================
True                     任意         **放行** —— 查过了，干净
False                    False        **熔断** —— 未知（没查成），保守处理
False                    True         ``action == continue`` → **放行**（信任 5B）
False                    True         ``action == pause``    → **熔断**
False                    True         ``action == abort``    → **熔断 + 抛 RiskBlocked**
=======================  ==========  =========================================

## 为什么 ``bypassable`` 现在是放行（2026-10-01 修正）

5C 初版把 ``is_clean=False`` 一律当熔断，导致
``severity=bypassable`` 的状态（``SEVERITY_ACTION`` 映射是
``continue``，"人不该被打断"）在闸口被**升级**成 pause ——
等于 guard 用事实核对覆盖了 5B 的产品判断。

**这个修正把职责分清楚了**：

- ``detected=False``（未知）→ guard **必须**保守熔断。
  因为这时候 5B **根本没做判断**，放行等于盲开车。
- ``detected=True``（查出来了）→ 5B **已经做了判断**，
  guard 尊重它：severity 说 continue 就 continue。

一句话：**guard 只在"5B 没判断"时越权；5B 判断过了就听 5B 的。**

完整的判定原则见 ``docs/risk_model.md``。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import AvatarError, RiskBlocked
from .context import InterventionContext, build_context
from .status import (
    ACTION_ABORT,
    ACTION_CONTINUE,
    ACTION_INTERFACE,
    ACTION_PAUSE,
)

__all__ = [
    "VK_F9",
    "SM_REMOTESESSION",
    "DEFAULT_INTERVENTION_TIMEOUT",
    "DEFAULT_POLL_INTERVAL",
    "DEBOUNCE_SECONDS",
    "HumanInterventionRequired",
    "HumanInterventionTimeout",
    "AbortSignal",
    "env_report",
    "has_desktop",
    "should_intervene",
    "translate_action",
    "notify",
    "beep",
    "wait_for_hotkey",
    "pause_for_human",
    "guard",
    "guarded",
    "selfcheck",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: F9 的虚拟键码。选 F9 是因为它极少被应用占用，
#: 而且单按不产生字符 —— 不会把东西打进当前焦点控件。
VK_F9 = 0x78

#: 热键轮询间隔（硬约束 A）。
#: 50ms 约等于人按键的最短持续时间 —— 比这更密是纯烧 CPU，
#: 更疏则可能整个错过一次短促的按下。
DEFAULT_POLL_INTERVAL = 0.05

#: 检测到按下后的去抖（硬约束 A）。
#: 人按键时机械抖动 + 键盘自动重发会产生连续"按下"信号。
#: 300ms 足够吞掉一整串，又不至于让人觉得"卡了"。
DEBOUNCE_SECONDS = 0.3

#: 默认等人时长（硬约束 #7）。5 分钟。
#: 再长人就该去干别的了，任务挂着占资源没意义。
DEFAULT_INTERVENTION_TIMEOUT = 300.0

#: Windows 的 SM_REMOTESESSION 索引。
#: 非零 = 当前是远程会话（RDP / 某些虚拟化），
#: 此时 Beep 和桌面通知通常不会真的到达用户眼前。
SM_REMOTESESSION = 0x1000

#: 退化模式下的固定等待时长。比正常短得多 —— 反正没人在看，
#: 长时间挂着只是白占资源。
DEGRADED_WAIT_SECONDS = 5.0

#: 通知标题（统一，方便人按标题搜日志）。
NOTIFY_TITLE = "需要人工处理"

#: 睡眠函数的模块级别名。
#:
#: ## 为什么要有这个别名
#:
#: 直接 ``_sleep(...)`` 写死在函数体里的话，想测"循环里到底
#: 睡了多少、睡的顺序对不对"就只能去改 ``time.sleep`` —— 那是
#: **全局** 修改，会污染整个进程（包括测试框架自己）。
#: 有了这个别名，测试只改 ``guard._sleep``，影响面精确。
#:
#: 这不是"为了测试而设计"，是"为了不被测试逼着做全局脏改动"。
_sleep = time.sleep


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class HumanInterventionRequired(AvatarError):
    """需要人介入 —— 熔断已经发生或正在发生。

    ## 为什么必须携带 context 而不是字符串

    ``AvatarError`` 允许只传 message，但本异常**不允许**。
    ``__init__`` 会检查 ``context`` 参数的合法性 —— 传入
    :class:`InterventionContext` 或它的 ``to_dict()`` 都行。

    理由：调用方 catch 这个异常时，唯一有价值的东西就是
    "现场是什么样"。只有一个字符串的异常，人拿到之后
    还得自己去屏幕上找 —— 那等于没熔断。

    继承 ``AvatarError``（而不是 ``RuntimeError``）是为了
    接入既有的异常层级与 exit_code 体系。

    ## ⚠ 与基类的 ``self.context`` 撞名

    ``AvatarError.__init__(message, **context)`` 会执行
    ``self.context = context``（一个 dict）。而本异常需要一个
    **InterventionContext 对象**叫这个名字。

    处理办法：**绕开基类的 ``**context`` 收集** —— 直接
    ``super().__init__(message)``，然后自己赋 ``self.context``。
    同时把细节塞进 repr 用的 ``self.detail``。这么做是安全的，
    因为本类重写了 ``__str__``，不会走到基类那个
    ``self.context.items()`` 上。
    """

    exit_code = 5      # 与 RiskBlocked 同码：都属于"被风险闸门拦下"

    def __init__(self, reason: str = "", context: Any = None,
                 **detail) -> None:
        # ⚠ super() 必须**第一个调** —— 基类会执行
        #   ``self.context = context``（空 dict）。放在后面的话，
        #   我们设的 InterventionContext 会被它覆盖成 {}。
        super().__init__(str(reason or "检出风险，需要人工处理"))

        # 宽松接受三种形态：InterventionContext / dict / None
        ctx = context
        if isinstance(ctx, InterventionContext):
            pass
        elif isinstance(ctx, dict):
            ctx = InterventionContext.from_dict(ctx)
        else:
            if ctx is not None:
                detail.setdefault("raw_context", repr(ctx)[:200])
            ctx = InterventionContext()

        self.reason = str(reason or "检出风险，需要人工处理")
        self.context = ctx
        self.detail = dict(detail)

    def __str__(self) -> str:
        """不用基类的实现 —— 基类会 ``self.context.items()``，
        而这里的 ``self.context`` 是 InterventionContext，没有 items。"""
        if self.detail:
            extra = ", ".join("%s=%r" % (k, v)
                              for k, v in sorted(self.detail.items()))
            return "%s [%s]" % (self.reason, extra)
        return self.reason

    def describe(self) -> str:
        """多行说明 —— 打印给人看。"""
        return "%s\n%s" % (self.reason, self.context.describe())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": type(self).__name__,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "context": self.context.to_dict(),
            "detail": self.detail,
        }


class HumanInterventionTimeout(HumanInterventionRequired):
    """人没在时限内出现。

    ## 为什么继承 HumanInterventionRequired 而不是并列

    两者对调用方是**同一件事**：任务停在这里了，需要人。
    区别只是"人来了没有"。继承关系让调用方的
    ``except HumanInterventionRequired`` 能一把兜住两种情况，
    需要细分的再单独 catch 本类。

    携带 ``waited``（实际等了多久）和 ``limit``（时限）——
    诊断时这两个数能区分"人没来"和"我们等的时间算错了"。
    """

    exit_code = 5

    def __init__(self, reason: str = "", context: Any = None,
                 waited: float = 0.0, limit: float = 0.0,
                 degraded: bool = False, **extra) -> None:
        self.waited = float(waited or 0.0)
        self.limit = float(limit or 0.0)
        self.degraded = bool(degraded)
        msg = (reason or "等待人工介入超时")
        if self.limit:
            msg = "%s（等了 %.1fs / 上限 %.1fs%s）" % (
                msg, self.waited, self.limit,
                "，无桌面环境降级" if self.degraded else "")
        super().__init__(msg, context=context, waited=round(self.waited, 3),
                         limit=round(self.limit, 3), degraded=self.degraded,
                         **extra)

    @property
    def is_degraded(self) -> bool:
        """是不是在无桌面环境里超时的 —— 这决定了排查方向。"""
        return self.degraded


class AbortSignal(AvatarError):
    """恢复策略选了 abort —— 任务转入 Blocked 状态。

    定义在 guard 里而不是 resume 里：guard 要在熔断阶段
    就能把它抛出来（比如调用方在 ``guarded()`` 上下文里
    决定中止），resume 复用同一个类，避免两个模块各定义一套。
    """

    exit_code = 5

    def __init__(self, reason: str = "", context: Any = None, **detail):
        # ⚠ 先调 super：基类会执行 ``self.context = context``（一个空 dict），
        #   必须让它在**我们赋值之前**跑完，否则我们设的
        #   InterventionContext 会被基类的空 dict 覆盖掉。
        #   （这个顺序踩过一次：异常里 context 变成 {} 而没人发现。）
        super().__init__(str(reason or "任务中止"))
        self.reason = str(reason or "任务中止")
        if isinstance(context, dict):
            context = InterventionContext.from_dict(context)
        self.context = context if isinstance(context, InterventionContext) \
            else InterventionContext()
        self.detail = dict(detail)

    def __str__(self) -> str:
        if self.detail:
            extra = ", ".join("%s=%r" % (k, v)
                              for k, v in sorted(self.detail.items()))
            return "%s [%s]" % (self.reason, extra)
        return self.reason


# --------------------------------------------------------------------------
# 环境探测
# --------------------------------------------------------------------------
def _user32():
    """拿 user32。拿不到返回 None —— 调用方走退化路径。"""
    try:
        from .._win32 import api as A
        return A.user32
    except Exception:      # noqa: BLE001
        return None


def env_report() -> Dict[str, Any]:
    """探测当前环境的 UI 能力。**永不抛异常。**

    返回::

        {"session": "console" | "remote" | "unknown",
         "desktop": bool,        # 有没有可交互桌面
         "beep": bool,           # winsound 能不能用
         "hotkey": bool,         # GetAsyncKeyState 能不能用
         "notify": bool,         # 能不能做桌面通知
         "reason": str}

    ``desktop=False`` 时，guard 走退化路径（硬约束 B）。
    """
    out: Dict[str, Any] = {
        "session": "unknown", "desktop": False, "beep": False,
        "hotkey": False, "notify": False, "reason": "", "platform": sys.platform,
    }
    reasons: List[str] = []

    # ---- 1) 会话类型：SM_REMOTESESSION ----
    u = _user32()
    if u is None:
        out["session"] = "unknown"
        reasons.append("拿不到 user32（非 Windows 或沙箱限制）")
    else:
        try:
            remote = int(u.GetSystemMetrics(SM_REMOTESESSION))
            out["session"] = "remote" if remote else "console"
            if remote:
                reasons.append("当前是远程会话（SM_REMOTESESSION≠0）—— "
                               "提示音与桌面通知可能到不了用户眼前")
        except Exception as exc:      # noqa: BLE001
            out["session"] = "unknown"
            reasons.append("SM_REMOTESESSION 查询失败：%s" % type(exc).__name__)

        # ---- 2) 有没有桌面（能否枚举窗口）----
        #   判据：能枚举到至少一个顶层窗口。枚举不到 = 没有交互桌面
        #   （服务会话、Session 0 隔离、无头 CI）。
        try:
            from ..app_manager import windows as W
            wins = W.list_windows()
            out["desktop"] = bool(wins)
            if not wins:
                reasons.append("枚举不到任何顶层窗口 —— 判定为无桌面")
        except Exception as exc:      # noqa: BLE001
            out["desktop"] = False
            reasons.append("窗口枚举失败：%s" % type(exc).__name__)

        # ---- 3) 热键轮询能力 ----
        try:
            out["hotkey"] = hasattr(u, "GetAsyncKeyState")
            if not out["hotkey"]:
                reasons.append("user32 上没有 GetAsyncKeyState")
        except Exception:      # noqa: BLE001
            out["hotkey"] = False

    # ---- 4) 提示音 ----
    try:
        import winsound      # noqa: F401
        out["beep"] = True
    except Exception:      # noqa: BLE001
        out["beep"] = False
        reasons.append("winsound 不可用（非 Windows）")

    # ---- 5) 通知能力 ----
    #   和 hotkey 一样依赖 shell32/user32；只在 Windows 上有意义
    try:
        from .._win32 import api as A
        out["notify"] = hasattr(A, "shell32") and hasattr(A.shell32,
                                                          "Shell_NotifyIconW")
        if not out["notify"]:
            reasons.append("Shell_NotifyIconW 不可用 —— 通知降级为文本")
    except Exception:      # noqa: BLE001
        out["notify"] = False
        reasons.append("shell32 不可用 —— 通知降级为文本")

    out["reason"] = "；".join(reasons) if reasons else "桌面环境完整"
    return out


def has_desktop() -> bool:
    """有没有可交互桌面。

    ``False`` 时 :func:`pause_for_human` 走退化路径（硬约束 B）：
    写日志 + 等固定超时 + 抛 ``HumanInterventionTimeout``。
    **不是**抛别的异常，也不是静默返回 —— 调用方必须知道
    "人在看，但不在这个环境里"。
    """
    try:
        return bool(env_report().get("desktop"))
    except Exception:      # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 通知（都不阻塞、都不抛）
# --------------------------------------------------------------------------
def beep(times: int = 3, *, freq: int = 880, dur_ms: int = 220,
         gap: float = 0.18) -> bool:
    """用 ``winsound.Beep`` 出声提醒。成功返回 True。

    ## 为什么是 Beep 不是 MessageBeep

    ``winsound.Beep(freq, dur)`` 能**指定频率和时长**，
    而 ``winsound.MessageBeep`` 依赖系统声音方案 ——
    用户把系统提示音设成"无"就彻底听不见了。

    三声短促的 880Hz 是经过考虑的：单声容易被忽略，
    连续长音招人烦。三短是"有事，但不是着火"。

    **绝不抛异常。** 出不了声不构成失败 —— 还有通知和热键。
    """
    ok = False
    try:
        import winsound
        for i in range(max(1, int(times))):
            try:
                winsound.Beep(int(freq), int(dur_ms))
                ok = True
            except Exception:      # noqa: BLE001
                # 某些环境 Beep 会失败但 MessageBeep 可用，试一次
                try:
                    winsound.MessageBeep()
                    ok = True
                except Exception:      # noqa: BLE001
                    return ok
            if i < times - 1:
                _sleep(max(0.0, float(gap)))
    except Exception:      # noqa: BLE001
        return ok
    return ok


def notify(title: str, message: str, *,
           timeout_ms: int = 10000,
           fallback_print: bool = True) -> bool:
    """桌面通知。优先 ``Shell_NotifyIcon``，失败降级为醒目文本。

    返回是否走了"真正的通知"路径（False 表示降级了）。

    ## 为什么不阻塞

    真实的通知气泡需要建一个隐藏窗口 + 消息循环才能收到回调 ——
    那会**阻塞**当前线程，而熔断路径上不能有任何阻塞（人在等）。
    所以这里用最轻量的做法：能弹就弹（``NOTIFYICON`` 一次性气球），
    弹不出就打印。**两种情况都立刻返回。**

    ## 降级文本为什么这么"吵"

    无桌面 / 通知不可用时，唯一能留下痕迹的地方是日志和终端。
    这时候用边框把消息框起来是**刻意的** —— 日志里几百行正常输出
    夹一条无边框的警告，运维扫一眼就划过去了。框起来的是
    "这条必须看到"。
    """
    # ---- 1) 试真通知 ----
    if sys.platform == "win32":
        try:
            from .._win32 import api as A
            fn = getattr(getattr(A, "shell32", None), "Shell_NotifyIconW", None)
            if fn is not None:
                # 建 NOTIFYICONDATAW 需要结构体定义；这里**只探测可用性**，
                # 真正的气球通知留给需要它的场景去接。
                # 现有环境没配该结构体 → 直接走降级，行为可预期。
                pass
        except Exception:      # noqa: BLE001
            pass

    # ---- 2) 降级：醒目文本 ----
    if fallback_print:
        try:
            line = "=" * 60
            print("\n" + line, file=sys.stderr)
            print("  %s" % title, file=sys.stderr)
            for ln in str(message).split("\n"):
                print("  %s" % ln, file=sys.stderr)
            print("  （等待人工处理，按 F9 继续）", file=sys.stderr)
            print(line + "\n", file=sys.stderr)
            sys.stderr.flush()
        except Exception:      # noqa: BLE001
            pass
    return False


def log_intervention(text: str, *, directory: Optional[str] = None) -> str:
    """把熔断事件追加写到一个日志文件。返回路径（失败返回空串）。

    为什么单独写一个文件而不是混进主日志：熔断是**低频高价值**事件
    （一天几次），塞进每秒几行的主日志里会被淹没。
    单独一个 ``interventions.log`` 让人能直接看"这台机器被拦过几次、
    都是什么题"。
    """
    try:
        d = directory
        if d is None:
            from .context import intervention_dir
            d = intervention_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "interventions.log")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, str(text).replace("\n", " ")))
        return path
    except Exception:      # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 热键
# --------------------------------------------------------------------------
def wait_for_hotkey(vk: int = VK_F9, *,
                    timeout: float = DEFAULT_INTERVENTION_TIMEOUT,
                    poll: float = DEFAULT_POLL_INTERVAL,
                    debounce: float = DEBOUNCE_SECONDS,
                    should_stop: Optional[Callable[[], bool]] = None,
                    on_tick: Optional[Callable[[float], None]] = None,
                    ) -> Tuple[bool, float]:
    """轮询等一个热键。返回 ``(是否按下了, 实际等了多久)``。

    ## 实现要点（硬约束 A）

    - ``GetAsyncKeyState(vk) & 0x8000`` —— 最高位是"当前按下"。
      不能只判 ``!= 0``：低字节还编码了"本线程启动后是否被按过"，
      那是历史信息，不是当前状态。
    - 循环内 ``sleep(poll)``，默认 **0.05s**。
    - 检测到按下后 ``sleep(debounce)``，默认 **0.3s** ——
      吞掉机械抖动和键盘自动重发。
    - 超时返回 ``(False, elapsed)``，**不抛异常** ——
      抛不抛是调用方的决定，这个函数只负责"等"。

    ## 为什么用 should_stop 回调而不是信号

    信号（``signal.signal``）只能在主线程注册，而且会和
    Ctrl+C 的默认处理打架。回调简单、可测、不占全局状态。

    ## Ctrl+C

    本函数**同步**执行，不在任何线程里。``KeyboardInterrupt``
    直接向上传播 —— 调用方能干净地中断。这是刻意的：
    起后台线程去轮询热键，会在解释器退出时留下卡死的
    非守护线程（除非小心 join），那比"同步等待"糟糕得多。
    """
    t0 = time.perf_counter()
    limit = max(0.0, float(timeout))
    step = max(0.005, float(poll))
    deb = max(0.0, float(debounce))

    u = _user32()
    if u is None:
        # 没有 user32 —— 等满超时后如实返回 False
        while True:
            el = time.perf_counter() - t0
            if el >= limit:
                return False, el
            if should_stop is not None and should_stop():
                return False, el
            _sleep(min(step, max(0.0, limit - el)))

    while True:
        el = time.perf_counter() - t0
        if el >= limit:
            return False, el
        if should_stop is not None and should_stop():
            return False, el

        try:
            state = int(u.GetAsyncKeyState(int(vk)))
            pressed = bool(state & 0x8000)
        except Exception:      # noqa: BLE001
            pressed = False

        if pressed:
            # 去抖：吃掉自动重发的连击
            if deb:
                _sleep(deb)
            return True, time.perf_counter() - t0

        if on_tick is not None:
            try:
                on_tick(el)
            except Exception:      # noqa: BLE001
                pass

        # 睡满一个间隔，但别睡过超时线
        remain = limit - (time.perf_counter() - t0)
        _sleep(min(step, max(0.0, remain)))


# --------------------------------------------------------------------------
# 判定入口（硬约束 E / F）
# --------------------------------------------------------------------------
def should_intervene(status: Any) -> Tuple[bool, str]:
    """要不要人为介入？返回 ``(是否介入, 原因)``。**本模块唯一的判定入口。**

    ## 判定表（这是 5B 与 5C 的职责边界，改之前先读完）

    =======================  ============  ===================================
    ``is_clean``             ``detected``   处置
    =======================  ============  ===================================
    True                     任意          **放行** —— 查过了，干净
    False                    False         **熔断** —— 未知，保守
    False                    True          ``continue`` → **放行**（信任 5B）
    False                    True          ``pause``    → **熔断**
    False                    True          ``abort``    → **熔断**（抛 RiskBlocked）
    =======================  ============  ===================================

    ## 核心不变式：guard 只在"5B 没判断"时越权

    这两种 ``is_clean=False`` 的情况**完全不同**，初版把它们混为一谈
    是错的：

    - ``detected=False`` → 5B **没做成判断**（OCR 没装、超时、
      检测器自己抛异常、页面读不出来）。这时候 guard **必须**
      保守熔断 —— 放行等于盲开车。
    - ``detected=True`` → 5B **做了判断**，只是结论是"不必打断"
      （``bypassable``）。guard **尊重这个判断**，放行。

    判据全部来自 :data:`status.ACTION_INTERFACE` / ``status.action``，
    **本函数不自造 severity → 动作的映射**（硬约束 E）。

    ## 返回的 reason 取值

    ``clean`` / ``unknown`` / ``detected_continue`` /
    ``detected_pause`` / ``detected_abort`` / ``status_missing`` /
    以及若干 ``*_unreadable`` —— 都是给日志和排查用的，
    调用方**不该**拿它当判据（判据是第一个返回值）。

    ## 为什么不抛异常

    这是一个**纯查询**：guard 要拿它做分派，也允许别的调用方
    （比如 CLI 的 ``avatar detect``）只想知道"要不要停"而不停。
    把"停"的动作留给 :func:`guard` / :func:`pause_for_human`。
    """
    if status is None:
        # 没有状态 = 不知道 = 停下来看
        return True, "status_missing"

    try:
        clean = bool(getattr(status, "is_clean", False))
    except Exception:      # noqa: BLE001
        return True, "is_clean_unreadable"

    if clean:
        return False, "clean"

    # ---- 不干净：先分清"没查成"和"查出来了" ----
    try:
        detected = bool(getattr(status, "detected", False))
    except Exception:      # noqa: BLE001
        return True, "detected_unreadable"

    if not detected:
        # 未知 —— 5B 没做成判断，保守熔断（硬约束 F 的修正版）
        return True, "unknown"

    # ---- 查出来了：尊重 5B 的 severity 判断 ----
    try:
        act = str(getattr(status, "action", "") or "")
    except Exception:      # noqa: BLE001
        return True, "action_unreadable"

    if act == ACTION_CONTINUE:
        return False, "detected_continue"
    if act == ACTION_ABORT:
        return True, "detected_abort"
    if act == ACTION_PAUSE:
        return True, "detected_pause"

    # 契约外的动作值 —— 说明 status.action 返回了 ACTION_INTERFACE
    # 里没有的东西。这时候**保守**，不能放行。
    return True, "detected_action_unknown:%s" % act


def translate_action(status: Any) -> str:
    """把 RiskStatus 翻成动作：``"continue"`` / ``"pause"`` / ``"abort"``。

    :func:`should_intervene` 的薄封装 —— 判定逻辑只有一份，
    这里只负责把 ``(要不要介入, 原因)`` 映射成三个动作字符串，
    供 :func:`guard` 分派。

    ## 为什么还需要这个函数

    ``guard()`` 的分派需要区分三种动作（continue 放行 / pause 等人 /
    abort 抛 RiskBlocked），而 ``should_intervene`` 只回答"要不要停"。
    两者是**同一个判断的两种粒度**，不是两套逻辑 ——
    这个函数里没有任何新的判定。
    """
    intervene, reason = should_intervene(status)

    if not intervene:
        return ACTION_CONTINUE

    # 介入 —— 但要区分"停下来等人"和"直接终止"
    if reason == "detected_abort":
        return ACTION_ABORT
    return ACTION_PAUSE


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def pause_for_human(reason: str = "",
                    context: Any = None,
                    timeout: float = DEFAULT_INTERVENTION_TIMEOUT,
                    *,
                    hwnd: int = 0,
                    url: str = "",
                    current_step: str = "",
                    status: Any = None,
                    capture: bool = True,
                    directory: Optional[str] = None,
                    notify_user: bool = True,
                    sound: bool = True,
                    poll: float = DEFAULT_POLL_INTERVAL,
                    debounce: float = DEBOUNCE_SECONDS,
                    should_stop: Optional[Callable[[], bool]] = None,
                    on_wait: Optional[Callable[[float, float], None]] = None,
                    degraded_wait: float = DEGRADED_WAIT_SECONDS,
                    ) -> Dict[str, Any]:
    """熔断：暂停 → 叫人 → 等人按 F9 → 继续。

    返回一个字典（不是 None —— 调用方需要知道实际等了多久、
    走的是哪条路径）::

        {"resumed": True, "waited": 12.3, "degraded": False,
         "context": InterventionContext, "screenshot": "...",
         "env": {...}}

    ## 参数

    ``context`` 可以传现成的 :class:`InterventionContext`；
    不传则用 ``status`` / ``hwnd`` / ``url`` / ``current_step``
    现场造一个（**并自动截图**）。

    ``should_stop`` —— 回调，返回 True 时立刻放弃等待并抛超时
    异常。用于上层被外部信号要求收工的场景。

    ``on_wait`` —— 每个轮询周期回调一次 ``(已等, 上限)``。
    给 CLI 画进度用，也方便自检注入断言。

    ``degraded_wait`` —— 退化路径下的固定等待秒数
    （:data:`DEGRADED_WAIT_SECONDS`，默认 5s）。做成参数是为了
    自检能在毫秒级验证这条路径 —— 否则单模块自检会因为
    "真的等 5 秒"而突破 2s 预算。

    ## 路径

    1. **探测环境** —— 无桌面 → 退化路径（硬约束 B）
    2. **造 context**（含截图）
    3. **出声 + 通知** —— 都失败也继续
    4. **轮询 F9** —— 按了 → 返回；超时 → 抛异常
    5. ``KeyboardInterrupt`` → **原样传播**，不吞（硬约束 G）

    ## 退化路径（硬约束 B）

    无桌面 / 远程会话时：**写日志 + 等固定超时 + 抛
    HumanInterventionTimeout**，并且 ``degraded=True``。
    不做声音、不做通知、不轮询热键 —— 那些在这个环境里
    全是假的，做了只会让人误以为"系统在等我"。
    """
    t0 = time.perf_counter()
    limit = max(0.0, float(timeout or 0.0))

    # ---- 1) 环境 ----
    env = env_report()
    degraded = not bool(env.get("desktop"))

    # ---- 2) 造/取 context ----
    ctx = context
    if isinstance(ctx, dict):
        ctx = InterventionContext.from_dict(ctx)
    if not isinstance(ctx, InterventionContext):
        ctx = build_context(status, hwnd=hwnd, url=url,
                            current_step=current_step,
                            capture=capture and not degraded,
                            directory=directory)

    # 把环境信息挂进 ctx，事后排查时能一眼看出"当时有没有桌面"
    try:
        ctx.extra.setdefault("env", {})
        if isinstance(ctx.extra["env"], dict):
            ctx.extra["env"].update({
                "session": env.get("session"),
                "desktop": env.get("desktop"),
                "degraded": degraded,
            })
    except Exception:      # noqa: BLE001
        pass

    if not reason:
        r = ctx.risk
        reason = r.describe() if r is not None else "检出风险，需要人工处理"

    # ---- 3) 退化路径（硬约束 B）----
    if degraded:
        log_intervention("DEGRADED 熔断：%s | env=%s | ctx=%s"
                         % (reason, env.get("reason", ""),
                            ctx.current_step or "-"),
                         directory=directory)
        text = ("无桌面环境，无法等待人工介入。\n%s\n%s"
                % (reason, ctx.describe()))
        if notify_user:
            notify(NOTIFY_TITLE, text)

        # 固定短等待 —— 给"其实是探测误判"留一点余地
        waived = max(0.0, float(degraded_wait or 0.0))
        waited = _sleep_with_stop(waived, should_stop,
                                  on_wait, limit or waived)
        raise HumanInterventionTimeout(
            "无桌面环境，人工介入不可用：%s" % reason,
            context=ctx, waited=time.perf_counter() - t0,
            limit=limit, degraded=True,
            env_reason=env.get("reason", ""))

    # ---- 4) 出声 + 通知 ----
    shot = ""
    if notify_user:
        try:
            notify(NOTIFY_TITLE,
                   "%s\n\n%s\n\n按 F9 继续。" % (reason, ctx.describe()))
        except Exception:      # noqa: BLE001
            pass
    if sound:
        try:
            beep()
        except Exception:      # noqa: BLE001
            pass

    log_intervention("熔断：%s | step=%s hwnd=%d url=%s shot=%s"
                     % (reason, ctx.current_step or "-",
                        int(ctx.hwnd or 0), ctx.url or "-",
                        ctx.screenshot_path or "-"),
                     directory=directory)
    shot = ctx.screenshot_path

    # ---- 5) 等 F9 ----
    pressed, waited = wait_for_hotkey(
        VK_F9, timeout=limit, poll=poll, debounce=debounce,
        should_stop=should_stop, on_tick=(
            (lambda el: on_wait(el, limit)) if on_wait is not None else None))

    if pressed:
        return {
            "resumed": True,
            "waited": round(waited, 3),
            "degraded": False,
            "context": ctx,
            "screenshot": shot,
            "env": env,
        }

    # 超时 / 被 should_stop 叫停
    log_intervention("超时：等了 %.1fs | step=%s"
                     % (waited, ctx.current_step or "-"),
                     directory=directory)
    raise HumanInterventionTimeout(
        "等待人工介入超时：%s" % reason,
        context=ctx, waited=waited, limit=limit, degraded=False,
        stopped_by_caller=bool(should_stop is not None and should_stop()))


def _sleep_with_stop(seconds: float, should_stop, on_wait,
                     display_limit: float) -> float:
    """可被 should_stop 打断的分片 sleep。返回实际睡了多久。"""
    t0 = time.perf_counter()
    target = max(0.0, float(seconds))
    while True:
        el = time.perf_counter() - t0
        if el >= target:
            return el
        if should_stop is not None and should_stop():
            return el
        if on_wait is not None:
            try:
                on_wait(el, display_limit)
            except Exception:      # noqa: BLE001
                pass
        _sleep(min(0.05, max(0.0, target - el)))


# --------------------------------------------------------------------------
# 便捷包装
# --------------------------------------------------------------------------
def guard(status: Any = None, *,
          hwnd: int = 0,
          url: str = "",
          current_step: str = "",
          timeout: float = DEFAULT_INTERVENTION_TIMEOUT,
          raise_on_pause: bool = False,
          **kw) -> Dict[str, Any]:
    """按 ``status`` 的处置动作执行。**5C 的推荐入口。**

    判据是 :func:`should_intervene`（见那里的判定表），
    然后按 :data:`ACTION_INTERFACE` 翻译：

    - ``continue`` → 什么都不做，返回 ``{"action": "continue"}``
    - ``pause``    → :func:`pause_for_human`
    - ``abort``    → 抛 :class:`RiskBlocked`（``exit_code=5``）

    ``raise_on_pause=True`` 时，pause 也改抛
    :class:`HumanInterventionRequired` —— 用于"我只想被通知，
    不想在这里阻塞"的调用方（比如异步框架里）。
    """
    act = translate_action(status)
    iface = ACTION_INTERFACE.get(act, {})

    if act == ACTION_CONTINUE:
        return {"action": ACTION_CONTINUE, "resumed": True, "waited": 0.0,
                "degraded": False, "context": None, "screenshot": "",
                "env": {}, "consumed_by": iface.get("consumer", "")}

    if act == ACTION_ABORT:
        # abort 不经过 guard —— 契约里明确写着 guard_entry=None
        r = getattr(status, "describe", None)
        msg = r() if callable(r) else "任务因风险被终止"
        ctx = build_context(status, hwnd=hwnd, url=url,
                            current_step=current_step,
                            capture=kw.pop("capture", False))
        raise RiskBlocked("任务终止：%s" % msg, context=ctx)

    # ---- pause ----
    ctx = build_context(status, hwnd=hwnd, url=url, current_step=current_step,
                        capture=kw.pop("capture", True))
    reason = ""
    r = getattr(status, "describe", None)
    if callable(r):
        reason = r()

    if raise_on_pause:
        raise HumanInterventionRequired(reason, context=ctx)

    out = pause_for_human(reason, ctx, timeout, **kw)
    out["action"] = ACTION_PAUSE
    out["consumed_by"] = iface.get("guard_entry", "pause_for_human")
    return out


def guarded(status: Any = None, **kw):
    """上下文管理器形式 —— ``with guarded(st): ...``

    等价于进入时调 :func:`guard`。pause 时**阻塞等待**，
    解析后继续；abort 时抛 ``RiskBlocked``；continue 时无感通过。

    这样调用方可以只包一层缩进，不为每个动作写 if/elif。
    """
    class _Guard:
        __slots__ = ("_kw", "_status", "result")

        def __init__(self, kw):
            self._kw = kw
            self._status = kw.pop("status", None)
            self.result = None

        def __enter__(self):
            self.result = guard(status=self._status, **self._kw)
            return self

        def __exit__(self, exc_type, exc, tb):
            return False      # 不吞任何异常
    return _Guard(dict(kw, status=status))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """guard 自检（硬约束：必须覆盖四条路径）。

    必测：**环境退化 / F9 恢复 / 超时 / Ctrl+C 中断**。

    ## 外部调用方路径

    本自检全程用 ``_me.xxx`` 访问本模块 —— 因为
    ``python -m avatar.risk_detector.guard`` 会让模块存在两份
    （``__main__`` + 正式名），裸名调用会绕过所有 monkeypatch。
    这是 5B 里花最久才定位的坑，这里直接规避。

    ``quick=True``（默认）：不碰真实桌面、不出声、不真等。
    """
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
    _add("VK_F9 是 0x78", _me.VK_F9 == 0x78, hex(_me.VK_F9))
    _add("轮询间隔 = 0.05s（硬约束 A）",
         _me.DEFAULT_POLL_INTERVAL == 0.05, _me.DEFAULT_POLL_INTERVAL)
    _add("去抖 = 0.3s（硬约束 A）",
         _me.DEBOUNCE_SECONDS == 0.3, _me.DEBOUNCE_SECONDS)
    _add("默认超时 = 300s",
         _me.DEFAULT_INTERVENTION_TIMEOUT == 300.0,
         _me.DEFAULT_INTERVENTION_TIMEOUT)

    # ============ 2) 环境探测不抛 ============
    env = _me.env_report()
    _add("env_report 返回完整字段",
         all(k in env for k in ("session", "desktop", "beep", "hotkey",
                                "notify", "reason")),
         sorted(env))
    _add("env_report 不抛异常", True, env.get("reason", "")[:48])
    _add("env_report.desktop 是 bool",
         isinstance(env.get("desktop"), bool), repr(env.get("desktop")))
    _add("has_desktop 与 env_report 一致",
         _me.has_desktop() == bool(env.get("desktop")), "一致")

    # ============ 3) 判定入口（硬约束 E / F，2026-10-01 修正）============
    from .status import RiskStatus, Severity

    # ---- should_intervene 的判定表 ----
    _add("should_intervene(None) → 介入（不知道就停）",
         _me.should_intervene(None)[0] is True, "status_missing")
    clean = RiskStatus.clean()
    _add("should_intervene(clean) → 放行",
         _me.should_intervene(clean)[0] is False, clean.describe())
    hit_i = RiskStatus.hit("slider", severity=Severity.INTERRUPT)
    _add("should_intervene(interrupt) → 介入",
         _me.should_intervene(hit_i)[0] is True,
         "reason=%s" % _me.should_intervene(hit_i)[1])
    hit_b = RiskStatus.hit("rate_limit", severity=Severity.BLOCK)
    _add("should_intervene(block) → 介入",
         _me.should_intervene(hit_b)[0] is True,
         "reason=%s" % _me.should_intervene(hit_b)[1])

    # ---- ★ 本次修正的核心：bypassable 放行 ----
    hit_bp = RiskStatus.hit("challenge_page", severity=Severity.BYPASSABLE)
    _add("★ should_intervene(bypassable) → 放行（detected=True，信任 5B）",
         _me.should_intervene(hit_bp)[0] is False,
         "is_clean=%s detected=%s action=%s → reason=%s"
         % (hit_bp.is_clean, hit_bp.detected, hit_bp.action,
            _me.should_intervene(hit_bp)[1]))
    _add("★ 前提：bypassable 的 is_clean 是 False 但 detected 是 True",
         hit_bp.is_clean is False and hit_bp.detected is True
         and hit_bp.action == "continue",
         "这正是「不能只看 is_clean」的实证")
    _add("★ info 也放行（同样 detected=True + action=continue）",
         _me.should_intervene(
             RiskStatus.hit("challenge_page", severity=Severity.INFO))[0]
         is False, "info")

    # ---- ★ 未知仍然熔断（保守，这条不能松）----
    unk = RiskStatus.unknown_(reason="ocr_unavailable")
    _add("★ should_intervene(unknown) → 介入（5B 没判断，保守）",
         _me.should_intervene(unk)[0] is True,
         "is_clean=%s detected=%s → reason=%s"
         % (unk.is_clean, unk.detected, _me.should_intervene(unk)[1]))
    _add("★ 未知的 status.action 是 continue —— 不能用它当判据",
         unk.action == "continue" and _me.should_intervene(unk)[0] is True,
         "action=%s 但 detected=%s" % (unk.action, unk.detected))
    _add("should_intervene(timeout) → 介入",
         _me.should_intervene(RiskStatus.timeout())[0] is True, "timeout")
    _add("should_intervene(error) → 介入",
         _me.should_intervene(RiskStatus.error(OSError("boom")))[0] is True,
         "error")

    # ---- 坏 status 一律保守 ----
    class _Bad(object):
        @property
        def is_clean(self):
            raise RuntimeError("坏掉了")

    _add("★ is_clean 读不出来 → 介入（保守，不崩）",
         _me.should_intervene(_Bad())[0] is True, "is_clean_unreadable")

    # ---- reason 取值必须稳定（日志要靠它排查）----
    _add("reason 是 str",
         all(isinstance(_me.should_intervene(s)[1], str)
             for s in (None, clean, hit_i, hit_b, hit_bp, unk)),
         "全部为 str")

    # ---- translate_action 是 should_intervene 的薄封装 ----
    _add("translate_action(None) → pause",
         _me.translate_action(None) == "pause", "None")
    _add("translate_action(clean) → continue",
         _me.translate_action(clean) == "continue", clean.describe())
    _add("translate_action(interrupt) → pause",
         _me.translate_action(hit_i) == "pause", hit_i.describe())
    _add("translate_action(block) → abort",
         _me.translate_action(hit_b) == "abort", hit_b.describe())
    _add("★ translate_action(bypassable) → continue（修正后）",
         _me.translate_action(hit_bp) == "continue",
         "is_clean=%s action=%s" % (hit_bp.is_clean, hit_bp.action))
    _add("translate_action(unknown) → pause（未知仍熔断）",
         _me.translate_action(unk) == "pause",
         "%s | action=%s" % (unk.describe(), unk.action))
    _add("translate_action(timeout) → pause",
         _me.translate_action(RiskStatus.timeout()) == "pause", "timeout")
    _add("translate_action(error) → pause",
         _me.translate_action(RiskStatus.error(OSError("boom"))) == "pause",
         "error")
    _add("translate_action 只返回 ACTION_INTERFACE 的三个值",
         all(_me.translate_action(s) in ("continue", "pause", "abort")
             for s in (None, clean, hit_i, hit_b, hit_bp, unk)),
         "契约内的值")
    _add("★ translate_action 与 should_intervene 判定一致",
         all((_me.translate_action(s) == "continue")
             == (not _me.should_intervene(s)[0])
             for s in (None, clean, hit_i, hit_b, hit_bp, unk)),
         "两者不许分叉")

    # ============ 4) 异常携带 context（硬约束 D）============
    from .context import InterventionContext
    ctx0 = InterventionContext(hwnd=7, url="https://a.com", current_step="s1")
    e1 = _me.HumanInterventionRequired("测试", context=ctx0)
    _add("HumanInterventionRequired 携带 context 对象",
         isinstance(e1.context, InterventionContext)
         and e1.context.hwnd == 7, e1.context.hwnd)
    _add("...吞掉 str context 也不崩",
         isinstance(_me.HumanInterventionRequired("x", context="abc").context,
                    InterventionContext), "兜底")
    _add("...dict context 能还原",
         _me.HumanInterventionRequired(
             "x", context=ctx0.to_dict()).context.current_step == "s1",
         "还原")
    _add("...None context 也给一个空 context（永不缺）",
         isinstance(_me.HumanInterventionRequired("x").context,
                    InterventionContext), "兜底")
    _add("...exit_code = 5", _me.HumanInterventionRequired("x").exit_code == 5,
         "5")
    _add("...describe 含 context 内容",
         "s1" in e1.describe(), e1.describe().split("\n")[0])

    e2 = _me.HumanInterventionTimeout("超时", context=ctx0,
                                      waited=12.5, limit=30.0)
    _add("HumanInterventionTimeout 是 Required 的子类",
         isinstance(e2, _me.HumanInterventionRequired), "继承链")
    _add("...携带 waited / limit",
         e2.waited == 12.5 and e2.limit == 30.0, (e2.waited, e2.limit))
    _add("...degraded 标志可查",
         e2.degraded is False and e2.is_degraded is False, "正常路径")
    e3 = _me.HumanInterventionTimeout("超时", context=ctx0, degraded=True)
    _add("...退化标志能带出来",
         e3.degraded is True and e3.is_degraded is True, "退化路径")
    _add("...消息里包含等待时长",
         "12.5" in str(e2) or "12.5" in e2.args[0], e2.args[0])

    # ============ 5) 提示音/通知不阻塞不抛 ============
    _add("beep(不存在的设备) 不抛",
         isinstance(_me.beep(times=1, freq=880, dur_ms=10), bool), "安全")
    _add("notify 返回 bool（降级为 False）",
         isinstance(_me.notify("t", "m", fallback_print=False), bool),
         "不阻塞")

    # ============ 6) 热键等待：可注入、不真等 ============
    # 6a) 立刻"按下"
    calls = {"n": 0}
    orig_u32 = _me._user32

    class _U:
        @staticmethod
        def GetAsyncKeyState(vk):
            calls["n"] += 1
            return -32768      # 0x8000 置位 = 按下
    _me._user32 = lambda: _U
    try:
        ok, waited = _me.wait_for_hotkey(timeout=1.0, poll=0.01, debounce=0.0)
        _add("F9 按下 → 返回 (True, elapsed)",
             ok is True and waited >= 0.0, (ok, round(waited, 3)))
        _add("...确实轮询过 GetAsyncKeyState", calls["n"] >= 1, calls["n"])
    finally:
        _me._user32 = orig_u32

    # 6b) 永不按 → 超时返回 False（不抛）
    class _U2:
        @staticmethod
        def GetAsyncKeyState(vk):
            return 0
    _me._user32 = lambda: _U2
    try:
        tp = time.perf_counter()
        ok2, waited2 = _me.wait_for_hotkey(timeout=0.2, poll=0.02,
                                           debounce=0.0)
        dt = time.perf_counter() - tp
        _add("永不按 → 超时返回 (False, elapsed) 不抛",
             ok2 is False, (ok2, round(waited2, 3)))
        _add("...实际等待接近 timeout（没提前退、没超太久）",
             0.18 <= dt <= 0.9, round(dt, 3))
    finally:
        _me._user32 = orig_u32

    # 6c) 去抖确实被 sleep 了（硬约束 A 的执行验证）
    #     打桩目标是 **guard._sleep 别名**，不是全局 time.sleep ——
    #     改全局会污染整个进程（含测试框架）。
    slept = {"v": []}
    orig_sleep = _me._sleep
    tick = {"n": 0}

    class _U3:
        @staticmethod
        def GetAsyncKeyState(vk):
            tick["n"] += 1
            return -32768 if tick["n"] >= 2 else 0

    def _fake_sleep(s):
        slept["v"].append(round(float(s), 4))
        return orig_sleep(min(float(s), 0.001))     # 真的睡一小下
    _me._sleep = _fake_sleep
    _me._user32 = lambda: _U3
    try:
        ok3, _ = _me.wait_for_hotkey(timeout=1.0, poll=0.05, debounce=0.3)
        _add("按下后 sleep(0.3) 去抖（硬约束 A 执行验证）",
             0.3 in slept["v"] or any(abs(x - 0.3) < 1e-6 for x in slept["v"]),
             slept["v"])
        _add("轮询期间 sleep(0.05)（硬约束 A 执行验证）",
             any(abs(x - 0.05) < 1e-6 for x in slept["v"]), slept["v"])
        _add("去抖在返回前执行（顺序正确）",
             ok3 is True and slept["v"] and abs(slept["v"][-1] - 0.3) < 1e-6,
             slept["v"][-4:])
        _add("去打桩的是模块级别名而非全局 time.sleep（不污染进程）",
             _me._sleep is _fake_sleep and time.sleep is orig_sleep,
             "精确打桩")
    finally:
        _me._sleep = orig_sleep
        _me._user32 = orig_u32

    # 6d) should_stop 能提前叫停
    _me._user32 = lambda: _U2
    try:
        st = {"n": 0}

        def _stop():
            st["n"] += 1
            return st["n"] > 3
        tp = time.perf_counter()
        ok4, _w = _me.wait_for_hotkey(timeout=5.0, poll=0.01,
                                      debounce=0.0, should_stop=_stop)
        _add("should_stop 能提前结束等待（不等满 5s）",
             ok4 is False and (time.perf_counter() - tp) < 2.0,
             round(time.perf_counter() - tp, 3))
    finally:
        _me._user32 = orig_u32

    # ============ 7) pause_for_human：F9 恢复路径 ============
    # 打桩：环境=有桌面、不出声、不通知、热键立刻按下
    saved = {"env": _me.env_report, "beep": _me.beep,
             "notify": _me.notify, "log": _me.log_intervention,
             "wait": _me.wait_for_hotkey}
    logs: List[str] = []
    _me.env_report = lambda: {"session": "console", "desktop": True,
                              "beep": True, "hotkey": True, "notify": True,
                              "reason": "", "platform": "win32"}
    _me.beep = lambda *a, **k: True
    _me.notify = lambda *a, **k: True
    _me.log_intervention = lambda text, **k: (logs.append(text), "/tmp/x.log")[1]
    _me.wait_for_hotkey = lambda vk, **k: (True, 3.5)
    try:
        out = _me.pause_for_human("测试原因", ctx0, timeout=30.0,
                                  capture=False)
        _add("F9 恢复路径 → resumed=True",
             out.get("resumed") is True, out.get("resumed"))
        _add("...waited 透传真实等待时长",
             out.get("waited") == 3.5, out.get("waited"))
        _add("...degraded=False", out.get("degraded") is False, "有桌面")
        _add("...context 原样带出",
             out.get("context") is ctx0, "同引用")
        _add("...写了一条熔断日志", len(logs) == 1, logs[:1])
        _add("...env 信息一并返回",
             isinstance(out.get("env"), dict), sorted(out.get("env", {})))
    finally:
        pass       # 保持打桩，下一段继续用

    # ============ 8) pause_for_human：超时路径 ============
    _me.wait_for_hotkey = lambda vk, **k: (False, 30.0)
    try:
        raised = None
        try:
            _me.pause_for_human("超时测试", ctx0, timeout=30.0, capture=False)
        except _me.HumanInterventionTimeout as exc:
            raised = exc
        _add("超时路径 → 抛 HumanInterventionTimeout",
             raised is not None, type(raised).__name__)
        _add("...异常携带 context（硬约束 D）",
             raised is not None and raised.context is ctx0, "同引用")
        _add("...异常携带 waited / limit",
             raised is not None and raised.waited == 30.0
             and raised.limit == 30.0, 
             (getattr(raised, "waited", None), getattr(raised, "limit", None)))
        _add("...degraded=False（正常超时不是退化）",
             raised is not None and raised.degraded is False, "正常路径")
        _add("...异常也是 HumanInterventionRequired（可一把兜住）",
             isinstance(raised, _me.HumanInterventionRequired), "继承链")
    finally:
        pass

    # ============ 9) 环境退化路径（硬约束 B）============
    _me.env_report = lambda: {"session": "remote", "desktop": False,
                              "beep": False, "hotkey": False, "notify": False,
                              "reason": "无桌面（自检打桩）", "platform": "win32"}
    # ⚠ 用 saved["wait"]（第 7 段进打桩**之前**存的真函数），
    #    不要在这里重新取 _me.wait_for_hotkey —— 那时它已经被
    #    第 8 段的超时桩替换了，还原会还原成一个桩。
    _saved_wait = saved["wait"]
    _called = {"wait": 0}

    def _must_not_call(vk, **k):
        _called["wait"] += 1
        return (True, 0.0)
    _me.wait_for_hotkey = _must_not_call
    logs2: List[str] = []
    _me.log_intervention = lambda text, **k: (logs2.append(text), "")[1]
    try:
        raised2 = None
        tp = time.perf_counter()
        try:
            # degraded_wait 传 0.05s —— 自检不该真等 5 秒
            _me.pause_for_human("退化测试", ctx0, timeout=300.0,
                                capture=False, degraded_wait=0.05)
        except _me.HumanInterventionTimeout as exc:
            raised2 = exc
        dt2 = time.perf_counter() - tp
        _add("无桌面 → 抛 HumanInterventionTimeout",
             raised2 is not None, type(raised2).__name__)
        _add("★ 无桌面时 degraded=True（硬约束 B）",
             raised2 is not None and raised2.degraded is True,
             getattr(raised2, "degraded", None))
        _add("★ 无桌面时不轮询热键（轮询是假的，做了反而误导）",
             _called["wait"] == 0, _called["wait"])
        _add("★ 无桌面时等的是 degraded_wait 而非 timeout（不是等满 300s）",
             dt2 < 2.0, round(dt2, 3))
        _add("无桌面时写了 DEGRADED 日志",
             any("DEGRADED" in x for x in logs2), logs2[:1])
        _add("无桌面时异常消息点明原因",
             raised2 is not None and "无桌面" in str(raised2.args[0]),
             str(raised2.args[0])[:60] if raised2 else "")
        _add("无桌面时 context 里挂了 env.degraded",
             ctx0.extra.get("env", {}).get("degraded") is True,
             ctx0.extra.get("env"))
    finally:
        _me.wait_for_hotkey = _saved_wait

    # ============ 10) Ctrl+C 干净打断（硬约束 G）============
    #    这一段必须用**真的** wait_for_hotkey —— 上面几段把它打桩了，
    #    先还原。测的是"真实等待循环里 KeyboardInterrupt 会不会被吞"，
    #    拿桩测等于没测。
    _me.wait_for_hotkey = _saved_wait

    class _UInt:
        @staticmethod
        def GetAsyncKeyState(vk):
            raise KeyboardInterrupt()
    _me._user32 = lambda: _UInt
    try:
        propagated = False
        try:
            _me.wait_for_hotkey(_me.VK_F9, timeout=1.0, poll=0.01,
                                debounce=0.0)
        except KeyboardInterrupt:
            propagated = True
        _add("★ KeyboardInterrupt 从等待循环传播出去（硬约束 G）",
             propagated is True, "不吞")
    finally:
        _me._user32 = orig_u32

    # 10b) KeyboardInterrupt 在 sleep 期间也要传播（更真实的中断点）
    #      人按 Ctrl+C 时进程多半正在 sleep，不是在调 API。
    _me.wait_for_hotkey = _saved_wait

    class _UIdle:
        @staticmethod
        def GetAsyncKeyState(vk):
            return 0

    _me._user32 = lambda: _UIdle
    orig_sleep2 = _me._sleep

    def _kbd_sleep(s):
        raise KeyboardInterrupt()
    _me._sleep = _kbd_sleep
    try:
        prop_sleep = False
        try:
            _me.wait_for_hotkey(_me.VK_F9, timeout=5.0, poll=0.05,
                                debounce=0.0)
        except KeyboardInterrupt:
            prop_sleep = True
        _add("★ sleep 期间的 Ctrl+C 也传播（最真实的中断点）",
             prop_sleep is True, "不吞")
    finally:
        _me._sleep = orig_sleep2
        _me._user32 = orig_u32

    # 10c) pause_for_human 也不吞 KeyboardInterrupt
    _me.env_report = lambda: {"session": "console", "desktop": True,
                              "beep": False, "hotkey": True, "notify": False,
                              "reason": "", "platform": "win32"}
    _me.beep = lambda *a, **k: False
    _me.notify = lambda *a, **k: False
    _me.log_intervention = lambda text, **k: ""

    def _raise_kbd(vk, **k):
        raise KeyboardInterrupt()
    _me.wait_for_hotkey = _raise_kbd
    try:
        prop2 = False
        try:
            _me.pause_for_human("中断测试", ctx0, timeout=10.0, capture=False)
        except KeyboardInterrupt:
            prop2 = True
        except Exception as exc:      # noqa: BLE001
            _add("pause_for_human 不吞 KeyboardInterrupt", False,
                 "抛了别的：%s: %s" % (type(exc).__name__, exc))
        _add("★ pause_for_human 不吞 KeyboardInterrupt（硬约束 G）",
             prop2 is True, "不吞")
    finally:
        _me.wait_for_hotkey = _saved_wait

    # 恢复全部打桩
    _me.env_report = saved["env"]
    _me.beep = saved["beep"]
    _me.notify = saved["notify"]
    _me.log_intervention = saved["log"]
    _me.wait_for_hotkey = saved["wait"]

    # ============ 11) guard() 三态翻译 ============
    _add("guard(clean) → continue 且零副作用",
         _me.guard(clean, capture=False).get("action") == "continue",
         "放行")
    try:
        guard_aborted = False
        try:
            _me.guard(hit_b, capture=False)
        except RiskBlocked:
            guard_aborted = True
        _add("guard(block) → 抛 RiskBlocked",
             guard_aborted is True, "abort 不经过 pause")
    except Exception as exc:      # noqa: BLE001
        _add("guard(block) → 抛 RiskBlocked", False, str(exc))
    _add("guard 消费的入口名与 ACTION_INTERFACE 一致",
         ACTION_INTERFACE[ACTION_PAUSE]["guard_entry"] == "pause_for_human",
         ACTION_INTERFACE[ACTION_PAUSE]["guard_entry"])

    # raise_on_pause 路径
    try:
        rp = None
        try:
            _me.guard(unk, capture=False, raise_on_pause=True)
        except _me.HumanInterventionRequired as exc:
            rp = exc
        _add("guard(raise_on_pause=True) → 抛 Required（不阻塞）",
             rp is not None and not isinstance(
                 rp, _me.HumanInterventionTimeout), type(rp).__name__)
        _add("...该异常带完整 context",
             rp is not None and isinstance(rp.context, InterventionContext),
             "硬约束 D")
    except Exception as exc:      # noqa: BLE001
        _add("guard(raise_on_pause=True)", False, str(exc))

    # ============ 12) guarded() 上下文管理器 ============
    try:
        with _me.guarded(clean, capture=False) as g:
            _add("guarded(clean) 能进入且 result 是 dict",
                 isinstance(g.result, dict), type(g.result).__name__)
    except Exception as exc:      # noqa: BLE001
        _add("guarded(clean) 能进入", False, str(exc))
    try:
        aborted = False
        try:
            with _me.guarded(hit_b, capture=False):
                pass
        except RiskBlocked:
            aborted = True
        _add("guarded(block) 抛 RiskBlocked 且不吞",
             aborted is True, "不吞异常")
    except Exception as exc:      # noqa: BLE001
        _add("guarded(block) 抛 RiskBlocked", False, str(exc))

    # ============ 13) 硬约束 I：docstring 有能力边界 ============
    _add("模块 docstring 含'能力边界'一节",
         "能力边界" in (_me.__doc__ or ""), "硬约束 I")

    # 顶层不自造判定逻辑：本模块不许 import fingerprints
    import ast as _ast
    import io as _io
    src = _io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    top_imports: List[str] = []
    for node in tree.body:
        if isinstance(node, _ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            top_imports.append(node.module or "")
    _add("顶层不 import fingerprints（不自造判定逻辑，硬约束 E）",
         not any("fingerprints" in m for m in top_imports), top_imports)
    _add("顶层不 import 任何 detector_*（guard 不管检测）",
         not any("detector_" in m for m in top_imports), top_imports)
    top_classes = [n.name for n in tree.body if isinstance(n, _ast.ClassDef)]
    _add("顶层只有异常类 + 上下文管理器（无生产用检测类）",
         all(c.endswith(("Required", "Timeout", "Signal", "_Guard"))
             or c.startswith("_") for c in top_classes), top_classes)

    # ============ 14) quick=False：真实环境一次 ============
    if not quick:
        try:
            e = _me.env_report()
            _add("真实环境探测（quick=False）", True,
                 "desktop=%s session=%s" % (e.get("desktop"),
                                            e.get("session")))
        except Exception as exc:      # noqa: BLE001
            _add("真实环境探测（quick=False）", True,
                 "失败但不构成失败：%s" % exc)
    else:
        checks.append({"name": "真实环境探测（quick 跳过）", "ok": True,
                       "detail": "(quick 跳过)"})

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-52s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
    n = len(r["checks"])
    print("guard 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
