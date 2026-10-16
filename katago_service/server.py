"""KataGo 云服务的 HTTP 面：``GET /health`` 与 ``POST /analyze``。

服务自己持有状态机（P10）：lifespan 先置 ``warming``，``start()`` 之后**立刻返回**并把
``warmup()`` 放到后台任务（P12 —— uvicorn 要等 lifespan 结束才绑端口，预热若卡在 lifespan 里，
整个预热窗口端口都不监听），预热结束再置 ``ready``/``dead``。**绝不能**把「就绪」寄托在
``proc.state_name() == "ready"`` 上：引擎若对所有请求都回错误应答，``state_name()`` 会永远停在
``warming``，按它判就绪就成了死锁（warming → /analyze 503 → 永无成功分析 → 永远 warming）。

``/analyze`` 的请求体与响应体都是 KataGo analysis 引擎原文，服务不做字段解释，
只负责鉴权、串行化与「未就绪/已死」的可读报错（见 docs/plans/01-katago-cloud-service.md）。

入口：``python -m katago_service.server --port 8100 ...``（必须在**仓库根目录**执行，
``katago_service`` 是包，父目录得在 sys.path 上；``run_server.bat`` 已处理）。
"""

import argparse
import asyncio
import hmac
import ipaddress
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .proc import KatagoProcess

STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_DEAD = "dead"

# 默认只监听回环：不带 token 的服务绝不能默认对外监听（跨机部署要显式 --host 0.0.0.0）
DEFAULT_HOST = "127.0.0.1"

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


def parse_args(argv=None):
    """解析命令行参数（``python -m katago_service.server``）。

    监听地址、引擎路径与令牌都可以省略：留空时回落到环境变量 ``KATAGO_HOST`` /
    ``KATAGO_EXE`` / ``KATAGO_MODEL`` / ``KATAGO_CONFIG`` / ``KATAGO_SERVICE_TOKEN``
    （见 ``resolve_host`` 与 ``build_app``）。
    """
    parser = argparse.ArgumentParser(
        prog="python -m katago_service.server",
        description="KataGo 云服务：常驻 analysis 引擎，对外提供 /health 与 /analyze",
    )
    parser.add_argument("--host", default=None,
                        help="监听地址（缺省时取环境变量 KATAGO_HOST，仍为空则 %s 仅回环）；"
                             "对外监听（如 0.0.0.0）时务必配置 token 并置于内网/TLS 反代之后"
                             % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=8100, help="监听端口（默认 8100）")
    parser.add_argument("--token", default="", help="Bearer 令牌；留空＝不校验（仅限本机回环调试）")
    parser.add_argument("--katago-exe", dest="katago_exe", default="", help="katago 可执行文件路径")
    parser.add_argument("--model", default="", help="KataGo 模型文件路径")
    parser.add_argument("--config", default="", help="KataGo analysis 配置文件路径")
    parser.add_argument("--analysis-threads", dest="analysis_threads", type=int, default=1,
                        help="引擎分析线程数（默认 1）")
    return parser.parse_args(argv)


def resolve_host(args) -> str:
    """监听地址：优先级 ``--host`` → 环境变量 ``KATAGO_HOST`` → 只监听回环（安全默认）。

    ``--host`` 的默认值必须是**空**，否则 ``args.host or ...`` 永远不会走到环境变量，
    ``KATAGO_HOST=0.0.0.0 python -m katago_service.server`` 就会静默只绑回环（P16-F1）。
    """
    return args.host or os.environ.get("KATAGO_HOST", "") or DEFAULT_HOST


def resolve_token(args) -> str:
    """Bearer 令牌：优先级 ``--token`` → 环境变量 ``KATAGO_SERVICE_TOKEN`` → 空（不校验）。"""
    return args.token or os.environ.get("KATAGO_SERVICE_TOKEN", "")


def build_app(args) -> FastAPI:
    """按命令行参数（含环境变量兜底）装配服务应用。

    参数名与 ``_build_proc`` 读的键名并不一致（``--katago-exe`` 的 dest 是 ``katago_exe``，
    引擎读的键却是 ``exe``），所以这里**显式映射**——直接把 argparse 的 namespace 当 cfg
    传进 ``create_app`` 会在启动时抛「cfg 缺少引擎路径：exe」。

    空字符串一律按「没传」处理：``run_server.bat`` 会把可能为空的环境变量直接当参数传进来。
    """
    cfg = {
        "exe": args.katago_exe or os.environ.get("KATAGO_EXE", ""),
        "model": args.model or os.environ.get("KATAGO_MODEL", ""),
        "config": args.config or os.environ.get("KATAGO_CONFIG", ""),
        "analysis_threads": args.analysis_threads,
    }
    return create_app(token=resolve_token(args), cfg=cfg)


def _is_loopback(host: str) -> bool:
    """是否只在本机可达的监听地址。

    用 ``ipaddress`` 解析而不是比对字面量：``127.0.0.2``、``[::1]`` 等回环变体也算回环，
    免得误报「对外监听」；非 IP 的主机名（如 ``localhost``）回落到字面量判断。
    """
    h = (host or "").strip().strip("[]")        # IPv6 监听地址可能写作 [::1]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return h.lower() == "localhost"


if __name__ == "__main__":
    import uvicorn

    _args = parse_args()
    _host = resolve_host(_args)
    _token = resolve_token(_args)
    # 默认只监听回环（与 backend/app/main.py 一致）；跨机部署用 --host/KATAGO_HOST 对外监听
    print("[KataGo服务] 启动 http://%s:%d（analysis-threads=%d）"
          % (_host, _args.port, _args.analysis_threads))
    try:
        _app = build_app(_args)
    except ValueError as e:
        # 完全没给引擎配置：给可读中文提示而不是裸 traceback（路径给错但非空的情况会优雅降级为 dead/503）
        print("[KataGo服务] 启动失败：%s" % e)
        print("[KataGo服务] 请用 --katago-exe/--model/--config 或环境变量 "
              "KATAGO_EXE/KATAGO_MODEL/KATAGO_CONFIG 指定引擎")
        raise SystemExit(2)
    if not _is_loopback(_host) and not _token:
        # 静态文档里写了不算数：这里必须真喊一声，否则「对外监听 + 无 token」会悄无声息。
        # 放在 build_app 成功之后：配置缺失时只报启动失败，不再先喊一句无关的暴露告警。
        print("[KataGo服务] 警告：正在对外监听（%s）且未配置 token，请务必配置 "
              "KATAGO_SERVICE_TOKEN 或置于内网/TLS 反代之后" % _host)
    uvicorn.run(_app, host=_host, port=_args.port)
