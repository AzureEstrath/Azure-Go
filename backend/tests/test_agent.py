"""AzureAgent 编排校验（stub 引擎/LLM，不触网、不起子进程）。

断言对齐 AzureGoGodot/tests/test_e2e.gd：手数、棋盘、快照、带标签的记忆、
无调用错误、事件形状（尤其 ai_move 带坐标）。
"""

import asyncio
import contextlib
import io
import os
import random
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.agent import AzureAgent
from app.joseki_progress import JosekiProgress
from app.profile_store import ProfileStore
from helpers import BoomLLM, ErrLLM, StubEngine, StubLLM, ZeroRng
from helpers import FixedRng, PvStubEngine
from helpers import mk_agent as _mk


class _RecallMemory:
    """主动回忆的假记忆库：只实现 agent 用到的两个召回入口。"""

    available = True

    def __init__(self, items=None):
        self.items = list(items or [])
        self.queries = []

    def recall_for(self, query, k=None):
        return ""

    def recall_items_for(self, query, k=None):
        self.queries.append(str(query or ""))
        return [dict(x) for x in self.items]


class _StateLife:
    """只给 ``_life_fields`` 读的假生命状态（不碰真实 life.db）。"""

    def __init__(self, **kw):
        self.state = {
            "energy": 0.72, "boredom": 0.16, "affinity": 0.35,
            "trust": 0.50, "dominance": 0.50,
        }
        self.state.update(kw)


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

        # 带标签的记忆：E（玩家的棋）与 A（Azure 的棋）
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

    def test_peer_remark_uses_screen_context(self):
        """窥屏搭话：台词仍走 AI_IDLE，且 prompt 里真的带上了看到的窗口与画面描述。"""
        async def run():
            agent, _eng, llm, bus = _mk()
            await agent.peer_remark({"fg_title": "Bilibili", "ocr": "围棋教学视频",
                                     "vlm": "他正在看一个围棋教学视频"})
            return llm, bus

        llm, bus = asyncio.run(run())
        self.assertEqual(len(bus.messages(events.AI_IDLE)), 1)
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[-1][0])
        self.assertIn("Bilibili", joined)
        self.assertIn("围棋教学视频", joined)
        self.assertIn("玩家", joined)

    def test_peer_remark_filters_private_screen(self):
        """窥屏隐私：密码 / 聊天 / 手机号等一律不进口型提示词，只留「私人的东西」。"""
        async def run():
            agent, _eng, llm, _bus = _mk()
            await agent.peer_remark({"fg_title": "微信 - 张三", "ocr": "密码 13800138000",
                                     "vlm": "聊天记录和银行卡余额"})
            return llm

        llm = asyncio.run(run())
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[-1][0])
        for secret in ("微信", "密码", "13800138000", "聊天记录", "银行卡", "余额"):
            self.assertNotIn(secret, joined)
        self.assertIn("私人的东西", joined)

    def test_persona_note_disabled_by_default(self):
        agent, _eng, _llm, _bus = _mk()
        self.assertEqual(agent.persona_note(), "")

    def test_persona_note_relationship_off_by_default(self):
        """默认不开关系：即便已经下过很多局，提示词里也不出现关系阶段 / 称呼变化。"""
        agent, _eng, _llm, _bus = _mk()
        agent.persona_enabled = True
        agent._persona = {"games_total": 12, "streak": -2}
        note = agent.persona_note()
        self.assertNotIn("熟络", note)
        self.assertNotIn("关系", note)
        self.assertIn("输", note, "情绪记忆与关系开关无关，仍应保留")

    def test_persona_note_stage_and_streak(self):
        agent, _eng, _llm, _bus = _mk()
        agent.persona_enabled = True
        agent.persona_relationship_enabled = True
        agent.persona_address_name = "搭档"
        agent._persona = {"games_total": 12, "streak": -2}
        note = agent.persona_note()
        self.assertIn("熟络", note)
        self.assertIn("搭档", note)
        self.assertIn("输", note)
        self.assertIn("不要逐条念出来", note)

    def test_reset_game_carries_mood(self):
        """情绪余温：上一局结束时的情绪按 keep=0.45 带进新局，而不是直接清零。"""
        agent, _eng, _llm, _bus = _mk()
        agent.persona_enabled = True
        agent._lingering_mood = {"v": 0.9, "c": 0.3, "a": 0.8}
        agent.reset_game(silent=True)
        self.assertAlmostEqual(agent.mood.v, 0.58 + (0.9 - 0.58) * 0.45, delta=0.01)
        self.assertAlmostEqual(agent.mood.c, 0.55 + (0.3 - 0.55) * 0.45, delta=0.01)
        self.assertIsNone(agent._lingering_mood, "余温只带一次，随后清掉")

    def test_persist_persona_writes_store(self):
        with tempfile.TemporaryDirectory() as d:
            agent, _eng, _llm, _bus = _mk()
            agent.persona_enabled = True
            agent.profile_store = ProfileStore(os.path.join(d, "profile.db"))
            agent.mood.v, agent.mood.c, agent.mood.a = 0.4, 0.7, 0.6
            asyncio.run(agent._persist_persona({"data": {"wr": 0.3}}))
            got = agent.profile_store.get_persona()
            self.assertEqual(got["games_total"], 1)
            self.assertEqual(got["last_result"], "win")
            self.assertEqual(got["streak"], 1)
            self.assertEqual(got["mood"], {"v": 0.4, "c": 0.7, "a": 0.6})

    def test_idle_remark_still_works(self):
        """窥屏恢复后，普通闲置主动搭话仍然照常。"""

        async def run():
            agent, _eng, _llm, bus = _mk()
            await agent.idle_remark()
            return bus

        bus = asyncio.run(run())
        self.assertEqual(len(bus.messages(events.AI_IDLE)), 1)

    def test_idle_pet_prompt_is_assistant_like_and_gentle_on_mood(self):
        """桌宠是助手：开口基线要低，但闲置结算不能反复把她记成“被晾着”。"""

        async def run():
            agent, _eng, llm, bus = _mk()
            before = (agent.mood.v, agent.mood.c, agent.mood.a)
            await agent.idle_remark("pet")
            return agent, llm, bus, before

        agent, llm, bus, before = asyncio.run(run())
        self.assertTrue(bus.messages(events.AI_IDLE))
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[0][0])
        self.assertIn("桌面助手", joined)
        self.assertNotIn("已经有一段时间没有动静", joined)
        self.assertEqual(llm.think_modes[0], 0, "桌宠走神应走快速档，不为一句话等强思考")
        self.assertGreater(agent.mood.v, before[0] - 0.02,
                           "桌宠频繁开口不应每次都按完整 idle_bored 扣情绪")
        self.assertGreaterEqual(agent.mood.v, before[0] - 1e-9,
                                "桌宠 idle_near 不应把愉悦度往下拉")

    def test_idle_recall_chance_uses_mood_and_slow_state(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = _RecallMemory()
        agent.life = _StateLife(energy=0.72, boredom=0.16, affinity=0.35, trust=0.50)
        low = agent.idle_recall_chance()
        agent.life = _StateLife(energy=0.80, boredom=0.95, affinity=0.85, trust=0.80)
        agent.mood.v = 0.85
        high = agent.idle_recall_chance()
        self.assertGreater(high, low)
        self.assertLessEqual(high, 0.30)
        self.assertGreaterEqual(low, 0.03)

    def test_idle_memory_query_is_mood_conditioned(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = _RecallMemory()
        agent.mood.v = 0.90
        agent.mood.c = 0.50
        agent.mood.a = 0.30
        self.assertIn("温暖", agent.idle_memory_query())
        agent.mood.v = 0.20
        self.assertIn("陪伴", agent.idle_memory_query())
        agent.mood.v = 0.55
        agent.mood.c = 0.90
        self.assertIn("最近在忙", agent.idle_memory_query())

    def test_idle_memory_hint_deduplicates_recent_memories(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = _RecallMemory([
            {"id": 11, "text": "玩家上次说喜欢雨天摆棋。", "score": 0.9},
            {"id": 12, "text": "玩家提过想去棋院看看。", "score": 0.8},
        ])
        agent.recall_rng = FixedRng(0.0)       # 概率门必定通过
        h1 = agent.idle_memory_hint()
        h2 = agent.idle_memory_hint()
        h3 = agent.idle_memory_hint()
        self.assertEqual(h1["id"], 11)
        self.assertEqual(h2["id"], 12)
        self.assertEqual(h3, {}, "同一条旧事 30 分钟内不重复；两条都用过就这次不提")
        self.assertTrue(agent.memory.queries)

    def test_idle_memory_hint_respects_probability_gate(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = _RecallMemory([{"id": 1, "text": "旧事", "score": 1.0}])
        agent.recall_rng = FixedRng(0.99)      # 高于任何合理概率
        self.assertEqual(agent.idle_memory_hint(), {})

    def test_idle_pet_prompt_can_include_specific_memory(self):
        async def run():
            agent, _eng, llm, _bus = _mk()
            agent.memory = _RecallMemory([
                {"id": 21, "text": "玩家上次说喜欢雨天的棋盘。", "score": 0.9},
            ])
            agent.recall_rng = FixedRng(0.0)
            await agent.idle_remark("pet")
            return llm

        llm = asyncio.run(run())
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[0][0])
        self.assertIn("喜欢雨天的棋盘", joined)
        self.assertIn("不要编造", joined)

    def test_idle_memory_hint_works_with_real_memory_service(self):
        """真实 MemoryService + 临时库：没有 embedding 时关键词联想也能选中旧事。"""
        from app.config import AzureConfig
        from app.memory_service import MemoryService

        with tempfile.TemporaryDirectory() as d:
            cfg = AzureConfig()
            cfg.memory_enabled = True
            cfg.memory_db_path = os.path.join(d, "memory.db")
            cfg.memory_top_k = 3
            cfg.memory_max_inject_chars = 240
            mem = MemoryService(cfg)
            try:
                mem.remember_fact("玩家上次说雨天摆棋很开心", tag="test")
                agent, _eng, _llm, _bus = _mk()
                agent.memory = mem
                agent.mood.v = 0.90
                agent.recall_rng = FixedRng(0.0)
                hint = agent.idle_memory_hint()
                self.assertTrue(hint)
                self.assertIn("雨天", hint["text"])
                self.assertIsInstance(hint["id"], int)
            finally:
                mem.close()

    def test_idle_game_prompt_can_include_specific_memory(self):
        """M2：桌宠与围棋闲聊共用同一份主动回忆。"""

        async def run():
            agent, _eng, llm, _bus = _mk()
            agent.memory = _RecallMemory([
                {"id": 31, "text": "玩家上次说喜欢雨天摆棋。", "score": 0.9},
            ])
            agent.recall_rng = FixedRng(0.0)
            await agent.idle_remark("game")
            return llm

        llm = asyncio.run(run())
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[0][0])
        self.assertIn("喜欢雨天摆棋", joined)


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

    def test_reset_game_clears_all_per_game_state(self):
        """「重来」必须把本局的账清干净（v3.12.2 检查）：棋谱 / 逐手存档 / 亏损手 /
        快照 / 虚着揭示 / 情绪偏航 / 定式冷却 / 归档指纹 / 事件表，一个都不留。"""
        agent, _eng, _llm, _bus = _mk()
        st = agent.st
        st.add_move("B", "Q16")
        st.add_move("W", "D4")
        st.make_snapshot()
        st.note_move(1, "aim", "A", "先占角")
        st.record_bad_move(0, "Q16", "胜率下滑")
        st.record_loss("Q16", 0.3, "胜率下滑")
        st.log_memory("E", "这手有点问题", 1)
        st.chat_history.append({"role": "user", "content": "你好"})
        st.pre_analysis = {"n": 1, "a": "x"}
        st.last_katago = "黑好"
        st.finished = True
        agent._reveal_pending = True
        agent._drift_pending = {"line": "b", "gtp": "D4"}
        agent._library_sig = ("sig",)
        agent._joseki_taught = {"小雪崩": 1}
        agent.event_log.add("test", 1, "事件", "")

        agent.reset_game(silent=True)

        self.assertEqual(st.moves, [])
        self.assertEqual(st.move_seq, [])
        self.assertEqual(st.snapshots, [])
        self.assertEqual(st.move_notes, [])
        self.assertEqual(st.losses, [])
        self.assertEqual(st.bad_moves, [])
        self.assertEqual(st.memory_log, [])
        self.assertEqual(st.chat_history, [])
        self.assertIsNone(st.pre_analysis)
        self.assertEqual(st.last_katago, "")
        self.assertFalse(st.finished)
        self.assertFalse(agent._reveal_pending)
        self.assertIsNone(agent._drift_pending)
        self.assertIsNone(agent._library_sig)
        self.assertEqual(agent._joseki_taught, {})
        self.assertEqual(agent.event_log.all(), [])

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

    def test_chat_json_reply_is_naturalized(self):
        """模型误吐日程 JSON：聊天历史与 WS 台词都必须是可读中文，不能留 `[` / 字段名。"""
        llm = StubLLM(text='[{"date":"2026-10-28", "time":"08:00", '
                           '"title":"无机化学实验", "notes":"李老师 @综合楼103"}]')
        agent, bus = self._run(llm, lambda a: a.chat("刚刚你看到了什么"))
        reply = agent.st.chat_history[-1]["content"]
        self.assertEqual(reply, "嗯，记下了。")
        self.assertNotIn('"date"', reply)
        self.assertNotIn("[{", reply)
        texts = [e["text"] for e in bus.messages(events.AI)]
        self.assertTrue(any("记下了" in t and "date" not in t for t in texts), texts)

    def test_summarize_degrades_cleanly(self):
        for llm in (ErrLLM(), BoomLLM()):
            tag = type(llm).__name__
            agent, bus = self._run_after_move(llm, lambda a: a.summarize("辛苦啦"))
            self._assert_no_error_text_and_unlocked(tag, agent, bus)
            texts = [str(e.get("text", "")) for e in bus.messages(events.AI)]
            self.assertTrue(any(t.startswith("📋 对局总结") for t in texts), tag)
            self._assert_says_something(tag, bus, events.AI)


class _SeqLLM:
    """按顺序返回不同文本的假 LLM：第一条装日程 JSON，第二条装自然聊天（v4.2.5 重答门卫）。"""

    think_mode = 0
    base_url = "http://localhost:8080/v1"

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []
        self.think_modes = []

    async def chat(self, messages, max_tokens=300, no_think=False, think_mode=None):
        self.calls.append((list(messages), max_tokens, no_think))
        self.think_modes.append(think_mode)
        i = min(len(self.calls) - 1, len(self.texts) - 1)
        return self.texts[i]


class _FakeTTS:
    """只记录合成请求的假 TTS：返回 ``openai`` 形态，让 ``speech`` 事件能发出来。"""

    def __init__(self):
        self.calls = []

    async def speak(self, text):
        self.calls.append(text)
        return {"mode": "openai", "id": "chat-audio", "tone": ""}


class PetChatTest(unittest.TestCase):
    """v4.2.5：聊天自然接话 + 结构化重答 + 桌宠聊天只发文字（预制音在前端）。"""

    def _run(self, llm, text="明天晚上有数据科学课", mode="pet", prebuilt=False):
        async def run():
            agent, _eng, _llm, bus = _mk()
            agent.llm = llm
            tts = _FakeTTS()
            agent.tts = tts
            agent.pet_chat_tts_prebuilt = prebuilt
            await agent.chat(text, mode)
            return agent, bus, tts

        return asyncio.run(run())

    def test_chat_context_asks_for_natural_continuation(self):
        llm = StubLLM()
        _agent, _bus, _tts = self._run(llm)
        ctx = "\n".join(str(m.get("content", "")) for m in llm.calls[0][0])
        self.assertIn("自然接话", ctx)
        self.assertIn("不用整理成 JSON", ctx)
        self.assertIn("也可以从你们的共同记忆里", ctx)

    def test_structured_chat_reply_retried_without_thinking(self):
        llm = _SeqLLM([
            '[{"date":"2026-10-30","time":"19:00","title":"数据科学"}]',
            "嗯，明晚七点的数据科学，我记住了。你最近是不是有点累？",
        ])
        agent, bus, _tts = self._run(llm)
        self.assertGreaterEqual(len(llm.calls), 2, "结构化输出后应重答一次")
        self.assertEqual(llm.think_modes[1], 0, "重答必须是快速档（think_mode=0）")
        self.assertIn("不要输出 JSON", llm.calls[1][0][-1]["content"])
        reply = agent.st.chat_history[-1]["content"]
        self.assertNotIn('"date"', reply)
        self.assertTrue(any("数据科学" in str(e.get("text", "")) for e in bus.messages(events.AI)))

    def test_pet_chat_prebuilt_skips_tts(self):
        llm = StubLLM()
        _agent, bus, tts = self._run(llm, prebuilt=True)
        self.assertEqual(tts.calls, [], "桌宠聊天预制音模式不应调用 TTS 合成")
        self.assertEqual(bus.of_type("speech"), [])
        self.assertTrue(bus.messages(events.AI))

    def test_pet_chat_can_still_use_tts_when_switched_off(self):
        llm = StubLLM()
        _agent, bus, tts = self._run(llm, prebuilt=False)
        self.assertTrue(tts.calls, "开关关闭后聊天应恢复整句 TTS")
        self.assertTrue(bus.of_type("speech"))


class ReminderTest(unittest.TestCase):
    """事务提醒：到点把事务喂给 LLM；失败 / 无连接退回模板并照常发 ai_remind。"""

    ITEM = {"id": 1, "title": "高等数学", "when_text": "2026-10-28 周三 08:00"}

    def test_reminder_uses_llm_line_and_emits_ai_remind(self):
        agent, _eng, llm, bus = _mk()
        llm.text = "该去上课啦，别迟到。"
        text = asyncio.run(agent.remind_schedule(self.ITEM, now=1_790_000_000.0))
        self.assertEqual(text, "该去上课啦，别迟到。")
        msgs = bus.messages(events.AI_REMIND)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], text)
        self.assertIn("高等数学", llm.calls[-1][0][-1]["content"])

    def test_reminder_falls_back_without_llm(self):
        agent, _eng, _llm, bus = _mk()
        agent.llm = ErrLLM()
        text = asyncio.run(agent.remind_schedule(self.ITEM, now=1_790_000_000.0))
        self.assertIn("到点了", text)
        self.assertIn("高等数学", text)
        self.assertEqual(bus.messages(events.AI_REMIND)[0]["text"], text)

    def test_empty_title_does_not_emit(self):
        agent, _eng, _llm, bus = _mk()
        text = asyncio.run(agent.remind_schedule({"title": "  "}, now=1_790_000_000.0))
        self.assertEqual(text, "")
        self.assertEqual(bus.messages(events.AI_REMIND), [])


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
        agent.memory = FakeMemory("玩家喜欢点三三")

        async def run():
            await agent.chat("你好")

        asyncio.run(run())
        # 真实断言：至少一条 user 消息里带上了召回文本
        sent = [m["content"] for c in agent.llm.calls for m in c[0] if m["role"] == "user"]
        self.assertTrue(any("玩家喜欢点三三" in s for s in sent))

    def test_memory_text_reaches_ai_turn_context(self):
        """她这一回合（_ai_turn）也要带召回文本——这条走 with_context 的透传路径。"""
        agent, _eng, _llm, _bus = _mk()
        agent.memory = FakeMemory("玩家喜欢点三三")

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        sent = [m["content"] for c in agent.llm.calls for m in c[0] if m["role"] == "user"]
        self.assertTrue(any("玩家喜欢点三三" in s for s in sent))

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


