"""工具层：三个工具的形状与失败吞咽；tool loop 的预算与事件。"""

import asyncio
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.tools import TOOLS, katago_analyze, joseki_lookup
from helpers import StubLLM


class StubEngine:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def analyze(self, moves, max_visits=None):
        self.calls.append([list(m) for m in moves])
        if self.error:
            return {"error": self.error}
        return {"rootInfo": {"winrate": 0.5, "scoreLead": 1.0},
                "moveInfos": [{"move": "Q16"}, {"move": "D4"}], "ownership": [0.0] * 361}


class ToolShapeTest(unittest.TestCase):
    def test_three_tools_exposed(self):
        self.assertEqual({t.name for t in TOOLS}, {"katago_analyze", "joseki_lookup", "board_query"})

    def test_katago_analyze_returns_text(self):
        out = asyncio.run(katago_analyze.ainvoke({"engine": StubEngine(), "moves": [["B", "Q16"]]}))
        self.assertIsInstance(out, str)
        self.assertIn("Q16", out)

    def test_katago_analyze_error_is_readable_text(self):
        out = asyncio.run(katago_analyze.ainvoke({"engine": StubEngine(error="服务不可用"), "moves": []}))
        self.assertIsInstance(out, str)
        self.assertIn("服务不可用", out)

    def test_joseki_lookup_unknown_name(self):
        out = joseki_lookup.invoke({"name": "不存在的定式"})
        self.assertIsInstance(out, str)
        self.assertTrue(out)          # 空/异常都不允许：必须给模型一句可读的话


class ToolLoopTest(unittest.TestCase):
    """tool loop 用假链驱动：一次工具调用 + 一次收尾；预算用尽必须硬停。"""

    class _Ai:
        def __init__(self, content="", tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class _Chain:
        def __init__(self, script):
            self.script = list(script)
            self.seen = []

        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, messages, max_tokens=None):
            self.seen.append(len(messages))
            return self.script.pop(0)

    def _agent(self, script, bus):
        from app.agent import AzureAgent
        a = AzureAgent(StubEngine(), llm=None, bus=bus)
        a.chain = self._Chain(script)
        a.tools_enabled = True
        return a

    def test_single_tool_round_then_answer(self):
        bus = events.RecordingBus()
        call = {"name": "board_query", "args": {}, "id": "c1"}
        a = self._agent([self._Ai(tool_calls=[call]), self._Ai(content="我看清楚了：这手在左上。")], bus)
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "own_turn",
                                       tools=[t for t in TOOLS if t.name == "board_query"]))
        self.assertIn("左上", out)
        starts = [e for e in bus.of_type("tool") if e["phase"] == "start"]
        ends = [e for e in bus.of_type("tool") if e["phase"] == "end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0]["ok"])
        self.assertIsInstance(ends[0]["ms"], int)

    def test_budget_is_hard_capped(self):
        bus = events.RecordingBus()
        call = {"name": "board_query", "args": {}, "id": "c1"}
        script = [self._Ai(tool_calls=[call]) for _ in range(10)] + [self._Ai(content="收尾")]
        a = self._agent(script, bus)
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe",
                                       tools=[t for t in TOOLS if t.name == "board_query"]))
        self.assertEqual(out, "收尾")
        self.assertEqual(len([e for e in bus.of_type("tool") if e["phase"] == "start"]), a.max_tool_calls)

    def test_tool_failure_does_not_raise(self):
        bus = events.RecordingBus()
        call = {"name": "katago_analyze", "args": {"moves": []}, "id": "c1"}
        a = self._agent([self._Ai(tool_calls=[call]), self._Ai(content="我再看看。")], bus)
        a.katago = StubEngine(error="引擎炸了")
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe", tools=TOOLS))
        self.assertTrue(out)
        self.assertFalse([e for e in bus.of_type("tool") if e["phase"] == "end"][0]["ok"])


class DisabledByDefaultTest(unittest.TestCase):
    def test_tools_off_keeps_old_path(self):
        from app.agent import AzureAgent
        a = AzureAgent(StubEngine(), llm=None, bus=events.RecordingBus())
        self.assertFalse(a.tools_enabled)


