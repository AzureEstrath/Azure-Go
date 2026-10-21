"""阿里云 Voice Studio 实时语音合成（WebSocket）适配器。

与 :mod:`app.tts` 的关系：**原有实现一行逻辑都不改**——这里继承 ``TTSClient``，
只替换「把一段文本变成一条音频」这一步（``_synthesize``）。清洗、分块、缓存、
LRU 双向淘汰、「失败整条降级 ``system``」、「``speak()`` 绝不抛异常」等既有
语义全部沿用，上层契约（``mode="openai"`` + ``id`` 可取音频）完全不变。

协议（《实时语音合成 WebSocket API 参考》，Voice Studio / 伶鹊）：

    建连（握手头 ``Authorization: Bearer <api_key>``）
      → ``run-task``（音色 / 格式 / 采样率，``input`` 固定空对象）
      → ``task-started``（**收到后才能发文本**）
      → ``continue-task``（全文；单次 ≤ 20000 字符，中文约 10000 字）
      → ``finish-task``（**不可省略**，否则尾部缓存文本不出声）
      → 若干 ``result-generated`` + 紧随其后的 BINARY 音频帧
      → ``task-finished``（**收齐后才能用**，提前关会丢尾音）

几条与 ``tts.py`` 同源的硬规则：

1. **任何失败都只降级，不抛给回合链路**：``_synthesize`` 返回 ``None`` 时，
   父类 ``speak()`` 会自动落成 ``mode="system"``（文字照常、``busy`` 照常归位）。
2. **超时 / 取消后必须丢弃这条连接**：``asyncio.wait_for`` 取消的是一次
   ``recv``/``send``，连接状态已不可知，复用会把下一条台词推进脏连接。
3. **连接是复用的**：一条连接只跑一个任务（协议约束），任务串行；
   业务空闲 60 秒会被服务端回收（关闭码 4408）、单连接上限 1 小时，
   所以这里在空闲 / 超龄时**主动换新连接**，而不是等它断。
"""

import asyncio
import inspect
import json
import time
import uuid

import websockets

from .tts import TTSClient

# 接口地址固定（Voice Studio），可用 ``tts_aliyun_ws_url`` 覆盖（联调 / 私有网关）
WS_URL = "wss://convai-platform.aliyuncs.com/api-ws/v1/inference"

# 单次 ``continue-task`` 的中文实际上限：协议按 UTF-16 计 20000 字符 ＝ 约 10000 汉字
CONTINUE_MAX_CHARS = 10000

# 业务空闲 60 秒服务端主动关闭（4408）；提前换新连接，别让下一条台词撞在断线上
IDLE_RECONNECT_SEC = 45.0
# 单连接最大时长 3600 秒；留 10 分钟余量
MAX_CONN_AGE_SEC = 3000.0

# 单条音频的接收上限：正常一段台词远小于这个值，超了说明跑飞了
MAX_AUDIO_BYTES = 16 * 1024 * 1024

# 握手 / 收流的兜底超时（秒，与 tts_timeout_sec 取小值）
OPEN_TIMEOUT_SEC = 10.0

DEFAULT_FORMAT = "pcm"           # 走裸 PCM：自己补 44 字节 WAV 头，容器形态完全可控
# 协议默认写 22050，但 CosyVoice3 系引擎（voice-tts-cosyvoice3-*）**只原生支持 24000**：
# 22050 会被 400 拒（"only supports sample_rate 24000 natively"）。24000 在协议允许集内，取它。
DEFAULT_SAMPLE_RATE = 24000


class _TaskFailed(Exception):
    """服务端 ``task-failed``。``client_error`` 重试无意义，直接整条降级。"""


def _text_of(cfg, name: str, default: str = "") -> str:
    v = getattr(cfg, name, default)
    return default if v is None else str(v)


def _int_cfg(cfg, name: str, default: int, lo, hi) -> int:
    """读一个整数配置项并钳到 ``[lo, hi]``；脏值退回默认（不让一个旋钮崩掉整回合）。"""
    try:
        return max(lo, min(hi, int(float(getattr(cfg, name, default)))))
    except (TypeError, ValueError):
        return default


def _float_cfg(cfg, name: str, default: float, lo, hi) -> float:
    try:
        return max(lo, min(hi, float(getattr(cfg, name, default))))
    except (TypeError, ValueError):
        return default


def _truthy(cfg, name: str, default: bool = True) -> bool:
    v = getattr(cfg, name, default)
    return default if v is None else bool(v)


