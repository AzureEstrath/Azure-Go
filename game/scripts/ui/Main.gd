extends Node3D
## 主场景：3D 棋盘 + 右侧聊天面板，接线全部服务
## 调试：`-- --shot=输出.png` 可在渲染数帧后自动截图并退出

const PANEL_W := 380
const TEAL := Color(0.306, 0.804, 0.769)
const TEAL_DARK := Color(0.173, 0.620, 0.620)
## 白底面板上的深色文字（手数 / 勾选项等）：与 TEAL_DARK 同一深色系。
## 两处曾各写一遍同一个字面量（v3.7.0 修复波收口），行为不变。
const PANEL_TEXT_DARK := Color(0.13, 0.30, 0.30)
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
var _joseki_card: JosekiCard = null        # 定式浮层参考图卡片（浮在棋盘上方，不进任何侧栏）
var _joseki_card_home := Vector2.ZERO      # 我们最后一次给卡片摆的位置（用户拖走后不再接管）
var _left_open := false                    # 左侧默认收起，保持棋盘视野
var _right_open := true
var _left_toggle: Button
var _right_toggle: Button
var _tts_status: Label
var _tts_url_edit: LineEdit
var _tts_model_edit: LineEdit
var _tts_voice_edit: LineEdit
var _tts_voice_opt: OptionButton
const AI_DEDUP_WINDOW := 8                 # 防复读窗口（兜底判据）：后端已按同样窗口去重，这里只兜 WS 重连后的乱序
var _recent_ai_texts: Array[String] = []   # 最近说过的 AI 原话（含落子宣告）：只兜 WS 重连后乱序到达的重复句

## —— 语音配对（计划 05：合成归后端，前端只播放）——
## 配对表、TTL/保鲜期、「她在忙吗」的判定全部在 `SpeechPairing`（可测决策层）里；本文件只留胶水：
##   · 取音频（HTTP）、调系统语音、显示气泡、`POST /api/reveal`（见 _on_pairing_display）
##   · 外部事实（后端 busy / 音频是否在播 / 是否还有取音频请求在途）由 `_pair_facts()` 打包喂给它
var pairing: SpeechPairing = null
var _speech_fetch: Array[Dictionary] = []  # 待取音频的 FIFO（**串行**取：保证播放顺序 = speech 到达顺序）
var _speech_fetch_busy := false
var _stream_turn := ""                     # 当前流式气泡对应的 turn_id（后端 token 流式通道：可选、默认关闭）
var _stream_label: Label = null            # 当前流式气泡：增量直接追加，最终由 message 全文收口
var _thinking_row: Control = null          # 「Azure 思考中……」占位气泡
var _thinking_label: Label = null
var _speech_hold := false                  # Azure 还有语音没播完：继续锁住棋盘
var _batch_pending := false                # 整段模式：还攒着没放出来的 Azure 文本（只当标记用，文本不再合并）
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
var _lbl_score_hint: Label                     # 目差下面那行 11px 小字：量级人话（空串＝还没有形势信息）
var _lbl_status: Label
var _status_before_tool := ""                  # 她查资料前状态栏的原文案（end 时恢复）
var _status_before_tool_bad := false           # 原文案的错误色标记（end 时一并恢复，别把红字洗成灰字）
var _status_bad := false                       # 状态栏当前是不是错误色（_set_status 记录，上面那行取用）
var _tool_active := false                      # 工具进度是否在跑（start/end 成对，避免重复记原文案）
var _chk_pass: CheckBox
var _joseki_opt: OptionButton                     # 定式导学＝「开关 + 频率」一格（关闭 / 低 25% / 中 50% / 高 75%）
var _drift_opt: OptionButton                      # 情绪波动＝「开关 + 基础概率」一格（关闭 / 低 3% / 中 5% / 高 6%；在 ⚙ 设置抽屉里）
var _btn_settings: Button                         # ⚙ 设置抽屉的开关
var _settings_box: PanelContainer                 # ⚙ 设置抽屉本体（情绪波动 / 同步说话 / 整段合成 / 长句合并 / 聊天摘要用 LLM / 画像，默认收起）
var _lbl_profile: Label                           # 画像面板的只读文本（v3.9.0：与开关同一格，不占右栏常驻位）
var _bad_box: HBoxContainer
var _chat_input: LineEdit
var _lbl_queue: Label                           # 右下角那行「排队中 N 条 · 队头前几个字」（队列空则隐藏）
var _buttons: Array[Button] = []
var _shot_path := ""

## —— 局后只读复盘（v3.7.0 任务 D）——
## 入口只在**对局结束后**出现（`st.finished` 全仓没人读 ⇒ 前端自己记一个本地标志，V19）；
## 复盘期间棋盘只读、四个按钮置灰、聊天禁用，退出时**逐项恢复**（V20/V21）。
var _game_ended := false                   # 本地标志：🏁 结束 流程走完置真、🔄 重来 置假
var _reviewing := false                    # 复盘回看中：冻结 UI + 静音（不调 chat/idle/hint/analyze）
var _review_total := 0                     # 进入复盘时的总手数（翻页夹取的上界）
var _review_cur := 0                       # 当前回看到第几手（0.._review_total）
var _review_box: HBoxContainer             # 复盘按钮组（对局结束前整组隐藏，不挤界面）
var _btn_review: Button                    # 📖 复盘 / ✅ 退出复盘
var _btn_prev: Button                      # ◀ 上一手（只在复盘模式内可见）
var _btn_next: Button                      # 下一手 ▶（只在复盘模式内可见）
var _btn_send: Button                      # 聊天发送（复盘期间与输入框一起禁用）

func _ready() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	cfg = AzureConfig.load_config()
	_apply_cli_overrides()
	_build_pairing()                           # 语音配对状态机：只做决策，副作用回调回本文件
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

## 语音配对状态机：纯决策（谁该显示、谁还在忙、什么该清出），副作用都由信号回到本文件
func _build_pairing() -> void:
	pairing = SpeechPairing.new()
	add_child(pairing)
	pairing.display_requested.connect(_on_pairing_display)
	pairing.resolved.connect(_on_pairing_resolved)

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
	board.view_changed.connect(func(t): _set_status(t))   # Home / C 动了视角：状态栏给一句反馈
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

	# 卡片先加：它是浮层，任何既有控件（左右栏 / 开关）都压在它上面——只浮在棋盘上，不挤压既有控件
	_build_joseki_card(root)
	_build_right_panel(root)
	_build_left_panel(root)
	_build_toggles(root)
	_apply_panel_layout(false)                  # 侧栏就位 + 棋盘取景按侧栏让位
	_place_joseki_card()                        # 再定卡片默认落位（必须等棋盘取景定下来，否则用错相机）

## 定式浮层参考图卡片：2D Control 浮在 3D 之上（与左右栏同一套「代码建节点」的做法，**不进侧栏**）。
## 内容全部来自后端 `joseki` 事件（后端是唯一权威）；默认落位在 `_place_joseki_card()` 里现算。
func _build_joseki_card(root: Control) -> void:
	_joseki_card = JosekiCard.new()
	_joseki_card.theme = root.theme                 # 用同一套中文字体
	root.add_child(_joseki_card)
	get_viewport().size_changed.connect(_on_viewport_resized)

## 默认落位：按**棋盘投影**挑第一个不与棋盘 / 已展开侧栏相交、且完整落在窗口内的空白角落。
## 原先硬编码「左下角」在 1280×800 下正压棋盘（棋盘近左角伸进卡片)，卡片一出生就被判 faded——故现算。
func _place_joseki_card() -> void:
	if _joseki_card == null:
		return
	_joseki_card.position = _joseki_card_anchor()
	_joseki_card_home = _joseki_card.position

## 窗口尺寸变了：只有用户还没动过卡片（还在我们摆的位置上）才重新摆位，绝不抢用户的手
func _on_viewport_resized() -> void:
	if _joseki_card == null or _joseki_card.position != _joseki_card_home:
		return
	_place_joseki_card()

