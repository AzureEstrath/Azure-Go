"""Azure 的基础审美与长期偏好（v4.6.1）：纯规则 / 聊天接线 / 落盘。

全部不触网：LLM / 引擎都是假的，profile.db 用临时目录。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import aesthetics, events
from app.agent import AzureAgent
from app.profile_store import ProfileStore
from helpers import StubEngine, StubLLM, ZeroRng


class SeqLLM:
    """按顺序返回预设台词的假 LLM；用来看「审美重答」有没有真的发生。"""

    think_mode = 0
    base_url = "http://localhost:8080/v1"

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    async def chat(self, messages, max_tokens=300, no_think=False, think_mode=None):
        self.calls.append((list(messages), max_tokens, no_think))
        if self.texts:
            return self.texts.pop(0)
        return "我其实没玩过几款二次元手游，说不出具体名字。"


class PureTest(unittest.TestCase):
    def test_default_axes_are_in_range(self):
        st = aesthetics.default_state()
        self.assertEqual(set(st["axes"]), set(aesthetics.AXES))
        for v in st["axes"].values():
            self.assertGreaterEqual(v, aesthetics.AXIS_MIN)
            self.assertLessEqual(v, aesthetics.AXIS_MAX)
        self.assertEqual(st["works"], [])

    def test_sanitize_dirty_state(self):
        st = aesthetics.sanitize({
            "axes": {"mood": 3.0, "craft": "坏值", "liveliness": -1},
            "works": [{"name": "星月夜", "owner": "user", "verdict": 9, "tags": ["mood", "乱写"]},
                      {"name": "", "owner": "azure"}, {"name": "星月夜", "owner": "user"}],
            "events": -5,
        })
        self.assertEqual(st["axes"]["mood"], aesthetics.AXIS_MAX)
        self.assertGreaterEqual(st["axes"]["craft"], aesthetics.AXIS_MIN)
        self.assertEqual(st["axes"]["liveliness"], aesthetics.AXIS_MIN)
        self.assertEqual(len(st["works"]), 1)
        self.assertEqual(st["works"][0]["verdict"], 1)
        self.assertEqual(st["works"][0]["tags"], ["mood"])
        self.assertEqual(st["events"], 0)

    def test_preference_question_rule(self):
        self.assertTrue(aesthetics.is_preference_question("Azure按你的审美最喜欢哪个器者"))
        self.assertTrue(aesthetics.is_preference_question("你最喜欢哪个二次元手游"))
        self.assertTrue(aesthetics.is_preference_question("你觉得这个画好看吗"))
        self.assertFalse(aesthetics.is_preference_question("今天天气怎么样"))
        self.assertFalse(aesthetics.is_preference_question("我今晚想吃面"))

    def test_user_preference_from_short_answer(self):
        hist = [
            {"role": "assistant", "content": "那你最喜欢哪幅画呢？"},
            {"role": "user", "content": "星月夜吧"},
        ]
        st = aesthetics.merge_user_evidence(aesthetics.default_state(), hist)
        self.assertEqual(len(st["works"]), 1)
        self.assertEqual(st["works"][0]["name"], "星月夜")
        self.assertEqual(st["works"][0]["owner"], "user")
        self.assertEqual(st["works"][0]["verdict"], 1)
        self.assertTrue(aesthetics.has_user_preference(st))

    def test_user_explicit_like_and_dislike(self):
        st = aesthetics.merge_user_evidence(aesthetics.default_state(), [], "我最喜欢星月夜")
        self.assertEqual(st["works"][0]["verdict"], 1)
        st = aesthetics.merge_user_evidence(st, [], "我不太喜欢原神，太热闹了")
        spot = [w for w in st["works"] if w["name"] == "原神"][0]
        self.assertEqual(spot["owner"], "user")
        self.assertEqual(spot["verdict"], -1)
        self.assertTrue(aesthetics.has_user_preference(st))

    def test_user_preference_does_not_change_azure_axes(self):
        base = aesthetics.default_state()
        st = aesthetics.merge_user_evidence(base, [], "我最喜欢热闹的抽卡游戏")
        self.assertEqual(st["axes"], base["axes"])

    def test_learn_from_own_reply_moves_axes_and_records_work(self):
        reply = ("我选《千里江山图》~ 青绿山水的意境，既有骨气又温柔，很合我的眼缘。")
        st, changed = aesthetics.learn_from_reply(aesthetics.default_state(), reply,
                                                  "Azure按你的审美最喜欢哪个器者")
        self.assertTrue(changed)
        self.assertGreater(st["axes"]["structure"], aesthetics.BASELINE["structure"])
        self.assertGreater(st["axes"]["gentleness"], aesthetics.BASELINE["gentleness"])
        row = [w for w in st["works"] if w["name"] == "千里江山图"][0]
        self.assertEqual(row["owner"], "azure")
        self.assertEqual(row["verdict"], 1)
        for v in st["axes"].values():
            self.assertGreaterEqual(v, aesthetics.AXIS_MIN)
            self.assertLessEqual(v, aesthetics.AXIS_MAX)

    def test_negative_own_reply_lowers_axis(self):
        st, changed = aesthetics.learn_from_reply(
            aesthetics.default_state(), "我不太喜欢原神，太热闹了。", "你觉得原神怎么样")
        self.assertTrue(changed)
        self.assertLess(st["axes"]["liveliness"], aesthetics.BASELINE["liveliness"])
        row = [w for w in st["works"] if w["name"] == "原神"][0]
        self.assertEqual(row["verdict"], -1)
        self.assertEqual(row["owner"], "azure")

    def test_unknown_own_choice_is_remembered(self):
        st, changed = aesthetics.learn_from_reply(
            aesthetics.default_state(),
            "我选了《星穹铁道》~ 它的画面和叙事挺合我的眼缘。",
            "你最喜欢哪个二次元手游")
        self.assertTrue(changed)
        row = [w for w in st["works"] if w["name"] == "星穹铁道"][0]
        self.assertEqual(row["owner"], "azure")
        self.assertEqual(row["verdict"], 1)
        self.assertGreater(st["axes"]["story"], aesthetics.BASELINE["story"])
        self.assertGreater(st["axes"]["craft"], aesthetics.BASELINE["craft"])

    def test_taste_note_has_no_numbers_and_has_own_standard(self):
        st = aesthetics.merge_user_evidence(aesthetics.default_state(), [], "星月夜吧")
        note = aesthetics.taste_note(st, "你最喜欢哪个二次元手游")
        self.assertIn("先按自己的眼光，不按热度", note)
        self.assertIn("你更看重", note)
        self.assertIn("星月夜", note)
        self.assertIn("先说出你按什么标准看", note)
        self.assertIn("不要用热度、销量、大众评价作理由", note)
        self.assertNotIn("0.", note)
        self.assertNotIn("星月夜吧", note)          # 只记对象，不把原句塞进提示词
        short = aesthetics.taste_note(st, "今天有点累")
        self.assertNotIn("先说出你按什么标准看", short)

    def test_strip_echo_question(self):
        reply = "我选《千里江山图》~ 青绿山水的意境，很合我的眼缘。你呢？"
        self.assertEqual(aesthetics.strip_echo_question(reply, True),
                         "我选《千里江山图》~ 青绿山水的意境，很合我的眼缘。")
        self.assertIn("你呢？", aesthetics.strip_echo_question(reply, False))
        # 真正的新问题不删
        fresh = "我更喜欢安静的。你最近还喜欢别的画吗？"
        self.assertIn("你最近还喜欢别的画吗", aesthetics.strip_echo_question(fresh, True))
        # 只剩一句反问时不硬删成空
        only = "你呢？"
        self.assertEqual(aesthetics.strip_echo_question(only, True), only)

    def test_needs_taste_retry(self):
        self.assertTrue(aesthetics.needs_taste_retry("我选原神", "你最喜欢哪个二次元手游"))
        self.assertTrue(aesthetics.needs_taste_retry("原神。", "你最喜欢哪个二次元手游"))
        self.assertFalse(aesthetics.needs_taste_retry(
            "我选《千里江山图》~ 青绿山水的意境，很合我的眼缘。", "你最喜欢哪个器者"))
        self.assertFalse(aesthetics.needs_taste_retry(
            "我其实没玩过几款二次元手游，说不出具体名字。", "你最喜欢哪个二次元手游"))
        self.assertFalse(aesthetics.needs_taste_retry("我选原神", "今天天气怎么样"))

    def test_fit_score_prefers_quiet_game(self):
        st = aesthetics.default_state()
        light = aesthetics.fit_score(st, "光遇")
        genshin = aesthetics.fit_score(st, "原神")
        self.assertIsNotNone(light)
        self.assertIsNotNone(genshin)
        self.assertGreater(light, genshin)
        self.assertIsNone(aesthetics.fit_score(st, "不存在的作品"))

    def test_taste_fallback_lists_criteria_not_a_hot_title(self):
        out = aesthetics.taste_fallback(aesthetics.default_state(), "你最喜欢哪个二次元手游")
        self.assertIn("按我自己的审美", out)
        self.assertIn("更看重", out)
        self.assertIn("不太来电", out)
        self.assertNotIn("原神", out)
        art = aesthetics.taste_fallback(aesthetics.default_state(), "你最喜欢哪幅画")
        self.assertNotIn("接触不多", art)
        self.assertIn("只凭热度", art)


class CountingStore:
    """只记 set_persona 次数的假画像库（验证「没有新判断就不写库」）。"""

    def __init__(self):
        self.calls = []

    def set_persona(self, items):
        self.calls.append(dict(items))
        return len(items)


class AgentAestheticTest(unittest.TestCase):
    def _agent(self, llm, profile_path):
        agent = AzureAgent(StubEngine(), llm, events.RecordingBus(),
                           joseki_rate=1.0, joseki_rng=ZeroRng(),
                           persona_enabled=True, aesthetic_enabled=True)
        agent.profile_store = ProfileStore(profile_path)
        return agent

    def test_chat_does_not_reask_known_user_preference(self):
        reply = ("我选《千里江山图》~ 青绿山水的意境，既有骨气又温柔，很合我的眼缘。你呢？")
        with tempfile.TemporaryDirectory() as d:
            agent = self._agent(StubLLM(text=reply), os.path.join(d, "profile.db"))
            agent.st.chat_history = [
                {"role": "assistant", "content": "那你最喜欢哪幅画呢？"},
                {"role": "user", "content": "星月夜吧"},
                {"role": "assistant", "content": "梵高啊……那种把情绪揉进笔触的感觉，确实迷人。"},
            ]
            asyncio.run(agent.chat("Azure按你的审美最喜欢哪个器者", "pet"))
            msgs = agent.bus.messages(events.AI)
            self.assertTrue(msgs)
            text = str(msgs[-1].get("text") or "")
            self.assertNotIn("你呢", text)
            self.assertIn("千里江山图", text)
            names = {(w["owner"], w["name"]) for w in agent._aesthetic["works"]}
            self.assertIn(("user", "星月夜"), names)
            self.assertIn(("azure", "千里江山图"), names)
            saved = agent.profile_store.get_persona()["aesthetic"]
            self.assertIn("千里江山图", json.dumps(saved, ensure_ascii=False))

    def test_preference_question_retries_without_own_standard(self):
        with tempfile.TemporaryDirectory() as d:
            llm = SeqLLM(["我选原神。",
                          "我没怎么玩过二次元手游。按我的审美，会更偏爱安静、有留白、"
                          "手作感强的类型，热闹抽卡向的不太合眼缘。"])
            agent = self._agent(llm, os.path.join(d, "profile.db"))
            asyncio.run(agent.chat("你最喜欢哪个二次元手游", "pet"))
            msgs = agent.bus.messages(events.AI)
            text = str(msgs[-1].get("text") or "")
            self.assertIn("安静", text)
            self.assertGreaterEqual(len(llm.calls), 2)

    def test_retry_still_hot_title_uses_taste_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            llm = SeqLLM(["我选原神。", "原神。"])
            agent = self._agent(llm, os.path.join(d, "profile.db"))
            asyncio.run(agent.chat("你最喜欢哪个二次元手游", "pet"))
            text = str(agent.bus.messages(events.AI)[-1].get("text") or "")
            self.assertIn("更看重", text)
            self.assertNotIn("我选原神", text)

    def test_disabled_aesthetic_keeps_reply_and_no_state(self):
        with tempfile.TemporaryDirectory() as d:
            agent = self._agent(StubLLM(text="我选《千里江山图》。你呢？"),
                                os.path.join(d, "profile.db"))
            agent.aesthetic_enabled = False
            agent.st.chat_history = [{"role": "user", "content": "星月夜吧"}]
            asyncio.run(agent.chat("按你的审美呢", "pet"))
            text = str(agent.bus.messages(events.AI)[-1].get("text") or "")
            self.assertIn("你呢", text)
            self.assertEqual(agent._aesthetic["works"], [])

    def test_load_persona_restores_aesthetic(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(os.path.join(d, "profile.db"))
            store.set_persona({"aesthetic": {
                "axes": {"mood": 0.91},
                "works": [{"name": "星月夜", "owner": "azure", "verdict": 1,
                           "tags": ["mood"], "ts": 1.0}],
            }})
            agent = self._agent(StubLLM(), os.path.join(d, "profile.db"))
            asyncio.run(agent.load_persona())
            self.assertGreater(agent._aesthetic["axes"]["mood"], aesthetics.BASELINE["mood"])
            self.assertEqual(agent._aesthetic["works"][0]["name"], "星月夜")

    def test_aesthetic_note_empty_when_persona_off(self):
        agent = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                           joseki_rate=1.0, joseki_rng=ZeroRng())
        self.assertEqual(agent.aesthetic_note("你最喜欢哪个二次元手游"), "")

    def test_chat_prompt_carries_aesthetic_and_known_user_pref(self):
        with tempfile.TemporaryDirectory() as d:
            agent = self._agent(StubLLM(text="我选《千里江山图》，很合我的眼缘。"),
                                os.path.join(d, "profile.db"))
            agent.st.chat_history = [
                {"role": "assistant", "content": "那你最喜欢哪幅画呢？"},
                {"role": "user", "content": "星月夜吧"},
            ]
            asyncio.run(agent.chat("Azure按你的审美最喜欢哪个器者", "pet"))
            system = str(agent.llm.calls[0][0][0].get("content") or "")
            self.assertIn("你的审美", system)
            self.assertIn("先按自己的眼光", system)
            self.assertIn("星月夜", system)

    def test_no_rewrite_when_nothing_new(self):
        with tempfile.TemporaryDirectory() as d:
            agent = self._agent(StubLLM(), os.path.join(d, "profile.db"))
            store = CountingStore()
            agent.profile_store = store
            agent.st.chat_history = [{"role": "user", "content": "星月夜吧"}]
            agent._aesthetic = aesthetics.merge_user_evidence(
                aesthetics.default_state(), [], "星月夜吧")
            agent._learn_and_save_aesthetics("今天天气怎么样", "今天还挺好的。")
            self.assertEqual(store.calls, [])

            reply = "我选《千里江山图》~ 青绿山水的意境，既有骨气又温柔，很合我的眼缘。"
            agent._learn_and_save_aesthetics("Azure按你的审美最喜欢哪个器者", reply)
            self.assertEqual(len(store.calls), 1)
            agent._learn_and_save_aesthetics("Azure按你的审美最喜欢哪个器者", reply)
            self.assertEqual(len(store.calls), 1, "同一判断重复出现不应反复写库或反复学")


if __name__ == "__main__":
    unittest.main()
