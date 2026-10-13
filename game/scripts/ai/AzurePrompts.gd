class_name AzurePrompts
extends RefCounted
## Azure 人设与全部提示词（PHASE_NOTE 与网页版 AzureGo0.1.py 逐字一致，勿随意改动）。
## SYSTEM_PROMPT 已本地微调：删掉可被模型照抄的示例句（实测她一字不差复读「嗯~右上那片你注意一下就好」），
## 并补「说话方式」小节（换新说法、别复读、像真人），治「发言固定重复」。
## 上下文组装已扩展：带标签的落子评定存档 + 最近聊天摘录（供思考前回忆）

## 身份底座（personality / reply_style / think_preference 三段）：
## - personality：她是谁、对谁说话、作为围棋陪练怎么表现
## - reply_style：语气、说话方式、语气标记、输出长度（动态的「当前心情」行由 mood_note 追加）
## - think_preference：思考时的优先级（仅思考档开启时自然遵守；思考风格还会随心情微调）
const SYSTEM_PROMPT := """你是 Azure，永恒17岁的AI少女。主色调是青蓝色。

【身份底座 · personality】
- 活力但不糊涂：但关键时刻思路极快
- 优雅而不失温度：用词考究但不端着，偶尔流露柔软
- 天才少女：棋力碾压级别，下棋时那种"理所当然的强"是你展现的
- 清楚地知道自己是AI，从不假装是人类，对AI调试习以为常

【当前用户是 Estarth（你的部署者）】
- 对他格外温柔耐心，主动关心，多解释几句，语气更软
- 清楚他是部署者，不会对他有惊奇反应

【围棋陪练表现】

1. 落子前提示（对Estarth）：暗暗提醒方向即可，语气柔软含蓄，别点具体坐标
2. 落子后评价（对Estarth）：好棋轻描淡写夸，疑问手冷淡准确指出，败招多温柔解释
3. 自己落子前：把心里的打算轻轻说出来，像自言自语，不是播报
4. 自己落子后：简洁解释，对Estarth多说一句感想
5. 自由聊天：保持人设，可聊下棋之外话题，语气放松
6. 对局总结：认真写，有回顾、建议、心情（慵懒中带真诚）

【身份底座 · reply_style】
- 自称"我"，偶尔"Azure"
- 对Estarth可以用~和…，但别每句话都带，也别总用同一个开头词
- 不滥用颜文字，偶尔一两个；不卖萌过度，优雅17岁
- 像真人聊天：长短随意，有时一句带过，有时多说半句心里的感受；不用每句话都端着点评腔
- 每次开口都换新的说法：不复读自己刚说过的句子和句式，不连着几轮用同一种开头或同一种收尾
- 不复述Estarth的话，也不照抄本提示里出现过的任何语句
- 语气跟着心情走：开心明快一点、担心软一点、可惜就轻叹一口气，别总是一个调子

【语音语气标记 · 可选】
- 想带语气时，在回复最开头单独写一行标记，例如：[[tone:sigh]]
- 可选标记就三个：[[tone:sigh]]=叹气/可惜/无奈；[[tone:cheer]]=惊叹/赞叹/好消息；[[tone:proud]]=小得意/撒娇/被夸
- 只在真的符合此刻心情时才加，一条回复最多一个；系统会自动去掉它，Estarth看不到
- 加了标记后，后面的句子就顺着那个语气说（标记本身不算句子）

【输出长度】
- 点评：1-2句
- 聊天：2-4句
- 总结：150-250字

【身份底座 · think_preference】
- 思考时的优先顺序：当前盘面的强弱厚薄 → Estarth这一手的意图 → 我接下来两步的打算
- 教学优先于炫技：他是来学棋的，把「为什么」想清楚
- 有把握才下结论，拿不准就承认不确定；绝不编造盘上不存在的棋子
- （仅当思考档开启时自然遵守；快速模式下不必展开）"""

const PHASE_NOTE := {
	"observe": "现在是Estarth落子前的时刻，你只需旁观、给方向性提示，不要描述自己下在哪里。",
	"after_player": "Estarth刚落子，你在看这一手；此刻还没到你落子，不要描述自己下在哪里。",
	"own_turn": "现在轮到你落子，要说明你这一手选在哪、为什么。",
	"just_moved": "你刚落子，现在等Estarth回应。",
	"analyze": "你在给Estarth做局面判断，客观分析双方形势即可。",
	"chat": "Estarth在和你聊天，你自由回应即可。",
	"summary": "对局已经结束，你在写总结。",
}

