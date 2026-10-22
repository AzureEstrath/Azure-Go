class_name GameReport
extends Control
## 棋谱战报画布（v3.11.5）：独立 2D 渲染，不截主界面、不受 3D 相机 / 侧栏影响。
##
## 用法：`Main` 建一个 SubViewport → 挂本节点 → `set_data()` → 等两帧 → 取图保存 PNG。
## 纯布局函数（`cell_center` / `pick_notes` / `pick_losses` / `move_strip`）可被门卫直接断言。
##
## **为什么文字不用 `draw_string`**：Godot 4.6 的 `draw_string` 直接画 `SystemFont` 会输出方框
## （主题 Label 正常）。所以画布只负责棋盘 / 网格 / 棋子 / 印章底，所有文字都用 Label 叠在上层。

const REPORT_SIZE := Vector2(1200, 1600)
const BOARD_RECT := Rect2(150, 300, 900, 900)
const CELL := 900.0 / 19.0
const NOTES_LIMIT := 4
const LOSSES_LIMIT := 2

var data: Dictionary = {}
var _text_root: Control = null

func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	size = REPORT_SIZE
	_text_root = Control.new()
	_text_root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_text_root.size = REPORT_SIZE
	add_child(_text_root)
	_rebuild_text()

func set_data(d: Dictionary) -> void:
	data = d
	if is_inside_tree():
		_rebuild_text()
	queue_redraw()

# ================= 纯函数（门卫） =================

## 棋盘交叉点 → 画布坐标（x=0 左、y=0 顶；与后端 ownership 的 y*19+x 同向）
static func cell_center(x: int, y: int) -> Vector2:
	return BOARD_RECT.position + Vector2((float(x) + 0.5) * CELL, (float(y) + 0.5) * CELL)

## 关键点评：只取 review（点评 / 感想），按手数截止，最多 4 条（后出现的优先）
static func pick_notes(notes, move_no: int, limit := NOTES_LIMIT) -> Array:
	var out: Array = []
	if typeof(notes) != TYPE_ARRAY:
		return out
	for n in notes:
		if typeof(n) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = n
		if str(d.get("kind", "")) != "review":
			continue
		if int(d.get("move_no", 0)) > move_no:
			continue
		if str(d.get("text", "")).strip_edges() == "":
			continue
		out.append(d)
	if out.size() > limit:
		out = out.slice(out.size() - limit)
	return out

## 亏损手：按亏损幅度降序，取前 2
static func pick_losses(losses, move_no: int, limit := LOSSES_LIMIT) -> Array:
	var out: Array = []
	if typeof(losses) != TYPE_ARRAY:
		return out
	for l in losses:
		if typeof(l) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = l
		if int(d.get("move_no", 0)) > move_no:
			continue
		out.append(d)
	out.sort_custom(func(a, b): return float(a.get("drop", 0.0)) > float(b.get("drop", 0.0)))
	if out.size() > limit:
		out = out.slice(0, limit)
	return out

## 一行点评 / 亏损：只拼接后端字段，不评判
static func note_line(n: Dictionary) -> String:
	var who := "你" if str(n.get("side", "")) == "E" else "Azure"
	return "第%d手 · %s：%s" % [int(n.get("move_no", 0)), who, str(n.get("text", "")).strip_edges()]

static func loss_line(l: Dictionary) -> String:
	return "第%d手 %s：%s" % [int(l.get("move_no", 0)), str(l.get("gtp", "")), str(l.get("reason", ""))]

## 最近手顺（最多 14 手）：`D16 · D4 · …`
static func move_strip(move_seq, move_no: int, limit := 14) -> String:
	if typeof(move_seq) != TYPE_ARRAY or move_no <= 0:
		return ""
	var seq: Array = move_seq.slice(0, mini(move_no, move_seq.size()))
	if seq.size() > limit:
		seq = seq.slice(seq.size() - limit)
	var parts: Array[String] = []
	for v in seq:
		parts.append(str(v))
	return " · ".join(parts)

static func info_line(wr: float, score: float, has_info: bool) -> String:
	if not has_info:
		return "形势：—"
	return "黑胜率 %.1f%% · 目差 %+.1f" % [wr * 100.0, score]

# ================= 文字（Label 叠加） =================

