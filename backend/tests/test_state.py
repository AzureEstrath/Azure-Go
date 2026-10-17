"""GameState 黄金样本比对：逐项对照 tests/golden/state.json。

另含 v3.7.0 的两份「本局完整账」门卫：``move_notes``（逐手存档）与 ``losses``（真亏手）。
"""

import copy
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


class MoveNotesTest(unittest.TestCase):
    """A：本局逐手存档 ``move_notes`` —— 纯追加、不滚动、不截断。"""

    def test_not_rolling_and_not_truncated(self):
        """① 连记 40 条、每条 300 字 → 一条不少、一字不截。"""
        st = GameState()
        long_text = "长" * 300
        for i in range(1, 41):
            st.note_move(i, "aim", "A", long_text)
        self.assertEqual(len(st.move_notes), 40, "不滚动：40 条应全在")
        self.assertEqual(len(st.move_notes[0]["text"]), 300, "不截断：首条仍是 300 字")
        self.assertEqual(st.move_notes[0]["move_no"], 1)
        self.assertEqual(st.move_notes[-1]["move_no"], 40)

    def test_two_kinds_on_same_move(self):
        """② 同一手可以既记 'aim'（用意）又记 'review'（感想）。"""
        st = GameState()
        st.note_move(3, "aim", "A", "她这一手的用意")
        st.note_move(3, "review", "A", "她这一手的感想")
        self.assertEqual([m["kind"] for m in st.move_notes], ["aim", "review"])
        self.assertEqual([m["side"] for m in st.move_notes], ["A", "A"])
        self.assertEqual([m["move_no"] for m in st.move_notes], [3, 3])

    def test_element_shape(self):
        st = GameState()
        st.note_move(2, "review", "E", "玩家那手的点评原文")
        self.assertEqual(
            st.move_notes[0],
            {"move_no": 2, "kind": "review", "side": "E", "text": "玩家那手的点评原文"},
        )

    def test_reset_clears_move_notes(self):
        """③ ``reset()`` 清空逐手存档。"""
        st = GameState()
        st.note_move(1, "aim", "A", "用意")
        st.note_move(1, "review", "E", "点评")
        st.reset()
        self.assertEqual(st.move_notes, [])

    def test_snapshot_and_rollback_do_not_touch_move_notes(self):
        """④ 快照 / 回滚不涉及 ``move_notes``（它不是状态机的一部分，悔棋不清账）。"""
        st = GameState()
        st.add_move("B", "D4")
        st.note_move(1, "review", "E", "第一手点评")
        snap = st.make_snapshot()
        self.assertNotIn("move_notes", snap, "快照不带走逐手存档")
        self.assertNotIn("losses", snap, "快照不带走亏损手账")
        st.add_move("W", "Q16")
        st.note_move(2, "aim", "A", "第二手用意")
        # 模拟悔棋：从快照恢复的那几项里没有 move_notes ⇒ 存档不受回滚影响
        st.moves = copy.deepcopy(snap["moves"])
        st.move_seq = list(snap["move_seq"])
        self.assertEqual(len(st.move_notes), 2, "回滚不截断逐手存档")


if __name__ == "__main__":
    unittest.main()
