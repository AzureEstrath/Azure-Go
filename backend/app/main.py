"""FastAPI 入口：REST（命令通道）+ WebSocket（事件通道）。

命令通道（REST）——客户端用这些触发后端动作，与 AzureAgent 的方法一一对应：
    POST /api/move        {x, y}            玩家落子（含点评与她应手）
    POST /api/hint                          方向性提示
    POST /api/analyze                       局面分析
    POST /api/chat        {text}            自由聊天
    POST /api/summarize   {note}            对局总结
    POST /api/undo        {turn}            俗手回档
    POST /api/reset                         重开一局
    POST /api/config      {allow_pass}      允许虚着开关
    POST /api/reveal                        落子宣告语音开始（需要 round-trip 的客户端）
    POST /api/interaction {what}            玩家亲密互动（摸头等）
    POST /api/idle                          闲置搭话
    POST /api/peer        {fg_title, ocr, vlm}  窥屏搭话
    POST /api/mood/tick                     情绪心跳（向基线回落）
    GET  /api/state                         当前棋盘权威快照
    GET  /api/health                        引擎/LLM 状态

事件通道（WebSocket /ws）——后端把 6 类事件推给客户端，契约见 events.py：
    message / state / info / busy / mood / joseki（连接建立时先发一次 hello 同步）。

REST 应答统一返回 {"ok": bool, "state": <棋盘快照>}：命令是否被接受由 ok/busy 判断，
台词等内容一律经由 WS 事件送达（音画同步由客户端本地完成）。
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import config as config_mod
from . import coords, events, prompts
from .agent import AzureAgent
from .engine import KataGoEngine
from .llm import LLMClient


def _int(v, d=-1):
    """把请求体字段安全转为 int；非法（缺省/非数字）时返回默认值，避免畸形请求打成 500。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def create_app(cfg=None, engine=None, llm=None, agent=None, autostart=True):
    """构造应用；测试可注入 stub 引擎/LLM/agent（autostart=False 跳过子进程启动）。"""
    cfg = cfg if cfg is not None else config_mod.AzureConfig.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prompts.configure(cfg)
        if agent is None:
            eng = engine if engine is not None else KataGoEngine(cfg.katago_bin, cfg.katago_model, cfg.katago_config, cfg.max_visits)
            cli = llm if llm is not None else LLMClient(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key, think_mode=cfg.llm_think_mode)
            app.state.agent = AzureAgent(eng, cli, events.EventBus(), joseki_teach=cfg.joseki_teach)
            if autostart:
                ok = await eng.start()
                if not ok:
                    print("[KataGo] 启动失败：" + eng.last_error())
                asyncio.create_task(cli.probe_models())
        else:
            app.state.agent = agent
        yield
        if autostart:
            await app.state.agent.katago.shutdown()
            await app.state.agent.llm.close()

    app = FastAPI(title="Azure 后端", lifespan=lifespan)

    def a():
        return app.state.agent

    def ok():
        s = a()
        return {"ok": True, "busy": s.busy, "state": s.st.payload()}

    @app.get("/api/health")
    async def health():
        s = a()
        return {
            "ok": True,
            "katago_dead": s.katago.is_dead(),
            "katago_error": s.katago.last_error(),
            "llm_url": s.llm.base_url,
        }

    @app.get("/api/state")
    async def get_state():
        return a().st.payload()

    @app.post("/api/move")
    async def move(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        x = _int(body.get("x"), -1)
        y = _int(body.get("y"), -1)
        if not (0 <= x < coords.SIZE and 0 <= y < coords.SIZE):
            return {"ok": False, "busy": s.busy, "state": s.st.payload()}
        await s.player_move(x, y)
        return ok()

    @app.post("/api/hint")
    async def hint():
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.hint()
        return ok()

    @app.post("/api/analyze")
    async def analyze():
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.analyze_position()
        return ok()

    @app.post("/api/chat")
    async def chat(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.chat(str(body.get("text", "")))
        return ok()

    @app.post("/api/summarize")
    async def summarize(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.summarize(str(body.get("note", "")))
        return ok()

    @app.post("/api/undo")
    async def undo(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        s.undo(_int(body.get("turn"), -1))
        return ok()

    @app.post("/api/reset")
    async def reset():
        a().reset_game()
        return ok()

    @app.post("/api/config")
    async def set_config(body: dict):
        s = a()
        if "allow_pass" in body:
            s.set_allow_pass(bool(body["allow_pass"]))
        if "limit_len" in body:                 # 客户端「短回复」开关：转发到后端编排
            s.limit_len = bool(body["limit_len"])
        if "joseki_teach" in body:              # 客户端「定式导学」开关：转发到后端编排
            s.joseki_teach = bool(body["joseki_teach"])
        return ok()

    @app.post("/api/reveal")
    async def reveal():
        a().reveal_move()
        return ok()

    @app.post("/api/interaction")
    async def interaction(body: dict):
        a().player_interaction(str(body.get("what", "")))
        return ok()

    @app.post("/api/idle")
    async def idle():
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.idle_remark()
        return ok()

    @app.post("/api/peer")
    async def peer(body: dict):
        s = a()
        if s.busy:
            return {"ok": False, "busy": True, "state": s.st.payload()}
        await s.peer_remark({"fg_title": body.get("fg_title", ""), "ocr": body.get("ocr", ""), "vlm": body.get("vlm", "")})
        return ok()

    @app.post("/api/mood/tick")
    async def mood_tick():
        a().tick_mood()
        return ok()

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        s = a()
        q = s.bus.subscribe()
        try:
            await sock.send_json(events.hello(s.st.payload(), s.mood_payload(), s.busy))
            while True:
                event = await q.get()
                await sock.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print("[WS] 连接异常：%s" % e)
        finally:
            s.bus.unsubscribe(q)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    print("Azure 后端启动...（KataGo 由本进程拉起，LLM 走云端）")
    uvicorn.run(app, host="127.0.0.1", port=8000)
