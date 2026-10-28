"""资料 → 日程 / 课程表：把 OCR / VLM / 文档文本交给 LLM 提取，再归一化成可持久化的条目。

设计边界：
- 只做**明确的日期 / 时间 / 约定 / 待办 / 课程**提取，不把普通闲聊编成日程；
- 课程表要保留**星期、节次、教师、地点**（前端据此画周课表），不能只压成一行文字；
- LLM 输出必须能落成 JSON 数组；解析失败返回空列表，不猜；
- 时间归一化在本地时区完成，支持「今天 / 明天 / 后天」、`YYYY-MM-DD`、`M月D日`、`HH:MM`；
- 没有日期的条目保留为「待定」，不硬塞到今天；没有时间的日期条目按「全天」存 09:00；
- 本模块不直接写库：调用方拿 ``normalize_items`` 的结果去 ``LifeStore.upsert_schedule``。
"""

import datetime
import json
import math
import re
import time

from . import schedule_learning

MAX_SOURCE_TEXT = 9000
MAX_ITEMS = 60
TITLE_MAX = 80
NOTES_MAX = 200
TEACHER_MAX = 40
LOCATION_MAX = 60
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WEEKDAYS = {"周一": 0, "星期二": 1, "周二": 1, "周三": 2, "星期四": 3,
             "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
             "星期一": 0, "礼拜一": 0, "礼拜二": 1, "礼拜三": 2,
             "礼拜四": 3, "礼拜五": 4, "礼拜六": 5, "礼拜天": 6}
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_EN_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
# —— 规则补抽（LLM 超时 / 漏掉聊天里的活动公告时兜底）——
_EVENT_WORDS = (
    "赛", "比赛", "活动", "讲座", "会议", "开会", "演出", "考试", "面试", "培训",
    "聚会", "典礼", "报名", "签到", "分享", "宣讲", "班会", "团建", "招聘",
    "双选", "志愿", "沙龙", "论坛",
)
_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～—–至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
_DATE_MD_RE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_LOCATION_RE = re.compile(r"(?:地点|场地|地址|位置)\s*[:：]\s*(.{1,40})")
_CLOCK_ONLY_RE = re.compile(r"^\s*\d{1,2}\s*[:：]\s*\d{2}\s*$")
_SENDER_RE = re.compile(
    r"^[\u4e00-\u9fa5]{1,10}(委员|老师|同学|学长|学姐|部长|主任|班长)$")


def _clip(text, limit):
    s = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _json_array(text):
    """从模型输出里尽量取出 JSON 数组；失败返回 ``[]``。"""
    raw = str(text or "").strip()
    if raw == "":
        return []
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    candidates = [raw]
    a, b = raw.find("["), raw.rfind("]")
    if a >= 0 and b > a:
        candidates.append(raw[a:b + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "schedule", "events", "data", "courses"):
                if isinstance(data.get(key), list):
                    return data[key]
    return []


def scan_text(scan):
    """把扫描器返回的 files[] 拼成一段给 LLM 的资料文本（限制总长）。"""
    parts = []
    for f in (scan or {}).get("files", []) if isinstance(scan, dict) else []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "") or "")
        pieces = []
        table = f.get("table")
        # 课程表已经由 OCR 坐标还原并在 main 里单独入库，**不要再喂给 LLM**：
        # 大表会挤占上下文、拖慢本地模型，甚至让同一批里的聊天通知被漏掉。
        if isinstance(table, list) and table and str(f.get("kind", "")) == "image":
            continue
        for key, label in (("text", "文档文本"), ("ocr", "OCR"), ("vlm", "VLM 描述")):
            val = str(f.get(key, "") or "").strip()
            if val:
                pieces.append("%s：%s" % (label, val))
        if pieces:
            parts.append("文件：%s\n%s" % (name, "\n".join(pieces)))
        elif f.get("error"):
            parts.append("文件：%s\n读取失败：%s" % (name, f.get("error")))
    return ("\n\n".join(parts))[:MAX_SOURCE_TEXT]


