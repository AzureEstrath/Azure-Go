"""聊天 → 日程：由 Azure 自己判断这句话里有没有要排的事（v4.2.0）。

边界（前后端分工）：
- **判断 / 抽取 / 写库全在后端**：聊天回复完成后，``main.py`` 起一个后台任务调本模块；
  前端只负责把结果当普通聊天补充显示，不参与「排不排」的决定。
- **先规则闸门、再 LLM**：明显没有时间 / 事项线索的闲聊**不调模型**（省一次生成本就很贵的
  本地推理）；有线索也只是「候选」，最终由模型按提示词判断，避免「提到明天就硬排」。
- **静默失败**：LLM 不通 / 超时 / 输出不是 JSON 时返回空列表，聊天照常，不向玩家报错。
- 本模块只产出 ``schedule.normalize_items`` 的形状；写库与自动排班由调用方完成。

``schedule_chat_enabled=false`` 时调用方直接跳过本模块（与旧行为逐字一致）。
"""

import datetime
import json
import re
import time

from . import schedule

MAX_TEXT = 1200
MAX_ITEMS = 4
TITLE_MAX = 32

# 时间线索：只要出现一个就值得让模型判断（不是「一定排」）。
_TIME_CUE = re.compile(
    r"(今天|明天|后天|大后天|今晚|明晚|明早|早上|上午|中午|下午|傍晚|晚上|夜里|凌晨|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]|周末|"
    r"下周|下下周|这周|本周|下个月|月底|月初|年底|年前|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|"
    r"\d{1,2}\s*[:：]\s*\d{2}|"
    r"\d{1,2}\s*点|"
    r"(半|一|两|三|四|五|六|七|八|九|十|几)\s*(个)?\s*(小时|钟头|分钟))",
    re.I)

# 事项线索：明确的待办 / 约定 / 截止 / 提醒动词。
_TASK_CUE = re.compile(
    r"(提醒|记一下|记住|记下|安排|排一下|排进|别忘了|截止|要交|提交|"
    r"报告|作业|论文|复习|预习|考试|面试|开会|会议|讲座|活动|比赛|聚会|"
    r"约好|约了|约在|约会|见面|吃饭|去医院|看病|取快递|取件|寄|报名|签到|打卡|"
    r"上课|出差|赶车|赶飞机|健身|锻炼|打扫|做完|写完|deadline|meeting|tomorrow|"
    r"remind|appointment)",
    re.I)


def _clip(text, limit=TITLE_MAX):
    s = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[:limit - 1] + "…"


def looks_schedulable(text):
    """规则闸门：这句话**值不值得**让模型判断（False ⇒ 一次 LLM 都不发）。"""
    s = str(text or "").strip()
    if s == "":
        return False
    return bool(_TIME_CUE.search(s) or _TASK_CUE.search(s))


def _json_object(text):
    """从模型输出里尽量取出 JSON 对象；失败返回 ``{}``。

    兼容三种常见形态：纯对象、代码围栏内的对象、以及模型直接吐一个数组
    （数组按 ``{"schedule": true, "items": [...]}`` 理解）。
    """
    raw = str(text or "").strip()
    if raw == "":
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    candidates = [raw]
    a, b = raw.find("{"), raw.rfind("}")
    if a >= 0 and b > a:
        candidates.append(raw[a:b + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"schedule": True, "items": data}
    # 对象解析失败时再试一次数组（模型可能只回一个裸数组）。
    a, b = raw.find("["), raw.rfind("]")
    if a >= 0 and b > a:
        try:
            data = json.loads(raw[a:b + 1])
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list):
            return {"schedule": True, "items": data}
    return {}


