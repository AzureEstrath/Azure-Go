# 计划 01：KataGo 自建云服务 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 KataGo 从“后端进程内的子进程”变成“独立可部署的云服务”，后端只留一个 HTTP 客户端；**不保留** local transport。

**Architecture:** 新建 `katago_service/`：FastAPI + 常驻 `katago analysis` 子进程，暴露 `POST /analyze` 与 `GET /health`。`POST /analyze` 的**请求体就是 KataGo analysis 引擎请求原文**（`id/moves/rules/komi/boardXSize/boardYSize/maxVisits/includeOwnership`），响应就是引擎响应原文——因此后端 `agent.py`、`prompts.py`、`state.py` 一行都不用改，只换 `engine.py` 的传输层。服务启动即自预热（空盘分析），OpenCL 首次调优从此发生在部署阶段。

**Tech Stack:** FastAPI / uvicorn / httpx（服务端不依赖 KataGo 的 Python 绑定，直接管子进程）；`unittest` + `fastapi.testclient.TestClient` + `httpx.MockTransport`。

**Spec:** `docs/backend-langchain-plan.md`（决策 D3/D4）

## Global Constraints

- 计划 0 必须先完成（本计划假设 `backend/app/services.py` 已删、`config.py` 已无本地服务字段）。
- 测试一律 `unittest`；后端：`cd backend && python -m unittest discover -s tests`；服务：`cd katago_service && python -m unittest discover -s tests`。不得引入 pytest。
- **任何测试都不得依赖真实 KataGo 进程或公网**：服务侧用假引擎，后端侧用 `httpx.MockTransport`。
- 注释、UI 文案、提交信息一律中文；提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.1.0**，同步写进 `README.md` 的「更新」小节。
- 两个必须照搬的坑（来自现有 `backend/app/engine.py` 的注释，删了就踩）：① **stderr 必须持续排空**，否则缓冲区写满后 KataGo 卡死；② asyncio 读行缓冲必须显式放大到 64MB，否则带 ownership 的超大 JSON 会触发「Separator is not found」并被误判为进程死亡。
- `max_visits` 仍是每次分析的入参（客户端可覆盖），不得写死在服务端。
- 后端的失败语义不得变：`analyze()` 出错必须返回 `{"error": "<可读中文>"}`（`agent.py` 靠 `"error" in analysis` 判定）。
- `GET /api/health` 的 `katago_dead` / `katago_error` 字段语义保留（现在表示“云服务不可用”）。

## Review Focus

- **服务在预热中就收到请求**：`/health` 应回 `warming`，`/analyze` 应回 400/503 的可读错误，**不得**让请求悬挂到客户端超时（客户端每手都在等）。
- **超大 ownership 响应**：19×19 + `includeOwnership` 的响应可达数百 KB~数 MB 单行——读行缓冲与超时都要能过；被截断时应报错而不是崩。
- **服务不可达时的玩家体验**：后端必须让 `busy` 归位、给出可读提示，棋盘不得永久锁死。
- **鉴权**：配置了 token 就必须校验（缺 token / 错 token → 401），未配置 token 时（本机回环调试）允许直连。
- **超时与重试边界**：客户端超时应重试 1 次后报错；重试不得导致同一手被分析两次而把 `pre_analysis` 缓存写脏。

---

### Task 1: 服务侧常驻分析进程（移植 + 预热）

**Files:**
- Create: `katago_service/__init__.py`（空）
- Create: `katago_service/proc.py`
- Create: `katago_service/tests/__init__.py`（空）
- Create: `katago_service/tests/test_proc.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class KatagoProcess:` `__init__(self, exe, model, cfg, analysis_threads=1, log_dir=None, max_visits=500)`
  - `async start(self) -> bool`（缺文件 → `_fail` 并返回 False）
  - `async analyze(self, req: dict) -> dict`（`req` 原样转发，自动补 `id`；失败返回 `{"error": ...}`）
  - `async warmup(self) -> None`（空盘低 visits 分析一次，吞掉结果）
  - `async shutdown(self) -> None`、`is_dead(self) -> bool`、`last_error(self) -> str`、`stderr_tail(self, n=3) -> str`
  - 模块常量 `REQ_TIMEOUT_MS = 30000`、`STARTUP_TIMEOUT_MS = 180000`、`STREAM_LIMIT = 64 * 1024 * 1024`

- [ ] **Step 1: 写失败测试** — `katago_service/tests/test_proc.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd katago_service && python -m unittest tests.test_proc -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'katago_service.proc'`）

- [ ] **Step 3: 实施** — `katago_service/proc.py`：从 `backend/app/engine.py` **逐段移植**子进程管理（`_drain_stderr`、`_read_stdout` 的 `id→Future` 派发、`_reject_all`、`_fail`、`shutdown`），把 `analyze(moves)` 的**请求体构造**去掉（改为原样转发调用方给的 `req`，只补 `"id": "r%d" % self._seq`），并新增 `warmup()`（`{"moves": [], "maxVisits": 1, "includeOwnership": False}`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd katago_service && python -m unittest tests.test_proc -v`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add katago_service/__init__.py katago_service/proc.py katago_service/tests/
git commit -m "feat(katago_service): 常驻 analysis 进程封装与预热（v3.1.0）"
```