def _split_text(text: str, limit: int = CONTINUE_MAX_CHARS) -> list[str]:
    """按 ``limit`` 硬切成若干段（协议按 UTF-16 计数，这里按字符切更保守）。"""
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """给裸 PCM（int16 小端、单声道）补一个标准 44 字节 WAV 头。

    前端 ``TTSClient._wav_stream`` 从偏移 12 起逐块扫 ``fmt `` / ``data``，靠头里的
    采样率 / 位深建 ``AudioStreamWAV``——所以头必须自洽（``data`` 长度写真实字节数），
    否则会念半截或整条不出声。奇数长度补一个填充字节（不计入 ``data`` 长度）。
    """
    if not pcm:
        return b""
    block = channels * bits // 8
    pad = b"\x00" if len(pcm) & 1 else b""
    fmt = (b"\x01\x00"                                  # PCM
           + channels.to_bytes(2, "little")
           + rate.to_bytes(4, "little")
           + (rate * block).to_bytes(4, "little")       # 字节率
           + block.to_bytes(2, "little")
           + bits.to_bytes(2, "little"))
    body = (b"WAVE"
            + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
            + b"data" + len(pcm).to_bytes(4, "little") + pcm + pad)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _event_of(message) -> str:
    """从下行 TEXT 帧里取 ``header.event``；脏帧一律当未知事件（前向兼容）。"""
    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    header = data.get("header")
    return str(header.get("event") or "") if isinstance(header, dict) else ""


def _failure_detail(message) -> str:
    """``task-failed`` 的可读描述：错误码 + 错误信息（都缺时退回原文）。"""
    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return str(message)[:200]
    if not isinstance(data, dict):
        return str(message)[:200]
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = header.get("error_code") or err.get("code") or ""
    msg = header.get("error_message") or err.get("message") or ""
    kind = err.get("type") or ""
    return " ".join(x for x in (str(code), str(msg), ("[%s]" % kind) if kind else "") if x) or "未知错误"


async def _open_ws(url: str, headers: dict, open_timeout: float):
    """建立 WebSocket 连接；兼容 ``websockets`` 12（``extra_headers``）与 14+（``additional_headers``）。

    这个差异是上游改名造成的：13 起新 asyncio 实现用 ``additional_headers``，
    旧实现用 ``extra_headers``；项目 requirements 只写了 ``>=12``，两种都可能装上。
    """
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        params = {}
    kw = {"max_size": MAX_AUDIO_BYTES + 64 * 1024, "open_timeout": open_timeout}
    if "additional_headers" in params:
        kw["additional_headers"] = headers
    elif "extra_headers" in params:
        kw["extra_headers"] = headers
    else:                                   # 都不认（未来版本再改名）：不带握手头会 401，明确报出来
        raise RuntimeError("当前 websockets 版本不支持自定义握手头（需要 additional_headers / extra_headers）")
    for name, value in (("ping_interval", 20), ("ping_timeout", 20), ("close_timeout", 2)):
        if name in params:
            kw[name] = value
    return await websockets.connect(url, **kw)


