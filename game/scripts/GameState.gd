class_name GameState
extends RefCounted
## 一局棋的全部状态（含文档中提到的「对局记忆」）

var moves: Array = []            # 传给 KataGo 的配对序列 [["B","Q16"],["W","D4"],...]
var move_seq: Array = []         # 展示/总结用的纯坐标序列 ["Q16","D4",...]
var board: GoBoard = GoBoard.new()
var snapshots: Array = []        # 每个玩家手之前的完整状态，供俗手回档
var bad_moves: Array = []        # 最近三次俗手
var memory_log: Array = []       # 带标签的对局记忆：{tag, move_no, text}；E=Estarth(黑)的棋，A=Azure(白)的棋
var chat_history: Array = []
var allow_pass := false          # 是否允许 Azure 虚着
var pre_analysis = null          # {n, a} 落子前局面分析缓存
var after_analysis = null        # {n, a} 玩家落子后局面分析缓存
var last_katago := ""            # 最近一次 KataGo 形势的文字摘要（胜率/目差/实地估算），供 Azure 上下文使用
var finished := false

func add_move(color: String, gtp: String) -> void:
	moves.append([color, gtp])
	move_seq.append(gtp)

func make_snapshot() -> Dictionary:
	return {
		"board": board.clone(),
		"moves": moves.duplicate(true),
		"move_seq": move_seq.duplicate(),
		"memory_log": memory_log.duplicate(true),
	}

## 最近一手实际落子的交叉点（跳过 pass）
func last_played_xy() -> Vector2i:
	for i in range(move_seq.size() - 1, -1, -1):
		if move_seq[i] != "pass":
			return Coords.gtp_to_xy(str(move_seq[i]))
	return Vector2i(-1, -1)

func payload() -> Dictionary:
	return {
		"stones": board.stones_list(last_played_xy()),
		"move_count": moves.size(),
		"move_seq": move_seq.duplicate(),
		"bad_moves": bad_moves.duplicate(),
		"allow_pass": allow_pass,
	}

## 记录一条带标签的对局记忆（思考前回忆用）：
## tag = "E"（对 Estarth 这手的分析/评价）或 "A"（Azure 自己的落子分析与思路）
func log_memory(tag: String, text: String, move_no: int) -> void:
	var t := " ".join(text.split("\n", false)).strip_edges()
	if t == "":
		return
	memory_log.append({"tag": tag, "move_no": move_no, "text": t.substr(0, 80)})
	while memory_log.size() > 18:
		memory_log.pop_front()

func record_bad_move(turn_index: int, gtp: String, reason: String) -> void:
	bad_moves.append({
		"turn_index": turn_index,
		"move_no": move_seq.size(),
		"gtp": gtp,
		"reason": reason,
	})
	while bad_moves.size() > 3:
		bad_moves.pop_front()

func reset() -> void:
	moves = []
	move_seq = []
	board = GoBoard.new()
	snapshots = []
	bad_moves = []
	memory_log = []
	chat_history = []
	pre_analysis = null
	after_analysis = null
	last_katago = ""
	finished = false