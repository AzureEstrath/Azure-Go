"""资料 → 日程：JSON 提取 / 日期归一化 / 去重 / 资料箱列表。"""

import asyncio
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import file_service, schedule
from app.config import AzureConfig
from helpers import StubLLM

# 固定本地时间：2026-10-27（周二）10:00
NOW = datetime.datetime(2026, 10, 27, 10, 0).timestamp()


class ScheduleExtractTest(unittest.TestCase):
    def test_json_array_accepts_fence_and_object(self):
        self.assertEqual(schedule._json_array('```json\n[{"title":"a"}]\n```'),
                         [{"title": "a"}])
        self.assertEqual(schedule._json_array('{"items":[{"title":"b"}]}'),
                         [{"title": "b"}])
        self.assertEqual(schedule._json_array("不是 JSON"), [])

    def test_normalize_items_resolves_relative_dates_and_times(self):
        items = schedule.normalize_items([
            {"date": "2026-10-28", "time": "15:30", "title": "评审",
             "notes": "带电脑"},
            {"date": "明天", "time": "09:00", "title": "晨会"},
            {"date": None, "time": "20:00", "title": "给妈妈打电话"},
            {"title": "没有日期的事"},
            {"date": "2026-10-28", "time": "15:30", "title": "评审"},
        ], source="a.png", now=NOW)
        self.assertEqual([x["title"] for x in items],
                         ["给妈妈打电话", "晨会", "评审", "没有日期的事"])
        by_title = {x["title"]: x for x in items}
        self.assertEqual(by_title["晨会"]["when_text"], "2026-10-28 09:00")
        self.assertEqual(by_title["评审"]["when_text"], "2026-10-28 15:30")
        self.assertEqual(by_title["给妈妈打电话"]["when_text"], "2026-10-27 20:00")
        self.assertEqual(by_title["没有日期的事"]["start_ts"], 0.0)
        self.assertEqual(by_title["没有日期的事"]["when_text"], "待定")
        self.assertEqual(len(items), 4, "重复标题 + 时间应去重")

    def test_date_only_is_all_day_and_past_time_rolls_to_tomorrow(self):
        items = schedule.normalize_items([
            {"date": "2026-11-01", "title": "全天整理"},
            {"time": "08:00", "title": "早起"},
        ], source="b.txt", now=NOW)
        by_title = {x["title"]: x for x in items}
        self.assertEqual(by_title["全天整理"]["when_text"], "2026-11-01（全天）")
        self.assertGreater(by_title["全天整理"]["deadline_ts"], 0.0)
        self.assertEqual(by_title["早起"]["when_text"], "2026-10-28 08:00")

    def test_extract_items_calls_llm_and_returns_list(self):
        llm = StubLLM(text='[{"date":"2026-10-28","time":"15:30","title":"评审"}]')
        got = asyncio.run(schedule.extract_items(llm, "文件：会议邀请\n时间：10月28日 15:30", now=NOW))
        self.assertEqual(got[0]["title"], "评审")
        self.assertEqual(llm.think_modes[-1], 0)

    def test_scan_text_and_memory_text(self):
        scan = {"files": [
            {"name": "a.png", "ocr": "10月28日 15:30 评审", "vlm": "会议室"},
            {"name": "b.txt", "text": "明天九点晨会"},
            {"name": "c.exe", "error": "不支持"},
        ]}
        text = schedule.scan_text(scan)
        self.assertIn("a.png", text)
        self.assertIn("10月28日", text)
        self.assertIn("明天九点晨会", text)
        self.assertIn("c.exe", text)
        self.assertIn("日程：", schedule.memory_text(
            {"when_text": "2026-10-28 15:30", "title": "评审", "notes": "带电脑"}))

    def test_course_items_keep_weekday_period_teacher_location(self):
        """课程表条目：节次跨两节写 1-2，教师/地点/日期都要留下（前端画周课表用）。"""
        items = schedule.normalize_items([
            {"date": "2026-10-28", "weekday": "周三", "period": "第1-2节",
             "time": "08:00", "end_time": "09:35", "title": "高等数学(基础)",
             "teacher": "王三强", "location": "二教20"},
        ], source="课表.png", now=NOW)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["weekday"], 2)
        self.assertEqual(it["period"], "1-2")
        self.assertEqual(it["period_start"], 1)
        self.assertEqual(it["period_end"], 2)
        self.assertEqual(it["start_time"], "08:00")
        self.assertEqual(it["end_time"], "09:35")
        self.assertEqual(it["teacher"], "王三强")
        self.assertEqual(it["location"], "二教20")
        self.assertEqual(it["date"], "2026-10-28")
        self.assertIn("周三", it["when_text"])
        self.assertIn("第1-2节", it["when_text"])
        self.assertIn("王三强", schedule.memory_text(it))
        self.assertIn("@二教20", schedule.memory_text(it))

    def test_task_priority_duration_and_deadline_are_parsed(self):
        items = schedule.normalize_items([
            {"title": "写实验报告", "priority": "紧急", "duration": "1.5小时",
             "deadline": "2026-10-28 18:00"},
        ], source="todo.txt", now=NOW)
        it = items[0]
        self.assertEqual(it["kind"], "task")
        self.assertEqual(it["priority"], 90)
        self.assertEqual(it["duration_min"], 90)
        self.assertGreater(it["deadline_ts"], 0.0)
        self.assertEqual(it["when_text"], "待定")

    def test_date_with_period_infers_weekday(self):
        """模型给了日期 + 节次但漏了星期：由日期推星期，前端照样能排进周课表。"""
        items = schedule.normalize_items([
            {"date": "2026-10-28", "period": "1-2", "title": "工程图学",
             "teacher": "徐道春"},
        ], source="课表.png", now=NOW)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["weekday"], 2)      # 2026-10-28 是周三
        self.assertEqual(it["date"], "2026-10-28")
        self.assertEqual(it["period"], "1-2")

    def test_same_course_in_two_weeks_is_deduped_by_date_period_title(self):
        raw = [
            {"date": "2026-10-28", "period": "1-2", "title": "高等数学"},
            {"date": "2026-10-28", "period": "1-2", "title": " 高等数学 "},
            {"date": "2026-11-04", "period": "1-2", "title": "高等数学"},
        ]
        items = schedule.normalize_items(raw, source="课表.png", now=NOW)
        self.assertEqual(len(items), 2, "同一周同一门课只留一条；下一周保留")
        self.assertEqual({x["date"] for x in items}, {"2026-10-28", "2026-11-04"})


