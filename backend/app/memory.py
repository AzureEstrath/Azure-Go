"""长期记忆：跨局记得玩家这个人（与 ``state.py`` 的局内记忆互不替代）。

局内记忆（``GameState.memory_log``）管「这盘棋」，随快照回滚、上限 18 条；
长期记忆管「这个人」，落 SQLite 文件、跨局存活。

设计取舍：
- **只用标准库**（``sqlite3`` + ``json``）：向量以 JSON 文本存库，余弦在 Python 里算，
  不引入 numpy / chroma / faiss——记忆规模上限 2000 条，这点计算量不值得加依赖。
- **检索两级**：配了 ``embed_fn`` 就按余弦相似度排序；**向量命中不足 ``k`` 条时，余量用关键词
  从尚未命中的行里补齐**（混合库：写入时 embedding 失败或旧库遗留的无向量行，不会被有向量的
  行压掉）；没配 ``embed_fn``、或云端 embedding 报错（401 / 超时）时整段降级为关键词匹配
  （score 固定 0.0，不伪造相似度）。
- **一切故障都不影响对局**：DB 打不开 / 不可写、embedding 抛异常都只打日志，
  写失败返回 ``None``、检索返回 ``[]``，绝不把异常抛回编排层。
- **写入有硬上限**：单条 ``MAX_TEXT_LEN`` 字、同 tag + 同 text 去重、
  库内总行数 ``MEMORY_MAX_ROWS``（超出按时间淘汰最旧，连同其向量）。
- **并发**：WS 与 REST 可能同时读写——连接 ``check_same_thread=False``，
  所有库操作走同一把锁；网络调用（embedding）放在锁外，别让慢接口卡住别的请求。
"""

import json
import math
import os
import re
import sqlite3
import threading
import time

from . import memory_semantics as emo_sem

MEMORY_MAX_ROWS = 2000          # 库内记忆行数上限（超出按时间淘汰最旧）
CHAT_MAX_ROWS = 1000            # 聊天原文流水上限（超出按时间淘汰最旧；与 memories 解耦）
EPISODE_GAP_SEC = 900           # 一段 episode 的空档上限：超过 15 分钟就收束成摘要
EPISODE_MAX_TURNS = 6           # 一段 episode 最多几轮对话（MaiBot 式 episode，不无限长）
EPISODE_MAX_CHARS = 620         # 一段 episode 的摘要上限（超了先收束，另起一段）
EPISODE_VALENCE_JUMP = 0.30     # 情绪效价跳变边界：同一段聊天里大起大落时另起一段
MAX_TEXT_LEN = 200              # 单条记忆写入时的字数上限
MAX_CHAT_USER_LEN = 240          # 聊天流水：玩家单句上限
MAX_CHAT_REPLY_LEN = 360         # 聊天流水：Azure 单句上限
MAX_INJECT_ITEM_CHARS = 120     # 注入提示词时单条记忆的字数上限

# 关键词检索的切分：ASCII 按词、CJK 按二元组（中文没空格，整串匹配对不上自然问句）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RE = re.compile(r"[0-9a-z_]+")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        kind TEXT NOT NULL DEFAULT 'fact',
        tag TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}',
        emo_v REAL NOT NULL DEFAULT -1,
        emo_a REAL NOT NULL DEFAULT -1,
        emo_tag TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by INTEGER NOT NULL DEFAULT 0,
        superseded_ts REAL NOT NULL DEFAULT 0,
        superseded_reason TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS vectors (
        mem_id INTEGER PRIMARY KEY,
        vec TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        result TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        moves TEXT NOT NULL DEFAULT '[]',
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        mode TEXT NOT NULL DEFAULT '',
        user_text TEXT NOT NULL DEFAULT '',
        reply_text TEXT NOT NULL DEFAULT '',
        mood TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        started_ts REAL NOT NULL DEFAULT 0,
        ended_ts REAL NOT NULL DEFAULT 0,
        turns INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        mood TEXT NOT NULL DEFAULT '{}',
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
)

