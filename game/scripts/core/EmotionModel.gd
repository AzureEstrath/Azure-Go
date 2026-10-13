class_name EmotionModel
extends RefCounted
## 三维情感模型：愉悦度 valence × 好奇度 curiosity × 唤醒度 arousal（各 0~1）。
## 每维分三带（低/中/高），3×3×3=27 个情绪区域；区域索引驱动表现层：
##   头像表情（morphs 持续层 + burst 一次性强调）/ 语气词（filler）/ TTS 音高微调（pitch）/
##   思考风格半句微调（think_hint，仅思考档开启时被调用方拼进提示词）。
##
## 数值变化两路来源：
##   ① 本地事件增量表（apply_event）：落子/聊天/互动/闲置等事件点即时结算，零额外开销；
##   ② LLM「情绪变化」小调用（apply_delta）：由 AzureAgent 异步限频执行，失败静默。
## 结算时机只在事件点与界面每 2 秒的心跳，绝不进每帧路径。

const BAND_LO := 1.0 / 3.0     # 低/中带界
const BAND_HI := 2.0 / 3.0     # 中/高带界
const HYST := 0.035            # 分带滞回：情绪值贴着边界时不来回横跳（防表情闪烁）
const DECAY_TAU := 150.0       # 向基线回落的时间常数（秒）
const DELTA_MAX := 0.4         # 单次增量上限（事件表与 LLM 都受此约束）

