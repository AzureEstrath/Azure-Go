class_name ModeTransition
extends CanvasLayer
## 桌宠 ⇄ 对弈的纸张转场（v4.6.0）。
##
## 两个独立窗口之间没法做真正的窗口动画，但可以用一张「纸」盖住切换：
## 桌宠先淡成纸 → 新窗口启动时同样是纸 → 纸淡开露出棋盘 / 桌宠。
## 只负责视觉覆盖，不碰窗口、服务或 busy；纯时间曲线 `alpha_at()` 可单测。

const COVER_SEC := 0.22
const REVEAL_SEC := 0.28
const LABEL_ALPHA := 0.38

signal finished

var _rect: ColorRect
var _label: Label
var _covering := false
var _active := false
var _elapsed := 0.0
var _duration := COVER_SEC

func _ready() -> void:
	layer = 90
	_rect = ColorRect.new()
	_rect.color = Color(GufengTheme.PAPER, 0.0)
	_rect.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_rect.mouse_filter = Control.MOUSE_FILTER_STOP
	add_child(_rect)
	_label = Label.new()
	_label.text = "Azure"
	_label.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	_label.add_theme_font_override("font", GufengTheme.font())
	_label.add_theme_font_size_override("font_size", 14)
	_label.add_theme_color_override("font_color", Color(GufengTheme.INK, 0.0))
	_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_label)
	visible = false
	set_process(false)

func _process(delta: float) -> void:
	if not _active:
		return
	_elapsed += delta
	var a := alpha_at(_elapsed, _duration, _covering)
	_rect.color = Color(GufengTheme.PAPER, a)
	_label.add_theme_color_override("font_color", Color(GufengTheme.INK, a * LABEL_ALPHA))
	if _elapsed >= _duration:
		_active = false
		set_process(false)
		visible = _covering
		_rect.mouse_filter = Control.MOUSE_FILTER_STOP if _covering else Control.MOUSE_FILTER_IGNORE
		finished.emit()

## 纯函数：覆盖 / 揭开到某一时刻的不透明度（smoothstep，端点严格 0 / 1）。
static func alpha_at(elapsed: float, duration: float, covering: bool) -> float:
	if duration <= 0.0:
		return 1.0 if covering else 0.0
	var t := clampf(float(elapsed) / float(duration), 0.0, 1.0)
	t = t * t * (3.0 - 2.0 * t)
	return t if covering else 1.0 - t

func busy() -> bool:
	return _active

## 立即盖满（新窗口第一帧就用纸挡住，避免白闪）。
func cover_now() -> void:
	_active = false
	set_process(false)
	_covering = true
	visible = true
	_rect.mouse_filter = Control.MOUSE_FILTER_STOP
	_rect.color = Color(GufengTheme.PAPER, 1.0)
	_label.add_theme_color_override("font_color", Color(GufengTheme.INK, LABEL_ALPHA))

func cover() -> void:
	if _active:
		await finished
	_covering = true
	_duration = COVER_SEC
	_elapsed = 0.0
	_active = true
	visible = true
	_rect.mouse_filter = Control.MOUSE_FILTER_STOP
	_rect.color = Color(GufengTheme.PAPER, 0.0)
	_label.add_theme_color_override("font_color", Color(GufengTheme.INK, 0.0))
	set_process(true)
	await finished

func reveal() -> void:
	if _active:
		await finished
	_covering = false
	_duration = REVEAL_SEC
	_elapsed = 0.0
	_active = true
	visible = true
	_rect.mouse_filter = Control.MOUSE_FILTER_STOP
	_rect.color = Color(GufengTheme.PAPER, 1.0)
	_label.add_theme_color_override("font_color", Color(GufengTheme.INK, LABEL_ALPHA))
	set_process(true)
	await finished
