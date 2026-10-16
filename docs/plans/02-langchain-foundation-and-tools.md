# 计划 02：LangChain 地基（LCEL）+ 工具层 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把后端 LLM 调用从 httpx 手写编排换成 LangChain（`ChatOpenAI` + LCEL），并新增第一层工具：**发言侧**由她自己决定要不要复算、查定式、核对盘面。

**Architecture:** `llm.py` 退化为“链的薄适配器”（保留 `chat()` 对外签名，内部走链）；新增 `llm_chain.py`（模型构造、错误映射、extra_body 合并）与 `tools.py`（三个工具 + 受限 tool loop）。**工具只影响她怎么说，不影响她下哪里**：落点仍由 `agent._pick_ai_move(after_infos)` 决定（改落点属计划 04）。所有既有硬规则（推理段不显示、8 条窗口去重、音画同步）不变。

**Tech Stack:** `langchain-core` / `langchain-openai`（`ChatOpenAI`、`ChatPromptTemplate`、`RunnableBranch`、`with_structured_output`、`bind_tools`）；`openai` 客户端的 `http_client` 参数用于注入 `httpx.MockTransport` 做无网测试。

**Spec:** `docs/backend-langchain-plan.md`（决策 D2/D3；硬规则清单）

## Global Constraints

- 前置：计划 0、01 已完成（`config.py` 已无本地服务与 `katago_*` 文件字段；KataGo 已是云服务客户端）。
- 测试一律 `unittest`：`cd backend && python -m unittest discover -s tests`；不得引入 pytest。
- 测试**不得触网**：一律 `http_client=httpx.Client(transport=httpx.MockTransport(handler))` 注入。
- **推理段永不显示给玩家**：只取 `AIMessage.content`，不得把 `additional_kwargs.reasoning_content` / `response_metadata` 拼进台词。
- `AzureAgent.clean_reply()`（去舞台提示）与 `llm.clean_content()`（去 think 段）仍是所有台词的必经之路。
- 工具**绝不允许抛异常**：任何失败都要转成可读的 `ToolMessage`，且必须保证 `busy` 能释放。
- 工具总次数上限 `MAX_TOOL_CALLS = 2`，单工具超时 `TOOL_TIMEOUT_SEC = 10`（超时即返回超时文本，不阻塞回合）。
- LLM 不可用时的兜底行为保持现状：返回 `{"error"}` 等价的可读中文 → `agent._llm_failed()` 判为失败 → 走兜底台词。
- 注释、提交信息一律中文；提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.2.0**，同步写进 `README.md` 的「更新」小节。

## Review Focus

- **云端模型的推理段**：DeepSeek-R1 类模型把推理放在 `reasoning_content`——它绝不能出现在气泡里（历史上 v1.3.1 修过一次同类事故）。
- **`extra_body` 被服务端拒绝**：某些 OpenAI 兼容服务遇到未知字段回 400/422——必须仍保留“去掉扩展字段重试一次”的安全网，不能因此失败。
- **工具循环失控**：模型连续请求工具、或工具超时 → 必须在 `MAX_TOOL_CALLS` 处硬停并照常出话，`busy` 归位。
- **工具输出当台词**：工具返回的原始 JSON/盘面文本绝不能直接投给玩家，必须经 `reply` 生成自然语言。
- **开场/闲置等短回合的时延**：工具只在既定两个调用点启用，不得让 `chat`/`idle` 等路径也走 tool loop 而变慢。

---

### Task 1: `llm_chain.py` —— 模型构造与扩展字段

**Files:**
- Create: `backend/app/llm_chain.py`
- Modify: `backend/app/config.py`（新增 `llm_extra_body: dict = {}`、`llm_think_extra_body: dict = {}`，从 `config.json` 同名键读入，非 dict 时忽略）
- Modify: `backend/requirements.txt`（`+ langchain-core>=0.3`、`+ langchain-openai>=0.2`）
- Create: `backend/tests/test_llm_chain.py`

**Interfaces:**
- Consumes: `AzureConfig`（`llm_base_url/llm_model/llm_api_key/llm_think_mode/llm_extra_body/llm_think_extra_body`）
- Produces:
  - `build_chat_model(cfg, *, http_client=None, temperature=0.7, top_p=0.9, frequency_penalty=0.4, presence_penalty=0.2) -> ChatOpenAI`
  - `merge_extra_body(cfg) -> dict`（`llm_extra_body` 打底；`think_mode > 0` 时并入 `llm_think_extra_body`）
  - 常量 `FAIL_TEXT = "唔...我脑子有点转不过来了"`、`REASON_KEYS = ("reasoning_content", "reasoning")`
  - `content_of(ai_message) -> str`（只取 `content`，去掉 `thinking` 段后 strip）

