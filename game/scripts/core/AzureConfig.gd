class_name AzureConfig
extends RefCounted
## 配置加载/保存：优先读「可执行文件同目录 / config.json」，其次项目内 res://config.json

## 朗读方式（与左侧栏下拉一一对应）：system（Windows 系统语音）/ local（本地 CosyVoice/MeloTTS）/
## openai（云端 OpenAI 兼容 /v1/audio/speech；有本地地址时本地优先，兼容旧配置）/
## aliyun（阿里云 Voice Studio 实时语音合成，WebSocket）。旧配置里的 "cosyvoice" 等未知值一律回落云端 openai
const TTS_MODES := ["system", "local", "openai", "aliyun"]
## 自用版界面主题（只影响客户端，后端不读）：暖白 / 宣纸 / 雾蓝 / 深色。
const UI_THEMES := ["warm", "classic", "mist", "dark"]
## 旧配置里已废弃的本地引擎音色 id → 回落模板默认音色；清单是**唯一副本**，UI（`Main._is_removed_voice()`）直接引用它。
## 自建 CosyVoice 服务仍在用的活音色（azure / azure-fast / melo）**不在此列**，必须原样保留（该服务默认音色就是 azure）。
## 这里用黑名单而非白名单：tts_voice 允许手填任意服务商音色，白名单会把用户自填值冲掉。
const LEGACY_TTS_VOICES := ["中文男", "日语男", "英文男", "韩语女", "azure-soft", "azure-brisk", "azure-calm"]
const TTS_VOICE_DEFAULT := "中文女"

