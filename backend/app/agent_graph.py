"""回合编排状态图（LangGraph）。

计划 04 的目标是把 ``AzureAgent`` 里「写死的函数调用顺序」显式成一张状态图。本文件是它的
**第一层骨架**：``prepare → decide → speak → finalize → END`` 依次相连、**没有任何条件边**，
拓扑与现有编排顺序一一对应、行为一字不变——图目前还没有被任何公开方法调用（Task 2 才接进去）。

后续任务（条件边 / 去重下沉 / 流式 token）都必须守住这几条约定：

- ``TurnState`` 的 7 个字段是本图与 ``AzureAgent`` 之间的**唯一接口**，一个都不能少；
- ``busy`` 归位由**调用方**（``AzureAgent`` 的公开方法）用 ``try/finally`` 兜住——图本身不碰
  ``busy``，但任一节点抛异常（工具超时 / LLM 500 / 解析失败）都必须让棋盘解锁；
- 台词一律经 ``agent.clean_reply()``（``_reply()`` 内部必经），推理段永不进台词；
- ``build_graph(agent)`` 用闭包捕获 ``agent`` 的依赖（``llm`` / ``katago`` / ``st`` / ``mood``），
  模块级不持有任何可变状态：同一个 ``agent`` 重建图是安全的，不同 ``agent`` 各有一张图。
"""

import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

# 节点名（顺序即拓扑顺序）：prepare → decide → speak → finalize
NODE_NAMES = ("prepare", "decide", "speak", "finalize")

# 一次回合内**总 LLM 调用次数**上限（含工具轮）：工具预算之外的硬闸，由 decide 消费（Task 3）
MAX_LLM_CALLS_PER_TURN = 4


class TurnState(TypedDict):
    """一次回合的状态：本图与 ``AzureAgent`` 之间的唯一接口（7 个字段一个都不能少）。"""

    phase: str              # 回合类型：after_player / own_turn / just_moved / observe / analyze / chat
    moves: list             # 回合开始时的棋谱快照（[[color, gtp], ...]，取自 agent.st.moves）
    messages: list          # 发给模型的消息（系统 + 上下文 + 历史窗口）
    reply: str              # 本回合已说出口的台词（必经 clean_reply）
    tool_calls: int         # 已执行的工具调用次数（上限 agent.max_tool_calls）
    move: dict | None       # 落点 {x, y, gtp, pass}；None = 本回合不落子（聊天/提示一类）
    turn_id: str            # 本轮唯一标识（流式 token 与重复发话的判据都靠它）


def _prepare_node(agent):
    """① prepare：建上下文与提示词（棋谱快照 + 系统消息）。"""

    async def prepare(state: TurnState) -> TurnState:
        # 消息为空时退回她现成的系统消息；逐阶段的 _phase_messages 在 Task 2 随公开方法接进来
        messages = list(state.get("messages") or [])
        if not messages:
            messages = [agent._sys_msg()]
        return {
            "phase": str(state.get("phase") or ""),
            "moves": list(state.get("moves") or agent.st.moves),
            "messages": messages,
            "reply": str(state.get("reply") or ""),
            "tool_calls": int(state.get("tool_calls") or 0),
            "turn_id": str(state.get("turn_id") or uuid.uuid4().hex),
        }

    return prepare


def _decide_node(agent):
    """② decide：取应手与工具（本任务先等价：落点仍由 ``agent._pick_ai_move`` 决定）。"""

    async def decide(state: TurnState) -> TurnState:
        # 这里只把工具计数收进状态；候选点（引擎候选 / 工具返回）进状态、逐轮预算
        # （MAX_LLM_CALLS_PER_TURN）与 decide → tools 的条件边都在 Task 2/3，本任务不预设它们的形状。
        return {"tool_calls": int(state.get("tool_calls") or 0)}

    return decide


def _speak_node(agent):
    """③ speak：出话（点评 / 落子宣告 / 聊天回复都走这里）。"""

    async def speak(state: TurnState) -> TurnState:
        reply = str(state.get("reply") or "")
        messages = list(state.get("messages") or [])
        if reply == "" and messages:
            # 台词一律走 _reply（clean_reply 必经）；云端失败时它返回空串，
            # 由调用方的兜底台词顶上——与现有编排同一个降级判据，这里不额外造词。
            reply = str(await agent._reply(messages, 300) or "")
        return {"reply": reply}

    return speak


def _finalize_node(agent):
    """④ finalize：收尾（发事件 / 写 ``st.memory_log``）。"""

    async def finalize(state: TurnState) -> TurnState:
        # 本任务只占住拓扑位置：各公开方法的事件 kind（ai / ai_comment / ai_move / ai_idle）与
        # 记忆标签（E / A）各不相同，Task 2 随公开方法一起搬进来，Task 4 再把 8 条窗口的
        # 复读抑制落在这一节点——现在写死任何一种口径都会与另一条路径不符。
        return {}

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
