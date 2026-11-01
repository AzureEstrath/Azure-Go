"""考试复习规划：Azure 自己排的「学习计划」（v4.7.0）。

与用户日程的区别：
- 条目 ``owner="azure"`` + 同一个 ``plan_group``；日程面板会标「规划」并给整组取消按钮；
- 自动排班把这类条目标成 ``locked``，不会在下一轮重排时把它们挪走；
- 计划只在**空闲段**里落位，不改用户的课程 / 约定 / 已有任务。

触发：聊天或资料扫描新增「考试 / 测验 / 期末 / 结业 / 补考…」条目后，由 ``ensure`` 生成。
范围：能从备注里读到「范围：…」就按范围切分重点；读不到就按整体复习的通用阶段排，
并返回 ``asked_scope=True`` 让调用方问一次——用户不答就算了，不反复追问。
"""

import datetime
import math
import re
import time

from . import auto_plan, schedule_learning

EXAM_RE = re.compile(r"(考试|测验|期末|期中|结业|补考|考核|exam|quiz)", re.I)
SCOPE_RE = re.compile(r"(?:考试)?(?:范围|考点|内容|章节|单元|单词|语法|阅读|听力|写作)"
                      r"\s*(?:是|为|:|：)?\s*([^。；\n]{2,120})")
MAX_TITLE = 100
MAX_NOTES = 300
MIN_SESSION_MIN = 30


