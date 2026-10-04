class_name GoBoard
extends RefCounted
## 完整围棋规则棋盘：提子、打劫、禁着点、自杀禁止
## （逐方法移植自网页版 AzureGo0.1.py 的 GoBoard）

const SIZE := 19
const EMPTY := ""
const KO_NONE := Vector2i(-1, -1)
const DIRS: Array[Vector2i] = [Vector2i(1, 0), Vector2i(-1, 0), Vector2i(0, 1), Vector2i(0, -1)]

var grid: Array = []            # grid[x][y] = "" / "black" / "white"
var ko_point := KO_NONE         # 打劫禁着点

func _init() -> void:
	for x in SIZE:
		var col: Array = []
		col.resize(SIZE)
		col.fill(EMPTY)
		grid.append(col)

static func opposite(color: String) -> String:
	return "white" if color == "black" else "black"

func at(x: int, y: int) -> String:
	if not Coords.valid(x, y):
		return EMPTY
	return grid[x][y]

func _neighbors(x: int, y: int) -> Array:
	var out: Array = []
	for d in DIRS:
		var nx: int = x + d.x
		var ny: int = y + d.y
		if nx >= 0 and nx < SIZE and ny >= 0 and ny < SIZE:
			out.append(Vector2i(nx, ny))
	return out

## 返回 {group: Array[Vector2i], libs: int}
func _group_and_liberties(x: int, y: int) -> Dictionary:
	var color: String = grid[x][y]
	var stack: Array = [Vector2i(x, y)]
	var seen := {Vector2i(x, y): true}
	var libs := 0
	var group: Array = []
	while not stack.is_empty():
		var p: Vector2i = stack.pop_back()
		group.append(p)
		for n in _neighbors(p.x, p.y):
			var v: String = grid[n.x][n.y]
			if v == EMPTY:
				libs += 1
			elif v == color and not seen.has(n):
				seen[n] = true
				stack.append(n)
	return {"group": group, "libs": libs}

## 落子并返回 {ok, captured, err}；会修改棋盘
func play(x: int, y: int, color: String) -> Dictionary:
	if not Coords.valid(x, y):
		return {"ok": false, "captured": [], "err": "超出棋盘"}
	if grid[x][y] != EMPTY:
		return {"ok": false, "captured": [], "err": "此处已有棋子"}
	if ko_point == Vector2i(x, y):
		return {"ok": false, "captured": [], "err": "打劫禁着点"}
	var opp := opposite(color)
	grid[x][y] = color
	var captured: Array = []
	for n in _neighbors(x, y):
		if grid[n.x][n.y] == opp:
			var g := _group_and_liberties(n.x, n.y)
			if int(g["libs"]) == 0:
				captured.append_array(g["group"])
	for p: Vector2i in captured:
		grid[p.x][p.y] = EMPTY
	var own := _group_and_liberties(x, y)
	if int(own["libs"]) == 0 and captured.is_empty():
		grid[x][y] = EMPTY
		return {"ok": false, "captured": [], "err": "自杀禁着点"}
	if captured.size() == 1 and (own["group"] as Array).size() == 1 and int(own["libs"]) == 1:
		ko_point = captured[0]
	else:
		ko_point = KO_NONE
	return {"ok": true, "captured": captured, "err": ""}

## 只检查不落子
func is_legal(x: int, y: int, color: String) -> Dictionary:
	var tmp := clone()
	return tmp.play(x, y, color)

func pass_move() -> void:
	ko_point = KO_NONE

func clone() -> GoBoard:
	var b := GoBoard.new()
	for x in SIZE:
		b.grid[x] = (grid[x] as Array).duplicate()
	b.ko_point = ko_point
	return b

## 棋子列表；last_point 上的那颗带 last=true
func stones_list(last_point := KO_NONE) -> Array:
	var out: Array = []
	for y in SIZE:
		for x in SIZE:
			var c: String = grid[x][y]
			if c != EMPTY:
				var d := {"x": x, "y": y, "c": c}
				if Vector2i(x, y) == last_point:
					d["last"] = true
				out.append(d)
	return out

## 统计双方棋子数（用于总结/显示）
func count() -> Dictionary:
	var b := 0
	var w := 0
	for x in SIZE:
		for y in SIZE:
			if grid[x][y] == "black":
				b += 1
			elif grid[x][y] == "white":
				w += 1
	return {"black": b, "white": w}