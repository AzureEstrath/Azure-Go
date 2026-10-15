extends Node3D
## 主场景：3D 棋盘 + 右侧聊天面板，接线全部服务
## 调试：`-- --shot=输出.png` 可在渲染数帧后自动截图并退出

const PANEL_W := 380
const TEAL := Color(0.306, 0.804, 0.769)
const TEAL_DARK := Color(0.173, 0.620, 0.620)
const BUBBLE_AI := Color(0.878, 0.957, 0.957)
const BUBBLE_SYS := Color(1.0, 0.953, 0.804)

## 启动设置面板确认（_ready 里 await 它，之后才按新配置起各服务）
signal setup_confirmed

var cfg: AzureConfig
## 对局编排入口：NetClient（编排与 KataGo/LLM 都在后端服务进程）。不写死类型，与信号接线解耦。
var agent
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
var _pending_ai: Array[Dictionary] = []    # 待显示的 Azure 文字（FIFO；每项 {text, reveal, kind?}）
const AI_DEDUP_WINDOW := 8                 # 防复读窗口（兜底判据）：后端已按同样窗口去重，这里只兜 WS 重连后的乱序
var _recent_ai_texts: Array[String] = []   # 最近说过的 AI 原话（含落子宣告）：只兜 WS 重连后乱序到达的重复句
var _stream_turn := ""                     # 当前流式气泡对应的 turn_id（后端 token 流式通道：可选、默认关闭）
var _stream_label: Label = null            # 当前流式气泡：增量直接追加，最终由 message 全文收口
var _thinking_row: Control = null          # 「Azure 思考中……」占位气泡
var _thinking_label: Label = null
var _speech_hold := false                  # Azure 还有语音没播完：继续锁住棋盘
var _pending_batch := ""                   # 攒起来的连续 Azure 文本：合并成「一整段」一次合成
var _pending_batch_kind := ""              # 这批文本的类型（""=对局发言；"idle"=走神搭话，可整批作废）
var _batch_token := 0                      # 去抖令牌：又来新消息就自增，让旧计时器失效
var _tts_playing := false                  # 当前是否真有音频在播（区别于「排队中/合成中」）
var _think_dots := 0                       # 「思考中…」省略号动效点数（1~3 循环）
var _think_last_ms := 0                    # 上次刷新省略号的时刻
var _blush_seq := 0                        # 摸头脸红的序号（新的一次摸头会让旧的褪红计时失效）

## 导演层（拟人化）：重要点评前的犹豫节拍 + 聊天面板打字节奏；均为确定性本地规则，零额外 LLM 调用
const HESITATE_FAST_MIN := 0.8             # 快速/无语音引擎：犹豫时长下限（秒）
const HESITATE_FAST_MAX := 2.5
const HESITATE_SLOW_MIN := 0.5             # 慢引擎（CosyVoice）：合成本来就慢，犹豫缩短避免雪上加霜
const HESITATE_SLOW_MAX := 1.4
const TYPING_SEC_PER_CHAR := 0.045         # 打字节奏：每字时长（秒），总时长再限制在 0.5~2.4s
const TYPING_MIN_SEC := 0.5
const TYPING_MAX_SEC := 2.4
var _msg_queue: Array[Dictionary] = []     # 待投递消息 {kind,text,tone,seq}：犹豫/延迟期间顺序不乱
var _msg_queue_busy := false               # 投递协程正在跑
var _msg_seq := 0                          # 入队序号（快进判定用）
var _queue_rush_seq := 0                   # 序号 ≤ 此值的消息立即投递（玩家/系统消息到达时快进）
var _typing_label: Label = null            # 正在打字的那条 Azure 气泡
var _typing_full := ""                     # 这条消息的完整文本
var _typing_shown := 0.0                   # 已显示字数（浮点累计）
var _typing_cps := 0.0                     # 当前打字速度（字/秒）
var _typing_queue: Array[Dictionary] = []  # 排队等待打字的 {label,text}（单活跃顺序播放，防抢跑）
var _tone_re := RegEx.new()                # [[tone:xxx]] 语气标记（首次使用时编译）
var _mood := ""                            # 当前情绪基调：同一情绪延续时只刷新计时、不再出声（治「语气太多」）
var _mood_ms := 0                          # 最近一次情绪刷新的时刻
const MOOD_KEEP_MS := 30000                # 同一情绪 30 秒内视为延续；情绪变化或久未出现才出声

## Azure 连着冒的多条消息在这么长时间内合并成一段再合成（CPU 合成慢，分开发会句间空很久）
const BATCH_WINDOW := 0.35

var _last_activity_ms := 0                 # 最近一次「玩家有操作」的时刻（闲置计时）
var _idle := false
var _idle_muted := false                   # 玩家已回来（落子/说话）：在途与排队的走神搭话全部作废
var _next_talk_ms := 0                     # 下一次闲置搭话的时刻
var _last_move_count := 0                  # 用于识别 Azure 刚落的那颗子

