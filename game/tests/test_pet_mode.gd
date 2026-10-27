extends SceneTree
## 桌宠形态纯函数自检（不渲染、不依赖后端）：
##   ① 启动形态选择（config / --no-pet / --pet / 截图 / 无头）
##   ② 情绪带 → 点击台词 / 光环颜色 / 数值摘要
##   ③ 右键菜单位置夹取、点击与拖动阈值、气泡停留时长
##   ④ 配置默认开、保存后仍能读回
## 运行：godot --headless --path game --script res://tests/test_pet_mode.gd

var _total := 0
var _fail := 0
var _main = null
var _pet = null

func _initialize() -> void:
	_main = preload("res://scripts/ui/Main.gd")
	_pet = preload("res://scripts/ui/PetMode.gd")
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[pet] 桌宠形态自检（启动选择 / 情绪台词 / 菜单几何 / 气泡时长）")
	_test_start_mode()
	_test_mood_lines()
	_test_geometry()
	_test_config_flag()
	await _test_ui_instance()
	if _fail == 0:
		print("[pet] PASS：", _total, " 项断言全部通过")
	else:
		print("[pet] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

func _test_start_mode() -> void:
	var no_args := PackedStringArray()
	var shot := PackedStringArray(["--shot=out.png"])
	var no_pet := PackedStringArray(["--no-pet"])
	var force := PackedStringArray(["--pet"])
	_check("默认按配置以桌宠启动", _main.should_start_in_pet_mode(true, no_args, false))
	_check("配置关掉 ⇒ 旧版直接进主界面", not _main.should_start_in_pet_mode(false, no_args, false))
	_check("--no-pet 显式关闭", not _main.should_start_in_pet_mode(true, no_pet, false))
	_check("--pet 可强制打开（配置关着也开）", _main.should_start_in_pet_mode(false, force, false))
	_check("截图模式不启动桌宠（会卡在等点击）", not _main.should_start_in_pet_mode(true, shot, false))
	_check("无头模式不启动桌宠", not _main.should_start_in_pet_mode(true, no_args, true))
	_check("--title 也不启动桌宠", not _main.should_start_in_pet_mode(true, PackedStringArray(["--title"]), false))
	_check("--no-pet 优先于 --pet", not _main.should_start_in_pet_mode(true,
		PackedStringArray(["--pet", "--no-pet"]), false))

func _test_mood_lines() -> void:
	_check("开心 → high", _pet.mood_band("开心", 0.8) == "high")
	_check("难过 → low", _pet.mood_band("难过", 0.2) == "low")
	_check("好奇 → curious", _pet.mood_band("好奇", 0.5) == "curious")
	_check("平静 → calm", _pet.mood_band("平静", 0.5) == "calm")
	_check("未知名字按数值兜底（低）", _pet.mood_band("未知", 0.1) == "low")
	_check("未知名字按数值兜底（高）", _pet.mood_band("未知", 0.9) == "high")
	for band in ["high", "curious", "low", "calm"]:
		var line: String = str(_pet.pick_reaction(band, 0.0))
		_check("情绪带 %s 有本地台词" % band, line != "")
		_check("情绪带 %s 的 roll=0 稳定" % band, line == str(_pet.pick_reaction(band, 0.0)))
	var col: Color = _pet.mood_color(0.9, 0.5, 0.5)
	_check("情绪颜色是有效 Color", col.a > 0.0)
	var summary: String = _pet.mood_summary(0.7, 0.6, 0.5, "愉悦")
	_check("数值摘要带三维数字", summary.contains("0.70") and summary.contains("0.60")
		and summary.contains("0.50") and summary.contains("愉悦"))

func _test_geometry() -> void:
	var win := Vector2(360, 540)
	var menu := Vector2(200, 300)
	var p: Vector2 = _pet.clamp_menu_position(Vector2(350, 530), menu, win)
	_check("菜单位置夹在窗内 X", p.x <= win.x - menu.x - 8.0 + 0.01)
	_check("菜单位置夹在窗内 Y", p.y <= win.y - menu.y - 8.0 + 0.01)
	var p2: Vector2 = _pet.clamp_menu_position(Vector2(-20, -20), menu, win)
	_check("负数落点夹到边距", p2.x >= 8.0 and p2.y >= 8.0)
	_check("拖动 0px 算点击", _pet.is_pet_click(0.0))
	_check("拖动 2px 仍算点击", _pet.is_pet_click(2.0))
	_check("拖动 20px 不算点击", not _pet.is_pet_click(20.0))
	_check("短句气泡至少 3 秒", _pet.bubble_ttl("在呢。") >= 3.0)
	_check("长句气泡上限 9 秒", _pet.bubble_ttl("一".repeat(200)) <= 9.0)
	_check("气泡时长随字数增长", _pet.bubble_ttl("一".repeat(40)) > _pet.bubble_ttl("在呢。"))

## 真正组装一次 PetMode（贴图加载 / 气泡 / 右键菜单 / 情绪刷新），抓运行时错误。
func _test_ui_instance() -> void:
	var host := Control.new()
	host.size = Vector2(360, 540)
	root.add_child(host)
	var p = _pet.new()
	host.add_child(p)
	await process_frame
	_check("PetMode 能装入场景树", is_instance_valid(p))
	_check("桌宠根铺满 360×540", p.size.x >= 360.0 and p.size.y >= 540.0)
	var menu = p.get_node_or_null("PetMenu")
	_check("右键菜单存在", menu != null)
	_check("右键菜单高度不会超出 540 高窗口", menu != null and menu.get_combined_minimum_size().y <= 420.0)
	_check("右键菜单默认收起", not p.is_menu_open())
	p.show_bubble("在呢。", 4.0)
	p.set_mood({"v": 0.7, "c": 0.6, "a": 0.5, "name": "愉悦"})
	p.set_online(true, "测试连接")
	p.set_speaking(true)
	p.set_speaking(false)
	await process_frame
	p.open_menu(Vector2(200, 300))
	await process_frame
	_check("右键菜单能打开", p.is_menu_open())
	_check("右键菜单完整落在窗口内", menu != null
		and menu.position.x + menu.size.x <= p.size.x + 0.5
		and menu.position.y + menu.size.y <= p.size.y + 0.5)
	p.close_menu()
	_check("右键菜单能收起", not p.is_menu_open())
	p.play_pat_effect()
	p.play_actions(["blush", "laugh"])
	await process_frame
	_check("互动特效不抛异常", true)
	host.queue_free()

func _test_config_flag() -> void:
	var cfg = AzureConfig.new()
	_check("配置默认 pet_enabled=true", cfg.pet_enabled)
	var tmp := "user://test_pet_config.json"
	var f := FileAccess.open(tmp, FileAccess.WRITE)
	f.store_string(JSON.stringify({"pet_enabled": false, "future_key": "保留"}))
	f.close()
	var loaded := AzureConfig.load_config(tmp)
	_check("pet_enabled=false 能被读入", not loaded.pet_enabled)
	loaded.save()
	var raw := FileAccess.open(tmp, FileAccess.READ)
	var d = JSON.parse_string(raw.get_as_text())
	raw.close()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(tmp))
	_check("save() 保留 pet_enabled=false", bool((d as Dictionary).get("pet_enabled", true)) == false)
	_check("save() 保留未知键", str((d as Dictionary).get("future_key", "")) == "保留")
