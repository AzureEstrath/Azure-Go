"""云端语音合成客户端：把一段台词变成「一条缓存音频」或「一条系统语音指令」。

职责边界（计划 05 · Task 2）：
- 文本侧一律先经 ``speech_text``：``extract_tone`` → ``clean_for_speech`` → ``chunk_for_synthesis``
  （Task 1 已交付），本模块只负责把块送去合成、把结果拼成**一条**音频、按上限缓存；
- ``tts_mode == "system"`` 时不发任何请求，只回一条「用系统语音念这段文本」的指令——
  这样系统语音模式的玩家也有声音（T11）；
- 合成失败（网络错误 / 超时 / 非 200 / 解析失败 / 任一块失败）一律**整条**降级为
  ``mode: "system"``：绝不返回半截音频，也绝不让回合卡死（fail-closed）；
- ``speak()`` **不抛异常**——它是回合链路的一环，抛出去就会让 ``busy`` 卡死（本项目最严重的故障形态）。

两条不能回退的硬规则：
1. 网络调用是**同步** ``httpx.Client``，必须经 ``await asyncio.to_thread(...)`` 放出去（T10）：
   一次 10~30 秒的合成直接跑在事件循环上会冻住心跳 / WebSocket / ``reveal``（同计划 03 的 I2）。
2. 缓存**条数与字节数双向**淘汰（``TTS_CACHE_MAX`` / ``TTS_CACHE_MAX_BYTES``）：长对局 +
   长总结会累积大量 WAV。

拼接规则（T1，混格式或任一块失败 ⇒ 整条降级 ``system``）：
- **单块**：响应原样入缓存——WAV 或 MP3 都接受（云端服务常忽略 ``response_format``）；
- **多块，全是 RIFF**：各块 ``fmt `` 段必须一致，只取各块 ``data`` 段拼起来并重建一个 WAV 头；
- **多块，全非 RIFF**（如 MP3）：直接字节拼接（MP3 帧拼接可播）。
"""

import asyncio
import threading
import uuid
from collections import OrderedDict

import httpx

from .speech_text import chunk_for_synthesis, clean_for_speech, extract_tone

# 缓存上限：条数与字节数（超出即按 LRU 淘汰最旧的）
TTS_CACHE_MAX = 32
TTS_CACHE_MAX_BYTES = 8 * 1024 * 1024

# 单块字数上限：``tts_long_chunk`` 打开时放宽（与前端 tts_long_chunk 的既有语义一致）
CHUNK_CHARS = 100
CHUNK_CHARS_LONG = 160

# 单次合成超时（秒）：配置缺字段 / 非正数 / 脏值都退回这个值
DEFAULT_TIMEOUT_SEC = 30.0

# 合成端点（OpenAI 兼容的 /audio/speech）
SPEECH_PATH = "/audio/speech"


def _text_of(cfg, name: str, default: str = "") -> str:
    """读一个字符串配置项；``None`` / 缺失都当空串。"""
    v = getattr(cfg, name, default)
    return default if v is None else str(v)


def _float_cfg(cfg, name: str, default: float) -> float:
    """读一个浮点配置项；缺失 / 脏值退回 ``default``（不因一个旋钮把这一回合搞崩）。"""
    try:
        return float(getattr(cfg, name, default))
    except (TypeError, ValueError):
        return default