## 音色候选（静态列表）：云端服务商各有音色名，这里只给常见几种；右侧输入框可手填任意音色 id
const VOICE_PRESETS := ["alloy", "echo", "fable", "nova", "onyx", "shimmer",
	"中文女", "粤语女", "英文女"]

## 闲置互动：玩家长时间无操作 → 看向玩家 + 歪头 + 偶尔主动搭话（搭话走后端 POST /api/idle）
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
var _status_before_tool := ""                  # 她查资料前状态栏的原文案（end 时恢复）
var _status_before_tool_bad := false           # 原文案的错误色标记（end 时一并恢复，别把红字洗成灰字）
var _status_bad := false                       # 状态栏当前是不是错误色（_set_status 记录，上面那行取用）
var _tool_active := false                      # 工具进度是否在跑（start/end 成对，避免重复记原文案）
var _chk_pass: CheckBox
var _chk_joseki: CheckBox
var _bad_box: HBoxContainer
var _chat_input: LineEdit
var _buttons: Array[Button] = []
var _shot_path := ""

func _ready() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	cfg = AzureConfig.load_config()
	_apply_cli_overrides()
	_font = _make_cjk_font()
	_build_world()
	_build_board()
	_build_ui()
	await _maybe_show_setup()                  # 启动设置面板：思考模式 + 各 API 配置（默认=当前配置）；确认后按新配置起服务
	_build_services()
	_greet()
	_handle_cli()

