class_name Board3D
extends Node3D
## 真实 3D 棋盘：棋盘面/侧厚/网格/星位/坐标、扁形棋子、射线拾取、右键拖拽旋转视角

signal intersection_clicked(x: int, y: int)

const CELL := 1.0
const HALF := 9.0
const STONE_R := 0.46
const STONE_H := 0.28          # 棋子总厚度：扁形（透镜状），约为半径的 0.6 倍
const PITCH_MIN := 0.26          # 约 15°
const PITCH_MAX := 1.536         # 约 88°
const ROT_SPEED := 0.007
const PITCH_SPEED := 0.005
const ZOOM_STEP := 0.9             # 每格滚轮的距离倍率
const DIST_MIN := 14.0
const DIST_MAX := 70.0
## 让棋盘在「窗口中心偏左」显示，为右侧面板留出空间
const PANEL_W := 380.0
## 注视点整体上移：为棋盘对面的 Azure 留出画面上方空间（开局即完整入画）
const VIEW_LIFT := 3.0
## 旋转视角结束后的注视锁定：先看玩家 1 秒，再交回鼠标跟随
const GAZE_LOCK_AFTER_ROTATE := 1.0
## 调试用：直接设定视角
func set_view(yaw_deg: float, pitch_deg: float, dist := 0.0) -> void:
	_yaw = deg_to_rad(yaw_deg)
	_pitch = clampf(deg_to_rad(pitch_deg), PITCH_MIN, PITCH_MAX)
	if dist > 0.0:
		_dist = clampf(dist, DIST_MIN, DIST_MAX)
	_update_camera()

var locked := false              # Azure 思考中时禁止落子
var idle_attention := false      # 玩家长时间无操作：化身转为看向玩家
var cam: Camera3D = null

var _stones_root: Node3D = null
var _ghost: MeshInstance3D = null
var _tip: Label3D = null
var _yaw := 0.0
var _pitch := deg_to_rad(38.0)   # 开局取景：平视偏俯，不做「俯视」视角
var _dist := 42.0
var _rotating := false
signal head_patted                # 右键按住不动：摸摸头
const PAT_HOLD_MS := 500          # 按住不动的判定时长（毫秒）
const PAT_DRAG_MAX := 12.0        # 摸头的净位移上限（px）：手抖来回抵消，拖拽旋转则是单向大位移
var _right_down_ms := 0           # 右键按下的时刻（判断「按住」）
var _pat_fired := false           # 本次按住是否已触发过摸头
var _pat_drag := 0.0              # 鼠标相对按下点的净位移（px）
var _pat_origin := Vector2.ZERO   # 右键按下的屏幕位置（净位移起点）
var _rotate_hold := 0.0          # 旋转结束后的注视锁定计时
var _look_point := Vector3.ZERO  # Azure 落子后的注视点
var _look_until := 0.0           # 落子注视的剩余时间
var _panel_left := false         # 左侧面板是否展开
var _panel_right := true         # 右侧面板是否展开
var _occupied := {}
var _displayed: Array = []       # 当前显示的棋子（权威 + 乐观）
var _optimistic: Array = []
var _hover := Vector2i(-1, -1)
var _font: Font = null
var _black_mat: StandardMaterial3D
var _white_mat: StandardMaterial3D
var _sphere: SphereMesh
var _dot_tex: ImageTexture

func _ready() -> void:
	_black_mat = StandardMaterial3D.new()
	_black_mat.albedo_color = Color(0.07, 0.07, 0.08)
	_black_mat.roughness = 0.22

	_white_mat = StandardMaterial3D.new()
	_white_mat.albedo_color = Color(0.95, 0.95, 0.91)
	_white_mat.roughness = 0.32

	# 扁形棋子：压扁的球体（上下两面外凸、腰部收边）＝ 真实围棋子的透镜轮廓
	_sphere = SphereMesh.new()
	_sphere.radius = STONE_R
	_sphere.height = STONE_H
	_sphere.radial_segments = 24
	_sphere.rings = 12

	_dot_tex = _make_dot_texture()
	_build_camera()
	_build_board()
	_stones_root = Node3D.new()
	add_child(_stones_root)
	_build_ghost()