def _pick_items(data):
    """从判断结果里取条目：``schedule=false`` 一律不排。"""
    if not isinstance(data, dict):
        return []
    sched = data.get("schedule")
    if isinstance(sched, bool) and not sched:
        return []
    if isinstance(sched, list):
        return sched
    if isinstance(sched, dict) and isinstance(sched.get("items"), list):
        return sched["items"]
    items = data.get("items")
    if isinstance(items, list):
        return items
    for key in ("events", "tasks", "schedule_items"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _drop_past(items, now):
    """去掉「已经彻底过去」的条目：明天还要交的逾期任务不算（截止仍在保护）。"""
    out = []
    for it in items or []:
        try:
            start = float(it.get("start_ts", 0) or 0)
            duration = max(5, int(it.get("duration_min", 0) or 0)) * 60.0
            deadline = float(it.get("deadline_ts", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            start, duration, deadline = 0.0, 300.0, 0.0
        end = start + duration if start > 0 else 0.0
        if end > 0 and end <= now - 7200.0 and (deadline <= 0 or deadline <= now):
            continue
        out.append(it)
    return out


async def extract(llm, text, now=None):
    """判断并抽取聊天里的日程；返回 ``schedule.normalize_items`` 形状的列表。

    失败（没有地址 / 超时 / 空输出 / 非 JSON / 判断为 false）一律返回 ``[]``，
    **绝不把错误文本当成日程**，也不向调用方抛异常。
    """
    raw_text = _clip(text, MAX_TEXT)
    if llm is None or raw_text == "" or not looks_schedulable(raw_text):
        return []
    ref = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    prompt = (
        "你是桌面助手 Azure 的日程判断器。判断玩家这句话里有没有**他自己要做的、"
        "值得记进日程的事**。\n"
        "只有同时满足才排：① 玩家明确说出了自己的安排 / 约定 / 任务 / 截止 / 需要提醒的事；"
        "② 这件事还没发生，或正在进行但还需要后续完成。\n"
        "不要排：闲聊、感受、假设（“如果我明天…”）、玩笑、问 Azure 自己的事、"
        "已经过去的活动、别人的事、泛泛的愿望。"
        "玩家只说“明天好忙”这类情绪、没有具体事项时，也不要排。\n"
        "今天是 %s（%s）。相对日期（今天/明天/后天/下周X）请换算成真实日期；"
        "不要编造日期、地点、时长、人物。\n"
        "只输出 JSON 对象，不要解释、不要代码围栏：\n"
        '{"schedule": false}\n'
        "或\n"
        '{"schedule": true, "items": [{"date": "YYYY-MM-DD" 或 null, '
        '"weekday": "周一" 或 null, "time": "HH:MM" 或 null, '
        '"end_time": "HH:MM" 或 null, "title": "10-30 字的事项", '
        '"location": "可选", "priority": "0-100 的整数", '
        '"duration_min": "预计分钟数", "deadline": "YYYY-MM-DD HH:MM" 或 null, '
        '"notes": "可选"}]}\n'
        "只有日期没有时刻也可以；只说“提醒我记一下”但没给日期时间时，date/time 都写 null。\n\n"
        "玩家刚说：「%s」"
    ) % (ref.strftime("%Y-%m-%d"), "周" + "一二三四五六日"[ref.weekday()], raw_text)
    msgs = [{"role": "user", "content": prompt}]
    try:
        try:
            out = await llm.chat(msgs, 500, think_mode=0)
        except TypeError:
            out = await llm.chat(msgs, 500)
    except Exception as e:                  # noqa: BLE001 —— 聊天排程绝不打挂聊天
        print("[Schedule] 聊天日程判断失败（已忽略）：%s" % e)
        return []
    data = _json_object(out)
    raw_items = _pick_items(data)
    if not raw_items:
        return []
    items = schedule.normalize_items(raw_items, source="chat", now=ref.timestamp())
    return _drop_past(items, ref.timestamp())[:MAX_ITEMS]


def _friendly_when(item):
    """``2026-10-28 周三 15:00-16:00`` → ``10月28日 周三 15:00-16:00``。"""
    when = str((item or {}).get("when_text", "") or "").strip()
    if when == "" or when == "待定":
        return ""
    return re.sub(r"(\d{4})-(\d{2})-(\d{2})",
                  lambda m: "%d月%d日" % (int(m.group(2)), int(m.group(3))),
                  when)


def confirmation(items):
    """排程完成后的一句自然补充（模板、零额外 LLM；不念数据库 / 系统）。"""
    rows = [it for it in (items or [])
            if isinstance(it, dict) and str(it.get("title", "")).strip() != ""]
    if not rows:
        return ""
    parts = []
    timed = 0
    undated = 0
    for it in rows[:2]:
        title = _clip(it.get("title", ""), 40)
        when = _friendly_when(it)
        if when:
            timed += 1
            parts.append("「%s」（%s）" % (title, when))
        else:
            undated += 1
            parts.append("「%s」" % title)
    head = "好，日程记好了：" if len(rows) == 1 else "好，日程记好了 %d 件：" % len(rows)
    text = head + "、".join(parts)
    if len(rows) > 2:
        text += " 等"
    text += "。"
    if timed > 0 and undated > 0:
        text += "有时间的到点会提醒你，没时间的先放进待办。"
    elif timed > 0:
        text += "到时候我会提醒你。"
    else:
        text += "先放进待办，有空档就给你排。"
    return text