async def extract_items(llm, text, now=None):
    """调用 LLM 从资料文本里提取原始日程 / 课程条目；失败返回 ``[]``。"""
    if llm is None or str(text or "").strip() == "":
        return []
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    prompt = (
        "你是日程 / 课程表整理助手。下面是从文件里提取的 OCR / VLM / 文档文本。\n"
        "只提取**明确出现过**的日期、时间、课程、约定、会议、提醒、待办；没有就返回空数组。\n"
        "如果文本是课程表：按「星期 + 第几节 + 课程名 + 教师 + 地点」逐条输出；"
        "连续两节就写 period=\"1-2\"，不要拆成两条；date 用表头给出的真实日期。\n"
        "寒暄、表情、没有日期 / 时间 / 地点的普通闲聊不要提取；"
        "但群通知、公告、聊天记录里明确写出日期、时间、活动或地点的（如比赛、讲座、聚会、开会），"
        "即使截图来自聊天，也必须提取；不要编造日期、教师、地点、人物。\n"
        "今天是 %s（%s）。相对日期（今天/明天/后天/下周X）请换算成真实日期。\n"
        "只输出 JSON 数组，不要解释、不要代码围栏。字段：\n"
        '[{"date":"YYYY-MM-DD" 或 null, "weekday":"周一" 或 null, '
        '"period":"1-2" 或 null, "time":"HH:MM" 或 null, "end_time":"HH:MM" 或 null, '
        '"title":"课程或事项", "teacher":"可选", "location":"可选", "notes":"可选", '
        '"priority":"0-100 的整数（重要/紧急越高）", "duration_min":"预计分钟数", '
        '"deadline":"YYYY-MM-DD HH:MM 或 null"}]\n\n'
        "资料：\n%s"
    ) % (ref.strftime("%Y-%m-%d"),
         ("周" + "一二三四五六日"[ref.weekday()]),
         str(text))
    msgs = [{"role": "user", "content": prompt}]
    try:
        try:
            out = await llm.chat(msgs, 1200, think_mode=0)
        except TypeError:
            out = await llm.chat(msgs, 1200)
    except Exception as e:                  # noqa: BLE001 —— 日程提取绝不打挂端点
        print("[Schedule] LLM 提取失败（已忽略）：%s" % e)
        return []
    return _json_array(out)[:MAX_ITEMS]


def _event_lines(text):
    """OCR / 聊天文本 → 一行一条（OCR 常用 `` | `` 分隔）。"""
    s = str(text or "").replace("｜", "|")
    s = re.sub(r"\s*\|\s*", "\n", s)
    return [ln.strip() for ln in s.splitlines() if ln.strip()]


def _event_date(text, ref):
    """公告文本里的日期：优先月/日，其次今天/明天/后天与星期。"""
    m = _DATE_MD_RE.search(text)
    if m:
        year = int(m.group(1)) if m.group(1) else ref.year
        try:
            d = datetime.date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        # 没写年份且已经过去很久：按明年的同一天理解（避免 1 月的公告被排到去年）。
        if not m.group(1) and d < ref.date() - datetime.timedelta(days=60):
            try:
                d = d.replace(year=ref.year + 1)
            except ValueError:
                pass
        return d
    for word, delta in (("今天", 0), ("明天", 1), ("后天", 2)):
        if word in text:
            return ref.date() + datetime.timedelta(days=delta)
    for word, wd in _WEEKDAYS.items():
        if word in text:
            add = (wd - ref.weekday()) % 7
            return ref.date() + datetime.timedelta(days=add)
    return None


def _event_time_range(line):
    """``中午12：00-1：00`` → (开始分钟, 结束分钟, match)；结束早于开始按跨 12 小时补。"""
    m = _TIME_RANGE_RE.search(str(line or ""))
    if not m:
        return None
    try:
        sh, sm, eh, em = (int(m.group(i)) for i in range(1, 5))
    except (TypeError, ValueError):
        return None
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    start = sh * 60 + sm
    end = eh * 60 + em
    ctx = str(line)[max(0, m.start() - 4):m.start()]
    if any(x in ctx for x in ("下午", "傍晚", "晚上", "夜里")) and start < 12 * 60:
        start += 12 * 60
    elif "中午" in ctx and start < 11 * 60:
        start += 12 * 60
    guard = 0
    while end <= start and guard < 3:
        end += 12 * 60
        guard += 1
    if end - start > 24 * 60:
        return None
    return start, end, m


