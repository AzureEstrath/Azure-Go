"""玩家棋风画像的存储层（``profile.db``）：**对局记录** + **每维画像** + **人格化状态** 三张表。

与 ``memory.db`` / ``joseki_progress.db`` 同性质：运行产物、已 gitignore、**独立文件**
（设计 §五 I：``profile.db`` 与 ``memory.db`` 互不依赖，memory 关着画像照常工作）。

契约（设计 §6.1 的表结构逐字照抄）：
- ``games``  —— **一局一行、只增不减**（本类没有任何删改记录的方法）；写入点在
  ``summarize()``（手动结束）里，**回合内零写入**；
- ``profile`` —— 每维一行：``value``（数值维是 ``[0,1]``；``blunder.top_type`` /
  ``persona.address_pref`` 这两个**非数值维**存自然字符串，SQLite 的 NUMERIC 亲和度
  存不了数字的文本会原样留成 TEXT）+ ``confidence`` + ``n_games`` + ``updated_ts``。
- ``persona`` —— 键值对：跨局关系 / 情绪余温 / 互动次数（人格化，与画像开关解耦）。
  值按 JSON 文本存，读回时还原类型；坏值原样返回文本，绝不抛。

降级策略（fail-open）：**所有方法一个都不许抛异常**——库被删 / 只读 / 目录不存在 /
库文件是垃圾内容，一律只 ``print("[Profile] …")`` 一行后返回降级值
（``add_game`` / ``upsert_axes`` / ``set_persona`` → ``0``、``last_game_ts`` → ``None``、
``games`` / ``recent_game_ts`` / ``get_profile`` / ``get_persona`` → ``[]`` / ``[]`` / ``{}`` / ``{}``），绝不影响对局；且
**同类原因每个实例只打一行**（口径同 ``joseki_progress.py`` / ``memory.py`` 的 ``_embed_warned``）。

线程与连接：所有方法都是**同步阻塞**的纯文件操作，**每次调用自开自关一条连接**、
不跨线程共享连接对象——调用方会（且必须）用 ``await asyncio.to_thread(...)`` 把它挪到
工作线程上（设计 §四「异步纪律」），别直接塞进 ``async def``。构造期**不碰磁盘**：
只有写路径（``add_game`` / ``upsert_axes``）才按需建目录与库；读路径**不建目录**，
但父目录存在时 ``sqlite3.connect`` 会顺手建出一个**空库文件**（行为无害：空库本来就
等于「无画像」），库文件在但还没表时顺手补一次建表。
"""

import contextlib
import json
import os
import sqlite3
import time

# 默认库路径：<backend>/data/profile.db（backend/data/ 已 gitignore，属运行产物）
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "profile.db")

# 拿不到库锁就认输的等待上限（调用方在 to_thread 里等，不能把一回合拖太久）
LOCK_TIMEOUT_SEC = 3.0

# ``games`` 表里存 JSON 文本的四列（spec §6.1 的 ``-- JSON`` 注释）；
# ``summary`` 是**自由文本**、不在此列。
JSON_COLUMNS = ("moves", "per_move_ms", "joseki_families", "chat_topics")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY, ts REAL,
        move_count INTEGER, final_black_wr REAL, final_score REAL,
        result TEXT,
        moves TEXT, per_move_ms TEXT,
        joseki_families TEXT, chat_topics TEXT,
        summary TEXT)""",
    """CREATE TABLE IF NOT EXISTS profile(
        axis TEXT PRIMARY KEY,
        value REAL, confidence REAL,
        n_games INTEGER, updated_ts REAL)""",
    """CREATE TABLE IF NOT EXISTS persona(
        key TEXT PRIMARY KEY,
        value TEXT, updated_ts REAL)""",
)


def _json_text(v, default: str) -> str:
    """把 JSON 列的值变成文本：已经是文本就原样留着，否则 ``json.dumps``；脏值退回 ``default``。

    ``None`` 视为**缺**（不是字面 ``null``）——退回 ``default``（``[]`` / ``{}``）。
    """
    if v is None:
        return default
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:                   # noqa: BLE001 —— 脏值不值得丢一整条记录
        return default


def _parse_json(v, default):
    """把 JSON 列读回 Python；**解析不出来就原样返回文本**（脏数据不丢、也不抛）。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:               # noqa: BLE001
            return v
    return default if v is None else v


