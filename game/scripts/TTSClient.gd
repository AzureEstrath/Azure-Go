class_name TTSClient
extends Node
## 本地 TTS 客户端：请求本机自建的语音服务（OpenAI 兼容 /v1/audio/speech，推荐 CosyVoice 3），
## 只做「文本 → 音频字节 → 播放」。模型推理在本地服务进程里跑（可用 GPU），游戏侧几乎零开销；
## 全程 localhost，不联网。

signal status(text: String)
signal auto_fallback(kind: String)     # 本地服务连不上时临时降级（不写配置）
signal speaking_changed(on: bool)      # 开始/结束朗读（化身说话时歪头用）
signal utterance_started               # 每段音频开始播放（逐句模式下用它显示对应的一条文字）

const QUEUE_MAX := 3
const FAIL_COOLDOWN := 6.0
const MAX_CHARS := 300
const FILLER_DIR := "res://assets/voice"   # 预制情绪语气词：填补合成等待的空白
const FILLER_COOLDOWN_MS := 4000           # 自动插话的最小间隔（避免一直「让我看看」）
var slow_chunk_chars := 100          # 慢引擎单次请求的字数上限（右侧栏「长句合并」开关：关=100/开=160）
const HTTP_TIMEOUT_FAST := 60.0      # 轻量引擎（melo）/ 系统语音：很快就回
const HTTP_TIMEOUT_SLOW := 240.0     # CosyVoice 系列 CPU 很慢：给足时间，别像以前那样 60 秒就被丢弃
## 讲话时要丢掉的装饰：emoji、箭头/符号、半角假名颜文字等
const _DROP_RE := "[\\x{1F000}-\\x{1FAFF}\\x{2190}-\\x{2BFF}\\x{FE00}-\\x{FE0F}\\x{2600}-\\x{27BF}\\x{FF61}-\\x{FF9F}\\x{30FB}\\x{FF65}\\x{2000}-\\x{200F}\\x{0370}-\\x{03FF}]"

## 朗读方式：system=Windows 系统语音（零配置可用）；openai=CosyVoice 等服务(OpenAI 兼容)；
## cosyvoice=CosyVoice 官方 fastapi（/inference_sft 表单接口）
var mode := "system"
var base_url := "http://127.0.0.1:9880/v1"
var model := "cosyvoice3"
var api_key := ""            # 云端 OpenAI 兼容 TTS：非空则发送 Authorization: Bearer <key>
var voice := "azure"
var speed := 1.0
var pitch := 1.0              # 播放层音调：直接改播放速率（会略带动语速）
var volume := 1.0
var enabled := false

var _sapi_pid := -1          # 系统语音的子进程
var _last_url := ""
var _speaking := false
var _prefer_mode := ""       # 临时降级前的引擎模式（本地服务就绪后自动切回）
var _recover_at := 0         # 下次探测服务可用性的时刻（ms）
var _probing := false        # 在途请求是否为「恢复探测」
var _http_busy := false      # Godot 的 HTTPRequest 同时只能有一个请求，单独跟踪避免 ERR_BUSY
var _http_since := 0

var _http: HTTPRequest = null
var _player: AudioStreamPlayer = null
var _queue: Array[String] = []
var _busy := false
var _cooldown := 0.0
var _base_ready := false
var _next_stream: AudioStream = null   # 边播边合成的下一句（预取），消除句间空白/卡住
var fast_voices := {}                  # 服务端标记为轻量的音色 id（sherpa/melo）：不需要按句拆分
var fillers: Dictionary = {}           # kind → Array[AudioStream]（surprise / happy / puzzle / think）
var _filler_player: AudioStreamPlayer = null
var _filler_last_ms := 0               # 上次插语气词的时刻（冷却用）

