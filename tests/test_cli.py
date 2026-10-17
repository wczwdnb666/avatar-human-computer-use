# -*- coding: utf-8 -*-
"""CLI 参数解析与输出格式测试。

## 这份测试盯的是两个真实踩过的坑（2026-10-17）

### 坑 1：``_parse_hwnd`` 同名覆盖

``avatar/cli.py`` 里曾有两份同名函数，后定义的覆盖前一份。
传 Namespace 的调用点（``shot`` / ``geom`` / ``detect`` / ``verify``）
全部拿到 ``None``，最后崩在 ``capture.py`` 的 ``int(None)`` 上 ——
**报错点离真凶 900 行**，栈顶完全无辜。

教训：同名函数覆盖是**静默**的，Python 不警告。这里用测试焊死
"五种入参都必须正确"，以后谁再复制一份同名函数，测试会立刻红。

### 坑 2：``shot --out x.png`` 写出 BMP

``_write_bmp`` 曾无条件写 24 位 BMP，文件名是 ``.png`` 但内容是
``BM`` 开头 —— 图片查看器打不开，必须手动转一道。

这里断言**文件头魔数**：PNG 是 ``\\x89PNG\\r\\n\\x1a\\n``，BMP 是 ``BM``。

## 依赖说明

- ``_parse_hwnd`` 的用例是**纯函数测试**，零依赖、任何环境都能跑。
- ``shot`` 的 CLI 用例需要桌面；没有桌面时**软跳过**（不算失败）。
  这样 GitHub Actions 的 Windows runner 上也能绿。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar import cli
from avatar.vision.capture import Bitmap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: PNG 文件头魔数。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ==========================================================================
# 一、_parse_hwnd：五种入参
# ==========================================================================
def test_parse_hwnd_namespace():
    """★ 传 argparse Namespace（这是 shot/geom/detect/verify 的调用方式）。

    坑 1 的原始症状：这个用例在旧代码上会拿到 None。
    """
    ns = argparse.Namespace(hwnd="0x00030104")
    got = cli._parse_hwnd(ns)
    assert got == 0x00030104, "Namespace(hwnd='0x00030104') → %r" % got


def test_parse_hwnd_bare_int():
    """传裸 int，原样返回。"""
    assert cli._parse_hwnd(196868) == 196868


def test_parse_hwnd_hex_string():
    """传 ``0x`` 前缀字符串（大小写都认）。"""
    assert cli._parse_hwnd("0x000A041E") == 0x000A041E
    assert cli._parse_hwnd("0X30104") == 0x30104


def test_parse_hwnd_decimal_string():
    """传十进制字符串。"""
    assert cli._parse_hwnd("196868") == 196868


def test_parse_hwnd_garbage_raises_systemexit():
    """★ 空串 / 垃圾串**必须抛 SystemExit**，不能静默返回 None。

    静默返回 None 正是坑 1 的帮凶：错误被吞掉，崩在几百行之外。
    报错必须发生在**定义处**，带上是哪个值解析不了。
    """
    bad_inputs = ("", "   ", "not-a-number", "0xZZZ", "Namespace(hwnd=1)")
    for bad in bad_inputs:
        try:
            got = cli._parse_hwnd(bad)
        except SystemExit:
            continue
        raise AssertionError(
            "垃圾输入 %r 没有抛 SystemExit，而是返回了 %r —— "
            "静默吞错会让崩溃发生在几百行之外" % (bad, got))


def test_parse_hwnd_none_falls_back_or_raises():
    """``Namespace(hwnd=None)`` 走前台回退；没有前台窗口时抛 SystemExit。

    **不允许**返回 None —— 那正是坑 1 的形态。
    """
    ns = argparse.Namespace(hwnd=None)
    try:
        got = cli._parse_hwnd(ns)
    except SystemExit:
        return          # 无前台窗口：明确报错，算对
    assert isinstance(got, int) and got > 0, \
        "回退前台失败时应抛 SystemExit，不能返回 %r" % (got,)


def test_parse_hwnd_no_fallback_returns_zero():
    """``fallback=False`` 时 None 返回 0（调用方自己判断有没有窗口）。"""
    assert cli._parse_hwnd(None, fallback=False) == 0


# ==========================================================================
# 二、_write_bmp：按后缀分流
# ==========================================================================
def _tiny_bitmap():
    """4x4 的纯色位图，够测文件头。"""
    return Bitmap(width=4, height=4, origin=(0, 0),
                  pixels=bytearray([40, 40, 40, 255] * 16))


def test_write_png_by_suffix():
    """★ ``.png`` 后缀必须写出 PNG 魔数，而不是 BMP 的 ``BM``。"""
    d = tempfile.mkdtemp(prefix="avatar_cli_test_")
    path = os.path.join(d, "tiny.png")
    try:
        cli._write_bmp(path, _tiny_bitmap())
        with open(path, "rb") as f:
            head = f.read(8)
        assert head == PNG_MAGIC, \
            ".png 文件头应为 PNG 魔数，实际 %r" % (head,)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def test_write_bmp_by_suffix():
    """``.bmp`` 后缀仍写 BMP（``BM`` 开头），不能被 png 分支误伤。"""
    d = tempfile.mkdtemp(prefix="avatar_cli_test_")
    path = os.path.join(d, "tiny.bmp")
    try:
        cli._write_bmp(path, _tiny_bitmap())
        with open(path, "rb") as f:
            head = f.read(2)
        assert head == b"BM", ".bmp 文件头应为 BM，实际 %r" % (head,)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rmdir(d)
        except OSError:
            pass


# ==========================================================================
# 三、CLI 端到端：avatar shot --out x.png
# ==========================================================================
def test_shot_cli_writes_png():
    """★ 跑真的 CLI：``avatar shot --out test.png`` → 文件头是 PNG。

    需要桌面环境；抓不到屏时**软跳过**（GitHub runner 可能没有交互桌面）。
    """
    d = tempfile.mkdtemp(prefix="avatar_cli_shot_")
    out = os.path.join(d, "test.png")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "avatar", "shot",
             "--out", out, "--downsample", "4"],
            capture_output=True, text=True, timeout=90, cwd=ROOT)
        if r.returncode != 0:
            msg = (r.stderr or "")[-300:]
            # 没有桌面 = 环境缺资源，不是代码错
            if any(k in msg for k in ("无桌面", "拿不到", "桌面环境",
                                      "user32", "gdi32")):
                print("  [SKIP] 无桌面环境，跳过 shot CLI 用例")
                return
            raise AssertionError("shot 退出码 %d：%s"
                                 % (r.returncode, msg))
        assert os.path.isfile(out), "没生成文件：%s" % out
        with open(out, "rb") as f:
            head = f.read(8)
        assert head == PNG_MAGIC, \
            "CLI 写出的 .png 文件头不是 PNG 魔数：%r" % (head,)
        assert not head.startswith(b"BM"), \
            "文件名是 .png 但内容写成了 BMP —— 后缀分流没生效"
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def test_shot_cli_bmp_still_works():
    """``--out x.bmp`` 仍写 BMP，确认分流没把老路径搞坏。"""
    d = tempfile.mkdtemp(prefix="avatar_cli_shot_")
    out = os.path.join(d, "test.bmp")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "avatar", "shot",
             "--out", out, "--downsample", "8"],
            capture_output=True, text=True, timeout=90, cwd=ROOT)
        if r.returncode != 0:
            msg = (r.stderr or "")[-300:]
            if any(k in msg for k in ("无桌面", "拿不到", "桌面环境",
                                      "user32", "gdi32")):
                print("  [SKIP] 无桌面环境，跳过 shot CLI 用例")
                return
            raise AssertionError("shot 退出码 %d：%s"
                                 % (r.returncode, msg))
        with open(out, "rb") as f:
            head = f.read(2)
        assert head == b"BM", "BMP 文件头应为 BM，实际 %r" % (head,)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
        try:
            os.rmdir(d)
        except OSError:
            pass