class AliyunTTSClient(TTSClient):
    """阿里云实时语音合成客户端：``speak()`` → 音频 id（``openai``）或系统语音（``system``）。

    只在 ``tts_mode == "aliyun"`` 时由 ``main.py`` 装配（见 ``is_aliyun_mode``）。
    ``http_client`` 参数不参与本实现（全程走 WebSocket），但保留父类签名的兼容性。
    """

    def __init__(self, cfg, http_client=None):
        super().__init__(cfg, http_client)
        self._ws = None
        self._opened_at = 0.0
        self._last_used = 0.0
        self._task_lock = None              # 惰性构造的 asyncio.Lock（协议：一条连接同时只跑一个任务）
        self._task_loop = None              # 该锁绑定的 loop（换了 loop 就重建，防跨 loop 复用）
        self._warned: set[str] = set()

    # ================= 只读属性 =================

    @property
    def enabled(self) -> bool:
        """``tts_enabled`` 即可（与父类不同：本模式不看 ``tts_base_url``）。

        缺 API Key / 音色时**仍算启用**——让 ``_synthesize`` 返回 ``None`` 走既有的
        「整条降级 ``system``」路径，玩家至少还有系统语音；若在这里判成未启用，
        ``speak()`` 会回 ``none``，结果是**完全没有声音**（本项目最不能接受的静默）。
        """
        return bool(getattr(self.cfg, "tts_enabled", False))

    async def close(self) -> None:
        """关闭 WebSocket（可重复调用）；父类的 HTTP 客户端本模式从未创建，一并收尾。"""
        await self._drop()
        await super().close()

    # ================= 合成 =================

    async def _synthesize(self, chunks: list[str]) -> bytes | None:
        """整段文本走**一个** WS 任务；失败返回 ``None``（父类据此整条降级 ``system``）。

        分块（``chunk_for_synthesis``）在这里只用来拼回整段：阿里云服务端自己按句切分，
        客户端逐块发只会把一句话拆成好几段音频，听感更碎。
        """
        text = "".join(chunks).strip()
        if not text:
            return None
        if not self._api_key():
            self._warn_once("api_key", "未配置 tts_aliyun_api_key（降级为系统语音）")
            return None
        if not self._voice():
            self._warn_once("voice", "未配置 tts_aliyun_voice（降级为系统语音）")
            return None

        async with self._task_gate():
            for attempt in (1, 2):
                try:
                    blob = await asyncio.wait_for(self._task(text), self._timeout())
                    if not blob:
                        return None
                    # 裸 PCM 自己补 WAV 头：引擎对容器的支持因音色而异，PCM 是最稳的那一档
                    return _pcm_to_wav(blob, self._sample_rate()) if self._format() == "pcm" else blob
                except _TaskFailed as e:
                    # 参数 / 音色 / 鉴权类错误：换连接重试没有意义
                    print("[TTS-阿里云] 任务失败（降级为系统语音）：%s" % e)
                    return None
                except Exception as e:      # noqa: BLE001 —— 超时 / 连接断了 / 握手 401 / 协议脏帧
                    # ``websockets`` 自己的保活超时也是 ``TimeoutError``（3.11 起与
                    # ``asyncio.TimeoutError`` 同一个类），所以这里统一按「超时」措辞，
                    # 但**都不重试**：超时重试只会把等待翻倍。
                    timed_out = isinstance(e, asyncio.TimeoutError)
                    detail = str(e) or type(e).__name__
                    print("[TTS-阿里云] 合成%s（第 %d 次，降级为系统语音）：%s"
                          % ("超时" if timed_out else "失败", attempt, detail))
                    await self._drop()      # 状态未知的连接绝不复用
                    if timed_out or attempt == 2:
                        return None
        return None

    async def _task(self, text: str) -> bytes | None:
        """一个完整的合成任务：run-task → 等 task-started → 送文本 → finish → 收音频。"""
        ws = await self._connection()
        task_id = uuid.uuid4().hex
        await self._send(ws, self._run_task_frame(task_id))
        await self._await_event(ws, "task-started", task_id)
        for part in _split_text(text):
            await self._send(ws, self._continue_frame(task_id, part))
        await self._send(ws, self._finish_frame(task_id))
        return await self._collect(ws, task_id)

    async def _await_event(self, ws, want: str, task_id: str) -> None:
        """等 ``want`` 事件；期间收到 ``task-failed`` 立即抛 ``_TaskFailed``。"""
        while True:
            message = await ws.recv()
            event = _event_of(message)
            if event == want:
                return
            if event == "task-failed":
                raise _TaskFailed("task_id=%s %s" % (task_id, _failure_detail(message)))
            # 其余（result-generated / 未来新增事件）忽略：服务端承诺前向兼容

    async def _collect(self, ws, task_id: str) -> bytes | None:
        """收 BINARY 音频帧直到 ``task-finished``；提前断线 / 无音频都按失败处理。"""
        parts: list[bytes] = []
        total = 0
        while True:
            message = await ws.recv()
            if isinstance(message, (bytes, bytearray, memoryview)):
                blob = bytes(message)
                total += len(blob)
                if total > MAX_AUDIO_BYTES:
                    raise RuntimeError("音频超过 %d 字节上限" % MAX_AUDIO_BYTES)
                parts.append(blob)
                continue
            event = _event_of(message)
            if event == "task-finished":
                self._last_used = time.monotonic()
                return b"".join(parts) if parts else None
            if event == "task-failed":
                raise _TaskFailed("task_id=%s %s" % (task_id, _failure_detail(message)))
            # result-generated（句级元数据）/ 未知事件：不影响音频字节的拼接顺序

    async def _send(self, ws, frame: dict) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))

    # ================= 连接管理 =================

    async def _connection(self):
        """取一条可用连接：空闲过久 / 超龄 / 断了都换新的（协议：一条连接一个任务）。"""
        now = time.monotonic()
        stale = (self._ws is None
                 or now - self._last_used > IDLE_RECONNECT_SEC
                 or now - self._opened_at > MAX_CONN_AGE_SEC)
        if stale:
            await self._drop()
            self._ws = await _open_ws(self._url(), self._headers(),
                                      min(self._timeout(), OPEN_TIMEOUT_SEC))
            self._opened_at = now
        self._last_used = now
        return self._ws

    async def _drop(self) -> None:
        """丢掉当前连接（关闭失败只打日志）。断开后 ``_connection`` 会重建。"""
        ws, self._ws = self._ws, None
        self._opened_at = 0.0
        self._last_used = 0.0
        if ws is None:
            return
        try:
            # 断线 / 半死连接上的 close() 可能一直等对端：给个硬上限，别拖住这一回合
            await asyncio.wait_for(ws.close(), 3)
        except Exception as e:              # noqa: BLE001 —— 关不掉不影响下一条（反正要重建）
            print("[TTS-阿里云] 关闭连接出错：%s" % (str(e) or type(e).__name__))

    def _task_gate(self) -> asyncio.Lock:
        """一条连接同时只跑一个任务（协议约束），并发调用在这里排队。

        注意**不能**叫 ``_lock``：父类 ``__init__`` 已经用同名属性放了一个
        ``threading.Lock``（惰性建 HTTP 客户端用），同名方法会被那个属性盖掉。
        """
        loop = asyncio.get_running_loop()
        if self._task_lock is None or self._task_loop is not loop:
            self._task_lock = asyncio.Lock()
            self._task_loop = loop          # 3.10+ 的 Lock 构造时不绑 loop，但绑上更稳
        return self._task_lock

    # ================= 配置读取 =================

    def _api_key(self) -> str:
        return _text_of(self.cfg, "tts_aliyun_api_key").strip()

    def _voice(self) -> str:
        """平台音色 ID（UUID 形式）。留空时退回 ``tts_voice``，方便只改一个字段的用户。"""
        own = _text_of(self.cfg, "tts_aliyun_voice").strip()
        return own or _text_of(self.cfg, "tts_voice").strip()

    def _url(self) -> str:
        """固定地址 + ``requestId``（官方强烈建议携带：握手被拒时是唯一的排障线索）。"""
        base = _text_of(self.cfg, "tts_aliyun_ws_url", WS_URL).strip() or WS_URL
        tag = "requestId=azurego-%s" % uuid.uuid4().hex[:16]
        return base + ("&" if "?" in base else "?") + tag

    def _headers(self) -> dict:
        headers = {"Authorization": "Bearer " + self._api_key()}
        if _truthy(self.cfg, "tts_aliyun_tn", True):
            headers["X-TTS-TN"] = "enable"   # 取值必须精确等于 enable（区分大小写）
        return headers

    def _format(self) -> str:
        """``tts_aliyun_format`` 归一化：不认识的值回落默认（``pcm``）。"""
        fmt = (_text_of(self.cfg, "tts_aliyun_format", DEFAULT_FORMAT).strip().lower()
               or DEFAULT_FORMAT)
        return fmt if fmt in ("mp3", "pcm", "wav", "opus") else DEFAULT_FORMAT

    def _sample_rate(self) -> int:
        """采样率：钳到协议区间；``opus`` 只认 8000/16000/24000/48000。"""
        sample = _int_cfg(self.cfg, "tts_aliyun_sample_rate", DEFAULT_SAMPLE_RATE, 8000, 48000)
        if self._format() == "opus" and sample not in (8000, 16000, 24000, 48000):
            sample = 24000
        return sample

    def _parameters(self) -> dict:
        """run-task 的 ``parameters``：音色 ID + 音频参数（``model`` 已废弃，不传）。"""
        fmt = self._format()
        sample = self._sample_rate()
        return {
            "voice": self._voice(),
            "format": fmt,
            "sample_rate": sample,
            # 语速沿用既有滑块（协议范围 0.5~2.0）；音调走播放层，不在合成侧改
            "rate": round(_float_cfg(self.cfg, "tts_speed", 1.0, 0.5, 2.0), 3),
            "volume": _int_cfg(self.cfg, "tts_aliyun_volume", 50, 0, 100),
        }

    def _timeout(self) -> float:
        t = _float_cfg(self.cfg, "tts_timeout_sec", 30.0, 1.0, 3600.0)
        return t if t > 0 else 30.0

    def _warn_once(self, key: str, message: str) -> None:
        """同一个配置缺失只吵一次：每一句台词都打一行会把控制台刷满。"""
        if key in self._warned:
            return
        self._warned.add(key)
        print("[TTS-阿里云] %s" % message)

    # ================= 帧构造（静态，便于单测逐字段核对） =================

    def _run_task_frame(self, task_id: str) -> dict:
        """``run-task``：``input`` 必须是空对象，文本一律走 ``continue-task``。"""
        return {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"parameters": self._parameters(), "input": {}},
        }

    @staticmethod
    def _continue_frame(task_id: str, text: str) -> dict:
        return {
            "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {"text": text}},
        }

    @staticmethod
    def _finish_frame(task_id: str) -> dict:
        return {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }


def is_aliyun_mode(cfg) -> bool:
    """``tts_mode`` 是否为 ``aliyun``（大小写 / 空白不敏感，与 ``tts._mode`` 同一归一化规则）。"""
    return _text_of(cfg, "tts_mode").strip().lower() == "aliyun"
