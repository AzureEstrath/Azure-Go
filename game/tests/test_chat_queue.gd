extends SceneTree
## 聊天排队（ChatQueue）自检 —— **带断言的门卫**。
## 覆盖：FIFO / 空文本忽略 / 上限 / 提示文案（条数 + 队头前 8 字 + 截断）/ push_front 保序 / should_flush 三条判据。
## 运行：godot --headless --path game --script res://tests/test_chat_queue.gd

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
	print("[chatq] 聊天排队自检（FIFO / 上限 / 提示文案 / 能不能发）")

	# —— ① 空队列与空文本 ——
	var q := ChatQueue.new()
	_check("① 空队列：is_empty / size=0 / 提示为空串",
			q.is_empty() and q.size() == 0 and q.hint_text() == "" and q.peek() == "" and q.pop() == "")
	_check("① 空文本与纯空白不入队",
			not q.enqueue("") and not q.enqueue("   ") and not q.enqueue("\t\n") and q.is_empty())

	# —— ② FIFO + 提示文案（条数 + 队头前 8 字） ——
	_check("② 入队成功返回 true", q.enqueue("第一条") and q.enqueue("第二条"))
	_check("② peek 是队头、size 对", q.peek() == "第一条" and q.size() == 2)
	_check("② hint_text：条数 + 队头前 8 字", q.hint_text() == "排队中 2 条 · 第一条")
	_check("② pop 出队即 FIFO", q.pop() == "第一条" and q.size() == 1 and q.peek() == "第二条")
	_check("② 出到空：peek/pop 回空串、提示回空串",
			q.pop() == "第二条" and q.is_empty() and q.peek() == "" and q.pop() == "" and q.hint_text() == "")

	# —— ③ 提示文案的截断 ——
	var q2 := ChatQueue.new()
	q2.enqueue("一二三四五六七八九十")
	_check("③ 超过 8 字截断并补省略号", q2.hint_text() == "排队中 1 条 · 一二三四五六七八…")
	var q2b := ChatQueue.new()
	q2b.enqueue("一二三四五六七八")
	_check("③ 正好 8 字不补省略号", q2b.hint_text() == "排队中 1 条 · 一二三四五六七八")

	# —— ④ 上限 ——
	var q3 := ChatQueue.new()
	q3.enqueue("1")
	q3.enqueue("2")
	q3.enqueue("3")
	q3.enqueue("4")
	q3.enqueue("5")
	_check("④ 上限 5 条：第 6 条被拒、队列不变",
			not q3.enqueue("6") and q3.size() == 5 and q3.peek() == "1"
			and q3.hint_text() == "排队中 5 条 · 1")

	# —— ⑤ push_front 保序（发出被挡下时退回队头） ——
	var q5 := ChatQueue.new()
	q5.enqueue("乙")
	q5.push_front("甲")
	_check("⑤ push_front 插到队头（退回的那条仍是下一条该发的）",
			q5.peek() == "甲" and q5.size() == 2 and q5.hint_text() == "排队中 2 条 · 甲")
	q5.clear()
	_check("⑤ clear 清空", q5.is_empty() and q5.hint_text() == "")

	# —— ⑥ should_flush：非空 + 不忙 + 不朗读，三条一起才算真 ——
	var q6 := ChatQueue.new()
	_check("⑥ 空队列永远不 flush（哪怕不忙不朗读）",
			not q6.should_flush({"busy": false, "speaking": false}))
	q6.enqueue("甲")
	_check("⑥ 忙时不 flush", not q6.should_flush({"busy": true, "speaking": false}))
	_check("⑥ 她还在朗读时不 flush", not q6.should_flush({"busy": false, "speaking": true}))
	_check("⑥ 又闲又静才 flush", q6.should_flush({"busy": false, "speaking": false}))
	_check("⑥ 缺键按「不忙不朗读」处理（只认显式 true）",
			q6.should_flush({}) and not q6.should_flush({"busy": true}))

	if _fail == 0:
		print("[chatq] PASS：", _total, " 项断言全部通过")
	else:
		print("[chatq] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
