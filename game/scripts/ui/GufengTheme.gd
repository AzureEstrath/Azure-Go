class_name GufengTheme
extends RefCounted
## 自用版主题：四套预设（暖白 / 宣纸 / 雾蓝 / 深色）。
##
## 布局、尺寸、控件位置完全不变，只换底色 / 面板 / 文字 / 强调色 / 字体。
## 由 `Main._ready()` 按 `config.json` 的 `ui_theme` 调 `apply_preset()`；默认 `mist`。
##
## 保留类名与工厂函数签名，避免大范围改引用。仍然**不引入字体文件、不改几何**。

## —— 预设 ——
## 每个预设只定义一套语义色，工厂函数只读这些语义色，不再各自写死颜色。
static var PALETTES := {
	"warm": {
		"paper": Color(0.953, 0.941, 0.918),        # #F3F0EA
		"paper_warm": Color(0.984, 0.973, 0.957),   # #FBF8F4（深色条上的字）
		"paper_tone": Color(0.933, 0.918, 0.886),
		"ink": Color(0.141, 0.161, 0.184),          # #24292F
		"ink_soft": Color(0.420, 0.447, 0.502),     # #6B7280
		"accent": Color(0.180, 0.490, 0.451),       # #2E7D73
		"danger": Color(0.753, 0.341, 0.310),       # #C0574F
		"warn": Color(0.722, 0.525, 0.184),
		"deep": Color(0.192, 0.255, 0.235),          # 标题条
		"on_accent": Color(1, 1, 1),
		"panel": Color(1, 1, 1, 0.94),
		"panel_soft": Color(0.933, 0.918, 0.886, 0.96),
		"panel_dark": Color(0.953, 0.941, 0.918, 0.98),
		"border": Color(0.847, 0.816, 0.769, 0.90),
		"bubble_user": Color(0.902, 0.949, 0.933, 0.95),
		"hover": Color(0.965, 0.957, 0.941, 0.98),
		"world": Color(0.953, 0.941, 0.918),
		"ambient": Color(0.98, 0.96, 0.93),
		"energy": 0.90,
		"label": Color(0.420, 0.447, 0.502),
		"label_outline": Color(0.984, 0.973, 0.957, 0.90),
		"shadow": 0.14,
		"serif": false,
	},
	"classic": {
		"paper": Color(0.937, 0.894, 0.796),        # #EFE4CB
		"paper_warm": Color(0.984, 0.965, 0.918),   # #FBF6EA
		"paper_tone": Color(0.941, 0.902, 0.816),
		"ink": Color(0.173, 0.165, 0.149),          # #2C2A26
		"ink_soft": Color(0.420, 0.384, 0.341),     # #6B6256
		"accent": Color(0.651, 0.231, 0.180),       # #A63B2E 朱砂
		"danger": Color(0.651, 0.231, 0.180),
		"warn": Color(0.557, 0.400, 0.169),         # 赭石
		"deep": Color(0.173, 0.165, 0.149),
		"on_accent": Color(0.984, 0.965, 0.918),
		"panel": Color(0.984, 0.965, 0.918, 0.94),
		"panel_soft": Color(0.937, 0.894, 0.796, 0.96),
		"panel_dark": Color(0.937, 0.894, 0.796, 0.98),
		"border": Color(0.851, 0.788, 0.659, 0.90),
		"bubble_user": Color(0.961, 0.902, 0.847, 0.95),
		"hover": Color(0.961, 0.914, 0.831, 0.98),
		"world": Color(0.937, 0.894, 0.796),
		"ambient": Color(0.98, 0.94, 0.86),
		"energy": 0.85,
		"label": Color(0.420, 0.384, 0.341),
		"label_outline": Color(0.984, 0.965, 0.918, 0.90),
		"shadow": 0.18,
		"serif": true,
	},
	"mist": {
		"paper": Color(0.918, 0.953, 0.973),        # #EAF3F8
		"paper_warm": Color(0.973, 0.988, 0.992),   # #F8FCFD
		"paper_tone": Color(0.886, 0.933, 0.953),
		"ink": Color(0.118, 0.227, 0.275),          # #1E3A46
		"ink_soft": Color(0.369, 0.467, 0.514),     # #5E7783
		"accent": Color(0.137, 0.478, 0.455),       # #227A74
		"danger": Color(0.753, 0.341, 0.310),       # #C0574F
		"warn": Color(0.722, 0.525, 0.184),
		"deep": Color(0.122, 0.290, 0.333),          # 标题条
		"on_accent": Color(1, 1, 1),
		"panel": Color(0.973, 0.988, 0.992, 0.94),
		"panel_soft": Color(0.886, 0.933, 0.953, 0.96),
		"panel_dark": Color(0.918, 0.953, 0.973, 0.98),
		"border": Color(0.788, 0.871, 0.910, 0.90),
		"bubble_user": Color(0.875, 0.941, 0.957, 0.95),
		"hover": Color(0.933, 0.969, 0.984, 0.98),
		"world": Color(0.918, 0.953, 0.973),
		"ambient": Color(0.95, 0.97, 0.98),
		"energy": 0.90,
		"label": Color(0.369, 0.467, 0.514),
		"label_outline": Color(0.973, 0.988, 0.992, 0.90),
		"shadow": 0.14,
		"serif": false,
	},
	"dark": {
		"paper": Color(0.043, 0.063, 0.078),        # #0B1114
		"paper_warm": Color(0.902, 0.933, 0.945),   # #E6EEF1
		"paper_tone": Color(0.086, 0.122, 0.145),
		"ink": Color(0.902, 0.933, 0.945),
		"ink_soft": Color(0.624, 0.706, 0.749),
		"accent": Color(0.310, 0.820, 0.773),       # #4FD1C5
		"danger": Color(0.878, 0.424, 0.459),       # #E06C75
		"warn": Color(0.855, 0.667, 0.302),
		"deep": Color(0.055, 0.090, 0.110),
		"on_accent": Color(0.031, 0.055, 0.063),
		"panel": Color(0.071, 0.102, 0.122, 0.94),
		"panel_soft": Color(0.090, 0.129, 0.153, 0.96),
		"panel_dark": Color(0.047, 0.071, 0.086, 0.98),
		"border": Color(0.180, 0.255, 0.290, 0.90),
		"bubble_user": Color(0.070, 0.145, 0.155, 0.95),
		"hover": Color(0.105, 0.165, 0.190, 0.98),
		"world": Color(0.043, 0.063, 0.078),
		"ambient": Color(0.62, 0.78, 0.84),
		"energy": 0.62,
		"label": Color(0.70, 0.80, 0.84),
		"label_outline": Color(0.02, 0.04, 0.05, 0.85),
		"shadow": 0.35,
		"serif": false,
	},
}

