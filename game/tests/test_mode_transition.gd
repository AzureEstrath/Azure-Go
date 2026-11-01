extends SceneTree
## 桌宠 ⇄ 对弈纸张转场（v4.6.0）纯逻辑门卫：
## 覆盖 / 揭开的端点严格 0/1、单调、时长为正；不依赖窗口、主题或场景树。

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

func _run() -> void:
	print("[transition] 桌宠 ⇄ 对弈纸张转场自检（端点 / 单调 / 时长 / busy）")
	var t = preload("res://scripts/ui/ModeTransition.gd").new()
	_check("覆盖从 0 开始", absf(t.alpha_at(0.0, t.COVER_SEC, true)) < 1e-6)
	_check("覆盖到时长时满纸", absf(t.alpha_at(t.COVER_SEC, t.COVER_SEC, true) - 1.0) < 1e-6)
	_check("揭开从满纸开始", absf(t.alpha_at(0.0, t.REVEAL_SEC, false) - 1.0) < 1e-6)
	_check("揭开到时长时全透明", absf(t.alpha_at(t.REVEAL_SEC, t.REVEAL_SEC, false)) < 1e-6)
	_check("覆盖曲线单调递增",
		t.alpha_at(0.05, t.COVER_SEC, true) < t.alpha_at(0.10, t.COVER_SEC, true)
		and t.alpha_at(0.10, t.COVER_SEC, true) < t.alpha_at(0.18, t.COVER_SEC, true))
	_check("揭开曲线单调递减",
		t.alpha_at(0.05, t.REVEAL_SEC, false) > t.alpha_at(0.10, t.REVEAL_SEC, false)
		and t.alpha_at(0.10, t.REVEAL_SEC, false) > t.alpha_at(0.22, t.REVEAL_SEC, false))
	_check("越界不越界（负时间 / 超时长）",
		absf(t.alpha_at(-0.1, t.COVER_SEC, true)) < 1e-6
		and absf(t.alpha_at(9.0, t.COVER_SEC, true) - 1.0) < 1e-6)
	_check("脏时长不炸：覆盖给满纸 / 揭开给全透明",
		absf(t.alpha_at(0.1, 0.0, true) - 1.0) < 1e-6
		and absf(t.alpha_at(0.1, -1.0, false)) < 1e-6)
	_check("两段时长都是可感知但很短的转场",
		t.COVER_SEC >= 0.12 and t.COVER_SEC <= 0.35
		and t.REVEAL_SEC >= 0.15 and t.REVEAL_SEC <= 0.45)
	_check("未开始转场时 busy 为假", not t.busy())
	t.free()

	if _fail == 0:
		print("[transition] PASS：", _total, " 项断言全部通过")
	else:
		print("[transition] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)