var max_visits := 500
var joseki_teach := true          # 定式导学：识别对局中走出的定式并讲解（右侧栏可关；关掉=行为零变化）
var joseki_rate := 0.5            # 定式提示频率（0~1，与开关同一格）：门控的基础概率；关闭档不动它，重新开启时频率能回来
## 情绪波动（v3.8.0）：小概率不走最优解、在 A/B/C 三条预测线里挑（越偏越少）。
## 默认关；关掉=行为零变化（恒走最优解、不掷骰、不记事件）。开关与参数同在 ⚙ 设置抽屉的一格里。
var emotion_drift := false
var drift_base := 0.03            # 偏航的基础概率（0~1，与开关同一格）；关闭档不动它，重新开启时概率能回来
## `drift_max` 的**只读镜像**（v3.9.0 / R-P3-4 彻底收口）：从 `config.json` 读、默认 `0.08`、脏值 → 默认。
## 前端**不写它、不推它**（`save()` 不落这个键、`/api/config` 也不发），只拿它当 `drift_base` 的
## 夹取上限；手改过的值随 `_raw` 原样保留。这样手改 `drift_max` 时两端读到的上限始终一致。
var drift_max := 0.08
const DRIFT_BASE_DEFAULT := 0.03  # 与 `AzureConfig.drift_base` 的字段默认 / 后端 `config.drift_base` 一致
## `drift_base` 的**缺省上限**（v3.8.0 修复波 / #4）：＝后端 `config.drift_max` 的默认（0.08）。
## 后端 `_apply` 把 `drift_base` 夹到 `min(值, drift_max)`、`drift_max` 另夹到 0.2；客户端过去
## 只夹到 `[0, 1]`，于是手改 `config.json` 写 `drift_base: 0.5` 时**前端留 0.5、后端是 0.08**。
## v3.9.0 起上限改读只读镜像 `drift_max`（见上），这个常量降级为「`drift_max` 缺省时的默认值」。
const DRIFT_BASE_LIMIT := 0.08
## `drift_max` 的默认（与后端 `config.drift_max` 一致）：镜像缺键 / 脏值时用它。
const DRIFT_MAX_DEFAULT := 0.08
## 后端配置层给 `drift_max` 的另一道上限（`config.DRIFT_MAX_LIMIT = 0.2`）：镜像也照此夹，
## 免得手改一个 `5.0` 时前端留 `5.0`、后端却只认 `0.2`。
const DRIFT_MAX_LIMIT := 0.2
## 聊天摘要用 LLM（v3.8.0）：默认关——聊天摘要走**规则式**（纯函数分段 + 模板摘要）、**零额外 LLM 调用**；
## 开启后**只在局末 / 空闲**用 LLM 分段总结（落子回合内绝不加调用）。开关在 ⚙ 设置抽屉里。
var chat_digest_llm := false
## 玩家棋风画像（v3.9.0）：跨局攒「她眼中的你」，手动结束对局时写进后端的 profile.db。
## 默认关；关掉=行为零变化（不注入画像块、不写记录、不攒画像）。开关与只读面板同在 ⚙ 设置抽屉的一格里。
var profile_enabled := false
## 人格化（自用版）：关系阶段 / 情绪余温 / 今天的小心思 / 夜間节奏 / 难度与偏航的半句提示。
## 默认开；关掉＝后端提示词逐字符不变。由 ⚙ 设置抽屉读写。
var persona_enabled := true
## 关系阶段与称呼变化：**默认关**——称呼保持 `prompt_user_name`（默认 朋友）不变。
## 开启后才按局数推进称呼；`persona_address_name` 为空 ⇒ 即便开启也仍用原名。
var persona_relationship_enabled := false
var persona_address_name := ""    # 高阶段称呼名（最多 16 字；留空=仍叫 prompt_user_name）
## 时间感知（v3.9.0）：让她知道现在几点、这一局下了多久、你多久没来、连续第几天。
## 默认关；关掉=行为零变化（不注入时间块）。与画像开关同一格、**一次推一次** `/api/config`。
var time_aware_enabled := false
# —— 前后端架构 ——
# 编排（KataGo / LLM / 定式 / 情绪）全部在后端服务进程，游戏只做渲染与语音，经 REST + WebSocket 通信
var backend_url := "http://127.0.0.1:8000"   # 后端服务地址（WS 地址由其推导）
## 自用版启动便利：游戏启动时自动拉起本地 LLM / TTS / KataGo / 后端（只在 backend_url 为本机时）。
## 由 `dev_local.ps1` 拉起游戏时它已置 `AZURE_SERVICES_MANAGED=1`，不会重复拉起。
var auto_start_services := true
## 窥屏：玩家长时间无操作且切去别的窗口时，看一眼前台画面再搭话。
## 本机默认自动拉起 VLM（GPU 常驻）；填了 vlm_base_url 则视为外部/自管服务，游戏不再碰本地进程。
var vlm_enabled := true
var vlm_base_url := ""
var vlm_auto_start := true
var vlm_port := 8090
var vlm_exe := ""                 # 高级：llama-server.exe 路径（留空自动找）
var vlm_model := ""               # 高级：VLM gguf 路径（留空自动找）
var vlm_mmproj := ""              # 高级：视觉编码器 mmproj 路径（留空自动找）
## 自用版主视觉：warm / classic / mist / dark；默认雾蓝清透。只给客户端 `GufengTheme` 用。
var ui_theme := "mist"
## 桌面宠物形态（默认开）：启动先显示 2D 桌宠，右键「围棋陪练」才展开正式界面。
## 关掉 = 回到旧版「启动即主界面」；`--no-pet` / `--pet` 可临时覆盖本次运行。
var pet_enabled := true
## 桌宠贴图显示高度（px，宽度按原图比例）；右键菜单可调、自动写回。默认 160。
var pet_height := 160
var llm_base_url := "https://api.deepseek.com/v1"   # 云端 LLM（OpenAI 兼容）地址；示例见 README 第四节
var llm_model := "deepseek-chat"
var llm_api_key := ""             # 云端 LLM（OpenAI 兼容）的 API Key；留空则不发送 Authorization
var llm_think_mode := 1           # 思考档位：0=快速（不思考） 1=中等（默认，限定推理预算） 2=强思考（自由推理）
## 说话详略（v3.11.1）：0=精简（点评 1 句 / 聊天 2 句，默认） 1=适中（不加额外约束） 2=详细（2~3 句 / 80 字内）。
## 与旧字段 `tts_limit_len`（布尔）同一件事的扩档：读配置时 `reply_style` 优先，只有旧键时按旧语义迁移
## （true → 0 / false → 1）；写回时两个键一起写，保证旧版本读到的仍是「开 / 关」两档。
var reply_style := 0
var vrm_path := "Azure.vrm"
# —— 提示词自定义（留空则用内置 Azure 人设）——
var prompt_system := ""           # 覆盖系统提示词（人设全文）；留空用内置
var prompt_user_name := "朋友" # 玩家称呼；会替换提示词里的占位名
var prompt_phase_notes: Dictionary = {}  # 覆盖阶段提示，键为阶段名（如 after_player / own_turn）
# —— 云端语音（OpenAI 兼容 /v1/audio/speech；system=Windows 系统语音兜底）——
var tts_enabled := false
var tts_mode := "openai"          # system / local / openai / aliyun；后端按它装配 TTS 客户端
var tts_base_url := "https://api.openai.com/v1"
var tts_model := "tts-1"
var tts_api_key := ""             # 云端 TTS（OpenAI 兼容 /v1/audio/speech）的 API Key；留空则不发送
var tts_voice := TTS_VOICE_DEFAULT
## —— 本地语音（优先；只读镜像）——
## 实际合成在后端：`tts_local_base_url` 非空 ⇒ 先试本地，失败再试云端，最后系统语音。
## 前端只读它做界面提示（左侧栏「实际朗读」），不改、不清空；手改 config.json 或
## `dev_local.ps1` 写入的值随 `_raw` 原样保留。
var tts_local_base_url := ""
var tts_local_model := ""
var tts_local_voice := ""
var tts_speed := 1.0
var tts_pitch := 1.0              # 播放层音调（0.8~1.3，会略带动语速）
var tts_volume := 1.0
## 对局音效（v3.11.5）：落子 / 提子 / 虚着 / 终局；纯前端，默认开、0.8 音量
var sfx_enabled := true
var sfx_volume := 0.8
## 领地热力图（v3.11.5）：默认关，避免常驻遮挡棋形；打开后按后端 ownership 事件刷新
var ownership_enabled := false
## 自适应难度（v3.11.5）：默认关；基线 1 入门 … 5 严苛（局内最多在基线 ±1 档内调整）
var adaptive_difficulty := false
var adaptive_difficulty_base := 3
# —— 阿里云 Voice Studio（tts_mode=aliyun；合成在后端，这里只是配置载体）——
var tts_aliyun_ws_url := ""       # 留空＝官方固定地址（wss://convai-platform.aliyuncs.com/api-ws/v1/inference）
var tts_aliyun_api_key := ""      # Voice Studio 签发的 Key；百炼原生 Key 会被 401
var tts_aliyun_voice := ""        # 平台音色 ID（UUID 形式），与 openai 的 tts_voice 各留各的
var tts_aliyun_format := "pcm"    # 走裸 PCM 由后端补 WAV 头（引擎对 mp3 支持因音色而异）
var tts_aliyun_sample_rate := 24000   # CosyVoice3 系引擎的 mp3 只认 24000（22050 会被 400 拒）
var tts_aliyun_tn := true         # 文本归一化：数字/日期读成口语
# —— 语音加速开关（右侧栏可改）——
var tts_limit_len := true         # 限制 Azure 回复长度（点评 1 句/聊天 2 句）→ 语音等待随字数下降
var tts_sync_speak := true        # 同步说话：Azure 文字等语音就绪后再一起显示（关掉则文字立即显示）
var tts_batch_speak := false      # 整段合成：连发的多条消息合并成一段连续朗读；关=逐句（第一句播完等第二句合成）
var tts_long_chunk := false       # 长句合并：单次 TTS 合成上限 100→160 字（首句更慢；关=100 更快出第一句）
## v4.2.5 桌宠聊天只用预制情绪音：true 时聊天回复不再合成整句（CosyVoice 服务照常拉起，
## 围棋/试听等路径仍可用）；false 时恢复聊天整句 TTS。默认开——聊天出字不再等语音合成。
var pet_chat_tts_prebuilt := true

