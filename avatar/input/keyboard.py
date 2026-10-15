# -*- coding: utf-8 -*-
"""键盘注入：SendInput 物理按键，支持 Unicode / 虚拟键码 / 修饰键组合。

设计约定
--------
1. **物理模式专有**。本模块只负责"真按键盘"。静默模式（PostMessage）
   不在这里，见 ``avatar/input/dispatcher.py``。
2. **Unicode 优先**。输入任意文本时优先走 ``KEYEVENTF_UNICODE``，绕开
   键盘布局（俄语/日语布局下 ``VkKeyScanW`` 出来的键码是错的）。
3. **修饰键与主键分离**。``hotkey`` 先按下所有修饰键、再敲主键、最后
   逆序抬起 —— 顺序错了快捷键不生效（Ctrl+Shift+S 必须先 Ctrl 后 Shift）。
4. **不吞异常**。SendInput 返回 0 一律抛 ``ActionFailed``，因为返回 0
   通常意味着被 UIPI / 安全桌面拦截，静默失败会让调用方误以为成功。
"""
from __future__ import annotations

import ctypes
import time
from typing import Iterable, Sequence

from .._win32 import structs as S
from .._win32.api import last_error, user32, win32_error_text
from ..errors import ActionFailed

__all__ = [
    "VK_MAP",
    "MODIFIER_VKS",
    "vk_for_key",
    "key_down",
    "key_up",
    "press",
    "hotkey",
    "type_text",
    "type_vk_sequence",
    "send_keyevent",
    "get_key_state",
    "is_key_pressed",
    "get_keyboard_layout",
]


# --------------------------------------------------------------------------
# 虚拟键码表
#
# 只列"有名字的"键。字符键（A-Z / 0-9）由 ``vk_for_key`` 直接算出来，
# 不写进表里 —— 写了反而容易和实际布局冲突。
# --------------------------------------------------------------------------
VK_MAP = {
    # 控制键
    "backspace": 0x08,
    "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "caps": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "select": 0x29,
    "print": 0x2A,
    "execute": 0x2B,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "help": 0x2F,
    # 修饰键（左右区分）
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "lcontrol": 0xA2,
    "rctrl": 0xA3,
    "rcontrol": 0xA3,
    "lalt": 0xA4,
    "lmenu": 0xA4,
    "ralt": 0xA5,
    "rmenu": 0xA5,
    # Windows 键
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    # 小键盘
    "num0": 0x60,
    "num1": 0x61,
    "num2": 0x62,
    "num3": 0x63,
    "num4": 0x64,
    "num5": 0x65,
    "num6": 0x66,
    "num7": 0x67,
    "num8": 0x68,
    "num9": 0x69,
    "multiply": 0x6A,
    "add": 0x6B,
    "separator": 0x6C,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numlock": 0x90,
    "scrolllock": 0x91,
    # 功能键
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "f13": 0x7C,
    "f14": 0x7D,
    "f15": 0x7E,
    "f16": 0x7F,
    "f17": 0x80,
    "f18": 0x81,
    "f19": 0x82,
    "f20": 0x83,
    "f21": 0x84,
    "f22": 0x85,
    "f23": 0x86,
    "f24": 0x87,
    # 浏览器 / 多媒体（部分键盘）
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
    "browser_refresh": 0xA8,
    "browser_stop": 0xA9,
    "browser_search": 0xAA,
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "media_next": 0xB0,
    "media_prev": 0xB1,
    "media_stop": 0xB2,
    "media_play_pause": 0xB3,
    # 符号键（US 布局下，供显式指定用）
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}


# 修饰键集合：hotkey 需要知道哪些是"按住不放"的
MODIFIER_VKS = frozenset(
    {
        0x10,  # SHIFT
        0x11,  # CONTROL
        0x12,  # MENU / ALT
        0x5B,  # LWIN
        0x5C,  # RWIN
        0xA0,  # LSHIFT
        0xA1,  # RSHIFT
        0xA2,  # LCONTROL
        0xA3,  # RCONTROL
        0xA4,  # LMENU
        0xA5,  # RMENU
    }
)