## 候选落位，按序取第一个可行的；全都不可行就退回左下角（会被避让判成 faded，但至少完整在窗口内）。
## 棋盘投影拿不到（没棋盘 / 没相机）时直接退回左下角，保持旧行为。
func _joseki_card_anchor() -> Vector2:
	var vp := get_viewport().get_visible_rect().size
	var fallback := Vector2(24.0, maxf(24.0, vp.y - JosekiCard.CARD_H - 24.0))
	var br := _board_screen_rect()
	if not br.has_area():
		return fallback
	var w := JosekiCard.CARD_W
	var h := JosekiCard.CARD_H
	var lx := float(PANEL_W) if _left_open else 0.0            # 展开的侧栏占掉的左右边界
	var rx := vp.x - (float(PANEL_W) if _right_open else 0.0)
	var x_lo := lx + 12.0
	var x_hi := maxf(x_lo, rx - w - 12.0)
	# 躲的东西：棋盘投影 + 已展开的侧栏（卡片不得挤压既有控件）
	var forbid: Array[Rect2] = [br]
	if _left_open and _left_panel != null:
		forbid.append(_left_panel.get_global_rect())
	if _right_open and _right_panel != null:
		forbid.append(_right_panel.get_global_rect())
	var inside := Rect2(Vector2(12.0, 12.0), vp - Vector2(24.0, 24.0))
	var anchors: Array[Vector2] = [
		Vector2(clampf(br.position.x, x_lo, x_hi), br.end.y + 12.0),                       # ① 棋盘左下外侧
		Vector2(clampf(br.position.x - w - 12.0, x_lo, x_hi), br.get_center().y - h * 0.5), # ② 棋盘左侧居中
		Vector2(clampf(rx - w - 12.0, x_lo, x_hi), br.end.y + 12.0),                       # ③ 右栏左边的空档
		Vector2(clampf(br.get_center().x - w * 0.5, x_lo, x_hi), br.end.y + 12.0),         # ④ 棋盘正下方
		Vector2(clampf(br.position.x, x_lo, x_hi), br.position.y - h - 12.0),              # ⑤ 棋盘左上外侧
	]
	for a in anchors:
		var r := Rect2(a, Vector2(w, h))
		if not inside.encloses(r):
			continue                                  # 露不全（窗口太矮/太窄）：换下一个
		var clash := false
		for f in forbid:
			if f.intersects(r):
				clash = true
				break
		if not clash:
			return a
	return fallback

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
	_lbl_moves.add_theme_color_override("font_color", PANEL_TEXT_DARK)  # 面板是白底：默认主题白字看不见（R-GH-1）
	info.add_child(_lbl_moves)
	_lbl_wr = Label.new()
	_lbl_wr.text = "黑胜率: --"
	_lbl_wr.add_theme_color_override("font_color", TEAL_DARK)
	info.add_child(_lbl_wr)
	# 目差：数字照旧保留（精度不丢）+ 下面一行 11px 小字给量级人话。**竖排不并排**——这一行已经贴着
	# 380 面板的宽度上限，并排会把「允许 Azure 虚着」挤出面板（同 Task F 里定式那一格挪行的理由）。
	var score_col := VBoxContainer.new()
	score_col.add_theme_constant_override("separation", 0)
	_lbl_score = Label.new()
	_lbl_score.text = score_line(null)
	_lbl_score.add_theme_color_override("font_color", Color(0.153, 0.682, 0.376))
	_lbl_score.tooltip_text = "目差 = 黑棋实地估算 − 白棋实地估算（正数＝黑好，负数＝白好）。\n" \
		+ "它和胜率偶尔会不同调（胜率 51% + 目差 -2.5 是正常的），两个一起看；下面那行是量级人话。"
	score_col.add_child(_lbl_score)
	_lbl_score_hint = Label.new()
	_lbl_score_hint.text = ""                      # 量级人话：空串＝还没有形势信息（占位不显示，行高不跳）
	_lbl_score_hint.add_theme_font_size_override("font_size", 11)
	_lbl_score_hint.add_theme_color_override("font_color", Color(0.45, 0.45, 0.45))
	score_col.add_child(_lbl_score_hint)
	info.add_child(score_col)
	_chk_pass = CheckBox.new()
	_chk_pass.text = "允许Azure虚着"
	_chk_pass.toggled.connect(_on_allow_pass_toggled)
	_style_check(_chk_pass, "允许 Azure 虚着")
	info.add_child(_chk_pass)

	# 定式导学：开关 + 频率合一（关闭 / 低 25% / 中 50% / 高 75%）。改动即时写回 config.json 并对当前对局生效。
	# OptionButton 比原来的勾选框宽，单起一行——否则会把「手数 / 胜率 / 目差 / 允许虚着」那一行挤出面板。
	var joseki_row := HBoxContainer.new()
	vb.add_child(joseki_row)
	_joseki_opt = OptionButton.new()
	_joseki_opt.add_item("定式导学：关闭", 0)
	_joseki_opt.add_item("定式导学：低 25%", 1)
	_joseki_opt.add_item("定式导学：中 50%", 2)
	_joseki_opt.add_item("定式导学：高 75%", 3)
	_joseki_opt.tooltip_text = "识别对局中走出的定式：讲解要点、在棋盘上高亮手顺轨迹。\n" \
		+ "· 关闭 = 不识别、不打扰\n" \
		+ "· 低 25% / 中 50% / 高 75% = 有资格提示时按该概率提示（讲解与卡片只看「有没有资格」，不重复掷骰）"
	_joseki_opt.selected = joseki_level_index(cfg.joseki_teach, cfg.joseki_rate)   # 只显示，不回写 config.json
	_joseki_opt.item_selected.connect(_on_joseki_level_selected)
	joseki_row.add_child(_joseki_opt)

	# 语音开关：短回复（对局常用，留外面）；同步说话 / 整段合成 / 长句合并进 ⚙ 设置抽屉（默认收起）。
	# 生效范围**不一样**：前两个立即生效；长句合并只写配置，分块在后端做 ⇒ 重启后端后生效（tooltip 必须说清）。
	var acc := HBoxContainer.new()
	acc.add_theme_constant_override("separation", 12)
	vb.add_child(acc)
	_acc_check(acc, "短回复", cfg.tts_limit_len, "len",
		"限制 Azure 每次只说 1~2 句：语音等待时间随字数下降（立即生效）")
	_btn_settings = Button.new()
	_btn_settings.text = "⚙ 设置 ▸"
	_btn_settings.toggle_mode = true
	_btn_settings.tooltip_text = "一次性设置：同步说话 / 整段合成（立即生效）、长句合并（重启后端后生效）；" \
		+ "改动都写回 config.json"
	_btn_settings.toggled.connect(_on_settings_toggled)
	acc.add_child(_btn_settings)

	_settings_box = PanelContainer.new()
	_settings_box.visible = false                  # 默认收起：右栏只留对局常用项
	_settings_box.add_theme_stylebox_override("panel", _style(Color(0.965, 0.98, 0.98), 6, 10))
	vb.add_child(_settings_box)
	var drawer := VBoxContainer.new()
	drawer.add_theme_constant_override("separation", 6)
	_settings_box.add_child(drawer)
	# 情绪波动（v3.8.0）：**开关与参数同占一格**（spec 第五节 K：不新增右栏控件位）——一个下拉框
	# 同时表达「关 / 低 3% / 中 5% / 高 6%」。改动即时写回 config.json 并推给后端（同 joseki 那条通道）。
	_drift_opt = OptionButton.new()
	_drift_opt.add_item("情绪波动：关闭", 0)
	_drift_opt.add_item("情绪波动：低 3%", 1)
	_drift_opt.add_item("情绪波动：中 5%", 2)
	_drift_opt.add_item("情绪波动：高 6%", 3)
	_drift_opt.tooltip_text = "开启后她**小概率**不走最优解：在三条预测线里挑一条（越偏越少），偶尔像人一样下偏。\n" \
		+ "· 关闭 = 行为零变化（恒走最优解、不掷骰）\n" \
		+ "· 档位 = 偏航的**基础**概率（这里是本地档位：3% / 5% / 6%）；心情越低落 / 好奇 / 激动，" \
		+ "实际概率会在此基础上再往上浮动（浮动上限由后端配置 drift_max 决定，默认 8%）——" \
		+ "所以每一档都留了情绪浮动的余量\n" \
		+ "· 偏得太远（胜率差超 5%）的线一律不用：宁可走最优解，也不下烂棋"
	_drift_opt.selected = drift_level_index(cfg.emotion_drift, cfg.drift_base)   # 只显示，不回写 config.json
	_drift_opt.item_selected.connect(_on_drift_level_selected)
	drawer.add_child(_drift_opt)
	_acc_check(drawer, "同步说话", cfg.tts_sync_speak, "sync",
		"开启：Azure 的文字等语音就绪后一起显示（消除文字先出、声音晚到的割裂感）；关闭：文字立即显示")
	_acc_check(drawer, "整段合成", cfg.tts_batch_speak, "batch",
		"开启：Azure 连发的多条消息合并成一段、连续朗读；关闭：逐句合成，第一句播完等第二句合成（想边听边等选它）")
	_acc_check(drawer, "长句合并", cfg.tts_long_chunk, "chunk",
		"单次 TTS 合成字数上限：关=100（更快出第一句，默认）；开=160（长句一次合成、但首句更慢）。"
		+ "改完重启后端才生效（分块在后端做）")
	# 聊天摘要用 LLM（v3.8.0）：**默认关**——聊天摘要走规则式（纯函数分段 + 模板摘要）、零额外 LLM 调用；
	# 开启后只在**局末 / 空闲**用 LLM 补做分段总结（落子回合内绝不加调用）。写回 config.json 并即时生效。
	_acc_check(drawer, "聊天摘要用 LLM", cfg.chat_digest_llm, "digest",
		"默认关：聊天摘要走规则式、**零额外 LLM 调用**；开启：只在**局末 / 空闲**用 LLM 分段总结"
		+ "（落子回合内绝不加调用）→ 立即生效")
	# 玩家棋风画像 + 时间感知（v3.9.0）：两个开关与只读面板**同一件事**，一起放在这一格
	# （控制者倾向：与 emotion_drift / chat_digest_llm 同形；两个开关**一次推一次** `/api/config`）。
	_acc_check(drawer, "玩家棋风画像", cfg.profile_enabled, "profile",
		"默认关。开启后，每次点「🏁 结束」会把这局记进后端的 profile.db，并跨局攒出「她眼中的你」"
		+ "（开局偏好 / 行棋风格 / 常见失误 / 常聊话题）；她只在相关时自然参考这份印象 → 立即生效")
	_acc_check(drawer, "时间感知", cfg.time_aware_enabled, "time_aware",
		"默认关。开启后她知道现在几点、这一局下了多久、你多久没来、连续第几天——只在相关时提，"
		+ "不会把数字当口头禅 → 立即生效")
	# 只读面板：一个按钮 + 一块文本（**不新增右栏常驻控件位**，与 joseki / drift / digest 同风格）。
	# 不并进 `_buttons`：它是只读 GET，复盘期间也允许看（冻结清单只锁「会改动局面 / 会让她开口」的控件）。
	var prof_row := HBoxContainer.new()
	prof_row.add_theme_constant_override("separation", 6)
	drawer.add_child(prof_row)
	_button(prof_row, "🪞 她眼中的你", _on_profile_panel)
	_lbl_profile = Label.new()
	_lbl_profile.add_theme_font_size_override("font_size", 11)
	_lbl_profile.add_theme_color_override("font_color", PANEL_TEXT_DARK)
	_lbl_profile.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_lbl_profile.visible = false
	drawer.add_child(_lbl_profile)

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
	_btn_send = send

	# 排队提示（她忙时发的聊天先排着；右下角显示「排队中 N 条 · 队头前几个字」，队列空则隐藏）
	_lbl_queue = Label.new()
	_lbl_queue.add_theme_font_size_override("font_size", 11)
	_lbl_queue.add_theme_color_override("font_color", Color(0.55, 0.55, 0.55))
	_lbl_queue.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_lbl_queue.visible = false
	vb.add_child(_lbl_queue)

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

	# 复盘（v3.7.0）：**只在对局结束后**出现（未结束整组隐藏 ⇒ 界面不变挤，V19）。
	# 进复盘后棋盘只读、上面四个按钮置灰、聊天禁用；◀ / ▶ 与 ← / → 只在复盘模式内生效。
	_review_box = HBoxContainer.new()
	_review_box.add_theme_constant_override("separation", 6)
	_review_box.visible = false
	vb.add_child(_review_box)
	_btn_prev = _button(_review_box, "◀ 上一手", func(): _review_step(-1))
	_btn_next = _button(_review_box, "下一手 ▶", func(): _review_step(1))
	_btn_prev.visible = false
	_btn_next.visible = false
	_btn_review = _button(_review_box, "📖 复盘", _on_review_toggle)
	_btn_review.tooltip_text = "只读回看这一局：棋盘不接受落子与摸头，她也不开口（← / → 翻页）"

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
	b_test.pressed.connect(_preview_tts)
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
	tip.text = "语音由后端合成（云端 OpenAI 兼容服务），本端只负责播放；\n" \
		+ "· 零配置兜底：上面第 1 项「Windows 系统语音」直接用本机 SAPI，不联网\n" \
		+ "· 云端合成不可用时，后端会直接改用系统语音念（配置里仍是你的选择）"
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
	var dark := PANEL_TEXT_DARK
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
		tts.mode = m                 # 本地镜像：只影响「导演层犹豫节拍」的快慢估计
		tts.stop()
	cfg.save()
	_tts_status.text = "朗读方式：%s（合成在后端，重启后端后生效）" % m

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
		# 合成归后端：地址/模型不再由前端使用（后端读同一份 config.json），音色本端系统语音还要用
		tts.voice = voice
	cfg.save()
	_tts_status.text = "语音设置已保存（合成服务地址/模型由后端读取，重启后端后生效）"

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
			if _batch_pending:
				_batch_token += 1
				_release_batch(true)
			elif pairing.has_pending():
				_flush_pending_ai()
		"chunk":
			cfg.tts_long_chunk = on
			# 分块已归后端：这个开关只写配置，后端按其 config.json 决定单块字数（重启后端后生效）
		"digest":
			cfg.chat_digest_llm = on
			if agent != null:
				# 原子入口（v3.8.0）：单字段也只推一次 `/api/config`（与 set_drift / set_joseki 同一形状）
				agent.set_digest(on)
		"profile":
			cfg.profile_enabled = on
			if agent != null:
				# 原子入口（v3.9.0）：画像 / 时间感知两个开关**一次推一次** `/api/config`
				agent.set_profile(cfg.profile_enabled, cfg.time_aware_enabled)
		"time_aware":
			cfg.time_aware_enabled = on
			if agent != null:
				agent.set_profile(cfg.profile_enabled, cfg.time_aware_enabled)
	cfg.save()
	if which in ["digest", "profile", "time_aware"]:
		# 它们不是语音项：只进底部状态栏——`_tts_status` 是左栏「语音朗读」的状态行，别写串
		var nm2 := "聊天摘要用 LLM" if which == "digest" else ("玩家棋风画像" if which == "profile" else "时间感知")
		_set_status("%s 已%s（立即生效）" % [nm2, "开启" if on else "关闭"])
		return
	var nm := "短回复" if which == "len" else ("同步说话" if which == "sync" else ("整段合成" if which == "batch" else "长句合并"))
	var scope := "（重启后端后生效）" if which == "chunk" else "（立即生效）"
	_tts_status.text = "%s：%s%s" % [nm, "开" if on else "关", scope]
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
	var speak_kind := "idle" if kind == "ai_idle" else ""
	var text_only := pairing.take_text_only_mark(kind) if is_ai else false
	var wants_speech := message_needs_speech(kind, text_only)
	if kind == "ai_idle" and _idle_muted:
		_register_tomb(speak_kind)           # 本地作废：留个占位吃掉它自己那条 speech
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
			# 兜底判据：后端已去重，这里只兜 WS 重连后乱序到达的重复句（含隔了几条的原样复读）。
			# 占位（tomb）是用来「吃掉它自己那条 speech」的——**无语音的台词不许留占位**，
			# 否则它会吃掉**下一条**真语音（修复轮 1 / C1）。
			if wants_speech:
				_register_tomb(speak_kind)
			return
		_recent_ai_texts.append(norm)
		while _recent_ai_texts.size() > AI_DEDUP_WINDOW:
			_recent_ai_texts.pop_front()
	# 所有消息统一入队、单协程顺序投递：ai_comment 前由导演层加犹豫节拍，延迟期间顺序不乱
	# 要由后端朗读的 AI 台词在这里就登记配对记录（登记顺序 = 与 speech 事件的配对顺序）；
	# 无语音的台词（推送方声明的 `AI_NOTICE` / 本地 `text_only` 标记）**不登记**——它没有语音，
	# 登记了会占掉配对位、把后面几条真语音整体错位（修复轮 1 / C1）。
	_msg_seq += 1
	var rec := {}
	if wants_speech:
		rec = _speech_register(text, kind == "ai_move", speak_kind)
	_msg_queue.append({"kind": kind, "text": text, "tone": tone, "seq": _msg_seq, "rec": rec})
	if not is_ai:
		_queue_rush_seq = _msg_seq           # 玩家/系统消息：把当前积压的投递全部快进
	_pump_msg_queue()

