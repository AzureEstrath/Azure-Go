extends Node3D
## 主场景：3D 棋盘 + 右侧聊天面板，接线全部服务
## 调试：`-- --shot=输出.png` 可在渲染数帧后自动截图并退出

const PANEL_W := 380
const TEAL := Color(0.306, 0.804, 0.769)
const TEAL_DARK := Color(0.173, 0.620, 0.620)
const BUBBLE_AI := Color(0.878, 0.957, 0.957)
const BUBBLE_SYS := Color(1.0, 0.953, 0.804)

var cfg: AzureConfig
var katago: KataGoEngine
var llm: LLMClient
var agent: AzureAgent
var board: Board3D
var ambience: Ambience
var avatar: AzureAvatar
var tts: TTSClient

var _left_panel: PanelContainer            # 左侧：语音设置
var _right_panel: PanelContainer           # 右侧：对局 / 聊天
var _left_open := false                    # 左侧默认收起，保持棋盘视野
var _right_open := true
var _left_toggle: Button
var _right_toggle: Button
var _tts_status: Label
var _tts_url_edit: LineEdit
var _tts_model_edit: LineEdit
var _tts_voice_edit: LineEdit
var _tts_voice_opt: OptionButton
var _tts_autostart_chk: CheckBox
var _chk_bf16: CheckBox                    # 右侧栏加速开关
var _chk_short: CheckBox
var _chk_len: CheckBox
var _chk_sync: CheckBox
var _chk_batch: CheckBox                   # 整段合成 / 逐句播报
var _chk_chunk: CheckBox                   # 长句合并（TTS 分块 100/160）
var _pending_ai: Array[Dictionary] = []    # 待显示的 Azure 文字（FIFO；每项 {text, reveal, kind?}）
var _thinking_row: Control = null          # 「Azure 思考中……」占位气泡
var _thinking_label: Label = null
var _speech_hold := false                  # Azure 还有语音没播完：继续锁住棋盘
var _pending_batch := ""                   # 攒起来的连续 Azure 文本：合并成「一整段」一次合成
var _pending_batch_kind := ""              # 这批文本的类型（""=对局发言；"idle"=走神搭话，可整批作废）
var _batch_token := 0                      # 去抖令牌：又来新消息就自增，让旧计时器失效
var _tts_pid := -1                         # 本游戏拉起的语音服务进程（用于开关切换后重启）
var _tts_playing := false                  # 当前是否真有音频在播（区别于「排队中/合成中」）
var _think_dots := 0                       # 「思考中…」省略号动效点数（1~3 循环）
var _think_last_ms := 0                    # 上次刷新省略号的时刻
var _blush_seq := 0                        # 摸头脸红的序号（新的一次摸头会让旧的褪红计时失效）

## Azure 连着冒的多条消息在这么长时间内合并成一段再合成（CPU 合成慢，分开发会句间空很久）
const BATCH_WINDOW := 0.35

var _last_activity_ms := 0                 # 最近一次「玩家有操作」的时刻（闲置计时）
var _idle := false
var _idle_muted := false                   # 玩家已回来（落子/说话）：在途与排队的走神搭话全部作废
var _next_talk_ms := 0                     # 下一次闲置搭话的时刻
var _last_move_count := 0                  # 用于识别 Azure 刚落的那颗子

## 长时间闲置窥屏（本地 PaddleOCR + Qwen2.5-VL，独立 python 进程一跑一停）
const IDLE_PEEK_SEC := 60.0                # 闲置超过这么久才值得窥屏（更短的闲置走普通搭话）
const PEEK_COOLDOWN_MS := 180000           # 两次窥屏至少隔 3 分钟（本地推理很重）
const PEEK_TIMEOUT_MS := 240000            # 窥屏进程超时：放弃，退回普通搭话
var _peek_busy := false
var _peek_started_ms := 0
var _peek_last_ms := 0
var _peek_ctx_activity := 0                # 发起窥屏时的「最近活动时刻」：对不上说明玩家已回来，结果作废
var _peek_out := ""                        # 窥屏结果 json 路径
var _peek_exe := ""                        # 跑窥屏的 python（需装好 paddleocr / llama-cpp-python）
var _peek_script := ""                     # azure_peek.py 路径
var _peek_probed := false
var _peek_ok := false

## 本地语音服务目录：优先「可执行文件同级的 cosyvoice\」（分享版），否则回退 D:/KataGo/cosyvoice（开发机）。
## 用 VBS 以「隐藏窗口的 python.exe」拉起：pythonw 加载 CosyVoice 时会静默崩溃。
var _tts_dir := "D:/KataGo/cosyvoice"
const TTS_PORT := 9880
## 音色只保留能听的女声（用户反馈男声/韩语均不可用）；melo 是轻量引擎
const COSYVOICE_PRESETS := ["中文女", "粤语女", "英文女"]
const CLONE_VOICE := "azure"               # CosyVoice 克隆音色（最像，但 CPU 上慢）
const CLONE_FAST := "azure-fast"           # 快速版：melo 合成 + OpenVoice 音色转换
## 闲置互动：玩家长时间无操作 → 看向玩家 + 歪头 + 偶尔主动搭话
const IDLE_SEC := 12.0
const IDLE_TALK_FIRST := 18.0
const IDLE_TALK_MIN := 45.0
const IDLE_TALK_MAX := 80.0

var _font: Font
var _ui_root: Control
var _chat_box: VBoxContainer
var _chat_scroll: ScrollContainer
var _lbl_moves: Label
var _lbl_wr: Label
var _lbl_score: Label
var _lbl_status: Label
var _chk_pass: CheckBox
var _bad_box: HBoxContainer
var _chat_input: LineEdit
var _buttons: Array[Button] = []
var _shot_path := ""

func _ready() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	_peek_last_ms = -PEEK_COOLDOWN_MS          # 允许开局后第一次长闲置就窥屏
	cfg = AzureConfig.load_config()
	_tts_dir = _find_tts_dir()
	_apply_cli_overrides()
	_font = _make_cjk_font()
	_build_world()
	_build_board()
	_build_ui()
	_build_services()
	_greet()
	_handle_cli()

## 语音服务目录：优先可执行文件同级的 cosyvoice\，否则回退开发机路径
func _find_tts_dir() -> String:
	var exe_dir := OS.get_executable_path().get_base_dir()
	for c in [exe_dir.path_join("cosyvoice"), "D:/KataGo/cosyvoice"]:
		if FileAccess.file_exists(c.path_join("azure_tts_server.py")):
			return c
	return exe_dir.path_join("cosyvoice")

## 命令行覆盖：--llm=http://host:port/v1  --model=模型名  --key=云端API Key（均不写回配置）
func _apply_cli_overrides() -> void:
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--llm="):
			cfg.llm_base_url = a.substr("--llm=".length())
		elif a.begins_with("--model="):
			cfg.llm_model = a.substr("--model=".length())
		elif a.begins_with("--key="):
			cfg.llm_api_key = a.substr("--key=".length())

# ================= 世界与棋盘 =================

func _make_cjk_font() -> Font:
	var f := SystemFont.new()
	f.font_names = PackedStringArray([
		"Microsoft YaHei UI", "Microsoft YaHei", "SimHei",
		"Noto Sans CJK SC", "PingFang SC", "sans-serif",
	])
	# 让 ♟️💡📊 等 emoji 也能显示
	var emoji := SystemFont.new()
	emoji.font_names = PackedStringArray(["Segoe UI Emoji", "Noto Color Emoji"])
	f.fallbacks = [emoji]
	return f

func _build_world() -> void:
	var we := WorldEnvironment.new()
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color(0.910, 0.957, 0.957)
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color(0.85, 0.89, 0.91)
	env.ambient_light_energy = 0.95
	we.environment = env
	add_child(we)

	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-55, -38, 0)
	light.light_energy = 1.2
	light.shadow_enabled = true
	add_child(light)

	# 环境：地面 + 风场（粒子与化身摆动共用同一风）
	ambience = Ambience.new()
	add_child(ambience)
	avatar = AzureAvatar.new()
	avatar.vrm_path = cfg.vrm_path
	avatar.wind_source = ambience
	add_child(avatar)

func _build_board() -> void:
	board = Board3D.new()
	add_child(board)
	board.setup_font(_font)
	board.intersection_clicked.connect(_on_intersection_clicked)
	board.head_patted.connect(_on_head_patted)
	if avatar != null:
		avatar.gaze_source = board          # 化身视线：旋转视角时看玩家，平时看鼠标落点
		board.pat_head = avatar             # 摸头触发区：左键点在头部附近即可摸头