- [ ] **Step 1: 写失败测试** — `backend/tests/test_llm_chain.py`

```python
"""链构造与请求体形状：不触网（MockTransport 注入）。"""

import asyncio
import inspect
import json
import os
import sys
import unittest

import httpx

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
    """返回 (model, seen)；seen 里记录每次请求的 JSON body。"""
    seen = []

    def handler(req):
        seen.append(json.loads(req.content.decode("utf-8")))
        body = reply if reply is not None else {"id": "x", "object": "chat.completion",
            "created": 0, "model": cfg.llm_model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "好"}}]}
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return build_chat_model(cfg, http_client=client), seen


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

    def test_content_of_keeps_content_only(self):
        from langchain_core.messages import AIMessage
        ai = AIMessage(content="你好~", additional_kwargs={"reasoning_content": "内心戏"})
        self.assertEqual(content_of(ai), "你好~")

    def test_content_of_strips_think_tags(self):
        from langchain_core.messages import AIMessage
        self.assertEqual(content_of(AIMessage(content="<" + "think" + ">推理" + "<" + "/" + "think" + ">回答")), "回答")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pip install -r requirements.txt && python -m unittest tests.test_llm_chain -v`
Expected: FAIL（`No module named 'app.llm_chain'`）

- [ ] **Step 3: 实施** — `llm_chain.py`：`ChatOpenAI(model=..., base_url=..., api_key=..., temperature=..., top_p=..., frequency_penalty=..., presence_penalty=..., timeout=..., http_client=http_client, **({"extra_body": e} if e else {}))`；`content_of()` 复用旧 `clean_content()` 的 think 剥离逻辑（把该函数从 `llm.py` 搬来，`llm.py` 改为 `from .llm_chain import content_of as clean_content` 以保留旧引用）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_llm_chain -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/llm_chain.py backend/app/config.py backend/requirements.txt backend/tests/test_llm_chain.py
git commit -m "feat(backend,llm): 引入 LangChain ChatOpenAI 与可配置 extra_body（v3.2.0）"
```

---

### Task 2: `llm.py` 改为链的薄适配器（保留 `chat()` 契约）

**Files:**
- Modify: `backend/app/llm.py`（内部改走链；删除本地预检 `_reachable/_try_connect/_resolve_addrs/_ensure_base/_local_target` 与 `localhost→127.0.0.1` 改写、`probe_models` 改为用链的 `models` 探测或直接删除调用点）
- Modify: `backend/app/main.py`（删 `asyncio.create_task(cli.probe_models())` 一行；`/api/health` 的 `llm_url` 保留）
- Rewrite: `backend/tests/test_llm.py`

**Interfaces:**
- Consumes: `build_chat_model`、`merge_extra_body`、`content_of`（Task 1）
- Produces:
  - `LLMClient(base_url, model, api_key, timeout_sec=120.0, think_mode=0, http_client=None)`
  - 属性 `LLMClient.chain`（`ChatOpenAI` 实例，供 `bind_tools` / `astream` / `with_structured_output` 复用）、`LLMClient.extra_body: dict`（可写；400/422 后被清空并记入 `_extra_body_unsupported`）
  - `async chat(messages, max_tokens=300, no_think=False) -> str`（失败返回 `"调用出错：" + 原因`，全部失败返回 `FAIL_TEXT` 的语义保留）
  - `clean_content` 仍可 `from app.llm import clean_content`

- [ ] **Step 1: 重写测试** — `backend/tests/test_llm.py`

```python
"""LLMClient（LangChain 版）校验：文本清洗、错误映射、400 降级安全网。不触网。"""

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
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMClient(base_url="https://api.example.com/v1", model="m", api_key="k", http_client=http, **kw)


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


