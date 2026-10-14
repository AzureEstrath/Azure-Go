class_name NetClient
extends Node
## 前端网络客户端：连远程后端（前端唯一的装配路径，编排与 KataGo/LLM 都在后端进程）。
##
## 对 Main.gd 暴露的信号与方法面（Main 的 agent.* 调用点只认这套契约）：
##   - 命令：REST POST（/api/move /api/hint /api/analyze /api/chat ... 见 docs）
##   - 事件：WebSocket /ws 推送（message / state / info / busy / mood / joseki；连上先收 hello 同步）
##
## 音画同步仍在客户端完成：`ai_move` 那条「落子宣告」的语音开始时，Main 调 reveal_move()，
## 这里 POST /api/reveal 让后端把已落到内部棋谱的那一手推给界面显示。
##
## 说明：前端没有任何本地编排实现，本客户端就是唯一入口；连不上后端时只发 net_status 提示。

# —— 对外契约（信号 + 方法）——
signal message(kind: String, text: String)
signal state_changed(payload: Dictionary)
signal info_changed(winrate: float, score: float)
signal busy_changed(busy: bool)
signal mood_changed(payload: Dictionary)
signal joseki_event(payload: Dictionary)
## 连接状态提示（Main 接到状态栏）
signal net_status(text: String, bad: bool)

const RECONNECT_MIN := 1.0
const RECONNECT_MAX := 8.0
const POST_TIMEOUT := 30.0   # REST 请求超时：后端卡住时也能回调释放节点，避免泄漏

var base_url := "http://127.0.0.1:8000"

var busy := false
var limit_len := false:
	set(v):
		limit_len = v
		_push_settings()
var joseki_teach := true:
	set(v):
		joseki_teach = v
		_push_settings()

var _ws: WebSocketPeer = null
var _opened_once := false
var _retry_wait := RECONNECT_MIN
var _retry_in := 0.0
var _settings_dirty := false

## 占位（引擎/LLM 都在后端，这里不需要初始化；保留签名让 Main 的调用点统一）
func setup(_engine = null, _client = null) -> void:
	pass

func _ready() -> void:
	_connect_ws()

func _process(delta: float) -> void:
	if _ws == null:
		if _retry_in > 0.0:
			_retry_in -= delta
			if _retry_in <= 0.0:
				_connect_ws()
		return
	_ws.poll()
	match _ws.get_ready_state():
		WebSocketPeer.STATE_OPEN:
			if not _opened_once:
				_opened_once = true
				_retry_wait = RECONNECT_MIN
				net_status.emit("后端已连接：%s" % _strip_slash(base_url), false)
				_flush_settings()
			while _ws.get_available_packet_count() > 0:
				_handle_packet(_ws.get_packet().get_string_from_utf8())
		WebSocketPeer.STATE_CLOSED:
			_ws.close()
			_ws = null
			_opened_once = false
			_schedule_retry()

func _exit_tree() -> void:
	if _ws != null:
		_ws.close()

# ================= 连接 =================

func _connect_ws() -> void:
	_ws = WebSocketPeer.new()
	var err := _ws.connect_to_url(_ws_url())
	if err != OK:
		_ws = null
		_schedule_retry()

func _schedule_retry() -> void:
	_retry_in = _retry_wait
	_retry_wait = minf(_retry_wait * 2.0, RECONNECT_MAX)
	net_status.emit("未连接后端（%s），%d 秒后重试…" % [_strip_slash(base_url), int(_retry_in)], true)

func _strip_slash(u: String) -> String:
	var s := u.strip_edges()
	while s.ends_with("/"):
		s = s.substr(0, s.length() - 1)
	return s

func _ws_url() -> String:
	var b := _strip_slash(base_url)
	if b.begins_with("https://"):
		return "wss://" + b.substr(8) + "/ws"
	if b.begins_with("http://"):
		return "ws://" + b.substr(7) + "/ws"
	return b + "/ws"

# ================= 事件 =================

func _handle_packet(raw: String) -> void:
	var d = JSON.parse_string(raw)
	if typeof(d) != TYPE_DICTIONARY:
		return
	var t := str((d as Dictionary).get("type", ""))
	match t:
		"hello":
			state_changed.emit(_dict(d.get("state", {})))
			mood_changed.emit(_dict(d.get("mood", {})))
			busy = bool(d.get("busy", false))
			busy_changed.emit(busy)
		"message":
			message.emit(str(d.get("kind", "ai")), str(d.get("text", "")))
		"state":
			state_changed.emit(_without_type(d))
		"info":
			info_changed.emit(float(d.get("winrate", 0.5)), float(d.get("score", 0.0)))
		"busy":
			busy = bool(d.get("busy", false))
			busy_changed.emit(busy)
		"mood":
			mood_changed.emit(_without_type(d))
		"joseki":
			joseki_event.emit(_without_type(d))

static func _dict(v) -> Dictionary:
	return v if typeof(v) == TYPE_DICTIONARY else {}

static func _without_type(d: Dictionary) -> Dictionary:
	var out := d.duplicate()
	out.erase("type")
	return out

# ================= 命令（REST，与 Main 的调用点一一对应） =================

func player_move(x: int, y: int) -> void:
	_post("/api/move", {"x": x, "y": y})

func hint() -> void:
	_post("/api/hint")

func analyze_position() -> void:
	_post("/api/analyze")

func chat(text: String) -> void:
	_post("/api/chat", {"text": text})

func summarize(note: String) -> void:
	_post("/api/summarize", {"note": note})

func undo(turn_index: int) -> void:
	_post("/api/undo", {"turn": turn_index})

func reset_game() -> void:
	_post("/api/reset")

func set_allow_pass(v: bool) -> void:
	_post("/api/config", {"allow_pass": v})

## 由界面在她「落子宣告」的语音开始播放时调用：让后端把这一手推给棋盘显示
func reveal_move() -> void:
	_post("/api/reveal")

func player_interaction(what: String) -> void:
	_post("/api/interaction", {"what": what})

func idle_remark() -> void:
	_post("/api/idle")

func tick_mood() -> void:
	_post("/api/mood/tick")

## 初始/重开后的情绪同步：remote 模式下由后端 hello 事件与 reset 事件推送，这里无需动作
func sync_mood(_force := false, _quiet := false) -> void:
	pass

func _push_settings() -> void:
	_settings_dirty = true
	if _ws != null and _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		_flush_settings()

func _flush_settings() -> void:
	if not _settings_dirty:
		return
	_settings_dirty = false
	_post("/api/config", {"limit_len": limit_len, "joseki_teach": joseki_teach})

func _post(path: String, body: Dictionary = {}) -> void:
	var req := HTTPRequest.new()
	req.timeout = POST_TIMEOUT        # 超时也会触发 request_completed → 回调里 queue_free
	add_child(req)
	req.request_completed.connect(func(_result, _code, _headers, _data): req.queue_free())
	var err := req.request(_strip_slash(base_url) + path,
			["Content-Type: application/json"], HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		req.queue_free()
