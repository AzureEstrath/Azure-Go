class_name AzureConfig
extends RefCounted
## 配置加载/保存：优先读「可执行文件同目录 / config.json」，其次项目内 res://config.json

## 朗读方式只有 system / openai 两种（与左侧栏下拉一一对应）：旧配置里的 "cosyvoice" 等未知值一律回落云端 openai
const TTS_MODES := ["system", "openai"]
## 旧配置里已废弃的本地引擎音色 id → 回落模板默认音色；清单是**唯一副本**，UI（`Main._is_removed_voice()`）直接引用它。
## 自建 CosyVoice 服务仍在用的活音色（azure / azure-fast / melo）**不在此列**，必须原样保留（该服务默认音色就是 azure）。
## 这里用黑名单而非白名单：tts_voice 允许手填任意服务商音色，白名单会把用户自填值冲掉。
const LEGACY_TTS_VOICES := ["中文男", "日语男", "英文男", "韩语女", "azure-soft", "azure-brisk", "azure-calm"]
const TTS_VOICE_DEFAULT := "中文女"

var max_visits := 500
var joseki_teach := true          # 定式导学：识别对局中走出的定式并讲解（右侧栏可关；关掉=行为零变化）
# —— 前后端架构 ——
# 编排（KataGo / LLM / 定式 / 情绪）全部在后端服务进程，游戏只做渲染与语音，经 REST + WebSocket 通信
var backend_url := "http://127.0.0.1:8000"   # 后端服务地址（WS 地址由其推导）
var llm_base_url := "https://api.deepseek.com/v1"   # 云端 LLM（OpenAI 兼容）地址；示例见 README 第四节
var llm_model := "deepseek-chat"
var llm_api_key := ""             # 云端 LLM（OpenAI 兼容）的 API Key；留空则不发送 Authorization
var llm_think_mode := 0           # 思考档位：0=快速（不思考） 1=中等（限定推理预算） 2=强思考（自由推理）
var vrm_path := "D:/KataGo/assets/Azure.vrm"
# —— 提示词自定义（留空则用内置 Azure 人设）——
var prompt_system := ""           # 覆盖系统提示词（人设全文）；留空用内置
var prompt_user_name := "Estarth" # 玩家称呼；会替换提示词中的「Estarth」
var prompt_phase_notes: Dictionary = {}  # 覆盖阶段提示，键为阶段名（如 after_player / own_turn）
# —— 云端语音（OpenAI 兼容 /v1/audio/speech；system=Windows 系统语音兜底）——
var tts_enabled := false
var tts_mode := "openai"          # openai（云端 OpenAI 兼容 /v1/audio/speech）/ system（Windows 系统语音兜底）
var tts_base_url := "https://api.openai.com/v1"
var tts_model := "tts-1"
var tts_api_key := ""             # 云端 TTS（OpenAI 兼容 /v1/audio/speech）的 API Key；留空则不发送
var tts_voice := TTS_VOICE_DEFAULT
var tts_speed := 1.0
var tts_pitch := 1.0              # 播放层音调（0.8~1.3，会略带动语速）
var tts_volume := 1.0
# —— 语音加速开关（右侧栏可改）——
var tts_limit_len := true         # 限制 Azure 回复长度（点评 1 句/聊天 2 句）→ 语音等待随字数下降
var tts_sync_speak := true        # 同步说话：Azure 文字等语音就绪后再一起显示（关掉则文字立即显示）
var tts_batch_speak := false      # 整段合成：连发的多条消息合并成一段连续朗读；关=逐句（第一句播完等第二句合成）
var tts_long_chunk := false       # 长句合并：单次 TTS 合成上限 100→160 字（首句更慢；关=100 更快出第一句）

var _path := ""
var _raw: Dictionary = {}       # 原样保存读到的配置，写回时只覆盖改动项（保留相对路径等写法）
var cli_locked: Dictionary = {} # 被命令行覆盖过的字段（Main 里登记）：save() 不回写，保持「CLI 只影响本次运行」

static func load_config() -> AzureConfig:
	var c := AzureConfig.new()
	var exe_dir := OS.get_executable_path().get_base_dir()
	var text := ""
	for p in [exe_dir.path_join("config.json"), "res://config.json"]:
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
			c.backend_url = str(d.get("backend_url", c.backend_url))
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
			c.tts_limit_len = bool(d.get("tts_limit_len", c.tts_limit_len))
			c.tts_sync_speak = bool(d.get("tts_sync_speak", c.tts_sync_speak))
			c.tts_batch_speak = bool(d.get("tts_batch_speak", c.tts_batch_speak))
			c.tts_long_chunk = bool(d.get("tts_long_chunk", c.tts_long_chunk))
	return c

## 旧配置归一化：v2.x 的 "cosyvoice"（本地 CosyVoice 表单接口）已不是合法朗读方式，一律回落云端 openai，
## 避免「存储值 ≠ 下拉显示值」（下拉只有 system / openai，未知值会让它默认落在 system 上）
static func _normalize_tts_mode(v: String) -> String:
	var m := v.strip_edges().to_lower()
	return m if m in TTS_MODES else "openai"

## 旧配置归一化：UI 视为「已移除」的旧音色（男声 / 韩语女 / 旧克隆音色）→ 回落模板默认音色；
## 自建服务仍在用的活音色（azure / azure-fast / melo）与手填 id 一律原样保留
static func _normalize_tts_voice(v: String) -> String:
	var s := v.strip_edges()
	return TTS_VOICE_DEFAULT if s.to_lower() in LEGACY_TTS_VOICES else s

## 写回配置文件（语音设置改动后调用，下次启动仍生效）
func save() -> bool:
	if _path == "":
		_path = "res://config.json"
	var path := _path
	if path.begins_with("res://"):
		path = ProjectSettings.globalize_path(path)
	var d := _raw.duplicate()
	d["_说明"] = "云端模型（LLM / 语音 TTS）与远程后端服务的地址与参数。打包发布时把本文件放在与可执行文件同一目录即可覆盖。"
	d.erase("katago_bin")                         # 本地编排已删除：旧配置里的 KataGo 键不再写回（读到时也一并清掉）
	d.erase("katago_model")
	d.erase("katago_config")
	d["max_visits"] = max_visits
	d["joseki_teach"] = joseki_teach
	d.erase("backend_mode")                       # 只剩「连后端」一条路径：旧配置里的 backend_mode 不再写回
	d["backend_url"] = backend_url
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
	d.erase("vlm_base_url")                       # 窥屏 VLM 已删除：旧键不再写回
	d.erase("vlm_auto_start")
	d.erase("vlm_exe")
	d.erase("vlm_model")
	d.erase("vlm_mmproj")
	d.erase("vlm_port")
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
		"tts_limit_len": tts_limit_len,
		"tts_sync_speak": tts_sync_speak,
		"tts_batch_speak": tts_batch_speak,
		"tts_long_chunk": tts_long_chunk,
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
