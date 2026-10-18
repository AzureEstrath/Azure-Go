"""客户端事件契约与事件总线（对应前端 `NetClient.gd` 的 10 个 signal）。

后端事件驱动前端 signal（其中 `memory` 不对应 signal，前端只记一行日志；反向地，前端的
`net_status` 是本地连接状态、不由后端事件驱动）；分离后是「后端发事件 → WS 推给客户端」。
本模块把契约集中在一处，避免各处 magic string：

    message       ↔  message(kind, text)        对话/点评/落子宣告（kind 见下）
    state         ↔  state_changed(payload)     棋盘与手数权威快照
    info          ↔  info_changed(winrate, score)
    busy          ↔  busy_changed(busy)
    mood          ↔  mood_changed(payload)      三维情绪区域（表情/语气词/音高）
    joseki        ↔  joseki_event(payload)      定式高亮轨迹（kind/corner/points）+ 卡片载荷
    tool          ↔  tool_event(payload)        她自己查资料（start/end，只给状态栏看）
    token         ↔  token_event(turn_id, delta) 流式增量（可选、默认关闭；最终内容以 message 为准）
    speech        ↔  speech_event(payload)      一条台词对应的语音（音频 id 或系统语音指令）
    memory        ↔  （无信号）                  长期记忆的写入/召回元数据，前端只记一行日志

事件统一为 dict，``type`` 字段走对应构造器；客户端按 type 分发。
message.kind 取值与前端 message 信号一致：
    ai / ai_notice / ai_comment / ai_move / ai_idle / user / system

``ai_notice``（v3.9.0 修复轮 1）＝**推送方声明「这条没有语音」**：与 ``ai`` 同样是她的普通台词
（前端的气泡样式 / 打字节奏 / 流式收口 / 思考占位替换一律按同一套走），**区别只有一个**——
它**不发** ``speech``，所以前端**不许**为它登记语音配对记录（登记了就会占掉配对位、
把后面几条真语音整体错位）。主动约战提醒与看门狗可读提示用它；
其余「后端只发文字」的提示都是**同步方法**的返回，由前端调用点 ``pairing.mark_text_only()`` 兜住。

``ai_move`` 额外带 ``move`` 字段（{x, y, gtp, pass}）；客户端在落子宣告语音开始时
``POST /api/reveal`` 让后端把这一手推到棋盘（round-trip：语音开始才揭示那一手）。

``tool`` 只描述「她自己查了一次资料」的进度（``phase`` = start/end），**不是台词**：
前端只用它更新状态栏文案，工具返回的原始文本永远只回到模型手里。

``joseki``（计划 07 T6）在旧三键之外带出**卡片载荷**：``kind/corner/points`` 是原来那条高亮轨迹
（取值路径不变），``family/name/k/line/candidates/show_card/mistake`` 供定式教学卡片（前端
Task 8）绘制。新字段**全部有安全缺省**（缺字段 / 脏值不抛），``mistake`` 只在**真是走错**时非
``None``。它仍是**棋盘注记、不是台词**：不新增事件种类，也不进任何 LLM 上下文。

``speech`` 是**语音**（计划 05）：与对应的 ``message`` 一一配对（同 ``turn_id`` + 递增 ``seq``），
``mode == "openai"`` 时按 ``id`` 去 ``GET /api/tts/audio/{id}`` 取音频，``mode == "system"`` 时
用 ``text``（清洗后）交给 Windows 系统语音。**它不改 ``message`` 的形状**。

``memory`` 只描述一次长期记忆动作（``op`` = write/recall），同样是**元数据、不是台词**：
前端不产生任何界面信号，只做轻量日志；记忆内容绝不进台词。
"""

# —— message.kind ——
AI = "ai"                    # 一般台词（提示/分析/总结/虚着说明等）
AI_NOTICE = "ai_notice"      # **推送方声明「这条没有语音」**的普通台词（v3.9.0 修复轮 1 / C1）：
                             # 主动约战提醒、看门狗可读提示——它们**不发** ``speech``，前端据此
                             # **不登记语音配对记录**（否则会占掉配对位、把后面三条真语音整体错位）
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
    """定式高亮轨迹 + **卡片载荷**（计划 07 T6）。

    旧字段（``kind`` / ``corner`` / ``points``）仍走原来那条「``payload`` 原样并入」的路，
    取值路径一行不改；新增的卡片字段一律用 ``.get()`` 取并给**安全缺省**——缺字段、错类型、
    脏值都只是退化成缺省，**绝不抛**（缺字段的旧后端 / 新前端混跑时不崩）：

        family      ""      族 id（``star_33`` 一类；计数与召回都按它）
        name        ""      课名（卡片标题）
        k          0       角内已吻合的手数（透明前缀长度）
        line        []      显示线：``[{"x": int, "y": int, "c": str}]``，**原样透传**
        candidates  []      分岔候选，**只留** ``{"name", "next"}``（``next`` 是坐标对，不手工转 list）
        show_card   False   这一手该不该上教学卡片（T5 的入围结论）
        mistake     None    **仅当真是走错**时是 ``{"move_no", "x", "y", "drop"}``，否则 ``None``

    ``mistake`` 的**唯一过滤点就是 ``_joseki_mistake``**（``_joseki_emit`` 不判、原样透传）。它收
    两种形态：``data["jk_mistake"]`` 那种**带 ``is_mistake`` 的原始 dict**（只在它 ``is True`` 时
    才认），以及已收敛好的 ``{"move_no","x","y","drop"}``（无该键就直接认）——没亏的那一手一律
    ``None``（不许被标红，U31）；四个数值各自包在 ``try`` 里，脏值退化成 ``0`` / ``0.0``。
    ``payload`` 本身不是 dict 时按空 payload 处理。
    """
    p = payload if isinstance(payload, dict) else {}
    e = {"type": "joseki"}
    e.update(p)                                   # ← 旧字段：取值路径不变
    e["family"] = str(p.get("family") or "")
    e["name"] = str(p.get("name") or "")
    try:
        e["k"] = int(p.get("k", 0))
    except (TypeError, ValueError):
        e["k"] = 0
    line = p.get("line")
    e["line"] = line if isinstance(line, list) else []
    cands = p.get("candidates")
    e["candidates"] = cands if isinstance(cands, list) else []
    e["show_card"] = bool(p.get("show_card", False))
    e["mistake"] = _joseki_mistake(p.get("mistake"))
    return e


