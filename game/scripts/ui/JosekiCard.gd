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

const CARD_TTL_SEC := 60.0    # 兜底 TTL：距最后一次 apply_event 超过这么久仍没新事件 → 自动收起（U38）
# 尺寸按「默认布局下能完整落在棋盘上方」定：1280×800、侧栏展开时棋盘顶在 y≈323，
# 卡片高必须 ≤ 299 才不会一出生就压棋盘（320 高时上沿会跑到 -9，只能退回兜底位）。
const CARD_W := 236.0         # 卡片宽（含内边距），Main 的默认落位与避让几何都用它
const CARD_H := 288.0         # 卡片高：头部 + 小棋盘 + 候选图例 + 内边距
const PAD := 14.0             # 卡片内边距
const HEAD_H := 34.0          # 头部占高（课名 / 结构点徽标 / 关闭）
const GRID_GAP := 6.0         # 头部与棋盘之间的留白
const TITLE_SIZE := 15        # 标题字号（12px 在 1280×800 下读不清）
const CLOSE_SIZE := 22.0      # 关闭热区边长（自绘 ×，不建子节点：五条不变量都靠 `_draw`）
const GRID := 19              # 小棋盘路数：后端坐标就是 19 路
# 角内取景窗口：定式只看一个角，19 路全盘会把 3~8 颗子摊成一片；按角截 9 路放大。
# 实测 25 条库线最宽只用 8 路，9 路窗口装得下全部；**任何一点（手顺 / 候选 / 走错）落在窗外就退回
# 19 路全盘**——参考图绝不裁子，也绝不把候选点画到画外。
const WINDOW := 9
# 避让时**只把外壳**（底色 / 网格 / 边框）压到这个系数：整卡一起压透明会把文字连同棋子糊成一片
# 灰影（v3.9.2 修复的正是这个——参考图是要读的，不是要藏的）。
const FADED_SHELL := 0.40

const BG := Color(0.07, 0.16, 0.18, 1.0)           # **不透明**深色底（v3.9.6：半透明底压在浅色棋盘上发灰，
#                                                    参考图要的是"一块实心黑板"，不是玻璃）
const BORDER := Color(0.306, 0.804, 0.769, 0.85)   # 与主界面同一套青蓝
const GRID_LINE := Color(0.83, 0.91, 0.90, 0.70)
# 结构点徽标：颜色 + 文案（`kind` 是后端给的四个值之一）
const BADGE_COLORS := {
	"entered": Color(0.31, 0.96, 0.90),
	"branch": Color(1.0, 0.72, 0.20),
	"deviated": Color(0.95, 0.30, 0.24),
	"formed": Color(0.75, 0.82, 0.82),
}
const BADGE_TEXT := {"entered": "进入", "branch": "分岔", "deviated": "走错", "formed": "收尾"}
const BADGE_W := 46.0         # 徽标胶囊宽（两个汉字 + 内边距）
const HOSHI := [Vector2i(3, 3), Vector2i(3, 9), Vector2i(3, 15), Vector2i(9, 3),
		Vector2i(9, 9), Vector2i(9, 15), Vector2i(15, 3), Vector2i(15, 9), Vector2i(15, 15)]
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
var _dim := 1.0                # 外壳亮度系数：1.0 = 正常，避让时 = FADED_SHELL（**不动整卡 modulate**）
var _since_event := 0.0        # 距上次 apply_event 的秒数，只由 _tick 推进（门卫注入，不真 sleep）

## —— 要画的字段（全部原样来自后端事件，卡片不加工）——
var _title := ""               # 课程名（事件的 name）
var _kind := ""                # 结构点（entered / branch / deviated / formed）：只用于头部徽标
var _corner := ""              # 角名（左上 / 右上 / 左下 / 右下）：决定 13 路取景窗口
var _line: Array = []          # 手顺：[{x, y, c}]，c = "B" / "W"
var _k := -1                   # 第 k 手（0 基）高亮；缺键/脏值/越界 = -1（不高亮）
var _candidates: Array = []    # 分岔候选：[{name, next: [x, y]}]
var _mistake = null            # 走错那一手 {move_no, x, y, drop}，否则 null

## 玩家手关过的「同一张」：同 (name, kind) 再来事件时不再自动弹（换结构点/换族照常弹）
var _dismissed_name := ""
var _dismissed_kind := ""

