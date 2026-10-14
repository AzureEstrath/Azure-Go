"""AzureAgent 编排校验（stub 引擎/LLM，不触网、不起子进程）。

断言对齐 AzureGoGodot/tests/test_e2e.gd：手数、棋盘、快照、带标签的记忆、
无调用错误、事件形状（尤其 ai_move 带坐标）。
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.agent import AzureAgent
from helpers import BoomLLM, ErrLLM
from helpers import mk_agent as _mk


class FlowTest(unittest.TestCase):
    def test_full_turn_matches_e2e_expectations(self):
        async def run():
            agent, eng, llm, bus = _mk()
            await agent.player_move(3, 3)
            await agent.player_move(15, 15)
            return agent, eng, bus

        agent, eng, bus = asyncio.run(run())
        st = agent.st

        # 手数与棋盘（对齐 test_e2e）
        self.assertEqual(len(st.moves), 4)
        self.assertEqual(st.board.at(3, 3), "black")
        counts = st.board.count()
        self.assertGreaterEqual(counts["white"], 2)
        self.assertEqual(counts["black"], 2)

        # 快照与 payload
        self.assertEqual(len(st.snapshots), 2)
        payload = st.payload()
        self.assertEqual(len(payload["stones"]), payload["move_count"])

        # 带标签的记忆：E（Estarth 的棋）与 A（Azure 的棋）
        tags = {str(m.get("tag", "")) for m in st.memory_log}
        self.assertIn("E", tags)
        self.assertIn("A", tags)

        # 无 LLM 调用错误
        joined = " ".join(e["text"] for e in bus.messages())
        self.assertNotIn("调用出错", joined)

        # ai_move 事件必须带坐标（音画同步的前提）
        moves = bus.messages(events.AI_MOVE)
        self.assertEqual(len(moves), 2)
        for e in moves:
            self.assertIn("move", e)
            self.assertTrue(0 <= e["move"]["x"] < 19)
            self.assertFalse(e["move"]["pass"])

        # 状态与忙碌事件
        self.assertGreaterEqual(len(bus.of_type("state")), 2)
        busy_seq = [e["busy"] for e in bus.of_type("busy")]
        self.assertEqual(busy_seq, [True, False, True, False])

        # 情绪事件（落子结算即时推送）
        self.assertTrue(bus.of_type("mood"))

    def test_state_converges_with_reveal(self):
        """落子宣告延迟揭示：内部棋谱先落子，reveal_move 才发可见状态。"""

        async def run():
            agent, eng, llm, bus = _mk()
            await agent.player_move(3, 3)
            return agent, bus

        agent, bus = asyncio.run(run())
        # 内部棋谱已含她这一手
        self.assertEqual(len(agent.st.moves), 2)
        self.assertTrue(agent._reveal_pending)
        before = len(bus.of_type("state"))
        agent.reveal_move()
        self.assertFalse(agent._reveal_pending)
        self.assertEqual(len(bus.of_type("state")), before + 1)

    def test_illegal_move_does_not_touch_engine(self):
        async def run():
            agent, eng, llm, bus = _mk()
            agent.st.board.play(0, 0, "black")
            await agent.player_move(0, 0)
            return agent, eng, bus

        agent, eng, bus = asyncio.run(run())
        self.assertEqual(len(eng.calls), 0)
        msgs = [e["text"] for e in bus.messages(events.AI)]
        self.assertTrue(any("这手不成立" in t for t in msgs))
        self.assertTrue(bus.of_type("state"))

    def test_pre_analysis_cache_avoids_extra_call(self):
        """第一手后缓存的「她落子后」分析，正好是下一手玩家落子前局面，应被复用。"""

        async def run():
            agent, eng, llm, bus = _mk()
            await agent.player_move(3, 3)
            n_after_first = len(eng.calls)
            cached_pos = eng.calls[-1]         # 第一手「她落子后」的局面（= 第二手玩家落子前局面）
            await agent.player_move(15, 15)
            return n_after_first, len(eng.calls), cached_pos, eng

        n1, n2, cached_pos, eng = asyncio.run(run())
        # 第二手不再重算「落子前局面」（命中缓存），只需「落子后」两次分析：
        # 一次给应手选点、一次给她落子后的信息栏 —— 与 Godot 版一致。
        self.assertEqual(n2 - n1, 2)
        # 缓存命中的直接证据：第二手期间没有再分析那个已缓存的局面
        self.assertEqual(
            [c for c in eng.calls if c == cached_pos], [cached_pos],
            "第二手不应重算已缓存的「落子前局面」",
        )

    def test_peek_removed(self):
        agent, _eng, _llm, _bus = _mk()
        self.assertFalse(hasattr(agent, "peer_remark"))

    def test_idle_remark_still_works(self):
        """删掉窥屏后，闲置主动搭话必须照常（Review Focus 第四条）。"""

        async def run():
            agent, _eng, _llm, bus = _mk()
            await agent.idle_remark()
            return bus

        bus = asyncio.run(run())
        self.assertEqual(len(bus.messages(events.AI_IDLE)), 1)


class ControlTest(unittest.TestCase):
    def test_undo_restores_snapshot(self):
        async def run():
            agent, eng, llm, bus = _mk()
            await agent.player_move(3, 3)
            bus.clear()
            agent.undo(0)
            return agent, bus

        agent, bus = asyncio.run(run())
        self.assertEqual(len(agent.st.moves), 0)
        self.assertEqual(len(agent.st.snapshots), 0)
        self.assertTrue(bus.of_type("state"))
        joined = " ".join(e["text"] for e in bus.messages(events.AI))
        self.assertIn("退回第0手", joined)

    def test_set_allow_pass_and_reset(self):
        async def run():
            agent, eng, llm, bus = _mk()
            agent.set_allow_pass(True)
            await agent.player_move(3, 3)
            bus.clear()
            agent.reset_game()
            return agent, bus

        agent, bus = asyncio.run(run())
        self.assertEqual(len(agent.st.moves), 0)
        self.assertTrue(agent.st.allow_pass)          # 重开保留虚着设置
        self.assertTrue(bus.of_type("state"))
        joined = " ".join(e["text"] for e in bus.messages(events.AI))
        self.assertIn("重新来吧", joined)

    def test_chat_and_analyze_and_summary(self):
        async def run():
            agent, eng, llm, bus = _mk()
            await agent.chat("你好呀~")
            await agent.analyze_position()            # 空盘提示，不调引擎
            await agent.player_move(3, 3)
            await agent.summarize("辛苦啦")
            return agent, eng, bus

        agent, eng, bus = asyncio.run(run())
        self.assertTrue(agent.st.finished)
        texts = [e["text"] for e in bus.messages(events.AI)]
        self.assertTrue(any("棋盘还空着" in t for t in texts))
        self.assertTrue(any(t.startswith("📋 对局总结") for t in texts))
        self.assertEqual(agent.st.chat_history[0]["role"], "user")


class CleanReplyTest(unittest.TestCase):
    def setUp(self):
        self.agent, _, _, _ = _mk()

    def test_removes_stage_direction_with_point(self):
        self.assertEqual(self.agent.clean_reply("好的（白棋落在 C4）我们继续"), "好的我们继续")

    def test_keeps_parenthetical_without_move_hint(self):
        text = "嗯~（我有点累了）先歇歇"
        self.assertEqual(self.agent.clean_reply(text), text)

    def test_empty_result_falls_back_to_original(self):
        self.assertEqual(self.agent.clean_reply("（落在 C4）"), "（落在 C4）")

    def test_collapses_blank_lines(self):
        self.assertEqual(self.agent.clean_reply("一\n\n\n\n二"), "一\n\n二")


class HelperTest(unittest.TestCase):
    def test_py_list_and_pct(self):
        self.assertEqual(AzureAgent._py_list(["Q16", "D4"]), "['Q16', 'D4']")
        self.assertEqual(AzureAgent._pct(0.512), "51.2%")

    def test_llm_failed_criteria(self):
        self.assertFalse(AzureAgent._llm_failed("嗯~ 挺好"))
        self.assertTrue(AzureAgent._llm_failed("调用出错：HTTP 500"))
        self.assertTrue(AzureAgent._llm_failed("唔...我脑子有点转不过来了"))
        self.assertTrue(AzureAgent._llm_failed(""))

    def test_chat_event_mapping(self):
        self.assertEqual(AzureAgent._chat_event("laugh"), "chat_laugh")
        self.assertEqual(AzureAgent._chat_event("unknown"), "chat_normal")

    def test_chat_emotion_keywords(self):
        self.assertEqual(AzureAgent.chat_emotion("哈哈哈太好玩了"), "laugh")
        self.assertEqual(AzureAgent.chat_emotion("谢谢你，好温柔"), "delight")
        self.assertEqual(AzureAgent.chat_emotion("今天好难过"), "sad")
        self.assertEqual(AzureAgent.chat_emotion("为什么呀？"), "puzzled")
        self.assertEqual(AzureAgent.chat_emotion("嗯嗯"), "pleased")

    def test_pick_ai_move_prefers_first_legal(self):
        agent, _, _, _ = _mk()
        pick = agent._pick_ai_move([{"move": "Q16"}, {"move": "D4"}])
        self.assertEqual(pick, ["Q16", 15, 3])

    def test_pick_ai_move_fallback_is_legal(self):
        agent, _, _, _ = _mk()
        # 候选全部非法（占满 Q16）：允许虚着则应选择 pass
        agent.st.allow_pass = True
        pick = agent._pick_ai_move([{"move": "pass"}])
        self.assertEqual(pick[0], "pass")

    def test_pick_ai_move_finds_nearby_when_no_candidates(self):
        agent, _, _, _ = _mk()
        pick = agent._pick_ai_move([])
        self.assertNotEqual(pick[0], "pass")
        x, y = pick[1], pick[2]
        self.assertEqual(agent.st.board.at(x, y), "")
        self.assertTrue(agent.st.board.is_legal(x, y, "white")["ok"])
        self.assertEqual((x, y), (9, 9))          # 空盘：就近中心


class FailSafeTest(unittest.TestCase):
    """云端 LLM 失败 / 未配置时：busy 必须归位，健康面必须可用。"""

    # —— 断言工具 ——

    def _run(self, llm, flow):
        """用失败的云端 LLM 跑一条流程，返回 (agent, bus)。"""
        async def run():
            agent, _eng, _llm, bus = _mk()
            agent.llm = llm
            await flow(agent)
            return agent, bus

        return asyncio.run(run())

    def _run_after_move(self, llm, flow):
        """先正常下一手铺出局面，再换成失败的云端 LLM 跑目标流程（bus 已清空）。"""
        async def run():
            agent, _eng, _llm, bus = _mk()
            await agent.player_move(3, 3)
            agent.llm = llm
            bus.clear()
            await flow(agent)
            return agent, bus

        return asyncio.run(run())

    def _assert_no_error_text_and_unlocked(self, tag, agent, bus):
        """玩家可见文本里不许出现「调用出错」，且 busy 必须归位（否则棋盘永久锁死）。"""
        for e in bus.messages():
            self.assertNotIn("调用出错", str(e.get("text", "")), "%s：错误文案漏给了玩家" % tag)
        self.assertFalse(agent.busy, "%s：busy 没归位" % tag)
        self.assertFalse(bus.of_type("busy")[-1]["busy"], "%s：最后一个 busy 事件应为 False" % tag)

    def _assert_says_something(self, tag, bus, kind):
        """该流程必须有一句非空的兜底台词顶上来。"""
        texts = [str(e.get("text", "")).strip() for e in bus.messages(kind)]
        self.assertTrue(texts, "%s：应有一句兜底台词" % tag)
        self.assertTrue(all(t != "" for t in texts), "%s：兜底台词不能为空" % tag)

    def test_busy_released_when_llm_raises(self):
        agent, eng, _llm, bus = _mk()
        agent.llm = BoomLLM()

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        self.assertFalse(agent.busy)
        self.assertFalse(bus.of_type("busy")[-1]["busy"])

    def test_move_degrades_cleanly(self):
        """点评 / 落子宣告退化为兜底台词；她的应手照常（Review Focus #2/#3）。"""
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run(llm, lambda a: a.player_move(3, 3))
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            self._assert_says_something(tag, bus, events.AI_COMMENT)     # 点评有兜底
            self.assertTrue(bus.messages(events.AI_MOVE), tag + "：落子宣告必须照常")
            self.assertEqual(len(agent.st.moves), 2, tag + "：她该下的一手不能丢")
            payload = agent.st.payload()
            self.assertEqual(len(payload["stones"]), payload["move_count"], tag + "：棋盘状态必须自洽")

    def test_hint_degrades_cleanly(self):
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run(llm, lambda a: a.hint())
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            self._assert_says_something(tag, bus, events.AI_COMMENT)

    def test_analyze_degrades_cleanly(self):
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run_after_move(llm, lambda a: a.analyze_position())
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            self._assert_says_something(tag, bus, events.AI)

    def test_chat_degrades_cleanly(self):
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run(llm, lambda a: a.chat("你好呀~"))
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            self._assert_says_something(tag, bus, events.AI)
            # 兜底台词要进聊天历史，下一轮上下文才自洽
            self.assertEqual(agent.st.chat_history[-1]["role"], "assistant", tag)

    def test_summarize_degrades_cleanly(self):
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run_after_move(llm, lambda a: a.summarize("辛苦啦"))
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            texts = [str(e.get("text", "")) for e in bus.messages(events.AI)]
            self.assertTrue(any(t.startswith("📋 对局总结") for t in texts), tag)
            self._assert_says_something(tag, bus, events.AI)


if __name__ == "__main__":
    unittest.main()
