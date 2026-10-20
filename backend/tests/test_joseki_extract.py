"""tools/joseki_extract.py 的纯函数门卫。

只测三个纯函数（角部归一化 / 去重 / 条目组装）：**不联网、不起引擎、不读环境变量**。
自对弈驱动（CLI）属开发工具，手工运行，不在回归范围内。

`tools/` 在**仓库根**，而测试一律从 `backend/` 跑（`python -m unittest discover -s tests`），
`backend/tests/helpers.py` 只把 `backend/` 加进 `sys.path`——所以这里自己把仓库根也加进去
（U8）；`backend/` 顺手补上，免得依赖导入顺序。
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import joseki                      # noqa: E402
from tools import joseki_extract            # noqa: E402


class CornerNormalizeTest(unittest.TestCase):
    def test_normalize_skips_pass_and_off_corner(self):
        """按手顺只收该角内着子：pass 与角外着手都要跳过（corner=0 左上）。"""
        moves = [["B", "Q16"], ["W", "pass"], ["B", "D16"], ["W", "Q4"], ["B", "D17"]]
        self.assertEqual(joseki_extract.corner_normalize(moves, 0), [[0, 3, 3], [0, 3, 2]])

    def test_normalize_mirror_corners_agree(self):
        """同一个形状摆在右上（corner=1）与左下（corner=2）时归一化结果一致。"""
        top_right = [["B", "Q16"], ["W", "Q15"], ["B", "Q17"]]
        left_bottom = [["B", "D4"], ["W", "D5"], ["B", "D3"]]
        expect = [[0, 3, 3], [1, 3, 4], [0, 3, 2]]
        self.assertEqual(joseki_extract.corner_normalize(top_right, 1), expect)
        self.assertEqual(joseki_extract.corner_normalize(left_bottom, 2), expect)

    def test_normalize_needs_two_corner_moves(self):
        """角内不足 2 手返回空表（哪怕手顺里有别的角）。"""
        self.assertEqual(joseki_extract.corner_normalize([["B", "D4"]], 0), [])
        self.assertEqual(joseki_extract.corner_normalize([["B", "Q16"]], 0), [])
        self.assertEqual(joseki_extract.corner_normalize([], 0), [])


class DedupeLinesTest(unittest.TestCase):
    def test_dedupe_keeps_longest_prefix(self):
        """互为前缀的两条只留最长；与输入顺序无关。"""
        short = [[0, 3, 3], [1, 2, 2]]
        long = [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2]]
        self.assertEqual(joseki_extract.dedupe_lines([long, short]), [long])
        self.assertEqual(joseki_extract.dedupe_lines([short, long]), [long])
        # 三截同族：只留最长的那条
        mid = [[0, 3, 3], [1, 2, 2], [0, 2, 3]]
        self.assertEqual(joseki_extract.dedupe_lines([short, long, mid]), [long])

    def test_dedupe_identical_and_unrelated_lines(self):
        """完全相同的两条留一条；互不构成前缀的全部保留且顺序稳定。"""
        a = [[0, 3, 3], [1, 2, 2]]
        b = [[0, 3, 3], [1, 3, 2]]          # 与 a 同长、第二手不同：互不构成前缀
        c = [[0, 3, 3], [1, 2, 2], [0, 2, 3]]
        self.assertEqual(joseki_extract.dedupe_lines([a, a, b]), [a, b])
        self.assertEqual(joseki_extract.dedupe_lines([b, a, a]), [b, a])
        self.assertEqual(joseki_extract.dedupe_lines([b, c, a]), [b, c])
        self.assertEqual(joseki_extract.dedupe_lines([]), [])


class ToEntryTest(unittest.TestCase):
    def test_to_entry_shape_matches_library(self):
        """to_entry 的键集合必须精确等于库条目的五个键（家族/名称/分类/手顺/讲解词）。"""
        entry = joseki_extract.to_entry(
            "star_33", "点三三·基本型", "star", [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2]],
            "对方点三三就还他角地。")
        self.assertEqual(set(entry.keys()),
                         {"family", "name", "cat", "seq", "key"})
        self.assertEqual(entry["family"], "star_33")
        self.assertEqual(entry["name"], "点三三·基本型")
        self.assertEqual(entry["cat"], "star")
        self.assertEqual(entry["seq"], [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2]])
        self.assertEqual(entry["key"], "对方点三三就还他角地。")
        # 库中每条都恰好这五个键——同形（对照 backend/app/joseki.py 的 JosekiData.LIST）
        self.assertTrue(joseki.JosekiData.LIST)
        for e in joseki.JosekiData.LIST:
            self.assertEqual(set(e.keys()), set(entry.keys()))
        self.assertEqual(set(joseki.JosekiData.LIST[0].keys()), set(entry.keys()))


if __name__ == "__main__":
    unittest.main()
