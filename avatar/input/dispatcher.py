# -*- coding: utf-8 -*-
"""交互模式分发：物理（SendInput）与静默（PostMessage）双模式。

两种模式的本质差异
------------------
物理模式（SendInput）
    - 走系统的**输入队列**，和真键盘真鼠标完全同一条路
    - 会改变真实光标位置、会抢焦点
    - **受 UIPI 限制**：目标进程权限更高时，事件被静默丢弃
    - 受前台窗口限制：事件发给**当前前台窗口**，不是你想发给谁就发给谁
    - 优点：兼容性最好，任何程序都认

静默模式（PostMessage）
    - 直接把窗口消息塞进目标窗口的消息队列
    - 不动光标、不抢焦点 —— 可以在后台操作不影响用户
    - **不受 UIPI 的输入队列限制**（但仍受 UIPI 的消息过滤限制）
    - 只对有窗口消息循环的目标有效：
      · Win32 传统窗口 ✓
      · Electron / QML / 部分自绘 UI ✗（它们不按坐标处理 WM_LBUTTONDOWN）
      · 游戏（DirectInput / Raw Input）✗
    - 需要一个**坐标**：PostMessage 的 lParam 是**客户区坐标**（不是屏幕）

默认策略
--------
**静默优先，物理兜底**。理由：
    - 静默不干扰用户（可以在用户用电脑的同时跑任务）
    - 静默更快（无轨迹、无延迟）
    - 静默失败时能明确知道失败（窗口不可达 / 消息被过滤）

但静默的兼容性差，所以每次失败都要能降级到物理模式 —— 这也是
为什么每个动作都返回 ``mode_used`` 和 ``fallback_used``。

质量硬约束 #6
-------------
静默模式必须先验证目标窗口可达（``IsWindow`` + 非最小化），
不可达**直接抛 ``TargetUnreachable``**，绝不允许静默失败。

⚠️ 静默模式的三个真实约束（实测得出，不是理论推测）
---------------------------------------------------

**一、`PostMessage` 是异步的，返回成功 ≠ 已生效。**

``PostMessage`` 只把消息排进目标线程队列，目标线程**必须跑消息泵
（``GetMessage``/``PeekMessage`` + ``DispatchMessage``）才会处理**。
所以返回非 0 只说明"排进队列了"，不说明"处理了"。

踩过的坑：写测试窗口时忘了跑消息泵，看到 ``WM_LBUTTONDOWN``
一条都没进回调，一度误判成"静默点击无效"。实际链路完好 ——
跑上消息泵后 ``WM_MOUSEMOVE / WM_LBUTTONDOWN / WM_LBUTTONUP``
三条全部到达，客户区坐标精确。需要确认生效的场合必须回读结果。

**二、`WM_CHAR` 必须用 ``SendMessage``，不能用 ``PostMessage``。**

跨进程 ``WM_CHAR`` 的字符不是从 ``wParam`` 读的，控件会去线程
输入队列取。``PostMessage`` 不改变输入队列状态 → 字符被忽略。
实测：``PostMessage(WM_CHAR)`` 后 ``WM_GETTEXTLENGTH`` 仍为 0；
``SendMessage(WM_CHAR)`` 正常写入。

**三、静默写入对自绘控件无效，必须做结果校验。**

Chromium/Electron/Qt 的输入框不基于 ``WM_CHAR`` 处理输入 ——
消息被收下但文字不产生。因此 ``dispatch_text`` 在静默写入后调
``text_matches`` 回读核对，不一致就降级物理或明确报错，
**不允许静默失败**。

静默 vs 物理 能力一览
---------------------

============================  ============  ============
能力                          静默          物理
============================  ============  ============
移动光标                      不改真实位置  改
抢焦点                        否            是
Win32 传统控件点击            ✓             ✓
Electron / 自绘 UI 点击       ✗             ✓
输入文本（Win32 Edit）        ✓             ✓
输入文本（Electron）          ✗             ✓
修饰键组合                    ✗             ✓
拖拽                          ✗             ✓
后台操作不影响用户            ✓             ✗
受 UIPI 输入队列限制          ✗（消息过滤仍受限） 是
============================  ============  ============
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from .._win32 import structs as S
from .._win32.api import last_error, user32, win32_error_text
from ..errors import ActionFailed, TargetUnreachable
from .. import input as input_layer
from ..human_entropy import entropy

__all__ = [
    "MODE_PHYSICAL",
    "MODE_SILENT",
    "ActionResult",
    "InteractionModeManager",
    "get_mode",
    "set_interaction_mode",
    "set_mode",
    "dispatch_click",
    "dispatch_key",
    "dispatch_text",
    "dispatch_move",
    "dispatch_scroll",
    "dispatch_drag",
    "check_window_reachable",
    "client_to_screen_lparam",
    "make_lparam",
    "describe_modes",
    "silent_get_text",
    "text_matches",
]


MODE_PHYSICAL = "physical"
MODE_SILENT = "silent"

_VALID_MODES = (MODE_PHYSICAL, MODE_SILENT)


# --------------------------------------------------------------------------
# ActionResult —— 质量硬约束 #7
# --------------------------------------------------------------------------
@dataclass
class ActionResult:
    """所有输入动作的统一返回。

    ``method`` / ``mode_used`` / ``fallback_used`` **全部必填**
    （质量硬约束 #7）。这不是为了日志好看 —— 是为了让上层能回答
    "这次到底是怎么点中的"，进而在失败时给出可操作的诊断。
    """

    #: 是否成功
    ok: bool

    #: 实际用的手段。
    #: "invoke" / "value" / "toggle"（UIA 原生）
    #: "mouse" / "keyboard"（输入层）
    #: "postmessage"（静默消息）
    method: str

    #: 实际用的模式。"physical" / "silent"
    mode_used: str

    #: 是否发生了降级（静默→物理，或 UIA→坐标）
    fallback_used: bool

    #: 人类可读的说明
    detail: str = ""

    #: 目标坐标（屏幕绝对坐标），无坐标动作为 None
    point: Optional[Tuple[int, int]] = None

    #: 目标窗口句柄
    hwnd: Optional[int] = None

    #: 耗时（秒）
    duration: float = 0.0

    #: 附加数据（例如 MoveResult.describe()）
    extra: Dict[str, Any] = field(default_factory=dict)

    #: 失败时的异常对象
    error: Optional[str] = None

    #: 执行结果验证结论（阶段四新增）。
    #:
    #: ``None``            —— 没做验证
    #: :class:`VerifyResult` —— 做了，带证据
    #:
    #: **刻意不放裸 bool**。动作返回 ``ok=True`` 只是"指令送出去了"，
    #: ``verified`` 才回答"事情真的发生了吗"。两者必须能分开看 ——
    #: 静默点击 ``PostMessage`` 返回成功但窗口没理它，这是经典翻车场景。
    #:
    #: 用 ``Optional[Any]`` 而不是 ``Optional["VerifyResult"]`` 是为了
    #: **避免 input 层反向依赖 verify 层**（verify.desktop 会 import
    #: vision/dispatcher，硬引用会成环）。这里只当个容器。
    verified: Optional[Any] = None

    #: 验证耗时（秒）。验证本身也是动作，也要计成本。
    verify_elapsed: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.ok)

    def is_confirmed(self) -> bool:
        """**确认**成功：动作发了 + 验证过了。

        与 ``bool(res)``（只看 ``ok``）的区别就是"发了" vs "成了"。
        没做验证时返回 ``False`` —— 严格口径，宁可让调用方知道
        "这只是发出去了"，也不要给一个虚假的确定感。
        """
        return bool(self.ok and self.verified is not None
                    and getattr(self.verified, "ok", False))

    def needs_attention(self) -> bool:
        """需要人/上层介入：动作发了，但结果是"未知"。

        "未知"不等于"失败"。失败可以重试固定路径，未知得换办法 ——
        降级到别的级别、或者上报。刻意与 :meth:`is_confirmed` 分开。
        """
        if self.verified is None:
            return False
        return bool(self.ok and getattr(self.verified, "unknown", False))

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "mode_used": self.mode_used,
            "fallback_used": self.fallback_used,
            "detail": self.detail,
            "point": list(self.point) if self.point else None,
            "hwnd": self.hwnd,
            "duration": round(self.duration, 4),
            "error": self.error,
            "extra": self.extra,
            # 阶段四新增。``verified`` 展开成 dict 而不是塞对象 ——
            # 这样 JSON 序列化、日志、CLI 都不用再特判。
            "verified": (self.verified.to_dict()
                         if hasattr(self.verified, "to_dict")
                         else self.verified),
            "verify_elapsed": round(self.verify_elapsed, 4),
        }

    def __str__(self) -> str:
        status = "✓" if self.ok else "✗"
        fb = "（降级）" if self.fallback_used else ""
        vtag = ""
        if self.verified is not None:
            v = getattr(self.verified, "verdict", None) or "?"
            vtag = {"ok": " 已验证", "fail": " 验证失败",
                    "unknown": " 结果未知"}.get(v, " 验证=%s" % v)
        return "%s %s via %s/%s%s %s%s" % (
            status, "成功" if self.ok else "失败",
            self.method, self.mode_used, fb, self.detail, vtag,
        )


# --------------------------------------------------------------------------
# 模式管理
# --------------------------------------------------------------------------
class InteractionModeManager:
    """全局交互模式。

    用类持有而不是模块变量，是为了让测试能构造隔离的实例，
    互不污染（测试并行跑时很重要）。
    """

    __slots__ = ("_mode", "_allow_fallback", "_force_physical_on_silent_fail")

    def __init__(
        self,
        mode: str = MODE_SILENT,
        *,
        allow_fallback: bool = True,
        force_physical_on_silent_fail: bool = True,
    ):
        if mode not in _VALID_MODES:
            raise ActionFailed(
                "未知交互模式：%r（可选：%s）" % (mode, " / ".join(_VALID_MODES)),
                method="dispatcher",
            )
        self._mode = mode
        self._allow_fallback = allow_fallback
        self._force_physical_on_silent_fail = force_physical_on_silent_fail

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def allow_fallback(self) -> bool:
        return self._allow_fallback

    def set(
        self,
        mode: str,
        *,
        allow_fallback: Optional[bool] = None,
    ) -> str:
        if mode not in _VALID_MODES:
            raise ActionFailed(
                "未知交互模式：%r（可选：%s）" % (mode, " / ".join(_VALID_MODES)),
                method="dispatcher",
            )
        old = self._mode
        self._mode = mode
        if allow_fallback is not None:
            self._allow_fallback = bool(allow_fallback)
        return old

    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self._mode,
            "allow_fallback": self._allow_fallback,
            "force_physical_on_silent_fail": self._force_physical_on_silent_fail,
        }

    def describe_all(self) -> Dict[str, Any]:
        """给 CLI / 报告用的完整模式说明。

        与 :meth:`describe` 的区别：``describe`` 只讲**当前状态**，
        这个方法还要讲**两种模式各自是什么** —— ``avatar mode`` 命令
        要把这张表打给用户看，光有 ``{"mode": "silent"}`` 是不够的。

        返回结构::

            {
              "current": "silent",              # 当前模式
              "allow_fallback": True,
              "modes": {
                "silent":   {"mechanism": ..., "focus": ..., ...},
                "physical": {...},
              },
              # 兼容旧键（CLI 历史版本 / 已有测试读过它们）
              "mode": "silent",
              "force_physical_on_silent_fail": True,
              "silent": {...}, "physical": {...},
            }

        为什么要带 ``mechanism`` 和 ``focus`` 两个字段：这两条是选模式时
        唯一真正要权衡的东西 —— "用哪种机制发"决定了"会不会抢焦点"。
        把它们写进结构里，调用方就不必自己维护一张对照表。
        """
        modes = {
            MODE_SILENT: {
                "mechanism": "PostMessage / SendMessage",
                "focus": "不抢焦点，不动系统光标",
                "cursor": "不移动",
                "requires_foreground": False,
                "requires_visible": True,
                "latency": "低（消息直接进目标队列）",
                "risk": "部分控件忽略无前置移动的点击",
                "detail": "静默模式：不抢焦点、不动系统光标，适合后台自动化。"
                          "要求目标窗口可见且未最小化。",
            },
            MODE_PHYSICAL: {
                "mechanism": "SendInput",
                "focus": "会移动系统光标、可能抢焦点",
                "cursor": "真实移动",
                "requires_foreground": False,
                "requires_visible": False,
                "latency": "高（受轨迹时长约束）",
                "risk": "会干扰用户当前操作；坐标受 DPI 缩放影响",
                "detail": "物理模式：真移动光标、真按键，兼容性最好，"
                          "但会打扰用户。作为静默模式的降级兜底。",
            },
        }
        out: Dict[str, Any] = {
            "current": self._mode,
            "allow_fallback": self._allow_fallback,
            "force_physical_on_silent_fail": self._force_physical_on_silent_fail,
            "modes": modes,
        }
        # 兼容旧键：老 CLI 读 info["silent"] / info["mode"]
        out["mode"] = self._mode
        out.update(modes)
        return out


_MANAGER = InteractionModeManager()


def get_mode() -> str:
    """取当前交互模式。"""
    return _MANAGER.mode


def set_interaction_mode(mode: str, *, allow_fallback: Optional[bool] = None) -> str:
    """设置交互模式，返回旧模式。

    :param mode: "physical" 或 "silent"
    :param allow_fallback: 是否允许静默失败后自动降级到物理
    """
    return _MANAGER.set(mode, allow_fallback=allow_fallback)


# 别名，CLI 更爱用这个短名字
set_mode = set_interaction_mode


def describe_modes() -> Dict[str, Any]:
    """两种交互模式的完整说明。供 ``avatar mode`` 与报告使用。

    见 :meth:`InteractionModeManager.describe_all`。
    """
    return _MANAGER.describe_all()


# --------------------------------------------------------------------------
# 窗口可达性检查 —— 质量硬约束 #6
# --------------------------------------------------------------------------
def check_window_reachable(hwnd: int) -> None:
    """验证窗口可用于静默模式。不可达直接抛 ``TargetUnreachable``。

    检查项（任一不满足即抛）：
        1. hwnd 非 0
        2. ``IsWindow(hwnd)`` 为真（句柄仍有效）
        3. 窗口**未最小化**（``IsIconic`` 为假）
           —— 最小化窗口的消息循环通常会被系统节流，消息发进去
              不会被处理，表现为"静默失败"
        4. 窗口**可见**（``IsWindowVisible``）
           —— 隐藏窗口同理

    **为什么要显式抛而不是返回 False**：
        "静默失败"是这类代码最难查的 bug。调用方看到 ok=False 可能
        选择忽略；抛异常则强制调用方处理（降级或报错）。
    """
    if not hwnd:
        raise TargetUnreachable("hwnd 为空，无法定位目标窗口", hwnd=0, reason="null_hwnd")

    if not user32.IsWindow(hwnd):
        raise TargetUnreachable(
            "窗口句柄无效（窗口可能已销毁）",
            hwnd=hwnd,
            reason="invalid_hwnd",
        )

    if user32.IsIconic(hwnd):
        raise TargetUnreachable(
            "目标窗口已最小化，静默消息不会被处理",
            hwnd=hwnd,
            reason="minimized",
        )

    if not user32.IsWindowVisible(hwnd):
        raise TargetUnreachable(
            "目标窗口不可见，静默消息不会被处理",
            hwnd=hwnd,
            reason="invisible",
        )


def _ensure_reachable(hwnd: int) -> None:
    """内部用的宽松版：句柄无效才抛，最小化/不可见返回但不抛。

    用于``dispatch_*`` 里已经决定走物理模式的场合 —— 物理模式
    不需要窗口可见（虽然最小化时点击会打到别处）。
    """
    if not hwnd:
        raise TargetUnreachable("hwnd 为空", hwnd=0, reason="null_hwnd")
    if not user32.IsWindow(hwnd):
        raise TargetUnreachable("窗口句柄无效", hwnd=hwnd, reason="invalid_hwnd")


# --------------------------------------------------------------------------
# lParam 构造
# --------------------------------------------------------------------------
def make_lparam(x: int, y: int) -> int:
    """把**客户区**坐标打包成 PostMessage 需要的 lParam。

    格式：低 16 位 = x，高 16 位 = y（都是**有符号** 16 位）。
    坐标可能是负的（客户区坐标一般不为负，但滚动视图可能），
    所以要按有符号处理。
    """
    x = int(x) & 0xFFFF
    y = int(y) & 0xFFFF
    return (y << 16) | x


def client_to_screen_lparam(hwnd: int, client_x: int, client_y: int) -> Tuple[int, int]:
    """客户区坐标 → 屏幕绝对坐标（用 ``ClientToScreen``）。

    静默模式需要**客户区**坐标（放 lParam），
    物理模式需要**屏幕**坐标（SendInput 是绝对坐标）。
    这个函数负责前者需要的换算，后者用 ``layout.client_to_screen``。
    """
    pt = S.POINT(int(client_x), int(client_y))
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        err = last_error()
        raise ActionFailed(
            "ClientToScreen 失败（错误 %d：%s）" % (err, win32_error_text(err)),
            method="dispatcher",
            hwnd=hwnd,
        )
    return (pt.x, pt.y)


# --------------------------------------------------------------------------
# 静默模式实现
# --------------------------------------------------------------------------
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010

_BUTTON_MSGS = {
    "left": (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON),
    "right": (_WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON),
    "middle": (_WM_MBUTTONDOWN, _WM_MBUTTONUP, _MK_MBUTTON),
}


def _silent_click(
    hwnd: int,
    client_x: int,
    client_y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.02,
) -> ActionResult:
    """PostMessage 静默点击。"""
    b = button.lower()
    if b not in _BUTTON_MSGS:
        raise ActionFailed(
            "静默模式暂不支持 %r 键（支持 left/right/middle）" % (button,),
            method="postmessage",
        )

    down_msg, up_msg, mk_flag = _BUTTON_MSGS[b]
    lp = make_lparam(client_x, client_y)

    for i in range(max(1, clicks)):
        # 先发一个 mousemove，让目标知道光标"在"这里
        # 很多控件会忽略没有前置 mousemove 的点击
        user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lp)

        ok_down = user32.PostMessageW(hwnd, down_msg, mk_flag, lp)
        if not ok_down:
            err = last_error()
            return ActionResult(
                ok=False,
                method="postmessage",
                mode_used=MODE_SILENT,
                fallback_used=False,
                detail="PostMessage 发送按下消息失败",
                point=(client_x, client_y),
                hwnd=hwnd,
                error="错误 %d：%s" % (err, win32_error_text(err)),
            )

        if interval > 0:
            time.sleep(min(0.02, interval))

        ok_up = user32.PostMessageW(hwnd, up_msg, 0, lp)
        if not ok_up:
            err = last_error()
            return ActionResult(
                ok=False,
                method="postmessage",
                mode_used=MODE_SILENT,
                fallback_used=False,
                detail="PostMessage 发送抬起消息失败",
                point=(client_x, client_y),
                hwnd=hwnd,
                error="错误 %d：%s" % (err, win32_error_text(err)),
            )

        if i < clicks - 1 and interval > 0:
            time.sleep(interval)

    return ActionResult(
        ok=True,
        method="postmessage",
        mode_used=MODE_SILENT,
        fallback_used=False,
        detail="PostMessage %s 点击 %d 次" % (b, clicks),
        point=(client_x, client_y),
        hwnd=hwnd,
    )


def _silent_text(hwnd: int, text: str, *, per_char_delay: float = 0.0) -> ActionResult:
    """PostMessage/SendMessage 静默输入文本。

    ⚠️ 关键实现细节（踩过坑，实测结论）
    ------------------------------------
    1. **必须用 ``SendMessageW``，不能用 ``PostMessageW``**。
       跨进程 ``WM_CHAR`` 的字符**不是**从 ``wParam`` 读的 —— 控件
       会去线程输入队列里取当前字符。``PostMessage`` 只把消息排进队，
       接收线程的输入队列状态没变，所以 ``WM_CHAR`` 被控件忽略。
       实测：``PostMessage(WM_CHAR)`` 后 ``WM_GETTEXTLENGTH`` 仍为 0；
       ``SendMessage(WM_CHAR)`` 正常写入。
       （注：`WM_SETTEXT` 用 PostMessage 也不能用于中文——字符串指针
       在跨进程传递时会失效。）

    2. **同步调用意味着会阻塞**。如果目标窗口的 UI 线程卡住，
       这个调用会一直等。所以外层应配合超时使用。

    3. **局限**：对不基于 ``WM_CHAR`` 处理输入的自绘控件
       （Chromium/Electron 的输入框、部分 Qt 控件）无效 —— 它们
       收下消息但不产生文字。这类目标必须走物理模式。
    """
    sent = 0
    for ch in text:
        # 用 SendMessageW 同步送 WM_CHAR。wParam 是字符码（BMP 内）。
        code = ord(ch)
        if code > 0xFFFF:
            # 超出 BMP 的字符：拆成代理对
            v = code - 0x10000
            ok = user32.SendMessageW(hwnd, _WM_CHAR, 0xD800 + (v >> 10), 0)
            ok2 = user32.SendMessageW(hwnd, _WM_CHAR, 0xDC00 + (v & 0x3FF), 0)
            if not (ok and ok2):
                err = last_error()
                return ActionResult(
                    ok=False, method="message", mode_used=MODE_SILENT,
                    fallback_used=False,
                    detail="SendMessage WM_CHAR 失败（已送 %d 个字符）" % sent,
                    hwnd=hwnd,
                    error="错误 %d：%s" % (err, win32_error_text(err)),
                    extra={"sent": sent, "total": len(text)},
                )
        else:
            user32.SendMessageW(hwnd, _WM_CHAR, code, 0)
        sent += 1
        if per_char_delay > 0:
            time.sleep(per_char_delay)

    return ActionResult(
        ok=True,
        method="message",
        mode_used=MODE_SILENT,
        fallback_used=False,
        detail="静默输入 %d 个字符" % sent,
        hwnd=hwnd,
        extra={"sent": sent, "total": len(text)},
    )


def silent_get_text(hwnd: int, limit: int = 65536) -> str:
    """用 ``WM_GETTEXT`` 回读目标控件的文本。

    这是静默模式的"结果校验"手段 —— 因为静默发送可能"成功返回
    但内容没进去"，调用方应该在写入后回读确认。
    """
    _WM_GETTEXTLENGTH = 0x000E
    _WM_GETTEXT = 0x000D

    n = user32.SendMessageW(hwnd, _WM_GETTEXTLENGTH, 0, 0)
    if n <= 0:
        return ""
    cap = min(int(n) + 2, int(limit))
    buf = ctypes.create_unicode_buffer(cap)
    user32.SendMessageW(
        hwnd, _WM_GETTEXT, cap,
        ctypes.cast(buf, ctypes.c_void_p).value,
    )
    return buf.value


def _silent_key(hwnd: int, vk: int) -> ActionResult:
    """PostMessage 静默按键（不含修饰键状态）。

    ⚠️ 修饰键在静默模式下**不可靠** —— Ctrl+C 这类组合键需要目标
    维护键盘状态（``GetKeyState``），而 PostMessage 不会更新它。
    组合键建议走物理模式。
    """
    ok_down = user32.PostMessageW(hwnd, _WM_KEYDOWN, vk, 0)
    if not ok_down:
        err = last_error()
        return ActionResult(
            ok=False, method="postmessage", mode_used=MODE_SILENT,
            fallback_used=False, detail="PostMessage WM_KEYDOWN 失败",
            hwnd=hwnd, error="错误 %d：%s" % (err, win32_error_text(err)),
        )
    time.sleep(0.01)
    ok_up = user32.PostMessageW(hwnd, _WM_KEYUP, vk, 0)
    if not ok_up:
        err = last_error()
        return ActionResult(
            ok=False, method="postmessage", mode_used=MODE_SILENT,
            fallback_used=False, detail="PostMessage WM_KEYUP 失败",
            hwnd=hwnd, error="错误 %d：%s" % (err, win32_error_text(err)),
        )
    return ActionResult(
        ok=True, method="postmessage", mode_used=MODE_SILENT,
        fallback_used=False, detail="PostMessage 按键 vk=0x%02X" % vk, hwnd=hwnd,
    )


def text_matches(hwnd: int, expected: str, *, strip: bool = False) -> bool:
    """回读目标窗口文本，检查是否包含了 ``expected``。

    **静默模式的结果校验器**。以"子串"而非"全等"判定 —— 因为目标
    可能本来就有内容（追加写入），或者控件会做格式化。

    :param strip: 是否去掉首尾空白再比（部分控件会补换行）
    """
    try:
        got = silent_get_text(hwnd)
    except Exception:
        return True  # 读不出来就不判定失败（可能是无文本接口的控件）
    if strip:
        got = got.strip()
        expected = expected.strip()
    if not expected:
        return True
    return expected in got


# --------------------------------------------------------------------------
# 统一分发
# --------------------------------------------------------------------------
def dispatch_click(
    hwnd: int,
    client_x: int,
    client_y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    human: bool = True,
    mode: Optional[str] = None,
    precision: float = 1.0,
    allow_fallback: Optional[bool] = None,
    verify: Any = None,
    verify_mode: Optional[str] = None,
) -> ActionResult:
    """点击目标窗口客户区坐标 ``(client_x, client_y)``。

    :param human: 物理模式下是否走真人化轨迹
    :param mode: 覆盖全局模式
    :param allow_fallback: 静默失败是否降级到物理
    :param verify: 执行结果验证（阶段四）。三种写法：

        - ``None`` / ``False`` —— 不验证（**默认**，零开销）
        - ``True`` —— 用全局钩子（需先 ``verify.hook.set_verify_hook``
          且 ``set_enabled(True)``）
        - ``dict`` / ``VerifyRequest`` / 可调用 —— 本次专用的判据，
          临时覆盖全局钩子

    :param verify_mode: 只验证不注册钩子时的快捷方式。传 ``"desktop"``
        会自动造一个"截图对比"判据（点完看画面有没有变）。
        这是**最弱**但**最通用**的判据 —— 比不上动作自己给的
        ``suggest``，但总比"什么都不验"强。

    流程：
        1. 解析模式
        2. 静默模式 → 校验可达 → PostMessage
           失败且允许降级 → 转物理
        3. 物理模式 → client 坐标换屏幕坐标 → 真人化/机械点击
        4. ``verify`` 要求时 → 跑验证，结论写进 ``ActionResult.verified``
    """
    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode
    if use_mode not in _VALID_MODES:
        raise ActionFailed("未知模式：%r" % (use_mode,), method="dispatcher")

    fb_allowed = _MANAGER.allow_fallback if allow_fallback is None else bool(allow_fallback)

    if not hwnd:
        raise TargetUnreachable("dispatch_click 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    # ---- 静默模式 ----
    if use_mode == MODE_SILENT:
        try:
            check_window_reachable(hwnd)
        except TargetUnreachable as exc:
            if not fb_allowed:
                raise
            # 降级到物理
            res = _physical_click(
                hwnd, client_x, client_y,
                button=button, clicks=clicks, human=human, precision=precision,
            )
            res.fallback_used = True
            res.detail = "%s（静默不可达：%s → 降级物理）" % (res.detail, exc.reason)
            res.duration = time.monotonic() - t0
            return _attach_verify(res, hwnd, verify, verify_mode,
                                  action="click", t0=t0)

        res = _silent_click(hwnd, client_x, client_y, button=button, clicks=clicks)
        if res.ok:
            res.duration = time.monotonic() - t0
            return _attach_verify(res, hwnd, verify, verify_mode,
                                  action="click", t0=t0)

        if not fb_allowed:
            res.duration = time.monotonic() - t0
            # 静默失败但没降级 —— 也验证一下：可能消息其实进了，
            # 只是 PostMessage 的返回值骗人（这正好是静默路径的经典坑）。
            return _attach_verify(res, hwnd, verify, verify_mode,
                                  action="click", t0=t0)

        # 静默发了但失败 → 降级
        phys = _physical_click(
            hwnd, client_x, client_y,
            button=button, clicks=clicks, human=human, precision=precision,
        )
        phys.fallback_used = True
        phys.detail = "%s（静默失败：%s → 降级物理）" % (phys.detail, res.error or "")
        phys.duration = time.monotonic() - t0
        return _attach_verify(phys, hwnd, verify, verify_mode,
                              action="click", t0=t0)

    # ---- 物理模式 ----
    res = _physical_click(
        hwnd, client_x, client_y,
        button=button, clicks=clicks, human=human, precision=precision,
    )
    res.duration = time.monotonic() - t0
    return _attach_verify(res, hwnd, verify, verify_mode,
                          action="click", t0=t0)


#: dispatch_click 的 verify 提前量：点击后画面/焦点变化需要时间落地，
#: 但这里**不 sleep** —— 让验证自己的 ``settle`` 去等。sleep 在这里
#: 只会让"不验证"的路径也变慢。
def _resolve_verify_spec(verify: Any, hwnd: int,
                         verify_mode: Optional[str]):
    """把 ``verify`` 参数归一成 (用不用钩子, 临时判据, 动作建议)。

    返回 ``(use_hook, temp_hook, suggest)``。

    - ``None`` / ``False`` → 都不做
    - ``True`` → 用全局钩子，且给一个最弱兜底判据
    - dict/VerifyRequest/可调用 → 临时钩子，直接用它
    - 其它 → 都不做（不抛 —— verify 参数写错不该让点击失败）
    """
    # 全局钩子是否可用（注册了 + 开关开着）
    try:
        from ..verify import hook as hook_mod
        hook_ready = hook_mod.has_verify_hook() and hook_mod.enabled()
    except Exception:      # noqa: BLE001
        hook_mod = None
        hook_ready = False

    # 兜底判据：截图对比。只在真正要验证时才造。
    suggest = None
    if verify_mode == "desktop" and hwnd:
        suggest = {"mode": "desktop", "target": hwnd,
                   "verify_window_title": None, "timeout": 5.0}

    if verify in (None, False):
        return (hook_ready, None, suggest)
    if verify is True:
        return (True, None, suggest)
    return (True, verify, suggest)


def _attach_verify(res: ActionResult, hwnd: int, verify: Any,
                   verify_mode: Optional[str], *, action: str,
                   t0: float) -> ActionResult:
    """给已完成的动作补上验证结论。

    **永不抛异常** —— 动作已经做完了，验证环节炸掉不能把成功说成失败。
    失败也只留痕（``extra["verify_error"]``），对外表现为 ``verified=None``。
    """
    use_hook, temp_hook, suggest = _resolve_verify_spec(verify, hwnd,
                                                        verify_mode)
    if not use_hook and temp_hook is None:
        return res

    try:
        from ..verify import hook as hook_mod
    except Exception:      # noqa: BLE001
        return res

    ctx = hook_mod.build_context(
        action,
        ok=res.ok,
        hwnd=hwnd,
        point=res.point,
        mode_used=res.mode_used,
        method=res.method,
        suggest=suggest,
        extra={"fallback_used": res.fallback_used},
    )

    vt0 = time.monotonic()
    try:
        vres = hook_mod.run_verify_hook(ctx, hook=temp_hook,
                                        force=temp_hook is not None)
    except Exception as exc:      # noqa: BLE001
        res.extra["verify_error"] = "%s: %s" % (type(exc).__name__, exc)
        res.verify_elapsed = time.monotonic() - vt0
        return res

    res.verify_elapsed = time.monotonic() - vt0
    if vres is not None:
        res.verified = vres
        res.extra["verified"] = vres.verdict
    res.duration = time.monotonic() - t0
    return res


# --------------------------------------------------------------------------
# 5E：风控闸（挂载点 1/3）
# --------------------------------------------------------------------------
def _risk_gate(hwnd: int, *, action: str = "") -> Optional[Dict[str, Any]]:
    """动作前的一道风控闸。**返回 None = 放行。**

    ## 为什么放在输入层而不是上层

    上层（planner）可以绕过、可以忘记调，而**输入层是必经之路**。
    把闸放在最靠近真实动作的地方，是"兜底"的唯一可靠做法。

    ## 为什么开销可以忽略

    风控集成**默认关闭**。未启用时这里只有一次 ``is_installed()``
    （一个字典查询），纳秒级。启用后也只读窗口标题，不截图。

    ## 延迟 import

    用函数内 import 而不是顶层 —— ``risk_detector`` 会 import
    ``errors`` 和 ``_win32``，和本模块有交叉，顶层 import 容易成环。
    而且默认关闭时，这个 import 根本不会发生。

    ## 异常处理

    ``import`` 失败（比如裁剪过的部署）→ 放行。**检测器坏了
    不该让输入层跟着不能用** —— 那是两个独立的能力。

    但 ``guard`` 自己抛的 ``RiskBlocked`` **会传播** ——
    那是"任务终止"的正确表达，吞掉它才是错的。
    """
    try:
        from ..risk_detector import integration as _ri
    except Exception:      # noqa: BLE001
        return None

    try:
        if not _ri.is_installed():
            return None
        return _ri.guard_before_action(hwnd, action=action)
    except BaseException:      # noqa: BLE001
        # 检测器自己坏了 → 放行，但别把异常吞掉（RiskBlocked 要传播）
        raise


def _blocked_by_risk(hwnd: int, gate: Dict[str, Any],
                     what: str) -> ActionResult:
    """被风控拦下时的返回值。

    ## 为什么返回 ActionResult 而不是抛异常

    输入层的契约就是"返回 ActionResult"。在这里抛异常，
    调用方的 ``if res.ok`` 就接不住了 —— "被风控拦下"
    会被当成"代码崩了"。用返回值表达，语义是连续的。

    （唯一的例外是 ``abort`` 语义 —— 那本来就该终止任务，
    由 ``guard`` 自己抛 ``RiskBlocked``，这里不拦。）
    """
    return ActionResult(
        ok=False,
        method="risk_guard",
        mode_used="",
        fallback_used=False,
        detail="风控拦截：%s 未执行" % what,
        point=None,
        hwnd=hwnd,
        extra={"risk": gate, "blocked_action": what},
    )


def _physical_click(
    hwnd: int,
    client_x: int,
    client_y: int,
    *,
    button: str,
    clicks: int,
    human: bool,
    precision: float,
) -> ActionResult:
    """物理点击：把客户区坐标换算到屏幕坐标再点。

    ## 5E：物理点击前有一道风控闸

    物理点击是**唯一真的会动鼠标**的动作 —— 点错地方代价最大。
    所以在这一层（而不是上层）加闸：只要走到物理点击，
    就必须先过一次 :func:`risk_detector.integration.guard_before_action`。

    那道闸只读窗口标题（**不截图**），所以对点击速度没有可感影响。
    风控集成默认关闭，未启用时这里是一次 ``is_installed()``
    的布尔判断 —— 纳秒级。
    """
    gate = _risk_gate(hwnd, action="physical_click")
    if gate is not None:
        return _blocked_by_risk(hwnd, gate, "物理点击")

    from ..app_manager import layout as layout_mod

    sx, sy = layout_mod.client_to_screen(hwnd, client_x, client_y)

    if human:
        from ..human_entropy import mouse as hm

        if clicks == 2:
            mv = hm.double_click_human(sx, sy, button=button, precision=precision)
        else:
            mv = hm.click_human(sx, sy, button=button, precision=precision)

        # 连击
        for _ in range(max(0, clicks - (2 if clicks == 2 else 1))):
            hm.click_human(None, None, button=button, human=True)
        extra = mv.describe()
    else:
        if clicks == 2:
            input_layer.mouse.double_click(sx, sy, button=button)
        else:
            input_layer.mouse.click(sx, sy, button=button, clicks=clicks)
        extra = {}

    return ActionResult(
        ok=True,
        method="mouse",
        mode_used=MODE_PHYSICAL,
        fallback_used=False,
        detail="物理%s点击 (%d, %d)" % (
            "真人化" if human else "机械", sx, sy
        ),
        point=(sx, sy),
        hwnd=hwnd,
        extra=extra,
    )


def dispatch_text(
    hwnd: int,
    text: str,
    *,
    human: bool = False,
    wpm: float = 320.0,
    mode: Optional[str] = None,
    client_x: Optional[int] = None,
    client_y: Optional[int] = None,
    allow_fallback: Optional[bool] = None,
) -> ActionResult:
    """向目标窗口输入文本。

    静默模式用 ``WM_CHAR``（需要 ``hwnd``）；
    物理模式用 ``SendInput`` 的 UNICODE 路径（发给前台窗口，
    所以如果 ``client_x/y`` 给了，会先点一下把焦点给目标）。
    """
    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode
    fb_allowed = _MANAGER.allow_fallback if allow_fallback is None else bool(allow_fallback)

    if not hwnd:
        raise TargetUnreachable("dispatch_text 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    if not text:
        return ActionResult(
            ok=True, method="text", mode_used=use_mode, fallback_used=False,
            detail="空文本，无需输入", hwnd=hwnd, duration=time.monotonic() - t0,
        )

    if use_mode == MODE_SILENT:
        try:
            check_window_reachable(hwnd)
        except TargetUnreachable as exc:
            if not fb_allowed:
                raise
            res = _physical_text(hwnd, text, human=human, wpm=wpm,
                                 client_x=client_x, client_y=client_y)
            res.fallback_used = True
            res.detail = "%s（静默不可达：%s → 降级物理）" % (res.detail, exc.reason)
            res.duration = time.monotonic() - t0
            return res

        res = _silent_text(hwnd, text)
        if res.ok and not text_matches(hwnd, text):
            # 静默"成功"但内容没进去 —— 这是自绘控件的典型症状。
            # 不允许静默失败：要么降级物理，要么明确报错。
            if fb_allowed:
                phys = _physical_text(hwnd, text, human=human, wpm=wpm,
                                      client_x=client_x, client_y=client_y)
                phys.fallback_used = True
                phys.detail = "%s（静默写入未被控件接受 → 降级物理）" % phys.detail
                phys.duration = time.monotonic() - t0
                return phys
            res.ok = False
            res.detail = "静默写入未被控件接受（可能是不处理 WM_CHAR 的自绘控件）"
        res.duration = time.monotonic() - t0
        return res

    res = _physical_text(hwnd, text, human=human, wpm=wpm,
                         client_x=client_x, client_y=client_y)
    res.duration = time.monotonic() - t0
    return res


def _physical_text(
    hwnd: int,
    text: str,
    *,
    human: bool,
    wpm: float,
    client_x: Optional[int],
    client_y: Optional[int],
) -> ActionResult:
    """物理输入文本。

    物理输入是发给**前台窗口**的，所以如果给了客户区坐标，
    先点一下把目标窗口激活并定位焦点。
    """
    if client_x is not None and client_y is not None:
        from ..app_manager import layout as layout_mod

        sx, sy = layout_mod.client_to_screen(hwnd, client_x, client_y)
        if human:
            from ..human_entropy import mouse as hm

            hm.click_human(sx, sy)
        else:
            input_layer.mouse.click(sx, sy)
        time.sleep(0.06)

    if human:
        from ..human_entropy import keyboard as hk

        tr = hk.type_text_human(text, wpm=wpm)
        return ActionResult(
            ok=True,
            method="keyboard",
            mode_used=MODE_PHYSICAL,
            fallback_used=False,
            detail="物理真人化输入 %d 字符（%.2fs）" % (tr.chars, tr.duration),
            hwnd=hwnd,
            extra=tr.describe(),
        )

    n = input_layer.keyboard.type_text(text)
    return ActionResult(
        ok=True,
        method="keyboard",
        mode_used=MODE_PHYSICAL,
        fallback_used=False,
        detail="物理输入 %d 字符" % n,
        hwnd=hwnd,
        extra={"typed": n},
    )


def dispatch_key(
    hwnd: int,
    key,
    *,
    human: bool = False,
    modifiers: Optional[Sequence] = None,
    mode: Optional[str] = None,
    allow_fallback: Optional[bool] = None,
) -> ActionResult:
    """按键（支持修饰键组合）。

    ``key`` 可以是键名字符串（"enter"）或虚拟键码。
    ``modifiers`` 例如 ``["ctrl", "shift"]``。
    """
    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode
    fb_allowed = _MANAGER.allow_fallback if allow_fallback is None else bool(allow_fallback)

    if not hwnd:
        raise TargetUnreachable("dispatch_key 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    combo = list(modifiers or []) + [key]

    if use_mode == MODE_SILENT and not modifiers:
        try:
            check_window_reachable(hwnd)
        except TargetUnreachable as exc:
            if not fb_allowed:
                raise
            res = _physical_key(combo, human=human)
            res.fallback_used = True
            res.detail = "%s（静默不可达：%s → 降级物理）" % (res.detail, exc.reason)
            res.hwnd = hwnd
            res.duration = time.monotonic() - t0
            return res

        vk = input_layer.keyboard.vk_for_key(key)
        res = _silent_key(hwnd, vk)
        res.duration = time.monotonic() - t0
        return res

    if use_mode == MODE_SILENT and modifiers:
        # 组合键在静默下不可靠 → 直接走物理（并标注降级）
        res = _physical_key(combo, human=human)
        res.fallback_used = True
        res.detail = "%s（静默不支持修饰键 → 降级物理）" % res.detail
        res.hwnd = hwnd
        res.duration = time.monotonic() - t0
        return res

    res = _physical_key(combo, human=human)
    res.hwnd = hwnd
    res.duration = time.monotonic() - t0
    return res


def _physical_key(combo: Sequence, *, human: bool) -> ActionResult:
    if human:
        from ..human_entropy import keyboard as hk

        if len(combo) > 1:
            hk.hotkey_human(combo)
            detail = "物理真人化组合键 %s" % "+".join(str(k) for k in combo)
        else:
            hk.press_key_human(combo[0])
            detail = "物理真人化按键 %s" % combo[0]
    else:
        if len(combo) > 1:
            input_layer.keyboard.hotkey(combo)
            detail = "物理组合键 %s" % "+".join(str(k) for k in combo)
        else:
            input_layer.keyboard.press(combo[0])
            detail = "物理按键 %s" % combo[0]

    return ActionResult(
        ok=True, method="keyboard", mode_used=MODE_PHYSICAL,
        fallback_used=False, detail=detail, extra={"combo": [str(k) for k in combo]},
    )


def dispatch_move(
    hwnd: int,
    client_x: int,
    client_y: int,
    *,
    human: bool = True,
    mode: Optional[str] = None,
    allow_fallback: Optional[bool] = None,
) -> ActionResult:
    """移动光标到目标窗口客户区坐标。

    静默模式下 PostMessage 一条 ``WM_MOUSEMOVE``（不动真实光标）。
    """
    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode
    fb_allowed = _MANAGER.allow_fallback if allow_fallback is None else bool(allow_fallback)

    if not hwnd:
        raise TargetUnreachable("dispatch_move 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    if use_mode == MODE_SILENT:
        try:
            check_window_reachable(hwnd)
        except TargetUnreachable as exc:
            if not fb_allowed:
                raise
            res = _physical_move(hwnd, client_x, client_y, human=human)
            res.fallback_used = True
            res.detail = "%s（静默不可达：%s → 降级物理）" % (res.detail, exc.reason)
            res.duration = time.monotonic() - t0
            return res

        lp = make_lparam(client_x, client_y)
        ok = user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lp)
        if ok:
            return ActionResult(
                ok=True, method="postmessage", mode_used=MODE_SILENT,
                fallback_used=False,
                detail="PostMessage 移动消息 (%d, %d)" % (client_x, client_y),
                point=(client_x, client_y), hwnd=hwnd,
                duration=time.monotonic() - t0,
            )
        err = last_error()
        phys = _physical_move(hwnd, client_x, client_y, human=human)
        phys.fallback_used = True
        phys.detail = "%s（静默失败：错误 %d → 降级物理）" % (phys.detail, err)
        phys.duration = time.monotonic() - t0
        return phys

    res = _physical_move(hwnd, client_x, client_y, human=human)
    res.duration = time.monotonic() - t0
    return res


def _physical_move(hwnd: int, client_x: int, client_y: int, *, human: bool) -> ActionResult:
    from ..app_manager import layout as layout_mod

    sx, sy = layout_mod.client_to_screen(hwnd, client_x, client_y)
    if human:
        from ..human_entropy import mouse as hm

        mv = hm.move_human(sx, sy)
        extra = mv.describe()
    else:
        input_layer.mouse.move_absolute(sx, sy)
        extra = {}

    return ActionResult(
        ok=True, method="mouse", mode_used=MODE_PHYSICAL,
        fallback_used=False,
        detail="物理%s移动 (%d, %d)" % ("真人化" if human else "机械", sx, sy),
        point=(sx, sy), hwnd=hwnd, extra=extra,
    )


def dispatch_scroll(
    hwnd: int,
    amount: int,
    *,
    client_x: Optional[int] = None,
    client_y: Optional[int] = None,
    human: bool = True,
    mode: Optional[str] = None,
) -> ActionResult:
    """滚轮。

    静默模式的 ``WM_MOUSEWHEEL`` 的 lParam 用的是**屏幕**坐标
    （这点和点击不同，容易踩坑），wParam 高位是 delta、低位是键状态。
    """
    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode

    if not hwnd:
        raise TargetUnreachable("dispatch_scroll 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    if use_mode == MODE_SILENT:
        check_window_reachable(hwnd)
        cx = client_x if client_x is not None else 0
        cy = client_y if client_y is not None else 0
        sx, sy = client_to_screen_lparam(hwnd, cx, cy)
        delta = (int(amount) & 0xFFFF) << 16
        lp = make_lparam(sx, sy)
        ok = user32.PostMessageW(hwnd, _WM_MOUSEWHEEL, delta, lp)
        if ok:
            return ActionResult(
                ok=True, method="postmessage", mode_used=MODE_SILENT,
                fallback_used=False,
                detail="PostMessage 滚轮 %d" % amount,
                point=(sx, sy), hwnd=hwnd, duration=time.monotonic() - t0,
            )
        err = last_error()
        raise ActionFailed(
            "PostMessage 滚轮失败（错误 %d：%s）" % (err, win32_error_text(err)),
            method="postmessage", hwnd=hwnd,
        )

    if human:
        from ..human_entropy import mouse as hm

        sx = sy = None
        if client_x is not None and client_y is not None:
            from ..app_manager import layout as layout_mod

            sx, sy = layout_mod.client_to_screen(hwnd, client_x, client_y)
        emitted = hm.scroll_human(amount, x=sx, y=sy)
        extra = {"emitted": emitted}
    else:
        if client_x is not None and client_y is not None:
            from ..app_manager import layout as layout_mod

            sx, sy = layout_mod.client_to_screen(hwnd, client_x, client_y)
            input_layer.mouse.move_absolute(sx, sy)
        input_layer.mouse.scroll(amount, steps=max(1, abs(amount) // 120))
        emitted = amount
        extra = {"emitted": emitted}

    return ActionResult(
        ok=True, method="mouse", mode_used=MODE_PHYSICAL,
        fallback_used=False,
        detail="物理%s滚轮 %d" % ("真人化" if human else "机械", emitted),
        hwnd=hwnd, extra=extra, duration=time.monotonic() - t0,
    )


def dispatch_drag(
    hwnd: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    button: str = "left",
    human: bool = True,
    mode: Optional[str] = None,
    precision: float = 1.0,
) -> ActionResult:
    """拖拽。

    ⚠️ 静默模式**不做**拖拽。原因：
        拖拽需要目标窗口在按下到抬起之间持续收到 mousemove 并保持
        按键状态（``GetKeyState`` 检查），PostMessage 不维护这个状态。
        实测在资源管理器、浏览器里都会失败。强行做只会得到
        "看起来成功但什么都没拖动"的结果 —— 这正是我们要避免的。
        所以这里在静默模式下直接抛 ``UnsupportedError``。
    """
    from ..errors import UnsupportedError

    t0 = time.monotonic()
    use_mode = mode or _MANAGER.mode

    if not hwnd:
        raise TargetUnreachable("dispatch_drag 需要 hwnd", hwnd=0, reason="null_hwnd")
    _ensure_reachable(hwnd)

    if use_mode == MODE_SILENT:
        raise UnsupportedError(
            "静默模式不支持拖拽（目标窗口需要维护按键状态，PostMessage 做不到）",
            capability="silent_drag",
            hwnd=hwnd,
        )

    from ..app_manager import layout as layout_mod

    sx1, sy1 = layout_mod.client_to_screen(hwnd, x1, y1)
    sx2, sy2 = layout_mod.client_to_screen(hwnd, x2, y2)

    if human:
        from ..human_entropy import mouse as hm

        segs = hm.drag_human(sx1, sy1, sx2, sy2, button=button, precision=precision)
        extra = {"segments": len(segs), "first": segs[0].describe() if segs else None}
    else:
        input_layer.mouse.drag(sx1, sy1, sx2, sy2, button=button)
        extra = {}

    return ActionResult(
        ok=True, method="mouse", mode_used=MODE_PHYSICAL,
        fallback_used=False,
        detail="物理%s拖拽 (%d,%d) → (%d,%d)" % (
            "真人化" if human else "机械", sx1, sy1, sx2, sy2
        ),
        point=(sx2, sy2), hwnd=hwnd, extra=extra,
        duration=time.monotonic() - t0,
    )


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """分发层自检。

    覆盖三样最容易悄悄坏掉的东西：模式管理器的状态机、
    ``lParam`` 的位打包、以及可达性检查的失败分支。
    **不真的点击或输入**，那些在 ``tests/test_phase3_dispatcher.py``。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 模式常量 -----------------------------------------------------------
    _add("MODE_SILENT == 'silent'", MODE_SILENT == "silent", MODE_SILENT)
    _add("MODE_PHYSICAL == 'physical'", MODE_PHYSICAL == "physical",
         MODE_PHYSICAL)

    # -- 模式管理器状态机 ---------------------------------------------------
    saved = get_mode()
    mgr = InteractionModeManager()
    try:
        _add("默认模式是静默", mgr.mode == MODE_SILENT, mgr.mode)

        mgr.set(MODE_PHYSICAL)
        _add("可切到物理", mgr.mode == MODE_PHYSICAL, mgr.mode)

        mgr.set(MODE_SILENT, allow_fallback=True)
        _add("可切回静默并带降级许可",
             mgr.mode == MODE_SILENT and mgr.allow_fallback is True,
             "mode=%s allow_fallback=%s" % (mgr.mode, mgr.allow_fallback))

        # 非法模式必须拒绝
        try:
            mgr.set("telepathy")
            _add("非法模式被拒绝", False, "居然接受了 'telepathy'")
        except Exception as exc:      # noqa: BLE001
            _add("非法模式被拒绝", True, type(exc).__name__)
    finally:
        mgr.set(saved)

    # -- describe_all 结构完整 ----------------------------------------------
    d = describe_modes()
    need = {"current", "mode", "silent", "physical", "modes",
            "allow_fallback", "force_physical_on_silent_fail"}
    _add("describe_modes 字段完整", need <= set(d),
         "缺 %s" % sorted(need - set(d)) if need - set(d) else "7 个字段")
    for key in ("silent", "physical"):
        sub = d.get(key) or {}
        _add("describe[%s] 有机制说明" % key,
             bool(sub.get("mechanism")),
             str(sub.get("mechanism", ""))[:40])

    # -- lParam 位打包 ------------------------------------------------------
    # 低 16 位 x、高 16 位 y
    lp = make_lparam(0x1234, 0x5678)
    _add("lParam 低位是 x", (lp & 0xFFFF) == 0x1234, "0x%08X" % lp)
    _add("lParam 高位是 y", ((lp >> 16) & 0xFFFF) == 0x5678, "0x%08X" % lp)
    # 负坐标必须按 16 位二补数打包，不能污染另一半
    neg = make_lparam(-1, -1)
    _add("lParam 负坐标两段独立",
         (neg & 0xFFFF) == 0xFFFF and ((neg >> 16) & 0xFFFF) == 0xFFFF,
         "0x%08X" % neg)

    # -- 可达性：无效句柄必须抛，不许静默失败（硬约束 #6）-----------------
    try:
        check_window_reachable(0)
        _add("空句柄被拒绝", False, "居然放过了 hwnd=0")
    except Exception as exc:      # noqa: BLE001
        _add("空句柄被拒绝", True, type(exc).__name__)

    try:
        check_window_reachable(0xDEADBEEF)
        _add("死句柄被拒绝", False, "居然放过了 0xDEADBEEF")
    except Exception as exc:      # noqa: BLE001
        _add("死句柄被拒绝", True, type(exc).__name__)

    # -- ActionResult 契约 --------------------------------------------------
    import dataclasses as _dc
    fields = {f.name: f for f in _dc.fields(ActionResult)}
    need_f = {"ok", "method", "mode_used", "fallback_used"}
    _add("ActionResult 必填字段存在", need_f <= set(fields),
         "缺 %s" % sorted(need_f - set(fields)) if need_f - set(fields)
         else "4 个字段")
    with_default = [n for n in need_f
                    if fields[n].default is not _dc.MISSING
                    or fields[n].default_factory is not _dc.MISSING]
    _add("必填字段无默认值（硬约束 #7）", not with_default,
         "有默认值：%s" % with_default if with_default else "4 个全必填")

    # -- 阶段四：执行结果验证字段 ------------------------------------------
    _add("ActionResult 有 verified 字段", "verified" in fields, "verified" in fields)
    _add("verified 默认 None（不验证）",
         fields["verified"].default is None, fields["verified"].default)
    _add("ActionResult 有 verify_elapsed 字段",
         "verify_elapsed" in fields, "verify_elapsed" in fields)

    res = ActionResult(ok=True, method="mouse", mode_used=MODE_PHYSICAL,
                       fallback_used=False)
    _add("未验证时 is_confirmed False", res.is_confirmed() is False, res.is_confirmed())
    _add("未验证时 needs_attention False", res.needs_attention() is False, res.needs_attention())
    _add("describe 带 verified 键", "verified" in res.describe(), list(res.describe()))
    _add("describe verified 为 None", res.describe()["verified"] is None, res.describe()["verified"])

    # 用轻量替身，避免 input 层真的 import verify
    class _VR(object):
        def __init__(self, verdict):
            self.verdict = verdict

        @property
        def ok(self):
            return self.verdict == "ok"

        @property
        def unknown(self):
            return self.verdict == "unknown"

        def to_dict(self):
            return {"verdict": self.verdict}

    res2 = ActionResult(ok=True, method="mouse", mode_used=MODE_PHYSICAL,
                        fallback_used=False, verified=_VR("ok"))
    _add("验证 ok 时 is_confirmed True", res2.is_confirmed() is True, res2.is_confirmed())
    _add("验证 ok 时 needs_attention False",
         res2.needs_attention() is False, res2.needs_attention())
    _add("describe 展开 verified", res2.describe()["verified"]["verdict"]
         == "ok", res2.describe()["verified"])
    _add("__str__ 标出已验证", "已验证" in str(res2), str(res2))

    res3 = ActionResult(ok=True, method="mouse", mode_used=MODE_PHYSICAL,
                        fallback_used=False, verified=_VR("unknown"))
    _add("未知时 is_confirmed False", res3.is_confirmed() is False, res3.is_confirmed())
    _add("未知时 needs_attention True", res3.needs_attention() is True, res3.needs_attention())
    _add("__str__ 标出结果未知", "结果未知" in str(res3), str(res3))

    res4 = ActionResult(ok=True, method="mouse", mode_used=MODE_PHYSICAL,
                        fallback_used=False, verified=_VR("fail"))
    _add("验证失败时 is_confirmed False", res4.is_confirmed() is False, res4.is_confirmed())
    _add("验证失败不算“未知”", res4.needs_attention() is False, res4.needs_attention())

    # -- 验证钩子接入点 ----------------------------------------------------
    spec = _resolve_verify_spec(None, 0, None)
    _add("verify=None 不触发",
         spec[1] is None, spec)
    spec = _resolve_verify_spec(True, 123, "desktop")
    _add("verify=True 用钩子且带兜底判据",
         spec[0] is True and spec[2] is not None, spec[:2])
    spec = _resolve_verify_spec({"mode": "desktop"}, 0, None)
    _add("verify=dict 作为临时钩子",
         spec[1] == {"mode": "desktop"}, spec[1])

    import inspect
    sig = inspect.signature(dispatch_click)
    _add("dispatch_click 有 verify 参数", "verify" in sig.parameters, list(sig.parameters))
    _add("verify 默认 None", sig.parameters["verify"].default is None, sig.parameters["verify"].default)
    _add("dispatch_click 有 verify_mode 参数",
         "verify_mode" in sig.parameters, "verify_mode" in sig.parameters)

    _add("_attach_verify 存在于模块",
         callable(globals().get("_attach_verify")), callable(globals().get("_attach_verify")))

    return {"checks": checks, "failures": failures, "ok": not failures}
