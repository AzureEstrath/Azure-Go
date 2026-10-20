extends SceneTree
## 定式参考图卡片（JosekiCard）自检 —— **纯逻辑门卫**，不渲染、不依赖场景树、不许真 sleep。
## 覆盖任务书 07-T8 的九条意图：缺字段不崩 / pass 与未知键忽略 / clear 收起 /
## show_card 缺省 / 收尾只有 `formed`（U45：`deviated` 不是收尾）/ k 越界不抛 / 避让状态机 /
## 兜底 TTL / stone_count 合法性；
## 另加修复轮 1 的三条：⑩ 避让的脏输入不抛（M5）、⑪ 拖动的松键与边界（M2）、
## ⑥ 里的高亮/候选/走错判据（M4：从 `_draw` 里抽成纯函数才测得到）。
## 运行：godot --headless --path game --script res://tests/test_joseki_card.gd

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

## 把若干矩形装成 `Array[Rect2]`（方法签名是强类型数组，不靠隐式转换）
func _rects(list: Array) -> Array[Rect2]:
	var out: Array[Rect2] = []
	for r in list:
		out.append(r)
	return out

func _run() -> void:
	print("[josekicard] 定式参考图卡片自检（状态机 / 缺省 / 避让 / TTL / 合法棋子数）")

	# —— ① 缺字段不崩（计划原文用例，保留） ——
	var c1 := JosekiCard.new()
	c1.apply_event({"type": "joseki", "kind": "entered"})
	_check("test_missing_fields_do_not_crash（缺 line/k/candidates 时安全缺省且显示）",
			c1.state() == "shown" and c1.stone_count() == 0)
	c1.free()

	# —— ② pass 与未知键忽略（计划原文用例，保留） ——
	var c2 := JosekiCard.new()
	c2.apply_event({
		"type": "joseki", "kind": "entered",
		"junk": {"nested": [1, 2, 3]},                       # 未知键：忽略
		"line": [
			{"x": 3, "y": 3, "c": "B"},
			{"x": -1, "y": -1, "c": "?"},                    # 越界：不画不计数
			"pass",                                          # 非字典（后端本不会给，防御）：忽略
		],
		"candidates": [{"name": "点三三·定形", "next": [4, 3]}, {"name": "坏候选", "next": "pass"}],
	})
	_check("test_pass_and_unknown_fields_are_ignored（只数得出 1 颗合法子）",
			c2.stone_count() == 1 and c2.state() == "shown")
	c2.free()

	# —— ③ clear 收起（计划原文用例，保留） ——
	var c3 := JosekiCard.new()
	c3.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	c3.clear()
	_check("test_clear_hides_it（clear 后 state=hidden、棋子清零）",
			c3.state() == "hidden" and c3.stone_count() == 0)
	c3.free()

	# —— ④ show_card 缺省＝显示；显式 false＝不显示（U7） ——
	var c4 := JosekiCard.new()
	c4.apply_event({"kind": "entered", "show_card": false,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": 4, "y": 3, "c": "W"}]})
	_check("④ show_card=false → hidden 且不留棋子（连数据一起收起）",
			c4.state() == "hidden" and c4.stone_count() == 0)
	c4.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("④ 缺 show_card → 按 true 显示（旧后端无该字段也不崩）",
			c4.state() == "shown" and c4.stone_count() == 1)
	c4.apply_event({"kind": "entered", "show_card": true, "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("④ show_card=true → 显示", c4.state() == "shown")

	# —— ⑤ 收尾只有 `formed` 一种；`deviated`（走错）**不是收尾**（U45） ——
	c4.apply_event({"kind": "formed"})
	_check("⑤ kind=formed → 立即 hidden（库线走完才是收尾）",
			c4.state() == "hidden" and c4.stone_count() == 0)
	# 回归：真走错那一手是「第 4 次起」唯一会上卡片的事件，绝不能被当成收尾收掉
	c4.apply_event({"kind": "deviated", "show_card": true,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": 5, "y": 5, "c": "W"}],
			"mistake": {"move_no": 5, "x": 1, "y": 4, "drop": 0.2}})
	_check("⑤ deviated + show_card=true → **不是收尾**，卡片照样上（U45 回归）",
			c4.state() == "shown" and c4.stone_count() == 2)
	_check("⑤ deviated 带的 mistake 那一手能标红（`_mistake_cell` 命中）",
			c4._mistake_cell() == Vector2i(1, 4))
	c4.apply_event({"kind": "deviated", "show_card": false})
	_check("⑤ deviated + show_card=false → 不上卡片（hidden）",
			c4.state() == "hidden" and c4.stone_count() == 0)
	c4.apply_event({"kind": "deviated"})
	_check("⑤ deviated 缺 show_card → 按 true 上卡片（与 entered 同一套缺省）", c4.state() == "shown")
	c4.free()

	# —— ⑥ k 高亮 / 候选记号 / 走错标红的判据（M4） ——
	# 这三条判据原先只活在 `_draw` 里，headless 门卫永远跑不到；抽成纯函数才测得到。
	var c5 := JosekiCard.new()
	var bad := JosekiCard.INVALID
	c5.apply_event({"kind": "entered", "k": 0, "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑥ k=0 命中第 0 手（高亮格 = 那颗子）", c5._k_cell() == Vector2i(3, 3))
	c5.apply_event({"kind": "entered", "k": 99, "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑥ k 远大于 len(line) 不抛，且不高亮（判据回哨兵）",
			c5._k_cell() == bad and c5.state() == "shown")
	c5.apply_event({"kind": "entered", "k": -5, "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑥ k 为负 → 不高亮", c5._k_cell() == bad)
	c5.apply_event({"kind": "entered", "k": 0, "line": []})
	_check("⑥ line 为空 + k=0 不抛，且不高亮", c5._k_cell() == bad and c5.state() == "shown")
	c5.apply_event({"kind": "entered", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": -5, "y": 3, "c": "W"}]})
	_check("⑥ k 指到非法坐标那一手 → 不高亮", c5._k_cell() == bad)
	c5.apply_event({"kind": "entered", "k": "x", "line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑥ k 是脏值（非数字）不抛，且不高亮", c5._k_cell() == bad and c5.state() == "shown")

	# 候选记号 / 走错标红的位置（同一套「抽出来才可测」）
	c5.apply_event({"kind": "entered",
			"line": [{"x": 3, "y": 3, "c": "B"}],
			"candidates": [{"name": "甲", "next": [4, 3]}, {"name": "坏", "next": "pass"},
					{"name": "乙", "next": [5, 5]}, {"name": "越界", "next": [0, 99]}],
			"mistake": {"move_no": 5, "x": 1, "y": 4, "drop": 0.2}})
	var cc := c5._candidate_cells()
	_check("⑥ 候选记号只给得出合法坐标的两格（非法候选不占位、不抛）",
			cc.size() == 2 and cc[0] == Vector2i(4, 3) and cc[1] == Vector2i(5, 5))
	_check("⑥ mistake 标红格 = 那一手坐标", c5._mistake_cell() == Vector2i(1, 4))
	c5.apply_event({"kind": "entered", "mistake": {"x": -3, "y": 0}})
	_check("⑥ mistake 坐标越界 → 不标红", c5._mistake_cell() == bad)
	c5.apply_event({"kind": "entered", "mistake": "oops"})
	_check("⑥ mistake 是脏类型 → 不标红且不抛", c5._mistake_cell() == bad)
	c5.apply_event({"kind": "entered"})
	_check("⑥ 没有 mistake → 不标红", c5._mistake_cell() == bad)
	c5.free()

	# —— ⑦ 避让状态机（U40） ——
	var c6 := JosekiCard.new()
	c6.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	var mine := c6.card_rect()
	var cover := Rect2(mine.get_center() - Vector2(8, 8), Vector2(16, 16))   # 含卡片中心
	c6.set_panel_overlap(_rects([cover]))
	_check("⑦ 与矩形重叠 → faded", c6.state() == "faded")
	c6.set_panel_overlap(_rects([]))
	_check("⑦ 空数组 → 回到 shown", c6.state() == "shown")
	c6.set_panel_overlap(_rects([cover]))
	c6.set_panel_overlap(_rects([Rect2(mine.end + Vector2(400, 400), Vector2(10, 10))]))
	_check("⑦ 不相交的矩形 → 回到 shown", c6.state() == "shown")
	c6.apply_event({"kind": "formed"})
	c6.set_panel_overlap(_rects([cover]))
	_check("⑦ hidden 时喂矩形不唤醒（仍 hidden）", c6.state() == "hidden")
	c6.free()

	# —— ⑧ 兜底 TTL：注入时间推进，不许真 sleep（U38） ——
	var c7 := JosekiCard.new()
	c7.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	c7._tick(JosekiCard.CARD_TTL_SEC - 1.0)
	_check("⑧ TTL 未到（还差 1 秒）→ 仍是 shown",
			c7.state() == "shown" and c7.stone_count() == 1)
	c7._tick(2.0)
	_check("⑧ 超过 CARD_TTL_SEC 无新事件 → 自动 hidden（兜底「脱先」）",
			c7.state() == "hidden" and c7.stone_count() == 0)
	c7.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	c7._tick(JosekiCard.CARD_TTL_SEC - 1.0)
	c7.apply_event({"kind": "entered", "line": [{"x": 4, "y": 3, "c": "W"}]})
	c7._tick(JosekiCard.CARD_TTL_SEC - 1.0)
	_check("⑧ 新事件重置 TTL（中段推进不被误判成收尾）",
			c7.state() == "shown" and c7.stone_count() == 1)
	c7.apply_event({"kind": "formed"})
	c7._tick(1.0)
	_check("⑧ hidden 时 _tick 空转不抛", c7.state() == "hidden")
	c7.free()

	# —— ⑨ stone_count 的合法性（U40） ——
	var c8 := JosekiCard.new()
	c8.apply_event({"kind": "entered", "line": [
		{"x": -1, "y": 0, "c": "B"},        # 越界（负）
		{"x": 99, "y": 0, "c": "B"},        # 越界（大于 18）
		{"x": "a", "y": 3, "c": "W"},       # 非数字
		{"y": 3, "c": "W"},                 # 缺 x
		{"x": 3.5, "y": 3, "c": "W"},       # 非整数（JSON 整数只会是 x.0）
		"pass",                              # 非字典
		null,                                # null
	]})
	_check("⑨ 非法坐标一律不计数", c8.stone_count() == 0)
	c8.apply_event({"kind": "entered", "line": [
		{"x": 3, "y": 3, "c": "B"}, {"x": 4, "y": 3, "c": "W"}]})
	_check("⑨ 合法黑白各一 → 2", c8.stone_count() == 2)
	c8.apply_event({"kind": "entered", "line": [{"x": 5.0, "y": 5.0, "c": "W"}]})
	_check("⑨ JSON 整数以 float 到达（NetClient 走 JSON.parse_string）也计入", c8.stone_count() == 1)
	c8.apply_event({"kind": "entered", "line": []})
	_check("⑨ line 空 → 0", c8.stone_count() == 0)
	c8.clear()
	_check("⑨ clear 后计数归零且 state=hidden", c8.stone_count() == 0 and c8.state() == "hidden")
	c8.free()

	# —— ⑩ 脏输入不抛（M5）：入参是**无类型 `Array`**，非 Rect2 项当「没有这个矩形」 ——
	var c9 := JosekiCard.new()
	c9.apply_event({"kind": "entered", "line": [{"x": 3, "y": 3, "c": "B"}]})
	c9.set_panel_overlap(["垃圾", 3, null, Vector2(1, 1), {}])
	_check("⑩ 全是非 Rect2 的脏数组 → 不抛、按不重叠处理（shown）", c9.state() == "shown")
	var cover2 := Rect2(c9.card_rect().get_center() - Vector2(8, 8), Vector2(16, 16))
	c9.set_panel_overlap(["垃圾", cover2])
	_check("⑩ 脏项跳过、真矩形照判（faded）", c9.state() == "faded")
	c9.set_panel_overlap([])
	_check("⑩ 空数组 → 回到 shown", c9.state() == "shown")
	c9.free()

	# —— ⑪ 拖动（M2）：松键递不到卡片时，下一次裸移动不能把卡片带走 ——
	# 门卫直接驱动内部标记（仓库既有门卫也这么干，见 test_speech_pairing.gd 的 `t._cooldown = 0.0`）
	var ca := JosekiCard.new()
	ca.position = Vector2(300, 300)
	ca._dragging = true
	ca._drag_step(Vector2(20, -10), true)
	_check("⑪ 按住时正常拖动", ca.position == Vector2(320, 290))
	ca._drag_step(Vector2(50, 50), false)
	_check("⑪ 真键位已松开 → 解除拖动标记且不动",
			ca.position == Vector2(320, 290) and not ca._dragging)
	ca._drag_step(Vector2(50, 50), true)
	_check("⑪ 松键后的裸移动不再漂移（没在拖动就不动）", ca.position == Vector2(320, 290))
	ca._dragging = true
	ca._drag_step(Vector2(-1000000.0, -1000000.0), true)
	_check("⑪ 树外拖动不做视口 clamp（只推进位置，纯逻辑）",
			ca.position == Vector2(320, 290) + Vector2(-1000000.0, -1000000.0))
	_check("⑪ clamp：至少留 48px 可见（左/下边界）",
			ca._clamp_pos(Vector2(-500, 900), Vector2(1280, 800)) == Vector2(48.0 - JosekiCard.CARD_W, 800.0 - 48.0))
	_check("⑪ clamp：右/上边界不被推出窗口",
			ca._clamp_pos(Vector2(9999, -9999), Vector2(1280, 800)) == Vector2(1280.0 - 48.0, 0.0))
	ca.free()

	# —— ⑫ F5：后端 `points`（JSON 数组 / 对象）→ 棋盘高亮认的 `Vector2i`（Main.joseki_points） ——
	# 后端从 Python 元组 `(x, y)` 经 JSON 化到前端是数组 `[x, y]`，而 Board3D.highlight_points 只认
	# `Vector2i`——此前缺这一段转换，高亮轨迹一笔都画不出来。Main.gd 无 class_name，用 preload 调静态方法。
	var main_script := preload("res://scripts/ui/Main.gd")
	var jpts := main_script.joseki_points({"points": [[3, 3], {"x": 15, "y": 3}, [5, 5]]})
	_check("⑫ [x,y] 与 {x,y} 两种形态都转成 Vector2i（顺序不变）",
			jpts.size() == 3 and jpts[0] == Vector2i(3, 3)
			and jpts[1] == Vector2i(15, 3) and jpts[2] == Vector2i(5, 5))
	_check("⑫ JSON 整数以 float 到达（x.0）也认",
			main_script.joseki_points({"points": [[3.0, 3.0]]})[0] == Vector2i(3, 3))
	var bad_pts := main_script.joseki_points({"points": [
		"pass", null, 3, {}, [1],
		["a", 2], [1.5, 2], [-1, 4], [19, 4], [4, 19],
		{"x": 3}, {"y": 4}, {"x": -1, "y": 2},
		[3, 4],
	]})
	_check("⑫ 脏值 / pass / 越界 / 缺键一律跳过，只留合法那一手（不抛）",
			bad_pts.size() == 1 and bad_pts[0] == Vector2i(3, 4))
	_check("⑫ 坐标对多带的元素只读前两个（[1,2,3] → (1,2)）",
			main_script.joseki_points({"points": [[1, 2, 3]]})[0] == Vector2i(1, 2))
	_check("⑫ points 为空列表 → 空数组", main_script.joseki_points({"points": []}).is_empty())
	_check("⑫ points 非列表（脏值）→ 空数组、不抛", main_script.joseki_points({"points": "oops"}).is_empty())
	_check("⑫ payload 没有 points 键 → 空数组", main_script.joseki_points({}).is_empty())

	# —— ⑬ 避让只淡「壳」：整卡 modulate 不许再动（v3.9.2）——
	# 症状（用户截图）：卡片被棋盘压住时整卡压到 28% → 12px 标题在浅底上等于消失，
	# 参考图变成一块灰影。现在只淡底色 / 网格 / 边框，标题与棋子保持不透明。
	var cb := JosekiCard.new()
	cb.apply_event({"kind": "entered", "name": "甲", "line": [{"x": 3, "y": 3, "c": "B"}], "k": 0})
	_check("⑬ 新卡：dim=1（正常）", is_equal_approx(cb.dim(), 1.0))
	cb.set_panel_overlap([Rect2(cb.card_rect().position + Vector2(10, 10), Vector2(20, 20))])
	_check("⑬ 被压住 ⇒ faded，且只有外壳变淡", cb.state() == "faded" and cb.dim() < 0.6)
	_check("⑬ 淡出不许动整卡透明度（否则字跟着一起糊）",
			is_equal_approx(cb.modulate.a, 1.0))
	cb.set_panel_overlap([])
	_check("⑬ 不压 ⇒ 外壳恢复不透明", is_equal_approx(cb.dim(), 1.0))
	cb.free()

	# —— ⑭ 落位是纯函数：可行位既不压棋盘格、也不出窗（v3.9.2）——
	# 旋转后的棋盘是菱形，用外接矩形判重叠会把两腰的空白也算成棋盘；这里按「格子矩形」判定，
	# 并挑**离当前位置最近**的可行位（画面轻微移动时卡片不会来回横跳）。
	var vp := Vector2(1280.0, 800.0)
	var card := Vector2(JosekiCard.CARD_W, JosekiCard.CARD_H)
	var cells: Array[Rect2] = [Rect2(Vector2(150, 323), Vector2(543, 304))]
	var a1: Vector2 = main_script.joseki_anchor(vp, cells, [], Vector2.ZERO, card, false, true)
	_check("⑭ 有棋盘格时落位不压棋盘", not Rect2(a1, card).intersects(cells[0]))
	_check("⑭ 落位完整在窗口内（四边各留 12px）",
			Rect2(Vector2(12, 12), vp - Vector2(24, 24)).encloses(Rect2(a1, card)))
	_check("⑭ 右栏展开 ⇒ 不压右栏", a1.x + card.x <= vp.x - 380.0 - 12.0 + 0.01)

	var small: Array[Rect2] = [Rect2(Vector2(600, 350), Vector2(100, 100))]
	_check("⑭ 当前位置靠上 ⇒ 挑上方候选",
			main_script.joseki_anchor(vp, small, [], Vector2(640, 0), card, false, false).y < 200.0)
	_check("⑭ 当前位置靠左 ⇒ 挑左侧候选",
			main_script.joseki_anchor(vp, small, [], Vector2(0, 420), card, false, false).x < 400.0)
	_check("⑭ 当前位置靠下 ⇒ 挑下方候选",
			main_script.joseki_anchor(vp, small, [], Vector2(640, 790), card, false, false).y > 400.0)
	var blocked: Array[Rect2] = [Rect2(Vector2.ZERO, vp)]
	_check("⑭ 哪都放不下 ⇒ 退回左下角兜底（且不抛）",
			main_script.joseki_anchor(vp, blocked, [], Vector2.ZERO, card, false, false)
			== Vector2(24.0, vp.y - card.y - 24.0))

	# —— ⑮ 关闭入口（v3.9.3）：卡片原先没有任何关闭控件，只能等 120s TTL 或 formed ——
	var cx := JosekiCard.new()
	cx.apply_event({"kind": "branch", "name": "乙", "corner": "左上", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": 3, "y": 2, "c": "B"}],
			"candidates": [{"name": "一间跳", "next": [5, 2]}]})
	_check("⑮ 关闭热区在卡片右上角",
			cx.close_rect().position.x > JosekiCard.CARD_W * 0.6
			and cx.close_rect().position.y < 40.0)
	_check("⑮ 热区中心算命中、棋盘区域不算",
			cx.hit_close(cx.close_rect().get_center()) and not cx.hit_close(Vector2(40, 200)))
	cx.dismiss()
	_check("⑮ dismiss 收起卡片", cx.state() == "hidden")
	cx.apply_event({"kind": "branch", "name": "乙", "corner": "左上", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}], "candidates": []})
	_check("⑮ 手关过的同一结构点不再自动弹", cx.state() == "hidden")
	cx.apply_event({"kind": "deviated", "name": "乙", "corner": "左上", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}], "candidates": []})
	_check("⑮ 换了结构点照常弹", cx.state() == "shown")
	cx.free()

	# —— ⑯ 角内取景窗口：9 路局部放大，任一点越界就退回 19 路（绝不裁子、不把候选画到画外）——
	var cy := JosekiCard.new()
	cy.apply_event({"kind": "entered", "name": "丙", "corner": "左上", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": 3, "y": 2, "c": "B"}]})
	var win1 := cy.view_window()
	_check("⑯ 左上角内两手 ⇒ 9 路取景、原点 (0,0)",
			int(win1["size"]) == 9 and win1["origin"] == Vector2i(0, 0))
	cy.apply_event({"kind": "entered", "name": "丁", "corner": "右下", "k": 1,
			"line": [{"x": 15, "y": 15, "c": "B"}, {"x": 16, "y": 14, "c": "W"}]})
	var win2 := cy.view_window()
	_check("⑯ 右下角 ⇒ 窗口原点 (10,10)",
			int(win2["size"]) == 9 and win2["origin"] == Vector2i(10, 10))
	cy.apply_event({"kind": "entered", "name": "戊", "corner": "左上", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}, {"x": 15, "y": 15, "c": "W"}]})
	_check("⑯ 有子跑出角内 ⇒ 退回 19 路（不裁子）", int(cy.view_window()["size"]) == 19)
	cy.apply_event({"kind": "entered", "name": "戊2", "corner": "左上", "k": 0,
			"line": [{"x": 3, "y": 3, "c": "B"}],
			"candidates": [{"name": "远候", "next": [12, 3]}]})
	_check("⑯ 候选点跑出角内 ⇒ 同样退回 19 路（不把候选画到画外）",
			int(cy.view_window()["size"]) == 19)
	cy.apply_event({"kind": "deviated", "name": "戊3", "corner": "左上", "k": 0,
			"line": [{"x": 3, "y": 3, "c": "B"}],
			"mistake": {"move_no": 2, "x": 12, "y": 12, "drop": 3.0}})
	_check("⑯ 走错点跑出角内 ⇒ 同样退回 19 路", int(cy.view_window()["size"]) == 19)
	cy.apply_event({"kind": "entered", "name": "己", "corner": "?", "k": 1,
			"line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑯ 角名不认识 ⇒ 19 路全盘", int(cy.view_window()["size"]) == 19)

	# —— ⑰ 头部徽标 / 候选图例 / TTL ——
	cy.apply_event({"kind": "branch", "name": "庚", "corner": "左上", "k": 0,
			"line": [{"x": 3, "y": 3, "c": "B"}],
			"candidates": [{"name": "一间跳", "next": [5, 2]}, {}, {"name": ""}, 3,
					{"name": "贴住", "next": [4, 2]}]})
	_check("⑰ 徽标：分岔", cy.badge_text() == "分岔")
	_check("⑰ 候选图例只收有名字的，脏值跳过",
			cy.legend_items() == ["一间跳", "贴住"])
	cy.apply_event({"kind": "deviated", "name": "庚", "corner": "左上", "k": 0,
			"line": [{"x": 3, "y": 3, "c": "B"}]})
	_check("⑰ 徽标：走错", cy.badge_text() == "走错")
	cy._tick(59.0)
	_check("⑰ TTL 60s：59 秒还在", cy.state() == "shown")
	cy._tick(2.0)
	_check("⑰ TTL 60s：61 秒收起", cy.state() == "hidden")
	cy.free()

	if _fail == 0:
		print("[josekicard] PASS：", _total, " 项断言全部通过")
	else:
		print("[josekicard] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