---

### Task 2: `/health` 与 `/analyze` 端点（含鉴权与 warming 语义）

**Files:**
- Create: `katago_service/server.py`
- Create: `katago_service/tests/test_server.py`

**Interfaces:**
- Consumes: `KatagoProcess`（Task 1 的签名）
- Produces:
  - `create_app(proc=None, token="", cfg=None) -> FastAPI`（`proc` 可注入假引擎；`token` 为空＝不校验）
  - `GET /health` → `{"ok": bool, "state": "warming"|"ready"|"dead", "katago_error": str, "uptime_s": float}`
  - `POST /analyze`（body：引擎请求原文）→ 200 引擎响应原文｜503 `{"error": ...}`（未就绪 / 进程已死）｜401 鉴权失败
  - 状态常量 `STATE_WARMING = "warming"`、`STATE_READY = "ready"`、`STATE_DEAD = "dead"`

- [ ] **Step 1: 写失败测试** — `katago_service/tests/test_server.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd katago_service && python -m unittest tests.test_server -v`
Expected: FAIL（`ModuleNotFoundError: katago_service.server`）

- [ ] **Step 3: 实施** — `katago_service/server.py`。要点：`create_app` 在 lifespan 里 `await proc.start()` → `await proc.warmup()` → 置 `STATE_READY`（先置 `STATE_WARMING`）；`/analyze` 用 `asyncio.Lock` 串行调用 `proc.analyze`（单用户场景，避免同一引擎并发争抢）；鉴权用 `Authorization: Bearer <token>` 比较；`state` 由 `proc.state_name()` 或 `is_dead()` 推导，保证可被假引擎驱动。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd katago_service && python -m unittest discover -s tests -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add katago_service/server.py katago_service/tests/test_server.py
git commit -m "feat(katago_service): /analyze 与 /health 端点、Bearer 鉴权与 warming 语义（v3.1.0）"
```

---

### Task 3: 部署物料（依赖、启动脚本、入口）

**Files:**
- Create: `katago_service/requirements.txt`（`fastapi>=0.110`、`uvicorn>=0.27`）
- Create: `katago_service/run_server.bat`
- Modify: `katago_service/server.py`（追加 `if __name__ == "__main__":` 入口：读命令行参数并 `uvicorn.run`）

**Interfaces:**
- Consumes: `create_app(proc, token, cfg)`（Task 2）
- Produces: 可执行入口 `python -m katago_service.server --port 8100 --token <t> --katago-exe <p> --model <p> --config <p> [--analysis-threads N]`；环境变量兜底 `KATAGO_SERVICE_TOKEN`、`KATAGO_EXE`、`KATAGO_MODEL`、`KATAGO_CONFIG`

- [ ] **Step 1: 写测试** — 追加到 `katago_service/tests/test_server.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd katago_service && python -m unittest tests.test_server.CliArgsTest -v`
Expected: FAIL（`parse_args` 不存在）

- [ ] **Step 3: 实施** — 用 `argparse` 实现 `parse_args(argv=None)`；`run_server.bat` 内容：`cd /d "%~dp0"` + `python -m katago_service.server --port 8100 --katago-exe "%KATAGO_EXE%" ...`（参数留空则由环境变量兜底）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd katago_service && python -m unittest discover -s tests -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add katago_service/requirements.txt katago_service/run_server.bat katago_service/server.py katago_service/tests/test_server.py
git commit -m "chore(katago_service): 依赖、启动脚本与命令行入口（v3.1.0）"
```

---

### Task 4: 后端 `engine.py` 改为 HTTP 客户端

**Files:**
- Modify: `backend/app/engine.py`（整文件重写为 HTTP 客户端）
- Rewrite: `backend/tests/test_engine.py`
- Modify: `backend/requirements.txt`（`httpx` 已在，无需新增）

**Interfaces:**
- Consumes: KataGo 服务 `GET /health`、`POST /analyze`（Task 2/3）
- Produces:
  - `KataGoEngine(url="http://127.0.0.1:8100", token="", max_visits=500, timeout_ms=30000, ready_timeout_ms=180000, client=None, ready_poll_sec=1.0)`
  - `async start() -> bool`（探 `/health` 并等 `state == "ready"`；不可达或 `dead` → `False` 且 `is_dead()` 为 True）
  - `async analyze(moves) -> dict`（请求体与旧实现**完全一致**：`rules="chinese"`、`komi=7.5`、`boardXSize/YSize=19`、`maxVisits`、`includeOwnership=True`）
  - `is_dead()`、`last_error()`、`stderr_tail(n=3)`、`async shutdown()`（沿用旧签名；`shutdown` 只关闭 httpx 客户端，不再杀进程）

