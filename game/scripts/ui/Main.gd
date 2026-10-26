extends Node3D
## 主场景：3D 棋盘 + 右侧聊天面板，接线全部服务
## 调试：`-- --shot=输出.png` 可在渲染数帧后自动截图并退出

const PANEL_W := 380
const BOARD_CELLS := 4                      # 棋盘切成 4×4 个格子矩形判重叠：菱形棋盘的外接矩形太"虚胖"
const BOARD_LAST := 18                      # 19 路棋盘的最大下标
const CARD_FOLLOW_SEC := 0.25                # 「卡片跟随取景」的节流间隔（秒）
const MOVE_PENDING_SEC := 12.0               # 「点下就锁」的兜底解锁：回包/事件全丢也不许把棋盘锁死

## 启动设置面板确认（_ready 里 await 它，之后才按新配置起各服务）
signal setup_confirmed
## 主界面「开始对局」（首次启动时 _ready 里 await 它，之后才进设置面板 / 起服务）
signal title_started

## —— 思考中的占位 ——
## 固定显示「Azure 思考中…」+ 省略号动效，不再轮播 JSON 短句。
## JSON 解析工具与数据文件保留给后续可选功能，但当前运行路径不读取、不轮播。
const THINKING_FALLBACK := "Azure 思考中"

var cfg: AzureConfig
## 对局编排入口：NetClient（编排与 KataGo/LLM 都在后端服务进程）。不写死类型，与信号接线解耦。
var agent
var board: Board3D
var ambience: Ambience
var avatar: AzureAvatar
var tts: TTSClient
var sfx: SfxPlayer                            # 对局音效（落子 / 提子 / 虚着 / 终局；纯前端）

var _left_panel: PanelContainer            # 左侧：语音设置
var _right_panel: PanelContainer           # 右侧：对局 / 聊天
var _joseki_card: JosekiCard = null        # 定式浮层参考图卡片（浮在棋盘上方，不进任何侧栏）
var _joseki_card_home := Vector2.ZERO      # 我们最后一次给卡片摆的位置（用户拖走后不再接管）
var _card_check := 0.0                     # 「跟随取景」的节流计时（见 _follow_joseki_card）
var _move_pending := false                 # 「点下就锁」的本地闩：busy 事件回来前挡住第二手（v3.9.5）
var _move_seq := 0                         # 本地闩的代数：作废还在跑的兜底计时器
var _left_open := false                    # 左侧默认收起，保持棋盘视野
var _right_open := true
var _left_toggle: Button
var _right_toggle: Button
var _tts_status: Label
var _tts_route: Label                       # 实际朗读路线（本地优先 → 云端 → 系统）：只读提示，合成在后端
var _tts_url_edit: LineEdit
var _tts_model_edit: LineEdit
var _tts_voice_edit: LineEdit
var _tts_voice_opt: OptionButton
var _tts_aliyun_key_edit: LineEdit
var _tts_aliyun_voice_edit: LineEdit
var _tts_aliyun_box: VBoxContainer          # 阿里云配置：挂在朗读方式下拉正下方，仅选阿里云时展开
## —— 主界面（纸幕封面，v3.11.2）——
var title_screen: TitleScreen = null       # 首次启动 / 对局中按 Esc 都是这一块
var _title_open := false                   # 封面开着：棋盘锁、聊天禁用、不闲置搭话
var _game_started := false                 # 已经过了首次「开始对局」（决定主按钮是「开始对局」还是「开始新对局」）
## 从主界面进入时用：后端 `/api/reset` 在 busy 时会拒绝，忙就先记下，等 `_on_busy_changed(false)` 补发
var _restart_when_idle := false
var _restart_retry_ms := 0                 # reset 请求没发出去时的重试时刻（不把新局静默吞掉）
var _setup_done := false                   # 本次运行已在主界面里点过「设置」：开局时不再弹第二遍
var _setup_showing := false                # 设置面板正在显示（防重入）
var _transition := false                   # 主界面 ⇄ 对局的运镜正在跑（期间不吃 Esc / 不叠加动画）
var _cover_only := false                   # --title：只画封面（截图调试）——不叠问候 / 设置面板
var _shot_requested := false               # 命令行带 --shot= / --demo：截图模式
## —— 启动预热（和经典版一样：主界面 / 设置面板期间先把 CosyVoice 起起来）——
var _services_prewarmed := false           # 已经拉过「只起语音」的预热启动器（只对质量引擎用）
var _local_tts_loading := false            # 本地质量引擎正在加载：客户端不出系统语音（只显示文字）
const AI_DEDUP_WINDOW := 8                 # 防复读窗口（兜底判据）：后端已按同样窗口去重，这里只兜 WS 重连后的乱序
var _recent_ai_texts: Array[String] = []   # 最近说过的 AI 原话（含落子宣告）：只兜 WS 重连后乱序到达的重复句

## —— 语音配对（计划 05：合成归后端，前端只播放）——
## 配对表、TTL/保鲜期、「她在忙吗」的判定全部在 `SpeechPairing`（可测决策层）里；本文件只留胶水：
##   · 取音频（HTTP）、调系统语音、显示气泡、`POST /api/reveal`（见 _on_pairing_display）
##   · 外部事实（后端 busy / 音频是否在播 / 是否还有取音频请求在途）由 `_pair_facts()` 打包喂给它
var pairing: SpeechPairing = null
var _speech_fetch: Array[Dictionary] = []  # 待取音频的 FIFO（**串行**取：保证播放顺序 = speech 到达顺序）
var _speech_fetch_current: Dictionary = {} # 正在取的那一条（v3.11.1：玩家落子打断时要一并收账）
var _speech_fetch_busy := false
var _stream_turn := ""                     # 当前流式气泡对应的 turn_id（后端 token 流式通道：可选、默认关闭）
var _stream_label: Label = null            # 当前流式气泡：增量直接追加，最终由 message 全文收口
var _thinking_row: Control = null          # 「Azure 思考中……」占位气泡
var _thinking_label: Label = null
var _speech_hold := false                  # Azure 还有语音没播完：只挡四个功能按钮（v3.11.1 起不再锁棋盘）
var _batch_pending := false                # 整段模式：还攒着没放出来的 Azure 文本（只当标记用，文本不再合并）
var _pending_batch_kind := ""              # 这批文本的类型（""=对局发言；"idle"=走神搭话，可整批作废）
var _batch_token := 0                      # 去抖令牌：又来新消息就自增，让旧计时器失效
var _tts_playing := false                  # 当前是否真有音频在播（区别于「排队中/合成中」）
var _think_dots := 0                       # 「思考中…」省略号动效点数（1~3 循环）
var _think_last_ms := 0                    # 上次刷新省略号的时刻
var _think_lines := PackedStringArray()    # 思考短句池（data/thinking_lines.json）
var _think_groups: Dictionary = {}         # 心情/局面分组 → 下标数组
var _think_prev := -1                      # 上一句下标：跨气泡也不连复
var _think_line_ms := 0                    # 上一句轮换时刻
var _think_dynamic := false                # 当前占位是否用短句轮播（数据缺失时退回固定文案）
var _mood_name := "平静"                    # 最近一次三维情绪区域名（选思考短句 / 身体语言用）
var _last_azure_wr := -1.0                 # 最近一次形势里的白方胜率（-1 ＝还没有数据）
var _blush_seq := 0                        # 摸头脸红的序号（新的一次摸头会让旧的褪红计时失效）

## 导演层（拟人化）：重要点评前的犹豫节拍 + 聊天面板打字节奏；均为确定性本地规则，零额外 LLM 调用
const HESITATE_FAST_MIN := 0.8             # 快速/无语音引擎：犹豫时长下限（秒）
const HESITATE_FAST_MAX := 2.5
const HESITATE_SLOW_MIN := 0.5             # 慢引擎（CosyVoice）：合成本来就慢，犹豫缩短避免雪上加霜
const HESITATE_SLOW_MAX := 1.4
const TYPING_SEC_PER_CHAR := 0.045         # 打字节奏：每字时长（秒），总时长再限制在 0.5~2.4s
const TYPING_MIN_SEC := 0.5
const TYPING_MAX_SEC := 2.4
var _msg_queue: Array[Dictionary] = []     # 待投递消息 {kind,text,tone,seq,rec}：犹豫/延迟期间顺序不乱
var _msg_queue_busy := false               # 投递协程正在跑
var _msg_seq := 0                          # 入队序号（快进判定用）
var _queue_rush_seq := 0                   # 序号 ≤ 此值的消息立即投递（玩家/系统消息到达时快进）
var _typing_label: Label = null            # 正在打字的那条 Azure 气泡
var _typing_full := ""                     # 这条消息的完整文本
var _typing_shown := 0.0                   # 已显示字数（浮点累计）
var _typing_cps := 0.0                     # 当前打字速度（字/秒）
var _typing_queue: Array[Dictionary] = []  # 排队等待打字的 {label,text}（单活跃顺序播放，防抢跑）
static var _tone_re := RegEx.new()         # [[tone:xxx]] 语气标记（首次使用时编译）
static var _action_re := RegEx.new()       # {行为} 标签（首次使用时编译）
## 情绪语气词：按「思考节点」插入，频率由当前情绪强度决定；预制音必须匹配上一句的语气 / 行为标签。
const FILLER_MIN_NODES := 2                # 两个思考节点内不重复插（节点内的连播闸）
const FILLER_MIN_GAP_MS := 6000            # 两个语气词之间的最短间隔
const FILLER_SEMANTIC_MAX_AGE_MS := 9000   # 上一句的语义标签只在最近 9 秒内有效
var _mood_v := 0.5                         # 当前愉悦度（0~1）
var _mood_c := 0.5                         # 当前好奇度（0~1）
var _mood_a := 0.5                         # 当前唤醒度（0~1）
var _last_mood_filler := ""                # 当前情绪区域自带的语气词（无上一句语义时的候选）
var _last_ai_tone := ""                    # 上一句 AI 台词的 [[tone]]（sigh / cheer / proud）
var _last_ai_acts: Array = []              # 上一句 AI 台词的行为标签（笑 / 得意 / 惊讶 / 担心 / 难过）
var _last_ai_semantic_ms := 0              # 上一句语义标签的时刻（过期不用于选语气词）
var _filler_nodes := 0                     # 距上次语气词经过的思考节点数
var _last_filler_ms := 0                   # 上次语气词的时刻（最短间隔）

## Azure 连着冒的多条消息在这么长时间内合并成一段再合成（CPU 合成慢，分开发会句间空很久）
const BATCH_WINDOW := 0.35

var _last_activity_ms := 0                 # 最近一次「玩家有操作」的时刻（闲置计时）
var _idle := false
var _idle_muted := false                   # 玩家已回来（落子/说话）：在途与排队的走神搭话全部作废
var _next_talk_ms := 0                     # 下一次闲置搭话的时刻
var _last_move_count := 0                  # 用于识别 Azure 刚落的那颗子
var _last_stone_count := 0                 # 上一份 state 的棋子数：判断提子 / 虚着
var _sfx_ready := false                    # 第一次权威 state 不播音效（连接 / 重连的一次性对齐）
var _last_move_seq: Array = []             # 最近一份 state 的 GTP 手顺（战报用）
var _last_wr := 0.5                        # 最近一次形势（战报用；黑方视角）
var _last_score := 0.0
var _info_ready := false
var _report_busy := false                  # 战报生成中：按钮防重入

## 音色候选（静态列表）：云端服务商各有音色名，这里只给常见几种；右侧输入框可手填任意音色 id
const VOICE_PRESETS := ["alloy", "echo", "fable", "nova", "onyx", "shimmer",
	"中文女", "粤语女", "英文女"]
## 需要 CosyVoice 质量引擎的本机音色（azure=经典版克隆音色；其余为 SFT 预设女声）。
const LOCAL_QUALITY_VOICES := ["azure", "中文女", "粤语女", "英文女"]
## 本地音色下拉（朗读方式=本地时显示）：把经典版的 Azure 克隆音色加回来。
const LOCAL_VOICE_PRESETS := [
	["azure", "Azure 克隆（最像 · CosyVoice）"],
	["melo", "MeloTTS（轻量）"],
	["xiaoya", "小雅 · 清亮少女（轻量）"],
	["shaonv", "少女音 · 高音（轻量）"],
	["shaonv2", "少女音 · 中音（轻量）"],
	["nvsheng", "女声 · 温柔（轻量）"],
]

## 闲置互动：玩家长时间无操作 → 看向玩家 + 歪头 + 偶尔主动搭话（搭话走后端 POST /api/idle）
const IDLE_SEC := 12.0
const IDLE_TALK_FIRST := 18.0
const IDLE_TALK_MIN := 45.0
const IDLE_TALK_MAX := 80.0

## 长时间闲置窥屏（本地 RapidOCR + Qwen2.5-VL，独立 python 进程一跑一停）
const PEEK_COOLDOWN_MS := 60000            # 两次窥屏至少隔 1 分钟（搭话本身也要间隔 45 秒起）
const PEEK_TIMEOUT_MS := 240000            # 窥屏进程超时：放弃，退回普通搭话
var _last_player_activity_ms := 0          # 只有玩家自己动鼠标/键盘才刷新（窥屏门槛用，不被她说话重置）
var _peek_busy := false
var _peek_started_ms := 0
var _peek_last_ms := 0
var _peek_ctx_activity := 0                # 发起窥屏时的玩家活动时刻：对不上说明玩家已回来，结果作废
var _peek_out := ""                        # 窥屏结果 json 路径
var _peek_exe := ""                        # 跑窥屏的 python（需装好 rapidocr / PIL / llama_cpp）
var _peek_script := ""                     # azure_peek.py 路径
var _peek_probed := false
var _peek_ok := false
var _peek_pid := -1                        # 正在跑的窥屏进程（退出/超时清掉，别留孤儿）
var _vlm_url := ""                         # 已解析的 VLM 地址（空串=没有，窥屏脚本回退 CPU）
var _vlm_starter := ""                     # start_vlm_server.py 路径

var _font: Font
var _ui_root: Control
var _chat_box: VBoxContainer
var _chat_scroll: ScrollContainer
var _btn_new_msg: Button                        # 玩家向上翻看时来了新消息：右下角提示，点了回底部
var _chat_follow := true                        # 是否跟随最新消息（在底部 = true）
var _chat_font_px := 15                         # 聊天正文字号（本局内可调，不改 config）
var _chat_line_px := 6                          # 气泡内行距
var _lbl_moves: Label
var _lbl_wr: Label
var _lbl_score: Label
var _lbl_score_hint: Label                     # 目差下面那行 11px 小字：量级人话（空串＝还没有形势信息）
## —— 局面点目（v3.12.0 采纳）——
var _estimate_box: PanelContainer
var _estimate_count: Label                     # 「点目（第 N 手）：黑 41 目 · 白 4 目」
var _estimate_lead: Label                      # 「黑领先 29.5 目（贴 7.5 目）」
var _estimate_move_count := -1                 # 上次点目对应的手数（只用于标注 / 收棋盘方块）
var _card_wr_bar: ProgressBar                  # 全局形势卡的胜率条（黑方占比）
var _card_wr_black: Label
var _card_wr_white: Label
var _card_score: Label                         # 目差（与右栏同源）
var _card_score_hint: Label                    # 量级人话
var _card_body: VBoxContainer                  # 可折叠部分
var _card_toggle: Button
var _card_global: Label                        # 她落子后那句全局判断
var _last_ai_kind := ""                        # 上一句 AI 消息类型：识别「落子宣告 → 全局判断」
var _lbl_status: Label
var _status_before_tool := ""                  # 她查资料前状态栏的原文案（end 时恢复）
var _status_before_tool_bad := false           # 原文案的错误色标记（end 时一并恢复，别把红字洗成灰字）
var _status_bad := false                       # 状态栏当前是不是错误色（_set_status 记录，上面那行取用）
var _tool_active := false                      # 工具进度是否在跑（start/end 成对，避免重复记原文案）
var _chk_pass: CheckBox
var _joseki_opt: OptionButton                     # 定式导学＝「开关 + 频率」一格（关闭 / 低 25% / 中 50% / 高 75% / 总是 100%）
var _drift_opt: OptionButton                      # 情绪波动＝「开关 + 基础概率」一格（关闭 / 低 3% / 中 5% / 高 6%；在 ⚙ 设置抽屉里）
var _adaptive_base_opt: OptionButton              # 自适应难度基线（1 入门 … 5 严苛；开关与基线同在两格）
var _reply_opt: OptionButton                      # 说话详略三档（精简 / 适中 / 详细；v3.11.1，原来那个「短回复」勾选框）
var _think_opt: OptionButton                      # 思考强度三档（快速 / 中等 / 强思考；v3.11.1，运行期即时生效）
var _btn_settings: Button                         # ⚙ 设置抽屉的开关
var _settings_box: PanelContainer                 # ⚙ 设置抽屉本体（情绪波动 / 同步说话 / 整段合成 / 长句合并 / 聊天摘要用 LLM / 画像，默认收起）
var _lbl_profile: Label                           # 画像面板的只读文本（v3.9.0：与开关同一格，不占右栏常驻位）
var _bad_box: HBoxContainer
var _chat_input: LineEdit
var _lbl_queue: Label                           # 右下角那行「排队中 N 条 · 队头前几个字」（队列空则隐藏）
var _buttons: Array[Button] = []
var _btn_reset: Button = null                   # 🔄 重来：对局结束后仍可用（开新局），复盘时与其余三个一起冻
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
var _btn_home: Button                      # 🏠 返回主界面（v3.11.3：对局结束后与复盘并排）
var _btn_report: Button                    # 战报（v3.11.5）：生成 PNG，局后与复盘并排
var game_library: GameLibrary = null       # 棋谱库（v3.11.5）：主界面打开的全屏模态
## 棋谱库请求序号：慢请求回来时若已有更新的请求 / 已换过一个库实例，就丢弃旧回包（防止点 A 看 B）
var _games_list_seq := 0
var _games_open_seq := 0
## —— 全盘 AI 复盘（v3.11.5）：棋谱库选一盘 → 主棋盘只读回放 + 分段点评 ——
var _fr_active := false                    # 全盘复盘模式：棋盘只读、聊天禁用、闲话停
var _fr_record: Dictionary = {}            # 正在复盘的那盘棋（来自棋谱库）
var _fr_segments: Array = []               # [[from, to), ...]：与后端 plan_segments 同一条公式
var _fr_index := 0                         # 下一段要请求的序号
var _fr_step := 0                          # 棋盘当前回放到第几手（0..总手数）
var _fr_paused := false                    # 暂停自动解说
var _fr_requesting := false                # 当前有段请求在途（暂停 / 重试时判断用）
var _fr_msg_seen := false                  # 这一段 WS 台词已到（HTTP 回包可能先到：等它落地再排下一段）
var _fr_token := 0                         # 退出 / 重开后旧回调一律作废
var _fr_last_text := ""                    # 上一段点评原文（喂给下一段防重复）
var _fr_saved_own: Array = []               # 进复盘前的领地图：退出时还原（不用再等一次分析）
var _fr_saved_stones: Array = []            # 进复盘前的盘面：退出时先本地还原，再拉权威 state 对齐
var _fr_saved_seq: Array = []
var _fr_box: HBoxContainer = null          # 全盘复盘按钮行（只在模式内显示）
var _fr_prev: Button = null
var _fr_next: Button = null
var _fr_toggle: Button = null
var _btn_fr_exit: Button = null
const FULL_REVIEW_MAX_SEGMENTS := 8
const FULL_REVIEW_MIN_SPAN := 12
const FULL_REVIEW_MAX_SPAN := 48
const FULL_REVIEW_TIMEOUT := 180.0          # 一段：N 次引擎 + 强思考 LLM + TTS，给足时间
var _btn_prev: Button                      # ◀ 上一手（只在复盘模式内可见）
var _btn_next: Button                      # 下一手 ▶（只在复盘模式内可见）
var _btn_send: Button                      # 聊天发送（复盘期间与输入框一起禁用）

func _ready() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	_last_player_activity_ms = _last_activity_ms  # 窥屏门槛用真实玩家操作时钟
	_peek_last_ms = -PEEK_COOLDOWN_MS             # 允许开局后第一次长闲置就窥屏
	_last_filler_ms = _last_activity_ms           # 开局先给一个最短间隔，不因初始 mood 立刻响语气词
	cfg = AzureConfig.load_config()
	_apply_cli_overrides()
	GufengTheme.apply_preset(cfg.ui_theme)     # 建任何 UI 之前先落主题（默认雾蓝）
	_build_pairing()                           # 语音配对状态机：只做决策，副作用回调回本文件
	_build_sfx()                               # 对局音效：预加载 6 个音频、4 个播放器轮转
	_font = _make_cjk_font()
	_build_world()
	_build_board()
	_build_ui()
	_build_title_screen()                      # 主界面：纸幕封面（棋盘与化身在纸幕后）
	_prewarm_local_services()                  # 封面 / 设置面板期间先把本地语音（按记住的音色）起起来
	await _maybe_show_title()                  # 首次启动：等「开始对局」
	if _cover_only and _shot_requested:
		_handle_cli()                          # 只拍封面：不起服务 / 不叠问候，避免聊天面板透在纸幕后
		return
	await _maybe_show_setup()                  # 启动设置面板：思考模式 + 各 API 配置（默认=当前配置）；确认后按新配置起服务
	if not _shot_requested:
		var cosy := await _maybe_ask_cosyvoice()
		var quality: bool = bool(cosy.get("quality", false))
		var wait_tts: bool = bool(cosy.get("wait", false))
		var spawned := _maybe_start_local_services(quality, wait_tts)
		_local_tts_loading = quality and (spawned or _services_prewarmed)
		if wait_tts and spawned:
			await _wait_local_tts_ready(true)
	if not _shot_requested:
		_begin_new_game_on_entry()             # 先立「等 reset」闸：hello 里上一局的迟到 state 不许闪出来
	if agent == null:
		_build_services()                      # 从主界面直接进过棋谱库时已经起过：不重复装配
	if tts != null:
		tts.set_suppress_system(_local_tts_loading)   # 装配时质量引擎还在加载：连第一句系统语音也压掉
	if _local_tts_loading:
		_watch_local_tts_ready()               # 不阻塞进对局：后台等就绪后解除「只显示文字」
	_start_launch_game()                       # 首次进对局也保证空局（后端可能还留着上次运行的棋局）
	_greet()
	_handle_cli()

## 自用版启动便利：游戏启动时自动拉起本地模型链路（LLM / TTS / KataGo / 后端）。
## 由 `dev_local.ps1` 拉起游戏时环境里有 AZURE_SERVICES_MANAGED=1，不再重复拉起。
## `quality_tts` 为真时要求 CosyVoice 质量引擎；`wait_tts` 为假时带 -NoWaitTts，先让后端起来。
## `tts_only` 为真时带 -TtsOnly：只起本地语音（启动预热用，不写配置 / 不起其他服务）。
## wscript 的参数顺序：**VBS 自身 → dev_local.ps1 → 可选开关**。顺序错了 wscript 会把 .ps1
## 当成脚本文件直接执行，Windows 报「没有 .ps1 的脚本引擎」。纯函数，门卫直接测。
static func hidden_launcher_args(vbs: String, script: String, quality: bool, wait: bool,
		tts_only := false) -> PackedStringArray:
	var out := PackedStringArray([vbs, script])
	if tts_only:
		out.append("-TtsOnly")
	if quality:
		out.append("-QualityTts")
		if not wait:
			out.append("-NoWaitTts")
	return out

func _maybe_start_local_services(quality_tts := false, wait_tts := false, tts_only := false) -> bool:
	if DisplayServer.get_name() == "headless":
		return false                              # 无头测试 / 自检：不拉起服务
	if not cfg.auto_start_services:
		return false
	if not _is_local_url(cfg.backend_url):
		return false
	if OS.get_environment("AZURE_SERVICES_MANAGED") == "1" and not quality_tts:
		return false                              # dev_local 已托管基础服务；只有切 CosyVoice 才再拉起一次
	var script := _dev_local_script()
	if not FileAccess.file_exists(script):
		return false                              # 导出包 / 非源码运行：没有启动器，静默跳过
	# 优先走 VBS 隐藏启动：OS.create_process 直接拉 powershell.exe 时，Windows 仍可能给
	# 控制台应用分配一个可见窗口（选「等待 CosyVoice」时该进程会存活 1–3 分钟，看起来像
	# 「又开了一个窗口」）。VBS 里 sh.Run(cmd, 0, False) 把 PowerShell 连同子进程彻底隐藏。
	var vbs := _repo_root().path_join("tools/run_hidden_dev_local.vbs")
	var pid := -1
	if FileAccess.file_exists(vbs):
		var vargs := hidden_launcher_args(vbs, script, quality_tts, wait_tts, tts_only)
		var wscript := OS.get_environment("SystemRoot").path_join("System32/wscript.exe")
		if wscript == "" or not FileAccess.file_exists(wscript):
			wscript = "wscript.exe"
		pid = OS.create_process(wscript, vargs)
	if pid <= 0:                                  # VBS 缺失 / 拉起失败：回落到直接 PowerShell（仍带 -NoGame）
		var ps := OS.get_environment("SystemRoot").path_join("System32/WindowsPowerShell/v1.0/powershell.exe")
		if ps == "" or not FileAccess.file_exists(ps):
			ps = "powershell.exe"
		var args := ["-NoProfile", "-ExecutionPolicy", "Bypass",
			"-WindowStyle", "Hidden", "-File", script, "-NoGame"]
		if tts_only:
			args.append("-TtsOnly")
		if quality_tts:
			args.append("-QualityTts")
			if not wait_tts:
				args.append("-NoWaitTts")
		pid = OS.create_process(ps, args)
	if pid > 0:
		if tts_only:
			_set_status("正在后台预加载 CosyVoice（主界面 / 设置面板期间）…" if quality_tts
					else "正在后台预加载本地语音（主界面 / 设置面板期间）…")
		elif quality_tts:
			_set_status("正在启动 CosyVoice（约 1–3 分钟）…" if wait_tts else "正在启动本地模型服务（CosyVoice 后台加载）…")
		else:
			_set_status("正在启动本地模型服务（LLM / TTS / KataGo / 后端）…")
		print("[Services] 本地服务启动器已拉起 pid=%d quality=%s wait=%s tts_only=%s"
				% [pid, quality_tts, wait_tts, tts_only])
		return true
	if tts_only:
		print("[Services] 语音预加载没有拉起；稍后完整启动流程会再试一次")
		return false
	_set_status("本地模型服务启动失败：请手动运行 dev_local.ps1 -NoGame", true)
	return false

