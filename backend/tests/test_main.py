"""FastAPI 装配冒烟测试（stub 引擎/LLM，不触网、不起子进程）。

覆盖 REST 命令通道与 WS 事件通道的接线：端点返回形状、WS 首帧 hello、
以及一次落子能把事件推给 WS 订阅者。
"""

import copy
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import events
from app.config import AzureConfig, DRIFT_MAX_LIMIT
from app.llm import LLMClient
from app.main import create_app
from app.profile_store import ProfileStore
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

    def test_peer_endpoint_forwards_info(self):
        """窥屏端点恢复：客户端把前台窗口 / OCR / VLM 描述交给后端组织台词。"""
        with _client() as client:
            r = client.post("/api/peer", json={"fg_title": "Bilibili",
                                               "ocr": "围棋教学视频",
                                               "vlm": "他正在看一个围棋教学视频"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

    def test_config_forwards_emotion_drift(self):
        """``/api/config`` 透传情绪波动开关与参数（v3.8.0）：脏值不生效、越界按上限夹取、绝不 500。

        修复轮 1：`drift_max` 夹到「小概率」上限 0.2（不是 1.0）、`drift_base` 再夹到 `[0, drift_max]`
        （Minor #3）；开关只认 JSON 的 true / false（Minor #7）。
        """
        app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app) as client:
            r = client.post("/api/config", json={"emotion_drift": True, "drift_base": 0.05,
                                                 "drift_max": 0.09, "pv_max_drop": 0.02})
            self.assertTrue(r.json()["ok"])
            s = app.state.agent
            self.assertTrue(s.emotion_drift)
            self.assertAlmostEqual(s.drift_base, 0.05)
            self.assertAlmostEqual(s.drift_max, 0.09)
            self.assertAlmostEqual(s.pv_max_drop, 0.02)

            r = client.post("/api/config", json={"drift_base": "abc", "drift_max": 5, "pv_max_drop": -1})
            self.assertTrue(r.json()["ok"])
            self.assertAlmostEqual(s.drift_base, 0.05, msg="脏值不生效：保留原值")
            self.assertAlmostEqual(s.drift_max, DRIFT_MAX_LIMIT, msg="drift_max 夹到 0.2（不是 1.0）")
            self.assertAlmostEqual(s.pv_max_drop, 0.0)

            client.post("/api/config", json={"drift_base": 0.9})
            self.assertAlmostEqual(s.drift_base, DRIFT_MAX_LIMIT, msg="base 不许超过 max")

            # 开关**只认 JSON 的 true / false**：字符串 "false" 是脏值 → 不生效（保留原值）
            client.post("/api/config", json={"emotion_drift": True})
            client.post("/api/config", json={"emotion_drift": "false"})
            self.assertTrue(s.emotion_drift, "脏值不生效（`bool('false')` 会把脏值读成「关」）")
            client.post("/api/config", json={"emotion_drift": False})
            self.assertFalse(s.emotion_drift, "开关能关回去（关掉＝现状）")


class AdaptiveSwitchTest(unittest.TestCase):
    """``/api/config`` 接通自适应难度（v3.11.5）：开关 + 基线一次落定，脏值不生效。"""

    def test_config_forwards_adaptive_switch_and_base(self):
        cfg = AzureConfig()
        app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app) as client:
            s = app.state.agent
            self.assertFalse(s.adaptive.enabled)
            self.assertEqual(s.adaptive.base_cfg, 3)
            r = client.post("/api/config", json={"adaptive_difficulty": True,
                                                 "adaptive_difficulty_base": 4})
            self.assertTrue(r.json()["ok"])
            self.assertTrue(s.adaptive.enabled)
            self.assertEqual((s.adaptive.base_cfg, s.adaptive.level), (4, 4))
            client.post("/api/config", json={"adaptive_difficulty_base": "oops"})
            self.assertEqual(s.adaptive.base_cfg, 4, "脏值不生效")
            client.post("/api/config", json={"adaptive_difficulty_base": 9})
            self.assertEqual(s.adaptive.base_cfg, 5, "越界夹到 1..5")


