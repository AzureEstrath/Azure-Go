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

from . import coords, events, joseki_gate, prompt_chains, prompts, pv_lines
from .agent_graph import (
    PHASE_AFTER_PLAYER, PHASE_ANALYZE, PHASE_CHAT, PHASE_IDLE, PHASE_JUST_MOVED,
    PHASE_OBSERVE, PHASE_OWN_TURN, PHASE_SUMMARY, build_graph, is_joseki_mistake, _emit_line,
)
from .board import GoBoard, SIZE as BOARD_SIZE
from .config import DRIFT_MAX_LIMIT
from .emotion import EmotionModel
from .event_log import EventLog
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

# —— 回合兜底看门狗（v3.7.0）：`busy` 不归位＝棋盘永久锁死，这是本项目最严重的故障形态 ——
# 值按**最坏一回合的真实上限**推（以 player_move 三阶段为最坏形态），再留余量：
#   ① LLM：一回合真实上限 **7 次** × 单次超时 120s（llm_chain.DEFAULT_TIMEOUT_SEC；ChatOpenAI
#      固定 max_retries=0）＝ 840s。7 次的构成：
#        · ``decide`` 里的工具轮预算 MAX_LLM_CALLS_PER_TURN ＝ 4 次（含工具往返，那个常量只算这一层）；
#        · ``speak`` 阶段的 3 次出话——_speak_after_player / _speak_own_turn / _speak_just_moved
#          各一次。**这三次不在 MAX_LLM_CALLS_PER_TURN 里**，所以真实上限是 4 + 3 ＝ 7 次。
#   ② 引擎：一回合 3 次 analyze（after_player 的「落子前 / 落子后」＋ just_moved 的复算）
#      × 单次 katago_timeout_ms × 引擎内部**两次尝试**（engine.py 的 ``for _ in range(2)``：
#      首次 + 重试 1 次）。**超时按实际配置取**：代码默认 45000ms（v3.8.0 起）⇒ 3 × 45 × 2 ＝ 270s；
#      随包 ``game/config.json`` 仍把它钉在 30000ms ⇒ 3 × 30 × 2 ＝ 180s（下面按**较大的那一侧**算）。
#   ③ 工具轮：max_tool_calls(2) × 单工具超时 TOOL_TIMEOUT_SEC(10s) ＝ 20s
#   真实上限 ≈ 840 + 270 + 20 ＝ **1130s**（≈18.8 分钟；随包 30s 超时下是 1040s，更小）
# 取值：WATCHDOG_SEC ＝ 1380s（23 分钟）＝ 真实上限 1130s × **1.22**，留 ≈250s 余量
# （给事件推送、TTS 合成、情绪小调用、长期记忆 embedding 这些零散等待——它们各自都有超时，
#  但叠起来仍可能吃掉几十秒）。
# 说明：这是**真卡死**的兜底，不是延迟上限——正常偏慢的一回合（几十秒级）离它很远，不会被误伤；
# 而云端 LLM 自己有 120s 超时、引擎自己有 45s（随包配置 30s）×2、工具 10s，所以只有在
# 「等一个永远不来的响应」时才会走到这里。
# ⚠ **1130s 不是严格上界**：它只算「正常编排」那条路。**降级路径**（工具开但链不可用时的
#   ``agent._tool_loop``，模型往返上限 ``MAX_TOOL_ROUNDS = 12``，见 agent.py 常量区）按 12 次
#   LLM × 120s 算可达 ≈1440s，**不计入**上面这个 1130s 的上界（该路径按 AGENTS.md 是「工具开但链
#   不可用」的兜底，本注释只描述主链路的预算）。
WATCHDOG_SEC = 1380

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

# —— 聊天事件（v3.8.0，设计 §6.6）：规则式主题裁切，**默认零 LLM** ——
# 触发点：``chat()`` 里她回复完之后。主题分段＝「时间窗 + 关键词」，摘要是模板句；
# 只有 ``chat_digest_llm=true`` 才用 LLM，且**只在局末 ``summarize()`` / 空闲 ``idle_remark()`` 补做**
# （落子回合内一次都不调，守住第四节性能红线）。
CHAT_TOPIC_WINDOW = 6           # 主题裁切的时间窗：只看最近 6 条玩家发言（与 chat_brief 同量级）
CHAT_TOPIC_SUMMARY_CHARS = 40   # 单段摘要上限（进事件表前截断）
CHAT_DIGEST_MAX_MSGS = 12       # LLM 补做时喂进去的聊天条数上限（可选路径，默认不走）
CHAT_DIGEST_MAX_TOKENS = 200    # LLM 补做的输出上限
CHAT_DIGEST_TIMEOUT_SEC = 10    # LLM 补做的单次超时：它跑在 `_turn_guard` 之外，必须自带上限
# 主题关键词表（按序优先：一条消息命中多类时取最靠前的那一类）；核不上归「闲聊」。
# 这里只是**分类用词**，不进提示词，也不放可被模型照抄的示例句。
CHAT_TOPIC_RULES = (
    ("定式", ("定式", "点三三", "挂角", "夹击", "靠压", "定形", "小飞")),
    ("死活", ("死活", "做活", "杀棋", "打劫", "紧气", "两眼")),
    ("形势", ("形势", "胜率", "领先", "落后", "目差", "厚薄", "大龙")),
    ("布局", ("布局", "大场", "模样", "拆边", "守角", "占角")),
    ("复盘", ("复盘", "回看", "悔棋", "重来")),
    ("心情", ("累", "难过", "开心", "无聊", "喜欢", "心情", "谢谢")),
    ("棋局", ("下棋", "这一手", "棋力", "棋局", "围棋")),
)
CHAT_TOPIC_DEFAULT = "闲聊"


def _chat_topic_of(text):
    """一条消息的主题：命中 ``CHAT_TOPIC_RULES`` 里最靠前的一类，返回 ``(主题, 命中的词)``。"""
    t = str(text or "")
    for topic, words in CHAT_TOPIC_RULES:
        for w in words:
            if w in t:
                return topic, w
    return CHAT_TOPIC_DEFAULT, ""