## 启动预热（和经典版一样「先起服务、后进对局」）：主界面 / 设置面板期间先把本地语音拉起来，
## 让 CosyVoice 的模型加载与玩家看封面、填设置的时间重叠。只起语音（-TtsOnly），
## 不写配置（设置面板还没确认），也不启动 LLM / KataGo / 后端——那些由后面的完整流程补齐。
func _prewarm_local_services() -> void:
	if DisplayServer.get_name() == "headless":
		return
	if not cfg.auto_start_services or not _is_local_url(cfg.backend_url):
		return
	if not FileAccess.file_exists(_dev_local_script()):
		return                                      # 导出包 / 非源码运行：没有启动器
	if OS.get_environment("AZURE_SERVICES_MANAGED") == "1":
		return                                      # dev_local 已把语音连同其他服务起好：不重复
	if _cli_visual_only():
		return                                      # 截图 / 演示：不启动任何服务
	if not _can_ask_cosyvoice():
		return                                      # 不走本地质量引擎（系统 / 云端语音）：按原流程
	if not local_voice_wants_quality(cfg.tts_local_voice):
		return                                      # 记住的是轻量音色：几秒就绪，不提前起质量引擎
	# wait=true：隐藏的启动器等 /health 就绪并预热首句后再退出；游戏这边**不等**。
	_services_prewarmed = _maybe_start_local_services(true, true, true)

## 这个本地音色要不要 CosyVoice 质量引擎（azure 克隆 / SFT 预设女声）。
## 轻量音色（melo / xiaoya / shaonv / ...）走 sherpa，不预热、也不弹「等不等」。
static func local_voice_wants_quality(voice: String) -> bool:
	return voice.strip_edges() in LOCAL_QUALITY_VOICES

## 只画界面 / 截图的命令行（--title / --shot= / --demo / --setup）：不预拉任何本地服务。
func _cli_visual_only() -> bool:
	for a in OS.get_cmdline_user_args():
		if a == "--title" or a == "--setup" or a == "--demo" or a.begins_with("--shot="):
			return true
	return false

## 开游戏询问：CosyVoice 未就绪时问是否等它；已就绪则直接沿用。
## 返回 {"quality": 是否起/保留 CosyVoice, "wait": 是否等它就绪}。
func _maybe_ask_cosyvoice() -> Dictionary:
	var out := {"quality": false, "wait": false}
	if not _can_ask_cosyvoice():
		return out
	if not local_voice_wants_quality(cfg.tts_local_voice):
		return out                                  # 用户明确选了轻量音色：按轻量起，不再追问
	var h := await _tts_health_once()
	if _cosyvoice_ready(h):
		out["quality"] = true                     # 已经是质量引擎：保留它，不再询问
		return out
	out["quality"] = true
	if _services_prewarmed:
		# 预热已经在后台加载 CosyVoice：不再弹「等不等」，也不挡住进对局——
		# 先只显示文字（客户端此时会压掉系统语音），质量引擎起好后自动接上。
		_set_status("CosyVoice 正在后台加载，先只显示文字；起好后自动用 Azure 音色。")
		return out
	out["wait"] = await _show_cosyvoice_dialog()
	return out

## 只有「本机后端 + 本机本地语音 + 自动拉起」才提问/接管。
## 即使由 dev_local 拉起，当前是轻量引擎也会询问是否切到 CosyVoice；已就绪则直接沿用。
func _can_ask_cosyvoice() -> bool:
	if DisplayServer.get_name() == "headless":
		return false
	if not cfg.auto_start_services:
		return false
	if not _local_tts_managed():
		return false
	return FileAccess.file_exists(_dev_local_script())

func _local_tts_managed() -> bool:
	if not cfg.tts_enabled:
		return false
	if cfg.tts_mode != "local":
		return false
	if not _is_local_url(cfg.backend_url):
		return false
	if not _is_local_url(cfg.tts_local_base_url):
		return false
	return true

## 等待 CosyVoice 就绪：只读 /health，不改配置。
func _wait_local_tts_ready(wait_for_cosy := false) -> void:
	if not wait_for_cosy or DisplayServer.get_name() == "headless":
		return
	var started := Time.get_ticks_msec()
	var limit_ms := 300000
	_set_status("正在等待 CosyVoice 就绪（约 1–3 分钟）…")
	var ready := false
	var note_ten := -1
	while Time.get_ticks_msec() - started < limit_ms:
		var h := await _tts_health_once()
		if _cosyvoice_ready(h):
			ready = true
			break
		var sec := int((Time.get_ticks_msec() - started) / 1000.0)
		if sec / 10 != note_ten:
			note_ten = sec / 10
			_set_status("正在等待 CosyVoice 就绪… %ds（模型比轻量引擎大，首次约 1–3 分钟；就绪后再开始对局）" % sec)
			print("[TTS] 等待 CosyVoice 就绪… %ds" % sec)
		await get_tree().create_timer(2.0).timeout
	_local_tts_loading = false                    # 就绪 / 超时都解除：超时后真连不上再按原逻辑降级
	if ready:
		_set_status("CosyVoice 已就绪（Azure 克隆音色），开始对局。")
		print("[TTS] CosyVoice 已就绪")
	else:
		_set_status("CosyVoice 启动超时，先用当前语音。", true)
		print("[TTS] CosyVoice 等待超时（300s）")

## 后台等质量引擎就绪（不阻塞进对局）：加载期压掉系统语音，就绪后解除并提示；
## 超时同样解除——与经典版 `notify_service_ready()` 同口径（之后真连不上再降级）。
func _watch_local_tts_ready() -> void:
	if DisplayServer.get_name() == "headless":
		return
	var started := Time.get_ticks_msec()
	var limit_ms := 300000
	while _local_tts_loading and Time.get_ticks_msec() - started < limit_ms:
		await get_tree().create_timer(2.0).timeout
		var h := await _tts_health_once()
		if _cosyvoice_ready(h):
			_local_tts_loading = false
			if tts != null:
				tts.set_suppress_system(false)
			_set_status("CosyVoice 已就绪（Azure 克隆音色），接下来用 Azure 音色。")
			return
	if _local_tts_loading:
		_local_tts_loading = false
		if tts != null:
			tts.set_suppress_system(false)
		_set_status("CosyVoice 仍在加载，先用当前语音。", true)

func _show_cosyvoice_dialog() -> bool:
	var dlg := ConfirmationDialog.new()
	dlg.title = "本地语音"
	dlg.dialog_text = "CosyVoice 质量引擎还没起好。\n\n等待：约 1–3 分钟，音色更接近 Azure。\n不等：先只显示文字（不退回系统语音），CosyVoice 起好后自动接上。"
	dlg.ok_button_text = "等待 CosyVoice"
	dlg.cancel_button_text = "先不等"
	dlg.exclusive = true
	add_child(dlg)
	var state := {"closed": false, "wait": false}
	dlg.confirmed.connect(func():
		state["wait"] = true
		state["closed"] = true
	)
	dlg.canceled.connect(func():
		state["closed"] = true
	)
	dlg.popup_centered()
	while not bool(state["closed"]):
		await get_tree().process_frame
	dlg.queue_free()
	return bool(state["wait"])

## 读一次本地 TTS 的 /health；失败 / 超时返回空字典。
func _tts_health_once() -> Dictionary:
	var base := _tts_base_url()
	if base == "":
		return {}
	var req := HTTPRequest.new()
	req.timeout = 2.0
	add_child(req)
	var box := {"done": false, "data": {}}
	req.request_completed.connect(func(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray):
		box["done"] = true
		if result == HTTPRequest.RESULT_SUCCESS and code >= 200 and code < 300:
			var d = JSON.parse_string(body.get_string_from_utf8())
			if typeof(d) == TYPE_DICTIONARY:
				box["data"] = d
	)
	var err := req.request(base + "/health")
	if err != OK:
		req.queue_free()
		return {}
	var deadline := Time.get_ticks_msec() + 2500
	while not bool(box["done"]) and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	req.queue_free()
	return box["data"] as Dictionary

func _cosyvoice_ready(h: Dictionary) -> bool:
	if h.is_empty():
		return false
	return str(h.get("model_dir", "")) != "" or str(h.get("model_dir_sft", "")) != ""

## 启动面板里的本地语音状态：预热在封面 / 设置期间已经启动，这里每秒刷新一次进度。
func _poll_setup_tts_state(label: Label) -> void:
	if DisplayServer.get_name() == "headless":
		return
	var started := Time.get_ticks_msec()
	while is_instance_valid(label):
		if not _local_tts_managed():
			label.text = "本地语音：当前不使用（朗读方式 = %s）" % cfg.tts_mode
			return
		var quality := local_voice_wants_quality(cfg.tts_local_voice)
		var h := await _tts_health_once()
		if not is_instance_valid(label):
			return
		if _cosyvoice_ready(h):
			label.text = "本地语音：CosyVoice 已就绪（Azure 克隆音色）"
			label.add_theme_color_override("font_color", GufengTheme.INK)
			return
		if not h.is_empty():
			label.text = ("本地语音：轻量引擎已就绪（CosyVoice 仍在后台加载）" if quality
					else "本地语音：轻量引擎已就绪")
		else:
			var sec := int((Time.get_ticks_msec() - started) / 1000.0)
			label.text = (("本地语音：CosyVoice 后台加载中… %ds（约 1–3 分钟）" % sec) if quality
					else "本地语音：等待本地服务… %ds" % sec)
		await get_tree().create_timer(1.0).timeout

## 本地 TTS 基地址：去掉尾部 /v1，拼 /health 用。
func _tts_base_url() -> String:
	var base := cfg.tts_local_base_url.strip_edges()
	while base.ends_with("/"):
		base = base.substr(0, base.length() - 1)
	if base.ends_with("/v1"):
		base = base.substr(0, base.length() - 3)
	return base

func _repo_root() -> String:
	return ProjectSettings.globalize_path("res://").simplify_path().path_join("..").simplify_path()

func _dev_local_script() -> String:
	return _repo_root().path_join("dev_local.ps1")

## 本机后端判断：只有 127.0.0.1 / localhost / ::1 才允许自动拉起本地服务。
static func _is_local_url(url: String) -> bool:
	var u := url.strip_edges().to_lower()
	if u.begins_with("http://"):
		u = u.substr(7)
	elif u.begins_with("https://"):
		u = u.substr(8)
	u = u.get_slice("/", 0)
	if u.begins_with("["):
		var close := u.find("]")
		u = u.substr(1, close - 1) if close > 1 else u
	else:
		u = u.get_slice(":", 0)
	return u in ["127.0.0.1", "localhost", "::1", "0.0.0.0"]

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
		elif a.begins_with("--theme="):
			cfg.ui_theme = a.substr("--theme=".length())
			cfg.cli_locked["ui_theme"] = true

## 语音配对状态机：纯决策（谁该显示、谁还在忙、什么该清出），副作用都由信号回到本文件
func _build_pairing() -> void:
	pairing = SpeechPairing.new()
	add_child(pairing)
	pairing.display_requested.connect(_on_pairing_display)
	pairing.resolved.connect(_on_pairing_resolved)

## 对局音效：纯前端、事件驱动；不参与编排与锁
func _build_sfx() -> void:
	sfx = SfxPlayer.new()
	sfx.enabled = cfg.sfx_enabled
	sfx.volume = cfg.sfx_volume
	add_child(sfx)

# ================= 世界与棋盘 =================

func _make_cjk_font() -> Font:
	# 现代深色主题：无衬线优先（字体定义只在 GufengTheme 一处）
	return GufengTheme.font()

func _build_world() -> void:
	var we := WorldEnvironment.new()
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = GufengTheme.WORLD_BG   # 自用版：无背景板 / 无地板，只留主题底色
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = GufengTheme.AMBIENT_COLOR
	env.ambient_light_energy = GufengTheme.AMBIENT_ENERGY
	we.environment = env
	add_child(we)

	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-55, -38, 0)
	light.light_energy = 0.95
	light.shadow_enabled = true
	add_child(light)

	# 环境：只保留风场（自用版删掉了地板 / 背景；化身摆动仍采样同一风）
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
	board.set_ownership_enabled(cfg.ownership_enabled)   # 领地热力图：默认关，打开后等 ownership 事件

# ================= 界面 =================

func _build_ui() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)

	var root := Control.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var th := GufengTheme.build_theme(_font)   # 现代深色主题：没被单独覆盖的控件也吃到这套皮
	root.theme = th
	layer.add_child(root)
	_ui_root = root

	# 卡片先加：它是浮层，任何既有控件（左右栏 / 开关）都压在它上面——只浮在棋盘上，不挤压既有控件
	_build_joseki_card(root)
	_build_estimate_box(root)
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

## 全局形势卡（左上角常驻纸签）：胜率条 / 目差每手更新；点目来自「局面分析」；
## 最后一栏是她落子后那句全局判断（与第三句共用同一份文本）。
## 可折叠；玩家下一手只收棋盘上的点目小方块，卡片本身保留上次点目（标注第 N 手）。
func _build_estimate_box(root: Control) -> void:
	_estimate_box = PanelContainer.new()
	_estimate_box.visible = false                 # 第一份 info / estimate 到了再出现
	_estimate_box.custom_minimum_size = Vector2(272, 0)
	_estimate_box.mouse_filter = Control.MOUSE_FILTER_STOP
	_estimate_box.tooltip_text = "全局形势：胜率 / 目差每手更新；点目来自「局面分析」（只统计归属明确的点）；\n" \
		+ "下面的「她的全局判断」来自她落子后那句，和聊天里第三句是同一份文本。"
	_estimate_box.add_theme_stylebox_override("panel", GufengTheme.slip(GufengTheme.PAPER, 10))
	root.add_child(_estimate_box)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 4)
	_estimate_box.add_child(vb)
	var head := HBoxContainer.new()
	vb.add_child(head)
	var title := Label.new()
	title.text = "全局形势"
	title.add_theme_font_size_override("font_size", 15)
	title.add_theme_color_override("font_color", GufengTheme.DAI)
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(title)
	_card_toggle = Button.new()
	_card_toggle.text = "展开"
	GufengTheme.apply_bare_button(_card_toggle)
	_card_toggle.pressed.connect(_toggle_estimate_body)
	head.add_child(_card_toggle)

	# —— 常驻摘要：胜率条（黑方占比；剩余就是白方） ——
	var wrow := HBoxContainer.new()
	wrow.add_theme_constant_override("separation", 6)
	vb.add_child(wrow)
	_card_wr_black = Label.new()
	_card_wr_black.text = "黑 --"
	_card_wr_black.add_theme_font_size_override("font_size", 12)
	_card_wr_black.add_theme_color_override("font_color", GufengTheme.INK)
	_card_wr_black.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	wrow.add_child(_card_wr_black)
	_card_wr_white = Label.new()
	_card_wr_white.text = "白 --"
	_card_wr_white.add_theme_font_size_override("font_size", 12)
	_card_wr_white.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_card_wr_white.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	wrow.add_child(_card_wr_white)
	_card_wr_bar = ProgressBar.new()
	_card_wr_bar.min_value = 0.0
	_card_wr_bar.max_value = 100.0
	_card_wr_bar.value = 50.0
	_card_wr_bar.show_percentage = false
	_card_wr_bar.custom_minimum_size = Vector2(0, 10)
	_card_wr_bar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var bar_bg := StyleBoxFlat.new()
	var bgc := GufengTheme.INK_SOFT
	bgc.a = 0.18                                  # 浅灰底：能看出白方剩余份额，又不抢墨色填充
	bar_bg.bg_color = bgc
	bar_bg.set_corner_radius_all(3)
	bar_bg.border_width_left = 1
	bar_bg.border_width_top = 1
	bar_bg.border_width_right = 1
	bar_bg.border_width_bottom = 1
	var border := GufengTheme.INK
	border.a = 0.22
	bar_bg.border_color = border
	var bar_fill := StyleBoxFlat.new()
	bar_fill.bg_color = GufengTheme.INK
	bar_fill.set_corner_radius_all(3)
	_card_wr_bar.add_theme_stylebox_override("background", bar_bg)
	_card_wr_bar.add_theme_stylebox_override("fill", bar_fill)
	vb.add_child(_card_wr_bar)

	# 目差留在摘要区：折叠后仍能一眼看到胜率 + 目差
	_card_score = Label.new()
	_card_score.text = "目差: --"
	_card_score.add_theme_font_size_override("font_size", 13)
	_card_score.add_theme_color_override("font_color", GufengTheme.INK)
	vb.add_child(_card_score)
	_card_score_hint = Label.new()
	_card_score_hint.add_theme_font_size_override("font_size", 11)
	_card_score_hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_card_score_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(_card_score_hint)

	# —— 可折叠部分：点目 / 她的全局判断 ——
	_card_body = VBoxContainer.new()
	_card_body.add_theme_constant_override("separation", 2)
	_card_body.visible = false                     # 默认收起：只留标题 + 胜率 + 目差，不挡棋盘视野
	vb.add_child(_card_body)
	_estimate_count = Label.new()
	_estimate_count.text = "点目：点「局面分析」后显示"
	_estimate_count.add_theme_font_size_override("font_size", 12)
	_estimate_count.add_theme_color_override("font_color", GufengTheme.INK)
	_estimate_count.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_card_body.add_child(_estimate_count)
	_estimate_lead = Label.new()
	_estimate_lead.add_theme_font_size_override("font_size", 12)
	_estimate_lead.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_estimate_lead.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_card_body.add_child(_estimate_lead)
	var gtitle := Label.new()
	gtitle.text = "她的全局判断"
	gtitle.add_theme_font_size_override("font_size", 11)
	gtitle.add_theme_color_override("font_color", GufengTheme.DAI)
	_card_body.add_child(gtitle)
	_card_global = Label.new()
	_card_global.text = "她落子后会说一句全局判断。"
	_card_global.add_theme_font_size_override("font_size", 12)
	_card_global.add_theme_color_override("font_color", GufengTheme.INK)
	_card_global.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_card_global.max_lines_visible = 8
	_card_body.add_child(_card_global)
	var hint := Label.new()
	hint.text = "胜率 / 目差每手更新；点目来自「局面分析」，仅供参考。"
	hint.add_theme_font_size_override("font_size", 11)
	hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_card_body.add_child(hint)
	_place_estimate_box()

## 折叠 / 展开卡片正文；标题与胜率条始终可见。
func _toggle_estimate_body() -> void:
	if _card_body == null:
		return
	_card_body.visible = not _card_body.visible
	_card_toggle.text = "收起" if _card_body.visible else "展开"

## 只收棋盘上的点目方块（卡片常驻保留上次点目）；标题 / 复盘 / 新局才整卡收起。
func _clear_estimate_marks() -> void:
	if board != null:
		board.clear_estimate()

## 新局 / 从封面回来：把卡片内容恢复成「等待第一份形势」。
func _reset_global_card() -> void:
	if _card_wr_bar == null:
		return
	_card_wr_bar.value = 50.0
	_card_wr_black.text = "黑 --"
	_card_wr_white.text = "白 --"
	_card_score.text = "目差: --"
	_card_score_hint.text = ""
	_estimate_count.text = "点目：点「局面分析」后显示"
	_estimate_lead.text = ""
	_card_global.text = "她落子后会说一句全局判断。"
	if _card_body != null:                     # 新局默认收起：只留胜率 / 目差，不挡棋盘视野
		_card_body.visible = false
		_card_toggle.text = "展开"
	_last_azure_wr = -1.0                      # 新局：思考短句回到「平静 / 等第一份形势」
	_mood_name = "平静"
	_estimate_move_count = -1
	_last_ai_kind = ""

## 第一份形势 / 从封面回来：把卡片摆出来（折叠状态由玩家自己保持）。
func _show_global_card() -> void:
	if _estimate_box != null and not _estimate_box.visible:
		_estimate_box.visible = true
		_place_estimate_box()

## 形势面板落位：左上角；左栏展开时向右让开，别压在侧栏上（棋盘投影在它下方，不会挡棋子）。
func _place_estimate_box() -> void:
	if _estimate_box == null:
		return
	var x := 20.0 if not _left_open else float(PANEL_W) + 20.0
	_estimate_box.position = Vector2(x, 20.0)

## 默认落位：按**棋盘格子**挑一个不与棋盘 / 已展开侧栏相交、且完整落在窗口内的空白角。
## 原先硬编码「左下角」在 1280×800 下正压棋盘（棋盘近左角伸进卡片)，卡片一出生就被判 faded——故现算。
func _place_joseki_card() -> void:
	if _joseki_card == null:
		return
	_joseki_card.position = _joseki_card_anchor()
	_joseki_card_home = _joseki_card.position

## 窗口尺寸变了：只有用户还没动过卡片（还在我们摆的位置上）才重新摆位，绝不抢用户的手
func _on_viewport_resized() -> void:
	_place_estimate_box()                        # 形势面板钉在左上角：窗口变了只需按侧栏重新算 x
	if _joseki_card == null or _joseki_card.position != _joseki_card_home:
		return
	_place_joseki_card()

## 跟随取景（每 CARD_FOLLOW_SEC 一次）：右键旋转 / 滚轮缩放 / C·Home 换视角 / 侧栏开合之后，
## 棋盘会长到卡片下面。只在**用户没拖过**（位置仍是我们摆的）且卡片真的被盖住或露不全时才重摆；
## 轻微移动不挪它——否则画面一抖卡片就在屏幕上横跳。
func _follow_joseki_card() -> void:
	if _joseki_card == null or not _joseki_card.visible:
		return
	if _joseki_card.position != _joseki_card_home:
		return                                    # 用户拖过：接管权归玩家
	var vp := get_viewport().get_visible_rect().size
	var card := Vector2(JosekiCard.CARD_W, JosekiCard.CARD_H)
	var r := Rect2(_joseki_card.position, card)
	var on_screen := Rect2(Vector2.ZERO, vp).encloses(r)
	var panels := _panel_rects()
	if on_screen and not _hits_rect(r, _board_cell_rects()) and not _hits_rect(r, panels):
		return                                    # 现在的位置就没事：不挪（避免画面一抖卡片横跳）
	var target := _joseki_card_anchor()
	if on_screen and _hits_rect(Rect2(target, card), _board_cell_rects()) \
			and _hits_rect(Rect2(target, card), panels):
		return                                    # 哪都放不下：留在原地，别做无用移动
	_joseki_card.position = target
	_joseki_card_home = target

func _joseki_card_anchor() -> Vector2:
	var vp := get_viewport().get_visible_rect().size
	var board_cells := _board_cell_rects()
	if board_cells.is_empty():
		var br := _board_screen_rect()             # 拿不到格子（相机没就位）：退回外接矩形
		if br.has_area():
			board_cells.append(br)
	var panels: Array = []
	if _left_open and _left_panel != null:
		panels.append(_left_panel.get_global_rect())
	if _right_open and _right_panel != null:
		panels.append(_right_panel.get_global_rect())
	if _estimate_box != null and _estimate_box.visible:
		panels.append(_estimate_box.get_global_rect())    # 形势纸签也是「别压」的矩形
	var prefer := _joseki_card.position if _joseki_card != null else Vector2.ZERO
	return joseki_anchor(vp, board_cells, panels, prefer,
			Vector2(JosekiCard.CARD_W, JosekiCard.CARD_H), _left_open, _right_open)

## 落位纯函数（门卫可直接断言）：五个候选位里挑**可行且离 `prefer` 最近**的那个。
## 可行 = 完整落在窗口内（四边各留 12px）、不与任何棋盘格子 / 已展开侧栏相交。
## 挑「最近的」而不是「第一个」：画面小幅移动时卡片不会在两个候选位之间来回跳。
static func joseki_anchor(vp: Vector2, board: Array, panels: Array, prefer: Vector2,
		card: Vector2, left_open: bool, right_open: bool) -> Vector2:
	var fallback := Vector2(24.0, maxf(24.0, vp.y - card.y - 24.0))
	var w := card.x
	var h := card.y
	var lx := float(PANEL_W) if left_open else 0.0            # 展开的侧栏占掉的左右边界
	var rx := vp.x - (float(PANEL_W) if right_open else 0.0)
	var x_lo := lx + 12.0
	var x_hi := maxf(x_lo, rx - w - 12.0)
	var br := Rect2()
	var has_board := false
	for it in board:
		if typeof(it) != TYPE_RECT2 or not (it as Rect2).has_area():
			continue
		br = (it as Rect2) if not has_board else br.merge(it as Rect2)
		has_board = true
	if not has_board:
		return fallback                                      # 没棋盘投影：退回左下角（旧行为）
	var inside := Rect2(Vector2(12.0, 12.0), vp - Vector2(24.0, 24.0))
	var anchors: Array[Vector2] = [
		Vector2(clampf(br.position.x, x_lo, x_hi), br.end.y + 12.0),                       # ① 棋盘左下外侧
		Vector2(clampf(br.position.x - w - 12.0, x_lo, x_hi), br.get_center().y - h * 0.5), # ② 棋盘左侧居中
		Vector2(clampf(rx - w - 12.0, x_lo, x_hi), br.end.y + 12.0),                       # ③ 右栏左边的空档
		Vector2(clampf(br.get_center().x - w * 0.5, x_lo, x_hi), br.end.y + 12.0),         # ④ 棋盘正下方
		Vector2(clampf(br.position.x, x_lo, x_hi), br.position.y - h - 12.0),              # ⑤ 棋盘左上外侧
	]
	var best := fallback
	var best_d := INF
	var found := false
	for a in anchors:
		var r := Rect2(a, card)
		if not inside.encloses(r):
			continue                                          # 露不全（窗口太矮/太窄）：换下一个
		if _hits_rect(r, board) or _hits_rect(r, panels):
			continue                                          # 压棋盘 / 压侧栏：换下一个
		var d := a.distance_to(prefer)
		if not found or d < best_d:
			found = true
			best_d = d
			best = a
	return best

