class_name TTSClient
extends Node
## 语音**播放器**（计划 05）：合成、切句、清洗全在后端，前端只按 `speech` 指令出声。
##
## 两个入口（都由 Main 接到 `speech` 事件后调用，各带回该条语音的 `seq`）：
##   `play_audio(bytes, seq)`  —— 播放 `GET /api/tts/audio/{id}` 取回的音频（WAV/MP3）
##   `speak_system(text, seq)` —— 让 Windows 系统语音念后端给的**清洗过的**文本（合成不可用时的降级指令）
## 两者都返回**分组号 group**：一条台词的语音只发一次 `utterance_started(group)`（`seq` 用于配对校验与日志）。
##
## 这里**不再**做：文本清洗、按句分块、任何合成请求、本地服务探测、自动降级
## （降级现在由后端直接发 `mode="system"` 的 `speech` 指令表达）。
## 音画同步仍在 Main：`ai_move` 那条语音开始播放（`utterance_started`）时它才去 `POST /api/reveal`。

signal status(text: String)
signal speaking_changed(on: bool)      # 开始/结束朗读（化身说话时歪头用）
signal utterance_started(group: int)   # 一条台词的语音开始播放（group=play_audio/speak_system 的返回值；0=试听）
signal speech_skipped(group: int)      # 这条语音不会再响（音频坏了 / 队列溢出 / 被停）：文字照常显示，只是没声音

const QUEUE_MAX := 3
const FAIL_COOLDOWN := 6.0             # 系统语音起不来之后缓一缓，别每帧重试
const FILLER_FORCE_GAP_MS := 4000      # 任意两个语气词之间的最小间隔（force 也不能贴脸连播，防多路叠着响）
const FILLER_THINK_GAP_MS := 1500      # 上一声是「思考声」时：情绪声可以更快接上（情绪优先）
const FILLER_DIR := "res://assets/voice"   # 预制情绪语气词：填补等语音的空白
const FILLER_COOLDOWN_MS := 9000           # 自动插话的最小间隔（避免一直「嗯……」）
const THINK_FILLER_PER_TURN := 1           # 每次玩家互动后 think 语气词上限（一回合最多「嗯……」一次，别反复）

## 朗读方式：只是**本地镜像**（用户在下拉框选的意图），用来判断「要不要缩短导演层犹豫节拍」；
## 真正走哪条路由后端按其配置决定（`speech` 事件里的 `mode`）。
var mode := "openai"
var voice := "中文女"
var speed := 1.0
var pitch := 1.0              # 播放层音调：直接改播放速率（会略带动语速）
var mood_pitch := 0.0         # 情绪区域的瞬时音高偏移（叠加在 pitch 上、不写回配置；±0.1 以内）
var volume := 1.0
var enabled := false

var _sapi_pid := -1          # 系统语音的子进程
var _speaking := false
var _cooldown := 0.0
var _group_seq := 0          # 语音分组号：每次 play_audio/speak_system 自增（一条台词一个）
var _play_group := 0         # 正在播的那条的分组号（0 = 试听/无分组）；停掉时用它补一条 speech_skipped
var _play_kind := ""         # 正在播放的声音类型（""=对局发言；"idle"=走神搭话，可被取消）

var _player: AudioStreamPlayer = null
var _queue: Array[Dictionary] = []      # 朗读队列：{stream|text, group, seq, start, kind, system}
var fillers: Dictionary = {}            # kind → Array[AudioStream]（surprise / happy / puzzle / think / pat）
var allow_fillers := true               # 是否允许自动插语气词：只在对局互动中允许，闲置搭话/开场问候不插（Main 控制）
var _filler_player: AudioStreamPlayer = null
var _filler_last_ms := 0               # 上次插语气词的时刻（冷却用）
var _filler_last_kind := ""            # 上次语气词的类型（情绪声可优先接在「思考声」后、并可打断它）
var _filler_last_idx := {}             # kind → 上次播的序号（避免同一句连着播）
var think_filler_budget := 0           # 本回合剩余可自动插的 think 语气词次数（Main 在玩家互动时重置）