## 区域索引 = v带*9 + c带*3 + a带（带 0/1/2 = 低/中/高）
## morphs 只用 Fcl_ALL_*（与化身现有形变收集一致）；filler 取自 TTS 预制语气词库；burst 复用现有一次性表情
const REGIONS: Array = [
	# —— 消极 · 淡漠 ——
	{"name": "落寞",     "morphs": {"Fcl_ALL_Sorrow": 0.35, "Fcl_ALL_Neutral": 0.30}, "filler": "sigh",     "pitch": -0.05, "burst": "sad"},
	{"name": "烦闷",     "morphs": {"Fcl_ALL_Sorrow": 0.30, "Fcl_ALL_Angry": 0.20},   "filler": "sigh",     "pitch": -0.04, "burst": "worried"},
	{"name": "烦躁",     "morphs": {"Fcl_ALL_Angry": 0.45, "Fcl_ALL_Sorrow": 0.20},   "filler": "hmph",     "pitch": 0.02,  "burst": "worried"},
	# —— 消极 · 平常 ——
	{"name": "失落",     "morphs": {"Fcl_ALL_Sorrow": 0.45},                           "filler": "sigh",     "pitch": -0.06, "burst": "sad"},
	{"name": "难过",     "morphs": {"Fcl_ALL_Sorrow": 0.60, "Fcl_ALL_Neutral": 0.15}, "filler": "sigh",     "pitch": -0.05, "burst": "sad"},
	{"name": "委屈",     "morphs": {"Fcl_ALL_Sorrow": 0.50, "Fcl_ALL_Angry": 0.18},   "filler": "hmph",     "pitch": -0.03, "burst": "sad"},
	# —— 消极 · 好奇 ——
	{"name": "忧心",     "morphs": {"Fcl_ALL_Sorrow": 0.35, "Fcl_ALL_Surprised": 0.15}, "filler": "sigh",   "pitch": -0.03, "burst": "worried"},
	{"name": "不安",     "morphs": {"Fcl_ALL_Sorrow": 0.40, "Fcl_ALL_Surprised": 0.30}, "filler": "sigh",   "pitch": 0.02,  "burst": "worried"},
	{"name": "惊慌",     "morphs": {"Fcl_ALL_Surprised": 0.60, "Fcl_ALL_Sorrow": 0.25}, "filler": "surprise", "pitch": 0.07, "burst": "worried"},
	# —— 中性 · 淡漠 ——
	{"name": "淡然",     "morphs": {"Fcl_ALL_Neutral": 0.40},                          "filler": "",         "pitch": -0.01, "burst": ""},
	{"name": "随性",     "morphs": {"Fcl_ALL_Neutral": 0.35, "Fcl_ALL_Fun": 0.12},     "filler": "",         "pitch": 0.0,   "burst": ""},
	{"name": "跳脱",     "morphs": {"Fcl_ALL_Fun": 0.35, "Fcl_ALL_Surprised": 0.20},   "filler": "happy",    "pitch": 0.04,  "burst": "puzzled"},
	# —— 中性 · 平常 ——
	{"name": "平静",     "morphs": {"Fcl_ALL_Neutral": 0.35, "Fcl_ALL_Fun": 0.15},     "filler": "",         "pitch": 0.0,   "burst": ""},
	{"name": "专注",     "morphs": {"Fcl_ALL_Neutral": 0.30, "Fcl_ALL_Surprised": 0.10}, "filler": "think",  "pitch": 0.0,   "burst": ""},
	{"name": "振奋",     "morphs": {"Fcl_ALL_Fun": 0.30, "Fcl_ALL_Surprised": 0.15},   "filler": "cheer",    "pitch": 0.05,  "burst": "pleased"},
	# —— 中性 · 好奇 ——
	{"name": "好奇",     "morphs": {"Fcl_ALL_Surprised": 0.30, "Fcl_ALL_Neutral": 0.15}, "filler": "puzzle", "pitch": 0.02,  "burst": "puzzled"},
	{"name": "兴致",     "morphs": {"Fcl_ALL_Surprised": 0.35, "Fcl_ALL_Fun": 0.30},   "filler": "cheer",    "pitch": 0.05,  "burst": "pleased"},
	{"name": "激动",     "morphs": {"Fcl_ALL_Surprised": 0.50, "Fcl_ALL_Fun": 0.30},   "filler": "surprise", "pitch": 0.07,  "burst": "delight"},
	# —— 积极 · 淡漠 ——
	{"name": "惬意",     "morphs": {"Fcl_ALL_Joy": 0.35, "Fcl_ALL_Neutral": 0.15},     "filler": "happy",    "pitch": 0.02,  "burst": ""},
	{"name": "愉悦",     "morphs": {"Fcl_ALL_Joy": 0.50},                              "filler": "cheer",    "pitch": 0.04,  "burst": "pleased"},
	{"name": "兴奋",     "morphs": {"Fcl_ALL_Joy": 0.50, "Fcl_ALL_Surprised": 0.30},   "filler": "cheer",    "pitch": 0.06,  "burst": "delight"},
	# —— 积极 · 平常 ——
	{"name": "开心",     "morphs": {"Fcl_ALL_Joy": 0.60},                              "filler": "happy",    "pitch": 0.05,  "burst": "pleased"},
	{"name": "欢喜",     "morphs": {"Fcl_ALL_Joy": 0.60, "Fcl_ALL_Fun": 0.25},         "filler": "happy",    "pitch": 0.05,  "burst": "delight"},
	{"name": "欢腾",     "morphs": {"Fcl_ALL_Joy": 0.60, "Fcl_ALL_Fun": 0.40, "Fcl_ALL_Surprised": 0.20}, "filler": "cheer", "pitch": 0.07, "burst": "delight"},
	# —— 积极 · 好奇 ——
	{"name": "期待",     "morphs": {"Fcl_ALL_Joy": 0.35, "Fcl_ALL_Surprised": 0.20},   "filler": "happy",    "pitch": 0.04,  "burst": "pleased"},
	{"name": "跃跃欲试", "morphs": {"Fcl_ALL_Joy": 0.40, "Fcl_ALL_Surprised": 0.30, "Fcl_ALL_Fun": 0.20}, "filler": "proud", "pitch": 0.05, "burst": "delight"},
	{"name": "惊喜",     "morphs": {"Fcl_ALL_Joy": 0.60, "Fcl_ALL_Surprised": 0.60, "Fcl_ALL_Fun": 0.25}, "filler": "surprise", "pitch": 0.08, "burst": "delight"},
]

