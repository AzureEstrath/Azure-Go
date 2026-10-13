"""Coords 黄金样本比对：逐项对照 tests/golden/coords.json。"""

import json
import os
import unittest

from app import coords

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "coords.json")


def _load():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


class CoordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load()

    def test_size(self):
        self.assertEqual(coords.SIZE, self.data["SIZE"])

    def test_letters(self):
        for e in self.data["letters"]:
            self.assertEqual(
                coords.letter(e["i"]),
                e["letter"],
                "letter(%d): 期望 %r 实际 %r" % (e["i"], e["letter"], coords.letter(e["i"])),
            )

    def test_xy_to_gtp(self):
        for e in self.data["xy_to_gtp"]:
            got = coords.xy_to_gtp(e["x"], e["y"])
            self.assertEqual(
                got,
                e["gtp"],
                "xy_to_gtp(%d,%d): 期望 %r 实际 %r" % (e["x"], e["y"], e["gtp"], got),
            )

    def test_gtp_to_xy(self):
        for e in self.data["gtp_to_xy"]:
            got = coords.gtp_to_xy(e["gtp"])
            self.assertEqual(
                got,
                (e["x"], e["y"]),
                "gtp_to_xy(%r): 期望 (%d,%d) 实际 %r" % (e["gtp"], e["x"], e["y"], got),
            )

    def test_gtp_to_xy_special(self):
        for e in self.data["gtp_to_xy_special"]:
            got = coords.gtp_to_xy(e["gtp"])
            self.assertEqual(
                got,
                (e["x"], e["y"]),
                "gtp_to_xy(%r): 期望 (%d,%d) 实际 %r" % (e["gtp"], e["x"], e["y"], got),
            )

    def test_valid(self):
        for e in self.data["valid_samples"]:
            got = coords.valid(e["x"], e["y"])
            self.assertEqual(
                got,
                e["valid"],
                "valid(%d,%d): 期望 %r 实际 %r" % (e["x"], e["y"], e["valid"], got),
            )


if __name__ == "__main__":
    unittest.main()