var _path := ""
var _raw: Dictionary = {}       # 原样保存读到的配置，写回时只覆盖改动项（保留相对路径等写法）
var cli_locked: Dictionary = {} # 被命令行覆盖过的字段（Main 里登记）：save() 不回写，保持「CLI 只影响本次运行」

## 读取配置；`path` 留空时按「可执行文件同目录 / config.json」→「res://config.json」依次查找（测试可传入临时文件）
static func load_config(path := "") -> AzureConfig:
	var c := AzureConfig.new()
	var exe_dir := OS.get_executable_path().get_base_dir()
	var text := ""
	var candidates: Array = [path] if path != "" else [exe_dir.path_join("config.json"), "res://config.json"]
	for p in candidates:
		if FileAccess.file_exists(p):
			var f := FileAccess.open(p, FileAccess.READ)
			if f != null:
				text = f.get_as_text()
				f.close()
				c._path = p
				break
	if text != "":
		var d = JSON.parse_string(text)
		if typeof(d) == TYPE_DICTIONARY:
			c._raw = (d as Dictionary).duplicate()
			c.max_visits = int(d.get("max_visits", c.max_visits))
			c.joseki_teach = bool(d.get("joseki_teach", c.joseki_teach))
			c.joseki_rate = _normalize_joseki_rate(d.get("joseki_rate", c.joseki_rate))
			c.emotion_drift = _normalize_flag(d.get("emotion_drift"), c.emotion_drift)
			c.drift_max = _normalize_drift_max(d.get("drift_max", c.drift_max))
			c.drift_base = _normalize_drift_base(d.get("drift_base", c.drift_base), c.drift_max)
			c.chat_digest_llm = _normalize_flag(d.get("chat_digest_llm"), c.chat_digest_llm)
			c.profile_enabled = _normalize_flag(d.get("profile_enabled"), c.profile_enabled)
			c.persona_enabled = _normalize_flag(d.get("persona_enabled"), c.persona_enabled)
			c.persona_relationship_enabled = _normalize_flag(d.get("persona_relationship_enabled"),
				c.persona_relationship_enabled)
			var addr_name = d.get("persona_address_name", c.persona_address_name)
			if typeof(addr_name) == TYPE_STRING:   # null / 数字 / 对象是脏值：保留默认空串
				c.persona_address_name = (addr_name as String).strip_edges().substr(0, 16)
			c.time_aware_enabled = _normalize_flag(d.get("time_aware_enabled"), c.time_aware_enabled)
			c.backend_url = str(d.get("backend_url", c.backend_url))
			c.auto_start_services = _normalize_flag(d.get("auto_start_services"), c.auto_start_services)
			c.vlm_enabled = _normalize_flag(d.get("vlm_enabled"), c.vlm_enabled)
			c.vlm_base_url = str(d.get("vlm_base_url", c.vlm_base_url)).strip_edges()
			c.vlm_auto_start = _normalize_flag(d.get("vlm_auto_start"), c.vlm_auto_start)
			c.vlm_port = _normalize_port(d.get("vlm_port"), c.vlm_port)
			c.vlm_exe = str(d.get("vlm_exe", c.vlm_exe)).strip_edges()
			c.vlm_model = str(d.get("vlm_model", c.vlm_model)).strip_edges()
			c.vlm_mmproj = str(d.get("vlm_mmproj", c.vlm_mmproj)).strip_edges()
			c.ui_theme = _normalize_ui_theme(str(d.get("ui_theme", c.ui_theme)))
			c.pet_enabled = _normalize_flag(d.get("pet_enabled"), c.pet_enabled)
			c.pet_height = clampi(int(d.get("pet_height", c.pet_height)), 96, 360)
			c.llm_base_url = str(d.get("llm_base_url", c.llm_base_url))
			c.llm_model = str(d.get("llm_model", c.llm_model))
			c.llm_api_key = str(d.get("llm_api_key", c.llm_api_key))
			if d.has("llm_think_mode"):
				c.llm_think_mode = clampi(int(d.get("llm_think_mode", c.llm_think_mode)), 0, 2)
			else:
				c.llm_think_mode = 2 if bool(d.get("llm_think_strong", false)) else 0   # 旧键（bool）迁移
			c.vrm_path = str(d.get("vrm_path", c.vrm_path))
			c.prompt_system = str(d.get("prompt_system", c.prompt_system))
			c.prompt_user_name = str(d.get("prompt_user_name", c.prompt_user_name))
			var pn = d.get("prompt_phase_notes", null)
			if typeof(pn) == TYPE_DICTIONARY:
				c.prompt_phase_notes = (pn as Dictionary).duplicate()
			c.tts_enabled = bool(d.get("tts_enabled", c.tts_enabled))
			c.tts_mode = _normalize_tts_mode(str(d.get("tts_mode", c.tts_mode)))
			c.tts_base_url = str(d.get("tts_base_url", c.tts_base_url))
			c.tts_model = str(d.get("tts_model", c.tts_model))
			c.tts_api_key = str(d.get("tts_api_key", c.tts_api_key))
			c.tts_voice = _normalize_tts_voice(str(d.get("tts_voice", c.tts_voice)))
			c.tts_speed = float(d.get("tts_speed", c.tts_speed))
			c.tts_pitch = float(d.get("tts_pitch", c.tts_pitch))
			c.tts_volume = float(d.get("tts_volume", c.tts_volume))
			c.sfx_enabled = bool(d.get("sfx_enabled", c.sfx_enabled))
			var sv = d.get("sfx_volume", c.sfx_volume)
			if typeof(sv) == TYPE_INT or typeof(sv) == TYPE_FLOAT:
				c.sfx_volume = clampf(float(sv), 0.0, 1.0)
			elif typeof(sv) == TYPE_STRING and (sv as String).strip_edges().is_valid_float():
				c.sfx_volume = clampf((sv as String).to_float(), 0.0, 1.0)
			else:
				c.sfx_volume = 0.8
			c.ownership_enabled = bool(d.get("ownership_enabled", c.ownership_enabled))
			c.adaptive_difficulty = _normalize_flag(d.get("adaptive_difficulty"), c.adaptive_difficulty)
			c.adaptive_difficulty_base = _normalize_adaptive_base(d.get("adaptive_difficulty_base"),
				c.adaptive_difficulty_base)
			c.tts_aliyun_ws_url = str(d.get("tts_aliyun_ws_url", c.tts_aliyun_ws_url))
			c.tts_aliyun_api_key = str(d.get("tts_aliyun_api_key", c.tts_aliyun_api_key))
			c.tts_aliyun_voice = str(d.get("tts_aliyun_voice", c.tts_aliyun_voice))
			c.tts_aliyun_format = str(d.get("tts_aliyun_format", c.tts_aliyun_format))
			c.tts_aliyun_sample_rate = int(d.get("tts_aliyun_sample_rate", c.tts_aliyun_sample_rate))
			c.tts_aliyun_tn = bool(d.get("tts_aliyun_tn", c.tts_aliyun_tn))
			c.tts_limit_len = bool(d.get("tts_limit_len", c.tts_limit_len))
			c.reply_style = _normalize_reply_style(d.get("reply_style", null), c.tts_limit_len)
			c.tts_limit_len = c.reply_style == 0      # 两个字段对齐（save() 也按 reply_style 推导旧键）
			c.tts_sync_speak = bool(d.get("tts_sync_speak", c.tts_sync_speak))
			c.tts_batch_speak = bool(d.get("tts_batch_speak", c.tts_batch_speak))
			c.tts_long_chunk = bool(d.get("tts_long_chunk", c.tts_long_chunk))
			c.pet_chat_tts_prebuilt = bool(d.get("pet_chat_tts_prebuilt", c.pet_chat_tts_prebuilt))
			c.tts_local_base_url = str(d.get("tts_local_base_url", c.tts_local_base_url)).strip_edges()
			c.tts_local_model = str(d.get("tts_local_model", c.tts_local_model)).strip_edges()
			c.tts_local_voice = str(d.get("tts_local_voice", c.tts_local_voice)).strip_edges()
	return c

