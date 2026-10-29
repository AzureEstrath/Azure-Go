"""回忆链（v4.3.0，D）：主动回忆不是「每次都提一件事」，而是一段可长可短的联想。

一条链有明确的生死条件——都由**话题**与**情绪**决定，不靠随机无限延长：

- **开始**：走神时的概率门（``idle_recall_chance``）或玩家明确提到「上次 / 还记得」；
  链与链之间还有冷却，避免刚回忆完又翻旧账。
- **延续**：同一段话题没有明显换题、情绪没有大幅跳变、链长还没到上限时，
  下一次主动开口可以接着这条链往下说（同一件旧事的细节 / 下一个相关回忆）。
- **结束**：链长到上限、超过时间窗、情绪跳变、或者玩家换了话题；结束后进入冷却。

本模块是**纯逻辑**（不读库、不调模型、没有随机副作用），时钟 / 随机 / 情绪全部由调用方注入。
"""

import math
import re

CHAIN_WINDOW_SEC = 20 * 60        # 一条链最多挂 20 分钟
CHAIN_COOLDOWN_SEC = 10 * 60      # 链结束后 10 分钟内不再主动开新链
CUE_COOLDOWN_SEC = 3 * 60         # 玩家明确提到旧事时，冷却只要求 3 分钟
MOOD_SHIFT_MAX = 0.32             # v/a 平均跳变超过它 ⇒ 这条链的情绪基础已经变了
MAX_DEPTH_CAP = 3                 # 最长链：3 次（短链只有 1 次）

# 玩家明确在说「旧事」的线索：这种时候可以主动接回忆，但仍受短冷却限制
_CUE_RE = re.compile(r"(上次|上回|之前|以前|那时候|还记得|记得|你说过|我跟你说|我们聊过|聊起过)")
# 玩家明确换话题：直接结束链，不再续
_SHIFT_RE = re.compile(r"(换个话题|说点别的|聊点别的|不说这个|不谈这个|算了|不提了)")

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RE = re.compile(r"[0-9a-z_]+")

# 不承载话题的二元组：出现也不影响“换没换题”的判断
_STOP_GRAMS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "就是", "还是", "可以",
    "现在", "时候", "今天", "明天", "昨天", "最近", "一起", "觉得", "好像", "真的",
    "其实", "然后", "因为", "所以", "但是", "如果", "已经", "有点", "一点", "事情",
    "东西", "地方", "知道", "喜欢", "开心", "难过",
}


def _clamp01(x):
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v):
        return None
    return max(0.0, min(1.0, v))


def mood_axis(mood):
    """取当前情绪的 (v, a)；脏值返回 ``None``（链不因坏数据而误判）。"""
    src = mood if isinstance(mood, dict) else {}
    v = _clamp01(src.get("v"))
    a = _clamp01(src.get("a"))
    if v is None or a is None:
        return None
    return v, a


def tokens(text, limit=24):
    """话题词：CJK 二元组 + ASCII 词，去掉不承载话题的停用二元组。"""
    s = str(text or "").lower()
    out = []
    for run in _CJK_RE.findall(s):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(w for w in _WORD_RE.findall(s) if len(w) >= 2)
    kept = []
    for t in out:
        if t in _STOP_GRAMS or t in kept:
            continue
        kept.append(t)
        if len(kept) >= limit:
            break
    return kept


def memory_cue(text) -> bool:
    """玩家是否在明确提旧事（「上次 / 还记得 / 那时候」）。"""
    return _CUE_RE.search(str(text or "")) is not None


def shift_cue(text) -> bool:
    """玩家是否在明确换话题。"""
    return _SHIFT_RE.search(str(text or "")) is not None


def topic_overlap(a, b) -> float:
    """两组话题词的重合度（交集 / 较小集合）；任一为空返回 0.0。"""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(min(len(ta), len(tb)))


def topic_shifted(text, topic) -> bool:
    """当前这句是否已经换题。

    短句（「嗯」「好呀」）不算换题——那只是接话；明确换话题词、或一句够长且
    与链话题**一个词都不重合**时才算换题。
    """
    s = " ".join(str(text or "").split())
    if s == "":
        return False
    if shift_cue(s):
        return True
    if len(tokens(s)) < 3:
        return False
    return topic_overlap(s, topic) <= 0.0


