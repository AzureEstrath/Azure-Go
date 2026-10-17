"""回合编排状态图（LangGraph）。

计划 04 的目标是把 ``AzureAgent`` 里「写死的函数调用顺序」显式成一张状态图。本文件是它的
**唯一编排实现**：六个需要 LLM 编排顺序的公开方法（``player_move`` / ``hint`` /
``analyze_position`` / ``chat`` / ``idle_remark`` / ``summarize``）在 ``agent.py`` 里只做
「构造初始状态 → 跑图」；引擎调用、提示词、台词、事件与 ``memory_log`` 标签的顺序全在这里
的四个节点里。其余公开方法（``undo`` / ``reset_game`` / ``set_allow_pass`` / ``reveal_move``
/ ``tick_mood`` / ``player_interaction``）不需要 LLM 编排，仍是同步的「直接发事件」。

拓扑：``prepare → decide → (tools → decide)* → speak → finalize → END``。``decide`` 之后是
**条件边**（``route_after_decide``）：模型在预算内请求了工具就先跑 ``tools`` 节点、把结果回到
``decide`` 再来一轮，直到没有工具请求或预算用尽；``tools → decide`` 与其余三条边都是无条件的。
**一次 ``graph.ainvoke`` 处理一个阶段**（``state["phase"]``）：``player_move`` 是多阶段
（``after_player → own_turn → just_moved``），公开方法依次跑三次、中间数据经 ``state["data"]``
透传；其余公开方法各跑一次。这样既保住了 ``busy`` 的多阶段序列（S12），也让每个节点的语义
保持单一。

约定（后续任务必须守住）：

- ``TurnState`` 声明的键是本图与 ``AzureAgent`` 之间的**唯一接口**：Task 1 的 7 个原名段一个都
  不能少、不许改名，本任务只新增 ``wants_tools``（条件边判据）与 ``data``（阶段之间的中间数据）。
  **单次 ``ainvoke`` 内的唯一总线是 ``state``**——模型消息列表是 ``state["messages"]``、工具计数是
  ``state["tool_calls"]``、待执行的工具请求与条件边判据是 ``state["data"]["pending_tool_calls"]`` 与
  ``state["wants_tools"]``、落点是 ``state["move"]``；``data`` 只承载**阶段之间**的临时数据
  （引擎分析 / 定式结果 / 提示词装配产物 / 跨阶段的 ``move`` 与 ``think_msg``），因为
  ``player_move`` 的三次 ``ainvoke`` 各自是无状态的一趟图，跨阶段只能靠 ``data``。``messages``
  用**默认覆盖 reducer + 节点内手动拼接**（不用 ``operator.add``：``decide`` 每轮都返回完整列表，
  叠加 reducer 会把消息越滚越多）；``message`` 的 kind 与 ``memory_log`` 的 tag 一律由 ``phase``
  推导，不另设字段（S11）。
- ``busy`` 归位由**调用方**（``AzureAgent`` 的公开方法）用 ``try/finally`` 兜住——那是异常路径的
  安全网；节点也在与今天**逐字相同的位置**调用 ``_set_busy(False)``（``chat`` 是「思考完就解锁、
  再追加历史与发话」，``just_moved`` / ``observe`` / ``analyze`` / ``summary`` 是发完事件之后），
  ``_set_busy`` 自带去重，多调一次不会产生多余的 ``busy`` 事件。
- 台词一律经 ``agent.clean_reply()``（``_reply()`` 内部必经），推理段永不进台词；
- speak 阶段还有一条**可选**（默认关闭，``agent.stream_tokens``）的流式通道：``_streamed_text``
  逐块推 ``token`` 事件（``ThinkStream`` 在累加文本上**跨块**剥离 think 段：成对 ``<think>…</think>``
  与 ``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`` 区间保证不漏；「只剩收尾标记」的降级形态只保证
  最终台词干净，边界见该类的「保证边界」；去重闸门同样生效、整句复读一条 token 都不发），
  台词仍以 ``_finalize_*`` 里经 ``clean_reply`` + ``_emit_deduped`` 的完整 ``message`` 收口，
  且与「对整条原文调 ``content_of``」同构；关闭时一个 ``token`` 都不发，事件序列与没有这个功能时逐字一致；
- 语音（计划 05）走 ``_emit_line``：先 ``message``、**紧随其后** ``await agent.tts.speak(text)`` 再发
  ``speech``（``turn_id`` + 同一回合内从 1 递增的 ``seq``，由 ``agent._new_turn()`` 重置；``pitch`` 取
  当前情绪区域）。三条不能回退的规则：``agent.tts is None``（未接线）时一条 ``speech`` 都不发、
  行为与没有这个功能时逐字一致；``speech`` 与对应的 ``message`` **共用 ``_emit_deduped`` 这一个闸门**
  （被去重抑制的台词连同它的 ``speech`` 一起不发）；``mode == "none"`` 不发。``ai_move`` 落子宣告
  不走判重（直发），它的 ``speech`` 照样紧跟其后——前端仍在「这条语音开始播放」时 ``POST /api/reveal``；
- ``build_graph(agent)`` 用闭包捕获 ``agent``，模块级不持有任何可变状态：同一个 ``agent`` 重建图
  是安全的，不同 ``agent`` 各有一张图。
"""

import asyncio
import random
import uuid
from typing import TypedDict

from langchain_core.messages import ToolMessage
from langgraph.graph import END, START, StateGraph

from . import coords, events, prompts
from .llm_chain import ThinkStream, content_of, content_text_of
from .tools import TOOLS

