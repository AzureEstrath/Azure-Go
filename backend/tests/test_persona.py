"""人格化纯逻辑与 persona 表存储（不触网、不碰真实库）。"""

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import persona
from app.profile_store import ProfileStore


class _Rng:
    def __init__(self, values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0) if self._values else 0.0


class StageTest(unittest.TestCase):
    def test_boundaries(self):
        got = [(n, persona.relationship_stage(n)["name"]) for n in (0, 2, 3, 9, 10, 29, 30)]
        self.assertEqual([g[1] for g in got],
                         ["初识", "初识", "熟悉", "熟悉", "熟络", "熟络", "老搭档"])

    def test_interactions_are_capped(self):
        a = persona.relationship_stage(0, 8)       # +1
        b = persona.relationship_stage(0, 800)     # 仍只 +6
        self.assertEqual(a["score"], 1)
        self.assertEqual(b["score"], 6)
        self.assertEqual(b["name"], "熟悉")

    def test_note_empty_when_never_played(self):
        self.assertEqual(persona.relationship_note(persona.relationship_stage(0, 99)), "")

    def test_note_mentions_games_and_name(self):
        note = persona.relationship_note(persona.relationship_stage(4, 0), "小明")
        self.assertIn("熟悉", note)
        self.assertIn("小明", note)
        self.assertIn("4 局", note)

    def test_address_name_only_at_close_stages(self):
        familiar = persona.relationship_note(persona.relationship_stage(4, 0), "小明", "搭档")
        self.assertNotIn("搭档", familiar)
        close = persona.relationship_note(persona.relationship_stage(12, 0), "小明", "搭档")
        self.assertIn("搭档", close)
        no_nick = persona.relationship_note(persona.relationship_stage(12, 0), "小明")
        self.assertNotIn("搭档", no_nick)
        self.assertIn("小明", no_nick)


class MoodTest(unittest.TestCase):
    def test_half_life(self):
        mood = {"v": 0.9, "c": 0.7, "a": 0.8}
        same = persona.mood_after_break(mood, 1000.0, now=1000.0, half_life_h=30.0)
        self.assertAlmostEqual(same["v"], 0.9, places=6)
        half = persona.mood_after_break(mood, 1.0, now=1.0 + 30.0 * 3600.0, half_life_h=30.0)
        self.assertAlmostEqual(half["v"], 0.58 + (0.9 - 0.58) * 0.5, places=6)
        old = persona.mood_after_break(mood, 1.0, now=1.0 + 30.0 * 3600.0 * 10, half_life_h=30.0)
        self.assertLess(abs(old["v"] - 0.58), 0.001)

    def test_dirty_input(self):
        self.assertEqual(persona.mood_after_break({"v": "x"}, 1.0), {})
        self.assertEqual(persona.mood_after_break({"v": math.nan, "c": 0.5, "a": 0.5}, 1.0), {})
        self.assertEqual(persona.mood_after_break({"v": 0.5, "c": 0.5, "a": 0.5}, 0), {})

    def test_memory_note_needs_two(self):
        self.assertEqual(persona.memory_note(1), "")
        self.assertEqual(persona.memory_note(-1), "")
        self.assertIn("赢", persona.memory_note(2))
        self.assertIn("输", persona.memory_note(-2))


class IntentTest(unittest.TestCase):
    def test_chance_zero_never(self):
        self.assertIsNone(persona.pick_intent(_Rng([0.0]), chance=0.0))

    def test_chance_one_always(self):
        item = persona.pick_intent(_Rng([0.0, 0.0]), chance=1.0)
        self.assertEqual(item["key"], "quiet")
        self.assertIn("安静", persona.intent_note(item))

    def test_miss_returns_none(self):
        self.assertIsNone(persona.pick_intent(_Rng([0.9]), chance=0.25))

    def test_idle_hint(self):
        self.assertEqual(persona.intent_idle_hint(None), "")
        self.assertIn("短一点", persona.intent_idle_hint({"key": "quiet"}))
        self.assertIn("最近在忙什么", persona.intent_idle_hint({"key": "curious"}))
        self.assertIn("布局", persona.intent_idle_hint({"key": "focused"}))
        self.assertIn("玩笑", persona.intent_idle_hint({"key": "playful"}))
        self.assertEqual(persona.intent_idle_hint({"key": "unknown"}), "")

    def test_bad_rng_does_not_raise(self):
        class Bad:
            def random(self):
                raise RuntimeError("boom")
        self.assertIsNone(persona.pick_intent(Bad(), chance=1.0))


class NightTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertIn("深夜", persona.night_note(23))
        self.assertIn("深夜", persona.night_note(2))
        self.assertIn("还没完全醒", persona.night_note(6))
        self.assertEqual(persona.night_note(12), "")
        self.assertEqual(persona.night_note(22), "")


class AdaptiveNoteTest(unittest.TestCase):
    def test_only_down(self):
        self.assertIn("放轻", persona.adaptive_soft_note(3, 2))
        self.assertEqual(persona.adaptive_soft_note(2, 3), "")
        self.assertEqual(persona.adaptive_soft_note(2, 2), "")
        self.assertEqual(persona.adaptive_soft_note("x", 2), "")