# 旧库缺列时的补列（表 -> 列 -> 建列语句）；主键列不在此列，缺主键的库不认。
_COLUMNS = {
    "memories": {
        "ts": "REAL NOT NULL DEFAULT 0",
        "kind": "TEXT NOT NULL DEFAULT 'fact'",
        "tag": "TEXT NOT NULL DEFAULT ''",
        "text": "TEXT NOT NULL DEFAULT ''",
        "meta": "TEXT NOT NULL DEFAULT '{}'",
        # v4.3.0：二维情绪矩阵 + 标签 + supersede 状态（旧库自动补列）
        "emo_v": "REAL NOT NULL DEFAULT -1",
        "emo_a": "REAL NOT NULL DEFAULT -1",
        "emo_tag": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "superseded_by": "INTEGER NOT NULL DEFAULT 0",
        "superseded_ts": "REAL NOT NULL DEFAULT 0",
        "superseded_reason": "TEXT NOT NULL DEFAULT ''",
    },
    "vectors": {"vec": "TEXT NOT NULL DEFAULT '[]'"},
    "games": {
        "ts": "REAL NOT NULL DEFAULT 0",
        "result": "TEXT NOT NULL DEFAULT ''",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "moves": "TEXT NOT NULL DEFAULT '[]'",
        "meta": "TEXT NOT NULL DEFAULT '{}'",
    },
    "chat_history": {
        "ts": "REAL NOT NULL DEFAULT 0",
        "mode": "TEXT NOT NULL DEFAULT ''",
        "user_text": "TEXT NOT NULL DEFAULT ''",
        "reply_text": "TEXT NOT NULL DEFAULT ''",
        "mood": "TEXT NOT NULL DEFAULT '{}'",
    },
    "episodes": {
        "ts": "REAL NOT NULL DEFAULT 0",
        "source": "TEXT NOT NULL DEFAULT ''",
        "started_ts": "REAL NOT NULL DEFAULT 0",
        "ended_ts": "REAL NOT NULL DEFAULT 0",
        "turns": "INTEGER NOT NULL DEFAULT 0",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "mood": "TEXT NOT NULL DEFAULT '{}'",
        "meta": "TEXT NOT NULL DEFAULT '{}'",
    },
}


def _clip(text: str, limit: int) -> str:
    """截到 ``limit`` 字；真的截了才用「…」标记（不静默砍半句）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[:limit - 1] + "…"


def _json(v) -> str:
    """把 meta 之类的字典序列化成文本；不是字典或不可序列化时退回 ``'{}'``。"""
    try:
        return json.dumps(v if isinstance(v, dict) else {}, ensure_ascii=False)
    except Exception:                   # noqa: BLE001 —— 脏 meta 不值得丢一条记忆
        return "{}"


def _loads_meta(raw) -> dict:
    """把库里的 meta 文本解析成字典；脏值 / 非字典一律 ``{}``（不抛异常）。"""
    try:
        got = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _keywords(query: str) -> list:
    """把查询切成关键词：ASCII 按词、CJK 按二元组（单字查询退化成单字）。

    整串匹配对不上自然问句（「讲讲定式」对不上「定式讲解」），二元组是纯标准库里
    最省事又够用的切法；命中的词越多越靠前不在关键词模式里体现（score 固定 0.0）。
    """
    q = str(query or "").strip().lower()
    if q == "":
        return []
    terms = [w for w in _WORD_RE.findall(q) if len(w) >= 2]
    for run in _CJK_RE.findall(q):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    if not terms:
        terms.append(q)
    return terms


def cosine(a: list, b: list) -> float:
    """纯 Python 余弦相似度；空向量 / 维度不一致返回 0.0（不抛异常）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += float(x) * float(y)
        na += float(x) * float(x)
        nb += float(y) * float(y)
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / math.sqrt(na * nb))


def inject_text(items: list, max_chars: int) -> str:
    """把召回结果拼成给提示词用的一段文字（逐条截到 ``MAX_INJECT_ITEM_CHARS``）。

    只管「拼」和「截」：标题、免责措辞由 ``prompts.long_term_note`` 负责；
    分隔符也算进 ``max_chars``，返回值不会超预算。
    """
    out = []
    used = 0
    limit = max(0, int(max_chars or 0))
    for it in items or []:
        text = " ".join(str((it or {}).get("text", "")).split())
        if text == "":
            continue
        piece = _clip(text, MAX_INJECT_ITEM_CHARS)
        sep = 1 if out else 0           # 「；」分隔符也占预算
        room = limit - used - sep
        if room <= 1:                   # 只剩放不下一个字的位置：不如不加
            break
        if len(piece) > room:
            piece = _clip(piece, room)
        out.append(piece)
        used += sep + len(piece)
    return "；".join(out)


