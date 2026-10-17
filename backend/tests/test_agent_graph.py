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

    def test_oversubscribed_parallel_calls_all_get_a_tool_message(self):
        """I1：第 2 轮并行要 2 个工具、只剩 1 个额度 ⇒ 超额调用不执行，但**必须补 ToolMessage**。

        AIMessage 里每个 ``tool_call_id`` 都要有应答，否则紧接着的 ``tools → decide`` 那一轮模型往返
        会被 OpenAI 兼容端点判成 400（AI 消息带 2 个 tool_call_id、却只有 1 条 ToolMessage）。
        """
        from langchain_core.messages import ToolMessage
        from app.agent_graph import PHASE_OBSERVE
        agent, _eng, _llm, bus = _mk()
        agent.tools_enabled = True
        agent.chain = _ScriptChain([
            _Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}]),
            _Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c2"},
                            {"name": "board_query", "args": {}, "id": "c3"}]),
            _Ai(content="看完了，先稳一手。"),
        ])

        state = asyncio.run(agent._turn(PHASE_OBSERVE))

        # c1、c2 执行到上限；c3 超额，不执行但仍补预算 ToolMessage
        self.assertEqual(state["tool_calls"], agent.max_tool_calls)
        want_ids = set()
        have_ids = set()
        for m in state["messages"]:
            for tc in (getattr(m, "tool_calls", None) or []):
                want_ids.add(str(tc.get("id", "")))
            if isinstance(m, ToolMessage):
                have_ids.add(str(m.tool_call_id))
        self.assertEqual(want_ids, {"c1", "c2", "c3"})
        self.assertTrue(want_ids.issubset(have_ids), "有 tool_call_id 没被应答：%s" % (want_ids - have_ids))
        # 只有 2 个工具真被执行（超额那个不发 tool 事件）
        starts = [e for e in bus.of_type("tool") if e["phase"] == "start"]
        self.assertEqual(len(starts), agent.max_tool_calls)


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


# —— 以下为计划 07 · Task 3 追加（既有断言一个字符都没改；追加在文件末尾，是为了让上面的既有内容
#    保持逐字节不变——`unittest.main()` 在本仓的唯一跑法 `python -m unittest discover -s tests` 下
#    不会执行）——


class JosekiMistakeRuleTest(unittest.TestCase):
    """「走错」= 真亏 **且** 不在库线上：计划 Step 1 给的四条纯函数用例，原样保留。"""

    def test_on_library_line_is_never_a_mistake(self):
        from app.agent_graph import is_joseki_mistake
        self.assertFalse(is_joseki_mistake({"kind": "deviated"}, True, 0.5, False))

    def test_off_line_without_loss_is_not_a_mistake(self):
        from app.agent_graph import is_joseki_mistake
        self.assertFalse(is_joseki_mistake({"kind": "deviated"}, False, 0.01, True))

    def test_off_line_and_losing_is_a_mistake(self):
        from app.agent_graph import is_joseki_mistake
        self.assertTrue(is_joseki_mistake({"kind": "deviated"}, False, 0.12, True))

    def test_formed_is_never_a_mistake(self):
        from app.agent_graph import is_joseki_mistake
        self.assertFalse(is_joseki_mistake({"kind": "formed"}, False, 0.5, False))


class _LosingEngine(StubEngine):
    """接线用例的引擎替身：按**手数**定死胜率，好造出「胜率掉 > 10%」的那半边。

    默认 `StubEngine` 恒返回 0.5，落子前后同值 ⇒ `drop` 永远是 0，构造不出真亏的局面；这里
    只多给一个 `winrates` 表（键＝分析时的总手数）。`CAND` 把玩家那一手排第一，让 `player_rank`
    ＝0（`rank_ok=True`），于是「判成走错」靠的确实是 `drop > 0.10` 这一条，而不是排名那一条。
    """

    CAND = ["B15"] + StubEngine.CAND

    def __init__(self, winrates=None):
        super().__init__()
        self.winrates = dict(winrates or {4: 0.50, 5: 0.70})   # 落子前 50%；落子后白方视角 70% ⇒ 黑方掉 20%

    async def analyze(self, moves):
        self.calls.append([list(m) for m in moves])
        wr = float(self.winrates.get(len(moves), 0.5))
        return {
            "rootInfo": {"winrate": wr, "scoreLead": 0.0},
            "moveInfos": [{"move": m, "visits": 1} for m in self.CAND],
            "ownership": [0.0] * 361,
        }


