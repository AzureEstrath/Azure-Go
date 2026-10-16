# 计划 05：语音合成搬到后端 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让“她说什么、怎么切句、什么语气”全部由后端决定，前端只负责取回音频并播放。

**Architecture:** 新增 `backend/app/speech_text.py`（从 `game/scripts/services/TTSClient.gd` 移植清洗/分句/长句合并/语气标记提取）与 `backend/app/tts.py`（云端 OpenAI 兼容 `/v1/audio/speech` 客户端 + LRU 音频缓存 + 合成失败时降级为“让前端用系统语音念”的指令）。后端在发台词时追加 `speech` 事件（携带音频 id、分组序、语气、音高、模式）；前端 `TTSClient.gd` 退化为“取音频 + 排队播放 + 音量 + 按指令调用系统语音”。

**Tech Stack:** httpx（云端 TTS）、标准库 `collections.OrderedDict`（LRU）、FastAPI `Response(media_type="audio/wav")`；`unittest` + `httpx.MockTransport`。

**Spec:** `docs/backend-langchain-plan.md`（决策 D1/D3/D7）

## Global Constraints

- 前置：计划 0/01 已完成（本地语音服务已删、`tts_mode` 已是云端 `openai`）。
- 测试一律 `unittest`：`cd backend && python -m unittest discover -s tests`；不得触网。
- **音画同步不变**：落子宣告仍靠前端在“这条语音开始播放”时 `POST /api/reveal`；因此 `speech` 事件必须携带能对应到具体台词的信息（`turn_id` + `seq`），但**不改变** `message` 事件的既有形状。
- **顺序与锁仍在前端**（呈现调度是前端职责）：`tts_sync_speak`（文字等语音就绪）、`_speech_hold`（语音未播完锁棋盘）保持现有实现。
- 合成失败必须优雅降级：后端改发 `mode:"system"` 的 `speech` 指令（前端用 Windows 系统语音念），**不得**让文字消失、不得阻塞回合。
- 清洗规则不得回退：emoji/颜文字/方括号标记要被去掉（现有 `clean_for_speech` 行为），`[[tone:sigh|cheer|proud]]` 只取一个且不算句子。
- 缓存上限 `TTS_CACHE_MAX = 32`（条）/ `TTS_CACHE_MAX_BYTES = 8 * 1024 * 1024`。
- 提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.5.0**，同步写进 `README.md` 的「更新」小节。

## Review Focus

- **合成慢/超时**：云端 TTS 可能 10~20 秒才回；超时必须降级为 `system` 指令而不是让回合卡死（玩家已习惯“首字 1~3 秒”的节奏）。
- **语气标记泄漏**：`[[tone:...]]` 若没被剥干净会直接被系统语音念出来（历史事故类型）。
- **音频与文字错位**：连发多条台词时，`speech` 的 `seq` 必须与 `message` 一一对应；前端乱序播放不可接受。
- **缓存内存**：长对局 + 长总结会累积大量 WAV；必须双向（条数 + 字节）淘汰。
- **`tts_enabled=false`**：不得发起任何 TTS 请求、不得发 `speech` 事件，文字照常。

---

### Task 1: `speech_text.py` —— 语音文本清洗与分句（从 GDScript 移植）

**Files:**
- Create: `backend/app/speech_text.py`
- Create: `backend/tests/test_speech_text.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `clean_for_speech(text: str) -> str`（去 emoji / 颜文字 / `[方括号]` 标记 / 多余空白，保留中文标点）
  - `extract_tone(text: str) -> tuple[str, str]`（返回 `(tone, rest)`，`tone ∈ {"", "sigh", "cheer", "proud"}`，只取第一个标记）
  - `split_sentences(text: str) -> list[str]`（按 `。！？；!?;\n` 切分，过滤空句）
  - `chunk_for_synthesis(text: str, max_chars: int = 100) -> list[str]`（长句合并：相邻短句拼到 `max_chars` 上限，超长句按标点再切）
  - 常量 `SYMBOL_RE`、`TONE_RE`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_speech_text.py`