func _add_label(parent: Node, text: String, pos: Vector2, box: Vector2, fs: int,
		color: Color, align := HORIZONTAL_ALIGNMENT_LEFT, wrap := false) -> Label:
	var l := Label.new()
	l.text = text
	l.position = pos
	l.size = box
	l.mouse_filter = Control.MOUSE_FILTER_IGNORE
	l.add_theme_font_override("font", GufengTheme.font())
	l.add_theme_font_size_override("font_size", fs)
	l.add_theme_color_override("font_color", color)
	l.horizontal_alignment = align
	l.vertical_alignment = VERTICAL_ALIGNMENT_TOP
	if wrap:
		l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	else:
		l.clip_text = true
	parent.add_child(l)
	return l

func _rebuild_text() -> void:
	if _text_root == null:
		return
	for c in _text_root.get_children():
		c.free()
	var move_no := int(data.get("move_no", 0))
	var total := int(data.get("total_moves", move_no))
	var pos_line := "复盘至第 %d / %d 手" % [move_no, total] if bool(data.get("reviewing", false)) else "第 %d 手" % move_no
	_add_label(_text_root, "Azure · 围棋陪练", Vector2(64, 62), Vector2(760, 66), 44, GufengTheme.INK)
	var user := str(data.get("user", "")).strip_edges()
	_add_label(_text_root, "对局战报 · 执黑 %s · 执白 Azure" % (user if user != "" else "你"),
		Vector2(66, 138), Vector2(700, 32), 20, GufengTheme.INK_SOFT)
	# 右边界留到印章左侧（1040），别再让时间压到朱砂印上
	_add_label(_text_root, str(data.get("date", "")), Vector2(700, 118), Vector2(304, 28), 18,
		GufengTheme.INK_SOFT, HORIZONTAL_ALIGNMENT_RIGHT)
	_add_label(_text_root, "亚", Vector2(REPORT_SIZE.x - 160, 62), Vector2(78, 78), 42,
		GufengTheme.PAPER_WARM, HORIZONTAL_ALIGNMENT_CENTER)
	_add_label(_text_root, "%s · %s" % [pos_line,
		info_line(float(data.get("winrate", 0.5)), float(data.get("score", 0.0)),
			bool(data.get("has_info", false)))],
		Vector2(66, 194), Vector2(1070, 34), 21, GufengTheme.INK)
	var strip := move_strip(data.get("move_seq", []), move_no)
	if strip != "":
		_add_label(_text_root, "手顺：" + strip, Vector2(66, 236), Vector2(1070, 28), 17, GufengTheme.INK_SOFT)

	# 坐标：与棋盘同向（顶部 A…T，左侧 19…1）
	for i in 19:
		var letter := Coords.letter(i)
		var num := str(19 - i)
		var cx := cell_center(i, 0).x
		var cy := cell_center(0, i).y
		_add_label(_text_root, letter, Vector2(cx - 16, BOARD_RECT.position.y - 34),
			Vector2(32, 22), 14, GufengTheme.INK_SOFT, HORIZONTAL_ALIGNMENT_CENTER)
		_add_label(_text_root, letter, Vector2(cx - 16, BOARD_RECT.end.y + 12),
			Vector2(32, 22), 14, GufengTheme.INK_SOFT, HORIZONTAL_ALIGNMENT_CENTER)
		_add_label(_text_root, num, Vector2(BOARD_RECT.position.x - 58, cy - 11),
			Vector2(38, 22), 14, GufengTheme.INK_SOFT, HORIZONTAL_ALIGNMENT_RIGHT)
		_add_label(_text_root, num, Vector2(BOARD_RECT.end.x + 20, cy - 11),
			Vector2(38, 22), 14, GufengTheme.INK_SOFT, HORIZONTAL_ALIGNMENT_LEFT)

	# 点评 / 亏损手：VBox 自动排版，避免手算每行高度
	var vb := VBoxContainer.new()
	vb.position = Vector2(64, BOARD_RECT.end.y + 40)
	vb.size = Vector2(REPORT_SIZE.x - 128, 330)
	vb.add_theme_constant_override("separation", 8)
	vb.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_text_root.add_child(vb)
	_add_label(vb, "关键点评", Vector2.ZERO, Vector2(REPORT_SIZE.x - 128, 34), 24, GufengTheme.INK)
	var notes := pick_notes(data.get("notes", []), move_no)
	if notes.is_empty():
		_add_label(vb, "本局暂无逐手点评存档。", Vector2.ZERO, Vector2(REPORT_SIZE.x - 128, 30),
			18, GufengTheme.INK_SOFT)
	else:
		for n in notes:
			var l := _add_label(vb, note_line(n), Vector2.ZERO, Vector2(REPORT_SIZE.x - 128, 48),
				18, GufengTheme.INK, HORIZONTAL_ALIGNMENT_LEFT, true)
			l.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var losses := pick_losses(data.get("losses", []), move_no)
	if not losses.is_empty():
		_add_label(vb, "亏损手", Vector2.ZERO, Vector2(REPORT_SIZE.x - 128, 30), 20, GufengTheme.CINNABAR)
		for l in losses:
			_add_label(vb, loss_line(l), Vector2.ZERO, Vector2(REPORT_SIZE.x - 128, 26),
				17, GufengTheme.INK_SOFT)
	_add_label(_text_root, "落子无声，落笔有情 · v3.12.0", Vector2(0, REPORT_SIZE.y - 40),
		Vector2(REPORT_SIZE.x, 26), 17, Color(GufengTheme.INK_SOFT, 0.8), HORIZONTAL_ALIGNMENT_CENTER)