# 节点名（顺序即拓扑顺序）：prepare → decide → speak → finalize
# 注意：``tools`` 是条件边上的**支线节点**，不在 ``NODE_NAMES`` 里（该常量锁的是主链路，Task 1 的
# 断言一字不动）。
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

# 走工具轮的阶段（与计划 02 的两处启用点一致：own_turn 的落子宣告、observe 的提示）
TOOL_PHASES = (PHASE_OWN_TURN, PHASE_OBSERVE)

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
    messages: list          # 发给模型的消息（系统 + 上下文 + 历史窗口 + 工具轮的 AI/Tool 消息）
    reply: str              # 本阶段已说出口的台词（必经 clean_reply）
    tool_calls: int         # 已执行的工具调用次数（上限 agent.max_tool_calls）
    wants_tools: bool       # 本轮 decide 里模型是否请求了**待执行**的工具（条件边判据）
    move: dict | None       # 落点 {x, y, gtp, pass}；None = 本阶段不落子
    turn_id: str            # 本轮唯一标识（流式 token 与重复发话的判据都靠它）
    data: dict              # 阶段之间的中间数据（引擎分析 / 提示词 / 定式结果 / short / 落点）


# ================= 走错判定（纯函数，供 after_player 用） =================

def is_joseki_mistake(jk: dict, player_on_line: bool, drop: float, rank_ok: bool) -> bool:
    """「走错」＝**真亏**且**不在库线上**（纯函数，无副作用、不调引擎）。

    - ``player_on_line=True``（这手正好是库里那一手）⇒ **恒为 False**：库里线条本身略亏时
      会出现「照书下还被骂」，这是最难看的误判，必须在进入任何教学分支之前就拦掉；
    - ``jk["kind"] != "deviated"``（没偏离库线，或识别器压根没出声）⇒ ``False``；
    - 其余与**既有俗手判定同一口径**：不在她算的前 5（``not rank_ok``）或胜率掉 > 10%。
    """
    if player_on_line:
        return False
    if str((jk or {}).get("kind", "")) != "deviated":
        return False
    return (not rank_ok) or (drop > 0.10)


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

    # ④‴ 走错判定：真亏 **且** 偏离库线才算「走错」；库线那一手永不判错（见 is_joseki_mistake）。
    #     只把结论放进本轮 data，不发事件、不改台词（卡片与事件由 T5/T6/T7 消费）。
    #     （T5：本块整体上移到 `_joseki_check()` 之后、发 joseki 事件之前——卡片门禁要先拿到它；
    #      块内容一字未改，也不发任何事件，故事件顺序仍是 state → joseki → mood。）
    #
    #     「库线那一手」取两款（设计 §6 D「库里线条本身略亏时不许出现『照书下还被骂』」）：
    #       ① 显示线的第 k 手（`line[k]`，0 基）——识别器给偏离用的那个「正解」点；
    #       ② 任一**候选线**在本格的下一手（`candidates[i]["next"]`）——玩家照的是分岔处的另一本。
    #     ⚠ 在现在的 `scan()` 语义下这一款**结构性不触发**：能被判成 `deviated` 的前提正是「玩家这手
    #     与共享前缀的下一手都不吻合」（吻合了的话那条线的最长前缀早就前进到 k+1，`len(seq) == k+1`
    #     这个偏离条件也就不成立，识别器会改判成中段 / 进入 / 分岔）。换句话说，「照书下不被骂」今天
    #     由识别器的判定严格性兜着；这条护栏是按设计先立在**编排侧**，扩库（T11a）之后继续成立。
    player_on_line = False
    if str(jk.get("kind", "")) == "deviated":
        line = jk.get("line") or []
        k = int(jk.get("k", 0))
        if len(line) > k:                               # line[k]（0 基）＝库里那一手
            p = line[k]
            player_on_line = int(p["x"]) == x and int(p["y"]) == y
        if not player_on_line:                          # 也可能是照着**另一条**库线走的（分岔那一手）
            for c in jk.get("candidates") or []:
                nxt = c.get("next")
                if (isinstance(nxt, (tuple, list)) and len(nxt) >= 2
                        and int(nxt[0]) == x and int(nxt[1]) == y):
                    player_on_line = True
                    break
    rank_ok = player_rank >= 0                          # 是否在她算的前 5（与俗手判定同一判据）
    data["jk_mistake"] = {
        "is_mistake": is_joseki_mistake(jk, player_on_line, drop, rank_ok),
        "on_line": player_on_line,
        "gtp": gtp,
        "x": x,
        "y": y,
        "drop": drop,
        "rank_ok": rank_ok,
        # T6：玩家这一手的 1 基序号。**必须在这里记下来**——`just_moved` 那个 `_joseki_emit`
        # 站点是在她自己落子之后才 emit 的，那里 `len(st.moves)` 已经前进，事件里的
        # `mistake.move_no` 只能用这里记下的值（取在 `add_move` 之后，正是玩家这手）。
        "move_no": len(agent.st.moves),
    }

    # ④‴′ 卡片门禁（T5）：前 3 次都上卡片，第 4 次起只在真的走错时才上。**先问后记**：
    #      ``seen < 3`` 必须在自增之前读（否则第一次出现时 seen 已是 1，前 3 次就只成立 2 次）。
    #      结论只落进本轮 data 交给 T6/T7 消费：本任务不新增/不删除任何事件，_joseki_emit 照旧。
    if jk:
        data["jk_show_card"] = await agent.joseki_show_card(jk, data.get("jk_mistake"))
        await agent.joseki_count_entry(jk)
        agent._joseki_emit(jk, data.get("jk_show_card", False), data.get("jk_mistake"))

    # ④′ 情绪反应：按这一手的好坏结算三维情绪（即时反馈不等 LLM）
    ev = "player_ok"
    if str(jk.get("kind", "")) == "formed" or player_rank == 0:
        ev = "player_good"
    elif player_rank < 0 or drop > 0.10:
        ev = "player_bad"
    elif player_rank >= 3:
        ev = "player_off"
    agent._mood_event(ev)

    # ⑤ Azure 点评这一手（T7：结构点按 `kind` **顶替**这一手的普通点评，不额外增加 LLM 调用）
    #    分流**只看 `kind`**（+ 走错时看 `is_mistake`）：进入 / 分岔 / 收尾必说。`show_card` 是**卡片**
    #    的次数规则（T5/T6/T8），管的是卡片字段、不管说不说——讲解顶替的是本来那条点评，不多气泡。
    eval_prompt = ""
    kind = str(jk.get("kind", ""))
    mistake = data.get("jk_mistake") or {}
    if kind == "entered":
        eval_prompt = prompts.joseki_enter_prompt(
            str(jk.get("name") or ""), str(jk.get("key") or ""), str(jk.get("corner") or ""))
    elif kind == "branch" or (kind == "deviated" and mistake.get("is_mistake") is True):
        # 「走错」＝真亏又出线：这里正是「有别的走法」的场合，与分岔同一套
        eval_prompt = prompts.joseki_branch_prompt(
            str(jk.get("name") or ""), str(jk.get("key") or ""),
            list(jk.get("candidates") or []))
    elif kind == "formed":
        eval_prompt = prompts.joseki_close_prompt(
            str(jk.get("name") or ""), str(jk.get("key") or ""), str(jk.get("corner") or ""),
            "player")
    if eval_prompt == "":
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
    data["jk"] = jk
    data["after_infos"] = after_infos
    data["after_black_wr"] = after_black_wr
    data["after_black_score"] = after_black_score
    return agent._phase_messages(
        "after_player",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             eval_prompt, "after_player", False, agent.st.last_katago, agent.st.board),
        len_note=agent._len_note(teach=str(jk.get("kind", "")) == "formed"),
    )


