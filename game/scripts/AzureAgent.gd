class_name AzureAgent
extends Node
## 对局编排：玩家落子 → Azure 点评 → Azure 思考并落子（等价于网页版 /api/* 的全部逻辑）

signal message(kind: String, text: String)          # kind: ai / user / system
signal state_changed(payload: Dictionary)
signal info_changed(winrate: float, score: float)
signal busy_changed(busy: bool)
signal emotion(kind: String)                        # 表情反应：delight / pleased / puzzled / worried

var st := GameState.new()
var katago: KataGoEngine = null
var llm: LLMClient = null

var busy := false
var limit_len := false          # 开启后追加「回复尽量短」要求：语音合成时间随字数下降（右侧栏可切）
var _reveal_pending := false    # 已落到内部棋谱但尚未显示：等她「落子宣告」的语音开始时再 reveal_move()

## 限制回复长度时追加的约束（在不动原人设的前提下，单独附在末尾）
const LEN_NOTE := "\n\n【本次额外要求 · 优先遵守】\n- 点评 / 提示：只回 1 句，尽量 20 字以内\n- 普通聊天：最多 2 句，尽量 40 字以内\n- 不复述局面、不客套，直接说结论就好"

func setup(engine: KataGoEngine, client: LLMClient) -> void:
	katago = engine
	llm = client

# ================= 工具 =================

## 渲染成 Python 风格的列表（与网页版提示词里的取值格式保持一致）
static func _py_list(arr: Array) -> String:
	var parts: Array[String] = []
	for v in arr:
		parts.append("'%s'" % str(v))
	return "[" + ", ".join(parts) + "]"

static func _pct(v: float) -> String:
	return "%.1f%%" % (v * 100.0)

## 把 KataGo 的形势数据（胜率/目差/逐点归属估算的双方实地）整理成一段文字，存进 st.last_katago，供 Azure 上下文使用
func _update_katago_brief(analysis: Dictionary, num_moves: int) -> void:
	if analysis.is_empty() or analysis.has("error"):
		return
	var conv := AzurePrompts.to_black_perspective(analysis, num_moves)
	var wr: float = conv[0]
	var sc: float = conv[1]
	var who := "Estarth（黑）稍优" if sc > 0.5 else ("Azure（白）稍优" if sc < -0.5 else "双方接近")
	var s := "黑方（Estarth）胜率 %.1f%%；目差 %+.1f 目（正数=黑/Estarth 领先，含贴目）；整体 %s。" % [wr * 100.0, sc, who]
	var own: Array = analysis.get("ownership", [])
	if own.size() > 0:
		var b := 0.0
		var w := 0.0
		for v in own:
			var f := float(v)
			if f > 0.0:
				b += f
			else:
				w -= f
		s += " 实地估算：Estarth（黑）约 %.0f 目，Azure（白）约 %.0f 目（按逐点归属估算，仅供参考）。" % [b, w]
	st.last_katago = s

func _sys_msg() -> Dictionary:
	return {"role": "system", "content": AzurePrompts._t(AzurePrompts.system_prompt()) + (LEN_NOTE if limit_len else "")}

func _user_msg(content: String) -> Dictionary:
	return {"role": "user", "content": content}

func _moves_with(color: String, gtp: String) -> Array:
	var out := st.moves.duplicate()
	out.append([color, gtp])
	return out

func _set_busy(v: bool) -> void:
	busy = v
	busy_changed.emit(v)

## 由界面在她「落子宣告」的语音开始播放时调用：把这一手真正显示到棋盘上
func reveal_move() -> void:
	if not _reveal_pending:
		return
	_reveal_pending = false
	state_changed.emit(st.payload())

## 括号里带「着棋点位」的舞台提示不需要显示，例如（白棋落在 C4）/（第7手 Q16）/（C4）
const _PAREN_GROUP_PATTERN := "[（(][^（()）\\n]{0,40}[）)]"
const _MOVE_HINT_PATTERN := "[A-HJ-Ta-hj-t]\\s?\\d{1,2}|落子|落在|虚着|pass|着子|第\\s?\\d+\\s?手"

static func clean_reply(text: String) -> String:
	if text == "":
		return text
	var grp := RegEx.new()
	var hint := RegEx.new()
	if grp.compile(_PAREN_GROUP_PATTERN) != OK or hint.compile(_MOVE_HINT_PATTERN) != OK:
		return text
	var out := text
	var groups := grp.search_all(out)
	for i in range(groups.size() - 1, -1, -1):      # 从后往前删，避免偏移
		var m := groups[i]
		if hint.search(m.get_string()) != null:
			out = out.substr(0, m.get_start()) + out.substr(m.get_end())
	out = out.strip_edges()
	while out.contains("\n\n\n"):
		out = out.replace("\n\n\n", "\n\n")
	return out if out != "" else text

