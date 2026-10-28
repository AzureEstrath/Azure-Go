"""自动排班：动态收缩 + 风险约束 + 滚动时域 + 最小扰动 + 事件触发。

基线仍是正部 James-Stein：

    θ_i = target_i + (1 - (m - 2) σ² / Σ(x_j - target_j)²) · (x_i - target_i)

但 v4.1.1 起 ``target`` 不再固定为全局均值，而是**分层经验贝叶斯**：
- 每类任务先由 ``schedule_learning`` 用 EWMA 维护真实耗时；
- 样本少时向类别先验收缩，样本多时向类别均值收缩，再向全局均值收缩；
- 样本 < 3 不做 JS，只用可信度 ``n/(n+2)`` 做温和收缩。

v4.1.2 起每条日程先**自动分级**（``classify_item``）：
- 课程 / 未来已约定时间不可移动；自动安排在锁定窗内也不移动；
- 只有日期没有时刻、或有截止时间的任务按「截止驱动」参与重排；
- 错过的旧约定与待定任务才允许重新落位；旧自动安排原时间仍可行时优先保位。

排序与放置：
- **72 小时内到期**的任务走 EDF；更远的截止任务按优先级密度排序，避免下周的小事抢占今天；
- 其余按**优先级密度**（score / duration）降序；
- 固定课程 / 用户已定时间 / 锁定窗口内的自动安排是占用区间；
- 每个任务按**最早一天 + 剩余最小**的 best-fit 放入空闲段；
- 新任务对齐到本地 :00/:30；固定项前后留 ``transition_min`` 通勤 / 切换时间；
- ``risk_z`` > 0 时按机会约束把时长放大（80% 完成概率 ≈ 0.84σ）；
- ``buffer_min`` 是**每日集中弹性块**（工作窗口末尾预留，不逐任务加安全时间）；
- ``lock_sec`` 内的自动安排不再移动（计划者—执行者分离，降低心理摩擦）。

``should_replan`` 是事件驱动的重排闸门：新增待定任务 / 关键任务延误超缓冲 /
未来容量不足时才重排，避免每条新信息都全量重排。

纯逻辑，不读写数据库；时间与历史都可注入，便于门卫测试。
"""

import datetime
import math
import re
import time

from . import schedule_learning

DEFAULT_WORK_START = "08:00"
DEFAULT_WORK_END = "22:00"
DEFAULT_DURATION = 60
DEFAULT_PRIORITY = 50
PRIOR_DURATION_VAR = 45.0 ** 2
PRIOR_PRIORITY_VAR = 18.0 ** 2
JS_FLOOR = 0.15
MIN_SLOT_MIN = 10
DEFAULT_BUFFER_MIN = 45
DEFAULT_LOCK_SEC = 12 * 3600
DEFAULT_RISK_Z = 0.0          # 纯函数默认不放大；生产配置由 AzureConfig 传入 0.84
MAX_RISK_FACTOR = 1.35
DEFAULT_TRANSITION_MIN = 0    # 纯函数默认不额外留白；生产配置默认 10 分钟
DEFAULT_ALIGN_MIN = 5         # 纯函数默认 5 分钟对齐；生产配置默认 30 分钟

# —— 日程自动分级（v4.1.2）——
# 分级只决定「这次排班能不能动」，不改变用户已经写下的时间。
GRADE_FIXED = "fixed"          # 课程 / 周课表：固定，绝不被自动排班移动
GRADE_COMMITTED = "committed"  # 用户已定时间的约定：固定，需用户自己改
GRADE_LOCKED = "locked"        # 自动安排且在未来锁定窗内：本次不移动
GRADE_DEADLINE = "deadline"    # 没有固定时间、但有截止时间：时间可动，截止不可动
GRADE_TENTATIVE = "tentative"  # 之前自动排过、锁定窗外：可重排，但优先保持原位
GRADE_FLEXIBLE = "flexible"    # 完全待定：按优先级密度与空闲区间放置
GRADE_CLOSED = "closed"        # 已完成 / 已取消 / 已归档：不参与排班

GRADE_LABEL = {
    GRADE_FIXED: "固定课程",
    GRADE_COMMITTED: "已约定",
    GRADE_LOCKED: "锁定中",
    GRADE_DEADLINE: "截止驱动",
    GRADE_TENTATIVE: "可调整",
    GRADE_FLEXIBLE: "待安排",
    GRADE_CLOSED: "已结束",
}


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _time_minutes(text, default):
    m = re.match(r"^(\d{1,2})\s*:\s*(\d{1,2})$", str(text or "").strip())
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if h < 0 or h > 23 or mi < 0 or mi > 59:
        return default
    return h * 60 + mi


