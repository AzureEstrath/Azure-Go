"""回合编排状态图（LangGraph）。

计划 04 的目标是把 ``AzureAgent`` 里「写死的函数调用顺序」显式成一张状态图。本文件是它的
**唯一编排实现**：六个需要 LLM 编排顺序的公开方法（``player_move`` / ``hint`` /
``analyze_position`` / ``chat`` / ``idle_remark`` / ``summarize``）在 ``agent.py`` 里只做
「构造初始状态 → 跑图」；引擎调用、提示词、台词、事件与 ``memory_log`` 标签的顺序全在这里
的四个节点里。其余公开方法（``undo`` / ``reset_game`` / ``set_allow_pass`` / ``reveal_move``
/ ``tick_mood`` / ``player_interaction``）不需要 LLM 编排，仍是同步的「直接发事件」。

拓扑：``prepare → decide → speak → finalize → END``，四条边都是**无条件的**。
**一次 ``graph.ainvoke`` 处理一个阶段**（``state["phase"]``）：``player_move`` 是多阶段
（``after_player → own_turn → just_moved``），公开方法依次跑三次、中间数据经 ``state["data"]``
透传；其余公开方法各跑一次。这样既保住了 ``busy`` 的多阶段序列（S12），也让每个节点的语义
保持单一。

约定（后续任务必须守住）：

- ``TurnState`` 的 7 个原名段是本图与 ``AzureAgent`` 之间的**唯一接口**，一个都不能少、不许改名；
  本任务新增 ``data``（每回合中间数据：引擎分析 / 落点候选 / 提示词 / 定式结果 / 短路口径），
  ``message`` 的 kind 与 ``memory_log`` 的 tag 一律由 ``phase`` 推导，不另设字段（S11）。
- ``busy`` 归位由**调用方**（``AzureAgent`` 的公开方法）用 ``try/finally`` 兜住——那是异常路径的
  安全网；节点也在与今天**逐字相同的位置**调用 ``_set_busy(False)``（``chat`` 是「思考完就解锁、
  再追加历史与发话」，``just_moved`` / ``observe`` / ``analyze`` / ``summary`` 是发完事件之后），
  ``_set_busy`` 自带去重，多调一次不会产生多余的 ``busy`` 事件。
- 台词一律经 ``agent.clean_reply()``（``_reply()`` 内部必经），推理段永不进台词；
- ``build_graph(agent)`` 用闭包捕获 ``agent``，模块级不持有任何可变状态：同一个 ``agent`` 重建图
  是安全的，不同 ``agent`` 各有一张图。
"""

import asyncio
import random
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import coords, events, prompts

# 节点名（顺序即拓扑顺序）：prepare → decide → speak → finalize
NODE_NAMES = ("prepare", "decide", "speak", "finalize")

# 一次回合内**总 LLM 调用次数**上限（含工具轮）：工具预算之外的硬闸，由 decide 消费（Task 3）
MAX_LLM_CALLS_PER_TURN = 4

# —— 阶段：一次 graph.ainvoke 处理一个 ——
PHASE_AFTER_PLAYER = "after_player"     # 玩家落子：引擎分析 + 落子 + 定式 + 情绪 + 点评
PHASE_OWN_TURN = "own_turn"             # 她思考并落子（含落子宣告与应手）
PHASE_JUST_MOVED = "just_moved"         # 她落子后的形势与感想
PHASE_OBSERVE = "observe"               # hint()
PHASE_ANALYZE = "analyze"               # analyze_position()
PHASE_CHAT = "chat"                     # chat()
PHASE_IDLE = "idle"                     # idle_remark()
PHASE_SUMMARY = "summary"               # summarize()

# 落子后的「犹豫节拍」：与她落子宣告同一段编排，位置与旧 _ai_turn 逐字一致
THINK_BEAT_SEC = 0.35


