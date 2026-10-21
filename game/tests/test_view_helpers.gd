extends SceneTree
## 视角便捷 + 悬停坐标/手数（v3.7.0 任务 G）前端门卫 —— **纯逻辑**，不渲染、不依赖场景树、不合成输入事件。
## 任务书点名的三个用例名**逐字照用**（空点 / 已有棋子带手数 / 盘外空串）；
## 另覆盖：手数由后端 `move_seq`（GTP 手顺，含 "pass"）推、四角轮转的方位角（含越界/脏值不越界）、
## 一键回初始取景（`capture_home_view` → `reset_view`）。
## `Board3D` 有 `class_name`：静态纯逻辑直接调；实例部分用 `Board3D.new()`（不 `_ready`、不进场景树）。
## 运行：godot --headless --path game --script res://tests/test_view_helpers.gd

var _total := 0
var _fail := 0

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[view] 视角便捷 + 悬停坐标/手数自检（文案三分支 / move_seq 推手数 / 四角 / 回初始取景）")
	test_tip_text_for_empty_point()
	test_tip_text_for_occupied_point_includes_move_number()
	test_tip_text_outside_board_is_empty()
	_test_move_no_from_seq()
	_test_corners()
	_test_reset_view()
	_test_camera_flight()

	if _fail == 0:
		print("[view] PASS：", _total, " 项断言全部通过")
	else:
		print("[view] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

## ① 空点：既有措辞逐字符沿用
func test_tip_text_for_empty_point() -> void:
	_check("test_tip_text_for_empty_point：空点给「D4 · 点击落子」",
			Board3D.tip_text_at(3, 15, 0) == "D4 · 点击落子")
	_check("① 列跳过 I（A..T）：(8,0) 是 J19", Board3D.tip_text_at(8, 0, 0) == "J19 · 点击落子")
	_check("① 角上是 A19 / T1", Board3D.tip_text_at(0, 0, 0) == "A19 · 点击落子"
			and Board3D.tip_text_at(18, 18, 0) == "T1 · 点击落子")
	_check("① 脏手数（负数）当空点处理，不当成「第 -1 手」", Board3D.tip_text_at(3, 15, -1) == "D4 · 点击落子")

## ① 已有棋子：坐标 + 手数
func test_tip_text_for_occupied_point_includes_move_number() -> void:
	_check("test_tip_text_for_occupied_point_includes_move_number：已有棋子给「D4 · 第 3 手」",
			Board3D.tip_text_at(3, 15, 3) == "D4 · 第 3 手")
	_check("① 两位数手数照样渲染", Board3D.tip_text_at(3, 15, 17) == "D4 · 第 17 手")

## ① 盘外：空串
func test_tip_text_outside_board_is_empty() -> void:
	_check("test_tip_text_outside_board_is_empty：四个方向出界都给空串",
			Board3D.tip_text_at(-1, 0, 0) == "" and Board3D.tip_text_at(19, 0, 0) == ""
			and Board3D.tip_text_at(0, -1, 0) == "" and Board3D.tip_text_at(0, 19, 0) == "")
	_check("① 盘外即便带手数也给空串（不硬造坐标）", Board3D.tip_text_at(-3, 30, 9) == "")

## ② 手数由 `move_seq` 推：取最后一次出现（提子后再下以最近一手为准）
func _test_move_no_from_seq() -> void:
	var seq := ["Q16", "D4", "pass", "Q4", "D4"]
	_check("② 手数 = 该点在 move_seq 里的最后一次出现的序号（1 基）",
			Board3D.move_no_in_seq(3, 15, seq) == 5)
	_check("② pass 也占一手：Q16 在第 1 手", Board3D.move_no_in_seq(15, 3, seq) == 1)
	_check("② 不在序列里的点 ⇒ 0", Board3D.move_no_in_seq(9, 9, seq) == 0)
	_check("② 大小写 / 空白容错（小写 d4 也认）", Board3D.move_no_in_seq(3, 15, [" d4 "]) == 1)
	_check("② 脏值不抛：非数组 / 元素非字符串 / 盘外一律 0",
			Board3D.move_no_in_seq(3, 15, null) == 0
			and Board3D.move_no_in_seq(3, 15, "oops") == 0
			and Board3D.move_no_in_seq(3, 15, [null, 7, "D4"]) == 3
			and Board3D.move_no_in_seq(-1, 0, seq) == 0)

	# 实例入口（`Board3D.new()`：不 `_ready`、不进场景树）
	var b := Board3D.new()
	b.set_move_seq(seq)
	_check("② 实例：空点给「点击落子」", b.tip_text(3, 3) == "D16 · 点击落子")
	# `set_stones` 依赖 `_ready()` 建的节点，这里直接把占用表注进去（字段就是普通 Dictionary）
	b._occupied[Vector2i(3, 15)] = true
	_check("② 实例：已有棋子给坐标 + 手数", b.tip_text(3, 15) == "D4 · 第 5 手")
	b.set_move_seq("脏值")
	_check("② 实例：手顺丢了也不报错（退成空点文案，坐标照给）",
			b.tip_text(3, 15) == "D4 · 点击落子" and b.tip_text(3, 3) == "D16 · 点击落子")
	b.free()

## ③ 四角轮转：方位角每 +1 走 90°；越界 / 脏值按 4 取模不越界
func _test_corners() -> void:
	_check("③ 四角方位角 = 45° + 90°×角号",
			is_equal_approx(Board3D.corner_yaw_deg(0), 45.0)
			and is_equal_approx(Board3D.corner_yaw_deg(1), 135.0)
			and is_equal_approx(Board3D.corner_yaw_deg(2), 225.0)
			and is_equal_approx(Board3D.corner_yaw_deg(3), 315.0))
	_check("③ 越界绕回：4 ⇒ 0、-1 ⇒ 3（脏值不越界）",
			is_equal_approx(Board3D.corner_yaw_deg(4), Board3D.corner_yaw_deg(0))
			and is_equal_approx(Board3D.corner_yaw_deg(-1), Board3D.corner_yaw_deg(3))
			and is_equal_approx(Board3D.corner_yaw_deg(99), Board3D.corner_yaw_deg(3)))
	_check("③ 角号只有四个（CORNER_COUNT=4）", Board3D.CORNER_COUNT == 4
			and Board3D.CORNER_NAMES.size() == 4 and Board3D.CORNER_GTPS.size() == 4)

## ④ 一键回初始取景
func _test_reset_view() -> void:
	var b := Board3D.new()
	b.capture_home_view()
	var home := b.view_state()
	_check("④ 启动取景就是默认取景（yaw 0° / pitch 38° / dist 42）",
			is_equal_approx(float(home["yaw_deg"]), 0.0)
			and is_equal_approx(float(home["pitch_deg"]), 38.0)
			and is_equal_approx(float(home["dist"]), 42.0))
	b.look_at_corner(1)
	_check("④ look_at_corner(1)：方位角 135°、俯仰与距离不动",
			is_equal_approx(float(b.view_state()["yaw_deg"]), 135.0)
			and is_equal_approx(float(b.view_state()["pitch_deg"]), 38.0)
			and is_equal_approx(float(b.view_state()["dist"]), 42.0))
	b.set_view(200.0, 10.0, 30.0)
	b.reset_view()
	_check("④ reset_view：转乱 / 缩放过的取景逐项回到捕获值", b.view_state() == home)
	b.free()

## ⑤ 主界面 ⇄ 对局的运镜（v3.11.3）：远看取景 / 回初始取景 / 缓动曲线 / 飞行状态
func _test_camera_flight() -> void:
	var b := Board3D.new()
	b.set_view(0.0, 27.0, 66.0)
	_check("⑤ 远看取景原样落下（yaw 0 / pitch 27 / dist 66）",
			is_equal_approx(float(b.view_state()["yaw_deg"]), 0.0)
			and is_equal_approx(float(b.view_state()["pitch_deg"]), 27.0)
			and is_equal_approx(float(b.view_state()["dist"]), 66.0))
	b.fly_home(0.0)                                   # 没有相机：立刻到位、不抛
	_check("⑤ fly_home(0) 立刻回到初始取景（正对棋盘）",
			is_equal_approx(float(b.view_state()["pitch_deg"]), 38.0)
			and is_equal_approx(float(b.view_state()["dist"]), 42.0))
	b.fly_to(0.0, 27.0, 66.0, 1.0)
	_check("⑤ 给出时长时处于「飞行中」", b.is_flying())
	b.fly_to(0.0, 27.0, 66.0, 0.0)
	_check("⑤ 时长 0：立刻到位、不再是飞行中", not b.is_flying()
			and is_equal_approx(float(b.view_state()["dist"]), 66.0))
	_check("⑤ 缓动曲线：f(0)=0 / f(0.5)=0.5 / f(1)=1", is_zero_approx(Board3D.fly_ease(0.0))
			and is_equal_approx(Board3D.fly_ease(0.5), 0.5)
			and is_equal_approx(Board3D.fly_ease(1.0), 1.0))
	var monotonic := true
	var prev := -1.0
	for i in 21:
		var e: float = Board3D.fly_ease(float(i) / 20.0)
		if e < prev - 0.0001:
			monotonic = false
		prev = e
	_check("⑤ 缓动曲线单调不回弹", monotonic)
	_check("⑤ 越界进度夹到 0..1（脏值不把相机甩出去）",
			is_zero_approx(Board3D.fly_ease(-3.0)) and is_equal_approx(Board3D.fly_ease(9.0), 1.0))
	b.free()
