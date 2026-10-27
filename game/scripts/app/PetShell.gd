extends Node
## 桌宠主程序（服务与内容接线）。
##
## 产品结构：**桌宠是主程序，围棋是它另开的独立窗口**。
##   · 启动即拉起 dev_local（LLM / TTS / KataGo / 后端）
##   · 右键聊天：走 /api/chat，回复显示在聊天面板、用 TTS 播放
##   · 待机/窥屏：走 /api/idle、/api/peer（共享记忆 + 情绪），只显示气泡 + 预制情绪音
##   · 围棋：另起进程打开 Main（1280×800），桌宠最小化；围棋关闭后恢复桌宠，棋局在 backend 保留
##   · 所有模式共用 backend 的 mood 与 memory；聊天流水 / 事件写入共享 memory.db

const NetClientScript := preload("res://scripts/net/NetClient.gd")
const TTSClientScript := preload("res://scripts/services/TTSClient.gd")
const PetViewScript := preload("res://scripts/app/PetView.gd")

const IDLE_FIRST_SEC := 15.0              # 助手存在感：先开口，但不抢话
const PEEK_FIRST_SEC := 150.0
const AWAY_NOTICE_SEC := 900.0            # 15 分钟：只有真的离开过，才提“你回来了”

var cfg: AzureConfig
var view
var agent
var tts

var _online := false
var _away_sec := -1.0
var _life_greeted := false
var _user_interacted := false
var _day_phase := ""
var _unanswered_proactive := 0
var _mood_v := 0.5
var _mood_c := 0.5
var _mood_a := 0.5
var _mood_name := "平静"
var _mood_pitch := 0.0
var _energy := 0.72
var _boredom := 0.16
var _affinity := 0.35
var _trust := 0.50
var _dominance := 0.50

var _services_started := false
var _game_running := false
var _game_pid := -1

var _last_activity := 0
var _next_idle := 0
var _next_peek := 0
var _idle_muted := false
var _pat_count := 0

var _chat_busy := false
var _chat_started_ms := 0
var _speech_kinds: Array[Dictionary] = []
var _audio_queue: Array[Dictionary] = []
var _audio_busy := false

var _peek_probed := false
var _peek_ok := false
var _peek_script := ""
var _peek_exe := ""
var _peek_busy := false
var _peek_started_ms := 0
var _peek_last_ms := 0
var _peek_out := ""
var _peek_pid := -1
var _peek_ctx_activity := 0
var _vlm_url := ""

var _tone_re := RegEx.new()
var _brace_re := RegEx.new()
var _peek_cooldown_ms := 180000

func _ready() -> void:
	cfg = AzureConfig.load_config()
	GufengTheme.apply_preset(cfg.ui_theme)
	_tone_re.compile("\\[\\[?\\s*tone\\s*[:：]\\s*([a-zA-Z]+)\\s*\\]\\]?")
	_brace_re.compile("\\{([^}]*)\\}")
	_build_ui()
	_build_services()
	_start_services()
	_last_activity = Time.get_ticks_msec()
	_next_idle = _last_activity + _first_idle_delay_ms()
	_next_peek = _last_activity + int(PEEK_FIRST_SEC * 1000.0)
	_peek_last_ms = -_peek_cooldown_ms
	_peek_cooldown_ms = _peek_delay_ms()
	view.show_bubble("嗯，我在。右键可以聊天，也能打开围棋。", 6.0)
	_play_tone("happy")
	get_tree().create_timer(0.2).timeout.connect(_refresh_title)
	set_process(true)

func _refresh_title() -> void:
	if DisplayServer.get_name() != "headless" and not _game_running:
		DisplayServer.window_set_title("Azure · 桌宠")

func _build_ui() -> void:
	view = PetViewScript.new()
	view.pet_height = clampi(int(cfg.pet_height), 96, 360)
	view.theme = GufengTheme.build_theme(GufengTheme.font())
	add_child(view)
	view.interacted.connect(_on_interact)
	view.chat_submitted.connect(_on_chat_submitted)
	view.chat_opened.connect(_on_chat_opened)
	view.chat_closed.connect(_on_chat_closed)
	view.files_opened.connect(_on_files_opened)
	view.files_closed.connect(_on_files_closed)
	view.files_refresh_requested.connect(_load_files)
	view.files_scan_requested.connect(_scan_files)
	view.go_requested.connect(_launch_game)
	view.size_selected.connect(_on_size_selected)
	view.quit_requested.connect(_on_quit)

