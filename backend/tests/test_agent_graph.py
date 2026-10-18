"""状态图骨架：拓扑存在、单路径等价、异常不吞 busy。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from helpers import FixedRng, PvStubEngine
from test_agent import ErrLLM, StubEngine, StubLLM, ZeroRng, _mk


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


class _CountingRng:
    """数「掷了几次」的随机源（v3.8.0 修复波 / Minor #3）：``random()`` 恒 ``0.0``（落在 B 段）并计数。"""

    def __init__(self):
        self.calls = 0

    def random(self):
        self.calls += 1
        return 0.0


class _DroppingEngine(StubEngine):
    """玩家落子**之后**胜率明显下滑的假引擎（黑方视角 0.5 → 0.2 ⇒ ``drop`` 0.3 > 0.10）。

    只改「玩家落子后局面」那一次（``len(moves) == 1``）的 ``rootInfo``：KataGo 的 ``rootInfo`` 是
    **行棋方（白）**视角，1 手时 ``to_black_perspective`` 会翻成 ``1 - 0.8 = 0.2``。
    """

    async def analyze(self, moves, max_visits=None):
        out = await super().analyze(moves, max_visits)
        if len(moves) == 1:
            out["rootInfo"] = {"winrate": 0.8, "scoreLead": 0.0}
        return out


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

    async def analyze(self, moves, max_visits=None):
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
            set(got.keys()),
            {"is_mistake", "on_line", "gtp", "x", "y", "drop", "rank_ok", "move_no"})
        self.assertIs(got["is_mistake"], True)                  # 偏离 + 真亏 ⇒ 走错
        self.assertIs(got["on_line"], False)                    # 库线上的那一手是 (5,2)，不是这手
        self.assertEqual(got["gtp"], "B15")
        self.assertEqual((got["x"], got["y"]), (1, 4))
        self.assertGreater(got["drop"], 0.10)
        self.assertIs(got["rank_ok"], True)
        self.assertEqual(got["move_no"], 5)                     # T6：4 手铺好 + 玩家这手 ＝ 第 5 手

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


# —— 以下为计划 07 · Task 7 追加（既有断言一个字符都没改；追加在文件末尾，理由同上）——
#
# T7：结构点开口（进入 / 分岔 / 收尾）并进**既有发言位**。台词分流**只看 `kind`**（+ 走错时看
# `is_mistake`，U34 修订版）：进入顶替玩家那一手的点评、分岔与走错并进同一格、收尾并进**走完那一方**
# 的发言位；中间吻合的推进一个字都不提（识别器自己沉默）。`data["jk_show_card"]` 是**卡片**的次数
# 规则（T5/T6/T8），管的是卡片字段，不管说不说——讲解是**顶替**那条本来就要发的台词，不多气泡、
# 不多调一次模型。

import tempfile                                                # noqa: E402

from app import coords, events, prompts                         # noqa: E402
from app.agent import AzureAgent                               # noqa: E402
from app.agent import LEN_NOTE, LEN_TEACH_NOTE                 # noqa: E402
from app.agent_graph import (MAX_LLM_CALLS_PER_TURN, PHASE_AFTER_PLAYER,   # noqa: E402
                             PHASE_JUST_MOVED, PHASE_OWN_TURN)
from app.joseki import JosekiData                              # noqa: E402
from app.joseki_progress import JosekiProgress                 # noqa: E402


class _FreshLLM(StubLLM):
    """每次回一句新话：免得原话去重把后面几条气泡吃掉，「这一回合几条台词」就数不准。"""

    def __init__(self):
        super().__init__()
        self.n = 0

    async def chat(self, messages, max_tokens=300, no_think=False):
        self.n += 1
        self.text = "第%d句话。" % self.n
        return await super().chat(messages, max_tokens, no_think)


def _user_texts(llm):
    """模型收到的**用户消息**文本（含情绪小调用那类旁路）。"""
    out = []
    for msgs, _mx, _nt in llm.calls:
        for m in msgs:
            if str(m.get("role", "")) == "user":
                out.append(str(m.get("content", "")))
    return out


def _phase_texts(llm):
    """只挑**阶段提示词**那一份：经 `with_context` 装配的才带「【现在】」（情绪小调用之类不带）。

    这样「这一回合到底出过几条编排提示」不受限频的情绪小调用影响。
    """
    return [t for t in _user_texts(llm) if "【现在】" in t]