func _ready() -> void:
	_http = HTTPRequest.new()
	_http.timeout = 60.0
	add_child(_http)
	_http.request_completed.connect(_on_done)
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
	"think": ["filler_think_1.wav"],
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
## force=true 只跳过冷却（用于跟表情绑定的即时反应）
func play_filler(kind: String, force := false) -> void:
	if not enabled or _filler_player == null or not fillers.has(kind):
		return
	if _speaking or _next_stream != null or _filler_player.playing:
		return
	var now := Time.get_ticks_msec()
	if not force and now - _filler_last_ms < FILLER_COOLDOWN_MS:
		return
	_filler_last_ms = now
	var arr: Array = fillers[kind]
	_filler_player.stream = arr[randi() % arr.size()]
	_filler_player.play()

## 一句话播完：无缝接上预取的下一句；没有预取时才算整段读完
func _on_spoken() -> void:
	if _next_stream != null:
		var s := _next_stream
		_next_stream = null
		_play_stream(s)                             # 下一句已合成好：立刻接着读，不留空白
		_pump()                                     # 顺便继续预取再下一句
		return
	_set_speaking(false)
	status.emit("朗读完成")
	_pump()

## 退出时别把正在朗读的系统语音进程留成孤儿
func _exit_tree() -> void:
	if _sapi_pid > 0:
		OS.kill(_sapi_pid)          # 无条件杀，is_process_running 有时已追不到子进程
	_sapi_pid = -1

func _process(delta: float) -> void:
	if _http_busy and Time.get_ticks_msec() - _http_since > int((_http.timeout + 20.0) * 1000.0):
		if _http != null:
			_http.cancel_request()               # 卡死保护：3 分钟没回包就取消
		_http_busy = false
		_busy = false
		_cooldown = FAIL_COOLDOWN
		_set_speaking(false)
		status.emit("TTS 服务超时，已取消本次朗读")
	if _cooldown > 0.0:
		_cooldown -= delta
		if _cooldown <= 0.0 and not _queue.is_empty():
			_pump()                              # 冷却结束后继续读队列里剩下的句子
	elif mode == "system" and _sapi_pid > 0 and not OS.is_process_running(_sapi_pid):
		_sapi_pid = -1
		_on_spoken()                             # 系统语音：本句读完，接着读下一句
	if _prefer_mode != "" and not _http_busy and not _busy and Time.get_ticks_msec() >= _recover_at:
		_probe_service()                         # 临时降级中：定期看看本地服务好了没
	# 合成等待超过 1.6 秒又没在出声：插一句预制语气词，别让等待显得像卡住
	if _http_busy and not _speaking and _next_stream == null and Time.get_ticks_msec() - _http_since > 1600:
		play_filler("think")

## 用户手动改了设置：取消「自动切回」状态
func clear_fallback() -> void:
	_prefer_mode = ""

## 探测本地服务是否就绪；就绪则从系统语音兜底切回原引擎
func _probe_service() -> void:
	var prefer := _prefer_mode if _prefer_mode != "" else mode
	var url := ""
	if prefer == "cosyvoice":
		var b := base_url.rstrip("/")
		if b.ends_with("/v1"):
			b = b.substr(0, b.length() - 3)
		url = b + "/health"
	else:
		# OpenAI 兼容服务（含云端）：探标准 /models；只要服务在线就会有 HTTP 响应
		url = base_url.strip_edges().rstrip("/") + "/models"
	if url.contains("://localhost"):
		url = url.replace("://localhost", "://127.0.0.1")
	_http.timeout = 4.0
	_probing = true
	_http_busy = true
	_http_since = Time.get_ticks_msec()
	if _http.request(url) != OK:
		_probing = false
		_http_busy = false
		_recover_at = Time.get_ticks_msec() + 15000

