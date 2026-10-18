extends SceneTree
## 配置读写自检（headless，无外部依赖）
## 覆盖计划 00 的 Review Focus：v2.x 旧配置不崩、废弃键不被写回；
## 以及 C1 反向守卫（计划 01）：KataGo 改为独立云服务后，save() 必须清掉已无读取方的 katago_* 旧键。
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

	var da := DirAccess.open("user://")
	if da != null:
		da.remove("test_config_tmp.json")

	if _fail == 0:
		print("[config] PASS：", _total, " 项断言全部通过")
	else:
		print("[config] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
