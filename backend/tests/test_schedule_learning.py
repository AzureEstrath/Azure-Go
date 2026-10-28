"""日程行为学习：任务分型 / EWMA / 变更点 / 偏好分 / 复盘（纯函数门卫）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import schedule_learning


NOW = 1_760_000_000.0


class InferTypeTest(unittest.TestCase):
    def test_course_is_always_course(self):
        self.assertEqual(schedule_learning.infer_task_type("随便", kind="course"), "course")

    def test_keywords_map_to_categories(self):
        cases = [
            ("组会讨论", "meeting"),
            ("跑步 5 公里", "exercise"),
            ("去超市买牛奶", "chore"),
            ("同学聚餐", "social"),
            ("复习第三章", "study"),
            ("写项目文档", "work"),
            ("随便看看", "general"),
        ]
        for title, want in cases:
            self.assertEqual(schedule_learning.infer_task_type(title), want, title)

    def test_notes_are_also_searched(self):
        self.assertEqual(
            schedule_learning.infer_task_type("下午", notes="和导师开会"), "meeting")


class UpdateStatsTest(unittest.TestCase):
    def test_first_completion_blends_prior(self):
        stats = schedule_learning.update_stats({}, "study", actual_min=30,
                                               planned_min=60, now=NOW)
        e = stats["study"]
        self.assertEqual(e["samples"], 1)
        self.assertEqual(e["done_count"], 1)
        self.assertAlmostEqual(e["dur_mean"], 50.0)      # (60*2 + 30) / 3
        self.assertAlmostEqual(e["updated_ts"], NOW)

    def test_ewma_moves_toward_new_observation(self):
        stats = {"study": {"task_type": "study", "dur_mean": 50.0, "dur_var": 100.0,
                           "samples": 1, "done_count": 1}}
        stats = schedule_learning.update_stats(stats, "study", actual_min=110,
                                               now=NOW + 1)
        self.assertAlmostEqual(stats["study"]["dur_mean"], 71.0)   # 50 + 0.35*60
        self.assertEqual(stats["study"]["samples"], 2)

    def test_deferred_only_increments_counter(self):
        stats = schedule_learning.update_stats({}, "work", actual_min=0,
                                               outcome="deferred", now=NOW)
        e = stats["work"]
        self.assertEqual(e["defer_count"], 1)
        self.assertEqual(e["samples"], 0)
        self.assertEqual(e["done_count"], 0)

    def test_skipped_does_not_learn_duration(self):
        stats = schedule_learning.update_stats({}, "work", actual_min=999,
                                               outcome="skipped", now=NOW)
        self.assertEqual(stats["work"]["samples"], 0)
        self.assertEqual(stats["work"]["done_count"], 0)

    def test_two_large_deviations_trigger_change_point(self):
        base = {"study": {"task_type": "study", "dur_mean": 60.0, "dur_var": 4.0,
                          "samples": 3, "done_count": 3}}
        once = schedule_learning.update_stats(base, "study", actual_min=100,
                                              now=NOW, alpha=0.05)
        self.assertEqual(once["study"]["dev_streak"], 1)
        twice = schedule_learning.update_stats(once, "study", actual_min=100,
                                               now=NOW + 1, alpha=0.05)
        e = twice["study"]
        self.assertEqual(e["dev_streak"], 0)
        self.assertAlmostEqual(e["dur_mean"], 100.0)
        self.assertAlmostEqual(e["change_point_ts"], NOW + 1)

    def test_normal_deviation_resets_streak(self):
        base = {"study": {"task_type": "study", "dur_mean": 60.0, "dur_var": 100.0,
                          "samples": 3, "done_count": 3, "dev_streak": 1}}
        out = schedule_learning.update_stats(base, "study", actual_min=61, now=NOW)
        self.assertEqual(out["study"]["dev_streak"], 0)


class PreferenceTest(unittest.TestCase):
    def test_bonus_is_bounded_and_needs_samples(self):
        self.assertEqual(schedule_learning.preference_bonus({"done_count": 1,
                                                             "defer_count": 0}), 0)
        self.assertGreater(schedule_learning.preference_bonus(
            {"done_count": 4, "defer_count": 0}), 0)
        self.assertLess(schedule_learning.preference_bonus(
            {"done_count": 0, "defer_count": 4}), 0)

    def test_plan_targets_exposes_mean_and_bonus(self):
        targets = schedule_learning.plan_targets({
            "chore": {"dur_mean": 25.0, "dur_var": 64.0, "samples": 4,
                      "done_count": 4, "defer_count": 0}})
        self.assertAlmostEqual(targets["chore"]["mean"], 25.0)
        self.assertEqual(targets["chore"]["samples"], 4)
        self.assertGreaterEqual(targets["chore"]["bonus"], 0)


class ReviewTest(unittest.TestCase):
    def test_review_reports_bias_and_counts(self):
        history = [
            {"task_type": "study", "status": "done", "outcome": "done",
             "duration_min": 60, "actual_min": 90, "updated_ts": NOW},
            {"task_type": "study", "status": "done", "outcome": "done",
             "duration_min": 60, "actual_min": 90, "updated_ts": NOW},
            {"task_type": "chore", "status": "planned", "outcome": "deferred",
             "defer_count": 1, "updated_ts": NOW},
        ]
        stats = {"study": {"dur_mean": 85.0, "dur_var": 100.0, "samples": 2,
                           "done_count": 2}}
        out = schedule_learning.review(history, stats, now=NOW)
        rows = {r["task_type"]: r for r in out["rows"]}
        self.assertAlmostEqual(rows["study"]["planned_mean"], 60.0)
        self.assertAlmostEqual(rows["study"]["actual_mean"], 90.0)
        self.assertAlmostEqual(rows["study"]["bias_pct"], 50.0)
        self.assertEqual(rows["chore"]["deferred"], 1)
        self.assertEqual(out["deferred"], 1)
        self.assertIn("学习", out["summary"])

    def test_empty_review_is_readable(self):
        out = schedule_learning.review([], {}, now=NOW)
        self.assertEqual(out["rows"], [])
        self.assertIn("还没有", out["summary"])


if __name__ == "__main__":
    unittest.main()
