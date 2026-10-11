class_name Ambience
extends Node3D
## 环境：地面 + 空间风场。
## 风场是一个连续向量场 W(pos, t)：缓慢转向的基础风 × 阵风包络 + 随位置偏移的湍流，
## 因此不同位置的头发/裙摆链在同一时刻受的风不同（阵风会「掠过」身体），而不是统一摆动。
## 化身的发/裙摆逐链采样该风场，风的可视化完全由布料/头发的物理响应体现（不使用粒子）。

const FLOOR_Y := -0.6

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
	var plane := PlaneMesh.new()
	plane.size = Vector2(260, 260)
	var mi := MeshInstance3D.new()
	mi.mesh = plane
	mi.position = Vector3(0, FLOOR_Y, 0)
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.945, 0.960, 0.950)
	mat.roughness = 1.0
	mi.material_override = mat
	add_child(mi)