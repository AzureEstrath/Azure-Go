"""对局编排（后端权威：对局、聊天、闲置搭话、情绪与定式都在这一层）。

职责：玩家落子 → Azure 点评 → Azure 思考并落子；提示/分析/聊天/闲置/总结/悔棋。
结果不直接返回给界面，而是交给 EventBus（事件契约见 ``events.py``），由 WS 推给客户端。

音画同步：``ai_move`` 事件带 ``move`` 坐标，客户端等她「落子宣告」的语音开始播放时本地揭示棋子；
后端把这一手**已落到内部棋谱**、可见更新延后（``reveal_move()`` 供需要 round-trip 的客户端调用）。
"""

import asyncio
import random
import re
import time
import uuid

from langchain_core.messages import ToolMessage

from . import coords, events, prompt_chains, prompts
from .agent_graph import (
    PHASE_AFTER_PLAYER, PHASE_ANALYZE, PHASE_CHAT, PHASE_IDLE, PHASE_JUST_MOVED,
    PHASE_OBSERVE, PHASE_OWN_TURN, PHASE_SUMMARY, build_graph, is_joseki_mistake, _emit_line,
)
from .board import GoBoard, SIZE as BOARD_SIZE
from .emotion import EmotionModel
from .joseki import JosekiRecognizer
from .llm_chain import chunk_text_of, content_of
from .state import GameState
from .tools import TOOLS, failed as tool_failed

# LLM「情绪变化」小调用：限频 + 忙时跳过，保护主对话的响应速度与 KV 前缀缓存
MOOD_LLM_GAP_MS = 15000
MOOD_LLM_MAX_TOKENS = 32

# 限制回复长度时追加的约束（在不动原人设的前提下，单独附在末尾）
LEN_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
            "- 点评 / 提示：只回 1 句，尽量 20 字以内\n"
            "- 普通聊天：最多 2 句，尽量 40 字以内\n"
            "- 不复述局面、不客套，直接说结论就好")

# 「短回复」模式下，定式讲解用的替代约束：讲解要讲清要点，允许 2~3 句，不被一句式限制压扁
LEN_TEACH_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
                  "- 这是定式讲解：把名称与要点讲清楚，2~3 句即可，别长篇大论\n"
                  "- 不复述局面、不客套，直接开讲就好")

# —— 发言侧工具（她自己决定要不要查资料）——
# 计划 02 的规则是「工具只影响怎么说、不影响下哪里」；计划 04 有意放宽（S21）：工具/模型在预算内
# 报出的落点可以成为候选，但必须过合法性边界（见 ``_resolve_ai_move``），默认仍回退
# ``_pick_ai_move``。``tools_enabled`` 默认 false，影响面有界。
# 工具总次数硬上限：一次发言最多两次，防止「一直查、不回答」
MAX_TOOL_CALLS = 2
# 单工具超时（秒）：超时即返回超时文本，不阻塞回合
TOOL_TIMEOUT_SEC = 10
# 一次 tool loop 的模型往返硬上限：工具预算之外，还允许若干轮「别再调工具、直接回答」的催促；
# 到顶就按现有内容收尾（拿不到正文时由调用方的兜底台词顶上），绝不允许无限往返
MAX_TOOL_ROUNDS = 12
# 参数里带棋谱的工具：模型没给棋谱时由后端注入当前局面
MOVE_TOOLS = ("katago_analyze", "board_query")
# 预算用尽 / 工具超时后给模型的追加指令（作为 ToolMessage 内容，不是台词）
TOOL_BUDGET_TEXT = "工具调用次数已达上限（%d 次），这次不再执行；请不要再调用任何工具，直接用自然语言回答。"
TOOL_TIMEOUT_TEXT = "工具执行超时（%d 秒），这次没取到结果；别再重试同一个工具，直接回答。"

# 括号里带「着棋点位」的舞台提示不需要显示，例如（白棋落在 C4）/（第7手 Q16）/（C4）
_PAREN_GROUP_PATTERN = "[（(][^（()）\\n]{0,40}[）)]"
_MOVE_HINT_PATTERN = "[A-HJ-Ta-hj-t]\\s?\\d{1,2}|落子|落在|虚着|pass|着子|第\\s?\\d+\\s?手"

# 从模型原话里认落点候选用的 GTP 坐标（大写列 A..T 跳过 I + 1~2 位行号）。
# 只认大写、且前后不能再粘字母数字，免得把 "Top3"、"a1" 这类普通词误认成坐标；
# 合法性一概交给 ``_resolve_ai_move``（所以误认也只会退回落点默认策略）。
_MOVE_TOKEN_PATTERN = r"(?<![A-Za-z0-9])[A-HJ-T]\d{1,2}(?!\d)"
_PASS_WORD_PATTERN = r"pass|虚着|不落子|弃权"

# AI 原话去重窗口（原在 ``Main.gd``：``AI_DEDUP_WINDOW`` / ``_recent_ai_texts``，本轮下沉到后端）。
# 最近 N 条 AI 原话（不含 ``ai_move`` 落子宣告）里 ``strip()`` 后一字不差即判重复；前端保留既有判据，
# 只兜「WS 重连后乱序」这一种后端看不到的情形。
AI_DEDUP_WINDOW = 8

# 闲置搭话兜底台词
IDLE_LINES = [
    "唔…还在吗？我在这里等你哦。",
    "在想什么呢？要不要再来一手？",
    "嗯…安静得都能听到风了呢。",
    "要不要聊聊天？我有点无聊了~",
]

# LLM 不可用（断网 / 401 / 429 / 超时 / 根本没配地址）时的兜底台词：
# 「棋局类」用于点评、提示、分析、落子宣告；「闲聊类」用于聊天与总结。
# 口吻与 IDLE_LINES 一致；多备几句随机挑，配合客户端的防复读窗口。
GAME_FALLBACK_LINES = [
    "唔…我脑子有点转不过来，这一手先不细说了。",
    "嗯…这盘棋我一时看不透，先接着下吧。",
    "唔…刚才那手我还没想明白，等会儿再跟你说。",
    "嗯…思路有点断了，先陪你摆着，一会儿再讲。",
]
CHAT_FALLBACK_LINES = [
    "唔…我脑子有点转不过来，这次就先说到这儿好不好？",
    "嗯…我现在有点迷糊，回头再好好陪你聊。",
    "唔…思路断了，等会儿再慢慢跟你说。",
]


