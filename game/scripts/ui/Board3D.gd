class_name Board3D
extends Node3D
## 真实 3D 棋盘：棋盘面/侧厚/网格/星位/坐标、扁形棋子、射线拾取、右键拖拽旋转视角
##
## 键盘：`Home` 回初始取景，`C` 依次看四个角（都不与聊天输入框冲突——输入框有焦点时按键归它）。

signal intersection_clicked(x: int, y: int)
signal view_changed(desc: String)   # 视角便捷键动了视角：给状态栏一句话反馈（文案在 Main 的 _set_status）

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
## 四角轮转：0=西北(A19) 1=西南(A1) 2=东南(T1) 3=东北(T19)
const CORNER_COUNT := 4
const CORNER_NAMES := ["西北", "西南", "东南", "东北"]
const CORNER_GTPS := ["A19", "A1", "T1", "T19"]
## 调试用：直接设定视角
func set_view(yaw_deg: float, pitch_deg: float, dist := 0.0) -> void:
	_yaw = deg_to_rad(yaw_deg)
	_pitch = clampf(deg_to_rad(pitch_deg), PITCH_MIN, PITCH_MAX)
	if dist > 0.0:
		_dist = clampf(dist, DIST_MIN, DIST_MAX)
	_update_camera()

# ================= 视角便捷（Home 回初始取景 / C 四角轮转） =================

var _home_yaw := 0.0             # 启动时捕获的取景（reset_view 的目标）
var _home_pitch := deg_to_rad(38.0)
var _home_dist := 42.0
var _corner := -1                # 上次看过的角（-1=还没看过；C 键从西北角开始轮转）

## 把当前视角记成「初始取景」。`_ready` 里记一次；`Main._handle_cli` 应用完
## `--yaw/--pitch/--dist` 再记一次——调试参数就是现成的目标值来源（spec B3）。
func capture_home_view() -> void:
	_home_yaw = _yaw
	_home_pitch = _pitch
	_home_dist = _dist

## 一键回初始取景（yaw / pitch / dist 三项；棋盘没有「tilt」这个量——`--tilt=` 是化身的歪头）
func reset_view() -> void:
	_yaw = _home_yaw
	_pitch = _home_pitch
	_dist = _home_dist
	_cancel_fly()
	_update_camera()

# ================= 主界面 ⇄ 对局的运镜（v3.11.3） =================
## 运镜只改取景三元组（yaw / pitch / dist），**不碰棋子、不碰面板**；进度由 `_process` 推进，
## 中途玩家拖拽 / 滚轮不会打断（`_unhandled_input` 在飞行中整体让路）。

var _fly_t := -1.0                # <0：没有在飞；>=0：已飞时长（秒）
var _fly_dur := 1.0
var _fly_from := Vector3.ZERO     # x=yaw(rad) y=pitch(rad) z=dist
var _fly_to := Vector3.ZERO
var _fly_done := Callable()

## 平滑进出（smoothstep）：两端速度为 0，中段最快——「缓动飞入」的手感。
## 纯函数，门卫可直接断言（`test_view_helpers.gd`）。
static func fly_ease(t: float) -> float:
	var k := clampf(t, 0.0, 1.0)
	return k * k * (3.0 - 2.0 * k)

## 缓动到目标取景。`duration <= 0` 视作立刻到位（主界面首帧就是这么摆的）。
func fly_to(yaw_deg: float, pitch_deg: float, dist: float, duration := 1.0,
		done := Callable()) -> void:
	_fly_from = Vector3(_yaw, _pitch, _dist)
	_fly_to = Vector3(deg_to_rad(yaw_deg), clampf(deg_to_rad(pitch_deg), PITCH_MIN, PITCH_MAX),
		clampf(dist, DIST_MIN, DIST_MAX))
	if duration <= 0.0:
		_cancel_fly()
		_yaw = _fly_to.x
		_pitch = _fly_to.y
		_dist = _fly_to.z
		_update_camera()
		if done.is_valid():
			done.call()
		return
	_fly_dur = duration
	_fly_t = 0.0
	_fly_done = done

## 缓动回初始取景（正对棋盘）：主界面 → 对局的那一段
func fly_home(duration := 1.0, done := Callable()) -> void:
	fly_to(rad_to_deg(_home_yaw), rad_to_deg(_home_pitch), _home_dist, duration, done)

