"""FP-Growth 关联挖掘 / 记忆关联 / 日程类别共现（v4.7.0）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import association, prompts
from app.config import AzureConfig
from app.life import LifeStore
from app.memory import MemoryStore
from app.memory_service import MemoryService


class FpGrowthTest(unittest.TestCase):
    def test_frequent_itemset_with_support(self):
        tx = [
            ["苹果", "米饭", "辣椒"],
            ["苹果", "米饭"],
            ["苹果", "米饭"],
            ["米饭", "辣椒"],
            ["苹果", "米饭", "辣椒"],
            ["苹果"],
        ]
        got = association.mine(tx, min_support=3, max_len=3)
        rows = {frozenset(x["items"]): x["support"] for x in got}
        self.assertEqual(rows.get(frozenset({"苹果", "米饭"})), 4)
        self.assertNotIn(frozenset({"苹果", "辣椒"}), rows)   # 只有 2 次，低于支持度
        self.assertEqual(len(rows), 2)             # 苹果-米饭、米饭-辣椒

    def test_patterns_confidence_and_lift(self):
        tx = [
            ["study", "exercise"],
            ["study", "exercise"],
            ["study", "exercise"],
            ["study", "exercise"],
            ["study"],
            ["exercise"],
            ["work", "read"],
            ["work", "read"],
        ]
        got = association.patterns(tx, min_support=3, min_confidence=0.6)
        self.assertTrue(got)
        row = got[0]
        self.assertEqual(set(row["items"]), {"study", "exercise"})
        self.assertEqual(row["support"], 4)
        self.assertGreater(row["confidence"], 0.6)
        self.assertGreater(row["lift"], 1.0)

    def test_single_occurrence_never_becomes_pattern(self):
        tx = [["苹果", "米饭", "辣椒"]]
        self.assertEqual(association.mine(tx, min_support=3), [])
        self.assertEqual(association.patterns(tx, min_support=3), [])

    def test_tokens_from_text(self):
        self.assertEqual(set(association.tokens_from_text("午餐吃了苹果，米饭和辣椒")),
                         {"苹果", "米饭", "辣椒", "午餐"})
        self.assertEqual(association.tokens_from_text("今天天气不错"), [])

    def test_category_bonus_is_small(self):
        rows = [{"items": ["study", "exercise"], "confidence": 0.8}]
        bonus = association.category_bonus(rows, gain=1.2)
        self.assertAlmostEqual(bonus["study"], 0.96)
        self.assertLessEqual(bonus["study"], 1.5)


class MemoryAssociationTest(unittest.TestCase):
    def test_memory_observations_build_patterns_and_note(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(os.path.join(d, "m.db"), embed_fn=None)
            for _ in range(4):
                store.observe_association_turn(["苹果", "米饭"])
            store.observe_association_turn(["苹果", "辣椒"])
            store.observe_association_turn(["米饭", "辣椒"])
            store.observe_association_turn(["工作", "阅读"])
            store.observe_association_turn(["工作", "阅读"])
            self.assertEqual(store.refresh_associations(min_support=3), 1)
            rows = store.association_patterns()
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0]["items"]), {"苹果", "米饭"})
            svc = MemoryService(AzureConfig())   # 只是为了走同一套 note 渲染
            svc.store = store
            note = svc.association_note("今天午餐吃苹果")
            self.assertIn("苹果", note)
            self.assertIn("米饭", note)
            self.assertEqual(svc.association_note("今天天气不错"), "")
            store.close()

    def test_memory_note_absent_when_disabled(self):
        svc = MemoryService(AzureConfig())
        self.assertEqual(svc.association_note("苹果"), "")
        self.assertEqual(svc.observe_associations("苹果和米饭"), 0)


class PromptAssociationTest(unittest.TestCase):
    def test_empty_association_keeps_block_identical(self):
        base = prompts.life_context_block([], schedule_text="明天有课")
        same = prompts.life_context_block([], schedule_text="明天有课", association_text="")
        self.assertEqual(base, same)
        self.assertNotIn("常一起提到", base)

    def test_association_block_carries_the_caveat(self):
        out = prompts.life_context_block([], association_text="常一起提到：苹果、米饭")
        self.assertIn("常一起提到", out)
        self.assertIn("不是事实", out)
        self.assertIn("别当成对方这次的选择", out)


class ScheduleAssociationTest(unittest.TestCase):
    def test_schedule_categories_mined_from_same_day(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            now = 1000000000.0
            # 4 天 study+exercise，另外各 1 天单独出现 → 支持 4 / 置信度 > 0.6 / lift > 1。
            for i in range(4):
                base = now + 86400 * (i + 1)
                store.upsert_schedule([
                    {"title": "学习", "start_ts": base, "duration_min": 60, "task_type": "study"},
                    {"title": "跑步", "start_ts": base + 7200, "duration_min": 30,
                     "task_type": "exercise"},
                ], "t", now=now)
            store.upsert_schedule([
                {"title": "学习", "start_ts": now + 86400 * 6, "duration_min": 60,
                 "task_type": "study"}], "t", now=now)
            store.upsert_schedule([
                {"title": "跑步", "start_ts": now + 86400 * 7, "duration_min": 30,
                 "task_type": "exercise"}], "t", now=now)
            for i in range(2):
                base = now + 86400 * (8 + i)
                store.upsert_schedule([
                    {"title": "工作", "start_ts": base, "duration_min": 60,
                     "task_type": "work"},
                    {"title": "社交", "start_ts": base + 3600, "duration_min": 30,
                     "task_type": "social"},
                ], "t", now=now)
            self.assertEqual(store.refresh_schedule_associations(now=now), 1)
            rows = store.load_schedule_associations()
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0]["items"]), {"study", "exercise"})
            self.assertGreater(rows[0]["confidence"], 0.6)
            store.close()


if __name__ == "__main__":
    unittest.main()
