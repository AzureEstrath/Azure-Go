"""SGF 导入 / 导出（v3.11.5）：坐标、虚着、变体跳过、规则校验、注释往返。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sgf
from app.game_store import DEMO_RECORD, DEMO_MOVES


class SgfRoundTripTest(unittest.TestCase):
    def test_demo_round_trip_keeps_moves_notes_summary(self):
        text = sgf.to_sgf(DEMO_RECORD)
        self.assertTrue(text.startswith("(;GM[1]FF[4]"))
        self.assertIn("B[pd]", text)
        got = sgf.parse_sgf(text)
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(len(got["moves"]), len(DEMO_MOVES))
        self.assertEqual(got["moves"][0], ["black", "Q16"])
        self.assertEqual(got["moves"][1], ["white", "D4"])
        self.assertEqual(got["summary"], DEMO_RECORD["summary"])
        self.assertEqual(got["black"], "Estarth")
        self.assertEqual(got["white"], "Azure")
        notes = DEMO_RECORD["meta"]["notes"]
        self.assertEqual(len(got["notes"]), len(notes))
        for want, have in zip(notes, got["notes"]):
            self.assertEqual(have["move_no"], want["move_no"])
            self.assertEqual(have["side"], want["side"])
            self.assertEqual(have["text"], want["text"])

    def test_pass_round_trip(self):
        rec = {"moves": [["black", "Q16"], ["white", "pass"], ["black", "pass"]],
               "meta": {"black": "A", "white": "B"}, "summary": ""}
        text = sgf.to_sgf(rec)
        self.assertIn(";W[]", text)
        self.assertIn(";B[]", text)
        got = sgf.parse_sgf(text)
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["moves"], [["black", "Q16"], ["white", "pass"], ["black", "pass"]])

    def test_escape_in_summary_and_comment(self):
        rec = {"moves": [["black", "D4"]], "summary": "括号]与反斜杠\\都在",
               "meta": {"notes": [{"move_no": 1, "kind": "review", "side": "E",
                                   "text": "这里]要转义\\才行"}]}}
        got = sgf.parse_sgf(sgf.to_sgf(rec))
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["summary"], "括号]与反斜杠\\都在")
        self.assertEqual(got["notes"][0]["text"], "这里]要转义\\才行")


class SgfParseTest(unittest.TestCase):
    def test_coordinates(self):
        got = sgf.parse_sgf("(;GM[1]FF[4]SZ[19];B[pd];W[dp];B[aa];W[ss])")
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["moves"], [["black", "Q16"], ["white", "D4"],
                                        ["black", "A19"], ["white", "T1"]])

    def test_main_line_skips_variations(self):
        got = sgf.parse_sgf("(;SZ[19];B[pd](;W[dd];B[qp])(;W[qp]);W[pp])")
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["moves"], [["black", "Q16"], ["white", "Q4"]])

    def test_comment_only_node_attaches_to_last_move(self):
        got = sgf.parse_sgf("(;SZ[19];B[pd];W[dd];C[这手很稳])")
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(len(got["notes"]), 1)
        self.assertEqual(got["notes"][0]["move_no"], 2)
        self.assertEqual(got["notes"][0]["side"], "A")
        self.assertEqual(got["notes"][0]["text"], "这手很稳")

    def test_exported_prefix_restores_side(self):
        got = sgf.parse_sgf("(;SZ[19];B[pd]C[Azure：我来说一句];W[dd]C[你：我也说一句])")
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual([n["side"] for n in got["notes"]], ["A", "E"])
        self.assertEqual([n["text"] for n in got["notes"]], ["我来说一句", "我也说一句"])

    def test_illegal_move_rejected(self):
        got = sgf.parse_sgf("(;SZ[19];B[pd];W[pd])")
        self.assertFalse(got["ok"])
        self.assertIn("不合法", got["error"])

    def test_color_order_break_rejected(self):
        got = sgf.parse_sgf("(;SZ[19];B[pd];B[dd])")
        self.assertFalse(got["ok"])
        self.assertIn("手序", got["error"])

    def test_setup_stones_rejected(self):
        got = sgf.parse_sgf("(;SZ[19]AB[pd];W[dd])")
        self.assertFalse(got["ok"])
        self.assertIn("让子", got["error"])

    def test_non_19_board_rejected(self):
        got = sgf.parse_sgf("(;SZ[13];B[dd])")
        self.assertFalse(got["ok"])
        self.assertIn("19", got["error"])

    def test_empty_and_garbage_rejected(self):
        self.assertFalse(sgf.parse_sgf("")["ok"])
        self.assertFalse(sgf.parse_sgf("hello")["ok"])
        self.assertFalse(sgf.parse_sgf("(;SZ[19]")["ok"])

    def test_result_date_and_names(self):
        got = sgf.parse_sgf("(;SZ[19]GM[1]PB[甲]PW[乙]RE[B+R]DT[2026-10-22]GC[好棋];B[pd])")
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["black"], "甲")
        self.assertEqual(got["white"], "乙")
        self.assertEqual(got["result"], "B+R")
        self.assertEqual(got["date"], "2026-10-22")
        self.assertEqual(got["summary"], "好棋")


if __name__ == "__main__":
    unittest.main()
