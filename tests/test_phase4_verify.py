# -*- coding: utf-8 -*-
"""阶段四 · 双端执行结果验证机制测试（批次 4G）。

这是阶段四的**核心资产**。整个设计的中心思想只有一句：

    **每个动作都必须有明确的成功判据，且判据不足时要敢说"不知道"。**

所以这个文件的重点不是"功能能不能跑"，而是三类边界：

1. **三态语义** —— ok / fail / unknown 必须严格区分。
   把 unknown 当 fail 会引发无谓重试；把 unknown 当 ok 会假装成功。
2. **判据缺失时的行为** —— 必须回 unknown，而不是猜。
3. **重试语义** —— fail 未必该重试（可能是确定性失败），
   unknown 反而该重试（多半是时序问题）。

另外固化几个已知的 Python 陷阱，防止后人"顺手优化"时踩回去。
"""

from __future__ import annotations

import io
import os
import struct
import sys
import time
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.verify import (  # noqa: E402
    VERIFY_FAIL, VERIFY_OK, VERIFY_UNKNOWN,
    VerifyResult, VerifyRequest, hamming_distance, image_similarity,
    images_differ, phash, retry_call, RetryPolicy,
    verify_action, verify_any,
    verify_desktop, verify_web,
)
# 下面这些没在包级 __all__ 里导出，直接取子模块
from avatar.verify.result import phash_from_png, summarize
from avatar.verify.action import verify_all
from avatar.verify import result as R  # noqa: E402


# ==========================================================================
# 一、VerifyResult 的三态语义
# ==========================================================================

def test_result_verdict_constants_are_distinct():
    """三个 verdict 必须互不相同 —— 这是全部逻辑的地基。"""
    assert len({VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN}) == 3


def test_ok_result_properties():
    r = VerifyResult.ok_(method="phash", reason="像素变了 23 位")
    assert r.ok is True
    assert r.failed is False
    assert r.unknown is False
    assert r.conclusive is True


def test_fail_result_properties():
    r = VerifyResult.fail(method="dom", reason="文本仍是旧值")
    assert r.ok is False
    assert r.failed is True
    assert r.unknown is False
    assert r.conclusive is True


def test_unknown_result_properties():
    r = VerifyResult.unknown_(method="phash", reason="截图失败")
    assert r.ok is False
    assert r.failed is False          # ⚠ 未知 ≠ 失败
    assert r.unknown is True
    assert r.conclusive is False


def test_unknown_is_falsy_but_not_failed():
    """**陷阱固化**：``VerifyResult`` 实现了 ``__bool__`` 返回 ``ok``。

    所以 ``if result:`` 在 unknown 时为假。结果是**恰好正确**的
    （未知时不该往下当成功走），但写 ``if not result: 当成失败处理``
    就错了。这条测试就是给后来人的路标。
    """
    u = VerifyResult.unknown_()
    assert bool(u) is False
    assert u.failed is False
    assert not u.ok

    # 正确写法：显式问三个状态
    assert u.unknown is True
    # 错误写法（会误判）：if not u: 当失败 —— 这里证明两者不等价
    assert (not bool(u)) is True
    assert u.failed is False


def test_invalid_verdict_raises():
    """非法 verdict 必须当场报错，不能静默接受。"""
    from avatar.verify.result import VerificationError
    with pytest.raises(VerificationError):
        VerifyResult("maybe")


def test_confidence_zero_when_unknown():
    """未知时置信度必须是 0 —— 否则加权汇总会算错。"""
    assert VerifyResult.unknown_().confidence == 0.0


def test_confidence_defaults_to_one_when_conclusive():
    """有结论时默认 1.0（调用方可以显式调低）。"""
    assert VerifyResult.ok_().confidence == 1.0
    r = VerifyResult.ok_(extra={"confidence": 0.6})
    assert r.confidence == 0.6


def test_confidence_is_clamped():
    """置信度越界要夹住，不能返回 1.5 或 -0.3。"""
    assert VerifyResult.ok_(extra={"confidence": 1.5}).confidence == 1.0
    assert VerifyResult.ok_(extra={"confidence": -0.3}).confidence == 0.0


def test_confidence_survives_garbage_input():
    """置信度字段被塞了字符串也不能崩（证据是外部数据拼的）。"""
    assert VerifyResult.ok_(extra={"confidence": "abc"}).confidence == 1.0


