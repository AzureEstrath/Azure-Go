"""云端 TTS 客户端：请求体形状、降级、缓存上限。不触网。"""

import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tts import TTS_CACHE_MAX, TTSClient


def _cfg(**kw):
    class C:
        tts_enabled = True
        tts_mode = "openai"
        tts_base_url = "https://tts.example.com/v1"
        tts_model = "tts-1"
        tts_api_key = "k"
        tts_voice = "alloy"
        tts_speed = 1.0
        tts_volume = 1.0
        tts_timeout_sec = 30
        tts_long_chunk = False
    c = C()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


WAV = b"RIFF" + b"\x00" * 40


def _client(handler, **kw):
    return TTSClient(_cfg(**kw), http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class SpeakTest(unittest.TestCase):
    def test_body_shape(self):
        seen = []

        def handler(req):
            seen.append((str(req.url), json.loads(req.content.decode("utf-8"))))
            return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})

        c = _client(handler)
        out = asyncio.run(c.speak("你好，Azure。"))
        self.assertEqual(out["mode"], "openai")
        url, body = seen[0]
        self.assertTrue(url.endswith("/audio/speech"))
        self.assertEqual(body["model"], "tts-1")
        self.assertEqual(body["voice"], "alloy")
        self.assertEqual(body["input"], "你好，Azure。")
        self.assertEqual(body["speed"], 1.0)
        self.assertEqual(body["response_format"], "wav")

    def test_audio_roundtrip_and_cache(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(c.audio(out["id"]), WAV)
        self.assertIsNone(c.audio("不存在"))

    def test_tone_extracted_before_synthesis(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        out = asyncio.run(c.speak("[[tone:sigh]]算了。"))
        self.assertEqual(out["tone"], "sigh")
        self.assertNotIn("tone:", seen[0]["input"])

    def test_http_error_degrades_to_system(self):
        c = _client(lambda req: httpx.Response(401, json={"error": "bad key"}))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "你好")
        self.assertEqual(out["id"], "")

    def test_timeout_degrades_to_system(self):
        def handler(req):
            raise httpx.ReadTimeout("slow", request=req)

        c = _client(handler)
        self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "system")

    def test_disabled_makes_no_request(self):
        called = []
        c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV), tts_enabled=False)
        self.assertFalse(c.enabled)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "none")
        self.assertEqual(called, [])

    def test_cache_evicts_oldest(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))

        async def run():
            ids = []
            for i in range(TTS_CACHE_MAX + 3):
                ids.append((await c.speak("第%d条" % i))["id"])
            return ids

        ids = asyncio.run(run())
        self.assertIsNone(c.audio(ids[0]))          # 最旧的已被淘汰
        self.assertIsNotNone(c.audio(ids[-1]))


if __name__ == "__main__":
    unittest.main()


# ============================================================================
# 以下为本任务实施者追加的用例（计划原文的用例一行未改，全部保留在上面）。
# 覆盖：T1 分块拼接三分支、T9 空块跳过、T10 to_thread、T11 enabled 真值表、
#       字节上限淘汰、close()、配置接线与脏值兜底。
# ============================================================================

import struct                                                                 # noqa: E402
import tempfile                                                               # noqa: E402
import threading                                                              # noqa: E402
import time                                                                   # noqa: E402
from unittest import mock                                                     # noqa: E402

from app import tts as tts_mod                                                # noqa: E402
from app.config import AzureConfig                                            # noqa: E402

MP3 = b"\xff\xfb\x90\x00" + b"M" * 20           # 不是 RIFF：走「原样接受 / 字节直拼」分支
LONG_TEXT = "测试句子。" * 30                    # 150 字（每句 5 字）


def _mk_wav(payload, ch=1, rate=24000, bits=16):
    """造一个 16bit PCM WAV：给拼接用例提供「合法且同参数」的块。"""
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(payload)) + payload)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _wav_chunks(b):
    """照前端 ``TTSClient._wav_stream`` 的方式逐块扫描：产出 ``(块名, 声明长度, 块体)``。"""
    pos = 12
    while pos + 8 <= len(b):
        cid = b[pos:pos + 4]
        sz = int.from_bytes(b[pos + 4:pos + 8], "little")
        yield cid, sz, b[pos + 8:pos + 8 + sz]
        pos += 8 + sz + (sz & 1)


