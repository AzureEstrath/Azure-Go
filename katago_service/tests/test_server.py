"""HTTP 面：健康态、鉴权、请求透传、未就绪语义（假引擎，不起真进程）。"""

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest import mock

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


class DrivenFake:
    """有生命周期的假引擎：状态由服务自己的状态机决定（P12 的回归守卫用）。

    gate 非 None 时 warmup 会阻塞到它被 set（让「预热中」这个窗口可观察）；
    fail_warmup 模拟「预热超时/报错但进程还活着」（P10：仍须 ready）；
    die_in_warmup 模拟「预热期间进程死掉」（须 dead）。
    """

    def __init__(self, gate=None, fail_warmup=False, die_in_warmup=False):
        self.gate = gate
        self.fail_warmup = fail_warmup
        self.die_in_warmup = die_in_warmup
        self.started = False
        self.warmed = False
        self.dead = False
        self.closed = False
        self.seen = []

    async def start(self):
        self.started = True
        return True

    async def warmup(self):
        while self.gate is not None and not self.gate.is_set():
            await asyncio.sleep(0.005)          # 让出事件循环：预热期间端口仍要能应答
        self.warmed = True
        if self.die_in_warmup:
            self.dead = True
            raise RuntimeError("预热时引擎崩了")
        if self.fail_warmup:
            raise TimeoutError("预热超时")

    async def shutdown(self):
        self.closed = True

    def is_dead(self):
        return self.dead

    def last_error(self):
        return "假错误" if self.dead else ""

    def state_name(self):
        return "dead" if self.dead else ("ready" if self.warmed else "warming")

    async def analyze(self, req):
        self.seen.append(dict(req))
        return {"rootInfo": {"winrate": 0.5}, "moveInfos": []}


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


class WarmingVisibilityTest(unittest.TestCase):
    """Review Focus #1 的回归守卫：预热在后台跑，warming 期间端口必须可达（P12）。

    预热若被卡回 lifespan 里（uvicorn 要等 lifespan 结束才绑端口），这里第一个
    /health 就会拿到 ready 而不是 warming —— 即「预热窗口内客户端只能连接被拒」。
    """

    def test_port_answers_while_warming(self):
        gate = threading.Event()
        # 兜底闹钟：万一预热被卡回 lifespan（回归），测试也要能收尾并给出断言失败，而不是永久挂住
        watchdog = threading.Timer(5.0, gate.set)
        watchdog.start()
        try:
            proc = DrivenFake(gate=gate)
            with _client(proc) as c:
                body = c.get("/health").json()               # 预热中端口可达＝本裁决的全部意义
                self.assertTrue(proc.started)                # start() 已跑完，预热留在后台
                self.assertEqual(body["state"], "warming")
                self.assertFalse(body["ok"])
                r = c.post("/analyze", json={"moves": []})   # 不得悬挂到客户端超时
                self.assertEqual(r.status_code, 503)
                self.assertIn("预热", r.json()["error"])
                gate.set()                                   # 预热完成
                self.assertTrue(self._wait_state(c, "ready"))
                self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 200)
        finally:
            watchdog.cancel()

    def test_warmup_failure_on_live_process_ends_ready(self):
        """P10：预热超时/报错但进程还活着 → ready（引擎会应答真实请求）。"""
        with _client(DrivenFake(fail_warmup=True)) as c:
            self.assertTrue(self._wait_state(c, "ready"))
            self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 200)

    def test_process_dead_after_warmup_ends_dead(self):
        with _client(DrivenFake(die_in_warmup=True)) as c:
            self.assertTrue(self._wait_state(c, "dead"))
            body = c.get("/health").json()
            self.assertFalse(body["ok"])
            self.assertIn("假错误", body["katago_error"])
            self.assertEqual(c.post("/analyze", json={"moves": []}).status_code, 503)

    def test_shutdown_cancels_warmup_task(self):
        """关闭时预热任务必须被取消并等待：不留孤儿任务、不打「Task was destroyed」噪音。"""
        gate = threading.Event()
        watchdog = threading.Timer(5.0, gate.set)   # 同上：回归时给出断言失败而不是永久挂住
        watchdog.start()
        try:
            proc = DrivenFake(gate=gate)
            with _client(proc) as c:
                self.assertEqual(c.get("/health").json()["state"], "warming")
            self.assertFalse(proc.warmed)       # 预热没跑完，说明是被取消的
            self.assertTrue(proc.closed)        # 引擎关闭发生在取消之后
        finally:
            watchdog.cancel()

    def _wait_state(self, c, want, tries=300):
        for _ in range(tries):
            if c.get("/health").json()["state"] == want:
                return True
            time.sleep(0.01)
        return False


