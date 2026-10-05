class_name AzureAvatar
extends Node3D
## Azure 的 VRM 化身：加载 VRM → 摆成鸭子坐（W 坐）→ 呼吸/眨眼/表情/视线跟随，头发与裙摆随风场摆动
## VRM 1.0（VRoid Studio 导出）：骨骼 J_Bip_*/J_Sec_*，表情 Fcl_*（眨眼/喜/惊/悲…），VRMC_springBone 提供发/裙摆链
##
## 摆姿原理（实测确认）：Godot 4 的骨骼 pose 就是「绝对局部变换」——未设置时等于 rest_local，
## 设置后直接替换局部变换：全局 = 父全局 * pose。因此「指向世界系目标」解为：
##   pose = Q(t_local) * rest_local_rot，其中 t_local = (父全局)^-1 * 目标方向。pose 与全局矩阵语义一致
## （姿态/动画都会改变全局矩阵，直接 set_bone_pose_rotation 在 Godot 4.6 完全安全）

const HIP_H_MODEL := 0.9279      # 静止站立时髋骨高度（模型米制，由静态探测得到）
const SIT_HIP_H := 0.19          # 坐下后髋部离地高度（模型米制；留出裙摆铺地的余量）

var vrm_path := "D:/KataGo/Azure.vrm"
var scale_factor := 13.5         # 棋盘 1 格 = 1 世界单位，化身按此比例缩到「同伴」尺寸
var floor_y := -0.6              # 地面高度（与棋盘底面一致）
var seat_z := -15.2              # 坐在棋盘对面（远端之后；裙摆/手都不越过棋盘边线）
var wind_source: Node = null     # 提供 wind_at(pos) -> Vector3（空间风场）
var gaze_source: Node = null     # 提供 gaze_target() -> Vector3（旋转视角时看玩家，平时看鼠标）

var ok := false

var _skel: Skeleton3D = null
var _face: MeshInstance3D = null
var _blink_shape := -1
var _spring_defs: Array = []     # [{names: [骨骼名], stiffness, drag}]

var _rest_local_rot: Dictionary = {}
var _rest_global_rot: Dictionary = {}
var _dir_local: Dictionary = {}        # idx -> 骨骼朝向（rest 全局系逆变换到局部）
var _posed: Dictionary = {}            # idx -> 已写入的 pose 旋转
var _posed_scale: Dictionary = {}      # idx -> 已写入的 pose 缩放（用于腿部加粗）
var _base_pose: Dictionary = {}        # idx -> 基底 pose（呼吸/视线/摆动以此叠加）
var _axis_y: Dictionary = {}           # idx -> 世界 Y 轴（视线偏航）在该骨局部系
var _axis_x: Dictionary = {}           # idx -> 世界 X 轴（点头俯仰）在该骨局部系
var _axis_z: Dictionary = {}           # idx -> 世界 Z 轴（歪头横滚）在该骨局部系
var _anim_idx: Dictionary = {}         # 骨骼名 -> idx（动画高频访问，避免每帧查名字）

var _chains: Array = []          # [{pos, amp, bones: [{idx, axis, move_dir, level, theta, vel, k, c, base}]}]
var _t := 0.0
var _blink_wait := 2.5
var _blink_t := -1.0

# 视线：先转头，再依次带上颈/胸/脊柱，眼睛补足残余，均带人体限幅
const GAZE_SMOOTH := 6.0
const YAW_HEAD := 0.84           # 48°
const YAW_NECK := 0.28           # 16°
const YAW_CHEST := 0.30          # 17°
const YAW_SPINE := 0.18          # 10°
## 抬头要够得着上方的玩家（相机转到高处时能真的仰头看），低头够看棋盘
const PITCH_UP_HEAD := 0.50      # 抬头上限 29°
const PITCH_UP_NECK := 0.12      # 7°
const PITCH_UP_CHEST := 0.18     # 10°（合计约 46°）
const PITCH_DOWN_HEAD := 0.35    # 低头上限 20°
const PITCH_DOWN_NECK := 0.09    # 5°
const PITCH_DOWN_CHEST := 0.14   # 8°（合计约 33°）
const PITCH_BIAS := 0.12         # 整体视线略抬 7°，避免显得一直盯着盘面
const YAW_EYE := 0.20            # 眼球附加 11°
const PITCH_EYE := 0.14
const GAZE_GREET_SEC := 1.4      # 开局先正对玩家看一眼，再交给鼠标跟随
var _gaze_dir := Vector3(0, 0, 1)
var _gaze_ready := false
var _gaze_pivot := Vector3.ZERO
var _gaze_greet := GAZE_GREET_SEC
var _dbg_gaze := Vector3.ZERO          # 调试用（yaw, pitch, 0）

