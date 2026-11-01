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
    "双选", "志愿", "沙龙", "论坛", "答辩", "审核", "参观", "展览", "导览",
    "观影", "运动会", "路演", "义诊", "公开课",
)
_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～—–至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
_SINGLE_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\s*[:：])")
_DATE_MD_RE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_LOCATION_RE = re.compile(
    r"(?:考试|活动|集合|上课|会议)?(?:地点|场地|地址|位置)(?:】|]）)?\s*[:：]\s*([^【】。；\n]{1,40})")
_LOCATION_IN_RE = re.compile(
    r"(?:在|到|前往)\s*([^，。；:：\s]{2,30})[，,]?(?:举行|举办|开展|进行|集合|参观)")
_CLOCK_ONLY_RE = re.compile(r"^\s*\d{1,2}\s*[:：]\s*\d{2}\s*$")
_HEADING_RE = re.compile("【([^】]{2,30})】")
_SECTION_HEADS = {
    "考试时间", "考试地点", "考试范围", "考核形式", "注意事项",
    "活动时间", "活动地点", "报名方式", "集合地点", "时间", "地点",
}
_DAY_PART_RE = re.compile("(凌晨|上午|中午|下午|傍晚|晚上|夜里)")
_DAY_PART_WINDOW = {
    "凌晨": (0, 5 * 60), "上午": (9 * 60, 11 * 60 + 30), "中午": (12 * 60, 13 * 60 + 30),
    "下午": (14 * 60, 17 * 60), "傍晚": (17 * 60 + 30, 19 * 60),
    "晚上": (19 * 60, 21 * 60), "夜里": (21 * 60, 23 * 60),
}
_SECTION_LINE_RE = re.compile(
    r"^【(考试时间|活动时间|时间|考试地点|活动地点|地点)】?[:：]?")
_GROUP_NOISE_RE = re.compile(
    r"(全体成员群|定位到聊天位置|本人课表|课表刷新|教务账号|查看$|切换$)")
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
        "同一活动里的分组 / 分批到场时间（「第一小组18:00」「第二小组18:10」…）只写进主活动备注，"
        "不要每个小组各输出一条；主活动只保留一条。\n"
        "原文同时给出具体日期和「本周六」等相对说法时，以具体日期为准"
        "（如「10月31日（本周六）」按 10 月 31 日）；已经过去的日期不要输出。\n"
        "只有「11月5日下午」这类时段时，date 写具体日期、day_part 写「下午」、time 留 null，"
        "不要自己猜钟点。\n"
        "标题用公告里的活动名（如「长征主题展览参观报名」），不要用「参观‘…’」这种长引文。\n"
        "今天是 %s（%s）。相对日期（今天/明天/后天/下周X）优先填 relative_date 原话短语，"
        "不要自己心算绝对日期；date 可留 null。\n"
        "只输出 JSON 数组，不要解释、不要代码围栏。字段：\n"
        '[{"date":"YYYY-MM-DD" 或 null, "relative_date":"原文里的日期短语（明天/下周三/M月D日）或 null", '
        '"day_part":"早上/中午/下午/晚上 或 null", '
        '"weekday":"周一" 或 null, '
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
    if _GROUP_NOISE_RE.search(s) or re.match(r"^\d{1,3}$", s):
        return True
    # 纯姓名 / 群昵称（「李姿睿」「体育委员许宇晨」）；带【】的注意事项不在此列。
    if re.match(r"^[\u4e00-\u9fa5]{2,3}$", s) or _SENDER_RE.match(s):
        return True
    if re.match(r"^[\u4e00-\u9fa5]{1,6}(委员|老师|同学|学长|学姐|部长|主任|班长)", s):
        return True
    if _LOCATION_RE.search(s) or _SECTION_LINE_RE.search(s):
        return True
    # 只带时间的碎片（「日）晚19：00-20：30。」「第一小组18:00」）不进备注。
    if len(s) <= 20 and (_TIME_RANGE_RE.search(s) or _SINGLE_TIME_RE.search(s)):
        return True
    t = _DATE_MD_RE.sub("", s)
    for w in _WEEKDAYS:
        t = t.replace(w, "")
    t = _DAY_PART_RE.sub("", t)
    t = _TIME_RANGE_RE.sub("", t)
    t = _SINGLE_TIME_RE.sub("", t)
    t = re.sub(r"[\s()（）【】\[\]·,，.。:：\-—/、]", "", t)
    return t == ""