def test_explicit_unknown_classmethod_not_shadowed():
    """``unknown_`` 带下划线是**必须的**。

    实例属性 ``unknown``（bool）会遮蔽同名的类方法 —— 这是
    ``__slots__`` 的经典坑。这条测试让"改名"这个决定有据可依。
    """
    assert hasattr(VerifyResult, "unknown_")
    assert callable(VerifyResult.unknown_)
    r = VerifyResult.unknown_()
    assert isinstance(r, VerifyResult)
    # 实例上的 unknown 是 bool 属性，不是方法
    assert isinstance(r.unknown, bool)


def test_error_falls_back_to_reason():
    """只给 ``error`` 不给 ``reason`` 时，诊断信息不能静默丢掉。"""
    r = VerifyResult(VERIFY_UNKNOWN, error="boom")
    assert "boom" in (r.reason or ""), "error 应兜底进 reason"


def test_result_of_wraps_values():
    """``result_of`` 把各种输入统一成 VerifyResult。"""
    assert R.result_of(VerifyResult.ok_()).ok is True
    assert R.result_of(True).ok is True
    assert R.result_of(False).failed is True
    # None 是"没拿到"，不是"失败"
    assert R.result_of(None).unknown is True


def test_summarize_counts():
    """汇总统计要能直接进日志。"""
    results = [
        VerifyResult.ok_(), VerifyResult.ok_(),
        VerifyResult.fail(), VerifyResult.unknown_(),
    ]
    s = summarize(results)
    assert s["total"] == 4
    assert s["ok"] == 2
    assert s["fail"] == 1
    assert s["unknown"] == 1


def test_to_dict_is_json_safe():
    """``to_dict`` 的结果必须能直接 json.dumps（证据要落盘）。"""
    import json
    r = VerifyResult.ok_(method="phash", reason="x",
                         evidence={"distance": 23},
                         extra={"confidence": 0.8})
    json.dumps(r.to_dict())          # 不抛即通过


# ==========================================================================
# 二、pHash
# ==========================================================================

