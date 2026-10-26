"""FastAPI 入口：REST（命令通道）+ WebSocket（事件通道）。

命令通道（REST）——客户端用这些触发后端动作，与 AzureAgent 的方法一一对应：
    POST /api/move        {x, y}            玩家落子（含点评与她应手）
    POST /api/hint                          方向性提示
    POST /api/analyze                       局面分析
    POST /api/chat        {text}            自由聊天
    POST /api/summarize   {note}            对局总结（局末自动归档到棋谱库，与画像开关无关）
    POST /api/undo        {turn}            俗手回档
    POST /api/reset                         重开一局
    POST /api/config      {allow_pass}      允许虚着开关（另见 limit_len / reply_style / think_mode /
                                            joseki_teach / joseki_rate /
                                            emotion_drift / drift_base / drift_max / pv_max_drop /
                                            chat_digest_llm / tools_enabled /
                                            profile_enabled / time_aware_enabled）——后两项是
                                            v3.11.1 的「说话详略三档」与「思考强度三档」，均即时生效
    POST /api/reveal                        落子宣告语音开始（需要 round-trip 的客户端）
    POST /api/interaction {what}            玩家亲密互动（摸头等）
    POST /api/idle                          闲置搭话
    POST /api/peer        {fg_title,ocr,vlm} 窥屏搭话（客户端截屏/OCR/VLM 后交后端组织台词）
    POST /api/mood/tick                     情绪心跳（向基线回落）
    POST /api/review      {on}              进入 / 退出复盘模式（只读回看；期间落子/聊天/闲置/提示/分析被拒）
    POST /api/review/goto {n}               复盘：把第 n 手（含）之后的**只读**快照推一份 state 事件
    GET  /api/review/notes                  复盘：本局逐手存档（move_notes）+ 亏损手账（losses）
    POST /api/review/full/segment {game_id, from, to}  全盘复盘：分析一段棋谱并让 Azure 点评（分段拉取）
    GET  /api/games                         棋谱库列表（首次访问会塞入内置示例局）
    GET  /api/games/{id}                    一盘棋谱全文（moves / meta / notes / losses）
    GET  /api/games/{id}/sgf                导出 SGF（application/x-go-sgf）
    POST /api/games/import {sgf}            导入 SGF（逐手规则校验，非法可读拒绝）
    DELETE /api/games/{id}                  删除一盘棋谱
    POST /api/tts/speak   {text}            试听/手动合成 → 音频（WAV/MP3，按内容嗅探 MIME；合成不出来 → 409）
    GET  /api/tts/audio/{audio_id}          取一条已合成的音频 → 音频（未命中 → 404）
    POST /api/memory/query  {query, k?}     检索长期记忆（管理面；默认关闭时返回空列表）
    POST /api/memory/write  {tag?, text}    手动写一条长期记忆（关闭 / 空文本时被拒）
    GET  /api/memory/stats                  长期记忆体量（行数 / 归档数 / 向量数 / 库路径）
    POST /api/memory/forget {ids?}/{all}    忘掉指定或全部长期记忆
    GET  /api/profile                       玩家棋风画像（只读：各维值 + 置信度 + 最近 N 局记录）
    GET  /api/state                         当前棋盘权威快照
    GET  /api/health                        引擎/LLM/长期记忆状态

事件通道（WebSocket /ws）——后端把 13 类事件推给客户端，契约见 events.py：
    message / state / info / estimate / ownership / busy / mood / joseki / tool / token / speech / memory
    / difficulty（连接建立时先发一次 hello 同步；
    若距上次对局够久，紧随 hello 之后还会有一条**主动约战提醒**的 message——只触发一次、模板句、零 LLM 调用）。
    `token` 是可选流式增量（`stream_tokens` 开启时才有，默认关闭；最终内容以 `message` 为准）。
    `speech` 是每条台词的语音（`mode` 为 `openai` 时按 `id` 拉上面那条音频端点；`system` 时用 `text`，
    交给 Windows 系统语音；语音关闭时根本不发），与对应 `message` 同 `turn_id`、`seq` 递增配对。
    `memory` 由记忆管理端点推送，是元数据（不是台词），前端只记日志。

REST 应答统一返回 {"ok": bool, "state": <棋盘快照>}：命令是否被接受由 ok/busy 判断，
台词等内容一律经由 WS 事件送达（音画同步由客户端本地完成）。
"""

from contextlib import asynccontextmanager

import asyncio
import sys
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from . import config as config_mod
from . import coords, events, persona, prompts, sgf
from . import full_review
from .agent import AzureAgent
from .engine import KataGoEngine
from .game_store import GameStore
from .joseki_progress import JosekiProgress
from .llm import LLMClient
from .memory_service import MemoryService
from .profile_store import ProfileStore
from .tts import TTSClient
from .tts_aliyun import AliyunTTSClient, is_aliyun_mode


def _build_tts(cfg):
    """按 ``tts_mode`` 选客户端：``aliyun`` 走 WebSocket 实时合成，其余沿用原实现。

    两者共享同一套 ``speak() -> {"id","mode"}`` + ``audio(id)`` 契约，所以装配处
    （agent / 两个 TTS 端点 / 前端）完全不需要知道用的是哪家。
    """
    return AliyunTTSClient(cfg) if is_aliyun_mode(cfg) else TTSClient(cfg)


