"""个人日程的行为学习：任务分型、EWMA 时长、变更点检测、轻量偏好记忆与复盘。

设计边界（对应用户的技术方案「轻量 MVP + 部分进阶」）：
- **估什么**：每类任务的真实耗时（``dur_mean`` / ``dur_var``）与完成 / 推迟次数；
- **怎么估**：EWMA（默认 α=0.35）让收缩目标随近期数据更新；样本少时按类别先验；
  连续偏差超过 2.5σ 且累计 2 次 ⇒ 变更点，重置该类别的均值（换路线 / 换工作等结构性变化）；
- **偏好**：用完成率 / 推迟次数做一个**很小**的偏好分，只当排序的平局加分，
  绝不覆盖用户明确给的高优先级（否则「拖延的任务永远排不上」）；
- **复盘**：按类别给出计划均值 / 实际均值 / 偏差百分比，供每周调整先验与缓冲。

纯函数，不读写数据库；所有时间可注入，便于门卫测试。
"""

import math
import re
import time

# 类别先验：新库 / 样本少时的起点（分钟）。值是「常见一次投入」的经验值，
# 不是硬编码规则——收到真实完成记录后会被 EWMA 覆盖。
CATEGORY_PRIOR = {
    "study": 60.0,
    "work": 90.0,
    "meeting": 45.0,
    "exercise": 45.0,
    "chore": 30.0,
    "social": 60.0,
    "course": 90.0,
    "general": 60.0,
}
CATEGORY_LABEL = {
    "study": "学习", "work": "工作", "meeting": "会议", "exercise": "运动",
    "chore": "杂务", "social": "社交", "course": "课程", "general": "通用",
}
CATEGORY_VAR = 30.0 ** 2          # 默认噪声方差（分钟²）
EWMA_ALPHA = 0.35                 # 时长均值 / 方差的更新速度
CHANGE_POINT_SIGMA = 2.5          # 偏差超过 2.5σ 记一次
CHANGE_POINT_STREAK = 2           # 连续两次 ⇒ 结构性变化，重置先验
MIN_SAMPLES_FOR_CHANGE = 3        # 样本太少不判变更点（避免新任务被当异常）
DEFAULT_PRIOR_WEIGHT = 2.0        # 首次完成时先验占的等效样本数

_KEYWORDS = (
    ("meeting", ("会议", "开会", "组会", "讨论", "面谈", "汇报", "答辩", "面试", "例会")),
    ("exercise", ("跑步", "健身", "锻炼", "游泳", "骑车", "瑜伽", "球")),
    ("chore", ("买", "采购", "取件", "寄", "打扫", "洗", "整理", "做饭", "缴费", "报销")),
    ("social", ("聚会", "聚餐", "生日", "拜访", "约饭", "电话", "聊天")),
    ("study", ("作业", "实验", "报告", "论文", "复习", "预习", "阅读", "学习", "做题",
               "考试", "课程", "背单词", "刷题")),
    ("work", ("写", "编码", "编程", "开发", "项目", "设计", "文档", "提交", "邮件", "画图")),
)


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


def infer_task_type(title="", kind="task", notes=""):
    """标题 / 备注 / 类型 → 单一类别；课程永远归 course，其余按关键词。"""
    if str(kind or "") == "course":
        return "course"
    text = "%s %s" % (str(title or ""), str(notes or ""))
    low = text.lower()
    for cat, words in _KEYWORDS:
        if any(w in low for w in words):
            return cat
    return "general"


def category_prior(task_type):
    return float(CATEGORY_PRIOR.get(str(task_type or "general"), CATEGORY_PRIOR["general"]))


def default_entry(task_type, now=None):
    """某类别尚无完成记录时的起点：均值 = 类别先验，样本 0。"""
    return {
        "task_type": str(task_type or "general"),
        "dur_mean": category_prior(task_type),
        "dur_var": CATEGORY_VAR,
        "samples": 0,
        "dev_streak": 0,
        "done_count": 0,
        "defer_count": 0,
        "change_point_ts": 0.0,
        "updated_ts": _num(now, time.time()),
    }


