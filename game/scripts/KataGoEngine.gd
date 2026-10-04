class_name KataGoEngine
extends Node
## 常驻 KataGo analysis 子进程：OS.execute_with_pipe 非阻塞管道，行分隔 JSON 请求/响应
## 请求格式要点：moves 必须是带颜色的配对 [["B","Q16"],["W","D4"]]，纯字符串会被本构建拒绝

signal engine_died(reason: String)

var max_visits := 100

var _exe := ""
var _model := ""
var _cfg := ""
var _stdio: FileAccess = null
var _errf: FileAccess = null
var _pid := -1
var _seq := 0
var _pending := {}                  # id -> {done, result, t0}
var _stderr_tail: Array[String] = []
var _dead := false
var _last_error := ""
var _startup_t0 := 0
var _ever_replied := false

const REQ_TIMEOUT_MS := 30000
const STARTUP_TIMEOUT_MS := 180000

func start(exe: String, model: String, cfg: String) -> bool:
	_exe = exe
	_model = model
	_cfg = cfg
	if not FileAccess.file_exists(exe):
		_fail("找不到 KataGo 可执行文件：%s" % exe)
		return false
	if not FileAccess.file_exists(model):
		_fail("找不到 KataGo 模型文件：%s" % model)
		return false
	if not FileAccess.file_exists(cfg):
		_fail("找不到 KataGo 配置文件：%s" % cfg)
		return false
	# 日志重定向到可写的用户数据目录：避免污染项目目录，导出后目录不可写时也不会出错
	var log_dir := ProjectSettings.globalize_path("user://katago_logs")
	DirAccess.make_dir_recursive_absolute(log_dir)
	var args := PackedStringArray([
		"analysis", "-model", model, "-config", cfg, "-analysis-threads", "1",
		"-override-config", "logDir=%s" % log_dir,
	])
	var res := OS.execute_with_pipe(exe, args, false)
	if not res.has("stdio") or res["stdio"] == null:
		_fail("无法启动 KataGo 子进程")
		return false
	_stdio = res["stdio"]
	_errf = res.get("stderr")
	_pid = int(res.get("pid", -1))
	_startup_t0 = Time.get_ticks_msec()
	print("[KataGo] 已启动 pid=%d  (%s)" % [_pid, exe])
	return true

func is_dead() -> bool:
	return _dead

func last_error() -> String:
	return _last_error

func stderr_tail(n := 3) -> String:
	var start := maxi(0, _stderr_tail.size() - n)
	var parts: Array[String] = []
	for i in range(start, _stderr_tail.size()):
		parts.append(_stderr_tail[i])
	return " | ".join(parts)

func _fail(reason: String) -> void:
	_dead = true
	_last_error = reason
	push_error("[KataGo] " + reason)
	engine_died.emit(reason)

func _exit_tree() -> void:
	shutdown()

## 退出时必须回收子进程，否则会残留 katago.exe
func shutdown() -> void:
	if _pid > 0:
		OS.kill(_pid)
		_pid = -1
	if _stdio != null:
		_stdio.close()
		_stdio = null
	if _errf != null:
		_errf.close()
		_errf = null

func _process(_delta: float) -> void:
	_drain_stderr()
	_read_stdout()

## stderr 必须持续排空，否则缓冲区写满后 KataGo 会卡死
func _drain_stderr() -> void:
	if _errf == null:
		return
	for i in 100:
		var line := _errf.get_line()
		if line == "":
			break
		var t := line.strip_edges()
		if t != "":
			_stderr_tail.append(t)
			if _stderr_tail.size() > 20:
				_stderr_tail.pop_front()

func _read_stdout() -> void:
	if _stdio == null:
		return
	for i in 200:                                  # 每帧限量，避免大响应卡住渲染
		var line := _stdio.get_line()
		if line == "":
			break
		var t := line.strip_edges()
		if t == "":
			continue
		var obj = JSON.parse_string(t)
		if typeof(obj) != TYPE_DICTIONARY:
			continue
		var id := str((obj as Dictionary).get("id", ""))
		if not _pending.has(id):
			continue
		var w: Dictionary = _pending[id]
		w["result"] = obj
		w["done"] = true
		_pending.erase(id)
		if (obj as Dictionary).has("error"):
			_last_error = "%s (field=%s)" % [str((obj as Dictionary)["error"]), str((obj as Dictionary).get("field", ""))]
			push_warning("[KataGo] " + _last_error)

## 异步分析：await analyze(moves)；出错时返回 {"error": "..."}
func analyze(moves: Array) -> Dictionary:
	if _dead:
		return {"error": "KataGo 未运行：" + _last_error}
	if _stdio == null:
		return {"error": "KataGo 管道不可用"}
	_seq += 1
	var id := "r%d" % _seq
	var req := {
		"id": id,
		"moves": moves,
		"rules": "chinese",
		"komi": 7.5,
		"boardXSize": 19,
		"boardYSize": 19,
		"maxVisits": max_visits,
		"includeOwnership": true,                    # 逐点归属（+ = 黑）：用于「双方实地估算」
	}
	var waiter := {"done": false, "result": {}, "t0": Time.get_ticks_msec()}
	_pending[id] = waiter                        # 先登记再写，避免错过响应
	_stdio.store_line(JSON.stringify(req))
	_stdio.flush()
	if _stdio.get_error() != OK:
		_pending.erase(id)
		_fail("KataGo 管道写入失败（%d）" % _stdio.get_error())
		return {"error": _last_error}
	while not waiter["done"]:
		if _dead:
			_pending.erase(id)
			return {"error": "KataGo 进程已退出：" + stderr_tail()}
		var elapsed := Time.get_ticks_msec() - int(waiter["t0"])
		var limit := STARTUP_TIMEOUT_MS if not _ever_replied else REQ_TIMEOUT_MS
		if elapsed > limit:
			_pending.erase(id)
			_last_error = "KataGo 分析超时（%d ms）" % elapsed
			return {"error": _last_error}
		await get_tree().process_frame
	_ever_replied = true
	return waiter["result"]