## 排队朗读一句（Azure 的话）。正在朗读时最多再排 3 句，避免越说越滞后
func speak(text: String) -> void:
	if not enabled:
		return
	var t := clean_for_speech(text)
	if t == "":
		return
	# 慢引擎：把整段按「整句」合并成较大的块（短回复通常只有一块 → 一次请求连续读完）
	var pieces: Array[String] = []
	if _slow_engine():
		pieces = _chunk_for_slow(t)
	else:
		pieces.append(t)
	var cap := QUEUE_MAX if mode == "system" else (4 if _slow_engine() else 3)
	if pieces.size() > cap:                          # 太长的消息只念前几句
		pieces.resize(cap)
	while _queue.size() > cap - pieces.size():       # 队列满了丢最旧的，保证不越说越滞后
		_queue.pop_front()
	_queue.append_array(pieces)
	_pump()

## 是否还在忙（合成中/排队中/正在播放）。「同步说话」用它判断文字该等语音，还是直接显示
func is_speaking() -> bool:
	return _speaking or _busy or _http_busy or _next_stream != null or not _queue.is_empty()

func stop() -> void:
	_queue.clear()
	_next_stream = null
	if _player != null and _player.playing:
		_player.stop()
	if _http_busy and _http != null:
		_http.cancel_request()          # 取消在途请求，否则下一次 request 会报 Busy
	_http_busy = false
	if _sapi_pid > 0:
		OS.kill(_sapi_pid)
	_sapi_pid = -1
	_busy = false
	_set_speaking(false)

## 试听（忽略队列与开关，直接读一句）
func preview(text: String) -> void:
	var t := clean_for_speech(text)
	if t == "":
		t = "嗯~ 我是 Azure，很高兴见到你。"
	stop()
	_queue.push_front(t)
	_pump(true)

## 文本 → 适合朗读的句子：去装饰/表情/括号标题，合并空白，限长
func clean_for_speech(text: String) -> String:
	var re := RegEx.new()
	var s := text.replace("**", "").replace("📊", "").replace("📋", "")
	if re.compile(_DROP_RE) == OK:
		s = re.sub(s, "", true)
	s = s.replace("【", "，").replace("】", "，")
	s = s.replace("()", "").replace("（）", "")          # 颜文字被清空后剩下的空括号
	s = " ".join(s.split("\n", false))
	while s.contains("  "):
		s = s.replace("  ", " ")
	s = s.strip_edges()
	if s.length() > MAX_CHARS:
		s = s.substr(0, MAX_CHARS) + "…"
	return s

## 长句拆成小句；短句整体保留（避免把「嗯」这种碎成单句）
static func _split_sentences(text: String) -> Array[String]:
	var out: Array[String] = []
	if text.length() <= 18:
		out.append(text)
		return out
	var cur := ""
	for i in text.length():
		var ch := text[i]
		cur += ch
		if ch in ["。", "！", "？", "；", "…", "!", "?", ";", ".", "\n"]:
			var s := cur.strip_edges()
			if s != "":
				out.append(s)
			cur = ""
	var tail := cur.strip_edges()
	if tail != "":
		out.append(tail)
	# 纯标点的碎片（如「...」被拆出来的「.」）并回前一句，别当成独立句子发出去
	var merged: Array[String] = []
	for p in out:
		var has_word := false
		for i in p.length():
			var c := p[i]
			if not (c in ["。", "！", "？", "；", "…", "!", "?", ";", ".", "\n", "，", ",", "、", "~", " "]):
				has_word = true
				break
		if has_word:
			merged.append(p)
		elif not merged.is_empty():
			merged[merged.size() - 1] += p
	if merged.is_empty():
		merged.append(text)
	return merged

## 慢引擎分块：按整句合并，每块不超过 slow_chunk_chars 字。
## 关键：不把一句话从标点中间切开——短消息会合成「一整块」，一次请求就连续读完整句。
func _chunk_for_slow(text: String) -> Array[String]:
	var sents := _split_sentences(text)
	var out: Array[String] = []
	var cur := ""
	for s in sents:
		if cur == "":
			cur = s
		elif cur.length() + s.length() <= slow_chunk_chars:
			cur += s
		else:
			out.append(cur)
			cur = s
	if cur != "":
		out.append(cur)
	if out.is_empty():
		out.append(text)
	return out

