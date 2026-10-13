"""KataGoEngine 移植校验：不依赖真实引擎，只验证失败路径与接口形状。"""

import asyncio
import unittest

from app.engine import KataGoEngine


class EngineFailureTest(unittest.TestCase):
    def test_missing_files_marks_dead(self):
        async def run():
            eng = KataGoEngine(exe="Z:/nope/katago.exe", model="Z:/nope/m.bin", cfg="Z:/nope/c.cfg")
            ok = await eng.start()
            return ok, eng.is_dead(), eng.last_error()

        ok, dead, err = asyncio.run(run())
        self.assertFalse(ok)
        self.assertTrue(dead)
        self.assertIn("找不到 KataGo", err)

    def test_analyze_on_dead_returns_error(self):
        async def run():
            eng = KataGoEngine(exe="Z:/nope/katago.exe", model="Z:/nope/m.bin", cfg="Z:/nope/c.cfg")
            await eng.start()
            return await eng.analyze([])

        r = asyncio.run(run())
        self.assertIn("error", r)

    def test_analyze_before_start_returns_error(self):
        async def run():
            eng = KataGoEngine(exe="Z:/nope/katago.exe", model="Z:/nope/m.bin", cfg="Z:/nope/c.cfg")
            return await eng.analyze([])

        r = asyncio.run(run())
        self.assertIn("error", r)

    def test_stderr_tail_format(self):
        eng = KataGoEngine()
        eng._stderr_tail = ["a", "b", "c", "d"]
        self.assertEqual(eng.stderr_tail(2), "c | d")
        self.assertEqual(eng.stderr_tail(0), "a | b | c | d")


if __name__ == "__main__":
    unittest.main()
