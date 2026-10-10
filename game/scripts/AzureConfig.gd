class_name AzureConfig
extends RefCounted
## 配置加载/保存：优先读「可执行文件同目录 / config.json」，其次项目内 res://config.json

var katago_bin := "bin/katago/katago.exe"
var katago_model := "models/model-b18c384nbt.bin.gz"   # 现代 b18c384nbt（v1.18 引擎，16 线程下 500 visits ≈ 2s/手）
var katago_config := "bin/katago/default_gtp.cfg"
var max_visits := 500
var llm_base_url := "http://localhost:8080/v1"
var llm_model := "qwen3.5"
var llm_api_key := ""             # 云端 LLM（OpenAI 兼容）的 API Key；留空则不发送 Authorization
var llm_think_strong := false     # 强思考：让思考型模型先推理再给答案（更慢、质量更好）；默认关=快速模式
# —— 本地 LLM 服务（llama-server 常驻，游戏启动时一并拉起；也可指向远程服务）——
var llm_auto_start := true        # base_url 指向本机时，启动游戏自动拉起本地 LLM 服务
var llm_exe := ""                 # llama-server.exe 路径；留空由启动器自动探测
var llm_model_path := ""          # 主模型 gguf 路径；留空由启动器自动探测
var llm_port := 8080              # 本地服务端口（应和 base_url 里的端口一致）
var vrm_path := "D:/KataGo/Azure.vrm"
# —— 提示词自定义（留空则用内置 Azure 人设）——
var prompt_system := ""           # 覆盖系统提示词（人设全文）；留空用内置
var prompt_user_name := "Estarth" # 玩家称呼；会替换提示词中的「Estarth」
var prompt_phase_notes: Dictionary = {}  # 覆盖阶段提示，键见 AzurePrompts.PHASE_NOTE
# —— 本地语音（自建 CosyVoice 3 等 OpenAI 兼容服务）——
var tts_enabled := false
var tts_mode := "system"          # system / openai / cosyvoice
var tts_auto_start := true        # 启动游戏时自动拉起本地语音服务（无窗口）
var tts_base_url := "http://127.0.0.1:9880/v1"
var tts_model := "cosyvoice3"
var tts_api_key := ""             # 云端 TTS（OpenAI 兼容 /v1/audio/speech）的 API Key；留空则不发送
var tts_voice := "中文女"
var tts_speed := 1.0
var tts_pitch := 1.0              # 播放层音调（0.8~1.3，会略带动语速）
var tts_volume := 1.0
# —— 语音加速开关（右侧栏可改）——
var tts_bf16 := true              # CosyVoice LLM 权重降为 bfloat16：约快 20~28%，个别词可能读错
var tts_short_prompt := false     # 用 ~2.7s 短参考替代 6.3s：实测耗时基本不变（会变慢/音色略变），默认关
var tts_limit_len := true         # 限制 Azure 回复长度（点评 1 句/聊天 2 句）→ 语音等待随字数下降
var tts_sync_speak := true        # 同步说话：Azure 文字等语音就绪后再一起显示（关掉则文字立即显示）
var tts_batch_speak := false      # 整段合成：连发的多条消息合并成一段连续朗读；关=逐句（第一句播完等第二句合成）
var tts_long_chunk := false       # 长句合并：单次 TTS 合成上限 100→160 字（首句更慢；关=100 更快出第一句）
# —— 窥屏 VLM（本地 llama-server，ROCm GPU 常驻；也可指向远程 OpenAI 兼容服务）——
var vlm_base_url := ""            # 留空=自动使用本地 llama-server（游戏侧负责拉起）；填了则视为外部服务，不碰本地
var vlm_auto_start := true        # 是否允许游戏侧拉起本地 VLM 服务（本机没有 llama-server 时自动跳过）
var vlm_exe := ""                 # llama-server.exe 路径；留空自动探测（D:/KataGo/llamacpp-rocm）
var vlm_model := ""               # VLM gguf；留空自动探测（D:/KataGo/models/Qwen2.5-VL-3B-Instruct-IQ4_NL.gguf）
var vlm_mmproj := ""              # 视觉编码器 gguf；留空自动探测
var vlm_port := 8090

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
			c.katago_bin = str(d.get("katago_bin", c.katago_bin))
			c.katago_model = str(d.get("katago_model", c.katago_model))
			c.katago_config = str(d.get("katago_config", c.katago_config))
			c.max_visits = int(d.get("max_visits", c.max_visits))
			c.llm_base_url = str(d.get("llm_base_url", c.llm_base_url))
			c.llm_model = str(d.get("llm_model", c.llm_model))
			c.llm_api_key = str(d.get("llm_api_key", c.llm_api_key))
			c.llm_think_strong = bool(d.get("llm_think_strong", c.llm_think_strong))
			c.llm_auto_start = bool(d.get("llm_auto_start", c.llm_auto_start))
			c.llm_exe = str(d.get("llm_exe", c.llm_exe))
			c.llm_model_path = str(d.get("llm_model_path", c.llm_model_path))
			c.llm_port = int(d.get("llm_port", c.llm_port))
			c.vrm_path = str(d.get("vrm_path", c.vrm_path))
			c.prompt_system = str(d.get("prompt_system", c.prompt_system))
			c.prompt_user_name = str(d.get("prompt_user_name", c.prompt_user_name))
			var pn = d.get("prompt_phase_notes", null)
			if typeof(pn) == TYPE_DICTIONARY:
				c.prompt_phase_notes = (pn as Dictionary).duplicate()
			c.tts_enabled = bool(d.get("tts_enabled", c.tts_enabled))
			c.tts_mode = str(d.get("tts_mode", c.tts_mode))
			c.tts_auto_start = bool(d.get("tts_auto_start", c.tts_auto_start))
			c.tts_base_url = str(d.get("tts_base_url", c.tts_base_url))
			c.tts_model = str(d.get("tts_model", c.tts_model))
			c.tts_api_key = str(d.get("tts_api_key", c.tts_api_key))
			c.tts_voice = str(d.get("tts_voice", c.tts_voice))
			c.tts_speed = float(d.get("tts_speed", c.tts_speed))
			c.tts_pitch = float(d.get("tts_pitch", c.tts_pitch))
			c.tts_volume = float(d.get("tts_volume", c.tts_volume))
			c.tts_bf16 = bool(d.get("tts_bf16", c.tts_bf16))
			c.tts_short_prompt = bool(d.get("tts_short_prompt", c.tts_short_prompt))
			c.tts_limit_len = bool(d.get("tts_limit_len", c.tts_limit_len))
			c.tts_sync_speak = bool(d.get("tts_sync_speak", c.tts_sync_speak))
			c.tts_batch_speak = bool(d.get("tts_batch_speak", c.tts_batch_speak))
			c.tts_long_chunk = bool(d.get("tts_long_chunk", c.tts_long_chunk))
			c.vlm_base_url = str(d.get("vlm_base_url", c.vlm_base_url))
			c.vlm_auto_start = bool(d.get("vlm_auto_start", c.vlm_auto_start))
			c.vlm_exe = str(d.get("vlm_exe", c.vlm_exe))
			c.vlm_model = str(d.get("vlm_model", c.vlm_model))
			c.vlm_mmproj = str(d.get("vlm_mmproj", c.vlm_mmproj))
			c.vlm_port = int(d.get("vlm_port", c.vlm_port))
	c.katago_bin = _resolve(c.katago_bin, exe_dir)
	c.katago_model = _resolve(c.katago_model, exe_dir)
	c.katago_config = _resolve(c.katago_config, exe_dir)
	if c.vlm_exe != "":
		c.vlm_exe = _resolve(c.vlm_exe, exe_dir)
	if c.vlm_model != "":
		c.vlm_model = _resolve(c.vlm_model, exe_dir)
	if c.vlm_mmproj != "":
		c.vlm_mmproj = _resolve(c.vlm_mmproj, exe_dir)
	if c.llm_exe != "":
		c.llm_exe = _resolve(c.llm_exe, exe_dir)
	if c.llm_model_path != "":
		c.llm_model_path = _resolve(c.llm_model_path, exe_dir)
	return c

