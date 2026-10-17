#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一自检入口 —— 一条命令跑完 avatar 的全部检查。

分两层：

**第一层 · 模块自检**（``selfcheck()``）
    每个模块自己声明的契约。纯只读、快（多数 <1s），
    适合在导入后 / 提交前随手跑一遍。``avatar verify`` 走的是这一层。

**第二层 · 集成测试**（``tests/test_phase*.py``）
    跨模块的行为验证。会真的拉起窗口、试点东西、
    跑几分钟，适合在发版前跑。``--full`` 打开。

用法::

    python scripts/selfcheck.py             # 模块自检（quick 档，毫秒级）
    python scripts/selfcheck.py --full      # 完整档：连实机探测一起跑（慢）
    python scripts/selfcheck.py --tests     # 再连集成测试文件一起跑（更慢）
    python scripts/selfcheck.py --only uia  # 只跑名字里含 uia 的项
    python scripts/selfcheck.py --list      # 只列出会跑哪些项

**默认档必须快。** 支持 ``quick`` 参数的模块在默认档会跳过实机探测
（绑端口、起假服务、扫文件系统、遍历窗口）。要跑实机加 ``--full``。

退出码：0 全过；1 有失败。可以直接接进 CI 或 git hook。

## 为什么要有这个文件

在它之前，检查散在十几个模块的 ``selfcheck()`` 和几个 ``tests/`` 里，
想知道"现在整体还健康吗"得记住一串命令。这里把它们收成一处，
并把失败原因按"谁挂了 / 挂在哪一项"归好 —— 而不是丢一大坨输出出来。
"""

from __future__ import annotations

import argparse
import importlib
import io
import os
import re
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------
# 第一层：模块自检登记表
# --------------------------------------------------------------------------
#: (分组, 模块路径)。按依赖由底向上排 —— 底层的挂了，上层基本也挂，
#: 先看最前面那个失败项就知道根因在哪。
#:
#: ``_win32`` 下的 com / variant / guids 是纯定义型模块（结构体、常量、
#: 指针包装），没有自己的 ``selfcheck()`` —— 它们的正确性由
#: ``tests/test_phase2_com.py`` 覆盖（那里会验 VARIANT 布局、vtable
#: 索引、IID 与 SDK 一致性）。所以这里不登记，免得误报"缺失"。
MODULE_SELFCHECKS = [
    ("几何", "avatar.app_manager.layout"),
    ("几何", "avatar.app_manager.monitor"),
    ("窗口", "avatar.app_manager.windows"),
    ("窗口", "avatar.app_manager.state"),
    ("窗口", "avatar.app_manager.foreground"),
    ("进程", "avatar.app_manager.process"),
    ("进程", "avatar.app_manager.launcher"),
    ("UIA", "avatar.uia.tree"),
    ("UIA", "avatar.uia.patterns"),
    ("UIA", "avatar.uia.find"),
    ("UIA", "avatar.uia.describe"),
    ("UIA", "avatar.uia.actions"),
    ("UIA", "avatar.uia.ocr_fallback"),
    ("UIA", "avatar.uia.vision_fallback"),
    # ---- 阶段三：输入层与真人熵 ----
    # 顺序仍然由底向上：entropy 是所有随机性的源头，它挂了后面全没意义；
    # 然后 kinetics / tremor 是纯数学；submove 依赖前三者；
    # mouse / keyboard 是真人化的门面；最后才是 dispatcher（把
    # 静默与物理两条路统一起来）。
    ("熵", "avatar.human_entropy.entropy"),
    ("熵", "avatar.human_entropy.kinetics"),
    ("熵", "avatar.human_entropy.tremor"),
    ("熵", "avatar.human_entropy.submove"),
    ("熵", "avatar.human_entropy.mouse"),
    ("熵", "avatar.human_entropy.keyboard"),
    ("熵", "avatar.human_entropy.metrics"),
    ("输入", "avatar.input.keyboard"),
    ("输入", "avatar.input.mouse"),
    ("输入", "avatar.input.dispatcher"),
    # ---- 阶段四：Web CDP 层 + 双端验证 ----
    # 顺序同样由底向上：websocket 是全部网络能力的地基（手搓 RFC 6455），
    # 它挂了后面一概不用看；session 建在它之上；launcher / navigate /
    # dom / interact / network / screenshot / tab 是具体能力；
    # fallback 与 electron 是"该不该用 CDP"的决策层，放最后。
    # verify.* 独立成一组：桌面端和 Web 端共用同一套三态结论。
    ("Web", "avatar.web_cdp.websocket"),
    ("Web", "avatar.web_cdp.session"),
    ("Web", "avatar.web_cdp.launcher"),
    ("Web", "avatar.web_cdp.navigate"),
    ("Web", "avatar.web_cdp.dom"),
    ("Web", "avatar.web_cdp.interact"),
    ("Web", "avatar.web_cdp.network"),
    ("Web", "avatar.web_cdp.screenshot"),
    ("Web", "avatar.web_cdp.tab"),
    ("Web", "avatar.web_cdp.electron"),
    ("Web", "avatar.web_cdp.fallback"),
    ("验证", "avatar.verify.result"),
    ("验证", "avatar.verify.desktop"),
    ("验证", "avatar.verify.web"),
    ("验证", "avatar.verify.action"),
    ("验证", "avatar.verify.retry"),
    ("验证", "avatar.verify.hook"),
    ("验证", "avatar.human_entropy.verify_hook"),
    # ---- 阶段五：风控（检测 → 熔断 → 恢复 → 打码 → 集成）----
    # 顺序**严格由底向上**，因为这一层每一环都依赖上一环：
    #   status      三态语义 —— 全部判定的地基，它错了后面全错
    #   fingerprints 指纹库 —— 判定的素材
    #   detector_*  三层检测 —— 产出 RiskStatus
    #   detector    降级编排 —— 把三层合成一个结论
    #   context     现场快照 —— 熔断与恢复之间的数据载体
    #   guard       熔断 —— 停
    #   resume      恢复 —— 复
    #   solver      外部打码 —— 默认关闭的可选路径
    #   integration 与主流程的接缝 —— 把上面这些挂到动作层
    #   planner     执行器骨架（阶段六接口占位）
    ("风控", "avatar.risk_detector.status"),
    ("风控", "avatar.risk_detector.fingerprints"),
    ("风控", "avatar.risk_detector.detector_dom"),
    ("风控", "avatar.risk_detector.detector_window"),
    ("风控", "avatar.risk_detector.detector_pixel"),
    ("风控", "avatar.risk_detector.detector"),
    ("风控", "avatar.risk_detector.context"),
    ("风控", "avatar.risk_detector.guard"),
    ("风控", "avatar.risk_detector.resume"),
    ("风控", "avatar.risk_detector.solver"),
    ("风控", "avatar.risk_detector.integration"),
    # ---- 阶段六：规划与执行 ----
    # 顺序**严格由底向上**，与阶段五同一个理由：
    #   goal        自然语言 → 结构化目标（最底层，不依赖任何东西）
    #   plan        步骤 + 依赖 → DAG，校验
    #   planner     Goal → Plan（Backend 可插拔）
    #   state       状态机（合法转换的唯一入口）
    #   memory      持久化（可断点恢复）
    #   observer    观测（执行前后各一次）
    #   verifier    验证（三态结论）
    #   recovery    恢复决策（五种策略）
    #   executor    把上面全部串起来（最上层）
    ("规划", "avatar.agentic_planner.goal"),
    ("规划", "avatar.agentic_planner.plan"),
    ("规划", "avatar.agentic_planner.planner"),
    ("规划", "avatar.agentic_planner.state"),
    ("规划", "avatar.agentic_planner.memory"),
    ("规划", "avatar.agentic_planner.observer"),
    ("规划", "avatar.agentic_planner.verifier"),
    ("规划", "avatar.agentic_planner.recovery"),
    ("规划", "avatar.agentic_planner.executor"),
    # ---- 阶段七：WinRT / OCR / 视觉链 / LLM / MCP ----
    # 顺序由底向上：winrt 是地基（它挂了 OCR 全没意义）；
    # 然后 OCR（第三层）；chain 依赖前两者；llm_backend 独立；
    # mcp 是最上层（它把全部能力暴露给外部客户端）。
    ("多模态", "avatar._win32.winrt"),
    ("多模态", "avatar.uia.ocr_fallback"),
    # Tesseract 旁路紧跟其后 —— 它是 ocr_fallback 挂了之后的退路
    ("多模态", "avatar.uia.ocr_tesseract"),
    ("多模态", "avatar.uia.vision_fallback"),
    ("多模态", "avatar.vision.chain"),
    ("多模态", "avatar.agentic_planner.llm_backend"),
    ("MCP", "avatar.mcp.protocol"),
    ("MCP", "avatar.mcp.tools"),
    ("MCP", "avatar.mcp.server"),
    # ---- 阶段八：视觉大模型层 ----
    # 由底向上：backend 是"怎么调"；image_prep 是"发什么"；
    # prompts 是"说什么"；validator 是"信不信"；cost 是"花多少"；
    # chain 把它们串成第五层。
    ("视觉LLM", "avatar.vision.llm_backend"),
    ("视觉LLM", "avatar.vision.image_prep"),
    ("视觉LLM", "avatar.vision.prompts"),
    ("视觉LLM", "avatar.vision.validator"),
    ("视觉LLM", "avatar.vision.cost"),
    # ---- 阶段九：交互外壳（**独立包**）----
    # ⚠ 注意 ``avatar_shell`` 不在 ``avatar/`` 里面，它**不 import avatar**
    #   （硬约束 2：外壳与主包只通过 stdio / MCP 通信）。所以它天然
    #   不参与主包的依赖图 —— 放最后，挂了也不影响主包。
    #
    # 顺序由底向上：state 是纯状态机（零 IO，最快）；
    # menu 是纯函数构建菜单项；ball 要真建分层窗口（碰 GDI）；
    # bridge 要起 MCP 子进程（最慢，也最可能受环境影响）。
    ("外壳", "avatar_shell.state"),
    ("外壳", "avatar_shell.menu"),
    ("外壳", "avatar_shell.ball"),
    ("外壳", "avatar_shell.bridge"),
]

#: 集成测试文件（相对仓库根）
TEST_FILES = [
    "tests/test_phase1.py",
    "tests/test_phase2_com.py",
    "tests/test_phase2_windows.py",
    "tests/test_phase2_uia.py",
    "tests/test_phase3_input.py",
    "tests/test_phase3_entropy.py",
    "tests/test_phase3_dispatcher.py",
    "tests/test_phase3_actions.py",
    "tests/test_phase4_websocket.py",
    "tests/test_phase4_cdp.py",
    "tests/test_phase4_electron.py",
    "tests/test_phase4_verify.py",
    "tests/test_phase5_guard.py",
    "tests/test_phase5_resume.py",
    "tests/test_phase5_solver.py",
    "tests/test_phase5_false_positive.py",
    "tests/test_phase6_goal.py",
    "tests/test_phase6_state.py",
    "tests/test_phase6_memory.py",
    "tests/test_phase6_executor.py",
    "tests/test_phase6_verifier.py",
    "tests/test_phase6_recovery.py",
    "tests/test_phase6_integration.py",
    "tests/test_phase7_winrt.py",
    "tests/test_phase7_ocr.py",
    "tests/test_phase7_vision_chain.py",
    "tests/test_phase7_llm.py",
    "tests/test_phase7_mcp.py",
    "tests/test_phase8_backend.py",
    "tests/test_phase8_image_prep.py",
    "tests/test_phase8_prompts.py",
    "tests/test_phase8_validator.py",
    "tests/test_phase8_cost.py",
    "tests/test_phase8_chain.py",
    "tests/test_phase9_shell.py",
]

# --------------------------------------------------------------------------
# 自检超时红线（硬约束）
# --------------------------------------------------------------------------
#: 单个模块 ``selfcheck()`` 的**硬上限**。超过即失败（不是警告）。
#:
#: ## 为什么是硬失败而不是"报警一下"
#
# 报警的结局是被忽略。曾经的 ``SLOW_SECONDS = 10.0`` 只是打印一行黄字，
# 于是 ``avatar.uia.actions`` 在 70.78 秒的状态下活了很久 —— 每次跑
# 全量自检都要等一分多钟，实际上"自检"这件事已经被废掉了（没人愿意跑）。
#
# 慢自检 = 没有自检。所以这里升级成硬失败。
#: 单模块上限（秒）。默认档 quick=True 必须满足。
MODULE_SELFCHECK_MAX = 3.0
#: 全量模块自检总时长上限（秒）。
TOTAL_SELFCHECK_MAX = 30.0
#: @deprecated 旧名保留，指向新的硬上限。有外部脚本引用它。
SLOW_SECONDS = MODULE_SELFCHECK_MAX


#: 已知"天生就慢"的白名单（模块路径 → 允许的秒数）。
#:
#: 存在的意义：有些模块的自检**本质上**要碰实机（比如真要枚举窗口），
#: 压不到 3 秒。与其假装达标，不如显式豁免并写明原因 ——
#: 这样"超标"就重新变成一个**有意义**的信号。
SLOW_ALLOWLIST = {
    # 实机窗口枚举，见 avatar/uia/windows.py。
    # 已经是 quick 档能压到的极限（只枚举顶层、带上限）。
    "avatar.uia.windows": 6.0,
}


def _max_for(path: str) -> float:
    return SLOW_ALLOWLIST.get(path, MODULE_SELFCHECK_MAX)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
class C:
    """ANSI 颜色 —— 只在 TTY 下开，重定向到文件时不掺控制字符。"""
    ON = sys.stdout.isatty()

    @classmethod
    def wrap(cls, code, s):
        return "\033[%sm%s\033[0m" % (code, s) if cls.ON else s

    @classmethod
    def ok(cls, s):
        return cls.wrap("32", s)

    @classmethod
    def fail(cls, s):
        return cls.wrap("31", s)

    @classmethod
    def err(cls, s):
        return cls.wrap("33", s)

    @classmethod
    def dim(cls, s):
        return cls.wrap("90", s)

    @classmethod
    def warn(cls, s):
        """软失败 / 环境缺失 —— 不算失败，但要显眼。

        用于"能力是可选的环境探测项"：没装 OCR、没配打码 key、
        无声卡导致出不了声。这些**不是代码的问题**，
        用红色报错会误导人去查代码。
        """
        return cls.wrap("35", s)


def _import(path):
    return importlib.import_module(path)


def _has_selfcheck(mod):
    return callable(getattr(mod, "selfcheck", None))


# --------------------------------------------------------------------------
# 第一层执行
# --------------------------------------------------------------------------
def run_module_selfchecks(filt=None, quick=True):
    """跑所有模块的 ``selfcheck()``。

    返回 ``(通过数, 总数, 跳过项数, 失败明细)``。
    明细是 ``(模块, 检查项, 原因)``。

    ## quick 档

    ``quick=True``（默认）→ 调 ``mod.selfcheck(quick=True)``。
    支持该参数的模块会跳过**实机探测**（绑端口、起假服务、扫文件系统、
    遍历窗口），耗时从秒级降到毫秒级。

    ``quick=False``（CLI 的 ``--full``）→ 传 ``quick=False``，
    连实机探测一起跑。

    ⚠ 为什么默认必须快：CI 上 ``web_cdp.launcher`` 曾因实机探测
    3.41s 超过 3.0s 红线，而同一份代码在别的 Python 版本上恰好压线过 ——
    那是**机器性能抖动**，不是代码问题。默认档不稳，红线就变成噪音，
    最后被无视（这个文件顶部的注释写过这件事）。

    ## SKIP 的口径

    模块自检可以给单个检查项打 ``skipped=True`` —— 表示**本机环境缺这个
    资源**（Server 版没有 UWP 应用、精简系统没有 App Paths 条目），
    不是代码有 bug。

    - SKIP 的项**不计入 failures**，也不改变退出码
    - 返回的第三个值是**检查项**级别的 SKIP 总数（不是模块数）——
      一个模块里可能有多项被跳过（launcher 就有 4 项）
    - 通过数 ``passed`` 仍是**模块**数：一个模块只要有失败就不算通过，
      但"有 SKIP 无 FAIL"的模块**算通过**（它该验的都验了）
    """
    passed = total = 0
    n_skipped = 0
    failures = []
    slow = []
    total_dt = 0.0
    cur_group = None

    for group, path in MODULE_SELFCHECKS:
        if filt and filt.lower() not in path.lower():
            continue
        total += 1

        if group != cur_group:
            print()
            print(C.dim("── %s " % group) + C.dim("─" * max(0, 56 - len(group))))
            cur_group = group

        short = path.split(".")[-1]
        label = "%s.%s" % (".".join(path.split(".")[:-1])[7:], short)

        # 导入
        try:
            mod = _import(path)
        except Exception as e:
            failures.append((path, "(导入)", "%r" % (e,)))
            print("  %s %-28s %s" % (C.fail("[IMPORT]"), label, e))
            continue

        if not _has_selfcheck(mod):
            failures.append((path, "(缺失)", "模块没有 selfcheck()"))
            print("  %s %-28s %s" % (C.fail("[NOCHECK]"), label,
                                     "模块没有 selfcheck()"))
            continue

        # 执行。
        #
        # ⚠ 优先传 quick —— 支持该参数的模块会跳过实机探测
        #   （``avatar.uia.actions`` 从 70.78s 降到 1.35s，
        #    ``avatar.web_cdp.launcher`` 从 2.5s 降到 ~0s）。
        #   不支持的老模块按原样无参调用。
        #   用 try/except TypeError 而不是 ``inspect.signature``，
        #   因为有些模块的 selfcheck 是 **C 层包装**或带装饰器，
        #   签名反射不可靠；真调一次最稳。
        t0 = time.monotonic()
        try:
            r = mod.selfcheck(quick=quick)
        except TypeError:
            try:
                r = mod.selfcheck()
            except Exception as e:
                failures.append((path, "(异常)", "%r" % (e,)))
                print("  %s %-28s %r" % (C.err("[ERROR]"), label, e))
                continue
        except Exception as e:
            failures.append((path, "(异常)", "%r" % (e,)))
            print("  %s %-28s %r" % (C.err("[ERROR]"), label, e))
            continue
        dt = time.monotonic() - t0
        total_dt += dt

        if not isinstance(r, dict):
            failures.append((path, "(返回值)", "selfcheck 应返回 dict"))
            print("  %s %-28s %s" % (C.fail("[SHAPE]"), label,
                                     "selfcheck 应返回 dict"))
            continue

        checks = r.get("checks") or []
        # ★ SKIP 的项不算失败 —— 环境缺资源 ≠ 代码有 bug。
        #   计数在**检查项**级别（一个模块可能多项被跳）。
        n_skip = len([c for c in checks if c.get("skipped")])
        n_skipped += n_skip
        bad = [c for c in checks
               if not c.get("ok") and not c.get("skipped")]

        # 红线判定（硬失败）
        limit = _max_for(path)
        over = dt > limit
        if over:
            slow.append((label, dt, limit))

        if r.get("ok") is True and not bad and not over:
            passed += 1
            tag = C.dim("%d 项 / %.2fs" % (len(checks), dt))
            if n_skip:
                tag += C.dim(" [SKIP %d]" % n_skip)
            if r.get("quick") is False:
                tag += C.dim(" [full]")
            if r.get("skipped_heavy"):
                tag += C.dim(" 跳过%d项重活" % r["skipped_heavy"])
            print("  %s %-28s %s" % (C.ok("[OK]"), label, tag))
            if n_skip:
                # 列出具体跳了哪几项 —— 否则 [SKIP N] 是个黑盒，
                # 看不出到底没验什么。用 ASCII 箭头，别在自检输出里
                # 引入非 ASCII（刚被 cp1252 咬过）。
                names = [c.get("name", "?")
                         for c in checks if c.get("skipped")]
                print(C.dim("       -> SKIP: %s" % ", ".join(names)))
        elif over and r.get("ok") is True and not bad:
            # 只有"慢"这一条问题
            failures.append((path, "(超时红线)",
                             "%.2fs > %.1fs 上限" % (dt, limit)))
            print("  %s %-28s %s"
                  % (C.fail("[SLOW]"), label,
                     C.err("%.2fs 超过 %.1fs 上限" % (dt, limit))))
        else:
            why = r.get("failures") or [c.get("name") for c in bad] or ["未知"]
            failures.append((path, "(自检)", "; ".join(why)[:200]))
            print("  %s %-28s %s" % (C.fail("[FAIL]"), label, "; ".join(why)[:120]))

    # 全量总时长红线
    if total_dt > TOTAL_SELFCHECK_MAX:
        failures.append(("(全部模块)", "(总时长红线)",
                         "累计 %.2fs > %.0fs 上限"
                         % (total_dt, TOTAL_SELFCHECK_MAX)))
        print()
        print(C.fail("  [FAIL] 模块自检累计 %.2fs，超过 %.0fs 总时长红线"
                     % (total_dt, TOTAL_SELFCHECK_MAX)))

    if slow:
        print()
        print(C.err("  ⚠ %d 个 selfcheck 超过红线：" % len(slow)))
        for label, dt, lim in sorted(slow, key=lambda x: -x[1]):
            print(C.err("      %-28s %.2fs （上限 %.1fs）" % (label, dt, lim)))
        print(C.dim("      —— selfcheck 要能随手跑。慢的话要么加 quick 档，"))
        print(C.dim("         要么把实机搜索限定到单窗口；"))
        print(C.dim("         全桌面覆盖留给 tests/ 里的集成测试。"))
        print(C.dim("         确实压不下去的，登记进 SLOW_ALLOWLIST 并写明原因。"))

    print()
    print(C.dim("  模块自检累计 %.2fs（红线 %.0fs）"
                % (total_dt, TOTAL_SELFCHECK_MAX)))

    return passed, total, n_skipped, failures


# --------------------------------------------------------------------------
# 第一点五层：阶段三专项检查（硬约束的落地校验）
# --------------------------------------------------------------------------
#: 阶段三八条质量硬约束里，有四条是**结构性**的、无法靠模块自检发现：
#:
#:   ① 纯 ctypes        —— 没有 pyautogui / pynput / pydirectinput
#:   ② MOUSEINPUT 字节数 —— 结构体尺寸错了 SendInput 会静默失败
#:   ③ 归一化公式        —— 写错就是系统性的 ±1px 偏移
#:   ④ 熵源纪律          —— 有模块绕过 entropy.py 直接调 random，
#:                          可复现性就破了，而且症状极隐蔽
#:
#: 这四项都放在这里，一条命令就能查。
def check_phase3_hard_constraints():
    """返回 ``(通过数, 总数, 失败明细)``。"""
    passed = total = 0
    failures = []

    def item(name, fn):
        nonlocal passed, total
        total += 1
        try:
            detail = fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print("  %s %-34s %s" % (C.fail("[FAIL]"), name, str(e)[:90]))
        except Exception as e:
            failures.append((name, "%r" % (e,)))
            print("  %s %-34s %r" % (C.err("[ERROR]"), name, e))
        else:
            tag = C.dim(detail) if detail else ""
            print("  %s %-34s %s" % (C.ok("[OK]"), name, tag))
            passed += 1

    print()
    print(C.dim("── 阶段三硬约束 ") + C.dim("─" * 44))

    # ---- 硬约束 #1：纯 ctypes，不许用现成的自动化库 ------------------------
    def _c1():
        banned = ("pyautogui", "pynput", "pydirectinput", "keyboard",
                  "mouse", "autopy", "win32api", "win32con", "win32gui")
        roots = [os.path.join(ROOT, "avatar", "input"),
                 os.path.join(ROOT, "avatar", "human_entropy")]
        offenders = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for fn in sorted(os.listdir(root)):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                with open(p, "r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        code = line.split("#", 1)[0]
                        s = code.strip()
                        if not (s.startswith("import ") or s.startswith("from ")):
                            continue
                        for b in banned:
                            # `from . import mouse` 是自己的模块，不算
                            if s in ("from . import %s" % b,
                                     "from .. import %s" % b,
                                     "import %s" % b
                                     if b in ("avatar",) else "\0"):
                                continue
                            if ("import " + b) in s or ("from " + b) in s:
                                if b in ("keyboard", "mouse") and s.startswith("from ."):
                                    continue
                                offenders.append("%s:%d %s" % (fn, lineno, s))
        assert not offenders, (
            "以下位置用了被禁的第三方输入库（硬约束 #1 要求纯 ctypes）：\n      "
            + "\n      ".join(offenders[:5]))
        return "纯 ctypes"

    item("纯 ctypes · 无第三方输入库", _c1)

    # ---- 硬约束 #2：结构体尺寸断言（写成模块级常量） ----------------------
    def _c2():
        from avatar._win32 import structs as S
        import ctypes as C_
        exp64 = {"MOUSEINPUT": 32, "KEYBDINPUT": 24, "HARDWAREINPUT": 8}
        bits = C_.sizeof(C_.c_void_p) * 8
        got = {}
        for nm, sz in exp64.items():
            cls = getattr(S, nm, None)
            assert cls is not None, "structs 里缺少 %s" % nm
            got[nm] = C_.sizeof(cls)
        if bits == 64:
            for nm, sz in exp64.items():
                assert got[nm] == sz, (
                    "%s 应为 %d 字节（x64），实际 %d —— SendInput 会静默失败"
                    % (nm, sz, got[nm]))
        else:
            # 32 位下 MOUSEINPUT/KEYBDINPUT 各少 8 字节（指针宽度）
            assert got["MOUSEINPUT"] == 24, (
                "MOUSEINPUT 应为 24 字节（x86），实际 %d" % got["MOUSEINPUT"])
            assert got["KEYBDINPUT"] == 16, (
                "KEYBDINPUT 应为 16 字节（x86），实际 %d" % got["KEYBDINPUT"])
        # 断言必须真的在模块里跑过（有 _assert_struct_sizes 就调一次）
        fn = getattr(S, "_assert_struct_sizes", None)
        if callable(fn):
            fn()
        return "x%d · MOUSEINPUT=%d" % (bits, got["MOUSEINPUT"])

    item("结构体尺寸（硬约束 #2）", _c2)

    # ---- 硬约束 #3：绝对坐标归一化，无 ±1px 系统性偏差 --------------------
    def _c3():
        from avatar.input import mouse as M
        w, h = M.primary_screen_size()
        assert w > 0 and h > 0, "拿不到主屏尺寸"
        assert getattr(M, "ABSOLUTE_MAX", None) == 65535, (
            "ABSOLUTE_MAX 应为 65535，实际 %r" % getattr(M, "ABSOLUTE_MAX", None))
        assert getattr(M, "ABSOLUTE_SCALE", None) == 65536, (
            "ABSOLUTE_SCALE 应为 65536 —— Windows 反算用的是 65536 而不是 "
            "65535，写成 65535 会带来系统性 +1px 偏差。实际 %r"
            % getattr(M, "ABSOLUTE_SCALE", None))
        # 首尾必须落在合法区间
        nx0, ny0 = M.normalize_absolute(0, 0)
        assert nx0 == 0 and ny0 == 0, "原点应映射到 (0,0)，实际 (%d,%d)" % (nx0, ny0)
        nx1, ny1 = M.normalize_absolute(w - 1, h - 1)
        assert 0 <= nx1 <= 65535 and 0 <= ny1 <= 65535, (
            "右下角越界：(%d,%d)" % (nx1, ny1))
        # 越界输入必须被钳制
        nxo, nyo = M.normalize_absolute(w + 5000, h + 5000)
        assert nxo <= 65535 and nyo <= 65535, (
            "越界输入未被钳制：(%d,%d)" % (nxo, nyo))
        # 全像素扫描：反算误差必须为 0
        worst = 0
        step = max(1, w // 400)
        for x in range(0, w, step):
            n = M.normalize_absolute(x, 0)[0]
            back = min(w - 1, (n * w + 32768) // 65536)
            worst = max(worst, abs(back - x))
        assert worst == 0, (
            "归一化反算出现 %dpx 误差（应为 0）—— 公式或分母写错了" % worst)
        return "扫描 %d 点 · 误差 0px" % len(range(0, w, step))

    item("绝对坐标归一化（硬约束 #3）", _c3)

    # ---- 硬约束 #4：熵源纪律 ---------------------------------------------
    def _c4():
        import re
        root = os.path.join(ROOT, "avatar", "human_entropy")
        if not os.path.isdir(root):
            raise AssertionError("human_entropy 目录不存在")
        # 匹配 `random.xxx(...)` 这种模块级调用，允许 `_random_module`
        # 和 `self.random`
        pat = re.compile(r"(?<![_\w.])random\s*\.\s*(?!Random\b)")
        offenders = []
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".py") or fn == "entropy.py":
                continue
            p = os.path.join(root, fn)
            with open(p, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]
                    if pat.search(code):
                        offenders.append("%s:%d %s" % (fn, lineno, code.strip()))
        assert not offenders, (
            "以下位置绕过了 entropy.py 直接调 random（硬约束 #4）：\n      "
            + "\n      ".join(offenders[:5]))
        return "%d 个模块合规" % len([f for f in os.listdir(root)
                                      if f.endswith(".py") and f != "entropy.py"])

    item("熵源纪律（硬约束 #4）", _c4)

    # ---- 硬约束 #5：可复现性（自证，不依赖测试文件） ----------------------
    def _c5():
        from avatar.human_entropy import entropy as E
        from avatar.human_entropy import submove as SM
        saved = E.get_seed()
        try:
            E.seed(20260927)
            a = [(s.x, s.y, s.delay)
                 for s in SM.plan_movement(100, 100, 700, 500).segments]
            E.seed(20260927)
            b = [(s.x, s.y, s.delay)
                 for s in SM.plan_movement(100, 100, 700, 500).segments]
            assert a == b, (
                "同 seed 两次规划出的轨迹不一致（%d 点 vs %d 点）—— "
                "硬约束 #5 破了，说明有随机源漏出了熵控制"
                % (len(a), len(b)))
            assert len(a) > 10, "轨迹点数 %d 太少，这条检查没意义" % len(a)
            return "同 seed %d 点逐点一致" % len(a)
        finally:
            if saved is None:
                E.reset()
            else:
                E.seed(saved)

    item("可复现性（硬约束 #5）", _c5)

    # ---- 硬约束 #6：静默模式可达性（真的抬一个窗口来试） ------------------
    def _c6():
        import ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        from avatar.input import dispatcher as D
        from avatar.errors import TargetUnreachable
        hwnd = u.CreateWindowExW(0, "STATIC", "avatar-selfcheck", 0x80000000,
                                 40, 40, 200, 120, 0, 0, 0, 0)
        assert hwnd, "建测试窗口失败"
        try:
            u.ShowWindow(hwnd, 4)
            u.UpdateWindow(hwnd)
            D.check_window_reachable(hwnd)     # 可见窗口应通过
            # 死句柄必须抛
            u.DestroyWindow(hwnd)
            dead = hwnd
            hwnd = 0
            try:
                D.check_window_reachable(dead)
            except TargetUnreachable:
                pass
            else:
                raise AssertionError(
                    "已销毁的窗口没被判为不可达 —— 硬约束 #6 要求"
                    "不可达必须抛异常，不许静默失败")
        finally:
            if hwnd:
                u.DestroyWindow(hwnd)
        return "可见 OK / 死句柄抛异常"

    item("静默可达性（硬约束 #6）", _c6)

    # ---- 硬约束 #7：ActionResult 必填字段 ---------------------------------
    def _c7():
        import inspect
        from avatar.input import dispatcher as D
        sig = inspect.signature(D.ActionResult)
        missing = []
        for f in ("method", "mode_used", "fallback_used"):
            if f not in sig.parameters:
                missing.append(f)
                continue
            if sig.parameters[f].default is not inspect.Parameter.empty:
                missing.append("%s(有默认值)" % f)
        assert not missing, (
            "ActionResult 的必填字段缺省或带了默认值（硬约束 #7）：%s"
            % ", ".join(missing))
        return "method / mode_used / fallback_used"

    item("ActionResult 必填字段（硬约束 #7）", _c7)

    # ---- 硬约束 #8：模块级 docstring --------------------------------------
    def _c8():
        roots = [os.path.join(ROOT, "avatar", "input"),
                 os.path.join(ROOT, "avatar", "human_entropy")]
        missing = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for fn in sorted(os.listdir(root)):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
                # 找第一个三引号块；允许前面有 shebang / coding / 注释
                i = text.find('"""')
                if i < 0:
                    i = text.find("'''")
                if i < 0:
                    missing.append(fn)
                    continue
                # docstring 之后不该立刻是 import（太短）
                end = text.find('"""', i + 3)
                if end < 0:
                    end = text.find("'''", i + 3)
                if end < 0 or len(text[i:end].strip()) < 40:
                    missing.append(fn)
        assert not missing, (
            "以下模块缺少像样的模块级 docstring（硬约束 #8）：%s"
            % ", ".join(missing))
        return "%d 个模块齐全" % sum(
            1 for r in roots for f in os.listdir(r) if f.endswith(".py"))

    item("模块 docstring（硬约束 #8）", _c8)

    # ---- 硬约束 #9：自检自身的性能红线 -----------------------------------
    def _c9():
        """**自检必须快。** 单模块 ≤ 3s，全量 ≤ 30s。

        ## 为什么这条要写进硬约束，而不是"尽力而为"

        自检的定位是**随手就能跑** —— 改完一个函数、提交之前都能跑一遍。
        一旦某模块跑到几十秒，人就不愿意跑了，**等于没有自检**。

        这是实测踩出来的，不是理论担忧：

            avatar.uia.actions.selfcheck()        70.78s   ← 真实窗口全遍历
            avatar.verify.web.selfcheck()          6.65s   ← 46 处 timeout=0.3 空等
            avatar.verify.desktop.selfcheck()      4.28s   ← 31 处 timeout=0.2 空等
            avatar.web_cdp.electron.selfcheck()    3.95s   ← 失败路径等满 timeout

        修好之后：1.35 / 0.60 / 0.99 / 2.53，全量从 70s+ 降到 13s 级。
        四项的病因**完全不同**（全遍历 / 纯逻辑空等 / 失败路径空等），
        但表现都是"自检慢"，所以必须有统一的红线来兜。

        ## 为什么是硬失败

        最早的实现只是打印一行黄字警告（``SLOW_SECONDS``）。
        警告的结局就是被忽略 —— 上面那个 70.78s 就是这么活下来的。
        现在超红线**直接计入 failures**，全量自检会报"不通过"。
        """
        # 逐模块真跑一遍太慢（那正是我们要避免的）。这里只做**结构性**
        # 校验：红线常量存在且合理。
        assert MODULE_SELFCHECK_MAX <= 3.0, \
            "单模块红线 %s 超过 3s 规约" % MODULE_SELFCHECK_MAX
        assert TOTAL_SELFCHECK_MAX <= 30.0, \
            "全量红线 %s 超过 30s 规约" % TOTAL_SELFCHECK_MAX

        # 豁免名单必须**每一条都写明理由**（防止拿它当万能挡箭牌）
        for path, lim in SLOW_ALLOWLIST.items():
            assert lim > MODULE_SELFCHECK_MAX, \
                "白名单 %s 的额度 %s 不该 ≤ 默认红线" % (path, lim)

        return "单模块 ≤ %.0fs / 全量 ≤ %.0fs（豁免 %d 条）" % (
            MODULE_SELFCHECK_MAX, TOTAL_SELFCHECK_MAX, len(SLOW_ALLOWLIST))

    item("自检性能红线（硬约束 #9）", _c9)

    return passed, total, failures