def chat_topics(history, window=CHAT_TOPIC_WINDOW):
    """规则式主题裁切：把最近聊天按关键词分段，返回 ``[(主题, 摘要)]``（旧 → 新）。

    - **时间窗**：``chat_history`` 里没有墙钟，用「最近 ``window`` 条**玩家**发言（连同其后的回复）」
      当窗口（与 ``chat_brief`` 的 6 条同量级）；窗口取的是**从开局起**那段历史的尾部，覆盖不止这一轮；
    - **关键词 / 相似度**：玩家发言命中 ``CHAT_TOPIC_RULES`` 里最靠前的一类即为该段的主题，
      核不上归「闲聊」；主题相同的连续玩家发言并成一段（相似度＝同类关键词）；
    - **两侧都用**（设计 §6.6：输入＝这轮玩家消息 **+ 她的回复**）：主题由玩家发言定，
      但段的摘要同时收两侧命中的关键词 ——「他说到 X」用玩家那侧、「我聊到 Y」用她那侧；
      她没回关键词时写成「我回应了」（**与有没有她的回复无关地稳定**，故去重不会被回合数打散）；
    - **纯函数、零 LLM**：不碰 ``self``，可直接在事件写入路径上调用。
    """
    msgs = [m for m in (history or [])
            if isinstance(m, dict) and str(m.get("role", "")) in ("user", "assistant")]
    uidx = [i for i, m in enumerate(msgs) if str(m.get("role", "")) == "user"]
    if window and window > 0 and len(uidx) > window:
        msgs = msgs[uidx[-int(window)]:]          # 窗口从「第 window 近的玩家发言」起，带上其后回复
    segs = []                                     # [主题, [他那侧的关键词], [她那侧的关键词]]
    for m in msgs:
        role = str(m.get("role", ""))
        topic, kw = _chat_topic_of(str(m.get("content", "")))
        if role == "user":
            if segs and segs[-1][0] == topic:
                if kw and kw not in segs[-1][1]:
                    segs[-1][1].append(kw)
            else:
                segs.append([topic, [kw] if kw else [], []])
        else:                                     # 她的回复：并进当前段（段还没起就从「闲聊」起）
            if not segs:
                segs.append([CHAT_TOPIC_DEFAULT, [], []])
            if kw and kw not in segs[-1][2]:
                segs[-1][2].append(kw)
    out = []
    for topic, his, mine in segs:
        his_s = ("他说到%s" % "、".join(his)) if his else "他随口聊了几句"
        mine_s = ("我聊到%s" % "、".join(mine)) if mine else "我回应了"
        out.append((topic, prompts._one_line("%s，%s" % (his_s, mine_s), CHAT_TOPIC_SUMMARY_CHARS)))
    return out


def _discard_task(task):
    """旧一代任务跑完时的收尾：只把异常取走（免得 asyncio 报「exception was never retrieved」）。

    看门狗**有意不取消**旧任务（见 ``AzureAgent._turn_guard``），所以它迟早会自己跑完；这里只是
    把它挂在 ``add_done_callback`` 上做一次收尾，不改变任何结果。
    """
    if not task.cancelled():
        task.exception()


# 定式提示频率的默认值（与 ``config.AzureConfig.joseki_rate`` 的字段默认一致）。
# **只有一个默认值、只有一条路径**：``p = clamp(rate + skill_adj + mood_adj, 0, 1)``——
# 没有「未接线就跳过频率层」这第二种模式（漏接线不该静默退回「每次都教」）。
JOSEKI_RATE_DEFAULT = 0.5

# 预测线 A/B/C 那一次分析的深度**下限**（与 ``config.AzureConfig.pv_max_visits`` 的字段默认一致）。
# 只作用于「玩家落子后局面」那一次分析（``agent_graph._prepare_after_player``），且**只增不减**：
# 调用点取 ``max(它, 引擎的 max_visits)``——三线只吃既有 ``moveInfos``（零额外调用），但要求更深的
# 搜索才拿得到足够长的 ``pv``；引擎默认本来就更深时，绝不把这一次砍浅。
PV_MAX_VISITS_DEFAULT = 150


def _clamp_rate(v):
    """定式提示频率的安全取值：夹到 ``[0, 1]``；转不出来（脏值）时退回默认 ``0.5``。

    口径与 ``config.AzureConfig._apply`` 一致（键缺失 / 脏值 → 用默认），脏值不抛、不把 agent 构造搞崩。
    """
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return JOSEKI_RATE_DEFAULT


# —— 情绪波动（v3.8.0）：开启后她**小概率**走偏优解（设计 §6.2）——
# 三个字段的代码默认与 ``config.AzureConfig`` 的同名字段一一对应（键缺失 / 脏值 → 用这些默认）。
DRIFT_BASE_DEFAULT = 0.03        # 基础偏航概率（平静时的 p_drift）
DRIFT_MAX_DEFAULT = 0.08         # p_drift 上限：再躁也不超过它，免得变成「乱下」
PV_MAX_DROP_DEFAULT = 0.05       # 偏航线与最优线的胜率差上限（超了这条线整条不用）
# 阶梯的切分：``p_drift`` 里 2/3 给 B、其余给 C ⇒ P(A) = 1-p > P(B) > P(C)（越偏越少，设计 §6.2）。
# ⚠ 这条不变量只在 ``p < 0.6`` 时成立（``P(A) = 1-p`` 要大于 ``P(B) = 2p/3``）——所以
# ``drift_max`` 必须被夹在「小概率」范围内（``config.DRIFT_MAX_LIMIT = 0.2``，修复轮 1 / Minor #3）：
# ``p ≤ 0.2 ⇒ P(A) = 1-p ≥ 0.8 > P(B) = 2p/3 ≤ 0.133 > P(C) = p/3 ≤ 0.067``。
DRIFT_B_TO_C_SPLIT = 2.0 / 3.0
# 情绪偏移的映射（设计 §八 把形状留给建造者）：
#   基线（= ``EmotionModel.BASELINE``，即「平静」）⇒ 偏移 0；越偏离基线越容易走偏。
#   三个权重只吃**既有 mood 三维**：愉悦度低（失落 / 难过）/ 好奇度高（好奇 / 兴致）/
#   唤醒度高（激动 / 烦躁）各占一份；分数到 ``DRIFT_SCORE_FULL`` 就吃满 ``drift_max``。
#   **引用 EmotionModel 的类级常量**，不抄字面量副本（修复轮 1 / Minor #2：抄副本会各自漂移）。
DRIFT_MOOD_BASELINE = EmotionModel.BASELINE
DRIFT_MOOD_WEIGHTS = (0.45, 0.20, 0.35)
DRIFT_SCORE_FULL = 0.5


def _clamp_unit(v, default):
    """``[0, 1]`` 的安全取值：键缺失 / 脏值 → ``default``（不抛，不把 agent 构造搞崩）。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return float(default)


def _as_float(v, default=0.0) -> float:
    """取一个 float：缺失 / 脏值 → ``default``（不抛）。

    偏航链路上「胜率差」要在两处读（挑线时判上限、记账时写进事件文本），共用这一处解析
    （修复轮 1 / Minor #6：原先两处各抄了一份 try/except）。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _mood_dim(mood, key):
    """取 mood 的一维（``v`` / ``c`` / ``a``）：非 dict / 缺失 / 脏值 → ``None``（「这一维取不到」）。

    只读既有维度、绝不抛——情绪取不到就不偏航，不允许它把对局搞坏。
    """
    if not isinstance(mood, dict):
        return None
    try:
        return max(0.0, min(1.0, float(mood[key])))
    except (KeyError, TypeError, ValueError):
        return None


