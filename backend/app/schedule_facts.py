"""日程事实（v4.3.0，E）：把「今天 / 明天 / 周五 / 下周 / 晚上 / 八点」变成真实时间窗，
再挑出对应日程，供聊天上下文与出话后的冲突核对共用。

这一层是**纯逻辑**：只接收 ``life.list_schedule`` 的行、当前时间与一句文本，不读库、不调模型。
分开写的原因（结构与逻辑分明）：

- ``time_ref``：自然语言 → 时间窗（只认常见说法，认不出就不猜）；
- ``select``：时间窗 → 真实条目 + 可注入文本（窗口内没有安排时给一句「没有安排」）；
- ``conflict``：已生成台词 → 与真实条目的冲突（只在**否定式日程判断**上触发，避免误杀闲聊）；
- ``correction_*``：把冲突条目变成给模型的一句事实 / 确定性兜底句。

中文时间说法宁可少认：认不出时退回原来的「未来 48 小时」，不把「下次」「有时候」当日期。
"""

import datetime
import math
import re

DAYS_CN = "一二三四五六日"
_WEEK_RE = re.compile(r"(下周|下个星期|下星期|本周|这周|周|星期|礼拜)([一二三四五六日天])")
_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
_HOUR_RE = re.compile(r"(\d{1,2})[点:：](\d{1,2})?")
_CN_HOUR_RE = re.compile(r"([零一二三四五六七八九十两]{1,3})[点時时](半|([零一二三四五六七八九十]{1,3})分)?")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_EARLY_RE = re.compile(r"(早八|早起|早上|早晨|清晨|上午)")
_NOON_RE = re.compile(r"(中午|午饭|午休)")
_AFTERNOON_RE = re.compile(r"(下午)")
_EVENING_RE = re.compile(r"(晚上|晚上好|晚饭|夜里|今晚|夜间)")

# 出话后的冲突判据：只管「否定式日程判断」，不碰普通情绪句
_NEG_RE = re.compile(
    r"(不用|不必|不需要|无需|没有|没|不|别)"
    r"(用|要|必|需要)?"
    r"(赶|上|去|做|开|参加|安排|忙|事|课|早起|早八|上班)")
_FREE_RE = re.compile(r"(有空|没事|闲着|不忙|睡懒觉|自然醒|休息)")
_SCHEDULE_WORDS = re.compile(
    r"(今天|今日|明天|明日|后天|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"下周|本周|这周|早上|上午|中午|下午|晚上|今晚|早八|早起|课|上班|开会|安排|日程|有空|没事)")

# 「没有课」这类否定只和课程类条目比，不能把晚上的电影 / 聚会也算成“有课”。
_CLASS_TITLE_WORDS = (
    "化学", "数学", "物理", "英语", "语文", "历史", "地理", "生物", "政治",
    "导论", "图学", "体育", "实验", "原理", "概论", "课", "讲座", "考试",
)