## 矩形与一组矩形是否相交（空矩形 / 非 Rect2 项一律跳过，不抛）
static func _hits_rect(r: Rect2, rects: Array) -> bool:
	for it in rects:
		if typeof(it) != TYPE_RECT2:
			continue
		var q: Rect2 = it
		if q.has_area() and q.intersects(r):
			return true
	return false

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
	panel.add_theme_stylebox_override("panel", GufengTheme.panel(12))
	root.add_child(panel)

	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	panel.add_child(vb)

	# 标题
	var head := PanelContainer.new()
	head.add_theme_stylebox_override("panel", GufengTheme.header())
	vb.add_child(head)
	var hrow := HBoxContainer.new()
	hrow.add_theme_constant_override("separation", 10)
	head.add_child(hrow)
	var hv := VBoxContainer.new()
	hv.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hrow.add_child(hv)
	var t1 := Label.new()
	t1.text = "Azure · 围棋陪练"
	t1.add_theme_font_size_override("font_size", 20)
	t1.add_theme_color_override("font_color", GufengTheme.PAPER_WARM)
	hv.add_child(t1)
	var t2 := Label.new()
	t2.text = "青蓝色 · 永恒17岁 · 天才少女 · 右键拖拽旋转视角 · 滚轮缩放"
	t2.add_theme_font_size_override("font_size", 11)
	t2.add_theme_color_override("font_color", Color(GufengTheme.PAPER_WARM, 0.88))
	t2.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hv.add_child(t2)
	hrow.add_child(GufengTheme.seal_char("弈"))      # 青蓝单字徽标：整块面板最重的一笔
	_rule(vb)

	# 信息栏
	var info := HBoxContainer.new()
	info.add_theme_constant_override("separation", 10)
	vb.add_child(info)
	_lbl_moves = Label.new()
	_lbl_moves.text = "手数: 0"
	_lbl_moves.add_theme_color_override("font_color", GufengTheme.INK)  # 面板是白底：默认主题白字看不见（R-GH-1）
	info.add_child(_lbl_moves)
	_lbl_wr = Label.new()
	_lbl_wr.text = "黑胜率: --"
	_lbl_wr.add_theme_color_override("font_color", GufengTheme.DAI)
	info.add_child(_lbl_wr)
	# 目差：数字照旧保留（精度不丢）+ 下面一行 11px 小字给量级人话。**竖排不并排**——这一行已经贴着
	# 380 面板的宽度上限，并排会把「允许 Azure 虚着」挤出面板（同 Task F 里定式那一格挪行的理由）。
	var score_col := VBoxContainer.new()
	score_col.add_theme_constant_override("separation", 0)
	_lbl_score = Label.new()
	_lbl_score.text = score_line(null)
	_lbl_score.add_theme_color_override("font_color", GufengTheme.DAI)
	_lbl_score.tooltip_text = "目差 = 黑棋实地估算 − 白棋实地估算（正数＝黑好，负数＝白好）。\n" \
		+ "它和胜率偶尔会不同调（胜率 51% + 目差 -2.5 是正常的），两个一起看；下面那行是量级人话。"
	score_col.add_child(_lbl_score)
	_lbl_score_hint = Label.new()
	_lbl_score_hint.text = ""                      # 量级人话：空串＝还没有形势信息（占位不显示，行高不跳）
	_lbl_score_hint.add_theme_font_size_override("font_size", 11)
	_lbl_score_hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	score_col.add_child(_lbl_score_hint)
	info.add_child(score_col)
	_chk_pass = CheckBox.new()
	_chk_pass.text = "允许Azure虚着"
	_chk_pass.toggled.connect(_on_allow_pass_toggled)
	_style_check(_chk_pass, "允许 Azure 虚着")
	info.add_child(_chk_pass)

	# 定式导学：开关 + 频率合一（关闭 / 低 25% / 中 50% / 高 75% / 总是 100%）。改动即时写回 config.json 并对当前对局生效。
	# OptionButton 比原来的勾选框宽，单起一行——否则会把「手数 / 胜率 / 目差 / 允许虚着」那一行挤出面板。
	var joseki_row := HBoxContainer.new()
	vb.add_child(joseki_row)
	_joseki_opt = OptionButton.new()
	_joseki_opt.add_item("定式导学：关闭", 0)
	_joseki_opt.add_item("定式导学：低 25%", 1)
	_joseki_opt.add_item("定式导学：中 50%", 2)
	_joseki_opt.add_item("定式导学：高 75%", 3)
	_joseki_opt.add_item("定式导学：总是 100%", 4)
	_joseki_opt.tooltip_text = "识别对局中走出的定式：讲解要点、在棋盘上高亮手顺轨迹。\n" \
		+ "· 关闭 = 不识别、不打扰\n" \
		+ "· 低 25% / 中 50% / 高 75% = 有资格提示时按该概率提示（讲解与卡片只看「有没有资格」，不重复掷骰）\n" \
		+ "· 总是 100% = 有资格就一定讲（复测定式功能时用这一档）"
	_joseki_opt.selected = joseki_level_index(cfg.joseki_teach, cfg.joseki_rate)   # 只显示，不回写 config.json
	_joseki_opt.item_selected.connect(_on_joseki_level_selected)
	joseki_row.add_child(_joseki_opt)

	# 说话详略（v3.11.1）：原来的「短回复」布尔开关扩成三档——精简 / 适中 / 详细。
	# 只改**提示词末尾的长度约束**（后端 `reply_style`），即时生效并写回 config.json。
	var acc := HBoxContainer.new()
	acc.add_theme_constant_override("separation", 12)
	vb.add_child(acc)
	_reply_opt = OptionButton.new()
	_reply_opt.add_item("说话：精简", 0)
	_reply_opt.add_item("说话：适中", 1)
	_reply_opt.add_item("说话：详细", 2)
	_reply_opt.tooltip_text = "她每次说多少（即时生效，并写回 config.json）：\n" \
		+ "· 精简 = 点评 1 句、聊天最多 2 句 → 语音等待最短（默认）\n" \
		+ "· 适中 = 不加额外约束（按人设自己收着说）\n" \
		+ "· 详细 = 允许展开 2~3 句（约 80 字内），仍不复述局面"
	_reply_opt.selected = clampi(cfg.reply_style, 0, 2)   # 只显示，不回写 config.json
	_reply_opt.item_selected.connect(_on_reply_style_selected)
	acc.add_child(_reply_opt)
	_btn_settings = Button.new()
	_btn_settings.text = "设置 ▸"
	_btn_settings.toggle_mode = true
	_btn_settings.tooltip_text = "一次性设置：同步说话 / 整段合成（立即生效）、长句合并（重启后端后生效）；" \
		+ "改动都写回 config.json"
	_btn_settings.toggled.connect(_on_settings_toggled)
	acc.add_child(_btn_settings)

	_settings_box = PanelContainer.new()
	_settings_box.visible = false                  # 默认收起：右栏只留对局常用项
	_settings_box.add_theme_stylebox_override("panel", GufengTheme.slip(GufengTheme.PAPER, 10))
	vb.add_child(_settings_box)
	var drawer := VBoxContainer.new()
	drawer.add_theme_constant_override("separation", 6)
	_settings_box.add_child(drawer)
	# 思考强度（v3.11.1）：从启动面板搬到这里，并且**运行期即时生效**——后端 `/api/config` 直接改档位
	# （推理预算 + 扩展字段一起跟着变），不用再重启后端。启动面板里的同一项仍写 config.json，两边同源。
	_think_opt = OptionButton.new()
	_think_opt.add_item("思考强度：快速", 0)
	_think_opt.add_item("思考强度：中等", 1)
	_think_opt.add_item("思考强度：强思考", 2)
	_think_opt.tooltip_text = "她回复前想多久（即时生效，并写回 config.json）：\n" \
		+ "· 快速 = 不思考，首字最快（默认，推荐边下边聊）\n" \
		+ "· 中等 = 先想几句再答，慢 3~6 秒\n" \
		+ "· 强思考 = 充分推理再回答，更慢、质量更好"
	_think_opt.selected = clampi(cfg.llm_think_mode, 0, 2)   # 只显示，不回写 config.json
	_think_opt.item_selected.connect(_on_think_mode_selected)
	drawer.add_child(_think_opt)
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
	# 对局音效（v3.11.5）：纯前端；落子 / 提子 / 虚着 / 终局，音量与开关写回 config.json
	_acc_check(drawer, "音效", cfg.sfx_enabled, "sfx",
		"落子、提子、虚着、终局的轻音效；关闭后完全静音（不影响语音朗读）→ 立即生效")
	_acc_check(drawer, "棋盘坐标", true, "coords",
		"显示 / 隐藏棋盘四边的 A–T、19–1 坐标（本局内生效，不改 config.json）")
	var chat_font_opt := OptionButton.new()
	chat_font_opt.add_item("聊天字号：小 13", 0)
	chat_font_opt.add_item("聊天字号：标准 15", 1)
	chat_font_opt.add_item("聊天字号：大 17", 2)
	chat_font_opt.selected = 1
	chat_font_opt.tooltip_text = "只改聊天正文，本局内生效（不改 config.json）"
	chat_font_opt.item_selected.connect(_on_chat_font_selected)
	drawer.add_child(chat_font_opt)
	var chat_gap_opt := OptionButton.new()
	chat_gap_opt.add_item("聊天行距：紧", 0)
	chat_gap_opt.add_item("聊天行距：标准", 1)
	chat_gap_opt.add_item("聊天行距：宽", 2)
	chat_gap_opt.selected = 1
	chat_gap_opt.tooltip_text = "气泡内行距，本局内生效（不改 config.json）"
	chat_gap_opt.item_selected.connect(_on_chat_gap_selected)
	drawer.add_child(chat_gap_opt)
	var sfx_vol_lbl := _field_label("音效音量")
	sfx_vol_lbl.tooltip_text = "只控制落子/提子/虚着/终局音效；语音朗读音量在左侧栏单独调"
	drawer.add_child(sfx_vol_lbl)
	var sfx_vol := HSlider.new()
	sfx_vol.min_value = 0.0
	sfx_vol.max_value = 1.0
	sfx_vol.step = 0.05
	sfx_vol.value = cfg.sfx_volume
	sfx_vol.custom_minimum_size = Vector2(0, 18)
	sfx_vol.value_changed.connect(func(v): _on_sfx_volume(v, false))
	sfx_vol.drag_ended.connect(func(_c): _on_sfx_volume(sfx_vol.value, true))
	drawer.add_child(sfx_vol)
	# 领地热力图（v3.11.5）：默认关；后端每次落子后复用既有分析推 ownership，前端只渲染
	_acc_check(drawer, "领地热力图", cfg.ownership_enabled, "ownership",
		"按 KataGo 逐点归属显示双方势力（墨色=黑 / 纸白=白），只做视觉辅助、不改变对局；关闭后立即隐藏")
	# 自适应难度（v3.11.5）：开关 + 基线同一行（不新增常驻控件位）；局内只在这条基线上下最多 ±1 档浮动
	var adaptive_row := HBoxContainer.new()
	adaptive_row.add_theme_constant_override("separation", 6)
	drawer.add_child(adaptive_row)
	_acc_check(adaptive_row, "自适应难度", cfg.adaptive_difficulty, "adaptive",
		"默认关。开启后按你的棋风画像、聊天（「太难 / 认真点」）与最近几手表现，"
		+ "在基线上下最多浮动 1 档；每 4 手最多调整一次，不会随意跳。关闭＝落点与原来完全一致")
	_adaptive_base_opt = OptionButton.new()
	for nm in ADAPTIVE_BASE_NAMES:
		_adaptive_base_opt.add_item("基线：" + nm)
	_adaptive_base_opt.selected = clampi(cfg.adaptive_difficulty_base, 1, 5) - 1
	_adaptive_base_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_adaptive_base_opt.tooltip_text = "1 入门 / 2 轻松 / 3 均衡（默认）/ 4 挑战 / 5 严苛。\n" \
		+ "自适应开启时，当前档位只在这条基线 ±1 档内移动；关掉后基线只用于下次开启。"
	_adaptive_base_opt.item_selected.connect(_on_adaptive_base_selected)
	adaptive_row.add_child(_adaptive_base_opt)
	# 聊天摘要用 LLM（v3.8.0）：**默认关**——聊天摘要走规则式（纯函数分段 + 模板摘要）、零额外 LLM 调用；
	# 开启后只在**局末 / 空闲**用 LLM 补做分段总结（落子回合内绝不加调用）。写回 config.json 并即时生效。
	_acc_check(drawer, "聊天摘要用 LLM", cfg.chat_digest_llm, "digest",
		"默认关：聊天摘要走规则式、**零额外 LLM 调用**；开启：只在**局末 / 空闲**用 LLM 分段总结"
		+ "（落子回合内绝不加调用）→ 立即生效")
	# 玩家棋风画像 + 时间感知（v3.9.0）：两个开关与只读面板**同一件事**，一起放在这一格
	# （控制者倾向：与 emotion_drift / chat_digest_llm 同形；两个开关**一次推一次** `/api/config`）。
	_acc_check(drawer, "玩家棋风画像", cfg.profile_enabled, "profile",
		"默认关。开启后，每次点「结束」会把这局记进后端的 profile.db，并跨局攒出「她眼中的你」"
		+ "（开局偏好 / 行棋风格 / 常见失误 / 常聊话题）；她只在相关时自然参考这份印象 → 立即生效")
	_acc_check(drawer, "时间感知", cfg.time_aware_enabled, "time_aware",
		"默认关。开启后她知道现在几点、这一局下了多久、你多久没来、连续第几天——只在相关时提，"
		+ "不会把数字当口头禅 → 立即生效")
	# 人格化（自用版）：关系阶段 / 情绪余温 / 今天的小心思 / 夜間节奏 / 难度与偏航的半句提示。
	_acc_check(drawer, "人格化", cfg.persona_enabled, "persona",
		"默认开。她会记得你们一起下过多少盘、上一局的情绪余温、偶尔有点自己的小心思；"
		+ "深夜放慢搭话，你长考时少打扰。关掉＝提示词逐字符不变、不写跨局状态 → 立即生效")
	# 关系阶段与称呼变化：默认关；开启后可填高阶段称呼名（留空=仍叫 prompt_user_name）。
	var rel_row := HBoxContainer.new()
	rel_row.add_theme_constant_override("separation", 6)
	drawer.add_child(rel_row)
	_acc_check(rel_row, "关系称呼", cfg.persona_relationship_enabled, "persona_relationship",
		"默认关。开启后按一起下完的局数推进称呼（初识 → 熟悉 → 熟络 → 老搭档）。"
		+ "右侧填高阶段称呼名；留空则始终用 config.json 的 prompt_user_name（默认 Estarth）。")
	var le_addr := LineEdit.new()
	le_addr.placeholder_text = "称呼名（留空=%s）" % cfg.prompt_user_name
	le_addr.text = cfg.persona_address_name
	le_addr.max_length = 16
	le_addr.custom_minimum_size = Vector2(120, 0)
	le_addr.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	le_addr.tooltip_text = "关系到了「熟络 / 老搭档」后才偶尔使用；最多 16 字。留空=始终用 prompt_user_name。"
	le_addr.text_submitted.connect(func(t): _on_persona_address_changed(t))
	le_addr.focus_exited.connect(func(): _on_persona_address_changed(le_addr.text))
	rel_row.add_child(le_addr)
	# 只读面板：一个按钮 + 一块文本（**不新增右栏常驻控件位**，与 joseki / drift / digest 同风格）。
	# 不并进 `_buttons`：它是只读 GET，复盘期间也允许看（冻结清单只锁「会改动局面 / 会让她开口」的控件）。
	var prof_row := HBoxContainer.new()
	prof_row.add_theme_constant_override("separation", 6)
	drawer.add_child(prof_row)
	_button(prof_row, "她眼中的你", _on_profile_panel)
	_lbl_profile = Label.new()
	_lbl_profile.add_theme_font_size_override("font_size", 11)
	_lbl_profile.add_theme_color_override("font_color", GufengTheme.INK)
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
	_chat_box.add_theme_constant_override("separation", _chat_line_px)
	_chat_scroll.add_child(_chat_box)
	_chat_scroll.get_v_scroll_bar().value_changed.connect(_on_chat_scrolled)

	# 新消息提示（只在玩家翻上去看旧消息时出现）
	_btn_new_msg = Button.new()
	_btn_new_msg.text = "↓ 新消息"
	_btn_new_msg.visible = false
	_btn_new_msg.size_flags_horizontal = Control.SIZE_SHRINK_END
	_btn_new_msg.tooltip_text = "回到最新消息"
	GufengTheme.apply_bare_button(_btn_new_msg)
	_btn_new_msg.pressed.connect(_scroll_to_bottom.bind(true))
	vb.add_child(_btn_new_msg)

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
	_lbl_queue.add_theme_font_size_override("font_size", 12)
	_lbl_queue.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_lbl_queue.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_lbl_queue.visible = false
	vb.add_child(_lbl_queue)

	_rule(vb, 0.18)
	# 功能按钮
	var btn_row := HBoxContainer.new()
	btn_row.add_theme_constant_override("separation", 6)
	vb.add_child(btn_row)
	_buttons.append(_button(btn_row, "提示", func():
		_allow_tts_fillers()
		agent.hint()))
	_buttons.append(_button(btn_row, "局面分析", func():
		_allow_tts_fillers()
		agent.analyze_position()))
	_buttons.append(_button(btn_row, "重来", _ask_reset))
	_btn_reset = _buttons[_buttons.size() - 1]
	var end_btn := _button(btn_row, "结束", _ask_end)
	GufengTheme.apply_seal_button(end_btn)          # 主操作：青蓝主按钮（现代主题里唯一的高饱和块）
	_buttons.append(end_btn)

	# 复盘（v3.7.0）：**只在对局结束后**出现（未结束整组隐藏 ⇒ 界面不变挤，V19）。
	# 进复盘后棋盘只读、上面四个按钮置灰、聊天禁用；◀ / ▶ 与 ← / → 只在复盘模式内生效。
	_review_box = HBoxContainer.new()
	_review_box.add_theme_constant_override("separation", 6)
	_review_box.visible = false
	vb.add_child(_review_box)
	_btn_prev = _button(_review_box, "上一手", func(): _review_step(-1))
	_btn_next = _button(_review_box, "下一手", func(): _review_step(1))
	_btn_prev.visible = false
	_btn_next.visible = false
	_btn_review = _button(_review_box, "复盘", _on_review_toggle)
	_btn_review.tooltip_text = "只读回看这一局：棋盘不接受落子与摸头，她也不开口（← / → 翻页）"
	# v3.11.3：局后给一个**明确的出口**——不用先想起 Esc，直接回封面（开始新局 / 设置 / 退出）
	_btn_home = _button(_review_box, "返回主界面", _on_back_to_title)
	_btn_home.tooltip_text = "收起这局，回主界面（可开新局 / 改设置 / 退出；对局中按 Esc 也是这里）"
	_btn_report = _button(_review_box, "战报", _on_report_request)
	_btn_report.tooltip_text = "生成一张 PNG 战报：棋盘、手顺、胜率/目差、关键点评与亏损手（保存到用户目录并复制路径）"

	# 全盘 AI 复盘（v3.11.5）：只有从棋谱库选一盘进来时才显示这一行，不挤普通界面。
	# 棋盘是**本地回放**（GameReplay），后端只按段做分析与点评，不碰当前对局。
	_fr_box = HBoxContainer.new()
	_fr_box.add_theme_constant_override("separation", 6)
	_fr_box.visible = false
	vb.add_child(_fr_box)
	_fr_prev = _button(_fr_box, "上一手", func(): _fr_step_by(-1))
	_fr_prev.tooltip_text = "只在这个模式下翻看棋谱；自动解说会继续按段落推进"
	_fr_next = _button(_fr_box, "下一手", func(): _fr_step_by(1))
	_fr_toggle = _button(_fr_box, "暂停", _on_fr_toggle)
	_fr_toggle.tooltip_text = "暂停 / 继续自动解说（当前这段说完后停下）"
	_btn_fr_exit = _button(_fr_box, "退出复盘", _exit_full_review)
	GufengTheme.apply_seal_button(_btn_fr_exit, false)
	_btn_fr_exit.tooltip_text = "结束全盘复盘，棋盘回到当前对局"

	# 俗手回档
	var bad_title := Label.new()
	bad_title.text = "俗手回档（点一下回到那手之前）"
	bad_title.add_theme_font_size_override("font_size", 12)
	bad_title.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	vb.add_child(bad_title)
	_bad_box = HBoxContainer.new()
	_bad_box.add_theme_constant_override("separation", 6)
	vb.add_child(_bad_box)
	_render_bad_moves([])

	_lbl_status = Label.new()
	_lbl_status.add_theme_font_size_override("font_size", 12)
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
	panel.add_theme_stylebox_override("panel", GufengTheme.panel(12))
	root.add_child(panel)

	# 这一栏控件多，窗口一矮就被裁掉下半截：整栏可滚（右栏有聊天区自己会缩，不需要）
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	panel.add_child(scroll)

	var vb := VBoxContainer.new()
	vb.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	vb.add_theme_constant_override("separation", 8)
	scroll.add_child(vb)

	var head := PanelContainer.new()
	head.add_theme_stylebox_override("panel", GufengTheme.header())
	vb.add_child(head)
	var hrow := HBoxContainer.new()
	hrow.add_theme_constant_override("separation", 10)
	head.add_child(hrow)
	var hv := VBoxContainer.new()
	hv.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hrow.add_child(hv)
	var t1 := Label.new()
	t1.text = "语音朗读"
	t1.add_theme_font_size_override("font_size", 18)
	t1.add_theme_color_override("font_color", GufengTheme.PAPER_WARM)
	hv.add_child(t1)
	var t2 := Label.new()
	t2.text = "云端 OpenAI 兼容语音服务 / Windows 系统语音 · 侧栏收起后依然朗读"
	t2.add_theme_font_size_override("font_size", 11)
	t2.add_theme_color_override("font_color", Color(GufengTheme.PAPER_WARM, 0.88))
	t2.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hv.add_child(t2)
	hrow.add_child(GufengTheme.seal_char("音"))
	_rule(vb)

	var chk := CheckBox.new()
	chk.button_pressed = cfg.tts_enabled
	chk.toggled.connect(_on_tts_enabled)
	_style_check(chk, "朗读 Azure 的话")
	vb.add_child(chk)

	vb.add_child(_field_label("朗读方式"))
	var opt := OptionButton.new()
	opt.fit_to_longest_item = false                  # 不让长选项文字撑宽面板（保证左右侧栏等宽）
	opt.add_item("系统语音", 0)
	opt.add_item("本地 CosyVoice", 1)
	opt.add_item("云端 OpenAI 兼容", 2)
	opt.add_item("阿里云 Voice Studio（实时流式）", 3)
	opt.selected = maxi(0, ["system", "local", "openai", "aliyun"].find(cfg.tts_mode))
	opt.item_selected.connect(_on_tts_mode)
	vb.add_child(opt)

	# 阿里云配置直接挂在朗读方式下拉下面：选阿里云才展开，其他模式收起。
	# Key 与平台音色 ID 各留各的，切模式不互相覆盖。
	_tts_aliyun_box = VBoxContainer.new()
	_tts_aliyun_box.add_theme_constant_override("separation", 4)
	_tts_aliyun_box.visible = cfg.tts_mode == "aliyun"
	vb.add_child(_tts_aliyun_box)

	_tts_aliyun_box.add_child(_field_label("阿里云 API Key"))
	_tts_aliyun_key_edit = LineEdit.new()
	_tts_aliyun_key_edit.text = cfg.tts_aliyun_api_key
	_tts_aliyun_key_edit.secret = true
	_tts_aliyun_key_edit.placeholder_text = "sk-…（Voice Studio 签发）"
	_tts_aliyun_key_edit.tooltip_text = "Voice Studio 控制台签发的 API Key；百炼原生 Key 无法通过鉴权（返回 401）"
	_tts_aliyun_key_edit.text_submitted.connect(func(_t): _apply_tts_fields())
	_tts_aliyun_key_edit.focus_exited.connect(_apply_tts_fields)
	_tts_aliyun_box.add_child(_tts_aliyun_key_edit)

	_tts_aliyun_box.add_child(_field_label("阿里云音色 ID（UUID）"))
	_tts_aliyun_voice_edit = LineEdit.new()
	_tts_aliyun_voice_edit.text = cfg.tts_aliyun_voice
	_tts_aliyun_voice_edit.placeholder_text = "在 Voice Studio「音色管理」里复制"
	_tts_aliyun_voice_edit.tooltip_text = "平台音色 ID（UUID 形式）；不接受 {model}:{voice} 或裸音色名"
	_tts_aliyun_voice_edit.text_submitted.connect(func(_t): _apply_tts_fields())
	_tts_aliyun_voice_edit.focus_exited.connect(_apply_tts_fields)
	_tts_aliyun_box.add_child(_tts_aliyun_voice_edit)

	vb.add_child(_field_label("云端服务地址（选云端 OpenAI 时填写）"))
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

	_tts_route = Label.new()
	_tts_route.add_theme_font_size_override("font_size", 11)
	_tts_route.add_theme_color_override("font_color", GufengTheme.DAI)
	_tts_route.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(_tts_route)

	_tts_status = Label.new()
	_tts_status.text = "未开启朗读"
	_tts_status.add_theme_font_size_override("font_size", 11)
	_tts_status.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	_tts_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(_tts_status)
	_refresh_tts_route()

	var tip := Label.new()
	tip.text = "语音由后端合成：系统＝Windows；本地＝只走本地 CosyVoice/MeloTTS（失败回落系统语音）；云端＝OpenAI 兼容（有本地地址时仍本地优先）；阿里云＝Voice Studio 实时流式。本端只负责播放。\n" \
		+ "· 本地地址 / 音色写在 config.json 的 tts_local_base_url / tts_local_voice（dev_local.ps1 会自动写入并切到本地）\n" \
		+ "· 上面第 3 项才是云端备选；第 4 项阿里云要填「API Key + 平台音色 ID」\n" \
		+ "· 切换朗读方式后重启后端生效\n" \
		+ "· 本地/云端/阿里云都不可用时，后端直接用系统语音念"
	tip.add_theme_font_size_override("font_size", 11)
	tip.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	tip.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(tip)

