class_name LLMClient
extends Node
## 本地 LLM（OpenAI 兼容 /v1/chat/completions）调用
## 与网页版一致：剥离思考段落，content 为空时带 enable_thinking=false 重试一次

var base_url := "http://localhost:8080/v1"
var model := "qwen3.5"
var api_key := ""                # 云端 OpenAI 兼容服务：非空则发送 Authorization: Bearer <key>
var timeout_sec := 120.0

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

var _base_ready := false
var _inflight := false           # 串行化排队：闲置搭话等新调用等待前一个完成，而不是并发撞 Busy

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

## 连接预检：避免本地 LLM 未启动时白等满整个超时（一次要等 2 分钟）
func _reachable() -> bool:
	var m := _url_re.search(base_url)
	if m == null:
		return true
	var host := m.get_string(2)
	var port := int(m.get_string(3)) if m.get_string(3) != "" else (443 if m.get_string(1) == "https" else 80)
	var addrs: Array[String] = []
	if host == "127.0.0.1" or host == "localhost" or host == "::1":
		addrs = ["127.0.0.1", "::1"]          # 先 IPv4，再 IPv6
	else:
		for ip in IP.resolve_hostname(host, IP.TYPE_ANY):
			addrs.append(ip)
	if addrs.is_empty():
		addrs = [host]
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
func chat(messages: Array, max_tokens := 300) -> String:
	var body := {
		"model": model,
		"messages": messages,
		"max_tokens": max_tokens,
		"temperature": 0.7,
		"top_p": 0.9,
	}
	var r := await _post(body)
	if r.has("_error"):
		return "调用出错：" + str(r["_error"])
	var content := _content_of(r)
	if content == "":
		# 思考型模型可能把 token 耗在推理上：关掉思考模式重试一次
		body["chat_template_kwargs"] = {"enable_thinking": false}
		r = await _post(body)
		if r.has("_error"):
			return "调用出错：" + str(r["_error"])
		content = _content_of(r)
		if content == "":
			content = clean_content(str(r.get("_reasoning", "")))
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
		return {"_error": "无法连接 LLM（%s），请确认服务已启动" % base_url}
	var headers := _headers()
	var err := _http.request(_endpoint(), headers, HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		return {"_error": "无法连接 LLM（%s），请确认服务已启动" % base_url}
	var res: Array = await _http.request_completed
	var code := int(res[1])
	var text := (res[3] as PackedByteArray).get_string_from_utf8()
	if code != 200:
		push_warning("[LLM] HTTP %d: %s" % [code, text.substr(0, 300)])
		return {"_error": "HTTP %d（检查模型名是否为 %s）" % [code, model]}
	var parsed = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		return {"_error": "响应不是合法 JSON"}
	return parsed

## 启动时探测可用模型，便于用户核对模型名（用独立节点，避免占用聊天通道）
func probe_models() -> void:
	await _ensure_base()
	if not await _reachable():
		print("[LLM] 无法连接 %s（聊天功能将不可用），请确认本地 LLM 已启动" % base_url)
		return
	var probe := HTTPRequest.new()
	probe.timeout = 5.0
	add_child(probe)
	var url := base_url.rstrip("/") + "/models"
	if probe.request(url) != OK:
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