"""棋谱库存储层（``games.db``）：一局一行，供棋谱库 / SGF 导出 / 后续全盘复盘读取。

与 ``profile.db`` / ``memory.db`` 同性质：运行产物、已 gitignore、**独立文件**。写入点只有
``summarize()``（手动结束对局）与 ``/api/games/import``；**落子回合内零写入**。

降级策略（fail-open）：所有方法都不许抛异常——库被删 / 只读 / 目录不存在 / 文件是垃圾内容，
一律只 ``print("[Games] …")`` 一行后返回降级值（``add_game`` / ``ensure_demo`` → 0，
``list_games`` → []，``get_game`` → None，``delete_game`` → False），绝不影响对局与库列表。
构造期**不碰磁盘**；只有写路径（add / delete / ensure_demo）才按需建目录与库。
"""

import json
import math
import os
import sqlite3
import threading
import time

# 默认库路径：<backend>/data/games.db
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "games.db")

MAX_MOVES = 1000
SUMMARY_KEEP = 4000

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL DEFAULT 0,
        move_count INTEGER NOT NULL DEFAULT 0,
        final_black_wr REAL,
        final_score REAL,
        result TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        moves TEXT NOT NULL DEFAULT '[]',
        meta TEXT NOT NULL DEFAULT '{}',
        demo INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_games_ts ON games(ts)",
    """CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '')""",
)

# —— 内置短示例棋局（用户要求「默认有一个短已下棋局供分析测试用」）——
# 24 手分投布局：双方各占两边，无提子；后续全盘 AI 复盘可以直接拿它试。
DEMO_MOVES = [
    ["black", "Q16"], ["white", "D4"],
    ["black", "D16"], ["white", "Q4"],
    ["black", "R14"], ["white", "F17"],
    ["black", "C14"], ["white", "R17"],
    ["black", "Q13"], ["white", "F16"],
    ["black", "C11"], ["white", "K17"],
    ["black", "R11"], ["white", "F13"],
    ["black", "Q10"], ["white", "K14"],
    ["black", "C8"], ["white", "F10"],
    ["black", "R8"], ["white", "F7"],
    ["black", "Q7"], ["white", "K8"],
    ["black", "C5"], ["white", "F4"],
]

DEMO_RECORD = {
    "ts": 0,
    "moves": DEMO_MOVES,
    "move_count": len(DEMO_MOVES),
    "final_black_wr": None,
    "final_score": None,
    "result": "",
    "summary": "内置示例棋局：24 手分投布局，双方各占两边。用来体验棋谱库、SGF 导出与全盘复盘。",
    "meta": {
        "demo": True,
        "title": "示例棋局 · 分投布局",
        "black": "玩家",
        "white": "Azure",
        "source": "demo",
        "notes": [
            {"move_no": 4, "kind": "review", "side": "E", "text": "右上分投，先把两边都占住。"},
            {"move_no": 6, "kind": "review", "side": "A", "text": "我挂左上角，和你分头经营。"},
            {"move_no": 12, "kind": "review", "side": "A", "text": "中腹还空着，别急着围。"},
            {"move_no": 24, "kind": "review", "side": "E", "text": "先把外势走厚，后面再看中腹。"},
        ],
        "losses": [],
    },
}


def _json(v, default: str) -> str:
    if v is None:
        return default
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:                   # noqa: BLE001 —— 脏值不值得丢一整条记录
        return default