class RecallTest(unittest.TestCase):
    NOW = 1_700_000_000.0

    def test_when_phrase(self):
        for days, word in ((0.5, "今天"), (1.5, "昨天"), (5, "前几天"),
                           (20, "上个月"), (400, "更早以前")):
            self.assertEqual(persona.when_phrase(self.NOW - days * 86400, self.NOW), word)

    def test_game_remark_uses_real_fields(self):
        rec = {"id": 3, "ts": self.NOW - 1.5 * 86400, "move_count": 87,
               "final_black_wr": 0.71, "summary": "这局你在右下角下得很漂亮。"}
        text = persona.library_remark(rec, self.NOW)
        self.assertIn("昨天", text)
        self.assertIn("87 手", text)
        self.assertIn("顺", text)
        self.assertIn("右下角下得很漂亮", text)

    def test_close_game(self):
        text = persona.library_remark({"ts": self.NOW, "move_count": 50,
                                       "final_black_wr": 0.50}, self.NOW)
        self.assertIn("咬得很紧", text)
        self.assertIn("没留下总结", text)

    def test_imported_game(self):
        text = persona.library_remark({"ts": self.NOW, "move_count": 120,
                                       "meta": {"source": "import"}}, self.NOW)
        self.assertIn("导进来", text)
        self.assertIn("120 手", text)
        self.assertNotIn("胜率", text)

    def test_review_intro_picks_worst_loss(self):
        text = persona.review_intro(80, [
            {"move_no": 12, "drop": 0.08}, {"move_no": 44, "drop": 0.23}])
        self.assertIn("第 44 手", text)
        self.assertIn("23%", text)

    def test_review_intro_without_losses(self):
        self.assertIn("没留下明显", persona.review_intro(30, []))
        self.assertIn("空着", persona.review_intro(0, []))


class PrivacyTest(unittest.TestCase):
    def test_markers(self):
        self.assertTrue(persona.is_private_text("微信", "随便聊聊"))
        self.assertTrue(persona.is_private_text("账单 2026-10"))
        self.assertFalse(persona.is_private_text("Bilibili", "围棋教学视频"))

    def test_phone_email_long_id(self):
        self.assertTrue(persona.is_private_text("13800138000"))
        self.assertTrue(persona.is_private_text("a@b.com"))
        self.assertTrue(persona.is_private_text("订单号 123456789012"))
        self.assertFalse(persona.is_private_text("第 123 手 胜率 56%"))


class PersonaStoreTest(unittest.TestCase):
    def test_round_trip_and_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(os.path.join(d, "profile.db"))
            self.assertEqual(store.get_persona(), {})
            self.assertEqual(store.set_persona({
                "games_total": 4, "streak": -2, "interactions_total": 12,
                "last_result": "loss", "mood": {"v": 0.4, "c": 0.6, "a": 0.5},
            }), 5)
            got = store.get_persona()
            self.assertEqual(got["games_total"], 4)
            self.assertEqual(got["last_result"], "loss")
            self.assertEqual(got["mood"], {"v": 0.4, "c": 0.6, "a": 0.5})
            # 覆盖写：不重复建行
            store.set_persona({"games_total": 5})
            self.assertEqual(store.get_persona()["games_total"], 5)
        with tempfile.TemporaryDirectory() as d:
            bad = ProfileStore(os.path.join(d, "missing", "profile.db"))
            self.assertEqual(bad.get_persona(), {})
            blocker = os.path.join(d, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("not a database")
            bad2 = ProfileStore(blocker)
            self.assertEqual(bad2.set_persona({"a": 1}), 0)


class EmotionLanguageTest(unittest.TestCase):
    """v4.3.0（C）：情绪只翻译成语言节奏；基线附近不改变提示词。"""

    def test_baseline_is_empty(self):
        self.assertEqual(persona.emotion_language_note({"v": 0.58, "c": 0.55, "a": 0.30}), "")

    def test_low_valence_short_and_soft(self):
        note = persona.emotion_language_note({"v": 0.30, "c": 0.40, "a": 0.20})
        self.assertIn("句子短一点", note)
        self.assertIn("不用为了热络硬撑", note)
        self.assertNotIn("0.30", note)          # 不报数值

    def test_bright_and_active(self):
        note = persona.emotion_language_note({"v": 0.85, "c": 0.70, "a": 0.75})
        self.assertIn("轻快", note)
        self.assertIn("问句最多一个", note)

    def test_dirty_values(self):
        self.assertEqual(persona.emotion_language_note({"v": "x", "c": 0.5, "a": 0.5}), "")
        self.assertEqual(persona.emotion_language_note(None), "")

    def test_note_keeps_persona_constraints(self):
        note = persona.emotion_language_note({"v": 0.30, "c": 0.40, "a": 0.20})
        self.assertIn("Azure", note)
        self.assertIn("不卖萌", note)

    def test_chat_budget(self):
        self.assertEqual(persona.chat_budget({"v": 0.58, "c": 0.55, "a": 0.30}), 400)
        self.assertEqual(persona.chat_budget({"v": 0.30, "c": 0.40, "a": 0.20}), 340)
        self.assertEqual(persona.chat_budget({"v": 0.85, "c": 0.70, "a": 0.75}), 460)
        self.assertEqual(persona.chat_budget({"v": 0.60, "c": 0.80, "a": 0.40}), 440)
        self.assertEqual(persona.chat_budget({"v": "x"}), 400)


if __name__ == "__main__":
    unittest.main()