class TurnState(TypedDict):
    """一次回合的状态：本图与 ``AzureAgent`` 之间的唯一接口（7 个原名段一个都不能少）。

    ``data`` 是本任务新增的**每回合中间数据**：引擎分析、落点候选、提示词、定式结果、
    ``short``（引擎不可用时的短路标记）都在这里，节点之间靠它透传；因为 LangGraph 只接受
    声明过的键，跨节点的中间结果必须有这么一个通道。
    """

    phase: str              # 回合类型：after_player / own_turn / just_moved / observe / analyze / chat / idle / summary
    moves: list             # 阶段开始时的棋谱快照（[[color, gtp], ...]，取自 agent.st.moves）
    messages: list          # 发给模型的消息（系统 + 上下文 + 历史窗口）
    reply: str              # 本阶段已说出口的台词（必经 clean_reply）
    tool_calls: int         # 已执行的工具调用次数（上限 agent.max_tool_calls）
    move: dict | None       # 落点 {x, y, gtp, pass}；None = 本阶段不落子
    turn_id: str            # 本轮唯一标识（流式 token 与重复发话的判据都靠它）
    data: dict              # 本阶段的中间数据（引擎分析 / 提示词 / 定式结果 / short）


# ================= ① prepare：建上下文与提示词 =================

async def _prepare_after_player(agent, data):
    """玩家落子：两次引擎分析 → 落子入谱 → 定式 → 情绪 → 点评提示词。"""
    x = int(data["x"])
    y = int(data["y"])
    gtp = coords.xy_to_gtp(x, y)
    n = len(agent.st.moves)

    # ① 玩家落子前局面：缓存复用，否则现算（黑方视角，用于给这一手排名）
    analysis = {}
    if agent.st.pre_analysis is not None and int(agent.st.pre_analysis["n"]) == n:
        analysis = agent.st.pre_analysis["a"]
    else:
        analysis = await agent.katago.analyze(agent.st.moves)
        if "error" in analysis:
            print("[KataGo] " + str(analysis["error"]))
            analysis = {}
        else:
            agent.st.pre_analysis = {"n": n, "a": analysis}
    winrate_before = 0.5
    move_infos = []
    if analysis:
        winrate_before = float((analysis.get("rootInfo") or {}).get("winrate", 0.5))
        move_infos = analysis.get("moveInfos", [])
    player_rank = -1
    for i in range(min(len(move_infos), 5)):
        if str(move_infos[i].get("move", "")) == gtp:
            player_rank = i
            break

    # ② 玩家落子后局面（轮到白方）：用于选择 Azure 的应手
    after = await agent.katago.analyze(agent._moves_with("B", gtp))
    after_infos = []
    after_black_wr = 0.5
    after_black_score = 0.0
    if "error" in after:
        print("[KataGo] " + str(after["error"]))
    else:
        after_infos = after.get("moveInfos", [])
        conv = prompts.to_black_perspective(after, n + 1)
        after_black_wr = float(conv[0])
        after_black_score = float(conv[1])
        agent._update_katago_brief(after, n + 1)       # 当前棋面的引擎形势，供 Azure 上下文

    # ③ 提交玩家落子（先存快照，供俗手回档）
    agent.st.snapshots.append(agent.st.make_snapshot())
    agent.st.board.play(x, y, "black")
    agent.st.add_move("B", gtp)
    turn_index = len(agent.st.snapshots) - 1

    # ④ 俗手判定：不在我算的前5，或胜率明显下滑
    drop = winrate_before - after_black_wr
    if player_rank < 0 or drop > 0.10:
        reason = "不在我算的前5候选" if player_rank < 0 else "胜率下滑%d%%" % int(round(drop * 100.0))
        agent.st.record_bad_move(turn_index, gtp, reason)
    agent._emit(events.state(agent.st.payload()))

    # ④″ 定式识别（纯本地计算、零引擎调用）
    jk = agent._joseki_check()
    if jk:
        agent._joseki_emit(jk)

    # ④′ 情绪反应：按这一手的好坏结算三维情绪（即时反馈不等 LLM）
    ev = "player_ok"
    if str(jk.get("kind", "")) == "formed" or player_rank == 0:
        ev = "player_good"
    elif player_rank < 0 or drop > 0.10:
        ev = "player_bad"
    elif player_rank >= 3:
        ev = "player_off"
    agent._mood_event(ev)

    # ⑤ Azure 点评这一手（定式成型时换成讲解，不额外增加 LLM 调用）
    eval_prompt = ""
    if str(jk.get("kind", "")) == "formed":
        eval_prompt = prompts.joseki_teach_prompt(str(jk["name"]), str(jk["key"]), str(jk["corner"]))
    else:
        if player_rank == 0:
            eval_prompt = "Estarth下了 %s，这是我算的最优手！胜率%s。轻描淡写夸一下，1-2句。" % (gtp, agent._pct(winrate_before))
        elif 0 < player_rank < 3:
            eval_prompt = "Estarth下了 %s，Top%d，胜率%s。肯定一下，1-2句。" % (gtp, player_rank + 1, agent._pct(winrate_before))
        elif player_rank >= 0:
            eval_prompt = "Estarth下了 %s，不在前5。委婉指出，给点鼓励，1-2句。" % gtp
        else:
            eval_prompt = "Estarth下了 %s，没进我算的前5。慵懒表示不太理解，1-2句。" % gtp
        eval_prompt += " 用你自己的口吻评，1句就好，别像解说员那样说「黑方/白棋」；先结合【当前棋面】确认这手落在哪、周围有没有子再说；不要提你自己接下来打算下哪里，不要复述胜率数值；开头和收尾都换一种新的说法，别复读你最近用过的口头禅。"
        # 防复读：上一手点评的原话拿出来明确避开
        prev_eval = ""
        for i in range(len(agent.st.memory_log) - 1, -1, -1):
            if str(agent.st.memory_log[i].get("tag", "")) == "E":
                prev_eval = str(agent.st.memory_log[i].get("text", ""))
                break
        if prev_eval != "":
            angles = ["这手的方向/意图", "厚薄与轻重", "形状好不好", "和附近棋子的呼应", "对以后势力的影响", "和刚才几手的联系"]
            eval_prompt += " 你上一手点评的原话是「%s」：这次不要复用里面出现过的形容词和句式，也别用同样的夸奖方式；从%s这个角度换个说法，还是一句话。" % (prev_eval, angles[random.randrange(len(angles))])
        # 定式偏离：在常规点评后轻轻提一句常规形状
        if str(jk.get("kind", "")) == "deviated":
            want = jk.get("expect", (-1, -1))
            if want[0] >= 0:
                eval_prompt += " " + prompts.joseki_hint_clause(str(jk["name"]), coords.xy_to_gtp(want[0], want[1]))
    data["messages"] = agent._phase_messages(
        "after_player",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             eval_prompt, "after_player", False, agent.st.last_katago, agent.st.board),
        len_note=agent._len_note(teach=str(jk.get("kind", "")) == "formed"),
    )
    data["jk"] = jk
    data["after_infos"] = after_infos
    data["after_black_wr"] = after_black_wr
    data["after_black_score"] = after_black_score


