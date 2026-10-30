"""对局编排（后端权威：对局、聊天、闲置搭话、情绪与定式都在这一层）。

职责：玩家落子 → Azure 点评 → Azure 思考并落子；提示/分析/聊天/闲置/总结/悔棋。
结果不直接返回给界面，而是交给 EventBus（事件契约见 ``events.py``），由 WS 推给客户端。

音画同步：``ai_move`` 事件带 ``move`` 坐标，客户端等她「落子宣告」的语音开始播放时本地揭示棋子；
后端把这一手**已落到内部棋谱**、可见更新延后（``reveal_move()`` 供需要 round-trip 的客户端调用）。
"""

import asyncio
import random
import re
import threading
import time
import uuid

from langchain_core.messages import ToolMessage

from . import actions, adaptive, coords, events, joseki_gate, lanes, persona, profile, prompt_chains, prompts, pv_lines, recall_chain, speech_text, time_ctx
from .agent_graph import (
    PHASE_AFTER_PLAYER, PHASE_ANALYZE, PHASE_CHAT, PHASE_IDLE, PHASE_JUST_MOVED,
    PHASE_OBSERVE, PHASE_OWN_TURN, PHASE_SUMMARY, PHASE_PEER, build_graph, is_joseki_mistake, _emit_line,
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
# 情绪小调用只是锦上添花：超过这个时间就取消，绝不占着单槽位生成锁拖慢下一句聊天。
MOOD_LLM_TIMEOUT_SEC = 6.0

# 主动回忆：同一条旧事 30 分钟内不再提；提示词里只放一小段，避免把记忆库倒出来
IDLE_RECALL_AVOID_SEC = 1800.0
IDLE_RECALL_MAX_CHARS = 110

# 限制回复长度时追加的约束（在不动原人设的前提下，单独附在末尾）
LEN_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
            "- 点评 / 提示：只回 1 句，尽量 20 字以内\n"
            "- 普通聊天：最多 2 句，尽量 40 字以内\n"
            "- 不复述局面、不客套，直接说结论就好")

# 「短回复」模式下，定式讲解用的替代约束：讲解要讲清要点，允许 2~3 句，不被一句式限制压扁
LEN_TEACH_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
                  "- 这是定式讲解：把名称与要点讲清楚，2~3 句即可，别长篇大论\n"
                  "- 不复述局面、不客套，直接开讲就好")

# 「说话详略」三档（v3.11.1，客户端 ⚙ 抽屉可调、即时生效）：
#   0 = 精简（沿用 ``LEN_NOTE``：点评 1 句 / 聊天 2 句）
#   1 = 适中（不加额外约束，模型按人设自己收着说 —— 与旧「短回复」关闭时逐字一致）
#   2 = 详细（允许展开 2~3 句，但仍封顶，不许长篇大论）
# 档位只改**提示词末尾的这条约束**，不改人设、不改阶段提示；``LEN_TEACH_NOTE`` 仍是定式讲解的特例。
LEN_VERBOSE_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
                    "- 这次可以展开一些：2~3 句，尽量 80 字以内\n"
                    "- 仍不复述局面、不客套，讲重点就好")
REPLY_STYLE_BRIEF = 0
REPLY_STYLE_NORMAL = 1
REPLY_STYLE_VERBOSE = 2


def _clamp_reply_style(v, fallback=REPLY_STYLE_NORMAL) -> int:
    """说话详略档位：脏值（None / 字符串 / NaN）退回 ``fallback``，数值夹到 ``0..2``。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return int(fallback)
    if f != f:                                   # NaN
        return int(fallback)
    return max(REPLY_STYLE_BRIEF, min(REPLY_STYLE_VERBOSE, int(f)))


# —— 主动约战 / 提醒（v3.9.0，设计 §5-E）——
# 「好久没来」的门槛：距上次对局不足一天就不打扰（刚下过）。台词是**模板句**，零 LLM 调用；
# 触发点只有一处（``main.py`` 的 WS 处理器在 ``hello`` 之后调 ``greet_with_context``），
# 实例上的 ``_greeted`` 保证**跨重连不重复**。
GREET_MIN_GAP_DAYS = 1.0

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
      但段的摘要同时收两侧命中的关键词 ——「玩家说到 X」用玩家那侧、「我聊到 Y」用她那侧；
      她没回关键词时写成「我回应了」（**与有没有她的回复无关地稳定**，故去重不会被回合数打散）；
    - **纯函数、零 LLM**：不碰 ``self``，可直接在事件写入路径上调用。
    """
    msgs = [m for m in (history or [])
            if isinstance(m, dict) and str(m.get("role", "")) in ("user", "assistant")]
    uidx = [i for i, m in enumerate(msgs) if str(m.get("role", "")) == "user"]
    if window and window > 0 and len(uidx) > window:
        msgs = msgs[uidx[-int(window)]:]          # 窗口从「第 window 近的玩家发言」起，带上其后回复
    segs = []                                     # [主题, [玩家那侧的关键词], [她那侧的关键词]]
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
        his_s = ("玩家说到%s" % "、".join(his)) if his else "玩家随口聊了几句"
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