# —— 以下为计划 04 · Task 5 追加（既有断言一个字符都没改）——


class StreamTest(unittest.TestCase):
    class StreamLLM(StubLLM):
        async def astream(self, messages, max_tokens=300):
            for piece in ["我", "在", "的~"]:
                yield piece

    def test_tokens_off_by_default(self):
        agent, _eng, _llm, bus = _mk()
        self.assertFalse(agent.stream_tokens)
        asyncio.run(agent.chat("在吗"))
        self.assertEqual(bus.of_type("token"), [])

    def test_tokens_emitted_when_enabled(self):
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.StreamLLM()
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))
        deltas = [e["delta"] for e in bus.of_type("token")]
        self.assertEqual("".join(deltas), "我在的~")
        self.assertTrue(all(e["turn_id"] == bus.of_type("token")[0]["turn_id"] for e in bus.of_type("token")))


# —— 以下为计划 04 · Task 5 评审 S31/S32/S33 的补充守卫（纯追加）——


class _Chunk:
    """流式 chunk 替身（DeepSeek-R1 形态：content 与推理段同块下发）。"""

    def __init__(self, content="", reasoning=""):
        self.content = content
        self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}
        self.response_metadata = {"model_name": "stub"}


class StreamGuardTest(unittest.TestCase):
    """S31 推理段永不进 delta / 默认关闭零 token；S33 turn_id 每次公开调用一次；S32 去重闸门不绕过。"""

    class ReasonLLM(StubLLM):
        """带推理段的流式替身：最后一块 content 为空（推理段还在继续）。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["你", "好", "~"]:
                yield _Chunk(piece, "内部推理段：我要不要理他")
            yield _Chunk("", "内部推理段：我要不要理他")

    class PrefixLLM(StubLLM):
        """先吐一整句（与窗口里那句重合），再续上后半句：以它开头的**新话**不许被误截。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["我在的~", "，怎么啦？"]:
                yield piece

    class TurnLLM(StubLLM):
        """每次调用换一句：`player_move` 三个阶段各自出话，便于核对 `turn_id` 是不是同一个。"""

        def __init__(self):
            super().__init__()
            self.n = 0

        async def astream(self, messages, max_tokens=300):
            self.n += 1
            for piece in ["第%d句" % self.n, "。"]:
                yield piece

    def test_reasoning_segment_never_leaks_into_delta(self):
        """硬规则 3：delta 只能来自 content 通道——带 `reasoning_content` 的块一个字都不许漏出去。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.ReasonLLM()
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))

        toks = bus.of_type("token")
        self.assertEqual("".join(e["delta"] for e in toks), "你好~")
        self.assertEqual(set(toks[0].keys()), {"type", "turn_id", "delta"})   # 新事件的自有形状
        for e in toks:
            self.assertNotIn("内部推理", str(e["delta"]))
        texts = [str(m["text"]) for m in bus.messages(events.AI)]
        self.assertEqual(texts, ["你好~"])                                    # message 仍是权威全文
        self.assertNotIn("内部推理", "".join(texts))

    def test_blank_content_chunk_emits_no_event(self):
        """S31②：空 content 的块（推理段还在继续）不发事件——4 块只出 3 条增量。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.ReasonLLM()
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))
        self.assertEqual(len(bus.of_type("token")), 3)

    def test_tokens_off_by_default_even_with_streamable_llm(self):
        """S31③：默认关闭时「能流式的模型」也一个 token 都不发，事件序列与普通模型逐字一致。"""
        a1, _e1, _l1, b1 = _mk()
        a1.llm = self.ReasonLLM()                # 模型侧能流式，但 stream_tokens 保持默认 false
        a2, _e2, _l2, b2 = _mk()                 # 普通 StubLLM（没有 astream）
        asyncio.run(a1.chat("在吗"))
        asyncio.run(a2.chat("在吗"))

        self.assertFalse(a1.stream_tokens)
        self.assertEqual(b1.of_type("token"), [])
        self.assertEqual(self._norm(b1.events), self._norm(b2.events))   # 类型 / 顺序 / 载荷全一致

    @staticmethod
    def _norm(events):
        """事件序列骨架：情绪三维向基线回落按真实时间算（浮点末位会抖），浮点归一到 6 位再比。"""
        def one(v):
            if isinstance(v, float):
                return round(v, 6)
            if isinstance(v, dict):
                return {k: one(x) for k, x in v.items()}
            if isinstance(v, list):
                return [one(x) for x in v]
            return v
        return [one(dict(e)) for e in events]

    def test_turn_id_is_one_per_public_call(self):
        """S33：`turn_id` 每次公开调用生成一次——`player_move` 的三阶段共用同一个。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.TurnLLM()
        agent.stream_tokens = True
        asyncio.run(agent.player_move(3, 3))

        toks = bus.of_type("token")
        self.assertTrue(toks, "三个阶段都该有流式增量")
        self.assertTrue(agent.turn_id)
        self.assertEqual({str(e["turn_id"]) for e in toks}, {agent.turn_id})   # 三阶段同一个
        self.assertEqual("".join(e["delta"] for e in toks), "第1句。第2句。第3句。")
        for m in bus.messages():                 # 既有 message 事件的形状一个字都没动
            self.assertNotIn("turn_id", m)

        # 硬规则 1 + 音画同步：流式开着也照旧——ai_move 带坐标，可见更新仍延后到 reveal_move()
        moves = bus.messages(events.AI_MOVE)
        self.assertEqual(len(moves), 1)
        self.assertEqual(set(moves[0]["move"].keys()), {"x", "y", "gtp", "pass"})
        self.assertTrue(agent._reveal_pending)
        before = len(bus.of_type("state"))
        agent.reveal_move()
        self.assertFalse(agent._reveal_pending)
        self.assertEqual(len(bus.of_type("state")), before + 1)

    def test_turn_id_changes_between_public_calls(self):
        """每次公开调用都是新的一轮：`turn_id` 不得沿用上一轮（配 speech / token 用）。"""
        agent, _eng, _llm, _bus = _mk()
        asyncio.run(agent.chat("在吗"))
        first = agent.turn_id
        self.assertTrue(first)
        asyncio.run(agent.hint())
        self.assertTrue(agent.turn_id)
        self.assertNotEqual(agent.turn_id, first)

    def test_dedup_gate_applies_to_streaming_path(self):
        """S32：流式路径同样过闸门——原样复读时 `message` 被拦，完整重复句也不出现在 token 流里。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = StreamTest.StreamLLM()       # 逐块吐「我」「在」「的~」
        agent.stream_tokens = True
        agent._note_line("我在的~")               # 最近 8 条窗口里已经有这句原话
        asyncio.run(agent.chat("在吗"))

        self.assertEqual(bus.messages(events.AI), [])              # 重复句不发 message
        deltas = [str(e["delta"]) for e in bus.of_type("token")]
        self.assertEqual(deltas, [])                              # S37：整句复读一条 token 都不发（不留残余气泡）

    def test_new_sentence_starting_with_a_window_line_is_not_truncated(self):
        """判据是「相等」不是「包含」：以窗口里那句开头的新话，流式下照样完整发出。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.PrefixLLM()
        agent.stream_tokens = True
        agent._note_line("我在的~")
        asyncio.run(agent.chat("在吗"))

        self.assertEqual("".join(str(e["delta"]) for e in bus.of_type("token")), "我在的~，怎么啦？")
        self.assertEqual([str(m["text"]) for m in bus.messages(events.AI)], ["我在的~，怎么啦？"])


class RealChainStreamTest(unittest.TestCase):
    """真链路径（``ChatOpenAI.astream``）也走同一条流式通道：MockTransport 回 SSE，全程不触网。

    其它用例用的是鸭子类型替身（对象上直接有 ``astream``）；这条走生产装配
    （``LLMClient.chain`` → ``_bind_stream`` 的 ``bind(extra_body=…)`` → ``astream``），
    顺带核对线上请求体仍是 ``stream: true`` + 我们注入的 ``max_tokens``。
    """

    def test_real_chain_streams_tokens_and_message_still_authoritative(self):
        import json
        import time
        import httpx
        from app.llm import LLMClient, REASON_HEADROOM_DEFAULT

        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "stub",
                       "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]}
                      for p in ["你", "好", "~"]]
            chunks.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "stub",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            body = "".join("data: %s\n\n" % json.dumps(c, ensure_ascii=False) for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=body.encode("utf-8"))

        llm = LLMClient("http://stub.local/v1", "stub-model", "k",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        bus = events.RecordingBus()
        agent = AzureAgent(StubEngine(), llm, bus, stream_tokens=True)
        agent._mood_ms = int(time.monotonic() * 1000)      # 情绪小调用限频：这轮跳过，只发这一次流式请求
        asyncio.run(agent.chat("在吗"))

        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0].get("stream"), True)                          # 走的真是流式
        self.assertEqual(seen[0]["max_tokens"], 400 + REASON_HEADROOM_DEFAULT)   # 输出上限照旧注入（含推理余量）
        toks = bus.of_type("token")
        self.assertEqual("".join(str(e["delta"]) for e in toks), "你好~")
        self.assertTrue(all(str(e["turn_id"]) == agent.turn_id for e in toks))
        self.assertEqual([str(m["text"]) for m in bus.messages(events.AI)], ["你好~"])   # 气泡以 message 为准


# —— 以下为 Task 5 首轮评审 S36/S37 的补充守卫（纯追加）——


class StreamThinkTest(unittest.TestCase):
    """S36：think 段被**切块**（含无配对开标签的收尾标记）时也不得漏进 delta 与 message。

    首轮的 `StreamGuardTest` 只喂 `additional_kwargs` 里的推理字段，抓不到「推理段写在 content
    通道、标签被切到不同块」这条路——下面的替身专门复现评审探针的两种形态。
    """

    class SplitTagLLM(StubLLM):
        """评审探针复现的形态：开标签被切块，随后是无配对开标签的收尾标记。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["<thi", "nk>内部推理：我要不要理他", "<｜end▁of▁thinking｜>", "你好~"]:
                yield piece

    class MarkerOnlyLLM(StubLLM):
        """无配对开标签：标记之前全是推理（标记自身也被切块）。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["<｜end", "▁of▁thinking｜>", "你好~"]:
                yield piece

    class PairTagLLM(StubLLM):
        """成对标签被切块：标签两侧的正文都要照常发出。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["先", "<th", "ink>推理</thi", "nk>后"]:
                yield piece

    class UnclosedLLM(StubLLM):
        """未闭合的开标签：`content_of` 的兜底是整条作废（空串）。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["<th", "ink>还没闭合"]:
                yield piece

    class WindowPrefixLLM(StubLLM):
        """只吐窗口那句的前缀（比窗口句短）：一条 token 都不发，但台词照旧出。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["我", "在"]:
                yield piece

    def _run(self, llm):
        agent, _eng, _llm, bus = _mk()
        agent.llm = llm
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))
        deltas = [str(e["delta"]) for e in bus.of_type("token")]
        texts = [str(m["text"]) for m in bus.messages(events.AI)]
        return agent, bus, deltas, texts

    def test_split_think_tag_never_leaks_across_chunks(self):
        """C1 核心：`<thi`/`nk>…` 被切块 + 收尾标记 ⇒ delta 与 message 都不含推理段。"""
        from app.llm_chain import content_of
        from types import SimpleNamespace

        _agent, _bus, deltas, texts = self._run(self.SplitTagLLM())

        # ① 推理段、标签、标记一个字都不许进事件
        self.assertTrue(deltas, "标记之后的正文该发出来")
        for s in deltas + texts:
            self.assertNotIn("内部推理", s)
            self.assertNotIn("think", s)
            self.assertNotIn("｜end", s)
        # ② 增量拼起来是最终台词的前缀（这里恰好相等）
        self.assertEqual("".join(deltas), "你好~")
        # ③ 最终 message 与「对同一条原文调 content_of」同构
        raw = "".join(["<thi", "nk>内部推理：我要不要理他", "<｜end▁of▁thinking｜>", "你好~"])
        self.assertEqual(texts, [content_of(SimpleNamespace(content=raw))])
        self.assertEqual(texts, ["你好~"])

    def test_marker_without_open_tag_hides_the_prefix(self):
        """无配对开标签的收尾标记：标记（含被切块的前半截）不漏，正文只在标记之后可见。"""
        _agent, _bus, deltas, texts = self._run(self.MarkerOnlyLLM())
        self.assertEqual(deltas, ["你好~"])
        self.assertEqual(texts, ["你好~"])

    def test_paired_tag_split_across_chunks_keeps_both_sides(self):
        """成对标签被切块：标签以外的正文（两侧）都完整发出，标签本身不漏。"""
        _agent, _bus, deltas, texts = self._run(self.PairTagLLM())
        self.assertEqual("".join(deltas), "先后")
        self.assertEqual(texts, ["先后"])
        for s in deltas:
            self.assertNotIn("推理", s)
            self.assertNotIn("think", s)

    def test_unclosed_think_falls_back_without_leaking(self):
        """未闭合 ⇒ `content_of` 兜底成空串 ⇒ 与不开流式一样由兜底台词顶上，推理段一个字不漏。"""
        _agent, _bus, deltas, texts = self._run(self.UnclosedLLM())
        self.assertEqual(deltas, [])
        self.assertTrue(texts and texts[0] != "")          # S16：不断言具体是哪一句兜底
        for s in texts:
            self.assertNotIn("还没闭合", s)
            self.assertNotIn("think", s)

    def test_repeat_prefix_holds_until_it_diverges(self):
        """S37：还是窗口句的前缀 ⇒ 继续扣留；分叉后积压的增量按序补发（顺序不乱、一条不少）。"""
        agent, _eng, _llm, bus = _mk()

        class DivergingLLM(StubLLM):
            async def astream(self, messages, max_tokens=300):
                for piece in ["我在的~", "，怎么啦？"]:
                    yield piece

        agent.llm = DivergingLLM()
        agent.stream_tokens = True
        agent._note_line("我在的~")
        asyncio.run(agent.chat("在吗"))

        deltas = [str(e["delta"]) for e in bus.of_type("token")]
        self.assertEqual(deltas, ["我在的~", "，怎么啦？"])
        self.assertEqual([str(m["text"]) for m in bus.messages(events.AI)], ["我在的~，怎么啦？"])

    def test_prefix_of_a_window_line_at_eof_emits_no_token_but_keeps_message(self):
        """S37 边界：整句只是窗口句的前缀（更短）⇒ 不发 token；台词照旧由 message 兜住（不丢话）。"""
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.WindowPrefixLLM()
        agent.stream_tokens = True
        agent._note_line("我在的~")
        asyncio.run(agent.chat("在吗"))

        self.assertEqual(bus.of_type("token"), [])
        self.assertEqual([str(m["text"]) for m in bus.messages(events.AI)], ["我在"])