class CliArgsTest(unittest.TestCase):
    def test_args_parse(self):
        args = server.parse_args(["--port", "8101", "--token", "t",
                                  "--katago-exe", "k.exe", "--model", "m.bin", "--config", "c.cfg",
                                  "--analysis-threads", "4"])
        self.assertEqual(args.port, 8101)
        self.assertEqual(args.token, "t")
        self.assertEqual(args.analysis_threads, 4)

    def test_args_default_port(self):
        self.assertEqual(server.parse_args([]).port, 8100)

    def test_args_default_analysis_threads(self):
        self.assertEqual(server.parse_args([]).analysis_threads, 1)

    def test_args_default_host_is_loopback(self):
        # --host 不传时由 resolve_host 兜底（flag → KATAGO_HOST → 回环），这里钉住最终值
        with mock.patch.dict(os.environ, {"KATAGO_HOST": ""}):
            self.assertEqual(server.resolve_host(server.parse_args([])), "127.0.0.1")

    def test_args_host_override(self):
        self.assertEqual(server.parse_args(["--host", "0.0.0.0"]).host, "0.0.0.0")


class CliCfgMappingTest(unittest.TestCase):
    """P13 守卫：命令行参数必须落到 ``_build_proc`` 读的键上（exe/model/config/analysis_threads）。

    参数名是 ``--katago-exe``（dest 为 ``katago_exe``），引擎读的键却是 ``exe``；
    把 argparse 的 namespace 直接当 cfg 传进 ``create_app`` 会在启动时抛
    ``ValueError: create_app 的 cfg 缺少引擎路径：exe``。下面两条就是那个坑的回归守卫。
    """

    def test_args_mapping_feeds_create_app(self):
        args = server.parse_args(["--katago-exe", "k.exe", "--model", "m.bin",
                                  "--config", "c.cfg", "--analysis-threads", "3"])
        app = server.build_app(args)            # 只构造引擎，不 start：路径无需真实存在
        proc = app.state.proc
        self.assertEqual((proc.exe, proc.model, proc.cfg), ("k.exe", "m.bin", "c.cfg"))
        self.assertEqual(proc.analysis_threads, 3)

    def test_empty_flags_fall_back_to_env(self):
        """run_server.bat 把（可能为空的）环境变量当参数传进来，空值必须回落到环境变量。"""
        env = {"KATAGO_EXE": "env-katago.exe", "KATAGO_MODEL": "env-model.bin",
               "KATAGO_CONFIG": "env-analysis.cfg", "KATAGO_SERVICE_TOKEN": "env-token"}
        with mock.patch.dict(os.environ, env):
            args = server.parse_args(["--katago-exe", "", "--model", "",
                                      "--config", "", "--token", ""])
            app = server.build_app(args)
        proc = app.state.proc
        self.assertEqual((proc.exe, proc.model, proc.cfg),
                         ("env-katago.exe", "env-model.bin", "env-analysis.cfg"))
        self.assertEqual(app.state.token, "env-token")

    def test_host_env_applies_without_flag(self):
        """P16-F1 回归守卫：不配 --host 时 KATAGO_HOST 也必须生效。

        ``--host`` 若带非空默认值，``args.host or KATAGO_HOST or DEFAULT_HOST`` 永远不会走到
        环境变量，``KATAGO_HOST=0.0.0.0 python -m katago_service.server`` 会静默只绑回环，
        README 里「set KATAGO_HOST=0.0.0.0 即可」的写法就成了错的。
        """
        with mock.patch.dict(os.environ, {"KATAGO_HOST": "0.0.0.0"}):
            self.assertEqual(server.resolve_host(server.parse_args([])), "0.0.0.0")

    def test_host_flag_beats_env_then_loopback(self):
        """优先级：--host → KATAGO_HOST → 回环；空字符串一律按「没传」处理（bat 的传参形态）。"""
        with mock.patch.dict(os.environ, {"KATAGO_HOST": "0.0.0.0"}):
            self.assertEqual(server.resolve_host(server.parse_args(["--host", "10.0.0.5"])), "10.0.0.5")
        with mock.patch.dict(os.environ, {"KATAGO_HOST": ""}):
            self.assertEqual(server.resolve_host(server.parse_args(["--host", ""])), "127.0.0.1")


class LoopbackTest(unittest.TestCase):
    """P19-⑤ 守卫：回环判定用 ipaddress 解析，回环变体不得误报「对外监听」。"""

    def test_loopback_variants_are_loopback(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(server._is_loopback(host), host)

    def test_non_loopback_is_not_loopback(self):
        for host in ("0.0.0.0", "10.0.0.5", "192.168.1.10", "example.com", ""):
            self.assertFalse(server._is_loopback(host), host)


if __name__ == "__main__":
    unittest.main()
