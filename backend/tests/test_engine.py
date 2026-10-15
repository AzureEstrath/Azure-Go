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


class MiscTest(unittest.TestCase):
    def test_stderr_tail_format(self):
        eng = KataGoEngine()
        eng._stderr_tail = ["a", "b", "c", "d"]
        self.assertEqual(eng.stderr_tail(2), "c | d")
        self.assertEqual(eng.stderr_tail(0), "a | b | c | d")


if __name__ == "__main__":
    unittest.main()