## 是否为慢速神经网络引擎（CosyVoice 克隆/预设；melo 与系统语音不算）
func _slow_engine() -> bool:
	if mode == "system":
		return false
	var v := voice.strip_edges().to_lower()
	if v.ends_with("-fast"):
		return false                       # 快速克隆版（melo + 音色转换）很快
	if fast_voices.get(v, false):
		return false                       # 服务端标记为轻量（engine=sherpa，如小雅/少女音）：不必拆句
	return v != "melo" and v != "sherpa" and v != "zh_en" and not v.begins_with("melo")

# ================= 内部 =================

func _set_speaking(v: bool) -> void:
	if v == _speaking:
		return
	_speaking = v
	speaking_changed.emit(v)

func _pump(force := false) -> void:
	if _busy or _http_busy or _queue.is_empty():
		return
	if not force and (not enabled or _cooldown > 0.0):
		return
	if mode == "system":
		if _sapi_pid > 0 and OS.is_process_running(_sapi_pid):
			return                               # 上一句还在读
		_speak_system(_queue.pop_front())
		return
	if _next_stream != null:
		return                                   # 已预取下一句：等它播完再合成，避免堆积
	_ensure_base()
	_http.timeout = HTTP_TIMEOUT_SLOW if _slow_engine() else HTTP_TIMEOUT_FAST
	var text: String = _queue.pop_front()
	_busy = true
	var headers: PackedStringArray
	var payload := ""
	if mode == "cosyvoice":                      # CosyVoice 官方 fastapi：表单接口
		headers = _with_auth(PackedStringArray(["Content-Type: application/x-www-form-urlencoded"]))
		payload = "tts_text=%s&spk_id=%s" % [text.uri_encode(), voice.uri_encode()]
	else:                                        # OpenAI 兼容 /v1/audio/speech
		headers = _with_auth(PackedStringArray(["Content-Type: application/json"]))
		payload = JSON.stringify({
			"model": model, "input": text, "voice": voice,
			"speed": speed, "response_format": "mp3",
		})
	_last_url = _endpoint()
	_http_busy = true
	_http_since = Time.get_ticks_msec()
	var err := _http.request(_last_url, headers, HTTPClient.METHOD_POST, payload)
	if err != OK:
		_busy = false
		_http_busy = false
		_cooldown = FAIL_COOLDOWN
		print("[TTS] 请求无法发出：%s  URL=%s" % [error_string(err), _last_url])
		status.emit("TTS 请求发送失败（%s）：%s" % [error_string(err), _last_url])
	elif _slow_engine():
		# 慢引擎（CosyVoice）：让用户知道在合成、大概要等多久，消除「文字出了但没声音」的割裂感
		status.emit("Azure 正在开口…（本句合成中，约 %d 秒）" % int(ceil(text.length() * 1.2)))

func _endpoint() -> String:
	var b := base_url.rstrip("/")
	return b + ("/inference_sft" if mode == "cosyvoice" else "/audio/speech")

## 本地服务无需鉴权；填了 api_key（云端 OpenAI 兼容 TTS）则带 Bearer
func _with_auth(h: PackedStringArray) -> PackedStringArray:
	var key := api_key.strip_edges()
	if key != "":
		h.append("Authorization: Bearer " + key)
	return h

## Windows 系统语音（零配置兜底）：用 PowerShell 调 SAPI 朗读，子进程异步跑
func _speak_system(text: String) -> void:
	var s := text.replace("'", "''").replace("\"", "")
	var rate := int(round(clampf((speed - 1.0) * 5.0, -8.0, 8.0)))
	var vol := int(round(clampf(volume * 67.0, 5.0, 100.0)))
	var script := "$ErrorActionPreference='SilentlyContinue'; Add-Type -AssemblyName System.Speech; " \
		+ "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; " \
		+ "$s.Rate=%d; $s.Volume=%d; " % [rate, vol]
	if voice.strip_edges() != "":
		script += "try { $s.SelectVoice('%s') } catch {}; " % voice.replace("'", "''")
	var pitch_pct := int(round(clampf((pitch - 1.0) * 100.0, -30.0, 40.0)))
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
		status.emit("系统语音不可用（无法启动 PowerShell）")
	else:
		_set_speaking(true)
		utterance_started.emit()
		status.emit("正在朗读…（系统语音）")

