"""生命时间线（拟人化 P0）：跨重启的时间事实，而不是“提示词里塞时间戳”。

设计目标（对应 DeepSeek 方案的 §2.1 / §2.2）：
- **只追加**：互动、主动搭话、聊天、对局开始/结束、落子都写成带墙钟的事件；
  修正由新事件表达，旧事件保留（本版先不实现 supersede，只把列留在表里）。
- **持久化**：状态与事件落 SQLite（默认 ``<backend>/data/life.db``），
  kill -9 / 重启后仍能回答“上一回它是什么时候见到 Estarth 的”。
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
        updated_ts REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_life_events_ts ON life_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_start ON schedule_items(start_ts)",
)

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
        return {
            "id": int(r[0]), "start_ts": float(r[1]), "when_text": str(r[2]),
            "title": str(r[3]), "notes": str(r[4]), "source": str(r[5]),
            "status": str(r[6]),
        }

    def upsert_schedule(self, items, source="", now=None):
        """写入 / 更新日程条目；返回保存后的列表。

        ``items`` 由 ``schedule.normalize_items`` 产出：``start_ts``（0=待定）、
        ``when_text``、``title``、``notes``。按 ``source + start_ts + title`` 去重，
        同一份资料重复扫描不会产生重复日程。失败返回 ``[]``。
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
                    row = self._db.execute(
                        "SELECT id FROM schedule_items WHERE source = ? AND start_ts = ? AND title = ? "
                        "ORDER BY id ASC LIMIT 1", (src, start, title)).fetchone()
                    if row is None:
                        cur = self._db.execute(
                            "INSERT INTO schedule_items(start_ts, when_text, title, notes, source, "
                            "status, created_ts, updated_ts) VALUES(?,?,?,?,?,'planned',?,?)",
                            (start, when_text, title, notes, src, t, t))
                        sid = int(cur.lastrowid)
                    else:
                        sid = int(row[0])
                        self._db.execute(
                            "UPDATE schedule_items SET when_text = ?, notes = ?, updated_ts = ? "
                            "WHERE id = ?", (when_text, notes, t, sid))
                    got = self._db.execute(
                        "SELECT id, start_ts, when_text, title, notes, source, status "
                        "FROM schedule_items WHERE id = ?", (sid,)).fetchone()
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

    def list_schedule(self, limit=50, include_undated=True):
        """日程列表：先按时间升序列出已定日期的，再按新到旧列出待定项。"""
        try:
            n = max(0, min(int(limit or 0), 200))
        except (TypeError, ValueError):
            n = 0
        if n == 0 or self._db is None:
            return []
        try:
            with self._lock:
                dated = [self._schedule_row(r) for r in self._db.execute(
                    "SELECT id, start_ts, when_text, title, notes, source, status "
                    "FROM schedule_items WHERE status = 'planned' AND start_ts > 0 "
                    "ORDER BY start_ts ASC, id ASC LIMIT ?", (n,)).fetchall()]
                rest = n - len(dated)
                undated = []
                if include_undated and rest > 0:
                    undated = [self._schedule_row(r) for r in self._db.execute(
                        "SELECT id, start_ts, when_text, title, notes, source, status "
                        "FROM schedule_items WHERE status = 'planned' AND start_ts <= 0 "
                        "ORDER BY id DESC LIMIT ?", (rest,)).fetchall()]
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
                    "SELECT id, start_ts, when_text, title, notes, source, status "
                    "FROM schedule_items WHERE status = 'planned' AND start_ts >= ? "
                    "ORDER BY start_ts ASC, id ASC LIMIT 1", (ref,)).fetchone()
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
            db.commit()
            self._db = db
        except Exception as e:              # noqa: BLE001 —— 库不可用＝只保留内存时间线
            print("[Life] 时间线库不可用（降级为内存）：%s" % e)
            self._db = None

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
