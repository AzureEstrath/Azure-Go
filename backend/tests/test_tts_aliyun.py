"""阿里云 Voice Studio（WebSocket）TTS 适配器：协议帧、复用、降级。

全部用本机假 WS 服务（``websockets.serve``），不触公网、不需要真实 Key。
"""

import asyncio
import json
import os
import sys
import unittest

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tts_aliyun import (CONTINUE_MAX_CHARS, AliyunTTSClient, _split_text,
                            is_aliyun_mode)

MP3_HEAD = b"\xff\xfb\x90\x00"


def _cfg(**kw):
    class C:
        tts_enabled = True
        tts_mode = "aliyun"
        tts_aliyun_ws_url = ""            # 用例里替换成假服务地址
        tts_aliyun_api_key = "sk-test"
        tts_aliyun_voice = "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37"
        tts_aliyun_format = "mp3"
        tts_aliyun_sample_rate = 22050
        tts_aliyun_volume = 50
        tts_aliyun_tn = True
        tts_voice = "中文女"
        tts_speed = 1.0
        tts_timeout_sec = 5
        tts_long_chunk = False
    c = C()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, "ws://127.0.0.1:%d" % port


def _run(handler, body):
    """起一个假服务跑完 ``body(url)``，收尾时关掉服务。"""
    async def main():
        server, url = await _serve(handler)
        try:
            return await body(url)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def _frame(raw):
    return json.loads(raw)


def _started(task_id):
    return json.dumps({"header": {"event": "task-started", "task_id": task_id,
                                  "attributes": {}}, "payload": {}})


def _finished(task_id):
    return json.dumps({"header": {"event": "task-finished", "task_id": task_id,
                                  "attributes": {"request_uuid": "r1"}},
                       "payload": {"usage": {"characters": 2}}})


def _failed(task_id, code="InvalidParameter", message="bad voice"):
    return json.dumps({"header": {"event": "task-failed", "task_id": task_id,
                                  "error_code": code, "error_message": message},
                       "payload": {"error": {"code": code, "message": message,
                                             "type": "client_error"}}})


async def _ok_handler(ws, *args):
    """最小可用服务端：run-task → task-started；continue → 元数据+音频；finish → 尾音+task-finished。"""
    async for raw in ws:
        if isinstance(raw, (bytes, bytearray)):
            continue
        frame = _frame(raw)
        task_id = frame["header"]["task_id"]
        action = frame["header"]["action"]
        if action == "run-task":
            await ws.send(_started(task_id))
        elif action == "continue-task":
            await ws.send(json.dumps({
                "header": {"event": "result-generated", "task_id": task_id},
                "payload": {"output": {"type": "sentence-synthesis", "sentence": {"index": 0}}},
            }))
            await ws.send(MP3_HEAD + b"BODY")
        elif action == "finish-task":
            await ws.send(MP3_HEAD + b"TAIL")
            await ws.send(_finished(task_id))


class FrameShapeTest(unittest.TestCase):
    """上行帧必须严格符合文档信封（字段名 / 取值 / task_id 一致性）。"""

    def test_happy_path_frames_and_audio(self):
        seen = []

        async def handler(ws, *args):
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                seen.append(_frame(raw))
                task_id = seen[-1]["header"]["task_id"]
                action = seen[-1]["header"]["action"]
                if action == "run-task":
                    await ws.send(_started(task_id))
                elif action == "continue-task":
                    await ws.send(MP3_HEAD + b"BODY")
                elif action == "finish-task":
                    await ws.send(MP3_HEAD + b"TAIL")
                    await ws.send(_finished(task_id))

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好，Azure。"), c
            finally:
                await c.close()

        out, client = _run(handler, body)
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(client.audio(out["id"]), MP3_HEAD + b"BODY" + MP3_HEAD + b"TAIL")
        self.assertEqual([f["header"]["action"] for f in seen],
                         ["run-task", "continue-task", "finish-task"])
        for f in seen:                      # 同一任务全部帧共用一个 task_id
            self.assertEqual(f["header"]["task_id"], seen[0]["header"]["task_id"])
            self.assertEqual(f["header"]["streaming"], "duplex")
        run = seen[0]["payload"]
        self.assertEqual(run["input"], {})              # 文本一律走 continue-task
        self.assertEqual(run["parameters"]["voice"], "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37")
        self.assertEqual(run["parameters"]["format"], "mp3")
        self.assertEqual(run["parameters"]["sample_rate"], 22050)
        self.assertEqual(run["parameters"]["rate"], 1.0)
        self.assertEqual(seen[1]["payload"]["input"]["text"], "你好，Azure。")
        self.assertEqual(seen[2]["payload"]["input"], {})   # finish-task 固定空对象

    def test_auth_header_and_tn(self):
        captured = {}

        async def handler(ws, *args):
            captured.update({k.lower(): v for k, v in dict(ws.request.headers).items()})
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                action = _frame(raw)["header"]["action"]
                if action == "run-task":
                    await ws.send(_started(task_id))
                elif action == "finish-task":
                    await ws.send(MP3_HEAD + b"A")
                    await ws.send(_finished(task_id))

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(captured.get("authorization"), "Bearer sk-test")
        self.assertEqual(captured.get("x-tts-tn"), "enable")

    def test_tn_disabled_omits_header(self):
        captured = {}

        async def handler(ws, *args):
            captured.update({k.lower(): v for k, v in dict(ws.request.headers).items()})
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                action = _frame(raw)["header"]["action"]
                if action == "run-task":
                    await ws.send(_started(task_id))
                elif action == "finish-task":
                    await ws.send(MP3_HEAD + b"A")
                    await ws.send(_finished(task_id))

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url, tts_aliyun_tn=False))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["mode"], "openai")
        self.assertNotIn("x-tts-tn", captured)


