"""日程 API：自动排班 / 留档 / 清理 / 手动提醒（临时 LifeStore，不触网）。"""

import os
import sys
import tempfile
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
                    row = [x for x in body["items"] if x["id"] == rid][0]
                    self.assertGreater(row["start_ts"], 0.0)
                    self.assertTrue(row["auto_planned"])

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


if __name__ == "__main__":
    unittest.main()