# —— 情绪棋风（v3.13.0）：与「情绪波动」不同，这是**她自愿**的风格选择，不是走偏 ——
# 只在 B 线与最优线胜率差 ≤ 1.5% 时考虑；心情好/兴奋时才可能选更锐的一手；
# 一次选完冷却 6 手，避免每手都「想试试」。低落 / 平静时一律走最优（更稳）。
STYLE_MAX_DROP = 0.015
STYLE_MAX_P = 0.16
STYLE_COOLDOWN_MOVES = 6
STYLE_MIN_V = 0.62
STYLE_MIN_A = 0.50


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
                 pv_max_drop=PV_MAX_DROP_DEFAULT, drift_rng=None, chat_digest_llm=False,
                 emotion_style=False,
                 profile_enabled=False, time_aware_enabled=False, reply_style=None,
                 persona_enabled=False, persona_rng=None,
                 persona_relationship_enabled=False, persona_address_name="",
                 adaptive_enabled=False, adaptive_base=adaptive.BASE_DEFAULT,
                 adaptive_spread=adaptive.SPREAD_DEFAULT, adaptive_rng=None,
                 life=None, recall_rng=None):
        self.st = GameState()
        self.katago = engine
        self.llm = llm
        self.bus = bus if bus is not None else events.EventBus()
        self.mood = EmotionModel()

        self.joseki_teach = joseki_teach
        # 说话详略（v3.11.1）：``reply_style`` 是唯一真身（0 精简 / 1 适中 / 2 详细）；
        # 旧字段 ``limit_len`` 保留成**属性**（见下面），读写都映射到 0（开）/ 1（关）两档。
        # 两者同时给出时 ``reply_style`` 优先；只给旧字段时按旧语义迁移（True → 精简）。
        if reply_style is None:
            self.reply_style = REPLY_STYLE_BRIEF if limit_len else REPLY_STYLE_NORMAL
        else:
            self.reply_style = _clamp_reply_style(reply_style)
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
        # 桌宠聊天只用预制音（v4.2.5）：true 时聊天这条链路不合成整句，前端播一句预制情绪音；
        # 语音服务本身仍照常启动（解耦「服务在跑」与「聊天是否调用它」）。
        self.pet_chat_tts_prebuilt = False
        # 桌宠聊天思考档（v4.2.9）：0 无 / 1 低 / 2 中 / 3 强；棋评仍走 llm_think_mode。
        self.pet_chat_think_level = 1

        # —— 跨局长期记忆：默认不接线（None），由 main.py 的 lifespan 注入 MemoryService ——
        self.memory = memory

        # —— 生命时间线（拟人化 P0）：默认不接线（None）⇒ 行为与旧版逐字符一致；
        #    由 main.py lifespan 注入 LifeStore。它只记录**时间事实**，不参与出话。
        self.life = life
        self._life_tick_ms = 0
        self._life_mood_ms = 0
        # 主动回忆：概率门 + 最近提过的旧事（同一条 30 分钟内不重复）；线程锁保护 to_thread 并发。
        self.recall_rng = recall_rng if recall_rng is not None else random.Random()
        self._recall_recent: list = []
        self._recall_lock = threading.Lock()
        # 回忆链（v4.3.0，D）：同一条联想可延续 1~3 步；链之间与步之间都有冷却，
        # 避免每句都“我突然想起”。
        self._recall_chain: dict = {}
        self._recall_chain_end_ts: float = 0.0
        self._chat_seq: int = 0             # 玩家聊天序号：回忆链只在“链开始后确实又聊过”时检查换题

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
        # 情绪棋风（v3.13.0）：默认关（测试 / 旧调用与现状一致）；运行时由 config.json 打开。
        self.emotion_style = bool(emotion_style)
        self._style_until_move = 0
        # 本轮偏航的**待记账**信息（修复轮 1 / Important #1）：``_drift_pick`` 只填它、不写表；
        # 落点定案并提交之后由 ``_record_drift`` 消费（一次性，绝不跨回合残留）。
        self._drift_pending = None

        # —— 局内事件表（v3.8.0，设计 §6.3）：**局内内存**、不进 GameState 快照、不随悔棋回退 ——
        #    写入点两处：「走偏优解」（``_record_drift``）与「聊过天」（``_record_chat_events``）；
        #    ``reset_game`` 清空（新一局不带旧账）。
        self.event_log = EventLog()

        # —— 时间感知的采集（v3.9.0，设计 §5-H）：**单手用时的采集是纯后端的**（不要求前端配合）——
        #    ``session_start`` ＝ 本局开始时刻（本局第一手到达时置位）、``move_ts_list`` ＝玩家每一手
        #    的到达时刻；两者都是**局内内存**、``reset_game()`` 清空，**不进 GameState 快照**
        #    （悔棋不回退——「这一手确实等了这么久」是既成事实）。打点由 ``player_move`` 自己在
        #    ``is_legal`` 通过之后调 ``mark_move_time()``（修复轮 1 / I-1：被拒的落子不打点）；
        #    结算（间隔 / 均值 / 时长）在 ``time_ctx.facts``（纯函数），本类只负责采集——不打点＝零成本，
        #    也**不新增任何 LLM / 引擎调用**。
        self.session_start = None
        self.move_ts_list = []

        # —— 聊天事件（v3.8.0，设计 §6.6）：规则式主题裁切**默认零 LLM** ——
        #    关闭（默认）时 ``chat()`` 之后只走规则式；开启后**只在局末 / 空闲**补一次 LLM 分段总结
        #    （``_digest_chat_llm``），落子回合内一次都不调（性能红线）。
        self.chat_digest_llm = bool(chat_digest_llm)

        # ——玩家棋风画像 + 时间感知（v3.9.0，设计 §6.4）：两个开关**默认都关**（关掉＝现状）——
        #    关着时 ``profile_facts`` / ``time_facts`` 直接返回空 dict ⇒ 提示词里的两块都不注入
        #    （与没有这两个功能逐字符同构）。画像库由 ``main.py`` 的 lifespan 注入
        #    （``self.profile_store``，与 ``memory`` / ``joseki_progress`` 同一套装配纪律）；
        #    未装配 ⇒ 一律当「无画像」（读路径不建库、不抛）。
        self.profile_enabled = bool(profile_enabled)
        self.time_aware_enabled = bool(time_aware_enabled)
        self.profile_store = None
        # —— 人格化（关系阶段 / 情绪余温 / 今天的小心思）：默认关；关着＝提示词逐字符不变 ——
        #    与画像开关**解耦**：画像记棋风，人格化记「你们一起下过多少盘、她心里还留着什么」。
        #    ``load_persona()`` 在 lifespan 里读一次 profile.db 的 persona 表（测试不调用 ⇒ 零变化）。
        self.persona_enabled = bool(persona_enabled)
        self.persona_rng = persona_rng if persona_rng is not None else random.Random()
        # 关系阶段 / 称呼变化：**默认关**，称呼保持 `prompts.user_name`（默认玩家）不变。
        # 开启后才按局数推进称呼；`persona_address_name` 为空 ⇒ 即便开启也仍用原名。
        self.persona_relationship_enabled = bool(persona_relationship_enabled)
        self.persona_address_name = (str(persona_address_name).strip()[:16]
                                     if isinstance(persona_address_name, str) else "")
        self._persona = {}
        self._lingering_mood = None
        self._interactions = 0
        self._intent = None
        self._soft_note = ""
        self._soft_note_turns = 0
        # —— 棋谱库（v3.11.5）：默认不接线（None）⇒ 局末不写库；由 main.py lifespan 注入 GameStore。
        #    ``player_name`` 只用于棋谱的 PB（黑方名），``_library_sig`` 防同一次结束重复归档。
        self.game_store = None
        self.player_name = "玩家"
        self._library_sig = None
        # —— 自适应难度（v3.11.5）：默认关；基线上下最多 ±spread 档，由 main.py lifespan 按配置接通。
        self.adaptive = adaptive.AdaptiveDifficulty(
            enabled=adaptive_enabled, base=adaptive_base, spread=adaptive_spread, rng=adaptive_rng)
        # 主动约战 / 提醒（v3.9.0，设计 §5-E）：**本次进程只触发一次**的标记。跨重连不重复
        # （WS 重连会再调 ``greet_with_context``，第二次直接返回）；``reset_game`` **有意不重置**
        # ——「打过招呼」是这一次会话的事实，与「这一局」无关。
        # ``_greet_busy`` 只挡**并发**（两个连接同时进来时只有一个能发）；修复轮 1 / Minor #1 起
        # ``_greeted`` **只在真的发了台词之后**才置位——「这次没说成」（没历史 / 不足一天）不吃掉机会。
        self._greeted = False
        self._greet_busy = False

        self._mood_region = -1
        self._mood_ms = 0
        self._mood_task = None             # 后台情绪精修任务（v4.2.8）：新聊天到达时取消
        self._mood_desc = ""
        self._mood_reason = ""             # 最近一次情绪事件的「为什么」：给提示词带一两回合
        self._mood_reason_until = -1       # 手数上界；超过就自动不提
        self._undo_ts = []                 # 最近悔棋时刻：短时间连悔 → 无奈
        self._pat_ts = []                  # 最近摸头时刻：连续摸头收益递减
        self._busy_seq = 0            # busy 的所有者编号（每次置真 +1）：见 `_clear_busy`
        self._busy_owner = 0
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

    def _watchdog_fire(self, token=None):
        """看门狗触发：解锁 + **前推一代** + 一行日志 + 一条可读提示（走既有 ``message`` 通道）。

        「前推一代」是这里的关键，也是「超时不再放开并发」的全部含义：解锁会让玩家立刻可以开始
        新的一手，而在途那一代（可能还卡在某个等待里、也可能刚恢复）的 ``epoch_ok`` 已经全部为假，
        它之后的一切写入与发话都会被守卫拦掉——解锁因此不会让两代抢写同一份 ``st``。

        **只在「我这一代」还活着时动手（v3.9.0 修复波）**：``token`` 是 ``_turn_guard`` 那一轮
        公开调用自己的代数。它已经不等于当前代数 ⇒ 说明这期间有更新的公开调用（玩家又落了一手 /
        悔棋 / 她自己的另一回合）在前推代数，这一次「超时」属于**过期的那一代**——既不该解锁
        （可能把别人的在途回合解锁掉），也不该再前推一代。``idle_remark`` 是唯一不进 ``busy``
        的入口，它的看门狗若在玩家新回合在途时迟到触发，正是这条守卫挡下的形态（``_ctx_extras``
        的 ``asyncio.to_thread(profile_facts)`` 在文件系统卡住时没有超时，不能只靠「idle 自身
        await 有界」来排除）。取不到 ``token``（老调用方 / 单测）时保持旧行为：无条件触发。

        解锁走 ``_set_busy``（自带去重）：随后公开方法的 ``finally`` 再调一次不会多发 ``busy`` 事件。
        """
        if token is not None and not self.epoch_ok(token):
            return
        self.epoch += 1
        self._set_busy(False)
        print("[Agent] 回合兜底超时（%g 秒）：已解锁棋盘，这一回合的后续写入一律作废。" % float(self.watchdog_sec))
        self._emit(events.message(events.AI_NOTICE, "唔…这一手我算得太久了，先接着下吧。"))

    async def _turn_guard(self, coro, token=None):
        """最外层看门狗：跑完一次公开编排；超时则解锁 + 前推一代，本回合余下的写入全部作废。

        返回编排结果；**超时返回 ``None``**（调用方据此放弃本回合的后续阶段，但**不抛异常**——
        真正的编排异常照旧由调用方的 ``except`` 兜住，行为与没有看门狗时一致）。

        ``token`` 是本次公开调用的代数（六个入口都传自己 ``begin_epoch()`` 的那一个）：超时时把它
        交给 ``_watchdog_fire``，**只有它还属于当前这一代才真的触发**（见那里的注释）。

        **有意不取消**旧任务：它可能卡在不可取消的等待里（引擎 / 云端 / 工具），``wait_for`` 式的
        取消只会把半截状态留在半路、还会把 CancelledError 抛进本来就在降级的外部调用；让它自己跑完、
        用代际守卫把它的写入全部拦掉，既简单又不会留下更坏的状态。旧任务跑完即自行结束
        （异常由 ``_discard_task`` 收走，不会变成「never retrieved」告警）。
        """
        task = asyncio.ensure_future(coro)
        done, _pending = await asyncio.wait({task}, timeout=float(self.watchdog_sec))
        if not done:
            self._watchdog_fire(token)
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
        if self._soft_note_turns > 0:               # 难度半句只顺延一个公开回合，之后自然消失
            self._soft_note_turns -= 1
            if self._soft_note_turns <= 0:
                self._soft_note = ""
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

    @staticmethod
    def _greet_line(gap_days) -> str:
        """按「距上次对局」挑一句**主动约战 / 提醒**的模板（v3.9.0，设计 §5-E）。

        **纯函数**（零 LLM、零磁盘、零随机）：不算「好久没来」（不足 ``GREET_MIN_GAP_DAYS`` 天 /
        脏值 / ``NaN`` / ``inf``）⇒ 返回空串（＝这次不打招呼）。台词分三档（一两天 / 几天 / 一周以上），
        每档一句固定模板——本功能一辈子只触发一次，不需要轮换文案。
        """
        try:
            d = float(gap_days)
        except (TypeError, ValueError):
            return ""
        if d != d or d < GREET_MIN_GAP_DAYS:        # NaN / 不足一天：刚下过，别打扰
            return ""
        try:
            days = int(d)
        except (OverflowError, ValueError):         # inf 之类
            return ""
        if days < 3:
            return "你回来啦~ 上次那盘我还记着呢，要不要再来一局？"
        if days < 7:
            return "好几天没见你落子了，手痒没有？来一盘吧~"
        return "好久不见啦…我都快记不清你下棋的样子了。要不要来一局，让我看看你有没有长进？"

    def _update_katago_brief(self, analysis, num_moves):
        """把 KataGo 形势数据整理成一段文字，存进 st.last_katago，供 Azure 上下文使用。"""
        if not analysis or "error" in analysis:
            return
        conv = prompts.to_black_perspective(analysis, num_moves)
        wr = float(conv[0])
        sc = float(conv[1])
        who = "玩家（黑）稍优" if sc > 0.5 else ("Azure（白）稍优" if sc < -0.5 else "双方接近")
        s = "黑方（玩家）胜率 %.1f%%；目差 %+.1f 目（正数=黑/玩家领先，含贴目）；整体 %s。" % (wr * 100.0, sc, who)
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
            s += " 实地估算：玩家（黑）约 %.0f 目，Azure（白）约 %.0f 目（按逐点归属估算，仅供参考）。" % (b, w)
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
            mood_name=self.mood.region_name(), mood_reason=self._mood_reason_text(),
            think_hint=self._think_hint(),
            persona_extra=self._extra_note(),
        )[0]["content"]

    def _sys_msg(self):
        """系统消息：聊天 / 闲置搭话这类「context_block + 历史窗口」的路径用；带 with_context 的路径走 `_phase_messages`。"""
        return {"role": "system", "content": self._sys_content(self._len_note())}

    @property
    def limit_len(self) -> bool:
        """旧字段兼容（v3.11.1）：``True`` ⇔ 精简档（``reply_style == 0``）。

        历史上它是「短回复」布尔开关；现在档位是 ``reply_style`` 三档，但它仍是大量调用点与
        测试的读写入口，所以做成属性：**读**回答「是不是精简档」，**写** true → 精简 / false → 适中。
        """
        return self.reply_style == REPLY_STYLE_BRIEF

    @limit_len.setter
    def limit_len(self, v):
        self.reply_style = REPLY_STYLE_BRIEF if bool(v) else REPLY_STYLE_NORMAL

    def _phase_messages(self, phase, context, len_note=None) -> list:
        """某阶段的完整消息（系统 + 上下文）：经 ``prompt_chains`` 的链构造。

        ``len_note=None`` 表示「按本局设置推导」：按 ``reply_style`` 取对应长度约束（精简 → ``LEN_NOTE``、
        适中 → 空、详细 → ``LEN_VERBOSE_NOTE``）——调用点不必各自记得；定式讲解这类特例显式传入
        ``LEN_TEACH_NOTE``。

        必须与旧 ``[_sys_msg(), _user_msg(context)]`` 逐字符一致——模型要看到的字与本次重构前
        完全相同，一个标点都不能变（tests/test_prompt_chains.py 逐字符锁定）。
        """
        if len_note is None:
            len_note = self._len_note()
        return prompt_chains.build_messages(
            phase, context=context, len_note=len_note,
            mood_name=self.mood.region_name(), mood_reason=self._mood_reason_text(),
            think_hint=self._think_hint(),
            persona_extra=self._extra_note(),
        )

    @staticmethod
    def _user_msg(content):
        return {"role": "user", "content": content}

    def _len_note(self, teach=False) -> str:
        """长度约束后缀：按 ``reply_style`` 三档取（精简 → ``LEN_NOTE``、适中 → 空、详细 → ``LEN_VERBOSE_NOTE``）；
        定式讲解（``teach=True``）在精简 / 详细档换 ``LEN_TEACH_NOTE``。

        调用点在 ``agent_graph`` 里——「定式成型那一手」是唯一显式传 ``len_note`` 的地方
        （tests/test_prompt_chains.py 的条数断言守着调用点有没有漏）。
        """
        style = _clamp_reply_style(self.reply_style)
        if style == REPLY_STYLE_BRIEF:
            return LEN_TEACH_NOTE if teach else LEN_NOTE
        if style == REPLY_STYLE_VERBOSE:
            return LEN_TEACH_NOTE if teach else LEN_VERBOSE_NOTE
        return ""

    def _moves_with(self, color, gtp):
        out = list(self.st.moves)
        out.append([color, gtp])
        return out

    def _set_busy(self, v):
        """设置忙碌状态；值没变就不推事件（嵌套的编排外壳会各自兜一次归位）。

        **置真时发放一个「所有者编号」并返回**（清位时把它交给 `_clear_busy`）。`busy` 是单例布尔，
        旧回合的 ``finally`` 若不核对所有权，会把**新回合**的锁提前解开——见 `_clear_busy`。
        """
        if v:
            self._busy_seq += 1
            self._busy_owner = self._busy_seq
        else:
            self._busy_owner = 0
        if self.busy == v:
            return self._busy_owner
        self.busy = v
        self._emit(events.busy(v))
        return self._busy_owner

    def _clear_busy(self, owner=0):
        """清 busy，但**只允许拥有者清**：旧回合的 ``finally`` 不许解开新回合的锁。

        复现路径（v3.9.5）：`_finalize_just_moved` 在她这一手讲完就 `_set_busy(False)`（设计如此：
        不让玩家等后台的情绪小调用），紧接着 `_mood_llm_refine()` 还要跑 1~20 秒（它有 15 秒限频，
        所以每两三手才出现一次）。玩家在这段窗口里落子 ⇒ 新回合把 busy 置真；旧回合的 ``finally``
        回来若无条件清位，就把**新回合**的锁解了 ⇒ 她还没应手，玩家又能再下一手。
        """
        if owner and owner != self._busy_owner:
            return
        self._set_busy(False)

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

    async def _reply(self, messages, max_tokens=300, think_mode=None, no_think=False) -> str:
        """所有对外展示的 LLM 回复统一过一遍清洗。

        ``think_mode`` 可按调用覆盖（桌宠走神/窥屏用快速档，别为一句 20 字的话
        等强思考）；``None`` 沿用客户端全局档位。
        ``no_think=True``（v4.2.9 桌宠「无思考」档）：连推理预算也不给。

        云端失败时 ``LLMClient.chat`` 返回「调用出错：…」文案（不抛异常）；这里再兜住
        「真抛出来」的情况（返回空串），由调用方的兜底台词顶上——她该下的棋、该念的
        落子宣告都不会因此丢掉。
        """
        try:
            try:
                text = await self.llm.chat(messages, max_tokens, think_mode=think_mode,
                                           no_think=no_think)
            except TypeError:
                # 鸭子类型替身可能只实现旧签名：先退到「只带档位」，再退到「只带两参」，
                # 绝不打挂这一回合。
                try:
                    if think_mode is None:
                        text = await self.llm.chat(messages, max_tokens)
                    else:
                        text = await self.llm.chat(messages, max_tokens, think_mode=think_mode)
                except TypeError:
                    text = await self.llm.chat(messages, max_tokens)
            return self.clean_reply(text)
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

    def _budget_of(self, max_tokens) -> int:
        """工具轮 / 流式通道的输出上限：有 ``LLMClient`` 就走它含推理余量的入口（与 ``chat()`` 同一套）。

        测试里的鸭子类型替身（``llm=None`` 或简易假链）没有 ``budget_for``，按原值发——那些用例
        钉的是绑定形状，不是预算数值。
        """
        fn = getattr(self.llm, "budget_for", None)
        return int(fn(max_tokens)) if callable(fn) else int(max_tokens)

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
            body["max_tokens"] = self._budget_of(max_tokens)
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
            body["max_tokens"] = self._budget_of(max_tokens)
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
        -玩家看到的永远是模型重写的自然语言（``clean_reply`` 必经），工具输出只回到模型手里；
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

    def _mood_axis(self) -> dict:
        """当前三维情绪（给记忆召回 / 语言节奏用）；不触发衰减结算以外的副作用。"""
        return {"v": float(self.mood.v), "c": float(self.mood.c), "a": float(self.mood.a)}

    def _long_term(self, query) -> str:
        """召回跨局长期记忆的注入文本；未接线 / 未启用 / 任何故障一律返回空串。

        默认不接线（``memory is None``）或 ``available == False`` 时不注入——
        关闭长期记忆时的提示词与没有这个功能时逐字符一致。召回在出话路径上，
        这里再兜一层异常保护，绝不让记忆把这一回合的 ``busy`` 卡住。

        v4.3.0：把**当前三维情绪**交给召回层，让「和此刻心情相近的旧事」稍微靠前
        （二维情绪矩阵在存储层；没有情绪标签的旧记忆按语义分排，不受影响）。

        本方法是**同步**的：调用点必须 ``await asyncio.to_thread(self._long_term, …)``——
        慢 embedding（最长 ``embed_timeout`` 秒）绝不能冻住事件循环（心跳与落子宣告都在这条循环上）。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return ""
        try:
            try:
                return str(mem.recall_for(str(query or ""), mood=self._mood_axis()) or "")
            except TypeError:               # 旧替身 / 旧实现只收 query
                return str(mem.recall_for(str(query or "")) or "")
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 长期记忆召回异常（已忽略）：%s" % e)
            return ""

    # —— 主动回忆：不是每句都提，按情绪/无聊/亲密与随机门决定 ——

    def idle_recall_chance(self) -> float:
        """一次走神里主动想起旧事的概率（0.03–0.30）。

        依据情绪系统设计：无聊高、亲密/信任高时更愿意回忆；精力低时少一点。
        这是**规则**，不调用 LLM；没有记忆库时为 0。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return 0.0
        f = self._life_fields() or {}
        boredom = float(f.get("boredom", 0.16))
        affinity = float(f.get("affinity", 0.35))
        trust = float(f.get("trust", 0.50))
        energy = float(f.get("energy", 0.72))
        p = 0.10 + 0.14 * boredom + 0.07 * affinity + 0.03 * trust
        if self.mood.v >= 0.62:
            p += 0.03                         # 心情好时更愿意分享旧事
        if energy <= 0.32:
            p -= 0.04                         # 累了就少翻旧账
        return max(0.03, min(0.30, p))

    def idle_memory_query(self) -> str:
        """按当前情绪挑选联想方向；向量/关键词召回都用它。

        不是把情绪数值扔给模型，而是用情绪**选一个回忆主题**：开心时想温暖的事，
        低落时想被陪伴的事，好奇时想知道你最近在忙什么。
        """
        f = self._life_fields() or {}
        affinity = float(f.get("affinity", 0.35))
        if self.mood.v >= 0.66 or affinity >= 0.65:
            return "玩家开心 喜欢 温暖 约定 一起做过的事"
        if self.mood.v <= 0.40:
            return "玩家难过 安慰 陪伴 低落 最近聊天"
        if self.mood.c >= 0.62:
            return "玩家最近在忙 好奇 新鲜事 计划"
        if self.mood.a >= 0.68:
            return "上次聊到 有趣 期待 一起做的事"
        return "玩家最近聊过 喜欢 习惯 事情"

    def _last_user_chat(self) -> str:
        """最近一条玩家发言（回忆链判断「话题有没有换」用）；没有返回空串。"""
        for h in reversed(list(getattr(self.st, "chat_history", []) or [])):
            if isinstance(h, dict) and str(h.get("role", "")) == "user":
                return str(h.get("content", "") or "")
        return ""

    def _end_recall_chain(self, now=None) -> None:
        """结束回忆链并记下冷却起点（v4.3.0，D）；线程安全，重复调用无副作用。"""
        ts = time.time() if now is None else float(now)
        with self._recall_lock:
            self._recall_chain = {}
            self._recall_chain_end_ts = ts

    def idle_memory_hint(self) -> dict:
        """走神时偶尔挑一条旧事（向量/关键词召回），返回 ``{id,text,score,chain}`` 或 ``{}``。

        - 概率由 ``idle_recall_chance`` 决定；同一条 30 分钟内不重复；
        - v4.3.0 **回忆链**：上一次提起的旧事可以在 1~3 步内接着往下说，但只要出现
          「换话题 / 情绪大幅跳变 / 超过时间窗 / 链长到上限」就收束；链与链之间还有冷却，
          不会每次走神都翻旧账；
        - **只返回一句短文本**，旧事原文由 ``agent_graph`` 放进提示词；
        - 记忆关闭 / 召回为空 / 任何故障 ⇒ ``{}``（提示词逐字符不变）。
        同步实现（召回可能含 embedding），调用点走 ``asyncio.to_thread``。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return {}
        now = time.time()
        mood = self._mood_axis()
        with self._recall_lock:
            chain = dict(self._recall_chain) if isinstance(self._recall_chain, dict) else {}
            chain = chain if chain else {}
            last_end = float(self._recall_chain_end_ts or 0.0)
            try:
                roll = float(self.recall_rng.random())
            except Exception:                    # noqa: BLE001 —— 随机源坏了就不提
                return {}
        if chain:
            # 只有链开始后玩家**确实又发过新消息**，才用那句话判换题；
            # 否则拿链开始前的旧消息会把刚开的链误判成“换话题”。
            new_chat = self._chat_seq > int(chain.get("chat_seq", 0) or 0)
            last_user = self._last_user_chat() if new_chat else ""
            if not recall_chain.should_continue(now, chain, mood, last_user):
                self._end_recall_chain(now)
                return {}
            query = recall_chain.chain_query(chain, self.idle_memory_query())
            continued = True
        else:
            chance = self.idle_recall_chance()
            if chance <= 0.0 or not recall_chain.should_start(
                    now, chain, last_end, mood, roll, chance):
                return {}
            query = self.idle_memory_query()
            continued = False
        try:
            fn = getattr(mem, "recall_items_for", None)
            if callable(fn):
                try:
                    items = fn(query, k=5, mood=mood)
                except TypeError:             # 旧替身只收 query / k
                    items = fn(query, k=5)
            else:
                text = str(mem.recall_for(query) or "").strip()
                items = [{"id": None, "text": text, "score": 0.0}] if text else []
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 主动回忆失败（已忽略）：%s" % e)
            return {}
        if not isinstance(items, list):
            return {}
        with self._recall_lock:
            self._recall_recent = [
                (key, ts) for key, ts in self._recall_recent
                if now - float(ts) <= IDLE_RECALL_AVOID_SEC
            ]
            used = {key for key, _ in self._recall_recent}
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text", "") or "").split())
                if text == "":
                    continue
                key = str(item.get("id")) if item.get("id") is not None else text
                if key in used:
                    continue
                if len(text) > IDLE_RECALL_MAX_CHARS:
                    text = text[:IDLE_RECALL_MAX_CHARS - 1] + "…"
                self._recall_recent.append((key, now))
                if continued:
                    chain = recall_chain.advance(chain, now, item_id=item.get("id"))
                else:
                    fields = self._life_fields() or {}
                    depth_max = recall_chain.max_depth(
                        mood, float(fields.get("affinity", 0.35)))
                    chain = recall_chain.start(text, now, mood,
                                               depth_max=depth_max, source="idle")
                    chain["chat_seq"] = int(self._chat_seq)
                    chain = recall_chain.advance(chain, now, item_id=item.get("id"))
                self._recall_chain = chain
                return {"id": item.get("id"), "text": text,
                        "score": item.get("score", 0.0),
                        "emotion": str(item.get("emo_tag", "") or ""),
                        "chain": {"continued": continued,
                                  "depth": int(chain.get("depth", 0)),
                                  "max_depth": int(chain.get("max_depth", 2))}}
        if continued:
            self._end_recall_chain(now)        # 链还在，但已经没有可提的下一条 ⇒ 收束
        return {}

    def chat_recall_hint(self, text) -> dict:
        """聊天里的**受控**回忆（v4.3.0，D）：只在明确提旧事、或已有链且话题未变时触发。

        与走神共用一个链状态：玩家说「上次说的那盘棋」可以开链；链没断时下一句可以
        接着补一个细节。已经在聊别的事 / 情绪跳变 / 两步之间太密 ⇒ 返回 ``{}`` 并结束链。
        同步实现（可能含 embedding），调用点走 ``asyncio.to_thread``。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return {}
        s = " ".join(str(text or "").split())
        if s == "":
            return {}
        now = time.time()
        mood = self._mood_axis()
        with self._recall_lock:
            chain = dict(self._recall_chain) if isinstance(self._recall_chain, dict) else {}
            chain = chain if chain else {}
        cue = recall_chain.memory_cue(s)
        if chain and recall_chain.should_continue(
                now, chain, mood, s, min_gap=90.0, check_topic=not cue):
            query = recall_chain.chain_query(chain, s)
            continued = True
        else:
            if chain:
                # 链已经走完 / 换了话题。玩家明确问旧事时允许**当场另起一条**新链，
                # 不再被旧链的 cooldown 卡住；只是普通换题就收束。
                self._end_recall_chain(now)
                chain = {}
            if cue and recall_chain.should_start(
                    now, {}, 0.0, mood, 0.0, 0.0, cue=True):
                query = s
                continued = False
            else:
                return {}
        try:
            fn = getattr(mem, "recall_items_for", None)
            if not callable(fn):
                return {}
            try:
                items = fn(query, k=4, mood=mood)
            except TypeError:
                items = fn(query, k=4)
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 聊天回忆失败（已忽略）：%s" % e)
            return {}
        with self._recall_lock:
            self._recall_recent = [
                (key, ts) for key, ts in self._recall_recent
                if now - float(ts) <= IDLE_RECALL_AVOID_SEC
            ]
            used = {str(i) for i in list(chain.get("ids") or [])}
            used.update(key for key, _ts in self._recall_recent)
            for item in list(items or []):
                if not isinstance(item, dict):
                    continue
                body = " ".join(str(item.get("text", "") or "").split())
                if body == "":
                    continue
                key = str(item.get("id")) if item.get("id") is not None else body
                if key in used:
                    continue
                self._recall_recent.append((key, now))
                if continued:
                    chain = recall_chain.advance(chain, now, item_id=item.get("id"))
                else:
                    fields = self._life_fields() or {}
                    depth_max = recall_chain.max_depth(
                        mood, float(fields.get("affinity", 0.35)))
                    chain = recall_chain.start(body, now, mood,
                                               depth_max=depth_max, source="chat")
                    chain["chat_seq"] = int(self._chat_seq)
                    chain = recall_chain.advance(chain, now, item_id=item.get("id"))
                self._recall_chain = chain
                return {"id": item.get("id"), "text": body,
                        "emotion": str(item.get("emo_tag", "") or ""),
                        "chain": {"continued": continued,
                                  "depth": int(chain.get("depth", 0)),
                                  "max_depth": int(chain.get("max_depth", 2))}}
        if continued:
            self._end_recall_chain(now)
        return {}

    def _remember_chat_turn(self, user_text, reply_text, mode=""):
        """把一轮聊天写进共享记忆（原文流水 + 可召回摘要）。同步实现，调用方走 to_thread。"""
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return
        try:
            remember = getattr(mem, "remember_chat", None)
            if callable(remember):
                remember(str(user_text or ""), str(reply_text or ""), str(mode or ""), self.mood_payload())
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 聊天记忆写入异常（已忽略）：%s" % e)

    def _remember_pet_line(self, kind, text, mode=""):
        """把桌宠的走神 / 窥屏搭话写成轻量事件记忆；失败不影响出话。"""
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return
        try:
            remember = getattr(mem, "remember_event", None)
            if callable(remember):
                remember(str(kind or "pet"), str(text or ""), str(mode or ""), self.mood_payload())
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 桌宠事件记忆写入异常（已忽略）：%s" % e)

    def load_recent_chat_history(self, limit=8):
        """启动时把最近的共享聊天流水灌回 ``st.chat_history``。

        这是「围棋 ↔ 桌宠 ↔ 下次启动」的连续性来源：桌宠聊过的事、上一盘棋后说过的话，
        在围棋的聊天/落子提示词里仍能看到最近窗口；长期细节仍由 memories 召回。
        关闭记忆 / 库不可用时什么都不做（提示词逐字符不变）。同步实现，调用方走 to_thread。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return
        try:
            recent = getattr(mem, "recent_chats", None)
            rows = list(recent(int(limit or 8)) or []) if callable(recent) else []
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 最近聊天恢复失败（已忽略）：%s" % e)
            rows = []
        hist = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            u = str(r.get("user_text", "") or "").strip()
            a = str(r.get("reply_text", "") or "").strip()
            if a:
                # 旧库里的回复可能还带 [[tone:...]]：恢复上下文前先剥掉，别让模型学着复读。
                a = speech_text.extract_tone(a)[1].strip()
            if u:
                hist.append({"role": "user", "content": u[:240]})
            if a:
                hist.append({"role": "assistant", "content": a[:360]})
        if hist:
            # 与局内窗口同量级：最多保留最近 2*limit 条，别把提示词撑爆
            self.st.chat_history = hist[-max(2, int(limit or 8) * 2):]

    # ================= 生命时间线（拟人化 P0） =================

    def load_life(self, now=None):
        """启动时接上持久化时间线：恢复跨重启的情绪余温（失败静默）。

        时间线库在 lifespan 构造时已经读过一次；这里只做**内存状态 → 情绪模型**的恢复，
        不额外读盘。没有装配 / 没有落盘情绪 / 脏值 ⇒ 不改情绪，保持旧版行为。
        ``now`` 可注入（测试用），生产留空走系统时钟。
        """
        life = self.life
        if life is None:
            return
        try:
            restored = life.mood_after_break(now=time.time() if now is None else now)
        except Exception as e:                  # noqa: BLE001
            print("[Life] 情绪恢复失败（已忽略）：%s" % e)
            return
        if not isinstance(restored, dict) or not restored:
            return
        try:
            self.mood.carry(restored, keep=1.0)
        except Exception as e:                  # noqa: BLE001
            print("[Life] 情绪恢复应用失败（已忽略）：%s" % e)

    def save_life(self):
        """进程退出 / 需要显式落盘时保存情绪与活跃时间（失败静默）。"""
        self._life_tick(force=True)
        self._life_save_mood(force=True)

    def _life_touch(self, kind, source="", summary="", meta=None, event=True, now=None):
        """写一条生命事件；没有装配时间线时零动作。任何故障只打日志。"""
        life = self.life
        if life is None:
            return
        try:
            life.touch(kind, source=source, summary=summary, meta=meta,
                       now=time.time() if now is None else now, event=event)
        except Exception as e:                  # noqa: BLE001
            print("[Life] 事件写入失败（已忽略）：%s" % e)

    def _life_fields(self) -> dict:
        """慢变量快照（精力 / 无聊 / 亲密 / 信任）；未装配 ⇒ ``{}``（旧载荷形状不变）。"""
        life = self.life
        if life is None:
            return {}
        try:
            s = life.state
        except Exception as e:                  # noqa: BLE001
            print("[Life] 慢变量读取失败（已忽略）：%s" % e)
            return {}
        out = {}
        for key, default in (("energy", 0.72), ("boredom", 0.16),
                             ("affinity", 0.35), ("trust", 0.50),
                             ("dominance", 0.50)):
            try:
                out[key] = max(0.0, min(1.0, float(s.get(key, default))))
            except (TypeError, ValueError):
                out[key] = default
        return out

    def life_note(self) -> str:
        """把慢变量翻译成**语气约束**（不报数字、不念状态名）。

        没有装配生命时间线 ⇒ 空串（提示词逐字符不变）。触发阈值刻意偏极端：
        只有真的累了 / 真的无聊了才说，避免每句都“演状态”。
        """
        f = self._life_fields()
        if not f:
            return ""
        parts = []
        energy = f.get("energy", 0.72)
        boredom = f.get("boredom", 0.16)
        affinity = f.get("affinity", 0.35)
        trust = f.get("trust", 0.50)
        dominance = f.get("dominance", 0.50)
        if energy <= 0.32:
            parts.append("你现在有点累：反应慢半拍、句子短一些，别硬撑热情。")
        elif energy >= 0.80:
            parts.append("你现在精神不错：可以主动一点，但别喧宾夺主。")
        if boredom >= 0.62:
            parts.append("你已经有一阵子没和玩家互动：心里有点想说话，但先读空气，别催促。")
        if affinity >= 0.70:
            parts.append("你们已经很熟了：语气可以放松、亲近一点，但别腻。")
        elif affinity <= 0.25:
            parts.append("你还在观察玩家：保持一点分寸，别一下子掏心。")
        if trust <= 0.30:
            parts.append("信任还没有完全建立：不要替玩家做决定，也别过度自证。")
        if dominance <= 0.30:
            parts.append("你此刻有点没底气：允许自己说不确定，别逞强。")
        elif dominance >= 0.70:
            parts.append("你现在比较笃定：可以给出自己的判断，但别压人。")
        if not parts:
            return ""
        return ("【此刻的身体与心绪 · 内部状态勿外露】\n"
                + "\n".join("· " + p for p in parts)
                + "\n（这些只影响语气、长度与主动性；不要提数值、状态名，也不要逐条念出来。）")

    def _life_save_mood(self, force=False):
        """落盘情绪快照（节流 10 秒；事件点可 force）。"""
        life = self.life
        if life is None:
            return
        now = time.time()
        if not force and now - self._life_mood_ms < 10.0:
            return
        self._life_mood_ms = now
        try:
            life.save_mood(self.mood_payload(), now=now)
        except Exception as e:                  # noqa: BLE001
            print("[Life] 情绪落盘失败（已忽略）：%s" % e)

    def _life_tick(self, force=False):
        """时间线心跳（节流 15 秒）：推进 last_tick_ts 并顺便保留情绪。"""
        life = self.life
        if life is None:
            return
        now = time.time()
        if not force and now - self._life_tick_ms < 15.0:
            return
        self._life_tick_ms = now
        self._life_mood_ms = now
        try:
            life.tick(now=now, mood=self.mood_payload())
        except Exception as e:                  # noqa: BLE001
            print("[Life] 心跳落盘失败（已忽略）：%s" % e)

    def _remember_game(self, summary, black_wr, score):
        """把这一局的总结写进长期记忆（``result`` 渲染成一行）；失败只打日志，不影响对局。

        同 ``_long_term``：同步实现，调用点走 ``asyncio.to_thread``（写库 + embedding 都在里面）。
        """
        mem = self.memory
        if mem is None or not bool(getattr(mem, "available", False)):
            return
        try:
            result = "黑方（玩家）胜率 %s，目差 %s" % (self._pct(black_wr), "%+.1f" % score)
            mem.remember_game(list(self.st.moves), result, str(summary or ""))
        except Exception as e:                  # noqa: BLE001
            print("[Agent] 长期记忆写入异常（已忽略）：%s" % e)

    # ================= 三维情绪 =================

    def _mood_event(self, ev, desc_extra="", scale=1.0, burst=""):
        """事件结算入口：本地增量立即生效，区域变化才推给界面。

        ``scale`` 供「哄与惯性」使用；``burst`` 非空时即使区域没变也推一次一次性表情
        （自己下好/下坏这种小情绪不该因为区域没跨带就完全看不见）。
        """
        self.mood.apply_event(ev, scale)
        self._mood_desc = EmotionModel.event_desc(ev) + desc_extra
        self._mood_reason = self._mood_desc
        self._mood_reason_until = len(self.st.moves) + 2
        self.sync_mood(bool(burst), False, burst)
        self._life_save_mood(True)

    def _mood_reason_text(self) -> str:
        """最近一次情绪事件的来由；超过两回合 / 复盘 / 空 ⇒ 空串（提示词逐字符不变）。"""
        if self.review or self._mood_reason == "":
            return ""
        if len(self.st.moves) > self._mood_reason_until:
            return ""
        return self._mood_reason

    def sync_mood(self, force=False, quiet=False, burst=""):
        """把当前情绪区域推给界面；区域没变时不产生任何事件（``burst`` 非空则强制推一次）。"""
        r = self.mood.region()
        if not force and r == self._mood_region:
            return
        self._mood_region = r
        info = self.mood.info(r)
        payload = {
            "region": r,
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": "" if quiet else str(info.get("filler", "")),
            "burst": "" if quiet else (str(burst) or str(info.get("burst", ""))),
            "pitch": float(info.get("pitch", 0.0)),
        }
        payload.update(self._life_fields())      # 未装配生命时间线 ⇒ 形状与旧版完全一致
        self._emit(events.mood(payload))
        self._life_save_mood(False)

    def _pat_scale(self) -> float:
        """摸头的效果系数：难过时更有效；烦躁/委屈时更难哄；连续摸头收益递减。"""
        info = self.mood.info(self.mood.region())
        name = str(info.get("name", ""))
        if name in ("难过", "失落", "落寞", "忧心", "不安"):
            scale = 1.25
        elif name in ("烦躁", "烦闷", "委屈"):
            scale = 0.35
        else:
            scale = 1.0
        now = time.time()
        self._pat_ts = [t for t in self._pat_ts if now - t <= 45.0]
        n = len(self._pat_ts)
        self._pat_ts.append(now)
        self._pat_ts = self._pat_ts[-4:]
        if n > 0:
            scale *= (0.65 ** min(n, 3))              # 第二次 65%，第三次 42%，之后约 27%
        return max(0.2, min(1.3, scale))

    def tick_mood(self):
        """客户端每 2 秒的心跳：只结算「向基线回落」。

        复盘期间**直接返回**（V14）：不衰减、不发 ``mood`` 事件——回看全程她要静音。
        ``mood_payload()`` 不受影响（``hello`` 同步照常可读）。
        """
        if self.review:
            return
        self._life_tick()
        self.sync_mood(False)

    def player_interaction(self, what):
        """玩家亲密互动（摸头等）：本地增量 + 静默同步（语气词让位给互动音效）。

        复盘期间**直接返回**（V14）：不产生任何情绪变化（连增量都不落）、不发 ``mood`` 事件。
        """
        if self.review:
            return
        self._persona_bump_interaction()
        self._life_touch("interaction", summary=what)
        scale = self._pat_scale() if str(what) == "patted" else 1.0
        self.mood.apply_event(what, scale)
        self._mood_desc = EmotionModel.event_desc(what)
        self._mood_reason = self._mood_desc
        self._mood_reason_until = len(self.st.moves) + 2
        self.sync_mood(False, True)
        self._life_save_mood(True)

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
        with lanes.track(lanes.LANE_PERSONA):
            try:
                d = await asyncio.wait_for(
                    self._mood_delta((self.mood.v, self.mood.c, self.mood.a), desc),
                    timeout=MOOD_LLM_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                print("[Agent] 情绪小调用超时（跳过，不占用下一句）")
                return
            if d is None:
                return
            self.mood.apply_delta(d[0], d[1], d[2])
            self.sync_mood(False)

    def cancel_background_llm(self):
        """取消后台情绪精修（v4.2.8）：新聊天到达时先让出单槽位生成锁。

        本地 llama-server 只有一个生成槽位；情绪小调用若卡在结构化输出上，会把
        `LLMClient._gen_lock` 占住，下一句聊天就只能排在它后面。这里取消在途任务，
        让聊天优先；情绪只保留本地事件结算结果，不影响连续性。
        """
        task = getattr(self, "_mood_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._mood_task = None

    def spawn_mood_refine(self):
        """把情绪精修丢到后台（不阻塞 `/api/chat` 回包，也不挡下一句）。"""
        self.cancel_background_llm()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._mood_task = loop.create_task(self._mood_llm_refine())

    @staticmethod
    def _chat_event(kind) -> str:
        return {
            "laugh": "chat_laugh",
            "delight": "chat_praise",
            "sad": "chat_sad",
            "puzzled": "chat_question",
        }.get(kind, "chat_normal")

    @staticmethod
    def is_casual_chat(text) -> bool:
        """玩家是不是在说「随便下下 / 放松点」：只影响情绪事件，不改回复内容。"""
        t = str(text or "").lower()
        return any(kw in t for kw in ["随便", "轻松", "不用认真", "玩玩", "放松", "随便聊"])

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

    # ================= 时间感知（采集，v3.9.0） =================

    def mark_move_time(self, now=None):
        """记一次「玩家这一手到达」的时刻（时间感知的**唯一采集点**，设计 §5-H）。

        由 ``player_move`` 在 ``is_legal`` 通过、真正落子之前调用（修复轮 1 / I-1：打点从
        ``main.py`` 的 ``/api/move`` 移到这里——坐标在盘内但 ``is_legal`` 不成立的落子同样是
        「被拒的一手」，在那里打点会污染 ``move_ts_list`` 与 ``avg_move_sec``）：
        本局第一手把它同时记成 ``session_start``（＝本局开始时刻），之后每一手只追加。
        下一手到达时，``time_ctx.facts`` 用相邻两个时刻之差结算**单手用时**（首手无前一手 ⇒ 不计）。

        ``now`` 留空用系统时钟；脏值（非数值 / 非正）**静默忽略**——采集绝不因为一个坏时间戳抛异常。
        本方法纯内存、零 IO、零调用；**悔棋不回退**（这一手确实等了这么久），``reset_game()`` 清空。
        """
        if now is None:
            t = time.time()
        else:
            if isinstance(now, bool):
                return
            try:
                t = float(now)
            except (TypeError, ValueError):
                return
        if t != t or t <= 0.0:              # NaN / 非正：脏值，不记
            return
        if self.session_start is None:
            self.session_start = t
        self.move_ts_list.append(t)
        # 落子也是“她还活着、和玩家在同一张桌上”的时间事实；只推进时间戳，不逐手写事件行。
        self._life_touch("game_move", event=False, now=t)

    def time_facts(self, *, now=None, last_game_ts=None, game_ts_list=None):
        """**时间块**的素材（纯计算、零 IO、零调用）：``time_aware_enabled`` 关着 ⇒ ``{}``。

        ``now`` 留空用系统时钟（``time_ctx.facts`` 本身要求显式锚点——时钟可注入才好测，这里替
        生产调用方补上）；``last_game_ts`` / ``game_ts_list`` 由调用方从 ``ProfileStore`` 读出来
        传进来（**那两次读是同步阻塞的，要 ``await asyncio.to_thread``**；本方法只做纯计算）。
        关着开关 ⇒ 空 dict ⇒ ``prompts.time_note`` 返回空串 ⇒ 提示词**逐字符不变**。

        装配了生命时间线时，再把 ``LifeStore.temporal_facts`` 的**相对时间**合并进来：
        距上次互动 / 主动开口 / 聊天 / 对局，以及昼夜相位。没有时间线 ⇒ 与旧版逐字符一致。
        """
        if not self.time_aware_enabled:
            return {}
        ref = time.time() if now is None else now
        facts = time_ctx.facts(now=ref,
                               session_start=self.session_start,
                               last_game_ts=last_game_ts,
                               move_ts_list=list(self.move_ts_list),
                               game_ts_list=game_ts_list)
        if not facts or self.life is None:
            return facts
        try:
            facts.update(self.life.temporal_facts(now=ref))
        except Exception as e:                  # noqa: BLE001
            print("[Life] 时间上下文计算失败（已忽略）：%s" % e)
        return facts

    def profile_facts(self):
        """**画像块**的素材：``profile_enabled`` 关着 / 未装配画像库 ⇒ ``{}``（＝不注入）。

        **同步读**（``ProfileStore.get_profile()`` 是阻塞的文件操作）——调用方必须
        ``await asyncio.to_thread(self.profile_facts)``，与 ``_long_term`` 同一套异步纪律。
        任何故障只打一行日志、返回 ``{}``：存储层自己已降级，这里再兜一次，绝不影响对局。
        """
        if not self.profile_enabled:
            return {}
        store = self.profile_store
        if store is None:
            return {}
        try:
            return store.get_profile() or {}
        except Exception as e:                      # noqa: BLE001 —— 读画像失败＝无画像
            print("[Agent] 画像读取异常（按「无画像」降级）：%s" % e)
            return {}

    # ================= 人格化：关系 / 情绪余温 / 今天的小心思 =================

    async def load_persona(self):
        """从 ``profile.db`` 的 persona 表读一次跨局状态（lifespan 调用；失败按空处理）。

        只读一次、缓存在实例上，**回合内零 IO**。情绪余温按「距上一局结束多久」向基线回落：
        刚结束几乎原样，隔一天剩约四成，隔三天基本平静（见 ``persona.mood_after_break``）。
        """
        if not self.persona_enabled:
            return
        store = self.profile_store
        if store is None:
            return
        try:
            data = await asyncio.to_thread(store.get_persona)
        except Exception as e:                      # noqa: BLE001 —— 读不到就当没有历史
            print("[Agent] 人格化状态读取异常（按初识降级）：%s" % e)
            data = {}
        self._persona = data if isinstance(data, dict) else {}
        try:
            self._interactions = int(self._persona.get("interactions_total") or 0)
        except (TypeError, ValueError):
            self._interactions = 0
        restored = persona.mood_after_break(self._persona.get("mood"),
                                             self._persona.get("last_game_ts"))
        if restored:
            self._lingering_mood = restored

    def _extra_note(self) -> str:
        """系统提示词附加段：人格化 + 生命状态 + 情绪语言节奏；都空 ⇒ 空串（提示词逐字符不变）。"""
        parts = [self.persona_note(), self.life_note(), self.emotion_language_note()]
        return "\n\n".join(p for p in parts if p)

    def emotion_language_note(self) -> str:
        """情绪 → 语言节奏（v4.3.0，C）：只把明显偏离基线的情绪翻译成句长 / 问句 / 快慢。

        基线附近返回空串（不改变系统提示词 / golden），也不会盖过人设；所有措辞都是
        Azure 自己的语气指令，不引入第二套人格。
        """
        try:
            return persona.emotion_language_note(self._mood_axis())
        except Exception as e:                  # noqa: BLE001 —— 语言节奏坏了不该影响出话
            print("[Agent] 情绪语言节奏生成失败（已忽略）：%s" % e)
            return ""

    def chat_budget(self) -> int:
        """聊天输出预算（v4.3.0，C）：默认 400；情绪低落 / 没精力收窄，明亮活跃放宽。"""
        try:
            return int(persona.chat_budget(self._mood_axis(), self._life_fields() or {}))
        except Exception:                       # noqa: BLE001
            return 400

    def persona_note(self) -> str:
        """人格化提示词附加段（关系 / 最近胜负 / 夜間语气 / 小目标 / 难度半句）。

        关着人格化 / 没有任何可说的内容 ⇒ ``""``（系统提示词逐字符不变）。全部是**语气指令**，
        明确要求模型不要逐条念出来；数据不足时宁可不写，不硬编。
        """
        if not self.persona_enabled:
            return ""
        parts = []
        if self.persona_relationship_enabled:
            stage = persona.relationship_stage(self._persona.get("games_total", 0),
                                               self._interactions)
            rel = persona.relationship_note(stage, prompts.user_name, self.persona_address_name)
            if rel:
                parts.append(rel)
        mem = persona.memory_note(self._persona.get("streak", 0))
        if mem:
            parts.append(mem)
        night = persona.night_note(time.localtime().tm_hour)
        if night:
            parts.append(night)
        intent = persona.intent_note(self._intent)
        if intent:
            parts.append(intent)
        if self._soft_note:
            parts.append(self._soft_note)
        if not parts:
            return ""
        return ("【此刻的你 · 只影响语气与分寸，不要逐条念出来】\n"
                + "\n".join("· " + p for p in parts))

    def _persona_bump_interaction(self):
        """互动计数（摸头等）：只进内存，局末随人格化状态一起落盘；复盘期间不计。"""
        if self.persona_enabled and not self.review:
            self._interactions += 1

    async def _persist_persona(self, state):
        """局末写人格化状态（关系局数 / 连续胜负 / 情绪快照）；任何故障只打日志。"""
        if not self.persona_enabled or self.profile_store is None:
            return
        data = dict(state.get("data") or {}) if isinstance(state, dict) else {}
        if not data:
            return
        try:
            wr = float(data.get("wr", 0.5))
        except (TypeError, ValueError):
            wr = 0.5
        result = "win" if wr < 0.5 else ("loss" if wr > 0.5 else "draw")   # Azure 视角
        prev = self._persona if isinstance(self._persona, dict) else {}
        try:
            games = int(prev.get("games_total") or 0) + 1
        except (TypeError, ValueError):
            games = 1
        old_streak = 0
        try:
            old_streak = int(prev.get("streak") or 0)
        except (TypeError, ValueError):
            old_streak = 0
        if result == prev.get("last_result"):
            if result == "win":
                streak = old_streak + 1 if old_streak > 0 else 1
            elif result == "loss":
                streak = old_streak - 1 if old_streak < 0 else -1
            else:
                streak = 0
        else:
            streak = 1 if result == "win" else (-1 if result == "loss" else 0)
        mood = {"v": round(float(self.mood.v), 4),
                "c": round(float(self.mood.c), 4),
                "a": round(float(self.mood.a), 4)}
        items = {
            "games_total": games,
            "first_game_ts": prev.get("first_game_ts") or time.time(),
            "last_game_ts": time.time(),
            "last_result": result,
            "streak": streak,
            "mood": mood,
            "interactions_total": int(self._interactions),
        }
        try:
            await asyncio.to_thread(self.profile_store.set_persona, items)
        except Exception as e:                      # noqa: BLE001 —— 写不进去只丢这一次
            print("[Agent] 人格化状态写入异常（已忽略）：%s" % e)
        self._persona.update(items)
        self._lingering_mood = mood                 # 下一局开头带一点余温
        self._soft_note = ""
        self._soft_note_turns = 0

    # ================= 对局记录与画像写入（v3.9.0，设计 §6.1 / §五 G） =================
    #
    # **写入点只有一处**：``summarize()``（手动结束对局）成功跑完那一回合之后。素材由
    # ``agent_graph._prepare_summary`` 备在 ``state["data"]`` 里（``game_rec`` / ``profile_prev``）
    # ——``profile_enabled`` 关着 / 未装配画像库时那两格根本不存在，这里什么都不做。
    # 两次同步库操作一律 ``await asyncio.to_thread``；任何故障只打日志、绝不影响对局。

    def _game_record(self, final_black_wr=0.5, final_score=0.0) -> dict:
        """组装本局的对局记录（spec §6.1 的键）；**纯内存、零 IO、零调用**。

        ``moves`` 的每条都带 ``color`` / ``gtp``，**玩家（黑）的每一条还带 ``drop``**
        （``0.0``＝量过、没亏；``> 0.10``＝真亏手）——这是 Q2 的 ``blunder.*`` / ``weak.corner_*``
        的**唯一信号源**，数据源就是 ``st.losses``（与俗手判定同源、只收 ``drop > 0.10``，
        且只收玩家那一手）。**只给玩家那一侧填 ``drop``**：Azure 自己的手不参与玩家的画像。

        ``ts`` 不在这里填：由 ``ProfileStore.add_game`` 按**落盘那一刻**的墙钟补
        （记录与画像的「局末」以写入时刻为准，与 ``games`` 表只增不减的语义一致）。
        ``summary`` 留空串，由 ``summarize()`` 在拿到总结原文后补（兜底台词不写进去）。
        """
        losses = {}
        for ls in self.st.losses:
            if not isinstance(ls, dict):
                continue
            try:
                losses[int(ls.get("move_no", -1))] = float(ls.get("drop", 0.0))
            except (TypeError, ValueError):
                continue
        moves = []
        for i, m in enumerate(self.st.moves, start=1):
            if not isinstance(m, (list, tuple)) or len(m) < 2:
                continue
            color = str(m[0])
            item = {"color": color, "gtp": str(m[1])}
            if color == "B":                    # 玩家：每一手都带 drop（0.0＝量过、没亏）
                item["drop"] = float(losses.get(i, 0.0))
            moves.append(item)
        return {
            "move_count": len(self.st.moves),
            "final_black_wr": float(final_black_wr),
            "final_score": float(final_score),
            "result": "黑方（%s）胜率 %s，目差 %s" % (prompts.user_name, self._pct(final_black_wr),
                                                     "%+.1f" % float(final_score)),
            "moves": moves,
            "per_move_ms": self._per_move_ms(),
            "joseki_families": self._joseki_families_played(),
            "chat_topics": chat_topics(self.st.chat_history),
            "summary": "",
        }

    def _per_move_ms(self) -> list:
        """相邻两次「玩家这一手到达」的间隔（毫秒）；**首手无前一手 ⇒ 不计**（与单手用时同口径）。

        纯内存、脏时刻逐条忽略；``reset_game`` 清空 ``move_ts_list``（新一局重新计）。
        """
        ts = []
        for t in self.move_ts_list:
            if isinstance(t, bool):
                continue
            try:
                f = float(t)
            except (TypeError, ValueError):
                continue
            if f == f and f > 0.0:              # NaN / 非正：脏值，忽略
                ts.append(f)
        return [round(max(0.0, b - a) * 1000.0, 1) for a, b in zip(ts, ts[1:])]

    def _joseki_families_played(self) -> list:
        """本局**玩家走过的定式族**（去重、保序）：从 ``st.joseki_log`` 的去重键里抠族名。

        键形如 ``E:<角>:<族>``（进入）/ ``B:<角>:<族>``（分岔）/ ``F:<角>:<类>``（成型）/
        ``D:<角>``（偏离）——族名只在 E / B 两类里，F / D 没有族 ⇒ 跳过。这是
        ``open.favorite_kakari`` 唯一的信号源（``joseki_families``），比 ``st.joseki_counted``
        全（后者只记「讲过的」那一族）。

        **玩家口径（v3.9.0 修复波）**：``_joseki_check()`` 在玩家落子后与她自己落子后**两处**都会
        调用、都往 ``joseki_log`` 追加；这里只收**玩家（黑）**那一手走出的族——否则
        ``open.favorite_kakari``（「他挂角比守角更主动」）会由两个人的落子算出来，与
        ``blunder.*`` / ``weak.corner_*`` 的玩家口径自相矛盾。``move_no`` 是**落子后**的手数
        （``len(st.moves)``，见 ``_joseki_check``），两处调用点都在 ``add_move`` 之后 ⇒
        ``st.moves[move_no - 1][0] == "B"`` 就是玩家那一手；悔棋回滚时 ``joseki_log`` 与
        ``moves`` 同随一份快照裁剪，索引口径一致。取不到手数（脏值 / 越界）时**保守跳过**。
        """
        out = []
        for e in self.st.joseki_log:
            key = str(e.get("key", "")) if isinstance(e, dict) else ""
            if not (key.startswith("E:") or key.startswith("B:")):
                continue
            try:
                mn = int(e.get("move_no"))
            except (TypeError, ValueError):
                continue                            # 取不到手数：不认领，宁可少算玩家偏好
            if mn <= 0 or mn > len(self.st.moves) or self.st.moves[mn - 1][0] != "B":
                continue                            # 她那一侧（W）走出的族不进玩家画像
            parts = key.split(":")
            fam = parts[2] if len(parts) > 2 else ""
            if fam != "" and fam not in out:
                out.append(fam)
        return out

    async def _persist_game(self, state):
        """把本局记录与融合后的画像落盘（v3.9.0 / Q7）：**写入点只在 ``summarize()`` 里**。

        ``state`` 是 summary 那一回合的终态（``_turn`` 的返回）；素材取不到（开关关着 /
        未装配库 / 这一代已被看门狗作废）⇒ 直接返回，一条都不写。任何故障只打日志。
        """
        data = dict(state.get("data") or {}) if isinstance(state, dict) else {}
        rec = data.get("game_rec")
        if not isinstance(rec, dict) or not rec:
            return
        if not data.get("failed"):              # 兜底台词不是总结：不写进记录（与长期记忆同一口径）
            rec = dict(rec)
            rec["summary"] = str(data.get("reply") or "")
        prev = data.get("profile_prev")
        try:
            await asyncio.to_thread(self._write_game_record, rec,
                                    dict(prev) if isinstance(prev, dict) else {})
        except Exception as e:                  # noqa: BLE001 —— 画像故障绝不影响对局
            print("[Agent] 画像写入异常（已忽略）：%s" % e)

    def _write_game_record(self, rec, prev):
        """同步落盘（调用方 ``await asyncio.to_thread``）：一条对局记录 + 融合后的整份画像。

        ``merge(prev, axes, 0)``：各维样本数一律以 ``prev`` 记录里的 ``n_games`` 为准（标量 ``0``
        只是「扁平 prev 没有样本数」时的兜底）；``extract_axes`` 对无信号的维**不产出**，
        ``merge`` 对缺键**原样带过**（不衰减、不涨计数、不改置信度）。
        """
        store = self.profile_store
        if store is None:
            return
        try:
            store.add_game(rec)
            store.upsert_axes(profile.merge(prev, profile.extract_axes(rec), 0))
        except Exception as e:                  # noqa: BLE001 —— 写不进去只丢这一次，绝不抛
            print("[Agent] 对局记录 / 画像写入异常（已忽略）：%s" % e)

    async def _persist_library(self, state):
        """把这一局写进棋谱库（v3.11.5）：**与画像开关无关**，每次「🏁 结束」归档一次。

        素材直接取 ``self.st``（moves / 逐手点评 / 亏损手账）与 summary 终态的 ``wr`` / ``score`` /
        ``reply``；LLM 兜底台词（``failed``）不写进 summary，但棋谱本身照存。
        ``_library_sig``＝(手数, 末手) 挡住同一次对局重复点「结束」造成的重复归档；``reset_game`` 清空。
        任何故障只打日志，绝不影响对局。
        """
        store = getattr(self, "game_store", None)
        if store is None or not self.st.moves:
            return
        data = dict(state.get("data") or {}) if isinstance(state, dict) else {}
        moves = []
        for m in self.st.moves:
            if isinstance(m, (list, tuple)) and len(m) >= 2:
                moves.append([str(m[0]), str(m[1])])
        if not moves:
            return
        sig = (len(moves), str(moves[-1][1]))
        if sig == self._library_sig:
            return
        summary = "" if data.get("failed") else str(data.get("reply") or "")
        rec = {
            "moves": moves,
            "move_count": len(moves),
            "final_black_wr": data.get("wr"),
            "final_score": data.get("score"),
            "result": "",
            "summary": summary,
            "meta": {
                "black": str(getattr(self, "player_name", "") or "玩家"),
                "white": "Azure",
                "source": "game",
                "notes": [dict(n) for n in self.st.move_notes if isinstance(n, dict)],
                "losses": [dict(l) for l in self.st.losses if isinstance(l, dict)],
            },
        }
        try:
            game_id = await asyncio.to_thread(store.add_game, rec)
        except Exception as e:                  # noqa: BLE001 —— 棋谱库故障绝不影响对局
            print("[Agent] 棋谱归档异常（已忽略）：%s" % e)
            return
        if game_id:
            self._library_sig = sig

    # ================= 自适应难度（v3.11.5） =================

    def _difficulty_profile_shift(self) -> int:
        """画像给基线的 ±1 档修正；画像关着 / 库不可用 ⇒ 0（绝不影响开局）。"""
        if not self.profile_enabled or self.profile_store is None:
            return 0
        try:
            return adaptive.profile_adj(self.profile_facts())
        except Exception as e:                  # noqa: BLE001 —— 画像读取失败＝无修正
            print("[Adaptive] 画像修正读取失败（按 0 处理）：%s" % e)
            return 0

    def _reset_adaptive(self) -> None:
        self.adaptive.reset(self._difficulty_profile_shift())
        self._emit_difficulty()

    def configure_adaptive(self, enabled, base, spread=None) -> None:
        """界面 / 配置接通时调用：基线变化才重置到基线，并通知前端状态栏。"""
        if spread is not None:
            try:
                self.adaptive.spread = max(0, min(2, int(spread)))
            except (TypeError, ValueError):
                pass
        self.adaptive.configure(enabled=enabled, base=base,
                               profile_shift=self._difficulty_profile_shift())
        self._emit_difficulty("")

    def _emit_difficulty(self, reason="") -> None:
        try:
            self._emit(events.difficulty(self.adaptive.level, self.adaptive.base,
                                         reason or self.adaptive.last_reason))
        except Exception as e:                  # noqa: BLE001 —— 一个状态事件不许影响对局
            print("[Adaptive] 难度事件发布失败（已忽略）：%s" % e)

    def _difficulty_pick(self, move_infos, pick):
        """把已有候选按当前档位弱化（档位 5 / 关闭时原样返回）；零额外引擎调用。"""
        if not self.adaptive.enabled:
            return pick

        def legal(gtp: str) -> bool:
            if gtp == "pass":
                return bool(self.st.allow_pass)
            x, y = coords.gtp_to_xy(gtp)
            return x >= 0 and y >= 0 and bool(self.st.board.is_legal(x, y, "white")["ok"])

        try:
            return self.adaptive.choose(list(move_infos or []), legal, self.st.allow_pass, pick)
        except Exception as e:                  # noqa: BLE001 —— 选点异常回到原落点
            print("[Adaptive] 落点弱化异常（回到原落点）：%s" % e)
            return pick

    def note_adaptive_move(self, move_no, black_wr, rank, drop) -> None:
        """玩家这一手的表现进自适应窗口（零额外调用）；真的调档才推一条 difficulty 事件。"""
        if not self.adaptive.enabled:
            return
        old_level = self.adaptive.level
        try:
            if self.adaptive.note_move(move_no, black_wr, rank, drop):
                if self.persona_enabled and self.adaptive.level < old_level:
                    self._soft_note = persona.adaptive_soft_note(old_level, self.adaptive.level)
                    self._soft_note_turns = 1
                self._emit_difficulty("")
        except Exception as e:                  # noqa: BLE001 —— 难度调节失败不影响对局
            print("[Adaptive] 表现记录失败（已忽略）：%s" % e)

    # ================= 主动约战 / 提醒（v3.9.0，设计 §5-E） =================

    async def greet_with_context(self, *, now=None) -> str:
        """玩家连上 / 首次交互时的**主动约战提醒**：**本次进程只触发一次**，零新增 LLM 调用。

        触发点只有一处——``main.py`` 的 WS 处理器在发出 ``hello`` 之后调它（连接即触发）；
        实例上的 ``_greeted`` 保证**跨重连不重复**（``reset_game`` 有意不重置：打过招呼是这次
        会话的事实）。它只读一次「距上次对局」（``ProfileStore.last_game_ts()``，阻塞读 ⇒
        ``await asyncio.to_thread``），按天数挑一句**模板台词**（``_greet_line``）发一条 ``message``。

        与 ``undo`` / ``reset_game`` 的短提示同一做法：**纯文字、不发 ``speech``**——但它是**推送型**
        （前端无从预知），所以走 ``events.AI_NOTICE``：**由推送方声明「这条没有语音」**，前端据此
        **不登记语音配对记录**（修复轮 1 / C1）。前端仍按「她这一轮已结束」当场显示，不引入合成等待。

        「这一次机会」只在**真的发了台词**之后才用掉（修复轮 1 / Minor #1）：没有历史对局 /
        距上次不足一天 / 棋盘上已经有子（不是「刚连上」）⇒ 什么都不说，**下次连接还能再说**；
        ``_greet_busy`` 只挡**并发**（同一时刻两个连接同时进来时只有一个能发），不是「用过就没了」。

        **不额外加开关**（控制者裁决保留：主动约战是独立延伸；代价与「关掉画像会连带让提醒消失」
        的隐式耦合由 Q8 文档写明）：未装配画像库或读失败同样只是「这次不打招呼」。
        任何故障都不抛、不碰 ``busy``、不调模型、不调引擎。返回发出去的台词（没发 ⇒ 空串）。
        """
        if self._greeted or self._greet_busy:      # 已经发过 / 正在算：都不再打第二遍
            return ""
        if self.st.moves:                           # 不是「刚连上」：这一局已经在下 / 下完了
            return ""
        self._greet_busy = True                     # 只挡**并发**：两个连接同时进来时只有一个能发
        try:
            last_ts = None
            store = self.profile_store
            if store is not None:
                try:
                    last_ts = await asyncio.to_thread(store.last_game_ts)
                except Exception as e:              # noqa: BLE001 —— 读不到就当「没有上次」
                    print("[Agent] 距上次对局读取失败（按「没有上次」降级）：%s" % e)
                    last_ts = None
            try:
                facts = time_ctx.facts(now=time.time() if now is None else now, last_game_ts=last_ts)
            except Exception as e:                  # noqa: BLE001 —— 时间算不出来就不打招呼
                print("[Agent] 时间事实计算失败（这次不打招呼）：%s" % e)
                return ""
            text = self._greet_line(facts.get("last_gap_days"))
            if text == "":
                return ""                           # 没有历史 / 不足一天：**不吃掉**这次机会（修复轮 1 / Minor #1）
            self._greeted = True                    # 确实发了台词，才把这一次机会用掉
            # `ai_notice`＝**推送方声明「这条没有语音」**（修复轮 1 / C1）：它不发 `speech`，
            # 前端据此**不登记语音配对记录**——否则它会占掉配对位，把连接后第一回合的三条真语音
            # 整体错位（点评等到落子宣告的音频才显示、揭示被推后、最后一条没声音）。
            self._emit(events.message(events.AI_NOTICE, text))
            self._life_touch("greet", source="startup")
            self._mood_event("reunion", burst="pleased")   # 隔了几天又见：心里是真的高兴
            return text
        finally:
            self._greet_busy = False

    # =================玩家落子 =================

    async def player_move(self, x, y):
        """玩家落子：点评 → 她思考落子 → 落子后感想。

        多阶段回合：依次跑 ``after_player → own_turn → just_moved`` 三个阶段的图（顺序见
        ``agent_graph.py``）。前置判定（已忙 / 非法手）留在公开契约里：非法手**不置 busy、不调引擎**，
        **也不打时间戳**（时间感知的采集点在合法性判定之后，修复轮 1 / I-1），
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
        # 时间感知的采集点（v3.9.0，设计 §5-H；修复轮 1 / I-1 起**放在合法性判定之后**）：
        # 只有真的一手（``is_legal`` 通过）才算「到达」，被拒的落子不打点——否则 ``session_start``
        # 会被一次失败尝试置位、``move_ts_list`` 多一个幽灵时刻、``avg_move_sec`` 的间隔被算歪。
        if self.move_ts_list:
            gap = time.time() - float(self.move_ts_list[-1])
            if 45.0 <= gap <= 900.0:
                self._mood_event("patient_wait")     # 你想了好一会儿：她安静地等，不催
        self.mark_move_time()
        owner = self._set_busy(True)
        self._new_turn()                            # 三阶段共用同一轮标识（S33）
        token = self.begin_epoch()                  # 三阶段共用同一代（看门狗超时后整代作废）
        try:
            await self._turn_guard(self._player_move_turns(x, y, token), token)
        except Exception as e:                      # 编排意外抛出（LLM/引擎）：不得让 busy 永久为真
            print("[Agent] 落子编排异常：%s" % e)
        finally:
            self._clear_busy(owner)                 # 只清自己那份：别解开后来新回合的锁

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
        if not self.emotion_drift and not self.emotion_style:
            return pick
        lines = pv_lines.extract_lines(list(move_infos or []), max_drop=max(self.pv_max_drop, STYLE_MAX_DROP))
        mood = self.mood_payload()
        letter = "a"
        mode = "drift"
        if self.emotion_drift:
            letter = self._maybe_drift(lines, mood, self.drift_rng)
        if letter == "a" and self.emotion_style:
            letter = self._style_letter(lines, mood, self.drift_rng)
            mode = "style"
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
            "mode": mode,
        }
        if mode == "style":
            self._style_until_move = len(self.st.moves) + STYLE_COOLDOWN_MOVES
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
            # **防御性冗余**（不是主判据）：`_drift_pick` 已经用同一个 `max_drop` 过滤过
            # `pv_lines.extract_lines`，生产上取不到「超限」的线；留着是为了本函数被单独调用
            # （单测 / 将来别处的调用方）时也守同一条边界——别把它当主闸门改。
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

    def _style_letter(self, lines, mood, rng) -> str:
        """情绪棋风：心情好 / 兴奋时，在胜率差 ≤ ``STYLE_MAX_DROP`` 的 B 线里选一手更锐的。

        与「情绪波动」分开：这里不是低落时的走偏，而是**她自愿**的风格选择；低落 / 平静
        一律走 A（更稳）。冷却 ``STYLE_COOLDOWN_MOVES`` 手，避免每手都想试。
        任何线缺失 / 胜率差超限 / 非法手 → 安全回落 ``"a"``。
        """
        if not self.emotion_style:
            return "a"
        if len(self.st.moves) < self._style_until_move:
            return "a"
        if not isinstance(lines, dict):
            return "a"
        if not self._line_usable(lines.get("a")) or not self._line_usable(lines.get("b")):
            return "a"
        drop = _as_float(lines.get("drop"), 1.0)
        if drop > STYLE_MAX_DROP:
            return "a"                      # 只在她自己也觉得「差不多」的手上试
        v = _mood_dim(mood, "v")
        a = _mood_dim(mood, "a")
        if v is None or a is None:
            return "a"
        if v < STYLE_MIN_V or a < STYLE_MIN_A:
            return "a"                      # 低落 / 平静：更稳
        p = 0.04 + 0.20 * max(0.0, v - 0.58) + 0.12 * max(0.0, a - 0.30)
        p = min(STYLE_MAX_P, p)
        if _rng_random(rng) >= p:
            return "a"
        return "b"

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
        mode = str(drift.get("mode") or "drift")
        if mode == "style":
            text = ("我在第%d手按自己的棋感选了一手：没走最稳的%s，选了%s（两者胜率只差约%d%%）"
                    % (move_no, drift.get("best") or "（未知）", drift.get("gtp"),
                       int(round(_as_float(drift.get("drop"), 0.0) * 100.0))))
            kind = "ai_style"
        else:
            text = "我在第%d手自主偏航：没走最优的%s，选了%s（与最优的胜率差约%d%%）" % (
                move_no, drift.get("best") or "（未知）", drift.get("gtp"),
                int(round(_as_float(drift.get("drop"), 0.0) * 100.0)))
            kind = "ai_bad"
        self.event_log.add(kind, move_no, text, {
            "line": str(drift.get("line") or ""),
            "gtp": str(drift.get("gtp") or ""),
            "best": str(drift.get("best") or ""),
            "drop": _as_float(drift.get("drop"), 0.0),
            "mood": str(drift.get("mood") or ""),
            "mode": mode,
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

    async def _digest_chat_llm(self, token=None) -> int:
        """可选（``chat_digest_llm=true``）：用 LLM 分段总结聊天并写进事件表。

        **只在局末 ``summarize()`` 与空闲 ``idle_remark()`` 补做**——落子 / 聊天回合内一次都不调
        （性能红线）。它跑在 ``_turn_guard`` 之外，所以自带 ``CHAT_DIGEST_TIMEOUT_SEC`` 单次超时
        （Minor #4：不给端点留两分钟的尾巴）；未启用 / 没聊过 / LLM 失败 / 超时 / 解析不出都只打日志
        并返回 0，绝不影响对局，也不动 ``busy`` 的归位位置。

        ``token`` 是调用方那一代的代数（可选）：**写表之前再核一次**（修复波 / Minor #2）——上面那次
        ``await`` 期间玩家可能已经起了新回合（``begin_epoch``），而摘要里的 ``move_no`` 取的是写表
        那一刻的 ``len(st.moves)``：不核代际的话，一份描述**旧**聊天的摘要会带上比它描述的那段聊天
        更新的手数。取不到 ``token``（老调用方 / 单测）时跳过复核，行为与既有逐字一致。
        """
        if not self.chat_digest_llm or self.llm is None:
            return 0
        lines = []
        for m in list(self.st.chat_history)[-CHAT_DIGEST_MAX_MSGS:]:
            if not isinstance(m, dict):
                continue
            who = "玩家" if str(m.get("role", "")) == "user" else "我"
            t = " ".join(str(m.get("content", "")).split())
            if t != "":
                lines.append("%s：%s" % (who, t))
        if not lines:
            return 0
        prompt = ("下面是这盘棋里我和玩家的聊天记录。请按主题把它分成若干段，每段一行，"
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
        if token is not None and not self.epoch_ok(token):   # 死代：一条都不写（见 docstring）
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
        owner = self._set_busy(True)
        self._new_turn()
        token = self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_OBSERVE), token)
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 提示异常：%s" % e)
            await _emit_line(self, events.AI_COMMENT, self._fallback_line("game"))
        finally:
            self._clear_busy(owner)

    async def analyze_position(self):
        """局面分析：以 Azure（执白）的视角解读局势（顺序见 ``agent_graph.py`` 的 analyze 阶段）。"""
        if self.busy:
            return
        if not self.st.moves:
            self._new_turn()                        # 空盘提示也是对外台词（T20①）
            await _emit_line(self, events.AI, "棋盘还空着呢~ 先落几手，我再帮你看看形势。")
            return
        owner = self._set_busy(True)
        self._new_turn()
        token = self.begin_epoch()
        try:
            await self._turn_guard(self._turn(PHASE_ANALYZE), token)
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 分析异常：%s" % e)
            await _emit_line(self, events.AI, "📊 " + self._fallback_line("game"))
        finally:
            self._clear_busy(owner)

    async def chat(self, text, mode="game"):
        """自由聊天（顺序见 ``agent_graph.py`` 的 chat 阶段）。

        ``mode`` 只用于共享记忆：``pet``（桌宠聊天面板）或 ``game``（围棋内聊天）。
        """
        self.cancel_background_llm()          # v4.2.8：新聊天优先，先让后台情绪小调用让出生成锁
        if self.busy:
            return
        if text is None or text.strip() == "":
            return
        self._life_touch("chat", source=mode, summary=text)
        self._chat_seq += 1
        self.st.chat_history.append({"role": "user", "content": text})
        # 自适应难度：玩家明确说「太难 / 认真点」时立刻调一档（受 4 手冷却与基线 ±1 约束）
        if self.adaptive.enabled and self.adaptive.note_chat(text, len(self.st.moves)):
            self._emit_difficulty("")
        # 按聊天内容即时结算情绪（本地关键词分类）
        kind = self.chat_emotion(text)
        ev = self._chat_event(kind)
        if ev == "chat_normal" and self.is_casual_chat(text):
            ev = "casual_play"                     # 「随便下下」：一起松下来，不是普通闲聊
        self._mood_event(ev, "，对面说：「%s」" % text[:40].strip())
        owner = self._set_busy(True)
        self._new_turn()
        token = self.begin_epoch()
        lane_started = lanes.begin(lanes.LANE_FOREGROUND)
        try:
            await self._turn_guard(self._turn(PHASE_CHAT, {"text": text, "mode": str(mode or "game")}), token)
        except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
            print("[Agent] 聊天异常：%s" % e)
        finally:
            self._clear_busy(owner)
            lanes.end(lanes.LANE_FOREGROUND, lane_started)
        # 她回复完之后：把这轮聊天按主题写进事件表（规则式、零 LLM；事件原文永不进台词）。
        # 代际守卫：看门狗刚超时 / 本回合被新调用作废时，这一代不再写事件。
        if self.epoch_ok(token):
            self._record_chat_events()

    # ================= 事务提醒（v4.1.0） =================

    @staticmethod
    def _reminder_fallback(item) -> str:
        """LLM 不可用 / 回复失败时的模板提醒（纯文本，绝不空口）。"""
        title = str((item or {}).get("title", "") or "").strip() or "这件事"
        when = str((item or {}).get("when_text", "") or "").strip()
        if len(when) > 40:
            when = when[:39] + "…"
        text = "到点了：「%s」" % title
        if when:
            text += "（%s）" % when
        return text + "，别忘了。"

    async def remind_schedule(self, item, now=None) -> str:
        """到点提醒：事务喂给 LLM 生成一句短提醒；没连接 / 失败时用模板气泡。

        边界：
        - **不进 busy、不跑编排图**：提醒不该占住棋盘；只走一次快速 LLM 调用（生成锁串行）；
        - 发 ``ai_remind``（**不发 speech**），前端用常驻提醒卡显示；聊天 / 资料面板开着也能看到；
        - **不写聊天历史**（提醒不是对话），由调用方（``main.py`` 的提醒循环）写 ``reminded_ts``；
        - 任何异常都退回模板，绝不让提醒循环挂掉。
        """
        title = str((item or {}).get("title", "") or "").strip()
        if title == "":
            return ""
        when = str((item or {}).get("when_text", "") or "").strip()
        ref = float(now) if now is not None else time.time()
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ref))
        except (OverflowError, OSError, ValueError):
            stamp = ""
        reply = ""
        text = ""
        try:
            prompt = (
                "现在是 %s。玩家的日程「%s」到时间了%s。"
                "用一句很短、自然的中文提醒他（25 字以内）：像朋友提醒，不要像任务播报，"
                "不要提“日程表 / 数据库 / 系统”，不要编造别的事。"
            ) % (stamp, title, ("（%s）" % when) if when else "")
            msgs = [self._sys_msg(), {"role": "user", "content": prompt}]
            reply = await self._reply(msgs, 80, think_mode=0)
            text = actions.naturalize(reply).strip()
        except Exception as e:                  # noqa: BLE001 —— 提醒失败只降级，不影响主循环
            print("[Agent] 事务提醒生成失败（用模板）：%s" % e)
            text = ""
        if text == "" or self._llm_failed(reply):
            text = self._reminder_fallback(item)
        self._emit(events.message(events.AI_REMIND, text))
        return text

    async def idle_remark(self, mode="game"):
        """闲置搭话：玩家一段时间没动静时，Azure 主动说一句短话。

        同样走看门狗（超时也解锁 + 前推一代），只是它**不进 busy**：与今天一致（空闲搭话不锁棋盘）。
        注意「新的公开调用会 ``begin_epoch()``」顺带带来了一个既有语义的加强：玩家一回来就落子时，
        还在途的走神搭话那一代随即作废，它那句就不再冒出来——与前端「走神搭话可整批作废」一致。
        """
        if self.busy:
            return
        self._life_touch("idle", source=mode)
        self._new_turn()
        token = self.begin_epoch()
        await self._turn_guard(self._turn(PHASE_IDLE, {"mode": str(mode or "game")}), token)
        # 空闲：`chat_digest_llm=true` 时在这里补做一次 LLM 分段总结（默认关，零额外调用）。
        # 代数一并带进去：那一趟 LLM ``await`` 里若玩家起了新回合，摘要不再写表（修复波 / Minor #2）。
        if self.epoch_ok(token):
            await self._digest_chat_llm(token)

    async def peer_remark(self, info, mode="game"):
        """窥屏搭话：玩家切去别的窗口时，结合前台窗口 / OCR / VLM 描述主动说一句。

        与 ``idle_remark`` 同一条「不进 busy」的通道（空闲搭话不锁棋盘），但 prompt 里带上看到的内容。
       玩家一回来就会 ``begin_epoch()``，这一代随即作废——与前端丢弃迟到的窥屏结果一致。
        """
        if self.busy:
            return
        title = str(info.get("fg_title", "")) if isinstance(info, dict) else ""
        self._life_touch("peer", source=mode, summary=title)
        self._new_turn()
        token = self.begin_epoch()
        await self._turn_guard(
            self._turn(PHASE_PEER, {"info": info if isinstance(info, dict) else {},
                                    "mode": str(mode or "game")}), token)

    async def summarize(self, note=""):
        """对局总结（顺序见 ``agent_graph.py`` 的 summary 阶段）。

        v3.9.0（Q7）：这一回合跑完之后，若 ``profile_enabled`` 且装配了画像库，就把**本局记录**
        （``games`` 表）与**融合后的画像**（``profile`` 表）落盘——素材由 ``_prepare_summary``
        备在终态的 ``data`` 里（那一格不存在就什么都不写）。两次库操作都走 ``to_thread``。
        """
        if self.busy:
            return
        if not self.st.moves:
            self._new_turn()                        # 空盘提示也是对外台词（T20①）
            await _emit_line(self, events.AI, "还没下过棋呢~")
            return
        self._life_touch("game_end", source="game", summary=note)
        owner = self._set_busy(True)
        self._new_turn()
        token = self.begin_epoch()
        state = None
        try:
            state = await self._turn_guard(self._turn(PHASE_SUMMARY, {"note": note}), token)
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 总结异常：%s" % e)
            await _emit_line(self, events.AI, "📋 对局总结\n" + self._fallback_line("chat"))
        finally:
            self._clear_busy(owner)
        # 局末（Q7）：**写入点只有这里**——本局记录 + 融合后的画像（开关关着 / 无素材 ⇒ 不写）
        if self.epoch_ok(token):
            await self._persist_game(state)
        # 棋谱库（v3.11.5）：与画像开关解耦，每局「结束」归档一次（重复点结束由 _library_sig 挡住）
        if self.epoch_ok(token):
            await self._persist_library(state)
        # 人格化（关系 / 连续胜负 / 情绪余温）：同样与画像开关解耦；关着人格化 ⇒ 一条都不写。
        if self.epoch_ok(token):
            await self._persist_persona(state)
        # 局末：`chat_digest_llm=true` 时在这里补做一次 LLM 分段总结（默认关，零额外调用）。
        # 代数一并带进去：那一趟 LLM ``await`` 里若玩家起了新回合，摘要不再写表（修复波 / Minor #2）。
        if self.epoch_ok(token):
            await self._digest_chat_llm(token)

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
        now = time.time()
        self._undo_ts = [t for t in self._undo_ts if now - t <= 90.0]
        repeated = len(self._undo_ts) > 0
        self._undo_ts.append(now)
        self._undo_ts = self._undo_ts[-3:]
        self._mood_event("undo_repeat" if repeated else "undo")
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

    def reset_game(self, silent=False):
        # v3.7.0：开新一代——新一局开始，上一代（可能还在后台收尾）的迟到写入一律作废
        self.begin_epoch()
        self._life_touch("game_start", source="game")
        keep_pass = self.st.allow_pass
        self.st.reset()
        self._library_sig = None                 # 新一局：同长度同末手也不再算「已归档」
        self._reset_adaptive()                    # 新一局：难度回到基线（按画像修正）
        self.st.allow_pass = keep_pass
        self._joseki_taught = {}                 # 新一局：门控的冷却表清空（手数从头数）
        # v3.12.2：上一局还没揭示的落子 / 待记账的情绪偏航不许漏进新局。
        # 两者都是**局内瞬时状态**，重开即失效；不写回 False 会让重来后的第一份
        # state 仍按「有一手等着 reveal」处理（前端要等一次永远不会来的语音）。
        self._reveal_pending = False
        self._drift_pending = None
        # v3.8.0：事件表是**局内内存**——新一局不带上一局的账（与「不随悔棋回退」是两回事，
        # 设计 §5 E/F：悔棋＝「发生过就是发生过」，重开＝另起一局）
        self.event_log.reset()
        # v3.9.0：时间感知的采集同样是**局内内存**——新一局从零开始（本局时长 / 单手用时重新计）
        self.session_start = None
        self.move_ts_list = []
        # 人格化（v3.x 自用版）：上一局的情绪余温带一半进新局；没有余温 / 关着 ⇒ 回到平静基线。
        # 「今天的小心思」每局重抽（小概率才有），难度半句类的一次性提示一并清掉。
        if self.life is not None:
            # 装配了生命时间线（桌宠 ↔ 围棋共享）：当前情绪已经是“最新事实”，
            # 新局保留它；旧的局末余温不再覆盖（life 快照比 game 结束快照更新）。
            self._lingering_mood = None
        elif self.persona_enabled and self._lingering_mood:
            self.mood.carry(self._lingering_mood, keep=0.45)
            self._lingering_mood = None
        else:
            self.mood.reset()
        self._soft_note = ""
        self._soft_note_turns = 0
        self._intent = persona.pick_intent(self.persona_rng) if self.persona_enabled else None
        self._undo_ts = []                       # 新局：连悔计数 / 摸头惯性 / 情绪来由都重新开始
        self._pat_ts = []
        self._mood_reason = ""
        self._mood_reason_until = -1
        self._style_until_move = 0
        self.sync_mood(True)
        self._emit(events.state(self.st.payload()))
        # v3.12.1：从主界面进对局用 ``silent=True``——开场不该每次都多一句「重新来吧」；
        # 玩家点「重来」仍走默认（有这一句）。
        if not silent:
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
        payload = {
            "region": self.mood.region(),
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": str(info.get("filler", "")),
            "burst": str(info.get("burst", "")),
            "pitch": float(info.get("pitch", 0.0)),
        }
        payload.update(self._life_fields())       # 未装配生命时间线 ⇒ 形状与旧版完全一致
        return payload
