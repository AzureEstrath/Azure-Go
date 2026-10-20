extends SceneTree
## 定式导学档位映射（v3.7.0 任务 F 抽出的纯函数 / R-F-2 #4 补门卫）—— **纯逻辑**，不渲染、不依赖场景树。
## `joseki_level_index(teach, rate)` 把「开关 + 频率」反推成下拉框档位（0=关闭 / 1=低 25% / 2=中 50% / 3=高 75% / 4=总是 100%）：
## 覆盖关闭档恒 0、四档精确命中、**并列取中**（两侧等距时优先中间档）、越界与脏值不越界（结果恒在 0..4）。
## `Main.gd` 没有 `class_name` ⇒ 只能 `preload` 调它的**静态函数**（V11）。
## 运行：godot --headless --path game --script res://tests/test_joseki_level.gd

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
	print("[josekilevel] 定式导学档位映射自检（关闭档 / 精确命中 / 并列取中 / 越界不越界）")
	var m := preload("res://scripts/ui/Main.gd")

	# —— 关闭档：恒 0，且**不看频率**（频率还留着，重新开启时能回来） ——
	_check("teach=false ⇒ 0（频率是多少都一样）",
			m.joseki_level_index(false, 0.75) == 0
			and m.joseki_level_index(false, 0.0) == 0
			and m.joseki_level_index(false, NAN) == 0)
	_check("teach=true 时结果恒在 1..4（0 只表示「关闭」）",
			m.joseki_level_index(true, 0.25) >= 1 and m.joseki_level_index(true, 1.0) <= 4)

	# —— 四档精确命中（与 JOSEKI_RATES = [0.25, 0.5, 0.75, 1.0] 一一对应） ——
	_check("0.25 ⇒ 1（低）", m.joseki_level_index(true, 0.25) == 1)
	_check("0.5 ⇒ 2（中）", m.joseki_level_index(true, 0.5) == 2)
	_check("0.75 ⇒ 3（高）", m.joseki_level_index(true, 0.75) == 3)
	_check("1.0 ⇒ 4（总是）", m.joseki_level_index(true, 1.0) == 4)

	# —— 并列取中：两侧**同时等距**时优先中间档（最难的一条，注释里就是这么写的） ——
	_check("0.375（1 与 2 等距）⇒ 2", m.joseki_level_index(true, 0.375) == 2)
	_check("0.625（2 与 3 等距）⇒ 2", m.joseki_level_index(true, 0.625) == 2)
	_check("0.875（3 与 4 等距）⇒ 3（并列仍优先靠中）", m.joseki_level_index(true, 0.875) == 3)

	# —— 非并列：取最近的一档（两侧都要各验一次，别只验往中档偏） ——
	_check("0.3 更近 0.25 ⇒ 1；0.7 更近 0.75 ⇒ 3",
			m.joseki_level_index(true, 0.3) == 1 and m.joseki_level_index(true, 0.7) == 3)
	_check("0.125 更近 0.25 ⇒ 1；0.8 更近 0.75 ⇒ 3",
			m.joseki_level_index(true, 0.125) == 1 and m.joseki_level_index(true, 0.8) == 3)

	# —— 越界 / 脏值不越界：0.0 / 1.0 / ±5 / 极大 / NaN / INF 都落在 1..4（关闭档是 0） ——
	_check("0.0 ⇒ 1、1.0 ⇒ 4（落在两端档，不越界）",
			m.joseki_level_index(true, 0.0) == 1 and m.joseki_level_index(true, 1.0) == 4)
	_check("越界 -5 ⇒ 1、5 ⇒ 4（远离棋盘一侧也只会贴到端点档）",
			m.joseki_level_index(true, -5.0) == 1 and m.joseki_level_index(true, 5.0) == 4)
	_check("脏值 NaN / INF 不越界（落回中间档 2，不抛）",
			m.joseki_level_index(true, NAN) == 2 and m.joseki_level_index(true, INF) == 2)
	var sweep := [0.0, 0.1, 0.25, 0.333, 0.375, 0.5, 0.625, 0.75, 0.9, 1.0, -100.0, 1e9, NAN, INF]
	var all_in_range := true
	var worst := ""
	for r in sweep:
		var idx: int = m.joseki_level_index(true, r)
		if idx < 1 or idx > 4:
			all_in_range = false
			worst = str(r)
	_check("扫描 14 个值（含脏值）：teach=true 恒在 1..4%s" % ("（越界：" + worst + "）" if worst != "" else ""),
			all_in_range)

	if _fail == 0:
		print("[josekilevel] PASS：", _total, " 项断言全部通过")
	else:
		print("[josekilevel] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