async def _prepare_own_turn(agent, data):
    """她的回合：上下文与提示词依赖落点，全在 decide 里建（此处无前置工作）。"""
    return


async def _prepare_just_moved(agent, data):
    """她落子后：引擎复算 → info → 落子后那句的提示词。"""
    player_eval = str(data.get("player_eval") or "")     # 本回合她对玩家那手的点评（after_player 阶段产出）
    think_msg = str(data.get("think_msg") or "")
    jk2 = data.get("jk2") or {}
    ai_gtp = str((data.get("move") or {}).get("gtp") or "")

    # ⑦ 先算「Azure 落子后局面」（轮到黑方）→ 供信息栏与下面「局势」的上下文使用
    post = await agent.katago.analyze(agent.st.moves)
    wr = float(data.get("after_black_wr", 0.5))
    sc = float(data.get("after_black_score", 0.0))
    if "error" in post:
        print("[KataGo] " + str(post["error"]))
    else:
        ri = post.get("rootInfo") or {}
        wr = float(ri.get("winrate", 0.5))
        sc = float(ri.get("scoreLead", 0.0))
        agent._update_katago_brief(post, len(agent.st.moves))
        # 这份「她落子后」的分析即下一手玩家落子前的局面：缓存复用
        agent.st.pre_analysis = {"n": len(agent.st.moves), "a": post}
    agent._emit(events.info(wr, sc))

    if str(jk2.get("kind", "")) == "formed":
        ai_eval_prompt = prompts.joseki_short_prompt(str(jk2["name"]), str(jk2["key"]), ai_gtp)
    elif ai_gtp == "pass":
        ai_eval_prompt = "你（Azure）这手选择虚着。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说你现在对局面的感觉、心里在琢磨什么；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (player_eval, think_msg)
    else:
        ai_eval_prompt = "你（Azure）刚下了 %s。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说：现在这盘棋我感觉怎么样、接下来我打算怎么走；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话，别用「黑方/白棋」这种旁观口吻。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (ai_gtp, player_eval, think_msg)
    data["messages"] = agent._phase_messages(
        "just_moved",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             ai_eval_prompt, "just_moved", False, agent.st.last_katago, agent.st.board,
                             data.get("lt")),
    )


