"""KataGo 常驻进程封装：不依赖真实引擎，只验证失败路径与请求形状。"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from katago_service import proc as proc_module
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


# 假引擎：与真实 kataGo 命令行同构（exe=python 时 args[1] 就是脚本名 "analysis"），
# 读一行 JSON 请求回一行 JSON；启动时先灌 ~430KB stderr（远超管道缓冲），用来验证持续排空。
# 不依赖真实 KataGo，也不联网。
FAKE_ENGINE = '''
import json, sys, time
out = sys.stdout.buffer
err = sys.stderr.buffer
for i in range(2000):
    err.write(("fake stderr %d " % i + "x" * 200 + "\\n").encode("utf-8"))
err.flush()
for line in sys.stdin.buffer:
    t = line.decode("utf-8", "replace").strip()
    if not t:
        continue
    req = json.loads(t)
    if req.get("slow"):
        time.sleep(0.4)
    if req.get("engineError"):
        out.write((json.dumps({"id": req.get("id"), "error": "illegal move", "field": "moves"}) + "\\n").encode("utf-8"))
        out.flush()
        continue
    resp = {"id": req.get("id"), "echo": req, "rootInfo": {"winrate": 0.5}}
    if req.get("big"):
        resp["ownership"] = [0.001] * 400000        # 单行 ~2.4MB：远超 asyncio 默认 64KiB 读行上限
    out.write((json.dumps(resp) + "\\n").encode("utf-8"))
    out.flush()
'''


class FakeEngineTest(unittest.TestCase):
    """假引擎跑通正向路径：预热、透传、坑①②、超时与关闭后的状态。"""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="fake_katago_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "analysis"), "w", encoding="utf-8") as f:
            f.write(FAKE_ENGINE)
        self.model = os.path.join(tmp, "m.bin")
        self.cfg = os.path.join(tmp, "c.cfg")
        for path in (self.model, self.cfg):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        self.log_dir = os.path.join(tmp, "logs")
        cwd = os.getcwd()
        os.chdir(tmp)                  # exe=python 时脚本名按相对路径解析，故切到假引擎所在目录
        self.addCleanup(os.chdir, cwd)

    def _with_proc(self, scenario):
        """起假引擎 → 跑 scenario(proc) → 收尾 shutdown；全程限时，避免回归时把测试挂住。"""
        async def run():
            p = KatagoProcess(exe=sys.executable, model=self.model, cfg=self.cfg, log_dir=self.log_dir)
            self.assertTrue(await p.start(), "假引擎未启动：" + p.last_error())
            try:
                await asyncio.wait_for(scenario(p), timeout=20)
            finally:
                await asyncio.wait_for(p.shutdown(), timeout=20)

        asyncio.run(run())

    @staticmethod
    async def _wait_for(cond, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    def test_warming_then_ready(self):
        async def scenario(p):
            self.assertEqual(p.state_name(), "warming")        # 首次成功分析之前
            await p.warmup()                                   # 空盘低 visits，结果吞掉
            self.assertEqual(p.state_name(), "ready")

        self._with_proc(scenario)

    def test_error_reply_keeps_warming_and_does_not_kill(self):
        async def scenario(p):
            r = await p.analyze({"moves": [], "engineError": True})
            self.assertEqual(r.get("error"), "illegal move")   # 引擎的合法应答原样回传
            self.assertFalse(p.is_dead())
            self.assertEqual(p.state_name(), "warming")        # 只有成功应答才算就绪
            self.assertIn("rootInfo", await p.analyze({"moves": []}))
            self.assertEqual(p.state_name(), "ready")          # 之后一次成功分析把它翻过来

        self._with_proc(scenario)

    def test_body_forwarded_verbatim_and_caller_dict_untouched(self):
        req = {"moves": [["B", "Q16"]], "rules": "chinese", "komi": 7.5,
               "boardXSize": 19, "boardYSize": 19, "maxVisits": 123, "includeOwnership": True}
        expected = dict(req)

        async def scenario(p):
            echo = (await p.analyze(req))["echo"]
            self.assertEqual(req, expected)                    # 调用方 dict 未被改写
            self.assertNotIn("id", req)
            self.assertEqual({k: v for k, v in echo.items() if k != "id"}, expected)
            self.assertEqual(echo["id"], "r1")                 # 只补一个 id

        self._with_proc(scenario)

    def test_big_single_line_reply_parses(self):
        """坑②：带 ownership 的超大单行必须读得下来（上限没传进子进程就会被截断）。"""
        async def scenario(p):
            r = await p.analyze({"moves": [], "big": True})
            self.assertEqual(len(r["ownership"]), 400000)

        self._with_proc(scenario)

    def test_stderr_is_drained_continuously(self):
        """坑①：假引擎先灌 ~430KB stderr，排空逻辑坏了它就会卡在读 stdin 之前。"""
        async def scenario(p):
            self.assertIn("rootInfo", await p.analyze({"moves": []}))
            drained = await self._wait_for(lambda: p.stderr_tail(0).count("fake stderr") >= 20)
            self.assertTrue(drained, "stderr 未被持续排空：" + p.stderr_tail(3))

        self._with_proc(scenario)

    def test_unserialisable_body_does_not_kill_engine(self):
        async def scenario(p):
            r = await p.analyze({"moves": {1, 2}})              # set 不可 JSON 序列化
            self.assertIn("无法序列化", r["error"])
            self.assertFalse(p.is_dead())                       # 不得连累共享引擎
            self.assertEqual(p.last_error(), "")
            self.assertIn("rootInfo", await p.analyze({"moves": []}))   # 引擎仍可用

        self._with_proc(scenario)

    def test_timeout_returns_error_without_killing(self):
        async def scenario(p):
            # 先成功一次：首个请求走 180s 启动档，之后才切到常规档（这里压到 100ms）
            self.assertIn("rootInfo", await p.analyze({"moves": []}))
            with mock.patch.object(proc_module, "REQ_TIMEOUT_MS", 100):
                r = await p.analyze({"moves": [], "slow": True})
            self.assertIn("超时", r["error"])
            self.assertFalse(p.is_dead())
            self.assertIn("rootInfo", await p.analyze({"moves": []}))   # 超时后引擎仍可用

        self._with_proc(scenario)

    def test_shutdown_ends_dead(self):
        async def scenario(p):
            await p.warmup()
            await p.shutdown()
            self.assertTrue(p.is_dead())
            self.assertEqual(p.state_name(), "dead")
            self.assertIn("error", await p.analyze({"moves": []}))

        self._with_proc(scenario)


if __name__ == "__main__":
    unittest.main()
