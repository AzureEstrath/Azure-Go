"""局内事件表（带时刻、只增不减、去重）：不落盘、不进快照、不随悔棋回退、新一局清空。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.agent import AzureAgent
from helpers import FixedRng, PvStubEngine, StubLLM, ZeroRng


class EventLogTest(unittest.TestCase):
    def test_add_records_time_kind_text_detail(self):
        from app.event_log import EventLog
        log = EventLog()
        ev = log.add("ai_bad", 7, "我在第7手下错了", {"gtp": "Q16", "drop": 0.12})
        self.assertEqual(ev["kind"], "ai_bad")
        self.assertEqual(ev["move_no"], 7)
        self.assertEqual(ev["text"], "我在第7手下错了")
        self.assertEqual(ev["detail"], {"gtp": "Q16", "drop": 0.12})
        self.assertIsInstance(ev["ts"], float)
        self.assertGreater(ev["ts"], 0.0)
        self.assertEqual(log.all(), [ev])

    def test_kinds_are_the_known_ones(self):
        from app.event_log import EVENT_KINDS, EventLog
        self.assertEqual(set(EVENT_KINDS),
                         {"ai_bad", "ai_style", "player_bad", "player_good", "chat", "joseki"})
        log = EventLog()
        for k in EVENT_KINDS:
            self.assertIsNotNone(log.add(k, 1, "第 %s 类事件" % k, {}))

    def test_duplicate_kind_and_text_is_not_written(self):
        """去重键 ＝ ``kind`` + 归一化 ``text``（命中则不写）。"""
        from app.event_log import EventLog
        log = EventLog()
        self.assertIsNotNone(log.add("player_bad", 3, "Estarth这手亏了", {}))
        self.assertIsNone(log.add("player_bad", 9, "Estarth这手亏了", {}), "同 kind 同 text 不写")
        self.assertIsNone(log.add("player_bad", 9, "  Estarth这手亏了\n", {}), "归一化后同一句不写")
        self.assertEqual(len(log.all()), 1)
        self.assertIsNotNone(log.add("player_good", 3, "Estarth这手亏了", {}), "不同 kind 照写")
        self.assertIsNotNone(log.add("player_bad", 3, "Estarth这手好棋", {}), "不同 text 照写")
        self.assertEqual(len(log.all()), 3)

    def test_all_covers_from_open_to_now_in_order(self):
        from app.event_log import EventLog
        log = EventLog()
        log.add("player_good", 1, "开局第一手很好", {})
        log.add("chat", 5, "聊了聊开局的想法", {})
        log.add("ai_bad", 9, "我第9手走偏了", {})
        got = log.all()
        self.assertEqual([e["move_no"] for e in got], [1, 5, 9])
        self.assertEqual([e["kind"] for e in got], ["player_good", "chat", "ai_bad"])
        self.assertEqual(len(log), 3)

    def test_reset_clears(self):
        from app.event_log import EventLog
        log = EventLog()
        log.add("ai_bad", 2, "我在第2手下错了", {})
        log.reset()
        self.assertEqual(log.all(), [])
        # reset 之后同一条可以重新写（去重集合一并清空）
        self.assertIsNotNone(log.add("ai_bad", 2, "我在第2手下错了", {}))

    def test_not_in_state_snapshot_survives_undo_but_reset_game_clears(self):
        """不进 ``GameState`` 快照、不随悔棋回退；但**新一局清空**（设计 §5 E/F）。

        v3.8.0 起事件表**已接进编排**（走偏优解写 ``ai_bad``），所以这里是**真断言**而不是
        「两不相干」的占位：跑一手真对局 → 事件表有内容、``payload()`` / 快照里没有它 →
        悔棋（``undo``）不退回 → ``reset_game()`` 清空（新一局不带上一局的账）。
        """
        async def run():
            # 情绪波动开着 + 恒掷中的 rng ⇒ 她这一手走偏，事件表因此必定有内容
            agent = AzureAgent(PvStubEngine(), StubLLM(), events.RecordingBus(),
                               joseki_rate=1.0, joseki_rng=ZeroRng(),
                               emotion_drift=True, drift_rng=FixedRng(0.0))
            await agent.player_move(3, 3)
            return agent

        agent = asyncio.run(run())
        st, log = agent.st, agent.event_log
        self.assertEqual(len(log.all()), 1, "跑一手真对局之后事件表有内容")
        self.assertEqual(log.all()[0]["kind"], "ai_bad")

        # 不进 GameState：payload() 与快照里都没有它
        self.assertNotIn("events", st.payload())
        self.assertNotIn("event_log", st.payload())
        self.assertNotIn("events", st.make_snapshot())
        self.assertNotIn("event_log", st.make_snapshot())
        self.assertFalse(hasattr(st, "event_log"), "事件表挂在 agent 上，不挂在 GameState 上")

        # 悔棋：事件表**不退回**（「发生过就是发生过」，设计 §5 F）
        agent.undo(0)
        self.assertEqual(len(log.all()), 1, "事件表不随悔棋回退")

        # 重开一局：**清空**（新一局不带上一局的账，设计 §5 E）
        agent.reset_game()
        self.assertEqual(log.all(), [], "reset_game 清空事件表")
        self.assertEqual(len(st.moves), 0)

    def test_dirty_inputs_do_not_raise(self):
        from app.event_log import EventLog
        log = EventLog()
        ev = log.add("ai_bad", "abc", None, None)
        self.assertEqual(ev["move_no"], 0)
        self.assertEqual(ev["text"], "")
        self.assertEqual(ev["detail"], {})
        self.assertEqual(len(log.all()), 1)


if __name__ == "__main__":
    unittest.main()
