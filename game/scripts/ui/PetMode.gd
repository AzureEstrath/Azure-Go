class_name PetMode
extends Control
## 2D 桌面宠物（启动形态）。
##
## 只负责画与交互：贴图、气泡、右键菜单、拖动、左键反馈、情绪颜色。
## 与后端 / 对局的一切接线都在 `Main.gd`（桌宠只是 `Main` 的一种窗口形态）：
##   · `go_requested`      —— 打开正式围棋助手（窗口切回 1280×800）
##   · `peek_requested`    —— 窥屏搭话（后台脚本 + /api/peer）
##   · `talk_requested`    —— 普通闲置搭话（/api/idle）
##   · `status_requested`  —— 显示当前情绪数值 + 长期记忆体量
##   · `mute_toggled`      —— 桌宠语音开关
##   · `quit_requested`    —— 真正退出程序（回桌宠 ≠ 退出）
##   · `interacted`        —— 左键点了一下（摸头；Main 负责 /api/interaction + 语气词）
##
## 主题全部来自 `GufengTheme`，与正式界面同一套字体 / 纸色 / 青蓝强调色。

signal go_requested
signal peek_requested
signal talk_requested
signal status_requested
signal mute_toggled(muted: bool)
signal quit_requested
signal interacted

## 桌宠窗口尺寸（Main 切换窗口时也读这里，保持唯一来源）
const WINDOW_SIZE := Vector2i(360, 540)
const PET_BOX := Rect2(30, 196, 300, 300)
const BUBBLE_W := 336.0
const BUBBLE_TOP := 12.0
const BUBBLE_MAX_H := 176.0
const MENU_MARGIN := 8.0
const DRAG_CLICK_MAX := 4.0
const BLUSH_SEC := 1.7

const LOW_NAMES := ["落寞", "烦闷", "烦躁", "失落", "难过", "委屈", "忧心", "不安", "惊慌"]
const HIGH_NAMES := ["惬意", "愉悦", "兴奋", "开心", "欢喜", "欢腾", "期待", "跃跃欲试", "惊喜", "振奋"]
const CURIOUS_NAMES := ["跳脱", "好奇", "兴致", "激动"]

const REACTIONS := {
	"high": [
		"嗯哼，被你摸到了。",
		"今天心情很好，要不要下一盘？",
		"再摸一下也可以哦。",
		"我在呢，笑得有点藏不住了。",
	],
	"curious": [
		"你在做什么呀？我有点好奇。",
		"右键点我，我可以看看你在忙什么。",
		"刚才那一下，是在夸我吗？",
	],
	"low": [
		"唔…让我靠一会儿。",
		"心情有点低，陪我坐坐吧。",
		"摸摸头会好一点。",
	],
	"calm": [
		"在呢。",
		"想下棋的话，右键叫我。",
		"我就在这里，不着急。",
		"桌面上的风，今天很轻。",
	],
}

## 内层特效层：画在贴图之上（脸红 / 爱心 / 涟漪），父节点只负责把数据交过来
class FxLayer extends Control:
	var host = null

	func _draw() -> void:
		if host != null:
			host.draw_fx(self)

var _pet_anchor: Control
var _pet_tex: TextureRect
var _bubble: PanelContainer
var _bubble_who: Label
var _bubble_label: Label
var _bubble_text := ""
var _bubble_until := 0
var _thinking := false
var _speaking := false
var _text_tween: Tween = null
var _bounce_tween: Tween = null

var _menu: PanelContainer
var _mute_btn: Button
var _mood_chip: Label
var _online_dot: Label
var _name_plate: PanelContainer
var _fx: FxLayer

var _mood_name := "平静"
var _mood_v := 0.5
var _mood_c := 0.5
var _mood_a := 0.5
var _online := false
var _muted := false

var _dragging := false
var _drag_moved := false
var _press_mouse := Vector2i.ZERO
var _press_win := Vector2i.ZERO
var _drag_tilt := 0.0
var _last_click_ms := -10000

var _blush := 0.0
var _blush_until := 0
var _t := 0.0
var _particles: Array[Dictionary] = []
var _ripples: Array[Dictionary] = []

func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	if theme == null:
		theme = GufengTheme.build_theme(GufengTheme.font())
	_build()
	set_process(true)