func _field_label(text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.add_theme_font_size_override("font_size", 12)
	l.add_theme_color_override("font_color", GufengTheme.INK)
	return l

## 统一勾选项样式：面板是深底，勾选框文字统一用亮色
## 一条细分隔线（分节用）：现代版式靠直线与留白分区，不靠卡片阴影
func _rule(parent: Node, alpha := 0.26) -> void:
	var r := ColorRect.new()
	r.color = Color(GufengTheme.INK, alpha)
	r.custom_minimum_size = Vector2(0, 1)
	r.mouse_filter = Control.MOUSE_FILTER_IGNORE
	parent.add_child(r)

func _style_check(chk: CheckBox, txt := "") -> void:
	if txt != "":
		chk.text = txt
	var dark := GufengTheme.INK
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
	GufengTheme.apply_bare_button(_right_toggle)
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
	GufengTheme.apply_bare_button(_left_toggle)
	root.add_child(_left_toggle)

func _set_panel_open(which: String, open: bool) -> void:
	if which == "left":
		_left_open = open
	else:
		_right_open = open
	_apply_panel_layout(true)

## 侧栏位置：展开时贴边显示，收起时整体滑出画面；棋盘取景按两侧占用自动让位。
## `apply_board=false` 只滑面板、不改棋盘取景（主界面运镜用：右侧让位一直保留，飞入时棋盘不跳）。
func _apply_panel_layout(animated := true, dur := 0.18, apply_board := true) -> void:
	var rl := -float(PANEL_W) if _right_open else 0.0
	var rr := 0.0 if _right_open else float(PANEL_W)
	var ll := 0.0 if _left_open else -float(PANEL_W)
	var lr := float(PANEL_W) if _left_open else 0.0
	if animated:
		var tw := create_tween().set_parallel(true)
		tw.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)   # 工具栏「飞入」的手感：起步快、贴边稳
		tw.tween_property(_right_panel, "offset_left", rl, dur)
		tw.tween_property(_right_panel, "offset_right", rr, dur)
		tw.tween_property(_left_panel, "offset_left", ll, dur)
		tw.tween_property(_left_panel, "offset_right", lr, dur)
	else:
		_right_panel.offset_left = rl
		_right_panel.offset_right = rr
		_left_panel.offset_left = ll
		_left_panel.offset_right = lr
	_right_toggle.text = "▶" if _right_open else "◀"
	_left_toggle.text = "◀" if _left_open else "▶"
	_place_estimate_box()                        # 左栏一开合，形势面板立刻跟着让位（不压在侧栏上）
	if apply_board and board != null:
		board.set_panels(_left_open, _right_open)

## 刷新「实际朗读」只读提示：本地优先 → 云端 → 系统（后端读同一份 config.json）
func _refresh_tts_route() -> void:
	if _tts_route != null:
		_tts_route.text = "实际朗读：" + cfg.tts_route_label()

func _on_tts_enabled(v: bool) -> void:
	cfg.tts_enabled = v
	if tts != null:
		tts.enabled = v
		if not v:
			tts.stop()
	_tts_status.text = ("已开启朗读 · " + cfg.tts_route_label()) if v else "未开启朗读"
	_refresh_tts_route()
	cfg.save()

func _on_tts_mode(i: int) -> void:
	var m: String = ["system", "local", "openai", "aliyun"][clampi(i, 0, 3)]
	if m == cfg.tts_mode:
		return                       # 程序化赋值也可能触发该信号：值没变就不写配置
	cfg.tts_mode = m
	if tts != null:
		tts.mode = m                 # 本地镜像：只影响「导演层犹豫节拍」的快慢估计
		# 本地模式按本地音色估引擎快慢；云端 / 系统 / 阿里云沿用云端音色
		tts.voice = (cfg.tts_local_voice if m == "local" and cfg.tts_local_voice != "" else cfg.tts_voice)
		tts.stop()
	cfg.save()
	_fill_voice_items()               # 本地 / 云端两套音色候选不同：切换朗读方式后重填
	_tts_status.text = "朗读方式：%s（合成在后端，重启后端后生效）" % m
	if _tts_aliyun_box != null:
		_tts_aliyun_box.visible = m == "aliyun"
	_refresh_tts_route()

func _apply_tts_fields() -> void:
	var url := _tts_url_edit.text.strip_edges()
	var model := _tts_model_edit.text.strip_edges()
	var voice := _tts_voice_edit.text.strip_edges()
	var akey := cfg.tts_aliyun_api_key
	var avoice := cfg.tts_aliyun_voice
	if _tts_aliyun_key_edit != null:              # 面板未建好时（极端时序）保持原值
		akey = _tts_aliyun_key_edit.text.strip_edges()
	if _tts_aliyun_voice_edit != null:
		avoice = _tts_aliyun_voice_edit.text.strip_edges()
	if url == cfg.tts_base_url and model == cfg.tts_model and voice == cfg.tts_voice \
			and akey == cfg.tts_aliyun_api_key and avoice == cfg.tts_aliyun_voice:
		return                       # 仅在用户真正改动时落盘，避免启动时把旧值写回去
	cfg.tts_base_url = url
	cfg.tts_model = model
	cfg.tts_voice = voice
	cfg.tts_aliyun_api_key = akey
	cfg.tts_aliyun_voice = avoice
	if tts != null:
		# 合成归后端：地址/模型/阿里云 Key 与音色 ID 都不由前端使用（后端读同一份 config.json），
		# 音色本端系统语音还要用
		tts.voice = voice
	cfg.save()
	_tts_status.text = "语音设置已保存（合成服务地址/模型/阿里云参数由后端读取，重启后端后生效）"
	_refresh_tts_route()

## 音色下拉：云端服务商各有音色名，这里只填静态候选；具体 id 也可在右侧输入框手填（不再向语音服务探测）
func _fill_voice_items() -> void:
	if _tts_voice_opt == null:
		return
	if cfg.tts_mode == "local":                         # 本地模式：显示经典版 Azure 克隆等本机音色
		_fill_local_voice_items()
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

## 本地音色下拉：把经典版的 Azure 克隆音色加回来；当前值是未知 id 时也保留一行。
func _fill_local_voice_items() -> void:
	var cur := cfg.tts_local_voice.strip_edges()
	if cur == "":
		cur = "azure"
	_tts_voice_opt.clear()
	var found := false
	for pair in LOCAL_VOICE_PRESETS:
		_tts_voice_opt.add_item(str(pair[1]))
		_tts_voice_opt.set_item_metadata(_tts_voice_opt.item_count - 1, str(pair[0]))
		if str(pair[0]) == cur:
			found = true
	if not found:
		_tts_voice_opt.add_item("当前：%s" % cur)
		_tts_voice_opt.set_item_metadata(_tts_voice_opt.item_count - 1, cur)
	for i in _tts_voice_opt.item_count:
		if str(_tts_voice_opt.get_item_metadata(i)) == cur:
			_tts_voice_opt.select(i)
			break

## 已移除的音色（男声 / 韩语女 / 旧克隆音色）：清单以 AzureConfig.LEGACY_TTS_VOICES 为准，不再各存一份
static func _is_removed_voice(v: String) -> bool:
	return v in AzureConfig.LEGACY_TTS_VOICES

func _on_voice_selected(i: int) -> void:
	var vid := str(_tts_voice_opt.get_item_metadata(i))
	if vid == "":
		return
	if cfg.tts_mode == "local":
		await _apply_local_voice(vid)
		return
	if vid == cfg.tts_voice:
		return
	cfg.tts_voice = vid
	_tts_voice_edit.text = vid
	if tts != null:
		tts.voice = vid
		tts.stop()
	cfg.save()
	_tts_status.text = "音色已切换：%s" % vid

## 本地音色：写 config + 通知后端内存立即生效（不重启后端、不丢棋局）；
## 需要质量引擎而当前不是时，用 `-TtsOnly` 在后台换引擎，加载期只显示文字（不退回系统语音）。
func _apply_local_voice(vid: String) -> void:
	if vid == cfg.tts_local_voice:
		return
	cfg.tts_local_voice = vid
	cfg.save()
	if tts != null:
		tts.voice = vid
		tts.stop()
	if agent != null:
		agent.set_tts_local_voice(vid)              # 后端 /api/config：下一句就用新音色
	_refresh_tts_route()
	if local_voice_wants_quality(vid):
		var h := await _tts_health_once()
		if _cosyvoice_ready(h):
			_local_tts_loading = false
			if tts != null:
				tts.set_suppress_system(false)
			_tts_status.text = "本地音色已切换：%s（立即生效）" % vid
			return
		_local_tts_loading = true
		if tts != null:
			tts.set_suppress_system(true)
		_maybe_start_local_services(true, false, true)   # 只起语音、换质量引擎；不写配置、不动后端
		_watch_local_tts_ready()
		_tts_status.text = "已切到 %s：CosyVoice 后台加载中，先只显示文字" % vid
		return
	# 轻量音色：质量引擎也能念它，当前有服务就不必停掉重起；没服务才拉一个轻量。
	var h2 := await _tts_health_once()
	_local_tts_loading = false
	if tts != null:
		tts.set_suppress_system(false)
	if h2.is_empty():
		_maybe_start_local_services(false, false, true)
	_tts_status.text = "本地音色已切换：%s（轻量引擎）" % vid

## 「重填音色候选」按钮：音色不再由服务端提供，这里只按静态候选重填一次下拉框
func _refresh_voices() -> void:
	_fill_voice_items()
	if cfg.tts_mode == "local":
		_tts_status.text = "本地音色候选已重填（%d 项）" % _tts_voice_opt.item_count
	else:
		_tts_status.text = "音色候选已重填（%d 项，也可手填服务商音色）" % _tts_voice_opt.item_count

## ⚙ 抽屉里这些开关只进状态栏（不是语音项）。集中成一张表，新增开关不会再漏写状态文案。
const ACC_STATUS_NAMES := {
	"digest": "聊天摘要用 LLM",
	"profile": "玩家棋风画像",
	"time_aware": "时间感知",
	"persona": "人格化",
	"persona_relationship": "关系称呼",
	"sfx": "音效",
	"ownership": "领地热力图",
	"adaptive": "自适应难度",
}

## 关系称呼名（⚙ 抽屉）：失焦 / 回车时写盘并推给后端；留空=仍叫 prompt_user_name。
func _on_persona_address_changed(text: String) -> void:
	var s := str(text).strip_edges().substr(0, 16)
	if s == cfg.persona_address_name:
		return
	cfg.persona_address_name = s
	cfg.save()
	if agent != null:
		agent.set_persona_relationship(cfg.persona_relationship_enabled, s)
	_set_status("关系称呼：%s" % (s if s != "" else "仍叫 %s" % cfg.prompt_user_name))

## 右侧栏语音开关：立即存盘并即时生效（都不需要重启任何服务）
func _on_acc_toggled(on: bool, which: String) -> void:
	match which:
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
		"sfx":
			cfg.sfx_enabled = on
			if sfx != null:
				sfx.enabled = on
		"coords":
			if board != null:
				board.set_coords_visible(on)
			_set_status("棋盘坐标已%s（本局内生效）" % ("显示" if on else "隐藏"))
			return
		"ownership":
			cfg.ownership_enabled = on
			if board != null:
				board.set_ownership_enabled(on)
		"adaptive":
			cfg.adaptive_difficulty = on
			if agent != null:
				# 原子入口：开关 + 基线一次推一次；后端会回到基线并推一条 difficulty 事件
				agent.set_adaptive(on, cfg.adaptive_difficulty_base)
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
		"persona":
			cfg.persona_enabled = on
			if agent != null:
				agent.set_persona(on)
		"persona_relationship":
			cfg.persona_relationship_enabled = on
			if agent != null:
				agent.set_persona_relationship(on, cfg.persona_address_name)
	cfg.save()
	if ACC_STATUS_NAMES.has(which):
		_set_status("%s 已%s（立即生效）" % [ACC_STATUS_NAMES[which], "开启" if on else "关闭"])
		return
	var nm := "同步说话" if which == "sync" else ("整段合成" if which == "batch" else "长句合并")
	var scope := "（重启后端后生效）" if which == "chunk" else "（立即生效）"
	_tts_status.text = "%s：%s%s" % [nm, "开" if on else "关", scope]
	_set_status("语音：%s已%s" % [nm, "开启" if on else "关闭"])

func _on_tts_speed(v: float, persist: bool) -> void:
	cfg.tts_speed = v
	if tts != null:
		tts.speed = v
	if persist:
		cfg.save()

## 音效音量（右侧 ⚙ 抽屉）：拖动即时生效，松手才写盘 + 状态栏提示
func _on_sfx_volume(v: float, persist: bool) -> void:
	cfg.sfx_volume = clampf(v, 0.0, 1.0)
	if sfx != null:
		sfx.volume = cfg.sfx_volume
	if persist:
		cfg.save()
		_set_status("音效音量：%d%%" % int(round(cfg.sfx_volume * 100.0)))

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
	if kind == "ai_review" and not _fr_active:
		if wants_speech:
			_register_tomb("")                   # 已经退出全盘复盘：迟到的点评连同它的语音一起丢掉
		return
	if kind == "ai_review":
		_fr_msg_seen = true                      # 这一段台词已落到前端（段间等待据此不再空等）
	if kind == "ai_idle" and _idle_muted:
		_register_tomb(speak_kind)           # 本地作废：留个占位吃掉它自己那条 speech
		return                               # 玩家已经回来继续下棋：这句走神搭话作废（不出声也不显示）
	var tone := ""
	if is_ai:
		var tone_parsed := _strip_tone(text)  # 剥离 [[tone:xxx]] 语气标记（玩家不可见、TTS 不朗读）
		tone = str(tone_parsed[0])
		text = str(tone_parsed[1])
		if text.strip_edges() == "":
			return
		# 防复读按「去掉 {行为} 后的正文」判断：只换动作、正文一样的两条也算复读
		var action_parsed := _strip_actions(text)
		var norm := str(action_parsed[1]).strip_edges()
		var acts: Array = action_parsed[0]
		if norm == "" and acts.is_empty():
			return
		if norm != "" and kind != "ai_move" and _recent_ai_texts.has(norm):
			print("[chat] 防复读（最近 %d 条内一字不差）已抑制：%s" % [_recent_ai_texts.size(), norm])
			# 兜底判据：后端已去重，这里只兜 WS 重连后乱序到达的重复句（含隔了几条的原样复读）。
			# 占位（tomb）是用来「吃掉它自己那条 speech」的——**无语音的台词不许留占位**，
			# 否则它会吃掉**下一条**真语音（修复轮 1 / C1）。
			if wants_speech:
				_register_tomb(speak_kind)
			return
		if norm != "":
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
	bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
	row.add_child(bubble)
	var inner := VBoxContainer.new()
	inner.add_theme_constant_override("separation", 2)
	bubble.add_child(inner)
	var who := Label.new()
	who.text = "Azure"
	who.add_theme_font_size_override("font_size", 11)
	who.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	inner.add_child(who)
	var lbl := Label.new()
	lbl.text = ""
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color", GufengTheme.INK)
	inner.add_child(lbl)
	_chat_box.add_child(row)
	_stream_label = lbl
	_scroll_to_bottom()

## 剥离回复开头的 [[tone:xxx]] 语气标记 → [tone, 干净文本]；未知标记照删但不播音（防标记泄漏给玩家）
static func _strip_tone(text: String) -> Array:
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

## 剥离回复里的 {行为} 标签 → [动作数组, 干净文本]。未知标签照样删（防花括号漏进气泡）。
static func _strip_actions(text: String) -> Array:
	if _action_re.get_pattern() == "":
		_action_re.compile("\\{\\s*([^{}]{1,16})\\s*\\}")
	var acts: Array = []
	for m in _action_re.search_all(text):
		acts.append(m.get_string(1).strip_edges())
	return [acts, _action_re.sub(text, "", true).strip_edges()]

## 标签名 → 化身动作键；与后端 `actions.ALLOWED` 一一对应。未知动作返回空串（忽略）。
static func action_key(name: String) -> String:
	match name.strip_edges().to_lower():
		"笑", "微笑", "轻笑", "smile":
			return "smile"
		"大笑", "笑出声", "laugh":
			return "laugh"
		"得意", "骄傲", "proud":
			return "proud"
		"惊讶", "吃惊", "意外", "surprise":
			return "surprise"
		"担心", "担忧", "worried":
			return "worried"
		"难过", "失落", "sad":
			return "sad"
		"脸红", "害羞", "blush":
			return "blush"
		"思考", "托腮", "think":
			return "think"
	return ""

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

## 轮到这条消息投递时真正执行（原 _add_msg 主体）：文字/语音仍按既有管线走。
## `rec` 是这条台词在 `_add_msg` 里登记的语音配对记录（{} = 这条不由后端朗读/语音关着）
func _deliver_msg(kind: String, text: String, tone: String, rec: Dictionary) -> void:
	var is_ai := kind.begins_with("ai")
	var reveal := kind == "ai_move"          # 这条是「Azure 落子宣告」：轮到她这段语音时再落子
	if is_ai:
		# 只记「上一句」的语义，真正插语气词在下一个思考节点（`_show_thinking`）做
		_last_ai_tone = tone
		_last_ai_acts = _strip_actions(text)[0]
		_last_ai_semantic_ms = Time.get_ticks_msec()
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
	# 全局形势卡：把「落子宣告」之后那句（第三句）同步到卡片；与聊天共用同一份文本。
	if kind.begins_with("ai"):
		if kind == "ai" and _last_ai_kind == "ai_move" and _card_global != null:
			var clean := str(_strip_actions(text)[1]).strip_edges()
			if clean != "":
				_card_global.text = clean
		_last_ai_kind = kind
	# {行为} 标签只在**文字真正出现**的这一刻演（有语音时这里正是语音开播 / 配对的显示点）
	if kind.begins_with("ai"):
		var parsed := _strip_actions(text)
		text = str(parsed[1]).strip_edges()
		var acts: Array = parsed[0]
		if not acts.is_empty():
			_play_actions(acts)
		if text == "":
			return
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
		bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_user())
		row.alignment = BoxContainer.ALIGNMENT_END
	elif kind == "system":
		bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_sys())
		row.alignment = BoxContainer.ALIGNMENT_BEGIN
	else:
		bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
		row.alignment = BoxContainer.ALIGNMENT_BEGIN
	row.add_child(bubble)

	var inner := VBoxContainer.new()
	inner.name = "ChatInner"
	inner.add_theme_constant_override("separation", _chat_line_px)
	bubble.add_child(inner)
	if kind == "ai":
		var who := Label.new()
		who.name = "ChatWho"
		who.text = "Azure"
		who.add_theme_font_size_override("font_size", 11)
		who.add_theme_color_override("font_color", GufengTheme.CINNABAR)
		inner.add_child(who)
	var lbl := Label.new()
	lbl.name = "ChatText"
	lbl.text = text if kind != "ai" else ""      # AI 消息先空着：交给 _start_typing 按节奏逐字显示
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_font_size_override("font_size", _chat_font_px)
	lbl.add_theme_color_override("font_color", GufengTheme.INK)   # 三种气泡都是纸底：一律墨字
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

## 思考短句池：只在第一次显示占位时读一次；文件缺失 ⇒ 空表，走固定占位回退。
func _ensure_thinking_lines() -> void:
	if not _think_lines.is_empty():
		return
	var f := FileAccess.open("res://data/thinking_lines.json", FileAccess.READ)
	if f == null:
		return
	var text := f.get_as_text()
	f.close()
	_think_lines = parse_thinking_lines(text)
	_think_groups = parse_thinking_moods(text, _think_lines.size())

## 当前该用哪组短句：情绪区域优先；情绪中立时看白方胜率（局面）。
func _thinking_pool() -> PackedInt32Array:
	var key := thinking_group_for_mood(_mood_name)
	if key == "" and _last_azure_wr >= 0.0:
		key = "negative" if _last_azure_wr < 0.42 else ("confident" if _last_azure_wr > 0.58 else "calm")
	if key == "":
		key = "calm"
	var pool := PackedInt32Array()
	var raw = _think_groups.get(key, null)
	if typeof(raw) == TYPE_ARRAY:
		for v in raw:
			var i := int(v)
			if i >= 0 and i < _think_lines.size() and not pool.has(i):
				pool.append(i)
	if pool.is_empty():                              # 分组缺失：退回全表
		for i in _think_lines.size():
			pool.append(i)
	return pool

func _pick_thinking_index() -> int:
	var idx := pick_thinking_index(_thinking_pool(), _think_prev, randf())
	if idx >= 0:
		_think_prev = idx
	return idx

## 显示「Azure 思考中……」占位气泡（等第一条真实 Azure 文字到来时原地替换）
func _show_thinking() -> void:
	if _thinking_label != null or _chat_box == null:
		return
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var bubble := PanelContainer.new()
	bubble.add_theme_stylebox_override("panel", GufengTheme.bubble_ai())
	row.add_child(bubble)
	var inner := VBoxContainer.new()
	inner.add_theme_constant_override("separation", 2)
	bubble.add_child(inner)
	var who := Label.new()
	who.text = "Azure"
	who.add_theme_font_size_override("font_size", 11)
	who.add_theme_color_override("font_color", GufengTheme.CINNABAR)
	inner.add_child(who)
	var lbl := Label.new()
	_ensure_thinking_lines()
	_think_dynamic = not _think_lines.is_empty()
	var think_idx := _pick_thinking_index() if _think_dynamic else -1
	lbl.text = _think_lines[think_idx] if think_idx >= 0 else THINKING_FALLBACK
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.custom_minimum_size = Vector2(292, 0)
	lbl.add_theme_color_override("font_color", GufengTheme.INK)
	inner.add_child(lbl)
	_chat_box.add_child(row)
	_thinking_row = row
	_thinking_label = lbl
	_think_last_ms = Time.get_ticks_msec()              # 省略号计时从这一句开始
	_think_line_ms = _think_last_ms
	_scroll_to_bottom()
	_filler_nodes += 1
	_maybe_semantic_filler()

func _clear_thinking() -> void:
	if _thinking_row != null and is_instance_valid(_thinking_row):
		_thinking_row.queue_free()
	_thinking_row = null
	_thinking_label = null

## 句间合成空隙也要有「思考中…」：下一句正在合成、还没开始播时显示占位。
## 有短句池时按心情/局面轮播（约 2.6 秒一句，连续不重复）；数据缺失才退回固定省略号动效。
func _update_thinking(now: int) -> void:
	if _thinking_label == null and pairing.has_pending() and not _tts_playing:
		_show_thinking()                                 # 文字还没轮到显示、也没在播：她在憋下一句
	if _thinking_label == null:
		return
	if _think_dynamic:
		if now - _think_line_ms >= 2600:
			var idx := _pick_thinking_index()
			if idx >= 0:
				_thinking_label.text = _think_lines[idx]
				_think_line_ms = now
		return
	if now - _think_last_ms >= 450:
		_think_last_ms = now
		_think_dots = _think_dots % 3 + 1
		_thinking_label.text = THINKING_FALLBACK + "…".repeat(_think_dots)

## 占位气泡只在「还等着、也没在说话」时清掉，避免把马上要被替换的占位提前删掉
func _clear_thinking_if_idle() -> void:
	if _thinking_label == null:
		return
	if pairing.has_pending() or _audio_in_flight() or (agent != null and agent.busy):
		return
	_clear_thinking()

## 棋盘锁 + 复盘冻结：Azure 思考中，或**正在只读复盘**；她「还有话没播完」不再锁棋盘（v3.11.1：
## 后端这一轮一结束、她那一手一揭示，玩家就能边听边下），只锁四个功能按钮。
## 冻结范围（棋盘只读 / 四个按钮 / 聊天）在静态 `review_lock_state` 里一处定死，
## 这里只把它**逐项应用**到控件上——退出复盘时同样走这里，所以「逐项恢复」不需要另一套逻辑（V20/V21）。
func _refresh_lock() -> void:
	var s := review_lock_state(_reviewing, agent != null and agent.busy, _speech_hold, _move_pending,
			pairing.pending_reveal(), _title_open, _fr_active, _game_ended, agent == null)
	if board != null:
		board.locked = bool(s["locked"])
		board.set_readonly(bool(s["readonly"]))   # 复盘只读（落子与摸头都禁；她思考中只禁落子）
	for b in _buttons:
		b.disabled = bool(s["buttons_disabled"])
	if _btn_reset != null:
		_btn_reset.disabled = bool(s["reset_disabled"])
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

## 界面的「冻结状态」：进复盘 / 她思考中 / 她还有话没播完 / 还有没揭示的落子，各自的冻结范围。
## 纯函数、不碰场景树 ⇒ 门卫可直接断言「进入复盘 ⇒ 冻结；退出 ⇒ 逐项回到进入前」（V11/V21）。
##
## **棋盘解锁点（v3.11.1）**：`locked` 只看「她这一轮还在跑（busy）/ 点下就锁（move_pending）/
## 还有没揭示的落子（reveal_pending）」——**不再看她还在不在说话**。
## 后端 `busy=false` 就已经空闲、可以接下一手；原来把 `speech_hold` 也算进 `locked`，玩家必须
## 把整段语音听完才能落子（「说话过程中不能下棋」）。现在语音继续播，棋盘已经放开。
## `speech_hold` 仍然留着，但只用于**四个功能按钮**（提示 / 局面分析 / 重来 / 结束）与进复盘的闸门：
## 一边听一边下棋是自然的，一边听一边再叠一个「局面分析」就不必了。
## `menu_open`（v3.11.2）＝主界面纸幕开着：棋盘、按钮、聊天与「别的改动入口」一起冻住。
## `game_ended`（v3.12.2）＝「结束」流程已经走完：提示 / 局面分析 / 结束三个按钮从此置灰
## （局面已经定案，不该再要提示、再点一次结束）；**重来不灰**——那是玩家开新局的出口。
static func review_lock_state(reviewing: bool, busy: bool, speech_hold: bool,
		move_pending := false, reveal_pending := false, menu_open := false,
		full_review := false, game_ended := false, client_missing := false) -> Dictionary:
	# `client_missing`＝NetClient 还没装配（例如选「等待 CosyVoice」的启动等待期）：
	# 棋盘 / 按钮 / 聊天一起锁住，避免点进 `agent.player_move` 打到 Nil。
	var locked := busy or move_pending or reveal_pending or menu_open or full_review or client_missing
	var base_disabled := locked or speech_hold or reviewing or full_review
	return {
		"readonly": reviewing or full_review,          # 棋盘只读：复盘 / 全盘复盘
		"locked": locked,                       # 她思考中 / 点下就锁 / 她的落子还没揭示 / 主界面开着
		"buttons_disabled": base_disabled or game_ended,   # 提示 / 局面分析 / 结束（重来见下一项）
		"reset_disabled": base_disabled,        # 复盘 / 全盘复盘 / 忙碌 / 语音未完：重来也冻
		"chat_editable": not reviewing and not menu_open and not full_review and not client_missing,    # 聊天禁用
		# 别的会改动**真实棋局**的入口（I1）：`⏪ 俗手回档` 按钮（`_bad_box`，每次 state 事件都会重建）
		# 与两个开关（允许虚着 → POST /api/config、定式导学）。后端刻意不门禁 /api/undo 与 /api/config，
		# 所以只能靠前端冻住——但**只有复盘**冻它们：她忙碌时回档 / 改配置是既有且被支持的玩法，不许回退。
		# `client_missing` 时同样冻：这些入口都会调 `agent`，不能让它打到 Nil。
		"extra_disabled": reviewing or menu_open or full_review or client_missing,
	}

