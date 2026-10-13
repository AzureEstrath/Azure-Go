"""JosekiData / JosekiRecognizer 黄金样本比对：逐项对照 tests/golden/joseki.json。"""

import json
import os
import unittest

from app.joseki import JosekiData, JosekiRecognizer

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "joseki.json")


def _load():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def _as_json(res):
    """把识别结果里的 Vector2i（Python 元组）转成 JSON 的 {"x","y"} 形状。"""
    out = dict(res)
    if "points" in out:
        out["points"] = [{"x": p[0], "y": p[1]} for p in out["points"]]
    if "expect" in out:
        e = out["expect"]
        out["expect"] = {"x": e[0], "y": e[1]}
    return out


class JosekiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load()

    def test_list(self):
        self.assertEqual(
            JosekiData.LIST, self.data["JosekiData_LIST"],
            "JosekiData.LIST 与 golden 不一致",
        )

    def test_scan(self):
        for case in self.data["scan"]:
            got = _as_json(JosekiRecognizer.scan(case["moves"]))
            exp = case["result"]
            self.assertEqual(got, exp, "%s scan: 期望 %r 实际 %r" % (case["case"], exp, got))


if __name__ == "__main__":
    unittest.main()