# 需要带 KEYEVENTF_EXTENDEDKEY 的键（右手区 + 小键盘以外的导航键）
_EXTENDED_VKS = frozenset(
    {
        0x21,  # PGUP
        0x22,  # PGDN
        0x23,  # END
        0x24,  # HOME
        0x25,  # LEFT
        0x26,  # UP
        0x27,  # RIGHT
        0x28,  # DOWN
        0x2D,  # INSERT
        0x2E,  # DELETE
        0x5B,  # LWIN
        0x5C,  # RWIN
        0x5D,  # APPS
        0x6F,  # DIVIDE
        0x90,  # NUMLOCK
        0xA3,  # RCONTROL
        0xA5,  # RMENU
        0x2C,  # PRINTSCREEN
    }
)


def vk_for_key(key) -> int:
    """把 ``"ctrl"`` / ``"enter"`` / ``"a"`` / ``"F5"`` 转成虚拟键码。

    非法输入抛 ``ActionFailed`` —— 键盘动作失败属于动作层错误（退出码 4），
    不是调用方参数错（那是 TypeError）。
    """
    if isinstance(key, int):
        if not (0 <= key <= 0xFF):
            raise ActionFailed("虚拟键码越界：%r" % (key,), method="keyboard")
        return key

    if not isinstance(key, str):
        raise ActionFailed("无法识别的按键类型：%r" % (key,), method="keyboard")

    k = key.strip().lower()
    if not k:
        raise ActionFailed("按键名为空", method="keyboard")

    if k in VK_MAP:
        return VK_MAP[k]

    # 单字符：A-Z / 0-9 直接算；符号优先查表（已在上方 VK_MAP 里）
    if len(k) == 1:
        ch = k.upper()
        if "A" <= ch <= "Z":
            return ord(ch)
        if "0" <= ch <= "9":
            return ord(ch)
        # 未在表里的符号：让 Win32 按当前布局去解
        vk_scan = user32.VkKeyScanW(k)
        if vk_scan != -1:
            return vk_scan & 0xFF

    # "f1" 之类已在表里；"num1" 也在表里。到这里说明真不认识
    raise ActionFailed("未知按键名：%r" % (key,), method="keyboard")


def get_keyboard_layout() -> int:
    """取当前线程的前台键盘布局 HKL。"""
    return user32.GetKeyboardLayout(0)


def _make_key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> S.INPUT:
    """构造一个 INPUT_KEYBOARD 事件。"""
    inp = S.INPUT()
    inp.type = S.INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def send_keyevent(inputs: Sequence[S.INPUT]) -> int:
    """把一批事件交给 SendInput，返回成功注入的事件数。

    失败（返回 0）直接抛 ``ActionFailed``，并带上 GetLastError 文本。
    """
    if not inputs:
        return 0
    n = len(inputs)
    arr = (S.INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(S.INPUT))
    if sent != n:
        err = last_error()
        raise ActionFailed(
            "SendInput 键盘事件注入失败：期望 %d 个，实际 %d 个（错误 %d：%s）"
            % (n, sent, err, win32_error_text(err)),
            method="keyboard",
            expected=n,
            sent=sent,
            last_error=err,
        )
    return sent


def _flags_for(vk: int, *, up: bool, unicode_mode: bool = False) -> int:
    flags = 0
    if vk in _EXTENDED_VKS:
        flags |= S.KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= S.KEYEVENTF_KEYUP
    if unicode_mode:
        flags |= S.KEYEVENTF_UNICODE
    return flags


def key_down(key) -> None:
    """按下按键（不抬起）。"""
    vk = vk_for_key(key)
    send_keyevent([_make_key_input(vk=vk, flags=_flags_for(vk, up=False))])


def key_up(key) -> None:
    """抬起按键。"""
    vk = vk_for_key(key)
    send_keyevent([_make_key_input(vk=vk, flags=_flags_for(vk, up=True))])


