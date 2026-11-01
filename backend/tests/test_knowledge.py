"""知识查证（v4.6.0）：触发规则 / 结构化解析 / 网页文本 / 记忆写入 / 冷却 / API。

全部不触网：网页客户端与 LLM 都是假的；记忆用临时库。
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import events, knowledge, prompts
from app.agent import AzureAgent
from app.config import AzureConfig
from app.main import create_app
from app.memory_service import MemoryService
from helpers import StubEngine, StubLLM, ZeroRng

JSON_QUANTUM = json.dumps({
    "topic": "量子纠缠",
    "summary": "两个粒子一旦纠缠，测量其中一个会立即影响另一个的测量结果。",
    "facts": ["不能用来超光速传信息", "是量子通信与量子计算的基础资源"],
    "confidence": 0.92,
}, ensure_ascii=False)

JSON_PHOTO = json.dumps({
    "topic": "光合作用",
    "summary": "植物用光能把二氧化碳和水转成有机物，并释放氧气。",
    "facts": ["发生在叶绿体", "光反应与暗反应两个阶段"],
    "confidence": 0.88,
}, ensure_ascii=False)


def _mem_cfg(tmpdir, **kw):
    """临时记忆库配置：开记忆、关 embedding（关键词模式，零网络）。"""
    cfg = AzureConfig()
    cfg.memory_enabled = True
    cfg.memory_db_path = os.path.join(tmpdir, "memory.db")
    cfg.embed_base_url = ""
    cfg.embed_model = ""
    cfg.embed_api_key = ""
    cfg.embed_local_model = ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class FakeWeb:
    """假网页客户端：``configured=True``，按查询返回固定文本。"""

    configured = True

    def __init__(self, answers=None, default=""):
        self.answers = dict(answers or {})
        self.default = default
        self.queries = []
        self.boom = False

    def ask(self, query):
        self.queries.append(query)
        if self.boom:
            raise RuntimeError("网页断连")
        return self.answers.get(query, self.default)


class NeedTest(unittest.TestCase):
    def test_knowledge_question_triggers(self):
        self.assertTrue(knowledge.need("什么是量子纠缠"))
        self.assertTrue(knowledge.need("能帮我查一下光合作用的原理吗"))

    def test_schedule_and_private_topics_do_not_trigger(self):
        self.assertFalse(knowledge.need("明天几点上课"))
        self.assertFalse(knowledge.need("这手棋下在哪里比较好"))
        self.assertFalse(knowledge.need("你吃饭了吗"))
        self.assertFalse(knowledge.need("我睡醒了"))

    def test_science_questions_about_sleep_or_food_trigger(self):
        self.assertTrue(knowledge.need("为什么人会睡觉"))
        self.assertTrue(knowledge.need("空腹吃柿子为什么不好"))

    def test_short_or_plain_chat_does_not_trigger(self):
        self.assertFalse(knowledge.need("在吗"))
        self.assertFalse(knowledge.need("今天有点累"))

    def test_uncertain_reply_can_trigger(self):
        self.assertTrue(knowledge.need("我好像一直没弄明白这件事", "我不确定。"))
        self.assertFalse(knowledge.need("今天好累", "我不确定。"))

    def test_query_of_strips_politeness(self):
        self.assertEqual(knowledge.query_of("请问什么是量子纠缠？"), "什么是量子纠缠")
        self.assertEqual(knowledge.query_of("Azure，帮我查查光合作用"), "查查光合作用")


class ParseTest(unittest.TestCase):
    def test_structured_json_parsed(self):
        item = knowledge.parse_structured(JSON_QUANTUM, "什么是量子纠缠", "网页版")
        self.assertIsNotNone(item)
        self.assertEqual(item["topic"], "量子纠缠")
        self.assertTrue(item["summary"])
        self.assertEqual(len(item["facts"]), 2)
        self.assertAlmostEqual(item["confidence"], 0.92)
        self.assertEqual(item["source"], "网页版")

    def test_low_confidence_not_stored(self):
        low = json.dumps({"topic": "猜测", "summary": "可能是这样吧", "confidence": 0.2},
                         ensure_ascii=False)
        self.assertIsNone(knowledge.parse_structured(low, "为什么", "网页版"))

    def test_plain_text_is_low_confidence(self):
        # 未按 JSON 回答的网页正文只当低置信摘要：默认阈值下不写记忆
        self.assertIsNone(knowledge.parse_structured("随便一段网页正文", "为什么", "网页版"))
        got = knowledge.parse_structured("随便一段网页正文", "为什么", "网页版",
                                         min_confidence=0.4)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got["confidence"], 0.5)

    def test_json_without_summary_is_rejected(self):
        self.assertIsNone(knowledge.parse_structured(
            '{"topic":"x","confidence":0.9}', "问题", "网页版"))

    def test_facts_are_clipped_and_deduped(self):
        raw = json.dumps({"topic": "t", "summary": "s", "confidence": 0.9,
                          "facts": ["同一句", "同一句", "x" * 200]}, ensure_ascii=False)
        item = knowledge.parse_structured(raw, "q", "网页版")
        self.assertEqual(len(item["facts"]), 2)
        self.assertLessEqual(len(item["facts"][1]), knowledge.MAX_FACT_CHARS)


class WebTextTest(unittest.TestCase):
    def test_sse_incremental_fragments_joined(self):
        body = "\n".join([
            'data: {"v":{"response":{"fragments":[{"content":"量子"}]}}}',
            'data: {"v":{"response":{"fragments":[{"content":"纠缠"}]}}}',
            "data: [DONE]",
        ])
        self.assertEqual(knowledge._extract_web_text(body, "text/event-stream"), "量子纠缠")

    def test_sse_cumulative_text_not_duplicated(self):
        body = "\n".join([
            'data: {"content":"量子"}',
            'data: {"content":"量子纠缠"}',
            "data: [DONE]",
        ])
        self.assertEqual(knowledge._extract_web_text(body, "text/event-stream"), "量子纠缠")

    def test_plain_json_takes_longest_text(self):
        body = json.dumps({"data": {"answer": "短句", "result": "一段完整的回答正文"}},
                          ensure_ascii=False)
        self.assertEqual(knowledge._extract_web_text(body), "一段完整的回答正文")

    def test_unparseable_body_returned_as_is(self):
        self.assertEqual(knowledge._extract_web_text("就是一段纯文本"), "就是一段纯文本")


class RenderTest(unittest.TestCase):
    def _rows(self):
        return [
            {"id": 1, "ts": 10, "tag": "knowledge:量子纠缠",
             "meta": {"topic": "量子纠缠", "summary": "两个粒子状态相关", "facts": ["不能超光速"]}},
            {"id": 2, "ts": 20, "tag": "knowledge:光合作用",
             "meta": {"topic": "光合作用", "summary": "植物把光变成有机物", "facts": []}},
        ]

    def test_matches_related_topic(self):
        out = knowledge.render_note(self._rows(), "量子纠缠是什么")
        self.assertIn("量子纠缠", out)
        self.assertNotIn("光合作用", out)

    def test_empty_query_or_rows(self):
        self.assertEqual(knowledge.render_note(self._rows(), ""), "")
        self.assertEqual(knowledge.render_note([], "量子"), "")

    def test_no_overlap_means_no_note(self):
        self.assertEqual(knowledge.render_note(self._rows(), "今天天气"), "")

    def test_memory_text_is_personified(self):
        text = knowledge.memory_text({"topic": "量子纠缠", "summary": "状态相关",
                                      "facts": ["不能超光速"]})
        self.assertIn("她查过「量子纠缠」", text)
        self.assertIn("要点：", text)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _mem_cfg(self.tmp.name)
        self.mem = MemoryService(self.cfg)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def _svc(self, web, **kw):
        for k, v in kw.items():
            setattr(self.cfg, k, v)
        return knowledge.KnowledgeService(self.cfg, memory=self.mem, web=web)

    def test_lookup_and_remember_writes_knowledge(self):
        web = FakeWeb({"什么是量子纠缠": JSON_QUANTUM})
        svc = self._svc(web)
        item = svc.lookup_and_remember("什么是量子纠缠")
        self.assertIsNotNone(item)
        rows = self.mem.knowledge_rows(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "knowledge:量子纠缠")
        self.assertEqual(rows[0]["meta"]["summary"], item["summary"])
        self.assertIn("量子纠缠", svc.note_for("量子纠缠是什么"))

    def test_low_confidence_not_written(self):
        low = json.dumps({"topic": "猜测", "summary": "可能是吧", "confidence": 0.1},
                         ensure_ascii=False)
        svc = self._svc(FakeWeb({"为什么": low}))
        self.assertIsNone(svc.lookup_and_remember("为什么"))
        self.assertEqual(self.mem.knowledge_rows(5), [])

    def test_same_topic_supersedes_old(self):
        first = json.dumps({"topic": "量子纠缠", "summary": "第一版摘要", "confidence": 0.9},
                           ensure_ascii=False)
        second = json.dumps({"topic": "量子纠缠", "summary": "第二版摘要", "confidence": 0.9},
                            ensure_ascii=False)
        svc = self._svc(FakeWeb({"什么是量子纠缠": first}))
        svc.lookup_and_remember("什么是量子纠缠")
        svc._last.clear()                       # 绕开冷却，模拟另一天再查同一话题
        svc.web.answers["什么是量子纠缠"] = second
        svc.lookup_and_remember("什么是量子纠缠")
        rows = self.mem.knowledge_rows(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["summary"], "第二版摘要")

    def test_web_error_is_swallowed(self):
        web = FakeWeb({"什么是量子纠缠": JSON_QUANTUM})
        web.boom = True
        svc = self._svc(web)
        self.assertIsNone(svc.lookup("什么是量子纠缠"))

    def test_web_not_configured_means_no_request(self):
        cfg_off = _mem_cfg(self.tmp.name, knowledge_provider="deepseek_web")
        web = FakeWeb(default=JSON_QUANTUM)
        web.configured = False
        svc = knowledge.KnowledgeService(cfg_off, memory=self.mem, web=web)
        self.assertFalse(svc.available)
        self.assertIsNone(svc.lookup_and_remember("什么是量子纠缠"))
        self.assertEqual(web.queries, [])

    def test_note_empty_when_disabled(self):
        self.cfg.knowledge_enabled = False
        svc = self._svc(FakeWeb(default=JSON_QUANTUM))
        self.assertEqual(svc.note_for("量子纠缠"), "")

    def test_cooldown_success_uses_full_window(self):
        svc = self._svc(FakeWeb(default=JSON_QUANTUM))
        svc._mark("量子纠缠问题", True)
        self.assertTrue(svc._cool("量子纠缠问题"))
        svc._last["量子纠缠问题"] = (time.monotonic() - svc.cooldown - 1.0, True)
        self.assertFalse(svc._cool("量子纠缠问题"))

    def test_cooldown_failure_is_short(self):
        svc = self._svc(FakeWeb(default=JSON_QUANTUM))
        svc._mark("量子纠缠问题", False)
        self.assertTrue(svc._cool("量子纠缠问题"))
        # 失败冷却最多 60 秒：哪怕成功冷却 600 秒，两分钟后也能重查
        svc._last["量子纠缠问题"] = (time.monotonic() - 120.0, False)
        self.assertFalse(svc._cool("量子纠缠问题"))

    def test_lookup_async_uses_web_thread_and_remembers(self):
        svc = self._svc(FakeWeb({"什么是量子纠缠": JSON_QUANTUM}))
        item = asyncio.run(svc.lookup_and_remember_async("什么是量子纠缠"))
        self.assertIsNotNone(item)
        self.assertEqual(len(self.mem.knowledge_rows(5)), 1)

    def test_llm_provider_async_fallback(self):
        llm = StubLLM(text=JSON_PHOTO)
        svc = self._svc(FakeWeb(default=""), knowledge_provider="llm", knowledge_llm_fallback=True)
        svc.llm = llm
        self.assertTrue(svc.available)
        item = asyncio.run(svc.lookup_async("光合作用是什么"))
        self.assertIsNotNone(item)
        self.assertEqual(item["source"], "本地模型兜底")
        self.assertEqual(len(llm.calls), 1)


class PromptTest(unittest.TestCase):
    def test_empty_knowledge_keeps_block_identical(self):
        base = prompts.life_context_block([], schedule_text="明天有课")
        same = prompts.life_context_block([], schedule_text="明天有课", knowledge_text="")
        self.assertEqual(base, same)
        self.assertNotIn("查过的资料", base)

    def test_knowledge_block_inserted(self):
        out = prompts.life_context_block([], knowledge_text="- 「量子纠缠」：状态相关")
        self.assertIn("查过的资料", out)
        self.assertIn("量子纠缠", out)
        self.assertIn("别念出处", out)


class ApiTest(unittest.TestCase):
    def test_search_and_recent_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _mem_cfg(d)
            mem = MemoryService(cfg)
            web = FakeWeb({"什么是量子纠缠": JSON_QUANTUM, "什么是光合作用": JSON_PHOTO})
            ks = knowledge.KnowledgeService(cfg, memory=mem, web=web)
            agent = AzureAgent(StubEngine(), StubLLM(text="这个我知道一点。"),
                               events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, memory=mem, knowledge=ks)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/knowledge/search", json={"query": "什么是量子纠缠"})
                    body = r.json()
                    self.assertTrue(body["ok"], body)
                    self.assertEqual(body["item"]["topic"], "量子纠缠")

                    r = client.post("/api/knowledge/search", json={"query": ""})
                    self.assertFalse(r.json()["ok"])

                    items = client.get("/api/knowledge/recent").json()["items"]
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["topic"], "量子纠缠")
                    self.assertEqual(items[0]["facts"], ["不能用来超光速传信息",
                                                         "是量子通信与量子计算的基础资源"])

                    # 健康面报「可用」，且不泄漏 token / cookie
                    health = client.get("/api/health").json()
                    self.assertTrue(health["knowledge_available"])
                    self.assertNotIn("token", health)
                    self.assertNotIn("cookie", health)
            finally:
                mem.close()

    def test_chat_triggers_background_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _mem_cfg(d)
            mem = MemoryService(cfg)
            web = FakeWeb({"光合作用是什么意思": JSON_PHOTO})
            ks = knowledge.KnowledgeService(cfg, memory=mem, web=web)
            agent = AzureAgent(StubEngine(), StubLLM(text="这个我知道一点。"),
                               events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, memory=mem, knowledge=ks)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat", json={"text": "光合作用是什么意思", "mode": "pet"})
                    self.assertTrue(r.json()["ok"], r.json())
                    deadline = time.time() + 2.0
                    while not mem.knowledge_rows(5) and time.time() < deadline:
                        time.sleep(0.02)
                    rows = mem.knowledge_rows(5)
                    self.assertEqual(len(rows), 1)
                    self.assertIn("光合作用", rows[0]["meta"]["topic"])
                    self.assertTrue(web.queries)
            finally:
                mem.close()

    def test_chat_with_schedule_like_text_does_not_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _mem_cfg(d)
            mem = MemoryService(cfg)
            web = FakeWeb(default=JSON_QUANTUM)
            ks = knowledge.KnowledgeService(cfg, memory=mem, web=web)
            agent = AzureAgent(StubEngine(), StubLLM(text="好，记下了。"),
                               events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, memory=mem, knowledge=ks)
            try:
                with TestClient(app) as client:
                    r = client.post("/api/chat", json={"text": "明天几点上课？", "mode": "pet"})
                    self.assertTrue(r.json()["ok"], r.json())
                    time.sleep(0.1)
                    self.assertEqual(web.queries, [])
                    self.assertEqual(mem.knowledge_rows(5), [])
            finally:
                mem.close()

    def test_unavailable_service_returns_readable_errors(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _mem_cfg(d, knowledge_enabled=False)
            mem = MemoryService(cfg)
            ks = knowledge.KnowledgeService(cfg, memory=mem, web=FakeWeb(default=JSON_QUANTUM))
            agent = AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                               joseki_rate=1.0, joseki_rng=ZeroRng())
            app = create_app(cfg=cfg, agent=agent, autostart=False, memory=mem, knowledge=ks)
            try:
                with TestClient(app) as client:
                    self.assertFalse(client.post("/api/knowledge/search",
                                                 json={"query": "什么是量子纠缠"}).json()["ok"])
                    self.assertEqual(client.get("/api/knowledge/recent").json()["items"], [])
                    self.assertFalse(client.get("/api/health").json()["knowledge_available"])
            finally:
                mem.close()


if __name__ == "__main__":
    unittest.main()