class ChunkJoinTest(unittest.TestCase):
    """T1：``speak()`` 消费 ``chunk_for_synthesis`` 的块，并拼成**一条**音频。"""

    def test_two_chunks_join_into_one_parseable_audio(self):
        inputs, payloads = [], [b"A" * 8, b"B" * 4]

        def handler(req):
            inputs.append(json.loads(req.content.decode("utf-8"))["input"])
            return httpx.Response(200, content=_mk_wav(payloads[len(inputs) - 1]))

        c = _client(handler)                      # tts_long_chunk=False ⇒ 上限 100 字
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(len(inputs), 2, "150 字按 100 字上限应切成两块")
        self.assertTrue(all(x for x in inputs))
        blob = c.audio(out["id"])
        self.assertIsNotNone(blob)
        self.assertEqual(blob[:4], b"RIFF")
        self.assertEqual(int.from_bytes(blob[4:8], "little"), len(blob) - 8)   # 头长度自洽
        chunks = {cid: (sz, body) for cid, sz, body in _wav_chunks(blob)}
        sz_fmt, fmt = chunks[b"fmt "]
        sz_data, data = chunks[b"data"]
        self.assertEqual(sz_fmt, len(fmt))
        self.assertEqual(sz_data, len(data))                                   # data 长度自洽
        self.assertEqual(data, payloads[0] + payloads[1])                      # 只拼 data 段
        self.assertEqual(struct.unpack("<H", fmt[2:4])[0], 1)                  # 单声道
        self.assertEqual(struct.unpack("<I", fmt[4:8])[0], 24000)              # 采样率保留
        self.assertEqual(struct.unpack("<H", fmt[14:16])[0], 16)               # 16bit

    def test_long_chunk_folds_into_one_request(self):
        inputs = []

        def handler(req):
            inputs.append(json.loads(req.content.decode("utf-8"))["input"])
            return httpx.Response(200, content=WAV)

        c = _client(handler, tts_long_chunk=True)  # 上限放宽到 160 字 ⇒ 一块
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0], LONG_TEXT)
        self.assertEqual(c.audio(out["id"]), WAV, "单块必须原样入缓存（WAV / MP3 都接受）")

    def test_single_non_riff_chunk_is_accepted_as_is(self):
        c = _client(lambda req: httpx.Response(200, content=MP3))
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(c.audio(out["id"]), MP3, "云端常忽略 response_format，MP3 也得认")

    def test_two_non_riff_chunks_concatenate_raw(self):
        parts = [MP3, MP3[:4] + b"N" * 10]

        def handler(req):
            return httpx.Response(200, content=parts.pop(0))

        c = _client(handler)
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(out["mode"], "openai")
        self.assertTrue(c.audio(out["id"]).startswith(b"\xff\xfb\x90\x00"))

    def test_mixed_formats_degrade_whole_utterance(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(200, content=_mk_wav(b"X" * 8) if len(calls) == 1 else MP3)

        c = _client(handler)
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(len(calls), 2, "两块都要试过才判定失败")
        self.assertEqual(out["mode"], "system", "混格式 ⇒ 整条降级为系统语音")
        self.assertEqual(out["id"], "")
        self.assertEqual(out["text"], LONG_TEXT)
        self.assertEqual(len(c._cache), 0, "绝不缓存半截音频")

    def test_broken_riff_header_degrades_whole_utterance(self):
        broken = (b"RIFF" + struct.pack("<I", 9999) + b"WAVE"
                  + b"fmt " + struct.pack("<I", 16) + b"\x00" * 16
                  + b"data" + struct.pack("<I", 500) + b"\x00" * 4)    # data 声明 500 字节，实际只有 4
        c = _client(lambda req: httpx.Response(200, content=broken))
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(len(c._cache), 0)

    def test_one_chunk_failing_degrades_whole_utterance(self):
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 2:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, content=_mk_wav(b"Y" * 8))

        c = _client(handler)
        out = asyncio.run(c.speak(LONG_TEXT))
        self.assertEqual(out["mode"], "system", "任一块失败 ⇒ 整条降级")
        self.assertEqual(len(c._cache), 0)


