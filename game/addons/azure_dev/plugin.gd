@tool
extends EditorPlugin
## 开发用：在编辑器工具栏提供「本地一键」与「全量测试」两个按钮。
## 只是开发便利工具——游戏运行时仍然只连后端，不自行拉起任何模型服务。

var _btn_run: Button
var _btn_test: Button


func _enter_tree() -> void:
	_btn_run = Button.new()
	_btn_run.text = "本地一键"
	_btn_run.tooltip_text = "启动本地 LLM(8080) / TTS(9880) / KataGo 服务(8100) / 后端(8000)，然后运行游戏"
	_btn_run.pressed.connect(_on_run_pressed)
	add_control_to_container(CONTAINER_TOOLBAR, _btn_run)

	_btn_test = Button.new()
	_btn_test.text = "全量测试"
	_btn_test.tooltip_text = "新开控制台跑后端 + KataGo 服务 + 前端 headless 全量测试"
	_btn_test.pressed.connect(_on_test_pressed)
	add_control_to_container(CONTAINER_TOOLBAR, _btn_test)


func _exit_tree() -> void:
	_remove_btn(_btn_run)
	_remove_btn(_btn_test)
	_btn_run = null
	_btn_test = null


func _remove_btn(btn: Button) -> void:
	if btn != null:
		remove_control_from_container(CONTAINER_TOOLBAR, btn)
		btn.queue_free()


## 仓库根目录：res:// 是 <repo>/game/，上一级即仓库根。
func _repo_root() -> String:
	return ProjectSettings.globalize_path("res://").path_join("..").simplify_path()


func _run_ps(extra: PackedStringArray) -> void:
	var script := _repo_root().path_join("dev_local.ps1")
	if not FileAccess.file_exists(script):
		push_error("[Azure] 找不到 dev_local.ps1：%s" % script)
		return
	var args := PackedStringArray(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script])
	args.append_array(extra)
	var pid := OS.create_process("powershell.exe", args)
	if pid <= 0:
		push_error("[Azure] 无法启动 PowerShell，请手动运行 dev_local.ps1")
	else:
		print("[Azure] 已启动：%s %s" % [script, " ".join(extra)])


func _on_run_pressed() -> void:
	_run_ps(PackedStringArray())


func _on_test_pressed() -> void:
	_run_ps(PackedStringArray(["-Tests"]))
