extends SceneTree
## 桌宠外壳（PetView / Launcher）纯函数与布局自检。
## 运行：godot --headless --path game --script res://tests/test_pet_shell.gd

var _total := 0
var _fail := 0

var _view_script = null
var _shell_script = null
var _launcher_script = null

## 只记录「有没有真的出声」，避免 headless 测试拉起系统语音进程。
class FakeTTS extends Node:
	var enabled := true
	var system_lines: Array[String] = []

	func set_suppress_system(_on: bool) -> void:
		pass

	func speak_system(text: String, _seq: int, _kind: String) -> void:
		system_lines.append(text)

	func play_audio(_body: PackedByteArray, _seq: int, _kind: String) -> void:
		pass

	func play_filler(_kind: String, _force: bool) -> void:
		pass

	func set_mood_pitch(_pitch: float) -> void:
		pass

class FakeAgent extends Node:
	var fetched: Array[String] = []

	func fetch_bytes(path: String, _method: int, _headers: Dictionary,
			_callback: Callable, _timeout: float) -> void:
		fetched.append(path)

func _initialize() -> void:
	_view_script = preload("res://scripts/app/PetView.gd")
	_shell_script = preload("res://scripts/app/PetShell.gd")
	_launcher_script = preload("res://scripts/app/Launcher.gd")
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[petshell] 桌宠外壳自检（尺寸 / 菜单 / 聊天 / 拖动 / 启动判断）")
	await _test_view()
	_test_shell_static()
	_test_launcher()
	await _test_shell_instance()
	if _fail == 0:
		print("[petshell] PASS：", _total, " 项断言全部通过")
	else:
		print("[petshell] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

func _test_view() -> void:
	var host := Control.new()
	host.size = Vector2(240, 280)
	root.add_child(host)
	var v = _view_script.new()
	v.pet_height = 160
	host.add_child(v)
	await process_frame
	await process_frame
	_check("PetView 能装入场景树", is_instance_valid(v))
	_check("默认高度 160", v.pet_height == 160)
	var tex: TextureRect = v.get_node_or_null("PetBody/PetTexture")
	_check("完整贴图已加载", tex != null and tex.texture != null)
	if tex != null:
		_check("贴图保持原始比例、不裁切", tex.stretch_mode == TextureRect.STRETCH_KEEP_ASPECT_CENTERED)
		_check("贴图随容器铺开（不是固定小图）", tex.expand_mode == TextureRect.EXPAND_IGNORE_SIZE)

	var menu = v.get_node_or_null("PetMenu")
	_check("右键菜单存在", menu != null)
	_check("菜单默认收起", not v.is_menu_open())
	v.open_menu(Vector2(120, 190))
	await process_frame
	_check("菜单能打开", v.is_menu_open())
	_check("菜单完整落在窗口内", menu != null
		and menu.position.x + menu.size.x <= v.size.x + 0.5
		and menu.position.y + menu.size.y <= v.size.y + 0.5)
	var veil = v.get_node_or_null("PetMenuVeil")
	_check("菜单有可点击的薄纱背景", veil != null and veil.visible)
	v.close_menu()
	_check("菜单能收起", not v.is_menu_open())

	v.append_chat("user", "你好")
	v.append_chat("assistant", "在呢。")
	v.open_chat()
	await process_frame
	await process_frame
	_check("聊天面板能打开", v.is_chat_open())
	var scroll = v.get_node_or_null("ChatPanel").get_child(0).get_child(2)
	var log = scroll.get_child(0)
	var rows = log.get_children()
	_check("聊天有两行", rows.size() == 2)
	if rows.size() == 2:
		var user_bubble = rows[0].get_child(rows[0].get_child_count() - 1)
		var ai_bubble = rows[1].get_child(0)
		_check("玩家消息在右、Azure 在左", user_bubble.position.x > ai_bubble.position.x)
	v.close_chat()
	await process_frame
	_check("聊天面板能收起", not v.is_chat_open())
	_check("聊天记录保留在面板里", log.get_child_count() == 2)

	v.open_files()
	await process_frame
	_check("资料面板能打开", v.is_files_open())
	_check("资料面板存在", v.get_node_or_null("FilesPanel") != null)
	_check("小窗口面板占满时宠物让位（不挡文字）", not v._pet_anchor.visible)
	v.set_files_info("C:/inbox", [{"name": "a.png"}, {"name": "b.pdf"}])
	_check("资料文件列表显示", v._files_rows.get_child_count() == 2)
	v.set_schedule_items([{"when_text": "2026-10-28 15:30", "title": "评审", "notes": ""}])
	_check("普通日程列表显示", v._schedule_rows.get_child_count() == 1)
	_check("没有课程时不画空表格", v._schedule_table == null)
	_check("复盘按钮存在", v._files_review != null)
	_check("执行反馈 / 复盘信号已定义",
		v.has_signal("schedule_feedback_requested") and v.has_signal("files_review_requested"))
	var agenda_wrap = v._schedule_rows.get_child(0)
	var task_row = agenda_wrap.get_child(1) if agenda_wrap.get_child_count() > 1 else null
	_check("普通日程行带完成 / 推迟按钮",
		task_row != null and task_row.get_child_count() == 3)
	v.set_schedule_items([
		{"when_text": "2026-10-28 周三 第1-2节 08:00-09:35",
		 "title": "高等数学", "teacher": "王三强", "location": "二教20",
		 "date": "2026-10-28", "weekday": 2, "period": "1-2",
		 "period_start": 1, "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
		{"when_text": "2026-10-27 周二 第6-7节 13:30-15:05",
		 "title": "学术英语", "teacher": "靳丽莹", "location": "一教30",
		 "date": "2026-10-27", "weekday": 1, "period": "6-7",
		 "period_start": 6, "period_end": 7, "start_time": "13:30", "end_time": "15:05"},
	])
	_check("周课表网格生成", v._schedule_table != null)
	_check("课程块跨节落在网格里", v._schedule_table.get_child_count() > 7 + 12)
	_check("表格宽度按面板铺开", v._schedule_table.custom_minimum_size.x >= 400.0)
	v.set_schedule_items([
		{"title": "高等数学", "teacher": "王三强", "location": "二教20",
		 "date": "2026-10-28", "weekday": 2, "period": "1-2", "period_start": 1,
		 "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
		{"title": "高等数学", "teacher": "王三强", "location": "二教20",
		 "date": "2026-11-04", "weekday": 2, "period": "1-2", "period_start": 1,
		 "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
	])
	_check("同一门课跨周扫描只留一格", v._schedule_table.get_child_count() == 7 + 12 + 1)
	_check("自动排班 / 归档 / 清理按钮存在",
		v._files_auto_plan != null and v._files_archive != null and v._files_cleanup != null)
	v.show_reminder("到点了：「高等数学」，别忘了。")
	_check("事务提醒卡能弹出", v._reminder != null and v._reminder.visible
		and v._reminder_label.text.contains("高等数学"))
	v._reminder.visible = false
	var block = null
	for c in v._schedule_table.get_children():
		if c is Panel:
			block = c
			break
	if block != null:
		v._show_hover_card(block, {"title": "高等数学", "when_text": "2026-10-28 周三 第1-2节",
			"teacher": "王三强", "location": "二教20"})
		_check("悬停展开卡显示完整课程信息",
			v._hover_card.visible and v._hover_label.text.contains("二教20"))
		v._hide_hover_card()
	else:
		_check("悬停展开卡显示完整课程信息", false)
	v.close_files()
	await process_frame
	_check("资料面板能收起", not v.is_files_open())
	_check("收起后面板让出的宠物回来", v._pet_anchor.visible)
	v.queue_free()
	host.queue_free()

func _test_shell_static() -> void:
	_check("localhost 是本地后端", _shell_script._is_local_url("http://127.0.0.1:8000"))
	_check("LAN host 不是本地后端", not _shell_script._is_local_url("http://192.168.1.8:8000"))
	_check("https 域名不是本地后端", not _shell_script._is_local_url("https://api.example.com/v1"))
	_check("离开不到 15 分钟不主动寒暄", _shell_script.away_notice(300.0) == "")
	_check("离开 20 分钟会说回来啦", _shell_script.away_notice(1200.0) != "")
	_check("离开一天以上会提上次聊到哪", _shell_script.away_notice(90000.0).contains("上次"))
	_check("离开时长的脏值不冒泡", _shell_script.away_notice(NAN) == "")
	_check("普通台词原样显示", _shell_script.clean_display_text("嗯，我在。") == "嗯，我在。")
	var schedule_text: String = _shell_script.clean_display_text(
		'[{"date":"2026-10-28","time":"08:00","title":"无机化学实验","notes":"杨悠笛 @二教10"}]')
	_check("日程 JSON 转成人话", schedule_text.contains("无机化学实验") and not schedule_text.contains("\"date\""))
	_check("半截 JSON 不显示成 [", _shell_script.clean_display_text("[{") == "")
	_check("认不出的 JSON 不显示", _shell_script.clean_display_text('{"foo":1}') == "")
	_check("句首 [笑] 不会被当成 JSON", _shell_script.clean_display_text("[笑] 好呀") == "[笑] 好呀")
	_check("句首长动作标签原样保留", _shell_script.clean_display_text("{轻轻叹了口气}") == "{轻轻叹了口气}")
	_check("精力低时主动搭话间隔变长", _shell_script.idle_mood_scale(0.2, 0.2) > 1.1)
	_check("无聊高时主动搭话更频繁", _shell_script.idle_mood_scale(0.7, 0.9) < 1.0)
	_check("助手开口基线不超过 20 秒", _shell_script.IDLE_FIRST_SEC <= 20.0)
	_check("低精力也不会把开口间隔放到沉默", _shell_script.idle_mood_scale(0.0, 0.0) <= 1.5)
	_check("精力低时窥屏更少", _shell_script.peek_mood_scale(0.2, 0.2) > 1.1)
	_check("无聊高时窥屏更频繁", _shell_script.peek_mood_scale(0.7, 0.9) < 1.0)
	var h_noise0: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "下午", 0, 0.0, 0.5)
	var h_noise1: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "下午", 0, 1.0, 0.5)
	_check("开口节奏不是固定间隔", not is_equal_approx(h_noise0, h_noise1))
	var h_unanswered: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "下午", 3, 0.0, 0.5)
	_check("连续没人接话会读空气拉长", h_unanswered > h_noise0)
	var h_night: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "深夜", 0, 0.0, 0.5)
	_check("深夜比下午更克制", h_night > h_noise0)
	var h_burst: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "下午", 0, 0.0, 0.10)
	_check("偶尔会有短追问", h_burst < h_noise0)
	var h_long: float = _shell_script.humanized_idle_delay_sec(
		0.72, 0.16, 0.35, 0.5, 0.5, 0.58, 0.30, "下午", 0, 0.0, 0.99)
	_check("偶尔会有长停顿", h_long > h_noise0)
	_check("人格化节奏有上限", h_long <= 150.0)