static var THEME_ID := "mist"
static var FONT_SERIF := false
static var PAPER := Color(0.918, 0.953, 0.973)
static var PAPER_WARM := Color(0.973, 0.988, 0.992)
static var PAPER_TONE := Color(0.886, 0.933, 0.953)
static var INK := Color(0.118, 0.227, 0.275)
static var INK_SOFT := Color(0.369, 0.467, 0.514)
static var CINNABAR := Color(0.753, 0.341, 0.310)
static var DAI := Color(0.137, 0.478, 0.455)
static var DEEP := Color(0.122, 0.290, 0.333)
static var OCHRE := Color(0.722, 0.525, 0.184)
static var ACCENT := DAI
static var ON_ACCENT := Color(1, 1, 1)
static var PANEL_BG := Color(0.973, 0.988, 0.992, 0.94)
static var PANEL_SOFT := Color(0.886, 0.933, 0.953, 0.96)
static var PANEL_DARK := Color(0.918, 0.953, 0.973, 0.98)
static var BORDER := Color(0.788, 0.871, 0.910, 0.90)
static var BORDER_SOFT := Color(0.788, 0.871, 0.910, 0.45)
static var BUBBLE_USER_BG := Color(0.875, 0.941, 0.957, 0.95)
static var HOVER_BG := Color(0.933, 0.969, 0.984, 0.98)
static var WORLD_BG := PAPER
static var AMBIENT_COLOR := Color(0.95, 0.97, 0.98)
static var AMBIENT_ENERGY := 0.90
static var LABEL_FG := Color(0.369, 0.467, 0.514)
static var LABEL_OUTLINE := Color(0.973, 0.988, 0.992, 0.90)
static var TIP_FG := ACCENT
static var SHADOW_ALPHA := 0.14