def emotion_bias(mood, drift_max=DRIFT_MAX_DEFAULT, baseline=DRIFT_MOOD_BASELINE) -> float:
    """情绪偏移：只吃既有 mood 三维（``v`` / ``c`` / ``a``），返回 ``[0, drift_max]``。

    ``score = 0.45*(0.58-v) + 0.20*(c-0.55) + 0.35*(a-0.30)``——基线「平静」恰为 ``0``，
    情绪越低落 / 越好奇 / 越激动，分数越高（三份权重见 ``DRIFT_MOOD_WEIGHTS``）；再按
    ``DRIFT_SCORE_FULL`` 归一、夹到 ``[0, drift_max]``。

    ``baseline`` 默认取 ``EmotionModel.BASELINE``；调用方传 ``self.mood.baseline`` 时，
    实例自定义过的基线也照用（不抄字面量副本，修复轮 1 / Minor #2）。

    非 dict / 任一维缺失或脏值 ⇒ ``0.0``（**不是**负数：波动只往上加，平静时就是 ``drift_base``）。
    """
    v = _mood_dim(mood, "v")
    c = _mood_dim(mood, "c")
    a = _mood_dim(mood, "a")
    if v is None or c is None or a is None:
        return 0.0
    base = baseline if isinstance(baseline, (list, tuple)) and len(baseline) == 3 else DRIFT_MOOD_BASELINE
    score = (DRIFT_MOOD_WEIGHTS[0] * (base[0] - v)
             + DRIFT_MOOD_WEIGHTS[1] * (c - base[1])
             + DRIFT_MOOD_WEIGHTS[2] * (a - base[2]))
    if score <= 0.0:
        return 0.0
    try:
        top = max(0.0, float(drift_max))
    except (TypeError, ValueError):
        top = DRIFT_MAX_DEFAULT
    return top * min(1.0, score / DRIFT_SCORE_FULL)


def _rng_random(rng) -> float:
    """从注入的随机源取一个 ``[0, 1)`` 的数；取不到（``None`` / 脏替身）⇒ ``1.0``（安全侧：不偏航）。"""
    try:
        return float(rng.random())
    except Exception:                   # noqa: BLE001 —— 随机源坏掉绝不抛，宁可恒走 A
        return 1.0