class ProfileTimeSwitchTest(unittest.TestCase):
    """``/api/config`` 透传两个开关（v3.9.0）：默认关、只认 JSON 的 true / false、脏值不生效。"""

    def test_config_forwards_profile_and_time_switches(self):
        # v3.9.0（Q1）：库路径指到临时目录——「空库 ⇒ 无画像」这一条不许依赖本机有没有
        # ``backend/data/profile.db``（那是运行产物，开发机上有真实数据）。
        tmp = tempfile.mkdtemp()
        cfg = AzureConfig()
        cfg.profile_db_path = os.path.join(tmp, "profile.db")
        app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app) as client:
            s = app.state.agent
            self.assertIsNotNone(s.profile_store, "画像库**无条件装配**（懒建库、零开销）")
            self.assertFalse(s.profile_enabled)
            self.assertFalse(s.time_aware_enabled)
            self.assertEqual(s.profile_facts(), {}, "关着 ⇒ 画像块素材为空（＝不注入）")
            self.assertEqual(s.time_facts(now=1.0), {}, "关着 ⇒ 时间块素材为空（＝不注入）")

            r = client.post("/api/config", json={"profile_enabled": True, "time_aware_enabled": True})
            self.assertTrue(r.json()["ok"])
            self.assertTrue(s.profile_enabled)
            self.assertTrue(s.time_aware_enabled)
            self.assertIn("date", s.time_facts(now=1792414980.0), "开着 ⇒ 时间事实真的有产出")
            # 库装配着但**是空的** ⇒ 开着也是「无画像」，不抛（空库 ≠ 故障）
            self.assertEqual(s.profile_facts(), {})

            # 脏值不生效（`bool("false")` 会把脏值读成「开」，所以只认 bool）
            client.post("/api/config", json={"profile_enabled": "false", "time_aware_enabled": 0})
            self.assertTrue(s.profile_enabled)
            self.assertTrue(s.time_aware_enabled)
            client.post("/api/config", json={"profile_enabled": False, "time_aware_enabled": False})
            self.assertFalse(s.profile_enabled)
            self.assertFalse(s.time_aware_enabled)

    def test_injected_agent_gets_the_cfg_switches(self):
        """注入的 agent 也以 cfg 为准（与 memory / tts / joseki_progress 同一套装配纪律）。"""
        cfg = AzureConfig()
        cfg.profile_enabled = True
        cfg.time_aware_enabled = True
        agent, _eng, _llm, _bus = mk_agent()
        app = create_app(cfg=cfg, agent=agent, autostart=False)
        with TestClient(app):
            self.assertTrue(agent.profile_enabled)
            self.assertTrue(agent.time_aware_enabled)
            self.assertIsNotNone(agent.profile_store, "注入的 agent 也接上同一个画像库实例")


class LocalVoiceConfigTest(unittest.TestCase):
    """``/api/config`` 本地音色：只改后端内存里的 TTS 配置（不重启、不丢棋局），脏值不生效。"""

    def test_config_updates_live_tts_voice(self):
        cfg = AzureConfig()
        cfg.tts_local_voice = "xiaoya"
        app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app) as client:
            tts = app.state.tts
            self.assertIsNotNone(tts, "TTS 客户端无条件装配")
            self.assertIsNotNone(getattr(tts, "cfg", None), "TTS 持有配置对象（运行期音色就改它）")
            r = client.post("/api/config", json={"tts_local_voice": "azure"})
            self.assertTrue(r.json()["ok"])
            self.assertEqual(tts.cfg.tts_local_voice, "azure")
            client.post("/api/config", json={"tts_local_voice": 7})   # 脏值不生效
            self.assertEqual(tts.cfg.tts_local_voice, "azure")


