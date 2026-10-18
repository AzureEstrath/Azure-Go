"""``ProfileStore``（``profile.db``）：对局记录只增不减、画像按维读写，
以及「库被删 / 只读 / 目录不存在 / 库是垃圾内容 → 一律当无画像、不抛」的降级矩阵。

门卫（spec §7 + 任务书 Q1）：空库 ``last_game_ts()`` → ``None``；``games`` 只增不减；
四种存储故障都只打一行日志、五个方法一个都不抛。
"""

import asyncio
import contextlib
import io
import os
import sqlite3
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/（app.*）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # tests/（helpers）

from app.profile_store import DEFAULT_DB_PATH, ProfileStore


def _rec(ts=1000.0, **kw):
    """一条最小可用对局记录（键名照 spec §6.1）。"""
    rec = {"ts": ts, "move_count": 4, "final_black_wr": 0.52, "final_score": 1.5,
           "result": "黑胜 1.5 目", "moves": [["B", "Q16"], ["W", "D4"]],
           "per_move_ms": [1200, 900], "joseki_families": {"star_33": 1},
           "chat_topics": [["定式", "他说到定式"]], "summary": "这一局下得挺稳。"}
    rec.update(kw)
    return rec


def _rows(db_path):
    """直接开库数行数（用例自己核对「只增不减」，不经过被测门面）。"""
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0])
    finally:
        conn.close()


