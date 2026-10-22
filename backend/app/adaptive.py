"""自适应难度（v3.11.5）：在**固定基线**上下做有边界的局内调节。

三条输入（全部零额外引擎 / LLM 调用）：
- **棋风画像**：开局时按 ``blunder.rate``（置信度够才用）给基线一个 ±1 档的修正；
- **对局表现**：最近 6 手玩家 rank / 亏损 / 黑胜率，连续吃力 ⇒ 降一档，连续压制 ⇒ 升一档；
- **聊天**：玩家明确说「太难 / 让着我 / 放水」或「太简单 / 认真点」时，立刻调一档。

两条硬约束（用户口径「不能随意难度波动」）：
- `level` 永远夹在 **基线 ± spread**（默认 ±1 档）且 1..5 内；
- 每 **4 手**最多调整一次（聊天与表现共用同一个冷却）。

落点侧只做「弱化」不做「强化」：在既有 ``moveInfos`` 里按档位对应的胜率容差挑一个合法点，
档位 5 = 恒走最优解（与没开功能逐字一致），档位越低容差越大、越可能选次优。
"""

import random

from . import coords

LEVEL_MIN = 1
LEVEL_MAX = 5
BASE_DEFAULT = 3
SPREAD_DEFAULT = 1
WINDOW = 6
COOLDOWN = 4

# 档位 → 允许的胜率损失（对 Azure 而言）与「真的去挑次优」的概率
TOLERANCE = {1: 0.18, 2: 0.12, 3: 0.07, 4: 0.03, 5: 0.0}
WEAKEN_PROB = {1: 0.55, 2: 0.38, 3: 0.22, 4: 0.10, 5: 0.0}

# 聊天关键词（先查「更难」，避免「别放水」被「放水」误判成想变简单）
HARDER_WORDS = ("别放水", "不要放水", "认真点", "用全力", "全力", "太简单", "不够强", "没挑战", "没意思", "无聊")
EASIER_WORDS = ("太难", "好难", "太强", "下不过", "让着我", "放水", "轻一点", "简单点", "吃力", "扛不住", "手下留情")