func is_flying() -> bool:
	return _fly_t >= 0.0

func _cancel_fly() -> void:
	_fly_t = -1.0
	_fly_done = Callable()

func _step_fly(delta: float) -> void:
	if _fly_t < 0.0:
		return
	_fly_t += delta
	var k := fly_ease(_fly_t / _fly_dur)
	_yaw = lerp_angle(_fly_from.x, _fly_to.x, k)
	_pitch = lerpf(_fly_from.y, _fly_to.y, k)
	_dist = lerpf(_fly_from.z, _fly_to.z, k)
	_update_camera()
	if _fly_t >= _fly_dur:
		_yaw = _fly_to.x
		_pitch = _fly_to.y
		_dist = _fly_to.z
		_update_camera()
		var cb := _fly_done
		_cancel_fly()
		if cb.is_valid():
			cb.call()

## 四角的相机方位角（度）：角 0 在 45°（从东南看西北角），每 +1 顺时针走一个角。
## 越界按 4 取模、负数也绕回来（脏值不越界）；纯函数、不碰场景树（门卫可直接断言）。
static func corner_yaw_deg(corner: int) -> float:
	return 45.0 + 90.0 * float(posmod(corner, CORNER_COUNT))

## 看某个角：只转方位角，**不动俯仰与距离**（玩家的缩放/俯仰不被抢走）
func look_at_corner(corner: int) -> void:
	_corner = posmod(corner, CORNER_COUNT)
	_yaw = deg_to_rad(corner_yaw_deg(_corner))
	_update_camera()

## C 键：平滑转到下一个角（只动方位角，保留当前俯仰与距离）。
## 复用 `fly_to` 的 smoothstep 飞行；飞行期间 `_unhandled_input` 整体让路，连按也不会叠加速度。
func fly_to_corner(corner: int, duration := 0.35) -> void:
	_corner = posmod(corner, CORNER_COUNT)
	fly_to(corner_yaw_deg(_corner), rad_to_deg(_pitch), _dist, duration)

## 当前视角（度 / 距离）：门卫与调试核对用（纯读，不碰场景树）
func view_state() -> Dictionary:
	return {"yaw_deg": rad_to_deg(_yaw), "pitch_deg": rad_to_deg(_pitch), "dist": _dist}

var locked := false              # Azure 思考中时禁止落子
var readonly := false            # 复盘回看（v3.7.0）：棋盘整体只读——**落子与摸头都不接受**（与 locked 独立）
var input_blocked := false       # 主界面封面期间：视角键 / 滚轮 / 拖拽一起让路（运镜另有 `is_flying()`）
var ownership_enabled := false   # 领地热力图开关（v3.11.5）：前端渲染，数据由后端 ownership 事件推来
var idle_attention := false      # 玩家长时间无操作：化身转为看向玩家
var cam: Camera3D = null

var _stones_root: Node3D = null
var _ghost: MeshInstance3D = null
var _tip: Label3D = null
var _coord_labels: Array[Label3D] = []   # 四边坐标 A–T / 19–1：可一键隐藏（设置抽屉）
var _yaw := 0.0
var _pitch := deg_to_rad(38.0)   # 开局取景：平视偏俯，不做「俯视」视角
var _dist := 42.0
var _rotating := false
signal head_patted                # 摸摸头：左键点在 Azure 头部附近触发（滑动/按住更快，快速点一下也算）
const PAT_HOLD_MS := 500          # 摸头模式按住多久即触发（毫秒）
const PAT_STROKE_PX := 36.0       # 摸头模式：滑动累计距离达标即触发（轻轻滑动也算）
var pat_head: Node = null         # Azure 化身（提供 head_world_pos() 供摸头区判定）
var _pat_down_ms := 0             # 摸头模式：左键按下的时刻（判断「按住」）
var _pat_fired := false           # 本次手势是否已触发过摸头
var _pat_stroke := 0.0            # 摸头模式内的滑动累计距离（px）
var _pat_active := false          # 摸头模式：不旋转视角，头部/视线跟着鼠标微动
var _rotate_hold := 0.0          # 旋转结束后的注视锁定计时
var _look_point := Vector3.ZERO  # Azure 落子后的注视点
var _look_until := 0.0           # 落子注视的剩余时间
var _panel_left := false         # 左侧面板是否展开
var _panel_right := true         # 右侧面板是否展开
## 面板让位的**实际**比例（0..1）：`set_panels` 只改目标，`_process` 里平滑逼近——
## 主界面进对局时棋盘随工具栏一起让位，不会「啪」地横跳（v3.11.3）。
const PANEL_BIAS_SPEED := 7.0
var _bias_l := 0.0
var _bias_r := 1.0
var _occupied := {}
var _displayed: Array = []       # 当前显示的棋子（权威 + 乐观）
var _optimistic: Array = []
var _hover := Vector2i(-1, -1)
var _move_seq_map := {}          # 后端 state 的 GTP 手顺**预建查表** {归一化 GTP: 手数}：悬停热路径只做一次 lookup
var _font: Font = null
var _black_mat: StandardMaterial3D
var _white_mat: StandardMaterial3D
var _sphere: SphereMesh
var _dot_tex: ImageTexture
var _ownership_plane: MeshInstance3D = null
var _ownership_tex: ImageTexture = null
var _ownership_img: Image = null          # 复用的 19×19 画布：每手只改像素、不新建纹理对象
var _ownership_has_data := false
var _ownership_values: Array = []

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
	capture_home_view()              # 启动取景就是「初始视角」（Main._handle_cli 里应用完调试参数会再记一次）

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