def _audio_media_type(blob: bytes) -> str:
    """按字节嗅探音频 MIME：云端服务常忽略 ``response_format``，不能一律标成 WAV。

    前端 ``TTSClient.play_audio`` 自己按字节解析（WAV/MP3 都认），这里只是让
    ``Content-Type`` 与真实内容一致（阿里云默认回 MP3）。
    """
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return "audio/wav"
    if blob[:3] == b"ID3" or (len(blob) >= 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if blob[:4] == b"OggS":
        return "audio/ogg"
    return "audio/wav"


def _int(v, d=-1):
    """把请求体字段安全转为 int；非法（缺省/非数字）时返回默认值，避免畸形请求打成 500。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _harden_stdout() -> None:
    """把标准输出的**编码错误策略**改成 ``replace``（v3.9.0）：日志打不出某个字符时不再抛异常。

    中文 Windows 的控制台 / 重定向文件是 cp936（``errors="strict"``），而日志里会带 emoji
    （如台词原文的 ``📋``）：``print`` 一旦遇到装不下的字符就抛 ``UnicodeEncodeError``。这些
    ``print`` 遍布在回合的调用链上——``llm.py`` 打服务商错误体、``profile_store.py`` /
    ``joseki_progress.py`` / ``memory.py`` 的 ``print("[…] …%s" % e)``——异常一路冒到公开方法，
    轻则丢一句日志、重则把整回合（连同写对局记录 / 画像融合）打挂。

    **只改 ``errors``、不动编码**：终端该用什么编码还用什么编码，只是把打不出的字符替换成
    ``?``，丢一个字符好过打挂一回合。带守卫：解释器 / 测试里被换过的 ``sys.stdout``（没有
    ``reconfigure``）或已关闭的流一律跳过，绝不让加固本身变成新故障。只在启动路径调用一次。
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except Exception:                       # noqa: BLE001 —— 加固失败不是故障，照常启动
        pass


# 画像面板（``GET /api/profile``）随各维一起带回来的**最近对局记录**条数（v3.9.0，设计 §6.4 / O20）。
# 口径（修复轮 1 / Minor #2 降级，别说成「每维的来源」）：这是**全局最近 N 局**——``profile`` 表
# 没有局 id 列，**逐维**溯源本来就做不到；面板据此只显示「这份画像基于最近哪几局」。
# 只带可读字段，不带 ``moves`` / ``per_move_ms`` 两列（面板不画棋谱，没必要传长 JSON）。
PROFILE_SOURCE_GAMES = 20


def create_app(cfg=None, engine=None, llm=None, agent=None, tts=None, autostart=True, memory=None,
               game_store=None):
    """构造应用；测试可注入 stub 引擎/LLM/agent/memory/tts/game_store（autostart=False 跳过引擎启动）。

    ``memory`` 显式传入时优先于 lifespan 构造的 ``MemoryService``（测试 seam）：
    ``agent`` 分支也不再覆盖它。``tts`` 同理——显式传入的假 TTS 优先；不传时 lifespan
    按 ``tts_mode`` 构造客户端（``aliyun`` → ``AliyunTTSClient``，其余 → ``TTSClient``），
    **两个分支**（自建 agent / 注入 agent）都把它挂到 agent 上（T16）。
    定式族计数（T5）没有 seam、也没有开关：lifespan 无条件构造 ``JosekiProgress()``（构造期不碰
    磁盘），**两个分支**都挂同一个实例（有开关就等于能悄悄关掉教学记忆，设计 §8.4 明确不设）。
    """
    cfg = cfg if cfg is not None else config_mod.AzureConfig.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prompts.configure(cfg)
        # 长期记忆：显式注入优先；默认关闭时零开销（不建库、不调接口）
        mem = memory if memory is not None else MemoryService(cfg)
        app.state.memory = mem
        # 云端语音：显式注入优先（测试用假 TTS）；默认关闭时 speak() 零请求
        tts_client = tts if tts is not None else _build_tts(cfg)
        app.state.tts = tts_client
        # 定式族计数（T5）：**无条件装配**、不设开关（设计 §8.4）；构造期不碰磁盘——
        # 库目录 / 库文件只在真正 bump 时才按需创建（tick 只读时连目录都不建）。
        joseki_progress = JosekiProgress()
        app.state.joseki_progress = joseki_progress
        # 玩家棋风画像（v3.9.0）：**无条件装配**——构造期不碰磁盘、库按需懒建 ⇒ 关着时零开销；
        # 与 `memory.db` **解耦**（不读 `memory_enabled`，设计 §五 I）。**写入只在 `summarize()`
        # 且 `profile_enabled=true` 时发生**；`GET /api/profile` 始终可读（关着 / 无画像 ⇒ 空结果）。
        profile_store = ProfileStore(cfg.profile_db_path)
        app.state.profile_store = profile_store
        # 棋谱库（v3.11.5）：无开关、独立文件；构造期不碰磁盘（首列 / 首次导入 / 局末归档才建库）。
        # `autostart=False` 是测试装配：**未显式注入就不落真实库**，免得全量门卫往 data/games.db 写对局；
        # 显式注入（测试自己的临时库）照常使用。
        gstore = game_store if game_store is not None else (
            GameStore(cfg.game_db_path) if autostart else None)
        app.state.game_store = gstore
        if agent is None:
            eng = engine if engine is not None else KataGoEngine(cfg.katago_url, cfg.katago_token, cfg.max_visits, cfg.katago_timeout_ms)
            cli = llm if llm is not None else LLMClient(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key,
                                                         think_mode=cfg.llm_think_mode,
                                                         base_extra_body=cfg.llm_extra_body,
                                                         think_extra_body=cfg.llm_think_extra_body)
            app.state.agent = AzureAgent(eng, cli, events.EventBus(), joseki_teach=cfg.joseki_teach,
                                         joseki_rate=cfg.joseki_rate,
                                         tools_enabled=cfg.tools_enabled, max_tool_calls=cfg.max_tool_calls,
                                         stream_tokens=cfg.stream_tokens,
                                         pv_max_visits=cfg.pv_max_visits,
                                         emotion_drift=cfg.emotion_drift,
                                         drift_base=cfg.drift_base, drift_max=cfg.drift_max,
                                         pv_max_drop=cfg.pv_max_drop,
                                         emotion_style=cfg.emotion_style,
                                         chat_digest_llm=cfg.chat_digest_llm,
                                         profile_enabled=cfg.profile_enabled,
                                         time_aware_enabled=cfg.time_aware_enabled,
                                         persona_enabled=(cfg.persona_enabled and autostart),
                                         persona_relationship_enabled=cfg.persona_relationship_enabled,
                                         persona_address_name=cfg.persona_address_name,
                                         reply_style=cfg.reply_style,
                                         memory=mem, tts=tts_client,
                                         joseki_progress=joseki_progress)
            app.state.agent.profile_store = profile_store   # v3.9.0：画像库无条件接上（懒建库、零开销）
            app.state.agent.game_store = gstore             # v3.11.5：棋谱库同样无条件接上（懒建库）
            app.state.agent.player_name = cfg.prompt_user_name
            _adaptive = getattr(app.state.agent, "configure_adaptive", None)
            if callable(_adaptive):                     # v3.11.5：默认关；关着＝落点与现状一致
                _adaptive(cfg.adaptive_difficulty, cfg.adaptive_difficulty_base,
                          cfg.adaptive_difficulty_spread)
            if autostart:
                ok = await eng.start()
                if not ok:
                    print("[KataGo] 云服务不可用：" + eng.last_error())
        else:
            app.state.agent = agent
            app.state.agent.memory = mem         # 注入的 agent 也接上同一个实例（显式 memory= 优先）
            app.state.agent.tts = tts_client     # 注入的 agent 同样接上（显式 tts= 优先）
            app.state.agent.joseki_progress = joseki_progress   # T5：注入的 agent 也接上同一个实例
            app.state.agent.profile_store = profile_store       # v3.9.0：画像库同样无条件接上
            # 人格化：测试装配（autostart=False）不读真实 profile.db，也不改提示词；
            # 生产在下面统一 load_persona() 读一次跨局状态。
            app.state.agent.persona_enabled = bool(cfg.persona_enabled and autostart)
            app.state.agent.persona_relationship_enabled = bool(cfg.persona_relationship_enabled)
            app.state.agent.persona_address_name = str(cfg.persona_address_name or "").strip()[:16]
            app.state.agent.player_name = cfg.prompt_user_name
            # v3.9.0：画像 / 时间感知两个开关同样以 cfg 为准（关着＝现状）
            app.state.agent.profile_enabled = bool(cfg.profile_enabled)
            app.state.agent.time_aware_enabled = bool(cfg.time_aware_enabled)
            _adaptive = getattr(app.state.agent, "configure_adaptive", None)
            if callable(_adaptive):                     # v3.11.5：注入 agent 也按配置接通
                _adaptive(cfg.adaptive_difficulty, cfg.adaptive_difficulty_base,
                          cfg.adaptive_difficulty_spread)
        # 人格化：生产装配读一次跨局状态（关系 / 情绪余温 / 互动数）。
        # 测试装配（autostart=False）刻意不读真实 profile.db，保持门卫与提示词逐字符不变。
        if autostart:
            _load_persona = getattr(app.state.agent, "load_persona", None)
            if callable(_load_persona):
                await _load_persona()
        yield
        mem.close()                              # 退出时收尾（可重复调用）
        await tts_client.close()                 # 语音客户端**无条件**关闭（autostart 为假时也要关）
        if autostart:
            await app.state.agent.katago.shutdown()
            await app.state.agent.llm.close()

    app = FastAPI(title="Azure 后端", lifespan=lifespan)

    def a():
        return app.state.agent

    def ok():
        s = a()
        return {"ok": True, "busy": s.busy, "state": s.st.payload()}

    def mem_store():
        """记忆库；未启用 / 未装配时为 ``None``（健康面与管理端点据此退化为空结果）。"""
        return getattr(getattr(app.state, "memory", None), "store", None)

    def memory_rows() -> int:
        """长期记忆行数：未启用 / 库不可用时为 0（健康面绝不上抛）。"""
        store = mem_store()
        if store is None:
            return 0
        try:
            return int(store.stats().get("rows", 0) or 0)
        except Exception:                       # noqa: BLE001
            return 0

    @app.get("/api/health")
    async def health():
        s = a()
        return {
            "ok": True,
            "katago_dead": s.katago.is_dead(),
            "katago_error": s.katago.last_error(),
            # 报的是**配置值**：留空时客户端会回退到 DEFAULT_URL（http://127.0.0.1:8100），
            # 故可能与引擎实例实际连的地址不同；这里不读引擎实例（测试注入的 stub 没有 .url）。
            "katago_url": cfg.katago_url,
            "llm_url": s.llm.base_url,
            "memory_rows": memory_rows(),
        }

    @app.get("/api/state")
    async def get_state():
        return a().st.payload()

    @app.get("/api/profile")
    async def get_profile():
        """玩家棋风画像（**只读**，v3.9.0，设计 §6.4 / O20）：各维值 + 置信度 + **最近 N 局记录**。

        与记忆管理面同一套口径：**画像功能关着 / 未装配库 / 库不可用 ⇒ 空结果，绝不 500**；
        两次库读都走 ``asyncio.to_thread``（同步 SQLite 不得冻住事件循环）。返回形状：

        ``{"ok": True, "enabled": bool, "axes": {axis: {value, confidence, n_games, updated_ts}},
        "games": [{id, ts, move_count, result}]}``

        ``games`` 是**全局最近 ``PROFILE_SOURCE_GAMES`` 局**（面板据此显示「这份画像基于最近哪几局」），
        **不是每维各自的来源**——``profile`` 表没有局 id 列，逐维溯源做不到（修复轮 1 / Minor #2）。
        ``enabled`` 报的是当前开关（关着也照常可读——面板据此提示「打开开关」）。
        """
        s = a()
        store = getattr(s, "profile_store", None)
        axes = {}
        games = []
        if store is not None:
            try:
                axes = dict(await asyncio.to_thread(store.get_profile) or {})
            except Exception as e:              # noqa: BLE001 —— 读不到就当作「无画像」
                print("[Profile] 画像读取失败（返回空结果）：%s" % e)
                axes = {}
            try:
                rows = list(await asyncio.to_thread(store.games, PROFILE_SOURCE_GAMES) or [])
                games = [{"id": _int(r.get("id"), 0), "ts": float(r.get("ts", 0.0) or 0.0),
                          "move_count": _int(r.get("move_count"), 0),
                          "result": str(r.get("result") or "")}
                         for r in rows if isinstance(r, dict)]
            except Exception as e:              # noqa: BLE001 —— 最近 N 局记录读不到不影响各维
                print("[Profile] 最近 N 局记录读取失败（返回空列表）：%s" % e)
                games = []
        return {"ok": True, "enabled": bool(getattr(s, "profile_enabled", False)),
                "axes": axes, "games": games}

    @app.post("/api/move")
    async def move(body: dict):
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        x = _int(body.get("x"), -1)
        y = _int(body.get("y"), -1)
        if not (0 <= x < coords.SIZE and 0 <= y < coords.SIZE):
            return {"ok": False, "busy": s.busy, "state": s.st.payload()}
        # 时间感知的打点**不在这里**（v3.9.0 修复轮 1 / I-1）：坐标在盘内但 ``is_legal`` 不成立的
        # 落子（自杀手 / 打劫回提 / 占用点）同样是「被拒的一手」，打点会污染 ``move_ts_list`` 与
        # ``avg_move_sec`` ⇒ 采集点已移进 ``agent.player_move`` 的合法性判定之后。
        await s.player_move(x, y)
        return ok()

    @app.post("/api/hint")
    async def hint():
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.hint()
        return ok()

    @app.post("/api/analyze")
    async def analyze():
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.analyze_position()
        return ok()

    @app.post("/api/chat")
    async def chat(body: dict):
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.chat(str(body.get("text", "")))
        return ok()

    @app.post("/api/summarize")
    async def summarize(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.summarize(str(body.get("note", "")))
        return ok()

    @app.post("/api/undo")
    async def undo(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        s.undo(_int(body.get("turn"), -1))
        return ok()

    @app.post("/api/reset")
    async def reset(body: dict = None):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        # v3.12.1：从主界面进对局用 ``{"silent": true}``——只清棋局，不添一句「重新来吧」
        s.reset_game(silent=bool((body or {}).get("silent", False)))
        return ok()

    @app.post("/api/config")
    async def set_config(body: dict):
        s = a()
        if "allow_pass" in body:
            s.set_allow_pass(bool(body["allow_pass"]))
        if "limit_len" in body:                 # 旧字段（客户端「短回复」布尔开关）：转发到后端编排
            s.limit_len = bool(body["limit_len"])   # 属性：true → 精简档 / false → 适中档
        if "reply_style" in body:               # 说话详略三档（v3.11.1）：0 精简 / 1 适中 / 2 详细
            try:                                # 脏值不生效（保留原档），不把端点打成 500
                s.reply_style = max(0, min(2, int(float(body["reply_style"]))))
            except (TypeError, ValueError):
                pass
        if "think_mode" in body:                # 思考强度（v3.11.1）：运行时改档，立即生效、不用重启后端
            llm = getattr(s, "llm", None)
            if llm is not None:
                try:
                    mode = max(0, min(2, int(float(body["think_mode"]))))
                except (TypeError, ValueError):
                    mode = None
                if mode is not None:
                    fn = getattr(llm, "set_think_mode", None)
                    if callable(fn):
                        fn(mode)
                    else:                       # 注入的假 LLM / 老实现：只落属性，行为与原来一致
                        llm.think_mode = mode
                    cfg.llm_think_mode = mode   # 进程内 cfg 跟着走（帧内自洽；落盘由客户端负责）
        if "joseki_teach" in body:              # 客户端「定式导学」开关：转发到后端编排
            s.joseki_teach = bool(body["joseki_teach"])
        if "joseki_rate" in body:               # 「定式导学」的频率（0~1，与开关同一格）：门控的基础概率
            try:
                s.joseki_rate = max(0.0, min(1.0, float(body["joseki_rate"])))
            except (TypeError, ValueError):     # 脏值不生效：保留原值，不把端点打成 500
                pass
        if "emotion_drift" in body:             # 「情绪波动」开关：开启后她小概率走偏优解（默认关）
            v = body["emotion_drift"]           # 只认 JSON 的 true / false；字符串 "false" 是脏值（修复轮 1 / Minor #7）
            if isinstance(v, bool):
                s.emotion_drift = v
        # 情绪波动的三个数值：脏值不生效、保留原值（与 joseki_rate 同一口径）。`drift_max` 先夹到
        # 「小概率」上限 `DRIFT_MAX_LIMIT`（**不是 1.0**），`drift_base` 再夹到 `[0, drift_max]`
        # （修复轮 1 / Minor #3：p 太大时阶梯不变量会反转、p = 1 时她恒偏航）。顺序固定：先 max 后 base。
        if "drift_max" in body:
            try:
                s.drift_max = min(max(0.0, min(1.0, float(body["drift_max"]))), config_mod.DRIFT_MAX_LIMIT)
            except (TypeError, ValueError):
                pass
        if "drift_base" in body:
            try:
                s.drift_base = min(max(0.0, min(1.0, float(body["drift_base"]))), s.drift_max)
            except (TypeError, ValueError):
                pass
        if "pv_max_drop" in body:
            try:
                s.pv_max_drop = max(0.0, min(1.0, float(body["pv_max_drop"])))
            except (TypeError, ValueError):
                pass
        if "chat_digest_llm" in body:           # 聊天摘要用 LLM（默认关）：开启后只在局末 / 空闲补做
            v = body["chat_digest_llm"]         # 只认 JSON 的 true / false；字符串 "false" 是脏值
            if isinstance(v, bool):
                s.chat_digest_llm = v
        if "tools_enabled" in body:             # 发言侧工具开关：开启后她才会在提示/落子宣告里查资料
            s.tools_enabled = bool(body["tools_enabled"])
        if "tts_local_voice" in body:           # 本地音色：只改后端内存里的 TTS 配置，不重启、不丢棋局
            v = body.get("tts_local_voice")
            if isinstance(v, str):              # 非字符串是脏值：保留原值
                tts_client = getattr(app.state, "tts", None)
                tts_cfg = getattr(tts_client, "cfg", None)
                if tts_cfg is not None:
                    tts_cfg.tts_local_voice = v.strip()
        if "profile_enabled" in body:           # 「玩家棋风画像」开关（默认关）：关着＝不注入画像块
            v = body["profile_enabled"]         # 只认 JSON 的 true / false；字符串 "false" 是脏值
            if isinstance(v, bool):
                s.profile_enabled = v
        if "time_aware_enabled" in body:        # 「时间感知」开关（默认关）：关着＝不注入时间块
            v = body["time_aware_enabled"]      # 与 `emotion_drift` / `chat_digest_llm` 同一口径
            if isinstance(v, bool):
                s.time_aware_enabled = v
        if "persona_enabled" in body:           # 「人格化」开关（默认开）：关着＝提示词逐字符不变
            v = body["persona_enabled"]
            if isinstance(v, bool):
                s.persona_enabled = v
                if v:                            # 运行时重新打开：补读一次跨局状态（失败按初识）
                    _loader = getattr(s, "load_persona", None)
                    if callable(_loader):
                        await _loader()
        if "persona_relationship_enabled" in body:   # 关系阶段与称呼变化（默认关）
            v = body["persona_relationship_enabled"]
            if isinstance(v, bool):
                s.persona_relationship_enabled = v
        if "persona_address_name" in body:      # 高阶段称呼名（留空＝仍叫 prompt_user_name）
            v = body.get("persona_address_name")
            if isinstance(v, str):              # null / 数字 / 对象是脏值：保留原值
                s.persona_address_name = v.strip()[:16]
        if ("adaptive_difficulty" in body or "adaptive_difficulty_base" in body) \
                and hasattr(s, "adaptive"):
            on = s.adaptive.enabled
            base = s.adaptive.base_cfg
            if "adaptive_difficulty" in body:
                v = body["adaptive_difficulty"]         # 只认 true / false；脏值保留原值
                if isinstance(v, bool):
                    on = v
            if "adaptive_difficulty_base" in body:
                try:
                    base = max(1, min(5, int(float(body["adaptive_difficulty_base"]))))
                except (TypeError, ValueError):
                    pass
            if on != s.adaptive.enabled or base != s.adaptive.base_cfg:
                cfg.adaptive_difficulty = on            # 进程内 cfg 跟着走（落盘由客户端负责）
                cfg.adaptive_difficulty_base = base
                fn = getattr(s, "configure_adaptive", None)
                if callable(fn):
                    fn(on, base, cfg.adaptive_difficulty_spread)
        return ok()

    @app.post("/api/reveal")
    async def reveal():
        a().reveal_move()
        return ok()

    @app.post("/api/interaction")
    async def interaction(body: dict):
        a().player_interaction(str(body.get("what", "")))
        return ok()

    @app.post("/api/idle")
    async def idle():
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.idle_remark()
        return ok()

    @app.post("/api/peer")
    async def peer(body: dict):
        s = a()
        denied = review_gate()                  # 复盘先于 busy（V6）
        if denied is not None:
            return denied
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.peer_remark({
            "fg_title": body.get("fg_title", ""),
            "ocr": body.get("ocr", ""),
            "vlm": body.get("vlm", ""),
        })
        return ok()

    @app.post("/api/mood/tick")
    async def mood_tick():
        a().tick_mood()
        return ok()

    # ================= 复盘模式（v3.7.0） =================
    # 复盘是**后端的模式**（只把前端棋盘设成只读挡不住她出声）：期间她不能开口——落子 / 聊天 /
    # 闲置 / 提示 / 分析一律可读拒绝，情绪心跳与语气词在 agent 侧冻结（V14）；回看只推 `state`
    # 事件（复用既有 payload 形状），不新增事件种类、不新增 LLM 调用。
    # 复盘**可进可出**且退出后回到进入前的状态：`goto` 只重放一份临时盘面，一个字都不改 `st`
    # （V7/V8），也**不碰** `st.finished` 的既有语义（「🏁 结束之后仍可继续落子」不变）。

    def review_gate():
        """复盘门禁（V6）：命中时返回**可读拒绝**，未命中返回 ``None``。

        判定顺序是**复盘先于 busy**：复盘期间本来就不该有在途回合，两个拒绝的形状刻意不同
        （复盘回 ``{"ok": False, "error": "复盘模式中"}``，busy 回 ``{"ok": False, "busy": True, …}``），
        前端据 ``error`` / ``busy`` 区分提示文案。**两种都不得触碰 ``busy``**。
        """
        if bool(getattr(a(), "review", False)):
            return {"ok": False, "error": "复盘模式中"}
        return None

    @app.post("/api/review")
    async def review(body: dict):
        """进入 / 退出复盘模式（本端点**自身不受**复盘门禁，否则进不去也出不来）。"""
        s = a()
        s.set_review(bool(body.get("on", False)))
        return {"ok": True, "on": bool(s.review)}

    @app.post("/api/review/goto")
    async def review_goto(body: dict):
        """回看第 ``n`` 手（含）的**只读**快照：重放临时盘面并推 `state`，**不改 ``st``**（V7）。

        ``n`` 越界夹到 ``[0, len(move_seq)]``（本任务钉住：不报错、不 500）；返回体回夹好的 ``n``，
        供前端知道实际落在第几手。
        """
        s = a()
        payload = s.review_snapshot(_int(body.get("n"), 0))
        s._emit(events.state(payload))
        return {"ok": True, "busy": s.busy, "n": payload["move_count"]}

    @app.get("/api/review/notes")
    async def review_notes():
        """本局逐手存档 + 亏损手账（V13 之后，悔掉的那几手已从两份账上消失）。

        形状＝ ``st`` 上那两份账的**副本**（空文本的 note **不过滤**——存档是原样回放，
        展示侧的取舍属于前端 D）。本端点同样不被复盘门禁挡（复盘期间正是要读它）。
        """
        s = a()
        return {"ok": True,
                "notes": [dict(n) for n in s.st.move_notes if isinstance(n, dict)],
                "losses": [dict(l) for l in s.st.losses if isinstance(l, dict)]}

    @app.post("/api/review/intro")
    async def review_intro():
        """普通复盘的开场白（**纯本地、零 LLM / 零 TTS**）。

        前端在 ``POST /api/review {"on": true}`` **之前**调用，拿到文字后在聊天区显示
        （只显示、不发声，保持「复盘期间静音」的既有约定）。文案只依据本局真实手数与
        亏损手账，不编造棋形。
        """
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        if bool(getattr(s, "review", False)):
            return {"ok": False, "error": "复盘模式中"}
        text = persona.review_intro(len(s.st.moves), list(s.st.losses or []))
        return {"ok": True, "text": text}

    # ================= 全盘 AI 复盘（v3.11.5，分段拉取） =================
    @app.post("/api/review/full/segment")
    async def review_full_segment(body: dict):
        """分析棋谱库里的一盘棋的 ``[from, to)`` 这一段，并让 Azure 用强思考点评。

        前端**按段拉取**：上一段的文字/语音播完再请求下一段（TTS 不堆积）。本端点不碰当前对局、
        不改 ``busy``；复盘期间（``review=true``）拒绝，避免两套「只读回看」叠在一起。
        """
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        if bool(getattr(s, "review", False)):
            return {"ok": False, "error": "先退出当前复盘，再开全盘复盘"}
        store = game_store_of()
        rec = await asyncio.to_thread(store.get_game, _int(body.get("game_id"), 0)) if store is not None else None
        if not rec:
            return JSONResponse(status_code=404, content={"ok": False, "error": "棋谱不存在"})
        # 上面的 to_thread 是一个 await 窗口：两次并发分段请求可能都通过了开头的 busy 检查。
        # 读库回来后再核一次（这一句与 _set_busy 之间没有 await，事件循环不会切走），
        # 否则第二个请求会与第一个共用 llm.think_mode / busy，跑成两段并行的「复盘」。
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        if bool(getattr(s, "review", False)):
            return {"ok": False, "error": "先退出当前复盘，再开全盘复盘"}
        moves = rec.get("moves") if isinstance(rec.get("moves"), list) else []
        n = len(moves)
        if n <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "这盘棋谱没有落子"})
        start = max(0, min(_int(body.get("from"), 0), n))
        end = max(start, min(_int(body.get("to"), start), n))
        if end <= start:
            return JSONResponse(status_code=400, content={"ok": False, "error": "这一段的起止手数无效"})
        if end - start > full_review.MAX_SPAN:
            end = start + full_review.MAX_SPAN              # 防单次请求把引擎拖太久
        # 这一段的引擎 / LLM / TTS 跑在 busy 里：前端照常「思考中」，且文字会等语音开播再显示。
        # 全盘复盘固定强思考：聊天里的 think_mode 覆盖只加预算，这里再把 llm 档位临时拨到 2
        # （让 `llm_think_extra_body` 这类扩展字段也一起进请求），结束后原样拨回。
        llm = getattr(s, "llm", None)
        prev_think = getattr(llm, "think_mode", 0)
        if hasattr(llm, "set_think_mode"):
            try:
                llm.set_think_mode(2)
            except Exception as e:              # noqa: BLE001 —— 档位切换失败不拖垮复盘
                print("[FullReview] 强思考档位切换失败（继续）：%s" % e)
        owner = s._set_busy(True)
        try:
            result = await full_review.comment_segment(
                s, rec, start, end,
                index=_int(body.get("index"), 0),
                total=max(1, _int(body.get("total"), 1)),
                previous=str(body.get("previous") or ""))
        finally:
            s._clear_busy(owner)
            if hasattr(llm, "set_think_mode"):
                try:
                    llm.set_think_mode(prev_think)
                except Exception:               # noqa: BLE001
                    pass
        return {"ok": True, **result}

    # ================= 棋谱库 + SGF（v3.11.5） =================
    # 只读写独立文件 ``games.db``，不碰当前对局、不碰复盘模式；列表 / 导入 / 导出 / 删除都
    # 不被 busy 门禁挡（它们本来就不改动棋局）。所有故障都回可读错误，绝不把端点打成 500。

    def game_store_of():
        return getattr(app.state, "game_store", None)

    def _with_date(rec: dict) -> dict:
        rec = dict(rec)
        try:
            rec["date"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(rec.get("ts") or 0)))
        except (TypeError, ValueError, OSError):
            rec["date"] = ""
        return rec

    @app.get("/api/games")
    async def games_list():
        """棋谱库列表；**库里一盘都没有时**先塞入内置示例局（首访即时可玩）。"""
        store = game_store_of()
        if store is None:
            return {"ok": True, "games": []}
        try:
            await asyncio.to_thread(store.ensure_demo)
            rows = await asyncio.to_thread(store.list_games)
        except Exception as e:                  # noqa: BLE001 —— 棋谱库故障只丢列表
            print("[Games] 列表读取异常（已忽略）：%s" % e)
            return {"ok": False, "error": "棋谱库读取失败", "games": []}
        return {"ok": True, "games": [_with_date(r) for r in rows if isinstance(r, dict)]}

    @app.get("/api/games/{game_id}")
    async def games_get(game_id: int):
        store = game_store_of()
        rec = await asyncio.to_thread(store.get_game, game_id) if store is not None else None
        if not rec:
            return JSONResponse(status_code=404, content={"ok": False, "error": "棋谱不存在"})
        return {"ok": True, "game": _with_date(rec)}

    @app.get("/api/games/{game_id}/remark")
    async def games_remark(game_id: int):
        """棋谱库某局的「她的回忆」：纯本地短文案（零 LLM、零引擎）。

        只用记录里真实存在的日期 / 手数 / 黑方胜率 / 总结 / 来源，不编造对局内容；
        前端在载入某局后展示在详情页，不打断翻手与全盘复盘。
        """
        store = game_store_of()
        rec = await asyncio.to_thread(store.get_game, game_id) if store is not None else None
        if not rec:
            return JSONResponse(status_code=404, content={"ok": False, "error": "棋谱不存在"})
        return {"ok": True, "text": persona.library_remark(_with_date(rec), time.time())}

    @app.get("/api/games/{game_id}/sgf")
    async def games_sgf(game_id: int):
        store = game_store_of()
        rec = await asyncio.to_thread(store.get_game, game_id) if store is not None else None
        if not rec:
            return JSONResponse(status_code=404, content={"ok": False, "error": "棋谱不存在"})
        text = sgf.to_sgf(rec)
        return Response(content=text, media_type="application/x-go-sgf",
                        headers={"Content-Disposition": 'attachment; filename="azure_game_%d.sgf"' % game_id})

    @app.post("/api/games/import")
    async def games_import(body: dict):
        """导入 SGF：**逐手重放校验**后才入库；坏棋谱回可读 error（400）。"""
        raw = body.get("sgf")
        if not isinstance(raw, str) or raw.strip() == "":
            return JSONResponse(status_code=400, content={"ok": False, "error": "空棋谱"})
        parsed = await asyncio.to_thread(sgf.parse_sgf, raw)
        if not parsed.get("ok"):
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": str(parsed.get("error") or "棋谱解析失败")})
        store = game_store_of()
        if store is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "棋谱库不可用"})
        rec = {
            "ts": time.time(),
            "moves": parsed["moves"],
            "move_count": len(parsed["moves"]),
            "final_black_wr": None,
            "final_score": None,
            "result": parsed.get("result") or "",
            "summary": parsed.get("summary") or "",
            "meta": {
                "black": parsed.get("black") or cfg.prompt_user_name,
                "white": parsed.get("white") or "Azure",
                "source": "import",
                "sgf_date": parsed.get("date") or "",
                "notes": parsed.get("notes") or [],
                "losses": [],
            },
        }
        game_id = await asyncio.to_thread(store.add_game, rec)
        if not game_id:
            return JSONResponse(status_code=503, content={"ok": False, "error": "棋谱写入失败（库不可用）"})
        return {"ok": True, "id": int(game_id)}

    @app.delete("/api/games/{game_id}")
    async def games_delete(game_id: int):
        store = game_store_of()
        ok_del = await asyncio.to_thread(store.delete_game, game_id) if store is not None else False
        if not ok_del:
            return JSONResponse(status_code=404, content={"ok": False, "error": "棋谱不存在"})
        return {"ok": True}

    # ================= 云端语音（计划 05） =================
    # 对局中的台词由 WS 的 `speech` 事件驱动（前端据 id 来拉音频）；这里两个端点是**试听/手动用**的。
    # 任何合成失败都只回可读错误（409 / 404），绝不把端点打成 500、更不影响对局。

    def tts_of():
        """当前 TTS 客户端；未装配时为 ``None``（端点据此退化为可读错误）。

        名字与 lifespan 里的局部变量 ``tts_client`` 区分开，避免两个作用域同名造成误读。
        """
        return getattr(app.state, "tts", None)

    @app.post("/api/tts/speak")
    async def tts_speak(body: dict):
        """试听：``{text}`` → ``audio/wav``；合成不出音频（关闭 / 系统语音 / 空文本）→ 409。"""
        c = tts_of()
        raw = body.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        if c is None or text == "":
            return JSONResponse({"ok": False, "error": "没有可朗读的文本"}, status_code=409)
        try:
            res = await c.speak(text)
        except Exception as e:                  # noqa: BLE001 —— 合成异常只回可读错误
            print("[TTS] 试听合成失败：%s" % e)
            return JSONResponse({"ok": False, "error": "语音合成失败，请稍后再试"}, status_code=409)
        res = res if isinstance(res, dict) else {}
        if str(res.get("mode") or "none") != "openai":
            return JSONResponse({"ok": False, "error": "当前语音方式不产生音频（请在设置里改用云端语音）"},
                                status_code=409)
        try:
            blob = c.audio(str(res.get("id") or ""))
        except Exception as e:                  # noqa: BLE001
            print("[TTS] 试听取音频失败：%s" % e)
            blob = None
        if not blob:
            return JSONResponse({"ok": False, "error": "音频已过期，请重试"}, status_code=409)
        return Response(content=bytes(blob), media_type=_audio_media_type(bytes(blob)))

    @app.get("/api/tts/audio/{audio_id}")
    async def tts_audio(audio_id: str):
        """取一条已合成的音频（``speech`` 事件 ``mode=="openai"`` 时带的 id）；未命中 → 404。"""
        c = tts_of()
        try:
            blob = c.audio(audio_id) if c is not None else None
        except Exception as e:                  # noqa: BLE001 —— 取不到就 404，绝不上抛
            print("[TTS] 取音频失败：%s" % e)
            blob = None
        if not blob:
            return JSONResponse({"ok": False, "error": "音频不存在或已过期"}, status_code=404)
        return Response(content=bytes(blob), media_type=_audio_media_type(bytes(blob)))

    # ================= 长期记忆管理面 =================
    # 默认关闭（memory_enabled=false）时全部退化为可读的空结果：查询面可用、写入被拒。
    # 任何记忆故障（死库 / embedding 报错）都只打日志，绝不打成 500、也不影响对局。

    def mem_k(body: dict) -> int:
        """召回条数：显式 ``k`` 优先（≤0 ＝不召回），否则用配置的 ``memory_top_k``（原始值，0＝不召回）。

        不再把 0 兜底成 3：``memory_top_k=0`` 与出话路径的 ``recall_for`` 同一语义——
        记忆开着但不召回，管理面据此返回空列表。
        """
        raw = body.get("k")
        if raw is None:
            return max(0, int(getattr(cfg, "memory_top_k", 3) or 0))
        return max(0, _int(raw, 0))

    def mem_event(op, tag, text, score=0.0):
        """把一次记忆动作作为元数据推给客户端（不是台词；前端只记一行日志）。"""
        bus = getattr(a(), "bus", None)
        if bus is None:
            return
        try:
            bus.publish(events.memory(op, tag, text, score))
        except Exception as e:                  # noqa: BLE001 —— 推不出去不影响端点结果
            print("[Memory] 事件推送失败（已忽略）：%s" % e)

    @app.post("/api/memory/query")
    async def memory_query(body: dict):
        """检索长期记忆 → ``{"ok": True, "items": [{id,ts,kind,tag,text,score}]}``。

        召回走 ``asyncio.to_thread``：慢 embedding / 大库扫描不得冻住事件循环。
        """
        store = mem_store()
        items = []
        if store is not None:
            try:
                q = str(body.get("query", "") or "")
                items = list(await asyncio.to_thread(store.recall, q, mem_k(body)) or [])
            except Exception as e:              # noqa: BLE001
                print("[Memory] 查询失败（已忽略）：%s" % e)
                items = []
        for it in items:
            if isinstance(it, dict):
                mem_event("recall", it.get("tag", ""), it.get("text", ""), it.get("score", 0.0))
        return {"ok": True, "items": items}

    @app.post("/api/memory/write")
    async def memory_write(body: dict):
        """手动写一条长期记忆 → ``{"ok": bool, "id": int|null}``（关闭 / 空文本被拒）。

        写入（含 embedding 往返）同样走 ``asyncio.to_thread``。
        """
        raw = body.get("text")
        text = str(raw).strip() if isinstance(raw, str) else ""
        tag = str(body.get("tag", "") or "")
        store = mem_store()
        if store is None or text == "":
            return {"ok": False, "id": None}
        try:
            mem_id = await asyncio.to_thread(store.write, text, kind="fact", tag=tag)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 写入失败（已忽略）：%s" % e)
            mem_id = None
        if mem_id is None:
            return {"ok": False, "id": None}
        mem_event("write", tag, text, 0.0)
        return {"ok": True, "id": mem_id}

    @app.get("/api/memory/stats")
    async def memory_stats():
        """长期记忆体量 → ``{"ok": True, "rows", "games", "has_vectors", "db"}``。"""
        store = mem_store()
        st = {}
        if store is not None:
            try:
                st = dict(store.stats() or {})
            except Exception as e:              # noqa: BLE001
                print("[Memory] 统计失败（已忽略）：%s" % e)
                st = {}
        return {"ok": True,
                "rows": _int(st.get("rows"), 0),
                "games": _int(st.get("games"), 0),
                "has_vectors": _int(st.get("has_vectors"), 0),
                "db": str(st.get("db", "") or "")}

    @app.post("/api/memory/forget")
    async def memory_forget(body: dict):
        """忘掉长期记忆 → ``{"ok": True, "removed": int}``（``{ids:[…]}`` 或 ``{all:true}``）。"""
        store = mem_store()
        if store is None:
            return {"ok": True, "removed": 0}
        ids = body.get("ids")
        if isinstance(ids, list):
            ids = [i for i in (_int(x, -1) for x in ids) if i >= 0]
        else:
            ids = None                          # 非列表（含缺省）＝不按 id 删
        try:
            removed = int(store.forget(ids, all=bool(body.get("all", False))) or 0)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 删除失败（已忽略）：%s" % e)
            removed = 0
        return {"ok": True, "removed": removed}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        s = a()
        q = s.bus.subscribe()
        try:
            await sock.send_json(events.hello(s.st.payload(), s.mood_payload(), s.busy))
            # v3.9.0（Q5）：玩家连上时的**主动约战提醒**——只读一次「距上次对局」挑一句模板台词
            # （零 LLM 调用），实例上的标记保证**跨重连不重复**。任何故障都不影响这条连接。
            try:
                await s.greet_with_context()
            except Exception as e:              # noqa: BLE001 —— 提醒失败绝不影响 WS
                print("[WS] 主动约战提醒失败（已忽略）：%s" % e)
            while True:
                event = await q.get()
                await sock.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print("[WS] 连接异常：%s" % e)
        finally:
            s.bus.unsubscribe(q)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    _harden_stdout()                         # v3.9.0：日志编码不再把整回合打挂（见函数注释）
    print("Azure 后端启动...（KataGo 走云服务，LLM 走云端）")
    uvicorn.run(app, host="127.0.0.1", port=8000)