class FilesTest(unittest.TestCase):
    def test_list_files_filters_unsupported_and_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
                f.write("hello")
            with open(os.path.join(d, "b.png"), "wb") as f:
                f.write(b"png")
            with open(os.path.join(d, "c.exe"), "wb") as f:
                f.write(b"exe")
            with open(os.path.join(d, ".hidden"), "w", encoding="utf-8") as f:
                f.write("x")
            cfg = AzureConfig()
            cfg.assistant_inbox = d
            got = file_service.list_files(cfg)
            self.assertTrue(got["ok"])
            self.assertEqual({x["name"] for x in got["files"]}, {"a.txt", "b.png"})

    def test_scan_auto_plans_undated_task(self):
        """扫描出待定任务后，auto_plan_enabled 会把它排进未来 7 天的空闲时间。"""
        from fastapi.testclient import TestClient
        from app import life
        from app.main import create_app
        from helpers import StubEngine

        d, py, script = file_service._find_scanner()
        if not py or not script:
            self.skipTest("本机没有 peek/.venv 扫描器")
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox")
            os.makedirs(inbox)
            with open(os.path.join(inbox, "todo.txt"), "w", encoding="utf-8") as f:
                f.write("写实验报告，预计一个半小时，很急。")
            store = life.LifeStore(os.path.join(tmp, "life.db"))
            try:
                cfg = AzureConfig()
                cfg.assistant_inbox = inbox
                cfg.vlm_auto_start = False
                cfg.auto_plan_enabled = True
                llm = StubLLM(text='[{"title":"写实验报告","priority":"紧急",'
                                  '"duration":"1.5小时"}]')
                app = create_app(cfg=cfg, engine=StubEngine(), llm=llm,
                                 autostart=False, life=store)
                with TestClient(app) as client:
                    body = client.post("/api/files/scan", json={}).json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(body["auto_plan"]["planned"], 1)
                    row = body["schedule"][0]
                    self.assertEqual(row["title"], "写实验报告")
                    self.assertGreater(row["start_ts"], 0.0)
                    self.assertTrue(row["auto_planned"])
                    self.assertEqual(row["duration_min"], 90)
            finally:
                store.close()

    def test_scan_endpoint_end_to_end_with_text_file(self):
        """真实扫描器 + 真实 LifeStore + StubLLM：txt → 日程 → 共享日程接口。"""
        from fastapi.testclient import TestClient
        from app import life
        from app.main import create_app
        from helpers import StubEngine

        d, py, script = file_service._find_scanner()
        if not py or not script:
            self.skipTest("本机没有 peek/.venv 扫描器")
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox")
            os.makedirs(inbox)
            with open(os.path.join(inbox, "meeting.txt"), "w", encoding="utf-8") as f:
                f.write("2026-10-28 15:30 项目评审，带上电脑。")
            store = life.LifeStore(os.path.join(tmp, "life.db"), now=NOW)
            try:
                cfg = AzureConfig()
                cfg.assistant_inbox = inbox
                cfg.vlm_auto_start = False
                llm = StubLLM(text='[{"date":"2026-10-28","time":"15:30",'
                                  '"title":"项目评审","notes":"带电脑"}]')
                app = create_app(cfg=cfg, engine=StubEngine(), llm=llm,
                                 autostart=False, life=store)
                with TestClient(app) as client:
                    r = client.post("/api/files/scan", json={})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(body["items"][0]["title"], "项目评审")
                    self.assertEqual(body["schedule"][0]["title"], "项目评审")
                    self.assertEqual(body["files"][0]["name"], "meeting.txt")
                    self.assertNotIn("text", body["files"][0], "API 不回传资料原文")
            finally:
                store.close()



if __name__ == "__main__":
    unittest.main()
