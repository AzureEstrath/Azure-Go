# 计划 03：长期记忆（云端 embedding）—— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Azure 跨局记得 Estarth——把对局总结与玩家偏好写成长期记忆（SQLite + 云端 embedding 语义检索），在合适的调用点把最相关的几条注入上下文。

**Architecture:** 新增 `backend/app/memory.py`（存储 + 检索 + 注入文本），与现有**局内记忆** `state.py:memory_log`（E/A 标签、上限 18 条、随快照回滚）互不替代：局内记忆管“这盘棋”，长期记忆管“这个人”。embedding 走**云端** OpenAI 兼容接口；接口不可用时自动降级为关键词检索（不崩、不刷屏）。功能默认关闭（`memory_enabled=false`）。

**Tech Stack:** 标准库 `sqlite3` + `json`（向量以 JSON 文本存库、余弦在 Python 里算——**不引入 numpy / chroma / faiss**）；`langchain-openai` 的 `OpenAIEmbeddings`。

**Spec:** `docs/backend-langchain-plan.md`（决策 D2/D3；「不改棋盘规则与情绪模型」非目标）

## Global Constraints

- 前置：计划 0/01/02 已完成。
- 测试一律 `unittest`：`cd backend && python -m unittest discover -s tests`；**不得触网**（embedding 以可注入的 `embed_fn` 假实现驱动）。
- 长期记忆**默认关闭**；关闭时 `context_block` 的输出必须与开启前**逐字符一致**（用测试锁定）。
- 不得改动 `prompts.context_block` 末尾的「铁律段」；新增内容插在铁律段**之前**。
- 检索结果注入有硬上限：`memory_top_k`（默认 3）与 `memory_max_inject_chars`（默认 240），单条截断到 120 字。
- 记忆写入有硬上限：单条 `text` ≤ 200 字，库内总行数上限 `MEMORY_MAX_ROWS = 2000`（超出按时间淘汰最旧）。
- 任何记忆故障（DB 不可写、embedding 401/超时）都不得影响对局：写失败只打日志。
- 提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.3.0**，同步写进 `README.md` 的「更新」小节。

## Review Focus

- **云端 embedding 不可用**：401 / 超时 / 未配置 → 必须降级成关键词检索并照常出话，不得抛异常、不得让 `busy` 卡住。
- **记忆污染**：把模型幻觉、临时台词、局面描述写进长期记忆会让“她记得的东西”逐渐失真——只允许写入总结、显式事实与玩家偏好，且写入前必须过长度与去重（同 tag + 同 text 不重复入库）。
- **注入串味**：注入的记忆绝不能被当成“当前盘面”，也不能出现在台词里的字面标签（如“【长期记忆】”）；`prompts.long_term_note` 的措辞必须与铁律段一致地强调“仅供参考、不许照抄”。
- **DB 并发与迁移**：WS 与 REST 可能同时读写——`sqlite3` 连接必须 `check_same_thread=False` + 单写锁；旧库缺表/缺列必须自动建表，不得崩。
- **默认关闭时零开销**：关闭时不得创建数据库文件、不得调用 embedding 接口。

---

### Task 1: `memory.py` —— 存储与检索

**Files:**
- Create: `backend/app/memory.py`
- Create: `backend/tests/test_memory.py`
- Modify: `backend/app/config.py`（新增 `memory_enabled=False`、`memory_db_path=""`、`embed_base_url=""`、`embed_model=""`、`embed_api_key=""`、`memory_top_k=3`、`memory_max_inject_chars=240`，均从 `config.json` 同名键读入）
- Modify: `.gitignore`（补 `backend/data/`）

**Interfaces:**
- Consumes: 无
- Produces:
  - `class MemoryStore:` `__init__(self, db_path: str, embed_fn=None, now_fn=None)`（属性 `_max_rows` 可在测试中收紧以验证淘汰）
    - `write(self, text: str, *, kind: str = "fact", tag: str = "", meta: dict | None = None) -> int | None`（≤200 字、同 tag+text 去重、超行数淘汰最旧；返回 id，失败返回 None）
    - `recall(self, query: str, k: int = 3) -> list[dict]`（返回 `[{"id","ts","kind","tag","text","score"}]`，按 score 降序）
    - `stats(self) -> dict`（`{"rows": int, "games": int, "has_vectors": int, "db": str}`）
    - `forget(self, ids: list[int] | None = None, all: bool = False) -> int`
    - `save_game(self, moves: list, result: str, summary: str, meta: dict | None = None) -> int`
    - `close(self) -> None`
  - `def cosine(a: list[float], b: list[float]) -> float`
  - 常量 `MEMORY_MAX_ROWS = 2000`、`MAX_TEXT_LEN = 200`、`MAX_INJECT_ITEM_CHARS = 120`
  - `def inject_text(items: list[dict], max_chars: int) -> str`（拼接成给提示词用的一段文字，逐条截断）