## 侧栏状态：展开的一侧会把棋盘从画面中心推开（单侧沿用既有取景，两侧都开时整体等比推远）。
## 平滑过渡（`_step_panel_bias`）：棋盘跟着面板一起挪，不瞬移。
func set_panels(left_open: bool, right_open: bool) -> void:
	_panel_left = left_open
	_panel_right = right_open
	_update_camera()

## 立刻对齐让位（主界面首帧用：不要开场先飘一下）
func snap_panels(left_open: bool, right_open: bool) -> void:
	_panel_left = left_open
	_panel_right = right_open
	_bias_l = 1.0 if left_open else 0.0
	_bias_r = 1.0 if right_open else 0.0
	_update_camera()

func _step_panel_bias(delta: float) -> void:
	var tl := 1.0 if _panel_left else 0.0
	var tr := 1.0 if _panel_right else 0.0
	if absf(_bias_l - tl) < 0.001 and absf(_bias_r - tr) < 0.001:
		_bias_l = tl
		_bias_r = tr
		return
	var k := clampf(PANEL_BIAS_SPEED * delta, 0.0, 1.0)
	_bias_l = lerpf(_bias_l, tl, k)
	_bias_r = lerpf(_bias_r, tr, k)
	_update_camera()

func _update_camera() -> void:
	if cam == null:
		return                                   # 还没建相机（`Board3D.new()` 的门卫 / 视角便捷键的纯逻辑）
	var vp := get_viewport().get_visible_rect().size
	var vh := vp.y
	var free_l := PANEL_W * _bias_l
	var free_r := PANEL_W * _bias_r
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
	# `look_at` 要的是**全局**坐标：棋盘可能被缩放/平移（真实尺度场景），局部 target 不能直接用
	cam.look_at(cam.get_parent().to_global(target), Vector3.UP)

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
	bmat.albedo_color = Color(0.722, 0.635, 0.463)
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
	_build_ownership()

## 领地热力图：19×19 的半透明贴图铺在棋盘面之上、网格之下（y=0.004）。
## 每个像素对应一个交叉点；线性过滤让色块变柔和，不挡棋子 / 网格 / 点击。
func _build_ownership() -> void:
	_ownership_tex = ImageTexture.new()
	_ownership_img = Image.create(19, 19, false, Image.FORMAT_RGBA8)
	_ownership_img.fill(Color(0, 0, 0, 0))
	_ownership_tex.set_image(_ownership_img)
	var pm := PlaneMesh.new()
	pm.size = Vector2(19.0 * CELL, 19.0 * CELL)
	_ownership_plane = MeshInstance3D.new()
	_ownership_plane.mesh = pm
	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	mat.albedo_texture = _ownership_tex
	mat.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED
	_ownership_plane.material_override = mat
	_ownership_plane.position = Vector3(0, 0.004, 0)
	_ownership_plane.visible = false
	add_child(_ownership_plane)

