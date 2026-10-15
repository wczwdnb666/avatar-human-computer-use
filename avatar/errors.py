# -*- coding: utf-8 -*-
"""统一异常层级。

设计原则：异常类型要能直接对应"该怎么处理"，而不是"哪个函数报的"。

    AvatarError
    ├─ EnvironmentError     环境不满足（DPI 未生效、无显示器、权限不足）
    │   └─ DpiSetupError
    ├─ TargetError          目标找不到（窗口/元素/图像）
    │   └─ LocateError          元素定位失败 —— 此时**必须拒绝执行**，不许瞎点坐标
    │   └─ WindowNotFound
    ├─ RiskBlocked          风险闸门拦截（唯一合法的人工打断之一）
    ├─ TimeoutError_        等待超时（注意别和内置 TimeoutError 撞名）
    └─ UnsupportedError     当前环境不支持该能力（应触发降级链）
"""
from __future__ import annotations


class AvatarError(Exception):
    """所有 Avatar 异常的基类。"""

    #: 进程退出码，让 CLI 调用方能区分失败原因
    exit_code = 1

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.context = context

    def __str__(self):
        base = super().__str__()
        if self.context:
            extra = ", ".join("%s=%r" % (k, v) for k, v in self.context.items())
            return "%s [%s]" % (base, extra)
        return base


class EnvironmentError_(AvatarError):
    """环境不满足。中文环境下不用内置 EnvironmentError（已废弃别名）。"""

    exit_code = 2


class DpiSetupError(EnvironmentError_):
    """DPI 感知设置失败 —— 高危，因为失败是静默的。"""

    exit_code = 2


class TargetError(AvatarError):
    """目标不存在或不可达。"""

    exit_code = 3


class WindowNotFound(TargetError):
    exit_code = 3


class LocateError(TargetError):
    """元素定位失败。

    这个异常的存在本身就是一条设计约束：**定位不到就不许动**。
    任何"定位失败就用坐标猜一个"的实现都被视为 bug。
    """

    exit_code = 4


class RiskBlocked(AvatarError):
    """被风险检测拦截 —— 对应产品哲学里的"高危确认"。"""

    exit_code = 5


class TimeoutError_(AvatarError):
    exit_code = 6

    def __init__(self, message: str, elapsed: float = 0.0, **context):
        super().__init__(message, elapsed=round(elapsed, 3), **context)
        self.elapsed = elapsed


class UnsupportedError(AvatarError):
    """能力不可用，调用方应沿降级链往下走。"""

    exit_code = 7

    def __init__(self, message: str, capability: str = "", **context):
        super().__init__(message, capability=capability, **context)
        self.capability = capability


class UnsafeStateError(AvatarError):
    """执行前的状态前置条件不满足（前台进程不对、窗口已关闭等）。"""

    exit_code = 8


# --------------------------------------------------------------------------
# 阶段二新增
# --------------------------------------------------------------------------
class ComErrorBase(AvatarError):
    """COM 相关错误的基类。

    真正的 ``ComError`` 在 ``avatar._win32.com`` 里定义（它需要引用
    HRESULT 格式化工具）。这里放基类是为了让上层模块不必导入 com
    就能做 ``except`` 判断。
    """

    exit_code = 9


class UiaUnavailable(AvatarError):
    """UI Automation 不可用（未安装、被策略禁用、公寓模式冲突）。

    这个异常的存在意义是**触发降级链**：捕获它 → 走视觉 / 坐标兜底。
    不允许"UIA 失败就当作元素不存在"。
    """

    exit_code = 10

    def __init__(self, message: str, reason: str = "", **context):
        super().__init__(message, reason=reason or None, **context)
        self.reason = reason


class ElementNotFound(LocateError):
    """元素定位失败。

    ⚠️ 这是本项目最重要的异常之一。它的存在就是为了强制一条规则：

        **定位不到元素，就不许动鼠标。**

    `avatar click` / `avatar set` 这类命令捕获到它必须直接失败退出，
    绝不允许"定位失败就用坐标猜一个"。
    """

    exit_code = 4

    def __init__(self, message: str, query: str = "", hwnd: int = 0, **context):
        super().__init__(message, query=query or None, hwnd=hwnd or None, **context)
        self.query = query
        self.hwnd = hwnd


class ElementStale(LocateError):
    """元素已失效（窗口被关闭、控件被重建、指针悬空）。

    与 ElementNotFound 的区别：元素**曾经**找到过，现在没了。
    调用方通常应该重新查找，而不是直接失败。
    """

    exit_code = 4

    def __init__(self, message: str = "元素已失效，需要重新查找", **context):
        super().__init__(message, **context)


class UipiBlocked(AvatarError):
    """UIPI（用户界面特权隔离）阻止了操作。

    场景：Avatar 以普通权限运行，目标窗口以管理员权限运行 →
    Windows 会**静默丢弃**我们的 SendInput / PostMessage，不报错。

    这是"权限不足"这一类人工打断的**程序化表达**。
    """

    exit_code = 5

    def __init__(self, message: str, self_integrity: str = "",
                 target_integrity: str = "", **context):
        super().__init__(
            message, self_integrity=self_integrity or None,
            target_integrity=target_integrity or None, **context,
        )
        self.self_integrity = self_integrity
        self.target_integrity = target_integrity


class ProcessBlocked(AvatarError):
    """进程操作被白名单拦截。

    默认策略：**只允许关闭自己启动的进程**。要操作其他进程必须显式
    传 ``allow_external=True``，否则抛这个异常。
    """

    exit_code = 5

    def __init__(self, message: str, process: str = "", operation: str = "", **context):
        super().__init__(message, process=process or None,
                         operation=operation or None, **context)
        self.process = process
        self.operation = operation


# --------------------------------------------------------------------------
# 阶段三新增：输入层
# --------------------------------------------------------------------------
class ActionFailed(AvatarError):
    """动作执行失败（注入被拒、目标窗口不可达、事件数量不符）。

    与 ``LocateError`` 的分工：
        - ``LocateError``  —— **还没动手**就定位不到目标
        - ``ActionFailed`` —— 已经要动手了，但系统没接受这次输入

    之所以单独成类而不是复用 ``LocateError``：这两种失败的重试策略
    完全不同。定位失败要重新查找，注入失败要检查权限、前台状态、
    目标窗口可达性 —— 混在一起调用方无法区分。
    """

    exit_code = 4

    def __init__(self, message: str = "动作执行失败", method: str = "", **context):
        super().__init__(message, method=method or None, **context)
        #: 失败时所用的手段（"mouse" / "keyboard" / "postmessage" / "invoke"...）
        self.method = method


class TargetUnreachable(ActionFailed):
    """静默模式的目标窗口不可达（窗口已销毁、已最小化、句柄无效）。

    质量硬约束 #6：静默模式必须先验证目标窗口可达，不可达**直接抛异常**，
    绝不允许静默失败。这个异常就是那条约束的载体。
    """

    exit_code = 3

    def __init__(self, message: str = "目标窗口不可达", hwnd: int = 0,
                 reason: str = "", **context):
        super().__init__(message, method="postmessage", hwnd=hwnd or None,
                         reason=reason or None, **context)
        self.hwnd = hwnd
        self.reason = reason