## 实际朗读路线（只用于界面提示；合成在后端，按 本地 → 云端 → 系统 逐级降级）
func tts_route_label() -> String:
	if tts_mode == "system":
		return "系统语音（Windows）"
	if tts_mode == "aliyun":
		if tts_aliyun_api_key != "" and tts_aliyun_voice != "":
			return "阿里云 Voice Studio（实时流式）"
		return "阿里云 Voice Studio（未配置 Key/音色，将回落系统语音）"
	if tts_mode == "local":
		if tts_local_base_url.strip_edges() == "":
			return "本地语音（未配置 tts_local_base_url，将回落系统语音）"
		var lv := (" · 音色 " + tts_local_voice) if tts_local_voice != "" else ""
		return "本地语音%s%s（失败回落系统语音）" % [tts_local_base_url, lv]
	if tts_local_base_url != "":
		var v := (" · 音色 " + tts_local_voice) if tts_local_voice != "" else ""
		return "云端语音（本地优先）%s%s" % [tts_local_base_url, v]
	if tts_base_url.strip_edges() != "":
		return "云端语音 %s" % tts_base_url
	return "未配置语音地址（不会出声）"

## 旧配置归一化：v2.x 的 "cosyvoice"（本地 CosyVoice 表单接口）已不是合法朗读方式，一律回落 openai，
## 避免「存储值 ≠ 下拉显示值」（下拉是 system / local / openai / aliyun，未知值会让它默认落在 system 上）
static func _normalize_tts_mode(v: String) -> String:
	var m := v.strip_edges().to_lower()
	return m if m in TTS_MODES else "openai"