async def _prepare_own_turn(agent, data):
    """她的回合：上下文与提示词依赖落点，全在 decide 里建（此处无前置工作）。"""
    return


async def _joseki_own_move(agent, data):
    """她那一步的定式识别 + 入围补齐（**只调一次** ``agent._joseki_check()``）。

    结论落在 ``data["jk2"]`` 与 ``data["jk_show_card"]``：识别器给的是她**自己那一手**的结果
    （纯本地计算），入围要 ``await``（``joseki_show_card`` / ``joseki_count_entry``），所以只能在
    async 的节点里做。两个调用点：

    ① ``finalize`` 节点的 ``own_turn``（正常回合，紧接在 ``just_moved`` 的 prepare 之前，结论正好
       赶上给她那句感想挑模板）；
    ② ``_prepare_just_moved`` 的表头（兜底：本回合没跑过 ``own_turn`` 就直接驱动 ``just_moved``）。

    ``data`` 里已有 ``jk2`` 就立刻返回——``_joseki_check()`` 会消耗去重键，**绝不能调第二次**
    （同角同族第二次必然返回 ``{}``）。``joseki_count_entry`` 同族同局只计一次，重复调用是 no-op。
    """
    if "jk2" in data:
        return
    jk2 = agent._joseki_check()
    data["jk2"] = jk2
    if "jk_show_card" not in data:               # 一个回合只有一个入围结论：after_player 定过就不改
        data["jk_show_card"] = await agent.joseki_show_card(jk2, data.get("jk_mistake"))
    await agent.joseki_count_entry(jk2)


async def _prepare_just_moved(agent, data):
    """她落子后：引擎复算 → info → 落子后那句的提示词。"""
    await _joseki_own_move(agent, data)          # 她那一手的定式结论（没跑过 own_turn 时在这里补上）
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
        # T7（U36）：她走完定式时，「收尾」并进**她的**发言位（这条落子后的感想）。
        #   同样只看 `kind`：`show_card` 只管卡片，不管说不说（U34 修订版）。
        ai_eval_prompt = prompts.joseki_close_prompt(
            str(jk2.get("name") or ""), str(jk2.get("key") or ""),
            str(jk2.get("corner") or ""), "azure")
    elif ai_gtp == "pass":
        ai_eval_prompt = "你（Azure）这手选择虚着。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说你现在对局面的感觉、心里在琢磨什么；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (player_eval, think_msg)
    else:
        ai_eval_prompt = "你（Azure）刚下了 %s。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说：现在这盘棋我感觉怎么样、接下来我打算怎么走；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话，别用「黑方/白棋」这种旁观口吻。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (ai_gtp, player_eval, think_msg)
    return agent._phase_messages(
        "just_moved",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             ai_eval_prompt, "just_moved", False, agent.st.last_katago, agent.st.board,
                             data.get("lt")),
    )


