class_name Ambience
extends Node3D
## 环境：木地板 + 空间风场。
## 风场是一个连续向量场 W(pos, t)：缓慢转向的基础风 × 阵风包络 + 随位置偏移的湍流，
## 因此不同位置的头发/裙摆链在同一时刻受的风不同（阵风会「掠过」身体），而不是统一摆动。
## 化身的发/裙摆逐链采样该风场，风的可视化完全由布料/头发的物理响应体现（不使用粒子）。
##
## v3.12.2 调整：背景是 `Board3D` 相机上的水墨山水；这里是一块**圆形木露台**——
## 棋盘落在圆心，四周环形矮护栏把「悬空圆盘」收成建筑构件，山水从栏杆上方露出来，
## 看起来是坐在山间露台中下棋。地板用用户提供的胡桃木贴图，整体压暗一档融进纸墨色调。

const FLOOR_Y := -0.6
const DECK_R := 22.0                # 露台半径：棋盘（19 路）居中，四周留出一圈走道
const DECK_SEGMENTS := 96           # 圆盘细分（够细，远看不露多边形边）
const DECK_EDGE_H := 0.35           # 圆盘厚度：远看有「一块台面」的体积，不是一张纸
const RAIL_H := 0.9                 # 环形矮护栏高度（不挡棋盘，只把地平线收住）
const RAIL_SEGMENTS := 64           # 环形护栏的分段数：够细，远看是圆环而不是多边形

var _t := 0.0
var _dir := Vector3(1, 0, -0.35).normalized()

func _ready() -> void:
	_build_floor()

func _process(delta: float) -> void:
	_t += delta
	# 基础风向缓慢偏转（两个低频正弦叠加），避免机械感
	var yaw := 0.55 * sin(_t * 0.083) + 0.22 * sin(_t * 0.031 + 1.7)
	_dir = Vector3(cos(yaw), 0.04 * sin(_t * 0.5), sin(yaw)).normalized()

## 位置 pos 处的风速向量（世界单位/秒）
func wind_at(pos: Vector3) -> Vector3:
	# 阵风包络：随位置有相位差，风速峰值会沿风向扫过场景
	var gust := 0.5 + 0.5 * sin(_t * 0.31 + 0.9 * pos.x - 0.7 * pos.z)
	var slow := 0.6 + 0.4 * sin(_t * 0.117 + 0.35 * pos.z + 2.1)
	var speed := 0.55 + 1.7 * gust * slow
	# 湍流：小尺度、随时间变化的三维起伏，让同一束头发也有细微的乱流
	var swirl := Vector3(
		sin(1.9 * pos.z + 2.3 * _t),
		0.35 * sin(2.1 * pos.x + 1.7 * _t + 1.3),
		sin(1.4 * pos.x - 1.1 * pos.z + 2.0 * _t + 0.7))
	return _dir * speed + swirl * (0.55 * slow)

## 无位置信息时的参考风
func wind() -> Vector3:
	return wind_at(Vector3.ZERO)

func _build_floor() -> void:
	_build_disc()
	_build_edge()
	_build_rail_ring()

## 圆盘台面：手工生成三角扇，UV 用**平面投影**（x/z 直接映射），木纹才能保持直纹；
## `PlaneMesh` 是方的、`CylinderMesh` 顶盖是极坐标 UV（会转成漩涡纹），都不合适。
func _build_disc() -> void:
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	for i in DECK_SEGMENTS:
		var a0 := TAU * float(i) / float(DECK_SEGMENTS)
		var a1 := TAU * float(i + 1) / float(DECK_SEGMENTS)
		_disc_vertex(st, Vector3.ZERO)
		_disc_vertex(st, Vector3(sin(a1) * DECK_R, 0.0, cos(a1) * DECK_R))
		_disc_vertex(st, Vector3(sin(a0) * DECK_R, 0.0, cos(a0) * DECK_R))
	st.generate_tangents()                     # 法线贴图需要切线
	var mi := MeshInstance3D.new()
	mi.mesh = st.commit()
	mi.position = Vector3(0, FLOOR_Y, 0)
	var mat := StandardMaterial3D.new()
	# 用户提供的胡桃木贴图：漫反射 + 法线；粗糙度统一，避免 roughness 贴图的线性/编码色差。
	var diff := load("res://assets/textures/floor/walnut_diff.jpg")
	var nor := load("res://assets/textures/floor/walnut_nor.png")
	if diff is Texture2D:
		mat.albedo_texture = diff
	if nor is Texture2D:
		mat.normal_enabled = true
		mat.normal_texture = nor
		mat.normal_scale = 0.65
	# 木纹重复：圆盘直径 44 单位，一格约 6 单位（与 19 路棋盘的比例接近）
	mat.uv1_scale = Vector3(7.0, 8.5, 1.0)
	mat.albedo_color = Color(0.80, 0.78, 0.75)  # 略微压暗：融入宣纸 / 墨色的整体色调
	mat.roughness = 0.66
	mat.metallic_specular = 0.22
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED
	mat.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR_WITH_MIPMAPS_ANISOTROPIC
	mi.material_override = mat
	add_child(mi)