class ReuseTest(unittest.TestCase):
    def test_second_speak_reuses_connection(self):
        connects = []

        async def handler(ws, *args):
            connects.append(1)
            await _ok_handler(ws)

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                first = await c.speak("第一句")
                second = await c.speak("第二句")
                return first, second
            finally:
                await c.close()

        first, second = _run(handler, body)
        self.assertEqual(first["mode"], "openai")
        self.assertEqual(second["mode"], "openai")
        self.assertEqual(len(connects), 1)   # 协议允许串行复用，就别每条台词都握手
        self.assertNotEqual(first["id"], second["id"])

    def test_dead_connection_is_rebuilt_once(self):
        """空闲/被回收的连接：第一次 send 抛错 → 丢弃重连 → 第二次成功。"""
        connects = []

        async def handler(ws, *args):
            connects.append(1)
            if len(connects) == 1:
                return                       # 第一条连接立刻断：模拟服务端 4408 回收
            await _ok_handler(ws)

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(len(connects), 2)


class DegradeTest(unittest.TestCase):
    """任何失败都必须落成 ``mode="system"``（文字照常），绝不抛异常。"""

    def test_task_failed_degrades(self):
        async def handler(ws, *args):
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                await ws.send(_failed(task_id))
                return

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "你好")
        self.assertEqual(out["id"], "")

    def test_empty_audio_degrades(self):
        async def handler(ws, *args):
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                if _frame(raw)["header"]["action"] == "run-task":
                    await ws.send(_started(task_id))
                else:
                    await ws.send(_finished(task_id))   # 一条音频帧都没有

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        self.assertEqual(_run(handler, body)["mode"], "system")

    def test_connection_refused_degrades(self):
        c = AliyunTTSClient(_cfg(tts_aliyun_ws_url="ws://127.0.0.1:9", tts_timeout_sec=2))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "你好")

    def test_missing_key_degrades_without_connecting(self):
        # 地址故意是打不通的：真的去连会慢，这里要求它在连之前就降级
        c = AliyunTTSClient(_cfg(tts_aliyun_ws_url="ws://127.0.0.1:9",
                                 tts_aliyun_api_key=""))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "你好")

    def test_missing_voice_degrades(self):
        c = AliyunTTSClient(_cfg(tts_aliyun_ws_url="ws://127.0.0.1:9",
                                 tts_aliyun_voice="", tts_voice=""))
        self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "system")

    def test_disabled_returns_none(self):
        c = AliyunTTSClient(_cfg(tts_aliyun_ws_url="ws://127.0.0.1:9", tts_enabled=False))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "none")
        self.assertEqual(out["text"], "")

    def test_enabled_does_not_require_base_url(self):
        """与父类不同：本模式不看 tts_base_url（否则开关一开就没声音）。"""
        c = AliyunTTSClient(_cfg(tts_base_url=""))
        self.assertTrue(c.enabled)


