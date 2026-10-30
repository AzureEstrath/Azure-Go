"""去本地化后的配置面与 LLM 客户端形状（对应决策 D3）。"""

import importlib.util
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AzureConfig, DRIFT_MAX_LIMIT
from app.llm import LLMClient

_REMOVED_CONFIG_FIELDS = ["llm_auto_start", "llm_port", "llm_exe", "llm_model_path"]


class DelocalizedConfigTest(unittest.TestCase):
    def test_local_service_fields_removed(self):
        c = AzureConfig()
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), "配置项应已删除：%s" % name)

    def test_legacy_config_keys_are_ignored(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"llm_exe": "D:/x/llama-server.exe", "llm_port": 8080,
                           "llm_base_url": "https://api.example.com/v1"}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.llm_base_url, "https://api.example.com/v1")
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), name)


class DelocalizedLLMTest(unittest.TestCase):
    def test_no_service_watch(self):
        self.assertNotIn("relaunch_cb", inspect.signature(LLMClient.__init__).parameters)
        self.assertFalse(hasattr(LLMClient(), "watch_loop"))

    def test_services_module_removed(self):
        self.assertIsNone(importlib.util.find_spec("app.services"))


class LLMTimeoutConfigTest(unittest.TestCase):
    """v4.2.8：单次 LLM 请求硬超时，脏值 / 越界回退，不影响启动。"""

    def test_default_and_round_trip(self):
        self.assertEqual(AzureConfig().llm_timeout_sec, 45.0)
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"llm_timeout_sec": 18.5}, f)
            self.assertEqual(AzureConfig.load(p).llm_timeout_sec, 18.5)

    def test_dirty_and_out_of_range(self):
        import tempfile, json
        cases = [("abc", 45.0), (1, 5.0), (999, 300.0)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            for raw, want in cases:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"llm_timeout_sec": raw}, f)
                self.assertEqual(AzureConfig.load(p).llm_timeout_sec, want, raw)

    def test_pet_chat_think_level_default_and_clamp(self):
        self.assertEqual(AzureConfig().pet_chat_think_level, 1)
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            for raw, want in [(3, 3), (9, 3), ("abc", 1)]:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"pet_chat_think_level": raw}, f)
                self.assertEqual(AzureConfig.load(p).pet_chat_think_level, want, raw)


class CloudKatagoConfigTest(unittest.TestCase):
    def test_katago_fields_replaced(self):
        c = AzureConfig()
        for gone in ("katago_bin", "katago_model", "katago_config"):
            self.assertFalse(hasattr(c, gone), gone)
        for now in ("katago_url", "katago_token", "katago_timeout_ms"):
            self.assertTrue(hasattr(c, now), now)

    def test_has_katago_follows_url(self):
        c = AzureConfig()
        c.katago_url = "http://127.0.0.1:8100"
        self.assertTrue(c.has_katago())
        c.katago_url = ""
        self.assertFalse(c.has_katago())

    def test_new_katago_keys_round_trip_through_load(self):
        """新三键必须真的被 load()/_apply() 读进来（防「字段加了但没接线」的回归）。"""
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"katago_url": "http://kg.example:9100", "katago_token": "tok",
                           "katago_timeout_ms": 5000}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.katago_url, "http://kg.example:9100")
        self.assertEqual(c.katago_token, "tok")
        self.assertEqual(c.katago_timeout_ms, 5000)
        self.assertTrue(c.has_katago())