class SystemModeTest(unittest.TestCase):
    """T11：``tts_mode=="system"`` 必须可表达（否则系统语音的玩家完全没声音）。"""

    def test_enabled_matrix(self):
        cases = [
            (True, "openai", "https://x/v1", True),
            (True, "openai", "", False),           # openai 但没有地址：未启用
            (True, "openai", "   ", False),        # 纯空白地址同样不算
            (True, "system", "", True),            # 系统语音不需要地址
            (True, "system", "https://x/v1", True),
            (False, "openai", "https://x/v1", False),
            (False, "system", "", False),
            # T12：未知 / 旧值镜像前端 _normalize_tts_mode → 回落 openai（有地址即启用）
            (True, "off", "https://x/v1", True),
            (True, "cosyvoice", "https://x/v1", True),
            (True, "cosyvoice", "", False),        # 回落 openai 但没地址：未启用
            (True, "", "", False),
        ]
        for on, mode, url, want in cases:
            c = TTSClient(_cfg(tts_enabled=on, tts_mode=mode, tts_base_url=url))
            self.assertIs(c.enabled, want, "tts_enabled=%s tts_mode=%r url=%r" % (on, mode, url))

    def test_system_mode_returns_cleaned_text_without_request(self):
        called = []
        c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV),
                    tts_mode="system", tts_base_url="")
        out = asyncio.run(c.speak("[[tone:cheer]]好棋！📊"))
        self.assertTrue(c.enabled)
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "好棋！", "语气标记与装饰符号不得被念出来")
        self.assertEqual(out["tone"], "cheer")
        self.assertEqual(out["id"], "")
        self.assertIsInstance(out["pitch"], float)
        self.assertEqual(called, [], "system 模式零请求")

    def test_disabled_is_none_in_both_modes(self):
        for mode in ("openai", "system"):
            called = []
            c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV),
                        tts_enabled=False, tts_mode=mode)
            out = asyncio.run(c.speak("你好"))
            self.assertEqual(out["mode"], "none")
            self.assertEqual(out["id"], "")
            self.assertEqual(called, [])


class EmptyBlockTest(unittest.TestCase):
    """T9 遗留：空 / 纯空白块不得 POST 出去。"""

    def test_empty_and_blank_text_makes_no_request(self):
        for mode in ("openai", "system"):
            called = []
            c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV),
                        tts_mode=mode, tts_base_url="https://tts.example.com/v1")
            for text in ("", "   ", "\n\n", "[[tone:sigh]]", None):
                out = asyncio.run(c.speak(text))
                self.assertEqual(out["mode"], "none", "文本=%r" % (text,))
                self.assertEqual(out["id"], "")
                self.assertEqual(out["text"], "")
            self.assertEqual(called, [], "%s 模式下一个请求都不该发" % mode)


class ByteCapTest(unittest.TestCase):
    """缓存必须**条数与字节数**双向淘汰。"""

    def test_byte_budget_evicts_oldest(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))       # 每条 44 字节

        async def run():
            ids = []
            for i in range(4):
                ids.append((await c.speak("第%d条" % i))["id"])
            return ids

        with mock.patch.object(tts_mod, "TTS_CACHE_MAX_BYTES", 100):    # 只装得下两条
            ids = asyncio.run(run())
        self.assertEqual(len(set(ids)), 4, "每条台词各自一个 id")
        self.assertIsNone(c.audio(ids[0]))
        self.assertIsNone(c.audio(ids[1]))
        self.assertIsNotNone(c.audio(ids[2]))
        self.assertIsNotNone(c.audio(ids[-1]))

    def test_audio_hit_refreshes_lru_order(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))

        async def first_three():
            ids = []
            for label in ("甲", "乙", "丙"):
                ids.append((await c.speak(label))["id"])
            return ids

        with mock.patch.object(tts_mod, "TTS_CACHE_MAX_BYTES", 100):    # 只装得下两条
            ids = asyncio.run(first_three())
            self.assertIsNone(c.audio(ids[0]))                          # 最旧的被淘汰
            self.assertIsNotNone(c.audio(ids[1]))                       # 命中：ids[1] 变最新
            newest = asyncio.run(c.speak("丁"))["id"]                   # 淘汰 ids[2]
            self.assertIsNone(c.audio(ids[2]))
        self.assertIsNotNone(c.audio(ids[1]))
        self.assertIsNotNone(c.audio(newest))


class OffLoopTest(unittest.TestCase):
    """T10：同步 httpx 必须经 ``asyncio.to_thread``——10~30 秒的合成不得冻住事件循环。"""

    def test_request_runs_off_the_event_loop_thread(self):
        threads = []

        def handler(req):
            threads.append(threading.current_thread())
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        seen = {}

        async def run():
            seen["loop"] = threading.current_thread()
            return await c.speak("你好")

        out = asyncio.run(run())
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], seen["loop"], "合成请求不得跑在事件循环线程上")

    def test_slow_synthesis_does_not_stall_the_loop(self):
        """事件同步版（T14③）：合成"卡住"到主协程放行为止——只有事件循环没被冻住才可能放行。"""
        release = threading.Event()

        def handler(req):
            release.wait(5.0)                      # 卡住合成；只应由循环上的 ticker 放行
            return httpx.Response(200, content=WAV)

        c = _client(handler)
        ticks = []

        async def ticker():
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks.append(1)
            release.set()

        async def run():
            out, _ = await asyncio.gather(c.speak("你好"), ticker())
            return out

        # 若合成阻塞事件循环，ticker 永远跑不到 release.set()，这里 2 秒后超时 ⇒ 测试失败
        out = asyncio.run(asyncio.wait_for(run(), timeout=2.0))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(len(ticks), 3, "合成期间事件循环必须还能跑别的任务")