- [ ] **Step 1: 写失败测试** — `backend/tests/test_memory.py`

```python
"""长期记忆：写入/检索/降级/上限，全部用假 embedding，不触网。"""

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.memory import MAX_TEXT_LEN, MemoryStore, cosine, inject_text


def fake_embed(text):
    """极简确定性 embedding：按字符哈希出 8 维向量，保证同词相近。"""
    v = [0.0] * 8
    for ch in text:
        v[ord(ch) % 8] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.dir.name, "m.db"), embed_fn=fake_embed)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_write_then_recall(self):
        i = self.store.write("Estarth 喜欢在布局阶段点三三", kind="preference", tag="estarth")
        self.assertIsInstance(i, int)
        hits = self.store.recall("布局怎么下三三", k=3)
        self.assertEqual(hits[0]["id"], i)
        self.assertIn("点三三", hits[0]["text"])
        self.assertIsInstance(hits[0]["score"], float)

    def test_duplicate_not_stored_twice(self):
        self.store.write("同一句话", kind="fact", tag="t")
        self.store.write("同一句话", kind="fact", tag="t")
        self.assertEqual(self.store.stats()["rows"], 1)

    def test_text_truncated_to_limit(self):
        self.store.write("长" * 500)
        text = self.store.recall("长", k=1)[0]["text"]
        self.assertLessEqual(len(text), MAX_TEXT_LEN)

    def test_row_cap_evicts_oldest(self):
        store = MemoryStore(os.path.join(self.dir.name, "cap.db"), embed_fn=fake_embed)
        for i in range(5):
            store.write("第%d条" % i, tag="t%d" % i)
        store._max_rows = 3                      # 便于测试：临时收紧上限
        store.write("第四条", tag="t9")
        self.assertEqual(store.stats()["rows"], 3)
        store.close()

    def test_forget_all(self):
        self.store.write("a", tag="t")
        self.store.write("b", tag="t")
        self.assertEqual(self.store.forget(all=True), 2)
        self.assertEqual(self.store.stats()["rows"], 0)

    def test_save_game(self):
        self.store.save_game([["B", "Q16"], ["W", "D4"]], result="B+2.5", summary="布局平稳")
        self.assertEqual(self.store.stats()["games"], 1)

    def test_no_embed_fn_degrades_to_keyword(self):
        store = MemoryStore(os.path.join(self.dir.name, "kw.db"), embed_fn=None)
        store.write("Estarth 喜欢听棋谱讲解", kind="preference", tag="estarth")
        hits = store.recall("讲解", k=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["score"], 0.0)      # 关键词命中：不给伪相似度
        store.close()

    def test_embed_fn_failure_is_swallowed(self):
        def boom(_t):
            raise RuntimeError("云端 embedding 401")

        store = MemoryStore(os.path.join(self.dir.name, "boom.db"), embed_fn=boom)
        self.assertIsInstance(store.write("还是能存进去", tag="t"), int)
        self.assertEqual(len(store.recall("存进去", k=1)), 1)
        store.close()


class MathTest(unittest.TestCase):
    def test_cosine_identity_and_orthogonal(self):
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_cosine_zero_vector_is_safe(self):
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)


class InjectTest(unittest.TestCase):
    def test_items_truncated_and_joined(self):
        items = [{"text": "甲" * 200}, {"text": "乙"}]
        out = inject_text(items, max_chars=240)
        self.assertLessEqual(len(out), 240 + 8)      # 允许分隔符开销
        self.assertIn("…", out)

    def test_empty(self):
        self.assertEqual(inject_text([], 240), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_memory -v`
Expected: FAIL（`No module named 'app.memory'`）