func _build_services() -> void:
	tts = TTSClientScript.new()
	tts.enabled = cfg.tts_enabled
	tts.mode = cfg.tts_mode
	tts.voice = (cfg.tts_local_voice if cfg.tts_mode == "local" and cfg.tts_local_voice != "" else cfg.tts_voice)
	tts.speed = cfg.tts_speed
	tts.pitch = cfg.tts_pitch
	tts.volume = cfg.tts_volume
	tts.set_suppress_system(true)                 # 桌宠绝不退回 Windows 系统语音
	add_child(tts)
	tts.status.connect(_on_tts_status)

	agent = NetClientScript.new()
	agent.base_url = cfg.backend_url
	agent.message.connect(_on_message)
	agent.mood_changed.connect(_on_mood)
	agent.busy_changed.connect(_on_busy)
	agent.speech_event.connect(_on_speech)
	agent.net_status.connect(_on_net_status)
	add_child(agent)
	agent.setup()
	_apply_agent_settings()

	var mood_timer := Timer.new()
	mood_timer.wait_time = 2.0
	mood_timer.autostart = true
	mood_timer.timeout.connect(func():
		if agent != null:
			agent.tick_mood())
	add_child(mood_timer)

	var life_timer := Timer.new()
	life_timer.wait_time = 60.0
	life_timer.autostart = true
	life_timer.timeout.connect(func():
		if agent != null and _online:
			agent.fetch_life(_on_life))
	add_child(life_timer)

func _apply_agent_settings() -> void:
	if agent == null:
		return
	agent.set_tts_local_voice(cfg.tts_local_voice)
	agent.reply_style = cfg.reply_style
	agent.think_mode = cfg.llm_think_mode
	agent.joseki_teach = cfg.joseki_teach
	agent.joseki_rate = cfg.joseki_rate
	agent.set_drift(cfg.emotion_drift, cfg.drift_base)
	agent.set_digest(cfg.chat_digest_llm)
	agent.set_profile(cfg.profile_enabled, cfg.time_aware_enabled)
	agent.set_persona_all(cfg.persona_enabled, cfg.persona_relationship_enabled, cfg.persona_address_name)
	agent.adaptive_difficulty = cfg.adaptive_difficulty
	agent.adaptive_difficulty_base = cfg.adaptive_difficulty_base

# ================= 服务 =================

func _start_services() -> void:
	if DisplayServer.get_name() == "headless":
		return
	if not cfg.auto_start_services or not _is_local_url(cfg.backend_url):
		return
	if OS.get_environment("AZURE_SERVICES_MANAGED") == "1":
		return                                    # dev_local 已经把服务起好
	var script := _dev_local_script()
	if not FileAccess.file_exists(script):
		return                                    # 导出包 / 外部后端：静默
	var vbs := _repo_root().path_join("tools/run_hidden_dev_local.vbs")
	var pid := -1
	if FileAccess.file_exists(vbs):
		var wscript := OS.get_environment("SystemRoot").path_join("System32/wscript.exe")
		if wscript == "" or not FileAccess.file_exists(wscript):
			wscript = "wscript.exe"
		# -NoWaitTts：TTS 模型在后台加载，别把后端 / 桌宠连接卡在语音预热上。
		pid = OS.create_process(wscript, PackedStringArray([vbs, script, "-NoWaitTts"]))
	if pid <= 0:
		var ps := OS.get_environment("SystemRoot").path_join("System32/WindowsPowerShell/v1.0/powershell.exe")
		if ps == "" or not FileAccess.file_exists(ps):
			ps = "powershell.exe"
		pid = OS.create_process(ps, ["-NoProfile", "-ExecutionPolicy", "Bypass",
			"-WindowStyle", "Hidden", "-File", script, "-NoGame", "-NoWaitTts"])
	if pid > 0:
		_services_started = true
		print("[Pet] 已拉起本地服务链 pid=%d" % pid)
	else:
		print("[Pet] 本地服务启动器没有拉起；等外部后端")

func _repo_root() -> String:
	return ProjectSettings.globalize_path("res://").simplify_path().path_join("..").simplify_path()

func _dev_local_script() -> String:
	return _repo_root().path_join("dev_local.ps1")