def _as_float(v, default: float = 0.0) -> float:
    """数值列的容错转换：``None`` / 非数字退回 ``default``。"""
    if isinstance(v, bool) or v is None:
        return float(default)
    try:
        return float(v)
    except Exception:                   # noqa: BLE001
        return float(default)


def _as_int(v, default: int = 0) -> int:
    """整数列的容错转换：``None`` / 非数字退回 ``default``。"""
    if isinstance(v, bool) or v is None:
        return int(default)
    try:
        return int(v)
    except Exception:                   # noqa: BLE001
        return int(default)


class ProfileStore:
    """``profile.db`` 的门面：构造期不吃磁盘 IO，所有故障都在方法内降级。"""

    def __init__(self, db_path: str = ""):
        """``db_path`` 留空（或 ``None``）走 ``DEFAULT_DB_PATH``；构造期不建目录、不建库。"""
        self._db_path = str(db_path or "").strip() or DEFAULT_DB_PATH
        self._warned = set()                # 已打过日志的降级原因：同类原因本实例只打一行

    # —— 对外接口（全部同步、全部不抛） ——

    def add_game(self, rec: dict) -> int:
        """落一条对局记录，返回新行的 ``id``；**任何存储故障返回 ``0``**（合法 id 从 1 起）。

        ``rec`` 的键以 spec §6.1 的列为准（``ts / move_count / final_black_wr /
        final_score / result / moves / per_move_ms / joseki_families / chat_topics /
        summary``）；**多出来的键一律忽略**（表形状固定），缺键按 0 / 空串补
        （``ts`` 缺或非法 → 当前墙钟；``move_count`` 缺或非法 → ``len(moves)``）。
        ``moves / per_move_ms / joseki_families / chat_topics`` 四列是 JSON：给的不是文本
        就替调用方 ``json.dumps``，序列化不了退回 ``[]`` / ``{}``。

        **只增不减**：本类不提供任何删改记录的方法。
        """
        r = rec if isinstance(rec, dict) else {}
        moves = r.get("moves", [])
        move_count = _as_int(r.get("move_count"), -1)
        if move_count < 0:
            move_count = len(moves) if isinstance(moves, (list, tuple)) else 0
        ts = _as_float(r.get("ts"), -1.0)
        if ts <= 0.0:                       # 缺 / 非法 / 非正：按「现在」记
            ts = time.time()
        try:
            with self._open(create=True) as conn:
                cur = conn.execute(
                    "INSERT INTO games(ts, move_count, final_black_wr, final_score, result,"
                    " moves, per_move_ms, joseki_families, chat_topics, summary)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ts, move_count,
                     _as_float(r.get("final_black_wr")), _as_float(r.get("final_score")),
                     str(r.get("result") or ""),
                     _json_text(moves, "[]"),
                     _json_text(r.get("per_move_ms", []), "[]"),
                     _json_text(r.get("joseki_families", []), "[]"),
                     _json_text(r.get("chat_topics", []), "[]"),
                     str(r.get("summary") or "")))
                conn.commit()
                return int(cur.lastrowid or 0)
        except Exception as e:              # noqa: BLE001 —— 写不进去只丢这一条，绝不抛
            return self._degrade("写入对局", e, 0)

    def last_game_ts(self) -> float | None:
        """最近一局记录的结束时刻（墙钟）；**空库 / 读不出来一律 ``None``**（＝「没有上次」）。"""
        try:
            with self._open(create=False) as conn:
                row = conn.execute("SELECT MAX(ts) FROM games").fetchone()
            if row is None or row[0] is None:
                return None
            return float(row[0])
        except Exception as e:              # noqa: BLE001
            return self._degrade("读取时刻", e, None)

    def games(self, n: int = 0) -> list:
        """最近 ``n`` 局记录，**最新在前**（``ts`` 倒序、同刻按 id 倒序）；``n <= 0`` 取全部。

        返回 ``[dict]``，一行一条、键＝表列；四个 JSON 列**解析成 Python 对象**
        （解析不出来的脏值**原样返回文本**，不丢、不抛）。读不出来一律 ``[]``。
        """
        limit = _as_int(n, 0)
        try:
            with self._open(create=False) as conn:
                sql = ("SELECT id, ts, move_count, final_black_wr, final_score, result,"
                       " moves, per_move_ms, joseki_families, chat_topics, summary"
                       " FROM games ORDER BY ts DESC, id DESC")
                if limit > 0:
                    sql += " LIMIT %d" % limit
                rows = conn.execute(sql).fetchall()
        except Exception as e:              # noqa: BLE001
            return self._degrade("读取对局", e, [])
        out = []
        for row in rows:
            rec = {"id": _as_int(row[0]), "ts": _as_float(row[1]),
                   "move_count": _as_int(row[2]), "final_black_wr": _as_float(row[3]),
                   "final_score": _as_float(row[4]), "result": str(row[5] or ""),
                   "summary": str(row[10] or "")}
            for i, col in enumerate(JSON_COLUMNS, start=6):
                rec[col] = _parse_json(row[i], [])
            out.append(rec)
        return out

    def recent_game_ts(self, n: int = 0) -> list:
        """最近 ``n`` 局的**结束时刻**（``float``，最新在前）；``n <= 0`` 取全部；读不出来一律 ``[]``。

        时间感知（v3.9.0）的「连续下棋天数」只需要**日期集合**，不需要整行记录——单独开这个方法
        是为了不让每次落子都去读 ``games`` 的**全量**行（``moves`` / ``per_move_ms`` 两列是长 JSON，
        解析它们纯属浪费）。查询只取 ``ts`` 一列、按 ``LIMIT`` 收口，开销是「几十行的一列读」。
        """
        limit = _as_int(n, 0)
        try:
            with self._open(create=False) as conn:
                sql = "SELECT ts FROM games ORDER BY ts DESC, id DESC"
                if limit > 0:
                    sql += " LIMIT %d" % limit
                rows = conn.execute(sql).fetchall()
        except Exception as e:              # noqa: BLE001
            return self._degrade("读取时刻", e, [])
        return [_as_float(row[0]) for row in rows]

    def get_profile(self) -> dict:
        """整份画像：``{axis: {"value","confidence","n_games","updated_ts"}}``；读不出来一律 ``{}``。

        ``value`` 数值维是 ``float``、``blunder.top_type`` / ``persona.address_pref``
        两个非数值维是字符串。空库 / 库不可用都返回 ``{}``（＝「无画像」，调用方据此
        走「逐字符不变」的空串路径）。
        """
        try:
            with self._open(create=False) as conn:
                rows = conn.execute(
                    "SELECT axis, value, confidence, n_games, updated_ts FROM profile").fetchall()
        except Exception as e:              # noqa: BLE001
            return self._degrade("读取画像", e, {})
        out = {}
        for row in rows:
            axis = str(row[0] or "")
            if axis == "":
                continue
            out[axis] = {"value": row[1], "confidence": _as_float(row[2]),
                         "n_games": _as_int(row[3]), "updated_ts": _as_float(row[4])}
        return out

    def upsert_axes(self, axes: dict) -> int:
        """按维写入 / 覆盖画像，返回**写入的维数**；任何存储故障返回 ``0``。

        ``axes`` 收两种形状：``{axis: value}``（只给值）或
        ``{axis: {"value":…, "confidence":…, "n_games":…}}``（``profile.merge`` 的返回形状）；
        只给值时 ``confidence`` / ``n_games`` 记 0（由调用方决定要不要先算）。空轴名 /
        ``value`` 为 ``None`` 的条目**跳过**（不计入返回值），不影响其余维。
        """
        items = axes if isinstance(axes, dict) else {}
        rows = []
        for axis, val in items.items():
            name = str(axis or "")
            if name == "":
                continue
            if isinstance(val, dict):
                value = val.get("value")
                if value is None:
                    continue
                conf = _as_float(val.get("confidence"))
                ngames = _as_int(val.get("n_games"))
            else:
                if val is None:
                    continue
                value, conf, ngames = val, 0.0, 0
            rows.append((name, value, conf, ngames))
        if not rows:
            return 0
        now = time.time()
        try:
            with self._open(create=True) as conn:
                for name, value, conf, ngames in rows:
                    conn.execute(
                        "INSERT INTO profile(axis, value, confidence, n_games, updated_ts)"
                        " VALUES(?,?,?,?,?) ON CONFLICT(axis) DO UPDATE SET"
                        " value = excluded.value, confidence = excluded.confidence,"
                        " n_games = excluded.n_games, updated_ts = excluded.updated_ts",
                        (name, value, conf, ngames, now))
                conn.commit()
            return len(rows)
        except Exception as e:              # noqa: BLE001 —— 写不进去只丢这一次，绝不抛
            return self._degrade("写入画像", e, 0)

    def get_persona(self) -> dict:
        """人格化状态（键值对）：读不出来一律 ``{}``（＝没有历史，按初识处理）。

        值按 JSON 还原（数字 / 布尔 / 对象都能回原类型）；存进去时不是合法 JSON 的脏值
        原样返回文本，不丢、不抛。
        """
        try:
            with self._open(create=False) as conn:
                rows = conn.execute("SELECT key, value FROM persona").fetchall()
        except Exception as e:              # noqa: BLE001
            return self._degrade("读取人格化状态", e, {})
        out = {}
        for row in rows:
            key = str(row[0] or "")
            if key == "":
                continue
            out[key] = _parse_json(row[1], None)
        return out

    def set_persona(self, items: dict) -> int:
        """按 key 写入 / 覆盖人格化状态，返回**写入的键数**；任何存储故障返回 ``0``。

        ``None`` / 空 key 的条目跳过；值统一 ``json.dumps`` 成文本（读时还原）。写入点只在
        ``summarize()`` 成功跑完之后（与画像同一条纪律，回合内零写入）。
        """
        data = items if isinstance(items, dict) else {}
        rows = []
        for key, value in data.items():
            name = str(key or "")
            if name == "" or value is None:
                continue
            try:
                text = json.dumps(value, ensure_ascii=False)
            except Exception:               # noqa: BLE001 —— 序列化不了的单个值跳过
                continue
            rows.append((name, text))
        if not rows:
            return 0
        now = time.time()
        try:
            with self._open(create=True) as conn:
                for name, text in rows:
                    conn.execute(
                        "INSERT INTO persona(key, value, updated_ts) VALUES(?,?,?)"
                        " ON CONFLICT(key) DO UPDATE SET"
                        " value = excluded.value, updated_ts = excluded.updated_ts",
                        (name, text, now))
                conn.commit()
            return len(rows)
        except Exception as e:              # noqa: BLE001 —— 写不进去只丢这一次，绝不抛
            return self._degrade("写入人格化状态", e, 0)

    # —— 内部 ——

    @contextlib.contextmanager
    def _open(self, *, create: bool):
        """自开自关一条连接；``create=True`` 时按需建目录（**只有写路径**才建）。

        - 目录建不出来 / 库开不出来 / 库不可写 / 库文件不是 SQLite：异常原样上抛，由调用方降级；
        - 每次调用都是**新连接**、退出即 ``close()``——``sqlite3.connect`` 的 ``with`` 块只提交
          事务、**不关连接**，而调用方可能把不同的调用丢到不同线程上，连接不跨线程共享；
        - 建表（``CREATE TABLE IF NOT EXISTS``）幂等，读路径也顺手做一次：库文件在但还没表
          （0 字节文件 / 别的什么东西留下的空文件）算「空库」，不算故障。
        """
        if create:
            d = os.path.dirname(os.path.abspath(self._db_path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=LOCK_TIMEOUT_SEC)
        try:
            for ddl in _SCHEMA:
                conn.execute(ddl)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def _degrade(self, action: str, err: Exception, fallback):
        """降级收口：一行日志 + 返回兜底值（``0`` / ``None`` / ``[]`` / ``{}``）。

        日志走 ``print``（与 ``memory.py`` / ``joseki_progress.py`` 的降级口径一致），
        且**同类原因每个实例只打一行**：库一直坏着时不能刷屏。节流只影响打不打日志，
        **不影响行为**——每次调用照样返回降级值、照样不抛。
        """
        if action not in self._warned:
            self._warned.add(action)
            print("[Profile] %s失败（按「无画像」降级，不影响对局）：%s -> %s"
                  % (action, self._db_path, err))
        return fallback
