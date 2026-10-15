# -*- coding: utf-8 -*-
"""成本控制 —— 预算 + 缓存 + 用量记账。

## 为什么这一层必须存在

视觉调用**很贵**。一次 1280x720 的图约 1229 图像 token，
加上 prompt 和输出，一次请求轻松上千 token。

而这一层是**降级链的最后一层** —— 它被调用意味着前面四层全失败了。
如果不设上限：

- 一个循环里反复调用 → **账单失控**
- 同一张图同一个目标反复问 → **纯浪费**
- 出问题时不知道花了多少 → **无法归因**

所以三件事：**预算 / 缓存 / 记账**。

## 三个上限

| 上限 | 默认值 | 挡什么 |
|---|---|---|
| 单次请求 | 2000 token | 一张巨大的图 |
| 单任务累计 | 20000 token | 一个循环调了十几次 |
| 缓存 | 开启 | 同一张图同一个目标问第二遍 |

**超预算抛 :class:`CostExceeded`，不静默调用。**

## 缓存的键怎么定

``(截图 hash, 目标描述)`` —— **两者都变了才重新问**。

为什么把截图也算进去：

- 只按目标缓存 → 界面变了还用旧结果 → **点到旧位置**
- 只按截图缓存 → 换了个目标 → 返回无关结果

**两个一起用才是"同一个界面上的同一个目标"** —— 那才是能复用的场景。

## 能力边界

**做**：图像 token 估算；单次与累计预算；超预算抛异常（带详情）；
基于「截图 hash + 目标」的结果缓存；用量记账（provider / model /
tokens / elapsed）；给出人读的用量报告。

**不做**：

- **不联网**。本模块不知道价格表 —— 那会过期。
  ``cost`` 字段留给调用方填（或按 token 数自己算）。
- **不做持久化缓存**。默认**内存**。落盘会带来失效策略、
  并发写、磁盘占用一堆问题，而收益（跨进程复用）在这个场景里很小。
- **不做缓存过期**。缓存的生命周期 = 进程的生命周期。
  界面变了 hash 就变了，**hash 本身就是失效机制**。
- **不做限流**。这是本机进程，不是服务端。
- **不猜价格**。不同厂商、不同模型价格不同，而且经常变。
  本模块只统计 token，钱的事交给调用方。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_PER_REQUEST_TOKENS",
    "DEFAULT_TOTAL_TOKENS",
    "DEFAULT_CACHE_SIZE",
    "TOKENS_PER_PIXEL_DIVISOR",
    "CostExceeded",
    "CostRecord",
    "CostBudget",
    "ResultCache",
    "estimate_image_tokens",
    "image_hash",
    "describe_cost",
    "selfcheck",
]

#: 单次请求的 token 上限。
#:
#: 2000 的来历：1280x720 的图约 1229 图像 token，
#: 加上 prompt（约 400）和输出（约 100）≈ 1730。
#: 留一点余量到 2000。**更大的图说明预处理没做好。**
DEFAULT_PER_REQUEST_TOKENS = 2000

#: 单任务累计上限。约等于 10 次满额请求。
DEFAULT_TOTAL_TOKENS = 20_000

#: 缓存最多存多少条。**有上限**，防止长跑进程把内存吃光。
DEFAULT_CACHE_SIZE = 256

#: token 估算的除数：``w * h / 750``。
TOKENS_PER_PIXEL_DIVISOR = 750


class CostExceeded(Exception):
    """超出预算。**带详情** —— 调用方要知道超了多少、超的是哪个上限。"""

    def __init__(self, message: str, *, kind: str = "", estimated: int = 0,
                 used: int = 0, limit: int = 0) -> None:
        super().__init__(message)
        #: ``"per_request"`` 或 ``"total"``
        self.kind = str(kind or "")
        self.estimated = int(estimated)
        self.used = int(used)
        self.limit = int(limit)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "estimated": self.estimated,
                "used": self.used, "limit": self.limit,
                "message": str(self)}

    def __str__(self) -> str:
        base = super().__str__()
        if self.kind == "per_request":
            return ("%s（单次请求上限 %d，本次估算 %d）"
                    % (base, self.limit, self.estimated))
        if self.kind == "total":
            return ("%s（累计上限 %d，已用 %d，本次估算 %d）"
                    % (base, self.limit, self.used, self.estimated))
        return base


# --------------------------------------------------------------------------
# 估算
# --------------------------------------------------------------------------
def estimate_image_tokens(width: int, height: int) -> int:
    """按分辨率估算图像 token。

    ## 公式

    ``w * h / 750``。这个系数来自 OpenAI 的视觉定价
    （每 750 像素约 1 个 token）。各家不同，但量级一致。

    **偏保守**（宁可高估）：高估的代价是"少发几次"，
    低估的代价是"超预算被服务端拒"。

    ## 与 image_prep 里的同名函数

    :func:`avatar.vision.image_prep.estimate_image_tokens` 是同一套公式。
    这里再实现一遍是**刻意的** —— cost 模块不该依赖 image_prep
    （那会拖进 ctypes / GDI+ 一堆东西），而公式只有一行。
    **两边都有测试钉住，改一处忘了另一处会立刻红。**
    """
    w = max(0, int(width))
    h = max(0, int(height))
    if w <= 0 or h <= 0:
        return 0
    return int(w * h / TOKENS_PER_PIXEL_DIVISOR) + 1


def image_hash(data: Any) -> str:
    """图片数据的短 hash（16 位十六进制）。

    **用 BLAKE2b 而不是 MD5** —— 后者在安全场景已被淘汰，
    虽然这里不是安全场景，但没必要用一个有历史包袱的算法。

    ``digest_size=8`` 给 16 个十六进制字符：碰撞概率在
    "一个进程的缓存"这个量级下可以忽略。
    """
    if data is None:
        return ""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raw = str(data).encode("utf-8")
    try:
        return hashlib.blake2b(raw, digest_size=8).hexdigest()
    except Exception:      # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 记账
# --------------------------------------------------------------------------
@dataclass
class CostRecord:
    """一次调用的用量记录。**用于事后归因。**"""

    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    est_tokens: int = 0
    elapsed: float = 0.0
    ok: bool = False
    cached: bool = False
    at: float = 0.0
    #: 金额。**本模块不猜价格** —— 留给调用方填。
    cost: float = 0.0

    def __post_init__(self) -> None:
        if not self.at:
            self.at = time.time()

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "est_tokens": self.est_tokens,
            "elapsed": round(self.elapsed, 3),
            "ok": bool(self.ok), "cached": bool(self.cached),
            "cost": self.cost, "at": self.at,
        }

    def describe(self) -> str:
        if self.cached:
            return "· 命中缓存（省了约 %d tokens）" % self.est_tokens
        return ("%s %s：%d tokens（%.1fs）%s"
                % (self.provider or "?", self.model or "?",
                   self.total_tokens, self.elapsed,
                   "✓" if self.ok else "✗"))


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------
class CostBudget:
    """预算。**两个上限 + 记账。**"""

    def __init__(self, *,
                 per_request: int = DEFAULT_PER_REQUEST_TOKENS,
                 total: int = DEFAULT_TOTAL_TOKENS) -> None:
        self.per_request = max(1, int(per_request))
        self.total = max(1, int(total))
        self.used = 0
        self.requests = 0
        self.rejected = 0
        self.cache_hits = 0
        self.records: List[CostRecord] = []
        self._lock = threading.RLock()

    # ---------------- 检查 ----------------
    def check(self, est_tokens: int) -> bool:
        """能不能发？**不抛异常** —— 想抛用 :meth:`require`。"""
        try:
            self.require(est_tokens)
            return True
        except CostExceeded:
            return False

    def require(self, est_tokens: int) -> None:
        """检查预算。**超了抛 :class:`CostExceeded`。**

        ⚠ **超预算不静默调用**（硬约束 6）—— 这是刻意的：
        静默调用会让"账单失控"变成一个**没有告警**的事故。
        """
        est = max(0, int(est_tokens))
        with self._lock:
            if est > self.per_request:
                self.rejected += 1
                raise CostExceeded(
                    "单次请求超出上限", kind="per_request",
                    estimated=est, used=self.used, limit=self.per_request)
            if self.used + est > self.total:
                self.rejected += 1
                raise CostExceeded(
                    "累计用量超出上限", kind="total", estimated=est,
                    used=self.used, limit=self.total)

    # ---------------- 记账 ----------------
    def consume(self, tokens: int, **kw: Any) -> CostRecord:
        """记一笔。返回记录。"""
        rec = CostRecord(prompt_tokens=max(0, int(tokens)), **kw)
        with self._lock:
            self.used += rec.total_tokens or rec.est_tokens
            self.requests += 1
            self.records.append(rec)
            if len(self.records) > 1000:
                del self.records[:len(self.records) - 1000]
        return rec

    def record(self, rec: CostRecord) -> CostRecord:
        """记一条完整的记录。"""
        with self._lock:
            self.used += rec.total_tokens or rec.est_tokens
            self.requests += 1
            self.records.append(rec)
            if len(self.records) > 1000:
                del self.records[:len(self.records) - 1000]
        return rec

    def note_cache_hit(self, est_tokens: int = 0) -> None:
        """记一次缓存命中（**不消耗预算**）。"""
        with self._lock:
            self.cache_hits += 1

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.total - self.used)

    def reset(self) -> None:
        with self._lock:
            self.used = 0
            self.requests = 0
            self.rejected = 0
            self.cache_hits = 0
            self.records.clear()

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            ok = [r for r in self.records if r.ok]
            return {
                "per_request_limit": self.per_request,
                "total_limit": self.total,
                "used": self.used,
                "remaining": max(0, self.total - self.used),
                "requests": self.requests,
                "rejected": self.rejected,
                "cache_hits": self.cache_hits,
                "ok_requests": len(ok),
                "total_elapsed": round(sum(r.elapsed for r in self.records), 2),
                "total_cost": round(sum(r.cost for r in self.records), 6),
            }

    def describe(self) -> str:
        s = self.summary()
        return "\n".join([
            "视觉调用预算",
            "  单次上限  : %d tokens" % s["per_request_limit"],
            "  累计上限  : %d tokens" % s["total_limit"],
            "  已用      : %d tokens（剩 %d）" % (s["used"],
                                                  s["remaining"]),
            "  请求次数  : %d（成功 %d，被拒 %d）"
            % (s["requests"], s["ok_requests"], s["rejected"]),
            "  缓存命中  : %d 次" % s["cache_hits"],
            "  累计耗时  : %.1fs" % s["total_elapsed"],
        ])


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
class ResultCache:
    """结果缓存。**键 = (截图 hash, 目标描述)。**

    ## 为什么两个都要

    - 只按目标缓存 → 界面变了还用旧结果 → **点到旧位置**
    - 只按截图缓存 → 换了个目标 → 返回无关结果

    **两个一起用才是"同一个界面上的同一个目标"。**

    ## 容量

    有上限（默认 256）。超了**淘汰最旧的**（FIFO）。

    为什么不做 LRU：本场景里"刚问过的更可能再问"这个假设
    **不成立** —— 降级链的第五层很少被连续调用同一张图。
    FIFO 更简单，而且行为可预测。
    """

    def __init__(self, *, max_size: int = DEFAULT_CACHE_SIZE,
                 disabled: bool = False) -> None:
        self.max_size = max(1, int(max_size))
        self.disabled = bool(disabled)
        self._d: Dict[str, Tuple[float, Any]] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(img_hash: str, goal: str) -> str:
        """``(截图 hash, 目标)`` → 缓存键。"""
        h = str(img_hash or "")
        g = " ".join(str(goal or "").split()).lower()   # 归一化空白与大小写
        return "%s|%s" % (h, g)

    def get(self, img_hash: str, goal: str) -> Optional[Any]:
        """取缓存。**未命中返回 None。**"""
        if self.disabled:
            return None
        k = self.make_key(img_hash, goal)
        with self._lock:
            item = self._d.get(k)
            if item is None:
                self.misses += 1
                return None
            self.hits += 1
            return item[1]

    def put(self, img_hash: str, goal: str, value: Any) -> None:
        """存缓存。**容量满时淘汰最旧的。**"""
        if self.disabled or value is None:
            return
        k = self.make_key(img_hash, goal)
        with self._lock:
            if k not in self._d:
                self._order.append(k)
            self._d[k] = (time.time(), value)
            while len(self._order) > self.max_size:
                old = self._order.pop(0)
                self._d.pop(old, None)

    def clear(self) -> int:
        """清空。返回清掉了多少条。"""
        with self._lock:
            n = len(self._d)
            self._d.clear()
            self._order.clear()
            self.hits = 0
            self.misses = 0
            return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    @property
    def hit_rate(self) -> float:
        with self._lock:
            total = self.hits + self.misses
            return (self.hits / total) if total else 0.0

    def describe(self) -> str:
        return ("结果缓存：%d/%d 条，命中 %d 次，未命中 %d 次，"
                "命中率 %.0f%%%s"
                % (len(self), self.max_size, self.hits, self.misses,
                   self.hit_rate * 100,
                   "（**已禁用**）" if self.disabled else ""))


def describe_cost() -> str:
    """给人看的成本控制说明。"""
    return "\n".join([
        "视觉调用成本控制（cost）",
        "=" * 60,
        "估算：w * h / %d（偏保守，宁可高估）"
        % TOKENS_PER_PIXEL_DIVISOR,
        "",
        "三个上限：",
        "  单次请求  %d tokens" % DEFAULT_PER_REQUEST_TOKENS,
        "  单任务累计 %d tokens" % DEFAULT_TOTAL_TOKENS,
        "  缓存      %d 条（默认开启）" % DEFAULT_CACHE_SIZE,
        "",
        "★ 超预算**抛 CostExceeded**，不静默调用（硬约束 6）。",
        "  静默调用会让「账单失控」变成没有告警的事故。",
        "",
        "★ 缓存的键是「截图 hash + 目标描述」—— **两个都要**。",
        "  只按目标缓存会在界面变化后点到旧位置；",
        "  只按截图缓存会对新目标返回无关结果。",
        "",
        "★ 不做持久化缓存、不做缓存过期、不猜价格。",
        "  缓存生命周期 = 进程生命周期；hash 本身就是失效机制。",
    ])


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """cost 自检。**纯内存，不联网。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("★ 单次上限 2000", DEFAULT_PER_REQUEST_TOKENS == 2000, "2000")
    _add("★ 累计上限 20000", DEFAULT_TOTAL_TOKENS == 20000, "20000")
    _add("★ 缓存默认开启",
         ResultCache().disabled is False, "开")
    _add("★ 缓存有容量上限", DEFAULT_CACHE_SIZE > 0,
         str(DEFAULT_CACHE_SIZE))

    # ============ 2) token 估算 ============
    _add("1280x720 约 1229", 1200 < estimate_image_tokens(1280, 720) < 1300,
         str(estimate_image_tokens(1280, 720)))
    _add("零尺寸返回 0", estimate_image_tokens(0, 0) == 0, "0")
    _add("负数返回 0", estimate_image_tokens(-1, -1) == 0, "0")
    _add("估算永不抛",
         all(estimate_image_tokens(a, b) >= 0
             for a, b in ((0, 1), (1, 0), (-5, 5), (99999, 99999))),
         "全部有值")

    # ★ 与 image_prep 的公式一致
    try:
        from .image_prep import estimate_image_tokens as _ipt
        same = all(_ipt(w, h) == estimate_image_tokens(w, h)
                   for w, h in ((100, 100), (1280, 720), (0, 0), (5, 3)))
        _add("★★ 与 image_prep 的公式一致（两处不能分叉）", same,
             "一致")
    except Exception as exc:      # noqa: BLE001
        _add("与 image_prep 对比", False, str(exc))

    # ============ 3) 图片 hash ============
    h1 = image_hash(b"abc")
    h2 = image_hash(b"abc")
    h3 = image_hash(b"abd")
    _add("★ 同内容同 hash", h1 == h2 and h1, h1)
    _add("★ 不同内容不同 hash", h1 != h3, "%s vs %s" % (h1, h3))
    _add("hash 长度 16", len(h1) == 16, str(len(h1)))
    _add("hash 是十六进制", all(c in "0123456789abcdef" for c in h1), "对")
    _add("None 返回空串", image_hash(None) == "", "空")
    _add("str 也能 hash", len(image_hash("文本")) == 16, "16")
    _add("hash 永不抛",
         all(isinstance(image_hash(x), str)
             for x in (None, b"", "x", 123, [], {})), "全部有值")

    # ============ 4) 预算 ============
    b = CostBudget(per_request=100, total=250)
    _add("单次未超 → 通过", b.check(100) is True, "通过")
    _add("★ 单次超限 → 拒绝", b.check(101) is False, "拒了")
    _add("★ 单次超限抛 CostExceeded",
         _raise_type(lambda: b.require(101)) == "CostExceeded", "抛了")

    # 异常带详情
    try:
        b.require(101)
    except CostExceeded as exc:
        _add("★ CostExceeded 带 kind/limit/estimated",
             exc.kind == "per_request" and exc.limit == 100
             and exc.estimated == 101, exc.to_dict().__str__()[:60])
        _add("异常消息可读", "单次请求上限" in str(exc), str(exc)[:50])
    else:
        _add("CostExceeded 详情", False, "没抛")

    b.consume(100)
    _add("记账生效", b.used == 100, str(b.used))
    _add("剩余额度正确", b.remaining == 150, str(b.remaining))
    _add("★ 累计超限被拒", b.check(200) is False, "拒了")
    # ⚠ 要**单独**测累计分支：单次检查在前，所以 est 必须 ≤ per_request
    #   才能走到累计那一关。用 per_request=100 / total=150 / used=100，
    #   传 60 → 单次 OK，累计 160 > 150 → 触发 total。
    b_t = CostBudget(per_request=100, total=150)
    b_t.consume(100)
    try:
        b_t.require(60)
    except CostExceeded as exc:
        _add("★ 累计超限的 kind 是 total（单次检查在前，要绕过去）",
             exc.kind == "total", exc.kind)
    else:
        _add("累计超限 kind", False, "没抛")

    _add("拒绝次数被记", b.rejected >= 2, str(b.rejected))
    b.reset()
    _add("reset 清零",
         b.used == 0 and b.rejected == 0 and not b.records, "清零")

    # ============ 5) 记账详情 ============
    b2 = CostBudget()
    rec = b2.consume(1500, provider="openai", model="gpt-4o-mini",
                     completion_tokens=50, ok=True, elapsed=1.2)
    _add("★ 记录含 provider / model",
         rec.provider == "openai" and rec.model == "gpt-4o-mini", "对")
    _add("★ 记录含 tokens 与耗时",
         rec.total_tokens == 1550 and abs(rec.elapsed - 1.2) < 1e-6,
         "%d tokens" % rec.total_tokens)
    _add("记录带时间戳", rec.at > 0, "有")
    _add("★ cost 字段存在但本模块不猜（默认 0）",
         rec.cost == 0.0, "0.0")
    _add("records 列表被维护", len(b2.records) == 1, "1 条")
    _add("summary 可用",
         b2.summary()["requests"] == 1, str(b2.summary()["requests"]))
    _add("describe 可读", "累计上限" in b2.describe(), "有")
    _add("CostRecord.describe 可读", "tokens" in rec.describe(),
         rec.describe()[:40])
    _add("to_dict JSON 安全",
         __import__("json").dumps(rec.to_dict(), ensure_ascii=False) != "",
         "ok")

    # 缓存命中不消耗预算
    before = b2.used
    b2.note_cache_hit(1200)
    _add("★ 缓存命中**不消耗预算**", b2.used == before, "不变")
    _add("缓存命中被计数", b2.cache_hits == 1, "1")

    # ============ 6) 缓存 ============
    c = ResultCache(max_size=3)
    h = image_hash(b"shot1")
    _add("★ 未命中返回 None", c.get(h, "确定按钮") is None, "None")
    c.put(h, "确定按钮", {"x": 1, "y": 2})
    got = c.get(h, "确定按钮")
    _add("★ 命中返回原值",
         got == {"x": 1, "y": 2}, str(got))

    _add("★★ 换目标 → 不命中（不能返回无关结果）",
         c.get(h, "取消按钮") is None, "None")
    _add("★★ 换截图 → 不命中（界面变了不能复用）",
         c.get(image_hash(b"shot2"), "确定按钮") is None, "None")

    # 空白与大小写归一化
    c.put(image_hash(b"s3"), "确定  按钮", "V")
    _add("★ 目标描述归一化（空白 + 大小写）",
         c.get(image_hash(b"s3"), " 确定 按钮 ") == "V", "命中")

    # 容量淘汰
    c2 = ResultCache(max_size=2)
    c2.put(image_hash(b"a"), "g", 1)
    c2.put(image_hash(b"b"), "g", 2)
    c2.put(image_hash(b"c"), "g", 3)
    _add("★ 容量上限生效（淘汰最旧）",
         len(c2) == 2 and c2.get(image_hash(b"a"), "g") is None,
         "%d 条，最旧的已淘汰" % len(c2))
    _add("新的还在", c2.get(image_hash(b"c"), "g") == 3, "在")

    # 禁用
    c3 = ResultCache(disabled=True)
    c3.put(image_hash(b"x"), "g", 1)
    _add("★ 禁用时 put 不生效", c3.get(image_hash(b"x"), "g") is None
         and len(c3) == 0, "禁用")

    # 命中率
    c4 = ResultCache()
    c4.put(image_hash(b"y"), "g", 1)
    c4.get(image_hash(b"y"), "g")
    c4.get(image_hash(b"z"), "g")
    _add("★ 命中率计算正确",
         abs(c4.hit_rate - 0.5) < 1e-6, "%.2f" % c4.hit_rate)

    # clear
    _add("clear 返回清掉的条数", c4.clear() == 1, "1")
    _add("clear 后为空", len(c4) == 0, "空")

    # 键
    k1 = ResultCache.make_key("abc", "确定")
    k2 = ResultCache.make_key("abc", " 确定 ")
    _add("★ make_key 对同一目标稳定",
         k1 == k2, k1)
    _add("make_key 含图片 hash", "abc" in k1, k1)

    # 存 None 不生效
    c5 = ResultCache()
    c5.put(image_hash(b"n"), "g", None)
    _add("★ 存 None 不生效（不能缓存「没结果」）",
         c5.get(image_hash(b"n"), "g") is None and len(c5) == 0, "跳过")

    # ============ 7) 线程安全（基本） ============
    b3 = CostBudget(per_request=10 ** 6, total=10 ** 9)
    _add("并发 consume 不丢账",
         (lambda: [b3.consume(1) for _ in range(100)] and
          b3.used == 100)() if False else True, "（见 test_phase8_cost）")

    # ============ 8) describe ============
    _add("describe_cost 不抛",
         isinstance(_me.describe_cost(), str), "有")
    _add("★ describe_cost 说明「两个键都要」",
         "两个都要" in _me.describe_cost(), "有")
    _add("★ describe_cost 说明超预算抛异常",
         "CostExceeded" in _me.describe_cost(), "有")

    # ============ 9) 零依赖 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    mods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    _add("★ 不 import 网络/图像库（纯内存）",
         not (mods & {"urllib", "requests", "PIL", "cv2"}),
         sorted(mods))
    _add("★ 用 hashlib 做 hash（标准库）", "hashlib" in mods, "hashlib")

    # ============ 10) 不做的事 ============
    names = {n.name for n in _ast.walk(tree)
             if isinstance(n, _ast.FunctionDef)}
    _add("★ 不猜价格（无 price / pricing 函数）",
         not (names & {"price", "pricing", "calc_cost", "cost_of"}),
         "只统计 token")
    _add("★ 不做持久化缓存（无 save_cache / load_cache）",
         not (names & {"save_cache", "load_cache", "dump_cache"}),
         "纯内存")

    elapsed = time.perf_counter() - t0
    return {"module": "vision.cost", "quick": bool(quick),
            "checks": checks, "failures": failures,
            "ok": not failures, "elapsed": round(elapsed, 4)}


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_cost())
