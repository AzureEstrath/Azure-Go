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

func _ready() -> void:
	var args := OS.get_cmdline_user_args()
	var game := _game_mode(args)
	if DisplayServer.get_name() != "headless":
		if game:
			_prepare_game_window()
		else:
			_prepare_pet_window()
	if game:
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

func _prepare_game_window() -> void:
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_ALWAYS_ON_TOP, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_RESIZE_DISABLED, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_TRANSPARENT, true)
	get_viewport().transparent_bg = false
	DisplayServer.window_set_title("Azure · 围棋陪练")
	DisplayServer.window_set_size(Vector2i(1280, 800))
	var scr := DisplayServer.screen_get_usable_rect(DisplayServer.window_get_current_screen())
	DisplayServer.window_set_position(scr.position + (scr.size - Vector2i(1280, 800)) / 2)

func _prepare_pet_window() -> void:
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_ALWAYS_ON_TOP, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_TRANSPARENT, true)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_RESIZE_DISABLED, true)
	get_viewport().transparent_bg = true
	DisplayServer.window_set_title("Azure · 桌宠")
	DisplayServer.window_set_size(Vector2i(240, 280))
