"""KataGo 常驻进程封装：不依赖真实引擎，只验证失败路径与请求形状。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from katago_service.proc import KatagoProcess, STREAM_LIMIT


class ProcFailureTest(unittest.TestCase):
    def test_missing_files_marks_dead(self):
        async def run():
            p = KatagoProcess(exe="Z:/nope/katago.exe", model="Z:/nope/m.bin", cfg="Z:/nope/c.cfg")
            ok = await p.start()
            return ok, p.is_dead(), p.last_error()

        ok, dead, err = asyncio.run(run())
        self.assertFalse(ok)
        self.assertTrue(dead)
        self.assertIn("找不到 KataGo", err)

    def test_analyze_before_start_returns_error(self):
        async def run():
            p = KatagoProcess(exe="Z:/nope/katago.exe", model="Z:/nope/m.bin", cfg="Z:/nope/c.cfg")
            return await p.analyze({"moves": []})

        r = asyncio.run(run())
        self.assertIn("error", r)

    def test_stderr_tail_format(self):
        p = KatagoProcess(exe="x", model="y", cfg="z")
        p._stderr_tail = ["a", "b", "c", "d"]
        self.assertEqual(p.stderr_tail(2), "c | d")
        self.assertEqual(p.stderr_tail(0), "a | b | c | d")

    def test_stream_limit_is_large(self):
        self.assertGreaterEqual(STREAM_LIMIT, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