class EdgeTest(unittest.TestCase):
    def test_tone_and_clean_are_inherited(self):
        async def handler(ws, *args):
            await _ok_handler(ws)

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("[[tone:sigh]]算了。")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["tone"], "sigh")
        self.assertEqual(out["mode"], "openai")

    def test_parameters_are_clamped(self):
        c = AliyunTTSClient(_cfg(tts_aliyun_format="FLAC", tts_aliyun_sample_rate=99999,
                                 tts_aliyun_volume=999, tts_speed=9.0))
        p = c._parameters()
        self.assertEqual(p["format"], "pcm")        # 不认识的值回落默认（pcm）
        self.assertEqual(p["sample_rate"], 48000)   # 钳到协议上限
        self.assertEqual(p["volume"], 100)
        self.assertEqual(p["rate"], 2.0)
        self.assertEqual(c._parameters()["voice"], "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37")

    def test_default_format_is_pcm(self):
        """留空 / 脏值都落到 pcm：CosyVoice3 系引擎对 mp3 的支持因音色而异，pcm 最稳。"""
        self.assertEqual(AliyunTTSClient(_cfg(tts_aliyun_format=""))._format(), "pcm")
        self.assertEqual(AliyunTTSClient(_cfg(tts_aliyun_format="MP4"))._format(), "pcm")

    def test_pcm_to_wav_header_is_self_consistent(self):
        from app.tts_aliyun import _pcm_to_wav

        pcm = b"\x01\x02" * 16                     # 32 字节
        wav = _pcm_to_wav(pcm, 24000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(wav[12:16], b"fmt ")
        self.assertEqual(int.from_bytes(wav[24:28], "little"), 24000)   # 采样率
        self.assertEqual(int.from_bytes(wav[34:36], "little"), 16)      # 位深
        self.assertEqual(int.from_bytes(wav[22:24], "little"), 1)       # 单声道
        self.assertEqual(int.from_bytes(wav[40:44], "little"), 32)      # data 长度
        self.assertEqual(wav[44:], pcm)
        self.assertEqual(len(wav), 44 + 32)
        self.assertEqual(_pcm_to_wav(b"", 24000), b"")

    def test_pcm_frames_are_wrapped_into_wav(self):
        """format=pcm 时，服务端给的是裸 PCM → 后端必须补成可播放的 WAV。"""
        pcm = b"\x10\x20" * 8

        async def handler(ws, *args):
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                action = _frame(raw)["header"]["action"]
                if action == "run-task":
                    await ws.send(_started(task_id))
                elif action == "continue-task":
                    await ws.send(pcm)
                elif action == "finish-task":
                    await ws.send(pcm)
                    await ws.send(_finished(task_id))

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url, tts_aliyun_format="pcm",
                                     tts_aliyun_sample_rate=24000))
            try:
                return await c.speak("你好"), c
            finally:
                await c.close()

        out, client = _run(handler, body)
        self.assertEqual(out["mode"], "openai")
        blob = client.audio(out["id"])
        self.assertEqual(blob[:4], b"RIFF")
        self.assertEqual(int.from_bytes(blob[24:28], "little"), 24000)
        self.assertEqual(int.from_bytes(blob[40:44], "little"), 32)     # 两帧各 16 字节
        self.assertEqual(blob[44:], pcm + pcm)

    def test_voice_falls_back_to_tts_voice(self):
        c = AliyunTTSClient(_cfg(tts_aliyun_voice="", tts_voice="longxiaochun"))
        self.assertEqual(c._parameters()["voice"], "longxiaochun")

    def test_split_text_respects_limit(self):
        text = "字" * (CONTINUE_MAX_CHARS + 5)
        parts = _split_text(text)
        self.assertEqual(len(parts), 2)
        self.assertEqual("".join(parts), text)
        self.assertEqual(len(parts[0]), CONTINUE_MAX_CHARS)

    def test_is_aliyun_mode(self):
        self.assertTrue(is_aliyun_mode(_cfg(tts_mode=" Aliyun ")))
        self.assertFalse(is_aliyun_mode(_cfg(tts_mode="openai")))
        self.assertFalse(is_aliyun_mode(_cfg(tts_mode="system")))

    def test_unknown_event_is_ignored(self):
        """服务端新增事件不应打断收流（前向兼容）。"""
        async def handler(ws, *args):
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                task_id = _frame(raw)["header"]["task_id"]
                action = _frame(raw)["header"]["action"]
                if action == "run-task":
                    await ws.send(_started(task_id))
                elif action == "finish-task":
                    await ws.send(json.dumps({"header": {"event": "brand-new-event",
                                                         "task_id": task_id}, "payload": {}}))
                    await ws.send(MP3_HEAD + b"X")
                    await ws.send(_finished(task_id))

        async def body(url):
            c = AliyunTTSClient(_cfg(tts_aliyun_ws_url=url))
            try:
                return await c.speak("你好")
            finally:
                await c.close()

        out = _run(handler, body)
        self.assertEqual(out["mode"], "openai")


class WiringTest(unittest.TestCase):
    """装配层：``tts_mode=aliyun`` 时应用里挂的必须是适配器，且端点仍按老契约工作。"""

    def test_app_installs_aliyun_client(self):
        from fastapi.testclient import TestClient
        from app.config import AzureConfig
        from app.main import create_app

        cfg = AzureConfig()
        cfg.tts_enabled = True
        cfg.tts_mode = "aliyun"
        cfg.tts_aliyun_api_key = "sk-test"
        cfg.tts_aliyun_voice = "9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37"
        app = create_app(cfg=cfg, autostart=False)
        with TestClient(app) as client:
            self.assertIsInstance(app.state.tts, AliyunTTSClient)
            self.assertIs(app.state.agent.tts, app.state.tts)   # 同一实例挂到 agent（speech 事件用它）
            # 未命中就是 404，绝不 500（取音频端点的既有契约）
            self.assertEqual(client.get("/api/tts/audio/nope").status_code, 404)

    def test_audio_media_type_sniffing(self):
        from app.main import _audio_media_type

        self.assertEqual(_audio_media_type(b"RIFF\x00\x00\x00\x00WAVEfmt "), "audio/wav")
        self.assertEqual(_audio_media_type(b"ID3\x04\x00\x00"), "audio/mpeg")
        self.assertEqual(_audio_media_type(b"\xff\xfb\x90\x00"), "audio/mpeg")
        self.assertEqual(_audio_media_type(b"OggS\x00\x02"), "audio/ogg")
        self.assertEqual(_audio_media_type(b""), "audio/wav")


if __name__ == "__main__":
    unittest.main()
