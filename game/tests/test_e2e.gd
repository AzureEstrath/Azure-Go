extends SceneTree
## 临时端到端自测：真实 KataGo + 真实流程

var _main = null
var _msgs: Array = []

func _initialize() -> void:
	_main = load("res://scenes/Main.tscn").instantiate()
	root.add_child(_main)
	_run()

func _wait_idle(agent, budget_ms: int, tag: String) -> int:
	var t0 := Time.get_ticks_msec()
	while agent.busy and Time.get_ticks_msec() - t0 < budget_ms:
		await process_frame
	var el := Time.get_ticks_msec() - t0
	print("[e2e] %s 用时 %d ms busy=%s" % [tag, el, agent.busy])
	return el

func _run() -> void:
	await create_timer(1.5).timeout
	var agent = _main.agent
	var st = agent.st
	agent.message.connect(func(kind, text): _msgs.append("%s|%s" % [kind, text]))
	print("[e2e] kataGo_dead=", _main.katago.is_dead(), " llm=", _main.cfg.llm_base_url)

	# —— 第一手 ——
	agent.player_move(3, 3)
	await _wait_idle(agent, 90000, "第1手")
	print("[e2e] moves=", st.moves, " seq=", st.move_seq)
	var c1: Dictionary = st.board.count()
	print("[e2e] stones black=", c1["black"], " white=", c1["white"], " at(3,3)=", st.board.at(3, 3))

	# —— 第二手 ——
	var p2 := Vector2i(15, 15)
	if st.board.at(p2.x, p2.y) != GoBoard.EMPTY:
		p2 = Vector2i(15, 3)
	agent.player_move(p2.x, p2.y)
	await _wait_idle(agent, 90000, "第2手")
	var c2: Dictionary = st.board.count()
	print("[e2e] moves=", st.moves.size(), " black=", c2["black"], " white=", c2["white"], " snapshots=", st.snapshots.size())
	var pl: Dictionary = st.payload()
	print("[e2e] payload stones=", (pl["stones"] as Array).size(), " move_count=", pl["move_count"])
	print("[e2e] memory_log=", st.memory_log)

	# —— 局面分析 / 提示 / 聊天 ——
	agent.analyze_position()
	await _wait_idle(agent, 90000, "局面分析")
	agent.chat("现在形势怎么样？")
	await _wait_idle(agent, 90000, "聊天")

	for m in _msgs:
		print("[msg] ", m)

	# —— 断言 ——
	var ok := true
	if st.moves.size() != 4:
		ok = false; print("[e2e] FAIL 手数应为 4，实为 ", st.moves.size())
	if st.board.at(3, 3) != "black":
		ok = false; print("[e2e] FAIL (3,3) 应为黑子")
	if int(c2["white"]) < 2:
		ok = false; print("[e2e] FAIL Azure 应落 2 子")
	if (pl["stones"] as Array).size() != int(pl["move_count"]):
		ok = false; print("[e2e] FAIL 棋子数与手数不一致")
	if st.snapshots.size() != 2:
		ok = false; print("[e2e] FAIL 快照数应为 2")
	var tags := {}
	for m in st.memory_log:
		tags[str((m as Dictionary).get("tag", ""))] = true
	if not tags.has("E"):
		ok = false; print("[e2e] FAIL 未记录带标签的 Estarth 落子评定（E）")
	if not tags.has("A"):
		ok = false; print("[e2e] FAIL 未记录带标签的 Azure 落子思路（A）")
	var joined := " ".join(PackedStringArray(_msgs))
	# 注意：「关闭思考重试」只在模型把 token 耗在思考段、content 为空时才会触发，
	# 模型正常直答时不会出现，因此不再要求出现该链路，只检查没有调用错误/思考段泄漏
	if joined.contains("调用出错"):
		ok = false; print("[e2e] FAIL LLM 调用出错")
	if joined.contains("推理"):
		ok = false; print("[e2e] FAIL 思考段落未被剥离")
	print("[e2e] RESULT=", "PASS" if ok else "FAIL")

	# 可选：截图（人工核对视觉，如斜视角下最后一手标记是否仍为正对玩家的圆点）
	# 注意用独立参数名 --tshot/--tyaw/--tpitch，避免与 Main 的调试参数 --shot 冲突
	var shot := ""
	var yaw := 40.0
	var pitch := 30.0
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--tshot="):
			shot = a.substr("--tshot=".length())
		elif a.begins_with("--tyaw="):
			yaw = float(a.substr("--tyaw=".length()))
		elif a.begins_with("--tpitch="):
			pitch = float(a.substr("--tpitch=".length()))
	if shot != "":
		_main.board.set_view(yaw, pitch)
		await create_timer(0.6).timeout
		await RenderingServer.frame_post_draw
		var e := root.get_texture().get_image().save_png(shot)
		print("[e2e] shot %s -> %s" % [error_string(e), shot])

	_main.katago.shutdown()
	quit(0 if ok else 1)