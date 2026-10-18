"""时间事实（v3.9.0，设计 §5-H / §6.3）：**纯函数、零 IO、零调用、时钟可注入**。

``facts(...)`` 把「当前时间 / 本局时长 / 单手用时 / 距上次对局的间隔 / 连续下棋天数」算成一个
dict，交给 ``prompts.time_note`` 渲染成提示词里的**时间块**（插在事件区块之后、画像块之前、
铁律段之前）。采集点在后端：``POST /api/move`` 到达时打一个时间戳（``agent.mark_move_time``），
下一手到达时结算——**不要求前端配合**。

三条不能回退的口径：

① **时钟可注入**——``now`` 由调用方给（生产传 ``time.time()``），本模块**不读系统时钟**；
   跨日 / 跨时区边界要能钉死，所以日历日一律按**本地时区**切（``time.localtime``）；
② **缺什么就不产出什么**——``now`` 脏、``session_start`` 缺失、只有 1 个落子时刻
   （**首手无前一手 ⇒ 不计**）、``game_ts_list`` 缺省（``None``）……对应字段**直接不产出**，
   绝不瞎猜一个 0；
③ **脏值不抛**——非数值 / 非正 / 未来时间戳一律忽略（相应字段不产出），绝不因为一个坏时间戳
   把一回合打挂。

**连续天数的判据（控制者钉死口径的落地）**：只吃 ``game_ts_list``（历史对局的**结束时刻**，
来自 ``ProfileStore.games(n=…)`` 的 ``ts`` 列）——先按**本地日历日**去重，再从今天往回数连续
有对局的天数。**今天还没下完不算断**：今天没有已结束的对局时，锚点退到昨天（你可能正在下这
一局）；连昨天也没有 ⇒ 断了，返回 ``0``（渲染侧对 ``<= 0`` 不开口）。``game_ts_list=None``
（没给历史）⇒ 该字段**不产出**；给了空列表 ⇒ ``0``（诚实：一局都没有）；给了元素但全是脏值
⇒ 判不出来，同样**不产出**。
"""

import datetime
import time

# 星期文案（``time.localtime().tm_wday``：0=周一 … 6=周日）
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

SEC_PER_DAY = 86400.0


def _as_ts(v):
    """转成「正的墙钟秒」；``bool`` / 非数值 / NaN / 非正一律 ``None``（脏值不抛）。"""
    if isinstance(v, bool):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t != t or t <= 0.0:              # NaN / 非正
        return None
    return t


def _local_day(ts) -> datetime.date:
    """墙钟秒 → **本地时区**的日历日（跨时区 / 跨日边界都按本地切）。"""
    lt = time.localtime(ts)
    return datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)


def _avg_gap(move_ts_list, t_now):
    """单手用时均值（秒）：相邻两个「玩家这一手到达」时刻之差取平均；算不出来返回 ``None``。

    - **首手无前一手 ⇒ 不计**（有效时刻少于 2 个就不产出这个字段）；
    - 未来 / 非数值 / 非正的时刻**逐条忽略**（不因为一条脏值丢掉整份均值）；
    - 差值为非正（时刻乱序）的那一对丢掉；一对都不剩 ⇒ ``None``。
    """
    if not isinstance(move_ts_list, (list, tuple)):
        return None
    ts = []
    for v in move_ts_list:
        t = _as_ts(v)
        if t is not None and t <= t_now:
            ts.append(t)
    if len(ts) < 2:
        return None
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0.0]
    if not gaps:
        return None
    return sum(gaps) / float(len(gaps))


def _streak(game_ts_list, t_now):
    """连续下棋天数（含今天）；判据见模块 docstring；判不出来返回 ``None``（＝不产出）。"""
    if game_ts_list is None:
        return None                     # 没给历史对局时刻：不产出（不瞎猜 0）
    if not isinstance(game_ts_list, (list, tuple)):
        return None
    if len(game_ts_list) == 0:
        return 0                        # 给了、但一局都没有：连续 0 天
    days = set()
    for v in game_ts_list:
        t = _as_ts(v)
        if t is None or t > t_now:      # 未来时间戳：忽略
            continue
        days.add(_local_day(t))
    if not days:
        return None                     # 有元素但全是脏值：判不出来
    anchor = _local_day(t_now)
    if anchor not in days:
        anchor -= datetime.timedelta(days=1)     # 今天还没下完不算断：从昨天往回数
        if anchor not in days:
            return 0                    # 昨天也没有 ⇒ 断了
    n = 0
    while anchor in days:
        n += 1
        anchor -= datetime.timedelta(days=1)
    return n


def facts(*, now, session_start=None, last_game_ts=None, move_ts_list=None,
          game_ts_list=None) -> dict:
    """算一份时间事实；``now`` 是**必给的**锚点（脏值 ⇒ 返回 ``{}``）。

    返回的字段（**缺信号就不产出该键**，调用方一律 ``dict.get`` 读）：

    | 键 | 含义 | 什么情况下不产出 |
    | --- | --- | --- |
    | ``now`` | 当前墙钟（epoch 秒） | ``now`` 脏（None / 非数值 / 非正）⇒ 整个 dict 是 ``{}`` |
    | ``date`` | 本地日历日 ``2026-10-19`` | 同上 |
    | ``weekday`` | 本地星期 ``周一``…``周日`` | 同上 |
    | ``clock`` | 本地时刻 ``21:03`` | 同上 |
    | ``session_sec`` | 本局时长（秒） | ``session_start`` 缺失 / 脏 / 晚于 ``now`` |
    | ``avg_move_sec`` | 单手用时均值（秒） | 有效落子时刻少于 2 个（**首手无前一手**）/ 差值全非正 |
    | ``last_gap_days`` | 距上次对局的间隔（天，float） | ``last_game_ts`` 缺失 / 脏 / 晚于 ``now`` |
    | ``streak_days`` | 连续下棋天数（含今天，int，可为 0） | ``game_ts_list`` 为 ``None`` / 全是脏值 |

    本函数是**纯函数**（O(时刻数) 的算术与比较），不读时钟、不碰盘、不调引擎 / LLM、不发事件。
    """
    t_now = _as_ts(now)
    if t_now is None:
        return {}
    out = {}
    lt = time.localtime(t_now)
    out["now"] = t_now
    out["date"] = "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
    out["weekday"] = WEEKDAY_CN[lt.tm_wday]
    out["clock"] = "%02d:%02d" % (lt.tm_hour, lt.tm_min)

    ss = _as_ts(session_start)
    if ss is not None and ss <= t_now:
        out["session_sec"] = t_now - ss

    avg = _avg_gap(move_ts_list, t_now)
    if avg is not None:
        out["avg_move_sec"] = avg

    lg = _as_ts(last_game_ts)
    if lg is not None and lg <= t_now:
        out["last_gap_days"] = (t_now - lg) / SEC_PER_DAY

    streak = _streak(game_ts_list, t_now)
    if streak is not None:
        out["streak_days"] = streak
    return out