def normalize_entry(raw, task_type="general"):
    """数据库行 / 字典 → 完整 stats 条目（脏值兜底）。"""
    if not isinstance(raw, dict):
        return default_entry(task_type)
    cat = str(raw.get("task_type") or task_type or "general")
    out = default_entry(cat)
    out["dur_mean"] = max(5.0, _num(raw.get("dur_mean"), out["dur_mean"]))
    out["dur_var"] = max(1.0, _num(raw.get("dur_var"), out["dur_var"]))
    for key in ("samples", "dev_streak", "done_count", "defer_count"):
        out[key] = max(0, _int(raw.get(key), 0))
    out["change_point_ts"] = max(0.0, _num(raw.get("change_point_ts"), 0.0))
    out["updated_ts"] = max(0.0, _num(raw.get("updated_ts"), 0.0))
    return out


def update_stats(stats, task_type, actual_min, planned_min=0.0, outcome="done",
                 now=None, alpha=EWMA_ALPHA):
    """把一次执行结果并进类别统计；返回**新的** stats 字典（不改入参）。

    规则：
    - ``done`` 且有正的实际耗时：EWMA 更新均值 / 方差；样本首次完成时先验占
      2 个等效样本（避免一条极端值把整类拉走）；
    - 连续两次偏差 > 2.5σ（样本 ≥ 3）⇒ 变更点：均值直接重置到本次实际值；
    - ``deferred``：只累加 defer_count（不更新时长，因为没真正执行）。
    - ``skipped``：只记录，不学习时长。
    """
    cat = str(task_type or "general")
    out = {k: normalize_entry(v, k) for k, v in (stats or {}).items()}
    entry = out.get(cat) or default_entry(cat, now)
    actual = _num(actual_min, 0.0)
    planned = _num(planned_min, 0.0)
    outcome = str(outcome or "done").lower()
    ref = _num(now, time.time())
    try:
        a = _clamp(float(alpha), 0.05, 0.95)
    except (TypeError, ValueError):
        a = EWMA_ALPHA

    if outcome == "deferred":
        entry["defer_count"] += 1
        entry["updated_ts"] = ref
        out[cat] = entry
        return out
    if outcome != "done" or actual <= 0.0:
        entry["updated_ts"] = ref
        out[cat] = entry
        return out

    entry["done_count"] += 1
    n = entry["samples"]
    prev_mean = entry["dur_mean"]
    prev_var = entry["dur_var"]
    if n <= 0:
        # 第一次完成：先验（等效 2 个样本）+ 本次实际值。
        base = planned if planned > 0.0 else category_prior(cat)
        w = DEFAULT_PRIOR_WEIGHT
        entry["dur_mean"] = (base * w + actual) / (w + 1.0)
        entry["dur_var"] = max(CATEGORY_VAR * 0.5,
                               (actual - entry["dur_mean"]) ** 2 + CATEGORY_VAR * 0.5)
    else:
        delta = actual - prev_mean
        entry["dur_mean"] = prev_mean + a * delta
        entry["dur_var"] = max(1.0, (1.0 - a) * prev_var + a * delta * delta)
    sigma = max(1.0, math.sqrt(max(1.0, prev_var)))
    if n >= MIN_SAMPLES_FOR_CHANGE and abs(actual - prev_mean) > CHANGE_POINT_SIGMA * sigma:
        entry["dev_streak"] = entry["dev_streak"] + 1
        if entry["dev_streak"] >= CHANGE_POINT_STREAK:
            # 结构性变化：均值重置到本次，方差回到先验，连续计数清零。
            entry["dur_mean"] = actual
            entry["dur_var"] = CATEGORY_VAR
            entry["dev_streak"] = 0
            entry["change_point_ts"] = ref
    else:
        entry["dev_streak"] = 0
    entry["samples"] = n + 1
    entry["updated_ts"] = ref
    out[cat] = entry
    return out