## 单个交叉点的归属颜色（纯函数，门卫可直接断言）：
## + 黑（墨色）/ - 白（纸白），透明度随 |v| 增大；|v| 很小就完全透明。
static func ownership_color(v: float) -> Color:
	var f := clampf(v, -1.0, 1.0)
	if not is_finite(f):
		f = 0.0
	var a := absf(f) * 0.50
	if a < 0.02:
		return Color(0, 0, 0, 0)
	return Color(0.06, 0.08, 0.10, a) if f > 0.0 else Color(0.99, 0.97, 0.92, a)

## 后端 ownership 事件 → 贴图。长度必须 361；脏值退 0，绝不抛。
## 复用同一块 Image / ImageTexture：每手只改 361 个像素再 update，不再新建纹理对象。
func set_ownership(values: Array) -> void:
	if values.size() != 19 * 19:
		clear_ownership()
		return
	if _ownership_img == null:
		_ownership_img = Image.create(19, 19, false, Image.FORMAT_RGBA8)
		_ownership_img.fill(Color(0, 0, 0, 0))
	for y in 19:
		for x in 19:
			var raw = values[y * 19 + x]
			var v := 0.0
			if typeof(raw) == TYPE_FLOAT or typeof(raw) == TYPE_INT:
				v = float(raw)
			_ownership_img.set_pixel(x, y, ownership_color(v))
	if _ownership_tex == null:
		_ownership_tex = ImageTexture.create_from_image(_ownership_img)
	else:
		_ownership_tex.update(_ownership_img)
	_ownership_values = values.duplicate()
	_ownership_has_data = true
	if _ownership_plane != null:
		_ownership_plane.visible = ownership_enabled

func set_ownership_enabled(on: bool) -> void:
	ownership_enabled = on
	if _ownership_plane != null:
		_ownership_plane.visible = on and _ownership_has_data

func clear_ownership() -> void:
	_ownership_has_data = false
	_ownership_values = []
	if _ownership_plane != null:
		_ownership_plane.visible = false

## 战报用：当前可见棋子（权威 + 乐观）与最近一次领地图；只读副本，调用方随便改。
func visible_stones() -> Array:
	var out: Array = _displayed.duplicate(true)
	out.append_array(_optimistic)
	return out

func ownership_values() -> Array:
	return _ownership_values.duplicate()

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
	# 自用版没有地板 / 背景板：坐标颜色跟随主题（浅色底用深字、深色底用亮字）。
	l.modulate = GufengTheme.LABEL_FG
	l.outline_size = 6
	l.outline_modulate = GufengTheme.LABEL_OUTLINE
	l.rotation_degrees = Vector3(-90, 0, 0)
	l.position = pos
	add_child(l)
	_coord_labels.append(l)

## 显示 / 隐藏棋盘四边的坐标（本局内即时生效；不影响落子与摸头）。
func set_coords_visible(v: bool) -> void:
	for l in _coord_labels:
		if is_instance_valid(l):
			l.visible = v

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
	_tip.modulate = GufengTheme.TIP_FG
	_tip.outline_size = 12
	_tip.outline_modulate = GufengTheme.LABEL_OUTLINE
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
		dot.modulate = Color(0.984, 0.965, 0.918) if color == "black" else Color(0.651, 0.231, 0.180)
		dot.position = _world(x, y, STONE_H + 0.05)
		_stones_root.add_child(dot)

func is_occupied(x: int, y: int) -> bool:
	return _occupied.has(Vector2i(x, y))

# ================= 悬停提示：坐标 + 手数 =================

## 后端 state 的 `move_seq`（GTP 手顺，含 "pass"）：每次 state 事件对齐一次。
## 这里**预建查表** `{归一化 GTP: 手数}`（同一交叉点取**最后一次**出现 ⇒ 后面的覆盖前面的）：
## 悬停是每次鼠标移动都会走的热路径，原先每次都把整条手顺 `strip_edges().to_upper()` 扫一遍
## （300 手 × 100Hz），现在只在 state 变更时扫一次。脏值（非数组 / 元素非字符串）一律跳过。
func set_move_seq(seq) -> void:
	_move_seq_map.clear()
	if typeof(seq) != TYPE_ARRAY:
		return
	var arr := seq as Array
	for i in arr.size():
		var v = arr[i]
		if typeof(v) == TYPE_STRING:
			_move_seq_map[str(v).strip_edges().to_upper()] = i + 1

