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
    if "candidates" in out:
        out["candidates"] = [
            dict(c, next={"x": c["next"][0], "y": c["next"][1]}) for c in out["candidates"]
        ]
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

    def test_entered_on_first_library_move(self):
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C17"]])   # 左上：星位 + 点三三
        self.assertEqual(r["kind"], "entered")
        self.assertEqual(r["family"], "star_33")
        self.assertEqual(r["k"], 2)
        self.assertEqual(len(r["line"]), 4)                        # 显示线＝核心手顺（点三三·基本型）
        self.assertEqual(r["line"][0]["c"], "B")

    def test_line_maps_mirrored_corner(self):
        r = JosekiRecognizer.scan([["B", "D4"], ["W", "C3"]])      # 左下角，对角镜像
        self.assertEqual(r["kind"], "entered")
        self.assertTrue(all(0 <= p["x"] < 19 and 0 <= p["y"] < 19 for p in r["line"]))

    def test_branch_when_two_lines_share_prefix(self):
        # 现有库里很难稳定构造出「同一方、下一步不同」的真实分岔（不同朝向把两条课凑到同一
        # 前缀时，下一步往往不是同一方），故用两条合成线钉住 branch 的判定；真实分岔由后续
        # 扩库任务补。
        saved = JosekiData.LIST
        try:
            JosekiData.LIST = [
                {"name": "测试线甲", "cat": "star", "family": "tk_a",
                 "seq": [[0, 3, 3], [1, 2, 2], [0, 2, 3]], "key": "甲"},
                {"name": "测试线乙", "cat": "star", "family": "tk_a",
                 "seq": [[0, 3, 3], [1, 2, 2], [0, 4, 3]], "key": "乙"},
            ]
            r = JosekiRecognizer.scan([["B", "D16"], ["W", "C17"]])
            self.assertEqual(r["kind"], "branch")
            self.assertEqual(r["family"], "tk_a")
            self.assertGreaterEqual(len(r["candidates"]), 1)
            for c in r["candidates"]:
                self.assertNotEqual(tuple(c["next"]), (2, 3))
        finally:
            JosekiData.LIST = saved

    def test_off_turn_candidate_is_not_a_fork(self):
        # 星位 + 白小飞挂：另一条读法（双飞燕）的下一步是白棋，不能当作同一方分岔的候选
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C14"]])
        self.assertEqual(r["kind"], "entered")
        self.assertEqual(r["candidates"], [])

    def test_formed_and_points_unchanged(self):
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]])
        self.assertEqual(r["kind"], "formed")
        self.assertEqual(r["points"], [(3, 3), (2, 2), (2, 3), (3, 2)])

    def test_advancing_mid_line_is_silent(self):
        # 角内全部吻合、但已过「进入」那一手、且无分岔 → 不出结果（中间推进一个字不提定式）
        self.assertEqual(JosekiRecognizer.scan([["B", "D16"], ["W", "C17"], ["B", "C16"]]), {})

    def test_off_line_move_does_not_fork(self):
        # 角内最后一手没吻合任何共享前缀（k < n）→ 不出结果，不得读成 branch
        self.assertEqual(JosekiRecognizer.scan([["B", "Q16"], ["W", "R14"], ["B", "T14"]]), {})


if __name__ == "__main__":
    unittest.main()