class TTSClient:
    """云端 TTS 客户端：``speak()`` → 音频 id（``openai``）或系统语音指令（``system``）。

    ``http_client`` 留空时**惰性**自建 ``httpx.Client``（只在真要发请求时构造：关闭 / 系统
    语音模式下零开销）。注入的客户端由本对象负责关闭（同 ``engine.py`` 的既有处置）。
    """

    def __init__(self, cfg, http_client=None):
        self.cfg = cfg
        self._client = http_client
        self._lock = threading.Lock()                            # 只为「并发首用时自建客户端」上锁
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()   # id → 音频（LRU：队首最旧）
        self._cache_bytes = 0

    # ================= 只读属性 =================

    @property
    def enabled(self) -> bool:
        """是否有话要说（T11）：``tts_enabled`` 且模式可表达。

        ``system`` 模式**必须**算启用——前端在 Task 4 之后只有「按 ``speech`` 指令播放」这
        一条路，判成未启用等于让系统语音模式的玩家完全没有声音。``openai`` 模式还要求
        有服务地址；模式既不是 ``system`` 也不是 ``openai``（含空串）一律未启用。
        """
        if not bool(getattr(self.cfg, "tts_enabled", False)):
            return False
        mode = _text_of(self.cfg, "tts_mode").strip()
        if mode == "system":
            return True
        return mode == "openai" and _text_of(self.cfg, "tts_base_url").strip() != ""

    def audio(self, audio_id: str) -> bytes | None:
        """按 id 取音频；未知 id 返回 ``None``。命中会刷新 LRU 位置。"""
        if not audio_id:
            return None
        blob = self._cache.get(audio_id)
        if blob is None:
            return None
        self._cache.move_to_end(audio_id)
        return blob

    # ================= 主入口 =================

    async def speak(self, text: str) -> dict:
        """清洗 → 分块 → 合成 → 缓存，返回 ``{"id", "mode", "text", "tone", "pitch"}``。

        ``mode`` 三态：``openai``（音频已入缓存，``id`` 可查）、``system``（``text`` 可直接
        朗读）、``none``（功能关闭或没有可念的内容，调用方不要发 ``speech`` 事件）。
        ``pitch`` 固定 ``0.0``——本层拿不到情绪，``speech`` 事件的音高由 agent 侧填（T2）。

        **任何路径都不抛异常**：异常 / 超时 / 非 200 / 解析失败全落成 ``system``（关闭时落
        ``none``），文字照常发。
        """
        tone, clean = "", ""
        try:
            tone, body = extract_tone("" if text is None else str(text))
            clean = clean_for_speech(body)
        except Exception as e:                  # noqa: BLE001 —— 清洗失败也不能让这一回合炸掉
            print("[TTS] 文本清洗失败（话不念了）：%s" % e)
            return _result("none", "", "", "")
        try:
            if not self.enabled:
                return _result("none", clean, tone)
            # 空 / 纯空白文本没有可念的内容：跳过空块、不发请求（T9 遗留）
            chunks = self._chunks(clean)
            if not chunks:
                return _result("none", clean, tone)
            if _text_of(self.cfg, "tts_mode").strip() == "system":
                return _result("system", clean, tone)      # 系统语音：一条指令，零请求
            blob = await self._synthesize(chunks)
            if blob is None:
                return _result("system", clean, tone)
            audio_id = uuid.uuid4().hex[:12]
            if not self._store(audio_id, blob):
                # 单条音频就超过整个字节预算：与其给前端一个查不到的 id，不如改用系统语音
                return _result("system", clean, tone)
            return _result("openai", clean, tone, audio_id)
        except Exception as e:                  # noqa: BLE001 —— fail-closed，绝不抛回回合链路
            print("[TTS] 合成失败（降级为系统语音）：%s" % e)
            return _result("system", clean, tone)

    async def close(self) -> None:
        """关闭 HTTP 客户端；可重复调用。缓存随对象一起回收。"""
        c, self._client = self._client, None
        if c is None:
            return
        try:
            await asyncio.to_thread(c.close)
        except Exception as e:                  # noqa: BLE001 —— 关闭失败只打日志
            print("[TTS] 关闭 HTTP 客户端出错：%s" % e)

    # ================= 内部：分块与合成 =================

    def _chunks(self, text: str) -> list[str]:
        """按 ``tts_long_chunk`` 定上限分块，并**跳过空 / 纯空白块**（T9 遗留，不发无谓请求）。"""
        max_chars = CHUNK_CHARS_LONG if bool(getattr(self.cfg, "tts_long_chunk", False)) else CHUNK_CHARS
        return [c for c in chunk_for_synthesis(text, max_chars=max_chars) if c.strip()]

    async def _synthesize(self, chunks: list[str]) -> bytes | None:
        """逐块合成；任一块失败返回 ``None``（整条降级，绝不返回半截音频）。

        每次网络往返都经 ``to_thread``（T10）：同步 ``httpx`` 不得跑在事件循环线程上。
        """
        parts: list[bytes] = []
        for chunk in chunks:
            blob = await asyncio.to_thread(self._post_chunk, chunk)
            if blob is None:
                return None
            parts.append(blob)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]                     # 单块：WAV / MP3 原样接受
        return _join_audio(parts)               # 多块：同格式才拼，否则 None

    def _post_chunk(self, text: str) -> bytes | None:
        """同步 POST 一块文本，返回音频字节；非 200 / 空响应 / 异常都返回 ``None``。"""
        try:
            body = {
                "model": _text_of(self.cfg, "tts_model"),
                "voice": _text_of(self.cfg, "tts_voice"),
                "input": text,
                "speed": _float_cfg(self.cfg, "tts_speed", 1.0),
                "response_format": "wav",
            }
            headers = {"Content-Type": "application/json"}
            key = _text_of(self.cfg, "tts_api_key").strip()
            if key:
                headers["Authorization"] = "Bearer " + key     # 留空＝不鉴权（本机回环调试）
            resp = self._http().post(self._speech_url(), json=body, headers=headers,
                                     timeout=self._timeout())
            if resp.status_code != 200:
                print("[TTS] 合成返回 HTTP %d（降级为系统语音）" % resp.status_code)
                return None
            data = bytes(resp.content or b"")
            if not data:
                print("[TTS] 合成返回空响应（降级为系统语音）")
                return None
            return data
        except Exception as e:                  # noqa: BLE001 —— 超时 / 连接被拒 / 解析失败
            print("[TTS] 合成请求失败（降级为系统语音）：%s" % e)
            return None

    def _http(self) -> httpx.Client:
        """惰性自建同步客户端：只有真要发请求时才构造（关闭 / 系统语音模式零开销）。

        加锁只为并发首用时不至于各造一个（多出来的那个没人关）。
        """
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=self._timeout())
        return self._client

    def _speech_url(self) -> str:
        return _text_of(self.cfg, "tts_base_url").strip().rstrip("/") + SPEECH_PATH

    def _timeout(self) -> float:
        t = _float_cfg(self.cfg, "tts_timeout_sec", DEFAULT_TIMEOUT_SEC)
        return t if t > 0 else DEFAULT_TIMEOUT_SEC

    # ================= 内部：缓存 =================

    def _store(self, audio_id: str, blob: bytes) -> bool:
        """入缓存并按条数 + 字节数双向淘汰；返回该条是否还在缓存里。"""
        self._cache[audio_id] = blob
        self._cache.move_to_end(audio_id)
        self._cache_bytes += len(blob)
        while len(self._cache) > TTS_CACHE_MAX or self._cache_bytes > TTS_CACHE_MAX_BYTES:
            if not self._evict_oldest():
                break
        return audio_id in self._cache

    def _evict_oldest(self) -> bool:
        """淘汰队首（最久未用）；缓存已空返回 ``False``。"""
        if not self._cache:
            return False
        _, blob = self._cache.popitem(last=False)
        self._cache_bytes -= len(blob)
        return True


