"""JosekiProgress：族计数跨局持久化，以及「任何存储故障只当第 1 次」的降级。"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

from app.joseki_progress import JosekiProgress


class JosekiProgressTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # 库路径本身先落成一个**文件**：下面「不可建库的路径」要拿它当父目录，
        # 父目录是个文件，`os.makedirs` 与 `sqlite3.connect` 才会真的失败。
        self.db = os.path.join(self.dir.name, "joseki_progress.db")
        open(self.db, "w").close()

    def tearDown(self):
        self.dir.cleanup()

    def test_first_time_is_zero_then_one(self):
        p = JosekiProgress(self.db)
        self.assertEqual(p.seen("star_33"), 0)
        self.assertEqual(p.bump("star_33"), 1)
        self.assertEqual(p.seen("star_33"), 1)

    def test_persists_across_instances(self):
        JosekiProgress(self.db).bump("star_33")
        self.assertEqual(JosekiProgress(self.db).seen("star_33"), 1)

    def test_unbump_never_goes_negative(self):
        p = JosekiProgress(self.db)
        p.bump("star_33")
        self.assertEqual(p.unbump("star_33"), 0)
        self.assertEqual(p.unbump("star_33"), 0)

    def test_readonly_dir_degrades_silently(self):
        p = JosekiProgress(os.path.join(self.db, "nope", "x.db"))   # 不可建库的路径
        self.assertEqual(p.seen("star_33"), 0)
        self.assertEqual(p.bump("star_33"), 0)

    # —— 额外边界用例（计划建议的 1~2 条） ——

    def test_unbump_without_row_stays_zero(self):
        """没有记录时 ``unbump`` 返回 0，且**不落 0 值行**（只动 count，不新建行）。"""
        p = JosekiProgress(self.db)
        self.assertEqual(p.unbump("never_seen"), 0)
        self.assertEqual(p.seen("never_seen"), 0)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT count FROM joseki_seen WHERE family = ?", ("never_seen",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_forget_all_counts_clears_and_degrades(self):
        p = JosekiProgress(self.db)
        p.bump("star_33")
        p.bump("star_33")
        p.bump("san_san")
        self.assertEqual(p.forget_all(), 2)                  # 清掉 2 个族（不是 3 次出现）
        self.assertEqual(p.seen("star_33"), 0)
        broken = JosekiProgress(os.path.join(self.db, "nope", "x.db"))
        self.assertEqual(broken.forget_all(), 0)             # 存储故障：0 且不抛

    def test_degradation_prints_once_per_reason_per_instance(self):
        """库一直坏着也**不得刷屏**：同类原因每个实例只打一行，返回值照旧每次降级。"""
        bad = os.path.join(self.db, "nope", "x.db")
        p = JosekiProgress(bad)                              # 第一实例：三种原因各允许一行
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            values = [p.seen("star_33"), p.seen("star_33"), p.seen("star_33")]
            values += [p.bump("star_33"), p.bump("star_33")]
            values.append(p.forget_all())
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(values, [0, 0, 0, 0, 0, 0])         # 每次调用都照降级值返回，不因节流而变
        self.assertEqual(len(lines), 3, "读取 / 写入 / 清空 各只许一行：%r" % lines)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            p.seen("star_33")                                # 同实例再来：静默
            JosekiProgress(bad).seen("star_33")              # 新实例：重新允许一行
        lines2 = [ln for ln in buf2.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines2), 1, "新实例应各有一行额度：%r" % lines2)


if __name__ == "__main__":
    unittest.main()
