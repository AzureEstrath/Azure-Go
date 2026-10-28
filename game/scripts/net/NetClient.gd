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
signal ownership_changed(values: Array, move_count: int)   # 逐点归属（+黑/-白，361 个）
signal estimate_event(payload: Dictionary)                # 局面分析的点目注记（v3.12.0 采纳）
signal difficulty_changed(level: int, base: int, reason: String)   # 自适应难度档位变化（v3.11.5）
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
## 后端明确拒绝了这一手（多半是「她还在应手」——`busy` 事件还没到客户端的竞态）：
## 载荷是**权威快照**，前端拿它撤掉乐观落下的那颗子，而不是让幽灵子留在盘上。
signal move_rejected(state: Dictionary)

const RECONNECT_MIN := 1.0
const RECONNECT_MAX := 8.0
const CONNECT_TIMEOUT := 8.0     # 远端地址黑洞时 TCP 可能要等 20 秒：超时后主动重连，别一直「连接中」
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
## 定式「开关 + 频率」批量写入中（v3.7.0 修复波）：期间两个 setter 都不各自推送，
## 由 `set_joseki()` 收尾统一推一次（见那里的注释）。
var _joseki_batch := false
## 情绪波动「开关 + 基础概率」批量写入中（v3.8.0）：与 `_joseki_batch` 同理。
var _drift_batch := false
## 聊天摘要用 LLM（单字段）原子入口的批量写入中（v3.8.0）：与 `_drift_batch` 同理——单字段本可只靠
## setter 推一次，这里保持与 `set_drift` / `set_joseki` 同一形状，将来加字段不会退回「两次 setter 各推一次」。
var _digest_batch := false
## 说话详略（v3.11.1）：0 精简 / 1 适中 / 2 详细。旧字段 `limit_len` 已并入它，
## 推送时仍带上 `limit_len = (reply_style == 0)`，让旧版后端也读得懂「精简」。
var tts_local_voice := ""                 # 本地音色：入树前 POST 可能失败，连上后随设置一起补发
var reply_style := 0:
	set(v):
		reply_style = v
		_push_settings()
## 思考强度（v3.11.1）：0 快速 / 1 中等 / 2 强思考。运行期推送，后端即时改档（不用重启）。
var think_mode := 0:
	set(v):
		think_mode = v
		_push_settings()
var joseki_teach := true:
	set(v):
		joseki_teach = v
		if not _joseki_batch:
			_push_settings()
var joseki_rate := 0.5:
	set(v):
		joseki_rate = v
		if not _joseki_batch:
			_push_settings()
var emotion_drift := false:
	set(v):
		emotion_drift = v
		if not _drift_batch:
			_push_settings()
var drift_base := 0.03:
	set(v):
		drift_base = v
		if not _drift_batch:
			_push_settings()
var chat_digest_llm := false:
	set(v):
		chat_digest_llm = v
		if not _digest_batch:
			_push_settings()
## 玩家棋风画像 / 时间感知（v3.9.0）：两个开关**一次落定、只推一次** `/api/config`
## （与 `set_joseki` / `set_drift` 同一个理由与同一个做法）。
var _profile_batch := false
var profile_enabled := false:
	set(v):
		profile_enabled = v
		if not _profile_batch:
			_push_settings()
var time_aware_enabled := false:
	set(v):
		time_aware_enabled = v
		if not _profile_batch:
			_push_settings()
## 人格化（自用版）：总开关 + 关系称呼（默认关）+ 高阶段称呼名。
## 走 `_push_settings` 统一推送，WS 重连时会和其余设置一起重新对齐（不会因为掉线丢一次改动）。
var _persona_batch := false
var persona_enabled := true:
	set(v):
		persona_enabled = v
		if not _persona_batch:
			_push_settings()
var persona_relationship_enabled := false:
	set(v):
		persona_relationship_enabled = v
		if not _persona_batch:
			_push_settings()
var persona_address_name := "":
	set(v):
		persona_address_name = str(v).strip_edges().substr(0, 16)
		if not _persona_batch:
			_push_settings()
