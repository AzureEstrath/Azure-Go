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
signal files_auto_plan_requested
signal files_archive_requested
signal files_cleanup_requested
signal files_review_requested
signal schedule_feedback_requested(id: int, outcome: String, actual_min: int)
signal go_requested
signal size_selected(height: int)
signal quit_requested

const SIZE_PRESETS := [120, 160, 200, 240]
const SIZE_LABELS := ["小", "中", "大", "特大"]
const MARGIN := 10.0
const BUBBLE_TOP := 8.0
const MENU_W := 210.0
const CHAT_W := 230.0
const FILES_W := 480.0
# 周课表网格：左侧节次列 + 7 个星期列 × 12 节。单元格外框与参考图一致，课程块跨行显示。
const TABLE_HEAD_H := 32.0
const TABLE_ROW_H := 26.0
const TABLE_TIME_W := 44.0
const TABLE_PERIODS := 12
# 标准节次开始时间（分钟）：第5节是午间空档，12:00-13:00 的约定会落在这一行。
const PERIOD_START_MIN := [480, 530, 590, 640, 700, 810, 860, 920, 970, 1110, 1160, 1210]
const EVENT_TINT := Color(0.96, 0.83, 0.56, 0.55)
const COURSE_TINTS := [
	Color(0.58, 0.84, 0.74, 0.40), Color(0.58, 0.80, 0.93, 0.40),
	Color(0.96, 0.82, 0.58, 0.40), Color(0.93, 0.70, 0.74, 0.40),
	Color(0.76, 0.74, 0.93, 0.40), Color(0.68, 0.88, 0.64, 0.40),
	Color(0.96, 0.89, 0.62, 0.40),
]
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
var _files_help: Label
var _files_rows: VBoxContainer
var _schedule_rows: VBoxContainer
var _schedule_table: Control
var _schedule_agenda: VBoxContainer
var _timetable: Dictionary = {}
var _files_status: Label
var _files_scan: Button
var _files_refresh: Button
var _files_auto_plan: Button
var _files_archive: Button
var _files_cleanup: Button
var _files_review: Button
var _files_close: Button
var _files_actions: GridContainer
var _files_bottom: GridContainer
var _reminder: PanelContainer
var _reminder_label: Label
var _reminder_until := 0
var _hover_card: PanelContainer
var _hover_label: Label
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

class GridLayer extends Control:
	"""周课表的底格：只画表头 / 网格线，课程块是它的子节点（可跨行）。"""
	var host = null

	func _draw() -> void:
		if host != null:
			host.draw_schedule_table(self, size)

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
	_build_cards()

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
	_files_close = close

	_files_folder = Label.new()
	_files_folder.text = "资料箱：assistant_files/inbox"
	_files_folder.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	# 自适应宽度，但保底 140：Label 的 autowrap 在 min 宽为 0 时会按 1px 排版（几百行高），
	# 反而把面板撑到几千像素、按钮全被挤出窗口；140 又能容纳窄窗口。
	_files_folder.custom_minimum_size = Vector2(140, 0)
	_files_folder.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_folder.add_theme_font_size_override("font_size", 10)
	_files_folder.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(_files_folder)

	var hint := Label.new()
	hint.text = "把图片 / PDF / 文档放进资料箱，点「开始处理」；OCR 会按行列还原课程表，Azure 再整理成日程。"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.custom_minimum_size = Vector2(140, 0)
	hint.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hint.add_theme_font_size_override("font_size", 10)
	hint.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.9))
	vb.add_child(hint)
	_files_help = hint

	_files_scroll = ScrollContainer.new()
	_files_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_files_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	vb.add_child(_files_scroll)
	var log := VBoxContainer.new()
	log.custom_minimum_size = Vector2(0, 0)
	log.size_flags_horizontal = Control.SIZE_EXPAND_FILL
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
	_files_status.custom_minimum_size = Vector2(140, 0)
	_files_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_status.add_theme_font_size_override("font_size", 10)
	_files_status.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(_files_status)

	# 操作区用 GridContainer：宽面板一行，窄面板自动折成两列/两排，避免最小宽度把面板撑出视口。
	_files_bottom = GridContainer.new()
	_files_bottom.columns = 3
	_files_bottom.add_theme_constant_override("h_separation", 4)
	_files_bottom.add_theme_constant_override("v_separation", 4)
	_files_bottom.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	vb.add_child(_files_bottom)
	_files_refresh = Button.new()
	_files_refresh.text = "刷新"
	_files_refresh.custom_minimum_size = Vector2(60, 28)
	GufengTheme.apply_button(_files_refresh)
	_files_refresh.pressed.connect(func(): files_refresh_requested.emit())
	_files_bottom.add_child(_files_refresh)
	_files_scan = Button.new()
	_files_scan.text = "开始处理"
	_files_scan.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_scan.custom_minimum_size = Vector2(84, 28)
	GufengTheme.apply_seal_button(_files_scan, false)
	_files_scan.pressed.connect(func(): files_scan_requested.emit())
	_files_bottom.add_child(_files_scan)
	# 底部始终可见的「收起」：窗口被限制 / 内嵌在编辑器小视口时，右上角的 × 可能够不到。
	var collapse := Button.new()
	collapse.text = "收起"
	collapse.custom_minimum_size = Vector2(60, 28)
	GufengTheme.apply_button(collapse)
	collapse.pressed.connect(close_files)
	_files_bottom.add_child(collapse)

	# 第二排：自动排班 / 留档 / 清理 / 复盘（窄面板折成 2×2）。
	_files_actions = GridContainer.new()
	_files_actions.columns = 4
	_files_actions.add_theme_constant_override("h_separation", 4)
	_files_actions.add_theme_constant_override("v_separation", 4)
	_files_actions.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	vb.add_child(_files_actions)
	_files_auto_plan = Button.new()
	_files_auto_plan.text = "自动排班"
	_files_auto_plan.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_auto_plan.custom_minimum_size = Vector2(70, 26)
	GufengTheme.apply_button(_files_auto_plan)
	_files_auto_plan.pressed.connect(func(): files_auto_plan_requested.emit())
	_files_actions.add_child(_files_auto_plan)
	_files_archive = Button.new()
	_files_archive.text = "归档过期"
	_files_archive.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_archive.custom_minimum_size = Vector2(70, 26)
	GufengTheme.apply_button(_files_archive)
	_files_archive.pressed.connect(func(): files_archive_requested.emit())
	_files_actions.add_child(_files_archive)
	_files_cleanup = Button.new()
	_files_cleanup.text = "清理留档"
	_files_cleanup.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_cleanup.custom_minimum_size = Vector2(70, 26)
	GufengTheme.apply_button(_files_cleanup)
	_files_cleanup.pressed.connect(func(): files_cleanup_requested.emit())
	_files_actions.add_child(_files_cleanup)
	_files_review = Button.new()
	_files_review.text = "复盘"
	_files_review.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_files_review.custom_minimum_size = Vector2(70, 26)
	GufengTheme.apply_button(_files_review)
	_files_review.pressed.connect(func(): files_review_requested.emit())
	_files_actions.add_child(_files_review)