## 命令行覆盖：--llm=http://host:port/v1  --model=模型名  --key=云端API Key（均不写回配置）
## 另有 --backend-url=http://host:port 指定后端服务地址
func _apply_cli_overrides() -> void:
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--backend-url="):
			cfg.backend_url = a.substr("--backend-url=".length())
			cfg.cli_locked["backend_url"] = true
		elif a.begins_with("--llm="):
			cfg.llm_base_url = a.substr("--llm=".length())
			cfg.cli_locked["llm_base_url"] = true
		elif a.begins_with("--model="):
			cfg.llm_model = a.substr("--model=".length())
			cfg.cli_locked["llm_model"] = true
		elif a.begins_with("--key="):
			cfg.llm_api_key = a.substr("--key=".length())
			cfg.cli_locked["llm_api_key"] = true

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

	# 定式导学开关：识别你与 Azure 走出的定式（讲解 + 棋盘高亮手顺），改动即时写回 config.json
	_chk_joseki = CheckBox.new()
	_chk_joseki.button_pressed = cfg.joseki_teach
	_chk_joseki.toggled.connect(_on_joseki_toggled)
	_chk_joseki.tooltip_text = "识别对局中走出的定式：讲解要点、在棋盘上高亮手顺轨迹（关掉=不识别、不打扰）"
	_style_check(_chk_joseki, "定式导学")
	info.add_child(_chk_joseki)

	# 语音开关（短回复、同步说话、整段合成、长句合并，均立即生效）
	var acc := GridContainer.new()
	acc.columns = 2
	acc.add_theme_constant_override("h_separation", 12)
	vb.add_child(acc)
	_acc_check(acc, "短回复", cfg.tts_limit_len, "len",
		"限制 Azure 每次只说 1~2 句：语音等待时间随字数下降（立即生效）")
	_acc_check(acc, "同步说话", cfg.tts_sync_speak, "sync",
		"开启：Azure 的文字等语音就绪后一起显示（消除文字先出、声音晚到的割裂感）；关闭：文字立即显示")
	_acc_check(acc, "整段合成", cfg.tts_batch_speak, "batch",
		"开启：Azure 连发的多条消息合并成一段、连续朗读；关闭：逐句合成，第一句播完等第二句合成（想边听边等选它）")
	_acc_check(acc, "长句合并", cfg.tts_long_chunk, "chunk",
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

## 左侧（与右侧等宽）：语音朗读设置
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
	t1.text = "🔊 语音朗读"
	t1.add_theme_font_size_override("font_size", 16)
	t1.add_theme_color_override("font_color", Color.WHITE)
	hv.add_child(t1)
	var t2 := Label.new()
	t2.text = "云端 OpenAI 兼容语音服务 / Windows 系统语音 · 侧栏收起后依然朗读"
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
	opt.add_item("CosyVoice OpenAI 接口", 1)
	opt.selected = maxi(0, ["system", "openai"].find(cfg.tts_mode))
	opt.item_selected.connect(_on_tts_mode)
	vb.add_child(opt)

	vb.add_child(_field_label("服务地址（上面第 2 项才需要）"))
	var le_url := LineEdit.new()
	le_url.text = cfg.tts_base_url
	le_url.tooltip_text = "云端 OpenAI 兼容语音服务地址，例如 https://api.openai.com/v1（自建服务填它的地址）"
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
	_fill_voice_items()
	col2.add_child(_tts_voice_opt)
	_tts_voice_opt.item_selected.connect(_on_voice_selected)

	var vrow := HBoxContainer.new()
	vrow.add_theme_constant_override("separation", 6)
	vb.add_child(vrow)
	var b_refresh := Button.new()
	b_refresh.text = "重填音色候选"
	b_refresh.tooltip_text = "音色候选是本地静态列表，点一下按当前配置重填下拉框；具体音色 id 也可在右侧手填"
	b_refresh.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	b_refresh.pressed.connect(_refresh_voices)
	vrow.add_child(b_refresh)
	_tts_voice_edit = LineEdit.new()             # 也可手动输入音色 id
	_tts_voice_edit.text = cfg.tts_voice
	_tts_voice_edit.tooltip_text = "手动输入音色 id（服务商不同，音色名也不同）"
	vrow.add_child(_tts_voice_edit)

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
	tip.text = "语音由云端 OpenAI 兼容服务合成（第 2 项），也可用手填音色 id；\n" \
		+ "· 零配置兜底：上面第 1 项「Windows 系统语音」直接用本机 SAPI，不联网\n" \
		+ "· 服务不可达时自动临时改用系统语音，只影响本次运行、不改配置"
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
	_tts_status.text = ("已开启朗读 · 语音服务 %s" % cfg.tts_base_url) if v else "未开启朗读"
	cfg.save()

func _on_tts_mode(i: int) -> void:
	var m: String = ["system", "openai"][clampi(i, 0, 1)]
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

## 音色下拉：云端服务商各有音色名，这里只填静态候选；具体 id 也可在右侧输入框手填（不再向语音服务探测）
func _fill_voice_items() -> void:
	if _tts_voice_opt == null:
		return
	if _is_removed_voice(cfg.tts_voice):                # 旧配置里的男声/克隆音色 → 迁到中文女
		cfg.tts_voice = "中文女"
		if _tts_voice_edit != null:
			_tts_voice_edit.text = cfg.tts_voice
		if tts != null:
			tts.voice = cfg.tts_voice
		cfg.save()
	_tts_voice_opt.clear()
	for v in VOICE_PRESETS:
		_tts_voice_opt.add_item(v)
		_tts_voice_opt.set_item_metadata(_tts_voice_opt.item_count - 1, v)
	for i in _tts_voice_opt.item_count:
		if str(_tts_voice_opt.get_item_metadata(i)) == cfg.tts_voice:
			_tts_voice_opt.select(i)
			break

## 已移除的音色（男声 / 韩语女 / 旧克隆音色）：清单以 AzureConfig.LEGACY_TTS_VOICES 为准，不再各存一份
static func _is_removed_voice(v: String) -> bool:
	return v in AzureConfig.LEGACY_TTS_VOICES

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
	_tts_status.text = "音色已切换：%s" % vid

## 「重填音色候选」按钮：音色不再由服务端提供，这里只按静态候选重填一次下拉框
func _refresh_voices() -> void:
	_fill_voice_items()
	_tts_status.text = "音色候选已重填（%d 项，也可手填服务商音色）" % _tts_voice_opt.item_count

## 右侧栏语音开关：立即存盘并即时生效（都不需要重启任何服务）
func _on_acc_toggled(on: bool, which: String) -> void:
	match which:
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
	var nm := "短回复" if which == "len" else ("同步说话" if which == "sync" else ("整段合成" if which == "batch" else "长句合并"))
	_tts_status.text = "%s：%s（立即生效）" % [nm, "开" if on else "关"]
	_set_status("语音：%s已%s" % [nm, "开启" if on else "关闭"])

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
	if kind == "ai_idle" and _idle_muted:
		return                               # 玩家已经回来继续下棋：这句走神搭话作废（不出声也不显示）
	var tone := ""
	if is_ai:
		var parsed := _strip_tone(text)      # 剥离 [[tone:xxx]] 语气标记（玩家不可见、TTS 不朗读）
		tone = str(parsed[0])
		text = str(parsed[1])
		if text.strip_edges() == "":
			return
		var norm := text.strip_edges()
		if kind != "ai_move" and _recent_ai_texts.has(norm):
			print("[chat] 防复读（最近 %d 条内一字不差）已抑制：%s" % [_recent_ai_texts.size(), norm])
			return                           # 兜底判据：后端已去重，这里只兜 WS 重连后乱序到达的重复句（含隔了几条的原样复读）
		_recent_ai_texts.append(norm)
		while _recent_ai_texts.size() > AI_DEDUP_WINDOW:
			_recent_ai_texts.pop_front()
	# 所有消息统一入队、单协程顺序投递：ai_comment 前由导演层加犹豫节拍，延迟期间顺序不乱
	_msg_seq += 1
	_msg_queue.append({"kind": kind, "text": text, "tone": tone, "seq": _msg_seq})
	if not is_ai:
		_queue_rush_seq = _msg_seq           # 玩家/系统消息：把当前积压的投递全部快进
	_pump_msg_queue()

## 流式增量（后端 `stream_tokens` 开启时才有；默认关闭时这个回调一次都不会被调用到）：
## 增量直接追加到本轮的流式气泡；气泡的最终内容一律由随后的 `message` 收口（见 _add_msg_now）
func _on_token_event(turn_id: String, delta: String) -> void:
	if delta == "":
		return
	if _stream_label == null or not is_instance_valid(_stream_label) or turn_id != _stream_turn:
		_stream_turn = turn_id                       # 新的一轮发言：另起一个流式气泡
		_add_stream_bubble()
	if _stream_label == null or not is_instance_valid(_stream_label):
		return
	_stream_label.text += delta
	_scroll_to_bottom()

## 建流式气泡：有「Azure 思考中……」占位就原地改成它，否则照 Azure 消息气泡的样子新建一个
## （不做打字机：增量本来就是一块块到的，TYPING_SEC_PER_CHAR 只兜没有 token 的普通消息）
func _add_stream_bubble() -> void:
	if _thinking_label != null and is_instance_valid(_thinking_label):
		_stream_label = _thinking_label
		_thinking_label = null
		_thinking_row = null
		_stream_label.text = ""
		return
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_theme_constant_override("separation", 0)
	row.alignment = BoxContainer.ALIGNMENT_BEGIN
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
	lbl.text = ""
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color", Color(0.106, 0.337, 0.337))
	inner.add_child(lbl)
	_chat_box.add_child(row)
	_stream_label = lbl
	_scroll_to_bottom()

## 剥离回复开头的 [[tone:xxx]] 语气标记 → [tone, 干净文本]；未知标记照删但不播音（防标记泄漏给玩家）
func _strip_tone(text: String) -> Array:
	if _tone_re.get_pattern() == "":
		_tone_re.compile("\\[\\[?\\s*tone\\s*[:：]\\s*([a-zA-Z]+)\\s*\\]\\]?")
	var tone := ""
	var m := _tone_re.search(text)
	if m != null:
		tone = m.get_string(1).to_lower()
	var clean := _tone_re.sub(text, "", true).strip_edges()
	if not ["sigh", "cheer", "proud"].has(tone):
		tone = ""
	return [tone, clean]

## 单协程顺序投递：重要点评前先犹豫一下（她空闲时才犹豫），其余消息即时投递
func _pump_msg_queue() -> void:
	if _msg_queue_busy:
		return
	_msg_queue_busy = true
	while not _msg_queue.is_empty():
		var e: Dictionary = _msg_queue[0]
		var delay := _director_hesitation(str(e.get("kind", "ai")), _msg_queue.size() - 1, int(e.get("seq", 0)))
		if delay > 0.0:
			_show_thinking()                 # 占位气泡 + 「嗯……」thinking 语气词
			var t0 := Time.get_ticks_msec()
			while (Time.get_ticks_msec() - t0) < int(delay * 1000.0):
				if _queue_rush_seq >= int(e.get("seq", 0)):
					break                    # 玩家/系统消息插队：立刻结束犹豫
				await get_tree().create_timer(0.05).timeout
		_msg_queue.pop_front()
		_deliver_msg(str(e.get("kind", "ai")), str(e.get("text", "")), str(e.get("tone", "")))
	_msg_queue_busy = false

## 导演层：是否该在开口前犹豫一下（只对「重要点评」生效）；她忙/后面还有积压时不犹豫，避免越拖越乱
func _director_hesitation(kind: String, waiting_behind: int, seq: int) -> float:
	if kind != "ai_comment":
		return 0.0
	if _queue_rush_seq >= seq:
		return 0.0                           # 玩家刚有动作：别端着，直接说
	if waiting_behind > 0:
		return 0.0                           # 这条后面还排着别的：别拖
	if tts != null and tts.is_speaking():
		return 0.0                           # 她还在说上一句：不叠犹豫
	if not _pending_ai.is_empty() or _pending_batch != "":
		return 0.0                           # 还有没显示/没合成完的话：优先消化积压
	if tts != null and tts.enabled and tts.is_slow_engine():
		return randf_range(HESITATE_SLOW_MIN, HESITATE_SLOW_MAX)
	return randf_range(HESITATE_FAST_MIN, HESITATE_FAST_MAX)

## 轮到这条消息投递时真正执行（原 _add_msg 主体）：先轻声补个语气词，文字/语音仍按既有管线走
func _deliver_msg(kind: String, text: String, tone: String) -> void:
	var is_ai := kind.begins_with("ai")
	var reveal := kind == "ai_move"          # 这条是「Azure 落子宣告」：轮到她这段语音时再落子
	if tone != "":
		_mood_sound(tone)                    # 语气标记 → 预制语气词（同一情绪延续时不重复出声）
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

## 把新一句并进攒着的文本：不用换行（部分语音服务会把 \n 当分句甚至丢掉），按标点补齐句读
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
	# 流式收口（后端 stream_tokens 开启时才有）：文字已由 `token` 增量显示在同一气泡里，
	# 最终内容以这条 message 为准——整条替换，不再另建气泡、也不再走打字机
	if kind == "ai" and _stream_label != null and is_instance_valid(_stream_label):
		_stream_label.text = text
		_stream_label = null
		_stream_turn = ""
		_scroll_to_bottom()
		return
	# 「Azure 思考中……」占位：第一条真实 Azure 文字原地替换它（原地开始打字）
	if kind == "ai" and _thinking_label != null:
		var typing_lbl := _thinking_label
		_thinking_label = null
		_thinking_row = null
		_start_typing(typing_lbl, text)
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
	lbl.text = text if kind != "ai" else ""      # AI 消息先空着：交给 _start_typing 按节奏逐字显示
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color",
		Color.WHITE if kind == "user" else Color(0.106, 0.337, 0.337))
	inner.add_child(lbl)

	_chat_box.add_child(row)
	if kind == "ai":
		_start_typing(lbl, text)             # 打字节奏：AI 消息逐字出现；玩家/系统消息即时显示
	_scroll_to_bottom()

