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

    def test_blended_rank_keeps_semantics_primary(self):
        # 语义分 0.8 但情绪相反 vs 语义分 0.4 但情绪一致：语义仍是主项
        a, _ = sem.blended_rank(0.8, self.CHAT, 0.0, 0.0)
        b, _ = sem.blended_rank(0.4, self.CHAT, 0.8, 0.8)
        self.assertGreater(a, b)
        # 同等语义时，情绪相近的更高
        a2, _ = sem.blended_rank(0.5, self.CHAT, 0.0, 0.0)
        b2, _ = sem.blended_rank(0.5, self.CHAT, 0.8, 0.8)
        self.assertGreater(b2, a2)


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