func _build_cards() -> void:
	# 事务提醒卡：到点时由 PetShell 调 show_reminder()；压在聊天 / 资料面板之上，12 秒后自动消失。
	_reminder = PanelContainer.new()
	_reminder.name = "ReminderCard"
	_reminder.add_theme_stylebox_override("panel", _card_style())
	_reminder.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_reminder.visible = false
	add_child(_reminder)
	var rv := VBoxContainer.new()
	rv.mouse_filter = Control.MOUSE_FILTER_IGNORE
	rv.add_theme_constant_override("separation", 2)
	var head := Label.new()
	head.text = "事务提醒"
	head.add_theme_font_size_override("font_size", 9)
	head.add_theme_color_override("font_color", GufengTheme.ACCENT)
	head.mouse_filter = Control.MOUSE_FILTER_IGNORE
	rv.add_child(head)
	_reminder_label = Label.new()
	_reminder_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_reminder_label.custom_minimum_size = Vector2(220, 0)
	_reminder_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_reminder_label.add_theme_font_size_override("font_size", 12)
	_reminder_label.add_theme_color_override("font_color", GufengTheme.INK)
	_reminder_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	rv.add_child(_reminder_label)
	_reminder.add_child(rv)
	# 课表格子的悬停展开卡：完整显示课程名 / 教师 / 地点 / 时间 / 备注。
	_hover_card = PanelContainer.new()
	_hover_card.name = "HoverCard"
	_hover_card.add_theme_stylebox_override("panel", _card_style())
	_hover_card.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_hover_card.visible = false
	add_child(_hover_card)
	_hover_label = Label.new()
	_hover_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_hover_label.custom_minimum_size = Vector2(180, 0)
	_hover_label.add_theme_font_size_override("font_size", 10)
	_hover_label.add_theme_color_override("font_color", GufengTheme.INK)
	_hover_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_hover_card.add_child(_hover_label)

func _card_style() -> StyleBoxFlat:
	var sb := GufengTheme.slip(GufengTheme.PAPER_WARM, 9)
	sb.border_color = GufengTheme.ACCENT
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(10)
	sb.shadow_color = Color(0, 0, 0, 0.12)
	sb.shadow_size = 6
	return sb

func show_reminder(text: String, secs := 12.0) -> void:
	if _reminder == null or _reminder_label == null:
		return
	var clean := str(text).strip_edges()
	if clean == "":
		return
	_reminder_label.text = clean
	var w := minf(320.0, maxf(200.0, size.x - MARGIN * 2.0))
	_reminder.custom_minimum_size = Vector2(w - 18.0, 0)
	_reminder_label.custom_minimum_size = Vector2(w - 36.0, 0)
	_reminder.visible = true
	_reminder.position = Vector2(MARGIN, 40.0)
	_reminder.size = Vector2(w, _reminder.get_combined_minimum_size().y)
	move_child(_reminder, get_child_count() - 1)
	_reminder_until = Time.get_ticks_msec() + int(maxf(2.0, secs) * 1000.0)