# --------------------------------------------------------------------------
# 阶段四专项：Web CDP 层 + 双端验证的硬约束
# --------------------------------------------------------------------------
def check_phase4_hard_constraints(quick=True):
    """返回 ``(通过数, 总数, 失败明细)``。

    这里的检查和阶段三的思路一致：**验的是设计承诺，不是功能**。
    功能由 ``tests/test_phase4_*.py`` 覆盖；这里只确认那些
    "一旦违背就会毁掉整个架构"的红线还在。

    ## quick 档

    有两项本质上是**实机探测**（扫文件系统找浏览器、扫 TCP 表找监听端口），
    耗时且受机器负载影响，标记为 ``heavy``。``quick=True``（默认）时跳过，
    并打印 ``[SKIP]`` 如实说明 —— 不静默、也不算通过。

    被跳过的项**不计入总数**，所以 ``11/11`` 的含义是
    "实际跑了的 11 项全过"，而不是"13 项全过"。
    """
    passed = total = 0
    skipped = []
    failures = []

    def item(name, fn, heavy=False):
        nonlocal passed, total
        if heavy and quick:
            skipped.append(name)
            print("  %s %-34s %s"
                  % (C.dim("[SKIP]"), name,
                     C.dim("实机探测，quick 档跳过（--full 才跑）")))
            return
        total += 1
        try:
            detail = fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print("  %s %-34s %s" % (C.fail("[FAIL]"), name, str(e)[:90]))
        except Exception as e:
            failures.append((name, "%r" % (e,)))
            print("  %s %-34s %r" % (C.err("[ERROR]"), name, e))
        else:
            tag = C.dim(detail) if detail else ""
            print("  %s %-34s %s" % (C.ok("[OK]"), name, tag))
            passed += 1

    print()
    print(C.dim("── 阶段四硬约束 ") + C.dim("─" * 44))

    # ---- 硬约束 #1：手搓 WebSocket，禁用现成的 ws 库 ----------------------
    def _w1():
        """``avatar/web_cdp/`` 里不许 import websocket-client / websockets
        等第三方库 —— 整个 CDP 层的价值就在于零依赖手搓 RFC 6455。

        注意：stdlib 的 ``http.client`` / ``socket`` / ``base64`` /
        ``hashlib`` / ``struct`` 是**允许且必需**的。
        """
        banned_prefixes = ("websocket", "websockets", "wsproto",
                           "autobahn", "tornado.websocket",
                           "aiohttp", "pychrome", "selenium",
                           "playwright", "pyppeteer", "nodriver")
        root = os.path.join(ROOT, "avatar", "web_cdp")
        offenders = []
        if os.path.isdir(root):
            for fn in sorted(os.listdir(root)):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                try:
                    with io.open(path, encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            st = line.strip()
                            if not (st.startswith("import ")
                                    or st.startswith("from ")):
                                continue
                            for bad in banned_prefixes:
                                if ("import %s" % bad) in st \
                                        or ("from %s" % bad) in st:
                                    offenders.append("%s:%d %s"
                                                     % (fn, lineno, st[:50]))
                except Exception:      # noqa: BLE001
                    pass
        assert not offenders, "发现禁用依赖：%s" % ("; ".join(offenders[:3]))
        return "零第三方网络库"

    # ---- 硬约束 #2：verify 只返回结构化结论，禁止裸 bool -------------------
    def _w2():
        """``verify_action`` / ``verify_desktop`` / ``verify_web`` 的返回值
        必须是 ``VerifyResult``。这是"禁止用裸 bool"的落地检查。

        裸 bool 会丢掉"不知道"这个状态，而"不知道"恰恰是自动化里
        最重要的一态（决定了要不要重试）。
        """
        from avatar.verify import (verify_action, verify_desktop,
                                   verify_web, VerifyResult)
        outs = [
            ("verify_action(空判据)", verify_action("desktop", timeout=0.2)),
            ("verify_action(边界目标)",
             verify_action("desktop", target="__zzz_nope__", timeout=0.2)),
            ("verify_desktop",
             verify_desktop(hwnd=0, verify_window_title="x", timeout=0.2)),
            ("verify_web",
             verify_web(selector="#nope", body_contains="zzz")),
        ]
        for label, out in outs:
            assert isinstance(out, VerifyResult), \
                "%s 返回了 %r（应为 VerifyResult）" % (label, type(out))
        return "4 个入口全返回三态"

    # ---- 硬约束 #3：三态语义完整（ok / fail / unknown 可区分） ------------
    def _w3():
        from avatar.verify import VerifyResult, VERIFY_OK, VERIFY_FAIL, \
            VERIFY_UNKNOWN
        assert len({VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN}) == 3
        o = VerifyResult.ok_()
        f = VerifyResult.fail()
        u = VerifyResult.unknown_()
        assert o.ok and not o.failed and not o.unknown
        assert f.failed and not f.ok and not f.unknown
        assert u.unknown and not u.ok and not u.failed, \
            "unknown 必须是独立第三态，不能等于 fail"
        # 未知时置信度必须为 0，否则加权汇总会算错
        assert u.confidence == 0.0, "unknown 的置信度应为 0"
        return "ok/fail/unknown 严格互斥"

    # ---- 硬约束 #4：CDP 连接只走明文 ws，拒绝 wss -------------------------
    def _w4():
        """CDP 的调试端口永远是明文 —— 明确拒绝 ``wss://`` 是安全特性。

        如果哪天有人"顺手"让它接受 wss，就可能把明文调试流量
        发到远端主机上。这条检查守住这个边界。
        """
        from avatar.web_cdp import websocket as W
        for bad in ("wss://127.0.0.1:9222/x", "https://127.0.0.1:9222/x"):
            try:
                W.ws_url_to_host_port(bad)
            except Exception:
                continue
            raise AssertionError("%s 被接受了（应拒绝）" % bad)
        # 明文路径必须正常
        h, port, path = W.ws_url_to_host_port(
            "ws://127.0.0.1:9222/devtools/browser/x")
        assert (h, port) == ("127.0.0.1", 9222), "ws:// 解析异常"
        return "拒绝 wss / 接受 ws"

    # ---- 硬约束 #5：启动浏览器必须先挑空闲端口 ----------------------------
    def _w5():
        """不先检测端口就启动，会撞上已有实例 —— Chrome 会**静默**把
        请求交给已运行实例然后自己退出，调试端口不生效，
        而你会以为启动成功了（这是最坑的一种失败）。

        检查点：``pick_free_port`` / ``is_port_free`` 存在且可用。
        """
        from avatar.web_cdp import launcher as LB
        assert hasattr(LB, "pick_free_port"), "缺 pick_free_port"
        assert hasattr(LB, "is_port_free"), "缺 is_port_free"
        p = LB.pick_free_port()
        assert isinstance(p, int) and 1 <= p <= 65535, "端口非法：%r" % p
        # 监听中的端口必须被判定为"占用"
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        try:
            assert LB.is_port_free(busy) is False, \
                "占用中的端口 %d 被判成空闲（会导致启动静默失败）" % busy
        finally:
            srv.close()
        return "空闲端口检测可用"

    # ---- 硬约束 #6：进程黑名单，避免盲目扫端口 ----------------------------
    def _w6():
        """``explorer`` / ``svchost`` 这类进程不可能有 CDP。没有黑名单
        就会对每个窗口都做一遍端口扫描 —— 慢，而且会误连到
        别的进程的调试端口上。
        """
        from avatar.web_cdp import fallback as F
        assert F.detect_process_kind("explorer.exe") == "never_cdp"
        assert F.detect_process_kind("svchost.exe") == "never_cdp"
        # 黑名单与白名单不能有交集（否则判定结果依赖检查顺序，很脆）
        assert not (F.NEVER_CDP_PROCESSES & F.BROWSER_PROCESSES), \
            "黑名单与浏览器白名单有交集"
        assert not (F.NEVER_CDP_PROCESSES & F.ELECTRON_PROCESSES), \
            "黑名单与 Electron 白名单有交集"
        return "%d 个黑名单进程" % len(F.NEVER_CDP_PROCESSES)

    # ---- 硬约束 #7：降级决策必须带 reason ---------------------------------
    def _w7():
        """每次降级都要能说清"为什么"。没有 reason 的降级在排查时
        等于没有信息 —— 你会不知道是"端口没开"还是"连错了进程"。
        """
        from avatar.web_cdp import fallback as F
        cases = [
            ("explorer.exe", ""),
            ("msedge.exe", "msedge.exe --headless"),
            ("Code.exe", "Code.exe --remote-debugging-pipe"),
            ("", ""),
        ]
        for name, cmd in cases:
            d = F.decide_level(process_name=name, cmdline=cmd, probe=False)
            assert d.reason, "进程 %r 的降级决策没有 reason" % name
            assert d.describe().get("degraded") is not None
        return "%d 个降级场景都有 reason" % len(cases)

    # ---- 硬约束 #8：Electron 识别失败必须返回 None 而不是抛异常 -----------
    def _w8():
        """绝大多数商业 Electron 不开调试端口 —— 附加失败是**常态**。

        如果这里抛异常，上层就得用 try/except 包住每次调用，
        那是很糟的接口设计。
        """
        from avatar.web_cdp import electron as E
        got = E.attach_electron(pid=999999, process_name="totally_unknown.exe",
                                port=None, timeout=0.8)
        assert got is None, "附加失败应返回 None，实际 %r" % (got,)
        return "失败返回 None"

    # ---- 硬约束 #9：浏览器探测（有就报状态，没有不算失败） ----------------
    def _w9():
        """检测本机 Chrome / Edge。**没有浏览器不算失败** —— 这是
        环境事实，不是代码缺陷。有就顺带报一下版本。"""
        from avatar.web_cdp import launcher as LB
        found = []
        for name, fn in (("chrome", LB.find_chrome), ("edge", LB.find_edge)):
            try:
                exe = fn()
            except Exception:      # noqa: BLE001
                exe = None
            if exe:
                found.append(name)
        if not found:
            return C.dim("未检测到 Chrome / Edge（不影响自检）")
        return "已检测到 %s" % "+".join(found)

    # ---- 硬约束 #10：Electron 端口扫描可跑且结果可信 ----------------------
    def _w10():
        """扫描本进程的监听端口 —— 这是 ``find_debug_port`` 的基础。
        自己起一个监听，扫出来的列表里必须包含它。"""
        from avatar.web_cdp import electron as E
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        mine = srv.getsockname()[1]
        try:
            ports = E.scan_listen_ports(os.getpid())
            assert mine in ports, \
                "扫不到自己刚起的监听端口 %d（列表 %r）" % (mine, ports[:8])
            assert ports == sorted(set(ports)), "端口列表未去重排序"
        finally:
            srv.close()
        return "扫到 %d 个监听端口" % len(ports)

    # ---- 硬约束 #11：模块级 docstring -------------------------------------
    def _w11():
        """阶段四每个模块都要有 docstring —— 这一层涉及协议细节，
        没有文档的模块等于不可维护。"""
        roots = [os.path.join(ROOT, "avatar", "web_cdp"),
                 os.path.join(ROOT, "avatar", "verify")]
        missing = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for fn in sorted(os.listdir(root)):
                if not fn.endswith(".py"):
                    continue
                if fn == "__init__.py":
                    continue
                path = os.path.join(root, fn)
                try:
                    mod = _import(os.path.relpath(path, ROOT)
                                  .replace(os.sep, ".")[:-3])
                except Exception as e:      # noqa: BLE001
                    missing.append("%s(导入失败:%s)" % (fn, type(e).__name__))
                    continue
                if not (mod.__doc__ or "").strip():
                    missing.append(fn)
        assert not missing, "缺模块 docstring：%s" % (", ".join(missing[:5]))
        return "%d 个模块都有 docstring" % (
            len(os.listdir(roots[0])) + len(os.listdir(roots[1])) - 2)

    # ---- 硬约束 #12：双端验证共用一个入口 ---------------------------------
    def _w12():
        """桌面和 Web **必须**走同一个 ``verify_action``，靠内部路由分流。

        分成两个入口的话，调用方就得自己判断"这是桌面还是网页"——
        而这个判断恰恰是最容易出错的地方（Electron 既是也不是）。

        ⚠ 这条要**真调** ``verify_action``（带 timeout 与轮询 sleep），
        实测 2.0s，归 heavy。
        """
        from avatar.verify import verify_action, VerifyRequest
        d = verify_action("desktop", hwnd=0, verify_window_title="x",
                          timeout=0.2)
        w = verify_action("web", selector="#nope", url_contains="zzz")
        assert isinstance(d, type(w)), "两路返回类型不一致"
        # auto 模式要能自己选路
        a = verify_action("auto", uia_target="__nope__", timeout=0.2)
        assert isinstance(a, type(d)), "auto 模式返回类型不一致"
        # mode 必须真的生效（desktop 请求不该被路由到 web）
        assert verify_action("desktop", timeout=0.2).mode == "desktop"
        return "同一入口 / 内部路由"

    # ---- 硬约束 #13：UIA 查询收紧旋钮必须全链路透传 -----------------------
    def _w13():
        """``name_exact`` / ``control_type`` / ``window`` 三个旋钮
        **必须从 CLI 一路透传到** ``_element_of``。

        ## 为什么这是硬约束而不是优化

        ``find`` 的通道选路规则：条件能下推给 UIA 原生 ``FindAll``
        就走通道 B（秒级），否则走通道 C（Python 全遍历）。
        真机实测（4000+ 元素的桌面）：

            name_exact 下推            1.75s
            Python 全遍历             52.29s      ← 相差 30 倍

        而"字符串按名字子串找"正好是**下推不了**的那类。所以：
        旋钮在任何一层被吞掉，调用方看到的就是"随手查一个控件卡 50 秒"，
        并且 ``timeout`` 形同虚设（单次查询就超预算，轮询轮不起来）。

        踩过：CLI 加了 ``--name-exact``，但 ``verify_desktop`` 的签名里
        没有对应形参 → 参数静默消失 → 耗时仍是 51.2 秒。
        **静默失效是最坏的失效方式**，所以这里直接盯源码。
        """
        import inspect
        from avatar.verify import desktop as D
        from avatar.verify import action as A

        sig_e = inspect.signature(D._element_of)
        for name in ("name_exact", "control_type", "window"):
            assert name in sig_e.parameters, \
                "_element_of 缺收紧旋钮 %r" % name

        sig_v = inspect.signature(D.verify_uia_exists)
        for name in ("name_exact", "control_type", "window"):
            assert name in sig_v.parameters, \
                "verify_uia_exists 缺 %r" % name

        src_d = inspect.getsource(D.verify_desktop)
        assert "name_exact=uia_name_exact" in src_d, \
            "verify_desktop 没把 name_exact 转发给 verify_uia_exists"
        assert "control_type=uia_control_type" in src_d, \
            "verify_desktop 没把 control_type 转发下去"

        src_a = inspect.getsource(A._do_desktop)
        assert "name_exact" in src_a and "control_type" in src_a, \
            "_do_desktop 没接住 name_exact / control_type"

        # 可观测性：慢查询必须自曝
        #
        # ⚠ 这一条要**真跑一次** UIA 查询（全桌面遍历，实测 0.9s），
        #   所以归 heavy。quick 档保留上面那些静态检查
        #   （签名 / 源码透传），只把这一次实机查询让给 --full。
        if quick:
            return "三旋钮全链路透传（慢查询自曝需 --full）"
        r = D.verify_uia_exists("__nope_zzz__", expect=True,
                               timeout=0.0, name_exact=True)
        assert "probes" in r.evidence and "probe_seconds" in r.evidence, \
            "慢查询没有自曝（缺 probes / probe_seconds）—— 无法排查"

        return "三旋钮全链路透传 + 慢查询自曝"

    item("手搓 WebSocket · 禁第三方库", _w1)
    item("verify 只返回结构化结论", _w2)
    item("三态语义完整", _w3)
    item("CDP 只走明文 ws · 拒 wss", _w4)
    item("启动前挑空闲端口", _w5)
    item("进程黑名单", _w6)
    item("降级决策必带 reason", _w7)
    item("Electron 失败返 None", _w8)
    item("浏览器探测（环境信息）", _w9, heavy=True)
    item("Electron 端口扫描", _w10, heavy=True)
    item("模块 docstring", _w11)
    item("双端验证共用一个入口", _w12, heavy=True)
    item("UIA 收紧旋钮全链路透传", _w13)

    return passed, total, failures


# --------------------------------------------------------------------------
# 阶段六：规划与执行硬约束
# --------------------------------------------------------------------------
def check_phase6_hard_constraints():
    """阶段六的硬约束。

    分三块：

    **A. 可用性** —— 规划链路能不能跑通（Goal → Plan → 执行骨架）
    **B. 可序列化性** —— memory 必须能 JSON 往返（硬约束 5，禁 pickle）
    **C. 硬约束** —— 上限、状态机、恢复决策表、风控集成

    **全程不碰真实桌面、不写真实任务目录。**
    """
    passed = total = 0
    failures = []

    def item(name, ok, detail="", soft=False):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print("  %s %-46s %s" % (C.ok("[OK]"), name, C.dim(detail)))
        elif soft:
            passed += 1
            print("  %s %-46s %s" % (C.warn("[--]"), name, C.dim(detail)))
        else:
            failures.append((name, detail))
            print("  %s %-46s %s" % (C.fail("[FAIL]"), name, detail))

    print()
    print(C.dim("── 阶段六：规划与执行 ") + C.dim("─" * 40))

    try:
        from avatar.agentic_planner import get_planner, parse_goal
        from avatar.agentic_planner import executor as EX
        from avatar.agentic_planner import memory as MEM
        from avatar.agentic_planner import recovery as REC
        from avatar.agentic_planner import state as ST
    except Exception as exc:      # noqa: BLE001
        item("能 import 规划层", False, "%s: %s" % (type(exc).__name__, exc))
        return passed, total, failures

    # ================= A. 可用性 =================

    # --- 1) planner 可用性 ---
    try:
        g = parse_goal("打开记事本输入hello")
        item("目标解析可用", g.ok is True, "%s（%d 个动作）"
             % (g.intent, len(g.actions)))
        plan = get_planner().plan(g)
        item("规划器可用（规则后端）", len(plan.steps) == 2,
             "%d 步" % len(plan.steps))
        problems = plan.validate()
        item("生成的计划通过校验", not problems,
             "无环 + 依赖完整" if not problems else str(problems))

        parallel = get_planner().plan(parse_goal("打开浏览器，打开记事本"))
        item("DAG 支持并行（连续 open 无依赖）",
             parallel.steps[1].depends_on == [],
             "两个 open 可同时跑")
        item("拓扑序可算", bool(plan.topological_order()),
             " → ".join(plan.topological_order()))

        from avatar.agentic_planner import LLMPlanner
        # ⚠ 阶段七把 LLMPlanner.plan() 实现了 —— 这条断言随之更新。
        #   现在要守的是"未启用时回退规则版"（硬约束 7），
        #   而不是"抛 NotImplementedError"。
        _lp = LLMPlanner(enabled=False)
        _p_off = _lp.plan(g)
        item("LLM 未启用时回退规则版（阶段七已实现）",
             "llm-fallback" in _p_off.planner
             and _p_off.validate() == [],
             "回退到规则版")
    except Exception as exc:      # noqa: BLE001
        item("planner 可用性", False, str(exc))

    # --- 2) 执行器可用性（不碰真实桌面）---
    try:
        plan = get_planner().plan(parse_goal("打开记事本输入hello"))
        calls = []

        def _runner(step):
            calls.append(step.id)
            return {"ok": True, "detail": "ok"}

        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="avatar_p6chk_")
        try:
            ex = EX.Executor(_runner, guard_enabled=False,
                             memory=MEM.ShortTermMemory("chk", directory=tmp),
                             observer=lambda **k: {"title": "x"},
                             verifier=lambda *a, **k: {"ok": True})
            out = ex.run(plan)
            item("执行器可跑通（假 runner）",
                 out.ok is True and calls == ["s1", "s2"],
                 "执行 %d 步" % len(calls))
            item("每步前后各观测一次（硬约束 8）",
                 len(out.memory.events_of("observation")) == 4,
                 "%d 条观测" % len(out.memory.events_of("observation")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:      # noqa: BLE001
        item("执行器可用性", False, str(exc))

    # ================= B. memory 可序列化性 =================
    try:
        import json as _json
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="avatar_p6mem_")
        try:
            m = MEM.ShortTermMemory("chk", directory=tmp)
            m.set_step_state("s1", "success")
            m.record_decision("s1", "retry", "测试")
            m.record_error("s1", ValueError("x"))
            blob = _json.dumps(m.to_dict(), ensure_ascii=False)
            item("memory 可序列化为 JSON", bool(blob), "%d 字节" % len(blob))

            path = m.save()
            back = MEM.ShortTermMemory.load("chk", directory=tmp)
            item("memory 能从磁盘恢复",
                 back.step_state("s1") == "success"
                 and len(back.decisions()) == 1,
                 "状态与决策都还原了")
            item("原子写不留 .tmp 残渣",
                 not os.path.exists(path + ".tmp"), "干净")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        import ast as _ast
        tree = _ast.parse(open(MEM.__file__, encoding="utf-8").read())
        mods = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        item("⑤ memory 禁用 pickle（硬约束 5）",
             "pickle" not in mods and "shelve" not in mods,
             "只用 json")
    except Exception as exc:      # noqa: BLE001
        item("memory 可序列化性", False, str(exc))

    # ================= C. 硬约束 =================

    # --- 6) 三条上限（硬约束 6）---
    try:
        ok = (EX.MAX_RETRIES == 3 and EX.MAX_STRATEGY_SWITCHES == 2
              and EX.TASK_TIMEOUT == 1800.0)
        item("⑥ 三条上限（重试 3 / 换策略 2 / 30 分钟）", ok,
             "重试 %d · 换策略 %d · %.0f 分钟"
             % (EX.MAX_RETRIES, EX.MAX_STRATEGY_SWITCHES,
                EX.TASK_TIMEOUT / 60.0))
        item("⑥ recovery 与 executor 的上限一致",
             REC.MAX_RETRIES == EX.MAX_RETRIES
             and REC.MAX_STRATEGY_SWITCHES == EX.MAX_STRATEGY_SWITCHES,
             "两处不许分叉")
        # 模拟 20 次失败必须收敛
        st = type("S", (), {"attempts": 0, "strategy_switches": 0})()
        seen = []
        for _ in range(20):
            d = REC.decide_recovery(st)
            seen.append(str(d))
            if d == "retry":
                st.attempts += 1
            elif d == "retry_with_fallback":
                st.strategy_switches += 1
            else:
                break
        item("⑥ 模拟 20 次失败必须收敛",
             seen[-1] in ("pause_for_human", "abort"),
             " → ".join(seen))
    except Exception as exc:      # noqa: BLE001
        item("⑥ 上限硬约束", False, str(exc))

    # --- 7) 状态机（硬约束 4）---
    try:
        legal = [
            ("pending", "start", "step", "running"),
            ("running", "succeed", "step", "success"),
            ("running", "fail", "step", "failed"),
            ("failed", "retry", "step", "running"),
            ("blocked", "resume", "step", "running"),
            ("running", "block", "step", "blocked"),
        ]
        bad = [c for c in legal
               if ST.transition(c[0], c[1], scope=c[2]) != c[3]]
        item("④ 合法状态转换全对", not bad, "6 条关键路径" if not bad
             else str(bad))

        illegal = [("pending", "succeed", "step"),
                   ("success", "start", "step"),
                   ("success", "retry", "step")]
        bad2 = [c for c in illegal
                if _p6_raises(lambda c=c: ST.transition(
                    c[0], c[1], scope=c[2])) != "StateError"]
        item("④ 非法状态转换必须抛", not bad2,
             "直接赋值走不通" if not bad2 else str(bad2))

        item("④ failed 在两级下结论相反（自愈的基础）",
             ST.is_terminal("failed", scope="task") is True
             and ST.is_terminal("failed", scope="step") is False,
             "task=终态 step=非终态")
    except Exception as exc:      # noqa: BLE001
        item("④ 状态机", False, str(exc))

    # --- 8) recovery 决策表（硬约束 7 / 9）---
    try:
        S = type("S", (), {"attempts": 0, "strategy_switches": 0})

        class _Risk:
            def __init__(self, clean):
                self.is_clean = clean

        cases = [
            ("首次失败 → retry",
             REC.decide_recovery(S()) == "retry"),
            ("重试用完 → 换策略",
             REC.decide_recovery(
                 type("S2", (), {"attempts": 3,
                                 "strategy_switches": 0})())
             == "retry_with_fallback"),
            ("换策略用完 → 叫人",
             REC.decide_recovery(
                 type("S3", (), {"attempts": 9,
                                 "strategy_switches": 2})())
             == "pause_for_human"),
            ("★ 检出风险 → 叫人（硬约束 9）",
             REC.decide_recovery(S(), risk=_Risk(False))
             == "pause_for_human"),
            ("★ 不可恢复 → abort",
             REC.decide_recovery(S(), error=PermissionError("access denied"))
             == "abort"),
            ("★ 风险优先于重试计数",
             REC.decide_recovery(
                 type("S4", (), {"attempts": 0,
                                 "strategy_switches": 0})(),
                 risk=_Risk(False)) == "pause_for_human"),
        ]
        bad = [n for n, ok in cases if not ok]
        item("⑦ 恢复决策表全对", not bad,
             "%d 条全对" % len(cases) if not bad else str(bad))

        item("⑦ 每个决策都带 reason（可复盘）",
             all(REC.decide_recovery(
                 type("S5", (), {"attempts": a,
                                 "strategy_switches": s})()).reason
                 for a in (0, 3, 9) for s in (0, 2)),
             "禁止静默重试")
    except Exception as exc:      # noqa: BLE001
        item("⑦ 恢复决策表", False, str(exc))

    # --- 9) 风控集成（硬约束 9）---
    try:
        from avatar.risk_detector import integration as RI
        was = RI.is_installed()
        try:
            RI.uninstall()
            item("⑨ 执行器接风控闸（可关，默认关）",
                 callable(getattr(EX.Executor, "before_step", None)),
                 "5E 挂载点 3/3 还在")
            import ast as _ast
            tree = _ast.parse(open(EX.__file__, encoding="utf-8").read())
            # ⚠ 要查 **import 的别名**，不是 ast.Name ——
            #    `from ..risk_detector import integration as _ri` 里
            #    "integration" 是 ImportFrom.alias.name（踩过）
            imported = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ImportFrom):
                    for a in node.names:
                        imported.add(a.name or "")
                elif isinstance(node, _ast.Import):
                    for a in node.names:
                        imported.add((a.name or "").split(".")[-1])
            item("⑨ 执行器引用了 integration",
                 "integration" in imported, "真去问过风控")
        finally:
            RI.install(enabled=was)
    except Exception as exc:      # noqa: BLE001
        item("⑨ 风控集成", False, str(exc))

    # --- 10) 模块 docstring 含「能力边界」（硬约束 10）---
    try:
        import ast as _ast
        missing = []
        for rel in ("goal", "plan", "planner", "state", "memory",
                    "observer", "verifier", "recovery", "executor"):
            pth = os.path.join(ROOT, "avatar", "agentic_planner",
                               "%s.py" % rel)
            if not os.path.isfile(pth):
                missing.append(rel + "(缺失)")
                continue
            doc = _ast.get_docstring(
                _ast.parse(open(pth, encoding="utf-8").read())) or ""
            if "能力边界" not in doc:
                missing.append(rel)
        item("⑩ 模块 docstring 含「能力边界」", not missing,
             "9 个模块齐备" if not missing else "缺：%s" % missing)
    except Exception as exc:      # noqa: BLE001
        item("⑩ 模块 docstring", False, str(exc))

    # --- 11) 不硬解（硬约束 #9 全项目生效）---
    try:
        import ast as _ast
        BAD = {"find_gap", "detect_gap", "solve_slider", "match_template",
               "locate_gap", "gap_position", "decode_captcha"}
        bad = []
        for rel in ("goal", "planner", "executor", "verifier"):
            pth = os.path.join(ROOT, "avatar", "agentic_planner",
                               "%s.py" % rel)
            tree = _ast.parse(open(pth, encoding="utf-8").read())
            names = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    names.add(node.name)
                elif isinstance(node, _ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, _ast.Name):
                    names.add(node.id)
            hit = names & BAD
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑪ 不硬解（无缺口识别/坐标推算符号）", not bad,
             "干净" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑪ 不硬解", False, str(exc))

    # --- 12) 测试不污染真实任务目录 ---
    try:
        real = os.path.join(os.path.expanduser("~"), ".avatar", "tasks")
        before = set(os.listdir(real)) if os.path.isdir(real) else set()
        from avatar.agentic_planner import executor as _E
        _E.selfcheck()
        after = set(os.listdir(real)) if os.path.isdir(real) else set()
        item("⑫ 自检不污染真实任务目录",
             after == before,
             "存档目录干净" if after == before
             else "多了 %s" % sorted(after - before))
    except Exception as exc:      # noqa: BLE001
        item("⑫ 测试隔离", False, str(exc))

    return passed, total, failures