# ================= 界面 =================

func _style(bg: Color, radius := 10, pad := 10) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.set_corner_radius_all(radius)
	sb.content_margin_left = pad
	sb.content_margin_right = pad
	sb.content_margin_top = int(pad * 0.7)
	sb.content_margin_bottom = int(pad * 0.7)
	return sb

func _build_ui() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)

	var root := Control.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var th := Theme.new()
	th.default_font = _font
	th.default_font_size = 14
	root.theme = th
	layer.add_child(root)
	_ui_root = root

	_build_right_panel(root)
	_build_left_panel(root)
	_build_toggles(root)
	_apply_panel_layout(false)

## 右侧：对局信息 / 聊天 / 功能按钮 / 俗手回档
func _build_right_panel(root: Control) -> void:
	var panel := PanelContainer.new()
	_right_panel = panel
	panel.anchor_left = 1.0
	panel.anchor_right = 1.0
	panel.anchor_top = 0.0
	panel.anchor_bottom = 1.0
	panel.offset_left = -PANEL_W
	panel.offset_right = 0
	panel.add_theme_stylebox_override("panel", _style(Color(1, 1, 1), 0, 12))
	root.add_child(panel)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	panel.add_child(vb)

	# 标题
	var head := PanelContainer.new()
	head.add_theme_stylebox_override("panel", _style(TEAL_DARK, 8, 12))
	vb.add_child(head)
	var hv := VBoxContainer.new()
	head.add_child(hv)
	var t1 := Label.new()
	t1.text = "♟️ Azure · 围棋陪练"
	t1.add_theme_font_size_override("font_size", 18)
	t1.add_theme_color_override("font_color", Color.WHITE)
	hv.add_child(t1)
	var t2 := Label.new()
	t2.text = "青蓝色 · 永恒17岁 · 天才少女 · 右键拖拽旋转视角 · 滚轮缩放"
	t2.add_theme_font_size_override("font_size", 11)
	t2.add_theme_color_override("font_color", Color(1, 1, 1, 0.85))
	t2.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hv.add_child(t2)

	# 信息栏
	var info := HBoxContainer.new()
	info.add_theme_constant_override("separation", 10)
	vb.add_child(info)
	_lbl_moves = Label.new()
	_lbl_moves.text = "手数: 0"
	info.add_child(_lbl_moves)
	_lbl_wr = Label.new()
	_lbl_wr.text = "黑胜率: --"
	_lbl_wr.add_theme_color_override("font_color", TEAL_DARK)
	info.add_child(_lbl_wr)
	_lbl_score = Label.new()
	_lbl_score.text = "目差: --"
	_lbl_score.add_theme_color_override("font_color", Color(0.153, 0.682, 0.376))
	info.add_child(_lbl_score)
	_chk_pass = CheckBox.new()
	_chk_pass.text = "允许Azure虚着"
	_chk_pass.toggled.connect(_on_allow_pass_toggled)
	_style_check(_chk_pass, "允许 Azure 虚着")
	info.add_child(_chk_pass)

	# 加速开关（bf16 / 短参考需重启语音服务；短回复、同步说话立即生效）
	var acc := GridContainer.new()
	acc.columns = 2
	acc.add_theme_constant_override("h_separation", 12)
	vb.add_child(acc)
	_chk_bf16 = _acc_check(acc, "bf16 加速", cfg.tts_bf16, "bf16",
		"把 CosyVoice 的 LLM 权重降到 bfloat16：约快 20~28%，个别词可能读错（需重启语音服务）")
	_chk_short = _acc_check(acc, "短参考", cfg.tts_short_prompt, "short",
		"用 ~2.7s 短参考替代 6.3s：实测总耗时基本不变，语速会变慢、音色略变（需重启语音服务）")
	_chk_len = _acc_check(acc, "短回复", cfg.tts_limit_len, "len",
		"限制 Azure 每次只说 1~2 句：语音等待时间随字数下降（立即生效）")
	_chk_sync = _acc_check(acc, "同步说话", cfg.tts_sync_speak, "sync",
		"开启：Azure 的文字等语音就绪后一起显示（消除文字先出、声音晚到的割裂感）；关闭：文字立即显示")
	_chk_batch = _acc_check(acc, "整段合成", cfg.tts_batch_speak, "batch",
		"开启：Azure 连发的多条消息合并成一段、连续朗读；关闭：逐句合成，第一句播完等第二句合成（想边听边等选它）")
	_chk_chunk = _acc_check(acc, "长句合并", cfg.tts_long_chunk, "chunk",
		"单次 TTS 合成字数上限：关=100（更快出第一句，默认）；开=160（长句一次合成、但首句更慢）")

	# 聊天区
	_chat_scroll = ScrollContainer.new()
	_chat_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_chat_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	vb.add_child(_chat_scroll)
	_chat_box = VBoxContainer.new()
	_chat_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_chat_box.add_theme_constant_override("separation", 6)
	_chat_scroll.add_child(_chat_box)

	# 输入行
	var inp_row := HBoxContainer.new()
	vb.add_child(inp_row)
	_chat_input = LineEdit.new()
	_chat_input.placeholder_text = "和Azure聊天..."
	_chat_input.max_length = 200
	_chat_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_chat_input.text_submitted.connect(func(_t): _send_chat())
	inp_row.add_child(_chat_input)
	var send := Button.new()
	send.text = "发送"
	send.pressed.connect(_send_chat)
	inp_row.add_child(send)

	# 功能按钮
	var btn_row := HBoxContainer.new()
	btn_row.add_theme_constant_override("separation", 6)
	vb.add_child(btn_row)
	_buttons.append(_button(btn_row, "💡 提示", func():
		_allow_tts_fillers()
		agent.hint()))
	_buttons.append(_button(btn_row, "📊 局面分析", func():
		_allow_tts_fillers()
		agent.analyze_position()))
	_buttons.append(_button(btn_row, "🔄 重来", _ask_reset))
	_buttons.append(_button(btn_row, "🏁 结束", _ask_end))

	# 俗手回档
	var bad_title := Label.new()
	bad_title.text = "⏪ 俗手回档（点一下回到那手之前）"
	bad_title.add_theme_font_size_override("font_size", 12)
	bad_title.add_theme_color_override("font_color", Color(0.627, 0.416, 0.173))
	vb.add_child(bad_title)
	_bad_box = HBoxContainer.new()
	_bad_box.add_theme_constant_override("separation", 6)
	vb.add_child(_bad_box)
	_render_bad_moves([])

	_lbl_status = Label.new()
	_lbl_status.add_theme_font_size_override("font_size", 11)
	_lbl_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(_lbl_status)