func _show_hover_card(anchor: Control, d: Dictionary) -> void:
	if _hover_card == null or _hover_label == null or anchor == null:
		return
	var title := str(d.get("title", "")).strip_edges()
	if title == "":
		return
	var lines: Array[String] = [title]
	var when := str(d.get("when_text", "")).strip_edges()
	if when != "":
		lines.append(when)
	var grade := _grade_text(d)
	if grade != "":
		lines.append("分级：%s" % grade)
	var reason := _reason_text(d)
	if reason != "":
		lines.append("排班理由：%s" % reason)
	var teacher := str(d.get("teacher", "")).strip_edges()
	var location := str(d.get("location", "")).strip_edges()
	if teacher != "":
		lines.append("教师：%s" % teacher)
	if location != "":
		lines.append("地点：%s" % location)
	var notes := str(d.get("notes", "")).strip_edges()
	if notes != "":
		lines.append(notes)
	_hover_label.text = "\n".join(lines)
	var w := minf(260.0, maxf(160.0, size.x - 32.0))
	_hover_label.custom_minimum_size = Vector2(w - 24.0, 0)
	_hover_card.custom_minimum_size = Vector2(w - 12.0, 0)
	_hover_card.visible = true
	_hover_card.reset_size()
	var card := _hover_card.get_combined_minimum_size()
	card.x = maxf(card.x, w)
	var gp := anchor.get_global_rect()
	var origin := get_global_rect().position
	var pos := gp.position - origin + Vector2(gp.size.x + 4.0, 0.0)
	if pos.x + card.x > size.x - 8.0:
		pos.x = maxf(8.0, gp.position.x - origin.x - card.x - 4.0)
	pos.y = clampf(pos.y, 8.0, maxf(8.0, size.y - card.y - 8.0))
	_hover_card.position = pos
	_hover_card.size = card
	move_child(_hover_card, get_child_count() - 1)

func _hide_hover_card() -> void:
	if _hover_card != null:
		_hover_card.visible = false

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
	# 资料 / 日程单独用紧凑窗口（不再为了并排宠物撑到 700 宽）；关闭后宠物回来。
	return Vector2i(maxi(544, int(FILES_W) + 64), maxi(520, pet_height + 150))

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
		# 聊天面板也跟着视口收窄；窄窗口下仍然保留关闭入口。
		var cw := minf(CHAT_W, maxf(180.0, s.x - MARGIN * 2.0))
		_chat.position = Vector2(MARGIN, MARGIN)
		_chat.size = Vector2(cw, maxf(80.0, s.y - MARGIN * 2.0))
		_chat.visible = _chat_open
	if _files != null:
		# 面板宽度跟着**实际视口**走：小屏 / 内嵌窗口也不把关闭按钮挤出屏幕。
		var fw := minf(FILES_W, maxf(200.0, s.x - MARGIN * 2.0))
		# 窄面板把操作按钮折成两列，宽面板保持一行；避免按钮的最小宽度把面板撑出视口。
		if _files_actions != null:
			_files_actions.columns = 4 if fw >= 360.0 else 2
		if _files_bottom != null:
			_files_bottom.columns = 3 if fw >= 260.0 else 2
		# 极窄 / 矮的窗口里先让位给日程与底部按钮，操作提示不再占高度。
		if _files_help != null:
			_files_help.visible = s.y >= 360.0
		_files.position = Vector2(MARGIN, MARGIN)
		_files.size = Vector2(fw, maxf(160.0, s.y - MARGIN * 2.0))
		_files.visible = _files_open
		# 面板占满宽度时宠物会从半透明面板后面透出来：位置不够就把宠物藏起来。
		if _pet_anchor != null:
			var panel_w := 0.0
			if _files_open:
				panel_w = _files.size.x
			elif _chat_open:
				panel_w = CHAT_W
			var need := MARGIN * 3.0 + panel_w + _pet_width()
			_pet_anchor.visible = not ((_files_open or _chat_open) and s.x < need)
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
	_hide_hover_card()
	_apply_window_size(_base_size(), true)
	_layout()
	files_closed.emit()

func is_files_open() -> bool:
	return _files_open

func _unhandled_key_input(event: InputEvent) -> void:
	# Esc 也能收起面板：鼠标点不到右上角 / 底部按钮时还有键盘出口。
	if event != null and event.is_action_pressed("ui_cancel"):
		if _files_open:
			close_files()
			get_viewport().set_input_as_handled()
		elif _chat_open:
			close_chat()
			get_viewport().set_input_as_handled()
		elif _menu_open:
			close_menu()
			get_viewport().set_input_as_handled()

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
		lbl.custom_minimum_size = Vector2(120, 0)
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
	_hide_hover_card()
	for c in _schedule_rows.get_children():
		c.queue_free()
	_schedule_table = null
	_schedule_agenda = null
	_timetable = {}
	var courses: Array = []
	var timed: Array = []
	var plain: Array = []
	for it in items:
		if typeof(it) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = it
		if str(d.get("title", "")).strip_edges() == "":
			continue
		var wd := int(d.get("weekday", -1))
		var p0 := int(d.get("period_start", 0))
		if wd >= 0 and wd < 7 and p0 >= 1 and p0 <= TABLE_PERIODS:
			courses.append(d)
		else:
			plain.append(d)
			# 有明确时刻、且不是自动排班的约定：同时画进周表对应时段。
			var ev := _event_block(d)
			if not ev.is_empty():
				timed.append(ev)
	courses = _dedupe_courses(courses)
	timed = _dedupe_events(timed)
	if courses.is_empty() and plain.is_empty():
		_schedule_rows.add_child(_files_hint("（还没有整理出日程）"))
		return
	# 先把日程 / 约定列表放在周表上方：即使表格很长，也不会把「其他日程」埋到滚动区底部。
	if not plain.is_empty():
		_build_agenda(plain)
	if not courses.is_empty() or not timed.is_empty():
		_build_timetable(courses, timed)