## 某交叉点在 `move_seq` 里的手数（1 基）：查 `set_move_seq` 预建的表；不在表里 / 盘外 ⇒ 0。
func move_no_at(x: int, y: int) -> int:
	if not Coords.valid(x, y):
		return 0
	return int(_move_seq_map.get(Coords.xy_to_gtp(x, y), 0))

## 手数推法的**纯函数参照实现**（静态、不碰场景树，门卫可直接断言；语义与上面的预建表逐字一致）：
## 取**最后一次**出现——同一交叉点被提后再下时以最近一手为准；复盘快照的 `move_seq` 已截到当前
## 回看手，天然对齐。不在序列里 / 脏值 / 盘外 ⇒ 0。实例路径走 `move_no_at`（O(1) 查表）。
static func move_no_in_seq(x: int, y: int, move_seq) -> int:
	if typeof(move_seq) != TYPE_ARRAY or not Coords.valid(x, y):
		return 0
	var want := Coords.xy_to_gtp(x, y)
	var found := 0
	for i in (move_seq as Array).size():
		var v = (move_seq as Array)[i]
		if typeof(v) == TYPE_STRING and str(v).strip_edges().to_upper() == want:
			found = i + 1
	return found

## 悬停文案的**纯逻辑核心**（静态、不碰场景树）：盘外 ⇒ 空串；`move_no > 0`（已有棋子）⇒
## 「D4 · 第 3 手」；否则（空点）⇒ 既有措辞「D4 · 点击落子」（逐字符沿用，不许改）。
static func tip_text_at(x: int, y: int, move_no: int) -> String:
	if not Coords.valid(x, y):
		return ""
	var label := Coords.xy_to_gtp(x, y)
	if move_no > 0:
		return "%s · 第 %d 手" % [label, move_no]
	return "%s · 点击落子" % label

## 悬停文案（实例入口）：已有棋子给坐标 + 手数，空点给「点击落子」
func tip_text(x: int, y: int) -> String:
	return tip_text_at(x, y, move_no_at(x, y) if is_occupied(x, y) else 0)

# ================= 定式高亮轨迹 =================

var _joseki_root: Node3D = null
var _joseki_marks: Array = []        # 淡出中的标记节点（Sprite3D / Label3D）
var _joseki_t := 0.0

## 定式高亮：formed=按手顺点亮编号光点；deviated=单点浅提示 几秒后自动淡出；新一轮会清掉上一轮
func highlight_points(points: Array, kind := "formed") -> void:
	_clear_joseki()
	_joseki_root = Node3D.new()
	add_child(_joseki_root)
	var idx := 0
	for p in points:
		if typeof(p) != TYPE_VECTOR2I:
			continue
		var px: Vector2i = p
		idx += 1
		var dot := Sprite3D.new()
		dot.texture = _dot_tex
		dot.billboard = BaseMaterial3D.BILLBOARD_ENABLED
		dot.pixel_size = 0.0064
		dot.modulate = Color(0.243, 0.361, 0.345, 0.0) if kind == "formed" else Color(0.557, 0.400, 0.169, 0.0)
		dot.position = _world(px.x, px.y, STONE_H + 0.42)
		_joseki_root.add_child(dot)
		_joseki_marks.append(dot)
		if kind == "formed" and points.size() > 1:
			var lb := Label3D.new()
			lb.text = str(idx)
			if _font != null:
				lb.font = _font
			lb.font_size = 64
			lb.outline_size = 16
			lb.pixel_size = 0.0026
			lb.billboard = BaseMaterial3D.BILLBOARD_ENABLED
			lb.no_depth_test = true
			lb.modulate = Color(0.984, 0.965, 0.918, 0.0)
			lb.outline_modulate = Color(0.173, 0.165, 0.149, 0.92)
			lb.position = _world(px.x, px.y, STONE_H + 1.25)
			_joseki_root.add_child(lb)
			_joseki_marks.append(lb)
	_joseki_t = 0.0

func _clear_joseki() -> void:
	if _joseki_root != null and is_instance_valid(_joseki_root):
		_joseki_root.queue_free()
	_joseki_root = null
	_joseki_marks = []
	_joseki_t = 0.0

# ================= 点目标记（v3.12.0 采纳） =================