## 所有对外展示的 LLM 回复统一过一遍清洗
func _reply(messages: Array, max_tokens := 300) -> String:
	return clean_reply(await llm.chat(messages, max_tokens))

## 聊天内容的粗分类 → 表情（本地关键词判断，不额外打扰 LLM）
static func chat_emotion(text: String) -> String:
	var t := text.to_lower()
	for kw in ["哈哈", "hh", "笑", "有趣", "好玩", "233", "草"]:
		if t.contains(kw):
			return "laugh"
	for kw in ["喜欢", "爱你", "谢谢", "温柔", "可爱", "厉害", "真棒", "漂亮", "好看", "帅", "辛苦了"]:
		if t.contains(kw):
			return "delight"
	for kw in ["难过", "伤心", "累", "烦", "不开心", "生气", "讨厌", "输了", "失败", "糟糕", "唉", "哭"]:
		if t.contains(kw):
			return "sad"
	for kw in ["？", "?", "吗", "怎么", "为什么", "什么", "如何", "能不能", "是不是"]:
		if t.contains(kw):
			return "puzzled"
	return "pleased"

# ================= 玩家落子 =================

func player_move(x: int, y: int) -> void:
	if busy:
		return
	var legal := st.board.is_legal(x, y, "black")
	if not legal["ok"]:
		message.emit("ai", "这手不成立：%s" % legal["err"])
		state_changed.emit(st.payload())          # 让界面撤销乐观落子
		return
	_set_busy(true)
	var gtp := Coords.xy_to_gtp(x, y)
	var n := st.moves.size()

	# ① 玩家落子前局面：缓存复用，否则现算（黑方视角，用于给这一手排名）
	var analysis: Dictionary = {}
	if st.pre_analysis != null and int(st.pre_analysis["n"]) == n:
		analysis = st.pre_analysis["a"]
	else:
		analysis = await katago.analyze(st.moves)
		if analysis.has("error"):
			push_warning("[KataGo] " + str(analysis["error"]))
			analysis = {}
		else:
			st.pre_analysis = {"n": n, "a": analysis}
	var winrate_before := 0.5
	var move_infos: Array = []
	if not analysis.is_empty():
		winrate_before = float((analysis.get("rootInfo", {}) as Dictionary).get("winrate", 0.5))
		move_infos = analysis.get("moveInfos", [])
	var player_rank := -1
	for i in mini(move_infos.size(), 5):
		if str((move_infos[i] as Dictionary).get("move", "")) == gtp:
			player_rank = i
			break

	# ② 玩家落子后局面（轮到白方）：用于选择 Azure 的应手
	var after := await katago.analyze(_moves_with("B", gtp))
	var after_infos: Array = []
	var after_black_wr := 0.5
	var after_black_score := 0.0
	if after.has("error"):
		push_warning("[KataGo] " + str(after["error"]))
	else:
		after_infos = after.get("moveInfos", [])
		var conv := AzurePrompts.to_black_perspective(after, n + 1)
		after_black_wr = conv[0]
		after_black_score = conv[1]
		st.after_analysis = {"n": n + 1, "a": after}
		_update_katago_brief(after, n + 1)            # 当前棋面的引擎形势，供 Azure 上下文

	# ③ 提交玩家落子（先存快照，供俗手回档）
	st.snapshots.append(st.make_snapshot())
	st.board.play(x, y, "black")
	st.add_move("B", gtp)
	var turn_index := st.snapshots.size() - 1

	# ④ 俗手判定：不在我算的前5，或胜率明显下滑
	var drop := winrate_before - after_black_wr
	if player_rank < 0 or drop > 0.10:
		var reason := "不在我算的前5候选" if player_rank < 0 else "胜率下滑%d%%" % int(round(drop * 100.0))
		st.record_bad_move(turn_index, gtp, reason)
	state_changed.emit(st.payload())

	# ④′ 表情反应：好手欣喜、一般平和、俗手先惊讶再失落（表情由界面接到化身上播放）
	if player_rank == 0:
		emotion.emit("delight")
	elif player_rank < 0 or drop > 0.10:
		emotion.emit("worried")
	elif player_rank < 3:
		emotion.emit("pleased")
	else:
		emotion.emit("puzzled")

	# ⑤ Azure 点评这一手
	var eval_prompt := ""
	if player_rank == 0:
		eval_prompt = "Estarth下了 %s，这是我算的最优手！胜率%s。轻描淡写夸一下，1-2句。" % [gtp, _pct(winrate_before)]
	elif player_rank > 0 and player_rank < 3:
		eval_prompt = "Estarth下了 %s，Top%d，胜率%s。肯定一下，1-2句。" % [gtp, player_rank + 1, _pct(winrate_before)]
	elif player_rank >= 0:
		eval_prompt = "Estarth下了 %s，不在前5。委婉指出，给点鼓励，1-2句。" % gtp
	else:
		eval_prompt = "Estarth下了 %s，没进我算的前5。慵懒表示不太理解，1-2句。" % gtp
	eval_prompt += " 只评价Estarth这一手（1句）；不要提你自己接下来打算下哪里，不要复述胜率数值。"
	var player_eval: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, eval_prompt, "after_player", false, st.last_katago)),
	], 300)
	st.log_memory("E", player_eval, st.moves.size())      # 给「Estarth 这手」的分析打标签存档
	message.emit("ai", player_eval)

	# ⑥ Azure 思考并落子
	await _ai_turn(after_infos, after_black_wr, after_black_score)
	_set_busy(false)