def _valence(mood):
    """从 episode 的 mood 列 / 当前 mood 里取愉悦度；取不到返回 ``None``（不猜）。"""
    src = mood
    if isinstance(src, str):
        try:
            src = json.loads(src or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(src, dict):
        return None
    try:
        v = float(src.get("v"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v) or v < 0.0 or v > 1.0:
        return None
    return v


def _valence_jump(prev_mood, mood) -> bool:
    """情绪边界：一段 episode 内效价跳变超过阈值 ⇒ 上一段收束。

    这是规则层的**事件分割**信号之一（话题 / 时间 / 情绪三路里先落情绪），
    不调用 LLM；取不到两边的效价时一律不切（宁可少切，不把普通聊天切碎）。
    """
    a = _valence(prev_mood)
    b = _valence(mood)
    if a is None or b is None:
        return False
    return abs(a - b) >= EPISODE_VALENCE_JUMP


class MemoryStore:
    """长期记忆库：一条 sqlite 连接 + 一把锁，所有操作都失败安全。"""

    def __init__(self, db_path: str, embed_fn=None, now_fn=None):
        """``embed_fn(text) -> list[float]`` 与 ``now_fn() -> float`` 都可注入（测试用假实现）。"""
        self._db_path = str(db_path or "")
        self._embed_fn = embed_fn
        self._now_fn = now_fn if callable(now_fn) else time.time
        self._max_rows = MEMORY_MAX_ROWS     # 测试可临时收紧，验证淘汰
        self._lock = threading.Lock()
        self._conn = None
        self._embed_warned = False
        if self._db_path == "":
            print("[Memory] 未给数据库路径，长期记忆本次运行内关闭。")
            return
        try:
            d = os.path.dirname(os.path.abspath(self._db_path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
            self._conn = conn
        except Exception as e:              # noqa: BLE001 —— 库开不了就当记忆功能不存在
            print("[Memory] 数据库不可用（%s），长期记忆本次运行内关闭：%s" % (self._db_path, e))
            self._conn = None

    # —— 建库 / 迁移 ——

    @staticmethod
    def _ensure_schema(conn):
        """建表 + 补列：旧库缺表、缺列都自动补齐，绝不因为库形状不对而崩。"""
        for ddl in _SCHEMA:
            conn.execute(ddl)
        for table, cols in _COLUMNS.items():
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
        try:
            # 唯一索引兜住并发下同 tag+text 的重复写入；旧库里已有重复行时建不起来，
            # 那种库去重退回写入前的查重（不因此判库不可用）。
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_tag_text ON memories(tag, text)")
        except Exception:                   # noqa: BLE001
            pass
        conn.commit()

    def _now(self) -> float:
        return float(self._now_fn())

    def _embed(self, text: str):
        """算向量；没配 embedding、或云端报错（401/超时）时返回 ``None``。

        失败只影响检索质量（降级为关键词），不影响写入；日志只在**首次**失败时打一行，
        避免每个回合刷屏。
        """
        if not callable(self._embed_fn):
            return None
        try:
            vec = [float(x) for x in self._embed_fn(text)]
            return vec if vec else None
        except Exception as e:              # noqa: BLE001
            if not self._embed_warned:
                self._embed_warned = True
                print("[Memory] embedding 不可用（本次运行内降级为关键词检索）：%s" % e)
            return None

    def _count(self, table: str) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])

    def _evict(self):
        """超过行数上限时按时间淘汰最旧（连同其向量）。调用方必须持锁。"""
        excess = self._count("memories") - int(self._max_rows)
        if excess <= 0:
            return
        rows = self._conn.execute(
            "SELECT id FROM memories ORDER BY ts ASC, id ASC LIMIT ?", (excess,)).fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return
        q = ",".join("?" * len(ids))
        self._conn.execute("DELETE FROM memories WHERE id IN (%s)" % q, ids)
        self._conn.execute("DELETE FROM vectors WHERE mem_id IN (%s)" % q, ids)

    # —— 对外接口 ——

    def write(self, text: str, *, kind: str = "fact", tag: str = "", meta: dict | None = None,
              mood=None) -> int | None:
        """写一条长期记忆，返回 id；失败返回 ``None``。

        规则：单条截到 ``MAX_TEXT_LEN``；同 tag + 同 text 视为重复（返回既有 id，
        不重复入库）；写入后超过 ``_max_rows`` 就淘汰最旧的。

        ``mood``（或 ``meta["mood"]``）里的三维情绪会转成**二维情绪矩阵**
        （``emo_v`` / ``emo_a`` + 九格标签）一起落库（v4.3.0）。情绪缺失时两列写 -1、
        标签空串——「不知道当时心情」不伪造成 0。
        """
        t = _clip(str(text or "").strip(), MAX_TEXT_LEN)
        if t == "":
            return None
        tag = str(tag or "")
        kind = str(kind or "fact")
        m = dict(meta) if isinstance(meta, dict) else {}
        emo = emo_sem.emotion_of_mood(mood if mood is not None else m.get("mood"))
        emo_v = float(emo["v"]) if emo else -1.0
        emo_a = float(emo["a"]) if emo else -1.0
        emo_tag = str(emo["tag"]) if emo else ""
        if self._conn is None:
            return None
        vec = self._embed(t)                # 网络调用放锁外，别卡住并发请求
        with self._lock:
            if self._conn is None:          # close() 可能刚好发生
                return None
            try:
                row = self._conn.execute(
                    "SELECT id, emo_v, status FROM memories WHERE tag = ? AND text = ? "
                    "ORDER BY id ASC LIMIT 1", (tag, t)).fetchone()
                if row is not None:
                    # 旧行没有情绪、这次有：顺手补上，不另起一行（也不覆盖已有标签）。
                    reactivated = str(row[2] or "active") != "active"
                    if reactivated:
                        # 同一条事实被重新写回（例如取消后又重新安排）：旧行重新生效，
                        # 否则会一直以「已失效」的身份存在，召回永远看不到。
                        self._conn.execute(
                            "UPDATE memories SET status = 'active', superseded_by = 0, "
                            "superseded_ts = 0, superseded_reason = '', meta = ?, ts = ? WHERE id = ?",
                            (_json(m), self._now(), int(row[0])))
                    if emo is not None and float(row[1] if row[1] is not None else -1.0) < 0.0:
                        self._conn.execute(
                            "UPDATE memories SET emo_v = ?, emo_a = ?, emo_tag = ? WHERE id = ?",
                            (emo_v, emo_a, emo_tag, int(row[0])))
                    if reactivated or (emo is not None
                                       and float(row[1] if row[1] is not None else -1.0) < 0.0):
                        self._conn.commit()
                    return int(row[0])
                cur = self._conn.execute(
                    "INSERT INTO memories(ts, kind, tag, text, meta, emo_v, emo_a, emo_tag) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (self._now(), kind, tag, t, _json(m), emo_v, emo_a, emo_tag))
                mem_id = int(cur.lastrowid)
                if vec:
                    self._conn.execute("INSERT OR REPLACE INTO vectors(mem_id, vec) VALUES(?,?)",
                                       (mem_id, json.dumps(vec)))
                self._evict()
                self._conn.commit()
                return mem_id
            except sqlite3.IntegrityError:
                # 并发下撞上唯一索引：按重复处理，返回既有 id
                try:
                    self._conn.rollback()
                    row = self._conn.execute(
                        "SELECT id FROM memories WHERE tag = ? AND text = ? ORDER BY id ASC LIMIT 1",
                        (tag, t)).fetchone()
                except Exception:           # noqa: BLE001
                    return None
                return int(row[0]) if row is not None else None
            except Exception as e:          # noqa: BLE001 —— 写失败只打日志，不影响对局
                print("[Memory] 写入失败（已忽略，不影响对局）：%s" % e)
                try:
                    self._conn.rollback()
                except Exception:           # noqa: BLE001
                    pass
                return None

    def recall(self, query: str, k: int = 3, mood=None) -> list:
        """召回最相关的 ``k`` 条记忆，按 score 降序；任何故障返回 ``[]``。

        返回 ``[{"id","ts","kind","tag","text","score","emo_v","emo_a","emo_tag","emo_affinity"}]``：
        有向量时 score 是余弦相似度，关键词命中固定 0.0。两级检索：向量先按余弦排序；**不足 ``k`` 条时**再
        用关键词从尚未命中的行里补齐——混合库里没向量的行（写入时 embedding 失败 / 旧库遗留）
        不会被有向量的行压掉。

        ``mood``（v4.3.0）：给出当前情绪时，排序分 = 语义分 × 0.75 + **二维情绪相似度** × 0.25。
        语义仍是主项，情绪只让「和此刻心情相近的旧事」稍微靠前；不给 mood 时排序与旧版逐字一致。
        被 supersede 的行（``status != 'active'``）不参与召回。
        """
        q = str(query or "").strip()
        k = max(0, int(k or 0))
        if q == "" or k == 0:
            return []
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = [dict(r) for r in self._conn.execute(
                    "SELECT id, ts, kind, tag, text, emo_v, emo_a, emo_tag FROM memories "
                    "WHERE status = 'active' ORDER BY ts DESC, id DESC")]
                vecs = {int(r[0]): r[1] for r in self._conn.execute("SELECT mem_id, vec FROM vectors")}
            except Exception as e:          # noqa: BLE001
                print("[Memory] 读取失败（已忽略）：%s" % e)
                return []
        hits = []
        qv = self._embed(q)                 # 网络调用放锁外

        def rank_of(row, base):
            return emo_sem.blended_rank(base, mood, row.get("emo_v"), row.get("emo_a"))

        if qv:
            try:
                scored = []
                for row in rows:
                    raw = vecs.get(int(row["id"]))
                    if not raw:
                        continue
                    base = cosine(qv, json.loads(raw))
                    rank, aff = rank_of(row, base)
                    scored.append((rank, row, base, aff))
                scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
                hits = [dict(row, score=float(base), emo_affinity=aff)
                        for _rank, row, base, aff in scored[:k]]
            except Exception as e:          # noqa: BLE001 —— 检索绝不把异常抛回编排层
                print("[Memory] 向量检索失败（已忽略）：%s" % e)
                hits = []
        if len(hits) < k:                   # 没向量 / 命中不够 k 条：余量退关键词补齐
            seen = {int(h["id"]) for h in hits}
            terms = _keywords(q)
            extra = []
            for row in rows:                # rows 已按时间倒序：关键词命中给最新的
                if int(row["id"]) in seen:  # 向量已命中的不重复计入
                    continue
                if any(t in str(row["text"]).lower() for t in terms):
                    rank, aff = rank_of(row, 0.0)
                    extra.append((rank, dict(row, score=0.0, emo_affinity=aff)))
            if extra:
                if mood is not None:        # 有当前情绪：情绪相近的关键词命中先上
                    extra.sort(key=lambda x: (-x[0], -x[1]["id"]))
                hits.extend(row for _rank, row in extra)
        if mood is not None and len(hits) > 1:
            # 向量命中与关键词补齐放在一起重排（只在给了 mood 时；旧路径逐字不变）
            def _rank(h):
                r, _a = rank_of(h, h.get("score", 0.0))
                return (-r, -int(h.get("id", 0)))
            hits.sort(key=_rank)
        return hits[:k]

    # —— supersede（v4.3.0）：旧事实失效，但不删除（保留审计与“当时知道什么”） ——

    def supersede(self, ids, by_id=0, reason="") -> int:
        """把指定记忆标记为失效；返回实际改动条数。已失效的行不会重复计数。"""
        sel = [int(i) for i in (ids or [])]
        if not sel:
            return 0
        with self._lock:
            if self._conn is None:
                return 0
            try:
                q = ",".join("?" * len(sel))
                cur = self._conn.execute(
                    "UPDATE memories SET status = 'superseded', superseded_by = ?, "
                    "superseded_ts = ?, superseded_reason = ? "
                    "WHERE id IN (%s) AND status = 'active'" % q,
                    [int(by_id or 0), self._now(), str(reason or "")[:80]] + sel)
                self._conn.commit()
                return int(cur.rowcount or 0)
            except Exception as e:          # noqa: BLE001
                print("[Memory] supersede 失败（已忽略）：%s" % e)
                try:
                    self._conn.rollback()
                except Exception:           # noqa: BLE001
                    pass
                return 0

    def _active_schedule_rows(self):
        """当前有效的 ``tag='schedule'`` 行（含解析后的 meta）；不可用时返回 ``[]``。"""
        rows = []
        try:
            for r in self._conn.execute(
                    "SELECT id, ts, text, meta FROM memories "
                    "WHERE tag = 'schedule' AND status = 'active' ORDER BY id ASC"):
                item = dict(r)
                item["meta"] = _loads_meta(item.get("meta"))
                rows.append(item)
        except Exception as e:              # noqa: BLE001
            print("[Memory] 日程记忆读取失败（已忽略）：%s" % e)
        return rows

    def supersede_schedule_item(self, item) -> int:
        """日程被取消时，把对应的旧记忆标记失效（v4.3.0，F）。

        两条判据，按可靠度从高到低：
        1. ``meta.schedule_id`` 相同（新写入的日程记忆）；
        2. 旧记忆没有 meta 时，退化为「标题 + 开始时刻」都出现在原文里（旧库兼容）。
        只动 ``tag='schedule'`` 且仍 active 的行；课程取消不走这条（上层已挡）。
        """
        it = item if isinstance(item, dict) else {}
        try:
            sid = int(it.get("id", 0) or 0)
        except (TypeError, ValueError):
            sid = 0
        title = str(it.get("title", "") or "").strip()
        clock = str(it.get("start_time", "") or "").strip()
        with self._lock:
            if self._conn is None:
                return 0
            try:
                rows = self._active_schedule_rows()
            except Exception:               # noqa: BLE001
                return 0
        ids = []
        for row in rows:
            meta = row.get("meta") or {}
            if sid > 0 and int(meta.get("schedule_id", 0) or 0) == sid:
                ids.append(int(row["id"]))
                continue
            text = str(row.get("text", ""))
            if title and title in text and (not clock or clock in text):
                ids.append(int(row["id"]))
        return self.supersede(ids, by_id=0, reason="schedule:cancelled")

    def supersede_schedule_conflicts(self, meta, keep_id=0) -> int:
        """同一天、同一标题的新日程写入后，让旧的同名条目失效（改时间 / 重排场景）。

        只在 ``tag='schedule'``、同一天、同标题时生效；不同日期的同名课程不受影响。
        新行本身由 ``keep_id`` 排除。
        """
        m = meta if isinstance(meta, dict) else {}
        title = str(m.get("title", "") or "").strip()
        day = emo_sem.day_key(m.get("start_ts"))
        if title == "" or day == "":
            return 0
        with self._lock:
            if self._conn is None:
                return 0
            rows = self._active_schedule_rows()
        ids = []
        for row in rows:
            if int(row.get("id", 0)) == int(keep_id or 0):
                continue
            old = row.get("meta") or {}
            if str(old.get("title", "") or "").strip() != title:
                continue
            if emo_sem.day_key(old.get("start_ts")) != day:
                continue
            ids.append(int(row["id"]))
        return self.supersede(ids, by_id=int(keep_id or 0), reason="schedule:updated")

    # —— 聊天流水（共享记忆的「最近发生了什么」；与可召回 memories 分开） ——

    def append_chat(self, user_text: str, reply_text: str, mode: str = "", mood=None) -> int | None:
        """追加一轮完整聊天（玩家+ Azure），返回行 id；失败返回 ``None``。

        这是**原文流水**：桌宠 / 围棋共用，用于启动时恢复“最近在聊什么”的连续性，
        同时给 memories 写一条可召回的摘要由 ``MemoryService`` 负责。
        """
        u = _clip(" ".join(str(user_text or "").split()), MAX_CHAT_USER_LEN)
        r = _clip(" ".join(str(reply_text or "").split()), MAX_CHAT_REPLY_LEN)
        if u == "" and r == "":
            return None
        m = str(mode or "")[:16]
        if self._conn is None:
            return None
        with self._lock:
            if self._conn is None:
                return None
            try:
                cur = self._conn.execute(
                    "INSERT INTO chat_history(ts, mode, user_text, reply_text, mood) VALUES(?,?,?,?,?)",
                    (self._now(), m, u, r, _json(mood if isinstance(mood, dict) else {})))
                row_id = int(cur.lastrowid)
                excess = self._count("chat_history") - int(CHAT_MAX_ROWS)
                if excess > 0:
                    rows = self._conn.execute(
                        "SELECT id FROM chat_history ORDER BY ts ASC, id ASC LIMIT ?",
                        (excess,)).fetchall()
                    ids = [int(x[0]) for x in rows]
                    if ids:
                        q = ",".join("?" * len(ids))
                        self._conn.execute("DELETE FROM chat_history WHERE id IN (%s)" % q, ids)
                self._conn.commit()
                return row_id
            except Exception as e:              # noqa: BLE001
                print("[Memory] 聊天流水写入失败（已忽略）：%s" % e)
                try:
                    self._conn.rollback()
                except Exception:               # noqa: BLE001
                    pass
                return None

    def recent_chats(self, limit: int = 50) -> list:
        """最近 ``limit`` 轮聊天（按时间正序返回，方便聊天面板直接渲染）。"""
        n = max(1, min(int(limit or 50), 200))
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = [dict(r) for r in self._conn.execute(
                    "SELECT id, ts, mode, user_text, reply_text, mood FROM chat_history "
                    "ORDER BY ts DESC, id DESC LIMIT ?", (n,))]
            except Exception as e:              # noqa: BLE001
                print("[Memory] 聊天流水读取失败（已忽略）：%s" % e)
                return []
        rows.reverse()
        for r in rows:
            try:
                m = json.loads(str(r.get("mood", "") or "{}"))
                r["mood"] = m if isinstance(m, dict) else {}
            except Exception:                   # noqa: BLE001
                r["mood"] = {}
        return rows

    # —— Episode：把一段连续聊天收束成可召回摘要（参考 MaiBot 的 episode 思路） ——

    def append_episode_turn(self, source: str, user_text: str, reply_text: str,
                            mood=None) -> int | None:
        """把一轮聊天并进当前 episode；满 6 轮 / 超 620 字 / 空档 15 分钟就收束。

        收束时写一条 ``kind=episode`` 的可召回记忆（前缀「最近聊天摘要」），
        以便围棋与桌宠在之后的提示词里都能召回这段相处的上下文。
        """
        src = str(source or "")[:16]
        u = _clip(" ".join(str(user_text or "").split()), MAX_CHAT_USER_LEN)
        r = _clip(" ".join(str(reply_text or "").split()), MAX_CHAT_REPLY_LEN)
        if u == "" and r == "":
            return None
        piece = "玩家：%s；Azure：%s" % (u[:90], r[:110])
        now = self._now()
        closed: list = []
        new_id = None
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT id, ts, turns, summary, mood FROM episodes "
                    "WHERE source = ? AND ended_ts = 0 ORDER BY id DESC LIMIT 1", (src,)).fetchone()
                if row is not None:
                    summary = str(row["summary"] or "")
                    full = (now - float(row["ts"])) > EPISODE_GAP_SEC \
                        or int(row["turns"]) >= EPISODE_MAX_TURNS \
                        or len(summary) + len(piece) + 1 > EPISODE_MAX_CHARS \
                        or _valence_jump(row["mood"], mood)
                    if full:
                        self._conn.execute(
                            "UPDATE episodes SET ended_ts = ?, ts = ? WHERE id = ?",
                            (float(row["ts"]), now, int(row["id"])))
                        closed.append((src, summary))
                        row = None
                if row is None:
                    cur = self._conn.execute(
                        "INSERT INTO episodes(ts, source, started_ts, ended_ts, turns, summary, mood) "
                        "VALUES(?,?,?,0,1,?,?)",
                        (now, src, now, piece, _json(mood if isinstance(mood, dict) else {})))
                    new_id = int(cur.lastrowid)
                else:
                    summary2 = summary + ("\n" if summary else "") + piece
                    self._conn.execute(
                        "UPDATE episodes SET ts = ?, turns = turns + 1, summary = ?, mood = ? WHERE id = ?",
                        (now, summary2, _json(mood if isinstance(mood, dict) else {}), int(row["id"])))
                    new_id = int(row["id"])
                self._conn.commit()
            except Exception as e:          # noqa: BLE001
                print("[Memory] episode 写入失败（已忽略）：%s" % e)
                try:
                    self._conn.rollback()
                except Exception:           # noqa: BLE001
                    pass
                return None
        # 收束后的摘要写成可召回记忆（锁外 write，避免嵌套锁 / 慢 embedding）
        for src2, summary in closed:
            if summary.strip():
                self.write("最近聊天摘要（%s）：%s" % (src2 or "共享", summary),
                           kind="episode", tag="episode:%s" % src2,
                           meta={"source": src2})
        return new_id

    def close_episode(self, source: str = "") -> None:
        """手动收束当前 episode（模式切换 / 退出前）；失败安全。"""
        src = str(source or "")[:16]
        closed: list = []
        with self._lock:
            if self._conn is None:
                return
            try:
                q = "SELECT id, ts, summary FROM episodes WHERE ended_ts = 0"
                args = ()
                if src:
                    q += " AND source = ?"
                    args = (src,)
                rows = self._conn.execute(q + " ORDER BY id DESC LIMIT 8", args).fetchall()
                for row in rows:
                    self._conn.execute(
                        "UPDATE episodes SET ended_ts = ?, ts = ? WHERE id = ?",
                        (float(row["ts"]), self._now(), int(row["id"])))
                    if str(row["summary"] or "").strip():
                        closed.append(str(row["summary"]))
                self._conn.commit()
            except Exception as e:          # noqa: BLE001
                print("[Memory] episode 收束失败（已忽略）：%s" % e)
        for summary in closed:
            self.write("最近聊天摘要（%s）：%s" % (src or "共享", summary),
                       kind="episode", tag="episode:%s" % src, meta={"source": src})

    def recent_episodes(self, limit: int = 3) -> list:
        """最近收束 / 进行中的 episode 摘要（启动 continuity 用）。"""
        n = max(1, min(int(limit or 3), 20))
        with self._lock:
            if self._conn is None:
                return []
            try:
                return [dict(r) for r in self._conn.execute(
                    "SELECT id, ts, source, started_ts, ended_ts, turns, summary, mood "
                    "FROM episodes ORDER BY ts DESC, id DESC LIMIT ?", (n,))]
            except Exception as e:          # noqa: BLE001
                print("[Memory] episode 读取失败（已忽略）：%s" % e)
                return []

    def stats(self) -> dict:
        """库的体量：``{"rows","games","episodes","has_vectors","db"}``（库不可用时全 0）。"""
        rows = games = hasv = episodes = 0
        with self._lock:
            if self._conn is not None:
                try:
                    rows = self._count("memories")
                    games = self._count("games")
                    hasv = self._count("vectors")
                    episodes = self._count("episodes")
                except Exception as e:      # noqa: BLE001
                    print("[Memory] 统计失败（已忽略）：%s" % e)
        return {"rows": rows, "games": games, "episodes": episodes,
                "has_vectors": hasv, "db": self._db_path}

    def forget(self, ids: list | None = None, all: bool = False) -> int:
        """删记忆（含向量），返回删除条数；``ids`` 与 ``all`` 都没给时什么都不做。

        只清记忆，不动 ``games`` 归档——归档不可召回，不参与提示词注入。
        """
        with self._lock:
            if self._conn is None:
                return 0
            try:
                if all:
                    n = self._count("memories")
                    self._conn.execute("DELETE FROM memories")
                    self._conn.execute("DELETE FROM vectors")
                    self._conn.execute("DELETE FROM chat_history")
                    self._conn.execute("DELETE FROM episodes")
                elif ids:
                    sel = [int(i) for i in ids]
                    q = ",".join("?" * len(sel))
                    n = int(self._conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE id IN (%s)" % q, sel).fetchone()[0])
                    self._conn.execute("DELETE FROM memories WHERE id IN (%s)" % q, sel)
                    self._conn.execute("DELETE FROM vectors WHERE mem_id IN (%s)" % q, sel)
                else:
                    return 0
                self._conn.commit()
                return n
            except Exception as e:          # noqa: BLE001
                print("[Memory] 删除失败（已忽略）：%s" % e)
                return 0

    def save_game(self, moves: list, result: str, summary: str, meta: dict | None = None) -> int | None:
        """落一条对局归档（棋谱 + 结果 + 总结），返回归档 id；失败返回 ``None``。

        只写 ``games`` 归档，**不**自动写 ``memories``：要让这盘的总结能被召回，
        由调用方（``memory_service.remember_game``）另外 ``write()`` 一条 ``kind="game"`` 的记忆。
        """
        with self._lock:
            if self._conn is None:
                return None
            try:
                cur = self._conn.execute(
                    "INSERT INTO games(ts, result, summary, moves, meta) VALUES(?,?,?,?,?)",
                    (self._now(), str(result or ""), str(summary or ""),
                     json.dumps(list(moves or []), ensure_ascii=False), _json(meta)))
                self._conn.commit()
                return int(cur.lastrowid)
            except Exception as e:          # noqa: BLE001
                print("[Memory] 对局归档失败（已忽略）：%s" % e)
                return None

    def close(self) -> None:
        """关闭连接；可重复调用（测试 tearDown 与进程退出都会走到）。"""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:               # noqa: BLE001
                pass
