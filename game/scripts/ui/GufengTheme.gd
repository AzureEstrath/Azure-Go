class_name GufengTheme
extends RefCounted
## 自用版主题：**现代深色 + 青蓝强调**（不再使用宣纸 / 墨框 / 朱砂印）。
##
## 保留类名与工厂函数签名，避免 `Main` / `TitleScreen` / `GameLibrary` / `GameReport`
## 大范围改引用；内部配色、字体、面与控件全部换成深色扁平语言：
##   · 深色底 + 半透明面板 + 1px 冷灰描边 + 10~14px 圆角；
##   · Azure 的青蓝 `ACCENT` 作为唯一高饱和强调色（主按钮、选中、焦点、滚动条）；
##   · 珊瑚红只做错误 / 亏损；琥珀只做分岔提示；
##   · 字体换无衬线（微软雅黑 / PingFang / Noto Sans），不再用楷体。
##
## 仍然**不引入字体文件、不改任何几何**：侧栏宽、卡片尺寸、避让落位全部沿用原值。

## —— 色 ——
const BG := Color(0.043, 0.063, 0.078)            # #0B1114 深色虚空
const PAPER := BG                                  # 兼容旧名：现在是大面积深色底
const PAPER_WARM := Color(0.902, 0.933, 0.945)    # #E6EEF1 主文字 / 亮面
const PAPER_TONE := Color(0.086, 0.122, 0.145)    # #16232A 次级面 / 悬停
const INK := Color(0.902, 0.933, 0.945)           # 兼容旧名：现在用于亮色文字
const INK_SOFT := Color(0.624, 0.706, 0.749)      # #9FB4BF 次级文字
const CINNABAR := Color(0.878, 0.424, 0.459)      # #E06C75 珊瑚红（错误 / 亏损）
const DAI := Color(0.310, 0.820, 0.773)           # #4FD1C5 Azure 青蓝
const DEEP := Color(0.055, 0.090, 0.110)          # 标题条底色
const OCHRE := Color(0.855, 0.667, 0.302)         # #DAA94D 琥珀（分岔 / 提示）
const ACCENT := DAI
const ON_ACCENT := Color(0.031, 0.055, 0.063)     # 强调块上的深色字

const PANEL_BG := Color(0.071, 0.102, 0.122, 0.94)
const PANEL_SOFT := Color(0.090, 0.129, 0.153, 0.96)
const PANEL_DARK := Color(0.047, 0.071, 0.086, 0.98)
const BORDER := Color(0.180, 0.255, 0.290, 0.90)
const BORDER_SOFT := Color(0.180, 0.255, 0.290, 0.45)

# ================= 字体 =================

## 无衬线优先：现代 UI 的骨架。取不到就顺着往系统 sans-serif 退。
static func font() -> Font:
	var f := SystemFont.new()
	f.font_names = PackedStringArray([
		"Microsoft YaHei UI", "Microsoft YaHei",
		"PingFang SC", "Noto Sans CJK SC", "sans-serif",
	])
	# 状态行/日志里仍可能混进符号，留一条 emoji 兜底（主题本身不再主动使用 emoji）
	var emoji := SystemFont.new()
	emoji.font_names = PackedStringArray(["Segoe UI Symbol", "Segoe UI Emoji"])
	f.fallbacks = [emoji]
	return f

# ================= 面 =================

static func _panel_box(bg: Color, border: Color, radius: int, pad: int, shadow := true) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(radius)
	sb.content_margin_left = pad + 2
	sb.content_margin_right = pad + 2
	sb.content_margin_top = pad
	sb.content_margin_bottom = pad
	if shadow:
		sb.shadow_color = Color(0, 0, 0, 0.35)
		sb.shadow_size = 10
		sb.shadow_offset = Vector2(0, 4)
	return sb

## 大面板：深色半透明 + 冷灰描边 + 圆角 + 轻阴影
static func panel(pad := 12) -> StyleBoxFlat:
	return _panel_box(PANEL_BG, BORDER, 14, pad)

## 主界面卡片：更深的半透明底 + 青蓝描边（棋盘/化身从后面透出来）
static func veil_card(alpha := 0.82, pad := 30) -> StyleBoxFlat:
	var bg := Color(PANEL_BG.r, PANEL_BG.g, PANEL_BG.b, alpha)
	var sb := _panel_box(bg, Color(ACCENT, 0.55), 18, pad)
	return sb