func _ai_turn(after_infos: Array, fallback_wr: float, fallback_score: float) -> void:
	var pick := _pick_ai_move(after_infos)
	var ai_gtp: String = pick[0]
	var ai_x: int = pick[1]
	var ai_y: int = pick[2]

	# 用意：落子前说明「我准备下在哪、想做什么」（此时棋谱里还没有她这一手，上下文才不会自相矛盾）
	var think_hint := "你准备虚着（pass）" if ai_gtp == "pass" else "你准备下在 %s" % ai_gtp
	var think_prompt := "轮到你下了。%s。用一句话说出你这一手的用意（打算下在哪、想做什么），直接说；不要复述刚才对Estarth那手的点评。" % think_hint
	await get_tree().create_timer(0.35).timeout
	var think_msg: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, think_prompt, "own_turn", false, st.last_katago)),
	], 80)
	message.emit("ai_move", think_msg)                    # 这条语音开始播放时，界面才把她的子放上棋盘

	# 真正落子：先落到内部棋谱；棋盘可见的更新延后到语音开始时（reveal_move）
	if ai_gtp == "pass":
		st.board.pass_move()
		st.add_move("W", "pass")
	else:
		st.board.play(ai_x, ai_y, "white")
		st.add_move("W", ai_gtp)
	_reveal_pending = true
	if ai_gtp == "pass":
		message.emit("ai", "（Azure本手选择了虚着）")

	# ⑦ 先算「Azure 落子后局面」（轮到黑方）→ 供信息栏与下面「局势」的上下文使用
	var post := await katago.analyze(st.moves)
	var wr := fallback_wr
	var sc := fallback_score
	if post.has("error"):
		push_warning("[KataGo] " + str(post["error"]))
	else:
		var ri: Dictionary = post.get("rootInfo", {})
		wr = float(ri.get("winrate", 0.5))
		sc = float(ri.get("scoreLead", 0.0))
		_update_katago_brief(post, st.moves.size())       # 更新为她落子后的最新形势
	info_changed.emit(wr, sc)

	# 局势：一句话说清当前形势（谁占优、关键处），不复述前文（上下文里的 KataGo 形势已是最新）
	var ai_eval_prompt := ""
	if ai_gtp == "pass":
		ai_eval_prompt = "你（Azure）选择了虚着。用一句话说说现在的局势（谁占优、关键处在哪）；不要复述刚才说过的话。"
	else:
		ai_eval_prompt = "你（Azure）下了 %s。用一句话说说现在的局势（谁占优、关键处在哪）；不要复述刚才说过的话。" % ai_gtp
	var ai_eval: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, ai_eval_prompt, "just_moved", false, st.last_katago)),
	], 120)
	st.log_memory("A", ai_eval, st.moves.size())          # 给「Azure 自己这手」的局势记录打标签存档
	message.emit("ai", ai_eval)
	# 不在这里 emit state_changed：落子的可见更新由 reveal_move() 在她这段语音开始时触发

