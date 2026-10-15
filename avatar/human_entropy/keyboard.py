# -*- coding: utf-8 -*-
"""真人化键盘：打字节奏。

人类打字的可测特征
------------------
1. **按键间隔不服从均匀分布，而是右偏的**。
   均值 130ms 左右，中位数 105ms，长尾到 400ms+。
   用对数正态或 Gamma 拟合，不要用 ``uniform(50,250)`` —— 那个
   均值 150ms 且完全对称，打出来像节拍器。

2. **词内快、词间慢**。同一个词里的字母间隔短（70-130ms），
   词与词之间的空格后间隔长（120-260ms）。

3. **难点字符慢**。需要 Shift 的大写字母、很少打的符号、
   键盘右上角的键，间隔明显更长。

4. **认知停顿**。句子结束时（. ! ?）会停 300-900ms；
   逗号后停 150-400ms。这不是"打字速度"，是"想下一句"。

5. **错误与回改**。真人按键错误率约 1-4%（取决于熟练度），
   发现错误后回改需要：删掉后面的字符 → 重打。这个行为让
   输入内容在时间轴上呈现"前进-后退-前进"的模式。

6. **WPM 波动**。整体速度不是恒定的，一段话内会有快慢变化。
   熟练打字员英文 40-80 WPM，中文拼音输入 25-50 字/分钟。

本模块的 ``type_text_human`` 会按上面六条生成事件序列。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .. import input as input_layer
from ..errors import ActionFailed
from . import entropy

__all__ = [
    "DEFAULT_WPM",
    "TypeResult",
    "keystroke_delay",
    "type_text_human",
    "press_key_human",
    "hotkey_human",
    "backspace_human",
    "clear_with_backspace",
    "estimate_duration",
    "wpm_to_delay",
]


#: 默认打字速度（英文单词/分钟）。中文按字符数折算见 wpm_to_delay。
DEFAULT_WPM = 320.0

# 需要用 Shift 的字符（US 布局）
_SHIFT_CHARS = set('~!@#$%^&*()_+{}|:"<>?')
# 慢速字符：键盘边缘、易错
_SLOW_CHARS = set("~!@#$%^&*()_+{}|:\"<>?`")
# 句子结束符 → 长停顿
_SENTENCE_END = set(".!?。！？")
# 子句分隔 → 中停顿
_CLAUSE_END = set(",;:，；：、")


@dataclass
class TypeResult:
    """一次真人化输入的结果。"""

    text: str
    typed: int = 0
    chars: int = 0
    words: int = 0
    duration: float = 0.0
    keystrokes: int = 0
    typos: int = 0
    corrections: int = 0
    pauses: int = 0
    wpm: float = 0.0
    human: bool = True

    def describe(self) -> dict:
        return {
            "chars": self.chars,
            "words": self.words,
            "duration": round(self.duration, 3),
            "keystrokes": self.keystrokes,
            "typos": self.typos,
            "corrections": self.corrections,
            "pauses": self.pauses,
            "wpm": round(self.wpm, 2),
            "human": self.human,
        }


# --------------------------------------------------------------------------
# 节奏计算
# --------------------------------------------------------------------------
def wpm_to_delay(wpm: float) -> float:
    """把 WPM 换算成平均每字符间隔（秒）。

    假定一个"词"= 5 个字符（业界惯例）。打字节奏里还包含停顿，
    所以实际平均间隔会大于纯击键间隔 —— 这里的返回值作为
    **击键中位间隔**的基准，停顿另行叠加。
    """
    if wpm <= 0:
        raise ActionFailed("WPM 必须为正数，收到 %r" % (wpm,), method="keyboard")
    chars_per_minute = wpm * 5.0
    seconds_per_char = 60.0 / chars_per_minute
    return seconds_per_char


def keystroke_delay(
    char: str,
    prev_char: str = "",
    next_char: str = "",
    *,
    base_delay: float = 0.115,
    skill: float = 1.0,
) -> float:
    """给定字符上下文，返回敲这个键之前该等多久（秒）。

    :param skill: 熟练度。1.0 = 普通；1.5 = 很快；0.6 = 很慢（新手）
    """
    # 基准：对数正态，median = base_delay，右偏长尾
    mu = math.log(max(0.012, base_delay / max(0.2, skill)))
    delay = entropy.lognormvariate(mu, 0.38)

    # 大写 / Shift 字符：多 25-70ms
    if char.isupper() or char in _SHIFT_CHARS:
        delay += entropy.uniform(0.025, 0.070)

    # 难打的字符
    if char in _SLOW_CHARS:
        delay += entropy.uniform(0.040, 0.115)

    # 连续同一个键（"ll"、"oo"）会有双字母加速
    if prev_char and char.lower() == prev_char.lower():
        delay *= entropy.uniform(0.72, 0.90)

    # 词首字符（前一个是空格）：略慢
    if prev_char in (" ", "\t", "\n", ""):
        delay += entropy.uniform(0.015, 0.065)

    # 连打同一个手指相邻键（近似：键盘水平邻位）会有轻微加速
    if prev_char and next_char:
        if abs(ord(char.lower()) - ord(prev_char.lower())) == 1 and char.isalpha():
            delay *= entropy.uniform(0.85, 0.98)

    return max(0.018, delay)


def _inter_char_pause(char: str, *, skill: float = 1.0) -> float:
    """字符之后的结构性停顿（句读、换行）。"""
    if char in _SENTENCE_END:
        # 句子结束：长停顿（思考）
        return entropy.clamped_gauss(0.52, 0.24, 0.18, 1.60) / max(0.5, skill)
    if char in _CLAUSE_END:
        return entropy.clamped_gauss(0.24, 0.12, 0.08, 0.75) / max(0.5, skill)
    if char == "\n":
        return entropy.clamped_gauss(0.35, 0.18, 0.10, 1.10) / max(0.5, skill)
    if char == " ":
        # 空格后是小停顿（词边界）
        return entropy.clamped_gauss(0.035, 0.030, 0.0, 0.18) / max(0.5, skill)
    return 0.0


def estimate_duration(
    text: str,
    *,
    wpm: float = DEFAULT_WPM,
    typo_rate: float = 0.0,
) -> float:
    """估算输入 ``text`` 大概要多久（秒），不实际输入。

    用于上层做超时预算。
    """
    if not text:
        return 0.0
    base = wpm_to_delay(wpm)
    total = 0.0
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        total += keystroke_delay(ch, prev, nxt, base_delay=base)
        total += _inter_char_pause(ch)
    # 错字代价：每个错字约等于多打 2 个字符 + 一次回改
    if typo_rate > 0:
        total += len(text) * typo_rate * (base * 3.2)
    return total


# --------------------------------------------------------------------------
# 打字
# --------------------------------------------------------------------------
def _neighbor_keys(char: str) -> List[str]:
    """给出与 ``char`` 在键盘上相邻、容易打错的字符。"""
    if not char.isalpha():
        # 符号的常见错按
        table = {
            ",": [".", "m"],
            ".": [",", "/"],
            ";": ["'", "l"],
            "'": [";", "["],
            "-": ["=", "0"],
            "=": ["-", "]"],
            "/": [".", ";"],
            "[": ["]", "p"],
            "]": ["[", "\\"],
        }
        return table.get(char, ["x"])

    # QWERTY 同行相邻 + 上下行
    rows = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
    lo = char.lower()
    cands: List[str] = []
    for row in rows:
        if lo in row:
            i = row.index(lo)
            for j in (i - 1, i + 1):
                if 0 <= j < len(row):
                    cands.append(row[j])
    if not cands:
        cands = ["x"]
    return cands


def backspace_human(*, count: int = 1, skill: float = 1.0) -> int:
    """真人化退格：连续退格时速度会加快（越删越快）。"""
    n = max(0, int(count))
    for i in range(n):
        delay = entropy.clamped_gauss(0.085, 0.030, 0.030, 0.26)
        # 连续删除加速
        delay *= max(0.45, 1.0 - i * 0.06) / max(0.5, skill)
        time.sleep(delay)
        input_layer.keyboard.press("backspace")
    return n


def clear_with_backspace(
    count: int,
    *,
    skill: float = 1.0,
    chunk_pause: bool = True,
) -> int:
    """用退格清空 ``count`` 个字符。

    真人不会一口气按 100 次退格 —— 会分几段，段间有微停顿。
    """
    remaining = max(0, int(count))
    done = 0
    while remaining > 0:
        chunk = min(remaining, entropy.randint(4, 14))
        backspace_human(count=chunk, skill=skill)
        done += chunk
        remaining -= chunk
        if remaining > 0 and chunk_pause:
            time.sleep(entropy.uniform(0.08, 0.30))
    return done


def type_text_human(
    text: str,
    *,
    wpm: float = DEFAULT_WPM,
    skill: float = 1.0,
    typo_rate: float = 0.0,
    correct_typos: bool = True,
    pause_at_end: bool = True,
    dry_run: bool = False,
) -> TypeResult:
    """人类化输入一段文本。

    :param wpm: 目标速度（英文单词/分钟；中文按字符折算）
    :param skill: 熟练度倍数，>1 更快
    :param typo_rate: 错字率（0-0.08）。0 = 不打错
    :param correct_typos: 打错后是否回改。False 时保留错误
    :param pause_at_end: 结束是否停一下（真人打完会停顿确认）
    :param dry_run: 只算不真的输入

    ⚠️ 实现说明：错字回改使用**退格**，这会删除已经输入的内容。
    如果上层是在一个"不能接受临时错误内容"的场景（例如监听输入
    事件的页面），把 ``typo_rate`` 设成 0。
    """
    if not text:
        return TypeResult(text=text, duration=0.0, human=True)

    base = wpm_to_delay(wpm)
    result = TypeResult(text=text, human=True)
    t0 = time.monotonic()

    # 按"词"切分，词间停顿更长
    buf: List[str] = []

    i = 0
    n = len(text)
    typed_buf: List[str] = []   # 记录真正已敲入的内容（用于回改）

    while i < n:
        ch = text[i]
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < n else ""

        # --- 错字 ---
        made_typo = False
        if typo_rate > 0 and ch.strip() and entropy.random() < typo_rate:
            wrong_pool = _neighbor_keys(ch)
            wrong = entropy.choice(wrong_pool)
            if wrong != ch:
                made_typo = True
                delay = keystroke_delay(wrong, prev, nxt, base_delay=base, skill=skill)
                if not dry_run:
                    time.sleep(delay)
                    input_layer.keyboard.type_text(wrong)
                result.keystrokes += 1
                result.typos += 1

                if correct_typos:
                    # 真人的"发现延迟"：打错后不是立刻发现，通常错 0-3 个字符后才发现
                    extra = entropy.choices([0, 1, 2, 3], weights=[0.45, 0.30, 0.17, 0.08], k=1)[0]
                    extra = min(extra, n - i - 1)
                    for k in range(extra):
                        c2 = text[i + 1 + k]
                        d2 = keystroke_delay(
                            c2,
                            text[i + k] if i + k >= 0 else "",
                            text[i + 2 + k] if i + 2 + k < n else "",
                            base_delay=base, skill=skill,
                        )
                        if not dry_run:
                            time.sleep(d2)
                            input_layer.keyboard.type_text(c2)
                        result.keystrokes += 1
                        typed_buf.append(c2)

                    # 停顿一下才发现
                    if not dry_run:
                        time.sleep(entropy.uniform(0.10, 0.42))

                    # 退格删掉
                    del_count = extra + 1
                    if not dry_run:
                        clear_with_backspace(del_count, skill=skill)
                    result.keystrokes += del_count
                    result.corrections += 1
                    for _ in range(del_count):
                        if typed_buf:
                            typed_buf.pop()

                    # 重新打正确的
                    if not dry_run:
                        time.sleep(entropy.uniform(0.06, 0.22))
                    for k in range(extra + 1):
                        c2 = text[i + k]
                        d2 = keystroke_delay(c2, prev if k == 0 else text[i + k - 1], "", base_delay=base, skill=skill)
                        if not dry_run:
                            time.sleep(d2)
                            input_layer.keyboard.type_text(c2)
                        result.keystrokes += 1
                        typed_buf.append(c2)
                    i += extra + 1
                    result.chars += extra + 1
                    continue

        if not made_typo:
            # --- 正常击键 ---
            delay = keystroke_delay(ch, prev, nxt, base_delay=base, skill=skill)

            # 10% 概率出现"卡壳"（想不起字/手滑）
            if entropy.random() < 0.10:
                delay += entropy.clamped_gauss(0.22, 0.14, 0.05, 0.95)
                result.pauses += 1

            if not dry_run:
                time.sleep(delay)
                input_layer.keyboard.type_text(ch)
            result.keystrokes += 1
            typed_buf.append(ch)
            result.chars += 1

        # --- 结构性停顿 ---
        pause = _inter_char_pause(ch, skill=skill)
        if pause > 0:
            result.pauses += 1
            if not dry_run:
                time.sleep(pause)

        i += 1

    # 结尾停顿（打完确认一下）
    if pause_at_end and not dry_run:
        time.sleep(entropy.uniform(0.12, 0.55))

    elapsed = time.monotonic() - t0
    result.duration = elapsed if not dry_run else estimate_duration(text, wpm=wpm, typo_rate=typo_rate)
    words = max(1, len(text) / 5.0) if text.strip() else 0.0
    if result.duration > 0 and words > 0:
        result.wpm = words / (result.duration / 60.0)
    return result


# --------------------------------------------------------------------------
# 单键 / 组合键
# --------------------------------------------------------------------------
def press_key_human(
    key,
    *,
    human: bool = True,
    repeats: int = 1,
) -> int:
    """真人化按一下某个键（含按住时长变化）。"""
    n = max(1, int(repeats))
    for i in range(n):
        if human:
            hold = entropy.clamped_gauss(0.068, 0.024, 0.025, 0.20)
        else:
            hold = 0.02
        input_layer.keyboard.press(key, hold=hold)
        if i < n - 1:
            time.sleep(entropy.clamped_gauss(0.105, 0.040, 0.030, 0.35) if human else 0.05)
    return n


def hotkey_human(keys: Sequence, *, human: bool = True) -> None:
    """真人化组合键。

    真人的修饰键不是同时按下的：Ctrl 先下、然后 S 下、S 先抬、
    Ctrl 后抬。这里把每个阶段的间隔也随机化。
    """
    if not human:
        input_layer.keyboard.hotkey(keys)
        return

    seq = list(keys)
    if not seq:
        raise ActionFailed("hotkey_human 至少需要一个按键", method="keyboard")

    vks = [input_layer.keyboard.vk_for_key(k) for k in seq]
    held = vks[:-1]
    main = vks[-1]

    # 逐个按下修饰键，间隔 18-60ms
    for k in held:
        input_layer.keyboard.key_down(k)
        time.sleep(entropy.uniform(0.018, 0.060))

    # 主键按下
    input_layer.keyboard.key_down(main)
    time.sleep(entropy.clamped_gauss(0.058, 0.022, 0.022, 0.18))

    # 主键抬起
    input_layer.keyboard.key_up(main)
    time.sleep(entropy.uniform(0.015, 0.055))

    # 逆序抬起修饰键
    for k in reversed(held):
        input_layer.keyboard.key_up(k)
        time.sleep(entropy.uniform(0.012, 0.048))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """键盘真人化自检（**只算节奏，不真按键**）。

    真按键会往用户当前焦点窗口打字 —— 自检绝不能干这个。
    节奏类的检查（右偏分布、Shift 更慢、标点停顿）都是纯计算。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = entropy.get_seed()
    try:
        # -- 间隔右偏 ---------------------------------------------------
        entropy.seed(13)
        delays = [keystroke_delay("a", base_delay=0.1) for _ in range(600)]
        _add("按键间隔恒为正", all(d > 0 for d in delays), "600 次采样")
        mean = sum(delays) / len(delays)
        med = sorted(delays)[len(delays) // 2]
        _add("间隔分布右偏（均值>中位数）", mean > med,
             "均值 %.4f > 中位 %.4f" % (mean, med))

        # -- Shift 字符更慢 ---------------------------------------------
        entropy.seed(19)
        plain = [keystroke_delay("a", base_delay=0.1) for _ in range(500)]
        shifted = [keystroke_delay("A", base_delay=0.1) for _ in range(500)]
        ap = sum(plain) / len(plain)
        as_ = sum(shifted) / len(shifted)
        _add("大写字符平均更慢", as_ > ap, "%.4f > %.4f" % (as_, ap))

        # -- 标点停顿 ---------------------------------------------------
        entropy.seed(23)
        normal = [_inter_char_pause("b") for _ in range(200)]
        clause = [_inter_char_pause(",") for _ in range(200)]
        sentence = [_inter_char_pause("。") for _ in range(200)]
        an = sum(normal) / 200
        ac = sum(clause) / 200
        asen = sum(sentence) / 200
        _add("句号停顿 > 普通字符", asen > an,
             "%.4f > %.4f" % (asen, an))
        _add("逗号停顿 > 普通字符", ac > an, "%.4f > %.4f" % (ac, an))
        _add("句号停顿 >= 逗号", asen >= ac, "%.4f >= %.4f" % (asen, ac))

        # -- 时长估算 ---------------------------------------------------
        short = estimate_duration("hello", wpm=320)
        long = estimate_duration("hello " * 40, wpm=320)
        _add("时长随长度增长", long > short, "%.3f > %.3f" % (long, short))
        fast = estimate_duration("hello world " * 10, wpm=600)
        slow = estimate_duration("hello world " * 10, wpm=150)
        _add("高 WPM 估时更短", fast < slow, "%.3f < %.3f" % (fast, slow))
        _add("默认 WPM 常量存在", DEFAULT_WPM > 0, "%.0f" % DEFAULT_WPM)

        # -- 邻近键 -----------------------------------------------------
        nbs = {ch: _neighbor_keys(ch) for ch in ("a", "s", "e", "t")}
        _add("常用键有邻近键映射",
             all(bool(v) for v in nbs.values()),
             ", ".join("%s→%d" % (k, len(v)) for k, v in nbs.items()))

        # -- 可复现 -----------------------------------------------------
        entropy.seed(37)
        d1 = estimate_duration("The quick brown fox", wpm=300)
        entropy.seed(37)
        d2 = estimate_duration("The quick brown fox", wpm=300)
        _add("时长估算可复现", d1 == d2, "%.6f" % d1)

    finally:
        if saved is None:
            entropy.reset()
        else:
            entropy.seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