class MemoryConfigTest(unittest.TestCase):
    """长期记忆配置面：embedding 超时可收紧（默认 10 秒）。"""

    def test_embed_timeout_default_and_round_trip(self):
        self.assertEqual(AzureConfig().embed_timeout, 10.0)
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"embed_timeout": 2.5}, f)
            self.assertEqual(AzureConfig.load(p).embed_timeout, 2.5)

    def test_embed_timeout_bad_value_falls_back(self):
        """脏值退回默认：不因一个旋钮把后端启动搞崩。"""
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"embed_timeout": "abc"}, f)
            self.assertEqual(AzureConfig.load(p).embed_timeout, 10.0)

    def test_embed_local_round_trip_and_clamp(self):
        """v4.3.1：本地 GGUF 路径 / 模式 / 线程 / 批大小都要能读回并按边界夹取。"""
        import tempfile, json
        cfg = AzureConfig()
        self.assertEqual(cfg.embed_mode, "auto")
        self.assertEqual(cfg.embed_local_gpu_layers, 0)
        self.assertEqual(cfg.embed_local_threads, 8)
        self.assertEqual(cfg.embed_batch_size, 16)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({
                    "embed_mode": "LOCAL",
                    "embed_local_model": "D:\\models\\emb.gguf",
                    "embed_local_gpu_layers": "all",
                    "embed_local_threads": 999,
                    "embed_local_ctx": 1,
                    "embed_batch_size": 0,
                    "embed_cache_size": -5,
                    "embed_backfill": False,
                }, f)
            got = AzureConfig.load(p)
            self.assertEqual(got.embed_mode, "local")
            self.assertEqual(got.embed_local_model, "D:\\models\\emb.gguf")
            self.assertEqual(got.embed_local_gpu_layers, 0, "脏值退回默认 0")
            self.assertEqual(got.embed_local_threads, 64)
            self.assertEqual(got.embed_local_ctx, 64)
            self.assertEqual(got.embed_batch_size, 1)
            self.assertEqual(got.embed_cache_size, 0)
            self.assertFalse(got.embed_backfill)


class PvDepthConfigTest(unittest.TestCase):
    """阶段三（v3.8.0）：``pv_max_visits`` 字段面 + ``katago_timeout_ms`` 的新代码默认。"""

    def test_defaults(self):
        c = AzureConfig()
        self.assertEqual(c.pv_max_visits, 150)
        self.assertEqual(c.katago_timeout_ms, 45000)

    def test_pv_max_visits_round_trip_and_dirty_fallback(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pv_max_visits": 220}, f)
            self.assertEqual(AzureConfig.load(p).pv_max_visits, 220)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pv_max_visits": "abc"}, f)
            self.assertEqual(AzureConfig.load(p).pv_max_visits, 150, "脏值退回默认")


class EmotionDriftConfigTest(unittest.TestCase):
    """情绪波动（v3.8.0）：开关默认关，三个数值默认 / 往返 / 脏值口径（与 joseki_rate 同一套守卫）。"""
    def test_defaults(self):
        c = AzureConfig()
        self.assertFalse(c.emotion_drift, "情绪波动必须默认关（关掉＝现状）")
        self.assertAlmostEqual(c.drift_base, 0.03)
        self.assertAlmostEqual(c.drift_max, 0.08)
        self.assertAlmostEqual(c.pv_max_drop, 0.05)

    def test_round_trip(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"emotion_drift": True, "drift_base": 0.05,
                           "drift_max": 0.09, "pv_max_drop": 0.02}, f)
            c = AzureConfig.load(p)
            self.assertTrue(c.emotion_drift)
            self.assertAlmostEqual(c.drift_base, 0.05)
            self.assertAlmostEqual(c.drift_max, 0.09)
            self.assertAlmostEqual(c.pv_max_drop, 0.02)

    def test_dirty_and_out_of_range_fall_back(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"emotion_drift": False, "drift_base": "abc",
                           "drift_max": 5, "pv_max_drop": -1}, f)
            c = AzureConfig.load(p)
            self.assertFalse(c.emotion_drift)
            self.assertAlmostEqual(c.drift_base, 0.03, msg="脏值退回默认")
            self.assertAlmostEqual(c.drift_max, DRIFT_MAX_LIMIT,
                                   msg="drift_max 夹到「小概率」上限 0.2（不是 1.0）")
            self.assertAlmostEqual(c.pv_max_drop, 0.0)

    def test_drift_max_limit_and_base_never_exceeds_max(self):
        """修复轮 1 / Minor #3：``drift_max ≤ DRIFT_MAX_LIMIT``（0.2）、``drift_base ≤ drift_max``。

        ``p = clamp(base + bias, 0, max)``，而阶梯是 ``P(A)=1-p``、``P(B)=2p/3``：``max`` 开到 1.0 时
        ``p = 1.0`` ⇒ 她**恒偏航**，``p ≥ 0.6`` ⇒ ``P(A) ≤ P(B)``——正是 spec §5-C 要防的「乱下」。
        """
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"emotion_drift": True, "drift_max": 1.0, "drift_base": 0.9}, f)
            c = AzureConfig.load(p)
            self.assertAlmostEqual(c.drift_max, DRIFT_MAX_LIMIT)
            self.assertAlmostEqual(c.drift_base, DRIFT_MAX_LIMIT, msg="base 不许超过 max")
            self.assertGreater(1.0 - c.drift_max, c.drift_max * 2.0 / 3.0,
                               "不变量：P(A) = 1-p 必须大于 P(B) = 2p/3")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"drift_max": 0.05, "drift_base": 0.9}, f)
            c = AzureConfig.load(p)
            self.assertAlmostEqual(c.drift_max, 0.05)
            self.assertAlmostEqual(c.drift_base, 0.05, msg="base 跟着 max 一起收")

    def test_switch_only_accepts_json_booleans(self):
        """修复轮 1 / Minor #7：``emotion_drift`` **只认 true / false**；字符串 / 数字 / null 是脏值 → 默认关。"""
        import tempfile, json
        for bad in ("false", "true", 1, 0, None, []):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "config.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"emotion_drift": bad}, f)
                c = AzureConfig.load(p)
                self.assertFalse(c.emotion_drift, "脏值 %r 必须退回默认 false" % (bad,))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"emotion_drift": True}, f)
            self.assertTrue(AzureConfig.load(p).emotion_drift)



