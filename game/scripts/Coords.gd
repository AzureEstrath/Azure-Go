class_name Coords
extends RefCounted
## 棋盘坐标换算：与 KataGo/GTP 一致，列 A..T 跳过 I，行 19..1

const SIZE := 19

static func letter(i: int) -> String:
	return String.chr(65 + i + (1 if i >= 8 else 0))

## GTP 坐标 -> 内部 (x, y)；pass 或非法返回 (-1, -1)
static func gtp_to_xy(pos: String) -> Vector2i:
	var p := pos.strip_edges()
	if p == "" or p.to_lower() == "pass":
		return Vector2i(-1, -1)
	var col := p.substr(0, 1).to_upper().unicode_at(0) - 65
	if col >= 8:
		col -= 1
	var row := p.substr(1).to_int()
	if col < 0 or col >= SIZE or row < 1 or row > SIZE:
		return Vector2i(-1, -1)
	return Vector2i(col, SIZE - row)

static func xy_to_gtp(x: int, y: int) -> String:
	return letter(x) + str(SIZE - y)

## 给阵法命名，用于日志与提示
static func valid(x: int, y: int) -> bool:
	return x >= 0 and x < SIZE and y >= 0 and y < SIZE