class_name LLMClient
extends Node
## 本地 LLM（OpenAI 兼容 /v1/chat/completions）调用
## 与网页版一致：剥离思考段落；思考档位只影响「她怎么想」，推理段永不显示给玩家

const REASON_BUDGET_MED := 160   # 中思考：推理段 token 预算（本地实测整条回复 ≈5 秒）
const REASON_STOP_MSG := "思考到此为止，请立即用中文直接给出最终回答，不要再继续分析。"   # 预算用尽时注入：逼模型转入正式回答

var base_url := "http://localhost:8080/v1"
var model := "qwen3.5"
var api_key := ""                # 云端 OpenAI 兼容服务：非空则发送 Authorization: Bearer <key>
var timeout_sec := 120.0
var think_mode := 0              # 思考档位：0=快速（不思考） 1=中等（限定推理预算） 2=强思考（自由推理）

# 对局中看护本地服务：偶发 GPU 故障会把 llama-server 中途打死（实测跑了 6 分多钟后
# ROCm 报 unspecified launch failure 直接退出）。持续不可达就回调重新拉起；
# Main 侧把 relaunch_cb 接到 _ensure_llm_url（启动器自带重试与并发去重，可放心重复调用）
var relaunch_cb: Callable = Callable()

const WATCH_INTERVAL := 15.0     # 看护轮询间隔（秒）
const WATCH_TRIGGER := 3         # 连续 3 次（≈45 秒）不可达才触发重新拉起
const WATCH_COOLDOWN_MS := 120000  # 触发后 2 分钟内不重复触发（留给启动器自愈的时间）

var _http: HTTPRequest = null
var _think_re: RegEx = null
var _url_re: RegEx = null
var _tag_open := ""      # 形如 <think>，运行时拼接以免被外部工具当标记处理
var _tag_close := ""     # 形如 </think>

func _ready() -> void:
	_http = HTTPRequest.new()
	_http.timeout = timeout_sec
	add_child(_http)
	var t := "think"
	_tag_open = "<" + t + ">"
	_tag_close = "</" + t + ">"
	_think_re = RegEx.new()
	_think_re.compile("(?s)" + _tag_open + ".*?" + _tag_close)
	_url_re = RegEx.new()
	_url_re.compile("^(https?)://([^:/]+)(?::(\\d+))?")
	var watch := Timer.new()
	watch.wait_time = WATCH_INTERVAL
	watch.autostart = true
	watch.timeout.connect(_watch_tick)
	add_child(watch)

var _base_ready := false
var _inflight := false           # 串行化排队：闲置搭话等新调用等待前一个完成，而不是并发撞 Busy
var _dns_cache: Dictionary = {}  # host → 解析结果：云端服务省掉每次请求的 DNS 解析（TCP 预检仍保留，服务挂掉能快速失败）
var _think_params_unsupported := false  # 服务端拒绝思考参数（云端 API 会 4xx）：探测一次后不再发送
var _watch_fails := 0            # 看护：连续不可达次数
var _watch_last_ms := -WATCH_COOLDOWN_MS

## 首次使用前把 localhost 规范为 IPv4 回环。
## Godot 在本机把 localhost 只解析到 IPv6 ::1，而本地 LLM 通常只监听 IPv4，
## 这会让每次请求先挂住数十秒才回退（甚至直接失败），因此直接改写为 127.0.0.1
func _ensure_base() -> void:
	if _base_ready:
		return
	_base_ready = true
	if base_url.contains("://localhost"):
		base_url = base_url.replace("://localhost", "://127.0.0.1")
		print("[LLM] localhost → 127.0.0.1（Godot 只把 localhost 解析为 IPv6，直连会卡顿）")

## 主机名 → 候选地址：本地回环固定先 IPv4 再 IPv6；其余主机解析一次后缓存
func _resolve_addrs(host: String) -> Array[String]:
	if host == "127.0.0.1" or host == "localhost" or host == "::1":
		return ["127.0.0.1", "::1"]          # 先 IPv4，再 IPv6
	if _dns_cache.has(host):
		var cached: Array[String] = []
		cached.assign(_dns_cache[host])
		return cached
	var out: Array[String] = []
	for ip in IP.resolve_hostname(host, IP.TYPE_ANY):
		out.append(ip)
	if out.is_empty():
		out.assign([host])
	_dns_cache[host] = out
	return out

## 连接预检：避免本地 LLM 未启动时白等满整个超时（一次要等 2 分钟）
func _reachable() -> bool:
	var m := _url_re.search(base_url)
	if m == null:
		return true
	var host := m.get_string(2)
	var port := int(m.get_string(3)) if m.get_string(3) != "" else (443 if m.get_string(1) == "https" else 80)
	var addrs := _resolve_addrs(host)
	var t0 := Time.get_ticks_msec()
	for a in addrs:
		if await _try_connect(a, port):
			if a == "::1" and not base_url.contains("::1"):
				push_warning("[LLM] 服务只监听 IPv6：请在 config.json 里把地址写成 http://[::1]:端口/v1")
			return true
		if Time.get_ticks_msec() - t0 > 3000:
			break
	return false