## ------------------- 打字节奏（拟人化） -------------------

## 开始「逐字显示」：长度 → 时长（0.5~2.4s）。已有气泡在打字时排队，保证一条条按顺序出现，防抢跑
func _start_typing(lbl: Label, text: String) -> void:
	if lbl == null or not is_instance_valid(lbl):
		return
	if _typing_label != null and is_instance_valid(_typing_label):
		_typing_queue.append({"label": lbl, "text": text})
		if _typing_queue.size() >= 2:
			_drain_typing()                  # 欠账堆积：旧的直接显示完，只留最新一条慢慢打
		return
	_begin_typing(lbl, text)

func _begin_typing(lbl: Label, text: String) -> void:
	_typing_label = lbl
	_typing_full = text
	_typing_shown = 0.0
	var dur := clampf(float(text.length()) * TYPING_SEC_PER_CHAR, TYPING_MIN_SEC, TYPING_MAX_SEC)
	_typing_cps = float(text.length()) / maxf(dur, 0.01)
	lbl.text = ""

## 把当前这条 + 队列里除最后一条外的欠账一次性显示完（越打越滞后时兜底）
func _drain_typing() -> void:
	if _typing_label != null and is_instance_valid(_typing_label):
		_typing_label.text = _typing_full
	_typing_label = null
	_typing_full = ""
	_typing_shown = 0.0
	while _typing_queue.size() > 1:
		var old: Dictionary = _typing_queue.pop_front()
		var ol = old.get("label")
		if ol != null and is_instance_valid(ol):
			ol.text = str(old.get("text", ""))
	var last: Dictionary = _typing_queue.pop_front()
	var l2 = last.get("label")
	if l2 != null and is_instance_valid(l2):
		_begin_typing(l2, str(last.get("text", "")))
	_scroll_to_bottom()

