class_name GufengTheme
extends RefCounted
## 古风主题（书卷气）：**宣纸 + 墨线 + 朱砂印**。
##
## 全局样式的唯一出处：`Main` / `JosekiCard` 只调用这里的工厂函数，不再各自配色。
## 两条底线：
##   · **不引入字体文件**——优先系统楷体（`KaiTi` / 华文楷体），没有就退回宋体 / 雅黑；
##   · **不改任何几何**——侧栏宽、卡片尺寸、避让落位全部沿用原值，门卫不受影响。
##
## 材质是程序化生成的贴图（`tools/` 里的脚本可复现）：
##   `paper.png`       256×256 可平铺宣纸（低频云斑 + 细颗粒 + 纤维）
##   `frame_line.png`  128×128 **纸页边**九宫格（1px 发丝线 + 内侧极淡压暗，**没有黑框**）
##   `frame_ink.png`   128×128 手绘墨框——只给**定式卡片**这张"纸片"用，侧栏不用
##
## 两条配色铁律（v3.10.1，来自实机反馈）：
##   · **不用近黑做背景**：标题条用黛青 `DEEP`，不用 `INK`；
##   · **不用高饱和荧光色**：朱砂 / 赭石 / 黛 全部压过明度，文字一律 ≥ 4.5:1。

## —— 色 ——
const PAPER := Color(0.961, 0.937, 0.878)        # #F5EFE0 宣纸
const PAPER_WARM := Color(0.984, 0.965, 0.918)   # #FBF6EA 纸白
const PAPER_TONE := Color(0.937, 0.894, 0.796)   # #EFE4CB 淡宣
const INK := Color(0.173, 0.165, 0.149)          # #2C2A26 墨（**只做文字与线**，不做大块底色）
const INK_SOFT := Color(0.353, 0.322, 0.286)     # #5A5249 淡墨（次级文字；纸上 ≥ 5:1）
const CINNABAR := Color(0.651, 0.231, 0.180)     # #A63B2E 朱砂
const DAI := Color(0.243, 0.361, 0.345)          # #3E5C58 黛
const DEEP := Color(0.169, 0.259, 0.239)         # #2B423D 黛青（标题条底色：深而不黑）
const OCHRE := Color(0.557, 0.400, 0.169)        # #8E662B 贝石（分岔印；对纸白 4.75:1）

## —— 材质 ——
const PAPER_TEX := preload("res://assets/ui/paper.png")
const FRAME_TEX := preload("res://assets/ui/frame_ink.png")     # 只给定式卡片
const LINE_TEX := preload("res://assets/ui/frame_line.png")     # 侧栏 / 弹窗：纸页边
const GRABBER_TEX := preload("res://assets/ui/grabber.png")
const CHECK_OFF_TEX := preload("res://assets/ui/check_off.png")
const CHECK_ON_TEX := preload("res://assets/ui/check_on.png")
const FRAME_MARGIN := 22                          # 九宫格边距（贴图里墨框画在 22px 以内）

# ================= 字体 =================

## 楷体优先：这套主题的"古风"一半靠字。取不到就顺着往宋体 / 雅黑退。
static func font() -> Font:
	var f := SystemFont.new()
	f.font_names = PackedStringArray([
		"KaiTi", "STKaiti", "华文楷体", "Kaiti SC",
		"SimSun", "NSimSun", "宋体",
		"Microsoft YaHei UI", "Microsoft YaHei", "sans-serif",
	])
	# 状态行/日志里仍可能混进符号，留一条 emoji 兜底（主题本身不再主动使用 emoji）
	var emoji := SystemFont.new()
	emoji.font_names = PackedStringArray(["Segoe UI Symbol", "Segoe UI Emoji"])
	f.fallbacks = [emoji]
	return f

# ================= 面 =================

## 大面板：宣纸底 + **发丝纸边**（没有黑框：实机反馈"工具栏黑边"就是原墨框）
static func panel(pad := 12) -> StyleBoxTexture:
	var sb := StyleBoxTexture.new()
	sb.texture = LINE_TEX
	sb.set_texture_margin_all(FRAME_MARGIN)
	sb.content_margin_left = pad + 4
	sb.content_margin_right = pad + 4
	sb.content_margin_top = pad
	sb.content_margin_bottom = pad
	return sb