# —— 以下为 Task 5 二轮复审 S38 的补充守卫（纯追加）——


class StreamMarkerFormTest(unittest.TestCase):
    """S38：开始/收尾标记形态，以及「保证边界」到底保证到哪。

    **更正记录**（本轮 `test_agent.py` 只许追加，故不改上面那条既有用例的 docstring）：
    `StreamThinkTest.test_marker_without_open_tag_hides_the_prefix` 覆盖的是「收尾标记是流的
    **第一块**」这个**退化子情形**——那时「标记之前全是推理」在标记到达前就已成立，delta 天然干净，
    它不能代表真实流序（标记必然最后到达）。真实流序分两类：`<｜begin▁of▁thinking｜>…<｜end…｜>`
    区间**保证不漏**（下面第一条用例）；网关吞掉开始标记、只剩 `CoT…<｜end…｜>` 属于 S38②
    **显式接受的降级**——只保证最终 `message`/TTS 干净（下面第二条用例）。
    """

    class BeginEndLLM(StubLLM):
        """真实流序 + 两个标记都被切块：区间整段（含标记）不外发。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["<｜beg", "in▁of▁thinking｜>", "内部推理：我要不要理他",
                          "<｜end▁of▁thi", "nking｜>", "你好~"]:
                yield piece

    class SwallowedBeginLLM(StubLLM):
        """降级形态：网关吞掉开始标记，只剩 CoT + 收尾标记（标记最后到达）。"""

        async def astream(self, messages, max_tokens=300):
            for piece in ["内部推理：我要不要理他", "<｜end▁of▁thinking｜>", "你好~"]:
                yield piece

    def _run(self, llm):
        agent, _eng, _llm, bus = _mk()
        agent.llm = llm
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))
        deltas = [str(e["delta"]) for e in bus.of_type("token")]
        texts = [str(m["text"]) for m in bus.messages(events.AI)]
        return agent, bus, deltas, texts

    def test_begin_end_interval_never_streams_reasoning_or_markers(self):
        """S38①：begin…end 跨块 ⇒ delta 里既没有推理正文、也没有任何标记，台词与 content_of 同构。"""
        from app.llm_chain import content_of
        from types import SimpleNamespace

        _agent, _bus, deltas, texts = self._run(self.BeginEndLLM())
        raw = "".join(["<｜beg", "in▁of▁thinking｜>", "内部推理：我要不要理他",
                       "<｜end▁of▁thi", "nking｜>", "你好~"])

        self.assertEqual(deltas, ["你好~"])
        for s in deltas + texts:
            self.assertNotIn("内部推理", s)
            self.assertNotIn("｜beg", s)
            self.assertNotIn("｜end", s)
            self.assertNotIn("▁of▁thinking", s)
        self.assertEqual(texts, ["你好~"])
        self.assertEqual(texts, [content_of(SimpleNamespace(content=raw))])   # 与整条原文同构

    def test_swallowed_begin_marker_still_yields_a_clean_message(self):
        """S38②：降级形态**只承诺最终台词干净**——这里只断言这一点（+ 标记永不外发）。

        保留真流式时不可能在收尾标记到达前判定「前面那些是推理」，故 token 流可能短暂显示推理原文
        （控制者已裁决接受）。**不**把「推理正文出现在 delta 里」写成期望行为的断言；能承诺的是：
        两个标记永不外发、最终 `message`（TTS 读的那份）与整条原文的 `content_of` 同构、
        气泡随后由 `message` 整条收口。
        """
        from app.llm_chain import content_of
        from types import SimpleNamespace

        _agent, _bus, deltas, texts = self._run(self.SwallowedBeginLLM())
        raw = "".join(["内部推理：我要不要理他", "<｜end▁of▁thinking｜>", "你好~"])

        self.assertEqual(texts, ["你好~"])                    # ← 这一形态下要守的就是这一条
        self.assertEqual(texts, [content_of(SimpleNamespace(content=raw))])
        for s in texts:
            self.assertNotIn("内部推理", s)
            self.assertNotIn("｜end", s)
        for s in deltas:                                      # 标记（含被切块的前半截）任何时候都不外发
            self.assertNotIn("｜end", s)
            self.assertNotIn("▁of▁thinking", s)


# ================= T5：入围规则接线（前 3 次都上卡片，第 4 次起只在走错时上） =================

class JosekiCardGateTest(unittest.TestCase):
    """端到端（真 ``JosekiProgress`` + 临时库）：入围结论落进 ``data["jk_show_card"]``。

    ``after_player`` 阶段既算「走错」（``jk_mistake``）又算入围，所以这里直接用公开的图入口
    （``_turn(PHASE_AFTER_PLAYER, ...)``）走一遍，断言下游 T6/T7 要消费的那个字段。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.progress = JosekiProgress(os.path.join(self.dir.name, "joseki_progress.db"))

    def tearDown(self):
        self.dir.cleanup()

    def _agent(self):
        # 必定讲（rate=1.0 + ZeroRng：画像 / 情绪的负浮动也压不过）：本组用例钉的是**资格层**
        # （族计数 / 走错），不是频率层；频率层的行为由 ``test_joseki_gate`` 显式设 rate + 注入 rng 覆盖。
        return AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                          joseki_progress=self.progress, joseki_rate=1.0, joseki_rng=ZeroRng())

    def _turn(self, agent, seed, x, y):
        """铺好角内既有手顺，再跑 ``after_player``（玩家那一手由这个阶段落到谱上）。"""
        from app import coords
        from app.agent_graph import PHASE_AFTER_PLAYER
        for color, gtp in seed:
            sx, sy = coords.gtp_to_xy(gtp)
            agent.st.board.play(sx, sy, "black" if color == "B" else "white")
            agent.st.add_move(color, gtp)
        agent._new_turn()
        return asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))

    def _entered_entry(self, agent):
        """角内第 1 手已在谱上，玩家再落 C17＝角内第 2 手 ⇒ 判 ``entered``（U52：角内恰好 2 手一律「进入」）。

        点三三这一族在第 3 手有真分岔（候选照旧带在事件里），但第一句说的是「这一角开始走」。
        """
        return self._turn(agent, [["W", "D16"]], 2, 2)

    def _deviated(self, agent):
        """角内 4 手吻合，玩家再落 B15 出线 ⇒ deviated（默认 stub 引擎下这手不在前 5 ⇒ 真亏）。"""
        return self._turn(agent, [["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]], 1, 4)

    def test_first_three_entries_show_the_card_the_fourth_does_not(self):
        for i in range(3):
            agent = self._agent()
            state = self._entered_entry(agent)
            with self.subTest(round=i + 1):
                self.assertEqual(str(state["data"]["jk"]["kind"]), "entered")
                self.assertEqual(str(state["data"]["jk"]["family"]), "star_33")
                self.assertIs(state["data"]["jk_show_card"], True)
                self.assertEqual(self.progress.seen("star_33"), i + 1, "跨局累计：每局只 +1")
                self.assertEqual(agent.st.joseki_counted, ["star_33"])
        agent = self._agent()
        state = self._entered_entry(agent)
        self.assertEqual(str(state["data"]["jk"]["family"]), "star_33")
        self.assertIs(state["data"]["jk_show_card"], False, "第 4 次进入这一族：不再上卡片")
        # 修复轮 1（评审 Important #1）：计数＝**教过几次**，不是「遇到几次」——第 4 次没讲 ⇒ 也不计数，
        # 所以 ``seen`` 停在 3（回归版「门控未命中也无条件计数」会给出 4）。
        self.assertEqual(self.progress.seen("star_33"), 3, "没讲就不计数（计数＝讲解次数）")

    def test_deviated_mistake_gets_the_card_after_the_third_time(self):
        for _ in range(3):
            self.progress.bump("star_33")                        # 前 3 次已经教过
        agent = self._agent()
        state = self._deviated(agent)
        self.assertEqual(str(state["data"]["jk"]["kind"]), "deviated")
        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], True)
        self.assertIs(state["data"]["jk_show_card"], True, "第 4 次但真的走错 ⇒ 上卡片")

    def test_deviated_without_mistake_gets_no_card_after_the_third_time(self):
        """不亏的偏离（她算的前 5 之内、胜率没掉）⇒ 第 4 次起不上卡片。"""
        for _ in range(3):
            self.progress.bump("star_33")

        class _FlatEngine(StubEngine):
            """把玩家那一手放进前 5，且胜率前后同值 ⇒ ``drop == 0`` 且 ``rank_ok`` ⇒ 不算走错。"""

            CAND = ["B15"] + StubEngine.CAND

        agent = self._agent()
        agent.katago = _FlatEngine()
        state = self._deviated(agent)
        self.assertEqual(str(state["data"]["jk"]["kind"]), "deviated")
        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], False)
        self.assertIs(state["data"]["jk_show_card"], False)

    def test_event_order_stays_state_joseki_mood(self):
        """U28：`jk_mistake` 块整体上移后，事件顺序仍是 ``state → joseki → mood``。"""
        agent = self._agent()
        self._deviated(agent)
        types = [str(e.get("type")) for e in agent.bus.events]
        self.assertLess(types.index("state"), types.index("joseki"))
        self.assertLess(types.index("joseki"), types.index("mood"))

    def test_no_store_keeps_teaching_every_time(self):
        """未接线（默认）⇒ 每次都上卡片，且不写任何计数状态。"""
        agent, _eng, _llm, _bus = _mk()
        self.assertIsNone(agent.joseki_progress)
        state = self._deviated(agent)
        self.assertIs(state["data"]["jk_show_card"], True)
        self.assertEqual(agent.st.joseki_counted, [])

    # —— T6：`move_no` 记在玩家这一手上，事件里带出来 ——

    def test_jk_mistake_records_the_players_one_based_move_number(self):
        """`data["jk_mistake"]["move_no"]` ＝玩家这一手的 1 基序号（4 手铺好 + 这手 ＝ 5）。"""
        agent = self._agent()
        state = self._deviated(agent)
        self.assertEqual(state["data"]["jk_mistake"]["move_no"], 5)
        self.assertEqual(state["data"]["jk_mistake"].get("gtp"), "B15")

    def test_deviated_event_carries_card_payload_and_move_no(self):
        """端到端：偏差那一手的 `joseki` 事件带出卡片载荷与走错注记（含 `move_no`）。"""
        for _ in range(3):
            self.progress.bump("star_33")                        # 前 3 次已教过 ⇒ 第 4 次只看是否真走错
        agent = self._agent()
        state = self._deviated(agent)

        jk = state["data"]["jk"]
        event = agent.bus.of_type("joseki")[-1]
        self.assertEqual(str(event["kind"]), "deviated")
        self.assertEqual(event["corner"], "左上")
        self.assertEqual(event["points"], [(5, 2)])              # U32：仍是「只点常规一手」
        self.assertIs(event["show_card"], True)
        # 透传：族 / 名 / k / 显示线逐键等于识别器那一份（line 的 `expect` 不在事件里）
        self.assertEqual(event["family"], str(jk["family"]))
        self.assertEqual(event["name"], str(jk["name"]))
        self.assertEqual(event["k"], int(jk["k"]))
        self.assertEqual(event["line"], jk["line"])
        self.assertEqual(event["mistake"], {
            "move_no": 5, "x": 1, "y": 4,
            "drop": state["data"]["jk_mistake"]["drop"],
        })
        self.assertTrue(event["line"])                           # 透传识别器的显示线
        self.assertEqual([set(c.keys()) for c in event["candidates"]], [{"name", "next"}] * len(event["candidates"]))