static func _is_local_url(url: String) -> bool:
	var u := url.strip_edges().to_lower()
	if u.begins_with("http://"):
		u = u.substr(7)
	elif u.begins_with("https://"):
		u = u.substr(8)
	u = u.get_slice("/", 0)
	if u.begins_with("["):
		var close := u.find("]")
		u = u.substr(1, close - 1) if close > 1 else u
	else:
		u = u.get_slice(":", 0)
	return u in ["127.0.0.1", "localhost", "::1", "0.0.0.0"]

# ================= 后端事件 =================

func _on_net_status(text: String, bad: bool) -> void:
	_online = not bad
	if text.strip_edges() != "":
		print("[Pet] " + text)
	view.set_online(_online)
	if _online:
		_load_chats()
		agent.fetch_mood(func(ok: bool, _c: int, d: Dictionary):
			if ok and not d.is_empty():
				_on_mood(d))
		agent.fetch_life(_on_life)
	else:
		view.set_chat_status("后端连接中…")
	_peek_ctx_activity = _last_activity

func _on_mood(p: Dictionary) -> void:
	_mood_name = str(p.get("name", _mood_name))
	_mood_v = clampf(float(p.get("v", _mood_v)), 0.0, 1.0)
	_mood_c = clampf(float(p.get("c", _mood_c)), 0.0, 1.0)
	_mood_a = clampf(float(p.get("a", _mood_a)), 0.0, 1.0)
	_mood_pitch = clampf(float(p.get("pitch", 0.0)), -0.15, 0.15)
	_energy = clampf(float(p.get("energy", _energy)), 0.0, 1.0)
	_boredom = clampf(float(p.get("boredom", _boredom)), 0.0, 1.0)
	_affinity = clampf(float(p.get("affinity", _affinity)), 0.0, 1.0)
	_trust = clampf(float(p.get("trust", _trust)), 0.0, 1.0)
	_dominance = clampf(float(p.get("dominance", _dominance)), 0.0, 1.0)
	if tts != null:
		tts.set_mood_pitch(_mood_pitch)

## 后端生命时间线：桌宠与围棋共用同一份“上一回见到 Estarth”的事实。
## 只在第一次连上时用；离开超过 15 分钟才主动提，避免每次重连都重复寒暄。
func _on_life(ok: bool, _c: int, d: Dictionary) -> void:
	if not ok or d.is_empty():
		return
	var temporal: Dictionary = d.get("temporal", {})
	_away_sec = float(temporal.get("since_interaction_sec", -1.0))
	_day_phase = str(temporal.get("day_phase", _day_phase))
	var st: Dictionary = d.get("state", {})
	_energy = clampf(float(st.get("energy", _energy)), 0.0, 1.0)
	_boredom = clampf(float(st.get("boredom", _boredom)), 0.0, 1.0)
	_affinity = clampf(float(st.get("affinity", _affinity)), 0.0, 1.0)
	_trust = clampf(float(st.get("trust", _trust)), 0.0, 1.0)
	_dominance = clampf(float(st.get("dominance", _dominance)), 0.0, 1.0)
	if _life_greeted or _game_running:
		return
	_life_greeted = true
	if _user_interacted or _away_sec < AWAY_NOTICE_SEC or view == null or view.is_chat_open():
		return
	var line := away_notice(_away_sec)
	if line == "":
		return
	view.show_bubble(line, 7.0)
	_play_tone(_tone_for_mood())

## 纯函数（门卫可测）：离开时长 → 是否值得提一句 / 说什么；非有限值 / 太短 ⇒ 空串。
static func away_notice(sec: float) -> String:
	if sec != sec or sec < AWAY_NOTICE_SEC or sec == INF:
		return ""
	if sec < 3600.0:
		return "刚刚还在忙呀，你回来啦。"
	if sec < 6.0 * 3600.0:
		return "有一会儿没见你了。"
	if sec < 24.0 * 3600.0:
		return "今天过得怎么样？"
	return "好久不见。上次聊到哪儿，我还记着呢。"

func _on_busy(busy: bool) -> void:
	if not busy and _chat_busy:
		view.set_chat_status("")
	# 回复消息到达时才会把输入框解锁；这里不清 _chat_busy，避免空白回合提前放行

