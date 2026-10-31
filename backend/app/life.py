"""生命时间线（拟人化 P0）：跨重启的时间事实，而不是“提示词里塞时间戳”。

设计目标（对应 DeepSeek 方案的 §2.1 / §2.2）：
- **只追加**：互动、主动搭话、聊天、对局开始/结束、落子都写成带墙钟的事件；
  修正由新事件表达，旧事件保留（本版先不实现 supersede，只把列留在表里）。
- **持久化**：状态与事件落 SQLite（默认 ``<backend>/data/life.db``），
  kill -9 / 重启后仍能回答“上一回它是什么时候见到玩家的”。
- **纯规则**：相对时间、昼夜相位由这里算，**不调用 LLM / embedding / 引擎**；
  提示词只拿现成事实，避免让模型从原始时间戳里自行猜。
- **故障安全**：库打不开 / 写失败只打日志，内存状态继续工作，绝不影响出话。

本模块不负责情绪数值的演化（那仍在 ``EmotionModel`` / 后续 PAD 层）；
它只负责**时间**：事件序、最近互动、最近主动、最近聊天、最近对局。
"""

import json
import math
import os
import sqlite3
import threading
import time

from .emotion import EmotionModel

# 默认库路径：<backend>/data/life.db（backend/data/ 已 gitignore，属运行产物）
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "life.db")

MAX_EVENTS = 4000                 # 时间线只保留最近这么多条（超出按 id 淘汰最旧）
MAX_SUMMARY_LEN = 120             # 单条事件摘要上限
MAX_MOOD_RESTORE_HALF_LIFE_H = 0.5   # 跨重启情绪恢复的半衰期：30 分钟（与慢变量时间常数一致）

# —— 心理慢变量（P0/P1 的生命感来源，不是可直接朗读的数值）——
LIFE_ENERGY_BASE = 0.72           # 精力基线：休息够了但不亢奋
LIFE_BOREDOM_BASE = 0.16          # 无聊基线：刚互动过时很低
LIFE_AFFINITY_BASE = 0.35         # 亲密度基线：熟悉但仍保持分寸
LIFE_TRUST_BASE = 0.50            # 信任基线：中性偏谨慎
LIFE_DOMINANCE_BASE = 0.50        # PAD 的 D：掌控感 / 底气
ENERGY_FLOOR = 0.12               # 长时间不互动后的精力下限
ENERGY_TAU_H = 8.0                # 精力向“疲惫”回落的时间常数（小时）
BOREDOM_TAU_H = 3.0               # 无聊累积的时间常数（小时）
AFFINITY_TAU_H = 72.0             # 亲密度缓慢回落（天级）
TRUST_TAU_H = 120.0               # 信任更慢回落
DOMINANCE_TAU_H = 48.0            # 掌控感回落（两天级）

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS life_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        updated_ts REAL NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS life_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}',
        superseded_by INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS schedule_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL NOT NULL DEFAULT 0,
        when_text TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'planned',
        created_ts REAL NOT NULL DEFAULT 0,
        updated_ts REAL NOT NULL DEFAULT 0,
        date_text TEXT NOT NULL DEFAULT '',
        weekday INTEGER NOT NULL DEFAULT -1,
        period TEXT NOT NULL DEFAULT '',
        period_start INTEGER NOT NULL DEFAULT 0,
        period_end INTEGER NOT NULL DEFAULT 0,
        start_time TEXT NOT NULL DEFAULT '',
        end_time TEXT NOT NULL DEFAULT '',
        teacher TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'task',
        priority INTEGER NOT NULL DEFAULT 50,
        duration_min INTEGER NOT NULL DEFAULT 60,
        deadline_ts REAL NOT NULL DEFAULT 0,
        auto_planned INTEGER NOT NULL DEFAULT 0,
        reminded_ts REAL NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        archived_ts REAL NOT NULL DEFAULT 0,
        task_type TEXT NOT NULL DEFAULT 'general',
        actual_min REAL NOT NULL DEFAULT 0,
        defer_count INTEGER NOT NULL DEFAULT 0,
        completed_ts REAL NOT NULL DEFAULT 0,
        outcome TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_life_events_ts ON life_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_start ON schedule_items(start_ts)",
    # 类别统计（v4.1.1）：EWMA 真实耗时 / 变更点 / 完成与推迟计数。与 schedule_items 解耦，
    # 清理旧日程不会丢掉「学习到的个人节奏」。
    """CREATE TABLE IF NOT EXISTS schedule_stats (
        task_type TEXT PRIMARY KEY,
        dur_mean REAL NOT NULL DEFAULT 60,
        dur_var REAL NOT NULL DEFAULT 900,
        samples INTEGER NOT NULL DEFAULT 0,
        dev_streak INTEGER NOT NULL DEFAULT 0,
        done_count INTEGER NOT NULL DEFAULT 0,
        defer_count INTEGER NOT NULL DEFAULT 0,
        change_point_ts REAL NOT NULL DEFAULT 0,
        updated_ts REAL NOT NULL DEFAULT 0
    )""",
)

# 日程表的**完整列**（顺序固定：`_schedule_row` 按这个下标读）。新增字段用于周课表 UI：
# date_text / weekday / period / start_time / end_time / teacher / location。
SCHEDULE_COLS = ("id", "start_ts", "when_text", "title", "notes", "source", "status",
                 "date_text", "weekday", "period", "period_start", "period_end",
                 "start_time", "end_time", "teacher", "location",
                 "kind", "priority", "duration_min", "deadline_ts",
                 "auto_planned", "reminded_ts", "archived", "archived_ts",
                 "task_type", "actual_min", "defer_count", "completed_ts", "outcome",
                 "updated_ts")
_SCHEDULE_SELECT = ", ".join(SCHEDULE_COLS)
# 旧库升级：v4.0.0 之前的 schedule_items 没有这些列，启动时按需 ALTER TABLE 补上。
_SCHEDULE_EXTRA_COLS = {
    "date_text": "TEXT NOT NULL DEFAULT ''",
    "weekday": "INTEGER NOT NULL DEFAULT -1",
    "period": "TEXT NOT NULL DEFAULT ''",
    "period_start": "INTEGER NOT NULL DEFAULT 0",
    "period_end": "INTEGER NOT NULL DEFAULT 0",
    "start_time": "TEXT NOT NULL DEFAULT ''",
    "end_time": "TEXT NOT NULL DEFAULT ''",
    "teacher": "TEXT NOT NULL DEFAULT ''",
    "location": "TEXT NOT NULL DEFAULT ''",
    "kind": "TEXT NOT NULL DEFAULT 'task'",
    "priority": "INTEGER NOT NULL DEFAULT 50",
    "duration_min": "INTEGER NOT NULL DEFAULT 60",
    "deadline_ts": "REAL NOT NULL DEFAULT 0",
    "auto_planned": "INTEGER NOT NULL DEFAULT 0",
    "reminded_ts": "REAL NOT NULL DEFAULT 0",
    "archived": "INTEGER NOT NULL DEFAULT 0",
    "archived_ts": "REAL NOT NULL DEFAULT 0",
    "task_type": "TEXT NOT NULL DEFAULT 'general'",
    "actual_min": "REAL NOT NULL DEFAULT 0",
    "defer_count": "INTEGER NOT NULL DEFAULT 0",
    "completed_ts": "REAL NOT NULL DEFAULT 0",
    "outcome": "TEXT NOT NULL DEFAULT ''",
}

