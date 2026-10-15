"""KataGoEngine（HTTP 客户端）校验：不触网，用 httpx.MockTransport。"""

import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import KataGoEngine

ANALYSIS = {"rootInfo": {"winrate": 0.62, "scoreLead": 3.5}, "moveInfos": [], "ownership": [0.0] * 361}


def _engine(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KataGoEngine(url="http://kg.test", client=client, ready_poll_sec=0.0, **kw)


def _raise_connect(req):
    raise httpx.ConnectError("boom", request=req)


def _raise_read_timeout(req):
    raise httpx.ReadTimeout("slow", request=req)


class StartTest(unittest.TestCase):
    def test_start_ok_when_ready(self):
        async def run():
            eng = _engine(lambda req: httpx.Response(200, json={"ok": True, "state": "ready"}))
            ok = await eng.start()
            return ok, eng.is_dead(), eng.last_error()

        ok, dead, err = asyncio.run(run())
        self.assertTrue(ok)
        self.assertFalse(dead)
        self.assertEqual(err, "")

    def test_start_waits_through_warming(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            state = "warming" if calls["n"] == 1 else "ready"
            return httpx.Response(200, json={"ok": True, "state": state})

        async def run():
            eng = _engine(handler)
            return await eng.start(), calls["n"]

        ok, n = asyncio.run(run())
        self.assertTrue(ok)
        self.assertEqual(n, 2)

    def test_start_false_when_unreachable(self):
        def handler(req):
            raise httpx.ConnectError("boom", request=req)

        async def run():
            eng = _engine(handler)
            ok = await eng.start()
            return ok, eng.is_dead(), eng.last_error()

        ok, dead, err = asyncio.run(run())
        self.assertFalse(ok)
        self.assertTrue(dead)
        self.assertIn("连不上", err)

    def test_start_false_when_service_dead(self):
        async def run():
            eng = _engine(lambda req: httpx.Response(200, json={"ok": False, "state": "dead",
                                                               "katago_error": "引擎崩了"}))
            ok = await eng.start()
            return ok, eng.last_error()

        ok, err = asyncio.run(run())
        self.assertFalse(ok)
        self.assertIn("引擎崩了", err)


class AnalyzeTest(unittest.TestCase):
    def test_body_matches_engine_protocol(self):
        seen = {}

        def handler(req):
            seen["body"] = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=ANALYSIS)

        async def run():
            eng = _engine(handler, max_visits=123)
            await eng.start()
            return await eng.analyze([["B", "Q16"]])

        r = asyncio.run(run())
        self.assertEqual(r["rootInfo"]["winrate"], 0.62)
        b = seen["body"]
        self.assertEqual(b["moves"], [["B", "Q16"]])
        self.assertEqual(b["rules"], "chinese")
        self.assertEqual(b["komi"], 7.5)
        self.assertEqual(b["boardXSize"], 19)
        self.assertEqual(b["boardYSize"], 19)
        self.assertEqual(b["maxVisits"], 123)
        self.assertTrue(b["includeOwnership"])
        self.assertIn("id", b)

    def test_service_503_maps_to_error(self):
        def handler(req):
            if req.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "state": "ready"})
            return httpx.Response(503, json={"error": "正在加载"})

        async def run():
            eng = _engine(handler)
            await eng.start()
            return await eng.analyze([])

        self.assertIn("正在加载", asyncio.run(run())["error"])

    def test_timeout_maps_to_error(self):
        def handler(req):
            if req.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "state": "ready"})
            raise httpx.ReadTimeout("slow", request=req)

        async def run():
            eng = _engine(handler, timeout_ms=50)
            await eng.start()
            return await eng.analyze([])

        r = asyncio.run(run())
        self.assertIn("error", r)
        self.assertIn("超时", r["error"])

    def test_analyze_on_dead_returns_error(self):
        async def run():
            eng = _engine(lambda req: httpx.Response(500))
            return await eng.analyze([])

        self.assertIn("error", asyncio.run(run()))

    def test_header_carries_token(self):
        seen = {}

        def handler(req):
            seen["auth"] = req.headers.get("authorization", "")
            return httpx.Response(200, json=ANALYSIS)

        async def run():
            eng = _engine(handler, token="s3cret")
            await eng.start()
            await eng.analyze([])

        asyncio.run(run())
        self.assertEqual(seen["auth"], "Bearer s3cret")


