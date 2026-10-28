"""自动排班：优先级 + James-Stein 收缩 + 空闲区间打包。

为什么不是「按时间随便塞」：从文件里抽出来的时长 / 优先级都是**带噪估计**，单条数字会被
模型或 OCR 的偶然误差带偏。这里用正部 James-Stein 估计把一批任务的原始估计向全局均值收缩：

    θ_i = μ + (1 - (m - 2) σ² / Σ(x_j - μ)²) · (x_i - μ)

- ``m`` 是这批待排任务数；``m < 3`` 不收缩（JS 没有意义）；
- ``σ²`` 取**历史已完成 / 已归档任务**的池内方差；历史不足 3 条时用领域先验
  （时长 45² 分钟²、优先级 18²），保证新库也能工作；
- 因子做正部截断并保留 15% 的个体差异（工程保护：不让长任务被整体拉成均值）。

排序与放置：
- 有截止时间的任务走 **EDF**（最早截止优先），并尽量放在截止前；
- 其余按 **优先级密度** ``score / duration`` 降序（加权最短处理时间，兼顾重要性与吞吐）；
- 固定课程 / 已定日程是占用区间，逐日切出工作窗口内的空闲段；每个任务选
  **best-fit**（能放下且剩余最小的段，平局取更早），deadline 任务只在截止前找位置；
- 所有函数都是纯逻辑，时间可注入，便于门卫测试；不读写数据库。
"""

import datetime
import math
import re
import time

DEFAULT_WORK_START = "08:00"
DEFAULT_WORK_END = "22:00"
DEFAULT_DURATION = 60
DEFAULT_PRIORITY = 50
PRIOR_DURATION_VAR = 45.0 ** 2
PRIOR_PRIORITY_VAR = 18.0 ** 2
JS_FLOOR = 0.15
MIN_SLOT_MIN = 10


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


def _score(priority, urgency):
    """优先级 0-100 + 紧迫度 0-1 → 综合分 0-1。"""
    return 0.62 * _clamp(priority, 0, 100) / 100.0 + 0.38 * _clamp(urgency, 0.0, 1.0)


def _ceil_ts(ts, step_sec=300):
    step = max(1, int(step_sec))
    return float(int(math.ceil(float(ts) / step)) * step)


def _day_bounds(day_index, ref, start_min, end_min):
    base = datetime.datetime.fromtimestamp(ref)
    d = base.date() + datetime.timedelta(days=int(day_index))
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


def plan(items, now=None, days=7, work_start=DEFAULT_WORK_START,
         work_end=DEFAULT_WORK_END, history=None):
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
        flexible_row = start <= 0.0 or auto
        if period != "" or (0 <= wd <= 6) or not flexible_row:
            if start > 0.0:
                fixed.append((start, start + dur * 60.0))
            continue
        flexible.append(dict(it))

    if not flexible:
        return {"assignments": [], "unscheduled": [], "fixed": len(fixed),
                "stats": {"tasks": 0, "prior_duration": DEFAULT_DURATION,
                          "prior_priority": DEFAULT_PRIORITY}}

    hist = list(history or [])
    d_mean, d_var = _history_stats(hist, "duration_min", DEFAULT_DURATION, PRIOR_DURATION_VAR)
    p_mean, p_var = _history_stats(hist, "priority", DEFAULT_PRIORITY, PRIOR_PRIORITY_VAR)
    raw_d = [_clamp(_int(it.get("duration_min"), DEFAULT_DURATION), 5, 24 * 60) for it in flexible]
    raw_p = [_clamp(_int(it.get("priority"), DEFAULT_PRIORITY), 0, 100) for it in flexible]
    shr_d = james_stein_shrink(raw_d, d_mean, d_var)
    shr_p = james_stein_shrink(raw_p, p_mean, p_var)

    enriched = []
    for i, it in enumerate(flexible):
        dur = _clamp(int(round(shr_d[i] / 5.0)) * 5, 5, 24 * 60)
        pri = _clamp(int(round(shr_p[i])), 0, 100)
        dl = _num(it.get("deadline_ts"), 0.0)
        score = _score(pri, _urgency(ref, dl))
        enriched.append({
            "item": it, "id": _int(it.get("id"), 0), "title": str(it.get("title", "")),
            "duration": dur, "priority": pri, "deadline_ts": dl,
            "score": score, "density": score / float(max(5, dur)),
        })

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
        for day in range(horizon):
            bounds = _day_bounds(day, ref, ws, we)
            if bounds is None:
                continue
            slots = _free_slots(bounds[0], bounds[1], busy)
            for s0, s1 in slots:
                if s1 - s0 < dur_sec:
                    continue
                if dl > 0 and s0 + dur_sec > dl + 1e-6:
                    # 截止前放不下：留作 fallback（最早可用），最后标记迟到。
                    if fallback is None:
                        fallback = (s0, s0 + dur_sec)
                    continue
                leftover = (s1 - s0) - dur_sec
                # 优先「最早的一天」、同一天里再挑剩余最小的段：优先级高 / 截止近的任务
                # 不会被一个很晚但刚好合身的空档拖到夜里。
                cand = (day, leftover, s0, s0 + dur_sec)
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
            "duration_min": row["duration"], "priority": row["priority"],
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
            "shrunk_duration_min": min((x["duration"] for x in enriched), default=0),
            "shrunk_duration_max": max((x["duration"] for x in enriched), default=0),
        },
    }