class ChatTest(unittest.TestCase):
    def test_plain_reply(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("好"))

        c = _client(handler)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 100))
        self.assertEqual(out, "好")
        self.assertEqual(seen[0]["max_tokens"], 100)

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

    def test_no_think_flag_still_accepted(self):
        seen = []

        def handler(req):
            seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json=_reply("嗯"))

        c = _client(handler, think_mode=1)
        out = asyncio.run(c.chat([{"role": "user", "content": "hi"}], 32, True))
        self.assertEqual(out, "嗯")
        self.assertEqual(seen[0]["max_tokens"], 32)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_llm -v`
Expected: FAIL（`LLMClient` 不接受 `http_client`；`extra_body` 属性不存在；401 文案不匹配）

- [ ] **Step 3: 实施** — `llm.py`：构造时 `self.chain = build_chat_model(cfg_like, http_client=...)`；`chat()` 用 `await self.chain.ainvoke(messages, max_tokens=...)`，把 `ChatOpenAI` 的异常与 HTTP 状态映射成 `"调用出错：HTTP 401（检查 llm_api_key）"` 这类可读中文；`extra_body` 作为可写属性（`test_400_strips_extra_body_and_retries_once` 会改它），首次 400/422 时置空并重试一次（并记住 `self._extra_body_unsupported = True`）。`max_tokens` 逻辑简化为：`think_mode==1` → `max_tokens + 160`；`think_mode==2` → `max_tokens + 800`；`no_think=True` → 原样，**不再注入 llama-server 私有字段**（改为由 `extra_body` 配置表达）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_llm tests.test_agent tests.test_main -v`
Expected: 全 PASS（`StubLLM` 无需改动——`agent` 只依赖 `chat()` 鸭子类型）

- [ ] **Step 5: 提交**

```bash
git add backend/app/llm.py backend/app/main.py backend/tests/test_llm.py
git commit -m "refactor(backend,llm): llm.py 改为 LangChain 链适配器，去掉本地预检（v3.2.0）"
```

---

### Task 3: 七阶段提示链化（`ChatPromptTemplate` + `RunnableBranch`）

**Files:**
- Create: `backend/app/prompt_chains.py`
- Create: `backend/tests/test_prompt_chains.py`
- Modify: `backend/app/agent.py`（`_sys_msg/_sys_teach_msg` 改为经链构造；`with_context(...)` 的**文本**产出保持不变，只作为模板变量传入）

**Interfaces:**
- Consumes: `prompts.system_prompt/phase_note/mood_note/with_context/context_block`
- Produces:
  - `PHASES = ("observe", "after_player", "own_turn", "just_moved", "analyze", "chat", "summary")`
  - `build_messages(phase, *, context="", prompt="", len_note="", mood_name="", think_hint="", history=None) -> list[dict]`
    - **`context` 与 `prompt` 的语义**：`context` 是 `prompts.with_context(...)` 的**完整产出**（已含【现在】+ prompt），非空时直接作为最后一条 user 消息内容（现状路径）；为空时才用裸 `prompt`（供计划 04 的图节点复用）。返回 `[{"role":"system",...}, *history, {"role":"user",...}]`，与旧 `_sys_msg() + _user_msg(with_context(...))` **逐字符等价**由测试锁定。
  - `phase_chain(phase) -> Runnable`（LCEL 组合，便于 tracing 与替换）

- [ ] **Step 1: 写失败测试** — `backend/tests/test_prompt_chains.py`

```python
"""阶段链：系统提示与用户消息的拼接必须与改造前逐字符一致。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prompts, prompt_chains


class PhaseMessagesTest(unittest.TestCase):
    def setUp(self):
        class C:
            prompt_user_name = "Estarth"
            prompt_system = ""
            prompt_phase_notes = {}
        prompts.configure(C())

    def test_phases_covered(self):
        self.assertEqual(set(prompt_chains.PHASES),
                         {"observe", "after_player", "own_turn", "just_moved", "analyze", "chat", "summary"})

    def test_system_message_prefix(self):
        msgs = prompt_chains.build_messages("observe", mood_name="平静", prompt="P")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertTrue(msgs[0]["content"].startswith(prompts.system_prompt()))
        self.assertIn(prompts.mood_note("平静"), msgs[0]["content"])

    def test_user_message_is_context_plus_prompt(self):
        moves = [["B", "Q16"], ["W", "D4"]]
        ctx = prompts.with_context(moves, [], [], "P", "observe", False, "", None)
        msgs = prompt_chains.build_messages("observe", context=ctx, prompt="P")
        self.assertEqual(msgs[-1], {"role": "user", "content": ctx})

    def test_history_inserted_between(self):
        hist = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的"}]
        msgs = prompt_chains.build_messages("chat", context="C", prompt="P", history=hist)
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])

    def test_len_note_appended(self):
        msgs = prompt_chains.build_messages("after_player", prompt="P", len_note="【短】")
        self.assertTrue(msgs[0]["content"].endswith("【短】"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_prompt_chains -v`