## 按引擎候选顺序取第一个在当前棋盘合法的点；不允许虚着时绝不虚着
func _pick_ai_move(move_infos: Array) -> Array:
	for m in move_infos:
		if typeof(m) != TYPE_DICTIONARY:
			continue
		var mv := str((m as Dictionary).get("move", ""))
		if mv == "":
			continue
		if mv == "pass":
			if st.allow_pass:
				return ["pass", -1, -1]
			continue
		var xy := Coords.gtp_to_xy(mv)
		if xy.x >= 0 and st.board.is_legal(xy.x, xy.y, "white")["ok"]:
			return [mv, xy.x, xy.y]
	if st.allow_pass:
		return ["pass", -1, -1]
	# 候选全部不可用：就近找任意合法点，保证 Azure 一定落子
	var near := st.last_played_xy()
	var best := Vector2i(-1, -1)
	var best_d := 1e18
	for y in GoBoard.SIZE:
		for x in GoBoard.SIZE:
			if st.board.at(x, y) != GoBoard.EMPTY:
				continue
			var dx := x - (near.x if near.x >= 0 else 9)
			var dy := y - (near.y if near.y >= 0 else 9)
			var d := float(dx * dx + dy * dy)
			if d < best_d and st.board.is_legal(x, y, "white")["ok"]:
				best_d = d
				best = Vector2i(x, y)
	if best.x >= 0:
		return [Coords.xy_to_gtp(best.x, best.y), best.x, best.y]
	return ["pass", -1, -1]

# ================= 其余功能 =================

## 💡 提示：给方向性提示，不直接说坐标
func hint() -> void:
	if busy:
		return
	_set_busy(true)
	var n := st.moves.size()
	var analysis := await katago.analyze(st.moves)
	if analysis.has("error"):
		push_warning("[KataGo] " + str(analysis["error"]))
		message.emit("ai", "嗯...我这边一时算不清，稍等再试吧~")
		_set_busy(false)
		return
	st.pre_analysis = {"n": n, "a": analysis}
	var tops: Array = []
	for m in (analysis.get("moveInfos", []) as Array).slice(0, 3):
		tops.append(str((m as Dictionary).get("move", "")))
	_update_katago_brief(analysis, n)
	var prompt := "当前局面：%s。我算到的好点：%s。Estarth还没落子，给个方向性提示，不要直接说坐标，1-2句。" % [
		AzurePrompts.board_summary(st.move_seq), _py_list(tops)]
	var msg: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, prompt, "observe", false, st.last_katago)),
	], 300)
	message.emit("ai", msg)
	_set_busy(false)

## 局面分析：以 Azure（执白）的视角解读局势
func analyze_position() -> void:
	if busy:
		return
	if st.moves.is_empty():
		message.emit("ai", "棋盘还空着呢~ 先落几手，我再帮你看看形势。")
		return
	_set_busy(true)
	var n := st.moves.size()
	var analysis := await katago.analyze(st.moves)
	if analysis.has("error"):
		push_warning("[KataGo] " + str(analysis["error"]))
		message.emit("ai", "唔…我这边一时算不清，稍后再让我看看吧。")
		_set_busy(false)
		return
	var conv := AzurePrompts.to_black_perspective(analysis, n)
	var black_wr: float = conv[0]
	var score_black: float = conv[1]
	var azure_wr := 1.0 - black_wr
	var tops: Array = []
	for m in (analysis.get("moveInfos", []) as Array).slice(0, 5):
		tops.append(str((m as Dictionary).get("move", "")))
	_update_katago_brief(analysis, n)
	var lead := "你略占上风" if azure_wr > 0.5 else ("局面胶着" if azure_wr > 0.45 else "Estarth领先")
	var prompt := "当前局面：%s，共%d手。你（Azure，执白）算出的判断：白方胜率约%s（%s），目差%s（正数是你领先）。你在考虑的几个点：%s。请以Azure口吻分析当前局势3-4句：局面走向、关键处或薄弱处，可以点醒Estarth，语气慵懒但专业。" % [
		AzurePrompts.board_summary(st.move_seq), n, _pct(azure_wr), lead, "%+.1f" % (-score_black), _py_list(tops)]
	var msg: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, prompt, "analyze", false, st.last_katago)),
	], 350)
	message.emit("ai", "📊 " + msg)
	info_changed.emit(black_wr, score_black)
	_set_busy(false)

