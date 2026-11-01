"""考试复习规划（v4.7.0）：空闲段分布 / Azure 属性 / 整组取消 / API。"""

import datetime
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import auto_plan, events, exam_plan
from app.agent import AzureAgent
from app.config import AzureConfig
from app.life import LifeStore
from app.main import create_app
from helpers import StubEngine, StubLLM, ZeroRng


class ChatScheduleLLM(StubLLM):
    """聊天回复与日程判断共用：按提示词认领返回不同文本。"""

    def __init__(self, chat_text="好，记下了。", schedule_text=""):
        super().__init__(text=chat_text)
        self.schedule_text = schedule_text

    async def chat(self, messages, max_tokens=300, no_think=False, think_mode=None):
        self.calls.append((list(messages), max_tokens, no_think))
        self.think_modes.append(think_mode)
        prompt = str(messages[-1].get("content", "")) if messages else ""
        if "日程判断器" in prompt:
            return self.schedule_text
        return self.text


class PurePlanTest(unittest.TestCase):
    def _exam(self, now, notes="考试范围：Unit 1、Unit 2、语法"):
        return {"id": 7, "title": "英语考试", "notes": notes, "kind": "task",
                "owner": "user", "deadline_ts": now + 14 * 86400,
                "start_ts": now + 14 * 86400, "duration_min": 60}

    def _busy(self, now, days, hour=18, dur=120, task_type="work"):
        base = datetime.datetime.fromtimestamp(now)
        start = (base.replace(hour=hour, minute=0, second=0) +
                 datetime.timedelta(days=days)).timestamp()
        return {"title": "忙", "kind": "task", "start_ts": start,
                "duration_min": dur, "status": "planned", "task_type": task_type}

    def test_is_exam_and_scope(self):
        now = time.time()
        self.assertTrue(exam_plan.is_exam(self._exam(now)))
        self.assertTrue(exam_plan.is_exam({"title": "结业考试", "notes": ""}))
        self.assertFalse(exam_plan.is_exam({"title": "英语课", "kind": "course",
                                            "period": "1-2"}))
        self.assertEqual(exam_plan.scope_text(self._exam(now)),
                         "Unit 1、Unit 2、语法")
        self.assertEqual(exam_plan.scope_parts("Unit 1、Unit 2、语法"),
                         ["Unit 1", "Unit 2", "语法"])
        self.assertEqual(exam_plan.plan_group(self._exam(now)), "exam:7")

    def test_build_plan_avoids_busy_and_spreads(self):
        now = time.time()
        exam = self._exam(now)
        busy = [self._busy(now, 1), self._busy(now, 4), self._busy(now, 8)]
        items = [exam] + busy
        plan = exam_plan.build_plan(exam, items, now=now, weeks=2, max_sessions=4,
                                    duration_min=50, buffer_min=0, transition_min=0)
        self.assertGreaterEqual(len(plan), 2)
        self.assertLessEqual(len(plan), 4)
        dates = {p["date"] for p in plan}
        self.assertEqual(len(dates), len(plan), "每天只放一次")
        for p in plan:
            a, b = p["start_ts"], p["start_ts"] + p["duration_min"] * 60
            for busy_item in busy:
                b0 = busy_item["start_ts"]
                b1 = b0 + busy_item["duration_min"] * 60
                self.assertFalse(a < b1 and b > b0, "不能排进已有日程")
            self.assertEqual(p["owner"], "azure")
            self.assertEqual(p["plan_group"], "exam:7")
            self.assertEqual(p["task_type"], "study")

    def test_build_plan_needs_future_exam(self):
        now = time.time()
        exam = self._exam(now)
        exam["deadline_ts"] = now + 3600
        exam["start_ts"] = now + 3600
        self.assertEqual(exam_plan.build_plan(exam, [exam], now=now), [])

    def test_locked_in_auto_plan(self):
        self.assertEqual(auto_plan.classify_item({
            "title": "复习", "owner": "azure", "plan_group": "exam:1",
            "start_ts": time.time() + 86400, "duration_min": 50,
        }), auto_plan.GRADE_LOCKED)


