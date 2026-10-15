"""客户端事件契约与事件总线（对应前端 `NetClient.gd` 的 7 个 signal）。

前端的 7 个 signal 由后端事件驱动；分离后是「后端发事件 → WS 推给客户端」。
本模块把契约集中在一处，避免各处 magic string：

    message       ↔  message(kind, text)        对话/点评/落子宣告（kind 见下）
    state         ↔  state_changed(payload)     棋盘与手数权威快照
    info          ↔  info_changed(winrate, score)
    busy          ↔  busy_changed(busy)
    mood          ↔  mood_changed(payload)      三维情绪区域（表情/语气词/音高）
    joseki        ↔  joseki_event(payload)      定式高亮轨迹（kind/corner/points）
    tool          ↔  tool_event(payload)        她自己查资料（start/end，只给状态栏看）
    token         ↔  token_event(turn_id, delta) 流式增量（可选、默认关闭；最终内容以 message 为准）
    memory        ↔  （无信号）                  长期记忆的写入/召回元数据，前端只记一行日志

事件统一为 dict，``type`` 字段走对应构造器；客户端按 type 分发。
message.kind 取值与前端 message 信号一致：
    ai / ai_comment / ai_move / ai_idle / user / system

``ai_move`` 额外带 ``move`` 字段（{x, y, gtp, pass}）；客户端在落子宣告语音开始时
``POST /api/reveal`` 让后端把这一手推到棋盘（round-trip：语音开始才揭示那一手）。

``tool`` 只描述「她自己查了一次资料」的进度（``phase`` = start/end），**不是台词**：
前端只用它更新状态栏文案，工具返回的原始文本永远只回到模型手里。

``memory`` 只描述一次长期记忆动作（``op`` = write/recall），同样是**元数据、不是台词**：
前端不产生任何界面信号，只做轻量日志；记忆内容绝不进台词。
"""

# —— message.kind ——
AI = "ai"                    # 一般台词（提示/分析/总结/虚着说明等）
AI_COMMENT = "ai_comment"    # 重要点评（导演层据此加犹豫节拍）
AI_MOVE = "ai_move"          # 落子宣告（带坐标；语音开始时揭示）
AI_IDLE = "ai_idle"          # 闲置搭话（玩家一回来可整句作废）
USER = "user"
SYSTEM = "system"


def message(kind, text, move=None):
    e = {"type": "message", "kind": kind, "text": text}
    if move is not None:
        e["move"] = move
    return e


def state(payload):
    e = {"type": "state"}
    e.update(payload)
    return e


def info(winrate, score):
    return {"type": "info", "winrate": winrate, "score": score}


def busy(is_busy):
    return {"type": "busy", "busy": is_busy}


def mood(payload):
    e = {"type": "mood"}
    e.update(payload)
    return e


def joseki(payload):
    e = {"type": "joseki"}
    e.update(payload)
    return e


def tool(phase, name, args=None, ok=True, ms=0):
    """工具执行进度（只给状态栏用，不是台词）：start 在调用前、end 在返回后。

    ``args`` 只带模型自己给的那份参数（注入的引擎对象不可序列化，也不该外泄）；
    ``ok``/``ms`` 只在 ``end`` 上有意义，``start`` 固定 ``True``/``0``。
    """
    return {"type": "tool", "phase": str(phase), "name": str(name),
            "args": dict(args or {}), "ok": bool(ok), "ms": int(ms)}


def token(turn_id, delta):
    """流式输出的一段增量（``stream_tokens`` 开启时才有；默认关闭时一个都不发）。

    ``delta`` 只来自回答的 ``content`` 通道——推理段（``reasoning_content`` / think 段）永不进这里
    （硬规则 3）；空内容的块不发事件。**它不是台词**：气泡的最终内容一律以随后的 ``message``
    为准（去重闸门只对 ``message`` 生效），``turn_id`` 用来把同一轮发言的增量与 ``message``、
    日后的 ``speech`` 配起来（每次公开编排调用生成一次）。
    """
    return {"type": "token", "turn_id": str(turn_id), "delta": str(delta)}


def memory(op, tag, text, score=0.0):
    """长期记忆的写入 / 召回（**元数据、不是台词**，前端只记一行日志）。

    ``op`` 取 ``write`` / ``recall``；``score`` 只在召回时有意义（关键词降级固定 0.0）。
    ``score`` 不是数字时按 0.0 处理——这个构造器不该因为脏值把端点打成 500。
    """
    try:
        sc = float(score)
    except (TypeError, ValueError):
        sc = 0.0
    return {"type": "memory", "op": str(op), "tag": str(tag), "text": str(text), "score": sc}


def hello(payload, mood_payload, busy):
    """WS 连接建立时的初始同步：棋盘状态 + 当前情绪 + 忙闲，保证客户端首屏一致。"""
    return {"type": "hello", "state": payload, "mood": mood_payload, "busy": bool(busy)}


class EventBus:
    """异步事件总线：agent 只管 publish，WS 连接各持一个订阅队列消费。

    单用户桌面场景，订阅者通常只有 1 个；队列满时丢弃最旧事件，避免拖慢对局。
    """

    def __init__(self, maxsize=256):
        self._queues = []
        self._maxsize = maxsize

    def publish(self, event):
        for q in list(self._queues):
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self):
        import asyncio
        q = asyncio.Queue(maxsize=self._maxsize)
        self._queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self._queues:
            self._queues.remove(q)


class RecordingBus:
    """测试用：把事件记进列表，可在断言后清空。"""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def clear(self):
        self.events = []

    def of_type(self, t):
        return [e for e in self.events if e.get("type") == t]

    def messages(self, kind=None):
        out = self.of_type("message")
        if kind is not None:
            out = [e for e in out if e.get("kind") == kind]
        return out