var _dragging := false         # 左键按住拖动中
var _hover_close := false      # 鼠标悬在关闭热区上（只影响画法，不动逻辑）

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
	# 玩家手关过这一张（同 name + kind）：不再自动弹回来；换结构点 / 换族才清掉这份记忆
	var nm := str(e.get("name", ""))
	if _dismissed_kind != "" and kind == _dismissed_kind and nm == _dismissed_name:
		return
	_dismissed_kind = ""
	_dismissed_name = ""
	# 换族 / 换角：直接替换成新的一张，不做叠卡（U38）
	_title = nm
	_kind = kind
	_corner = str(e.get("corner", ""))
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
	_dim = 1.0
	_since_event = 0.0
	_title = ""
	_kind = ""
	_corner = ""
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

## 外壳亮度系数（纯逻辑、树外可读）：1.0 = 正常，避让时 = FADED_SHELL。
## 只作用于底色 / 网格 / 边框；标题与棋子**恒为不透明**（淡出不许把参考图变成灰影）。
func dim() -> float:
	return _dim

## 关闭热区（卡片右上角，自绘 ×）：纯几何，树外可断言。
func close_rect() -> Rect2:
	return Rect2(CARD_W - PAD - CLOSE_SIZE, (HEAD_H - CLOSE_SIZE) * 0.5, CLOSE_SIZE, CLOSE_SIZE)

## 坐标是否落在关闭热区里（`_gui_input` 与门卫共用同一判据）
func hit_close(p: Vector2) -> bool:
	return close_rect().has_point(p)

## 关掉这张卡，并记住「同一张别再自动弹」：同 (name, kind) 再来事件时直接忽略。
## 换了结构点 / 换了定式（name 不同）⇒ 照常弹，不用玩家再点一次关闭。
func dismiss() -> void:
	_dismissed_name = _title
	_dismissed_kind = _kind
	clear()

## 头部徽标文案（纯逻辑）：进入 / 分岔 / 走错 / 收尾；不认识的 kind → 空串（不画徽标）
func badge_text() -> String:
	return str(BADGE_TEXT.get(_kind, ""))

## 候选图例（纯逻辑）：只收有名字的候选，非字典 / 空名一律跳过（脏值不抛）
func legend_items() -> Array:
	var out: Array = []
	for cand in _candidates:
		if typeof(cand) != TYPE_DICTIONARY:
			continue
		var nm := str((cand as Dictionary).get("name", "")).strip_edges()
		if nm != "":
			out.append(nm)
	return out

## 角内取景窗口：`{origin: Vector2i, size: int}`。按 `_corner` 取 9 路局部放大；
## **手顺 / 候选 / 走错任何一点落在窗口外就退回 19 路全盘**（参考图绝不裁子，也不把候选画到画外）；
## 角名不认识也退回全盘。
func view_window() -> Dictionary:
	var size := WINDOW
	var origin := Vector2i.ZERO
	match _corner:
		"右上": origin = Vector2i(GRID - size, 0)
		"左下": origin = Vector2i(0, GRID - size)
		"右下": origin = Vector2i(GRID - size, GRID - size)
		"左上": origin = Vector2i.ZERO
		_: return {"origin": Vector2i.ZERO, "size": GRID}
	var pts: Array[Vector2i] = []
	for it in _line:
		pts.append(_pos_of(it))
	for cand in _candidates:
		if typeof(cand) == TYPE_DICTIONARY:
			pts.append(_pos_of_next((cand as Dictionary).get("next", null)))
	if _mistake != null:
		pts.append(_pos_of(_mistake))
	for p in pts:
		if p == INVALID:
			continue
		if p.x < origin.x or p.y < origin.y or p.x >= origin.x + size or p.y >= origin.y + size:
			return {"origin": Vector2i.ZERO, "size": GRID}
	return {"origin": origin, "size": size}

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
	queue_redraw()                             # 不动 modulate：淡出只由 `_dim` 作用在外壳上

func _set_faded(v: bool) -> void:
	if _faded == v:
		return
	_faded = v
	_dim = FADED_SHELL if v else 1.0
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
	# 只有「壳」吃 dim：底色 / 边框 / 网格。文字与棋子一律原色（见 `dim()` 的注释）。
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(BG, BG.a * _dim)
	sb.border_color = Color(BORDER, BORDER.a * _dim)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(10)
	sb.shadow_color = Color(0.0, 0.0, 0.0, 0.22 * _dim)
	sb.shadow_size = 8
	sb.shadow_offset = Vector2(0, 3)
	draw_style_box(sb, whole)

	var f := get_theme_default_font()
	_draw_header(f)
	_draw_grid(f)
	_draw_legend(f)