# 表情：常态微微笑做基线 + 事件驱动的小时间线（惊讶→失落等）
# 思考中（busy）收起常态表情，保持中性；空闲时自动回到微微笑
var _expr_shapes: Dictionary = {}      # 表情名 -> 形状索引
var _expr_queue: Array = []            # [[形状名, 目标值, 上升, 保持, 回落], ...]
var _expr_stage := 0
var _expr_t := 0.0
var _expr_targets: Dictionary = {}
var _expr_cur: Dictionary = {}
var _expr_baseline: Dictionary = {}    # 常态表情：形状 -> 值
var _expr_thinking := false

# 歪头小动作：闲置 / 说话时偶尔轻轻歪一下（绕世界 Z 轴横滚），随机起止
var _idle := false
var _speaking := false
var _tilt_cur := 0.0
var _tilt_target := 0.0
var _tilt_next := 0.0
var _tilt_phase_until := 0.0
var _tilt_active := false
var _tilt_debug := 0.0                 # 调试：常驻歪头角（--tilt= 传入度数）

func _ready() -> void:
	var err := _load()
	if err != "":
		push_warning("[Avatar] " + err)
		return
	ok = true
	_build_rig()
	_place()            # 先落位：之后所有姿态/裙摆计算都在最终世界系里进行（裙摆要按离地高度铺）
	_pose_w_sit()
	_capture_bases()
	_build_axes()
	_build_chains()
	_update_gaze_pivot()
	_update_baseline()          # 常态微微笑

# ================= 加载 =================

func _load() -> String:
	if not FileAccess.file_exists(vrm_path):
		return "找不到 VRM：%s" % vrm_path
	var bytes := FileAccess.get_file_as_bytes(vrm_path)
	if bytes.is_empty():
		return "VRM 读取失败：%s" % vrm_path
	var doc := GLTFDocument.new()
	var state := GLTFState.new()
	if doc.append_from_buffer(bytes, "", state) != OK:
		return "VRM 解析失败（GLTFDocument）"
	var scene := doc.generate_scene(state)
	if scene == null:
		return "VRM 场景生成失败"
	var meshes: Array[MeshInstance3D] = []
	_collect(scene, meshes)
	if _skel == null:
		return "VRM 内没有骨骼"
	for mi in meshes:
		var mesh := mi.mesh
		if mesh == null:
			continue
		for i in mesh.get_blend_shape_count():
			var nm: String = mesh.get_blend_shape_name(i)
			if nm == "Fcl_EYE_Close":
				_face = mi
				_blink_shape = i
			if nm.begins_with("Fcl_ALL_"):
				_expr_shapes[nm] = i
		if _face != null and not _expr_shapes.is_empty():
			for si in mesh.get_surface_count():          # 脸部材质复制一份：脸红靠染色叠加
				var sm := mesh.surface_get_material(si)
				if sm is StandardMaterial3D:
					var dup: StandardMaterial3D = (sm as StandardMaterial3D).duplicate()
					mi.set_surface_override_material(si, dup)
					_blush_mats.append([dup, dup.albedo_color])
			break
	_spring_defs = _parse_springs(bytes)
	scene.scale = Vector3.ONE * scale_factor
	add_child(scene)
	if _face == null:
		push_warning("[Avatar] 未找到眨眼表情（Fcl_EYE_Close），将跳过眨眼")
	return ""

func _collect(n: Node, meshes: Array) -> void:
	if n is Skeleton3D:
		_skel = n as Skeleton3D
	if n is MeshInstance3D:
		meshes.append(n)
	for c in n.get_children():
		_collect(c, meshes)

## 从 GLB 的 JSON chunk 中取 VRMC_springBone 链定义（发/裙摆）
func _parse_springs(bytes: PackedByteArray) -> Array:
	var out: Array = []
	if bytes.size() < 20 or bytes.decode_u32(0) != 0x46546C67:      # "glTF"
		return out
	if bytes.decode_u32(16) != 0x4E4F534A:                          # "JSON"
		return out
	var clen := bytes.decode_u32(12)
	var j = JSON.parse_string(bytes.slice(20, 20 + clen).get_string_from_utf8())
	if typeof(j) != TYPE_DICTIONARY:
		return out
	var nodes: Array = (j as Dictionary).get("nodes", [])
	var ext: Dictionary = (j as Dictionary).get("extensions", {})
	var sb: Dictionary = ext.get("VRMC_springBone", {})
	for s in (sb.get("springs", []) as Array):
		var names: Array[String] = []
		var stiff := 0.5
		var drag := 0.4
		for jt in (s.get("joints", []) as Array):
			var nidx := int((jt as Dictionary).get("node", -1))
			if nidx >= 0 and nidx < nodes.size():
				var nm := str((nodes[nidx] as Dictionary).get("name", ""))
				if _skel.find_bone(nm) >= 0:
					names.append(nm)
			stiff = float((jt as Dictionary).get("stiffness", stiff))
			drag = float((jt as Dictionary).get("dragForce", drag))
		if names.size() >= 2:
			out.append({"names": names, "stiffness": stiff, "drag": drag})
	return out

# ================= 骨骼与姿态工具 =================