# ================= T6：`joseki` 事件扩字段（卡片载荷），旧字段路径一行不改 =================

class JosekiEventPayloadTest(unittest.TestCase):
    """`_joseki_emit` 直接发事件的形状：旧字段照旧、新字段齐全、`deviated` 行为不动（U30–U33）。"""

    def _emit(self, agent, r, **kw):
        agent._joseki_emit(r, **kw)
        return agent.bus.of_type("joseki")[-1]

    def test_legacy_fields_are_untouched_end_to_end(self):
        """同一 payload 经真事件总线出去：`type/kind/corner/points` 逐键相等。"""
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {"kind": "formed", "corner": "左上", "cat": "star",
                               "points": [(3, 3), (2, 2)]})
        self.assertEqual({k: e[k] for k in ("type", "kind", "corner", "points")},
                         {"type": "joseki", "kind": "formed", "corner": "左上",
                          "points": [(3, 3), (2, 2)]})

    def test_sparse_scan_result_gets_safe_defaults(self):
        """识别器只给旧字段时，卡片载荷全部安全缺省，且不抛。"""
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {"kind": "formed", "corner": "左上", "points": [(3, 3)]})
        self.assertEqual(e["family"], "")
        self.assertEqual(e["name"], "")
        self.assertEqual(e["k"], 0)
        self.assertEqual(e["line"], [])
        self.assertEqual(e["candidates"], [])
        self.assertIs(e["show_card"], False)
        self.assertIsNone(e["mistake"])

    def test_dirty_k_falls_back_to_zero(self):
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {"kind": "formed", "corner": "左上", "points": [(3, 3)],
                               "family": "star_33", "k": "x"})
        self.assertEqual(e["k"], 0)

    def test_candidates_keep_only_name_and_next(self):
        """U33：候选只透传 `name` / `next`，不把 `family` / `line` 塞进去；`next` 不手工转 list。"""
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {
            "kind": "entered", "corner": "左上", "points": [(3, 3)],
            "candidates": [{"name": "点三三·定形", "family": "star_33", "next": (4, 3),
                            "line": [{"x": 1, "y": 1, "c": "B"}]}],
        })
        self.assertEqual(e["candidates"], [{"name": "点三三·定形", "next": (4, 3)}])

    def test_deviated_points_replacement_is_unchanged(self):
        """U32：给了 `expect` ⇒ 事件 `points == [expect]`（前端高亮轨迹仍在用）。"""
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {"kind": "deviated", "corner": "左上", "points": [(9, 9)],
                               "expect": (5, 2)})
        self.assertEqual(e["points"], [(5, 2)])

    def test_deviated_without_expect_sends_nothing(self):
        """U32：`deviated` 且 `expect` 缺失 ⇒ 提前 return（不发事件），保持不变。"""
        agent, _eng, _llm, bus = _mk()
        agent._joseki_emit({"kind": "deviated", "corner": "左上", "points": [(9, 9)]})
        self.assertEqual(bus.of_type("joseki"), [])

    def test_empty_points_sends_nothing(self):
        agent, _eng, _llm, bus = _mk()
        agent._joseki_emit({"kind": "formed", "corner": "左上", "points": []})
        self.assertEqual(bus.of_type("joseki"), [])

    def test_mistake_payload_only_when_is_mistake_true(self):
        """U31：`jk_mistake` 的 `is_mistake` 为 True ⇒ 含 `move_no/x/y/drop`；False ⇒ None。"""
        agent, _eng, _llm, _bus = _mk()
        e = self._emit(agent, {"kind": "deviated", "corner": "左上", "points": [(9, 9)],
                               "expect": (5, 2), "name": "点三三·定形", "family": "star_33",
                               "k": 4, "line": [], "candidates": []},
                       show_card=True,
                       mistake={"is_mistake": True, "on_line": False, "gtp": "B15",
                                "x": 1, "y": 4, "drop": 0.203, "rank_ok": True,
                                "move_no": 5})
        self.assertEqual(e["mistake"], {"move_no": 5, "x": 1, "y": 4, "drop": 0.203})
        self.assertIs(e["show_card"], True)

        agent2, _e2, _l2, bus2 = _mk()
        agent2._joseki_emit({"kind": "formed", "corner": "左上", "points": [(3, 3)]},
                            show_card=False,
                            mistake={"is_mistake": False, "on_line": False, "gtp": "B15",
                                     "x": 1, "y": 4, "drop": 0.0, "rank_ok": True,
                                     "move_no": 5})
        e2 = bus2.of_type("joseki")[-1]
        self.assertIsNone(e2["mistake"])
        self.assertIs(e2["show_card"], False)

    def test_missing_or_dirty_mistake_is_none(self):
        agent, _eng, _llm, _bus = _mk()
        for bad in (None, "oops", {"is_mistake": True, "move_no": "x", "x": None,
                                   "y": "y", "drop": "z"}):
            with self.subTest(mistake=bad):
                agent.bus.clear()
                e = self._emit(agent, {"kind": "formed", "corner": "左上", "points": [(3, 3)]},
                               mistake=bad)
                if isinstance(bad, dict):
                    self.assertEqual(e["mistake"], {"move_no": 0, "x": 0, "y": 0, "drop": 0.0})
                else:
                    self.assertIsNone(e["mistake"])

    def test_default_call_signature_still_works(self):
        """U30：新增参数有安全缺省 ⇒ 既有单参调用照旧可用。"""
        agent, _eng, _llm, bus = _mk()
        agent._joseki_emit({"kind": "formed", "corner": "左上", "points": [(3, 3)]})
        self.assertEqual(len(bus.of_type("joseki")), 1)
        self.assertIsNone(bus.of_type("joseki")[-1]["mistake"])


# ================= 聊天事件（v3.8.0）：主题裁切 + 去重，默认零 LLM =================


class ChatEventTest(unittest.TestCase):
    """``chat()`` 之后把聊天按主题写进事件表：默认规则式（零 LLM）、去重、覆盖从开局起。"""

    def test_chat_digest_is_rule_based_and_needs_no_llm(self):
        """规则式主题裁切**零 LLM**；端到端 chat() 之后事件表里有 ``chat`` 事件。"""

        async def run():
            agent, _eng, llm, bus = _mk()
            await agent.chat("这手点三三的定式怎么样")
            return agent, llm, bus

        agent, llm, bus = asyncio.run(run())
        evs = [e for e in agent.event_log.all() if e["kind"] == "chat"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["detail"]["topic"], "定式")
        self.assertIn("定式", evs[0]["text"])
        # 事件原文永不进台词
        joined = " ".join(str(e.get("text", "")) for e in bus.messages())
        self.assertNotIn(evs[0]["text"], joined)

        # 直接调裁切步：一次模型调用都不发（假 LLM 计数不变）
        agent.st.chat_history = [{"role": "user", "content": "这手点三三的定式怎么样"}]
        before = len(llm.calls)
        written = agent._record_chat_events()
        self.assertEqual(len(llm.calls), before, "规则式主题裁切零 LLM 调用")
        self.assertEqual(written, 0, "同一主题 + 同一摘要 ⇒ 去重不写")

    def test_repeated_topic_not_written_again(self):
        """重复主题不写：同一轮聊天再来一遍，事件表不增。"""

        async def run():
            agent, _eng, llm, bus = _mk()
            await agent.chat("这手点三三的定式怎么样")
            await agent.chat("这手点三三的定式怎么样")
            return agent

        agent = asyncio.run(run())
        evs = [e for e in agent.event_log.all() if e["kind"] == "chat"]
        self.assertEqual(len(evs), 1, "主题 + 归一化摘要命中已存在 ⇒ 不写第二条")

    def test_chat_events_cover_from_opening(self):
        """覆盖从开局起：move 0 聊的主题与后来的主题都在表里（只增不减）。"""

        async def run():
            agent, _eng, llm, bus = _mk()
            await agent.chat("这手点三三的定式怎么样")     # move 0
            await agent.player_move(3, 3)                  # 2 手
            await agent.chat("我今天有点累")               # move 2，换个主题
            return agent

        agent = asyncio.run(run())
        evs = [e for e in agent.event_log.all() if e["kind"] == "chat"]
        self.assertEqual(len(evs), 2)
        self.assertIn(0, [e["move_no"] for e in evs], "开局那一轮也在表里")
        self.assertEqual({e["detail"]["topic"] for e in evs}, {"定式", "心情"})

    def test_digest_llm_defaults_off(self):
        agent, _eng, _llm, _bus = _mk()
        self.assertFalse(agent.chat_digest_llm, "chat_digest_llm 默认关")

    def test_digest_llm_only_at_summary_and_idle(self):
        """``chat_digest_llm=true``：落子 / 聊天回合内零额外调用，只在局末 / 空闲补做。"""

        async def run():
            agent, _eng, llm, bus = _mk()
            agent.chat_digest_llm = True
            hits = {"n": 0}
            orig = agent._digest_chat_llm

            async def spy(*args, **kw):
                hits["n"] += 1
                return await orig(*args, **kw)

            agent._digest_chat_llm = spy
            await agent.player_move(3, 3)                  # 落子回合：不补做
            self.assertEqual(hits["n"], 0, "落子回合内不许补做 LLM 摘要")
            await agent.chat("今天天气不错")               # 聊天回合：不补做
            self.assertEqual(hits["n"], 0, "聊天回合内不许补做 LLM 摘要")
            await agent.summarize("")                      # 局末：补做一次
            self.assertEqual(hits["n"], 1)
            await agent.idle_remark()                      # 空闲：再补做一次
            self.assertEqual(hits["n"], 2)
            return agent

        asyncio.run(run())

    def test_digest_llm_off_makes_no_extra_calls_in_move_turns(self):
        """开关本身不影响落子回合的引擎 / LLM 调用数（落子回合零额外调用）。"""

        async def run(flag):
            agent, _eng, llm, _bus = _mk()
            agent.chat_digest_llm = flag
            await agent.player_move(3, 3)
            return len(llm.calls), len(agent.katago.calls)

        self.assertEqual(asyncio.run(run(False)), asyncio.run(run(True)))


