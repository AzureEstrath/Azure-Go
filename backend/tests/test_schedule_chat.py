"""聊天 → 日程判断器（纯逻辑 + StubLLM，不触网）。"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import schedule_chat
from helpers import StubLLM

NOW = datetime.datetime(2026, 10, 28, 10, 0).timestamp()


class GateTest(unittest.IsolatedAsyncioTestCase):
    def test_plain_chat_skips_llm(self):
        for text in ("有点累", "你喜欢什么颜色", "陪我聊会儿"):
            self.assertFalse(schedule_chat.looks_schedulable(text), text)

    def test_time_or_task_cue_is_a_candidate(self):
        for text in ("明天下午三点开会", "周五之前要交报告", "提醒我买牛奶",
                     "下周三答辩", "晚上八点半健身"):
            self.assertTrue(schedule_chat.looks_schedulable(text), text)

    async def test_extract_skips_gate_without_calling_llm(self):
        llm = StubLLM(text='{"schedule": true, "items": [{"title": "随便"}]}')
        self.assertEqual(await schedule_chat.extract(llm, "有点累", now=NOW), [])
        self.assertEqual(len(llm.calls), 0)


class ExtractTest(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_false_creates_nothing(self):
        llm = StubLLM(text='{"schedule": false}')
        self.assertEqual(await schedule_chat.extract(llm, "明天好忙", now=NOW), [])
        self.assertEqual(len(llm.calls), 1)

    async def test_timed_item_is_normalized(self):
        llm = StubLLM(text=(
            '```json\n{"schedule": true, "items": ['
            '{"date": "2026-10-28", "time": "15:30", "title": "评审",'
            ' "location": "会议室", "priority": 80, "duration_min": 60}]}\n```'))
        items = await schedule_chat.extract(llm, "今天下午三点半有评审", now=NOW)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["title"], "评审")
        self.assertEqual(it["start_time"], "15:30")
        self.assertEqual(it["location"], "会议室")
        self.assertEqual(it["priority"], 80)
        self.assertEqual(it["kind"], "task")
        self.assertGreater(it["start_ts"], 0.0)

    async def test_bare_array_is_accepted(self):
        llm = StubLLM(text='[{"time": "18:00", "title": "取快递"}]')
        items = await schedule_chat.extract(llm, "晚上六点取快递", now=NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "取快递")

    async def test_schedule_list_shorthand_is_accepted(self):
        llm = StubLLM(text='{"schedule": [{"time": "18:00", "title": "取快递"}]}')
        items = await schedule_chat.extract(llm, "晚上六点取快递", now=NOW)
        self.assertEqual(len(items), 1)

    async def test_invalid_output_is_silent(self):
        for out in ("嗯~ 好的呀。", "", "```json\n{", "调用出错：HTTP 401"):
            llm = StubLLM(text=out)
            self.assertEqual(
                await schedule_chat.extract(llm, "明天下午三点开会", now=NOW), [], out)

    async def test_past_event_is_dropped(self):
        llm = StubLLM(text=(
            '{"schedule": true, "items": ['
            '{"date": "2026-10-27", "time": "15:00", "title": "昨天的会"}]}'))
        self.assertEqual(
            await schedule_chat.extract(llm, "昨天下午三点开会", now=NOW), [])

    async def test_undated_task_is_kept(self):
        llm = StubLLM(text='{"schedule": true, "items": [{"title": "买牛奶"}]}')
        items = await schedule_chat.extract(llm, "提醒我买牛奶", now=NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["when_text"], "待定")


class ConfirmationTest(unittest.TestCase):
    def test_timed_item_mentions_time_and_reminder(self):
        line = schedule_chat.confirmation([{
            "title": "评审", "when_text": "2026-10-28 周三 15:30-16:30"}])
        self.assertIn("评审", line)
        self.assertIn("10月28日", line)
        self.assertIn("提醒", line)
        self.assertNotIn("2026-", line)

    def test_undated_item_goes_to_todo(self):
        line = schedule_chat.confirmation([{"title": "买牛奶", "when_text": "待定"}])
        self.assertIn("买牛奶", line)
        self.assertIn("待办", line)

    def test_multiple_items_are_counted(self):
        line = schedule_chat.confirmation([
            {"title": "评审", "when_text": "2026-10-28 15:30"},
            {"title": "买牛奶", "when_text": "待定"},
        ])
        self.assertIn("2 件", line)
        self.assertIn("待办", line)

    def test_empty_items_returns_empty(self):
        self.assertEqual(schedule_chat.confirmation([]), "")
        self.assertEqual(schedule_chat.confirmation([{"title": ""}]), "")


if __name__ == "__main__":
    unittest.main()
