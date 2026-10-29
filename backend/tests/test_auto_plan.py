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

    def test_deferred_twice_prefers_large_block(self):
        """连续推迟 ≥2 次：宁可放到后一天的大段空闲，也不再塞进当天碎片。"""
        busy_day1 = self._item(9, "周四占用", 750, 50, start=_ts(2026, 10, 29, 8, 0))
        once = self._item(1, "推迟一次", 60, 50, defer_count=1)
        twice = self._item(2, "连续推迟", 60, 50, defer_count=2)
        out = auto_plan.plan([busy_day1, once, twice], now=NOW, days=3)
        by = {x["title"]: x for x in out["assignments"]}
        once_day = datetime.datetime.fromtimestamp(by["推迟一次"]["start_ts"]).date()
        twice_day = datetime.datetime.fromtimestamp(by["连续推迟"]["start_ts"]).date()
        self.assertEqual(once_day, datetime.datetime(2026, 10, 29).date())
        self.assertEqual(twice_day, datetime.datetime(2026, 10, 30).date())
        self.assertIn("连续推迟", by["连续推迟"]["reason"])
        self.assertIn("大段空闲", by["连续推迟"]["reason"])

    def test_assignment_reason_mentions_deadline_and_buffer(self):
        task = self._item(1, "报告", 60, 80, deadline=_ts(2026, 10, 28, 18, 0))
        out = auto_plan.plan([task], now=NOW, days=1,
                             category_stats={"study": {"mean": 60, "var": 900,
                                                       "samples": 5, "bonus": 0}},
                             risk_z=0.84)
        got = out["assignments"][0]
        self.assertIn("截止", got["reason"])
        self.assertIn("缓冲", got["reason"])

    def test_kept_slot_says_so(self):
        task = self._item(1, "保持原位", 60, 50, start=NOW + 2 * 3600.0,
                          auto_planned=True)
        out = auto_plan.plan([task], now=NOW, days=1)
        self.assertEqual(out["assignments"][0]["reason"], "保持原定时间")

    def test_deadline_task_is_placed_before_deadline(self):
        fixed = self._item(9, "整天占用", 480, 50, start=_ts(2026, 10, 28, 9, 0))
        urgent = self._item(1, "截止任务", 60, 60, deadline=_ts(2026, 10, 28, 18, 0))
        other = self._item(2, "普通任务", 60, 30)
        out = auto_plan.plan([fixed, urgent, other], now=NOW, days=1)
        by = {x["title"]: x for x in out["assignments"]}
        self.assertFalse(by["截止任务"]["late"])
        self.assertLessEqual(by["截止任务"]["start_ts"] + 60 * 60,
                             _ts(2026, 10, 28, 18, 0))

    def test_far_deadline_does_not_preempt_high_priority(self):
        far = self._item(1, "下周才到期", 60, 10, deadline=NOW + 5 * 86400.0)
        hot = self._item(2, "今天重要", 60, 100)
        out = auto_plan.plan([far, hot], now=NOW, days=7)
        by = {x["title"]: x for x in out["assignments"]}
        self.assertLess(by["今天重要"]["start_ts"], by["下周才到期"]["start_ts"])

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


