"""HTTP 面：健康态、鉴权、请求透传、未就绪语义（假引擎，不起真进程）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient

from katago_service import server


class FakeProc:
    """假引擎：可控状态与固定响应。"""

    def __init__(self, state="ready", reply=None):
        self.state = state
        self.reply = reply if reply is not None else {"rootInfo": {"winrate": 0.5}, "moveInfos": []}
        self.seen = []

    def is_dead(self):
        return self.state == "dead"

    def last_error(self):
        return "假错误" if self.state == "dead" else ""

    def state_name(self):
        return self.state

    async def analyze(self, req):
        self.seen.append(dict(req))
        return dict(self.reply)


def _client(proc, token=""):
    return TestClient(server.create_app(proc=proc, token=token))


class HealthTest(unittest.TestCase):
    def test_health_shape(self):
        with _client(FakeProc()) as c:
            r = c.get("/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["state"], "ready")

    def test_health_reports_dead(self):
        with _client(FakeProc(state="dead")) as c:
            body = c.get("/health").json()
            self.assertFalse(body["ok"])
            self.assertEqual(body["state"], "dead")
            self.assertIn("假错误", body["katago_error"])


class AnalyzeTest(unittest.TestCase):
    def test_passthrough_request_and_reply(self):
        proc = FakeProc()
        with _client(proc) as c:
            r = c.post("/analyze", json={"moves": [["B", "Q16"]], "maxVisits": 100})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(proc.seen[0]["moves"], [["B", "Q16"]])
            self.assertIn("rootInfo", r.json())

    def test_warming_returns_503(self):
        with _client(FakeProc(state="warming")) as c:
            r = c.post("/analyze", json={"moves": []})
            self.assertEqual(r.status_code, 503)
            self.assertIn("error", r.json())

    def test_dead_returns_503(self):
        with _client(FakeProc(state="dead")) as c:
            self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 503)

    def test_engine_error_passthrough_is_200(self):
        proc = FakeProc(reply={"error": "illegal move", "field": "moves"})
        with _client(proc) as c:
            r = c.post("/analyze", json={"moves": [["B", "A1"]]})
            self.assertEqual(r.status_code, 200)       # 引擎的合法应答：由调用方判断 error 字段
            self.assertEqual(r.json()["error"], "illegal move")


class AuthTest(unittest.TestCase):
    def test_token_required_when_configured(self):
        with _client(FakeProc(), token="secret") as c:
            self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 401)
            self.assertEqual(c.get("/health").status_code, 200)      # 健康检查不鉴权

    def test_token_accepted(self):
        with _client(FakeProc(), token="secret") as c:
            r = c.post("/analyze", json={"moves": []}, headers={"Authorization": "Bearer secret"})
            self.assertEqual(r.status_code, 200)

    def test_no_token_configured_allows_local(self):
        with _client(FakeProc()) as c:
            self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