- [ ] **Step 3: 实施** — `memory.py`：
  - 建表（`CREATE TABLE IF NOT EXISTS memories(...)`、`vectors(...)`、`games(...)`）+ `PRAGMA journal_mode=WAL`；`sqlite3.connect(path, check_same_thread=False)` + `threading.Lock` 包写。
  - `write()`：截断 → 查重（同 `tag` + 同 `text` 直接返回既有 id）→ 插入 → 若 `embed_fn` 可用则算向量存 `vectors`（异常吞掉）→ 超 `_max_rows` 时删最旧（含其向量）。
  - `recall()`：有向量则按余弦排序取前 k；否则 `LIKE` 关键词检索（score 固定 0.0），二者都无命中返回 `[]`。
  - `inject_text()`：逐条截到 `MAX_INJECT_ITEM_CHARS`，按总字数上限拼接。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_memory -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/memory.py backend/app/config.py backend/tests/test_memory.py .gitignore
git commit -m "feat(backend,memory): 长期记忆存储与语义检索（云端 embedding + 关键词降级）（v3.3.0）"
```

---

### Task 2: 云端 embedding 接入与提示词注入

**Files:**
- Create: `backend/app/memory_service.py`
- Modify: `backend/app/prompts.py`（新增 `long_term_note(text: str) -> str`；`context_block(..., long_term: str = "")` 在**铁律段之前**插入该段；`long_term == ""` 时输出与现状逐字符一致）
- Create: `backend/tests/test_prompts_long_term.py`

**Interfaces:**
- Consumes: `MemoryStore`（Task 1）、`OpenAIEmbeddings`（`langchain-openai`）
- Produces:
  - `build_embed_fn(cfg) -> Callable[[str], list[float]] | None`（`embed_base_url/embed_model/embed_api_key` 任一为空 → `None`）
  - `class MemoryService:` `__init__(self, cfg)`；`available -> bool`；`recall_for(query: str, k: int | None = None) -> str`（直接返回可注入的文本，关闭/无命中返回 `""`）；`remember_game(moves, result, summary)`、`remember_fact(text, tag="")`
  - `prompts.long_term_note(text) -> str`
  - `prompts.context_block(moves, memory_log, chat_history, phase, skip_last_chat=False, katago="", board=None, long_term="")`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_prompts_long_term.py`

```python
"""注入段必须可选，且默认输出与改造前逐字符一致。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prompts

MOVES = [["B", "Q16"], ["W", "D4"]]


class LongTermNoteTest(unittest.TestCase):
    def test_empty_means_no_section(self):
        base = prompts.context_block(MOVES, [], [], "observe")
        with_empty = prompts.context_block(MOVES, [], [], "observe", long_term="")
        self.assertEqual(base, with_empty)
        self.assertNotIn("长期记忆", base)

    def test_section_inserted_before_iron_rules(self):
        out = prompts.context_block(MOVES, [], [], "observe", long_term="Estarth 喜欢点三三")
        self.assertIn("长期记忆", out)
        self.assertIn("Estarth 喜欢点三三", out)
        self.assertLess(out.index("长期记忆"), out.index("铁律"))
        self.assertIn("A→T 从左到右", out)        # 铁律段正文没被动过

    def test_note_wording_carries_reference_only_clause(self):
        note = prompts.long_term_note("他喜欢简明")
        self.assertIn("仅供参考", note)
        self.assertIn("别照抄", note)


class MemoryServiceTest(unittest.TestCase):
    def _cfg(self, **kw):
        class C:
            memory_enabled = True
            memory_db_path = ""
            embed_base_url = ""
            embed_model = ""
            embed_api_key = ""
            memory_top_k = 3
            memory_max_inject_chars = 240
        c = C()
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_disabled_returns_empty_and_no_db(self):
        import tempfile
        from app.memory_service import MemoryService
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "nope.db")
            svc = MemoryService(self._cfg(memory_enabled=False, memory_db_path=db))
            self.assertFalse(svc.available)
            self.assertEqual(svc.recall_for("任意"), "")
            self.assertFalse(os.path.exists(db))
            svc.close()

    def test_enabled_without_embedding_still_works_by_keyword(self):
        import tempfile
        from app.memory_service import MemoryService
        with tempfile.TemporaryDirectory() as d:
            svc = MemoryService(self._cfg(memory_db_path=os.path.join(d, "m.db")))
            self.assertTrue(svc.available)
            svc.remember_fact("Estarth 喜欢听定式讲解", tag="estarth")
            self.assertIn("定式讲解", svc.recall_for("讲讲定式"))
            svc.close()

    def test_build_embed_fn_requires_full_config(self):
        from app.memory_service import build_embed_fn
        self.assertIsNone(build_embed_fn(self._cfg()))
        self.assertIsNotNone(build_embed_fn(self._cfg(embed_base_url="https://e.example.com/v1",
                                                      embed_model="emb", embed_api_key="k")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_prompts_long_term -v`