```python
"""语音文本清洗：规则必须与前端 TTSClient.gd 的既有行为一致。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.speech_text import chunk_for_synthesis, clean_for_speech, extract_tone, split_sentences


class CleanTest(unittest.TestCase):
    def test_strips_emoji_and_faces(self):
        out = clean_for_speech("📊 黑棋在左上形成势力 (｡･ω･｡) 【对局总结】\n这手不错~  ⏪")
        self.assertNotIn("📊", out)
        self.assertNotIn("｡･ω･｡", out)
        self.assertNotIn("【", out)
        self.assertIn("黑棋在左上形成势力", out)
        self.assertIn("这手不错", out)

    def test_keeps_chinese_punctuation(self):
        self.assertIn("，", clean_for_speech("你好，Azure。"))

    def test_collapses_whitespace(self):
        self.assertEqual(clean_for_speech("  甲   乙  "), "甲 乙")

    def test_empty(self):
        self.assertEqual(clean_for_speech(""), "")


class ToneTest(unittest.TestCase):
    def test_extract_first_only(self):
        tone, rest = extract_tone("[[tone:sigh]]算了。\n[[tone:cheer]]不过这手不错！")
        self.assertEqual(tone, "sigh")
        self.assertIn("不过这手不错", rest)          # 标记全部剥掉，正文保留
        self.assertNotIn("tone:", rest)

    def test_unknown_tone_ignored(self):
        tone, rest = extract_tone("[[tone:angry]]哼")
        self.assertEqual(tone, "")
        self.assertNotIn("tone:", rest)

    def test_no_tone(self):
        self.assertEqual(extract_tone("你好"), ("", "你好"))


class SplitTest(unittest.TestCase):
    def test_split_multiple(self):
        self.assertEqual(split_sentences("你好。我在这里！在吗？"), ["你好。", "我在这里！", "在吗？"])

    def test_split_drops_empty(self):
        self.assertEqual(split_sentences("嗯。。\n\n"), ["嗯。。"])

    def test_chunk_merges_short(self):
        chunks = chunk_for_synthesis("甲。乙。丙。", max_chars=100)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "甲。乙。丙。")

    def test_chunk_splits_long(self):
        chunks = chunk_for_synthesis("甲" * 250, max_chars=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertGreaterEqual(len(chunks), 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_speech_text -v`
Expected: FAIL（`No module named 'app.speech_text'`）

- [ ] **Step 3: 实施** — 以 `game/scripts/services/TTSClient.gd` 的 `clean_for_speech/_split_sentences/_chunk_for_slow` 与 `Main.gd:_strip_tone` 为行为基准逐条移植（emoji 判定用 Unicode 区间 + 已知符号表，不要引入第三方库）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_speech_text -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/speech_text.py backend/tests/test_speech_text.py
git commit -m "feat(backend,tts): 语音文本清洗/分句/语气提取移植到后端（v3.5.0）"
```

---

### Task 2: `tts.py` —— 云端合成 + 缓存 + 降级

**Files:**
- Create: `backend/app/tts.py`
- Create: `backend/tests/test_tts_backend.py`
- Modify: `backend/app/config.py`（新增 `tts_enabled=False`、`tts_mode="system"`、`tts_base_url`、`tts_model`、`tts_api_key`、`tts_voice`、`tts_speed=1.0`、`tts_volume=1.0`、`tts_timeout_sec=30`、`tts_long_chunk=False`）

**Interfaces:**
- Consumes: `speech_text`（Task 1）
- Produces:
  - `class TTSClient:` `__init__(self, cfg, http_client=None)`
    - `enabled -> bool`（`tts_enabled and tts_mode == "openai" and tts_base_url`）
    - `async speak(self, text: str) -> dict`（返回 `{"id": str, "mode": "openai"|"system", "text": str, "tone": str, "pitch": float}`；`openai` 模式下音频已入缓存，`system` 模式下 `text` 为可直接朗读的清洗后文本）
    - `audio(self, audio_id: str) -> bytes | None`
    - `async close(self) -> None`
  - 常量 `TTS_CACHE_MAX = 32`、`TTS_CACHE_MAX_BYTES = 8 * 1024 * 1024`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_tts_backend.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_tts_backend -v`
Expected: FAIL（`No module named 'app.tts'`）

- [ ] **Step 3: 实施** — `tts.py`：`speak()` 先 `extract_tone` → `clean_for_speech`；`mode=="openai"` 时 POST 合成（`Authorization: Bearer` 仅在 key 非空时带）、入 LRU 缓存并返回 id（用 `uuid4().hex[:12]`）；任何异常/非 200 → 返回 `{"id":"", "mode":"system", "text":清洗后文本, "tone":tone, "pitch":0.0}`；`enabled` 为假 → `{"id":"", "mode":"none", ...}`，且**不发请求**。LRU 淘汰同时看条数与字节数。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_tts_backend -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tts.py backend/app/config.py backend/tests/test_tts_backend.py
git commit -m "feat(backend,tts): 云端语音合成客户端与音频缓存（v3.5.0）"
```

---

### Task 3: 后端发 `speech` 事件与音频端点

**Files:**
- Modify: `backend/app/events.py`（新增 `speech(audio_id, mode, tone, pitch, turn_id, seq, text="")`）
- Modify: `backend/app/agent.py`（`finalize` 阶段对每条对外台词追加 `speech` 事件；`tts_enabled=false` 时不发）
- Modify: `backend/app/main.py`（`POST /api/tts/speak` `{text}` → `audio/wav`（试听/手动用）；`GET /api/tts/audio/{audio_id}` → `audio/wav` 或 404；lifespan 构造 `TTSClient(cfg)` 注入 agent 并 `close()`）
- Modify: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: `TTSClient`（Task 2）
- Produces:
  - `create_app(cfg=None, engine=None, llm=None, agent=None, tts=None, autostart=True)`（新增 `tts` 注入口，供测试注入假 TTS；`tts=None` 时用 `TTSClient(cfg)` 构造）
  - 事件：`{"type": "speech", "id": str, "mode": "openai"|"system"|"none", "tone": str, "pitch": float, "turn_id": str, "seq": int, "text": str}`（`text` 仅在 `mode=="system"` 时非空）
  - **发事件的判据**：`mode == "none"`（语音关闭）时**不发** `speech`；`openai` 与 `system` 都发。
  - `POST /api/tts/speak` → `Response(media_type="audio/wav")`；合成失败 → 409 `{"ok": False, "error": "..."}`（试听按钮可提示）
  - `GET /api/tts/audio/{audio_id}` → `audio/wav` | 404

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_main.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_main -v`
Expected: FAIL（无 `speech` 事件；`/api/tts/audio/...` 404 断言通过但前一条失败）