class DynamicPlanTest(unittest.TestCase):
    """v4.1.1：分层目标 / 机会约束 / 每日缓冲 / 锁定窗口 / 推迟。"""

    def _item(self, i, title, dur=60, pri=50, start=0.0, deadline=0.0, **kw):
        row = {"id": i, "title": title, "duration_min": dur, "priority": pri,
               "start_ts": start, "deadline_ts": deadline, "kind": "task",
               "status": "planned", "archived": False, "auto_planned": False,
               "task_type": "general"}
        row.update(kw)
        return row

    def test_category_stats_shrink_toward_learned_mean(self):
        item = self._item(1, "学习任务", 120, 50, task_type="study")
        out = auto_plan.plan(
            [item], now=NOW, days=2,
            category_stats={"study": {"task_type": "study", "dur_mean": 30.0,
                                      "dur_var": 100.0, "samples": 5}})
        got = out["assignments"][0]
        self.assertLess(got["duration_min"], 120)
        self.assertGreaterEqual(got["duration_min"], 80)
        self.assertEqual(out["stats"]["groups"], {"study": 1})

    def test_declared_duration_is_kept_without_history(self):
        item = self._item(1, "自定义时长", 30, 50)
        out = auto_plan.plan([item], now=NOW, days=1)
        self.assertEqual(out["assignments"][0]["duration_min"], 30)

    def test_history_prior_prefers_actual_duration(self):
        history = [{"kind": "task", "status": "done", "duration_min": 60,
                    "actual_min": 30} for _ in range(3)]
        item = self._item(1, "新任务", 120, 50)
        out = auto_plan.plan([item], now=NOW, days=2, history=history)
        self.assertGreaterEqual(out["stats"]["prior_duration"], 25)
        self.assertLessEqual(out["stats"]["prior_duration"], 35)

    def test_risk_z_pads_duration_for_chance_constraint(self):
        item = self._item(1, "风险任务", 60, 50)
        out = auto_plan.plan([item], now=NOW, days=1, risk_z=0.84)
        got = out["assignments"][0]
        self.assertGreater(got["risk_min"], got["duration_min"])
        self.assertLessEqual(got["risk_min"], got["duration_min"] * 1.35)
        self.assertEqual(out["stats"]["risk_z"], 0.84)

    def test_daily_buffer_reserves_end_of_day(self):
        # 工作窗口 08:00-22:00 = 840 分钟；留 120 分钟缓冲后只剩 720，730 分钟放不下。
        item = self._item(1, "超长任务", 730, 50)
        with_buffer = auto_plan.plan([item], now=NOW, days=1, buffer_min=120)
        without = auto_plan.plan([item], now=NOW, days=1, buffer_min=0)
        self.assertEqual(with_buffer["assignments"], [])
        self.assertEqual(len(without["assignments"]), 1)

    def test_locked_auto_plan_is_not_moved(self):
        locked = self._item(1, "锁窗内", 60, 50, start=NOW + 2 * 3600.0,
                            auto_planned=True)
        out = auto_plan.plan([locked], now=NOW, days=2, lock_sec=12 * 3600.0)
        self.assertEqual(out["assignments"], [])
        self.assertEqual(out["fixed"], 1)

    def test_auto_plan_outside_lock_window_keeps_slot(self):
        later = self._item(1, "锁窗外", 60, 50, start=NOW + 24 * 3600.0,
                           auto_planned=True)
        out = auto_plan.plan([later], now=NOW, days=2, lock_sec=12 * 3600.0)
        self.assertEqual(len(out["assignments"]), 1)
        self.assertAlmostEqual(out["assignments"][0]["start_ts"], NOW + 24 * 3600.0)

    def test_auto_plan_beyond_horizon_is_kept(self):
        later = self._item(1, "窗口外", 60, 50, start=NOW + 4 * 86400.0,
                           auto_planned=True)
        out = auto_plan.plan([later], now=NOW, days=3, lock_sec=0)
        self.assertEqual(out["assignments"], [])
        self.assertEqual(out["fixed"], 1)

    def test_deferred_task_moves_to_next_day(self):
        item = self._item(1, "推迟过", 60, 50, defer_count=1)
        out = auto_plan.plan([item], now=NOW, days=2)
        got = out["assignments"][0]
        day = datetime.datetime.fromtimestamp(got["start_ts"]).date()
        self.assertEqual(day, datetime.datetime(2026, 10, 29).date())

    def test_transition_gap_keeps_space_around_fixed_events(self):
        course = self._item(9, "课程", 60, 50, start=NOW + 3600.0,
                            kind="course", weekday=2, period="1")
        task = self._item(1, "任务", 60, 50)
        out = auto_plan.plan([course, task], now=NOW, days=1,
                             transition_min=10, align_min=30)
        start = out["assignments"][0]["start_ts"]
        self.assertGreaterEqual(start, NOW + 2 * 3600.0 + 10 * 60.0)

    def test_task_start_is_aligned_to_half_hour(self):
        ref = _ts(2026, 10, 28, 8, 7)
        task = self._item(1, "对齐任务", 60, 50)
        out = auto_plan.plan([task], now=ref, days=1, align_min=30)
        start = datetime.datetime.fromtimestamp(out["assignments"][0]["start_ts"])
        self.assertEqual((start.hour, start.minute), (8, 30))

    def test_grade_counts_and_assignment_grade(self):
        course = self._item(9, "课程", 60, 50, start=NOW + 3600.0,
                            kind="course", weekday=2, period="1")
        committed = self._item(8, "约定", 30, 70, start=NOW + 5 * 3600.0)
        task = self._item(1, "任务", 60, 50)
        out = auto_plan.plan([course, committed, task], now=NOW, days=2)
        grades = out["stats"]["grades"]
        self.assertEqual(grades.get("fixed"), 1)
        self.assertEqual(grades.get("committed"), 1)
        self.assertEqual(grades.get("flexible"), 1)
        self.assertEqual(out["assignments"][0]["grade"], "flexible")
        self.assertEqual(out["assignments"][0]["grade_label"], "待安排")