## 推送方声明「这条没有语音」的 kind（后端 `events.AI_NOTICE`，v3.9.0 修复轮 1 / C1）：
## 主动约战提醒与看门狗可读提示**不发** `speech`。前端**无从预知**它们，所以必须由推送方声明；
## 否则它们会被当成普通台词登记配对记录，占掉配对位、把后面三条真语音整体错位
## （点评等到落子宣告的音频才显示、揭示被推后、最后一条没声音）。
## 与 `mark_text_only()` 是同一语义，区别只在于「谁来声明」。
const AI_NOTICE := "ai_notice"

## 这条 `message` 要不要登记语音配对记录？（**纯函数**，headless 门卫直接断言）
## 三种「没有语音」的来源：① 推送方声明（`AI_NOTICE`）；② 调用点本地标记（undo / reset /
## 允许虚着 / 本地开场问候，走 `pairing.mark_text_only()`）；③ 不是她的话（玩家 / 系统消息）。
static func message_needs_speech(kind: String, text_only := false) -> bool:
	if not kind.begins_with("ai"):
		return false
	if kind == AI_NOTICE:
		return false
	return not text_only

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
		var rec: Dictionary = e.get("rec", {})
		_deliver_msg(str(e.get("kind", "ai")), str(e.get("text", "")), str(e.get("tone", "")), rec)
	_msg_queue_busy = false

## 导演层：是否该在开口前犹豫一下（只对「重要点评」生效）；她忙/后面还有积压时不犹豫，避免越拖越乱
func _director_hesitation(kind: String, waiting_behind: int, seq: int) -> float:
	if kind != "ai_comment":
		return 0.0
	if _queue_rush_seq >= seq:
		return 0.0                           # 玩家刚有动作：别端着，直接说
	if waiting_behind > 0:
		return 0.0                           # 这条后面还排着别的：别拖
	if _audio_in_flight():
		return 0.0                           # 她还在说上一句（或语音还在取）：不叠犹豫
	if pairing.has_pending() or _batch_pending:
		return 0.0                           # 还有没显示/没播完的话：优先消化积压
	if tts != null and tts.enabled and tts.is_slow_engine():
		return randf_range(HESITATE_SLOW_MIN, HESITATE_SLOW_MAX)
	return randf_range(HESITATE_FAST_MIN, HESITATE_FAST_MAX)

## 轮到这条消息投递时真正执行（原 _add_msg 主体）：先轻声补个语气词，文字/语音仍按既有管线走。
## `rec` 是这条台词在 `_add_msg` 里登记的语音配对记录（{} = 这条不由后端朗读/语音关着）
func _deliver_msg(kind: String, text: String, tone: String, rec: Dictionary) -> void:
	var is_ai := kind.begins_with("ai")
	var reveal := kind == "ai_move"          # 这条是「Azure 落子宣告」：轮到她这段语音时再落子
	if tone != "":
		_mood_sound(tone)                    # 语气标记 → 预制语气词（同一情绪延续时不重复出声）
	if is_ai and tts != null and tts.enabled and not rec.is_empty():
		var tts_kind := "idle" if kind == "ai_idle" else ""      # 走神搭话可被整类取消（cancel_kind("idle")）
		if cfg.tts_batch_speak:
			# 整段排队连续播报：各条语音自己按 speech 事件排队播，文字攒着等这段语音开口时一起显示
			if _batch_pending and _pending_batch_kind != tts_kind:
				_batch_token += 1
				_release_batch(true)         # 批次按类型分组：走神搭话不跟对局发言混进同一段语音
			pairing.arm(rec, false)
			if pairing.played(rec):
				pairing.display(rec)     # 语音在轮到自己投递前就出结果了（整段模式没有逐句那处补偿）：立刻补上
			_batch_pending = true
			_pending_batch_kind = tts_kind
			_batch_token += 1
			var token := _batch_token
			get_tree().create_timer(BATCH_WINDOW).timeout.connect(func(): _flush_batch(token))
		else:
			# 逐句播报：语音由 speech 事件驱动（按到达顺序与这条配对）。
			# 「同步说话」只在**她这一轮还没结束**（或这条要揭示棋子）时让文字等语音——
			# 见 `should_wait_voice`：回合已经结束的台词（聊天 / 闲置搭话 / 非法手提示）当场显示，
			# 她上一句的音频还在播时也不能扣着（否则回复会一直挂在「Azure 思考中……」里，
			# 等到玩家再发一条才放出来）。
			var wait_voice: bool = pairing.should_wait_voice(cfg.tts_sync_speak, _pair_facts(), reveal)
			pairing.arm(rec, wait_voice)
			if not wait_voice or pairing.played(rec):
				pairing.display(rec)         # 文字立即显示 / 语音先到先播过了：这一刻就把文字补上
		pairing.release_stale(_pair_facts())  # 后端这一轮已结束（如 undo/reset 只有文字）：别一直等语音
		return
	if is_ai:
		_add_msg_now("ai", text)                 # 未启用语音：直接显示
		if reveal:
			_reveal_move()
		return
	# 非 Azure 消息（玩家输入 / 系统提示）：先把积压的 Azure 内容放出来，保证聊天顺序不乱
	if cfg.tts_batch_speak:
		if _batch_pending:
			_batch_token += 1
			_release_batch(true)
	elif pairing.has_pending():
		_flush_pending_ai()
	_add_msg_now(kind, text)

## 去抖计时器到点：只有最后一个令牌有效（期间又来了新消息就作废）
func _flush_batch(token: int) -> void:
	if token != _batch_token:
		return
	_release_batch(false)

## 一批 Azure 文本的**显示**时机。合成已归后端（各条 speech 自己排队播），这里只决定「文字现在显示，
## 还是等这段连续语音开口时再显示」——「整段合成」开关的语义因此从「一次合成一整段」变成「整段排队连续播报」。
func _release_batch(force_show: bool) -> void:
	var had := _batch_pending
	_batch_pending = false
	_pending_batch_kind = ""
	if not had:
		return
	if tts == null or not tts.enabled:
		_flush_pending_ai()             # 语音关着：文字直接显示
		return
	if cfg.tts_sync_speak and not force_show and not _tts_playing:
		# 文字等这段连续语音开口时（_on_tts_speaking → _flush_pending_ai）与声音一起出现；
		# 万一这段语音最后没到（后端只发了文字/音频取不回），pairing.release_stale 会兜底放出来。
		# （逐句模式的同类判断在 `should_wait_voice`；整段模式这里本来就要求「已经在响了」才等，
		#  所以聊天这类「回合已结束」的回复不会像逐句模式那样被扣住。）
		return
	_flush_pending_ai()                 # 已经在响了 / 需要保序：先显示文字

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
	pairing.flush_pending()

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
	if _thinking_label == null and pairing.has_pending() and not _tts_playing:
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
	if pairing.has_pending() or _audio_in_flight() or (agent != null and agent.busy):
		return
	_clear_thinking()

