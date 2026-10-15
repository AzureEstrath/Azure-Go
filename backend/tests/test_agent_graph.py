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
    """拓扑守卫：本任务唯一的实质产出是「四条无条件边」，必须真的锁住。

    `GraphShapeTest` 只比对了 `NODE_NAMES`（常量自比）和「有没有 `ainvoke`」（任何 `compile()`
    结果都为真），`build_graph` 漏接一条边、接错方向、或偷偷引入条件边，它都照样绿。
    这里直接读 langgraph 编译后的图：节点集合 + **边集合**（一条不多一条不少）+ 每条边都非条件边。
    """

    def test_topology_is_the_unconditional_chain(self):
        from app.agent_graph import build_graph
        agent, _eng, _llm, _bus = _mk()
        g = build_graph(agent).get_graph()

        # 节点：langgraph 1.x 会把哨兵节点 __start__ / __end__ 一并列出来
        nodes = {str(n) for n in g.nodes}
        self.assertEqual(nodes, {"__start__", "prepare", "decide", "speak", "finalize", "__end__"})

        # 边：恰好是 START→prepare→decide→speak→finalize→END，一条不多一条不少
        edges = {(str(e.source), str(e.target)) for e in g.edges}
        self.assertEqual(edges, {
            ("__start__", "prepare"),
            ("prepare", "decide"),
            ("decide", "speak"),
            ("speak", "finalize"),
            ("finalize", "__end__"),
        })

        # 本任务的硬约束：拓扑等价 ⇒ 一条条件边都不许有
        for e in g.edges:
            self.assertFalse(e.conditional, "边 %s→%s 是条件边（本任务只允许无条件边）" % (e.source, e.target))


if __name__ == "__main__":
    unittest.main()
