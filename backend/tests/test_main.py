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
from app.llm import LLMClient
from app.main import create_app
from helpers import BoomLLM, ErrLLM, StubEngine, StubLLM


def _client():
    app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
    return TestClient(app)


class RestTest(unittest.TestCase):
    def test_health_state_and_move(self):
        with _client() as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(set(r.json()),
                             {"ok", "katago_dead", "katago_error", "katago_url", "llm_url"})

            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertFalse(body["busy"])
            self.assertEqual(body["state"]["move_count"], 2)   # 玩家一手 + 她一手

    def test_move_bad_body_no_500(self):
        """畸形/越界坐标不应打成 500，而是优雅返回 ok=false（C1）。"""
        with _client() as client:
            for body in ({"x": "abc", "y": 3}, {}, {"x": 99, "y": 3}, {"x": 3, "y": -5}):
                r = client.post("/api/move", json=body)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["ok"])
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

    def test_config_undo_reset(self):
        with _client() as client:
            self.assertTrue(client.post("/api/config", json={"allow_pass": True}).json()["ok"])
            client.post("/api/move", json={"x": 3, "y": 3})
            self.assertTrue(client.post("/api/reset").json()["ok"])
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

    def test_peer_endpoint_gone(self):
        with _client() as client:
            self.assertEqual(client.post("/api/peer", json={"fg_title": "x"}).status_code, 404)


class WebSocketTest(unittest.TestCase):
    def test_hello_on_connect(self):
        with _client() as client:
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertIn("state", hello)
                self.assertIn("mood", hello)
                self.assertIn("busy", hello)

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


class CloudFailRestTest(unittest.TestCase):
    """⚠️ 云端 LLM 不可用（未配置地址 / 断网 / 抛异常）时：REST 通道仍须正常应答、棋盘自洽。"""

    def test_move_ok_without_cloud_llm(self):
        for llm in (LLMClient(base_url=""), BoomLLM()):
            with TestClient(create_app(engine=StubEngine(), llm=llm, autostart=False)) as client:
                self.assertTrue(client.get("/api/health").json()["ok"])
                self.assertEqual(client.get("/api/state").json()["move_count"], 0)
                r = client.post("/api/move", json={"x": 3, "y": 3})
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertTrue(body["ok"])
                self.assertFalse(body["busy"])
                st = body["state"]
                self.assertEqual(len(st["stones"]), st["move_count"])   # 棋盘状态自洽
                self.assertGreaterEqual(st["move_count"], 1)

    def test_ws_never_pushes_error_text(self):
        """玩家可见文本里不许出现「调用出错」：点评与落子宣告都退化为兜底台词。"""
        for llm in (LLMClient(base_url=""), ErrLLM()):    # 未配置地址 / 401
            with TestClient(create_app(engine=StubEngine(), llm=llm, autostart=False)) as client:
                with client.websocket_connect("/ws") as ws:
                    self.assertEqual(ws.receive_json()["type"], "hello")
                    self.assertTrue(client.post("/api/move", json={"x": 3, "y": 3}).json()["ok"])

                    got = []
                    for _ in range(60):
                        e = ws.receive_json()
                        got.append(e)
                        if any(x.get("type") == "message" and x.get("kind") == events.AI_MOVE for x in got):
                            break

                    moved = [e for e in got if e.get("type") == "message" and e.get("kind") == events.AI_MOVE]
                    self.assertTrue(moved, "落子宣告必须照常（Review Focus #3）")
                    for e in got:
                        self.assertNotIn("调用出错", str(e.get("text", "")))


class AssemblyTest(unittest.TestCase):
    """装配接线：cfg 的扩展字段必须经 ``merge_extra_body`` 落到 LLMClient 与链上（Task 4 的接线点）。"""

    def test_cfg_extra_body_reaches_llm_client(self):
        from app.config import AzureConfig
        cfg = AzureConfig()
        cfg.llm_extra_body = {"top_k": 40}
        cfg.llm_think_mode = 2
        cfg.llm_think_extra_body = {"reasoning_effort": "high"}
        app = create_app(cfg=cfg, autostart=False)
        with TestClient(app):
            merged = {"top_k": 40, "reasoning_effort": "high"}
            self.assertEqual(app.state.agent.llm.extra_body, merged)
            self.assertEqual(app.state.agent.llm.chain.extra_body, merged)   # 链上也带着同一份


if __name__ == "__main__":
    unittest.main()
