"""生命时间线（P0）门卫：持久化、相对时间、情绪恢复、与 agent / API 的接线。

全部用临时库与假 agent，不触网、不碰真实 data/life.db。
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import life
from app import prompts
from app.main import create_app
from helpers import StubEngine, StubLLM, mk_agent

T0 = 1_760_000_000.0          # 2025-10 的任意墙钟锚点，测试不依赖本地日期


class LifeStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "life.db")
        self.store = life.LifeStore(self.path)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_touch_persists_state_and_events(self):
        eid = self.store.touch("chat", source="pet", summary="  你好   呀  ", now=T0)
        self.assertGreater(eid, 0)
        s = self.store.state
        self.assertAlmostEqual(s["last_interaction_ts"], T0)
        self.assertAlmostEqual(s["last_chat_ts"], T0)
        ev = self.store.recent_events(1)[0]
        self.assertEqual(ev["kind"], "chat")
        self.assertEqual(ev["source"], "pet")
        self.assertEqual(ev["summary"], "你好 呀")

        self.store.touch("idle", now=T0 + 60)
        s2 = self.store.state
        self.assertAlmostEqual(s2["last_proactive_ts"], T0 + 60)
        self.assertAlmostEqual(s2["last_interaction_ts"], T0, msg="主动搭话不冒充玩家互动")

        self.store.close()
        self.store = life.LifeStore(self.path)
        s3 = self.store.state
        self.assertAlmostEqual(s3["last_interaction_ts"], T0)
        self.assertAlmostEqual(s3["last_proactive_ts"], T0 + 60)
        self.assertEqual([e["kind"] for e in self.store.recent_events(5)], ["idle", "chat"])

    def test_touch_without_event_only_moves_timestamps(self):
        self.store.touch("game_move", now=T0, event=False)
        self.assertEqual(self.store.recent_events(5), [])
        s = self.store.state
        self.assertAlmostEqual(s["last_game_ts"], T0)
        self.assertAlmostEqual(s["last_interaction_ts"], T0)

    def test_temporal_facts(self):
        self.store.touch("chat", now=T0)
        self.store.touch("idle", now=T0 + 60)
        self.store.touch("game_move", now=T0 + 120, event=False)
        out = self.store.temporal_facts(now=T0 + 600)
        self.assertAlmostEqual(out["since_interaction_sec"], 480.0)   # 最近一次是 t+120 的落子
        self.assertAlmostEqual(out["since_proactive_sec"], 540.0)
        self.assertAlmostEqual(out["since_chat_sec"], 600.0)
        self.assertAlmostEqual(out["since_game_sec"], 480.0)
        self.assertIn("day_phase", out)
        self.assertIn("is_weekend", out)
        self.assertIn("hour", out)

    def test_temporal_facts_missing_signals_are_absent(self):
        out = self.store.temporal_facts(now=T0)
        for key in ("since_interaction_sec", "since_proactive_sec",
                    "since_chat_sec", "since_game_sec"):
            self.assertNotIn(key, out)
        self.assertIn("day_phase", out)

    def test_dirty_mood_is_ignored_and_valid_mood_roundtrips(self):
        self.store.save_mood({"v": 2.0, "c": "oops", "a": 0.2}, now=T0)
        self.assertEqual(self.store.state["mood"], {})

        self.store.save_mood({"v": 0.9, "c": 0.2, "a": 0.8, "name": "开心"}, now=T0)
        self.assertEqual(self.store.state["mood"], {"v": 0.9, "c": 0.2, "a": 0.8})
        self.store.close()
        self.store = life.LifeStore(self.path)
        rest = self.store.mood_after_break(now=T0 + 1800.0)   # 半小时 = 半衰期
        self.assertGreater(rest["v"], 0.58)
        self.assertLess(rest["v"], 0.9)
        self.assertGreater(rest["a"], 0.30)
        self.assertLess(rest["a"], 0.8)

    def test_future_mood_timestamp_is_not_restored(self):
        self.store.save_mood({"v": 0.9, "c": 0.2, "a": 0.8}, now=T0 + 100.0)
        self.assertEqual(self.store.mood_after_break(now=T0), {})

    def test_energy_and_boredom_evolve_with_time(self):
        store = life.LifeStore(os.path.join(self.dir.name, "evolve.db"), now=T0)
        s0 = store.state
        self.assertAlmostEqual(s0["energy"], life.LIFE_ENERGY_BASE)
        self.assertAlmostEqual(s0["boredom"], life.LIFE_BOREDOM_BASE)
        store.tick(now=T0 + 8.0 * 3600.0)
        s1 = store.state
        self.assertLess(s1["energy"], s0["energy"])
        self.assertGreater(s1["boredom"], s0["boredom"])
        self.assertGreaterEqual(s1["energy"], life.ENERGY_FLOOR)
        self.assertLessEqual(s1["boredom"], 1.0)
        store.close()

    def test_chat_restores_energy_and_relieves_boredom(self):
        store = life.LifeStore(os.path.join(self.dir.name, "chat.db"), now=T0)
        store.tick(now=T0 + 8.0 * 3600.0)
        before = store.state
        store.touch("chat", summary="聊会儿", now=T0 + 8.0 * 3600.0 + 60.0)
        after = store.state
        self.assertGreater(after["energy"], before["energy"])
        self.assertLess(after["boredom"], before["boredom"])
        self.assertGreater(after["affinity"], before["affinity"])
        self.assertGreater(after["trust"], before["trust"])
        store.close()

    def test_slow_state_survives_restart(self):
        path = os.path.join(self.dir.name, "restart.db")
        store = life.LifeStore(path, now=T0)
        store.tick(now=T0 + 6.0 * 3600.0)
        before = store.state
        store.close()
        store2 = life.LifeStore(path, now=T0 + 6.0 * 3600.0)
        after = store2.state
        self.assertAlmostEqual(after["energy"], before["energy"], places=6)
        self.assertAlmostEqual(after["boredom"], before["boredom"], places=6)
        self.assertAlmostEqual(after["affinity"], before["affinity"], places=6)
        self.assertAlmostEqual(after["trust"], before["trust"], places=6)
        self.assertAlmostEqual(after["dominance"], before["dominance"], places=6)
        store2.close()

    def test_event_cap_keeps_newest(self):
        old = life.MAX_EVENTS
        life.MAX_EVENTS = 3
        try:
            for i in range(6):
                self.store.touch("chat", summary=str(i), now=T0 + i)
            rows = self.store.recent_events(10)
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["summary"] for r in rows], ["5", "4", "3"])
        finally:
            life.MAX_EVENTS = old

    def test_schedule_upsert_upcoming_and_context(self):
        saved = self.store.upsert_schedule([
            {"start_ts": T0 + 3600.0, "when_text": "2026-10-27 18:00",
             "title": "和朋友喝咖啡", "notes": "带棋盘", "source": "a.png"},
            {"start_ts": 0.0, "when_text": "待定", "title": "整理旧棋谱",
             "notes": "", "source": "a.png"},
        ], source="a.png", now=T0)
        self.assertEqual(len(saved), 2)
        rows = self.store.list_schedule(10)
        self.assertEqual(rows[0]["title"], "和朋友喝咖啡")
        self.assertEqual(rows[1]["title"], "整理旧棋谱")
        self.assertEqual(self.store.next_schedule(now=T0)["title"], "和朋友喝咖啡")
        ctx = self.store.temporal_facts(now=T0)
        self.assertEqual(ctx["next_schedule_title"], "和朋友喝咖啡")
        self.assertAlmostEqual(ctx["next_schedule_in_sec"], 3600.0)
        # 同一来源 + 同一时间 + 同一标题：更新备注，不新增行
        again = self.store.upsert_schedule([
            {"start_ts": T0 + 3600.0, "when_text": "2026-10-27 18:00",
             "title": "和朋友喝咖啡", "notes": "改成带棋盘和计时器", "source": "a.png"},
        ], source="a.png", now=T0 + 10)
        self.assertEqual(len(again), 1)
        self.assertEqual(len(self.store.list_schedule(10)), 2)
        self.assertIn("计时器", self.store.next_schedule(now=T0)["notes"])
        # 完成后不再作为“下一条”
        self.assertTrue(self.store.set_schedule_status(saved[0]["id"], "done"))
        self.assertEqual(self.store.next_schedule(now=T0), {})
        self.assertEqual(len(self.store.list_schedule(10)), 1)

    def test_schedule_dirty_values_are_safe(self):
        self.assertEqual(self.store.upsert_schedule([], source="x"), [])
        saved = self.store.upsert_schedule([
            {"start_ts": "bad", "title": "   ", "when_text": None},
            {"start_ts": float("inf"), "title": "坏时间", "when_text": "待定"},
        ], source="x")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["start_ts"], 0.0)
        self.assertEqual(self.store.next_schedule(now=T0), {})


    def test_schedule_course_fields_and_dedupe(self):
        """课程条目带星期 / 节次 / 教师 / 地点；同一门课重复扫描按日期+节次+标题更新而不是新增。"""
        saved = self.store.upsert_schedule([{
            "start_ts": T0 + 3600.0, "when_text": "2026-10-28 周三 第1-2节 08:00-09:35",
            "title": "高等数学", "notes": "", "source": "课表.png",
            "date": "2026-10-28", "weekday": 2, "period": "1-2",
            "period_start": 1, "period_end": 2, "start_time": "08:00",
            "end_time": "09:35", "teacher": "张老师", "location": "综合楼101",
        }], source="课表.png", now=T0)
        self.assertEqual(len(saved), 1)
        rows = self.store.list_schedule(10)
        row = rows[0]
        self.assertEqual(row["weekday"], 2)
        self.assertEqual(row["period"], "1-2")
        self.assertEqual(row["period_start"], 1)
        self.assertEqual(row["period_end"], 2)
        self.assertEqual(row["start_time"], "08:00")
        self.assertEqual(row["end_time"], "09:35")
        self.assertEqual(row["teacher"], "张老师")
        self.assertEqual(row["location"], "综合楼101")
        self.assertEqual(row["date"], "2026-10-28")
        again = self.store.upsert_schedule([{
            "start_ts": T0 + 3700.0, "when_text": "2026-10-28 周三 第1-2节 08:00-09:35",
            "title": "高等数学", "notes": "带上作业", "source": "课表2.png",
            "date": "2026-10-28", "weekday": 2, "period": "1-2",
            "period_start": 1, "period_end": 2, "start_time": "08:00",
            "end_time": "09:35", "teacher": "", "location": "",
        }], source="课表2.png", now=T0 + 10)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], saved[0]["id"])
        self.assertEqual(again[0]["notes"], "带上作业")
        # 换一张图 / 重扫时 source 会变，但内容相同：不新增，合并来源并保留旧教师。
        self.assertIn("课表.png", again[0]["source"])
        self.assertIn("课表2.png", again[0]["source"])
        self.assertEqual(again[0]["teacher"], "张老师")
        self.assertEqual(again[0]["location"], "综合楼101")
        self.assertEqual(len(self.store.list_schedule(10)), 1)

    def test_startup_cleans_existing_course_duplicates(self):
        """旧版按 source 去重留下的重复行，启动时按课程格子清理并合并来源。"""
        saved = self.store.upsert_schedule([{
            "start_ts": T0 + 3600.0, "when_text": "2026-10-28 周三 第1-2节 08:00-09:35",
            "title": "高等数学", "source": "课表.png",
            "date": "2026-10-28", "weekday": 2, "period": "1-2",
            "period_start": 1, "period_end": 2, "start_time": "08:00",
            "end_time": "09:35", "teacher": "张老师", "location": "综合楼101",
        }], source="课表.png", now=T0)
        self.assertEqual(len(saved), 1)
        self.store._db.execute(
            "INSERT INTO schedule_items(start_ts, title, source, date_text, weekday, period, "
            "period_start, period_end, start_time, end_time, kind, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'planned')",
            (T0 + 3600.0, "高等数学", "课表2.png", "2026-10-28", 2, "1-2",
             1, 2, "08:00", "09:35", "course"))
        self.store._db.commit()
        self.assertEqual(len(self.store.list_schedule(10)), 2)
        self.store.close()
        self.store = life.LifeStore(self.path)
        rows = self.store.list_schedule(10)
        self.assertEqual(len(rows), 1)
        self.assertIn("课表.png", rows[0]["source"])
        self.assertIn("课表2.png", rows[0]["source"])
        self.assertEqual(rows[0]["teacher"], "张老师")
        self.assertEqual(rows[0]["location"], "综合楼101")

    def test_migration_downgrades_weekday_only_course(self):
        """旧数据把群公告标成 course：没有节次时迁移要降级为普通约定。"""
        saved = self.store.upsert_schedule([{
            "start_ts": T0 + 7200.0, "when_text": "2026-10-29 周四 12:00-13:00",
            "title": "院系杯男子篮球赛", "source": "chat.jpg",
            "date": "2026-10-29", "weekday": 3, "period": "",
            "start_time": "12:00", "end_time": "13:00", "kind": "course",
        }], source="chat.jpg", now=T0)
        self.assertEqual(saved[0]["kind"], "course")
        self.store.close()
        self.store = life.LifeStore(self.path)
        rows = self.store.list_schedule(10)
        self.assertEqual(rows[0]["kind"], "task")

    def test_schedule_reminder_archive_cleanup_and_plan_update(self):
        """到点提醒 / 留档 / 清理 / 自动排班写入：完整生命周期（纯临时库）。"""
        saved = self.store.upsert_schedule([
            {"start_ts": T0 + 120.0, "when_text": "2026-10-27 18:02", "title": "到点任务",
             "priority": 90, "duration_min": 30, "kind": "task", "source": "a.txt"},
            {"start_ts": T0 - 20 * 3600.0, "when_text": "昨天", "title": "过期任务",
             "priority": 40, "duration_min": 20, "kind": "task", "source": "b.txt"},
        ], source="a.txt", now=T0)
        self.assertEqual(len(saved), 2)
        due = self.store.due_reminders(now=T0 + 180.0, window_sec=600, lead_sec=60)
        self.assertEqual([x["title"] for x in due], ["到点任务"])
        self.assertTrue(self.store.mark_reminded(due[0]["id"], now=T0 + 180.0))
        self.assertEqual(self.store.due_reminders(now=T0 + 200.0), [])

        self.assertGreaterEqual(self.store.archive_schedule(now=T0), 1)
        rows = self.store.list_schedule(20)
        self.assertEqual([x["title"] for x in rows], ["到点任务"])
        archived_rows = self.store.list_schedule(20, include_archived=True)
        expired = [x for x in archived_rows if x["title"] == "过期任务"][0]
        self.assertTrue(expired["archived"])
        self.assertGreaterEqual(self.store.cleanup_schedule(days=1, now=T0 + 2 * 86400.0), 1)

        # 自动排班写入：未定任务获得 start_ts / when_text，并标记 auto_planned。
        task = self.store.upsert_schedule([{
            "start_ts": 0.0, "when_text": "待定", "title": "待安排", "kind": "task",
            "priority": 70, "duration_min": 45, "source": "c.txt",
        }], source="c.txt", now=T0)[0]
        out = self.store.update_schedule_plan([{
            "id": task["id"], "start_ts": T0 + 3600.0,
            "when_text": "2026-10-27 19:00-19:45（自动安排）", "duration_min": 45,
        }], now=T0)
        row = [x for x in out if x["id"] == task["id"]][0]
        self.assertAlmostEqual(row["start_ts"], T0 + 3600.0)
        self.assertTrue(row["auto_planned"])
        self.assertIn("自动安排", row["when_text"])

    def test_schedule_stats_and_execution_feedback(self):
        """类别统计持久化 + 完成 / 推迟 / 跳过三种反馈。"""
        task = self.store.upsert_schedule([{
            "start_ts": T0 + 60.0, "when_text": "今天 09:00", "title": "复习第三章",
            "kind": "task", "source": "plan.txt", "task_type": "study",
            "duration_min": 60, "priority": 70,
        }], source="plan.txt", now=T0)[0]
        self.assertEqual(task["task_type"], "study")

        done = self.store.record_feedback(task["id"], "done", actual_min=45,
                                          now=T0 + 600.0)
        self.assertEqual(done["status"], "done")
        self.assertAlmostEqual(done["actual_min"], 45.0)
        self.assertAlmostEqual(done["completed_ts"], T0 + 600.0)
        self.assertEqual(done["outcome"], "done")

        stats = self.store.load_schedule_stats()
        self.assertEqual(stats, {})
        self.assertTrue(self.store.save_schedule_stats({
            "study": {"dur_mean": 50.0, "dur_var": 120.0, "samples": 1,
                      "done_count": 1, "defer_count": 0, "dev_streak": 0,
                      "change_point_ts": 0.0, "updated_ts": T0 + 600.0}}))
        got = self.store.load_schedule_stats()["study"]
        self.assertAlmostEqual(got["dur_mean"], 50.0)
        self.assertAlmostEqual(got["dur_var"], 120.0)
        self.assertEqual(got["samples"], 1)

        defer = self.store.upsert_schedule([{
            "start_ts": T0 + 3600.0, "when_text": "今天 10:00", "title": "写报告",
            "kind": "task", "source": "plan.txt", "task_type": "work",
            "auto_planned": True, "duration_min": 60,
        }], source="plan.txt", now=T0)[0]
        self.store.mark_reminded(defer["id"], T0 + 10.0)
        moved = self.store.record_feedback(defer["id"], "deferred", now=T0 + 120.0)
        self.assertEqual(moved["status"], "planned")
        self.assertAlmostEqual(moved["start_ts"], 0.0)
        self.assertEqual(moved["when_text"], "待定")
        self.assertFalse(moved["auto_planned"])
        self.assertAlmostEqual(moved["reminded_ts"], 0.0)
        self.assertEqual(moved["defer_count"], 1)
        self.assertEqual(moved["outcome"], "deferred")

        skip = self.store.upsert_schedule([{
            "start_ts": T0 + 7200.0, "when_text": "今天 11:00", "title": "取消的事",
            "kind": "task", "source": "plan.txt", "duration_min": 30,
        }], source="plan.txt", now=T0)[0]
        cancelled = self.store.record_feedback(skip["id"], "skipped", now=T0 + 180.0)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["outcome"], "skipped")

        history = self.store.list_schedule_history(20)
        self.assertGreaterEqual(len(history), 3)
        self.assertEqual(history[0]["id"], task["id"])       # updated_ts 最大的在前
        self.assertEqual({x["id"] for x in history[:3]},
                         {task["id"], defer["id"], skip["id"]})

    def test_delete_schedule_source_replaces_old_scan(self):
        """重扫同一份资料：按来源清掉旧文本行，新的结构化课程再写入。"""
        self.store.upsert_schedule([{
            "start_ts": T0 + 60.0, "when_text": "2026-10-27 08:00",
            "title": "旧文本日程", "source": "课表.jpg",
        }], source="课表.jpg", now=T0)
        self.assertEqual(len(self.store.list_schedule(10)), 1)
        removed = self.store.delete_schedule_source("课表.jpg")
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.list_schedule(10), [])
        self.store.upsert_schedule([{
            "start_ts": T0 + 3600.0, "when_text": "2026-10-28 周三 第1-2节",
            "title": "高等数学", "source": "课表.jpg", "date": "2026-10-28",
            "weekday": 2, "period": "1-2", "teacher": "张老师",
        }], source="课表.jpg", now=T0)
        rows = self.store.list_schedule(10)
        self.assertEqual([x["title"] for x in rows], ["高等数学"])

    def test_old_schedule_table_is_migrated(self):
        """v4.0.0 之前的旧库只有 7 列：启动时补列，旧行照常可读、新字段走默认值。"""
        d = tempfile.TemporaryDirectory()
        path = os.path.join(d.name, "old.db")
        try:
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE schedule_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL NOT NULL DEFAULT 0,
                when_text TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned', created_ts REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0)""")
            conn.execute("INSERT INTO schedule_items(start_ts, when_text, title, source) "
                         "VALUES(?,?,?,?)", (T0 + 60.0, "2026-10-27 18:00", "旧日程", "old.png"))
            conn.commit()
            conn.close()
            store = life.LifeStore(path, now=T0)
            try:
                rows = store.list_schedule(10)
                self.assertEqual(rows[0]["title"], "旧日程")
                self.assertEqual(rows[0]["weekday"], -1)
                self.assertEqual(rows[0]["period"], "")
                store.upsert_schedule([{
                    "start_ts": T0 + 3600.0, "when_text": "2026-10-28 周三 第1-2节",
                    "title": "新课程", "date": "2026-10-28", "weekday": 2,
                    "period": "1-2", "teacher": "张老师", "location": "综合楼101",
                }], source="new.png", now=T0)
                new_rows = store.list_schedule(10)
                self.assertEqual({x["title"] for x in new_rows}, {"旧日程", "新课程"})
            finally:
                store.close()
        finally:
            d.cleanup()

    def test_unavailable_db_falls_back_to_memory(self):
        bad = tempfile.TemporaryDirectory()
        try:
            store = life.LifeStore(bad.name)       # 路径是目录：sqlite 打不开
            self.assertFalse(store.available)
            store.touch("chat", summary="won't persist", now=T0)
            self.assertAlmostEqual(store.state["last_interaction_ts"], T0)
            self.assertIn("since_interaction_sec", store.temporal_facts(now=T0 + 5))
            self.assertEqual(store.recent_events(5), [])
            store.close()
        finally:
            bad.cleanup()


class LifeAgentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = life.LifeStore(os.path.join(self.dir.name, "life.db"), now=T0)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_time_facts_merges_temporal_context(self):
        agent, _, _, _ = mk_agent()
        agent.life = self.store
        agent.time_aware_enabled = True
        self.store.touch("chat", now=T0 - 7200.0)
        facts = agent.time_facts(now=T0)
        self.assertRegex(facts["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("weekday", facts)
        self.assertAlmostEqual(facts["since_interaction_sec"], 7200.0)
        self.assertIn("day_phase", facts)

    def test_time_facts_unchanged_without_life(self):
        agent, _, _, _ = mk_agent()
        agent.time_aware_enabled = True
        facts = agent.time_facts(now=T0)
        self.assertNotIn("since_interaction_sec", facts)
        self.assertIn("date", facts)

    def test_load_life_restores_mood_with_decay(self):
        agent, _, _, _ = mk_agent()
        agent.life = self.store
        self.store.save_mood({"v": 0.95, "c": 0.1, "a": 0.9}, now=T0 - 1800.0)
        agent.load_life(now=T0)
        self.assertGreater(agent.mood.v, 0.58)
        self.assertLess(agent.mood.v, 0.95)
        self.assertGreater(agent.mood.a, 0.30)
        self.assertLess(agent.mood.a, 0.9)

    def test_reset_game_keeps_mood_when_life_is_shared(self):
        agent, _, _, _ = mk_agent()
        agent.life = self.store
        agent.mood.v = 0.91
        agent.mood.c = 0.21
        agent.mood.a = 0.77
        agent.reset_game()
        self.assertAlmostEqual(agent.mood.v, 0.91, places=4)
        self.assertAlmostEqual(agent.mood.c, 0.21, places=4)
        self.assertAlmostEqual(agent.mood.a, 0.77, places=4)

    def test_mood_payload_adds_slow_fields_only_with_life(self):
        agent, _, _, _ = mk_agent()
        self.assertNotIn("energy", agent.mood_payload())
        agent.life = self.store
        payload = agent.mood_payload()
        for key in ("energy", "boredom", "affinity", "trust", "dominance"):
            self.assertIn(key, payload)
            self.assertGreaterEqual(payload[key], 0.0)
            self.assertLessEqual(payload[key], 1.0)

    def test_life_note_translates_extreme_state_without_numbers(self):
        agent, _, _, _ = mk_agent()
        agent.life = self.store
        self.store.tick(now=T0 + 12.0 * 3600.0)
        note = agent.life_note()
        self.assertIn("累", note)
        self.assertIn("互动", note)
        self.assertNotIn("%", note)
        self.assertNotIn("0.", note)
        # 不装配生命时间线：空串（提示词逐字符不变）
        plain, _, _, _ = mk_agent()
        self.assertEqual(plain.life_note(), "")

    def test_extra_note_includes_life_without_persona(self):
        agent, _, _, _ = mk_agent()
        agent.life = self.store
        self.store.tick(now=T0 + 12.0 * 3600.0)
        note = agent._extra_note()
        self.assertIn("身体与心绪", note)


class LifeApiTest(unittest.TestCase):
    def test_life_endpoint_reports_state_temporal_and_events(self):
        d = tempfile.TemporaryDirectory()
        store = life.LifeStore(os.path.join(d.name, "life.db"))
        try:
            store.touch("chat", source="pet", summary="你好", now=T0)
            app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False, life=store)
            with TestClient(app) as client:
                r = client.get("/api/life")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertTrue(body["ok"])
                self.assertTrue(body["available"])
                self.assertIn("state", body)
                self.assertIn("temporal", body)
                self.assertEqual(body["events"][0]["kind"], "chat")
                self.assertGreaterEqual(body["temporal"]["since_interaction_sec"], 0.0)

                # 心跳也让同一份时间线推进（桌宠 / 围棋看到的是同一条）
                client.post("/api/mood/tick")
                body2 = client.get("/api/life").json()
                self.assertGreaterEqual(body2["state"]["last_tick_ts"], T0)

                r2 = client.get("/api/life?limit=0")
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json()["events"], [])
        finally:
            store.close()
            d.cleanup()

    def test_files_and_schedule_endpoints(self):
        from app.config import AzureConfig

        d = tempfile.TemporaryDirectory()
        store = life.LifeStore(os.path.join(d.name, "life.db"), now=T0)
        inbox = os.path.join(d.name, "inbox")
        os.makedirs(inbox)
        with open(os.path.join(inbox, "会议.txt"), "w", encoding="utf-8") as f:
            f.write("10月28日 15:30 评审")
        try:
            store.upsert_schedule([{
                "start_ts": T0 + 3600.0, "when_text": "2026-10-27 18:00",
                "title": "和朋友喝咖啡", "notes": "", "source": "会议.txt",
            }], source="会议.txt", now=T0)
            cfg = AzureConfig()
            cfg.assistant_inbox = inbox
            app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(),
                             autostart=False, life=store)
            with TestClient(app) as client:
                files = client.get("/api/files").json()
                self.assertTrue(files["ok"])
                self.assertEqual(files["files"][0]["name"], "会议.txt")
                sched = client.get("/api/schedule").json()
                self.assertTrue(sched["ok"])
                self.assertEqual(sched["items"][0]["title"], "和朋友喝咖啡")
                self.assertEqual(client.post("/api/schedule/status",
                                             json={"id": sched["items"][0]["id"],
                                                   "status": "done"}).json()["ok"], True)
        finally:
            store.close()
            d.cleanup()