## 写回配置文件（语音设置改动后调用，下次启动仍生效）
func save() -> bool:
	if _path == "":
		_path = "res://config.json"
	var path := _path
	if path.begins_with("res://"):
		path = ProjectSettings.globalize_path(path)
	var d := _raw.duplicate()
	d["_说明"] = "KataGo / 本地 LLM / 本地语音 的路径与参数。打包发布时把本文件放在与可执行文件同一目录即可覆盖。"
	d["katago_bin"] = d.get("katago_bin", katago_bin)
	d["katago_model"] = d.get("katago_model", katago_model)
	d["katago_config"] = d.get("katago_config", katago_config)
	d["max_visits"] = max_visits
	d["llm_base_url"] = llm_base_url
	d["llm_model"] = llm_model
	d["llm_api_key"] = llm_api_key
	d["llm_think_strong"] = llm_think_strong
	d["llm_auto_start"] = llm_auto_start
	d["llm_port"] = llm_port
	d["llm_exe"] = d.get("llm_exe", llm_exe)
	d["llm_model_path"] = d.get("llm_model_path", llm_model_path)
	d["vrm_path"] = d.get("vrm_path", vrm_path)
	d["vlm_base_url"] = vlm_base_url
	d["vlm_port"] = vlm_port
	d.merge({
		"tts_enabled": tts_enabled,
		"tts_mode": tts_mode,
		"tts_auto_start": tts_auto_start,
		"tts_base_url": tts_base_url,
		"tts_model": tts_model,
		"tts_voice": tts_voice,
		"tts_speed": tts_speed,
		"tts_pitch": tts_pitch,
		"tts_volume": tts_volume,
		"tts_bf16": tts_bf16,
		"tts_short_prompt": tts_short_prompt,
		"tts_limit_len": tts_limit_len,
		"tts_sync_speak": tts_sync_speak,
		"tts_batch_speak": tts_batch_speak,
		"tts_long_chunk": tts_long_chunk,
	}, true)
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

## 相对路径依次在「可执行文件目录 → 项目目录 → 常见 KataGo 安装目录」下查找
static func _resolve(p: String, exe_dir: String) -> String:
	if p.is_absolute_path():
		return p
	var cands: Array[String] = [
		exe_dir.path_join(p),
		ProjectSettings.globalize_path("res://").path_join(p),
		"D:/KataGo".path_join(p),
	]
	for c in cands:
		if FileAccess.file_exists(c):
			return c
	return cands[0]