func _dedupe_courses(courses: Array) -> Array:
	"""同一门课可能来自多周扫描：按「星期 + 起始节 + 标题」去重，保留日期最新的一条。"""
	var map := {}
	for it in courses:
		var d: Dictionary = it
		var key := "%d|%d|%s" % [int(d.get("weekday", -1)),
			int(d.get("period_start", 0)), str(d.get("title", ""))]
		if not map.has(key):
			map[key] = d
		else:
			var old: Dictionary = map[key]
			if str(d.get("date", "")) > str(old.get("date", "")):
				map[key] = d
	var out: Array = []
	for k in map.keys():
		out.append(map[k])
	out.sort_custom(func(a, b):
		if int(a.get("weekday", 0)) != int(b.get("weekday", 0)):
			return int(a.get("weekday", 0)) < int(b.get("weekday", 0))
		return int(a.get("period_start", 0)) < int(b.get("period_start", 0)))
	return out

func _weekday_from_item(d: Dictionary) -> int:
	"""优先用库里的 weekday；没有就从 date / start_ts 推（0=周一）。"""
	var wd := int(d.get("weekday", -1))
	if wd >= 0 and wd < 7:
		return wd
	var date := str(d.get("date", "")).strip_edges()
	if date.length() >= 10:
		var unix := int(Time.get_unix_time_from_datetime_string(date.substr(0, 10) + "T00:00:00"))
		if unix > 0:
			var dd := Time.get_datetime_dict_from_unix_time(unix)
			return (int(dd.get("weekday", 0)) + 6) % 7
	var ts := float(d.get("start_ts", 0.0))
	if ts > 0.0:
		var dd2 := Time.get_datetime_dict_from_unix_time(int(ts))
		return (int(dd2.get("weekday", 0)) + 6) % 7
	return -1

func _clock_minutes(text: String) -> int:
	var re := RegEx.new()
	re.compile("(\\d{1,2})\\s*[:：]\\s*(\\d{2})")
	var m := re.search(str(text))
	if m == null:
		return -1
	var h := int(m.get_string(1))
	var mi := int(m.get_string(2))
	if h > 23 or mi > 59:
		return -1
	return h * 60 + mi

func _item_start_minutes(d: Dictionary) -> int:
	var t := _clock_minutes(str(d.get("start_time", "")))
	if t >= 0:
		return t
	var when := str(d.get("when_text", ""))
	if "全天" in when:
		return -1
	return _clock_minutes(when)

func _period_index(minutes: int) -> int:
	var idx := 1
	for i in range(PERIOD_START_MIN.size()):
		if minutes >= PERIOD_START_MIN[i]:
			idx = i + 1
	return clampi(idx, 1, TABLE_PERIODS)

func _event_block(d: Dictionary) -> Dictionary:
	"""有明确时刻、尚未自动排班的约定 → 周表块（避免被只画课程的网格漏掉）。"""
	if bool(d.get("auto_planned", false)):
		return {}
	if str(d.get("grade", "")) == "closed":
		return {}
	if str(d.get("status", "planned")) != "planned":
		return {}
	var wd := _weekday_from_item(d)
	var start := _item_start_minutes(d)
	if wd < 0 or start < 0:
		return {}
	var dur := maxi(5, int(d.get("duration_min", 60)))
	var p0 := _period_index(start)
	var p1 := _period_index(start + dur - 1)
	var ev := d.duplicate()
	ev["_event_wd"] = wd
	ev["_event_p0"] = p0
	ev["_event_p1"] = maxi(p0, p1)
	return ev

func _dedupe_events(items: Array) -> Array:
	var map := {}
	for it in items:
		var d: Dictionary = it
		var key := "%d|%d|%s" % [int(d.get("_event_wd", -1)),
			int(d.get("_event_p0", 0)), str(d.get("title", ""))]
		if not map.has(key):
			map[key] = d
	var out: Array = []
	for k in map.keys():
		out.append(map[k])
	return out

func _period_time_map(courses: Array) -> Dictionary:
	"""节次 → 开始时间（只收每门课的起始节；结束节不冒充下一节的开始时间）。"""
	var out := {}
	for it in courses:
		var d: Dictionary = it
		var p := int(d.get("period_start", 0))
		var t0 := str(d.get("start_time", "")).strip_edges()
		if p >= 1 and t0 != "" and not out.has(p):
			out[p] = t0
	return out

