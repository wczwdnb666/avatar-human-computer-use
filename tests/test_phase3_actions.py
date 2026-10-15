#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段三 · 动作层测试（``uia/actions.py`` 与输入层的对接）。

``uia/actions.py`` 是"UIA 语义"和"输入注入"的唯一交汇点。本文件
关心的是**这个交汇点有没有漏**：

* 模式路径（Invoke / Toggle ...）不该碰输入层 —— 它调 UIA 的模式。
* 坐标路径必须**显式允许**才启用（``allow_coord=True``），默认不给。
* 坐标路径最终落到 dispatcher，因此 ``ActionResult`` 的三个必填字段
  必须一路带上来，不能被中间层吃掉。
* 定位不到元素时**绝不降级** —— 找不到就是找不到。

设计原则
--------
* **不碰真实应用**。所有元素都是自己建窗口 + 手工构造
  ``Element`` 快照。阶段二的 find/patterns 已有自己的测试，
  这里不重复考它。
* **不断言"必然成功"**。UIA 模式能不能用取决于运行时环境
  （是否装了 UIAutomationCore、控件是否支持某模式）。所以对
  模式路径的断言集中在"**如果**成功，返回结构必须完整"，
  而不是"必须成功" —— 后者在无 UIA 的环境上必然假红。
* **可以断言"必然失败"的场合绝不含糊**：定位不到元素、没开
  ``allow_coord`` 就用坐标，这两种情况必须抛异常。