def _seed_moves(agent, seq):
    """把一串 ``[color, gtp]`` 直接铺进 stub 局面的棋盘与棋谱。

    省掉真下棋的多阶段编排（那会让她在中间插进自己的应手，角上的手顺就不再是库里那条）；
    识别器只看 ``st.moves`` 的手顺与颜色，棋盘同步铺好是为了让快照 / 形势文本自洽。
    """
    from app import coords
    for color, gtp in seq:
        x, y = coords.gtp_to_xy(gtp)
        agent.st.board.play(x, y, "black" if color == "B" else "white")
        agent.st.add_move(color, gtp)


# 左上角已按库线吻合 4 手（星位点三三基本型；golden joseki.json 的 deviated 局面就是它的第 5 手）
_DEVIATED_BASE = [["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]]


class JosekiMistakeWiringTest(unittest.TestCase):
    """T3 接线：判定结论落进**本轮** ``data["jk_mistake"]``，且既有俗手路径一字不变（U19）。"""

    def _after_player(self, agent, x, y):
        """只跑 ``after_player`` 这一个阶段（与 ``player_move`` 的公开路径同一入口）。"""
        from app.agent_graph import PHASE_AFTER_PLAYER
        agent._new_turn()
        return asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))

    def test_deviated_and_losing_lands_as_mistake_and_keeps_bad_move(self):
        agent, _eng, _llm, bus = _mk()
        agent.katago = _LosingEngine()
        # 角上已按库线吻合 4 手，玩家再落 B15 偏离库线（golden joseki.json 的 deviated 局面）
        _seed_moves(agent, _DEVIATED_BASE)
        state = self._after_player(agent, 1, 4)

        self.assertEqual(str(state["data"]["jk"]["kind"]), "deviated")
        got = state["data"]["jk_mistake"]
        self.assertEqual(
            set(got.keys()), {"is_mistake", "on_line", "gtp", "x", "y", "drop", "rank_ok"})
        self.assertIs(got["is_mistake"], True)                  # 偏离 + 真亏 ⇒ 走错
        self.assertIs(got["on_line"], False)                    # 库线上的那一手是 (5,2)，不是这手
        self.assertEqual(got["gtp"], "B15")
        self.assertEqual((got["x"], got["y"]), (1, 4))
        self.assertGreater(got["drop"], 0.10)
        self.assertIs(got["rank_ok"], True)

        # U19：既有俗手记录照旧触发，条目形状 / 文案 / 位置一字不变（走的就是 drop 那半边）
        self.assertEqual(agent.st.bad_moves, [{
            "turn_index": 0, "move_no": 5, "gtp": "B15", "reason": "胜率下滑20%",
        }])
        self.assertEqual(bus.of_type("state")[-1]["bad_moves"], agent.st.bad_moves)

    def test_formed_is_not_a_mistake(self):
        """收尾（formed）不吃误判：这一课走完就不判「走错」，哪怕排名不足。

        手顺用的是 golden ``basic_formed`` 那四手（左上星位点三三基本型）——玩家执黑、最后一手
        必须是他自己下的，故取其**换色映射**（golden 的 ``kobayashi_formed`` 正是这一支，结果与
        ``basic_formed`` 逐字段相同）；下面同时钉住 four points，确保比对的确实是那一课。
        """
        agent, _eng, _llm, _bus = _mk()
        _seed_moves(agent, [["W", "D16"], ["B", "C17"], ["W", "C16"]])
        state = self._after_player(agent, 3, 2)                 # 玩家落 D17，恰好走完基本型

        self.assertEqual(str(state["data"]["jk"]["kind"]), "formed")
        self.assertEqual(
            state["data"]["jk"]["points"], [(3, 3), (2, 2), (2, 3), (3, 2)])
        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], False)
        self.assertIs(state["data"]["jk_mistake"]["on_line"], False)

    # —— 修复轮 1（裁决 U21）：护栏 D「照书下不许被骂」——

    def test_following_another_library_line_is_not_a_mistake(self):
        """U21 第一款（护栏 D 端到端）：照**另一条**库线走，不许被说成走错。

        手顺是「基本型 4 手 + 定形二的第 5 手 E16(4,3)」。这一手**不是** deviated——识别器会把它
        读成 k=5 的「正跟着书走的中段」而按设计沉默，所以「没有 deviated」这条断言我用的是**识别器
        口径**：另立一个同局面的 stub agent 调 ``_joseki_check()`` 断言返回 ``{}``，并同时断言本回合
        没有任何 ``kind == "deviated"`` 的 ``joseki`` 事件。

        引擎特意用会掉胜率的那个替身（drop=0.20、排名也在前 5 之外）：这场合**单按俗手口径必判坏手**，
        所以下面那个 ``is_mistake is False`` 不是「什么都没发生」换来的。
        """
        agent, _eng, _llm, bus = _mk()
        agent.katago = _LosingEngine()
        _seed_moves(agent, _DEVIATED_BASE)
        state = self._after_player(agent, 4, 3)                 # 玩家落 E16＝定形二在该格的下一手

        # ① 识别器口径（另立一个同局面：`_joseki_check()` 会写去重日志，不能拿本回合那个 agent 问）
        probe, _e2, _l2, _b2 = _mk()
        _seed_moves(probe, _DEVIATED_BASE)
        probe.st.board.play(4, 3, "black")
        probe.st.add_move("B", "E16")
        self.assertEqual(probe._joseki_check(), {})              # 中段推进 ⇒ 沉默，不判 deviated
        # ② 端到端：本回合没有 deviated 的 joseki 事件
        self.assertEqual(
            [e for e in bus.of_type("joseki") if str(e.get("kind", "")) == "deviated"], [])
        # ③ 结论：照书下（另一本）⇒ 不是走错（尽管这一手真亏、排名也在前 5 之外）
        got = state["data"]["jk_mistake"]
        self.assertGreater(got["drop"], 0.10)
        self.assertIs(got["rank_ok"], False)
        self.assertIs(got["is_mistake"], False)

    def test_guard_d_covers_the_other_library_line_at_a_fork(self):
        """U21 第二款（护栏 D 的**候选**分支）：玩家这手若正是候选线上那一手，也不算走错。

        现在 ``scan()`` 产不出「deviated + 这手落在候选上」这种组合（见上一条：那种手顺被读成中段
        推进而沉默）——所以这里**按识别器的形状合成一个 deviated 结果**（``line`` / ``k`` / ``candidates``
        齐全，坐标照 golden 的 deviated 局面）把这一款钉成活代码；扩库（T11a）之后它就会真的被走到。
        """
        agent, _eng, _llm, _bus = _mk()
        agent.katago = _LosingEngine()                          # drop = 0.20：单按俗手口径必然判错
        _seed_moves(agent, _DEVIATED_BASE)

        full = [{"x": 3, "y": 3, "c": "B"}, {"x": 2, "y": 2, "c": "W"},
                {"x": 2, "y": 3, "c": "B"}, {"x": 3, "y": 2, "c": "W"},
                {"x": 5, "y": 2, "c": "B"}, {"x": 4, "y": 2, "c": "W"},
                {"x": 4, "y": 3, "c": "B"}, {"x": 5, "y": 1, "c": "W"}]     # = 定形一（golden 的 line）
        other = full[:4] + [{"x": 4, "y": 3, "c": "B"}, {"x": 5, "y": 2, "c": "W"}]   # = 定形二
        agent._joseki_check = lambda: {
            "kind": "deviated", "k": 4, "corner": "左上", "cat": "star",
            "name": "点三三·定形", "family": "star_33", "key": "（合成）",
            "line": full, "expect": (5, 2),
            "candidates": [{"name": "点三三·定形", "family": "star_33",
                            "next": (4, 3), "line": other}],
        }
        state = self._after_player(agent, 4, 3)                  # 玩家落 E16＝候选线的下一手

        got = state["data"]["jk_mistake"]
        self.assertIs(got["on_line"], True)                      # 照书下（另一本）：候选那一手
        self.assertIs(got["is_mistake"], False)                  # ⇒ 不算走错
        self.assertGreater(got["drop"], 0.10)                    # 这一手确实「真亏」：判 False 靠的正是护栏