## 能不能进复盘（I2）：正在跑回合（如「结束」的总结还在途中）或她还有语音没播完时**先别进**。
## 后端 `set_review` 允许 busy 进入、且**不取消**在途回合 ⇒ 那段台词 + 语音 + 气泡会落到复盘期间，
## 破掉「回看全程不出声」。纯函数、门卫可直接断言。
static func review_can_enter(busy: bool, speech_hold: bool) -> bool:
	return not (busy or speech_hold)

## 全盘复盘的分段计划：与后端 `full_review.plan_segments` **同一条公式**（门卫两边都钉）。
## 尽量 8 段以内、每段至少 12 手、最长 48 手；返回 `[[from, to), ...]`（0 基、覆盖 0..total）。
static func full_review_segments(total: int) -> Array:
	var n := maxi(total, 0)
	if n == 0:
		return []
	var count := int(ceil(float(n) / float(FULL_REVIEW_MIN_SPAN)))
	count = clampi(count, 1, FULL_REVIEW_MAX_SEGMENTS)
	count = maxi(count, int(ceil(float(n) / float(FULL_REVIEW_MAX_SPAN))))
	var base := int(floor(float(n) / float(count)))
	var extra := n - base * count
	var out: Array = []
	var start := 0
	for i in count:
		var span := base + (1 if i < extra else 0)
		out.append([start, start + span])
		start += span
	return out

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

func _scroll_to_bottom(force := false) -> void:
	await get_tree().process_frame
	if _chat_scroll == null:
		return
	if force or _chat_follow:
		_chat_scroll.scroll_vertical = 1 << 20
		_chat_follow = true
		if _btn_new_msg != null:
			_btn_new_msg.visible = false
	elif _btn_new_msg != null:
		_btn_new_msg.visible = true

## 聊天滚动：在底部就跟随新消息；翻上去后新消息只亮提示，不把玩家拽回底部。
func _on_chat_scrolled(_v: float) -> void:
	if _chat_scroll == null:
		return
	var bar := _chat_scroll.get_v_scroll_bar()
	_chat_follow = bar.value >= bar.max_value - bar.page - 8.0
	if _chat_follow and _btn_new_msg != null:
		_btn_new_msg.visible = false

func _on_chat_font_selected(i: int) -> void:
	_chat_font_px = [13, 15, 17][clampi(i, 0, 2)]
	_apply_chat_style()

func _on_chat_gap_selected(i: int) -> void:
	_chat_line_px = [2, 6, 10][clampi(i, 0, 2)]
	_apply_chat_style()

## 把字号 / 行距应用到已经显示的气泡（新气泡在建的时候直接取当前值）。
func _apply_chat_style() -> void:
	if _chat_box == null:
		return
	_chat_box.add_theme_constant_override("separation", maxi(4, _chat_line_px))
	for row in _chat_box.get_children():
		for bubble in row.get_children():
			for inner in bubble.get_children():
				if inner is VBoxContainer:
					inner.add_theme_constant_override("separation", _chat_line_px)
					var lbl := inner.get_node_or_null("ChatText") as Label
					if lbl != null:
						lbl.add_theme_font_size_override("font_size", _chat_font_px)

func _render_bad_moves(list: Array) -> void:
	for c in _bad_box.get_children():
		c.queue_free()
	if list.is_empty():
		var l := Label.new()
		l.text = "暂无俗手记录"
		l.add_theme_font_size_override("font_size", 12)
		l.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
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
			_sfx_ready = false               # 悔棋：下一份 state 只对齐，不把回退当提子
			if board != null:
				board.clear_ownership()       # 旧领地图对应的是被悔掉的那一手，先清掉
			agent.undo(idx))
		_bad_box.add_child(btn)
	# 复盘期间重建出来的按钮**必须是灰的**（I1）：每次 `state` 事件（含复盘翻页的只读快照）都会走到这里
	_refresh_lock()

func _set_status(text: String, bad := false) -> void:
	_status_bad = bad
	_lbl_status.text = text
	_lbl_status.add_theme_color_override("font_color",
		GufengTheme.CINNABAR if bad else GufengTheme.INK_SOFT)

## 简易模态输入框（用于「重来」确认与总结备注）
func _ask_note(title_text: String, on_ok: Callable) -> void:
	var back := ColorRect.new()
	back.name = "ModalVeil"                       # 供 `_modal_open()` 识别（Esc 不抢键盘）
	back.color = Color(0.09, 0.08, 0.06, 0.45)
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
	box.add_theme_stylebox_override("panel", GufengTheme.panel(14))
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

# ================= 主界面（纸幕封面 + 运镜，v3.11.3） =================
## 远看取景：离棋盘远、略偏一个角度（入场时一边飞近、一边把棋盘转正）
const CAM_MENU_YAW := 10.0
const CAM_MENU_PITCH := 36.0
const CAM_MENU_DIST := 64.0
## 运镜节奏（秒）：淡出 → 相机飞入 → 工具栏飞入；回主界面时反过来
const TITLE_FADE_OUT := 0.35
const TITLE_FLY_IN := 1.05
const TITLE_PANEL_IN := 0.35
const TITLE_PANEL_OUT := 0.22
const TITLE_FLY_OUT := 0.95
const TITLE_FADE_IN := 0.45

## 建主界面：它是 `_ui_root` 的最后一个孩子，所以压在左右栏 / 定式卡片之上。
func _build_title_screen() -> void:
	title_screen = TitleScreen.new()
	title_screen.theme = _ui_root.theme          # 与棋盘同一套深色 / 无衬线主题
	title_screen.primary_pressed.connect(_on_title_primary)
	title_screen.settings_pressed.connect(_on_title_settings)
	title_screen.library_pressed.connect(_on_title_library)
	title_screen.quit_pressed.connect(func(): get_tree().quit())
	_ui_root.add_child(title_screen)

## 首次启动时显示封面并等「开始对局」。调试模式：
## `--shot=` / `--demo` / `--setup` 默认跳过（截图与自动开局都别被封面挡住）；
## `--title` 强制只把封面画出来（供截图核对），不等待。
func _maybe_show_title() -> void:
	if DisplayServer.get_name() == "headless":
		return                                   # 无窗口：没人能点，等了就死锁
	var shot := false
	var force := false
	for a in OS.get_cmdline_user_args():
		if a == "--title":
			force = true
			_cover_only = true
		elif a.begins_with("--shot=") or a == "--demo" or a == "--setup":
			shot = true
	_shot_requested = shot
	if shot and not force:
		return
	_open_title_screen(false)                    # 首帧直接摆到「远看棋盘 + 封面」，无动画
	if shot:
		return                                   # 截图模式：画出来即可，不能卡在等确认上
	await title_started

## 打开封面。首次启动 `animated=false`（直接摆好）；对局中按 Esc `animated=true`：
## 工具栏先滑出 → 相机飞回远处 → 封面再淡入（`_play_exit_to_menu`）。
func _open_title_screen(animated := false) -> void:
	if title_screen == null or _title_open or _transition:
		return
	_title_open = true
	_hide_estimate()                              # 封面期间不留对局内的临时注记
	if board != null:
		board.input_blocked = true                # 封面期间 Home / C / 拖拽 / 滚轮都不许动视角
	title_screen.set_restart_mode(_game_started)  # 首次「开始对局」/ 对局中回来「开始新对局」
	_idle = false
	_set_idle_state(false)
	_refresh_lock()
	_set_status("主界面：开始 / 设置 / 退出（对局中按 Esc 可回到这里）")
	if not animated:
		title_screen.set_alpha(1.0)
		_enter_menu_view_immediate()
		title_screen.open()
		return
	title_screen.close()                          # 先藏起来：等工具栏退场、相机飞远，再淡入
	title_screen.set_alpha(0.0)
	_play_exit_to_menu()

func _close_title_screen() -> void:
	if not _title_open:
		return
	_title_open = false
	if board != null:
		board.input_blocked = false
	if avatar != null:
		avatar.visible = true                    # 封面收起：化身回到棋盘对面
	if title_screen != null:
		title_screen.close()
	_refresh_lock()
	_show_global_card()                           # 从封面回来：形势卡带着上一局的最后数据回来
	_bump_activity()                              # 从菜单回来不算「闲置」

## 主界面首帧：两侧栏与贴边开关都收起来，相机直接摆到「远看 + 正对棋盘」取景
## （`snap_panels(false,false)`：没有侧栏就别留出面板的位置，否则棋盘会偏在一边）。
func _enter_menu_view_immediate() -> void:
	_left_open = false
	_right_open = false
	_apply_panel_layout(false, 0.0, false)
	_right_toggle.visible = false
	_left_toggle.visible = false
	if avatar != null:
		avatar.visible = false                   # 封面只透棋盘，不让化身在纸签后面留一团影子
	if _joseki_card != null:
		_joseki_card.visible = false
	if board != null:
		board.snap_panels(false, false)           # 主界面没有侧栏：棋盘正对镜头
		board.set_view(CAM_MENU_YAW, CAM_MENU_PITCH, CAM_MENU_DIST)

## 工具栏（右侧面板 + 两侧贴边开关）淡出：回主界面那一段
func _hide_game_chrome(dur: float) -> void:
	_left_open = false
	_right_open = false
	_apply_panel_layout(true, dur, false)         # 只滑出面板，不动棋盘取景（同上，避免跳一下）
	_right_toggle.visible = false
	_left_toggle.visible = false
	if _joseki_card != null:
		_joseki_card.visible = false
	if board != null:
		board.set_panels(false, false)            # 平滑回中间：棋盘跟着工具栏一起让位

## 工具栏飞入：开始 / 开始新对局那一段（右侧面板从画面外滑进来，棋盘同时让位）
func _show_game_chrome(dur: float) -> void:
	_left_open = false
	_right_open = true
	_apply_panel_layout(true, dur)
	_right_toggle.visible = true
	_left_toggle.visible = true

## 入场：封面淡出 → 相机从远处缓动到棋盘正面 → 右侧工具栏飞入。
## 全程 `_title_open` 仍为真（棋盘锁着）；调用方在 `await` 结束后才解锁。
func _play_enter_game() -> void:
	_transition = true
	_refresh_lock()
	if avatar != null:
		avatar.visible = true                    # 先回到纸幕后：封面淡出时她已经在对局位
	if title_screen != null:
		await title_screen.fade_out(TITLE_FADE_OUT)
		title_screen.close()
	if board != null:
		board.fly_home(TITLE_FLY_IN)              # 「正对棋盘」＝回到捕获的初始取景
	await get_tree().create_timer(TITLE_FLY_IN).timeout
	_show_game_chrome(TITLE_PANEL_IN)
	await get_tree().create_timer(TITLE_PANEL_IN).timeout
	_transition = false

## 退场（反向）：工具栏先滑出 → 相机飞回远处 → 封面再淡入
func _play_exit_to_menu() -> void:
	_transition = true
	_hide_game_chrome(TITLE_PANEL_OUT)
	await get_tree().create_timer(TITLE_PANEL_OUT).timeout
	if board != null:
		board.fly_to(CAM_MENU_YAW, CAM_MENU_PITCH, CAM_MENU_DIST, TITLE_FLY_OUT)
	await get_tree().create_timer(TITLE_FLY_OUT).timeout
	if avatar != null:
		avatar.visible = false                   # 纸签浮上来前先收起化身：别在标题后面留影子
	if title_screen != null:
		title_screen.open()
		await title_screen.fade_in(TITLE_FADE_IN)
	_transition = false

## 开始 / 开始新对局：首次开局先过设置面板（盖在封面上），再走入场运镜。
## **每次从主界面进对局都是新的一局**（v3.12.1：不再有「继续上一局」）——
## 进门前的清屏放在运镜之前，运镜期间看到的就是空棋盘。
func _on_title_primary() -> void:
	if _transition or _setup_showing:
		return                                   # 运镜中 / 设置面板已开：不重入
	var first := not _game_started
	if first and not _setup_done:
		_transition = true                       # 面板开着也算「切换中」：Esc 与重复点击都不响应
		_build_setup_panel()
		await setup_confirmed
		_setup_done = true
	if entry_needs_restart(_game_started):
		_begin_new_game_on_entry()
	await _play_enter_game()
	_close_title_screen()
	if first:
		_game_started = true
		title_started.emit()                      # 释放 _ready 的等待，继续起服务 / 问候

## 从主界面进对局要不要重开一局：首次启动不用（后端本来就是空局、服务还没起），
## 其余每一次都要——「每次从主界面进入游戏刷新棋局，不遗留上一局」。纯函数，门卫可直接断言。
static func entry_needs_restart(game_started) -> bool:
	match typeof(game_started):
		TYPE_BOOL:
			return game_started
		TYPE_INT, TYPE_FLOAT:
			return game_started != 0
		_:
			return false

## 从主界面进入＝新的一局：先在本地清屏（运镜期间就看不到旧棋子），再让后端 reset。
## 后端 `/api/reset` 忙时会拒绝（保护在途回合），所以忙就先挂着，等 `_on_busy_changed(false)` 补发。
## 不这样做的话，「她还在想的时候按 Esc 回主界面再进来」会静默保留上一局。
func _begin_new_game_on_entry() -> void:
	_reset_game_view()
	_restart_when_idle = true
	_try_restart_backend()

## 首次进对局也保证是空局：后端是独立进程，可能跨游戏运行（上一次对局还在里面）——
## 不 reset 的话 `hello` 会把上一局的 stones 推回来。走与「从主界面进来」同一条路（静默 + 忙时等 idle）。
func _start_launch_game() -> void:
	if _shot_requested:                          # 截图 / 演示模式只清本地，不误重置真实后端棋局
		_reset_game_view()
		_restart_when_idle = false
		return
	if agent == null:
		return
	_begin_new_game_on_entry()

func _try_restart_backend() -> void:
	if not _restart_when_idle or agent == null:
		return
	if agent.busy:
		return                                   # 等这一轮收尾（`_on_busy_changed(false)` 会再进来）
	if not agent.reset_game(true):                # 静默新局：进门不该每次都多一句「重新来吧」
		_restart_retry_ms = Time.get_ticks_msec() + 1500
		return                                   # 请求没发出去：保留标志，_process 里重试
	_restart_when_idle = false

## 新的一局的侧栏文案（纯函数：从主界面进入 / 「重来」共用，门卫可直接断言）。
## `state` 事件只带棋盘与俗手，**胜率 / 目差不会自己归零**——必须在这里显式清，否则上一局会留着。
static func fresh_game_labels() -> Dictionary:
	return {"moves": "手数: 0", "winrate": "黑胜率: --", "score": score_line(null), "hint": ""}

## 把界面恢复到空局：棋盘 / 手数 / 胜率 / 目差 / 俗手 / 复盘入口 / 领地图 / 点目注记一起清。
## v3.12.2：复盘计数与「回看中」标志也在这里归零——从主界面进来时，上一局的复盘态不许漏进新局。
func _reset_game_view() -> void:
	_hide_estimate()
	_reset_global_card()
	_show_global_card()                           # 新局：卡片带着占位文案回来，第一份 info 再填数
	_release_move_pending()
	_last_move_count = 0
	_last_stone_count = 0
	_last_move_seq = []
	_last_wr = 0.5
	_last_score = 0.0
	_info_ready = false
	_sfx_ready = false                       # 下一份空盘 state 只对齐，不把收子声当提子
	_reviewing = false
	_review_total = 0
	_review_cur = 0
	if _btn_prev != null:
		_btn_prev.visible = false
	if _btn_next != null:
		_btn_next.visible = false
	if _btn_review != null:
		_btn_review.text = "复盘"
	if _btn_home != null:
		_btn_home.visible = true
	var lab := fresh_game_labels()
	_lbl_moves.text = str(lab["moves"])
	_lbl_wr.text = str(lab["winrate"])
	_lbl_score.text = str(lab["score"])
	if _lbl_score_hint != null:
		_lbl_score_hint.text = str(lab["hint"])
	_render_bad_moves([])
	if _joseki_card != null:
		_joseki_card.clear()
	_game_ended = false
	_refresh_review_entry()
	if board != null:
		board.clear_ownership()
		board.set_stones([])
		board.set_move_seq([])
	_refresh_lock()                          # 对局结束的置灰 / 复盘冻结一起回到新局口径

## 主界面的「设置」：复用启动设置面板（只换主按钮措辞），改完回主界面、不掉出对局。
func _on_title_settings() -> void:
	if _setup_showing or _transition:
		return
	_build_setup_panel("保存设置", "改完点「保存设置」回到主界面；改过的值会写回 config.json。", "返回")

# ================= 棋谱库（v3.11.5） =================
## 只在主界面打开：全屏模态纸签。网络 / 文件选择都在这里，`GameLibrary` 只做列表与本地回放；
## 打开期间封面仍在（棋盘锁着），不会误落子。所有读 / 导入 / 导出 / 删除都不碰当前对局。

func _on_title_library() -> void:
	if _transition or _setup_showing:
		return
	if game_library != null:
		return
	if agent == null:
		# 首次启动时服务还没装配：先走一遍「出发前的准备」，再用同一份配置起客户端。
		# 面板没有取消键（与首次开局一致），所以这里 await 不会被晾住。
		if not _setup_done:
			_transition = true
			_build_setup_panel("保存并进入棋谱库",
				"以下默认就是上次的配置，直接点「保存并进入棋谱库」即可；改过的值会写回 config.json。", "")
			await setup_confirmed
			_transition = false
		if agent == null:
			_build_services()
	if agent == null:
		_set_status("后端未连接：棋谱库需要后端", true)
		return
	game_library = GameLibrary.new()
	game_library.closed.connect(func(): game_library = null)
	game_library.refresh_requested.connect(_games_refresh)
	game_library.import_requested.connect(_games_import)
	game_library.game_selected.connect(_games_open)
	game_library.export_requested.connect(_games_export)
	game_library.delete_requested.connect(_games_delete)
	game_library.game_review_requested.connect(_on_game_review)
	_ui_root.add_child(game_library)
	game_library.set_status("正在读取棋谱库…")
	_games_refresh()

## 棋谱库的错误文案：HTTP 0 通常是后端没起（比裸「HTTP 0」可读）
func _games_error_text(code: int, body: PackedByteArray) -> String:
	if code == 0:
		return "连不上后端（先启动 backend\\run_server.bat）"
	return _api_error_text(code, body)

## 列表：`select_id > 0` 时刷新后直接选中它（导入 / 删除后用）；否则保持当前选择或选第一盘。
func _games_refresh(select_id := 0) -> void:
	if game_library == null or agent == null:
		return
	_games_list_seq += 1
	var seq := _games_list_seq
	var lib := game_library
	agent.fetch_bytes("/api/games", HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if not is_instance_valid(lib) or game_library != lib or seq != _games_list_seq:
				return                              # 库实例已被释放 / 旧回包 / 已经换过一个库实例：直接丢弃
			if not ok:
				game_library.set_status("棋谱库读取失败：" + _games_error_text(code, body), true)
				return
			var d := _json_dict(body)
			var list = d.get("games", [])
			if typeof(list) != TYPE_ARRAY:
				list = []
			game_library.set_games(list)
			game_library.set_status("共 %d 盘棋谱" % (list as Array).size())
			var target := int(select_id)
			if target <= 0:
				target = game_library.selected_id()
				if target > 0 and game_library.has_selection():
					target = 0                      # 纯刷新：保留玩家正在看的回看位置，不重载整盘
			if target <= 0 and not (list as Array).is_empty():
				target = int(((list as Array)[0] as Dictionary).get("id", 0))
			if target > 0:
				_games_open(target))

func _games_open(game_id: int) -> void:
	if game_library == null or agent == null or game_id <= 0:
		return
	_games_open_seq += 1
	var seq := _games_open_seq
	var lib := game_library
	agent.fetch_bytes("/api/games/%d" % game_id, HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if not is_instance_valid(lib) or game_library != lib or seq != _games_open_seq:
				return                              # 库实例已释放 / 又点了别的棋谱 / 库已换实例：旧回包作废
			if not ok:
				game_library.set_status("读到第 %d 盘失败：%s" % [game_id, _games_error_text(code, body)], true)
				return
			var d := _json_dict(body)
			var rec = d.get("game", {})
			if typeof(rec) != TYPE_DICTIONARY or (rec as Dictionary).is_empty():
				game_library.set_status("这盘棋谱是空的", true)
				return
			game_library.set_selected(rec)
			game_library.set_status("已载入第 %d 盘" % game_id)
			# 她的回忆（纯本地短文案，零 LLM）：旧回包 / 已换库实例直接丢弃。
			agent.fetch_bytes("/api/games/%d/remark" % game_id, HTTPClient.METHOD_GET, {},
				func(ok2: bool, code2: int, body2: PackedByteArray):
					if not is_instance_valid(lib) or game_library != lib or seq != _games_open_seq:
						return
					if not ok2:
						return
					var d2 := _json_dict(body2)
					game_library.set_remark(str(d2.get("text", "")))))

func _games_import() -> void:
	if game_library == null or agent == null:
		return
	var fd := FileDialog.new()
	fd.title = "导入 SGF 棋谱"
	fd.access = FileDialog.ACCESS_FILESYSTEM
	fd.file_mode = FileDialog.FILE_MODE_OPEN_FILE
	fd.filters = PackedStringArray(["*.sgf ; SGF 棋谱"])
	fd.use_native_dialog = true
	fd.file_selected.connect(func(path: String):
		fd.queue_free()
		if game_library == null:
			return
		var f := FileAccess.open(path, FileAccess.READ)
		if f == null:
			game_library.set_status("文件打不开：" + path, true)
			return
		var text := f.get_as_text()
		f.close()
		if text.strip_edges() == "":
			game_library.set_status("文件是空的", true)
			return
		game_library.set_status("正在导入…")
		agent.fetch_bytes("/api/games/import", HTTPClient.METHOD_POST, {"sgf": text},
			func(ok: bool, code: int, body: PackedByteArray):
				if game_library == null:
					return
				if not ok:
					game_library.set_status("导入失败：" + _games_error_text(code, body), true)
					return
				var d := _json_dict(body)
				var gid := int(d.get("id", 0))
				game_library.set_status("已导入第 %d 盘" % gid)
				_games_refresh(gid)))
	fd.canceled.connect(func(): fd.queue_free())
	add_child(fd)
	fd.popup_centered_ratio(0.65)

func _games_export(game_id: int) -> void:
	if game_library == null or agent == null or game_id <= 0:
		return
	game_library.set_status("正在导出 SGF…")
	agent.fetch_bytes("/api/games/%d/sgf" % game_id, HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if game_library == null:
				return
			if not ok:
				game_library.set_status("导出失败：" + _games_error_text(code, body), true)
				return
			_save_sgf_dialog(game_id, body.get_string_from_utf8()))

func _save_sgf_dialog(game_id: int, text: String) -> void:
	var fd := FileDialog.new()
	fd.title = "导出 SGF 棋谱"
	fd.access = FileDialog.ACCESS_FILESYSTEM
	fd.file_mode = FileDialog.FILE_MODE_SAVE_FILE
	fd.filters = PackedStringArray(["*.sgf ; SGF 棋谱"])
	fd.current_file = "azure_game_%d.sgf" % game_id
	fd.use_native_dialog = true
	fd.file_selected.connect(func(path: String):
		fd.queue_free()
		if game_library == null:
			return
		if not path.to_lower().ends_with(".sgf"):
			path += ".sgf"
		var f := FileAccess.open(path, FileAccess.WRITE)
		if f == null:
			game_library.set_status("写不进这个位置：" + path, true)
			return
		f.store_string(text)
		f.close()
		game_library.set_status("已导出：" + path))
	fd.canceled.connect(func(): fd.queue_free())
	add_child(fd)
	fd.popup_centered_ratio(0.65)

func _games_delete(game_id: int) -> void:
	if game_library == null or agent == null or game_id <= 0:
		return
	agent.fetch_bytes("/api/games/%d" % game_id, HTTPClient.METHOD_DELETE, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if game_library == null:
				return
			if not ok:
				game_library.set_status("删除失败：" + _games_error_text(code, body), true)
				return
			game_library.set_status("已删除第 %d 盘" % game_id)
			_games_refresh())

# ================= 全盘 AI 复盘（v3.11.5） =================
## 棋谱库选一盘 → 入场 → 棋盘本地回放；后端按段分析并让 Azure 用强思考点评。
## 棋子始终由 `GameReplay` 在本地铺（不经过 WS state），因此**不会碰当前对局**；
## 退出时拉一次 `/api/state` 把棋盘恢复成原来的局面。

func _on_game_review(record: Dictionary) -> void:
	if _fr_active or _transition or agent == null:
		return
	var moves = record.get("moves", [])
	if typeof(moves) != TYPE_ARRAY or (moves as Array).is_empty():
		_set_status("这盘棋谱没有落子，没法复盘", true)
		return
	_fr_token += 1
	var token := _fr_token
	_fr_record = record.duplicate(true)
	_fr_segments = full_review_segments((moves as Array).size())
	_fr_index = 0
	_fr_step = 0
	_fr_paused = false
	_fr_requesting = false
	_fr_msg_seen = false
	_fr_last_text = ""
	# 先置模式再放 title_started：`_ready` 可能同步恢复并 `_greet()`，别让开场白插进复盘
	_fr_active = true
	if avatar != null:
		avatar.set_serious(true)                    # 复盘是讲解场合：收住大表情
	_hide_estimate()
	if _joseki_card != null:
		_joseki_card.clear()                        # 上一局残留的定式卡片不许压在这盘复盘上
	# 从主界面进来：先把封面收掉、飞进对局视野（首次还要释放 _ready 的等待）
	if _title_open:
		var first := not _game_started
		await _play_enter_game()
		_close_title_screen()
		if first:
			_game_started = true
			title_started.emit()
	if token != _fr_token:
		return
	if board != null:
		_fr_saved_own = board.ownership_values()
		_fr_saved_stones = board.visible_stones()
		_fr_saved_seq = _last_move_seq.duplicate()
		board.clear_ownership()
		board.set_move_seq(_fr_move_seq())
	_apply_full_review_board()
	_refresh_review_entry()
	_refresh_lock()
	_set_status("全盘复盘：共 %d 段，准备第 1 段…" % _fr_segments.size())
	_fr_next_segment(token)