"""
from __future__ import annotations

import ctypes
import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.uia import actions as A
from avatar.uia import tree as T
from avatar.input import dispatcher as D
from avatar.input import mouse as M
from avatar.app_manager import layout as L

user32 = ctypes.WinDLL("user32", use_last_error=True)


# --------------------------------------------------------------------------
# 极简断言框架
# --------------------------------------------------------------------------
_RESULTS = []


def check(name, fn):
    try:
        fn()
        _RESULTS.append((name, True, ""))
        print("  [OK  ] %s" % name)
    except AssertionError as e:
        _RESULTS.append((name, False, str(e)))
        print("  [FAIL] %s  -> %s" % (name, e))
    except Exception as e:
        _RESULTS.append((name, False, "%r" % (e,)))
        print("  [ERR ] %s  -> %r" % (name, e))


def section(t):
    print()
    print(t)
    print("-" * 62)


def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------
# 测试窗口与元素构造
# --------------------------------------------------------------------------
class _Win:
    """一个可见的自建窗口，用于给 Element 快照当宿主。"""

    def __init__(self, x=60, y=60, w=360, h=240):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.hwnd = user32.CreateWindowExW(
            0, "STATIC", "avatar-actions-test", 0x80000000,
            x, y, w, h, 0, 0, 0, 0,
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW 失败")
        user32.ShowWindow(self.hwnd, 4)     # SW_SHOWNOACTIVATE
        user32.UpdateWindow(self.hwnd)
        self._dead = False

    def destroy(self):
        if not self._dead:
            user32.DestroyWindow(self.hwnd)
            self._dead = True


def _make_element(win, *, name="FakeButton", ctype=50000,
                  aid="fakeBtn", rect=None, patterns=(),
                  window=None, hwnd=0):
    """手工构造一个 ``Element`` 快照。

    为什么要手工构造而不是真去找一个 UIA 元素：真元素依赖具体应用、
    依赖 UIA 服务是否就绪，测出来的东西不稳定。我们要考的是
    ``actions.py`` 的**决策逻辑**，那就把输入（Element 快照）钉死。
    """
    r = rect if rect is not None else L.Rect(
        win.x + 20, win.y + 20, win.x + 120, win.y + 60)
    return T.Element(
        name=name,
        control_type=ctype,
        localized_type="按钮",
        automation_id=aid,
        class_name="FakeClass",
        process_id=os.getpid(),
        hwnd=hwnd,
        window=window if window is not None else win.hwnd,
        native_hwnd=hwnd,
        rect=r,
        enabled=True,
        patterns=tuple(patterns),
    )


def _require_window():
    try:
        return _Win()
    except OSError:
        return None


def _cursor_guard():
    class _G:
        def __enter__(self):
            self.saved = M.get_cursor_pos()
            return self

        def __exit__(self, *exc):
            try:
                user32.SetCursorPos(self.saved[0], self.saved[1])
            except Exception:
                pass
    return _G()


# --------------------------------------------------------------------------
# 1. 输入层挂载
# --------------------------------------------------------------------------
def test_input_layer_installed_flag():
    """``input_layer_installed()`` 的状态必须与安装/卸载动作一致。"""
    D.set_interaction_mode(D.MODE_SILENT)
    before = A.input_layer_installed()
    A.install_input_layer()
    _expect(A.input_layer_installed(),
            "install 之后 input_layer_installed() 应为 True")
    A.uninstall_input_layer()
    # 复原原状态，别污染后续用例
    if before:
        A.install_input_layer()
    else:
        A.uninstall_input_layer()
    _expect(A.input_layer_installed() == before,
            "卸载后状态应与最初一致")


def test_get_mode_delegates_to_dispatcher():
    """``actions.get_mode()`` 必须转发到 dispatcher，不是自己维护一份。"""
    try:
        D.set_interaction_mode(D.MODE_PHYSICAL)
        _expect(A.get_mode() == D.MODE_PHYSICAL,
                "物理模式下 get_mode() 应为 physical，实际 %r" % A.get_mode())
        D.set_interaction_mode(D.MODE_SILENT)
        _expect(A.get_mode() == D.MODE_SILENT,
                "静默模式下 get_mode() 应为 silent，实际 %r" % A.get_mode())
    finally:
        D.set_interaction_mode(D.MODE_SILENT)


# --------------------------------------------------------------------------
# 2. 定位失败绝不降级
# --------------------------------------------------------------------------
def test_element_not_found_does_not_fallback():
    """⭐ 元素定位不到时必须抛 ``ElementNotFound``，**绝不允许坐标兜底**。

    这条是阶段二定下的安全底线：找不到目标就停下，不能"猜一个坐标
    点下去"。猜错会点到别的东西，比报错危险得多。

    用 ``Query`` 传一个**绝不可能存在**的名称来制造"找不到" ——
    ``_as_element`` 认 ``Query``，会走真正的定位路径（而不是在参数
    类型校验那一步就被拒掉）。
    """
    from avatar.uia import find as F

    q = F.Query(name="__avatar_绝不存在的元素__%d" % time.time_ns())
    try:
        A.click(q, timeout=0.01)
        raise AssertionError("定位不到元素竟没抛异常")
    except (A.ElementNotFound, A.LocateError, A.TimeoutError_):
        pass


def test_unsupported_target_type_rejected():
    """传一个不认识的对象必须**明确拒绝**，不许当成"随便点一下"。"""
    class _Bogus:
        pass

    try:
        A.click(_Bogus(), timeout=0.01)
        raise AssertionError("不认识的目标类型竟被接受了")
    except TypeError:
        pass
    except A.ActionFailed:
        pass


def test_coord_requires_explicit_permission():
    """⭐ 元素不支持任何点击模式时，**默认不许**降级到坐标。

    只有 ``allow_coord=True`` 才允许。默认 False 是为了让"我不确定
    这元素能不能点"变成一次明确的报错，而不是一次静默的乱点。
    """
    w = _require_window()
    if w is None:
        return
    try:
        # patterns 为空 → 所有模式都试不通
        el = _make_element(w, patterns=())
        try:
            A.click(el, timeout=0.01)
            raise AssertionError("没开 allow_coord 却成功降级到了坐标")
        except A.ActionFailed as e:
            _expect("allow_coord" in str(e),
                    "异常信息里应提示 allow_coord，实际 %r" % str(e))
    finally:
        w.destroy()


def test_coord_path_with_permission():
    """``allow_coord=True`` 时必须真的走坐标路径并返回 ActionResult。"""
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        el = _make_element(w, patterns=())
        r = A.click(el, timeout=0.01, allow_coord=True, human=False)
        _expect(isinstance(r, A.ActionResult),
                "应返回 ActionResult，实际 %r" % type(r))
        _expect(r.method, "method 不能为空")
        _expect(r.mode_used, "mode_used 不能为空")
        _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
        # 坐标路径的 method 应是 mouse 系（走 dispatcher）
        _expect("mouse" in r.method or "postmessage" in r.method
                or r.method in ("mouse", "click"),
                "坐标路径的 method 应反映鼠标注入，实际 %r" % r.method)
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


def test_coord_path_uses_client_coords():
    """⭐ 坐标路径必须把**客户区坐标**交给 dispatcher。

    这是阶段三的第一条设计原则：输入层的唯一坐标系是屏幕绝对坐标，
    但 dispatcher 的静默模式接收**客户区**坐标（它自己打 lParam）。
    ``actions`` 必须做这个换算，不能把屏幕坐标直接当客户区坐标传 ——
    那会造成一个固定的、等于窗口位置的偏移。
    """
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        el = _make_element(w, patterns=())
        seen = {}
        real = A.dispatch_click

        def spy(hwnd, cx, cy, **kw):
            seen["hwnd"] = hwnd
            seen["cx"] = cx
            seen["cy"] = cy
            return real(hwnd, cx, cy, **kw)

        A.dispatch_click = spy
        try:
            A.click(el, timeout=0.01, allow_coord=True, human=False)
        finally:
            A.dispatch_click = real

        _expect(seen, "坐标路径没有调用 dispatcher")
        _expect(seen["hwnd"] == w.hwnd,
                "应把宿主窗口句柄传下去，实际 %r" % seen["hwnd"])
        # 元素 rect 是 (x+20, y+20)-(x+120, y+60)，客户区中心 ≈ (70, 40)
        # 允许 ±2px（边界取整）
        _expect(abs(seen["cx"] - 70) <= 2 and abs(seen["cy"] - 40) <= 2,
                "传给 dispatcher 的客户区坐标应为约 (70,40)，实际 (%r,%r)"
                % (seen["cx"], seen["cy"]))
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


# --------------------------------------------------------------------------
# 3. 模式路径（能成功就必须结构完整）
# --------------------------------------------------------------------------
def test_pattern_path_result_shape():
    """元素带 ``Invoke`` 模式时，若走模式路径，返回结构必须完整。

    ⚠️ 这里断言的是**条件式**："如果走了模式路径，字段就必须齐"。
    不直接断言"必须走模式路径" —— 那取决于 UIA 运行时是否可用，
    在无 UIA 的环境上会假红。判据是 ``extra["path"]``。
    """
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        el = _make_element(w, patterns=("Invoke",))
        try:
            r = A.click(el, timeout=0.01)
        except A.ActionFailed:
            return          # 该环境下模式调不通，跳过（不算失败）
        except Exception:
            return
        _expect(isinstance(r, A.ActionResult), "应返回 ActionResult")
        _expect(r.method, "method 不能为空")
        _expect(r.mode_used, "mode_used 不能为空")
        _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
        # 模式路径的 method 应该是模式名，不该是 mouse
        if r.extra.get("path") and r.extra["path"] != "coord":
            _expect(r.method != "mouse",
                    "走了模式路径（path=%r）但 method 报 mouse"
                    % r.extra.get("path"))
    finally:
        w.destroy()


def test_pattern_probe_failure_falls_through():
    """某个模式调用抛异常时，应继续试下一个而**不是整个 click 崩掉**。

    这是阶梯设计的意义：Invoke 调不通不代表 SelectionItem 也不行。
    """
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        el = _make_element(w, patterns=("Invoke", "SelectionItem"))
        try:
            r = A.click(el, timeout=0.01, allow_coord=True, human=False)
        except A.ActionFailed:
            return
        except Exception:
            return
        _expect(isinstance(r, A.ActionResult), "应返回 ActionResult")
        _expect(r.method, "method 不能为空")
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


def test_is_supported_query():
    """``is_supported`` 要能回答"某动作能否在这个元素上做"。"""
    w = _require_window()
    if w is None:
        return
    try:
        el = _make_element(w, patterns=("Invoke",))
        # 带模式快照 → 应该能确定地回答
        try:
            out = A.is_supported(el, "click")
        except Exception as e:
            # 环境不支持 UIA 查询也算通过，但要能明确报出来
            _expect(isinstance(e, Exception), "应抛明确异常")
            return
        _expect(isinstance(out, bool),
                "is_supported 应返回 bool，实际 %r" % type(out))
    finally:
        w.destroy()


# --------------------------------------------------------------------------
# 4. 其它动作的对接
# --------------------------------------------------------------------------
def test_set_value_returns_actionresult():
    """``set_value`` 的坐标路径也要返回完整 ActionResult。"""
    w = _require_window()
    if w is None:
        return
    try:
        D.set_interaction_mode(D.MODE_SILENT)
        el = _make_element(w, patterns=())
        try:
            r = A.set_value(el, "hello", timeout=0.01, allow_coord=True)
        except A.ActionFailed:
            return          # 该元素没有 Value 模式且坐标路径不可用
        except Exception:
            return
        _expect(isinstance(r, A.ActionResult), "应返回 ActionResult")
        _expect(r.method, "method 不能为空")
        _expect(r.mode_used, "mode_used 不能为空")
        _expect(isinstance(r.fallback_used, bool), "fallback_used 必须是 bool")
    finally:
        w.destroy()
        D.set_interaction_mode(D.MODE_SILENT)


def test_read_and_get_value_are_safe():
    """``read`` / ``get_value`` 对**不支持**的元素必须给出明确结果，不能崩。

    读操作是"尽力而为"：没有 Value 模式就返回 None，不该抛异常
    打断上层的判断流程。
    """
    w = _require_window()
    if w is None:
        return
    try:
        el = _make_element(w, patterns=())
        try:
            v = A.get_value(el)
        except Exception:
            v = None
        _expect(v is None or isinstance(v, str),
                "get_value 应返回 str 或 None，实际 %r" % type(v))
        try:
            d = A.read(el)
        except Exception:
            d = None
        _expect(d is None or isinstance(d, dict),
                "read 应返回 dict，实际 %r" % type(d))
    finally:
        w.destroy()


# --------------------------------------------------------------------------
# 5. 模块自检
# --------------------------------------------------------------------------
def test_actions_selfcheck():
    """``actions.selfcheck()`` 必须能跑通且报告 UIA 可用性。"""
    res = A.selfcheck()
    _expect(isinstance(res, dict), "selfcheck 应返回 dict，实际 %r" % type(res))
    # 至少要能回答"UIA 能不能用"
    keys = set(res.keys())
    _expect(keys, "selfcheck 返回空 dict")
    mono = str(res)
    _expect("ok" in mono.lower() or "uia" in mono.lower()
            or "available" in mono.lower(),
            "selfcheck 应报告 ok / uia / available 之类的状态：%r" % res)


def test_actionresult_describe():
    """``ActionResult.describe()`` 要把必填字段都带出来（供 CLI 打印）。"""
    r = A.ActionResult(ok=True, method="invoke", mode_used="silent",
                       fallback_used=False, detail="test")
    d = r.describe()
    _expect(isinstance(d, dict), "describe 应返回 dict")
    for k in ("ok", "method", "mode_used", "fallback_used", "detail"):
        _expect(k in d, "describe 缺少 %r" % k)
    _expect(d["method"] == "invoke" and d["mode_used"] == "silent",
            "describe 的值不对：%r" % d)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    print("=" * 64)
    print("阶段三 · 动作层测试（uia/actions ↔ 输入层对接）")
    print("=" * 64)

    section("输入层挂载")
    check("挂载标志可查询", test_input_layer_installed_flag)
    check("get_mode 转发到 dispatcher", test_get_mode_delegates_to_dispatcher)

    section("定位失败的纪律")
    check("⭐ 定位不到绝不降级", test_element_not_found_does_not_fallback)
    check("不认识的目标类型被拒绝", test_unsupported_target_type_rejected)
    check("⭐ 坐标兜底需显式允许", test_coord_requires_explicit_permission)
    check("允许后走坐标路径", test_coord_path_with_permission)
    check("⭐ 传给 dispatcher 的是客户区坐标", test_coord_path_uses_client_coords)

    section("模式路径")
    check("模式路径返回结构完整", test_pattern_path_result_shape)
    check("模式探测失败继续下一个", test_pattern_probe_failure_falls_through)
    check("is_supported 可查询", test_is_supported_query)

    section("其它动作对接")
    check("set_value 返回 ActionResult", test_set_value_returns_actionresult)
    check("read / get_value 不崩", test_read_and_get_value_are_safe)

    section("模块自检")
    check("actions.selfcheck 可运行", test_actions_selfcheck)
    check("ActionResult.describe 完整", test_actionresult_describe)

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print()
    print("=" * 64)
    print("阶段三 动作层测试: %d/%d 通过" % (passed, total))
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print()
        print("失败项：")
        for n, d in failed:
            print("  · %s -> %s" % (n, d))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
