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
