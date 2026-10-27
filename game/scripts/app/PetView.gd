class_name PetView
extends Control
## 桌宠主界面（PetShell 的“身体”）。
##
## 只负责窗口形态、布局、气泡、右键菜单、聊天面板与点击反馈：
##   · 主程序是桌宠；围棋由 PetShell 另开独立窗口
##   · 完整半身贴图（不裁圆、不做待机摇摆/倾斜）
##   · 悬浮球式拖动：按住跟手，松手带一点惯性，限制在屏幕内
##   · 尺寸可调（右键菜单），记住位置与尺寸
##   · 聊天气泡/记录只出现在右键聊天面板；桌宠气泡只用于待机情绪台词

signal interacted
signal chat_submitted(text: String)
signal chat_opened
signal chat_closed
signal files_opened
signal files_closed
signal files_refresh_requested
signal files_scan_requested
signal go_requested
signal size_selected(height: int)
signal quit_requested

const SIZE_PRESETS := [120, 160, 200, 240]
const SIZE_LABELS := ["小", "中", "大", "特大"]
const MARGIN := 10.0
const BUBBLE_TOP := 8.0
const MENU_W := 210.0
const CHAT_W := 230.0
const FILES_W := 300.0
const DRAG_CLICK_MAX := 4.0
const INERTIA_FRICTION := 6.0
const INERTIA_MAX := 1600.0
const BLUSH_SEC := 1.6

var pet_height := 160

var _tex: Texture2D
var _pet_anchor: Control
var _pet_tex: TextureRect
var _bubble: PanelContainer
var _bubble_label: Label
var _bubble_until := 0
var _menu: PanelContainer
var _menu_veil: ColorRect
var _chat: PanelContainer
var _chat_log: VBoxContainer
var _chat_scroll: ScrollContainer
var _chat_input: LineEdit
var _chat_send: Button
var _chat_status: Label
var _files: PanelContainer
var _files_scroll: ScrollContainer
var _files_folder: Label
var _files_rows: VBoxContainer
var _schedule_rows: VBoxContainer
var _files_status: Label
var _files_scan: Button
var _files_refresh: Button
var _online_dot: Label
var _fx: FxLayer

var _chat_open := false
var _files_open := false
var _menu_open := false
var _pet_box := Rect2()
var _window_dragging := false
var _drag_moved := false
var _press_mouse := Vector2i.ZERO
var _press_win := Vector2i.ZERO
var _last_motion_ms := 0
var _last_drag_d := Vector2.ZERO
var _drag_vel := Vector2.ZERO
var _inertia := false
var _blush := 0.0
var _blush_until := 0
var _particles: Array[Dictionary] = []
var _ripples: Array[Dictionary] = []
var _t := 0.0

class FxLayer extends Control:
	var host = null

	func _draw() -> void:
		if host != null:
			host.draw_fx(self)

func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	if theme == null:
		theme = GufengTheme.build_theme(GufengTheme.font())
	_tex = load("res://assets/azure_pet.png") as Texture2D
	_build()
	resized.connect(_layout)
	_apply_window_size(_base_size(), false)
	_restore_position()
	_layout()
	set_process(true)

# ================= 构建 =================

func _build() -> void:
	_bubble = PanelContainer.new()
	_bubble.name = "PetBubble"
	_bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
	_bubble.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_bubble.visible = false
	add_child(_bubble)
	_bubble_label = Label.new()
	_bubble_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bubble_label.max_lines_visible = 4
	_bubble_label.add_theme_font_size_override("font_size", 13)
	_bubble_label.add_theme_color_override("font_color", GufengTheme.INK)
	_bubble_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_bubble.add_child(_bubble_label)

	_pet_anchor = Control.new()
	_pet_anchor.name = "PetBody"
	_pet_anchor.mouse_filter = Control.MOUSE_FILTER_STOP
	_pet_anchor.mouse_default_cursor_shape = Control.CURSOR_DRAG
	_pet_anchor.gui_input.connect(_on_pet_input)
	add_child(_pet_anchor)
	_pet_tex = TextureRect.new()
	_pet_tex.name = "PetTexture"
	_pet_tex.texture = _tex
	_pet_tex.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_pet_tex.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_pet_tex.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_pet_tex.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_pet_anchor.add_child(_pet_tex)

	_fx = FxLayer.new()
	_fx.host = self
	_fx.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_fx.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_fx)

	_build_menu()
	_build_chat()
	_build_files()

