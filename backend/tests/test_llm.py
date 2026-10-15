"""LLMClient（LangChain 版）校验：文本清洗、错误映射、400 降级安全网。不触网。

langchain-openai 1.x 的异步路径（``ainvoke``）只认 ``http_async_client``：同步/异步两个假
客户端（各挂一个 MockTransport）都必须注入，否则测试会真的去连 api.example.com。

输出上限走线上键 ``max_tokens``（经 ``bind(extra_body=...)`` 顶层合并）：``ChatOpenAI``
自己的 ``max_tokens`` 会被改名成 ``max_completion_tokens``，而目标服务端（DeepSeek 等）认的是前者。
"""

import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import LLMClient


def _reply(content):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def _client(handler, **kw):
    """全程不触网的 LLMClient：同步/异步两条路径都注入 MockTransport（异步路径只认 http_async_client）。"""
    kw.setdefault("base_url", "https://api.example.com/v1")
    return LLMClient(model="m", api_key="k",
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                     http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


class CleanContentTest(unittest.TestCase):
    def test_strip_think_block(self):
        c = LLMClient()
        self.assertEqual(c.clean_content("" + "<" + "think" + ">推理中" + "<" + "/" + "think" + ">你好~"), "你好~")

    def test_unclosed_think_means_empty(self):
        c = LLMClient()
        self.assertEqual(c.clean_content("<" + "think" + ">还没闭合"), "")

    def test_empty(self):
        c = LLMClient()
        self.assertEqual(c.clean_content(""), "")

    def test_plain_text_kept(self):
        c = LLMClient()
        self.assertEqual(c.clean_content("  嗯~ 挺好  "), "嗯~ 挺好")

    def test_module_level_reexport(self):
        """旧引用 `from app.llm import clean_content` 必须继续可用（实现只有 llm_chain 一处）。"""
        from app.llm import clean_content
        self.assertEqual(clean_content("<" + "think" + ">推理中" + "<" + "/" + "think" + ">你好~"), "你好~")
        self.assertEqual(clean_content(""), "")


class ChatTest(unittest.TestCase):
    def test_plain_reply(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        # 输出上限的线上键就是 max_tokens（不是 langchain-openai 默认改的 max_completion_tokens）
        self.assertEqual(seen[0]["max_tokens"], 100)
        self.assertNotIn("max_completion_tokens", seen[0])

    def test_reasoning_content_not_shown(self):
        def handler(req):
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "嗯~ 这手不错",
                                         "reasoning_content": "内心戏：他这手很差"}}]})

        c = _client(handler)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, "嗯~ 这手不错")
        self.assertNotIn("内心戏", out)

    def test_empty_content_returns_fail_text(self):
        c = _client(lambda req: httpx.Response(200, json=_reply("")))
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}])),
                         "唔...我脑子有点转不过来了")

    def test_http_401_is_readable(self):
        c = _client(lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertTrue(out.startswith("调用出错："))
        self.assertIn("401", out)

    def test_connection_error_is_readable(self):
        def handler(req):
            raise httpx.ConnectError("boom", request=req)

        c = _client(handler)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertTrue(out.startswith("调用出错："))

    def test_transport_error_does_not_mark_unsupported(self):
        """传输层失败（连接被拒/断开）不许扣「服务端拒绝参数」的帽子：那会永久去掉扩展字段。

        判据只看 HTTP 400/422（``_strip_on_reject``）；连接失败连响应都没有，更不该重试。
        """
        calls = []

        def handler(req):
            calls.append(req)
            raise httpx.ConnectError("boom", request=req)

        c = _client(handler, extra_body={"top_k": 40})
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertTrue(out.startswith("调用出错："))
        self.assertFalse(c._extra_body_unsupported)
        self.assertEqual(c.extra_body, {"top_k": 40})
        self.assertEqual(len(calls), 1)                       # 传输层失败不做重试

    def test_rate_limit_503_and_timeout_messages(self):
        """429 / 503 / 超时各自给可读中文：运维看文案能分辨原因，玩家只看到「调用出错：…」。"""
        c = _client(lambda req: httpx.Response(429, json={"error": {"message": "slow down"}}))
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertIn("429", out)
        self.assertIn("限流", out)

        c = _client(lambda req: httpx.Response(503, json={"error": {"message": "down"}}))
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertIn("503", out)

        def handler(req):
            raise httpx.ReadTimeout("slow", request=req)

        c = _client(handler, timeout_sec=7.0)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertIn("超时", out)
        self.assertIn("7 秒", out)

    def test_400_strips_extra_body_and_retries_once(self):
        seen = []

        def handler(req):
            body = json.loads(req.content.decode("utf-8"))
            seen.append(body)
            if "reasoning_effort" in body:
                return httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}})
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=2)
        c.extra_body = {"reasoning_effort": "high"}          # 模拟配置里的扩展字段
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        self.assertIn("reasoning_effort", seen[0])
        self.assertNotIn("reasoning_effort", seen[1])
        self.assertEqual(c.extra_body, {})                   # 记住服务端不认扩展字段
        self.assertTrue(c._extra_body_unsupported)

    def test_422_also_strips_extra_body(self):
        """某些 OpenAI 兼容服务用 422 表达「不认这个字段」，安全网要一并覆盖。"""
        seen = []

        def handler(req):
            body = json.loads(req.content.decode("utf-8"))
            seen.append(body)
            if "top_k" in body:
                return httpx.Response(422, json={"error": {"message": "unexpected field top_k"}})
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, extra_body={"top_k": 40})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}])), "好")
        self.assertIn("top_k", seen[0])
        self.assertNotIn("top_k", seen[1])

    def test_budget_binding_keeps_extra_body_keys(self):
        """按调用 bind 时用户扩展字段不许被挤掉：两者都要在请求体里。"""
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, extra_body={"top_k": 40})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100)), "好")
        self.assertEqual(seen[0]["top_k"], 40)
        self.assertEqual(seen[0]["max_tokens"], 100)

    def test_explicit_max_tokens_wins(self):
        """配置里写死了 max_tokens：注入的预算必须让位（尊重显式配置）。"""
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, extra_body={"max_tokens": 7})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100)), "好")
        self.assertEqual(seen[0]["max_tokens"], 7)

    def test_explicit_max_completion_tokens_wins(self):
        """配置里写死了 max_completion_tokens：连 max_tokens 都不许注入。"""
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, extra_body={"max_completion_tokens": 9})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100)), "好")
        self.assertEqual(seen[0]["max_completion_tokens"], 9)
        self.assertNotIn("max_tokens", seen[0])

    def test_400_on_max_tokens_recovers_without_budget(self):
        """只认 max_completion_tokens 的服务端会 400 拒绝 max_tokens：去掉重试必须救回来。"""
        seen = []

        def handler(req):
            body = json.loads(req.content.decode("utf-8"))
            seen.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: max_tokens"}})
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler)          # 用户没配扩展字段：注入的 max_tokens 就是唯一的「加料」
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        self.assertEqual(seen[0]["max_tokens"], 100)
        self.assertNotIn("max_tokens", seen[1])
        self.assertTrue(c._extra_body_unsupported)

    def test_unconfigured_base_url_never_reaches_network(self):
        """未配置地址（config.json 可能留空）：必须立刻返回可读错误，不许真连外网。

        openai 客户端会把空 base_url 静默退回 https://api.openai.com/v1，所以这里用
        「一被调用就断言失败」的假客户端锁住「一个请求都不许发」。
        """
        def handler(req):
            raise AssertionError("不该发出请求：" + str(req.url))

        c = _client(handler, base_url="")
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        self.assertTrue(out.startswith("调用出错："))
        self.assertIn("llm_base_url", out)

    def test_no_think_flag_still_accepted(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("嗯"))

        c = _client(handler, think_mode=1)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 32, True))
        self.assertEqual(out, "嗯")
        self.assertEqual(seen[0]["max_tokens"], 32)   # no_think：不加推理预算

    def test_reason_budget_ladder(self):
        """思考档位 1/2 各追加一段推理预算（+160 / +800）；档位 0 与 no_think 都不追加。"""
        from app.llm import REASON_BUDGET_MED, REASON_BUDGET_STRONG
        self.assertEqual((REASON_BUDGET_MED, REASON_BUDGET_STRONG), (160, 800))
        c = _client(lambda req: httpx.Response(200, json=_reply("好")))
        self.assertEqual(c._budget(100, False), 100)
        c.think_mode = 1
        self.assertEqual(c._budget(100, False), 260)
        c.think_mode = 2
        self.assertEqual(c._budget(100, False), 900)
        self.assertEqual(c._budget(100, True), 100)   # no_think 优先：强思考也不追加


class ChainTest(unittest.TestCase):
    """链属性与参数转发：Task 4/5 会复用 ``chain``（bind_tools / astream / 结构化输出）。"""

    def test_chain_is_chat_openai(self):
        from langchain_openai import ChatOpenAI
        c = _client(lambda req: httpx.Response(200, json=_reply("好")))
        self.assertIsInstance(c.chain, ChatOpenAI)

    def test_timeout_sec_reaches_chain(self):
        c = _client(lambda req: httpx.Response(200, json=_reply("好")), timeout_sec=7.5)
        self.assertEqual(c.chain.request_timeout, 7.5)

    def test_extra_body_wired_at_construction(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=2, extra_body={"reasoning_effort": "high"})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}])), "好")
        self.assertEqual(seen[0]["reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