class CloseTest(unittest.TestCase):
    """``close()`` 可重复调用，并且真的关掉 HTTP 客户端。"""

    def test_close_is_idempotent_and_closes_client(self):
        http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=WAV)))
        c = TTSClient(_cfg(), http_client=http)
        asyncio.run(c.close())
        self.assertTrue(http.is_closed)
        asyncio.run(c.close())                     # 再关一次不得抛

    def test_close_without_request_makes_no_client(self):
        c = TTSClient(_cfg())                      # 没注入：close 不得反向构造出一个真客户端
        asyncio.run(c.close())
        self.assertIsNone(c._client)


class RobustnessTest(unittest.TestCase):
    """``speak()`` 任何路径都不抛异常：抛出去就会让这一回合的 busy 卡死。"""

    def test_cfg_without_tts_fields_never_raises(self):
        c = TTSClient(object(), http_client=httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=WAV))))
        self.assertFalse(c.enabled)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "none")
        self.assertEqual(set(out), {"id", "mode", "text", "tone", "pitch"})

    def test_dirty_numbers_fall_back(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, content=WAV)

        c = _client(handler, tts_speed="abc", tts_timeout_sec="abc")
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(out["text"], "", "openai 模式不回传台词文本（T13）")
        self.assertEqual(seen[0]["speed"], 1.0)
        self.assertEqual(c._timeout(), 30.0)

    def test_connect_error_degrades_and_never_raises(self):
        def handler(req):
            raise httpx.ConnectError("refused", request=req)

        c = _client(handler)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["text"], "你好")


class ConfigWiringTest(unittest.TestCase):
    """``tts_*`` 字段必须真的被 ``load()/_apply()`` 读进来（防「字段加了但没接线」）。"""

    KEYS = {"tts_enabled": True, "tts_mode": "openai", "tts_base_url": "https://api.example.com/v1",
            "tts_model": "tts-1", "tts_api_key": "k", "tts_voice": "中文女",
            "tts_speed": 1.5, "tts_volume": 0.5, "tts_timeout_sec": 12, "tts_long_chunk": True}

    def test_defaults_are_off_and_system(self):
        c = AzureConfig()
        self.assertFalse(c.tts_enabled)
        self.assertEqual(c.tts_mode, "system")
        self.assertEqual(c.tts_timeout_sec, 30)
        self.assertFalse(c.tts_long_chunk)

    def test_round_trip_through_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.KEYS, f)
            c = AzureConfig.load(p)
        for k, v in self.KEYS.items():
            self.assertEqual(getattr(c, k), v, k)

    def test_dirty_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"tts_speed": "abc", "tts_volume": None, "tts_timeout_sec": "abc"}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.tts_speed, 1.0)
        self.assertEqual(c.tts_volume, 1.0)
        self.assertEqual(c.tts_timeout_sec, 30.0)


# ============================================================================
# 评审修复轮（I1 / I2 / T14）新增的用例。
# ============================================================================


class ModeNormalizationTest(unittest.TestCase):
    """T12 / I1：``tts_mode`` 必须归一化，并镜像前端 ``AzureConfig._normalize_tts_mode``（:98-100）。"""

    def test_case_and_spaces_are_normalized(self):
        for raw in ("System", "SYSTEM", "  system  "):
            c = TTSClient(_cfg(tts_mode=raw, tts_base_url=""))
            self.assertTrue(c.enabled, raw)
            self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "system", raw)
        for raw in ("OpenAI", "  openai  "):
            called = []
            c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV), tts_mode=raw)
            self.assertTrue(c.enabled, raw)
            self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "openai", raw)
            self.assertEqual(called, [1], "%r 必须走合成（有声音）" % raw)

    def test_legacy_mode_falls_back_to_openai(self):
        """旧值 ``cosyvoice``：与前端同一规则回落 openai——判成未启用会让老机器整条语音静默消失。"""
        called = []
        c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV),
                    tts_mode="cosyvoice")
        self.assertTrue(c.enabled)
        out = asyncio.run(c.speak("你好"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(len(called), 1, "回落 openai 就必须真的走合成")
        self.assertNotEqual(out["id"], "")

    def test_unknown_mode_without_base_url_stays_silent(self):
        """回落 openai 但没有地址：仍是未启用（与计划字面的 openai 判据一致）。"""
        called = []
        c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV),
                    tts_mode="cosyvoice", tts_base_url="")
        self.assertFalse(c.enabled)
        self.assertEqual(asyncio.run(c.speak("你好"))["mode"], "none")
        self.assertEqual(called, [])


