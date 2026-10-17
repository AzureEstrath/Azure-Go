"""定式族计数：跨局记住每个族（``family``）被走到过几次。

用途：卡片门禁要看「这个族是第几次遇到」（前 3 次一律上卡片，第 4 次起才要求真的亏了），
这个次数必须跨局存活，所以落一份 SQLite 文件（性质同 ``memory.db``：运行产物、已 gitignore）。

契约：
- ``seen(family) -> int`` —— 该族**历史累计**出现次数；没记录 / 读不出来都是 ``0``（＝「第 1 次」）；
- ``bump(family) -> int`` —— 加一并返回新值；``unbump(family) -> int`` —— 减一（**不得低于 0**）；
  写失败返回**旧值**（旧值也读不出来就是 ``0``）；
- ``forget_all() -> int`` —— 清空并返回清掉的**条数**（失败返回 ``0``）。

降级策略（fail-open）：**四个方法一个都不许抛异常**——库文件被删 / 被占用、所在目录只读、
首次运行目录不存在，一律只 ``print("[Joseki] …")`` 一行后返回上述降级值，
绝不影响对局（照样上卡片、照样能下完这盘棋）；且**同类原因每个实例只打一行**——
``seen`` 每回合都会被调用，库一直坏着时不能刷屏（口径同 ``memory.py`` 一次性的 ``_embed_warned``）。

线程与连接：所有方法都是**同步阻塞**的纯文件操作，**每次调用自开自关一条连接**、
不跨线程共享连接对象——调用方会（且必须）用 ``await asyncio.to_thread(...)`` 把它挪到
工作线程上，别直接塞进 ``async def``。构造期**不碰磁盘**：只有写路径（``bump`` / ``unbump`` /
``forget_all``）才按需建目录与库；读路径（``seen``）**不建目录**，但父目录存在时 ``sqlite3.connect`` 会顺手建出一个**空库文件**（行为无害：没记录本来就等于 0），库文件在但还没表时顺手补一次建表。
"""

import contextlib
import os
import sqlite3
import time

# 默认库路径：<backend>/data/joseki_progress.db（backend/data/ 已 gitignore，属运行产物）
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "joseki_progress.db")

# 拿不到库锁就认输的等待上限（调用方在 to_thread 里等，不能把一回合拖太久）
LOCK_TIMEOUT_SEC = 3.0

_SCHEMA = """CREATE TABLE IF NOT EXISTS joseki_seen (
    family TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    last_ts REAL NOT NULL
)"""


class JosekiProgress:
    """族计数的持久化门面：构造期不吃磁盘 IO，所有故障都在方法内降级。"""

    def __init__(self, db_path: str = ""):
        """``db_path`` 留空（或 ``None``）走 ``DEFAULT_DB_PATH``；构造期不建目录、不建库。"""
        self._db_path = str(db_path or "").strip() or DEFAULT_DB_PATH
        self._warned = set()                # 已打过日志的降级原因：同类原因本实例只打一行

    # —— 对外接口（全部同步、全部不抛） ——

    def seen(self, family: str) -> int:
        """该族**历史累计**出现次数；没记录 / 读不出来一律 ``0``（＝「第 1 次」）。"""
        fam = str(family or "")
        try:
            with self._open(create=False) as conn:
                row = conn.execute(
                    "SELECT count FROM joseki_seen WHERE family = ?", (fam,)).fetchone()
            return max(0, int(row[0])) if row is not None else 0
        except Exception as e:              # noqa: BLE001 —— 读不出来就当第 1 次，绝不抛
            return self._degrade("读取", e, 0)

    def bump(self, family: str) -> int:
        """加一并返回新值；任何存储故障返回**旧值**（旧值也读不出来时 ``0``），不抛。"""
        return self._change(str(family or ""), 1)

    def unbump(self, family: str) -> int:
        """减一（**不得低于 0**）并返回新值；任何存储故障返回旧值，不抛。

        只动 ``count``：行不存在时返回 ``0`` 且**不新建行**（不落 0 值 / 负值），
        ``last_ts`` 保持「最近一次出现」的语义、不被撤销动作改写。
        """
        return self._change(str(family or ""), -1)

    def forget_all(self) -> int:
        """清空所有族计数，返回清掉的**条数**（族数，不是出现次数）；库不可用时 ``0`` 且不抛。

        条数用「先 ``SELECT COUNT(*)`` 再 ``DELETE``」（同 ``MemoryStore.forget(all=True)`` 的口径），
        不依赖 ``cursor.rowcount``。
        """
        try:
            with self._open(create=True) as conn:
                n = int(conn.execute("SELECT COUNT(*) FROM joseki_seen").fetchone()[0])
                conn.execute("DELETE FROM joseki_seen")
                conn.commit()
            return n
        except Exception as e:              # noqa: BLE001
            return self._degrade("清空", e, 0)

    # —— 内部 ——

    def _change(self, family: str, delta: int) -> int:
        """``bump`` / ``unbump`` 的共同实现；写失败一律返回写之前的旧值。"""
        old = self.seen(family)             # 降级兜底用的旧值（读不出来时它自己就降到 0）
        try:
            with self._open(create=True) as conn:
                if delta > 0:
                    conn.execute(
                        "INSERT INTO joseki_seen(family, count, last_ts) VALUES(?, 1, ?) "
                        "ON CONFLICT(family) DO UPDATE SET count = count + 1, "
                        "last_ts = excluded.last_ts",
                        (family, time.time()))
                else:
                    conn.execute(
                        "UPDATE joseki_seen SET count = MAX(0, count - 1) WHERE family = ?",
                        (family,))
                conn.commit()
                row = conn.execute(
                    "SELECT count FROM joseki_seen WHERE family = ?", (family,)).fetchone()
            return max(0, int(row[0])) if row is not None else max(0, old)
        except Exception as e:              # noqa: BLE001 —— 写不进去只丢这一次，绝不抛
            return self._degrade("写入", e, max(0, old))

    @contextlib.contextmanager
    def _open(self, *, create: bool):
        """自开自关一条连接；``create=True`` 时按需建目录（**只有写路径**才建）。

        - 目录建不出来 / 库开不出来 / 库不可写：异常原样上抛，由调用方降级；
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
            conn.execute(_SCHEMA)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def _degrade(self, action: str, err: Exception, fallback: int) -> int:
        """降级收口：一行日志 + 返回兜底值（调用方传「旧值」或 ``0``）。

        日志走 ``print``（与 ``memory.py`` / ``memory_service.py`` 的降级口径一致），
        且**同类原因每个实例只打一行**（口径同 ``memory.py`` 的 ``_embed_warned``）：
        ``seen`` 一回合至少被叫一次，库一直坏着时不能刷屏。节流只影响打不打日志，
        **不影响行为**——每次调用照样返回降级值、照样不抛。
        """
        if action not in self._warned:
            self._warned.add(action)
            print("[Joseki] 族计数%s失败（按 %d 降级，不影响对局）：%s -> %s"
                  % (action, fallback, self._db_path, err))
        return fallback