func _build_menu() -> void:
	# 菜单打开时的薄纱：既压暗背景，也让「点空白处收起菜单」在透明窗上可靠命中
	_menu_veil = ColorRect.new()
	_menu_veil.name = "PetMenuVeil"
	_menu_veil.color = Color(0.04, 0.10, 0.10, 0.10)
	_menu_veil.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_menu_veil.mouse_filter = Control.MOUSE_FILTER_STOP
	_menu_veil.visible = false
	_menu_veil.gui_input.connect(func(event: InputEvent):
		if event is InputEventMouseButton and (event as InputEventMouseButton).pressed:
			close_menu())
	add_child(_menu_veil)

	_menu = PanelContainer.new()
	_menu.name = "PetMenu"
	_menu.add_theme_stylebox_override("panel", GufengTheme.panel(9))
	_menu.mouse_filter = Control.MOUSE_FILTER_STOP
	_menu.visible = false
	add_child(_menu)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 5)
	_menu.add_child(vb)

	var head := HBoxContainer.new()
	head.add_theme_constant_override("separation", 6)
	vb.add_child(head)
	_online_dot = Label.new()
	_online_dot.text = "●"
	_online_dot.add_theme_font_size_override("font_size", 9)
	_online_dot.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	head.add_child(_online_dot)
	var title := Label.new()
	title.text = "Azure"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	title.add_theme_font_size_override("font_size", 13)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	head.add_child(title)
	var close := Button.new()
	close.text = "×"
	close.custom_minimum_size = Vector2(24, 24)
	GufengTheme.apply_bare_button(close)
	close.pressed.connect(close_menu)
	head.add_child(close)

	_rule(vb)

	var chat := _menu_button(vb, "聊天")
	chat.tooltip_text = "打开聊天面板（聊天回复用 TTS，不冒泡）"
	chat.pressed.connect(func():
		close_menu()
		open_chat())

	var files := _menu_button(vb, "资料与日程")
	files.tooltip_text = "把 assistant_files/inbox 的资料经 OCR / VLM 整理成日程"
	files.pressed.connect(func():
		close_menu()
		open_files())

	var go := _menu_button(vb, "围棋陪练", true)
	go.tooltip_text = "单独打开围棋窗口；关闭后棋局保留"
	go.pressed.connect(func():
		close_menu()
		go_requested.emit())

	var size_row := HBoxContainer.new()
	size_row.add_theme_constant_override("separation", 4)
	vb.add_child(size_row)
	var sl := Label.new()
	sl.text = "大小"
	sl.custom_minimum_size = Vector2(30, 0)
	sl.add_theme_font_size_override("font_size", 11)
	sl.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	size_row.add_child(sl)
	for i in SIZE_PRESETS.size():
		var b := Button.new()
		b.text = SIZE_LABELS[i]
		b.custom_minimum_size = Vector2(34, 26)
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		GufengTheme.apply_button(b)
		b.pressed.connect(_on_size_pick.bind(int(SIZE_PRESETS[i])))
		size_row.add_child(b)

	_rule(vb)

	var quit := _menu_button(vb, "退出桌宠")
	quit.tooltip_text = "真正退出程序"
	quit.pressed.connect(func():
		close_menu()
		quit_requested.emit())

	var hint := Label.new()
	hint.text = "左键摸摸 · 拖动悬浮球 · 右键菜单"
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.custom_minimum_size = Vector2(176, 0)
	hint.add_theme_font_size_override("font_size", 9)
	hint.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.8))
	vb.add_child(hint)
	_menu.reset_size()

func _menu_button(parent: Node, text: String, primary := false) -> Button:
	var b := Button.new()
	b.text = text
	b.custom_minimum_size = Vector2(176, 30)
	b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if primary:
		GufengTheme.apply_seal_button(b, false)
	else:
		GufengTheme.apply_button(b)
	parent.add_child(b)
	return b

func _rule(parent: Node) -> void:
	var r := ColorRect.new()
	r.color = Color(GufengTheme.INK, 0.20)
	r.custom_minimum_size = Vector2(0, 1)
	r.mouse_filter = Control.MOUSE_FILTER_IGNORE
	parent.add_child(r)

