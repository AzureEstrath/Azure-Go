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

    def test_relative_date_wins_over_llm_absolute_date(self):
        """v4.3.1：LLM 只给原话日期短语时，绝对日期由 Python 规则算（不让模型心算）。"""
        items = schedule.normalize_items([
            # 下周三的“周三”在周二看是明天，但“下周”必须落到下一周
            {"relative_date": "下周三", "time": "15:00", "title": "组会",
             "date": "2026-10-28"},
        ], source="chat", now=NOW)
        self.assertEqual(items[0]["when_text"], "2026-11-04 15:00")

    def test_day_part_disambiguates_ambiguous_hour(self):
        items = schedule.normalize_items([
            {"relative_date": "明天", "day_part": "晚上", "time": "8:00", "title": "电话"},
            {"relative_date": "明天", "day_part": "早上", "time": "8:00", "title": "晨跑"},
        ], source="chat", now=NOW)
        by_title = {x["title"]: x for x in items}
        self.assertEqual(by_title["电话"]["when_text"], "2026-10-28 20:00")
        self.assertEqual(by_title["晨跑"]["when_text"], "2026-10-28 08:00")

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

    def test_scan_text_skips_structured_course_table(self):
        """课程表已由坐标还原并单独入库，不再整表喂 LLM（否则会挤掉聊天公告）。"""
        scan = {"files": [
            {"name": "table.jpg", "kind": "image", "ocr": "高等数学 第1-2节",
             "table": [{"title": "高等数学", "period_start": 1}]},
            {"name": "chat.jpg", "kind": "image", "table": [],
             "ocr": "周四(10月29日） | 院系杯男子篮球赛 | 中午12：00-1：00 | 地点：篮球场"},
        ]}
        text = schedule.scan_text(scan)
        self.assertNotIn("高等数学", text)
        self.assertIn("院系杯男子篮球赛", text)
        self.assertIn("篮球场", text)

    def test_chat_announcement_fallback(self):
        """本地模型超时 / 漏掉聊天截图时，规则补抽仍要留下明确活动。"""
        scan = {"files": [{
            "name": "chat.jpg", "kind": "image", "table": [],
            "ocr": "23:23 | 体育委员许宇晨 | 周四(10月29日） | 院系杯男子篮球赛 | "
                   "中午12：00-1：00 | A队 VS B队 | 地点：篮球场 | "
                   "各位同学可以前往比赛现场观赛并 | 签到签退，将会获得二课堂加分， | "
                   "25，26级同学可获得0.2综素加分～",
        }]}
        raws = schedule.fallback_event_items(scan, now=NOW)
        self.assertEqual(len(raws), 1, raws)
        raw = raws[0]
        self.assertEqual(raw["title"], "院系杯男子篮球赛")
        self.assertEqual(raw["date"], "2026-10-29")
        self.assertEqual(raw["time"], "12:00")
        self.assertEqual(raw["end_time"], "13:00")
        self.assertEqual(raw["duration_min"], 60)
        self.assertEqual(raw["location"], "篮球场")
        self.assertIn("A队 VS B队", raw["notes"])

        items = schedule.normalize_items(raws, source="chat.jpg", now=NOW)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["kind"], "task")
        self.assertEqual(it["date"], "2026-10-29")
        self.assertEqual(it["when_text"], "2026-10-29 12:00-13:00")
        self.assertEqual(it["location"], "篮球场")
        self.assertEqual(it["task_type"], "exercise")

    def test_fallback_reads_wrapped_exam_notice(self):
        """v4.5.4：截图把「考试时间 / 地点」拆成多行时，仍要拼回标题、时间与地点。"""
        scan = {"files": [{
            "name": "exam.jpg", "kind": "image", "table": [],
            "ocr": "16:24 | 97 | 第77期党校初级班全体成员群(158） | 李姿睿 | "
                   "【结业考试通知】 | 各位77期党校初级班本科生、研究 | 生： | "
                   "77期党校初级班结业考试安排如 | 下: | 【考核形式】：线下闭卷 | "
                   "【考试时间）：11月8日（周 | 日）晚19：00-20：30。 | "
                   "【考试地点】：二教101-105 | 【考试范围】：入党誓词、党 | "
                   "章、党史、长征胜利九十周年相关内容 | 【注意事项】：请所有同学携带 | "
                   "好学生证或者身份证以备核查。",
        }]}
        raws = schedule.fallback_event_items(scan, now=NOW)
        self.assertEqual(len(raws), 1, raws)
        raw = raws[0]
        self.assertEqual(raw["title"], "结业考试通知")
        self.assertEqual(raw["date"], "2026-11-08")
        self.assertEqual(raw["time"], "19:00")
        self.assertEqual(raw["end_time"], "20:30")
        self.assertEqual(raw["location"], "二教101-105")
        self.assertIn("线下闭卷", raw["notes"])

    def test_fallback_reads_practice_notice_and_skips_past(self):
        """实践活动公告：标题从「日期前最后一句」重建；已经过去的公告不再新建。"""
        scan = {"files": [{
            "name": "practice.jpg", "kind": "image", "table": [],
            "ocr": "各位本科生小组长： | 大家好！ | 77期党校初级班实践活动第 | "
                   "一次审核将于10月31日（本周 | 六）晚18：00在森工楼316举 | 行。 | "
                   "各小组答辩到场时间如下： | 第一小组18:00 | 第九小组19:20 | "
                   "请大家提前做好准备，按时 | 定位到聊天位置>",
        }]}
        raws = schedule.fallback_event_items(scan, now=NOW)
        self.assertEqual(len(raws), 1, raws)
        raw = raws[0]
        self.assertEqual(raw["title"], "77期党校初级班实践活动第一次审核")
        self.assertEqual(raw["date"], "2026-10-31")
        self.assertEqual(raw["time"], "18:00")
        self.assertEqual(raw["location"], "森工楼316")
        # 换到活动当天之后再扫同一张图：过期公告不写。
        later = schedule.fallback_event_items(
            scan, now=datetime.datetime(2026, 11, 1, 10, 0).timestamp())
        self.assertEqual(later, [])

    def test_fallback_day_part_event(self):
        """只有「11月5日下午」没有钟点时：用下午默认窗口，并保留展览标题与地点。"""
        scan = {"files": [{
            "name": "exhibition.jpg", "kind": "image", "table": [],
            "ocr": "安安顺顺 | 【长征主题展览参观报名】 | 我校拟于11月5日下午到中国 | "
                   "人民革命军事博物馆，参观“伟大的远征——纪 | "
                   "念中国工农红军长征胜利90周年主题展览”。现以党支 | "
                   "部为单位开放参观报名，学校统一 | 安排往返交通。",
        }]}
        raws = schedule.fallback_event_items(scan, now=NOW)
        self.assertEqual(len(raws), 1, raws)
        raw = raws[0]
        self.assertEqual(raw["title"], "长征主题展览参观报名")
        self.assertEqual(raw["date"], "2026-11-05")
        self.assertEqual(raw["time"], "14:00")
        self.assertEqual(raw["end_time"], "17:00")
        self.assertEqual(raw["location"], "中国人民革命军事博物馆")

    def test_merge_fallback_merges_rewritten_title(self):
        """LLM 与规则补抽对同一场考试用了不同说法：同日期 + 同时刻 + 共同关键词 → 一条。"""
        fb = [{"date": "2026-11-08", "time": "19:00", "end_time": "20:30",
               "title": "结业考试通知", "location": "二教101-105"}]
        llm_raw = [{"date": "2026-11-08", "relative_date": "11月8日",
                    "time": "19:00", "end_time": "20:30",
                    "title": "77期党校初级班结业考试", "location": "二教101-105"}]
        merged = schedule.merge_event_fallback(llm_raw, fb)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "77期党校初级班结业考试")

    def test_merge_fallback_fills_missing_daypart_time(self):
        """LLM 只写「下午」时，补抽读出的 14:00-17:00 与地点要补进同一条。"""
        fb = [{"date": "2026-11-05", "time": "14:00", "end_time": "17:00",
               "title": "长征主题展览参观报名", "location": "中国人民革命军事博物馆"}]
        llm_raw = [{"date": "2026-11-05", "day_part": "下午",
                    "title": "长征主题展览参观报名"}]
        merged = schedule.merge_event_fallback(llm_raw, fb)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["time"], "14:00")
        self.assertEqual(merged[0]["end_time"], "17:00")
        self.assertEqual(merged[0]["location"], "中国人民革命军事博物馆")

    def test_drop_past_events_keeps_courses(self):
        """旧公告不再新建；课程表条目（包括过去几天的课）照常保留。"""
        now = datetime.datetime(2026, 11, 1, 10, 0).timestamp()
        items = [
            {"kind": "task", "start_ts": now - 86400.0, "title": "昨天的活动",
             "start_time": "18:00", "date": "2026-10-31"},
            {"kind": "task", "start_ts": 0.0, "title": "昨天只有日期",
             "date": "2026-10-31"},
            {"kind": "task", "start_ts": now + 86400.0, "title": "明天的活动",
             "start_time": "18:00", "date": "2026-11-02"},
            {"kind": "course", "start_ts": now - 86400.0, "title": "高等数学",
             "start_time": "08:00", "date": "2026-10-31"},
        ]
        kept = schedule.drop_past_events(items, now=now)
        self.assertEqual([x["title"] for x in kept], ["明天的活动", "高等数学"])

    def test_reconcile_source_dates_uses_explicit_date(self):
        """模型把「10月31日（本周六）」算成下一个周六时，按源文件的明确日期改回来。"""
        scan = {"files": [{
            "name": "practice.jpg", "kind": "image", "table": [],
            "ocr": "77期党校初级班实践活动第 | 一次审核将于10月31日（本周 | "
                   "六）晚18：00在森工楼316举 | 行。",
        }]}
        items = [{"title": "77期党校初级班实践活动第一次审核",
                  "date": "2026-11-07", "time": "18:00"}]
        got = schedule.reconcile_source_dates(items, scan, now=NOW)
        self.assertEqual(got[0]["date"], "2026-10-31")

    def test_collapse_group_items_merges_subtimes(self):
        """第一小组…第九小组的到场时间合并成一条主活动，分段时间进备注。"""
        rows = [
            {"title": "77期党校初级班实践活动第一次审核（第%s小组答辩）" % cn,
             "date": "2026-11-07", "time": tm, "location": "森工楼316"}
            for cn, tm in zip("一二三四五六七八九",
                              ["18:00", "18:10", "18:20", "18:30", "18:40",
                               "18:50", "19:00", "19:10", "19:20"])
        ]
        got = schedule.collapse_group_items(rows)
        self.assertEqual(len(got), 1, got)
        it = got[0]
        self.assertEqual(it["title"], "77期党校初级班实践活动第一次审核")
        self.assertEqual(it["time"], "18:00")
        self.assertEqual(it["end_time"], "19:30")
        self.assertIn("第一小组 18:00", it["notes"])
        self.assertIn("第九小组 19:20", it["notes"])

    def test_merge_fallback_uses_concise_announcement_title(self):
        """LLM 标题是一长段引文时，用规则补抽的短公告标题。"""
        fb = [{"date": "2026-11-05", "time": "14:00", "title": "长征主题展览参观报名"}]
        llm_raw = [{"date": "2026-11-05", "title":
                    "参观‘伟大纪念中国工农红军长征的胜利90周年主题展览’"}]
        merged = schedule.merge_event_fallback(llm_raw, fb)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "长征主题展览参观报名")

    def test_drop_past_source_items_uses_announcement_date(self):
        """LLM 把「10月31日（本周六）」算成 11-07 时，源公告的过期日期仍要挡住它；
        同一群的另一条未来考试不能被误伤。"""
        scan = {"files": [
            {"name": "practice.jpg", "kind": "image", "table": [],
             "ocr": "77期党校初级班实践活动第 | 一次审核将于10月31日（本周 | "
                    "六）晚18：00在森工楼316举 | 行。"},
            {"name": "exam.jpg", "kind": "image", "table": [],
             "ocr": "第77期党校初级班全体成员群(158） | 【结业考试通知】 | "
                    "77期党校初级班结业考试安排如 | 下: | 【考试时间】：11月8日（周 | "
                    "日）晚19：00-20：30。 | 【考试地点】：二教101-105"},
        ]}
        now = datetime.datetime(2026, 11, 1, 10, 0).timestamp()
        items = [
            {"title": "77期党校初级班实践活动第一次审核", "date": "2026-11-07",
             "time": "18:00", "kind": "task"},
            {"title": "77期党校初级班结业考试", "date": "2026-11-08",
             "time": "19:00", "kind": "task"},
        ]
        kept = schedule.drop_past_source_items(items, scan, now=now)
        self.assertEqual([x["title"] for x in kept], ["77期党校初级班结业考试"])

    def test_fallback_ignores_plain_chat(self):
        scan = {"files": [{
            "name": "chat.jpg", "kind": "image", "table": [],
            "ocr": "周四中午12：00-1：00 一起去吃饭吧",
        }]}
        self.assertEqual(schedule.fallback_event_items(scan, now=NOW), [])

    def test_merge_fallback_skips_llm_duplicate(self):
        fb = [{"date": "2026-10-29", "time": "12:00", "title": "院系杯男子篮球赛"}]
        llm_raw = [{"date": "2026-10-29", "time": "12:00",
                    "title": "院系杯男子篮球赛", "location": "篮球场"}]
        merged = schedule.merge_event_fallback(llm_raw, fb)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["location"], "篮球场")

    def test_weekday_event_without_period_is_task(self):
        items = schedule.normalize_items([
            {"date": "2026-10-29", "weekday": "周四", "time": "12:00",
             "end_time": "13:00", "title": "院系杯男子篮球赛", "location": "篮球场",
             "kind": "course"},
        ], source="chat.jpg", now=NOW)
        self.assertEqual(items[0]["kind"], "task")

    def test_course_with_period_stays_course(self):
        items = schedule.normalize_items([
            {"date": "2026-10-29", "weekday": "周四", "period": "1-2",
             "title": "高等数学", "kind": "course"},
        ], source="table.jpg", now=NOW)
        self.assertEqual(items[0]["kind"], "course")

    def test_course_items_keep_weekday_period_teacher_location(self):
        """课程表条目：节次跨两节写 1-2，教师/地点/日期都要留下（前端画周课表用）。"""
        items = schedule.normalize_items([
            {"date": "2026-10-28", "weekday": "周三", "period": "第1-2节",
             "time": "08:00", "end_time": "09:35", "title": "高等数学(基础)",
             "teacher": "张老师", "location": "综合楼101"},
        ], source="课表.png", now=NOW)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["weekday"], 2)
        self.assertEqual(it["period"], "1-2")
        self.assertEqual(it["period_start"], 1)
        self.assertEqual(it["period_end"], 2)
        self.assertEqual(it["start_time"], "08:00")
        self.assertEqual(it["end_time"], "09:35")
        self.assertEqual(it["teacher"], "张老师")
        self.assertEqual(it["location"], "综合楼101")
        self.assertEqual(it["date"], "2026-10-28")
        self.assertIn("周三", it["when_text"])
        self.assertIn("第1-2节", it["when_text"])
        self.assertIn("张老师", schedule.memory_text(it))
        self.assertIn("@综合楼101", schedule.memory_text(it))

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
            future = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
            with open(os.path.join(inbox, "meeting.txt"), "w", encoding="utf-8") as f:
                f.write("%s 15:30 项目评审，带上电脑。" % future)
            store = life.LifeStore(os.path.join(tmp, "life.db"), now=NOW)
            try:
                cfg = AzureConfig()
                cfg.assistant_inbox = inbox
                cfg.vlm_auto_start = False
                llm = StubLLM(text='[{"date":"%s","time":"15:30",'
                                  '"title":"项目评审","notes":"带电脑"}]' % future)
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
