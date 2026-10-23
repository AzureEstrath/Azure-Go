extends SceneTree
## 化身 VRM 路径解析门卫（v3.12.2）——纯函数，不加载模型。
##
## 背景：`vrm_path` 写成相对路径（如 `"Azure.vrm"`）时，Godot 的 `FileAccess`
## 按**进程工作目录**解析，而源码运行的工作目录是 `game/`，发布包则是 exe 同目录。
## 以前只试一次原样路径，找不到就 `return`，整个化身变成空节点（看起来像「人物消失」）。
##
## 这里钉住：绝对路径原样；相对路径按 exe 同目录 → res:// → 工程根依次找；
## 仓库根目录的 `Azure.vrm` 在源码运行时能被解析到；缺失文件返回空串而不是乱猜。
## 运行：godot --headless --path game --script res://tests/test_avatar_path.gd

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
	print("[avatar] VRM 路径解析自检（绝对 / 相对候选 / 工程根）")
	var avatar := preload("res://scripts/ui/AzureAvatar.gd")

	# —— ① 绝对路径：原样一个候选，不拼接任何目录 ——
	var abs_path := ProjectSettings.globalize_path("res://").path_join("__no_such__.vrm")
	var abs_cand := avatar.vrm_candidates(abs_path)
	_check("① 绝对路径只返回自身", abs_cand.size() == 1 and abs_cand[0] == abs_path)
	_check("① 不存在的绝对路径解析为空（不乱猜）", avatar.resolve_vrm_path(abs_path) == "")

	# —— ② 相对路径：候选顺序 exe 同目录 → res:// → 工程根（cwd 可能与 res 重复，去重） ——
	var cand := avatar.vrm_candidates("Azure.vrm")
	_check("② 相对路径至少有 3 个候选", cand.size() >= 3)
	var exe_dir := OS.get_executable_path().get_base_dir()
	var res_dir := ProjectSettings.globalize_path("res://")
	_check("② 第一个候选是 exe 同目录", cand[0] == exe_dir.path_join("Azure.vrm"))
	_check("② 候选里包含 res:// 与工程根（game/ 的上一级）",
			cand.has(res_dir.path_join("Azure.vrm"))
			and cand.has(res_dir.trim_suffix("/").get_base_dir().path_join("Azure.vrm")))
	_check("② 候选去重（没有重复项）", _unique(cand))

	# —— ③ 仓库根目录的 Azure.vrm：源码运行时必须能解析到（用户提供的测试 / 打包用模型） ——
	var root_vrm := res_dir.trim_suffix("/").get_base_dir().path_join("Azure.vrm")
	var resolved := avatar.resolve_vrm_path("Azure.vrm")
	if FileAccess.file_exists(root_vrm):
		_check("③ game/../Azure.vrm 存在时能被解析到", resolved == root_vrm)
	else:
		_check("③ 仓库根没有 VRM（跳过解析断言，仅确认不抛）", resolved == "" or FileAccess.file_exists(resolved))

	# —— ④ 空 / 纯空白：没有候选、解析为空 ——
	_check("④ 空路径没有候选", avatar.vrm_candidates("").is_empty())
	_check("④ 纯空白路径没有候选（strip 后为空）", avatar.vrm_candidates("   ").is_empty())

	if _fail == 0:
		print("[avatar] PASS：", _total, " 项断言全部通过")
	else:
		print("[avatar] FAIL：", _total, " 项断言中 ", _fail, " 项失败")
	quit(0 if _fail == 0 else 1)

func _unique(arr: PackedStringArray) -> bool:
	var seen := {}
	for v in arr:
		if seen.has(v):
			return false
		seen[v] = true
	return true