class AzureAgent:
    # 回合兜底看门狗（v3.7.0）：算式与取值见模块顶部的 ``WATCHDOG_SEC`` 注释。
    # 实例上另有 ``self.watchdog_sec``（可注入/可覆盖，测试用它把等待压到毫秒级）。
    WATCHDOG_SEC = WATCHDOG_SEC

    def __init__(self, engine, llm, bus=None, joseki_teach=True, limit_len=False,
                 tools_enabled=False, max_tool_calls=None, memory=None, stream_tokens=False,
                 tts=None, joseki_progress=None, joseki_rate=JOSEKI_RATE_DEFAULT, joseki_rng=None,
                 pv_max_visits=PV_MAX_VISITS_DEFAULT, emotion_drift=False,
                 drift_base=DRIFT_BASE_DEFAULT, drift_max=DRIFT_MAX_DEFAULT,
                 pv_max_drop=PV_MAX_DROP_DEFAULT, drift_rng=None, chat_digest_llm=False):
        self.st = GameState()
        self.katago = engine
        self.llm = llm
        self.bus = bus if bus is not None else events.EventBus()
        self.mood = EmotionModel()

        self.joseki_teach = joseki_teach
        self.limit_len = limit_len
        self.busy = False

        # —— 回合代数（v3.7.0）：每一次公开编排调用开启新一代，本回合的所有写入都带这一代的 token ——
        #    看门狗超时 / 新的公开调用都会把它前推，旧一代的 ``epoch_ok`` 随即全部为假 ⇒
        #    它之后的一切 ``st.*`` 写入与事件发射一律作废（见 ``agent_graph`` 的守卫）。
        self.epoch = 0
        self._epoch_token = 0       # 当前在途那一代的 token（``begin_epoch()`` 刷新）
        # 看门狗秒数：取模块常量，实例上可覆盖（测试用极小值，绝不真的等 23 分钟）
        self.watchdog_sec = float(self.WATCHDOG_SEC)

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

        # —— 定式提示门控（T5 / v3.7.0）：资格层 + 频率层 ——
        #    ``joseki_rate`` 是**基础频率**（0~1，默认 ``JOSEKI_RATE_DEFAULT = 0.5``，与 config 字段一致）；
        #    永远只有一条路径：``p = clamp(rate + skill_adj + mood_adj, 0, 1)``。生产由 main.py 传
        #    ``cfg.joseki_rate``；**测试装配要「必定讲」就显式传 ``1.0`` + 注入固定 rng**（画像 / 情绪的
        #    负浮动会把 ``p`` 压到 1 以下、仍会掷一次；见 ``tests/helpers.ZeroRng``）。
        #    ``joseki_rng`` 是**注入的**随机源：默认自带一个 ``random.Random()``，单测注入固定替身。
        #    冷却表 ``_joseki_taught``（族 → 最近讲过的手数）纯局内，不落盘、随新一局清空。
        self.joseki_rate = _clamp_rate(joseki_rate)
        self.joseki_rng = joseki_rng if joseki_rng is not None else random.Random()
        self._joseki_taught = {}

        # —— 预测线 A/B/C（v3.8.0）：只给「玩家落子后局面」那一次分析加深度（零额外引擎调用）——
        #    它是**下限**（只增不减，调用点取 max(它, 引擎默认)），与 config 的 ``pv_max_visits``
        #    同义；``None`` / 脏值一律退回默认，绝不因一个旋钮把 agent 构造搞崩。
        try:
            self.pv_max_visits = max(1, int(pv_max_visits))
        except (TypeError, ValueError):
            self.pv_max_visits = PV_MAX_VISITS_DEFAULT

        # —— 情绪波动（v3.8.0）：小概率走偏优解，**默认关**（设计 §6.2）——
        #    关闭时逐项与现状一致：不解析线、不掷骰、不记事件（``_drift_pick`` 第一行就返回）。
        #    ``emotion_drift`` 是开关；``drift_base`` / ``drift_max`` 定 ``p_drift = clamp(base+bias, 0, max)``；
        #    ``pv_max_drop`` 是偏航线与最优线的胜率差上限（超了整条不用）。
        #    ``drift_max`` 夹到**小概率上限** ``DRIFT_MAX_LIMIT``（0.2，不是 1.0），``drift_base`` 再夹到
        #    ``[0, drift_max]``（修复轮 1 / Minor #3：p 太大时阶梯不变量会反转、甚至恒偏航）；
        #    三个数值的脏值都退回默认——与 ``config.AzureConfig`` 同口径（这里的上限是**最后一道**防线，
        #    即使有人直接构造 agent 或运行时改属性，``_drift_probability`` 也不会把 p 放到 1.0 以上）。
        #    ``drift_rng`` 是**注入的**随机源（生产用自带 ``random.Random()``，单测注入固定替身）。
        self.emotion_drift = bool(emotion_drift)
        self.drift_max = min(_clamp_unit(drift_max, DRIFT_MAX_DEFAULT), DRIFT_MAX_LIMIT)
        self.drift_base = min(_clamp_unit(drift_base, DRIFT_BASE_DEFAULT), self.drift_max)
        self.pv_max_drop = _clamp_unit(pv_max_drop, PV_MAX_DROP_DEFAULT)
        self.drift_rng = drift_rng if drift_rng is not None else random.Random()
        # 本轮偏航的**待记账**信息（修复轮 1 / Important #1）：``_drift_pick`` 只填它、不写表；
        # 落点定案并提交之后由 ``_record_drift`` 消费（一次性，绝不跨回合残留）。
        self._drift_pending = None

        # —— 局内事件表（v3.8.0，设计 §6.3）：**局内内存**、不进 GameState 快照、不随悔棋回退 ——
        #    写入点两处：「走偏优解」（``_record_drift``）与「聊过天」（``_record_chat_events``）；
        #    ``reset_game`` 清空（新一局不带旧账）。
        self.event_log = EventLog()

        # —— 聊天事件（v3.8.0，设计 §6.6）：规则式主题裁切**默认零 LLM** ——
        #    关闭（默认）时 ``chat()`` 之后只走规则式；开启后**只在局末 / 空闲**补一次 LLM 分段总结
        #    （``_digest_chat_llm``），落子回合内一次都不调（性能红线）。
        self.chat_digest_llm = bool(chat_digest_llm)

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

    # ================= 回合代数与兜底看门狗（v3.7.0） =================

    def begin_epoch(self) -> int:
        """开启新一代（一次公开编排调用＝一代）：自增并返回新代数，同时记住它是在途那一代。

        ``player_move`` 的三个阶段共用一个 token；「新的公开调用」与「看门狗超时」都会前推代数，
        让上一代的 ``epoch_ok`` 变成假。
        """
        self.epoch += 1
        self._epoch_token = self.epoch
        return self.epoch

    def epoch_ok(self, token) -> bool:
        """``token`` 是否仍属于**当前**这一代（超时 / 新回合都会让旧一代作废）。

        取不到 token（``None`` / 脏值）按「不属于」处理——宁可不写，也不让一个来路不明的写入
        落到盘面上。
        """
        try:
            return int(token) == self.epoch
        except (TypeError, ValueError):
            return False

    def turn_alive(self) -> bool:
        """**在途那一代**是否仍然有效（看门狗超时后为假）。

        给拿不到本轮 ``data`` 的少数调用点用（流式 ``token`` 事件、情绪小调用、悔棋的退回计数）：
        它们只该在「本回合还没被作废」时动手。新的公开调用会 ``begin_epoch()`` 刷新它，所以
        ``undo`` / ``reset_game`` 这类**新的用户动作**不受上一轮超时的影响（见那两处的注释）。
        """
        return self.epoch_ok(self._epoch_token)

    def _watchdog_fire(self):
        """看门狗触发：解锁 + **前推一代** + 一行日志 + 一条可读提示（走既有 ``message`` 通道）。

        「前推一代」是这里的关键，也是「超时不再放开并发」的全部含义：解锁会让玩家立刻可以开始
        新的一手，而在途那一代（可能还卡在某个等待里、也可能刚恢复）的 ``epoch_ok`` 已经全部为假，
        它之后的一切写入与发话都会被守卫拦掉——解锁因此不会让两代抢写同一份 ``st``。

        解锁走 ``_set_busy``（自带去重）：随后公开方法的 ``finally`` 再调一次不会多发 ``busy`` 事件。
        """
        self.epoch += 1
        self._set_busy(False)
        print("[Agent] 回合兜底超时（%g 秒）：已解锁棋盘，这一回合的后续写入一律作废。" % float(self.watchdog_sec))
        self._emit(events.message(events.AI, "唔…这一手我算得太久了，先接着下吧。"))

    async def _turn_guard(self, coro):
        """最外层看门狗：跑完一次公开编排；超时则解锁 + 前推一代，本回合余下的写入全部作废。

        返回编排结果；**超时返回 ``None``**（调用方据此放弃本回合的后续阶段，但**不抛异常**——
        真正的编排异常照旧由调用方的 ``except`` 兜住，行为与没有看门狗时一致）。

        **有意不取消**旧任务：它可能卡在不可取消的等待里（引擎 / 云端 / 工具），``wait_for`` 式的
        取消只会把半截状态留在半路、还会把 CancelledError 抛进本来就在降级的外部调用；让它自己跑完、
        用代际守卫把它的写入全部拦掉，既简单又不会留下更坏的状态。旧任务跑完即自行结束
        （异常由 ``_discard_task`` 收走，不会变成「never retrieved」告警）。
        """
        task = asyncio.ensure_future(coro)
        done, _pending = await asyncio.wait({task}, timeout=float(self.watchdog_sec))
        if not done:
            self._watchdog_fire()
            task.add_done_callback(_discard_task)
            return None
        return task.result()

    async def _turn(self, phase, data=None, turn_id=None):
        """跑一次回合状态图（一个阶段），返回终态。

        编排顺序（引擎调用 / 提示词 / 台词 / 事件 / 记忆标签）全在 ``agent_graph`` 的四个节点里；
        这里只把「阶段 + 棋谱快照 + 中间数据」交出去。多阶段回合（``player_move``）由调用方依次
        跑，``turn_id`` 一路沿用（流式 token 与重复发话的判据靠它）。

        本回合的**代数**（v3.7.0）随 ``data`` 一路带下去：节点里每一个写 ``st.*`` / 发事件之前都要
        拿它核对一次（见 ``agent_graph._alive``）。跨阶段的 ``data`` 沿用第一阶段的那个 token，
        所以 ``player_move`` 的三次 ``ainvoke`` 属于同一代。
        """
        d = dict(data or {})
        d.setdefault("epoch", self._epoch_token)
        state = {
            "phase": str(phase),
            "moves": list(self.st.moves),
            "messages": [],
            "reply": "",
            "tool_calls": 0,
            "wants_tools": False,
            "move": None,
            "turn_id": str(turn_id or self.turn_id or ""),
            "data": d,
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

        v3.7.0 看门狗守卫：在途那一代已被作废（看门狗刚超时）时直接返回——它的心情结算与 ``mood``
        事件都属于那一代，跟着作废（``busy`` 那时已是 False，光靠上面那条判据拦不住）。
        """
        if self.llm is None or self._mood_desc == "" or self.busy:
            return
        if not self.turn_alive():
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

    def _joseki_emit(self, r, show_card=False, mistake=None, epoch=None):
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

        ``epoch``（v3.7.0）：看门狗守卫——带了它且已经不属于当前这一代时，这条棋盘注记不发。
        调用点都从本轮 ``data`` 里传 ``epoch``；不传（既有单测的直接调用）时行为与以前逐字一致。
        """
        if epoch is not None and not self.epoch_ok(epoch):
            return
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

    # —— 定式提示门控（T5 / v3.7.0）：资格层（``joseki_show_card``）+ 频率层（``joseki_gate_show``）——

    async def _joseki_seen(self, family) -> int:
        """该族历史出现次数；未接线 / 族 id 缺失 / 读不出来一律 ``0``（＝「第 1 次」，fail-open）。

        同步磁盘读走 ``asyncio.to_thread``（慢库不得冻住事件循环）；``JosekiProgress.seen`` 自己吞掉
        异常并降级为 0，这里再兜一层脏值（替身 / 将来实现给了 ``None`` 之类）。
        """
        fam = str(family or "")
        if self.joseki_progress is None or fam == "":
            return 0
        try:
            return max(0, int(await asyncio.to_thread(self.joseki_progress.seen, fam)))
        except (TypeError, ValueError):
            return 0

    def _joseki_mistake_of(self, jk, mistake) -> bool:
        """按 ``is_joseki_mistake`` 的口径重算「走错」（**不**直接采信 ``mistake["is_mistake"]``）。

        与卡片门禁同一口径：``kind != deviated`` 或 ``mistake`` 形状不对 ⇒ ``False``；判定抛异常
        （脏 ``drop``）⇒ ``False`` + 一行日志，绝不把这一回合打挂。
        """
        if str((jk or {}).get("kind", "")) != "deviated":
            return False
        if not isinstance(mistake, dict):
            return False
        try:
            return is_joseki_mistake(jk, bool(mistake.get("on_line")),
                                     float(mistake.get("drop") or 0.0),
                                     bool(mistake.get("rank_ok")))
        except Exception as e:                  # noqa: BLE001 —— 脏 mistake 绝不许把这一回合打挂
            # 保守取向（与整条链一致）：判不出来就**当没走错**——既不误判「走错」、也不额外打扰玩家；
            # 只留一行日志，对局照常。
            print("[Joseki] 走错判定失败（按「不算走错」保守处理，不影响对局）：%s" % e)
            return False

    async def _joseki_eligibility(self, jk, mistake):
        """资格层一次算完：返回 ``(该不该教, seen, is_mistake)``。

        **同一手只读一次族计数、只判一次走错**（修复轮 1 / 评审 Minor #2）：``joseki_show_card`` 与
        ``joseki_gate_show`` 都从这里取结论，真库下每手只多一次 SQLite 读（识别链路不在热路径上多做东西）。
        读序与以前一致：先 ``seen`` 再判走错；脏 ``mistake`` 的那一行日志也仍然只打一次。
        """
        seen = await self._joseki_seen(jk.get("family"))
        is_mistake = self._joseki_mistake_of(jk, mistake)
        return (joseki_gate.eligible(seen, str(jk.get("kind", "")), is_mistake,
                                     bool(self.joseki_teach)), seen, is_mistake)

    async def joseki_show_card(self, jk, mistake=None) -> bool:
        """**资格层**（T5 / v3.7.0）：这条定式**该不该教**（只读：不改 ``st``、不碰计数）。

        判定并入 ``joseki_gate.eligible``：同一族**已教过**次数（跨局）``seen < 3`` ⇒ 有资格；
        第 4 次起只有「走错」（``kind == "deviated"`` 且 ``is_joseki_mistake(...)``：真亏**且**不在
        库线上）才有资格；``joseki_teach=false`` ⇒ 一律无资格。``joseki_progress`` 未接线 / 族 id 缺失
        （旧库、异常形状）⇒ **保守地有资格**（宁可多教一次）；次数读不出来时 ``JosekiProgress.seen``
        自己降到 ``0``（当成第 1 次），所以坏库的表现就是「每次都教」，绝不影响对局。

        顺序固定「**先问后记**」——自增在 ``joseki_count_entry`` 里（且只在真的讲了时才记，见
        ``agent_graph`` 的两个调用点）；若先自增再问，第一次出现时 ``seen`` 已是 1，「前 3 次」就只
        成立 2 次。

        ``mistake`` 是 ``data["jk_mistake"]`` 那个 dict（键 ``is_mistake/on_line/gtp/x/y/drop/rank_ok``）
        ——这里按 ``is_joseki_mistake`` 的口径重算，而不是直接采信 ``is_mistake``。

        注：本方法自 T5 起只回答**资格层**（频率层在 ``joseki_gate_show``）；返回值同时用作卡片字段
        ``data["jk_show_card"]`` 的前半段（命中频率层才真的上卡片）。
        """
        if not jk:
            return False
        return (await self._joseki_eligibility(jk, mistake))[0]

    async def joseki_gate_show(self, jk, mistake=None, epoch=None) -> bool:
        """**门控总出口**（T5 / v3.7.0）：这一手该不该讲（资格层 + 频率层；只读，不改任何状态）。

        只决定**文本与卡片**：不改棋子、不改 ``message`` / ``speech`` 配对、不改 ``busy``、不改状态机。
        **未命中 ⇒ 与「没识别到定式」逐字段同构**（不换讲解模板、不上卡片、不发 ``joseki`` 事件）。

        决策顺序（见 ``joseki_gate``）：资格层 → 走错必讲（绕过频率层与冷却）→ 同族冷却 →
        掷 ``p = clamp(rate + skill_adj + mood_adj, 0, 1)``（**唯一路径**；``p >= 1`` 直接讲、不掷 rng，
        ``p <= 0`` 直接不讲）。画像只吃**局内信号**（``st.player_ranks`` + 该族计数），情绪吃当前区域；
        两者取不到 → 0。命中时记下「这一族最近讲过的手数」供冷却用；每次决策留**一行**日志
        （rate / adj / roll / 命中）。

        ``epoch``（v3.7.0）：看门狗守卫——带了它且已经不属于当前这一代时直接不放行（冷却表也不写）。
        **两处**核对：入口一次（省掉无谓的资格层查询），资格层的 ``to_thread`` 之后、写冷却表之前
        再一次（v3.7.0 修复波）——冷却表绝不能由「会被调用方丢弃的那一代」写入。
        """
        if not jk or not self.joseki_teach:
            return False
        if epoch is not None and not self.epoch_ok(epoch):
            return False
        kind = str(jk.get("kind", ""))
        family = str(jk.get("family") or "")
        # 资格层一次算完（修复轮 1 / Minor #2）：族计数只读一次、走错只判一次，供下面复用
        ok, seen, is_mistake = await self._joseki_eligibility(jk, mistake)
        if not ok:
            return False
        move_no = len(self.st.moves)
        sk = joseki_gate.skill_adj(list(self.st.player_ranks), seen)
        md = joseki_gate.mood_adj(self.mood_payload())
        d = joseki_gate.decision(
            rng=self.joseki_rng, rate=self.joseki_rate, skill_adj=sk, mood_adj=md,
            seen=seen, kind=kind, mistake=is_mistake, teach_on=True,
            last_taught_move=self._joseki_taught.get(family), move_no=move_no)
        print("[Joseki] 定式提示门控：rate=%.2f skill=%+.2f mood=%+.2f p=%.2f roll=%s 冷却=%s 命中=%s"
              "（%s / %s / 第%d手）"
              % (self.joseki_rate, sk, md, d["p"],
                 "-" if d["roll"] is None else "%.3f" % d["roll"],
                 "是" if d["cooldown"] else "否", "是" if d["show"] else "否",
                 family or "-", kind or "-", move_no))
        if d["show"] and family != "":
            # 看门狗守卫（v3.7.0 修复波）：上面的资格层会 ``to_thread`` 读族计数（慢库 / 慢磁盘），
            # 这一段 ``await`` 之后代数可能已经失效——**冷却表不许由会被丢弃的那一代写入**：调用方
            # （``agent_graph`` 的 ``_alive``）会把计数与事件一起丢掉，但 ``_joseki_taught`` 一旦记下，
            # 这一族在冷却窗口（8 手）内就会被静默压掉一次讲解。「命中才记」的语义不变，只是把
            # 「记」推到代数复核之后。
            if epoch is not None and not self.epoch_ok(epoch):
                return False
            self._joseki_taught[family] = move_no        # 冷却起点：这一族最近讲过的手数
        return bool(d["show"])

    async def joseki_count_entry(self, jk, epoch=None) -> None:
        """记一次「这一族**被讲了**」（**同族同局最多计 1 次**；只写计数，不发事件、不决定卡片）。

        **只在门控命中（真的讲了）时调用**（修复轮 1 / 评审 Important #1）：资格层的 ``seen < 3``
        语义是「**教过**几次」而不是「遇到几次」——按遭遇计数会让频率层把「前 3 次」的教学预算
        静默吃掉（默认 0.5 下前 3 次遭遇期望只讲约 1.5 次，之后除走错就再也不讲）。走错那条路
        （必讲、绕过频率层）也是一次讲解，照旧计数。

        先把族落进 ``st.joseki_counted``（并把它从 ``st.joseki_dropped`` 里摘掉——新的一集开始），
        再 ``bump``。磁盘 IO 走 ``to_thread``（慢库不得冻住事件循环）；失败也只是丢这一次记忆——
        ``JosekiProgress`` 内部已吞掉异常并降级返回旧值，这里绝不抛、绝不影响对局。

        ``epoch``（v3.7.0）：看门狗守卫——带了它且已经不属于当前这一代时，这一族的计数（内存与
        磁盘）都不再写。调用点都从本轮 ``data`` 里传；不传时行为与以前逐字一致。
        """
        if not jk or self.joseki_progress is None:
            return
        if epoch is not None and not self.epoch_ok(epoch):
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

        v3.7.0 看门狗守卫：在途那一代已经被作废时（看门狗刚超时、玩家随即悔棋）这一笔退回计数
        一并作废——这一局的族计数与盘面同属一代，不该由一代已死的动作去改。``undo`` 自己会先
        ``begin_epoch()``，所以正常的悔棋永远走得到下面（守卫只在「旧一代还在收尾」时拦下）。
        """
        if self.joseki_progress is None:
            return
        if not self.turn_alive():
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
        token = self.begin_epoch()                  # 三阶段共用同一代（看门狗超时后整代作废）
        try:
            await self._turn_guard(self._player_move_turns(x, y, token))
        except Exception as e:                      # 编排意外抛出（LLM/引擎）：不得让 busy 永久为真
            print("[Agent] 落子编排异常：%s" % e)
        finally:
            self._set_busy(False)

    async def _player_move_turns(self, x, y, token):
        """``player_move`` 的三个阶段（同一个 epoch，交给 ``_turn_guard`` 整体兜底）。

        阶段之间核对一次代数：看门狗一触发，这一代就作废了，剩下的阶段一个都不许再跑
        （否则她会在玩家已经重新落子之后，拿旧局面继续下她那一手）。
        """
        state = await self._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y})
        if not self.epoch_ok(token):
            return
        state = await self._turn(PHASE_OWN_TURN, state.get("data"), state.get("turn_id"))
        if not self.epoch_ok(token):
            return
        await self._turn(PHASE_JUST_MOVED, state.get("data"), state.get("turn_id"))

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

    # ================= 情绪波动：小概率走偏优解（v3.8.0，默认关） =================

    def _drift_pick(self, move_infos, pick) -> list:
        """情绪波动的落点定案：**选出 A 之后、真正落子之前**（设计 §6.2）。

        只吃**既有那一次**分析的 ``moveInfos``（``pv_lines.extract_lines`` 是纯函数，零引擎调用）；
        **关闭（默认）时原样返回 ``pick``**——不解析线、不掷骰、不记事件，与没有这个功能时逐项一致。

        **只返回落点、不写事件表**（修复轮 1 / Important #1）：选中 B/C 时把「待记账」信息放进
        ``self._drift_pending``（由 ``_take_drift_pending`` 取走交给本轮 ``data``），事件改由
        ``_record_drift`` 在**落点定案并提交之后**写——工具轮重定案 / 回落到 A / 本回合被作废，
        三种情形都不会留下与实际落点不符的事件。
        """
        self._drift_pending = None
        if not self.emotion_drift:
            return pick
        lines = pv_lines.extract_lines(list(move_infos or []), max_drop=self.pv_max_drop)
        mood = self.mood_payload()
        letter = self._maybe_drift(lines, mood, self.drift_rng)
        if letter == "a":
            return pick
        line = lines.get(letter) or {}
        moves = list(line.get("moves") or [])
        gtp = str(moves[0]) if moves else ""
        xy = coords.gtp_to_xy(gtp)
        # 二次核对（``_maybe_drift`` 已判过一次）：任何意外都回到既有落点，绝不下出非法手
        if xy[0] < 0 or xy[1] < 0 or not self.st.board.is_legal(xy[0], xy[1], "white")["ok"]:
            return pick
        a_moves = list((lines.get("a") or {}).get("moves") or [])
        self._drift_pending = {
            "line": letter,
            "gtp": gtp,
            "best": str(a_moves[0]) if a_moves else "",
            "drop": _as_float(lines.get("drop"), 0.0),
            "mood": str(mood.get("name") or "") if isinstance(mood, dict) else "",
        }
        return [gtp, xy[0], xy[1]]

    def _take_drift_pending(self):
        """取走本轮待记账的偏航信息（``None`` ＝ 本轮没偏航）；**一次性**：取走即清空。"""
        pending = self._drift_pending
        self._drift_pending = None
        return pending

    def _maybe_drift(self, lines, mood, rng) -> str:
        """按阶梯概率在 A / B / C 里挑一条线，返回线名 ``"a"`` / ``"b"`` / ``"c"``。

        概率：``p = p_drift(mood)`` 里 ``2/3`` 给 B、其余给 C ⇒ ``P(A) > P(B) > P(C)``（越偏越少；
        ``p`` 被 ``drift_max ≤ DRIFT_MAX_LIMIT`` 兜着，所以 ``P(A) ≥ 0.8``）。
        **安全缺省**（宁可走 A 也不许下出烂棋，设计 §三）：开关关闭 / 线缺失 / 胜率差超
        ``pv_max_drop`` / 首手不是当前棋盘的合法点 —— 一律**逐级回落**（``c → b → a``），
        最终必是 ``"a"``（调用方随即保留既有 ``_pick_ai_move`` 的结果）。
        """
        if not self.emotion_drift:
            return "a"
        if not isinstance(lines, dict):
            return "a"
        a = lines.get("a")
        if not self._line_usable(a):
            return "a"                      # 连 A 都取不到：无从偏航
        drop = _as_float(lines.get("drop"), 0.0)
        if drop > self.pv_max_drop:
            return "a"                      # 偏得太远：这一批线整条不用
        p = self._drift_probability(mood)
        if p <= 0.0:
            return "a"
        r = _rng_random(rng)
        if r < p * DRIFT_B_TO_C_SPLIT:
            want = "b"
        elif r < p:
            want = "c"
        else:
            return "a"
        for letter in (want, "b", "a"):     # C → B → A 逐级回落
            if self._line_usable(lines.get(letter)):
                return letter
        return "a"

    def _drift_probability(self, mood) -> float:
        """``p_drift = clamp(drift_base + emotion_bias(mood), 0, drift_max)``（设计 §6.2）。

        基线取**这一局情绪模型的实例基线**（``self.mood.baseline``）——实例自定义过基线也照用。
        """
        p = self.drift_base + emotion_bias(mood, self.drift_max, self.mood.baseline)
        if p < 0.0:
            return 0.0
        return self.drift_max if p > self.drift_max else p

    def _line_usable(self, line) -> bool:
        """该线首手在**当前棋盘**上可用（复用 ``_pick_ai_move`` 的既有合法性判据）。

        虚着**不算**可用：偏航只挑实落点，宁可走 A（``_pick_ai_move`` 自己会在允许时虚着）。
        """
        moves = line.get("moves") if isinstance(line, dict) else None
        if not isinstance(moves, (list, tuple)) or not moves:
            return False
        gtp = str(moves[0] or "")
        if gtp == "" or gtp.lower() == "pass":
            return False
        xy = coords.gtp_to_xy(gtp)
        if xy[0] < 0 or xy[1] < 0:
            return False
        return bool(self.st.board.is_legal(xy[0], xy[1], "white")["ok"])

    def _record_drift(self, drift, gtp) -> bool:
        """落点**定案并提交**之后记账（修复轮 1 / Important #1）：只有最终落点就是那个偏航点时才写。

        事件原文是**给模型看的素材**：永不进台词、不产生 ``speech``。手数取**提交之后**的
        ``len(st.moves)``——她这一手的 1 基序号（与 ``note_move`` 同义）。

        三种情形都**不留**事件（事件与事实必须一致：它是 P5 事件区块喂给模型的素材）：
        工具轮把落点重定案 / 回落到 A / 本回合被看门狗作废（调用点在 ``_alive`` 守卫之后）。
        返回是否真的写了（便于门卫断言）。
        """
        if not isinstance(drift, dict) or str(drift.get("gtp") or "") != str(gtp or ""):
            return False
        move_no = len(self.st.moves)
        text = "我在第%d手自主偏航：没走最优的%s，选了%s（与最优的胜率差约%d%%）" % (
            move_no, drift.get("best") or "（未知）", drift.get("gtp"),
            int(round(_as_float(drift.get("drop"), 0.0) * 100.0)))
        self.event_log.add("ai_bad", move_no, text, {
            "line": str(drift.get("line") or ""),
            "gtp": str(drift.get("gtp") or ""),
            "best": str(drift.get("best") or ""),
            "drop": _as_float(drift.get("drop"), 0.0),
            "mood": str(drift.get("mood") or ""),
        })
        return True

    # ================= 聊天事件（v3.8.0，设计 §6.6） =================

    def _record_chat_events(self) -> int:
        """聊天收尾后：规则式主题裁切 → 逐段写 ``chat`` 事件；返回**新写入**的条数。

        - **默认零 LLM**：分段与摘要都走 ``chat_topics``（纯函数），这里一次模型调用都不发；
        - **去重**：交给事件表（``kind`` + 归一化 ``text``）——主题 + 摘要命中已存在时 ``add`` 返回
          ``None``，因此重复主题不会反复写入（设计 §6.6「不要反复写入同一内容」）；
        - 事件原文是**给模型看的素材**：永不进台词、不产生 ``speech``；
        - **补写段用当前手数**（Minor #1，有意保留）：窗口里未写过的旧段这次一并补上，手数一律取
          ``len(st.moves)``（当前手数）——事件表只增不减、不随悔棋回退，用「补写时刻」比用「原始手数」
          更简单也不会造出比当前还新的时刻；代价是旧段的手数不精确，但衰减只按「从现在往回数」用，够用；
        - 任何异常只打日志，绝不影响对局。
        """
        try:
            segs = chat_topics(self.st.chat_history)
        except Exception as e:                      # noqa: BLE001 —— 裁切坏了也不能影响对局
            print("[Agent] 聊天主题裁切异常（已忽略）：%s" % e)
            return 0
        move_no = len(self.st.moves)
        n = 0
        for topic, summary in segs:
            ev = self.event_log.add("chat", move_no, "%s：%s" % (topic, summary),
                                    {"topic": topic, "summary": summary})
            if ev is not None:
                n += 1
        return n

    def _record_digest_lines(self, out) -> int:
        """把 LLM 补做的「主题：摘要」逐行写进事件表（解析不出的行直接丢，绝不抛）。

        **失败文案闸门**（Important #1）：``LLMClient.chat`` 失败时**不抛异常**，返回「调用出错：…」/
        「唔...我脑子有点转不过来了」这类文案；整段与单行都再挡一次——否则「调用出错」会被按「：」
        切成 ``topic``、当成一条 ``chat`` 事件落表，再经事件区块注入后续每一条提示词。
        """
        if self._llm_failed(out):                   # 整段就是失败文案：一条都不写
            return 0
        move_no = len(self.st.moves)
        n = 0
        for raw in str(out or "").splitlines():
            line = " ".join(str(raw).split()).strip(" -•\t")
            if line == "":
                continue
            for sep in ("：", ":"):
                if sep in line:
                    topic, summary = line.split(sep, 1)
                    break
            else:
                continue
            topic = " ".join(topic.split())
            summary = prompts._one_line(summary, CHAT_TOPIC_SUMMARY_CHARS)
            if topic == "" or summary == "" or self._llm_failed(topic):
                continue                            # 单行 topic 像失败文案（「调用出错」等）：跳过
            ev = self.event_log.add("chat", move_no, "%s：%s" % (topic, summary),
                                    {"topic": topic, "summary": summary, "source": "llm"})
            if ev is not None:
                n += 1
        return n

    async def _digest_chat_llm(self) -> int:
        """可选（``chat_digest_llm=true``）：用 LLM 分段总结聊天并写进事件表。

        **只在局末 ``summarize()`` 与空闲 ``idle_remark()`` 补做**——落子 / 聊天回合内一次都不调
        （性能红线）。它跑在 ``_turn_guard`` 之外，所以自带 ``CHAT_DIGEST_TIMEOUT_SEC`` 单次超时
        （Minor #4：不给端点留两分钟的尾巴）；未启用 / 没聊过 / LLM 失败 / 超时 / 解析不出都只打日志
        并返回 0，绝不影响对局，也不动 ``busy`` 的归位位置。
        """
        if not self.chat_digest_llm or self.llm is None:
            return 0
        lines = []
        for m in list(self.st.chat_history)[-CHAT_DIGEST_MAX_MSGS:]:
            if not isinstance(m, dict):
                continue
            who = "他" if str(m.get("role", "")) == "user" else "我"
            t = " ".join(str(m.get("content", "")).split())
            if t != "":
                lines.append("%s：%s" % (who, t))
        if not lines:
            return 0
        prompt = ("下面是这盘棋里我和 Estarth 的聊天记录。请按主题把它分成若干段，每段一行，"
                  "格式为「主题：一句话摘要」，主题用两到四个字，摘要不超过二十字；"
                  "不要复述原话，不要输出别的解释。\n\n" + "\n".join(lines))
        try:
            out = await asyncio.wait_for(
                self.llm.chat([{"role": "user", "content": prompt}], CHAT_DIGEST_MAX_TOKENS),
                CHAT_DIGEST_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            print("[Agent] 聊天 LLM 摘要超时（%d 秒，已忽略）" % CHAT_DIGEST_TIMEOUT_SEC)
            return 0
        except Exception as e:                      # noqa: BLE001
            print("[Agent] 聊天 LLM 摘要异常（已忽略）：%s" % e)
            return 0
        if self._llm_failed(out):                   # 失败文案**不抛异常**，必须在这里挡一次
            print("[Agent] 聊天 LLM 摘要失败（返回错误文案，已忽略）")
            return 0
        return self._record_digest_lines(out)

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
        self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_OBSERVE))
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
        self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_ANALYZE))
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
        token = self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_CHAT, {"text": text}))
        except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
            print("[Agent] 聊天异常：%s" % e)
        finally:
            self._set_busy(False)
        # 她回复完之后：把这轮聊天按主题写进事件表（规则式、零 LLM；事件原文永不进台词）。
        # 代际守卫：看门狗刚超时 / 本回合被新调用作废时，这一代不再写事件。
        if self.epoch_ok(token):
            self._record_chat_events()

    async def idle_remark(self):
        """闲置搭话：Estarth 一段时间没动静时，Azure 主动说一句短话。

        同样走看门狗（超时也解锁 + 前推一代），只是它**不进 busy**：与今天一致（空闲搭话不锁棋盘）。
        注意「新的公开调用会 ``begin_epoch()``」顺带带来了一个既有语义的加强：玩家一回来就落子时，
        还在途的走神搭话那一代随即作废，它那句就不再冒出来——与前端「走神搭话可整批作废」一致。
        """
        if self.busy:
            return
        self._new_turn()
        token = self.begin_epoch()
        await self._turn_guard(self._turn(PHASE_IDLE))
        # 空闲：`chat_digest_llm=true` 时在这里补做一次 LLM 分段总结（默认关，零额外调用）
        if self.epoch_ok(token):
            await self._digest_chat_llm()

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
        token = self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_SUMMARY, {"note": note}))
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 总结异常：%s" % e)
            await _emit_line(self, events.AI, "📋 对局总结\n" + self._fallback_line("chat"))
        finally:
            self._set_busy(False)
        # 局末：`chat_digest_llm=true` 时在这里补做一次 LLM 分段总结（默认关，零额外调用）
        if self.epoch_ok(token):
            await self._digest_chat_llm()

    # 注意（计划 05 · T20②）：下面四个**同步**方法里的短提示**故意保持纯文字**（不发 `speech`）——
    # 它们是同步方法（S5 不许改成 async），补 `speech` 就得 `await`、还得动端点；Task 5 会在 README 写明。
    def undo(self, turn_index):
        """俗手回档：撤销该手及其后的 Azure 应手，回到该手之前。

        v3.7.0：开头 ``begin_epoch()`` 开新一代——玩家这一手动作会把**还在途的上一代**（例如
        刚被看门狗解锁、却仍在后台收尾的那一回合）一并作废，免得它的迟到写入盖回这次回档。
        """
        if self.busy:
            return
        self.begin_epoch()
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
        self.st.player_ranks = s["player_ranks"]        # 棋力画像（v3.7.0）：悔掉的几手不再计入
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
        # v3.7.0：开新一代——新一局开始，上一代（可能还在后台收尾）的迟到写入一律作废
        self.begin_epoch()
        keep_pass = self.st.allow_pass
        self.st.reset()
        self.st.allow_pass = keep_pass
        self._joseki_taught = {}                 # 新一局：门控的冷却表清空（手数从头数）
        # v3.8.0：事件表是**局内内存**——新一局不带上一局的账（与「不随悔棋回退」是两回事，
        # 设计 §5 E/F：悔棋＝「发生过就是发生过」，重开＝另起一局）
        self.event_log.reset()
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
