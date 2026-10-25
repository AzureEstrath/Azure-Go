"""三维情感模型：愉悦度 valence × 好奇度 curiosity × 唤醒度 arousal（各 0~1）。

逐方法移植自 Godot 客户端 ``scripts/core/EmotionModel.gd``，行为严格对齐。
每维分三带（低/中/高），3×3×3=27 个情绪区域；区域索引驱动表现层：
头像表情（morphs 持续层 + burst 一次性强调）/ 语气词（filler）/ TTS 音高微调（pitch）/
思考风格半句微调（think_hint）。

数值变化两路来源：
  ① 本地事件增量表（apply_event）：落子/聊天/互动/闲置等事件点即时结算；
  ② LLM「情绪变化」小调用（apply_delta）：由 AzureAgent 异步限频执行，失败静默。
结算时机只在事件点与界面心跳，绝不进每帧路径。
"""

import re
import time

BAND_LO = 1.0 / 3.0     # 低/中带界
BAND_HI = 2.0 / 3.0     # 中/高带界
HYST = 0.035            # 分带滞回：情绪值贴着边界时不来回横跳（防表情闪烁）
DECAY_TAU = 150.0       # 向基线回落的时间常数（秒）
DELTA_MAX = 0.4         # 单次增量上限（事件表与 LLM 都受此约束）
# 基线（「平静」）：初始值 = 回落目标。**唯一一份数字**——别处（如情绪波动的 `agent.emotion_bias`）
# 引用它而不是抄一份字面量（修复轮 1 / Minor #2：抄副本会各自静默漂移）。
BASELINE = (0.58, 0.55, 0.30)