def _class_like(item) -> bool:
    """条目是否像课程（kind=course 或标题含学科词）；拿不准按“不是课”处理。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("kind", "") or "") == "course":
        return True
    title = str(item.get("title", "") or "")
    return any(w in title for w in _CLASS_TITLE_WORDS)


def _as_ts(v):
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f) or f <= 0.0:
        return None
    return f


def _cn_number(s) -> int | None:
    """中文数字（0~23）→ int：「八」=8、「十一」=11、「二十三」=23；认不出返回 ``None``。"""
    t = str(s or "").strip()
    if t == "":
        return None
    if "十" in t:
        left, right = t.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(t)


def _day_label(d: datetime.date, today: datetime.date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return "周" + DAYS_CN[d.weekday()]


def _at(day: datetime.date, hour: int, minute: int = 0) -> float:
    return datetime.datetime.combine(day, datetime.time(hour, minute)).timestamp()


def time_ref(text, now=None):
    """自然语言 → 时间窗 ``{start,end,label,day,period,hour}``；认不出返回 ``None``。

    支持：今天 / 明天 / 后天 / 大后天、本周X / 周X / 下周X、M月D日、
    早上 / 上午 / 中午 / 下午 / 晚上 / 今晚、以及「八点」这类整点。
    没有日期、只有时段（「晚上吃什么」）也**不**当成今天：宁可退回 48 小时默认窗口。
    """
    s = str(text or "")
    if s.strip() == "":
        return None
    now_ts = float(now) if now is not None else datetime.datetime.now().timestamp()
    now_dt = datetime.datetime.fromtimestamp(now_ts)
    today = now_dt.date()
    day = None
    day_label = ""

    m_date = _DATE_RE.search(s)
    if m_date:
        try:
            month, dom = int(m_date.group(1)), int(m_date.group(2))
            candidate = datetime.date(today.year, month, dom)
            if candidate < today - datetime.timedelta(days=1):
                candidate = datetime.date(today.year + 1, month, dom)
            day = candidate
            day_label = "%d月%d日" % (month, dom)
        except ValueError:
            day = None

    if day is None:
        if "大后天" in s:
            day, day_label = today + datetime.timedelta(days=3), "大后天"
        elif "后天" in s:
            day, day_label = today + datetime.timedelta(days=2), "后天"
        elif "明天" in s or "明日" in s or "明儿" in s:
            day, day_label = today + datetime.timedelta(days=1), "明天"
        elif "今天" in s or "今日" in s or "今晚" in s:
            day, day_label = today, "今天"

    if day is None:
        m_week = _WEEK_RE.search(s)
        if m_week:
            prefix, wd = m_week.group(1), m_week.group(2)
            idx = DAYS_CN.find("日" if wd == "天" else wd)
            if idx >= 0:
                monday = today - datetime.timedelta(days=today.weekday())
                target = monday + datetime.timedelta(days=idx)
                if prefix.startswith("下"):
                    target += datetime.timedelta(days=7)
                elif prefix not in ("本周", "这周") and target < today:
                    target += datetime.timedelta(days=7)
                day = target
                day_label = _day_label(day, today)

    if day is None:
        return None

    period = ""
    if _EARLY_RE.search(s):
        period = "早上"
    elif _NOON_RE.search(s):
        period = "中午"
    elif _AFTERNOON_RE.search(s):
        period = "下午"
    elif _EVENING_RE.search(s):
        period = "晚上"

    hour = None
    mi = 0
    m_hour = _HOUR_RE.search(s)
    if m_hour:
        try:
            h = int(m_hour.group(1))
            mi = int(m_hour.group(2) or 0)
            if 0 <= h <= 23 and 0 <= mi <= 59:
                hour = h
        except ValueError:
            hour = None
    if hour is None:
        m_cn = _CN_HOUR_RE.search(s)
        if m_cn:
            prev = s[m_cn.start() - 1] if m_cn.start() > 0 else ""
            # 「一点」在中文里常是「有一点 / 快一点」，没有「半 / 分」时不当成 1 点；
            # 前面是这些程度词时也不是时刻（「明天晚一点」「稍微早一点」）。
            vague = (m_cn.group(1) == "一" and not m_cn.group(2) and not m_cn.group(3))
            if prev not in "快慢早晚稍再等好多大少小有" and not vague:
                h = _cn_number(m_cn.group(1))
                if h is not None and 0 <= h <= 23:
                    hour = h
                    if m_cn.group(2) == "半":
                        mi = 30
                    elif m_cn.group(3):
                        mm = _cn_number(m_cn.group(3))
                        mi = mm if mm is not None and 0 <= mm <= 59 else 0

    if hour is not None:
        start_h, end_h = hour, hour + 1
        start_m, end_m = mi, mi
    elif period == "早上":
        start_h, end_h, start_m, end_m = 5, 12, 0, 0
    elif period == "中午":
        start_h, end_h, start_m, end_m = 11, 14, 0, 0
    elif period == "下午":
        start_h, end_h, start_m, end_m = 12, 18, 0, 0
    elif period == "晚上":
        start_h, end_h, start_m, end_m = 17, 24, 0, 0
    else:
        start_h, end_h, start_m, end_m = 0, 24, 0, 0

    start = _at(day, start_h, start_m)
    if end_h >= 24:
        end = _at(day + datetime.timedelta(days=1), 0, end_m)
    else:
        end = _at(day, end_h, end_m)
    label = day_label + (period if period else "")
    return {"start": start, "end": end, "label": label,
            "day": day.isoformat(), "period": period, "hour": hour}


def normalize(rows, limit=200):
    """日程行 → 统一形状（只留 planned、有标题、有开始时间）；脏行静默跳过。"""
    out = []
    for r in list(rows or [])[:int(limit or 200)]:
        if not isinstance(r, dict):
            continue
        if str(r.get("status", "planned") or "planned") != "planned":
            continue
        start = _as_ts(r.get("start_ts"))
        if start is None:
            continue
        title = str(r.get("title", "") or "").strip()
        if title == "":
            continue
        out.append(dict(r, start_ts=start, title=title))
    out.sort(key=lambda x: x["start_ts"])
    return out


def select(rows, text="", now=None, default_hours=48, limit=8):
    """按文本时间窗（认不出→未来 48 小时）挑条目，返回 ``{rows, ref, text}``。

    - 有明确时间窗且窗口内没有条目：``text`` 是一句「该时段没有安排」，让模型别编；
    - 没有明确时间窗且 48 小时内没有条目：``text`` 为空串（不新增提示词段落）；
    - 文本里的条目：``- 明天（周五）08:00 有机化学``。
    """
    now_ts = float(now) if now is not None else datetime.datetime.now().timestamp()
    ref = time_ref(text, now_ts)
    items = normalize(rows)
    if ref is not None:
        picked = [it for it in items if ref["start"] <= it["start_ts"] < ref["end"]]
        if not picked:
            return {"rows": [], "ref": ref, "text": "（%s没有安排）" % ref["label"]}
    else:
        picked = [it for it in items if it["start_ts"] >= now_ts - 60.0
                  and it["start_ts"] <= now_ts + float(default_hours) * 3600.0]
    picked = picked[:max(0, int(limit or 8))]
    return {"rows": picked, "ref": ref, "text": format_lines(picked, now_ts)}


def format_lines(items, now=None):
    """日程行 → 给模型看的短行（最多 8 行，避免撑爆上下文）。"""
    now_ts = float(now) if now is not None else datetime.datetime.now().timestamp()
    today = datetime.datetime.fromtimestamp(now_ts).date()
    lines = []
    for it in list(items or [])[:8]:
        d = datetime.datetime.fromtimestamp(float(it["start_ts"]))
        label = _day_label(d.date(), today)
        clock = str(it.get("start_time", "") or "").strip() or d.strftime("%H:%M")
        lines.append("- %s（周%s）%s %s" % (label, DAYS_CN[d.weekday()], clock, it["title"]))
    return "\n".join(lines)


def fact_line(items) -> str:
    """冲突核对时给模型的一句事实（取最早的一条，不念全部）。"""
    its = list(items or [])
    if not its:
        return ""
    it = its[0]
    d = datetime.datetime.fromtimestamp(float(it["start_ts"]))
    today = datetime.datetime.now().date()
    clock = str(it.get("start_time", "") or "").strip() or d.strftime("%H:%M")
    return "%s %s 有%s" % (_day_label(d.date(), today), clock, it["title"])


def _bucket(text) -> str:
    if _EARLY_RE.search(text):
        return "morning"
    if _NOON_RE.search(text):
        return "noon"
    if _AFTERNOON_RE.search(text):
        return "afternoon"
    if _EVENING_RE.search(text):
        return "evening"
    return ""


def _clock_hour(item) -> int:
    d = datetime.datetime.fromtimestamp(float(item["start_ts"]))
    clock = str(item.get("start_time", "") or "").strip()
    m = re.match(r"(\d{1,2})[:：](\d{1,2})", clock)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return d.hour


def conflict(reply, items, ref=None):
    """台词是否和真实日程冲突；有冲突返回那条日程（dict），否则 ``None``。

    只在「否定式日程判断」上触发：
    - 「明天不用赶早八」+ 明早有 08:00 的课 ⇒ 冲突；
    - 「下午没事」+ 14:00 有约定 ⇒ 冲突；
    - 「明天没有课」+ 明天有条目 ⇒ 冲突；
    - 「今天有点累」「我不太喜欢吃早餐」⇒ 不触发（没有日程判断 / 不是时间话）。
    """
    s = str(reply or "")
    if s == "" or not items:
        return None
    if not _SCHEDULE_WORDS.search(s):
        return None
    if not (_NEG_RE.search(s) or _FREE_RE.search(s)):
        return None
    bucket = _bucket(s)
    class_claim = ("课" in s) and bucket == ""
    for it in items:
        if class_claim and not _class_like(it):
            continue
        h = _clock_hour(it)
        if bucket == "morning" and h >= 12:
            continue
        if bucket == "noon" and not (11 <= h < 14):
            continue
        if bucket == "afternoon" and not (12 <= h < 18):
            continue
        if bucket == "evening" and h < 17:
            continue
        return it
    return None


def correction_prompt(item, ref=None) -> str:
    """给模型的纠正说明（只给事实，不规定具体措辞）。"""
    fact = fact_line([item]) if item else ""
    if fact == "":
        return ""
    return ("（事实核对：真实日程里%s。你刚才那句和它对不上，请按这个事实自然重说一句，"
            "保留你原来的语气；不要提日程表 / 系统 / 纠正。）" % fact)


def correction_sentence(item, ref=None) -> str:
    """重答仍冲突 / 超时时的确定性兜底句（不会编错，也不生硬念日程）。"""
    if not isinstance(item, dict):
        return ""
    fact = fact_line([item])
    if fact == "":
        return ""
    try:
        h = _clock_hour(item)
    except Exception:                       # noqa: BLE001
        h = 12
    tail = "早起还是躲不掉" if h < 10 else "时间还是排上了"
    return "对了，我刚看了一眼日程——%s，%s。" % (fact, tail)