func _build_chat() -> void:
	_chat = PanelContainer.new()
	_chat.name = "ChatPanel"
	_chat.add_theme_stylebox_override("panel", GufengTheme.panel(9))
	_chat.mouse_filter = Control.MOUSE_FILTER_STOP
	_chat.visible = false
	add_child(_chat)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 6)
	_chat.add_child(vb)

	var head := HBoxContainer.new()
	vb.add_child(head)
	var title := Label.new()
	title.text = "Azure · 聊天"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	title.add_theme_font_size_override("font_size", 13)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	head.add_child(title)
	var close := Button.new()
	close.text = "×"
	close.custom_minimum_size = Vector2(24, 24)
	GufengTheme.apply_bare_button(close)
	close.pressed.connect(close_chat)
	head.add_child(close)

	_rule(vb)

	_chat_scroll = ScrollContainer.new()
	_chat_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_chat_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	vb.add_child(_chat_scroll)
	_chat_log = VBoxContainer.new()
	_chat_log.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_chat_log.custom_minimum_size = Vector2(CHAT_W - 34, 0)  # ScrollContainer 不会自动把子项拉到视口宽
	_chat_log.add_theme_constant_override("separation", 5)
	_chat_scroll.add_child(_chat_log)

	_chat_status = Label.new()
	_chat_status.add_theme_font_size_override("font_size", 10)
	_chat_status.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_chat_status.text = ""
	vb.add_child(_chat_status)

	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 4)
	vb.add_child(row)
	_chat_input = LineEdit.new()
	_chat_input.placeholder_text = "和 Azure 说点什么…"
	_chat_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_chat_input.text_submitted.connect(func(_t: String): _submit_chat())
	row.add_child(_chat_input)
	_chat_send = Button.new()
	_chat_send.text = "发送"
	_chat_send.custom_minimum_size = Vector2(48, 28)
	GufengTheme.apply_seal_button(_chat_send, false)
	_chat_send.pressed.connect(_submit_chat)
	row.add_child(_chat_send)

func _build_files() -> void:
	_files = PanelContainer.new()
	_files.name = "FilesPanel"
	_files.add_theme_stylebox_override("panel", GufengTheme.panel(9))
	_files.mouse_filter = Control.MOUSE_FILTER_STOP
	_files.visible = false
	add_child(_files)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 6)
	_files.add_child(vb)

	var head := HBoxContainer.new()
	vb.add_child(head)
	var title := Label.new()
	title.text = "Azure · 资料与日程"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	title.add_theme_font_size_override("font_size", 13)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	head.add_child(title)
	var close := Button.new()
	close.text = "×"
	close.custom_minimum_size = Vector2(24, 24)
	GufengTheme.apply_bare_button(close)
	close.pressed.connect(close_files)
	head.add_child(close)

	_files_folder = Label.new()
	_files_folder.text = "资料箱：assistant_files/inbox"
	_files_folder.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_files_folder.custom_minimum_size = Vector2(FILES_W - 34, 0)
	_files_folder.add_theme_font_size_override("font_size", 10)
	_files_folder.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(_files_folder)

	var hint := Label.new()
	hint.text = "把图片 / PDF / 文档放进资料箱，点「开始处理」；OCR + VLM 会整理成日程。"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.custom_minimum_size = Vector2(FILES_W - 34, 0)
	hint.add_theme_font_size_override("font_size", 10)
	hint.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.9))
	vb.add_child(hint)

	_files_scroll = ScrollContainer.new()
	_files_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_files_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	vb.add_child(_files_scroll)
	var log := VBoxContainer.new()
	log.custom_minimum_size = Vector2(FILES_W - 34, 0)
	log.add_theme_constant_override("separation", 4)
	_files_scroll.add_child(log)

	var fh := Label.new()
	fh.text = "资料文件"
	fh.add_theme_font_size_override("font_size", 11)
	fh.add_theme_color_override("font_color", GufengTheme.INK)
	log.add_child(fh)
	_files_rows = VBoxContainer.new()
	_files_rows.add_theme_constant_override("separation", 3)
	log.add_child(_files_rows)

	_rule(log)
	var sh := Label.new()
	sh.text = "日程"
	sh.add_theme_font_size_override("font_size", 11)
	sh.add_theme_color_override("font_color", GufengTheme.INK)
	log.add_child(sh)
	_schedule_rows = VBoxContainer.new()
	_schedule_rows.add_theme_constant_override("separation", 3)
	log.add_child(_schedule_rows)

	_files_status = Label.new()
	_files_status.text = ""
	_files_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_files_status.custom_minimum_size = Vector2(FILES_W - 34, 0)
	_files_status.add_theme_font_size_override("font_size", 10)
	_files_status.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(_files_status)

	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 4)
	vb.add_child(row)
	_files_refresh = Button.new()
	_files_refresh.text = "刷新"
	_files_refresh.custom_minimum_size = Vector2(54, 28)
	GufengTheme.apply_button(_files_refresh)
	_files_refresh.pressed.connect(func(): files_refresh_requested.emit())
	row.add_child(_files_refresh)
	_files_scan = Button.new()
	_files_scan.text = "开始处理"
	_files_scan.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_scan.custom_minimum_size = Vector2(110, 28)
	GufengTheme.apply_seal_button(_files_scan, false)
	_files_scan.pressed.connect(func(): files_scan_requested.emit())
	row.add_child(_files_scan)

func _submit_chat() -> void:
	var text := _chat_input.text.strip_edges()
	if text == "" or _chat_send.disabled:
		return
	_chat_input.text = ""
	append_chat("user", text)
	chat_submitted.emit(text)

# ================= 尺寸 / 窗口 / 布局 =================