func _build() -> void:
	# ① 气泡：纸签样式，跟在贴图上方；初始不显示
	_bubble = PanelContainer.new()
	_bubble.name = "PetBubble"
	_bubble.anchor_left = 0.5
	_bubble.anchor_right = 0.5
	_bubble.offset_left = -BUBBLE_W * 0.5
	_bubble.offset_right = BUBBLE_W * 0.5
	_bubble.offset_top = BUBBLE_TOP
	_bubble.offset_bottom = BUBBLE_TOP + 92.0
	_bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
	_bubble.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_bubble.visible = false
	add_child(_bubble)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 3)
	_bubble.add_child(vb)
	_bubble_who = Label.new()
	_bubble_who.text = "Azure"
	_bubble_who.add_theme_font_size_override("font_size", 10)
	_bubble_who.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	_bubble_who.mouse_filter = Control.MOUSE_FILTER_IGNORE
	vb.add_child(_bubble_who)
	_bubble_label = Label.new()
	_bubble_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bubble_label.max_lines_visible = 7
	_bubble_label.add_theme_font_size_override("font_size", 14)
	_bubble_label.add_theme_color_override("font_color", GufengTheme.INK)
	_bubble_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	vb.add_child(_bubble_label)

	# ② 贴图：透明 PNG，按底边中心做呼吸 / 点击弹跳
	_pet_anchor = Control.new()
	_pet_anchor.name = "PetBody"
	_pet_anchor.position = PET_BOX.position
	_pet_anchor.size = PET_BOX.size
	_pet_anchor.pivot_offset = Vector2(PET_BOX.size.x * 0.5, PET_BOX.size.y)
	_pet_anchor.mouse_filter = Control.MOUSE_FILTER_STOP
	_pet_anchor.mouse_default_cursor_shape = Control.CURSOR_DRAG
	_pet_anchor.gui_input.connect(_on_pet_input)
	add_child(_pet_anchor)

	_pet_tex = TextureRect.new()
	_pet_tex.texture = load("res://assets/azure_pet.png")
	_pet_tex.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_pet_tex.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_pet_tex.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_pet_tex.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_pet_anchor.add_child(_pet_tex)

	# ③ 名牌：在线点 + 当前情绪名（数值在提示里；右键菜单可看完整数值）
	_name_plate = PanelContainer.new()
	_name_plate.name = "PetNamePlate"
	_name_plate.anchor_left = 0.5
	_name_plate.anchor_right = 0.5
	_name_plate.offset_left = -110
	_name_plate.offset_right = 110
	_name_plate.offset_top = 500
	_name_plate.offset_bottom = 530
	_name_plate.add_theme_stylebox_override("panel", GufengTheme.slip(GufengTheme.PANEL_SOFT, 6))
	_name_plate.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_name_plate)

	var nb := HBoxContainer.new()
	nb.alignment = BoxContainer.ALIGNMENT_CENTER
	nb.add_theme_constant_override("separation", 7)
	nb.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_name_plate.add_child(nb)
	_online_dot = Label.new()
	_online_dot.text = "●"
	_online_dot.add_theme_font_size_override("font_size", 10)
	_online_dot.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	_online_dot.mouse_filter = Control.MOUSE_FILTER_IGNORE
	nb.add_child(_online_dot)
	_mood_chip = Label.new()
	_mood_chip.text = "Azure · 平静"
	_mood_chip.add_theme_font_size_override("font_size", 12)
	_mood_chip.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_mood_chip.mouse_filter = Control.MOUSE_FILTER_IGNORE
	nb.add_child(_mood_chip)

	# ④ 特效层：贴图上面（脸红 / 爱心 / 涟漪）
	_fx = FxLayer.new()
	_fx.host = self
	_fx.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_fx.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_fx)

	# ⑤ 右键菜单：最后加入，保证压在贴图与气泡之上
	_build_menu()

