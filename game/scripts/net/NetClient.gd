class_name NetClient
extends Node
## 前端网络客户端：连远程后端（前端唯一的装配路径，编排与 KataGo/LLM 都在后端进程）。
##
## 对 Main.gd 暴露的信号与方法面（Main 的 agent.* 调用点只认这套契约）：
##   - 命令：REST POST（/api/move /api/hint /api/analyze /api/chat ... 见 docs）
##   - 事件：WebSocket /ws 推送（message / state / info / busy / mood / joseki / tool / token / speech / memory；连上先收 hello 同步）
##     token 是可选（默认关闭）的流式增量：只影响气泡的显示过程，最终内容仍以 message 为准
##     speech 是每条台词的**语音指令**（`mode=openai` 时按 id 去 GET /api/tts/audio/{id} 取音频；
##     `mode=system` 时按 text 交给系统语音）——合成全在后端，前端只取音频 + 播放
##     memory 是长期记忆写入/召回的元数据：只记一行日志，不进台词、不产生任何信号
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
## 她自己查资料（phase=start/end）：只给状态栏用，工具返回的原文永远不会走到这里
signal tool_event(payload: Dictionary)
## 流式增量（可选、默认关闭：后端 `stream_tokens` 开启时才会收到这个事件）
## `turn_id` 标识同一轮发言，`delta` 是这一块新增的文字；最终内容一律以 `message` 为准
signal token_event(turn_id: String, delta: String)
## 云端语音指令（计划 05）：紧随对应的 `message` 之后到达，`turn_id`+`seq` 标识它与哪条台词配对。
## `mode=openai` 时用 `id` 去 GET /api/tts/audio/{id} 取音频；`mode=system` 时用 `text` 交系统语音。
## 合成全在后端，前端不再自行合成——这里只把原样 payload 转给 Main。
signal speech_event(payload: Dictionary)
## 连接状态提示（Main 接到状态栏）
signal net_status(text: String, bad: bool)
## 聊天排队内容变了（入队 / 出队 / 退回 / 清空）：Main 据此刷新右下角那行提示（计划 06）
signal chat_queue_changed()

const RECONNECT_MIN := 1.0
const RECONNECT_MAX := 8.0
const POST_TIMEOUT := 30.0   # REST 请求超时：后端卡住时也能回调释放节点，避免泄漏
const CHAT_RETRY_GAP_MS := 2000  # 聊天发送失败（含被后端挡下）后的重试间隔：Main 每帧催一次也不会变成请求风暴

var base_url := "http://127.0.0.1:8000"

var busy := false

## 她忙时玩家发的聊天在这里排队（计划 06）：`chat()` 忙则入队，`Main` 在她空闲时调
## `flush_queued_chat()` 续发；提示文案与「能不能发」的判据都在 `ChatQueue` 里（纯逻辑、有门卫）。
var _chat_queue := ChatQueue.new()
var _chat_retry_ms := 0                  # 发送失败后的重试闸：`CHAT_RETRY_GAP_MS` 内不重发同一条
## 刚发出去一条聊天：等她这一轮**真的结束**（收到 `busy=false`）之前不再发下一条。
## 「一条接一条」不能只看 `busy`——`busy` 事件有网络往返，本机可能刚好赶不上，远端后端就会两条并排发出去。
var _chat_in_flight := false
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
			_apply_busy(bool(d.get("busy", false)))
		"message":
			message.emit(str(d.get("kind", "ai")), str(d.get("text", "")))
		"state":
			state_changed.emit(_without_type(d))
		"info":
			info_changed.emit(float(d.get("winrate", 0.5)), float(d.get("score", 0.0)))
		"busy":
			_apply_busy(bool(d.get("busy", false)))
		"mood":
			mood_changed.emit(_without_type(d))
		"joseki":
			joseki_event.emit(_without_type(d))
		"tool":
			tool_event.emit(_without_type(d))
		"token":
			# 流式增量（可选、默认关闭）：只喂气泡的显示过程，最终以 message 收口
			token_event.emit(str(d.get("turn_id", "")), str(d.get("delta", "")))
		"speech":
			# 这条台词的语音指令（合成在后端）：Main 据此取音频 / 调系统语音，并把它与
			# 待显示的那条台词配起来（turn_id + seq）
			speech_event.emit(_without_type(d))
		"memory":
			# 长期记忆的写入 / 召回：元数据（不是台词）——只记一行日志，不产生任何界面信号
			print("[NetClient] memory op=%s tag=%s score=%s text=%s" % [
				str(d.get("op", "")), str(d.get("tag", "")), str(d.get("score", 0.0)), str(d.get("text", ""))])

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

## 自由聊天。她忙（后端 `POST /api/chat` 会挡下）时**先本地排队**，空闲时 `flush_queued_chat()` 续发。
## 返回 `false` 表示**队列满了、这条没收** —— 调用方要保留输入框原文并在状态栏提示，绝不吞玩家的话。
func chat(text: String) -> bool:
	if text.strip_edges() == "":
		return true                      # 空话后端也当没发：既不排队也不发（别占住队头）
	if _can_send_now():
		_chat_in_flight = true           # 这一条出去了：下一条要等她这一轮结束（busy=false）才放
		_send_chat_now(text)
		return true
	return _enqueue_chat(text)

