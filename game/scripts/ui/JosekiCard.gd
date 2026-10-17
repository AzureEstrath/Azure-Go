class_name JosekiCard
extends Control
## 定式参考图卡片：浮在 3D 棋盘上方的小棋盘浮层（2D Control，不进任何侧栏）。
## 只**渲染**后端 `joseki` 事件给的字段（name / line / k / candidates / mistake），
## 本文件不做任何定式识别、编排或局面重放——后端是唯一权威。
##
## 五条硬约束（任务书 07-T8 的 U13 / U38 / U39 / U40）：
##   · 五个公开方法（apply_event / set_panel_overlap / clear / state / stone_count）一律不依赖场景树：
##     headless 门卫 `JosekiCard.new()` 就能直接驱动。缺省值写在**成员声明处**，不放 `_ready()`。
##   · `_draw` / `_gui_input` / `_process` 只在 `is_inside_tree()` 为真时工作；树外只更新纯数据。
##   · 卡片不产生任何台词 / `speech`，不碰 `message`。
##   · 非法坐标（越界 / 非数字 / 缺键）不画、不计数、不抛；`pass` 一律忽略。
##   · TTL 兜底走**可注入**的 `_tick(delta)`：后端不识别「脱先」（不发收尾事件），久无新事件就自己收起；
##     而收尾**只有** `formed`（库线走完）一种，`deviated` 是「走错」→ 照常上卡片并把那一手标红（U45）。

const CARD_TTL_SEC := 120.0   # 兜底 TTL：距最后一次 apply_event 超过这么久仍没新事件 → 自动收起（U38）
const CARD_W := 200.0         # 卡片宽（含内边距），Main 的默认落位与避让几何都用它
const CARD_H := 222.0         # 卡片高：上边距 + 标题 + 小棋盘 + 下边距
const PAD := 14.0             # 卡片内边距
const HEAD_H := 22.0          # 标题占高
const GRID := 19              # 小棋盘路数：后端坐标就是 19 路
const FADED_ALPHA := 0.28     # 避让时的透明度（半透明：还看得见，但不挡事）

const BG := Color(0.07, 0.16, 0.18, 0.72)          # 半透明深色底（不切断棋盘视线）
const BORDER := Color(0.306, 0.804, 0.769, 0.85)   # 与主界面同一套青蓝
const GRID_LINE := Color(0.83, 0.91, 0.90, 0.55)
const STONE_B := Color(0.10, 0.11, 0.12)
const STONE_W := Color(0.97, 0.97, 0.95)
const RING := Color(0.31, 0.96, 0.90)              # 第 k 手的高亮环
const CAND := Color(1.0, 0.72, 0.20)               # 候选点记号（菱形，与高亮环区分开）
const CAND_FG := Color(0.12, 0.10, 0.03)           # 候选点序号（画在菱形上）
const BAD := Color(0.95, 0.30, 0.24)               # 走错那一手
const TITLE_FG := Color(0.90, 0.98, 0.97)
const INVALID := Vector2i(-1, -1)                  # 非法坐标哨兵（棋盘坐标为 0..18，不会撞上）

## —— 状态机（数组/缺省一律写在成员声明处：门卫 `.new()` 后即可用）——
var _active := false           # 有没有卡片内容：apply_event 置真，clear / 兜底 TTL 置假
var _faded := false            # 避让：被喂进来的矩形压住时淡出
var _since_event := 0.0        # 距上次 apply_event 的秒数，只由 _tick 推进（门卫注入，不真 sleep）

## —— 要画的字段（全部原样来自后端事件，卡片不加工）——
var _title := ""               # 课程名（事件的 name）
var _line: Array = []          # 手顺：[{x, y, c}]，c = "B" / "W"
var _k := -1                   # 第 k 手（0 基）高亮；缺键/脏值/越界 = -1（不高亮）
var _candidates: Array = []    # 分岔候选：[{name, next: [x, y]}]
var _mistake = null            # 走错那一手 {move_no, x, y, drop}，否则 null

var _dragging := false         # 左键按住拖动中

