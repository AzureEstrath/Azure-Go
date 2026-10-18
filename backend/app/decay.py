"""事件遗忘权重 → 4 档淡化（纯函数）。

分工（用户已定）：**权重只决定「在接下来的多少步淡化」，不决定淡化程度**——程度是固定的 4 档。
``DECAY_SPAN = [1, 3, 6]`` 是每档的**累计上界**（第 1 档到第 1 手、第 2 档到第 3 手、第 3 档到
第 6 手），第 4 档到 ``DECAY_DROP_AT = 12``；超过 12 手**不再进提示词**（事件仍留在表里）。

``level()`` 返回 ``1..4``；``DECAY_DROPPED = 0`` 是**哨兵**：已淡出、不进提示词——超过
``DECAY_DROP_AT``、事件时刻比当前手还新（脏值）、或参数根本转不出数字，都返回它（不抛）。

``bad_move_phrase()`` 是给下游（事件区块）用的**文案模板**（设计 §6.4 的四档，信息量递减）：
档 1 说「影响 + 后果」、档 2 只说影响、档 3 只说事实（第几手）、档 4 只留方位。缺数据就
**降级到信息量更少的那一档**，绝不把半句话拼出来；方位词按坐标核对，**核不上就不说方位**
（项目既有硬要求：四角词要求两轴都能定，正中的行 / 列凑不出四角词 ⇒ 不说）。
"""

from . import coords

DECAY_SPAN = [1, 3, 6]      # 每档的累计上界（手）：权重只决定「多少步」
DECAY_DROP_AT = 12          # 超过它就不再进提示词（事件仍留在事件表里）
DECAY_DROPPED = 0           # 哨兵：已淡出，不进提示词

_CORNERS = {(-1, -1): "左上角", (1, -1): "右上角", (-1, 1): "左下角", (1, 1): "右下角"}


def _as_int(v):
    """转 int；转不出来（``None`` / 脏串 / bool）返回 ``None``，由调用方决定怎么降级。"""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def level(event_move_no, now_move_no) -> int:
    """事件相对当前手的淡化档位：``1..4``，``DECAY_DROPPED``(0) ＝ 不进提示词。"""
    ev = _as_int(event_move_no)
    now = _as_int(now_move_no)
    if ev is None or now is None:               # 说不清时刻：按已淡出处理
        return DECAY_DROPPED
    d = now - ev
    if d < 0:                                   # 事件比当前手还新：脏值，不抛
        return DECAY_DROPPED
    for i, span in enumerate(DECAY_SPAN):
        if d <= span:
            return i + 1
    if d <= DECAY_DROP_AT:
        return len(DECAY_SPAN) + 1              # 第 4 档
    return DECAY_DROPPED


def corner_word(x, y):
    """由坐标推四角方位词（「左上角」…）；两轴都定不下来时返回空串（**不说方位**）。"""
    xi = _as_int(x)
    yi = _as_int(y)
    if xi is None or yi is None or not (0 <= xi < coords.SIZE and 0 <= yi < coords.SIZE):
        return ""
    mid = coords.SIZE // 2                      # 19 ⇒ 9
    sx = -1 if xi < mid else (1 if xi > mid else 0)
    sy = -1 if yi < mid else (1 if yi > mid else 0)
    if sx == 0 or sy == 0:                      # 正中那一列 / 那一行：凑不出四角词
        return ""
    return _CORNERS[(sx, sy)]


def bad_move_phrase(lv, *, move_no=None, impact="", consequence="", x=None, y=None) -> str:
    """按档位渲染「我下错了」那句（设计 §6.4 的四档模板）；``DECAY_DROPPED`` ⇒ 空串。"""
    lv = _as_int(lv) or 0
    if lv <= 0:
        return ""
    n = _as_int(move_no)
    impact = str(impact or "").strip()
    consequence = str(consequence or "").strip()
    if lv == 1 and n is not None and impact and consequence:
        return "我在 %d 手下错了，影响了 %s 导致 %s，我要进行微量的情绪平衡来调整" % (
            n, impact, consequence)
    if lv <= 2 and n is not None and impact:
        return "我在 %d 手下错了，目前因为那一步 %s" % (n, impact)
    if lv <= 3 and n is not None:
        return "我在 %d 手下错了一手" % n
    where = corner_word(x, y)
    if lv >= 4 and where:                       # 档 4 的正解：只留方位
        return "我在%s下错过一手" % where
    if n is not None:                           # 档 4 但方位核不上 ⇒ 退回「第几手」这句事实
        return "我在 %d 手下错了一手" % n
    if where:
        return "我在%s下错过一手" % where
    return "我下错过一手"