class ToolSafetyTest(unittest.TestCase):
    """补充锁定三条硬约束：单工具超时（TOOL_TIMEOUT_SEC）、直连链的地址快拦（Q7）、
    输出上限的线上键（``max_tokens``，不能是 langchain-openai 改名的 ``max_completion_tokens``）。"""

    class _Chain(ToolLoopTest._Chain):
        """把每次请求的消息也记下来，便于断言 ToolMessage 的内容。"""

        def __init__(self, script):
            super().__init__(script)
            self.msgs = []

        async def ainvoke(self, messages, max_tokens=None):
            self.msgs.append(list(messages))
            return await super().ainvoke(messages, max_tokens=max_tokens)

    def test_tool_timeout_becomes_readable_text(self):
        from unittest import mock
        from langchain_core.tools import tool
        from app.agent import AzureAgent

        @tool
        async def slow_tool() -> str:
            """永远不会按时返回的假工具（只用来验超时路径）。"""
            await asyncio.sleep(5.0)
            return "不该等到我"

        bus = events.RecordingBus()
        a = AzureAgent(StubEngine(), llm=None, bus=bus)
        a.chain = self._Chain([ToolLoopTest._Ai(tool_calls=[{"name": "slow_tool", "args": {}, "id": "c1"}]),
                               ToolLoopTest._Ai(content="那就不查了。")])
        a.tools_enabled = True
        with mock.patch("app.agent.TOOL_TIMEOUT_SEC", 0.05):
            out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe", tools=[slow_tool]))
        self.assertEqual(out, "那就不查了。")           # 超时不阻塞回合：模型照常收尾
        ends = [e for e in bus.of_type("tool") if e["phase"] == "end"]
        self.assertEqual(len(ends), 1)
        self.assertFalse(ends[0]["ok"])
        self.assertIn("超时", str(a.chain.msgs[1][-1].content))   # 超时文本进 ToolMessage，模型看得见

    def test_unconfigured_url_never_touches_chain(self):
        """未配置 llm_base_url：工具路径必须与 chat() 同一判据，一个请求都不发（Q7）。"""
        from app.agent import AzureAgent
        from app.llm import LLMClient

        def handler(req):
            raise AssertionError("不该发出请求：" + str(req.url))

        llm = LLMClient(base_url="", http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        bus = events.RecordingBus()
        a = AzureAgent(StubEngine(), llm, bus)
        a.tools_enabled = True
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe", tools=TOOLS))
        self.assertTrue(out.startswith("调用出错："))    # 退回 chat()：可读中文，且一个请求都没发
        self.assertEqual(bus.of_type("tool"), [])

    def test_tools_on_hint_still_answers_and_unlocks(self):
        """工具开着的提示回合：模型一直要工具也必须硬停、照常出话、busy 归位（Review Focus 第三条）。"""
        from app.agent import AzureAgent

        class _LoopChain:
            """永远只回「要工具」的假链：验预算硬停与回合收尾。"""

            def bind_tools(self, _tools):
                return self

            async def ainvoke(self, messages, max_tokens=None):
                return ToolLoopTest._Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}])

        bus = events.RecordingBus()
        a = AzureAgent(StubEngine(), llm=None, bus=bus)
        a.chain = _LoopChain()
        a.tools_enabled = True
        asyncio.run(a.hint())
        self.assertFalse(a.busy)                                        # 棋盘必须解锁
        self.assertFalse(bus.of_type("busy")[-1]["busy"])
        self.assertTrue(bus.messages(events.AI_COMMENT))                # 照常出话（兜底台词顶上）
        self.assertEqual(len([e for e in bus.of_type("tool") if e["phase"] == "start"]), a.max_tool_calls)

    def test_real_chain_round_trip_keeps_max_tokens(self):
        """真链走一遍完整 tool loop：工具真绑上、ToolMessage 真回传、上限仍是线上键 max_tokens。"""
        import json
        from langchain_openai import ChatOpenAI
        from app.agent import AzureAgent

        seen = []

        def handler(req):
            body = json.loads(req.content.decode("utf-8"))
            seen.append(body)
            if len(seen) == 1:                  # 第一轮：模型要查盘面
                return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant", "content": "",
                                             "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "board_query", "arguments": "{}"}}]}}]})
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "盘面我看过了。"}}]})

        a = AzureAgent(StubEngine(), llm=None, bus=events.RecordingBus())
        a.chain = ChatOpenAI(model="m", base_url="https://api.example.com/v1", api_key="k",
                             http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             max_retries=0)
        a.tools_enabled = True
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe",
                                       tools=[t for t in TOOLS if t.name == "board_query"]))
        self.assertEqual(out, "盘面我看过了。")
        self.assertEqual(seen[0]["max_tokens"], 120)
        self.assertNotIn("max_completion_tokens", seen[0])
        self.assertEqual([t["function"]["name"] for t in seen[0]["tools"]], ["board_query"])
        self.assertEqual(seen[1]["messages"][-1]["role"], "tool")        # 工具结果作为 ToolMessage 回传
        self.assertIn("X = Estarth", seen[1]["messages"][-1]["content"])  # 回给模型的是盘面图，不是台词


class _StatusErr(Exception):
    """带 ``status_code`` 的假异常：形状与 ``openai.APIStatusError`` 一致（安全网只读这个属性）。"""

    def __init__(self, code):
        super().__init__("HTTP %d" % code)
        self.status_code = code