async def _prepare_observe(agent, data):
    """提示：引擎分析 → 形势摘要 → 方向性提示词（引擎失败则短路）。

    短路提示是**玩家可见台词**：走 ``_emit_line``（T20①）——它紧跟着一条 ``speech``，
    与正常出话那条路同一个发射口（否则 Task 4 之后这行会「只显示、不发声」）。
    """
    n = len(agent.st.moves)
    analysis = await agent.katago.analyze(agent.st.moves)
    if "error" in analysis:
        print("[KataGo] " + str(analysis["error"]))
        await _emit_line(agent, events.AI, "嗯...我这边一时算不清，稍等再试吧~")
        data["short"] = True
        return
    agent.st.pre_analysis = {"n": n, "a": analysis}
    tops = [str(m.get("move", "")) for m in (analysis.get("moveInfos", []) or [])[:3]]
    agent._update_katago_brief(analysis, n)
    prompt = "当前局面：%s。我算到的好点：%s。Estarth还没落子，结合【当前棋面】给个方向性提示，1-2句；不要直接说坐标，提到某片区域前先确认那里确实有子（没有的子别乱说）；换个说法开场，别重复你上一次提示的句式和口头禅。" % (
        prompts.board_summary(agent.st.move_seq), agent._py_list(tops))
    return agent._phase_messages(
        "observe",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "observe", False, agent.st.last_katago, agent.st.board),
    )


async def _prepare_analyze(agent, data):
    """局面分析：引擎分析 → 白方视角解读提示词（引擎失败则短路；短路提示走 ``_emit_line``，T20①）。"""
    n = len(agent.st.moves)
    analysis = await agent.katago.analyze(agent.st.moves)
    if "error" in analysis:
        print("[KataGo] " + str(analysis["error"]))
        await _emit_line(agent, events.AI, "唔…我这边一时算不清，稍后再让我看看吧。")
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
    data["info"] = {"wr": black_wr, "score": score_black}
    return agent._phase_messages(
        "analyze",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "analyze", False, agent.st.last_katago, agent.st.board),
    )


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
        return msgs
    except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
        print("[Agent] 聊天异常：%s" % e)
        return []


async def _prepare_idle(agent, data):
    """闲置搭话：情绪结算 + 一句主动开口的提示词。"""
    agent._mood_event("idle_bored")
    prompt = "Estarth 已经有一段时间没有动静了，你有点好奇他在做什么。主动对他说一句很短的话（20字以内）：问问他还在不在、在忙什么，或要不要继续下棋、聊聊天。慵懒、亲切，1句。"
    return [
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
    data["wr"] = final_wr
    data["score"] = final_score
    return agent._phase_messages(
        "summary",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             prompt, "summary", False, agent.st.last_katago, agent.st.board),
    )


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
        msgs = []
        fn = _PREPARE.get(phase)
        if fn is not None:
            msgs = list(await fn(agent, data) or [])
        return {
            "phase": phase,
            "moves": list(state.get("moves") or agent.st.moves),
            "messages": msgs,
            "reply": "",
            "tool_calls": int(state.get("tool_calls") or 0),
            "wants_tools": False,
            "move": state.get("move"),
            "turn_id": str(state.get("turn_id") or uuid.uuid4().hex),
            "data": data,
        }

    return prepare


# ================= ② decide：取应手与工具 =================

