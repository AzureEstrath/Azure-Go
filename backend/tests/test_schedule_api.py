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


if __name__ == "__main__":
    unittest.main()
