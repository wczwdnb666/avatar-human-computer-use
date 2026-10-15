# -*- coding: utf-8 -*-
"""阶段八 · 成本控制（``vision/cost``）。

## 这个文件在守什么

1. **超预算抛 CostExceeded**（硬约束 6）—— 不静默调用
2. **缓存键 = 截图 hash + 目标**（硬约束 7）—— 两个都要
3. **缓存默认开启**
"""
from __future__ import annotations

import json
import threading

import pytest

from avatar.vision.cost import (
    DEFAULT_CACHE_SIZE,
    DEFAULT_PER_REQUEST_TOKENS,
    DEFAULT_TOTAL_TOKENS,
    CostBudget,
    CostExceeded,
    CostRecord,
    ResultCache,
    estimate_image_tokens,
    image_hash,
)


class TestContract:
    def test_docstring_has_boundary(self):
        import avatar.vision.cost as M
        assert "能力边界" in (M.__doc__ or "")

    def test_defaults(self):
        assert DEFAULT_PER_REQUEST_TOKENS == 2000
        assert DEFAULT_TOTAL_TOKENS == 20000
        assert DEFAULT_CACHE_SIZE > 0

    def test_cache_on_by_default(self):
        """★ 缓存**默认开启**（硬约束 7）。"""
        assert ResultCache().disabled is False


class TestTokenEstimate:
    def test_1280x720(self):
        assert 1200 < estimate_image_tokens(1280, 720) < 1300

    def test_zero(self):
        assert estimate_image_tokens(0, 0) == 0
        assert estimate_image_tokens(-1, -1) == 0

    def test_matches_image_prep(self):
        """★★ 两处公式不能分叉。"""
        from avatar.vision.image_prep import (
            estimate_image_tokens as other)
        for w, h in ((100, 100), (1280, 720), (0, 0), (5, 3)):
            assert estimate_image_tokens(w, h) == other(w, h), (w, h)


class TestImageHash:
    def test_stable(self):
        assert image_hash(b"abc") == image_hash(b"abc")

    def test_different(self):
        assert image_hash(b"abc") != image_hash(b"abd")

    def test_length(self):
        assert len(image_hash(b"x")) == 16

    def test_hex(self):
        assert all(c in "0123456789abcdef" for c in image_hash(b"x"))

    def test_none(self):
        assert image_hash(None) == ""

    def test_never_raises(self):
        for v in (None, b"", "x", 123, [], {}):
            assert isinstance(image_hash(v), str)