## 棋盘锁 + 复盘冻结：Azure 思考中，或她还有话没播完（整段关闭时尤其要等她说完），
## 或**正在只读复盘**。冻结范围（棋盘只读 / 四个按钮 / 聊天）在静态 `review_lock_state` 里一处定死，
## 这里只把它**逐项应用**到控件上——退出复盘时同样走这里，所以「逐项恢复」不需要另一套逻辑（V20/V21）。
func _refresh_lock() -> void:
	var s := review_lock_state(_reviewing, agent != null and agent.busy, _speech_hold)
	if board != null:
		board.locked = bool(s["locked"])
		board.set_readonly(bool(s["readonly"]))   # 复盘只读（落子与摸头同源）
	for b in _buttons:
		b.disabled = bool(s["buttons_disabled"])
	if _chat_input != null:
		_chat_input.editable = bool(s["chat_editable"])
	if _btn_send != null:
		_btn_send.disabled = not bool(s["chat_editable"])
	# 复盘期间一并冻住的「别的改动入口」（I1）：同一个判据、同一处落值——`state` 事件反复重建
	# 俗手回档按钮也不会把冻结冲掉（重建后由 `_render_bad_moves` 再叫一次这里）。
	var extra := bool(s["extra_disabled"])
	if _bad_box != null:
		for c in _bad_box.get_children():
			if c is Button:
				(c as Button).disabled = extra
	if _chk_pass != null:
		_chk_pass.disabled = extra
	if _joseki_opt != null:
		_joseki_opt.disabled = extra
	if _drift_opt != null:
		_drift_opt.disabled = extra

## 界面的「冻结状态」：进复盘 / 她思考中 / 她还有话没播完，各自的冻结范围。
## 纯函数、不碰场景树 ⇒ 门卫可直接断言「进入复盘 ⇒ 冻结；退出 ⇒ 逐项回到进入前」（V11/V21）。
static func review_lock_state(reviewing: bool, busy: bool, speech_hold: bool) -> Dictionary:
	var locked := busy or speech_hold
	return {
		"readonly": reviewing,                  # 棋盘只读：**只在复盘**（不接受落子与摸头）
		"locked": locked,                       # 她思考中 / 她的语音还没播完
		"buttons_disabled": locked or reviewing,   # 四个功能按钮：锁着或复盘中都置灰
		"chat_editable": not reviewing,         # 聊天：复盘期间禁用（她忙时照旧可排队）
		# 别的会改动**真实棋局**的入口（I1）：`⏪ 俗手回档` 按钮（`_bad_box`，每次 state 事件都会重建）
		# 与两个开关（允许虚着 → POST /api/config、定式导学）。后端刻意不门禁 /api/undo 与 /api/config，
		# 所以只能靠前端冻住——但**只有复盘**冻它们：她忙碌时回档 / 改配置是既有且被支持的玩法，不许回退。
		"extra_disabled": reviewing,
	}

## 能不能进复盘（I2）：正在跑回合（如「🏁 结束」的总结还在途中）或她还有语音没播完时**先别进**。
## 后端 `set_review` 允许 busy 进入、且**不取消**在途回合 ⇒ 那段台词 + 语音 + 气泡会落到复盘期间，
## 破掉「回看全程不出声」。纯函数、门卫可直接断言。
static func review_can_enter(busy: bool, speech_hold: bool) -> bool:
	return not (busy or speech_hold)

## 复盘翻页的目标手数：`cur + delta` 夹到 `0..total`（上界＝总手数，下界＝0 手）。
## 纯函数、不碰场景树（V11）；`delta` 先夹到 ±total，`cur` 越界也照夹——脏值一律不抛、不越界。
static func review_step_target(cur: int, delta: int, total: int) -> int:
	var t := maxi(0, total)
	var d := clampi(delta, -t, t)
	return clampi(cur + d, 0, t)

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
			if agent != null and not agent.busy:
				pairing.mark_text_only()     # 回档提示只有文字（后端同步方法，T20②）
			agent.undo(idx))
		_bad_box.add_child(btn)
	# 复盘期间重建出来的按钮**必须是灰的**（I1）：每次 `state` 事件（含复盘翻页的只读快照）都会走到这里
	_refresh_lock()

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
	agent.joseki_rate = cfg.joseki_rate         # 定式提示频率（与开关同一格）：同一通道推给后端
	# 情绪波动与聊天摘要走**原子入口**（v3.8.0 修复波 / #5）：与 P7 的「不退回两次 setter 各推一次」同一口径
	# ——「开关 + 基础概率」一次落定、只推一次 `/api/config`（单字段的 `set_digest` 同一形状）。
	agent.set_drift(cfg.emotion_drift, cfg.drift_base)
	agent.set_digest(cfg.chat_digest_llm)
	agent.set_profile(cfg.profile_enabled, cfg.time_aware_enabled)   # v3.9.0：画像 / 时间感知两个开关
	_wire_agent()
	_set_status("后端模式：编排与引擎在 %s（本地不启动 KataGo/LLM）" % cfg.backend_url)

## 客户端侧语音：只负责播放（后端按 speech 事件送来的音频 / 系统语音指令），不合成、不起服务
func _build_client_tts() -> void:
	tts = TTSClient.new()
	tts.enabled = cfg.tts_enabled
	tts.mode = cfg.tts_mode
	tts.voice = cfg.tts_voice
	tts.speed = cfg.tts_speed
	tts.pitch = cfg.tts_pitch
	tts.volume = cfg.tts_volume
	add_child(tts)
	tts.allow_fillers = false                     # 开场问候/闲置搭话等非玩家触发的语音不插语气词，等真正对局互动再放行
	tts.status.connect(_on_tts_status)
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
	agent.speech_event.connect(_on_speech_event)  # 语音指令（合成在后端）：取音频 / 调系统语音 + 与台词配对
	agent.chat_queue_changed.connect(func(): _refresh_chat_queue_hint())   # 排队内容变了：刷新右下角提示
	var mood_timer := Timer.new()                 # 三维情绪向基线回落的心跳：2 秒一次，区域没变时零开销
	mood_timer.wait_time = 2.0
	mood_timer.autostart = true
	mood_timer.timeout.connect(func(): agent.tick_mood())
	add_child(mood_timer)
	agent.sync_mood(true)                         # 初始「平静」立即生效（表情/音高一次对齐）

## 后端连接状态：连上/断开提示进状态栏。断开时在途的语音事件已经丢了，别让文字一直等它。
func _on_net_status(text: String, bad: bool) -> void:
	_set_status(text, bad)
	if bad:
		pairing.release_stale(_pair_facts(), true)

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
## + 浮层参考图卡片（只渲染后端给的字段：name / line / k / candidates / mistake）
func _on_joseki_event(payload: Dictionary) -> void:
	if _joseki_card != null:
		_joseki_card.apply_event(payload)
	if board == null:
		return
	var pts := joseki_points(payload)
	if pts.is_empty():
		return
	board.highlight_points(pts, str(payload.get("kind", "formed")))

## 后端 `joseki` 事件的 `points` → `Board3D.highlight_points` 认的 `Vector2i` 数组。
## WebSocket 送来的是 JSON：后端的 Python 元组 `(x, y)` 到这里是数组 `[x, y]`（也可能是 `{x, y}`），
## 而 `Board3D.highlight_points` 只认 `Vector2i`（非 `Vector2i` 一律跳过）——不在这里转换，高亮轨迹
## 就一笔都画不出来。纯函数、不碰场景树，门卫可直接调（`preload("res://scripts/ui/Main.gd")`）。
static func joseki_points(payload: Dictionary) -> Array[Vector2i]:
	var out: Array[Vector2i] = []
	var raw = payload.get("points", [])
	if typeof(raw) != TYPE_ARRAY:
		return out                      # 非列表（脏值）：一个都不画
	for it in raw:
		var p := _joseki_point(it)
		if p.x >= 0:
			out.append(p)
	return out

## 单个 `points` 项 → `Vector2i`：`[x, y]` 与 `{x, y}` 两种形态都认；脏值 / 出盘 / `pass` → `(-1, -1)`
static func _joseki_point(v) -> Vector2i:
	var x := -1
	var y := -1
	if typeof(v) == TYPE_ARRAY:
		var a: Array = v
		if a.size() >= 2:
			x = _int_coord(a[0])
			y = _int_coord(a[1])
	elif typeof(v) == TYPE_DICTIONARY:
		var d: Dictionary = v
		x = _int_coord(d.get("x", null))
		y = _int_coord(d.get("y", null))
	return Vector2i(x, y) if Coords.valid(x, y) else Vector2i(-1, -1)

## JSON 整数在前端是 float（x.0），这里只认整数值；字符串 / null / `1.5` / `"pass"` → `-1`
static func _int_coord(v) -> int:
	if typeof(v) == TYPE_INT:
		return v
	if typeof(v) == TYPE_FLOAT and is_finite(float(v)) and float(v) == floor(float(v)):
		return int(v)
	return -1

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
	if pairing.has_pending() and (text.contains("失败") or text.contains("无响应") or text.contains("无法")):
		_flush_pending_ai()              # 语音出错：文字不能丢，立即显示

func _greet() -> void:
	var greet := "嗯~ %s，来下棋吧...我会陪你的 (｡･ω･｡)" % cfg.prompt_user_name
	if tts != null and tts.play_welcome(cfg.prompt_user_name):
		_add_msg_now("ai", greet)             # 预制欢迎语音已经开口：文字直接显示，零等待
	else:
		pairing.mark_text_only()              # 本地问候没有后端语音（预制欢迎语音缺失时）：文字直接显示
		_add_msg("ai", greet)
	_add_msg("system", "你执黑先行。左键落子，右键拖拽可旋转棋盘视角；Home 回正视角，C 依次看四个角。")

func _on_intersection_clicked(x: int, y: int) -> void:
	_allow_tts_fillers()
	agent.player_move(x, y)

func _on_state_changed(p: Dictionary) -> void:
	board.set_stones(p["stones"])
	board.set_move_seq(p.get("move_seq", []))       # 悬停已有棋子时推手数用（后端 state 自带 GTP 手顺）
	var mc := int(p["move_count"])
	if mc > _last_move_count:                       # Azure 每落一子：先注视自己的落点，再交还鼠标跟随
		var stone := _last_stone(p["stones"])
		if not stone.is_empty() and str(stone["c"]) == "white":
			board.look_at_board_point(board.stone_world(int(stone["x"]), int(stone["y"])), 2.0)
	_last_move_count = mc
	if _reviewing:
		_review_cur = mc                                # 复盘翻页的进度以后端推来的只读快照为准
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
	_lbl_score.text = score_line(sc)                       # 原始数字照旧保留
	if _lbl_score_hint != null:
		_lbl_score_hint.text = score_hint(sc, wr)          # 下面那行量级人话

## 目差的量级人话（四档；与 `winrate_band` 同一套档位编号）
const SCORE_PHRASES := ["差不多", "半个角", "一个角的实空", "已经很难追了"]

## 脏值 / null ⇒ NAN。**不认字符串**：`float("abc")` 会悄悄给 0.0，那会把脏值说成「差不多」。
static func _metric(v) -> float:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			return float(v)
		_:
			return NAN

