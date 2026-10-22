class_name GameLibrary
extends Control
## 棋谱库（v3.11.5）：全屏模态纸签——左侧列表，右侧盘面 / 形势 / 总结 / 翻手。
##
## 这个类**只负责界面与本地回放**（`GameReplay` / `MiniBoard`），网络与文件对话框都在 `Main`：
## 列表 / 导入 / 导出 / 删除由 Main 发请求，再把数据喂回来。信号都是「请求」语义。
##
## 纯本地、零引擎调用；打开时棋盘仍被主界面纸幕挡着，不会误落子。

signal closed
signal refresh_requested
signal import_requested
signal game_selected(game_id: int)
signal export_requested(game_id: int)
signal delete_requested(game_id: int)
signal game_review_requested(record: Dictionary)

const PANEL_MIN := Vector2(1080, 700)
const LIST_W := 420.0
const BOARD_SIDE := 316.0

var _games: Array = []
var _record: Dictionary = {}
var _selected_id := 0
var _step := 0
var _move_count := 0
var _rows := {}                         # id -> Button（刷新选中态用）
var _status: Label = null
var _list_box: VBoxContainer = null
var _detail_title: Label = null
var _detail_meta: Label = null
var _summary: Label = null
var _step_label: Label = null
var _board: MiniBoard = null
var _btn_prev: Button = null
var _btn_next: Button = null
var _btn_export: Button = null
var _btn_delete: Button = null
var _btn_full: Button = null
var _confirm: ColorRect = null

func _ready() -> void:
	name = "ModalVeil"                          # Main 的 `_modal_open()` 据此不抢 Esc
	focus_mode = Control.FOCUS_ALL
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_STOP
	_build()
	open()