func _on_message(kind: String, text: String) -> void:
	if not kind.begins_with("ai"):
		return
	var parsed := _clean_ai(text)
	if parsed.is_empty():
		return
	var body := str(parsed.get("text", ""))
	var acts: Array = parsed.get("acts", [])
	if not acts.is_empty():
		view.play_actions(acts)
	if body == "":
		return
	var chat_like := kind == "ai" or kind == "ai_comment" or kind == "ai_move"
	if chat_like and _chat_busy:
		# 聊天面板里的一次回复：只进面板，不冒泡；随后到达的 speech 事件对应这条
		view.append_chat("assistant", body)
		_chat_busy = false
		view.set_chat_busy(false)
		_queue_speech("chat")
		return
	if kind == "ai_idle" or kind == "ai_notice":
		if kind != "ai_notice":
			_queue_speech("idle")
		if _idle_muted or view.is_chat_open():
			return
		view.show_bubble(body)
		if kind != "ai_notice":
			_note_proactive_line()
		_play_tone(_tone_for_mood())
		return
	if chat_like:
		# 非聊天请求触发的主动话（问候 / 走神）：桌宠气泡 + 预制语气，不进聊天面板
		if _idle_muted or view.is_chat_open():
			return
		view.show_bubble(body)
		_play_tone(_tone_for_mood())

func _clean_ai(text: String) -> Dictionary:
	var t := str(text)
	var tone := ""
	var m := _tone_re.search(t)
	if m != null:
		tone = m.get_string(1)
		t = _tone_re.sub(t, "", true)
	var acts: Array = []
	for am in _brace_re.search_all(t):
		var act := am.get_string(1).strip_edges()
		if act != "":
			acts.append(act)
	t = _brace_re.sub(t, "", true).strip_edges()
	if t == "" and acts.is_empty():
		return {}
	return {"tone": tone, "text": t, "acts": acts}

# ================= 聊天 =================

func _on_chat_opened() -> void:
	_last_activity = Time.get_ticks_msec()
	_cancel_peek()
	_load_chats()
	view.set_chat_busy(_chat_busy)
	if not _online:
		view.set_chat_status("后端连接中，先把话存这儿…")

func _on_chat_closed() -> void:
	_bump_activity()

# ================= 资料与日程 =================

func _on_files_opened() -> void:
	_last_activity = Time.get_ticks_msec()
	_idle_muted = true
	_cancel_peek()
	_load_files()

func _on_files_closed() -> void:
	_bump_activity()

func _load_files() -> void:
	if view == null:
		return
	if agent == null or not _online:
		view.set_files_status("后端还没连上，稍后再试。")
		return
	view.set_files_status("读取资料箱…")
	agent.fetch_assistant_files(func(ok: bool, _c: int, d: Dictionary):
		if not ok:
			view.set_files_status("资料箱读取失败，稍后再试。")
			return
		var files: Array = d.get("files", [])
		view.set_files_info(str(d.get("folder", "")), files)
		view.set_files_status("资料箱里有 %d 个可处理文件。" % files.size()))
	agent.fetch_schedule(func(ok: bool, _c: int, d: Dictionary):
		if ok:
			view.set_schedule_items(d.get("items", [])))

func _scan_files() -> void:
	if view == null:
		return
	if agent == null or not _online:
		view.set_files_status("后端还没连上，稍后再试。")
		view.set_files_busy(false)
		return
	view.set_files_busy(true)
	_cancel_peek()
	agent.scan_assistant_files(func(ok: bool, _c: int, d: Dictionary):
		view.set_files_busy(false)
		if not ok:
			view.set_files_status(str(d.get("error", "资料处理失败，稍后再试。")))
			return
		view.set_files_info(str(d.get("folder", "")), d.get("files", []))
		var items: Array = d.get("items", [])
		var sched: Array = d.get("schedule", [])
		view.set_schedule_items(sched if not sched.is_empty() else items)
		view.set_files_status("整理出 %d 条日程，已写入共享记忆与上下文。" % items.size())
		_bump_activity())

func _bump_activity() -> void:
	_last_activity = Time.get_ticks_msec()
	_unanswered_proactive = 0
	_next_idle = _last_activity + _idle_delay_ms()

func _on_chat_submitted(text: String) -> void:
	_user_interacted = true
	_bump_activity()
	_idle_muted = true
	_cancel_peek()
	if agent == null or not _online:
		view.set_chat_status("后端还没连上，稍后再发。")
		view.set_chat_busy(false)
		return
	if agent.busy or _chat_busy:
		view.set_chat_status("Azure 还在想上一句…")
		view.set_chat_busy(false)
		return
	if not agent.chat(text, "pet"):
		view.set_chat_status("消息没发出去，稍后再试。")
		view.set_chat_busy(false)
		return
	_chat_busy = true
	_chat_started_ms = Time.get_ticks_msec()
	view.set_chat_busy(true)
	_speech_kinds.clear()