class StorePlanTest(unittest.TestCase):
    def test_ensure_creates_and_cancel_stops_regeneration(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            now = time.time()
            exam = {"title": "英语考试", "notes": "", "kind": "task",
                    "deadline_ts": now + 14 * 86400, "start_ts": now + 14 * 86400,
                    "task_type": "study"}
            saved = store.upsert_schedule([exam], "chat", now=now)
            info = exam_plan.ensure(store, saved, cfg, now)
            self.assertEqual(len(info["groups"]), 1)
            group = info["groups"][0]["group"]
            self.assertTrue(info["asked_scope"])
            items = store.list_schedule(100, include_archived=False, include_done=True)
            plan_items = [x for x in items if x.get("owner") == "azure"]
            self.assertGreaterEqual(len(plan_items), 2)
            self.assertTrue(all(x.get("plan_group") == group for x in plan_items))
            self.assertTrue(all("范围未说明" in x.get("notes", "") for x in plan_items))

            again = exam_plan.ensure(store, saved, cfg, now)
            self.assertEqual(again["groups"], [], "已有规划组不应重复生成")

            cancelled = store.cancel_plan(group, now)
            self.assertEqual(cancelled, len(plan_items))
            after_cancel = exam_plan.ensure(store, saved, cfg, now)
            self.assertEqual(after_cancel["groups"], [], "取消后不应自动重建")

            self.assertEqual(store.update_plan_notes(group, "范围：Unit 1", now), 0,
                             "已取消的组不再补备注")
            store.close()

    def test_update_plan_notes_after_scope_answer(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            now = time.time()
            saved = store.upsert_schedule([{
                "title": "英语考试", "notes": "", "kind": "task",
                "deadline_ts": now + 14 * 86400, "start_ts": now + 14 * 86400,
            }], "chat", now=now)
            info = exam_plan.ensure(store, saved, cfg, now)
            group = info["groups"][0]["group"]
            changed = store.update_plan_notes(group, "范围：Unit 1", now)
            self.assertGreaterEqual(changed, 1)
            items = [x for x in store.list_schedule(100, include_done=True)
                     if x.get("plan_group") == group]
            self.assertTrue(all("Unit 1" in x.get("notes", "") for x in items))
            store.close()


class PlanApiTest(unittest.TestCase):
    def _wait_plan(self, store, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            rows = [x for x in store.list_schedule(100, include_done=True)
                    if x.get("owner") == "azure"]
            if rows:
                return rows
            time.sleep(0.05)
        return [x for x in store.list_schedule(100, include_done=True)
                if x.get("owner") == "azure"]

    def test_chat_exam_triggers_revision_plan(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            cfg.auto_plan_enabled = False
            when = datetime.datetime.fromtimestamp(time.time()) + datetime.timedelta(days=14)
            date = when.date().isoformat()
            llm = ChatScheduleLLM(schedule_text=(
                '{"schedule": true, "items": [{"title": "英语考试", '
                '"date": "%s", "time": "09:00", "deadline": "%s 09:00"}]}' % (date, date)))
            bus = events.RecordingBus()
            agent = AzureAgent(StubEngine(), llm, bus, joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat", json={
                        "text": "我下下周有一场英语考试", "mode": "pet"})
                    self.assertTrue(r.json()["ok"])
                    rows = self._wait_plan(store)
                    self.assertGreaterEqual(len(rows), 2)
                    self.assertTrue(all(x.get("plan_group") for x in rows))
                    self.assertTrue(all(x.get("owner") == "azure" for x in rows))
            finally:
                store.close()

    def test_plan_cancel_and_associations_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            store = LifeStore(os.path.join(d, "life.db"))
            cfg = AzureConfig()
            now = time.time()
            saved = store.upsert_schedule([{
                "title": "结业考试", "notes": "考试范围：Unit 1、Unit 2",
                "kind": "task", "deadline_ts": now + 14 * 86400,
                "start_ts": now + 14 * 86400, "task_type": "study",
            }], "scan", now=now)
            agent = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                               joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, life=store)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/schedule/plan").json()
                    self.assertTrue(r["ok"])
                    self.assertEqual(len(r["groups"]), 1)
                    group = r["groups"][0]["group"]
                    rows = [x for x in r["items"] if x.get("plan_group") == group]
                    self.assertGreaterEqual(len(rows), 2)
                    self.assertTrue(all(x["owner"] == "azure" for x in rows))

                    c = client.post("/api/schedule/plan/cancel", json={"group": group}).json()
                    self.assertTrue(c["ok"])
                    self.assertEqual(c["cancelled"], len(rows))

                    assoc = client.get("/api/schedule/associations").json()
                    self.assertTrue(assoc["ok"])
                    self.assertIsInstance(assoc["items"], list)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
