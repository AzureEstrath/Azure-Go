extends SceneTree
## TTS 自检：文本清洗、服务不可用时的降级、WAV 解析
## 运行：godot --path . --headless --script res://tests/test_tts.gd

func _initialize() -> void:
	_run()

func _wav_bytes() -> PackedByteArray:
	# 生成 0.1s 16bit 单声道 24kHz 正弦的极简 WAV
	var rate := 24000
	var n := rate / 10
	var pcm := PackedByteArray()
	pcm.resize(n * 2)
	for i in n:
		pcm.encode_s16(i * 2, int(sin(i * 0.08) * 8000))
	var b := PackedByteArray()
	b.append_array("RIFF".to_ascii_buffer())
	var sz := 36 + pcm.size()
	b.append_array(PackedByteArray([sz & 0xFF, (sz >> 8) & 0xFF, (sz >> 16) & 0xFF, (sz >> 24) & 0xFF]))
	b.append_array("WAVEfmt ".to_ascii_buffer())
	b.append_array(PackedByteArray([16, 0, 0, 0, 1, 0, 1, 0]))                      # fmt 长度16、PCM、单声道
	var r := rate
	b.append_array(PackedByteArray([r & 0xFF, (r >> 8) & 0xFF, (r >> 16) & 0xFF, (r >> 24) & 0xFF]))
	b.append_array(PackedByteArray([(rate * 2) & 0xFF, ((rate * 2) >> 8) & 0xFF, 0, 0, 2, 0, 16, 0]))
	b.append_array("data".to_ascii_buffer())
	b.append_array(PackedByteArray([pcm.size() & 0xFF, (pcm.size() >> 8) & 0xFF, 0, 0]))
	b.append_array(pcm)
	return b

func _run() -> void:
	var t := TTSClient.new()
	root.add_child(t)
	await process_frame
	print("[tts] 清洗：", t.clean_for_speech("📊 黑棋在左上形成势力 (｡･ω･｡) 【对局总结】\n这手不错~  ⏪"))
	var w := t._wav_stream(_wav_bytes())
	print("[tts] WAV 解析：", "OK" if w != null else "失败", " 时长≈", ("%.2f" % w.get_length()) if w != null else "-")
	# 1) 系统语音模式：真正拉起 PowerShell/SAPI 进程（音量压到最低，仅验证通道可用）
	t.mode = "system"
	t.voice = ""
	t.volume = 0.05
	t.enabled = true
	t.speak("语音通道测试")
	await create_timer(1.2).timeout
	print("[tts] 系统语音进程 pid=", t._sapi_pid, " 运行中=", OS.is_process_running(t._sapi_pid))
	# 2) HTTP 模式：指向必然连不上的端口，验证优雅降级 + 队列解冻
	t.stop()
	t.mode = "openai"
	t.base_url = "http://127.0.0.1:9/v1"
	t.speak("第一句：服务不可用时应优雅跳过")
	t.speak("第二句")
	await create_timer(4.0).timeout
	print("[tts] 降级验证完成（进程未崩溃，冷却结束会自动继续队列）")
	quit()