def _date_only(item):
    """只有日期、没有具体时刻的条目（when_text 带「全天」，且没有任何 HH:MM）。"""
    when = str(item.get("when_text", "") or "")
    if "全天" in when:
        return True
    if str(item.get("start_time", "") or "").strip() != "":
        return False
    # update_schedule_plan 会把 when_text 写成「日期 08:00-09:00（自动安排）」：
    # 只要出现了钟点，就不再是「全天」。
    if re.search(r"\d{1,2}\s*:\s*\d{1,2}", when):
        return False
    return bool(re.search(r"\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}", when))


def classify_item(item, now=None, lock_sec=0.0):
    """给一条日程定「刚性等级」：固定 / 已约定 / 锁定 / 截止 / 可调 / 待安排 / 结束。

    规则刻意简单、可解释（UI 与排班共用同一套判定）：
    - 课程（kind=course，或带星期 / 节次）→ ``fixed``，自动排班永不移动；
    - 有明确开始时间且还没过去、又不是自动排的 → ``committed``，属于用户已定下的约定；
    - 自动排过、在未来锁定窗内 → ``locked``；
    - 只有日期 / 没有时刻，或没有固定时间但有截止时间 → ``deadline``；
    - 自动排过、锁定窗外 → ``tentative``（可重排，但优先留在原位）；
    - 其余 → ``flexible``；完成 / 取消 / 归档 → ``closed``。
    """
    if not isinstance(item, dict):
        return GRADE_CLOSED
    if bool(item.get("archived")):
        return GRADE_CLOSED
    status = str(item.get("status", "planned") or "planned").strip().lower()
    if status in ("done", "cancelled", "canceled"):
        return GRADE_CLOSED
    kind = str(item.get("kind", "task") or "task").strip().lower()
    period = str(item.get("period", "") or "").strip()
    weekday = _int(item.get("weekday", -1), -1)
    if kind == "course" or period != "" or 0 <= weekday <= 6:
        return GRADE_FIXED
    start = _num(item.get("start_ts"), 0.0)
    auto = bool(item.get("auto_planned"))
    date_only = _date_only(item)
    ref = _num(now, time.time()) or time.time()
    # 用户定下的时间只要还没过去（含刚好到点）就是不可动的约定；错过的任务允许重排。
    if start >= ref and not auto and not date_only:
        return GRADE_COMMITTED
    lock = max(0.0, _num(lock_sec, 0.0))
    if auto and start > 0.0 and start > ref and (start - ref) <= lock:
        return GRADE_LOCKED
    if date_only or _num(item.get("deadline_ts"), 0.0) > 0.0:
        return GRADE_DEADLINE
    if auto:
        return GRADE_TENTATIVE
    return GRADE_FLEXIBLE