func _build_menu() -> void:
	_menu = PanelContainer.new()
	_menu.name = "PetMenu"
	_menu.add_theme_stylebox_override("panel", GufengTheme.panel(10))
	_menu.mouse_filter = Control.MOUSE_FILTER_STOP
	_menu.visible = false
	add_child(_menu)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 6)
	_menu.add_child(vb)

	var head := HBoxContainer.new()
	head.add_theme_constant_override("separation", 8)
	vb.add_child(head)
	var seal := PanelContainer.new()
	seal.add_theme_stylebox_override("panel", GufengTheme.seal(4))
	seal.custom_minimum_size = Vector2(28, 28)
	seal.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	head.add_child(seal)
	var seal_lbl := Label.new()
	seal_lbl.text = "亚"
	seal_lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	seal_lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	seal_lbl.add_theme_font_size_override("font_size", 16)
	seal_lbl.add_theme_color_override("font_color", GufengTheme.ON_ACCENT)
	seal.add_child(seal_lbl)
	var title := Label.new()
	title.text = "Azure · 桌宠"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	title.add_theme_font_size_override("font_size", 14)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	head.add_child(title)
	var close := Button.new()
	close.text = "×"
	close.custom_minimum_size = Vector2(26, 26)
	close.tooltip_text = "收起菜单"
	GufengTheme.apply_bare_button(close)
	close.pressed.connect(close_menu)
	head.add_child(close)

	_rule(vb)

	var go := _menu_button(vb, "围棋陪练", true)
	go.tooltip_text = "打开正式围棋助手；退出后回到桌宠"
	go.pressed.connect(func():
		close_menu()
		go_requested.emit())

	var peek := _menu_button(vb, "看看我在干嘛")
	peek.tooltip_text = "截一张前台画面，结合 OCR / VLM 主动搭话（隐私内容会被过滤）"
	peek.pressed.connect(func():
		close_menu()
		peek_requested.emit())

	var talk := _menu_button(vb, "陪我聊两句")
	talk.tooltip_text = "让 Azure 结合当前情绪与记忆主动说一句"
	talk.pressed.connect(func():
		close_menu()
		talk_requested.emit())

	var status := _menu_button(vb, "心情 · 记忆")
	status.tooltip_text = "查看当前三维情绪数值与长期记忆体量"
	status.pressed.connect(func():
		close_menu()
		status_requested.emit())

	_mute_btn = _menu_button(vb, "静音", false)
	_mute_btn.tooltip_text = "只关桌宠语音，不关正式对局里的朗读"
	_mute_btn.pressed.connect(func():
		_muted = not _muted
		_refresh_mute_text()
		mute_toggled.emit(_muted))

	_rule(vb)

	var quit_btn := _menu_button(vb, "退出桌宠", false)
	quit_btn.tooltip_text = "真正退出程序（菜单里的「围棋陪练」才是回对局）"
	quit_btn.pressed.connect(func():
		close_menu()
		quit_requested.emit())

	var hint := Label.new()
	hint.text = "左键摸摸 · 拖动换位置 · 右键开菜单"
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.custom_minimum_size = Vector2(188, 0)     # 不给宽度时最小宽=1px，会一字一行把菜单撑到 600px
	hint.add_theme_font_size_override("font_size", 10)
	hint.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.8))
	vb.add_child(hint)
	_refresh_mute_text()

func _menu_button(parent: Node, text: String, primary := false) -> Button:
	var b := Button.new()
	b.text = text
	b.custom_minimum_size = Vector2(188, 34)
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

# ================= 对外状态 =================

## 显示一句气泡。`secs<=0` 时按字数自动定时；正在朗读时由 Main 调 `set_speaking` 延长。
func show_bubble(text: String, secs := 0.0, who := "Azure") -> void:
	var clean := str(text).strip_edges()
	if clean == "":
		return
	_bubble_text = clean
	_bubble_who.text = who
	_bubble_label.text = clean
	_bubble_label.visible_ratio = 0.0
	_thinking = false
	_bubble.visible = true
	_bubble_until = Time.get_ticks_msec() + int((secs if secs > 0.0 else bubble_ttl(clean)) * 1000.0)
	_relayout_bubble()
	if _text_tween != null and _text_tween.is_valid():
		_text_tween.kill()
	var dur := clampf(float(clean.length()) * 0.022, 0.12, 0.8)
	_text_tween = create_tween()
	_text_tween.tween_property(_bubble_label, "visible_ratio", 1.0, dur).set_trans(Tween.TRANS_SINE)
	queue_redraw()

## 思考占位：不自动消失（等真实台词 / busy=false 收口）
func show_thinking(text := "Azure 思考中…") -> void:
	if _bubble.visible and _thinking:
		return
	show_bubble(text, 60.0)
	_thinking = true