func _fr_move_seq() -> Array:
	var out: Array = []
	var moves = _fr_record.get("moves", [])
	if typeof(moves) != TYPE_ARRAY:
		return out
	for m in moves:
		if typeof(m) == TYPE_ARRAY and (m as Array).size() >= 2:
			out.append(str((m as Array)[1]))
	return out

func _fr_total_moves() -> int:
	var moves = _fr_record.get("moves", [])
	return (moves as Array).size() if typeof(moves) == TYPE_ARRAY else 0

func _apply_full_review_board() -> void:
	if board == null:
		return
	board.set_stones(GameReplay.stones_at(_fr_record.get("moves", []), _fr_step))

func _fr_step_by(delta: int) -> void:
	if not _fr_active:
		return
	_fr_step = clampi(_fr_step + delta, 0, _fr_total_moves())
	_apply_full_review_board()
	_set_status("全盘复盘 · 手动回看第 %d / %d 手" % [_fr_step, _fr_total_moves()])

func _on_fr_toggle() -> void:
	if not _fr_active:
		return
	_fr_paused = not _fr_paused
	_fr_toggle.text = "继续" if _fr_paused else "暂停"
	if _fr_paused:
		_set_status("全盘复盘已暂停（第 %d / %d 手）；点「继续」接着解说" % [_fr_step, _fr_total_moves()])
		return
	_set_status("全盘复盘继续…")
	if not _fr_requesting:
		_fr_next_segment(_fr_token)

## 请求下一段。**上一段的文字/语音没播完不会走到这里**（`_fr_wait_then_next` 在等）。
func _fr_next_segment(token: int) -> void:
	if not _fr_active or token != _fr_token:
		return
	if _fr_paused or _fr_requesting:
		return
	if _fr_index >= _fr_segments.size():
		_fr_finish()
		return
	var seg: Array = _fr_segments[_fr_index]
	var from_no := int(seg[0])
	var to_no := int(seg[1])
	_fr_step = to_no
	_apply_full_review_board()
	var total := _fr_segments.size()
	_set_status("全盘复盘 · 第 %d/%d 段（第 %d-%d 手）· 她正在算…"
			% [_fr_index + 1, total, from_no + 1, to_no])
	_fr_requesting = true
	_fr_msg_seen = false
	var body := {
		"game_id": int(_fr_record.get("id", 0)),
		"from": from_no,
		"to": to_no,
		"index": _fr_index,
		"total": total,
		"previous": _fr_last_text,
	}
	var done := func(ok: bool, code: int, data: PackedByteArray):
		if not _fr_active or token != _fr_token:
			return                                  # 旧回包不许改新一次复盘的「请求在途」状态
		_fr_requesting = false
		if not ok:
			_fr_paused = true
			_fr_toggle.text = "继续"
			_set_status("这一段复盘失败：%s（点「继续」重试）" % _games_error_text(code, data), true)
			return
		var d := _json_dict(data)
		_fr_last_text = str(d.get("text", ""))
		_fr_index += 1
		_set_status("全盘复盘 · 第 %d/%d 段已说完（第 %d-%d 手）"
				% [_fr_index, total, from_no + 1, int(d.get("to", to_no))])
		_fr_wait_then_next(token)
	agent.fetch_bytes("/api/review/full/segment", HTTPClient.METHOD_POST, body,
		done, FULL_REVIEW_TIMEOUT)

## 等这一段的语音 / 打字都结束，再请求下一段（**这就是「分几段防 TTS 忙不过来」**）。
func _fr_wait_then_next(token: int) -> void:
	if not _fr_active or token != _fr_token:
		return
	# HTTP 回包可能先于 WS 台词到达：先给它 2 秒落地，否则会「上一段还没开口就排下一段」。
	var landed := 0
	while landed < 10 and not _fr_msg_seen:
		if not _fr_active or token != _fr_token:
			return
		if tts != null and tts.enabled and tts.is_speaking():
			break                                          # 已经在播：台词必然已到
		await get_tree().create_timer(0.2).timeout
		landed += 1
	var guard := 0
	while guard < 300:                                   # 最多等 60 秒，绝不把复盘卡死
		if not _fr_active or token != _fr_token:
			return
		var speaking := tts != null and tts.enabled and (tts.is_speaking() or _speech_busy())
		var typing := not _typing_full.is_empty()
		var queued := _msg_queue_busy or not _msg_queue.is_empty()
		if not speaking and not typing and not queued:
			break
		await get_tree().create_timer(0.2).timeout
		guard += 1
	if not _fr_active or token != _fr_token or _fr_paused:
		return
	if tts == null or not tts.enabled:
		# 没有语音：按字数留出阅读时间，别让下一段追着上一段
		var wait_sec := clampf(float(_fr_last_text.length()) / 9.0, 1.5, 7.0)
		await get_tree().create_timer(wait_sec).timeout
	else:
		await get_tree().create_timer(0.35).timeout
	if not _fr_active or token != _fr_token or _fr_paused:
		return
	_fr_next_segment(token)

func _fr_finish() -> void:
	if not _fr_active:
		return
	_fr_toggle.text = "完成"
	_fr_toggle.disabled = true
	_set_status("全盘复盘看完了（共 %d 手）。点「退出复盘」回到当前棋盘。" % _fr_total_moves())

## 退出：清掉本地回放，拉一次权威 state 回到当前对局（后端全程没被改过）。
func _exit_full_review() -> void:
	if not _fr_active:
		return
	_fr_active = false
	if avatar != null:
		avatar.set_serious(false)                   # 退出复盘：恢复平时的表情幅度
	_fr_token += 1
	_fr_paused = false
	_fr_requesting = false
	_fr_msg_seen = false
	_fr_record = {}
	_fr_segments = []
	_fr_index = 0
	_fr_step = 0
	_fr_last_text = ""
	if _fr_toggle != null:
		_fr_toggle.text = "暂停"
		_fr_toggle.disabled = false
	_refresh_review_entry()
	_refresh_lock()
	if board != null:
		board.clear_ownership()
		board.set_stones(_fr_saved_stones)           # 先本地还原，别在等 state 时露出复盘棋谱
		board.set_move_seq(_fr_saved_seq)
		if _fr_saved_own.size() == 361 and cfg.ownership_enabled:
			board.set_ownership(_fr_saved_own)
			board.set_ownership_enabled(true)
	_fr_saved_own = []
	_fr_saved_stones = []
	_fr_saved_seq = []
	if agent == null:
		_set_status("已退出全盘复盘")
		return
	agent.fetch_bytes("/api/state", HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if not ok:
				_set_status("退出全盘复盘后同步失败：%s" % _games_error_text(code, body), true)
				return
			var st := _json_dict(body)
			if not st.is_empty():
				_on_state_changed(st)
			_refresh_lock()
			_set_status("已退出全盘复盘，回到当前对局"))

## Esc：对局中回主界面；主界面里再按一次回对局。弹窗开着 / 复盘 / 运镜中都不抢。
## 走 `_input`（与复盘翻页键同一个最早入口）：不然焦点在按钮上时 Esc 会被别处吃掉。
func _menu_keys(event: InputEvent) -> void:
	if not (event is InputEventKey):
		return
	var k := event as InputEventKey
	if not k.pressed or k.echo or k.keycode != KEY_ESCAPE:
		return
	if _transition:
		return                                   # 运镜期间不叠加第二次切换
	if _modal_open():
		return                                   # 设置面板 / 备注框开着：Esc 不抢它们的键盘
	if _fr_active:
		get_viewport().set_input_as_handled()
		_exit_full_review()                      # 全盘复盘：Esc = 退出复盘（回到当前对局）
		return
	if _title_open:
		if _game_started:
			get_viewport().set_input_as_handled()
			_on_title_primary()                  # 反向出场：淡出 → 飞入 → 工具栏飞入
		return
	if not _game_started or _reviewing:
		return
	var focus := get_viewport().gui_get_focus_owner()
	if focus is LineEdit or focus is TextEdit:
		return                                   # 正在输入：Esc 归输入框（别一按就把键盘抢到菜单）
	_open_title_screen(true)
	get_viewport().set_input_as_handled()

## 有没有模态遮罩（设置面板 / 重来与总结的备注框）：它们都叫 `ModalVeil`。
func _modal_open() -> bool:
	if _ui_root == null:
		return false
	for c in _ui_root.get_children():
		if c is Control and c.name == "ModalVeil" and not (c as Control).is_queued_for_deletion():
			return true
	return false

# ================= 思考短句解析工具（当前运行路径不轮播） =================

## JSON → 短句表（**纯函数**，门卫直接调它）：接受 `{"lines": [...]}` 或裸数组；
## 非字符串 / 空串 / 重复句一律丢掉（重复句轮换时看起来就像卡住了）。
static func parse_thinking_lines(text: String) -> PackedStringArray:
	var out := PackedStringArray()
	var j := JSON.new()
	if j.parse(text) != OK:
		return out                               # 坏 JSON：静默回退（`JSON.parse_string` 会往控制台喷错误）
	var d = j.data
	var arr = null
	if typeof(d) == TYPE_DICTIONARY:
		arr = (d as Dictionary).get("lines", null)
	elif typeof(d) == TYPE_ARRAY:
		arr = d
	if typeof(arr) != TYPE_ARRAY:
		return out
	var seen := {}
	for v in arr:
		if typeof(v) != TYPE_STRING:
			continue
		var s := (v as String).strip_edges()
		if s == "" or seen.has(s):
			continue
		seen[s] = true
		out.append(s)
		if out.size() >= 64:                 # 防御：再长也不会把这一句撑爆气泡
			break
	return out

## 下一句的下标（**纯函数**）：随机挑一句、**绝不与上一句相同**（count > 1 时）。
## `rng_value` 由调用方给（`randf()`），测试里可以钉死，不引入全局随机状态。
static func next_thinking_index(prev: int, count: int, rng_value: float) -> int:
	if count <= 0:
		return -1
	if count == 1:
		return 0
	var r := clampf(rng_value, 0.0, 0.999999)
	if prev < 0 or prev >= count:
		return mini(count - 1, int(floor(r * float(count))))
	var n := int(floor(r * float(count - 1)))     # 0..count-2：把「上一句」那个空位先拿掉
	if n >= prev:
		n += 1                                    # 再补回去 ⇒ 一定绕开上一句
	return n

## `moods` 分组 → 下标数组（纯函数）：只收合法下标、去重；坏 JSON / 缺键 / 脏值 → 空字典。
static func parse_thinking_moods(text: String, count: int) -> Dictionary:
	var out := {}
	var j := JSON.new()
	if j.parse(text) != OK:
		return out
	var d = j.data
	if typeof(d) != TYPE_DICTIONARY:
		return out
	var moods = (d as Dictionary).get("moods", null)
	if typeof(moods) != TYPE_DICTIONARY:
		return out
	for key in (moods as Dictionary).keys():
		var arr = moods[key]
		if typeof(arr) != TYPE_ARRAY:
			continue
		var idxs: Array[int] = []
		var seen := {}
		for v in arr:
			if typeof(v) != TYPE_INT and typeof(v) != TYPE_FLOAT:
				continue
			var i := int(v)
			if i < 0 or i >= count or seen.has(i):
				continue
			seen[i] = true
			idxs.append(i)
		if not idxs.is_empty():
			out[str(key)] = idxs
	return out

## 情绪区域 → 短句分组；中立 / 未知名返回空串（由调用方用局面兜底）。
static func thinking_group_for_mood(mood_name: String) -> String:
	var n := mood_name.strip_edges()
	if n in ["落寞", "烦闷", "烦躁", "失落", "难过", "委屈", "忧心", "不安", "惊慌"]:
		return "negative"
	if n in ["惬意", "愉悦", "兴奋", "开心", "欢喜", "欢腾", "期待", "跃跃欲试", "惊喜"]:
		return "confident"
	if n in ["好奇", "兴致", "激动", "跳脱", "专注", "振奋"]:
		return "alert"
	return ""

## 从分组池里挑一句（纯函数）：**一定绕开上一句**；池只有一句时就只能复用它。
static func pick_thinking_index(pool: PackedInt32Array, prev: int, rng_value: float) -> int:
	if pool.is_empty():
		return -1
	var candidates: Array[int] = []
	for i in pool:
		if i != prev:
			candidates.append(i)
	if candidates.is_empty():
		return pool[0]
	var r := clampf(rng_value, 0.0, 0.999999)
	return candidates[int(floor(r * float(candidates.size())))]

# ================= 启动设置面板 =================

## 启动设置：思考模式 + LLM 的 API 配置 + 阿里云语音合成（Key / 音色 ID），默认全部取当前 config.json 的值。
## 确认后写回 config.json，_ready 再继续按新配置起服务。朗读方式（`tts_mode`）不在这里改——
## 那是左侧栏「朗读方式」的下拉，启动面板只负责把阿里云那两项配好。
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
		elif a == "--title":
			_cover_only = true
	if _cover_only and _shot_requested and not force:
		return                                   # 只拍封面时，不要叠启动设置面板
	if shot and not force:
		return
	if _setup_done and not force:
		return                                   # 主界面里已经点过「设置」：开局不再弹第二遍
	_build_setup_panel()
	if shot:
		return                                   # 截图模式：画出来即可，不能卡在等确认上
	await setup_confirmed
	_setup_done = true

## 参数只为「主界面 → 设置」复用：主按钮改成「保存设置」、说明改成保存后回主界面、另给一个「返回」不落盘。
## 首次开局的默认值就是原来的「开始对局 / 出发前的准备」，一个字都没变。
func _build_setup_panel(primary_text := "开始对局",
		hint_text := "以下默认就是上次的配置，直接点「开始对局」即可；改过的值会写回 config.json。",
		cancel_text := "") -> void:
	_setup_showing = true
	var back := ColorRect.new()
	back.name = "ModalVeil"                       # 供 `_modal_open()` 识别（Esc 不抢键盘）
	back.color = Color(0.09, 0.08, 0.06, 0.55)
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
	box.add_theme_stylebox_override("panel", GufengTheme.panel(16))
	back.add_child(box)

	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	# 对话框拆成「滚动内容 + 固定动作区」：内容再长，「开始对局」也不会被滚出可视区
	var outer := VBoxContainer.new()
	outer.add_theme_constant_override("separation", 10)
	box.add_child(outer)
	outer.add_child(scroll)
	var vb := VBoxContainer.new()
	vb.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	vb.add_theme_constant_override("separation", 8)
	scroll.add_child(vb)

	# 品牌图（用户提供）：放在「出发前的准备」上方；图片缺失时静默跳过，不影响设置面板。
	var brand_tex = load("res://assets/AzureGo2.0.png")
	if brand_tex != null:
		var brand_card := PanelContainer.new()
		brand_card.add_theme_stylebox_override("panel", GufengTheme.panel(12))
		brand_card.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
		brand_card.custom_minimum_size = Vector2(148, 148)
		var brand_img := TextureRect.new()
		brand_img.texture = brand_tex
		brand_img.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		brand_img.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
		brand_img.custom_minimum_size = Vector2(124, 124)
		brand_img.mouse_filter = Control.MOUSE_FILTER_IGNORE
		brand_card.add_child(brand_img)
		vb.add_child(brand_card)

	var title := Label.new()
	title.text = "出发前的准备"
	title.add_theme_font_size_override("font_size", 20)
	title.add_theme_color_override("font_color", GufengTheme.INK)
	vb.add_child(title)
	var sub := Label.new()
	sub.text = hint_text
	sub.add_theme_font_size_override("font_size", 11)
	sub.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	sub.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(sub)

	# 本地语音预加载进度：预热在封面 / 设置期间就已经在跑，这里给玩家一个确定的进度。
	var tts_live := Label.new()
	tts_live.text = "本地语音：检查中…"
	tts_live.add_theme_font_size_override("font_size", 11)
	tts_live.add_theme_color_override("font_color", GufengTheme.DAI)
	tts_live.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vb.add_child(tts_live)
	_poll_setup_tts_state(tts_live)

	vb.add_child(_field_label("界面主题（重启游戏后生效）"))
	var theme_opt := OptionButton.new()
	theme_opt.fit_to_longest_item = false
	for t in GufengTheme.theme_options():
		theme_opt.add_item(str(t["label"]))
		theme_opt.set_item_metadata(theme_opt.item_count - 1, str(t["id"]))
	for i in theme_opt.item_count:
		if str(theme_opt.get_item_metadata(i)) == cfg.ui_theme:
			theme_opt.select(i)
			break
	theme_opt.tooltip_text = "四套主题只换 UI 皮肤：暖白 / 宣纸 / 雾蓝 / 深色。设置面板本身不会即时换皮，重启游戏后应用。"
	vb.add_child(theme_opt)

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

	var tts_body := _setup_section(vb, "语音 TTS（本地 / 云端 / 阿里云）", cfg.tts_mode == "aliyun")
	var le_tts_url := _setup_field(tts_body, "云端服务地址（本地没有时才用）", cfg.tts_base_url,
		"OpenAI 兼容地址，如 https://api.example.com/v1；本地优先地址由 config.json 的 tts_local_base_url 配置")
	var le_tts_voice := _setup_field(tts_body, "云端音色", cfg.tts_voice,
		"云端服务的音色 id（各服务商不同，如 alloy / 中文女）；进游戏后左侧栏也可下拉改")
	var tts_route := Label.new()
	tts_route.text = "实际朗读：" + cfg.tts_route_label()
	tts_route.add_theme_font_size_override("font_size", 11)
	tts_route.add_theme_color_override("font_color", GufengTheme.DAI)
	tts_route.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	tts_body.add_child(tts_route)

	# 阿里云不再单独占一块：收进「语音 TTS」分节里，和朗读方式在同一栏。
	var aliyun_body := _setup_section(tts_body, "阿里云 Voice Studio（可选）", cfg.tts_mode == "aliyun")
	var le_tts_key := _setup_field(aliyun_body, "API Key（Voice Studio 签发）", cfg.tts_aliyun_api_key,
		"Voice Studio 控制台签发的 sk-…；百炼原生 Key 无法通过鉴权（返回 401）。留空=这条回落系统语音")
	le_tts_key.secret = true
	var le_tts_voice_id := _setup_field(aliyun_body, "音色 ID（平台音色 UUID）", cfg.tts_aliyun_voice,
		"在 Voice Studio「音色管理」里复制，形如 9f2a1c4e-7b30-4d51-8a6c-2e5f1b8d0a37；不接受裸音色名")
	var tts_hint := Label.new()
	var mode_names := {"system": "系统语音", "openai": "OpenAI 兼容", "aliyun": "阿里云 Voice Studio"}
	tts_hint.text = "当前朗读方式：%s（进游戏后在左侧栏「朗读方式」切换）" \
		% str(mode_names.get(cfg.tts_mode, cfg.tts_mode))
	tts_hint.add_theme_font_size_override("font_size", 11)
	tts_hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	tts_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	aliyun_body.add_child(tts_hint)

	var vlm_body := _setup_section(vb, "窥屏 VLM（长时间闲置、切去别的窗口时看她一眼）", false)
	var chk_vlm := CheckBox.new()
	chk_vlm.button_pressed = cfg.vlm_enabled
	_style_check(chk_vlm, "启用长时间闲置窥屏")
	vlm_body.add_child(chk_vlm)
	var le_vlm_url := _setup_field(vlm_body, "服务地址（留空=本机自动拉起）", cfg.vlm_base_url,
		"填了就走这个外部/自管 OpenAI 兼容 VLM 服务；留空则由游戏在本机端口拉起 llama-server")
	var le_vlm_port := _setup_field(vlm_body, "本机服务端口", str(cfg.vlm_port),
		"留空服务地址时，游戏在 127.0.0.1 的这个端口自动拉起 VLM 服务；默认 8090")
	var vlm_hint := Label.new()
	vlm_hint.text = "只在游戏窗口失去焦点、且你约 12 秒没操作时触发；截图、OCR 与画面描述都留在本机，不联网。看到密码、聊天、支付、账号等私密内容时不会提，只当你在忙。"
	vlm_hint.add_theme_font_size_override("font_size", 11)
	vlm_hint.add_theme_color_override("font_color", GufengTheme.INK_SOFT)
	vlm_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	vlm_body.add_child(vlm_hint)

	var sep := ColorRect.new()
	sep.color = Color(GufengTheme.INK, 0.22)
	sep.custom_minimum_size = Vector2(0, 1)
	sep.mouse_filter = Control.MOUSE_FILTER_IGNORE
	outer.add_child(sep)
	var hb := HBoxContainer.new()
	hb.alignment = BoxContainer.ALIGNMENT_END
	outer.add_child(hb)
	if cancel_text != "":                          # 从主界面进来的「返回」：不落盘、不置 _setup_done
		var cancel := Button.new()
		cancel.text = cancel_text
		GufengTheme.apply_button(cancel)
		cancel.pressed.connect(func():
			_setup_showing = false
			back.queue_free())
		hb.add_child(cancel)
	# 「本地全套」：把本机服务地址与本机音色一次填好（不碰 API Key / 云端音色）。
	var rec := Button.new()
	rec.text = "本地全套"
	rec.tooltip_text = "填入本机 LLM 8080 / TTS 9880 / 后端 8000 与 Azure 克隆音色；不清空 API Key 与 config 里已有的 KataGo 地址"
	GufengTheme.apply_button(rec)
	rec.pressed.connect(func():
		cfg.backend_url = "http://127.0.0.1:8000"
		cfg.llm_base_url = "http://localhost:8080/v1"
		cfg.tts_enabled = true
		cfg.tts_mode = "local"
		cfg.tts_local_base_url = "http://127.0.0.1:9880/v1"
		cfg.tts_local_voice = "azure"
		cfg.auto_start_services = true
		le_llm_url.text = cfg.llm_base_url
		le_tts_url.text = ""
		tts_route.text = "实际朗读：" + cfg.tts_route_label()
		_fill_voice_items()                       # 左侧栏音色下拉同步成本地列表（Azure 克隆）
		_refresh_tts_route()
		cfg.save()
		_set_status("已填入并保存本地全套：LLM 8080 / TTS 9880 / 后端 8000 + Azure 克隆（KataGo 沿用 config）"))
	hb.add_child(rec)
	var start := Button.new()
	start.text = primary_text
	start.add_theme_font_size_override("font_size", 16)
	GufengTheme.apply_seal_button(start)
	hb.add_child(start)
	start.pressed.connect(func():
		_setup_showing = false
		_setup_done = true                    # 本次运行已经配过：开局不再弹第二遍
		_apply_setup(think_opt, le_llm_url, le_llm_model, le_llm_key,
			le_tts_url, le_tts_voice, le_tts_key, le_tts_voice_id,
			chk_vlm, le_vlm_url, le_vlm_port, theme_opt)
		back.queue_free()
		setup_confirmed.emit())
	start.grab_focus()
	# 高度随内容自适应（折叠时紧凑，展开分节后变高），并保持垂直居中；+64 是底部固定的动作区
	await get_tree().process_frame
	var h := clampf(vb.get_combined_minimum_size().y + 24.0 + 64.0, 240.0, 640.0)
	var shift_y := 26.0                         # 设置面板整体略微下移（约 3% 屏高）
	box.offset_top = -h * 0.5 + shift_y
	box.offset_bottom = h * 0.5 + shift_y

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

## 应用启动设置：写回 config.json（下次启动仍是这些值），随后 _build_services 按新配置起服务。
## TTS 分两节：云端/本地（`tts_base_url` / `tts_voice`）与阿里云（`tts_aliyun_api_key` / `tts_aliyun_voice`）——
## **不改 `tts_mode`**：朗读方式是玩家在左侧栏选的，启动面板不该每次开局都把它悄悄改回去。
func _apply_setup(think_opt: OptionButton, llm_url: LineEdit, llm_model: LineEdit, llm_key: LineEdit,
		tts_url: LineEdit, tts_voice: LineEdit, tts_key: LineEdit, tts_voice_id: LineEdit,
		vlm_on: CheckBox, vlm_url: LineEdit, vlm_port: LineEdit,
		theme_opt: OptionButton) -> void:
	cfg.llm_think_mode = clampi(think_opt.selected, 0, 2)
	if theme_opt != null:
		cfg.ui_theme = str(theme_opt.get_item_metadata(clampi(theme_opt.selected, 0, theme_opt.item_count - 1)))
	if llm_url.text.strip_edges() != "":         # 地址/模型/音色不允许被清成空：空则保留原值
		cfg.llm_base_url = llm_url.text.strip_edges()
	if llm_model.text.strip_edges() != "":
		cfg.llm_model = llm_model.text.strip_edges()
	cfg.llm_api_key = llm_key.text.strip_edges() # Key 可以清空
	if tts_url.text.strip_edges() != "":         # 云端地址 / 音色：空则保留原值
		cfg.tts_base_url = tts_url.text.strip_edges()
	if tts_voice.text.strip_edges() != "":
		cfg.tts_voice = tts_voice.text.strip_edges()
	cfg.tts_aliyun_api_key = tts_key.text.strip_edges()      # Key 可以清空（→ 回落系统语音）
	if tts_voice_id.text.strip_edges() != "":                # 音色 ID 不允许被清成空：空则保留原值
		cfg.tts_aliyun_voice = tts_voice_id.text.strip_edges()
	cfg.vlm_enabled = vlm_on.button_pressed
	cfg.vlm_base_url = vlm_url.text.strip_edges()             # 地址可留空：空=本机自动拉起
	var vport := vlm_port.text.strip_edges()
	if vport.is_valid_int():
		cfg.vlm_port = clampi(vport.to_int(), 1024, 65535)
	_vlm_url = ""                                             # 设置变了：下次重新解析 VLM 地址
	_vlm_starter = ""
	cfg.save()
	if agent != null:
		agent.think_mode = cfg.llm_think_mode       # 主界面「设置」改完即时生效（v3.11.2：不用重启后端）
	if _tts_url_edit != null:                    # 左侧语音栏建在面板之前：同步显示新值
		_tts_url_edit.text = cfg.tts_base_url
		_tts_model_edit.text = cfg.tts_model
		_tts_voice_edit.text = cfg.tts_voice
		if _tts_aliyun_key_edit != null:
			_tts_aliyun_key_edit.text = cfg.tts_aliyun_api_key
		if _tts_aliyun_voice_edit != null:
			_tts_aliyun_voice_edit.text = cfg.tts_aliyun_voice
		_refresh_tts_route()
	print("[Setup] 启动设置已保存：思考模式=%d · LLM=%s (%s) · 朗读方式=%s · 云端=%s/%s · 阿里云音色=%s%s · 窥屏=%s/%s"
			% [cfg.llm_think_mode, cfg.llm_base_url, cfg.llm_model, cfg.tts_mode,
			   cfg.tts_base_url, cfg.tts_voice, cfg.tts_aliyun_voice,
			   "" if cfg.tts_aliyun_api_key != "" else "（未填 Key）",
			   "开" if cfg.vlm_enabled else "关",
			   cfg.vlm_base_url if cfg.vlm_base_url != "" else "127.0.0.1:%d" % cfg.vlm_port])