def _clean_event_title(text):
    s = str(text or "").strip()
    s = re.sub(r"【[^】]*】", "", s)
    s = _DATE_MD_RE.sub("", s)
    for w in _WEEKDAYS:
        s = s.replace(w, "")
    s = _DAY_PART_RE.sub("", s)
    s = _TIME_RANGE_RE.sub("", s)
    s = _SINGLE_TIME_RE.sub("", s)
    s = re.sub(r"(将于|定于|拟于|计划于|安排在|预计在)", "", s)
    s = re.sub(r"^[：:，,、\s]+|[\s：:，,、。；;]+$", "", s)
    s = re.sub(r"[\s()（）·•,，.。:：;；\-—/]", "", s)
    return s.strip()


def _is_event_title(title, text, allow_generic=False):
    if title == "" or len(title) < 2 or len(title) > 40:
        return False
    if any(w in title for w in _EVENT_WORDS):
        return True
    # 公告标题（【…】）允许不带活动词；正文片段必须有实际内容且不是「本晚」这类碎词。
    return bool(allow_generic) and len(title) >= 4


def _clean_event_location(text):
    s = str(text or "").strip()
    s = re.sub(r"^(?:地点|场地|地址|位置)\s*[:：]\s*", "", s)
    s = re.sub(r"(?:举行|举办|开展|进行|集合|参观)$", "", s).strip()
    s = re.sub(r"[\s.。;；,，【】]+", "", s)
    return s[:30]


def _event_location(lines, flat):
    # 先按 OCR 行读「地点：篮球场」——行尾天然断开，不会把下一句并进地点。
    for line in lines:
        m = _LOCATION_RE.search(str(line or ""))
        if m:
            loc = _clean_event_location(m.group(1))
            if loc:
                return loc
    m = _LOCATION_IN_RE.search(flat)
    if m:
        return _clean_event_location(m.group(1))
    return ""


def _event_time_window(flat, date_end):
    """日期之后的一小段窗口里找时间：先整段，再单个钟点，最后退「下午 / 晚上」的默认时段。

    返回 ``(开始分钟, 结束分钟, 时间在 flat 里的位置或 None)``；找不到返回 ``None``。
    """
    start_at = max(0, date_end - 5) if date_end else 0
    window = flat[start_at:start_at + 240] if date_end else flat[:300]
    tr = _event_time_range(window)
    if tr is not None:
        start, end, m = tr
        return start, end, start_at + m.start()
    m = _SINGLE_TIME_RE.search(window)
    if m:
        try:
            hh, mm = int(m.group(1)), int(m.group(2))
        except (TypeError, ValueError):
            hh, mm = -1, -1
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            start = hh * 60 + mm
            ctx = window[max(0, m.start() - 4):m.start()]
            if any(x in ctx for x in ("下午", "傍晚", "晚上", "夜里")) and start < 12 * 60:
                start += 12 * 60
            elif "中午" in ctx and start < 11 * 60:
                start += 12 * 60
            return start, min(24 * 60, start + 90), start_at + m.start()
    part = _DAY_PART_RE.search(window)
    if part:
        start, end = _DAY_PART_WINDOW.get(part.group(1), (9 * 60, 11 * 60))
        return start, end, None
    return None


def _event_title(flat, date_start, date_end, time_start):
    limit = time_start if time_start is not None else min(len(flat), date_end + 140)
    # ① 群公告的【标题】（跳过「【考试时间】」这类字段名）。
    for m in _HEADING_RE.finditer(flat[:limit]):
        name = m.group(1).strip()
        if name in _SECTION_HEADS:
            continue
        cand = _clean_event_title(name)
        if _is_event_title(cand, flat, allow_generic=True):
            return cand
    # ② 日期与时间之间的那一段（篮球赛那种「日期 → 标题 → 时间」的写法）。
    seg = flat[date_end:limit] if date_end else ""
    if not _SECTION_LINE_RE.search(seg):
        cand = _clean_event_title(seg)
        if _is_event_title(cand, flat):
            return cand
    # ③ 日期前最后一句（「77期…实践活动第一次审核将于」这种公告正文）。
    pre = flat[max(0, date_start - 200):date_start] if date_start else ""
    parts = re.split(r"[。！？；]", pre)
    cand = _clean_event_title(parts[-1] if parts else pre)
    if _is_event_title(cand, flat):
        return cand
    return ""


