# -*- coding: utf-8 -*-
"""熵源统一管理：所有随机数必须从这里取。

为什么不让各模块自己 ``import random``
--------------------------------------
1. **可复现**。同一 seed + 同一路径 = 同一轨迹。这在测试里是硬要求
   （质量硬约束 #5），在排障时也是救命的 —— 出问题的轨迹可以精确重放。
   如果各模块各自持有 ``random.Random`` 实例，种子就管不住全局。
2. **可审计**。想知道"这个动作的随机性从哪来"，只需要看这一个文件。
3. **可替换**。将来要把伪随机换成真随机（``secrets``）、或换成录制的
   真人轨迹样本，只需改这里。

用法
----
::

    from avatar.human_entropy import entropy

    entropy.seed(42)              # 可复现模式
    entropy.uniform(0.1, 0.2)
    entropy.gauss(100.0, 12.0)
    entropy.shuffle(items)
    entropy.seed(None)            # 回到真随机

纪律
----
本包内**禁止**出现 ``import random`` / ``random.random()`` 等直接调用。
质量硬约束 #4 的落地检查见 ``scripts/selfcheck.py`` 的
``check_entropy_discipline``。
"""
from __future__ import annotations

import math
import random as _random_module
import threading
from typing import Iterable, List, Optional, Sequence, TypeVar

__all__ = [
    "seed",
    "get_seed",
    "is_reproducible",
    "reset",
    "Rng",
    "random",
    "uniform",
    "gauss",
    "normalvariate",
    "lognormvariate",
    "triangular",
    "choice",
    "choices",
    "shuffle",
    "sample",
    "randint",
    "betavariate",
    "expovariate",
    "clamped_gauss",
    "jitter",
    "derive",
]

T = TypeVar("T")


# 全局默认种子。None = 真随机（用系统熵）。
_DEFAULT_SEED: Optional[int] = None


class Rng:
    """一个受管的随机源。

    内部持有一个 ``random.Random`` 实例。**只有本模块可以 new 它**，
    其余模块通过模块级函数使用全局默认实例，或者显式构造一个
    隔离的 Rng（用于"两条独立轨迹"这种需要各自复现的场合）。
    """

    __slots__ = ("_seed", "_rng", "_lock")

    def __init__(self, seed_value: Optional[int] = None):
        self._seed = seed_value
        self._rng = _random_module.Random(seed_value)
        # 熵源可能被多线程（并行轨迹生成）共享；Random 本身非线程安全
        self._lock = threading.Lock()

    # -- 种子管理 --------------------------------------------------------
    @property
    def seed(self) -> Optional[int]:
        return self._seed

    def reseed(self, seed_value: Optional[int]) -> None:
        """重设种子。传 None 表示用系统熵（不可复现）。"""
        with self._lock:
            self._seed = seed_value
            self._rng = _random_module.Random(seed_value)

    def derive(self, tag: str) -> "Rng":
        """从当前种子派生一个子熵源（保持可复现性）。

        用途：一条轨迹里的"路径抖动"和"时间抖动"如果共用一个序列，
        调用顺序一变结果就全变。派生后各自独立、互不干扰。

        注意与模块级 :func:`derive` 的区别：这个是从**本实例**派生，
        而模块级那个是从**全局**熵源派生。两者算法一致。
        """
        return Rng(_combine_seed(self._seed, tag))

    # -- 取值 ------------------------------------------------------------
    def random(self) -> float:
        with self._lock:
            return self._rng.random()

    def uniform(self, a: float, b: float) -> float:
        with self._lock:
            return self._rng.uniform(a, b)

    def gauss(self, mu: float, sigma: float) -> float:
        with self._lock:
            return self._rng.gauss(mu, sigma)

    def normalvariate(self, mu: float, sigma: float) -> float:
        with self._lock:
            return self._rng.normalvariate(mu, sigma)

    def lognormvariate(self, mu: float, sigma: float) -> float:
        with self._lock:
            return self._rng.lognormvariate(mu, sigma)

    def triangular(self, low: float, high: float, mode: float) -> float:
        with self._lock:
            return self._rng.triangular(low, high, mode)

    def betavariate(self, alpha: float, beta: float) -> float:
        with self._lock:
            return self._rng.betavariate(alpha, beta)

    def expovariate(self, lambd: float) -> float:
        with self._lock:
            return self._rng.expovariate(lambd)

    def choice(self, seq: Sequence[T]) -> T:
        with self._lock:
            return self._rng.choice(list(seq))

    def choices(self, population: Sequence[T], weights=None, k: int = 1) -> List[T]:
        with self._lock:
            return self._rng.choices(list(population), weights=weights, k=k)

    def shuffle(self, items: List[T]) -> None:
        with self._lock:
            self._rng.shuffle(items)

    def sample(self, population: Sequence[T], k: int) -> List[T]:
        with self._lock:
            return self._rng.sample(list(population), k)

    def randint(self, a: int, b: int) -> int:
        with self._lock:
            return self._rng.randint(a, b)


