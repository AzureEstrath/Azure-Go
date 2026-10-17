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

    def test_every_entry_has_family(self):
        for e in JosekiData.LIST:
            self.assertIn("family", e)
            self.assertRegex(e["family"], r"^[a-z0-9_]+$")

    def test_variants_of_one_lesson_share_family(self):
        fams = [e["family"] for e in JosekiData.LIST if e["name"].startswith("点三三")]
        self.assertEqual(len(set(fams)), 1)

    def test_family_values_per_entry(self):
        # 七条各归哪一族（顺序同 JosekiData.LIST，golden 已锁死顺序）
        self.assertEqual(
            [e["family"] for e in JosekiData.LIST],
            ["star_33"] * 3 + ["star_kakari_small"] * 2 + ["star_double_kakari"] * 2,
        )


if __name__ == "__main__":
    unittest.main()