## 自适应难度（v3.11.5）：开关 + 基线（1..5）一次推一次；与 profile 同一套原子入口做法。
var _adaptive_batch := false
var adaptive_difficulty := false:
	set(v):
		adaptive_difficulty = v
		if not _adaptive_batch:
			_push_settings()
var adaptive_difficulty_base := 3:
	set(v):
		adaptive_difficulty_base = v
		if not _adaptive_batch:
			_push_settings()

var _ws: WebSocketPeer = null
var _opened_once := false
var _retry_wait := RECONNECT_MIN
var _retry_in := 0.0
var _connecting_for := 0.0       # 本次「连接中」已等了多少秒（CONNECT_TIMEOUT 后主动放弃）
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
				_connecting_for = 0.0
				net_status.emit("后端已连接：%s" % _strip_slash(base_url), false)
				_settings_dirty = true          # 新连接可能就是新起的后端进程：把当前设置再对齐一次
				_flush_settings()
			while _ws.get_available_packet_count() > 0:
				_handle_packet(_ws.get_packet().get_string_from_utf8())
		WebSocketPeer.STATE_CONNECTING:
			_connecting_for += delta
			if _connecting_for >= CONNECT_TIMEOUT:
				_ws.close()                     # 远端一直不响应：放弃这次，按退避重来
				_ws = null
				_opened_once = false
				_connecting_for = 0.0
				_schedule_retry()
		WebSocketPeer.STATE_CLOSED:
			_ws.close()
			_ws = null
			_opened_once = false
			_connecting_for = 0.0
			_schedule_retry()

func _exit_tree() -> void:
	if _ws != null:
		_ws.close()

# ================= 连接 =================

func _connect_ws() -> void:
	_ws = WebSocketPeer.new()
	_connecting_for = 0.0
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
		"estimate":
			# 点目注记：原样交给 Main（只画棋盘 / 面板，不产生台词、不改 busy）
			estimate_event.emit(_without_type(d))
		"ownership":
			var vals = d.get("values", [])
			ownership_changed.emit(vals if typeof(vals) == TYPE_ARRAY else [],
				int(d.get("move_count", 0)))
		"difficulty":
			difficulty_changed.emit(int(d.get("level", 3)), int(d.get("base", 3)),
				str(d.get("reason", "")))
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
	# 这一手必须**看回包**：后端忙时会回 `{"ok": false, "busy": true, "state": …}`（HTTP 仍是 200），
	# 而棋盘在她应手之前已经乐观画上了这颗子——不看回包那它就变成永远撤不掉的幽灵子
	# （v3.9.5：玩家会以为「她还没下，我又下了一手」）。拒绝时把权威快照交回 Main。
	fetch_bytes("/api/move", HTTPClient.METHOD_POST, {"x": x, "y": y},
			func(ok: bool, _code: int, body: PackedByteArray) -> void:
				if not ok:
					return                       # 连不上：棋盘锁的超时兜底会放行，别在这里解锁
				var d = JSON.parse_string(body.get_string_from_utf8())
				if typeof(d) != TYPE_DICTIONARY:
					return
				if bool(d.get("ok", true)):
					return                       # 正常受理：等 busy / state 事件
				_apply_busy(bool(d.get("busy", false)))
				var st = d.get("state", null)
				if typeof(st) == TYPE_DICTIONARY:
					move_rejected.emit(st))

func hint() -> void:
	_post("/api/hint")

func analyze_position() -> void:
	_post("/api/analyze")