## 半透明纸签：**只给主界面封面用**——棋盘要在纸面后面透出来（侧栏仍用 `panel()`，不透明）。
## `alpha` 压得越低，棋盘越清楚；封面文字是墨色，纸上仍够 4.5:1。
static func veil_card(alpha := 0.82, pad := 30) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(PAPER_WARM.r, PAPER_WARM.g, PAPER_WARM.b, alpha)
	sb.border_color = Color(INK, 0.30)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(0)
	sb.content_margin_left = pad + 4
	sb.content_margin_right = pad + 4
	sb.content_margin_top = pad
	sb.content_margin_bottom = pad
	return sb

## 标题条：**黛青底**（深色但不是黑）+ 朱砂横线。右侧压一枚单字印。
static func header() -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = DEEP
	sb.border_color = CINNABAR
	sb.border_width_bottom = 2
	sb.content_margin_left = 12
	sb.content_margin_right = 12
	sb.content_margin_top = 9
	sb.content_margin_bottom = 9
	return sb

## 纸签：拉起式对话、设置抽屉、确认框都用它（无圆角、1px 淡墨线）
static func slip(bg := PAPER_WARM, pad := 12) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = Color(INK, 0.28)
	sb.set_border_width_all(1)
	# 无圆角：古风版式靠直线与留白，不靠圆角
	sb.set_corner_radius_all(0)
	sb.content_margin_left = pad
	sb.content_margin_right = pad
	sb.content_margin_top = int(pad * 0.75)
	sb.content_margin_bottom = int(pad * 0.75)
	return sb

# ================= 对话气泡（三种） =================

## 她的话：纸白底 + **左侧墨色竖线**（像棋谱边注），不切角
static func bubble_ai() -> StyleBoxFlat:
	var sb := slip(PAPER_WARM, 10)
	sb.border_color = Color(INK, 0.55)
	sb.border_width_left = 3
	sb.border_width_right = 0
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	return sb

## 系统提示：淡宣底 + 极轻的墨线
static func bubble_sys() -> StyleBoxFlat:
	var sb := slip(PAPER_TONE, 10)
	sb.border_color = Color(INK, 0.22)
	return sb

## 玩家的话：纸白底 + **右侧朱砂竖线**
static func bubble_user() -> StyleBoxFlat:
	var sb := slip(PAPER_WARM, 10)
	sb.border_color = Color(CINNABAR, 0.85)
	sb.border_width_left = 0
	sb.border_width_right = 3
	sb.border_width_top = 0
	sb.border_width_bottom = 0
	return sb

## 朱砂印：印章式色块（徽标 / 主按钮 / 印章字）
static func seal(pad := 10) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = CINNABAR
	sb.border_color = Color(0.55, 0.16, 0.12)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(1)
	sb.content_margin_left = pad
	sb.content_margin_right = pad
	sb.content_margin_top = int(pad * 0.6)
	sb.content_margin_bottom = int(pad * 0.6)
	return sb

# ================= 控件 =================

## 按钮：一条墨线 + 纸底；悬停变朱砂字，按下反白。（无图标、无圆角、无阴影）
static func apply_button(b: Button, big := false) -> void:
	var fs := 15 if big else 13
	b.add_theme_font_size_override("font_size", fs)
	b.add_theme_stylebox_override("normal", _btn_box(PAPER_WARM, 0.45, false))
	b.add_theme_stylebox_override("hover", _btn_box(PAPER_TONE, 0.85, false))
	b.add_theme_stylebox_override("pressed", _btn_box(DEEP, 0.9, true))
	b.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), 0.0, false))
	b.add_theme_stylebox_override("disabled", _btn_box(PAPER, 0.12, false))
	b.add_theme_color_override("font_color", INK)
	b.add_theme_color_override("font_hover_color", CINNABAR)
	b.add_theme_color_override("font_pressed_color", PAPER_WARM)
	b.add_theme_color_override("font_disabled_color", Color(INK, 0.45))

static func _btn_box(bg: Color, border_a: float, inverted: bool) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = Color(CINNABAR if inverted else INK, border_a)
	sb.set_border_width_all(1)
	# 只留底部一条粗线，像纸签压在桌面上
	sb.border_width_top = 0
	sb.border_width_left = 0
	sb.border_width_right = 0
	sb.border_width_bottom = 2
	sb.set_corner_radius_all(0)
	sb.content_margin_left = 10
	sb.content_margin_right = 10
	# 最小点按高度 ≈ 32px（原来 24px 在 4K/缩放屏上太难点）
	sb.content_margin_top = 7
	sb.content_margin_bottom = 7
	return sb