# 事件类型 → 需要更新的时间戳字段。所有事件都能写进时间线；
# 只有互动类事件会推进 last_interaction_ts。
_TOUCH_FIELDS = {
    "chat": ("last_interaction_ts", "last_chat_ts"),
    "interaction": ("last_interaction_ts",),
    "game_start": ("last_interaction_ts", "last_game_ts"),
    "game_end": ("last_interaction_ts", "last_game_ts"),
    "game_move": ("last_interaction_ts", "last_game_ts"),
    "idle": ("last_proactive_ts",),
    "peer": ("last_proactive_ts",),
    "greet": ("last_proactive_ts",),
    "startup": (),
}

# 事件 → 心理慢变量增量（energy, boredom, affinity, trust, dominance）。
# 数值刻意偏小：单次互动不该把“疲惫/无聊”一次抹平，否则又回到函数式桌宠。
_INTERACTION_EFFECTS = {
    "chat": (0.080, -0.180, 0.012, 0.004, 0.010),
    "interaction": (0.050, -0.100, 0.020, 0.010, 0.008),
    "game_start": (-0.010, -0.050, 0.002, 0.000, 0.002),
    "game_end": (-0.020, -0.030, 0.004, 0.002, 0.003),
    "game_move": (-0.002, -0.010, 0.0005, 0.000, 0.0005),
    "idle": (-0.002, -0.040, 0.001, 0.000, 0.000),
    "peer": (-0.002, -0.035, 0.001, 0.000, 0.000),
    "greet": (-0.001, -0.020, 0.002, 0.000, 0.000),
}


def _as_ts(v):
    """正的、有限的、可表示的墙钟秒；``bool`` / 脏值 / 非正 / 未来不做判断（调用方再比）。"""
    if isinstance(v, bool):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(t) or t <= 0.0 or t > 253402300799.0:
        return None
    return t


def _clip(text, limit=MAX_SUMMARY_LEN):
    """事件摘要截断：真的截了才加省略号。"""
    s = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit - 1] + "…"


def _merge_sources(old, new, limit=200):
    """合并扫描来源：按逗号拆分去重，保留先后顺序（不丢之前扫到的文件名）。"""
    out = []
    for text in (old, new):
        for part in str(text or "").split(","):
            name = part.strip()
            if name and name not in out:
                out.append(name)
    return ",".join(out)[:limit]


def _safe_int(v, default=0):
    """脏值 → 默认整数（bool 不算数字，避免 True 变成 1 混进星期 / 节次）。"""
    if isinstance(v, bool):
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _json(v):
    """字典 → JSON 文本；不可序列化时退回 ``{}``（脏数据不透传）。"""
    try:
        return json.dumps(v if isinstance(v, dict) else {}, ensure_ascii=False)
    except Exception:                   # noqa: BLE001
        return "{}"