def _event_notes(lines, title, location):
    out, seen = [], set()
    first_heading = next((i for i, x in enumerate(lines) if "【" in str(x or "")),
                         len(lines))
    for idx, line in enumerate(lines):
        s = str(line or "").strip()
        if s == "" or _event_noise_line(s):
            continue
        # 公告标题之前的纯昵称 / 发送人名（「安安顺顺」）不进备注。
        if idx < first_heading and re.match(r"^[\u4e00-\u9fa5]{2,4}$", s):
            continue
        if title and title in _clean_event_title(s):
            continue
        if _HEADING_RE.search(s) and (_clean_event_title(s) == "" or title in s):
            continue
        if title and len(_clean_event_title(s)) >= 4 and title.startswith(_clean_event_title(s)):
            continue
        if location and location in s:
            continue
        if _DATE_MD_RE.search(s) or _TIME_RANGE_RE.search(s) or _SINGLE_TIME_RE.search(s):
            continue
        if (re.match(r"^[：:，,、\s]*(各位|大家|同学们)", s)
                or s.startswith("请")):
            continue
        if re.search(r"(安排如|如下)", s) or re.search(r"第[一二三四五六七八九十]+小组", s):
            continue
        if len(s) <= 3 or s in seen:
            continue
        seen.add(s)
        # 换行被截图切断的同一段（「入党誓词、党」+「章…」）拼回去；【…】另起一段。
        if out and not s.startswith("【") and not out[-1].endswith(("。", "！", "？", "；")):
            out[-1] += s
        else:
            out.append(s)
    return _clip(" | ".join(out), NOTES_MAX)


def _event_from_text(text, ref, allow_past=False):
    """从聊天公告 / OCR 文本里规则抽一条明确活动；抽不出返回 None。

    v4.5.4 数据清洗：先按「去换行后的整段」重建日期、时间、标题与地点——截图 OCR 常把
    「将于 / 举行 / 地点」拆到两行；标题优先取【公告标题】，其次日期与时间之间的短句，
    最后日期前最后一句。默认跳过已经过去的公告；``allow_past=True`` 仅供源文件过期判定。
    """
    lines = _event_lines(text)
    if not lines:
        return None
    flat = "".join(lines)
    date = _event_date(flat, ref)
    if date is None or (not allow_past and date < ref.date()):
        return None
    dm = _DATE_MD_RE.search(flat)
    date_start = dm.start() if dm else 0
    date_end = dm.end() if dm else 0
    got = _event_time_window(flat, date_end)
    if got is None:
        return None
    start, end, time_start = got
    title = _event_title(flat, date_start, date_end, time_start)
    if title == "":
        return None
    if not (any(w in title for w in _EVENT_WORDS)
            or any(k in flat for k in ("签到", "报名", "举行", "举办", "集合", "参观"))):
        return None

    location = _event_location(lines, flat)
    notes = _event_notes(lines, title, location)
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


def _norm_event_key(title):
    s = str(title or "")
    s = re.sub(r"【[^】]*】", "", s)
    s = re.sub(r"(通知|公告|报名|安排|关于)", "", s)
    s = re.sub(r"[\s()（）·,，.。:：;；\-—/]", "", s)
    return s


def _title_grams(title):
    s = _norm_event_key(title)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _raw_same_event(a, b):
    ta, tb = _norm_event_key(a.get("title", "")), _norm_event_key(b.get("title", ""))
    ad, bd = str(a.get("date") or ""), str(b.get("date") or "")
    same_date = not ad or not bd or ad == bd
    at, bt = _parse_time(a.get("time")), _parse_time(b.get("time"))
    same_clock = (at is not None and bt is not None
                  and abs(at.hour * 60 + at.minute - bt.hour * 60 - bt.minute) <= 5)
    if ta and tb:
        if (ta == tb or ta in tb or tb in ta) and same_date:
            return True
        # 同一时刻、标题换了个说法（「结业考试」vs「结业考试通知」/「77期…结业考试」）：
        # 看内容二元组是否有足够的公共片段，避免把「篮球赛 / 足球赛」误并。
        # 一方没给时间（如 LLM 只写「11月5日下午」）时，也允许合并并把时间补进来。
        overlap = _title_grams(ta) & _title_grams(tb)
        if same_date and len(overlap) >= 2 and (at is None or bt is None or same_clock):
            return True
        la = str(a.get("location") or "").strip()
        lb = str(b.get("location") or "").strip()
        if same_date and same_clock and la and lb and la == lb:
            return True
        return False
    if at is not None and bt is not None:
        return same_date and same_clock and (not ta or not tb)
    return False


