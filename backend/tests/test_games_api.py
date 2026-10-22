"""棋谱库 REST 端点（v3.11.5）：列表（示例播种）/ 导入 / 读取 / 导出 SGF / 删除。"""

import asyncio
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
from helpers import StubEngine, StubLLM, mk_agent


SGF3 = "(;GM[1]FF[4]SZ[19]PB[甲]PW[乙]RE[W+1.5]GC[测试棋谱];B[pd];W[dd];B[qp])"


class GamesApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="games_api_")
        self.store = GameStore(os.path.join(self.dir, "games.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _client(self):
        app = create_app(cfg=AzureConfig(), engine=StubEngine(), llm=StubLLM(),
                         autostart=False, game_store=self.store)
        return TestClient(app)

    def test_list_seeds_demo(self):
        with self._client() as client:
            r = client.get("/api/games")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["games"]), 1)
            demo = data["games"][0]
            self.assertTrue(demo["demo"])
            self.assertEqual(demo["move_count"], 24)
            self.assertTrue(demo["date"])
            self.assertEqual(self.store.count(), 1)

    def test_import_get_export_delete(self):
        with self._client() as client:
            client.get("/api/games")                 # 先播种示例
            r = client.post("/api/games/import", json={"sgf": SGF3})
            self.assertEqual(r.status_code, 200, r.text)
            gid = r.json()["id"]
            self.assertGreater(gid, 0)

            rows = client.get("/api/games").json()["games"]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["id"], gid)      # 导入的这盘最新

            game = client.get("/api/games/%d" % gid).json()["game"]
            self.assertEqual(game["moves"], [["black", "Q16"], ["white", "D16"], ["black", "R4"]])
            self.assertEqual(game["result"], "W+1.5")
            self.assertEqual(game["summary"], "测试棋谱")
            self.assertEqual(game["meta"]["black"], "甲")
            self.assertEqual(game["meta"]["white"], "乙")

            sgf = client.get("/api/games/%d/sgf" % gid)
            self.assertEqual(sgf.status_code, 200)
            self.assertIn("B[pd]", sgf.text)
            self.assertIn("PB[甲]", sgf.text)
            self.assertIn("GC[测试棋谱]", sgf.text)

            self.assertEqual(client.delete("/api/games/%d" % gid).status_code, 200)
            self.assertEqual(client.get("/api/games/%d" % gid).status_code, 404)
            self.assertEqual(len(client.get("/api/games").json()["games"]), 1)

    def test_bad_import_returns_readable_error(self):
        with self._client() as client:
            r = client.post("/api/games/import", json={"sgf": "(;SZ[19];B[pd];W[pd])"})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.json()["ok"])
            self.assertIn("不合法", r.json()["error"])
            r2 = client.post("/api/games/import", json={"sgf": ""})
            self.assertEqual(r2.status_code, 400)
            self.assertEqual(self.store.count(), 0)

    def test_missing_game_404(self):
        with self._client() as client:
            self.assertEqual(client.get("/api/games/9999").status_code, 404)
            self.assertEqual(client.get("/api/games/9999/sgf").status_code, 404)
            self.assertEqual(client.delete("/api/games/9999").status_code, 404)


class LibraryArchiveTest(unittest.TestCase):
    """局末自动归档：`_persist_library` 写棋谱库，且同一局重复「结束」不重复写。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="library_archive_")
        self.store = GameStore(os.path.join(self.dir, "games.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_archive_once_per_game(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.game_store = self.store
        agent.player_name = "小明"
        agent.st.moves = [["black", "Q16"], ["white", "D4"]]
        agent.st.move_seq = ["Q16", "D4"]
        agent.st.move_notes = [{"move_no": 1, "kind": "review", "side": "E", "text": "这手不错"}]
        agent.st.losses = [{"move_no": 2, "gtp": "D4", "drop": 0.2, "reason": "亏了"}]
        state = {"data": {"wr": 0.42, "score": -1.5, "reply": "这局下得挺稳。", "failed": False}}

        asyncio.run(agent._persist_library(state))
        rows = self.store.list_games()
        self.assertEqual(len(rows), 1)
        rec = self.store.get_game(rows[0]["id"])
        self.assertEqual(rec["moves"], [["black", "Q16"], ["white", "D4"]])
        self.assertEqual(rec["summary"], "这局下得挺稳。")
        self.assertAlmostEqual(rec["final_black_wr"], 0.42)
        self.assertEqual(rec["meta"]["black"], "小明")
        self.assertEqual(rec["notes"][0]["text"], "这手不错")
        self.assertEqual(rec["losses"][0]["drop"], 0.2)

        asyncio.run(agent._persist_library(state))       # 同一局再点一次结束：不重复归档
        self.assertEqual(self.store.count(), 1)

        agent.st.moves.append(["black", "D16"])          # 继续下了一手再结束：新的一份
        asyncio.run(agent._persist_library(state))
        self.assertEqual(self.store.count(), 2)

    def test_failed_summary_still_archives_moves(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.game_store = self.store
        agent.st.moves = [["black", "Q16"]]
        asyncio.run(agent._persist_library(
            {"data": {"reply": "兜底台词", "failed": True}}))
        rec = self.store.get_game(self.store.list_games()[0]["id"])
        self.assertEqual(rec["moves"], [["black", "Q16"]])
        self.assertEqual(rec["summary"], "")             # 兜底台词不写进总结


if __name__ == "__main__":
    unittest.main()
