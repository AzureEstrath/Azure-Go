"""TTS 三级链：本地 → 云端 → 系统语音（v3.11 增量）。"""
import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tts import TTSClient

WAV = b"RIFF" + b"\x00" * 40
MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 40      # 非 RIFF：多块直接字节拼接


def _cfg(**kw):
    class C:
        tts_enabled = True
        tts_mode = "openai"
        tts_base_url = "https://cloud.example.com/v1"
        tts_model = "cloud-model"
        tts_api_key = "ck"
        tts_voice = "cloud-voice"
        tts_speed = 1.0
        tts_timeout_sec = 30
        tts_long_chunk = False
        tts_local_base_url = "http://127.0.0.1:9880/v1"
        tts_local_model = ""
        tts_local_api_key = ""
        tts_local_voice = ""
        tts_local_timeout_sec = 0
    c = C()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _client(handler, **kw):
    return TTSClient(_cfg(**kw), http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class LocalFirstTest(unittest.TestCase):
    def test_local_is_tried_before_cloud(self):
        calls = []

        def handler(req):
            calls.append(req.url.host)
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(calls, ["127.0.0.1"])          # 云端一次都没碰

    def test_local_voice_and_model_override(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, content=WAV)

        c = _client(handler, tts_local_voice="xiaoya", tts_local_model="cosyvoice3")
        asyncio.run(c.speak("你好"))
        self.assertEqual(seen[0]["voice"], "xiaoya")
        self.assertEqual(seen[0]["model"], "cosyvoice3")

    def test_local_inherits_cloud_fields_when_empty(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        asyncio.run(c.speak("你好"))
        self.assertEqual(seen[0]["voice"], "cloud-voice")
        self.assertEqual(seen[0]["model"], "cloud-model")

    def test_enabled_with_only_local_url(self):
        c = TTSClient(_cfg(tts_base_url="", tts_local_base_url="http://127.0.0.1:9880/v1"))
        self.assertTrue(c.enabled)


class LocalModeTest(unittest.TestCase):
    """``tts_mode="local"``：只走本地端点，失败回落系统语音（**绝不偷偷走云端**）。"""

    def test_local_mode_never_touches_cloud(self):
        calls = []

        def handler(req):
            calls.append(req.url.host)
            return httpx.Response(200, content=WAV)

        c = _client(handler, tts_mode="local")
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(calls, ["127.0.0.1"])

    def test_local_mode_failure_degrades_to_system_without_cloud(self):
        calls = []

        def handler(req):
            calls.append(req.url.host)
            return httpx.Response(500)

        c = _client(handler, tts_mode="local")
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(calls, ["127.0.0.1"], "选了本地就不该产生云端调用")

    def test_local_mode_without_address_is_enabled_and_system(self):
        c = TTSClient(_cfg(tts_mode="local", tts_local_base_url=""))
        self.assertTrue(c.enabled, "本地地址为空也要有系统语音兜底，不能整条静音")
        self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "system")


class FallbackTest(unittest.TestCase):
    def test_local_failure_falls_back_to_cloud(self):
        calls = []

        def handler(req):
            calls.append(req.url.host)
            if req.url.host == "127.0.0.1":
                return httpx.Response(500, text="local down")
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(calls, ["127.0.0.1", "cloud.example.com"])

    def test_both_failing_degrades_to_system(self):
        c = _client(lambda req: httpx.Response(503))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")

    def test_no_local_uses_cloud_only(self):
        calls = []

        def handler(req):
            calls.append(req.url.host)
            return httpx.Response(200, content=WAV)

        c = _client(handler, tts_local_base_url="")
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(calls, ["cloud.example.com"])

    def test_local_only_failure_degrades_to_system(self):
        c = _client(lambda req: httpx.Response(500), tts_base_url="")
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")

    def test_local_http_error_does_not_mix_with_cloud(self):
        # 本地第一块成功、第二块失败 ⇒ 整条改用云端，不把两路音频拼起来
        calls = []

        def handler(req):
            host = req.url.host
            calls.append(host)
            if host == "127.0.0.1":
                # 第一个分块成功、第二个分块 500（用输入长度区分不了块，这里按调用次数）
                return httpx.Response(200, content=MP3) if len(calls) == 1 else httpx.Response(500)
            return httpx.Response(200, content=MP3)

        long_text = "这手棋下得不错。" * 20          # 会被切成多块
        c = _client(handler, tts_long_chunk=False)
        out = asyncio.run(c.speak(long_text))
        self.assertEqual(out["mode"], "openai")
        self.assertIn("cloud.example.com", calls)


if __name__ == "__main__":
    unittest.main()