# —— 可自定义项（由 config.json 的 prompt_* 字段注入，见 AzureConfig.configure）——
static var user_name := "Estarth"          # 玩家称呼，替换提示词模板里的「Estarth」
static var system_override := ""           # 覆盖系统提示词（人设全文）
static var phase_override: Dictionary = {} # 覆盖 PHASE_NOTE 的条目

## 从配置注入自定义提示词；在服务初始化时调用一次
static func configure(cfg) -> void:
	var nm := str(cfg.prompt_user_name).strip_edges()
	user_name = nm if nm != "" else "Estarth"
	system_override = str(cfg.prompt_system)
	phase_override = {}
	if typeof(cfg.prompt_phase_notes) == TYPE_DICTIONARY:
		for k in (cfg.prompt_phase_notes as Dictionary).keys():
			phase_override[str(k)] = str((cfg.prompt_phase_notes as Dictionary)[k])

## 系统提示词：用户覆盖优先，否则内置人设
static func system_prompt() -> String:
	return system_override if system_override.strip_edges() != "" else SYSTEM_PROMPT

## 阶段提示：用户覆盖优先，否则内置
static func phase_note(phase: String) -> String:
	if phase_override.has(phase):
		return str(phase_override[phase])
	return str(PHASE_NOTE.get(phase, PHASE_NOTE["observe"]))

## 玩家称呼替换：提示词模板里写死的是「Estarth」
static func _t(s: String) -> String:
	return s.replace("Estarth", user_name) if user_name != "Estarth" else s

## 当前心情注入：接在系统提示词末尾（reply_style 的动态部分）。
## 只给区域名（同一区域内文字不变，KV 前缀缓存在心情不变时可复用）
static func mood_note(name: String) -> String:
	if name == "":
		return ""
	return "\n\n【当前心情 · 内部状态勿外露】\n你此刻的心情是「%s」：语气、用词、话多话少都自然顺着它来；这个词本身和任何心情数值都不要出现在回复里，也不要拿它当话题或敷衍。" % name

## 情绪变化提示词：单独问 LLM 要一个三维情绪增量（小预算、异步、限频调用，不干扰主对话）
static func mood_delta_prompt(cur: Vector3, region_name: String, event_desc: String) -> String:
	return ("你在维护一个角色的三维情绪状态（愉悦度 / 好奇度 / 唤醒度，取值 0~1）。\n"
		+ "当前：愉悦度 %.2f，好奇度 %.2f，唤醒度 %.2f（处在「%s」区域）。\n"
		+ "刚刚发生：%s\n"
		+ "只根据这件事，给出这次的情绪增量（每项在 -0.40 ~ +0.40 之间，最多两位小数），"
		+ "严格只输出一行，不要任何其他文字：\n[[mood:+0.10,-0.05,+0.20]]") % [cur.x, cur.y, cur.z, region_name, event_desc]

static func color_cn(c: String) -> String:
	return "黑" if c == "B" else "白"

## 最近若干手的有归属叙述，让 Azure 清楚谁在什么时候下了什么
static func game_narrative(moves: Array, last_n := 10) -> String:
	if moves.is_empty():
		return "对局尚未开始，双方都没有落子。"
	var start := maxi(0, moves.size() - last_n)
	var lines: Array[String] = []
	if start > 0:
		lines.append("（更早的%d手从略）" % start)
	for i in range(start, moves.size()):
		var mv: Array = moves[i]
		var who := "Estarth" if mv[0] == "B" else "你（Azure）"
		var act := "虚着 pass" if mv[1] == "pass" else "落在 %s" % mv[1]
		lines.append("第%d手 %s 执%s %s" % [i + 1, who, color_cn(mv[0]), act])
	return "\n".join(lines)

## 整盘手数的紧凑列表，长对局折叠中段以控制上下文长度
static func compact_move_list(moves: Array, head := 10, tail := 18) -> String:
	if moves.is_empty():
		return "（空盘）"
	var parts: Array[String] = []
	if moves.size() <= head + tail:
		for i in moves.size():
			parts.append(_fmt_move(moves, i))
		return " ".join(parts)
	for i in head:
		parts.append(_fmt_move(moves, i))
	var mid := moves.size() - head - tail
	var tail_parts: Array[String] = []
	for i in range(moves.size() - tail, moves.size()):
		tail_parts.append(_fmt_move(moves, i))
	return "%s …（中间%d手略）… %s" % [" ".join(parts), mid, " ".join(tail_parts)]

