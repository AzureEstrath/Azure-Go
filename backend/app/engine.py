"""常驻 KataGo analysis 子进程（对应 ``KataGoEngine.gd``）。

与 Godot 版一致：行分隔 JSON 请求/响应，moves 必须是带颜色的配对 [["B","Q16"],["W","D4"]]。
Godot 用 ``OS.execute_with_pipe`` + 每帧轮询；Python 侧改用 ``asyncio`` 子进程与读行任务，
语义不变：请求超时 30s、启动（首次响应）超时 180s、stderr 必须持续排空否则 KataGo 会卡死。

对外接口与 Godot 版一一对应：``start() / is_dead() / last_error() / stderr_tail() / analyze() / shutdown()``。
"""

import asyncio
import json
import os

REQ_TIMEOUT_MS = 30000
STARTUP_TIMEOUT_MS = 180000

# asyncio 子进程默认单行上限 64 KiB：KataGo 带 includeOwnership 的回应是一整行
# 超大 JSON（可达数百 KB~数 MB），会触发「Separator is not found」并误判为进程死亡。
# Godot 版按字节扫描无此限制，这里必须显式放大读行缓冲。
_STREAM_LIMIT = 64 * 1024 * 1024

# 日志重定向到后端可写目录：避免污染项目目录，也避免 KataGo 因 logDir 不可写而出错
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "katago")

DEFAULT_EXE = r"D:\KataGo\bin\katago\katago.exe"
DEFAULT_MODEL = r"D:\KataGo\models\model-b18c384nbt.bin.gz"
DEFAULT_CFG = r"D:\KataGo\bin\katago\default_gtp.cfg"


class KataGoEngine:
    def __init__(self, exe=DEFAULT_EXE, model=DEFAULT_MODEL, cfg=DEFAULT_CFG, max_visits=500):
        self.exe = exe
        self.model = model
        self.cfg = cfg
        self.max_visits = max_visits

        self._proc = None
        self._read_task = None
        self._err_task = None
        self._seq = 0
        self._pending = {}              # id -> asyncio.Future
        self._stderr_tail = []
        self._dead = False
        self._last_error = ""
        self._ever_replied = False

    # ================= 生命周期 =================

    async def start(self) -> bool:
        for path, what in ((self.exe, "可执行文件"), (self.model, "模型文件"), (self.cfg, "配置文件")):
            if not os.path.isfile(path):
                self._fail("找不到 KataGo %s：%s" % (what, path))
                return False
        try:
            os.makedirs(_LOG_DIR, exist_ok=True)
        except Exception:
            pass
        args = [
            self.exe, "analysis",
            "-model", self.model,
            "-config", self.cfg,
            "-analysis-threads", "1",
            "-override-config", "logDir=%s" % _LOG_DIR,
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
            )
        except Exception as e:
            self._fail("无法启动 KataGo 子进程：%s" % e)
            return False
        self._read_task = asyncio.create_task(self._read_stdout())
        self._err_task = asyncio.create_task(self._drain_stderr())
        print("[KataGo] 已启动 pid=%d  (%s)" % (self._proc.pid, self.exe))
        return True

    def is_dead(self) -> bool:
        return self._dead

    def last_error(self) -> str:
        return self._last_error

    def stderr_tail(self, n=3) -> str:
        tail = self._stderr_tail[-n:] if n > 0 else self._stderr_tail
        return " | ".join(tail)

    def _fail(self, reason):
        self._dead = True
        self._last_error = reason
        print("[KataGo] " + reason)
        self._reject_all(reason)

    def _reject_all(self, reason):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({"error": reason})
        self._pending.clear()

    async def shutdown(self):
        for t in (self._read_task, self._err_task):
            if t is not None:
                t.cancel()
        self._read_task = None
        self._err_task = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

    # ================= 读行 =================

    async def _drain_stderr(self):
        """stderr 必须持续排空，否则缓冲区写满后 KataGo 会卡死。"""
        proc = self._proc
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                t = line.decode("utf-8", "replace").strip()
                if t:
                    self._stderr_tail.append(t)
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail.pop(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _read_stdout(self):
        proc = self._proc
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                t = line.decode("utf-8", "replace").strip()
                if not t:
                    continue
                try:
                    obj = json.loads(t)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                rid = str(obj.get("id", ""))
                fut = self._pending.pop(rid, None)
                if fut is not None and not fut.done():
                    if "error" in obj:
                        self._last_error = "%s (field=%s)" % (obj["error"], obj.get("field", ""))
                        print("[KataGo] " + self._last_error)
                    fut.set_result(obj)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._dead:
                self._fail("KataGo 输出读取失败：%s" % e)
            return
        # stdout 关闭 = 进程退出
        if not self._dead:
            self._dead = True
            self._last_error = "KataGo 进程已退出：" + self.stderr_tail()
            self._reject_all(self._last_error)

    # ================= 分析 =================

    async def analyze(self, moves) -> dict:
        if self._dead:
            return {"error": "KataGo 未运行：" + self._last_error}
        if self._proc is None or self._proc.stdin is None:
            return {"error": "KataGo 管道不可用"}

        self._seq += 1
        rid = "r%d" % self._seq
        req = {
            "id": rid,
            "moves": moves,
            "rules": "chinese",
            "komi": 7.5,
            "boardXSize": 19,
            "boardYSize": 19,
            "maxVisits": self.max_visits,
            "includeOwnership": True,       # 逐点归属（+ = 黑）：用于「双方实地估算」
        }
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[rid] = fut                     # 先登记再写，避免错过响应
        try:
            self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception as e:
            self._pending.pop(rid, None)
            self._fail("KataGo 管道写入失败（%s）" % e)
            if not fut.done():
                fut.cancel()
            return {"error": self._last_error}

        limit_ms = REQ_TIMEOUT_MS if self._ever_replied else STARTUP_TIMEOUT_MS
        try:
            result = await asyncio.wait_for(fut, timeout=limit_ms / 1000.0)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            self._last_error = "KataGo 分析超时（%d ms）" % limit_ms
            return {"error": self._last_error}
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise
        self._ever_replied = True
        return result
