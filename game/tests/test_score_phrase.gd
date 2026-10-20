extends SceneTree
## 目差说人话（v3.7.0 任务 H）前端门卫 —— **纯逻辑**，不渲染、不依赖场景树。
## 任务书点名的三个用例名**逐字照用**：近均势 / 胜率与目差不同调不矛盾 / null 安全；
## 另加量级分档的边界、脏值、以及「两个信号取更保守那一档」的策略断言。
## `Main.gd` 没有 `class_name` ⇒ 只能 `preload` 调它的**静态函数**（V11）。
## 运行：godot --headless --path game --script res://tests/test_score_phrase.gd

var _total := 0
var _fail := 0
var _m = null                       # Main.gd 脚本（静态函数入口）

func _initialize() -> void:
	_m = preload("res://scripts/ui/Main.gd")
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _run() -> void:
	print("[score] 目差说人话自检（量级分档 / 两个信号不打架 / 脏值安全 / 数字照旧保留）")
	test_near_even()
	test_winrate_and_score_disagree_does_not_contradict()
	test_null_score_is_safe()
	_test_bands()
	_test_raw_number_is_kept()

	# —— 附带：量级话不带「谁领先」（正负由数字自己说，措辞与胜率不可能互相矛盾） ——
	_check("四档措辞都在，且都不带「黑/白/领先/落后」（中性量级话）",
			_m.SCORE_PHRASES.size() == 4
			and _m.SCORE_PHRASES[0] == "差不多" and _m.SCORE_PHRASES[1] == "半个角"
			and _m.SCORE_PHRASES[2] == "一个角的实空" and _m.SCORE_PHRASES[3] == "已经很难追了")

	if _fail == 0:
		print("[score] PASS：", _total, " 项断言全部通过")
	else:
		print("[score] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

## 近均势：0.5 目 / 51% ⇒ 差不多
func test_near_even() -> void:
	_check("test_near_even：目差 0.5 + 胜率 51% ⇒ 差不多", _m.score_phrase(0.5, 0.51) == "差不多")
	_check("test_near_even：正负对称时措辞一致（绝不偏向某一方）",
			_m.score_phrase(1.0, 0.49) == _m.score_phrase(-1.0, 0.51))

## 胜率与目差偶尔打架（胜率 51% + 目差 -2.5 是正常的）：措辞不得让人以为矛盾
func test_winrate_and_score_disagree_does_not_contradict() -> void:
	var p := str(_m.score_phrase(-2.5, 0.51))
	_check("test_winrate_and_score_disagree_does_not_contradict：不说「你领先」也不说「已经很难追」",
			not p.contains("领先") and not p.contains("追") and not p.contains("落后"))
	_check("…落在最保守的档（差不多 / 半个角）", p == "差不多" or p == "半个角")
	# 打架时**取更保守的那一档**：目差 -20（黑大差）但胜率 55%（黑稍好）⇒ 不许跟着目差喊「很难追」
	_check("…目差与胜率打架时取更保守的一档（不跟目差喊）",
			_m.score_phrase(-20.0, 0.55) != "已经很难追了")
	# 同调时该笃定就笃定（不是一律和稀泥）
	_check("…两个信号同调（目差 +20 / 胜率 95%）⇒ 已经很难追了",
			_m.score_phrase(20.0, 0.95) == "已经很难追了")
	_check("…胜率单边脏值时退回只看目差那一档（不连累有效的目差）",
			_m.score_phrase(20.0, "nan") == "已经很难追了"
			and _m.score_phrase(0.5, null) == "差不多")

## null / 脏值安全缺省
func test_null_score_is_safe() -> void:
	_check("test_null_score_is_safe：score=null / winrate=null ⇒ --", _m.score_phrase(null, null) == "--")
	_check("…脏值（字符串 / 数组 / 字典 / bool / NaN / INF）一律 --，不抛",
			_m.score_phrase("abc", 0.5) == "--" and _m.score_phrase([], {}) == "--"
			and _m.score_phrase(true, 0.5) == "--"
			and _m.score_phrase(NAN, 0.5) == "--" and _m.score_phrase(INF, 0.5) == "--")
	_check("…未知分档一律 -1（供上面两条判空用）",
			_m.score_band(null) == -1 and _m.score_band("x") == -1 and _m.winrate_band(null) == -1)

## 分档边界：半开区间（边界值落进高一档）
func _test_bands() -> void:
	_check("目差分档：2.5 / 6.5 / 12.5 是半开区间下界（±对称）",
			_m.score_band(2.49) == 0 and _m.score_band(2.5) == 1
			and _m.score_band(6.5) == 2 and _m.score_band(12.5) == 3
			and _m.score_band(-2.5) == 1 and _m.score_band(-12.5) == 3)
	_check("胜率分档：45~55 / 55~65 / 65~80 / 80+（半开区间、两侧对称）",
			_m.winrate_band(0.5) == 0 and _m.winrate_band(0.46) == 0 and _m.winrate_band(0.44) == 1
			and _m.winrate_band(0.55) == 1 and _m.winrate_band(0.65) == 2 and _m.winrate_band(0.8) == 3
			and _m.winrate_band(0.2) == 3)

## 原始数字仍然显示（任务书要求）：那一行的文案由 `score_line` 定，数字格式与改动前逐字符一致
func _test_raw_number_is_kept() -> void:
	_check("原始数字照旧：+3.5 ⇒ 「目差: +3.5」", _m.score_line(3.5) == "目差: +3.5")
	_check("原始数字照旧：负数带符号 / 0 也照旧", _m.score_line(-2.5) == "目差: -2.5"
			and _m.score_line(0.0) == "目差: +0.0")
	_check("没有信息时沿用既有「目差: --」", _m.score_line(null) == "目差: --"
			and _m.score_line("脏值") == "目差: --")
	_check("小字在没信息时给空串（只占位不显示）", _m.score_hint(null, null) == ""
			and _m.score_hint(0.5, 0.51) == "差不多")