## 目差的量级档位（按 |目差| 划分；未知 / 脏值 ⇒ -1）
static func score_band(score) -> int:
	var s := _metric(score)
	if is_nan(s) or is_inf(s):
		return -1
	var a := absf(s)
	if a < 2.5:
		return 0
	if a < 6.5:
		return 1
	if a < 12.5:
		return 2
	return 3

## 胜率的量级档位（按领先方距 50% 的差划分，与上面同一套编号；未知 / 脏值 ⇒ -1）
static func winrate_band(winrate) -> int:
	var w := _metric(winrate)
	if is_nan(w) or is_inf(w):
		return -1
	var d := absf(clampf(w, 0.0, 1.0) - 0.5)
	if d < 0.05:
		return 0
	if d < 0.15:
		return 1
	if d < 0.30:
		return 2
	return 3

## 目差说人话（静态纯函数，门卫直接断言）：**两个信号一起看，取更保守（更接近均势）的那一档**。
## 目差与胜率偶尔打架（胜率 51% + 目差 -2.5 是正常的），取小档保证措辞永远不会比另一个信号更笃定，
## 读起来不矛盾；两个信号同调时该笃定就笃定。措辞本身不带「谁领先」（正负由数字自己说）。
## 脏值 / null ⇒ "--"。
static func score_phrase(score, winrate) -> String:
	var sb := score_band(score)
	if sb < 0:
		return "--"
	var wb := winrate_band(winrate)
	return SCORE_PHRASES[mini(sb, wb) if wb >= 0 else sb]

## 目差那一行的文案：**原始数字仍然显示**（精度不丢）；没有信息时沿用既有「目差: --」
static func score_line(score) -> String:
	var s := _metric(score)
	return "目差: --" if is_nan(s) or is_inf(s) else "目差: %+.1f" % s

## 目差下面那行小字：量级人话；没有信息时给空串（空串只占位、不显示）
static func score_hint(score, winrate) -> String:
	var p := score_phrase(score, winrate)
	return "" if p == "--" else p

func _on_busy_changed(busy: bool) -> void:
	if not busy:
		_tool_active = false               # 回合结束：某个 start 的 end 万一没到，别让它卡住下一次 start
	if avatar != null:
		avatar.set_thinking(busy)          # 思考中回到中性表情，空闲时恢复常态微笑
	if busy:
		_show_thinking()                   # 聊天栏先占位「Azure 思考中……」
	else:
		pairing.release_stale(_pair_facts())   # 回合结束：等不到语音的文字直接放出来（落子也照常揭示）
		_speech_hold = _speech_busy()      # Azure 还有话没播完（含音频还在取）：继续锁住棋盘，等她说完
		if not _speech_hold:
			_reveal_move()                 # 兜底：语音不可用/未播时，也要把落子显示出来
	_refresh_lock()
	_clear_thinking_if_idle()

func _on_allow_pass_toggled(pressed: bool) -> void:
	pairing.mark_text_only()                 # 这条提示只有文字（后端同步方法，T20②）
	agent.set_allow_pass(pressed)

## 定式导学的四档频率（与下拉框 index-1 一一对应）：低 25% / 中 50% / 高 75%
const JOSEKI_RATES := [0.25, 0.5, 0.75]

## 由 (开关, 频率) 反推下拉框档位：关闭 ⇒ 0；否则取最接近 `joseki_rate` 的一档，
## **并列时取「中 50%」**（两侧同时等距时优先中间档）。纯函数、不碰场景树（门卫可直接断言）。
static func joseki_level_index(teach: bool, rate: float) -> int:
	if not teach:
		return 0
	var best := 2                                 # 初始就压在「中 50%」上：并列时它胜出
	var best_d := INF
	for i in JOSEKI_RATES.size():
		var d := absf(rate - JOSEKI_RATES[i])
		var idx := i + 1
		if d < best_d or (is_equal_approx(d, best_d) and absi(idx - 2) < absi(best - 2)):
			best_d = d
			best = idx
	return best

## 情绪波动的三档基础概率（与下拉框 index-1 一一对应）：低 3% / 中 5% / 高 6%。
## ⚠ **每一档都必须明显低于后端 `drift_max`（默认 0.08）**：`emotion_bias` 只往上加，档位一旦
## 顶到上限，该档就被夹平成「固定概率」，情绪再也不参与（修复轮 1 / Important #2）。最高档因此
## 从 8% 降到 6%（留出 2 个百分点的情绪余量）。
const DRIFT_BASES := [0.03, 0.05, 0.06]

## 由 (开关, 基础概率) 反推下拉框档位：关闭 ⇒ 0；否则取最接近 `drift_base` 的一档，
## **并列时取更低的那一档**（偏航宁可保守：这是「她可能下偏」的旋钮，不是玩法难度）。
## 纯函数、不碰场景树（门卫可直接断言，V11 同款做法）。
static func drift_level_index(on: bool, base: float) -> int:
	if not on:
		return 0
	var best := 1                                 # 初始压在最低档：并列时它胜出
	var best_d := INF
	for i in DRIFT_BASES.size():
		var d := absf(base - DRIFT_BASES[i])
		var idx := i + 1
		if d < best_d or (is_equal_approx(d, best_d) and idx < best):
			best_d = d
			best = idx
	return best

## ⚙ 设置抽屉：只切可见性，不改任何开关的值（抽屉里的开关仍各自写回 config.json）
func _on_settings_toggled(on: bool) -> void:
	if _settings_box != null:
		_settings_box.visible = on
	if _btn_settings != null:
		_btn_settings.text = "⚙ 设置 ▾" if on else "⚙ 设置 ▸"

# ================= 画像面板（只读，v3.9.0 / Q6） =================

## 面板里各维的人话标签（键名照 spec §6.2；未列出的键一律不渲染——未知键忽略、不抛）。
## `persona.topic.*` 是前缀族，走 `_profile_axis_label` 里的前缀分支。
const PROFILE_AXIS_LABELS := {
	"open.star": "开局·星位", "open.komoku": "开局·小目",
	"open.sansan": "开局·三三", "open.mokuhazushi": "开局·目外",
	"open.favorite_kakari": "挂角倾向", "style.aggression": "爱缠斗",
	"style.tenuki": "常脱先", "style.territory_vs_influence": "势力倾向",
	"blunder.rate": "失误率", "blunder.top_type": "常亏手型",
	"weak.corner_tl": "左上易亏", "weak.corner_tr": "右上易亏",
	"weak.corner_bl": "左下易亏", "weak.corner_br": "右下易亏",
}

## 一维的显示名；未知键 / 空的 `persona.topic.` ⇒ 空串（＝不渲染）。
static func _profile_axis_label(axis: String) -> String:
	if PROFILE_AXIS_LABELS.has(axis):
		return str(PROFILE_AXIS_LABELS[axis])
	if axis.begins_with("persona.topic."):
		var t := axis.substr("persona.topic.".length()).strip_edges()
		return ("常聊·" + t) if t != "" else ""
	return ""

## 数值容错（与 `AzureConfig` 的守卫同一口径）：非数值 / NaN / 脏字符串 ⇒ `default`。
static func _profile_num(v, default := 0.0) -> float:
	match typeof(v):
		TYPE_INT, TYPE_FLOAT:
			var f := float(v)
			return default if is_nan(f) else f
		TYPE_STRING:
			var s := (v as String).strip_edges()
			return s.to_float() if s.is_valid_float() else default
		_:
			return default

## 一维的值 → 一行可读文本：数值维按百分比（画像块的刻度就是 `[0,1]`），非数值维（手型）原样显示。
static func _profile_value_text(v) -> String:
	if typeof(v) == TYPE_STRING:
		return (v as String).strip_edges()
	if typeof(v) == TYPE_INT or typeof(v) == TYPE_FLOAT:
		var f := _profile_num(v, -1.0)
		if f < 0.0:
			return "?"
		return "%d%%" % int(round(f * 100.0))
	return "?"

## 画像面板文本（**纯函数**，headless 门卫直接调它）：把 `GET /api/profile` 的返回渲染成只读文本。
## 四种退化都要有可读文案：后端没返回 / 请求被拒 / 功能关着 / 还没攒下画像。未知维忽略、脏值不抛。
static func profile_panel_text(payload) -> String:
	if typeof(payload) != TYPE_DICTIONARY:
		return "读取失败：后端没有返回可用数据（先确认后端已启动）"
	var p: Dictionary = payload
	if not bool(p.get("ok", false)):
		return "读取失败：后端拒绝了这次请求"
	if not bool(p.get("enabled", false)):
		return "画像功能关着。打开上面的「玩家棋风画像」，下完一局并点「🏁 结束」之后，这里就会有内容。"
	var axes = p.get("axes", {})
	var lines := ""
	var n_axes := 0
	if typeof(axes) == TYPE_DICTIONARY:
		var keys: Array = (axes as Dictionary).keys()
		keys.sort()                                  # 键名排序：面板顺序稳定、可断言
		for key in keys:
			var label := _profile_axis_label(str(key))
			if label == "":
				continue
			var rec = (axes as Dictionary)[key]
			var value = rec
			var conf := 0.0
			var n := 0
			if typeof(rec) == TYPE_DICTIONARY:
				value = (rec as Dictionary).get("value")
				conf = _profile_num((rec as Dictionary).get("confidence", 0.0))
				n = int(_profile_num((rec as Dictionary).get("n_games", 0)))
			if value == null:
				continue
			var row := "%s：%s（把握 %d%%，%d 局）" % [label, _profile_value_text(value),
				int(round(clampf(conf, 0.0, 1.0) * 100.0)), n]
			lines += ("\n" if lines != "" else "") + row
			n_axes += 1
	if n_axes == 0:
		return "还没有攒下画像——下完一局并点「🏁 结束」之后就会有一条。"
	var games = p.get("games", [])
	var n_games := (games as Array).size() if typeof(games) == TYPE_ARRAY else 0
	return "她眼中的你（最近 %d 局记录）" % n_games + lines

## 「🪞 她眼中的你」按钮：只读拉一次 `/api/profile` 并把结果铺到下面那块文本上（无副作用）。
func _on_profile_panel() -> void:
	if _lbl_profile == null:
		return
	_lbl_profile.visible = true
	if agent == null:
		_lbl_profile.text = "读取失败：后端还没连上（状态栏会提示连接情况）"
		return
	_lbl_profile.text = "读取中…"
	agent.fetch_profile(func(ok: bool, code: int, body: PackedByteArray) -> void:
		if _lbl_profile == null:
			return
		if not ok:
			_lbl_profile.text = "读取失败（后端没连上或返回了 %d）" % code
			return
		var d = JSON.parse_string(body.get_string_from_utf8())
		_lbl_profile.text = profile_panel_text(d))