def is_exam(item):
    """这条日程是不是考试 / 测验（标题或备注命中；课程表条目不算）。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("kind") or "") == "course" or str(item.get("period") or "") != "":
        return False
    text = " ".join(str(item.get(k) or "") for k in ("title", "notes"))
    return bool(EXAM_RE.search(text))


def exam_ts(item):
    """考试时间：优先截止 / 开始时间；没有返回 0。"""
    if not isinstance(item, dict):
        return 0.0
    for key in ("deadline_ts", "start_ts"):
        try:
            v = float(item.get(key) or 0.0)
        except (TypeError, ValueError, OverflowError):
            v = 0.0
        if math.isfinite(v) and v > 0.0:
            return v
    return 0.0


def plan_group(item):
    """稳定的规划组 id：同一场考试重复触发时认得出是同一组。"""
    if not isinstance(item, dict):
        return ""
    sid = 0
    try:
        sid = int(item.get("id") or 0)
    except (TypeError, ValueError):
        sid = 0
    if sid > 0:
        return "exam:%d" % sid
    ts = exam_ts(item)
    return "exam:%d" % int(ts) if ts > 0 else ""


def scope_text(item):
    """从备注里读考试范围；读不到返回空串（不猜）。"""
    notes = str((item or {}).get("notes") or "").strip()
    if notes == "":
        return ""
    m = SCOPE_RE.search(notes)
    if m:
        return m.group(1).strip(" ：:，,。；;")
    return ""


def scope_parts(scope):
    """把「Unit 1、Unit 2、语法」这类范围拆成重点；拆不出 / 太多就返回空。"""
    s = str(scope or "").strip()
    if s == "":
        return []
    parts = [x.strip(" ：:，,。；;（）()") for x in re.split(r"[、,，；;/\n]+", s)]
    parts = [x for x in parts if 1 < len(x) <= 24]
    return parts[:8] if len(parts) >= 2 else []


def _fmt(start_ts, duration_min):
    a = datetime.datetime.fromtimestamp(start_ts)
    b = datetime.datetime.fromtimestamp(start_ts + duration_min * 60)
    return "%s %s-%s（Azure 规划）" % (a.date().isoformat(), a.strftime("%H:%M"), b.strftime("%H:%M"))


def _ceil_5(ts):
    try:
        a = datetime.datetime.fromtimestamp(float(ts))
        sec = (a.second + a.minute * 60)
        return (a - datetime.timedelta(seconds=sec)).timestamp() + math.ceil(
            sec / 300.0) * 300.0
    except (OverflowError, OSError, ValueError):
        return float(ts)


def _day_key(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _day_types(items, exclude_group=""):
    """每个日期已有哪些任务类别（用于「常一起出现」的日期偏好）。"""
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if exclude_group and str(it.get("plan_group") or "") == exclude_group:
            continue
        if str(it.get("kind") or "") == "course":
            continue
        day = str(it.get("date") or "")[:10] or _day_key(it.get("start_ts"))
        if day == "":
            continue
        cat = str(it.get("task_type") or "").strip().lower()
        if cat == "":
            cat = schedule_learning.infer_task_type(
                it.get("title", ""), "task", it.get("notes", ""))
        if cat:
            out.setdefault(day, set()).add(cat)
    return out


def _assoc_day_bonus(assoc, day_types, day):
    """某个日期是否与「学习」常一起出现：只加很小的分，不改变硬约束。"""
    if not assoc:
        return 0.0
    types = set(day_types.get(day, set())) | {"study"}
    for row in assoc:
        if not isinstance(row, dict):
            continue
        items = set(str(x) for x in row.get("items") or [])
        if "study" in items and (items & types):
            try:
                return min(0.25, 0.08 * float(row.get("confidence", 0.0)))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _pick_slots(free, items, assoc, count, duration_min):
    """从空闲段里挑 count 个不同日期、分布开、靠近晚上的 50 分钟段。"""
    if not free:
        return []
    day_types = _day_types(items)
    by_day = {}
    for day, start, end in free:
        if end - start < min(duration_min, MIN_SESSION_MIN) * 60:
            continue
        by_day.setdefault(day, []).append((start, end))
    days = sorted(by_day)
    if not days:
        return []
    # 不要「从明天起每天一次」：在可用的日期里均匀取 count 个，近考试更密。
    n = min(max(1, int(count)), len(days))
    if len(days) > n:
        picks = []
        for i in range(n):
            idx = int(round((i + 0.5) * len(days) / n - 0.5))
            idx = max(0, min(len(days) - 1, idx))
            while idx in picks:
                idx -= 1
                if idx < 0:
                    idx = len(days) - 1
            picks.append(idx)
        days = [days[i] for i in picks]
    chosen = []
    for day in days:
        best = None
        best_score = -1e18
        for start, end in by_day[day]:
            begin = _ceil_5(start)
            # 长空档里优先把复习放到 18:00 之后；短空档仍从可用起点开始。
            try:
                d0 = datetime.datetime.fromtimestamp(start)
                base = d0.replace(hour=0, minute=0, second=0, microsecond=0)
                evening = (base + datetime.timedelta(hours=18)).timestamp()
                if start <= evening and end - evening >= duration_min * 60:
                    begin = evening
            except (OverflowError, OSError, ValueError):
                pass
            dur = min(duration_min, int((end - begin) // 60))
            if dur < MIN_SESSION_MIN:
                continue
            # 偏好 18:00-21:00；不硬套，找不到就在这一天最早的可用段。
            try:
                d = datetime.datetime.fromtimestamp(begin)
                base = d.replace(hour=0, minute=0, second=0, microsecond=0)
                pref_a = (base + datetime.timedelta(hours=18)).timestamp()
                pref_b = (base + datetime.timedelta(hours=21)).timestamp()
                overlap = max(0.0, min(begin + dur * 60, pref_b) - max(begin, pref_a))
            except (OverflowError, OSError, ValueError):
                overlap = 0.0
            score = overlap / 3600.0
            score += _assoc_day_bonus(assoc, day_types, _day_key(begin))
            if score > best_score:
                best_score = score
                best = (begin, dur)
        if best is not None:
            chosen.append((day, best[0], best[1]))
    return chosen[:max(1, int(count))]


def _focus_list(scope, count):
    parts = scope_parts(scope)
    if parts:
        out = []
        for i in range(count):
            out.append("重点：" + parts[i % len(parts)])
        return out
    generic = ["通读范围，列出不熟的点", "做一组题，整理错题",
               "回看错题与薄弱点", "整体串联，做一次模拟"]
    return [generic[min(i, len(generic) - 1)] for i in range(count)]


def build_plan(exam, items, now=None, weeks=3, max_sessions=6,
               duration_min=50, work_start=auto_plan.DEFAULT_WORK_START,
               work_end=auto_plan.DEFAULT_WORK_END, buffer_min=0,
               transition_min=0, assoc=None):
    """生成一场考试的复习计划；返回可直接写库的条目列表（可能为空）。"""
    if not isinstance(exam, dict) or not is_exam(exam):
        return []
    ref = float(now if now is not None else time.time())
    when = exam_ts(exam)
    if when <= ref + 3600.0:
        return []
    group = plan_group(exam)
    if group == "":
        return []
    try:
        weeks = max(1, min(8, int(weeks or 3)))
        max_sessions = max(2, min(12, int(max_sessions or 6)))
        duration = max(MIN_SESSION_MIN, min(120, int(duration_min or 50)))
    except (TypeError, ValueError):
        weeks, max_sessions, duration = 3, 6, 50
    exam_day = datetime.datetime.fromtimestamp(when).date()
    today = datetime.datetime.fromtimestamp(ref).date()
    days_to_exam = (exam_day - today).days
    if days_to_exam < 2:
        return []
    horizon = max(1, min(weeks * 7, days_to_exam - 1))
    if horizon < 1:
        return []
    count = max(1, min(max_sessions, weeks * 2, horizon))
    scope = scope_text(exam)
    free = auto_plan.free_slots(items, now=ref, days=horizon,
                                work_start=work_start, work_end=work_end,
                                buffer_min=buffer_min, transition_min=transition_min)
    chosen = _pick_slots(free, items, assoc, count, duration)
    if not chosen:
        return []
    focuses = _focus_list(scope, len(chosen))
    title0 = str(exam.get("title") or "考试").strip()[:60]
    out = []
    for i, (day, start, dur) in enumerate(chosen):
        when_text = _fmt(start, dur)
        date_text = _day_key(start)
        notes = "Azure 规划 · 第%d/%d次；%s" % (i + 1, len(chosen), focuses[i])
        if scope:
            notes += "；范围：" + scope
        else:
            notes += "；范围未说明，先按整体复习"
        out.append({
            "title": ("复习：%s · 第%d/%d次" % (title0, i + 1, len(chosen)))[:MAX_TITLE],
            "notes": notes[:MAX_NOTES],
            "start_ts": start,
            "when_text": when_text,
            "date": date_text,
            "start_time": datetime.datetime.fromtimestamp(start).strftime("%H:%M"),
            "end_time": datetime.datetime.fromtimestamp(start + dur * 60).strftime("%H:%M"),
            "kind": "task",
            "task_type": "study",
            "priority": 60,
            "duration_min": dur,
            "deadline_ts": when,
            "auto_planned": True,
            "owner": "azure",
            "plan_group": group,
            "source": "azure_plan",
        })
    return out


def ensure(store, items, cfg, now=None):
    """给新增的考试条目补 Azure 复习计划；返回 ``{groups, items, asked_scope}``。

    - 已有同组未完成计划 ⇒ 跳过（不重复生成）；
    - 范围读不到 ⇒ 先按整体排，`asked_scope=True`（调用方问一次，不反复追）；
    - 任何存储 / 计算失败都只返回空结果，不影响聊天与资料处理。
    """
    empty = {"groups": [], "items": [], "asked_scope": False}
    if store is None or not bool(getattr(cfg, "exam_plan_enabled", True)):
        return empty
    try:
        assoc = store.load_schedule_associations(12) \
            if callable(getattr(store, "load_schedule_associations", None)) else []
    except Exception:                       # noqa: BLE001
        assoc = []
    groups = []
    created = []
    working = list(items or [])
    for item in items or []:
        if not isinstance(item, dict) or str(item.get("owner") or "user") != "user":
            continue
        if not is_exam(item):
            continue
        group = plan_group(item)
        if group == "":
            continue
        try:
            check = getattr(store, "has_plan_group", None)
            if callable(check):
                exists = bool(check(group))
            else:
                exists = bool(store.has_active_plan(group))
            if exists:
                continue
        except Exception:                   # noqa: BLE001
            continue
        plan = build_plan(
            item, working, now=now,
            weeks=int(getattr(cfg, "exam_plan_weeks", 3) or 3),
            max_sessions=int(getattr(cfg, "exam_plan_max_sessions", 6) or 6),
            duration_min=int(getattr(cfg, "exam_plan_duration_min", 50) or 50),
            work_start=str(getattr(cfg, "schedule_work_start", auto_plan.DEFAULT_WORK_START)
                           or auto_plan.DEFAULT_WORK_START),
            work_end=str(getattr(cfg, "schedule_work_end", auto_plan.DEFAULT_WORK_END)
                         or auto_plan.DEFAULT_WORK_END),
            buffer_min=int(getattr(cfg, "schedule_buffer_min", 45) or 0),
            transition_min=int(getattr(cfg, "schedule_transition_min", 10) or 0),
            assoc=assoc)
        if not plan:
            continue
        try:
            saved = store.upsert_schedule(plan, "azure_plan")
        except Exception as e:              # noqa: BLE001
            print("[Plan] 复习计划写入失败（已忽略）：%s" % e)
            continue
        if not saved:
            continue
        working.extend(saved)               # 同一批里第二场考试不会和第一场的复习重叠
        groups.append({
            "group": group,
            "exam_id": int(item.get("id") or 0),
            "title": str(item.get("title") or ""),
            "count": len(saved),
            "scope_known": bool(scope_text(item)),
        })
        created.extend(saved)
    return {"groups": groups, "items": created,
            "asked_scope": any(not g.get("scope_known") for g in groups)}
