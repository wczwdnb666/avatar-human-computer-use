# -*- coding: utf-8 -*-
"""UIA 控件模式的端到端验证（需要真实桌面会话）。

职责
----
验证"读树 → 取模式 → 真的操作控件 → 校验结果"这条完整链路。
这是阶段二的核心验收：**光能读树不算数，得能动作**。

用法::

    python tools/e2e_patterns.py

依赖
----
· ``avatar.uia.tree`` / ``avatar.uia.patterns``
· ``avatar.app_manager.launcher``（启动并关闭记事本）
· 需要真实桌面（无桌面会话下 UIA 不可用）

注意
----
本脚本会**启动并关闭记事本**。运行前请保存好当前正在编辑的内容。
"""
import os
import sys
import time

# 允许从仓库根目录直接运行
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from avatar.uia import tree as T       # noqa: E402
from avatar.uia import patterns as P   # noqa: E402
from avatar.app_manager import launcher as L  # noqa: E402


def hr(title):
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def main():
    failures = []

    def check(name, ok, detail=""):
        print("   [%s] %-30s %s" % ("OK" if ok else "!!", name, detail))
        if not ok:
            failures.append(name)

    hr("1. 启动记事本")
    app = L.open_app("notepad", timeout=20.0)
    check("open_app 成功", app.ok and bool(app.hwnd),
          "pid=%s hwnd=0x%X" % (app.pid, app.hwnd or 0))
    if not app.ok or not app.hwnd:
        return _report(failures)
    time.sleep(1.2)

    hr("2. 读窗口 UIA 元素")
    win = T.from_window(app.hwnd)
    check("from_window 成功", bool(win), "name=%r type=%s" % (win.name[:30],
                                                             win.type_name))
    check("窗口有真实矩形", win.rect is not None, "rect=%s" % win.rect)
    check("window 句柄正确", win.window == app.hwnd,
          "window=0x%X hwnd=0x%X" % (win.window, win.hwnd))

    hr("3. 枚举后代并探测模式")
    desc = T.descendants(win, max_depth=8, limit=400, with_patterns=True)
    interactive = [e for e in desc if e.is_interactive]
    check("后代遍历有结果", len(desc) > 0, "%d 个后代" % len(desc))
    check("找到可交互控件", len(interactive) > 0,
          "%d 个可交互" % len(interactive))
    with_pats = [e for e in desc if e.patterns]
    check("模式探测有结果", len(with_pats) > 0,
          "%d 个元素带模式信息" % len(with_pats))

    hr("4. Value 模式：写入并回读文本")
    editor = None
    for e in desc:
        if e.control_type in (T.ControlType.DOCUMENT, T.ControlType.EDIT):
            editor = e
            break
    if editor is None:
        check("找到编辑区", False, "没找到 Document/Edit 控件")
    else:
        check("找到编辑区", True, "type=%s class=%r"
              % (editor.type_name, editor.class_name))
        vp = P.wrap(editor, "Value", required=False)
        if vp is None:
            check("编辑区支持 Value 模式", False, "不支持（可能需要键入路径）")
        else:
            try:
                mark = "AvatarE2E-%d" % (int(time.time()) % 100000)
                ok = vp.set_value("你好，Avatar！\n" + mark)
                check("SetValue 返回成功", ok)
                time.sleep(0.4)
                back = vp.value or ""
                check("回读内容一致", mark in back, "读回 %r" % back[:40])
            finally:
                vp.release()

    hr("5. Window 模式：读状态 + 最大化/还原")
    wp = P.wrap(win, "Window", required=False)
    if wp is None:
        check("窗口支持 Window 模式", False, "不支持")
    else:
        try:
            start = wp.visual_state
            check("能读 visual_state", start in (0, 1, 2),
                  "visual=%s modal=%s topmost=%s"
                  % (wp.visual_state_name, wp.is_modal, wp.is_topmost))
            wp.maximize()
            check("maximize 生效", wp.visual_state == P.WVS_MAXIMIZED,
                  "现在 %s" % wp.visual_state_name)
            wp.restore()
            check("restore 生效", wp.visual_state == P.WVS_NORMAL,
                  "现在 %s" % wp.visual_state_name)
        finally:
            wp.release()

    hr("6. pattern_actions / best_pattern 决策")
    acts = [(e, P.pattern_actions(e)) for e in interactive[:10]]
    non_empty = [a for _, a in acts if a]
    check("pattern_actions 有输出", len(non_empty) > 0,
          "%d/%d 个元素能列出动作" % (len(non_empty), len(acts)))

    best = [(e, P.best_pattern(e, ["Invoke", "Toggle", "SelectionItem",
                                   "ExpandCollapse", "LegacyIAccessible"]))
            for e in interactive[:10]]
    chosen = [b for _, b in best if b]
    check("best_pattern 能挑出路径", len(chosen) > 0,
          "%d/%d 个元素有首选模式" % (len(chosen), len(best)))
    print()
    print("   决策示例（这就是「元素定位优先」的落点）:")
    for e, b in best[:8]:
        if b:
            print("     %-12s %-20s → %s"
                  % (e.type_name, (e.name or "(无名)")[:18], b))

    hr("7. 关闭记事本")
    try:
        n = L.close_app("notepad.exe", process="notepad.exe", force=True)
        check("close_app 处理了窗口", n > 0, "%d 个窗口" % n)
        time.sleep(1.2)
        check("进程已退出", not L.is_running("notepad.exe"),
              "is_running=%s" % L.is_running("notepad.exe"))
    except Exception as exc:      # noqa: BLE001
        check("close_app 无异常", False, "%s: %s" % (type(exc).__name__, exc))

    return _report(failures)


def _report(failures):
    print()
    if failures:
        print("端到端验证: 失败 %d 项 —— %s" % (len(failures), ", ".join(failures)))
        return 1
    print("端到端验证: 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