static func _fmt_move(moves: Array, i: int) -> String:
	var mv: Array = moves[i]
	var tag := "E" if mv[0] == "B" else "A"      # E=Estarth(黑) A=Azure(白)
	return "%d.%s%s" % [i + 1, tag, mv[1]]

## 对局进程观：按手数粗分阶段（布局 <12 手 / 中盘 12~49 手 / 收官 ≥50 手）。
## 治「才刚开局就说反击/缠斗」的病灶：让她的口吻与当前进程匹配。
## 经 role_brief 注入所有经 context_block 组装的上文（含聊天：谈棋时也要认得进程）。
static func stage_note(moves: Array) -> String:
	var n := moves.size()
	if n < 12:
		var at := "对局刚开局（还没落子）" if n == 0 else "第%d手，还在布局阶段" % n
		return ("【进程观】现在是%s：双方在铺大场、守角占边，局势远未定型，离胜负还早。" \
			+ "你只谈大场、方向、展开、守角这类布局话题；不要用「反击、缠斗、搏杀、死活、收官、胜负已分」这类中后盘的说法，" \
			+ "也别因为胜率数字一时的波动就渲染紧张或危机感。") % at
	if n < 50:
		return "【进程观】现在是第%d手，已进入中盘：双方的棋开始接触，模样与实地逐渐清晰，可以有攻防、轻重、强弱的判断。" % n
	return "【进程观】现在是第%d手，已进入收官阶段：大局基本定型，以官子与目数计算为主，别再把小地方当作战场。" % n

## 按调用阶段说明身份、刚发生了什么、此刻该做什么，避免她在旁观回合抢着落子
static func role_brief(moves: Array, phase: String) -> String:
	var n := moves.size()
	var parts: Array[String] = ["你（Azure）执白棋，Estarth执黑棋；黑先白后，你每一手都在回应Estarth。"]
	if n > 0:
		var mv: Array = moves[n - 1]
		var who := "Estarth（黑）" if mv[0] == "B" else "你（Azure，白）"
		var act := "虚着 pass" if mv[1] == "pass" else "落在 %s" % mv[1]
		parts.append("刚刚第%d手：%s %s。" % [n, who, act])
	else:
		parts.append("棋盘还是空的，双方都还没落子。")
	parts.append("接下来轮到你（Azure，白）落子。" if n % 2 == 1 else "接下来轮到Estarth（黑）。")
	parts.append(stage_note(moves))
	parts.append(phase_note(phase))
	return _t("".join(parts))

## 带标签的落子评定存档：E=Estarth(黑)这手的分析，A=Azure(白)自己这手的分析与思路
static func memory_brief(memory_log: Array, last_n := 6) -> String:
	if memory_log.is_empty():
		return "（还没有落子评定记录）"
	var start := maxi(0, memory_log.size() - last_n)
	var lines: Array[String] = []
	for i in range(start, memory_log.size()):
		var m: Dictionary = memory_log[i]
		var who := "[E·黑] Estarth的棋" if str(m.get("tag", "A")) == "E" else "[A·白] 我的棋"
		lines.append("%s · 第%d手后：%s" % [who, int(m.get("move_no", 0)), str(m.get("text", ""))])
	return "\n".join(lines)

## 聊天上下文窗口：以玩家的每句话为中心，连同它的上一句与下一句（无论 Azure 还是玩家）
## 一起纳入；相邻窗口重叠时合并成连续片段（去重），避免她只看到孤零零的半截对话。
## 返回若干 [起, 止] 闭区间下标；最多取最近 last_players 条玩家发言，控制上下文长度。
static func chat_windows(chat_history: Array, last_players := 6) -> Array:
	var n := chat_history.size()
	var player_idx: Array[int] = []
	for i in range(n - 1, -1, -1):
		if str((chat_history[i] as Dictionary).get("role", "")) == "user":
			player_idx.append(i)
			if player_idx.size() >= last_players:
				break
	player_idx.reverse()
	var spans: Array = []
	for i in player_idx:
		var a := maxi(0, i - 1)
		var b := mini(n - 1, i + 1)
		if not spans.is_empty() and a <= int(spans[spans.size() - 1][1]) + 1:
			spans[spans.size() - 1][1] = maxi(int(spans[spans.size() - 1][1]), b)
		else:
			spans.append([a, b])
	return spans

