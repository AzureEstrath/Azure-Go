"""FastAPI 装配冒烟测试（stub 引擎/LLM，不触网、不起子进程）。

覆盖 REST 命令通道与 WS 事件通道的接线：端点返回形状、WS 首帧 hello、
以及一次落子能把事件推给 WS 订阅者。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import events
from app.main import create_app
from test_agent import StubEngine, StubLLM


def _client():
    app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
    return TestClient(app)


class RestTest(unittest.TestCase):
    def test_health_state_and_move(self):
        with _client() as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertFalse(body["busy"])
            self.assertEqual(body["state"]["move_count"], 2)   # 玩家一手 + 她一手

    def test_config_undo_reset(self):
        with _client() as client:
            self.assertTrue(client.post("/api/config", json={"allow_pass": True}).json()["ok"])
            client.post("/api/move", json={"x": 3, "y": 3})
            self.assertTrue(client.post("/api/reset").json()["ok"])
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)


class WebSocketTest(unittest.TestCase):
    def test_hello_on_connect(self):
        with _client() as client:
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertIn("state", hello)
                self.assertIn("mood", hello)

    def test_events_pushed_to_subscriber(self):
        with _client() as client:
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "hello")

                r = client.post("/api/move", json={"x": 3, "y": 3})
                self.assertTrue(r.json()["ok"])

                got = []
                for _ in range(60):
                    e = ws.receive_json()
                    got.append(e)
                    if any(x.get("type") == "message" and x.get("kind") == events.AI_MOVE for x in got):
                        break

                types = {e["type"] for e in got}
                self.assertIn("state", types)
                self.assertIn("busy", types)
                moved = [e for e in got if e.get("type") == "message" and e.get("kind") == events.AI_MOVE]
                self.assertTrue(moved)
                self.assertIn("move", moved[0])


if __name__ == "__main__":
    unittest.main()