## 每帧推进打字（_process 调用）：打完自动接队列里的下一条
func _tick_typing(delta: float) -> void:
	if _typing_label == null:
		return
	if not is_instance_valid(_typing_label):
		_typing_label = null
		return
	var total := _typing_full.length()
	_typing_shown += _typing_cps * delta
	var n := int(_typing_shown)
	if n >= total:
		_typing_label.text = _typing_full
		_typing_label = null
		_typing_full = ""
		_typing_shown = 0.0
		_scroll_to_bottom()
		if not _typing_queue.is_empty():
			var nxt: Dictionary = _typing_queue.pop_front()
			var nl = nxt.get("label")
			if nl != null and is_instance_valid(nl):
				_begin_typing(nl, str(nxt.get("text", "")))
		return
	if n > _typing_label.text.length():
		_typing_label.text = _typing_full.substr(0, n)
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
	var locked: bool = (agent != null and agent.busy) or _speech_hold
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
	_status_bad = bad
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

# ================= 启动设置面板 =================

## 启动设置：思考模式 + LLM / 语音 TTS 的 API 配置（默认全部取当前 config.json 的值）。
## 确认后写回 config.json，_ready 再继续按新配置起服务。
## 调试自动化（--shot= / --demo）默认跳过面板；带 --setup 时只把面板画出来（不等确认），供截图核对样式。
func _maybe_show_setup() -> void:
	if DisplayServer.get_name() == "headless":
		return                                   # 无窗口（自动化测试 / --check-only）：没人能点，等了就死锁
	var shot := false
	var force := false
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--shot=") or a == "--demo":
			shot = true
		elif a == "--setup":
			force = true
	if shot and not force:
		return
	_build_setup_panel()
	if shot:
		return                                   # 截图模式：画出来即可，不能卡在等确认上
	await setup_confirmed