def decorate_items(items, now=None, lock_sec=0.0):
    """给 API 返回的日程补上 ``grade`` / ``grade_label``（不改库、不改入参）。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            out.append(it)
            continue
        d = dict(it)
        grade = classify_item(it, now, lock_sec)
        d["grade"] = grade
        d["grade_label"] = GRADE_LABEL.get(grade, grade)
        out.append(d)
    return out


def james_stein_shrink(values, prior_mean, prior_var, floor=JS_FLOOR):
    """正部 James-Stein 收缩；``len(values) < 3`` 时原样返回。

    ``prior_var`` 是**噪声方差**的估计（不是样本方差）：它越大，越相信个体值；
    越小，越把整批拉向均值。因子截断到 ``[floor, 1]``，保留一点个体差异。
    """
    vals = [_num(v) for v in (values or [])]
    n = len(vals)
    if n < 3:
        return vals
    mean = sum(vals) / n
    ss = sum((v - mean) ** 2 for v in vals)
    if ss <= 1e-9:
        return vals
    try:
        pv = max(0.0, float(prior_var))
    except (TypeError, ValueError):
        pv = 0.0
    factor = 1.0 - (n - 2) * pv / ss
    factor = _clamp(factor, max(0.0, min(1.0, floor)), 1.0)
    return [mean + factor * (v - mean) for v in vals]


def shrink_toward(values, targets, prior_var, floor=JS_FLOOR):
    """向**各自目标**收缩的 JS：``target_i + factor·(x_i - target_i)``。

    样本 < 3 时不做 JS（返回原值），由调用方的可信度权重负责温和收缩。
    """
    vals = [_num(v) for v in (values or [])]
    tgts = [_num(t) for t in (targets or [])]
    n = len(vals)
    if n < 3 or len(tgts) != n:
        return vals
    ss = sum((v - t) ** 2 for v, t in zip(vals, tgts))
    if ss <= 1e-9:
        return vals
    try:
        pv = max(0.0, float(prior_var))
    except (TypeError, ValueError):
        pv = 0.0
    factor = 1.0 - (n - 2) * pv / ss
    factor = _clamp(factor, max(0.0, min(1.0, floor)), 1.0)
    return [t + factor * (v - t) for v, t in zip(vals, tgts)]


def _history_stats(history, key, prior_mean, prior_var):
    """历史任务 → (均值, 噪声方差)；不足 3 条时退回先验。"""
    vals = []
    for it in history or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("kind", "task")) != "task":
            continue
        if not (bool(it.get("archived")) or str(it.get("status", "")) in ("done", "cancelled")):
            continue
        v = _num(it.get(key), 0.0)
        if key == "duration_min":
            # 有真实耗时就以真实耗时为准（学习闭环），没有才退回计划时长。
            actual = _num(it.get("actual_min"), 0.0)
            if actual > 0.0:
                v = actual
        if v > 0:
            vals.append(v)
    if len(vals) < 3:
        return float(prior_mean), float(prior_var)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
    return mean, max(var, float(prior_var) * 0.25)


def _urgency(now, deadline_ts, horizon_h=72.0):
    """截止越近越接近 1；无截止给 0.30 的常态权重。"""
    dl = _num(deadline_ts, 0.0)
    if dl <= 0:
        return 0.30
    hours = max(0.0, (dl - now) / 3600.0)
    return _clamp(1.0 - hours / float(horizon_h), 0.0, 1.0)


def _score(priority, urgency, pref_bonus=0.0):
    """优先级 0-100 + 紧迫度 0-1 + 类别偏好（很小） → 综合分 0-1。"""
    base = 0.62 * _clamp(priority, 0, 100) / 100.0 + 0.38 * _clamp(urgency, 0.0, 1.0)
    return _clamp(base + 0.02 * _clamp(pref_bonus, -5.0, 5.0), 0.0, 1.0)


def _ceil_ts(ts, step_sec=300):
    step = max(1, int(step_sec))
    return float(int(math.ceil(float(ts) / step)) * step)


def _ceil_local(ts, step_sec=1800):
    """按**本地挂钟**向上取整（08:07 → 08:30），避免时区偏移不是整小时时错位。"""
    step = max(1, int(step_sec))
    try:
        dt = datetime.datetime.fromtimestamp(float(ts))
    except (ValueError, OverflowError, OSError):
        return _ceil_ts(ts, step)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = (dt - midnight).total_seconds()
    rounded = math.ceil(offset / step) * step
    try:
        return (midnight + datetime.timedelta(seconds=rounded)).timestamp()
    except (ValueError, OverflowError):
        return _ceil_ts(ts, step)


def _day_bounds(day_index, ref, start_min, end_min, buffer_min=0):
    base = datetime.datetime.fromtimestamp(ref)
    d = base.date() + datetime.timedelta(days=int(day_index))
    end_min = max(start_min + MIN_SLOT_MIN, end_min - max(0, int(buffer_min)))
    if end_min <= start_min:
        return None
    try:
        a = datetime.datetime.combine(d, datetime.time(start_min // 60, start_min % 60))
        b = datetime.datetime.combine(d, datetime.time(end_min // 60, end_min % 60))
    except ValueError:
        return None
    start, end = a.timestamp(), b.timestamp()
    if day_index == 0:
        start = max(start, _ceil_ts(ref))
    return (start, end) if end > start else None


def _free_slots(start, end, busy):
    """从 [start,end] 里扣掉 busy 区间，返回可用段（按开始时间升序）。"""
    out = []
    cursor = float(start)
    for b0, b1 in sorted((float(a), float(b)) for a, b in busy):
        if b1 <= cursor:
            continue
        if b0 >= end:
            break
        if b0 > cursor:
            out.append((cursor, min(b0, end)))
        cursor = max(cursor, b1)
        if cursor >= end:
            break
    if cursor < end:
        out.append((cursor, end))
    return [(a, b) for a, b in out if b - a >= MIN_SLOT_MIN * 60]


def _fmt_range(start_ts, duration_min):
    a = datetime.datetime.fromtimestamp(start_ts)
    b = datetime.datetime.fromtimestamp(start_ts + duration_min * 60)
    return "%s %s-%s（自动安排）" % (a.date().isoformat(), a.strftime("%H:%M"), b.strftime("%H:%M"))


def _category_target(item, global_mean, global_var, targets):
    """某任务的收缩目标：类别均值（可信度加权）+ 类别先验 / 全局均值兜底。"""
    cat = str(item.get("task_type") or schedule_learning.infer_task_type(
        item.get("title", ""), item.get("kind", "task"), item.get("notes", "")))
    t = (targets or {}).get(cat)
    if isinstance(t, dict):
        n = max(0, _int(t.get("samples"), 0))
        w = n / (n + 3.0)
        mean = w * _num(t.get("mean"), global_mean) + (1.0 - w) * global_mean
        var = w * _num(t.get("var"), global_var) + (1.0 - w) * global_var
        return cat, mean, max(1.0, var), _num(t.get("bonus"), 0.0), n
    prior = schedule_learning.category_prior(cat)
    # 没有类别记录：先验与全局均值各占一半（新库也不至于把任务全按 60 分钟）。
    return cat, 0.5 * prior + 0.5 * global_mean, max(1.0, global_var), 0.0, 0


def _shrink_group(rows, global_var):
    """同类别内做 JS；没有类别样本时完全相信本次估计，有样本才向类别均值靠。"""
    groups = {}
    for row in rows:
        groups.setdefault(row["cat"], []).append(row)
    for cat, group in groups.items():
        sampled = [r for r in group if r["category_samples"] > 0]
        # 无样本的类别目标只是先验，不能覆盖用户/资料给出的时长。
        for r in group:
            if r["category_samples"] <= 0:
                r["shrunk_duration"] = r["raw_duration"]
        if len(sampled) >= 3:
            vals = shrink_toward([r["raw_duration"] for r in sampled],
                                 [r["target_mean"] for r in sampled],
                                 max(1.0, sum(r["target_var"] for r in sampled) / len(sampled)))
            for r, v in zip(sampled, vals):
                r["shrunk_duration"] = v
        else:
            for r in sampled:
                n = r["category_samples"]
                # 1~2 条样本按 n/(n+2) 温和收缩，不把单次估计直接拉到类别均值。
                w = n / (n + 2.0)
                r["shrunk_duration"] = r["target_mean"] + w * (r["raw_duration"] - r["target_mean"])
    return groups


def _risk_duration(base_min, target_var, risk_z):
    """机会约束：``mean + z·σ``（上限 1.35 倍），返回放大后的分钟数（5 的倍数）。"""
    base = max(5.0, _num(base_min, 60.0))
    try:
        z = max(0.0, float(risk_z))
    except (TypeError, ValueError):
        z = 0.0
    if z <= 0:
        return int(round(base / 5.0) * 5)
    cv = _clamp(math.sqrt(max(1.0, _num(target_var, 900.0))) / base, 0.05, 0.8)
    factor = _clamp(1.0 + z * cv, 1.0, MAX_RISK_FACTOR)
    return max(5, int(round(base * factor / 5.0) * 5))


def plan(items, now=None, days=7, work_start=DEFAULT_WORK_START,
         work_end=DEFAULT_WORK_END, history=None, category_stats=None,
         risk_z=DEFAULT_RISK_Z, buffer_min=0, lock_sec=0,
         transition_min=DEFAULT_TRANSITION_MIN, align_min=DEFAULT_ALIGN_MIN):
    """给未定时间 / 之前自动排过的任务分配时间；返回 assignments / unscheduled / stats。"""
    ref = _num(now, time.time()) or time.time()
    try:
        horizon = max(1, min(30, int(days)))
    except (TypeError, ValueError):
        horizon = 7
    ws = _time_minutes(work_start, 8 * 60)
    we = _time_minutes(work_end, 22 * 60)
    if we <= ws + MIN_SLOT_MIN:
        ws, we = 8 * 60, 22 * 60
    try:
        buffer = max(0, min(240, int(buffer_min)))
    except (TypeError, ValueError):
        buffer = 0
    try:
        lock = max(0.0, float(lock_sec))
    except (TypeError, ValueError):
        lock = 0.0
    try:
        transition = max(0, min(60, int(transition_min)))
    except (TypeError, ValueError):
        transition = 0
    try:
        align = max(1, min(60, int(align_min)))
    except (TypeError, ValueError):
        align = 5

    fixed, flexible = [], []
    grade_counts = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        grade = classify_item(it, ref, lock)
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        if grade == GRADE_CLOSED:
            continue
        start = _num(it.get("start_ts"), 0.0)
        dur = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60)
        auto = bool(it.get("auto_planned"))
        fixed_like = grade in (GRADE_FIXED, GRADE_COMMITTED, GRADE_LOCKED)
        # 滚动时域：已有自动安排若落在本次窗口之外，就不拉回来重排（最小扰动）。
        beyond = grade == GRADE_TENTATIVE and auto and start > ref + horizon * 86400.0
        if fixed_like or beyond:
            if start > 0.0:
                pad = transition * 60.0
                fixed.append((start - pad, start + dur * 60.0 + pad))
            continue
        row = dict(it)
        row["_grade"] = grade
        flexible.append(row)

    base_stats = {
        "tasks": 0, "buffer_min": buffer, "risk_z": round(_num(risk_z, 0.0), 2),
        "transition_min": transition, "align_min": align,
        "grades": grade_counts,
    }
    if not flexible:
        return {"assignments": [], "unscheduled": [], "fixed": len(fixed),
                "stats": base_stats}

    hist = list(history or [])
    d_mean, d_var = _history_stats(hist, "duration_min", DEFAULT_DURATION, PRIOR_DURATION_VAR)
    p_mean, p_var = _history_stats(hist, "priority", DEFAULT_PRIORITY, PRIOR_PRIORITY_VAR)
    targets = schedule_learning.plan_targets(category_stats or {})

    enriched = []
    for it in flexible:
        cat, t_mean, t_var, bonus, n = _category_target(it, d_mean, d_var, targets)
        raw_d = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60)
        raw_p = _clamp(_int(it.get("priority"), DEFAULT_PRIORITY), 0, 100)
        enriched.append({
            "item": it, "id": _int(it.get("id"), 0), "title": str(it.get("title", "")),
            "cat": cat, "category_samples": n, "target_mean": t_mean, "target_var": t_var,
            "pref_bonus": bonus, "raw_duration": raw_d, "raw_priority": raw_p,
            "grade": it.get("_grade") or GRADE_FLEXIBLE,
        })

    groups = _shrink_group(enriched, d_var)
    raw_p_all = [r["raw_priority"] for r in enriched]
    shr_p = james_stein_shrink(raw_p_all, p_mean, p_var)
    for row, pv in zip(enriched, shr_p):
        row["priority"] = _clamp(int(round(pv)), 0, 100)
        base = _clamp(int(round(row["shrunk_duration"] / 5.0)) * 5, 5, 24 * 60)
        row["base_duration"] = base
        row["duration"] = _risk_duration(base, row["target_var"], risk_z)
        dl = _num(row["item"].get("deadline_ts"), 0.0)
        row["deadline_ts"] = dl
        row["score"] = _score(row["priority"], _urgency(ref, dl), row["pref_bonus"])
        row["density"] = row["score"] / float(max(5, row["duration"]))

    # EDF 只抢「72 小时内到期」的任务；更远的截止任务按优先级密度参与常规排序，
    # 避免一个下周才到期的小任务占掉今天最好的时段。
    deadline_rows = [x for x in enriched if x["deadline_ts"] > 0]
    near_deadline = [x for x in deadline_rows if _urgency(ref, x["deadline_ts"]) > 0.0]
    far_deadline = [x for x in deadline_rows if _urgency(ref, x["deadline_ts"]) <= 0.0]
    other_rows = [x for x in enriched if x["deadline_ts"] <= 0] + far_deadline
    near_deadline.sort(key=lambda x: (x["deadline_ts"], -x["score"], -x["priority"]))
    other_rows.sort(key=lambda x: (-x["density"], -x["score"], -x["priority"],
                                   x["deadline_ts"] if x["deadline_ts"] > 0 else float("inf")))
    ordered = near_deadline + other_rows

    busy = list(fixed)
    assignments, unscheduled = [], []
    for row in ordered:
        dur_sec = row["duration"] * 60.0
        dl = row["deadline_ts"]
        existing = _num(row["item"].get("start_ts"), 0.0) if row["item"].get("auto_planned") else 0.0
        if existing <= ref:
            existing = 0.0
        best = None
        fallback = None
        # 推迟过的任务优先排到明天（「推迟」≠ 原地再排一次）；只有未来没有别的空档才回到今天。
        day_order = list(range(horizon))
        if _int(row["item"].get("defer_count"), 0) > 0 and horizon > 1:
            day_order = list(range(1, horizon)) + [0]
        for rank, day in enumerate(day_order):
            bounds = _day_bounds(day, ref, ws, we, buffer)
            if bounds is None:
                continue
            slots = _free_slots(bounds[0], bounds[1], busy)
            for s0, s1 in slots:
                if s1 - s0 < dur_sec:
                    continue
                # 先看「保持原位」：之前自动排过、锁定窗外的任务，原时间仍可用就不移动。
                if existing > 0.0 and existing >= s0 - 1e-6 and \
                        existing + dur_sec <= s1 + 1e-6 and \
                        (dl <= 0 or existing + dur_sec <= dl + 1e-6):
                    cand = (0, rank, (s1 - existing) - dur_sec, existing)
                    if best is None or cand < best:
                        best = cand
                # 新任务对齐到 :00 / :30，接近真实日历习惯；放不下时才用 5 分钟粒度。
                start = _ceil_local(s0, align * 60)
                if start + dur_sec > s1 + 1e-6:
                    start = _ceil_ts(s0, 5 * 60)
                if start + dur_sec > s1 + 1e-6:
                    continue
                if dl > 0 and start + dur_sec > dl + 1e-6:
                    if fallback is None:
                        fallback = (start, start + dur_sec)
                    continue
                same = 0 if existing > 0.0 and abs(start - existing) <= 60.0 else 1
                leftover = (s1 - start) - dur_sec
                cand = (same, rank, leftover, start)
                if best is None or cand < best:
                    best = cand
        if best is None and fallback is not None:
            best = (0, 0, 0.0, fallback[0])
        if best is None:
            unscheduled.append({"id": row["id"], "title": row["title"],
                                "reason": "未来 %d 天没有足够的空闲" % horizon})
            continue
        start = best[3]
        end = start + dur_sec
        late = bool(dl > 0 and start + dur_sec > dl + 1e-6)
        busy.append((start, end))
        assignments.append({
            "id": row["id"], "title": row["title"], "start_ts": start,
            "when_text": _fmt_range(start, row["duration"]) + ("（已超截止）" if late else ""),
            "duration_min": row["base_duration"], "risk_min": row["duration"],
            "priority": row["priority"], "task_type": row["cat"],
            "grade": row["grade"], "grade_label": GRADE_LABEL.get(row["grade"], row["grade"]),
            "score": round(row["score"], 4), "late": late,
        })

    return {
        "assignments": assignments,
        "unscheduled": unscheduled,
        "fixed": len(fixed),
        "stats": {
            "tasks": len(flexible),
            "prior_duration": round(d_mean, 1),
            "prior_priority": round(p_mean, 1),
            "duration_variance": round(d_var, 1),
            "priority_variance": round(p_var, 1),
            "buffer_min": buffer,
            "risk_z": round(_num(risk_z, 0.0), 2),
            "transition_min": transition,
            "align_min": align,
            "grades": grade_counts,
            "locked": sum(1 for x in items or [] if isinstance(x, dict) and x.get("auto_planned")),
            "shrunk_duration_min": min((x["base_duration"] for x in enriched), default=0),
            "shrunk_duration_max": max((x["base_duration"] for x in enriched), default=0),
            "risk_duration_min": min((x["duration"] for x in enriched), default=0),
            "risk_duration_max": max((x["duration"] for x in enriched), default=0),
            "groups": {cat: len(g) for cat, g in groups.items()},
        },
    }


def _fixed_intervals(items, pad_min=0):
    """已承诺 / 锁定 / 课程区间（可加通勤与切换留白）；只计未归档且仍 planned 的条目。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or bool(it.get("archived")):
            continue
        if str(it.get("status", "planned") or "planned") != "planned":
            continue
        # 全天截止任务没有固定占用；它的 09:00 只是排序锚点，不应算进容量。
        if str(it.get("kind", "task")) != "course" and \
                not bool(it.get("auto_planned")) and _date_only(it):
            continue
        start = _num(it.get("start_ts"), 0.0)
        if start <= 0.0:
            continue
        dur = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60)
        pad = max(0, _int(pad_min, 0)) * 60.0
        out.append((start - pad, start + dur * 60.0 + pad))
    return out