func _load_chats() -> void:
	if agent == null or not _online:
		return
	agent.fetch_chat_history(50, func(ok: bool, _c: int, d: Dictionary):
		if not ok:
			return
		var items: Array = d.get("items", [])
		view.set_chat_history(items)
		if view.is_chat_open():
			view.set_chat_busy(_chat_busy))

func _on_tts_status(text: String) -> void:
	if view == null:
		return
	if text.contains("失败") or text.contains("无法") or text.contains("取回") or text.contains("无响应"):
		view.set_chat_status("语音：" + text)

# ================= 语音 =================

func _queue_speech(kind: String) -> void:
	if tts == null or not cfg.tts_enabled:
		return
	_speech_kinds.append({"kind": kind, "ts": Time.get_ticks_msec()})
	while _speech_kinds.size() > 8:
		_speech_kinds.pop_front()

func _pop_speech_kind() -> String:
	var now := Time.get_ticks_msec()
	while not _speech_kinds.is_empty() and now - int(_speech_kinds[0].get("ts", 0)) > 20000:
		_speech_kinds.pop_front()
	if _speech_kinds.is_empty():
		return ""
	return str(_speech_kinds.pop_front().get("kind", ""))

func _on_speech(payload: Dictionary) -> void:
	if tts == null or not cfg.tts_enabled:
		return
	var pending := _pop_speech_kind()
	# speech 可能先于台词到达：聊天请求在途时按聊天回复处理；
	# 其余（待机 / 窥屏）一律只播预制语气，不播系统或合成人声。
	var kind := "chat" if (_chat_busy or pending == "chat") else pending
	if kind != "chat":
		return
	var mode := str(payload.get("mode", ""))
	if mode == "system":
		# 待机 / 窥屏绝不退回系统语音；只有聊天面板这一条允许（用户显式选了 system 时才走）
		if cfg.tts_mode == "system":
			tts.set_suppress_system(false)
			tts.speak_system(str(payload.get("text", "")), int(payload.get("seq", 0)), "chat")
			tts.set_suppress_system(true)
		return
	if mode != "openai":
		return
	_audio_queue.append({
		"id": str(payload.get("id", "")),
		"seq": int(payload.get("seq", 0)),
	})
	_pump_audio()

func _pump_audio() -> void:
	if _audio_busy or _audio_queue.is_empty() or agent == null:
		return
	_audio_busy = true
	var job: Dictionary = _audio_queue.pop_front()
	var done := func(ok: bool, _c: int, body: PackedByteArray):
		_audio_busy = false
		if ok and not body.is_empty():
			tts.play_audio(body, int(job.get("seq", 0)), "chat")
		_pump_audio()
	agent.fetch_bytes("/api/tts/audio/%s" % str(job.get("id", "")), HTTPClient.METHOD_GET, {},
		done, 60.0)

func _play_tone(kind: String) -> void:
	if tts != null and tts.enabled:
		tts.play_filler(kind, true)

func _tone_for_mood() -> String:
	if _mood_name in ["难过", "失落", "落寞", "忧心", "不安", "委屈", "烦闷", "烦躁", "惊慌"]:
		return "sigh"
	if _mood_name in ["激动", "惊喜", "兴奋", "欢腾", "跃跃欲试"]:
		return "cheer"
	if _mood_name in ["好奇", "兴致", "跳脱"]:
		return "puzzle"
	if _mood_name in ["开心", "欢喜", "愉悦", "惬意", "期待"]:
		return "happy"
	if _mood_a >= 0.68:
		return "surprise"
	return "happy" if _mood_v >= 0.5 else "pat"

# ================= 点击 / 待机 / 窥屏 =================

func _on_interact() -> void:
	_user_interacted = true
	_bump_activity()
	_idle_muted = true
	_cancel_peek()
	view.play_pat_effect()
	_play_tone("pat")
	var line := _reaction_line()
	view.show_bubble(line)
	if agent != null and _online:
		agent.player_interaction("patted")
		_pat_count += 1
		if _pat_count % 5 == 0:
			agent.memory_write("Estarth 在桌面上摸了摸 Azure 的头。", "pet:pat")