def mood_shifted(chain_mood, mood) -> bool:
    """链开始时的情绪与此刻相比，v/a 平均跳变是否超过阈值。"""
    a = mood_axis(chain_mood)
    b = mood_axis(mood)
    if a is None or b is None:
        return False
    return (abs(a[0] - b[0]) + abs(a[1] - b[1])) / 2.0 > MOOD_SHIFT_MAX


def max_depth(mood, affinity=0.35) -> int:
    """这条链能走几步：累 / 静 ⇒ 1（短链）；平常 ⇒ 2；亲近且心情亮 ⇒ 3（长链）。"""
    axis = mood_axis(mood)
    if axis is None:
        return 2
    v, a = axis
    if v <= 0.38 or a <= 0.24:
        return 1
    try:
        aff = float(affinity)
    except (TypeError, ValueError):
        aff = 0.35
    if aff >= 0.65 and v >= 0.62 and a >= 0.35:
        return MAX_DEPTH_CAP
    return 2


def start(topic, now, mood, *, depth_max=2, source="idle") -> dict:
    """开一条链；``topic`` 是这次回忆的原始文本 / 话题词来源。"""
    axis = mood_axis(mood) or (0.55, 0.35)
    return {
        "topic": str(topic or "")[:160],
        "depth": 0,
        "max_depth": max(1, min(MAX_DEPTH_CAP, int(depth_max or 2))),
        "start_ts": float(now),
        "last_ts": float(now),
        "mood": {"v": axis[0], "a": axis[1]},
        "source": str(source or "idle"),
        "ids": [],
    }


def advance(chain, now, item_id=None) -> dict:
    """记一次延续：深度 +1、刷新时间；返回**新字典**（不改原对象，便于并发快照）。"""
    out = dict(chain or {})
    out["depth"] = int(out.get("depth", 0)) + 1
    out["last_ts"] = float(now)
    ids = list(out.get("ids") or [])
    if item_id is not None:
        ids.append(item_id)
    out["ids"] = ids[-8:]
    return out


def should_start(now, chain, last_end_ts, mood, roll, chance, *, cue=False) -> bool:
    """是否可以开一条新链。

    - 已有链 ⇒ 不开新链（先延续或等它自然结束）；
    - 玩家明确提旧事：只要求 ``CUE_COOLDOWN_SEC`` 的短冷却，不必再赌概率；
    - 走神主动提：先过 ``CHAIN_COOLDOWN_SEC``，再赌 ``idle_recall_chance``。
    """
    if chain:
        return False
    try:
        now_f = float(now)
        last_end = float(last_end_ts or 0.0)
        roll_f = float(roll)
        p = float(chance)
    except (TypeError, ValueError):
        return False
    if cue:
        return now_f - last_end >= CUE_COOLDOWN_SEC
    if now_f - last_end < CHAIN_COOLDOWN_SEC:
        return False
    return 0.0 <= roll_f < p


def should_continue(now, chain, mood, text="", min_gap=0.0) -> bool:
    """这条链能否接着往下回忆（话题 / 情绪 / 长度 / 间隔 / 时间五道门）。

    ``min_gap`` 给聊天用（默认 90 秒）：玩家连发两句时不必每句都再翻一页回忆；
    走神调用保持 0（它本来就隔了几分钟）。
    """
    if not chain:
        return False
    try:
        now_f = float(now)
        last = float(chain.get("last_ts", 0.0))
        gap = max(0.0, float(min_gap))
    except (TypeError, ValueError):
        return False
    if now_f - last > CHAIN_WINDOW_SEC:
        return False
    if now_f - last < gap:
        return False
    if int(chain.get("depth", 0)) >= int(chain.get("max_depth", 2)):
        return False
    if mood_shifted(chain.get("mood"), mood):
        return False
    if text and topic_shifted(text, chain.get("topic", "")):
        return False
    return True


def chain_query(chain, fallback=""):
    """延续时召回用的话题串：链话题 + 当前情绪选的方向。"""
    topic = str((chain or {}).get("topic", "") or "").strip()
    fb = str(fallback or "").strip()
    if topic and fb:
        return topic + " " + fb
    return topic or fb


def prompt_hint(continued: bool) -> str:
    """给提示词的一句「怎么接」；不规定具体内容，只约束别复读。"""
    if continued:
        return ("接着刚才想起的那件旧事往下说半句就够了（可以补一个细节或感受），"
                "别把原句再念一遍；说够就自然收住。")
    return ("如果这件旧事和眼前的话自然接得上，可以轻轻带半句；接不上就什么都别提，"
            "不要提“记忆 / 数据库 / 上次记录”这类词，也不要编造。")
