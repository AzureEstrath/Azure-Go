extends SceneTree
## 配置读写自检（headless，无外部依赖）
## 覆盖计划 00 的 Review Focus：v2.x 旧配置不崩、废弃键不被写回；
## 以及 C1 反向守卫（计划 01）：KataGo 改为独立云服务后，save() 必须清掉已无读取方的 katago_* 旧键。
## 另：R-F-2 #5 —— `joseki_rate` 的字符串分支与数值分支共用一套守卫（同一个值两条路读出来一致、不产出 NaN）。
## 全程只用临时文件，绝不读写仓库里的 game/config.json。
## 运行：godot --headless --path game --script res://tests/test_config.gd

const TMP := "user://test_config_tmp.json"
## 「后端自行拉起引擎」时代的旧键值（只需非默认、可辨识；save() 之后必须消失）
const KATAGO_BIN := "D:/KataGo/bin/katago/katago.exe"
const KATAGO_MODEL := "D:/KataGo/models/model-b18c384nbt.bin.gz"
const KATAGO_CONFIG := "D:/KataGo/bin/katago/default_gtp.cfg"
## save() 应清掉的旧键（v2.x 遗留：本地编排 / 窥屏 VLM / 本地语音拉起）
const DEAD_KEYS := [
	"backend_mode", "llm_auto_start", "llm_exe", "llm_model_path", "llm_port",
	"vlm_base_url", "vlm_auto_start", "vlm_exe", "vlm_model", "vlm_mmproj", "vlm_port",
	"tts_auto_start", "tts_bf16", "tts_short_prompt",
]

var _total := 0
var _fail := 0

## 注入式替身（修复波 #2 的门卫）：只数 `_push_settings` 被调用几次（不触网、不建 WS）。
## `NetClient` 是 `class_name`，headless 下 `new()` 不会 `_ready`（不连 WS）；`_push_settings` 是唯一
## 推送入口——覆写它即可把「推了几次」变成可断言的事实（真实 POST 只在 WS 打开时由 `_flush_settings` 发出）。
class CountingNetClient extends NetClient:
	var pushes := 0

	func _push_settings() -> void:
		pushes += 1

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _write_raw(d: Dictionary) -> void:
	var f := FileAccess.open(TMP, FileAccess.WRITE)
	f.store_string(JSON.stringify(d, "  "))
	f.close()

func _read_raw() -> Dictionary:
	var f := FileAccess.open(TMP, FileAccess.READ)
	var text := f.get_as_text()
	f.close()
	var d = JSON.parse_string(text)
	return d if typeof(d) == TYPE_DICTIONARY else {}

