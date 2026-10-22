"""局面点目（v3.12.0）：``ownership`` → 棋盘标记 + 面板数字的**纯函数**门卫。

这里的断言全部不碰引擎 / 事件总线：``estimate.build`` 只吃引擎响应原文与棋子列表，
所以不需要任何 stub 装配。编排侧（analyze 阶段真的发 ``estimate`` 事件）的用例在
``test_agent_graph.py`` 的 ``AnalyzeEstimateTest``。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import estimate


def _analysis(ownership, score=0.0):
    return {"rootInfo": {"winrate": 0.5, "scoreLead": score}, "ownership": ownership}


class BuildTest(unittest.TestCase):
    def test_marks_flat_ownership_with_engine_orientation(self):
        """扁平数组：``index = y * 19 + x``、``y = 0`` 是**最上面一行**（与内部坐标一致）。"""
        own = [0.0] * 361
        own[0] = 0.9                       # (x=0, y=0)
        own[2 * 19 + 5] = -0.9             # (x=5, y=2)
        est = estimate.build(_analysis(own), 0, [])
        self.assertEqual(est["cells"], [[0, 0, "black"], [5, 2, "white"]])
        self.assertEqual(est["black"], 1)
        self.assertEqual(est["white"], 1)
        self.assertEqual(est["komi"], 7.5)
        self.assertEqual(est["move_count"], 0)

    def test_threshold_is_half(self):
        """只标「定了的点」：|ownership| ≥ 0.5 才标，0.49 不标；两侧对称。"""
        own = [0.0] * 361
        own[0] = 0.49
        own[1] = 0.5
        own[2] = -0.5
        own[3] = -0.51
        est = estimate.build(_analysis(own), 0, [])
        self.assertEqual(est["cells"], [[1, 0, "black"], [2, 0, "white"], [3, 0, "white"]])
        self.assertEqual((est["black"], est["white"]), (1, 2))

    def test_live_stone_is_not_marked_dead_stone_is(self):
        """归属与棋子同色 ⇒ 活子不叠方块；相反 ⇒ 被点死，方块画在棋子上。"""
        own = [0.0] * 361
        own[3 * 19 + 3] = 1.0              # 黑活子 (3,3)
        own[10 * 19 + 10] = 0.0            # 中间模糊地带
        own[15 * 19 + 3] = -1.0            # 黑子被点死 (3,15)
        own[0] = -1.0                      # 白方看成自己的空地 (0,0)
        stones = [{"x": 3, "y": 3, "c": "black"}, {"x": 3, "y": 15, "c": "black"}]
        est = estimate.build(_analysis(own), 2, stones)
        self.assertIn([3, 15, "white"], est["cells"], "被点死的黑子必须标出来")
        self.assertNotIn([3, 3, "black"], est["cells"], "活子不必再叠方块")
        self.assertIn([0, 0, "white"], est["cells"])
        self.assertEqual((est["black"], est["white"]), (0, 2))

    def test_nested_ownership_is_accepted(self):
        """嵌套 19×19（旧测试 / 某些封装）与扁平数组同义。"""
        rows = [[0.0] * 19 for _ in range(19)]
        rows[4][7] = 1.0
        own = [v for row in rows for v in row]
        self.assertEqual(estimate.build(_analysis(rows), 0, [])["cells"],
                         estimate.build(_analysis(own), 0, [])["cells"])

    def test_lead_follows_black_perspective(self):
        """``lead`` 与右栏「目差」同源：偶数手直接取 ``scoreLead``，奇数手翻面。"""
        own = [1.0] * 361
        self.assertEqual(estimate.build(_analysis(own, 3.0), 0, [])["lead"], 3.0)
        self.assertEqual(estimate.build(_analysis(own, 3.0), 1, [])["lead"], -3.0)

    def test_dirty_values_degrade_without_raising(self):
        own = [0.0] * 361
        own[0] = "x"                       # 非数值 ⇒ 按 0（不标）
        own[1] = None
        own[2] = 0.9
        analysis = _analysis(own, "bad")
        est = estimate.build(analysis, "n", [{"x": "a", "y": 0, "c": "black"}, "oops"])
        self.assertEqual(est["cells"], [[2, 0, "black"]])
        self.assertIsNone(est["lead"], "目差脏值 ⇒ None（标记照常显示）")
        self.assertEqual(est["move_count"], 0)

    def test_missing_or_bad_ownership_returns_none(self):
        """没有归属信息 / 形状不对 / 引擎报错 ⇒ 什么都不显示（不是空图，是不发事件）。"""
        self.assertIsNone(estimate.build(_analysis(None), 0, []))
        self.assertIsNone(estimate.build(_analysis([0.0] * 100), 0, []))
        self.assertIsNone(estimate.build(_analysis([]), 0, []))
        self.assertIsNone(estimate.build({"error": "云服务不可用"}, 0, []))
        self.assertIsNone(estimate.build(None, 0, []))


if __name__ == "__main__":
    unittest.main()