class BasicTest(unittest.TestCase):
    """正常路径：记录只增不减、JSON 列往返、画像按维读写。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "profile.db")
        self.store = ProfileStore(self.db)

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_db_is_no_profile(self):
        """门卫：空库 ``last_game_ts`` → ``None``，``games`` → ``[]``，``get_profile`` → ``{}``。"""
        self.assertIsNone(self.store.last_game_ts())
        self.assertEqual(self.store.games(), [])
        self.assertEqual(self.store.games(5), [])
        self.assertEqual(self.store.get_profile(), {})

    def test_add_game_returns_id_and_last_ts(self):
        self.assertEqual(self.store.add_game(_rec(ts=111.0)), 1)
        self.assertEqual(self.store.add_game(_rec(ts=222.0)), 2)
        self.assertEqual(self.store.last_game_ts(), 222.0)

    def test_games_are_append_only_and_newest_first(self):
        """``games`` 只增不减：没有任何删改方法，行数只随 ``add_game`` 涨。"""
        for ts in (10.0, 30.0, 20.0):
            self.store.add_game(_rec(ts=ts))
        self.assertEqual(_rows(self.db), 3)
        self.assertEqual([g["ts"] for g in self.store.games()], [30.0, 20.0, 10.0])
        self.assertEqual([g["ts"] for g in self.store.games(2)], [30.0, 20.0])
        # 门面只暴露「读 + 追加」，没有删 / 改记录的入口
        for name in ("delete_game", "remove_game", "update_game", "clear", "forget_all"):
            self.assertFalse(hasattr(self.store, name), "不该有 %s（记录只增不减）" % name)

    def test_record_round_trips_json_columns(self):
        """四个 JSON 列写进去再读出来是 Python 对象；``summary`` 是自由文本、不是 JSON。"""
        self.store.add_game(_rec(ts=5.0, moves=[["B", "Q16"]], per_move_ms=[100],
                                 joseki_families={"star_33": 2},
                                 chat_topics=[["定式", "他说到定式"]], summary="纯文本 {不是 JSON}"))
        got = self.store.games()[0]
        self.assertEqual(got["moves"], [["B", "Q16"]])
        self.assertEqual(got["per_move_ms"], [100])
        self.assertEqual(got["joseki_families"], {"star_33": 2})
        self.assertEqual(got["chat_topics"], [["定式", "他说到定式"]])
        self.assertEqual(got["summary"], "纯文本 {不是 JSON}")
        self.assertEqual(got["move_count"], 4)
        self.assertEqual(got["final_black_wr"], 0.52)

    def test_dirty_record_still_stores_and_never_raises(self):
        """脏值：缺键 / 非数字 / 不可序列化的 JSON 列 —— 照旧落一行、不抛。"""
        bad = {"ts": "不是数字", "move_count": None, "final_black_wr": "abc",
               "moves": object(), "per_move_ms": None, "joseki_families": {1, 2},
               "chat_topics": "not json", "summary": None}
        row_id = self.store.add_game(bad)
        self.assertEqual(row_id, 1)
        got = self.store.games()[0]
        self.assertGreater(got["ts"], 0.0)                  # 非法 ts 按「现在」记
        self.assertEqual(got["final_black_wr"], 0.0)
        self.assertEqual(got["move_count"], 0)
        self.assertEqual(got["moves"], [])                  # 序列化不了退回 []
        self.assertEqual(got["per_move_ms"], [])
        self.assertEqual(got["summary"], "")
        self.assertEqual(got["chat_topics"], "not json")    # 解析不出来的脏值原样留着
        self.assertEqual(self.store.add_game("根本不是字典"), 2)
        self.assertEqual(_rows(self.db), 2)

    def test_move_count_falls_back_to_moves_length(self):
        self.store.add_game({"ts": 1.0, "moves": [["B", "Q16"], ["W", "D4"], ["B", "C3"]]})
        self.assertEqual(self.store.games()[0]["move_count"], 3)

    def test_upsert_axes_round_trip_both_shapes(self):
        """两种形状都收：``{axis: value}`` 与 ``{axis: {value,confidence,n_games}}``。"""
        n = self.store.upsert_axes({
            "open.star": 0.75,
            "style.aggression": {"value": 0.4, "confidence": 0.5, "n_games": 20},
            "blunder.top_type": "随手",                     # 非数值维：原样存字符串
        })
        self.assertEqual(n, 3)
        prof = self.store.get_profile()
        self.assertEqual(set(prof), {"open.star", "style.aggression", "blunder.top_type"})
        self.assertEqual(prof["open.star"]["value"], 0.75)
        self.assertEqual(prof["open.star"]["n_games"], 0)    # 只给值：n_games 记 0
        self.assertEqual(prof["style.aggression"]["n_games"], 20)
        self.assertEqual(prof["style.aggression"]["confidence"], 0.5)
        self.assertEqual(prof["blunder.top_type"]["value"], "随手")
        self.assertGreater(prof["open.star"]["updated_ts"], 0.0)

    def test_upsert_axes_overwrites_same_axis(self):
        """同一维第二次写入＝覆盖（表主键就是 ``axis``），不会长出第二行。"""
        self.store.upsert_axes({"open.star": 0.2})
        self.store.upsert_axes({"open.star": 0.9})
        prof = self.store.get_profile()
        self.assertEqual(len(prof), 1)
        self.assertEqual(prof["open.star"]["value"], 0.9)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0], 1)
        finally:
            conn.close()

    def test_upsert_axes_skips_empty_axis_and_none_value(self):
        self.assertEqual(self.store.upsert_axes({"": 1.0, "open.star": None,
                                                 "open.komoku": 0.5}), 1)
        self.assertEqual(set(self.store.get_profile()), {"open.komoku"})
        self.assertEqual(self.store.upsert_axes({}), 0)
        self.assertEqual(self.store.upsert_axes(None), 0)

    def test_works_under_asyncio_to_thread(self):
        """契约：方法同步阻塞、由调用方 ``await asyncio.to_thread(...)`` 挪到线程上。"""
        async def go():
            row_id = await asyncio.to_thread(self.store.add_game, _rec(ts=7.0))
            ts = await asyncio.to_thread(self.store.last_game_ts)
            await asyncio.to_thread(self.store.upsert_axes, {"open.star": 1.0})
            prof = await asyncio.to_thread(self.store.get_profile)
            return row_id, ts, prof

        row_id, ts, prof = asyncio.run(go())
        self.assertEqual((row_id, ts), (1, 7.0))
        self.assertEqual(prof["open.star"]["value"], 1.0)

    def test_default_path_is_data_profile_db(self):
        """默认库路径＝``backend/data/profile.db``（已 gitignore），且**读路径不建目录**。

        **不再碰默认路径**（修复轮 1 / M-4）：``ProfileStore()`` 的读路径会 ``sqlite3.connect``
        到真实数据目录、顺手留一个空 ``profile.db``——跑一次单测就在仓库里建库，Q3 落真数据后
        这条用例还会读到开发者的真实画像。只断言路径常量，行为验证走临时目录。
        """
        self.assertEqual(DEFAULT_DB_PATH, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "profile.db"))
        # 父目录不存在时，读路径**什么都不建**（写路径才建目录，见 ``_open(create=True)``）
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope", "profile.db")
            self.assertEqual(ProfileStore(missing).get_profile(), {})
            self.assertEqual(ProfileStore(missing).games(3), [])
            self.assertIsNone(ProfileStore(missing).last_game_ts())
            self.assertFalse(os.path.isdir(os.path.join(d, "nope")), "读路径不得建目录")


class DegradeTest(unittest.TestCase):
    """降级矩阵：库被删 / 只读 / 目录不存在 / 库是垃圾内容 —— 一律当「无画像」、不抛。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "profile.db")

    def tearDown(self):
        self.dir.cleanup()

    def _assert_reads_are_empty(self, store):
        self.assertIsNone(store.last_game_ts())
        self.assertEqual(store.games(), [])
        self.assertEqual(store.games(3), [])
        self.assertEqual(store.get_profile(), {})

    def test_missing_dir_degrades_silently(self):
        """目录不存在（父路径是个**文件**）：写 / 读都降级，不抛。"""
        blocked = os.path.join(self.dir.name, "afile")
        open(blocked, "w").close()
        store = ProfileStore(os.path.join(blocked, "nope", "profile.db"))
        self.assertEqual(store.add_game(_rec()), 0)
        self.assertEqual(store.upsert_axes({"open.star": 0.5}), 0)
        self._assert_reads_are_empty(store)

    def test_deleted_db_reads_as_no_profile_then_recreates(self):
        """库被删：先当「无画像」，下一次写入按需重建（旧记录确实没了，不再抛）。"""
        store = ProfileStore(self.db)
        self.assertEqual(store.add_game(_rec(ts=99.0)), 1)
        os.remove(self.db)
        self._assert_reads_are_empty(store)
        self.assertEqual(store.add_game(_rec(ts=123.0)), 1)      # 重建后 id 从 1 起
        self.assertEqual(store.last_game_ts(), 123.0)

    def test_readonly_db_degrades_writes_and_still_reads(self):
        """库只读：**写降级为 0**、不抛；读照旧返回已有内容（读只读库本来就没问题）。"""
        store = ProfileStore(self.db)
        store.add_game(_rec(ts=42.0))
        store.upsert_axes({"open.star": 0.3})
        os.chmod(self.db, stat.S_IREAD)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(store.add_game(_rec(ts=43.0)), 0)
                self.assertEqual(store.upsert_axes({"open.star": 0.9}), 0)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2, "写入对局 / 写入画像 各一行：%r" % lines)
            self.assertTrue(all("[Profile]" in ln for ln in lines))
            self.assertEqual(store.last_game_ts(), 42.0)         # 读得到（没被改）
            self.assertEqual(store.get_profile()["open.star"]["value"], 0.3)
        finally:
            os.chmod(self.db, stat.S_IWRITE)                     # Windows：还原才能删临时目录

    def test_garbage_db_file_degrades_silently(self):
        """库文件是垃圾内容（不是 SQLite）：五个方法全降级、不抛。"""
        with open(self.db, "w") as f:
            f.write("这不是一个 SQLite 文件" * 20)
        store = ProfileStore(self.db)
        self.assertEqual(store.add_game(_rec()), 0)
        self.assertEqual(store.upsert_axes({"open.star": 0.5}), 0)
        self._assert_reads_are_empty(store)

    def test_degradation_prints_once_per_reason_per_instance(self):
        """库一直坏着也**不得刷屏**：同类原因每个实例只打一行，返回值照旧每次降级。"""
        blocked = os.path.join(self.dir.name, "afile")
        open(blocked, "w").close()
        bad = os.path.join(blocked, "nope", "profile.db")
        store = ProfileStore(bad)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            values = [store.add_game(_rec()), store.add_game(_rec()),
                      store.upsert_axes({"open.star": 0.5}), store.upsert_axes({"open.star": 0.5}),
                      store.last_game_ts(), store.games(), store.get_profile()]
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(values, [0, 0, 0, 0, None, [], {}], "每次调用都照降级值返回，不因节流而变")
        self.assertEqual(len(lines), 5,
                         "写对局 / 写画像 / 读时刻 / 读对局 / 读画像 各一行：%r" % lines)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            store.games()                                    # 同实例再来：静默
            ProfileStore(bad).games()                        # 新实例：重新允许一行
        lines2 = [ln for ln in buf2.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines2), 1, "新实例应各有一行额度：%r" % lines2)


if __name__ == "__main__":
    unittest.main()