async def _prepare_observe(agent, data):
    """提示：引擎分析 → 形势摘要 → 方向性提示词（引擎失败则短路）。"""
    n = len(agent.st.moves)
    analysis = await agent.katago.analyze(agent.st.moves)
    if "error" in analysis:
        print("[KataGo] " + str(analysis["error"]))
        agent._emit(events.message(events.AI, "嗯...我这边一时算不清，稍等再试吧~"))
        data["short"] = True
        return
    agent.st.pre_analysis = {"n": n, "a": analysis}
    tops = [str(m.get("move", "")) for m in (analysis.get("moveInfos", []) or [])[:3]]
    agent._update_katago_brief(analysis, n)
    prompt = "当前局面：%s。我算到的好点：%s。Estarth还没落子，结合【当前棋面】给个方向性提示，1-2句；不要直接说坐标，提到某片区域前先确认那里确实有子（没有的子别乱说）；换个说法开场，别重复你上一次提示的句式和口头禅。" % (
        prompts.board_summary(agent.st.move_seq), agent._py_list(tops))
    data["messages"] = agent._phase_messages(
        "observe",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "observe", False, agent.st.last_katago, agent.st.board),
    )


async def _prepare_analyze(agent, data):
    """局面分析：引擎分析 → 白方视角解读提示词（引擎失败则短路）。"""
    n = len(agent.st.moves)
    analysis = await agent.katago.analyze(agent.st.moves)
    if "error" in analysis:
        print("[KataGo] " + str(analysis["error"]))
        agent._emit(events.message(events.AI, "唔…我这边一时算不清，稍后再让我看看吧。"))
        data["short"] = True
        return
    conv = prompts.to_black_perspective(analysis, n)
    black_wr = float(conv[0])
    score_black = float(conv[1])
    azure_wr = 1.0 - black_wr
    tops = [str(m.get("move", "")) for m in (analysis.get("moveInfos", []) or [])[:5]]
    agent._update_katago_brief(analysis, n)
    lead = "你略占上风" if azure_wr > 0.5 else ("局面胶着" if azure_wr > 0.45 else "Estarth领先")
    prompt = "当前局面：%s，共%d手。你（Azure，执白）算出的判断：白方胜率约%s（%s），目差%s（正数是你领先）。你在考虑的几个点：%s。请用你自己的视角（我/你，别说「黑方/白棋」）分析当前局势3-4句：局面走向、关键处或薄弱处，可以点醒Estarth；语气慵懒但专业，先结合【当前棋面】核对再说，别提到盘上没有的子。" % (
        prompts.board_summary(agent.st.move_seq), n, agent._pct(azure_wr), lead, "%+.1f" % (-score_black), agent._py_list(tops))
    data["messages"] = agent._phase_messages(
        "analyze",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "analyze", False, agent.st.last_katago, agent.st.board),
    )
    data["info"] = {"wr": black_wr, "score": score_black}