func _add_timetable_block(grid: Control, col_w: float, d: Dictionary,
		wd: int, p0: int, p1: int, is_event: bool) -> void:
	var title := str(d.get("title", "")).strip_edges()
	var teacher := str(d.get("teacher", "")).strip_edges()
	var location := str(d.get("location", "")).strip_edges()
	var tint: Color = EVENT_TINT if is_event else COURSE_TINTS[absi(hash(title)) % COURSE_TINTS.size()]
	var block_h := float(p1 - p0 + 1) * TABLE_ROW_H - 3
	# 用 Panel（不是 PanelContainer）：块要精确控制文字位置，容器会把 Label 压回最小高度。
	var panel := Panel.new()
	panel.position = Vector2(TABLE_TIME_W + float(wd) * col_w + 2,
		TABLE_HEAD_H + float(p0 - 1) * TABLE_ROW_H + 1)
	panel.size = Vector2(col_w - 4, block_h)
	panel.mouse_filter = Control.MOUSE_FILTER_PASS
	var sb := StyleBoxFlat.new()
	sb.bg_color = tint
	sb.border_color = Color(GufengTheme.ACCENT, 0.8) if is_event else Color(tint.r, tint.g, tint.b, 0.85)
	sb.set_border_width_all(1)
	if is_event:
		sb.border_width_left = 3
	sb.set_corner_radius_all(4)
	panel.add_theme_stylebox_override("panel", sb)
	var lbl := Label.new()
	lbl.position = Vector2(3, 1)
	lbl.size = Vector2(col_w - 10, block_h - 4)
	var text := title
	if is_event:
		# 约定块：窄列优先保留标题；宽列再带一行分级。
		if col_w >= 52.0:
			var grade := _grade_text(d)
			if grade != "":
				text += "\n" + grade
	elif col_w >= 52.0:
		# 窄列不塞教师 / 地点，给课程名留出可读空间；悬停卡仍显示完整信息。
		var sub: Array[String] = []
		if teacher != "":
			sub.append(teacher)
		if location != "":
			sub.append(location)
		if not sub.is_empty():
			text += "\n" + " ".join(sub)
	lbl.text = text
	lbl.autowrap_mode = TextServer.AUTOWRAP_ARBITRARY
	lbl.clip_text = true
	lbl.max_lines_visible = 4
	lbl.add_theme_font_size_override("font_size", 7 if is_event and col_w < 44.0 else 8)
	lbl.add_theme_color_override("font_color", GufengTheme.INK)
	# 悬停只保留一个自绘浮层：**不再设置 tooltip_text**，避免 Godot 默认黑底白字提示
	# 和这张卡同时出现。
	panel.set_meta("sched_item", d)
	lbl.mouse_filter = Control.MOUSE_FILTER_IGNORE
	panel.add_child(lbl)
	# 鼠标停留 → 浮层展开完整信息（格子放不下的长标题 / 教师 / 地点 / 备注）。
	panel.mouse_entered.connect(func(): _show_hover_card(panel, d))
	panel.mouse_exited.connect(_hide_hover_card)
	grid.add_child(panel)

