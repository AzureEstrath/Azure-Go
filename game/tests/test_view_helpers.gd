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
	_test_corner_flight()
	_test_pat_zone_distance()
	_test_ownership_color()
	_test_estimate_helpers()
	_test_idle_talk_delay()
	_test_hidden_launcher_args()
	_test_praise_keywords()

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

## ⑨ 闲置搭话节奏（人格化）：白天 18 秒首句；深夜 ×1.5；轮到玩家长考时至少 30 秒；脏 roll 不越界。
func _test_idle_talk_delay() -> void:
	var main_script := preload("res://scripts/ui/Main.gd")
	_check("⑨ 白天首句 18 秒", is_equal_approx(main_script.idle_talk_delay(14, false, true, 0.0), 18.0))
	_check("⑨ 深夜首句 ×1.5 = 27 秒", is_equal_approx(main_script.idle_talk_delay(23, false, true, 0.0), 27.0))
	_check("⑨ 轮到玩家长考：至少 30 秒",
		main_script.idle_talk_delay(14, true, true, 0.0) >= 30.0
		and main_script.idle_talk_delay(23, true, true, 0.0) >= 30.0)
	_check("⑨ 后续间隔 45~80 秒（roll 两端）",
		is_equal_approx(main_script.idle_talk_delay(14, false, false, 0.0), 45.0)
		and is_equal_approx(main_script.idle_talk_delay(14, false, false, 1.0), 80.0))
	_check("⑨ 脏 roll（NAN / 越界）不越界",
		is_equal_approx(main_script.idle_talk_delay(14, false, false, NAN), 62.5)
		and is_equal_approx(main_script.idle_talk_delay(14, false, false, 5.0), 80.0))


## ⑩ wscript 隐藏启动参数：第一个必须是 VBS，第二个是 dev_local.ps1；顺序错了会报
## 「没有 .ps1 的脚本引擎」。质量引擎 / 等待开关按原语义追加。
func _test_hidden_launcher_args() -> void:
	var main_script := preload("res://scripts/ui/Main.gd")
	var a := main_script.hidden_launcher_args("C:/x/run.vbs", "C:/x/dev_local.ps1", true, true)
	_check("⑩ wscript 参数：VBS 在前、dev_local.ps1 在后",
		a.size() == 3 and a[0] == "C:/x/run.vbs" and a[1] == "C:/x/dev_local.ps1")
	_check("⑩ 等待质量引擎：带 -QualityTts、不带 -NoWaitTts",
		a.has("-QualityTts") and not a.has("-NoWaitTts"))
	var b := main_script.hidden_launcher_args("v", "s", true, false)
	_check("⑩ 不等待：同时带 -NoWaitTts", b.has("-QualityTts") and b.has("-NoWaitTts"))
	var c := main_script.hidden_launcher_args("v", "s", false, false)
	_check("⑩ 基础服务：不带质量 / 等待开关", not c.has("-QualityTts") and not c.has("-NoWaitTts"))
	# ⑪ 启动预热：-TtsOnly 只起语音（主界面 / 设置面板期间），且排在质量 / 等待开关之前。
	var d := main_script.hidden_launcher_args("v", "s", true, true, true)
	_check("⑪ 预热质量引擎：-TtsOnly 在前，随后 -QualityTts、不带 -NoWaitTts",
		d.size() == 4 and d[0] == "v" and d[1] == "s" and d[2] == "-TtsOnly"
		and d[3] == "-QualityTts" and not d.has("-NoWaitTts"))
	var e := main_script.hidden_launcher_args("v", "s", false, true, true)
	_check("⑪ 预热轻量引擎：只多一个 -TtsOnly",
		e.size() == 3 and e[2] == "-TtsOnly" and not e.has("-QualityTts"))
	_check("⑪ 完整启动不带 -TtsOnly（预热开关只属于预热入口）", not c.has("-TtsOnly"))
	_check("⑪ Azure 克隆 / SFT 预设要质量引擎",
		main_script.local_voice_wants_quality("azure")
		and main_script.local_voice_wants_quality("中文女"))
	_check("⑪ 轻量音色不升级质量（xiaoya / melo / 空）",
		not main_script.local_voice_wants_quality("xiaoya")
		and not main_script.local_voice_wants_quality("melo")
		and not main_script.local_voice_wants_quality(""))


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

## ⑤′ C 键平滑转视角：给时长时先进入飞行，推进后落到目标角；俯仰与距离不丢
func _test_corner_flight() -> void:
	var b := Board3D.new()
	b.fly_to_corner(1, 0.0)
	_check("⑤′ fly_to_corner(1, 0)：立即到 135°",
			not b.is_flying()
			and is_equal_approx(float(b.view_state()["yaw_deg"]), 135.0)
			and is_equal_approx(float(b.view_state()["pitch_deg"]), 38.0)
			and is_equal_approx(float(b.view_state()["dist"]), 42.0))
	b.fly_to_corner(2, 0.4)
	_check("⑤′ 给时长时进入飞行，朝向不瞬移",
			b.is_flying() and is_equal_approx(float(b.view_state()["yaw_deg"]), 135.0))
	b._step_fly(0.4)
	_check("⑤′ 飞行结束落到 225°，俯仰 / 距离保持",
			not b.is_flying()
			and is_equal_approx(float(b.view_state()["yaw_deg"]), 225.0)
			and is_equal_approx(float(b.view_state()["pitch_deg"]), 38.0)
			and is_equal_approx(float(b.view_state()["dist"]), 42.0))
	b.free()