class ProfileApiTest(unittest.TestCase):
    """``GET /api/profile``（v3.9.0 / Q6）：**只读**，各维值 + 置信度 + 最近 N 局记录；无画像 ⇒ 空结果、不 500。"""

    def _app(self, tmp, seed=True):
        db = os.path.join(tmp, "profile.db")
        cfg = AzureConfig()
        cfg.profile_db_path = db
        if seed:
            store = ProfileStore(db)
            store.add_game({"ts": time.time() - 86400.0, "move_count": 20, "result": "黑方胜"})
            store.upsert_axes({"open.star": {"value": 0.9, "confidence": 0.4, "n_games": 8}})
        return create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)

    def test_reads_axes_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with TestClient(app) as client:
                r = client.get("/api/profile")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual(set(body), {"ok", "enabled", "axes", "games"})
                self.assertTrue(body["ok"])
                self.assertFalse(body["enabled"], "开关默认关——关着也照常可读")
                self.assertAlmostEqual(body["axes"]["open.star"]["value"], 0.9)
                self.assertAlmostEqual(body["axes"]["open.star"]["confidence"], 0.4)
                self.assertEqual(body["axes"]["open.star"]["n_games"], 8)
                self.assertEqual(len(body["games"]), 1,
                                 "最近 N 局记录（全局最近，不是每维各自的来源）")
                self.assertEqual(set(body["games"][0]), {"id", "ts", "move_count", "result"})
                self.assertEqual(body["games"][0]["result"], "黑方胜")
                # 开关打开后面板能区分「开着但还没攒下」与「关着」
                client.post("/api/config", json={"profile_enabled": True})
                self.assertTrue(client.get("/api/profile").json()["enabled"])

    def test_empty_profile_is_not_a_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(self._app(tmp, seed=False)) as client:
                r = client.get("/api/profile")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["axes"], {}, "空库 ⇒ 空画像（不是 500）")
                self.assertEqual(r.json()["games"], [])

    def test_summarize_writes_the_profile_end_to_end(self):
        """端到端（Q7）：开关打开 + 走真端点 ⇒ 结束后 ``GET /api/profile`` 里真的出现画像与最近 N 局记录。

        这是「画像真的生效」的整链路证据：落子（采集）→ ``/api/summarize``（写记录 + 融合画像）
        → 只读端点（各维 + 置信度 + 最近 N 局记录）。全程零额外 LLM / 引擎调用（与 ``test_agent`` 的
        调用数断言成对）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AzureConfig()
            cfg.profile_db_path = os.path.join(tmp, "profile.db")
            cfg.profile_enabled = True
            with TestClient(create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(),
                                       autostart=False)) as client:
                self.assertTrue(client.post("/api/move", json={"x": 3, "y": 3}).json()["ok"])
                self.assertTrue(client.post("/api/move", json={"x": 15, "y": 15}).json()["ok"])
                self.assertTrue(client.post("/api/summarize", json={"note": ""}).json()["ok"])

                body = client.get("/api/profile").json()
                self.assertTrue(body["enabled"])
                self.assertIn("open.star", body["axes"], "两手的开局偏好真的攒下来了")
                self.assertAlmostEqual(body["axes"]["open.star"]["value"], 1.0, places=6)
                self.assertIn("blunder.rate", body["axes"],
                              "玩家每一手都带 drop（0.0＝量过、没亏）⇒ 失误那一组也产出")
                self.assertAlmostEqual(body["axes"]["blunder.rate"]["value"], 0.0, places=6)
                self.assertEqual(len(body["games"]), 1, "最近 N 局记录：这一局可追溯")
                self.assertEqual(body["games"][0]["move_count"], 4)

    def test_broken_store_still_returns_an_empty_result(self):
        """库不可用（父路径是个文件 ⇒ sqlite 打不开）⇒ 照旧 200 + 空结果（与记忆管理面同一套口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "afile")            # 普通文件：拿它当目录用必然打不开库
            with open(bad, "w", encoding="utf-8") as f:
                f.write("not a dir")
            cfg = AzureConfig()
            cfg.profile_db_path = os.path.join(bad, "profile.db")
            with TestClient(create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(),
                                       autostart=False)) as client:
                r = client.get("/api/profile")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["axes"], {})
                self.assertEqual(r.json()["games"], [])


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
        cfg.profile_db_path = os.path.join(d, "p.db")   # 别读真实画像：否则连上 WS 会先推约战提醒
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
        with tempfile.TemporaryDirectory() as d:
            cfg = AzureConfig()
            cfg.profile_db_path = os.path.join(d, "p.db")   # 空画像：不触发主动约战提醒
            app = create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(),
                             tts=FakeTTS(), autostart=False)
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


# ================= 计划 05 · Task 3 修复轮（T20）：异步路径的玩家可见台词也要带 speech =================
# 这一组是 **agent 级**用例（直接看 bus 上的事件序列，不走 WS）：换引擎/换 TTS 注入最省事，
# 也避开了 WS 读取的时序不确定性。放在本文件是因为本轮只许改 test_main.py（其余测试文件不动）。

import asyncio


class ErrEngine(StubEngine):
    """引擎失败（云服务不可用）：``analyze`` 回 ``{"error": …}`` ⇒ prepare 的短路提示路径。"""

    async def analyze(self, moves, max_visits=None):
        return {"error": "云服务不可用"}


class BoomEngine(StubEngine):
    """引擎直接抛（比「回 error」更凶的失败形态）⇒ 公开方法的 ``except`` 兜底路径。"""

    async def analyze(self, moves, max_visits=None):
        raise RuntimeError("云服务断连")


def _mk_tts_agent(engine=None, llm=None, tts=None):
    """造一个 RecordingBus 的 agent（复用本文件的 stub），可注入引擎 / LLM / TTS。"""
    from app.agent import AzureAgent
    bus = events.RecordingBus()
    return AzureAgent(engine or StubEngine(), llm or StubLLM(), bus, tts=tts), bus


