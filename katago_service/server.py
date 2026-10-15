"""KataGo 云服务的 HTTP 面：``GET /health`` 与 ``POST /analyze``。

服务自己持有状态机（P10）：lifespan 先置 ``warming``，``start()`` 之后**立刻返回**并把
``warmup()`` 放到后台任务（P12 —— uvicorn 要等 lifespan 结束才绑端口，预热若卡在 lifespan 里，
整个预热窗口端口都不监听），预热结束再置 ``ready``/``dead``。**绝不能**把「就绪」寄托在
``proc.state_name() == "ready"`` 上：引擎若对所有请求都回错误应答，``state_name()`` 会永远停在
``warming``，按它判就绪就成了死锁（warming → /analyze 503 → 永无成功分析 → 永远 warming）。

``/analyze`` 的请求体与响应体都是 KataGo analysis 引擎原文，服务不做字段解释，
只负责鉴权、串行化与「未就绪/已死」的可读报错（见 docs/plans/01-katago-cloud-service.md）。
"""

import asyncio
import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .proc import KatagoProcess

STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_DEAD = "dead"

# 服务侧引擎请求超时由 proc.REQ_TIMEOUT_MS 决定；这里不设第二套超时，
# 避免两层超时互相掩盖（客户端超时与重试在 backend/app/engine.py）。


def _cfg_get(cfg, key, default=None):
    """读配置项：同时兼容映射与 ``argparse.Namespace`` 两种形态。"""
    if cfg is None:
        return default
    if hasattr(cfg, "get"):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _has_lifecycle(proc) -> bool:
    """引擎是否由本服务负责启动与预热。

    真引擎（``KatagoProcess``）有 ``start``/``warmup``，由服务驱动；
    假引擎（测试）只提供 ``analyze``/``state_name``/``is_dead``/``last_error``，
    状态以它自报的为准，不要求它多出任何方法。
    """
    return callable(getattr(proc, "start", None)) or callable(getattr(proc, "warmup", None))


def _last_error(proc) -> str:
    get = getattr(proc, "last_error", None)
    return get() if callable(get) else ""


def _is_dead(proc) -> bool:
    get = getattr(proc, "is_dead", None)
    return bool(get()) if callable(get) else False


def _current_state(app) -> str:
    """当前服务状态：``warming`` / ``ready`` / ``dead``。

    进程已死一律 ``dead``（无论服务自己走到哪一步）；
    自建引擎以服务自己的状态机为准（P10）；注入的假引擎自报状态优先。
    """
    proc = app.state.proc
    if _is_dead(proc):
        return STATE_DEAD
    if app.state.driven:
        return app.state.state
    name = getattr(proc, "state_name", None)
    if callable(name):
        reported = name()
        if reported in (STATE_WARMING, STATE_READY, STATE_DEAD):
            return reported
    return app.state.state


def _not_ready_error(app, state) -> str:
    """未就绪时的可读报错：客户端每手都在等，必须立刻给出人能看懂的原因。"""
    if state == STATE_DEAD:
        return "KataGo 引擎不可用：%s" % (_last_error(app.state.proc) or "未知原因")
    return "KataGo 引擎正在预热，请稍后重试"


def _build_proc(cfg) -> KatagoProcess:
    """按配置构造真引擎（生产路径；测试注入假引擎时不会走到这里）。"""
    if cfg is None:
        raise ValueError("create_app 需要 proc 或 cfg 之一：proc 注入引擎，cfg 提供引擎路径")
    missing = [k for k in ("exe", "model", "config") if not _cfg_get(cfg, k)]
    if missing:
        raise ValueError("create_app 的 cfg 缺少引擎路径：%s" % ", ".join(missing))
    return KatagoProcess(
        exe=_cfg_get(cfg, "exe"),
        model=_cfg_get(cfg, "model"),
        cfg=_cfg_get(cfg, "config"),
        analysis_threads=_cfg_get(cfg, "analysis_threads", 1),
        log_dir=_cfg_get(cfg, "log_dir"),
        max_visits=_cfg_get(cfg, "max_visits", 500),
    )


