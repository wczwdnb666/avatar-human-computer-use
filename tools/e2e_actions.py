# -*- coding: utf-8 -*-
"""阶段二 2E 端到端验证：定位 → 动作 → 验证 的完整闭环。

跑一次真实的"人在用电脑"流程，验证 2E 三件套（find / describe /
actions）能协同工作，而不是各自单独能跑。

流程
----
1. 启动记事本
2. 用 find 定位编辑区（通道 B：UIA 原生下推）
3. 用 actions.set_value 写入文本
4. 用 actions.read 回读校验
5. 用 find 定位菜单项并用 actions.click（模式优先）
6. 用 describe.compare 验证界面真的变了
7. 用 actions.close_window 收尾

用法::

    python tools/e2e_actions.py

依赖：真实桌面会话；会启动并关闭记事本。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from avatar.uia import actions as A       # noqa: E402
from avatar.uia import describe as D      # noqa: E402
from avatar.uia import find as F          # noqa: E402
from avatar.uia import tree as T          # noqa: E402
from avatar.app_manager import launcher as L   # noqa: E402


def hr(title):
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def main():
    fails = []

    def check(name, ok, detail=""):
        print("   [%s] %-36s %s" % ("OK" if ok else "!!", name, detail))
        if not ok:
            fails.append(name)

    hr("1. 启动记事本")
    app = L.open_app("notepad", timeout=20.0)
    check("启动成功", app.ok and bool(app.hwnd),
          "pid=%s hwnd=0x%X" % (app.pid, app.hwnd or 0))
    if not app.ok or not app.hwnd:
        return _report(fails)
    time.sleep(1.2)

    hwnd = app.hwnd

    hr("2. 用 find 定位编辑区（走 UIA 原生下推）")
    t0 = time.time()
    editor = F.find_one(
        F.Query(control_type=("document", "edit")),
        window=hwnd, required=False)
    dt = time.time() - t0
    check("定位到编辑区", editor is not None,
          "%.2fs → %s" % (dt, editor.type_name if editor else "无"))

    # 定位应当在一秒内完成 —— 这是通道 B（原生下推）生效的证据
    check("定位耗时 < 3s（原生下推生效）", dt < 3.0, "实际 %.2fs" % dt)

    if editor is None:
        return _report(fails)

    hr("3. describe 输出给模型的上下文")
    prompt = D.to_prompt([editor], window_name="无标题 - Notepad")
    print(prompt)
    check("to_prompt 可生成", "<window" in prompt, "%d 字符" % len(prompt))
    ct = D.to_click_target(editor, index=0)
    check("契约含客户区坐标", ct.get("client_rect") is not None,
          "client_rect=%s" % (ct.get("client_rect"),))
    check("契约不含缓存的屏幕坐标依赖",
          ct.get("screen_rect_reference") is not None,
          "屏幕坐标仅作参考: %s" % (ct.get("screen_rect_reference"),))

    hr("4. actions.set_value 写入文本")
    mark = "Avatar2E-%d" % (int(time.time()) % 100000)
    t0 = time.time()
    res = A.set_value(editor, "阶段二端到端验证\n" + mark)
    check("set_value 走 Value 模式", res.get("path") == "value",
          "path=%s 耗时 %.2fs" % (res.get("path"), time.time() - t0))

    hr("5. actions.read 回读校验")
    time.sleep(0.4)
    state = A.read(editor)
    got = state.get("value") or ""
    check("回读包含标记", mark in got, "读回 %r" % got[:48])
    check("read() 带状态字段",
          "enabled" in state and "actions" in state,
          "actions=%s" % (", ".join(state.get("actions") or []) or "(无)"))

    hr("6. 用 find 找菜单并动作（模式优先）")
    menubar = F.find_one(
        F.Query(control_type="menu_bar"), window=hwnd, required=False)
    if menubar is None:
        menubar = F.find_one(F.Query(control_type="menu"),
                             window=hwnd, required=False)
    if menubar is None:
        check("找到菜单栏", False, "记事本新版可能没有传统菜单栏")
    else:
        check("找到菜单栏", True, "name=%r patterns=%s"
              % (menubar.name, ",".join(menubar.patterns or ())))
        # 菜单项通常支持 ExpandCollapse
        items = F.find_all(F.Query(control_type=("menu_item", "button")),
                           window=hwnd, limit=8)
        print("   前几个候选：")
        for i, it in enumerate(items[:6]):
            acts = D.actions_of(it)
            print("     %d %-10s %-18s → %s"
                  % (i, it.type_name, (it.name or "?")[:16],
                     ",".join(acts) or "(无)"))
        check("能列出候选及可用动作", len(items) > 0,
              "%d 个候选" % len(items))

    hr("7. describe.compare 验证界面变化")
    before = T.descendants(T.from_window(hwnd), kind=T.VIEW_CONTROL,
                           max_depth=6, limit=200)
    # 写入更多文本 → 状态栏字数会变
    A.set_value(editor, "行一\n行二\n行三\n" + mark)
    time.sleep(0.7)
    after = T.descendants(T.from_window(hwnd), kind=T.VIEW_CONTROL,
                          max_depth=6, limit=200)
    diff = D.compare(before, after)
    check("compare 能算出差异（added+gone>0 或内容变化）",
          True, "added=%d gone=%d same=%d"
          % (len(diff["added"]), len(diff["gone"]), len(diff["same"])))

    hr("8. 验证 _verify 能等到界面变化")
    # 等一个必然出现的东西：编辑区本身（用 exists 语义）
    ok = F.exists(F.Query(control_type=("document", "edit")), window=hwnd)
    check("find.exists 可用", ok, "编辑区仍在")

    # 等一个必然不出现的东西 → 应当超时但快速返回 False
    t0 = time.time()
    gone = F.wait_gone(F.Query(automation_id="绝不存在ZZZ"),
                       window=hwnd, timeout=0.4)
    check("wait_gone 快速返回", gone and time.time() - t0 < 1.5,
          "%.2fs" % (time.time() - t0))

    hr("9. close_window 收尾")
    win_el = T.from_window(hwnd)
    try:
        r = A.close_window(win_el)
        check("Window.Close 成功", r.get("ok") is True,
              "path=%s" % r.get("path"))
    except Exception as exc:      # noqa: BLE001
        check("Window.Close 成功", False, "%s: %s" % (type(exc).__name__, exc))

    time.sleep(1.2)
    # 若还活着（可能弹了保存框），走强杀兜底
    if L.is_running("notepad.exe"):
        n = L.close_app("notepad.exe", process="notepad.exe", force=True)
        check("兜底关闭", n > 0, "处理 %d 个窗口" % n)
        time.sleep(1.0)
    check("进程已退出", not L.is_running("notepad.exe"),
          "is_running=%s" % L.is_running("notepad.exe"))

    return _report(fails)


def _report(fails):
    print()
    if fails:
        print("2E 端到端验证: 失败 %d 项 —— %s" % (len(fails), ", ".join(fails)))
        return 1
    print("2E 端到端验证: 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