## 主题归一化：未知值回落雾蓝（默认），避免旧配置 / 手写值让启动落进空主题。
static func _normalize_ui_theme(v: String) -> String:
	var s := v.strip_edges().to_lower()
	return s if s in UI_THEMES else "mist"

static func _normalize_port(v, fallback: int) -> int:
	if typeof(v) == TYPE_INT or typeof(v) == TYPE_FLOAT:
		return clampi(int(v), 1024, 65535)
	if typeof(v) == TYPE_STRING and (v as String).strip_edges().is_valid_int():
		return clampi((v as String).strip_edges().to_int(), 1024, 65535)
	return clampi(fallback, 1024, 65535)

## 旧配置归一化：UI 视为「已移除」的旧音色（男声 / 韩语女 / 旧克隆音色）→ 回落模板默认音色；
## 自建服务仍在用的活音色（azure / azure-fast / melo）与手填 id 一律原样保留
static func _normalize_tts_voice(v: String) -> String:
	var s := v.strip_edges()
	return TTS_VOICE_DEFAULT if s.to_lower() in LEGACY_TTS_VOICES else s

## 定式频率归一化：**与后端 `config._apply` 同口径**——数值（含数字字符串）越界夹到 [0,1]；
## 脏值（非数字 / null / 数组等）退回默认 0.5，不让一个旋钮把启动搞崩。JSON 的 true/false 按
## Python `float(bool)` 的语义折成 1.0/0.0（后端也是这么算的）。
## 数值与字符串**共用 `_clamp_rate` 这一套守卫**：字符串单独一套判据会出现「同一个值经 JSON
## 数字 / 字符串两条路读出来不一样」的坑（R-F-2 #5）。字符串认不出数字就退回默认 0.5。
static func _normalize_joseki_rate(v) -> float:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return _clamp_rate(float(v))
		TYPE_STRING:
			var s := (v as String).strip_edges()
			return _clamp_rate(s.to_float()) if s.is_valid_float() else 0.5
		TYPE_BOOL:
			return 1.0 if v else 0.0
		_:
			return 0.5