## 左侧（与右侧等宽）：本地语音朗读设置
func _build_left_panel(root: Control) -> void:
	var panel := PanelContainer.new()
	_left_panel = panel
	panel.anchor_left = 0.0
	panel.anchor_right = 0.0
	panel.anchor_top = 0.0
	panel.anchor_bottom = 1.0
	panel.offset_left = -PANEL_W
	panel.offset_right = 0
	panel.add_theme_stylebox_override("panel", _style(Color(1, 1, 1), 0, 12))
	root.add_child(panel)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	panel.add_child(vb)

	var head := PanelContainer.new()
	head.add_theme_stylebox_override("panel", _style(TEAL_DARK, 8, 12))
	vb.add_child(head)
	var hv := VBoxContainer.new()
	head.add_child(hv)
	var t1 := Label.new()
	t1.text = "🔊 语音（本地 TTS）"
	t1.add_theme_font_size_override("font_size", 16)
	t1.add_theme_color_override("font_color", Color.WHITE)
	hv.add_child(t1)
	var t2 := Label.new()
	t2.text = "本机自建 CosyVoice 3 等服务 · 全程 localhost · 侧栏收起后依然朗读"
	t2.add_theme_font_size_override("font_size", 11)
	t2.add_theme_color_override("font_color", Color(1, 1, 1, 0.85))
	t2.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hv.add_child(t2)

	var chk := CheckBox.new()
	chk.button_pressed = cfg.tts_enabled
	chk.toggled.connect(_on_tts_enabled)
	_style_check(chk, "朗读 Azure 的话")
	vb.add_child(chk)

	vb.add_child(_field_label("朗读方式"))
	var opt := OptionButton.new()
	opt.fit_to_longest_item = false                  # 不让长选项文字撑宽面板（保证左右侧栏等宽）
	opt.add_item("系统语音", 0)
	opt.add_item("CosyVoice 表单接口", 1)
	opt.add_item("CosyVoice OpenAI 接口", 2)
	opt.selected = maxi(0, ["system", "openai", "cosyvoice"].find(cfg.tts_mode))
	opt.item_selected.connect(_on_tts_mode)
	vb.add_child(opt)

	vb.add_child(_field_label("服务地址（上面第 2/3 项才需要）"))
	var le_url := LineEdit.new()
	le_url.text = cfg.tts_base_url
	le_url.tooltip_text = "例如 http://127.0.0.1:9880/v1"
	le_url.text_submitted.connect(func(_t): _apply_tts_fields())
	le_url.focus_exited.connect(_apply_tts_fields)
	vb.add_child(le_url)
	_tts_url_edit = le_url

	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	vb.add_child(row)
	var col1 := VBoxContainer.new()
	col1.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(col1)
	col1.add_child(_field_label("模型"))
	_tts_model_edit = LineEdit.new()
	_tts_model_edit.text = cfg.tts_model
	col1.add_child(_tts_model_edit)
	var col2 := VBoxContainer.new()
	col2.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(col2)
	col2.add_child(_field_label("音色（可下拉选）"))
	_tts_voice_opt = OptionButton.new()
	_tts_voice_opt.fit_to_longest_item = false       # 同上：避免被最长音色名撑宽
	_fill_voice_items([])
	col2.add_child(_tts_voice_opt)
	_tts_voice_opt.item_selected.connect(_on_voice_selected)

	var vrow := HBoxContainer.new()
	vrow.add_theme_constant_override("separation", 6)
	vb.add_child(vrow)
	var b_refresh := Button.new()
	b_refresh.text = "刷新音色列表"
	b_refresh.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	b_refresh.pressed.connect(_refresh_voices)
	vrow.add_child(b_refresh)
	_tts_voice_edit = LineEdit.new()             # 也可手动输入音色 id
	_tts_voice_edit.text = cfg.tts_voice
	_tts_voice_edit.tooltip_text = "手动输入音色 id（如 melo / 中文女）"
	vrow.add_child(_tts_voice_edit)

	var chk_auto := CheckBox.new()
	chk_auto.button_pressed = cfg.tts_auto_start
	chk_auto.toggled.connect(func(v): cfg.tts_auto_start = v; cfg.save())
	_tts_autostart_chk = chk_auto
	_style_check(chk_auto, "启动游戏时自动拉起语音服务")
	vb.add_child(chk_auto)

	vb.add_child(_field_label("语速"))
	var sp := HSlider.new()
	sp.min_value = 0.5
	sp.max_value = 2.0
	sp.step = 0.05
	sp.value = cfg.tts_speed
	sp.custom_minimum_size = Vector2(0, 18)
	sp.value_changed.connect(func(v): _on_tts_speed(v, false))
	sp.drag_ended.connect(func(_c): _on_tts_speed(sp.value, true))
	vb.add_child(sp)

	vb.add_child(_field_label("音调（播放层变调，会略带动语速）"))
	var pt := HSlider.new()
	pt.min_value = 0.8
	pt.max_value = 1.3
	pt.step = 0.01
	pt.value = cfg.tts_pitch
	pt.custom_minimum_size = Vector2(0, 18)
	pt.value_changed.connect(func(v): _on_tts_pitch(v, false))
	pt.drag_ended.connect(func(_c): _on_tts_pitch(pt.value, true))
	vb.add_child(pt)

	vb.add_child(_field_label("音量"))
	var vol := HSlider.new()
	vol.min_value = 0.0
	vol.max_value = 1.5
	vol.step = 0.05
	vol.value = cfg.tts_volume
	vol.custom_minimum_size = Vector2(0, 18)
	vol.value_changed.connect(func(v): _on_tts_volume(v, false))
	vol.drag_ended.connect(func(_c): _on_tts_volume(vol.value, true))
	vb.add_child(vol)

	var btn_row := HBoxContainer.new()
	btn_row.add_theme_constant_override("separation", 6)
	vb.add_child(btn_row)
	var b_test := Button.new()
	b_test.text = "▶ 试听"
	b_test.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	b_test.pressed.connect(func():
		if tts != null:
			tts.preview("嗯~ 我是 Azure，很高兴见到你，请多指教。"))
	btn_row.add_child(b_test)
	var b_stop := Button.new()
	b_stop.text = "■ 停止"
	b_stop.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	b_stop.pressed.connect(func():
		if tts != null:
			tts.stop())
	btn_row.add_child(b_stop)

	_tts_status = Label.new()
	_tts_status.text = "未开启朗读"
	_tts_status.add_theme_font_size_override("font_size", 11)
	_tts_status.add_theme_color_override("font_color", Color(0.45, 0.45, 0.45))
	_tts_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(_tts_status)

	var tip := Label.new()
	tip.text = "本机已部署本地语音（都在 D:\\KataGo\\cosyvoice）：\n" \
		+ "· 音色：azure-fast（你的克隆音色·快速版，推荐）/ azure（CosyVoice 原版，较慢）/ melo / 中文女 / 粤语女 / 英文女\n" \
		+ "· 换引擎音色后需重启语音服务；零配置兜底：上面第 1 项「Windows 系统语音」"
	tip.add_theme_font_size_override("font_size", 10)
	tip.add_theme_color_override("font_color", Color(0.6, 0.6, 0.6))
	tip.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(tip)

