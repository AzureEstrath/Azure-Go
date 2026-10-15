class_name SpeechPairing
extends Node
## 语音配对状态机（计划 05 · Task 4 从 `Main.gd` 抽出的**可测决策层**；T22）。
##
## 后端每条对外台词都是「先 `message`、`await` 合成、紧跟一条 `speech`」，前端要把这两半配起来，
## 并回答三类问题：
##   · 这条 `speech` 该配给哪条台词（`accept_speech`：按**到达顺序**取最早那条还没配上的）；
##   · 这条台词现在能不能显示（`arm` / `display` / `flush_pending` / `release_stale`）；
##   · 她这一侧还有没有语音在途（`busy` / `audio_in_flight`：棋盘锁与「思考中…」占位都看它）。
##
## 这里**不做任何副作用**：取音频（HTTP）、播放、显示气泡、`POST /api/reveal` 都留在 `Main.gd`，
## 由信号 `display_requested` / `resolved` 回调出去。时钟可注入（`clock`）：默认取
## `Time.get_ticks_msec()`，测试里换成手动推进的假时钟，用来钉住两条时间规则
## （等不到语音的配对位 `SPEECH_PAIR_TTL_MS` 后清出、只有文字的标记 `TEXT_ONLY_TTL_MS` 保鲜）。
##
## 记录（`Dictionary`）字段：
##   `id` / `text` / `reveal` / `kind` —— 这条台词
##   `tomb`       本地已丢弃（走神作废 / 防复读）：只占位吃掉它自己那条 `speech`，不出声也不显示
##   `t`          登记时刻（毫秒；配 TTL 用）
##   `got`        已配上 `speech`；`group` 交给 `TTSClient` 后拿到的分组号（-1 = 还没交）
##   `played`     语音已出结果（开播 / 判定无声 / 被丢弃）
##   `armed`      已进入显示队列（`_deliver_msg` 跑过）；`displayed` 文字已显示
##   `released`   已经「不等它了」（不再算「她在忙」，也就不会把棋盘锁死）
##   `wait_display` 「同步说话」开着时：文字等这条语音开播才显示
##
## **不变量（T25，最要紧的一条）**：任何**已登记、且未被本地作废**（非 `tomb`、未被 `forget_idle()`）
## 的台词，**最终一定会被显示一次**——无论它的 `speech` 到没到、无论配对位是否过了 TTL、
## 无论那一轮是否还 `busy`：
##   · `speech` 到了 → 开播时（`display`）或判定无声时（`finish` + `display`）显示；
##   · `speech` 没到 → 回合结束时 `release_stale()` 放出来；若那一轮一直 `busy` / 音频一直在途，
##     TTL 到期时 `prune_expired()` **先显示再清出**；
##   · 音频取不回 / 解析不了 → 调用方补 `speech_skipped` → 显示（`release_stale` 也会兜底）。
## 只有两类记录**有意不显示**：`tomb`（本地丢弃）与 `forget_idle()` 作废的走神搭话。

## 该把这条台词的文字显示出来了（Main 收到后：`_add_msg_now` + 必要时 `_reveal_move()`）
signal display_requested(text: String, reveal: bool)
## 有记录出了结果（开播 / 判定无声 / 被清出）：Main 据此刷新棋盘锁与「思考中…」占位
signal resolved()

const TEXT_ONLY_TTL_MS := 3000     # 「只有文字」标记的保鲜期：那次调用没落到（后端被 busy 挡下）就自然失效
const SPEECH_PAIR_TTL_MS := 45000  # 配对位保鲜：一条 speech 最晚也会在后端合成超时内发出（默认 30s）

## 毫秒时钟（可注入；测试里换成手动推进的假时钟）
var clock: Callable = Callable()

## 上一次 `accept_speech` 没取走配对位的原因：`""`=取走了 / `"stale"`=重复或乱序 / `"orphan"`=没有对应台词
var last_drop_reason := ""

var _pairs: Array[Dictionary] = []
var _by_group := {}
var _next_id := 0
var _text_only_left := 0
var _text_only_ms := 0
var _last_turn := ""
var _last_seq := 0

func now_ms() -> int:
	return int(clock.call()) if clock.is_valid() else Time.get_ticks_msec()

# ================= 登记 / 「只有文字」标记 =================

## 登记一条要朗读的台词（`tomb=true` 只占位吃掉它自己那条 `speech`）。语音关着时由调用方决定不登记。
func register(text: String, reveal: bool, kind: String, tomb := false) -> Dictionary:
	_next_id += 1
	var rec := {
		"id": _next_id, "text": text, "reveal": reveal, "kind": kind, "tomb": tomb,
		"t": now_ms(), "got": false, "group": -1, "played": false,
		"armed": false, "displayed": false, "released": false, "wait_display": false,
	}
	_pairs.append(rec)
	return rec

## 本地要发起一条「只有文字」的调用（`undo` / `reset_game` / `set_allow_pass`）或本地台词（开场问候）时打标记
func mark_text_only() -> void:
	_text_only_left = 1
	_text_only_ms = now_ms()