## 频率的唯一守卫：NaN ⇒ 默认 0.5（脏值不当成 0 或 1），其余夹到 [0,1]。
## **有意不拦 INF**：`clampf(INF, 0, 1)` = 1.0，与后端 `min(1.0, inf)` 一致（JSON 的 1e999 两端都当 1.0）。
static func _clamp_rate(f: float) -> float:
	return 0.5 if is_nan(f) else clampf(f, 0.0, 1.0)

## 布尔开关归一化（v3.8.0 / 修复轮 1 / Minor #7）：**只认 JSON 的 true / false**。
## GDScript 的 `bool("false")` 是 `true`，直接用它会把字符串脏值读成「开」；数字 / null 同理当脏值，
## 一律退回默认（`fallback`）。本批新加的开关用它；既有开关的读法一个字不动。
static func _normalize_flag(v, fallback: bool) -> bool:
	return v if typeof(v) == TYPE_BOOL else fallback

## 自适应难度基线归一化（v3.11.5）：夹到 1..5；数字字符串也认，脏值退回默认 3。
static func _normalize_adaptive_base(v, fallback := 3) -> int:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return clampi(int(v), 1, 5)
		TYPE_STRING:
			var s := (v as String).strip_edges()
			if s.is_valid_int():
				return clampi(s.to_int(), 1, 5)
			if s.is_valid_float():
				return clampi(int(s.to_float()), 1, 5)
			return fallback
		_:
			return fallback

## 说话详略归一化（v3.11.1）：数值（含数字字符串）夹到 0~2；`null` / 脏值退回「由旧字段迁移」——
## `limit_len_fallback=true` ⇒ 0（精简），否则 1（适中）。旧配置只有 `tts_limit_len`，走的就是这条迁移。
static func _normalize_reply_style(v, limit_len_fallback := false) -> int:
	var legacy := 0 if limit_len_fallback else 1
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return clampi(int(v), 0, 2)
		TYPE_STRING:
			var s := (v as String).strip_edges()
			return clampi(s.to_int(), 0, 2) if s.is_valid_int() else legacy
		_:
			return legacy