func _pet_width() -> float:
	if _tex == null or _tex.get_height() <= 0:
		return float(pet_height)
	return float(pet_height) * float(_tex.get_width()) / float(_tex.get_height())

func _base_size() -> Vector2i:
	var pw := _pet_width()
	return Vector2i(maxi(240, int(round(pw)) + 32), maxi(280, pet_height + 110))

func _chat_size() -> Vector2i:
	var pw := _pet_width()
	return Vector2i(maxi(450, int(round(pw)) + 280), maxi(470, pet_height + 120))

func _files_size() -> Vector2i:
	var pw := _pet_width()
	return Vector2i(maxi(640, int(round(pw)) + int(FILES_W) + 56), maxi(500, pet_height + 170))

func _pet_rect_for(win: Vector2i) -> Rect2:
	var pw := _pet_width()
	var x := (float(win.x) - pw) * 0.5
	if _chat_open or _files_open:
		x = float(win.x) - pw - MARGIN
	var y := float(win.y) - float(pet_height) - MARGIN
	return Rect2(Vector2(x, y), Vector2(pw, float(pet_height)))

func _apply_window_size(target: Vector2i, keep_pet_anchor := true) -> void:
	if DisplayServer.get_name() == "headless":
		return
	var old_win := DisplayServer.window_get_size()
	var old_pet := _pet_rect_for(old_win)
	var old_br := DisplayServer.window_get_position() + Vector2i(
		int(round(old_pet.position.x + old_pet.size.x)),
		int(round(old_pet.position.y + old_pet.size.y)))
	DisplayServer.window_set_size(target)
	var new_pet := _pet_rect_for(target)
	var pos := DisplayServer.window_get_position()
	if keep_pet_anchor:
		pos = old_br - Vector2i(int(round(new_pet.position.x + new_pet.size.x)),
			int(round(new_pet.position.y + new_pet.size.y)))
	pos = _clamp_window_pos(pos, target)
	DisplayServer.window_set_position(pos)
	call_deferred("_layout")

func _clamp_window_pos(pos: Vector2i, win_size := Vector2i.ZERO) -> Vector2i:
	var s := win_size if win_size.x > 0 else DisplayServer.window_get_size()
	var scr := DisplayServer.screen_get_usable_rect(DisplayServer.window_get_current_screen())
	var max_x := scr.position.x + maxi(0, scr.size.x - s.x)
	var max_y := scr.position.y + maxi(0, scr.size.y - s.y)
	return Vector2i(clampi(pos.x, scr.position.x, max_x), clampi(pos.y, scr.position.y, max_y))

func _layout() -> void:
	if _tex == null:
		return
	var s := size
	if s.x < 1.0 or s.y < 1.0:
		s = get_viewport_rect().size
	_pet_box = _pet_rect_for(Vector2i(int(s.x), int(s.y)))
	_pet_anchor.position = _pet_box.position
	_pet_anchor.size = _pet_box.size
	if _bubble != null:
		_bubble.visible = _bubble.visible and not _chat_open and not _files_open
		if _bubble.visible:
			_relayout_bubble()
	if _chat != null:
		_chat.position = Vector2(MARGIN, MARGIN)
		_chat.size = Vector2(CHAT_W, maxf(80.0, s.y - MARGIN * 2.0))
		_chat.visible = _chat_open
	if _files != null:
		_files.position = Vector2(MARGIN, MARGIN)
		_files.size = Vector2(FILES_W, maxf(160.0, s.y - MARGIN * 2.0))
		_files.visible = _files_open
	queue_redraw()

func set_pet_height(h: int) -> void:
	var nh := clampi(h, 96, 360)
	if nh == pet_height:
		return
	var old_win := DisplayServer.window_get_size()
	var old_pet := _pet_rect_for(old_win)
	var old_br := DisplayServer.window_get_position() + Vector2i(
		int(round(old_pet.position.x + old_pet.size.x)),
		int(round(old_pet.position.y + old_pet.size.y)))
	pet_height = nh
	var target := _files_size() if _files_open else (_chat_size() if _chat_open else _base_size())
	DisplayServer.window_set_size(target)
	var new_pet := _pet_rect_for(target)
	var pos := old_br - Vector2i(int(round(new_pet.position.x + new_pet.size.x)),
		int(round(new_pet.position.y + new_pet.size.y)))
	DisplayServer.window_set_position(_clamp_window_pos(pos, target))
	call_deferred("_layout")
	save_position()

func _on_size_pick(h: int) -> void:
	set_pet_height(h)
	size_selected.emit(pet_height)

func open_chat() -> void:
	if _chat_open:
		return
	if _files_open:
		close_files()
	_chat_open = true
	_menu.visible = false
	_menu_open = false
	_bubble.visible = false
	_apply_window_size(_chat_size(), true)
	_layout()
	_chat.visible = true
	_chat_input.grab_focus()
	chat_opened.emit()

