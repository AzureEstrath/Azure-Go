extends SceneTree
## 战报纯函数门卫（v3.11.5）：坐标 / 点评筛选 / 亏损排序 / 手顺 / 形势行。
## 不渲染（渲染由实机/临时探针验证），全部纯逻辑。

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
	print("[report] 棋谱战报纯函数自检")
	_check("① 左上交叉点在棋盘内", GameReport.cell_center(0, 0).is_equal_approx(
		GameReport.BOARD_RECT.position + Vector2(GameReport.CELL, GameReport.CELL) * 0.5))
	_check("① 右下交叉点在棋盘内", GameReport.BOARD_RECT.has_point(GameReport.cell_center(18, 18)))
	var notes := [
		{"move_no": 3, "kind": "review", "side": "E", "text": "这一手有点急"},
		{"move_no": 5, "kind": "review", "side": "A", "text": "我喜欢这里"},
		{"move_no": 7, "kind": "review", "side": "A", "text": "   "},
		{"move_no": 9, "kind": "aim", "side": "A", "text": "打算下这里"},
		{"move_no": 99, "kind": "review", "side": "E", "text": "未来的一手"},
	]
	var picked := GameReport.pick_notes(notes, 10)
	_check("② 只取 review、非空、手数截止", picked.size() == 2
		and int(picked[0]["move_no"]) == 3 and int(picked[1]["move_no"]) == 5)
	var many: Array = []
	for i in range(8):
		many.append({"move_no": i + 1, "kind": "review", "side": "A", "text": "第%d手" % (i + 1)})
	var capped := GameReport.pick_notes(many, 8)
	_check("② 最多 4 条，保留后出现的", capped.size() == 4 and int(capped[3]["move_no"]) == 8)
	var losses := [
		{"move_no": 2, "gtp": "D4", "drop": 0.12, "reason": "小亏"},
		{"move_no": 4, "gtp": "Q16", "drop": 0.35, "reason": "大亏"},
		{"move_no": 8, "gtp": "C3", "drop": 0.20, "reason": "中亏"},
		{"move_no": 99, "gtp": "T1", "drop": 0.90, "reason": "未来"},
	]
	var ls := GameReport.pick_losses(losses, 10)
	_check("③ 亏损手按 drop 降序、最多 2 条、手数截止", ls.size() == 2
		and str(ls[0]["gtp"]) == "Q16" and str(ls[1]["gtp"]) == "C3")
	_check("④ 手顺只取前 N 手", GameReport.move_strip(["D16", "D4", "Q16", "Q4"], 2) == "D16 · D4")
	_check("④ 空手数 / 脏值给空串", GameReport.move_strip(["D16"], 0) == ""
		and GameReport.move_strip("bad", 3) == "")
	_check("⑤ 点评行带手数 / 说话人", GameReport.note_line(notes[0]).contains("第3手")
		and GameReport.note_line(notes[0]).contains("你"))
	_check("⑤ 形势行：有数据给百分比 / 目差，没数据给破折号",
		GameReport.info_line(0.291, -1.9, true).contains("29.1%")
		and GameReport.info_line(0.291, -1.9, true).contains("-1.9")
		and GameReport.info_line(0.5, 0.0, false).contains("—"))
	print("[report] %s：%d 项断言%s" % ["PASS" if _fail == 0 else "FAIL", _total,
		"全部通过" if _fail == 0 else "，%d 项失败" % _fail])
	quit(0 if _fail == 0 else 1)