## 打过标记的下一条 **`kind == "ai"`** 的台词不登记配对记录（它没有语音，也就没什么可配的）。
## 只认 `ai`：点评(`ai_comment`)/落子宣告(`ai_move`)/走神搭话(`ai_idle`) 各有自己的管线，
## 万一标记没被那条提示消费掉，也不该被它们顺手吃掉（否则那条点评会被当成「没有语音」）。
func take_text_only_mark(kind: String) -> bool:
	if _text_only_left <= 0 or kind != "ai":
		return false
	_text_only_left = 0
	return now_ms() - _text_only_ms <= TEXT_ONLY_TTL_MS

# ================= 配对（speech 到达） =================

## 校验并取走配对位：只认**最早那条还没配上**的记录（后端 `message→speech` 严格串行，
## 所以到达顺序就是配对顺序）。返回 `{}` 表示这条 `speech` 不该出声（原因见 `last_drop_reason`）。
func accept_speech(turn_id: String, seq: int) -> Dictionary:
	last_drop_reason = "stale"
	if turn_id == _last_turn and seq <= _last_seq:
		return {}                                  # 同一回合内 seq 必须递增（兜 WS 重连后的乱序/重发）
	_last_turn = turn_id                           # 跨回合时 seq 会重置：换 turn_id 就重新从 1 起算
	_last_seq = seq
	for rec in _pairs:
		if not bool(rec["got"]):
			rec["got"] = true
			last_drop_reason = ""
			return rec
	last_drop_reason = "orphan"
	return {}

## 记下这条台词的分组号：语音开播 / 被判无声时按分组号精确回查
func set_group(rec: Dictionary, group: int) -> void:
	rec["group"] = group
	_by_group[group] = rec

func group_of(group: int) -> Dictionary:
	return _by_group.get(group, {})

# ================= 显示决策 =================

## 进入显示队列（`Main._deliver_msg` 跑到这条时调用）：`wait_display=true` 表示文字要等这段语音开播
func arm(rec: Dictionary, wait_display: bool) -> void:
	rec["armed"] = true
	rec["wait_display"] = wait_display

## 现在就把这条文字显示出来（已经显示过 / 占位记录不重复显示）：真的显示了才发 `display_requested`
func display(rec: Dictionary) -> bool:
	if rec.is_empty() or bool(rec["displayed"]) or bool(rec["tomb"]):
		return false
	rec["displayed"] = true
	display_requested.emit(str(rec["text"]), bool(rec["reveal"]))
	return true

## 把还等着显示的全放出来（整段模式的一段语音开口 / 语音出错兜底）：返回放了几条。
## 只放**已 arm** 的（还没轮到的台词仍按投递顺序等 `_deliver_msg`）。
func flush_pending() -> int:
	var n := 0
	for rec in _pairs.duplicate():
		if not bool(rec["armed"]) or bool(rec["displayed"]) or bool(rec["tomb"]):
			continue
		if display(rec):
			n += 1
	return n

## 还有等着显示的文字吗（「思考中…」占位 / 出错兜底都看它）
func has_pending() -> bool:
	for rec in _pairs:
		if bool(rec["armed"]) and not bool(rec["displayed"]) and not bool(rec["tomb"]):
			return true
	return false

# ================= 结果与清理 =================

## 这条语音有结论了（已开播 / 判定无声 / 被取消 / 被丢弃）：出表，并通知调用方刷新锁
func finish(rec: Dictionary) -> void:
	if rec.is_empty():
		return
	rec["played"] = true
	if int(rec["group"]) > 0:
		_by_group.erase(int(rec["group"]))
	prune()
	resolved.emit()

## 出表（只留还没出结果的）
func prune() -> void:
	var kept: Array[Dictionary] = []
	for rec in _pairs:
		if bool(rec["played"]):
			continue
		kept.append(rec)
	_pairs = kept

## 配对位保鲜：过了窗口还没配上的记录清出（腾出配对位，免得它吃掉后面那条台词的语音）。
##
## **不变量（T25）**：**清出之前，只要这条台词的文字还没显示过，就先把它显示出来**——绝不静默丢台词。
## 于是只有两种记录可以直接清掉：已经显示过的，与本地作废的占位（`tomb`）；还没轮到 `_deliver_msg`
## 的（未 `arm`）**留着不清**——它马上会被 `arm`，此刻清掉就等于丢字（那时 `_deliver_msg` 只在
## `played` 时才显示，而这条永远不会有 `played`）。
## 返回被清出的台词（供调用方记一行日志）。
func prune_expired() -> Array[String]:
	var out: Array[String] = []
	var now := now_ms()
	var kept: Array[Dictionary] = []
	for rec in _pairs:
		if bool(rec["got"]) or now - int(rec["t"]) <= SPEECH_PAIR_TTL_MS:
			kept.append(rec)
			continue
		if bool(rec["tomb"]):
			continue                                   # 占位没有文字：直接清掉
		if not bool(rec["displayed"]):
			if not bool(rec["armed"]):
				kept.append(rec)                       # 还没轮到 _deliver_msg：留着，别丢它
				continue
			display(rec)                               # 先把文字放出来，再腾出配对位
		out.append(str(rec["text"]))
	_pairs = kept
	return out