# --------------------------------------------------------------------------
# 全局默认熵源
# --------------------------------------------------------------------------
_GLOBAL = Rng(_DEFAULT_SEED)


def seed(seed_value: Optional[int]) -> None:
    """设定全局种子。

    ``seed(42)`` 进可复现模式；``seed(None)`` 回真随机。
    """
    global _DEFAULT_SEED
    _DEFAULT_SEED = seed_value
    _GLOBAL.reseed(seed_value)
    # 派生流按种子缓存，种子一变必须整体作废
    _DERIVED.clear()


def get_seed() -> Optional[int]:
    """取当前全局种子。None 表示真随机。"""
    return _GLOBAL.seed


def is_reproducible() -> bool:
    """当前是否处于可复现模式。"""
    return _GLOBAL.seed is not None


def reset() -> None:
    """回到真随机（等价于 ``seed(None)``）。"""
    seed(None)


def _combine_seed(base: Optional[int], tag: str) -> int:
    """把 (基础种子, 标签) 混合成一个新的整数种子。

    ``base`` 为 None（真随机模式）时，用一个系统随机的基数 ——
    派生流仍然是"一次运行内稳定"的，只是跨运行不复现。
    """
    if base is None:
        base = _random_module.randrange(1 << 30)
    h = 0
    for ch in tag:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (int(base) * 1_000_003 + h) & 0x7FFFFFFF


class _DerivedRng(Rng):
    """派生熵源：**幂等**地持有同一个底层流。

    与 :class:`Rng` 的唯一区别是 ``derive()`` 返回它自己 ——
    于是 ``E.derive("mouse")`` 反复调用拿到的是**同一个**流，
    序列会正常推进，而不是每次都从同一起点重放。

    这一点很容易写错：如果 ``derive()`` 每次都 new 一个同种子的
    Rng，那么 ``[E.derive("mouse").uniform(0,1) for _ in range(5)]``
    会得到 5 个**完全相同**的数字 —— 派生流看起来"可复现"，
    实际上是一个死掉的常数源。测试里那条"同 tag 可复现"的断言
    会通过，但整条流没有任何熵。
    """

    __slots__ = ()

    def derive(self, tag: str) -> "Rng":
        return self


#: 按 (基础种子, tag) 缓存派生流。种子一变，缓存整体作废 ——
#: 否则 ``seed(1)`` 之后再 ``seed(2)`` 会拿到 seed=1 时期的旧流。
_DERIVED: "dict[tuple, _DerivedRng]" = {}


def _derived_for(base: Optional[int], tag: str) -> _DerivedRng:
    key = (base, tag)
    got = _DERIVED.get(key)
    if got is None:
        got = _DerivedRng(_combine_seed(base, tag))
        # 真随机模式（base=None）下基数每次都不同，缓存会无限膨胀，故不缓存
        if base is not None:
            _DERIVED[key] = got
    return got


def derive(tag: str) -> Rng:
    """从当前全局种子派生一个**独立的、可推进的**子熵源。

    为什么需要它：一条轨迹里"路径抖动"和"时间抖动"如果共用一个
    序列，任何一处调用顺序的变化都会让另一处的结果跟着变 ——
    调参时根本分不清是哪个环节在起作用。派生后两条流各自独立。

    关键性质（``tests/test_phase3_entropy.py`` 逐条钉住）：

    * **可复现** —— 同一种子 + 同一 tag 必得同一序列；
    * **可推进** —— 反复取同 tag 拿到的是**同一个流**，逐次调用
      得到不同的值（这条最关键，写错就成了常数源）；
    * **互不干扰** —— 不同 tag 得到不同序列；
    * **不污染主流** —— 派生/使用派生流**不会**推进全局序列，
      所以"派生前后主流序列一致"。

    实现上不触碰全局 ``_GLOBAL``，只读它的种子，因此第四条自动成立。
    """
    return _derived_for(_GLOBAL.seed, tag)


def random() -> float:
    return _GLOBAL.random()


def uniform(a: float, b: float) -> float:
    return _GLOBAL.uniform(a, b)


def gauss(mu: float, sigma: float) -> float:
    return _GLOBAL.gauss(mu, sigma)


def normalvariate(mu: float, sigma: float) -> float:
    return _GLOBAL.normalvariate(mu, sigma)


def lognormvariate(mu: float, sigma: float) -> float:
    return _GLOBAL.lognormvariate(mu, sigma)


def triangular(low: float, high: float, mode: float) -> float:
    return _GLOBAL.triangular(low, high, mode)


def betavariate(alpha: float, beta: float) -> float:
    return _GLOBAL.betavariate(alpha, beta)


