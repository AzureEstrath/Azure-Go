"""棋谱库存储层（games.db）：增删查、示例局播种、脏值与降级。"""

import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game_store import DEMO_MOVES, GameStore


class GameStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="games_store_")
        self.store = GameStore(os.path.join(self.dir, "games.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _add(self, moves=None, ts=1000.0, summary="", meta=None):
        moves = moves or [["black", "Q16"], ["white", "D4"]]
        return self.store.add_game({
            "ts": ts, "moves": moves,
            "move_count": len(moves), "final_black_wr": 0.42,
            "final_score": -1.5, "result": "", "summary": summary,
            "meta": meta or {"black": "Estarth", "white": "Azure",
                             "notes": [{"move_no": 1, "kind": "review", "side": "E", "text": "好手"}],
                             "losses": []},
        })

    def test_add_list_get(self):
        gid = self._add(summary="第一盘")
        self.assertGreater(gid, 0)
        rows = self.store.list_games()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], gid)
        self.assertEqual(rows[0]["move_count"], 2)
        self.assertAlmostEqual(rows[0]["final_black_wr"], 0.42)
        self.assertEqual(rows[0]["summary"], "第一盘")
        self.assertNotIn("moves", rows[0])              # 列表不带整盘棋谱

        rec = self.store.get_game(gid)
        self.assertEqual(rec["moves"], [["black", "Q16"], ["white", "D4"]])
        self.assertEqual(rec["notes"][0]["text"], "好手")
        self.assertEqual(rec["meta"]["black"], "Estarth")

    def test_order_and_limit(self):
        a = self._add(ts=100.0, summary="旧")
        b = self._add(ts=200.0, summary="新")
        rows = self.store.list_games(limit=1)
        self.assertEqual([r["id"] for r in rows], [b])
        self.assertEqual([r["id"] for r in self.store.list_games()], [b, a])

    def test_delete(self):
        gid = self._add()
        self.assertTrue(self.store.delete_game(gid))
        self.assertFalse(self.store.delete_game(gid))
        self.assertIsNone(self.store.get_game(gid))
        self.assertEqual(self.store.count(), 0)

    def test_dirty_record_never_raises(self):
        self.assertEqual(self.store.add_game({}), 0)
        self.assertEqual(self.store.add_game({"moves": [["x", "Q16"], ["black", ""]]}), 1)
        rec = self.store.get_game(1)
        self.assertEqual(rec["moves"], [["black", "pass"]])   # 非法颜色被丢、空坐标按虚着存
        self.assertEqual(self.store.get_game("abc"), None)
        self.assertEqual(self.store.delete_game(None), False)

    def test_ensure_demo_seeds_once(self):
        self.assertGreater(self.store.ensure_demo(), 0)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.ensure_demo(), 0)          # 已有棋谱：不再塞
        rec = self.store.list_games()[0]
        self.assertTrue(rec["demo"])
        self.assertEqual(rec["move_count"], len(DEMO_MOVES))
        self.assertTrue(self.store.get_game(rec["id"])["demo"])
        self.store.delete_game(rec["id"])
        self.assertEqual(self.store.ensure_demo(), 0)           # 用户删掉示例：不复活
        self.assertEqual(self.store.count(), 0)

        fresh = GameStore(os.path.join(self.dir, "fresh.db"))
        self.assertGreater(fresh.ensure_demo(), 0)              # 新库：示例重新出现

    def test_nan_and_inf_never_poison_json(self):
        """NaN / inf 不许进库：它们会让 `/api/games` 的 JSON 序列化整页 500。"""
        gid = self.store.add_game({
            "ts": float("nan"), "moves": [["black", "Q16"]], "move_count": 1,
            "final_black_wr": float("nan"), "final_score": float("inf"),
        })
        self.assertGreater(gid, 0)
        row = self.store.list_games()[0]
        self.assertTrue(math.isfinite(row["ts"]))
        self.assertIsNone(row["final_black_wr"])
        self.assertIsNone(row["final_score"])
        rec = self.store.get_game(gid)
        self.assertIsNone(rec["final_black_wr"])
        self.assertIsNone(rec["final_score"])

    def test_ensure_demo_is_thread_safe(self):
        """并发首访（连点两次刷新）只许塞一盘示例：检查 → 写入在同一把锁里。"""
        import threading
        results = []
        barrier = threading.Barrier(6)

        def work():
            barrier.wait()
            results.append(self.store.ensure_demo())

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.store.count(), 1, "并发首访只能留下一盘示例")
        self.assertEqual(sum(1 for r in results if r > 0), 1, "只有真正写入的那一次返回 id")

    def test_fail_open_on_unusable_path(self):
        blocked = os.path.join(self.dir, "blocked")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("not a directory")
        store = GameStore(os.path.join(blocked, "games.db"))
        self.assertEqual(store.add_game({"moves": [["black", "Q16"]]}), 0)
        self.assertEqual(store.list_games(), [])
        self.assertIsNone(store.get_game(1))
        self.assertFalse(store.delete_game(1))
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.ensure_demo(), 0)


if __name__ == "__main__":
    unittest.main()