## 后端 `estimate` 事件里的归属小方块：空点贴在棋盘面上，已有棋子的点抬到棋子上（被点死的对手子）。
const EST_MARK := 0.30          # 小方块边长（格距 1.0）
const EST_Y := 0.03             # 空点：压在网格线 / 星位之上
const EST_DEAD_Y := STONE_H + 0.02
const EST_BLACK := Color(0.129, 0.122, 0.110)
const EST_WHITE := Color(0.980, 0.953, 0.886)

var _estimate_root: Node3D = null

## 一格点目数据是否可用（静态纯函数：门卫直接断言，JSON 里的数字是 float 也认）。
static func estimate_cell_ok(c) -> bool:
	if typeof(c) != TYPE_ARRAY or (c as Array).size() < 3:
		return false
	var x = (c as Array)[0]
	var y = (c as Array)[1]
	var side = (c as Array)[2]
	if typeof(x) != TYPE_INT and typeof(x) != TYPE_FLOAT:
		return false
	if typeof(y) != TYPE_INT and typeof(y) != TYPE_FLOAT:
		return false
	if typeof(side) != TYPE_STRING:
		return false
	if not (side == "black" or side == "white"):
		return false
	var xi := int(x)
	var yi := int(y)
	return xi >= 0 and xi < 19 and yi >= 0 and yi < 19

## 画 / 换一批点目标记；传空数组等于清除。全部脏值跳过（不画、不抛）。
func set_estimate(cells) -> void:
	clear_estimate()
	if typeof(cells) != TYPE_ARRAY:
		return
	var black := SurfaceTool.new()
	var white := SurfaceTool.new()
	black.begin(Mesh.PRIMITIVE_TRIANGLES)
	white.begin(Mesh.PRIMITIVE_TRIANGLES)
	var nb := 0
	var nw := 0
	var half := EST_MARK * 0.5
	for c in (cells as Array):
		if not estimate_cell_ok(c):
			continue
		var x := int((c as Array)[0])
		var y := int((c as Array)[1])
		var side := str((c as Array)[2])
		var h := EST_DEAD_Y if is_occupied(x, y) else EST_Y
		var p := _world(x, y, h)
		var st := black if side == "black" else white
		_mark_quad(st, p, half)
		if side == "black":
			nb += 1
		else:
			nw += 1
	if nb == 0 and nw == 0:
		return
	_estimate_root = Node3D.new()
	add_child(_estimate_root)
	if nb > 0:
		_estimate_root.add_child(_mark_mesh(black.commit(), EST_BLACK))
	if nw > 0:
		_estimate_root.add_child(_mark_mesh(white.commit(), EST_WHITE))

func _mark_quad(st: SurfaceTool, p: Vector3, half: float) -> void:
	st.add_vertex(Vector3(p.x - half, p.y, p.z - half))
	st.add_vertex(Vector3(p.x + half, p.y, p.z + half))
	st.add_vertex(Vector3(p.x + half, p.y, p.z - half))
	st.add_vertex(Vector3(p.x - half, p.y, p.z - half))
	st.add_vertex(Vector3(p.x - half, p.y, p.z + half))
	st.add_vertex(Vector3(p.x + half, p.y, p.z + half))

func _mark_mesh(mesh: ArrayMesh, col: Color) -> MeshInstance3D:
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = col
	m.cull_mode = BaseMaterial3D.CULL_DISABLED
	mi.material_override = m
	return mi

## 收起点目标记（玩家下一手 / 重来 / 换局面 / 进主界面 / 进复盘）。
func clear_estimate() -> void:
	if _estimate_root != null and is_instance_valid(_estimate_root):
		_estimate_root.queue_free()
	_estimate_root = null

# ================= 化身视线目标 =================

func _process(delta: float) -> void:
	_step_panel_bias(delta)                      # 面板让位平滑（棋盘不横跳）
	_step_fly(delta)                             # 主界面 ⇄ 对局的运镜（v3.11.3）
	if _rotate_hold > 0.0:
		_rotate_hold -= delta
	if _look_until > 0.0:
		_look_until -= delta
	# 定式高亮：快速淡入 → 停留 → 整体淡出
	if not _joseki_marks.is_empty():
		_joseki_t += delta
		var ja := 1.0
		if _joseki_t < 0.25:
			ja = _joseki_t / 0.25
		elif _joseki_t > 3.4:
			ja = clampf(1.0 - (_joseki_t - 3.4) / 1.1, 0.0, 1.0)
		for m in _joseki_marks:
			if is_instance_valid(m):
				(m as Node3D).modulate.a = ja
		if ja <= 0.0:
			_clear_joseki()
	# 摸头判定：摸头模式内滑动累计达标或按住到时间即触发
	if _pat_active and not _pat_fired:
		var held := Time.get_ticks_msec() - _pat_down_ms
		if held >= PAT_HOLD_MS or _pat_stroke >= PAT_STROKE_PX:
			_pat_fired = true
			head_patted.emit()