func _init() -> void:
	# 树外也成立的属性：门卫不看场景树（U13）。鼠标只在自己矩形内被吃掉（U40）
	mouse_filter = Control.MOUSE_FILTER_STOP
	mouse_default_cursor_shape = Control.CURSOR_DRAG
	tooltip_text = "定式参考图（可拖动）"
	size = Vector2(CARD_W, CARD_H)

func _ready() -> void:
	set_process(true)                                       # 兜底 TTL 每帧推进（树外由门卫直接调 _tick）
	_sync_visual()

func _process(delta: float) -> void:
	_tick(delta)

# ================= 公开接口（五个，全部树外可调） =================

## 后端 `joseki` 事件 → 卡片。缺字段、错类型、脏值一律安全缺省、忽略未知键，**绝不抛**（旧后端混跑不崩）。
func apply_event(e: Dictionary) -> void:
	if typeof(e) != TYPE_DICTIONARY:
		return
	var kind := str(e.get("kind", ""))
	# 收尾**只有**库线走完（`formed`）这一种：立即收起，不等兜底 TTL（U38）。
	# **偏离库线（`deviated`）不是收尾**（U45）：它是「走错」，而 `mistake` 只在真走错时才非 null——
	# 「第 4 次起只在真走错时才上卡片」正是靠这种事件上卡片，所以它走正常路径
	# （`show_card` 门禁 → 渲染 → 把走错那一手标红），绝不在这里 clear。
	if kind == "formed":
		clear()
		return
	# show_card：缺失＝true（旧后端不带该字段）；显式 false＝这一手不上卡片（U7）
	if not _truthy(e.get("show_card", true)):
		clear()
		return
	# 换族 / 换角：直接替换成新的一张，不做叠卡（U38）
	_title = str(e.get("name", ""))
	_line = e["line"] if typeof(e.get("line", null)) == TYPE_ARRAY else []
	_candidates = e["candidates"] if typeof(e.get("candidates", null)) == TYPE_ARRAY else []
	_k = int(e["k"]) if _is_int_ish(e.get("k", null)) else -1
	var mk = e.get("mistake", null)
	_mistake = mk if typeof(mk) == TYPE_DICTIONARY else null
	_active = true
	_since_event = 0.0            # 新事件：兜底 TTL 从头计时（中段推进不会被误判成收尾）
	# 注意：不动 `_faded`——避让结论交给下一次 set_panel_overlap，避免「压在棋盘下时闪一帧全不透明」
	_sync_visual()

## 避让：与给定矩形（Main 每帧算出的「棋盘落点热区」+「鼠标附近的小方块」+「展开的侧栏」）重叠时淡出。
## 卡片只认矩形，几何计算在 Main.gd（U39）。收起状态喂矩形**不唤醒**（U40）。
## 参数刻意用**无类型 `Array`**（与仓库惯例一致）：本文件的契约是「脏输入绝不抛」，
## 写成 `Array[Rect2]` 会让任何无类型调用方直接吃运行时类型错误；非 Rect2 项跳过即可（M5）。
func set_panel_overlap(rects: Array) -> void:
	if not _active:
		return
	var mine := card_rect()
	for r in rects:
		if typeof(r) != TYPE_RECT2:
			continue
		if (r as Rect2).intersects(mine):
			_set_faded(true)
			return
	_set_faded(false)

## 收起：清数据 + 藏卡片（`formed` 收尾、`show_card=false`、兜底 TTL 都走这里）
func clear() -> void:
	_active = false
	_faded = false
	_since_event = 0.0
	_title = ""
	_line = []
	_k = -1
	_candidates = []
	_mistake = null
	_sync_visual()

## 纯逻辑状态（供门卫断言）："hidden" 收起 / "faded" 避让中半透明 / "shown" 正常显示
func state() -> String:
	if not _active:
		return "hidden"
	return "faded" if _faded else "shown"

## 当前画得出来的合法棋子数（非法坐标不计；供门卫断言）
func stone_count() -> int:
	var n := 0
	for it in _line:
		if _pos_of(it) != INVALID:
			n += 1
	return n

## 卡片自己的屏幕矩形：树内用真实布局；树外（门卫）退回「常量尺寸 + 当前位置」，保证可判且确定
func card_rect() -> Rect2:
	if is_inside_tree() and size.x > 0.0 and size.y > 0.0:
		return Rect2(global_position, size)
	return Rect2(position, Vector2(CARD_W, CARD_H))

