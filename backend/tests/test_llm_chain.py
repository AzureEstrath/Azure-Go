"""链构造与请求体形状：不触网（MockTransport 注入）。"""

import asyncio
import inspect
import json
import os
import sys
import unittest

import httpx
import openai

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AzureConfig
from app.llm_chain import build_chat_model, content_of, merge_extra_body


class _Cfg:
    def __init__(self, **kw):
        self.llm_base_url = "https://api.example.com/v1"
        self.llm_model = "some-cloud-model"
        self.llm_api_key = "sk-test"
        self.llm_think_mode = 0
        self.llm_extra_body = {}
        self.llm_think_extra_body = {}
        for k, v in kw.items():
            setattr(self, k, v)


def _capture(cfg, reply=None):
    """返回 (model, seen)；seen 里记录每次请求的 JSON body。

    同步 invoke 走 http_client，异步 ainvoke 只认 http_async_client（langchain-openai 1.x
    的异步路径不看 http_client），两个都注入才能保证全程不触网。
    """
    seen = []

    def handler(req):
        seen.append(json.loads(req.content.decode("utf-8")))
        body = reply if reply is not None else {"id": "x", "object": "chat.completion",
            "created": 0, "model": cfg.llm_model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "好"}}]}
        return httpx.Response(200, json=body)

    model = build_chat_model(
        cfg,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return model, seen


class MergeExtraBodyTest(unittest.TestCase):
    def test_empty_by_default(self):
        self.assertEqual(merge_extra_body(_Cfg()), {})

    def test_base_merged_always(self):
        cfg = _Cfg(llm_extra_body={"top_k": 40})
        self.assertEqual(merge_extra_body(cfg), {"top_k": 40})

    def test_think_extra_only_when_thinking(self):
        cfg = _Cfg(llm_extra_body={"top_k": 40}, llm_think_extra_body={"reasoning_effort": "high"})
        self.assertEqual(merge_extra_body(cfg), {"top_k": 40})
        cfg.llm_think_mode = 2
        self.assertEqual(merge_extra_body(cfg), {"top_k": 40, "reasoning_effort": "high"})

    def test_config_ignores_non_dict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"llm_extra_body": "oops", "llm_think_extra_body": [1, 2]}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.llm_extra_body, {})
        self.assertEqual(c.llm_think_extra_body, {})


class BuildModelTest(unittest.TestCase):
    def test_request_body_shape(self):
        model, seen = _capture(_Cfg())
        asyncio.run(model.ainvoke([{"role": "user", "content": "hi"}]))
        b = seen[0]
        self.assertEqual(b["model"], "some-cloud-model")
        self.assertEqual(b["temperature"], 0.7)
        self.assertEqual(b["top_p"], 0.9)
        self.assertEqual(b["frequency_penalty"], 0.4)
        self.assertEqual(b["presence_penalty"], 0.2)
        self.assertNotIn("cache_prompt", b)          # llama-server 私有参数不再默认发送

    def test_extra_body_reaches_request(self):
        model, seen = _capture(_Cfg(llm_extra_body={"top_k": 40}, llm_think_mode=2,
                                    llm_think_extra_body={"reasoning_effort": "high"}))
        asyncio.run(model.ainvoke([{"role": "user", "content": "hi"}]))
        self.assertEqual(seen[0]["top_k"], 40)
        self.assertEqual(seen[0]["reasoning_effort"], "high")

    def test_uses_configured_base_url(self):
        cfg = _Cfg()
        m = build_chat_model(cfg)
        self.assertIn("api.example.com", str(m.openai_api_base))

    def test_empty_api_key_builds_but_fails_at_request(self):
        """模板里 llm_api_key 为空：构造必须成功，失败推迟到请求期（哨兵 key）。"""
        def handler(req):
            return httpx.Response(401, json={"error": {"message": "Missing credentials"}})

        model = build_chat_model(
            _Cfg(llm_api_key=""),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.assertIn("api.example.com", str(model.openai_api_base))    # 构造期不抛
        with self.assertRaises(openai.OpenAIError):                     # 请求期才失败
            asyncio.run(model.ainvoke([{"role": "user", "content": "hi"}]))

    def test_content_of_keeps_content_only(self):
        from langchain_core.messages import AIMessage
        ai = AIMessage(content="你好~", additional_kwargs={"reasoning_content": "内心戏"})
        self.assertEqual(content_of(ai), "你好~")

    def test_content_of_strips_think_tags(self):
        from langchain_core.messages import AIMessage
        self.assertEqual(content_of(AIMessage(content="<" + "think" + ">推理" + "<" + "/" + "think" + ">回答")), "回答")


if __name__ == "__main__":
    unittest.main()
