extends SceneTree
## 语音配对状态机自检（计划 05 · Task 4 · T22）——**带断言的门卫**。
## 这里是本任务最危险的逻辑（配对 / TTL / 占位 / 释放）：出错的表现是「文字卡死 / 静音 / 棋盘锁死」。
## 覆盖：①speech 先于 _deliver_msg 到达 ②只有 message 没有 speech ③音频 404 与解析不了
##       ④跨回合 turn_id 重置 ⑤队列溢出丢最旧 ⑥配对位 TTL 与「只有文字」标记保鲜期 ⑦占位/走神作废
## 运行：godot --headless --path game --script res://tests/test_speech_pairing.gd

const Clock := 1000000                     # 假时钟起点（毫秒）

var _total := 0
var _fail := 0
var _clock_ms := Clock
var _shown: Array[String] = []             # display_requested 收到的台词
var _reveals: Array[bool] = []             # 每条是否要求「落子揭示」
var _resolved := 0

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

# ================= 测试替身：假时钟 + 副作用收集 =================

func _fake_clock() -> int:
	return _clock_ms

func _advance(ms: int) -> void:
	_clock_ms += ms

func _on_display(text: String, reveal: bool) -> void:
	_shown.append(text)
	_reveals.append(reveal)

func _on_resolved() -> void:
	_resolved += 1

func _reset() -> void:
	_clock_ms = Clock
	_shown.clear()
	_reveals.clear()
	_resolved = 0

## 新建一台状态机（每个用例一台）；时钟换成假时钟，输入输出都留在本文件
func _new_pairing() -> SpeechPairing:
	var p := SpeechPairing.new()
	p.clock = Callable(self, "_fake_clock")
	p.display_requested.connect(_on_display)
	p.resolved.connect(_on_resolved)
	root.add_child(p)
	return p

## 交给状态机的「外部事实」（与 Main._pair_facts() 同形）
func _env(turn_active := false, playing := false, fetching := false) -> Dictionary:
	return {"turn_active": turn_active, "playing": playing, "fetching": fetching}

## Main 里那两段胶水的等价物：语音开播 / 被判定无声时按分组号回查 → 出表 →（该显示的）显示。
## 逐句 + 同步说话模式下才等开播显示（`wait_display`），与 Main 的 `_on_utterance_started` 同构。
func _wire_tts(p: SpeechPairing, t: TTSClient) -> void:
	t.utterance_started.connect(func(g: int):
		var rec := p.group_of(g)
		if rec.is_empty():
			return
		p.finish(rec)
		if p.wait_display(rec):
			p.display(rec))
	t.speech_skipped.connect(func(g: int):
		var rec := p.group_of(g)
		if rec.is_empty():
			return
		p.finish(rec)
		p.display(rec))

# ================= 音频素材 =================

## `sec` 秒 16bit 单声道 24kHz 正弦的极简 WAV（时长够长：测试期间不会被播完打断）
func _wav(sec := 1.0) -> PackedByteArray:
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
	b.append_array(PackedByteArray([16, 0, 0, 0, 1, 0, 1, 0]))
	var r := rate
	b.append_array(PackedByteArray([r & 0xFF, (r >> 8) & 0xFF, (r >> 16) & 0xFF, (r >> 24) & 0xFF]))
	b.append_array(PackedByteArray([(rate * 2) & 0xFF, ((rate * 2) >> 8) & 0xFF, 0, 0, 2, 0, 16, 0]))
	b.append_array("data".to_ascii_buffer())
	b.append_array(PackedByteArray([pcm.size() & 0xFF, (pcm.size() >> 8) & 0xFF, 0, 0]))
	b.append_array(pcm)
	return b

## 「RIFF 头 + 位深不是 16bit」的坏 WAV：走解析路径判废（不会惊动引擎的 MP3 解码器报错）
func _broken_wav() -> PackedByteArray:
	var b := _wav(0.1)
	b[34] = 8
	return b

func _wait(sec: float) -> void:
	await create_timer(sec).timeout

# ================= 用例 =================

