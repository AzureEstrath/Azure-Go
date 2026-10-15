"""工具层：三个工具的形状与失败吞咽；tool loop 的预算与事件。"""

import asyncio
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.tools import TOOLS, katago_analyze, joseki_lookup


class StubEngine:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def analyze(self, moves):
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


if __name__ == "__main__":
    unittest.main()