def expovariate(lambd: float) -> float:
    return _GLOBAL.expovariate(lambd)


def choice(seq: Sequence[T]) -> T:
    return _GLOBAL.choice(seq)


def choices(population: Sequence[T], weights=None, k: int = 1) -> List[T]:
    return _GLOBAL.choices(population, weights=weights, k=k)


def shuffle(items: List[T]) -> None:
    _GLOBAL.shuffle(items)


def sample(population: Sequence[T], k: int) -> List[T]:
    return _GLOBAL.sample(population, k)


def randint(a: int, b: int) -> int:
    return _GLOBAL.randint(a, b)


# --------------------------------------------------------------------------
# 组合工具
# --------------------------------------------------------------------------
def clamped_gauss(mu: float, sigma: float, low: float, high: float) -> float:
    """截断高斯：落在 [low, high] 之外就重新采样，最多 16 次。

    直接用 ``min(max(...))`` 截断会让结果堆积在边界（分布畸变，
    一次采样有 ~32% 概率落在 ±1σ 外，两端会明显"粘边"）；
    拒绝采样保持分布形状不变。

    为什么要截断：真人不会出现负的反应时间、也不会出现 3 秒的按键间隔。
    纯高斯在 4σ 外的尾巴会生成物理上荒谬的值。
    """
    if low > high:
        low, high = high, low
    for _ in range(16):
        v = _GLOBAL.gauss(mu, sigma)
        if low <= v <= high:
            return v
    # 16 次都出界（sigma 极大或区间极窄）：退化为均匀分布
    return _GLOBAL.uniform(low, high)


def jitter(value: float, ratio: float, *, low: float = None, high: float = None) -> float:
    """给 ``value`` 加一个相对抖动（±ratio 比例），可选再钳制。"""
    delta = abs(value) * ratio
    v = value + _GLOBAL.uniform(-delta, delta)
    if low is not None and v < low:
        v = low
    if high is not None and v > high:
        v = high
    return v


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """熵源自检：种子管理、派生流、可复现性、组合工具。

    这里是**所有随机性的源头**，它错了后面全错，所以查得细一点。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    saved = _GLOBAL.seed
    try:
        # -- 种子管理 ---------------------------------------------------
        seed(4242)
        _add("seed/get_seed 往返", get_seed() == 4242, "get_seed()=%r" % (get_seed(),))
        _add("设种子后 is_reproducible", is_reproducible() is True, "")

        a = [uniform(0.0, 1.0) for _ in range(8)]
        seed(4242)
        b = [uniform(0.0, 1.0) for _ in range(8)]
        _add("同 seed 序列一致", a == b, "%d 个采样点逐点相同" % len(a))

        seed(4243)
        c = [uniform(0.0, 1.0) for _ in range(8)]
        _add("异 seed 序列不同", a != c, "")

        reset()
        _add("reset 后回到真随机", get_seed() is None and not is_reproducible(), "")

        # -- 派生流 -----------------------------------------------------
        seed(555)
        main1 = [uniform(0.0, 1.0) for _ in range(5)]
        seed(555)
        d1 = [derive("mouse").uniform(0.0, 1.0) for _ in range(5)]
        seed(555)
        d1b = [derive("mouse").uniform(0.0, 1.0) for _ in range(5)]
        seed(555)
        d3 = [derive("keyboard").uniform(0.0, 1.0) for _ in range(5)]
        seed(555)
        main2 = [uniform(0.0, 1.0) for _ in range(5)]

        _add("derive 同 tag 可复现", d1 == d1b, "")
        _add("derive 异 tag 互相独立", d1 != d3, "")
        _add("derive 不污染主流", main1 == main2, "")
        # ⭐ 这条最容易写错：如果 derive 每次 new 一个同种子 Rng，
        #    五次调用会拿到同一个数，派生流变成死掉的常数源。
        _add("derive 流真的在推进", len(set(d1)) == len(d1),
             "5 次调用得到 %d 个不同值" % len(set(d1)))

        # -- 组合工具 ---------------------------------------------------
        seed(1)
        vals = [clamped_gauss(0.0, 1.0, -2.0, 2.0) for _ in range(400)]
        _add("clamped_gauss 落在区间内",
             all(-2.0 <= v <= 2.0 for v in vals),
             "400 次采样无越界")
        j = [jitter(10.0, 0.2) for _ in range(200)]
        _add("jitter 幅度在 ±20% 内",
             all(8.0 <= v <= 12.0 for v in j), "200 次采样全在界内")

        _add("randint 落在闭区间",
             all(5 <= randint(5, 9) <= 9 for _ in range(200)), "")

    finally:
        if saved is None:
            reset()
        else:
            seed(saved)

    return {"checks": checks, "failures": failures, "ok": not failures}