class ChatDigestEpochTest(unittest.TestCase):
    """v3.8.0 修复波 / Minor #2：补做摘要的那趟 LLM ``await`` 之后**再核一次代际**。

    摘要里的 ``move_no`` 取**写表那一刻**的 ``len(st.moves)``；不核代际的话，一份描述**旧**聊天的
    摘要会带上比它描述的那段聊天更新的手数（事件区块里就成了错位的「跨手脉络」素材）。
    """

    class _DigestLLM:
        think_mode = 0
        base_url = "https://api.example.com/v1"

        async def chat(self, messages, max_tokens=300, no_think=False):
            return "定式：聊了点三三"

    def _seeded(self, llm=None):
        agent, _eng, _llm, _bus = _mk()
        agent.chat_digest_llm = True
        agent.llm = llm if llm is not None else self._DigestLLM()
        agent.st.chat_history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "嗯~ 你好呀"},
        ]
        return agent

    def test_dead_epoch_writes_nothing_and_live_epoch_still_writes(self):
        agent = self._seeded()

        class _BumpingLLM(ChatDigestEpochTest._DigestLLM):
            async def chat(self, messages, max_tokens=300, no_think=False):
                agent.begin_epoch()                     # 模拟：这一趟 await 期间玩家起了新回合
                return "定式：聊了点三三"

        agent.llm = _BumpingLLM()
        token = agent.begin_epoch()
        n = asyncio.run(agent._digest_chat_llm(token))
        self.assertEqual(n, 0, "死代不许写")
        self.assertEqual(agent.event_log.all(), [], "死代一条都不许落表")

        # 对照：活代照写（证明上一条不是「什么都没发生」换来的）
        live = self._seeded()
        live_token = live.begin_epoch()
        self.assertEqual(asyncio.run(live._digest_chat_llm(live_token)), 1)
        self.assertEqual(len(live.event_log.all()), 1)

    def test_missing_token_keeps_legacy_behaviour(self):
        """取不到 token（老调用方 / 单测直接调）⇒ 跳过复核，行为与既有逐字一致。"""
        agent = self._seeded()
        self.assertEqual(asyncio.run(agent._digest_chat_llm()), 1)


class SlowLLM:
    """`chat` 慢返回（用于验证补做路径自带超时）。"""

    think_mode = 0
    base_url = "https://api.example.com/v1"

    async def chat(self, messages, max_tokens=300, no_think=False):
        await asyncio.sleep(1.0)
        return "定式：聊了点三三"


class ChatDigestFailureTest(unittest.TestCase):
    """修复轮 1 / Important #1：LLM **失败文案**（不抛异常）绝不许落进事件表。"""

    def _seeded(self):
        agent, _eng, _llm, _bus = _mk()
        agent.chat_digest_llm = True
        agent.st.chat_history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "嗯~ 你好呀"},
        ]
        return agent

    def test_llm_failure_text_never_enters_event_log(self):
        """假 LLM 返回「调用出错：…」⇒ 事件表零新增，提示词里也不出现该文案。"""
        from app import prompts

        agent = self._seeded()
        agent.llm = ErrLLM()                        # chat 返回「调用出错：HTTP 401（…）」，不抛
        n = asyncio.run(agent._digest_chat_llm())
        self.assertEqual(n, 0)
        self.assertEqual([e for e in agent.event_log.all() if e["kind"] == "chat"], [],
                         "失败文案绝不许落表")
        out = prompts.context_block(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                                    "chat", events=agent.event_log.all())
        self.assertNotIn("调用出错", out)
        self.assertNotIn("llm_base_url", out)

    def test_record_digest_lines_rejects_failure_lines(self):
        """第二道闸门：即便有人绕过 `_digest_chat_llm` 直接喂失败文案，也不写。"""
        agent = self._seeded()
        self.assertEqual(agent._record_digest_lines("调用出错：未配置 LLM 地址（llm_base_url）"), 0)
        self.assertEqual(agent.event_log.all(), [])
        # 正常行照旧能写（闸门不能把好行也挡掉）
        self.assertEqual(agent._record_digest_lines("定式：聊了点三三"), 1)
        self.assertEqual(len(agent.event_log.all()), 1)

    def test_record_digest_lines_keeps_only_the_legal_line(self):
        """第二道闸门的**单行** topic 判据：合法首行照写，失败次行**只丢它自己**（不是整段丢）。

        整段闸门看 `startswith("调用出错")`，所以「首行合法 + 次行是失败文案」不会被整段挡下——
        这一例正好单独钉住单行闸门：若不挡，失败文案会按「：」切成 `topic="调用出错"` 落进事件表，
        再经事件区块注入后续每一条提示词。
        """
        agent = self._seeded()
        n = agent._record_digest_lines("定式：聊了点三三\n调用出错：HTTP 401")
        self.assertEqual(n, 1, "只该落表合法的那条")
        evs = [e for e in agent.event_log.all() if e["kind"] == "chat"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["detail"].get("topic"), "定式")
        self.assertNotIn("调用出错", evs[0]["text"])

    def test_digest_llm_has_its_own_timeout(self):
        """Minor #4：补做路径自带单次超时，不等满 LLM 的 120s。"""
        from app import agent as agent_mod

        async def run():
            agent = self._seeded()
            agent.llm = SlowLLM()
            old = agent_mod.CHAT_DIGEST_TIMEOUT_SEC
            agent_mod.CHAT_DIGEST_TIMEOUT_SEC = 0.05
            try:
                t0 = time.monotonic()
                n = await agent._digest_chat_llm()
                return n, time.monotonic() - t0
            finally:
                agent_mod.CHAT_DIGEST_TIMEOUT_SEC = old

        n, dt = asyncio.run(run())
        self.assertEqual(n, 0)
        self.assertLess(dt, 0.9, "超时必须自己兜住（慢 LLM 睡 1 秒，补做不该等它）")


# ================= 情绪波动：小概率走偏优解（v3.8.0，默认关） =================


class DriftEngine(PvStubEngine):
    """（见 ``helpers.PvStubEngine``：带 ``pv`` 的假引擎，三条预测线都凑得出来。）"""


class BoomRng:
    """一旦被掷就炸：证明某条路径**一次 rng 都没碰**。"""

    def random(self):
        raise AssertionError("这条路径不该掷 rng")


def _drift_lines(drop=0.01):
    """三条线都可用的一组预测线（A=Q16 / B=D4 / C=Q4，都落在空棋盘上）。"""
    return {
        "a": {"moves": ["Q16", "D4", "Q4"], "winrate": 0.5, "score": 0.0},
        "b": {"moves": ["D4", "C3", "R14"], "winrate": 0.5, "score": 0.0, "diff_from_a": 3},
        "c": {"moves": ["Q4", "R4", "C16"], "winrate": 0.5, "score": 0.0,
              "diff_from_a": 3, "diff_from_b": 3},
        "drop": drop,
    }


# 情绪最躁的一档：``p_drift`` 吃满 ``drift_max``（0.08）——阶梯两段因此固定在
# [0, 0.0533) → B、[0.0533, 0.08) → C、其余 → A。
_AGITATED = {"v": 0.0, "c": 1.0, "a": 1.0}
_NEUTRAL = {"v": 0.58, "c": 0.55, "a": 0.30}