## 切换预设。未知值回落 mist；调用方应在建 UI 之前调一次。
static func apply_preset(id: String) -> void:
	var key := id.strip_edges().to_lower()
	if not PALETTES.has(key):
		key = "mist"
	THEME_ID = key
	var p: Dictionary = PALETTES[key]
	PAPER = p["paper"]
	PAPER_WARM = p["paper_warm"]
	PAPER_TONE = p["paper_tone"]
	INK = p["ink"]
	INK_SOFT = p["ink_soft"]
	CINNABAR = p["danger"]
	DAI = p["accent"]
	DEEP = p["deep"]
	OCHRE = p["warn"]
	ACCENT = p["accent"]
	ON_ACCENT = p["on_accent"]
	PANEL_BG = p["panel"]
	PANEL_SOFT = p["panel_soft"]
	PANEL_DARK = p["panel_dark"]
	BORDER = p["border"]
	BORDER_SOFT = Color(p["border"], 0.45)
	BUBBLE_USER_BG = p["bubble_user"]
	HOVER_BG = p["hover"]
	WORLD_BG = p["world"]
	AMBIENT_COLOR = p["ambient"]
	AMBIENT_ENERGY = float(p["energy"])
	LABEL_FG = p["label"]
	LABEL_OUTLINE = p["label_outline"]
	TIP_FG = p["accent"]
	SHADOW_ALPHA = float(p["shadow"])
	FONT_SERIF = bool(p["serif"])

static func theme_options() -> Array:
	return [
		{"id": "warm", "label": "暖白简约"},
		{"id": "classic", "label": "宣纸古风"},
		{"id": "mist", "label": "雾蓝清透"},
		{"id": "dark", "label": "深色青蓝"},
	]

# ================= 字体 =================

static func font() -> Font:
	var f := SystemFont.new()
	if FONT_SERIF:
		f.font_names = PackedStringArray(["KaiTi", "STKaiti", "华文楷体", "Kaiti SC", "SimSun", "宋体", "serif"])
	else:
		f.font_names = PackedStringArray(["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
			"Noto Sans CJK SC", "sans-serif"])
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
	if shadow and SHADOW_ALPHA > 0.0:
		sb.shadow_color = Color(0, 0, 0, SHADOW_ALPHA)
		sb.shadow_size = 10
		sb.shadow_offset = Vector2(0, 4)
	return sb

static func panel(pad := 12) -> StyleBoxFlat:
	return _panel_box(PANEL_BG, BORDER, 14, pad)

static func veil_card(alpha := 0.82, pad := 30) -> StyleBoxFlat:
	var bg := Color(PANEL_BG.r, PANEL_BG.g, PANEL_BG.b, alpha)
	return _panel_box(bg, Color(ACCENT, 0.55), 18, pad)

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

static func slip(bg := PANEL_SOFT, pad := 12) -> StyleBoxFlat:
	return _panel_box(bg, BORDER_SOFT, 10, pad, false)

# ================= 聊天面板（v4.4.0 · 方案 A） =================

static func chat_header() -> StyleBoxFlat:
	"""聊天标题条：浅雾纸、一条很细的青绿底线；不抢正文。"""
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(PAPER_WARM, 0.58)
	sb.border_color = Color(ACCENT, 0.22)
	sb.border_width_bottom = 1
	sb.corner_radius_top_left = 12
	sb.corner_radius_top_right = 12
	sb.corner_radius_bottom_left = 0
	sb.corner_radius_bottom_right = 0
	sb.content_margin_left = 10
	sb.content_margin_right = 8
	sb.content_margin_top = 9
	sb.content_margin_bottom = 8
	return sb

static func chat_dock() -> StyleBoxFlat:
	"""底部工具托盘：很淡的雾色，和聊天正文隔开但不另外做成一张卡。"""
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(PAPER_TONE, 0.38)
	sb.border_color = Color(ACCENT, 0.16)
	sb.border_width_top = 1
	sb.corner_radius_bottom_left = 10
	sb.corner_radius_bottom_right = 10
	sb.content_margin_left = 8
	sb.content_margin_right = 8
	sb.content_margin_top = 7
	sb.content_margin_bottom = 8
	return sb

static func seg_box(on: bool, hover := false) -> StyleBoxFlat:
	"""思考档的分段按钮：选中是青绿实心，未选中是半透明雾色。"""
	var sb := StyleBoxFlat.new()
	if on:
		sb.bg_color = ACCENT.lightened(0.06) if hover else ACCENT
		sb.border_color = Color(ON_ACCENT, 0.20)
	else:
		sb.bg_color = Color(PANEL_SOFT, 0.55 if not hover else 0.86)
		sb.border_color = Color(ACCENT, 0.16 if not hover else 0.38)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(8)
	sb.content_margin_left = 7
	sb.content_margin_right = 7
	sb.content_margin_top = 4
	sb.content_margin_bottom = 4
	return sb

