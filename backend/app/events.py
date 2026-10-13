"""客户端事件契约与事件总线（对应 AzureAgent.gd 的 6 个 signal）。

AzureAgent.gd 通过 6 个 signal 把结果推给界面；分离后改为「后端发事件 → WS 推给客户端」。
本模块把契约集中在一处，避免各处 magic string：

    message       ↔  message(kind, text)        对话/点评/落子宣告（kind 见下）
    state         ↔  state_changed(payload)     棋盘与手数权威快照
    info          ↔  info_changed(winrate, score)
    busy          ↔  busy_changed(busy)
    mood          ↔  mood_changed(payload)      三维情绪区域（表情/语气词/音高）
    joseki        ↔  joseki_event(payload)      定式高亮轨迹（kind/corner/points）

事件统一为 dict，``type`` 字段走对应构造器；客户端按 type 分发。
message.kind 取值与 AzureAgent 的 message.emit 完全一致：
    ai / ai_comment / ai_move / ai_idle / user / system

``ai_move`` 额外带 ``move`` 字段（{x, y, gtp, pass}）：语音开始播放时客户端据此本地揭示棋子，
不做 round-trip（与现有「落子宣告语音开始时才 reveal」一致）。
"""

# —— message.kind ——
AI = "ai"                    # 一般台词（提示/分析/总结/虚着说明等）
AI_COMMENT = "ai_comment"    # 重要点评（导演层据此加犹豫节拍）
AI_MOVE = "ai_move"          # 落子宣告（带坐标；语音开始时揭示）
AI_IDLE = "ai_idle"          # 闲置/窥屏搭话（玩家一回来可整句作废）
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


def hello(payload, mood_payload):
    """WS 连接建立时的初始同步：棋盘状态 + 当前情绪，保证客户端首屏一致。"""
    return {"type": "hello", "state": payload, "mood": mood_payload}


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