# ================= 服务与接线 =================

const NetClientScript := preload("res://scripts/net/NetClient.gd")

## 装配服务：编排与 KataGo/LLM 都在后端服务进程；游戏只做渲染与语音，
## 通过 NetClient 以 REST（命令）+ WebSocket（事件）与后端通信。
func _build_services() -> void:
	if agent != null:
		return                                    # 已装配（例如首次从主界面进棋谱库时提前起过）
	_build_client_tts()                           # 语音朗读归客户端
	agent = NetClientScript.new()
	agent.base_url = cfg.backend_url
	agent.net_status.connect(_on_net_status)
	add_child(agent)                              # _ready 里发起 WS 连接（连上先收 hello 同步棋盘与情绪）
	agent.setup()                                 # 占位：引擎/LLM 由后端持有
	agent.set_tts_local_voice(cfg.tts_local_voice)  # 本地音色：后端内存立即对齐（不重启、不丢棋局）
	agent.reply_style = cfg.reply_style           # 说话详略三档（连上后自动补发；旧版后端读 limit_len）
	agent.think_mode = cfg.llm_think_mode         # 思考强度：运行期可改，不用重启后端
	agent.joseki_teach = cfg.joseki_teach
	agent.joseki_rate = cfg.joseki_rate         # 定式提示频率（与开关同一格）：同一通道推给后端
	# 情绪波动与聊天摘要走**原子入口**（v3.8.0 修复波 / #5）：与 P7 的「不退回两次 setter 各推一次」同一口径
	# ——「开关 + 基础概率」一次落定、只推一次 `/api/config`（单字段的 `set_digest` 同一形状）。
	agent.set_drift(cfg.emotion_drift, cfg.drift_base)
	agent.set_digest(cfg.chat_digest_llm)
	agent.set_profile(cfg.profile_enabled, cfg.time_aware_enabled)   # v3.9.0：画像 / 时间感知两个开关
	agent.set_persona_all(cfg.persona_enabled, cfg.persona_relationship_enabled, cfg.persona_address_name)
	agent.adaptive_difficulty = cfg.adaptive_difficulty              # v3.11.5：自适应难度（默认关）
	agent.adaptive_difficulty_base = cfg.adaptive_difficulty_base
	_wire_agent()
	_refresh_lock()                               # 装配完成：解开启动等待期对棋盘的冻结
	_set_status("后端模式：编排与引擎在 %s（本地不启动 KataGo/LLM）" % cfg.backend_url)

## 客户端侧语音：只负责播放（后端按 speech 事件送来的音频 / 系统语音指令），不合成、不起服务
func _build_client_tts() -> void:
	tts = TTSClient.new()
	tts.enabled = cfg.tts_enabled
	tts.mode = cfg.tts_mode
	tts.voice = (cfg.tts_local_voice if cfg.tts_mode == "local" and cfg.tts_local_voice != "" else cfg.tts_voice)
	tts.speed = cfg.tts_speed
	tts.pitch = cfg.tts_pitch
	tts.volume = cfg.tts_volume
	tts.set_suppress_system(_local_tts_loading)    # 质量引擎加载期：系统语音降级不出声（只显示文字）
	add_child(tts)
	tts.allow_fillers = false                     # 开场问候/闲置搭话等非玩家触发的语音不插语气词，等真正对局互动再放行
	tts.status.connect(_on_tts_status)
	tts.speaking_changed.connect(_on_tts_speaking)
	tts.utterance_started.connect(_on_utterance_started)
	tts.speech_skipped.connect(_on_speech_skipped)   # 这条语音不会再响（被停/队列溢出）：文字兜底立即显示
	if cfg.vlm_enabled:
		_ensure_vlm_url()                         # 后台预热窥屏 VLM（GPU 常驻；本机没有环境则静默跳过）

## agent 的信号接线 + 情绪心跳（NetClient 的信号面）
func _wire_agent() -> void:
	agent.message.connect(_add_msg)
	agent.state_changed.connect(_on_state_changed)
	agent.info_changed.connect(_on_info_changed)
	agent.ownership_changed.connect(_on_ownership_changed)
	agent.estimate_event.connect(_on_estimate)      # 局面点目（只在点「局面分析」时来）
	agent.difficulty_changed.connect(_on_difficulty_changed)
	agent.busy_changed.connect(_on_busy_changed)
	agent.move_rejected.connect(_on_move_rejected)   # 这一手被后端拒绝：撤掉乐观子（v3.9.5）
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

## {行为} 标签 → 化身动作：只在文字/语音出现的时刻调用（见 `_add_msg_now`）。
## 一个段里最多两个：表情按顺序播放，后一个覆盖前一个；脸红单独走定时器。
func _play_actions(acts: Array) -> void:
	if avatar == null:
		return
	var played := 0
	for a in acts:
		var key := action_key(str(a))
		if key == "":
			continue
		played += 1
		match key:
			"smile":
				avatar.play_expression("pleased")
			"laugh":
				avatar.play_expression("laugh")
			"proud":
				avatar.play_expression("delight")
			"surprise":
				avatar.play_expression("puzzled")
			"worried":
				avatar.play_expression("worried")
			"sad":
				avatar.play_expression("sad")
			"think":
				avatar.play_expression("puzzled")
			"blush":
				_blush_seq += 1
				var token := _blush_seq
				avatar.set_blush(true)
				get_tree().create_timer(2.6).timeout.connect(func():
					if token == _blush_seq and avatar != null:
						avatar.set_blush(false))
		if played >= 2:
			break

## 三维情绪区域变化：持续表情 / 一次性强调表情 / TTS 音高微调；语气词只在思考节点决定。
func _on_mood_changed(p: Dictionary) -> void:
	if tts != null:
		tts.set_mood_pitch(float(p.get("pitch", 0.0)))
	if avatar != null:
		avatar.set_mood(p.get("morphs", {}))
		avatar.set_arousal(float(p.get("a", _mood_a)))
		var burst := str(p.get("burst", ""))
		if burst != "":
			avatar.play_expression(burst)          # 一次性强调（叠加在持续层上，几秒后回落到区域表情）
	_mood_name = str(p.get("name", _mood_name))
	_mood_v = clampf(float(p.get("v", _mood_v)), 0.0, 1.0)
	_mood_c = clampf(float(p.get("c", _mood_c)), 0.0, 1.0)
	_mood_a = clampf(float(p.get("a", _mood_a)), 0.0, 1.0)
	_last_mood_filler = str(p.get("filler", ""))

## 思考节点插语气词：预制音必须匹配上一句的语气 / 行为标签；频率由当前情绪强度决定。
## 节点 = 新的「Azure 思考中…」占位（语音段之间的真实空隙），不再按墙上时钟定时。
func _maybe_semantic_filler() -> void:
	if tts == null or not tts.enabled:
		return
	if _filler_nodes < FILLER_MIN_NODES:
		return
	var now := Time.get_ticks_msec()
	if now - _last_filler_ms < FILLER_MIN_GAP_MS:
		return
	if tts.is_speaking():
		return                                  # 有语音在播：本节点不插，等下一个空隙
	var kind := ""
	var semantic := false
	if now - _last_ai_semantic_ms <= FILLER_SEMANTIC_MAX_AGE_MS:
		if _last_ai_tone != "":
			kind = _last_ai_tone               # 台词自带的 [[tone]] 优先，语义最准
			semantic = true
		else:
			kind = _filler_for_acts(_last_ai_acts)
			semantic = kind != ""
	var strength := _mood_intensity()
	if kind == "" and _last_mood_filler != "" and strength >= 0.35:
		kind = _last_mood_filler               # 没有台词语义时才退回情绪区域，且只在情绪明显时
	if kind == "" or kind == "think":
		return                                  # 思考声不再插；只保留有语义 / 情绪方向的反应
	if semantic and not _filler_mood_ok(kind):
		return                                  # 台词语义与当前情绪方向冲突：宁可不插
	var prob := (0.08 + 0.42 * strength) if semantic else (0.02 + 0.20 * strength)
	if randf() >= prob:
		return
	_last_filler_ms = now
	_filler_nodes = 0
	tts.play_filler(kind, true)
	print("[TTS] 情绪语气词（节点）：%s · 语义=%s · 强度=%.2f" % [kind, semantic, strength])

## 行为标签 → 可匹配的预制语气词；不匹配的标签（blush / think）不出声。
static func _filler_for_acts(acts: Array) -> String:
	for a in acts:
		match action_key(str(a)):
			"smile", "laugh":
				return "happy"
			"proud":
				return "proud"
			"surprise":
				return "surprise"
			"worried", "sad":
				return "sigh"
	return ""

## 情绪强度：三维偏离 0.5 的最大值（0 平静，1 极端）。
func _mood_intensity() -> float:
	return clampf(maxf(absf(_mood_v - 0.5),
		maxf(absf(_mood_c - 0.5), absf(_mood_a - 0.5))) * 2.0, 0.0, 1.0)

## 台词语义与情绪方向不能打架：叹气别在明显开心时响，笑/得意别在明显难过时响。
func _filler_mood_ok(kind: String) -> bool:
	match kind:
		"sigh":
			return _mood_v <= 0.60
		"happy", "cheer", "proud":
			return _mood_v >= 0.40
		"surprise":
			return _mood_a >= 0.50
		"puzzle":
			return _mood_c >= 0.45
		"hmph":
			return _mood_a >= 0.45
	return true

## 定式识别成功：角部手顺高亮轨迹（几秒后自动淡出；纯显示，不影响棋局状态）
## + 浮层参考图卡片（只渲染后端给的字段：name / line / k / candidates / mistake）
func _on_joseki_event(payload: Dictionary) -> void:
	if _joseki_card != null:
		_joseki_card.apply_event(payload)
		_follow_joseki_card()                   # 形势纸签正开着时，新出的卡片别压在它上面
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
		avatar.avoid_gaze(2.0)                 # 被摸头：先害羞地把视线移开一点
	if tts != null:
		tts.play_filler("pat", true)
	_bump_activity()
	_set_status("你摸了摸 Azure 的头…")
	_blush_seq += 1
	var seq := _blush_seq
	get_tree().create_timer(3.5).timeout.connect(func():
		if seq == _blush_seq and avatar != null:
			avatar.set_blush(false)
			avatar.clear_expression())      # 脸红退去时一并收起摸头引发的笑，避免一直闭眼笑

func _on_tts_status(text: String) -> void:
	if _tts_status != null:
		_tts_status.text = text
	_set_status("语音：" + text)          # 底部状态行始终可见，左侧栏收起时也能看到
	if pairing.has_pending() and (text.contains("失败") or text.contains("无响应") or text.contains("无法")):
		_flush_pending_ai()              # 语音出错：文字不能丢，立即显示

func _greet() -> void:
	if _fr_active:
		return                               # 直接从主界面进全盘复盘：开场白不插进来
	var greet := "嗯~ %s，来下棋吧...我会陪你的 (｡･ω･｡)" % cfg.prompt_user_name
	if tts != null and tts.play_welcome(cfg.prompt_user_name):
		_add_msg_now("ai", greet)             # 预制欢迎语音已经开口：文字直接显示，零等待
	else:
		pairing.mark_text_only()              # 本地问候没有后端语音（预制欢迎语音缺失时）：文字直接显示
		_add_msg("ai", greet)
	_add_msg("system", "你执黑先行。左键落子，右键拖拽可旋转棋盘视角；Home 回正视角，C 依次看四个角。")

func _on_intersection_clicked(x: int, y: int) -> void:
	# 选「等待 CosyVoice」时 `_ready` 会在装配 NetClient 之前一直等语音就绪；棋盘已经画好、
	# 玩家这时点进来会打到 `agent`（Nil）。这里先挡住并说明原因，绝不把棋盘锁在半路。
	if agent == null:
		_set_status("本地服务还在启动（等待语音就绪），稍后再落子。", true)
		return
	_allow_tts_fillers()
	_clear_estimate_marks()                     # 一手落下：棋盘方块过期，形势卡保留（等本手分析刷新）
	_last_ai_kind = ""                          # 新回合：下一句点评 / 落子 / 全局判断重新计数
	_interrupt_speech_for_move()                # v3.11.1：你一落子，她上一轮没说完的话就让位
	if sfx != null:
		sfx.play("stone")                       # 玩家落子：乐观棋子出现的那一刻就响
	# 点下就锁（本地闩）：`busy` 事件回来之前还有一瞬，期间再点会发出第二手——后端会挡下，
	# 但棋盘上已经乐观画上了那颗子，回包又被忽略 ⇒ 撤不掉的幽灵子（v3.9.5）。闩由
	# busy / state / 拒绝回包解开，另有 MOVE_PENDING_SEC 兜底，绝不把棋盘锁死。
	_move_pending = true
	_move_seq += 1
	var seq := _move_seq
	_refresh_lock()
	get_tree().create_timer(MOVE_PENDING_SEC).timeout.connect(func():
		if seq == _move_seq and _move_pending:
			_move_pending = false
			_refresh_lock())
	agent.player_move(x, y)

## 解开「点下就锁」的本地闩（并作废还在跑的兜底计时器）；解锁当场刷新一次，别等下一个事件
func _release_move_pending() -> void:
	if not _move_pending:
		return
	_move_pending = false
	_move_seq += 1
	_refresh_lock()

## 后端明确拒绝了这一手（她还在应手 / 非法）：按权威快照撤掉乐观落下的那颗子。
func _on_move_rejected(st: Dictionary) -> void:
	_release_move_pending()
	if board != null:
		var stones = st.get("stones", [])
		if typeof(stones) == TYPE_ARRAY:
			board.set_stones(stones)
		var seq = st.get("move_seq", [])
		if typeof(seq) == TYPE_ARRAY:
			board.set_move_seq(seq)
	_set_status("这一手没走成：她还在应手，等她落子后再下。")
	_refresh_lock()

func _on_state_changed(p: Dictionary) -> void:
	if _fr_active:
		return                                  # 全盘复盘：棋盘由本地回放掌控，别被 WS state 盖掉
	if _restart_when_idle:
		return                                  # 正在等新局 reset：旧回合的迟到 state 不许把旧盘面推回来
	_release_move_pending()                # 权威快照到了：乐观落子已经被它取代
	var stones = p.get("stones", [])
	if typeof(stones) != TYPE_ARRAY:
		stones = []
	var prev_count := _last_stone_count
	board.set_stones(stones)
	board.set_move_seq(p.get("move_seq", []))       # 悬停已有棋子时推手数用（后端 state 自带 GTP 手顺）
	var seq = p.get("move_seq", [])
	if typeof(seq) == TYPE_ARRAY:
		_last_move_seq = seq.duplicate()            # 战报用：最近手顺
	var mc := int(p["move_count"])
	if mc < _last_move_count and not _reviewing:
		_info_ready = false                         # 重来 / 悔棋不推 info：别把上一局的形势写进战报
	_last_stone_count = stones.size()
	if not _sfx_ready:
		_sfx_ready = true                           # 首次（或重连后）只对齐，不把她已有的棋子一次性播出声音
	elif not _reviewing and mc > _last_move_count:
		_play_move_sfx(p, prev_count)
	if mc > _last_move_count:                       # Azure 每落一子：先注视自己的落点，再交还鼠标跟随
		var stone := _last_stone(stones)
		if not stone.is_empty() and str(stone["c"]) == "white":
			board.look_at_board_point(board.stone_world(int(stone["x"]), int(stone["y"])), 2.0)
	_last_move_count = mc
	if _estimate_move_count >= 0 and mc != _estimate_move_count:
		_clear_estimate_marks()                        # 局面变了：只收棋盘方块，形势卡保留上次点目
	if _reviewing:
		_review_cur = mc                                # 复盘翻页的进度以后端推来的只读快照为准
	_lbl_moves.text = "手数: %d" % mc
	_chk_pass.set_pressed_no_signal(bool(p["allow_pass"]))
	_render_bad_moves(p["bad_moves"])

## 一手的音效：虚着 → 提子 → 她的落子。
## 玩家的落子在 `_on_intersection_clicked` 已经响过，这里不重复；她的子要等 state 揭示才响。
func _play_move_sfx(p: Dictionary, prev_count: int) -> void:
	if sfx == null:
		return
	var seq = p.get("move_seq", [])
	if typeof(seq) == TYPE_ARRAY and seq.size() > 0 and str(seq[seq.size() - 1]) == "pass":
		sfx.play("pass")
		return
	if _last_stone_count < prev_count + 1:
		sfx.play("capture")
		return
	var stone := _last_stone(p.get("stones", []))
	if not stone.is_empty() and str(stone["c"]) == "white":
		sfx.play("stone")

## payload 里带 last=true 的那颗子（最近一手；pass 时为旧值，不会触发注视）
static func _last_stone(stones: Array) -> Dictionary:
	for s in stones:
		if typeof(s) == TYPE_DICTIONARY and bool((s as Dictionary).get("last", false)):
			return s as Dictionary
	return {}

func _on_info_changed(wr: float, sc: float) -> void:
	_last_wr = wr
	_last_score = sc
	_last_azure_wr = 1.0 - clampf(wr, 0.0, 1.0)
	_info_ready = true
	_lbl_wr.text = "黑胜率: %.1f%%" % (wr * 100.0)
	_lbl_score.text = score_line(sc)                       # 原始数字照旧保留
	if _lbl_score_hint != null:
		_lbl_score_hint.text = score_hint(sc, wr)          # 下面那行量级人话
	# 全局形势卡：胜率条 + 目差每手更新（黑方视角，与右栏同源）
	if _card_wr_bar != null:
		_card_wr_bar.value = clampf(wr * 100.0, 0.0, 100.0)
		_card_wr_black.text = "黑 %.1f%%" % (wr * 100.0)
		_card_wr_white.text = "白 %.1f%%" % ((1.0 - wr) * 100.0)
		_card_score.text = score_line(sc)
		_card_score_hint.text = score_hint(sc, wr)
		_show_global_card()

## 领地热力图：后端每次落子后的既有分析会推 361 个归属值（+黑/-白）。
## 复盘快照没有 ownership：进入复盘时已清掉旧图，避免把终局领地盖在回放上。
func _on_ownership_changed(values: Array, _move_count: int) -> void:
	if board == null or _reviewing or _fr_active:
		return
	board.set_ownership(values)
	board.set_ownership_enabled(cfg.ownership_enabled)

## 形势点目（v3.12.0 采纳）：只在「局面分析」的回包里来一次。棋盘画小方块 + 左上角纸签；
## 手数与当前局面不一致（迟到的包）就不显示。它是临时注记：下一手 / 换局面 / 进复盘都会收起。
func _on_estimate(payload: Dictionary) -> void:
	if _estimate_box == null or _reviewing or _fr_active or _title_open:
		return
	var mc := int(payload.get("move_count", -1))
	if mc >= 0 and mc != _last_move_count:
		return                                    # 迟到的旧判断：对应的是上一手，别贴到现在的盘面
	_estimate_move_count = mc
	var count := estimate_count_line(payload.get("black", 0), payload.get("white", 0))
	_estimate_count.text = ("点目（第 %d 手）：%s" % [mc, count]) if mc >= 0 else count
	_estimate_lead.text = estimate_lead_line(payload.get("lead"), payload.get("komi"))
	if board != null:
		var cells = payload.get("cells", [])
		board.set_estimate(cells if typeof(cells) == TYPE_ARRAY else [])
	_show_global_card()
	_follow_joseki_card()                       # 定式卡片若正好在左上角，立刻让开形势纸签

## 收起整张形势卡（连同棋盘上的点目小方块）：标题 / 复盘 / 新局这些出口用。
## 玩家下一手不走这里——那只收方块，卡片常驻保留「上次点目（第 N 手）」。
func _hide_estimate() -> void:
	_estimate_move_count = -1
	if _estimate_box != null:
		_estimate_box.visible = false
	if board != null:
		board.clear_estimate()

## 「黑 41 目 · 白 4 目」：JSON 里是 float，显示成整数；脏值 / 缺字段 ⇒ 「形势: --」，不抛。
static func estimate_count_line(black, white) -> String:
	var b := _metric(black)
	var w := _metric(white)
	if is_nan(b) or is_inf(b) or is_nan(w) or is_inf(w):
		return "形势: --"
	return "黑 %d 目 · 白 %d 目" % [int(b), int(w)]

## 「黑领先 29.5 目（贴 7.5 目）」：`lead` 是黑方视角目差（含贴目，正数＝黑好），`komi` 是贴目。
## |lead| < 0.5 说「双方接近」；脏值只是少说一句（`--` / 不带贴目），不把面板打崩。
static func estimate_lead_line(lead, komi) -> String:
	var s := _metric(lead)
	var txt := "--"
	if not is_nan(s) and not is_inf(s):
		if absf(s) < 0.5:
			txt = "双方接近"
		elif s > 0.0:
			txt = "黑领先 %.1f 目" % s
		else:
			txt = "白领先 %.1f 目" % -s
	var k := _metric(komi)
	if is_nan(k) or is_inf(k):
		return txt
	return "%s（贴 %.1f 目）" % [txt, k]

## 自适应难度档位变化（v3.11.5）：只喂状态栏，不产生台词 / 语音 / 动画。
func _on_difficulty_changed(level: int, base: int, reason: String) -> void:
	if not cfg.adaptive_difficulty:
		return
	var lv := clampi(level, 1, 5)
	var bs := clampi(base, 1, 5)
	var why := str(reason).strip_edges()
	var tail := (" · " + why) if why != "" and not why.begins_with("基线") else ""
	_set_status("自适应难度：%s（基线 %s）%s"
		% [ADAPTIVE_BASE_NAMES[lv - 1], ADAPTIVE_BASE_NAMES[bs - 1], tail])

## 自适应难度基线（1..5）：写回 config.json 并原子推给后端；当前对局立即按新基线重置档位。
func _on_adaptive_base_selected(i: int) -> void:
	var base := clampi(i + 1, 1, 5)
	if base == cfg.adaptive_difficulty_base:
		return
	cfg.adaptive_difficulty_base = base
	cfg.save()
	if agent != null:
		agent.set_adaptive(cfg.adaptive_difficulty, base)
	_set_status("自适应难度基线：%s（立即生效）" % ADAPTIVE_BASE_NAMES[base - 1])

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
	_release_move_pending()                # 后端已接手（或回合已结束）：本地闩让位给真实状态
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
		_try_restart_backend()             # 主界面进来时她还在想：等这一轮收尾再补发新局 reset
	_refresh_lock()
	_clear_thinking_if_idle()

func _on_allow_pass_toggled(pressed: bool) -> void:
	pairing.mark_text_only()                 # 这条提示只有文字（后端同步方法，T20②）
	agent.set_allow_pass(pressed)

## 自适应难度的五档基线（索引 0 = 入门 … 4 = 严苛；配置里存 1..5）
const ADAPTIVE_BASE_NAMES := ["入门", "轻松", "均衡", "挑战", "严苛"]

## 定式导学的四档频率（与下拉框 index-1 一一对应）：低 25% / 中 50% / 高 75% / 总是 100%。
## 最高档给「总是」是给**复测**用的：有资格就一定讲，不必改 config.json 也不会被掷骰吞掉。
const JOSEKI_RATES := [0.25, 0.5, 0.75, 1.0]

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
		_btn_settings.text = "设置 ▾" if on else "设置 ▸"

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
		return "画像功能关着。打开上面的「玩家棋风画像」，下完一局并点「结束」之后，这里就会有内容。"
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
		return "还没有攒下画像——下完一局并点「结束」之后就会有一条。"
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

## 说话详略下拉（右侧栏常驻）：写回 config.json，再走既有 `_flush_settings` 通道推给后端，立即生效。
## 旧键 `tts_limit_len` 同步写回（`save()` 里按 `reply_style == 0` 推导），旧版本读到的仍是「开 / 关」。
func _on_reply_style_selected(i: int) -> void:
	var lvl := clampi(i, 0, 2)
	if lvl == cfg.reply_style:
		return                       # 程序化赋值也可能触发该信号：值没变就不写配置
	cfg.reply_style = lvl
	cfg.tts_limit_len = lvl == 0
	cfg.save()
	if agent != null:
		agent.reply_style = lvl
	var names := ["精简", "适中", "详细"]
	_set_status("说话详略：%s（立即生效）" % names[lvl])
	if _tts_status != null:
		_tts_status.text = "说话详略：%s（立即生效）" % names[lvl]

## 思考强度下拉（⚙ 设置抽屉）：写回 config.json 并推给后端，**不用重启后端**（v3.11.1）。
func _on_think_mode_selected(i: int) -> void:
	var lvl := clampi(i, 0, 2)
	if lvl == cfg.llm_think_mode:
		return
	cfg.llm_think_mode = lvl
	cfg.save()
	if agent != null:
		agent.think_mode = lvl
	var names := ["快速", "中等", "强思考"]
	_set_status("思考强度：%s（立即生效）" % names[lvl])

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
	var names := ["关闭", "低 25%", "中 50%", "高 75%", "总是 100%"]
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