- [ ] **Step 1: 重写测试** — `backend/tests/test_engine.py`

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_engine -v`
Expected: FAIL（`KataGoEngine.__init__` 不接受 `url/client` 等参数；`start()` 仍去找文件）

- [ ] **Step 3: 实施** — 重写 `backend/app/engine.py`：保留类名 `KataGoEngine`、保留 `is_dead/last_error/stderr_tail/shutdown` 签名与 `_stderr_tail` 属性；删除 `_STREAM_LIMIT`、`DEFAULT_EXE/MODEL/CFG`、`_LOG_DIR`、子进程相关全部代码；`analyze()` 里请求体照抄旧实现（含 `includeOwnership=True`）；超时用 `timeout_ms`，失败重试 **1 次**（仅对连接类错误与超时），两次都失败才返回 `{"error": ...}`。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_engine -v`
Expected: 全 PASS

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine.py backend/tests/test_engine.py
git commit -m "refactor(backend): KataGo 引擎改为云服务 HTTP 客户端（v3.1.0）"
```

---

### Task 5: 后端配置与装配接线

**Files:**
- Modify: `backend/app/config.py`（删 `katago_bin/katago_model/katago_config`，新增 `katago_url="http://127.0.0.1:8100"`、`katago_token=""`、`katago_timeout_ms=30000`；`has_paths()` 改名 `has_katago()`，判据改为 `katago_url` 非空——已核实 `has_paths()` **当前无调用方**，改名安全）
- Modify: `backend/app/main.py`（lifespan 里 `KataGoEngine(cfg.katago_url, cfg.katago_token, cfg.max_visits, cfg.katago_timeout_ms)`；删掉 KataGo 失败时的文件路径措辞；`/api/health` 增加 `katago_url` 字段）
- Modify: `backend/tests/test_config.py`（把 `katago_*` 断言改为新字段）

**Interfaces:**
- Consumes: `KataGoEngine(url, token, max_visits, timeout_ms)`（Task 4）
- Produces: `AzureConfig` 字段集 = `katago_url / katago_token / katago_timeout_ms / max_visits / joseki_teach / llm_* / prompt_*`；`GET /api/health` → `{ok, katago_dead, katago_error, katago_url, llm_url}`

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_config.py`

```python
class CloudKatagoConfigTest(unittest.TestCase):
    def test_katago_fields_replaced(self):
        c = AzureConfig()
        for gone in ("katago_bin", "katago_model", "katago_config"):
            self.assertFalse(hasattr(c, gone), gone)
        for now in ("katago_url", "katago_token", "katago_timeout_ms"):
            self.assertTrue(hasattr(c, now), now)

    def test_has_katago_follows_url(self):
        c = AzureConfig()
        c.katago_url = "http://127.0.0.1:8100"
        self.assertTrue(c.has_katago())
        c.katago_url = ""
        self.assertFalse(c.has_katago())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_config -v`
Expected: FAIL

- [ ] **Step 3: 实施** — 按 `Files` 修改；`game/config.json` 的对应字段在 Task 6 一并改。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/app/main.py backend/tests/test_config.py
git commit -m "refactor(backend): 配置与装配改接 KataGo 云服务（v3.1.0）"
```

---

### Task 6: 配置模板、文档与版本

**Files:**
- Modify: `game/config.json`（删 `katago_bin/katago_model/katago_config`；新增 `katago_url`、`katago_token`、`katago_timeout_ms`；`_说明` 补一句“KataGo 由独立云服务提供”）
- Modify: `README.md`（结构树补 `katago_service\`；第三节字段表替换 KataGo 三行为 `katago_url/katago_token/katago_timeout_ms`；新增「KataGo 服务部署」小节：机器要求、`pip install -r katago_service/requirements.txt`、`run_server.bat` 参数、`/health` 自检、Bearer token 与务必只在内网/TLS 后暴露；第九节新增 `### v3.1.0`）
- Modify: `AGENTS.md`（把“KataGo 子进程/`katago.exe` 随包”的描述改为“KataGo 是独立部署的云服务，仓库只含客户端”；补 `katago_service/` 的定位与「改动后端时不要再引入子进程」的约束）

**Interfaces:**
- Consumes: 无
- Produces: 与代码一致的模板与文档

- [ ] **Step 1: 改文档与模板** — 按 `Files` 执行。
- [ ] **Step 2: 校验**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `cd katago_service && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `rg -n "katago_bin|katago_model|katago_config" README.md AGENTS.md game backend docs`
Expected: 无匹配（历史计划文档里的措辞除外）

- [ ] **Step 3: 提交**

```bash
git add README.md AGENTS.md game/config.json
git commit -m "docs(config): KataGo 云服务配置、部署说明与 v3.1.0（v3.1.0）"
```