func _ready() -> void:
	_player = AudioStreamPlayer.new()
	add_child(_player)
	_player.finished.connect(_on_spoken)
	_filler_player = AudioStreamPlayer.new()      # 语气词单独一个播放器：不干扰正式朗读
	_filler_player.volume_db = -7.0
	add_child(_filler_player)
	_load_fillers()

## 预制语气词清单（显式列出：导出后 res:// 里只剩 .import，靠枚举会拿不到文件）
const FILLER_FILES := {
	"surprise": ["filler_surprise_1.wav"],
	"happy": ["filler_happy_1.wav", "filler_happy_2.wav"],
	"puzzle": ["filler_puzzle_1.wav", "filler_puzzle_2.wav"],
	"think": ["filler_think_1.wav", "filler_think_2.wav"],
	"pat": ["filler_pat_1.wav", "filler_pat_2.wav", "filler_pat_3.wav", "filler_pat_4.wav"],
	"sigh": ["filler_sigh_1.wav", "filler_sigh_2.wav"],          # 叹气/可惜：唉……、哎呀
	"cheer": ["filler_cheer_1.wav", "filler_cheer_2.wav"],       # 惊叹/赞叹：哇！、好哎~
	"proud": ["filler_proud_1.wav", "filler_proud_2.wav"],       # 小得意/撒娇：哼哼~、诶嘿
	"hmph": ["filler_hmph_1.wav", "filler_hmph_2.wav"],          # 生气/得意的哼：情绪区域爆发时用
}

## 载入预制语气词：res://assets/voice/ 下按清单加载
func _load_fillers() -> void:
	for kind in FILLER_FILES.keys():
		var list: Array = []
		for name in FILLER_FILES[kind]:
			var st := load(FILLER_DIR.path_join(str(name)))
			if st is AudioStream:
				list.append(st)
			else:
				push_warning("[TTS] 语气词缺失：%s" % name)
		if not list.is_empty():
			fillers[kind] = list
	print("[TTS] 语气词库：%s" % str(fillers.keys()))

## 播一句预制语气词填补等待空白；正在正式朗读/已在插话时让位。
## force=true 跳过冷却与 allow_fillers 开关（用于跟表情绑定的即时反应）
func play_filler(kind: String, force := false) -> void:
	if not enabled or _filler_player == null or not fillers.has(kind):
		return
	if not force and not allow_fillers:
		return
	if not force and kind == "think" and think_filler_budget <= 0:
		return                                       # 本回合的 think 语气词额度用完了（「让我看看…有一次就行」）
	if is_speaking():
		return                                       # 她正在正式朗读（或还有语音排着）：语气词一律让位
	var now := Time.get_ticks_msec()
	var can_cut_think := force and _filler_player.playing and _filler_last_kind == "think"
	if _filler_player.playing and not can_cut_think:
		return                                       # 已在插话中：别叠着响
	if can_cut_think:
		_filler_player.stop()                        # 情绪声优先：打断还在播的「嗯……」
	else:
		var gap := FILLER_FORCE_GAP_MS if force else FILLER_COOLDOWN_MS
		if force and _filler_last_kind == "think":
			gap = FILLER_THINK_GAP_MS                # 上一声是思考声：情绪声可以更快接上
		if now - _filler_last_ms < gap:
			return
	if not force and kind == "think":
		think_filler_budget -= 1
	_filler_last_ms = now
	_filler_last_kind = kind
	var arr: Array = fillers[kind]
	var idx := randi() % arr.size()
	if arr.size() > 1 and int(_filler_last_idx.get(kind, -1)) == idx:
		idx = (idx + 1) % arr.size()                 # 不连着播同一句（「嗯~」「让我看看」轮换）
	_filler_last_idx[kind] = idx
	_filler_player.stream = arr[idx]
	_filler_player.play()

## 玩家发起一次互动（落子 / 提问 / 功能按钮）：重置本回合的 think 语气词额度
func begin_filler_turn() -> void:
	think_filler_budget = THINK_FILLER_PER_TURN

