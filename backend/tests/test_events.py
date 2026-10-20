"""事件契约与事件总线：字段形状与订阅发布。"""

import asyncio
import unittest

from app import events


class EventShapeTest(unittest.TestCase):
    def test_message(self):
        e = events.message(events.AI_MOVE, "我下这里", move={"x": 3, "y": 3, "gtp": "D16", "pass": False})
        self.assertEqual(e["type"], "message")
        self.assertEqual(e["kind"], "ai_move")
        self.assertEqual(e["text"], "我下这里")
        self.assertEqual(e["move"]["gtp"], "D16")

    def test_message_without_move_has_no_key(self):
        e = events.message(events.AI, "嗯")
        self.assertNotIn("move", e)

    def test_state_flat(self):
        e = events.state({"move_count": 2, "allow_pass": False})
        self.assertEqual(e["type"], "state")
        self.assertEqual(e["move_count"], 2)

    def test_info_and_busy(self):
        self.assertEqual(events.info(0.6, 1.5), {"type": "info", "winrate": 0.6, "score": 1.5})
        self.assertEqual(events.busy(True), {"type": "busy", "busy": True})

    def test_mood_and_joseki(self):
        m = events.mood({"region": 12, "name": "平静"})
        self.assertEqual(m["type"], "mood")
        self.assertEqual(m["name"], "平静")
        j = events.joseki({"kind": "formed", "corner": "左上", "points": [(3, 3)]})
        self.assertEqual(j["type"], "joseki")
        self.assertEqual(j["points"], [(3, 3)])

    def test_hello(self):
        h = events.hello({"move_count": 0}, {"region": 12}, True)
        self.assertEqual(h["type"], "hello")
        self.assertEqual(h["state"]["move_count"], 0)
        self.assertEqual(h["mood"]["region"], 12)
        self.assertTrue(h["busy"])


class JosekiEventShapeTest(unittest.TestCase):
    """T6：``joseki`` 事件扩出卡片载荷，**旧字段的取值路径一行不改**。

    新字段一律走 ``.get()`` + 安全缺省（缺字段 / 脏值退化成缺省，绝不抛）。
    """

    def test_joseki_event_keeps_legacy_fields(self):
        """旧字段逐键不动：``type`` / ``kind`` / ``corner`` / ``points`` 取值路径不变。"""
        payload = {"kind": "formed", "corner": "左上", "points": [(3, 3)]}
        e = events.joseki(payload)
        self.assertEqual(e["type"], "joseki")
        self.assertEqual(e["kind"], "formed")
        self.assertEqual(e["corner"], "左上")
        self.assertEqual(e["points"], [(3, 3)])

    def test_joseki_event_carries_card_payload(self):
        e = events.joseki({"kind": "entered", "family": "star_33", "name": "点三三·基本型",
                           "k": 2, "line": [{"x": 3, "y": 3, "c": "B"}],
                           "candidates": [{"name": "点三三·定形", "next": (4, 3)}],
                           "show_card": True})
        self.assertEqual(e["family"], "star_33")
        self.assertEqual(e["name"], "点三三·基本型")
        self.assertEqual(e["k"], 2)
        self.assertEqual(e["line"], [{"x": 3, "y": 3, "c": "B"}])
        self.assertEqual(e["candidates"], [{"name": "点三三·定形", "next": (4, 3)}])
        self.assertTrue(e["show_card"])
        self.assertIsNone(e["mistake"])                      # 没给 mistake ⇒ None

    def test_joseki_event_defaults_when_fields_missing(self):
        """只给 ``kind`` / ``corner`` 时新字段全部安全缺省。"""
        e = events.joseki({"kind": "entered", "corner": "左上"})
        self.assertEqual(e["family"], "")
        self.assertEqual(e["name"], "")
        self.assertEqual(e["k"], 0)
        self.assertEqual(e["line"], [])
        self.assertEqual(e["candidates"], [])
        self.assertIs(e["show_card"], False)
        self.assertIsNone(e["mistake"])

    def test_joseki_event_mistake_only_when_it_is_one(self):
        """``mistake`` 只在**真是走错**时非 None；没亏那一手不许被标红（U31）。"""
        e = events.joseki({"kind": "deviated", "mistake": {
            "is_mistake": True, "move_no": 5, "x": 1, "y": 4, "drop": 0.2}})
        self.assertEqual(e["mistake"], {"move_no": 5, "x": 1, "y": 4, "drop": 0.2})

        e2 = events.joseki({"kind": "deviated", "mistake": {
            "is_mistake": False, "move_no": 5, "x": 1, "y": 4, "drop": 0.0}})
        self.assertIsNone(e2["mistake"])

    def test_joseki_event_dirty_values_never_raise(self):
        """脏值 / 错类型一律退化成缺省，不得把端点打成 500。"""
        e = events.joseki({"kind": "formed", "corner": "左上", "points": [],
                           "family": None, "name": 0, "k": "x", "line": "oops",
                           "candidates": None, "show_card": 0, "mistake": "oops"})
        self.assertEqual(e["family"], "")
        self.assertEqual(e["name"], "")                      # 脏值 / 假值一律退回空串
        self.assertEqual(e["k"], 0)
        self.assertEqual(e["line"], [])
        self.assertEqual(e["candidates"], [])
        self.assertIs(e["show_card"], False)
        self.assertIsNone(e["mistake"])

    def test_joseki_event_mistake_dirty_numbers_fall_back_to_zero(self):
        e = events.joseki({"kind": "deviated", "mistake": {
            "is_mistake": True, "move_no": "x", "x": None, "y": "y", "drop": "z"}})
        self.assertEqual(e["mistake"], {"move_no": 0, "x": 0, "y": 0, "drop": 0.0})


class RecordingBusTest(unittest.TestCase):
    def test_helpers(self):
        bus = events.RecordingBus()
        bus.publish(events.message(events.AI, "a"))
        bus.publish(events.message(events.AI_MOVE, "b"))
        bus.publish(events.info(0.5, 0.0))
        self.assertEqual(len(bus.of_type("message")), 2)
        self.assertEqual(len(bus.messages(events.AI_MOVE)), 1)
        bus.clear()
        self.assertEqual(bus.events, [])


class AsyncBusTest(unittest.TestCase):
    def test_publish_reaches_subscriber(self):
        async def run():
            bus = events.EventBus()
            q = bus.subscribe()
            bus.publish(events.busy(True))
            got = await asyncio.wait_for(q.get(), timeout=1.0)
            bus.unsubscribe(q)
            return got

        self.assertEqual(asyncio.run(run()), {"type": "busy", "busy": True})

    def test_unsubscribe_stops_delivery(self):
        async def run():
            bus = events.EventBus()
            q = bus.subscribe()
            bus.unsubscribe(q)
            bus.publish(events.busy(False))
            return q.empty()

        self.assertTrue(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