# 区域索引 = v带*9 + c带*3 + a带（带 0/1/2 = 低/中/高）
# morphs 只用 Fcl_ALL_*；filler 取自 TTS 预制语气词库；burst 复用现有一次性表情
REGIONS = [
    # —— 消极 · 淡漠 ——
    {"name": "落寞", "morphs": {"Fcl_ALL_Sorrow": 0.35, "Fcl_ALL_Neutral": 0.30}, "filler": "sigh", "pitch": -0.05, "burst": "sad"},
    {"name": "烦闷", "morphs": {"Fcl_ALL_Sorrow": 0.30, "Fcl_ALL_Angry": 0.20}, "filler": "sigh", "pitch": -0.04, "burst": "worried"},
    {"name": "烦躁", "morphs": {"Fcl_ALL_Angry": 0.45, "Fcl_ALL_Sorrow": 0.20}, "filler": "hmph", "pitch": 0.02, "burst": "worried"},
    # —— 消极 · 平常 ——
    {"name": "失落", "morphs": {"Fcl_ALL_Sorrow": 0.45}, "filler": "sigh", "pitch": -0.06, "burst": "sad"},
    {"name": "难过", "morphs": {"Fcl_ALL_Sorrow": 0.60, "Fcl_ALL_Neutral": 0.15}, "filler": "sigh", "pitch": -0.05, "burst": "sad"},
    {"name": "委屈", "morphs": {"Fcl_ALL_Sorrow": 0.50, "Fcl_ALL_Angry": 0.18}, "filler": "hmph", "pitch": -0.03, "burst": "sad"},
    # —— 消极 · 好奇 ——
    {"name": "忧心", "morphs": {"Fcl_ALL_Sorrow": 0.35, "Fcl_ALL_Surprised": 0.15}, "filler": "sigh", "pitch": -0.03, "burst": "worried"},
    {"name": "不安", "morphs": {"Fcl_ALL_Sorrow": 0.40, "Fcl_ALL_Surprised": 0.30}, "filler": "sigh", "pitch": 0.02, "burst": "worried"},
    {"name": "惊慌", "morphs": {"Fcl_ALL_Surprised": 0.60, "Fcl_ALL_Sorrow": 0.25}, "filler": "surprise", "pitch": 0.07, "burst": "worried"},
    # —— 中性 · 淡漠 ——
    {"name": "淡然", "morphs": {"Fcl_ALL_Neutral": 0.40}, "filler": "", "pitch": -0.01, "burst": ""},
    {"name": "随性", "morphs": {"Fcl_ALL_Neutral": 0.35, "Fcl_ALL_Fun": 0.12}, "filler": "", "pitch": 0.0, "burst": ""},
    {"name": "跳脱", "morphs": {"Fcl_ALL_Fun": 0.35, "Fcl_ALL_Surprised": 0.20}, "filler": "happy", "pitch": 0.04, "burst": "puzzled"},
    # —— 中性 · 平常 ——
    {"name": "平静", "morphs": {"Fcl_ALL_Neutral": 0.35, "Fcl_ALL_Fun": 0.15}, "filler": "", "pitch": 0.0, "burst": ""},
    {"name": "专注", "morphs": {"Fcl_ALL_Neutral": 0.30, "Fcl_ALL_Surprised": 0.10}, "filler": "think", "pitch": 0.0, "burst": ""},
    {"name": "振奋", "morphs": {"Fcl_ALL_Fun": 0.30, "Fcl_ALL_Surprised": 0.15}, "filler": "cheer", "pitch": 0.05, "burst": "pleased"},
    # —— 中性 · 好奇 ——
    {"name": "好奇", "morphs": {"Fcl_ALL_Surprised": 0.30, "Fcl_ALL_Neutral": 0.15}, "filler": "puzzle", "pitch": 0.02, "burst": "puzzled"},
    {"name": "兴致", "morphs": {"Fcl_ALL_Surprised": 0.35, "Fcl_ALL_Fun": 0.30}, "filler": "cheer", "pitch": 0.05, "burst": "pleased"},
    {"name": "激动", "morphs": {"Fcl_ALL_Surprised": 0.50, "Fcl_ALL_Fun": 0.30}, "filler": "surprise", "pitch": 0.07, "burst": "delight"},
    # —— 积极 · 淡漠 ——
    {"name": "惬意", "morphs": {"Fcl_ALL_Fun": 0.30, "Fcl_ALL_Neutral": 0.15}, "filler": "happy", "pitch": 0.02, "burst": ""},
    {"name": "愉悦", "morphs": {"Fcl_ALL_Fun": 0.30, "Fcl_ALL_Joy": 0.12}, "filler": "cheer", "pitch": 0.04, "burst": "pleased"},
    {"name": "兴奋", "morphs": {"Fcl_ALL_Fun": 0.28, "Fcl_ALL_Joy": 0.15, "Fcl_ALL_Surprised": 0.24}, "filler": "cheer", "pitch": 0.06, "burst": "delight"},
    # —— 积极 · 平常 ——
    # 持续层以 Fcl_ALL_Fun（睁眼微笑）为主、Fcl_ALL_Joy（闭眼大笑形）只留一点：
    # 之前 Joy 0.5~0.6 会让情绪层长期挂着「闭眼大笑」，一句说完也停不下来；
    # 真正的大笑交给 burst / 一次性表情（几秒后回落）。
    {"name": "开心", "morphs": {"Fcl_ALL_Fun": 0.32, "Fcl_ALL_Joy": 0.16}, "filler": "happy", "pitch": 0.05, "burst": "pleased"},
    {"name": "欢喜", "morphs": {"Fcl_ALL_Fun": 0.36, "Fcl_ALL_Joy": 0.16}, "filler": "happy", "pitch": 0.05, "burst": "delight"},
    {"name": "欢腾", "morphs": {"Fcl_ALL_Fun": 0.38, "Fcl_ALL_Joy": 0.18, "Fcl_ALL_Surprised": 0.16}, "filler": "cheer", "pitch": 0.07, "burst": "delight"},
    # —— 积极 · 好奇 ——
    {"name": "期待", "morphs": {"Fcl_ALL_Fun": 0.28, "Fcl_ALL_Joy": 0.14, "Fcl_ALL_Surprised": 0.20}, "filler": "happy", "pitch": 0.04, "burst": "pleased"},
    {"name": "跃跃欲试", "morphs": {"Fcl_ALL_Fun": 0.30, "Fcl_ALL_Joy": 0.16, "Fcl_ALL_Surprised": 0.30}, "filler": "proud", "pitch": 0.05, "burst": "delight"},
    {"name": "惊喜", "morphs": {"Fcl_ALL_Fun": 0.28, "Fcl_ALL_Joy": 0.22, "Fcl_ALL_Surprised": 0.50}, "filler": "surprise", "pitch": 0.08, "burst": "delight"},
]

# 本地事件 → 三维增量（事件点即时结算；数值刻意偏小，避免情绪跳变）
EVENT_DELTAS = {
    "player_good": (0.07, 0.05, 0.06),      # 好手 / 定式成型
    "player_ok": (0.04, 0.03, 0.03),        # 前 3 候选
    "player_off": (-0.01, 0.05, 0.04),      # 不在前 5：微微意外
    "player_bad": (-0.09, 0.05, 0.10),      # 俗手 / 胜率下滑
    "chat_praise": (0.10, 0.03, 0.07),      # 夸她
    "chat_laugh": (0.06, 0.05, 0.08),       # 开玩笑
    "chat_sad": (-0.10, 0.02, 0.05),        # 说了难过的事
    "chat_question": (0.01, 0.09, 0.03),    # 提问
    "chat_normal": (0.02, 0.03, 0.02),      # 普通闲聊
    "patted": (0.12, -0.02, 0.10),          # 摸头
    "idle_bored": (-0.03, 0.06, -0.04),     # 玩家长时间无动静
    "peek": (-0.01, 0.10, 0.05),            # 悄悄看到玩家切去了别的窗口：好奇、轻微被晾着
    "undo": (-0.03, 0.04, 0.02),            # 悔棋
    "summary_win": (0.08, 0.0, 0.03),       # 对局结束：自己赢了
    "summary_lose": (-0.06, 0.03, -0.02),   # 对局结束：玩家赢了
}