func _build_setup_panel() -> void:
	var back := ColorRect.new()
	back.color = Color(0, 0, 0, 0.45)
	back.set_anchors_preset(Control.PRESET_FULL_RECT)
	back.mouse_filter = Control.MOUSE_FILTER_STOP
	_ui_root.add_child(back)

	var box := PanelContainer.new()
	box.anchor_left = 0.5
	box.anchor_right = 0.5
	box.anchor_top = 0.5
	box.anchor_bottom = 0.5
	box.offset_left = -280
	box.offset_right = 280
	box.offset_top = -290
	box.offset_bottom = 290
	box.add_theme_stylebox_override("panel", _style(Color.WHITE, 12, 16))
	back.add_child(box)

	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	box.add_child(scroll)
	var vb := VBoxContainer.new()
	vb.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	vb.add_theme_constant_override("separation", 8)
	scroll.add_child(vb)

	var title := Label.new()
	title.text = "⚙️ 出发前的准备"
	title.add_theme_font_size_override("font_size", 20)
	title.add_theme_color_override("font_color", TEAL_DARK)
	vb.add_child(title)
	var sub := Label.new()
	sub.text = "以下默认就是上次的配置，直接点「开始对局」即可；改过的值会写回 config.json。"
	sub.add_theme_font_size_override("font_size", 11)
	sub.add_theme_color_override("font_color", Color(0.45, 0.45, 0.45))
	sub.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(sub)

	vb.add_child(_field_label("思考模式（只影响 Qwen3 这类思考型模型）"))
	var think_opt := OptionButton.new()
	think_opt.fit_to_longest_item = false
	think_opt.add_item("快速：不思考，首字 1~2 秒（推荐）")
	think_opt.add_item("中等：先想几句再答，约 3~6 秒")
	think_opt.add_item("强思考：先推理再回答，更慢、质量更好")
	think_opt.selected = clampi(cfg.llm_think_mode, 0, 2)
	think_opt.tooltip_text = "快速直接出答案（1~2 秒）；中等给推理限预算并催它收尾作答（4~7 秒）；强思考让模型充分推理（10 秒上下，偶有更久）"
	vb.add_child(think_opt)

	var llm_body := _setup_section(vb, "LLM 对话 API（点评 / 聊天）")
	var le_llm_url := _setup_field(llm_body, "服务地址", cfg.llm_base_url,
		"云端 OpenAI 兼容地址，如 https://api.deepseek.com/v1（也可指向自建服务）")
	var le_llm_model := _setup_field(llm_body, "模型名", cfg.llm_model,
		"填服务商要求的模型 id，如 deepseek-chat")
	var le_llm_key := _setup_field(llm_body, "API Key（只有云端需要）", cfg.llm_api_key,
		"留空=不发送 Authorization；只会存在本机的 config.json 里")

	var tts_body := _setup_section(vb, "语音 TTS API")
	var le_tts_url := _setup_field(tts_body, "服务地址", cfg.tts_base_url,
		"云端 OpenAI 兼容地址，如 https://api.example.com/v1")
	var le_tts_voice := _setup_field(tts_body, "音色", cfg.tts_voice,
		"云端服务的音色 id（各服务商不同，如 alloy / 中文女）；进游戏后左侧栏也可下拉改")

	var hb := HBoxContainer.new()
	hb.alignment = BoxContainer.ALIGNMENT_END
	vb.add_child(hb)
	var start := Button.new()
	start.text = "▶ 开始对局"
	start.add_theme_font_size_override("font_size", 16)
	hb.add_child(start)
	start.pressed.connect(func():
		_apply_setup(think_opt, le_llm_url, le_llm_model, le_llm_key, le_tts_url, le_tts_voice)
		back.queue_free()
		setup_confirmed.emit())
	start.grab_focus()
	# 高度随内容自适应（折叠时紧凑，展开分节后变高），并保持垂直居中
	await get_tree().process_frame
	var h := clampf(vb.get_combined_minimum_size().y + 24.0, 240.0, 640.0)
	box.offset_top = -h * 0.5
	box.offset_bottom = h * 0.5