func _field_label(text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.add_theme_font_size_override("font_size", 11)
	l.add_theme_color_override("font_color", Color(0.35, 0.45, 0.45))
	return l

## 统一勾选项样式：面板是白底，默认主题的浅色字/浅色勾选框几乎看不见 → 全部改成深色
func _style_check(chk: CheckBox, txt := "") -> void:
	if txt != "":
		chk.text = txt
	var dark := Color(0.13, 0.30, 0.30)
	chk.add_theme_font_size_override("font_size", 12)
	for k in ["font_color", "font_hover_color", "font_pressed_color", "font_focus_color"]:
		chk.add_theme_color_override(k, dark)
	for k in ["icon_normal_color", "icon_hover_color", "icon_pressed_color", "icon_focus_color"]:
		chk.add_theme_color_override(k, dark)

## 右侧栏「加速开关」：建一个并统一样式
func _acc_check(parent: Control, txt: String, pressed: bool, key: String, tip: String) -> CheckBox:
	var c := CheckBox.new()
	c.button_pressed = pressed
	c.tooltip_text = tip
	c.toggled.connect(_on_acc_toggled.bind(key))
	parent.add_child(c)
	_style_check(c, txt)
	return c

## 两侧收起/展开开关（贴在画面最边缘，收起后仍可点开）
func _build_toggles(root: Control) -> void:
	_right_toggle = Button.new()
	_right_toggle.anchor_left = 1.0
	_right_toggle.anchor_right = 1.0
	_right_toggle.offset_left = -32
	_right_toggle.offset_right = -6
	_right_toggle.offset_top = 10
	_right_toggle.offset_bottom = 40
	_right_toggle.tooltip_text = "收起 / 展开右侧面板"
	_right_toggle.pressed.connect(func(): _set_panel_open("right", not _right_open))
	root.add_child(_right_toggle)

	_left_toggle = Button.new()
	_left_toggle.anchor_left = 0.0
	_left_toggle.anchor_right = 0.0
	_left_toggle.offset_left = 6
	_left_toggle.offset_right = 32
	_left_toggle.offset_top = 10
	_left_toggle.offset_bottom = 40
	_left_toggle.tooltip_text = "收起 / 展开语音设置"
	_left_toggle.pressed.connect(func(): _set_panel_open("left", not _left_open))
	root.add_child(_left_toggle)

func _set_panel_open(which: String, open: bool) -> void:
	if which == "left":
		_left_open = open
	else:
		_right_open = open
	_apply_panel_layout(true)

## 侧栏位置：展开时贴边显示，收起时整体滑出画面；棋盘取景按两侧占用自动让位
func _apply_panel_layout(animated := true) -> void:
	var rl := -float(PANEL_W) if _right_open else 0.0
	var rr := 0.0 if _right_open else float(PANEL_W)
	var ll := 0.0 if _left_open else -float(PANEL_W)
	var lr := float(PANEL_W) if _left_open else 0.0
	if animated:
		var tw := create_tween().set_parallel(true)
		tw.tween_property(_right_panel, "offset_left", rl, 0.18)
		tw.tween_property(_right_panel, "offset_right", rr, 0.18)
		tw.tween_property(_left_panel, "offset_left", ll, 0.18)
		tw.tween_property(_left_panel, "offset_right", lr, 0.18)
	else:
		_right_panel.offset_left = rl
		_right_panel.offset_right = rr
		_left_panel.offset_left = ll
		_left_panel.offset_right = lr
	_right_toggle.text = "▶" if _right_open else "◀"
	_left_toggle.text = "◀" if _left_open else "▶"
	if board != null:
		board.set_panels(_left_open, _right_open)

func _on_tts_enabled(v: bool) -> void:
	cfg.tts_enabled = v
	if tts != null:
		tts.enabled = v
		if not v:
			tts.stop()
	_tts_status.text = ("已开启朗读 · 本地服务 %s" % cfg.tts_base_url) if v else "未开启朗读"
	cfg.save()

func _on_tts_mode(i: int) -> void:
	var m: String = ["system", "openai", "cosyvoice"][clampi(i, 0, 2)]
	if m == cfg.tts_mode:
		return                       # 程序化赋值也可能触发该信号：值没变就不写配置
	cfg.tts_mode = m
	if tts != null:
		tts.mode = m
		tts.clear_fallback()          # 用户手动改设置：取消「服务就绪后自动切回」状态
		tts.stop()
	cfg.save()
	_tts_status.text = "朗读方式：%s" % m

func _apply_tts_fields() -> void:
	var url := _tts_url_edit.text.strip_edges()
	var model := _tts_model_edit.text.strip_edges()
	var voice := _tts_voice_edit.text.strip_edges()
	if url == cfg.tts_base_url and model == cfg.tts_model and voice == cfg.tts_voice:
		return                       # 仅在用户真正改动时落盘，避免启动时把旧值写回去
	cfg.tts_base_url = url
	cfg.tts_model = model
	cfg.tts_voice = voice
	if tts != null:
		tts.base_url = url
		tts.model = model
		tts.voice = voice
	cfg.save()
	_tts_status.text = "语音设置已保存"

## 音色下拉：只保留能听的女声（melo / 中文女 / 粤语女 / 英文女）；服务端返回的男声与克隆音色统一过滤
func _fill_voice_items(from_server: Array) -> void:
	if _tts_voice_opt == null:
		return
	if _is_removed_voice(cfg.tts_voice):                # 旧配置里的男声/克隆音色 → 迁到中文女
		cfg.tts_voice = "中文女"
		if _tts_voice_edit != null:
			_tts_voice_edit.text = cfg.tts_voice
		if tts != null:
			tts.voice = cfg.tts_voice
		cfg.save()
	var items: Array = [
		[CLONE_FAST, "Azure 快速版"],
		[CLONE_VOICE, "Azure 克隆（最像）"],
		["melo", "MeloTTS"],
	]
	for p in COSYVOICE_PRESETS:
		items.append([p, "%s（预设）" % p])
	for v in from_server:
		if typeof(v) != TYPE_DICTIONARY:
			continue
		var vid := str((v as Dictionary).get("id", ""))
		var eng := str((v as Dictionary).get("engine", ""))
		if eng == "sherpa" and tts != null:
			tts.fast_voices[vid] = true          # 轻量音色：客户端不必按句拆分/给超长超时
		# 允许：melo、克隆音色、预设女声，以及服务端注册的所有轻量女声（engine=sherpa，如少女/元气音色）
		var allowed := vid == "melo" or vid == CLONE_VOICE or vid.ends_with("-fast") \
			or COSYVOICE_PRESETS.has(vid) or eng == "sherpa"
		if not allowed:
			continue                                    # 不再展示男声 / 旧克隆音色
		var dup := false
		for it in items:
			if str(it[0]) == vid:
				dup = true
				break
		if not dup:
			items.append([vid, str((v as Dictionary).get("label", vid))])
	_tts_voice_opt.clear()
	for it in items:
		_tts_voice_opt.add_item(str(it[1]))
		_tts_voice_opt.set_item_metadata(_tts_voice_opt.item_count - 1, str(it[0]))
	for i in _tts_voice_opt.item_count:
		if str(_tts_voice_opt.get_item_metadata(i)) == cfg.tts_voice:
			_tts_voice_opt.select(i)
			break

## 已移除的音色（男声 / 韩语女 / 旧克隆音色）；azure 是新的克隆音色，保留
static func _is_removed_voice(v: String) -> bool:
	return v in ["中文男", "日语男", "英文男", "韩语女", "azure-soft", "azure-brisk", "azure-calm"]

func _on_voice_selected(i: int) -> void:
	var vid := str(_tts_voice_opt.get_item_metadata(i))
	if vid == "" or vid == cfg.tts_voice:
		return
	cfg.tts_voice = vid
	_tts_voice_edit.text = vid
	if tts != null:
		tts.voice = vid
		tts.stop()
	cfg.save()
	_tts_status.text = "音色已切换：%s（换引擎音色时需重启语音服务）" % vid

## 从服务端 /voices 拉取音色清单
func _refresh_voices() -> void:
	var url := _health_url().replace("/health", "/voices")
	var req := HTTPRequest.new()
	add_child(req)
	if req.request(url) != OK:
		req.queue_free()
		_tts_status.text = "无法连接语音服务（%s）" % url
		return
	var res: Array = await req.request_completed
	req.queue_free()
	var parsed = JSON.parse_string((res[3] as PackedByteArray).get_string_from_utf8())
	var list: Array = []
	if typeof(parsed) == TYPE_DICTIONARY:
		list = (parsed as Dictionary).get("voices", [])
	_fill_voice_items(list)
	_tts_status.text = "音色列表已刷新（%d 项）" % _tts_voice_opt.item_count

func _health_url() -> String:
	var b := cfg.tts_base_url.strip_edges().rstrip("/")
	if b.ends_with("/v1"):
		b = b.substr(0, b.length() - 3)
	return b + "/health"

## 是否已有语音服务实例在启动或运行：读服务端写的实例锁（pid），再用进程存活判断。
## 模型加载期间端口未监听、/health 必然失败，只靠健康检查会误判成「没在跑」而重复拉起。
func _service_started_or_starting() -> bool:
	var lock := _tts_dir.path_join(".tts_server.lock")
	if not FileAccess.file_exists(lock):
		return false
	var f := FileAccess.open(lock, FileAccess.READ)
	if f == null:
		return false
	var pid := int(f.get_as_text().strip_edges())
	f.close()
	return pid > 0 and OS.is_process_running(pid)

## 启动游戏时自动拉起本地语音服务（pythonw，后台无窗口）
func _autostart_tts() -> void:
	if not cfg.tts_auto_start or not cfg.tts_enabled or cfg.tts_mode == "system":
		return
	var py := _tts_dir.path_join(".venv/Scripts/python.exe")
	if not FileAccess.file_exists(py):
		_tts_status.text = "未找到本地语音环境（缺 %s）" % py
		return
	var probe := HTTPRequest.new()
	add_child(probe)
	probe.timeout = 3.0
	if probe.request(_health_url()) == OK:
		var res: Array = await probe.request_completed
		probe.queue_free()
		if int(res[0]) == HTTPRequest.RESULT_SUCCESS and int(res[1]) > 0:
			_tts_status.text = "已连接本地语音服务（%s）" % cfg.tts_base_url
			_refresh_voices()
			return
	else:
		probe.queue_free()
	# 服务加载模型（1~2 分钟）期间端口还没监听，健康检查必然失败：
	# 先看有没有实例正在启动/运行，避免重复拉起——两个实例互抢 CPU 会让「启动」看起来一直失败
	if _service_started_or_starting():
		print("[TTS] 已有语音服务实例在启动/运行，等待它就绪（不重复拉起）")
		_tts_status.text = "语音服务已在启动（加载模型约 1~2 分钟），等它就绪…"
	else:
		_spawn_tts_server()
	for i in 160:                                 # 等就绪（双模型加载较慢，最多约 4 分钟）
		await get_tree().create_timer(1.5).timeout
		var p2 := HTTPRequest.new()
		add_child(p2)
		p2.timeout = 3.0
		if p2.request(_health_url()) == OK:
			var r2: Array = await p2.request_completed
			p2.queue_free()
			if int(r2[0]) == HTTPRequest.RESULT_SUCCESS and int(r2[1]) > 0:
				_tts_status.text = "本地语音服务已就绪（%s）" % cfg.tts_base_url
				_refresh_voices()
				return
		else:
			p2.queue_free()
	_tts_status.text = "本地语音服务仍在加载，稍后自动可用（日志：cosyvoice/server_run.log）"

func _spawn_tts_server() -> void:
	var voice := cfg.tts_voice.strip_edges()
	var low := voice.to_lower()
	var want_cosy := low != "melo" and low != "sherpa" and low != "zh_en"
	var launcher := _tts_dir.path_join("run_hidden.vbs")
	var py := _tts_dir.path_join(".venv/Scripts/python.exe")
	var script := _tts_dir.path_join("azure_tts_server.py")
	var args := PackedStringArray([launcher, py, script, "--port", str(TTS_PORT),
		"--threads", "12", "--cosyvoice", ("1" if want_cosy else "0")])
	if want_cosy:
		# 克隆音色要 0.5B 模型、预设女声要 SFT 模型：两个都加载，按音色自动路由
		args.append_array(["--model_dir", "pretrained_models/Fun-CosyVoice3-0.5B",
			"--sft_dir", "pretrained_models/CosyVoice-300M-SFT"])
		if cfg.tts_bf16:
			args.append_array(["--bf16", "1"])
		if cfg.tts_short_prompt:
			args.append_array(["--prompt", "short"])
		print("[TTS] 后台启动本地语音服务（隐藏窗口）：音色=%s bf16=%s 短参考=%s"
			% [voice, cfg.tts_bf16, cfg.tts_short_prompt])
	var pid := OS.create_process("wscript.exe", args)
	if pid > 0:
		_tts_pid = pid
		_tts_status.text = "已后台启动本地语音服务（pid=%d），正在加载模型…" % pid
	else:
		_tts_status.text = "本地语音服务启动失败（%s）" % launcher

## 右侧栏加速开关：立即存盘；需要服务重启的（bf16/短参考）顺手重启本游戏拉起的服务
func _on_acc_toggled(on: bool, which: String) -> void:
	match which:
		"bf16":
			cfg.tts_bf16 = on
		"short":
			cfg.tts_short_prompt = on
		"len":
			cfg.tts_limit_len = on
			if agent != null:
				agent.limit_len = on
		"sync":
			cfg.tts_sync_speak = on
		"batch":
			cfg.tts_batch_speak = on
			# 切换时先把两种模式各自积压的内容放出来，避免丢字 / 乱序
			if _pending_batch != "":
				_batch_token += 1
				_release_batch(true)
			elif not _pending_ai.is_empty():
				_flush_pending_ai()
		"chunk":
			cfg.tts_long_chunk = on
			if tts != null:
				tts.slow_chunk_chars = 160 if on else 100
	cfg.save()
	if which == "len" or which == "sync" or which == "batch" or which == "chunk":
		var nm := "短回复" if which == "len" else ("同步说话" if which == "sync" else ("整段合成" if which == "batch" else "长句合并"))
		_tts_status.text = "%s：%s（立即生效）" % [nm, "开" if on else "关"]
		_set_status("语音：%s已%s" % [nm, "开启" if on else "关闭"])
		return
	_tts_status.text = "已保存：%s（重启语音服务中…）" % ("开" if on else "关")
	_restart_tts_server()

func _restart_tts_server() -> void:
	if _tts_pid > 0 and OS.is_process_running(_tts_pid):
		OS.kill(_tts_pid)
		_tts_pid = -1
		await get_tree().create_timer(1.0).timeout
		_spawn_tts_server()
		for i in 200:                              # 等模型重新加载（约 2 分钟）
			await get_tree().create_timer(1.5).timeout
			var p := HTTPRequest.new()
			add_child(p)
			p.timeout = 3.0
			if p.request(_health_url()) == OK:
				var r: Array = await p.request_completed
				p.queue_free()
				if int(r[0]) == HTTPRequest.RESULT_SUCCESS and int(r[1]) > 0:
					_tts_status.text = "语音服务已按新开关重启完成"
					return
			else:
				p.queue_free()
		_tts_status.text = "语音服务重启中，稍后自动可用"
	else:
		_tts_status.text = "开关已保存；当前语音服务不是本游戏拉起的，请手动重启它（或重启游戏）后生效"

func _on_tts_speed(v: float, persist: bool) -> void:
	cfg.tts_speed = v
	if tts != null:
		tts.speed = v
	if persist:
		cfg.save()

func _on_tts_pitch(v: float, persist: bool) -> void:
	cfg.tts_pitch = v
	if tts != null:
		tts.pitch = v
	if persist:
		cfg.save()

func _on_tts_volume(v: float, persist: bool) -> void:
	cfg.tts_volume = v
	if tts != null:
		tts.volume = v
	if persist:
		cfg.save()

func _button(parent: Node, text: String, cb: Callable) -> Button:
	var b := Button.new()
	b.text = text
	b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	b.pressed.connect(cb)
	parent.add_child(b)
	return b

func _add_msg(kind: String, text: String) -> void:
	var is_ai := kind.begins_with("ai")
	var reveal := kind == "ai_move"          # 这条是「Azure 落子宣告」：轮到她这段语音时再落子
	if kind == "ai_idle" and _idle_muted:
		return                               # 玩家已经回来继续下棋：这句走神搭话作废（不出声也不显示）
	if is_ai and tts != null and tts.enabled:
		var tts_kind := "idle" if kind == "ai_idle" else ""      # 走神搭话可被整类取消（cancel_kind("idle")）
		if cfg.tts_batch_speak:
			# 整段合成：把连着冒出来的多条消息攒成「一整段」再一次合成，连续朗读、无句间空白
			if _pending_batch != "" and _pending_batch_kind != tts_kind:
				_batch_token += 1
				_release_batch(true)         # 批次按类型分组：走神搭话不跟对局发言混进同一段语音
			_pending_ai.append({"text": text, "reveal": reveal, "kind": tts_kind})
			_pending_batch = _join_batch(_pending_batch, text)
			_pending_batch_kind = tts_kind
			_batch_token += 1
			var token := _batch_token
			get_tree().create_timer(BATCH_WINDOW).timeout.connect(func(): _flush_batch(token))
		else:
			# 逐句播报：每条各自合成；轮到它开始播放时再显示文字（见 _on_utterance_started）
			# 先入队再合成：拿 speak() 的分组号，保证「一条消息 ⇄ 一次 utterance_started」严格对应
			if cfg.tts_sync_speak:
				_pending_ai.append({"text": text, "reveal": reveal, "group": -1, "kind": tts_kind})
				_pending_ai[_pending_ai.size() - 1]["group"] = tts.speak(text, tts_kind)
			else:
				_add_msg_now("ai", text)
				if reveal:
					_reveal_move()
				tts.speak(text, tts_kind)
		return
	if is_ai:
		_add_msg_now("ai", text)                 # 未启用语音：直接显示
		if reveal:
			_reveal_move()
		return
	# 非 Azure 消息（玩家输入 / 系统提示）：先把积压的 Azure 内容放出来，保证聊天顺序不乱
	if cfg.tts_batch_speak:
		if _pending_batch != "":
			_batch_token += 1
			_release_batch(true)
	elif not _pending_ai.is_empty():
		_flush_pending_ai()
	_add_msg_now(kind, text)

## 把新一句并进攒着的文本：不用换行（CosyVoice 会把 \n 当分句甚至丢掉），按标点补齐句读
static func _join_batch(cur: String, add: String) -> String:
	var a := add.strip_edges()
	if a == "":
		return cur
	if cur == "":
		return a
	var tail := cur.substr(cur.length() - 1, 1)
	if ["。", "！", "？", "…", "；", "，", ",", ".", "!", "?", ";", "、"].has(tail):
		return cur + a
	return cur + "。" + a

## 去抖计时器到点：只有最后一个令牌有效（期间又来了新消息就作废）
func _flush_batch(token: int) -> void:
	if token != _batch_token:
		return
	_release_batch(false)

## 把攒着的 Azure 文本作为一整段处理：送一次合成，并按「同步说话」开关决定文字何时显示
func _release_batch(force_show: bool) -> void:
	var text := _pending_batch
	var tts_kind := _pending_batch_kind
	_pending_batch = ""
	_pending_batch_kind = ""
	if text == "":
		return
	if tts == null or not tts.enabled:
		_flush_pending_ai()
		return
	if cfg.tts_sync_speak and not force_show and not tts.is_speaking():
		tts.speak(text, tts_kind)       # 文字等语音开始播放时（_on_tts_speaking）与声音一起出现
	else:
		_flush_pending_ai()             # 先显示文字（已在说话 / 需要保序）
		tts.speak(text, tts_kind)

func _add_msg_now(kind: String, text: String) -> void:
	# 「Azure 思考中……」占位：第一条真实 Azure 文字原地替换它
	if kind == "ai" and _thinking_label != null:
		_thinking_label.text = text
		_thinking_label = null
		_thinking_row = null
		_scroll_to_bottom()
		return
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_theme_constant_override("separation", 0)
	var bubble := PanelContainer.new()
	if kind == "user":
		bubble.add_theme_stylebox_override("panel", _style(TEAL, 10, 10))
		row.alignment = BoxContainer.ALIGNMENT_END
	elif kind == "system":
		bubble.add_theme_stylebox_override("panel", _style(BUBBLE_SYS, 10, 10))
		row.alignment = BoxContainer.ALIGNMENT_BEGIN
	else:
		bubble.add_theme_stylebox_override("panel", _style(BUBBLE_AI, 10, 10))
		row.alignment = BoxContainer.ALIGNMENT_BEGIN
	row.add_child(bubble)

	var inner := VBoxContainer.new()
	inner.add_theme_constant_override("separation", 2)
	bubble.add_child(inner)
	if kind == "ai":
		var who := Label.new()
		who.text = "Azure"
		who.add_theme_font_size_override("font_size", 10)
		who.add_theme_color_override("font_color", Color(0.6, 0.6, 0.6))
		inner.add_child(who)
	var lbl := Label.new()
	lbl.text = text
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color",
		Color.WHITE if kind == "user" else Color(0.106, 0.337, 0.337))
	inner.add_child(lbl)

	_chat_box.add_child(row)
	_scroll_to_bottom()