def merge_event_fallback(raw_items, fallback_items):
    """把规则补抽结果并入 LLM 结果；同日期 + 同标题 / 同时刻的不重复添加。

    v4.5.4：命中同一件事时不整条丢弃补抽——把它多出来的时间 / 地点 / 备注补进 LLM 条目。
    （典型场景：LLM 只写「11月5日下午」，规则补抽读出了 14:00-17:00 与博物馆地点。）
    """
    out = [dict(x) for x in raw_items or [] if isinstance(x, dict)]
    for fb in fallback_items or []:
        if not isinstance(fb, dict):
            continue
        hit = next((x for x in out if _raw_same_event(x, fb)), None)
        if hit is not None:
            # LLM 把标题写成「参观‘伟大…’」这类长引文时，换回公告里的短标题。
            if (len(str(hit.get("title", ""))) > 24
                    and 0 < len(str(fb.get("title", ""))) <= 18):
                hit["title"] = str(fb["title"])
            for field in ("date", "time", "end_time", "location", "notes",
                          "duration_min", "priority"):
                if not hit.get(field) and fb.get(field):
                    hit[field] = fb[field]
            continue
        out.append(dict(fb))
        if len(out) >= MAX_ITEMS:
            break
    return out


def drop_past_events(items, now=None):
    """资料扫描的过期清理：已经开始超过 2 小时、且没有截止时间的普通条目不再新建。

    课程表是「这一周的固定安排」照常保留（学生可能补看过去几天的课）；
    只挡聊天公告里的过期活动，避免把昨天 / 上周的截图又写进日程。
    """
    try:
        ref = float(time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        ref = time.time()
    out = []
    ref_date = datetime.datetime.fromtimestamp(ref).date()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("kind", "")) == "course":
            out.append(it)
            continue
        try:
            start = float(it.get("start_ts", 0) or 0)
            deadline = float(it.get("deadline_ts", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            start, deadline = 0.0, 0.0
        # 只有日期没有时刻的旧公告（10月29日「篮球赛」）也要挡掉：按 date 字段比较。
        date_text = str(it.get("date") or it.get("date_text") or "").strip()[:10]
        if deadline <= 0.0 and date_text:
            try:
                if datetime.date.fromisoformat(date_text) < ref_date:
                    continue
            except ValueError:
                pass
        if start > 0.0 and start <= ref - 7200.0 and deadline <= 0.0:
            continue
        out.append(it)
    return out


_GROUP_ITEM_RE = re.compile(r"(?:^|（|\()第[一二三四五六七八九十]+(?:小组|组)(?:答辩)?(?:）|\))")
_GROUP_LABEL_RE = re.compile(r"(第[一二三四五六七八九十]+(?:小组|组))")


def _item_title_tokens(title):
    """标题里用于「对齐源文件日期」的内容二元组（去掉公告 / 报名这类通用词）。"""
    s = _norm_event_key(title)
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    stop = {"活动", "通知", "公告", "报名", "考试", "主题", "参观", "小组", "答辩",
            "第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八", "第九"}
    return {g for g in grams if g not in stop}


def _scan_file_texts(scan):
    """每个资料文件的整段文本；已经有结构化课程表的文件跳过（课程单独入库）。"""
    out = []
    for f in (scan or {}).get("files", []) if isinstance(scan, dict) else []:
        if not isinstance(f, dict):
            continue
        if isinstance(f.get("table"), list) and f.get("table"):
            continue
        text = "".join(str(f.get(k, "") or "").strip() for k in ("text", "ocr", "vlm"))
        if text:
            out.append((str(f.get("name", "")), text))
    return out


def _date_near_text(text, tokens, ref):
    """标题片段附近（±320 字）的明确日期；没有就把文件里唯一的日期当锚点。"""
    matches = list(_DATE_MD_RE.finditer(text))
    if not matches:
        return None
    spots = [p for p in (text.find(t) for t in tokens) if p >= 0]
    if spots:
        p = min(spots)
        best = min(matches, key=lambda m: abs(m.start() - p))
        if abs(best.start() - p) <= 320:
            return _event_date(best.group(0), ref)
    dates = {d for d in (_event_date(m.group(0), ref) for m in matches) if d is not None}
    return dates.pop() if len(dates) == 1 else None


def reconcile_source_dates(items, scan, now=None):
    """把 LLM 条目的日期对回源文件里的明确日期（v4.5.4）。

    模型偶尔会把「10月31日（本周六）」解析成下一个周六，或把「11月5日」写成 11-04。
    这里按标题内容片段找到最匹配的文件，再取标题附近（或该文件唯一）的明确日期覆盖；
    只在源文件里真的写了日期时才覆盖，不改无日期条目。
    """
    try:
        ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    except (TypeError, ValueError, OverflowError):
        ref = datetime.datetime.now()
    files = _scan_file_texts(scan)
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        d = dict(it)
        tokens = _item_title_tokens(d.get("title", ""))
        best_text, best_score = "", 0
        for _name, text in files:
            score = sum(1 for t in tokens if t in text)
            if score > best_score:
                best_text, best_score = text, score
        if tokens and best_score > 0:
            near = _date_near_text(best_text, tokens, ref)
            if near is not None and str(d.get("date") or "") != near.isoformat():
                d["date"] = near.isoformat()
                d["relative_date"] = None
                d["weekday"] = None
        out.append(d)
    return out


def collapse_group_items(items):
    """把「第一小组18:00 / 第二小组18:10 …」合并回一条主活动（v4.5.4）。

    分组到场时间属于同一场答辩 / 活动的子细节，不该在日程里占 N 条；
    合并后主条目的开始取最早、结束取最晚，原始分段时间写进备注。
    """
    keep, groups = [], {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        m = _GROUP_ITEM_RE.search(title)
        if m is None:
            keep.append(dict(it))
            continue
        prefix = title[:m.start()].strip(" （）()")
        if prefix == "":
            prefix = "活动"
        key = (prefix, str(it.get("date", "")), str(it.get("location", "")))
        groups.setdefault(key, []).append(dict(it))
    for (prefix, date, loc), rows in groups.items():
        if len(rows) == 1:
            keep.append(rows[0])
            continue
        times = [x for x in (_parse_time(r.get("time")) for r in rows) if x is not None]
        ends = [x for x in (_parse_time(r.get("end_time")) for r in rows) if x is not None]
        main = next((x for x in keep
                     if str(x.get("title", "")).strip() == prefix
                     and str(x.get("date", "")) == date), None)
        if main is None:
            main = dict(rows[0])
            main["title"] = prefix
            keep.append(main)
        if times:
            start = min(t.hour * 60 + t.minute for t in times)
            end = (max(t.hour * 60 + t.minute for t in ends) if ends
                   else max(t.hour * 60 + t.minute for t in times) + 10)
            if end <= start:
                end = max(t.hour * 60 + t.minute for t in times) + 10
            main["time"] = "%02d:%02d" % (start // 60, start % 60)
            main["end_time"] = "%02d:%02d" % (end // 60, end % 60)
            main["duration_min"] = max(10, end - start)
        labels = []
        for r in rows:
            lm = _GROUP_LABEL_RE.search(str(r.get("title", "")))
            if lm and r.get("time"):
                labels.append("%s %s" % (lm.group(1), r["time"]))
        if labels:
            note = "各组时间：" + "、".join(labels)
            main["notes"] = _clip(" | ".join(
                [x for x in (str(main.get("notes", "")).strip(), note) if x]), NOTES_MAX)
        if not main.get("location") and loc:
            main["location"] = loc
    return keep


def drop_past_source_items(items, scan, now=None):
    """源文件层面的过期兜底（v4.5.4）：即使 LLM 把「10月31日（本周六）」算成 11-07，
    只要源公告本身是过期活动，就把标题片段对得上的条目一并挡掉。

    只看**各文件的规则抽取值**（不依赖 LLM 日期），按标题内容二元组重叠 ≥2 才命中；
    并且要求条目与该文件最匹配——否则同一群里「77期党校初级班」的考试会被另一条过期
    活动误伤。
    """
    try:
        ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    except (TypeError, ValueError, OverflowError):
        ref = datetime.datetime.now()
    files = _scan_file_texts(scan)
    past_by_file = {}
    for _name, text in files:
        got = _event_from_text(text, ref, allow_past=True)
        if not got or not got.get("date"):
            continue
        try:
            d = datetime.date.fromisoformat(str(got["date"])[:10])
        except ValueError:
            continue
        if d < ref.date():
            past_by_file[text] = _item_title_tokens(got.get("title", ""))
    if not past_by_file:
        return list(items or [])
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("kind", "")) == "course":
            out.append(it)
            continue
        tokens = _item_title_tokens(it.get("title", ""))
        if not tokens:
            out.append(it)
            continue
        best_text, best_score = "", 0
        for _name, text in files:
            score = sum(1 for t in tokens if t in text)
            if score > best_score:
                best_text, best_score = text, score
        past_tokens = past_by_file.get(best_text, set())
        if best_score >= 2 and past_tokens and len(tokens & past_tokens) >= 2:
            continue
        out.append(it)
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
    # v4.3.1：相对周次由规则落地，不让 LLM 心算。「下周三」= 下周一 + 3；
    # 裸「周三」仍取最近的下一个周三（今天就是周三时取下周，保持旧行为）。
    if "下周" in s or "下个星期" in s or "下星期" in s:
        for word, wd in _WEEKDAYS.items():
            if word in s:
                monday = today - datetime.timedelta(days=today.weekday())
                return monday + datetime.timedelta(days=7 + wd)
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


def _day_part_of(*parts):
    """文本里的时段词 → 早上 / 中午 / 下午 / 晚上；认不出返回空串。"""
    for raw in parts:
        s = str(raw or "")
        if any(w in s for w in ("早上", "早晨", "清晨", "上午", "早八", "早起")):
            return "早上"
        if any(w in s for w in ("中午", "午饭", "午休")):
            return "中午"
        if "下午" in s:
            return "下午"
        if any(w in s for w in ("傍晚", "晚上", "晚饭", "夜里", "今晚", "夜间")):
            return "晚上"
    return ""


def _adjust_time_for_day_part(tm, day_part):
    """把「8:00 + 晚上」这类只有数字、没有 AM/PM 的时刻按原话时段补正。"""
    if tm is None or day_part == "":
        return tm
    h, minute = tm.hour, tm.minute
    if day_part in ("下午", "晚上") and h < 12:
        h += 12
    elif day_part == "中午" and h < 11:
        h += 12
    if h > 23:
        return tm
    return datetime.time(h, minute)


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
    # v4.3.1：优先用原话日期短语由规则落地；LLM 的绝对 date 只当兜底。
    rel_text = str(raw.get("relative_date", raw.get("when_date_text", "")) or "").strip()
    date = _parse_date(rel_text, ref) if rel_text else None
    if date is None:
        date = _parse_date(raw.get("date", raw.get("day", raw.get("when_date", ""))), ref)
    raw_time = str(raw.get("time", raw.get("at", raw.get("start_time", raw.get("start", "")))) or "")
    day_part = str(raw.get("day_part", "") or "").strip() or _day_part_of(
        rel_text, raw.get("date", ""), raw_time)
    tm = _adjust_time_for_day_part(_parse_time(raw_time), day_part)
    end_tm = _adjust_time_for_day_part(_parse_time(raw.get("end_time", raw.get("end", ""))), day_part)
    if end_tm is None:
        # 模型有时把时间写成 "08:00-09:35"：拆开保留结束时间（周课表要显示时间跨度）。
        parts = [x for x in re.split(r"[-~～—–]", raw_time) if x.strip()]
        if len(parts) >= 2:
            end_tm = _adjust_time_for_day_part(_parse_time(parts[1]), day_part)

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

    # 只有**真的带节次**的条目才是课程；群公告里的「周四篮球赛」即使模型写了
    # kind=course，也按普通约定处理（否则会被当成不可移动的固定课）。
    kind = "course" if period is not None else "task"
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

    # 细化已有条目（v4.5.0）：只认正整数的 refine_id，脏值 / bool / 负数一律当「新建」。
    try:
        refine_id = int(raw.get("refine_id", raw.get("update_id", raw.get("target_id", 0)) or 0))
    except (TypeError, ValueError, OverflowError):
        refine_id = 0
    if refine_id <= 0:
        refine_id = 0

    return {
        "refine_id": refine_id,
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
        elif it.get("refine_id"):
            # 细化条目按 id 去重：同一次回复里对同一条的两次补充只保留最后一次。
            key = ("refine", it["refine_id"], _norm_title(it["title"]))
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


def memory_meta(item):
    """日程记忆的结构化 meta（v4.3.0，F）：给 supersede 判定「同一天同标题」用。

    只取稳定字段；脏值按 0 / 空串，绝不因为一条日程的字段坏了而让记忆写入抛异常。
    """
    it = item if isinstance(item, dict) else {}
    try:
        sid = int(it.get("id", 0) or 0)
    except (TypeError, ValueError):
        sid = 0
    try:
        start = float(it.get("start_ts", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        start = 0.0
    return {"schedule_id": sid,
            "title": str(it.get("title", "") or "").strip(),
            "start_ts": start,
            "when_text": str(it.get("when_text", "") or "").strip(),
            "status": str(it.get("status", "planned") or "planned")}