class TestBudget:
    def test_per_request_limit(self):
        b = CostBudget(per_request=100, total=1000)
        assert b.check(100) is True
        assert b.check(101) is False

    def test_raises_cost_exceeded(self):
        """★★ 超预算**抛异常**（硬约束 6）。"""
        b = CostBudget(per_request=100, total=1000)
        with pytest.raises(CostExceeded):
            b.require(101)

    def test_exception_details(self):
        b = CostBudget(per_request=100, total=1000)
        with pytest.raises(CostExceeded) as ei:
            b.require(101)
        assert ei.value.kind == "per_request"
        assert ei.value.limit == 100
        assert ei.value.estimated == 101

    def test_total_limit(self):
        """★ 累计分支要绕过单次检查才能测到。"""
        b = CostBudget(per_request=100, total=150)
        b.consume(100)
        with pytest.raises(CostExceeded) as ei:
            b.require(60)
        assert ei.value.kind == "total"

    def test_consume_and_remaining(self):
        b = CostBudget(total=1000)
        b.consume(300)
        assert b.used == 300 and b.remaining == 700

    def test_rejected_counter(self):
        b = CostBudget(per_request=10, total=10)
        b.check(999)
        b.check(999)
        assert b.rejected == 2

    def test_reset(self):
        b = CostBudget()
        b.consume(100)
        b.reset()
        assert b.used == 0 and b.requests == 0 and not b.records

    def test_cache_hit_no_cost(self):
        """★ 缓存命中不消耗预算。"""
        b = CostBudget()
        before = b.used
        b.note_cache_hit(1200)
        assert b.used == before and b.cache_hits == 1

    def test_thread_safety(self):
        """★ 并发记账不丢。"""
        b = CostBudget(per_request=10 ** 6, total=10 ** 9)
        def _work():
            for _ in range(100):
                b.consume(1)
        ts = [threading.Thread(target=_work) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert b.used == 400, b.used


class TestRecord:
    def test_fields(self):
        r = CostRecord(provider="openai", model="m", prompt_tokens=100,
                       completion_tokens=20, elapsed=1.5, ok=True)
        assert r.total_tokens == 120
        assert r.provider == "openai" and r.at > 0

    def test_cost_default_zero(self):
        """★ 本模块**不猜价格** —— cost 默认 0。"""
        assert CostRecord().cost == 0.0

    def test_json_safe(self):
        assert json.dumps(CostRecord().to_dict(), ensure_ascii=False)

    def test_describe(self):
        assert "tokens" in CostRecord().describe()
        assert "缓存" in CostRecord(cached=True).describe()


class TestCache:
    def test_miss_then_hit(self):
        c = ResultCache()
        h = image_hash(b"shot")
        assert c.get(h, "确定") is None
        c.put(h, "确定", {"x": 1})
        assert c.get(h, "确定") == {"x": 1}

    def test_different_goal_misses(self):
        """★★ 换目标不能命中 —— 否则返回无关结果。"""
        c = ResultCache()
        h = image_hash(b"shot")
        c.put(h, "确定", 1)
        assert c.get(h, "取消") is None

    def test_different_image_misses(self):
        """★★ 换截图不能命中 —— 界面变了不能复用旧位置。"""
        c = ResultCache()
        c.put(image_hash(b"s1"), "确定", 1)
        assert c.get(image_hash(b"s2"), "确定") is None

    def test_goal_normalized(self):
        c = ResultCache()
        h = image_hash(b"s")
        c.put(h, "确定  按钮", 1)
        assert c.get(h, " 确定 按钮 ") == 1

    def test_capacity_evicts_oldest(self):
        c = ResultCache(max_size=2)
        c.put(image_hash(b"a"), "g", 1)
        c.put(image_hash(b"b"), "g", 2)
        c.put(image_hash(b"c"), "g", 3)
        assert len(c) == 2 and c.get(image_hash(b"a"), "g") is None
        assert c.get(image_hash(b"c"), "g") == 3

    def test_disabled(self):
        c = ResultCache(disabled=True)
        c.put(image_hash(b"x"), "g", 1)
        assert c.get(image_hash(b"x"), "g") is None and len(c) == 0

    def test_none_not_cached(self):
        """★ 不能缓存「没结果」。"""
        c = ResultCache()
        c.put(image_hash(b"n"), "g", None)
        assert len(c) == 0

    def test_hit_rate(self):
        c = ResultCache()
        c.put(image_hash(b"y"), "g", 1)
        c.get(image_hash(b"y"), "g")
        c.get(image_hash(b"z"), "g")
        assert abs(c.hit_rate - 0.5) < 1e-6

    def test_clear(self):
        c = ResultCache()
        c.put(image_hash(b"a"), "g", 1)
        assert c.clear() == 1 and len(c) == 0

    def test_make_key_stable(self):
        assert ResultCache.make_key("h", "确定") == \
            ResultCache.make_key("h", " 确定 ")

    def test_make_key_has_hash(self):
        assert "abc" in ResultCache.make_key("abc", "g")


class TestNoPricing:
    def test_no_pricing_functions(self):
        """★ 不猜价格 —— 那会过期。"""
        import ast
        import avatar.vision.cost as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert not (names & {"price", "pricing", "calc_cost", "cost_of"})

    def test_no_persistence(self):
        """★ 不做持久化缓存 —— 会带来失效策略/并发写一堆问题。"""
        import ast
        import avatar.vision.cost as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert not (names & {"save_cache", "load_cache", "dump_cache"})

    def test_no_network(self):
        import ast
        import avatar.vision.cost as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not (mods & {"urllib", "requests", "PIL"}), mods
        assert "hashlib" in mods
