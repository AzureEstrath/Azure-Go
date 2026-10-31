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

from app.llm import LLMClient, REASON_HEADROOM_DEFAULT


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


class PerCallThinkTest(unittest.TestCase):
    """v4.2.8：per-call 快速档必须同时去掉思考扩展字段（否则聊天仍会开思考、卡 20 秒）。"""

    def test_fast_call_restores_base_think_flag(self):
        """基础配置显式 `enable_thinking:false` 时，快速档必须把它带回去（不是删掉整块）。"""
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=1,
                    base_extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                    think_extra_body={"chat_template_kwargs": {"enable_thinking": True}})
        asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100, think_mode=0))
        self.assertEqual(seen[0]["chat_template_kwargs"]["enable_thinking"], False)

    def test_fast_call_strips_think_extra_body(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=1,
                    think_extra_body={"chat_template_kwargs": {"enable_thinking": True}})
        asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100, think_mode=0))
        self.assertNotIn("chat_template_kwargs", seen[0])
        self.assertIn("max_tokens", seen[0])

    def test_no_think_flag_also_strips_think_extra_body(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=1,
                    think_extra_body={"reasoning_effort": "high"})
        asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100, no_think=True))
        self.assertNotIn("reasoning_effort", seen[0])


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
        self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)
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
        """空内容 + ``finish_reason=stop``（不是 length）：直接兜底，**不许**重试。"""
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(200, json=_reply(""))

        c = _client(handler)
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}])),
                         "唔...我脑子有点转不过来了")
        self.assertEqual(len(calls), 1)

    def test_length_finish_with_empty_content_retries_without_budget(self):
        """推理段烧光预算（finish_reason=length + 空 content）⇒ 去掉输出上限重试一次，台词照常。"""
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            if len(seen) == 1:
                return httpx.Response(200, json={
                    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "finish_reason": "length",
                                 "message": {"role": "assistant", "content": ""}}]})
            return httpx.Response(200, json=_reply("重试后的台词"))

        c = _client(handler)
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 80)),
                         "重试后的台词")
        self.assertEqual(len(seen), 2)
        self.assertIn("max_tokens", seen[0])
        self.assertNotIn("max_tokens", seen[1])        # 重试这一次不许再带上限

    def test_length_finish_retry_also_empty_falls_back(self):
        """重试仍空 ⇒ 落兜底台词（只重试一次，不许循环）。"""
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": ""}}]})

        c = _client(handler)
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 80)),
                         "唔...我脑子有点转不过来了")
        self.assertEqual(len(calls), 2)

    def test_length_retry_failure_is_readable(self):
        """重试那一次失败（HTTP 500）：返回可读错误、总共 2 次请求。"""
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, json={
                    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "finish_reason": "length",
                                 "message": {"role": "assistant", "content": ""}}]})
            return httpx.Response(500, json={"error": {"message": "boom"}})

        c = _client(handler)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 80))
        self.assertTrue(out.startswith("调用出错："))
        self.assertIn("HTTP 500", out)
        self.assertEqual(len(calls), 2)

    def test_length_after_rejected_params_does_not_resend_same_request(self):
        """400 之后的重试本身就是裸请求：再命中「空 + length」不许把同形请求再发一遍。"""
        seen = []

        def handler(req):
            body = json.loads(req.content.decode("utf-8"))
            seen.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: max_tokens"}})
            return httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": ""}}]})

        c = _client(handler)
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 80)),
                         "唔...我脑子有点转不过来了")
        self.assertEqual(len(seen), 2)              # 400 + 一条裸重试，到此为止

    def test_budget_for_public_entry_matches_ladder(self):
        """工具轮 / 流式通道共用 ``budget_for``：与 ``chat()`` 同一套推理余量（== _budget(n, False)）。"""
        c = _client(lambda req: httpx.Response(200, json=_reply("好")))
        self.assertEqual(c.budget_for(80), 80 + REASON_HEADROOM_DEFAULT)
        c.think_mode = 2
        self.assertEqual(c.budget_for(80), 80 + REASON_HEADROOM_DEFAULT + 800)

    def test_truncated_but_nonempty_reply_is_used_as_is(self):
        """现状契约（防回归）：有内容就算被截断也照用，不许重试——重试只针对「空 content」。"""
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": "这手很实"}}]})

        c = _client(handler)
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}], 80)), "这手很实")
        self.assertEqual(len(calls), 1)

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
        self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)

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
        self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)
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
        """推理余量是**底**，思考档位 1/2 再各叠加 +160 / +800；no_think 一个都不加（阶梯必须单调）。"""
        from app.llm import REASON_BUDGET_MED, REASON_BUDGET_STRONG
        self.assertEqual((REASON_BUDGET_MED, REASON_BUDGET_STRONG), (160, 800))
        c = _client(lambda req: httpx.Response(200, json=_reply("好")))
        self.assertEqual(c._budget(100, False), 100 + REASON_HEADROOM_DEFAULT)
        c.think_mode = 1
        self.assertEqual(c._budget(100, False), 100 + REASON_HEADROOM_DEFAULT + 160)
        c.think_mode = 2
        self.assertEqual(c._budget(100, False), 100 + REASON_HEADROOM_DEFAULT + 800)
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

    def test_strong_thinking_has_a_longer_timeout_chain(self):
        """v4.4.1：强思考不再被普通请求的 45 秒超时打断。"""
        c = _client(lambda req: httpx.Response(200, json=_reply("好")), timeout_sec=7.5)
        self.assertEqual(c._sync_long_chain().request_timeout, 90.0)
        c.timeout_sec = 120.0
        c._built_long_extra_body = None
        self.assertEqual(c._sync_long_chain().request_timeout, 120.0)

    def test_extra_body_wired_at_construction(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler, think_mode=2, extra_body={"reasoning_effort": "high"})
        self.assertEqual(asyncio.run(c.chat([{"role": "user", "content": "hi"}])), "好")
        self.assertEqual(seen[0]["reasoning_effort"], "high")