# 事件的中文短描述（喂给 LLM「情绪变化」提示词；玩家一律称「对面」——「他」会被台词照抄成第三人称）
EVENT_DESCS = {
    "player_good": "对面下出了一手好棋",
    "player_ok": "对面下了一手不错的棋",
    "player_off": "对面下了一手不太常见的棋",
    "player_bad": "对面下出了俗手，局面变差了",
    "chat_praise": "对面夸了你",
    "chat_laugh": "对面和你开玩笑，气氛轻松",
    "chat_sad": "对面说了些难过的事",
    "chat_question": "对面问了你一个问题",
    "chat_normal": "对面和你聊了几句",
    "patted": "对面摸了摸你的头",
    "idle_bored": "对面很久没有动静了",
    "peek": "你悄悄看了一眼屏幕，对面正在忙别的事",
    "undo": "对面悔棋重下了",
    "summary_win": "对局结束，你赢了",
    "summary_lose": "对局结束，对面赢了",
}

# LLM「情绪变化」输出的解析正则（含全角冒号/逗号）
_DELTA_RE = re.compile(
    r"\[\[\s*mood\s*[:：]\s*([+-]?\d+(?:\.\d+)?)\s*[,，]\s*"
    r"([+-]?\d+(?:\.\d+)?)\s*[,，]\s*([+-]?\d+(?:\.\d+)?)\s*\]\]"
)