def _result(mode: str, text: str, tone: str, audio_id: str = "") -> dict:
    """统一的返回形状：``pitch`` 固定 0.0（T2，音高由 agent 侧填）。"""
    return {"id": audio_id, "mode": mode, "text": text, "tone": tone, "pitch": 0.0}


# ================= 内部：音频拼接（T1） =================

def _join_audio(parts: list[bytes]) -> bytes | None:
    """多块拼接：全 WAV（``fmt`` 一致）→ 重建 WAV 头；全非 WAV → 字节直拼；其余 → ``None``。

    ``None`` 即「整条降级 ``system``」。注意「带了 RIFF 头却解析不出来」也算失败：若把它当
    非 WAV 直拼，第二块的 RIFF 头会夹在音频中间，前端只会念出第一小截（T1 禁止的半截音频）。
    """
    parsed = [_parse_wav(p) for p in parts]
    if all(p is not None for p in parsed):      # 全是可解析的 WAV
        fmts = {p[0] for p in parsed}
        if len(fmts) != 1:
            return None                         # WAV 但参数不一致（采样率 / 位深不同）
        data = b"".join(p[1] for p in parsed)
        return _build_wav(fmts.pop(), data) if data else None
    if any(p[:4] == b"RIFF" for p in parts):    # 混格式 / RIFF 头损坏：不敢拼
        return None
    return b"".join(parts)                      # 全是 MP3 之类：帧拼接可播


def _parse_wav(b: bytes) -> tuple[bytes, bytes] | None:
    """解析出 ``(fmt 段, 所有 data 段的拼接)``；不是 WAV / 缺段 / 头与实际长度不符 → ``None``。"""
    if len(b) < 12 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    pos, fmt, datas = 12, None, []
    while pos + 8 <= len(b):
        cid = b[pos:pos + 4]
        size = int.from_bytes(b[pos + 4:pos + 8], "little")
        end = pos + 8 + size
        if end > len(b):                        # 头部声明比实际长：不敢拼，交给降级
            return None
        body = b[pos + 8:end]
        if cid == b"fmt " and fmt is None:
            fmt = body
        elif cid == b"data":
            datas.append(body)
        pos = end + (size & 1)                  # 块体按偶数对齐
    if fmt is None or not datas:
        return None
    return fmt, b"".join(datas)


def _build_wav(fmt: bytes, data: bytes) -> bytes:
    """用 ``fmt`` 段与 PCM 数据重建一个 16bit WAV（头长与 ``data`` 长度自洽）。

    前端 ``TTSClient._wav_stream`` 从偏移 12 起逐块扫 ``fmt `` / ``data``，所以这里必须
    补上 RIFF 头、按偶数对齐块体，并把 ``data`` 的**真实**长度写进头里。
    """
    fmt_pad = fmt + (b"\x00" if len(fmt) & 1 else b"")
    data_pad = data + (b"\x00" if len(data) & 1 else b"")
    body = (b"WAVE"
            + b"fmt " + len(fmt).to_bytes(4, "little") + fmt_pad
            + b"data" + len(data).to_bytes(4, "little") + data_pad)
    return b"RIFF" + len(body).to_bytes(4, "little") + body