## 标题条：深色底 + 底部青蓝线；顶部两角跟随面板圆角
static func header() -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = DEEP
	sb.border_color = ACCENT
	sb.border_width_bottom = 2
	sb.set_corner_radius_all(0)
	sb.corner_radius_top_left = 12
	sb.corner_radius_top_right = 12
	sb.content_margin_left = 12
	sb.content_margin_right = 12
	sb.content_margin_top = 9
	sb.content_margin_bottom = 9
	return sb

## 纸签 / 抽屉 / 确认框：深色圆角卡片
static func slip(bg := PANEL_SOFT, pad := 12) -> StyleBoxFlat:
	return _panel_box(bg, BORDER_SOFT, 10, pad, false)

# ================= 对话气泡（三种） =================

## 她的话：深色面 + 左侧青蓝竖线
static func bubble_ai() -> StyleBoxFlat:
	var sb := slip(PANEL_SOFT, 10)
	sb.border_color = Color(ACCENT, 0.70)
	sb.border_width_left = 3
	sb.border_width_right = 0
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	return sb

## 系统提示：更暗的面 + 极轻描边
static func bubble_sys() -> StyleBoxFlat:
	var sb := slip(PANEL_DARK, 10)
	sb.border_color = BORDER_SOFT
	return sb

## 玩家的话：青蓝染色面 + 右侧青蓝竖线
static func bubble_user() -> StyleBoxFlat:
	var sb := slip(Color(0.070, 0.145, 0.155, 0.95), 10)
	sb.border_color = Color(ACCENT, 0.85)
	sb.border_width_left = 0
	sb.border_width_right = 3
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	return sb

## 强调块：青蓝底 + 深色字（主按钮 / 徽标 / 印章位）
static func seal(pad := 10) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = ACCENT
	sb.border_color = Color(0.18, 0.62, 0.58)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(8)
	sb.content_margin_left = pad
	sb.content_margin_right = pad
	sb.content_margin_top = int(pad * 0.6)
	sb.content_margin_bottom = int(pad * 0.6)
	return sb

# ================= 控件 =================

## 按钮：深色面 + 1px 冷灰边；悬停青蓝字，按下青蓝底反深色字。
static func apply_button(b: Button, big := false) -> void:
	var fs := 16 if big else 14
	b.add_theme_font_size_override("font_size", fs)
	b.add_theme_stylebox_override("normal", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	b.add_theme_stylebox_override("hover", _btn_box(Color(0.105, 0.165, 0.190, 0.98), Color(ACCENT, 0.75), false))
	b.add_theme_stylebox_override("pressed", _btn_box(ACCENT, ACCENT, true))
	b.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), Color(ACCENT, 0.55), false))
	b.add_theme_stylebox_override("disabled", _btn_box(Color(0.06, 0.08, 0.10, 0.55), BORDER_SOFT, false))
	b.add_theme_color_override("font_color", INK)
	b.add_theme_color_override("font_hover_color", ACCENT)
	b.add_theme_color_override("font_pressed_color", ON_ACCENT)
	b.add_theme_color_override("font_disabled_color", Color(INK_SOFT, 0.45))

static func _btn_box(bg: Color, border: Color, inverted := false) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border if not inverted else Color(ON_ACCENT, 0.35)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(9)
	sb.content_margin_left = 12
	sb.content_margin_right = 12
	sb.content_margin_top = 8
	sb.content_margin_bottom = 8
	return sb

## 青蓝主按钮：整块强调色，深色字
static func apply_seal_button(b: Button, big := true) -> void:
	b.add_theme_font_size_override("font_size", 16 if big else 14)
	b.add_theme_stylebox_override("normal", seal(12))
	b.add_theme_stylebox_override("hover", _seal_variant(Color(0.38, 0.88, 0.82)))
	b.add_theme_stylebox_override("pressed", _seal_variant(Color(0.22, 0.66, 0.61)))
	b.add_theme_stylebox_override("focus", _seal_variant(Color(0, 0, 0, 0)))
	b.add_theme_color_override("font_color", ON_ACCENT)
	b.add_theme_color_override("font_hover_color", ON_ACCENT)
	b.add_theme_color_override("font_pressed_color", ON_ACCENT)

static func _seal_variant(bg: Color) -> StyleBoxFlat:
	var sb := seal(12)
	sb.bg_color = bg
	return sb