func close_chat() -> void:
	if not _chat_open:
		return
	_chat_open = false
	_chat.visible = false
	_apply_window_size(_base_size(), true)
	_layout()
	chat_closed.emit()

func is_chat_open() -> bool:
	return _chat_open

# ================= 资料与日程 =================

func open_files() -> void:
	if _files_open:
		return
	if _chat_open:
		close_chat()
	_files_open = true
	_menu.visible = false
	_menu_open = false
	_bubble.visible = false
	_apply_window_size(_files_size(), true)
	_layout()
	_files.visible = true
	files_opened.emit()

func close_files() -> void:
	if not _files_open:
		return
	_files_open = false
	_files.visible = false
	_apply_window_size(_base_size(), true)
	_layout()
	files_closed.emit()

func is_files_open() -> bool:
	return _files_open

func set_files_info(folder: String, files: Array) -> void:
	if _files_folder != null:
		_files_folder.text = "资料箱：" + str(folder)
	if _files_rows == null:
		return
	for c in _files_rows.get_children():
		c.queue_free()
	if files.is_empty():
		_files_rows.add_child(_files_hint("（资料箱还是空的，放些图片 / PDF / 文档进来）"))
		return
	for it in files:
		if typeof(it) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = it
		var name := str(d.get("name", "")).strip_edges()
		if name == "":
			continue
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 4)
		var lbl := Label.new()
		lbl.text = "· " + name
		lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		lbl.autowrap_mode = TextServer.AUTOWRAP_ARBITRARY
		lbl.add_theme_font_size_override("font_size", 10)
		lbl.add_theme_color_override("font_color", GufengTheme.INK)
		var err := str(d.get("error", "")).strip_edges()
		if err != "":
			lbl.text += "（%s）" % err
			lbl.add_theme_color_override("font_color", GufengTheme.CINNABAR)
		row.add_child(lbl)
		_files_rows.add_child(row)

func set_schedule_items(items: Array) -> void:
	if _schedule_rows == null:
		return
	for c in _schedule_rows.get_children():
		c.queue_free()
	if items.is_empty():
		_schedule_rows.add_child(_files_hint("（还没有整理出日程）"))
		return
	for it in items:
		if typeof(it) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = it
		var title := str(d.get("title", "")).strip_edges()
		if title == "":
			continue
		var when := str(d.get("when_text", "")).strip_edges()
		var lbl := Label.new()
		lbl.text = "%s  %s" % [when, title]
		lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		lbl.custom_minimum_size = Vector2(FILES_W - 40, 0)
		lbl.add_theme_font_size_override("font_size", 10)
		lbl.add_theme_color_override("font_color", GufengTheme.INK)
		var notes := str(d.get("notes", "")).strip_edges()
		var src := str(d.get("source", "")).strip_edges()
		var tips: Array[String] = []
		for x in [notes, src]:
			if str(x) != "":
				tips.append(str(x))
		lbl.tooltip_text = "\n".join(tips)
		_schedule_rows.add_child(lbl)

func set_files_status(text: String) -> void:
	if _files_status != null:
		_files_status.text = str(text)

func set_files_busy(on: bool) -> void:
	if _files_scan != null:
		_files_scan.disabled = on
	if _files_refresh != null:
		_files_refresh.disabled = on
	if on:
		set_files_status("正在用 OCR / VLM 阅读资料，再整理成日程…")

func _files_hint(text: String) -> Label:
	var lbl := Label.new()
	lbl.text = text
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(FILES_W - 40, 0)
	lbl.add_theme_font_size_override("font_size", 10)
	lbl.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.9))
	return lbl

# ================= 气泡 =================

func show_bubble(text: String, secs := 0.0) -> void:
	if _chat_open or _files_open or _bubble == null:
		return
	var clean := str(text).strip_edges()
	if clean == "":
		return
	_bubble_label.text = clean
	_bubble.visible = true
	_bubble_until = Time.get_ticks_msec() + int((secs if secs > 0.0 else _bubble_ttl(clean)) * 1000.0)
	_relayout_bubble()
	queue_redraw()

func _relayout_bubble() -> void:
	var avail := maxf(120.0, size.x - 16.0)
	var pet_top := _pet_box.position.y if _pet_box.size.y > 0 else size.y - pet_height - MARGIN
	var max_h := maxf(48.0, pet_top - BUBBLE_TOP - 8.0)
	_bubble_label.custom_minimum_size = Vector2(avail - 22.0, 0)
	var h := 52.0
	var font: Font = _bubble_label.get_theme_font("font")
	if font != null:
		var ts := font.get_multiline_string_size(_bubble_label.text, HORIZONTAL_ALIGNMENT_LEFT,
			avail - 22.0, _bubble_label.get_theme_font_size("font_size"))
		h = clampf(ts.y + 22.0, 45.0, max_h)
	_bubble.position = Vector2(8.0, BUBBLE_TOP)
	_bubble.size = Vector2(avail, h)
	queue_redraw()