## 本地事件 → 三维增量（事件点即时结算；数值刻意偏小，避免情绪跳变）
const EVENT_DELTAS := {
	"player_good": Vector3(0.07, 0.05, 0.06),      # 好手 / 定式成型
	"player_ok": Vector3(0.04, 0.03, 0.03),        # 前 3 候选
	"player_off": Vector3(-0.01, 0.05, 0.04),      # 不在前 5：微微意外
	"player_bad": Vector3(-0.09, 0.05, 0.10),      # 俗手 / 胜率下滑
	"chat_praise": Vector3(0.10, 0.03, 0.07),      # 夸她
	"chat_laugh": Vector3(0.06, 0.05, 0.08),       # 开玩笑
	"chat_sad": Vector3(-0.10, 0.02, 0.05),        # 说了难过的事
	"chat_question": Vector3(0.01, 0.09, 0.03),    # 提问
	"chat_normal": Vector3(0.02, 0.03, 0.02),      # 普通闲聊
	"patted": Vector3(0.12, -0.02, 0.10),          # 摸头
	"idle_bored": Vector3(-0.03, 0.06, -0.04),     # 玩家长时间无动静
	"peek": Vector3(-0.02, 0.08, 0.03),            # 窥屏看到他在忙别的
	"undo": Vector3(-0.03, 0.04, 0.02),            # 悔棋
	"summary_win": Vector3(0.08, 0.0, 0.03),       # 对局结束：自己赢了
	"summary_lose": Vector3(-0.06, 0.03, -0.02),   # 对局结束：他赢了
}

## 事件的中文短描述（喂给 LLM「情绪变化」提示词）
const EVENT_DESCS := {
	"player_good": "他下出了一手好棋",
	"player_ok": "他下了一手不错的棋",
	"player_off": "他下了一手不太常见的棋",
	"player_bad": "他下出了俗手，局面变差了",
	"chat_praise": "他夸了你",
	"chat_laugh": "他和你开玩笑，气氛轻松",
	"chat_sad": "他说了些难过的事",
	"chat_question": "他问了你一个问题",
	"chat_normal": "他和你聊了几句",
	"patted": "他摸了摸你的头",
	"idle_bored": "他很久没有动静了",
	"peek": "你偷偷看到他在忙别的事情",
	"undo": "他悔棋重下了",
	"summary_win": "对局结束，你赢了",
	"summary_lose": "对局结束，他赢了",
}

# ================= 状态 =================

var v := 0.58                  # 愉悦度
var c := 0.55                  # 好奇度
var a := 0.30                  # 唤醒度
var baseline := Vector3(0.58, 0.55, 0.30)   # 初始值 / 回落目标（平静）

var _bands := Vector3i(1, 1, 0)   # 当前各维分带（滞回比较用）
var _region := 12                 # 当前区域索引（baseline → 平静）
var _t_ms := 0                    # 上次结算时刻（懒衰减基准）

func _init() -> void:
	_t_ms = Time.get_ticks_msec()

# ================= 查询 =================

## 当前区域索引（先结算懒衰减；区域变化只可能发生在这里）
func region() -> int:
	_decay()
	return _region_of(v, c, a)

## 区域数据 {name, morphs, filler, pitch, burst}；idx<0 取当前区域
func info(idx := -1) -> Dictionary:
	var i := _region if idx < 0 else idx
	if i < 0 or i >= REGIONS.size():
		i = 12
	return REGIONS[i]

func region_name() -> String:
	return str(info().get("name", ""))