func _reaction_line() -> String:
	if _energy <= 0.30:
		var tired := ["唔…有点没力气。", "让我靠一会儿。", "今天慢一点也没关系吧。"]
		return str(tired[randi() % tired.size()])
	if _boredom >= 0.65:
		var bored := ["桌面好安静。", "你是不是很久没理我了。", "我在等你说句话。"]
		return str(bored[randi() % bored.size()])
	var high := ["嗯哼，被你摸到了。", "再摸一下也可以哦。", "今天心情很好。"]
	var low := ["唔…让我靠一会儿。", "摸摸头会好一点。", "陪我坐一会儿吧。"]
	var calm := ["在呢。", "我就在这里。", "桌面上的风，今天很轻。"]
	var pool := high if _mood_v >= 0.62 else (low if _mood_v <= 0.40 else calm)
	return str(pool[randi() % pool.size()])

func _process(_delta: float) -> void:
	var now := Time.get_ticks_msec()
	if _chat_busy and now - _chat_started_ms > 60000:
		_chat_busy = false
		view.set_chat_busy(false)
		view.set_chat_status("这一句等太久了，稍后再试。")
	if _game_running:
		_tick_game(now)
		return
	if _peek_busy:
		_tick_peek(now)
		return
	var hold: bool = view.is_chat_open() or view.is_menu_open() \
		or view.is_files_open() or (agent != null and agent.busy)
	if hold:
		_last_activity = now
	if agent == null:
		return
	if now >= _next_peek and not hold and _can_peek():
		_start_peek(now)
		return
	if now >= _next_idle and not hold:
		_request_idle(now)

func _request_idle(now: int) -> void:
	_idle_muted = false
	if _online and not agent.busy:
		agent.idle_remark("pet")
	else:
		view.show_bubble(_reaction_line())
		_play_tone(_tone_for_mood())
		_note_proactive_line()
	_next_idle = now + _idle_delay_ms()

## 主动开口之后的“读空气”记录：连着说、对方一直没回应时，下一次自然拉长。
func _note_proactive_line() -> void:
	_unanswered_proactive += 1
	_next_idle = Time.get_ticks_msec() + _idle_delay_ms()

func _first_idle_delay_ms() -> int:
	# 初次连接：先让助手露个面，但保留一点随机，不做秒表。
	return int(randf_range(11.0, 18.0) * 1000.0)

func _idle_delay_ms() -> int:
	var sec := humanized_idle_delay_sec(
		_energy, _boredom, _affinity, _trust, _dominance,
		_mood_v, _mood_a, _day_phase, _unanswered_proactive,
		randf(), randf())
	return int(sec * 1000.0)

## 人格化节奏（纯函数，门卫可测）：不是固定间隔，也不是纯随机——
## 基础分布 + 时段 + 心情/慢变量 + 连续未回应次数 + 偶发“追问/长停顿”。
static func humanized_idle_delay_sec(energy: float, boredom: float, affinity: float,
		trust: float, dominance: float, mood_v: float, mood_a: float,
		day_phase: String, unanswered: int, noise: float, roll: float) -> float:
	var base := lerpf(32.0, 72.0, clampf(noise, 0.0, 1.0))
	base *= idle_mood_scale(energy, boredom)
	if affinity >= 0.70:
		base *= 0.93
	elif affinity <= 0.25:
		base *= 1.06
	if trust <= 0.30:
		base *= 1.08
	if dominance <= 0.30:
		base *= 1.04
	if mood_v <= 0.40:
		base *= 1.10
	elif mood_v >= 0.66:
		base *= 0.94
	if mood_a >= 0.68:
		base *= 0.93
	match day_phase:
		"深夜":
			base *= 1.35
		"夜里":
			base *= 1.18
		"清晨":
			base *= 1.08
		"傍晚":
			base *= 0.93
	# 连着主动开口、Estarth 一直没接话：读空气，逐步拉长（不是完全沉默）。
	if unanswered >= 1:
		base *= 1.0 + minf(0.75, 0.25 * float(unanswered))
	# 偶发短追问（亲近/兴致）与偶发长停顿（克制），让节奏不像定时器。
	if roll < 0.16:
		base *= 0.62
	elif roll > 0.94:
		base *= 1.35
	return clampf(base, 18.0, 150.0)

## 纯函数（门卫可测）：精力低 → 稍少开口；无聊高 → 更想找人说话。
## 注意这里是“助手基线”，低精力也只放慢，不允许把开口间隔放到几乎沉默。
static func idle_mood_scale(energy: float, boredom: float) -> float:
	var s := 1.0
	if energy <= 0.30:
		s *= 1.30
	elif energy >= 0.80:
		s *= 0.90
	if boredom >= 0.65:
		s *= 0.70
	elif boredom <= 0.25:
		s *= 1.05
	return clampf(s, 0.55, 1.50)