class ProfileTimeConfigTest(unittest.TestCase):
    """阶段四（v3.9.0）：``profile_enabled`` / ``time_aware_enabled`` 两个开关。

    口径与 ``emotion_drift`` / ``chat_digest_llm`` **同形**：默认关、只认 JSON 的 true / false，
    字符串 / 数字 / null 是脏值（保留默认关）。
    """

    def test_defaults_are_off(self):
        c = AzureConfig()
        self.assertFalse(c.profile_enabled, "画像必须默认关（关掉＝现状）")
        self.assertFalse(c.time_aware_enabled, "时间感知必须默认关（关掉＝现状）")

    def test_round_trip(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"profile_enabled": True, "time_aware_enabled": True}, f)
            c = AzureConfig.load(p)
        self.assertTrue(c.profile_enabled)
        self.assertTrue(c.time_aware_enabled)

    def test_switch_only_accepts_json_booleans(self):
        import tempfile, json
        for bad in ("false", "true", 1, 0, None, []):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "config.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"profile_enabled": bad, "time_aware_enabled": bad}, f)
                c = AzureConfig.load(p)
            self.assertFalse(c.profile_enabled, "脏值 %r 必须退回默认 false" % (bad,))
            self.assertFalse(c.time_aware_enabled, "脏值 %r 必须退回默认 false" % (bad,))


class PersonaConfigTest(unittest.TestCase):
    """人格化：总开关默认开；关系阶段与称呼默认关，称呼名只认字符串、最多 16 字。"""

    def test_defaults(self):
        c = AzureConfig()
        self.assertTrue(c.persona_enabled)
        self.assertFalse(c.persona_relationship_enabled, "关系称呼必须默认关（称呼保持原名）")
        self.assertEqual(c.persona_address_name, "")

    def test_round_trip(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"persona_enabled": False, "persona_relationship_enabled": True,
                           "persona_address_name": "  搭档  "}, f)
            c = AzureConfig.load(p)
        self.assertFalse(c.persona_enabled)
        self.assertTrue(c.persona_relationship_enabled)
        self.assertEqual(c.persona_address_name, "搭档")

    def test_dirty_values_fall_back(self):
        import tempfile, json
        for bad in ("true", 1, None, []):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "config.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"persona_relationship_enabled": bad}, f)
                c = AzureConfig.load(p)
            self.assertFalse(c.persona_relationship_enabled, "脏值 %r 必须退回默认 false" % (bad,))
        for bad in (1, None, [], {}):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "config.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"persona_address_name": bad}, f)
                c = AzureConfig.load(p)
            self.assertEqual(c.persona_address_name, "", "脏值 %r 必须退回空串" % (bad,))

    def test_address_name_is_truncated(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"persona_address_name": "12345678901234567890"}, f)
            c = AzureConfig.load(p)
        self.assertEqual(len(c.persona_address_name), 16)