func _build() -> void:
	var veil := ColorRect.new()
	veil.color = Color(0.09, 0.08, 0.06, 0.52)
	veil.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	veil.mouse_filter = Control.MOUSE_FILTER_STOP
	add_child(veil)

	var center := CenterContainer.new()
	center.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	center.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(center)
	var panel := PanelContainer.new()
	panel.add_theme_stylebox_override("panel", GufengTheme.veil_card(0.97, 22))
	panel.custom_minimum_size = PANEL_MIN
	center.add_child(panel)

	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 10)
	panel.add_child(root)

	# —— 标题行 ——
	var head := HBoxContainer.new()
	head.add_theme_constant_override("separation", 10)
	root.add_child(head)
	var title := _label("棋谱库 · 对局存档", 26, GufengTheme.INK)
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(title)
	_status = _label("", 12, GufengTheme.INK_SOFT)
	_status.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_status.clip_text = true
	head.add_child(_status)
	var close_btn := Button.new()
	close_btn.text = "关闭"
	GufengTheme.apply_button(close_btn)
	close_btn.pressed.connect(close)
	head.add_child(close_btn)

	# —— 左列表 / 右详情 ——
	var body := HBoxContainer.new()
	body.add_theme_constant_override("separation", 16)
	body.size_flags_vertical = Control.SIZE_EXPAND_FILL
	root.add_child(body)

	var left := VBoxContainer.new()
	left.add_theme_constant_override("separation", 8)
	left.custom_minimum_size = Vector2(LIST_W, 0)
	left.size_flags_vertical = Control.SIZE_EXPAND_FILL
	body.add_child(left)
	var acts := HBoxContainer.new()
	acts.add_theme_constant_override("separation", 8)
	left.add_child(acts)
	var import_btn := Button.new()
	import_btn.text = "导入 SGF"
	GufengTheme.apply_seal_button(import_btn, false)
	import_btn.custom_minimum_size = Vector2(0, 34)
	import_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	import_btn.tooltip_text = "从本机选择一份 19 路 SGF 棋谱；入库前会逐手校验"
	import_btn.pressed.connect(func(): import_requested.emit())
	acts.add_child(import_btn)
	var refresh_btn := Button.new()
	refresh_btn.text = "刷新"
	GufengTheme.apply_button(refresh_btn)
	refresh_btn.custom_minimum_size = Vector2(0, 34)
	refresh_btn.pressed.connect(func(): refresh_requested.emit())
	acts.add_child(refresh_btn)

	var scroll := ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	left.add_child(scroll)
	_list_box = VBoxContainer.new()
	_list_box.add_theme_constant_override("separation", 6)
	_list_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(_list_box)

	var right := VBoxContainer.new()
	right.add_theme_constant_override("separation", 6)
	right.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	right.size_flags_vertical = Control.SIZE_EXPAND_FILL
	body.add_child(right)

	_detail_title = _label("选择一盘棋谱", 22, GufengTheme.INK)
	_detail_title.clip_text = true
	right.add_child(_detail_title)
	_detail_meta = _label("从左边选一盘，右侧可以逐手翻看盘面。", 13, GufengTheme.INK_SOFT, true)
	_detail_meta.custom_minimum_size = Vector2(0, 40)
	right.add_child(_detail_meta)
	_board = MiniBoard.new()
	_board.custom_minimum_size = Vector2(BOARD_SIDE, BOARD_SIDE)
	_board.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
	right.add_child(_board)

	var step_row := HBoxContainer.new()
	step_row.alignment = BoxContainer.ALIGNMENT_CENTER
	step_row.add_theme_constant_override("separation", 10)
	right.add_child(step_row)
	_btn_prev = Button.new()
	_btn_prev.text = "上一手"
	GufengTheme.apply_button(_btn_prev)
	_btn_prev.custom_minimum_size = Vector2(96, 30)
	_btn_prev.pressed.connect(func(): _step_by(-1))
	step_row.add_child(_btn_prev)
	_step_label = _label("第 0 / 0 手", 13, GufengTheme.INK_SOFT)
	_step_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_step_label.custom_minimum_size = Vector2(150, 0)
	step_row.add_child(_step_label)
	_btn_next = Button.new()
	_btn_next.text = "下一手"
	GufengTheme.apply_button(_btn_next)
	_btn_next.custom_minimum_size = Vector2(96, 30)
	_btn_next.pressed.connect(func(): _step_by(1))
	step_row.add_child(_btn_next)

	var sum_title := _label("局后总结", 15, GufengTheme.INK)
	right.add_child(sum_title)
	var sum_scroll := ScrollContainer.new()
	sum_scroll.custom_minimum_size = Vector2(0, 74)
	sum_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	right.add_child(sum_scroll)
	_summary = _label("", 13, GufengTheme.INK_SOFT, true)
	_summary.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	sum_scroll.add_child(_summary)

	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 8)
	right.add_child(actions)
	_btn_full = Button.new()
	_btn_full.text = "全盘复盘"
	GufengTheme.apply_seal_button(_btn_full, false)
	_btn_full.custom_minimum_size = Vector2(0, 34)
	_btn_full.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_btn_full.tooltip_text = "让 Azure 用强思考从头到尾分段点评这一局；棋盘只读回放，可以随时暂停"
	_btn_full.pressed.connect(func(): _start_full_review())
	actions.add_child(_btn_full)
	_btn_export = Button.new()
	_btn_export.text = "导出 SGF"
	GufengTheme.apply_button(_btn_export)
	_btn_export.custom_minimum_size = Vector2(0, 34)
	_btn_export.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_btn_export.pressed.connect(func():
		if _selected_id > 0:
			export_requested.emit(_selected_id))
	actions.add_child(_btn_export)
	_btn_delete = Button.new()
	_btn_delete.text = "删除"
	GufengTheme.apply_button(_btn_delete)
	_btn_delete.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	_btn_delete.custom_minimum_size = Vector2(96, 34)
	_btn_delete.pressed.connect(func(): _ask_delete())
	actions.add_child(_btn_delete)

	var hint := _label("SGF 为标准 19 路棋谱；删除不可恢复。← / → 可逐手翻看。", 11,
		Color(GufengTheme.INK_SOFT, 0.8))
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	root.add_child(hint)
	_clear_detail()
	_btn_export.disabled = true
	_btn_delete.disabled = true
	if _btn_full != null:
		_btn_full.disabled = true

func _label(text: String, fs: int, color: Color, wrap := false) -> Label:
	var l := Label.new()
	l.text = text
	l.add_theme_font_size_override("font_size", fs)
	l.add_theme_color_override("font_color", color)
	l.mouse_filter = Control.MOUSE_FILTER_IGNORE
	if wrap:
		l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	return l