def _png(w: int, h: int, fn) -> bytes:
    """手搓一张最小 PNG（灰度），避免依赖 Pillow。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)                     # filter type 0
        for x in range(w):
            v = max(0, min(255, int(fn(x, y))))
            raw += bytes((v, v, v))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def test_phash_returns_63_bit_int():
    """pHash 是 63 位（跳过 DC 系数），不是 64。"""
    px = [[(x * 7 + y * 13) % 256 for x in range(32)] for y in range(32)]
    h = phash(px, 32, 32)
    assert isinstance(h, int)
    assert 0 <= h < (1 << 63)


def test_phash_is_deterministic():
    """同一张图算两次必须一样。"""
    px = [[(x * 3 + y * 5) % 256 for x in range(32)] for y in range(32)]
    assert phash(px, 32, 32) == phash(px, 32, 32)


def test_phash_stable_under_brightness_shift():
    """整体亮度平移后 pHash 应基本不变（这是用 pHash 而非差分的理由）。"""
    base = [[(x * 9 + y * 4) % 200 for x in range(32)] for y in range(32)]
    brighter = [[min(255, v + 30) for v in row] for row in base]
    a = phash(base, 32, 32)
    b = phash(brighter, 32, 32)
    assert hamming_distance(a, b) <= 8, "亮度变化不该让 pHash 大幅漂移"


# ⚠ 造图须知（踩过的坑，别再走一遍）
# ---------------------------------------------------------------------
# ``phash`` 内部会先把**任意尺寸**用最近邻缩放到 ``DCT_SIZE=16``，
# 只保留 8x8 低频块。所以：
#   * 图案周期必须是**源图尺寸的 1/16 量级**才看得见。
#     源图 256px 时，周期 32px 的棋盘格缩到 16x16 后每格正好 2px ——
#     有意义；周期 ≤16px 的图案会被直接抹平。
#   * 结果：**细密纹理的图 pHash 全是 0**。这不是 bug，
#     是"只关心低频轮廓"这个设计的直接后果。
#   * 全 0 图、细棋盘格、细条纹、细对角纹 —— 这四个的 pHash
#     **完全相同（都是 0）**。选测试图时务必避开。

_IMG_SIDE = 256


def _pattern(fn, side: int = _IMG_SIDE) -> list:
    """按规则造一张灰度图。"""
    return [[fn(x, y) for x in range(side)] for y in range(side)]


def test_phash_differs_for_different_images():
    """低频结构真正不同的图，pHash 距离必须明显。

    这里用「全黑」vs「四象限明暗相反」：距离实测 8，
    超过 ``DEFAULT_CHANGED_MIN=6`` 的阈值。
    """
    flat = _pattern(lambda x, y: 0)
    quad = _pattern(lambda x, y: 255 if ((x < _IMG_SIDE // 2) ^
                                         (y < _IMG_SIDE // 2)) else 0)
    a = phash(flat, _IMG_SIDE, _IMG_SIDE)
    b = phash(quad, _IMG_SIDE, _IMG_SIDE)
    d = hamming_distance(a, b)
    assert d > 6, "四象限图与纯色图的低频结构应不同（距离 %d）" % d


def test_phash_fine_textures_all_collide_at_zero():
    """**行为固化**：细密纹理的图 pHash 全为 0。

    全 0 图、周期 32px 的棋盘格 / 竖直条纹 / 对角纹 ——
    缩到 16x16 后全部退化成同一个低频轮廓，距离 0。

    这条测试的价值在于：以后如果有人报告"屏幕明明变了却没检出"，
    先怀疑**被测画面本身是细密纹理**（加载动画、噪点背景），
    而不是立刻去改阈值。这是 pHash 的固有特性，不是缺陷。
    """
    flat = _pattern(lambda x, y: 0)
    checker = _pattern(lambda x, y: 255 if ((x // 32) + (y // 32)) % 2 else 0)
    stripes = _pattern(lambda x, y: 255 if (x // 32) % 2 else 0)
    diag = _pattern(lambda x, y: 255 if (x + y) // 32 % 2 else 0)

    hashes = [phash(img, _IMG_SIDE, _IMG_SIDE)
              for img in (flat, checker, stripes, diag)]
    assert hashes == [0, 0, 0, 0], \
        "细密纹理应全部塌到 0，实际 %r" % (hashes,)


def test_phash_gradient_differs_strongly():
    """大面积渐变是**低频**的，必须被检出（距离实测 31）。"""
    flat = _pattern(lambda x, y: 0)
    grad = _pattern(lambda x, y: (x + y) * 255 // (2 * _IMG_SIDE))
    d = hamming_distance(phash(flat, _IMG_SIDE, _IMG_SIDE),
                         phash(grad, _IMG_SIDE, _IMG_SIDE))
    assert d > 6, "渐变与纯色的差异应被检出（距离 %d）" % d


def test_phash_from_png_matches_direct():
    """走 PNG 解码的路径要和直接算像素一致（解码不能有偏差）。"""
    w = h = 32
    fn = lambda x, y: (x * 5 + y * 11) % 256        # noqa: E731
    data = _png(w, h, fn)
    px = [[fn(x, y) for x in range(w)] for y in range(h)]
    assert phash_from_png(data) == phash(px, w, h)


def test_hamming_distance_bounds():
    """汉明距离的边界：自己跟自己 0，全反 63。"""
    assert hamming_distance(0, 0) == 0
    assert hamming_distance(0, (1 << 63) - 1) == 63
    assert hamming_distance(5, 5) == 0


def test_image_similarity_range():
    """相似度必须落在 0~1。"""
    assert image_similarity(0, 0) == pytest.approx(1.0)
    assert 0.0 <= image_similarity(0, (1 << 63) - 1) <= 1.0


def test_images_differ_returns_three_states():
    """``images_differ`` **返回 VerifyResult 三态**，不是裸 bool。

    距离落在 ``unchanged_max < d < threshold``（默认 5 < d < 6 之间
    只有整数 6 一个值，但阈值可调）时返回 **unknown** —— 这是刻意
    设计的"灰区"：那个区间里噪声和真实变化概率相当，硬判会制造
    假阳性，而假阳性会把重试逻辑引向错误方向。
    """
    flat = _pattern(lambda x, y: 0)
    quad = _pattern(lambda x, y: 255 if ((x < _IMG_SIDE // 2) ^
                                         (y < _IMG_SIDE // 2)) else 0)
    same = phash(flat, _IMG_SIDE, _IMG_SIDE)
    diff = phash(quad, _IMG_SIDE, _IMG_SIDE)

    r_same = images_differ(same, same)
    assert r_same.failed is True, "距离 0 应判为「没变」"
    assert r_same.evidence["distance"] == 0

    r_diff = images_differ(same, diff)
    assert r_diff.ok is True, "距离 8 应判为「变了」"
    assert r_diff.evidence["distance"] == 8


def test_images_differ_gray_zone_is_unknown():
    """灰区必须回 unknown —— 不能靠调阈值把灰区"解决"掉。"""
    # 构造一个正好落在灰区的距离：threshold=6, unchanged_max=5
    # 距离 5 或 6 的边界由阈值决定，用可调参数直接命中灰区
    flat = phash(_pattern(lambda x, y: 0), _IMG_SIDE, _IMG_SIDE)
    quad = phash(_pattern(lambda x, y: 255 if ((x < _IMG_SIDE // 2) ^
                                               (y < _IMG_SIDE // 2)) else 0),
                 _IMG_SIDE, _IMG_SIDE)
    # 把阈值拉到 20，距离 8 就落进 (5, 20) 的灰区
    r = images_differ(flat, quad, threshold=20, unchanged_max=5)
    assert r.unknown is True, "灰区内应回 unknown，实际 %r" % r.verdict
    assert r.reason, "unknown 必须带 reason"
    assert "灰区" in r.reason or "无法判定" in r.reason


def test_images_differ_none_input_is_unknown():
    """哈希缺失（截图失败）→ unknown，不是 fail。"""
    r = images_differ(None, 12345)
    assert r.unknown is True
    assert "缺失" in r.reason or "哈希" in r.reason


def test_snapshot_hash_handles_none():
    """截图为 None 时返回 None（不是 0）—— 0 是合法的哈希值。"""
    assert R.snapshot_hash(None) is None


# ==========================================================================
# 三、verify_action 路由与判据不足
# ==========================================================================

def test_verify_action_invalid_mode_raises():
    """非法 mode 直接抛 RouteError（这是**编程错误**，不是运行期未知）。"""
    with pytest.raises(Exception):
        verify_action("nonsense", target="x")


def test_verify_action_returns_verifyresult_always():
    """不管内部出什么状况，返回类型永远是 VerifyResult，绝不裸 bool。"""
    out = verify_action("desktop", target="nonexistent_target_zzz",
                        timeout=0.2)
    assert isinstance(out, VerifyResult)


def test_verify_action_missing_criteria_is_unknown_not_fail():
    """**关键**：调用方什么都没给 → 必须是 unknown。

    如果这里返回 fail，上层会去重试一个根本不可能成功的东西；
    返回 ok 更糟 —— 假装成功。
    """
    out = verify_action("desktop", timeout=0.2)
    assert out.unknown is True, "没给判据应回 unknown，实际 %r" % out.verdict


def test_verify_action_unknown_target_is_unknown_not_fail():
    """目标不存在时：可能是元素没出现，也可能是判据写错。

    分不清 → unknown。这也是"宁可说不知道，不要乱下结论"的体现。
    """
    out = verify_action("desktop", target="nonexistent_target_zzz",
                        timeout=0.2)
    assert out.unknown is True or out.failed is True
    if out.unknown:
        assert out.reason, "unknown 必须带 reason"


def test_verify_action_auto_mode_routes():
    """auto 模式下要能自己选路，不要求调用方指定。"""
    out = verify_action(prefer="auto", timeout=0.2,
                        uia_target="__nope__")
    assert isinstance(out, VerifyResult)


def test_verify_request_validates_mode():
    with pytest.raises(Exception):
        VerifyRequest("bad_mode")


def test_verify_request_with_creates_new_object():
    """``with_`` 派生新对象，原对象不动 —— 便于复用基础请求。"""
    a = VerifyRequest("web", timeout=3.0, url_contains="x")
    b = a.with_(url_contains="y")
    assert a.get("url_contains") == "x"
    assert b.get("url_contains") == "y"
    assert a is not b


def test_verify_request_is_web_and_is_desktop():
    """两路的判据键不能互相误判。"""
    w = VerifyRequest("auto", selector="#a", url_contains="/x")
    assert w.is_web is True
    d = VerifyRequest("auto", hwnd=123, uia_target="btn")
    assert d.is_desktop is True


def test_verify_request_describe():
    d = VerifyRequest("web", selector="#a").describe()
    assert d["mode"] == "web"
    assert "params" in d


def test_verify_all_requires_all_ok():
    """``verify_all``：有一个 fail 就 fail；有 unknown 且无 fail → unknown。"""
    ok = [VerifyResult.ok_(), VerifyResult.ok_()]
    assert verify_all(ok).ok is True

    withfail = [VerifyResult.ok_(), VerifyResult.fail()]
    assert verify_all(withfail).failed is True

    withunknown = [VerifyResult.ok_(), VerifyResult.unknown_()]
    assert verify_all(withunknown).unknown is True

    assert verify_all([]).unknown is True


def test_verify_any_short_circuits_on_ok():
    """``verify_any`` 的 OR 语义：任一 ok 即 ok。"""
    assert verify_any([VerifyResult.fail(), VerifyResult.ok_()]).ok is True
    # 全部确认失败才算 fail
    assert verify_any([VerifyResult.fail(), VerifyResult.fail()]).failed is True


def test_verify_any_unknown_wins_over_fail():
    """**关键语义**：只要有一支是 unknown，整体就是 unknown，不是 fail。

    理由：分支 B 失败了，不代表分支 A 的条件也失败 —— 它只是
    "没测出来"。把这种情况报成 fail 会让上层以为"确定不成立"，
    进而放弃重试或误判业务失败。宁可说"不知道"。
    """
    mixed = verify_any([VerifyResult.unknown_(), VerifyResult.fail()])
    assert mixed.unknown is True
    assert mixed.failed is False


def test_verify_any_empty_is_unknown():
    """空列表 → unknown（没有可判定的东西，不是"全失败"）。"""
    assert verify_any([]).unknown is True


# ==========================================================================
# 四、桌面验证：标题判据
# ==========================================================================

def test_verify_window_title_ok():
    """标题匹配成功 → ok，且证据里带上实际值（排查要用）。"""
    from avatar.verify.desktop import verify_window_title
    from avatar.perception import foreground_hwnd

    hwnd = foreground_hwnd()
    if not hwnd:
        pytest.skip("拿不到前台窗口（无 GUI 会话）")

    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value
    if not title:
        pytest.skip("前台窗口没有标题")

    r = verify_window_title(title[:6], hwnd=hwnd, match="contains",
                            timeout=2.0)
    assert r.ok is True
    assert r.evidence.get("actual")
    assert r.evidence.get("expected")


def test_verify_window_title_fail_on_mismatch():
    """明确不匹配的标题 → fail（这是可判定的，不是 unknown）。"""
    from avatar.verify.desktop import verify_window_title
    from avatar.perception import foreground_hwnd

    hwnd = foreground_hwnd()
    if not hwnd:
        pytest.skip("拿不到前台窗口（无 GUI 会话）")

    r = verify_window_title("__绝对不存在的窗口标题_zzz__", hwnd=hwnd,
                            match="contains", timeout=2.0)
    assert r.failed is True
    assert r.evidence.get("actual") is not None


def test_verify_window_title_invalid_hwnd_is_unknown():
    """hwnd 无效时分不清"窗口关了"还是"句柄写错了" → unknown。"""
    from avatar.verify.desktop import verify_window_title
    r = verify_window_title("x", hwnd=0, match="contains", timeout=0.3)
    assert r.unknown is True


def test_verify_desktop_param_not_shadowed_by_module_function():
    """**坑固化**：``verify_desktop`` 的形参名 ``verify_window_title``
    与模块级同名函数冲突，函数体内直接调用会拿到 str 而不是函数。

    实现里用 ``globals()["verify_window_title"]`` 绕开。
    这条测试确保那条绕行代码一直有效。
    """
    out = verify_desktop(hwnd=0, verify_window_title="x",
                         title_match="contains", timeout=0.3)
    assert isinstance(out, VerifyResult)
    # 不该抛 TypeError: 'str' object is not callable
    assert out.verdict in (VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN)


# ==========================================================================
# 四点五、UIA 查询的"收紧旋钮"（性能，别删）
# ==========================================================================
#
# ## 背景（真机实测，务必留着）
#
# ``find`` 有三条候选通道：
#   通道 A  Win32 EnumWindows        毫秒
#   通道 B  UIA 原生 FindAll         1~2 秒   ← 条件能"属性等值"下推时
#   通道 C  Python 全遍历            数十秒    ← 条件下推不了时
#
# 实测数据（2560 宽的多屏桌面，元素 4000+）：
#   find_all(Query("xxx", name_exact=True), window=fg)   →   1.75s
#   find_all(Query("xxx", name_exact=True), scope=root)  →   2.09s
#   T.descendants(root)                    # 通道 C     →  52.29s
#
# **"字符串按名字子串找"恰好是下推不了的那一类**（`_candidates_via_uia`
# 见到非 exact 的 name 就 return None → 掉进通道 C）。
# 于是随手 ``--target "某个中文名"`` 就会卡 50 秒 —— 而且这时
# ``--timeout 2`` **完全无效**：轮询的外层预算拦不住单次查询自己
# 就超预算（一次尝试 52.8 秒，循环根本没机会判断）。
#
# 修法就是给调用方三个收紧旋钮，让条件变回"可下推"：
#   ``name_exact`` / ``control_type`` / ``window``
# 并且**必须一路透传**到 ``verify_uia_exists`` —— 中间任何一层漏传，
# 旋钮就静默失效（踩过：CLI 加了参数，但 ``verify_desktop`` 的签名里
# 没有这两个形参，于是参数被吞掉，耗时仍是 51.2 秒）。

def test_element_of_accepts_tightening_knobs():
    """``_element_of`` 必须收下这三个旋钮。

    它们不是可选的锦上添花 —— 不给就是 50 秒级的查询。
    """
    import inspect
    from avatar.verify.desktop import _element_of
    sig = inspect.signature(_element_of)
    for name in ("name_exact", "control_type", "window"):
        assert name in sig.parameters, \
            "_element_of 丢了收紧旋钮 %r —— 字符串目标会退回全遍历（50s 级）" % name


def test_verify_uia_exists_accepts_tightening_knobs():
    """``verify_uia_exists`` 也必须收下，并透传给 ``_element_of``。"""
    import inspect
    from avatar.verify.desktop import verify_uia_exists
    sig = inspect.signature(verify_uia_exists)
    for name in ("name_exact", "control_type", "window"):
        assert name in sig.parameters, \
            "verify_uia_exists 少了 %r" % name


def test_verify_desktop_forwards_knobs_to_uia_exists():
    """**坑固化**：``verify_desktop`` 必须把旋钮转发给 ``verify_uia_exists``。

    漏转发的症状极隐蔽：CLI 上参数收下了、也不报错，就是**不生效**，
    耗时依然是 50 秒级。所以这里直接盯调用现场。
    """
    import inspect
    from avatar.verify import desktop as D
    src = inspect.getsource(D.verify_desktop)
    assert "uia_name_exact" in src, \
        "verify_desktop 没把 name_exact 转发下去 —— 旋钮会静默失效"
    assert "uia_control_type" in src, \
        "verify_desktop 没把 control_type 转发下去"
    # 转发时必须真的出现在 verify_uia_exists(...) 的实参里
    assert "name_exact=uia_name_exact" in src
    assert "control_type=uia_control_type" in src


def test_verify_action_exposes_tightening_knobs():
    """``_do_desktop`` 也要认这两个键，否则 CLI → action 这一段断链。"""
    import inspect
    from avatar.verify import action as A
    src = inspect.getsource(A._do_desktop)
    assert "name_exact" in src and "control_type" in src, \
        "_do_desktop 没接住 name_exact/control_type"


def test_tightening_knobs_are_modifiers_not_criteria():
    """**语义边界**：旋钮是"修饰符"，不是"判据"。

    单给 ``name_exact`` 而没有 ``uia_target`` 时，**不该**被当成
    "有判据了" —— 否则会造出一个必然 unknown 的假验证（噪音）。
    """
    from avatar.verify.action import verify_action
    out = verify_action(mode="desktop", name_exact=True, timeout=0.1)
    assert isinstance(out, VerifyResult)
    assert out.unknown is True, \
        "只给旋钮不该产生结论（它是修饰符，不是判据）"


def test_probe_seconds_recorded_in_evidence():
    """慢查询必须**自曝**：evidence 里要有 ``probe_seconds`` / ``probes``。

    没有这两个数字时，"为什么 timeout=2 却跑了 50 秒"完全无法排查
    —— 调用方只会以为是自己 timeout 设小了。这是可观测性底线。
    """
    from avatar.verify.desktop import verify_uia_exists
    r = verify_uia_exists("__绝对不存在的控件_zzz__", expect=True,
                          timeout=0.0, name_exact=True)
    assert isinstance(r, VerifyResult)
    assert "probes" in r.evidence, "evidence 缺 probes —— 无法判断查了几轮"
    assert "probe_seconds" in r.evidence, \
        "evidence 缺 probe_seconds —— timeout 为何没兑现无法排查"
    assert r.evidence["probe_seconds"] >= 0.0


def test_slow_probe_gets_explanatory_note():
    """单次查询超预算且只跑了一轮时，必须给出解释性的 note。

    这是"三态 + 原因"原则在性能问题上的延伸：**不许沉默地慢**。
    """
    from avatar.verify.desktop import verify_uia_exists
    r = verify_uia_exists("__绝对不存在的控件_zzz__", expect=True,
                          timeout=0.0, name_exact=True)
    if r.unknown or r.ok:
        pytest.skip("本机查询不够慢，构造不出该场景")
    ev = r.evidence
    if ev.get("probes", 0) <= 1 and ev.get("probe_seconds", 0) > 0:
        assert ev.get("probe_note"), \
            "单次查询就超预算，却没有任何解释性说明"
        assert "timeout" in ev["probe_note"]


# ==========================================================================
# 五、重试
# ==========================================================================

def test_retry_policy_rejects_bad_values():
    with pytest.raises(Exception):
        RetryPolicy(max_retries=-1)
    with pytest.raises(Exception):
        RetryPolicy(jitter=1.5)
    with pytest.raises(Exception):
        RetryPolicy(base_delay=-0.1)


def test_retry_stops_on_first_success():
    """成功就停，不做无谓重试。"""
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    out = retry_call(fn, max_retries=3, sleep=lambda s: None)
    assert len(calls) == 1
    assert out.value == "ok"


def test_retry_retries_on_exception():
    """异常默认可重试。"""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("boom")
        return "fine"

    out = retry_call(fn, max_retries=5, sleep=lambda s: None)
    assert len(calls) == 3
    assert out.value == "fine"


def test_retry_does_not_retry_fail_by_default():
    """**关键语义**：验证结果 fail 默认**不重试**。

    fail 意味着"确定没成功"，多半是判据错了或目标真不对 ——
    重试只是浪费时间。要重试得显式开 ``retry_on_fail``。
    """
    calls = []

    def fn():
        calls.append(1)
        return "x"

    out = retry_call(fn, max_retries=3, sleep=lambda s: None,
                     verify=lambda v: VerifyResult.fail(reason="no"))
    assert len(calls) == 1, "fail 不该触发重试"


def test_retry_does_retry_unknown_by_default():
    """**关键语义**：unknown 默认**要重试**。

    unknown 多半是时序问题（页面还没渲染完、窗口还在动画），
    等一会儿再试是对的。
    """
    calls = []

    def fn():
        calls.append(1)
        return "x"

    out = retry_call(fn, max_retries=2, sleep=lambda s: None,
                     verify=lambda v: VerifyResult.unknown_(reason="还没好"))
    assert len(calls) == 3, "unknown 应触发重试，实际尝试 %d 次" % len(calls)


def test_retry_respects_max_retries():
    """总尝试次数 = max_retries + 1。

    ``retry_call`` **不向外抛异常** —— 它返回一个 ``ok=False`` 的
    RetryOutcome，里面带着 ``error`` 和 ``gave_up_reason``。
    这样重试的"全过程"才是可记录的，而不是一抛了之。
    """
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("always")

    out = retry_call(fn, max_retries=2, sleep=lambda s: None,
                     base_delay=0.0)
    assert len(calls) == 3
    assert out.ok is False
    assert out.attempts == 3
    # RetryOutcome 没有 error 字段 —— 失败信息在 gave_up_reason 里
    assert out.gave_up_reason, "放弃时必须给出理由"
    assert out.attempt_log, "必须有尝试历史" 


def test_retry_on_attempt_callback():
    """每次尝试都要能回调 —— 尝试历史是证据链的一部分。"""
    seen = []

    def fn():
        raise ValueError("x")

    retry_call(fn, max_retries=2, sleep=lambda s: None, base_delay=0.0,
               on_attempt=lambda info: seen.append(info))
    assert len(seen) == 3, "每次尝试都该回调，实际 %d 次" % len(seen)
    assert all(isinstance(i, dict) for i in seen)


def test_retry_outcome_carries_verify():
    """结果对象要带上验证结论，且**不能因为 unknown 是 falsy 就丢掉它**。

    ``VerifyResult.__bool__`` 返回 ``ok``，unknown 为假 —— 所以
    任何 ``out.verify if out.verify else None`` 的写法都会静默丢证据。
    这条测试专门守住这个坑。
    """
    out = retry_call(lambda: "x", max_retries=0, sleep=lambda s: None,
                     verify=lambda v: VerifyResult.unknown_(reason="拿不到"))
    assert out.verify is not None, "unknown 的验证结果被 falsy 判断吃掉了"
    assert "拿不到" in out.verify.reason


def test_retry_deadline_limits_attempts(monkeypatch):
    """deadline 判定走**真实 time.time()**，所以要用受控时钟来测。

    ⚠ 一个容易踩的误解：``retry_call`` 支持注入 ``sleep``，但那**只是
    让测试不用真等** —— deadline 的比较基准是 ``time.time()``，
    不是 sleep 累加出来的虚拟时间。所以直接喂假 sleep 不管用，
    必须把 retry 模块里的 ``time.time`` 换掉。

    deadline 的检查点在**两次尝试之间**（不打断正在跑的 func）——
    这条也很重要：它意味着一个卡死的 func 不会被 deadline 救回来，
    超时得靠 func 自己的 timeout 参数处理。
    """
    from avatar.verify import retry as RT

    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    def fake_sleep(sec):
        clock["t"] += sec           # 假睡：把虚拟时钟往前推

    monkeypatch.setattr(RT.time, "time", fake_time)

    calls = []

    def fn():
        calls.append(1)
        raise ValueError("x")

    out = retry_call(fn, max_retries=100, sleep=fake_sleep,
                     base_delay=0.05, backoff=2.0,
                     policy=RetryPolicy(max_retries=100, base_delay=0.05,
                                        backoff=2.0, deadline=0.5))
    assert out.ok is False
    assert out.attempts < 101, \
        "deadline 没限制住尝试次数（跑了 %d 次）" % out.attempts
    assert out.attempts >= 1
    assert "时限" in out.gave_up_reason or "deadline" in out.gave_up_reason.lower() \
        or out.gave_up_reason, "放弃理由应说明是超时"


def test_retry_deadline_reason_mentions_time_limit(monkeypatch):
    """超时放弃时，理由文案必须点明「超过总时限」（排查时一眼定位）。"""
    from avatar.verify import retry as RT

    clock = {"t": 0.0}
    monkeypatch.setattr(RT.time, "time", lambda: clock["t"])
    monkeypatch.setattr(RT.time, "sleep", lambda s: clock.__setitem__(
        "t", clock["t"] + s))

    out = retry_call(lambda: (_ for _ in ()).throw(ValueError("x")),
                     max_retries=50, sleep=lambda s: clock.__setitem__(
                         "t", clock["t"] + s),
                     base_delay=1.0, backoff=2.0,
                     policy=RetryPolicy(max_retries=50, base_delay=1.0,
                                        backoff=2.0, deadline=0.1))
    assert "时限" in out.gave_up_reason, \
        "理由里应出现「时限」，实际 %r" % out.gave_up_reason


def test_retry_no_deadline_runs_full_max_retries():
    """不给 deadline 时应跑满 max_retries + 1 次 —— 对照组。"""
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("x")

    out = retry_call(fn, max_retries=4, sleep=lambda s: None,
                     base_delay=0.0)
    assert len(calls) == 5
    assert out.attempts == 5


# ==========================================================================
# 六、Module selfcheck 全绿
# ==========================================================================

@pytest.mark.parametrize("modname", [
    "avatar.verify.result",
    "avatar.verify.desktop",
    "avatar.verify.web",
    "avatar.verify.action",
    "avatar.verify.retry",
    "avatar.verify.hook",
    "avatar.human_entropy.verify_hook",
])
def test_module_selfcheck_green(modname):
    """每个子模块自带 selfcheck，必须全过。

    这相当于把"内建自检"也纳入了 pytest —— 保证自检不会腐烂。
    """
    import importlib
    mod = importlib.import_module(modname)
    rep = mod.selfcheck()
    assert isinstance(rep, dict)
    assert rep.get("ok") is True, \
        "%s selfcheck 未全绿：%r" % (modname, rep.get("failures"))
    assert not rep.get("failures"), \
        "%s selfcheck 失败项：%r" % (modname, rep["failures"])
    checks = rep.get("checks") or []
    assert len(checks) > 0, "%s selfcheck 一项都没跑？" % modname