async def _prepare_chat(agent, data):
    """聊天：长期记忆召回 → 上下文块 + 历史窗口 + 这一句。

    消息构造整体照旧裹在同一条 ``try`` 里（与旧 ``chat()`` 逐字一致）：任何意外都按「这次没说出话」
    处理，台词由 ``finalize`` 的兜底顶上，绝不让棋盘锁死。
    """
    text = str(data.get("text") or "")
    try:
        lt = await asyncio.to_thread(agent._long_term, text)     # 慢 embedding 不得冻住事件循环
        msgs = [
            agent._sys_msg(),
            agent._user_msg(prompts.context_block(agent.st.moves, agent.st.memory_log, agent.st.chat_history, "chat", True, agent.st.last_katago, agent.st.board,
                                                  long_term=lt)),
        ]
        hist = agent.st.chat_history
        upto = len(hist) - 1            # 最后一条是刚追加的这句，下面单独发，避免重复
        for sp in prompts.chat_windows(hist[:upto]):
            for i in range(int(sp[0]), int(sp[1]) + 1):
                msgs.append(hist[i])
        msgs.append({"role": "user", "content": text})
        data["messages"] = msgs
    except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
        print("[Agent] 聊天异常：%s" % e)
        data["messages"] = []


async def _prepare_idle(agent, data):
    """闲置搭话：情绪结算 + 一句主动开口的提示词。"""
    agent._mood_event("idle_bored")
    prompt = "Estarth 已经有一段时间没有动静了，你有点好奇他在做什么。主动对他说一句很短的话（20字以内）：问问他还在不在、在忙什么，或要不要继续下棋、聊聊天。慵懒、亲切，1句。"
    data["messages"] = [
        agent._sys_msg(),
        agent._user_msg(prompts.context_block(agent.st.moves, agent.st.memory_log, agent.st.chat_history, "chat", True, agent.st.last_katago, agent.st.board)),
        {"role": "user", "content": prompt},
    ]


async def _prepare_summary(agent, data):
    """对局总结：终局引擎形势 → 终局情绪 → 总结提示词。"""
    note = str(data.get("note") or "")
    n = len(agent.st.moves)
    final_wr = 0.5
    final_score = 0.0
    analysis = await agent.katago.analyze(agent.st.moves)
    if "error" in analysis:
        print("[KataGo] " + str(analysis["error"]))
    else:
        conv = prompts.to_black_perspective(analysis, n)
        final_wr = float(conv[0])
        final_score = float(conv[1])
    agent._update_katago_brief(analysis, n)
    # 终局情绪：她赢下（黑方胜率 < 0.5）平和地开心，输了则有点低落
    agent._mood_event("summary_win" if final_wr < 0.5 else "summary_lose")
    seq = " → ".join(str(s) for s in agent.st.move_seq)
    prompt = "对局结束，共%d手。\n序列：%s\n最终黑胜率：%s，目差：%s\nEstarth备注：%s\n\n写总结：1.对局回顾 2.给Estarth的改进建议 2-3条 3.你的心情\n用Azure口吻，150-250字，慵懒真诚。" % (
        n, seq, agent._pct(final_wr), "%+.1f" % final_score, note)
    data["messages"] = agent._phase_messages(
        "summary",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "summary", False, agent.st.last_katago, agent.st.board),
    )
    data["wr"] = final_wr
    data["score"] = final_score


_PREPARE = {
    PHASE_AFTER_PLAYER: _prepare_after_player,
    PHASE_OWN_TURN: _prepare_own_turn,
    PHASE_JUST_MOVED: _prepare_just_moved,
    PHASE_OBSERVE: _prepare_observe,
    PHASE_ANALYZE: _prepare_analyze,
    PHASE_CHAT: _prepare_chat,
    PHASE_IDLE: _prepare_idle,
    PHASE_SUMMARY: _prepare_summary,
}


def _prepare_node(agent):
    """① prepare：建上下文与提示词（含阶段所需的引擎调用）。"""

    async def prepare(state: TurnState) -> TurnState:
        phase = str(state.get("phase") or "")
        data = dict(state.get("data") or {})
        fn = _PREPARE.get(phase)
        if fn is not None:
            await fn(agent, data)
        return {
            "phase": phase,
            "moves": list(state.get("moves") or agent.st.moves),
            "messages": list(data.get("messages") or []),
            "reply": "",
            "tool_calls": int(state.get("tool_calls") or 0),
            "move": state.get("move"),
            "turn_id": str(state.get("turn_id") or uuid.uuid4().hex),
            "data": data,
        }

    return prepare


# ================= ② decide：取应手与工具 =================