# ================= 对外（Main 喂数据） =================

func open() -> void:
	visible = true
	grab_focus()

func close() -> void:
	visible = false
	closed.emit()
	queue_free()

## 把当前选中的棋谱整盘交给 Main 开「全盘 AI 复盘」；先隐藏自己，别挡入场运镜。
func _start_full_review() -> void:
	if _selected_id <= 0 or _record.is_empty():
		return
	var rec := _record.duplicate(true)
	close()
	game_review_requested.emit(rec)

func set_status(text: String, bad := false) -> void:
	if _status == null:
		return
	_status.text = text
	_status.tooltip_text = text
	_status.add_theme_color_override("font_color",
		GufengTheme.CINNABAR if bad else GufengTheme.INK_SOFT)

func selected_id() -> int:
	return _selected_id

func set_games(games: Array) -> void:
	_games = games.duplicate(true)
	if _list_box == null:
		return
	for c in _list_box.get_children():
		c.queue_free()
	_rows.clear()
	var group := ButtonGroup.new()
	group.allow_unpress = false
	for g in _games:
		if typeof(g) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = g
		var game_id := int(d.get("id", 0))
		if game_id <= 0:
			continue
		var b := Button.new()
		b.text = _row_text(d)
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.toggle_mode = true
		b.button_group = group
		b.custom_minimum_size = Vector2(0, 36)
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		GufengTheme.apply_button(b)
		b.add_theme_font_size_override("font_size", 13)
		var tip := str(d.get("summary", "")).strip_edges()
		b.tooltip_text = tip if tip != "" else "第 %d 手" % int(d.get("move_count", 0))
		b.pressed.connect(_on_row_pressed.bind(game_id))
		_list_box.add_child(b)
		_rows[game_id] = b
		if game_id == _selected_id:
			b.button_pressed = true
	if _games.is_empty():
		var empty := _label("还没有棋谱。点「导入 SGF」，或下完一局点「结束」自动存档。",
			13, GufengTheme.INK_SOFT, true)
		empty.custom_minimum_size = Vector2(LIST_W - 24, 0)
		_list_box.add_child(empty)
	if _selected_id != 0 and not _rows.has(_selected_id):
		_selected_id = 0
		_clear_detail()
		_btn_export.disabled = true
		_btn_delete.disabled = true
		if _btn_full != null:
			_btn_full.disabled = true

func set_selected(rec: Dictionary) -> void:
	_record = rec
	_selected_id = int(rec.get("id", 0))
	var moves = rec.get("moves", [])
	_move_count = int(rec.get("move_count", moves.size() if typeof(moves) == TYPE_ARRAY else 0))
	_step = _move_count
	if _rows.has(_selected_id):
		(_rows[_selected_id] as Button).button_pressed = true
	_update_detail()

func _on_row_pressed(game_id: int) -> void:
	game_selected.emit(game_id)

func _row_text(d: Dictionary) -> String:
	var date := str(d.get("date", "")).strip_edges()
	if date == "":
		date = "未记日期"
	else:
		date = date.substr(0, 10)
	var mc := int(d.get("move_count", 0))
	var wr = d.get("final_black_wr", null)
	var tail := "未记形势"
	if typeof(wr) == TYPE_FLOAT or typeof(wr) == TYPE_INT:
		tail = "黑胜率 %.0f%%" % (float(wr) * 100.0)
	elif bool(d.get("demo", false)):
		tail = "示例棋谱"
	return "%s · %d 手 · %s" % [date, mc, tail]

# ================= 详情 / 翻手 =================

func _update_detail() -> void:
	var meta: Dictionary = _record.get("meta", {}) if typeof(_record.get("meta", {})) == TYPE_DICTIONARY else {}
	var title := str(meta.get("title", "")).strip_edges()
	if title == "":
		title = "对局存档 · 第 %d 手" % _move_count
	_detail_title.text = title
	var bits: Array[String] = []
	var date := str(_record.get("date", "")).strip_edges()
	if date != "":
		bits.append(date)
	bits.append("%d 手" % _move_count)
	var wr = _record.get("final_black_wr", null)
	if typeof(wr) == TYPE_FLOAT or typeof(wr) == TYPE_INT:
		bits.append("黑胜率 %.1f%%" % (float(wr) * 100.0))
	var sc = _record.get("final_score", null)
	if typeof(sc) == TYPE_FLOAT or typeof(sc) == TYPE_INT:
		bits.append("目差 %+.1f" % float(sc))
	var result := str(_record.get("result", "")).strip_edges()
	if result != "":
		bits.append("结果 " + result)
	_detail_meta.text = " · ".join(bits)
	var summary := str(_record.get("summary", "")).strip_edges()
	_summary.text = summary if summary != "" else "（这盘没有留下总结）"
	_btn_export.disabled = _selected_id == 0
	_btn_delete.disabled = _selected_id == 0
	if _btn_full != null:
		_btn_full.disabled = _selected_id == 0
	_update_step()

