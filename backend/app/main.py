"""FastAPI 入口：REST（命令通道）+ WebSocket（事件通道）。

命令通道（REST）——客户端用这些触发后端动作，与 AzureAgent 的方法一一对应：
    POST /api/move        {x, y}            玩家落子（含点评与她应手）
    POST /api/hint                          方向性提示
    POST /api/analyze                       局面分析
    POST /api/chat        {text}            自由聊天
    POST /api/summarize   {note}            对局总结
    POST /api/undo        {turn}            俗手回档
    POST /api/reset                         重开一局
    POST /api/config      {allow_pass}      允许虚着开关（另见 limit_len / joseki_teach / tools_enabled）
    POST /api/reveal                        落子宣告语音开始（需要 round-trip 的客户端）
    POST /api/interaction {what}            玩家亲密互动（摸头等）
    POST /api/idle                          闲置搭话
    POST /api/mood/tick                     情绪心跳（向基线回落）
    POST /api/memory/query  {query, k?}     检索长期记忆（管理面；默认关闭时返回空列表）
    POST /api/memory/write  {tag?, text}    手动写一条长期记忆（关闭 / 空文本时被拒）
    GET  /api/memory/stats                  长期记忆体量（行数 / 归档数 / 向量数 / 库路径）
    POST /api/memory/forget {ids?}/{all}    忘掉指定或全部长期记忆
    GET  /api/state                         当前棋盘权威快照
    GET  /api/health                        引擎/LLM/长期记忆状态

事件通道（WebSocket /ws）——后端把 8 类事件推给客户端，契约见 events.py：
    message / state / info / busy / mood / joseki / tool / memory（连接建立时先发一次 hello 同步）。
    `memory` 由记忆管理端点推送，是元数据（不是台词），前端只记日志。

REST 应答统一返回 {"ok": bool, "state": <棋盘快照>}：命令是否被接受由 ok/busy 判断，
台词等内容一律经由 WS 事件送达（音画同步由客户端本地完成）。
"""

from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import config as config_mod
from . import coords, events, prompts
from .agent import AzureAgent
from .engine import KataGoEngine
from .llm import LLMClient
from .llm_chain import merge_extra_body
from .memory_service import MemoryService