## 偏航基础概率归一化：与 `joseki_rate` **同一套守卫**（数值 / 字符串共用判据、NaN ⇒ 默认 0.03、
## 脏值不把启动搞崩），但**夹取上限不同**：夹到 `[0, limit]`（`limit` 缺省＝`DRIFT_MAX_DEFAULT`，
## 生产路径传只读镜像 `drift_max`），让两端读到同一个值（v3.8.0 修复波 / #4 → v3.9.0 镜像化）。
static func _normalize_drift_base(v, limit := DRIFT_MAX_DEFAULT) -> float:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return _clamp_drift_base(float(v), limit)
		TYPE_STRING:
			var s := (v as String).strip_edges()
			return _clamp_drift_base(s.to_float(), limit) if s.is_valid_float() else DRIFT_BASE_DEFAULT
		TYPE_BOOL:
			return 1.0 if v else 0.0
		_:
			return DRIFT_BASE_DEFAULT

## 基础概率的唯一守卫：NaN ⇒ 默认 0.03（脏值不当成 0 或 1），其余夹到 `[0, limit]`。
## ⚠ 上限是 `limit`（只读镜像 `drift_max`，缺省 0.08）而**不是 1.0**：见 `DRIFT_BASE_LIMIT` 的注释。
static func _clamp_drift_base(f: float, limit := DRIFT_MAX_DEFAULT) -> float:
	return DRIFT_BASE_DEFAULT if is_nan(f) else clampf(f, 0.0, limit)

## `drift_max` 只读镜像归一化（v3.9.0）：与 `drift_base` **同一套守卫**（数值 / 字符串共用判据、
## NaN ⇒ 默认 0.08、脏值不把启动搞崩），上限是后端配置层的 `DRIFT_MAX_LIMIT`（0.2）。
static func _normalize_drift_max(v) -> float:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return _clamp_drift_max(float(v))
		TYPE_STRING:
			var s := (v as String).strip_edges()
			return _clamp_drift_max(s.to_float()) if s.is_valid_float() else DRIFT_MAX_DEFAULT
		TYPE_BOOL:
			return 1.0 if v else 0.0
		_:
			return DRIFT_MAX_DEFAULT

## 镜像的唯一守卫：NaN ⇒ 默认 0.08，其余夹到 `[0, DRIFT_MAX_LIMIT]`（与后端 `_apply` 同形）。
static func _clamp_drift_max(f: float) -> float:
	return DRIFT_MAX_DEFAULT if is_nan(f) else clampf(f, 0.0, DRIFT_MAX_LIMIT)