## 三维情绪区域 → 音高微调（由 Main 在 mood_changed 时下发；只影响本句听感，不改用户配置）
func set_mood_pitch(p: float) -> void:
	mood_pitch = clampf(p, -0.15, 0.15)

## 播放预制欢迎语音（assets/voice/welcome_<名字>.wav）：开场问候零等待，不必等合成。
## 有对应文件才播并返回 true（调用方据此把文字直接显示）；否则返回 false，走普通流程。
func play_welcome(name: String) -> bool:
	if not enabled or _player == null:
		return false
	var key := name.strip_edges().to_lower()
	if key == "":
		return false
	var res := FILLER_DIR.path_join("welcome_%s.wav" % key)
	if not ResourceLoader.exists(res):
		push_warning("[TTS] 预制欢迎语音缺失：%s（退回普通流程）" % res)
		return false
	var st := load(res)
	if not (st is AudioStream):
		push_warning("[TTS] 预制欢迎语音无法加载：%s" % res)
		return false
	stop()                                        # 清掉可能还排着的旧语音，欢迎语先出声
	_play_stream(st, 0, false, "")
	print("[TTS] 播放预制欢迎语音：%s" % res)
	return true

# ================= 外部入口（由 Main 的 _on_speech_event 调用） =================

## 播放一段后端取回的音频。`seq` 是这条语音在它所属回合内的序号（配对校验 / 日志用）。
## 解析不了（既不是可用的 WAV 也不是 MP3）时**不静默卡住**：按「这条语音不会再响」处理——
## 补发 `speech_skipped(group)` 让调用方把文字照常显示，并在状态栏给一条可读提示。
## `kind` 沿用既有语义（""=对局发言；"idle"=走神搭话），让 `cancel_kind("idle")` 仍能整类作废。
func play_audio(bytes: PackedByteArray, seq: int, kind := "") -> int:
	_group_seq += 1
	var g := _group_seq
	var stream := _make_stream(bytes)
	if stream == null:
		print("[TTS] 音频无法解析：%d 字节（seq=%d）" % [bytes.size(), seq])
		status.emit("这段语音的音频无法解析（%d 字节），改为只显示文字" % bytes.size())
		speech_skipped.emit.call_deferred(g)
		return g
	_queue.append({"stream": stream, "group": g, "seq": seq, "start": true,
			"kind": kind, "system": false})
	_enforce_cap()
	_pump()
	return g

## 交给 Windows 系统语音朗读一段（后端给的是清洗后的文本，这里**不再做任何清洗**）。
## 文本为空 / PowerShell 起不来时同样补 `speech_skipped`，让文字照常显示。
func speak_system(text: String, seq: int, kind := "") -> int:
	_group_seq += 1
	var g := _group_seq
	if text.strip_edges() == "":
		status.emit("这条语音没有可念的文本，改为只显示文字")
		speech_skipped.emit.call_deferred(g)
		return g
	_queue.append({"text": text, "group": g, "seq": seq, "start": true,
			"kind": kind, "system": true})
	_enforce_cap()
	_pump()
	return g

## 队列上限（沿用既有口径：系统语音最紧，慢引擎多留一块）——说话不能越排越滞后。
func _queue_cap() -> int:
	return QUEUE_MAX if mode == "system" else (4 if is_slow_engine() else 3)

## 队列满了丢**最旧**的：丢的那条不会再响，补发 speech_skipped 让它的文字照常显示。
func _enforce_cap() -> void:
	var cap := _queue_cap()
	while _queue.size() > cap:
		var old: Dictionary = _queue.pop_front()
		if bool(old.get("start", false)):
			speech_skipped.emit.call_deferred(int(old["group"]))

## 是否还有语音没播完（播放中 / 排着队）。「同步说话」与棋盘锁用它判断她是不是还在说。
## 注意：合成在途（后端正在合成、音频还没到前端）不在这个口径里 —— Main 另行统计配对表。
func is_speaking() -> bool:
	return _speaking or not _queue.is_empty()

