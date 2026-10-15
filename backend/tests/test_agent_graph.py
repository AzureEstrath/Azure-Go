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


class GraphTopologyTest(unittest.TestCase):
    """拓扑守卫：把「主链路 + 条件边支线」钉住。

    Task 1 的这版守卫锁的是「四条无条件边」（当时的硬约束）；Task 3 按计划**有意**引入条件边
    ``decide → tools → decide``，这条守卫随之升级为锁**新拓扑**（否则它会把本任务的核心交付物
    判成非法）。断言仍然直接读 langgraph 编译后的图：节点集合恰好 7 个、边集合恰好多一条支线、
    且**只有** ``decide`` 的两条分支是条件边——漏接/接错/少接任意一条边都会红。
    """

    def test_topology_is_chain_plus_tool_branch(self):
        from app.agent_graph import build_graph
        agent, _eng, _llm, _bus = _mk()
        g = build_graph(agent).get_graph()

        # 节点：langgraph 1.x 会把哨兵节点 __start__ / __end__ 一并列出来；tools 是条件边支线
        nodes = {str(n) for n in g.nodes}
        self.assertEqual(
            nodes, {"__start__", "prepare", "decide", "tools", "speak", "finalize", "__end__"})

        # 边：主链路 + decide 的两条条件分支 + tools 回到 decide，一条不多一条不少
        edges = {(str(e.source), str(e.target)) for e in g.edges}
        self.assertEqual(edges, {
            ("__start__", "prepare"),
            ("prepare", "decide"),
            ("decide", "tools"),
            ("decide", "speak"),
            ("tools", "decide"),
            ("speak", "finalize"),
            ("finalize", "__end__"),
        })

        # 条件边**只**允许 decide 的两条分支；tools 回到 decide 必须是普通边（否则可能死循环失控）
        conditional = {(str(e.source), str(e.target)) for e in g.edges if e.conditional}
        self.assertEqual(conditional, {("decide", "tools"), ("decide", "speak")})


if __name__ == "__main__":
    unittest.main()


# —— 以下为计划 04 · Task 3 追加（既有内容一字未改；追加在 guard 之后，是因为本仓的唯一跑法
#    `python -m unittest discover -s tests` 不会执行上面的 `unittest.main()`）——


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