Expected: FAIL

- [ ] **Step 3: 实施** — `prompts.long_term_note()` 文案自行撰写，但必须含“仅供参考”“别照抄”“不是当前盘面”三层含义；`context_block` 的插入位置在「【最近的对话】」之后、「（铁律 · 先核对再开口：」之前。`memory_service.py` 负责把 `cfg` 变成 `MemoryStore`（`memory_db_path` 为空时默认 `<backend>/data/memory.db`，并 `os.makedirs`），`recall_for` 内部做 `inject_text`。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_prompts_long_term tests.test_prompts -v`
Expected: 全 PASS（`tests/golden/prompts.json` 不得回归失败）

- [ ] **Step 5: 提交**

```bash
git add backend/app/memory_service.py backend/app/prompts.py backend/tests/test_prompts_long_term.py
git commit -m "feat(backend,prompts): 长期记忆段注入（默认空、铁律段之前）（v3.3.0）"
```

---

### Task 3: 编排接线（写入点与召回点）

**Files:**
- Modify: `backend/app/agent.py`（`__init__` 接受 `memory=None`；`summarize()` 末尾写长期记忆；`chat()` 与 `_ai_turn()` 两个调用点把 `long_term=` 传进 `with_context`）
- Modify: `backend/app/main.py`（lifespan 构造 `MemoryService(cfg)` 并注入 agent；退出时 `close()`；`/api/health` 增加 `memory_rows`）
- Modify: `backend/tests/test_agent.py`（新增：默认无记忆时 `with_context` 收到的 `long_term` 为空；注入假 memory 时会传入）

**Interfaces:**
- Consumes: `MemoryService`（Task 2）
- Produces: `AzureAgent(engine, llm, bus=None, joseki_teach=True, limit_len=False, memory=None)`，属性 `AzureAgent.memory`（可后置赋值；`None` 或 `available == False` 时召回文本为空串）；`GET /api/health` → `{..., "memory_rows": int}`

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_agent.py`

```python
class FakeMemory:
    def __init__(self, text=""):
        self.text = text
        self.games = []

    @property
    def available(self):
        return True

    def recall_for(self, query, k=None):
        return self.text

    def remember_game(self, moves, result, summary):
        self.games.append((len(moves), result, summary))


class MemoryWiringTest(unittest.TestCase):
    def test_no_memory_keeps_long_term_empty(self):
        agent, _eng, llm, _bus = _mk()
        agent.memory = None

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        for msgs, _mt, _nt in llm.calls:
            for m in msgs:
                if m["role"] == "user":
                    self.assertNotIn("长期记忆", m["content"])

    def test_memory_text_reaches_prompts(self):
        agent, _eng, _llm, _bus = _mk()
        agent.memory = FakeMemory("Estarth 喜欢点三三")

        async def run():
            await agent.chat("你好")

        asyncio.run(run())
        joined = " ".join(m["content"] for _msgs, _a, _b in [] for m in [])  # 占位，见下方真实断言
        # 真实断言：至少一条 user 消息里带上了召回文本
        sent = [m["content"] for c in agent.llm.calls for m in c[0] if m["role"] == "user"]
        self.assertTrue(any("Estarth 喜欢点三三" in s for s in sent))

    def test_summary_writes_long_term_memory(self):
        agent, _eng, _llm, _bus = _mk()
        mem = FakeMemory()
        agent.memory = mem

        async def run():
            await agent.player_move(3, 3)
            await agent.summarize("测试备注")

        asyncio.run(run())
        self.assertEqual(len(mem.games), 1)
        self.assertGreater(mem.games[0][0], 0)
```

（`test_memory_text_reaches_prompts` 里那行占位语句在实现时删掉，只保留 `sent` 断言。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: FAIL（`AzureAgent` 不接受 `memory`；召回文本未注入）

