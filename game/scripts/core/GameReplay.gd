class_name GameReplay
extends RefCounted
## 棋谱本地重放（v3.11.5）：把 [[color, gtp], ...] 还原成第 n 手的盘面，供棋谱库预览。
##
## 只做显示需要的规则：落子 / 提子 / 虚着 / 占用点跳过；打劫不判（入库前已由后端逐手校验）。
## 纯函数、不碰节点，门卫可直接断言。输出形状与 `Board3D.set_stones` / 战报一致：
## `[{"x": int, "y": int, "c": "black"|"white", "last": true?}, ...]`（y=0 是顶行）。

const SIZE := 19
const DIRS := [Vector2i(1, 0), Vector2i(-1, 0), Vector2i(0, 1), Vector2i(0, -1)]

static func stones_at(moves, n: int) -> Array:
	var grid: Array = []
	for x in SIZE:
		var col: Array = []
		col.resize(SIZE)
		col.fill("")
		grid.append(col)
	var seq: Array = moves if typeof(moves) == TYPE_ARRAY else []
	var upto := mini(maxi(n, 0), seq.size())
	var last := Vector2i(-1, -1)
	for i in upto:
		var m = seq[i]
		var color := _color_of(m)
		var gtp := _gtp_of(m)
		if color == "":
			continue
		if gtp == "" or gtp.to_lower() == "pass":
			continue
		var xy := Coords.gtp_to_xy(gtp)
		if not Coords.valid(xy.x, xy.y):
			continue
		if grid[xy.x][xy.y] != "":
			continue
		_place(grid, xy.x, xy.y, color)
		last = xy
	return _stones(grid, last)

static func _place(grid: Array, x: int, y: int, color: String) -> void:
	grid[x][y] = color
	var opp := "white" if color == "black" else "black"
	for d in DIRS:
		var nx: int = x + d.x
		var ny: int = y + d.y
		if not Coords.valid(nx, ny) or grid[nx][ny] != opp:
			continue
		var group := _group(grid, nx, ny, opp)
		if _liberties(grid, group) == 0:
			for p in group:
				grid[p.x][p.y] = ""

static func _group(grid: Array, x: int, y: int, color: String) -> Array[Vector2i]:
	var out: Array[Vector2i] = []
	var seen := {Vector2i(x, y): true}
	var stack: Array[Vector2i] = [Vector2i(x, y)]
	while not stack.is_empty():
		var p: Vector2i = stack.pop_back()
		out.append(p)
		for d in DIRS:
			var nx: int = p.x + d.x
			var ny: int = p.y + d.y
			var q := Vector2i(nx, ny)
			if Coords.valid(nx, ny) and not seen.has(q) and grid[nx][ny] == color:
				seen[q] = true
				stack.append(q)
	return out

static func _liberties(grid: Array, group: Array[Vector2i]) -> int:
	var libs := 0
	for p in group:
		for d in DIRS:
			var nx: int = p.x + d.x
			var ny: int = p.y + d.y
			if Coords.valid(nx, ny) and grid[nx][ny] == "":
				libs += 1
	return libs

static func _stones(grid: Array, last: Vector2i) -> Array:
	var out: Array = []
	for y in SIZE:
		for x in SIZE:
			var c: String = grid[x][y]
			if c == "":
				continue
			var d := {"x": x, "y": y, "c": c}
			if x == last.x and y == last.y:
				d["last"] = true
			out.append(d)
	return out

static func _color_of(m) -> String:
	var c := ""
	if typeof(m) == TYPE_ARRAY and (m as Array).size() >= 2:
		c = str(m[0]).strip_edges().to_lower()
	elif typeof(m) == TYPE_DICTIONARY:
		var d: Dictionary = m
		c = str(d.get("color", d.get("c", ""))).strip_edges().to_lower()
	if c in ["b", "black", "黑"]:
		return "black"
	if c in ["w", "white", "白"]:
		return "white"
	return ""

static func _gtp_of(m) -> String:
	if typeof(m) == TYPE_ARRAY and (m as Array).size() >= 2:
		return str(m[1]).strip_edges()
	if typeof(m) == TYPE_DICTIONARY:
		return str((m as Dictionary).get("gtp", "")).strip_edges()
	return ""