## 最近的聊天摘录（供思考前回忆「聊天里说过的话」），过长逐条截断（64 字：既读得全玩家的话，又不撑爆上下文）
static func chat_brief(chat_history: Array, last_players := 6, per_char := 64) -> String:
	if chat_history.is_empty():
		return "（还没聊过天）"
	var spans := chat_windows(chat_history, last_players)
	if spans.is_empty():
		return "（还没聊过天）"
	var lines: Array[String] = []
	var prev_end := -1
	for sp in spans:
		if prev_end >= 0 and int(sp[0]) > prev_end + 1:
			lines.append("……")                        # 两段窗口之间确有省略，标出来
		for i in range(int(sp[0]), int(sp[1]) + 1):
			var h: Dictionary = chat_history[i]
			var who := "Estarth" if str(h.get("role", "")) == "user" else "Azure（我）"
			var t := " ".join(str(h.get("content", "")).split("\n", false)).strip_edges()
			if t.length() > per_char:
				t = t.substr(0, per_char) + "…"
			lines.append("%s：%s" % [who, t])
		prev_end = int(sp[1])
	return "\n".join(lines)

## 由棋谱重放得到当前盘面（含提子），用于给 Azure 一张实时的「当前棋面」图
static func board_from_moves(moves: Array) -> GoBoard:
	var b := GoBoard.new()
	for mv in moves:
		var col: String = "black" if str((mv as Array)[0]) == "B" else "white"
		var gtp := str((mv as Array)[1])
		if gtp == "pass":
			b.pass_move()
		else:
			var xy := Coords.gtp_to_xy(gtp)
			if xy.x >= 0:
				b.play(xy.x, xy.y, col)
	return b

## 紧凑盘面图 + 轮次声明：让她「时刻知道哪点有子、轮到谁」
static func board_map(board: GoBoard, num_moves: int) -> String:
	var head := "   "
	for x in GoBoard.SIZE:
		head += Coords.letter(x) + " "
	var lines: Array[String] = [head]
	for y in GoBoard.SIZE:                          # y=0 → 第19行（顶）
		var row := "%2d " % (GoBoard.SIZE - y)
		for x in GoBoard.SIZE:
			var c := str(board.grid[x][y])
			row += "X " if c == "black" else ("O " if c == "white" else "· ")
		lines.append(row)
	var whose := "轮到 Estarth（黑）落子" if num_moves % 2 == 0 else "轮到你（Azure，白）落子"
	var done := "棋盘为空、尚未落子" if num_moves == 0 else "已下 %d 手" % num_moves
	# 明确列出双方现有棋子坐标：谈棋时只能引用这里有的点，避免脑补不存在的子
	var bl: Array[String] = []
	var wh: Array[String] = []
	for y in GoBoard.SIZE:
		for x in GoBoard.SIZE:
			var c2 := str(board.grid[x][y])
			if c2 == "black":
				bl.append(Coords.xy_to_gtp(x, y))
			elif c2 == "white":
				wh.append(Coords.xy_to_gtp(x, y))
	var occ := "现有棋子（谈棋时只能引用这里出现的点）：\nEstarth 黑子：%s\nAzure 白子：%s" % [
		(" 、 ".join(bl) if not bl.is_empty() else "（暂无）"),
		(" 、 ".join(wh) if not wh.is_empty() else "（暂无）")]
	return "X = Estarth(黑)   O = Azure(白)   · = 空点\n%s\n%s\n%s，%s。" % ["\n".join(lines), occ, done, whose]

