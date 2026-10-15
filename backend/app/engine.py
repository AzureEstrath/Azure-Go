"""KataGo analysis 客户端：对接独立的 KataGo 云服务（HTTP），不再自己拉起子进程。

服务侧接口（见 ``katago_service/server.py``）：
    ``GET  /health``  → ``{"ok": bool, "state": "warming"|"ready"|"dead", "katago_error": str}``
    ``POST /analyze`` → 200 引擎响应原文｜503 未就绪/已死｜401 鉴权失败

对外接口与子进程版一一对应：``start() / is_dead() / last_error() / stderr_tail() / analyze() / shutdown()``，
``agent.py`` 与 ``prompts.py`` 因此无需改动（``analyze()`` 的请求体逐字段照抄旧实现）。

子进程时代的两条硬约束——stderr 必须持续排空（否则缓冲区写满后 KataGo 卡死）、读行缓冲必须显式放大到
64MB（否则带 ownership 的超大 JSON 会触发「Separator is not found」并被误判为进程死亡）——随引擎一起
搬进了 ``katago_service/proc.py``；本文件不再持有管道，也就没有这两处坑。

**就绪判定只认 ``state``，不认 ``ok``**：服务预热期会回 ``{"ok": false, "state": "warming"}``，
按 ``ok`` 判会把「正在加载模型」误当成「不可用」。
"""

import asyncio
import time

import httpx

DEFAULT_URL = "http://127.0.0.1:8100"
REQ_TIMEOUT_MS = 30000          # 单次 /analyze 超时
READY_TIMEOUT_MS = 180000       # 等 /health 变 ready 的上限（首次分析要等引擎加载模型）
READY_POLL_SEC = 1.0            # 预热期的轮询间隔

STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_DEAD = "dead"


def _health_state(resp):
    """解析 ``/health`` 响应，返回 ``(state, body)``；形状不对时 ``state`` 为空串。"""
    if resp.status_code != 200:
        return "", {}
    try:
        body = resp.json()
    except Exception:
        return "", {}
    if not isinstance(body, dict):
        return "", {}
    state = body.get("state")
    return (state if state in (STATE_WARMING, STATE_READY, STATE_DEAD) else ""), body