## 头部：课名 + 结构点徽标 + 关闭 ×（自绘，不建子节点：五条不变量都靠 `_draw` 与纯函数）
func _draw_header(f: Font) -> void:
	var cr := close_rect()
	if f != null:
		var badge := badge_text()
		var badge_x := cr.position.x - 10.0 - BADGE_W
		var title_w := maxf(40.0, badge_x - 8.0 - PAD)
		# 标题用后端给的课名；没有就给个中性占位（卡片不自己编台词）
		draw_string(f, Vector2(PAD, HEAD_H * 0.5 + 5.0), _title if _title != "" else "定式参考",
				HORIZONTAL_ALIGNMENT_LEFT, title_w, TITLE_SIZE, TITLE_FG)
		if badge != "":
			var col: Color = BADGE_COLORS.get(_kind, Color(0.75, 0.82, 0.82))
			var pill := StyleBoxFlat.new()
			pill.bg_color = Color(col, 0.16)
			pill.border_color = Color(col, 0.70)
			pill.set_border_width_all(1)
			pill.set_corner_radius_all(10)
			var br := Rect2(badge_x, (HEAD_H - 20.0) * 0.5, BADGE_W, 20.0)
			draw_style_box(pill, br)
			draw_string(f, Vector2(br.position.x, br.position.y + 15.0), badge,
					HORIZONTAL_ALIGNMENT_CENTER, br.size.x, 12, col)
	# 关闭 ×：悬停时给一层浅底（光标形状在 `_gui_input` 里切）
	if _hover_close:
		var hsb := StyleBoxFlat.new()
		hsb.bg_color = Color(1.0, 1.0, 1.0, 0.16)
		hsb.set_corner_radius_all(6)
		draw_style_box(hsb, cr.grow(2.0))
	var c := cr.position + cr.size * 0.5
	var q := cr.size.x * 0.26
	var xc := Color(TITLE_FG, 0.95 if _hover_close else 0.70)
	draw_line(c + Vector2(-q, -q), c + Vector2(q, q), xc, 1.6)
	draw_line(c + Vector2(-q, q), c + Vector2(q, -q), xc, 1.6)
	draw_line(Vector2(PAD, HEAD_H), Vector2(CARD_W - PAD, HEAD_H), Color(BORDER, 0.35 * _dim), 1.0)

