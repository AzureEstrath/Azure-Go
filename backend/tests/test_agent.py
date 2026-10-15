"""AzureAgent 编排校验（stub 引擎/LLM，不触网、不起子进程）。

断言对齐 AzureGoGodot/tests/test_e2e.gd：手数、棋盘、快照、带标签的记忆、
无调用错误、事件形状（尤其 ai_move 带坐标）。
"""

import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.agent import AzureAgent
from helpers import BoomLLM, ErrLLM, StubEngine, StubLLM
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


class FakeMemory:
    def __init__(self, text=""):
        self.text = text
        self.games = []

    @property
    def available(self):
        return True

    def recall_for(self, query, k=None):
        return self.text

    def remember_game(self, moves, result, summary):
        self.games.append((len(moves), result, summary))


class MemoryWiringTest(unittest.TestCase):
    def test_constructor_accepts_memory(self):
        """构造参数接口：main.py 的 lifespan 用 ``memory=`` 注入。"""
        mem = FakeMemory()
        agent = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(), memory=mem)
        self.assertIs(agent.memory, mem)

    def test_no_memory_keeps_long_term_empty(self):
        agent, _eng, llm, _bus = _mk()
        agent.memory = None

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        for msgs, _mt, _nt in llm.calls:
            for m in msgs:
                if m["role"] == "user":
                    self.assertNotIn("长期记忆", m["content"])

    def test_memory_text_reaches_prompts(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = FakeMemory("Estarth 喜欢点三三")

        async def run():
            await agent.chat("你好")

        asyncio.run(run())
        # 真实断言：至少一条 user 消息里带上了召回文本
        sent = [m["content"] for c in agent.llm.calls for m in c[0] if m["role"] == "user"]
        self.assertTrue(any("Estarth 喜欢点三三" in s for s in sent))

    def test_memory_text_reaches_ai_turn_context(self):
        """她这一回合（_ai_turn）也要带召回文本——这条走 with_context 的透传路径。"""
        agent, _eng, _llm, _bus = _mk()
        agent.memory = FakeMemory("Estarth 喜欢点三三")

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        sent = [m["content"] for c in agent.llm.calls for m in c[0] if m["role"] == "user"]
        self.assertTrue(any("Estarth 喜欢点三三" in s for s in sent))

    def test_summary_writes_long_term_memory(self):
        agent, _eng, _llm, _bus = _mk()
        mem = FakeMemory()
        agent.memory = mem

        async def run():
            await agent.player_move(3, 3)
            await agent.summarize("测试备注")

        asyncio.run(run())
        self.assertEqual(len(mem.games), 1)
        self.assertGreater(mem.games[0][0], 0)

    def test_summary_fallback_is_not_stored(self):
        """云端失败时的兜底台词不算总结：不得写进长期记忆（记忆污染）。"""
        agent, _eng, _llm, _bus = _mk()
        mem = FakeMemory()
        agent.memory = mem
        agent.llm = ErrLLM()

        async def run():
            await agent.player_move(3, 3)
            await agent.summarize("辛苦啦")

        asyncio.run(run())
        self.assertEqual(len(mem.games), 0)


class ThreadProbeMemory:
    """记录召回 / 写入在哪个线程被调用：记忆调用必须走工作线程，不得阻塞事件循环。"""

    def __init__(self):
        self.threads = []
        self.games = []

    @property
    def available(self):
        return True

    def recall_for(self, query, k=None):
        self.threads.append(threading.current_thread())
        return ""

    def remember_game(self, moves, result, summary):
        self.threads.append(threading.current_thread())
        self.games.append((len(moves), result, summary))


class MemoryOffLoopTest(unittest.TestCase):
    """记忆调用必须走 ``asyncio.to_thread``：慢 embedding 不得冻住整个事件循环（I2）。"""

    def test_memory_calls_run_off_the_event_loop(self):
        agent, _eng, _llm, _bus = _mk()
        mem = ThreadProbeMemory()
        agent.memory = mem
        seen = {}

        async def run():
            seen["loop"] = threading.current_thread()
            await agent.player_move(3, 3)       # _ai_turn 里的召回
            await agent.chat("你好")            # chat 里的召回
            await agent.summarize("备注")       # summarize 里的写入

        asyncio.run(run())
        self.assertGreaterEqual(len(mem.threads), 3)
        for t in mem.threads:
            self.assertIsNot(t, seen["loop"], "记忆调用不得跑在事件循环线程上")


class BoomMemory:
    """召回与写入都抛异常的替身：记忆故障只该打日志，绝不能让这一回合卡死。"""

    @property
    def available(self):
        return True

    def recall_for(self, query, k=None):
        raise RuntimeError("云端 embedding 401")

    def remember_game(self, moves, result, summary):
        raise RuntimeError("库坏了")


class MemoryFailureTest(unittest.TestCase):
    """记忆故障（I2 的守卫路径）：回合照常走完、``busy`` 归位、台词照常发出。"""

    def test_raising_memory_keeps_turns_alive(self):
        agent, _eng, _llm, bus = _mk()
        agent.memory = BoomMemory()

        async def run():
            await agent.player_move(3, 3)
            await agent.chat("你好")
            await agent.summarize("备注")

        asyncio.run(run())
        self.assertFalse(agent.busy)                                        # busy 必须归位
        self.assertTrue(bus.messages(events.AI_MOVE))                       # 落子宣告照常
        texts = [str(e.get("text", "")) for e in bus.messages(events.AI)]
        self.assertTrue(any(t.startswith("📋 对局总结") for t in texts))     # 总结照常发出


if __name__ == "__main__":
    unittest.main()


# —— 以下为计划 04 · Task 2 追加（既有断言一个字符都没改；追加在 guard 之后，
#    是为了让上面的既有内容保持逐字节不变——`unittest.main()` 在本仓的唯一跑法
#    `python -m unittest discover -s tests` 下不会执行）——


class GraphNodeExceptionTest(unittest.TestCase):
    """S9：图里的节点**真的抛出**时，`busy` 必须归位、棋盘不得被永久锁死。

    与 `FailSafeTest` 不同，这里不是「LLM 返回错误文案」而是异常从 `graph.ainvoke` 里冒出来：
    把节点会调用的 `agent._pick_ai_move`（decide 节点）与 `agent.katago.analyze`（prepare 节点）
    换成抛异常的替身。异常只有公开方法的 `try/finally` 兜得住——去掉那对 `try/finally`，
    下面的 busy 断言与「随后还能照常出话」都会红。
    """

    def test_node_exception_releases_busy_and_unlocks(self):
        agent, _eng, _llm, bus = _mk()

        def boom(_infos):
            raise RuntimeError("节点炸了（模拟工具超时 / 返回解析失败）")

        agent._pick_ai_move = boom
        asyncio.run(agent.player_move(3, 3))

        # 异常确实发生在图中间：玩家那手的点评已发，她那一手还没宣告
        self.assertTrue(bus.messages(events.AI_COMMENT), "点评阶段应已跑完")
        self.assertEqual(bus.messages(events.AI_MOVE), [], "异常应发生在落子宣告之前")

        # ① busy 归位 ② busy 事件序列最后一条是 False（且没有多余的 True/False）
        self.assertFalse(agent.busy)
        seq = [e["busy"] for e in bus.of_type("busy")]
        self.assertTrue(seq)
        self.assertIs(seq[-1], False)
        self.assertEqual(seq, [True, False])

        # ③ 棋盘没被永久锁死：还原替身、换一句**不重复**的新台词（S28：与 Task 4 的 8 条窗口去重兼容——
        #    StubLLM 恒定复读同一句本就该被拦下，那是设计行为、不是「锁死」），随后照样能出话
        del agent._pick_ai_move                 # 删掉实例上的替身，回到类方法
        agent.llm.text = "异常之后我还在…"       # 换成窗口里没有的新台词，避免被判重复抑制
        before = len(bus.messages())
        asyncio.run(agent.hint())
        self.assertFalse(agent.busy)
        self.assertGreater(len(bus.messages()), before, "异常之后必须还能正常出话")
        self.assertIs([e["busy"] for e in bus.of_type("busy")][-1], False)

    def test_engine_exception_degrades_with_fallback_line(self):
        """引擎在节点里抛出：公开方法的兜底路径照旧给玩家一句可读台词，且 busy 归位。"""

        async def boom(_moves):
            raise RuntimeError("引擎连接被拒绝")

        agent, _eng, _llm, bus = _mk()
        agent.katago.analyze = boom
        asyncio.run(agent.hint())

        self.assertFalse(agent.busy)
        self.assertIs([e["busy"] for e in bus.of_type("busy")][-1], False)
        texts = [str(e.get("text", "")).strip() for e in bus.messages(events.AI_COMMENT)]
        self.assertTrue(texts, "引擎异常时应有兜底台词")
        self.assertTrue(all(t != "" for t in texts))
        for e in bus.messages():
            self.assertNotIn("调用出错", str(e.get("text", "")))


# —— 以下为计划 04 · Task 4 追加（既有断言一个字符都没改）——


class DedupTest(unittest.TestCase):
    def test_repeat_within_window_is_suppressed(self):
        agent, _eng, llm, bus = _mk()
        sent = []

        async def run():
            for _ in range(2):
                bus.clear()
                await agent.chat("你好")
                sent.append([e["text"] for e in bus.messages(events.AI)])

        llm.text = "我在的~"
        asyncio.run(run())
        self.assertEqual(len(sent[0]), 1)
        self.assertEqual(len(sent[1]), 0)          # 隔一轮的原样复读被拦下

    def test_move_announcement_not_deduped(self):
        agent, _eng, llm, bus = _mk()
        llm.text = "我在D16补一手。"

        async def run():
            await agent.player_move(3, 3)
            first = len(bus.messages(events.AI_MOVE))
            await agent.player_move(15, 15)
            return first, len(bus.messages(events.AI_MOVE))

        first, second = asyncio.run(run())
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)                # 落子宣告不受复读抑制

    def test_window_is_eight(self):
        from app.agent import AI_DEDUP_WINDOW
        self.assertEqual(AI_DEDUP_WINDOW, 8)

    # —— 计划 04 · Task 4 评审 I1 / S30 追加：窗口上限与判据边界（纯追加）——

    def test_window_evicts_oldest_beyond_eight(self):
        """窗口上限**严格** 8：压 9 条互不相同的台词，最旧的那条必须被丢出。

        这条用例是硬规则 2「最近 8 条窗口」唯一的会失败守卫——把驱逐逻辑删掉、或把
        ``> AI_DEDUP_WINDOW`` 放宽（如 ``* 2``），下面的 ``len == 8`` 与
        ``_is_repeat("第1句") is False`` 都会红。
        """
        agent, _eng, _llm, _bus = _mk()
        for i in range(1, 10):                      # 压 9 条：第1句 ~ 第9句
            agent._note_line("第%d句" % i)
        self.assertEqual(len(agent._recent_lines), 8)          # 上限严格 8，不是 9
        self.assertFalse(agent._is_repeat("第1句"))            # 最旧（第1句）已被丢出窗口
        self.assertTrue(agent._is_repeat("第9句"))             # 刚说的这句仍在
        self.assertTrue(agent._is_repeat("第2句"))             # 只丢最旧一条，其余照旧在窗内

    def test_whitespace_is_normalized_before_compare(self):
        """判据与前端一致：``strip()`` 后**完全相等**才算重复（前后空白不影响）。"""
        agent, _eng, _llm, _bus = _mk()
        agent._note_line("我在的~")
        self.assertTrue(agent._is_repeat("  我在的~  "))        # strip 后完全相等 → 判重复
        self.assertTrue(agent._is_repeat("我在的~"))

    def test_blank_lines_neither_repeat_nor_enter_window(self):
        """空串 / 纯空白 / ``None``：既不算重复，也不进窗口（否则会污染窗口把真台词挤出去）。"""
        agent, _eng, _llm, _bus = _mk()
        self.assertFalse(agent._is_repeat(""))
        self.assertFalse(agent._is_repeat("   "))
        self.assertFalse(agent._is_repeat(None))
        agent._note_line("")
        agent._note_line("   ")
        agent._note_line(None)
        self.assertEqual(agent._recent_lines, [])              # 一条都没入窗

    def test_substring_is_not_a_repeat(self):
        """「包含但完全不等」不算重复：判据是相等，不是包含（与前端逐字一致）。"""
        agent, _eng, _llm, _bus = _mk()
        agent._note_line("我在的~")
        self.assertFalse(agent._is_repeat("我在的~，怎么啦？"))   # 以它开头但更长 → 不判重
        self.assertFalse(agent._is_repeat("我"))                # 只是子串 → 不判重
        self.assertFalse(agent._is_repeat("我不在的~"))          # 只差一个字 → 不判重