def press(key, *, hold: float = 0.0) -> None:
    """按一下（按下 + 抬起）。

    ``hold`` 是按住时长（秒）。真人按键的 hold 一般在 30-90ms。
    """
    vk = vk_for_key(key)
    down = _make_key_input(vk=vk, flags=_flags_for(vk, up=False))
    up = _make_key_input(vk=vk, flags=_flags_for(vk, up=True))
    send_keyevent([down])
    if hold > 0:
        time.sleep(hold)
    send_keyevent([up])


def hotkey(keys: Iterable, *, hold: float = 0.02) -> None:
    """组合键：按下全部修饰键 → 敲主键 → 逆序抬起。

    例：``hotkey(["ctrl", "shift", "s"])``

    注意顺序：修饰键必须**先于**主键按下且**后于**主键抬起，
    否则系统会当成"按下了 Ctrl+S，又按了 Shift"。
    """
    seq = list(keys)
    if not seq:
        raise ActionFailed("hotkey 至少需要一个按键", method="keyboard")

    vks = [vk_for_key(k) for k in seq]

    # 前面所有非修饰键也当作"按住不放"，最后一个是主键
    held = vks[:-1]
    main = vks[-1]

    down_events = []
    for vk in held:
        down_events.append(_make_key_input(vk=vk, flags=_flags_for(vk, up=False)))
    down_events.append(_make_key_input(vk=main, flags=_flags_for(main, up=False)))

    up_events = []
    up_events.append(_make_key_input(vk=main, flags=_flags_for(main, up=True)))
    for vk in reversed(held):
        up_events.append(_make_key_input(vk=vk, flags=_flags_for(vk, up=True)))

    send_keyevent(down_events)
    if hold > 0:
        time.sleep(hold)
    send_keyevent(up_events)


def type_text(text: str, *, inter_key_delay: float = 0.0) -> int:
    """用 ``KEYEVENTF_UNICODE`` 逐字符输入文本。

    返回注入的字符数。

    为什么走 UNICODE 而不是 VkKeyScan：
        ``VkKeyScanW`` 依赖当前键盘布局。用户切成俄语/日语布局时，
        中文文本会被映射成一堆莫名其妙的键码。UNICODE 模式直接把
        字符码喂给系统，布局无关。

    ``inter_key_delay`` 是字符之间的固定间隔（秒）。真人化打字不要用
    这个参数 —— 那是 ``human_entropy.keyboard`` 的活；这里只提供最
    朴素的固定节奏，供程序化输入和测试使用。
    """
    if not text:
        return 0

    total = 0
    for ch in text:
        code = ord(ch)

        # BMP 内（<=0xFFFF）一个事件对就够；超出 BMP 需要代理对
        if code > 0xFFFF:
            code -= 0x10000
            units = [
                0xD800 + (code >> 10),
                0xDC00 + (code & 0x3FF),
            ]
        else:
            units = [code]

        events = []
        for unit in units:
            # KEYEVENTF_UNICODE 时 wVk 必须为 0，字符码填在 wScan
            events.append(
                _make_key_input(
                    vk=0,
                    scan=unit,
                    flags=S.KEYEVENTF_UNICODE,
                )
            )
            events.append(
                _make_key_input(
                    vk=0,
                    scan=unit,
                    flags=S.KEYEVENTF_UNICODE | S.KEYEVENTF_KEYUP,
                )
            )

        send_keyevent(events)
        total += 1
        if inter_key_delay > 0:
            time.sleep(inter_key_delay)

    return total


def type_vk_sequence(
    keys: Sequence,
    *,
    inter_key_delay: float = 0.0,
) -> int:
    """按虚拟键码序列逐键敲击（走 VK 而非 UNICODE）。

    用于需要触发快捷键语义的场合，例如 ``["ctrl", "c"]`` 每个元素
    都是一个 hotkey 组。
    """
    total = 0
    for item in keys:
        if isinstance(item, (list, tuple)):
            hotkey(item)
        else:
            press(item)
        total += 1
        if inter_key_delay > 0:
            time.sleep(inter_key_delay)
    return total