func _build_timetable(courses: Array, timed: Array = []) -> void:
	# 表格宽度跟着视口走：7 天优先全部可见；只有极窄窗口才用内部横向滚动。
	var table_w := clampf(size.x - MARGIN * 2.0 - 22.0, 280.0, FILES_W - 44.0)
	var col_w := (table_w - TABLE_TIME_W) / 7.0
	var table_h := TABLE_HEAD_H + TABLE_ROW_H * TABLE_PERIODS
	# —— 周标题：从课程日期推本周一 / 周日 ——
	var dates: Array[String] = []
	for it in courses:
		var ds := str(it.get("date", "")).strip_edges()
		if ds.length() >= 10:
			dates.append(ds)
	var note := Label.new()
	note.text = "本月课表"
	var today_col := -1
	if not dates.is_empty():
		dates.sort()
		var week_start := dates[0]
		var end_txt := ""
		var mon_unix := 0
		var unix := int(Time.get_unix_time_from_datetime_string(week_start + "T00:00:00"))
		if unix > 0:
			var d0 := Time.get_datetime_dict_from_unix_time(unix)
			mon_unix = unix - ((int(d0.get("weekday", 0)) + 6) % 7) * 86400
			var ed := Time.get_datetime_dict_from_unix_time(mon_unix + 6 * 86400)
			end_txt = "%02d-%02d" % [int(ed.get("month", 0)), int(ed.get("day", 0))]
		note.text = "本周课表 · %s ~ %s" % [week_start.substr(5), end_txt]
		# 只有扫描的正是本周时才高亮今天这一列（旧周课表不冒充今天）。
		var today_unix := int(Time.get_unix_time_from_datetime_string(
			Time.get_date_string_from_system() + "T00:00:00"))
		if mon_unix > 0 and today_unix >= mon_unix and today_unix <= mon_unix + 6 * 86400:
			today_col = (int(Time.get_datetime_dict_from_unix_time(today_unix).get("weekday", 0)) + 6) % 7
	note.add_theme_font_size_override("font_size", 10)
	note.add_theme_color_override("font_color", GufengTheme.INK)
	_schedule_rows.add_child(note)
	# —— 网格底图 ——
	# 表格宽度跟着视口走：7 天优先全部可见；只有极窄窗口才用内部横向滚动。
	var holder := Control.new()
	holder.custom_minimum_size = Vector2(table_w, table_h)
	holder.mouse_filter = Control.MOUSE_FILTER_PASS
	var grid := GridLayer.new()
	grid.host = self
	grid.position = Vector2.ZERO
	grid.size = Vector2(table_w, table_h)
	grid.custom_minimum_size = Vector2(table_w, table_h)
	grid.mouse_filter = Control.MOUSE_FILTER_PASS
	holder.add_child(grid)
	var hscroll := ScrollContainer.new()
	hscroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_AUTO
	hscroll.vertical_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	hscroll.custom_minimum_size = Vector2(0, table_h + 14.0)
	hscroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hscroll.add_child(holder)
	_timetable = {"col_w": col_w, "width": table_w, "height": table_h, "today_col": today_col}
	_schedule_table = grid
	_schedule_rows.add_child(hscroll)
	# —— 表头：星期 + 日期 ——
	var col_dates := {}
	for it in courses:
		var wd := int(it.get("weekday", -1))
		var ds := str(it.get("date", "")).strip_edges()
		if wd >= 0 and wd < 7 and ds.length() >= 10 and not col_dates.has(wd):
			col_dates[wd] = ds.substr(5)
	for wd in range(7):
		var head := Label.new()
		var suffix := ""
		if col_dates.has(wd):
			suffix = "\n" + str(col_dates[wd])
		head.text = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][wd] + suffix
		head.position = Vector2(TABLE_TIME_W + float(wd) * col_w, 1)
		head.size = Vector2(col_w, TABLE_HEAD_H - 2)
		head.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		head.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
		head.add_theme_font_size_override("font_size", 9 if wd == today_col else 8)
		head.add_theme_color_override("font_color",
			GufengTheme.ACCENT if wd == today_col else GufengTheme.INK_SOFT)
		head.mouse_filter = Control.MOUSE_FILTER_IGNORE
		grid.add_child(head)
	# —— 左侧节次 / 时间 ——
	var times := _period_time_map(courses)
	for p in range(1, TABLE_PERIODS + 1):
		var lbl := Label.new()
		var t := str(times.get(p, ""))
		lbl.text = str(p) + ("\n" + t if t != "" else "")
		lbl.position = Vector2(0, TABLE_HEAD_H + float(p - 1) * TABLE_ROW_H)
		lbl.size = Vector2(TABLE_TIME_W - 2, TABLE_ROW_H)
		lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
		lbl.add_theme_font_size_override("font_size", 7)
		lbl.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.9))
		lbl.mouse_filter = Control.MOUSE_FILTER_IGNORE
		grid.add_child(lbl)
	# —— 课程块（跨节）——
	var occupied := {}
	for it in courses:
		var d: Dictionary = it
		var wd := clampi(int(d.get("weekday", 0)), 0, 6)
		var p0 := clampi(int(d.get("period_start", 1)), 1, TABLE_PERIODS)
		var p1 := clampi(int(d.get("period_end", p0)), p0, TABLE_PERIODS)
		for p in range(p0, p1 + 1):
			occupied["%d|%d" % [wd, p]] = true
		_add_timetable_block(grid, col_w, d, wd, p0, p1, false)
	# —— 约定 / 定时事件：按开始时刻落到对应节次行，不再只出现在列表里 ——
	# 同一格有多个约定时合并成一个「N 项约定」块（悬停卡列出全部）；
	# 若该格已被课程占用，就不画事件块，避免盖住课程（上方列表仍完整显示）。
	var groups := {}
	var group_order := []
	for it in timed:
		var ev: Dictionary = it
		var wd := clampi(int(ev.get("_event_wd", 0)), 0, 6)
		var p0 := clampi(int(ev.get("_event_p0", 1)), 1, TABLE_PERIODS)
		var p1 := clampi(int(ev.get("_event_p1", p0)), p0, TABLE_PERIODS)
		var blocked := false
		for p in range(p0, p1 + 1):
			if occupied.has("%d|%d" % [wd, p]):
				blocked = true
				break
		if blocked:
			continue
		var key := "%d|%d" % [wd, p0]
		if not groups.has(key):
			groups[key] = []
			group_order.append(key)
		groups[key].append(ev)
	for key in group_order:
		var group: Array = groups[key]
		var base: Dictionary = group[0]
		var wd := clampi(int(base.get("_event_wd", 0)), 0, 6)
		var p0 := clampi(int(base.get("_event_p0", 1)), 1, TABLE_PERIODS)
		var p1 := clampi(int(base.get("_event_p1", p0)), p0, TABLE_PERIODS)
		for ev in group:
			p1 = maxi(p1, clampi(int(ev.get("_event_p1", p0)), p0, TABLE_PERIODS))
		if group.size() == 1:
			_add_timetable_block(grid, col_w, base, wd, p0, p1, true)
			continue
		var merged: Dictionary = base.duplicate()
		merged["title"] = "%d 项约定" % group.size()
		merged["grade"] = "committed"
		merged["grade_label"] = "已约定"
		var whens: Array[String] = []
		var seen_when := {}
		var lines: Array[String] = []
		for ev in group:
			var title := str(ev.get("title", "")).strip_edges()
			var when := str(ev.get("when_text", "")).strip_edges()
			var loc := str(ev.get("location", "")).strip_edges()
			if when != "" and not seen_when.has(when):
				seen_when[when] = true
				whens.append(when)
			var line := title
			if when != "":
				line += "（%s）" % when
			if loc != "":
				line += " " + loc
			lines.append(line)
		merged["when_text"] = "；".join(whens)
		merged["notes"] = "\n".join(lines)
		_add_timetable_block(grid, col_w, merged, wd, p0, p1, true)