# ================= 窥屏（按心情自动，不手动） =================

func _can_peek() -> bool:
	return cfg.vlm_enabled and _online and agent != null and not agent.busy \
		and not view.is_chat_open() and not view.is_files_open() and _peek_available()

## 窥屏节奏由心情驱动：愉悦 / 唤醒高时更频繁，低落时更少；
## 再叠 25% 随机抖动，避免像定时任务。
func _peek_delay_ms() -> int:
	var base := randf_range(150.0, 300.0)
	if _mood_v >= 0.62:
		base *= 0.70
	elif _mood_v <= 0.40:
		base *= 1.55
	if _mood_a >= 0.68:
		base *= 0.80
	elif _mood_a <= 0.30:
		base *= 1.20
	base *= peek_mood_scale(_energy, _boredom)
	base *= randf_range(0.75, 1.25)
	return int(clampf(base, 90.0, 540.0) * 1000.0)

## 纯函数（门卫可测）：精力低 → 少去窥屏；无聊高 → 更想看看你在忙什么。
static func peek_mood_scale(energy: float, boredom: float) -> float:
	var s := 1.0
	if energy <= 0.30:
		s *= 1.35
	elif energy >= 0.80:
		s *= 0.90
	if boredom >= 0.65:
		s *= 0.82
	elif boredom <= 0.25:
		s *= 1.10
	return clampf(s, 0.65, 1.60)

func _start_peek(now: int) -> void:
	_peek_busy = true
	_peek_started_ms = now
	_peek_ctx_activity = _last_activity
	_peek_out = OS.get_user_data_dir().path_join("pet_peek_%d.json" % now)
	var args := PackedStringArray([_peek_script, "--out", _peek_out,
		"--game-pid", str(OS.get_process_id()), "--peek-behind"])
	var vlm := _ensure_vlm_url()
	if vlm != "":
		args.append_array(["--vlm-url", vlm])
	var pid := OS.create_process(_peek_exe, args)
	if pid <= 0:
		_peek_busy = false
		_next_peek = now + _peek_cooldown_ms
		agent.idle_remark("pet")
		return
	_peek_pid = pid
	_peek_last_ms = now
	_peek_cooldown_ms = _peek_delay_ms()
	_next_peek = now + _peek_cooldown_ms
	view.show_bubble("让我看看你在忙什么…", 5.0)
	_play_tone("puzzle")

func _tick_peek(now: int) -> void:
	if FileAccess.file_exists(_peek_out):
		_peek_pid = -1
		var data := {}
		var f := FileAccess.open(_peek_out, FileAccess.READ)
		if f != null:
			var parsed = JSON.parse_string(f.get_as_text())
			if typeof(parsed) == TYPE_DICTIONARY:
				data = parsed
			f.close()
		DirAccess.remove_absolute(_peek_out)
		_peek_busy = false
		if data.is_empty():
			return
		if _last_activity != _peek_ctx_activity or view.is_chat_open() or agent == null or agent.busy:
			return
		if not bool(data.get("ok", true)) or bool(data.get("is_game", true)):
			agent.idle_remark("pet")
			return
		agent.peer_remark(data, "pet")
		return
	if now - _peek_started_ms > 240000:
		if _peek_pid > 0:
			OS.kill(_peek_pid)
		_peek_pid = -1
		_peek_busy = false
		if _last_activity == _peek_ctx_activity and not view.is_chat_open() and agent != null and not agent.busy:
			agent.idle_remark("pet")

func _cancel_peek() -> void:
	if not _peek_busy:
		return
	if _peek_pid > 0:
		OS.kill(_peek_pid)
	_peek_pid = -1
	_peek_busy = false
	if _peek_out != "" and FileAccess.file_exists(_peek_out):
		DirAccess.remove_absolute(_peek_out)
	_peek_cooldown_ms = _peek_delay_ms()
	_next_peek = Time.get_ticks_msec() + _peek_cooldown_ms