class _FakeChain:
    """按脚本回话的假链（``bind_tools`` 原样返回自己）：记录每次 ``bind`` 的扩展字段与调用次数。

    ``reject_first=True`` 时第一次请求抛 HTTP 400 —— 用来验 ``_chain_invoke`` 的
    「服务端拒绝参数时去掉扩展字段重试一次」安全网。
    """

    def __init__(self, script, reject_first=False):
        self.script = list(script)
        self.reject_first = reject_first
        self.binds = []                  # 每次 bind 传下来的 extra_body
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    def bind(self, **kw):
        self.binds.append(dict(kw.get("extra_body") or {}))
        return self

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        if self.reject_first and self.calls == 1:
            raise _StatusErr(400)
        return self.script.pop(0)


class AiTurnToolsTest(unittest.TestCase):
    """落子宣告那一半的工具启用点（``agent._ai_turn``）——与 ``hint()`` 并列的第二处入口。

    这里跑完整回合（``player_move``）而不是只调 ``_tool_loop``：工具开着时「落点照旧由
    ``_pick_ai_move`` 决定、音画同步的坐标照发、busy 照归位」这几条硬规则，只有在整条
    编排里才验证得了（``hint()`` 的启用点已由 ToolSafetyTest 覆盖，这一处此前没有测试）。
    """

    def _agent(self, chain):
        from app.agent import AzureAgent
        bus = events.RecordingBus()
        a = AzureAgent(StubEngine(), StubLLM(), bus)     # 点评/落子后那几句走假 LLM，工具路径走假链
        a.chain = chain
        a.tools_enabled = True
        return a, bus

    def test_tools_on_ai_turn_keeps_move_and_unlocks(self):
        call = {"name": "board_query", "args": {}, "id": "c1"}
        chain = _FakeChain([ToolLoopTest._Ai(tool_calls=[call]),
                            ToolLoopTest._Ai(content="这手先挂角，把左边的模样撑起来。")])
        a, bus = self._agent(chain)
        asyncio.run(a.player_move(3, 3))

        # 工具真的跑了一轮：start/end 成对，且结果是成功的
        starts = [e for e in bus.of_type("tool") if e["phase"] == "start"]
        ends = [e for e in bus.of_type("tool") if e["phase"] == "end"]
        self.assertEqual([e["name"] for e in starts], ["board_query"])
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0]["ok"])

        # 落子宣告照常：是模型的话（不是兜底台词），且坐标由 _pick_ai_move 决定
        moves = bus.messages(events.AI_MOVE)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["text"], "这手先挂角，把左边的模样撑起来。")
        mv = moves[0]["move"]
        self.assertEqual((mv["gtp"], mv["x"], mv["y"]), ("Q16", 15, 3))
        self.assertFalse(mv["pass"])

        # 棋盘必须解锁（不归位＝永久锁死）
        self.assertFalse(a.busy)
        self.assertFalse(bus.of_type("busy")[-1]["busy"])
        self.assertEqual(len(a.st.moves), 2)

    def test_rejected_extra_body_recovers_without_it(self):
        """服务端拒绝参数（HTTP 400）时工具路径去掉扩展字段重试一次：回合照常出话，不降级成兜底台词。"""
        chain = _FakeChain([ToolLoopTest._Ai(content="我查过了，这手先守角。")], reject_first=True)
        a, bus = self._agent(chain)
        asyncio.run(a.player_move(3, 3))

        self.assertEqual(chain.binds[0], {"max_tokens": 80})     # 第一次带着注入的输出上限（被拒的就是它）
        self.assertEqual(chain.binds[-1], {})                    # 重试不再带任何扩展字段
        self.assertEqual(chain.calls, 2)
        moves = bus.messages(events.AI_MOVE)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["text"], "我查过了，这手先守角。")
        self.assertFalse(a.busy)


class ExtraBodyRejectTest(unittest.TestCase):
    """服务端拒绝过扩展字段之后（``llm._extra_body_unsupported``）：工具路径不许再加料。

    不加这道闸，每次工具调用都会先挨一次 400 再走重试（每回合最多两次），
    与 ``LLMClient._extra_for`` 的「置位后永久不再加料」不一致。
    """

    def test_extra_body_not_injected_after_provider_reject(self):
        from app.agent import AzureAgent
        llm = StubLLM()
        llm._extra_body_unsupported = True               # 与 LLMClient 被拒后的状态一致
        chain = _FakeChain([ToolLoopTest._Ai(tool_calls=[{"name": "board_query", "args": {}, "id": "c1"}]),
                            ToolLoopTest._Ai(content="盘面我看过了。")])
        a = AzureAgent(StubEngine(), llm, events.RecordingBus())
        a.chain = chain
        a.tools_enabled = True
        out = asyncio.run(a._tool_loop([{"role": "user", "content": "x"}], 120, "observe",
                                       tools=[t for t in TOOLS if t.name == "board_query"]))
        self.assertEqual(out, "盘面我看过了。")
        self.assertEqual(chain.binds, [{}])              # 一个扩展字段都不许再加
        self.assertEqual(chain.calls, 2)                 # 两次都是模型往返，没有 400 重试


if __name__ == "__main__":
    unittest.main()