class TextContractTest(unittest.TestCase):
    """T13 / I2：``text`` **仅**在 ``mode=="system"`` 时非空（计划 Task 3 的事件契约）。"""

    def test_openai_mode_returns_empty_text(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))
        out = asyncio.run(c.speak("[[tone:sigh]]算了。"))
        self.assertEqual(out["mode"], "openai")
        self.assertEqual(out["text"], "", "openai 的台词由缓存音频承载，不回传文本")
        self.assertEqual(out["tone"], "sigh")
        self.assertNotEqual(out["id"], "")

    def test_none_mode_returns_empty_text(self):
        called = []

        def handler(req):
            called.append(1)
            return httpx.Response(200, content=WAV)

        out = asyncio.run(_client(handler, tts_enabled=False).speak("你好"))
        self.assertEqual((out["mode"], out["text"]), ("none", ""))
        out2 = asyncio.run(_client(handler).speak("   "))
        self.assertEqual((out2["mode"], out2["text"]), ("none", ""))
        self.assertEqual(called, [], "none 模式零请求")

    def test_system_mode_keeps_the_text(self):
        c = _client(lambda req: httpx.Response(200, content=WAV), tts_mode="system", tts_base_url="")
        out = asyncio.run(c.speak("[[tone:cheer]]好棋！"))
        self.assertEqual((out["mode"], out["text"]), ("system", "好棋！"))
        # 合成失败降级为 system 时同样要带上文本（前端得靠它念出来）
        c2 = _client(lambda req: httpx.Response(401))
        out2 = asyncio.run(c2.speak("[[tone:cheer]]好棋！"))
        self.assertEqual((out2["mode"], out2["text"], out2["id"]), ("system", "好棋！", ""))

    def test_every_mode_keeps_the_five_keys(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))
        self.assertEqual(set(asyncio.run(c.speak("你好"))),
                         {"id", "mode", "text", "tone", "pitch"})


class SingleBlobOverBudgetTest(unittest.TestCase):
    """T14①：单条就超过字节预算时**直接不插**，不得为了塞不下的一段清空整个缓存。"""

    def test_oversized_audio_does_not_wipe_the_cache(self):
        c = _client(lambda req: httpx.Response(200, content=WAV))       # 每条 44 字节
        with mock.patch.object(tts_mod, "TTS_CACHE_MAX_BYTES", 100):
            keep = asyncio.run(c.speak("留着"))["id"]
            self.assertIsNotNone(c.audio(keep))
        with mock.patch.object(tts_mod, "TTS_CACHE_MAX_BYTES", 10):     # 单条 44 > 10
            out = asyncio.run(c.speak("塞不下"))
            self.assertEqual(out["mode"], "system")
            self.assertEqual(out["id"], "")
        self.assertIsNotNone(c.audio(keep), "塞不下的一条不得把已有缓存清空")
        self.assertEqual(list(c._cache), [keep], "缓存内容一条不少、一条不多")


class _Boom:
    """``str()`` 就抛的探针：用来制造清洗阶段的异常。"""

    def __str__(self):
        raise RuntimeError("坏文本")


class CleaningFailureTest(unittest.TestCase):
    """T14②：清洗阶段的异常落 ``system``（计划 Step 3「任何异常 → system」），``none`` 只留给关闭态。"""

    def test_cleaning_exception_degrades_to_system(self):
        called = []
        c = _client(lambda req: called.append(1) or httpx.Response(200, content=WAV))
        out = asyncio.run(c.speak(_Boom()))
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["id"], "")
        self.assertEqual(called, [], "清洗都失败了，不该再发请求")
        self.assertEqual(set(out), {"id", "mode", "text", "tone", "pitch"})

    def test_cleaning_exception_when_disabled_stays_none(self):
        c = _client(lambda req: httpx.Response(200, content=WAV), tts_enabled=False)
        self.assertEqual(asyncio.run(c.speak(_Boom()))["mode"], "none", "关闭态优先于降级")
