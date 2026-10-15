"""FastAPI 装配冒烟测试（stub 引擎/LLM，不触网、不起子进程）。

覆盖 REST 命令通道与 WS 事件通道的接线：端点返回形状、WS 首帧 hello、
以及一次落子能把事件推给 WS 订阅者。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import events
from app.llm import LLMClient
from app.main import create_app
from helpers import BoomLLM, ErrLLM, StubEngine, StubLLM, mk_agent


def _client():
    app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
    return TestClient(app)


class RestTest(unittest.TestCase):
    def test_health_state_and_move(self):
        with _client() as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(set(r.json()),
                             {"ok", "katago_dead", "katago_error", "katago_url", "llm_url", "memory_rows"})

            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertFalse(body["busy"])
            self.assertEqual(body["state"]["move_count"], 2)   # 玩家一手 + 她一手

    def test_move_bad_body_no_500(self):
        """畸形/越界坐标不应打成 500，而是优雅返回 ok=false（C1）。"""
        with _client() as client:
            for body in ({"x": "abc", "y": 3}, {}, {"x": 99, "y": 3}, {"x": 3, "y": -5}):
                r = client.post("/api/move", json=body)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["ok"])
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

    def test_config_undo_reset(self):
        with _client() as client:
            self.assertTrue(client.post("/api/config", json={"allow_pass": True}).json()["ok"])
            client.post("/api/move", json={"x": 3, "y": 3})
            self.assertTrue(client.post("/api/reset").json()["ok"])
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

    def test_peer_endpoint_gone(self):
        with _client() as client:
            self.assertEqual(client.post("/api/peer", json={"fg_title": "x"}).status_code, 404)


class WebSocketTest(unittest.TestCase):
    def test_hello_on_connect(self):
        with _client() as client:
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertIn("state", hello)
                self.assertIn("mood", hello)
                self.assertIn("busy", hello)

    def test_events_pushed_to_subscriber(self):
        with _client() as client:
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "hello")

                r = client.post("/api/move", json={"x": 3, "y": 3})
                self.assertTrue(r.json()["ok"])

                got = []
                for _ in range(60):
                    e = ws.receive_json()
                    got.append(e)
                    if any(x.get("type") == "message" and x.get("kind") == events.AI_MOVE for x in got):
                        break

                types = {e["type"] for e in got}
                self.assertIn("state", types)
                self.assertIn("busy", types)
                moved = [e for e in got if e.get("type") == "message" and e.get("kind") == events.AI_MOVE]
                self.assertTrue(moved)
                self.assertIn("move", moved[0])


class CloudFailRestTest(unittest.TestCase):
    """⚠️ 云端 LLM 不可用（未配置地址 / 断网 / 抛异常）时：REST 通道仍须正常应答、棋盘自洽。"""

    def test_move_ok_without_cloud_llm(self):
        for llm in (LLMClient(base_url=""), BoomLLM()):
            with TestClient(create_app(engine=StubEngine(), llm=llm, autostart=False)) as client:
                self.assertTrue(client.get("/api/health").json()["ok"])
                self.assertEqual(client.get("/api/state").json()["move_count"], 0)
                r = client.post("/api/move", json={"x": 3, "y": 3})
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertTrue(body["ok"])
                self.assertFalse(body["busy"])
                st = body["state"]
                self.assertEqual(len(st["stones"]), st["move_count"])   # 棋盘状态自洽
                self.assertGreaterEqual(st["move_count"], 1)

    def test_ws_never_pushes_error_text(self):
        """玩家可见文本里不许出现「调用出错」：点评与落子宣告都退化为兜底台词。"""
        for llm in (LLMClient(base_url=""), ErrLLM()):    # 未配置地址 / 401
            with TestClient(create_app(engine=StubEngine(), llm=llm, autostart=False)) as client:
                with client.websocket_connect("/ws") as ws:
                    self.assertEqual(ws.receive_json()["type"], "hello")
                    self.assertTrue(client.post("/api/move", json={"x": 3, "y": 3}).json()["ok"])

                    got = []
                    for _ in range(60):
                        e = ws.receive_json()
                        got.append(e)
                        if any(x.get("type") == "message" and x.get("kind") == events.AI_MOVE for x in got):
                            break

                    moved = [e for e in got if e.get("type") == "message" and e.get("kind") == events.AI_MOVE]
                    self.assertTrue(moved, "落子宣告必须照常（Review Focus #3）")
                    for e in got:
                        self.assertNotIn("调用出错", str(e.get("text", "")))


class MemoryHealthTest(unittest.TestCase):
    """长期记忆接线：lifespan 构造 ``MemoryService`` 并注入 agent；健康面报行数。"""

    def test_disabled_reports_zero_rows(self):
        """默认关闭：不建库、健康面报 0（零开销的判据之一）。"""
        with _client() as client:
            self.assertEqual(client.get("/api/health").json()["memory_rows"], 0)

    def test_enabled_rows_reach_health_and_close_on_exit(self):
        from app.config import AzureConfig
        with tempfile.TemporaryDirectory() as d:
            cfg = AzureConfig()
            cfg.memory_enabled = True
            cfg.memory_db_path = os.path.join(d, "m.db")
            app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)
            with TestClient(app) as client:
                svc = app.state.agent.memory           # lifespan 注入的实例
                self.assertTrue(svc.available)
                self.assertIsInstance(svc.remember_fact("Estarth 喜欢点三三", tag="estarth"), int)
                self.assertEqual(client.get("/api/health").json()["memory_rows"], 1)
            # 退出时 close()：库已关，统计退回全 0（库文件仍在）
            self.assertEqual(app.state.memory.store.stats()["rows"], 0)


class MemoryApiTest(unittest.TestCase):
    """记忆管理 API：默认关闭时的端点形状（计划 03 Task 4 的钉死用例）。"""

    def test_endpoints_shape_when_disabled(self):
        with _client() as client:
            r = client.post("/api/memory/query", json={"query": "x"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"ok": True, "items": []})
            self.assertTrue(client.get("/api/memory/stats").json()["ok"])
            self.assertEqual(client.post("/api/memory/forget", json={"all": True}).json()["removed"], 0)

    def test_write_bad_body_no_500(self):
        with _client() as client:
            for body in ({}, {"text": 123}, {"text": ""}):
                r = client.post("/api/memory/write", json=body)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["ok"])


class MemoryApiEnabledTest(unittest.TestCase):
    """启用长期记忆（临时库、不配 embedding → 关键词检索）：四个端点真正读写同一个库。"""

    def _app(self, d):
        from app.config import AzureConfig
        cfg = AzureConfig()
        cfg.memory_enabled = True
        cfg.memory_db_path = os.path.join(d, "m.db")
        return create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)

    def test_write_query_stats_forget_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with TestClient(self._app(d)) as client:
                r = client.post("/api/memory/write", json={"tag": "estarth", "text": "Estarth 喜欢点三三"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["ok"])
                self.assertIsInstance(r.json()["id"], int)

                st = client.get("/api/memory/stats").json()
                self.assertTrue(st["ok"])
                self.assertEqual((st["rows"], st["games"], st["has_vectors"]), (1, 0, 0))
                self.assertTrue(st["db"].endswith("m.db"))

                q = client.post("/api/memory/query", json={"query": "点三三"}).json()
                self.assertTrue(q["ok"])
                self.assertEqual(len(q["items"]), 1)
                item = q["items"][0]
                self.assertEqual(set(item), {"id", "ts", "kind", "tag", "text", "score"})
                self.assertEqual(item["text"], "Estarth 喜欢点三三")
                self.assertEqual(item["tag"], "estarth")
                self.assertEqual(item["kind"], "fact")

                f = client.post("/api/memory/forget", json={"ids": [item["id"]]}).json()
                self.assertEqual(f, {"ok": True, "removed": 1})
                self.assertEqual(client.get("/api/memory/stats").json()["rows"], 0)

    def test_duplicate_write_and_query_k(self):
        with tempfile.TemporaryDirectory() as d:
            with TestClient(self._app(d)) as client:
                a1 = client.post("/api/memory/write", json={"tag": "t", "text": "布局喜欢点三三"}).json()
                a2 = client.post("/api/memory/write", json={"tag": "t", "text": "布局喜欢点三三"}).json()
                self.assertEqual(a1["id"], a2["id"])       # 同 tag + 同 text 不重复入库
                client.post("/api/memory/write", json={"tag": "t", "text": "布局喜欢挂角"})
                self.assertEqual(client.get("/api/memory/stats").json()["rows"], 2)

                self.assertEqual(len(client.post("/api/memory/query", json={"query": "布局", "k": 1}).json()["items"]), 1)
                self.assertEqual(len(client.post("/api/memory/query", json={"query": "布局"}).json()["items"]), 2)

    def test_top_k_zero_means_no_recall(self):
        """``memory_top_k=0`` 就是「不召回」：管理面返回空列表，不再兜底成 3 条（M2）。"""
        from app.config import AzureConfig
        with tempfile.TemporaryDirectory() as d:
            cfg = AzureConfig()
            cfg.memory_enabled = True
            cfg.memory_db_path = os.path.join(d, "m.db")
            cfg.memory_top_k = 0
            app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)
            with TestClient(app) as client:
                client.post("/api/memory/write", json={"tag": "t", "text": "布局喜欢点三三"})
                self.assertEqual(client.get("/api/memory/stats").json()["rows"], 1)
                # 缺省 k：用配置值 0 → 不召回（与出话路径 recall_for 同一语义）
                self.assertEqual(client.post("/api/memory/query", json={"query": "点三三"}).json()["items"], [])
                # 显式 k≤0：同样不召回
                self.assertEqual(client.post("/api/memory/query", json={"query": "点三三", "k": 0}).json()["items"], [])
                # 显式正数 k：照常召回
                self.assertEqual(len(client.post("/api/memory/query", json={"query": "点三三", "k": 1}).json()["items"]), 1)

    def test_memory_event_on_write_and_recall(self):
        """``memory`` 事件是元数据（op/tag/text/score），写与查各推一条；前端不产生任何信号。"""
        with tempfile.TemporaryDirectory() as d:
            with TestClient(self._app(d)) as client:
                with client.websocket_connect("/ws") as ws:
                    self.assertEqual(ws.receive_json()["type"], "hello")
                    self.assertTrue(client.post("/api/memory/write",
                                               json={"tag": "estarth", "text": "他喜欢点三三"}).json()["ok"])
                    ev = ws.receive_json()
                    self.assertEqual(set(ev), {"type", "op", "tag", "text", "score"})
                    self.assertEqual((ev["type"], ev["op"]), ("memory", "write"))
                    self.assertEqual(ev["tag"], "estarth")
                    self.assertEqual(ev["text"], "他喜欢点三三")
                    self.assertEqual(ev["score"], 0.0)

                    client.post("/api/memory/query", json={"query": "点三三"})
                    rec = ws.receive_json()
                    self.assertEqual((rec["type"], rec["op"]), ("memory", "recall"))
                    self.assertEqual(rec["text"], "他喜欢点三三")
                    self.assertEqual(rec["score"], 0.0)     # 关键词降级：不给伪相似度


class _BoomStore:
    """坏库替身：所有方法都抛异常（模拟死库 / 库结构坏掉）。"""

    def write(self, *a, **kw):
        raise RuntimeError("库坏了")

    def recall(self, *a, **kw):
        raise RuntimeError("库坏了")

    def stats(self):
        raise RuntimeError("库坏了")

    def forget(self, *a, **kw):
        raise RuntimeError("库坏了")


class _BoomMemory:
    """长期记忆替身：服务面可用（available=True）但库全面故障。

    ``close()`` 照常不抛——真实 ``MemoryService.close()`` 就是可重复调用且不上抛。
    """

    available = True

    def __init__(self):
        self.store = _BoomStore()

    def recall_for(self, *a, **kw):
        return ""

    def close(self):
        pass


class MemoryApiDegradeTest(unittest.TestCase):
    """关闭 / 库故障 / 注入 seam：端点一律降级为可读结果，显式 ``memory=`` 参数优先。"""

    def test_disabled_shapes_are_complete(self):
        """默认关闭：查询面可用（空结果）、写入被拒，四个端点都不 500。"""
        with _client() as client:
            self.assertEqual(client.get("/api/memory/stats").json(),
                             {"ok": True, "rows": 0, "games": 0, "has_vectors": 0, "db": ""})
            self.assertEqual(client.post("/api/memory/forget", json={}).json(), {"ok": True, "removed": 0})
            self.assertEqual(client.post("/api/memory/write", json={"text": "写不进去"}).json(),
                             {"ok": False, "id": None})
            self.assertEqual(client.post("/api/memory/query", json={}).json(), {"ok": True, "items": []})

    def test_broken_store_degrades_without_500(self):
        app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False, memory=_BoomMemory())
        with TestClient(app) as client:
            r = client.post("/api/memory/query", json={"query": "x"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"ok": True, "items": []})
            self.assertEqual(client.get("/api/memory/stats").json(),
                             {"ok": True, "rows": 0, "games": 0, "has_vectors": 0, "db": ""})
            self.assertEqual(client.post("/api/memory/forget", json={"all": True}).json(),
                             {"ok": True, "removed": 0})
            self.assertEqual(client.post("/api/memory/write", json={"text": "还是写不进去"}).json(),
                             {"ok": False, "id": None})

    def test_explicit_memory_takes_precedence(self):
        """``memory=`` 显式参数优先：agent 分支不再覆盖它（测试 seam 必须诚实）。"""
        agent, _eng, _llm, _bus = mk_agent()
        fake = _BoomMemory()
        app = create_app(agent=agent, autostart=False, memory=fake)
        with TestClient(app):
            self.assertIs(app.state.memory, fake)
            self.assertIs(app.state.agent.memory, fake)

    def test_injected_agent_gets_lifespan_memory_by_default(self):
        """不传 ``memory=`` 时维持现状：注入的 agent 接上 lifespan 构造的实例（默认关闭）。"""
        agent, _eng, _llm, _bus = mk_agent()
        app = create_app(agent=agent, autostart=False)
        with TestClient(app):
            self.assertIs(app.state.agent.memory, app.state.memory)
            self.assertFalse(app.state.agent.memory.available)


class AssemblyTest(unittest.TestCase):
    """装配接线：cfg 的扩展字段必须经 ``merge_extra_body`` 落到 LLMClient 与链上（Task 4 的接线点）。"""

    def test_cfg_extra_body_reaches_llm_client(self):
        from app.config import AzureConfig
        cfg = AzureConfig()
        cfg.llm_extra_body = {"top_k": 40}
        cfg.llm_think_mode = 2
        cfg.llm_think_extra_body = {"reasoning_effort": "high"}
        app = create_app(cfg=cfg, autostart=False)
        with TestClient(app):
            merged = {"top_k": 40, "reasoning_effort": "high"}
            self.assertEqual(app.state.agent.llm.extra_body, merged)
            self.assertEqual(app.state.agent.llm.chain.extra_body, merged)   # 链上也带着同一份


class FakeTTS:
    """假 TTS：不发网络请求，固定给 openai 模式。"""

    def __init__(self, mode="openai"):
        self.mode = mode
        self.spoken = []

    @property
    def enabled(self):
        return self.mode != "none"

    async def speak(self, text):
        self.spoken.append(text)
        return {"id": "a%d" % len(self.spoken), "mode": self.mode, "text": "" if self.mode == "openai" else text,
                "tone": "", "pitch": 0.0}

    def audio(self, audio_id):
        return b"RIFF" + b"\x00" * 40 if audio_id.startswith("a") else None

    async def close(self):
        pass


class TtsApiTest(unittest.TestCase):
    def test_speech_event_pairs_with_message(self):
        """每条对外台词都必须有一条对应的 speech 事件（同 turn_id，seq 递增）。"""
        app = create_app(engine=StubEngine(), llm=StubLLM(), tts=FakeTTS(), autostart=False)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()                                  # hello
                client.post("/api/move", json={"x": 3, "y": 3})
                seen = []
                for _ in range(20):
                    ev = ws.receive_json()
                    seen.append(ev)
                    if ev.get("type") == "busy" and ev.get("busy") is False:
                        break
        msgs = [e for e in seen if e.get("type") == "message"]
        speech = [e for e in seen if e.get("type") == "speech"]
        self.assertGreaterEqual(len(speech), len(msgs))
        self.assertEqual([e["seq"] for e in speech], sorted(e["seq"] for e in speech))
        self.assertEqual(len({e["turn_id"] for e in speech}), 1)

    def test_speech_suppressed_when_disabled(self):
        app = create_app(engine=StubEngine(), llm=StubLLM(), tts=FakeTTS(mode="none"), autostart=False)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                client.post("/api/move", json={"x": 3, "y": 3})
                seen = []
                for _ in range(20):
                    ev = ws.receive_json()
                    seen.append(ev)
                    if ev.get("type") == "busy" and ev.get("busy") is False:
                        break
        self.assertEqual([e for e in seen if e.get("type") == "speech"], [])
        self.assertTrue([e for e in seen if e.get("type") == "message"])

    def test_audio_endpoint_404_for_unknown(self):
        with _client() as client:
            self.assertEqual(client.get("/api/tts/audio/nope").status_code, 404)


class TtsEndpointContractTest(unittest.TestCase):
    """两个新端点的契约（T17）：合成不出来回可读 409，音频缺失回 404，**都不许 500**。"""

    def test_speak_endpoint_409_without_audio(self):
        """默认 cfg（tts_enabled=false）：试听拿不到音频 → 409 + 可读中文错误。"""
        with _client() as client:
            for body in ({}, {"text": ""}, {"text": "你好呀"}, {"text": 123}):
                r = client.post("/api/tts/speak", json=body)
                self.assertEqual(r.status_code, 409)
                self.assertFalse(r.json()["ok"])
                self.assertTrue(r.json()["error"])

    def test_speak_and_audio_roundtrip(self):
        """注入假 TTS：试听拿到 ``audio/wav``，按 ``speech`` 事件的 id 能取回同一条音频。"""
        app = create_app(engine=StubEngine(), llm=StubLLM(), tts=FakeTTS(), autostart=False)
        with TestClient(app) as client:
            r = client.post("/api/tts/speak", json={"text": "你好呀"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "audio/wav")
            self.assertTrue(r.content.startswith(b"RIFF"))
            self.assertEqual(client.get("/api/tts/audio/a1").status_code, 200)
            self.assertEqual(client.get("/api/tts/audio/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