func _build_rig() -> void:
	var n := _skel.get_bone_count()
	for i in n:
		_rest_local_rot[i] = _skel.get_bone_rest(i).basis.get_rotation_quaternion()
		_anim_idx[_skel.get_bone_name(i)] = i
	for i in n:
		_rest_global_rot[i] = _rest_global(i)
	for i in n:
		var dir_world := _bone_axis_world(i)
		if dir_world == Vector3.ZERO:
			continue
		_dir_local[i] = (_rest_global_rot[i] as Quaternion).inverse() * dir_world

func _rest_global(i: int) -> Quaternion:
	if _rest_global_rot.has(i):
		return _rest_global_rot[i]
	var g: Quaternion = _rest_local_rot[i]
	var p := _skel.get_bone_parent(i)
	if p >= 0:
		g = _rest_global(p) * g
	return g

## 骨骼的「主要朝向」：优先同类（J_Bip→J_Bip / J_Sec→J_Sec）主链子骨，否则退回自身骨轴
func _bone_axis_world(i: int) -> Vector3:
	var own := _skel.get_bone_name(i)
	var kids := _skel.get_bone_children(i)
	var pick := -1
	for c in kids:
		if _same_family(own, _skel.get_bone_name(c)):
			pick = c
			break
	if pick < 0:
		for c in kids:
			if not _skel.get_bone_name(c).begins_with("J_Adj"):
				pick = c
				break
	var rest_i := _skel.get_bone_global_rest(i).origin
	if pick >= 0:
		return (_skel.get_bone_global_rest(pick).origin - rest_i).normalized()
	var p := _skel.get_bone_parent(i)
	if p >= 0:
		return (rest_i - _skel.get_bone_global_rest(p).origin).normalized()
	return Vector3.ZERO

static func _same_family(a: String, b: String) -> bool:
	if a.begins_with("J_Bip"):
		return b.begins_with("J_Bip")
	if a.begins_with("J_Sec"):
		return b.begins_with("J_Sec")
	return false

## 骨骼当前全局旋转（pose 即绝对局部变换；未写入时按 rest 处理）
func _cur_global(i: int) -> Quaternion:
	var local: Quaternion = _posed[i] if _posed.has(i) else _rest_local_rot[i]
	var p := _skel.get_bone_parent(i)
	if p >= 0:
		return _cur_global(p) * local
	return local

## 骨骼当前全局变换（含位置）——完全由自身累积计算。
## 注意：get_bone_global_pose 在同一帧内读到的是过期值（pose 写入后要等引擎更新），
## 摆姿阶段必须用这个函数取位置，否则会用错原点。
func _cur_global_xform(i: int) -> Transform3D:
	var rot: Quaternion = _posed[i] if _posed.has(i) else _rest_local_rot[i]
	var sc: Vector3 = _posed_scale.get(i, Vector3.ONE)
	var local := Transform3D(Basis(rot).scaled(sc), _skel.get_bone_rest(i).origin)
	var p := _skel.get_bone_parent(i)
	if p >= 0:
		return _cur_global_xform(p) * local
	return local

func _set_pose(idx: int, pose_rot: Quaternion) -> void:
	_posed[idx] = pose_rot
	_skel.set_bone_pose_rotation(idx, pose_rot)

## 让骨骼的「主朝向」指向世界系目标方向，以此摆出坐姿
func _aim(bone_name: String, target: Vector3) -> void:
	var idx := _skel.find_bone(bone_name)
	if idx < 0:
		push_warning("[Avatar] 缺少骨骼：" + bone_name)
		return
	if not _dir_local.has(idx):
		return
	var p := _skel.get_bone_parent(idx)
	var parent_cur: Quaternion = _cur_global(p) if p >= 0 else Quaternion()
	var base := _rest_local_rot[idx] as Quaternion
	var from: Vector3 = base * (_dir_local[idx] as Vector3)     # rest 姿态下、局部系里的朝向
	var t_local: Vector3 = parent_cur.inverse() * target.normalized()
	var axis := from.cross(t_local)
	if axis.length_squared() < 1e-10:
		_set_pose(idx, base)
		return
	var q_aim := Quaternion(axis.normalized(), from.angle_to(t_local))
	_set_pose(idx, q_aim * base)

# ================= 鸭子坐 =================