func _try_connect(host: String, port: int) -> bool:
	var sp := StreamPeerTCP.new()
	if sp.connect_to_host(host, port) != OK:
		return false
	var t0 := Time.get_ticks_msec()
	while sp.get_status() == StreamPeerTCP.STATUS_CONNECTING and Time.get_ticks_msec() - t0 < 700:
		await get_tree().process_frame
		sp.poll()
	var ok := sp.get_status() == StreamPeerTCP.STATUS_CONNECTED
	sp.disconnect_from_host()
	return ok

## 剥离思考型模型的推理段落；若思考段被截断（未闭合）则视为无有效内容
func clean_content(text: String) -> String:
	if text == "":
		return ""
	var out := _think_re.sub(text, "", true).strip_edges()
	if out.contains(_tag_open):
		return ""
	return out

func _endpoint() -> String:
	return base_url.rstrip("/") + "/chat/completions"

## 请求头：本地服务无需鉴权；填了 api_key 则按 OpenAI 规范带 Bearer
func _headers() -> PackedStringArray:
	var h := PackedStringArray(["Content-Type: application/json"])
	var key := api_key.strip_edges()
	if key != "":
		h.append("Authorization: Bearer " + key)
	return h

## 返回回复文本；失败时返回可读的错误说明
## no_think=true：强制不思考（情绪增量这类小请求用，避免推理段吃掉小预算、拖慢主对话交替）
func chat(messages: Array, max_tokens := 300, no_think := false) -> String:
	var body := {
		"model": model,
		"messages": messages,
		"max_tokens": max_tokens,
		"temperature": 0.7,
		"top_p": 0.9,
		# 降复读（OpenAI 兼容参数，llama-server 原生支持）：
		# 让她少用重复的口头禅和句式（实测她连「嗯~…就好」「轻轻应了一手」都复读），发言更像人
		"frequency_penalty": 0.4,
		"presence_penalty": 0.2,
		"cache_prompt": true,          # 复用上轮 KV 前缀（人设+对局状态不变）：本地点评首字更快
	}
	# 思考档位（本地 Qwen3 系实测：同一句点评 0≈1 秒 / 1≈5 秒 / 2≈10 秒起步）：
	# - 1 中等：给推理段限 token 预算，并在预算用尽处注入收尾提示，让模型转入正式回答；
	# - 2 强思考：推理不设限，max_tokens 加预算，答案不被推理段挤掉；
	# 云端 API 拒绝这些参数（4xx）时自动去掉并记住，之后不再发送。
	var with_think := not _think_params_unsupported
	if with_think:
		body["chat_template_kwargs"] = {"enable_thinking": think_mode > 0 and not no_think}
		if think_mode == 1 and not no_think:
			body["reasoning_budget_tokens"] = REASON_BUDGET_MED
			body["reasoning_budget_message"] = REASON_STOP_MSG
	if not no_think:
		if think_mode == 2:
			body["max_tokens"] = max_tokens + 800
		elif think_mode == 1:
			body["max_tokens"] = max_tokens + REASON_BUDGET_MED
	var r := await _post(body)
	var err_txt := str(r.get("_error", ""))
	if err_txt != "" and with_think and (err_txt.begins_with("HTTP 400") or err_txt.begins_with("HTTP 422")):
		# 只有服务端「明确拒绝参数」（400/422）才去掉重试；连接失败等不许乱扣帽子，否则一次断连就永久丢档
		_think_params_unsupported = true
		print("[LLM] 服务端不支持思考参数（多为云端 API），本次去掉重试")
		body.erase("chat_template_kwargs")
		body.erase("reasoning_budget_tokens")
		body.erase("reasoning_budget_message")
		r = await _post(body)
		with_think = false
	if r.has("_error"):
		return "调用出错：" + str(r["_error"])
	var content := _content_of(r)
	if content == "" and with_think and think_mode == 0:
		# 关思考仍为空（罕见）：退回默认模式再试一次
		body.erase("chat_template_kwargs")
		r = await _post(body)
		if not r.has("_error"):
			content = _content_of(r)
	if content == "" and with_think and think_mode > 0:
		# 推理段把预算全耗光、正文还没开口（强思考偶发）：
		# 改用快速模式立刻再要一次正式回答——绝不把原始推理段显示给玩家
		body["chat_template_kwargs"] = {"enable_thinking": false}
		body.erase("reasoning_budget_tokens")
		body.erase("reasoning_budget_message")
		body["max_tokens"] = max_tokens
		r = await _post(body)
		if not r.has("_error"):
			content = _content_of(r)
	return content if content != "" else "唔...我脑子有点转不过来了"