func _update_step() -> void:
	_step = clampi(_step, 0, _move_count)
	if _step_label != null:
		_step_label.text = "第 %d / %d 手" % [_step, _move_count]
	if _board != null:
		_board.set_stones(GameReplay.stones_at(_record.get("moves", []), _step))
	if _btn_prev != null:
		_btn_prev.disabled = _selected_id == 0 or _step <= 0
	if _btn_next != null:
		_btn_next.disabled = _selected_id == 0 or _step >= _move_count

func _step_by(delta: int) -> void:
	if _selected_id == 0:
		return
	_step = clampi(_step + delta, 0, _move_count)
	_update_step()

func _clear_detail() -> void:
	_record = {}
	_move_count = 0
	_step = 0
	if _detail_title != null:
		_detail_title.text = "选择一盘棋谱"
	if _detail_meta != null:
		_detail_meta.text = "从左边选一盘，右侧可以逐手翻看盘面。"
	if _summary != null:
		_summary.text = ""
	if _board != null:
		_board.set_stones([])
	if _step_label != null:
		_step_label.text = "第 0 / 0 手"
	if _btn_prev != null:
		_btn_prev.disabled = true
	if _btn_next != null:
		_btn_next.disabled = true

# ================= 删除确认 / 键盘 =================

func _ask_delete() -> void:
	if _selected_id <= 0 or _confirm != null:
		return
	var back := ColorRect.new()
	back.name = "ConfirmVeil"
	back.color = Color(0.09, 0.08, 0.06, 0.55)
	back.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	back.mouse_filter = Control.MOUSE_FILTER_STOP
	add_child(back)
	var box := PanelContainer.new()
	box.anchor_left = 0.5
	box.anchor_right = 0.5
	box.anchor_top = 0.5
	box.anchor_bottom = 0.5
	box.offset_left = -220
	box.offset_right = 220
	box.offset_top = -90
	box.offset_bottom = 90
	box.add_theme_stylebox_override("panel", GufengTheme.panel(14))
	back.add_child(box)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 10)
	box.add_child(vb)
	var l := _label("删掉这盘棋谱？删了就找不回来。", 15, GufengTheme.INK, true)
	vb.add_child(l)
	var row := HBoxContainer.new()
	row.alignment = BoxContainer.ALIGNMENT_END
	vb.add_child(row)
	var cancel := Button.new()
	cancel.text = "取消"
	GufengTheme.apply_button(cancel)
	cancel.pressed.connect(_close_confirm)
	row.add_child(cancel)
	var ok := Button.new()
	ok.text = "删除"
	GufengTheme.apply_seal_button(ok, false)
	ok.pressed.connect(func():
		var game_id := _selected_id
		_close_confirm()
		delete_requested.emit(game_id))
	row.add_child(ok)
	_confirm = back

func _close_confirm() -> void:
	if _confirm != null:
		_confirm.queue_free()
		_confirm = null

func _unhandled_input(event: InputEvent) -> void:
	if not visible or not (event is InputEventKey):
		return
	var k := event as InputEventKey
	if not k.pressed or k.echo:
		return
	if k.keycode == KEY_ESCAPE:
		if _confirm != null:
			_close_confirm()
		else:
			close()
		get_viewport().set_input_as_handled()
		return
	if _confirm != null or _selected_id == 0:
		return
	if k.keycode == KEY_LEFT:
		_step_by(-1)
		get_viewport().set_input_as_handled()
	elif k.keycode == KEY_RIGHT:
		_step_by(1)
		get_viewport().set_input_as_handled()