def clamp_level(value, default: int = BASE_DEFAULT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = int(default)
    return max(LEVEL_MIN, min(LEVEL_MAX, n))


def profile_adj(profile) -> int:
    """画像修正：常亏手 → 基线降 1 档；很少亏手 → 升 1 档；置信度不足 → 0。"""
    if not isinstance(profile, dict):
        return 0
    row = profile.get("blunder.rate")
    if isinstance(row, dict):
        try:
            value = float(row.get("value"))
            conf = float(row.get("confidence", 1.0))
        except (TypeError, ValueError):
            return 0
    else:
        return 0
    if conf < 0.30:
        return 0
    if value >= 0.20:
        return -1
    if value <= 0.05:
        return 1
    return 0


def chat_signal(text) -> int:
    """聊天里的显式难度诉求：+1 更难 / -1 更简单 / 0 无。"""
    s = str(text or "")
    if any(w in s for w in HARDER_WORDS):
        return 1
    if any(w in s for w in EASIER_WORDS):
        return -1
    return 0


def performance_signal(samples, black_wr) -> int:
    """最近窗口的对局表现：+1 她该加强 / -1 她该放轻 / 0 不动。

    ``samples``：``[{"rank": int, "drop": float, "black_wr": float}, ...]``。
    """
    if not isinstance(samples, list) or len(samples) < 3:
        return 0
    ranks = []
    drops = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            r = int(s.get("rank", -1))
            ranks.append(6 if r < 0 else r)
            drops.append(max(0.0, float(s.get("drop", 0.0))))
        except (TypeError, ValueError):
            continue
    if len(ranks) < 3:
        return 0
    avg_rank = sum(ranks) / len(ranks)
    avg_drop = sum(drops) / len(drops)
    try:
        wr = float(black_wr)
    except (TypeError, ValueError):
        wr = 0.5
    # 玩家明显吃力：排名靠后 / 亏损多 / 黑胜率低，任意两条同时成立才降档
    struggle = (avg_rank >= 2.5) + (avg_drop >= 0.07) + (wr <= 0.36)
    if struggle >= 2:
        return -1
    # 玩家明显压制：排名靠前 / 几乎不亏 / 黑胜率高，三条同时成立才升档
    dominate = (avg_rank <= 1.2) and (avg_drop <= 0.025) and (wr >= 0.64)
    if dominate:
        return 1
    return 0


class AdaptiveDifficulty:
    """局内状态：基线（画像修正后）+ 当前档位 + 调整冷却。"""

    def __init__(self, enabled=False, base=BASE_DEFAULT, spread=SPREAD_DEFAULT, rng=None):
        self.enabled = bool(enabled)
        self.base_cfg = clamp_level(base)
        try:
            self.spread = max(0, min(2, int(spread)))
        except (TypeError, ValueError):
            self.spread = SPREAD_DEFAULT
        self.rng = rng if rng is not None else random.Random()
        self.base = self.base_cfg
        self.level = self.base
        self.last_reason = ""
        self._samples = []
        self._last_adjust = -999

    def reset(self, profile_shift: int = 0):
        """新一局：基线 = 配置基线 + 画像修正，当前档位回到基线，冷却清空。"""
        self.base = clamp_level(self.base_cfg + int(profile_shift or 0))
        self.level = self.base
        self._samples = []
        self._last_adjust = -999
        self.last_reason = "基线 %d" % self.base
        return self.level

    def configure(self, enabled=None, base=None, profile_shift: int = 0):
        if enabled is not None:
            self.enabled = bool(enabled)
        if base is not None:
            self.base_cfg = clamp_level(base)
        self.reset(profile_shift)

    def _apply(self, delta: int, reason: str) -> bool:
        lo = max(LEVEL_MIN, self.base - self.spread)
        hi = min(LEVEL_MAX, self.base + self.spread)
        new_level = max(lo, min(hi, self.level + int(delta)))
        if new_level == self.level:
            return False
        self.level = new_level
        self.last_reason = reason
        return True

    def note_move(self, move_no, black_wr, rank, drop) -> bool:
        """记一手玩家表现；到了冷却点、信号成立才调一档。返回是否变化。"""
        if not self.enabled:
            return False
        try:
            self._samples.append({"rank": int(rank), "drop": float(drop),
                                  "black_wr": float(black_wr)})
        except (TypeError, ValueError):
            return False
        while len(self._samples) > WINDOW:
            self._samples.pop(0)
        try:
            n = int(move_no)
        except (TypeError, ValueError):
            n = 0
        if n - self._last_adjust < COOLDOWN:
            return False
        sig = performance_signal(self._samples, black_wr)
        if sig == 0:
            return False
        self._last_adjust = n
        reason = "你这几手有点吃力，我放轻一点" if sig < 0 else "你压得很稳，我认真一点"
        return self._apply(sig, reason)

    def note_chat(self, text, move_no) -> bool:
        """聊天里的显式诉求；同样受 4 手冷却约束。返回是否变化。"""
        if not self.enabled:
            return False
        sig = chat_signal(text)
        if sig == 0:
            return False
        try:
            n = int(move_no)
        except (TypeError, ValueError):
            n = 0
        if n - self._last_adjust < COOLDOWN:
            return False
        self._last_adjust = n
        reason = "你说想轻松一点" if sig < 0 else "你说想认真一点"
        return self._apply(sig, reason)

    def choose(self, move_infos, is_legal, allow_pass, fallback):
        """在既有候选里按档位挑一个**合法且不更强**的点；档位 5 / 关闭时原样返回 fallback。"""
        if not self.enabled or self.level >= LEVEL_MAX:
            return fallback
        if not isinstance(move_infos, list) or not move_infos:
            return fallback
        if self.rng.random() >= WEAKEN_PROB.get(self.level, 0.0):
            return fallback
        tol = TOLERANCE.get(self.level, 0.0)
        if tol <= 0.0:
            return fallback
        fb_gtp = str((fallback or [""])[0]) if isinstance(fallback, list) else ""
        fb_wr = None
        cands = []
        for m in move_infos:
            if not isinstance(m, dict):
                continue
            gtp = str(m.get("move", ""))
            if gtp == "":
                continue
            try:
                wr = float(m.get("winrate"))
            except (TypeError, ValueError):
                continue
            if gtp == "pass" and not allow_pass:
                continue
            if not is_legal(gtp):
                continue
            if gtp == fb_gtp:
                fb_wr = wr
            cands.append((gtp, wr))
        if not cands:
            return fallback
        base_wr = fb_wr if fb_wr is not None else max(wr for _, wr in cands)
        pool = []
        for gtp, wr in cands:
            loss = base_wr - wr
            if loss < -1e-9 or loss > tol:
                continue                      # 不许变强；超过容差的弱手也不要
            weight = 1.0 + 3.0 * max(0.0, 1.0 - loss / max(tol, 1e-9))
            pool.append((gtp, weight))
        if not pool:
            return fallback
        total = sum(w for _, w in pool)
        roll = self.rng.random() * total
        chosen = pool[-1][0]
        for gtp, w in pool:
            roll -= w
            if roll <= 0:
                chosen = gtp
                break
        x, y = coords.gtp_to_xy(chosen)
        if x < 0 or y < 0:
            return fallback
        return [chosen, x, y]