class TtsAsyncPathTest(unittest.TestCase):
    """T20①：异步路径上的每条玩家可见台词，都必须紧跟一条同 ``turn_id`` 的 ``speech``。"""

    def _assert_paired(self, agent, bus):
        """有 ``message`` 就有紧随的 ``speech``（同 ``turn_id``、``seq`` 从 1 连续）。"""
        evs = bus.events
        msgs = [i for i, e in enumerate(evs) if e.get("type") == "message"]
        sps = [e for e in evs if e.get("type") == "speech"]
        self.assertTrue(msgs, "这条路径必须有玩家可见台词")
        self.assertEqual(len(sps), len(msgs), "message 与 speech 必须一一配对：%s" % evs)
        for i in msgs:
            self.assertEqual(evs[i + 1]["type"], "speech", "speech 必须紧跟 message：%s" % evs[i])
            self.assertEqual(evs[i + 1]["turn_id"], agent.turn_id)
            self.assertNotIn("turn_id", evs[i])          # message 的形状不许变
        self.assertEqual([e["seq"] for e in sps], list(range(1, len(msgs) + 1)))

    def test_engine_failure_shortcut_gets_speech(self):
        """引擎失败短路提示（``_prepare_observe``）也要发声。"""
        agent, bus = _mk_tts_agent(engine=ErrEngine(), tts=FakeTTS())
        asyncio.run(agent.hint())
        self.assertIn("一时算不清", bus.messages()[0]["text"])
        self._assert_paired(agent, bus)

    def test_engine_failure_analyze_shortcut_gets_speech(self):
        """引擎失败短路提示（``_prepare_analyze``）也要发声（先攒一手棋谱）。"""
        agent, bus = _mk_tts_agent(engine=StubEngine(), tts=FakeTTS())

        async def go(a):
            await a.player_move(3, 3)
            a.bus.clear()
            a.katago = ErrEngine()
            await a.analyze_position()

        asyncio.run(go(agent))
        self.assertIn("一时算不清", bus.messages()[0]["text"])
        self._assert_paired(agent, bus)

    def test_empty_board_hints_get_speech(self):
        """空盘提示（``analyze_position`` / ``summarize`` 的早退分支）也要发声。"""
        for name, args in (("analyze_position", ()), ("summarize", ("",))):
            agent, bus = _mk_tts_agent(tts=FakeTTS())
            with self.subTest(case=name):
                asyncio.run(getattr(agent, name)(*args))
                self._assert_paired(agent, bus)

    def test_exception_fallbacks_get_speech(self):
        """三个公开方法的异常兜底台词（引擎直接抛）也要发声。"""
        for name, args in (("hint", ()), ("analyze_position", ()), ("summarize", ("",))):
            agent, bus = _mk_tts_agent(tts=FakeTTS())

            async def go(a, name=name, args=args):
                await a.player_move(3, 3)               # 先用正常引擎攒一手棋谱
                a.bus.clear()
                a.katago = BoomEngine()                 # 再换成「直接抛」的引擎
                await getattr(a, name)(*args)

            with self.subTest(case=name):
                asyncio.run(go(agent))
                self._assert_paired(agent, bus)

    def test_illegal_move_hint_gets_speech(self):
        """非法手提示（``player_move`` 的早退分支）也要发声，且与提示同 ``turn_id``。"""
        agent, bus = _mk_tts_agent(tts=FakeTTS())

        async def go(a):
            await a.player_move(3, 3)
            a.bus.clear()
            await a.player_move(3, 3)                   # 同一手再落一次 ⇒ 非法手提示

        asyncio.run(go(agent))
        self.assertTrue(bus.messages()[0]["text"].startswith("这手不成立"))
        self._assert_paired(agent, bus)
        types = [e["type"] for e in bus.events]
        self.assertIn("state", types)
        self.assertLess(types.index("state"), types.index("message"),
                        "界面重同步不能被合成时长拖延（state 先于提示）")

    def test_system_mode_speech_carries_text(self):
        """T20③①：``mode == "system"`` 的 ``speech`` 必须带**非空** ``text``（前端据此走系统语音）。"""
        agent, bus = _mk_tts_agent(tts=FakeTTS("system"))
        asyncio.run(agent.hint())
        sps = bus.of_type("speech")
        self.assertTrue(sps)
        self.assertEqual({e["mode"] for e in sps}, {"system"})
        self.assertTrue(all(e["text"] for e in sps), "system 模式的 text 不得为空：%s" % sps)

    def test_no_speech_when_tts_not_wired(self):
        """T20③②：``tts is None``（未接线）⇒ 一条 ``speech`` 都不发，文字照常。"""
        agent, bus = _mk_tts_agent(tts=None)
        asyncio.run(agent.hint())
        self.assertEqual(bus.of_type("speech"), [])
        self.assertTrue(bus.messages())

    def test_sync_short_hints_stay_text_only(self):
        """T20②：``undo``（两条）/ ``reset_game`` / ``set_allow_pass`` 的短提示**故意保持纯文字**。

        它们是同步方法：补 ``speech`` 就得 ``await``（S5 不许改签名）+ 改既有端点（T18），故本轮显式接受。
        """
        agent, bus = _mk_tts_agent(tts=FakeTTS())

        async def go(a):
            await a.player_move(3, 3)
            a.bus.clear()
            a.undo(0)                                    # 有效回档 → 「退回第 N 手」
            a.undo(-1)                                   # 回档点不存在 → 「已经不存在了哦」
            a.set_allow_pass(True)
            a.reset_game()

        asyncio.run(go(agent))
        self.assertEqual(len(bus.messages()), 4)         # undo×2 / set_allow_pass / reset_game 各一条
        self.assertEqual(bus.of_type("speech"), [], "同步方法的短提示不得发 speech（T20②）")


