"""JosekiData / JosekiRecognizer 黄金样本比对：逐项对照 tests/golden/joseki.json。"""

import json
import os
import unittest

from app.joseki import REGION, JosekiData, JosekiRecognizer

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
        # 每条各归哪一族（顺序同 JosekiData.LIST，golden 已锁死顺序）
        self.assertEqual(
            [e["family"] for e in JosekiData.LIST],
            ["star_33"] * 3 + ["star_kakari_small"] * 2 + ["star_double_kakari"] * 2
            + ["star_kakari_small"] * 2 + ["star_shimari"] * 2
            + ["star_kakari_big"] * 2 + ["star_kakari_one"] * 2
            + ["komoku_small_kakari"] * 2 + ["komoku_shimari"] + ["komoku_low_kakari"]
            + ["sansan_kakari"] * 2,
        )

    def test_real_library_at_least_two_lines_or_exception(self):
        """U12：每条核心族至少两条线；只有一条的必须进显式例外清单并写明理由。"""
        # 例外：这两族引擎自对弈里只出现「同一个形状」的单独一手（守角 / 一间低挂），
        # 对方紧接着的应手在这批提取里没有出现，故各留一条；不放宽成「几条都行」。
        single_ok = {
            "komoku_shimari": "小目守角在引擎自对弈里只出现一间跳一种守法，没有第二条同形线",
            "komoku_low_kakari": "一间低挂后引擎只给出一间跳应，没有再出现第二种应法",
        }
        counts = {}
        for e in JosekiData.LIST:
            counts[e["family"]] = counts.get(e["family"], 0) + 1
        for fam, n in counts.items():
            if n < 2:
                self.assertIn(fam, single_ok, "族 %s 只有 %d 条线，且不在例外清单里" % (fam, n))
                self.assertTrue(single_ok[fam], "族 %s 的例外理由不能为空" % fam)

    def test_no_two_entries_share_identical_seq(self):
        """U12：两条完全相同的 seq 不许出现。"""
        seen = {}
        for e in JosekiData.LIST:
            key = tuple(tuple(int(v) for v in step) for step in e["seq"])
            self.assertNotIn(key, seen, "seq 重复：%s 与 %s" % (e["name"], seen.get(key)))
            seen[key] = e["name"]

    def test_all_coords_inside_corner_region(self):
        """U12：所有归一化坐标都必须在角内（max(nx, ny) < REGION）。"""
        for e in JosekiData.LIST:
            for col, nx, ny in e["seq"]:
                self.assertIn(col, (0, 1), "%s 的 col 只能是 0/1：%r" % (e["name"], col))
                self.assertLess(max(nx, ny), REGION, "%s 有角外坐标：%r" % (e["name"], [nx, ny]))

    def test_family_entries_share_two_move_prefix(self):
        """U12：同族内任一条都必须与同族另一条共享 ≥ 2 手前缀（核心+延伸 / 分岔都真的存在）。"""
        fams = {}
        for e in JosekiData.LIST:
            fams.setdefault(e["family"], []).append(tuple(tuple(int(v) for v in s) for s in e["seq"]))
        for fam, seqs in fams.items():
            if len(seqs) < 2:
                continue                                     # 单条族由例外清单兜住
            for s in seqs:
                shared = max(
                    (len([1 for i in range(min(len(s), len(o))) if s[i] == o[i]])
                     for o in seqs if o is not s),
                    default=0,
                )
                self.assertGreaterEqual(shared, 2, "族 %s 里有条线没跟同族另一条共享 ≥2 手前缀" % fam)

    def test_entered_on_first_library_move(self):
        # 星位 + 点三三（角内恰好 2 手）仍是进入：本条手顺上库内只有一条读法（点三三·基本型），
        # 第 3 手没有分歧。T11a 的「挡这边」替代线**没有**入库——它会把这个局面变成 branch，
        # 而 pin 住 entered 的编排用例（test_joseki_progress / test_agent / test_agent_graph）
        # 不在本任务的提交范围内，详见 task-11-report.md。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C17"]])   # 左上：星位 + 点三三
        self.assertEqual(r["kind"], "entered")
        self.assertEqual(r["family"], "star_33")
        self.assertEqual(r["k"], 2)
        self.assertEqual(len(r["line"]), 4)                        # 显示线＝核心手顺（点三三·基本型）
        self.assertEqual(r["line"][0]["c"], "B")
        self.assertEqual(r["candidates"], [])

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

    def test_branch_candidates_are_always_the_same_colour(self):
        # U16：候选只能是「轮到的那一方」的走法。星位 + 白小飞挂之后，真实库里现在有两条同一方
        # （黑）的应法——贴住（E17 一路）与小飞应（另一边），所以这里由 entered 变成 branch；
        # 不变的是：每个候选第 k 手的颜色必须与显示线第 k 手相同（跨族同色允许，跨色不许）。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C14"]])
        self.assertEqual(r["kind"], "branch")
        self.assertGreaterEqual(len(r["candidates"]), 1)
        ref_col = r["line"][r["k"]]["c"]
        for c in r["candidates"]:
            self.assertEqual(c["line"][r["k"]]["c"], ref_col)
            self.assertNotEqual(tuple(c["next"]),
                                (r["line"][r["k"]]["x"], r["line"][r["k"]]["y"]))
        # 另一条读法（双飞燕）的下一步是白棋，不能当作同一方分岔的候选
        self.assertNotIn("star_double_kakari", [c["family"] for c in r["candidates"]])

    def test_branch_on_real_library_after_knight_approach(self):
        # U4：真实库的第二条分岔——白小飞挂之后，黑既可贴住（原有靠压线）也可在另一边小飞应（新线）。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "F17"]])
        self.assertEqual(r["kind"], "branch")
        self.assertEqual(r["k"], 2)
        fams = sorted(c["family"] for c in r["candidates"])
        self.assertIn("star_kakari_small", fams)
        self.assertTrue(all(c["line"][r["k"]]["c"] == r["line"][r["k"]]["c"] for c in r["candidates"]))

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