## 玩家是否正在拖拽旋转视角
func is_rotating() -> bool:
	return _rotating

## 鼠标是否位于 Azure 头部附近（摸头触发区；半径随窗口高度放大，容易命中）
## 放大视角后头可能出画：判定用「头 → 髋 → 脚」的身体中线，点在可见的头部/上身上都算摸头。
func _in_head_zone(pos: Vector2) -> bool:
	if pat_head == null or cam == null:
		return false
	var pts: Array = []
	if pat_head.has_method("pat_zone_world"):
		pts = pat_head.call("pat_zone_world")
	elif pat_head.has_method("head_world_pos"):
		pts = [pat_head.call("head_world_pos")]
	var proj: Array[Vector2] = []
	for p in pts:
		if typeof(p) != TYPE_VECTOR3:
			continue
		var wp: Vector3 = p
		if wp.is_finite() and not cam.is_position_behind(wp):
			proj.append(cam.unproject_position(wp))
	if proj.is_empty():
		return false
	var r := maxf(110.0, get_viewport().get_visible_rect().size.y * 0.15)
	if proj.size() == 1:
		return pos.distance_to(proj[0]) <= r
	var best := INF
	for i in range(proj.size() - 1):
		best = minf(best, _point_segment_distance(pos, proj[i], proj[i + 1]))
	return best <= r

## 点到线段的最短距离（屏幕坐标；纯函数，门卫可直接断言）
static func _point_segment_distance(p: Vector2, a: Vector2, b: Vector2) -> float:
	var ab := b - a
	var l2 := ab.length_squared()
	if l2 <= 0.0001:
		return p.distance_to(a)
	var t := clampf((p - a).dot(ab) / l2, 0.0, 1.0)
	return p.distance_to(a + ab * t)

## 化身的注视点优先级：摸头中（跟随鼠标微动）→ 旋转视角（含结束后 1 秒）→ Azure 刚落的子 → 闲置看玩家 → 鼠标落点
func gaze_target() -> Vector3:
	if _pat_active:
		var mp := get_viewport().get_mouse_position()
		if cam != null:
			return cam.project_position(mp, 8.0)   # 摸头中：头/颈/目光跟着鼠标轻轻走
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
	# 2.5 是**棋盘单位**：棋盘若被整体缩放（真实尺度场景），要按缩放换算；
	# 否则相机在 2 m 高时减去 2.5 会落到地下，Azure 只能低头、抬不起视线对视。
	var s := global_transform.basis.get_scale().y
	return cam.global_position - Vector3(0, 2.5 * s, 0)

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
	# 棋盘可能被缩放到真实尺度 / 平移过：拾取一律换到棋盘**本地**坐标再换算交叉点
	var local := to_local(hit)
	var bx := int(round(local.x / CELL + HALF))
	var by := int(round(local.z / CELL + HALF))
	if not Coords.valid(bx, by):
		return Vector2i(-1, -1)
	if local.distance_to(_world(bx, by)) > CELL * 0.45:
		return Vector2i(-1, -1)
	return Vector2i(bx, by)

## 能不能落子：复盘只读或她思考中都不行。
## 纯函数、只读两个标志、不碰场景树 ⇒ `Board3D.new()` 就能断言（V11，不必合成输入事件）。
func placement_allowed() -> bool:
	return not readonly and not locked

## 能不能摸头：**只有复盘只读挡**；她思考中（`locked`）仍可摸——与经典版一致。
## 摸头只是互动，不改棋局，没有理由跟着落子一起冻。
func pat_allowed() -> bool:
	return not readonly

## 复盘只读开关（进入/退出复盘时由 `Main._refresh_lock()` 逐次对齐）：
## 顺手作废进行中的摸头手势——否则手势的松手与 `_process` 触发会落到只读期间。
func set_readonly(v: bool) -> void:
	readonly = v
	if v:
		_pat_active = false