def preference_bonus(entry):
    """类别偏好分（-5..+5）：完成率高的类别在**同优先级**时略靠前。

    只做平局加分，不压过用户给的优先级；推迟次数不降权（拖延任务更需要被排进去）。
    """
    if not isinstance(entry, dict):
        return 0
    done = max(0, _int(entry.get("done_count"), 0))
    defer = max(0, _int(entry.get("defer_count"), 0))
    total = done + defer
    if total < 2:
        return 0
    return int(round(_clamp((done / float(total) - 0.5) * 10.0, -5.0, 5.0)))


def plan_targets(stats):
    """给自动排班用的类别目标：均值 / 方差 / 样本数 / 偏好分。"""
    out = {}
    for cat, raw in (stats or {}).items():
        e = normalize_entry(raw, cat)
        out[str(cat)] = {
            "mean": e["dur_mean"], "var": e["dur_var"], "samples": e["samples"],
            "bonus": preference_bonus(e),
        }
    return out


def review(history, stats, now=None):
    """每周复盘：按类别的计划 / 实际偏差 + 完成 / 推迟计数。"""
    ref = _num(now, time.time())
    buckets = {}
    for it in history or []:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("task_type") or "general")
        b = buckets.setdefault(cat, {"planned": [], "actual": [], "done": 0,
                                     "deferred": 0, "skipped": 0})
        outcome = str(it.get("outcome") or "")
        if outcome == "deferred" or _int(it.get("defer_count"), 0) > 0:
            b["deferred"] += max(1, _int(it.get("defer_count"), 0))
        elif str(it.get("status", "")) == "done":
            b["done"] += 1
            actual = _num(it.get("actual_min"), 0.0)
            planned = _num(it.get("duration_min"), 0.0)
            if actual > 0:
                b["actual"].append(actual)
            if planned > 0:
                b["planned"].append(planned)
        elif str(it.get("status", "")) == "cancelled":
            b["skipped"] += 1
    rows = []
    for cat, b in sorted(buckets.items()):
        if not (b["planned"] or b["actual"] or b["done"] or b["deferred"] or b["skipped"]):
            continue
        planned_mean = sum(b["planned"]) / len(b["planned"]) if b["planned"] else 0.0
        actual_mean = sum(b["actual"]) / len(b["actual"]) if b["actual"] else 0.0
        bias = 0.0
        if planned_mean > 0 and actual_mean > 0:
            bias = (actual_mean - planned_mean) / planned_mean
        e = normalize_entry((stats or {}).get(cat), cat)
        rows.append({
            "task_type": cat, "label": CATEGORY_LABEL.get(cat, cat),
            "planned_mean": round(planned_mean, 1), "actual_mean": round(actual_mean, 1),
            "bias_pct": round(bias * 100.0, 1),
            "done": b["done"], "deferred": b["deferred"], "skipped": b["skipped"],
            "ewma_mean": round(e["dur_mean"], 1), "samples": e["samples"],
            "change_point_ts": e["change_point_ts"],
        })
    total_done = sum(r["done"] for r in rows)
    total_defer = sum(r["deferred"] for r in rows)
    return {
        "ok": True, "rows": rows, "done": total_done, "deferred": total_defer,
        "summary": _review_summary(rows, total_done, total_defer),
        "now": ref,
    }


def _review_summary(rows, done, deferred):
    if not rows:
        return "还没有可复盘的完成记录。"
    worst = max(rows, key=lambda r: abs(float(r.get("bias_pct", 0.0))))
    if abs(float(worst.get("bias_pct", 0.0))) >= 10.0:
        direction = "偏慢" if float(worst["bias_pct"]) > 0 else "偏快"
        return "%s类任务实际比计划%s %.0f%%；完成 %d 条，推迟 %d 条。" % (
            worst.get("label", "任务"), direction, abs(float(worst["bias_pct"])), done, deferred)
    return "完成 %d 条，推迟 %d 条；各类时长偏差都在 10%% 以内。" % (done, deferred)