func clear_if_thinking() -> void:
	if not _thinking:
		return
	_thinking = false
	_bubble.visible = false
	queue_redraw()

## 朗读开始 / 结束：结束之后再留 2.4 秒，避免话音刚落气泡就消失
func set_speaking(on: bool) -> void:
	_speaking = on
	if _bubble.visible and on:
		_bubble_until = maxi(_bubble_until, Time.get_ticks_msec() + 1500)
	elif _bubble.visible and not _thinking:
		_bubble_until = Time.get_ticks_msec() + 2400

func set_mood(p: Dictionary) -> void:
	_mood_name = str(p.get("name", _mood_name))
	_mood_v = clampf(float(p.get("v", _mood_v)), 0.0, 1.0)
	_mood_c = clampf(float(p.get("c", _mood_c)), 0.0, 1.0)
	_mood_a = clampf(float(p.get("a", _mood_a)), 0.0, 1.0)
	_refresh_mood_text()
	queue_redraw()

func set_online(on: bool, note := "") -> void:
	_online = on
	if _online_dot != null:
		_online_dot.add_theme_color_override("font_color",
			GufengTheme.DAI if on else GufengTheme.CINNABAR)
	if _name_plate != null:
		_name_plate.tooltip_text = note if note != "" else ("后端已连接" if on else "后端未连接")
	_refresh_mood_text()

func set_muted(m: bool) -> void:
	_muted = m
	_refresh_mute_text()

func is_muted() -> bool:
	return _muted

func is_menu_open() -> bool:
	return _menu != null and _menu.visible

func open_menu(pos: Vector2) -> void:
	if _menu == null:
		return
	if _menu.visible:
		close_menu()
		return
	_menu.visible = true
	_menu.size = _menu.get_combined_minimum_size()
	_menu.position = clamp_menu_position(pos, _menu.size, size)
	_menu.queue_redraw()

func close_menu() -> void:
	if _menu != null:
		_menu.visible = false

func play_pat_effect() -> void:
	var now := Time.get_ticks_msec()
	var double := now - _last_click_ms < 360
	_last_click_ms = now
	_blush = 1.0
	_blush_until = now + int(BLUSH_SEC * 1000.0)
	_spawn_hearts(6 if double else 4)
	if double:
		_spawn_ripple(PET_BOX.position + Vector2(PET_BOX.size.x * 0.5, PET_BOX.size.y * 0.86), 6.0)
	if _bounce_tween != null and _bounce_tween.is_valid():
		_bounce_tween.kill()
	_pet_anchor.scale = Vector2.ONE
	_bounce_tween = create_tween()
	_bounce_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_bounce_tween.tween_property(_pet_anchor, "scale", Vector2(1.06, 0.94), 0.13)
	_bounce_tween.tween_property(_pet_anchor, "scale", Vector2(0.98, 1.02), 0.12)
	_bounce_tween.tween_property(_pet_anchor, "scale", Vector2.ONE, 0.20)

## 后端台词里的 {行为} 标签 → 桌宠的小动作（最多两个，与正式界面同口径）
func play_actions(acts: Array) -> void:
	var played := 0
	for a in acts:
		match str(a):
			"blush":
				_blush = 1.0
				_blush_until = Time.get_ticks_msec() + int(BLUSH_SEC * 1000.0)
			"smile", "laugh":
				_spawn_hearts(3)
			"proud":
				_spawn_sparkles(4)
			"surprise":
				_spawn_ripple(PET_BOX.position + Vector2(PET_BOX.size.x * 0.5, PET_BOX.size.y * 0.5), 4.0)
			"worried", "sad":
				_spawn_sparkles(2, Color(GufengTheme.CINNABAR, 0.8))
			_:
				continue
		played += 1
		if played >= 2:
			break

# ================= 拖动 / 点击 =================

func _on_pet_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_RIGHT and mb.pressed:
			open_menu(_pet_anchor.position + mb.position)   # 事件坐标在贴图控件内，换成桌宠根坐标
			accept_event()
			return
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_press_mouse = DisplayServer.mouse_get_position()
				_press_win = DisplayServer.window_get_position()
				_drag_moved = false
				_dragging = true
				accept_event()
			elif _dragging:
				_dragging = false
				if _drag_moved:
					save_window_position()
				else:
					interacted.emit()
				_drag_tilt = 0.0
				accept_event()
	elif event is InputEventMouseMotion and _dragging:
		var mp := DisplayServer.mouse_get_position()
		var d := mp - _press_mouse
		if Vector2(d.x, d.y).length() > DRAG_CLICK_MAX:
			_drag_moved = true
		DisplayServer.window_set_position(_press_win + d)
		_drag_tilt = clampf(float(d.x) / 220.0, -0.18, 0.18)
		accept_event()