def _load(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:               # noqa: BLE001
            return default
    return default if v is None else v


def _num(v):
    """可空数值列：None / 脏值 / NaN / inf → None（不是 0——「没量过」与「0.0」必须能分开）。

    NaN / inf 必须挡掉：它们会原样写进 SQLite，再经 `/api/games` 的 JSON 序列化变成
    「Out of range float values are not JSON compliant」→ 整个列表 500。
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _clean_moves(moves) -> list:
    out = []
    if not isinstance(moves, (list, tuple)):
        return out
    for m in moves:
        if isinstance(m, (list, tuple)) and len(m) >= 2:
            color, gtp = str(m[0]).strip().lower(), str(m[1]).strip()
        elif isinstance(m, dict):
            color = str(m.get("color") or m.get("c") or "").strip().lower()
            gtp = str(m.get("gtp") or "").strip()
        else:
            continue
        if color in ("b", "black", "黑"):
            color = "black"
        elif color in ("w", "white", "白"):
            color = "white"
        else:
            continue
        out.append([color, gtp if gtp else "pass"])
        if len(out) >= MAX_MOVES:
            break
    return out


class GameStore:
    """``games.db`` 的门面：构造期不吃磁盘 IO，所有故障都在方法内降级。"""

    def __init__(self, db_path: str = ""):
        self._db_path = str(db_path or "").strip() or DEFAULT_DB_PATH
        self._warned = set()
        # 首访播种的「检查 → 写入」必须原子：前端连点两次「刷新」会并发打两次 /api/games，
        # 两次都看到空库就会各塞一盘示例（列表里出现两条一模一样的示例局）。
        self._demo_lock = threading.Lock()

    # —— 内部 ——

    def _warn(self, key: str, e) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        print("[Games] %s（已降级）：%s" % (key, e))

    def _connect(self):
        d = os.path.dirname(os.path.abspath(self._db_path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for sql in _SCHEMA:
            conn.execute(sql)
        return conn

    # —— 对外接口（全部同步、全部不抛）——

    def add_game(self, rec: dict) -> int:
        """落一条棋谱，返回新行 id；失败返回 0（合法 id 从 1 起）。"""
        rec = rec if isinstance(rec, dict) else {}
        moves = _clean_moves(rec.get("moves"))
        if not moves:
            return 0
        meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        meta = dict(meta)
        if "notes" in rec and "notes" not in meta:
            meta["notes"] = rec.get("notes")
        if "losses" in rec and "losses" not in meta:
            meta["losses"] = rec.get("losses")
        try:
            move_count = int(rec.get("move_count", len(moves)))
        except (TypeError, ValueError):
            move_count = len(moves)
        try:
            ts = float(rec.get("ts") if rec.get("ts") is not None else time.time())
        except (TypeError, ValueError):
            ts = time.time()
        if not math.isfinite(ts):
            ts = time.time()                   # NaN / inf 时间戳同样会毒化列表 JSON
        summary = str(rec.get("summary") or "")[:SUMMARY_KEEP]
        result = str(rec.get("result") or "")[:64]
        demo = 1 if bool(meta.get("demo")) else 0
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO games(ts, move_count, final_black_wr, final_score, result, summary, moves, meta, demo)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts, move_count, _num(rec.get("final_black_wr")), _num(rec.get("final_score")),
                     result, summary, _json(moves, "[]"), _json(meta, "{}"), demo))
                conn.commit()
                return int(cur.lastrowid or 0)
            finally:
                conn.close()
        except Exception as e:          # noqa: BLE001 —— 棋谱库故障不许影响对局
            self._warn("写入失败", e)
            return 0

    def list_games(self, limit: int = 200) -> list:
        """按时间倒序列出元数据（不含 moves，避免一次传整库棋谱）。"""
        try:
            conn = self._connect()
            try:
                try:
                    lim = max(1, min(int(limit), 1000))
                except (TypeError, ValueError):
                    lim = 200
                rows = conn.execute(
                    "SELECT id, ts, move_count, final_black_wr, final_score, result, summary, meta, demo"
                    " FROM games ORDER BY ts DESC, id DESC LIMIT ?", (lim,)).fetchall()
                out = []
                for r in rows:
                    meta = _load(r["meta"], {})
                    if not isinstance(meta, dict):
                        meta = {}
                    out.append({
                        "id": int(r["id"]),
                        "ts": float(_num(r["ts"]) or 0.0),
                        "move_count": int(r["move_count"] or 0),
                        "final_black_wr": _num(r["final_black_wr"]),
                        "final_score": _num(r["final_score"]),
                        "result": str(r["result"] or ""),
                        "summary": str(r["summary"] or "")[:160],
                        "demo": bool(r["demo"]),
                        "title": str(meta.get("title") or ""),
                    })
                return out
            finally:
                conn.close()
        except Exception as e:          # noqa: BLE001
            self._warn("列表读取失败", e)
            return []

    def get_game(self, game_id: int):
        """读一整盘（含 moves / meta / notes / losses）；不存在或故障返回 None。"""
        try:
            gid = int(game_id)
        except (TypeError, ValueError):
            return None
        try:
            conn = self._connect()
            try:
                r = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
            finally:
                conn.close()
            if r is None:
                return None
            meta = _load(r["meta"], {})
            if not isinstance(meta, dict):
                meta = {}
            moves = _load(r["moves"], [])
            if not isinstance(moves, list):
                moves = []
            return {
                "id": int(r["id"]),
                "ts": float(_num(r["ts"]) or 0.0),
                "move_count": int(r["move_count"] or len(moves)),
                "final_black_wr": _num(r["final_black_wr"]),
                "final_score": _num(r["final_score"]),
                "result": str(r["result"] or ""),
                "summary": str(r["summary"] or ""),
                "moves": moves,
                "meta": meta,
                "demo": bool(r["demo"]),
                "notes": meta.get("notes") if isinstance(meta.get("notes"), list) else [],
                "losses": meta.get("losses") if isinstance(meta.get("losses"), list) else [],
            }
        except Exception as e:          # noqa: BLE001
            self._warn("读取失败", e)
            return None

    def delete_game(self, game_id: int) -> bool:
        try:
            gid = int(game_id)
        except (TypeError, ValueError):
            return False
        try:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM games WHERE id = ?", (gid,))
                conn.commit()
                return int(cur.rowcount or 0) > 0
            finally:
                conn.close()
        except Exception as e:          # noqa: BLE001
            self._warn("删除失败", e)
            return False

    def count(self) -> int:
        try:
            conn = self._connect()
            try:
                return int(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] or 0)
            finally:
                conn.close()
        except Exception as e:          # noqa: BLE001
            self._warn("计数失败", e)
            return 0

    def ensure_demo(self) -> int:
        """这个库第一次被打开时塞入内置示例棋局；**同一个库只塞一次**。

        ``meta.demo_seeded`` 记住「已经给过示例」：用户把示例删掉后不会再自动塞回来
        （否则每点一次刷新它就复活，很像删不掉）。库文件整个删掉 = 新库，示例重新出现。
        """
        with self._demo_lock:
            try:
                conn = self._connect()
                try:
                    n = int(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] or 0)
                    row = conn.execute("SELECT value FROM meta WHERE key = 'demo_seeded'").fetchone()
                    seeded = row is not None and str(row[0]) == "1"
                finally:
                    conn.close()
            except Exception as e:              # noqa: BLE001
                self._warn("示例播种检查失败", e)
                return 0
            if n > 0 or seeded:
                return 0
            rec = dict(DEMO_RECORD)
            rec["ts"] = time.time()
            game_id = self.add_game(rec)
            if game_id:
                try:
                    conn = self._connect()
                    try:
                        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('demo_seeded', '1')")
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as e:  # noqa: BLE001 —— 示例已写进去，标记失败最多再试一次
                    self._warn("示例标记失败", e)
            return game_id