def _int(v, d=-1):
    """把请求体字段安全转为 int；非法（缺省/非数字）时返回默认值，避免畸形请求打成 500。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def create_app(cfg=None, engine=None, llm=None, agent=None, autostart=True, memory=None):
    """构造应用；测试可注入 stub 引擎/LLM/agent/memory（autostart=False 跳过引擎启动）。

    ``memory`` 显式传入时优先于 lifespan 构造的 ``MemoryService``（测试 seam）：
    ``agent`` 分支也不再覆盖它。
    """
    cfg = cfg if cfg is not None else config_mod.AzureConfig.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prompts.configure(cfg)
        # 长期记忆：显式注入优先；默认关闭时零开销（不建库、不调接口）
        mem = memory if memory is not None else MemoryService(cfg)
        app.state.memory = mem
        if agent is None:
            eng = engine if engine is not None else KataGoEngine(cfg.katago_url, cfg.katago_token, cfg.max_visits, cfg.katago_timeout_ms)
            cli = llm if llm is not None else LLMClient(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key,
                                                         think_mode=cfg.llm_think_mode,
                                                         extra_body=merge_extra_body(cfg))
            app.state.agent = AzureAgent(eng, cli, events.EventBus(), joseki_teach=cfg.joseki_teach,
                                         tools_enabled=cfg.tools_enabled, max_tool_calls=cfg.max_tool_calls,
                                         memory=mem)
            if autostart:
                ok = await eng.start()
                if not ok:
                    print("[KataGo] 云服务不可用：" + eng.last_error())
        else:
            app.state.agent = agent
            app.state.agent.memory = mem         # 注入的 agent 也接上同一个实例（显式 memory= 优先）
        yield
        mem.close()                              # 退出时收尾（可重复调用）
        if autostart:
            await app.state.agent.katago.shutdown()
            await app.state.agent.llm.close()

    app = FastAPI(title="Azure 后端", lifespan=lifespan)

    def a():
        return app.state.agent

    def ok():
        s = a()
        return {"ok": True, "busy": s.busy, "state": s.st.payload()}

    def mem_store():
        """记忆库；未启用 / 未装配时为 ``None``（健康面与管理端点据此退化为空结果）。"""
        return getattr(getattr(app.state, "memory", None), "store", None)

    def memory_rows() -> int:
        """长期记忆行数：未启用 / 库不可用时为 0（健康面绝不上抛）。"""
        store = mem_store()
        if store is None:
            return 0
        try:
            return int(store.stats().get("rows", 0) or 0)
        except Exception:                       # noqa: BLE001
            return 0

    @app.get("/api/health")
    async def health():
        s = a()
        return {
            "ok": True,
            "katago_dead": s.katago.is_dead(),
            "katago_error": s.katago.last_error(),
            # 报的是**配置值**：留空时客户端会回退到 DEFAULT_URL（http://127.0.0.1:8100），
            # 故可能与引擎实例实际连的地址不同；这里不读引擎实例（测试注入的 stub 没有 .url）。
            "katago_url": cfg.katago_url,
            "llm_url": s.llm.base_url,
            "memory_rows": memory_rows(),
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
        if "tools_enabled" in body:             # 发言侧工具开关：开启后她才会在提示/落子宣告里查资料
            s.tools_enabled = bool(body["tools_enabled"])
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

    @app.post("/api/mood/tick")
    async def mood_tick():
        a().tick_mood()
        return ok()

    # ================= 长期记忆管理面 =================
    # 默认关闭（memory_enabled=false）时全部退化为可读的空结果：查询面可用、写入被拒。
    # 任何记忆故障（死库 / embedding 报错）都只打日志，绝不打成 500、也不影响对局。

    def mem_k(body: dict) -> int:
        """召回条数：显式 ``k`` 优先（≤0 ＝不召回），否则用配置的 ``memory_top_k``（原始值，0＝不召回）。

        不再把 0 兜底成 3：``memory_top_k=0`` 与出话路径的 ``recall_for`` 同一语义——
        记忆开着但不召回，管理面据此返回空列表。
        """
        raw = body.get("k")
        if raw is None:
            return max(0, int(getattr(cfg, "memory_top_k", 3) or 0))
        return max(0, _int(raw, 0))

    def mem_event(op, tag, text, score=0.0):
        """把一次记忆动作作为元数据推给客户端（不是台词；前端只记一行日志）。"""
        bus = getattr(a(), "bus", None)
        if bus is None:
            return
        try:
            bus.publish(events.memory(op, tag, text, score))
        except Exception as e:                  # noqa: BLE001 —— 推不出去不影响端点结果
            print("[Memory] 事件推送失败（已忽略）：%s" % e)

    @app.post("/api/memory/query")
    async def memory_query(body: dict):
        """检索长期记忆 → ``{"ok": True, "items": [{id,ts,kind,tag,text,score}]}``。

        召回走 ``asyncio.to_thread``：慢 embedding / 大库扫描不得冻住事件循环。
        """
        store = mem_store()
        items = []
        if store is not None:
            try:
                q = str(body.get("query", "") or "")
                items = list(await asyncio.to_thread(store.recall, q, mem_k(body)) or [])
            except Exception as e:              # noqa: BLE001
                print("[Memory] 查询失败（已忽略）：%s" % e)
                items = []
        for it in items:
            if isinstance(it, dict):
                mem_event("recall", it.get("tag", ""), it.get("text", ""), it.get("score", 0.0))
        return {"ok": True, "items": items}

    @app.post("/api/memory/write")
    async def memory_write(body: dict):
        """手动写一条长期记忆 → ``{"ok": bool, "id": int|null}``（关闭 / 空文本被拒）。

        写入（含 embedding 往返）同样走 ``asyncio.to_thread``。
        """
        raw = body.get("text")
        text = str(raw).strip() if isinstance(raw, str) else ""
        tag = str(body.get("tag", "") or "")
        store = mem_store()
        if store is None or text == "":
            return {"ok": False, "id": None}
        try:
            mem_id = await asyncio.to_thread(store.write, text, kind="fact", tag=tag)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 写入失败（已忽略）：%s" % e)
            mem_id = None
        if mem_id is None:
            return {"ok": False, "id": None}
        mem_event("write", tag, text, 0.0)
        return {"ok": True, "id": mem_id}

    @app.get("/api/memory/stats")
    async def memory_stats():
        """长期记忆体量 → ``{"ok": True, "rows", "games", "has_vectors", "db"}``。"""
        store = mem_store()
        st = {}
        if store is not None:
            try:
                st = dict(store.stats() or {})
            except Exception as e:              # noqa: BLE001
                print("[Memory] 统计失败（已忽略）：%s" % e)
                st = {}
        return {"ok": True,
                "rows": _int(st.get("rows"), 0),
                "games": _int(st.get("games"), 0),
                "has_vectors": _int(st.get("has_vectors"), 0),
                "db": str(st.get("db", "") or "")}

    @app.post("/api/memory/forget")
    async def memory_forget(body: dict):
        """忘掉长期记忆 → ``{"ok": True, "removed": int}``（``{ids:[…]}`` 或 ``{all:true}``）。"""
        store = mem_store()
        if store is None:
            return {"ok": True, "removed": 0}
        ids = body.get("ids")
        if isinstance(ids, list):
            ids = [i for i in (_int(x, -1) for x in ids) if i >= 0]
        else:
            ids = None                          # 非列表（含缺省）＝不按 id 删
        try:
            removed = int(store.forget(ids, all=bool(body.get("all", False))) or 0)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 删除失败（已忽略）：%s" % e)
            removed = 0
        return {"ok": True, "removed": removed}

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
    print("Azure 后端启动...（KataGo 走云服务，LLM 走云端）")
    uvicorn.run(app, host="127.0.0.1", port=8000)
