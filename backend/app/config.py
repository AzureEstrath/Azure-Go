"""后端配置（对应 ``AzureConfig.gd`` 中与后端相关的字段）。

前后端分离后，config.json 仍只有一份：客户端只读它需要的（渲染 / 语音 TTS / VRM 等），
后端只读引擎与 LLM、提示词自定义部分。这里只保留后端需要的字段，避免两边职责重叠。

KataGo 由独立的云服务提供（见 ``katago_service/``）：这里只存服务地址、鉴权 token 与超时，
不再解析引擎可执行文件 / 模型 / 配置的路径。
"""

import json
import os

# 情绪波动的「小概率」上限（v3.8.0 / 修复轮 1 / Minor #3）：`drift_max` **不许**开到 1.0——
# 它是 `p_drift` 的天花板，而阶梯是 `P(A)=1-p`、`P(B)=2p/3`、`P(C)=p/3`：`p ≥ 0.6` 时
# `P(A) ≤ P(B)`、`p = 1.0` 时她**恒偏航**，正是 spec §5-C 要防的「变成乱下」。
# 0.2 的余量：`P(A) ≥ 0.8 > P(B) ≤ 0.133`，且比默认 `drift_max=0.08` 宽一倍多，够用不越界。
DRIFT_MAX_LIMIT = 0.2