async def _decide_own_turn(agent, data):
    """取应手（默认策略仍是 ``agent._pick_ai_move``）并建「用意」提示词，返回这条提示的消息。

    ``_pick_ai_move`` 是纯读操作；召回走 ``to_thread``（慢 embedding 不得冻住事件循环），
    与旧 ``_ai_turn`` 的先后顺序逐字一致。「用意」提示词依赖落点，所以只能在这里建。
    工具开着时，模型可以在工具轮里报出自己选的落点（``_resolve_ai_move`` 定案，见 S21）。
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
    return agent._phase_messages(
        "own_turn",
        prompts.with_context(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                             think_prompt, "own_turn", False, agent.st.last_katago, agent.st.board, lt),
    )


def route_after_decide(state, max_calls=2):
    """``decide`` 之后的条件边：还有待执行的工具请求且在预算内 → ``tools``，否则 → ``speak``。

    一律 ``.get`` 读键、对**普通 dict** 也成立（计划给定的用例直接传
    ``{"tool_calls": 0, "wants_tools": False}``，不能假设键一定在）。
    """
    if bool(state.get("wants_tools")):
        if int(state.get("tool_calls") or 0) < int(max_calls):
            return "tools"
    return "speak"


def _tool_round_ready(agent) -> bool:
    """工具轮的前置条件（与 ``agent._tool_loop`` 的三条前置逐条一致）：链可用、地址已配、预算 > 0。

    任一条不满足就**不进工具轮**，整段编排回到 Task 2 的路径（``speak`` 里走 ``_tool_loop`` →
    它自己也会退回 ``_reply``），保证「工具开着但不可用」与不开工具完全同一条路。
    """
    return agent.chain is not None and agent._chain_ok() and agent.max_tool_calls > 0


async def _decide_tool_round(agent, phase, data, messages, tool_calls):
    """跑**一轮**工具感知的模型调用，返回 ``(messages, tool_calls, wants_tools)``。

    与 ``agent._tool_loop`` 的每一轮同构：模型要工具且还有预算 → 把这条 AI 消息并入消息、把待
    执行调用记在 ``data["pending_tool_calls"]``，交给 ``tools`` 节点（条件边）；否则这一轮的
    ``content`` 就是终点，写进 ``data["tool_final"]``。异常一律吞成「这轮没答上来」（``tool_final``
    置空串，由调用方的兜底台词顶上），绝不抛出。
    """
    max_tokens = 80 if phase == PHASE_OWN_TURN else 300
    try:
        bound, kwargs = agent._bind_tools(list(TOOLS), max_tokens)
        ai = await agent._chain_invoke(bound, messages, kwargs)
    except Exception as e:                          # noqa: BLE001 —— 工具轮绝不抛异常
        print("[Agent] 工具轮调用异常（%s）：%s" % (phase, e))
        data["tool_final"] = ""
        return messages, tool_calls, False
    calls = list(getattr(ai, "tool_calls", None) or [])
    if calls and tool_calls < agent.max_tool_calls:
        data["pending_tool_calls"] = calls
        return messages + [ai], tool_calls, True
    # 没有工具请求、或预算已用尽：这就是她要说的话（预算用尽时不再执行，直接收尾）
    data["tool_final"] = agent.clean_reply(content_of(ai))
    return messages, tool_calls, False


def _settle_tool_final(agent, phase, data, text):
    """工具轮的终点：把模型最后那句落到本阶段的出品上。

    ``own_turn``：这是落子宣告，并把落点定案（S21 的三条边界都在 ``agent._resolve_ai_move`` 里）；
    ``observe``：这是提示台词，写进 ``data["reply"]`` 供 ``speak`` 直接用。
    """
    if phase == PHASE_OWN_TURN:
        cand = agent._candidate_from_text(text)
        pick = agent._resolve_ai_move(cand, data.get("after_infos"))
        data["move"] = {"x": pick[1], "y": pick[2], "gtp": pick[0], "pass": pick[0] == "pass"}
        data["think_msg"] = text
    else:
        data["reply"] = text


def _decide_node(agent):
    """② decide：取应手（首次进入时）＋ 工具轮（条件边可反复进入本节点）。"""

    async def decide(state: TurnState) -> TurnState:
        phase = str(state.get("phase") or "")
        data = dict(state.get("data") or {})
        messages = list(state.get("messages") or [])
        tool_calls = int(state.get("tool_calls") or 0)

        # 首次进入**本阶段**：与 Task 2 逐字一致的装配（own_turn 取默认落点 + 建「用意」提示词）。
        # 工具轮会带着同一份 data 再次进入本节点，那时 messages 已带上 AI/Tool 消息，装配不能再跑
        # 一遍；而 data 是三个阶段的跨阶段载体，所以标记必须带上 phase（否则 after_player 的标记
        # 会让 own_turn 整个跳过装配）。
        if data.get("setup_phase") != phase:
            if phase == PHASE_OWN_TURN and not data.get("short"):
                messages = list(await _decide_own_turn(agent, data) or [])
            data["setup_phase"] = phase

        wants = False
        if (agent.tools_enabled and phase in TOOL_PHASES and not data.get("short")
                and _tool_round_ready(agent)
                and int(data.get("llm_rounds", 0)) < MAX_LLM_CALLS_PER_TURN):
            data["llm_rounds"] = int(data.get("llm_rounds", 0)) + 1
            messages, tool_calls, wants = await _decide_tool_round(
                agent, phase, data, messages, tool_calls)
            if not wants:                            # 工具轮的终点：落点定案 / 提示台词定稿
                _settle_tool_final(agent, phase, data, str(data.get("tool_final") or ""))

        return {
            "messages": messages,
            "tool_calls": tool_calls,
            "wants_tools": bool(wants),
            "move": data.get("move") or state.get("move"),
            "data": data,
        }

    return decide


# ================= ②′ tools：执行工具轮（条件边支线） =================

def _tools_node(agent):
    """``decide`` 与 ``speak`` 之间的支线：执行模型请求的工具，把结果回灌进 ``messages``。

    复用计划 02 的工具集与 ``agent._run_tool``（start/end 事件、超时、未知工具、引擎失败全在那里
    兜住，绝不抛异常）；本节点只做「执行 + 拼接」：每个工具结果作为一条 ``ToolMessage`` **追加进声明过的
    ``state["messages"]``**（覆盖 reducer + 手动拼接，不会丢消息），工具输出只回到模型手里、
    永不进台词。执行完回到 ``decide`` 再来一轮（条件边 ``tools → decide``）。

    超额调用（预算已满）**不执行但必须补一条 ``TOOL_BUDGET_TEXT`` 的 ToolMessage**：这一轮的
    AIMessage 里每个 ``tool_call_id`` 都要有应答，否则紧接着的 ``tools → decide`` 那一轮模型往返
    会被 OpenAI 兼容端点判成 400（与 ``agent._tool_loop`` 的既有语义逐字对齐）。
    """

    async def tools_node(state: TurnState) -> TurnState:
        from .agent import TOOL_BUDGET_TEXT        # 延迟导入：agent ↔ agent_graph 是模块级循环依赖
        data = dict(state.get("data") or {})
        messages = list(state.get("messages") or [])
        tool_calls = int(state.get("tool_calls") or 0)
        by_name = {t.name: t for t in TOOLS}
        for tc in list(data.get("pending_tool_calls") or []):
            name = str(tc.get("name", ""))
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            if tool_calls >= agent.max_tool_calls:      # 预算用尽：不执行，但补预算提示（不得留未应答的 id）
                messages = messages + [ToolMessage(
                    TOOL_BUDGET_TEXT % agent.max_tool_calls,
                    tool_call_id=str(tc.get("id", "")),
                )]
                continue
            tool_calls += 1
            messages = messages + [ToolMessage(
                await agent._run_tool(by_name, name, args),
                tool_call_id=str(tc.get("id", "")),
            )]
        data["pending_tool_calls"] = []
        return {
            "messages": messages,
            "tool_calls": tool_calls,
            "wants_tools": False,
            "data": data,
        }

    return tools_node


# ================= ③ speak：出话 =================

async def _speak_after_player(agent, data, msgs):
    """玩家那手的点评（定式成型时换成讲解，不额外增加 LLM 调用）。"""
    jk = data.get("jk") or {}
    player_eval = await _say(agent, msgs, 300)
    if str(jk.get("kind", "")) == "formed" and agent._llm_failed(player_eval):
        player_eval = prompts.joseki_fallback(str(jk["name"]), str(jk["key"]))
    elif agent._llm_failed(player_eval):        # 云端失败：点评退化为兜底台词
        player_eval = agent._fallback_line("game")
    data["reply"] = player_eval
    data["player_eval"] = player_eval           # 供 own_turn / just_moved 阶段的提示词引用（同一句）


async def _speak_own_turn(agent, data, msgs):
    """她的落子宣告：先发带坐标的 ``ai_move``，再把这一手落到内部棋谱。

    工具轮若已在 ``decide`` 里跑完（``data["think_msg"]`` 已定稿），这里不再调模型；否则与
    Task 2 同一条路（工具关 → ``_reply``；工具开但链不可用 → ``_tool_loop`` 自己退回 ``_reply``）。
    """
    think_msg = data.get("think_msg")
    if think_msg is None:
        if agent.tools_enabled:
            think_msg = await agent._tool_loop(msgs, 80, "own_turn")
        else:
            think_msg = await _say(agent, msgs, 80)
    if agent._llm_failed(think_msg):            # 云端失败：落子宣告退化为兜底台词（坐标照常揭示）
        think_msg = agent._fallback_line("game")
    data["think_msg"] = think_msg

    mv = dict(data.get("move") or {})
    ai_gtp = str(mv.get("gtp") or "")
    ai_x = int(mv.get("x", -1))
    ai_y = int(mv.get("y", -1))
    # 这条语音开始播放时，界面才把她的子放上棋盘（事件带坐标，客户端本地揭示）；
    # speech 紧跟在它后面发（T3/T2b：语音与气泡一一配对，前端仍在语音开始时 reveal）
    await _emit_line(agent, events.AI_MOVE, think_msg, move={
        "x": ai_x, "y": ai_y, "gtp": ai_gtp, "pass": ai_gtp == "pass",
    })

    # 真正落子：先落到内部棋谱；棋盘可见的更新延后到语音开始时（reveal_move）
    if ai_gtp == "pass":
        agent.st.board.pass_move()
        agent.st.add_move("W", "pass")
    else:
        agent.st.board.play(ai_x, ai_y, "white")
        agent.st.add_move("W", ai_gtp)
    agent._reveal_pending = True
    if ai_gtp == "pass":
        await _emit_line(agent, events.AI, "（Azure本手选择了虚着）")


async def _speak_just_moved(agent, data, msgs):
    """她落子后那句感想。"""
    jk2 = data.get("jk2") or {}
    ai_eval = await _say(agent, msgs, 120)
    if str(jk2.get("kind", "")) == "formed" and agent._llm_failed(ai_eval):
        ai_eval = prompts.joseki_fallback(str(jk2["name"]), str(jk2["key"]))
    elif agent._llm_failed(ai_eval):            # 云端失败：落子后那句退化为兜底台词
        ai_eval = agent._fallback_line("game")
    data["reply"] = ai_eval


async def _speak_observe(agent, data, msgs):
    """提示台词（工具轮若已在 ``decide`` 里定稿则直接用；否则与 Task 2 同一条路）。"""
    if data.get("short"):
        return
    msg = data.get("reply")
    if msg is None:
        if agent.tools_enabled:                 # 工具只在这一处与落子宣告启用（短回合不背工具往返）
            msg = await agent._tool_loop(msgs, 300, "observe")
        else:
            msg = await _say(agent, msgs, 300)
    if agent._llm_failed(msg):                  # 云端失败：提示退化为兜底台词
        msg = agent._fallback_line("game")
    data["reply"] = msg


async def _speak_analyze(agent, data, msgs):
    """分析台词。"""
    if data.get("short"):
        return
    msg = await _say(agent, msgs, 350)
    if agent._llm_failed(msg):                  # 云端失败：分析退化为兜底台词（形势数据照常下发）
        msg = agent._fallback_line("game")
    data["reply"] = msg


async def _speak_chat(agent, data, msgs):
    """聊天台词（消息构造失败时按空回复处理，由 finalize 的兜底顶上）。"""
    data["reply"] = await _say(agent, msgs, 400) if msgs else ""


async def _speak_idle(agent, data, msgs):
    """闲置搭话台词。"""
    reply = await _say(agent, msgs, 90)
    if agent._llm_failed(reply):
        reply = agent._idle_line()
    data["reply"] = reply


async def _speak_summary(agent, data, msgs):
    """总结台词。"""
    text = await _say(agent, msgs, 600)
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
        msgs = list(state.get("messages") or [])
        fn = _SPEAK.get(phase)
        if fn is not None:
            await fn(agent, data, msgs)
        return {"reply": str(data.get("reply") or ""), "data": data}

    return speak


# ================= ③′ speak 的可选流式通道（默认关闭） =================

async def _streamed_text(agent, msgs, max_tokens):
    """speak 阶段的出话：``stream_tokens`` 开启且来源支持 ``astream`` 时逐块发 ``token``，返回完整台词。

    返回 ``None`` 表示「这次不走流式」，调用方回落到 ``agent._reply``——默认关闭时走的永远是那条路，
    事件序列与没有这个功能时逐字一致（S31③）。四条不可回退的约束：

    - **推理段永不显示**：``agent._stream_chunks`` 只吐回答原文（content 通道），think 段的剥离由
      ``ThinkStream`` 在**累加文本**上跨块完成。**保证 delta 与台词都干净**的形态：成对
      ``<think>…</think>``、``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`` 区间（含标签/标记被切块，
      标记自身永不外发）；**只保证最终台词干净**的降级形态（token 流可能短暂出现推理原文，气泡随后
      由 ``message`` 整条替换）：网关吞掉开始标记、只剩 ``CoT…<｜end▁of▁thinking｜>``，以及未闭合的
      ``<think>``（台词按 ``content_text_of`` 兜底成兜底句）。空增量不发事件（硬规则 3）；
    - **台词定稿与整条原文同构**：结束时对**累加原文**走 ``llm_chain.content_text_of``（即
      ``content_of`` 的纯文本版，含「未闭合 ``<think>`` → 空串」的兜底），再经 ``clean_reply``——
      与非流式路径（``LLMClient.chat`` → ``content_of`` → ``clean_reply``）逐字同一条规则
      （降级形态下 token 流可以脏，**定稿这一步不脏**：TTS 读的就是它）；
    - **去重闸门对流式路径同样生效**（S32/S37）：累加的可见文本**还是窗口里某句的前缀**时先扣住
      已收到的增量不发，一旦分叉（确认是新话）立刻按序补发；整句原样复读 ⇒ 一条 ``token`` 都不发
      （它的 ``message`` 也照旧被 ``_emit_deduped`` 拦下），前端不会留下没收口的残余气泡。
      判据是「**是窗口某句的前缀**」（不是「相等」、更不是「包含」）：分叉即补发，以那句开头的新话照样完整发出；
    - **流式本身失败**（连接断开 / 服务端报错）**绝不影响这一回合**：返回 ``None`` 让调用方走非流式那条路，
      台词必定完整（前端的气泡最终内容以 ``message`` 为准，会被整条替换）。
    """
    chunks = agent._stream_chunks(msgs, max_tokens)
    if chunks is None:
        return None
    stream = ThinkStream()
    deltas = []                                 # 收到的可见增量（按序）
    sent = 0                                    # 已经发出去的条数
    acc = ""                                    # 可见文本累加（扣留判据用它）
    held = True                                 # 是否仍在「可能只是某句前缀」的扣留区
    try:
        async for piece in chunks:
            delta = stream.feed(piece)
            if delta == "":
                continue                        # 这一块全是推理段 / 标记 / 滞留尾巴：不发事件
            deltas.append(delta)
            acc += delta
            if held and not agent._is_repeat_prefix(agent.clean_reply(acc)):
                held = False                    # 分叉了：确认是新话，积压的增量按序补发
            if not held:
                for d in deltas[sent:]:
                    agent._emit(events.token(agent.turn_id, d))
                sent = len(deltas)
    except Exception as e:                          # noqa: BLE001 —— 流式失败退回非流式，绝不抛出
        print("[Agent] 流式输出失败（退回非流式）：%s" % e)
        return None
    text = content_text_of(stream.raw)              # 定稿：与「对整条原文调 content_of」同构
    if text == "":
        return ""                                   # 未闭合兜底 / 空内容：交给调用方的兜底判据
    return agent.clean_reply(text)


async def _say(agent, msgs, max_tokens):
    """speak 阶段的统一出话口：流式开着就走流式，否则与 Task 2/3/4 的 ``agent._reply`` 同一条路。"""
    text = await _streamed_text(agent, msgs, max_tokens)
    return await agent._reply(msgs, max_tokens) if text is None else text


# ================= ④ finalize：发事件、写 memory_log、归位 busy =================

async def _emit_speech(agent, text):
    """把一条**刚发出去的**台词交给 TTS，并按需补发一条 ``speech`` 事件（与它相邻，T2b/T3）。

    ``tts`` 未接线（``None``）时**什么都不做**——行为与没有这个功能时逐字一致（一条 ``speech`` 都不发）。
    ``mode == "none"``（语音关闭 / 没有可念的内容）同样不发；``openai`` / ``system`` 都发。

    TTS 出错**绝不影响这一回合**：``TTSClient.speak()`` 自己就不抛（失败一律降级 ``system``），
    这里再兜一层只是为了「注入的假 TTS 抛了」也不至于把 ``busy`` 卡死。
    """
    tts = getattr(agent, "tts", None)
    if tts is None:
        return
    try:
        res = await tts.speak(text)
    except Exception as e:                          # noqa: BLE001 —— 语音失败只丢语音，文字照常
        print("[TTS] 合成异常（本条不发语音事件）：%s" % e)
        return
    res = res if isinstance(res, dict) else {}
    mode = str(res.get("mode") or "none")
    if mode == "none":
        return
    agent._emit(events.speech(
        res.get("id") or "", mode, res.get("tone") or "",
        agent.speech_pitch(), agent.turn_id, agent._next_speech_seq(), res.get("text") or "",
    ))


async def _emit_line(agent, kind, text, move=None):
    """发一条对外台词：先 ``message``，**紧随其后**补 ``speech``（音画同步：前端按 turn_id+seq 配对）。"""
    agent._emit(events.message(kind, text, move))
    await _emit_speech(agent, text)


async def _emit_deduped(agent, kind, text):
    """发一条**非落子宣告**的 ``message`` 事件：窗口内一字不差的原话不再推给前端。

    只做「发事件」这一道闸门（S25/S26）：调用方的 ``memory_log`` / ``chat_history`` 写入**照常**发生；
    被抑制时**不发任何替代事件**（**连同它的 ``speech`` 一起不发**，T3——否则玩家会听到一句屏幕上没有的话）；
    被抑制的那句照常入窗（S27，与前端 ``_recent_ai_texts`` 一致）。
    ``ai_move`` 落子宣告不走这里（在 ``_speak_own_turn`` 里直发），永不受判重。
    """
    from .agent import AI_DEDUP_WINDOW        # 延迟导入：agent ↔ agent_graph 是模块级循环依赖
    if agent._is_repeat(text):
        print("[Agent] 原话去重（最近 %d 条内一字不差）已抑制：%s" % (AI_DEDUP_WINDOW, text))
    else:
        await _emit_line(agent, kind, text)
    agent._note_line(text)


async def _finalize_after_player(agent, data):
    """点评入档并发给玩家（tag ``E``）。**不归位 busy**：本回合还有她自己的落子。"""
    text = str(data.get("reply") or "")
    agent.st.log_memory("E", text, len(agent.st.moves))
    await _emit_deduped(agent, events.AI_COMMENT, text)


async def _finalize_own_turn(agent, data):
    """她那一步的定式结论（``jk2``）**已由 ``finalize`` 节点算好**，这里只把它留在本轮 data 里，
    供下一阶段（``just_moved`` 挑模板 / 发 ``joseki`` 事件）使用。**不归位 busy**。

    这里**不得**再调 ``agent._joseki_check()``：它会消耗去重键（同角同族第二次必然返回 ``{}``），
    而结论已经在上一步算出来了（T7 把这次识别上移到 async 的节点外壳，因为要 ``await`` 入围判定）。
    """
    data["jk2"] = data.get("jk2") or {}


async def _finalize_just_moved(agent, data):
    """落子后感想入档（tag ``A``）、发事件、定式轨迹，然后归位 busy 与情绪小调用。"""
    text = str(data.get("reply") or "")
    agent.st.log_memory("A", text, len(agent.st.moves))
    await _emit_deduped(agent, events.AI_COMMENT, text)
    # 卡片载荷（T6）：`jk_show_card` / `jk_mistake` 是**本回合 data 里既有的值**——玩家那一手在
    # `after_player` 里定，她那一手由 `finalize` 节点（T7）在她这一格补齐；这里只读不改。
    # `_set_busy(False)` 的位置**不动**（T7 只换了 `jk2` 的算法在哪一步，事件顺序与解锁点一字未变）。
    agent._joseki_emit(data.get("jk2") or {},
                       data.get("jk_show_card", False), data.get("jk_mistake"))
    agent._set_busy(False)
    await agent._mood_llm_refine()
    # 不在这里 emit state：落子的可见更新由 reveal_move() 在她这段语音开始时触发


async def _finalize_observe(agent, data):
    """提示发事件（短路时只归位 busy），然后解锁。"""
    if not data.get("short"):
        await _emit_deduped(agent, events.AI_COMMENT, str(data.get("reply") or ""))
    agent._set_busy(False)


async def _finalize_analyze(agent, data):
    """分析发事件 + 形势栏（短路时只归位 busy），然后解锁。"""
    if not data.get("short"):
        await _emit_deduped(agent, events.AI, "📊 " + str(data.get("reply") or ""))
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
    await _emit_deduped(agent, events.AI, reply)
    await agent._mood_llm_refine()


async def _finalize_idle(agent, data):
    """闲置搭话收尾（不进 busy，与今天一致）。"""
    reply = str(data.get("reply") or "")
    agent.st.chat_history.append({"role": "assistant", "content": reply})
    await _emit_deduped(agent, events.AI_IDLE, reply)


async def _finalize_summary(agent, data):
    """总结收尾：置终局标记 → 发话 → 真总结才写长期记忆 → 解锁。"""
    text = str(data.get("reply") or "")
    agent.st.finished = True
    await _emit_deduped(agent, events.AI, "📋 对局总结\n" + text)
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
        # —— T7（U36）：她那一步的定式识别从同步的 ``_finalize_own_turn`` 上移到这个 async 节点 ——
        # 入围判定要 ``await``，而结论必须赶在 ``_prepare_just_moved`` 挑模板之前算好，所以落在
        # ``own_turn`` 这一步（紧接在 ``just_moved`` 的 prepare 之前）。``just_moved`` 那一格不在这里
        # ——它由 ``_prepare_just_moved`` 自己的兜底覆盖（更早，正好在挑模板之前）。
        if phase == PHASE_OWN_TURN:
            await _joseki_own_move(agent, data)
        fn = _FINALIZE.get(phase)
        if fn is not None:
            await fn(agent, data)
        return {"data": data}

    return finalize


def build_graph(agent):
    """按 ``agent`` 装配回合状态图。

    拓扑：``prepare → decide → (tools → decide)* → speak → finalize → END``。``decide`` 之后是
    条件边 ``route_after_decide``（还有待执行的工具请求且在预算内就去 ``tools``，否则去 ``speak``）。
    """
    g = StateGraph(TurnState)
    g.add_node("prepare", _prepare_node(agent))
    g.add_node("decide", _decide_node(agent))
    g.add_node("tools", _tools_node(agent))
    g.add_node("speak", _speak_node(agent))
    g.add_node("finalize", _finalize_node(agent))
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "decide")
    g.add_conditional_edges(
        "decide",
        lambda state: route_after_decide(state, agent.max_tool_calls),
        {"tools": "tools", "speak": "speak"},
    )
    g.add_edge("tools", "decide")
    g.add_edge("speak", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
