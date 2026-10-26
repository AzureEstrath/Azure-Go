extends SceneTree
## 思考短句解析工具自检——纯逻辑：JSON 解析 / 20 句 / 脏值过滤 / 轮换纯函数 / 心情分组。
## 运行：godot --headless --path game --script res://tests/test_thinking_lines.gd

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
	print("[think] 思考短句解析工具自检（JSON 预存 20 句 / 轮换纯函数 / 固定占位）")
	var main_script := preload("res://scripts/ui/Main.gd")

	# —— ① 出货的那份 JSON：恰好 20 句、无空句、无重复、长度可控 ——
	var f := FileAccess.open("res://data/thinking_lines.json", FileAccess.READ)
	_check("① 数据文件可读", f != null)
	var text := ""
	if f != null:
		text = f.get_as_text()
		f.close()
	var lines: PackedStringArray = main_script.parse_thinking_lines(text)
	_check("① 预存 20 句", lines.size() == 20)
	var no_blank := true
	var no_dup := true
	var short_enough := true
	var seen := {}
	for s in lines:
		var v := str(s).strip_edges()
		if v == "":
			no_blank = false
		if seen.has(v):
			no_dup = false
		seen[v] = true
		if v.length() > 30:
			short_enough = false
	_check("① 没有空句", lines.size() > 0 and no_blank)
	_check("① 没有重复句（重复轮换看起来像卡住）", no_dup)
	_check("① 每句不超过 30 字（气泡里放得下）", short_enough)

	# —— ② 脏值：坏 JSON / 缺键 / 非字符串 / 重复句 ——
	_check("② 坏 JSON → 空表（调用方据此回退旧动效）", main_script.parse_thinking_lines("not json").is_empty())
	_check("② 空串 → 空表", main_script.parse_thinking_lines("").is_empty())
	_check("② 缺 lines 键 → 空表", main_script.parse_thinking_lines("{\"other\": 1}").is_empty())
	_check("② lines 不是数组 → 空表", main_script.parse_thinking_lines("{\"lines\": \"一句\"}").is_empty())
	var dirty: PackedStringArray = main_script.parse_thinking_lines(
		"{\"lines\": [1, null, \"  \", \"好\", \"好\", \" 再看 \"]}")
	_check("② 非字符串 / 空串 / 重复句被丢掉、其余去空白",
		dirty.size() == 2 and str(dirty[0]) == "好" and str(dirty[1]) == "再看")
	var bare: PackedStringArray = main_script.parse_thinking_lines("[\"甲\", \"乙\"]")
	_check("② 裸数组也接受", bare.size() == 2)

	# —— ③ 轮换：每秒一句、随机不连复 ——
	_check("③ 没有句子时返回 -1（调用方走回退）", main_script.next_thinking_index(0, 0, 0.5) == -1)
	_check("③ 只有一句时恒为 0", main_script.next_thinking_index(0, 1, 0.999) == 0)
	var prev := -1
	var all_in_range := true
	var never_same := true
	for i in 200:
		var nxt: int = main_script.next_thinking_index(prev, 20, fmod(float(i) * 0.137, 1.0))
		if nxt < 0 or nxt >= 20:
			all_in_range = false
		if prev >= 0 and nxt == prev:
			never_same = false
		prev = nxt
	_check("③ 200 次轮换都落在 0..19", all_in_range)
	_check("③ 200 次轮换没有一次与上一句相同", never_same)
	_check("③ 首句下标可由 rng 钉死（0.999 → 最后一句）",
		main_script.next_thinking_index(-1, 20, 0.999) == 19)
	_check("③ 首句下标钉在 0.0 → 第一句", main_script.next_thinking_index(-1, 20, 0.0) == 0)
	_check("③ 脏 prev（越界）按「没有上一句」处理",
		main_script.next_thinking_index(99, 20, 0.0) == 0)
	_check("③ 固定占位文案是「Azure 思考中」", str(main_script.THINKING_FALLBACK) == "Azure 思考中")

	# —— ④ 心情/局面分组：解析 / 覆盖 / 抽句不连复 ——
	var moods: Dictionary = main_script.parse_thinking_moods(text, lines.size())
	_check("④ 解析出四组", moods.size() >= 4)
	var covered := {}
	for key in moods.keys():
		for i in moods[key]:
			covered[int(i)] = true
	_check("④ 分组覆盖全部 20 句", covered.size() == lines.size())
	_check("④ 坏 JSON → 空字典", main_script.parse_thinking_moods("not json", 20).is_empty())
	var dirty_moods: Dictionary = main_script.parse_thinking_moods(
		"{\"moods\": {\"negative\": [1, 1, 99, -1, \"x\"]}}", 20)
	_check("④ 越界 / 重复 / 非数字下标被丢掉",
		dirty_moods.has("negative") and dirty_moods["negative"].size() == 1
		and int(dirty_moods["negative"][0]) == 1)
	_check("④ 情绪映射：难过→negative",
		main_script.thinking_group_for_mood("难过") == "negative")
	_check("④ 情绪映射：开心→confident",
		main_script.thinking_group_for_mood("开心") == "confident")
	_check("④ 情绪映射：好奇→alert",
		main_script.thinking_group_for_mood("好奇") == "alert")
	_check("④ 平静 / 未知情绪交给局面兜底",
		main_script.thinking_group_for_mood("平静") == ""
		and main_script.thinking_group_for_mood("??") == "")
	var pool := PackedInt32Array([1, 3, 13, 18])
	var picked: int = main_script.pick_thinking_index(pool, 1, 0.0)
	_check("④ 分组抽句绕开上一句", picked != 1 and pool.has(picked))
	_check("④ 空池 → -1", main_script.pick_thinking_index(PackedInt32Array(), -1, 0.5) == -1)
	_check("④ 单句池只能复用", main_script.pick_thinking_index(PackedInt32Array([5]), 5, 0.5) == 5)

	if _fail == 0:
		print("[think] PASS：", _total, " 项断言全部通过")
	else:
		print("[think] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