## 写回配置文件（语音设置改动后调用，下次启动仍生效）
func save() -> bool:
	if _path == "":
		_path = "res://config.json"
	var path := _path
	if path.begins_with("res://"):
		path = ProjectSettings.globalize_path(path)
	var d := _raw.duplicate()
	d["_说明"] = "云端模型（LLM / 语音 TTS）与远程后端服务的地址与参数；KataGo 由独立部署的云服务提供（katago_url，部署见 README「KataGo 服务部署」）。打包发布时把本文件放在与可执行文件同一目录即可覆盖。"
	# katago_bin / katago_model / katago_config 是「后端自行拉起 KataGo 进程」时代的旧键（v2.x~v3.0.0）：
	# 计划 01 起 KataGo 由独立部署的云服务提供，后端只连 katago_url / katago_token / katago_timeout_ms，
	# 这三个键已无任何读取方，随旧配置一起清理，免得用户存档里一直留着失效的引擎路径。
	# （katago_url 等新键前端不读，作为未知键随 _raw 原样保留，不会被这里动到。）
	d.erase("katago_bin")
	d.erase("katago_model")
	d.erase("katago_config")
	d["max_visits"] = max_visits
	d["joseki_teach"] = joseki_teach
	d["joseki_rate"] = joseki_rate                 # 与开关同一格：关闭档也写回（重新开启时频率能回来）
	d["emotion_drift"] = emotion_drift
	d["drift_base"] = drift_base                   # 与开关同一格：关闭档也写回（重新开启时概率能回来）
	# `drift_max` 是**只读镜像**（v3.9.0）：前端不写它、也不推它——手改过的值随 `_raw` 原样保留。
	d["chat_digest_llm"] = chat_digest_llm        # 聊天摘要用 LLM（默认关）：单字段开关，写回即生效
	d["profile_enabled"] = profile_enabled        # 玩家棋风画像（默认关）：与下面的时间感知同一格
	d["persona_enabled"] = persona_enabled        # 人格化（默认开）：关系 / 情绪余温 / 小目标 / 夜間节奏
	d["persona_relationship_enabled"] = persona_relationship_enabled   # 关系称呼变化（默认关）
	d["persona_address_name"] = persona_address_name                   # 高阶段称呼名；空=仍叫原名
	d["time_aware_enabled"] = time_aware_enabled  # 时间感知（默认关）：两个字段一起原子推一次 `/api/config`
	d.erase("backend_mode")                       # 只剩「连后端」一条路径：旧配置里的 backend_mode 不再写回
	d["backend_url"] = backend_url
	d["auto_start_services"] = auto_start_services
	d["vlm_enabled"] = vlm_enabled
	d["vlm_base_url"] = vlm_base_url
	d["vlm_auto_start"] = vlm_auto_start
	d["vlm_port"] = vlm_port
	d["vlm_exe"] = vlm_exe
	d["vlm_model"] = vlm_model
	d["vlm_mmproj"] = vlm_mmproj
	d["ui_theme"] = ui_theme
	d["pet_enabled"] = pet_enabled
	d["pet_height"] = pet_height
	d["llm_base_url"] = llm_base_url
	d["llm_model"] = llm_model
	d["llm_api_key"] = llm_api_key
	d["llm_think_mode"] = llm_think_mode
	d.erase("llm_think_strong")                   # 旧 bool 键已被 llm_think_mode 取代，写回时清掉
	d.erase("llm_auto_start")                     # 本地 LLM 服务编排已删除：旧键不再写回，让 v2.x 配置自我清理
	d.erase("llm_port")
	d.erase("llm_exe")
	d.erase("llm_model_path")
	d["vrm_path"] = d.get("vrm_path", vrm_path)
	d.merge({
		"tts_enabled": tts_enabled,
		"tts_mode": tts_mode,
		"tts_base_url": tts_base_url,
		"tts_model": tts_model,
		"tts_api_key": tts_api_key,
		"tts_voice": tts_voice,
		"tts_speed": tts_speed,
		"tts_pitch": tts_pitch,
		"tts_volume": tts_volume,
		"sfx_enabled": sfx_enabled,
		"sfx_volume": sfx_volume,
		"ownership_enabled": ownership_enabled,
		"adaptive_difficulty": adaptive_difficulty,
		"adaptive_difficulty_base": adaptive_difficulty_base,
		"tts_aliyun_ws_url": tts_aliyun_ws_url,
		"tts_aliyun_api_key": tts_aliyun_api_key,
		"tts_aliyun_voice": tts_aliyun_voice,
		"tts_aliyun_format": tts_aliyun_format,
		"tts_aliyun_sample_rate": tts_aliyun_sample_rate,
		"tts_aliyun_tn": tts_aliyun_tn,
		"tts_limit_len": reply_style == 0,   # 旧键跟随新档位写回（旧版本读到的仍是「短回复 开/关」）
		"reply_style": reply_style,          # 说话详略三档（v3.11.1）：0 精简 / 1 适中 / 2 详细
		"tts_sync_speak": tts_sync_speak,
		"tts_batch_speak": tts_batch_speak,
		"tts_long_chunk": tts_long_chunk,
		"pet_chat_tts_prebuilt": pet_chat_tts_prebuilt,
	}, true)
	d.erase("tts_auto_start")                     # 本地语音服务拉起已删除：旧键不再写回
	d.erase("tts_bf16")
	d.erase("tts_short_prompt")
	for k in cli_locked:                      # 命令行覆盖过的字段保持原值：--llm/--model/--key 只影响本次运行
		if _raw.has(k):
			d[k] = _raw[k]
		else:
			d.erase(k)                        # 原文件本来就没这个键：也不要写进去
	var f := FileAccess.open(path, FileAccess.WRITE)
	if f == null:
		return false
	f.store_string(JSON.stringify(d, "  "))
	f.close()
	return true