class EmotionDriftTest(unittest.TestCase):
    """``_maybe_drift`` 的阶梯 / 截断 / 可复现 / 回落，以及「默认关＝现状」的端到端证据。"""

    def test_drift_off_always_a(self):
        """``emotion_drift=false``（默认）⇒ 恒走 A：**不掷 rng**、不记事件、不解析线。

        端到端同时钉死「关掉＝现状」：同一套装配下，关闭时落点＝A 首手、事件表为空、
        引擎与 LLM 调用次数与「开着她也不多花一次调用」逐项一致。
        """
        agent, _eng, _llm, _bus = _mk()
        self.assertFalse(agent.emotion_drift, "情绪波动必须默认关")
        lines = _drift_lines()
        for rng in (BoomRng(), ZeroRng(), FixedRng(0.0)):
            self.assertEqual(agent._maybe_drift(lines, _AGITATED, rng), "a")
        self.assertEqual(agent.event_log.all(), [])

        async def run(drift, rng):
            a = AzureAgent(DriftEngine(), StubLLM(), events.RecordingBus(),
                           joseki_rate=1.0, joseki_rng=ZeroRng(),
                           emotion_drift=drift, drift_rng=rng)
            await a.player_move(3, 3)                    # 玩家D16，不挡三条线
            return a, a.bus

        off, bus_off = asyncio.run(run(False, BoomRng()))
        self.assertEqual(off.st.moves[-1][1], "Q16", "关闭时她仍走 A 的首手")
        self.assertEqual(off.event_log.all(), [], "关闭时不记任何事件")
        self.assertEqual(bus_off.messages(events.AI_MOVE)[0]["move"]["gtp"], "Q16")
        self.assertEqual(len(off.katago.calls), 3,
                         "第一回合仍是「落子前 + 落子后 + 她落子后复算」三次分析，一次不多")
        self.assertEqual(len(off.llm.calls), 4,
                         "第一回合仍是四条 LLM 调用（点评 / 用意 / 感想 + 一次情绪小调用），一条不多")

        on, bus_on = asyncio.run(run(True, FixedRng(0.0)))   # 0.0 落在 B 段
        self.assertEqual(len(on.katago.calls), len(off.katago.calls), "偏航零额外引擎调用")
        self.assertEqual(len(on.llm.calls), len(off.llm.calls), "偏航零额外 LLM 调用")
        self.assertEqual(on.st.moves[-1][1], "D4", "开启且掷中 B 时改走 B 的首手")
        self.assertEqual(bus_on.messages(events.AI_MOVE)[0]["move"]["gtp"], "D4", "音画同步照旧带坐标")

    def test_drift_probability_clamped(self):
        """``p_drift = clamp(drift_base + emotion_bias(mood), 0, drift_max)``：两头都要夹住；
        ``drift_max`` 本身还被「小概率」上限 ``DRIFT_MAX_LIMIT``（0.2）收口（修复轮 1 / Minor #3）。"""
        agent, _eng, _llm, _bus = _mk()
        agent.drift_base, agent.drift_max = 0.5, 0.08
        self.assertAlmostEqual(agent._drift_probability(_AGITATED), 0.08, msg="base 超上限 ⇒ 夹到 drift_max")
        agent.drift_base = -1.0
        self.assertEqual(agent._drift_probability(_AGITATED), 0.0, "和为负 ⇒ 夹到 0（恒走 A）")
        agent.drift_base = 0.03
        self.assertAlmostEqual(agent._drift_probability(_NEUTRAL), 0.03, msg="平静 ⇒ 就是 drift_base")
        self.assertAlmostEqual(agent._drift_probability(_AGITATED), 0.08, msg="最躁 ⇒ 吃满 drift_max")
        for dirty in (None, "oops", [], {"v": 0.1}, {"v": "x", "c": 0.5, "a": 0.5}):
            self.assertAlmostEqual(agent._drift_probability(dirty), 0.03, msg="缺失 / 脏值 ⇒ 偏移 0")

        # 构造器同样夹取（修复轮 1 / Minor #3）：`drift_max` 收口到 DRIFT_MAX_LIMIT（**不是 1.0**），
        # `drift_base` 再夹到 [0, drift_max]；脏值退回默认（绝不因一个旋钮把 agent 构造搞崩）。
        from app.config import DRIFT_MAX_LIMIT
        self.assertAlmostEqual(DRIFT_MAX_LIMIT, 0.2)
        a2 = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                        drift_base=5, drift_max=-3, pv_max_drop="x")
        self.assertAlmostEqual(a2.drift_max, 0.0)
        self.assertAlmostEqual(a2.drift_base, 0.0)
        self.assertAlmostEqual(a2.pv_max_drop, 0.05)
        self.assertEqual(a2._maybe_drift(_drift_lines(), _AGITATED, FixedRng(0.0)), "a",
                         "drift_max=0 ⇒ 概率为 0，恒走 A")
        a3 = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                        drift_base=0.5, drift_max=1.0)
        self.assertAlmostEqual(a3.drift_max, DRIFT_MAX_LIMIT, "drift_max 不许开到 1.0（否则她恒偏航）")
        self.assertAlmostEqual(a3.drift_base, DRIFT_MAX_LIMIT, "drift_base 夹到 [0, drift_max]")
        self.assertLess(a3._drift_probability(_AGITATED), 1.0, "p 恒 < 1：A 永远有机会被选中")

    def test_ui_levels_never_flatline_the_mood_bias(self):
        """修复轮 1 / Important #2：UI 的**每一档**都必须明显低于 `drift_max`。

        `emotion_bias` 只往上加 ⇒ 档位一旦顶到 `drift_max`，`p = clamp(base + bias, 0, max)` 就被夹平、
        情绪完全不参与（老「高 8%」那档正是如此，而 tooltip 却承诺会浮动）。这里逐档钉死
        「平静 ⇒ 就是这一档的基础概率；情绪一上来 p 必须**变大**」。
        """
        from app.agent import DRIFT_MAX_DEFAULT
        levels = [0.03, 0.05, 0.06]          # 与前端 `Main.DRIFT_BASES` 一一对应（前端门卫另有一条 < 0.08）
        self.assertEqual(levels[-1], 0.06, "最高档是 6%（8% 会被默认上限夹平）")
        for base in levels:
            agent, _eng, _llm, _bus = _mk()
            agent.emotion_drift = True
            agent.drift_base = base
            calm = agent._drift_probability(_NEUTRAL)
            stirred = agent._drift_probability(_AGITATED)
            self.assertAlmostEqual(calm, base, msg="平静 ⇒ 就是这一档的基础概率")
            self.assertGreater(stirred, calm, msg="每一档下情绪都必须仍能改变 p")
            self.assertLessEqual(stirred, DRIFT_MAX_DEFAULT, msg="浮动上限仍是 drift_max")
            self.assertLess(base, DRIFT_MAX_DEFAULT, msg="档位必须低于 drift_max，留出情绪余量")


    def test_drift_rng_reproducible(self):
        """注入 rng ⇒ 决策可复现：同种子同串、换种子不同串（说明确实在掷，而不是恒 A）。"""
        lines, mood = _drift_lines(), _AGITATED

        def seq(seed):
            agent, _eng, _llm, _bus = _mk()
            agent.emotion_drift = True
            agent.drift_rng = random.Random(seed)
            return [agent._maybe_drift(lines, mood, agent.drift_rng) for _ in range(200)]

        self.assertEqual(seq(2026), seq(2026), "同一颗种子必须给出同一串结果")
        self.assertNotEqual(seq(2026), seq(2027), "换种子应当给出不同串（确实在掷骰）")

    def test_ladder_prefers_a_then_b_then_c(self):
        """阶梯：``P(A) > P(B) > P(C)``（越偏越少），三段相加为 1；**天花板档**也成立。"""
        agent, _eng, _llm, _bus = _mk()
        agent.emotion_drift = True
        agent.drift_rng = random.Random(20261017)
        lines, mood = _drift_lines(), _AGITATED
        counts = {"a": 0, "b": 0, "c": 0}
        for _ in range(20000):
            counts[agent._maybe_drift(lines, mood, agent.drift_rng)] += 1
        self.assertEqual(sum(counts.values()), 20000)
        self.assertGreater(counts["a"], counts["b"], "A 是常态")
        self.assertGreater(counts["b"], counts["c"], "越偏越少：B 多于 C")
        self.assertGreater(counts["c"], 0, "C 真的会被走到（不是摆设）")
        # 与理论值同量级（p = 0.08 ⇒ A ≈ 92%、B ≈ 5.33%、C ≈ 2.67%）
        self.assertAlmostEqual(counts["b"] / 20000.0, 0.08 * 2.0 / 3.0, delta=0.01)
        self.assertAlmostEqual(counts["c"] / 20000.0, 0.08 / 3.0, delta=0.01)

        # 修复轮 1 / Minor #3：把 p 顶到**允许的最大值**（drift_max = DRIFT_MAX_LIMIT = 0.2）也仍是
        # 「A 常态」——这正是把上限从 1.0 收到 0.2 要守的不变量（p ≥ 0.6 时 P(A) ≤ P(B)、
        # p = 1.0 时她恒偏航，那是 spec §5-C 明令要防的「变成乱下」）。
        from app.config import DRIFT_MAX_LIMIT
        ceil_agent, _e, _l, _b = _mk()
        ceil_agent.emotion_drift = True
        ceil_agent.drift_max = DRIFT_MAX_LIMIT
        ceil_agent.drift_base = DRIFT_MAX_LIMIT
        ceil_agent.drift_rng = random.Random(20261017)
        ceil = {"a": 0, "b": 0, "c": 0}
        for _ in range(20000):
            ceil[ceil_agent._maybe_drift(lines, mood, ceil_agent.drift_rng)] += 1
        self.assertGreater(ceil["a"], ceil["b"], "天花板档：P(A) 仍最大")
        self.assertGreater(ceil["b"], ceil["c"], "天花板档：P(B) 仍大于 P(C)")
        self.assertGreater(ceil["a"] / 20000.0, 0.75, "P(A) 至少 0.8 量级（1-p = 0.8）")


    def test_illegal_first_move_falls_back(self):
        """首手不是当前棋盘的合法点 ⇒ ``C → B → A`` 逐级回落，绝不下出非法手。"""
        agent, _eng, _llm, _bus = _mk()
        agent.emotion_drift = True
        lines, mood = _drift_lines(), _AGITATED
        want_c, want_b = FixedRng(0.06), FixedRng(0.01)     # 0.06 在 C 段，0.01 在 B 段

        agent.st.board.play(3, 15, "black")                 # D4（B 的首手）被占
        self.assertEqual(agent._maybe_drift(lines, mood, want_c), "c", "C 合法就照 C 走")
        self.assertEqual(agent._maybe_drift(lines, mood, want_b), "a", "想要 B 而 B 非法 ⇒ 退回 A")

        agent2, _e2, _l2, _b2 = _mk()
        agent2.emotion_drift = True
        agent2.st.board.play(15, 15, "black")               # Q4（C 的首手）被占
        self.assertEqual(agent2._maybe_drift(lines, mood, want_c), "b", "C 非法 ⇒ 逐级退到 B")
        self.assertEqual(agent2._maybe_drift(lines, mood, want_b), "b")

        agent3, _e3, _l3, _b3 = _mk()
        agent3.emotion_drift = True
        agent3.st.board.play(3, 15, "black")                # B 与 C 都不合法
        agent3.st.board.play(15, 15, "black")
        self.assertEqual(agent3._maybe_drift(lines, mood, want_c), "a")
        self.assertEqual(agent3._maybe_drift(lines, mood, want_b), "a")

        agent4, _e4, _l4, _b4 = _mk()
        agent4.emotion_drift = True
        agent4.st.board.play(15, 3, "black")                # Q16（A 的首手）被占 ⇒ 无从偏航
        self.assertEqual(agent4._maybe_drift(lines, mood, want_b), "a")
        for bad in (None, "oops", [], {"b": lines["b"]}, {"a": {"moves": []}}):
            self.assertEqual(agent4._maybe_drift(bad, mood, want_b), "a", "取不到 A ⇒ 恒走 A")

    def test_weak_line_never_taken(self):
        """胜率差超 ``pv_max_drop`` ⇒ 这一批线整条不用；缺线 / 虚着同样不用（宁可走 A）。"""
        agent, _eng, _llm, _bus = _mk()
        agent.emotion_drift = True
        self.assertAlmostEqual(agent.pv_max_drop, 0.05)
        self.assertEqual(agent._maybe_drift(_drift_lines(drop=0.2), _AGITATED, FixedRng(0.0)), "a",
                         "drop 超上限 ⇒ 不偏航")
        self.assertEqual(agent._maybe_drift(_drift_lines(drop=0.05), _AGITATED, FixedRng(0.0)), "b",
                         "恰好等于上限 ⇒ 照旧可用（判据是「超」）")
        agent.pv_max_drop = 0.0
        self.assertEqual(agent._maybe_drift(_drift_lines(drop=0.0), _AGITATED, FixedRng(0.0)), "b")
        missing = _drift_lines()
        missing["b"] = None
        self.assertEqual(agent._maybe_drift(missing, _AGITATED, FixedRng(0.0)), "a", "B 缺失 ⇒ 走 A")
        passed = _drift_lines()
        passed["b"] = {"moves": ["pass"], "winrate": 0.5, "score": 0.0}
        self.assertEqual(agent._maybe_drift(passed, _AGITATED, FixedRng(0.0)), "a", "虚着不算可用偏航点")

    def test_drift_records_an_ai_bad_event_end_to_end(self):
        """走 B / C 时记一条 ``ai_bad`` 事件（可读文本 + 结构化 detail）；关闭时一条都不写。"""

        async def run(drift, rng):
            a = AzureAgent(DriftEngine(), StubLLM(), events.RecordingBus(),
                           joseki_rate=1.0, joseki_rng=ZeroRng(),
                           emotion_drift=drift, drift_rng=rng)
            await a.player_move(3, 3)
            return a

        off = asyncio.run(run(False, FixedRng(0.0)))
        self.assertEqual(off.event_log.all(), [])

        on = asyncio.run(run(True, FixedRng(0.0)))
        events_list = on.event_log.all()
        self.assertEqual(len(events_list), 1)
        ev = events_list[0]
        self.assertEqual(ev["kind"], "ai_bad")
        self.assertEqual(ev["move_no"], 2, "她这一手落子后的 1 基手数")
        self.assertIn("自主偏航", ev["text"], "事件里要有可读文本")
        self.assertIn("D4", ev["text"])
        self.assertEqual(ev["detail"]["line"], "b")
        self.assertEqual(ev["detail"]["gtp"], "D4")
        self.assertEqual(ev["detail"]["best"], "Q16")
        # 事件原文**永不进台词**：台词里不许出现这句事件文本
        self.assertNotIn(ev["text"], " ".join(m["text"] for m in on.bus.messages()))
        self.assertFalse(hasattr(on.st, "event_log"), "事件表不进 GameState")

    def test_drift_event_only_when_the_move_is_actually_taken(self):
        """修复轮 1 / Important #1：**先定案、后记账**——偏航点没被真的走成，就不留事件。

        ``_drift_pick`` 只返回落点（把待记账信息放进 ``_drift_pending``）；记账由 ``_record_drift``
        在落点**定案并提交**之后做，且**只在最终落点就是那个偏航点时**写。工具轮重定案 / 回落成 A
        两种情形都不留事件（事件是 P5 事件区块喂给模型的素材，与事实不符会被原样注入提示词）。
        """
        agent, _eng, _llm, _bus = _mk()
        agent.emotion_drift = True
        agent.drift_rng = FixedRng(0.0)                 # 0.0 落在 B 段
        infos = PvStubEngine().move_infos()
        pick = agent._pick_ai_move(infos)
        self.assertEqual(pick[0], "Q16", "A 的首手")

        drifted = agent._drift_pick(infos, pick)
        self.assertEqual(drifted[0], "D4", "掷中 B ⇒ 落点改成 B 的首手")
        self.assertEqual(agent.event_log.all(), [], "定案阶段**一条都不许**写事件表")
        pending = agent._take_drift_pending()
        self.assertEqual(pending["gtp"], "D4")
        self.assertEqual(pending["best"], "Q16")
        self.assertIsNone(agent._take_drift_pending(), "取走即清空（绝不跨回合残留）")

        # ① 落点被重定案（工具轮里模型自报别的点）⇒ 不记账
        self.assertFalse(agent._record_drift(pending, "Q4"))
        self.assertEqual(agent.event_log.all(), [], "最终落点不是偏航点 ⇒ 一条都不写")
        # ② 回落成 A ⇒ 不记账
        self.assertFalse(agent._record_drift(pending, "Q16"))
        self.assertEqual(agent.event_log.all(), [])
        # ③ 取不到待记账信息（没偏航 / 已被取走）⇒ 不记账
        self.assertFalse(agent._record_drift(None, "D4"))
        self.assertEqual(agent.event_log.all(), [])

        # ④ 真的走了偏航点 ⇒ 记一条；手数取**提交之后**的 len(st.moves)
        agent.st.add_move("W", "D4")
        self.assertTrue(agent._record_drift(pending, "D4"))
        ev = agent.event_log.all()[0]
        self.assertEqual(ev["kind"], "ai_bad")
        self.assertEqual(ev["move_no"], 1)
        self.assertIn("自主偏航", ev["text"])
        self.assertEqual(ev["detail"]["line"], "b")


# —— 以下为阶段四 · Q1（v3.9.0）追加：画像块 / 时间块**真的进了发给模型的提示词** ——
#    （端到端：开关打开 + 有画像 + 真跑一回合；以及「两个开关都关 ⇒ 与改动前逐字符一致」）


class _WiringStore:
    """画像库替身（装配点用到的三个读接口 + 访问记录）。"""

    def __init__(self, profile=None, last_ts=None, ts_list=None):
        self.profile = dict(profile or {})
        self.last_ts = last_ts
        self.ts_list = list(ts_list or [])
        self.reads = []

    def get_profile(self):
        self.reads.append("get_profile")
        return dict(self.profile)

    def last_game_ts(self):
        self.reads.append("last_game_ts")
        return self.last_ts

    def recent_game_ts(self, n=0):
        self.reads.append("recent_game_ts")
        return list(self.ts_list)