def get_key_state(vk: int) -> int:
    """取按键当前状态（含当前线程队列状态）。"""
    return user32.GetKeyState(vk)


def is_key_pressed(key) -> bool:
    """``GetAsyncKeyState`` 判定按键是否物理按下（最高位）。"""
    vk = vk_for_key(key)
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """键盘输入层自检（**只做只读查询，不真的按键**）。

    ``send_keyevent`` 会真的往系统里注入事件，所以自检只覆盖：
    映射表完整性、常量值、只读状态查询。真实注入由
    ``tests/test_phase3_input.py`` 覆盖。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 映射表 ------------------------------------------------------------
    _add("VK_MAP 非空", len(VK_MAP) > 40, "%d 个键名" % len(VK_MAP))
    # MODIFIER_VKS 存的是 VK 整数，不是键名：
    # 0x10/0x11/0x12 是通用 SHIFT/CTRL/ALT，0x5B/0x5C 是两个 WIN，
    # 0xA0-0xA5 是左右各自的 SHIFT/CTRL/ALT。
    _add("修饰键 VK 集合齐全",
         {0x10, 0x11, 0x12} <= MODIFIER_VKS and {0x5B, 0x5C} <= MODIFIER_VKS,
         "%d 个修饰 VK" % len(MODIFIER_VKS))
    _add("修饰键含左右变体",
         {0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5} <= MODIFIER_VKS,
         "6 个左右变体")
    miss = [k for k in ("a", "z", "0", "9", "f1", "f12", "enter", "esc",
                        "tab", "space", "backspace", "delete", "left",
                        "right", "up", "down")
            if vk_for_key(k) is None]
    _add("常用键名可解析", not miss, "缺 %s" % miss if miss else "16 个键名 OK")

    # -- 大小写不敏感 -------------------------------------------------------
    _add("键名大小写不敏感",
         vk_for_key("ENTER") == vk_for_key("enter"),
         "ENTER == enter")

    # -- 不支持的形式必须明确拒绝 -------------------------------------------
    bad = 0
    for probe in ("0x5A", "不存在的键名", ""):
        try:
            vk_for_key(probe)
        except Exception:
            bad += 1
    _add("非法键名被拒绝", bad == 3, "%d/3 抛出" % bad)

    # -- 常量值 -------------------------------------------------------------
    from .._win32 import structs as S
    _add("KEYEVENTF_EXTENDEDKEY == 1",
         S.KEYEVENTF_EXTENDEDKEY == 1, str(S.KEYEVENTF_EXTENDEDKEY))
    _add("KEYEVENTF_KEYUP == 2",
         S.KEYEVENTF_KEYUP == 2, str(S.KEYEVENTF_KEYUP))
    _add("KEYEVENTF_UNICODE == 4",
         S.KEYEVENTF_UNICODE == 4, str(S.KEYEVENTF_UNICODE))
    _add("INPUT_KEYBOARD == 1",
         S.INPUT_KEYBOARD == 1, str(S.INPUT_KEYBOARD))

    # -- 键盘布局可读 -------------------------------------------------------
    try:
        hkl = get_keyboard_layout()
        _add("键盘布局句柄可读", isinstance(hkl, int) and hkl != 0,
             "HKL=0x%08X" % hkl)
    except Exception as exc:      # noqa: BLE001
        _add("键盘布局句柄可读", False, "%s: %s" % (type(exc).__name__, exc))

    # -- 只读状态查询 -------------------------------------------------------
    try:
        pressed = is_key_pressed("shift")
        _add("is_key_pressed 可读", isinstance(pressed, bool),
             "shift=%s" % pressed)
    except Exception as exc:      # noqa: BLE001
        _add("is_key_pressed 可读", False, "%s: %s" % (type(exc).__name__, exc))

    return {"checks": checks, "failures": failures, "ok": not failures}