## 朱砂主按钮（结束 / 确认）：整块红印，反白字
static func apply_seal_button(b: Button, big := true) -> void:
	b.add_theme_font_size_override("font_size", 15 if big else 13)
	b.add_theme_stylebox_override("normal", seal(12))
	b.add_theme_stylebox_override("hover", _seal_variant(Color(0.72, 0.27, 0.21)))
	b.add_theme_stylebox_override("pressed", _seal_variant(Color(0.50, 0.15, 0.11)))
	b.add_theme_stylebox_override("focus", _seal_variant(Color(0, 0, 0, 0)))
	b.add_theme_color_override("font_color", PAPER_WARM)
	b.add_theme_color_override("font_hover_color", PAPER_WARM)
	b.add_theme_color_override("font_pressed_color", PAPER_TONE)

static func _seal_variant(bg: Color) -> StyleBoxFlat:
	var sb := seal(12)
	sb.bg_color = bg
	return sb

## 画面边缘的收起/展开小按钮：**透明底**（面板标题就在它旁边，实心底会把第一个字盖掉）
static func apply_bare_button(b: Button) -> void:
	b.add_theme_font_size_override("font_size", 14)
	var empty := StyleBoxFlat.new()
	empty.bg_color = Color(0, 0, 0, 0)
	var hover := StyleBoxFlat.new()
	hover.bg_color = Color(INK, 0.08)
	hover.border_color = Color(INK, 0.35)
	hover.border_width_bottom = 1
	b.add_theme_stylebox_override("normal", empty)
	b.add_theme_stylebox_override("hover", hover)
	b.add_theme_stylebox_override("pressed", hover)
	b.add_theme_stylebox_override("focus", empty)
	b.add_theme_color_override("font_color", INK_SOFT)
	b.add_theme_color_override("font_hover_color", CINNABAR)
	b.add_theme_color_override("font_pressed_color", CINNABAR)

## 单字朱砂印（标题条右端 / 卡片角标）：方印 + 纸白字
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
	l.add_theme_font_size_override("font_size", 17)
	l.add_theme_color_override("font_color", PAPER_WARM)
	p.add_child(l)
	return p

## 输入框：纸面 + 底部墨线（无框、无圆角）
static func apply_line_edit(e: LineEdit) -> void:
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(PAPER_WARM, 0.9)
	sb.border_color = Color(INK, 0.45)
	sb.border_width_bottom = 1
	sb.content_margin_left = 8
	sb.content_margin_right = 8
	sb.content_margin_top = 7
	sb.content_margin_bottom = 7
	e.add_theme_stylebox_override("normal", sb)
	e.add_theme_stylebox_override("focus", sb)
	e.add_theme_color_override("font_color", INK)
	e.add_theme_color_override("font_placeholder_color", Color(INK_SOFT, 0.85))
	e.add_theme_color_override("caret_color", CINNABAR)

## 下拉框：纸签 + 墨线（与按钮同一套语言）
static func apply_option(o: OptionButton) -> void:
	o.add_theme_font_size_override("font_size", 13)
	o.add_theme_stylebox_override("normal", _btn_box(PAPER_WARM, 0.45, false))
	o.add_theme_stylebox_override("hover", _btn_box(PAPER_TONE, 0.85, false))
	o.add_theme_stylebox_override("pressed", _btn_box(PAPER_TONE, 0.85, false))
	o.add_theme_stylebox_override("focus", _btn_box(Color(0, 0, 0, 0), 0.0, false))
	o.add_theme_color_override("font_color", INK)
	o.add_theme_color_override("font_hover_color", CINNABAR)

# ================= 组装 =================