class KataGoEngine:
    def __init__(self, url=DEFAULT_URL, token="", max_visits=500,
                 timeout_ms=REQ_TIMEOUT_MS, ready_timeout_ms=READY_TIMEOUT_MS,
                 client=None, ready_poll_sec=READY_POLL_SEC):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.token = token or ""
        self.max_visits = max_visits
        self.timeout_ms = int(timeout_ms)
        self.ready_timeout_ms = int(ready_timeout_ms)
        self.ready_poll_sec = ready_poll_sec

        # 请求头只在构造时算一次；未配置 token（本机回环调试）就不带 Authorization
        self._headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        self._timeout_s = max(self.timeout_ms, 1) / 1000.0
        self._client = client if client is not None else httpx.AsyncClient(timeout=self._timeout_s)

        self._seq = 0
        self._stderr_tail = []          # 历史字段：见 stderr_tail()
        self._dead = False
        self._last_error = ""

    # ================= 生命周期 =================

    async def start(self) -> bool:
        """探 ``/health`` 并等到 ``state == "ready"``。

        ``warming`` 只是「还在加载模型」，继续轮询；``dead`` 或连不上才算失败（置 ``_dead``）。
        探测本身出别的岔子（响应不可解析、状态码异常）时返回 False 但**不判死**：
        云服务是可恢复的，把结论写死只会让棋盘永久锁死，真实原因交给后续 ``analyze()`` 报。
        """
        deadline = time.monotonic() + self.ready_timeout_ms / 1000.0
        while True:
            try:
                resp = await self._client.get(self._url("/health"), headers=self._headers,
                                              timeout=self._timeout_s)
            except httpx.TimeoutException:
                self._fail("连不上 KataGo 云服务：健康检查超时（%s）" % self.url)
                return False
            except httpx.TransportError as e:
                self._fail("连不上 KataGo 云服务：%s（%s）" % (e, self.url))
                return False
            except Exception as e:
                self._record_error("KataGo 云服务健康检查失败：%s" % e)
                return False

            state, body = _health_state(resp)
            if state == STATE_READY:
                print("[KataGo] 云服务已就绪：%s" % self.url)
                return True
            if state == STATE_DEAD:
                self._fail("KataGo 云服务报告引擎已死：%s" % (body.get("katago_error") or "未知原因"))
                return False
            if state == STATE_WARMING:
                if time.monotonic() >= deadline:
                    self._record_error("等待 KataGo 云服务预热超时（%d ms）" % self.ready_timeout_ms)
                    return False
                await asyncio.sleep(self.ready_poll_sec)
                continue
            # 响应不是 /health 的形状：立刻放弃（不空转到超时），但同样不判死
            self._record_error("KataGo 云服务健康检查响应异常（HTTP %d）" % resp.status_code)
            return False

    def is_dead(self) -> bool:
        return self._dead

    def last_error(self) -> str:
        return self._last_error

    def stderr_tail(self, n=3) -> str:
        """历史字段：子进程时代是引擎 stderr 尾部；改为云服务后不再产生，保留以兼容调用方。"""
        tail = self._stderr_tail[-n:] if n > 0 else self._stderr_tail
        return " | ".join(tail)

    def _record_error(self, reason) -> str:
        self._last_error = reason
        print("[KataGo] " + reason)
        return reason

    def _fail(self, reason):
        self._dead = True
        self._record_error(reason)

    async def shutdown(self):
        """只关闭 httpx 客户端：引擎进程归 KataGo 云服务所有，后端不再杀进程。"""
        try:
            await self._client.aclose()
        except Exception as e:
            print("[KataGo] 关闭 HTTP 客户端出错：%s" % e)

    def _url(self, path) -> str:
        return self.url + path

    # ================= 分析 =================

    async def analyze(self, moves) -> dict:
        """请求一次分析；失败一律返回 ``{"error": <可读中文>}``（``agent.py`` 靠 ``"error" in analysis`` 判定）。

        连接类错误与超时重试 **1 次**：重试在本次调用内部完成、只返回一个结果，
        不会让同一手被分析两次而把 ``pre_analysis`` 缓存写脏。
        """
        self._seq += 1
        req = {
            "id": "r%d" % self._seq,
            "moves": moves,
            "rules": "chinese",
            "komi": 7.5,
            "boardXSize": 19,
            "boardYSize": 19,
            "maxVisits": self.max_visits,
            "includeOwnership": True,       # 逐点归属（+ = 黑）：用于「双方实地估算」
        }
        err = ""
        for _ in range(2):                  # 首次 + 重试 1 次
            try:
                resp = await self._client.post(self._url("/analyze"), json=req,
                                               headers=self._headers, timeout=self._timeout_s)
            except httpx.TimeoutException:
                err = "KataGo 云服务响应超时（%d ms）" % self.timeout_ms
                continue
            except httpx.TransportError as e:
                err = "连不上 KataGo 云服务：%s（%s）" % (e, self.url)
                continue
            except Exception as e:
                err = "KataGo 云服务请求失败：%s" % e
                break
            return self._parse_reply(resp)
        return {"error": self._record_error(err)}

    def _parse_reply(self, resp) -> dict:
        """200 → 引擎响应原文（引擎自报的 ``error`` 字段是合法应答，交调用方判断）；非 200 → 可读中文错误。"""
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                return {"error": self._record_error("KataGo 云服务响应无法解析：%s" % e)}
            if not isinstance(data, dict):
                return {"error": self._record_error("KataGo 云服务响应不是 JSON 对象")}
            if "error" in data:             # 引擎的合法应答：只记录，不影响调用方判定
                self._record_error("%s (field=%s)" % (data["error"], data.get("field", "")))
            else:
                # 分析成功即摘掉「死」标记：云服务重启后能自愈，棋盘不会被永久锁死
                self._dead = False
                self._last_error = ""
            return data
        msg = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                msg = str(body.get("error", ""))
        except Exception:
            pass
        if not msg:
            msg = "KataGo 云服务返回 HTTP %d" % resp.status_code
        return {"error": self._record_error(msg)}
