"""GameState 黄金样本比对：逐项对照 tests/golden/state.json。"""

import json
import os
import unittest

from app import coords
from app.state import GameState

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "state.json")


def _load():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


class GameStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load()

    def _build(self):
        """按 inputs 记录的构造步骤复原状态。"""
        d = self.data["inputs"]
        st = GameState()
        board_color = {"B": "black", "W": "white"}
        for color, gtp in d["moves"]:
            st.add_move(color, gtp)
            if gtp != "pass":
                x, y = coords.gtp_to_xy(gtp)
                st.board.play(x, y, board_color[color])
        for bm in d["bad_moves"]:
            st.record_bad_move(bm["turn_index"], bm["gtp"], bm["reason"])
        for m in d["memory_log"]:
            st.log_memory(m["tag"], m["text"], m["move_no"])
        st.allow_pass = d["allow_pass"]
        st.last_katago = d["last_katago"]
        return st

    def test_fields(self):
        st = self._build()
        d = self.data["inputs"]
        self.assertEqual(st.allow_pass, d["allow_pass"], "allow_pass 不一致")
        self.assertEqual(st.last_katago, d["last_katago"], "last_katago 不一致")
        self.assertEqual(st.moves, d["moves"], "moves 不一致")
        self.assertEqual(len(st.move_seq), len(d["moves"]), "move_seq 长度不一致")

    def test_last_played_xy(self):
        st = self._build()
        exp = (self.data["last_played_xy"]["x"], self.data["last_played_xy"]["y"])
        got = st.last_played_xy()
        self.assertEqual(got, exp, "last_played_xy: 期望 %r 实际 %r" % (exp, got))

    def test_payload(self):
        st = self._build()
        got = st.payload()
        exp = self.data["payload"]
        self.assertEqual(got, exp, "payload: 期望 %r 实际 %r" % (exp, got))

    def test_snapshot(self):
        st = self._build()
        snap = st.make_snapshot()
        exp = self.data["snapshot"]
        self.assertEqual(snap["moves"], exp["moves"], "snapshot.moves: 期望 %r 实际 %r" % (exp["moves"], snap["moves"]))
        self.assertEqual(
            snap["move_seq"], exp["move_seq"],
            "snapshot.move_seq: 期望 %r 实际 %r" % (exp["move_seq"], snap["move_seq"]),
        )
        self.assertEqual(
            snap["memory_log"], exp["memory_log"],
            "snapshot.memory_log: 期望 %r 实际 %r" % (exp["memory_log"], snap["memory_log"]),
        )
        self.assertEqual(
            snap["joseki_log"], exp["joseki_log"],
            "snapshot.joseki_log: 期望 %r 实际 %r" % (exp["joseki_log"], snap["joseki_log"]),
        )
        bexp = exp["board"]
        self.assertEqual(
            snap["board"].grid, bexp["grid"],
            "snapshot.board.grid 不一致",
        )
        ko_exp = (bexp["ko_point"]["x"], bexp["ko_point"]["y"])
        self.assertEqual(
            snap["board"].ko_point, ko_exp,
            "snapshot.board.ko_point: 期望 %r 实际 %r" % (ko_exp, snap["board"].ko_point),
        )

    def test_snapshot_board_is_clone(self):
        """快照棋盘应为独立副本，改动不影响主棋盘。"""
        st = self._build()
        snap = st.make_snapshot()
        snap["board"].play(0, 0, "black")
        self.assertEqual(st.board.grid[0][0], "", "snapshot 棋盘应为主棋盘的克隆")


if __name__ == "__main__":
    unittest.main()
