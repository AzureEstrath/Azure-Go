"""局内事件表（带时刻、只增不减、去重）：不落盘、不进快照、不随悔棋回退。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    def test_kinds_are_the_five_known_ones(self):
        from app.event_log import EVENT_KINDS, EventLog
        self.assertEqual(set(EVENT_KINDS),
                         {"ai_bad", "player_bad", "player_good", "chat", "joseki"})
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

    def test_not_in_state_snapshot_and_survives_undo_reset(self):
        """不进 ``GameState`` 快照、不随悔棋 / 重开回退（「发生过就是发生过」）。"""
        from app.event_log import EventLog
        from app.state import GameState
        st = GameState()
        log = EventLog()
        log.add("player_bad", 2, "Estarth这手亏了", {})
        st.add_move("B", "Q16")
        st.snapshots.append(st.make_snapshot())
        self.assertNotIn("events", st.payload())
        self.assertNotIn("event_log", st.payload())
        self.assertNotIn("events", st.make_snapshot())
        st.reset()                                  # 悔棋同义：只回滚对局状态
        self.assertEqual(len(log.all()), 1, "事件表不随对局状态回退")

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
