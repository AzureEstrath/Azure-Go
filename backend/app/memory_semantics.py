"""记忆的情绪语义（v4.3.0）：二维情绪矩阵、标签、相似度与"事实变更"线索。

这一层是**纯逻辑**：不读库、不碰时钟、不调用模型。存储（``memory.py``）只负责把
``(valence, arousal)`` 两列与标签写进去，召回时再用这里的相似度做排序；人格化语言
（``persona.py``）与日程事实（``schedule_facts.py``）各自复用同一套阈值，避免三处
各写一份数字。

二维矩阵取 **愉悦度 valence × 唤醒度 arousal**（都 0~1，3×3 九格）：
它是情绪记忆最稳定、也最容易和语言语速/句长对应起来的两轴。三维情绪里的
好奇心仍留在 ``mood`` 元数据里，但不参与「记忆当时的情绪」标签。
"""

import json
import math

BAND_LO = 1.0 / 3.0
BAND_HI = 2.0 / 3.0

# 3×3 标签：行 = 愉悦度 低/中/高，列 = 唤醒度 低/中/高
MOOD_2D_LABELS = (
    ("低落", "不安", "焦躁"),
    ("淡然", "平静", "振奋"),
    ("惬意", "开心", "兴奋"),
)

# 召回排序时情绪相似度的权重：语义相似度仍是主项（0.75），情绪只做偏置。
EMOTION_RANK_WEIGHT = 0.25

# 「事实变更」线索：出现这些词时，旧记忆可能与新事实冲突（供上层决定是否 supersede）
_CHANGE_WORDS = (
    "取消", "撤掉", "删掉", "去掉", "退掉", "退了", "不去了", "不吃了", "不做了",
    "不开了", "不参加了", "改成", "改到", "换成", "搬到", "搬家", "不再", "以后不",
    "现在住", "现在用", "已经结束", "已经退",
)


def _clamp01(x):
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v) or v < 0.0 or v > 1.0:
        return None
    return v


def _as_dict(mood):
    if isinstance(mood, dict):
        return mood
    if isinstance(mood, str):
        try:
            got = json.loads(mood or "{}")
        except (TypeError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}
    return {}


def band(x) -> int:
    """情绪值 → 低/中/高带（0/1/2）；脏值返回 -1（不猜）。"""
    v = _clamp01(x)
    if v is None:
        return -1
    if v < BAND_LO:
        return 0
    if v < BAND_HI:
        return 1
    return 2


def label_of(valence, arousal) -> str:
    """二维情绪矩阵 → 中文标签；任一维脏值返回空串。"""
    vb = band(valence)
    ab = band(arousal)
    if vb < 0 or ab < 0:
        return ""
    return MOOD_2D_LABELS[vb][ab]


def emotion_of_mood(mood):
    """从三维 mood 字典 / JSON 里提取二维情绪；缺维或脏值返回 ``None``。

    只认 ``v`` / ``a``：好奇心（``c``）不进入记忆标签，但仍会作为 meta 原样保留。
    """
    src = _as_dict(mood)
    v = _clamp01(src.get("v"))
    a = _clamp01(src.get("a"))
    if v is None or a is None:
        return None
    return {"v": v, "a": a, "tag": label_of(v, a)}


def emotion_affinity(mood, emo_v, emo_a):
    """当前情绪与记忆当时情绪的相似度（0~1）；任一未知返回 ``None``。

    距离用二维欧氏距离除以最大距离 ``sqrt(2)``，再取反：同格 ≈ 1，对角 ≈ 0。
    """
    cur = emotion_of_mood(mood)
    if cur is None:
        return None
    v = _clamp01(emo_v)
    a = _clamp01(emo_a)
    if v is None or a is None or v < 0.0 or a < 0.0:
        return None
    d = math.sqrt((cur["v"] - v) ** 2 + (cur["a"] - a) ** 2) / math.sqrt(2.0)
    return max(0.0, min(1.0, 1.0 - d))


def blended_rank(base_score, mood, emo_v, emo_a):
    """召回排序分：语义分 × 0.75 + 情绪相似度 × 0.25；情绪未知时退化为语义分。

    返回 ``(rank, affinity_or_None)``，调用方拿 affinity 决定要不要展示标签。
    """
    try:
        base = float(base_score)
    except (TypeError, ValueError, OverflowError):
        base = 0.0
    aff = emotion_affinity(mood, emo_v, emo_a)
    if aff is None:
        return base, None
    return base * (1.0 - EMOTION_RANK_WEIGHT) + aff * EMOTION_RANK_WEIGHT, aff


def has_change_cue(text) -> bool:
    """文本是否带「这件事变了 / 取消了」的语义线索（纯规则，不判断具体对象）。"""
    t = str(text or "")
    return any(w in t for w in _CHANGE_WORDS)


def day_key(ts):
    """本地日期键（YYYY-MM-DD）；脏值返回空串。给日程记忆做「同一天」比较用。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