## 面板里可折叠的分节（默认收起，减少第一眼信息量）
func _setup_section(vb: VBoxContainer, title_text: String, expanded := false) -> VBoxContainer:
	var btn := Button.new()
	btn.text = ("▾ " if expanded else "▸ ") + title_text
	btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
	vb.add_child(btn)
	var body := VBoxContainer.new()
	body.add_theme_constant_override("separation", 4)
	body.visible = expanded
	vb.add_child(body)
	btn.pressed.connect(func():
		body.visible = not body.visible
		btn.text = ("▾ " if body.visible else "▸ ") + title_text)
	return body

## 面板里的「标签 + 输入框」
func _setup_field(parent: Control, label_text: String, value: String, tip := "") -> LineEdit:
	parent.add_child(_field_label(label_text))
	var le := LineEdit.new()
	le.text = value
	if tip != "":
		le.tooltip_text = tip
	parent.add_child(le)
	return le

## 应用启动设置：写回 config.json（下次启动仍是这些值），随后 _build_services 按新配置起服务
func _apply_setup(think_opt: OptionButton, llm_url: LineEdit, llm_model: LineEdit, llm_key: LineEdit,
		tts_url: LineEdit, tts_voice: LineEdit) -> void:
	cfg.llm_think_mode = clampi(think_opt.selected, 0, 2)
	if llm_url.text.strip_edges() != "":         # 地址/模型/音色不允许被清成空：空则保留原值
		cfg.llm_base_url = llm_url.text.strip_edges()
	if llm_model.text.strip_edges() != "":
		cfg.llm_model = llm_model.text.strip_edges()
	cfg.llm_api_key = llm_key.text.strip_edges() # Key 可以清空
	if tts_url.text.strip_edges() != "":
		cfg.tts_base_url = tts_url.text.strip_edges()
	if tts_voice.text.strip_edges() != "":
		cfg.tts_voice = tts_voice.text.strip_edges()
	cfg.save()
	if _tts_url_edit != null:                    # 左侧语音栏建在面板之前：同步显示新值
		_tts_url_edit.text = cfg.tts_base_url
		_tts_model_edit.text = cfg.tts_model
		_tts_voice_edit.text = cfg.tts_voice
	print("[Setup] 启动设置已保存：思考模式=%d · LLM=%s (%s) · TTS=%s/%s"
			% [cfg.llm_think_mode, cfg.llm_base_url, cfg.llm_model,
			   cfg.tts_base_url, cfg.tts_voice])

# ================= 服务与接线 =================

const NetClientScript := preload("res://scripts/net/NetClient.gd")

## 装配服务：编排与 KataGo/LLM 都在后端服务进程；游戏只做渲染与语音，
## 通过 NetClient 以 REST（命令）+ WebSocket（事件）与后端通信。
func _build_services() -> void:
	_build_client_tts()                           # 语音朗读归客户端
	agent = NetClientScript.new()
	agent.base_url = cfg.backend_url
	agent.net_status.connect(_on_net_status)
	add_child(agent)                              # _ready 里发起 WS 连接（连上先收 hello 同步棋盘与情绪）
	agent.setup()                                 # 占位：引擎/LLM 由后端持有
	agent.limit_len = cfg.tts_limit_len           # 转发给后端（连上后自动补发）
	agent.joseki_teach = cfg.joseki_teach
	_wire_agent()
	_set_status("后端模式：编排与引擎在 %s（本地不启动 KataGo/LLM）" % cfg.backend_url)

## 客户端侧语音：只负责请求云端合成与播放，不启动任何模型服务
func _build_client_tts() -> void:
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
	tts.speech_skipped.connect(_on_speech_skipped)   # 这条语音不会再响（被停/队列溢出）：文字兜底立即显示

## agent 的信号接线 + 情绪心跳（NetClient 的信号面）
func _wire_agent() -> void:
	agent.message.connect(_add_msg)
	agent.state_changed.connect(_on_state_changed)
	agent.info_changed.connect(_on_info_changed)
	agent.busy_changed.connect(_on_busy_changed)
	agent.mood_changed.connect(_on_mood_changed)
	agent.joseki_event.connect(_on_joseki_event)
	agent.tool_event.connect(_on_tool_event)
	agent.token_event.connect(_on_token_event)    # 可选（默认关闭）的流式增量：只影响气泡显示过程
	var mood_timer := Timer.new()                 # 三维情绪向基线回落的心跳：2 秒一次，区域没变时零开销
	mood_timer.wait_time = 2.0
	mood_timer.autostart = true
	mood_timer.timeout.connect(func(): agent.tick_mood())
	add_child(mood_timer)
	agent.sync_mood(true)                         # 初始「平静」立即生效（表情/音高一次对齐）

## 后端连接状态：连上/断开提示进状态栏
func _on_net_status(text: String, bad: bool) -> void:
	_set_status(text, bad)

