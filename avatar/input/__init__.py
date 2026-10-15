# -*- coding: utf-8 -*-
"""输入层：物理模式（SendInput）原语。

本包只做一件事 —— **把"想按什么"翻译成系统真的收到了的输入事件**。
不含真人化（在 ``avatar.human_entropy``），不含静默模式与分发
（在 ``avatar.input.dispatcher``）。

模块划分
--------
``keyboard``
    SendInput 键盘注入。Unicode / 虚拟键码 / 修饰键组合。
``mouse``
    SendInput 鼠标注入。绝对坐标归一化 / 点击 / 滚轮 / 拖拽。
``dispatcher``
    静默（PostMessage）与物理（SendInput）双模式分发，坐标从窗口
    客户区换算到屏幕绝对坐标。**上层的统一入口。**

坐标契约
--------
本包的每个公开函数，只要带 ``x``/``y`` 参数，接收的都是
**物理屏幕绝对坐标**。窗口客户区坐标必须先经
``avatar.app_manager.layout.client_to_screen`` 换算。
"""
from __future__ import annotations

from . import keyboard, mouse

# dispatcher 会 import 本包的 keyboard/mouse，以及 app_manager.layout。
# 放在最后导入避免顺序问题；它的导入开销很小（只绑定了几个常量）。
from . import dispatcher

from .dispatcher import (
    MODE_PHYSICAL,
    MODE_SILENT,
    ActionResult,
    describe_modes,
    dispatch_click,
    dispatch_drag,
    dispatch_key,
    dispatch_move,
    dispatch_scroll,
    dispatch_text,
    get_mode,
    set_interaction_mode,
    set_mode,
)

__all__ = [
    "keyboard",
    "mouse",
    "dispatcher",
    "MODE_PHYSICAL",
    "MODE_SILENT",
    "ActionResult",
    "get_mode",
    "set_mode",
    "set_interaction_mode",
    "describe_modes",
    "dispatch_click",
    "dispatch_text",
    "dispatch_key",
    "dispatch_move",
    "dispatch_scroll",
    "dispatch_drag",
]