func _bubble_ttl(text: String) -> float:
	return clampf(3.0 + float(text.length()) * 0.06, 3.5, 8.0)

# ================= 右键菜单 =================

func open_menu(pos: Vector2) -> void:
	if _menu == null:
		return
	_menu_veil.visible = true
	_menu.visible = true
	_menu_open = true
	_menu.reset_size()
	_menu.size = _menu.get_combined_minimum_size()
	var mx := maxf(4.0, size.x - _menu.size.x - 4.0)
	var my := maxf(4.0, size.y - _menu.size.y - 4.0)
	_menu.position = Vector2(clampf(pos.x, 4.0, mx), clampf(pos.y, 4.0, my))

func close_menu() -> void:
	if _menu != null:
		_menu.visible = false
	if _menu_veil != null:
		_menu_veil.visible = false
	_menu_open = false

func is_menu_open() -> bool:
	return _menu_open

# ================= 拖动（悬浮球手感） =================

func _on_pet_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_RIGHT and mb.pressed:
			open_menu(_pet_anchor.position + mb.position)
			accept_event()
			return
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_begin_drag()
				accept_event()
			elif _window_dragging:
				_finish_drag()
				accept_event()

func _begin_drag() -> void:
	_inertia = false
	_press_mouse = DisplayServer.mouse_get_position()
	_press_win = DisplayServer.window_get_position()
	_last_motion_ms = Time.get_ticks_msec()
	_last_drag_d = Vector2.ZERO
	_drag_vel = Vector2.ZERO
	_drag_moved = false
	_window_dragging = true

## 拖动不依赖 MouseMotion 事件：每帧读一次全局鼠标位置。
## 透明置顶窗在部分 Windows / DWM 组合下会把 motion 事件延后到松开之后，
## 轮询光标才能保证「按住就跟手」，也顺便让惯性速度更平滑。
func _tick_drag(delta: float) -> void:
	if not _window_dragging:
		return
	if not Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT):
		_finish_drag()
		return
	var mp := DisplayServer.mouse_get_position()
	var d := Vector2(mp - _press_mouse)
	if d.length() > DRAG_CLICK_MAX:
		_drag_moved = true
	var now := Time.get_ticks_msec()
	var dt := maxf(0.001, float(now - _last_motion_ms) / 1000.0)
	if _drag_moved:
		_drag_vel = (d - _last_drag_d) / dt
		if _drag_vel.length() > INERTIA_MAX:
			_drag_vel = _drag_vel.normalized() * INERTIA_MAX
		DisplayServer.window_set_position(_clamp_window_pos(_press_win + Vector2i(d)))
	_last_drag_d = d
	_last_motion_ms = now

func _finish_drag() -> void:
	if not _window_dragging:
		return
	_window_dragging = false
	if _drag_moved:
		_start_inertia()
	else:
		interacted.emit()

func _start_inertia() -> void:
	if _drag_vel.length() < 110.0:
		save_position()
		return
	_inertia = true

func _process(delta: float) -> void:
	_t += delta
	_tick_drag(delta)
	if _inertia:
		var pos := DisplayServer.window_get_position()
		var step := _drag_vel * delta
		var target := Vector2i(int(round(pos.x + step.x)), int(round(pos.y + step.y)))
		var clamped := _clamp_window_pos(target)
		if clamped.x != target.x:
			_drag_vel.x = 0.0
		if clamped.y != target.y:
			_drag_vel.y = 0.0
		DisplayServer.window_set_position(clamped)
		_drag_vel *= exp(-INERTIA_FRICTION * delta)
		if _drag_vel.length() < 18.0:
			_inertia = false
			save_position()
	if _bubble != null and _bubble.visible and not _chat_open \
			and Time.get_ticks_msec() >= _bubble_until:
		_bubble.visible = false
		queue_redraw()
	if _blush > 0.0 and Time.get_ticks_msec() >= _blush_until:
		_blush = 0.0
	_update_particles(delta)
	_fx.queue_redraw()
	queue_redraw()

func _update_particles(delta: float) -> void:
	var keep: Array[Dictionary] = []
	for p in _particles:
		p["age"] = float(p["age"]) + delta
		if float(p["age"]) < float(p["life"]):
			p["pos"] = (p["pos"] as Vector2) + (p["vel"] as Vector2) * delta
			p["vel"] = (p["vel"] as Vector2) + Vector2(0.0, 36.0 * delta)
			keep.append(p)
	_particles = keep
	var keep_r: Array[Dictionary] = []
	for r in _ripples:
		r["age"] = float(r["age"]) + delta
		if float(r["age"]) < float(r["life"]):
			keep_r.append(r)
	_ripples = keep_r