class ResetSilentTest(unittest.TestCase):
    """v3.12.1：从主界面进对局走 ``silent`` reset——只推 state，不添「重新来吧」。"""

    def test_silent_reset_skips_the_hint(self):
        agent, bus = _mk_tts_agent(tts=None)
        asyncio.run(agent.player_move(3, 3))
        bus.clear()
        agent.reset_game(silent=True)
        self.assertEqual(bus.of_type("message"), [], "静默 reset 不该有台词")
        self.assertTrue(bus.of_type("state"), "棋盘快照照发")

    def test_endpoint_accepts_no_body_and_silent(self):
        """旧调用（不带 body）与从主界面进门（``silent``）都返回 ok。"""
        app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app) as client:
            self.assertTrue(client.post("/api/reset").json()["ok"])
            self.assertTrue(client.post("/api/reset", json={"silent": True}).json()["ok"])


# ================= 计划 08 · Task C：后端复盘模式（v3.7.0） =================
# 复盘是**后端的模式**（只把前端棋盘设成只读挡不住她出声）：这里守门禁（复盘先于 busy）、
# 只读重放、静音、存档形状，以及阶段一顺延的 V13 剪枝。
# 沿用本文件既有的 ``create_app(engine=…, llm=…, autostart=False)`` + ``TestClient`` 套路；
# 需要看 bus 事件（静音）或直接注入状态（V15 / finished / V13）的用例改为注入 agent。


