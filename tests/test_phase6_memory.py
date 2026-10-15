# -*- coding: utf-8 -*-
"""阶段六 · 任务记忆（``memory``）。

## 这个文件在守什么

"自动恢复"的前提是"记得住"。这里守四件事：

1. **JSON 而非 pickle**（硬约束 5）—— 存档可能被外部写入，
   反序列化 pickle 等于执行那个文件里的代码
2. **跨进程恢复真的能还原状态** —— 不是"存了就算"
3. **task_id 校验** —— 它会拼进文件路径，不校验就是任意文件写
4. **脏数据不抛** —— 存档可能被手改过、被老版本写过

## 隔离

所有测试用 ``tmp_path``，**绝不碰** ``~/.avatar/tasks/`` 的真实存档。
"""

from __future__ import annotations

import json
import os

import pytest

from avatar.agentic_planner.memory import (
    MAX_EVENTS,
    MemoryError_,
    ShortTermMemory,
    delete_task,
    list_tasks,
)


@pytest.fixture
def mem(tmp_path):
    return ShortTermMemory("t1", directory=str(tmp_path))


@pytest.fixture
def d(tmp_path):
    return str(tmp_path)


# ==========================================================================
# 1. task_id 校验（路径穿越）
# ==========================================================================
class TestTaskIdSafety:
    @pytest.mark.parametrize("bad", [
        "../etc/passwd", "a/b", "a\\b", "a b", "有中文",
        "a.json", "x" * 65, "a:b", "a*b", "a;b", "./a", "..",
    ])
    def test_illegal_rejected(self, bad):
        """★★ task_id 会拼进文件路径 —— 必须拦住路径穿越。"""
        with pytest.raises(MemoryError_):
            ShortTermMemory(bad)

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_id_allowed_but_unsavable(self, empty):
        """★ 空 task_id 是**合法**的（表示"还没绑定任务"）。

        执行器会先创建一个未绑定的 memory，`bind_plan` 时再补 id。
        但**不能保存** —— 没有 id 就没有文件名。
        """
        m = ShortTermMemory(empty)
        assert m.task_id == ""
        with pytest.raises(MemoryError_):
            m.save()

    @pytest.mark.parametrize("good", ["abc", "task-1", "T_2", "t" * 64])
    def test_legal_accepted(self, good):
        assert ShortTermMemory(good).task_id == good

    def test_load_rejects_bad_id(self, d):
        with pytest.raises(MemoryError_):
            ShortTermMemory.load("../x", directory=d)


# ==========================================================================
# 2. 记录
# ==========================================================================
class TestRecording:
    def test_step_state(self, mem):
        mem.set_step_state("s1", "running")
        assert mem.step_state("s1") == "running"
        mem.set_step_state("s2", "success")
        assert mem.step_state("s2") == "success"

    def test_transition_records_and_syncs(self, mem):
        mem.record_transition("s1", "pending", "running", "start")
        assert len(mem.events_of("transition")) == 1
        assert mem.step_state("s1") == "running"

    def test_task_transition(self, mem):
        mem.record_task_transition("pending", "running", "start")
        assert mem.task_state == "running"

    def test_error_records_type(self, mem):
        mem.record_error("s1", ValueError("炸了"))
        e = mem.errors()[0]
        assert e["error"]["type"] == "ValueError"
        assert "炸了" in e["error"]["message"]

    def test_decision_is_recorded(self, mem):
        """★ 硬约束 7：自愈决策必须记入 memory，禁止静默重试。"""
        mem.record_decision("s1", "retry", "第一次失败")
        decs = mem.decisions()
        assert len(decs) == 1
        assert decs[0]["strategy"] == "retry"
        assert decs[0]["reason"]

    def test_observation(self, mem):
        mem.record_observation("s1", "before", {"title": "记事本"})
        mem.record_observation("s1", "after", {"title": "记事本"})
        obs = mem.events_of("observation")
        assert len(obs) == 2
        assert {o["phase"] for o in obs} == {"before", "after"}

    def test_counters(self, mem):
        assert mem.bump_attempts("s1") == 1
        assert mem.bump_attempts("s1") == 2
        assert mem.attempts_of("s1") == 2
        assert mem.bump_switches("s1") == 1

    def test_notes(self, mem):
        mem.note("人工备注")
        assert "人工备注" in mem.notes


# ==========================================================================
# 3. 事件上限
# ==========================================================================
class TestEventCap:
    def test_capped(self, mem):
        for i in range(MAX_EVENTS + 200):
            mem._emit("t", i=i)
        assert len(mem.events) == MAX_EVENTS

    def test_keeps_newest(self, mem):
        """★ 截断丢的是**最老的** —— 最新的排查线索要留着。"""
        for i in range(MAX_EVENTS + 10):
            mem._emit("t", i=i)
        assert mem.events[-1]["i"] == MAX_EVENTS + 9