## 语音就绪（开始播放）时，把等待中的 Azure 文字一起显示出来
func _flush_pending_ai() -> void:
	if _pending_ai.is_empty():
		return
	for e in _pending_ai:
		_add_msg_now("ai", str((e as Dictionary).get("text", "")))
		if bool((e as Dictionary).get("reveal", false)):
			_reveal_move()
	_pending_ai.clear()

## 显示「Azure 思考中……」占位气泡（等第一条真实 Azure 文字到来时原地替换）
func _show_thinking() -> void:
	if _thinking_label != null or _chat_box == null:
		return
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var bubble := PanelContainer.new()
	bubble.add_theme_stylebox_override("panel", _style(BUBBLE_AI, 10, 10))
	row.add_child(bubble)
	var inner := VBoxContainer.new()
	inner.add_theme_constant_override("separation", 2)
	bubble.add_child(inner)
	var who := Label.new()
	who.text = "Azure"
	who.add_theme_font_size_override("font_size", 10)
	who.add_theme_color_override("font_color", Color(0.6, 0.6, 0.6))
	inner.add_child(who)
	var lbl := Label.new()
	lbl.text = "Azure 思考中……"
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color", Color(0.106, 0.337, 0.337))
	inner.add_child(lbl)
	_chat_box.add_child(row)
	_thinking_row = row
	_thinking_label = lbl
	_scroll_to_bottom()
	if tts != null:
		tts.play_filler("think")         # 思考时先「嗯……」一声，别让等待无声（冷却由 TTS 内部管）