class ReviewModeTest(unittest.TestCase):
    def _client(self):
        app = create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)
        return app, TestClient(app)

    def _enter(self, client, on=True):
        r = client.post("/api/review", json={"on": on})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _goto(self, client, n):
        r = client.post("/api/review/goto", json={"n": n})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _next_state(self, ws, tries=6):
        """读到第一条 ``state`` 事件（``goto`` 只推这一条）。"""
        for _ in range(tries):
            e = ws.receive_json()
            if e.get("type") == "state":
                return e
        self.fail("没收到 state 事件")

    def test_review_intro_uses_real_state(self):
        """复盘开场白：空盘温和提示；有亏损手时点出第几手 / 大致掉多少；复盘模式下拒绝。"""
        app, client = self._client()
        with client:
            r = client.post("/api/review/intro")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertIn("空着", r.json()["text"])

            st = app.state.agent.st
            st.add_move("B", "Q16")
            st.record_loss("Q16", 0.23, "测试")
            st.add_move("W", "D4")
            r2 = client.post("/api/review/intro")
            text = r2.json()["text"]
            self.assertIn("第 1 手", text)
            self.assertIn("23%", text)

            self._enter(client)
            r3 = client.post("/api/review/intro")
            self.assertEqual(r3.status_code, 200)
            self.assertFalse(r3.json()["ok"])
            self.assertEqual(r3.json()["error"], "复盘模式中")

    def test_review_blocks_move_chat_idle(self):
        """① 复盘期间六个入口一律可读拒绝（V6 形状），且**不碰 busy**、一条都落不下去。"""
        app, client = self._client()
        with client:
            self.assertEqual(self._enter(client), {"ok": True, "on": True})
            for path, body in (("/api/move", {"x": 3, "y": 3}),
                               ("/api/chat", {"text": "在吗"}),
                               ("/api/idle", {}),
                               ("/api/peer", {"fg_title": "Bilibili"}),
                               ("/api/hint", {}),
                               ("/api/analyze", {})):
                with self.subTest(path=path):
                    r = client.post(path, json=body)
                    self.assertEqual(r.status_code, 200)
                    # V6：不复用 busy 那套形状（{"ok": False, "busy": True, "state": …}）
                    self.assertEqual(r.json(), {"ok": False, "error": "复盘模式中"})
                    self.assertFalse(app.state.agent.busy, "复盘拒绝不得改动 busy")
            self.assertEqual(client.get("/api/state").json()["move_count"], 0)

    def test_exit_review_restores_normal_play(self):
        """② 退出复盘后落子重新可用（复盘可进可出）。"""
        app, client = self._client()
        with client:
            self._enter(client, True)
            self.assertEqual(self._enter(client, False), {"ok": True, "on": False})
            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertTrue(r.json()["ok"])
            self.assertEqual(r.json()["state"]["move_count"], 2)

    def test_enter_review_while_busy_keeps_busy(self):
        """V15：busy 为真时**允许**进复盘（否则永远进不去），但进出都不许擅自改 busy。"""
        app, client = self._client()
        with client:
            agent = app.state.agent
            agent._set_busy(True)
            self.assertEqual(self._enter(client, True), {"ok": True, "on": True})
            self.assertTrue(agent.busy, "进复盘不许动 busy")
            # 判定顺序：复盘**先于** busy（V6）——两个都命中时回的是复盘那套形状
            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertEqual(r.json(), {"ok": False, "error": "复盘模式中"})
            self.assertTrue(agent.busy)
            self._enter(client, False)
            self.assertTrue(agent.busy, "退出复盘也不许擅自改 busy")

    def test_review_does_not_touch_finished_semantics(self):
        """③ 复盘进出不改 ``finished`` 语义：结束后仍可继续落子这个现状不变。"""
        app, client = self._client()
        with client:
            agent = app.state.agent
            agent.st.finished = True                        # 现状：总结之后仍能接着下
            self._enter(client, True)
            self._enter(client, False)
            self.assertTrue(agent.st.finished, "复盘不许碰 finished")
            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertTrue(r.json()["ok"], "finished 为真不挡落子（既有语义）")
            self.assertEqual(r.json()["state"]["move_count"], 2)

    def test_goto_pushes_state_for_that_move(self):
        """④ ``goto`` 推一份只读 ``state``（前 n 手），``st`` 逐字段不变；越界的 n 夹到合法范围。"""
        app, client = self._client()
        with client:
            agent = app.state.agent
            for x, y in ((0, 0), (0, 18), (18, 18)):        # 玩家三手 ⇒ 共 6 手
                self.assertTrue(client.post("/api/move", json={"x": x, "y": y}).json()["ok"])
            self.assertEqual(agent.st.payload()["move_count"], 6)
            before = (copy.deepcopy(agent.st.moves), list(agent.st.move_seq),
                      copy.deepcopy(agent.st.board.grid), agent.st.board.ko_point,
                      copy.deepcopy(agent.st.bad_moves), agent.st.allow_pass,
                      agent.st.finished, copy.deepcopy(agent.st.move_notes),
                      copy.deepcopy(agent.st.losses), len(agent.st.snapshots))
            self._enter(client, True)
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "hello")
                self.assertEqual(self._goto(client, 4)["n"], 4)
                replay = self._next_state(ws)
                self.assertEqual(replay["move_count"], 4)
                self.assertEqual(replay["move_seq"], before[1][:4])
                self.assertEqual(len(replay["stones"]), 4)
                self.assertEqual(replay["bad_moves"], before[4])
                self.assertEqual(replay["allow_pass"], before[5])
                # 越界：n < 0 → 0；n > 手数 → len(move_seq)
                self.assertEqual(self._goto(client, -3)["n"], 0)
                self.assertEqual(self._next_state(ws)["move_count"], 0)
                self.assertEqual(self._goto(client, 99)["n"], 6)
                self.assertEqual(self._next_state(ws)["move_count"], 6)
            self.assertEqual(agent.st.payload()["move_count"], 6, "goto 不许改 st")
            self.assertEqual(copy.deepcopy(agent.st.moves), before[0])
            self.assertEqual(list(agent.st.move_seq), before[1])
            self.assertEqual(copy.deepcopy(agent.st.board.grid), before[2])
            self.assertEqual(agent.st.board.ko_point, before[3])
            self.assertEqual(copy.deepcopy(agent.st.bad_moves), before[4])
            self.assertEqual(agent.st.allow_pass, before[5])
            self.assertEqual(agent.st.finished, before[6])
            self.assertEqual(copy.deepcopy(agent.st.move_notes), before[7])
            self.assertEqual(copy.deepcopy(agent.st.losses), before[8])
            self.assertEqual(len(agent.st.snapshots), before[9])
            self.assertEqual(client.get("/api/state").json()["move_count"], 6)

    def test_reconnect_during_review_still_blocks(self):
        """⑤ 断线重连（``hello`` 同步）后仍是复盘模式；``hello`` 给权威盘面、重放交给 ``goto``。"""
        app, client = self._client()
        with client:
            for x, y in ((0, 0), (0, 18)):                  # 共 4 手
                self.assertTrue(client.post("/api/move", json={"x": x, "y": y}).json()["ok"])
            self._enter(client, True)
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "hello")
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertEqual(hello["state"]["move_count"], 4)
            r = client.post("/api/move", json={"x": 3, "y": 3})
            self.assertEqual(r.json(), {"ok": False, "error": "复盘模式中"})
            self.assertEqual(client.post("/api/idle", json={}).json()["error"], "复盘模式中")

    def test_review_notes_shape(self):
        """⑥ 存档端点：``ok`` + ``notes`` + ``losses`` 与 ``st`` 两份账一致；空文本的 note **不过滤**。"""
        app, client = self._client()
        with client:
            agent = app.state.agent
            self.assertTrue(client.post("/api/move", json={"x": 0, "y": 0}).json()["ok"])
            # StubEngine 的胜率前后都是 0.5 ⇒ 不会有真亏手，手工造一条（端点只做透传）
            agent.st.record_loss("Q16", 0.3, "胜率下滑30%")
            agent.st.note_move(len(agent.st.moves), "review", "A", "")   # 空文本存档
            self._enter(client, True)                       # 复盘期间照样可读（本端点不被门禁挡）
            r = client.get("/api/review/notes")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(set(body), {"ok", "notes", "losses"})
            self.assertTrue(body["ok"])
            self.assertEqual(body["notes"], agent.st.move_notes)
            self.assertEqual(body["losses"], agent.st.losses)
            self.assertEqual(set(body["notes"][0]), {"move_no", "kind", "side", "text"})
            self.assertEqual(set(body["losses"][0]), {"move_no", "gtp", "drop", "reason"})
            self.assertEqual(body["notes"][-1]["text"], "", "空文本的 note 不过滤（口径钉在这里）")

    def test_review_freezes_mood(self):
        """⑦ 静音（V14）：复盘期间心跳与互动不衰减、不发 ``mood`` 事件；``mood_payload`` 照常可读。"""
        agent, _eng, _llm, bus = mk_agent()
        with TestClient(create_app(agent=agent, autostart=False)) as client:
            client.post("/api/interaction", json={"what": "patted"})    # 复盘外：真的会动情绪
            self.assertTrue(bus.of_type("mood"), "复盘外互动应当有 mood 事件（否则本用例空转）")
            self._enter(client, True)
            before = (agent.mood.v, agent.mood.c, agent.mood.a)
            bus.clear()
            client.post("/api/mood/tick", json={})
            client.post("/api/interaction", json={"what": "patted"})
            self.assertEqual((agent.mood.v, agent.mood.c, agent.mood.a), before, "复盘期间情绪不许漂移")
            self.assertEqual(bus.of_type("mood"), [])
            payload = agent.mood_payload()                              # hello 同步要用：照常可读
            self.assertEqual(payload["name"], agent.mood.region_name())
            self.assertAlmostEqual(payload["v"], before[0], places=2)

    def test_undo_prunes_move_notes_and_losses(self):
        """⑧ V13：``undo`` 后两份账里 ``move_no > len(st.moves)`` 的尾部被剪掉（``<=`` 保留到最后保留那一手）。"""
        agent, _eng, _llm, _bus = mk_agent()

        async def play():
            await agent.player_move(0, 0)
            await agent.player_move(0, 18)

        asyncio.run(play())
        self.assertEqual(len(agent.st.moves), 4)
        agent.st.losses.append({"move_no": 2, "gtp": "Q16", "drop": 0.3, "reason": "胜率下滑30%"})
        agent.st.losses.append({"move_no": 4, "gtp": "D4", "drop": 0.4, "reason": "胜率下滑40%"})
        # 1=玩家点评（E）；2=她的用意 + 落子后感想（A）；3 / 4 同理
        self.assertEqual([n["move_no"] for n in agent.st.move_notes], [1, 2, 2, 3, 4, 4])
        agent.undo(1)                                       # 退回第 2 手之前（快照 1）
        self.assertEqual(len(agent.st.moves), 2)
        self.assertEqual([n["move_no"] for n in agent.st.move_notes], [1, 2, 2])
        self.assertEqual([l["move_no"] for l in agent.st.losses], [2], "留到 len(st.moves)（含）为止")