static func apply_seg_button(b: Button, on: bool) -> void:
	b.add_theme_stylebox_override("normal", seg_box(on, false))
	b.add_theme_stylebox_override("hover", seg_box(on, true))
	b.add_theme_stylebox_override("pressed", seg_box(true, false))
	b.add_theme_stylebox_override("focus", seg_box(on, true))
	var fg := ON_ACCENT if on else INK_SOFT
	b.add_theme_color_override("font_color", fg)
	b.add_theme_color_override("font_hover_color", fg)
	b.add_theme_color_override("font_pressed_color", ON_ACCENT)
	b.add_theme_color_override("font_focus_color", fg)

# ================= 对话气泡 =================

static func bubble_ai() -> StyleBoxFlat:
	var sb := slip(PANEL_SOFT, 9)
	sb.border_color = Color(ACCENT, 0.58)
	sb.border_width_left = 3
	sb.border_width_right = 0
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	sb.corner_radius_top_left = 3
	sb.corner_radius_top_right = 11
	sb.corner_radius_bottom_right = 11
	sb.corner_radius_bottom_left = 11
	sb.shadow_color = Color(0, 0, 0, 0.045)
	sb.shadow_size = 4
	sb.shadow_offset = Vector2(0, 2)
	return sb

static func bubble_sys() -> StyleBoxFlat:
	var sb := slip(PANEL_DARK, 10)
	sb.border_color = BORDER_SOFT
	return sb

static func bubble_user() -> StyleBoxFlat:
	var sb := slip(BUBBLE_USER_BG, 9)
	sb.border_color = Color(ACCENT, 0.72)
	sb.border_width_left = 0
	sb.border_width_right = 3
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	sb.corner_radius_top_left = 11
	sb.corner_radius_top_right = 3
	sb.corner_radius_bottom_right = 11
	sb.corner_radius_bottom_left = 11
	sb.shadow_color = Color(0, 0, 0, 0.04)
	sb.shadow_size = 4
	sb.shadow_offset = Vector2(0, 2)
	return sb

static func seal(pad := 10) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = ACCENT
	sb.border_color = Color(ACCENT.darkened(0.25))
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(8)
	sb.content_margin_left = pad
	sb.content_margin_right = pad
	sb.content_margin_top = int(pad * 0.6)
	sb.content_margin_bottom = int(pad * 0.6)
	return sb

# ================= 控件 =================

static func apply_button(b: Button, big := false) -> void:
	var fs := 16 if big else 14
	b.add_theme_font_size_override("font_size", fs)
	b.add_theme_stylebox_override("normal", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	b.add_theme_stylebox_override("hover", _btn_box(HOVER_BG, Color(ACCENT, 0.75), false))
	b.add_theme_stylebox_override("pressed", _btn_box(ACCENT, ACCENT, true))
	b.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), Color(ACCENT, 0.55), false))
	b.add_theme_stylebox_override("disabled", _btn_box(Color(PANEL_DARK, 0.55), BORDER_SOFT, false))
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

static func apply_seal_button(b: Button, big := true) -> void:
	b.add_theme_font_size_override("font_size", 16 if big else 14)
	b.add_theme_stylebox_override("normal", seal(12))
	b.add_theme_stylebox_override("hover", _seal_variant(ACCENT.lightened(0.12)))
	b.add_theme_stylebox_override("pressed", _seal_variant(ACCENT.darkened(0.18)))
	b.add_theme_stylebox_override("focus", _seal_variant(Color(0, 0, 0, 0)))
	b.add_theme_color_override("font_color", ON_ACCENT)
	b.add_theme_color_override("font_hover_color", ON_ACCENT)
	b.add_theme_color_override("font_pressed_color", ON_ACCENT)

static func _seal_variant(bg: Color) -> StyleBoxFlat:
	var sb := seal(12)
	sb.bg_color = bg
	return sb

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

# ================= 测试清理（v4.4.2） =================

