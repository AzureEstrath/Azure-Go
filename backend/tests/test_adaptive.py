"""自适应难度（v3.11.5）：画像 / 聊天 / 表现信号、基线夹取、冷却、落点弱化。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import adaptive


class FixedRng:
    def __init__(self, v=0.0):
        self.v = float(v)

    def random(self):
        return self.v


class SeqRng:
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def random(self):
        v = self.seq[min(self.i, len(self.seq) - 1)] if self.seq else 0.0
        self.i += 1
        return v


class SignalTest(unittest.TestCase):
    def test_profile_adj(self):
        self.assertEqual(adaptive.profile_adj({"blunder.rate": {"value": 0.3, "confidence": 0.6}}), -1)
        self.assertEqual(adaptive.profile_adj({"blunder.rate": {"value": 0.02, "confidence": 0.6}}), 1)
        self.assertEqual(adaptive.profile_adj({"blunder.rate": {"value": 0.3, "confidence": 0.2}}), 0)
        self.assertEqual(adaptive.profile_adj({"blunder.rate": 0.3}), 0)   # 扁平脏值：无置信度不判
        self.assertEqual(adaptive.profile_adj(None), 0)

    def test_chat_signal(self):
        self.assertEqual(adaptive.chat_signal("这也太难了吧"), -1)
        self.assertEqual(adaptive.chat_signal("让着我一点嘛"), -1)
        self.assertEqual(adaptive.chat_signal("你能不能认真点"), 1)
        self.assertEqual(adaptive.chat_signal("别放水"), 1)                # 先查「更难」
        self.assertEqual(adaptive.chat_signal("今天天气不错"), 0)

    def test_performance_signal(self):
        bad = [{"rank": 5, "drop": 0.12}] * 4
        self.assertEqual(adaptive.performance_signal(bad, 0.30), -1)
        good = [{"rank": 0, "drop": 0.0}] * 4
        self.assertEqual(adaptive.performance_signal(good, 0.70), 1)
        mixed = [{"rank": 2, "drop": 0.03}] * 4
        self.assertEqual(adaptive.performance_signal(mixed, 0.50), 0)
        self.assertEqual(adaptive.performance_signal([{"rank": 5, "drop": 0.1}], 0.2), 0)  # 样本不足


class StateTest(unittest.TestCase):
    def test_chat_steps_are_bounded_and_cooled(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=3, spread=1, rng=FixedRng(0))
        a.reset(0)
        self.assertEqual(a.level, 3)
        self.assertTrue(a.note_chat("太难了", 1))
        self.assertEqual(a.level, 2)
        self.assertFalse(a.note_chat("还是太难", 2))      # 4 手冷却内不再动
        self.assertFalse(a.note_chat("太难了", 5))        # 已到基线 -1，夹住
        self.assertEqual(a.level, 2)

        b = adaptive.AdaptiveDifficulty(enabled=True, base=3, spread=1, rng=FixedRng(0))
        b.reset(0)
        self.assertTrue(b.note_chat("认真点", 1))
        self.assertEqual(b.level, 4)
        self.assertFalse(b.note_chat("再用全力", 6))      # 已到基线 +1，夹住

    def test_profile_shift_moves_baseline(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=3, spread=1)
        a.reset(-1)
        self.assertEqual(a.base, 2)
        self.assertEqual(a.level, 2)
        a.configure(enabled=True, base=4, profile_shift=1)
        self.assertEqual((a.base, a.level), (5, 5))

    def test_disabled_never_moves(self):
        a = adaptive.AdaptiveDifficulty(enabled=False, base=3)
        a.reset(0)
        self.assertFalse(a.note_chat("太难", 1))
        self.assertFalse(a.note_move(1, 0.2, 5, 0.2))
        self.assertEqual(a.level, 3)

    def test_performance_updates_after_window_and_cooldown(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=3, spread=1)
        a.reset(0)
        for i in range(1, 4):
            a.note_move(i, 0.30, 5, 0.12)
        self.assertEqual(a.level, 2)                      # 三手都吃力：降一档
        for i in range(4, 11):
            a.note_move(i, 0.70, 0, 0.0)
        self.assertEqual(a.level, 3)                      # 后 6 手全压制：升回基线


class PickTest(unittest.TestCase):
    INFOS = [
        {"move": "Q16", "winrate": 0.55},
        {"move": "D4", "winrate": 0.50},
        {"move": "Q4", "winrate": 0.45},
        {"move": "D16", "winrate": 0.30},
        {"move": "pass", "winrate": 0.60},
    ]

    def _legal(self, gtp):
        return gtp != "D4"                       # D4 非法；pass 由 allow_pass 另判

    def test_easy_level_chooses_weaker_but_within_tolerance(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=1, spread=0, rng=SeqRng([0.0, 0.99]))
        a.reset(0)
        pick = a.choose(self.INFOS, self._legal, False, ["Q16", 15, 3])
        self.assertNotEqual(pick[0], "Q16")
        self.assertIn(pick[0], ("Q4",))          # D4 非法、D16 超容差、pass 不允许
        self.assertEqual(pick, ["Q4", 15, 15])

    def test_level_five_and_disabled_keep_fallback(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=5, spread=0, rng=FixedRng(0))
        a.reset(0)
        self.assertEqual(a.choose(self.INFOS, self._legal, False, ["Q16", 15, 3]), ["Q16", 15, 3])
        b = adaptive.AdaptiveDifficulty(enabled=False)
        b.reset(0)
        self.assertEqual(b.choose(self.INFOS, self._legal, False, ["Q16", 15, 3]), ["Q16", 15, 3])

    def test_never_picks_legal_pass_when_disallowed(self):
        a = adaptive.AdaptiveDifficulty(enabled=True, base=1, spread=0, rng=FixedRng(0))
        a.reset(0)
        self.assertNotEqual(a.choose(self.INFOS, lambda g: True, False, ["Q16", 15, 3])[0], "pass")


if __name__ == "__main__":
    unittest.main()