class JosekiActiveSpeechTest(unittest.TestCase):
    """T7 接线：进入 / 分岔 / 走错 / 收尾 四处开口 + 中段沉默 + 闸门 + 她那一格 + 不新增台词。"""

    # 角内先铺 1 手（白小目 D17），玩家再落 F17(5,2)＝一间低挂 ⇒ k == len(seq) == 2 且无分岔 ⇒ entered
    # （T11 修复轮 U49：点三三补了「挡另一边」之后，星位＋点三三那个局面已经是真分岔，故换到这里；
    #  小目一间低挂只有一条 3 手线，2 手时不会分岔）
    ENTER_SEED = [["W", "D17"]]
    # 中段推进：角内 4 手吻合核心手顺后，玩家落 F17(5,2)＝更长那条定式的第 5 手（黑）
    # ⇒ k == len(seq) == 5、没有候选 ⇒ 识别器沉默（`jk == {}`），一个字都不该提定式
    MID_SEED = [["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]]
    # 她走完定式、而玩家那一手没结果时用的**合成**识别结果（识别器形状，见 `_joseki.py`）
    FORM = {"kind": "formed", "family": "star_33", "cat": "star", "name": "点三三·基本型",
            "key": "（要点）", "corner": "左上", "k": 4, "line": [], "candidates": [],
            "points": [(15, 3)]}

    def _after_player(self, agent, x, y):
        """只跑 `after_player` 那一个阶段（与 `player_move` 的公开路径同一入口）。"""
        agent._new_turn()
        return asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))

    def _own_turn_formed(self):
        """「玩家那手没结果、她那一步走完定式」：结论只在**她那一格**出的整条路。

        第 1 次 `_joseki_check()` 是玩家那手（返回空 = 没结果），第 2 次是她那一步（返回 formed）。
        返回 (agent, llm, calls, own_turn 终态, just_moved 终态)。
        """
        llm = _FreshLLM()
        # 必定讲（rate=1.0 + ZeroRng：画像 / 情绪的负浮动也压不过）：本用例钉的是「她那一步的
        # 模板与事件」，不是频率层
        agent = AzureAgent(StubEngine(), llm, events.RecordingBus(),
                           joseki_rate=1.0, joseki_rng=ZeroRng())
        calls = []

        def fake():
            calls.append(1)
            return dict(self.FORM) if len(calls) > 1 else {}

        agent._joseki_check = fake
        state = self._after_player(agent, 3, 3)
        own = asyncio.run(agent._turn(PHASE_OWN_TURN, state["data"], state["turn_id"]))
        after = asyncio.run(agent._turn(PHASE_JUST_MOVED, own["data"], own["turn_id"]))
        return agent, llm, calls, own, after

    # —— ① 进入：**顶替**这一手的普通点评（同一时刻只出一条气泡）——

    def test_enter_replaces_the_comment(self):
        agent, _eng, llm, bus = _mk()
        _seed_moves(agent, self.ENTER_SEED)
        state = self._after_player(agent, 5, 2)

        jk = state["data"]["jk"]
        self.assertEqual(str(jk["kind"]), "entered")
        self.assertIs(state["data"]["jk_show_card"], True)

        text = _phase_texts(llm)[0]
        self.assertIn(prompts.joseki_enter_prompt(
            str(jk["name"]), str(jk["key"]), str(jk["corner"])), text)
        # 顶替：普通点评那几档与旧的整段讲解一个字都不在
        self.assertNotIn("用你自己的口吻评", text)
        for marker in ("这是我算的最优手", "不在前5", "没进我算的前5"):
            self.assertNotIn(marker, text)
        self.assertNotIn(prompts.joseki_teach_prompt(
            str(jk["name"]), str(jk["key"]), str(jk["corner"])), text)
        self.assertEqual(len(bus.messages()), 1, "进入那一手只发一条台词")

    # —— ② 分岔 / 走错：并进这一手的发言位，候选的下一手坐标当素材 ——

    def test_branch_dispatch_gives_each_candidate_its_next_move(self):
        """这里的库已经有真分岔了（见 test_joseki 的 fork 用例），但本用例要钉的是**分流模板**，
        所以仍按识别器形状合成一个 `branch` 结果（与 T2 的 `test_branch_has_its_own_key` 同一手法）。"""
        agent, _eng, llm, _bus = _mk()
        result = {"kind": "branch", "family": "star_kakari_small", "cat": "star", "corner": "左上",
                  "name": "小飞挂·靠压定式", "key": "（要点）", "k": 4, "side": "player",
                  "points": [(15, 3)], "line": [],
                  "candidates": [{"name": "小飞挂·靠压定形", "family": "star_kakari_small",
                                  "next": (4, 2), "line": []},
                                 {"name": "小飞挂·靠压定形", "family": "star_kakari_small",
                                  "next": (5, 3), "line": []}]}
        agent._joseki_check = lambda: dict(result)
        _seed_moves(agent, self.MID_SEED)
        state = self._after_player(agent, 5, 2)

        self.assertEqual(str(state["data"]["jk"]["kind"]), "branch")
        self.assertIs(state["data"]["jk_show_card"], True)
        text = _phase_texts(llm)[0]
        self.assertIn(prompts.joseki_branch_prompt(
            "小飞挂·靠压定式", "（要点）", result["candidates"]), text)
        self.assertNotIn("用你自己的口吻评", text)

    def test_mistake_uses_the_branch_template(self):
        """走错（真亏 **且** 偏离库线）：这一手也走「这里有别的走法」那一套，末尾不再轻提。"""
        agent, _eng, llm, _bus = _mk()
        agent.katago = _LosingEngine()
        _seed_moves(agent, _DEVIATED_BASE)
        state = self._after_player(agent, 1, 4)                  # B15：真亏且偏出库线

        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], True)
        self.assertIs(state["data"]["jk_show_card"], True)
        jk = state["data"]["jk"]
        text = _phase_texts(llm)[0]
        self.assertIn(prompts.joseki_branch_prompt(
            str(jk["name"]), str(jk["key"]), list(jk.get("candidates") or [])), text)
        self.assertNotIn("用你自己的口吻评", text)
        self.assertNotIn(prompts.joseki_hint_clause(
            str(jk["name"]), coords.xy_to_gtp(jk["expect"][0], jk["expect"][1])), text)

    def test_deviated_without_loss_keeps_the_plain_comment_and_hint_clause(self):
        """偏离但没亏**不是**走错：原样普通点评 + 末尾那句轻提（U35 表里的「其余」一路）。"""
        class _FlatEngine(StubEngine):
            CAND = ["B15"] + StubEngine.CAND        # 排名进前 5、胜率不掉 ⇒ 不算真亏

        agent, _eng, llm, _bus = _mk()
        agent.katago = _FlatEngine()
        _seed_moves(agent, _DEVIATED_BASE)
        state = self._after_player(agent, 1, 4)

        self.assertEqual(str(state["data"]["jk"]["kind"]), "deviated")
        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], False)
        self.assertIs(state["data"]["jk_show_card"], True, "第一次见到这一族：仍然入围")
        jk = state["data"]["jk"]
        text = _phase_texts(llm)[0]
        self.assertIn("用你自己的口吻评", text)
        self.assertIn(prompts.joseki_hint_clause(
            str(jk["name"]), coords.xy_to_gtp(jk["expect"][0], jk["expect"][1])), text)
        self.assertNotIn("不要提胜率数值", text)

    # —— ③ 收尾：并进**走完那一方**的发言位 ——

    def test_close_follows_the_side_that_finished(self):
        # ① 玩家走完：角内 3 手吻合后，他落 D17(3,2) 收尾 ⇒ after_player 那条点评
        agent, _eng, llm, _bus = _mk()
        _seed_moves(agent, [["W", "D16"], ["B", "C17"], ["W", "C16"]])
        state = self._after_player(agent, 3, 2)
        jk = state["data"]["jk"]
        self.assertEqual(str(jk["kind"]), "formed")
        self.assertIn(prompts.joseki_close_prompt(
            str(jk["name"]), str(jk["key"]), str(jk["corner"]), "player"), _phase_texts(llm)[0])
        self.assertNotIn(prompts.joseki_short_prompt(
            str(jk["name"]), str(jk["key"]), "D17"), _phase_texts(llm)[0])

        # ② 她走完：玩家那手没结果 ⇒ 入围在她那一格补齐，她落子后的感想说 azure 那一版
        agent2, llm2, calls, _own, _after = self._own_turn_formed()
        hers = _phase_texts(llm2)[-1]                    # 最后一条阶段提示＝她那句感想
        self.assertIn(prompts.joseki_close_prompt(
            "点三三·基本型", "（要点）", "左上", "azure"), hers)
        self.assertNotIn(prompts.joseki_short_prompt("点三三·基本型", "（要点）", "Q16"), hers)
        self.assertNotIn(prompts.joseki_close_prompt(
            "点三三·基本型", "（要点）", "左上", "player"), hers)
        self.assertEqual(len(calls), 2, "她那一步只许识别一次")

    # —— ④ 中段推进：一个字都不提定式（靠识别器沉默，不靠提示词兜）——

    def test_middle_moves_do_not_mention_joseki(self):
        agent, _eng, llm, bus = _mk()
        _seed_moves(agent, self.MID_SEED)
        state = self._after_player(agent, 5, 2)                  # F17＝更长那条线的第 5 手

        self.assertEqual(state["data"]["jk"], {}, "中段推进：识别器必须沉默")
        self.assertEqual(bus.of_type("joseki"), [])
        text = _phase_texts(llm)[0]
        self.assertIn("用你自己的口吻评", text)                     # 走的是普通点评
        for e in JosekiData.LIST:                                # 定式名一个字都不出现
            self.assertNotIn(str(e["name"]), text)
        self.assertNotIn("不要提胜率数值", text)                    # 三条模板的文案也不出现

    # —— ⑤ T5（v3.7.0）口径修订：**资格层 + 频率层**决定讲不讲，卡片与讲解同一件事 ——

    def test_fourth_entry_is_not_taught_after_the_family_is_learned(self):
        """这一族已经教过 3 次 ⇒ 资格层不再放行（既没走错、也过了「前 3 次」）。

        v3.7.0 口径修订（T5）：`data["jk_show_card"]` 不再是「只管卡片」的次数规则，而是**门控**
        的结论——未命中与「没识别到定式」逐字段同构：普通点评、不上卡片、不发 `joseki` 事件。
        """
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        progress = JosekiProgress(os.path.join(d.name, "joseki_progress.db"))
        for _ in range(3):
            progress.bump("komoku_low_kakari")                   # 前 3 次已经教过
        llm = StubLLM()
        agent = AzureAgent(StubEngine(), llm, events.RecordingBus(), joseki_progress=progress)
        _seed_moves(agent, self.ENTER_SEED)
        state = self._after_player(agent, 5, 2)

        jk = state["data"]["jk"]
        self.assertEqual(str(jk["kind"]), "entered", "识别结果仍在，只是不讲")
        self.assertIs(state["data"]["jk_show_card"], False, "第 4 次进入：门控不放行")
        self.assertEqual(agent.bus.of_type("joseki"), [], "未命中：不发 joseki 事件")
        text = _phase_texts(llm)[0]
        self.assertIn("用你自己的口吻评", text)                     # 未命中＝普通点评
        self.assertNotIn(prompts.joseki_enter_prompt(
            str(jk["name"]), str(jk["key"]), str(jk["corner"])), text)

    # —— ⑥ 她那一步：`jk2` 被算出来、卡片结论被补齐，事件照发 ——

    def test_own_turn_slot_computes_jk2_fills_the_gate_and_emits(self):
        _agent, _llm, calls, own, after = self._own_turn_formed()

        self.assertEqual(str(own["data"]["jk2"]["kind"]), "formed")
        self.assertIs(own["data"]["jk_show_card"], True, "after_player 没结论 ⇒ 在她那一格补齐")
        self.assertEqual(len(calls), 2, "`_joseki_check()` 只调一次（第二次会吃掉去重键）")
        # 事件照发：她那一手的 joseki 轨迹仍在 `just_moved` 的 finalize 里、带卡片载荷
        event = _agent.bus.of_type("joseki")[-1]
        self.assertEqual(str(event["kind"]), "formed")
        self.assertIs(event["show_card"], True)
        self.assertEqual(after["data"]["jk2"], own["data"]["jk2"])

    def test_just_moved_driven_alone_still_gets_her_verdict(self):
        """直接驱动 `just_moved`（本回合没跑过 `own_turn`）：兜底也要在她那句之前算出结论。

        用的是真识别结果（角内 4 手走完基本型、最后一手是她下的）——兜底若落在挑模板**之后**，
        这条就会红：她这句会退回普通感想。
        """
        agent = AzureAgent(StubEngine(), _FreshLLM(), events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
        _seed_moves(agent, [["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]])
        agent._new_turn()
        state = asyncio.run(agent._turn(PHASE_JUST_MOVED, {}))

        jk2 = state["data"]["jk2"]
        self.assertEqual(str(jk2["kind"]), "formed")
        self.assertIs(state["data"]["jk_show_card"], True)
        self.assertIn(prompts.joseki_close_prompt(
            str(jk2["name"]), str(jk2["key"]), str(jk2["corner"]), "azure"),
            _phase_texts(agent.llm)[-1])
        self.assertIs(agent.bus.of_type("joseki")[-1]["show_card"], True)

    def test_her_close_is_not_taught_after_the_family_is_learned(self):
        """她那一步同理（v3.7.0 口径修订 T5）：这一族已教过 3 次 ⇒ 门控不放行，退回普通感想。

        玩家那手没出定式结果、这一族又已经教过 3 次 ⇒ `jk_show_card` 在她那一格才定下来、且为假；
        她那句是**普通感想**，既不是 `joseki_close_prompt(..., "azure")`、也不发 `joseki` 事件。
        """
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        progress = JosekiProgress(os.path.join(d.name, "joseki_progress.db"))
        for _ in range(3):
            progress.bump("star_33")
        llm = _FreshLLM()
        agent = AzureAgent(StubEngine(), llm, events.RecordingBus(), joseki_progress=progress)
        calls = []

        def fake():
            calls.append(1)
            return dict(self.FORM) if len(calls) > 1 else {}     # 玩家那手没结果；她那一步成型

        agent._joseki_check = fake
        state = self._after_player(agent, 3, 3)
        self.assertNotIn("jk_show_card", state["data"], "玩家那手没出定式结果")
        state = asyncio.run(agent._turn(PHASE_OWN_TURN, state["data"], state["turn_id"]))
        self.assertIs(state["data"]["jk_show_card"], False, "这一族已教过 3 次：门控不放行")
        ai_gtp = str(state["data"]["move"]["gtp"])
        state = asyncio.run(agent._turn(PHASE_JUST_MOVED, state["data"], state["turn_id"]))

        text = _phase_texts(llm)[-1]
        self.assertNotIn(prompts.joseki_close_prompt("点三三·基本型", "（要点）", "左上", "azure"), text)
        self.assertNotIn(prompts.joseki_short_prompt("点三三·基本型", "（要点）", ai_gtp), text)
        self.assertIn("换一句新话", text, "退回普通感想")
        self.assertEqual(
            [e for e in agent.bus.of_type("joseki") if str(e.get("kind", "")) == "formed"], [],
            "未命中：她那一步也不发 joseki 事件")

    # —— ⑦ 不新增台词 / 不多调一次模型 ——

    def test_entered_turn_still_has_exactly_three_lines(self):
        agent = AzureAgent(StubEngine(), _FreshLLM(), events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
        _seed_moves(agent, self.ENTER_SEED)
        asyncio.run(agent.player_move(5, 2))

        msgs = agent.bus.messages()
        self.assertEqual(len(msgs), 3, "点评/讲解 + 落子宣告 + 落子后感想：一条都不许多")
        kinds = [str(e.get("kind", "")) for e in msgs]
        self.assertEqual(kinds.count(events.AI_COMMENT), 2)
        self.assertEqual(kinds.count(events.AI_MOVE), 1)

    def test_llm_calls_stay_within_budget(self):
        agent = AzureAgent(StubEngine(), _FreshLLM(), events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
        _seed_moves(agent, self.ENTER_SEED)
        asyncio.run(agent.player_move(5, 2))
        self.assertEqual(len(_phase_texts(agent.llm)), 3, "本任务只换提示词：一回合仍恰好三次")

        # 开了工具的那条路照旧受硬上限管（本任务不动预算、也不加调用）
        llm2 = _FreshLLM()
        agent2 = AzureAgent(StubEngine(), llm2, events.RecordingBus(), tools_enabled=True,
                            joseki_rate=1.0, joseki_rng=ZeroRng())
        chain = _ScriptChain([_Ai(content="嗯~ 这手我想想。")])
        agent2.chain = chain
        _seed_moves(agent2, self.ENTER_SEED)
        state = self._after_player(agent2, 5, 2)
        state = asyncio.run(agent2._turn(PHASE_OWN_TURN, state["data"], state["turn_id"]))
        self.assertEqual(int(state["data"]["llm_rounds"]), 1)
        self.assertLessEqual(int(state["data"]["llm_rounds"]), MAX_LLM_CALLS_PER_TURN)
        self.assertEqual(len(chain.seen), 1, "工具轮只走一轮")
        self.assertEqual(len(_phase_texts(llm2)), 1, "她那一步的用意来自工具轮，不走 llm.chat")

    def test_own_turn_gate_does_not_add_a_line(self):
        """她那一步的卡片结论补齐只写 `data`、**不发**事件，也不多出一条台词（发话点仍只在 finalize）。"""
        agent, _llm, _calls, _own, after = self._own_turn_formed()
        self.assertEqual(len(agent.bus.messages()), 3, "点评 + 落子宣告 + 她的感想")
        self.assertEqual(str(after["data"]["reply"]), "第3句话。")


# —— 以下为阶段一终审修复波（F1 / F2）追加（既有断言一个字符都没改；追加在文件末尾）——


class _HerJosekiFixture(unittest.TestCase):
    """F1/F2 的共用夹具：玩家那手与她那一手都想出定式结果时，驱动完整的三阶段回合。"""

    # 她那一手的合成识别结果（族与玩家那手不同，便于断言「用的是她的族」）
    HER_ENTER = {"kind": "entered", "family": "komoku_low_kakari", "cat": "komoku",
                 "name": "小目·一间低挂", "key": "（要点）", "corner": "右下", "k": 2,
                 "line": [{"x": 3, "y": 15, "c": "B"}, {"x": 5, "y": 15, "c": "W"}],
                 "candidates": [], "points": [(3, 15), (5, 15)]}
    HER_BRANCH = {"kind": "branch", "family": "star_kakari_small", "cat": "star",
                  "name": "小飞挂·靠压定式", "key": "（要点）", "corner": "左上", "k": 2, "line": [],
                  "candidates": [{"name": "小飞挂·靠压定形", "family": "star_kakari_small",
                                  "next": (4, 2), "line": []}],
                  "points": [(15, 3)]}

    def _drive(self, agent, x, y):
        """``after_player → own_turn → just_moved`` 三个公开阶段依次跑完（与 ``player_move`` 同入口）。"""
        agent._new_turn()
        state = asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))
        own = asyncio.run(agent._turn(PHASE_OWN_TURN, state["data"], state["turn_id"]))
        after = asyncio.run(agent._turn(PHASE_JUST_MOVED, own["data"], own["turn_id"]))
        return state, own, after

    def _script_her(self, agent, her, real):
        """第一次 ``_joseki_check()`` 走真识别（玩家那手），第二次起返回她的合成结果。"""
        calls = []

        def fake():
            calls.append(1)
            return real() if len(calls) == 1 else dict(her)

        agent._joseki_check = fake
        return calls


class HerOwnJosekiVerdictTest(_HerJosekiFixture):
    """F1 回归：她那一手的 ``joseki`` 事件用自己的 ``jk2`` 结论，绝不照搬玩家那手的 mistake / show_card。"""

    def test_her_event_never_carries_the_players_mistake(self):
        agent, _eng, _llm, bus = _mk()
        agent.katago = _LosingEngine()               # 玩家那手真亏 ⇒ 真走错
        _seed_moves(agent, _DEVIATED_BASE)           # 左上 star_33 已吻合 4 手
        her = dict(self.HER_ENTER, family="star_kakari_big")     # 第一次见的一族（与玩家那手不同）
        calls = self._script_her(agent, her, agent._joseki_check)
        state, _own, after = self._drive(agent, 1, 4)

        self.assertIs(state["data"]["jk_mistake"]["is_mistake"], True)
        self.assertIs(state["data"]["jk_show_card"], True)        # 玩家那一手自己的结论
        self.assertEqual(len(calls), 2, "她那一步只许识别一次")
        her_event = bus.of_type("joseki")[-1]
        self.assertEqual(str(her_event["family"]), "star_kakari_big", "事件是她的族")
        self.assertIsNone(her_event["mistake"], "她那手的事件不得带上玩家那手的走错标记（F1）")
        self.assertIs(her_event["show_card"], True)
        self.assertIs(after["data"]["jk_show_card"], True)

    def test_her_card_verdict_comes_from_her_own_family(self):
        """玩家那一族已教过 3 次（他这一手不上卡片），她那一手是另一族、第一次见 ⇒ 她上卡片。"""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        progress = JosekiProgress(os.path.join(d.name, "joseki_progress.db"))
        for _ in range(3):
            progress.bump("komoku_low_kakari")                   # 玩家那一族：已教 3 次
        agent = AzureAgent(StubEngine(), _FreshLLM(), events.RecordingBus(),
                           joseki_progress=progress, joseki_rate=1.0, joseki_rng=ZeroRng())
        _seed_moves(agent, [["W", "D17"]])                       # 左上小目；玩家落 F17(5,2) ⇒ entered
        her = dict(self.HER_ENTER, family="star_kakari_big")     # 她那一族：第一次见
        calls = self._script_her(agent, her, agent._joseki_check)
        state, own, _after = self._drive(agent, 5, 2)

        self.assertEqual(str(state["data"]["jk"]["kind"]), "entered")
        self.assertEqual(str(state["data"]["jk"]["family"]), "komoku_low_kakari")
        self.assertIs(state["data"]["jk_show_card"], False, "玩家那一族已教 3 次：他这一手不上卡片")
        self.assertIs(own["data"]["jk_show_card"], True, "她那手第一次见 ⇒ 用**她的**族重算后上卡片")
        her_event = agent.bus.of_type("joseki")[-1]
        self.assertEqual(str(her_event["family"]), "star_kakari_big")
        self.assertIs(her_event["show_card"], True)
        self.assertIsNone(her_event["mistake"])
        self.assertEqual(len(calls), 2)


class HerStructureSpeechTest(_HerJosekiFixture):
    """F2：她那一手的进入 / 分岔也并进她的发言位（此前只换 formed 的模板）。"""

    def test_her_entered_and_branch_speak_the_matching_wording(self):
        cases = (("entered", self.HER_ENTER), ("branch", self.HER_BRANCH))
        for kind, result in cases:
            with self.subTest(kind=kind):
                llm = _FreshLLM()
                agent = AzureAgent(StubEngine(), llm, events.RecordingBus(), joseki_rate=1.0, joseki_rng=ZeroRng())
                calls = []

                def fake():
                    calls.append(1)
                    return {} if len(calls) == 1 else dict(result)   # 玩家那手没结果；她那一步有

                agent._joseki_check = fake
                _seed_moves(agent, [["W", "D17"]])
                asyncio.run(agent.player_move(5, 2))

                text = _phase_texts(llm)[-1]                     # 最后一条阶段提示＝她那句感想
                name = str(result["name"])
                key = str(result["key"])
                if kind == "entered":
                    self.assertIn(prompts.joseki_enter_prompt(name, key, str(result["corner"])), text)
                else:
                    self.assertIn(prompts.joseki_branch_prompt(
                        name, key, list(result["candidates"])), text)
                self.assertNotIn("换一句新话", text, "顶替：普通感想那一档一个字都不在")
                self.assertEqual(len(agent.bus.messages()), 3,
                                 "结构点顶替本来那条：气泡数仍是 3，不新增")
                self.assertEqual(len(calls), 2, "她那一步只许识别一次")


# —— 以下为「体验与可靠性批 v3.7.0」· Task E 追加（既有断言一个字符都没改；追加在文件末尾，
#    理由同上：本仓的唯一跑法 `python -m unittest discover -s tests` 不会执行上面的 `unittest.main()`）——
#
# `busy` 兜底看门狗 + 代际守卫：一回合卡死时 `busy` 必须归位（不归位＝棋盘永久锁死，本项目最严重的
# 故障形态）；而且**超时不是只解锁**——旧那一代的后续写入（棋谱 / 快照 / 俗手账 / 逐手存档 / 揭示
# 标记 / 台词与 speech / 定式计数）一律作废，否则解锁后新回合会与旧回合抢写同一份 `st`。


class _SlowEngine(StubEngine):
    """固定延迟的假引擎：模拟「正常偏慢」的一回合（总耗时仍在看门狗预算内）。"""

    def __init__(self, delay=0.01):
        super().__init__()
        self.delay = float(delay)

    async def analyze(self, moves, max_visits=None):
        await asyncio.sleep(self.delay)
        return await super().analyze(moves)


class _SlowLLM(StubLLM):
    """固定延迟的假 LLM：同上。"""

    def __init__(self, delay=0.01, text="嗯~ 这一步我看着挺顺。"):
        super().__init__(text)
        self.delay = float(delay)

    async def chat(self, messages, max_tokens=300, no_think=False):
        await asyncio.sleep(self.delay)
        return await super().chat(messages, max_tokens, no_think)


class _HangingEngine(StubEngine):
    """卡死的假引擎：先挂满 ``hang`` 秒再返回（比看门狗长 ⇒ 超时先到）。

    有意**不**抛异常也不理取消：它模拟的是「等一个永远等不到的响应」，超时后它照样会恢复并把
    剩下的编排跑完——这正是要看代际守卫拦下来的那一批迟到写入。
    """

    def __init__(self, hang=0.3):
        super().__init__()
        self.hang = float(hang)

    async def analyze(self, moves, max_visits=None):
        await asyncio.sleep(self.hang)
        return await super().analyze(moves)


class _HangingTTS:
    """卡死的假 TTS：第 ``hang_at`` 次 ``speak`` 先挂满 ``hang`` 秒再返回（其余立刻返回）。

    ``_emit_line`` 是「先发 ``message``、再 ``await`` 合成」——挂在这里正是那次 ``await`` 的窗口；
    合成返回后 ``_emit_speech`` 还会补一条 ``speech`` 事件，所以「第 N 条 ``speech`` 到了」是
    「这一段 ``await`` 已经过去」的**确定性**信号（同一协程步内紧接着就是落子写入，见下）。
    """

    def __init__(self, hang_at=2, hang=0.6):
        self.hang_at = int(hang_at)
        self.hang = float(hang)
        self.calls = 0

    @property
    def enabled(self):
        return True

    async def speak(self, text):
        self.calls += 1
        if self.calls == self.hang_at:
            await asyncio.sleep(self.hang)
        return {"id": "t%d" % self.calls, "mode": "openai", "text": "",
                "tone": "", "pitch": 0.0}


async def _wait_for(pred, timeout=5.0, step=0.01):
    """轮询等待（替代「睡够某个秒数」）：判别力来自**僵尸那一代真的跑到了某处**，不是等够时间。

    到 ``timeout`` 仍未满足就返回 ``False``（由调用方的断言暴露问题），绝不真等 ``WATCHDOG_SEC``。
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + float(timeout)
    while not pred():
        if loop.time() > end:
            return False
        await asyncio.sleep(step)
    return True


class BusyWatchdogTest(unittest.TestCase):
    """看门狗（超时解锁 + 前推代数）与代际守卫（旧一代的写入全部作废）。"""

    HINT = "算得太久"                       # 超时那条可读提示的辨识片段

    def _hints(self, bus):
        return [e for e in bus.messages() if self.HINT in str(e.get("text", ""))]

    def test_normal_slow_turn_is_not_interrupted(self):
        # 三次 ainvoke + 重试的引擎调用 + 工具轮，全在 WATCHDOG_SEC 内 → 不触发
        agent, _eng, _llm, bus = _mk()
        agent.katago = _SlowEngine(0.02)          # 每次引擎分析 20ms
        agent.llm = _SlowLLM(0.02)                # 每次出话 20ms
        agent.tools_enabled = True                # 她那一步走工具轮（多一次模型往返）
        agent.chain = _ScriptChain([_Ai(content="嗯~ 这手我想想。")])
        agent.watchdog_sec = 1.0                  # 远大于本回合总耗时（含 0.35s 的犹豫节拍）

        asyncio.run(agent.player_move(3, 3))

        self.assertFalse(agent.busy)
        self.assertEqual(self._hints(bus), [], "正常偏慢的一回合不该触发看门狗")
        self.assertEqual([e["busy"] for e in bus.of_type("busy")], [True, False])
        self.assertEqual(len(bus.messages(events.AI_MOVE)), 1, "落子宣告照发")
        self.assertEqual(len(agent.st.moves), 2, "玩家那一手与她那一手都落进棋谱")

    def test_watchdog_releases_busy_on_a_hung_turn(self):
        # 卡死 → busy 归位、只发一条可读提示
        agent, _eng, _llm, bus = _mk()
        hang = _HangingEngine(0.3)                # 第一次引擎分析就挂 0.3s
        agent.katago = hang
        agent.watchdog_sec = 0.05

        async def run():
            await agent.player_move(3, 3)
            # 轮询等「僵尸那一代真的跑完它的两次引擎分析」（0.3s × 2）——固定 sleep 的判别力来自
            # 「等够了」，这里改成等一个**事件**：只有僵尸跑完，「旧一代什么都没发」才不是
            # 「它还没来得及发」换来的（原来的 0.4s 其实还在第二次分析的等待里）。
            await _wait_for(lambda: len(hang.calls) >= 2)

        asyncio.run(run())

        self.assertFalse(agent.busy, "超时必须归位，否则棋盘永久锁死")
        self.assertEqual([e["busy"] for e in bus.of_type("busy")], [True, False])
        hints = self._hints(bus)
        self.assertEqual(len(hints), 1, "超时只发一条可读提示（走既有 message 通道）")
        self.assertEqual(str(hints[0].get("type")), "message")
        self.assertEqual(str(hints[0].get("kind")), events.AI)
        self.assertEqual(len(hang.calls), 2, "僵尸那一代的两次引擎分析都已跑完（不是「还没轮到」）")
        # 旧一代恢复后什么都没发出来：整回合只有那一条提示
        self.assertEqual(len(bus.messages()), 1)

    def test_own_turn_write_is_guarded_after_the_tts_await(self):
        """修复波 #1 / R-I-3：``_speak_own_turn`` 在 ``_emit_line``（内含 TTS 合成 ``await``）**之后**
        也要核对代数——超时后的旧一代不许落子进棋谱、不许写逐手存档（玩家可能已经重新落子）。

        时序：after_player 的点评（TTS 第 1 次，立刻返回）→ own_turn 的犹豫节拍 0.35s →
        落子宣告的 TTS（第 2 次，挂 0.6s）；看门狗 0.5s **正好落在合成挂住期间**。
        断言前等到第 2 条 ``speech``（合成返回后才会补发）——它与落子写入在**同一协程步**内，
        所以「等到它」等价于「僵尸已经走到并越过那个写入点」，断言不会因为「还没跑到」而假绿。
        """
        agent, _eng, _llm, bus = _mk()
        agent.tts = _HangingTTS(hang_at=2, hang=0.6)
        agent.watchdog_sec = 0.5                  # > 犹豫节拍(0.35)；< 节拍 + 合成挂住(0.35+0.6)

        async def run():
            await agent.player_move(3, 3)
            await _wait_for(lambda: len(bus.of_type("speech")) >= 2)

        asyncio.run(run())

        self.assertFalse(agent.busy, "超时必须归位")
        self.assertEqual(len(self._hints(bus)), 1, "看门狗照发一条可读提示")
        self.assertEqual(len(bus.of_type("speech")), 2, "第二条 speech（落子宣告）已到 ⇒ 僵尸越过了合成 await")
        # —— 三条写入全都不许发生 ——
        self.assertEqual(len(agent.st.moves), 1, "棋谱里只剩玩家那一手；她那一手必须被守卫拦下")
        self.assertEqual(agent.st.moves[0][0], "B")
        stones = [(x, y) for x in range(19) for y in range(19) if agent.st.board.at(x, y) != ""]
        self.assertEqual(stones, [(3, 3)], "内部棋盘上只该有玩家那一颗子")
        self.assertEqual([n["kind"] for n in agent.st.move_notes], ["review"],
                         "逐手存档只该有玩家点评那一条；她的「用意」（kind=aim）不许写")
        self.assertFalse(agent._reveal_pending, "旧一代不许置揭示标记")

    def test_late_writes_after_timeout_are_discarded(self):
        # 超时后旧那一代的所有写入全部作废（st.moves / bad_moves / move_notes 不变）
        agent, _eng, _llm, bus = _mk()
        agent.katago = _HangingEngine(0.3)
        agent.watchdog_sec = 0.05
        before = (len(agent.st.moves), len(agent.st.bad_moves), len(agent.st.move_notes),
                  len(agent.st.snapshots), len(agent.st.losses), len(agent.st.memory_log))

        async def run():
            await agent.player_move(3, 3)
            await asyncio.sleep(0.4)              # 卡死那一代恢复后继续跑完

        asyncio.run(run())

        after = (len(agent.st.moves), len(agent.st.bad_moves), len(agent.st.move_notes),
                 len(agent.st.snapshots), len(agent.st.losses), len(agent.st.memory_log))
        self.assertEqual(after, before, "超时后旧那一代的写入必须全部作废")
        self.assertEqual(agent.st.board.at(3, 3), "", "玩家那一手也不许落进内部棋盘")
        self.assertEqual(agent.st.move_seq, [])
        self.assertFalse(agent._reveal_pending)
        self.assertEqual(len(self._hints(bus)), 1, "提示照发（归上一条用例细看），但写入一律作废")

    def test_no_double_busy_event(self):
        # 看门狗归位 + 公开方法 finally 再归位：busy 事件一条不多（`_set_busy` 自带去重）
        agent, _eng, _llm, bus = _mk()
        agent.katago = _HangingEngine(0.3)
        agent.watchdog_sec = 0.05

        async def run():
            await agent.player_move(3, 3)
            await asyncio.sleep(0.4)

        asyncio.run(run())
        seq = [e["busy"] for e in bus.of_type("busy")]
        self.assertEqual(seq, [True, False])
        self.assertEqual(seq.count(False), 1, "解锁只许有一条 busy 事件")

        # 棋盘不再锁死：紧接着的一次公开调用照常受理（新回合 = 新一代）
        agent.katago = StubEngine()
        agent.watchdog_sec = 1.0
        asyncio.run(agent.player_move(15, 15))
        self.assertFalse(agent.busy)
        self.assertEqual([e["busy"] for e in bus.of_type("busy")], [True, False, True, False])
        self.assertEqual(len(bus.messages(events.AI_MOVE)), 1)
        self.assertEqual(len(agent.st.moves), 2)

    def test_dead_summary_generation_writes_no_brief_and_no_mood(self):
        """总结阶段（``_prepare_summary``）的死代守卫：过期形势与死代情绪都不许跨代外泄。

        这一格是「引擎分析 ``await`` → 写 ``st.last_katago`` / 发 ``mood``」的窗口，正是看门狗会
        超时的地方。若漏了守卫，一个已被作废的总结代会把**过期形势**留在 ``st.last_katago``
        （它会被下一回合的 ``context_block`` / ``with_context`` 读走），并把死代的 ``mood`` 事件
        推进新对局——本任务要拦的正是这种跨代污染。
        """
        agent, _eng, _llm, bus = _mk()
        _seed_moves(agent, [["B", "Q16"], ["W", "D4"]])
        agent.st.last_katago = "旧形势"
        agent.katago = _HangingEngine(0.3)        # 终局那次分析挂 0.3s（看门狗先到）
        agent.watchdog_sec = 0.05

        async def run():
            await agent.summarize("修复轮 1")
            await asyncio.sleep(0.4)              # 卡死那一代恢复后继续跑完

        asyncio.run(run())

        self.assertFalse(agent.busy)
        self.assertEqual(agent.st.last_katago, "旧形势", "死代不许把过期形势注入下一回合的上下文")
        self.assertEqual(bus.of_type("mood"), [], "死代不许把 mood 事件推进新对局")
        self.assertEqual(len(self._hints(bus)), 1)

        # 对照：没超时的总结照旧写形势、照发 mood（证明上面那两条不是「什么都没发生」换来的）
        agent.katago = StubEngine()
        agent.watchdog_sec = 1.0
        asyncio.run(agent.summarize("修复轮 1"))
        self.assertNotEqual(agent.st.last_katago, "旧形势")
        self.assertTrue(bus.of_type("mood"))
        self.assertFalse(agent.busy)


# —— 以下为修复轮 R-1（v3.7.0）追加（既有断言一个字符都没改；追加在文件末尾，理由同上）——
#
# Wave B 把 4 个「2 手族」的 `kind` 从 `formed` 改成 `entered` 之后，编排里有**三处仍按 `formed`
# 门控**的耦合没跟着放宽（情绪分支 / `_len_note` / 玩家侧 LLM 失败兜底）。这里按「`entered`（含 2 手族）
# 也算结构点」把这三条钉死：2 手族走出进入时，三处都必须走**结构点**那一支。

def _system_text(llm):
    """模型收到的**系统消息**（长度约束 ``len_note`` 落在那里，见 ``prompt_chains``）。"""
    for msgs, _mx, _nt in llm.calls:
        for m in msgs:
            if str(m.get("role", "")) == "system":
                return str(m.get("content", ""))
    return ""


class TwoMoveFamilyStructureCouplingTest(unittest.TestCase):
    """R-1：2 手族走出 ``entered`` 时，情绪 / 长度约束 / 兜底台词都走结构点那一支。"""

    ENTER_SEED = [["W", "D17"]]        # 小目一间低挂：角内恰好 2 手即 entered（2 手族）

    def _after_player(self, agent, x, y):
        """只跑 `after_player` 那一个阶段（与 `player_move` 的公开路径同一入口）。"""
        agent._new_turn()
        return asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))

    def test_two_move_family_entered_counts_as_a_structure_point(self):
        # ① 情绪：进入是结构点 ⇒ 与 formed 同一档（player_good），不是普通那一手点评的 player_ok
        agent, _eng, _llm, _bus = _mk()
        agent.limit_len = True                       # 让 `_len_note` 真的进提示词（见 ②）
        _seed_moves(agent, self.ENTER_SEED)
        seen = []
        orig = agent._mood_event
        agent._mood_event = lambda ev, desc="": (seen.append(ev), orig(ev, desc))[1]
        state = self._after_player(agent, 5, 2)
        jk = state["data"]["jk"]
        self.assertEqual(str(jk["kind"]), "entered", "2 手族的角内 2 手＝进入")
        self.assertEqual(seen, ["player_good"], "进入也算结构点：情绪走 good 那一支")

        # ② 长度约束：结构点用 LEN_TEACH_NOTE（讲解允许 2~3 句），不是普通那句 LEN_NOTE
        sys_text = _system_text(agent.llm)           # 长度约束落在**系统消息**里（见 prompt_chains）
        self.assertIn(LEN_TEACH_NOTE, sys_text)
        self.assertNotIn(LEN_NOTE, sys_text)

        # ③ 兜底：玩家那一手云端失败时，结构点退化为**定式**兜底台词（不是普通兜底）
        agent2, _eng2, _llm2, _bus2 = _mk()
        agent2.llm = ErrLLM()                        # 「调用出错：…」＝真实失败形态（不抛）
        _seed_moves(agent2, self.ENTER_SEED)
        state2 = self._after_player(agent2, 5, 2)
        jk2 = state2["data"]["jk"]
        self.assertEqual(str(jk2["kind"]), "entered")
        self.assertIn(prompts.joseki_fallback(str(jk2["name"]), str(jk2["key"])),
                      str(state2["data"]["reply"]), "结构点的失败兜底＝定式兜底台词")
        self.assertNotIn(agent2._fallback_line("game"), str(state2["data"]["reply"]))


# —— 以下为阶段三（v3.8.0）追加：深搜**只给**「玩家落子后局面」那一次（既有断言一字未改）——


class PvDepthCallSiteTest(unittest.TestCase):
    """``max_visits`` 的调用点：整局里**只有** after_player 的那一次分析带深度，且**只增不减**。

    其余调用点（``just_moved`` 的复算、提示 / 分析 / 总结、工具）一律**不传**——全局提深度会让
    每回合都变慢（``post`` 也变慢，≈10s→≈20s，逼近超时），所以深度只给三线要用的那一次。
    而「只给那一次」不等于「可以砍浅」：实际传的是 ``max(pv_max_visits, 引擎默认)``——代码默认
    配置（``max_visits=500``）下必须传 500，否则三线降级，且**同一次**分析的 ``after_black_wr``
    （玩家这手亏没亏的判据源）也会变噪。
    """

    def _run_turn(self, agent):
        async def run():
            await agent.player_move(3, 3)
            await agent.hint()
            await agent.analyze_position()
            await agent.summarize("辛苦啦")

        asyncio.run(run())

    def test_only_the_after_player_analysis_carries_the_override(self):
        agent, eng, _llm, _bus = _mk()                       # StubEngine(max_visits=500)＝代码默认
        self.assertEqual(agent.pv_max_visits, 150)
        self._run_turn(agent)
        deep = [v for v in eng.visits if v is not None]
        self.assertEqual(deep, [500], "只增不减：引擎默认 500 > 150 ⇒ 那一次传 500（不许砍到 150）")
        i = eng.visits.index(500)
        self.assertEqual(eng.calls[i][-1], ["B", "D16"], "深搜那一次＝玩家刚落下那一手之后的局面")

    def test_shallow_engine_default_is_raised_to_pv_max_visits(self):
        """引擎默认更浅（随包 ``game/config.json`` 的 ``max_visits=100``）⇒ 那一次抬到 150。"""
        agent, _eng, _llm, _bus = _mk()
        agent.katago = StubEngine(max_visits=100)
        self._run_turn(agent)
        deep = [v for v in agent.katago.visits if v is not None]
        self.assertEqual(deep, [150], "150 > 100 ⇒ 三线要的更深搜索在这里生效")
        i = agent.katago.visits.index(150)
        self.assertEqual(agent.katago.calls[i][-1], ["B", "D16"])


class DriftSettlementTest(unittest.TestCase):
    """修复轮 1 / Important #1：偏航**先定案、后记账**——事件必须与最终落点一致。

    事件表是本功能唯一的产出物、也是 P5「事件区块」喂给模型的素材：与事实不符会被原样注入提示词。
    偏航点可能在 `decide` 之后被工具轮重定案（``_settle_tool_final`` 走 ``_resolve_ai_move``），
    所以记账必须等落点**定案并提交**之后再做（``_speak_own_turn`` 里），且只在最终落点就是那个
    偏航点时写。
    """

    def _agent_with_drift(self):
        agent, _eng, _llm, bus = _mk()
        agent.katago = PvStubEngine()            # 三条线凑得出来（StubEngine 没有 pv ⇒ 只有 A）
        agent.emotion_drift = True
        agent.drift_rng = FixedRng(0.0)          # 0.0 落在 B 段 ⇒ 偏航点是 D4
        return agent, bus

    def test_tool_round_resettlement_leaves_no_drift_event(self):
        """工具轮把落点重定案成别的点 ⇒ 事件表里**不许**留下「我偏航选了 D4」。"""
        agent, bus = self._agent_with_drift()
        agent.tools_enabled = True
        agent.chain = _ScriptChain([_Ai(content="我下 Q4。")])   # 工具轮：模型自报另一个合法点

        asyncio.run(agent.player_move(3, 3))

        self.assertEqual(agent.st.moves[-1][1], "Q4", "落点被工具轮重定案（不是偏航点 D4）")
        self.assertEqual(bus.messages(events.AI_MOVE)[-1]["move"]["gtp"], "Q4", "音画同步照旧一致")
        self.assertEqual(agent.event_log.all(), [], "最终落点不是偏航点 ⇒ 一条事件都不许留")
        self.assertFalse(agent.busy)

    def test_drifted_move_is_recorded_when_no_tool_round_intervenes(self):
        """反向：没有工具轮插手 ⇒ 真的走了偏航点，事件照写（正反两面都要有）。"""
        agent, bus = self._agent_with_drift()

        asyncio.run(agent.player_move(3, 3))

        self.assertEqual(agent.st.moves[-1][1], "D4", "走的是 B 的首手")
        self.assertEqual(bus.messages(events.AI_MOVE)[-1]["move"]["gtp"], "D4")
        log = agent.event_log.all()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["kind"], "ai_bad")
        self.assertIn("自主偏航", log[0]["text"])

    def test_dead_turn_leaves_no_ghost_drift_event(self):
        """修复轮 1 / Minor #4：回合被看门狗作废 ⇒ 连**幽灵事件**都不许留（记账点在提交之后）。

        时序与 ``BusyWatchdogTest.test_own_turn_write_is_guarded_after_the_tts_await`` 同款：
        落子宣告的 TTS 挂住 0.6s、看门狗 0.5s 正好落在合成挂住期间 ⇒ own_turn 越过写入点时
        本回合已被作废。记在 ``_drift_pick`` 里（定案即记账）的话，这里会留下一条幽灵事件。
        """
        agent, bus = self._agent_with_drift()
        agent.tts = _HangingTTS(hang_at=2, hang=0.6)
        agent.watchdog_sec = 0.5

        async def run():
            await agent.player_move(3, 3)
            await _wait_for(lambda: len(bus.of_type("speech")) >= 2)

        asyncio.run(run())

        self.assertFalse(agent.busy, "超时必须归位")
        self.assertEqual(len(agent.st.moves), 1, "她那一手被守卫拦下（棋谱只剩玩家那一手）")
        self.assertEqual(agent.event_log.all(), [], "作废的回合不许留下幽灵偏航事件")

    def test_drift_is_rolled_once_per_turn_across_tool_rounds(self):
        """修复波 / Minor #3：走**真**工具轮（``decide → tools → decide``）也**只掷一次**。

        重掷会把有效偏航概率放大成「进几次乘几倍」——工具轮每多一轮就多掷一次。这条钉的是**可观测
        不变量**（一轮一次），不绑具体机制：图路径今天由 ``decide`` 的 ``setup_phase`` 守卫兜着，
        ``_decide_own_turn`` 里那道 ``data["drift_picked"]`` 复用是同一不变量的第二层保险。
        """
        agent, _bus = self._agent_with_drift()
        rng = _CountingRng()
        agent.drift_rng = rng
        agent.tools_enabled = True
        agent.chain = _ScriptChain([
            _Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}]),
            _Ai(content="我下 Q4。"),                  # 工具轮自报另一个合法点（不是偏航点 D4）
        ])

        asyncio.run(agent.player_move(3, 3))

        self.assertEqual(rng.calls, 1, "一轮只掷一次（条件边再进入 decide 也不重掷）")
        self.assertEqual(agent.st.moves[-1][1], "Q4", "落点仍由工具轮定案")
        self.assertEqual(agent.event_log.all(), [], "最终落点不是偏航点 ⇒ 一条事件都不留")

    def test_same_data_never_rolls_twice(self):
        """修复波 / Minor #3 的**直接**门卫：同一份 ``data`` 两次进入 ``_decide_own_turn`` 也不重掷。

        生产路径本来就只进一次（``decide`` 的 ``setup_phase`` 守卫），所以这条测的是那一层保险本身
        ——去掉 ``data["drift_picked"]`` 复用，这里会掷两次（rng.calls == 2）。
        """
        from app import agent_graph

        agent, _bus = self._agent_with_drift()
        rng = _CountingRng()
        agent.drift_rng = rng
        data = {"after_infos": PvStubEngine().move_infos(), "epoch": agent._epoch_token}
        old = agent_graph.THINK_BEAT_SEC
        agent_graph.THINK_BEAT_SEC = 0                 # 犹豫节拍与本用例无关：压到 0，别真等
        try:
            asyncio.run(agent_graph._decide_own_turn(agent, data))
            asyncio.run(agent_graph._decide_own_turn(agent, data))
        finally:
            agent_graph.THINK_BEAT_SEC = old

        self.assertEqual(rng.calls, 1, "同一份 data 再进一次也不重掷")
        self.assertEqual(data["drift_pick"][0], "D4", "复用第一次掷出的落点")
        self.assertEqual(data["drift"]["gtp"], "D4", "待记账信息照旧透传给 speak")


class PlayerEventWriteTest(unittest.TestCase):
    """v3.8.0 修复波 / #1：**玩家侧的好手 / 坏手也进事件表**（P3 的四个写入点之二）。

    判据与情绪分流同一档（``_prepare_after_player`` 里的 ``ev``）：
    ``player_good``＝她算的最优手（rank 0）或定式结构点；``player_bad``＝不在前 5 或胜率掉 > 10%；
    ``player_ok``（前 3 内）与 ``player_off``（rank 3~4）是**常态**、按既有口径**不写**（防噪音）。
    ``StubEngine`` 的候选表固定，所以 rank 由落点唯一决定（Q16=0 / D4=1 / D16=3 / 其余 −1）。
    """

    def test_good_move_writes_a_player_good_event(self):
        agent, _eng, _llm, _bus = _mk()
        asyncio.run(agent.player_move(15, 3))           # Q16 ＝ 第一候选 ⇒ rank 0 ⇒ player_good
        log = [e for e in agent.event_log.all() if e["kind"] == "player_good"]
        self.assertEqual(len(log), 1)
        ev = log[0]
        self.assertEqual(ev["move_no"], 1, "玩家这一手的 1 基序号（add_move 之后）")
        self.assertEqual(ev["detail"]["rank"], 0)
        self.assertEqual(ev["detail"]["gtp"], "Q16")
        self.assertIn("第1手", ev["text"])

    def test_bad_move_writes_a_player_bad_event(self):
        agent, _eng, _llm, _bus = _mk()
        asyncio.run(agent.player_move(8, 8))            # J11 不在前 5 ⇒ rank −1 ⇒ player_bad
        log = [e for e in agent.event_log.all() if e["kind"] == "player_bad"]
        self.assertEqual(len(log), 1)
        ev = log[0]
        self.assertEqual(ev["move_no"], 1)
        self.assertEqual(ev["detail"]["rank"], -1)
        self.assertEqual(ev["detail"]["drop"], 0.0)
        self.assertIn("他", ev["text"], "玩家口径是第三人称（第一人称会把玩家的坏手说成 Azure 自己下的）")
        self.assertNotIn("我", ev["text"])
        # 事件原文**永不进台词**：它只进事件表 / 提示词，不产生 message
        self.assertNotIn(ev["text"], " ".join(m["text"] for m in _bus.messages()))

    def test_normal_moves_write_nothing(self):
        """防噪音：前 3 内的普通手（player_ok）与 rank 3~4（player_off）都不写。"""
        a1, _e1, _l1, _b1 = _mk()
        asyncio.run(a1.player_move(3, 15))              # D4 ＝ 第二候选 ⇒ rank 1 ⇒ player_ok
        self.assertEqual(a1.event_log.all(), [])
        a2, _e2, _l2, _b2 = _mk()
        asyncio.run(a2.player_move(3, 3))               # D16 ＝ 第四候选 ⇒ rank 3 ⇒ player_off
        self.assertEqual(a2.event_log.all(), [])

    def test_bad_move_by_winrate_drop_records_the_drop(self):
        """第二条判据：**真亏**（胜率掉 > 10%）也写 ``player_bad``，``detail.drop`` 是实打实的那个数。

        落点取 rank 3 的 D16 ⇒ 「好手」那一支不成立，走的正是 ``drop > 0.10``（黑方视角 0.5 → 0.2）。
        """
        agent, _eng, _llm, _bus = _mk()
        agent.katago = _DroppingEngine()
        asyncio.run(agent.player_move(3, 3))
        log = [e for e in agent.event_log.all() if e["kind"] == "player_bad"]
        self.assertEqual(len(log), 1)
        ev = log[0]
        self.assertAlmostEqual(ev["detail"]["drop"], 0.3, places=6)
        self.assertEqual(ev["detail"]["rank"], 3)
        self.assertIn("胜率掉了约30%", ev["text"])
        self.assertIn("左上这一片", ev["text"], "方位词按坐标核对（D16 在左上）")