## 思考风格微调（仅思考档开启时由调用方拼进提示词）：随分带给出半句差异
func think_hint() -> String:
	var parts: Array[String] = []
	if _bands.z >= 2:
		parts.append("此刻精神很活跃：可以顺着棋感放开算，允许一点跳跃和冒险的念头")
	elif _bands.z == 0:
		parts.append("此刻心境平缓：思考放慢半拍，多确认一步再开口")
	else:
		parts.append("按平时节奏思考即可")
	if _bands.y >= 2:
		parts.append("多揣摩他的意图和后续变化")
	elif _bands.y == 0:
		parts.append("不用发散，盯住盘面本身")
	return "；".join(parts) + "。"

# ================= 变更 =================

## 事件结算：查表取增量并立即生效（供落子/聊天/互动/闲置等事件点调用）
func apply_event(ev: String) -> void:
	var d: Vector3 = EVENT_DELTAS.get(ev, Vector3.ZERO)
	apply_delta(d.x, d.y, d.z)

## 施加三维增量（自动夹取单次幅度与总量 0~1），并刷新区域分带
func apply_delta(dv: float, dc: float, da: float) -> void:
	_decay()
	v = clampf(v + clampf(dv, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
	c = clampf(c + clampf(dc, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
	a = clampf(a + clampf(da, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
	_region_of(v, c, a)

## 回到基线（重新开局用；分带与区域一并复位，不走滞回）
func reset() -> void:
	v = baseline.x
	c = baseline.y
	a = baseline.z
	_bands = Vector3i(_raw_band(v), _raw_band(c), _raw_band(a))
	_region = _bands.x * 9 + _bands.y * 3 + _bands.z

# ================= 解析 =================

## 解析 LLM「情绪变化」输出：[[mood:+0.10,-0.05,+0.20]] → [是否有效, Vector3]
static func parse_delta(text: String) -> Array:
	var re := RegEx.new()
	if re.compile("\\[\\[\\s*mood\\s*[:：]\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*[,，]\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*[,，]\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*\\]\\]") != OK:
		return [false, Vector3.ZERO]
	var m := re.search(text)
	if m == null:
		return [false, Vector3.ZERO]
	return [true, Vector3(m.get_string(1).to_float(), m.get_string(2).to_float(), m.get_string(3).to_float())]

## 事件的中文短描述（无记录返回空串）
static func event_desc(ev: String) -> String:
	return str(EVENT_DESCS.get(ev, ""))

# ================= 内部 =================

## 懒衰减：把三维朝基线拉回（时间常数 DECAY_TAU），只在结算时按 elapsed 计算
func _decay() -> void:
	var now := Time.get_ticks_msec()
	var dt := float(now - _t_ms) / 1000.0
	_t_ms = now
	if dt <= 0.0:
		return
	var k := clampf(dt / DECAY_TAU, 0.0, 1.0)
	v += (baseline.x - v) * k
	c += (baseline.y - c) * k
	a += (baseline.z - a) * k

## 由三维值算区域并刷新分带（带滞回，防边界抖动）
func _region_of(nv: float, nc: float, na: float) -> int:
	_bands = Vector3i(_band(nv, _bands.x), _band(nc, _bands.y), _band(na, _bands.z))
	_region = _bands.x * 9 + _bands.y * 3 + _bands.z
	return _region

## 单维分带（0/1/2=低/中/高），带滞回：相邻跨带需越过边界 HYST 以外，否则维持原带；
## 一次跨两带（两条边界都已越过）直接接受，不参与滞回
static func _band(x: float, prev: int) -> int:
	var raw := _raw_band(x)
	if raw == prev or absi(raw - prev) > 1:
		return raw
	if raw < prev:                                   # 相邻下降：上一条边界
		var edge := BAND_LO if prev == 1 else BAND_HI
		return prev if x > edge - HYST else raw
	var edge2 := BAND_LO if raw == 1 else BAND_HI    # 相邻上升：目标带的下边界
	return prev if x < edge2 + HYST else raw

static func _raw_band(x: float) -> int:
	return 0 if x < BAND_LO else (1 if x < BAND_HI else 2)