func _clear_thinking() -> void:
	if _thinking_row != null and is_instance_valid(_thinking_row):
		_thinking_row.queue_free()
	_thinking_row = null
	_thinking_label = null

## 句间合成空隙也要有「思考中…」：下一句正在合成、还没开始播时显示占位，并让省略号动起来
func _update_thinking(now: int) -> void:
	if _thinking_label == null and not _pending_ai.is_empty() and not _tts_playing:
		_show_thinking()                                 # 文字还没轮到显示、也没在播：她在憋下一句
	if _thinking_label == null:
		return
	if now - _think_last_ms >= 450:
		_think_last_ms = now
		_think_dots = _think_dots % 3 + 1
		_thinking_label.text = "Azure 思考中" + "…".repeat(_think_dots)

## 占位气泡只在「还等着、也没在说话」时清掉，避免把马上要被替换的占位提前删掉
func _clear_thinking_if_idle() -> void:
	if _thinking_label == null:
		return
	if not _pending_ai.is_empty() or (tts != null and tts.is_speaking()) or (agent != null and agent.busy):
		return
	_clear_thinking()

## 棋盘锁：Azure 思考中，或她还有话没播完（整段关闭时尤其要等她说完）
func _refresh_lock() -> void:
	var locked := (agent != null and agent.busy) or _speech_hold
	if board != null:
		board.locked = locked
	for b in _buttons:
		b.disabled = locked

## 让 Azure 的那颗子在她这段语音开始时才出现在棋盘上
func _reveal_move() -> void:
	if agent != null:
		agent.reveal_move()

func _scroll_to_bottom() -> void:
	await get_tree().process_frame
	if _chat_scroll != null:
		_chat_scroll.scroll_vertical = 1 << 20

func _render_bad_moves(list: Array) -> void:
	for c in _bad_box.get_children():
		c.queue_free()
	if list.is_empty():
		var l := Label.new()
		l.text = "暂无俗手记录"
		l.add_theme_font_size_override("font_size", 12)
		l.add_theme_color_override("font_color", Color(0.7, 0.7, 0.7))
		_bad_box.add_child(l)
		return
	for b in list:
		var d: Dictionary = b
		var btn := Button.new()
		btn.text = "第%d手 %s" % [int(d["move_no"]), str(d["gtp"])]
		btn.tooltip_text = str(d["reason"])
		btn.add_theme_font_size_override("font_size", 12)
		var idx := int(d["turn_index"])
		btn.pressed.connect(func():
			_allow_tts_fillers()
			agent.undo(idx))
		_bad_box.add_child(btn)

func _set_status(text: String, bad := false) -> void:
	_lbl_status.text = text
	_lbl_status.add_theme_color_override("font_color",
		Color(0.8, 0.2, 0.2) if bad else Color(0.45, 0.45, 0.45))

## 简易模态输入框（用于「重来」确认与总结备注）
func _ask_note(title_text: String, on_ok: Callable) -> void:
	var back := ColorRect.new()
	back.color = Color(0, 0, 0, 0.35)
	back.set_anchors_preset(Control.PRESET_FULL_RECT)
	back.mouse_filter = Control.MOUSE_FILTER_STOP
	_ui_root.add_child(back)

	var box := PanelContainer.new()
	box.anchor_left = 0.5
	box.anchor_right = 0.5
	box.anchor_top = 0.5
	box.anchor_bottom = 0.5
	box.offset_left = -190
	box.offset_right = 190
	box.offset_top = -80
	box.offset_bottom = 80
	box.add_theme_stylebox_override("panel", _style(Color.WHITE, 10, 14))
	back.add_child(box)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	box.add_child(vb)
	var l := Label.new()
	l.text = title_text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(l)
	var le := LineEdit.new()
	le.placeholder_text = "可选备注"
	vb.add_child(le)
	var hb := HBoxContainer.new()
	hb.alignment = BoxContainer.ALIGNMENT_END
	vb.add_child(hb)
	var cancel := Button.new()
	cancel.text = "取消"
	cancel.pressed.connect(func(): back.queue_free())
	hb.add_child(cancel)
	var ok := Button.new()
	ok.text = "确定"
	hb.add_child(ok)
	var finish := func(t: String):
		back.queue_free()
		on_ok.call(t)
	ok.pressed.connect(func(): finish.call(le.text))
	le.text_submitted.connect(func(t): finish.call(t))
	le.grab_focus()

# ================= 服务与接线 =================

func _build_services() -> void:
	katago = KataGoEngine.new()
	katago.max_visits = cfg.max_visits
	add_child(katago)
	if katago.start(cfg.katago_bin, cfg.katago_model, cfg.katago_config):
		_set_status("KataGo 已就绪：%s" % cfg.katago_bin)
	else:
		_set_status("KataGo 启动失败：%s" % katago.last_error(), true)

	llm = LLMClient.new()
	llm.base_url = cfg.llm_base_url
	llm.model = cfg.llm_model
	llm.api_key = cfg.llm_api_key
	add_child(llm)

	tts = TTSClient.new()
	tts.enabled = cfg.tts_enabled
	tts.mode = cfg.tts_mode
	tts.base_url = cfg.tts_base_url
	tts.model = cfg.tts_model
	tts.api_key = cfg.tts_api_key
	tts.voice = cfg.tts_voice
	tts.speed = cfg.tts_speed
	tts.pitch = cfg.tts_pitch
	tts.volume = cfg.tts_volume
	tts.slow_chunk_chars = 160 if cfg.tts_long_chunk else 100
	add_child(tts)
	tts.allow_fillers = false                     # 开场问候/闲置搭话等非玩家触发的语音不插语气词，等真正对局互动再放行
	tts.status.connect(_on_tts_status)
	tts.auto_fallback.connect(_on_tts_fallback)
	tts.speaking_changed.connect(_on_tts_speaking)
	tts.utterance_started.connect(_on_utterance_started)
	_autostart_tts()                              # 需要时后台拉起本地语音服务（无窗口）

	AzurePrompts.configure(cfg)                   # 注入 config.json 的自定义人设/称呼/阶段提示
	agent = AzureAgent.new()
	add_child(agent)
	agent.setup(katago, llm)
	agent.limit_len = cfg.tts_limit_len       # 右侧栏「短回复」开关
	agent.message.connect(_add_msg)
	agent.state_changed.connect(_on_state_changed)
	agent.info_changed.connect(_on_info_changed)
	agent.busy_changed.connect(_on_busy_changed)
	agent.emotion.connect(_on_emotion)
	llm.probe_models()
	_warm_up_engine()                             # 后台预热 KataGo：把首次 OpenCL 调优放在开局前完成

