extends SceneTree
## 局面点目（v3.12.0）前端门卫 —— **纯逻辑**，不渲染、不依赖场景树。
## 覆盖：形势面板两条文案（数字 / 脏值 / 贴目）与 `Board3D.estimate_cell_ok` 的坐标校验
## （JSON 里的数字是 float，也要认；越界 / 错色 / 缺键一律丢）。
## `Main.gd` / `Board3D.gd` 都没有 `class_name` ⇒ 只能 `preload` 调它们的**静态函数**。
## 运行：godot --headless --path game --script res://tests/test_estimate.gd

var _total := 0
var _fail := 0
var _main = null
var _board = null

func _initialize() -> void:
	_main = preload("res://scripts/ui/Main.gd")
	_board = preload("res://scripts/ui/Board3D.gd")
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[estimate] 局面点目自检（面板文案 / 脏值安全 / 格子校验 / 棋盘标记）")
	_test_count_line()
	_test_lead_line()
	_test_cell_ok()
	_test_board_markers()

	if _fail == 0:
		print("[estimate] PASS：", _total, " 项断言全部通过")
	else:
		print("[estimate] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

## 双方点数那一行：JSON 数字是 float，显示成整数；脏值 ⇒ 「形势: --」
func _test_count_line() -> void:
	_check("数字照旧：黑 41 / 白 4 ⇒ 「黑 41 目 · 白 4 目」",
			_main.estimate_count_line(41, 4) == "黑 41 目 · 白 4 目")
	_check("JSON 的 float 也认（41.0 / 4.0 不显示小数点）",
			_main.estimate_count_line(41.0, 4.0) == "黑 41 目 · 白 4 目")
	_check("0 也照旧显示（一面还没点出东西时）",
			_main.estimate_count_line(0, 0) == "黑 0 目 · 白 0 目")
	_check("脏值（null / 字符串 / 数组 / NaN / INF）⇒ 「形势: --」，不抛",
			_main.estimate_count_line(null, 4) == "形势: --"
			and _main.estimate_count_line("41", 4) == "形势: --"
			and _main.estimate_count_line([], {}) == "形势: --"
			and _main.estimate_count_line(NAN, 4) == "形势: --"
			and _main.estimate_count_line(41, INF) == "形势: --")

## 领先那一行：正数＝黑好、负数＝白好（与右栏目差同一口径）；含贴目说明
func _test_lead_line() -> void:
	_check("黑领先：+29.5 贴 7.5 ⇒ 「黑领先 29.5 目（贴 7.5 目）」",
			_main.estimate_lead_line(29.5, 7.5) == "黑领先 29.5 目（贴 7.5 目）")
	_check("白领先：-3.5 ⇒ 「白领先 3.5 目（贴 7.5 目）」（不带负号）",
			_main.estimate_lead_line(-3.5, 7.5) == "白领先 3.5 目（贴 7.5 目）")
	_check("接近均势（|lead| < 0.5）⇒ 「双方接近」",
			_main.estimate_lead_line(0.0, 7.5) == "双方接近（贴 7.5 目）"
			and _main.estimate_lead_line(0.4, 7.5) == "双方接近（贴 7.5 目）"
			and _main.estimate_lead_line(-0.4, 7.5) == "双方接近（贴 7.5 目）")
	_check("边界 0.5 不算接近（有明确领先方）",
			_main.estimate_lead_line(0.5, 7.5) == "黑领先 0.5 目（贴 7.5 目）"
			and _main.estimate_lead_line(-0.5, 7.5) == "白领先 0.5 目（贴 7.5 目）")
	_check("目差脏值 ⇒ 「--」但仍带贴目；贴目脏值 ⇒ 只给领先文案",
			_main.estimate_lead_line(null, 7.5) == "--（贴 7.5 目）"
			and _main.estimate_lead_line("abc", 7.5) == "--（贴 7.5 目）"
			and _main.estimate_lead_line(2.0, null) == "黑领先 2.0 目")

## 棋盘标记：真的建出两个网格（黑 / 白各一）、脏格子被跳过、清空真的收掉。
## 这一段要实例化 Board3D（`_ready` 会建棋盘 / 相机），headless 下也能跑；不影响上面的纯逻辑断言。
func _test_board_markers() -> void:
	var b = _board.new()
	root.add_child(b)
	b.set_estimate([[0, 0, "black"], [1, 1, "white"], [-1, 0, "black"], "oops"])
	var root_node = b._estimate_root
	_check("set_estimate 建出标记层，且只收合法格子（黑 / 白各一层网格）",
			root_node != null and root_node.get_child_count() == 2)
	b.clear_estimate()
	_check("clear_estimate 真的收掉标记层", b._estimate_root == null)
	b.set_estimate([])
	_check("空数组＝不画（不建空层）", b._estimate_root == null)
	b.set_estimate("oops")
	_check("非数组也不抛、不画", b._estimate_root == null)
	b.queue_free()

## 棋盘格子校验：合法坐标 / 颜色才画；脏值一律跳过（点目是锦上添花，绝不能把棋盘搞崩）
func _test_cell_ok() -> void:
	_check("合法格子：整数 / 浮点 / 字符串颜色", _board.estimate_cell_ok([0, 0, "black"])
			and _board.estimate_cell_ok([18, 18, "white"])
			and _board.estimate_cell_ok([3.0, 9.0, "white"]))
	_check("越界一律丢（-1 / 19）", not _board.estimate_cell_ok([-1, 0, "black"])
			and not _board.estimate_cell_ok([19, 0, "black"])
			and not _board.estimate_cell_ok([0, 19, "white"]))
	_check("错颜色 / 非字符串颜色一律丢",
			not _board.estimate_cell_ok([0, 0, "red"])
			and not _board.estimate_cell_ok([0, 0, 1]))
	_check("形状不对 / 非数组 / 脏坐标一律丢，不抛",
			not _board.estimate_cell_ok("oops")
			and not _board.estimate_cell_ok([0, 0])
			and not _board.estimate_cell_ok(["x", 0, "black"])
			and not _board.estimate_cell_ok([null, 0, "black"]))