def _event_noise_line(line):
    s = str(line or "").strip()
    if s == "" or _CLOCK_ONLY_RE.match(s):
        return True
    if len(s) <= 16 and re.search(r"(委员|老师|同学|学长|学姐|部长|主任|班长)", s):
        return True
    if _LOCATION_RE.search(s):
        return True
    t = _DATE_MD_RE.sub("", s)
    for w in _WEEKDAYS:
        t = t.replace(w, "")
    t = re.sub(r"[\s()（）【】\[\]·,，.。:：\-—/]", "", t)
    return t == ""


def _event_from_text(text, ref):
    """从聊天公告 / OCR 文本里规则抽一条明确活动；抽不出返回 None。"""
    lines = _event_lines(text)
    if not lines:
        return None
    time_idx = -1
    tr = None
    for i, line in enumerate(lines):
        got = _event_time_range(line)
        if got is not None:
            time_idx = i
            tr = got
            break
    if tr is None:
        return None
    date = _event_date(text, ref)
    if date is None:
        return None

    title = ""
    title_idx = -1
    for i in range(time_idx - 1, -1, -1):
        if _event_noise_line(lines[i]):
            continue
        cand = _DATE_MD_RE.sub("", lines[i])
        for w in _WEEKDAYS:
            cand = cand.replace(w, "")
        cand = _TIME_RANGE_RE.sub("", cand)
        cand = re.sub(r"^[\s\-–—·•]+|[\s\-–—·•]+$", "", cand).strip()
        if 2 <= len(cand) <= TITLE_MAX:
            title = cand
            title_idx = i
            break
    if title == "":
        return None
    if not (any(w in title for w in _EVENT_WORDS)
            or any(k in text for k in ("签到", "报名", "举行", "举办", "集合"))):
        return None

    location = ""
    for line in lines:
        m = _LOCATION_RE.search(line)
        if m:
            location = _clip(m.group(1).strip(" .,;，。"), LOCATION_MAX)
            break

    notes_parts = []
    for i, line in enumerate(lines):
        if i == time_idx or i == title_idx or _event_noise_line(line):
            continue
        notes_parts.append(line)
    notes = _clip(" | ".join(notes_parts), NOTES_MAX)
    start, end, _m = tr
    duration = max(5, min(24 * 60, end - start))

    def _clock(minutes):
        minutes %= 24 * 60
        return "%02d:%02d" % (minutes // 60, minutes % 60)

    return {
        "date": date.isoformat(),
        "weekday": None,
        "period": None,
        "time": _clock(start),
        "end_time": _clock(end),
        "title": title,
        "location": location,
        "notes": notes,
        "priority": 50,
        "duration_min": duration,
        "deadline": None,
        "source": "",
    }


def fallback_event_items(scan, now=None):
    """规则补抽：LLM 失败 / 漏掉聊天里的活动公告时，至少把明确的日期+时间活动留下。"""
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    out, seen = [], set()
    for f in (scan or {}).get("files", []) if isinstance(scan, dict) else []:
        if not isinstance(f, dict) or f.get("table"):
            continue
        text = "\n".join(str(f.get(k, "") or "").strip()
                         for k in ("text", "ocr", "vlm") if str(f.get(k, "") or "").strip())
        if text == "":
            continue
        item = _event_from_text(text, ref)
        if not item:
            continue
        key = (item.get("date"), item.get("time"), _norm_title(item.get("title")))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def _raw_same_event(a, b):
    ta, tb = _norm_title(a.get("title", "")), _norm_title(b.get("title", ""))
    ad, bd = str(a.get("date") or ""), str(b.get("date") or "")
    if ta and tb and (ta == tb or ta in tb or tb in ta):
        return not ad or not bd or ad == bd
    at, bt = _parse_time(a.get("time")), _parse_time(b.get("time"))
    if at is not None and bt is not None and \
            abs(at.hour * 60 + at.minute - bt.hour * 60 - bt.minute) <= 5:
        return (not ad or not bd or ad == bd) and (not ta or not tb)
    return False


def merge_event_fallback(raw_items, fallback_items):
    """把规则补抽结果并入 LLM 结果；同日期 + 同标题 / 同时刻的不重复添加。"""
    out = [dict(x) for x in raw_items or [] if isinstance(x, dict)]
    for fb in fallback_items or []:
        if not isinstance(fb, dict):
            continue
        if any(_raw_same_event(x, fb) for x in out):
            continue
        out.append(dict(fb))
        if len(out) >= MAX_ITEMS:
            break
    return out


def _int_cn(v):
    s = str(v or "").strip()
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    if s in _CN_NUM:
        return _CN_NUM[s]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_NUM:
        return 10 + _CN_NUM[s[1]]
    if len(s) == 2 and s[0] in _CN_NUM and s[1] == "十":
        return _CN_NUM[s[0]] * 10
    if len(s) == 3 and s[0] in _CN_NUM and s[1] == "十" and s[2] in _CN_NUM:
        return _CN_NUM[s[0]] * 10 + _CN_NUM[s[2]]
    return None


def _parse_weekday(raw):
    """``周一`` / ``星期一`` / ``Monday`` / 0-6 → 0-6；认不出返回 ``None``。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            i = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return i if 0 <= i <= 6 else None
    s = str(raw).strip().lower()
    if s == "" or s in ("null", "none", "无", "待定"):
        return None
    if s.isdigit():
        i = int(s)
        return i if 0 <= i <= 6 else None
    if s in _EN_WEEKDAYS:
        return _EN_WEEKDAYS[s]
    for word, wd in _WEEKDAYS.items():
        if word in s:
            return wd
    return None


def _parse_period(raw):
    """``1-2`` / ``第1-2节`` / ``1,2`` → ``(start, end)``；认不出返回 ``None``。"""
    if isinstance(raw, (list, tuple)):
        nums = [int(x) for x in raw if str(x).strip().isdigit()]
    else:
        nums = [int(x) for x in re.findall(r"\d{1,2}", str(raw or ""))]
    if not nums:
        return None
    start = nums[0]
    end = nums[1] if len(nums) > 1 else start
    if start < 1 or start > 20:
        return None
    end = max(start, min(end, 20))
    if end - start > 6:
        end = start
    return start, end


def _parse_date(raw, ref):
    s = str(raw or "").strip()
    if s == "" or s.lower() in ("null", "none", "无", "待定"):
        return None
    today = ref.date()
    if "今天" in s or "今日" in s:
        return today
    if "后天" in s:
        return today + datetime.timedelta(days=2)
    if "明天" in s or "明日" in s:
        return today + datetime.timedelta(days=1)
    for word, wd in _WEEKDAYS.items():
        if word in s:
            add = (wd - today.weekday()) % 7
            if add == 0:
                add = 7
            return today + datetime.timedelta(days=add)
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:
        try:
            d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today - datetime.timedelta(days=180):
                d = d.replace(year=today.year + 1)
            return d
        except ValueError:
            return None
    return None


def _parse_time(raw):
    s = str(raw or "").strip()
    if s == "" or s.lower() in ("null", "none", "无", "待定"):
        return None
    m = re.search(r"(上午|早上|下午|傍晚|晚上|夜里|凌晨)?\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?", s)
    if not m:
        return None
    h = int(m.group(2))
    minute = int(m.group(3) or 0)
    period = m.group(1) or ""
    if period in ("下午", "傍晚", "晚上", "夜里") and h < 12:
        h += 12
    if period == "凌晨" and h == 12:
        h = 0
    if h > 23 or minute > 59:
        return None
    return datetime.time(h, minute)


def _parse_priority(raw):
    """优先级 → 0-100（越高越重要）；支持数字 / 高·中·低 / 紧急·重要·普通。"""
    if isinstance(raw, bool):
        return 50
    if isinstance(raw, (int, float)):
        try:
            return max(0, min(100, int(round(float(raw)))))
        except (TypeError, ValueError, OverflowError):
            return 50
    s = str(raw or "").strip().lower()
    if s == "" or s in ("null", "none", "无", "待定"):
        return 50
    if s.isdigit():
        return max(0, min(100, int(s)))
    if any(w in s for w in ("紧急", "立刻", "马上", "最高", "critical", "urgent")):
        return 90
    if any(w in s for w in ("高", "重要", "high", "must")):
        return 80
    if any(w in s for w in ("低", "不急", "low", "someday")):
        return 20
    if any(w in s for w in ("中", "普通", "一般", "medium", "normal")):
        return 50
    return 50


def _parse_duration(raw, default=60):
    """预计时长 → 分钟（5~1440）；支持数字 / "30分钟" / "1.5小时"。"""
    if isinstance(raw, bool):
        return int(default)
    if isinstance(raw, (int, float)):
        try:
            v = float(raw)
        except (TypeError, ValueError, OverflowError):
            return int(default)
        return max(5, min(1440, int(round(v))))
    s = str(raw or "").strip().lower()
    if s == "" or s in ("null", "none", "无", "待定"):
        return int(default)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return int(default)
    try:
        v = float(nums[0])
    except ValueError:
        return int(default)
    if any(w in s for w in ("小时", "钟头", "hour", "h")) and "分钟" not in s and "min" not in s:
        v *= 60.0
    return max(5, min(1440, int(round(v))))


def _parse_deadline(raw, ref):
    """截止时间 → 墙钟秒；支持时间戳 / 日期 / "10月28日 18:00"；没有返回 0。"""
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        try:
            v = float(raw)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return v if math.isfinite(v) and v > 0.0 else 0.0
    s = str(raw or "").strip()
    if s == "" or s.lower() in ("null", "none", "无", "待定"):
        return 0.0
    d = _parse_date(s, ref)
    if d is None:
        return 0.0
    tm = _parse_time(s)
    if tm is None:
        tm = datetime.time(23, 59)
    try:
        return datetime.datetime.combine(d, tm).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def _parse_item(raw, ref, source):
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title", raw.get("name", raw.get("summary", ""))), TITLE_MAX)
    if title == "":
        return None
    notes = _clip(raw.get("notes", raw.get("note", raw.get("details", ""))), NOTES_MAX)
    teacher = _clip(raw.get("teacher", raw.get("instructor", "")), TEACHER_MAX)
    location = _clip(raw.get("location", raw.get("place", raw.get("room", ""))), LOCATION_MAX)
    weekday = _parse_weekday(raw.get("weekday", raw.get("day_of_week", raw.get("week"))))
    period = _parse_period(raw.get("period", raw.get("periods", raw.get("sections"))))
    date = _parse_date(raw.get("date", raw.get("day", raw.get("when_date", ""))), ref)
    raw_time = str(raw.get("time", raw.get("at", raw.get("start_time", raw.get("start", "")))) or "")
    tm = _parse_time(raw_time)
    end_tm = _parse_time(raw.get("end_time", raw.get("end", "")))
    if end_tm is None:
        # 模型有时把时间写成 "08:00-09:35"：拆开保留结束时间（周课表要显示时间跨度）。
        parts = [x for x in re.split(r"[-~～—–]", raw_time) if x.strip()]
        if len(parts) >= 2:
            end_tm = _parse_time(parts[1])

    if date is None and weekday is not None:
        add = (weekday - ref.weekday()) % 7
        date = ref.date() + datetime.timedelta(days=add)
        if add == 0 and tm is not None and datetime.datetime.combine(date, tm) <= ref:
            date = date + datetime.timedelta(days=7)
    if date is None and tm is not None and period is None and weekday is None:
        date = ref.date()
        if datetime.datetime.combine(date, tm) <= ref:
            date = date + datetime.timedelta(days=1)
    # 有日期 + 节次但没写星期：直接由日期推星期，前端才能把课程放进周课表的正确列。
    if weekday is None and date is not None and period is not None:
        weekday = date.weekday()

    raw_kind = str(raw.get("kind", "") or "").strip().lower()
    # 只有「明确课程 / 有节次」才是课程；仅有星期的时间型事件是普通约定（如周四篮球赛）。
    kind = "course" if raw_kind == "course" or period is not None else "task"
    task_type = str(raw.get("task_type", raw.get("type", "")) or "").strip().lower()
    if task_type not in schedule_learning.CATEGORY_PRIOR:
        task_type = schedule_learning.infer_task_type(title, kind, notes)
    priority = _parse_priority(raw.get("priority", raw.get("importance", raw.get("urgent"))))
    duration = _parse_duration(raw.get("duration_min", raw.get("duration", raw.get("minutes"))))
    deadline_ts = _parse_deadline(raw.get("deadline", raw.get("due", raw.get("due_at"))), ref)
    # 只有日期、没有时刻的普通任务是「全天截止」：自动排班可以在截止前安排，
    # 但不应把它当成 09:00 的硬约定。
    if kind == "task" and date is not None and tm is None and deadline_ts <= 0.0:
        try:
            deadline_ts = datetime.datetime.combine(date, datetime.time(23, 59)).timestamp()
        except (ValueError, OverflowError):
            deadline_ts = 0.0
    if kind == "course":
        if tm is not None and end_tm is not None:
            try:
                delta = (datetime.datetime.combine(date or ref.date(), end_tm)
                         - datetime.datetime.combine(date or ref.date(), tm)).total_seconds() / 60.0
                if delta > 0:
                    duration = max(5, int(round(delta)))
            except (ValueError, OverflowError):
                pass
        elif period is not None:
            duration = max(duration, (period[1] - period[0] + 1) * 45)

    when_text = "待定"
    start_ts = 0.0
    date_text = ""
    if date is not None:
        date_text = date.isoformat()
    if date is not None and tm is not None:
        start_ts = datetime.datetime.combine(date, tm).timestamp()
    elif date is not None and period is not None:
        # 只知节次：用当天上午九点做排序锚点，真正的展示用 period 字段。
        start_ts = datetime.datetime.combine(date, datetime.time(9, 0)).timestamp()
    elif date is not None:
        start_ts = datetime.datetime.combine(date, datetime.time(9, 0)).timestamp()

    bits = []
    if date_text:
        bits.append(date_text)
    if weekday is not None:
        bits.append(_WEEKDAY_NAMES[weekday])
    if period is not None:
        p0, p1 = period
        bits.append("第%d-%d节" % (p0, p1) if p1 > p0 else "第%d节" % p0)
    if tm is not None:
        t = tm.strftime("%H:%M")
        if end_tm is not None:
            t += "-" + end_tm.strftime("%H:%M")
        bits.append(t)
    if bits and (tm is not None or period is not None or weekday is not None):
        when_text = " ".join(bits)
    elif date is not None:
        when_text = "%s（全天）" % date_text

    return {
        "start_ts": start_ts,
        "when_text": when_text,
        "title": title,
        "notes": notes,
        "source": _clip(raw.get("source", "") or source, 200),
        "date": date_text,
        "weekday": weekday if weekday is not None else -1,
        "period": ("%d-%d" % period) if period is not None and period[1] > period[0]
                  else (str(period[0]) if period is not None else ""),
        "period_start": period[0] if period is not None else 0,
        "period_end": period[1] if period is not None else 0,
        "start_time": tm.strftime("%H:%M") if tm is not None else "",
        "end_time": end_tm.strftime("%H:%M") if end_tm is not None else "",
        "teacher": teacher,
        "location": location,
        "kind": kind,
        "task_type": task_type,
        "priority": priority,
        "duration_min": duration,
        "deadline_ts": deadline_ts,
        "auto_planned": False,
    }


def _norm_title(text):
    return re.sub(r"[\s（）()【】\[\]·,，.。:：\-—/]+", "", str(text or ""))


def normalize_items(raw_items, source="", now=None):
    """原始 LLM / OCR 条目 → 可写库的日程；去重、排序、限制条数。"""
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    out, seen = [], set()
    for raw in raw_items or []:
        it = _parse_item(raw, ref, source)
        if not it:
            continue
        if it["kind"] == "course" or it["period"]:
            # 课程按**格子**去重（日期 + 星期 + 起始节）：同一格不可能同时上两门课。
            # 扫描器的几何还原排在 LLM 提取之前（main.py 的合并顺序），所以冲突时保留更可靠的那份。
            if it["date"] or it["weekday"] >= 0:
                key = ("slot", it["date"], it["weekday"], it["period_start"])
            else:
                key = ("course", it["date"], it["period"], _norm_title(it["title"]))
        else:
            key = ("item", round(float(it["start_ts"]), 1), _norm_title(it["title"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= MAX_ITEMS:
            break
    out.sort(key=lambda x: (x["start_ts"] <= 0,
                            x["start_ts"] if x["start_ts"] > 0 else 0,
                            x["weekday"] if x["weekday"] >= 0 else 9,
                            x["period_start"], x["title"]))
    return out


def memory_text(item):
    """一条日程写入长期记忆时的文本。"""
    when = str(item.get("when_text", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    notes = str(item.get("notes", "") or "").strip()
    extra = " ".join(x for x in (str(item.get("teacher", "") or "").strip(),
                                 ("@" + str(item.get("location", "") or "").strip())
                                 if str(item.get("location", "") or "").strip()
                                 and not str(item.get("location", "") or "").startswith("@")
                                 else str(item.get("location", "") or "").strip()) if x)
    text = "日程：%s %s" % (when, title)
    if extra:
        text += "（%s）" % extra
    if notes:
        text += "（%s）" % notes
    return text.strip()
