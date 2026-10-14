# 计划 04：LangGraph Agent Loop —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把回合编排从“写死的函数调用顺序”改成显式状态图（LangGraph），让工具在**预算内**真正参与决策（包括复算后再定落点），并把 AI 原话去重从前端下沉到后端。

**Architecture:** 新增 `agent_graph.py`：`prepare → decide → (tools)* → speak → finalize`，`AzureAgent` 的公开方法（`player_move/hint/analyze/chat/idle_remark/summarize/undo/reset`）退化为“构造初始状态 → `graph.ainvoke()`”。迁移必须**分步且每步可验收**：先做拓扑等价的单节点图（行为一字不变），再逐个方法搬到真正带条件边的图上；每一步都用现有 `tests/test_agent.py` 兜底。

**Tech Stack:** `langgraph`（`StateGraph`、`TypedDict` 状态、条件边）；`langchain-core` 消息类型；`unittest` + 假链/假引擎。

**Spec:** `docs/backend-langchain-plan.md`（硬规则 1~5；非目标：不改棋盘与情绪模型）

## Global Constraints

- 前置：计划 0/01/02 已完成（计划 03 可选，不构成依赖）。
- 测试一律 `unittest`：`cd backend && python -m unittest discover -s tests`；不得触网。
- **公开契约不变**：`main.py` 的全部端点、`events.py` 的全部事件形状、`AzureAgent` 的全部公开方法签名，都不许改（前端 `NetClient.gd` 一行不动）。
- **硬规则必须在图里有对应守卫**（每条都要有测试）：
  1. `ai_move` 事件仍带 `{"x","y","gtp","pass"}` 且**落子可见更新仍由 `POST /api/reveal` 触发**；
  2. AI 原话按最近 8 条窗口去重（本轮从 `Main.gd` 下沉到后端 `finalize`）；
  3. 推理段永不显示；
  4. 落子宣告不参与复读抑制；
  5. `prompts.context_block` 的铁律段（含方位词核对）不被改动。
- 工具预算沿用计划 02：`MAX_TOOL_CALLS = 2`、`TOOL_TIMEOUT_SEC = 10`；`decide` 节点内**总 LLM 调用次数**上限 `MAX_LLM_CALLS_PER_TURN = 4`（含工具轮）。
- `busy` 语义不变：进入图即 `True`，图结束（含异常）必须 `False`——用 `try/finally` 兜底。
- 提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.4.0**，同步写进 `README.md` 的「更新」小节。

## Review Focus

- **图异常导致 busy 永久为真**：任一节点抛异常（工具超时、LLM 500、JSON 解析失败）都必须让 `busy` 归位并发一次 `busy:false`——这是最严重的故障形态。
- **重复发话**：条件边把同一轮走两遍时，`message` 事件不得重复推送（同 `turn_id` + 同文本只发一次）。
- **音画同步被打乱**：`reveal_move()` 与图的顺序无关，必须在“图形走完但 `_reveal_pending` 仍为真”时照样工作（`state` 事件由 `reveal` 独立触发）。
- **流式与音画同步互相干扰**：开启 `token` 流式时，文字早于语音到达，`_speech_hold`（语音未播完锁棋盘）必须仍然正确。
- **去重下沉后前端仍收到重复句**：迁移期两端都判会“双保险”掩盖 bug——必须在后端测试里断言重复句**不出现在事件里**，而不是断言前端过滤掉了。

---

### Task 1: 依赖与拓扑等价的状态图骨架

**Files:**
- Create: `backend/app/agent_graph.py`
- Modify: `backend/requirements.txt`（`+ langgraph>=0.2`）
- Create: `backend/tests/test_agent_graph.py`