class ProfileTimePromptWiringTest(unittest.TestCase):
    """v3.9.0（Q1）：8 个装配点**全部**接上画像 / 时间区块，且关掉时逐字符不变。

    「装配点」＝``agent_graph`` 里所有 ``with_context`` / ``context_block`` 调用：``player_move``
    覆盖 ``after_player`` / ``own_turn`` / ``just_moved``，``hint`` / ``analyze_position`` /
    ``chat`` / ``idle_remark`` / ``summarize`` 各覆盖一格 —— 合起来正好 8 格。
    """

    PROFILE = {"open.star": {"value": 0.9, "confidence": 0.5, "n_games": 10, "updated_ts": 1.0}}

    @staticmethod
    def _ctx_calls(llm):
        """所有**带上下文块**的 LLM 调用（正文里含【当前棋面】的那几条），按调用顺序。"""
        out = []
        for msgs, _mt, _nt in llm.calls:
            joined = "\n".join(str(m.get("content", "")) for m in msgs)
            if "【当前棋面" in joined:
                out.append(joined)
        return out

    def _store(self):
        base = time.time() - 3 * 86400.0
        return _WiringStore(profile=self.PROFILE, last_ts=base, ts_list=[base])

    def test_blocks_reach_every_assembly_point(self):
        async def run():
            agent, _eng, llm, _bus = _mk()
            agent.profile_store = self._store()
            agent.profile_enabled = True
            agent.time_aware_enabled = True
            await agent.player_move(3, 3)                # after_player / own_turn / just_moved
            await agent.hint()                           # observe
            await agent.analyze_position()               # analyze
            await agent.chat("今天天气不错")               # chat
            await agent.idle_remark()                    # idle
            await agent.summarize("")                    # summary
            return agent, llm

        agent, llm = asyncio.run(run())
        ctx = self._ctx_calls(llm)
        self.assertEqual(len(ctx), 8, "六个公开入口覆盖 8 个装配点（一阶段一次上下文装配）")
        for i, joined in enumerate(ctx):
            self.assertIn("【时间事实", joined, "第 %d 条上下文缺时间块" % i)
            self.assertIn("【她眼中的你", joined, "第 %d 条上下文缺画像块" % i)
            self.assertIn("星位", joined, "画像块里要真的有这一维（开局爱走「星位」）")
            self.assertIn("距上次对局", joined, "时间块里要真的有间隔事实")
        # 两块都插在铁律段**之前**（铁律段本身一个字没动）
        for joined in ctx:
            self.assertLess(joined.index("【她眼中的你"), joined.index("（铁律 · 先核对再开口："))

    def test_switches_off_keep_the_prompt_byte_identical(self):
        """两个开关都关 ⇒ 提示词与「没有这两个功能」**逐字符一致**，且一次盘都不碰。

        六个公开入口**全覆盖**（修复轮 1 / Minor #9）：`player_move` 覆盖三格，`hint` / `analyze` /
        `chat` / `idle` / `summarize` 各一格——8 个装配点一个不漏。
        """

        async def run(with_store):
            agent, _eng, llm, _bus = _mk()
            store = None
            if with_store:
                store = self._store()
                agent.profile_store = store
            # 两个开关保持默认（都关）
            await agent.player_move(3, 3)
            await agent.hint()
            await agent.analyze_position()
            await agent.chat("聊两句")
            await agent.idle_remark()
            await agent.summarize("")
            return llm, store

        llm_on, store = asyncio.run(run(True))
        llm_off, _ = asyncio.run(run(False))
        self.assertEqual(store.reads, [], "两个开关都关 ⇒ 一次磁盘访问都不做（零开销）")
        a = self._ctx_calls(llm_on)
        b = self._ctx_calls(llm_off)
        self.assertEqual(len(a), len(b))
        self.assertEqual(len(a), 8, "六个入口覆盖 8 个装配点（少一个就说明那条没被对照到）")
        for i, (x, y) in enumerate(zip(a, b)):
            self.assertEqual(x, y, "第 %d 条上下文与没有画像 / 时间功能时逐字符一致" % i)
            self.assertNotIn("【时间事实", x)
            self.assertNotIn("【她眼中的你", x)


# —— 以下为阶段四 · Q5（v3.9.0）追加：主动约战 / 提醒（连接时一次、零新增调用） ——


class GreetLineTest(unittest.TestCase):
    """``_greet_line``：模板句纯函数（三档 + 不打扰的四种情形），**零 LLM / 零磁盘**。"""

    def test_tiers_by_gap(self):
        self.assertIn("回来啦", AzureAgent._greet_line(1.0))
        self.assertIn("回来啦", AzureAgent._greet_line(2.9))
        self.assertIn("好几天", AzureAgent._greet_line(3.0))
        self.assertIn("好几天", AzureAgent._greet_line(6.9))
        self.assertIn("好久不见", AzureAgent._greet_line(7.0))
        self.assertIn("好久不见", AzureAgent._greet_line(30))

    def test_no_line_when_not_long_enough_or_dirty(self):
        for bad in (None, "", "oops", [], {}, 0.0, 0.99, -3.0, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.assertEqual(AzureAgent._greet_line(bad), "",
                                 "不足一天 / 脏值 / NaN / inf ⇒ 不打扰")


class GreetWithContextTest(unittest.TestCase):
    """``greet_with_context``：**只在连接 / 首次交互触发一次**、跨重连不重复、零新增 LLM 调用。"""

    @staticmethod
    def _agent(last_ts):
        agent, _eng, llm, bus = _mk()
        agent.profile_store = _WiringStore(last_ts=last_ts)
        return agent, llm, bus

    def test_greets_once_and_never_calls_the_model(self):
        agent, llm, bus = self._agent(time.time() - 3 * 86400.0)

        async def run():
            first = await agent.greet_with_context(now=time.time())
            second = await agent.greet_with_context(now=time.time())     # 重连 / 二次交互
            return first, second

        first, second = asyncio.run(run())
        self.assertIn("好几天", first, "三天没来 ⇒ 第二档模板")
        self.assertEqual(second, "", "只触发一次：跨重连不再重复")
        msgs = bus.messages(events.AI_NOTICE)               # v3.9.0：提醒按 kind 过滤，把「它是 ai_notice」钉住
        self.assertEqual(len(msgs), 1, "只推一条 message")
        self.assertEqual(msgs[0]["text"], first)
        self.assertEqual(llm.calls, [], "模板句：一条 LLM 调用都不新增")
        self.assertFalse(agent.busy, "不碰 busy")

    def test_the_greeting_declares_it_has_no_speech(self):
        """修复轮 1 / C1：提醒**不发 ``speech``**，所以必须走 ``ai_notice``（推送方声明「无语音」）。

        否则前端会为它登记配对记录、占掉配对位，把连接后第一回合的三条真语音整体错位
        （点评等到落子宣告的音频才显示、揭示被推后、最后一条没声音）。
        """
        agent, _llm, bus = self._agent(time.time() - 3 * 86400.0)
        asyncio.run(agent.greet_with_context())
        got = bus.messages(events.AI_NOTICE)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], events.AI_NOTICE, "必须声明「这条没有语音」")
        self.assertEqual(bus.of_type("speech"), [], "它一条 speech 都不发（与声明一致）")

    def test_a_missed_greeting_does_not_use_up_the_chance(self):
        """修复轮 1 / Minor #1：**只有真的发了台词**才用掉这次机会。

        「这次没说成」（没有历史对局 / 距上次不足一天）⇒ 下次连接还能再说。
        """
        agent, _llm, bus = self._agent(None)                 # ① 没有历史对局
        self.assertEqual(asyncio.run(agent.greet_with_context()), "")
        self.assertEqual(bus.messages(events.AI_NOTICE), [])
        agent.profile_store = _WiringStore(last_ts=time.time() - 9 * 86400.0)
        self.assertIn("好久不见", asyncio.run(agent.greet_with_context()),
                      "上一次没说成，机会还在")

        # ② 距上次不足一天（刚下过）同样不吃掉机会
        agent2, _l2, bus2 = self._agent(time.time() - 3600.0)
        self.assertEqual(asyncio.run(agent2.greet_with_context()), "")
        agent2.profile_store = _WiringStore(last_ts=time.time() - 5 * 86400.0)
        self.assertIn("好几天", asyncio.run(agent2.greet_with_context()))
        self.assertEqual(len(bus2.messages(events.AI_NOTICE)), 1)

        # ③ 棋盘上已经有子（不是「刚连上」）同样不吃掉机会
        agent3, _l3, bus3 = self._agent(time.time() - 5 * 86400.0)
        agent3.st.add_move("B", "Q16")
        self.assertEqual(asyncio.run(agent3.greet_with_context()), "")
        agent3.reset_game()                                  # 它自己会发一条「重新来吧」提示
        bus3.clear()
        self.assertIn("好几天", asyncio.run(agent3.greet_with_context()))
        self.assertEqual(len(bus3.messages(events.AI_NOTICE)), 1)

    def test_no_greeting_without_history_or_when_too_soon(self):
        # ① 没有历史对局（库是空的 / 未装配）
        agent, llm, bus = self._agent(None)
        self.assertEqual(asyncio.run(agent.greet_with_context()), "")
        self.assertEqual(bus.messages(events.AI_NOTICE), [])
        self.assertEqual(llm.calls, [])
        # ② 距上次不足一天：刚下过，别打扰
        agent2, _l2, bus2 = self._agent(time.time() - 3600.0)
        self.assertEqual(asyncio.run(agent2.greet_with_context()), "")
        self.assertEqual(bus2.messages(events.AI_NOTICE), [])

    def test_no_greeting_when_a_game_is_already_on_the_board(self):
        """不是「刚连上」（棋盘上已经有子）⇒ 不打扰，也不吃掉这次机会之外的东西。"""
        agent, _llm, bus = self._agent(time.time() - 5 * 86400.0)
        agent.st.add_move("B", "Q16")
        self.assertEqual(asyncio.run(agent.greet_with_context()), "")
        self.assertEqual(bus.messages(events.AI_NOTICE), [])

    def test_store_failure_is_only_a_missing_greeting(self):
        """库坏了 ⇒ 当作「没有上次」：不抛、不发、不卡。"""

        class Boom(_WiringStore):
            def last_game_ts(self):
                raise RuntimeError("库坏了")

        agent, _eng, llm, bus = _mk()
        agent.profile_store = Boom(last_ts=time.time() - 9 * 86400.0)
        self.assertEqual(asyncio.run(agent.greet_with_context()), "")
        self.assertEqual(bus.messages(events.AI_NOTICE), [])
        self.assertFalse(agent.busy)
        self.assertEqual(llm.calls, [])

    def test_reset_game_does_not_reset_the_greeted_mark(self):
        """``reset_game`` **有意不重置**「已打过招呼」：跨重连 / 重开一局都不再补打。"""
        agent, _llm, _bus = self._agent(time.time() - 9 * 86400.0)
        self.assertIn("好久不见", asyncio.run(agent.greet_with_context()))
        agent.reset_game()
        self.assertEqual(asyncio.run(agent.greet_with_context()), "")

    def test_no_extra_llm_or_engine_calls_in_a_move_turn(self):
        """假 LLM / 假引擎的调用数与「没有这个提醒」时逐项一致（提醒零新增调用）。"""

        async def run(with_greet):
            agent, eng, llm, _bus = _mk()
            agent.profile_store = _WiringStore(last_ts=time.time() - 3 * 86400.0)
            if with_greet:
                await agent.greet_with_context()
            await agent.player_move(3, 3)
            return len(llm.calls), len(eng.calls)

        self.assertEqual(asyncio.run(run(True)), asyncio.run(run(False)))


# —— 以下为阶段四 · Q7（v3.9.0）追加：局末进步报告 + 写对局记录与画像 ——


class _LossEngine(StubEngine):
    """玩家落子**之后**胜率明显下滑的假引擎（黑方视角 0.5 → 0.2 ⇒ ``drop`` 0.3 > 0.10）。

    只改「玩家落子后局面」那一次（``len(moves) == 1``）的 ``rootInfo``：KataGo 的 ``rootInfo`` 是
    **行棋方（白）**视角，1 手时 ``to_black_perspective`` 会翻成 ``1 - 0.8 = 0.2``。
    """

    async def analyze(self, moves, max_visits=None):
        out = await super().analyze(moves, max_visits)
        if len(moves) == 1:
            out["rootInfo"] = {"winrate": 0.8, "scoreLead": 0.0}
        return out


