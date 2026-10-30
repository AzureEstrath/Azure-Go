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

import asyncio
import datetime
import json
import re
import time

from . import schedule

MAX_TEXT = 1200
MAX_ITEMS = 4
TITLE_MAX = 32
# 后台日程判断的单次 LLM 上限（v4.2.8）：本地单槽位模型偶尔会卡在长输出上，
# 超时就放弃这一轮，绝不占着生成锁拖慢下一句聊天。
LLM_TIMEOUT_SEC = 10.0

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
        '"relative_date": "原话里的日期短语（明天/下周三/10月31日）或 null", '
        '"day_part": "早上/中午/下午/晚上 或 null", '
        '"weekday": "周一" 或 null, "time": "HH:MM" 或 null, '
        '"end_time": "HH:MM" 或 null, "title": "10-30 字的事项", '
        '"location": "可选", "priority": "0-100 的整数", '
        '"duration_min": "预计分钟数", "deadline": "YYYY-MM-DD HH:MM" 或 null, '
        '"notes": "可选"}]}\n'
        "日期和时间只按玩家原话写：有原话日期短语就填 relative_date，不要自己心算；"
        "只有日期没有时刻也可以；只说“提醒我记一下”但没给日期时间时，date/time 都写 null。\n\n"
        "玩家刚说：「%s」"
    ) % (ref.strftime("%Y-%m-%d"), "周" + "一二三四五六日"[ref.weekday()], raw_text)
    msgs = [{"role": "user", "content": prompt}]
    try:
        try:
            out = await asyncio.wait_for(
                llm.chat(msgs, 500, think_mode=0), timeout=LLM_TIMEOUT_SEC)
        except TypeError:
            out = await asyncio.wait_for(
                llm.chat(msgs, 500), timeout=LLM_TIMEOUT_SEC)
    except Exception as e:                  # noqa: BLE001 —— 聊天排程绝不打挂聊天
        print("[Schedule] 聊天日程判断失败（已忽略）：%s" % e)
        return []
    data = _json_object(out)
    raw_items = _pick_items(data)
    if not raw_items:
        return []
    items = schedule.normalize_items(raw_items, source="chat", now=ref.timestamp())
    return _drop_past(items, ref.timestamp())[:MAX_ITEMS]


# 取消 / 撤销线索（v4.2.7）：玩家说“不吃了 / 退掉了 / 取消 / 别提醒”时，
# 先用**规则 + 现有日程标题**做匹配，匹配不到再让 LLM 结合最近对话判断。
_CANCEL_CUE = re.compile(
    r"(取消|撤掉|删掉|去掉|退掉|退订|退了|不要了|不去了|不吃了|不做了|不开了|"
    r"不参加了|不用了|别提醒|不用提醒|不提醒|不安排了|别安排|"
    r"作废|算了|不弄了|不搞了)",
    re.I)

# 标题二元组里这些太泛，不能拿来当取消匹配依据（否则“提醒”二字会误伤别的日程）。
_CANCEL_STOP_GRAMS = {
    "提醒", "记得", "一下", "时候", "已经", "今天", "明天", "后天", "今晚", "明晚",
    "早上", "上午", "中午", "下午", "晚上", "这个", "那个", "一个", "需要", "要去",
    "我的", "你的", "去拿", "准备", "安排",
}


def looks_cancellable(text):
    """这句话是不是在取消 / 撤销某件事（纯规则；False ⇒ 连模型都不问）。"""
    s = str(text or "").strip()
    return bool(s) and bool(_CANCEL_CUE.search(s))


def _is_question(text):
    """疑问句（“取消了吗 / 外卖退了吗？”）只回答，绝不能据此撤销日程。"""
    t = str(text or "").strip()
    return ("？" in t or "?" in t or t.endswith("吗") or t.endswith("呢") or t.endswith("么"))