## 能不能**直发**（而不是排队）：她这一轮没在跑、没有刚发出去还没结账的、队列是空的、后端也连着。
## 只有这条路走直发 —— 于是 `_send_chat_now` 失败时的 `push_front` 一定是真正的队头（顺序不乱），
## 上限也不会被绕过（未连接 / 忙 / 队里还有东西时一律走 `enqueue`，那里才有 `CHAT_QUEUE_MAX` 这道闸）。
func _can_send_now() -> bool:
	return not busy and not _chat_in_flight and _chat_queue.is_empty() and _ws_open()

func _ws_open() -> bool:
	return _ws != null and _ws.get_ready_state() == WebSocketPeer.STATE_OPEN

## 忙闲同步（WS 的 `hello` / `busy` 事件都走这里）：`busy=false` 说明她这一轮结束了 ——
## 排队聊天可以发下一条了（「一条接一条」的另一半：放行在 `chat()` / `flush_queued_chat()`）。
func _apply_busy(v: bool) -> void:
	busy = v
	if not v:
		_chat_in_flight = false
	busy_changed.emit(v)

## 入队（尾部）：满了/空文本 ⇒ `false`（队列不变）；成功 ⇒ 发一次 `chat_queue_changed`。
func _enqueue_chat(text: String) -> bool:
	if not _chat_queue.enqueue(text):
		return false
	chat_queue_changed.emit()
	return true

## 真发一条聊天（带回复判定）：**只有明确回 `ok: true` 才算收下**；忙（`busy=false` 事件还没到的竞态）、
## 非 2xx、回包读不懂（代理 HTML 错误页一类）⇒ 这条**退回队头**（它本来是最该发的一条，退回也保序），
## 并压一个重试闸防每帧重发。宁可极少数情况下重复发一次，也不吞玩家的话。
func _send_chat_now(text: String) -> void:
	fetch_bytes("/api/chat", HTTPClient.METHOD_POST, {"text": text},
		func(ok: bool, _code: int, body: PackedByteArray):
			if ok and _chat_reply_ok(body):
				return                   # 收下了：`_chat_in_flight` 交给随后的 `busy` 事件清（她这一轮开始）
			_chat_in_flight = false      # 没送出去：立刻放行（重试由节流窗口管）
			_chat_retry_ms = Time.get_ticks_msec() + CHAT_RETRY_GAP_MS
			_chat_queue.push_front(text)
			chat_queue_changed.emit())

## 回包是否表示「后端收下了这条聊天」（成功是 `{"ok": true}`；忙时是 `{"ok": false, "busy": true, …}`）。
## 先看首字节再解析：空包 / HTML 错误页这类一眼就不是 JSON 对象的，直接判「没收下」，
## 顺带避开 `JSON.parse_string` 对脏输入的报错噪音。
static func _chat_reply_ok(body: PackedByteArray) -> bool:
	if body.is_empty() or body[0] != 123:       # 123 = '{'
		return false
	var d = JSON.parse_string(body.get_string_from_utf8())
	return typeof(d) == TYPE_DICTIONARY and bool((d as Dictionary).get("ok", false))

## 催一次续发。`env` 是「她现在闲不闲」的外部事实（`{"busy": …, "speaking": …}`，由 Main 打包）。
## 四道门禁：① 没有刚发出去还等她开播/结账的（`_chat_in_flight`，保证一条接一条）；② 状态机判据
## （空 / 忙 / 还在朗读都不发）；③ WS 已连上（没连上不发请求，免得 Main 每帧催成请求风暴）；
## ④ 失败节流窗口内不重发同一条。
func flush_queued_chat(env: Dictionary) -> void:
	if _chat_in_flight or not _chat_queue.should_flush(env):
		return
	if not _ws_open():
		return                           # 没连上后端：消息留在队列里，等重连 + 空闲再自动补发
	if Time.get_ticks_msec() < _chat_retry_ms:
		return
	_chat_in_flight = true
	_send_chat_now(_chat_queue.pop())
	chat_queue_changed.emit()

## 清空排队（测试 / 收尾用）
func clear_queued_chat() -> void:
	_chat_queue.clear()
	chat_queue_changed.emit()

func has_queued_chat() -> bool:
	return not _chat_queue.is_empty()

func queued_chat_count() -> int:
	return _chat_queue.size()

## 右下角那行提示（`排队中 N 条 · 队头前 8 字`；队列空 ⇒ `""`）
func queued_chat_hint() -> String:
	return _chat_queue.hint_text()

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

## 取一次响应体（GET 音频 / POST 试听合成）。与 _post 的「发完不管」不同，这里把结果交给回调：
## `callback(ok: bool, code: int, body: PackedByteArray)`，只有 HTTP 2xx 才算 ok。
## 连请求都发不出去（err != OK）时**立即**回调失败，不让调用方干等超时；节点用完自释放。
## `timeout` 用秒（默认 POST_TIMEOUT）：试听要等后端合成，调用方可放宽。
func fetch_bytes(path: String, method: int, body: Dictionary, callback: Callable,
		timeout := POST_TIMEOUT) -> void:
	var req := HTTPRequest.new()
	req.timeout = timeout
	add_child(req)
	var payload := JSON.stringify(body) if method != HTTPClient.METHOD_GET else ""
	req.request_completed.connect(func(result: int, code: int, _headers: PackedStringArray, data: PackedByteArray):
		req.queue_free()
		if callback.is_valid():
			callback.call(result == HTTPRequest.RESULT_SUCCESS and code >= 200 and code < 300, code, data))
	var err := req.request(_strip_slash(base_url) + path,
			["Content-Type: application/json"], method, payload)
	if err != OK:
		req.queue_free()
		if callback.is_valid():
			callback.call(false, 0, PackedByteArray())