class RetryAndDeadTest(unittest.TestCase):
    """Review Focus #5：重试次数必须钉死，且 ``_dead`` 的置位/自愈要成对验证。

    连接类错误与超时**恰好重试 1 次**（共 2 次尝试）；HTTP 状态响应（503 预热 / 401 鉴权）
    **不重试**（共 1 次）。``_dead`` 只在传输层失败（旧子进程时代的「管道断了」）时置位，
    超时与状态响应都不置；一次成功分析把它清掉（云服务重启后能自愈）。
    """

    def _counting(self, behavior):
        """包一层计数：只统计 ``/analyze`` 的调用次数，``/health`` 不计入。"""
        calls = {"n": 0}

        def handler(req):
            if req.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "state": "ready"})
            calls["n"] += 1
            return behavior(req)

        return handler, calls

    def test_transport_error_retries_exactly_once(self):
        handler, calls = self._counting(_raise_connect)

        async def run():
            eng = _engine(handler)
            await eng.start()
            return await eng.analyze([]), eng.is_dead(), calls["n"]

        r, dead, n = asyncio.run(run())
        self.assertIn("error", r)
        self.assertEqual(n, 2)          # 首次 + 重试 1 次
        self.assertTrue(dead)           # 传输层失败＝旧时代的「管道断了」：置 _dead

    def test_timeout_retries_exactly_once_without_marking_dead(self):
        handler, calls = self._counting(_raise_read_timeout)

        async def run():
            eng = _engine(handler, timeout_ms=50)
            await eng.start()
            return await eng.analyze([]), eng.is_dead(), calls["n"]

        r, dead, n = asyncio.run(run())
        self.assertIn("超时", r["error"])
        self.assertEqual(n, 2)          # 超时也重试 1 次
        self.assertFalse(dead)          # 但超时不置死（旧实现里超时不杀进程）

    def test_503_does_not_retry(self):
        handler, calls = self._counting(
            lambda req: httpx.Response(503, json={"error": "正在加载"}))

        async def run():
            eng = _engine(handler)
            await eng.start()
            return await eng.analyze([]), eng.is_dead(), calls["n"]

        r, dead, n = asyncio.run(run())
        self.assertIn("正在加载", r["error"])
        self.assertEqual(n, 1)          # 状态响应不重试
        self.assertFalse(dead)          # 也不置死

    def test_401_does_not_retry(self):
        handler, calls = self._counting(
            lambda req: httpx.Response(401, json={"error": "鉴权失败"}))

        async def run():
            eng = _engine(handler)
            await eng.start()
            return await eng.analyze([]), eng.is_dead(), calls["n"]

        r, dead, n = asyncio.run(run())
        self.assertIn("鉴权失败", r["error"])
        self.assertEqual(n, 1)
        self.assertFalse(dead)

    def test_transport_failure_marks_dead_then_success_clears(self):
        """自愈：传输失败置 ``_dead``，云服务恢复后一次成功分析把它清掉。"""
        calls = {"n": 0}

        def handler(req):
            if req.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "state": "ready"})
            calls["n"] += 1
            if calls["n"] <= 2:         # 首次 + 重试都失败
                raise httpx.ConnectError("boom", request=req)
            return httpx.Response(200, json=ANALYSIS)

        async def run():
            eng = _engine(handler)
            await eng.start()
            first = await eng.analyze([])
            dead_after_failure = eng.is_dead()
            second = await eng.analyze([])
            return first, dead_after_failure, second, eng.is_dead(), eng.last_error()

        first, dead_after_failure, second, dead_after_success, err = asyncio.run(run())
        self.assertIn("error", first)
        self.assertTrue(dead_after_failure)
        self.assertIn("rootInfo", second)
        self.assertFalse(dead_after_success)
        self.assertEqual(err, "")


class MiscTest(unittest.TestCase):
    def test_stderr_tail_format(self):
        eng = KataGoEngine()
        eng._stderr_tail = ["a", "b", "c", "d"]
        self.assertEqual(eng.stderr_tail(2), "c | d")
        self.assertEqual(eng.stderr_tail(0), "a | b | c | d")


if __name__ == "__main__":
    unittest.main()