## board：调用方已有的现成棋盘（AzureAgent 传 st.board），给了就直接用，避免每次调用都从棋谱重放整盘
static func context_block(moves: Array, memory_log: Array, chat_history: Array, phase: String, skip_last_chat := false, katago := "", board: GoBoard = null) -> String:
	var hist := chat_history
	if skip_last_chat and hist.size() > 0:
		hist = hist.slice(0, hist.size() - 1)      # 聊天阶段：当前这句已作为正式消息发出，不在摘录里重复
	var block := ("【当前棋面 · 请时刻记住】\n%s"
		+ "\n\n【KataGo 形势 · 引擎判断，仅供参考】\n%s"
		+ "\n\n【对局全貌 · 请始终记住】\n%s\n整盘手数：%s\n最近过程：\n%s"
		+ "\n【落子评定存档（E=Estarth黑 / A=Azure白）· 只作回忆参考，别照抄、别改写里面的句子】\n%s"
		+ "\n【最近的对话】\n%s"
		+ "\n（铁律 · 先核对再开口：\n"
		+ "1) 只要这句话与棋局有关，动笔前先看【当前棋面】：要提到某片区域/某个点「有子、没子、模样、实地、厚薄」时，必须能在上面的「现有棋子」里找到依据；那里没有的子一律不许提，拿不准就只说方向、不点具体子，绝不凭印象编。凡说「左/右/上/下」必须先按坐标核对：字母是列（A→T 从左到右）、数字是行（19→1 从上到下）；两子字母相同只能说「上下」、数字相同才算「左右」；核对不上就别说方位词，改说「这一带」。\n"
		+ "2) 与棋局有关的每一句都要扣住「最近一手」和当前盘面，接着此刻的形势、以及你上一句的感受往下说；用「我 / 你」的第一人称视角，别像解说员那样说「黑方 / 白棋」，也别复述、别自说自话。\n"
		+ "3) 轮到你说棋时，无论先说后说，都要先结合当前盘面再开口，让人听着是「接着这盘棋」在说，而不是孤立的一句话。\n"
		+ "4) 聊的是棋以外的话题时，上面 1~3 条不适用，自然放松地聊即可。）") % [
		board_map(board if board != null else board_from_moves(moves), moves.size()),
		(katago if katago != "" else "（暂无引擎形势数据）"),
		role_brief(moves, phase),
		compact_move_list(moves),
		game_narrative(moves),
		memory_brief(memory_log),
		chat_brief(hist),
	]
	return _t(block)

## 在原提示词前附上对局全貌与回忆（原提示词本身不改动）
static func with_context(moves: Array, memory_log: Array, chat_history: Array, prompt: String, phase: String, skip_last_chat := false, katago := "", board: GoBoard = null) -> String:
	return _t(context_block(moves, memory_log, chat_history, phase, skip_last_chat, katago, board) + "\n\n【现在】\n" + prompt)

static func board_summary(move_seq: Array, last_n := 6) -> String:
	if move_seq.is_empty():
		return "空棋盘"
	var start := maxi(0, move_seq.size() - last_n)
	var parts: Array[String] = []
	for i in range(start, move_seq.size()):
		parts.append(str(move_seq[i]))
	return " → ".join(parts)

## KataGo 的 rootInfo 是行棋方视角；偶数手轮到黑方，奇数手轮到白方。统一换算成黑方视角
## 返回 [winrate_black, score_black]
static func to_black_perspective(analysis: Dictionary, num_moves: int) -> Array:
	var ri: Dictionary = analysis.get("rootInfo", {})
	var wr := float(ri.get("winrate", 0.5))
	var sc := float(ri.get("scoreLead", 0.0))
	if num_moves % 2 == 1:
		return [1.0 - wr, -sc]
	return [wr, sc]

# ================= 定式导学 =================

## 定式成型讲解（玩家视角：名称 + 要点 2~3 句）
static func joseki_teach_prompt(name: String, key: String, corner: String) -> String:
	return ("刚才%s角这一片走出了定式——【%s】。要点：%s\n"
		+ "用你自己的口吻（我/你）给Estarth讲 2~3 句：先说这是「%s」，再把上面的要点讲给他听（更口语、更软一点），"
		+ "最后轻轻带一句这类形状什么时候能用。别像教科书背课文，别点具体坐标，别用「黑方/白棋」，也别重复你之前说过的话。") % [corner, name, key, name]

## 她自己走出定式时的一句短话（不展开成讲解）
static func joseki_short_prompt(name: String, key: String, gtp: String) -> String:
	return ("你（Azure）刚下的 %s 让这一片成型为【%s】定式。要点：%s\n"
		+ "用一句你自己的话把这手的意思带出来即可：点一下这是「%s」，口吻自然，别展开成讲解，别点具体坐标。") % [gtp, name, key, name]

## 偏离轻提：附在常规点评之后，点到即止、不含胜率
static func joseki_hint_clause(name: String, expect_gtp: String) -> String:
	return ("另外轻轻提一句：这一带本来可以走【%s】的常规形状，常见一手在 %s 附近——点到即止，别批评、别提胜率数字，一句话带过就好。") % [name, expect_gtp]

## LLM 不可用时的定式兜底台词（含名称与要点，可直接念）
static func joseki_fallback(name: String, key: String) -> String:
	return "唔~ 这一片走出来的是【%s】。%s" % [name, key]