Expected: FAIL（`No module named 'app.prompt_chains'`）

- [ ] **Step 3: 实施** — `prompt_chains.py` 用 `ChatPromptTemplate.from_messages` + `RunnableBranch` 组合七个阶段；`build_messages()` 直接用 `format_messages()` 产出，保证与旧拼接顺序一致（system → history → user）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_prompt_chains tests.test_agent -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/prompt_chains.py backend/app/agent.py backend/tests/test_prompt_chains.py
git commit -m "feat(backend,prompts): 七阶段提示改为 LangChain 模板链（v3.2.0）"
```

---

### Task 4: 情绪增量结构化输出（保留正则兜底）

**Files:**
- Modify: `backend/app/llm.py`（新增 `async mood_delta(...) -> tuple` 或返回解析结果的辅助方法）
- Modify: `backend/app/agent.py`（`_mood_llm_refine()` 优先用结构化输出；失败回落到 `EmotionModel.parse_delta`)
- Create: `backend/tests/test_mood_delta.py`

**Interfaces:**
- Consumes: `EmotionModel.parse_delta(text)`（正则兜底，签名不变）
- Produces: `class MoodDelta(BaseModel)`（字段 `dv: float`、`dc: float`、`da: float`，各自 `-0.40 ~ 0.40`）；`LLMClient.mood_delta(cur: tuple, region_name: str, desc: str) -> list[float] | None`（`None` 表示拿不到，调用方回落到正则）

- [ ] **Step 1: 写失败测试** — `backend/tests/test_mood_delta.py`

```python
"""结构化情绪增量：能解析则用，解析不了返回 None（由调用方回落正则）。"""

import asyncio
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import LLMClient


def _tool_call_reply(args_json):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "MoodDelta", "arguments": args_json}}]}}]}


