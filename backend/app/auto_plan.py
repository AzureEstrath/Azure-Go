"""自动排班：动态收缩 + 风险约束 + 滚动时域 + 最小扰动 + 事件触发。

基线仍是正部 James-Stein：

    θ_i = target_i + (1 - (m - 2) σ² / Σ(x_j - target_j)²) · (x_i - target_i)

但 v4.1.1 起 ``target`` 不再固定为全局均值，而是**分层经验贝叶斯**：
- 每类任务先由 ``schedule_learning`` 用 EWMA 维护真实耗时；
- 样本少时向类别先验收缩，样本多时向类别均值收缩，再向全局均值收缩；
- 样本 < 3 不做 JS，只用可信度 ``(n+1)/(n+3)`` 做温和收缩。

排序与放置：
- 有截止时间的任务走 **EDF**；
- 其余按**优先级密度**（score / duration）降序；
- 固定课程 / 用户已定时间 / 锁定窗口内的自动安排是占用区间；
- 每个任务按**最早一天 + 剩余最小**的 best-fit 放入空闲段；
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
         risk_z=DEFAULT_RISK_Z, buffer_min=0, lock_sec=0):
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

    fixed, flexible = [], []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if bool(it.get("archived")) or str(it.get("status", "planned")) != "planned":
            continue
        start = _num(it.get("start_ts"), 0.0)
        dur = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60)
        period = str(it.get("period", "") or "")
        wd = _int(it.get("weekday", -1), -1)
        auto = bool(it.get("auto_planned"))
        committed = start > 0.0 and not auto
        locked = auto and start > ref and (start - ref) <= lock
        # 滚动时域：已有自动安排若落在本次窗口之外，就不拉回来重排（最小扰动）。
        beyond = auto and start > ref + horizon * 86400.0
        if period != "" or (0 <= wd <= 6) or committed or locked or beyond:
            if start > 0.0:
                fixed.append((start, start + dur * 60.0))
            continue
        flexible.append(dict(it))

    if not flexible:
        return {"assignments": [], "unscheduled": [], "fixed": len(fixed),
                "stats": {"tasks": 0, "buffer_min": buffer, "risk_z": _num(risk_z, 0.0)}}

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

    # EDF 先排截止任务；其余按优先级密度（加权最短处理时间）。
    deadline_rows = [x for x in enriched if x["deadline_ts"] > 0]
    other_rows = [x for x in enriched if x["deadline_ts"] <= 0]
    deadline_rows.sort(key=lambda x: (x["deadline_ts"], -x["score"], -x["priority"]))
    other_rows.sort(key=lambda x: (-x["density"], -x["score"], -x["priority"]))
    ordered = deadline_rows + other_rows

    busy = list(fixed)
    assignments, unscheduled = [], []
    for row in ordered:
        dur_sec = row["duration"] * 60.0
        dl = row["deadline_ts"]
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
                if dl > 0 and s0 + dur_sec > dl + 1e-6:
                    if fallback is None:
                        fallback = (s0, s0 + dur_sec)
                    continue
                leftover = (s1 - s0) - dur_sec
                cand = (rank, leftover, s0, s0 + dur_sec)
                if best is None or cand < best:
                    best = cand
        if best is None and fallback is not None:
            best = (0, 0.0, fallback[0], fallback[1])
        if best is None:
            unscheduled.append({"id": row["id"], "title": row["title"],
                                "reason": "未来 %d 天没有足够的空闲" % horizon})
            continue
        start = _ceil_ts(best[2])
        end = start + dur_sec
        late = bool(dl > 0 and start + dur_sec > dl + 1e-6)
        busy.append((start, end))
        assignments.append({
            "id": row["id"], "title": row["title"], "start_ts": start,
            "when_text": _fmt_range(start, row["duration"]) + ("（已超截止）" if late else ""),
            "duration_min": row["base_duration"], "risk_min": row["duration"],
            "priority": row["priority"], "task_type": row["cat"],
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
            "locked": sum(1 for x in items or [] if isinstance(x, dict) and x.get("auto_planned")),
            "shrunk_duration_min": min((x["base_duration"] for x in enriched), default=0),
            "shrunk_duration_max": max((x["base_duration"] for x in enriched), default=0),
            "risk_duration_min": min((x["duration"] for x in enriched), default=0),
            "risk_duration_max": max((x["duration"] for x in enriched), default=0),
            "groups": {cat: len(g) for cat, g in groups.items()},
        },
    }


def _fixed_intervals(items):
    out = []
    for it in items or []:
        if not isinstance(it, dict) or bool(it.get("archived")):
            continue
        start = _num(it.get("start_ts"), 0.0)
        if start <= 0.0:
            continue
        dur = _clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60)
        out.append((start, start + dur * 60.0))
    return out


def should_replan(items, now=None, horizon_days=3, work_start=DEFAULT_WORK_START,
                  work_end=DEFAULT_WORK_END, buffer_min=DEFAULT_BUFFER_MIN,
                  grace_min=10, capacity_ratio=0.85):
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
        if start <= 0.0:
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
    fixed = _fixed_intervals(items)
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
