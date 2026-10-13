"""LLMClient 移植校验：文本清洗、请求体重试阶梯（不触网，注入假 _post）。"""

import asyncio
import unittest

from app.llm import FAIL_TEXT, REASON_BUDGET_MED, LLMClient


def _content(text):
    return {"choices": [{"message": {"content": text}}]}


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


class BodyLadderTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_think_mode1_injects_budget(self):
        c = LLMClient(think_mode=1)
        seen = []

        async def fake(body):
            seen.append(dict(body))
            return _content("好")

        c._post = fake
        out = self._run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        self.assertEqual(seen[0]["max_tokens"], 100 + REASON_BUDGET_MED)
        self.assertEqual(seen[0]["reasoning_budget_tokens"], REASON_BUDGET_MED)
        self.assertTrue(seen[0]["chat_template_kwargs"]["enable_thinking"])

    def test_think_mode1_no_think_skips_reasoning(self):
        c = LLMClient(think_mode=1)
        seen = []

        async def fake(body):
            seen.append(dict(body))
            return _content("好")

        c._post = fake
        out = self._run(c.chat([{"role": "user", "content": "hi"}], 32, True))
        self.assertEqual(out, "好")
        self.assertFalse(seen[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("reasoning_budget_tokens", seen[0])
        self.assertEqual(seen[0]["max_tokens"], 32)

    def test_mode2_adds_budget_no_reasoning_keys(self):
        c = LLMClient(think_mode=2)
        seen = []

        async def fake(body):
            seen.append(dict(body))
            return _content("好")

        c._post = fake
        self._run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(seen[0]["max_tokens"], 900)
        self.assertNotIn("reasoning_budget_tokens", seen[0])

    def test_400_strips_think_params_and_remembers(self):
        c = LLMClient(think_mode=1)
        seen = []

        async def fake(body):
            seen.append(dict(body))
            if len(seen) == 1:
                return {"_error": "HTTP 400（检查模型名是否为 qwen3.5）"}
            return _content("好")

        c._post = fake
        out = self._run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        self.assertTrue(c._think_params_unsupported)
        self.assertNotIn("chat_template_kwargs", seen[1])

    def test_connect_failure_not_treated_as_unsupported(self):
        c = LLMClient(think_mode=1)

        async def fake(body):
            return {"_error": "LLM 连接被中断（http://127.0.0.1:8080/v1），可能服务刚重启，稍等再试"}

        c._post = fake
        out = self._run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertTrue(out.startswith("调用出错："))
        self.assertFalse(c._think_params_unsupported)

    def test_empty_content_falls_back_to_fail_text(self):
        c = LLMClient(think_mode=0)

        async def fake(body):
            return _content("")

        c._post = fake
        out = self._run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, FAIL_TEXT)

    def test_localhost_normalized_once(self):
        c = LLMClient(base_url="http://localhost:8080/v1")
        c._ensure_base()
        c._ensure_base()
        self.assertEqual(c.base_url, "http://127.0.0.1:8080/v1")

    def test_local_target_detection(self):
        self.assertTrue(LLMClient(base_url="http://127.0.0.1:1/v1")._local_target())
        self.assertFalse(LLMClient(base_url="https://api.example.com/v1")._local_target())


if __name__ == "__main__":
    unittest.main()