## 定式导学「开关 + 频率」下拉（右侧栏）：写回 config.json，再走既有 `_flush_settings` 通道推给后端，
## 并对当前对局立即生效。「关闭」只关开关、**不动 joseki_rate**（重新开启时频率能回来）。
func _on_joseki_level_selected(i: int) -> void:
	var lvl := clampi(i, 0, JOSEKI_RATES.size())
	var teach := lvl > 0
	var rate: float = float(JOSEKI_RATES[lvl - 1]) if teach else cfg.joseki_rate
	if teach == cfg.joseki_teach and is_equal_approx(rate, cfg.joseki_rate):
		return                       # 程序化赋值也可能触发该信号：值没变就不写配置
	cfg.joseki_teach = teach
	cfg.joseki_rate = rate
	cfg.save()
	if agent != null:
		# 原子入口（v3.7.0 修复波）：开关与频率**一次推一次** `/api/config`。分两次赋 setter 会各推
		# 一条 POST、到达顺序无保证，极端情况下后端留下「新开关 + 旧频率」。
		agent.set_joseki(teach, rate)
	var names := ["关闭", "低 25%", "中 50%", "高 75%"]
	_set_status("定式导学：%s" % names[lvl])

## 情绪波动「开关 + 基础概率」下拉（⚙ 设置抽屉）：写回 config.json，再走既有 `_flush_settings` 通道推给
## 后端，并对当前对局立即生效。「关闭」只关开关、**不动 drift_base**（重新开启时概率能回来）。
func _on_drift_level_selected(i: int) -> void:
	var lvl := clampi(i, 0, DRIFT_BASES.size())
	var on := lvl > 0
	var base: float = float(DRIFT_BASES[lvl - 1]) if on else cfg.drift_base
	if on == cfg.emotion_drift and is_equal_approx(base, cfg.drift_base):
		return                       # 程序化赋值也可能触发该信号：值没变就不写配置
	cfg.emotion_drift = on
	cfg.drift_base = base
	cfg.save()
	if agent != null:
		# 原子入口（v3.8.0）：开关与基础概率**一次推一次** `/api/config`（与 `set_joseki` 同一个理由）
		agent.set_drift(on, base)
	var names := ["关闭", "低 3%", "中 5%", "高 6%"]
	_set_status("情绪波动：%s" % names[lvl])

func _send_chat() -> void:
	var t := _chat_input.text
	if t.strip_edges() == "":
		return
	if agent != null and not agent.chat(t):
		# 队列满了：**不吞玩家的话**——原文留在输入框，只在状态栏提示（聊天栏不出现这条）
		_set_status("排队已满（最多 %d 条），等她说完了再发" % ChatQueue.CHAT_QUEUE_MAX)
		return
	_allow_tts_fillers()
	_add_msg("user", t)
	_chat_input.text = ""

## 右下角那行排队提示：`排队中 N 条 · 队头前几个字`（队列空 ⇒ 隐藏）
func _refresh_chat_queue_hint() -> void:
	if _lbl_queue == null:
		return
	var queued: bool = agent != null and agent.has_queued_chat()
	_lbl_queue.text = agent.queued_chat_hint() if queued else ""
	_lbl_queue.visible = queued

## 她已经处于「这一轮结束、语音也播完」（`hold` 为假）时，把玩家排队的聊天自动发下一条——
## 玩家不用再发一条来「催」。队列空时每帧只多一次判断，零开销；`flush_queued_chat` 自己还有
## 「没连上后端就不发 / 失败节流窗口」两道闸。
func _tick_chat_queue(hold: bool) -> void:
	if agent == null or hold or not agent.has_queued_chat():
		return
	agent.flush_queued_chat({"busy": agent.busy, "speaking": tts != null and tts.is_speaking()})

## 玩家主动发起对局互动：允许 TTS 在合成等待时插「嗯…」这类语气词。
## 同时把还在合成/排队/播放的走神搭话全部作废——人已经回来下棋了，别再追着问「在忙什么呀」
func _allow_tts_fillers() -> void:
	_cancel_idle_speech()
	if tts != null:
		tts.allow_fillers = true
		tts.begin_filler_turn()          # 本回合语气词限次（think 至多两次，且不连着播同一句）

## 作废走神搭话：文字与语音（在途取音频、播放中、排队中）一起清掉，让新的内容立刻顶上
func _cancel_idle_speech() -> void:
	_idle_muted = true
	if _pending_batch_kind == "idle":
		_batch_pending = false
		_pending_batch_kind = ""
		_batch_token += 1                # 让还在路上的去抖计时器失效
	var kept_q: Array[Dictionary] = []
	for e in _msg_queue:
		if str((e as Dictionary).get("kind", "")) == "ai_idle":
			continue                     # 投递队列里还没开口的走神搭话：一并作废
		kept_q.append(e)
	_msg_queue = kept_q
	_queue_rush_seq = _msg_seq           # 其余积压快进（玩家刚动手，别让她慢慢来）
	if tts != null:
		tts.cancel_kind("idle")
	pairing.forget_idle()                # 配对表里走神搭话的文字一并作废（含它自己那条 speech 的占位）
	_speech_hold = _speech_busy()
	_refresh_lock()
	_clear_thinking_if_idle()

func _ask_reset() -> void:
	_ask_note("重新开始？当前棋局与俗手记录都会清空。", func(_note):
		_allow_tts_fillers()
		pairing.mark_text_only()             # 重来提示只有文字（后端同步方法，T20②）
		_game_ended = false                  # 新的一局：复盘入口收起（V19，本地标志）
		_refresh_review_entry()
		agent.reset_game())

func _ask_end() -> void:
	_ask_note("对局总结：想对Azure说什么吗？（可留空直接确定）", func(note):
		_allow_tts_fillers()
		_game_ended = true                   # 「🏁 结束」流程走完：复盘入口出现（V19；不读 st.finished）
		_refresh_review_entry()
		agent.summarize(note))

# ================= 局后只读复盘（v3.7.0 任务 D） =================
# 反目标：回看全程她**不出声**（后端已冻结，前端也绝不为此发 chat/idle/hint/analyze）、界面不更挤
# （入口只在局后出现）。只读范围：棋盘（落子与摸头同源，Board3D.placement_allowed）+ 四个功能按钮
# + 聊天输入。冻结与恢复都经由既有的 `_refresh_lock()`（V20/V21），不另起一套互斥逻辑。

## 复盘入口 / 退出（📖 复盘 ↔ ✅ 退出复盘）。三个端点都走 `Main.gd` 的 `fetch_bytes`（V18：
## 不给 `NetClient.gd` 加方法）。
## 入口闸门（I2）：`🏁 结束` 的总结还在途中（`busy`）或她的语音没播完（`_speech_hold`）时**拒绝进入**——
## 后端允许 busy 进复盘且不取消在途回合，那样总结的台词 + 语音会落到复盘期间（破「回看全程不出声」）。
func _on_review_toggle() -> void:
	if _reviewing:
		_exit_review()
		return
	if not review_can_enter(agent != null and agent.busy, _speech_hold):
		_set_status("她还在总结这一局（或语音还没说完），等这一轮收尾再复盘")
		return
	_enter_review()

## 进入复盘：POST /api/review {"on": true} → 冻结 UI + 空闲收尾 + 用**新鲜**的 state 对齐总手数 + 拉存档。
## **不推盘面**：棋盘上本来就是终局那手，玩家按 ◀ 往回看即可。
func _enter_review() -> void:
	if agent == null:
		_set_status("后端未连接：无法进入复盘")
		return
	agent.fetch_bytes("/api/review", HTTPClient.METHOD_POST, {"on": true},
		func(ok: bool, code: int, body: PackedByteArray):
			if not ok:
				_set_status("进入复盘失败：%s" % _api_error_text(code, body))
				return
			_reviewing = true
			_cancel_idle_speech()                       # 复盘期间绝不出声：作废还没开口 / 还在合成的走神搭话（V20）
			_review_total = _last_move_count            # 先给个下界；下面用新鲜 state 覆盖（Minor #6）
			_review_cur = _review_total
			_btn_prev.visible = true
			_btn_next.visible = true
			_btn_review.text = "✅ 退出复盘"
			_refresh_lock()                             # 冻结（只读 / 置灰 / 禁聊天 / 俗手回档与开关）在这里一处收口
			_set_status("复盘：只读回看中 · 第 %d / %d 手（← → 或按钮翻页）"
					% [_review_cur, _review_total])
			_load_review_notes()
			# 总手数取**新鲜**的 `GET /api/state`：本地 `_last_move_count` 可能因 WS 断线而落后（Minor #6）。
			# 复用的 `_on_state_changed` 顺便把棋盘 / 手数 / 俗手回档也对齐一次（它内部会同步 `_review_cur`）。
			agent.fetch_bytes("/api/state", HTTPClient.METHOD_GET, {},
				func(ok2: bool, code2: int, body2: PackedByteArray):
					if not ok2 or not _reviewing:
						return
					var st := _json_dict(body2)
					if not st.is_empty():
						_review_total = int(st.get("move_count", _review_total))
						_on_state_changed(st)))

## 退出复盘：POST /api/review {"on": false} → 逐项恢复 + **重新同步**。
## 后端退出复盘**不推 state**（V18）：棋盘上还留着复盘到的那一手，必须自己 `GET /api/state` 拉回终局。
## 退出**什么都不改**（V21）：不调 undo / reset。复盘期间「什么都不改」靠的是**前端把改动入口全冻住**——
## 除四个功能按钮与聊天外，`⏪ 俗手回档`（走 /api/undo）与两个开关（走 /api/config）也一并置灰（I1），
## 后端刻意不门禁这两个端点，所以复盘期间 `st` 确实一个字都没被改过，重同步只是把显示拉回来。
func _exit_review() -> void:
	if agent == null:
		return
	agent.fetch_bytes("/api/review", HTTPClient.METHOD_POST, {"on": false},
		func(ok: bool, code: int, body: PackedByteArray):
			if not ok:
				_set_status("退出复盘失败：%s" % _api_error_text(code, body))
				return
			_reviewing = false
			_btn_prev.visible = false
			_btn_next.visible = false
			_btn_review.text = "📖 复盘"
			_refresh_lock()                             # 只读 / 四个按钮 / 聊天逐项回到进入前（V21）
			agent.fetch_bytes("/api/state", HTTPClient.METHOD_GET, {},
				func(ok2: bool, code2: int, body2: PackedByteArray):
					if not ok2:
						_set_status("退出复盘后同步失败：%s" % _api_error_text(code2, body2))
						return
					var st := _json_dict(body2)
					if not st.is_empty():
						_on_state_changed(st)           # 复用既有 state 处理：棋盘 / 手数 / 虚着 / 俗手回档一起回位
					_set_status("已退出复盘，回到当前局面")))