def should_replan(items, now=None, horizon_days=3, work_start=DEFAULT_WORK_START,
                  work_end=DEFAULT_WORK_END, buffer_min=DEFAULT_BUFFER_MIN,
                  grace_min=10, capacity_ratio=0.85, transition_min=0):
    """事件驱动闸门：只在「需要」时重排。

    返回 ``{"trigger": bool, "reasons": [...], "free_min":..., "demand_min":...}``。
    条件（任一满足即触发）：
    - 有未定时间的任务（且其中含 24h 内截止或优先级 ≥ 85 的）；
    - 任务已超过「开始 + 计划时长 + 宽限」且优先级 ≥ 70，或延误超过每日缓冲；
    - 未来滚动窗口内高优先级任务的总时长超过可用空闲的 85%。
    """
    ref = _num(now, time.time()) or time.time()
    try:
        horizon = max(1, min(7, int(horizon_days)))
    except (TypeError, ValueError):
        horizon = 3
    ws = _time_minutes(work_start, 8 * 60)
    we = _time_minutes(work_end, 22 * 60)
    try:
        buffer = max(0, min(240, int(buffer_min)))
    except (TypeError, ValueError):
        buffer = 0
    try:
        grace = max(0.0, float(grace_min)) * 60.0
    except (TypeError, ValueError):
        grace = 600.0
    reasons = []
    unplanned = []
    overdue = []
    demand = 0.0
    for it in items or []:
        if not isinstance(it, dict) or bool(it.get("archived")):
            continue
        if str(it.get("status", "planned")) != "planned":
            continue
        if str(it.get("kind", "task")) != "task":
            continue
        start = _num(it.get("start_ts"), 0.0)
        dur = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60) * 60.0
        pri = _clamp(_int(it.get("priority"), DEFAULT_PRIORITY), 0, 100)
        dl = _num(it.get("deadline_ts"), 0.0)
        # 只有日期 / 没有时刻的任务视为「全天截止」：09:00 只是显示锚点，不能当作已占用。
        if start <= 0.0 or (not bool(it.get("auto_planned")) and _date_only(it)):
            unplanned.append(it)
            if pri >= 70 or (dl > 0 and dl - ref <= 24 * 3600.0):
                demand += dur
            continue
        if ref > start + dur + grace:
            overdue.append(it)
            delay = ref - (start + dur)
            if pri >= 70 or delay > max(30, buffer) * 60.0:
                reasons.append("overdue")
                demand += dur
        elif pri >= 70:
            demand += dur
    if unplanned:
        urgent = [x for x in unplanned
                  if _clamp(_int(x.get("priority"), DEFAULT_PRIORITY), 0, 100) >= 85
                  or (_num(x.get("deadline_ts"), 0.0) > 0
                      and _num(x.get("deadline_ts"), 0.0) - ref <= 24 * 3600.0)]
        reasons.append("urgent_unplanned" if urgent else "unplanned")
    fixed = _fixed_intervals(items, transition_min)
    free = 0.0
    for day in range(horizon):
        bounds = _day_bounds(day, ref, ws, we, buffer)
        if bounds is None:
            continue
        for s0, s1 in _free_slots(bounds[0], bounds[1], fixed):
            free += (s1 - s0) / 60.0
    capacity = max(0.0, free) * max(0.5, min(1.0, float(capacity_ratio)))
    if demand > capacity and demand > 0:
        reasons.append("capacity")
    reasons = list(dict.fromkeys(reasons))
    return {
        "trigger": bool(reasons),
        "reasons": reasons,
        "free_min": round(free, 1),
        "demand_min": round(demand, 1),
        "unplanned": len(unplanned),
        "overdue": len(overdue),
        "unplanned_ids": sorted(_int(x.get("id"), 0) for x in unplanned
                                if _int(x.get("id"), 0) > 0),
        "overdue_ids": sorted(_int(x.get("id"), 0) for x in overdue
                              if _int(x.get("id"), 0) > 0),
    }