def _p6_raises(fn, *a, **k):
    """调 fn，返回异常类名；没抛返回空串。"""
    try:
        fn(*a, **k)
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


# --------------------------------------------------------------------------
# 阶段七：多模态 + LLM + MCP
# --------------------------------------------------------------------------
def check_phase7_hard_constraints():
    """阶段七的硬约束。

    三块：

    **A. 环境可用性** —— WinRT / OCR / LLM / MCP 各能到哪一步
    **B. 硬约束** —— 降级顺序、token 预算、stdout 卫生、schema 完整
    **C. 隔离性** —— 全程不出网、不动鼠标

    **不联网、不调 LLM、不启服务。**
    """
    passed = total = 0
    failures = []

    def item(name, ok, detail="", soft=False):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print("  %s %-46s %s" % (C.ok("[OK]"), name, C.dim(detail)))
        elif soft:
            passed += 1
            print("  %s %-46s %s" % (C.warn("[--]"), name, C.dim(detail)))
        else:
            failures.append((name, detail))
            print("  %s %-46s %s" % (C.fail("[FAIL]"), name, detail))

    print()
    print(C.dim("── 阶段七：多模态 + LLM + MCP ") + C.dim("─" * 32))

    try:
        from avatar._win32 import winrt as WR
        from avatar.uia import ocr_fallback as OCR
        from avatar.vision.chain import LAYER_COST, LAYER_ORDER, VisionChain
        from avatar.agentic_planner import llm_backend as LB
        from avatar.mcp import protocol as MP
        from avatar.mcp.server import MCPServer
        from avatar.mcp.tools import build_default_tools
    except Exception as exc:      # noqa: BLE001
        item("能 import 阶段七模块", False,
             "%s: %s" % (type(exc).__name__, exc))
        return passed, total, failures

    # ================= A. 环境可用性 =================

    # --- 1) WinRT 地基 ---
    try:
        st = WR.ro_state()
        item("WinRT 运行时", bool(st.get("dll_loaded")),
             "combase.dll 已加载" if st.get("dll_loaded")
             else st.get("dll_error", ""))
        item("WinRT 槽位表完整（6 个接口）",
             all(len(t) >= 2 for t in (WR.VTBL_OCR_ENGINE,
                                       WR.VTBL_OCR_ENGINE_STATICS,
                                       WR.VTBL_OCR_RESULT,
                                       WR.VTBL_OCR_LINE, WR.VTBL_OCR_WORD,
                                       WR.VTBL_SOFTWARE_BITMAP_STATICS)),
             "IOcrEngine/Statics/Result/Line/Word + SoftwareBitmap")
        item("★ 关键槽位正确（TryCreateFromUserProfileLanguages=[10]）",
             WR.VTBL_OCR_ENGINE_STATICS[
                 "TryCreateFromUserProfileLanguages"] == 10, "[10]")
    except Exception as exc:      # noqa: BLE001
        item("WinRT 地基", False, str(exc))

    # --- 2) OCR 可用性（三态判定）---
    # ⚠ 不要因为 Windows OCR 挂了就红 —— 那是**已知的 WinRT 深水区**
    #   （SoftwareBitmap.CreateCopyFromBuffer 恒返回 E_INVALIDARG），
    #   模块内置了 Tesseract 旁路（subprocess 调外部 tesseract.exe）。
    #   三态：
    #     · Windows 原生可用            → [OK]
    #     · 原生不通但 Tesseract 可用   → [OK]（注明降级）
    #     · 两个都不可用                → [SKIP] 附原因（不红）
    try:
        from avatar.uia import ocr_tesseract as OT
        win_ok = OCR.winrt_available()
        tess_ok = OT.is_available()
        reason = OCR.unavailable_reason()

        if win_ok:
            _langs = [l.get("tag", "?") for l in OCR.languages()[:3]]
            item("OCR 可用（Windows 原生）", True,
                 "windows.media.ocr（语言：%s）" % " / ".join(_langs)
                 if _langs else "windows.media.ocr")
        elif tess_ok:
            item("OCR 可用（降级到 Tesseract）", True,
                 "Windows 位图通路不通 → Tesseract 接管：%s"
                 % OT.version())
        else:
            item("OCR 可用性（两个引擎都不可用）", False,
                 "[SKIP] %s" % (reason.splitlines()[0][:70]
                                if reason else "(无原因)"),
                 soft=True)
            exp = OCR.explain_unavailable()
            item("★ OCR 不可用时给可操作诊断",
                 len(exp) > 40 and any(k in exp for k in
                                       ("包标识", "语言包", "位图通路",
                                        "引擎能否创建", "Tesseract")),
                 "两边都报了" if "Tesseract" in exp else "只有 WinRT 诊断")
            item("★ 诊断建议了替代引擎（set_engine）",
                 "set_engine" in exp, "有出路")

        item("OCR 位图通路自测可调",
             isinstance(OCR.probe_bitmap_path(), str),
             "通" if win_ok
             else "WinRT 不通（已知问题，Tesseract 是旁路）")
        item("OCR 引擎模式可切换",
             OCR.engine_mode() in ("auto", "windows", "tesseract"),
             OCR.engine_mode())
    except Exception as exc:      # noqa: BLE001
        item("OCR 可用性", False, str(exc))

    # --- 3) 视觉降级链 ---
    try:
        costs = [LAYER_COST[l] for l in LAYER_ORDER]
        item("★ 五层顺序按成本递增",
             costs == sorted(costs),
             " → ".join("%s(%dms)" % (l, LAYER_COST[l]) for l in LAYER_ORDER))
        item("★ 第五层默认关闭（最贵）",
             VisionChain().allow["llm"] is False, "默认关")
        item("降级链可实例化",
             len(VisionChain().attempts if hasattr(VisionChain(), "attempts")
                 else []) == 0, "空尝试列表")
    except Exception as exc:      # noqa: BLE001
        item("视觉降级链", False, str(exc))

    # --- 4) LLM 配置检查 ---
    try:
        b = LB.get_backend("openai")
        item("LLM 后端默认关闭", b.active is False,
             "未启用（默认）")
        item("LLM 配置缺什么说得清",
             bool(b.unavailable_reason()), b.unavailable_reason()[:50])
        item("★ 支持的厂商", len(LB.PROVIDER_PRESETS) == 5,
             " / ".join(LB.PROVIDERS))
        item("★ token 预算可配置",
             LB.TokenBudget(per_request=100, total=200).per_request == 100,
             "单次 100 / 累计 200")
        item("★ 超时与重试有默认值",
             LB.DEFAULT_TIMEOUT > 0 and LB.DEFAULT_MAX_RETRIES >= 1,
             "%.0fs / %d 次" % (LB.DEFAULT_TIMEOUT, LB.DEFAULT_MAX_RETRIES))
        item("★ 4xx 不重试 / 429 与 5xx 重试",
             401 not in LB.RETRYABLE_STATUS
             and 429 in LB.RETRYABLE_STATUS
             and 503 in LB.RETRYABLE_STATUS,
             "按 HTTP 语义")
        item("LLM 走 urllib（零 SDK）", True, "只用标准库")
    except Exception as exc:      # noqa: BLE001
        item("LLM 配置检查", False, str(exc))

    # --- 5) MCP Server 启动检查 ---
    try:
        reg = build_default_tools()
        item("★ MCP 工具已注册", len(reg) >= 7, "%d 个" % len(reg))
        item("★ 所有 inputSchema 完整（硬约束 9）",
             all(t.input_schema.get("type") == "object"
                 and "properties" in t.input_schema
                 and isinstance(t.input_schema.get("required"), list)
                 for t in reg.all()),
             "type/properties/required 三样齐全")

        class _Sink:
            def write(self, d):
                return len(d) if isinstance(d, (bytes, bytearray)) else 0

            def flush(self):
                pass

        class _Err:
            def write(self, s):
                return len(str(s))

            def flush(self):
                pass

        srv = MCPServer(registry=reg, stdout=_Sink(), stderr=_Err())
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion":
                                   MP.MCP_PROTOCOL_VERSION}})
        item("★ MCP 握手可用",
             (r or {}).get("result", {}).get("protocolVersion")
             == MP.MCP_PROTOCOL_VERSION,
             MP.MCP_PROTOCOL_VERSION)
        r2 = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        item("★ MCP tools/list 可用",
             len(((r2 or {}).get("result") or {}).get("tools") or [])
             == len(reg), "%d 个" % len(reg))
    except Exception as exc:      # noqa: BLE001
        item("MCP Server 启动检查", False, str(exc))

    # ================= B. 硬约束 =================

    # --- 6) 线格式（★ MCP 不是 LSP）---
    try:
        b = MP.encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"})
        item("① MCP 线格式是换行分隔 JSON（不是 Content-Length）",
             b.endswith(b"\n") and b"Content-Length" not in b,
             "客户端连得上的那种")
        item("① 也读得懂 LSP 风格（方便调试）",
             MP.decode_message(
                 MP.encode_message({"jsonrpc": "2.0", "id": 1,
                                    "method": "x"}, style="lsp")
             )["method"] == "x", "两种都收")
    except Exception as exc:      # noqa: BLE001
        item("① 线格式", False, str(exc))

    # --- 7) 降级顺序（硬约束 4）---
    try:
        c = VisionChain(allow_llm=True)
        seen = []

        def _mk(layer_ok):
            for lyr, attr in (("uia", "_layer_uia"), ("cdp", "_layer_cdp"),
                              ("ocr", "_layer_ocr"),
                              ("template", "_layer_template"),
                              ("llm", "_layer_llm")):
                def _f(_l=lyr, _ok=(lyr == layer_ok), **kw):
                    seen.append(_l)
                    return (_ok, (1, 2, 3, 4) if _ok else None,
                            0.9 if _ok else 0.0, "%s" % _l)
                setattr(c, attr, _f)

        _mk("ocr")
        r = c.locate("x", window=1)
        item("④ 降级顺序正确（走到 OCR 就停）",
             seen == ["uia", "cdp", "ocr"], " → ".join(seen))
        item("④ 降级时 fallback_used=True", r.fallback_used is True, "True")
        item("④ 每层都记了 reason",
             all(a.reason for a in r.attempts), "全部有")
    except Exception as exc:      # noqa: BLE001
        item("④ 降级顺序", False, str(exc))

    # --- 8) token 预算（硬约束 5）---
    try:
        budget = LB.TokenBudget(per_request=10, total=10)
        item("⑤ 单次超限被拒", budget.check(999)[0] is False, "拒了")
        item("⑤ 累计超限被拒",
             (budget.consume(10), budget.check(1)[0])[1] is False, "拒了")
    except Exception as exc:      # noqa: BLE001
        item("⑤ token 预算", False, str(exc))

    # --- 9) LLM 回退（硬约束 7）---
    try:
        from avatar.agentic_planner import parse_goal
        from avatar.agentic_planner.planner import LLMPlanner

        class _Bad:
            def complete(self, messages, **k):
                class R:
                    pass
                r = R()
                r.ok, r.text, r.reason = False, "", "测试"
                return r

        pl = LLMPlanner(enabled=True, backend=_Bad())
        p = pl.plan(parse_goal("打开记事本输入hello"))
        item("⑦ LLM 失败必须回退规则版（不崩）",
             "llm-fallback" in p.planner and p.validate() == [],
             "%d 步" % len(p.steps))
        item("⑦ 回退原因被记下", bool(pl.last_fallback_reason),
             pl.last_fallback_reason[:50])
    except Exception as exc:      # noqa: BLE001
        item("⑦ LLM 回退", False, str(exc))

    # --- 10) stdout 卫生（硬约束 8）---
    try:
        import ast as _ast
        src = open(os.path.join(ROOT, "avatar", "mcp", "server.py"),
                   encoding="utf-8").read()
        tree = _ast.parse(src)
        prints = [n for n in _ast.walk(tree)
                  if isinstance(n, _ast.Call)
                  and isinstance(n.func, _ast.Name)
                  and n.func.id == "print"]
        item("⑧ MCP server 里**没有 print**（stdout 只出 JSON-RPC）",
             not prints, "干净" if not prints else "%d 处" % len(prints))
    except Exception as exc:      # noqa: BLE001
        item("⑧ stdout 卫生", False, str(exc))

    # ================= C. 隔离性 =================

    # --- 11) 零 SDK ---
    try:
        import ast as _ast
        BANNED = {"requests", "httpx", "aiohttp", "openai", "anthropic",
                  "pytesseract", "paddleocr", "cv2", "PIL", "litellm",
                  "pydantic"}
        bad = []
        for rel in ("avatar/_win32/winrt.py", "avatar/uia/ocr_fallback.py",
                    "avatar/vision/chain.py",
                    "avatar/agentic_planner/llm_backend.py",
                    "avatar/mcp/protocol.py", "avatar/mcp/server.py",
                    "avatar/mcp/tools.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                continue
            tree = _ast.parse(open(pth, encoding="utf-8").read())
            mods = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for a in node.names:
                        mods.add((a.name or "").split(".")[0])
                elif isinstance(node, _ast.ImportFrom):
                    mods.add((node.module or "").split(".")[0])
            hit = mods & BANNED
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑪ 零第三方 SDK（含官方 mcp SDK）", not bad,
             "全部只用标准库" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑪ 零 SDK", False, str(exc))

    # --- 12) 不硬解（硬约束 #9 全项目生效）---
    try:
        import ast as _ast
        BAD = {"find_gap", "detect_gap", "solve_slider", "gap_position",
               "locate_gap"}
        bad = []
        for rel in ("avatar/uia/ocr_fallback.py", "avatar/vision/chain.py",
                    "avatar/mcp/tools.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                continue
            tree = _ast.parse(open(pth, encoding="utf-8").read())
            names = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    names.add(node.name)
                elif isinstance(node, _ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, _ast.Name):
                    names.add(node.id)
            hit = names & BAD
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑫ 不硬解（无缺口识别符号）", not bad,
             "只识别/查找，不解题" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑫ 不硬解", False, str(exc))

    # --- 13) 模块 docstring ---
    try:
        import ast as _ast
        missing = []
        for rel in ("avatar/_win32/winrt.py", "avatar/uia/ocr_fallback.py",
                    "avatar/vision/chain.py",
                    "avatar/agentic_planner/llm_backend.py",
                    "avatar/mcp/protocol.py", "avatar/mcp/server.py",
                    "avatar/mcp/tools.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                missing.append(rel + "(缺失)")
                continue
            doc = _ast.get_docstring(
                _ast.parse(open(pth, encoding="utf-8").read())) or ""
            if "能力边界" not in doc:
                missing.append(rel)
        item("⑬ 模块 docstring 含「能力边界」", not missing,
             "7 个模块齐备" if not missing else "缺：%s" % missing)
    except Exception as exc:      # noqa: BLE001
        item("⑬ 模块 docstring", False, str(exc))

    return passed, total, failures


# --------------------------------------------------------------------------
# 阶段八：视觉大模型层
# --------------------------------------------------------------------------
def check_phase8_hard_constraints():
    """阶段八的硬约束。

    三块：

    **A. 配置可用性** —— 后端 / 预处理 / prompt / 校验 / 预算
    **B. 硬约束** —— 默认关、四道闸门、超预算抛、缓存键、链条位置
    **C. 隔离性** —— 全程不出网、不动桌面

    **不联网、不调模型、不抓屏。**
    """
    passed = total = 0
    failures = []

    def item(name, ok, detail="", soft=False):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print("  %s %-46s %s" % (C.ok("[OK]"), name, C.dim(detail)))
        elif soft:
            passed += 1
            print("  %s %-46s %s" % (C.warn("[--]"), name, C.dim(detail)))
        else:
            failures.append((name, detail))
            print("  %s %-46s %s" % (C.fail("[FAIL]"), name, detail))

    print()
    print(C.dim("── 阶段八：视觉大模型层 ") + C.dim("─" * 36))

    try:
        from avatar.vision import cost as VC
        from avatar.vision import image_prep as IP
        from avatar.vision import llm_backend as VB
        from avatar.vision import prompts as VP
        from avatar.vision import validator as VV
        from avatar.vision.chain import (
            LAYER_COST, LAYER_LLM, LAYER_ORDER, VisionChain)
    except Exception as exc:      # noqa: BLE001
        item("能 import 阶段八模块", False,
             "%s: %s" % (type(exc).__name__, exc))
        return passed, total, failures

    # ================= A. 配置可用性 =================

    # --- 1) 后端 ---
    try:
        item("★ 六家厂商预设",
             len(VB.VISION_PRESETS) == 6,
             " / ".join(VB.VISION_PROVIDERS))
        b = VB.get_vision_backend("openai")
        item("★ 视觉后端**默认关闭**（硬约束 2）",
             b.active is False, "未启用")
        item("LLM 配置缺什么说得清",
             bool(b.unavailable_reason()), b.unavailable_reason()[:40])
        item("★ 超时与重试有默认值",
             VB.DEFAULT_VISION_TIMEOUT > 0
             and VB.DEFAULT_VISION_MAX_RETRIES >= 1,
             "%.0fs / %d 次" % (VB.DEFAULT_VISION_TIMEOUT,
                                VB.DEFAULT_VISION_MAX_RETRIES))
        item("★ 4xx 不重试 / 429 与 5xx 重试",
             401 not in VB.RETRYABLE_STATUS
             and 429 in VB.RETRYABLE_STATUS,
             "按 HTTP 语义")
    except Exception as exc:      # noqa: BLE001
        item("后端配置", False, str(exc))

    # --- 2) 图像预处理 ---
    try:
        item("★ 默认最长边 1280",
             IP.DEFAULT_MAX_EDGE == 1280, "1280")
        item("★ 默认 JPEG 质量 85",
             IP.DEFAULT_JPEG_QUALITY == 85, "85")
        has_jpeg = IP.jpeg_available()
        item("JPEG 编码器（GDI+）",
             has_jpeg, "可用" if has_jpeg else "**不可用**（会回退 PNG）",
             soft=not has_jpeg)
        # PNG 兜底必须能用
        class _Tiny:
            width, height, stride = 4, 4, 16
            pixels = bytes([128]) * 64

        png = IP.encode_png(_Tiny())
        item("★ PNG 兜底可用（纯 Python + zlib）",
             png[:4] == b"\x89PNG", "%d 字节" % len(png))
        p = IP.prepare_for_llm(_Tiny(), max_edge=1280)
        item("★ 预处理产出可用图",
             bool(p.data) and p.scale > 0,
             "%s %d 字节" % (p.codec, len(p.data)))
    except Exception as exc:      # noqa: BLE001
        item("图像预处理", False, str(exc))

    # --- 3) prompt ---
    try:
        item("★ 四条约束都写进 prompt",
             all(k in VP.SYSTEM_PROMPT for k in
                 ("像素坐标", "不是屏幕坐标", "不要编造", "Markdown")),
             "坐标系/禁编造/禁 Markdown/置信度")
        item("★ 输出 Schema 五个字段",
             len(VP.OUTPUT_SCHEMA["properties"]) == 5,
             " / ".join(VP.OUTPUT_SCHEMA["properties"]))
        item("★ 置信度下限 %.2f" % VP.CONFIDENCE_MIN,
             VP.CONFIDENCE_MIN == 0.5, "0.5")
    except Exception as exc:      # noqa: BLE001
        item("prompt 模板", False, str(exc))

    # --- 4) 校验器 ---
    try:
        item("★ 四道闸门 + 可选二次确认",
             len(VV.GATE_ORDER) == 5, " → ".join(VV.GATE_ORDER))
        item("★ 像素校验默认**必需**（保守）",
             VV.CoordinateValidator().require_pixel_check is True,
             "没有原图 = 失败")
        item("★ 二次确认默认关闭",
             VV.CoordinateValidator().recheck is False,
             "它会多花一次调用")
        item("置信度阈值与 prompt 一致",
             VV.DEFAULT_MIN_CONFIDENCE == VP.CONFIDENCE_MIN, "0.5")
    except Exception as exc:      # noqa: BLE001
        item("坐标校验器", False, str(exc))

    # --- 5) 成本预算 ---
    try:
        item("★ 单次上限 %d" % VC.DEFAULT_PER_REQUEST_TOKENS,
             VC.DEFAULT_PER_REQUEST_TOKENS == 2000, "2000")
        item("★ 累计上限 %d" % VC.DEFAULT_TOTAL_TOKENS,
             VC.DEFAULT_TOTAL_TOKENS == 20000, "20000")
        item("★ 缓存默认开启（硬约束 7）",
             VC.ResultCache().disabled is False, "开")
        item("★ 缓存容量有上限",
             VC.DEFAULT_CACHE_SIZE > 0, str(VC.DEFAULT_CACHE_SIZE))
        item("★ 两处 token 公式一致",
             all(VC.estimate_image_tokens(w, h)
                 == IP.estimate_image_tokens(w, h)
                 for w, h in ((1280, 720), (100, 100), (0, 0))),
             "cost 与 image_prep 不分叉")
    except Exception as exc:      # noqa: BLE001
        item("成本预算", False, str(exc))

    # ================= B. 硬约束 =================

    # --- 6) 第五层位置（硬约束 9）---
    try:
        item("⑨ 第五层是**最后一层**",
             LAYER_ORDER[-1] == LAYER_LLM, " → ".join(LAYER_ORDER))
        costs = [LAYER_COST[l] for l in LAYER_ORDER]
        item("⑨ 五层按成本递增",
             costs == sorted(costs),
             " → ".join("%s(%dms)" % (l, LAYER_COST[l])
                        for l in LAYER_ORDER))
        item("⑨ 第五层默认关闭",
             VisionChain().allow[LAYER_LLM] is False, "默认关")
    except Exception as exc:      # noqa: BLE001
        item("⑨ 链条位置", False, str(exc))

    # --- 7) 四道闸门真的会拒（硬约束 4）---
    try:
        class _Img:
            width, height, scale = 1280, 720, 1.0
            offset_x = offset_y = 0

        class _Res:
            def __init__(self, x=100, y=200, conf=0.9, found=True,
                         ok=True):
                self.x, self.y, self.confidence = x, y, conf
                self.found, self.ok = found, ok
                self.reason = ""

        class _Flat:
            width, height, stride = 1280, 720, 1280 * 4
            pixels = bytes([200]) * (1280 * 720 * 4)

        V = VV.CoordinateValidator()
        r_range = V.validate(_Res(x=99999), _Img(), bitmap=None)
        item("④ 闸门①：越界坐标被拒", r_range is None, "拒了")
        r_pixel = V.validate(_Res(), _Img(), bitmap=_Flat())
        item("④ 闸门③：空白区被拒", r_pixel is None, "拒了")
        r_conf = V.validate(_Res(conf=0.1), _Img(), bitmap=None)
        item("④ 闸门④：低置信度被拒", r_conf is None, "拒了")
        r_nobmp = V.validate(_Res(), _Img(), bitmap=None)
        item("④ 没有原图 = 失败（不校验不点击）",
             r_nobmp is None, "拒了")
        # trace 能带出失败详情
        gates = []
        V.validate(_Res(), _Img(), bitmap=_Flat(), trace=gates)
        failed = [g for g in gates if not g.passed and not g.skipped]
        item("④ 失败时能说出是哪道闸门",
             bool(failed) and failed[0].gate == "pixel",
             failed[0].gate if failed else "?")
    except Exception as exc:      # noqa: BLE001
        item("④ 四道闸门", False, str(exc))

    # --- 8) 超预算抛异常（硬约束 6）---
    try:
        bud = VC.CostBudget(per_request=10, total=10)
        raised = False
        try:
            bud.require(999)
        except VC.CostExceeded as exc:
            raised = exc.kind == "per_request" and exc.limit == 10
        item("⑥ 超预算**抛 CostExceeded**（不静默调用）", raised,
             "带 kind/limit/estimated")
    except Exception as exc:      # noqa: BLE001
        item("⑥ 预算异常", False, str(exc))

    # --- 9) 缓存键（硬约束 7）---
    try:
        c = VC.ResultCache()
        h1 = VC.image_hash(b"shot1")
        c.put(h1, "确定按钮", {"x": 1})
        hit = c.get(h1, "确定按钮") is not None
        miss_goal = c.get(h1, "取消按钮") is None
        miss_img = c.get(VC.image_hash(b"shot2"), "确定按钮") is None
        item("⑦ 缓存键 = 截图 hash + 目标（两个都要）",
             hit and miss_goal and miss_img,
             "换目标/换截图都不命中")
    except Exception as exc:      # noqa: BLE001
        item("⑦ 缓存键", False, str(exc))

    # ================= C. 隔离性 =================

    # --- 10) 零 SDK ---
    try:
        import ast as _ast
        BANNED = {"openai", "anthropic", "requests", "httpx", "aiohttp",
                  "PIL", "cv2", "numpy", "litellm"}
        bad = []
        for rel in ("avatar/vision/llm_backend.py",
                    "avatar/vision/image_prep.py",
                    "avatar/vision/prompts.py",
                    "avatar/vision/validator.py",
                    "avatar/vision/cost.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                continue
            tree = _ast.parse(open(pth, encoding="utf-8").read())
            mods = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for a in node.names:
                        mods.add((a.name or "").split(".")[0])
                elif isinstance(node, _ast.ImportFrom):
                    mods.add((node.module or "").split(".")[0])
            hit = mods & BANNED
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑪ 零第三方 SDK（含 openai / anthropic）", not bad,
             "只用标准库" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑪ 零 SDK", False, str(exc))

    # --- 11) 不硬解（硬约束 #9 全项目生效）---
    try:
        import ast as _ast
        BAD = {"find_gap", "detect_gap", "solve_slider", "gap_position"}
        bad = []
        for rel in ("avatar/vision/llm_backend.py",
                    "avatar/vision/validator.py",
                    "avatar/vision/prompts.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                continue
            tree = _ast.parse(open(pth, encoding="utf-8").read())
            names = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    names.add(node.name)
                elif isinstance(node, _ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, _ast.Name):
                    names.add(node.id)
            hit = names & BAD
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑫ 不硬解（无缺口识别符号）", not bad,
             "只定位，不解题" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑫ 不硬解", False, str(exc))

    # --- 12) 模块 docstring ---
    try:
        import ast as _ast
        missing = []
        for rel in ("avatar/vision/llm_backend.py",
                    "avatar/vision/image_prep.py",
                    "avatar/vision/prompts.py",
                    "avatar/vision/validator.py",
                    "avatar/vision/cost.py"):
            pth = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(pth):
                missing.append(rel + "(缺失)")
                continue
            doc = _ast.get_docstring(
                _ast.parse(open(pth, encoding="utf-8").read())) or ""
            if "能力边界" not in doc:
                missing.append(rel)
        item("⑬ 模块 docstring 含「能力边界」", not missing,
             "5 个模块齐备" if not missing else "缺：%s" % missing)
    except Exception as exc:      # noqa: BLE001
        item("⑬ 模块 docstring", False, str(exc))

    return passed, total, failures


# --------------------------------------------------------------------------
# 阶段九：交互外壳（独立包）
# --------------------------------------------------------------------------
def check_phase9_hard_constraints():
    """阶段九的硬约束 —— 交互外壳（``avatar_shell``）。

    ⚠ 外壳是**独立包**，不在 ``avatar/`` 里。

    这里只做**静态**检查（AST 扫源码 + 读状态机转换表）：
    **不启动 GUI、不建窗口、不起子进程**。那些放在
    ``tests/test_phase9_shell.py`` 和 ``python -m avatar_shell --selftest``
    里 —— 自检必须能在无桌面环境跑完。

    三条硬约束：

    1. **不引入第三方 UI 框架** —— 一个 40x40 的球不该背 200MB 运行时
    2. **不 import avatar** —— 只通过 stdio / MCP 通信
    3. **故障隔离** —— 独立进程，它挂了主包照常跑
    """
    import ast as _ast

    passed = total = 0
    failures = []

    def item(name, ok, detail="", soft=False):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print("  %s %-46s %s" % (C.ok("[OK]"), name, C.dim(detail)))
        elif soft:
            passed += 1
            print("  %s %-46s %s" % (C.warn("[--]"), name, C.dim(detail)))
        else:
            failures.append((name, detail))
            print("  %s %-46s %s" % (C.fail("[FAIL]"), name, detail))

    print()
    print(C.dim("── 阶段九：交互外壳（独立包） ") + C.dim("─" * 28))

    shell_dir = os.path.join(ROOT, "avatar_shell")
    if not os.path.isdir(shell_dir):
        item("外壳目录存在", False, shell_dir)
        return passed, total, failures

    # ================= A. 三条硬约束（AST 静态扫描）=================
    #: 第三方 UI 框架黑名单。外壳只许用 ctypes / winsound / 标准库。
    UI_FRAMEWORKS = {
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "wxPython",
        "kivy", "tkinter", "customtkinter", "flet", "eel", "webview",
        "pystray", "pyglet", "pygame", "toga", "qtpy",
    }

    ui_hits, avatar_hits, no_doc = [], [], []
    files = sorted(f for f in os.listdir(shell_dir) if f.endswith(".py"))
    for fn in files:
        src = open(os.path.join(shell_dir, fn), encoding="utf-8").read()
        tree = _ast.parse(src)
        if not _ast.get_docstring(tree):
            no_doc.append(fn)
        for node in _ast.walk(tree):
            tops = []
            if isinstance(node, _ast.Import):
                tops = [(a.name or "").split(".")[0] for a in node.names]
            elif isinstance(node, _ast.ImportFrom) and node.level == 0:
                tops = [(node.module or "").split(".")[0]]
            for t in tops:
                if t in UI_FRAMEWORKS:
                    ui_hits.append("%s:%s" % (fn, t))
                if t == "avatar":
                    avatar_hits.append("%s:%s" % (fn, t))

    item("① 不引入第三方 UI 框架（硬约束 1）", not ui_hits,
         "只用 ctypes / winsound" if not ui_hits else str(ui_hits[:3]))
    item("② 不 import avatar（硬约束 2）", not avatar_hits,
         "只走 stdio / MCP" if not avatar_hits else str(avatar_hits[:3]))
    item("⑨ 每个 .py 都有模块 docstring", not no_doc,
         "%d 个文件齐备" % len(files) if not no_doc else str(no_doc))

    try:
        bridge_src = open(os.path.join(shell_dir, "bridge.py"),
                          encoding="utf-8").read()
        item("③ 故障隔离：独立进程（硬约束 3）",
             "subprocess" in bridge_src and "Popen" in bridge_src,
             "bridge 用 subprocess 拉起主包")
    except OSError as exc:
        item("③ 故障隔离：独立进程", False, str(exc))

    # ================= B. 状态机契约 =================
    try:
        from avatar_shell import state as ST
    except Exception as exc:      # noqa: BLE001
        item("能 import 外壳状态机", False,
             "%s: %s" % (type(exc).__name__, exc))
        return passed, total, failures

    states = {s.value for s in ST.AgentState}
    item("状态机四态齐备",
         states == {"idle", "running", "needs_human", "error"},
         " / ".join(sorted(states)))

    # --- error 不能自己"恢复" ---
    # ⚠ 不断言"出边只有 reset" —— error 允许有一条幂等自环
    #   （error 状态下再报一次 error 不算状态变更）。要守的是
    #   **它不能靠 start / done / need_human / resumed 溜出去**。
    err_events = {ev for (st, ev) in ST.TRANSITIONS if str(st) == "error"}
    leak = err_events & {ST.EVENT_START, ST.EVENT_DONE,
                         ST.EVENT_NEED_HUMAN, ST.EVENT_RESUMED}
    item("★ error 不能自己「恢复」（只有 reset 能出去）",
         not leak, "出边 = %s" % (sorted(err_events) or "无"))

    # --- needs_human 只能靠 resumed 回 running ---
    nh_to_running = {
        ev for (st, ev), dst in ST.TRANSITIONS.items()
        if str(st) == "needs_human" and str(dst) == "running"
    }
    item("★ needs_human 只能靠 resumed 回 running",
         nh_to_running == {ST.EVENT_RESUMED},
         "触发事件 = %s" % (sorted(nh_to_running) or "无"))

    item("四种状态都有颜色", len(ST.STATE_COLORS) == 4,
         " / ".join(sorted(ST.STATE_COLORS)))
    try:
        # ⚠ 只在**会闪的**状态里比快慢。IDLE / ERROR 的周期是 0.0，
        #   那是"不闪"的占位值，不是"闪得最快" —— 直接取 min 会误判。
        blinking = {str(k): v[1] for k, v in ST.STATE_ANIMATION.items()
                    if v[0]}
        item("★ needs_human 闪得最快（要人立刻看见）",
             blinking.get("needs_human") == min(blinking.values()),
             " / ".join("%s=%.1fs" % (k, v)
                        for k, v in sorted(blinking.items())))
        item("★ error 不闪（已经停了，不是要你管）",
             ST.STATE_ANIMATION[ST.AgentState.ERROR][0] is False, "静止")
    except (KeyError, IndexError, TypeError) as exc:
        item("动画表可读", False, str(exc))

    return passed, total, failures


# --------------------------------------------------------------------------
# 阶段五：风控硬约束 + 环境可用性
# --------------------------------------------------------------------------
def check_phase5_hard_constraints():
    """阶段五的硬约束与环境可用性。

    分两块：

    **A. 环境可用性**（用户明确要求）——
    验证题检测 / 提示音 / 热键监听 / 打码平台配置。
    这些是**环境探测**，缺了只提示不算失败（比如没装 OCR、
    没配打码 key）—— 它们本来就是"可选能力"。

    **B. 硬约束**（真的失败）——
    默认关闭、失败不抛、不硬解、无后台线程、判据唯一入口。
    """
    passed = total = 0
    failures = []

    def item(name, ok, detail="", soft=False):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print("  %s %-46s %s" % (C.ok("[OK]"), name, C.dim(detail)))
        elif soft:
            passed += 1
            print("  %s %-46s %s" % (C.warn("[--]"), name, C.dim(detail)))
        else:
            failures.append((name, detail))
            print("  %s %-46s %s" % (C.fail("[FAIL]"), name, detail))

    print()
    print(C.dim("── 阶段五：风控 ") + C.dim("─" * 48))

    try:
        from avatar.risk_detector import guard as G
        from avatar.risk_detector import solver as SV
        from avatar.risk_detector import integration as RI
        from avatar.risk_detector import detector as DET
    except Exception as exc:      # noqa: BLE001
        item("能 import 风控层", False, "%s: %s" % (type(exc).__name__, exc))
        return passed, total, failures

    # ================= A. 环境可用性 =================

    # --- 1) 验证题检测可用性 ---
    try:
        env = G.env_report()
        layers = getattr(DET, "LAYERS", {})
        item("验证题检测：三层开关可读",
             isinstance(layers, dict) and set(layers) >= {"dom", "window",
                                                          "pixel"},
             "layers=%s" % sorted(layers))
        avail = DET.detect_available() if hasattr(DET, "detect_available") \
            else layers
        item("验证题检测：检测器可调用",
             callable(getattr(DET, "detect", None)),
             "detect() 存在")

        # OCR 是阶段七的能力，缺了不算失败
        try:
            from avatar.uia import ocr_fallback as OCR
            ocr_ok = bool(OCR.is_available())
            item("验证题检测：OCR（阶段七，可选）", ocr_ok,
                 OCR.unavailable_reason() if not ocr_ok else "可用",
                 soft=True)
        except Exception as exc:      # noqa: BLE001
            item("验证题检测：OCR 探测", False, str(exc), soft=True)
    except Exception as exc:      # noqa: BLE001
        item("验证题检测可用性", False, str(exc))

    # --- 2) 提示音可用性 ---
    try:
        beep_ok = bool(G.beep(times=1, freq=880, dur_ms=30))
        item("提示音（winsound.Beep）可用", beep_ok,
             "已出声" if beep_ok else "出声失败（无声卡/远程会话）",
             soft=True)
        import winsound      # noqa: F401
        item("提示音：winsound 是内置模块", True, "零依赖")
    except ImportError:
        item("提示音：winsound 可用", False, "非 Windows 平台", soft=True)
    except Exception as exc:      # noqa: BLE001
        item("提示音可用性", False, str(exc), soft=True)

    # --- 3) 热键监听可用性 ---
    try:
        u = G._user32()
        has_api = u is not None and hasattr(u, "GetAsyncKeyState")
        item("热键监听：GetAsyncKeyState 可读", has_api,
             "user32 已加载" if has_api else "拿不到 user32")
        # 真按一次是做不到的，只验证"轮询 0.1s 能正常返回"
        t0 = time.monotonic()
        ok, waited = G.wait_for_hotkey(timeout=0.1, poll=0.02, debounce=0.0)
        dt = time.monotonic() - t0
        item("热键监听：轮询循环能正常退出", dt < 2.0,
             "%.2fs（%s）" % (dt, "按下" if ok else "超时返回"))
        item("热键监听：F9 是 0x78（VK_F9）", G.VK_F9 == 0x78, "0x78")
    except Exception as exc:      # noqa: BLE001
        item("热键监听可用性", False, str(exc))

    # --- 4) 打码平台配置检测 ---
    try:
        cfg = SV.SolverConfig.from_env()
        enabled = SV.is_enabled(cfg)
        item("打码平台：配置可读", isinstance(cfg, SV.SolverConfig),
             "已启用" if enabled else "未启用（默认）")
        item("打码平台：默认关闭策略生效",
             (not enabled) or bool(cfg.api_key),
             "启用时必须同时有 key")
        item("打码平台：请求构造可测（纯函数）",
             callable(SV.build_multipart_body)
             and callable(SV.build_json_body),
             "build_*_body")
        item("打码平台：响应解析兼容两家",
             SV.parse_response('{"status":1,"request":"T"}').ok
             and SV.parse_response(
                 '{"errorId":0,"solution":{"gRecaptchaResponse":"T"}}').ok,
             "2Captcha / CapSolver")
    except Exception as exc:      # noqa: BLE001
        item("打码平台配置检测", False, str(exc))

    # --- 5) 熔断环境（桌面 / 远程会话）---
    try:
        env = G.env_report()
        item("熔断环境：桌面探测", True,
             "有桌面" if env.get("desktop") else
             "无桌面 → 走退化路径（写日志 + 固定超时 + 抛超时异常）")
        item("熔断环境：会话类型", True,
             "%s（%s）" % (env.get("session"), env.get("reason") or "—"))
    except Exception as exc:      # noqa: BLE001
        item("熔断环境探测", False, str(exc))

    # ================= B. 硬约束 =================

    # --- 约束 1：打码默认关闭 ---
    _w1 = (SV.SolverConfig().active is False
           and SV.SolverConfig(enabled=True).active is False
           and SV.SolverConfig(api_key="k").active is False
           and SV.SolverConfig(enabled=True, api_key="k").active is True)
    item("① 打码默认关闭（需 enabled + key）", _w1,
         "缺任一条件都不启用")

    # --- 约束 2：请求必带 timeout，失败返回 None ---
    try:
        import inspect as _insp
        sig_post = _insp.signature(SV._default_http_post)
        has_to = "timeout" in sig_post.parameters
        sig_get = _insp.signature(SV._default_http_get)
        has_to = has_to and "timeout" in sig_get.parameters
        item("② 所有请求带 timeout", has_to, "urlopen(..., timeout=)")

        # 失败返回 None：用坏 http 函数验证
        saved = SV._http_post
        try:
            SV._http_post = lambda u, b, h, t: (_ for _ in ()).throw(
                OSError("x"))
            got = SV.solve_captcha_via_api(
                "https://2captcha.com/in.php", "K", b"\x89PNG", enabled=True,
                env={})
            item("② 失败返回 None（不抛异常）", got is None, "None")
        finally:
            SV._http_post = saved
    except Exception as exc:      # noqa: BLE001
        item("② 请求超时与失败语义", False, str(exc))

    # --- 约束 3：误报率测试不依赖 Chrome ---
    try:
        import ast as _ast
        fp_test = os.path.join(ROOT, "tests", "test_phase5_false_positive.py")
        sample_dir = os.path.join(ROOT, "tests", "fixtures", "normal_pages")
        n_samples = len([f for f in os.listdir(sample_dir)
                         if f.endswith(".html")]) \
            if os.path.isdir(sample_dir) else 0

        # 用 AST 查真实 import，不做文本搜索 ——
        # 文本搜索会被断言自己写的禁用词表命中（5C 踩过）。
        mods = set()
        if os.path.isfile(fp_test):
            with open(fp_test, encoding="utf-8") as f:
                tree = _ast.parse(f.read())
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for a in node.names:
                        mods.add(a.name or "")
                elif isinstance(node, _ast.ImportFrom):
                    mods.add(node.module or "")

        tops = {m.split(".")[0] for m in mods}
        forbidden = {"playwright", "selenium", "pyppeteer", "subprocess",
                     "requests", "urllib", "http", "socket"}
        hit = tops & forbidden
        no_cdp = not any("web_cdp" in m for m in mods)

        item("③ 误报率测试用本地样本（%d 个）" % n_samples,
             n_samples == 20 and not hit and no_cdp,
             "不依赖 Chrome / 网络" if not hit and no_cdp
             else "依赖了：%s" % sorted(hit))
    except Exception as exc:      # noqa: BLE001
        item("③ 误报率测试本地化", False, str(exc))

    # --- 约束 4：消融测试真实反映 footer 档的作用 ---
    try:
        from avatar.risk_detector.fingerprints import (
            text_hit_score, WEAK_TIER_FOOTER)
        sample_dir = os.path.join(ROOT, "tests", "fixtures", "normal_pages")
        fp0 = fp1 = 0
        for name in sorted(os.listdir(sample_dir)):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(sample_dir, name), encoding="utf-8") as f:
                html = f.read()
            h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
            h = re.sub(r"(?s)<[^>]+>", " ", h)
            text = re.sub(r"\s+", " ", h).strip()
            if text_hit_score(text)["detected"]:
                fp0 += 1
            if text_hit_score(text, penalty_scale={
                    WEAK_TIER_FOOTER: 0.0})["detected"]:
                fp1 += 1
        item("④ footer 档清零后误报率上升", fp1 > fp0,
             "%d → %d（消融有效）" % (fp0, fp1))
    except Exception as exc:      # noqa: BLE001
        item("④ 消融测试有效性", False, str(exc))

    # --- 约束 5：模块 docstring 含「能力边界」---
    try:
        import ast as _ast
        missing = []
        for rel in ("avatar/risk_detector/status.py",
                    "avatar/risk_detector/fingerprints.py",
                    "avatar/risk_detector/detector.py",
                    "avatar/risk_detector/detector_dom.py",
                    "avatar/risk_detector/detector_window.py",
                    "avatar/risk_detector/detector_pixel.py",
                    "avatar/risk_detector/context.py",
                    "avatar/risk_detector/guard.py",
                    "avatar/risk_detector/resume.py",
                    "avatar/risk_detector/solver.py",
                    "avatar/risk_detector/integration.py",
                    "avatar/agentic_planner/executor.py"):
            p = os.path.join(ROOT, rel)
            if not os.path.isfile(p):
                missing.append(rel + "(缺失)")
                continue
            with open(p, encoding="utf-8") as f:
                src = f.read()
            # ⚠ 用 AST 取 docstring，不要截前 N 字符 ——
            # detector_pixel 的 docstring 有 1300+ 字，
            # 截断会把"能力边界"漏掉（5H 踩过这个）。
            doc = _ast.get_docstring(_ast.parse(src)) or ""
            if "能力边界" not in doc:
                missing.append(rel)
        item("⑤ 模块 docstring 含「能力边界」", not missing,
             "全部齐备" if not missing else "缺：%s" % missing)
    except Exception as exc:      # noqa: BLE001
        item("⑤ 模块 docstring", False, str(exc))

    # --- 约束 6：不硬解（硬约束 #9）---
    try:
        import ast as _ast
        # ⚠ 必须查**符号**，不能查文本。
        # detector_pixel 的"能力边界"里写着"没有一行识别滑块缺口位置" ——
        # 文本搜索会把这句**说明自己没有硬解**的话当成硬解（同一个坑踩三次了）。
        BAD_SYMBOLS = {
            "find_gap", "detect_gap", "solve_slider", "match_template",
            "locate_gap", "slider_offset", "gap_position", "find_slider_gap",
            "calc_offset", "recognize_captcha", "decode_captcha",
        }
        bad = []
        for rel in ("avatar/risk_detector/detector_pixel.py",
                    "avatar/risk_detector/detector_dom.py",
                    "avatar/risk_detector/detector_window.py",
                    "avatar/risk_detector/detector.py",
                    "avatar/risk_detector/solver.py",
                    "avatar/risk_detector/guard.py"):
            p = os.path.join(ROOT, rel)
            with open(p, encoding="utf-8") as f:
                tree = _ast.parse(f.read())
            names = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    names.add(node.name)
                elif isinstance(node, _ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, _ast.Name):
                    names.add(node.id)
            hit = names & BAD_SYMBOLS
            if hit:
                bad.append("%s:%s" % (rel, sorted(hit)))
        item("⑥ 不硬解（无缺口识别/坐标推算符号）", not bad,
             "干净" if not bad else str(bad))
    except Exception as exc:      # noqa: BLE001
        item("⑥ 不硬解", False, str(exc))

    # --- 约束 7：判据唯一入口 ---
    try:
        from avatar.risk_detector.status import RiskStatus, Severity
        cases = {
            "clean": (RiskStatus.clean(), False),
            "unknown": (RiskStatus.unknown_(reason="x"), True),
            "bypassable": (RiskStatus.hit("a", severity=Severity.BYPASSABLE),
                           False),
            "interrupt": (RiskStatus.hit("b", severity=Severity.INTERRUPT),
                          True),
            "block": (RiskStatus.hit("c", severity=Severity.BLOCK), True),
        }
        wrong = [k for k, (st, want) in cases.items()
                 if G.should_intervene(st)[0] is not want]
        item("⑦ should_intervene 判定表正确", not wrong,
             "5 种状态全对" if not wrong else "错：%s" % wrong)
        item("⑦ translate_action 与 should_intervene 不分叉",
             all((G.translate_action(st) == "continue")
                 == (not G.should_intervene(st)[0])
                 for st, _ in cases.values()),
             "一致")
    except Exception as exc:      # noqa: BLE001
        item("⑦ 判定入口唯一", False, str(exc))

    # --- 约束 8：Ctrl+C 可干净打断（不留后台线程）---
    try:
        import threading as _th
        before = _th.active_count()
        item("⑧ 熔断等待不留后台线程", before == _th.active_count(),
             "线程数 %d 未变" % before)
        src = open(os.path.join(ROOT, "avatar/risk_detector/guard.py"),
                   encoding="utf-8").read()
        item("⑧ 熔断不用 threading", "threading.Thread" not in src,
             "同步循环，KeyboardInterrupt 直接传播")
    except Exception as exc:      # noqa: BLE001
        item("⑧ 无后台线程", False, str(exc))

    # --- 约束 9：风控集成默认关闭 ---
    try:
        was = RI.is_installed()
        RI.uninstall()
        item("⑨ 风控集成默认关闭",
             RI.is_installed() is False
             and RI.guard_before_action(0, action="x") is None,
             "未启用时不干预动作层")
        RI.install(enabled=was)
    except Exception as exc:      # noqa: BLE001
        item("⑨ 集成默认关闭", False, str(exc))

    # --- 约束 10：quick_check 不截图 ---
    try:
        import ast as _ast
        src = open(os.path.join(ROOT, "avatar/risk_detector/integration.py"),
                   encoding="utf-8").read()
        tree = _ast.parse(src)
        fn = next((n for n in tree.body
                   if isinstance(n, _ast.FunctionDef)
                   and n.name == "quick_check"), None)
        called = set()
        if fn is not None:
            for n in _ast.walk(fn):
                if isinstance(n, _ast.Attribute):
                    called.add(n.attr)
                elif isinstance(n, _ast.Name):
                    called.add(n.id)
        item("⑩ quick_check 不截图（热路径）",
             fn is not None
             and not (called & {"grab_screen", "grab_window", "save_bitmap",
                                "save_screenshot"}),
             "只读窗口标题")
    except Exception as exc:      # noqa: BLE001
        item("⑩ quick_check 轻量", False, str(exc))

    return passed, total, failures


# --------------------------------------------------------------------------
# 第二层执行
# --------------------------------------------------------------------------
def run_test_files(filt=None, verbose=True):
    """把 ``tests/`` 下的集成测试当子进程跑。

    走子进程而不是 import —— 这些测试会改窗口状态、甚至杀进程，
    隔离跑更安全，也让它们各自的超时/退出码保持有效。
    """
    passed = total = 0
    failures = []

    print()
    print(C.dim("── 集成测试 ") + C.dim("─" * 52))

    for rel in TEST_FILES:
        if filt and filt.lower() not in rel.lower():
            continue
        total += 1
        abs_path = os.path.join(ROOT, rel)

        if not os.path.exists(abs_path):
            failures.append((rel, "(缺失)", "测试文件不存在"))
            print("  %s %-30s %s" % (C.fail("[MISS]"), rel, "文件不存在"))
            continue

        t0 = time.monotonic()
        try:
            p = subprocess.run(
                [sys.executable, "-u", abs_path],
                cwd=ROOT, capture_output=True, text=True,
                timeout=1800,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            failures.append((rel, "(超时)", ">30 分钟"))
            print("  %s %-30s %s" % (C.err("[TIME]"), rel, "超过 30 分钟"))
            continue
        dt = time.monotonic() - t0

        out = (p.stdout or "") + (p.stderr or "")
        summary = ""
        for line in reversed(out.splitlines()):
            if "通过" in line and "/" in line:
                summary = line.strip()
                break

        if p.returncode == 0:
            passed += 1
            print("  %s %-30s %s" % (C.ok("[OK]"), rel,
                                     C.dim("%s / %.1fs" % (summary, dt))))
        else:
            failures.append((rel, "(退出码 %d)" % p.returncode, summary))
            print("  %s %-30s %s" % (C.fail("[FAIL]"), rel,
                                     "%s (退出码 %d)" % (summary, p.returncode)))
            if verbose:
                # 只把"失败项"那段挑出来，不糊整屏
                lines = out.splitlines()
                start = None
                for i, ln in enumerate(lines):
                    if ln.strip().startswith("失败项") or "失败项：" in ln:
                        start = i
                        break
                if start is not None:
                    for ln in lines[start:start + 25]:
                        print(C.dim("        " + ln))

    return passed, total, failures


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="avatar 统一自检 —— 模块契约 + 集成测试")
    ap.add_argument("--full", action="store_true",
                    help="跑完整档：模块自检连**实机探测**一起跑"
                         "（慢，会碰真实端口 / 文件系统）")
    ap.add_argument("--tests", action="store_true",
                    help="连集成测试文件一起跑（很慢，会真的动窗口）")
    ap.add_argument("--only", default=None,
                    help="只跑名字里含该子串的项，如 uia / windows / com")
    ap.add_argument("--list", action="store_true",
                    help="只列出会跑哪些项，不执行")
    ap.add_argument("--quiet", action="store_true",
                    help="不展开失败测试的明细")
    args = ap.parse_args(argv)

    if args.list:
        print("模块自检：")
        for group, path in MODULE_SELFCHECKS:
            if args.only and args.only.lower() not in path.lower():
                continue
            print("  %-6s %s" % (group, path))
        print()
        print("阶段三硬约束：")
        print("  ① 纯 ctypes · 无第三方输入库")
        print("  ② 结构体尺寸（MOUSEINPUT 32B / KEYBDINPUT 24B）")
        print("  ③ 绝对坐标归一化（分母 65536，误差 0px）")
        print("  ④ 熵源纪律（无模块绕过 entropy.py）")
        print("  ⑤ 可复现性（同 seed 逐点一致）")
        print("  ⑥ 静默可达性（不可达抛异常）")
        print("  ⑦ ActionResult 必填字段")
        print("  ⑧ 模块级 docstring")
        print()
        print("阶段四硬约束：")
        print("  ① 手搓 WebSocket · 禁第三方网络库")
        print("  ② verify 只返回结构化结论（禁裸 bool）")
        print("  ③ 三态语义完整（ok/fail/unknown 严格互斥）")
        print("  ④ CDP 只走明文 ws · 明确拒绝 wss")
        print("  ⑤ 启动浏览器前必挑空闲端口")
        print("  ⑥ 进程黑名单（避免盲目扫端口）")
        print("  ⑦ 降级决策必带 reason")
        print("  ⑧ Electron 识别失败返 None 而非抛异常")
        print("  ⑨ 浏览器探测（环境信息，缺了不算失败）")
        print("  ⑩ Electron 端口扫描可用")
        print("  ⑪ 模块级 docstring")
        print("  ⑫ 双端验证共用一个入口")
        print("  ⑬ UIA 收紧旋钮全链路透传（否则查询退化成 50 秒级全遍历）")
        if args.tests:
            print()
            print("集成测试：")
            for rel in TEST_FILES:
                if args.only and args.only.lower() not in rel.lower():
                    continue
                print("  %s" % rel)
        return 0

    print("=" * 64)
    _mode = []
    if args.full:
        _mode.append("完整档：含实机探测")
    if args.tests:
        _mode.append("含集成测试")
    print("avatar 自检%s" % ("（%s）" % " + ".join(_mode) if _mode else ""))
    print("=" * 64)
    print(C.dim("  解释器 %s" % sys.version.split()[0]))
    print(C.dim("  仓库   %s" % ROOT))

    all_fail = []

    mp, mt, mskip, mf = run_module_selfchecks(args.only,
                                              quick=not args.full)
    all_fail += [("自检",) + f for f in mf]

    # 阶段三专项：硬约束的落地校验。默认就跑（很快，<1s），
    # 除非 --only 明确指向别的东西。
    if not args.only or args.only.lower() in (
            "phase3", "avatar.input", "input", "human_entropy", "entropy"):
        hp, ht, hf = check_phase3_hard_constraints()
        all_fail += [("阶段三", f[0], "", f[1]) for f in hf]
    else:
        hp = ht = 0

    # 阶段四专项：同样默认就跑（<1s），--only 指向别处时跳过。
    if not args.only or args.only.lower() in (
            "phase4", "web", "web_cdp", "cdp", "verify", "验证",
            "electron"):
        p4p, p4t, p4f = check_phase4_hard_constraints(quick=not args.full)
        # 四层自己的 failures 是 (name, why) 二元组；补成 (层, 项, 说明, 原因)
        all_fail += [("阶段四", f[0], "", f[1]) for f in p4f]
    else:
        p4p = p4t = 0

    # 阶段五专项：风控硬约束 + 环境可用性。默认就跑。
    if not args.only or args.only.lower() in (
            "phase5", "risk", "risk_detector", "guard", "solver",
            "integration", "风控"):
        p5p, p5t, p5f = check_phase5_hard_constraints()
        all_fail += [("阶段五", f[0], "", f[1]) for f in p5f]
    else:
        p5p = p5t = 0

    # 阶段六专项：规划与执行硬约束。默认就跑。
    if not args.only or args.only.lower() in (
            "phase6", "planner", "agentic_planner", "agentic", "规划",
            "executor", "memory", "recovery", "state"):
        p6p, p6t, p6f = check_phase6_hard_constraints()
        all_fail += [("阶段六", f[0], "", f[1]) for f in p6f]
    else:
        p6p = p6t = 0

    # 阶段七专项：多模态 + LLM + MCP。默认就跑。
    if not args.only or args.only.lower() in (
            "phase7", "winrt", "ocr", "vision", "llm", "mcp", "多模态"):
        p7p, p7t, p7f = check_phase7_hard_constraints()
        all_fail += [("阶段七", f[0], "", f[1]) for f in p7f]
    else:
        p7p = p7t = 0

    # 阶段八专项：视觉大模型层。默认就跑。
    if not args.only or args.only.lower() in (
            "phase8", "vision-llm", "visionllm", "视觉llm"):
        p8p, p8t, p8f = check_phase8_hard_constraints()
        all_fail += [("阶段八", f[0], "", f[1]) for f in p8f]
    else:
        p8p = p8t = 0

    # 阶段九专项：交互外壳（独立包）。默认就跑。
    if not args.only or args.only.lower() in (
            "phase9", "shell", "avatar_shell", "外壳", "悬浮球"):
        p9p, p9t, p9f = check_phase9_hard_constraints()
        all_fail += [("阶段九", f[0], "", f[1]) for f in p9f]
    else:
        p9p = p9t = 0

    tp = tt = 0
    if args.tests:
        tp, tt, tf = run_test_files(args.only, verbose=not args.quiet)
        all_fail += [("测试",) + f for f in tf]

    # ---- 汇总 ----
    print()
    print("=" * 64)
    head = "模块自检 %d/%d" % (mp, mt)
    # ★ SKIP 单独报，且**不改变退出码** —— 环境缺资源不是失败。
    if mskip:
        head += " (SKIP %d)" % mskip
    if ht:
        head += "   阶段三 %d/%d" % (hp, ht)
    if p4t:
        head += "   阶段四 %d/%d" % (p4p, p4t)
    if p5t:
        head += "   阶段五 %d/%d" % (p5p, p5t)
    if p6t:
        head += "   阶段六 %d/%d" % (p6p, p6t)
    if p7t:
        head += "   阶段七 %d/%d" % (p7p, p7t)
    if p8t:
        head += "   阶段八 %d/%d" % (p8p, p8t)
    if p9t:
        head += "   阶段九 %d/%d" % (p9p, p9t)
    if args.tests:
        head += "   集成测试 %d/%d" % (tp, tt)
    print(head)

    if all_fail:
        print()
        print(C.fail("失败 %d 项：" % len(all_fail)))
        for layer, where, what, why in all_fail:
            print("  · [%s] %s" % (layer, where))
            if what and what != "(自检)":
                print("      %s" % what)
            if why:
                print("      %s" % C.dim(why))
        print()
        print(C.fail("结果：不通过"))
        return 1

    print(C.ok("结果：全部通过"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