def _loads(text):
    """JSON 文本 → dict；坏行退回 ``{}``。"""
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clamp01(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not math.isfinite(f):
        return float(default)
    return max(0.0, min(1.0, f))


def _sanitize_mood(mood):
    """只收 v/c/a 三键；缺任何一维 / 脏值 ⇒ ``{}``（不存半份情绪）。"""
    if not isinstance(mood, dict):
        return {}
    try:
        v, c, a = float(mood.get("v")), float(mood.get("c")), float(mood.get("a"))
    except (TypeError, ValueError, OverflowError):
        return {}
    if not all(math.isfinite(x) for x in (v, c, a)):
        return {}
    return {"v": _clamp01(v), "c": _clamp01(c), "a": _clamp01(a)}


def _default_state(now):
    now = _as_ts(now) or time.time()
    return {
        "created_ts": now,
        "updated_ts": now,
        "last_evolve_ts": now,
        "last_tick_ts": 0.0,
        "last_interaction_ts": 0.0,
        "last_proactive_ts": 0.0,
        "last_chat_ts": 0.0,
        "last_game_ts": 0.0,
        "mood": {},
        "mood_ts": 0.0,
        "energy": LIFE_ENERGY_BASE,
        "boredom": LIFE_BOREDOM_BASE,
        "affinity": LIFE_AFFINITY_BASE,
        "trust": LIFE_TRUST_BASE,
        "dominance": LIFE_DOMINANCE_BASE,
        "meta": {},
    }


def _sanitize_state(raw, now):
    """旧库 / 脏 JSON → 完整的默认状态（只保留合法时间戳与合法情绪）。"""
    base = _default_state(now)
    if not isinstance(raw, dict):
        return base
    for key in ("created_ts", "updated_ts", "last_evolve_ts", "last_tick_ts",
                "last_interaction_ts", "last_proactive_ts", "last_chat_ts",
                "last_game_ts", "mood_ts"):
        t = _as_ts(raw.get(key))
        if t is not None:
            base[key] = t
    for key in ("energy", "boredom", "affinity", "trust", "dominance"):
        if key in raw:
            base[key] = _clamp01(raw.get(key), base[key])
    mood = _sanitize_mood(raw.get("mood"))
    if mood:
        base["mood"] = mood
    meta = raw.get("meta")
    base["meta"] = meta if isinstance(meta, dict) else {}
    return base


def _day_phase(hour):
    """本地小时 → 昼夜相位（人话，供时间块自然使用）。"""
    try:
        h = int(hour)
    except (TypeError, ValueError, OverflowError):
        return ""
    if 0 <= h < 5:
        return "深夜"
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 12:
        return "上午"
    if 12 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 22:
        return "傍晚"
    return "夜里"


def mood_after_break(mood, last_ts, now=None, half_life_h=MAX_MOOD_RESTORE_HALF_LIFE_H):
    """上一次落盘的情绪 → 现在还剩多少（跨重启恢复用）。

    与 ``persona.mood_after_break`` 同一数学形状，但这里用**更短的半衰期**：
    P0 还没有双速情绪，先把「重启后仍带着一点刚才的余温」落地；
    后续 PAD 双速层会接管这条口径，调用方只需替换本函数。
    """
    src = _sanitize_mood(mood)
    if not src:
        return {}
    ts = _as_ts(last_ts)
    if ts is None:
        return {}
    ref = _as_ts(now) or time.time()
    if ts > ref:                            # 未来时间戳是脏值：不猜、不恢复
        return {}
    hours = max(0.0, (ref - ts) / 3600.0)
    keep = 0.5 ** (hours / max(0.05, float(half_life_h)))
    base = EmotionModel.BASELINE
    return {
        "v": _clamp01(base[0] + (src["v"] - base[0]) * keep),
        "c": _clamp01(base[1] + (src["c"] - base[1]) * keep),
        "a": _clamp01(base[2] + (src["a"] - base[2]) * keep),
    }


class LifeStore:
    """生命时间线：状态 + 只追加事件。

    构造即尝试打开库并载入状态；打不开也继续用内存状态（``available == False``）。
    所有公开方法都不上抛异常。
    """

    def __init__(self, db_path="", now=None):
        self._lock = threading.Lock()
        self._db = None
        self._db_path = str(db_path or "").strip() or DEFAULT_DB_PATH
        self._state = _default_state(now)
        self._open()
        self._load(now)

    # ================= 查询 =================

    @property
    def available(self):
        return self._db is not None

    @property
    def state(self):
        """状态副本（调用方只读；嵌套 dict 也复制一层，避免误改）。"""
        with self._lock:
            return self._copy_state_locked()

    def _copy_state_locked(self):
        s = dict(self._state)
        s["mood"] = dict(s.get("mood") or {})
        s["meta"] = dict(s.get("meta") or {})
        return s

    def mood_after_break(self, now=None, half_life_h=MAX_MOOD_RESTORE_HALF_LIFE_H):
        """跨重启情绪恢复；没有落盘情绪 / 脏值 ⇒ ``{}``。"""
        with self._lock:
            mood = dict(self._state.get("mood") or {})
            ts = self._state.get("mood_ts") or 0.0
        return mood_after_break(mood, ts, now=now, half_life_h=half_life_h)

    def temporal_facts(self, now=None):
        """四尺度时间上下文的一部分（规则计算，零 IO、零 LLM）。

        返回键（缺信号就不产出）：
        ``since_interaction_sec`` / ``since_proactive_sec`` / ``since_chat_sec`` /
        ``since_game_sec`` / ``day_phase`` / ``is_weekend`` / ``hour``。

        “since” 只从**有效且不晚于 now**的时间戳算；没有发生过就**不产出**，
        绝不补一个 0 误导模型。
        """
        ref = _as_ts(now) or time.time()
        with self._lock:
            snap = self._copy_state_locked()
        out = {}
        for key, field in (
            ("since_interaction_sec", "last_interaction_ts"),
            ("since_proactive_sec", "last_proactive_ts"),
            ("since_chat_sec", "last_chat_ts"),
            ("since_game_sec", "last_game_ts"),
        ):
            ts = _as_ts(snap.get(field))
            if ts is not None and ts <= ref:
                out[key] = max(0.0, ref - ts)
        try:
            lt = time.localtime(ref)
            out["day_phase"] = _day_phase(lt.tm_hour)
            out["hour"] = int(lt.tm_hour)
            out["is_weekend"] = bool(lt.tm_wday >= 5)
        except (OverflowError, OSError, ValueError):
            pass
        nxt = self.next_schedule(ref)
        if nxt:
            out["next_schedule_title"] = str(nxt.get("title", ""))
            out["next_schedule_at"] = str(nxt.get("when_text", ""))
            try:
                out["next_schedule_in_sec"] = max(0.0, float(nxt.get("start_ts", 0)) - ref)
            except (TypeError, ValueError, OverflowError):
                pass
        return out

    def reset_chat_state(self, now=None) -> dict:
        """测试数据清理：删掉对话类事件，并把慢变量 / 情绪回到默认基线。

        只动 ``life_events`` 里的 chat / idle / peer / greet 与 ``life_state``；
        **不删除日程、课程、棋谱或画像**。返回删掉的事件条数与新状态副本。
        """
        now = _as_ts(now) or time.time()
        removed = 0
        with self._lock:
            if self._db is not None:
                try:
                    cur = self._db.execute(
                        "DELETE FROM life_events WHERE kind IN ('chat', 'idle', 'peer', 'greet')")
                    self._db.commit()
                    removed = int(cur.rowcount or 0)
                except Exception as e:          # noqa: BLE001
                    print("[Life] 清理对话事件失败（已忽略）：%s" % e)
            self._state = _default_state(now)
            self._persist_state_locked()
            return {"events": removed, "state": self._copy_state_locked()}

    def recent_events(self, limit=20):
        """最近事件（新 → 旧）；库不可用返回 ``[]``。"""
        try:
            n = max(0, min(int(limit or 0), MAX_EVENTS))
        except (TypeError, ValueError):
            n = 0
        if n == 0 or self._db is None:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, ts, kind, source, summary, meta, superseded_by "
                    "FROM life_events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            out = []
            for r in rows:
                out.append({
                    "id": int(r[0]), "ts": float(r[1]), "kind": str(r[2]),
                    "source": str(r[3]), "summary": str(r[4]),
                    "meta": _loads(r[5]), "superseded_by": int(r[6]),
                })
            return out
        except Exception as e:              # noqa: BLE001
            print("[Life] 时间线读取失败（已忽略）：%s" % e)
            return []

    def snapshot(self, now=None, events_limit=20):
        """API / 调试用快照：状态 + 时间上下文 + 最近事件 + 日程。"""
        ref = _as_ts(now) or time.time()
        return {
            "state": self.state,
            "temporal": self.temporal_facts(ref),
            "events": self.recent_events(events_limit),
            "schedule": self.list_schedule(20),
            "available": self.available,
        }

    def stats(self):
        """库体量（健康面用；失败返回 0）。"""
        if self._db is None:
            return {"events": 0, "schedule": 0, "db": ""}
        try:
            with self._lock:
                n = int(self._db.execute("SELECT COUNT(*) FROM life_events").fetchone()[0])
                s = int(self._db.execute("SELECT COUNT(*) FROM schedule_items").fetchone()[0])
            return {"events": n, "schedule": s, "db": self._db_path}
        except Exception as e:              # noqa: BLE001
            print("[Life] 体量读取失败（已忽略）：%s" % e)
            return {"events": 0, "schedule": 0, "db": self._db_path}

    # ================= 日程 =================

    @staticmethod
    def _schedule_row(r):
        """一行日程 → dict（列顺序与 ``SCHEDULE_COLS`` 一致；旧行缺列时用默认值兜底）。"""
        def _num(v, d=0):
            try:
                return int(v)
            except (TypeError, ValueError, OverflowError):
                return d

        def _text(i):
            return str(r[i] or "") if len(r) > i else ""

        return {
            "id": _num(r[0]), "start_ts": float(r[1] or 0.0), "when_text": _text(2),
            "title": _text(3), "notes": _text(4), "source": _text(5),
            "status": _text(6) or "planned",
            "date": _text(7),
            "weekday": _num(r[8], -1) if len(r) > 8 else -1,
            "period": _text(9),
            "period_start": _num(r[10], 0) if len(r) > 10 else 0,
            "period_end": _num(r[11], 0) if len(r) > 11 else 0,
            "start_time": _text(12),
            "end_time": _text(13),
            "teacher": _text(14),
            "location": _text(15),
            "kind": _text(16) or "task",
            "priority": _num(r[17], 50) if len(r) > 17 else 50,
            "duration_min": _num(r[18], 60) if len(r) > 18 else 60,
            "deadline_ts": float(r[19] or 0.0) if len(r) > 19 else 0.0,
            "auto_planned": bool(_num(r[20], 0)) if len(r) > 20 else False,
            "reminded_ts": float(r[21] or 0.0) if len(r) > 21 else 0.0,
            "archived": bool(_num(r[22], 0)) if len(r) > 22 else False,
            "archived_ts": float(r[23] or 0.0) if len(r) > 23 else 0.0,
            "task_type": _text(24) or "general",
            "actual_min": float(r[25] or 0.0) if len(r) > 25 else 0.0,
            "defer_count": _num(r[26], 0) if len(r) > 26 else 0,
            "completed_ts": float(r[27] or 0.0) if len(r) > 27 else 0.0,
            "outcome": _text(28),
            "updated_ts": float(r[29] or 0.0) if len(r) > 29 else 0.0,
        }

    def upsert_schedule(self, items, source="", now=None):
        """写入 / 更新日程条目；返回保存后的列表。

        ``items`` 由 ``schedule.normalize_items`` 产出：``start_ts``（0=待定）、
        ``when_text``、``title``、``notes``，课程条目还带 ``date / weekday / period /
        start_time / end_time / teacher / location``。去重键：
        - 课程条目：``source + date + period + title``（同一份课表重复扫描不重复）；
        - 普通条目：``source + start_ts + title``。
        失败返回 ``[]``。
        """
        if not isinstance(items, list) or self._db is None:
            return []
        t = _as_ts(now) or time.time()
        out = []
        with self._lock:
            try:
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    title = _clip(it.get("title", ""), 100)
                    if title == "":
                        continue
                    try:
                        start = float(it.get("start_ts", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        start = 0.0
                    if not math.isfinite(start) or start < 0.0:
                        start = 0.0
                    when_text = _clip(it.get("when_text", ""), 64)
                    notes = _clip(it.get("notes", ""), 300)
                    src = _clip(it.get("source", source), 200)
                    date_text = _clip(it.get("date", ""), 16)
                    weekday = _safe_int(it.get("weekday"), -1)
                    period = _clip(it.get("period", ""), 16)
                    p_start = _safe_int(it.get("period_start"), 0)
                    p_end = _safe_int(it.get("period_end"), 0)
                    start_time = _clip(it.get("start_time", ""), 8)
                    end_time = _clip(it.get("end_time", ""), 8)
                    teacher = _clip(it.get("teacher", ""), 64)
                    location = _clip(it.get("location", ""), 100)
                    kind = _clip(it.get("kind", ""), 16) or ("course" if period != "" else "task")
                    priority = max(0, min(100, _safe_int(it.get("priority"), 50)))
                    duration = max(5, min(24 * 60, _safe_int(it.get("duration_min"), 60)))
                    try:
                        deadline = float(it.get("deadline_ts", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        deadline = 0.0
                    if not math.isfinite(deadline) or deadline < 0.0:
                        deadline = 0.0
                    auto_planned = 1 if bool(it.get("auto_planned", False)) else 0
                    task_type = _clip(it.get("task_type", ""), 24) or (
                        "course" if kind == "course" else "general")
                    # 去重键**不再包含 source**：同一课表换一张图 / 重扫一次时 source 会变，
                    # 旧逻辑会把整张表再插一遍。课程按「日期（或星期）+ 节次 + 标题」，
                    # 普通条目按「开始时间 + 标题」；命中后合并 source 与缺失字段。
                    row = None
                    if kind == "course" and (date_text != "" or weekday >= 0):
                        if date_text != "":
                            row = self._db.execute(
                                "SELECT id, source, notes, teacher, location FROM schedule_items "
                                "WHERE archived = 0 AND date_text = ? AND period = ? "
                                "AND title = ? ORDER BY id ASC LIMIT 1",
                                (date_text, period, title)).fetchone()
                        else:
                            row = self._db.execute(
                                "SELECT id, source, notes, teacher, location FROM schedule_items "
                                "WHERE archived = 0 AND date_text = '' AND weekday = ? "
                                "AND period = ? AND title = ? ORDER BY id ASC LIMIT 1",
                                (weekday, period, title)).fetchone()
                    if row is None:
                        row = self._db.execute(
                            "SELECT id, source, notes, teacher, location FROM schedule_items "
                            "WHERE archived = 0 AND start_ts = ? AND title = ? "
                            "ORDER BY id ASC LIMIT 1",
                            (start, title)).fetchone()
                    if row is None:
                        cur = self._db.execute(
                            "INSERT INTO schedule_items(start_ts, when_text, title, notes, source, "
                            "status, created_ts, updated_ts, date_text, weekday, period, period_start, "
                            "period_end, start_time, end_time, teacher, location, kind, priority, "
                            "duration_min, deadline_ts, auto_planned, task_type) "
                            "VALUES(?,?,?,?,?,'planned',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (start, when_text, title, notes, src, t, t, date_text, weekday, period,
                             p_start, p_end, start_time, end_time, teacher, location, kind, priority,
                             duration, deadline, auto_planned, task_type))
                        sid = int(cur.lastrowid)
                    else:
                        sid = int(row[0])
                        src = _merge_sources(row[1], src)
                        # 新扫描缺字段时保留旧值，避免「换一张图后教师 / 地点被清空」。
                        notes = notes or str(row[2] or "")
                        teacher = teacher or str(row[3] or "")
                        location = location or str(row[4] or "")
                        self._db.execute(
                            "UPDATE schedule_items SET start_ts = ?, when_text = ?, notes = ?, "
                            "source = ?, date_text = ?, weekday = ?, period = ?, period_start = ?, "
                            "period_end = ?, start_time = ?, end_time = ?, teacher = ?, location = ?, "
                            "kind = ?, priority = ?, duration_min = ?, deadline_ts = ?, "
                            "auto_planned = ?, task_type = ?, updated_ts = ? WHERE id = ?",
                            (start, when_text, notes, src, date_text, weekday, period, p_start, p_end,
                             start_time, end_time, teacher, location, kind, priority, duration,
                             deadline, auto_planned, task_type, t, sid))
                    got = self._db.execute(
                        "SELECT %s FROM schedule_items WHERE id = ?" % _SCHEDULE_SELECT,
                        (sid,)).fetchone()
                    if got is not None:
                        out.append(self._schedule_row(got))
                self._db.commit()
            except Exception as e:          # noqa: BLE001
                print("[Life] 日程写入失败（已忽略）：%s" % e)
                try:
                    self._db.rollback()
                except Exception:           # noqa: BLE001
                    pass
                return []
        return out

    def delete_schedule_source(self, sources):
        """删掉某次扫描来源下的旧日程（重扫同一批资料时用新结构替换旧文本行）。

        只按 ``source`` 精确匹配；``sources`` 可以是单个字符串或字符串列表。
        返回删除行数；库不可用 / 空列表返回 0。
        """
        if isinstance(sources, str):
            sources = [sources]
        names = [str(s) for s in (sources or []) if str(s).strip()]
        if not names or self._db is None:
            return 0
        try:
            with self._lock:
                q = ",".join("?" * len(names))
                cur = self._db.execute(
                    "DELETE FROM schedule_items WHERE source IN (%s)" % q, names)
                self._db.commit()
                return int(cur.rowcount or 0)
        except Exception as e:              # noqa: BLE001
            print("[Life] 旧日程清理失败（已忽略）：%s" % e)
            return 0

    def load_schedule_stats(self):
        """读取类别统计（task_type → dict）；库不可用返回空字典。"""
        if self._db is None:
            return {}
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT task_type, dur_mean, dur_var, samples, dev_streak, done_count, "
                    "defer_count, change_point_ts, updated_ts FROM schedule_stats").fetchall()
            out = {}
            for r in rows:
                key = str(r[0] or "general")
                out[key] = {
                    "task_type": key, "dur_mean": float(r[1] or 60.0),
                    "dur_var": float(r[2] or 900.0), "samples": _safe_int(r[3], 0),
                    "dev_streak": _safe_int(r[4], 0), "done_count": _safe_int(r[5], 0),
                    "defer_count": _safe_int(r[6], 0),
                    "change_point_ts": float(r[7] or 0.0), "updated_ts": float(r[8] or 0.0),
                }
            return out
        except Exception as e:              # noqa: BLE001
            print("[Life] 类别统计读取失败（已忽略）：%s" % e)
            return {}

    def save_schedule_stats(self, stats):
        """写入类别统计（整表 upsert）；失败忽略。"""
        if self._db is None or not isinstance(stats, dict):
            return False
        try:
            with self._lock:
                for key, raw in stats.items():
                    if not isinstance(raw, dict):
                        continue
                    cat = str(key or raw.get("task_type") or "general")
                    self._db.execute(
                        "INSERT INTO schedule_stats(task_type, dur_mean, dur_var, samples, "
                        "dev_streak, done_count, defer_count, change_point_ts, updated_ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_type) DO UPDATE SET "
                        "dur_mean=excluded.dur_mean, dur_var=excluded.dur_var, "
                        "samples=excluded.samples, dev_streak=excluded.dev_streak, "
                        "done_count=excluded.done_count, defer_count=excluded.defer_count, "
                        "change_point_ts=excluded.change_point_ts, updated_ts=excluded.updated_ts",
                        (cat, float(raw.get("dur_mean", 60.0) or 60.0),
                         float(raw.get("dur_var", 900.0) or 900.0),
                         _safe_int(raw.get("samples"), 0), _safe_int(raw.get("dev_streak"), 0),
                         _safe_int(raw.get("done_count"), 0), _safe_int(raw.get("defer_count"), 0),
                         float(raw.get("change_point_ts", 0.0) or 0.0),
                         float(raw.get("updated_ts", 0.0) or 0.0)))
                self._db.commit()
            return True
        except Exception as e:              # noqa: BLE001
            print("[Life] 类别统计写入失败（已忽略）：%s" % e)
            return False

    def list_schedule_history(self, limit=500):
        """学习 / 复盘用：所有状态（完成 / 取消 / 归档）按更新时间倒序。"""
        try:
            n = max(1, min(int(limit or 0), 1000))
        except (TypeError, ValueError):
            n = 500
        if self._db is None:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT %s FROM schedule_items ORDER BY updated_ts DESC, id DESC LIMIT ?"
                    % _SCHEDULE_SELECT, (n,)).fetchall()
            return [self._schedule_row(r) for r in rows]
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程历史读取失败（已忽略）：%s" % e)
            return []

    def record_feedback(self, item_id, outcome="done", actual_min=0, now=None):
        """记录一次执行结果：done / deferred / skipped；返回更新后的条目或 ``{}``。

        - ``done``：状态置 done，写入 actual_min / completed_ts；
        - ``deferred``：推迟计数 +1，清空时间回到待定，提醒标记重置（下次还能提醒）；
        - ``skipped``：状态置 cancelled。
        """
        if self._db is None:
            return {}
        try:
            sid = int(item_id)
        except (TypeError, ValueError, OverflowError):
            return {}
        result = str(outcome or "done").strip().lower()
        if result not in ("done", "deferred", "skipped"):
            result = "done"
        try:
            actual = max(0.0, float(actual_min or 0))
        except (TypeError, ValueError, OverflowError):
            actual = 0.0
        if not math.isfinite(actual):
            actual = 0.0
        t = _as_ts(now) or time.time()
        try:
            with self._lock:
                if result == "done":
                    self._db.execute(
                        "UPDATE schedule_items SET status='done', actual_min=?, completed_ts=?, "
                        "outcome='done', updated_ts=? WHERE id=?",
                        (actual, t, t, sid))
                elif result == "deferred":
                    self._db.execute(
                        "UPDATE schedule_items SET status='planned', start_ts=0, when_text='待定', "
                        "auto_planned=0, reminded_ts=0, defer_count=defer_count+1, "
                        "outcome='deferred', updated_ts=? WHERE id=?", (t, sid))
                else:
                    self._db.execute(
                        "UPDATE schedule_items SET status='cancelled', outcome='skipped', "
                        "updated_ts=? WHERE id=?", (t, sid))
                self._db.commit()
                r = self._db.execute(
                    "SELECT %s FROM schedule_items WHERE id = ?" % _SCHEDULE_SELECT,
                    (sid,)).fetchone()
            return self._schedule_row(r) if r is not None else {}
        except Exception as e:              # noqa: BLE001
            print("[Life] 执行结果写入失败（已忽略）：%s" % e)
            return {}

    def list_schedule_plan(self, limit=200):
        """自动排班输入：当前所有未归档的 planned 条目（含待定项）。"""
        return self.list_schedule(limit, include_undated=True, include_archived=False)

    def update_schedule_plan(self, assignments, now=None):
        """写入自动排班结果（start_ts / when_text / duration_min / auto_planned）。"""
        if not isinstance(assignments, list) or self._db is None:
            return []
        t = _as_ts(now) or time.time()
        with self._lock:
            try:
                for a in assignments:
                    if not isinstance(a, dict):
                        continue
                    try:
                        sid = int(a.get("id"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    try:
                        start = float(a.get("start_ts", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        start = 0.0
                    if not math.isfinite(start) or start < 0.0:
                        start = 0.0
                    when_text = _clip(a.get("when_text", ""), 64)
                    duration = max(5, min(24 * 60, _safe_int(a.get("duration_min"), 60)))
                    # 重新排班后重置提醒标记：新时间到了还会再提醒一次。
                    self._db.execute(
                        "UPDATE schedule_items SET start_ts = ?, when_text = ?, duration_min = ?, "
                        "auto_planned = 1, reminded_ts = 0, updated_ts = ? WHERE id = ? AND archived = 0",
                        (start, when_text, duration, t, sid))
                self._db.commit()
            except Exception as e:          # noqa: BLE001
                print("[Life] 排班写入失败（已忽略）：%s" % e)
                try:
                    self._db.rollback()
                except Exception:           # noqa: BLE001
                    pass
                return []
        return self.list_schedule(200)

    def archive_schedule(self, ids=None, before_ts=None, now=None):
        """留档：把指定 / 过期条目移入归档（不删除，仍可查）。返回归档条数。"""
        if self._db is None:
            return 0
        t = _as_ts(now) or time.time()
        where, params = ["archived = 0"], []
        id_list = []
        for v in (ids or []):
            try:
                id_list.append(int(v))
            except (TypeError, ValueError, OverflowError):
                continue
        if id_list:
            where.append("id IN (%s)" % ",".join("?" * len(id_list)))
            params.extend(id_list)
        try:
            cutoff = float(before_ts) if before_ts is not None else (t - 12 * 3600.0)
        except (TypeError, ValueError, OverflowError):
            cutoff = t - 12 * 3600.0
        if not id_list:
            where.append("start_ts > 0 AND start_ts < ?")
            params.append(cutoff)
        try:
            with self._lock:
                cur = self._db.execute(
                    "UPDATE schedule_items SET archived = 1, archived_ts = ?, updated_ts = ? "
                    "WHERE " + " AND ".join(where), [t, t] + params)
                self._db.commit()
                return int(cur.rowcount or 0)
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程归档失败（已忽略）：%s" % e)
            return 0

    def cleanup_schedule(self, days=30, include_unarchived=False, now=None):
        """清理：删除超过保留期的归档条目（可选连过期未归档一起删）。返回删除条数。"""
        if self._db is None:
            return 0
        t = _as_ts(now) or time.time()
        try:
            keep_days = max(1, min(3650, int(days)))
        except (TypeError, ValueError):
            keep_days = 30
        cutoff = t - keep_days * 86400.0
        try:
            with self._lock:
                cur = self._db.execute(
                    "DELETE FROM schedule_items WHERE archived = 1 AND archived_ts > 0 "
                    "AND archived_ts < ?", (cutoff,))
                removed = int(cur.rowcount or 0)
                if include_unarchived:
                    cur2 = self._db.execute(
                        "DELETE FROM schedule_items WHERE archived = 0 AND start_ts > 0 "
                        "AND start_ts < ?", (cutoff,))
                    removed += int(cur2.rowcount or 0)
                self._db.commit()
                return removed
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程清理失败（已忽略）：%s" % e)
            return 0

    def due_reminders(self, now=None, window_sec=600, lead_sec=60):
        """到点提醒：已到 / 即将到（默认提前 60 秒）且尚未提醒过的未归档条目。"""
        if self._db is None:
            return []
        ref = _as_ts(now) or time.time()
        try:
            window = max(0.0, float(window_sec))
        except (TypeError, ValueError):
            window = 600.0
        try:
            lead = max(0.0, float(lead_sec))
        except (TypeError, ValueError):
            lead = 60.0
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT %s FROM schedule_items WHERE status = 'planned' AND archived = 0 "
                    "AND reminded_ts = 0 AND start_ts > 0 AND start_ts <= ? AND start_ts >= ? "
                    "ORDER BY start_ts ASC" % _SCHEDULE_SELECT,
                    (ref + lead, ref - window)).fetchall()
            return [self._schedule_row(r) for r in rows]
        except Exception as e:              # noqa: BLE001
            print("[Life] 到点提醒读取失败（已忽略）：%s" % e)
            return []

    def mark_reminded(self, item_id, now=None):
        """标记已提醒（失败忽略）；返回是否更新。"""
        if self._db is None:
            return False
        try:
            sid = int(item_id)
        except (TypeError, ValueError, OverflowError):
            return False
        t = _as_ts(now) or time.time()
        try:
            with self._lock:
                cur = self._db.execute(
                    "UPDATE schedule_items SET reminded_ts = ?, updated_ts = ? WHERE id = ?",
                    (t, t, sid))
                self._db.commit()
                return bool(cur.rowcount)
        except Exception as e:              # noqa: BLE001
            print("[Life] 提醒标记失败（已忽略）：%s" % e)
            return False

    def auto_archive(self, now=None, days=7):
        """自动留档：把已经过去 N 天的条目移入归档（默认 7 天，不删除）。"""
        if self._db is None:
            return 0
        ref = _as_ts(now) or time.time()
        try:
            keep_days = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            keep_days = 7
        cutoff = ref - keep_days * 86400.0
        try:
            with self._lock:
                cur = self._db.execute(
                    "UPDATE schedule_items SET archived = 1, archived_ts = ?, updated_ts = ? "
                    "WHERE archived = 0 AND start_ts > 0 AND start_ts < ?",
                    (ref, ref, cutoff))
                self._db.commit()
                return int(cur.rowcount or 0)
        except Exception as e:              # noqa: BLE001
            print("[Life] 自动留档失败（已忽略）：%s" % e)
            return 0

    def get_schedule_item(self, item_id):
        """按 id 取一条日程（含归档）；不存在 / 库不可用返回 ``{}``。"""
        if self._db is None:
            return {}
        try:
            sid = int(item_id)
        except (TypeError, ValueError, OverflowError):
            return {}
        try:
            with self._lock:
                r = self._db.execute(
                    "SELECT %s FROM schedule_items WHERE id = ?" % _SCHEDULE_SELECT,
                    (sid,)).fetchone()
            return self._schedule_row(r) if r is not None else {}
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程读取失败（已忽略）：%s" % e)
            return {}

    def list_schedule(self, limit=50, include_undated=True, include_archived=False,
                      include_done=False):
        """日程列表：先按时间升序列出已定日期的，再按新到旧列出待定项。

        默认**不含归档、不含已完成**（自动排班 / 提醒只看 planned）；``include_archived=True``
        供留档查看；``include_done=True`` 给日历展示用——完成的任务仍留在周表里（标「已完成」）。
        取消 / 归档仍然不进来。
        """
        try:
            n = max(0, min(int(limit or 0), 200))
        except (TypeError, ValueError):
            n = 0
        if n == 0 or self._db is None:
            return []
        arch = "" if include_archived else " AND archived = 0"
        state = " AND status IN ('planned', 'done')" if include_done else " AND status = 'planned'"
        try:
            with self._lock:
                dated = [self._schedule_row(r) for r in self._db.execute(
                    "SELECT %s FROM schedule_items WHERE start_ts > 0%s%s "
                    "ORDER BY start_ts ASC, id ASC LIMIT ?" % (_SCHEDULE_SELECT, state, arch),
                    (n,)).fetchall()]
                rest = n - len(dated)
                undated = []
                if include_undated and rest > 0:
                    undated = [self._schedule_row(r) for r in self._db.execute(
                        "SELECT %s FROM schedule_items WHERE start_ts <= 0%s%s "
                        "ORDER BY id DESC LIMIT ?" % (_SCHEDULE_SELECT, state, arch),
                        (rest,)).fetchall()]
            return dated + undated
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程读取失败（已忽略）：%s" % e)
            return []

    def next_schedule(self, now=None):
        """下一条已定日期的日程（用于时间上下文的 eventual 尺度）；没有返回 ``{}``。"""
        if self._db is None:
            return {}
        ref = _as_ts(now) or time.time()
        try:
            with self._lock:
                r = self._db.execute(
                    "SELECT %s FROM schedule_items WHERE status = 'planned' AND archived = 0 "
                    "AND start_ts >= ? ORDER BY start_ts ASC, id ASC LIMIT 1" % _SCHEDULE_SELECT,
                    (ref,)).fetchone()
            return self._schedule_row(r) if r is not None else {}
        except Exception as e:              # noqa: BLE001
            print("[Life] 下一条日程读取失败（已忽略）：%s" % e)
            return {}

    def set_schedule_status(self, item_id, status="done"):
        """标记日程完成 / 取消；返回是否更新成功。"""
        try:
            sid = int(item_id)
        except (TypeError, ValueError):
            return False
        st = str(status or "").strip().lower()
        if st not in ("planned", "done", "cancelled"):
            return False
        if self._db is None:
            return False
        with self._lock:
            try:
                cur = self._db.execute(
                    "UPDATE schedule_items SET status = ?, updated_ts = ? WHERE id = ?",
                    (st, time.time(), sid))
                self._db.commit()
                return bool(cur.rowcount)
            except Exception as e:          # noqa: BLE001
                print("[Life] 日程状态更新失败（已忽略）：%s" % e)
                return False

    # ================= 写入 =================

    def touch(self, kind, source="", summary="", meta=None, now=None, event=True):
        """记录一次生命事件并推进对应时间戳；返回事件 id（``event=False`` 或库不可用时 0）。

        ``kind`` 见 ``_TOUCH_FIELDS``；未知 kind 仍会写事件，但不推进任何时间戳。
        这是**规则层**写入，不接受 LLM 输出；任何故障只打日志。
        """
        t = _as_ts(now) or time.time()
        k = _clip(kind, 32) or "event"
        src = _clip(source, 32)
        text = _clip(summary)
        m = meta if isinstance(meta, dict) else {}
        eid = 0
        with self._lock:
            # 先把“到此刻为止”的慢变量结清，再叠加这次事件的小增量。
            self._evolve_locked(t)
            self._apply_interaction_locked(k)
            fields = _TOUCH_FIELDS.get(k, ())
            for f in fields:
                self._state[f] = t
            self._state["updated_ts"] = t
            if event and self._db is not None:
                try:
                    cur = self._db.execute(
                        "INSERT INTO life_events(ts, kind, source, summary, meta, superseded_by) "
                        "VALUES(?,?,?,?,?,0)",
                        (t, k, src, text, _json(m)))
                    self._db.commit()
                    eid = int(cur.lastrowid or 0)
                    self._trim_events_locked()
                except Exception as e:      # noqa: BLE001 —— 写失败只降级，不抛回编排层
                    print("[Life] 时间线写入失败（已忽略）：%s" % e)
            self._persist_state_locked()
        return eid

    def tick(self, now=None, mood=None):
        """心跳：推进慢变量、记录最后活跃，并可选落盘一次情绪快照。"""
        t = _as_ts(now) or time.time()
        clean = _sanitize_mood(mood) if mood is not None else {}
        with self._lock:
            self._evolve_locked(t)
            self._state["last_tick_ts"] = t
            self._state["updated_ts"] = t
            if clean:
                self._state["mood"] = clean
                self._state["mood_ts"] = t
            self._persist_state_locked()

    def save_mood(self, mood, now=None, touch_tick=True):
        """落盘情绪快照（只 v/c/a）；脏值忽略，不覆盖旧快照。"""
        clean = _sanitize_mood(mood)
        if not clean:
            return
        t = _as_ts(now) or time.time()
        with self._lock:
            self._evolve_locked(t)
            self._state["mood"] = clean
            self._state["mood_ts"] = t
            self._state["updated_ts"] = t
            if touch_tick:
                self._state["last_tick_ts"] = t
            self._persist_state_locked()

    def close(self):
        with self._lock:
            db, self._db = self._db, None
            if db is not None:
                try:
                    db.close()
                except Exception:           # noqa: BLE001
                    pass

    # ================= 内部 =================

    def _open(self):
        try:
            d = os.path.dirname(os.path.abspath(self._db_path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            db = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5.0)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            for sql in _SCHEMA:
                db.execute(sql)
            self._migrate_schedule_locked(db)
            self._dedupe_schedule_locked(db)
            db.commit()
            self._db = db
        except Exception as e:              # noqa: BLE001 —— 库不可用＝只保留内存时间线
            print("[Life] 时间线库不可用（降级为内存）：%s" % e)
            self._db = None

    @staticmethod
    def _dedupe_schedule_locked(db):
        """启动时清掉历史重复：同一课程格子 / 同一时刻同一标题只保留最新一行。

        旧版去重键带 ``source``，每次重扫或新增一张课表都会把整张表再插一遍；
        这里做一次幂等清理：保留 ``updated_ts`` 最新的一行，合并 source 与
        教师 / 地点 / 备注，再删除其余行。失败只打日志，不影响启动。
        """
        try:
            rows = db.execute(
                "SELECT id, source, date_text, weekday, period, start_ts, title, notes, "
                "teacher, location, updated_ts, kind FROM schedule_items "
                "WHERE archived = 0 AND status = 'planned' ORDER BY id").fetchall()
            groups = {}
            for r in rows:
                title = str(r[6] or "").strip()
                if title == "":
                    continue
                period = str(r[4] or "").strip()
                kind = str(r[11] or "").strip()
                if kind == "course" or period != "":
                    date_text = str(r[2] or "").strip()
                    if date_text != "":
                        key = ("course", date_text, period, title)
                    else:
                        key = ("course", _safe_int(r[3], -1), period, title)
                else:
                    key = ("task", float(r[5] or 0.0), title)
                groups.setdefault(key, []).append(r)
            removed = 0
            for group in groups.values():
                if len(group) < 2:
                    continue
                keep = max(group, key=lambda x: (float(x[10] or 0.0), int(x[0])))
                src = ""
                notes = str(keep[7] or "")
                teacher = str(keep[8] or "")
                location = str(keep[9] or "")
                for r in group:
                    src = _merge_sources(src, r[1])
                    if len(str(r[7] or "")) > len(notes):
                        notes = str(r[7])
                    if len(str(r[8] or "")) > len(teacher):
                        teacher = str(r[8])
                    if len(str(r[9] or "")) > len(location):
                        location = str(r[9])
                db.execute(
                    "UPDATE schedule_items SET source = ?, notes = ?, teacher = ?, "
                    "location = ? WHERE id = ?",
                    (src, notes, teacher, location, int(keep[0])))
                for r in group:
                    if int(r[0]) != int(keep[0]):
                        db.execute("DELETE FROM schedule_items WHERE id = ?", (int(r[0]),))
                        removed += 1
            if removed:
                print("[Life] 日程去重：清理 %d 条历史重复" % removed)
        except Exception as e:              # noqa: BLE001
            print("[Life] 日程去重失败（忽略）：%s" % e)

    @staticmethod
    def _migrate_schedule_locked(db):
        """旧库升级：缺哪列补哪列（只加列、不改已有数据，失败不影响其他功能）。"""
        try:
            have = {str(r[1]) for r in db.execute("PRAGMA table_info(schedule_items)").fetchall()}
            for name, spec in _SCHEDULE_EXTRA_COLS.items():
                if name not in have:
                    db.execute("ALTER TABLE schedule_items ADD COLUMN %s %s" % (name, spec))
            # 旧课表行没有 kind：只有**真的带节次**的才补成 course；仅写星期的是普通约定
            # （如群公告里的「周四篮球赛」），不能当成不可移动的固定课。
            db.execute("UPDATE schedule_items SET kind = 'course' "
                       "WHERE kind = 'task' AND period <> ''")
            db.execute("UPDATE schedule_items SET kind = 'task' "
                       "WHERE kind = 'course' AND period = '' AND period_start = 0")
        except Exception as e:              # noqa: BLE001 —— 迁移失败只打日志
            print("[Life] 日程表升级失败（继续用旧列）：%s" % e)

    def _load(self, now=None):
        if self._db is None:
            return
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT state FROM life_state WHERE id = 1").fetchone()
            if row is None:
                self._persist_state_locked()
                return
            raw = _loads(row[0])
            self._state = _sanitize_state(raw, time.time())
            # 进程不在时时间也在走：启动即把慢变量结转到“现在”。
            with self._lock:
                self._evolve_locked(_as_ts(now) or time.time())
                self._persist_state_locked()
        except Exception as e:              # noqa: BLE001
            print("[Life] 时间线状态读取失败（按空状态继续）：%s" % e)
            self._state = _default_state(time.time())

    def _evolve_locked(self, now):
        """按真实流逝时间推进精力 / 无聊 / 亲密度 / 信任（闭式指数，不做逐秒循环）。

        这层是**规则**，不调用 LLM：无互动时精力会掉、无聊会涨；亲密度与信任回得很慢。
        时钟倒退 / 脏值一律跳过，不把状态往过去倒。
        """
        last = _as_ts(self._state.get("last_evolve_ts"))
        if last is None:
            self._state["last_evolve_ts"] = now
            return
        if now <= last:
            return
        dt_h = (now - last) / 3600.0

        energy = _clamp01(self._state.get("energy"), LIFE_ENERGY_BASE)
        self._state["energy"] = ENERGY_FLOOR + (energy - ENERGY_FLOOR) * math.exp(
            -dt_h / ENERGY_TAU_H)

        boredom = _clamp01(self._state.get("boredom"), LIFE_BOREDOM_BASE)
        self._state["boredom"] = 1.0 - (1.0 - boredom) * math.exp(
            -dt_h / BOREDOM_TAU_H)

        affinity = _clamp01(self._state.get("affinity"), LIFE_AFFINITY_BASE)
        self._state["affinity"] = LIFE_AFFINITY_BASE + (
            affinity - LIFE_AFFINITY_BASE) * math.exp(-dt_h / AFFINITY_TAU_H)

        trust = _clamp01(self._state.get("trust"), LIFE_TRUST_BASE)
        self._state["trust"] = LIFE_TRUST_BASE + (
            trust - LIFE_TRUST_BASE) * math.exp(-dt_h / TRUST_TAU_H)

        dominance = _clamp01(self._state.get("dominance"), LIFE_DOMINANCE_BASE)
        self._state["dominance"] = LIFE_DOMINANCE_BASE + (
            dominance - LIFE_DOMINANCE_BASE) * math.exp(-dt_h / DOMINANCE_TAU_H)

        for key in ("energy", "boredom", "affinity", "trust", "dominance"):
            self._state[key] = _clamp01(self._state.get(key), 0.0)
        self._state["last_evolve_ts"] = now
        self._state["updated_ts"] = now

    def _apply_interaction_locked(self, kind):
        """把一次事件的小增量加到慢变量上；未知事件零动作。"""
        d = _INTERACTION_EFFECTS.get(str(kind))
        if d is None:
            return
        for key, delta in zip(("energy", "boredom", "affinity", "trust", "dominance"), d):
            cur = _clamp01(self._state.get(key), 0.0)
            self._state[key] = _clamp01(cur + float(delta), 0.0)

    def _persist_state_locked(self):
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO life_state(id, updated_ts, state) VALUES(1, ?, ?)",
                (float(self._state.get("updated_ts") or time.time()),
                 _json(self._state)))
            self._db.commit()
        except Exception as e:              # noqa: BLE001
            print("[Life] 时间线状态写入失败（已忽略）：%s" % e)

    def _trim_events_locked(self):
        """按 id 只保留最近 ``MAX_EVENTS`` 条（不逐条删除，一次 SQL 收口）。"""
        try:
            self._db.execute(
                "DELETE FROM life_events WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM life_events)",
                (MAX_EVENTS,))
            self._db.commit()
        except Exception as e:              # noqa: BLE001
            print("[Life] 时间线裁剪失败（已忽略）：%s" % e)