func _run() -> void:
	print("[config] 配置读写自检（临时文件 ", TMP, "，不碰 game/config.json）")

	# —— 1) v2.x 旧配置：cosyvoice → openai；azure 是自建服务活音色，须原样保留 ——
	_write_raw({"tts_mode": "cosyvoice", "tts_voice": "azure"})
	var c1 := AzureConfig.load_config(TMP)
	_check("v2.x tts_mode=cosyvoice 归一化为 openai", c1.tts_mode == "openai")
	_check("自建服务活音色 azure 原样保留", c1.tts_voice == "azure")

	# —— 2) 已废弃音色 id → 模板默认音色 ——
	for dead in ["中文男", "azure-soft"]:
		_write_raw({"tts_voice": dead})
		var c2 := AzureConfig.load_config(TMP)
		_check("废弃音色 %s → %s" % [dead, AzureConfig.TTS_VOICE_DEFAULT],
			c2.tts_voice == AzureConfig.TTS_VOICE_DEFAULT)

	# —— 3) save()：清旧键（含计划 01 起失效的 katago_*）与未知键、写回改动值 ——
	var raw := {
		# v2.x~v3.0.0 的「后端自行拉起引擎」旧键：KataGo 现在是独立云服务，这三个键必须被清掉
		"katago_bin": KATAGO_BIN,
		"katago_model": KATAGO_MODEL,
		"katago_config": KATAGO_CONFIG,
		"future_unknown_key": "保留我",
		"tts_api_key": "sk-old",
	}
	for k in DEAD_KEYS:
		raw[k] = "旧值"
	_write_raw(raw)
	var c3 := AzureConfig.load_config(TMP)
	c3.tts_api_key = "sk-changed"
	_check("save() 返回成功", c3.save())
	var out := _read_raw()
	for k in DEAD_KEYS:
		_check("save() 清除旧键 " + k, not out.has(k))
	_check("save() 清除旧键 katago_bin", not out.has("katago_bin"))
	_check("save() 清除旧键 katago_model", not out.has("katago_model"))
	_check("save() 清除旧键 katago_config", not out.has("katago_config"))
	_check("save() 保留未知键", out.get("future_unknown_key", "") == "保留我")
	_check("save() 写回改动的 tts_api_key", out.get("tts_api_key", "") == "sk-changed")

	# —— 4) 定式导学＝开关 + 频率（joseki_teach / joseki_rate）——
	# 口径与后端 `config._apply` 一致：键缺失 ⇒ 默认（teach=true / rate=0.5）；
	# 脏值（非数字）⇒ rate 退回 0.5；数值越界 ⇒ 夹到 [0,1]。
	_write_raw({})
	var c4 := AzureConfig.load_config(TMP)
	_check("joseki_rate 键缺失 ⇒ 默认 0.5", is_equal_approx(c4.joseki_rate, 0.5))
	_check("joseki_teach 键缺失 ⇒ 默认 true", c4.joseki_teach)

	for bad in ["abc", null]:
		_write_raw({"joseki_rate": bad})
		var cd := AzureConfig.load_config(TMP)
		_check("joseki_rate 脏值 %s ⇒ 0.5" % str(bad), is_equal_approx(cd.joseki_rate, 0.5))
	for pair in [[5.0, 1.0], [-1, 0.0]]:
		_write_raw({"joseki_rate": pair[0]})
		var cr := AzureConfig.load_config(TMP)
		_check("joseki_rate 越界 %s ⇒ 夹到 %s" % [str(pair[0]), str(pair[1])],
			is_equal_approx(cr.joseki_rate, pair[1]))

	# R-F-2 #5：字符串分支与数值分支**共用同一套守卫**（`_clamp_rate`）——同一个值经 JSON 字符串 /
	# 数字两条路读出来必须一致，尤其不许让字符串路径产出 NaN（那会被 save() 原样写回 config.json）。
	# R-GH-2：大数用 `"1e308"`（double 内的合法值，夹到 1.0）而**不用 `"1e999"`**——后者会让引擎
	# 自己打一行 `WARNING: Exponent too high`（门禁输出应当干净）。INF 的覆盖改由下面那条
	# 直接用 `INF` 常量的断言承担（走数值分支，同样零警告）。
	for pair in [["0.25", 0.25], ["0.5", 0.5], ["0.0", 0.0], ["1.0", 1.0], ["5", 1.0],
			["nan", 0.5], ["inf", 0.5], ["-inf", 0.5], ["1e308", 1.0]]:
		_write_raw({"joseki_rate": pair[0]})
		var cstr := AzureConfig.load_config(TMP)
		_check("joseki_rate 字符串 \"%s\" ⇒ %s（与数值分支同口径）" % [str(pair[0]), str(pair[1])],
			is_equal_approx(cstr.joseki_rate, pair[1]))
	_check("两条路共用一套守卫：NaN ⇒ 0.5、INF ⇒ 1.0（都不越界、都不产出 NaN）",
			not is_nan(AzureConfig._normalize_joseki_rate("nan"))
			and is_equal_approx(AzureConfig._normalize_joseki_rate("nan"), 0.5)
			and is_equal_approx(AzureConfig._normalize_joseki_rate(NAN), 0.5)
			and is_equal_approx(AzureConfig._normalize_joseki_rate(INF), 1.0)
			and is_equal_approx(AzureConfig._normalize_joseki_rate(5), 1.0))

	# 往返：读入 → 改动 → save() → 重新解析；开关 true / false 两个方向都要能回来
	_write_raw({"joseki_rate": 0.25, "joseki_teach": false, "future_unknown_key": "保留我"})
	var c5 := AzureConfig.load_config(TMP)
	_check("joseki_rate 读入 0.25", is_equal_approx(c5.joseki_rate, 0.25))
	_check("joseki_teach 读入 false", not c5.joseki_teach)
	c5.joseki_rate = 0.75
	c5.joseki_teach = true
	_check("save() 写回 joseki 改动成功", c5.save())
	var out2 := _read_raw()
	_check("save() 写回改动后的 joseki_rate", is_equal_approx(float(out2.get("joseki_rate", -1.0)), 0.75))
	_check("save() 写回 joseki_teach=true", bool(out2.get("joseki_teach", false)))
	_check("save() 仍保留未知键", out2.get("future_unknown_key", "") == "保留我")

	_write_raw({"joseki_teach": true, "joseki_rate": 0.5})
	var c6 := AzureConfig.load_config(TMP)
	_check("joseki_teach 读入 true", c6.joseki_teach)
	c6.joseki_teach = false
	c6.save()
	var out3 := _read_raw()
	_check("save() 写回 joseki_teach=false", not bool(out3.get("joseki_teach", true)))
	_check("save() 未改动的 joseki_rate 原值不变", is_equal_approx(float(out3.get("joseki_rate", -1.0)), 0.5))

	# —— 5) 情绪波动＝开关 + 基础概率（emotion_drift / drift_base，v3.8.0）——
	# 口径与 `joseki` 那一格一致：键缺失 ⇒ 默认（false / 0.03）；脏值 ⇒ 0.03；
	# **越界 ⇒ 夹到 `[0, DRIFT_BASE_LIMIT]`（0.08）而不是 `[0, 1]`**（v3.8.0 修复波 / #4：后端
	# `config._apply` 把 `drift_base` 夹到 `min(值, drift_max)`、`drift_max` 默认 0.08——两端必须
	# 读到同一个值，否则前端会把 0.5 推给后端、自己却留着 0.5）。
	_write_raw({})
	var c7 := AzureConfig.load_config(TMP)
	_check("emotion_drift 键缺失 ⇒ 默认 false", not c7.emotion_drift)
	_check("drift_base 键缺失 ⇒ 默认 0.03", is_equal_approx(c7.drift_base, 0.03))

	for bad in ["abc", null]:
		_write_raw({"drift_base": bad})
		var cd2 := AzureConfig.load_config(TMP)
		_check("drift_base 脏值 %s ⇒ 0.03" % str(bad), is_equal_approx(cd2.drift_base, 0.03))
	for pair in [[5.0, 0.08], [-1, 0.0]]:
		_write_raw({"drift_base": pair[0]})
		var cr2 := AzureConfig.load_config(TMP)
		_check("drift_base 越界 %s ⇒ 夹到 %s" % [str(pair[0]), str(pair[1])],
			is_equal_approx(cr2.drift_base, pair[1]))
	for pair in [["0.08", 0.08], ["0.03", 0.03], ["nan", 0.03], ["1e308", 0.08]]:
		_write_raw({"drift_base": pair[0]})
		var cstr2 := AzureConfig.load_config(TMP)
		_check("drift_base 字符串 \"%s\" ⇒ %s（与 joseki_rate 同一套守卫）" % [str(pair[0]), str(pair[1])],
			is_equal_approx(cstr2.drift_base, pair[1]))
	_check("drift_base 的 NaN / INF 口径：NaN ⇒ 默认 0.03，其余夹到 DRIFT_BASE_LIMIT",
			is_equal_approx(AzureConfig._normalize_drift_base(NAN), 0.03)
			and is_equal_approx(AzureConfig._normalize_drift_base(INF), AzureConfig.DRIFT_BASE_LIMIT)
			and is_equal_approx(AzureConfig._normalize_drift_base(5), AzureConfig.DRIFT_BASE_LIMIT))
	# 修复波 / #4：上限常量就是后端 `drift_max` 的默认（0.08），且「手改 0.5」两端一致
	_check("DRIFT_BASE_LIMIT 与后端 drift_max 默认一致（0.08）",
			is_equal_approx(AzureConfig.DRIFT_BASE_LIMIT, 0.08))
	_write_raw({"drift_base": 0.5})
	var ccap := AzureConfig.load_config(TMP)
	_check("手改 drift_base=0.5 ⇒ 前端也夹到 0.08（与后端同一个值，不再留 0.5）",
			is_equal_approx(ccap.drift_base, 0.08))

	# 往返：读入 → 改动 → save() → 重新解析；开关两个方向都要能回来，未知键照旧原样保留
	_write_raw({"emotion_drift": true, "drift_base": 0.05, "future_unknown_key": "保留我"})
	var c8 := AzureConfig.load_config(TMP)
	_check("emotion_drift 读入 true", c8.emotion_drift)
	_check("drift_base 读入 0.05", is_equal_approx(c8.drift_base, 0.05))

	# 修复轮 1 / Minor #7：开关**只认 JSON 的 true / false**。GDScript 的 `bool("false")` 是 true，
	# 直接用它会把字符串脏值读成「开」；数字 / null 同理当脏值 → 默认 false（后端 `config._apply` 同口径）。
	for bad in ["false", "true", 1, 0, null]:
		_write_raw({"emotion_drift": bad})
		var cf := AzureConfig.load_config(TMP)
		_check("emotion_drift 脏值 %s ⇒ 默认 false（不把脏值读成「开」）" % str(bad), not cf.emotion_drift)

	c8.emotion_drift = false
	c8.drift_base = 0.08
	_check("save() 写回情绪波动改动成功", c8.save())
	var out4 := _read_raw()
	_check("save() 写回 emotion_drift=false", not bool(out4.get("emotion_drift", true)))
	_check("save() 写回改动后的 drift_base", is_equal_approx(float(out4.get("drift_base", -1.0)), 0.08))
	_check("save() 写回情绪波动时仍保留未知键", out4.get("future_unknown_key", "") == "保留我")

	_write_raw({"emotion_drift": false, "drift_base": 0.03})
	var c9 := AzureConfig.load_config(TMP)
	c9.emotion_drift = true
	c9.save()
	var out5 := _read_raw()
	_check("save() 写回 emotion_drift=true", bool(out5.get("emotion_drift", false)))
	_check("save() 未改动的 drift_base 原值不变", is_equal_approx(float(out5.get("drift_base", -1.0)), 0.03))

	# —— 6) 聊天摘要用 LLM（chat_digest_llm，v3.8.0）——
	# 口径与 `emotion_drift` 完全一致：键缺失 ⇒ 默认 false；**只认 JSON 的 true / false**（脏值 ⇒ 默认，
	# `bool("false")` 是 true 的坑同样要挡）；往返读写与未知键原样保留照旧。
	_write_raw({})
	var c10 := AzureConfig.load_config(TMP)
	_check("chat_digest_llm 键缺失 ⇒ 默认 false", not c10.chat_digest_llm)
	for bad in ["false", "true", 1, 0, null]:
		_write_raw({"chat_digest_llm": bad})
		var cdg := AzureConfig.load_config(TMP)
		_check("chat_digest_llm 脏值 %s ⇒ 默认 false（不把脏值读成「开」）" % str(bad), not cdg.chat_digest_llm)
	_write_raw({"chat_digest_llm": true, "future_unknown_key": "保留我"})
	var c11 := AzureConfig.load_config(TMP)
	_check("chat_digest_llm 读入 true", c11.chat_digest_llm)
	c11.chat_digest_llm = false
	_check("save() 写回 chat_digest_llm 改动成功", c11.save())
	var out6 := _read_raw()
	_check("save() 写回 chat_digest_llm=false", not bool(out6.get("chat_digest_llm", true)))
	_check("save() 写回 chat_digest_llm 时仍保留未知键", out6.get("future_unknown_key", "") == "保留我")

	var da := DirAccess.open("user://")
	if da != null:
		da.remove("test_config_tmp.json")

	# —— 6′) 玩家棋风画像 / 时间感知（profile_enabled / time_aware_enabled，v3.9.0）——
	# 口径与 `chat_digest_llm` 完全一致：键缺失 ⇒ 默认 false；**只认 JSON 的 true / false**
	# （`bool("false")` 是 true 的坑同样要挡）；往返读写与未知键原样保留照旧。
	_write_raw({})
	var c12 := AzureConfig.load_config(TMP)
	_check("profile_enabled 键缺失 ⇒ 默认 false", not c12.profile_enabled)
	_check("time_aware_enabled 键缺失 ⇒ 默认 false", not c12.time_aware_enabled)
	for bad in ["false", "true", 1, 0, null]:
		_write_raw({"profile_enabled": bad, "time_aware_enabled": bad})
		var cpf := AzureConfig.load_config(TMP)
		_check("两个画像开关脏值 %s ⇒ 默认 false（不把脏值读成「开」）" % str(bad),
			not cpf.profile_enabled and not cpf.time_aware_enabled)
	_write_raw({"profile_enabled": true, "time_aware_enabled": true, "future_unknown_key": "保留我"})
	var c13 := AzureConfig.load_config(TMP)
	_check("profile_enabled 读入 true", c13.profile_enabled)
	_check("time_aware_enabled 读入 true", c13.time_aware_enabled)
	c13.profile_enabled = false
	c13.time_aware_enabled = false
	_check("save() 写回画像开关成功", c13.save())
	var out7 := _read_raw()
	_check("save() 写回 profile_enabled=false", not bool(out7.get("profile_enabled", true)))
	_check("save() 写回 time_aware_enabled=false", not bool(out7.get("time_aware_enabled", true)))
	_check("save() 写回画像开关时仍保留未知键", out7.get("future_unknown_key", "") == "保留我")

	# —— 6″) 画像 / 时间感知两个开关的**原子推送**（v3.9.0，与 set_drift / set_joseki 同一条做法） ——
	var nf := CountingNetClient.new()
	nf.set_profile(true, true)
	_check("一次改画像 / 时间感知只推一次 /api/config（原子入口）", nf.pushes == 1)
	_check("原子入口落定后两个字段都生效", nf.profile_enabled and nf.time_aware_enabled)
	var nf2 := CountingNetClient.new()
	nf2.profile_enabled = true
	_check("单独赋 profile_enabled 仍推一次（setter 语义与既有开关一致）", nf2.pushes == 1)
	var nf3 := CountingNetClient.new()
	nf3.time_aware_enabled = true
	_check("单独赋 time_aware_enabled 仍推一次（setter 语义与既有开关一致）", nf3.pushes == 1)
	var nf4 := CountingNetClient.new()
	nf4.profile_enabled = true
	nf4.time_aware_enabled = true
	_check("对照：分两次赋 setter 会推两次（原子入口因此必要）", nf4.pushes == 2)
	nf4.free()
	nf3.free()
	nf2.free()
	nf.free()

	# —— 6‴) 画像面板文本（纯函数 `Main.profile_panel_text`，v3.9.0 / Q6） ——
	# 四种退化都要有可读文案；有画像时逐维渲染（数值按百分比 + 把握 + 局数），未知键忽略、脏值不抛。
	var mp := preload("res://scripts/ui/Main.gd")
	_check("profile_panel_text：后端没返回 ⇒ 可读失败文案",
		mp.profile_panel_text(null).contains("读取失败"))
	_check("profile_panel_text：ok=false ⇒ 可读失败文案",
		mp.profile_panel_text({"ok": false}).contains("读取失败"))
	_check("profile_panel_text：功能关着 ⇒ 提示去打开开关",
		mp.profile_panel_text({"ok": true, "enabled": false, "axes": {}, "games": []}).contains("关着"))
	_check("profile_panel_text：开着但没画像 ⇒ 提示还没攒下",
		mp.profile_panel_text({"ok": true, "enabled": true, "axes": {}, "games": []}).contains("还没有攒下"))
	var panel := mp.profile_panel_text({
		"ok": true, "enabled": true,
		"axes": {
			"open.star": {"value": 0.75, "confidence": 0.5, "n_games": 8},
			"blunder.top_type": {"value": "深入", "confidence": 0.25, "n_games": 4},
			"persona.topic.定式": {"value": 0.6, "confidence": 0.1, "n_games": 2},
			"unknown.axis": {"value": 0.9, "confidence": 1.0, "n_games": 9},
		},
		"games": [{"id": 1}, {"id": 2}],
	})
	_check("profile_panel_text：标题带来源局数", panel.contains("2 局记录"))
	_check("profile_panel_text：数值维按百分比 + 把握 + 局数",
		panel.contains("开局·星位：75%（把握 50%，8 局）"))
	_check("profile_panel_text：非数值维原样显示（手型）", panel.contains("常亏手型：深入"))
	_check("profile_panel_text：persona.topic.* 走前缀族标签", panel.contains("常聊·定式：60%"))
	_check("profile_panel_text：未知维忽略（不渲染、不抛）", not panel.contains("unknown.axis"))
	_check("profile_panel_text：脏值不抛（缺 value / 脏 confidence）",
		not mp.profile_panel_text({"ok": true, "enabled": true,
			"axes": {"open.star": {"confidence": "abc"}, "style.tenuki": {"value": 0.2}},
			"games": []}).is_empty())

	# —— 7) 定式「开关 + 频率」原子推送（v3.7.0 修复波 / 跨任务发现 #2） ——
	# 一次改档位必须**只推一次** `/api/config`：分两次赋 setter 会各推一条 POST（`_post` 每次
	# `HTTPRequest.new()`，到达顺序无保证），极端情况下后端留下「新开关 + 旧频率」，直到下次改配置才自纠。
	var nc := CountingNetClient.new()
	nc.set_joseki(true, 0.75)
	_check("一次改档位只推一次 /api/config（原子入口）", nc.pushes == 1)
	_check("原子入口落定后两个字段都生效",
			nc.joseki_teach and is_equal_approx(nc.joseki_rate, 0.75))
	var nc2 := CountingNetClient.new()
	nc2.joseki_teach = false
	_check("单独赋 joseki_teach 仍推一次（既有 setter 语义不变）", nc2.pushes == 1)
	var nc3 := CountingNetClient.new()
	nc3.joseki_rate = 0.25
	_check("单独赋 joseki_rate 仍推一次（既有 setter 语义不变）", nc3.pushes == 1)
	var nc4 := CountingNetClient.new()
	nc4.joseki_teach = true
	nc4.joseki_rate = 0.75
	_check("对照：分两次赋 setter 会推两次（这正是原子入口要避免的）", nc4.pushes == 2)
	nc4.free()
	nc3.free()
	nc2.free()
	nc.free()

	# —— 3) 情绪波动「开关 + 基础概率」原子推送（v3.8.0，与 `set_joseki` 同一条做法） ——
	var nd := CountingNetClient.new()
	nd.set_drift(true, 0.08)
	_check("一次改情绪波动档位只推一次 /api/config（原子入口）", nd.pushes == 1)
	_check("原子入口落定后两个字段都生效",
			nd.emotion_drift and is_equal_approx(nd.drift_base, 0.08))
	var nd2 := CountingNetClient.new()
	nd2.emotion_drift = true
	_check("单独赋 emotion_drift 仍推一次（setter 语义与 joseki 一致）", nd2.pushes == 1)
	var nd3 := CountingNetClient.new()
	nd3.drift_base = 0.05
	_check("单独赋 drift_base 仍推一次（setter 语义与 joseki 一致）", nd3.pushes == 1)
	var nd4 := CountingNetClient.new()
	nd4.emotion_drift = true
	nd4.drift_base = 0.05
	_check("对照：分两次赋 setter 会推两次（原子入口因此必要）", nd4.pushes == 2)
	nd4.free()
	nd3.free()
	nd2.free()
	nd.free()

	# —— 4) 情绪波动档位映射（Main.drift_level_index）：关闭 ⇒ 0，否则取最近的一档、并列取更保守的一档 ——
	var m := preload("res://scripts/ui/Main.gd")
	_check("drift_level_index：关闭档恒 0（基础概率是多少都一样）",
			m.drift_level_index(false, 0.08) == 0 and m.drift_level_index(false, 0.0) == 0
			and m.drift_level_index(false, NAN) == 0)
	_check("drift_level_index：三档精确命中（0.03 ⇒ 1 / 0.05 ⇒ 2 / 0.06 ⇒ 3）",
			m.drift_level_index(true, 0.03) == 1 and m.drift_level_index(true, 0.05) == 2
			and m.drift_level_index(true, 0.06) == 3)
	_check("drift_level_index：0.08（后端默认上限）只会贴到最高档，不越界",
			m.drift_level_index(true, 0.08) == 3)
	# 修复轮 1 / Important #2：**每一档都必须明显低于后端 drift_max（默认 0.08）**——`emotion_bias`
	# 只往上加，档位一旦顶到上限就被夹平成「固定概率」，情绪再也不参与（老「高 8%」正是如此，
	# 而 tooltip 却承诺会浮动）。这条与后端 `test_ui_levels_never_flatline_the_mood_bias` 成对。
	var levels: Array = m.DRIFT_BASES
	_check("三档基础概率逐档递增，且**每一档都 < 0.08**（留出情绪浮动的余量）",
			levels.size() == 3 and float(levels[0]) < float(levels[1])
			and float(levels[1]) < float(levels[2]) and float(levels[2]) < 0.08)
	_check("最高档是 6%（不是 8%：8% 会被 drift_max 夹平）", is_equal_approx(float(levels[2]), 0.06))
	_check("drift_level_index：并列取更低的一档（0.04 在 1 与 2 之间 ⇒ 1）",
			m.drift_level_index(true, 0.04) == 1)
	_check("drift_level_index：越界 / 脏值不越界（0.0 ⇒ 1、5.0 ⇒ 3、NaN ⇒ 1，恒在 1..3）",
			m.drift_level_index(true, 0.0) == 1 and m.drift_level_index(true, 5.0) == 3
			and m.drift_level_index(true, NAN) == 1)

	# —— 5) 聊天摘要用 LLM 原子推送（v3.8.0，与 `set_drift` / `set_joseki` 同一条做法） ——
	var ne := CountingNetClient.new()
	ne.set_digest(true)
	_check("一次切聊天摘要只推一次 /api/config（原子入口）", ne.pushes == 1)
	_check("原子入口落定后字段生效", ne.chat_digest_llm)
	var ne2 := CountingNetClient.new()
	ne2.chat_digest_llm = true
	_check("单独赋 chat_digest_llm 仍推一次（setter 语义与既有开关一致）", ne2.pushes == 1)
	ne2.free()
	ne.free()

	if _fail == 0:
		print("[config] PASS：", _total, " 项断言全部通过")
	else:
		print("[config] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
