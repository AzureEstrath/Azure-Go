"""GoBoard 黄金样本比对：逐项对照 tests/golden/board.json。"""

import json
import os
import unittest

from app.board import GoBoard

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "board.json")


def _load():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def _sorted_caps(caps):
    return sorted(tuple(c) for c in caps)


class GoBoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load()

    def _replay(self, sc):
        """按 moves 顺序落子，返回棋盘。"""
        board = GoBoard()
        for i, (x, y, color) in enumerate(sc["moves"]):
            board.play(x, y, color)
        return board

    def test_steps(self):
        for sc in self.data["scenarios"]:
            board = GoBoard()
            for i, (x, y, color) in enumerate(sc["moves"]):
                got = board.play(x, y, color)
                exp = sc["steps"][i]
                tag = "%s 第%d手 (%d,%d,%s)" % (sc["name"], i + 1, x, y, color)
                self.assertEqual(got["ok"], exp["ok"], "%s ok: 期望 %r 实际 %r" % (tag, exp["ok"], got["ok"]))
                self.assertEqual(got["err"], exp["err"], "%s err: 期望 %r 实际 %r" % (tag, exp["err"], got["err"]))
                self.assertEqual(
                    _sorted_caps(got["captured"]),
                    _sorted_caps(exp["captured"]),
                    "%s captured: 期望 %r 实际 %r" % (tag, _sorted_caps(exp["captured"]), _sorted_caps(got["captured"])),
                )

    def test_grid(self):
        for sc in self.data["scenarios"]:
            board = self._replay(sc)
            exp = [list(col) for col in sc["grid"]]
            self.assertEqual(board.grid, exp, "%s grid 不一致" % sc["name"])

    def test_ko_point(self):
        for sc in self.data["scenarios"]:
            board = self._replay(sc)
            exp = (sc["ko_point"]["x"], sc["ko_point"]["y"])
            self.assertEqual(board.ko_point, exp, "%s ko_point: 期望 %r 实际 %r" % (sc["name"], exp, board.ko_point))

    def test_count(self):
        for sc in self.data["scenarios"]:
            board = self._replay(sc)
            self.assertEqual(board.count(), sc["count"], "%s count 不一致" % sc["name"])

    def test_stones_list(self):
        for sc in self.data["scenarios"]:
            board = self._replay(sc)
            last = (sc["last_xy"]["x"], sc["last_xy"]["y"])
            got = board.stones_list(last)
            self.assertEqual(got, sc["stones_list"], "%s stones_list 不一致" % sc["name"])

    def test_is_legal(self):
        for sc in self.data["scenarios"]:
            board = self._replay(sc)
            for x in range(19):
                for y in range(19):
                    idx = x * 19 + y
                    for color, key in (("black", "legal_black"), ("white", "legal_white")):
                        exp = sc[key][idx]
                        got = board.is_legal(x, y, color)
                        tag = "%s is_legal(%d,%d,%s)" % (sc["name"], x, y, color)
                        self.assertEqual(got["ok"], exp["ok"], "%s ok: 期望 %r 实际 %r" % (tag, exp["ok"], got["ok"]))
                        self.assertEqual(got["err"], exp["err"], "%s err: 期望 %r 实际 %r" % (tag, exp["err"], got["err"]))


if __name__ == "__main__":
    unittest.main()
