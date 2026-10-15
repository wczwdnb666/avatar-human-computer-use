# -*- coding: utf-8 -*-
"""验证结果数据结构与图像比对工具。

## VerifyResult：四态而非两态

动作执行有三种结局，验证也有三种：

=========  ============================================================
成功        拿到了"确实变了"的证据
失败        拿到了"确实没变/变错了"的证据
**未知**      **没拿到证据** —— 截图失败、控件找不到、超时
=========  ============================================================

第三态是这套设计的灵魂。"我不知道"和"我确认没成功"必须分开，
因为它们的**处置方式完全不同**：

- 失败 → 报告，可能回滚
- 未知 → 重试 / 换手段 / 降级，**不该当作失败上报**

用一个裸 bool 表达这三态是不可能的。这就是为什么 ``ok`` 之外
还有 ``unknown`` 字段，且 :data:`VERIFY_UNKNOWN` 是独立常量。

## pHash（感知哈希）

桌面端验证的核心。做法：

1. 灰度化（加权 ``0.299R + 0.587G + 0.114B``）
2. 缩到 ``N×N``（默认 16×16）—— 缩放本身就是最强的抗噪手段
3. 算 2D DCT（``scipy`` 不许用，自己手搓 O(N⁴)，N=16 时 65536 次乘加，够快）
4. 取左上 ``8×8`` 低频块（跳过 DC 项）
5. 与**中位数**比较：大于中位数置 1，否则 0 → 63 位整数

为什么用 DCT 而不是简单的均值哈希：DCT 保留了低频结构信息，
对亮度变化、轻微位移不敏感，但对**结构性变化**（出现一个对话框、
按钮变色）极其敏感。打榜场景里"页面多了一行字"要能检出来。

汉明距离判据（本实现是 63 位：8×8=64 个系数跳过 DC 后剩 63 位）:

- ``0``  完全相同
- ``≤ 5``   基本没变（压缩噪声 / 帧间抖动）
- ``6~10``  疑似有变化
- ``> 10``  明确有变化

这些阈值是经验值，**不是绝对真理** —— 实际使用中应该由
调用方按场景调，所以全部做成参数并暴露出来。
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..errors import AvatarError

__all__ = [
    "VerificationError",
    "VerifyResult",
    "VERIFY_OK",
    "VERIFY_FAIL",
    "VERIFY_UNKNOWN",
    "HASH_SIDE",
    "HASH_BITS",
    "DCT_SIZE",
    "phash",
    "phash_from_png",
    "hamming_distance",
    "image_similarity",
    "images_differ",
    "snapshot_hash",
    "result_of",
    "summarize",
]


class VerificationError(AvatarError):
    """验证过程本身出错（不是"验证结果为失败"）。"""

    exit_code = 22


#: 验证结论常量。**不要**用 True/False 代替 —— 三态必须显式。
VERIFY_OK = "ok"            # 确认成功
VERIFY_FAIL = "fail"        # 确认失败
VERIFY_UNKNOWN = "unknown"  # 无法判定

_VALID_VERDICTS = (VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN)

# 默认阈值（针对 63 位 pHash 的汉明距离——
# 64 位版本的经典阈值是 5/10，按比例缩到 63 位后基本不变）
DEFAULT_UNCHANGED_MAX = 5    # ≤5 认为没变
DEFAULT_CHANGED_MIN = 6      # ≥6 认为变了
DEFAULT_SIMILARITY_MIN = 0.90  # 相似度阈值（0~1）


class VerifyResult(object):
    """一次验证的完整结论。

    **必填只有 ``verdict``**，其余按情况给。``evidence`` 是"凭什么这么说"
    的原始材料 —— 给人和给模型看的都放这儿。

    构造推荐用类方法，语义更清楚::

        VerifyResult.ok_(method="phash", reason="像素变化 23 位",
                         evidence={"distance": 23})
        VerifyResult.fail(method="dom", reason="文本仍是旧值",
                          evidence={"actual": "旧值"})
        VerifyResult.unknown_(method="phash", reason="截图失败")
    """

    __slots__ = ("verdict", "method", "reason", "evidence", "target",
                 "mode", "elapsed", "checked_at", "extra", "error")

    def __init__(
        self,
        verdict: str = VERIFY_UNKNOWN,
        *,
        method: str = "",
        reason: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        target: str = "",
        mode: str = "",
        elapsed: float = 0.0,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if verdict not in _VALID_VERDICTS:
            raise VerificationError("非法 verdict: %r" % (verdict,))
        self.verdict = verdict
        self.method = method
        self.reason = reason
        self.evidence = dict(evidence or {})
        self.target = target
        self.mode = mode
        self.elapsed = float(elapsed)
        self.checked_at = time.time()
        self.extra = dict(extra or {})
        self.error = error
        # 兜底：调用方可能只写了 ``error`` 没写 ``reason``。
        # 这种情况**不变量**（verdict ∈ 三态）没破，但诊断信息会丢 ——
        # 实测踩过：retry 里传了 reason 却被静默丢掉，reason 变空串，
        # 上层想按 reason 分类都做不了。这里把 error 回填当 reason 用。
        if not self.reason and self.error:
            self.reason = str(self.error)

    # ---------------- 属性 ----------------

    @property
    def ok(self) -> bool:
        """确认成功。**只有这一种情况返回 True。**"""
        return self.verdict == VERIFY_OK

    @property
    def failed(self) -> bool:
        """确认失败（区别于"未知"）。"""
        return self.verdict == VERIFY_FAIL

    @property
    def unknown(self) -> bool:
        """无法判定 —— 既不是成功也不是失败。"""
        return self.verdict == VERIFY_UNKNOWN

    @property
    def conclusive(self) -> bool:
        """是否有确定结论（成功或失败）。"""
        return self.verdict in (VERIFY_OK, VERIFY_FAIL)

    @property
    def retryable(self) -> bool:
        """是否值得重试。未知值得重试，确认成功不值得，确认失败看场景。

        这里保守地认为"确认失败也值得重试一次" —— 因为有些失败是
        时序问题（DOM 还没更新完）。真正的重试策略在 retry 模块里，
        那里有次数上限。
        """
        return self.verdict != VERIFY_OK

    @property
    def confidence(self) -> float:
        """置信度 0~1。

        未知 = 0（毫无把握）；有结论时取 ``extra["confidence"]``，
        默认 1.0。有些判据（比如 pHash 距离刚好在阈值边上）
        应该给更低的置信度。
        """
        if self.unknown:
            return 0.0
        try:
            return max(0.0, min(1.0, float(self.extra.get("confidence", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    # ---------------- 构造 ----------------

    @classmethod
    def ok_(cls, **kw) -> "VerifyResult":
        return cls(VERIFY_OK, **kw)

    @classmethod
    def fail(cls, **kw) -> "VerifyResult":
        return cls(VERIFY_FAIL, **kw)

    @classmethod
    def unknown_(cls, **kw) -> "VerifyResult":
        """构造"未知"结果。

        **注意名字带下划线** —— 因为实例属性 ``unknown``（bool）
        已经占用了这个名字，同名类方法会被 slot descriptor 遮蔽。
        这是 ``__slots__`` 的经典坑：属性名和方法名不能撞。
        """
        return cls(VERIFY_UNKNOWN, **kw)

    @classmethod
    def from_bool(cls, value: Any, **kw) -> "VerifyResult":
        """把裸 bool 包装成结果。**只在明确知道语义时用**。

        ``None`` 会被包成"未知"而不是"失败" —— 这是刻意的，
        因为 ``None`` 通常意味着"没拿到"。
        """
        if value is None:
            return cls.unknown_(reason="没有拿到判据", **kw)
        return cls(VERIFY_OK if value else VERIFY_FAIL, **kw)

    # ---------------- 输出 ----------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ok": self.ok,
            "unknown": self.unknown,
            "method": self.method,
            "reason": self.reason,
            "evidence": self.evidence,
            "target": self.target,
            "mode": self.mode,
            "confidence": self.confidence,
            "elapsed": round(self.elapsed, 4),
            "checked_at": self.checked_at,
            "extra": self.extra,
            "error": self.error,
        }

    def describe(self) -> str:
        """一行人类可读的说明。"""
        tag = {"ok": "✓", "fail": "✗", "unknown": "?"}.get(self.verdict, "?")
        bits = ["%s %s" % (tag, self.verdict.upper())]
        if self.method:
            bits.append("[%s]" % self.method)
        if self.target:
            bits.append(str(self.target))
        if self.reason:
            bits.append(self.reason)
        return " ".join(bits)

    def __bool__(self) -> bool:
        """``if result`` 等价于 ``result.ok``。

        注意：unknown 会被转成 False。**不要依赖这个语义做决策** ——
        显式检查 ``.unknown``。提供它只是为了方便日志里的 ``if r:``。
        """
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        return "<VerifyResult %s %s %s>" % (self.verdict, self.method,
                                            self.reason[:40])

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, VerifyResult):
            return (self.verdict == other.verdict
                    and self.method == other.method
                    and self.reason == other.reason)
        if isinstance(other, bool):
            return self.ok == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.verdict, self.method, self.reason))


# --------------------------------------------------------------------------
# pHash
# --------------------------------------------------------------------------

#: DCT 的低频方块边长。8×8 取到 64 个系数，**跳过 DC 后剩 63 位**。
HASH_SIDE = 8
#: 实际哈希位数（跳过 DC）。对外暴露，供相似度计算对齐分母。
HASH_BITS = HASH_SIDE * HASH_SIDE - 1
#: 做 DCT 前的缩放边长。16 足够；32 会慢 16 倍而收益很小。
DCT_SIZE = 16

# 预计算 DCT 余弦基，避免每次重算（O(N²) 内存换 O(N²) 时间）
_COS_CACHE = {}  # type: Dict[int, List[List[float]]]


def _cos_table(n: int) -> List[List[float]]:
    """``_cos_table(n)[u][x] = cos((2x+1)·u·π / (2n))``。"""
    t = _COS_CACHE.get(n)
    if t is not None:
        return t
    table = []
    for u in range(n):
        row = []
        for x in range(n):
            row.append(math.cos((2 * x + 1) * u * math.pi / (2 * n)))
        table.append(row)
    _COS_CACHE[n] = table
    return table


def _to_gray(pixels: Sequence[Sequence[Union[int, float, Sequence[int]]]],
             width: int, height: int) -> List[List[float]]:
    """把二维像素数组转灰度。

    支持三种输入：
    - ``int`` / ``float`` 单通道
    - 长度 3 的序列 ``(r, g, b)``
    - 长度 4 的序列 ``(r, g, b, a)``（alpha 会被合成到黑底上）
    """
    out = []  # type: List[List[float]]
    for y in range(height):
        row = pixels[y]
        grow = []
        for x in range(width):
            p = row[x]
            if isinstance(p, (int, float)):
                grow.append(float(p))
                continue
            try:
                n = len(p)
            except TypeError:
                grow.append(0.0)
                continue
            if n == 0:
                grow.append(0.0)
                continue
            if n >= 4:
                a = float(p[3]) / 255.0
                r = float(p[0]) * a
                g = float(p[1]) * a
                b = float(p[2]) * a
            else:
                r, g = float(p[0]), float(p[1])
                b = float(p[2]) if n >= 3 else g
            grow.append(0.299 * r + 0.587 * g + 0.114 * b)
        out.append(grow)
    return out


def _resize_nearest(gray: List[List[float]], src_w: int, src_h: int,
                    dst_n: int) -> List[List[float]]:
    """缩放。

    用的是**区域平均**而不是最近邻 —— 代价一样（O(目标像素)），
    但抗混叠好太多。

    为什么必须抗混叠：最近邻在 64→16 时只采样 16×16 个点，
    一张"左白右黑"的图如果采样点全落在白区，等于是把图看成了纯色。
    区域平均会把这个变化**保留**在数值里，DCT 才能捕捉到。
    实测：最近邻下左右翻转图的汉明距离只有 4（低于阈值，误判"没变"），
    区域平均后明显拉开。
    """
    out = []  # type: List[List[float]]
    if src_w <= 0 or src_h <= 0:
        return [[0.0] * dst_n for _ in range(dst_n)]

    for y in range(dst_n):
        y0 = int(y * src_h / dst_n)
        y1 = max(y0 + 1, int((y + 1) * src_h / dst_n))
        y1 = min(y1, src_h)
        row = []
        for x in range(dst_n):
            x0 = int(x * src_w / dst_n)
            x1 = max(x0 + 1, int((x + 1) * src_w / dst_n))
            x1 = min(x1, src_w)
            s = 0.0
            cnt = 0
            for yy in range(y0, y1):
                gy = gray[yy]
                for xx in range(x0, x1):
                    s += gy[xx]
                    cnt += 1
            row.append(s / cnt if cnt else 0.0)
        out.append(row)
    return out


def _resize_nearest_legacy(gray: List[List[float]], src_w: int, src_h: int,
                           dst_n: int) -> List[List[float]]:
    """最近邻（保留作为降级实现，正常不用）。"""
    out = []  # type: List[List[float]]
    if src_w <= 0 or src_h <= 0:
        return [[0.0] * dst_n for _ in range(dst_n)]
    for y in range(dst_n):
        sy = min(src_h - 1, int(y * src_h / dst_n))
        src_row = gray[sy]
        row = []
        for x in range(dst_n):
            sx = min(src_w - 1, int(x * src_w / dst_n))
            row.append(float(src_row[sx]))
        out.append(row)
    return out


def _dct2(block: List[List[float]], n: int) -> List[List[float]]:
    """2D DCT-II（分离式：先行后列）。手搓，不依赖 numpy/scipy。

    复杂度 O(n³) —— 单个方向 n² × n = n³，n=16 时 4096 次乘加，
    两个方向共 8192 次。毫秒级。
    """
    cos = _cos_table(n)
    # 先行变换
    rows = []  # type: List[List[float]]
    for y in range(n):
        src = block[y]
        row = []
        for u in range(n):
            s = 0.0
            cu = cos[u]
            for x in range(n):
                s += src[x] * cu[x]
            row.append(s)
        rows.append(row)
    # 再列变换
    out = [[0.0] * n for _ in range(n)]
    for x in range(n):
        col = [rows[y][x] for y in range(n)]
        for v in range(n):
            s = 0.0
            cv = cos[v]
            for y in range(n):
                s += col[y] * cv[y]
            out[v][x] = s
    return out


def phash(pixels: Sequence[Sequence[Any]], width: int, height: int,
          *,
          hash_side: int = HASH_SIDE,
          dct_size: int = DCT_SIZE) -> int:
    """算感知哈希，返回整数（``hash_side=8`` 时 63 位）。

    ``pixels`` 是 ``pixels[y][x]`` 形式的二维序列。
    失败抛 :class:`VerificationError`。
    """
    if width <= 0 or height <= 0:
        raise VerificationError("图像尺寸非法：%dx%d" % (width, height))
    if not pixels:
        raise VerificationError("像素数据为空")

    try:
        gray = _to_gray(pixels, width, height)
    except (IndexError, TypeError) as exc:
        raise VerificationError("像素数据不完整：%s" % (exc,))

    small = _resize_nearest(gray, width, height, dct_size)
    coef = _dct2(small, dct_size)

    # 取左上 hash_side×hash_side 低频块，**跳过 DC（[0][0]）**。
    #
    # 为什么必须跳 DC：DC 系数是整块的灰度均值乘以常数，
    # 它**永远**远大于其它所有系数。留在里面的话，
    # 无论图是什么样，DC 那一位恒为 1 —— 等于白占一位，
    # 还让纯色图（本该是"无结构"，hash 全 0）变成一个非零值。
    #
    # 代价是只有 hash_side²-1 位（8×8 → 63 位），不是 64。
    # 63 位完全够用，汉明距离阈值只需按比例微调（见 BITS 常量）。
    vals = []  # type: List[float]
    for v in range(hash_side):
        for u in range(hash_side):
            if v == 0 and u == 0:
                continue
            vals.append(coef[v][u])

    if not vals:
        raise VerificationError("DCT 系数为空")

    srt = sorted(vals)
    n = len(srt)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0

    # 用**严格大于**比较，且对浮点残差留容差。
    #
    # 容差为什么用相对量：DC 已被排除，但残留的 AC 浮点误差
    # （纯色图上实测约 2e-12）与中位数的量级无关 ——
    # 中位数可能是 1e6 也可能是 0。用 `max(1.0, |med|) * 1e-9`
    # 这样的下界，两种情形都能把 2e-12 这类残差滤掉。
    scale = max(1.0, abs(med))
    eps = scale * 1e-9
    h = 0
    for i, val in enumerate(vals):
        if val > med + eps:
            h |= (1 << i)
    return h


def phash_from_png(data: bytes, **kw) -> int:
    """从 PNG 字节直接算 pHash。

    **纯标准库解 PNG** —— 只支持 8 位 RGB/RGBA/灰度，
    不碰调色板和隔行（CDP 截图实测都是 8 位非隔行）。
    遇到不支持的类型抛 :class:`VerificationError`，由调用方转"未知"。
    """
    if not data:
        raise VerificationError("PNG 数据为空")
    try:
        import zlib
        import struct
    except Exception as exc:      # pragma: no cover
        raise VerificationError("导入失败：%s" % (exc,))

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VerificationError("不是 PNG 数据")

    pos = 8
    width = height = 0
    bit_depth = color_type = interlace = 0
    idat = []  # type: List[bytes]

    while pos + 8 <= len(data):
        try:
            (length,) = struct.unpack(">I", data[pos:pos + 4])
            tag = data[pos + 4:pos + 8]
        except Exception:
            break
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # 长度 + 类型 + 数据 + CRC

        if tag == b"IHDR":
            if len(body) < 13:
                raise VerificationError("IHDR 长度非法")
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body[:13])
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break

    if width <= 0 or height <= 0:
        raise VerificationError("没读到 IHDR")
    if interlace:
        raise VerificationError("不支持隔行 PNG")
    if bit_depth != 8:
        raise VerificationError("只支持 8 位 PNG，实际 %d" % bit_depth)

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise VerificationError("未知 color_type %d" % color_type)
    if color_type == 3:
        raise VerificationError("不支持调色板 PNG")

    try:
        raw = zlib.decompress(b"".join(idat))
    except Exception as exc:
        raise VerificationError("zlib 解压失败：%s" % (exc,))

    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise VerificationError("IDAT 数据不足（%d < %d）"
                                % (len(raw), (stride + 1) * height))

    # 逆滤波（PNG 的 5 种 filter）
    rows = []  # type: List[bytearray]
    prev = bytearray(stride)
    off = 0
    for _y in range(height):
        ft = raw[off]
        off += 1
        line = bytearray(raw[off:off + stride])
        off += stride
        if ft == 0:
            pass
        elif ft == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ft == 2:      # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:      # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:      # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise VerificationError("未知 filter 类型 %d" % ft)
        rows.append(line)
        prev = line

    # 转成 pixels[y][x] 形式
    pixels = []  # type: List[List[Any]]
    for y in range(height):
        line = rows[y]
        row = []
        for x in range(width):
            i = x * channels
            if channels == 1:
                row.append(line[i])
            elif channels == 2:
                row.append((line[i], line[i], line[i]))
            elif channels == 3:
                row.append((line[i], line[i + 1], line[i + 2]))
            else:
                row.append((line[i], line[i + 1], line[i + 2], line[i + 3]))
        pixels.append(row)

    return phash(pixels, width, height, **kw)


def hamming_distance(a: int, b: int) -> int:
    """两个哈希的汉明距离（不同位的个数）。"""
    try:
        x = int(a) ^ int(b)
    except (TypeError, ValueError):
        raise VerificationError("哈希必须是整数")
    if x < 0:
        x = -x
    count = 0
    while x:
        count += x & 1
        x >>= 1
    return count


def image_similarity(a: int, b: int, *, bits: int = HASH_BITS) -> float:
    """相似度 0~1（1 = 完全相同）。"""
    if bits <= 0:
        raise VerificationError("bits 必须为正")
    d = hamming_distance(a, b)
    return max(0.0, 1.0 - float(min(d, bits)) / float(bits))


def images_differ(
    a: int,
    b: int,
    *,
    threshold: int = DEFAULT_CHANGED_MIN,
    unchanged_max: int = DEFAULT_UNCHANGED_MAX,
) -> VerifyResult:
    """判断两张图（的 pHash）是否**确实不同**。

    返回三态：明确不同 / 明确相同 / 判不出来。
    距离落在 ``unchanged_max < d < threshold`` 之间时算"未知"
    —— 这是**刻意的**：那个区间里噪声和真实变化的概率相当，
    硬判会制造假阳性，而假阳性会让重试逻辑走向错误方向。
    """
    if a is None or b is None:
        return VerifyResult.unknown_(method="phash", reason="哈希缺失")
    d = hamming_distance(a, b)
    ev = {"distance": d, "threshold": threshold, "unchanged_max": unchanged_max,
          "similarity": round(image_similarity(a, b), 4)}

    if d >= threshold:
        return VerifyResult.ok_(
            method="phash",
            reason="图像变化 %d 位（阈值 %d）" % (d, threshold),
            evidence=ev,
            extra={"confidence": min(1.0, 0.6 + d / 40.0)})
    if d <= unchanged_max:
        return VerifyResult.fail(
            method="phash",
            reason="图像几乎没变（距离 %d ≤ %d）" % (d, unchanged_max),
            evidence=ev,
            extra={"confidence": 0.9})
    return VerifyResult.unknown_(
        method="phash",
        reason="图像变化 %d 位，落在灰区（%d~%d），无法判定"
               % (d, unchanged_max + 1, threshold - 1),
        evidence=ev)


def snapshot_hash(shot: Any) -> Optional[int]:
    """从截图对象（阶段二的 Shot 或 web_cdp 的 ShotResult）算 pHash。

    拿不到就返回 **None**（让上层转成"未知"，而不是抛错）。
    """
    if shot is None:
        return None
    data = None
    for attr in ("data", "png", "raw", "bytes_"):
        v = getattr(shot, attr, None)
        if isinstance(v, (bytes, bytearray)) and v:
            data = bytes(v)
            break
    if data is None:
        path = getattr(shot, "path", None)
        if isinstance(path, str) and path:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception:
                return None
    if not data:
        return None
    try:
        return phash_from_png(data)
    except Exception:
        # 不是 PNG（可能是 BMP/JPEG）—— 尝试用 PIL 之外的手段。
        # 本库零依赖，所以直接放弃，返回 None。
        return None


def result_of(value: Any, **kw) -> VerifyResult:
    """把任意东西规整成 VerifyResult。

    已经是 VerifyResult → 原样返回（可覆盖 kwargs 之外的东西不动）。
    bool → 成功/失败。None → 未知。dict 里有 verdict → 取它。
    其他 → 未知（附带原值的 repr）。
    """
    if isinstance(value, VerifyResult):
        return value
    if value is None:
        return VerifyResult.unknown_(reason="值为 None", **kw)
    if isinstance(value, bool):
        return VerifyResult.from_bool(value, **kw)
    if isinstance(value, dict):
        v = value.get("verdict")
        if v in _VALID_VERDICTS:
            ev = dict(kw.pop("evidence", {}) or {})
            ev.update(value.get("evidence") or {})
            return VerifyResult(
                v,
                method=value.get("method") or kw.pop("method", ""),
                reason=value.get("reason") or kw.pop("reason", ""),
                evidence=ev,
                **kw)
        if "ok" in value:
            return VerifyResult.from_bool(bool(value["ok"]), **kw)
    return VerifyResult.unknown_(reason="无法识别的判据：%r" % (value,), **kw)


def summarize(results: Sequence[VerifyResult]) -> Dict[str, Any]:
    """汇总一批验证结果。"""
    out = {"total": 0, "ok": 0, "fail": 0, "unknown": 0,
           "all_ok": False, "any_ok": False, "verdict": VERIFY_UNKNOWN}
    for r in results or ():
        rr = result_of(r)
        out["total"] += 1
        if rr.ok:
            out["ok"] += 1
        elif rr.failed:
            out["fail"] += 1
        else:
            out["unknown"] += 1
    if out["total"]:
        out["all_ok"] = out["ok"] == out["total"]
        out["any_ok"] = out["ok"] > 0
        if out["ok"] == out["total"]:
            out["verdict"] = VERIFY_OK
        elif out["fail"]:
            out["verdict"] = VERIFY_FAIL
        else:
            out["verdict"] = VERIFY_UNKNOWN
    return out


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def _gen_pixels(w: int, h: int, fn) -> List[List[Any]]:
    return [[fn(x, y) for x in range(w)] for y in range(h)]


def _make_png(w: int, h: int, fn) -> bytes:
    """造一个真 PNG（filter=0，无隔行，8 位 RGB）。"""
    import struct
    import zlib

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter None
        for x in range(w):
            r, g, b = fn(x, y)
            raw.extend((r & 0xFF, g & 0xFF, b & 0xFF))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    return (b"\x89PNG\r\n\x1a\n" + ihdr
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    import os
    import tempfile

    # --- 三态常量互不相同 ---
    add("三态常量互异",
        len({VERIFY_OK, VERIFY_FAIL, VERIFY_UNKNOWN}) == 3)
    add("VERIFY_OK 是字符串", isinstance(VERIFY_OK, str))

    # --- VerifyResult 基本 ---
    r = VerifyResult.ok_(method="phash", reason="变了")
    add("ok_ 后 ok True", r.ok is True)
    add("ok_ 后 failed False", r.failed is False)
    add("ok_ 后 unknown False", r.unknown is False)
    add("ok_ 后 conclusive True", r.conclusive is True)
    add("ok_ 后 confidence 1.0", r.confidence == 1.0, r.confidence)
    add("ok_ 后 retryable False", r.retryable is False)

    r = VerifyResult.fail(method="dom", reason="没变")
    add("fail 后 failed True", r.failed is True)
    add("fail 后 ok False", r.ok is False)
    add("fail 后 conclusive True", r.conclusive is True)
    add("fail 后 retryable True", r.retryable is True)

    r = VerifyResult.unknown_(method="phash", reason="截不到")
    add("unknown 后 unknown True", r.unknown is True)
    add("unknown 后 ok False", r.ok is False)
    add("unknown 后 failed False", r.failed is False)
    add("unknown 后 conclusive False", r.conclusive is False)
    add("unknown 后 confidence 0.0", r.confidence == 0.0, r.confidence)
    add("unknown 后 retryable True", r.retryable is True)
    add("unknown 与 fail 属性不同", r.unknown != VerifyResult.fail().unknown)

    # --- 非法 verdict 抛错 ---
    try:
        VerifyResult("maybe")
        add("非法 verdict 抛 VerificationError", False, "没抛")
    except VerificationError:
        add("非法 verdict 抛 VerificationError", True)

    # --- from_bool ---
    add("from_bool True → ok", VerifyResult.from_bool(True).ok is True)
    add("from_bool False → fail", VerifyResult.from_bool(False).failed is True)
    add("from_bool None → unknown",
        VerifyResult.from_bool(None).unknown is True)

    # --- confidence 可覆盖 ---
    r = VerifyResult.ok_(extra={"confidence": 0.7})
    add("confidence 可覆盖", abs(r.confidence - 0.7) < 1e-9, r.confidence)
    r = VerifyResult.ok_(extra={"confidence": 5})
    add("confidence 上限夹到 1", r.confidence == 1.0)
    r = VerifyResult.ok_(extra={"confidence": -3})
    add("confidence 下限夹到 0", r.confidence == 0.0)

    # --- describe / to_dict ---
    r = VerifyResult.ok_(method="phash", reason="距离 23", target="btn")
    add("describe 带对勾", "✓" in r.describe(), r.describe())
    add("describe 带 method", "phash" in r.describe())
    add("describe 带 target", "btn" in r.describe())
    add("fail describe 带叉",
        "✗" in VerifyResult.fail().describe())
    add("unknown describe 带问号",
        "?" in VerifyResult.unknown_().describe())
    d = r.to_dict()
    add("to_dict 字段齐",
        set(d) >= {"verdict", "ok", "unknown", "method", "reason", "evidence",
                   "target", "mode", "confidence", "elapsed", "checked_at",
                   "extra", "error"}, sorted(d))
    add("to_dict ok 与实际一致", d["ok"] is True)

    # --- evidence 独立拷贝（防外部改） ---
    src = {"a": 1}
    r = VerifyResult.ok_(evidence=src)
    src["a"] = 999
    add("evidence 是拷贝不受外部影响", r.evidence["a"] == 1, r.evidence)

    # --- bool 转换 ---
    add("bool(ok) 为 True", bool(VerifyResult.ok_()) is True)
    add("bool(fail) 为 False", bool(VerifyResult.fail()) is False)
    add("bool(unknown) 为 False", bool(VerifyResult.unknown_()) is False)

    # --- __eq__ ---
    add("同参数结果相等",
        VerifyResult.ok_(method="m", reason="r") ==
        VerifyResult.ok_(method="m", reason="r"))
    add("不同 verdict 不等",
        VerifyResult.ok_(method="m") != VerifyResult.fail(method="m"))
    add("与 True 比较",
        VerifyResult.ok_() == True)  # noqa: E712
    add("fail 与 False 比较",
        VerifyResult.fail() == False)  # noqa: E712
    a1 = VerifyResult.ok_(method="m", reason="r")
    add("可哈希", len({a1, VerifyResult.ok_(method="m", reason="r")}) == 1)

    # --- DCT 余弦表 ---
    t = _cos_table(4)
    add("余弦表 4 行", len(t) == 4)
    add("余弦表每行 4 列", all(len(r) == 4 for r in t))
    add("u=0 时全为 1", all(abs(v - 1.0) < 1e-12 for v in t[0]), t[0])
    add("余弦表缓存复用", _cos_table(4) is _cos_table(4))

    # --- 灰度转换 ---
    g = _to_gray([[(255, 255, 255)]], 1, 1)
    add("白色灰度 255", abs(g[0][0] - 255) < 1e-6, g[0][0])
    g = _to_gray([[(0, 0, 0)]], 1, 1)
    add("黑色灰度 0", abs(g[0][0]) < 1e-6)
    g = _to_gray([[(255, 0, 0)]], 1, 1)
    add("纯红灰度 ≈76.245", abs(g[0][0] - 76.245) < 0.01, g[0][0])
    g = _to_gray([[(0, 255, 0)]], 1, 1)
    add("纯绿灰度 ≈149.685", abs(g[0][0] - 149.685) < 0.01, g[0][0])
    g = _to_gray([[(0, 0, 255)]], 1, 1)
    add("纯蓝灰度 ≈29.07", abs(g[0][0] - 29.07) < 0.01, g[0][0])
    g = _to_gray([[128]], 1, 1)
    add("单通道整数直通", g[0][0] == 128.0)
    g = _to_gray([[(255, 255, 255, 255)]], 1, 1)
    add("RGBA 全不透明 = 255", abs(g[0][0] - 255) < 1e-6, g[0][0])
    g = _to_gray([[(255, 255, 255, 0)]], 1, 1)
    add("RGBA 全透明 = 0（黑底）", abs(g[0][0]) < 1e-6, g[0][0])
    g = _to_gray([[(255, 255, 255, 128)]], 1, 1)
    add("RGBA 半透明 ≈127.5", abs(g[0][0] - 127.5) < 0.6, g[0][0])

    # --- phash 稳定性 ---
    flat = _gen_pixels(32, 32, lambda x, y: (128, 128, 128))
    h1 = phash(flat, 32, 32)
    add("纯色图 hash 为 0（无差异）", h1 == 0, h1)

    # 同一图案两次必须一样
    pat = _gen_pixels(64, 64, lambda x, y: (255, 255, 255) if x < 32 else (0, 0, 0))
    h2 = phash(pat, 64, 64)
    h3 = phash(pat, 64, 64)
    add("同图 hash 可复现", h2 == h3, (h2, h3))
    add("左右分块图 hash 非 0", h2 != 0, h2)

    # 亮度整体偏移 → hash 应**基本相同**（pHash 抗亮度的本质是
    # DCT 的线性性：AC 系数等比缩放，与中位数的大小关系不变）。
    # 但由于低频系数的浮点残差，偶尔会有 1~2 位翻转 ——
    # 这本身属于"几乎没变"的范围，所以断言用距离而不是相等。
    bright = _gen_pixels(64, 64, lambda x, y: (200, 200, 200) if x < 32
                         else (60, 60, 60))
    h4 = phash(bright, 64, 64)
    d_bright = hamming_distance(h2, h4)
    add("整体亮度偏移后 hash 基本不变（距离 ≤ 2）", d_bright <= 2,
        (hex(h2), hex(h4), d_bright))

    # 结构变化 → hash 应不同。
    # **用局部结构变化做用例**（页面多一块/多一行），这才是真实场景。
    blk = _gen_pixels(64, 64, lambda x, y: (0, 0, 0)
                      if (10 <= x < 18 and 10 <= y < 18) else (255, 255, 255))
    h5 = phash(blk, 64, 64)
    add("局部黑块出现后 hash 不同", h2 != h5, (hex(h2), hex(h5)))
    add("局部黑块距离 > 阈值",
        hamming_distance(h2, h5) > DEFAULT_CHANGED_MIN,
        hamming_distance(h2, h5))

    line_img = _gen_pixels(64, 64, lambda x, y: (0, 0, 0) if y == 40
                           else ((255, 255, 255) if x < 32 else (0, 0, 0)))
    add("多一行细线距离 > 阈值",
        hamming_distance(h2, phash(line_img, 64, 64)) > DEFAULT_CHANGED_MIN,
        hamming_distance(h2, phash(line_img, 64, 64)))

    # **已知盲区**：整体左右/上下翻转。DCT 看的是低频能量分布，
    # 左右镜像不改变能量分布，所以 pHash 对它不敏感（实测距离 4）。
    # 这是算法固有性质，不是缺陷 —— 记录下来，避免以后误以为是 bug。
    inverted = _gen_pixels(64, 64, lambda x, y: (0, 0, 0) if x < 32
                           else (255, 255, 255))
    h_inv = phash(inverted, 64, 64)
    d_inv = hamming_distance(h2, h_inv)
    add("已知盲区：左右镜像距离很小（≤6）", d_inv <= 6, d_inv)
    add("已知盲区：镜像不被判为变化（< 阈值）",
        d_inv < DEFAULT_CHANGED_MIN, d_inv)

    # 平移不敏感（抗抖动）
    shifted = _gen_pixels(64, 64, lambda x, y: (255, 255, 255)
                          if 8 <= x < 40 else (0, 0, 0))
    add("平移 8px 距离很小（抗抖动）",
        hamming_distance(h2, phash(shifted, 64, 64)) <= 3,
        hamming_distance(h2, phash(shifted, 64, 64)))

    # 尺寸不同但内容相同 → hash 应相同（缩放抗性）
    pat2 = _gen_pixels(128, 128, lambda x, y: (255, 255, 255) if x < 64
                       else (0, 0, 0))
    h6 = phash(pat2, 128, 128)
    add("同图案不同尺寸 hash 相同（缩放抗性）", h2 == h6, (h2, h6))

    # --- phash 异常 ---
    for bad_w, bad_h, label in ((0, 10, "宽 0"), (10, 0, "高 0"),
                                (-1, 10, "宽负数")):
        try:
            phash(flat, bad_w, bad_h)
            add("phash 尺寸非法抛错(%s)" % label, False, "没抛")
        except VerificationError:
            add("phash 尺寸非法抛错(%s)" % label, True)
    try:
        phash([], 1, 1)
        add("phash 空像素抛错", False, "没抛")
    except VerificationError:
        add("phash 空像素抛错", True)
    try:
        phash([[1, 2]], 5, 1)   # 行比声明短
        add("phash 像素不足抛错", False, "没抛")
    except VerificationError:
        add("phash 像素不足抛错", True)

    # --- 汉明距离 ---
    add("相同为 0", hamming_distance(0b1010, 0b1010) == 0)
    add("差 1 位", hamming_distance(0b1010, 0b1011) == 1)
    add("差 2 位", hamming_distance(0b1010, 0b1100) == 2)
    add("全 63 位差", hamming_distance(0, (1 << 63) - 1) == 63)
    add("负数取绝对值",
        hamming_distance(-1, 0) == 1, hamming_distance(-1, 0))
    try:
        hamming_distance("x", 1)
        add("非整数抛错", False, "没抛")
    except VerificationError:
        add("非整数抛错", True)

    # --- 相似度 ---
    add("相同相似度 1.0", image_similarity(0, 0) == 1.0)
    add("全差相似度 0.0",
        image_similarity(0, (1 << 63) - 1) == 0.0)
    # 63 位下：16 位差 → 1 - 16/63 ≈ 0.746
    add("差 16 位相似度 = 1-16/63",
        abs(image_similarity(0, 0xFFFF) - (1 - 16.0 / 63.0)) < 1e-9,
        image_similarity(0, 0xFFFF))
    # 显式给 bits 时按其计算
    add("bits 显式给定时按它算",
        abs(image_similarity(0, 0xFFFF, bits=64) - 0.75) < 1e-9,
        image_similarity(0, 0xFFFF, bits=64))
    add("HASH_BITS 为 63", HASH_BITS == 63, HASH_BITS)
    add("相似度不超 1", image_similarity(0, 0) <= 1.0)
    add("相似度不为负", image_similarity(0, (1 << 63) - 1) >= 0.0)
    try:
        image_similarity(0, 0, bits=0)
        add("bits=0 抛错", False, "没抛")
    except VerificationError:
        add("bits=0 抛错", True)

    # --- images_differ 三态 ---
    v = images_differ(h2, h5)
    add("明确不同 → ok", v.ok is True, v.describe())
    add("明确不同 method=phash", v.method == "phash")
    add("明确不同 evidence 有 distance",
        "distance" in v.evidence, v.evidence)

    v = images_differ(h2, h2)
    add("完全相同 → fail", v.failed is True, v.describe())
    add("完全相同 confidence 0.9", abs(v.confidence - 0.9) < 1e-9, v.confidence)

    # 灰区：距离 6（阈值默认 6 时算不同；调到 20 就进灰区）
    v = images_differ(0, 0b111111, threshold=20, unchanged_max=5)
    add("灰区 → unknown", v.unknown is True, v.describe())
    add("灰区 reason 说明无法判定", "无法判定" in v.reason, v.reason)

    v = images_differ(None, h2)
    add("哈希缺失 → unknown", v.unknown is True)

    # 边界：距离正好 = threshold → 算不同
    v = images_differ(0, 0b111111, threshold=6, unchanged_max=5)
    add("距离=阈值 算不同", v.ok is True, v.describe())
    # 边界：距离正好 = unchanged_max → 算相同
    v = images_differ(0, 0b11111, threshold=6, unchanged_max=5)
    add("距离=unchanged_max 算相同", v.failed is True, v.describe())

    # --- PNG 解析 ---
    png = _make_png(64, 64, lambda x, y: (255, 255, 255) if x < 32
                    else (0, 0, 0))
    add("PNG 是合法签名", png.startswith(b"\x89PNG\r\n\x1a\n"))
    hp = phash_from_png(png)
    add("PNG 解析 hash 与直接算一致", hp == h2, (hp, h2))

    # 灰度 PNG（color_type 0）
    import struct
    import zlib

    def _png_gray(w, h):
        raw = bytearray()
        for y in range(h):
            raw.append(0)
            for x in range(w):
                raw.append(255 if x < w // 2 else 0)

        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload +
                    struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        return (b"\x89PNG\r\n\x1a\n" + ihdr
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))

    hg = phash_from_png(_png_gray(64, 64))
    add("灰度 PNG 可解析且 hash 一致", hg == h2, (hg, h2))

    # filter=1 (Sub) 的 PNG。注意 RGB 三通道，Sub 滤波是**逐字节**做差
    # 且跨通道连续（bpp=3 时 line[i] -= line[i-3]），不是逐像素。
    def _png_sub(w, h):
        raw = bytearray()
        bpp = 3
        for y in range(h):
            raw.append(1)  # Sub filter
            line = bytearray()
            for x in range(w):
                v = 255 if x < w // 2 else 0
                line.extend((v, v, v))
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                raw.append((line[i] - left) & 0xFF)

        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload +
                    struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        return (b"\x89PNG\r\n\x1a\n" + ihdr
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))

    try:
        hs = phash_from_png(_png_sub(64, 64))
        add("Sub filter PNG 逆滤波正确", hs == h2, (hex(hs), hex(h2)))
    except VerificationError as exc:
        add("Sub filter PNG 逆滤波正确", False, str(exc))

    # 异常 PNG
    for bad, label in ((b"", "空数据"), (b"notapng", "非 PNG签名"),
                       (b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "截断")):
        try:
            phash_from_png(bad)
            add("PNG 异常抛错(%s)" % label, False, "没抛")
        except VerificationError:
            add("PNG 异常抛错(%s)" % label, True)

    # 隔行 PNG 要拒（造一个 IHDR 里 interlace=1 的）
    def _png_interlaced(w, h):
        raw = bytearray([0] * ((w * 3 + 1) * h))

        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload +
                    struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 1))
        return (b"\x89PNG\r\n\x1a\n" + ihdr
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))

    try:
        phash_from_png(_png_interlaced(8, 8))
        add("隔行 PNG 被拒", False, "没抛")
    except VerificationError as exc:
        add("隔行 PNG 被拒", "隔行" in str(exc), str(exc))

    # 调色板 PNG 要拒
    def _png_palette(w, h):
        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload +
                    struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0))
        plte = chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
        raw = bytes([0] * ((w + 1) * h))
        return (b"\x89PNG\r\n\x1a\n" + ihdr + plte
                + chunk(b"IDAT", zlib.compress(raw, 6))
                + chunk(b"IEND", b""))

    try:
        phash_from_png(_png_palette(4, 4))
        add("调色板 PNG 被拒", False, "没抛")
    except VerificationError as exc:
        add("调色板 PNG 被拒", "调色板" in str(exc), str(exc))

    # --- snapshot_hash ---
    class _Shot(object):
        def __init__(self, data):
            self.data = data

    add("snapshot_hash 从 data 算", snapshot_hash(_Shot(png)) == h2)
    add("snapshot_hash None → None", snapshot_hash(None) is None)
    add("snapshot_hash 空 data → None", snapshot_hash(_Shot(b"")) is None)
    add("snapshot_hash 非 PNG → None",
        snapshot_hash(_Shot(b"JPEGDATA")) is None)

    tmpd = tempfile.mkdtemp(prefix="avatar-vfy-")
    fp = os.path.join(tmpd, "s.png")
    with open(fp, "wb") as f:
        f.write(png)

    class _PathShot(object):
        def __init__(self, path):
            self.path = path

    add("snapshot_hash 从 path 算", snapshot_hash(_PathShot(fp)) == h2)
    add("snapshot_hash 坏路径 → None",
        snapshot_hash(_PathShot(os.path.join(tmpd, "nope.png"))) is None)

    # --- result_of ---
    base = VerifyResult.ok_(method="m")
    add("result_of 原样返回 VerifyResult",
        result_of(base) is base)
    add("result_of True → ok", result_of(True).ok is True)
    add("result_of False → fail", result_of(False).failed is True)
    add("result_of None → unknown", result_of(None).unknown is True)
    d = {"verdict": "ok", "method": "x", "reason": "y",
         "evidence": {"k": 1}}
    rr = result_of(d)
    add("result_of dict verdict",
        rr.ok is True and rr.method == "x" and rr.evidence["k"] == 1, rr)
    rr = result_of({"ok": True})
    add("result_of dict ok 键", rr.ok is True)
    rr = result_of("乱写的")
    add("result_of 未知类型 → unknown", rr.unknown is True, rr.reason)

    # --- summarize ---
    s = summarize([VerifyResult.ok_(), VerifyResult.ok_()])
    add("summarize 全 ok", s["all_ok"] is True and s["ok"] == 2, s)
    add("summarize 全 ok verdict", s["verdict"] == VERIFY_OK)
    s = summarize([VerifyResult.ok_(), VerifyResult.fail()])
    add("summarize 有失败", s["verdict"] == VERIFY_FAIL and s["all_ok"] is False, s)
    add("summarize any_ok", s["any_ok"] is True)
    s = summarize([VerifyResult.unknown_(), VerifyResult.unknown_()])
    add("summarize 全未知", s["verdict"] == VERIFY_UNKNOWN and s["fail"] == 0, s)
    add("summarize 全未知 not all_ok", s["all_ok"] is False)
    s = summarize([])
    add("summarize 空输入安全", s["total"] == 0 and s["all_ok"] is False, s)

    import shutil
    shutil.rmtree(tmpd, ignore_errors=True)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "verify.result"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
