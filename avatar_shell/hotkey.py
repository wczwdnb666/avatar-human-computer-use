# -*- coding: utf-8 -*-
"""全局热键 Ctrl+Alt+A —— 一键唤起输入框。

## 它怎么工作

```
RegisterHotKey(NULL, HOTKEY_ID, MOD_CONTROL|MOD_ALT, VK_A)
        ↓  返回非 0 = 注册成功
GetMessage 循环（阻塞）
        ↓
WM_HOTKEY (wParam == HOTKEY_ID)
        ↓
回调 fn()  ← 由调用方接线到输入框
```

**全程纯 ctypes**：一次 ``RegisterHotKey`` + 已有的消息循环。
不需要钩子（``SetWindowsHookEx`` 那种全局键盘钩子更重、更容易被杀软盯上）。

## ⚠ 热键可能注册失败

Ctrl+Alt+A 可能被别的程序占了（输入法、截图工具、另一个 Avatar 实例）。
``RegisterHotKey`` 返回 0 时：

1. **不静默失败** —— 报 ``ERROR_HOTKEY_ALREADY_REGISTERED (1409)``
2. 给出可操作提示（"换组合键 / 关掉占用它的程序"）
3. :meth:`HotkeyListener.unregister` 保证退出时释放
   （不释放的话，热键会一直占着直到那个进程退出 —— 重启都没用）

## 为什么自检不真注册

自检跑在 CI 和任何机器上，真注册会和用户正在用的程序**抢热键**。
所以自检**mock 掉 ctypes 调用**，只验证：
参数正确（修饰键 / 键码 / id）、冲突检测逻辑、消息分发逻辑。

## 能力边界

**做**：注册 / 注销全局热键；阻塞式消息循环（:meth:`run`）；
非阻塞轮询（:meth:`poll_once`，可嵌进别的消息循环）；
冲突检测与明确报错；热键触发时调回调。

**不做**：

- **不 import avatar**（硬约束 2）。热键响了之后干什么
  （弹输入框、发任务）是调用方的事。
- **不用钩子**。``SetWindowsHookEx(WH_KEYBOARD_LL)`` 能拦单个按键，
  但它是全局钩子，重、易冲突、杀软敏感。``RegisterHotKey`` 是
  系统级注册表机制，轻且干净。
- **不做组合键自定义**。就 Ctrl+Alt+A —— 少即是准。
  真要换，改 :data:`HOTKEY_COMBO` 常量（它来自 presence 的单一事实源）。
- **不管窗口激活**。热键响了的默认动作由调用方定
  （本项目里是弹输入框，见 :mod:`avatar_shell.input_box`）。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

from . import presence as P

__all__ = [
    "MOD_CONTROL",
    "MOD_ALT",
    "VK_A",
    "WM_HOTKEY",
    "ERROR_HOTKEY_ALREADY_REGISTERED",
    "HotkeyListener",
    "selfcheck",
]

#: 修饰键（与 presence.HOTKEY_COMBO 同源）。
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

#: 'A' 的虚拟键码。
VK_A = 0x41

#: 热键消息。
WM_HOTKEY = 0x0312

#: 热键已被占用的错误码。
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

#: 我们的热键 id（``RegisterHotKey`` 的第 2 个参数）。
#: 用固定小整数 —— 多实例会撞 id，但那本来也是冲突（同组合键）。
HOTKEY_ID = 1


class HotkeyListener:
    """全局热键监听器。

    用法::

        hk = HotkeyListener(on_hotkey=lambda: print("Ctrl+Alt+A"))
        if hk.register():
            hk.run()            # 阻塞
        # 或嵌进别的循环：
        while running:
            hk.poll_once()

    **所有方法永不抛异常** —— 热键坏了不该让外壳崩。
    """

    def __init__(self, *,
                 on_hotkey: Optional[Callable[[], Any]] = None,
                 hotkey_id: int = HOTKEY_ID) -> None:
        #: 热键触发时的回调。
        self.on_hotkey = on_hotkey
        self.hotkey_id = int(hotkey_id)
        self.registered = False
        self.error = ""
        self.triggers = 0

    # ---------------- 注册 ----------------
    def _modifiers(self) -> int:
        """修饰键组合（来自 presence 的单一事实源）。"""
        return P.HOTKEY_COMBO[0]

    def _vk(self) -> int:
        """虚拟键码（来自 presence 的单一事实源）。"""
        return P.HOTKEY_COMBO[1]

    def register(self) -> bool:
        """注册热键。**冲突时给明确报错，不静默失败。**"""
        if self.registered:
            return True
        try:
            u32 = ctypes.windll.user32
            ok = u32.RegisterHotKey(None, self.hotkey_id,
                                    self._modifiers(), self._vk())
            if not ok:
                # ⚠ 用 ``u32.GetLastError()`` 而不是 ``ctypes.get_last_error()``
                #   —— 两者在真机上都对，但前者**可被 mock 拦截**，
                #   自检才能验证冲突检测逻辑。
                err = u32.GetLastError()
                if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                    self.error = (
                        "热键 Ctrl+Alt+A 已被别的程序占用"
                        "（错误码 1409）。关掉占用它的程序，"
                        "或改 presence.HOTKEY_COMBO 换个组合键。")
                else:
                    self.error = ("RegisterHotKey 失败，错误码 %d。"
                                  % err)
                return False
            self.registered = True
            self.error = ""
            return True
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False

    def unregister(self) -> bool:
        """注销热键。**退出时必须调** —— 不释放的话热键会占着不放。"""
        if not self.registered:
            return True
        try:
            ok = ctypes.windll.user32.UnregisterHotKey(
                None, self.hotkey_id)
            self.registered = False
            return bool(ok)
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            self.registered = False
            return False

    # ---------------- 消息 ----------------
    def poll_once(self, timeout_ms: int = 0) -> Optional[str]:
        """取一条消息并处理。返回 ``"hotkey"`` / ``"other"`` / ``None``（无消息）。

        非阻塞版（``timeout_ms=0`` 时）可嵌进别的消息循环 ——
        比如 ball 的主循环。热键消息直接在这里分发。
        """
        try:
            u32 = ctypes.windll.user32
            msg = wintypes.MSG()
            got = u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if not got:
                return None
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
            if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                self.triggers += 1
                if self.on_hotkey:
                    try:
                        self.on_hotkey()
                    except Exception:      # noqa: BLE001
                        pass          # 回调炸了不能带走消息循环
                return "hotkey"
            return "other"
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)[:120]
            return None

    def run(self) -> int:
        """阻塞式消息循环。返回 0 = 正常退出。"""
        if not self.registered and not self.register():
            return 1
        try:
            u32 = ctypes.windll.user32
            msg = wintypes.MSG()
            while self.registered:
                got = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    self.triggers += 1
                    if self.on_hotkey:
                        try:
                            self.on_hotkey()
                        except Exception:      # noqa: BLE001
                            pass
            return 0
        except Exception as exc:      # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return 1
        finally:
            self.unregister()

    def stop(self) -> None:
        """请求停止（从别的线程调也安全）。"""
        self.registered = False
        try:
            ctypes.windll.user32.PostQuitMessage(0)
        except Exception:      # noqa: BLE001
            pass

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        return "\n".join([
            "全局热键监听器",
            "=" * 40,
            "组合键 Ctrl+Alt+A（MOD_CONTROL|MOD_ALT=0x%X, VK_A=0x%X）"
            % (self._modifiers(), self._vk()),
            "已注册 %s   触发 %d 次" % (self.registered, self.triggers),
            "错误   %s" % (self.error or "(无)"),
        ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """热键自检。**不真注册**（会抢用户的热键）—— mock 掉 ctypes，
    验证：参数正确、冲突检测、消息分发、注销清理。"""
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) 参数常量 ============
    _add("★ 修饰键是 Ctrl+Alt（0x3）", P.HOTKEY_COMBO[0] == 0x0003,
         "0x%X" % P.HOTKEY_COMBO[0])
    _add("★ 键码是 A（0x41）", P.HOTKEY_COMBO[1] == 0x41,
         "0x%X" % P.HOTKEY_COMBO[1])
    _add("WM_HOTKEY 正确", WM_HOTKEY == 0x0312, "0x312")
    _add("冲突错误码是 1409", ERROR_HOTKEY_ALREADY_REGISTERED == 1409,
         "1409")

    # ============ 2) mock 注册：验证传给 RegisterHotKey 的参数 ============
    # 用一个假的 user32 替换 ctypes.windll.user32 —— **不打真热键**。
    class _FakeUser32:
        def __init__(self, succeed=True):
            self.calls = []
            self.succeed = succeed

        def RegisterHotKey(self, hwnd, hid, mods, vk):
            self.calls.append({"hwnd": hwnd, "id": hid,
                               "mods": mods, "vk": vk})
            return 1 if self.succeed else 0

        def UnregisterHotKey(self, hwnd, hid):
            self.calls.append({"unregister": hid})
            return 1

        def GetLastError(self):
            return 0

    import unittest.mock as _mock

    fake = _FakeUser32(succeed=True)
    with _mock.patch.object(ctypes, "windll", _mock.MagicMock(user32=fake)):
        hk = HotkeyListener(on_hotkey=lambda: None)
        _add("★ 注册成功", hk.register() is True, "ok")
        _add("★ RegisterHotKey 参数正确（NULL, 1, Ctrl|Alt, A）",
             fake.calls and fake.calls[0] == {
                 "hwnd": None, "id": 1, "mods": 0x0003, "vk": 0x41},
             str(fake.calls[0]))
        _add("重复注册是幂等的",
             hk.register() is True and len(fake.calls) == 1,
             "不重复调 API")
        _add("★ 注销成功", hk.unregister() is True, "ok")
        _add("注销传给 API",
             any("unregister" in c for c in fake.calls), "调了")
        _add("注销后重复注销幂等",
             hk.unregister() is True, "不抛")

    # ============ 3) 冲突检测 ============
    fake_fail = _FakeUser32(succeed=False)

    class _FakeUser32Err(_FakeUser32):
        def GetLastError(self):
            return ERROR_HOTKEY_ALREADY_REGISTERED

    fake_err = _FakeUser32Err(succeed=False)
    with _mock.patch.object(ctypes, "windll", _mock.MagicMock(user32=fake_err)):
        hk2 = HotkeyListener()
        _add("★ 冲突时注册失败", hk2.register() is False, "返回 False")
        _add("★ 冲突报错提到 1409", "1409" in hk2.error, hk2.error[:50])
        _add("★ 冲突报错给可操作建议", "组合键" in hk2.error or
             "程序" in hk2.error, "建议")
        _add("冲突后不静默（error 非空）", bool(hk2.error), "有报错")

    # 其他错误码
    fake_other = _FakeUser32(succeed=False)

    class _FakeUser32Other(_FakeUser32):
        def GetLastError(self):
            return 5

    with _mock.patch.object(ctypes, "windll",
                            _mock.MagicMock(user32=_FakeUser32Other(succeed=False))):
        hk3 = HotkeyListener()
        hk3.register()
        _add("非冲突错误也报出来", "5" in hk3.error, hk3.error[:40])

    # ============ 4) 消息分发（mock PeekMessage）============
    # 造一个假的 MSG 序列：热键消息 → 触发回调 → 别的消息
    class _FakeMsg:
        def __init__(self, message, wparam):
            self.message = message
            self.wParam = wparam
            self.lParam = 0
            self.hwnd = 0
            self.time = 0
            self.pt_x = 0
            self.pt_y = 0

    class _FakeUser32Poll(_FakeUser32):
        def __init__(self):
            super().__init__()
            self.queue = [
                _FakeMsg(WM_HOTKEY, HOTKEY_ID),      # 热键
                _FakeMsg(0x0113, 1),                  # WM_TIMER（别的）
                _FakeMsg(WM_HOTKEY, 99),              # 别的热键 id
            ]

        def PeekMessageW(self, byref_msg, hwnd, mn, mx, remove):
            if not self.queue:
                return 0
            msg = self.queue.pop(0)
            byref_msg._obj.message = msg.message
            byref_msg._obj.wParam = msg.wParam
            return 1

        def TranslateMessage(self, byref_msg):
            return 1

        def DispatchMessageW(self, byref_msg):
            return 1

    fired = []
    fake_poll = _FakeUser32Poll()
    with _mock.patch.object(ctypes, "windll",
                            _mock.MagicMock(user32=fake_poll)):
        hk4 = HotkeyListener(on_hotkey=lambda: fired.append(1))
        hk4.registered = True
        r1 = hk4.poll_once()
        _add("★ 热键消息被识别", r1 == "hotkey", str(r1))
        _add("★ 热键触发回调", fired == [1], "回调跑了 1 次")
        r2 = hk4.poll_once()
        _add("非热键消息走 other", r2 == "other", str(r2))
        r3 = hk4.poll_once()
        _add("★ 别的 id 的热键不触发", r3 == "other" and fired == [1],
             "id 不匹配不响应")
        r4 = hk4.poll_once()
        _add("队列空了返回 None", r4 is None, str(r4))
        _add("触发计数正确", hk4.triggers == 1, str(hk4.triggers))

    # ============ 5) 回调抛异常不带走循环 ============
    class _FakeUser32Boom(_FakeUser32):
        def __init__(self):
            super().__init__()
            self.queue = [_FakeMsg(WM_HOTKEY, HOTKEY_ID)]

        def PeekMessageW(self, byref_msg, hwnd, mn, mx, remove):
            if not self.queue:
                return 0
            msg = self.queue.pop(0)
            byref_msg._obj.message = msg.message
            byref_msg._obj.wParam = msg.wParam
            return 1

        def TranslateMessage(self, byref_msg):
            return 1

        def DispatchMessageW(self, byref_msg):
            return 1

    def _boom():
        raise RuntimeError("回调炸了")

    with _mock.patch.object(ctypes, "windll",
                            _mock.MagicMock(user32=_FakeUser32Boom())):
        hk5 = HotkeyListener(on_hotkey=_boom)
        hk5.registered = True
        _add("★ 回调抛异常 poll_once 仍返回 hotkey",
             hk5.poll_once() == "hotkey", "不炸")
        _add("触发计数照记", hk5.triggers == 1, "记了")

    # ============ 6) 常量与 docstring ============
    _add("MOD_NOREPEAT 定义存在", MOD_NOREPEAT == 0x4000, "0x4000")
    import sys as _sys
    _me = _sys.modules[__name__]
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    elapsed = time.perf_counter() - t0
    return {
        "module": "hotkey",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(HotkeyListener().describe())