**Interfaces:**
- Consumes: `events.EventBus`、`prompts`、`agent.AzureAgent`
- Produces:
  - `class TurnState(TypedDict)`（字段：`phase: str`、`moves: list`、`messages: list`、`reply: str`、`tool_calls: int`、`move: dict | None`、`turn_id: str`）
  - `build_graph(agent) -> CompiledGraph`（`agent` 为 `AzureAgent`，节点函数从其取 `llm/katago/st/mood`）
  - `NODE_NAMES = ("prepare", "decide", "speak", "finalize")`、`MAX_LLM_CALLS_PER_TURN = 4`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_agent_graph.py`

```python
"""状态图骨架：拓扑存在、单路径等价、异常不吞 busy。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from test_agent import StubEngine, StubLLM, _mk


class GraphShapeTest(unittest.TestCase):
    def test_nodes_present(self):
        from app.agent_graph import NODE_NAMES, build_graph
        agent, _eng, _llm, _bus = _mk()
        g = build_graph(agent)
        self.assertEqual(set(NODE_NAMES), {"prepare", "decide", "speak", "finalize"})
        self.assertTrue(hasattr(g, "ainvoke"))

    def test_budget_constant(self):
        from app.agent_graph import MAX_LLM_CALLS_PER_TURN
        self.assertEqual(MAX_LLM_CALLS_PER_TURN, 4)


class GraphFailSafeTest(unittest.TestCase):
    class BoomLLM(StubLLM):
        async def chat(self, messages, max_tokens=300, no_think=False):
            raise RuntimeError("云端 500")

    def test_exception_still_releases_busy(self):
        agent, eng, _llm, bus = _mk()
        agent.llm = self.BoomLLM()

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        self.assertFalse(agent.busy)
        self.assertEqual([e["busy"] for e in bus.of_type("busy")][-1], False)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pip install -r requirements.txt && python -m unittest tests.test_agent_graph -v`
Expected: FAIL（`No module named 'app.agent_graph'`）

- [ ] **Step 3: 实施** — 建 `StateGraph(TurnState)`，先只连 `prepare → decide → speak → finalize → END` 四条边（**没有条件边**），四个节点函数暂时直接调用 `agent` 现有方法（`_sys_msg`/`_reply` 等），确保“先等价、后演化”。`build_graph(agent)` 用 `agent` 闭包捕获依赖，避免图内再持有全局状态。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_agent_graph tests.test_agent -v`
Expected: 全 PASS（`test_agent` 的 40+ 条行为断言一条都不许变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent_graph.py backend/requirements.txt backend/tests/test_agent_graph.py
git commit -m "feat(backend,agent): LangGraph 状态图骨架（拓扑等价，行为不变）（v3.4.0）"
```

---

### Task 2: 把公开方法搬进图（逐方法迁移）

**Files:**
- Modify: `backend/app/agent.py`（`player_move/hint/analyze_position/chat/idle_remark/summarize/undo/reset_game/set_allow_pass/reveal_move/tick_mood/player_interaction` 全部改为“构造状态 → 跑图 / 直接事件”，函数体内**不再有**编排顺序）
- Modify: `backend/app/agent_graph.py`（把 `player_move` 与 `_ai_turn` 的步骤拆进 `prepare/decide/speak`：`prepare` 建上下文与提示词；`decide` 取应手与工具；`speak` 出点评与落子宣告；`finalize` 发事件、写 `memory_log`、`_set_busy(False)`）
- Modify: `backend/tests/test_agent.py`（不得改动任何既有断言；只允许在文件末尾追加）

**Interfaces:**
- Consumes: `TurnState`、`build_graph`（Task 1）
- Produces: 每次公开方法调用 = 一次 `graph.ainvoke(state)`；`AzureAgent` 保留 `busy`、`st`、`mood`、`katago`、`llm` 属性

- [ ] **Step 1: 迁移一个方法（`hint`）并跑测试**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: 全 PASS（事件序列与迁移前一致）

- [ ] **Step 2: 迁移 `player_move` + `_ai_turn`（最复杂的一步）** — 关键不变式：`ai_move` 事件带坐标、`_reveal_pending = True`、内部棋谱先落子、`state` 事件延后到 `reveal_move()`。
- [ ] **Step 3: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_agent tests.test_main -v`
Expected: 全 PASS

- [ ] **Step 4: 迁移其余方法（`analyze/chat/idle/summarize`）** — 每个方法迁完立刻跑一次 `python -m unittest tests.test_agent -v`。

- [ ] **Step 5: 全量回归并提交**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

```bash
git add backend/app/agent.py backend/app/agent_graph.py backend/tests/test_agent.py
git commit -m "refactor(backend,agent): 编排拆入状态图节点，公开契约不变（v3.4.0）"
```

---

### Task 3: 工具参与决策（条件边 + 落点复算）

**Files:**
- Modify: `backend/app/agent_graph.py`（新增条件边：`decide` → `tools` → `decide`，直到 `tool_calls` 用完或无工具请求；`tools` 节点复用计划 02 的工具集与事件）
- Modify: `backend/app/agent.py`（`_pick_ai_move` 保留为默认策略；新增“工具候选优先”分支：`decide` 里若模型给出候选点且合法则用它，否则回退 `_pick_ai_move`）
- Modify: `backend/tests/test_agent_graph.py`

**Interfaces:**
- Consumes: `tools.TOOLS`、`events.tool`（计划 02）
- Produces: 条件边函数 `route_after_decide(state, max_calls=2) -> "tools" | "speak"`；工具请求次数写入 `state["tool_calls"]`

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_agent_graph.py`

```python
class ToolRoutingTest(unittest.TestCase):
    def test_no_tool_request_goes_straight_to_speak(self):
        from app.agent_graph import route_after_decide
        st = {"tool_calls": 0, "wants_tools": False}
        self.assertEqual(route_after_decide(st, max_calls=2), "speak")

    def test_tool_request_within_budget_goes_to_tools(self):
        from app.agent_graph import route_after_decide
        st = {"tool_calls": 1, "wants_tools": True}
        self.assertEqual(route_after_decide(st, max_calls=2), "tools")

    def test_budget_exhausted_forces_speak(self):
        from app.agent_graph import route_after_decide
        st = {"tool_calls": 2, "wants_tools": True}
        self.assertEqual(route_after_decide(st, max_calls=2), "speak")


class ToolShapesEventsTest(unittest.TestCase):
    def test_tool_events_order(self):
        """工具轮必须成对发 start/end，且 end 一定在 speak 之前。"""
        agent, _eng, _llm, bus = _mk()
        agent.tools_enabled = True

        async def run():
            await agent.hint()

        asyncio.run(run())
        kinds = [e["type"] for e in bus.events]
        for i, e in enumerate(bus.of_type("tool")):
            self.assertEqual(e["phase"], "start" if i % 2 == 0 else "end")
        if "tool" in kinds:
            self.assertLess(kinds.index("tool"), kinds.index("message"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent_graph -v`
Expected: FAIL（`route_after_decide` 不存在）

- [ ] **Step 3: 实施** — 实现 `route_after_decide(state, max_calls)` 与 `tools` 节点；`tools` 节点执行后把 `state["tool_calls"] += 1` 并把结果作为 `ToolMessage` 追加进 `state["messages"]`。落点分支：`decide` 结束前若 `state["move"]` 为空则调 `agent._pick_ai_move(...)`。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_agent_graph tests.test_tools tests.test_agent -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent_graph.py backend/app/agent.py backend/tests/test_agent_graph.py
git commit -m "feat(backend,agent): 条件边让工具参与落点候选与发言决策（v3.4.0）"
```

---

### Task 4: 去重下沉后端（8 条窗口）

**Files:**
- Modify: `backend/app/agent.py`（新增 `_recent_lines: list[str]`（上限 8）与 `_is_repeat(text) -> bool`；`finalize` 对**非落子宣告**的台词判重，重复则改发既有的下一句候选或直接不发）
- Modify: `backend/app/agent_graph.py`（`finalize` 节点调用去重）
- Modify: `game/scripts/ui/Main.gd`（保留现有兜底判据；注释改为“后端已去重，这里只兜 WS 重连后的乱序”）
- Modify: `backend/tests/test_agent.py`（追加去重断言）

**Interfaces:**
- Consumes: 无
- Produces:
  - `AI_DEDUP_WINDOW = 8`
  - `AzureAgent._is_repeat(text: str) -> bool`（最近 8 条**一字不差**才算重复；`ai_move` 类台词不入窗口、不受判重影响）

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_agent.py`

```python
class DedupTest(unittest.TestCase):
    def test_repeat_within_window_is_suppressed(self):
        agent, _eng, llm, bus = _mk()
        sent = []

        async def run():
            for _ in range(2):
                bus.clear()
                await agent.chat("你好")
                sent.append([e["text"] for e in bus.messages(events.AI)])

        llm.text = "我在的~"
        asyncio.run(run())
        self.assertEqual(len(sent[0]), 1)
        self.assertEqual(len(sent[1]), 0)          # 隔一轮的原样复读被拦下

    def test_move_announcement_not_deduped(self):
        agent, _eng, llm, bus = _mk()
        llm.text = "我在D16补一手。"

        async def run():
            await agent.player_move(3, 3)
            first = len(bus.messages(events.AI_MOVE))
            await agent.player_move(15, 15)
            return first, len(bus.messages(events.AI_MOVE))

        first, second = asyncio.run(run())
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)                # 落子宣告不受复读抑制

    def test_window_is_eight(self):
        from app.agent import AI_DEDUP_WINDOW
        self.assertEqual(AI_DEDUP_WINDOW, 8)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: FAIL（第二条 `sent[1]` 长度为 1）

- [ ] **Step 3: 实施** — 判据必须与前端一致：`text.strip()` 后**完全相等**才算重复；窗口只装“非 `ai_move`”的台词；被抑制时打一条 `print` 便于排查，且不得再发 `message` 事件。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent.py backend/app/agent_graph.py backend/tests/test_agent.py game/scripts/ui/Main.gd
git commit -m "feat(backend,agent): 原话去重（8 条窗口）下沉后端，前端仅作兜底（v3.4.0）"
```

---

### Task 5: 流式 `token` 事件（默认关闭）

**Files:**
- Modify: `backend/app/agent.py`（`speak` 阶段在 `stream_tokens=true` 时用 `astream` 逐块发 `token`）
- Modify: `backend/app/events.py`（新增 `token(turn_id, delta)`）
- Modify: `backend/app/config.py`（`stream_tokens=False`）
- Modify: `game/scripts/net/NetClient.gd`（`token` → 新信号 `token_event(turn_id, delta)`）
- Modify: `game/scripts/ui/Main.gd`（`_on_token_event`：把增量追加到当前气泡；`TYPING_SEC_PER_CHAR` 仅在没有 token 时兜底）
- Modify: `backend/tests/test_agent.py`

**Interfaces:**
- Consumes: 无
- Produces: `{"type": "token", "turn_id": str, "delta": str}`；`AzureAgent.turn_id`（每回合唯一）

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_agent.py`

```python
class StreamTest(unittest.TestCase):
    class StreamLLM(StubLLM):
        async def astream(self, messages, max_tokens=300):
            for piece in ["我", "在", "的~"]:
                yield piece

    def test_tokens_off_by_default(self):
        agent, _eng, _llm, bus = _mk()
        self.assertFalse(agent.stream_tokens)
        asyncio.run(agent.chat("在吗"))
        self.assertEqual(bus.of_type("token"), [])

    def test_tokens_emitted_when_enabled(self):
        agent, _eng, _llm, bus = _mk()
        agent.llm = self.StreamLLM()
        agent.stream_tokens = True
        asyncio.run(agent.chat("在吗"))
        deltas = [e["delta"] for e in bus.of_type("token")]
        self.assertEqual("".join(deltas), "我在的~")
        self.assertTrue(all(e["turn_id"] == bus.of_type("token")[0]["turn_id"] for e in bus.of_type("token")))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: FAIL（`stream_tokens` 属性不存在）

- [ ] **Step 3: 实施** — `speak` 阶段：`stream_tokens` 为真且链支持 `astream` 时走流式（逐块发 `token`，结束发完整 `message`）；否则维持现路径。**关键**：流式下仍要先 `clean_reply()` 再发 `message`（气泡最终内容以 `message` 为准，`token` 只是过程）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 前端接线并冒烟**

按上方 `Files` 改 `NetClient.gd` / `Main.gd`。

Run: `godot --headless --path game --import`
Expected: 无 `SCRIPT ERROR`

- [ ] **Step 6: 提交**

```bash
git add backend/app/agent.py backend/app/agent_graph.py backend/app/events.py backend/app/config.py backend/tests/test_agent.py game/scripts/net/NetClient.gd game/scripts/ui/Main.gd
git commit -m "feat(backend,ui): 可选 token 流式输出（默认关闭）（v3.4.0）"
```

---

### Task 6: 配置、文档与版本

**Files:**
- Modify: `game/config.json`（新增 `stream_tokens: false`）
- Modify: `README.md`（补 `stream_tokens`；「工具」小节改为“工具可参与落点候选”；第九节新增 `### v3.4.0`）
- Modify: `AGENTS.md`（补：编排在 `agent_graph.py` 的图里；改图时必须守住 5 条硬规则与 `busy` 的 `try/finally`；`AI_DEDUP_WINDOW` 现在是后端常量）

**Interfaces:**
- Consumes: 无
- Produces: 与代码一致的文档

- [ ] **Step 1: 改文档与模板**
- [ ] **Step 2: 校验**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `rg -n "AI_DEDUP_WINDOW" backend game`
Expected: 后端为常量定义；前端仅注释提及

- [ ] **Step 3: 提交**

```bash
git add README.md AGENTS.md game/config.json
git commit -m "docs(config): 状态图与流式开关说明，README 记录 v3.4.0（v3.4.0）"
```