## 自由聊天
func chat(text: String) -> void:
	if busy:
		return
	if text.strip_edges() == "":
		return
	st.chat_history.append({"role": "user", "content": text})
	emotion.emit(chat_emotion(text))       # 按聊天内容给个即时表情反应
	_set_busy(true)
	var msgs: Array = [
		_sys_msg(),
		_user_msg(AzurePrompts.context_block(st.moves, st.memory_log, st.chat_history, "chat", true, st.last_katago)),
		{"role": "assistant", "content": "嗯，我都记着呢"},
	]
	var hist: Array = st.chat_history
	var start := maxi(0, hist.size() - 6)
	for i in range(start, hist.size()):
		msgs.append(hist[i])
	msgs.append({"role": "user", "content": text})
	var reply: String = await _reply(msgs, 400)
	st.chat_history.append({"role": "assistant", "content": reply})
	message.emit("ai", reply)
	_set_busy(false)

## 闲置搭话：Estarth 一段时间没动静时，Azure 主动说一句短话（主场景在空闲计时到点时调用）
const IDLE_LINES := [
	"唔…还在吗？我在这里等你哦。",
	"在想什么呢？要不要再来一手？",
	"嗯…安静得都能听到风了呢。",
	"要不要聊聊天？我有点无聊了~",
]

func idle_remark() -> void:
	if busy:
		return
	var prompt := "Estarth 已经有一段时间没有动静了，你有点好奇他在做什么。主动对他说一句很短的话（20字以内）：可以歪头看看他、问问他还在不在、要不要继续下棋或聊聊天。慵懒、亲切，1句。"
	var reply: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.context_block(st.moves, st.memory_log, st.chat_history, "chat", true, st.last_katago)),
		{"role": "assistant", "content": "嗯，我都记着呢"},
		{"role": "user", "content": prompt},
	], 90)
	if reply.strip_edges() == "" or reply.begins_with("调用出错") or reply.contains("脑子有点转不过来"):
		reply = str(IDLE_LINES[randi() % IDLE_LINES.size()])     # LLM 不可用时兜底台词
	st.chat_history.append({"role": "assistant", "content": reply})
	message.emit("ai", reply)

## 对局总结
func summarize(note: String) -> void:
	if busy:
		return
	if st.moves.is_empty():
		message.emit("ai", "还没下过棋呢~")
		return
	_set_busy(true)
	var n := st.moves.size()
	var final_wr := 0.5
	var final_score := 0.0
	var analysis := await katago.analyze(st.moves)
	if analysis.has("error"):
		push_warning("[KataGo] " + str(analysis["error"]))
	else:
		var conv := AzurePrompts.to_black_perspective(analysis, n)
		final_wr = conv[0]
		final_score = conv[1]
	_update_katago_brief(analysis, n)
	var seq := " → ".join(PackedStringArray(st.move_seq))
	var prompt := "对局结束，共%d手。\n序列：%s\n最终黑胜率：%s，目差：%s\nEstarth备注：%s\n\n写总结：1.对局回顾 2.给Estarth的改进建议 2-3条 3.你的心情\n用Azure口吻，150-250字，慵懒真诚。" % [
		n, seq, _pct(final_wr), "%+.1f" % final_score, note]
	var text: String = await _reply([
		_sys_msg(),
		_user_msg(AzurePrompts.with_context(st.moves, st.memory_log, st.chat_history, prompt, "summary", false, st.last_katago)),
	], 600)
	st.finished = true
	message.emit("ai", "📋 对局总结\n" + text)
	_set_busy(false)

## 俗手回档：撤销该手及其后的 Azure 应手，回到该手之前
func undo(turn_index: int) -> void:
	if busy:
		return
	if turn_index < 0 or turn_index >= st.snapshots.size():
		message.emit("ai", "这个回档点已经不存在了哦")
		return
	var s: Dictionary = st.snapshots[turn_index]
	st.board = s["board"]
	st.moves = s["moves"]
	st.move_seq = s["move_seq"]
	st.memory_log = s["memory_log"]
	st.snapshots = st.snapshots.slice(0, turn_index)
	var kept: Array = []
	for b in st.bad_moves:
		if int((b as Dictionary)["turn_index"]) < turn_index:
			kept.append(b)
	st.bad_moves = kept
	st.pre_analysis = null
	st.after_analysis = null
	state_changed.emit(st.payload())
	message.emit("ai", "嗯~ 那我们退回第%d手，重新想想…" % st.moves.size())

func reset_game() -> void:
	var keep_pass := st.allow_pass
	st.reset()
	st.allow_pass = keep_pass
	state_changed.emit(st.payload())
	message.emit("ai", "嗯~ 重新来吧...")

func set_allow_pass(v: bool) -> void:
	st.allow_pass = v
	message.emit("ai", "好~ 那这局我允许自己虚着。" if v else "嗯，这局我不会虚着，会认真陪你下。")
	state_changed.emit(st.payload())