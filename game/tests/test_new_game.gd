extends SceneTree
## 「从主界面进入＝新的一局」（v3.12.1）前端门卫 —— **纯逻辑**，不渲染、不依赖场景树。
##
## 钉住三件事：
##   ① 首次启动不 reset（后端本来就是空局、服务还没起），其余每一次进对局都要；
##   ② 空局的侧栏文案（`state` 事件不带胜率 / 目差，新局里不能留着上一局的数字）；
##   ③ `TitleScreen` 的措辞由 `test_title_screen.gd` 管，这里只管 `Main` 的入口策略。
## 运行：godot --headless --path game --script res://tests/test_new_game.gd

var _total := 0
var _fail := 0
var _m = null

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
	print("[new_game] 新对局入口自检（每次进对局都重开 / 空局文案）")
	_test_entry_restarts_every_time()
	_test_fresh_labels()

	if _fail == 0:
		print("[new_game] PASS：", _total, " 项断言全部通过")
	else:
		print("[new_game] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

## 「每次从主界面进入游戏刷新棋局」：只有首次启动可以沿用后端那份空局。
func _test_entry_restarts_every_time() -> void:
	_check("首次启动（_game_started=false）不 reset：后端本来就是空局",
			not _m.entry_needs_restart(false))
	_check("对局中回过主界面（_game_started=true）再进来：必须重开一局",
			_m.entry_needs_restart(true))
	_check("脏值也按「已经开过局」处理（真实调用点只传 bool，纯函数兜底不抛）",
			_m.entry_needs_restart(1) and not _m.entry_needs_restart(null))

## 新局时侧栏必须回到「空局」：手数 0 / 胜率与目差都回到 -- / 量级小字清空。
## 不这样做的话，`reset` 的 `state` 事件只换棋盘，上一局的胜率与目差会一直挂在右栏。
func _test_fresh_labels() -> void:
	var lab: Dictionary = _m.fresh_game_labels()
	_check("空局：手数归零", str(lab.get("moves", "")) == "手数: 0")
	_check("空局：胜率回到 --", str(lab.get("winrate", "")) == "黑胜率: --")
	_check("空局：目差回到 --（沿用 score_line(null)）",
			str(lab.get("score", "")) == "目差: --" and _m.score_line(null) == "目差: --")
	_check("空局：目差下面的量级小字清空（不显示上一局的「已经很难追了」）",
			str(lab.get("hint", "x")) == "")
	_check("字段齐全（四格：手数 / 胜率 / 目差 / 小字）", lab.size() == 4)