def _clamp01(v, fallback):
    """``[0, 1]`` 的安全取值：缺失 / 脏值 → ``fallback``（不抛，不因一个旋钮把启动搞崩）。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return float(fallback)


class AzureConfig:
    def __init__(self):
        self.katago_url = "http://127.0.0.1:8100"   # KataGo 云服务地址（与 katago_service 默认端口一致）
        self.katago_token = ""                      # Bearer token；留空＝不鉴权（本机回环调试）
        self.katago_timeout_ms = 45000              # 单次 /analyze 超时（代码默认，v3.8.0：30000 → 45000，给深搜那一次留余量；随包 game/config.json 仍写 30000）
        self.max_visits = 500
        self.pv_max_visits = 150                    # 预测线 A/B/C 那一次分析的深度**下限**（只增不减，见 agent_graph._deep_visits）
        self.joseki_teach = True
        self.joseki_rate = 0.5                      # 定式提示频率（0~1）：与开关同一格；门控唯一路径 p=clamp(rate+adj,0,1)
        # —— 情绪波动（v3.8.0）：开启后她小概率走偏优解（默认关，关闭时行为与现状逐项一致）——
        self.emotion_drift = False
        self.drift_base = 0.03                      # 基础偏航概率（平静时的 p_drift）
        self.drift_max = 0.08                       # p_drift 上限
        self.pv_max_drop = 0.05                     # 偏航线与最优线的胜率差上限（超了整条不用）
        # 情绪棋风（v3.13.0）：心情好时在「差不多」的两手里选更锐的一手；默认关，随包配置打开。
        self.emotion_style = False
        # —— 聊天事件摘要（v3.8.0）：默认规则式（零 LLM）；开启后只在局末 / 空闲补做 LLM 分段 ——
        self.chat_digest_llm = False
        # ——玩家棋风画像 + 时间感知（v3.9.0，设计 §6.4）：两个开关**默认都关**（关掉＝现状）——
        #    关着时提示词里的画像块 / 时间块都不注入（与没有这两个功能逐字符同构）；
        #    读入口径与 `emotion_drift` / `chat_digest_llm` 同形：只认 JSON 的 true / false。
        self.profile_enabled = False
        self.time_aware_enabled = False
        # 人格化（关系阶段 / 情绪余温 / 小目标 / 夜間语气）：默认开；关掉＝提示词逐字符不变。
        # 与 `profile_enabled` 解耦：画像记棋风，人格化记「你们一起下过多少盘、她心里还留着什么」。
        self.persona_enabled = True
        # 关系阶段与称呼变化：**默认关**（称呼保持 prompt_user_name，默认玩家不变）。
        # 开启后才按局数推进称呼；`persona_address_name` 为空 ⇒ 即便开启也仍用原名。
        self.persona_relationship_enabled = False
        self.persona_address_name = ""
        # 画像库路径：留空＝<backend>/data/profile.db（由 profile_store.DEFAULT_DB_PATH 决定，
        # 已 gitignore、属运行产物）。与 memory_db_path 同一形状：只存路径、不在这里碰盘。
        self.profile_db_path = ""
        # 棋谱库路径（v3.11.5）：留空＝<backend>/data/games.db；与 profile / memory 一样只存路径。
        self.game_db_path = ""
        # 生命时间线（拟人化 P0）：留空＝<backend>/data/life.db；与其它库一样只存路径、不在这里碰盘。
        # 这条时间线始终装配（不受 memory_enabled 影响）——它是跨重启的“时间事实”，不是可选记忆。
        self.life_db_path = ""
        # 资料箱：桌宠右键「资料与日程」扫描的文件夹；留空＝<repo>/assistant_files/inbox。
        self.assistant_inbox = ""
        # 自动排班 / 到点提醒 / 留档（v4.1.0）：自动排班只管待定与之前自动排过的任务。
        self.auto_plan_enabled = True
        self.auto_plan_days = 7
        # 聊天里由 Azure 自己判断是否排入日程（v4.2.0）；关闭时聊天与旧版逐字一致。
        self.schedule_chat_enabled = True
        self.schedule_reminder_enabled = True
        self.schedule_archive_days = 7
        self.schedule_retention_days = 30
        # v4.1.1 动态排程：每日集中缓冲 / 已承诺窗口 / 机会约束 / EWMA 学习速度。
        self.schedule_buffer_min = 45       # 每天工作窗口末尾预留的弹性时间（分钟）
        self.schedule_lock_hours = 12       # 自动安排在未来多少小时内不再移动
        self.schedule_risk_z = 0.84         # 机会约束：mean + z·σ（0.84 ≈ 80% 完成概率）
        self.schedule_ewma_alpha = 0.35     # 真实耗时 EWMA 的更新速度
        # v4.1.2 日程分级 / 真实手感：课程与约定不移动，任务按 :00/:30 对齐并留切换时间。
        self.schedule_transition_min = 10   # 课程 / 约定前后的通勤与切换留白（分钟）
        self.schedule_align_min = 30        # 普通任务开始时间的对齐粒度（分钟）
        # —— 自适应难度（v3.11.5）：默认关；基线 1 入门 … 5 严苛，局内最多在基线 ±spread 档内调整 ——
        self.adaptive_difficulty = False
        self.adaptive_difficulty_base = 3
        self.adaptive_difficulty_spread = 1
        self.llm_base_url = "https://api.deepseek.com/v1"   # 云端 OpenAI 兼容地址（与 game/config.json 模板一致）
        self.llm_model = "deepseek-chat"
        self.llm_api_key = ""
        self.llm_think_mode = 1          # 默认中等思考（0 快速 / 1 中等 / 2 强思考）
        # 说话详略（v3.11.1）：0=精简（1 句 / 20 字，默认——与旧「短回复」开关的默认 true 一致）
        # 1=适中（不加额外约束）2=详细（2~3 句 / 80 字内）。
        # 与旧字段 `tts_limit_len`（布尔）同一件事的扩档：文件里两个键都在时以 `reply_style` 为准，
        # 只有旧键时按旧语义迁移（true → 0 精简 / false → 1 适中）。
        self.reply_style = 0
        # —— 请求体扩展字段（透传给 OpenAI 兼容服务，如 top_k / reasoning_effort）——
        self.llm_extra_body = {}            # 始终并入
        self.llm_think_extra_body = {}      # 仅思考档位开启（think_mode > 0）时并入
        # —— 发言侧工具（她自己查盘/复算/查定式；只影响怎么说，不影响下哪里）——
        self.tools_enabled = False          # 默认关闭：行为与没有工具时完全一致
        self.max_tool_calls = 2             # 一次发言的工具次数上限（agent 侧还有硬上限）
        # —— 流式输出（默认关闭）：开启后 speak 阶段逐块推 `token` 事件，最终仍以完整 message 收口 ——
        self.stream_tokens = False
        # —— 长期记忆（跨局记得玩家；默认关闭，关闭时不建库、不调 embedding）——
        self.memory_enabled = False
        self.memory_db_path = ""            # 留空＝<backend>/data/memory.db（由 memory_service 决定）
        self.embed_base_url = ""            # 云端 OpenAI 兼容 embedding 地址；三者任一为空则降级为关键词检索
        self.embed_model = ""
        self.embed_api_key = ""
        self.memory_top_k = 3               # 每次召回条数上限
        self.memory_max_inject_chars = 240  # 注入提示词的总字数上限（单条另截到 120 字）
        self.embed_timeout = 10.0           # 单次 embedding 超时（秒）；可收紧，memory_service 用它装配云端客户端
        # —— 云端语音合成（后端合成 + 缓存；Task 2 起）：默认关闭、默认系统语音兜底 ——
        self.tts_enabled = False            # 关闭时零开销：不发请求、不发 speech 事件，文字照常
        self.tts_mode = "system"            # "openai"＝云端合成；"system"＝让前端用系统语音念
        self.tts_base_url = ""              # 云端 OpenAI 兼容地址；留空＝不试云端
        self.tts_model = "tts-1"
        self.tts_api_key = ""               # 留空＝不带 Authorization（本机回环调试）
        self.tts_voice = ""
        self.tts_speed = 1.0
        self.tts_volume = 1.0
        self.tts_timeout_sec = 30           # 云端单次合成超时；超时整条降级
        self.tts_long_chunk = False         # 打开后单块字数上限放宽（templates 里该键已在）
        # 桌宠聊天语音（v4.2.5）：默认只用预制音，不调 CosyVoice 合成整句；
        # 服务照常由启动流程拉起（这里只决定聊天这条链路是否合成）。
        self.pet_chat_tts_prebuilt = True
        # —— 本地语音：tts_mode=local 时只走它（失败回落系统语音）；tts_mode=openai 时非空则先试它，
        #    失败再试云端 tts_base_url，最后系统语音（旧配置兼容）——
        self.tts_local_base_url = ""        # 本地 OpenAI 兼容地址（如 http://127.0.0.1:9880/v1）
        self.tts_local_model = ""           # 留空沿用 tts_model
        self.tts_local_api_key = ""         # 留空沿用 tts_api_key
        self.tts_local_voice = ""           # 留空沿用 tts_voice
        self.tts_local_timeout_sec = 120    # 本地模型冷启动较慢；0 / 脏值 → 沿用 tts_timeout_sec
        # —— 阿里云 Voice Studio 实时语音合成（tts_mode="aliyun"；WebSocket 流式）——
        # 与上面的 openai 三件套互不影响：切换模式只改 tts_mode，两套配置各留各的。
        self.tts_aliyun_ws_url = ""         # 留空＝官方固定地址（见 tts_aliyun.WS_URL）
        self.tts_aliyun_api_key = ""        # Voice Studio 签发的 Key（sk-…）；百炼原生 Key 会被 401
        self.tts_aliyun_voice = ""          # 平台音色 ID（UUID）；留空则退回 tts_voice
        self.tts_aliyun_format = "pcm"      # mp3 / pcm / wav / opus（pcm 自己补 WAV 头，最稳）
        self.tts_aliyun_sample_rate = 24000   # CosyVoice3 系引擎的 mp3 只认 24000（22050 会被 400 拒）
        self.tts_aliyun_volume = 50         # 0~100，合成侧音量（播放音量是客户端的 tts_volume）
        self.tts_aliyun_tn = True           # 文本归一化：把「12345」读成口语，默认开
        # —— 提示词自定义（留空则用内置 Azure 人设）——
        self.prompt_system = ""
        self.prompt_user_name = "玩家"
        self.prompt_phase_notes = {}

        self._path = ""
        self._raw = {}

    @classmethod
    def load(cls, path=None):
        c = cls()
        if path is None:
            env = os.environ.get("AZURE_CONFIG", "")
            if env and os.path.isfile(env):
                path = env
            else:
                for cand in (os.path.join(os.getcwd(), "config.json"),
                             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")):
                    if os.path.isfile(cand):
                        path = cand
                        break
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
            if isinstance(d, dict):
                c._path = path
                c._raw = d
                c._apply(d)
        return c

    def _apply(self, d):
        self.katago_url = str(d.get("katago_url", self.katago_url))
        self.katago_token = str(d.get("katago_token", self.katago_token))
        self.katago_timeout_ms = int(d.get("katago_timeout_ms", self.katago_timeout_ms))
        self.max_visits = int(d.get("max_visits", self.max_visits))
        # 预测线 A/B/C 那一次分析的深度**下限**（修复轮 1 起**只增不减**）：调用点取
        # ``max(pv_max_visits, 引擎的 max_visits)``——随包模板 max_visits=100 时 150 是加深；
        # 代码默认 max_visits=500 时那一次照旧 500（绝不被砍浅：那同一次分析的产物还是
        # 「玩家这手亏没亏」的判据源）。脏值保留 ``__init__`` 的默认（不因一个旋钮把启动搞崩）。
        try:
            self.pv_max_visits = max(1, int(d.get("pv_max_visits", self.pv_max_visits)))
        except (TypeError, ValueError):
            pass
        self.joseki_teach = bool(d.get("joseki_teach", self.joseki_teach))
        try:
            self.joseki_rate = max(0.0, min(1.0, float(d.get("joseki_rate", self.joseki_rate))))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把启动搞崩
            self.joseki_rate = 0.5
        # 情绪波动（v3.8.0）：开关**只认 JSON 的 true / false**——字符串 `"false"` / 数字 / null
        # 一律当脏值 → 默认关（修复轮 1 / Minor #7：`bool("false")` 是 True，会把脏值读成「开」）。
        v = d.get("emotion_drift")
        self.emotion_drift = v if isinstance(v, bool) else self.emotion_drift
        # 三个数值：脏值退回默认；`drift_max` 再夹到「小概率」上限 `DRIFT_MAX_LIMIT`（不是 1.0），
        # `drift_base` 夹到 `[0, drift_max]`（修复轮 1 / Minor #3）。
        self.drift_max = min(_clamp01(d.get("drift_max"), 0.08), DRIFT_MAX_LIMIT)
        self.drift_base = min(_clamp01(d.get("drift_base"), 0.03), self.drift_max)
        self.pv_max_drop = _clamp01(d.get("pv_max_drop"), 0.05)
        v = d.get("emotion_style")
        self.emotion_style = v if isinstance(v, bool) else self.emotion_style
        # 聊天事件摘要（v3.8.0）：开关**只认 JSON 的 true / false**（与 `emotion_drift` 同一口径，
        # 字符串 `"false"` 是脏值 → 保留默认关）。
        v = d.get("chat_digest_llm")
        self.chat_digest_llm = v if isinstance(v, bool) else self.chat_digest_llm
        # 画像 / 时间感知开关（v3.9.0）：同样**只认 JSON 的 true / false**（字符串 `"false"` /
        # 数字 / null 是脏值 → 保留默认关）。
        v = d.get("profile_enabled")
        self.profile_enabled = v if isinstance(v, bool) else self.profile_enabled
        v = d.get("time_aware_enabled")
        self.time_aware_enabled = v if isinstance(v, bool) else self.time_aware_enabled
        v = d.get("persona_enabled")
        self.persona_enabled = v if isinstance(v, bool) else self.persona_enabled
        v = d.get("persona_relationship_enabled")
        self.persona_relationship_enabled = v if isinstance(v, bool) else self.persona_relationship_enabled
        v = d.get("persona_address_name")
        if isinstance(v, str):                  # null / 数字 / 对象都是脏值：保留默认空串
            self.persona_address_name = v.strip()[:16]
        self.profile_db_path = str(d.get("profile_db_path", self.profile_db_path))
        self.game_db_path = str(d.get("game_db_path", self.game_db_path))
        self.life_db_path = str(d.get("life_db_path", self.life_db_path))
        self.assistant_inbox = str(d.get("assistant_inbox", self.assistant_inbox))
        v = d.get("auto_plan_enabled")
        self.auto_plan_enabled = v if isinstance(v, bool) else self.auto_plan_enabled
        try:
            self.auto_plan_days = max(1, min(30, int(d.get("auto_plan_days", self.auto_plan_days))))
        except (TypeError, ValueError):
            pass
        v = d.get("schedule_chat_enabled")
        self.schedule_chat_enabled = v if isinstance(v, bool) else self.schedule_chat_enabled
        v = d.get("schedule_reminder_enabled")
        self.schedule_reminder_enabled = v if isinstance(v, bool) else self.schedule_reminder_enabled
        try:
            self.schedule_archive_days = max(1, min(365, int(
                d.get("schedule_archive_days", self.schedule_archive_days))))
            self.schedule_retention_days = max(1, min(3650, int(
                d.get("schedule_retention_days", self.schedule_retention_days))))
            self.schedule_buffer_min = max(0, min(240, int(
                d.get("schedule_buffer_min", self.schedule_buffer_min))))
            self.schedule_lock_hours = max(0, min(72, int(
                d.get("schedule_lock_hours", self.schedule_lock_hours))))
            self.schedule_risk_z = max(0.0, min(2.0, float(
                d.get("schedule_risk_z", self.schedule_risk_z))))
            self.schedule_ewma_alpha = max(0.05, min(0.95, float(
                d.get("schedule_ewma_alpha", self.schedule_ewma_alpha))))
            self.schedule_transition_min = max(0, min(60, int(
                d.get("schedule_transition_min", self.schedule_transition_min))))
            self.schedule_align_min = max(1, min(60, int(
                d.get("schedule_align_min", self.schedule_align_min))))
        except (TypeError, ValueError):
            pass
        v = d.get("adaptive_difficulty")        # 只认 JSON true / false（与 profile_enabled 同口径）
        self.adaptive_difficulty = v if isinstance(v, bool) else self.adaptive_difficulty
        try:
            self.adaptive_difficulty_base = max(1, min(5, int(d.get("adaptive_difficulty_base", 3))))
        except (TypeError, ValueError):
            self.adaptive_difficulty_base = 3
        try:
            self.adaptive_difficulty_spread = max(0, min(2, int(d.get("adaptive_difficulty_spread", 1))))
        except (TypeError, ValueError):
            self.adaptive_difficulty_spread = 1
        self.llm_base_url = str(d.get("llm_base_url", self.llm_base_url))
        self.llm_model = str(d.get("llm_model", self.llm_model))
        self.llm_api_key = str(d.get("llm_api_key", self.llm_api_key))
        if "llm_think_mode" in d:
            self.llm_think_mode = max(0, min(2, int(d.get("llm_think_mode", self.llm_think_mode))))
        else:
            self.llm_think_mode = 2 if bool(d.get("llm_think_strong", False)) else 0   # 旧键（bool）迁移
        # 说话详略：`reply_style` 优先（脏值退回默认 0 = 精简）；只有旧键 `tts_limit_len` 时按旧语义迁移。
        if "reply_style" in d:
            try:
                self.reply_style = max(0, min(2, int(float(d.get("reply_style")))))
            except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把后端启动搞崩
                self.reply_style = 0
        elif "tts_limit_len" in d:
            self.reply_style = 0 if bool(d.get("tts_limit_len")) else 1
        else:
            self.reply_style = 0                # 两个键都没有：与客户端旧默认（短回复开）一致
        eb = d.get("llm_extra_body")
        if isinstance(eb, dict):
            self.llm_extra_body = dict(eb)
        tb = d.get("llm_think_extra_body")
        if isinstance(tb, dict):
            self.llm_think_extra_body = dict(tb)
        self.tools_enabled = bool(d.get("tools_enabled", self.tools_enabled))
        self.max_tool_calls = max(0, int(d.get("max_tool_calls", self.max_tool_calls)))
        self.stream_tokens = bool(d.get("stream_tokens", self.stream_tokens))
        self.memory_enabled = bool(d.get("memory_enabled", self.memory_enabled))
        self.memory_db_path = str(d.get("memory_db_path", self.memory_db_path))
        self.embed_base_url = str(d.get("embed_base_url", self.embed_base_url))
        self.embed_model = str(d.get("embed_model", self.embed_model))
        self.embed_api_key = str(d.get("embed_api_key", self.embed_api_key))
        self.memory_top_k = max(0, int(d.get("memory_top_k", self.memory_top_k)))
        self.memory_max_inject_chars = max(0, int(d.get("memory_max_inject_chars", self.memory_max_inject_chars)))
        try:
            self.embed_timeout = max(0.0, float(d.get("embed_timeout", self.embed_timeout)))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把启动搞崩
            self.embed_timeout = 10.0
        self.tts_enabled = bool(d.get("tts_enabled", self.tts_enabled))
        self.tts_mode = str(d.get("tts_mode", self.tts_mode))
        self.tts_base_url = str(d.get("tts_base_url", self.tts_base_url))
        self.tts_model = str(d.get("tts_model", self.tts_model))
        self.tts_api_key = str(d.get("tts_api_key", self.tts_api_key))
        self.tts_voice = str(d.get("tts_voice", self.tts_voice))
        try:
            self.tts_speed = max(0.0, float(d.get("tts_speed", self.tts_speed)))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把后端启动搞崩
            self.tts_speed = 1.0
        try:
            self.tts_volume = max(0.0, float(d.get("tts_volume", self.tts_volume)))
        except (TypeError, ValueError):
            self.tts_volume = 1.0
        try:
            self.tts_timeout_sec = max(1.0, float(d.get("tts_timeout_sec", self.tts_timeout_sec)))
        except (TypeError, ValueError):
            self.tts_timeout_sec = 30.0
        self.tts_long_chunk = bool(d.get("tts_long_chunk", self.tts_long_chunk))
        self.pet_chat_tts_prebuilt = bool(d.get(
            "pet_chat_tts_prebuilt", self.pet_chat_tts_prebuilt))
        self.tts_local_base_url = str(d.get("tts_local_base_url", self.tts_local_base_url))
        self.tts_local_model = str(d.get("tts_local_model", self.tts_local_model))
        self.tts_local_api_key = str(d.get("tts_local_api_key", self.tts_local_api_key))
        self.tts_local_voice = str(d.get("tts_local_voice", self.tts_local_voice))
        try:
            self.tts_local_timeout_sec = max(0.0, float(d.get("tts_local_timeout_sec",
                                                              self.tts_local_timeout_sec)))
        except (TypeError, ValueError):
            self.tts_local_timeout_sec = 120.0
        self.tts_aliyun_ws_url = str(d.get("tts_aliyun_ws_url", self.tts_aliyun_ws_url))
        self.tts_aliyun_api_key = str(d.get("tts_aliyun_api_key", self.tts_aliyun_api_key))
        self.tts_aliyun_voice = str(d.get("tts_aliyun_voice", self.tts_aliyun_voice))
        self.tts_aliyun_format = str(d.get("tts_aliyun_format", self.tts_aliyun_format))
        try:
            self.tts_aliyun_sample_rate = int(d.get("tts_aliyun_sample_rate",
                                                    self.tts_aliyun_sample_rate))
        except (TypeError, ValueError):
            self.tts_aliyun_sample_rate = 24000
        try:
            self.tts_aliyun_volume = int(d.get("tts_aliyun_volume", self.tts_aliyun_volume))
        except (TypeError, ValueError):
            self.tts_aliyun_volume = 50
        self.tts_aliyun_tn = bool(d.get("tts_aliyun_tn", self.tts_aliyun_tn))
        self.prompt_system = str(d.get("prompt_system", self.prompt_system))
        self.prompt_user_name = str(d.get("prompt_user_name", self.prompt_user_name))
        pn = d.get("prompt_phase_notes")
        if isinstance(pn, dict):
            self.prompt_phase_notes = dict(pn)

    def has_katago(self) -> bool:
        """是否**显式配置**了 KataGo 服务地址（判据：``katago_url`` 非空）。

        注意「留空」不等于「不接入」：引擎客户端会把空地址回退到 ``DEFAULT_URL``
        （``http://127.0.0.1:8100``，见 ``engine.py``），装配处照样会构造它并 ``start()``。
        所以本判据只回答「用户有没有写地址」，**不代表** KataGo 相关功能已关闭。
        """
        return bool(self.katago_url)
