"""自动排班：James-Stein 收缩 + 优先级密度 + 空闲区间打包（纯逻辑门卫）。"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auto_plan


def _ts(y, m, d, hh, mm=0):
    return datetime.datetime(y, m, d, hh, mm).timestamp()


NOW = _ts(2026, 10, 28, 8, 0)


class JamesSteinTest(unittest.TestCase):
    def test_too_few_values_are_untouched(self):
        self.assertEqual(auto_plan.james_stein_shrink([30.0], 60.0, 2025.0), [30.0])
        self.assertEqual(auto_plan.james_stein_shrink([30.0, 90.0], 60.0, 2025.0), [30.0, 90.0])

    def test_no_variance_keeps_values(self):
        self.assertEqual(auto_plan.james_stein_shrink([60.0, 60.0, 60.0], 60.0, 2025.0),
                         [60.0, 60.0, 60.0])

    def test_values_shrink_toward_the_mean(self):
        raw = [30.0, 60.0, 60.0, 90.0]
        got = auto_plan.james_stein_shrink(raw, 60.0, 2025.0)
        self.assertEqual(len(got), 4)
        self.assertTrue(all(55.0 <= x <= 65.0 for x in got), got)
        self.assertLess(abs(got[0] - 60.0), abs(raw[0] - 60.0))
        self.assertLess(abs(got[-1] - 60.0), abs(raw[-1] - 60.0))

    def test_floor_keeps_individual_signal(self):
        raw = [0.0, 0.0, 100.0]
        got = auto_plan.james_stein_shrink(raw, 50.0, 1e9)
        # prior_var 极大 ⇒ 因子被下限 0.15 截住，不会把 100 全部拉成均值 33.3
        self.assertGreater(got[-1], 33.4)


class PlanTest(unittest.TestCase):
    def _item(self, i, title, dur=60, pri=50, start=0.0, deadline=0.0, **kw):
        row = {"id": i, "title": title, "duration_min": dur, "priority": pri,
               "start_ts": start, "deadline_ts": deadline, "kind": "task",
               "status": "planned", "archived": False, "auto_planned": False}
        row.update(kw)
        return row

    def test_high_priority_task_gets_the_early_slot(self):
        course = self._item(9, "课程", 120, 50, start=_ts(2026, 10, 28, 9, 0),
                            kind="course", period="1-2", weekday=2)
        a = self._item(1, "重要任务", 60, 90)
        b = self._item(2, "普通任务", 60, 20)
        out = auto_plan.plan([course, a, b], now=NOW, days=2)
        by = {x["title"]: x for x in out["assignments"]}
        self.assertEqual(set(by), {"重要任务", "普通任务"})
        self.assertEqual(datetime.datetime.fromtimestamp(by["重要任务"]["start_ts"]).hour, 8)
        self.assertEqual(datetime.datetime.fromtimestamp(by["普通任务"]["start_ts"]).hour, 11)
        self.assertEqual(out["unscheduled"], [])

    def test_density_orders_short_high_priority_before_long(self):
        a = self._item(1, "长任务", 120, 100)
        b = self._item(2, "短任务", 30, 50)
        out = auto_plan.plan([a, b], now=NOW, days=1)
        first = sorted(out["assignments"], key=lambda x: x["start_ts"])[0]
        self.assertEqual(first["title"], "短任务")

    def test_deadline_task_is_placed_before_deadline(self):
        fixed = self._item(9, "整天占用", 480, 50, start=_ts(2026, 10, 28, 9, 0))
        urgent = self._item(1, "截止任务", 60, 60, deadline=_ts(2026, 10, 28, 18, 0))
        other = self._item(2, "普通任务", 60, 30)
        out = auto_plan.plan([fixed, urgent, other], now=NOW, days=1)
        by = {x["title"]: x for x in out["assignments"]}
        self.assertFalse(by["截止任务"]["late"])
        self.assertLessEqual(by["截止任务"]["start_ts"] + 60 * 60,
                             _ts(2026, 10, 28, 18, 0))

    def test_no_room_is_reported_unscheduled(self):
        fixed = self._item(9, "全天占用", 840, 50, start=_ts(2026, 10, 28, 8, 0))
        a = self._item(1, "放不下", 60, 50)
        out = auto_plan.plan([fixed, a], now=NOW, days=1)
        self.assertEqual(out["assignments"], [])
        self.assertEqual(out["unscheduled"][0]["id"], 1)

    def test_empty_and_archived_items_are_ignored(self):
        self.assertEqual(auto_plan.plan([], now=NOW)["assignments"], [])
        archived = self._item(2, "归档任务", 60, 90, archived=True)
        self.assertEqual(auto_plan.plan([archived], now=NOW)["assignments"], [])


if __name__ == "__main__":
    unittest.main()