## 自由聊天。她忙（后端 `POST /api/chat` 会挡下）时**先本地排队**，空闲时 `flush_queued_chat()` 续发。
## 返回 `false` 表示**队列满了、这条没收** —— 调用方要保留输入框原文并在状态栏提示，绝不吞玩家的话。
func chat(text: String, mode := "game") -> bool:
	if text.strip_edges() == "":
		return true                      # 空话后端也当没发：既不排队也不发（别占住队头）
	if _can_send_now():
		_chat_in_flight = true           # 这一条出去了：下一条要等她这一轮结束（busy=false）才放
		_send_chat_now(text, mode)
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
func _send_chat_now(text: String, mode := "game") -> void:
	fetch_bytes("/api/chat", HTTPClient.METHOD_POST, {"text": text, "mode": mode},
		func(ok: bool, _code: int, body: PackedByteArray):
			if ok and _chat_reply_ok(body):
				return                   # 收下了：`_chat_in_flight` 交给随后的 `busy` 事件清（她这一轮开始）
			_chat_in_flight = false      # 没送出去：立刻放行（重试由节流窗口限）
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

## 开新局。`silent=true`（v3.12.1）＝只清棋局、不添「重新来吧」那句——从主界面进对局时用。
## 返回 false 表示请求没发出去，调用方应保留「待重开」标志并重试。
func reset_game(silent := false) -> bool:
	return _post("/api/reset", {"silent": silent})

func set_allow_pass(v: bool) -> void:
	_post("/api/config", {"allow_pass": v})

## 本地音色运行期切换：只更新后端内存里的 TTS 配置（不重启后端、不丢棋局），下一句立即生效。
## 同时记进字段：桌宠启动时后端可能还没起来，这条 POST 会失败——WS 连上后的 `_flush_settings`
## 会把它和其他运行期设置一起补发。
func set_tts_local_voice(v: String) -> void:
	tts_local_voice = v
	_post("/api/config", {"tts_local_voice": v})

## 由界面在她「落子宣告」的语音开始播放时调用：让后端把这一手推给棋盘显示
func reveal_move() -> void:
	_post("/api/reveal")

func player_interaction(what: String) -> void:
	_post("/api/interaction", {"what": what})

func idle_remark(mode := "game") -> void:
	_post("/api/idle", {"mode": mode})

## 窥屏搭话：客户端截图 + OCR + VLM 后，把看到的内容交给后端组织台词。
func peer_remark(info: Dictionary, mode := "game") -> void:
	_post("/api/peer", {"fg_title": str(info.get("fg_title", "")),
		"ocr": str(info.get("ocr", "")),
		"vlm": str(info.get("vlm", "")), "mode": mode})

func tick_mood() -> void:
	_post("/api/mood/tick")

## 当前情绪快照（桌宠「心情 · 记忆」用）：`callback(ok, code, {v,c,a,name,...})`。
## 同一情绪区域内的小幅数值变化不会发 mood 事件，桌宠点完 / 打开面板时主动拉一次。
func fetch_mood(callback: Callable) -> void:
	_fetch_json("/api/mood", HTTPClient.METHOD_GET, {}, callback)

## 生命时间线快照（桌宠启动 / 重连）：`callback(ok, code, {state,temporal,events,...})`。
## 与围棋共用同一条后端时间线；桌宠用 `temporal.since_interaction_sec` 判断“离开多久”。
func fetch_life(callback: Callable) -> void:
	_fetch_json("/api/life?limit=12", HTTPClient.METHOD_GET, {}, callback)

## 资料箱文件列表（桌宠右键「资料与日程」）：`callback(ok, code, {folder,files,...})`。
func fetch_assistant_files(callback: Callable) -> void:
	_fetch_json("/api/files", HTTPClient.METHOD_GET, {}, callback, 20.0)

## 共享日程列表：`callback(ok, code, {items:[...]})`。
func fetch_schedule(callback: Callable) -> void:
	_fetch_json("/api/schedule?limit=30", HTTPClient.METHOD_GET, {}, callback, 20.0)

## 开始处理资料：OCR/VLM + LLM 提取日程是长任务，超时放宽到 5 分钟。
func scan_assistant_files(callback: Callable) -> void:
	_fetch_json("/api/files/scan", HTTPClient.METHOD_POST, {}, callback, 300.0)

## 自动排班：优先级 + James-Stein 收缩 + 空闲区间 best-fit；只动待定 / 自动排过的任务。
func auto_plan_schedule(callback: Callable, days := 7) -> void:
	_fetch_json("/api/schedule/auto_plan", HTTPClient.METHOD_POST,
		{"days": clampi(days, 1, 30)}, callback, 60.0)

## 留档：把过期条目移入归档（不删除）。
func archive_schedule(callback: Callable) -> void:
	_fetch_json("/api/schedule/archive", HTTPClient.METHOD_POST, {}, callback, 30.0)

## 清理：删除超过保留期的归档条目（默认 30 天，不动未归档）。
func cleanup_schedule(callback: Callable, days := 30) -> void:
	_fetch_json("/api/schedule/cleanup", HTTPClient.METHOD_POST,
		{"days": clampi(days, 1, 3650), "include_unarchived": false}, callback, 30.0)

## 执行反馈：done / deferred / skipped；完成后更新类别 EWMA，推迟后后端会立即重排。
func feedback_schedule(item_id: int, outcome: String, actual_min: int,
		callback: Callable) -> void:
	_fetch_json("/api/schedule/feedback", HTTPClient.METHOD_POST,
		{"id": item_id, "outcome": outcome, "actual_min": maxi(0, actual_min)},
		callback, 30.0)

## 每周复盘：按类别返回计划 / 实际耗时偏差与完成、推迟计数。
func fetch_schedule_review(callback: Callable, days := 7) -> void:
	_fetch_json("/api/schedule/review?days=%d" % clampi(days, 1, 365),
		HTTPClient.METHOD_GET, {}, callback, 20.0)

## 最近聊天流水（桌宠聊天面板 / 启动恢复）：`callback(ok, code, {"items":[...]})`。
func fetch_chat_history(limit: int, callback: Callable) -> void:
	_fetch_json("/api/chat/history?limit=%d" % clampi(limit, 1, 200),
		HTTPClient.METHOD_GET, {}, callback)

## 权威棋盘快照（从桌宠切回正式界面时补一次，防止后台走了几手而前端还停在旧盘面）。
func fetch_state(callback: Callable) -> void:
	_fetch_json("/api/state", HTTPClient.METHOD_GET, {}, callback)

func memory_stats(callback: Callable) -> void:
	_fetch_json("/api/memory/stats", HTTPClient.METHOD_GET, {}, callback)

func memory_query(query: String, k: int, callback: Callable) -> void:
	_fetch_json("/api/memory/query", HTTPClient.METHOD_POST, {"query": query, "k": k}, callback)

func memory_write(text: String, tag := "") -> void:
	_post("/api/memory/write", {"text": text, "tag": tag})

## 初始/重开后的情绪同步：remote 模式下由后端 hello 事件与 reset 事件推送，这里无需动作
func sync_mood(_force := false, _quiet := false) -> void:
	pass

func _push_settings() -> void:
	_settings_dirty = true
	if _ws != null and _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		_flush_settings()

## 定式导学「开关 + 频率」**原子入口**（v3.7.0 修复波）：两个字段一次落定，**只推一次** `/api/config`。
##
## 分别赋 `joseki_teach` / `joseki_rate` 会各触发一次 setter → 两次 `_push_settings`；`_post` 每次
## `HTTPRequest.new()` 各开一条连接，**到达顺序无保证**——极端情况下后端会留下「新开关 + 旧频率」，
## 直到下次改配置才自纠。这里先落两个字段、再统一推一次，载荷与 `_flush_settings()` 完全一致。
func set_joseki(teach: bool, rate: float) -> void:
	_joseki_batch = true
	joseki_teach = teach
	joseki_rate = rate
	_joseki_batch = false
	_push_settings()

## 情绪波动「开关 + 基础概率」**原子入口**（v3.8.0）：与 `set_joseki` 同一个理由与同一个做法——
## 两个字段一次落定、**只推一次** `/api/config`（分两次赋 setter 会各推一条 POST，到达顺序无保证）。
func set_drift(on: bool, base: float) -> void:
	_drift_batch = true
	emotion_drift = on
	drift_base = base
	_drift_batch = false
	_push_settings()

## 聊天摘要用 LLM **原子入口**（v3.8.0）：与 `set_drift` / `set_joseki` 同一个理由与同一个做法——
## 一次落定、**只推一次** `/api/config`（默认关：摘要走规则式、零额外 LLM 调用；开启后只在局末 / 空闲补做）。
func set_digest(on: bool) -> void:
	_digest_batch = true
	chat_digest_llm = on
	_digest_batch = false
	_push_settings()

## 玩家棋风画像 / 时间感知两个开关的**原子入口**（v3.9.0）：与 `set_drift` / `set_joseki` 同一个理由与
## 同一个做法——两个字段一次落定、**只推一次** `/api/config`（分两次赋 setter 会各推一条 POST，
## 到达顺序无保证）。两个开关在界面上属于同一件事（画像面板那一格），所以一起推。
func set_profile(profile_on: bool, time_on: bool) -> void:
	_profile_batch = true
	profile_enabled = profile_on
	time_aware_enabled = time_on
	_profile_batch = false
	_push_settings()

## 人格化总开关 + 关系称呼**一次落定、只推一次**（启动装配用）：
## 分两次调 `set_persona` / `set_persona_relationship` 会各发一条 POST，到达顺序无保证，
## 可能出现「关系刚开又被上一条旧载荷关掉」。启动时用这个原子入口。
func set_persona_all(enabled: bool, relationship_on: bool, address_name: String) -> void:
	_persona_batch = true
	persona_enabled = enabled
	persona_relationship_enabled = relationship_on
	persona_address_name = address_name
	_persona_batch = false
	_push_settings()

## 人格化开关（自用版）：单字段，与其余设置一起推 `/api/config`；关掉后后端提示词逐字符不变。
func set_persona(on: bool) -> void:
	_persona_batch = true
	persona_enabled = on
	_persona_batch = false
	_push_settings()

## 关系阶段与称呼变化（默认关）：开关 + 高阶段称呼名一次推一次；留空＝仍叫 prompt_user_name。
func set_persona_relationship(on: bool, address_name: String) -> void:
	_persona_batch = true
	persona_relationship_enabled = on
	persona_address_name = address_name
	_persona_batch = false
	_push_settings()

## 自适应难度「开关 + 基线」原子入口（v3.11.5）：两个字段一次落定、只推一次 `/api/config`。
func set_adaptive(on: bool, base: int) -> void:
	_adaptive_batch = true
	adaptive_difficulty = on
	adaptive_difficulty_base = clampi(base, 1, 5)
	_adaptive_batch = false
	_push_settings()

## 画像面板（**只读**，v3.9.0 / Q6）：`GET /api/profile` → 回调 `(ok, code, body)`。
## 只读、无副作用（不写库、不改任何开关）；连不上后端时回调 `ok=false`（与 `fetch_bytes` 同一套收账）。
func fetch_profile(callback: Callable) -> void:
	fetch_bytes("/api/profile", HTTPClient.METHOD_GET, {}, callback)

func _flush_settings() -> void:
	if not _settings_dirty:
		return
	_settings_dirty = false
	_post("/api/config", {"reply_style": reply_style, "limit_len": reply_style == 0,
		"think_mode": think_mode, "tts_local_voice": tts_local_voice,
		"joseki_teach": joseki_teach, "joseki_rate": joseki_rate,
		"emotion_drift": emotion_drift, "drift_base": drift_base, "chat_digest_llm": chat_digest_llm,
		"profile_enabled": profile_enabled, "time_aware_enabled": time_aware_enabled,
		"persona_enabled": persona_enabled,
		"persona_relationship_enabled": persona_relationship_enabled,
		"persona_address_name": persona_address_name,
		"adaptive_difficulty": adaptive_difficulty,
		"adaptive_difficulty_base": adaptive_difficulty_base})

## 取一次 JSON：HTTP 2xx 且响应体是对象才算 ok；其余一律 callback(false, code, {})。
func _fetch_json(path: String, method: int, body: Dictionary, callback: Callable,
		timeout := POST_TIMEOUT) -> void:
	var done := func(ok: bool, code: int, data: PackedByteArray):
		var parsed = JSON.parse_string(data.get_string_from_utf8())
		var d := {}
		if typeof(parsed) == TYPE_DICTIONARY:
			d = parsed
		if callback.is_valid():
			callback.call(ok and typeof(parsed) == TYPE_DICTIONARY, code, d)
	fetch_bytes(path, method, body, done, timeout)

func _post(path: String, body: Dictionary = {}) -> bool:
	var req := HTTPRequest.new()
	req.timeout = POST_TIMEOUT        # 超时也会触发 request_completed → 回调里 queue_free
	add_child(req)
	req.request_completed.connect(func(_result, _code, _headers, _data): req.queue_free())
	var err := req.request(_strip_slash(base_url) + path,
			["Content-Type: application/json"], HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		req.queue_free()
		return false
	return true

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