func _input(event: InputEvent) -> void:
	if not is_visible_in_tree() or _menu == null or not _menu.visible:
		return
	if event is InputEventMouseButton and (event as InputEventMouseButton).pressed:
		var pos: Vector2 = (event as InputEventMouseButton).position
		if not _menu.get_global_rect().has_point(pos):
			close_menu()
			get_viewport().set_input_as_handled()

func restore_window_position() -> void:
	var c := ConfigFile.new()
	var ok := c.load("user://pet_window.cfg") == OK
	var scr := DisplayServer.screen_get_usable_rect(DisplayServer.window_get_current_screen())
	var pos: Vector2i
	if ok:
		pos = Vector2i(int(c.get_value("window", "x", 0)), int(c.get_value("window", "y", 0)))
	else:
		pos = scr.position + scr.size - WINDOW_SIZE - Vector2i(24, 42)
	var max_x := scr.position.x + maxi(0, scr.size.x - WINDOW_SIZE.x)
	var max_y := scr.position.y + maxi(0, scr.size.y - WINDOW_SIZE.y)
	pos.x = clampi(pos.x, scr.position.x, max_x)
	pos.y = clampi(pos.y, scr.position.y, max_y)
	DisplayServer.window_set_position(pos)

func save_window_position() -> void:
	if DisplayServer.get_name() == "headless":
		return
	var pos := DisplayServer.window_get_position()
	var c := ConfigFile.new()
	c.load("user://pet_window.cfg")                 # 保留其他键（现在没有，但别覆盖掉未来字段）
	c.set_value("window", "x", pos.x)
	c.set_value("window", "y", pos.y)
	c.save("user://pet_window.cfg")

# ================= 绘制 =================

func _process(delta: float) -> void:
	_t += delta
	var bob := sin(_t * 1.6) * 2.0
	var sway := sin(_t * 0.9) * 0.006
	if _speaking:
		bob += sin(_t * 7.0) * 1.1
		sway += sin(_t * 5.0) * 0.004
	if _dragging:
		bob = 0.0
	_pet_anchor.position = PET_BOX.position + Vector2(0.0, bob)
	_drag_tilt = lerpf(_drag_tilt, 0.0, clampf(delta * 8.0, 0.0, 1.0))
	_pet_anchor.rotation = sway + _drag_tilt

	if _blush > 0.0 and Time.get_ticks_msec() >= _blush_until:
		_blush = 0.0
	_update_particles(delta)
	_fx.queue_redraw()
	queue_redraw()

	if _bubble.visible and not _thinking and not _speaking and not _menu.visible \
			and Time.get_ticks_msec() >= _bubble_until:
		_bubble.visible = false
		_bubble_text = ""
		queue_redraw()

func _update_particles(delta: float) -> void:
	var keep: Array[Dictionary] = []
	for p in _particles:
		p["age"] = float(p["age"]) + delta
		if float(p["age"]) < float(p["life"]):
			p["pos"] = (p["pos"] as Vector2) + (p["vel"] as Vector2) * delta
			p["vel"] = (p["vel"] as Vector2) + Vector2(0.0, 34.0 * delta)
			keep.append(p)
	_particles = keep
	var keep_r: Array[Dictionary] = []
	for r in _ripples:
		r["age"] = float(r["age"]) + delta
		if float(r["age"]) < float(r["life"]):
			keep_r.append(r)
	_ripples = keep_r