## 预热 KataGo：首次运行需 OpenCL 调优（约 1~3 分钟），提前触发，避免玩家第一手时干等
func _warm_up_engine() -> void:
	if katago == null:
		return
	_set_status("KataGo 正在启动（首次运行需调优，约 1~3 分钟）…")
	var r := await katago.analyze([])
	if r.has("error"):
		_set_status("KataGo 启动失败：%s" % str(r["error"]), true)
	else:
		_set_status("KataGo 已就绪")

## 本地语音服务没启动时自动降级到系统语音（只影响本次运行，不改配置）
func _on_tts_fallback(_kind: String) -> void:
	_tts_status.text = "本地语音服务未启动，已临时改用系统语音"
	_set_status("语音：本地服务未启动，已临时改用系统语音（配置里仍是你的选择）")

func _on_emotion(kind: String) -> void:
	if avatar != null:
		avatar.play_expression(kind)
	if tts != null:                                # 预制语气词即时补一句，填补后续合成的等待
		match kind:
			"delight", "laugh":
				tts.play_filler("happy", true)
			"puzzled":
				tts.play_filler("puzzle", true)
			"worried", "sad":
				tts.play_filler("surprise", true)

## 左键点在 Azure 头部附近摸摸头：开心 + 微微脸红 + 一句轻快的语气词
func _on_head_patted() -> void:
	if avatar != null:
		avatar.play_expression("laugh")
		avatar.set_blush(true)
	if tts != null:
		tts.play_filler("pat", true)
	_bump_activity()
	_set_status("你摸了摸 Azure 的头…")
	_blush_seq += 1
	var seq := _blush_seq
	get_tree().create_timer(3.5).timeout.connect(func():
		if seq == _blush_seq and avatar != null:
			avatar.set_blush(false))

func _on_tts_status(text: String) -> void:
	if _tts_status != null:
		_tts_status.text = text
	_set_status("语音：" + text)          # 底部状态行始终可见，左侧栏收起时也能看到
	if not _pending_ai.is_empty() and (text.contains("失败") or text.contains("无响应") or text.contains("无法")):
		_flush_pending_ai()              # 语音出错：文字不能丢，立即显示

func _greet() -> void:
	_add_msg("ai", "嗯~ %s，来下棋吧...我会陪你的 (｡･ω･｡)" % cfg.prompt_user_name)
	_add_msg("system", "你执黑先行。左键落子，右键拖拽可旋转棋盘视角。")

func _on_intersection_clicked(x: int, y: int) -> void:
	_allow_tts_fillers()
	agent.player_move(x, y)

func _on_state_changed(p: Dictionary) -> void:
	board.set_stones(p["stones"])
	var mc := int(p["move_count"])
	if mc > _last_move_count:                       # Azure 每落一子：先注视自己的落点，再交还鼠标跟随
		var stone := _last_stone(p["stones"])
		if not stone.is_empty() and str(stone["c"]) == "white":
			board.look_at_board_point(board.stone_world(int(stone["x"]), int(stone["y"])), 2.0)
	_last_move_count = mc
	_lbl_moves.text = "手数: %d" % mc
	_chk_pass.set_pressed_no_signal(bool(p["allow_pass"]))
	_render_bad_moves(p["bad_moves"])

## payload 里带 last=true 的那颗子（最近一手；pass 时为旧值，不会触发注视）
static func _last_stone(stones: Array) -> Dictionary:
	for s in stones:
		if typeof(s) == TYPE_DICTIONARY and bool((s as Dictionary).get("last", false)):
			return s as Dictionary
	return {}

func _on_info_changed(wr: float, sc: float) -> void:
	_lbl_wr.text = "黑胜率: %.1f%%" % (wr * 100.0)
	_lbl_score.text = "目差: %+.1f" % sc

func _on_busy_changed(busy: bool) -> void:
	if avatar != null:
		avatar.set_thinking(busy)          # 思考中回到中性表情，空闲时恢复常态微笑
	if busy:
		_show_thinking()                   # 聊天栏先占位「Azure 思考中……」
	elif tts != null and tts.is_speaking():
		_speech_hold = true                # Azure 还有话没播完：继续锁住棋盘，等她说完
	else:
		_reveal_move()                     # 兜底：语音不可用/未播时，也要把落子显示出来
	_refresh_lock()
	_clear_thinking_if_idle()

func _on_allow_pass_toggled(pressed: bool) -> void:
	agent.set_allow_pass(pressed)

func _send_chat() -> void:
	var t := _chat_input.text
	if t.strip_edges() == "":
		return
	_allow_tts_fillers()
	_add_msg("user", t)
	_chat_input.text = ""
	agent.chat(t)

## 玩家主动发起对局互动：允许 TTS 在合成等待时插「嗯…」这类语气词。
## 同时把还在合成/排队/播放的走神搭话全部作废——人已经回来下棋了，别再追着问「在忙什么呀」
func _allow_tts_fillers() -> void:
	_cancel_idle_speech()
	if tts != null:
		tts.allow_fillers = true
		tts.begin_filler_turn()          # 本回合语气词限次（think 至多两次，且不连着播同一句）

## 作废走神搭话：文字与语音（在途合成结果、预取段、播放中、排队中）一起清掉，让新的内容立刻顶上
func _cancel_idle_speech() -> void:
	_idle_muted = true
	if _pending_batch_kind == "idle":
		_pending_batch = ""
		_pending_batch_kind = ""
		_batch_token += 1                # 让还在路上的去抖计时器失效
	var kept: Array[Dictionary] = []
	for e in _pending_ai:
		if str((e as Dictionary).get("kind", "")) == "idle":
			continue                     # 还没轮到显示的走神搭话文字：直接丢
		kept.append(e)
	_pending_ai = kept
	if tts != null:
		tts.cancel_kind("idle")
	_speech_hold = tts != null and tts.is_speaking()
	_refresh_lock()
	_clear_thinking_if_idle()

func _ask_reset() -> void:
	_ask_note("重新开始？当前棋局与俗手记录都会清空。", func(_note):
		_allow_tts_fillers()
		agent.reset_game())

func _ask_end() -> void:
	_ask_note("对局总结：想对Azure说什么吗？（可留空直接确定）", func(note):
		_allow_tts_fillers()
		agent.summarize(note))

# ================= 闲置互动（看玩家 / 歪头 / 主动搭话） =================

## 鼠标移动、点击、敲键盘都算「玩家有操作」
func _input(event: InputEvent) -> void:
	if event is InputEventMouseMotion:
		if (event as InputEventMouseMotion).relative.length() > 2.0:
			_bump_activity()
	elif event is InputEventMouseButton and (event as InputEventMouseButton).pressed:
		_bump_activity()
	elif event is InputEventKey and (event as InputEventKey).pressed:
		_bump_activity()

func _bump_activity() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	if _idle:
		_idle = false
		_set_idle_state(false)

