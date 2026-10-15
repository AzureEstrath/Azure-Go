"""结构化情绪增量：能解析则用，解析不了返回 None（由调用方回落正则）。

langchain-openai 1.x 的异步路径（``ainvoke``）只认 ``http_async_client``：同步/异步两个假
客户端（各挂一个 MockTransport）都必须注入，否则测试会真的去连 api.example.com。

结构化输出走 ``method="function_calling"``（工具名即模型名 ``MoodDelta``）：1.x 的默认方法
改发 ``response_format.json_schema``，那种形状下服务端回的是 JSON 正文而不是工具调用。
"""

import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.llm import LLMClient


class _StubEngine:
    """假引擎：情绪小调用根本不碰引擎，这里只为把 AzureAgent 装配起来。"""

    async def analyze(self, moves):
        return {"rootInfo": {"winrate": 0.5, "scoreLead": 0.0}, "moveInfos": [], "ownership": []}


def _tool_call_reply(args_json):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "MoodDelta", "arguments": args_json}}]}}]}


def _client(handler):
    """全程不触网的 LLMClient：同步/异步两条路径都注入 MockTransport（异步路径只认 http_async_client）。"""
    return LLMClient(base_url="https://api.example.com/v1", model="m", api_key="k",
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                     http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class MoodDeltaTest(unittest.TestCase):
    def test_structured_parse_success(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_tool_call_reply(json.dumps(
                {"dv": 0.1, "dc": -0.05, "da": 0.2})))

        c = _client(handler)
        out = asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "他夸我了"))
        self.assertAlmostEqual(out[0], 0.1, places=6)
        self.assertAlmostEqual(out[1], -0.05, places=6)
        self.assertAlmostEqual(out[2], 0.2, places=6)
        # 结构化请求必须把 schema 交给服务端（function_calling：tools + 强制的 tool_choice）
        self.assertEqual(seen[0]["tools"][0]["function"]["name"], "MoodDelta")
        self.assertEqual(seen[0]["tool_choice"]["function"]["name"], "MoodDelta")
        # 问的是情绪增量：当前值、区域与刚刚发生的事都要在提示词里
        prompt = seen[0]["messages"][-1]["content"]
        self.assertIn("0.50", prompt)
        self.assertIn("平静", prompt)
        self.assertIn("他夸我了", prompt)

    def test_unparsable_returns_none(self):
        c = _client(lambda req: httpx.Response(200, json={"id": "x", "object": "chat.completion",
            "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "我不知道"}}]}))
        self.assertIsNone(asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "他夸我了")))

    def test_clamped_to_range(self):
        c = _client(lambda req: httpx.Response(200, json=_tool_call_reply(json.dumps(
            {"dv": 5.0, "dc": -9.0, "da": 0.0}))))
        out = asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "x"))
        self.assertEqual(out, [0.4, -0.4, 0.0])

    def test_missing_field_returns_none(self):
        """模型漏字段（schema 校验失败）也算拿不到：静默返回 None，绝不抛给上层。"""
        c = _client(lambda req: httpx.Response(200, json=_tool_call_reply(json.dumps({"dv": 0.1}))))
        self.assertIsNone(asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "x")))

    def test_unconfigured_base_url_never_reaches_network(self):
        """未配置地址（config.json 可能留空）：一个请求都不许发（openai 客户端会静默退回 api.openai.com）。"""
        def handler(req):
            raise AssertionError("不该发出请求：" + str(req.url))

        c = _client(handler)
        c.base_url = ""
        self.assertIsNone(asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "x")))


class AgentFallbackTest(unittest.TestCase):
    """调用方语义：结构化能拿到就用它；拿不到才回落正则（旧的 ``chat`` 路径，v2 行为不变）。"""

    class _LLM:
        """假 LLM：``chat`` 一被调用就记账，用来分辨走的是哪条路。"""

        think_mode = 0
        base_url = "https://api.example.com/v1"

        def __init__(self, structured, text=""):
            self.structured = structured
            self.text = text
            self.calls = []

        async def mood_delta(self, cur, region_name, desc):
            self.calls.append("mood_delta")
            return self.structured

        async def chat(self, messages, max_tokens=300, no_think=False):
            self.calls.append("chat")
            return self.text

    def _agent(self, llm):
        from app.agent import AzureAgent
        a = AzureAgent(_StubEngine(), llm, events.RecordingBus())
        a._mood_desc = "他夸了你"
        return a

    def test_structured_delta_applied_without_regex_call(self):
        llm = self._LLM([0.2, -0.1, 0.1])
        a = self._agent(llm)
        v0, c0, a0 = a.mood.v, a.mood.c, a.mood.a
        asyncio.run(a._mood_llm_refine())
        self.assertAlmostEqual(a.mood.v, v0 + 0.2, places=3)
        self.assertAlmostEqual(a.mood.c, c0 - 0.1, places=3)
        self.assertAlmostEqual(a.mood.a, a0 + 0.1, places=3)
        self.assertEqual(llm.calls, ["mood_delta"])       # 拿到结构化结果就不再问第二次

    def test_structured_none_falls_back_to_regex(self):
        llm = self._LLM(None, text="[[mood:+0.10,-0.05,+0.20]]")
        a = self._agent(llm)
        v0 = a.mood.v
        asyncio.run(a._mood_llm_refine())
        self.assertAlmostEqual(a.mood.v, v0 + 0.1, places=3)
        self.assertEqual(llm.calls, ["mood_delta", "chat"])

    def test_llm_without_mood_delta_keeps_old_path(self):
        """鸭子类型：只实现 ``chat()`` 的 LLM（既有 stub 的形状）必须照旧走正则路径。"""

        class ChatOnly:
            think_mode = 0
            base_url = "https://api.example.com/v1"

            def __init__(self):
                self.calls = []

            async def chat(self, messages, max_tokens=300, no_think=False):
                self.calls.append("chat")
                return "[[mood:+0.10,-0.05,+0.20]]"

        llm = ChatOnly()
        a = self._agent(llm)
        v0 = a.mood.v
        asyncio.run(a._mood_llm_refine())
        self.assertAlmostEqual(a.mood.v, v0 + 0.1, places=3)
        self.assertEqual(llm.calls, ["chat"])

    def test_malformed_structured_shape_falls_back(self):
        """脏返回值（形状不对）不得抛异常：当作没拿到，回落正则。"""
        llm = self._LLM({"dv": 0.1}, text="[[mood:+0.10,-0.05,+0.20]]")
        a = self._agent(llm)
        v0 = a.mood.v
        asyncio.run(a._mood_llm_refine())
        self.assertAlmostEqual(a.mood.v, v0 + 0.1, places=3)
        self.assertEqual(llm.calls, ["mood_delta", "chat"])

    def test_both_paths_failed_is_silent(self):
        """两条路都拿不到：静默返回，不抛异常、不改情绪（情绪小调用绝不打扰主流程）。"""
        llm = self._LLM(None, text="调用出错：HTTP 401（检查 llm_api_key 是否正确）")
        a = self._agent(llm)
        v0, c0, a0 = a.mood.v, a.mood.c, a.mood.a
        asyncio.run(a._mood_llm_refine())
        self.assertEqual((a.mood.v, a.mood.c, a.mood.a), (v0, c0, a0))


if __name__ == "__main__":
    unittest.main()
