"""全盘复盘端点（v3.11.5）：分段分析、强思考点评、WS 推送、可读错误。"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app.config import AzureConfig
from app.game_store import GameStore
from app.main import create_app
from helpers import StubLLM

MOVES = [["black", "Q16"], ["white", "D4"], ["black", "D16"],
         ["white", "Q4"], ["black", "R14"], ["white", "F17"]]


class ApiStepEngine:
    async def analyze(self, moves, max_visits=None):
        black = sum(1 for m in moves if str(m[0]).lower() in ("b", "black"))
        target = 0.5 - 0.05 * black
        side_wr = target if len(moves) % 2 == 0 else 1.0 - target
        return {"rootInfo": {"winrate": side_wr, "scoreLead": 0.0},
                "moveInfos": [], "ownership": [0.0] * 361}


class FullReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="full_review_api_")
        self.store = GameStore(os.path.join(self.dir, "games.db"))
        self.gid = self.store.add_game({
            "moves": MOVES, "move_count": len(MOVES), "summary": "测试复盘",
            "meta": {"black": "玩家", "white": "Azure",
                     "notes": [{"move_no": 2, "kind": "review", "side": "E",
                                "text": "这手有点急"}]},
        })

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _app(self, llm=None):
        return create_app(cfg=AzureConfig(), engine=ApiStepEngine(),
                          llm=llm or StubLLM("这一段{笑}你走得挺稳。"),
                          autostart=False, game_store=self.store)

    def test_segment_returns_analysis_and_pushes_message(self):
        app = self._app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "hello")
                r = client.post("/api/review/full/segment",
                                json={"game_id": self.gid, "from": 0, "to": 4,
                                      "index": 0, "total": 2})
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["from"], 0)
                self.assertEqual(body["to"], 4)
                self.assertEqual(len(body["rows"]), 4)
                self.assertTrue(body["engine_ok"])
                self.assertIn("{笑}", body["text"])
                self.assertEqual(app.state.agent.llm.think_modes, [2])

                e = ws.receive_json()
                for _ in range(20):
                    if e.get("type") == "message" and "{笑}" in str(e.get("text", "")):
                        break
                    e = ws.receive_json()
                self.assertEqual(e["type"], "message")
                self.assertIn("{笑}", e["text"])

    def test_busy_and_bad_ranges_are_readable(self):
        app = self._app()
        with TestClient(app) as client:
            app.state.agent.busy = True
            r = client.post("/api/review/full/segment",
                            json={"game_id": self.gid, "from": 0, "to": 4})
            self.assertFalse(r.json()["ok"])
            self.assertTrue(r.json().get("busy"))
            app.state.agent.busy = False

            self.assertEqual(client.post("/api/review/full/segment",
                                         json={"game_id": 9999, "from": 0, "to": 4}).status_code, 404)
            self.assertEqual(client.post("/api/review/full/segment",
                                         json={"game_id": self.gid, "from": 4, "to": 4}).status_code, 400)

    def test_llm_failure_uses_fallback(self):
        from helpers import ErrLLM
        app = self._app(ErrLLM())
        with TestClient(app) as client:
            r = client.post("/api/review/full/segment",
                            json={"game_id": self.gid, "from": 0, "to": 6,
                                  "index": 0, "total": 1})
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["text"])
            self.assertNotIn("调用出错", body["text"])


if __name__ == "__main__":
    unittest.main()