func _build_agenda(items: Array) -> void:
	var wrap := VBoxContainer.new()
	wrap.add_theme_constant_override("separation", 2)
	wrap.custom_minimum_size = Vector2(0, 0)
	wrap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_schedule_agenda = wrap
	_schedule_rows.add_child(wrap)
	wrap.add_child(_files_hint("日程 / 约定"))
	for it in items:
		var d: Dictionary = it
		var when := str(d.get("when_text", "")).strip_edges()
		var title := str(d.get("title", "")).strip_edges()
		var status := str(d.get("status", "planned")).strip_edges()
		var tag := _priority_tag(d)
		var grade_tag := _grade_tag(d)
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 3)
		var lbl := Label.new()
		lbl.text = "· %s  " % when
		if status == "planned" and grade_tag != "":
			lbl.text += "[%s] " % grade_tag
		lbl.text += "%s%s" % [tag, title]
		if status == "done":
			lbl.text += "（已完成）"
		elif status == "cancelled":
			lbl.text += "（已取消）"
		lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		lbl.custom_minimum_size = Vector2(96, 0)
		lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		lbl.add_theme_font_size_override("font_size", 10)
		lbl.add_theme_color_override("font_color",
			GufengTheme.INK_SOFT if status != "planned" else GufengTheme.INK)
		# 悬停只显示一张自绘卡；不再用 Godot 默认的黑底 tooltip（会和卡片重复）。
		lbl.set_meta("sched_item", d)
		lbl.mouse_entered.connect(func(): _show_hover_card(lbl, d))
		lbl.mouse_exited.connect(_hide_hover_card)
		row.add_child(lbl)
		if status == "planned":
			# 执行反馈：完成 → 后端按「现在 - 开始时间」估实际耗时；推迟 → 明天优先再排。
			var bd := _compact_button("完成")
			bd.pressed.connect(func(): schedule_feedback_requested.emit(
				int(d.get("id", 0)), "done", 0))
			row.add_child(bd)
			var bp := _compact_button("推迟")
			bp.pressed.connect(func(): schedule_feedback_requested.emit(
				int(d.get("id", 0)), "deferred", 0))
			row.add_child(bp)
		wrap.add_child(row)

func _priority_tag(d: Dictionary) -> String:
	var pri := int(d.get("priority", 50))
	if pri >= 85:
		return "[紧急] "
	if pri >= 70:
		return "[高] "
	return ""

func _grade_tag(d: Dictionary) -> String:
	"""日程分级短标签（后端已分级；离线 / 测试数据时按字段兜底推断）。"""
	var grade := str(d.get("grade", "")).strip_edges()
	if grade == "":
		if str(d.get("kind", "")) == "course" or str(d.get("period", "")) != "":
			grade = "fixed"
		elif int(d.get("start_ts", 0)) > 0 and not bool(d.get("auto_planned", false)):
			grade = "committed"
		elif float(d.get("deadline_ts", 0.0)) > 0.0:
			grade = "deadline"
		elif bool(d.get("auto_planned", false)):
			grade = "tentative"
		else:
			grade = "flexible"
	match grade:
		"fixed":
			return "固定"
		"committed":
			return "约定"
		"locked":
			return "锁定"
		"deadline":
			return "截止"
		"tentative":
			return "可调"
		"flexible":
			return "待排"
		"closed":
			return "结束"
	return ""

func _grade_text(d: Dictionary) -> String:
	var label := str(d.get("grade_label", "")).strip_edges()
	if label != "":
		return label
	match _grade_tag(d):
		"固定":
			return "固定课程"
		"约定":
			return "已约定（不会自动移动）"
		"锁定":
			return "锁定中（本次不重排）"
		"截止":
			return "截止驱动（时间可调）"
		"可调":
			return "可调整（优先保持原位）"
		"待排":
			return "待安排（按优先级放置）"
		"结束":
			return "已结束（不再自动重排）"
	return ""