## 兜底 TTL 推进（秒）。时间由调用方注入——树内是 `_process`，门卫直接调它，**不需要真 sleep**（U38）。
func _tick(delta: float) -> void:
	if not _active:
		return
	_since_event += delta
	if _since_event > CARD_TTL_SEC:
		clear()

# ================= 纯数据辅助（不碰场景树） =================

## 数字判据：NetClient 走 `JSON.parse_string`，整数到了前端是 float，所以 x.0 要认；
## 字符串、null、1.5 这类一律不算（U40「非数字不画」）。
func _is_int_ish(v) -> bool:
	var t := typeof(v)
	if t == TYPE_INT:
		return true
	if t == TYPE_FLOAT:
		return is_finite(float(v)) and float(v) == floor(float(v))
	return false

## 坐标换算：非字典 / 缺键 / 非整数 / 越界 → INVALID（不画、不计数、不抛）
func _pos_of(v) -> Vector2i:
	if typeof(v) != TYPE_DICTIONARY:
		return INVALID
	var d: Dictionary = v
	if not _is_int_ish(d.get("x", null)) or not _is_int_ish(d.get("y", null)):
		return INVALID
	return _pos_clamped(int(d["x"]), int(d["y"]))

## 候选点的 `next` 是坐标对 `[x, y]`（后端原样透传）；也容忍 `{x, y}` 形态
func _pos_of_next(v) -> Vector2i:
	if typeof(v) == TYPE_ARRAY:
		var a: Array = v
		if a.size() < 2 or not _is_int_ish(a[0]) or not _is_int_ish(a[1]):
			return INVALID
		return _pos_clamped(int(a[0]), int(a[1]))
	return _pos_of(v)

func _pos_clamped(x: int, y: int) -> Vector2i:
	if x < 0 or y < 0 or x >= GRID or y >= GRID:
		return INVALID
	return Vector2i(x, y)

## show_card 取值：缺省 true；后端发的是 JSON 布尔，字符串 "false"/"0" 也按字面认
func _truthy(v) -> bool:
	if typeof(v) == TYPE_STRING:
		var s := str(v).strip_edges().to_lower()
		return s != "" and s != "false" and s != "0"
	return bool(v)

## 视觉副作用只在本函数发生，且只在树内——树外（门卫）只更新纯数据（U13）
func _sync_visual() -> void:
	if not is_inside_tree():
		return
	visible = _active
	modulate.a = FADED_ALPHA if _faded else 1.0
	queue_redraw()

func _set_faded(v: bool) -> void:
	if _faded == v:
		return
	_faded = v
	_sync_visual()

## 第 k 手要画高亮环的那一格：`k` 越界 / 脏值 / 该手坐标非法 → 哨兵（`_draw` 与门卫共用同一个判据）。
## `_draw` 在 headless 门卫里永远跑不到，所以这条判据必须抽出来才测得到（M4）。
func _k_cell() -> Vector2i:
	if _k < 0 or _k >= _line.size():
		return INVALID
	return _pos_of(_line[_k])

## 每个候选点要画记号的那一格（非法坐标的候选直接跳过，也**不占**记号序号）
func _candidate_cells() -> Array[Vector2i]:
	var out: Array[Vector2i] = []
	for cand in _candidates:
		if typeof(cand) != TYPE_DICTIONARY:
			continue
		var p := _pos_of_next((cand as Dictionary).get("next", null))
		if p != INVALID:
			out.append(p)
	return out

## 走错那一手要标红的那一格：没有 `mistake` / 坐标非法 → 哨兵
func _mistake_cell() -> Vector2i:
	if _mistake == null:
		return INVALID
	return _pos_of(_mistake)

# ================= 绘制（不推演出任何后端没给的结论） =================