## 玩家这句话是不是在夸她（本地关键词，零 LLM）：只用来决定「被夸时短暂移开视线」。
## 误判成本很低：最坏只是害羞一下，不影响回复内容。
static func looks_like_praise(text: String) -> bool:
	var t := text.strip_edges().to_lower()
	if t == "":
		return false
	for kw in ["喜欢", "爱你", "可爱", "厉害", "真棒", "漂亮", "好看", "温柔", "谢谢", "辛苦了", "帅"]:
		if t.contains(kw):
			return true
	return false

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
	if avatar != null and looks_like_praise(t):
		avatar.avoid_gaze(1.6)                 # 被夸：不直勾勾盯着，先让开一下视线
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

## 玩家主动发起对局互动：作废还在合成/排队/播放的走神搭话。
## 自动语气词已停用（只保留摸头时的预制反应），这里不再开启 TTS 插话。
func _allow_tts_fillers() -> void:
	_cancel_idle_speech()

## 玩家落子时打断她上一轮还没播完的语音（v3.11.1）。
## 棋盘在「她这一轮结束 + 落子揭示」后就放开了，但她的旧话可能还在念——玩家一动，旧话让位，
## 新回合的回应不必排在它后面。两条路径都要收账，否则会留下 `got && !played` 的悬空记录
## （那种记录会让 `audio_in_flight()` 恒真，是本项目最怕的「棋盘锁死」形态）：
##   · 正在播 / 排队中的：`TTSClient.interrupt_for_move()` 先把文字收账（立即显示），当前句 0.25s
##     淡出；不再补预制短句，新回合的回应直接接上；
##   · 还在取音频（HTTP 在途 / 排队）的：`TTSClient` 根本不知道它们，这里自己 finish + display，
##     在途那条回来时 `resolve_fetch` 见到 `played` 会直接判 `done`，不会补播。
func _interrupt_speech_for_move() -> void:
	if tts == null:
		return
	if not tts.is_speaking() and _speech_fetch.is_empty() and _speech_fetch_current.is_empty():
		return
	tts.interrupt_for_move(0.25)
	var jobs: Array[Dictionary] = _speech_fetch.duplicate()
	_speech_fetch.clear()
	if not _speech_fetch_current.is_empty():
		jobs.append(_speech_fetch_current)
		_speech_fetch_current = {}
	for job in jobs:
		var rec: Dictionary = job.get("rec", {})
		if rec.is_empty() or pairing.played(rec):
			continue
		pairing.finish(rec)
		pairing.display(rec)           # 先显示再收账：打断的只是声音，台词不能丢
	_speech_hold = _speech_busy()
	_refresh_lock()

## 作废走神搭话：文字与语音（在途取音频、播放中、排队中）一起清掉，让新的内容立刻顶上
func _cancel_idle_speech() -> void:
	_idle_muted = true
	_cancel_peek()                   # 玩家回来了：正在跑的窥屏直接收掉
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
		_reset_game_view()                   # 棋盘 / 手数 / 胜率 / 目差 / 俗手 / 复盘入口一起清
		agent.reset_game())

func _ask_end() -> void:
	_ask_note("对局总结：想对Azure说什么吗？（可留空直接确定）", func(note):
		_allow_tts_fillers()
		_game_ended = true                   # 「结束」流程走完：复盘入口出现（V19；不读 st.finished）
		if sfx != null:
			sfx.play("game_end")
		_refresh_review_entry()
		_refresh_lock()                      # 立刻置灰提示 / 局面分析 / 结束（重来仍可用）
		agent.summarize(note))

# ================= 局后只读复盘（v3.7.0 任务 D） =================
# 反目标：回看全程她**不出声**（后端已冻结，前端也绝不为此发 chat/idle/hint/analyze）、界面不更挤
# （入口只在局后出现）。只读范围：棋盘（落子与摸头都禁，Board3D.placement_allowed / pat_allowed）+ 四个功能按钮
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

## 「返回主界面」（v3.11.3）：局后与「复盘」并排的出口。若还在复盘，先把后端那份只读状态解除，
## 前端同一帧就走反向运镜——不阻塞回封面（解冻回包迟到也不会把界面拽回去）。
func _on_back_to_title() -> void:
	if _transition or _title_open:
		return
	if _reviewing:
		_exit_review()
	_open_title_screen(true)

# ================= 棋谱战报（v3.11.5） =================
## 生成 PNG：棋盘、手顺、形势、关键点评与亏损手。独立 2D 画布（GameReport），
## 不截主界面、不受 3D 相机 / 侧栏开合影响；复盘时按当前回看手数截断。
func _on_report_request() -> void:
	if _report_busy or board == null:
		return
	_report_busy = true
	_btn_report.disabled = true
	_btn_report.text = "生成中…"
	_set_status("正在生成战报…")
	if agent == null:
		_build_and_save_report({})
		return
	agent.fetch_bytes("/api/review/notes", HTTPClient.METHOD_GET, {},
		func(ok: bool, _code: int, body: PackedByteArray):
			var d := _json_dict(body) if ok else {}
			_build_and_save_report(d))

func _build_and_save_report(d: Dictionary) -> void:
	var move_no := _review_cur if _reviewing else _last_move_count
	var total := _review_total if _reviewing else _last_move_count
	var data := {
		"move_no": move_no,
		"total_moves": total,
		"reviewing": _reviewing,
		"stones": board.visible_stones() if board != null else [],
		"ownership": board.ownership_values() if board != null else [],
		"ownership_enabled": cfg.ownership_enabled,
		"winrate": _last_wr,
		"score": _last_score,
		"has_info": _info_ready and (not _reviewing or _review_cur >= _review_total),
		"move_seq": _last_move_seq,
		"notes": d.get("notes", []),
		"losses": d.get("losses", []),
		"date": Time.get_datetime_string_from_system().replace("T", " "),
		"user": cfg.prompt_user_name,
	}
	var vp := SubViewport.new()
	vp.size = Vector2i(int(GameReport.REPORT_SIZE.x), int(GameReport.REPORT_SIZE.y))
	vp.transparent_bg = false
	vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	var canvas := GameReport.new()
	canvas.set_data(data)
	vp.add_child(canvas)
	add_child(vp)
	await get_tree().process_frame
	await get_tree().process_frame
	await RenderingServer.frame_post_draw
	var img := vp.get_texture().get_image() if vp.get_texture() != null else null
	vp.queue_free()
	if img == null:
		_finish_report(false, "")
		return
	var dir_abs := ProjectSettings.globalize_path("user://reports")
	DirAccess.make_dir_recursive_absolute(dir_abs)
	var stamp := Time.get_datetime_string_from_system().replace(":", "-").replace("T", "_")
	var rel := "user://reports/azure_report_%s.png" % stamp
	var err := img.save_png(rel)
	_finish_report(err == OK, ProjectSettings.globalize_path(rel))

func _finish_report(ok: bool, path: String) -> void:
	_report_busy = false
	if _btn_report != null:
		_btn_report.disabled = false
		_btn_report.text = "战报"
	if ok:
		DisplayServer.clipboard_set(path)
		_set_status("战报已保存（路径已复制）：" + path)
	else:
		_set_status("战报生成失败：无法写入用户目录", true)

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
			_hide_estimate()                                # 复盘期间不留对局内的临时注记
			if board != null:
				board.clear_ownership()                     # 复盘快照没有 ownership：别把终局领地盖在回放上
			_cancel_idle_speech()                       # 复盘期间绝不出声：作废还没开口 / 还在合成的走神搭话（V20）
			_review_total = _last_move_count            # 先给个下界；下面用新鲜 state 覆盖（Minor #6）
			_review_cur = _review_total
			_btn_prev.visible = true
			_btn_next.visible = true
			_btn_review.text = "退出复盘"
			_btn_home.visible = false           # 复盘时这一行已有 4 个按钮；回封面请先「退出复盘」
			_refresh_lock()                             # 冻结（只读 / 置灰 / 禁聊天 / 俗手回档与开关）在这里一处收口
			_set_status("复盘：只读回看中 · 第 %d / %d 手（← → 或按钮翻页）"
					% [_review_cur, _review_total])
			_load_review_intro()
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
			_sfx_ready = false                           # 退出复盘：拉回终局的 state 只对齐，不播提子/落子声
			_btn_prev.visible = false
			_btn_next.visible = false
			_btn_review.text = "复盘"
			_btn_home.visible = true
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

## 复盘入口的显隐：普通复盘只在**对局结束后**出现；全盘复盘模式则只显示它自己那一行。
func _refresh_review_entry() -> void:
	if _review_box != null:
		_review_box.visible = _game_ended and not _fr_active
	if _fr_box != null:
		_fr_box.visible = _fr_active

## 复盘翻页键（← / →）：**只在复盘模式内生效**，绝不抢聊天框的键盘输入（V20）。
## 走 `_input`（最早一步）而不是 `_unhandled_input`：复盘期间聊天框已被禁用（`editable=false`），
## 而焦点在按钮上时方向键会在 GUI 阶段被吃掉去做焦点导航——放到最早才抢不到。
## 复盘模式外立刻返回，键盘行为与以前一字不差。
func _review_keys(event: InputEvent) -> void:
	if not (event is InputEventKey):
		return
	var k := event as InputEventKey
	if not k.pressed or k.echo:
		return
	if _fr_active and not _reviewing:
		if k.keycode == KEY_LEFT:
			_fr_step_by(-1)
			get_viewport().set_input_as_handled()
		elif k.keycode == KEY_RIGHT:
			_fr_step_by(1)
			get_viewport().set_input_as_handled()
		return
	if not _reviewing:
		return
	if k.keycode == KEY_LEFT:
		_review_step(-1)
		get_viewport().set_input_as_handled()
	elif k.keycode == KEY_RIGHT:
		_review_step(1)
		get_viewport().set_input_as_handled()

## 复盘开场白（POST /api/review/intro）：后端按真实手数 / 亏损手账给一句短话，
## 前端**只显示不发声**（mark_text_only），保持复盘期间静音的既有约定；随后再拉逐手存档。
## 失败 / 空文案 ⇒ 直接去拉存档，不挡复盘。
func _load_review_intro() -> void:
	if agent == null:
		_load_review_notes()
		return
	agent.fetch_bytes("/api/review/intro", HTTPClient.METHOD_POST, {},
		func(ok: bool, code: int, body: PackedByteArray):
			if ok:
				var d := _json_dict(body)
				var text := str(d.get("text", "")).strip_edges()
				if text != "" and _reviewing:
					pairing.mark_text_only()
					_add_msg("ai", text)
			_load_review_notes())

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
			var text := str(_strip_tone(str(d.get("text", "")))[1]).strip_edges()
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
	_menu_keys(event)                           # Esc：对局 ⇄ 主界面（弹窗开着时不抢）
	_review_keys(event)

func _bump_activity() -> void:
	_last_activity_ms = Time.get_ticks_msec()
	_last_player_activity_ms = _last_activity_ms  # 只有真实玩家操作才推进这个时钟
	_cancel_peek()                                # 人已经回来：正在跑的窥屏没有意义，别浪费 OCR/VLM
	if _idle:
		_idle = false
		_set_idle_state(false)

## 闲置搭话的等待秒数（纯函数，门卫可测）：
## - 深夜 / 清晨整体放慢，别在困的时候还频繁搭话；
## - 对局中轮到玩家（黑）且游戏窗口在前台：至少 30 秒，给他长考的空间。
static func idle_talk_delay(hour: int, player_turn: bool, first: bool, roll: float) -> float:
	var base := IDLE_TALK_FIRST if first else lerpf(IDLE_TALK_MIN, IDLE_TALK_MAX,
		clampf(0.5 if not is_finite(roll) else roll, 0.0, 1.0))
	if hour >= 23 or hour < 6:
		base *= 1.5
	if player_turn:
		base = maxf(base, 30.0)
	return base

func _hour_now() -> int:
	return int(Time.get_datetime_dict_from_system().get("hour", 12))

## 是否「对局中轮到玩家、且他就在游戏窗口里」：用于把闲置搭话放慢，不误伤挂机窥屏。
func _player_turn_now() -> bool:
	return _last_move_count > 0 and _last_move_count % 2 == 0 and DisplayServer.window_is_focused()

func _process(delta: float) -> void:
	var now := Time.get_ticks_msec()
	# 复盘 / 主界面期间也按「她占着棋盘」处理（M3）：闲置计时停摆、聊天队列不续发——否则玩家回看 / 看菜单时会被算成闲置。
	var hold := _azure_hold_active() or _reviewing or _title_open
	if hold:
		# 她还在思考 / 朗读 / 有句子没播完：棋盘锁着，玩家此刻根本无法落子，
		# 这段时间不能算「玩家闲置」——否则会出现她边堵着棋盘边问「你在忙什么呀？还要继续下棋吗」
		_last_activity_ms = now
		if _idle:
			_idle = false
			_set_idle_state(false)
	_tick_chat_queue(hold)                              # 她这一轮结束、语音也播完：自动续发排队的聊天
	if _restart_when_idle and agent != null and not agent.busy and now >= _restart_retry_ms:
		_try_restart_backend()                          # reset 没发出去 / 后端刚回来：补发，别吞掉新局
	if not _idle and now - _last_activity_ms >= int(IDLE_SEC * 1000.0):
		_idle = true                                    # 长时间没操作：看向玩家、准备歪头
		_set_idle_state(true)
		_next_talk_ms = now + int(idle_talk_delay(_hour_now(), _player_turn_now(), true, 0.0) * 1000.0)
	# 复盘期间绝不出声（V20/M3）：走神搭话本来该由后端门禁挡住，前端也不许发——不依赖 Task C。
	if _idle and now >= _next_talk_ms and not _reviewing and not _title_open and not _fr_active \
			and agent != null and not agent.busy:
		_next_talk_ms = now + int(idle_talk_delay(_hour_now(), _player_turn_now(), false, randf()) * 1000.0)
		_idle_muted = false                             # 新的一轮搭话：先解除「玩家已回来」的作废标记
		_request_idle_talk(now)
	if _peek_busy:
		_tick_peek(now)                                 # 窥屏结果就绪后由她主动开口
	for text in pairing.prune_expired():                 # 等不到语音的配对位清出，别让它吃掉后面那条
		print("[TTS] 等不到语音的台词已从配对表清出：%s" % text.substr(0, 20))
	pairing.release_stale(_pair_facts())                # 回合结束后还等不到语音的文字别一直等
	_sync_speech_hold()                                 # 语音侧锁的兜底对齐（音频 404/取不回时不会锁死棋盘）
	_update_thinking(now)
	_tick_typing(delta)                                 # 打字节奏推进（AI 消息逐字显示）
	_tick_joseki_card()                                 # 浮层卡片的避让重算（几何算在这里，卡片只认矩形）
	_card_check += delta
	if _card_check >= CARD_FOLLOW_SEC:
		_card_check = 0.0
		_follow_joseki_card()                           # 取景变了就把卡片挪出棋盘（用户拖过则不动）

## 定式浮层卡片的每帧避让：把「棋盘落点热区」「鼠标附近的小方块」「展开的侧栏」喂给卡片。
## 几何计算留在本文件（U39：卡片只认矩形，U39 里那两个是最小集）；兜底 TTL 由卡片自己的 `_process` 推进。
func _tick_joseki_card() -> void:
	if _joseki_card == null or _joseki_card.state() == "hidden":
		return                                      # 卡片收起时不必每帧投影 4×4 棋盘格子（常态零开销）
	var mp := get_viewport().get_mouse_position()
	var rects: Array = _board_cell_rects()
	if rects.is_empty():
		rects.append(_board_screen_rect())             # 相机没就位：退回外接矩形（可能是空矩形 = 不管）
	rects.append(Rect2(mp - Vector2(48.0, 48.0), Vector2(96.0, 96.0)))
	rects.append_array(_panel_rects())                 # 展开的侧栏也算「别挡」：不挤压既有控件
	_joseki_card.set_panel_overlap(rects)

## 已展开的侧栏 + 正在显示的形势纸签（避让与「跟随落位」共用同一份判据）
func _panel_rects() -> Array:
	var out: Array = []
	if _left_open and _left_panel != null:
		out.append(_left_panel.get_global_rect())
	if _right_open and _right_panel != null:
		out.append(_right_panel.get_global_rect())
	if _estimate_box != null and _estimate_box.visible:
		out.append(_estimate_box.get_global_rect())
	return out

## 棋盘在屏幕上的**格子矩形**（BOARD_CELLS×BOARD_CELLS 片）：旋转后的棋盘是菱形，用外接矩形判重叠
## 会把两腰的空白三角也算成「棋盘」——卡片落在空地上也会被判 faded。按格子切碎后误判基本消失，
## 而「卡片只认矩形」（U39）这条契约不变。相机背后的角所在的格子直接丢掉（同 `_board_screen_rect` 的守卫）。
func _board_cell_rects() -> Array[Rect2]:
	var out: Array[Rect2] = []
	if board == null or board.cam == null:
		return out
	var cam := board.cam
	var n := BOARD_CELLS
	var rows: Array = []
	for iy in n + 1:
		var row: Array = []
		for ix in n + 1:
			var bx := int(round(float(ix) * float(BOARD_LAST) / float(n)))
			var by := int(round(float(iy) * float(BOARD_LAST) / float(n)))
			var w := board.stone_world(bx, by)
			row.append(Vector2.INF if cam.is_position_behind(w) else cam.unproject_position(w))
		rows.append(row)
	for iy in n:
		for ix in n:
			var a: Vector2 = rows[iy][ix]
			var b: Vector2 = rows[iy][ix + 1]
			var c: Vector2 = rows[iy + 1][ix + 1]
			var d: Vector2 = rows[iy + 1][ix]
			if a == Vector2.INF or b == Vector2.INF or c == Vector2.INF or d == Vector2.INF:
				continue
			var lo := Vector2(minf(minf(a.x, b.x), minf(c.x, d.x)), minf(minf(a.y, b.y), minf(c.y, d.y)))
			var hi := Vector2(maxf(maxf(a.x, b.x), maxf(c.x, d.x)), maxf(maxf(a.y, b.y), maxf(c.y, d.y)))
			out.append(Rect2(lo, hi - lo))
	return out

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

# ================= 长时间闲置窥屏（本地 RapidOCR + Qwen2.5-VL，独立 python 进程一跑一停） =================

## 这一轮闲置到底说什么：玩家切去了别的窗口就先窥屏，否则普通走神搭话。
## 游戏窗口在前台 = 玩家就在游戏里（窥屏只会看到游戏本身）：不白起 python 进程。
func _request_idle_talk(now: int) -> void:
	if agent == null or _peek_busy:
		return
	var idle_ms := now - _last_player_activity_ms
	var peekable := cfg.vlm_enabled and not DisplayServer.window_is_focused()
	if peekable and idle_ms >= int(IDLE_SEC * 1000.0) \
			and now - _peek_last_ms >= PEEK_COOLDOWN_MS and _peek_available():
		_start_peek(now)
	else:
		agent.idle_remark()

## 窥屏环境是否可用：需要 azure_peek.py 与一个装好 rapidocr / PIL 的 python。
## 探测一次后缓存；缺任何一样就永远退回普通走神搭话（优雅降级）。
func _peek_available() -> bool:
	if _peek_probed:
		return _peek_ok
	_peek_probed = true
	var dirs: Array[String] = [
		_repo_root().path_join("peek"),
		OS.get_executable_path().get_base_dir().path_join("peek"),
		"D:/KataGo/peek",
	]
	for d in dirs:
		var script := d.path_join("azure_peek.py")
		if not FileAccess.file_exists(script):
			continue
		var pys: Array[String] = [
			d.path_join(".venv/Scripts/pythonw.exe"),     # 无控制台：不会给窥屏 / VLM 弹出窗口
			d.path_join(".venv/Scripts/python.exe"),
			"D:/KataGo/peek/.venv/Scripts/pythonw.exe",   # 原开发机环境（RapidOCR 已装好）
			"D:/KataGo/peek/.venv/Scripts/python.exe",
			"E:/AI_Omni/Python/pythonw.exe",
			"E:/AI_Omni/Python/python.exe",
		]
		for py in pys:
			if FileAccess.file_exists(py):
				_peek_script = script
				_peek_exe = py
				_peek_ok = true
				print("[Peek] 窥屏可用：%s + %s" % [py, script])
				return true
	print("[Peek] 未找到窥屏环境（azure_peek.py / python），长时间闲置只走普通搭话")
	return false

## 确保窥屏 VLM 服务可用，返回传给窥屏脚本的 --vlm-url（空字符串=不可用，脚本回退 CPU 推理）。
## - cfg.vlm_base_url 填了：视为外部/自管服务，直接用，不碰本地进程
## - 留空且 vlm_auto_start：本机 python 拉起 start_vlm_server.py（内部有 lock + 健康检查防重复）
func _ensure_vlm_url() -> String:
	if _vlm_url != "":
		return _vlm_url
	var manual := cfg.vlm_base_url.strip_edges()
	if manual != "":
		_vlm_url = manual
		return _vlm_url
	if not cfg.vlm_auto_start:
		return ""
	if not _peek_available():                     # 窥屏环境本身不可用：不值得起服务
		return ""
	if _vlm_starter == "":
		var dirs: Array[String] = [
			_repo_root().path_join("peek"),
			OS.get_executable_path().get_base_dir().path_join("peek"),
			"D:/KataGo/peek",
		]
		for d in dirs:
			var st := d.path_join("start_vlm_server.py")
			if FileAccess.file_exists(st):
				_vlm_starter = st
				break
	if _vlm_starter == "":
		return ""                                 # 本机没有 VLM 启动器：窥屏走 CPU/降级
	var args := PackedStringArray([_vlm_starter, "--port", str(cfg.vlm_port)])
	if cfg.vlm_exe != "":
		args.append_array(["--exe", cfg.vlm_exe])
	if cfg.vlm_model != "":
		args.append_array(["--model", cfg.vlm_model])
	if cfg.vlm_mmproj != "":
		args.append_array(["--mmproj", cfg.vlm_mmproj])
	var pid := OS.create_process(_peek_exe, args)
	if pid <= 0:
		print("[Peek] VLM 服务启动器拉起失败，窥屏将回退 CPU 推理")
		return ""
	_vlm_url = "http://127.0.0.1:%d/v1" % cfg.vlm_port
	print("[Peek] 已后台拉起 VLM 服务（端口 %d，加载需要一点时间）" % cfg.vlm_port)
	return _vlm_url

## 后台拉起一次窥屏：独立 python 进程截图 → OCR → VLM，把结果写成 json（不阻塞游戏）
func _start_peek(now: int) -> void:
	_peek_busy = true
	_peek_started_ms = now
	_peek_ctx_activity = _last_player_activity_ms
	# 结果文件按次命名：超时被杀掉的旧进程即使晚写结果，也不会串到下一轮窥屏里
	_peek_out = OS.get_user_data_dir().path_join("peek_result_%d.json" % now)
	var pargs := PackedStringArray([
		_peek_script, "--out", _peek_out, "--game-pid", str(OS.get_process_id())])
	var vlm_url := _ensure_vlm_url()             # 顺手确保 VLM 服务在跑（幂等）
	if vlm_url != "":
		pargs.append_array(["--vlm-url", vlm_url])
	var pid := OS.create_process(_peek_exe, pargs)
	if pid <= 0:
		_peek_busy = false
		if agent != null:
			agent.idle_remark()
		return
	_peek_pid = pid
	_peek_last_ms = now                          # 真的拉起来了才吃冷却
	print("[Peek] 已发起窥屏（pid=%d），等结果…" % pid)

## 玩家回来 / 进入复盘：正在跑的窥屏直接收掉（结果文件也清掉，别在下次误读）
func _cancel_peek() -> void:
	if not _peek_busy:
		return
	if _peek_pid > 0:
		OS.kill(_peek_pid)
	_peek_pid = -1
	_peek_busy = false
	if _peek_out != "" and FileAccess.file_exists(_peek_out):
		DirAccess.remove_absolute(_peek_out)

## 每帧看窥屏结果好了没；等不到就超时放弃
func _tick_peek(now: int) -> void:
	if FileAccess.file_exists(_peek_out):
		_peek_pid = -1
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
			print("[Peek] 结果文件为空/损坏，放弃本次窥屏")
			return
		# 结果回来时玩家已经动过（或已不闲置）：这条窥屏内容过期了，不说
		if _last_player_activity_ms != _peek_ctx_activity or not _idle or agent == null or agent.busy:
			print("[Peek] 结果到达时玩家已回来/不再闲置/Azure 正忙，本次窥屏作废")
			return
		if not bool(data.get("ok", true)):
			print("[Peek] 窥屏失败（ok=false），退回普通搭话")
			agent.idle_remark()
			return
		if bool(data.get("is_game", true)):
			print("[Peek] 前台仍是游戏本身，退回普通搭话")
			agent.idle_remark()
		else:
			print("[Peek] 结合窥屏内容搭话：前台=%s" % str(data.get("fg_title", "")))
			agent.peer_remark(data)
		return
	if now - _peek_started_ms > PEEK_TIMEOUT_MS:
		if _peek_pid > 0:
			OS.kill(_peek_pid)                       # 超时进程多半还在跑：杀掉，别留孤儿占资源
		_peek_pid = -1
		_peek_busy = false
		if _idle and agent != null and not agent.busy \
				and _last_player_activity_ms == _peek_ctx_activity:
			print("[Peek] 超时仍未出结果，退回普通搭话")
			agent.idle_remark()

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
		if not _speech_hold and avatar != null:
			avatar.clear_expression()   # 整段说完：收起一次性表情（笑/惊讶），别让笑脸一直挂着
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
	if typeof(d) == TYPE_DICTIONARY:
		var msg := str((d as Dictionary).get("error", ""))
		if msg != "":
			return msg
		if bool((d as Dictionary).get("busy", false)):
			return "她还在忙（上一轮没结束），稍后再试"   # busy 拒绝是 HTTP 200，不能只显示「HTTP 200」
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
	_speech_fetch_current = job          # v3.11.1：玩家落子打断时，在途这条也要收账
	if agent == null:
		_speech_fetch_busy = false
		_speech_fetch_current = {}
		pairing.resolve_fetch(rec, false, 0)
		_note_audio_failed("后端未连接")
		_pump_speech_fetch()
		return
	agent.fetch_bytes("/api/tts/audio/%s" % str(job["id"]), HTTPClient.METHOD_GET, {},
		func(ok: bool, code: int, body: PackedByteArray):
			_speech_fetch_busy = false
			_speech_fetch_current = {}
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
	_refresh_lock()                    # v3.11.1：落子揭示 ⇒ `pending_reveal()` 清掉 ⇒ 棋盘当场解锁

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