## 生成一个小圆点贴图，供「最后一手」标记使用
func _make_dot_texture() -> ImageTexture:
	var s := 64
	var img := Image.create(s, s, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	var c := (s - 1) * 0.5
	var r := s * 0.40
	for y in s:
		for x in s:
			var d := Vector2(x - c, y - c).length()
			var a := clampf((r - d) / 1.5, 0.0, 1.0)      # 边缘羽化，避免锯齿
			if a > 0.0:
				img.set_pixel(x, y, Color(1, 1, 1, a))
	return ImageTexture.create_from_image(img)

func setup_font(f: Font) -> void:
	_font = f
	_tip.font = f

# ================= 建造 =================

func _build_camera() -> void:
	cam = Camera3D.new()
	cam.fov = 40.0
	cam.near = 0.5
	cam.far = 300.0
	add_child(cam)
	_update_camera()
	cam.make_current()

## 侧栏状态：展开的一侧会把棋盘从画面中心推开（单侧沿用既有取景，两侧都开时整体等比推远）
func set_panels(left_open: bool, right_open: bool) -> void:
	_panel_left = left_open
	_panel_right = right_open
	_update_camera()

func _update_camera() -> void:
	var vp := get_viewport().get_visible_rect().size
	var vh := vp.y
	var free_l := PANEL_W if _panel_left else 0.0
	var free_r := PANEL_W if _panel_right else 0.0
	var fit := 1.0
	if free_l + free_r > PANEL_W:
		fit = (vp.x - PANEL_W) / maxf(240.0, vp.x - free_l - free_r)
	var d := _dist * fit
	var cp := cos(_pitch)
	var eye := Vector3(d * cp * sin(_yaw), d * sin(_pitch), d * cp * cos(_yaw))
	# 相机与其注视点一起横向平移（不改变视线方向，只把棋盘推到没被侧栏挡住的一侧）
	var lateral := ((free_r - free_l) * 0.5) * d * tan(deg_to_rad(cam.fov * 0.5)) / (vh * 0.5)
	var right := Vector3(cos(_yaw), 0.0, -sin(_yaw))
	var target := right * lateral + Vector3(0.0, VIEW_LIFT, 0.0)
	eye += target
	cam.position = eye
	cam.look_at(target, Vector3.UP)

func _world(bx: int, by: int, h := 0.0) -> Vector3:
	return Vector3((bx - HALF) * CELL, h, (by - HALF) * CELL)

func _build_board() -> void:
	# 棋盘本体：顶面正好在 y = 0
	var box := BoxMesh.new()
	box.size = Vector3(19.0 * CELL, 0.6, 19.0 * CELL)
	var bmi := MeshInstance3D.new()
	bmi.mesh = box
	bmi.position = Vector3(0, -0.3, 0)
	var bmat := StandardMaterial3D.new()
	bmat.albedo_color = Color(0.804, 0.722, 0.541)
	bmat.roughness = 0.85
	bmi.material_override = bmat
	add_child(bmi)

	# 网格线（合成一个网格，双面材质省去朝向烦恼）
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	var w := 0.018
	var y := 0.012
	for i in 19:
		var c := (i - HALF) * CELL
		_quad(st, c - w, -HALF * CELL, c + w, HALF * CELL, y)      # 竖线
		_quad(st, -HALF * CELL, c - w, HALF * CELL, c + w, y)      # 横线
	st.generate_normals()
	var gmi := MeshInstance3D.new()
	gmi.mesh = st.commit()
	var gmat := StandardMaterial3D.new()
	gmat.albedo_color = Color(0.243, 0.180, 0.094)
	gmat.roughness = 1.0
	gmat.cull_mode = BaseMaterial3D.CULL_DISABLED
	gmi.material_override = gmat
	add_child(gmi)

	# 星位
	var star := CylinderMesh.new()
	star.top_radius = 0.075
	star.bottom_radius = 0.075
	star.height = 0.02
	var smat := StandardMaterial3D.new()
	smat.albedo_color = Color(0.196, 0.157, 0.086)
	for sx: int in [3, 9, 15]:
		for sy: int in [3, 9, 15]:
			var mi := MeshInstance3D.new()
			mi.mesh = star
			mi.position = _world(sx, sy, 0.015)
			mi.material_override = smat
			add_child(mi)

	_build_labels()

func _quad(st: SurfaceTool, x0: float, z0: float, x1: float, z1: float, y: float) -> void:
	var a := Vector3(x0, y, z0)
	var b := Vector3(x1, y, z0)
	var c := Vector3(x1, y, z1)
	var d := Vector3(x0, y, z1)
	st.add_vertex(a); st.add_vertex(c); st.add_vertex(b)
	st.add_vertex(a); st.add_vertex(d); st.add_vertex(c)

## 横竖坐标序号：列 A..T（跳过 I），行 19..1，随棋盘平躺
func _build_labels() -> void:
	for i in 19:
		var lt := Coords.letter(i)
		_label(lt, Vector3((i - HALF) * CELL, 0.02, -HALF * CELL - 0.95))
		_label(lt, Vector3((i - HALF) * CELL, 0.02, HALF * CELL + 0.95))
		var num := str(19 - i)
		_label(num, Vector3(-HALF * CELL - 0.95, 0.02, (i - HALF) * CELL))
		_label(num, Vector3(HALF * CELL + 0.95, 0.02, (i - HALF) * CELL))

func _label(text: String, pos: Vector3) -> void:
	var l := Label3D.new()
	l.text = text
	l.font_size = 64
	l.pixel_size = 0.008
	l.modulate = Color(0.24, 0.29, 0.24)
	l.rotation_degrees = Vector3(-90, 0, 0)
	l.position = pos
	add_child(l)

func _build_ghost() -> void:
	_ghost = MeshInstance3D.new()
	_ghost.mesh = _sphere
	var gmat := StandardMaterial3D.new()
	gmat.albedo_color = Color(0, 0, 0, 0.32)
	gmat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	_ghost.material_override = gmat
	_ghost.visible = false
	add_child(_ghost)

	_tip = Label3D.new()
	_tip.billboard = BaseMaterial3D.BILLBOARD_ENABLED
	_tip.font_size = 48
	_tip.pixel_size = 0.006
	_tip.modulate = Color(0.09, 0.42, 0.42)
	_tip.outline_size = 12
	_tip.no_depth_test = true
	_tip.visible = false
	add_child(_tip)

# ================= 棋子显示 =================

## 服务器权威棋盘
func set_stones(stones: Array) -> void:
	_optimistic.clear()
	_apply(stones)

## 立即落子的乐观棋子（提交前先显示，服务器返回后由 set_stones 覆盖）
func add_optimistic(x: int, y: int, color: String) -> void:
	_optimistic.append({"x": x, "y": y, "c": color})
	_apply(_displayed)

func _apply(authoritative: Array) -> void:
	for c in _stones_root.get_children():
		c.queue_free()
	_occupied.clear()
	_displayed = authoritative.duplicate()
	for s in authoritative:
		_occupied[Vector2i(int(s["x"]), int(s["y"]))] = true
	for s in _optimistic:
		_occupied[Vector2i(int(s["x"]), int(s["y"]))] = true
	for s in authoritative:
		_make_stone(int(s["x"]), int(s["y"]), str(s["c"]), bool(s.get("last", false)))
	for s in _optimistic:
		_make_stone(int(s["x"]), int(s["y"]), str(s["c"]), true)

func _make_stone(x: int, y: int, color: String, last: bool) -> void:
	var mi := MeshInstance3D.new()
	mi.mesh = _sphere
	mi.material_override = _black_mat if color == "black" else _white_mat
	mi.position = _world(x, y, STONE_H * 0.5)
	_stones_root.add_child(mi)
	if last:
		# 最后一手标记：始终正对玩家的圆点，不随棋盘视角转动
		var dot := Sprite3D.new()
		dot.texture = _dot_tex
		dot.billboard = BaseMaterial3D.BILLBOARD_ENABLED
		dot.pixel_size = 0.004
		dot.modulate = Color(0.306, 0.804, 0.769) if color == "black" else Color(0.906, 0.298, 0.235)
		dot.position = _world(x, y, STONE_H + 0.05)
		_stones_root.add_child(dot)

func is_occupied(x: int, y: int) -> bool:
	return _occupied.has(Vector2i(x, y))

# ================= 化身视线目标 =================

func _process(delta: float) -> void:
	if _rotate_hold > 0.0:
		_rotate_hold -= delta
	if _look_until > 0.0:
		_look_until -= delta
	# 右键按住不动 0.5 秒 → 摸头（相对按下点的移动超过阈值视为旋转视角，不触发）
	if _rotating and not _pat_fired and _pat_drag <= PAT_DRAG_MAX \
			and Time.get_ticks_msec() - _right_down_ms >= PAT_HOLD_MS:
		_pat_fired = true
		head_patted.emit()

## 玩家是否正在拖拽旋转视角
func is_rotating() -> bool:
	return _rotating

## 化身的注视点优先级：旋转视角（含结束后 1 秒）→ Azure 刚落的子 → 闲置看玩家 → 鼠标落点
func gaze_target() -> Vector3:
	if _rotating or _rotate_hold > 0.0:
		return _player_eye()
	if _look_until > 0.0:
		return _look_point
	if idle_attention:
		return _player_eye()
	var hit := _mouse_board_point(get_viewport().get_mouse_position())
	return hit if hit.is_finite() else _player_eye()

## Azure 落子后注视自己的落点（一小段时间后交还给鼠标跟随）
func look_at_board_point(p: Vector3, dur := 2.0) -> void:
	_look_point = p
	_look_until = dur

## 交叉点的世界坐标（棋子上方一点，供化身注视用）
func stone_world(x: int, y: int) -> Vector3:
	return _world(x, y, STONE_H + 0.2)

## 玩家所在方向（开局注视、旋转视角时注视都用它）
func player_eye() -> Vector3:
	return _player_eye()

## 玩家视角的大致位置（朝向相机、略低于观察点，视线更自然）
func _player_eye() -> Vector3:
	if cam == null:
		return Vector3(0, 0, 14)
	return cam.global_position - Vector3(0, 2.5, 0)

## 鼠标射线与棋盘平面的交点（限制在棋盘附近）；不可用时返回 Vector3.INF
func _mouse_board_point(mp: Vector2) -> Vector3:
	if cam == null:
		return Vector3.INF
	var from := cam.project_ray_origin(mp)
	var dir := cam.project_ray_normal(mp)
	if absf(dir.y) < 1e-6:
		return Vector3.INF
	var t := -from.y / dir.y
	if t <= 0.0:
		return Vector3.INF
	var hit := from + dir * t
	if absf(hit.x) > 24.0 or hit.z < -26.0 or hit.z > 24.0:
		return Vector3.INF
	return Vector3(clampf(hit.x, -12.0, 12.0), 0.0, clampf(hit.z, -13.0, 12.0))

# ================= 拾取与交互 =================

## 屏幕坐标 → 最近的交叉点；盘外或太偏返回 (-1,-1)
func pick(mouse_pos: Vector2) -> Vector2i:
	if cam == null:
		return Vector2i(-1, -1)
	var from := cam.project_ray_origin(mouse_pos)
	var dir := cam.project_ray_normal(mouse_pos)
	if absf(dir.y) < 1e-6:
		return Vector2i(-1, -1)
	var t := -from.y / dir.y
	if t <= 0.0:
		return Vector2i(-1, -1)
	var hit := from + dir * t
	var bx := int(round(hit.x / CELL + HALF))
	var by := int(round(hit.z / CELL + HALF))
	if not Coords.valid(bx, by):
		return Vector2i(-1, -1)
	if hit.distance_to(_world(bx, by)) > CELL * 0.45:
		return Vector2i(-1, -1)
	return Vector2i(bx, by)

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_RIGHT:
			if mb.pressed:
				_right_down_ms = Time.get_ticks_msec()   # 按住不动 → 摸头；拖动 → 旋转
				_pat_fired = false
				_pat_drag = 0.0
				_pat_origin = mb.position
			elif _rotating:
				_rotate_hold = GAZE_LOCK_AFTER_ROTATE   # 松开后先看玩家 1 秒
				# 松手兜底：按住时长已够、也没怎么移动 → 补一次摸头
				if not _pat_fired and _pat_drag <= PAT_DRAG_MAX \
						and Time.get_ticks_msec() - _right_down_ms >= PAT_HOLD_MS:
					head_patted.emit()
			_rotating = mb.pressed
			get_viewport().set_input_as_handled()
			return
		if mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_UP:
			_dist = maxf(DIST_MIN, _dist * ZOOM_STEP)      # 滚轮上推：拉近
			_update_camera()
			get_viewport().set_input_as_handled()
			return
		if mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			_dist = minf(DIST_MAX, _dist / ZOOM_STEP)      # 滚轮下拉：拉远
			_update_camera()
			get_viewport().set_input_as_handled()
			return
		if mb.button_index == MOUSE_BUTTON_LEFT and mb.pressed and not locked:
			var hit := pick(get_viewport().get_mouse_position())
			if hit.x >= 0 and not is_occupied(hit.x, hit.y):
				add_optimistic(hit.x, hit.y, "black")
				intersection_clicked.emit(hit.x, hit.y)
				get_viewport().set_input_as_handled()
		return
	if event is InputEventMouseMotion:
		var mm := event as InputEventMouseMotion
		if _rotating:
			_pat_drag = (mm.position - _pat_origin).length()   # 净位移：手抖会相互抵消
			_yaw -= mm.relative.x * ROT_SPEED
			_pitch = clampf(_pitch + mm.relative.y * PITCH_SPEED, PITCH_MIN, PITCH_MAX)
			_update_camera()
			return
		_update_hover(get_viewport().get_mouse_position())

func _update_hover(mouse_pos: Vector2) -> void:
	_hover = pick(mouse_pos)
	var valid := _hover.x >= 0 and not is_occupied(_hover.x, _hover.y) and not locked
	_ghost.visible = valid
	_tip.visible = valid
	if valid:
		_ghost.position = _world(_hover.x, _hover.y, STONE_H * 0.5)
		_tip.text = "%s%d · 点击落子" % [Coords.letter(_hover.x), 19 - _hover.y]
		_tip.position = _world(_hover.x, _hover.y, STONE_H + 0.55)