class _Ai:
    """假链的「一条模型回复」：和 `app.agent._tool_loop` 认的鸭子类型一致。"""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ScriptChain:
    """按脚本回话的假链（`bind_tools`/`bind` 原样返回自己，记录每次请求的消息）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def bind_tools(self, _tools):
        return self

    def bind(self, **_kw):
        return self

    async def ainvoke(self, messages, **_kw):
        self.seen.append(list(messages))
        item = self.script.pop(0)
        return item() if callable(item) else item


class ToolRouteRealTriggerTest(unittest.TestCase):
    """S6：让工具路由**真的触发**（计划给的 `ToolShapesEventsTest` 在 `StubLLM` 下空转）。

    用一个能 `bind_tools`/`bind` 且第一轮返回 `tool_calls` 的假链，直接驱动图（`observe` 阶段），
    断言：①`tool` 事件成对且 `end` 在 `message` 之前；②`state["tool_calls"]` 真的递增；
    ③工具输出只回到模型手里、**不出现在任何 `message` 文本里**。
    """

    def test_tool_round_emits_paired_events_and_keeps_output_out_of_lines(self):
        from app.agent_graph import PHASE_OBSERVE
        agent, _eng, _llm, bus = _mk()
        agent.tools_enabled = True
        agent.chain = _ScriptChain([
            _Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}]),
            _Ai(content="我看了下盘面，先在右边稳一手。"),
        ])

        state = asyncio.run(agent._turn(PHASE_OBSERVE))

        # ② 工具真的执行了一轮：state 上的计数递增，事件成对且成功
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(state["wants_tools"], False)
        starts = [e for e in bus.of_type("tool") if e["phase"] == "start"]
        ends = [e for e in bus.of_type("tool") if e["phase"] == "end"]
        self.assertEqual([e["name"] for e in starts], ["board_query"])
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0]["ok"])

        # ① 最后一次 tool 事件在第一条 message 之前（end 先于 speak）
        kinds = [e["type"] for e in bus.events]
        self.assertIn("tool", kinds)
        last_tool = max(i for i, e in enumerate(bus.events) if e["type"] == "tool")
        first_msg = next(i for i, e in enumerate(bus.events) if e["type"] == "message")
        self.assertLess(last_tool, first_msg)

        # ③ 台词是模型重写的那句；board_query 的输出一个字符都没进任何 message 文本
        texts = [str(e.get("text", "")) for e in bus.messages()]
        self.assertIn("我看了下盘面，先在右边稳一手。", texts)
        self.assertFalse(any("X = Estarth" in t for t in texts), "工具输出漏进台词了")

    def test_tool_rounds_terminate_and_respect_llm_cap(self):
        """条件边不得死循环：模型永远要工具也必须收敛，且 `decide` 内 LLM 调用数 ≤ 上限。"""
        from app.agent_graph import MAX_LLM_CALLS_PER_TURN

        class _LoopChain:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, _tools):
                return self

            def bind(self, **_kw):
                return self

            async def ainvoke(self, messages, **_kw):
                self.calls += 1
                return _Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}])

        agent, _eng, _llm, bus = _mk()
        agent.tools_enabled = True
        chain = _LoopChain()
        agent.chain = chain

        asyncio.run(agent.hint())

        self.assertLessEqual(chain.calls, MAX_LLM_CALLS_PER_TURN)
        starts = [e for e in bus.of_type("tool") if e["phase"] == "start"]
        self.assertEqual(len(starts), agent.max_tool_calls)      # 预算硬停
        self.assertFalse(agent.busy)
        self.assertIs([e["busy"] for e in bus.of_type("busy")][-1], False)


class ToolsDisabledPathTest(unittest.TestCase):
    """S20：`tools_enabled=False` 时工具**完全不上线路**（与 Task 2 逐字一致）。

    就算在实例上挂了链，工具关着时也一个请求都不许发、一条 `tool` 事件都不许有；
    落点仍由 `_pick_ai_move` 决定、出话仍走 `llm.chat`。
    """

    class _ForbiddenChain:
        def bind_tools(self, _tools):
            raise AssertionError("工具关着时不该绑工具")

        async def ainvoke(self, *_a, **_k):
            raise AssertionError("工具关着时不该走链")

    def test_disabled_never_touches_chain_or_emits_tool_events(self):
        agent, _eng, llm, bus = _mk()
        agent.chain = self._ForbiddenChain()        # 挂着也不能碰
        self.assertFalse(agent.tools_enabled)

        asyncio.run(agent.player_move(3, 3))
        asyncio.run(agent.hint())

        self.assertEqual(bus.of_type("tool"), [])
        self.assertTrue(llm.calls)                  # 出话走的还是 llm.chat（Task 2 的路）
        self.assertFalse(agent.busy)
        mv = bus.messages(events.AI_MOVE)[0]["move"]
        self.assertEqual(mv["gtp"], "Q16")          # 落点仍是 _pick_ai_move 的引擎首选


class ToolMoveCandidateTest(unittest.TestCase):
    """S21：模型报出的落点候选参与定案——三条不可回退的边界各一条用例。"""

    def _run_player_move(self, content, allow_pass=False):
        agent, _eng, _llm, bus = _mk()
        agent.tools_enabled = True
        agent.chain = _ScriptChain([_Ai(content=content)])
        agent.st.allow_pass = bool(allow_pass)
        asyncio.run(agent.player_move(3, 3))
        return agent, bus

    def test_legal_candidate_is_used(self):
        agent, bus = self._run_player_move("我就下 D4 吧。")
        moves = bus.messages(events.AI_MOVE)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["move"]["gtp"], "D4")     # 模型候选合法 → 用它
        self.assertEqual((moves[0]["move"]["x"], moves[0]["move"]["y"]), (3, 15))
        self.assertFalse(moves[0]["move"]["pass"])
        self.assertEqual(agent.st.board.at(3, 15), "white")
        self.assertTrue(agent._reveal_pending)

    def test_pass_candidate_only_when_allowed(self):
        # 不允许虚着：pass 候选被拒 → 回退到引擎首选（Q16），这一手照下
        agent, bus = self._run_player_move("这一手我想虚着。", allow_pass=False)
        mv = bus.messages(events.AI_MOVE)[0]["move"]
        self.assertFalse(mv["pass"])
        self.assertEqual(mv["gtp"], "Q16")
        # 允许虚着：pass 候选被采纳
        agent2, bus2 = self._run_player_move("这一手我想虚着。", allow_pass=True)
        mv2 = bus2.messages(events.AI_MOVE)[0]["move"]
        self.assertTrue(mv2["pass"])
        self.assertEqual(mv2["gtp"], "pass")

    def test_bad_candidate_still_yields_a_usable_move(self):
        # D16 刚被玩家占了：候选非法 → 回退；ai_move 仍带合法坐标、揭示流程照走
        agent, bus = self._run_player_move("我下 D16。")
        mv = bus.messages(events.AI_MOVE)[0]["move"]
        self.assertFalse(mv["pass"])
        self.assertTrue(0 <= mv["x"] < 19 and 0 <= mv["y"] < 19, "必须产出棋盘内的合法落点")
        self.assertEqual(agent.st.board.at(mv["x"], mv["y"]), "white")
        self.assertTrue(agent._reveal_pending)