def _joseki_mistake(m):
    """把「走错那一手」收敛成卡片的注记，**不是走错就 None**（U31）。

    接受两种输入：``_joseki_emit`` 已过滤好的 ``{"move_no","x","y","drop"}``（无 ``is_mistake``
    键），以及 ``data["jk_mistake"]`` 那种带 ``is_mistake`` 的原始 dict。后者只在
    ``is_mistake is True`` 时才认——没亏的那一手不该被标红。
    """
    if not isinstance(m, dict):
        return None
    if "is_mistake" in m and m.get("is_mistake") is not True:
        return None

    def _num(v, cast, default):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    return {
        "move_no": _num(m.get("move_no"), int, 0),
        "x": _num(m.get("x"), int, 0),
        "y": _num(m.get("y"), int, 0),
        "drop": _num(m.get("drop"), float, 0.0),
    }


def tool(phase, name, args=None, ok=True, ms=0):
    """工具执行进度（只给状态栏用，不是台词）：start 在调用前、end 在返回后。

    ``args`` 只带模型自己给的那份参数（注入的引擎对象不可序列化，也不该外泄）；
    ``ok``/``ms`` 只在 ``end`` 上有意义，``start`` 固定 ``True``/``0``。
    """
    return {"type": "tool", "phase": str(phase), "name": str(name),
            "args": dict(args or {}), "ok": bool(ok), "ms": int(ms)}


def token(turn_id, delta):
    """流式输出的一段增量（``stream_tokens`` 开启时才有；默认关闭时一个都不发）。

    ``delta`` 只来自回答的 ``content`` 通道——``reasoning_content`` / ``response_metadata`` 之类的
    推理字段永不进这里。content 通道里的 think 段由 ``llm_chain.ThinkStream`` 跨块剥离：成对
    ``<think>…</think>`` 与 ``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`` 区间**保证不漏**（标记自身
    也不外发），畸形形态（网关只留收尾标记、未闭合 ``<think>``）下 token 流可能短暂带出推理原文，
    但**最终 ``message`` 与 TTS 恒为干净台词**（气泡由 ``message`` 整条收口）。

    **它不是台词**：气泡的最终内容一律以随后的 ``message`` 为准（去重闸门只对 ``message`` 生效），
    ``turn_id`` 用来把同一轮发言的增量与 ``message``、日后的 ``speech`` 配起来
    （每次公开编排调用生成一次）。
    """
    return {"type": "token", "turn_id": str(turn_id), "delta": str(delta)}


def speech(audio_id, mode, tone, pitch, turn_id, seq, text=""):
    """一条台词的语音（计划 05）：与对应的 ``message`` 同 ``turn_id``、按 ``seq`` 递增一一配对。

    ``mode`` 三态（与 :mod:`app.tts` 的返回值同源）：
        ``openai`` —— 音频已合成并在缓存里，``id`` 可经 ``GET /api/tts/audio/{id}`` 取回；
        ``system`` —— 合成不可用（未配置 / 超时 / 云端报错），``text`` 是清洗后可直接朗读的文本；
        ``none``   —— **不发这个事件**（语音关闭 / 没有可念的内容），故构造器只管前两种。

    ``tone`` 是 ``[[tone:sigh|cheer|proud]]`` 里剥出来的那一个语气标记（没有则为空串）；
    ``pitch`` 是当前情绪区域的音高偏移（与 ``mood`` 事件同源），由前端叠加到播放音高上。

    两条边界：**``message`` 的形状不受影响**（气泡仍整条由 ``message`` 收口，``turn_id`` 不进
    ``message``）；``seq`` 只在**同一** ``turn_id`` 内从 1 单调递增，跨回合会重置——前端必须按
    ``turn_id + seq`` 配对，不能只看 ``seq``。脏值不抛：``pitch`` / ``seq`` 非数字时退回 ``0``。
    """
    try:
        p = float(pitch)
    except (TypeError, ValueError):
        p = 0.0
    try:
        s = int(seq)
    except (TypeError, ValueError):
        s = 0
    return {"type": "speech", "id": str(audio_id), "mode": str(mode), "tone": str(tone),
            "pitch": p, "turn_id": str(turn_id), "seq": s, "text": str(text)}


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
