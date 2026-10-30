"""记忆情绪语义（v4.3.0，A/F）：二维矩阵、标签、召回偏置、变更线索。纯函数，不碰库。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import memory_semantics as sem


class LabelTest(unittest.TestCase):
    def test_3x3_labels(self):
        self.assertEqual(sem.label_of(0.2, 0.2), "低落")
        self.assertEqual(sem.label_of(0.2, 0.5), "不安")
        self.assertEqual(sem.label_of(0.2, 0.9), "焦躁")
        self.assertEqual(sem.label_of(0.5, 0.2), "淡然")
        self.assertEqual(sem.label_of(0.5, 0.5), "平静")
        self.assertEqual(sem.label_of(0.5, 0.9), "振奋")
        self.assertEqual(sem.label_of(0.9, 0.2), "惬意")
        self.assertEqual(sem.label_of(0.9, 0.5), "开心")
        self.assertEqual(sem.label_of(0.9, 0.9), "兴奋")

    def test_invalid_means_empty(self):
        self.assertEqual(sem.label_of(None, 0.5), "")
        self.assertEqual(sem.label_of(0.5, float("nan")), "")
        self.assertEqual(sem.band(-0.1), -1)


class EmotionOfMoodTest(unittest.TestCase):
    def test_three_dim_to_two_dim(self):
        got = sem.emotion_of_mood({"v": 0.8, "c": 0.9, "a": 0.6})
        self.assertEqual(got["tag"], "开心")
        self.assertAlmostEqual(got["v"], 0.8)
        self.assertAlmostEqual(got["a"], 0.6)

    def test_json_and_missing_dim(self):
        self.assertEqual(sem.emotion_of_mood('{"v":0.2,"a":0.8}')["tag"], "焦躁")
        self.assertIsNone(sem.emotion_of_mood({"v": 0.5}))
        self.assertIsNone(sem.emotion_of_mood("bad json"))
        self.assertIsNone(sem.emotion_of_mood(None))

    def test_dirty_values(self):
        self.assertIsNone(sem.emotion_of_mood({"v": "abc", "a": 0.5}))
        self.assertIsNone(sem.emotion_of_mood({"v": 2.0, "a": 0.5}))


class AffinityTest(unittest.TestCase):
    CHAT = {"v": 0.8, "a": 0.8}

    def test_same_is_one_opposite_is_zero(self):
        self.assertAlmostEqual(sem.emotion_affinity(self.CHAT, 0.8, 0.8), 1.0)
        self.assertAlmostEqual(sem.emotion_affinity({"v": 1.0, "a": 1.0}, 0.0, 0.0), 0.0)

    def test_unknown_returns_none(self):
        self.assertIsNone(sem.emotion_affinity(self.CHAT, -1.0, 0.5))
        self.assertIsNone(sem.emotion_affinity({"v": 0.5}, 0.5, 0.5))

    def test_dynamic_weight_grows_away_from_baseline(self):
        baseline = {"v": 0.58, "a": 0.30}
        calm = sem.state_emotion_weight(baseline)
        low = sem.state_emotion_weight({"v": 0.20, "a": 0.10})
        strong = sem.state_emotion_weight({"v": 0.95, "a": 0.85})
        self.assertAlmostEqual(calm, sem.EMOTION_RANK_BASE)
        self.assertGreater(low, calm)
        self.assertGreater(strong, low)
        self.assertLessEqual(strong, sem.EMOTION_RANK_MAX)

    def test_blended_rank_keeps_semantics_primary(self):
        # 语义分 0.8 但情绪相反 vs 语义分 0.4 但情绪一致：加性偏置仍让语义主导
        a, _ = sem.blended_rank(0.8, self.CHAT, 0.0, 0.0)
        b, _ = sem.blended_rank(0.4, self.CHAT, 0.8, 0.8)
        self.assertGreater(a, b)
        # 同等语义时，情绪相近的更高
        a2, _ = sem.blended_rank(0.5, self.CHAT, 0.0, 0.0)
        b2, _ = sem.blended_rank(0.5, self.CHAT, 0.8, 0.8)
        self.assertGreater(b2, a2)


class SomaticTest(unittest.TestCase):
    """v4.4.0：编码时的情感电荷与低巩固分保留惩罚。"""

    def test_charge_is_absolute_and_unknown_is_zero(self):
        calm = sem.somatic_charge(0.5, 0.5)
        strong = sem.somatic_charge(0.1, 0.9)
        self.assertAlmostEqual(calm, 0.0)
        self.assertGreater(strong, 0.5)
        self.assertEqual(sem.somatic_charge(-1.0, 0.5), 0.0)
        self.assertEqual(sem.somatic_charge(None, 0.5), 0.0)

    def test_charge_can_raise_rank_without_current_mood(self):
        low, _ = sem.blended_rank(0.5, None, 0.5, 0.5)
        high, _ = sem.blended_rank(0.5, None, 0.1, 0.9)
        self.assertGreater(high, low)

    def test_charge_does_not_override_a_large_semantic_gap(self):
        semantic, _ = sem.blended_rank(0.8, None, 0.5, 0.5)
        charged, _ = sem.blended_rank(0.4, None, 0.1, 0.9)
        self.assertGreater(semantic, charged)

    def test_retention_penalty_only_for_low_salience(self):
        self.assertLess(sem.retention_penalty({"salience": 0.2}), 0.0)
        self.assertEqual(sem.retention_penalty({"salience": 0.8}), 0.0)
        self.assertEqual(sem.retention_penalty({}), 0.0)
        self.assertEqual(sem.retention_penalty(None), 0.0)


class ChangeCueTest(unittest.TestCase):
    def test_change_words(self):
        self.assertTrue(sem.has_change_cue("我不吃了，已经退掉了"))
        self.assertTrue(sem.has_change_cue("课表改到周三了"))
        self.assertTrue(sem.has_change_cue("我搬家了"))
        self.assertFalse(sem.has_change_cue("今天天气不错"))

    def test_day_key(self):
        import datetime
        ts = datetime.datetime(2026, 10, 30, 8, 0).timestamp()
        self.assertEqual(sem.day_key(ts), "2026-10-30")
        self.assertEqual(sem.day_key("bad"), "")


if __name__ == "__main__":
    unittest.main()
