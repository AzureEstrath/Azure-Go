"""事件遗忘权重 → 4 档淡化（纯函数）：权重只决定「多少步」，程度＝固定 4 档。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class LevelTest(unittest.TestCase):
    def test_level_boundaries(self):
        """``d = now - event`` 的边界：1 / 3 / 6 是每档上界，第 4 档到 12。"""
        from app.decay import DECAY_DROP_AT, DECAY_SPAN, level
        self.assertEqual(DECAY_SPAN, [1, 3, 6])
        self.assertEqual(DECAY_DROP_AT, 12)
        table = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 3, 7: 4, 12: 4}
        for d, want in table.items():
            self.assertEqual(level(10, 10 + d), want, "d=%d" % d)
        self.assertEqual([level(0, 1), level(0, 3), level(0, 6), level(0, 12)], [1, 2, 3, 4])

    def test_dropped_after_drop_at(self):
        """超过 ``DECAY_DROP_AT`` ⇒ 哨兵 0（不再进提示词，但仍留在事件表里）。"""
        from app.decay import DECAY_DROPPED, level
        self.assertEqual(DECAY_DROPPED, 0)
        for d in (13, 20, 100):
            self.assertEqual(level(1, 1 + d), 0)

    def test_negative_delta_is_dropped_not_raised(self):
        from app.decay import level
        self.assertEqual(level(9, 3), 0, "事件比当前手还新：脏值 → 不进提示词")

    def test_dirty_inputs_do_not_raise(self):
        from app.decay import level
        for a, b in (("abc", 3), (3, None), (None, None), (1, "x")):
            self.assertEqual(level(a, b), 0, "(%r, %r)" % (a, b))
        self.assertEqual(level(1.5, 2.5), 1, "数值型脏值仍按差值算")


class CornerWordTest(unittest.TestCase):
    def test_quadrants_and_middles(self):
        """象限 + 方位（不是「真角区」）：象限中部也要给词；用词与设计 §6.4 模板一致（**不带「角」**）。"""
        from app.decay import corner_word
        cases = [
            (3, 3, "左上"), (8, 8, "左上"), (0, 0, "左上"),      # 左上象限（含中部）
            (15, 3, "右上"), (13, 5, "右上"), (18, 0, "右上"),
            (3, 15, "左下"), (5, 13, "左下"), (0, 18, "左下"),
            (15, 15, "右下"), (16, 16, "右下"), (18, 18, "右下"),
        ]
        for x, y, want in cases:
            self.assertEqual(corner_word(x, y), want, "(%d,%d)" % (x, y))
        self.assertNotIn("角", corner_word(3, 3), "档 4 只说方位，不说「角」")

    def test_no_direction_when_coords_are_ambiguous(self):
        """方位词按坐标核对，核不上就不说方位（正中那一列 / 行凑不出方位词）。"""
        from app.decay import corner_word
        for x, y in ((9, 3), (3, 9), (9, 9), (None, 3), (3, None), (-1, 3), (3, 99), ("a", 3)):
            self.assertEqual(corner_word(x, y), "", "(%r,%r) 不该给出方位" % (x, y))


class PhraseTest(unittest.TestCase):
    def test_dropped_level_renders_nothing(self):
        from app.decay import bad_move_phrase
        self.assertEqual(bad_move_phrase(0, move_no=3, impact="角上的实空",
                                        consequence="胜率下滑", x=3, y=3), "")

    def test_level1_has_impact_and_consequence(self):
        from app.decay import bad_move_phrase
        s = bad_move_phrase(1, move_no=7, impact="左下角的实空", consequence="胜率掉了一截")
        self.assertIn("7", s)
        self.assertIn("左下角的实空", s)
        self.assertIn("胜率掉了一截", s)

    def test_info_decreases_with_level(self):
        """档 1→4 信息量递减：原因 + 后果 → 只留事实 → 只留方位。"""
        from app.decay import bad_move_phrase
        s1 = bad_move_phrase(1, move_no=7, impact="实空", consequence="胜率下滑")
        s2 = bad_move_phrase(2, move_no=7, impact="实空", consequence="胜率下滑")
        s3 = bad_move_phrase(3, move_no=7, impact="实空", consequence="胜率下滑")
        s4 = bad_move_phrase(4, move_no=7, impact="实空", consequence="胜率下滑", x=3, y=3)
        self.assertIn("胜率下滑", s1)
        self.assertNotIn("胜率下滑", s2)
        self.assertIn("实空", s2)
        self.assertNotIn("实空", s3)
        self.assertIn("7", s3)
        self.assertNotIn("7", s4)
        self.assertIn("左上", s4)
        self.assertGreater(len(s1), len(s2))
        self.assertGreater(len(s2), len(s3))

    def test_phrase_degrades_when_data_missing(self):
        from app.decay import bad_move_phrase
        # 档 1 缺「影响」⇒ 退到档 2 的说法（说不出影响就不硬拼半句）
        s = bad_move_phrase(1, move_no=7, consequence="胜率下滑")
        self.assertIn("7", s)
        self.assertNotIn("胜率下滑", s)
        # 连手数都没有 ⇒ 退到只说方位
        s2 = bad_move_phrase(2, move_no=None, x=3, y=3)
        self.assertIn("左上", s2)
        self.assertNotIn("7", s2)
        # 什么都核不上 ⇒ 最少的那句（不抛、不为空）
        s3 = bad_move_phrase(4, move_no=None, x=None, y=None)
        self.assertTrue(s3)
        self.assertNotIn("左上", s3)

    def test_level4_falls_back_to_fact_when_direction_unclear(self):
        from app.decay import bad_move_phrase
        s = bad_move_phrase(4, move_no=7, x=9, y=3)      # 正中那一列 ⇒ 方位核不上
        self.assertIn("7", s)
        self.assertNotIn("角", s)


if __name__ == "__main__":
    unittest.main()