func _unhandled_input(event: InputEvent) -> void:
	if input_blocked or is_flying():
		return                                   # 封面 / 运镜期间输入整体让路（落子 / 摸头 / 旋转 / 缩放 / Home·C）
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				if _in_head_zone(mb.position):
					if not pat_allowed():
						return                 # 只有复盘只读挡摸头；她思考中（locked）照样能摸
					# 摸头模式：视角不动，头部/视线跟着鼠标微动；滑动/按住在 _process 里触发，快速点一下松手补一次
					_pat_active = true
					_pat_fired = false
					_pat_stroke = 0.0
					_pat_down_ms = Time.get_ticks_msec()
					get_viewport().set_input_as_handled()
					return
				if not placement_allowed():
					return                     # 复盘只读 / 她思考中：只挡落子
				var hit := pick(mb.position)
				if hit.x >= 0 and not is_occupied(hit.x, hit.y):
					add_optimistic(hit.x, hit.y, "black")
					intersection_clicked.emit(hit.x, hit.y)
					get_viewport().set_input_as_handled()
				return
			if _pat_active:
				if not _pat_fired:
					_pat_fired = true
					head_patted.emit()          # 快速点一下（没滑动也没按够时间）松手也算一次摸头
				_pat_active = false
				get_viewport().set_input_as_handled()
			return
		if mb.button_index == MOUSE_BUTTON_RIGHT:
			if mb.pressed:
				_pat_active = false             # 右键拖拽始终是旋转视角
				_rotating = true
			elif _rotating:
				_rotating = false
				_rotate_hold = GAZE_LOCK_AFTER_ROTATE   # 松开后先看玩家 1 秒
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
		return
	if event is InputEventMouseMotion:
		var mm := event as InputEventMouseMotion
		if _pat_active:
			_pat_stroke += mm.relative.length()          # 摸头模式：只累计抚摸距离，视角不动
			return
		if _rotating:
			_yaw -= mm.relative.x * ROT_SPEED
			_pitch = clampf(_pitch + mm.relative.y * PITCH_SPEED, PITCH_MIN, PITCH_MAX)
			_update_camera()
			return
		_update_hover(get_viewport().get_mouse_position())
		return
	if event is InputEventKey:
		var k := event as InputEventKey
		if not k.pressed or k.echo:
			return
		# 修饰键一律不参与（R-GH-2）：`Ctrl+C` / `Shift+C` / `Alt+C` 都不是「看角」的意思——
		# 这两个快捷键是**裸键**手势，带上修饰键多半是别处的组合键（复制 / 输入大写），与「不易误触」一致。
		if k.ctrl_pressed or k.alt_pressed or k.meta_pressed or k.shift_pressed:
			return
		match k.keycode:
			KEY_HOME:
				reset_view()
				view_changed.emit("视角：已回到初始取景")
			KEY_C:
				fly_to_corner(_corner + 1, 0.35)
				view_changed.emit("视角：看%s角（%s）" % [CORNER_NAMES[_corner], CORNER_GTPS[_corner]])
			_:
				return
		_rotate_hold = GAZE_LOCK_AFTER_ROTATE          # 视角动了：跟手动旋转一样先看玩家 1 秒
		get_viewport().set_input_as_handled()

func _update_hover(mouse_pos: Vector2) -> void:
	_hover = pick(mouse_pos)
	var on_board := _hover.x >= 0 and not _in_head_zone(mouse_pos)   # 头部附近左键是摸头，不提示落子
	var occupied := on_board and is_occupied(_hover.x, _hover.y)
	var placeable := on_board and not occupied and placement_allowed()
	# 提示分三种：已有棋子（任何状态下都给坐标 + 手数）、空点且真能落子（沿用既有「点击落子」）、
	# 其余（盘外 / 她思考中 / 只读复盘的空点）不提示。
	_ghost.visible = placeable
	_tip.visible = on_board and (occupied or placement_allowed())
	if placeable:
		_ghost.position = _world(_hover.x, _hover.y, STONE_H * 0.5)
	if _tip.visible:
		_tip.text = tip_text(_hover.x, _hover.y)
		_tip.position = _world(_hover.x, _hover.y, STONE_H + 0.55)