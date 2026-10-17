extends SceneTree
## `NetClient` 聊天排队分发自检 —— **带断言的门卫**（不建场景树、不触网：只验决策面）。
## 覆盖：忙则入队 / 提示文案 / 信号 / 忙·朗读中·未连接都不发 / 队列满不吞 / 回包判忙。
## 运行：godot --headless --path game --script res://tests/test_net_chat.gd
##
## 注意：故意**不**把 `NetClient` 加进场景树 —— 那样 `_ready()` 会去连 WS；这里只测「要不要发 / 要不要排」。

var _total := 0
var _fail := 0
var _changed := 0          # `chat_queue_changed` 收到的次数（GDScript 的 lambda 按值捕获，计数必须用成员变量）

func _initialize() -> void:
	_run()

func _on_queue_changed() -> void:
	_changed += 1

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[netchat] 聊天排队分发自检（入队 / 提示 / 门禁 / 不吞）")

	# —— 回包判定（纯函数：成功 `{"ok": true}`；忙时 `{"ok": false, "busy": true, …}`） ——
	_check("回包 ok:true 判「收下了」", NetClient._chat_reply_ok('{"ok": true}'.to_utf8_buffer()))
	_check("回包 ok:false + busy:true 判「没收下」（要退回队列）",
			not NetClient._chat_reply_ok('{"ok": false, "busy": true, "state": {}}'.to_utf8_buffer()))
	_check("空包 / HTML 错误页 判「没收下」（退回，不丢话）",
			not NetClient._chat_reply_ok(PackedByteArray())
			and not NetClient._chat_reply_ok("<html>502</html>".to_utf8_buffer()))

	# —— 忙则入队 ——
	var nc := NetClient.new()
	nc.chat_queue_changed.connect(_on_queue_changed)
	nc.busy = true
	_check("忙时 chat() 入队并返回 true",
			nc.chat("甲") and nc.has_queued_chat() and nc.queued_chat_count() == 1)
	_check("空文本既不排队也不算「没收」（返回 true、队列不变）",
			nc.chat("") and nc.chat("   ") and nc.queued_chat_count() == 1)
	nc.chat("乙")
	_check("提示文案只带队头前 8 字", nc.queued_chat_hint() == "排队中 2 条 · 甲")
	_check("每次入队都发 chat_queue_changed", _changed == 2)

	# —— 门禁：忙 / 朗读中 / 未连接都不发（消息原样留队） ——
	nc.flush_queued_chat({"busy": true, "speaking": false})
	nc.flush_queued_chat({"busy": false, "speaking": true})
	_check("忙 / 还在朗读时不 flush（原样留队）",
			nc.queued_chat_count() == 2 and nc.queued_chat_hint() == "排队中 2 条 · 甲")
	nc.flush_queued_chat({"busy": false, "speaking": false})
	_check("空闲但未连接：不丢消息（原样留队）", nc.queued_chat_count() == 2)
	_check("被门禁拦下时不发多余信号", _changed == 2)

	# —— 队列满：不吞 ——
	nc.chat("丙")
	nc.chat("丁")
	nc.chat("戊")
	_check("队列满：第 6 条返回 false 且队列不变",
			not nc.chat("己") and nc.queued_chat_count() == 5)

	# —— 清空 ——
	nc.clear_queued_chat()
	_check("清空后提示为空", not nc.has_queued_chat() and nc.queued_chat_hint() == "" and _changed == 6)

	nc.free()

	if _fail == 0:
		print("[netchat] PASS：", _total, " 项断言全部通过")
	else:
		print("[netchat] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
