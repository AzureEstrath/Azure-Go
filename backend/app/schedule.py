"""资料 → 日程：把 OCR / VLM / 文档文本交给 LLM 提取，再归一化成可持久化的日程。

设计边界：
- 只做**明确的日期 / 时间 / 约定 / 待办**提取，不把普通闲聊编成日程；
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

MAX_SOURCE_TEXT = 9000
MAX_ITEMS = 30
TITLE_MAX = 80
NOTES_MAX = 200
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WEEKDAYS = {"周一": 0, "星期二": 1, "周二": 1, "周三": 2, "星期四": 3,
             "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
             "星期一": 0, "礼拜一": 0, "礼拜二": 1, "礼拜三": 2,
             "礼拜四": 3, "礼拜五": 4, "礼拜六": 5, "礼拜天": 6}


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
            for key in ("items", "schedule", "events", "data"):
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
    """调用 LLM 从资料文本里提取原始日程条目；失败返回 ``[]``。"""
    if llm is None or str(text or "").strip() == "":
        return []
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    prompt = (
        "你是日程整理助手。下面是从文件里提取的 OCR / VLM / 文档文本。\n"
        "只提取**明确出现过**的日期、时间、约定、会议、提醒、待办；没有就返回空数组。\n"
        "不要编造日期、地点、人物；不要把普通聊天当成日程；不要把“今天天气”当成事件。\n"
        "今天是 %s（%s）。相对日期（今天/明天/后天/下周X）请换算成真实日期。\n"
        "只输出 JSON 数组，不要解释、不要代码围栏。字段：\n"
        '[{"date":"YYYY-MM-DD" 或 null, "time":"HH:MM" 或 null, '
        '"title":"简短标题", "notes":"可选备注"}]\n\n'
        "资料：\n%s"
    ) % (ref.strftime("%Y-%m-%d"),
         ("周" + "一二三四五六日"[ref.weekday()]),
         str(text))
    msgs = [{"role": "user", "content": prompt}]
    try:
        try:
            out = await llm.chat(msgs, 700, think_mode=0)
        except TypeError:
            out = await llm.chat(msgs, 700)
    except Exception as e:                  # noqa: BLE001 —— 日程提取绝不打挂端点
        print("[Schedule] LLM 提取失败（已忽略）：%s" % e)
        return []
    return _json_array(out)[:MAX_ITEMS]


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


def _parse_item(raw, ref, source):
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title", raw.get("name", raw.get("summary", ""))), TITLE_MAX)
    if title == "":
        return None
    notes = _clip(raw.get("notes", raw.get("note", raw.get("details", ""))), NOTES_MAX)
    date = _parse_date(raw.get("date", raw.get("day", raw.get("when_date", ""))), ref)
    tm = _parse_time(raw.get("time", raw.get("at", raw.get("when_time", ""))))
    when_text = ""
    start_ts = 0.0
    if date is None and tm is not None:
        date = ref.date()
        if datetime.datetime.combine(date, tm) <= ref:
            date = date + datetime.timedelta(days=1)
    if date is not None:
        if tm is None:
            tm = datetime.time(9, 0)
            when_text = "%s（全天）" % date.isoformat()
        else:
            when_text = "%s %s" % (date.isoformat(), tm.strftime("%H:%M"))
        start_ts = datetime.datetime.combine(date, tm).timestamp()
    else:
        when_text = "待定"
    return {"start_ts": start_ts, "when_text": when_text, "title": title,
            "notes": notes, "source": source}


def normalize_items(raw_items, source="", now=None):
    """原始 LLM 条目 → 可写库的日程；去重、排序、限制条数。"""
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    out, seen = [], set()
    for raw in raw_items or []:
        it = _parse_item(raw, ref, source)
        if not it:
            continue
        key = (round(float(it["start_ts"]), 1), it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= MAX_ITEMS:
            break
    out.sort(key=lambda x: (x["start_ts"] <= 0, x["start_ts"] if x["start_ts"] > 0 else 0,
                            x["title"]))
    return out


def memory_text(item):
    """一条日程写入长期记忆时的文本。"""
    when = str(item.get("when_text", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    notes = str(item.get("notes", "") or "").strip()
    text = "日程：%s %s" % (when, title)
    if notes:
        text += "（%s）" % notes
    return text.strip()