class AliyunTtsConfigTest(unittest.TestCase):
    """阿里云 TTS 配置面：新键必须真的被 ``load()`` 读进来（防「字段加了但没接线」）。"""

    def test_defaults(self):
        c = AzureConfig()
        self.assertEqual(c.tts_aliyun_format, "pcm")     # CosyVoice3 系引擎对 mp3 支持因音色而异
        self.assertEqual(c.tts_aliyun_sample_rate, 24000)  # 22050 会被 400 拒
        self.assertEqual(c.tts_aliyun_volume, 50)
        self.assertTrue(c.tts_aliyun_tn)
        self.assertEqual(c.tts_aliyun_ws_url, "")      # 留空＝tts_aliyun.WS_URL

    def test_round_trip_through_load(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"tts_enabled": True, "tts_mode": "aliyun",
                           "tts_aliyun_ws_url": "wss://example.test/api-ws/v1/inference",
                           "tts_aliyun_api_key": "sk-test",
                           "tts_aliyun_voice": "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37",
                           "tts_aliyun_format": "wav",
                           "tts_aliyun_sample_rate": 16000,
                           "tts_aliyun_volume": 80,
                           "tts_aliyun_tn": False}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.tts_mode, "aliyun")
        self.assertEqual(c.tts_aliyun_api_key, "sk-test")
        self.assertEqual(c.tts_aliyun_voice, "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37")
        self.assertEqual(c.tts_aliyun_format, "wav")
        self.assertEqual(c.tts_aliyun_sample_rate, 16000)
        self.assertEqual(c.tts_aliyun_volume, 80)
        self.assertFalse(c.tts_aliyun_tn)

    def test_bad_values_fall_back(self):
        """脏值退回默认：不因一个旋钮把后端启动搞崩。"""
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"tts_aliyun_sample_rate": "abc", "tts_aliyun_volume": None}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.tts_aliyun_sample_rate, 24000)
        self.assertEqual(c.tts_aliyun_volume, 50)


class ReplyStyleConfigTest(unittest.TestCase):
    """说话详略三档（v3.11.0）：新键优先、旧键迁就、脏值不炸。"""

    def _load(self, data):
        import tempfile, json, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return AzureConfig.load(p)

    def test_default_is_brief(self):
        """默认 0（精简）：与旧「短回复」开关的默认 true 一致，不静默改变既有行为。"""
        self.assertEqual(AzureConfig().reply_style, 0)

    def test_new_key_round_trip_and_clamp(self):
        self.assertEqual(self._load({"reply_style": 1}).reply_style, 1)
        self.assertEqual(self._load({"reply_style": 2}).reply_style, 2)
        self.assertEqual(self._load({"reply_style": 9}).reply_style, 2)
        self.assertEqual(self._load({"reply_style": -3}).reply_style, 0)

    def test_dirty_falls_back_to_brief(self):
        self.assertEqual(self._load({"reply_style": "oops"}).reply_style, 0)
        self.assertEqual(self._load({"reply_style": None}).reply_style, 0)

    def test_missing_keys_match_legacy_default(self):
        self.assertEqual(self._load({}).reply_style, 0)   # 旧默认 tts_limit_len=true

    def test_legacy_tts_limit_len_migrates(self):
        self.assertEqual(self._load({"tts_limit_len": True}).reply_style, 0)
        self.assertEqual(self._load({"tts_limit_len": False}).reply_style, 1)

    def test_new_key_wins_over_legacy(self):
        self.assertEqual(self._load({"reply_style": 2, "tts_limit_len": True}).reply_style, 2)


class MainTtsRoutingTest(unittest.TestCase):
    """``tts_mode`` 决定装配哪个客户端（两者共享同一套 speak/audio 契约）。"""

    def test_build_tts_routes_by_mode(self):
        from app.main import _build_tts
        from app.tts import TTSClient
        from app.tts_aliyun import AliyunTTSClient

        c = AzureConfig()
        c.tts_mode = "aliyun"
        self.assertIsInstance(_build_tts(c), AliyunTTSClient)
        c.tts_mode = "openai"
        self.assertIsInstance(_build_tts(c), TTSClient)
        self.assertNotIsInstance(_build_tts(c), AliyunTTSClient)
        c.tts_mode = "system"
        self.assertIsInstance(_build_tts(c), TTSClient)


class EmotionStyleConfigTest(unittest.TestCase):
    """情绪棋风（v3.13.0）：默认关；只认 JSON 的 true / false。"""

    def test_default_off_and_round_trip(self):
        import tempfile, json
        self.assertFalse(AzureConfig().emotion_style)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"emotion_style": True}, f)
            self.assertTrue(AzureConfig.load(p).emotion_style)


if __name__ == "__main__":
    unittest.main()