## 翻页：POST /api/review/goto {"n"}（后端推一份**只读** state 快照回来）。
## 夹取逻辑在静态 `review_step_target`（纯函数、门卫直接测）；这里只发请求 + 报进度（V11/V18）。
func _review_step(delta: int) -> void:
	if not _reviewing or agent == null:
		return
	var target := review_step_target(_review_cur, delta, _review_total)
	if target == _review_cur:
		return                                          # 已经在头 / 尾：不发冗余请求
	var prev := _review_cur
	_review_cur = target                                # 乐观推进：连按两下按的是新位置
	agent.fetch_bytes("/api/review/goto", HTTPClient.METHOD_POST, {"n": target},
		func(ok: bool, code: int, body: PackedByteArray):
			if not _reviewing:
				return                                  # 已经退出复盘：这份迟到的回包什么都不改（Minor #7）
			if not ok:
				_review_cur = prev                      # 显示没动，计数也退回去（她自己会由 state 校准）
				_set_status("复盘翻页失败：%s" % _api_error_text(code, body))
				return
			_set_status("复盘：第 %d / %d 手" % [_review_cur, _review_total]))

## 复盘入口的显隐：只在**对局结束后**出现（V19 的本地标志，不改后端 `finished` 语义）。
func _refresh_review_entry() -> void:
	if _review_box != null:
		_review_box.visible = _game_ended

## 复盘翻页键（← / →）：**只在复盘模式内生效**，绝不抢聊天框的键盘输入（V20）。
## 走 `_input`（最早一步）而不是 `_unhandled_input`：复盘期间聊天框已被禁用（`editable=false`），
## 而焦点在按钮上时方向键会在 GUI 阶段被吃掉去做焦点导航——放到最早才抢不到。
## 复盘模式外立刻返回，键盘行为与以前一字不差。
func _review_keys(event: InputEvent) -> void:
	if not _reviewing or not (event is InputEventKey):
		return
	var k := event as InputEventKey
	if not k.pressed or k.echo:
		return
	if k.keycode == KEY_LEFT:
		_review_step(-1)
		get_viewport().set_input_as_handled()
	elif k.keycode == KEY_RIGHT:
		_review_step(1)
		get_viewport().set_input_as_handled()

## 拉本局逐手存档（GET /api/review/notes）并**只渲染**到聊天区：**一条** system 气泡（不刷屏）、
## 不登记语音配对、不触发任何 LLM/TTS ⇒ 复盘期间她一个字都不说。
## 拉不到就静默跳过（存档是锦上添花，不影响回看）。
func _load_review_notes() -> void:
	if agent == null:
		return
	agent.fetch_bytes("/api/review/notes", HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if not ok:
				return
			var d := _json_dict(body)
			if d.is_empty():
				return
			var lines := review_note_lines(d.get("notes", []), d.get("losses", []))
			if lines.is_empty():
				_add_msg("system", "复盘：这一局没有留下逐手存档")
				return
			var joined := ""
			for l in lines:
				joined += l + "\n"
			_add_msg("system", "复盘存档（%d 条）：\n%s" % [lines.size(), joined.strip_edges(false, true)]))

## 逐手存档 / 亏损手账 → 聊天区的纯文本行（**只渲染后端字段，不评判**，V18）。
## 纯函数、不碰场景树，门卫可直接调（与 `joseki_points` 同一套做法：`preload("res://scripts/ui/Main.gd")`）。
static func review_note_lines(notes, losses) -> Array[String]:
	var out: Array[String] = []
	if typeof(notes) == TYPE_ARRAY:
		for n in notes:
			if typeof(n) != TYPE_DICTIONARY:
				continue                                # 脏值跳过
			var d: Dictionary = n
			var text := str(d.get("text", "")).strip_edges()
			if text == "":
				continue                                # 空文本不算内容（后端不过滤，前端也不硬造）
			var who := "你" if str(d.get("side", "")) == "E" else "Azure"
			var what := "用意" if str(d.get("kind", "")) == "aim" else "感想"
			out.append("第%d手 · %s的%s：%s" % [int(d.get("move_no", 0)), who, what, text])
	if typeof(losses) == TYPE_ARRAY:
		for l in losses:
			if typeof(l) != TYPE_DICTIONARY:
				continue
			var d2: Dictionary = l
			out.append("亏损手 第%d手 %s：%s" % [int(d2.get("move_no", 0)), str(d2.get("gtp", "")),
				str(d2.get("reason", ""))])
	return out

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
	_review_keys(event)

func _bump_activity() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	if _idle:
		_idle = false
		_set_idle_state(false)

func _process(delta: float) -> void:
	var now := Time.get_ticks_msec()
	# 复盘期间也按「她占着棋盘」处理（M3）：闲置计时停摆、聊天队列不续发——否则玩家回看时会被算成闲置，
	# 一退出复盘她就立刻来一句（而且复盘期间本来就该静音）。
	var hold := _azure_hold_active() or _reviewing
	if hold:
		# 她还在思考 / 朗读 / 有句子没播完：棋盘锁着，玩家此刻根本无法落子，
		# 这段时间不能算「玩家闲置」——否则会出现她边堵着棋盘边问「你在忙什么呀？还要继续下棋吗」
		_last_activity_ms = now
		if _idle:
			_idle = false
			_set_idle_state(false)
	_tick_chat_queue(hold)                              # 她这一轮结束、语音也播完：自动续发排队的聊天
	if not _idle and now - _last_activity_ms >= int(IDLE_SEC * 1000.0):
		_idle = true                                    # 长时间没操作：看向玩家、准备歪头
		_set_idle_state(true)
		_next_talk_ms = now + int(IDLE_TALK_FIRST * 1000.0)
	# 复盘期间绝不出声（V20/M3）：走神搭话本来该由后端门禁挡住，前端也不许发——不依赖 Task C。
	if _idle and now >= _next_talk_ms and not _reviewing and agent != null and not agent.busy:
		_next_talk_ms = now + randi_range(int(IDLE_TALK_MIN * 1000.0), int(IDLE_TALK_MAX * 1000.0))
		_idle_muted = false                             # 新的一轮搭话：先解除「玩家已回来」的作废标记
		agent.idle_remark()                             # 长时间没动静：主动问一句（后端 POST /api/idle）
	for text in pairing.prune_expired():                 # 等不到语音的配对位清出，别让它吃掉后面那条
		print("[TTS] 等不到语音的台词已从配对表清出：%s" % text.substr(0, 20))
	pairing.release_stale(_pair_facts())                # 回合结束后还等不到语音的文字别一直等
	_sync_speech_hold()                                 # 语音侧锁的兜底对齐（音频 404/取不回时不会锁死棋盘）
	_update_thinking(now)
	_tick_typing(delta)                                 # 打字节奏推进（AI 消息逐字显示）
	_tick_joseki_card()                                 # 浮层卡片的避让重算（几何算在这里，卡片只认矩形）

## 定式浮层卡片的每帧避让：把「棋盘落点热区」「鼠标附近的小方块」「展开的侧栏」喂给卡片。
## 几何计算留在本文件（U39：卡片只认矩形，U39 里那两个是最小集）；兜底 TTL 由卡片自己的 `_process` 推进。
func _tick_joseki_card() -> void:
	if _joseki_card == null:
		return
	var mp := get_viewport().get_mouse_position()
	var rects: Array = [
		_board_screen_rect(),
		Rect2(mp - Vector2(48.0, 48.0), Vector2(96.0, 96.0)),
	]
	if _left_open and _left_panel != null:
		rects.append(_left_panel.get_global_rect())    # 展开的左侧栏也算「别挡」：不挤压既有控件
	if _right_open and _right_panel != null:
		rects.append(_right_panel.get_global_rect())
	_joseki_card.set_panel_overlap(rects)

## 棋盘（19 路）在屏幕上的包围盒：四角经棋盘相机 `unproject_position` 出来再取包围盒。
## 相机背后的角要丢掉：它的 unproject 会翻到画面另一侧，把包围盒撑到整个视口（＝永久 faded），
## 与 `Board3D` 的摸头判定（`Board3D.gd:374`）用同一个守卫（M6）。
func _board_screen_rect() -> Rect2:
	if board == null or board.cam == null:
		return Rect2()
	var cam := board.cam
	var lo := Vector2(INF, INF)
	var hi := Vector2(-INF, -INF)
	for c: Vector2i in [Vector2i(0, 0), Vector2i(18, 0), Vector2i(0, 18), Vector2i(18, 18)]:
		var w := board.stone_world(c.x, c.y)
		if cam.is_position_behind(w):
			continue
		var p := cam.unproject_position(w)
		lo = Vector2(minf(lo.x, p.x), minf(lo.y, p.y))
		hi = Vector2(maxf(hi.x, p.x), maxf(hi.y, p.y))
	if lo.x > hi.x or lo.y > hi.y:
		return Rect2()                                 # 四角全在相机背后：当拿不到棋盘投影
	return Rect2(lo, hi - lo)

## 棋盘锁的语音侧判据每帧对齐一次：音频也可能「根本没来」（404 / 后端断了），
## 只靠 speaking_changed / busy 事件会漏掉那种情况，`_speech_hold` 就会一直挂着把棋盘锁死。
func _sync_speech_hold() -> void:
	if not _speech_hold or _speech_busy():
		return
	_speech_hold = false
	_refresh_lock()

## 她这一侧还没收尾：回合还在跑 / 语音侧还没完（含「音频还在取」）/ 调用方那个闩还挂着。
## 四个「她还在忙吗」谓词里最外层的那个（判据都在 SpeechPairing 里，便于门卫钉一致性）。
func _azure_hold_active() -> bool:
	return pairing.hold_active(_pair_facts(), _speech_hold)

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
		# 注意：这里只代表「这一句播完了」。后面常有排队/还在取的语音，
		# 所以要看整队是否真的空了才算说完；否则句间空隙里棋盘会被提前解锁。
		_speech_hold = _speech_busy()
	_refresh_lock()
	_clear_thinking_if_idle()

## 每段语音开始播放：逐句模式下把与之配对的那条 Azure 文字显示出来（文字与声音成对）。
## 分组号就是配对记录里的 group：语音一开播就按它精确回查，一条台词只显示一次、乱序也不会错位。
func _on_utterance_started(group: int) -> void:
	var rec: Dictionary = pairing.group_of(group)
	if not rec.is_empty():
		pairing.finish(rec)          # 这段语音有结果了：退出配对表（不再占着「在途」）
	if cfg.tts_batch_speak:
		return
	if group <= 0:                   # 防御：试听的音频不登记配对，永远走不到「有待播文字」这一步
		return
	if rec.is_empty() or not pairing.wait_display(rec):
		return                       # 没配对到台词 / 文字早已显示过
	pairing.display(rec)             # 显示文字；`_reveal_move()` 就在这一刻（音画同步，语义未变）

