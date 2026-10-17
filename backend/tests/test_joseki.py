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
            + ["sansan_kakari"] * 2 + ["star_33"] * 2 + ["komoku_tsuke"] * 2,
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
        """U12：同族内任一条都必须与同族另一条共享 ≥ 2 手**逐手相同**的前缀（核心+延伸 / 分岔都真的存在）。"""
        fams = {}
        for e in JosekiData.LIST:
            fams.setdefault(e["family"], []).append(tuple(tuple(int(v) for v in s) for s in e["seq"]))
        for fam, seqs in fams.items():
            if len(seqs) < 2:
                continue                                     # 单条族由例外清单兜住
            for i, s in enumerate(seqs):
                best = 0
                for j, o in enumerate(seqs):
                    if i == j:
                        continue
                    k = 0
                    while k < min(len(s), len(o)) and s[k] == o[k]:
                        k += 1
                    best = max(best, k)
                self.assertGreaterEqual(
                    best, 2,
                    "族 %s 的第 %d 条线（%s）没跟同族另一条共享 ≥2 手前缀" % (fam, i, s[:2]),
                )

    def test_entered_is_still_reachable(self):
        """U49 附则：补了分岔之后，「进入」必须仍然到得了——拿一条 2 手不构成分岔的手顺钉住它。

        小目一间低挂（3 手核心）在角内 2 手时没有第二条同前缀线，所以还是 `entered`；
        守角 / 大飞挂 / 一间挂那几族是 2 手条目，走到那里已是 `formed`（成型），不是进入。
        """
        r = JosekiRecognizer.scan([["W", "D17"], ["B", "F17"]])   # 左上：白小目 + 黑一间低挂
        self.assertEqual(r["kind"], "entered")
        self.assertEqual(r["family"], "komoku_low_kakari")
        self.assertEqual(r["k"], 2)
        self.assertEqual(r["candidates"], [])
        self.assertEqual(len(r["line"]), 3)                       # 显示线＝这一课的核心手顺

    def test_branch_on_real_library_star_33_fork(self):
        # U49：点三三补了「挡另一边」（另一边的挡法）之后，进入这一课第 3 手就是真分岔：
        # 显示线＝点三三·基本型（挡 (2,3) 那一边），候选＝另一边挡 (3,2)。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C17"]])   # 左上：星位 + 点三三
        self.assertEqual(r["kind"], "branch")
        self.assertEqual(r["family"], "star_33")
        self.assertEqual(r["k"], 2)
        self.assertEqual(len(r["line"]), 4)
        self.assertEqual(r["line"][0]["c"], "B")
        ref_next = (r["line"][r["k"]]["x"], r["line"][r["k"]]["y"])
        self.assertEqual(ref_next, (2, 3))
        self.assertEqual(
            [(c["family"], c["name"], tuple(c["next"])) for c in r["candidates"]],
            [("star_33", "点三三·挡另一边", (3, 2))],
        )

    def test_line_maps_mirrored_corner(self):
        r = JosekiRecognizer.scan([["B", "D4"], ["W", "C3"]])      # 左下角，对角镜像
        # 与 test_branch_on_real_library_star_33_fork 同一局面的镜像读法：补了「挡另一边」之后
        # 进入这一课第 3 手即有分岔。本用例只钉「显示线映射到镜像角」这件事，故两种都收。
        self.assertIn(r["kind"], ("entered", "branch"))
        self.assertEqual(r["family"], "star_33")
        self.assertTrue(all(0 <= p["x"] < 19 and 0 <= p["y"] < 19 for p in r["line"]))

    def test_branch_when_two_lines_share_prefix(self):
        # T2 的合成库用例（U4 要求原样留着）：真实库现在也有真分岔了（见 test_branch_on_real_library_star_33_fork
        # 与 test_branch_on_real_library_after_knight_approach），这里继续用两条合成线钉住 branch 的最基本判定。
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
        # U16：候选只能是「轮到的那一方」的走法。星位 + 白小飞挂之后，真实库里有两条同一方（黑）
        # 的应法——贴住（4,2 一路，原有靠压线）与在另一边小飞应（2,5，新线）；这里钉住候选的**实际内容**
        # （名字 + 落点；跨族同色允许），并确认另一条读法（双飞燕）的下一步是白棋、不在候选里。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "C14"]])
        self.assertEqual(r["kind"], "branch")
        self.assertEqual(r["family"], "star_kakari_small")
        self.assertEqual(r["k"], 2)
        self.assertEqual(
            [(c["family"], c["name"], tuple(c["next"])) for c in r["candidates"]],
            [("star_kakari_small", "小飞挂·靠压定式", (2, 4))],
        )
        self.assertNotIn("star_double_kakari", [c["family"] for c in r["candidates"]])

    def test_branch_on_real_library_after_knight_approach(self):
        # U4：真实库的分岔——白小飞挂之后，黑既可贴住（4,2，原有靠压线）也可在另一边小飞应（2,5，新线）。
        r = JosekiRecognizer.scan([["B", "D16"], ["W", "F17"]])
        self.assertEqual(r["kind"], "branch")
        self.assertEqual(r["family"], "star_kakari_small")
        self.assertEqual(r["k"], 2)
        ref_next = (r["line"][r["k"]]["x"], r["line"][r["k"]]["y"])
        self.assertEqual(ref_next, (2, 5))                        # 显示线＝新线（小飞挂·小飞应）
        self.assertEqual(
            [(c["name"], c["family"], tuple(c["next"])) for c in r["candidates"]],
            [("小飞挂·靠压定式", "star_kakari_small", (4, 2))],
        )
        self.assertNotEqual(tuple(r["candidates"][0]["next"]), ref_next)

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