## 语音服务不可达时自动降级到系统语音（只影响本次运行，不改配置）
func _on_tts_fallback(_kind: String) -> void:
	_tts_status.text = "语音服务连不上，已临时改用系统语音"
	_set_status("语音：语音服务不可达，已临时改用系统语音（配置里仍是你的选择）")

## 语气词统一出口：同一情绪在 MOOD_KEEP_MS 内延续时只刷新计时、不再出声（治「一惊一乍、语气太多」）；
## 情绪变化或隔了很久才再次出现，才真正响一声。"" 是 no-op（平淡发言不代表情绪结束）。
func _mood_sound(kind: String) -> void:
	if tts == null or kind == "":
		return
	var now := Time.get_ticks_msec()
	if kind == _mood and now - _mood_ms < MOOD_KEEP_MS:
		_mood_ms = now                             # 情绪还在延续：保持基调，不再出声
		return
	_mood = kind
	_mood_ms = now
	tts.play_filler(kind, true)

## 三维情绪区域变化：持续表情 / 一次性强调表情 / 语气词 / TTS 音高微调
func _on_mood_changed(p: Dictionary) -> void:
	if tts != null:
		tts.set_mood_pitch(float(p.get("pitch", 0.0)))
	if avatar != null:
		avatar.set_mood(p.get("morphs", {}))
		var burst := str(p.get("burst", ""))
		if burst != "":
			avatar.play_expression(burst)          # 一次性强调（叠加在持续层上，几秒后回落到区域表情）
	var f := str(p.get("filler", ""))
	if f != "":
		_mood_sound(f)                             # 区域对应的预制语气词（同情绪延续时不出声）

## 定式识别成功：角部手顺高亮轨迹（几秒后自动淡出；纯显示，不影响棋局状态）
func _on_joseki_event(payload: Dictionary) -> void:
	if board == null:
		return
	var pts: Array = payload.get("points", [])
	if pts.is_empty():
		return
	board.highlight_points(pts, str(payload.get("kind", "formed")))

## 她自己查资料（发言侧工具）：只在状态栏提示一句，end 时恢复原文案（连错误色一起恢复）。
## 工具返回的原始盘面/JSON 永远不会显示给玩家——她的台词仍由后端生成的对话事件送达。
func _on_tool_event(payload: Dictionary) -> void:
	if _lbl_status == null:
		return
	if str(payload.get("phase", "")) == "start":
		if not _tool_active:
			_status_before_tool = _lbl_status.text          # 记下原状态（文案 + 是不是错误色），工具返回后原样恢复
			_status_before_tool_bad = _status_bad
			_tool_active = true
		_set_status("她正在查资料…")
	elif _tool_active:
		_set_status(_status_before_tool, _status_before_tool_bad)
		_tool_active = false

## 左键点在 Azure 头部附近摸摸头：开心 + 微微脸红 + 一句轻快的语气词（情绪模型同步静默加成）
func _on_head_patted() -> void:
	if agent != null:
		agent.player_interaction("patted")     # 三维情绪：愉悦度/唤醒度上升（静默：不出区域语气词，避免和「pat」叠音）
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
	var greet := "嗯~ %s，来下棋吧...我会陪你的 (｡･ω･｡)" % cfg.prompt_user_name
	if tts != null and tts.play_welcome(cfg.prompt_user_name):
		_add_msg_now("ai", greet)             # 预制欢迎语音已经开口：文字直接显示，零等待
	else:
		_add_msg("ai", greet)
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
	if not busy:
		_tool_active = false               # 回合结束：某个 start 的 end 万一没到，别让它卡住下一次 start
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

## 定式导学开关（右侧栏）：写回 config.json，并对当前对局立即生效
func _on_joseki_toggled(pressed: bool) -> void:
	cfg.joseki_teach = pressed
	cfg.save()
	if agent != null:
		agent.joseki_teach = pressed
	_set_status("定式导学已%s" % ("开启" if pressed else "关闭"))

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
	var kept_q: Array[Dictionary] = []
	for e in _msg_queue:
		if str((e as Dictionary).get("kind", "")) == "ai_idle":
			continue                     # 投递队列里还没开口的走神搭话：一并作废
		kept_q.append(e)
	_msg_queue = kept_q
	_queue_rush_seq = _msg_seq           # 其余积压快进（玩家刚动手，别让她慢慢来）
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

func _process(delta: float) -> void:
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
		agent.idle_remark()                             # 长时间没动静：主动问一句（后端 POST /api/idle）
	_update_thinking(now)
	_tick_typing(delta)                                 # 打字节奏推进（AI 消息逐字显示）

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

## 这条语音不会再响了（被 stop / 队列溢出丢弃）：文字/落子照常显示，只是这一句没有声音
func _on_speech_skipped(group: int) -> void:
	if cfg.tts_batch_speak:
		_flush_pending_ai()
	else:
		_on_utterance_started(group)

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