# —— 阶段四 · Q5（v3.9.0）追加：连上后端时的主动约战提醒 ——


class ProfileGreetWsTest(unittest.TestCase):
    """连上后端时，若「距上次对局」够久 ⇒ 紧随 ``hello`` 之后来一条主动约战提醒（只一次）。

    用 ``cfg.profile_db_path`` 指到一个临时库（唯一可控的装配入口：注入的 agent 也会被 lifespan
    覆盖成同一个 ``ProfileStore``），因此这条用例同时钉住「库路径可配」。
    """

    def _app(self, tmp, days):
        db = os.path.join(tmp, "profile.db")
        store = ProfileStore(db)
        store.add_game({"ts": time.time() - days * 86400.0, "move_count": 10, "result": "旧局"})
        cfg = AzureConfig()
        cfg.profile_db_path = db
        return create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(), autostart=False)

    def test_greeting_follows_hello_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(self._app(tmp, 3.0)) as client:
                with client.websocket_connect("/ws") as ws:
                    self.assertEqual(ws.receive_json()["type"], "hello")
                    e = ws.receive_json()
                    self.assertEqual(e["type"], "message", "紧跟 hello 的那条就是提醒")
                    # 修复轮 1 / C1：**推送方声明「这条没有语音」**——前端据此不登记语音配对记录，
                    # 否则它会占掉配对位、把连接后第一回合的三条真语音整体错位。
                    self.assertEqual(e["kind"], "ai_notice")
                    self.assertIn("好几天", e["text"])
                # 重连（同一个 agent 实例）：标记还在 ⇒ 不再有第二条提醒
                with client.websocket_connect("/ws") as ws2:
                    self.assertEqual(ws2.receive_json()["type"], "hello")
                    client.post("/api/config", json={"allow_pass": True})
                    e2 = ws2.receive_json()             # set_allow_pass 先发它自己那条 message
                    self.assertEqual(e2["type"], "message")
                    self.assertNotIn("好几天", e2["text"], "重连不再补打：队头就是动作自己那条")
                    self.assertIn("虚着", e2["text"])

    def test_no_greeting_without_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AzureConfig()
            cfg.profile_db_path = os.path.join(tmp, "profile.db")   # 空库（连文件都还没有）
            with TestClient(create_app(cfg=cfg, engine=StubEngine(), llm=StubLLM(),
                                       autostart=False)) as client:
                with client.websocket_connect("/ws") as ws:
                    self.assertEqual(ws.receive_json()["type"], "hello")
                    client.post("/api/config", json={"allow_pass": True})
                    e = ws.receive_json()
                    self.assertEqual(e["type"], "message")
                    self.assertIn("虚着", e["text"], "没有历史对局 ⇒ 一条提醒都不发")