## 回合结束 / WS 断开后，等不到语音的文字不能一直等：直接显示（落子也照常揭示）。
## `env` 是外部事实：`turn_active`=后端这一轮还在跑 / `playing`=音频在播或排队 / `fetching`=还有音频请求在途。
## 只放文字、**不拆配对位**——那条语音可能还在后端合成，等它到了照样要占它自己的位置、照样要出声；
## 放掉之后这条不再算「她在忙」（`released`），否则后端一条 `speech` 都不发时棋盘会被锁死。
## `forced=true`（连接断了）：在途的 `speech` 已经丢了，连配对位一起清掉，返回放掉的文字条数。
## 配对位的 TTL 清出在 `prune_expired()`（由调用方按帧调用），这里不负责。
func release_stale(env: Dictionary, forced := false) -> int:
	if not forced:
		if bool(env.get("turn_active", false)):
			return 0                       # 回合还在跑：她的语音还没发完
		if audio_in_flight(env):
			return 0                       # 还有音频在播 / 排队 / 正在取：等它
	# 走到这里说明「不会有音频再挡着了」：全表标记 released（含已配对的，它们等会儿照常出声，
	# 只是不再算「她在忙」）。注意：非 forced 时 audio_in_flight() 为假已保证没有 got&&!played 的记录。
	for rec in _pairs:
		rec["released"] = true
	var n := 0
	for rec in _pairs.duplicate():
		if bool(rec["armed"]) and not bool(rec["displayed"]) and not bool(rec["tomb"]):
			if display(rec):
				n += 1
	if forced:
		for rec in _pairs.duplicate():
			if not bool(rec["got"]):
				rec["released"] = true
				finish(rec)                # 连不上了：这条语音不会再来了
	return n

## 走神搭话整批作废：文字不再显示；已经配上 / 在取的记录出表（音频由调用方 `cancel_kind("idle")` 停播）。
## 还没配上 speech 的留成占位：吃掉它自己那条语音，免得错配到下一条台词。
func forget_idle() -> void:
	var done: Array[Dictionary] = []
	for rec in _pairs:
		if str(rec["kind"]) != "idle":
			continue
		rec["displayed"] = true            # 这段文字不再显示
		if not bool(rec["got"]):
			rec["tomb"] = true             # 还没配对：留着吃掉它自己那条 speech
		elif not bool(rec["played"]):
			done.append(rec)
	for rec in done:
		finish(rec)

# ================= 状态查询 =================

## 她这一侧还有语音的事没完（排队 / 播放 / 取音频 / 等配对）：棋盘锁、语气词让位、闲置判定都看它。
## 「音频还在路上」也必须算——否则后端 busy 一结束就会在音频到达之前提前解锁棋盘。
## 已经不再等语音的（占位 / 出过结果 / 已被 `release_stale` 放掉的）不算：不能把棋盘锁死。
func busy(env: Dictionary) -> bool:
	if bool(env.get("playing", false)):
		return true
	for rec in _pairs:
		if not bool(rec["tomb"]) and not bool(rec["played"]) and not bool(rec["released"]):
			return true
	return false

## 音频侧还有活干（在播 / 排队 / 正在取）：这时不能把等待中的文字当「等不到语音」放出来
func audio_in_flight(env: Dictionary) -> bool:
	if bool(env.get("playing", false)) or bool(env.get("fetching", false)):
		return true
	for rec in _pairs:
		if bool(rec["got"]) and not bool(rec["played"]):
			return true
	return false

## 她这一侧还没收尾（棋盘该锁着）：回合还在跑 / 调用方的闩还挂着 / 语音侧还没完。
## 这是第四个「她还在忙吗」谓词（`_pending_ai`→`has_pending` / `_tts_playing` / `audio_in_flight` / 这个）。
## 与 `busy` 的分工：`busy` 只看语音侧（在播 + 表里还有没出结果的），这里再加上「回合」与「闩」。
func hold_active(env: Dictionary, hold_latch := false) -> bool:
	return bool(env.get("turn_active", false)) or hold_latch or busy(env)

# —— 小访问器（免得调用方到处写字段名） ——

func is_tomb(rec: Dictionary) -> bool:
	return bool(rec.get("tomb", false))

func played(rec: Dictionary) -> bool:
	return bool(rec.get("played", false))

func wait_display(rec: Dictionary) -> bool:
	return bool(rec.get("wait_display", false))

func kind_of(rec: Dictionary) -> String:
	return str(rec.get("kind", ""))

func pair_count() -> int:
	return _pairs.size()

func pending_count() -> int:
	var n := 0
	for rec in _pairs:
		if bool(rec["armed"]) and not bool(rec["displayed"]) and not bool(rec["tomb"]):
			n += 1
	return n