- [ ] **Step 3: 实施** — `agent.py` 的 `finalize` 里：对每条将要 `publish` 的 `message` 事件，先 `await self.tts.speak(text)` 再 `publish(speech(...))`（`seq` 自增）。**注意**：`ai_move` 的顺序不变——`speech` 紧跟对应 `message` 之后发出，前端仍在语音开始播放时 `POST /api/reveal`。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/events.py backend/app/agent.py backend/app/main.py backend/tests/test_main.py
git commit -m "feat(backend,api): speech 事件与语音音频端点（v3.5.0）"
```

---

### Task 4: 前端 `TTSClient.gd` 退化为播放器

**Files:**
- Modify: `game/scripts/net/NetClient.gd`（`speech` → 新信号 `speech_event(payload)`）
- Modify: `game/scripts/ui/Main.gd`（`_on_speech_event(payload)`：`openai` → `GET /api/tts/audio/{id}` 取字节后交给 `tts.play_audio(bytes, seq)`；`system` → `tts.speak_system(payload.text, seq)`；维护 `_speech_pending_seq` 保证顺序）
- Modify: `game/scripts/services/TTSClient.gd`（**删除**：`clean_for_speech`、`_split_sentences`、`_chunk_for_slow`、`_speak_system` 里的清洗、`preview`、本地服务探测/`is_slow_engine` 的本地判定；**保留**：`play_audio(bytes, seq)`、`speak_system(text, seq)`、播放队列、`volume`、`speaking_changed/utterance_started/speech_skipped` 信号、预制语气词播放）
- Modify: `game/tests/test_tts.gd`（保留 WAV 解析与队列断言；删掉清洗用例——它已搬后端）

**Interfaces:**
- Consumes: `speech` 事件、`GET /api/tts/audio/{id}`、`POST /api/tts/speak`
- Produces: `TTSClient.play_audio(bytes: PackedByteArray, seq: int) -> int`（返回分组号）、`TTSClient.speak_system(text: String, seq: int) -> int`

- [ ] **Step 1: 改前端** — 按 `Files` 执行；`_on_utterance_started` 里的 `reveal_move()` 调用链**不许动**（音画同步）。
- [ ] **Step 2: 确认清洗逻辑已不在前端**

Run: `rg -n "clean_for_speech|_split_sentences|_chunk_for_slow" game`
Expected: 无匹配

- [ ] **Step 3: 冒烟验证**

Run: `godot --headless --path game --import`
Expected: 无 `SCRIPT ERROR`

Run: `godot --path game --headless --script res://tests/test_tts.gd`
Expected: WAV 解析 OK、队列不崩溃

- [ ] **Step 4: 提交**

```bash
git add game
git commit -m "refactor(game,tts): TTSClient 退化为播放器，清洗与合成归后端（v3.5.0）"
```

---

### Task 5: 配置、文档与版本

**Files:**
- Modify: `game/config.json`（删 `tts_limit_len/tts_sync_speak/tts_batch_speak/tts_long_chunk` 中已归后端的项——保留 `tts_sync_speak`（前端调度用）；新增 `tts_timeout_sec: 30`）
- Modify: `README.md`（第五节「接语音」改写为：合成在后端、前端只播放；字段表更新；新增「语音端点」小节含 curl 示例；第九节新增 `### v3.5.0`）
- Modify: `AGENTS.md`（补：改语音链路时清洗/分句在后端、`speech` 与 `message` 的配对关系、`reveal` 时机不许改）

**Interfaces:**
- Consumes: 无
- Produces: 与代码一致的文档

- [ ] **Step 1: 改文档与模板**
- [ ] **Step 2: 校验**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `godot --headless --path game --quit`
Expected: 无 `SCRIPT ERROR`

- [ ] **Step 3: 提交**

```bash
git add README.md AGENTS.md game/config.json
git commit -m "docs(config): 语音链路后端化说明，README 记录 v3.5.0（v3.5.0）"
```