func _process(_delta: float) -> void:
	var now := Time.get_ticks_msec()
	if _azure_hold_active():
		# 她还在思考 / 朗读 / 有句子没播完：棋盘锁着，玩家此刻根本无法落子，
		# 这段时间不能算「玩家闲置」——否则会出现她边堵着棋盘边问「你在忙什么呀？还要继续下棋吗」
		_last_activity_ms = now
		if _idle:
			_idle = false
			_set_idle_state(false)
	if not _idle and now - _last_activity_ms >= int(IDLE_SEC * 1000.0):
		_idle = true                                    # 长时间没操作：看向玩家、准备歪头
		_set_idle_state(true)
		_next_talk_ms = now + int(IDLE_TALK_FIRST * 1000.0)
	if _idle and now >= _next_talk_ms and agent != null and not agent.busy:
		_next_talk_ms = now + randi_range(int(IDLE_TALK_MIN * 1000.0), int(IDLE_TALK_MAX * 1000.0))
		_idle_muted = false                             # 新的一轮搭话：先解除「玩家已回来」的作废标记
		if not _peek_busy:
			var idle_ms := now - _last_activity_ms
			if idle_ms >= int(IDLE_PEEK_SEC * 1000.0) and now - _peek_last_ms >= PEEK_COOLDOWN_MS and _peek_available():
				_start_peek(now)                        # 闲置很久：先窥屏看一眼玩家在干嘛（还在游戏里/切去了别处）
			else:
				agent.idle_remark()
	if _peek_busy:
		_tick_peek(now)                                 # 窥屏结果就绪后由她主动开口
	_update_thinking(now)

## 她这一侧还没收尾：思考中，或还有语音在排队/合成/播放（棋盘处于锁定状态的两种来源）
func _azure_hold_active() -> bool:
	if agent != null and agent.busy:
		return true
	if _speech_hold:
		return true
	return tts != null and tts.is_speaking()

func _set_idle_state(v: bool) -> void:
	if board != null:
		board.idle_attention = v
	if avatar != null:
		avatar.set_idle(v)
	if v and tts != null:
		tts.allow_fillers = false      # 进入闲置：主动搭话等不插「嗯…」（玩家一动、再次对局互动就会重新放行）

## 朗读开始/结束 → 化身说话时轻轻歪头
func _on_tts_speaking(v: bool) -> void:
	if avatar != null:
		avatar.set_speaking(v)
	_tts_playing = v
	if v and cfg.tts_batch_speak:
		_flush_pending_ai()          # 整段模式：文字与这段连续语音一起显示
	if not v:
		# 注意：这里只代表「这一句播完了」。后面常有排队/正在合成的句子，
		# 所以要看整队是否真的空了才算说完；否则句间合成空隙里棋盘会被提前解锁。
		_speech_hold = tts != null and tts.is_speaking()
	_refresh_lock()
	_clear_thinking_if_idle()

## 每段语音开始播放：逐句模式下把与之对应的一条 Azure 文字显示出来（文字与声音成对）
func _on_utterance_started(group: int) -> void:
	if cfg.tts_batch_speak:
		return
	if group <= 0:                   # group=0 是试听：不显示任何待播文字
		return
	for i in _pending_ai.size():     # 按分组号精确匹配：一条消息只显示一次，乱序/丢块也不会错位
		if int(_pending_ai[i].get("group", -1)) != group:
			continue
		var e: Dictionary = _pending_ai[i]
		_pending_ai.remove_at(i)
		_add_msg_now("ai", str(e.get("text", "")))
		if bool(e.get("reveal", false)):
			_reveal_move()           # 说到这手棋时，才把子放上棋盘
		return

# ================= 长时间闲置窥屏（本地 PaddleOCR + Qwen2.5-VL，独立 python 进程一跑一停） =================

## 窥屏环境是否可用：需要 azure_peek.py 与一个装好 paddleocr / llama-cpp-python 的 python。
## 探测一次后缓存；缺任何一样就永远退回普通走神搭话（优雅降级）
func _peek_available() -> bool:
	if _peek_probed:
		return _peek_ok
	_peek_probed = true
	var exe_dir := OS.get_executable_path().get_base_dir()
	var dirs: Array[String] = [exe_dir.path_join("peek"), "D:/KataGo/peek"]
	for d in dirs:
		var script := d.path_join("azure_peek.py")
		if not FileAccess.file_exists(script):
			continue
		var pys: Array[String] = [d.path_join(".venv/Scripts/python.exe"), d.path_join("python.exe"), "E:/AI_Omni/Python/python.exe"]
		for py in pys:
			if FileAccess.file_exists(py):
				_peek_script = script
				_peek_exe = py
				_peek_ok = true
				print("[Peek] 窥屏可用：%s + %s" % [py, script])
				return true
	print("[Peek] 未找到窥屏环境（azure_peek.py / python），长时间闲置只走普通搭话")
	return false

## 后台拉起一次窥屏：独立 python 进程截图 → OCR → VLM，把结果写成 json（不阻塞游戏）
func _start_peek(now: int) -> void:
	_peek_busy = true
	_peek_started_ms = now
	_peek_last_ms = now
	_peek_ctx_activity = _last_activity_ms
	_peek_out = OS.get_user_data_dir().path_join("peek_result.json")
	if FileAccess.file_exists(_peek_out):
		DirAccess.remove_absolute(_peek_out)
	var pid := OS.create_process(_peek_exe, PackedStringArray([
		_peek_script, "--out", _peek_out, "--game-pid", str(OS.get_process_id())]))
	if pid <= 0:
		_peek_busy = false
		agent.idle_remark()                      # 拉不起来：照旧问一句
		return
	print("[Peek] 已发起窥屏（pid=%d），等结果…" % pid)

## 每帧看窥屏结果好了没；等不到就超时放弃
func _tick_peek(now: int) -> void:
	if FileAccess.file_exists(_peek_out):
		var data: Dictionary = {}
		var f := FileAccess.open(_peek_out, FileAccess.READ)
		if f != null:
			var parsed = JSON.parse_string(f.get_as_text())
			if typeof(parsed) == TYPE_DICTIONARY:
				data = parsed
			f.close()
		DirAccess.remove_absolute(_peek_out)
		_peek_busy = false
		if data.is_empty():
			return
		# 结果回来时玩家已经动过（或已不闲置）：这条窥屏内容过期了，不说
		if _last_activity_ms != _peek_ctx_activity or not _idle or agent.busy:
			return
		if not bool(data.get("ok", true)):
			agent.idle_remark()                  # 窥屏失败：退回普通搭话
			return
		if bool(data.get("is_game", true)):
			agent.idle_remark()                  # 前台还是游戏：普通走神提醒
		else:
			agent.peer_remark(data)              # 切去别的窗口了：结合窥屏内容与性格说
		return
	if now - _peek_started_ms > PEEK_TIMEOUT_MS:
		_peek_busy = false
		if _idle and not agent.busy and _last_activity_ms == _peek_ctx_activity:
			agent.idle_remark()                  # 窥屏没结果：退回普通搭话

# ================= 调试截图 =================

func _handle_cli() -> void:
	var yaw := 0.0
	var pitch := 52.0
	var dist := 0.0
	var has_view := false
	var demo := false
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--shot="):
			_shot_path = a.substr("--shot=".length())
		elif a.begins_with("--yaw="):
			yaw = float(a.substr("--yaw=".length()))
			has_view = true
		elif a.begins_with("--pitch="):
			pitch = float(a.substr("--pitch=".length()))
			has_view = true
		elif a.begins_with("--dist="):
			dist = float(a.substr("--dist=".length()))
			has_view = true
		elif a.begins_with("--tilt="):
			if avatar != null:
				avatar.debug_tilt(float(a.substr("--tilt=".length())))
		elif a.begins_with("--panels="):        # 调试截图用：--panels=both/left/right
			var pv := a.substr("--panels=".length())
			_set_panel_open("left", pv == "left" or pv == "both")
			_set_panel_open("right", pv == "right" or pv == "both")
		elif a == "--demo":
			demo = true
	if demo:
		# 调试用示例棋子：纯视觉核对，不走引擎
		board.set_stones([
			{"x": 3, "y": 3, "c": "black"}, {"x": 15, "y": 15, "c": "white"},
			{"x": 15, "y": 3, "c": "white"}, {"x": 3, "y": 15, "c": "black"},
			{"x": 9, "y": 9, "c": "black"}, {"x": 9, "y": 10, "c": "white"},
			{"x": 4, "y": 9, "c": "white", "last": true},
		])
	if has_view:
		board.set_view(yaw, pitch, dist)
	if _shot_path == "":
		return
	await get_tree().create_timer(2.5).timeout
	await RenderingServer.frame_post_draw
	var img := get_viewport().get_texture().get_image()
	var err := img.save_png(_shot_path)
	print("[shot] %s -> %s" % [error_string(err), _shot_path])
	print("[shot] viewport=%s  left_panel=%.0f  right_panel=%.0f  (PANEL_W=%d)"
			% [get_viewport().get_visible_rect().size, _left_panel.size.x, _right_panel.size.x, PANEL_W])
	get_tree().quit()