func _peek_available() -> bool:
	if _peek_probed:
		return _peek_ok
	_peek_probed = true
	var dirs := [_repo_root().path_join("peek"),
		OS.get_executable_path().get_base_dir().path_join("peek"),
		"D:/KataGo/peek"]
	for d in dirs:
		var script: String = str(d).path_join("azure_peek.py")
		if not FileAccess.file_exists(script):
			continue
		var pys := [d.path_join(".venv/Scripts/pythonw.exe"), d.path_join(".venv/Scripts/python.exe"),
			"D:/KataGo/peek/.venv/Scripts/pythonw.exe", "D:/KataGo/peek/.venv/Scripts/python.exe",
			"E:/AI_Omni/Python/pythonw.exe", "E:/AI_Omni/Python/python.exe"]
		for py in pys:
			if FileAccess.file_exists(py):
				_peek_script = script
				_peek_exe = py
				_peek_ok = true
				return true
	print("[Pet] 未找到窥屏环境：自动窥屏退化为普通待机搭话")
	return false

func _ensure_vlm_url() -> String:
	if DisplayServer.get_name() == "headless":
		return ""
	if _vlm_url != "":
		return _vlm_url
	var manual := cfg.vlm_base_url.strip_edges()
	if manual != "":
		_vlm_url = manual
		return _vlm_url
	if not cfg.vlm_auto_start or not _peek_available():
		return ""
	var starter := ""
	for d in [_repo_root().path_join("peek"), OS.get_executable_path().get_base_dir().path_join("peek"), "D:/KataGo/peek"]:
		var st: String = str(d).path_join("start_vlm_server.py")
		if FileAccess.file_exists(st):
			starter = st
			break
	if starter == "":
		return ""
	var args := PackedStringArray([starter, "--port", str(cfg.vlm_port)])
	if cfg.vlm_exe != "":
		args.append_array(["--exe", cfg.vlm_exe])
	if cfg.vlm_model != "":
		args.append_array(["--model", cfg.vlm_model])
	if cfg.vlm_mmproj != "":
		args.append_array(["--mmproj", cfg.vlm_mmproj])
	if OS.create_process(_peek_exe, args) <= 0:
		return ""
	_vlm_url = "http://127.0.0.1:%d/v1" % cfg.vlm_port
	return _vlm_url

# ================= 围棋独立窗口 =================

func _launch_game() -> void:
	if _game_running:
		return
	_last_activity = Time.get_ticks_msec()
	_cancel_peek()
	_chat_busy = false
	view.set_chat_busy(false)
	view.close_chat()
	view.close_files()
	view.close_menu()
	view.save_position()
	if tts != null:
		tts.stop()
	_game_running = true
	_game_pid = -1
	if DisplayServer.get_name() != "headless":
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)
	var exe := OS.get_executable_path()
	if exe.ends_with("_console.exe"):
		var alt := exe.substr(0, exe.length() - "_console.exe".length()) + ".exe"
		if FileAccess.file_exists(alt):
			exe = alt
	var args := PackedStringArray()
	if not OS.has_feature("template"):
		args.append("--path")
		args.append(ProjectSettings.globalize_path("res://"))
	args.append("--")
	args.append("--game")
	args.append("--from-pet")
	if _services_started:
		OS.set_environment("AZURE_SERVICES_MANAGED", "1")
	var pid := OS.create_process(exe, args)
	if pid <= 0:
		_restore_after_game()
		view.show_bubble("围棋窗口没有打开，稍后再试。", 5.0)
		return
	_game_pid = pid

func _tick_game(now: int) -> void:
	if _game_pid <= 0 or OS.is_process_running(_game_pid):
		return
	_restore_after_game(now)

func _restore_after_game(now := 0) -> void:
	_game_running = false
	_game_pid = -1
	if DisplayServer.get_name() != "headless":
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, true)
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_ALWAYS_ON_TOP, true)
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_TRANSPARENT, true)
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_RESIZE_DISABLED, true)
		get_viewport().transparent_bg = true
		DisplayServer.window_set_title("Azure · 桌宠")
	view.restore_after_game()
	_last_activity = now if now > 0 else Time.get_ticks_msec()
	_unanswered_proactive = 0
	_next_idle = _last_activity + _first_idle_delay_ms()
	_next_peek = _last_activity + int(PEEK_FIRST_SEC * 1000.0)
	view.show_bubble("棋局先放着，我回来了。", 6.0)
	_play_tone("happy")
	if agent != null and _online:
		agent.fetch_mood(func(ok: bool, _c: int, d: Dictionary):
			if ok and not d.is_empty():
				_on_mood(d))
		_load_chats()

func _on_size_selected(h: int) -> void:
	cfg.pet_height = clampi(h, 96, 360)
	cfg.save()

func _on_quit() -> void:
	view.save_position()
	get_tree().quit()