class ThinkModeRuntimeTest(unittest.TestCase):
    """思考强度运行时可调（v3.11.0）：改档位 ⇒ 扩展字段跟着进 / 出，下一次调用即生效。

    覆盖两条不变量：① 基础字段永远在；② 思考字段只在档位 > 0 时并入（改回 0 必须**撤掉**，
    不能用旧合并结果再叠一次）。
    """

    def test_merge_and_withdraw(self):
        c = LLMClient(model="m", api_key="k", think_mode=0,
                      base_extra_body={"top_k": 40},
                      think_extra_body={"reasoning_effort": "high"})
        self.assertEqual(c.extra_body, {"top_k": 40})
        c.set_think_mode(2)
        self.assertEqual(c.extra_body, {"top_k": 40, "reasoning_effort": "high"})
        c.set_think_mode(0)
        self.assertEqual(c.extra_body, {"top_k": 40})

    def test_clamps_and_ignores_dirty(self):
        c = LLMClient(model="m", api_key="k", think_mode=1, base_extra_body={"a": 1})
        self.assertEqual(c.set_think_mode(9), 2)
        self.assertEqual(c.think_mode, 2)
        self.assertEqual(c.set_think_mode("oops"), 2)     # 脏值：保持原档，不抛
        self.assertEqual(c.set_think_mode(-1), 0)

    def test_extra_body_shape_after_withdraw(self):
        """撤档后请求体里不能再有思考字段（只测合并结果，不发请求）。"""
        c = LLMClient(model="m", api_key="k", think_mode=2,
                      base_extra_body={"top_k": 40},
                      think_extra_body={"reasoning_effort": "high"})
        c.set_think_mode(0)
        self.assertNotIn("reasoning_effort", c.extra_body)

    def test_legacy_single_extra_body_untouched(self):
        """不传两份原料时保持旧行为：set_think_mode 只改档位，不动 extra_body。"""
        c = LLMClient(model="m", api_key="k", think_mode=0, extra_body={"top_k": 40})
        c.set_think_mode(2)
        self.assertEqual(c.extra_body, {"top_k": 40})


if __name__ == "__main__":
    unittest.main()