func _run() -> void:
	print("[pair] 语音配对状态机自检（配对 / 释放 / TTL / 占位）")

	# —— ① speech 先于 _deliver_msg 到达（乱序到达仍能正确配对） ——
	var p := _new_pairing()
	_reset()
	var rec := p.register("甲", true, "")
	var paired := p.accept_speech("t1", 1)
	_check("① speech 先到也配得上（按到达顺序，配的是同一条）", paired.get("id") == rec.get("id"))
	p.set_group(rec, 11)
	p.finish(rec)                                   # 语音先开播（这时还没轮到 _deliver_msg）
	_check("① 语音开播时文字还没进显示队列：先不显示", _shown.is_empty())
	_check("① 记录已出结果（Main 据此在 _deliver_msg 时立刻补显示）", p.played(rec))
	p.arm(rec, true)
	if p.played(rec):
		p.display(rec)
	_check("① 文字补上了（语音先播也不会丢字，揭示也带上）", _shown == ["甲"] and _reveals[0])
	_check("① 没有残留（不会卡住）", not p.has_pending() and not p.busy(_env()))

	# —— ①b 音频在途 / 回合未结束时**不许**提前把文字放出来 ——
	var p1b := _new_pairing()
	_reset()
	var r1b := p1b.register("乙", false, "")
	p1b.arm(r1b, true)
	p1b.accept_speech("t2", 1)                      # 已配对：音频正在后端合成/取回
	_check("①b 已配对但音频还没到：她这一侧算「在忙」", p1b.busy(_env()))
	p1b.release_stale(_env(false, true, false))     # playing=true：音频在播/排队
	_check("①b 音频在途时不放文字（否则会先于声音出现）", _shown.is_empty() and p1b.has_pending())
	p1b.release_stale(_env(true, false, false))     # turn_active=true：回合还在跑
	_check("①b 回合还在跑时也不放", _shown.is_empty())
	p1b.release_stale(_env())                       # 回合结束，但它的音频仍在途（got 且未 played）
	_check("①b 已配对、音频还没到时也不放（等它）", _shown.is_empty() and p1b.has_pending())
	p1b.finish(r1b)                                 # 音频最终没来（404）→ 收账
	p1b.display(r1b)
	_check("①b 取音频失败后才放出来（不卡死）", _shown == ["乙"] and not p1b.busy(_env()))

	# —— ② 只有 message 没有 speech（文字照常显示、锁一定归位、不卡死） ——
	var p2 := _new_pairing()
	_reset()
	var r2 := p2.register("丙", false, "")
	p2.arm(r2, true)
	_check("② 等语音时她这一侧算「在忙」（棋盘锁着）", p2.busy(_env()))
	p2.release_stale(_env())                        # 后端这一轮发完了，还是没有 speech
	_check("② 没有 speech 时文字照常显示", _shown == ["丙"])
	_check("② 锁归位（busy 为假）", not p2.busy(_env(false, false, false)))
	_check("② 没有待显示残留", not p2.has_pending())
	var r2b := p2.register("丙2", false, "")
	p2.arm(r2b, true)
	_check("② 新记录会让棋盘重新锁住（判据可用）", p2.busy(_env()))
	p2.release_stale(_env())
	_check("② 再放一次仍然归位", not p2.busy(_env()) and _shown == ["丙", "丙2"])

	# —— ②b WS 断开（forced）：连在途的都放出来，配对位一起清掉 ——
	var p2b := _new_pairing()
	_reset()
	var r2c := p2b.register("丁", true, "")
	p2b.arm(r2c, true)
	p2b.release_stale(_env(false, true, false), true)   # 音频还在途，但连接断了
	_check("②b 断线时连在途的都放出来（不卡死）", _shown == ["丁"] and _reveals[0])
	_check("②b 等不到语音的配对位也清掉了（不会有孤儿占位）", p2b.pair_count() == 0)

	# —— 真 TTSClient：音频坏了 / 队列溢出的那两个用例要真播一遍 ——
	var t := TTSClient.new()
	t.enabled = true
	t.mode = "openai"
	t.voice = "melo"                                # 非慢引擎：队列上限 QUEUE_MAX=3
	t.volume = 0.05
	root.add_child(t)
	var p3 := _new_pairing()
	_wire_tts(p3, t)
	await process_frame

	# —— ③ 音频无法解析 / 404：走 speech_skipped，文字照常、不锁死 ——
	_reset()
	var r3 := p3.register("戊", true, "")
	p3.arm(r3, true)
	p3.accept_speech("t3", 1)
	var g3 := t.play_audio(_broken_wav(), 1, "")
	p3.set_group(r3, g3)
	await _wait(0.2)
	_check("③ 坏音频走 speech_skipped：文字照常显示（含揭示）", _shown == ["戊"] and _reveals[0])
	_check("③ 不锁死（没有音频在播、也没有等语音的记录）", not p3.busy(_env(false, t.is_speaking(), false)))
	_check("③ 记录已出表", p3.group_of(g3).is_empty())
	_reset()
	var r3b := p3.register("己", false, "")
	p3.arm(r3b, true)
	p3.accept_speech("t3", 2)
	p3.finish(r3b)                                  # Main._speech_audio_failed 的等价物（404 → 收账 + 显示）
	p3.display(r3b)
	_check("③b 404 时文字照常显示、不锁死", _shown == ["己"] and not p3.busy(_env()))

	# —— ④ 跨回合 turn_id 重置（同回合内 seq 必须递增，跨回合从 1 重新开始） ——
	var p4 := _new_pairing()
	_reset()
	var a4 := p4.register("庚", false, "")
	_check("④ 回合 1 的 seq=1 正常配上", p4.accept_speech("t1", 1).get("id") == a4.get("id"))
	_check("④ 同回合内 seq 不递增 ⇒ 判 stale 丢弃",
			p4.accept_speech("t1", 1).is_empty() and p4.last_drop_reason == "stale")
	var b4 := p4.register("辛", true, "")
	var got4 := p4.accept_speech("t2", 1)           # 新回合：seq 从 1 重新开始
	_check("④ 跨回合 seq 重置不串味（配到新回合那条）", got4.get("id") == b4.get("id"))
	_check("④ 没有对应台词时判 orphan（绝不凭空出声）",
			p4.accept_speech("t2", 2).is_empty() and p4.last_drop_reason == "orphan")

	# —— ⑤ 队列溢出：最旧那条被丢并补 speech_skipped，文字照常显示 ——
	t.stop()
	t._cooldown = 0.0
	_reset()
	var skipped: Array[int] = []
	t.speech_skipped.connect(func(g: int): skipped.append(g))
	var groups: Array[int] = []
	var recs5: Array[Dictionary] = []
	for i in 5:
		var r5 := p3.register("溢%d" % i, false, "")
		p3.arm(r5, false)                           # 逐句但「同步说话」关：文字等自己的语音开播（这里只看被丢那条）
		var g5 := t.play_audio(_wav(3.0), i + 1, "")
		p3.set_group(r5, g5)
		groups.append(g5)
		recs5.append(r5)
	await _wait(0.3)
	_check("⑤ 溢出只丢最旧那一条（speech_skipped 只发一次，且正是它）", skipped == [groups[1]])
	_check("⑤ 被丢的那条文字照常显示（不丢字）", _shown == ["溢1"])
	_check("⑤ 被丢的那条已出表（不再占分组位）",
			p3.played(recs5[1]) and p3.group_of(groups[1]).is_empty())
	p3.release_stale(_env(), true)                  # 兜底（例如断线）之后彻底归位
	_check("⑤ 兜底放掉之后她这一侧归位（busy 为假）", not p3.busy(_env()))
	t.stop()

	# —— ⑥ 配对位 TTL + 「只有文字」标记保鲜期（假时钟推进） ——
	var p6 := _new_pairing()
	_reset()
	var r6 := p6.register("壬", false, "")
	p6.arm(r6, true)
	_advance(SpeechPairing.SPEECH_PAIR_TTL_MS - 1000)
	_check("⑥ 还没到窗口：配对位留着（继续等它的语音）", p6.prune_expired().is_empty() and p6.pair_count() == 1)
	_advance(2000)
	var gone := p6.prune_expired()
	_check("⑥ 过了窗口：等不到语音的配对位清出（免得吃掉后面那条的语音）",
			gone == ["壬"] and p6.pair_count() == 0)
	p6.mark_text_only()
	_check("⑥b 标记先被非 ai 类型忽略（不被它们顺手吃掉）", not p6.take_text_only_mark("ai_comment"))
	_check("⑥b kind==ai 时立刻取用有效", p6.take_text_only_mark("ai"))
	p6.mark_text_only()
	_advance(SpeechPairing.TEXT_ONLY_TTL_MS + 1000)
	_check("⑥b 过了保鲜期就失效（那次调用没落到）", not p6.take_text_only_mark("ai"))

	# —— ⑦ 占位（tomb）吃掉它自己那条 speech；走神搭话整批作废 ——
	var p7 := _new_pairing()
	_reset()
	var tomb := p7.register("", false, "idle", true)
	var paired7 := p7.accept_speech("t9", 1)
	_check("⑦ 占位吃掉了它自己那条 speech（不出声、也不显示）",
			paired7.get("id") == tomb.get("id") and _shown.is_empty())
	p7.finish(tomb)
	_check("⑦ 占位不会漏到后面（配对表干净）", p7.pair_count() == 0)
	var idle := p7.register("在忙什么呀", false, "idle")
	p7.arm(idle, true)
	p7.accept_speech("t10", 1)
	p7.forget_idle()
	_check("⑦ 走神搭话作废：文字不再显示", _shown.is_empty() and not p7.has_pending())
	_check("⑦ 已配对的走神搭话记录出表（音频由调用方 cancel_kind 停播）", p7.pair_count() == 0)
	var idle2 := p7.register("在忙什么呀2", false, "idle")
	p7.arm(idle2, true)
	p7.forget_idle()
	_check("⑦ 还没配对的走神搭话留成占位（吃掉它自己那条 speech）",
			p7.is_tomb(idle2) and not p7.has_pending())

	if _fail == 0:
		print("[pair] PASS：", _total, " 项断言全部通过")
	else:
		print("[pair] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
