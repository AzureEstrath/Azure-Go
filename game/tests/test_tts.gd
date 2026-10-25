extends SceneTree
## TTS 播放器自检（计划 05：清洗/合成已归后端，前端只播放）——**带断言的门卫**。
## 覆盖：WAV 解析、play_audio/speak_system 的「一条台词只发一次 utterance_started」、
##      无法解析的字节走 speech_skipped、队列溢出丢最旧并补 speech_skipped、真实系统语音通道。
## 运行：godot --headless --path game --script res://tests/test_tts.gd

var _total := 0
var _fail := 0
var _started: Array[int] = []      # utterance_started 收到的分组号
var _skipped: Array[int] = []      # speech_skipped 收到的分组号

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _on_started(group: int) -> void:
	_started.append(group)

func _on_skipped(group: int) -> void:
	_skipped.append(group)

func _count(list: Array, v: int) -> int:
	var n := 0
	for x in list:
		if int(x) == v:
			n += 1
	return n

## 生成 `sec` 秒 16bit 单声道 24kHz 正弦的极简 WAV（时长够长：测试期间不会被播完打断）
func _wav_bytes(sec := 1.0) -> PackedByteArray:
	var rate := 24000
	var n := int(rate * sec)
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

func _wait(sec: float) -> void:
	await create_timer(sec).timeout

func _run() -> void:
	print("[tts] 播放器自检（清洗/合成已归后端）")
	var t := TTSClient.new()
	root.add_child(t)
	t.utterance_started.connect(_on_started)
	t.speech_skipped.connect(_on_skipped)
	await process_frame

	# —— 1) WAV 解析（保留原有用例） ——
	var wav := _wav_bytes()
	var w := t._wav_stream(wav)
	_check("WAV 解析成功且时长≈1.0s", w != null and absf(w.get_length() - 1.0) < 0.1)
	_check("TTSClient 只暴露播放器入口（play_audio/speak_system/is_slow_engine）",
			t.has_method("play_audio") and t.has_method("speak_system")
			and t.has_method("is_slow_engine"))

	# —— 2) play_audio：能播且 utterance_started 只发一次 ——
	t.enabled = true
	t.mode = "openai"
	t.voice = "melo"                       # 非慢引擎：队列上限 3（下面的溢出用例据此）
	t.volume = 0.05
	_started.clear()
	_skipped.clear()
	var g1 := t.play_audio(wav, 7)
	await _wait(0.2)
	_check("play_audio 返回非 0 分组号", g1 > 0)
	_check("utterance_started 只发一次（分组号对得上）", _count(_started, g1) == 1)
	_check("play_audio 未误发 speech_skipped", _skipped.is_empty())

	# —— 3) 无法解析的字节：按「这条不会再响」处理（speech_skipped + 可读提示） ——
	# 用「RIFF 头 + 位深不是 16bit」的坏 WAV（而不是随机字节）：走解析路径判废，不会惊动引擎的 MP3 解码器报错
	var bad := _wav_bytes(0.1)
	bad[34] = 8                            # fmt 的「位深」低字节 16 → 8：拿不到 16bit PCM
	var g2 := t.play_audio(bad, 8)
	await _wait(0.2)
	_check("坏音频走 speech_skipped（分组号对得上）", _count(_skipped, g2) == 1)
	_check("坏音频不会发 utterance_started", _count(_started, g2) == 0)

	# —— 4) speak_system：同构（一条台词只发一次 utterance_started；真起一次 PowerShell） ——
	t.stop()
	_skipped.clear()
	var g3 := t.speak_system("语音通道测试", 9)
	await _wait(0.5)
	_check("speak_system 返回非 0 分组号", g3 > 0)
	_check("speak_system 起进程并只发一次 utterance_started", _count(_started, g3) == 1)
	_check("系统语音进程在跑", t._sapi_pid > 0 and OS.is_process_running(t._sapi_pid))
	t.stop()
	t._cooldown = 0.0                      # 机器上真起不了 PowerShell 时别让冷却拖累后面的队列用例
	await _wait(0.2)

	# —— 4′) suppress_system：CosyVoice 加载期不出系统语音（只显示文字） ——
	t.stop()
	_skipped.clear()
	t.set_suppress_system(true)
	var g6 := t.speak_system("加载期不该念出来", 40)
	await _wait(0.2)
	_check("suppress_system：不出声、只补 speech_skipped",
			_count(_skipped, g6) == 1 and _count(_started, g6) == 0)
	t.set_suppress_system(false)

	# —— 5) 队列溢出：丢最旧的一条并补 speech_skipped ——
	# 第 1 条会立刻开播（不在队列里），随后 3 条把队列填满，第 5 条进来必须挤掉最旧的那条。
	t.voice = "melo"                       # is_slow_engine()==false ⇒ 上限 QUEUE_MAX(3)
	t.mode = "openai"
	t._cooldown = 0.0
	_skipped.clear()
	var g4 := t.play_audio(_wav_bytes(3.0), 10)     # 正在播
	var queued: Array[int] = []
	for i in 3:
		queued.append(t.play_audio(_wav_bytes(3.0), 11 + i))
	var g_last := t.play_audio(_wav_bytes(3.0), 20)
	await _wait(0.2)
	_check("溢出丢的是最旧那条（补 speech_skipped）", _count(_skipped, queued[0]) == 1)
	_check("溢出只丢一条", _skipped.size() == 1)
	_check("正在播的那条不受影响", _count(_skipped, g4) == 0 and _count(_started, g4) == 1)
	_check("新进来的一条留在队列里（没被丢）", _count(_skipped, g_last) == 0)
	t.stop()

	# —— 6) stop()：连「正在播的那条」也要补 speech_skipped（否则 Main 的棋盘锁收不了账） ——
	t.stop()
	_skipped.clear()
	var g5 := t.play_audio(_wav_bytes(2.0), 30)
	await _wait(0.15)
	_check("stop() 前它在播（utterance_started 已发）", _count(_started, g5) == 1)
	t.stop()
	await _wait(0.15)
	_check("stop() 给正在播的那条补了 speech_skipped", _count(_skipped, g5) == 1)

	# —— 7) cancel_kind / is_speaking 仍可用（Main 的走神作废依赖它） ——
	t.stop()
	_check("stop() 后队列清空（is_speaking 为假）", not t.is_speaking())
	_check("cancel_kind(\"\") 是 no-op", not t.cancel_kind(""))
	_check("is_slow_engine 仍是纯函数（melo 不算慢）", not t.is_slow_engine())
	t.voice = "中文女"
	_check("is_slow_engine 认得慢引擎音色", t.is_slow_engine())
	t.mode = "aliyun"                      # 云端实时流式：不是慢引擎，队列上限按 3 算
	_check("is_slow_engine 不把 aliyun 当慢引擎", not t.is_slow_engine())
	t.mode = "system"
	_check("is_slow_engine 不把 system 当慢引擎", not t.is_slow_engine())

	if _fail == 0:
		print("[tts] PASS：", _total, " 项断言全部通过")
	else:
		print("[tts] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
