extends SceneTree
## 局后只读复盘（v3.7.0 任务 D）前端门卫 —— **纯逻辑**，不渲染、不依赖场景树、不合成输入事件、不许真 sleep。
## 覆盖任务书五条意图：① 翻页两端夹取 ② 只读挡落子 ③ 夹取的脏值 ④ 只读与摸头**同源**
## ⑤ 退出复盘逐项恢复；另加 ⑥ 逐手存档的纯渲染（只渲染后端字段、不评判）、
## ⑦ 复盘期间「别的改动入口」（俗手回档 / 两个开关）一并冻结（修复轮 1 · I1）、
## ⑧ 进复盘的闸门（总结还在途中 / 语音没播完不许进，修复轮 1 · I2）。
## `Main.gd` 没有 `class_name` ⇒ 只能 `preload` 调它的**静态函数**（V11）；
## `Board3D` 用 `Board3D.new()` 调只读数据的方法（`set_stones` 依赖 `_ready()`，这里一概不碰 3D）。
## 运行：godot --headless --path game --script res://tests/test_review_mode.gd

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
	print("[review] 局后只读复盘自检（翻页夹取 / 只读挡落子与摸头 / 冻结与恢复 / 存档渲染）")
	var main_script := preload("res://scripts/ui/Main.gd")

	# —— ① 翻页夹取：两端不动，中间正常加减（计划原文用例） ——
	var n := 7
	_check("① test_step_clamps_at_both_ends：0 手时按「上一手」不动",
			main_script.review_step_target(0, -1, n) == 0)
	_check("① 末手时按「下一手」不动", main_script.review_step_target(n, 1, n) == n)
	_check("① 中间正常加减（3→4 / 3→2）",
			main_script.review_step_target(3, 1, n) == 4
			and main_script.review_step_target(3, -1, n) == 2)

	# —— ② 只读挡落子 ——
	var b := Board3D.new()
	_check("② test_readonly_blocks_placement：新棋盘可落子", b.placement_allowed())
	b.set_readonly(true)
	_check("② set_readonly(true) 后落子判据为假", not b.placement_allowed())
	b.set_readonly(false)
	_check("② set_readonly(false) 后落子判据为真", b.placement_allowed())
	b.locked = true
	_check("② 她在思考时（locked）落子判据同样为假", not b.placement_allowed())
	b.locked = false
	b.free()

	# —— ③ 夹取的脏值：delta=0 / 极大、total=0、cur 越界，一律夹到 0..total 且不抛 ——
	_check("③ delta=0 原地不动", main_script.review_step_target(4, 0, 9) == 4)
	_check("③ delta 极大（+1000）夹到末手", main_script.review_step_target(4, 1000, 9) == 9)
	_check("③ delta 极小（-1000）夹到 0", main_script.review_step_target(4, -1000, 9) == 0)
	_check("③ total=0：任何方向（含 cur 有值）都停在 0",
			main_script.review_step_target(0, 1, 0) == 0
			and main_script.review_step_target(0, -1, 0) == 0
			and main_script.review_step_target(3, 1, 0) == 0)
	_check("③ cur 为负（越界）夹到 0",
			main_script.review_step_target(-5, -1, 9) == 0
			and main_script.review_step_target(-5, 1, 9) == 0)
	_check("③ cur 大于 total（越界）夹到 total",
			main_script.review_step_target(99, 1, 9) == 9
			and main_script.review_step_target(99, 0, 9) == 9)
	_check("③ total 为负（脏值）当 0 处理", main_script.review_step_target(3, 1, -4) == 0)

	# —— ④ 只读与摸头**同源**：`_unhandled_input` 里落子与摸头问的是同一个 `placement_allowed()` ——
	var b2 := Board3D.new()
	b2.set_readonly(true)
	_check("④ 只读后摸头判据（同一入口 placement_allowed）同样为假", not b2.placement_allowed())
	b2.locked = true
	_check("④ readonly 与 locked 彼此独立（两个标志各管各的，可同时为真）",
			b2.readonly and b2.locked and not b2.placement_allowed())
	b2.set_readonly(false)
	_check("④ 只解除只读、locked 仍真 ⇒ 依旧为假", not b2.placement_allowed())
	b2.free()

	# —— ⑤ 退出复盘的逐项恢复（V21）：UI 冻结状态抽成纯函数（V20：沿用既有 _refresh_lock 的思路） ——
	var before := main_script.review_lock_state(false, false, false)   # 进入复盘前
	var during := main_script.review_lock_state(true, false, false)    # 复盘期间
	var after := main_script.review_lock_state(false, false, false)    # 退出复盘后
	_check("⑤ 进入前不冻结（棋盘不需要只读、按钮不灰、聊天可输入）",
			not bool(before["readonly"]) and not bool(before["buttons_disabled"])
			and bool(before["chat_editable"]))
	_check("⑤ 复盘期间：棋盘只读 + 四个按钮置灰 + 聊天禁用",
			bool(during["readonly"]) and bool(during["buttons_disabled"])
			and not bool(during["chat_editable"]))
	_check("⑤ 退出后逐项回到进入前（同一输入 ⇒ 同一状态）", after == before)
	_check("⑤ 她思考中（busy）不设只读、但按钮置灰（既有行为不变、聊天不受影响）",
			not bool(main_script.review_lock_state(false, true, false)["readonly"])
			and bool(main_script.review_lock_state(false, true, false)["buttons_disabled"])
			and bool(main_script.review_lock_state(false, true, false)["chat_editable"]))
	var b3 := Board3D.new()
	b3.set_readonly(true)
	b3.set_readonly(false)
	_check("⑤ 只读标志设真再设假 ⇒ placement_allowed() 回到真（退出复盘逐项恢复的一环）",
			b3.placement_allowed())
	b3.free()

	# —— ⑥ 逐手存档的**纯渲染**（GET /api/review/notes；只渲染后端字段，不评判） ——
	var lines := main_script.review_note_lines(
		[{"move_no": 3, "kind": "aim", "side": "A", "text": "先占角"},
		 {"move_no": 4, "kind": "review", "side": "E", "text": "   "},
		 "脏值",
		 {"move_no": 6, "kind": "review", "side": "E", "text": "我走小了"}],
		[{"move_no": 5, "gtp": "Q16", "drop": 0.32, "reason": "胜率下滑32%"}, 7])
	_check("⑥ 空文本与脏值跳过，其余逐手渲染（含她与你两边）",
			lines.size() == 3
			and str(lines[0]).contains("第3手") and str(lines[0]).contains("先占角")
			and str(lines[1]).contains("第6手") and str(lines[1]).contains("我走小了")
			and str(lines[2]).contains("Q16") and str(lines[2]).contains("胜率下滑32%"))
	_check("⑥ 两份账都空 ⇒ 零行（不硬造内容）", main_script.review_note_lines([], []).is_empty())
	_check("⑥ 非数组脏值（null / 字符串）不抛、返回空", main_script.review_note_lines(null, null).is_empty()
			and main_script.review_note_lines("oops", {}).is_empty())

	# —— ⑦ 复盘期间**别的**改动入口一并冻结（修复轮 1 · I1）——
	# 俗手回档按钮（`_bad_box`，每次 state 事件都会重建）与两个开关（允许虚着 → /api/config、定式导学）
	# 都会改动**真实棋局**，而后端刻意不门禁 /api/undo 与 /api/config ⇒ 复盘期间必须一起冻结。
	# 它们不是 `_buttons`，所以冻结判据必须与棋盘/四个按钮**同一个来源**（`review_lock_state`）。
	_check("⑦ 复盘期间：俗手回档 / 两个开关一并冻结",
			bool(main_script.review_lock_state(true, false, false)["extra_disabled"]))
	_check("⑦ 她思考中**不**冻结它们（既有行为：忙时也能回档 / 改配置，不回退）",
			not bool(main_script.review_lock_state(false, true, false)["extra_disabled"])
			and not bool(main_script.review_lock_state(false, false, false)["extra_disabled"]))
	_check("⑦ 语音未播完同样不冻结它们（同上：只有复盘才冻）",
			not bool(main_script.review_lock_state(false, false, true)["extra_disabled"]))

	# —— ⑧ 进复盘的闸门（修复轮 1 · I2）——
	# 后端 `set_review` 允许 busy 进入、也不取消在途回合：总结还在途中就进复盘，
	# 那一段的**台词 + 语音 + 气泡**会落到复盘期间（破「回看全程不出声」）。
	_check("⑧ 她在跑回合（总结还在途中）时不许进复盘", not main_script.review_can_enter(true, false))
	_check("⑧ 她的语音还没播完时不许进复盘", not main_script.review_can_enter(false, true))
	_check("⑧ 又闲又静（她这一轮收尾、语音也完了）才允许进复盘", main_script.review_can_enter(false, false))

	if _fail == 0:
		print("[review] PASS：", _total, " 项断言全部通过")
	else:
		print("[review] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
