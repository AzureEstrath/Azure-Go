class_name MiniBoard
extends Control
## 棋谱库右侧的 2D 小棋盘（v3.11.5）：只画木盘 / 网格 / 星位 / 棋子 / 最后一手朱砂圈。
## 坐标方向与主棋盘、战报一致：x=0 左、y=0 顶。文字不在这里画（避免 draw_string 的中文坑）。

var stones: Array = []

func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	clip_contents = true

func set_stones(values: Array) -> void:
	stones = values
	queue_redraw()

func _draw() -> void:
	var side := minf(size.x, size.y)
	if side <= 0.0:
		return
	var pad := side * 0.035
	var cell := (side - pad * 2.0) / 19.0
	var origin := Vector2((size.x - side) * 0.5 + pad, (size.y - side) * 0.5 + pad)
	draw_rect(Rect2(Vector2.ZERO, size), Color(0.722, 0.635, 0.463, 0.30))
	var board := Rect2(origin - Vector2(cell, cell) * 0.5, Vector2(cell, cell) * 20.0)
	draw_rect(board, Color(0.722, 0.635, 0.463))
	draw_rect(board, Color(0.243, 0.180, 0.094, 0.55), false, 1.0)
	var line := Color(0.243, 0.180, 0.094, 0.9)
	for i in 19:
		var c := origin + Vector2(i + 0.5, i + 0.5) * cell
		draw_line(Vector2(origin.x + cell * 0.5, c.y), Vector2(origin.x + cell * 18.5, c.y), line, 1.0)
		draw_line(Vector2(c.x, origin.y + cell * 0.5), Vector2(c.x, origin.y + cell * 18.5), line, 1.0)
	for sx in [3, 9, 15]:
		for sy in [3, 9, 15]:
			draw_circle(origin + Vector2(sx + 0.5, sy + 0.5) * cell, maxf(1.5, cell * 0.16),
				Color(0.196, 0.157, 0.086))
	for s in stones:
		if typeof(s) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = s
		var x := int(d.get("x", -1))
		var y := int(d.get("y", -1))
		if not Coords.valid(x, y):
			continue
		var center := origin + Vector2(x + 0.5, y + 0.5) * cell
		var r := cell * 0.44
		var black := str(d.get("c", "")) == "black"
		draw_circle(center, r, Color(0.06, 0.06, 0.07) if black else Color(0.97, 0.96, 0.91))
		draw_arc(center, r, 0.0, TAU, 24, Color(0.0, 0.0, 0.0, 0.20), 1.0)
		if bool(d.get("last", false)):
			draw_arc(center, r * 0.42, 0.0, TAU, 18, GufengTheme.CINNABAR, 2.0)