class GradeTest(unittest.TestCase):
    def _item(self, **kw):
        row = {"id": 1, "title": "x", "duration_min": 60, "priority": 50,
               "start_ts": 0.0, "deadline_ts": 0.0, "kind": "task",
               "status": "planned", "archived": False, "auto_planned": False}
        row.update(kw)
        return row

    def test_course_is_always_fixed(self):
        item = self._item(kind="course", weekday=2, period="1-2")
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_FIXED)

    def test_user_set_time_is_committed(self):
        item = self._item(start_ts=NOW + 3600.0)
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_COMMITTED)

    def test_missed_user_time_can_be_replanned(self):
        item = self._item(start_ts=NOW - 3600.0)
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_FLEXIBLE)
        out = auto_plan.plan([item], now=NOW, days=1)
        self.assertEqual(len(out["assignments"]), 1)

    def test_ongoing_event_is_committed_and_not_replanned(self):
        """已经开始但还没结束的约定（如 12:00-13:00 比赛）不能当成待安排。"""
        item = self._item(start_ts=NOW - 30 * 60.0)
        self.assertEqual(auto_plan.classify_item(item, now=NOW),
                         auto_plan.GRADE_COMMITTED)
        out = auto_plan.plan([item], now=NOW, days=1)
        self.assertEqual(out["assignments"], [])
        self.assertEqual(out["fixed"], 1)

    def test_weekday_event_without_period_is_committed(self):
        """只有星期的事件（周四篮球赛）是约定，不是固定课程。"""
        item = self._item(start_ts=NOW + 3600.0, weekday=3)
        self.assertEqual(auto_plan.classify_item(item, now=NOW),
                         auto_plan.GRADE_COMMITTED)

    def test_course_without_period_is_still_fixed(self):
        item = self._item(start_ts=NOW + 3600.0, weekday=3, kind="course")
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_FIXED)

    def test_auto_within_lock_is_locked(self):
        item = self._item(start_ts=NOW + 3600.0, auto_planned=True)
        self.assertEqual(auto_plan.classify_item(item, now=NOW, lock_sec=12 * 3600.0),
                         auto_plan.GRADE_LOCKED)

    def test_deadline_task_is_deadline_driven(self):
        item = self._item(deadline_ts=NOW + 86400.0)
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_DEADLINE)

    def test_date_only_task_is_deadline_driven(self):
        item = self._item(start_ts=NOW + 3600.0, when_text="2026-10-28（全天）",
                          deadline_ts=NOW + 12 * 3600.0)
        self.assertEqual(auto_plan.classify_item(item, now=NOW), auto_plan.GRADE_DEADLINE)

    def test_scheduled_when_text_is_not_date_only(self):
        item = self._item(start_ts=NOW + 24 * 3600.0, auto_planned=True,
                          when_text="2026-10-29 08:00-09:00（自动安排）")
        self.assertEqual(auto_plan.classify_item(item, now=NOW, lock_sec=0),
                         auto_plan.GRADE_TENTATIVE)

    def test_auto_outside_lock_is_tentative(self):
        item = self._item(start_ts=NOW + 86400.0, auto_planned=True)
        self.assertEqual(auto_plan.classify_item(item, now=NOW, lock_sec=12 * 3600.0),
                         auto_plan.GRADE_TENTATIVE)

    def test_free_task_and_closed_item(self):
        self.assertEqual(auto_plan.classify_item(self._item(), now=NOW),
                         auto_plan.GRADE_FLEXIBLE)
        self.assertEqual(auto_plan.classify_item(self._item(status="done"), now=NOW),
                         auto_plan.GRADE_CLOSED)

    def test_decorate_adds_readable_grade(self):
        out = auto_plan.decorate_items([self._item(kind="course", weekday=1,
                                                   period="3-4")], now=NOW)
        self.assertEqual(out[0]["grade"], "fixed")
        self.assertEqual(out[0]["grade_label"], "固定课程")


class ReplanGateTest(unittest.TestCase):
    def _item(self, i, title, start=0.0, pri=50, deadline=0.0, **kw):
        row = {"id": i, "title": title, "duration_min": 60, "priority": pri,
               "start_ts": start, "deadline_ts": deadline, "kind": "task",
               "status": "planned", "archived": False, "auto_planned": False}
        row.update(kw)
        return row

    def test_planned_items_without_events_do_not_trigger(self):
        gate = auto_plan.should_replan(
            [self._item(1, "已排", start=NOW + 3600.0)], now=NOW, horizon_days=1)
        self.assertFalse(gate["trigger"])
        self.assertEqual(gate["unplanned_ids"], [])

    def test_new_unplanned_task_triggers(self):
        gate = auto_plan.should_replan([self._item(1, "待定", pri=85)],
                                       now=NOW, horizon_days=2)
        self.assertTrue(gate["trigger"])
        self.assertIn("urgent_unplanned", gate["reasons"])
        self.assertEqual(gate["unplanned_ids"], [1])

    def test_date_only_task_counts_as_unplanned(self):
        item = self._item(1, "全天整理", start=NOW + 3600.0,
                          when_text="2026-10-28（全天）",
                          deadline_ts=NOW + 12 * 3600.0)
        gate = auto_plan.should_replan([item], now=NOW, horizon_days=1)
        self.assertTrue(gate["trigger"])
        self.assertEqual(gate["unplanned_ids"], [1])

    def test_overdue_task_triggers_with_id(self):
        old = self._item(1, "逾期", start=NOW - 3 * 3600.0, pri=80)
        gate = auto_plan.should_replan([old], now=NOW, horizon_days=1)
        self.assertTrue(gate["trigger"])
        self.assertIn("overdue", gate["reasons"])
        self.assertEqual(gate["overdue_ids"], [1])

    def test_capacity_shortage_triggers(self):
        fixed = [self._item(9, "占用", start=NOW + 8 * 3600.0, duration_min=600)]
        tasks = [self._item(i, "高优%d" % i, pri=100) for i in range(1, 9)]
        gate = auto_plan.should_replan(fixed + tasks, now=NOW, horizon_days=1)
        self.assertTrue(gate["trigger"])
        self.assertIn("capacity", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