## 画面边缘的收起/展开小按钮：透明底，悬停给一层青蓝淡底
static func apply_bare_button(b: Button) -> void:
	b.add_theme_font_size_override("font_size", 14)
	var empty := StyleBoxFlat.new()
	empty.bg_color = Color(0, 0, 0, 0)
	var hover := StyleBoxFlat.new()
	hover.bg_color = Color(ACCENT, 0.12)
	hover.set_corner_radius_all(8)
	b.add_theme_stylebox_override("normal", empty)
	b.add_theme_stylebox_override("hover", hover)
	b.add_theme_stylebox_override("pressed", hover)
	b.add_theme_stylebox_override("focus", empty)
	b.add_theme_color_override("font_color", INK_SOFT)
	b.add_theme_color_override("font_hover_color", ACCENT)
	b.add_theme_color_override("font_pressed_color", ACCENT)

## 单字青蓝徽标（标题条右端 / 卡片角标）
static func seal_char(ch: String) -> Control:
	var p := PanelContainer.new()
	p.custom_minimum_size = Vector2(30, 30)
	p.size_flags_horizontal = Control.SIZE_SHRINK_END
	p.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	p.add_theme_stylebox_override("panel", seal(4))
	var l := Label.new()
	l.text = ch
	l.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	l.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	l.add_theme_font_size_override("font_size", 18)
	l.add_theme_color_override("font_color", ON_ACCENT)
	p.add_child(l)
	return p

## 输入框：深色面 + 1px 冷灰边 + 圆角；焦点仍用同一套面，光标青蓝
static func apply_line_edit(e: LineEdit) -> void:
	e.add_theme_stylebox_override("normal", _input_box())
	e.add_theme_stylebox_override("focus", _input_box())
	e.add_theme_color_override("font_color", INK)
	e.add_theme_color_override("font_placeholder_color", Color(INK_SOFT, 0.78))
	e.add_theme_color_override("caret_color", ACCENT)

## 下拉框：与按钮同一套语言
static func apply_option(o: OptionButton) -> void:
	o.add_theme_font_size_override("font_size", 13)
	o.add_theme_stylebox_override("normal", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	o.add_theme_stylebox_override("hover", _btn_box(Color(0.105, 0.165, 0.190, 0.98), Color(ACCENT, 0.75), false))
	o.add_theme_stylebox_override("pressed", _btn_box(ACCENT, ACCENT, true))
	o.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), Color(ACCENT, 0.55), false))
	o.add_theme_color_override("font_color", INK)
	o.add_theme_color_override("font_hover_color", ACCENT)
	o.add_theme_color_override("font_pressed_color", ON_ACCENT)

# ================= 组装 =================

