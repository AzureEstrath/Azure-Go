"""玩家私人事实守卫（v4.3.3）：不替玩家编造「刚吃完晚饭」这类没有依据的经历。"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import personal_facts as pf

NOW_1559 = datetime.datetime(2026, 10, 30, 15, 59).timestamp()
NOW_1900 = datetime.datetime(2026, 10, 30, 19, 0).timestamp()


class MealClaimTest(unittest.TestCase):
    def test_screenshot_dinner_claim_is_unsupported(self):
        ev = pf.collect_evidence(
            history=[{"role": "user", "content": "今天几点吃饭合适啊Azure"}],
            now=NOW_1559)
        claim = pf.unsupported("刚吃完晚饭没多久吧……要是还饿，吃点水果或者喝杯热牛奶就好啦。", ev)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["kind"], "meal")
        self.assertEqual(claim["slot"], "dinner")
        self.assertEqual(claim["reason"], "time", "15:59 说刚吃完晚饭应先按时间不合理处理")

    def test_evening_guess_still_needs_user_evidence(self):
        ev = pf.collect_evidence(
            history=[{"role": "user", "content": "今天几点吃饭合适啊Azure"}],
            now=NOW_1900)
        self.assertIsNotNone(pf.unsupported("你刚吃完晚饭吧？", ev),
                             "即便在晚饭点，没有玩家原话也不能当成已知")

    def test_user_statement_is_evidence(self):
        ev = pf.collect_evidence(
            history=[
                {"role": "user", "content": "我今天六点就吃完晚饭了"},
                {"role": "assistant", "content": "好呀"},
            ],
            now=NOW_1900)
        self.assertIsNone(pf.unsupported("刚吃完晚饭没多久吧……", ev))

    def test_user_question_is_not_evidence(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "我晚饭吃过了吗"}], now=NOW_1900)
        self.assertIsNotNone(pf.unsupported("刚吃完晚饭没多久吧……", ev),
                             "玩家反问自己吃没吃，不能当成她已经吃过")

    def test_negative_cue_wins(self):
        ev = pf.collect_evidence(
            history=[
                {"role": "user", "content": "我不吃了，已经退掉了"},
                {"role": "user", "content": "今天几点吃饭合适啊Azure"},
            ],
            now=NOW_1900)
        claim = pf.unsupported("刚吃完晚饭没多久吧……", ev)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["reason"], "negative")

    def test_stale_negative_cue_is_ignored(self):
        history = ([{"role": "user", "content": "我饿了"}] +
                   [{"role": "user", "content": "随便聊一句%d" % i} for i in range(8)] +
                   [{"role": "user", "content": "今天几点吃饭合适啊Azure"}])
        ev = pf.collect_evidence(history=history, now=NOW_1900)
        claim = pf.unsupported("刚吃完晚饭没多久吧……", ev)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["reason"], "no_source", "很久以前的「饿了」不能当这次的反向线索")
        line = pf.fallback_reply(claim, ev)
        self.assertNotIn("取消", line)
        self.assertNotIn("还没吃", line)

    def test_questions_and_advice_are_not_claims(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "今天几点吃饭合适"}], now=NOW_1559)
        for text in ("你吃过了吗？", "晚饭六点半到七点吃比较合适。",
                     "吃饱了才有力气继续做事。", "要是还饿，吃点水果就好。"):
            self.assertIsNone(pf.unsupported(text, ev), text)

    def test_azure_self_claim_is_not_a_user_fact(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "你吃了吗"}], now=NOW_1900)
        self.assertIsNone(pf.unsupported("我刚吃完晚饭，你呢？", ev))


class OtherClaimTest(unittest.TestCase):
    def test_work_claim_requires_user_words(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "今天有点累"}], now=NOW_1559)
        self.assertIsNotNone(pf.unsupported("你刚下班吧，先歇会儿。", ev))
        ev2 = pf.collect_evidence(history=[{"role": "user", "content": "我刚下班"}], now=NOW_1559)
        self.assertIsNone(pf.unsupported("你刚下班吧，先歇会儿。", ev2))

    def test_health_claim_requires_user_words(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "今天有点累"}], now=NOW_1559)
        self.assertIsNotNone(pf.unsupported("你感冒了还撑着？", ev))


class ReplyTest(unittest.TestCase):
    def test_correction_prompt_names_the_fact_without_system_talk(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "今天几点吃饭合适"}], now=NOW_1559)
        claim = pf.unsupported("刚吃完晚饭没多久吧……", ev)
        text = pf.correction_prompt(claim, ev)
        self.assertIn("没有说过", text)
        self.assertIn("不要替玩家编造", text)
        self.assertIn("不要提系统", text)

    def test_fallback_answers_without_claim(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "今天几点吃饭合适"}], now=NOW_1559)
        claim = pf.unsupported("刚吃完晚饭没多久吧……", ev)
        line = pf.fallback_reply(claim, ev)
        self.assertNotIn("刚吃完", line)
        self.assertIn("晚饭", line)

    def test_fallback_negative_mentions_cancelled_takeout(self):
        ev = pf.collect_evidence(history=[{"role": "user", "content": "我不吃了，已经退掉了"}], now=NOW_1900)
        claim = pf.unsupported("刚吃完晚饭没多久吧……", ev)
        line = pf.fallback_reply(claim, ev)
        self.assertIn("取消", line)
        self.assertNotIn("刚吃完", line)


if __name__ == "__main__":
    unittest.main()