# ================= 绘制 / 点击反馈 =================

func _draw() -> void:
	# 气泡尾巴（不画任何待机摇摆 / 光环抖动）
	if _bubble != null and _bubble.visible and not _chat_open:
		var bx := _bubble.position.x + _bubble.size.x * 0.5
		var by := _bubble.position.y + _bubble.size.y - 1.0
		draw_colored_polygon(PackedVector2Array([
			Vector2(bx - 7.0, by - 2.0),
			Vector2(bx + 7.0, by - 2.0),
			Vector2(bx, by + 8.0),
		]), Color(GufengTheme.PANEL_SOFT.r, GufengTheme.PANEL_SOFT.g,
			GufengTheme.PANEL_SOFT.b, 0.97))

func draw_fx(c: CanvasItem) -> void:
	if _blush > 0.01 and _pet_box.size.x > 0.0:
		var fc := _pet_box.position
		var sz := _pet_box.size
		var a := _blush * 0.38
		c.draw_circle(fc + Vector2(sz.x * 0.43, sz.y * 0.62), sz.x * 0.037,
			Color(0.94, 0.52, 0.58, a))
		c.draw_circle(fc + Vector2(sz.x * 0.67, sz.y * 0.62), sz.x * 0.037,
			Color(0.94, 0.52, 0.58, a))
	for p in _particles:
		var life := float(p["life"])
		var age := float(p["age"])
		var k := clampf(1.0 - age / maxf(life, 0.01), 0.0, 1.0)
		var pp: Vector2 = p["pos"]
		var pcol: Color = p["color"]
		pcol.a *= k
		if str(p["kind"]) == "heart":
			c.draw_colored_polygon(_heart_points(pp, float(p["size"]) * (0.7 + 0.3 * k)), pcol)
		else:
			var s := float(p["size"]) * (0.5 + 0.5 * k)
			c.draw_colored_polygon(PackedVector2Array([
				pp + Vector2(0.0, -s), pp + Vector2(s * 0.3, 0.0),
				pp + Vector2(0.0, s), pp + Vector2(-s * 0.3, 0.0),
			]), pcol)
	for r in _ripples:
		var life := float(r["life"])
		var age := float(r["age"])
		var k := clampf(age / maxf(life, 0.01), 0.0, 1.0)
		var rr := float(r["r"]) + k * 42.0
		var rp: Vector2 = r["pos"]
		var rcol: Color = r["color"]
		rcol.a *= (1.0 - k) * 0.50
		c.draw_arc(rp, rr, 0.0, TAU, 28, rcol, 1.8, true)

func _heart_points(center: Vector2, s: float) -> PackedVector2Array:
	var pts := PackedVector2Array()
	var n := 18
	for i in n:
		var t := TAU * float(i) / float(n)
		var x := 16.0 * pow(sin(t), 3.0)
		var y := -(13.0 * cos(t) - 5.0 * cos(2.0 * t) - 2.0 * cos(3.0 * t) - cos(4.0 * t))
		pts.append(center + Vector2(x, y) * (s / 16.0))
	return pts

func play_pat_effect() -> void:
	_blush = 1.0
	_blush_until = Time.get_ticks_msec() + int(BLUSH_SEC * 1000.0)
	_spawn_hearts(4)
	_spawn_ripple(_pet_box.position + Vector2(_pet_box.size.x * 0.5, _pet_box.size.y * 0.82), 5.0)

func play_actions(acts: Array) -> void:
	var played := 0
	for a in acts:
		match str(a):
			"blush":
				_blush = 1.0
				_blush_until = Time.get_ticks_msec() + int(BLUSH_SEC * 1000.0)
			"smile", "laugh":
				_spawn_hearts(3)
			"proud", "surprise":
				_spawn_sparkles(3)
			"worried", "sad":
				_spawn_sparkles(2, Color(GufengTheme.CINNABAR, 0.8))
			_:
				continue
		played += 1
		if played >= 2:
			break

func _spawn_hearts(n: int) -> void:
	var sz := _pet_box.size
	for i in n:
		var angle := randf_range(-2.6, -0.5)
		_particles.append({
			"kind": "heart",
			"pos": _pet_box.position + Vector2(sz.x * randf_range(0.28, 0.72), sz.y * randf_range(0.32, 0.60)),
			"vel": Vector2(cos(angle), sin(angle)) * randf_range(24.0, 48.0),
			"age": 0.0, "life": randf_range(0.9, 1.4), "size": randf_range(5.0, 9.0),
			"color": Color(GufengTheme.CINNABAR.r, GufengTheme.CINNABAR.g,
				GufengTheme.CINNABAR.b, randf_range(0.55, 0.9)),
		})
	while _particles.size() > 24:
		_particles.pop_front()