async def _decide_own_turn(agent, data):
    """取应手（落点仍由 ``agent._pick_ai_move`` 决定）并建「用意」提示词。

    ``_pick_ai_move`` 是纯读操作；召回走 ``to_thread``（慢 embedding 不得冻住事件循环），
    与旧 ``_ai_turn`` 的先后顺序逐字一致。「用意」提示词依赖落点，所以只能在这里建。
    """
    pick = agent._pick_ai_move(list(data.get("after_infos") or []))
    ai_gtp, ai_x, ai_y = pick[0], pick[1], pick[2]
    data["move"] = {"x": ai_x, "y": ai_y, "gtp": ai_gtp, "pass": ai_gtp == "pass"}

    last_gtp = str(agent.st.moves[-1][1]) if agent.st.moves else "（空盘）"
    lt = await asyncio.to_thread(agent._long_term, "最近一手 %s，阶段 own_turn" % last_gtp)
    data["lt"] = lt

    think_hint = "你准备虚着（pass）" if ai_gtp == "pass" else "你准备下在 %s" % ai_gtp
    think_prompt = "轮到你下了。%s。用你自己的口吻（我/你）一句话说出这一手的用意：打算下在哪、想在当前盘面上达成什么、跟最近这几手有什么呼应，直接说。注意：你刚点评过Estarth那一手，这里不要再评价它、不要改口说相反的话，也别复述点评；不要用「黑方/白棋」的旁观口吻。换一个全新的说法，别总用「嗯~」「唔」开头，句式也别和刚才那条点评一样。" % think_hint
    await asyncio.sleep(THINK_BEAT_SEC)         # 落子宣告前的犹豫节拍（位置与旧 _ai_turn 一致）
    data["messages"] = agent._phase_messages(
        "own_turn",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             think_prompt, "own_turn", False, agent.st.last_katago, agent.st.board, lt),
    )


def _decide_node(agent):
    """② decide：取应手与工具（落点仍由 ``agent._pick_ai_move`` 决定）。"""

    async def decide(state: TurnState) -> TurnState:
        phase = str(state.get("phase") or "")
        data = dict(state.get("data") or {})
        if phase == PHASE_OWN_TURN and not data.get("short"):
            await _decide_own_turn(agent, data)
        return {
            "messages": list(data.get("messages") or []),
            "tool_calls": int(data.get("tool_calls") or state.get("tool_calls") or 0),
            "move": data.get("move") or state.get("move"),
            "data": data,
        }

    return decide


# ================= ③ speak：出话 =================

async def _speak_after_player(agent, data):
    """玩家那手的点评（定式成型时换成讲解，不额外增加 LLM 调用）。"""
    jk = data.get("jk") or {}
    player_eval = await agent._reply(list(data.get("messages") or []), 300)
    if str(jk.get("kind", "")) == "formed" and agent._llm_failed(player_eval):
        player_eval = prompts.joseki_fallback(str(jk["name"]), str(jk["key"]))
    elif agent._llm_failed(player_eval):        # 云端失败：点评退化为兜底台词
        player_eval = agent._fallback_line("game")
    data["reply"] = player_eval
    data["player_eval"] = player_eval           # 供 own_turn / just_moved 阶段的提示词引用（同一句）


async def _speak_own_turn(agent, data):
    """她的落子宣告：先发带坐标的 ``ai_move``，再把这一手落到内部棋谱。"""
    msgs = list(data.get("messages") or [])
    if agent.tools_enabled:                     # 工具只在这一处与提示启用；落点照旧由 _pick_ai_move 决定
        think_msg = await agent._tool_loop(msgs, 80, "own_turn")
    else:
        think_msg = await agent._reply(msgs, 80)
    if agent._llm_failed(think_msg):            # 云端失败：落子宣告退化为兜底台词（坐标照常揭示）
        think_msg = agent._fallback_line("game")
    data["think_msg"] = think_msg

    mv = dict(data.get("move") or {})
    ai_gtp = str(mv.get("gtp") or "")
    ai_x = int(mv.get("x", -1))
    ai_y = int(mv.get("y", -1))
    # 这条语音开始播放时，界面才把她的子放上棋盘（事件带坐标，客户端本地揭示）
    agent._emit(events.message(events.AI_MOVE, think_msg, move={
        "x": ai_x, "y": ai_y, "gtp": ai_gtp, "pass": ai_gtp == "pass",
    }))

    # 真正落子：先落到内部棋谱；棋盘可见的更新延后到语音开始时（reveal_move）
    if ai_gtp == "pass":
        agent.st.board.pass_move()
        agent.st.add_move("W", "pass")
    else:
        agent.st.board.play(ai_x, ai_y, "white")
        agent.st.add_move("W", ai_gtp)
    agent._reveal_pending = True
    if ai_gtp == "pass":
        agent._emit(events.message(events.AI, "（Azure本手选择了虚着）"))