func _pose_w_sit() -> void:
	# 躯干：微微前倾，朝向棋盘
	_aim("J_Bip_C_Spine", Vector3(0, 1, 0.16))
	_aim("J_Bip_C_Chest", Vector3(0, 1, 0.12))
	_aim("J_Bip_C_UpperChest", Vector3(0, 1, 0.08))
	# 颈/头不在此处瞄准：头骨的「主朝向」参考子骨是发根，方向不可靠，
	# 会让下巴上翘。头颈保持与微前倾的躯干一致，朝向完全交给视线系统（_animate_face）
	# 手臂：垂到身前，双手落在腿上
	for side in [["L", 1.0], ["R", -1.0]]:
		var s: String = side[0]
		var m: float = side[1]
		_aim("J_Bip_%s_Shoulder" % s, Vector3(0.95 * m, -0.02, 0.31))
		_aim("J_Bip_%s_UpperArm" % s, Vector3(0.28 * m, -0.90, 0.33))
		_aim("J_Bip_%s_LowerArm" % s, Vector3(-0.17 * m, -0.54, 0.83))
		_aim("J_Bip_%s_Hand" % s, Vector3(-0.15 * m, -0.55, 0.82))
	# 腿：大腿向外前铺在地面，小腿折回身侧，脚收到臀旁（W 坐）
	for side in [["L", 1.0], ["R", -1.0]]:
		var s: String = side[0]
		var m: float = side[1]
		# 大腿外展幅度收敛（合拢一些）；小腿沿大腿「外侧」直直折回，避免与大腿重叠穿模
		_aim("J_Bip_%s_UpperLeg" % s, Vector3(0.46 * m, -0.22, 0.86))
		_aim("J_Bip_%s_LowerLeg" % s, Vector3(0.16 * m, -0.02, -0.99))
		_aim("J_Bip_%s_Foot" % s, Vector3(0.40 * m, -0.14, -0.90))
	# 大腿/小腿横向加粗一点，显得有肉感（长度方向不变，姿态不受影响）
	for side in ["L", "R"]:
		_fatten("J_Bip_%s_UpperLeg" % side, 1.16)
		_fatten("J_Bip_%s_LowerLeg" % side, 1.08)
	_pose_skirt()

## 骨横向加粗：只放大垂直于骨长的两个轴（先判断骨长轴在局部系的哪个方向）
func _fatten(bone_name: String, k: float) -> void:
	var idx := _skel.find_bone(bone_name)
	if idx < 0 or not _dir_local.has(idx):
		return
	var d: Vector3 = (_dir_local[idx] as Vector3).abs()
	var axis := 2
	if d.y >= d.x and d.y >= d.z:
		axis = 1
	elif d.x >= d.z:
		axis = 0
	var s := Vector3(k, k, k)
	s[axis] = 1.0
	_posed_scale[idx] = s
	_skel.set_bone_pose_scale(idx, s)

## 裙摆摊开：按「离地高度自适应」铺设——越高的骨越向下压，接近地面的骨收平，
## 于是整条裙子自然铺在地面之上（不穿地、也不悬空），头发保持自然下垂
func _pose_skirt() -> void:
	const HOVER := 0.5          # 落在空地上的裙摆停在离地约 0.5 世界单位处（布料厚度余量）
	const LIFT_MAX := 2.0       # 前方要「搭在大腿上」的额外抬升：大腿顶面约在离地 1.9
	var hip_z := seat_z
	for item in _skirt_bones_by_depth():
		var idx: int = item[1]
		var origin_world: Vector3 = _skel.global_transform * _cur_global_xform(idx).origin
		var child := _bone_child(idx)
		var seg_len := 0.35
		if child >= 0:
			var child_world: Vector3 = _skel.global_transform * _cur_global_xform(child).origin
			seg_len = maxf((child_world - origin_world).length(), 0.05)
		# 高度场：前方（大腿所在的一侧）抬到大腿之上，越往外越回落，最终铺在地面
		var dz := origin_world.z - hip_z
		var r := Vector2(origin_world.x, origin_world.z - hip_z).length()
		var lift := clampf(dz / 3.0, 0.0, 1.0) * clampf((5.5 - r) / 2.5, 0.0, 1.0)
		var want_y := floor_y + HOVER + LIFT_MAX * lift
		# 目标：骨末端落在「向外 + 该处高度」的位置 → 不论骨长，裙子搭腿后自然铺地
		var dy := clampf(want_y - origin_world.y, -seg_len * 0.95, seg_len * 0.95)
		var dh := sqrt(maxf(seg_len * seg_len - dy * dy, 0.0004))
		var o := _skel.get_bone_global_rest(idx).origin
		var az := atan2(o.x, o.z)
		var out_dir := Vector3(sin(az), 0.0, cos(az))
		_aim(_skel.get_bone_name(idx), (out_dir * dh + Vector3(0, dy, 0)).normalized())

## 骨骼的主链子骨（与 _bone_axis_world 的取向规则一致）
func _bone_child(i: int) -> int:
	var own := _skel.get_bone_name(i)
	for c in _skel.get_bone_children(i):
		if _same_family(own, _skel.get_bone_name(c)):
			return c
	for c in _skel.get_bone_children(i):
		if not _skel.get_bone_name(c).begins_with("J_Adj"):
			return c
	return -1

## 所有裙装骨（裙摆 + 外套下摆），按骨骼层级深度排序（父先于子）
func _skirt_bones_by_depth() -> Array:
	var list: Array = []
	for i in _skel.get_bone_count():
		var nm := _skel.get_bone_name(i)
		if not nm.contains("Skirt"):
			continue
		var depth := 0
		var p := _skel.get_bone_parent(i)
		while p >= 0:
			depth += 1
			p = _skel.get_bone_parent(p)
		list.append([depth, i])
	list.sort_custom(func(a, b): return int(a[0]) < int(b[0]))
	return list