# ==========================================================================
# 4. 序列化（硬约束 5）
# ==========================================================================
class TestSerialization:
    def test_json_safe(self, mem):
        mem.record_error("s1", ValueError("x"))
        mem.record_observation("s1", "before", {"a": [1, 2]})
        blob = json.dumps(mem.to_dict(), ensure_ascii=False)
        assert blob

    def test_no_pickle_import(self):
        """★★ 硬约束 5：禁用 pickle。"""
        import ast
        import avatar.agentic_planner.memory as M
        tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add((a.name or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert "pickle" not in mods
        assert "shelve" not in mods      # shelve 内部用 pickle

    def test_roundtrip(self, mem):
        mem.set_step_state("s1", "success")
        mem.record_decision("s1", "retry", "r")
        mem.bump_attempts("s1")
        back = ShortTermMemory.from_json(mem.to_json())
        assert back.task_id == mem.task_id
        assert back.step_state("s1") == "success"
        assert back.attempts_of("s1") == 1
        assert len(back.decisions()) == 1

    @pytest.mark.parametrize("garbage", [
        {}, None, {"steps": "不是字典"}, {"events": "不是列表"},
        {"notes": 123}, {"created_at": "abc"}, {"task_id": 12345},
        {"goal": "不是字典"}, [],
    ])
    def test_garbage_tolerated(self, garbage):
        """★ 脏数据不抛 —— 存档可能被手改过、被老版本写过。"""
        assert ShortTermMemory.from_dict(garbage) is not None


# ==========================================================================
# 5. 落盘与恢复
# ==========================================================================
class TestPersistence:
    def test_save_creates_json(self, mem):
        path = mem.save()
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            assert json.load(f)["task_id"] == "t1"

    def test_atomic_write_no_tmp_left(self, mem):
        """★ 原子写：先写 .tmp 再 replace，不留残渣。"""
        path = mem.save()
        assert not os.path.isfile(path + ".tmp")

    def test_cross_process_restore(self, mem, d):
        """★★ 跨进程恢复：load 出来的状态要完整。"""
        mem.set_step_state("s1", "success")
        mem.record_transition("s2", "pending", "running", "start")
        mem.record_decision("s2", "retry_with_fallback", "换条路")
        mem.record_error("s2", OSError("断了"))
        mem.bump_attempts("s2")
        mem.save()

        back = ShortTermMemory.load("t1", directory=d)
        assert back.step_state("s1") == "success"
        assert back.step_state("s2") == "running"
        assert back.attempts_of("s2") == 1
        assert len(back.decisions()) == 1        # 决策可复盘
        assert len(back.errors()) == 1

    def test_load_missing_raises(self, d):
        with pytest.raises(MemoryError_):
            ShortTermMemory.load("nope", directory=d)

    def test_load_corrupt_raises(self, d):
        """★ 损坏的存档要明确报错，不能返回半个对象。"""
        with open(os.path.join(d, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{不是合法 json")
        with pytest.raises(MemoryError_):
            ShortTermMemory.load("broken", directory=d)

    def test_save_without_task_id_raises(self, d):
        with pytest.raises(MemoryError_):
            ShortTermMemory("", directory=d).save()

    def test_plan_snapshot_restorable(self, mem):
        """★ 断点恢复的核心：计划快照能还原成 Plan。"""
        from avatar.agentic_planner import parse_goal
        from avatar.agentic_planner.plan import Plan
        from avatar.agentic_planner.planner import RuleBasedPlanner

        plan = RuleBasedPlanner().plan(parse_goal("打开记事本输入hello"))
        mem.bind_plan(plan)
        assert len(mem.steps) == len(plan.steps)
        back = Plan.from_dict(mem.plan)
        assert back.plan_id == plan.plan_id
        assert back.validate() == []


# ==========================================================================
# 6. 列表与删除
# ==========================================================================
class TestListAndDelete:
    def test_list(self, mem, d):
        mem.save()
        ShortTermMemory("t2", directory=d).save()
        ids = {t["task_id"] for t in list_tasks(directory=d)}
        assert {"t1", "t2"} <= ids

    def test_list_marks_broken(self, d):
        with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{坏的")
        tasks = list_tasks(directory=d)
        assert any(t.get("broken") for t in tasks), \
            "坏存档也要列出来 —— 不然用户不知道它坏了"

    def test_list_empty_dir(self, tmp_path):
        assert list_tasks(directory=str(tmp_path / "nope")) == []

    def test_delete(self, mem, d):
        mem.save()
        assert delete_task("t1", directory=d) is True
        assert delete_task("t1", directory=d) is False   # 第二次没有了
        assert delete_task("../x", directory=d) is False  # 非法 id 不抛


# ==========================================================================
# 7. 摘要
# ==========================================================================
class TestSummary:
    def test_summary_contains_key_info(self, mem):
        mem.set_step_state("s1", "failed")
        mem.record_decision("s1", "retry", "第一次")
        mem.record_error("s1", ValueError("炸了"))
        text = mem.summary()
        assert "t1" in text
        assert "s1" in text
        assert "自愈决策" in text, "复盘要看决策"

    def test_summary_never_raises(self):
        assert isinstance(ShortTermMemory("empty").summary(), str)