class GameRecordWriteTest(unittest.TestCase):
    """v3.9.0（Q7）：``summarize()`` 里落一条对局记录 + 融合后的画像；关着 / 坏了都不影响对局。"""

    REC_KEYS = {"id", "ts", "move_count", "final_black_wr", "final_score", "result",
                "moves", "per_move_ms", "joseki_families", "chat_topics", "summary"}

    @staticmethod
    def _agent(store=None, enabled=True, engine=None):
        agent, _eng, llm, bus = _mk()
        agent.katago = engine if engine is not None else _LossEngine()
        agent.profile_store = store
        agent.profile_enabled = enabled
        return agent, llm, bus

    @staticmethod
    def _summary_prompt(llm):
        """最后那条「对局总结」提示（按正文特征挑出来，避免依赖调用序号）。"""
        for msgs, _mt, _nt in reversed(llm.calls):
            joined = "\n".join(str(m.get("content", "")) for m in msgs)
            if "对局结束，共" in joined:
                return joined
        return ""

    def test_summary_writes_the_record_and_merges_the_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            agent, llm, _bus = self._agent(store)

            async def run():
                await agent.player_move(3, 3)          # 玩家D16（真亏 30%）
                await agent.summarize("随便聊聊")

            asyncio.run(run())
            games = store.games()
            self.assertEqual(len(games), 1, "手动结束 ⇒ 落一条对局记录（只增不减）")
            rec = games[0]
            self.assertEqual(set(rec), self.REC_KEYS, "键名照 spec §6.1（外加存储层的 id）")
            self.assertEqual(rec["move_count"], 2)
            self.assertGreater(rec["ts"], 0.0)
            self.assertIn("胜率", rec["result"])
            self.assertEqual(rec["summary"], llm.text, "真总结写进记录")
            self.assertEqual(rec["per_move_ms"], [], "只有一手玩家落子 ⇒ 没有间隔（首手不计）")
            self.assertEqual(rec["joseki_families"], [], "本局没识别出定式族")
            self.assertEqual(rec["chat_topics"], [], "本局没聊过天")
            # ``moves`` 的每条都带 color / gtp；**玩家（黑）的每一条带 drop**（Q2 的唯一信号源）
            self.assertEqual([m["color"] for m in rec["moves"]], ["B", "W"])
            for m in rec["moves"]:
                self.assertIn("gtp", m)
            self.assertAlmostEqual(rec["moves"][0]["drop"], 0.3, places=6, msg="真亏手填进 drop")
            self.assertNotIn("drop", rec["moves"][1], "她那一侧不填 drop（不进玩家画像）")
            # 画像：有 drop 信号 ⇒ 失误那一组也产出；开局偏好照旧
            prof = store.get_profile()
            self.assertAlmostEqual(prof["blunder.rate"]["value"], 1.0, places=6)
            self.assertEqual(prof["blunder.rate"]["n_games"], 1)
            self.assertIn("blunder.top_type", prof)
            self.assertAlmostEqual(prof["open.star"]["value"], 1.0, places=6, msg="D16 在角上＝星位")

    def test_no_drop_signal_still_marks_the_player_moves_as_measured(self):
        """没有真亏手时，玩家每一手仍然带 ``drop: 0.0``——「量过、没亏」也是信号。

        否则 ``extract_axes`` 的 ``_has_drop_data`` 会判成「本局没有失误信号」，
        ``blunder.rate`` 永远不产出，「他很少下出明显亏损的棋」这句话也就永远学不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            agent, _llm, _bus = self._agent(store, engine=StubEngine())

            async def run():
                await agent.player_move(3, 15)          # D4：在她算的第二候选 ⇒ 不亏
                await agent.summarize("")

            asyncio.run(run())
            rec = store.games()[0]
            self.assertEqual(rec["moves"][0]["drop"], 0.0)
            prof = store.get_profile()
            self.assertAlmostEqual(prof["blunder.rate"]["value"], 0.0, places=6)

    def test_nothing_is_written_when_the_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            agent, _llm, _bus = self._agent(store, enabled=False)

            async def run():
                await agent.player_move(3, 3)
                await agent.summarize("")

            asyncio.run(run())
            self.assertEqual(store.games(), [], "开关关着 ⇒ 一条记录都不写")
            self.assertEqual(store.get_profile(), {}, "开关关着 ⇒ 画像不更新")

    def test_summarize_calls_the_model_the_same_number_of_times(self):
        """进步报告**并进现有** ``summarize()`` 提示词：LLM / 引擎调用数与改动前一致。"""

        async def run(enabled):
            with tempfile.TemporaryDirectory() as tmp:
                agent, _llm, _bus = self._agent(ProfileStore(os.path.join(tmp, "profile.db")),
                                                enabled=enabled)
                await agent.player_move(3, 3)
                await agent.summarize("")
                return len(agent.llm.calls), len(agent.katago.calls)

        on = asyncio.run(run(True))
        off = asyncio.run(run(False))
        self.assertEqual(on, off, "开关打开不新增任何 LLM / 引擎调用")

    def test_progress_block_lands_in_the_summary_prompt_and_off_is_identical(self):
        async def run(enabled, store):
            agent, llm, _bus = self._agent(store, enabled=enabled)
            await agent.player_move(3, 3)
            await agent.summarize("")
            return self._summary_prompt(llm)

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            on = asyncio.run(run(True, store))
            off = asyncio.run(run(False, store))
        control = asyncio.run(run(False, None))
        self.assertIn("【本局记录与上一版印象", on, "开关打开 ⇒ 素材真的进了发给模型的提示词")
        self.assertIn("共 2 手", on)
        self.assertEqual(off, control, "开关关着 ⇒ 与没有这个功能逐字符一致")
        self.assertNotIn("【本局记录与上一版印象", off)
        self.assertNotIn("【本局记录与上一版印象", control)

    def test_the_previous_profile_is_the_merge_basis(self):
        """第二局的进步报告素材里要出现**上一版印象**，且新画像在旧画像上继续融合。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))

            async def run():
                agent, llm, _bus = self._agent(store, engine=StubEngine())
                llm.text = "第一局的总结。"               # 两局用不同台词，避开去重窗口（见报告里的取舍）
                await agent.player_move(3, 15)          # 第一局：D4（星位那一带）
                await agent.summarize("")
                first = store.get_profile()
                agent.reset_game()                      # 同步方法：新一局（记录与画像照旧留着）
                agent.katago = _LossEngine()
                llm.text = "第二局的总结。"
                await agent.player_move(3, 3)           # 第二局：D16，真亏一手
                await agent.summarize("")
                return first, self._summary_prompt(llm), store

            first, prompt, store = asyncio.run(run())
            self.assertIn("【本局记录与上一版印象", prompt)
            self.assertIn("上一版印象", prompt)
            self.assertIn("开局爱走「星位」", prompt, "上一版画像真的被喂给了模型")
            second = store.get_profile()
            self.assertEqual(second["open.star"]["n_games"], 2, "同维继续累加样本数")
            self.assertGreaterEqual(len(store.games()), 2, "两局两条记录")

    def test_write_failure_keeps_the_turn_alive(self):
        """库坏了 ⇒ 只打日志：总结照常出话、``busy`` 归位（本项目最严重的故障形态不许发生）。"""

        class Boom:
            def get_profile(self):
                raise RuntimeError("库坏了")

            def add_game(self, rec):
                raise RuntimeError("库坏了")

            def upsert_axes(self, axes):
                raise RuntimeError("库坏了")

        agent, _llm, bus = self._agent(Boom())

        async def run():
            await agent.player_move(3, 3)
            await agent.summarize("")

        asyncio.run(run())
        self.assertFalse(agent.busy, "写入故障绝不许把棋盘锁死")
        self.assertTrue(any("📋 对局总结" in m["text"] for m in bus.messages(events.AI)))

    def test_llm_failure_still_records_the_game_without_a_summary(self):
        """云端总结失败 ⇒ 记录照写（这一局确实下完了），但 ``summary`` 留空——兜底台词不是总结。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            agent, _llm, bus = self._agent(store)
            agent.llm = ErrLLM()                        # 真实失败形态：返回「调用出错：…」文案（不抛）

            async def run():
                await agent.player_move(3, 3)
                await agent.summarize("")

            asyncio.run(run())
            self.assertFalse(agent.busy)
            games = store.games()
            self.assertEqual(len(games), 1, "总结失败也是「这一局结束了」：记录照写")
            self.assertEqual(games[0]["summary"], "", "兜底台词不写进记录")
            self.assertGreater(games[0]["move_count"], 0)


class DedupLogEncodingTest(unittest.TestCase):
    """修复轮 1 / I1：去重命中时的那行日志**不许**因为控制台编码把整回合打挂。

    复现：台词里带 emoji（总结那条是 ``📋 对局总结\\n…``），而那行 ``print`` 打的是**台词原文**。
    中文 Windows 的控制台 / 重定向文件是 cp936（``errors="strict"``）⇒ ``print`` 抛
    ``UnicodeEncodeError``：异常被 ``summarize()`` 的 ``except`` 兜住（``busy`` 归位），但这条总结
    **既不显示、也不写记录**——本批新增的「写对局记录 + 融合画像」正好在那之后，所以后果是**丢数据**。

    这里把 stdout 换成**真正的 GBK 流**（`errors="strict"`），把触发条件钉死；断言「两局都照常完成、
    两条记录都落盘」，并断言那行日志**确实打过**（否则这条用例是空的）。
    """

    EMOJI = "嗯~ 这一步我看着挺顺。😀"       # cp936 装不下：正是触发条件

    def _run_with_gbk_stdout(self, factory) -> str:
        """在 GBK（strict）的 stdout 下跑一段协程，返回捕获到的日志文本。"""
        raw = io.BytesIO()
        gbk = io.TextIOWrapper(raw, encoding="gbk", errors="strict", newline="")
        try:
            with contextlib.redirect_stdout(gbk):
                asyncio.run(factory())
            gbk.flush()
        finally:
            gbk.detach()                    # 别让 wrapper 关掉 BytesIO（我们要读它）
        return raw.getvalue().decode("gbk", "replace")

    def test_dedup_hit_keeps_the_turn_and_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(os.path.join(tmp, "profile.db"))
            agent, _eng, llm, _bus = _mk()
            agent.katago = _LossEngine()
            agent.profile_store = store
            agent.profile_enabled = True
            llm.text = self.EMOJI            # 每条台词都带 emoji（点评 / 用意 / 感想 / 总结）

            async def run():
                await agent.player_move(3, 3)     # 同文三条 ⇒ 去重命中（那行 print 在这儿）
                await agent.summarize("")         # 第一局的总结入窗
                agent.reset_game()
                await agent.player_move(3, 3)
                await agent.summarize("")         # 第二局的总结**原话一字不差** ⇒ 去重命中（触发点）

            log = self._run_with_gbk_stdout(run)
            self.assertIn("原话去重", log, "去重确实命中（否则这条用例是空的）")
            self.assertIn("?", log, "emoji 被降级成 '?'：日志安全化真的生效")
            self.assertFalse(agent.busy, "回合照常结束（不许把棋盘锁死）")
            self.assertEqual(len(store.games()), 2,
                             "两局都照常落盘——去重命中只是不打这句台词，不许打挂回合（丢记录）")
            self.assertTrue(all(g["summary"] == self.EMOJI for g in store.games()),
                            "记录里的总结仍是**原文**（只降级日志副本，不动台词）")

class BusyOwnershipTest(unittest.TestCase):
    """busy 的「所有者」闸门（v3.9.5）：旧回合的 finally 不许解开新回合的锁。

    复现路径：`_finalize_just_moved` 讲完就 `_set_busy(False)`（设计如此），随后
    `_mood_llm_refine()` 还要跑 1~20 秒（15 秒限频 ⇒ 每两三手出现一次）；玩家在这段窗口里落子
    ⇒ 新回合把 busy 置真，旧回合 finally 回来若无条件清位，就把新回合的锁解了 ⇒ 她还没应手，
   玩家又能再下一手。
    """

    def _agent(self):
        return AzureAgent(StubEngine(), StubLLM(), events.RecordingBus())

    def test_stale_turn_cannot_unlock_a_newer_turn(self):
        a = self._agent()
        old = a._set_busy(True)
        self.assertTrue(a.busy)
        a._set_busy(False)                 # 旧回合讲完就解锁（节点的既有行为）
        a._set_busy(True)                  # 玩家在窗口里落子：新回合置真
        a._clear_busy(old)                 # 旧回合的 finally 回来
        self.assertTrue(a.busy, "旧回合不许把新回合的锁提前解开")
        a._set_busy(False)
        self.assertFalse(a.busy)

    def test_owner_clear_still_unlocks_when_nothing_new_started(self):
        a = self._agent()
        owner = a._set_busy(True)
        a._clear_busy(owner)
        self.assertFalse(a.busy, "没有新回合时，拥有者自己清位照常生效")

    def test_clear_without_owner_is_unconditional(self):
        """不传 owner（旧调用点 / 看门狗）保持旧语义：无条件清位。"""
        a = self._agent()
        a._set_busy(True)
        a._clear_busy()
        self.assertFalse(a.busy)

class PersonaStyleTest(unittest.TestCase):
    """情绪棋风 / 随便聊天 / 摸头惯性（v3.13.0）。"""

    def test_casual_chat_keywords(self):
        self.assertTrue(AzureAgent.is_casual_chat("今天随便下下"))
        self.assertTrue(AzureAgent.is_casual_chat("放松点，不用认真"))
        self.assertFalse(AzureAgent.is_casual_chat("这手该怎么下"))
        self.assertFalse(AzureAgent.is_casual_chat(None))

    def test_style_only_when_happy_and_close(self):
        agent, _eng, _llm, _bus = _mk()
        agent.emotion_style = True
        happy = {"v": 0.85, "c": 0.55, "a": 0.65}
        sad = {"v": 0.35, "c": 0.55, "a": 0.65}
        close = _drift_lines(drop=0.01)
        self.assertEqual(agent._style_letter(close, sad, FixedRng(0.0)), "a",
                         "低落时更稳：哪怕掷中也不试 B")
        self.assertEqual(agent._style_letter(_drift_lines(drop=0.03), happy, FixedRng(0.0)), "a",
                         "胜率差超过 1.5%：不拿这一手试风格")
        self.assertEqual(agent._style_letter(close, happy, FixedRng(0.0)), "b",
                         "心情好 + 两手接近：可以按自己的棋感走 B")
        self.assertEqual(agent._style_letter(close, happy, FixedRng(0.99)), "a",
                         "风格选择仍是小概率，不是必偏")
        agent._style_until_move = 10
        self.assertEqual(agent._style_letter(close, happy, FixedRng(0.0)), "a",
                         "冷却期内不再连续「想试试」")

    def test_style_pick_end_to_end_writes_ai_style(self):
        """心情好 + 两手接近 ⇒ 真落 B，并记 ``ai_style``（不是 ``ai_bad``）。"""
        async def run():
            a = AzureAgent(DriftEngine(), StubLLM(), events.RecordingBus(),
                           joseki_rate=0.0, emotion_style=True, drift_rng=FixedRng(0.0))
            a.mood.v, a.mood.c, a.mood.a = 0.85, 0.55, 0.65
            await a.player_move(3, 3)                 # 玩家D16，不挡 B 的 D4
            return a

        a = asyncio.run(run())
        self.assertEqual(a.st.moves[-1][1], "D4", "掷中风格时走 B 的首手")
        kinds = [e["kind"] for e in a.event_log.all()]
        self.assertIn("ai_style", kinds)
        self.assertNotIn("ai_bad", kinds, "风格选择不是「下坏了」")

    def test_pat_scale_diminishes_and_depends_on_mood(self):
        agent, _eng, _llm, _bus = _mk()
        first = agent._pat_scale()
        second = agent._pat_scale()
        third = agent._pat_scale()
        self.assertAlmostEqual(first, 1.0)
        self.assertGreater(first, second)
        self.assertGreater(second, third)
        self.assertLess(third, 0.5, "连续摸头收益必须明显递减")

        sad, _e1, _l1, _b1 = _mk()
        sad.mood.v, sad.mood.c, sad.mood.a = 0.2, 0.5, 0.2
        self.assertGreater(sad._pat_scale(), 1.0, "难过时安慰更有效")
        angry, _e2, _l2, _b2 = _mk()
        angry.mood.v, angry.mood.c, angry.mood.a = 0.1, 0.1, 0.9
        self.assertLess(angry._pat_scale(), 0.5, "烦躁时摸头没那么容易哄好")