## 记录基底 pose：摆姿完成后，呼吸/视线/摆动都以此为基准叠加
func _capture_bases() -> void:
	for bn in ["J_Bip_C_Spine", "J_Bip_C_Chest", "J_Bip_C_UpperChest", "J_Bip_C_Neck", "J_Bip_C_Head",
			"J_Bip_L_Shoulder", "J_Bip_R_Shoulder",
			"J_Adj_L_FaceEye", "J_Adj_R_FaceEye"]:
		var idx := _skel.find_bone(bn)
		if idx >= 0:
			_base_pose[idx] = _posed.get(idx, _rest_local_rot[idx])
	for def in _spring_defs:
		for nm in (def["names"] as Array):
			var idx := _skel.find_bone(str(nm))
			if idx >= 0 and not _base_pose.has(idx):
				_base_pose[idx] = _posed.get(idx, _rest_local_rot[idx])

## 预计算视线用的世界轴（在骨骼局部系中的表示）
func _build_axes() -> void:
	for bn in ["J_Bip_C_Spine", "J_Bip_C_Chest", "J_Bip_C_UpperChest", "J_Bip_C_Neck", "J_Bip_C_Head",
			"J_Adj_L_FaceEye", "J_Adj_R_FaceEye"]:
		var idx := _skel.find_bone(bn)
		if idx < 0 or not _base_pose.has(idx):
			continue
		var g := _cur_global(idx)
		_axis_y[idx] = g.inverse() * Vector3.UP
		_axis_x[idx] = g.inverse() * Vector3.RIGHT
		_axis_z[idx] = g.inverse() * Vector3(0, 0, 1)

# ================= 风场驱动的发/裙摆摆动 =================

func _build_chains() -> void:
	var reference_wind := Vector3(1, 0, -0.35).normalized()
	for def in _spring_defs:
		var names: Array = def["names"]
		var stiffness := float(def["stiffness"])
		var drag := float(def["drag"])
		var is_hair := str(names[0]).contains("Hair")
		var amp := 0.19 if is_hair else 0.09
		var bones: Array = []
		var root_idx := _skel.find_bone(str(names[0]))
		var chain_pos := Vector3.ZERO
		if root_idx >= 0:
			chain_pos = _skel.global_transform * _cur_global_xform(root_idx).origin
		for k in names.size():
			var idx := _skel.find_bone(str(names[k]))
			if idx < 0 or not _dir_local.has(idx):
				continue
			var g := _cur_global(idx)
			var d_world: Vector3 = g * (_dir_local[idx] as Vector3)
			var axis_world := d_world.cross(reference_wind)
			if axis_world.length_squared() < 1e-8:
				continue
			axis_world = axis_world.normalized()
			var kk := 16.0 + 34.0 * stiffness
			bones.append({
				"idx": idx,
				"axis": g.inverse() * axis_world,
				"move_dir": axis_world.cross(d_world).normalized(),
				"level": k,
				"theta": 0.0,
				"vel": 0.0,
				"k": kk,
				"c": 2.0 * sqrt(kk) * (0.45 + 0.5 * (1.0 - drag)),
				"base": _base_pose.get(idx, _rest_local_rot[idx]),
			})
		if bones.size() >= 2:
			_chains.append({"pos": chain_pos, "amp": amp, "bones": bones})

## 链根位置处的风（空间风场；没有 wind_at 时退回全局风）
func _chain_wind(pos: Vector3) -> Vector3:
	if wind_source == null:
		return Vector3.ZERO
	if wind_source.has_method("wind_at"):
		return wind_source.call("wind_at", pos)
	if wind_source.has_method("wind"):
		return wind_source.call("wind")
	return Vector3.ZERO

# ================= 放置与动画 =================

func _place() -> void:
	position = Vector3(0.0, floor_y - (HIP_H_MODEL - SIT_HIP_H) * scale_factor, seat_z)

## 注视支点取摆好位之后的头部世界坐标（视线方向由「头 → 注视点」决定）
func _update_gaze_pivot() -> void:
	var hidx := _skel.find_bone("J_Bip_C_Head")
	if hidx >= 0:
		_gaze_pivot = _skel.global_transform * _cur_global_xform(hidx).origin

## 头部世界坐标（供 Board3D 的摸头触发区判定）
func head_world_pos() -> Vector3:
	if not ok:
		return Vector3.INF
	_update_gaze_pivot()
	return _gaze_pivot

func _process(delta: float) -> void:
	if not ok:
		return
	var dt := minf(delta, 0.05)
	_t += delta
	_animate_tilt(dt)
	_animate_face(dt)
	_animate_sway(dt)
	_animate_blink(delta)
	_animate_expression(dt)
	_animate_blush(dt)
	rotation.y = 0.010 * sin(_t * 0.31)