def _cancel_candidates(items, now=None):
    """现有可取消条目：planned、非课程、时间在合理窗口内（或待定）。"""
    try:
        ref = float(time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        ref = time.time()
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("status", "planned")) != "planned":
            continue
        if str(it.get("kind", "")) == "course" or str(it.get("period", "")).strip() != "":
            continue
        try:
            start = float(it.get("start_ts", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            start = 0.0
        if start > 0.0 and (start < ref - 6 * 3600.0 or start > ref + 14 * 86400.0):
            continue
        out.append(it)
    return out


def _topic_grams(title):
    """标题里的可用匹配片段：中文二元组 + 3 字母以上的英文词。"""
    t = str(title or "").strip().lower()
    if t == "":
        return set()
    grams = set()
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g.strip() == "" or g in _CANCEL_STOP_GRAMS:
            continue
        if re.match(r"^[\u4e00-\u9fff]{2}$", g):
            grams.add(g)
    for word in re.findall(r"[a-z]{3,}", t):
        grams.add(word)
    return grams


def match_cancellations(text, context_text, items, now=None):
    """规则匹配取消对象：返回被取消的日程 id 列表（不调 LLM）。

    匹配依据是「玩家这句话 + 最近几轮对话」里出现的**现有日程标题片段**
    （例如标题「去拿外卖」会匹配上下文里的「外卖」）。多个条目同等命中且分数很低时
    视为歧义，返回空列表交给 LLM 兜底。
    """
    if not looks_cancellable(text):
        return []
    if _is_question(text):
        return []
    hay = (str(context_text or "") + " " + str(text or "")).lower()
    if hay.strip() == "":
        return []
    best = 0
    matches = []
    for it in _cancel_candidates(items, now=now):
        title = str(it.get("title", "")).strip()
        if title == "":
            continue
        grams = _topic_grams(title)
        score = sum(1 for g in grams if g in hay)
        if len(title) >= 2 and title.lower() in hay:
            score += 2
        if score <= 0:
            continue
        sid = int(it.get("id", 0) or 0)
        if sid <= 0:
            continue
        if score > best:
            best = score
            matches = [sid]
        elif score == best:
            matches.append(sid)
    if not matches:
        return []
    if len(matches) > 1 and best < 2:
        return []
    return matches


def _cancel_ids_from(data, valid_ids):
    """从模型 JSON 里读 ``cancel_ids`` / ids / cancel，只保留候选里的 id。"""
    if not isinstance(data, dict):
        return []
    raw = data.get("cancel_ids")
    if raw is None:
        raw = data.get("ids")
    if raw is None:
        raw = data.get("cancel")
    if isinstance(raw, dict):
        raw = raw.get("ids") or raw.get("cancel_ids") or []
    if not isinstance(raw, list):
        return []
    out = []
    for v in raw:
        try:
            sid = int(v)
        except (TypeError, ValueError):
            continue
        if sid in valid_ids and sid not in out:
            out.append(sid)
    return out


async def extract_cancellations(llm, text, context_text, items, now=None):
    """判断玩家这句话是否要取消现有日程；返回要取消的 id 列表。

    先走**规则匹配**（零模型调用，响应快、不会误杀课程）；规则拿不准时，才把
    「最近对话 + 现有待办清单」交给 LLM 做语言理解。失败一律返回 ``[]``。
    """
    candidates = _cancel_candidates(items, now=now)
    if not candidates or not looks_cancellable(text):
        return []
    if _is_question(text):
        return []                          # 疑问句不撤销，避免把“取消了吗？”当成取消
    ids = match_cancellations(text, context_text, candidates, now=now)
    if ids or llm is None:
        return ids
    lines = []
    for it in candidates[:12]:
        lines.append("- id=%s 标题=%s 时间=%s" % (
            it.get("id", 0), _clip(it.get("title", ""), 40), _clip(it.get("when_text", ""), 40)))
    prompt = (
        "玩家可能正在取消 / 撤销某个待办或约定。结合最近对话判断：玩家明确不要了、"
        "已经退掉 / 取消 / 不去了的，才列出来；只是抱怨、改主意但没取消的不要列。\n"
        "现有待办：\n%s\n\n最近对话：%s\n玩家刚说：「%s」\n"
        "只输出 JSON：{\"cancel_ids\": [id, ...]}；没有要取消的就输出 {\"cancel_ids\": []}。"
    ) % ("\n".join(lines), _clip(context_text, 600), _clip(text, MAX_TEXT))
    try:
        try:
            out = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], 200, think_mode=0),
                timeout=LLM_TIMEOUT_SEC)
        except TypeError:
            out = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], 200),
                timeout=LLM_TIMEOUT_SEC)
    except Exception as e:                  # noqa: BLE001 —— 取消判断失败不影响聊天
        print("[Schedule] 取消日程判断失败（已忽略）：%s" % e)
        return []
    data = _json_object(out)
    valid = {int(it.get("id", 0) or 0) for it in candidates}
    return _cancel_ids_from(data, valid)


def cancellation_confirmation(rows):
    """取消后的数据刷新文案（不发 speech；桌宠面板只当刷新信号，围棋端不再显示）。"""
    rows = [it for it in (rows or []) if isinstance(it, dict)]
    if not rows:
        return "日程已更新。"
    titles = "、".join("「%s」" % _clip(it.get("title", ""), 30) for it in rows[:2])
    return "好，我把%s从日程里撤掉了。" % titles


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