func _draw() -> void:
	# 脚下情绪光环：颜色随愉悦度 / 好奇度变化，呼吸幅度随唤醒度
	var col := mood_color(_mood_v, _mood_c, _mood_a)
	var center := PET_BOX.position + Vector2(PET_BOX.size.x * 0.5, PET_BOX.size.y * 0.87)
	var pulse := 1.0 + sin(_t * 2.2) * (0.02 + 0.04 * _mood_a)
	for i in 3:
		var r := PET_BOX.size.x * (0.30 + 0.10 * float(i)) * pulse
		draw_circle(center, r, Color(col.r, col.g, col.b, 0.055 - 0.012 * float(i)))

	# 气泡尾巴（画在子节点之前，所以只在气泡下方露出）
	if _bubble != null and _bubble.visible:
		var bx := size.x * 0.5
		var by := _bubble.position.y + _bubble.size.y - 1.0
		var fill := Color(GufengTheme.PANEL_SOFT.r, GufengTheme.PANEL_SOFT.g,
			GufengTheme.PANEL_SOFT.b, 0.97)
		draw_colored_polygon(PackedVector2Array([
			Vector2(bx - 8.0, by - 2.0),
			Vector2(bx + 8.0, by - 2.0),
			Vector2(bx, by + 9.0),
		]), fill)

func draw_fx(c: CanvasItem) -> void:
	# 脸红：两张脸颊（贴图里脸在偏左上，按比例落在脸上）
	if _blush > 0.01:
		var fc := PET_BOX.position
		var sz := PET_BOX.size
		var a := _blush * 0.42
		c.draw_circle(fc + Vector2(sz.x * 0.415, sz.y * 0.635), sz.x * 0.043,
			Color(0.94, 0.52, 0.58, a))
		c.draw_circle(fc + Vector2(sz.x * 0.665, sz.y * 0.635), sz.x * 0.043,
			Color(0.94, 0.52, 0.58, a))

	for p in _particles:
		var life := float(p["life"])
		var age := float(p["age"])
		var k := clampf(1.0 - age / maxf(life, 0.01), 0.0, 1.0)
		var pp: Vector2 = p["pos"]
		var pcol: Color = p["color"]
		pcol.a *= k
		if str(p["kind"]) == "heart":
			c.draw_colored_polygon(heart_points(pp, float(p["size"]) * (0.7 + 0.3 * k)), pcol)
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
		var rr := float(r["r"]) + k * 46.0
		var rp: Vector2 = r["pos"]
		var rcol: Color = r["color"]
		rcol.a *= (1.0 - k) * 0.55
		c.draw_arc(rp, rr, 0.0, TAU, 32, rcol, 2.0, true)

func heart_points(center: Vector2, s: float) -> PackedVector2Array:
	var pts := PackedVector2Array()
	var n := 20
	for i in n:
		var t := TAU * float(i) / float(n)
		var x := 16.0 * pow(sin(t), 3.0)
		var y := -(13.0 * cos(t) - 5.0 * cos(2.0 * t) - 2.0 * cos(3.0 * t) - cos(4.0 * t))
		pts.append(center + Vector2(x, y) * (s / 16.0))
	return pts

func _spawn_hearts(n: int) -> void:
	var sz := PET_BOX.size
	for i in n:
		var angle := randf_range(-2.6, -0.5)
		var speed := randf_range(26.0, 58.0)
		_particles.append({
			"kind": "heart",
			"pos": PET_BOX.position + Vector2(sz.x * randf_range(0.28, 0.72), sz.y * randf_range(0.34, 0.62)),
			"vel": Vector2(cos(angle), sin(angle)) * speed,
			"age": 0.0,
			"life": randf_range(0.9, 1.5),
			"size": randf_range(6.0, 11.0),
			"color": Color(GufengTheme.CINNABAR.r, GufengTheme.CINNABAR.g, GufengTheme.CINNABAR.b,
				randf_range(0.55, 0.9)),
		})
	while _particles.size() > 28:
		_particles.pop_front()

func _spawn_sparkles(n: int, color := Color(0, 0, 0, 0)) -> void:
	var col := color
	if col.a <= 0.0:
		col = Color(GufengTheme.DAI.r, GufengTheme.DAI.g, GufengTheme.DAI.b, 0.9)
	var sz := PET_BOX.size
	for i in n:
		_particles.append({
			"kind": "spark",
			"pos": PET_BOX.position + Vector2(sz.x * randf_range(0.2, 0.8), sz.y * randf_range(0.2, 0.7)),
			"vel": Vector2(randf_range(-22.0, 22.0), randf_range(-44.0, -20.0)),
			"age": 0.0,
			"life": randf_range(0.6, 1.0),
			"size": randf_range(4.0, 8.0),
			"color": col,
		})