## 呼吸 + 视线跟随（合并写入同一批骨骼，避免重复 set）
func _animate_face(dt: float) -> void:
	# —— 注视方向（低通平滑，鼠标抖动不会带得她乱晃）——
	var target := _gaze_pivot + Vector3(0, 0, 10)
	if _gaze_greet > 0.0:
		_gaze_greet -= dt                       # 开局先正对玩家
		if gaze_source != null and gaze_source.has_method("player_eye"):
			target = gaze_source.call("player_eye")
	elif gaze_source != null and gaze_source.has_method("gaze_target"):
		target = gaze_source.call("gaze_target")
	var raw := target - _gaze_pivot
	if raw.length_squared() < 1e-6:
		raw = Vector3(0, 0, 1)
	raw = raw.normalized()
	if not _gaze_ready:
		_gaze_dir = raw
		_gaze_ready = true
	_gaze_dir = _gaze_dir.slerp(raw, clampf(dt * GAZE_SMOOTH, 0.0, 1.0))
	var d := _gaze_dir
	var yaw := atan2(d.x, d.z)                    # 面向 +Z，+X 是她的左手侧，与绕 +Y 旋转同向
	# 绕世界 +X 转正角＝脸朝下：目标在下方(look_down>0)就低头，在上方就抬头
	var look_down := -asin(clampf(d.y, -1.0, 1.0)) - PITCH_BIAS
	# —— 人体式分配：头 → 颈 → 胸 → 脊柱，眼球补足残余 ——
	var y_h := clampf(yaw, -YAW_HEAD, YAW_HEAD)
	var rest := yaw - y_h
	var y_n := clampf(rest, -YAW_NECK, YAW_NECK)
	rest -= y_n
	var y_c := clampf(rest, -YAW_CHEST, YAW_CHEST)
	rest -= y_c
	var y_s := clampf(rest, -YAW_SPINE, YAW_SPINE)
	var p_h := clampf(look_down, -PITCH_UP_HEAD, PITCH_DOWN_HEAD)
	var p_n := clampf(look_down - p_h, -PITCH_UP_NECK, PITCH_DOWN_NECK)
	var p_c := clampf(look_down - p_h - p_n, -PITCH_UP_CHEST, PITCH_DOWN_CHEST)
	var y_e := clampf(yaw - (y_s + y_c + y_n + y_h), -YAW_EYE, YAW_EYE)
	var p_e := clampf(look_down - (p_h + p_n + p_c), -PITCH_EYE, PITCH_EYE)
	_dbg_gaze = Vector3(yaw, look_down, 0.0)      # 调试用：期望的视线偏航/低头量
	# —— 呼吸 ——
	var br := sin(_t * 1.15)
	var br2 := sin(_t * 1.15 - 0.45)
	_anim_pose("J_Bip_C_Spine", 0.0, y_s, 0.0)
	_anim_pose("J_Bip_C_Chest", 0.020 * br, y_c, p_c)
	_anim_pose("J_Bip_C_UpperChest", -0.012 * br, 0.0, 0.0)
	_anim_pose("J_Bip_C_Neck", -0.010 * br, y_n, p_n, _tilt_cur * 0.25)
	_anim_pose("J_Bip_C_Head", 0.008 * br, y_h, p_h, _tilt_cur)
	_anim_pose("J_Adj_L_FaceEye", 0.0, y_e, p_e)
	_anim_pose("J_Adj_R_FaceEye", 0.0, y_e, p_e)
	_breath_pose("J_Bip_L_Shoulder", Vector3(0, 0, 1), 0.022 * br2)
	_breath_pose("J_Bip_R_Shoulder", Vector3(0, 0, 1), -0.022 * br2)

## 在基底 pose 上叠加：呼吸（局部 X）+ 视线偏航（世界 Y）+ 俯仰（世界 X）+ 歪头横滚（世界 Z）
func _anim_pose(bone_name: String, breath_ang: float, yaw: float, pitch: float, roll := 0.0) -> void:
	var idx: int = _anim_idx.get(bone_name, -1)
	if idx < 0 or not _base_pose.has(idx):
		return
	var q: Quaternion = _base_pose[idx]
	if absf(breath_ang) > 1e-5:
		q = q * Quaternion(Vector3(1, 0, 0), breath_ang)
	if absf(yaw) > 1e-4:
		q = q * Quaternion(_axis_y.get(idx, Vector3.UP), yaw)
	if absf(pitch) > 1e-4:
		q = q * Quaternion(_axis_x.get(idx, Vector3.RIGHT), pitch)
	if absf(roll) > 1e-4:
		q = q * Quaternion(_axis_z.get(idx, Vector3(0, 0, 1)), roll)
	_skel.set_bone_pose_rotation(idx, q)

func _breath_pose(bone_name: String, axis: Vector3, ang: float) -> void:
	var idx: int = _anim_idx.get(bone_name, -1)
	if idx < 0 or not _base_pose.has(idx):
		return
	_skel.set_bone_pose_rotation(idx, (_base_pose[idx] as Quaternion) * Quaternion(axis, ang))