def _clampf(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


class EmotionModel:
    """三维情感模型（对应 GDScript ``EmotionModel``）。"""

    # 常量（对外与 GDScript 的 class const 对齐）
    REGIONS = REGIONS
    EVENT_DELTAS = EVENT_DELTAS
    EVENT_DESCS = EVENT_DESCS
    BAND_LO = BAND_LO
    BAND_HI = BAND_HI
    HYST = HYST
    DECAY_TAU = DECAY_TAU
    DELTA_MAX = DELTA_MAX
    # 基线（「平静」）：初始值与回落目标都是它。**类级常量**，供别处（如情绪波动的
    # `agent.emotion_bias`）引用同一份数字，别抄字面量副本（修复轮 1 / Minor #2）。
    BASELINE = BASELINE

    def __init__(self):
        self.v = BASELINE[0]           # 愉悦度
        self.c = BASELINE[1]           # 好奇度
        self.a = BASELINE[2]           # 唤醒度
        self.baseline = BASELINE       # 初始值 / 回落目标（平静）

        self._bands = [1, 1, 0]        # 当前各维分带（滞回比较用）
        self._region = 12              # 当前区域索引（baseline → 平静）
        self._t_ms = 0                 # 上次结算时刻（懒衰减基准）

        self._t_ms = time.monotonic() * 1000.0

    # ================= 查询 =================

    def region(self):
        """当前区域索引（先结算懒衰减；区域变化只可能发生在这里）。"""
        self._decay()
        return self._region_of(self.v, self.c, self.a)

    def info(self, idx=-1):
        """区域数据 {name, morphs, filler, pitch, burst}；idx<0 取当前区域。"""
        i = self._region if idx < 0 else idx
        if i < 0 or i >= len(REGIONS):
            i = 12
        return REGIONS[i]

    def region_name(self):
        return str(self.info().get("name", ""))

    def think_hint(self):
        """思考风格微调：随分带给出半句差异。"""
        parts = []
        if self._bands[2] >= 2:
            parts.append("此刻精神很活跃：可以顺着棋感放开算，允许一点跳跃和冒险的念头")
        elif self._bands[2] == 0:
            parts.append("此刻心境平缓：思考放慢半拍，多确认一步再开口")
        else:
            parts.append("按平时节奏思考即可")
        if self._bands[1] >= 2:
            parts.append("多揣摩对面的意图和后续变化")
        elif self._bands[1] == 0:
            parts.append("不用发散，盯住盘面本身")
        return "；".join(parts) + "。"

    # ================= 变更 =================

    def apply_event(self, ev):
        """事件结算：查表取增量并立即生效。"""
        d = EVENT_DELTAS.get(ev, (0.0, 0.0, 0.0))
        self.apply_delta(d[0], d[1], d[2])

    def apply_delta(self, dv, dc, da):
        """施加三维增量（自动夹取单次幅度与总量 0~1），并刷新区域分带。"""
        self._decay()
        self.v = _clampf(self.v + _clampf(dv, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
        self.c = _clampf(self.c + _clampf(dc, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
        self.a = _clampf(self.a + _clampf(da, -DELTA_MAX, DELTA_MAX), 0.0, 1.0)
        self._region_of(self.v, self.c, self.a)

    def reset(self):
        """回到基线（重新开局用；分带与区域一并复位，不走滞回）。"""
        self.v = self.baseline[0]
        self.c = self.baseline[1]
        self.a = self.baseline[2]
        self._bands = [self._raw_band(self.v), self._raw_band(self.c), self._raw_band(self.a)]
        self._region = self._bands[0] * 9 + self._bands[1] * 3 + self._bands[2]

    def carry(self, mood, keep=0.5):
        """把上一局的**情绪余温**带进新局：``keep`` 是保留比例，其余回落到基线。

        人格化用：刚结束的一局若有点低落 / 得意，下一局开头不必假装什么都没发生；
        ``keep=0.5`` 即「一半余温、一半平静」。``mood`` 脏值 / 缺维 ⇒ 退回 ``reset()``。
        """
        src = mood if isinstance(mood, dict) else {}
        try:
            v, c, a = float(src.get("v")), float(src.get("c")), float(src.get("a"))
        except (TypeError, ValueError):
            self.reset()
            return
        try:
            k = max(0.0, min(1.0, float(keep)))
        except (TypeError, ValueError):
            k = 0.5
        bv, bc, ba = self.baseline
        self.v = _clampf(bv + (v - bv) * k, 0.0, 1.0)
        self.c = _clampf(bc + (c - bc) * k, 0.0, 1.0)
        self.a = _clampf(ba + (a - ba) * k, 0.0, 1.0)
        self._bands = [self._raw_band(self.v), self._raw_band(self.c), self._raw_band(self.a)]
        self._region = self._bands[0] * 9 + self._bands[1] * 3 + self._bands[2]

    # ================= 解析 =================

    @staticmethod
    def parse_delta(text):
        """解析 LLM「情绪变化」输出：[[mood:+0.10,-0.05,+0.20]] → [是否有效, (dv,dc,da)]。"""
        m = _DELTA_RE.search(text)
        if m is None:
            return [False, (0.0, 0.0, 0.0)]
        return [True, (float(m.group(1)), float(m.group(2)), float(m.group(3)))]

    @staticmethod
    def event_desc(ev):
        """事件的中文短描述（无记录返回空串）。"""
        return str(EVENT_DESCS.get(ev, ""))

    # ================= 内部 =================

    def _decay(self):
        """懒衰减：把三维朝基线拉回（时间常数 DECAY_TAU），只在结算时按 elapsed 计算。"""
        now = time.monotonic() * 1000.0
        dt = float(now - self._t_ms) / 1000.0
        self._t_ms = now
        if dt <= 0.0:
            return
        k = _clampf(dt / DECAY_TAU, 0.0, 1.0)
        self.v += (self.baseline[0] - self.v) * k
        self.c += (self.baseline[1] - self.c) * k
        self.a += (self.baseline[2] - self.a) * k

    def _region_of(self, nv, nc, na):
        """由三维值算区域并刷新分带（带滞回，防边界抖动）。"""
        self._bands = [
            self._band(nv, self._bands[0]),
            self._band(nc, self._bands[1]),
            self._band(na, self._bands[2]),
        ]
        self._region = self._bands[0] * 9 + self._bands[1] * 3 + self._bands[2]
        return self._region

    @staticmethod
    def _band(x, prev):
        """单维分带（0/1/2=低/中/高），带滞回。

        相邻跨带需越过边界 HYST 以外，否则维持原带；
        一次跨两带（两条边界都已越过）直接接受，不参与滞回。
        """
        raw = EmotionModel._raw_band(x)
        if raw == prev or abs(raw - prev) > 1:
            return raw
        if raw < prev:                                   # 相邻下降：上一条边界
            edge = BAND_LO if prev == 1 else BAND_HI
            return prev if x > edge - HYST else raw
        edge2 = BAND_LO if raw == 1 else BAND_HI         # 相邻上升：目标带的下边界
        return prev if x < edge2 + HYST else raw

    @staticmethod
    def _raw_band(x):
        return 0 if x < BAND_LO else (1 if x < BAND_HI else 2)
