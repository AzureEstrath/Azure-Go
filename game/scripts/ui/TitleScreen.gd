class_name TitleScreen
extends Control
## 主界面（深色封面，v3.11.3）。
##
## 设计取向：**不另起一套视觉**——深色遮罩、青蓝线、强调块、无衬线全部取自 `GufengTheme`，
## 3D 棋盘与化身仍在深色遮罩后面（半透明），所以从主界面进对局没有"场景切换"的割裂感。
##
## 版式（v3.11.3）：封面是**远看棋盘时**的一枚半透明深色卡片——卡片偏上（给棋盘留出下半屏），
## 满屏遮罩只做一层很淡的深色（`Veil` 的 alpha 压在 0.4，棋盘清楚可见）。
## 进出对局时的淡入 / 淡出由 `Main` 编排，这里只提供 `fade_in` / `fade_out` / `set_alpha`。
##
## 这个类只负责画与发信号，**不碰配置、不碰服务、不碰棋盘**：
##   · `primary_pressed`  —— 开始对局 / 开始新对局（由 `Main` 决定是不是首次开局）
##   · `settings_pressed` —— 打开 `Main` 的启动设置面板（复用同一块面板，不另写一份设置）
##   · `library_pressed`  —— 打开棋谱库（v3.11.5；列表 / SGF 由 `Main` 接线）
##   · `quit_pressed`     —— 退出游戏
##
## `set_restart_mode(true)` 把主按钮改成「开始新对局」、提示行明说**上一局不保留**——
## 对局中按 Esc 回来时，进入就是新的一局（v3.12.1），不再有「继续上一局」这条路径。

signal primary_pressed
signal settings_pressed
signal library_pressed
signal quit_pressed

var _primary: Button = null
var _hint: Label = null

func _ready() -> void:
	# 必须用 `set_anchors_and_offsets_preset`：`set_anchors_preset` 会保留当前尺寸（这里是 0），
	# 于是 offsets 变成 -父宽/-父高，纸幕实际是 0×0（v3.11.2 实机截图发现的坑）。
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_STOP
	_build()
	visible = false                              # 由 Main 的 open()/close() 控制

func _build() -> void:
	# ① 淡淡的深色遮罩：只做一层「远看」的空气感，棋盘与化身必须看得清
	var veil := ColorRect.new()
	veil.name = "Veil"
	veil.color = Color(GufengTheme.PAPER.r, GufengTheme.PAPER.g, GufengTheme.PAPER.b, 0.40)
	veil.set_anchors_preset(Control.PRESET_FULL_RECT)
	veil.mouse_filter = Control.MOUSE_FILTER_STOP     # 挡住棋盘点击（菜单期间不落子、不摸头）
	add_child(veil)

	# ② 深色卡片：摆在屏幕视觉中心（锚点区间上下对称于 50%），棋盘在下方露出来。
	# v3.12.1：继续下调到正中——上半屏留白，卡片不再贴着上方。
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	center.anchor_top = 0.24
	center.anchor_bottom = 0.76
	center.offset_top = 0.0
	center.offset_bottom = 0.0
	center.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(center)

	var box := PanelContainer.new()
	box.add_theme_stylebox_override("panel", GufengTheme.veil_card(0.92, 26))   # 下调后压在棋盘上：提高不透明度保证文字可读
	box.custom_minimum_size = Vector2(560, 0)
	center.add_child(box)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	vb.alignment = BoxContainer.ALIGNMENT_CENTER
	box.add_child(vb)

	# 青蓝徽标（单字「亚」）：与标题条 / 定式卡片同一套强调色
	var mark := PanelContainer.new()
	mark.add_theme_stylebox_override("panel", GufengTheme.seal(8))
	mark.custom_minimum_size = Vector2(50, 50)
	mark.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
	var mark_label := Label.new()
	mark_label.text = "亚"
	mark_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	mark_label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	mark_label.add_theme_font_size_override("font_size", 26)
	mark_label.add_theme_color_override("font_color", GufengTheme.ON_ACCENT)
	mark.add_child(mark_label)
	vb.add_child(mark)

	var title := Label.new()
	title.text = "Azure · 围棋陪练"
	title.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	title.add_theme_font_size_override("font_size", 32)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	vb.add_child(title)

	var sub := Label.new()
	sub.text = "青蓝色 · 永恒十七岁 · 天才少女"
	sub.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	sub.add_theme_font_size_override("font_size", 12)
	sub.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(sub)

	_rule(vb)

	# 主按钮：青蓝强调块（现代主题里唯一的高饱和块）
	_primary = Button.new()
	_primary.text = "开始对局"
	_primary.custom_minimum_size = Vector2(0, 44)
	_primary.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	GufengTheme.apply_seal_button(_primary)
	_primary.pressed.connect(func(): primary_pressed.emit())
	vb.add_child(_primary)

	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 10)
	vb.add_child(row)
	var library_btn := Button.new()
	library_btn.text = "棋谱库"
	library_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	library_btn.custom_minimum_size = Vector2(0, 36)
	library_btn.tooltip_text = "查看已保存的对局，导入 / 导出 SGF 棋谱"
	GufengTheme.apply_button(library_btn)
	library_btn.pressed.connect(func(): library_pressed.emit())
	row.add_child(library_btn)

	var settings_btn := Button.new()
	settings_btn.text = "设置"
	settings_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	settings_btn.custom_minimum_size = Vector2(0, 36)
	settings_btn.tooltip_text = "打开启动设置面板（思考强度 / LLM / 语音），改完回到主界面"
	GufengTheme.apply_button(settings_btn)
	settings_btn.pressed.connect(func(): settings_pressed.emit())
	row.add_child(settings_btn)

	var quit_btn := Button.new()
	quit_btn.text = "退出"
	quit_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	quit_btn.custom_minimum_size = Vector2(0, 36)
	GufengTheme.apply_button(quit_btn)
	quit_btn.pressed.connect(func(): quit_pressed.emit())
	row.add_child(quit_btn)

	_hint = Label.new()
	_hint.text = "左键落子，右键拖拽旋转视角 · 对局中按 Esc 回主界面"
	_hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_hint.add_theme_font_size_override("font_size", 11)
	_hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vb.add_child(_hint)

	# 底部署名（封面落款位）：不显示版本号，避免每次发版都要改这张封面
	var sign := Label.new()
	sign.text = "落子无声，落笔有情"
	sign.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	sign.add_theme_font_size_override("font_size", 11)
	sign.add_theme_color_override("font_color", Color(GufengTheme.INK_SOFT, 0.75))
	vb.add_child(sign)

