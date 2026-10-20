"""事件遗忘权重 → 4 档淡化（纯函数）。

分工（用户已定）：**权重只决定「在接下来的多少步淡化」，不决定淡化程度**——程度是固定的 4 档。
``DECAY_SPAN = [1, 3, 6]`` 是每档的**累计上界**（第 1 档到第 1 手、第 2 档到第 3 手、第 3 档到
第 6 手），第 4 档到 ``DECAY_DROP_AT = 12``；超过 12 手**不再进提示词**（事件仍留在表里）。

``level()`` 返回 ``1..4``；``DECAY_DROPPED = 0`` 是**哨兵**：已淡出、不进提示词——超过
``DECAY_DROP_AT``、事件时刻比当前手还新（脏值）、或参数根本转不出数字，都返回它（不抛）。

**事件表 × 悔棋的注入侧副作用（有意保留）**：事件表本身**不随悔棋回退**（「发生过就是发生过」，
见 ``event_log``），而悔棋会把当前手数往回缩 ⇒ 悔棋前写下的那些事件的 ``d = now - ev`` 暂时变成
负数，``level()`` 走 ``d < 0`` 那一支、**暂时不进提示词**；等手数重新长回那一手之上，它们又照常
按档位进区块。也就是「悔棋期间那些事她暂时不提，重走之后又会想起来」——存储一字未动，只在注入侧
随时间轴自然出现 / 消失。

``bad_move_phrase()`` 是**「下错了」那句的文案模板**（设计 §6.4 的四档，信息量递减），
生产调用点在 ``agent_graph._record_player_event``（``subject="他"`` 写 ``player_bad`` 事件）：
档 1 说「影响 + 后果」、档 2 只说影响、档 3 只说事实（第几手）、档 4 只留方位。缺数据就
**降级到信息量更少的那一档**，绝不把半句话拼出来；方位词按坐标核对，**核不上就不说方位**
（项目既有硬要求：方位词要求两轴都能定，正中的行 / 列凑不出方位 ⇒ 不说）。档 4 的方位词用
设计 §6.4 模板的**原词**（「左上 / 左下 / 右上 / 右下」，**不带「角」字**）——它不是「真角区」的
判定，而是「象限 + 方位」：``(8, 8)`` 也在上左，故返回「左上」。
"""

from . import coords

DECAY_SPAN = [1, 3, 6]      # 每档的累计上界（手）：权重只决定「多少步」
DECAY_DROP_AT = 12          # 超过它就不再进提示词（事件仍留在事件表里）
DECAY_DROPPED = 0           # 哨兵：已淡出，不进提示词

_CORNERS = {(-1, -1): "左上", (1, -1): "右上", (-1, 1): "左下", (1, 1): "右下"}


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
    """由坐标推方位词（「左上」…，与设计 §6.4 档 4 模板用词一致，**不带「角」字**）。

    它是「象限 + 方位」而非「真角区」判定：只要两轴都能定（不在正中那一列 / 那一行）就给词；
    核不上（越界 / 脏值 / 正中的行或列）返回空串——**不说方位**。
    """
    xi = _as_int(x)
    yi = _as_int(y)
    if xi is None or yi is None or not (0 <= xi < coords.SIZE and 0 <= yi < coords.SIZE):
        return ""
    mid = coords.SIZE // 2                      # 19 ⇒ 9
    sx = -1 if xi < mid else (1 if xi > mid else 0)
    sy = -1 if yi < mid else (1 if yi > mid else 0)
    if sx == 0 or sy == 0:                      # 正中那一列 / 那一行：凑不出方位词
        return ""
    return _CORNERS[(sx, sy)]


def bad_move_phrase(lv, *, subject="我", move_no=None, impact="", consequence="",
                    x=None, y=None) -> str:
    """按档位渲染「下错了」那句（设计 §6.4 的四档模板）；``DECAY_DROPPED`` ⇒ 空串。

    ``subject`` 是**主语**，默认 ``"我"``：Azure 自述口径，输出与既有**逐字符相同**（档 1 的收尾句
    「我要进行微量的情绪平衡来调整」是 Azure 自己的反应，故只在第一人称下出现）。传第三人称
    （``"他"``）时给 ``player_bad`` 事件用——**同一套档位与方位词判据**，只换主语、省去那句自述反应；
    玩家口径不能复用第一人称（否则「他下的坏手」会被写成 Azure 自己下的）。
    """
    lv = _as_int(lv) or 0
    if lv <= 0:
        return ""
    s = str(subject if subject is not None else "").strip() or "我"
    n = _as_int(move_no)
    impact = str(impact or "").strip()
    consequence = str(consequence or "").strip()
    if lv == 1 and n is not None and impact and consequence:
        head = "%s在 %d 手下错了，影响了 %s 导致 %s" % (s, n, impact, consequence)
        return head + "，我要进行微量的情绪平衡来调整" if s == "我" else head
    if lv <= 2 and n is not None and impact:
        return "%s在 %d 手下错了，目前因为那一步 %s" % (s, n, impact)
    if lv <= 3 and n is not None:
        return "%s在 %d 手下错了一手" % (s, n)
    where = corner_word(x, y)
    if lv >= 4 and where:                       # 档 4 的正解：只留方位
        return "%s在%s下错过一手" % (s, where)
    if n is not None:                           # 档 4 但方位核不上 ⇒ 退回「第几手」这句事实
        return "%s在 %d 手下错了一手" % (s, n)
    if where:
        return "%s在%s下错过一手" % (s, where)
    return "%s下错过一手" % s