## 停掉所有朗读（文字不会跟着消失：排着队的首块 + 正在播的那条都补发 speech_skipped）
func stop() -> void:
	for it in _queue:
		if bool(it.get("start", false)):
			speech_skipped.emit.call_deferred(int(it["group"]))
	_queue.clear()
	if _play_group > 0:                      # 正在播的那条被停掉了：调用方据此收账（文字照常显示）
		speech_skipped.emit.call_deferred(_play_group)
		_play_group = 0
	if _player != null and _player.playing:
		_player.stop()
	if _sapi_pid > 0:
		OS.kill(_sapi_pid)
	_sapi_pid = -1
	_play_kind = ""
	_set_speaking(false)

## 取消某一类（如 "idle"=走神搭话）尚未说完的语音：清队列、停当前播放。返回是否取消到了东西。
func cancel_kind(kind: String) -> bool:
	if kind == "":
		return false
	var hit := false
	var kept: Array[Dictionary] = []
	for it in _queue:
		if str(it.get("kind", "")) == kind:
			hit = true
		else:
			kept.append(it)
	_queue = kept
	if _play_kind == kind:                            # 正在播：停掉（stop() 不触发 finished，手动收尾）
		if _sapi_pid > 0:
			OS.kill(_sapi_pid)
			_sapi_pid = -1
		if _player != null and _player.playing:
			_player.stop()
		_play_kind = ""
		_set_speaking(false)
		hit = true
	if hit:
		_pump()                                       # 若队列里还有别的（玩家互动的新回复），立刻接着来
	return hit

## 是否为慢速神经网络引擎（自建 CosyVoice 的克隆/预设音色；melo 与系统语音不算）。
## 合成已归后端，这里只剩「按音色估计引擎快慢」这一条纯函数语义：
## 导演层用它判断犹豫节拍时长（慢引擎缩短犹豫，避免雪上加霜）。
func is_slow_engine() -> bool:
	if mode == "system":
		return false
	var v := voice.strip_edges().to_lower()
	if v.ends_with("-fast"):
		return false                       # 快速克隆版（melo + 音色转换）很快
	return v != "melo" and v != "sherpa" and v != "zh_en" and not v.begins_with("melo")

# ================= 内部 =================

func _set_speaking(v: bool) -> void:
	if v == _speaking:
		return
	_speaking = v
	speaking_changed.emit(v)

## 一句话播完：接着读队列里的下一句
func _on_spoken() -> void:
	_play_kind = ""
	_play_group = 0
	_set_speaking(false)
	status.emit("朗读完成")
	_pump()

## 退出时别把正在朗读的系统语音进程留成孤儿
func _exit_tree() -> void:
	if _sapi_pid > 0:
		OS.kill(_sapi_pid)          # 无条件杀，is_process_running 有时已追不到子进程
	_sapi_pid = -1

func _process(delta: float) -> void:
	if _cooldown > 0.0:
		_cooldown -= delta
		if _cooldown <= 0.0 and not _queue.is_empty():
			_pump()                              # 冷却结束后继续读队列里剩下的
	elif _sapi_pid > 0 and not OS.is_process_running(_sapi_pid):
		_sapi_pid = -1
		_on_spoken()                             # 系统语音：本句读完，接着读下一句

## 出队一条并开播（播放中 / 队列空 / 冷却中都不动）
func _pump() -> void:
	if _speaking or _queue.is_empty() or _cooldown > 0.0:
		return
	var item: Dictionary = _queue.pop_front()
	if bool(item.get("system", false)):
		_speak_system(str(item.get("text", "")), int(item["group"]),
				bool(item.get("start", true)), str(item.get("kind", "")))
		return
	_play_stream(item["stream"], int(item["group"]), bool(item.get("start", true)),
			str(item.get("kind", "")))