class AzureAgent:
    def __init__(self, engine, llm, bus=None, joseki_teach=True, limit_len=False,
                 tools_enabled=False, max_tool_calls=None, memory=None, stream_tokens=False,
                 tts=None, joseki_progress=None):
        self.st = GameState()
        self.katago = engine
        self.llm = llm
        self.bus = bus if bus is not None else events.EventBus()
        self.mood = EmotionModel()

        self.joseki_teach = joseki_teach
        self.limit_len = limit_len
        self.busy = False

        # —— 复盘模式（v3.7.0）：后端的**独立模式**（可进可出、不碰 `st`、不碰 `busy`）——
        #    `review` 期间她不能开口：落子/聊天/闲置/提示/分析在 main.py 侧一律可读拒绝，
        #    情绪心跳与语气词在这里冻结（tick_mood / player_interaction 直接返回，V14）。
        #    回看只推 `state` 事件（复用既有 payload 形状），不新增事件种类、不新增 LLM 调用。
        self.review = False
        # 进入复盘那一刻的 `busy` 快照（V15）：**只记下、不回落**——复盘期间本来就不该有在途回合，
        # 退出时擅自把 busy 改回去会与「复盘不得改动 busy」冲突；故它只是诊断信息，没有消费方。
        self._review_busy_before = False

        # —— 发言侧工具：默认关闭，行为与没有工具时完全一致 ——
        self.tools_enabled = bool(tools_enabled)
        want = MAX_TOOL_CALLS if max_tool_calls is None else int(max_tool_calls)
        self.max_tool_calls = max(0, min(want, MAX_TOOL_CALLS))    # 配置只能收紧，不能突破硬上限
        self._chain = None

        # —— 流式输出（可选、默认关闭）：关闭时一个 token 事件都不发，事件序列与没有这个功能时
        #    逐字一致；开启后 speak 阶段逐块推 `token`，台词仍以完整 message 收口（S31/S32）——
        self.stream_tokens = bool(stream_tokens)
        # 本轮唯一标识：**每次公开编排调用生成一次**（player_move 的三个阶段共用同一个），
        # 供 token（本任务）与 speech（计划 05）配对；它不进既有事件形状，只在新事件里带（S33）。
        self.turn_id = ""
        # 本回合已发了几条 speech（T15）：同一 turn_id 内从 1 单调递增，由 `_new_turn()` 重置。
        self._speech_seq = 0

        # —— 云端语音（计划 05）：默认不接线（None）⇒ 行为与今天逐字一致（一条 speech 都不发）；
        #    由 main.py 的 lifespan 注入 TTSClient（或测试注入假 TTS）——
        self.tts = tts

        # —— 跨局长期记忆：默认不接线（None），由 main.py 的 lifespan 注入 MemoryService ——
        self.memory = memory

        # —— 跨局的定式族计数（T5）：默认不接线（None）⇒ 一律当作「第 1 次」、每次都上卡片；
        #    由 main.py 的 lifespan 无条件注入 JosekiProgress（没有开关，设计 §8.4）——
        self.joseki_progress = joseki_progress

        self._mood_region = -1
        self._mood_ms = 0
        self._mood_desc = ""
        self._reveal_pending = False
        self._recent_lines = []       # AI 原话去重窗口（最近 AI_DEDUP_WINDOW 条，跨回合存活，见 Main.gd 原判据）
        self._graph = None            # 回合状态图：首次用到时装配（build_graph 用闭包捕获 self）

        self._group_re = re.compile(_PAREN_GROUP_PATTERN)
        self._hint_re = re.compile(_MOVE_HINT_PATTERN)
        self._move_token_re = re.compile(_MOVE_TOKEN_PATTERN)
        self._pass_word_re = re.compile(_PASS_WORD_PATTERN, re.IGNORECASE)

    # ================= 回合状态图 =================

    @property
    def graph(self):
        """回合编排状态图（``agent_graph.py``）：公开方法只负责「构造状态 → ainvoke」。

        每次现取（惰性装配一次就缓存）：图用闭包捕获 ``self``，节点在调用时才读
        ``self.llm`` / ``self.katago`` 等属性，所以换掉 llm/引擎不需要重建图。
        """
        if self._graph is None:
            self._graph = build_graph(self)
        return self._graph

    async def _turn(self, phase, data=None, turn_id=None):
        """跑一次回合状态图（一个阶段），返回终态。

        编排顺序（引擎调用 / 提示词 / 台词 / 事件 / 记忆标签）全在 ``agent_graph`` 的四个节点里；
        这里只把「阶段 + 棋谱快照 + 中间数据」交出去。多阶段回合（``player_move``）由调用方依次
        跑，``turn_id`` 一路沿用（流式 token 与重复发话的判据靠它）。
        """
        state = {
            "phase": str(phase),
            "moves": list(self.st.moves),
            "messages": [],
            "reply": "",
            "tool_calls": 0,
            "wants_tools": False,
            "move": None,
            "turn_id": str(turn_id or self.turn_id or ""),
            "data": dict(data or {}),
        }
        return await self.graph.ainvoke(state)

    def _is_repeat_prefix(self, text) -> bool:
        """``text``（strip 后）是否还是去重窗口里某句原话的**前缀**（整句相等也算）。

        流式扣留的判据（S37）：只要累加的可见文本还是某句的前缀，就先扣住已经收到的增量不发——
        整句原样复读时**一条 token 都不发**（它的 ``message`` 也照旧被闸门拦下）；一旦分叉
        （确认是另一句新话）立刻把积压的增量按序补发。空串不算前缀，否则一开局就会把全部扣住。
        """
        norm = str(text or "").strip()
        if norm == "":
            return False
        return any(str(line).startswith(norm) for line in self._recent_lines)

    def _new_turn(self) -> str:
        """开启一次公开编排调用：生成新的 ``turn_id``（同一回合的多个阶段一路沿用，S33）。"""
        self.turn_id = uuid.uuid4().hex
        self._speech_seq = 0                        # T15：seq 是**同一** turn_id 内从 1 起的单调序号
        return self.turn_id

    def _next_speech_seq(self) -> int:
        """取下一条 ``speech`` 事件的序号（T15：同一 ``turn_id`` 内从 1 起单调递增）。"""
        self._speech_seq += 1
        return self._speech_seq

    def speech_pitch(self) -> float:
        """当前情绪区域的音高偏移（T2：与 ``mood`` 事件同源；取不到时退回 ``0.0``）。

        ``speech`` 事件的音高由 agent 侧填——``tts.py`` 那一层拿不到情绪（它的 ``pitch`` 恒为 0.0）。
        """
        try:
            return float(self.mood.info(self.mood.region()).get("pitch", 0.0))
        except Exception:                           # noqa: BLE001 —— 情绪数据脏了也不能让语音链断掉
            return 0.0

    # ================= 事件 =================

    def _emit(self, event):
        self.bus.publish(event)

    # ================= AI 原话去重（8 条窗口，原在 Main.gd） =================

    def _is_repeat(self, text) -> bool:
        """最近 ``AI_DEDUP_WINDOW`` 条 AI 原话里是否已有 ``text.strip()`` 后**完全相等**的一句。

        判据与前端 ``Main.gd`` 的 ``_recent_ai_texts`` 逐字一致（``strip()`` 后完全相等，不是包含）；
        窗口里只有「非 ``ai_move``」的台词，空串一律不算重复。
        """
        norm = str(text or "").strip()
        return norm != "" and norm in self._recent_lines

    def _note_line(self, text):
        """把一条 AI 原话记入去重窗口（超出上限丢最旧）。

        **被抑制的那句也要入窗**（S27）：否则第二次、第三次相同的话会从中间漏出去一次，
        与前端「每次 AI 文本都记进窗口」的行为不一致。
        """
        norm = str(text or "").strip()
        if norm == "":
            return
        self._recent_lines.append(norm)
        while len(self._recent_lines) > AI_DEDUP_WINDOW:
            self._recent_lines.pop(0)

    # ================= 工具 =================

    @staticmethod
    def _py_list(arr) -> str:
        """渲染成 Python 风格的列表（与网页版提示词里的取值格式保持一致）。"""
        return "[" + ", ".join("'%s'" % str(v) for v in arr) + "]"

    @staticmethod
    def _pct(v) -> str:
        return "%.1f%%" % (v * 100.0)

    @staticmethod
    def _llm_failed(t) -> bool:
        """LLM 调用失败的统一判据（与闲置搭话的兜底判据一致）。"""
        t = (t or "").strip()
        return t == "" or t.startswith("调用出错") or "脑子有点转不过来" in t

    @staticmethod
    def _fallback_line(kind="game") -> str:
        """LLM 不可用时露给玩家的兜底台词（kind：``game`` 棋局类 / ``chat`` 闲聊类）。

        云端失败在 ``LLMClient`` 里表现为「调用出错：…」文案（断网 / 401 / 429 / 超时），
        也可能意外抛出；两种情况下都必须换成这句玩家能读的话，绝不把错误文案当成她的台词。
        """
        lines = CHAT_FALLBACK_LINES if kind == "chat" else GAME_FALLBACK_LINES
        return str(lines[random.randrange(len(lines))])

    @staticmethod
    def _idle_line() -> str:
        """闲置搭话的兜底短句（LLM 不可用时）。"""
        return str(IDLE_LINES[random.randrange(len(IDLE_LINES))])

    def _update_katago_brief(self, analysis, num_moves):
        """把 KataGo 形势数据整理成一段文字，存进 st.last_katago，供 Azure 上下文使用。"""
        if not analysis or "error" in analysis:
            return
        conv = prompts.to_black_perspective(analysis, num_moves)
        wr = float(conv[0])
        sc = float(conv[1])
        who = "Estarth（黑）稍优" if sc > 0.5 else ("Azure（白）稍优" if sc < -0.5 else "双方接近")
        s = "黑方（Estarth）胜率 %.1f%%；目差 %+.1f 目（正数=黑/Estarth 领先，含贴目）；整体 %s。" % (wr * 100.0, sc, who)
        own = analysis.get("ownership", [])
        if own:
            b = 0.0
            w = 0.0
            for v in own:
                f = float(v)
                if f > 0.0:
                    b += f
                else:
                    w -= f
            s += " 实地估算：Estarth（黑）约 %.0f 目，Azure（白）约 %.0f 目（按逐点归属估算，仅供参考）。" % (b, w)
        self.st.last_katago = s

    def _think_hint(self) -> str:
        """思考风格半句：只在思考档开启时注入（判据与旧 ``_sys_content`` 一致）。"""
        if self.llm is not None and getattr(self.llm, "think_mode", 0) > 0:
            return self.mood.think_hint()
        return ""

    def _sys_content(self, len_note) -> str:
        """系统消息文本：经 ``prompt_chains`` 的模板链构造（人设 + 心情 + 思考风格 + 长度约束）。

        系统消息与阶段无关——阶段提示只在「无 context」的回退路径里补进系统消息，这里既没有
        context 也没有 prompt，固定按首个阶段取链，产出与旧实现逐字符一致（test_prompt_chains 锁定）。
        """
        return prompt_chains.build_messages(
            prompt_chains.PHASES[0], len_note=len_note,
            mood_name=self.mood.region_name(), think_hint=self._think_hint(),
        )[0]["content"]

    def _sys_msg(self):
        """系统消息：聊天 / 闲置搭话这类「context_block + 历史窗口」的路径用；带 with_context 的路径走 `_phase_messages`。"""
        return {"role": "system", "content": self._sys_content(LEN_NOTE if self.limit_len else "")}

    def _phase_messages(self, phase, context, len_note=None) -> list:
        """某阶段的完整消息（系统 + 上下文）：经 ``prompt_chains`` 的链构造。

        ``len_note=None`` 表示「按本局设置推导」：``limit_len`` 开着就附上 ``LEN_NOTE``（短回复约束）——
        与旧 ``_sys_msg()`` 的做法一致，调用点不必各自记得；定式讲解这类特例显式传入 ``LEN_TEACH_NOTE``。

        必须与旧 ``[_sys_msg(), _user_msg(context)]`` 逐字符一致——模型要看到的字与本次重构前
        完全相同，一个标点都不能变（tests/test_prompt_chains.py 逐字符锁定）。
        """
        if len_note is None:
            len_note = LEN_NOTE if self.limit_len else ""
        return prompt_chains.build_messages(
            phase, context=context, len_note=len_note,
            mood_name=self.mood.region_name(), think_hint=self._think_hint(),
        )

    @staticmethod
    def _user_msg(content):
        return {"role": "user", "content": content}

    def _len_note(self, teach=False) -> str:
        """长度约束后缀：短回复开着时附 ``LEN_NOTE``；定式讲解换 ``LEN_TEACH_NOTE``。

        调用点在 ``agent_graph`` 里——「定式成型那一手」是唯一显式传 ``len_note`` 的地方
        （tests/test_prompt_chains.py 的条数断言守着调用点有没有漏）。
        """
        if not self.limit_len:
            return ""
        return LEN_TEACH_NOTE if teach else LEN_NOTE

    def _moves_with(self, color, gtp):
        out = list(self.st.moves)
        out.append([color, gtp])
        return out

    def _set_busy(self, v):
        """设置忙碌状态；值没变就不推事件（嵌套的编排外壳会各自兜一次归位）。"""
        if self.busy == v:
            return
        self.busy = v
        self._emit(events.busy(v))

    def clean_reply(self, text) -> str:
        """去掉带点位的舞台提示；清洗后为空则退回原文。"""
        if text == "" or text is None:
            return text
        out = text
        matches = list(self._group_re.finditer(out))
        for m in reversed(matches):              # 从后往前删，避免偏移
            if self._hint_re.search(m.group(0)):
                out = out[:m.start()] + out[m.end():]
        out = out.strip()
        while "\n\n\n" in out:
            out = out.replace("\n\n\n", "\n\n")
        return out if out != "" else text

    async def _reply(self, messages, max_tokens=300) -> str:
        """所有对外展示的 LLM 回复统一过一遍清洗。

        云端失败时 ``LLMClient.chat`` 返回「调用出错：…」文案（不抛异常）；这里再兜住
        「真抛出来」的情况（返回空串），由调用方的兜底台词顶上——她该下的棋、该念的
        落子宣告都不会因此丢掉。
        """
        try:
            return self.clean_reply(await self.llm.chat(messages, max_tokens))
        except Exception as e:                  # noqa: BLE001
            print("[Agent] LLM 调用异常：%s" % e)
            return ""

    # ================= 发言侧工具（她自己查资料） =================

    @property
    def chain(self):
        """LangChain 链：默认实时取 ``llm.chain``。

        ``LLMClient`` 在扩展字段被服务端拒绝后会**重建**链实例，抱住旧引用会一直带着被拒的字段，
        所以这里每次现取；测试可直接赋值替换（赋值即固定，不再回看 ``llm.chain``）。
        """
        if self._chain is not None:
            return self._chain
        return getattr(self.llm, "chain", None)

    @chain.setter
    def chain(self, v):
        self._chain = v

    def _chain_ok(self) -> bool:
        """直连链的前置检查（``LLMClient.chat`` 里 ``_url_ok`` 的同一判据）。

        未配置 ``llm_base_url`` 时 openai 客户端会静默退回 api.openai.com：真连外网、单次卡几十秒，
        所以工具路径也必须先拦（链自己没这个预检）。没接 LLM（测试注入链）时无从查起，按可用处理。
        """
        if self.llm is None:
            return True
        u = str(getattr(self.llm, "base_url", "") or "").strip().lower()
        return u.startswith("http://") or u.startswith("https://")

    def _tool_args(self, name, args) -> dict:
        """补齐工具参数：引擎与当前棋谱由后端注入，模型不必（也不该）自己编。"""
        out = dict(args or {})
        if name == "katago_analyze":
            out["engine"] = self.katago
        if name in MOVE_TOOLS and not out.get("moves"):
            out["moves"] = [list(m) for m in self.st.moves]
        return out

    def _bind_tools(self, pool, max_tokens) -> tuple:
        """给链绑上工具与本次输出上限，返回 ``(可调用的链, ainvoke 的调用参数)``。

        上限走 ``extra_body`` 的线上键 ``max_tokens``（与 ``LLMClient.chat`` 同一理由：
        ``ainvoke(max_tokens=…)`` 会被 langchain-openai 改名成 ``max_completion_tokens``，
        目标服务端不认）；``bind(extra_body=…)`` 是整体替换，所以先并入链自带的扩展字段。
        没有 ``bind`` 的鸭子类型链（测试替身）只能退回 ``ainvoke`` 参数。

        服务端拒绝过扩展字段（``llm._extra_body_unsupported``）时什么都不加：与
        ``LLMClient._extra_for`` 同一判据——不加料，工具路径才不会每次调用都白挨一次 400 再重试。
        """
        bound = self.chain.bind_tools(list(pool))
        if not hasattr(bound, "bind"):
            return bound, {"max_tokens": max_tokens}
        if getattr(self.llm, "_extra_body_unsupported", False):
            return bound.bind(extra_body={}), {}
        body = dict(getattr(self.chain, "extra_body", None) or {})
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body["max_tokens"] = max_tokens
        return bound.bind(extra_body=body), {}

    async def _chain_invoke(self, bound, msgs, kwargs):
        """发一次工具链请求；服务端拒绝参数（HTTP 400/422）时去掉扩展字段与输出上限重试一次。

        与 ``LLMClient.chat`` 同一安全网：有的 OpenAI 兼容服务遇到未知字段直接 400，
        工具路径不能因此把本来能说话的回合变成兜底台词。
        """
        try:
            return await bound.ainvoke(msgs, **kwargs)
        except Exception as e:                  # noqa: BLE001
            if getattr(e, "status_code", None) not in (400, 422):
                raise
            print("[Agent] 工具调用被服务端拒绝参数（HTTP %d），去掉扩展字段与输出上限重试一次" % e.status_code)
            return await bound.bind(extra_body={}).ainvoke(msgs)

    # ================= 流式输出（可选、默认关闭） =================

    def _bind_stream(self, src, max_tokens):
        """给流式来源补上输出上限：与 ``_bind_tools`` 同一手法（``bind(extra_body=…)`` 是整体替换）。"""
        if not hasattr(src, "bind"):
            return src                              # 鸭子类型替身（测试）：不支持 bind，照原样用
        if getattr(self.llm, "_extra_body_unsupported", False):
            return src.bind(extra_body={})          # 服务端拒绝过扩展字段：什么都不加
        body = dict(getattr(src, "extra_body", None) or {})
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body["max_tokens"] = max_tokens
        return src.bind(extra_body=body)

    def _stream_chunks(self, messages, max_tokens=300):
        """本次出话的可流式来源：返回「逐块产出**回答原文**」的异步生成器；不可流式时返回 ``None``。

        ``stream_tokens`` 关闭（默认）时**直接返回 None**——关闭态一个 ``token`` 事件都不会发，
        事件序列与没有这个功能时逐字一致（S31③）。开启后优先用实时取到的 LangChain 链
        （与 ``bind_tools`` 同一个来源）；鸭子类型替身只实现 ``astream`` 时用它自己。

        与 ``LLMClient.chat`` 同一条安全网：地址没配（``_chain_ok`` 为假）时不发任何请求——
        否则 openai 客户端会静默退回 api.openai.com 真连外网。输出上限按线上键 ``max_tokens``
        绑进请求体（理由同 ``_bind_tools``：``ChatOpenAI`` 的 ``max_tokens`` 会被改名）。

        每块经 ``llm_chain.chunk_text_of`` 取**原文**（只认回答的 content 通道，``reasoning_content``
        之类的推理字段一概不读），空块直接跳过；think 段的剥离**不在这里**做——chunk 会把标签切块，
        必须由 ``ThinkStream`` 在**累加文本**上跨块剥离（见 ``llm_chain.ThinkStream``）。
        """
        if not self.stream_tokens or not self._chain_ok():
            return None
        src = self.chain
        if src is None or not hasattr(src, "astream"):
            src = self.llm if hasattr(self.llm, "astream") else None
        if src is None:
            return None
        src = self._bind_stream(src, max_tokens)

        async def gen():
            async for chunk in src.astream(messages):
                text = chunk_text_of(chunk)
                if text != "":
                    yield text

        return gen()

    async def _run_tool(self, by_name, name, args) -> str:
        """执行一个工具：发 ``tool`` start/end 事件，返回给模型的 ``ToolMessage`` 文本；绝不抛出。

        ``ok`` 的判据只有两个：工具自己给出的失败文本（``tools.FAIL_PREFIX``）、超时。
        ``args`` 传的是**模型给的那份**——注入的引擎对象不可序列化，也不该推给客户端。
        """
        self._emit(events.tool("start", name, args))
        t0 = time.monotonic()
        if name not in by_name:
            text = "没有这个工具（%s），可用的只有：%s。" % (name, "、".join(sorted(by_name)))
            ok = False
        else:
            try:
                text = str(await asyncio.wait_for(
                    by_name[name].ainvoke(self._tool_args(name, args)), TOOL_TIMEOUT_SEC))
                ok = not tool_failed(text)
            except asyncio.TimeoutError:
                text = TOOL_TIMEOUT_TEXT % TOOL_TIMEOUT_SEC
                ok = False
            except Exception as e:              # noqa: BLE001
                text = "工具执行失败（%s），这次先不参考它，直接回答。" % e
                ok = False
        self._emit(events.tool("end", name, args, ok, int((time.monotonic() - t0) * 1000)))
        return text

    async def _tool_loop(self, messages, max_tokens, phase="", tools=None) -> str:
        """受限 tool loop：模型可以在预算内先查资料，再落回一句自然语言。

        **现状（计划 04 · Task 3 起）：工具轮的主实现已搬到状态图**——``agent_graph._decide_tool_round``
        （每轮一次模型调用）＋ ``tools`` 节点（执行 + 回灌 ``ToolMessage``），由条件边
        ``decide → tools → decide`` 驱动。本函数的早退判据与图的 ``_tool_round_ready`` 是同一条
        （链不可用 / 地址未配 / 预算为 0 时直接 ``_reply``），所以工具开着时它**只在「链不可用」这一
        降级路径**上被调用（此时它等价于 ``_reply``）；它的 ``for`` 循环体目前**只被
        ``tests/test_tools.py`` 直接覆盖**（保留是为了不删既有守卫，且两条语义必须与图对齐——
        尤其「超额工具的每个 ``tool_call_id`` 都要补一条预算 ``ToolMessage``」）。

        只在 ``hint()`` 与落子宣告（``own_turn`` 阶段）两处启用（且 ``tools_enabled`` 为真）：
        聊天/闲置这类短回合不走 tool loop，免得白等工具往返的时延。

        硬规则：
        - 工具次数上限 ``self.max_tool_calls``（默认 ``MAX_TOOL_CALLS``），单工具超时 ``TOOL_TIMEOUT_SEC``；
          预算用尽后不再执行工具，只追加「别再调工具、直接回答」的指令催它收尾；
        - 工具与链的任何失败都不许抛出：失败变成可读 ``ToolMessage``，链抛错则返回空串，
          由调用方的兜底台词顶上（与 ``_reply`` 同一降级）；
        - 玩家看到的永远是模型重写的自然语言（``clean_reply`` 必经），工具输出只回到模型手里；
        - 链不可用（没接 LLM / 未配置地址）或预算为 0 时退回 ``_reply``：与不开工具完全同一条路。
        """
        pool = list(TOOLS if tools is None else tools)
        by_name = {t.name: t for t in pool}
        if self.chain is None or not self._chain_ok() or self.max_tool_calls <= 0:
            return await self._reply(messages, max_tokens)
        bound, kwargs = self._bind_tools(pool, max_tokens)
        msgs = list(messages)
        used = 0
        ai = None
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                ai = await self._chain_invoke(bound, msgs, kwargs)
            except Exception as e:              # noqa: BLE001
                print("[Agent] tool loop 调用异常（%s）：%s" % (phase, e))
                return ""
            calls = list(getattr(ai, "tool_calls", None) or [])
            if not calls:                       # 没有工具请求：这就是她要说的话
                return self.clean_reply(content_of(ai))
            msgs.append(ai)
            for tc in calls:
                name = str(tc.get("name", ""))
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                if used >= self.max_tool_calls:  # 预算用尽：不再执行，催它直接回答（不存在的工具同样占预算）
                    msgs.append(ToolMessage(TOOL_BUDGET_TEXT % self.max_tool_calls,
                                            tool_call_id=str(tc.get("id", ""))))
                    continue
                used += 1
                msgs.append(ToolMessage(await self._run_tool(by_name, name, args),
                                        tool_call_id=str(tc.get("id", ""))))
        print("[Agent] tool loop 到达往返上限（%d 次，%s）：按现有内容收尾" % (MAX_TOOL_ROUNDS, phase))
        return self.clean_reply(content_of(ai)) if ai is not None else ""

    # ================= 长期记忆（跨局） =================

    def _long_term(self, query) -> str:
        """召回跨局长期记忆的注入文本；未接线 / 未启用 / 任何故障一律返回空串。

        默认不接线（``memory is None``）或 ``available == False`` 时不注入——
        关闭长期记忆时的提示词与没有这个功能时逐字符一致。召回在出话路径上，
        这里再兜一层异常保护，绝不让记忆把这一回合的 ``busy`` 卡住。

        本方法是**同步**的：调用点必须 ``await asyncio.to_thread(self._long_term, …)``——
        慢 embedding（最长 ``embed_timeout`` 秒）绝不能冻住事件循环（心跳与落子宣告都在这条循环上）。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return ""
        try:
            return str(mem.recall_for(str(query or "")) or "")
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 长期记忆召回异常（已忽略）：%s" % e)
            return ""

    def _remember_game(self, summary, black_wr, score):
        """把这一局的总结写进长期记忆（``result`` 渲染成一行）；失败只打日志，不影响对局。

        同 ``_long_term``：同步实现，调用点走 ``asyncio.to_thread``（写库 + embedding 都在里面）。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return
        try:
            result = "黑方（Estarth）胜率 %s，目差 %s" % (self._pct(black_wr), "%+.1f" % score)
            mem.remember_game(list(self.st.moves), result, str(summary or ""))
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 长期记忆写入异常（已忽略）：%s" % e)

    # ================= 三维情绪 =================

    def _mood_event(self, ev, desc_extra=""):
        """事件结算入口：本地增量立即生效，区域变化才推给界面。"""
        self.mood.apply_event(ev)
        self._mood_desc = EmotionModel.event_desc(ev) + desc_extra
        self.sync_mood(False)

    def sync_mood(self, force=False, quiet=False):
        """把当前情绪区域推给界面；区域没变时不产生任何事件。"""
        r = self.mood.region()
        if not force and r == self._mood_region:
            return
        self._mood_region = r
        info = self.mood.info(r)
        self._emit(events.mood({
            "region": r,
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": "" if quiet else str(info.get("filler", "")),
            "burst": "" if quiet else str(info.get("burst", "")),
            "pitch": float(info.get("pitch", 0.0)),
        }))

    def tick_mood(self):
        """客户端每 2 秒的心跳：只结算「向基线回落」。

        复盘期间**直接返回**（V14）：不衰减、不发 ``mood`` 事件——回看全程她要静音。
        ``mood_payload()`` 不受影响（``hello`` 同步照常可读）。
        """
        if self.review:
            return
        self.sync_mood(False)

    def player_interaction(self, what):
        """玩家亲密互动（摸头等）：本地增量 + 静默同步（语气词让位给互动音效）。

        复盘期间**直接返回**（V14）：不产生任何情绪变化（连增量都不落）、不发 ``mood`` 事件。
        """
        if self.review:
            return
        self.mood.apply_event(what)
        self._mood_desc = EmotionModel.event_desc(what)
        self.sync_mood(False, True)

    async def _mood_delta(self, cur, desc):
        """情绪增量：优先结构化输出，拿不到才回落到正则解析（``None`` = 两条路都没拿到）。

        结构化能力由 LLM 客户端提供（``LLMClient.mood_delta``）；只实现 ``chat()`` 的鸭子类型
        客户端没有它，直接走正则——与 ``_think_hint`` 看 ``think_mode`` 是同一种写法。

        失败一律静默（含异常、含形状不对的脏返回值）：情绪小调用绝不打扰主流程，也绝不把错误
        文案变成她的台词。
        """
        region = self.mood.region_name()
        fn = getattr(self.llm, "mood_delta", None)
        if fn is not None:
            try:
                d = await fn(cur, region, desc)
            except Exception as e:                  # noqa: BLE001
                print("[Agent] 情绪结构化输出异常：%s" % e)
                d = None
            if isinstance(d, (list, tuple)) and len(d) == 3:   # 形状不对＝这次没拿到
                return d
        try:
            text = await self.llm.chat([{
                "role": "user",
                "content": prompts.mood_delta_prompt(cur, region, desc),
            }], MOOD_LLM_MAX_TOKENS, True)
        except Exception as e:                      # noqa: BLE001
            print("[Agent] 情绪小调用异常：%s" % e)
            return None
        if self._llm_failed(text):
            return None
        parsed = EmotionModel.parse_delta(text)
        return parsed[1] if parsed[0] else None

    async def _mood_llm_refine(self):
        """单独的「情绪变化」小调用：异步、限频、失败静默；绝不等它。

        优先结构化输出（模型直接给三维增量），拿不到才回落到正则解析（v2 行为不变）；两条路都
        拿不到就静默收场，情绪只保留本地事件结算的结果。
        """
        if self.llm is None or self._mood_desc == "" or self.busy:
            return
        now = int(time.monotonic() * 1000)
        if now - self._mood_ms < MOOD_LLM_GAP_MS:
            return
        self._mood_ms = now
        desc = self._mood_desc
        self._mood_desc = ""
        d = await self._mood_delta((self.mood.v, self.mood.c, self.mood.a), desc)
        if d is None:
            return
        self.mood.apply_delta(d[0], d[1], d[2])
        self.sync_mood(False)

    @staticmethod
    def _chat_event(kind) -> str:
        return {
            "laugh": "chat_laugh",
            "delight": "chat_praise",
            "sad": "chat_sad",
            "puzzled": "chat_question",
        }.get(kind, "chat_normal")

    @staticmethod
    def chat_emotion(text) -> str:
        """聊天内容的粗分类 → 表情（本地关键词判断，不额外打扰 LLM）。"""
        t = (text or "").lower()
        for kw in ["哈哈", "hh", "笑", "有趣", "好玩", "233", "草"]:
            if kw in t:
                return "laugh"
        for kw in ["喜欢", "爱你", "谢谢", "温柔", "可爱", "厉害", "真棒", "漂亮", "好看", "帅", "辛苦了"]:
            if kw in t:
                return "delight"
        for kw in ["难过", "伤心", "累", "烦", "不开心", "生气", "讨厌", "输了", "失败", "糟糕", "唉", "哭"]:
            if kw in t:
                return "sad"
        for kw in ["？", "?", "吗", "怎么", "为什么", "什么", "如何", "能不能", "是不是"]:
            if kw in t:
                return "puzzled"
        return "pleased"

    # ================= 定式导学 =================

    def _joseki_check(self) -> dict:
        """定式识别（纯本地、零引擎调用，每手一次）。"""
        if not self.joseki_teach:
            return {}
        r = JosekiRecognizer.scan(self.st.moves)
        if not r:
            return {}
        kind = str(r["kind"])
        # 去重防刷屏，**按 kind 分家**（Entry/分叉各自记账，不许吃掉偏离的提醒额度）：
        #   进入「E:<角>:<族>」每角每族一次；分岔「B:<角>:<族>」每角每族一次；
        #   成型「F:<角>:<类>」每角每类一次；偏离「D:<角>」每角一次、全局最多 2 次（悔棋回滚后
        #   可重新识别）。未知 kind 退回 D:<角>（防御：绝不让它绕过去重）。
        if kind == "entered":
            dedup = "E:%s:%s" % (r["corner"], r.get("family", ""))
        elif kind == "branch":
            dedup = "B:%s:%s" % (r["corner"], r.get("family", ""))
        elif kind == "formed":
            dedup = "F:%s:%s" % (r["corner"], r["cat"])
        else:
            dedup = "D:%s" % r["corner"]
        if self._joseki_logged(dedup):
            return {}
        if kind == "deviated":
            dcount = sum(1 for e in self.st.joseki_log if str(e.get("key", "")).startswith("D:"))
            if dcount >= 2:
                return {}
        self.st.joseki_log.append({"key": dedup, "move_no": len(self.st.moves)})
        return r

    def _joseki_logged(self, key) -> bool:
        return any(str(e.get("key", "")) == key for e in self.st.joseki_log)

    def _joseki_emit(self, r, show_card=False, mistake=None):
        """棋盘高亮轨迹：成型=按手顺点亮；偏离=只点「常规一手」那个位置。

        卡片载荷（计划 07 T6）：``show_card`` / ``mistake`` 由调用方（``agent_graph`` 的两个
        ``_joseki_emit`` 调用点）从**本轮** ``data`` 里传进来，两个参数都有安全缺省，既有的单参
        调用签名照旧可用。``mistake`` 这里**原样透传**（本函数不做任何判定）：传进来的可能是
        ``data["jk_mistake"]`` 那种带 ``is_mistake`` 的原始 dict（玩家那一手），也可能是已收敛好的
        ``{"move_no","x","y","drop"}`` 或 ``None``；**唯一的过滤点在 ``events._joseki_mistake``**
        （它只在 ``is_mistake is True`` 时才认，否则 ``None``——没亏的那一手不上卡片标记，U31）。

        旧行为一字不动（U32）：``deviated`` 仍把 ``points`` 换成 ``[expect]``、``expect`` 缺失
        时提前 return，``points`` 为空时也提前 return（不发事件）——新字段只是**追加**在发出去的
        那个 payload 上。
        """
        if not r:
            return
        pts = list(r.get("points", []))
        if str(r["kind"]) == "deviated":
            ex = r.get("expect", (-1, -1))
            if ex[0] < 0:
                return
            pts = [ex]
        if not pts:
            return
        try:
            k = int(r.get("k", 0))
        except (TypeError, ValueError):
            k = 0
        cands = []
        for c in r.get("candidates") or []:
            if isinstance(c, dict):            # 只留卡片要的两键；`next` 原样（JSON 化后是数组）
                cands.append({"name": c.get("name"), "next": c.get("next")})
        self._emit(events.joseki({
            "kind": str(r["kind"]),
            "corner": str(r["corner"]),
            "points": pts,
            "family": str(r.get("family") or ""),
            "name": str(r.get("name") or ""),
            "k": k,
            "line": list(r.get("line") or []),
            "candidates": cands,
            "show_card": bool(show_card),
            "mistake": mistake,
        }))

    # —— 卡片门禁（T5）：前 3 次都上卡片，第 4 次起只在真的走错时才上 ——

    async def joseki_show_card(self, jk, mistake=None) -> bool:
        """这一手该不该上「定式教学卡片」（**只读**：不改 ``st``、不碰计数）。

        自增在 ``joseki_count_entry`` 里，顺序固定「**先问后记**」——若先自增再问，第一次出现时
        ``seen`` 已是 1，``seen < 3`` 只在第 1、2 次成立，与「前 3 次都上卡片」矛盾。

        判定：同一族历史出现次数（跨局）``seen < 3`` ⇒ 一律 True；第 4 次起只有 ``kind ==
        "deviated"`` 且 ``is_joseki_mistake(...)``（真亏**且**不在库线上）才 True。
        ``joseki_progress`` 未接线 / 族 id 缺失（旧库、异常形状）⇒ **保守地 True**（宁可多教一次）；
        次数读不出来时 ``JosekiProgress.seen`` 自己降到 ``0``（当成第 1 次），所以坏库的表现就是
        「每次都教」，绝不影响对局。``mistake`` 是 T3 放进 ``data["jk_mistake"]`` 的那个 dict
        （键 ``is_mistake/on_line/gtp/x/y/drop/rank_ok``）——这里按 ``is_joseki_mistake`` 的口径
        重算，而不是直接采信 ``is_mistake``。
        """
        if not jk:
            return False
        if self.joseki_progress is None:
            return True
        family = str(jk.get("family") or "")
        if family == "":
            return True
        seen = await asyncio.to_thread(self.joseki_progress.seen, family)   # 永不抛，故障时 0
        try:
            seen = int(seen)
        except (TypeError, ValueError):      # 替身 / 将来实现给了脏值：当成第 1 次（fail-open）
            seen = 0
        if seen < 3:
            return True
        if str(jk.get("kind", "")) != "deviated":
            return False
        if not isinstance(mistake, dict):
            return False
        try:
            return is_joseki_mistake(jk, bool(mistake.get("on_line")),
                                     float(mistake.get("drop") or 0.0),
                                     bool(mistake.get("rank_ok")))
        except Exception as e:                  # noqa: BLE001 —— 脏 mistake 绝不许把这一回合打挂
            # 保守取向（与整条链一致）：判不出来就**当没走错**——第 4 次起默认不上卡片，
            # 既不误判「走错」、也不额外打扰玩家；只留一行日志，对局照常。
            print("[Joseki] 走错判定失败（按「不算走错」保守处理，不影响对局）：%s" % e)
            return False

    async def joseki_count_entry(self, jk) -> None:
        """记一次「这一族被走到了」（**同族同局最多计 1 次**；只写计数，不发事件、不决定卡片）。

        先把族落进 ``st.joseki_counted``（并把它从 ``st.joseki_dropped`` 里摘掉——新的一集开始），
        再 ``bump``。磁盘 IO 走 ``to_thread``（慢库不得冻住事件循环）；失败也只是丢这一次记忆——
        ``JosekiProgress`` 内部已吞掉异常并降级返回旧值，这里绝不抛、绝不影响对局。
        """
        if not jk or self.joseki_progress is None:
            return
        family = str(jk.get("family") or "")
        if family == "":
            return
        if family in self.st.joseki_counted:
            return
        self.st.joseki_counted.append(family)
        if family in self.st.joseki_dropped:
            self.st.joseki_dropped = [f for f in self.st.joseki_dropped if f != family]
        await asyncio.to_thread(self.joseki_progress.bump, family)

    def _unbump_offloop(self, families):
        """退回族计数（悔棋）：只做磁盘 IO，异常在 ``JosekiProgress`` 内部已吞。

        ``undo`` 是**同步**方法，而 ``/api/undo`` 是 ``async def``，所以按「有没有运行中的事件
        循环」分两种走法：**有 loop**（真实端点）时把磁盘动作丢到线程池（``run_in_executor``，
        不 await）；**没有 loop**（纯同步单测 / 被同步代码直接调用）时就地同步做完——两者都
        **不阻塞**调用方。绝不能把整个 ``undo`` 丢线程：``_emit`` / ``bus.publish`` 从工作线程唤醒
        ``asyncio.Queue`` 不是线程安全的，所以这一层只碰 ``JosekiProgress``。
        """
        if self.joseki_progress is None:
            return
        fams = [f for f in dict.fromkeys(families or []) if f]      # 去重 + 丢掉空族
        if not fams:
            return

        def run():
            for f in fams:
                self.joseki_progress.unbump(f)          # 内部已降级，不会抛

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                            # 没有运行中的 loop：就地做完（确定性）
            loop = None
        if loop is None:
            run()
            return
        loop.run_in_executor(None, run)

    # ================= 玩家落子 =================

    async def player_move(self, x, y):
        """玩家落子：点评 → 她思考落子 → 落子后感想。

        多阶段回合：依次跑 ``after_player → own_turn → just_moved`` 三个阶段的图（顺序见
        ``agent_graph.py``）。前置判定（已忙 / 非法手）留在公开契约里：非法手**不置 busy、不调引擎**，
        只发一条可读提示 + 权威快照让界面撤销乐观落子。提示**也是台词**：走 ``_emit_line``（T20①），
        开局一次 ``_new_turn()`` 给配对的 ``speech`` 一个干净的 ``turn_id`` / ``seq``。

        这里把 ``state`` **放在提示之前**（与旧顺序相反，有意为之）：前端只在收到 WS 的 ``state``
        时才 ``board.set_stones`` 重同步，而 ``_emit_line`` 里的合成要等（最快 1~3 秒、失败最坏
        30 秒）——先推快照，界面才不会把这一手不成立的子留在棋盘上。
        """
        if self.busy:
            return
        legal = self.st.board.is_legal(x, y, "black")
        if not legal["ok"]:
            self._new_turn()                            # 这行提示自己就是一次对外发言（T20①）
            self._emit(events.state(self.st.payload()))   # 先让界面撤销乐观落子（不等合成）
            await _emit_line(self, events.AI, "这手不成立：%s" % legal["err"])
            return
        self._set_busy(True)
        self._new_turn()                            # 三阶段共用同一轮标识（S33）
        try:
            state = await self._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y})
            state = await self._turn(PHASE_OWN_TURN, state.get("data"), state.get("turn_id"))
            await self._turn(PHASE_JUST_MOVED, state.get("data"), state.get("turn_id"))
        except Exception as e:                      # 编排意外抛出（LLM/引擎）：不得让 busy 永久为真
            print("[Agent] 落子编排异常：%s" % e)
        finally:
            self._set_busy(False)

    def _pick_ai_move(self, move_infos) -> list:
        """按引擎候选顺序取第一个在当前棋盘合法的点；不允许虚着时绝不虚着。"""
        for m in move_infos:
            if not isinstance(m, dict):
                continue
            mv = str(m.get("move", ""))
            if mv == "":
                continue
            if mv == "pass":
                if self.st.allow_pass:
                    return ["pass", -1, -1]
                continue
            xy = coords.gtp_to_xy(mv)
            if xy[0] >= 0 and self.st.board.is_legal(xy[0], xy[1], "white")["ok"]:
                return [mv, xy[0], xy[1]]
        if self.st.allow_pass:
            return ["pass", -1, -1]
        # 候选全部不可用：就近找任意合法点，保证 Azure 一定落子
        near = self.st.last_played_xy()
        best = (-1, -1)
        best_d = 1e18
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                if self.st.board.at(x, y) != "":
                    continue
                dx = x - (near[0] if near[0] >= 0 else 9)
                dy = y - (near[1] if near[1] >= 0 else 9)
                d = float(dx * dx + dy * dy)
                if d < best_d and self.st.board.is_legal(x, y, "white")["ok"]:
                    best_d = d
                    best = (x, y)
        if best[0] >= 0:
            return [coords.xy_to_gtp(best[0], best[1]), best[0], best[1]]
        return ["pass", -1, -1]

    def _candidate_from_text(self, text) -> dict | None:
        """从模型原话里认出它自己报出的落点候选（S21）：GTP 坐标或虚着；认不出返回 ``None``。

        只做「认」，**不做合法性判断**——合法性由 ``_resolve_ai_move`` 统一把关（与
        ``_pick_ai_move`` 同一判据），这样「模型报了个坏点」也一定能回退到可用落点。
        """
        t = str(text or "")
        if t == "":
            return None
        if self._pass_word_re.search(t):
            return {"pass": True}
        m = self._move_token_re.search(t)
        if m is None:
            return None
        return {"pass": False, "gtp": m.group(0)}

    def _resolve_ai_move(self, candidate, move_infos) -> list:
        """落点定案（S21 三条不可回退的边界），返回 ``[gtp, x, y]``：

        1. 模型候选点必须过 ``board.is_legal(x, y, "white")["ok"]`` 才用，否则回退 ``_pick_ai_move``；
        2. ``pass`` 只在 ``st.allow_pass`` 为真时可用（沿用 ``_pick_ai_move`` 的既有判据）；
        3. **任何情况下都产出可用落点**——``_pick_ai_move`` 本身就保证这一点（合法点或允许时的 pass），
           所以候选再离谱也不会「这一手不下」。
        """
        if isinstance(candidate, dict):
            if candidate.get("pass"):
                if self.st.allow_pass:
                    return ["pass", -1, -1]
            else:
                gtp = str(candidate.get("gtp") or "")
                xy = coords.gtp_to_xy(gtp)
                if xy[0] >= 0 and self.st.board.is_legal(xy[0], xy[1], "white")["ok"]:
                    return [gtp, xy[0], xy[1]]
        return self._pick_ai_move(list(move_infos or []))

    # ================= 其余功能 =================

    async def hint(self):
        """💡 提示：给方向性提示，不直接说坐标（顺序见 ``agent_graph.py`` 的 observe 阶段）。"""
        if self.busy:
            return
        self._set_busy(True)
        self._new_turn()
        try:
            await self._turn(PHASE_OBSERVE)
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 提示异常：%s" % e)
            await _emit_line(self, events.AI_COMMENT, self._fallback_line("game"))
        finally:
            self._set_busy(False)

    async def analyze_position(self):
        """局面分析：以 Azure（执白）的视角解读局势（顺序见 ``agent_graph.py`` 的 analyze 阶段）。"""
        if self.busy:
            return
        if not self.st.moves:
            self._new_turn()                        # 空盘提示也是对外台词（T20①）
            await _emit_line(self, events.AI, "棋盘还空着呢~ 先落几手，我再帮你看看形势。")
            return
        self._set_busy(True)
        self._new_turn()
        try:
            await self._turn(PHASE_ANALYZE)
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 分析异常：%s" % e)
            await _emit_line(self, events.AI, "📊 " + self._fallback_line("game"))
        finally:
            self._set_busy(False)

    async def chat(self, text):
        """自由聊天（顺序见 ``agent_graph.py`` 的 chat 阶段）。"""
        if self.busy:
            return
        if text is None or text.strip() == "":
            return
        self.st.chat_history.append({"role": "user", "content": text})
        # 按聊天内容即时结算情绪（本地关键词分类）
        self._mood_event(self._chat_event(self.chat_emotion(text)), "，他说：「%s」" % text[:40].strip())
        self._set_busy(True)
        self._new_turn()
        try:
            await self._turn(PHASE_CHAT, {"text": text})
        except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
            print("[Agent] 聊天异常：%s" % e)
        finally:
            self._set_busy(False)

    async def idle_remark(self):
        """闲置搭话：Estarth 一段时间没动静时，Azure 主动说一句短话。"""
        if self.busy:
            return
        self._new_turn()
        await self._turn(PHASE_IDLE)                 # 不进 busy：与今天一致（空闲搭话不锁棋盘）

    async def summarize(self, note=""):
        """对局总结（顺序见 ``agent_graph.py`` 的 summary 阶段）。"""
        if self.busy:
            return
        if not self.st.moves:
            self._new_turn()                        # 空盘提示也是对外台词（T20①）
            await _emit_line(self, events.AI, "还没下过棋呢~")
            return
        self._set_busy(True)
        self._new_turn()
        try:
            await self._turn(PHASE_SUMMARY, {"note": note})
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 总结异常：%s" % e)
            await _emit_line(self, events.AI, "📋 对局总结\n" + self._fallback_line("chat"))
        finally:
            self._set_busy(False)

    # 注意（计划 05 · T20②）：下面四个**同步**方法里的短提示**故意保持纯文字**（不发 `speech`）——
    # 它们是同步方法（S5 不许改成 async），补 `speech` 就得 `await`、还得动端点；Task 5 会在 README 写明。
    def undo(self, turn_index):
        """俗手回档：撤销该手及其后的 Azure 应手，回到该手之前。"""
        if self.busy:
            return
        if turn_index < 0 or turn_index >= len(self.st.snapshots):
            self._emit(events.message(events.AI, "这个回档点已经不存在了哦"))
            return
        self._mood_event("undo")
        # 族计数要在**覆盖 st 之前**先记下「回档前已计过数的族」——覆盖之后就再也看不到它们了
        old_counted = list(self.st.joseki_counted)
        s = self.st.snapshots[turn_index]
        self.st.board = s["board"]
        self.st.moves = s["moves"]
        self.st.move_seq = s["move_seq"]
        self.st.memory_log = s["memory_log"]
        self.st.joseki_log = s["joseki_log"]      # 定式去重记录随快照回滚：悔棋重走那手可以重新讲解
        self.st.joseki_counted = s["joseki_counted"]    # 族计数随快照回滚（快照＝这一手之前，故这一手计的那次退回）
        self.st.joseki_dropped = s["joseki_dropped"]    # 已被退回计数的族也随快照回到当时的样子
        self.st.snapshots = self.st.snapshots[:turn_index]
        self.st.bad_moves = [b for b in self.st.bad_moves if int(b["turn_index"]) < turn_index]
        self.st.pre_analysis = None
        # V13：逐手存档 / 亏损手账**不随快照回滚**，但悔掉的那几手必须从两份账上消失——
        # 否则「悔棋 + 重下」会在同一 move_no 留下两条 (move_no, kind, side) 完全相同的记录，
        # 复盘存档分不出哪条有效，losses 还会给出「这一手掉了 30%」这种事实错误的数据。
        # `<=`：move_no 是**落子后**的手数，快照恢复后的 len(st.moves) 就是最后留下的那一手。
        # （memory_log / joseki_log 的既有回滚行为不动）
        keep = len(self.st.moves)
        self.st.move_notes = [nd for nd in self.st.move_notes if int(nd["move_no"]) <= keep]
        self.st.losses = [ls for ls in self.st.losses if int(ls["move_no"]) <= keep]
        # 退回计数：快照里没有、回档前却有 ⇒ 这一手（及其后）计过数的族。把它们记进 joseki_dropped
        # （再计数时会被 joseki_count_entry 摘掉），并把库里的计数退回去。
        rolled = [f for f in dict.fromkeys(old_counted) if f not in self.st.joseki_counted]
        if rolled:
            merged = list(self.st.joseki_dropped)
            for f in rolled:
                if f not in merged:
                    merged.append(f)
            self.st.joseki_dropped = merged
            self._unbump_offloop(rolled)
        self._emit(events.state(self.st.payload()))
        self._emit(events.message(events.AI, "嗯~ 那我们退回第%d手，重新想想…" % len(self.st.moves)))

    def reset_game(self):
        keep_pass = self.st.allow_pass
        self.st.reset()
        self.st.allow_pass = keep_pass
        self.mood.reset()                        # 新一局：情绪回到基线（平静）
        self.sync_mood(True)
        self._emit(events.state(self.st.payload()))
        self._emit(events.message(events.AI, "嗯~ 重新来吧..."))

    def set_allow_pass(self, v):
        self.st.allow_pass = bool(v)
        self._emit(events.message(events.AI, "好~ 那这局我允许自己虚着。" if v else "嗯，这局我不会虚着，会认真陪你下。"))
        self._emit(events.state(self.st.payload()))

    def reveal_move(self):
        """由客户端在她「落子宣告」的语音开始播放时调用：把这一手真正显示到棋盘上。"""
        if not self._reveal_pending:
            return
        self._reveal_pending = False
        self._emit(events.state(self.st.payload()))

    # ================= 复盘模式（v3.7.0） =================

    def set_review(self, on):
        """进入 / 退出复盘模式。

        **不碰 ``busy``**（V15）：``busy`` 为真时也允许进入——否则玩家永远进不去；退出时也不
        擅自把它改回去（复盘期间本来就不该有在途回合，改了反而与「复盘不得改动 busy」冲突）。
        进入那一刻的 ``busy`` 只记进 ``_review_busy_before`` 备查（取舍见任务报告）。

        退出**什么都不用恢复**：``review_snapshot()`` 只重放临时盘面，``st`` 一个字都没改（V8）。
        """
        v = bool(on)
        if v and not self.review:
            self._review_busy_before = bool(self.busy)
        self.review = v

    def review_snapshot(self, n) -> dict:
        """按 ``st.move_seq`` 的前 ``n`` 手重放一份**只读**盘面，返回与 ``payload()`` 同形状的 dict。

        **只读**（V7/V8）：临时盘面、临时列表都是新建的，``st`` 上一个字段都不改——既不临时改
        ``st.moves`` 再改回来（异常路径会留半截状态），也不碰 ``st.finished``；退出复盘因此
        **不需要恢复任何东西**。

        规则与真实对局一致（提子 / 打劫都由 ``GoBoard.play`` 结算）：坐标按 ``move_seq`` 取，
        颜色按交手顺序推（黑先，玩家永远是黑、她永远是白，``st.moves`` 逐手交替）；``pass``
        跳过落子但仍占一手（``move_count`` / ``move_seq`` 与「这一手存在」一致），脏坐标只跳过
        不抛。``n`` 越界夹到 ``[0, len(move_seq)]``（不报错、不 500）；``stones`` 用临时盘面的
        着子表，最后一手按 ``last_played_xy`` 的语义带 ``last=true``。
        """
        seq = list(self.st.move_seq)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        n = max(0, min(n, len(seq)))
        temp = GoBoard()
        last_xy = (-1, -1)
        for i in range(n):
            gtp = str(seq[i])
            if gtp.lower() == "pass":
                temp.pass_move()
                continue
            xy = coords.gtp_to_xy(gtp)
            if xy[0] < 0 or xy[1] < 0:
                continue
            res = temp.play(xy[0], xy[1], "black" if i % 2 == 0 else "white")
            if res.get("ok"):
                last_xy = xy
        return {
            "stones": temp.stones_list(last_xy),
            "move_count": n,
            "move_seq": seq[:n],
            "bad_moves": [dict(b) for b in self.st.bad_moves],
            "allow_pass": self.st.allow_pass,
        }

    def mood_payload(self) -> dict:
        """当前情绪快照（WS 连接建立时随 hello 一起下发）。"""
        info = self.mood.info(self.mood.region())
        return {
            "region": self.mood.region(),
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": str(info.get("filler", "")),
            "burst": str(info.get("burst", "")),
            "pitch": float(info.get("pitch", 0.0)),
        }
