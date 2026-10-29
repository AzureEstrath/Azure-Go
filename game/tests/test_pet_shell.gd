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
	var chat_rect := Rect2(v._chat.position, v._chat.size)
	_check("窄窗口聊天面板完整在视口内",
		chat_rect.end.x <= v.size.x + 0.5 and chat_rect.end.y <= v.size.y + 0.5)
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
	v.append_chat("assistant", "[[tone:proud]] {轻轻点头} 记着呢")
	var last_row = log.get_child(log.get_child_count() - 1)
	var last_text: Label = last_row.get_child(0).get_child(0)
	_check("聊天气泡不显示语气 / 动作标记",
		last_text.text.contains("记着呢") and not last_text.text.contains("tone")
		and not last_text.text.contains("轻轻点头"))

	v.open_files()
	await process_frame
	_check("资料面板能打开", v.is_files_open())
	_check("资料面板存在", v.get_node_or_null("FilesPanel") != null)
	_check("小窗口面板占满时宠物让位（不挡文字）", not v._pet_anchor.visible)
	var panel_rect := Rect2(v._files.position, v._files.size)
	_check("窄窗口资料面板完整在视口内",
		panel_rect.end.x <= v.size.x + 0.5 and panel_rect.end.y <= v.size.y + 0.5)
	_check("窄窗口关闭按钮可达", v._files_close != null
		and v._files_close.get_global_rect().end.x <= v.get_global_rect().end.x + 0.5)
	_check("窄窗口操作按钮折成两列",
		v._files_actions.columns == 2 and v._files_bottom.columns == 2)
	var buttons_inside := true
	for b in v._files_actions.get_children():
		var br: Rect2 = b.get_global_rect()
		if br.end.x > v.get_global_rect().end.x + 0.5 \
				or br.end.y > v.get_global_rect().end.y + 0.5:
			buttons_inside = false
	_check("窄窗口操作按钮都在视口内", buttons_inside)
	_check("资料窗口不再撑到 700 宽", v._files_size().x <= 560)
	v.set_files_info("C:/inbox", [{"name": "a.png"}, {"name": "b.pdf"}])
	_check("资料文件列表显示", v._files_rows.get_child_count() == 2)
	v._calendar_month = "2026-10"
	v.set_schedule_items([{"when_text": "2026-10-28 15:30", "title": "评审", "notes": ""}])
	_check("月历默认显示 42 个日格", v._calendar_grid != null
		and v._calendar_grid.get_child_count() == 42)
	_check("月历标题显示年月", v._calendar_title != null
		and v._calendar_title.text.contains("2026") and v._calendar_title.text.contains("10"))
	_check("没有课程时默认不画周表", v._schedule_table == null)
	_check("本月日程列表显示", v._schedule_agenda != null
		and v._schedule_agenda.get_child_count() >= 2)
	_check("复盘按钮存在", v._files_review != null)
	_check("执行反馈 / 复盘信号已定义",
		v.has_signal("schedule_feedback_requested") and v.has_signal("files_review_requested"))
	var agenda_wrap = v._schedule_agenda
	var task_row = agenda_wrap.get_child(1) if agenda_wrap.get_child_count() > 1 else null
	_check("普通日程行带完成 / 推迟按钮",
		task_row != null and task_row.get_child_count() == 3)
	_check("普通日程行显示自动分级",
		task_row != null and task_row.get_child(0).text.contains("[待排]"))
	v._shift_month(1)
	_check("月历能翻到下个月", v._calendar_title != null
		and v._calendar_title.text.contains("11"))
	v._shift_month(-1)
	_check("月历能翻回本月", v._calendar_title != null
		and v._calendar_title.text.contains("10"))
	var click_ev := InputEventMouseButton.new()
	click_ev.button_index = MOUSE_BUTTON_LEFT
	click_ev.pressed = true
	v._on_calendar_cell_input(click_ev, "2026-10-29")
	_check("点月历日期进入那一周", v._schedule_view == "week"
		and v._week_start == "2026-10-26")
	v._open_current_week()
	_check("本周按钮回到当前周", v._week_start == "" and v._schedule_view == "week")
	v.show_month()
	_check("月历按钮能回到月历", v._calendar_grid != null)
	await process_frame
	await process_frame
	_check("窄窗口月历网格不超出面板", v._calendar_grid != null
		and v._calendar_grid.size.x <= v._files.size.x + 0.5)
	v.set_schedule_items([
		{"when_text": "2026-10-28 周三 第1-2节 08:00-09:35",
		 "title": "高等数学", "teacher": "张老师", "location": "综合楼101",
		 "date": "2026-10-28", "weekday": 2, "period": "1-2",
		 "period_start": 1, "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
		{"when_text": "2026-10-27 周二 第6-7节 13:30-15:05",
		 "title": "学术英语", "teacher": "李老师", "location": "综合楼203",
		 "date": "2026-10-27", "weekday": 1, "period": "6-7",
		 "period_start": 6, "period_end": 7, "start_time": "13:30", "end_time": "15:05"},
	])
	v.show_week_for_date("2026-10-28")
	_check("点日期能打开对应周", v._week_start == "2026-10-26"
		and v._week_title != null and v._week_title.text.contains("10月"))
	_check("周课表网格生成", v._schedule_table != null)
	_check("课程块跨节落在网格里", v._schedule_table.get_child_count() > 7 + 12)
	_check("表格宽度按面板铺开（窄窗口也保留 7 天）", v._schedule_table.custom_minimum_size.x >= 280.0
		and v._schedule_table.custom_minimum_size.x <= 480.0)
	v.set_schedule_items([
		{"title": "高等数学", "teacher": "张老师", "location": "综合楼101",
		 "date": "2026-10-28", "weekday": 2, "period": "1-2", "period_start": 1,
		 "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
		{"title": "高等数学", "teacher": "张老师", "location": "综合楼101",
		 "date": "2026-11-04", "weekday": 2, "period": "1-2", "period_start": 1,
		 "period_end": 2, "start_time": "08:00", "end_time": "09:35"},
	])
	_check("同一门课跨周扫描只留一格", v._schedule_table.get_child_count() == 7 + 12 + 1)
	v.show_week_for_date("2026-11-04")
	_check("下一周只显示它自己的课表", v._schedule_table != null
		and v._schedule_table.get_child_count() == 7 + 12 + 1)
	v._calendar_month = "2026-11"
	v.show_month()
	await process_frame
	await process_frame
	var cell_1104 := false
	var cell_1111 := false
	for cell in v._calendar_grid.get_children():
		if not (cell is PanelContainer):
			continue
		var cdate := str(cell.get_meta("calendar_date", ""))
		var has_math := _calendar_cell_has_text(cell, "高等数学")
		if cdate == "2026-11-04":
			cell_1104 = has_math
		elif cdate == "2026-11-11":
			cell_1111 = has_math
	_check("月历只在课程真实日期显示", cell_1104 and not cell_1111)
	v.show_week_for_date("2026-10-26")
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
		_check("课程块不再使用原生黑底 tooltip", block.tooltip_text == "")
		v._show_hover_card(block, {"title": "高等数学", "when_text": "2026-10-28 周三 第1-2节",
			"teacher": "张老师", "location": "综合楼101",
			"grade": "fixed", "grade_label": "固定课程"})
		_check("悬停展开卡显示完整课程信息",
			v._hover_card.visible and v._hover_label.text.contains("综合楼101"))
		_check("悬停展开卡显示课程分级",
			v._hover_label.text.contains("分级：固定课程"))
		v._hide_hover_card()
	else:
		_check("悬停展开卡显示完整课程信息", false)
	host.size = Vector2(360, 540)
	await process_frame
	await process_frame
	v.set_schedule_items([
		{"id": 1, "title": "高等数学", "teacher": "张老师", "location": "综合楼101",
		 "date": "2026-10-28", "weekday": 2, "period": "1-2",
		 "period_start": 1, "period_end": 2, "start_time": "08:00",
		 "end_time": "09:35", "kind": "course", "status": "planned"},
		{"id": 4, "title": "班级篮球赛（A 队 VS B 队）",
		 "when_text": "2026-10-29 周四 12:00-13:00", "weekday": 3, "period": "",
		 "period_start": 0, "start_ts": 1793246400, "start_time": "12:00",
		 "end_time": "13:00", "kind": "task", "status": "planned",
		 "grade": "committed", "grade_label": "已约定", "location": "体育馆",
		 "priority": 50, "duration_min": 60},
		{"id": 5, "title": "班级足球赛（A 队 VS C 队）",
		 "when_text": "2026-10-29 周四 12:20-13:20", "weekday": 3, "period": "",
		 "period_start": 0, "start_ts": 1793247600, "start_time": "12:20",
		 "end_time": "13:20", "kind": "task", "status": "planned",
		 "grade": "committed", "grade_label": "已约定", "location": "体育馆",
		 "priority": 50, "duration_min": 60},
	])
	await process_frame
	var event_wrap = v._schedule_agenda
	var event_row = event_wrap.get_child(1) if event_wrap != null and event_wrap.get_child_count() > 1 else null
	_check("聊天公告提取的活动按普通日程显示并标为约定",
		event_row != null and event_row.get_child(0).text.contains("篮球赛")
		and event_row.get_child(0).text.contains("[约定]"))
	_check("360 宽也能完整显示 7 天", v._schedule_table != null
		and v._timetable.has("col_w")
		and float(v._timetable.get("col_w", 0.0)) * 7.0 + 44.0
			<= float(v._timetable.get("width", 0.0)) + 0.5)
	var event_block = null
	if v._schedule_table != null:
		for c in v._schedule_table.get_children():
			if c is Panel and c.has_meta("sched_item"):
				var meta: Dictionary = c.get_meta("sched_item")
				if str(meta.get("title", "")).contains("篮球赛") \
						or str(meta.get("notes", "")).contains("篮球赛"):
					event_block = c
					break
	_check("同一格多个约定合并成一个块", event_block != null
		and str(event_block.get_meta("sched_item").get("title", "")) == "2 项约定")
	_check("合并块在悬停卡里列出两场比赛", event_block != null
		and str(event_block.get_meta("sched_item").get("notes", "")).contains("足球赛"))
	_check("定时约定块也不使用原生黑底 tooltip", event_block != null
		and event_block.tooltip_text == "")
	var event_lbl = event_row.get_child(0) if event_row != null else null
	_check("日程行不再使用原生黑底 tooltip", event_lbl != null
		and event_lbl.tooltip_text == "")
	if event_lbl != null:
		v._show_hover_card(event_lbl, event_lbl.get_meta("sched_item"))
		_check("日程行悬停只显示一张自绘卡",
			v._hover_card.visible and v._hover_label.text.contains("篮球赛"))
		_check("悬停卡显示排班理由",
			v._hover_label.text.contains("排班理由："))
		v._hide_hover_card()
	# 已经结束的星期型事件：不再自动重排，但仍应留在它所属那一周的周表里。
	v.set_schedule_items([
		{"id": 4, "title": "班级篮球赛",
		 "when_text": "2026-10-29 周四 12:00-13:00", "weekday": 3, "period": "",
		 "period_start": 0, "start_ts": 1793246400, "start_time": "12:00",
		 "end_time": "13:00", "kind": "task", "status": "planned",
		 "grade": "closed", "grade_label": "已结束", "location": "体育馆",
		 "priority": 50, "duration_min": 60},
		{"id": 5, "title": "班级足球赛",
		 "when_text": "2026-10-29 周四 12:20-13:20", "weekday": 3, "period": "",
		 "period_start": 0, "start_ts": 1793247600, "start_time": "12:20",
		 "end_time": "13:20", "kind": "task", "status": "planned",
		 "grade": "closed", "grade_label": "已结束", "location": "体育馆",
		 "priority": 50, "duration_min": 60},
	])
	v.show_week_for_date("2026-10-29")
	var closed_block = null
	if v._schedule_table != null:
		for c in v._schedule_table.get_children():
			if c is Panel and c.has_meta("sched_item") \
					and str(c.get_meta("sched_item").get("title", "")) == "2 项约定":
				closed_block = c
				break
	_check("已结束的两条约定仍留在本周周表", closed_block != null)
	_check("已结束的合并块标为已结束", closed_block != null
		and str(closed_block.get_meta("sched_item").get("grade", "")) == "closed")
	# 完成的任务也不能从周表消失（后端 include_done + 前端允许 done）。
	v.set_schedule_items([{
		"id": 6, "title": "交实验报告",
		"when_text": "2026-10-29 周四 19:00-20:00", "date": "2026-10-29",
		"weekday": 3, "period": "", "period_start": 0, "start_ts": 1793271600,
		"start_time": "19:00", "end_time": "20:00", "kind": "task",
		"status": "done", "grade": "closed", "grade_label": "已结束",
		"duration_min": 60,
	}])
	v.show_week_for_date("2026-10-29")
	var done_block = null
	if v._schedule_table != null:
		for c in v._schedule_table.get_children():
			if c is Panel and c.has_meta("sched_item") \
					and str(c.get_meta("sched_item").get("title", "")) == "交实验报告":
				done_block = c
				break
	_check("完成的任务仍留在周表", done_block != null
		and str(done_block.get_meta("sched_item").get("status", "")) == "done")
	_check("完成的任务悬停显示已完成", done_block != null
		and v._grade_text(done_block.get_meta("sched_item")).contains("已完成"))
	host.size = Vector2(544, 520)
	await process_frame
	await process_frame
	var wide_rect := Rect2(v._files.position, v._files.size)
	_check("宽窗口操作按钮恢复一行",
		v._files_actions.columns == 4 and v._files_bottom.columns == 3)
	_check("宽窗口资料面板完整在视口内",
		wide_rect.end.x <= v.size.x + 0.5 and wide_rect.end.y <= v.size.y + 0.5)
	_check("宽窗口重新显示操作提示", v._files_help.visible)
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
		'[{"date":"2026-10-28","time":"08:00","title":"无机化学实验","notes":"李老师 @综合楼103"}]')
	_check("日程 JSON 只转一句自然确认", schedule_text == "嗯，记下了。")
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
	_test_chat_schedule_note(shell)
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

## 聊天排程补充：不显示额外播报，只当数据刷新信号；也不抢 `_chat_busy`。
func _test_chat_schedule_note(shell) -> void:
	shell.view.open_chat()
	var before: int = shell.view._chat_log.get_child_count()
	shell._chat_busy = true
	shell._on_message("ai_schedule", "好，日程记好了：「组会」（10月30日 周五 15:00）。")
	_check("聊天排程不再插入生硬播报", shell.view._chat_log.get_child_count() == before)
	_check("聊天排程补充不抢聊天 busy", shell._chat_busy)
	shell._chat_busy = false
	shell.view.close_chat()

## 月历日格里有没有某个文字（日格 = PanelContainer → VBox → Labels）。
func _calendar_cell_has_text(cell, needle: String) -> bool:
	if cell == null or cell.get_child_count() == 0:
		return false
	var vb = cell.get_child(0)
	if vb == null:
		return false
	for lbl in vb.get_children():
		if lbl is Label and str(lbl.text).contains(needle):
			return true
	return false

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
