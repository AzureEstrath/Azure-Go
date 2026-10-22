extends SceneTree
## 主界面（纸幕封面）自检（v3.11.2）——纯 UI 组装：三键信号 / 「开始 ⇄ 开始新对局」措辞 / 显隐 / 纸幕挡点击。
## 运行：godot --headless --path game --script res://tests/test_title_screen.gd

var _total := 0
var _fail := 0
var _primary_hits := 0
var _settings_hits := 0
var _quit_hits := 0

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

## 递归收集所有按钮（封面的按钮由类内部组装，门卫不做节点路径硬编码）
func _buttons(node: Node, out: Array[Button]) -> void:
	for c in node.get_children():
		if c is Button:
			out.append(c as Button)
		_buttons(c, out)

func _press(t: TitleScreen, label: String) -> bool:
	var bs: Array[Button] = []
	_buttons(t, bs)
	for b in bs:
		if b.text == label:
			b.pressed.emit()
			return true
	return false

func _run() -> void:
	print("[title] 主界面自检（纸幕 / 三键 / 开始新对局）")
	var t := TitleScreen.new()
	t.theme = GufengTheme.build_theme(GufengTheme.font())
	root.add_child(t)
	await process_frame                 # 等 `_ready` 把纸幕与按钮建好（headless 下加节点不保证同步 ready）
	t.primary_pressed.connect(func(): _primary_hits += 1)
	t.settings_pressed.connect(func(): _settings_hits += 1)
	t.quit_pressed.connect(func(): _quit_hits += 1)

	# —— ① 默认状态：隐藏、主按钮是「开始对局」——
	_check("① 默认隐藏（等 Main 打开）", not t.visible)
	_check("① 主按钮默认「开始对局」", t.primary_text() == "开始对局")
	_check("① 纸幕挡点击（整块 Control 吃掉鼠标，不落到棋盘）",
		t.mouse_filter == Control.MOUSE_FILTER_STOP)
	var veil := t.get_node_or_null("Veil")
	_check("① 宣纸遮罩存在且也挡点击",
		veil != null and (veil as Control).mouse_filter == Control.MOUSE_FILTER_STOP)
	_check("① 纸幕是半透明的（远处的棋盘要透出来）",
		veil != null and (veil as ColorRect).color.a < 0.7)

	# —— ② 打开 / 关闭 ——
	t.open()
	_check("② open() 后可见", t.visible)
	t.close()
	_check("② close() 后隐藏", not t.visible)

	# —— ③ 「开始 ⇄ 开始新对局」只换措辞，键位与信号不变 ——
	t.set_restart_mode(true)
	_check("③ 对局中回来：主按钮变「开始新对局」", t.primary_text() == "开始新对局")
	_check("③ 新对局模式照样能按主键", _press(t, "开始新对局") and _primary_hits == 1)
	t.set_restart_mode(false)
	_check("③ 首次启动：回到「开始对局」", t.primary_text() == "开始对局")
	_check("③ 开始键也能按", _press(t, "开始对局") and _primary_hits == 2)

	# —— ④ 设置 / 退出各自的信号 ——
	_check("④ 「设置」发出 settings_pressed", _press(t, "设置") and _settings_hits == 1)
	_check("④ 「退出」发出 quit_pressed", _press(t, "退出") and _quit_hits == 1)
	_check("④ 三键互不串台（主键没被多按）", _primary_hits == 2
		and _settings_hits == 1 and _quit_hits == 1)

	# —— ⑤ 封面不碰游戏状态：只发信号，自己不改任何东西（这里只钉「不抛、不崩」）——
	t.set_restart_mode(true)
	t.set_restart_mode(false)
	_check("⑤ 反复切换措辞不抛、状态稳定", t.primary_text() == "开始对局")

	# —— ⑥ 淡入淡出（运镜用）：透明度由 Main 编排，封面只负责自己那一层 ——
	t.set_alpha(0.4)
	_check("⑥ set_alpha 直接控制整块封面", is_equal_approx(t.alpha_value(), 0.4))
	t.set_alpha(1.0)
	t.open()
	await t.fade_out(0.02)
	_check("⑥ fade_out 之后完全透明（淡出后相机才开始飞）", t.alpha_value() <= 0.01)
	await t.fade_in(0.02)
	_check("⑥ fade_in 之后不透明度回到 1", t.alpha_value() >= 0.99)
	t.set_alpha(1.0)
	t.close()

	t.queue_free()
	if _fail == 0:
		print("[title] PASS：", _total, " 项断言全部通过")
	else:
		print("[title] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