- [ ] **Step 3: 实施** — `agent.py`：把 `memory` 存为 `self.memory`；在 `chat()` 与 `_ai_turn()` 里 `lt = self.memory.recall_for(query) if self.memory and self.memory.available else ""`，`query` 分别取“玩家这句话”与“最近一手 + 阶段”。`summarize()` 成功后 `self.memory.remember_game(self.st.moves, result, text)`（`result` 用最终黑胜率/目差渲染成一行字符串）。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent.py backend/app/main.py backend/tests/test_agent.py
git commit -m "feat(backend,agent): 长期记忆写入与两处召回接线（v3.3.0）"
```

---

### Task 4: 记忆管理 API 与 `memory` 事件

**Files:**
- Modify: `backend/app/main.py`（新增 `POST /api/memory/query`、`POST /api/memory/write`、`GET /api/memory/stats`、`POST /api/memory/forget`）
- Modify: `backend/app/events.py`（新增 `memory(op, tag, text, score)`）
- Modify: `backend/tests/test_main.py`（端点形状与降级）
- Modify: `game/scripts/net/NetClient.gd`（`memory` 事件 → 忽略或轻微日志；不需要新信号）

**Interfaces:**
- Consumes: `MemoryService`（Task 2）
- Produces:
  - `POST /api/memory/query` `{query, k?}` → `{"ok": True, "items": [{id,ts,kind,tag,text,score}]}`
  - `POST /api/memory/write` `{tag?, text}` → `{"ok": True, "id": int|null}`
  - `GET /api/memory/stats` → `{"ok": True, "rows", "games", "has_vectors", "db"}`
  - `POST /api/memory/forget` `{ids?}` / `{all:true}` → `{"ok": True, "removed": int}`
  - 事件：`{"type": "memory", "op": "write"|"recall", "tag": str, "text": str, "score": float}`

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_main.py`

```python
class MemoryApiTest(unittest.TestCase):
    def test_endpoints_shape_when_disabled(self):
        with _client() as client:
            r = client.post("/api/memory/query", json={"query": "x"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"ok": True, "items": []})
            self.assertTrue(client.get("/api/memory/stats").json()["ok"])
            self.assertEqual(client.post("/api/memory/forget", json={"all": True}).json()["removed"], 0)

    def test_write_bad_body_no_500(self):
        with _client() as client:
            for body in ({}, {"text": 123}, {"text": ""}):
                r = client.post("/api/memory/write", json=body)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["ok"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_main -v`
Expected: FAIL（404）

- [ ] **Step 3: 实施** — 端点实现；`memory_enabled=false` 时全部返回空结果且 `ok` 为真（查询面可用、写入被拒）。`create_app(..., memory=None)` 允许测试注入。
- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/main.py backend/app/events.py backend/tests/test_main.py game/scripts/net/NetClient.gd
git commit -m "feat(backend,api): 长期记忆管理端点与 memory 事件（v3.3.0）"
```

---

### Task 5: 配置、文档与版本

**Files:**
- Modify: `game/config.json`（新增 `memory_enabled: false`、`memory_db_path: ""`、`embed_base_url/embed_model/embed_api_key`、`memory_top_k: 3`、`memory_max_inject_chars: 240`）
- Modify: `README.md`（第三节字段表补记忆相关项；新增「长期记忆」小节：存什么、怎么用、怎么忘掉（含 `/api/memory/*` 示例 curl）；第九节新增 `### v3.3.0`）
- Modify: `AGENTS.md`（补：`backend/data/` 是运行产物（已 gitignore）；长期记忆默认关闭；改 `prompts.context_block` 时必须保持「空 `long_term` 时输出不变」）

**Interfaces:**
- Consumes: 无
- Produces: 与代码一致的配置说明

- [ ] **Step 1: 改文档与模板**
- [ ] **Step 2: 校验**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `cd backend && python -c "from app.config import AzureConfig as C; c=C.load('../game/config.json'); print(c.memory_enabled, c.memory_top_k)"`
Expected: `False 3`

- [ ] **Step 3: 提交**

```bash
git add README.md AGENTS.md game/config.json
git commit -m "docs(config): 长期记忆配置与用法说明，README 记录 v3.3.0（v3.3.0）"
```