func _draw() -> void:
	var whole := Rect2(Vector2.ZERO, Vector2(CARD_W, CARD_H))
	draw_rect(whole, BG, true)
	draw_rect(whole, BORDER, false, 1.0)

	var f := get_theme_default_font()
	var step := _grid_step()
	var x0 := PAD
	var y0 := PAD + HEAD_H
	if f != null:
		# 标题用后端给的课名；没有就给个中性占位（卡片不自己编台词）
		draw_string(f, Vector2(x0, PAD + 12.0), _title if _title != "" else "定式参考",
				HORIZONTAL_ALIGNMENT_LEFT, CARD_W - PAD * 2.0, 12, TITLE_FG)

	# 小棋盘网格（19 路）
	var side := step * float(GRID - 1)
	for i in GRID:
		var t := float(i) * step
		draw_line(Vector2(x0 + t, y0), Vector2(x0 + t, y0 + side), GRID_LINE, 1.0)
		draw_line(Vector2(x0, y0 + t), Vector2(x0 + side, y0 + t), GRID_LINE, 1.0)

	# 手顺着子：黑 B / 白 W（c 不认识按黑画，不抛）
	var r := step * 0.42
	for i in _line.size():
		var p := _pos_of(_line[i])
		if p == INVALID:
			continue
		draw_circle(_cell_pos(p), r, _stone_color(_line[i]))

	# 第 k 手高亮环（判据在 `_k_cell()`：越界 / 脏值 / 坐标非法一律不高亮也不抛）
	var pk := _k_cell()
	if pk != INVALID:
		draw_arc(_cell_pos(pk), r + 3.0, 0.0, TAU, 24, RING, 2.0)

	# 候选点：各自 next 位置画菱形 + 序号（与高亮环形状不同，一眼分得开）
	var cells := _candidate_cells()
	for i in cells.size():
		var q := _cell_pos(cells[i])
		var d := r * 0.95
		draw_colored_polygon(PackedVector2Array([q + Vector2(0.0, -d), q + Vector2(d, 0.0),
				q + Vector2(0.0, d), q + Vector2(-d, 0.0)]), CAND)
		if f != null:
			draw_string(f, q + Vector2(-2.5, 3.0), str(i + 1),
					HORIZONTAL_ALIGNMENT_LEFT, 0.0, 9, CAND_FG)

	# 走错那一手：坐标匹配才标红（后端只在真是走错时才给 mistake）
	var pm := _mistake_cell()
	if pm != INVALID:
		var qm := _cell_pos(pm)
		draw_arc(qm, r + 4.0, 0.0, TAU, 24, BAD, 2.0)
		draw_line(qm - Vector2(r, r), qm + Vector2(r, r), BAD, 2.0)
		draw_line(qm - Vector2(r, -r), qm + Vector2(r, -r), BAD, 2.0)

func _grid_step() -> float:
	return (CARD_W - PAD * 2.0) / float(GRID - 1)

func _cell_pos(p: Vector2i) -> Vector2:
	var s := _grid_step()
	return Vector2(PAD + float(p.x) * s, PAD + HEAD_H + float(p.y) * s)

func _stone_color(it) -> Color:
	if typeof(it) == TYPE_DICTIONARY and str((it as Dictionary).get("c", "")).to_upper().begins_with("W"):
		return STONE_W
	return STONE_B

# ================= 交互：可拖动（只在自己矩形内吃鼠标） =================

## 拖动一步（纯逻辑，门卫可直接调）：
##   · 没在拖动 → 不动（第一次裸移动不能把卡片带走）
##   · `left_pressed` 为假 → 解除拖动标记、不动：**松键事件未必递得到卡片**（在卡片外松的），
##     所以以真实键位为准，否则下一次裸移动会把卡片悄悄拖走（M2）
func _drag_step(rel: Vector2, left_pressed: bool) -> void:
	if not _dragging:
		return
	if not left_pressed:
		_dragging = false
		return
	position += rel
	if is_inside_tree():                       # 树外（门卫）拿不到视口，只推进位置
		position = _clamp_pos(position, get_viewport_rect().size)

## 位置限制到窗口内：至少留 48px 可见，拖出去就再也找不回来的卡片等于丢了
func _clamp_pos(p: Vector2, vp: Vector2) -> Vector2:
	return p.clamp(Vector2(48.0 - CARD_W, 0.0), Vector2(vp.x - 48.0, vp.y - 48.0))

func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			_dragging = mb.pressed
			accept_event()
	elif event is InputEventMouseMotion:
		if not _dragging:
			return
		_drag_step((event as InputEventMouseMotion).relative,
				Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT))
		accept_event()