## 真正装配一次 PetShell（不拉服务、不连后端）：抓「界面没建出来」这类运行时错误。
func _test_shell_instance() -> void:
	var shell = _shell_script.new()
	root.add_child(shell)
	await process_frame
	await process_frame
	_check("PetShell 能装配出桌宠界面", shell.view != null)
	_check("PetShell 发出启动问候", shell.view != null and shell.view._bubble.visible)
	_check("PetShell 创建了后端客户端", shell.agent != null)
	_check("PetShell 创建了语音客户端", shell.tts != null)
	shell._on_mood({"energy": 0.21, "boredom": 0.81, "affinity": 0.66, "trust": 0.44, "dominance": 0.55})
	_check("桌宠接收精力 / 无聊 / 亲密 / 信任 / 掌控感",
		is_equal_approx(shell._energy, 0.21) and is_equal_approx(shell._boredom, 0.81)
		and is_equal_approx(shell._affinity, 0.66) and is_equal_approx(shell._trust, 0.44)
		and is_equal_approx(shell._dominance, 0.55))
	shell._energy = 0.10
	shell._boredom = 0.10
	var tired_line: String = shell._reaction_line()
	_check("精力低时台词变疲惫",
		tired_line in ["唔…有点没力气。", "让我靠一会儿。", "今天慢一点也没关系吧。"])
	shell._energy = 0.70
	shell._boredom = 0.90
	var bored_line: String = shell._reaction_line()
	_check("无聊高时台词想找人说话",
		bored_line in ["桌面好安静。", "你是不是很久没理我了。", "我在等你说句话。"])
	shell._life_greeted = true
	shell._on_life(true, 0, {"state": {"energy": 0.33, "boredom": 0.44}, "temporal": {}})
	_check("生命时间线刷新慢变量",
		is_equal_approx(shell._energy, 0.33) and is_equal_approx(shell._boredom, 0.44))
	_test_speech_policy(shell)
	shell.queue_free()
	await process_frame