## 小棋盘：窗口由 `view_window()` 决定（角内 13 路 / 越界退回 19 路）
func _draw_grid(f: Font) -> void:
	var win := view_window()
	var n := int(win["size"])
	var o: Vector2i = win["origin"]
	var origin := _grid_origin()
	var side := CARD_W - PAD * 2.0
	var step := side / float(n - 1)
	var grid_fg := Color(GRID_LINE, GRID_LINE.a * _dim)
	for i in n:
		var t := float(i) * step
		draw_line(origin + Vector2(t, 0.0), origin + Vector2(t, side), grid_fg, 1.0)
		draw_line(origin + Vector2(0.0, t), origin + Vector2(side, t), grid_fg, 1.0)
	# 星位：只画落在窗口里的（13 路时通常剩 2~3 个，用来定方向）
	for h in HOSHI:
		var hp: Vector2i = h
		if hp.x < o.x or hp.y < o.y or hp.x >= o.x + n or hp.y >= o.y + n:
			continue
		draw_circle(_cell_pos(hp), maxf(1.6, step * 0.085),
				Color(GRID_LINE, GRID_LINE.a * 0.85 * _dim))

	# 手顺着子：黑 B / 白 W（c 不认识按黑画，不抛）；石子够大时叠手顺数字
	var r := step * 0.44
	for i in _line.size():
		var p := _pos_of(_line[i])
		if p == INVALID:
			continue
		var col := _stone_color(_line[i])
		var ctr := _cell_pos(p)
		draw_circle(ctr, r, col)
		draw_arc(ctr, r, 0.0, TAU, 20,
				Color(0.0, 0.0, 0.0, 0.30) if col == STONE_W else Color(1.0, 1.0, 1.0, 0.18), 1.0)
		# 19 路回退时 r≈5.6，写数字只会变成噪点：低于 7 就不写
		if f != null and r >= 7.0:
			var num := str(i + 1)
			var fs := clampi(int(round(step * 0.60)), 9, 12)
			var fg := Color(0.08, 0.10, 0.10) if col == STONE_W else Color(0.95, 0.98, 0.97)
			draw_string(f, ctr + Vector2(-float(fs) * 0.5 * float(num.length()), float(fs) * 0.36),
					num, HORIZONTAL_ALIGNMENT_LEFT, 0.0, fs, fg)

	# 第 k 手高亮环（判据在 `_k_cell()`：越界 / 脏值 / 坐标非法一律不高亮也不抛）
	var pk := _k_cell()
	if pk != INVALID:
		draw_arc(_cell_pos(pk), r + 3.0, 0.0, TAU, 24, RING, 2.0)

	# 候选点：画成**白子** + 琥珀外环（提示「这是待选的一手，不是已经落下的子」）+ 序号。
	# 退回 19 路时石子直径只有 ~10px，白子+数字糊成一团：那时只画一圈琥珀环，序号交给底部图例。
	var cells := _candidate_cells()
	for i in cells.size():
		var q := _cell_pos(cells[i])
		var rq := r * 0.92
		if rq >= 6.5:
			draw_circle(q, rq, STONE_W)
			draw_arc(q, rq, 0.0, TAU, 20, Color(0.0, 0.0, 0.0, 0.30), 1.0)
			draw_arc(q, rq + 1.8, 0.0, TAU, 24, CAND, 1.6)
			if f != null:
				var fs2 := clampi(int(round(step * 0.52)), 9, 12)
				draw_string(f, q + Vector2(-float(fs2) * 0.5, float(fs2) * 0.36), str(i + 1),
						HORIZONTAL_ALIGNMENT_LEFT, 0.0, fs2, CAND_FG)
		else:
			draw_arc(q, maxf(2.5, rq), 0.0, TAU, 16, CAND, 2.0)

	# 走错那一手：坐标匹配才标红（后端只在真是走错时才给 mistake）
	var pm := _mistake_cell()
	if pm != INVALID:
		var qm := _cell_pos(pm)
		draw_arc(qm, r + 4.0, 0.0, TAU, 24, BAD, 2.0)
		draw_line(qm - Vector2(r, r), qm + Vector2(r, r), BAD, 2.0)
		draw_line(qm - Vector2(r, -r), qm + Vector2(r, -r), BAD, 2.0)

## 底部候选图例：`① 一间跳  ② 贴住`（候选名后端一直在发，以前白扔了）
func _draw_legend(f: Font) -> void:
	if f == null:
		return
	var items := legend_items()
	if items.is_empty():
		return
	var fs := 12
	var y := CARD_H - 12.0
	var x := PAD
	var shown := 0
	for i in items.size():
		if shown >= 3:                              # 最多三条，多了用省略号收尾
			draw_string(f, Vector2(x, y), "…", HORIZONTAL_ALIGNMENT_LEFT, 0.0, fs,
					Color(TITLE_FG, 0.70))
			break
		var txt := "%d %s" % [i + 1, items[i]]
		var w := f.get_string_size(txt, HORIZONTAL_ALIGNMENT_LEFT, -1, fs).x
		if x + w > CARD_W - PAD:
			break
		draw_string(f, Vector2(x, y), txt, HORIZONTAL_ALIGNMENT_LEFT, 0.0, fs,
				Color(TITLE_FG, 0.88))
		x += w + 14.0
		shown += 1

func _grid_origin() -> Vector2:
	return Vector2(PAD, HEAD_H + GRID_GAP)

func _grid_step() -> float:
	var win := view_window()
	return (CARD_W - PAD * 2.0) / float(int(win["size"]) - 1)

func _cell_pos(p: Vector2i) -> Vector2:
	var win := view_window()
	var o: Vector2i = win["origin"]
	var s := _grid_step()
	return _grid_origin() + Vector2(float(p.x - o.x) * s, float(p.y - o.y) * s)

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
			if mb.pressed and hit_close(mb.position):   # 关闭优先于拖动：别让 × 变成「拖一下」
				_dragging = false
				dismiss()
				accept_event()
				return
			_dragging = mb.pressed
			accept_event()
	elif event is InputEventMouseMotion:
		var over := hit_close((event as InputEventMouseMotion).position)
		if over != _hover_close:
			_hover_close = over
			mouse_default_cursor_shape =	Control.CURSOR_POINTING_HAND if over else Control.CURSOR_DRAG
			queue_redraw()
		if not _dragging:
			return
		_drag_step((event as InputEventMouseMotion).relative,
				Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT))
		accept_event()