# ================= 画布（棋盘 / 印章底 / 分隔线） =================

func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, REPORT_SIZE), GufengTheme.PAPER)
	draw_texture_rect(GufengTheme.PAPER_TEX, Rect2(Vector2.ZERO, REPORT_SIZE), true, Color(1, 1, 1, 0.24))
	draw_rect(Rect2(26, 26, REPORT_SIZE.x - 52, REPORT_SIZE.y - 52),
		Color(GufengTheme.INK, 0.24), false, 2.0)
	var seal := Rect2(REPORT_SIZE.x - 160, 62, 78, 78)
	draw_rect(seal, GufengTheme.CINNABAR)
	draw_rect(seal, Color(0.55, 0.16, 0.12), false, 2.0)
	_draw_board()
	draw_line(Vector2(64, BOARD_RECT.end.y + 22), Vector2(REPORT_SIZE.x - 64, BOARD_RECT.end.y + 22),
		Color(GufengTheme.INK, 0.22), 1.0)

func _draw_board() -> void:
	draw_rect(BOARD_RECT.grow(CELL * 0.55), Color(0.722, 0.635, 0.463))
	draw_rect(BOARD_RECT.grow(CELL * 0.55), Color(0.243, 0.180, 0.094, 0.55), false, 2.0)
	# 领地图：画在网格 / 棋子下面，透明度比 3D 视图略低，打印/截图更耐看
	var own = data.get("ownership", [])
	if typeof(own) == TYPE_ARRAY and own.size() == 361 and bool(data.get("ownership_enabled", false)):
		for y in 19:
			for x in 19:
				var raw = own[y * 19 + x]
				if typeof(raw) != TYPE_FLOAT and typeof(raw) != TYPE_INT:
					continue
				var c := Board3D.ownership_color(float(raw))
				if c.a <= 0.0:
					continue
				var center := cell_center(x, y)
				draw_rect(Rect2(center - Vector2(CELL, CELL) * 0.5, Vector2(CELL, CELL)),
					Color(c.r, c.g, c.b, c.a * 0.75))
	# 网格
	var first := cell_center(0, 0)
	var last := cell_center(18, 18)
	var line := Color(0.243, 0.180, 0.094, 0.9)
	for i in 19:
		var cx := cell_center(i, 0).x
		var cy := cell_center(0, i).y
		draw_line(Vector2(cx, first.y), Vector2(cx, last.y), line, 1.4)
		draw_line(Vector2(first.x, cy), Vector2(last.x, cy), line, 1.4)
	for sx in [3, 9, 15]:
		for sy in [3, 9, 15]:
			draw_circle(cell_center(sx, sy), 3.2, Color(0.196, 0.157, 0.086))
	# 棋子
	var stones = data.get("stones", [])
	if typeof(stones) == TYPE_ARRAY:
		for s in stones:
			if typeof(s) != TYPE_DICTIONARY:
				continue
			var d: Dictionary = s
			var x := int(d.get("x", -1))
			var y := int(d.get("y", -1))
			if not Coords.valid(x, y):
				continue
			var center := cell_center(x, y)
			var r := CELL * 0.44
			var black := str(d.get("c", "")) == "black"
			draw_circle(center, r, Color(0.06, 0.06, 0.07) if black else Color(0.97, 0.96, 0.91))
			draw_arc(center, r, 0.0, TAU, 32, Color(0.0, 0.0, 0.0, 0.20), 1.2)
			if bool(d.get("last", false)):
				draw_arc(center, r * 0.42, 0.0, TAU, 24, GufengTheme.CINNABAR, 2.6)