## 待机 / 窥屏只允许气泡 + 预制语气；只有聊天面板这一条可以走完整语音。
func _test_speech_policy(shell) -> void:
	var fake_tts := FakeTTS.new()
	var fake_agent := FakeAgent.new()
	shell.tts = fake_tts
	shell.agent = fake_agent
	shell.cfg.tts_enabled = true

	shell._chat_busy = false
	shell._speech_kinds.clear()
	shell._on_speech({"mode": "system", "text": "待机不该念出来", "seq": 1})
	_check("待机 speech 不走系统语音", fake_tts.system_lines.is_empty())

	shell._speech_kinds.append({"kind": "idle", "ts": Time.get_ticks_msec()})
	shell._on_speech({"mode": "system", "text": "走神也不念", "seq": 2})
	_check("走神 speech 不走系统语音", fake_tts.system_lines.is_empty())

	shell.cfg.tts_mode = "system"
	shell._chat_busy = true
	shell._on_speech({"mode": "system", "text": "聊天这句可以念", "seq": 3})
	_check("聊天 speech 允许系统语音",
		fake_tts.system_lines.size() == 1 and fake_tts.system_lines[0] == "聊天这句可以念")

	shell.cfg.tts_mode = "local"
	shell._on_speech({"mode": "system", "text": "本地模式不退化系统", "seq": 4})
	_check("本地模式不退回系统语音", fake_tts.system_lines.size() == 1)

	shell._chat_busy = false
	shell._speech_kinds.clear()
	shell._audio_queue.clear()
	shell._audio_busy = false
	shell._on_speech({"mode": "openai", "id": "idle1", "seq": 5})
	_check("待机合成语音也不会取回", fake_agent.fetched.is_empty())

	shell._chat_busy = true
	shell._on_speech({"mode": "openai", "id": "chat1", "seq": 6})
	_check("聊天合成语音会取回",
		fake_agent.fetched.size() == 1 and fake_agent.fetched[0] == "/api/tts/audio/chat1")

	shell._chat_busy = false
	shell._audio_busy = false
	shell._audio_queue.clear()
	shell.agent = null
	shell.tts = null
	fake_tts.free()
	fake_agent.free()

func _test_launcher() -> void:
	_check("默认配置开桌宠 ⇒ 桌宠外壳",
		not _launcher_script.pick_mode(PackedStringArray([]), false, true))
	_check("配置关桌宠 ⇒ 围棋窗口",
		_launcher_script.pick_mode(PackedStringArray([]), false, false))
	_check("--pet 强制桌宠",
		not _launcher_script.pick_mode(PackedStringArray(["--pet"]), false, false))
	_check("--game 强制围棋",
		_launcher_script.pick_mode(PackedStringArray(["--game"]), false, true))
	_check("--no-pet 强制围棋",
		_launcher_script.pick_mode(PackedStringArray(["--no-pet"]), false, true))
	_check("--shot 走围棋（截图调试）",
		_launcher_script.pick_mode(PackedStringArray(["--shot=x.png"]), false, true))
	_check("无头模式走围棋", _launcher_script.pick_mode(PackedStringArray([]), true, true))