static func apply_danger_button(b: Button) -> void:
	"""危险操作按钮：低饱和朱砂描边，只有 hover / pressed 才加重。"""
	b.add_theme_font_size_override("font_size", 13)
	b.add_theme_stylebox_override("normal", _btn_box(Color(PANEL_SOFT, 0.72), Color(CINNABAR, 0.30), false))
	b.add_theme_stylebox_override("hover", _btn_box(Color(CINNABAR, 0.09), Color(CINNABAR, 0.62), false))
	b.add_theme_stylebox_override("pressed", _btn_box(Color(CINNABAR, 0.88), Color(CINNABAR, 0.88), true))
	b.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), Color(CINNABAR, 0.45), false))
	b.add_theme_color_override("font_color", CINNABAR)
	b.add_theme_color_override("font_hover_color", CINNABAR.darkened(0.10))
	b.add_theme_color_override("font_pressed_color", ON_ACCENT)
	b.add_theme_color_override("font_focus_color", CINNABAR)

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

# ================= 组装 =================

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
	th.set_stylebox("hover", "Button", _btn_box(HOVER_BG, Color(ACCENT, 0.75), false))
	th.set_stylebox("pressed", "Button", _btn_box(ACCENT, ACCENT, true))
	th.set_stylebox("normal", "OptionButton", _btn_box(PANEL_SOFT, BORDER_SOFT, false))
	th.set_stylebox("hover", "OptionButton", _btn_box(HOVER_BG, Color(ACCENT, 0.75), false))
	th.set_stylebox("pressed", "OptionButton", _btn_box(ACCENT, ACCENT, true))
	th.set_stylebox("focus", "OptionButton", _btn_box(Color(0, 0, 0, 0), Color(ACCENT, 0.55), false))
	th.set_color("font_pressed_color", "OptionButton", ON_ACCENT)
	th.set_stylebox("normal", "LineEdit", _input_box())
	th.set_stylebox("focus", "LineEdit", _input_box())
	th.set_color("font_placeholder_color", "LineEdit", Color(INK_SOFT, 0.78))
	th.set_color("caret_color", "LineEdit", ACCENT)
	var track := StyleBoxFlat.new()
	track.bg_color = Color(INK_SOFT, 0.32)
	track.set_corner_radius_all(2)
	track.content_margin_top = 1
	track.content_margin_bottom = 1
	th.set_stylebox("slider", "HSlider", track)
	th.set_stylebox("grabber_area", "HSlider", _bar(ACCENT))
	th.set_stylebox("grabber_area_highlight", "HSlider", _bar(ACCENT))
	var grab := _grabber_icon()
	th.set_icon("grabber", "HSlider", grab)
	th.set_icon("grabber_highlight", "HSlider", grab)
	th.set_icon("unchecked", "CheckBox", _check_icon(false))
	th.set_icon("checked", "CheckBox", _check_icon(true))
	for st in ["normal", "pressed", "hover", "disabled", "focus"]:
		th.set_stylebox(st, "CheckBox", _empty_box())
	for st in ["font_color", "font_pressed_color", "font_hover_color", "font_focus_color", "font_disabled_color"]:
		th.set_color(st, "CheckBox", INK)
	th.set_stylebox("scroll", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("scroll_focus", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("grabber", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.35), 3))
	th.set_stylebox("grabber_highlight", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.55), 3))
	th.set_stylebox("grabber_pressed", "VScrollBar", _scroll_grabber(Color(ACCENT, 0.75), 3))
	# v4.4.2：Godot 默认 tooltip 是黑底白字，与雾蓝界面割裂；这里统一成浅雾纸卡片。
	th.set_stylebox("panel", "TooltipPanel", _panel_box(PANEL_BG, BORDER, 8, 7, true))
	th.set_color("font_color", "TooltipLabel", INK)
	th.set_font_size("font_size", "TooltipLabel", 11)
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
	sb.bg_color = PANEL_DARK
	sb.border_color = BORDER
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(8)
	sb.content_margin_left = 9
	sb.content_margin_right = 9
	sb.content_margin_top = 7
	sb.content_margin_bottom = 7
	return sb

static func _grabber_icon() -> Texture2D:
	var img := Image.create(16, 16, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	var c := Vector2(8, 8)
	for y in 16:
		for x in 16:
			if Vector2(x + 0.5, y + 0.5).distance_to(c) <= 6.0:
				img.set_pixel(x, y, ACCENT)
	return ImageTexture.create_from_image(img)

static func _check_icon(on: bool) -> Texture2D:
	var img := Image.create(16, 16, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	var box := Color(INK_SOFT, 0.70)
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