async def _warmup_then_ready(app: FastAPI, proc) -> None:
    """后台预热，结束后落状态（P12）。

    预热超时/报错但进程还活着，仍必须是 ``ready``（P10）：引擎确实起来了，
    真实请求它会应答（首次分析走 ``proc`` 里的长超时档），不能因为预热失败就永久 503。
    """
    try:
        warm = getattr(proc, "warmup", None)
        if callable(warm):
            await warm()
    except asyncio.CancelledError:
        raise                           # 关闭时被取消：状态交给 shutdown，不在这里改
    except Exception as e:
        # 预热失败本身不判死：下面按 is_dead() 决定（P10 —— 进程活着就仍是 ready）
        print("[KataGo服务] 预热异常：%s" % e)
    app.state.state = STATE_DEAD if _is_dead(proc) else STATE_READY


async def _stop_warmup(app: FastAPI) -> None:
    """关闭前收掉预热后台任务：不留孤儿任务，也不出现「Task was destroyed but it is pending」。"""
    task = app.state.warm_task
    app.state.warm_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("[KataGo服务] 预热任务异常退出：%s" % e)


async def _shutdown(proc) -> None:
    stop = getattr(proc, "shutdown", None)
    if not callable(stop):
        return
    try:
        await stop()
    except Exception as e:
        print("[KataGo服务] 关闭引擎出错：%s" % e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.state = STATE_WARMING
    proc = app.state.proc
    app.state.driven = _has_lifecycle(proc)
    if app.state.driven:
        start = getattr(proc, "start", None)
        if callable(start):
            await start()               # 很快：只拉起进程，不等它预热完
        if _is_dead(proc):
            app.state.state = STATE_DEAD
        else:
            # P12：预热放后台，lifespan 立刻返回。uvicorn 是 lifespan 结束后才绑端口的，
            # 若在这里 await warmup()，整个预热窗口端口都不监听 —— 客户端只会拿到
            # 连接被拒（连 /health 的 warming 都看不到），后端 180s 的就绪等待也没了意义。
            app.state.warm_task = asyncio.create_task(_warmup_then_ready(app, proc))
    try:
        yield
    finally:
        await _stop_warmup(app)
        await _shutdown(proc)


def create_app(proc=None, token="", cfg=None) -> FastAPI:
    """构造服务应用。

    proc:  注入的引擎。生产由调用方构造真引擎，测试注入假引擎；为 None 时按 cfg 构造。
    token: Bearer 令牌；空串＝不校验（本机回环调试）。``/health`` 永不鉴权。
    cfg:   引擎路径配置（映射或 argparse.Namespace，含 exe/model/config，
           可选 analysis_threads/log_dir/max_visits）；与 proc 二选一。
    """
    if proc is None:
        proc = _build_proc(cfg)
    app = FastAPI(title="KataGo 云服务", lifespan=_lifespan)
    app.state.proc = proc
    app.state.token = token or ""
    app.state.state = STATE_WARMING
    app.state.driven = False
    app.state.warm_task = None          # P12 的后台预热任务
    app.state.started_at = time.monotonic()
    # 单用户场景：同一引擎不做并发分析，靠锁串行化，避免引擎侧争抢
    app.state.lock = asyncio.Lock()

    def _authorized(request: Request) -> bool:
        if not app.state.token:
            return True                 # 未配置 token：允许直连（仅限本机回环）
        got = request.headers.get("authorization", "")
        # 按字节比较：compare_digest 遇到非 ASCII 的 str 会抛 TypeError（token 可能含中文）
        return hmac.compare_digest(got.encode("utf-8"), ("Bearer " + app.state.token).encode("utf-8"))

    @app.get("/health")
    async def health():
        state = _current_state(app)
        return {
            "ok": state == STATE_READY,
            "state": state,
            "katago_error": _last_error(app.state.proc),
            "uptime_s": round(time.monotonic() - app.state.started_at, 3),
        }

    @app.post("/analyze")
    async def analyze(request: Request):
        if not _authorized(request):
            return JSONResponse(status_code=401, content={"error": "鉴权失败：缺少或错误的 Bearer 令牌"})
        state = _current_state(app)
        if state != STATE_READY:
            # 预热中/进程已死都必须立刻回可读错误，不能挂到客户端超时
            return JSONResponse(status_code=503, content={"error": _not_ready_error(app, state)})
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "请求体不是合法 JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象"})
        async with app.state.lock:
            try:
                result = await app.state.proc.analyze(body)     # 请求体原样转发
            except Exception as e:
                return JSONResponse(status_code=503, content={"error": "分析失败：%s" % e})
        return JSONResponse(content=result)

    return app