## ④′ 摸头判定：头出画时用「头 → 髋 → 脚」折线，点在可见的身体上仍算命中
func _test_pat_zone_distance() -> void:
	_check("④′ 线段上的点距离为 0",
			is_zero_approx(Board3D._point_segment_distance(Vector2(50, 50), Vector2(0, 50), Vector2(100, 50))))
	_check("④′ 线段外按垂直距离算（10px）",
			is_equal_approx(Board3D._point_segment_distance(Vector2(50, 60), Vector2(0, 50), Vector2(100, 50)), 10.0))
	_check("④′ 头在屏幕上方、髋在下方：点身体中段仍命中（距离 0）",
			is_zero_approx(Board3D._point_segment_distance(Vector2(50, 40), Vector2(50, -200), Vector2(50, 300))))

## ⑥ 领地热力图颜色（v3.11.5）：+ 黑 / - 白 / 0 透明；脏值不抛、越界夹到 [-1,1]
func _test_ownership_color() -> void:
	var black := Board3D.ownership_color(0.8)
	var white := Board3D.ownership_color(-0.8)
	var clear := Board3D.ownership_color(0.0)
	_check("⑥ 黑色领地：深色 + 透明度随 |v|", black.r < 0.2 and black.a > 0.3 and black.a < 0.5)
	_check("⑥ 白色领地：亮色 + 透明度随 |v|", white.r > 0.9 and white.a > 0.3 and white.a < 0.5)
	_check("⑥ 归属接近 0：完全透明", is_zero_approx(clear.a))
	_check("⑥ 越界 / NaN 不抛且夹到合法范围",
			Board3D.ownership_color(9.0).a <= 0.50 + 0.001
			and Board3D.ownership_color(-9.0).a <= 0.50 + 0.001
			and is_zero_approx(Board3D.ownership_color(NAN).a))
	# 实例：不 `_ready`（没有平面 / 纹理）也要能写；第二次走复用画布 + update
	var b := Board3D.new()
	var vals: Array = []
	vals.resize(361)
	vals.fill(0.0)
	vals[0] = 0.8
	b.set_ownership(vals)
	var ok_first := b.ownership_values().size() == 361
	vals[0] = -0.8
	b.set_ownership(vals)
	_check("⑥′ 领地图写入不依赖场景树、可重复更新",
			ok_first and b.ownership_values().size() == 361
			and is_equal_approx(float(b.ownership_values()[0]), -0.8))
	b.set_ownership([0.0])
	_check("⑥′ 形状不对（非 361）⇒ 清空而不是崩", b.ownership_values().is_empty())
	b.free()

## ⑦ 局面点目（v3.12.0 采纳）：面板两行文案 + 棋盘小方块的格位容错。
## `Main.gd` 没有 `class_name` ⇒ `preload` 调静态函数（同 test_joseki_level 的做法）。
func _test_estimate_helpers() -> void:
	var m := preload("res://scripts/ui/Main.gd")
	_check("⑦ 点目计数：黑 41 目 · 白 4 目",
			m.estimate_count_line(41, 4) == "黑 41 目 · 白 4 目")
	_check("⑦ 计数认 JSON 的 float 数字", m.estimate_count_line(40.6, 3.9) == "黑 40 目 · 白 3 目")
	_check("⑦ 计数脏值 ⇒ 「形势: --」，不硬报 0",
			m.estimate_count_line(null, 4) == "形势: --"
			and m.estimate_count_line("x", -5) == "形势: --")
	_check("⑦ 黑领先：正数 + 贴目", m.estimate_lead_line(29.5, 7.5) == "黑领先 29.5 目（贴 7.5 目）")
	_check("⑦ 白领先：负数取绝对值", m.estimate_lead_line(-3.0, 6.5) == "白领先 3.0 目（贴 6.5 目）")
	_check("⑦ 接近均势 / 缺数据 / 缺贴目各有话说",
			m.estimate_lead_line(0.0, 7.5) == "双方接近（贴 7.5 目）"
			and m.estimate_lead_line(null, 7.5) == "--（贴 7.5 目）"
			and m.estimate_lead_line("x", 7.5) == "--（贴 7.5 目）"
			and m.estimate_lead_line(2.0, null) == "黑领先 2.0 目")
	_check("⑦ 点目格：合法 / 越界 / 颜色 / 非数组",
			Board3D.estimate_cell_ok([3, 15, "black"])
			and Board3D.estimate_cell_ok([0.0, 18.0, "white"])
			and not Board3D.estimate_cell_ok([-1, 0, "black"])
			and not Board3D.estimate_cell_ok([0, 19, "white"])
			and not Board3D.estimate_cell_ok([3, 3, "red"])
			and not Board3D.estimate_cell_ok([3, 3])
			and not Board3D.estimate_cell_ok("oops"))

func _test_praise_keywords() -> void:
	var m := preload("res://scripts/ui/Main.gd")
	_check("⑧ 被夸关键词：喜欢 / 厉害 / 谢谢 命中",
		m.looks_like_praise("我喜欢你下棋的样子")
		and m.looks_like_praise("这手真厉害")
		and m.looks_like_praise("谢谢 Azure"))
	_check("⑧ 普通聊天 / 空串不误判",
		not m.looks_like_praise("这手该怎么下")
		and not m.looks_like_praise("")
		and not m.looks_like_praise("   "))