func _disc_vertex(st: SurfaceTool, p: Vector3) -> void:
	st.set_normal(Vector3.UP)
	st.set_uv(Vector2(p.x / (DECK_R * 2.0) + 0.5, p.z / (DECK_R * 2.0) + 0.5))
	st.add_vertex(p)

## 台面侧壁：只在远看时露出的一圈厚度，深木色，把圆盘从「一张纸」变成「一块台面」。
func _build_edge() -> void:
	var cyl := CylinderMesh.new()
	cyl.top_radius = DECK_R
	cyl.bottom_radius = DECK_R
	cyl.height = DECK_EDGE_H
	cyl.radial_segments = DECK_SEGMENTS
	cyl.rings = 1
	cyl.cap_top = false
	cyl.cap_bottom = false
	var mi := MeshInstance3D.new()
	mi.mesh = cyl
	mi.position = Vector3(0, FLOOR_Y - DECK_EDGE_H * 0.5, 0)
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.30, 0.22, 0.15)
	mat.roughness = 0.8
	mi.material_override = mat
	add_child(mi)

## 环形矮护栏：沿圆周排一圈弧形木段 + 压顶，读作「栏杆」而不是一堵墙。
## 板段长 = 弦长（半径 R、圆心角 2π/N），旋转到圆的切线方向；相邻板段略微重叠，接缝不露空。
## 用 `MultiMeshInstance3D` 一次画完 64 段：几何更圆滑，但只有两个 draw call（性能考虑）。
func _build_rail_ring() -> void:
	var wall_mat := StandardMaterial3D.new()
	wall_mat.albedo_color = Color(0.196, 0.141, 0.094)
	wall_mat.roughness = 0.78
	var cap_mat := StandardMaterial3D.new()
	cap_mat.albedo_color = Color(0.259, 0.188, 0.122)
	cap_mat.roughness = 0.55

	var chord := 2.0 * DECK_R * sin(PI / float(RAIL_SEGMENTS)) + 0.05
	var box := BoxMesh.new()
	box.size = Vector3(chord, RAIL_H, 0.35)
	var cap := BoxMesh.new()
	cap.size = Vector3(chord + 0.10, 0.14, 0.47)

	var walls := MultiMesh.new()
	walls.transform_format = MultiMesh.TRANSFORM_3D
	walls.mesh = box
	walls.instance_count = RAIL_SEGMENTS
	var caps := MultiMesh.new()
	caps.transform_format = MultiMesh.TRANSFORM_3D
	caps.mesh = cap
	caps.instance_count = RAIL_SEGMENTS
	for i in RAIL_SEGMENTS:
		var a := TAU * float(i) / float(RAIL_SEGMENTS)
		var basis := Basis(Vector3.UP, a)     # 板段长轴沿圆的切线
		var at := Vector3(sin(a) * DECK_R, 0.0, cos(a) * DECK_R)
		walls.set_instance_transform(i, Transform3D(basis,
			at + Vector3(0, FLOOR_Y + RAIL_H * 0.5, 0)))
		caps.set_instance_transform(i, Transform3D(basis,
			at + Vector3(0, FLOOR_Y + RAIL_H + 0.07, 0)))

	var wmi := MultiMeshInstance3D.new()
	wmi.multimesh = walls
	wmi.material_override = wall_mat
	wmi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(wmi)
	var cmi := MultiMeshInstance3D.new()
	cmi.multimesh = caps
	cmi.material_override = cap_mat
	cmi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(cmi)