async def _speak_just_moved(agent, data):
    """她落子后那句感想。"""
    jk2 = data.get("jk2") or {}
    ai_eval = await agent._reply(list(data.get("messages") or []), 120)
    if str(jk2.get("kind", "")) == "formed" and agent._llm_failed(ai_eval):
        ai_eval = prompts.joseki_fallback(str(jk2["name"]), str(jk2["key"]))
    elif agent._llm_failed(ai_eval):            # 云端失败：落子后那句退化为兜底台词
        ai_eval = agent._fallback_line("game")
    data["reply"] = ai_eval


async def _speak_observe(agent, data):
    """提示台词（工具开着时先查资料再落回一句自然语言）。"""
    if data.get("short"):
        return
    msgs = list(data.get("messages") or [])
    if agent.tools_enabled:                     # 工具只在这一处与落子宣告启用（短回合不背工具往返）
        msg = await agent._tool_loop(msgs, 300, "observe")
    else:
        msg = await agent._reply(msgs, 300)
    if agent._llm_failed(msg):                  # 云端失败：提示退化为兜底台词
        msg = agent._fallback_line("game")
    data["reply"] = msg


async def _speak_analyze(agent, data):
    """分析台词。"""
    if data.get("short"):
        return
    msg = await agent._reply(list(data.get("messages") or []), 350)
    if agent._llm_failed(msg):                  # 云端失败：分析退化为兜底台词（形势数据照常下发）
        msg = agent._fallback_line("game")
    data["reply"] = msg


async def _speak_chat(agent, data):
    """聊天台词（消息构造失败时按空回复处理，由 finalize 的兜底顶上）。"""
    msgs = list(data.get("messages") or [])
    data["reply"] = await agent._reply(msgs, 400) if msgs else ""


async def _speak_idle(agent, data):
    """闲置搭话台词。"""
    reply = await agent._reply(list(data.get("messages") or []), 90)
    if agent._llm_failed(reply):
        reply = agent._idle_line()
    data["reply"] = reply


async def _speak_summary(agent, data):
    """总结台词。"""
    text = await agent._reply(list(data.get("messages") or []), 600)
    failed = agent._llm_failed(text)
    if failed:                                  # 云端失败：总结退化为兜底台词
        text = agent._fallback_line("chat")
    data["reply"] = text
    data["failed"] = bool(failed)


_SPEAK = {
    PHASE_AFTER_PLAYER: _speak_after_player,
    PHASE_OWN_TURN: _speak_own_turn,
    PHASE_JUST_MOVED: _speak_just_moved,
    PHASE_OBSERVE: _speak_observe,
    PHASE_ANALYZE: _speak_analyze,
    PHASE_CHAT: _speak_chat,
    PHASE_IDLE: _speak_idle,
    PHASE_SUMMARY: _speak_summary,
}


def _speak_node(agent):
    """③ speak：出话（点评 / 落子宣告 / 聊天回复都走这里）。"""

    async def speak(state: TurnState) -> TurnState:
        phase = str(state.get("phase") or "")
        data = dict(state.get("data") or {})
        fn = _SPEAK.get(phase)
        if fn is not None:
            await fn(agent, data)
        return {"reply": str(data.get("reply") or ""), "data": data}

    return speak


# ================= ④ finalize：发事件、写 memory_log、归位 busy =================

async def _finalize_after_player(agent, data):
    """点评入档并发给玩家（tag ``E``）。**不归位 busy**：本回合还有她自己的落子。"""
    text = str(data.get("reply") or "")
    agent.st.log_memory("E", text, len(agent.st.moves))
    agent._emit(events.message(events.AI_COMMENT, text))