func _content_of(r: Dictionary) -> String:
	var choices = r.get("choices")
	if typeof(choices) != TYPE_ARRAY or (choices as Array).is_empty():
		return ""
	var first = choices[0]
	if typeof(first) != TYPE_DICTIONARY:
		return ""
	var msg = (first as Dictionary).get("message")
	if typeof(msg) != TYPE_DICTIONARY:
		return ""
	if not r.has("_reasoning"):
		r["_reasoning"] = str((msg as Dictionary).get("reasoning_content", ""))
	return clean_content(str((msg as Dictionary).get("content", "")))

## 串行入口：同一时刻只有一个请求在飞（闲置搭话可能与落子分析几乎同时发起）
func _post(body: Dictionary) -> Dictionary:
	while _inflight:
		await get_tree().create_timer(0.12).timeout
	_inflight = true
	var r: Dictionary = await _post_locked(body)
	_inflight = false
	return r

func _post_locked(body: Dictionary) -> Dictionary:
	if _http == null:
		return {"_error": "HTTP 客户端未就绪"}
	await _ensure_base()
	if not await _reachable():
		if _local_target():
			return {"_error": "本地 LLM 服务暂未就绪（%s 正在加载或自动重试，最长几分钟），稍等再试" % base_url}
		return {"_error": "无法连接 LLM（%s），请确认服务已启动" % base_url}
	var headers := _headers()
	var err := _http.request(_endpoint(), headers, HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		return {"_error": "无法连接 LLM（%s），请确认服务已启动" % base_url}
	var res: Array = await _http.request_completed
	var code := int(res[1])
	var text := (res[3] as PackedByteArray).get_string_from_utf8()
	if code == 0:
		# TCP 连上了但请求中断：服务多半刚崩溃/重启（不是「不支持参数」，别误判成 4xx）
		return {"_error": "LLM 连接被中断（%s），可能服务刚重启，稍等再试" % base_url}
	if code != 200:
		push_warning("[LLM] HTTP %d: %s" % [code, text.substr(0, 300)])
		if code == 503:
			# llama-server 加载模型期间对一切请求回 503：这是「还在加载」不是出错，
			# 别对玩家显示「检查模型名」这种吓人的话（首屏 503 全走这里）
			if _local_target():
				return {"_error": "本地 LLM 正在加载模型（十几秒到半分钟），稍等一小会儿再和我说话吧"}
			return {"_error": "服务端暂时不可用（HTTP 503），稍后再试"}
		return {"_error": "HTTP %d（检查模型名是否为 %s）" % [code, model]}
	var parsed = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		return {"_error": "响应不是合法 JSON"}
	return parsed

## 服务地址是否指向本机（本地服务加载/重启期允许「稍等再试」的提示语）
func _local_target() -> bool:
	return base_url.contains("127.0.0.1") or base_url.contains("localhost") or base_url.contains("::1")

## 看护本地服务：不可达时回调重新拉起（只对本机目标生效；未接回调则不做任何事）
func _watch_tick() -> void:
	if relaunch_cb.is_null():
		return
	await _ensure_base()
	if not _local_target():
		return
	if await _reachable():
		_watch_fails = 0
		return
	_watch_fails += 1
	if _watch_fails < WATCH_TRIGGER:
		return
	var now := Time.get_ticks_msec()
	if now - _watch_last_ms < WATCH_COOLDOWN_MS:
		return
	_watch_last_ms = now
	_watch_fails = 0
	print("[LLM] 本地服务已无响应，请求重新拉起…")
	relaunch_cb.call()

## 启动时探测可用模型，便于用户核对模型名（用独立节点，避免占用聊天通道）
func probe_models() -> void:
	await _ensure_base()
	if not await _reachable():
		if _local_target():
			print("[LLM] 本地服务 %s 暂未就绪（正在加载或自动重试，稍后会自行恢复，不影响下棋）" % base_url)
		else:
			print("[LLM] 无法连接 %s（聊天功能将不可用），请确认服务已启动" % base_url)
		return
	var probe := HTTPRequest.new()
	probe.timeout = 5.0
	add_child(probe)
	var url := base_url.rstrip("/") + "/models"
	if probe.request(url, _headers()) != OK:      # 云端服务 /models 也需要鉴权
		probe.queue_free()
		return
	var res: Array = await probe.request_completed
	probe.queue_free()
	if int(res[0]) != HTTPRequest.RESULT_SUCCESS:
		return
	var text := (res[3] as PackedByteArray).get_string_from_utf8()
	var parsed = JSON.parse_string(text)
	if typeof(parsed) == TYPE_DICTIONARY:
		var ids: Array[String] = []
		for m in (parsed as Dictionary).get("data", []):
			ids.append(str((m as Dictionary).get("id", "")))
		print("[LLM] 可用模型：", ids, " 当前使用：", model)