func _reason_text(d: Dictionary) -> String:
	"""悬停卡里的排班理由：后端 assignment 带 reason 时直接用，否则按字段兜底推断。"""
	var custom := str(d.get("reason", "")).strip_edges()
	if custom != "":
		return custom
	var tag := _grade_tag(d)
	var text := ""
	match tag:
		"固定":
			text = "固定课程，不自动移动"
		"约定":
			text = "已约定时间，不自动移动"
		"锁定":
			text = "在锁定窗内，本次不重排"
		"截止":
			text = "按截止时间优先，时间可调"
		"可调":
			text = "上次自动安排，原时间可行时优先保位"
		"待排":
			text = "待安排，按优先级密度放入最早空档"
		"结束":
			text = "已结束，不再自动重排"
	if bool(d.get("auto_planned", false)):
		var defer := int(d.get("defer_count", 0))
		if defer >= 2:
			text = "连续推迟 %d 次，优先放到较大空档" % defer
		elif defer == 1:
			text = "上次推迟过，优先排到明天"
	if str(d.get("when_text", "")).contains("已超截止"):
		text += "；已晚于截止"
	return text

func draw_schedule_table(c: Control, s: Vector2) -> void:
	if _timetable.is_empty() or s.x < 4.0 or s.y < 4.0:
		return
	var col_w := float(_timetable.get("col_w", 0.0))
	if col_w <= 0.0:
		return
	# 表头浅色底 + 网格线（细、低对比，课程块在它上面跨行）。
	c.draw_rect(Rect2(Vector2.ZERO, Vector2(s.x, TABLE_HEAD_H)), Color(GufengTheme.ACCENT, 0.10))
	var today_col := int(_timetable.get("today_col", -1))
	if today_col >= 0 and today_col < 7:
		var tx := TABLE_TIME_W + float(today_col) * col_w
		c.draw_rect(Rect2(tx, 0, col_w, TABLE_HEAD_H), Color(GufengTheme.ACCENT, 0.20))
		c.draw_rect(Rect2(tx, TABLE_HEAD_H, col_w, s.y - TABLE_HEAD_H), Color(GufengTheme.ACCENT, 0.035))
	var line := Color(GufengTheme.INK, 0.10)
	for i in range(TABLE_PERIODS + 1):
		var y := TABLE_HEAD_H + float(i) * TABLE_ROW_H
		c.draw_line(Vector2(0, y), Vector2(s.x, y), line, 1.0)
	for i in range(9):
		var x := 0.0 if i == 0 else TABLE_TIME_W + float(i - 1) * col_w
		if i == 8:
			x = s.x - 1.0
		c.draw_line(Vector2(x, 0), Vector2(x, s.y), line, 1.0)
	c.draw_line(Vector2(TABLE_TIME_W, TABLE_HEAD_H), Vector2(TABLE_TIME_W, s.y),
		Color(GufengTheme.INK, 0.16), 1.0)

func set_files_status(text: String) -> void:
	if _files_status != null:
		_files_status.text = str(text)

func set_files_busy(on: bool) -> void:
	for b in [_files_scan, _files_refresh, _files_auto_plan, _files_archive,
			_files_cleanup, _files_review]:
		if b != null:
			b.disabled = on
	if on:
		set_files_status("正在处理，请稍等…")

func _files_hint(text: String) -> Label:
	var lbl := Label.new()
	lbl.text = text
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(140, 0)
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	lbl.add_theme_font_size_override("font_size", 10)
	lbl.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.9))
	return lbl

func _compact_button(text: String) -> Button:
	"""小号文字按钮：紧凑边距，窄面板的日程行里也放得下。"""
	var b := Button.new()
	b.text = text
	b.custom_minimum_size = Vector2(34, 20)
	b.add_theme_font_size_override("font_size", 10)
	var normal := StyleBoxFlat.new()
	normal.bg_color = Color(GufengTheme.PANEL_SOFT, 0.92)
	normal.border_color = GufengTheme.BORDER_SOFT
	normal.set_border_width_all(1)
	normal.set_corner_radius_all(6)
	normal.content_margin_left = 4
	normal.content_margin_right = 4
	normal.content_margin_top = 2
	normal.content_margin_bottom = 2
	var hover := normal.duplicate()
	hover.bg_color = GufengTheme.HOVER_BG
	hover.border_color = Color(GufengTheme.ACCENT, 0.75)
	var pressed := normal.duplicate()
	pressed.bg_color = GufengTheme.ACCENT
	pressed.border_color = GufengTheme.ACCENT
	b.add_theme_stylebox_override("normal", normal)
	b.add_theme_stylebox_override("hover", hover)
	b.add_theme_stylebox_override("pressed", pressed)
	b.add_theme_color_override("font_color", GufengTheme.INK)
	b.add_theme_color_override("font_hover_color", GufengTheme.ACCENT)
	b.add_theme_color_override("font_pressed_color", GufengTheme.ON_ACCENT)
	return b

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
	if _reminder != null and _reminder.visible and Time.get_ticks_msec() >= _reminder_until:
		_reminder.visible = false
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