func _spawn_ripple(pos: Vector2, radius: float) -> void:
	_ripples.append({
		"pos": pos,
		"r": radius,
		"age": 0.0,
		"life": 0.8,
		"color": Color(GufengTheme.DAI.r, GufengTheme.DAI.g, GufengTheme.DAI.b, 0.75),
	})

# ================= 布局 / 文案（纯函数，门卫可测） =================

func _relayout_bubble() -> void:
	if _bubble == null:
		return
	var font: Font = _bubble_label.get_theme_font("font")
	var fs := _bubble_label.get_theme_font_size("font_size")
	var inner_w := BUBBLE_W - 40.0
	var h := 92.0
	if font != null:
		var tsz := font.get_multiline_string_size(_bubble_text, HORIZONTAL_ALIGNMENT_LEFT, inner_w, fs)
		h = clampf(tsz.y + 42.0, 64.0, BUBBLE_MAX_H)
	_bubble_label.custom_minimum_size = Vector2(inner_w, 0.0)
	_bubble.offset_left = -BUBBLE_W * 0.5
	_bubble.offset_right = BUBBLE_W * 0.5
	_bubble.offset_top = BUBBLE_TOP
	_bubble.offset_bottom = BUBBLE_TOP + h

func _refresh_mood_text() -> void:
	if _mood_chip == null:
		return
	if not _online:
		_mood_chip.text = "Azure · 离线"
		return
	_mood_chip.text = "Azure · " + _mood_name
	_name_plate.tooltip_text = mood_summary(_mood_v, _mood_c, _mood_a, _mood_name)

func _refresh_mute_text() -> void:
	if _mute_btn != null:
		_mute_btn.text = "取消静音" if _muted else "静音"

static func mood_band(name: String, v := 0.5) -> String:
	if name in LOW_NAMES:
		return "low"
	if name in HIGH_NAMES:
		return "high"
	if name in CURIOUS_NAMES:
		return "curious"
	if v < 0.42:
		return "low"
	if v > 0.62:
		return "high"
	return "calm"

static func reaction_lines(band: String) -> Array:
	return REACTIONS.get(band, REACTIONS["calm"]) as Array

## 左键点击的即时台词：情绪带优先，roll 决定同组内的句子（调用方负责避开上一句）
static func pick_reaction(band: String, roll: float) -> String:
	var lines := reaction_lines(band)
	if lines.is_empty():
		return "在呢。"
	var idx := int(clampf(roll, 0.0, 0.999) * float(lines.size()))
	return str(lines[idx])

## 三维数值 → 光环颜色（愉悦 / 好奇 / 唤醒都在影响观感；返回不透明色，透明度由绘制处给）
static func mood_color(v: float, c: float, a: float) -> Color:
	var vv := clampf(v, 0.0, 1.0)
	var cc := clampf(c, 0.0, 1.0)
	var aa := clampf(a, 0.0, 1.0)
	var cool := GufengTheme.CINNABAR.lerp(GufengTheme.DAI, clampf(vv * 1.1, 0.0, 1.0))
	var warm := GufengTheme.OCHRE.lerp(cool, 1.0 - cc * 0.35)
	return warm.lerp(cool, clampf(0.55 + aa * 0.3, 0.0, 1.0))

static func mood_summary(v: float, c: float, a: float, name: String) -> String:
	return "%s · 愉悦 %.2f · 好奇 %.2f · 唤醒 %.2f" % [
		name if name != "" else "平静", clampf(v, 0.0, 1.0), clampf(c, 0.0, 1.0), clampf(a, 0.0, 1.0)]

static func bubble_ttl(text: String) -> float:
	return clampf(2.5 + float(text.length()) * 0.075, 3.0, 9.0)

static func clamp_menu_position(pos: Vector2, menu_size: Vector2, win_size: Vector2) -> Vector2:
	var mx := maxf(MENU_MARGIN, win_size.x - menu_size.x - MENU_MARGIN)
	var my := maxf(MENU_MARGIN, win_size.y - menu_size.y - MENU_MARGIN)
	return Vector2(clampf(pos.x, MENU_MARGIN, mx), clampf(pos.y, MENU_MARGIN, my))

static func is_pet_click(drag_distance: float) -> bool:
	return drag_distance <= DRAG_CLICK_MAX
