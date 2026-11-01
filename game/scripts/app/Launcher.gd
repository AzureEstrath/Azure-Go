extends Node
## 进程入口：桌宠是主程序；围棋是桌宠另开的独立窗口。
##
## 规则：
##   · 默认 / `--pet`：加载 PetShell（透明置顶小窗）
##   · `--game` / `--no-pet` / 截图调试参数 / 无头：加载 Main.tscn（正式围棋窗口）
##   · `--from-pet`：由 PetShell 拉起，Main 继续 backend 里的上一局
##
## 这样项目主场景不再是“带桌宠模式的围棋”，而是“能打开围棋的桌宠”。

const MainScene := preload("res://scenes/Main.tscn")
const PetShellScript := preload("res://scripts/app/PetShell.gd")
const GAME_SIZE := Vector2i(1280, 800)
const PAPER_CLEAR := Color(0.953, 0.941, 0.918)

func _ready() -> void:
	var args := OS.get_cmdline_user_args()
	var game := _game_mode(args)
	if DisplayServer.get_name() != "headless":
		if game:
			_prepare_game_window(args)
		else:
			_prepare_pet_window()
	if game:
		# 桌宠打开的窗口：第一帧先铺纸，Main 再用 ModeTransition 淡开，避免白闪 / 桌面空洞。
		RenderingServer.set_default_clear_color(PAPER_CLEAR)
		var main = MainScene.instantiate()
		add_child(main)
	else:
		add_child(PetShellScript.new())

func _game_mode(args: PackedStringArray) -> bool:
	return pick_mode(args, DisplayServer.get_name() == "headless",
		AzureConfig.load_config().pet_enabled)

## 纯函数（门卫可测）：这次运行加载围棋窗口，还是桌宠外壳。
static func pick_mode(args: PackedStringArray, headless: bool, pet_enabled: bool) -> bool:
	if headless:
		return true                            # 无头测试 / 自检：走正式窗口，不卡在桌宠交互
	for a in args:
		if a == "--pet":
			return false
	for a in args:
		if a == "--game" or a == "--no-pet":
			return true
		if a == "--title" or a == "--demo" or a == "--setup" or a.begins_with("--shot="):
			return true
	return not pet_enabled

func _prepare_game_window(args: PackedStringArray = []) -> void:
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_ALWAYS_ON_TOP, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_RESIZE_DISABLED, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_TRANSPARENT, true)
	get_viewport().transparent_bg = false
	DisplayServer.window_set_title("Azure · 围棋陪练")
	DisplayServer.window_set_size(GAME_SIZE)
	var scr := _screen_rect_for(pet_position(args))
	DisplayServer.window_set_position(scr.position + (scr.size - GAME_SIZE) / 2)

func _prepare_pet_window() -> void:
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_ALWAYS_ON_TOP, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_TRANSPARENT, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_RESIZE_DISABLED, true)
	get_viewport().transparent_bg = true
	DisplayServer.window_set_title("Azure · 桌宠")
	DisplayServer.window_set_size(Vector2i(240, 280))

## 桌宠拉起的窗口：沿用桌宠所在屏幕，而不是新进程默认的主屏。
func _screen_rect_for(point: Vector2i) -> Rect2i:
	if point.x < 0 or point.y < 0:
		return DisplayServer.screen_get_usable_rect(DisplayServer.window_get_current_screen())
	var idx := screen_for_point(point, _screen_rects())
	return DisplayServer.screen_get_usable_rect(idx)

func _screen_rects() -> Array:
	var out := []
	for i in range(DisplayServer.get_screen_count()):
		out.append(DisplayServer.screen_get_usable_rect(i))
	return out

## 纯函数（门卫可测）：桌宠位置落在哪块屏幕；认不出返回 0。
static func screen_for_point(point: Vector2i, rects: Array) -> int:
	for i in range(rects.size()):
		var r: Rect2i = rects[i]
		if r.has_point(point):
			return i
	return 0

## 纯函数：从 `--pet-x=123 --pet-y=456` 取桌宠位置；缺任何一维返回 (-1,-1)。
static func pet_position(args: PackedStringArray) -> Vector2i:
	var x := -1
	var y := -1
	for a in args:
		var s := str(a)
		if s.begins_with("--pet-x="):
			x = int(s.substr(8))
		elif s.begins_with("--pet-y="):
			y = int(s.substr(8))
	if x < 0 or y < 0:
		return Vector2i(-1, -1)
	return Vector2i(x, y)