class LifePromptTest(unittest.TestCase):
    def test_prompt_renders_phase_and_relative_time(self):
        text = prompts.time_note({
            "date": "2026-10-19", "weekday": "周一", "clock": "21:03",
            "day_phase": "夜里", "since_interaction_sec": 7200.0,
            "since_proactive_sec": 3600.0, "since_chat_sec": 7200.0,
            "next_schedule_title": "和朋友喝咖啡",
            "next_schedule_at": "2026-10-19 22:00",
            "next_schedule_in_sec": 3600.0,
        })
        self.assertIn("（夜里）", text)
        self.assertIn("距上次和玩家互动 2 小时", text)
        self.assertIn("距上次你主动开口 1 小时", text)
        self.assertIn("距上次聊天 2 小时", text)
        self.assertIn("日程上最近的是「和朋友喝咖啡」", text)

    def test_prompt_omits_short_gaps(self):
        text = prompts.time_note({
            "date": "2026-10-19", "weekday": "周一", "clock": "21:03",
            "day_phase": "夜里", "since_interaction_sec": 30.0,
        })
        self.assertIn("（夜里）", text)
        self.assertNotIn("距上次和玩家互动", text)

    def test_old_shape_unchanged(self):
        text = prompts.time_note({"date": "2026-10-19", "weekday": "周一", "clock": "21:03"})
        self.assertIn("现在是 2026-10-19 周一 21:03", text)
        self.assertNotIn("（夜里）", text)