func _rule(parent: Node) -> void:
	var r := ColorRect.new()
	r.color = Color(GufengTheme.INK, 0.24)
	r.custom_minimum_size = Vector2(0, 1)
	r.mouse_filter = Control.MOUSE_FILTER_IGNORE
	parent.add_child(r)

## 首次启动 vs 对局中按 Esc：只换主按钮与提示行的措辞，其余一字不动。
## 对局中回来时**没有「继续」语义**：进入就是新的一局（`Main` 会清棋盘并 reset 后端）。
func set_restart_mode(on: bool) -> void:
	if _primary != null:
		_primary.text = "开始新对局" if on else "开始对局"
	if _hint != null:
		_hint.text = ("Esc 开始新对局（上一局不保留） · 左键落子，右键拖拽旋转视角" if on
			else "左键落子，右键拖拽旋转视角 · 对局中按 Esc 回主界面")

## 主按钮文字（门卫用；不暴露内部节点）
func primary_text() -> String:
	return _primary.text if _primary != null else ""

## 整块封面的不透明度（0..1）：淡入淡出由 `Main` 运镜时调用
func set_alpha(a: float) -> void:
	modulate.a = clampf(a, 0.0, 1.0)

func alpha_value() -> float:
	return modulate.a

## 淡出：开始对局时先让封面化进纸里（0.35s 左右），再飞相机
func fade_out(dur := 0.35) -> void:
	if dur <= 0.0:
		set_alpha(0.0)
		return
	var tw := create_tween()
	tw.tween_property(self, "modulate:a", 0.0, dur).set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN)
	await tw.finished

## 淡入：从主界面飞远之后，封面再浮上来
func fade_in(dur := 0.45) -> void:
	if dur <= 0.0:
		set_alpha(1.0)
		return
	var tw := create_tween()
	tw.tween_property(self, "modulate:a", 1.0, dur).set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_OUT)
	await tw.finished

func open() -> void:
	visible = true
	if _primary != null:
		_primary.grab_focus()

func close() -> void:
	visible = false
	if _primary != null and _primary.has_focus():
		_primary.release_focus()                  # 藏起来还占着焦点：Enter 会打到看不见的按钮上