class ChatStyleRuntimeTest(unittest.TestCase):
    """说话详略 / 思考强度都能在运行期改（v3.11.0）：一次 ``/api/config`` 立即生效、不用重启后端。"""

    def _app(self):
        cfg = AzureConfig()
        cfg.llm_extra_body = {"top_k": 40}
        cfg.llm_think_extra_body = {"reasoning_effort": "high"}
        cfg.llm_think_mode = 0
        return create_app(cfg=cfg, autostart=False)

    def test_reply_style_and_think_mode_live(self):
        app = self._app()
        with TestClient(app) as client:
            agent = app.state.agent
            llm = agent.llm
            self.assertEqual(agent.reply_style, 0)          # 默认「精简」＝旧「短回复」开
            self.assertEqual(llm.extra_body, {"top_k": 40})

            r = client.post("/api/config", json={"reply_style": 2, "think_mode": 2})
            self.assertTrue(r.json()["ok"])
            self.assertEqual(agent.reply_style, 2)
            self.assertEqual(llm.think_mode, 2)
            self.assertEqual(llm.extra_body, {"top_k": 40, "reasoning_effort": "high"})

            client.post("/api/config", json={"reply_style": 0, "think_mode": 0})
            self.assertEqual(agent.reply_style, 0)
            self.assertEqual(llm.think_mode, 0)
            self.assertEqual(llm.extra_body, {"top_k": 40})   # 思考字段必须撤掉

    def test_dirty_values_do_not_500_or_change_state(self):
        app = self._app()
        with TestClient(app) as client:
            client.post("/api/config", json={"reply_style": 2, "think_mode": 1})
            r = client.post("/api/config", json={"reply_style": "abc", "think_mode": None})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(app.state.agent.reply_style, 2)
            self.assertEqual(app.state.agent.llm.think_mode, 1)

    def test_legacy_limit_len_still_maps_to_brief(self):
        app = self._app()
        with TestClient(app) as client:
            client.post("/api/config", json={"limit_len": True})
            self.assertEqual(app.state.agent.reply_style, 0)
            client.post("/api/config", json={"limit_len": False})
            self.assertEqual(app.state.agent.reply_style, 1)


if __name__ == "__main__":
    unittest.main()