## 根主题：没被单独覆盖的控件（Label / Button / LineEdit …）也吃到这套皮
static func build_theme(f: Font) -> Theme:
	var th := Theme.new()
	th.default_font = f
	th.default_font_size = 14
	th.set_color("font_color", "Label", INK)
	th.set_color("font_color", "Button", INK)
	th.set_color("font_color", "CheckBox", INK)
	th.set_color("font_color", "OptionButton", INK)
	th.set_color("font_color", "LineEdit", INK)
	th.set_stylebox("panel", "PanelContainer", panel(10))
	th.set_stylebox("normal", "Button", _btn_box(PAPER_WARM, 0.45, false))
	th.set_stylebox("hover", "Button", _btn_box(PAPER_TONE, 0.85, false))
	th.set_stylebox("pressed", "Button", _btn_box(DEEP, 0.9, true))
	th.set_stylebox("normal", "OptionButton", _btn_box(PAPER_WARM, 0.45, false))
	th.set_stylebox("hover", "OptionButton", _btn_box(PAPER_TONE, 0.85, false))
	th.set_stylebox("pressed", "OptionButton", _btn_box(PAPER_TONE, 0.85, false))
	th.set_stylebox("focus", "OptionButton", _btn_box(Color(0, 0, 0, 0), 0.0, false))
	th.set_color("font_pressed_color", "OptionButton", CINNABAR)
	th.set_stylebox("normal", "LineEdit", _input_box())
	th.set_stylebox("focus", "LineEdit", _input_box())
	th.set_color("font_placeholder_color", "LineEdit", Color(INK_SOFT, 0.85))
	th.set_color("caret_color", "LineEdit", CINNABAR)
	# 滑杆：墨色细线 + 朱砂已选段 + 纸面圆点（默认的灰蓝滑块与宣纸不搭）
	var track := StyleBoxFlat.new()
	track.bg_color = Color(INK, 0.28)
	track.content_margin_top = 1
	track.content_margin_bottom = 1
	th.set_stylebox("slider", "HSlider", track)
	th.set_stylebox("grabber_area", "HSlider", _bar(CINNABAR))
	th.set_stylebox("grabber_area_highlight", "HSlider", _bar(CINNABAR))
	th.set_icon("grabber", "HSlider", GRABBER_TEX)
	th.set_icon("grabber_highlight", "HSlider", GRABBER_TEX)
	# 复选框：墨线方框 / 朱砂勾
	th.set_icon("unchecked", "CheckBox", CHECK_OFF_TEX)
	th.set_icon("checked", "CheckBox", CHECK_ON_TEX)
	# 勾选态在 Godot 里走 Button 的 "pressed" 样式 ⇒ 会继承成深色块把字吞掉：显式清空
	for st in ["normal", "pressed", "hover", "disabled", "focus"]:
		th.set_stylebox(st, "CheckBox", _empty_box())
	for st in ["font_color", "font_pressed_color", "font_hover_color", "font_focus_color", "font_disabled_color"]:
		th.set_color(st, "CheckBox", INK)
	# 滚动条：极细墨线（默认那根灰条在纸上太抢眼）
	th.set_stylebox("scroll", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("scroll_focus", "VScrollBar", _scroll_grabber(Color(0, 0, 0, 0), 3))
	th.set_stylebox("grabber", "VScrollBar", _scroll_grabber(Color(INK, 0.28), 3))
	th.set_stylebox("grabber_highlight", "VScrollBar", _scroll_grabber(Color(CINNABAR, 0.50), 3))
	th.set_stylebox("grabber_pressed", "VScrollBar", _scroll_grabber(Color(CINNABAR, 0.70), 3))
	return th

## 空样式（复选框 / 只留图标的地方用）
static func _empty_box() -> StyleBoxEmpty:
	return StyleBoxEmpty.new()

## 滚动条：一条 6px 宽的细条（内容边距决定宽度）
static func _scroll_grabber(col: Color, half := 3) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = col
	sb.set_corner_radius_all(half)
	sb.content_margin_left = half
	sb.content_margin_right = half
	return sb

## 滑杆「已选」那一段：一条朱砂细条
static func _bar(col: Color) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = col
	sb.content_margin_top = 1
	sb.content_margin_bottom = 1
	return sb

## 输入框底：纸面 + 一条底部墨线（无框无圆角）
static func _input_box() -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(PAPER_WARM, 0.92)
	sb.border_color = Color(INK, 0.45)
	sb.border_width_bottom = 1
	sb.content_margin_left = 8
	sb.content_margin_right = 8
	sb.content_margin_top = 7
	sb.content_margin_bottom = 7
	return sb