## 播放一段音频（统一入口：音量/音调/状态）
func _play_stream(stream: AudioStream, group: int, start: bool, kind := "") -> void:
	if _filler_player != null and _filler_player.playing:
		_filler_player.stop()            # 正式朗读开始：语气词让位
	_player.stream = stream
	_player.volume_db = linear_to_db(clampf(volume, 0.01, 2.0))
	_player.pitch_scale = clampf(pitch + mood_pitch, 0.5, 2.0)
	_player.play()
	_play_kind = kind
	_play_group = group if start else 0
	_set_speaking(true)
	if start:
		# 延到本帧末再报「这条开始播了」：调用方（Main）先把分组号记进配对表
		utterance_started.emit.call_deferred(group)
	status.emit("正在朗读…")

## Windows 系统语音（零配置兜底）：用 PowerShell 调 SAPI 朗读，子进程异步跑。
## 文本由后端清洗好传来，这里**不再清洗**（语气标记在后端就剥干净了）。
func _speak_system(text: String, group: int, start: bool, kind := "") -> void:
	var s := text.replace("'", "''").replace("\"", "")
	var rate := int(round(clampf((speed - 1.0) * 5.0, -8.0, 8.0)))
	var vol := int(round(clampf(volume * 67.0, 5.0, 100.0)))
	var script := "$ErrorActionPreference='SilentlyContinue'; Add-Type -AssemblyName System.Speech; " \
		+ "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; " \
		+ "$s.Rate=%d; $s.Volume=%d; " % [rate, vol]
	if voice.strip_edges() != "":
		script += "try { $s.SelectVoice('%s') } catch {}; " % voice.replace("'", "''")
	var pitch_pct := int(round(clampf((pitch + mood_pitch - 1.0) * 100.0, -30.0, 40.0)))
	if pitch_pct == 0:
		script += "$s.Speak('%s')" % s
	else:
		# SAPI 无独立音高参数：用 SSML prosody 近似，个别引擎不支持时回退普通朗读
		var xml := text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
		var ssml := ("<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
			+ "<prosody pitch='%+d%%'>" % pitch_pct) + xml + "</prosody></speak>"
		script += "try { $s.SpeakSsml('%s') } catch { $s.Speak('%s') }" % [ssml.replace("'", "''"), s]
	_sapi_pid = OS.create_process("powershell",
		PackedStringArray(["-NoProfile", "-WindowStyle", "Hidden", "-Command", script]))
	if _sapi_pid <= 0:
		_sapi_pid = -1
		_cooldown = FAIL_COOLDOWN                # 失败后缓一缓，避免每帧重试
		_set_speaking(false)
		status.emit("系统语音不可用（无法启动 PowerShell），这条改为只显示文字")
		if start:
			speech_skipped.emit.call_deferred(group)
	else:
		_play_kind = kind
		_play_group = group if start else 0
		_set_speaking(true)
		if start:
			utterance_started.emit.call_deferred(group)   # 延到本帧末：调用方先把这条的分组号记好
		status.emit("正在朗读…（系统语音）")

## mp3（默认）或 16bit PCM wav（服务端可配）
func _make_stream(b: PackedByteArray) -> AudioStream:
	if b.size() < 12:
		return null
	if b.slice(0, 4).get_string_from_ascii() == "RIFF":
		return _wav_stream(b)
	var mp3 := AudioStreamMP3.new()
	mp3.data = b
	if mp3.get_length() <= 0.0:
		return null
	return mp3

func _wav_stream(b: PackedByteArray) -> AudioStream:
	var pos := 12
	var ch := 1
	var rate := 24000
	var pcm_ok := false
	var data := PackedByteArray()
	while pos + 8 <= b.size():
		var id := b.slice(pos, pos + 4).get_string_from_ascii()
		var sz := int(b.decode_u32(pos + 4))
		var body := b.slice(pos + 8, mini(pos + 8 + sz, b.size()))
		if id == "fmt " and body.size() >= 16:
			ch = body.decode_u16(2)
			rate = int(body.decode_u32(4))
			pcm_ok = body.decode_u16(14) == 16
		elif id == "data":
			data = body
		pos += 8 + sz + (sz & 1)
	if not pcm_ok or data.is_empty():
		return null
	var s := AudioStreamWAV.new()
	s.format = AudioStreamWAV.FORMAT_16_BITS
	s.mix_rate = rate
	s.stereo = ch == 2
	s.data = data
	return s