def _client(handler):
    return LLMClient(base_url="https://api.example.com/v1", model="m", api_key="k",
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class MoodDeltaTest(unittest.TestCase):
    def test_structured_parse_success(self):
        c = _client(lambda req: httpx.Response(200, json=_tool_call_reply(json.dumps(
            {"dv": 0.1, "dc": -0.05, "da": 0.2}))))
        out = asyncio.run(c.mood_delta((0.5, 0.5, 0.5), "平静", "他夸我了"))
        self.assertAlmostEqual(out[0], 0.1, places=6)
        self.assertAlmostEqual(out[2], 0.2, places=6)

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_mood_delta -v`
Expected: FAIL（`LLMClient.mood_delta` 不存在）

- [ ] **Step 3: 实施** — 用 `ChatOpenAI(...).with_structured_output(MoodDelta)`；把结果裁剪到 `[-0.4, 0.4]`；任何异常返回 `None`。`agent._mood_llm_refine()` 改为：结构化失败 → `EmotionModel.parse_delta` 原有逻辑（保持 v2 行为不变）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_mood_delta tests.test_emotion tests.test_agent -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/llm.py backend/app/agent.py backend/tests/test_mood_delta.py
git commit -m "feat(backend,emotion): 情绪增量改用结构化输出，保留正则兜底（v3.2.0）"
```

---

### Task 5: 工具层与受限 tool loop（B1）

**Files:**
- Create: `backend/app/tools.py`
- Modify: `backend/app/events.py`（新增 `tool(phase, name, args, ok, ms)` 事件构造器）
- Modify: `backend/app/agent.py`（新增 `_tool_loop()`；只在 `hint()` 与 `_ai_turn()` 的 `think_msg` 两处启用工具）
- Modify: `backend/app/main.py`（`POST /api/config` 支持 `tools_enabled`；默认 `False` → 行为等同改造前）
- Modify: `backend/app/config.py`（`tools_enabled=False`、`max_tool_calls=2`）
- Create: `backend/tests/test_tools.py`
- Modify: `game/scripts/net/NetClient.gd`（`_handle_packet` 新增 `tool` → 新信号 `tool_event(payload)`）、`game/scripts/ui/Main.gd`（`_on_tool_event` 只更新状态栏文案）

**Interfaces:**
- Consumes: `KataGoEngine.analyze(moves)`、`prompts.board_map/board_summary`、`JosekiData.LIST/JosekiRecognizer.scan(moves)`（后端侧 `app/joseki.py`）
- Produces:
  - `TOOLS = [katago_analyze, joseki_lookup, board_query]`（LangChain `@tool` 装饰，名称即模型可见名）
  - `AzureAgent.chain`（LangChain Runnable，`llm.chain` 的引用；测试可替换）、`AzureAgent.tools_enabled: bool`（默认 `False`）、`AzureAgent.max_tool_calls: int`（默认取 `cfg.max_tool_calls`）
  - `async _tool_loop(self, messages, max_tokens, phase, tools=None) -> str`（`agent` 内部；`MAX_TOOL_CALLS` 硬上限）
  - 事件：`{"type": "tool", "phase": "start"|"end", "name": str, "args": dict, "ok": bool, "ms": int}`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_tools.py`

```python
"""工具层：三个工具的形状与失败吞咽；tool loop 的预算与事件。"""

import asyncio
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_tools -v`
Expected: FAIL（`No module named 'app.tools'`）

- [ ] **Step 3: 实施**
  - `tools.py`：用 LangChain `@tool` 定义三个工具；`katago_analyze(engine, moves)` 内部调 `engine.analyze`，失败/`error` 都返回可读中文；`joseki_lookup(name)` 查 `JosekiData`（查不到返回“库里没有这个定式”）；`board_query(moves)` 返回 `prompts.board_map(prompts.board_from_moves(moves), len(moves))`。
  - `agent.py`：`_tool_loop()` 按 `Interfaces` 实现——`bind_tools` → `ainvoke` → 有 `tool_calls` 则逐个执行（`asyncio.wait_for(..., TOOL_TIMEOUT_SEC)`，异常吞成文本）、发 `tool` start/end 事件、把结果作为 `ToolMessage` 追加；达到 `max_tool_calls` 后强制以“禁止再调用工具、直接回答”的追加指令收尾。
  - 只在 `hint()` 与 `_ai_turn()` 的 `think_msg` 两处启用，且 `self.tools_enabled` 为真才走 `_tool_loop`。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_tools tests.test_agent tests.test_main -v`
Expected: 全 PASS

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 前端接 `tool` 事件**

`NetClient.gd`：`_handle_packet` 的 `match` 里新增 `"tool": tool_event.emit(_without_type(d))`；声明 `signal tool_event(payload: Dictionary)`。
`Main.gd`：在 `_wire_agent()` 里接 `agent.tool_event.connect(_on_tool_event)`；`_on_tool_event(payload)` 只在 `phase == "start"` 时 `_set_status("她正在查资料…")`，`end` 时恢复原状态文案。

验证：

Run: `godot --headless --path game --import`
Expected: 无 `SCRIPT ERROR`

- [ ] **Step 6: 提交**

```bash
git add backend/app/tools.py backend/app/events.py backend/app/agent.py backend/app/config.py backend/app/main.py backend/tests/test_tools.py game/scripts/net/NetClient.gd game/scripts/ui/Main.gd
git commit -m "feat(backend,agent): 工具层与受限 tool loop（发言侧自主查盘/复算/查定式）（v3.2.0）"
```

---

### Task 6: 配置、文档与版本

**Files:**
- Modify: `game/config.json`（新增 `llm_extra_body`、`llm_think_extra_body`、`tools_enabled: false`、`max_tool_calls: 2`）
- Modify: `README.md`（第三节字段表补上述四项；新增「工具（发言侧）」小节说明工具范围与默认关闭；第九节新增 `### v3.2.0`）
- Modify: `AGENTS.md`（补：LLM 走 LangChain；`llm_extra_body` 的用途；**工具只影响发言、不影响落点**；改 `agent.py` 时必须保留音画同步与去重硬规则）

**Interfaces:**
- Consumes: 无
- Produces: 与代码一致的配置说明

- [ ] **Step 1: 改文档与模板**
- [ ] **Step 2: 校验**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `cd backend && python -c "from app.config import AzureConfig as C; c=C.load('../game/config.json'); print(c.tools_enabled, c.max_tool_calls, c.llm_extra_body)"`
Expected: `False 2 {}`

- [ ] **Step 3: 提交**

```bash
git add README.md AGENTS.md game/config.json
git commit -m "docs(config): 补 LangChain 与工具层配置说明，README 记录 v3.2.0（v3.2.0）"
```
