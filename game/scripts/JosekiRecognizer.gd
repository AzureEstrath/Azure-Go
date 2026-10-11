class_name JosekiRecognizer
extends RefCounted
## 定式识别器：纯本地计算、零引擎调用（每手几十次比较，不在热路径上）
## 做法：取「最新一手所在角」的全部着子序列做角部归一化，
##      再对定式库做 4 组变换（2 朝向 × 2 换色）下的最长前缀游走。
## 识别结果：
##   formed   —— 序列恰好走完某条定式（刚成型）：points = 该定式实际棋盘点（供棋盘高亮轨迹）
##   deviated —— 玩家在角内落子偏离了已吻合 ≥MIN_PREFIX 手的定式：expect = 库中下一手的棋盘点

const REGION := 8          # 距角 8 路以内视作该角
const MIN_PREFIX := 4      # 偏离提醒的最低吻合手数（不足不打扰）
const CORNERS := ["左上", "右上", "左下", "右下"]

## moves: GameState.moves 原样（[["B","Q16"],["W","pass"], ...]）
static func scan(moves: Array) -> Dictionary:
	# 1) 最新一手落在哪个角（归一化后取离角最近的那个）
	var last := Vector2i(-1, -1)
	for i in range(moves.size() - 1, -1, -1):
		var gt := str((moves[i] as Array)[1])
		if gt == "pass":
			continue
		last = Coords.gtp_to_xy(gt)
		break
	if last.x < 0:
		return {}
	var c := -1
	var best_d := 999
	for ci in 4:
		var n0 := _norm(ci, last.x, last.y)
		var d := maxi(n0.x, n0.y)
		if d < best_d:
			best_d = d
			c = ci
	if c < 0 or best_d >= REGION:
		return {}
	# 2) 该角内的全部着子（按手顺，跳过 pass 与角外）
	var seq: Array = []
	for mv in moves:
		var gt := str((mv as Array)[1])
		if gt == "pass":
			continue
		var xy := Coords.gtp_to_xy(gt)
		if xy.x < 0:
			continue
		var n := _norm(c, xy.x, xy.y)
		if maxi(n.x, n.y) < REGION:
			seq.append({"col": str((mv as Array)[0]), "nx": n.x, "ny": n.y, "gx": xy.x, "gy": xy.y})
	if seq.size() < 2:
		return {}
	# 3) 定式库最长前缀游走（2 朝向：原样/对角镜像；2 换色：先落子方同色/异色）
	var first_col := str(seq[0]["col"])
	var best: Dictionary = {}
	for e in JosekiData.LIST:
		var pat: Array = (e as Dictionary)["seq"]
		if pat.size() < 2:
			continue
		for tr in [false, true]:
			for same in [true, false]:
				var k := _match_prefix(seq, pat, first_col, tr, same)
				if k <= 0:
					continue
				var complete := k == pat.size() and k == seq.size()
				var score := [1 if complete else 0, k, pat.size()]
				if best.is_empty() or _better(score, best["score"]):
					best = {"e": e, "k": k, "tr": tr, "same": same, "score": score, "complete": complete}
	if best.is_empty():
		return {}
	var k: int = best["k"]
	var e: Dictionary = best["e"]
	var pat: Array = e["seq"]
	# side：以「角内最后一手」是谁下的为准（成型时即 k-1，偏离时即刚落的第 k+1 手）
	var side := "player" if str(seq[seq.size() - 1]["col"]) == "B" else "azure"
	var base := {
		"name": str(e["name"]), "cat": str(e["cat"]),
		"corner": CORNERS[c], "side": side, "k": k,
	}
	if bool(best["complete"]):
		var pts: Array = []
		for i in k:
			pts.append(Vector2i(int(seq[i]["gx"]), int(seq[i]["gy"])))
		base["kind"] = "formed"
		base["key"] = str(e["key"])
		base["points"] = pts
		return base
	# 4) 偏离：玩家刚在角内落子、且与某个已吻合 4+ 手的定式不同（只对玩家开口，避免打扰）
	if k >= MIN_PREFIX and pat.size() > k and seq.size() == k + 1 and side == "player":
		var item: Array = pat[k]
		var want_col := first_col if (int(item[0]) == 0) == bool(best["same"]) else _opp(first_col)
		if str(seq[k]["col"]) == want_col:
			var ex := int(item[2]) if bool(best["tr"]) else int(item[1])
			var ey := int(item[1]) if bool(best["tr"]) else int(item[2])
			base["kind"] = "deviated"
			base["key"] = str(e["key"])
			base["expect"] = _denorm(c, ex, ey)
			return base
	return {}

## 角部归一化：0=左上 1=右上 2=左下 3=右下（y=0 为顶边，向下递增）
static func _norm(c: int, x: int, y: int) -> Vector2i:
	match c:
		0: return Vector2i(x, y)
		1: return Vector2i(GoBoard.SIZE - 1 - x, y)
		2: return Vector2i(x, GoBoard.SIZE - 1 - y)
		3: return Vector2i(GoBoard.SIZE - 1 - x, GoBoard.SIZE - 1 - y)
	return Vector2i(-1, -1)

static func _denorm(c: int, nx: int, ny: int) -> Vector2i:
	match c:
		0: return Vector2i(nx, ny)
		1: return Vector2i(GoBoard.SIZE - 1 - nx, ny)
		2: return Vector2i(nx, GoBoard.SIZE - 1 - ny)
		3: return Vector2i(GoBoard.SIZE - 1 - nx, GoBoard.SIZE - 1 - ny)
	return Vector2i(-1, -1)

static func _opp(c: String) -> String:
	return "W" if c == "B" else "B"

## 前缀吻合长度：颜色与坐标均一致才算吻合
static func _match_prefix(seq: Array, pat: Array, first_col: String, tr: bool, same: bool) -> int:
	var k := 0
	var n := mini(seq.size(), pat.size())
	for i in n:
		var item: Array = pat[i]
		var want := first_col if (int(item[0]) == 0) == same else _opp(first_col)
		var s: Dictionary = seq[i]
		if str(s["col"]) != want:
			return k
		var px := int(item[2]) if tr else int(item[1])
		var py := int(item[1]) if tr else int(item[2])
		if int(s["nx"]) != px or int(s["ny"]) != py:
			return k
		k += 1
	return k

## 评分比较：完整匹配优先 → 吻合手数多优先 → 线条更长优先
static func _better(a: Array, b: Array) -> bool:
	if int(a[0]) != int(b[0]):
		return int(a[0]) > int(b[0])
	if int(a[1]) != int(b[1]):
		return int(a[1]) > int(b[1])
	return int(a[2]) > int(b[2])