func _spawn_sparkles(n: int, color := Color(0, 0, 0, 0)) -> void:
	var col := color
	if col.a <= 0.0:
		col = Color(GufengTheme.DAI.r, GufengTheme.DAI.g, GufengTheme.DAI.b, 0.9)
	var sz := _pet_box.size
	for i in n:
		_particles.append({
			"kind": "spark",
			"pos": _pet_box.position + Vector2(sz.x * randf_range(0.2, 0.8), sz.y * randf_range(0.2, 0.7)),
			"vel": Vector2(randf_range(-20.0, 20.0), randf_range(-38.0, -18.0)),
			"age": 0.0, "life": randf_range(0.6, 1.0), "size": randf_range(3.5, 7.0),
			"color": col,
		})

func _spawn_ripple(pos: Vector2, radius: float) -> void:
	_ripples.append({"pos": pos, "r": radius, "age": 0.0, "life": 0.7,
		"color": Color(GufengTheme.DAI.r, GufengTheme.DAI.g, GufengTheme.DAI.b, 0.7)})

# ================= 聊天面板 =================

func append_chat(role: String, text: String) -> void:
	if _chat_log == null:
		return
	var clean := str(text).strip_edges()
	if clean == "":
		return
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var bubble := PanelContainer.new()
	if role == "user":
		bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_user())
	else:
		bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
	bubble.size_flags_horizontal = Control.SIZE_SHRINK_END if role == "user" else Control.SIZE_SHRINK_BEGIN
	bubble.custom_minimum_size = Vector2(150, 0)
	if role == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		row.add_child(spacer)
	var lbl := Label.new()
	lbl.text = clean
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(150, 0)
	lbl.add_theme_font_size_override("font_size", 12)
	lbl.add_theme_color_override("font_color", GufengTheme.INK)
	bubble.add_child(lbl)
	row.add_child(bubble)
	if role != "user":
		var spacer2 := Control.new()
		spacer2.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		row.add_child(spacer2)
	_chat_log.add_child(row)
	_scroll_chat()

func set_chat_history(items: Array) -> void:
	if _chat_log == null:
		return
	for c in _chat_log.get_children():
		c.queue_free()
	for it in items:
		if typeof(it) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = it
		var u := str(d.get("user_text", "")).strip_edges()
		var a := str(d.get("reply_text", "")).strip_edges()
		if u != "":
			append_chat("user", u)
		if a != "":
			append_chat("assistant", a)
	_scroll_chat()

func set_chat_busy(on: bool) -> void:
	if _chat_input != null:
		_chat_input.editable = not on
	if _chat_send != null:
		_chat_send.disabled = on
	if _chat_status != null:
		_chat_status.text = "Azure 正在想…" if on else ""

func set_chat_status(text: String) -> void:
	if _chat_status != null:
		_chat_status.text = str(text)

func _scroll_chat() -> void:
	if _chat_scroll == null:
		return
	await get_tree().process_frame
	_chat_scroll.scroll_vertical = int(_chat_scroll.get_v_scroll_bar().max_value)

func set_online(on: bool) -> void:
	if _online_dot != null:
		_online_dot.add_theme_color_override("font_color",
			GufengTheme.DAI if on else GufengTheme.CINNABAR)

# ================= 位置持久化 =================

func _restore_position() -> void:
	var c := ConfigFile.new()
	var ok := c.load("user://pet_window.cfg") == OK
	var scr := DisplayServer.screen_get_usable_rect(DisplayServer.window_get_current_screen())
	var pos := scr.position + scr.size - DisplayServer.window_get_size() - Vector2i(24, 42)
	if ok:
		pos = Vector2i(int(c.get_value("window", "x", pos.x)), int(c.get_value("window", "y", pos.y)))
	DisplayServer.window_set_position(_clamp_window_pos(pos))

func save_position() -> void:
	if DisplayServer.get_name() == "headless":
		return
	var pos := DisplayServer.window_get_position()
	var c := ConfigFile.new()
	c.load("user://pet_window.cfg")
	c.set_value("window", "x", pos.x)
	c.set_value("window", "y", pos.y)
	c.save("user://pet_window.cfg")

## 独立围棋窗口关闭后：收起聊天面板，回到小桌宠尺寸与上次位置。
func restore_after_game() -> void:
	_chat_open = false
	_files_open = false
	_menu_open = false
	_inertia = false
	if _chat != null:
		_chat.visible = false
	if _files != null:
		_files.visible = false
	if _menu != null:
		_menu.visible = false
	if _bubble != null:
		_bubble.visible = false
	var target := _base_size()
	if DisplayServer.get_name() != "headless":
		DisplayServer.window_set_size(target)
		_restore_position()
	call_deferred("_layout")
