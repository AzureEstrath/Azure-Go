"""回忆链（v4.3.0，D）：开始 / 延续 / 终止的纯逻辑，不依赖真实记忆库。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import recall_chain as rc

MOOD = {"v": 0.58, "c": 0.55, "a": 0.35}
LOW = {"v": 0.30, "c": 0.40, "a": 0.20}
BRIGHT = {"v": 0.80, "c": 0.70, "a": 0.65}


class DepthTest(unittest.TestCase):
    def test_short_when_low(self):
        self.assertEqual(rc.max_depth(LOW, 0.8), 1)

    def test_normal_is_two(self):
        self.assertEqual(rc.max_depth(MOOD, 0.35), 2)

    def test_long_when_close_and_bright(self):
        self.assertEqual(rc.max_depth(BRIGHT, 0.75), 3)

    def test_dirty_mood_defaults(self):
        self.assertEqual(rc.max_depth({"v": "x"}, 0.35), 2)


class StartTest(unittest.TestCase):
    def test_chance_gate_and_cooldown(self):
        self.assertTrue(rc.should_start(1000, {}, 0, MOOD, 0.0, 0.2))
        self.assertFalse(rc.should_start(1000, {}, 0, MOOD, 0.9, 0.2))
        # 刚结束一条链：10 分钟内不开新链
        self.assertFalse(rc.should_start(1000, {}, 1000 - 60, MOOD, 0.0, 1.0))

    def test_cue_bypasses_chance_but_not_short_cooldown(self):
        self.assertTrue(rc.should_start(1000, {}, 0, MOOD, 0.99, 0.1, cue=True))
        self.assertFalse(rc.should_start(1000, {}, 1000 - 60, MOOD, 0.0, 0.1, cue=True))
        self.assertTrue(rc.should_start(1000, {}, 1000 - rc.CUE_COOLDOWN_SEC, MOOD, 0.0, 0.1, cue=True))

    def test_existing_chain_blocks_new_start(self):
        chain = rc.start("雨天", 1000, MOOD)
        self.assertFalse(rc.should_start(1000, chain, 0, MOOD, 0.0, 1.0))


class ContinueTest(unittest.TestCase):
    def test_basic_continue(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertTrue(rc.should_continue(1100, chain, MOOD, "雨天的棋盘很好看"))

    def test_stops_at_max_depth(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=1)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertFalse(rc.should_continue(1100, chain, MOOD))

    def test_stops_on_window(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertFalse(rc.should_continue(1000 + rc.CHAIN_WINDOW_SEC + 10, chain, MOOD))

    def test_stops_on_mood_shift(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertFalse(rc.should_continue(1100, chain, {"v": 0.1, "a": 0.9}))

    def test_stops_on_topic_shift(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertFalse(rc.should_continue(1100, chain, MOOD, "我们换个话题，聊聊最近的新番动画吧"))

    def test_short_ack_does_not_shift_topic(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertTrue(rc.should_continue(1100, chain, MOOD, "嗯嗯"))

    def test_chat_min_gap(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        self.assertFalse(rc.should_continue(1030, chain, MOOD, "雨天", min_gap=90))
        self.assertTrue(rc.should_continue(1100, chain, MOOD, "雨天", min_gap=90))

    def test_explicit_memory_cue_skips_topic_check(self):
        """玩家明确提旧事时，不因为这句话字面换了话题就断链。"""
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=2)
        chain = rc.advance(chain, 1000, item_id=1)
        text = "你还记得上次那件事吗"
        self.assertFalse(rc.should_continue(1100, chain, MOOD, text))
        self.assertTrue(rc.should_continue(1100, chain, MOOD, text, check_topic=False))


class TopicTest(unittest.TestCase):
    def test_cue_and_shift(self):
        self.assertTrue(rc.memory_cue("你还记得上次说的那盘棋吗"))
        self.assertTrue(rc.memory_cue("之前我们去过的棋院"))
        self.assertFalse(rc.memory_cue("明天吃什么"))
        self.assertTrue(rc.shift_cue("换个话题吧"))
        self.assertTrue(rc.topic_shifted("我们聊点别的，最近的新番动画不错", "雨天摆棋"))

    def test_overlap(self):
        self.assertGreater(rc.topic_overlap("雨天摆棋很开心", "雨天在窗边摆棋"), 0.3)


class StateTest(unittest.TestCase):
    def test_start_and_advance_are_pure_copies(self):
        chain = rc.start("雨天摆棋", 1000, MOOD, depth_max=3, source="chat")
        self.assertEqual(chain["depth"], 0)
        self.assertEqual(chain["source"], "chat")
        nxt = rc.advance(chain, 1100, item_id=7)
        self.assertEqual(chain["depth"], 0, "原对象不变")
        self.assertEqual(nxt["depth"], 1)
        self.assertEqual(nxt["ids"], [7])

    def test_clamps_depth(self):
        chain = rc.start("x", 1, MOOD, depth_max=9)
        self.assertEqual(chain["max_depth"], rc.MAX_DEPTH_CAP)


if __name__ == "__main__":
    unittest.main()