## 根主题：没被单独覆盖的控件（Label / Button / LineEdit …）也吃到这套皮
static func build_theme(f: Font) -> Theme:
	var th := Theme.new()
	th.default_font = f
	th.default_font_size = 15
	th.set_color("font_color", "Label", INK)
	th.set_color("font_color", "Button", INK)
	th.set_color("font_color", "CheckBox", INK)
	th.set_color("font_color", "OptionButton", INK)
	th.set_color("font_color", "LineEdit", INK)
	th.set_stylebox("panel", "PanelContainer", panel(10))
	th.set_stylebox("normal", "Button", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	th.set_stylebox("hover", "Button", _btn_box(Color(0.105, 0.165, 0.190, 0.98), Color(ACCENT, 0.75), false))
	th.set_stylebox("pressed", "Button", _btn_box(ACCENT, ACCENT, true))
	th.set_stylebox("normal", "OptionButton", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	th.set_stylebox("hover", "OptionButton", _btn_box(Color(0.105, 0.165, 0.190, 0.98), Color(ACCENT, 0.75), false))
	th.set_stylebox("pressed", "OptionButton", _btn_box(ACCENT, ACCENT, true))
	th.set_stylebox("focus", "OptionButton", _btn_box(Color(0, 0, 0, 0), Color(ACCENT, 0.55), false))
	th.set_color("font_pressed_color", "OptionButton", ON_ACCENT)
	th.set_stylebox("normal", "LineEdit", _input_box())
	th.set_stylebox("focus", "LineEdit", _input_box())
	th.set_color("font_placeholder_color", "LineEdit", Color(INK_SOFT, 0.78))
	th.set_color("caret_color", "LineEdit", ACCENT)
	# 滑杆：冷灰细线 + 青蓝已选段 + 青蓝圆点
	var track := StyleBoxFlat.new()
	track.bg_color = Color(0.20, 0.30, 0.36, 0.55)
	track.set_corner_radius_all(2)
	track.content_margin_top = 1
	track.content_margin_bottom = 1
	th.set_stylebox("slider", "HSlider", track)
	th.set_stylebox("grabber_area", "HSlider", _bar(ACCENT))
	th.set_stylebox("grabber_area_highlight", "HSlider", _bar(ACCENT))
	var grab := _grabber_icon()
	th.set_icon("grabber", "HSlider", grab)
	th.set_icon("grabber_highlight", "HSlider", grab)
	# 复选框：深色方框 / 青蓝勾
	th.set_icon("unchecked", "CheckBox", _check_icon(false))
	th.set_icon("checked", "CheckBox", _check_icon(true))
	# 勾选态在 Godot 里走 Button 的 "pressed" 样式 ⇒ 会继承成强调色块把字吞掉：显式清空
	for st in ["normal", "pressed", "hover", "disabled", "focus"]:
		th.set_stylebox(st, "CheckBox", _empty_box())
	for st in ["font_color", "font_pressed_color", "font_hover_color", "font_focus_color", "font_disabled_color"]:
		th.set_color(st, "CheckBox", INK)
	# 滚动条：细青蓝条
	th.set_stylebox("scroll", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("scroll_focus", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("grabber", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.35), 3))
	th.set_stylebox("grabber_highlight", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.55), 3))
	th.set_stylebox("grabber_pressed", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.75), 3))
	return th

static func _empty_box() -> StyleBoxEmpty:
	return StyleBoxEmpty.new()

static func _scroll_grabber(col: Color, half := 3) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = col
	sb.set_corner_radius_all(half)
	sb.content_margin_left = half
	sb.content_margin_right = half
	return sb

static func _bar(col: Color) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = col
	sb.set_corner_radius_all(2)
	sb.content_margin_top = 1
	sb.content_margin_bottom = 1
	return sb

static func _input_box() -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.047, 0.071, 0.086, 0.96)
	sb.border_color = BORDER
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(8)
	sb.content_margin_left = 9
	sb.content_margin_right = 9
	sb.content_margin_top = 7
	sb.content_margin_bottom = 7
	return sb

# ================= 程序化图标（不引入图片资源） =================

## 滑杆圆点：16×16 青蓝实心圆
static func _grabber_icon() -> Texture2D:
	var img := Image.create(16, 16, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	var c := Vector2(8, 8)
	for y in 16:
		for x in 16:
			if Vector2(x + 0.5, y + 0.5).distance_to(c) <= 6.0:
				img.set_pixel(x, y, ACCENT)
	return ImageTexture.create_from_image(img)

## 复选框：深色方框 + 青蓝勾；勾用 1px 线，16px 下够清楚
static func _check_icon(on: bool) -> Texture2D:
	var img := Image.create(16, 16, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	var box := Color(0.20, 0.30, 0.36, 0.95)
	for i in range(2, 14):
		img.set_pixel(i, 2, box)
		img.set_pixel(i, 13, box)
		img.set_pixel(2, i, box)
		img.set_pixel(13, i, box)
	if on:
		for i in range(3, 13):
			for j in range(3, 13):
				img.set_pixel(i, j, ACCENT)
		_draw_icon_line(img, Vector2i(4, 8), Vector2i(7, 11), ON_ACCENT)
		_draw_icon_line(img, Vector2i(7, 11), Vector2i(12, 5), ON_ACCENT)
	return ImageTexture.create_from_image(img)

## 简易 Bresenham 画线（只给 16px 图标用）
static func _draw_icon_line(img: Image, a: Vector2i, b: Vector2i, col: Color) -> void:
	var dx := absi(b.x - a.x)
	var dy := -absi(b.y - a.y)
	var sx := 1 if a.x < b.x else -1
	var sy := 1 if a.y < b.y else -1
	var err := dx + dy
	var p := a
	while true:
		img.set_pixel(p.x, p.y, col)
		if p == b:
			break
		var e2 := 2 * err
		if e2 >= dy:
			err += dy
			p.x += sx
		if e2 <= dx:
			err += dx
			p.y += sy
