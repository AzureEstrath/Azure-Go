"""日程 API：自动排班 / 留档 / 清理 / 手动提醒（临时 LifeStore，不触网）。"""

import datetime
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import events
from app.agent import AzureAgent
from app.config import AzureConfig
from app.life import LifeStore
from app.main import create_app
from helpers import StubEngine, StubLLM, ZeroRng


class SchedulePlanningApiTest(unittest.TestCase):
    def test_auto_plan_archive_cleanup_and_remind(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            saved = store.upsert_schedule([{
                "start_ts": 0.0, "when_text": "待定", "title": "写实验报告",
                "kind": "task", "priority": 90, "duration_min": 45, "source": "a.txt",
            }], source="a.txt")
            rid = saved[0]["id"]
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            bus = events.RecordingBus()
            agent = AzureAgent(StubEngine(), StubLLM(text="记得写实验报告。"), bus,
                               joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/schedule/auto_plan", json={"days": 7})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(len(body["planned"]), 1)
                    self.assertEqual(body["stats"]["grades"].get("flexible"), 1)
                    self.assertEqual(body["stats"]["align_min"], 30)
                    self.assertEqual(body["stats"]["transition_min"], 10)
                    row = [x for x in body["items"] if x["id"] == rid][0]
                    self.assertGreater(row["start_ts"], 0.0)
                    self.assertTrue(row["auto_planned"])
                    self.assertIn(row["grade"], ("locked", "tentative"))
                    self.assertIn(row["grade_label"], ("锁定中", "可调整"))

                    r = client.post("/api/schedule/remind", json={"id": rid})
                    self.assertTrue(r.json()["ok"])
                    self.assertIn("实验报告", r.json()["text"])
                    self.assertEqual(r.json()["text"], "记得写实验报告。")
                    # 提醒文本走 ai_remind 事件（不是普通台词 / 语音）
                    bus = app.state.agent.bus
                    msgs = bus.messages(events.AI_REMIND)
                    self.assertEqual(len(msgs), 1)

                    r = client.post("/api/schedule/archive", json={"ids": [rid]})
                    self.assertEqual(r.json()["archived"], 1)
                    self.assertEqual(client.get("/api/schedule").json()["items"], [])
                    arch = client.get("/api/schedule?include_archived=1").json()["items"]
                    self.assertEqual(arch[0]["id"], rid)
                    self.assertTrue(arch[0]["archived"])
                    self.assertEqual(arch[0]["grade"], "closed")

                    r = client.post("/api/schedule/cleanup", json={"days": 1})
                    self.assertTrue(r.json()["ok"])
                    self.assertEqual(r.json()["removed"], 0)
            finally:
                store.close()


class ScheduleFeedbackApiTest(unittest.TestCase):
    def test_feedback_updates_stats_and_deferred_replans(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            done_item = store.upsert_schedule([{
                "start_ts": 0.0, "when_text": "待定", "title": "复习第三章",
                "kind": "task", "priority": 80, "duration_min": 60,
                "task_type": "study", "source": "a.txt",
            }], source="a.txt")[0]
            defer_item = store.upsert_schedule([{
                "start_ts": 0.0, "when_text": "待定", "title": "写项目文档",
                "kind": "task", "priority": 60, "duration_min": 90,
                "task_type": "work", "source": "a.txt",
            }], source="a.txt")[0]
            skip_item = store.upsert_schedule([{
                "start_ts": 0.0, "when_text": "待定", "title": "缴费",
                "kind": "task", "priority": 40, "duration_min": 20,
                "task_type": "chore", "source": "a.txt",
            }], source="a.txt")[0]
            cfg = AzureConfig()
            app = create_app(cfg=cfg, agent=AzureAgent(
                StubEngine(), StubLLM(text="好。"), events.RecordingBus()),
                autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/schedule/feedback", json={
                        "id": done_item["id"], "outcome": "done", "actual_min": 42})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(body["item"]["status"], "done")
                    self.assertAlmostEqual(body["item"]["actual_min"], 42.0)
                    self.assertAlmostEqual(body["stats"]["dur_mean"], 54.0)  # (60*2+42)/3
                    self.assertEqual(body["stats"]["samples"], 1)

                    r = client.post("/api/schedule/feedback", json={
                        "id": defer_item["id"], "outcome": "deferred"})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertAlmostEqual(body["item"]["start_ts"], 0.0)
                    self.assertEqual(body["item"]["defer_count"], 1)
                    self.assertEqual(body["stats"]["defer_count"], 1)
                    self.assertEqual(body["stats"]["samples"], 0)
                    self.assertIsNotNone(body["replanned"])
                    self.assertGreaterEqual(body["replanned"]["planned"], 1)
                    rows = {x["id"]: x for x in body["items"]}
                    self.assertGreater(rows[defer_item["id"]]["start_ts"], 0.0)

                    r = client.post("/api/schedule/feedback", json={
                        "id": skip_item["id"], "outcome": "skipped"})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(body["item"]["status"], "cancelled")
                    self.assertEqual(body["item"]["outcome"], "skipped")

                    r = client.get("/api/schedule/review?days=7")
                    rev = r.json()
                    self.assertTrue(rev["ok"], rev)
                    self.assertIn("rows", rev)
                    rows = {x["task_type"]: x for x in rev["rows"]}
                    self.assertEqual(rows["study"]["done"], 1)
                    self.assertEqual(rows["work"]["deferred"], 1)
                    self.assertEqual(rows["chore"]["skipped"], 1)
                    self.assertIn("days", rev)

                    r = client.post("/api/schedule/feedback", json={"id": 99999})
                    self.assertFalse(r.json()["ok"])
            finally:
                store.close()


class _ChatScheduleLLM(StubLLM):
    """聊天回复与日程判断共用一次装配：按提示词认领返回不同文本。"""

    def __init__(self, chat_text="好，我记着。", schedule_text=""):
        super().__init__(text=chat_text)
        self.schedule_text = schedule_text

    async def chat(self, messages, max_tokens=300, no_think=False, think_mode=None):
        self.calls.append((list(messages), max_tokens, no_think))
        self.think_modes.append(think_mode)
        prompt = str(messages[-1].get("content", "")) if messages else ""
        if "日程判断器" in prompt:
            return self.schedule_text
        return self.text


class ChatScheduleApiTest(unittest.TestCase):
    def _wait_items(self, store, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            items = store.list_schedule(50)
            if items:
                return items
            time.sleep(0.05)
        return store.list_schedule(50)

    def _wait_messages(self, bus, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            msgs = bus.messages(events.AI_SCHEDULE)
            if msgs:
                return msgs
            time.sleep(0.05)
        return bus.messages(events.AI_SCHEDULE)

    def test_chat_schedules_and_emits_ai_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            llm = _ChatScheduleLLM(schedule_text=(
                '{"schedule": true, "items": ['
                '{"date": "2026-10-30", "time": "15:00", "title": "组会",'
                ' "duration_min": 60}]}'))
            bus = events.RecordingBus()
            agent = AzureAgent(StubEngine(), llm, bus, joseki_rate=1.0,
                               joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat",
                                    json={"text": "周五下午三点提醒我组会", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    items = self._wait_items(store)
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["title"], "组会")
                    self.assertEqual(items[0]["start_time"], "15:00")
                    self.assertTrue(any("组会" in str(c[0]) for c in llm.calls
                                        if c and c[0]))
                    msgs = self._wait_messages(bus)
                    self.assertEqual(len(msgs), 1)
                    self.assertIn("组会", msgs[0]["text"])
                    self.assertIn("提醒", msgs[0]["text"])
            finally:
                store.close()

    def test_chat_cancel_removes_takeout_from_schedule(self):
        """v4.2.7：先说“外卖提醒我去拿”，再改口“不吃了/退掉了”，必须从日程里撤掉。"""
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            schedule_text = (
                '{"schedule": true, "items": ['
                '{"date": "%s", "time": "23:00", "title": "去拿外卖", "duration_min": 60}]}'
                % tomorrow)
            llm = _ChatScheduleLLM(chat_text="好，我记着。", schedule_text=schedule_text)
            bus = events.RecordingBus()
            agent = AzureAgent(StubEngine(), llm, bus, joseki_rate=1.0,
                               joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat", json={
                        "text": "明天晚上23点外卖送到，提醒我去拿", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    scheduled = self._wait_items(store)
                    self.assertEqual(len(scheduled), 1)
                    sid = int(scheduled[0]["id"])
                    self.assertEqual(scheduled[0]["title"], "去拿外卖")

                    r = client.post("/api/chat", json={
                        "text": "我不吃了，已经退掉了", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    end = time.time() + 3.0
                    while time.time() < end:
                        if sid not in [int(x["id"]) for x in store.list_schedule(50)]:
                            break
                        time.sleep(0.05)
                    self.assertNotIn(sid, [int(x["id"]) for x in store.list_schedule(50)])
                    row = store.get_schedule_item(sid)
                    self.assertEqual(row.get("status"), "cancelled")
                    msgs = self._wait_messages(bus)
                    self.assertTrue(any("撤掉" in str(m.get("text", "")) for m in msgs))
            finally:
                store.close()

    def test_plain_chat_does_not_touch_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            llm = _ChatScheduleLLM(schedule_text='{"schedule": false}')
            agent = AzureAgent(StubEngine(), llm, events.RecordingBus(),
                               joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat", json={"text": "有点累", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    time.sleep(0.3)
                    self.assertEqual(store.list_schedule(50), [])
            finally:
                store.close()

    def test_disabled_switch_skips_schedule_judgement(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            cfg.schedule_chat_enabled = False
            llm = _ChatScheduleLLM(schedule_text=(
                '{"schedule": true, "items": [{"title": "组会"}]}'))
            agent = AzureAgent(StubEngine(), llm, events.RecordingBus(),
                               joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat",
                                    json={"text": "周五下午三点提醒我组会", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    time.sleep(0.3)
                    self.assertEqual(store.list_schedule(50), [])
                    self.assertFalse(any("日程判断器" in str(c[0]) for c in llm.calls
                                         if c and c[0]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