async def _finalize_own_turn(agent, data):
    """落子完成后立刻做定式识别（纯本地计算），供下一阶段的上下文使用。**不归位 busy**。"""
    data["jk2"] = agent._joseki_check()


async def _finalize_just_moved(agent, data):
    """落子后感想入档（tag ``A``）、发事件、定式轨迹，然后归位 busy 与情绪小调用。"""
    text = str(data.get("reply") or "")
    agent.st.log_memory("A", text, len(agent.st.moves))
    agent._emit(events.message(events.AI_COMMENT, text))
    agent._joseki_emit(data.get("jk2") or {})
    agent._set_busy(False)
    await agent._mood_llm_refine()
    # 不在这里 emit state：落子的可见更新由 reveal_move() 在她这段语音开始时触发


async def _finalize_observe(agent, data):
    """提示发事件（短路时只归位 busy），然后解锁。"""
    if not data.get("short"):
        agent._emit(events.message(events.AI_COMMENT, str(data.get("reply") or "")))
    agent._set_busy(False)


async def _finalize_analyze(agent, data):
    """分析发事件 + 形势栏（短路时只归位 busy），然后解锁。"""
    if not data.get("short"):
        agent._emit(events.message(events.AI, "📊 " + str(data.get("reply") or "")))
        info = data.get("info") or {}
        agent._emit(events.info(float(info.get("wr", 0.5)), float(info.get("score", 0.0))))
    agent._set_busy(False)


async def _finalize_chat(agent, data):
    """聊天收尾：**先归位 busy**（与今天逐字一致：思考完就解锁），再追加历史、发话、情绪小调用。"""
    agent._set_busy(False)
    reply = str(data.get("reply") or "")
    if agent._llm_failed(reply):                # 云端失败：聊天退化为兜底台词
        reply = agent._fallback_line("chat")
    agent.st.chat_history.append({"role": "assistant", "content": reply})
    agent._emit(events.message(events.AI, reply))
    await agent._mood_llm_refine()


async def _finalize_idle(agent, data):
    """闲置搭话收尾（不进 busy，与今天一致）。"""
    reply = str(data.get("reply") or "")
    agent.st.chat_history.append({"role": "assistant", "content": reply})
    agent._emit(events.message(events.AI_IDLE, reply))


async def _finalize_summary(agent, data):
    """总结收尾：置终局标记 → 发话 → 真总结才写长期记忆 → 解锁。"""
    text = str(data.get("reply") or "")
    agent.st.finished = True
    agent._emit(events.message(events.AI, "📋 对局总结\n" + text))
    if not data.get("failed"):                  # 只写真总结：兜底台词不是总结，写进去会污染记忆
        await asyncio.to_thread(agent._remember_game, text,
                                float(data.get("wr", 0.5)), float(data.get("score", 0.0)))
    agent._set_busy(False)


_FINALIZE = {
    PHASE_AFTER_PLAYER: _finalize_after_player,
    PHASE_OWN_TURN: _finalize_own_turn,
    PHASE_JUST_MOVED: _finalize_just_moved,
    PHASE_OBSERVE: _finalize_observe,
    PHASE_ANALYZE: _finalize_analyze,
    PHASE_CHAT: _finalize_chat,
    PHASE_IDLE: _finalize_idle,
    PHASE_SUMMARY: _finalize_summary,
}


def _finalize_node(agent):
    """④ finalize：收尾（发事件 / 写 ``st.memory_log`` / 归位 ``busy``）。"""

    async def finalize(state: TurnState) -> TurnState:
        phase = str(state.get("phase") or "")
        data = dict(state.get("data") or {})
        fn = _FINALIZE.get(phase)
        if fn is not None:
            await fn(agent, data)
        return {"data": data}

    return finalize


def build_graph(agent):
    """按 ``agent`` 装配回合状态图：``prepare → decide → speak → finalize → END``（无分支）。"""
    g = StateGraph(TurnState)
    g.add_node("prepare", _prepare_node(agent))
    g.add_node("decide", _decide_node(agent))
    g.add_node("speak", _speak_node(agent))
    g.add_node("finalize", _finalize_node(agent))
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "decide")
    g.add_edge("decide", "speak")
    g.add_edge("speak", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