## Godot 只把 localhost 解析为 IPv6，本地服务多监听 IPv4，统一改写成 127.0.0.1
func _ensure_base() -> void:
	if _base_ready:
		return
	_base_ready = true
	if base_url.contains("://localhost"):
		base_url = base_url.replace("://localhost", "://127.0.0.1")

func _on_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_busy = false
	_http_busy = false
	if _probing:                                     # 恢复探测的结果
		_probing = false
		# 服务在线即视为恢复：200 最佳；云端 TTS 的 /models 可能返回 401/404，只要 <500 就说明链路已通
		var back := result == HTTPRequest.RESULT_SUCCESS and code > 0 and code < 500
		if back and _prefer_mode != "":
			mode = _prefer_mode
			_prefer_mode = ""
			status.emit("语音服务已恢复，自动切回「%s」朗读" % mode)
			_pump()
		else:
			_recover_at = Time.get_ticks_msec() + 15000
		return
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		var conn_failed := result == HTTPRequest.RESULT_CANT_CONNECT or result == HTTPRequest.RESULT_CONNECTION_ERROR \
			or result == HTTPRequest.RESULT_CANT_RESOLVE
		if conn_failed and mode != "system":
			# 本地服务没启动：临时改走系统语音，保证「有声音」，但不写回配置
			_cooldown = 0.0
			_prefer_mode = mode                       # 记住原引擎，服务就绪后自动切回
			_recover_at = Time.get_ticks_msec() + 8000
			mode = "system"
			auto_fallback.emit("system")
			_pump()
			return
		_cooldown = FAIL_COOLDOWN
		_set_speaking(false)
		var head := body.slice(0, mini(body.size(), 160)).get_string_from_utf8().replace("\n", " ").strip_edges()
		var why := ""
		if result == HTTPRequest.RESULT_TIMEOUT:
			why = "合成超过 %d 秒仍未完成" % int(_http.timeout)
		elif result != HTTPRequest.RESULT_SUCCESS:
			why = error_string(result)
		else:
			why = "HTTP %d" % code
		var msg := "TTS 无响应（%s）" % why
		if head != "":
			msg += " · " + head
		print("[TTS] %s  URL=%s" % [msg, _last_url])
		status.emit(msg)
		_pump()
		return
	var stream := _make_stream(body)
	if stream == null:
		var sig := body.slice(0, mini(body.size(), 12))
		print("[TTS] 音频无法解析：%d 字节，头部=%s  URL=%s" % [body.size(), sig, _last_url])
		status.emit("TTS 返回的音频无法解析（%d 字节）" % body.size())
		_pump()
		return
	if _player.playing:                              # 上一句还在播：先缓存，播完无缝接上
		_next_stream = stream
		_pump()                                      # 继续预取再下一句
		return
	_play_stream(stream)
	_pump()                                          # 边播边合成下一句，消除句间停顿

## 播放一段音频（统一入口：音量/音调/状态）
func _play_stream(stream: AudioStream) -> void:
	if _filler_player != null and _filler_player.playing:
		_filler_player.stop()            # 正式朗读开始：语气词让位
	_player.stream = stream
	_player.volume_db = linear_to_db(clampf(volume, 0.01, 2.0))
	_player.pitch_scale = clampf(pitch, 0.5, 2.0)
	_player.play()
	_set_speaking(true)
	utterance_started.emit()
	status.emit("正在朗读…")

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