## 逐链采样风场：链首由风驱动，后续关节被前一节牵引形成波浪式滞后
func _animate_sway(dt: float) -> void:
	for ch in _chains:
		var wind := _chain_wind(ch["pos"] as Vector3)
		var speed := wind.length()
		var strength := clampf(speed / 2.4, 0.0, 1.15)
		var hat := wind / maxf(speed, 1e-6)
		var amp := float(ch["amp"])
		var prev := 0.0
		var prev_level := -1
		for b in (ch["bones"] as Array):
			var d: Dictionary = b
			var lvl := int(d["level"])
			if lvl <= prev_level:
				prev = 0.0
			var drive: float
			if lvl == 0:
				drive = amp * strength * maxf(0.0, hat.dot(d["move_dir"] as Vector3))
			else:
				drive = prev * 0.92
			prev_level = lvl
			var k := float(d["k"])
			var c := float(d["c"])
			var acc := k * (drive - float(d["theta"])) - c * float(d["vel"])
			var vel := float(d["vel"]) + acc * dt
			var theta := clampf(float(d["theta"]) + vel * dt, -amp * 2.0, amp * 2.0)
			d["vel"] = vel
			d["theta"] = theta
			_skel.set_bone_pose_rotation(int(d["idx"]),
				(d["base"] as Quaternion) * Quaternion(d["axis"] as Vector3, theta))
			prev = theta

func _animate_blink(delta: float) -> void:
	if _face == null or _blink_shape < 0:
		return
	if _blink_t < 0.0:
		_blink_wait -= delta
		if _blink_wait <= 0.0:
			_blink_t = 0.0
		else:
			return
	_blink_t += delta
	var v := 0.0
	if _blink_t < 0.055:
		v = _blink_t / 0.055
	elif _blink_t < 0.10:
		v = 1.0
	elif _blink_t < 0.22:
		v = 1.0 - (_blink_t - 0.10) / 0.12
	else:
		_blink_t = -1.0
		_blink_wait = randf_range(2.2, 4.8)
		if randf() < 0.3:
			_blink_wait = 0.16          # 偶尔连眨两下
		v = 0.0
	_face.set_blend_shape_value(_blink_shape, clampf(v, 0.0, 1.0))

# ================= 闲置 / 说话时的歪头小动作 =================

## 闲置状态（玩家长时间没操作，由主场景置位）：会不时歪歪头
func set_idle(v: bool) -> void:
	_idle = v
	if v:
		_tilt_next = 0.0                  # 进入闲置后先歪一次

## 朗读中：说话时轻轻歪头
func set_speaking(v: bool) -> void:
	_speaking = v

## 调试：常驻歪头角度（度），配合 --tilt= 截图核对
func debug_tilt(deg: float) -> void:
	_tilt_debug = deg_to_rad(deg)

func _animate_tilt(dt: float) -> void:
	if absf(_tilt_debug) > 1e-4:
		_tilt_cur = _tilt_debug
		return
	if _idle or _speaking:
		if _tilt_active:
			if _t > _tilt_phase_until:
				_tilt_active = false
				_tilt_target = 0.0
				_tilt_next = _t + randf_range(3.0, 6.5)
		elif _t > _tilt_next:
			_tilt_active = true
			var amp := 0.14 if _idle else 0.065     # 闲置时歪得明显些，说话时轻轻歪
			_tilt_target = randf_range(amp * 0.6, amp) * (1.0 if randf() < 0.5 else -1.0)
			_tilt_phase_until = _t + randf_range(1.1, 2.2)
	else:
		_tilt_active = false
		_tilt_target = 0.0
		_tilt_next = _t + 0.6
	_tilt_cur += (_tilt_target - _tilt_cur) * clampf(dt * 3.2, 0.0, 1.0)

# ================= 表情反应（Estarth 棋的不同表现） =================

## 思考中（KataGo 分析 / LLM 生成）保持中性表情，结束后回到常态微笑
func set_thinking(v: bool) -> void:
	_expr_thinking = v
	_update_baseline()

func _update_baseline() -> void:
	_expr_baseline.clear()
	if _expr_thinking or _expr_shapes.is_empty():
		return
	var smile := int(_expr_shapes.get("Fcl_ALL_Fun", -1))    # 常态微微笑
	if smile >= 0:
		_expr_baseline[smile] = 0.28

# —— 脸红：这个 VRM 没有 blush 形变，用脸部材质叠一层淡粉近似 ——
var _blush_cur := 0.0
var _blush_target := 0.0
var _blush_mats: Array = []              # [StandardMaterial3D, 原始 albedo_color]

## 摸头等亲密互动时脸红（v=false 自然褪去）
func set_blush(v: bool) -> void:
	_blush_target = 1.0 if v else 0.0

func _animate_blush(dt: float) -> void:
	if _blush_mats.is_empty():
		return
	var rate := clampf(dt * 2.6, 0.0, 1.0)
	_blush_cur += (_blush_target - _blush_cur) * rate
	if absf(_blush_target - _blush_cur) < 0.01:
		_blush_cur = _blush_target
	for e in _blush_mats:
		var m: StandardMaterial3D = e[0]
		var base: Color = e[1]
		m.albedo_color = base.lerp(Color(1.0, 0.58, 0.63, base.a), _blush_cur * 0.5)