## 这条语音不会再响了（音频坏了 / 被 stop / 队列溢出丢弃）：文字/落子照常显示，只是这一句没有声音
func _on_speech_skipped(group: int) -> void:
	var rec: Dictionary = pairing.group_of(group)
	if not rec.is_empty():
		pairing.finish(rec)
	if cfg.tts_batch_speak:
		_flush_pending_ai()
	elif not rec.is_empty():
		pairing.display(rec)

# ================= 语音配对与播放（计划 05：合成归后端，本端只取音频 + 播放） =================
#
# 配对状态机在 `SpeechPairing`（`game/scripts/services/SpeechPairing.gd`，纯决策 + 可注入时钟）；
# 本文件只留胶水：登记时喂它一条记录、收到 speech 时让它配对、由它告诉我「该显示哪条文字」。
# 后端的出话顺序是**严格串行**的：`message(A)` → 合成 A → `speech(A)` → `message(B)` → …（同步方法
# 里那几条只有文字的短提示会让这个序列多出一个「没有 speech 的 message」）。于是：
#   1) `_add_msg` 为每条要朗读的 AI 台词登记一条**配对记录**（登记顺序 = speech 到达顺序）；
#   2) `_on_speech_event` 交给状态机：按到达顺序取**最早那条还没配上的**记录，并用 `turn_id`+`seq` 递增校验；
#   3) 配对成功后取音频（GET /api/tts/audio/{id}）→ `tts.play_audio(bytes, seq)`，或直接
#      `tts.speak_system(text, seq)`；
#   4) `utterance_started(group)` 回来时 `pairing.group_of(group)` 精确回查 → `pairing.display()`
#      → `display_requested` 信号回到 `_on_pairing_display`：显示文字 + `_reveal_move()`。
# 兜底（保证任何一步出问题都**不会把文字卡死**）：
#   · 音频 404 / 解析不了 → TTSClient 或本端补 `speech_skipped` → 文字立即显示；
#   · 回合结束（busy=false）还等不到 speech（后端那一轮已经发完了）→ `pairing.release_stale` 直接显示；
#   · WS 断开 → 在途 speech 已经丢了 → `pairing.release_stale(..., true)` 全部放出来；
#   · 音频「根本没来」（404）→ `_process` 里 `_sync_speech_hold` 每帧清一次锁，不会锁死棋盘。
# 反过来，本地丢弃的台词（走神作废 / 防复读）会留一条 `tomb` 占位：**吃掉它自己那条 speech**，
# 避免把下一条台词的语音错配到它身上（后端那条语音本来就发出来了）。

## 试听用的一句话（后端 POST /api/tts/speak 合成后回音频）
const PREVIEW_TEXT := "嗯~ 我是 Azure，很高兴见到你，请多指教。"
const TTS_FETCH_TIMEOUT := 60.0     # 取音频/试听合成：比普通 REST 松一点（后端可能正在合成）

## 后端可读错误（409/404 都回 {"ok":false,"error":"..."}）：取它的 error 文案，取不到就回 HTTP 码
static func _api_error_text(code: int, body: PackedByteArray) -> String:
	var d = JSON.parse_string(body.get_string_from_utf8())
	if typeof(d) == TYPE_DICTIONARY and str((d as Dictionary).get("error", "")) != "":
		return str((d as Dictionary)["error"])
	return "HTTP %d" % code

## 响应体 → Dictionary：非 JSON 对象 / 解析失败一律空字典（调用方不必自己判类型）
static func _json_dict(body: PackedByteArray) -> Dictionary:
	var d = JSON.parse_string(body.get_string_from_utf8())
	return d if typeof(d) == TYPE_DICTIONARY else {}

## 交给配对状态机的「外部事实」：她这一轮是否还在跑 / 音频是否在播或排队 / 是否还有取音频请求在途
func _pair_facts() -> Dictionary:
	return {
		"turn_active": agent != null and agent.busy,
		"playing": tts != null and tts.is_speaking(),
		"fetching": _speech_fetch_busy or not _speech_fetch.is_empty(),
	}

## 登记一条「要由后端朗读的台词」的配对记录（登记顺序 = 与 speech 事件的配对顺序）。
## 语音关着时不登记（后端那时也不会发 speech，文字一律直接显示，与没有这个功能时逐字一致）。
## `tomb=true`：这条已在本地被丢弃（走神作废 / 防复读），只留个占位吃掉它自己那条 speech。
func _speech_register(text: String, reveal: bool, kind: String, tomb := false) -> Dictionary:
	if tts == null or not tts.enabled:
		return {}
	return pairing.register(text, reveal, kind, tomb)

## 本地丢弃的台词留个占位（吃掉它自己那条 speech）
func _register_tomb(kind: String) -> void:
	_speech_register("", false, kind, true)

## 收到后端一条语音指令（紧随对应 message 之后）：与待播台词配对 → 取音频 / 交系统语音。
## 语音关闭时**一条请求都不发**（后端也不会发 speech），文字照常显示的老路子不受影响。
func _on_speech_event(payload: Dictionary) -> void:
	if tts == null or not tts.enabled:
		return
	var smode := str(payload.get("mode", ""))
	if smode != "openai" and smode != "system":
		return                                   # none / 脏值：不出声，也不占用配对位
	var turn := str(payload.get("turn_id", ""))
	var seq := int(payload.get("seq", 0))
	var rec := pairing.accept_speech(turn, seq)
	if rec.is_empty():
		_report_speech_drop(turn, seq)
		return                                   # 重复/乱序，或没有等着它的台词：绝不凭空出声
	if pairing.is_tomb(rec):
		pairing.finish(rec)                      # 本地已丢弃的台词：吃掉它自己那条语音，不出声
		return
	if tts != null:
		tts.set_mood_pitch(float(payload.get("pitch", 0.0)))   # T2：每条语音自带音高偏移，开播前落到播放层
	var kind := pairing.kind_of(rec)
	if smode == "system":
		pairing.set_group(rec, tts.speak_system(str(payload.get("text", "")), seq, kind))
		return
	_speech_fetch.append({"id": str(payload.get("id", "")), "seq": seq, "rec": rec})
	_pump_speech_fetch()

## 配对没成功只记一行日志（判定在状态机里，日志留在这一层）
func _report_speech_drop(turn: String, seq: int) -> void:
	if pairing.last_drop_reason == "stale":
		print("[TTS] 重复/乱序的 speech 已丢弃：turn=%s seq=%d" % [turn, seq])
	else:
		print("[TTS] 收到没有对应台词的 speech（turn=%s seq=%d），已忽略" % [turn, seq])

## 取音频（GET /api/tts/audio/{id}）。**串行**一次一条：保证 play_audio 的调用顺序 = speech 到达顺序，
## 否则两条语音同时取、谁先回来谁先播，音频就会和文字错位。
## 每个出口都必须给出**结论**：能播就播，不能播（HTTP 失败 / **200 但 body 为空**）一律由
## `pairing.resolve_fetch()` 收账（finish + 显示文字），绝不把记录留在 `got && !played`（那会锁死棋盘）。
func _pump_speech_fetch() -> void:
	if _speech_fetch_busy or _speech_fetch.is_empty():
		return
	_speech_fetch_busy = true
	var job: Dictionary = _speech_fetch.pop_front()
	var rec: Dictionary = job["rec"]
	if agent == null:
		_speech_fetch_busy = false
		pairing.resolve_fetch(rec, false, 0)
		_note_audio_failed("后端未连接")
		_pump_speech_fetch()
		return
	agent.fetch_bytes("/api/tts/audio/%s" % str(job["id"]), HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			_speech_fetch_busy = false
			var verdict := pairing.resolve_fetch(rec, ok, body.size())
			if verdict == "":
				# 音频到手：交给 TTSClient 播（取回期间没被作废才走得到这里）
				pairing.set_group(rec, tts.play_audio(body, int(job["seq"]), pairing.kind_of(rec)))
			elif verdict != "done":
				_note_audio_failed(_audio_why(verdict, code, body))
			_pump_speech_fetch())

## 取音频失败的原因文案（收账与「文字照常显示」已由 `pairing.resolve_fetch()` 做掉，这里只提示）
func _audio_why(verdict: String, code: int, body: PackedByteArray) -> String:
	if verdict == "empty":
		return "后端返回了空音频"
	return _api_error_text(code, body) if code > 0 else "后端不可达"

## 取音频失败的提示（状态栏 + 左侧栏）：只提示，不改任何配对状态
func _note_audio_failed(why: String) -> void:
	print("[TTS] 取音频失败：%s" % why)
	_on_tts_status("这段语音的音频取回失败（%s），改为只显示文字" % why)

## 配对状态机说「这条台词的文字该出现了」：显示气泡 + 该揭示的落子在这一刻揭示（音画同步）
func _on_pairing_display(text: String, reveal: bool) -> void:
	_add_msg_now("ai", text)
	if reveal:
		_reveal_move()                 # 说到这手棋时，才把子放上棋盘

## 配对状态机说「有条语音出结果了」：刷新棋盘锁与「思考中…」占位
func _on_pairing_resolved() -> void:
	_refresh_lock()
	_clear_thinking_if_idle()

## 她这一侧还有语音的事没完（排队/播放/取音频/等配对）：棋盘锁、语气词让位、闲置判定都看它。
## 「音频还在路上」也必须算——否则后端 busy 一结束就会在音频到达之前提前解锁棋盘。
func _speech_busy() -> bool:
	return pairing.busy(_pair_facts())

## 音频侧还有活干（在播 / 排队 / 正在取）：这时不能把等待中的文字当「等不到语音」放出来。
## 已作废的 / 已出结果的记录不算：否则后端一条 speech 都不发时棋盘会被锁死。
func _audio_in_flight() -> bool:
	return pairing.audio_in_flight(_pair_facts())

## 试听按钮：合成在后端 —— POST /api/tts/speak 取回音频再播；失败只在状态栏提示，不打扰对局。
## 有意**不看** `tts.enabled`：试听是用户显式点的（与 BASE 的 `preview()` 一致），关了朗读也该能试。
func _preview_tts() -> void:
	if agent == null:
		_on_tts_status("试听失败（后端未连接）")
		return
	if tts != null:
		tts.stop()
	_tts_status.text = "正在合成试听…"
	var done := func(ok: bool, code: int, body: PackedByteArray):
		if ok and not body.is_empty():
			if tts != null:
				# `seq=0` 只是「没有回合序号」；TTSClient 照常分配一个非零分组号，
				# 而且这音频没在配对表里登记，所以不会触发任何待播文字的显示
				tts.play_audio(body, 0)
			return
		_on_tts_status("试听失败：%s" % (_api_error_text(code, body) if code > 0 else "后端不可达"))
	agent.fetch_bytes("/api/tts/speak", HTTPClient.METHOD_POST, {"text": PREVIEW_TEXT},
			done, TTS_FETCH_TIMEOUT)

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
		board.capture_home_view()          # 调试参数就是「初始取景」：Home 键回到这里
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