## kind: delight（好手）/ pleased（不错）/ puzzled（略意外）/ worried（俗手、明显失着）
##       sad（难过）/ laugh（被逗笑）
func play_expression(kind: String) -> void:
	var steps := _expr_steps_for(kind)
	if steps.is_empty() or _expr_shapes.is_empty():
		return
	_expr_queue = steps
	_expr_stage = 0
	_expr_t = 0.0
	_expr_targets.clear()
	_apply_expr_stage()

func _expr_steps_for(kind: String) -> Array:
	match kind:
		"delight":
			return [["Fcl_ALL_Joy", 0.90, 0.30, 1.6, 0.55]]
		"pleased":
			return [["Fcl_ALL_Fun", 0.80, 0.35, 1.3, 0.55]]      # 比常态微笑明显一档
		"puzzled":
			return [["Fcl_ALL_Surprised", 0.50, 0.25, 0.5, 0.40]]
		"worried":
			return [["Fcl_ALL_Surprised", 0.85, 0.20, 0.5, 0.30], ["Fcl_ALL_Sorrow", 0.50, 0.50, 1.2, 0.70]]
		"sad":
			return [["Fcl_ALL_Sorrow", 0.55, 0.50, 1.6, 0.80]]
		"laugh":
			return [["Fcl_ALL_Joy", 0.85, 0.18, 0.8, 0.50]]
	return []

func _apply_expr_stage() -> void:
	_expr_targets.clear()
	if _expr_stage >= _expr_queue.size():
		return                                   # 结束：目标清零，表情自然回落
	var s: Array = _expr_queue[_expr_stage]
	var idx := int(_expr_shapes.get(str(s[0]), -1))
	if idx < 0:
		return
	_expr_targets[idx] = float(s[1])
	if not _expr_cur.has(idx):
		_expr_cur[idx] = 0.0

func _animate_expression(dt: float) -> void:
	# 目标 = 常态基线 ∪ 当前表情步骤（步骤优先）
	var targets := _expr_baseline.duplicate()
	for k in _expr_targets.keys():
		targets[k] = _expr_targets[k]
	if not _expr_queue.is_empty():
		_expr_t += dt
		if _expr_stage < _expr_queue.size():
			var s: Array = _expr_queue[_expr_stage]
			if _expr_t >= float(s[2]) + float(s[3]) + float(s[4]):
				_expr_t = 0.0
				_expr_stage += 1
				_apply_expr_stage()
		else:
			_expr_queue = []
	var rate := clampf(dt * 9.0, 0.0, 1.0)
	for key in targets.keys():
		var idx := int(key)
		if not _expr_cur.has(idx):
			_expr_cur[idx] = 0.0
		var cur := float(_expr_cur[idx])
		var tgt := float(targets[idx])
		if absf(cur - tgt) > 0.004:
			cur += (tgt - cur) * rate
			_expr_cur[idx] = cur
			_write_shape(idx, cur)
	for key in _expr_cur.keys():                 # 已结束的表情自然回落到基线/零
		var idx := int(key)
		if targets.has(idx):
			continue
		var cur := float(_expr_cur[key])
		if cur > 0.004:
			cur = maxf(0.0, cur * (1.0 - rate))
			_expr_cur[idx] = cur
			_write_shape(idx, cur)

func _write_shape(idx: int, v: float) -> void:
	if _face != null:
		_face.set_blend_shape_value(idx, clampf(v, 0.0, 1.0))

# ================= 调试 =================

func debug_dump() -> void:
	if not ok:
		print("[avatar] 未加载")
		return
	print("[avatar] scale=", scale_factor, " pos=", position)
	for bn in ["J_Bip_C_Hips", "J_Bip_C_Head", "J_Bip_L_LowerLeg", "J_Bip_L_Foot", "J_Bip_L_Hand"]:
		var idx := _skel.find_bone(bn)
		if idx < 0:
			continue
		var w := _skel.global_transform * _skel.get_bone_global_pose(idx)
		print("[avatar] %-22s world=%s" % [bn, str(w.origin)])
	# 裙摆边界：检查是否穿地 / 越过棋盘远端（-9.5）
	var min_y := 1e9
	var max_z := -1e9
	for def in _spring_defs:
		if not str((def["names"] as Array)[0]).contains("Skirt"):
			continue
		for nm in (def["names"] as Array):
			var i2 := _skel.find_bone(str(nm))
			if i2 >= 0:
				var wp := (_skel.global_transform * _skel.get_bone_global_pose(i2)).origin
				min_y = minf(min_y, wp.y)
				max_z = maxf(max_z, wp.z)
	print("[avatar] skirt min_y=%.3f (地面 %.2f)  max_z=%.3f (棋盘远端 -9.50)" % [min_y, floor_y, max_z])
	print("[avatar] chains=", _chains.size(), " faces=", _face != null, " exprs=", _expr_shapes.keys())