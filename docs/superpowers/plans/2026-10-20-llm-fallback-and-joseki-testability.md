# 兜底台词频繁 & 定式可测性 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「嗯…思路有点断了…」这类兜底台词不再几乎每句都出现；并给「定式导学」一个不依赖运气、不依赖对手走在库线上的可复现验证入口。

**Architecture:** 两个问题互不相干，两条独立线，可分别合并：

- **问题②是 bug**：`backend/app/llm.py` 的输出上限策略对推理型模型不成立。`deepseek-v4.1-flash` 把**推理 token 也计入 `max_tokens`**，而代码给每处台词注入的上限只有 80~600（`think_mode=0` 不加任何推理预算）⇒ 服务端在推理段就把预算烧光 ⇒ `finish_reason="length"`、`content` 为空 ⇒ `chat()` 把空内容映射成 `FAIL_TEXT` ⇒ `agent._llm_failed()` 判真 ⇒ `_fallback_line("game")`。修法两层：给注入预算加**推理余量**（常态不再被截断），并在「空内容 + `finish_reason=length`」时**去掉注入上限重试一次**（长尾兜底）。
- **问题①不是识别器坏了**：25 条库线逐手回放全部能出 `entered` / `formed`；坏在**可复现性**。`entered` 只需要角内 2 手吻合库的前 2 手，**玩家自己就能造**（实测：黑 `D16` 星位 → 她下别处 → 黑 `F17` 小飞守角 ⇒ `entered / 星位·小飞守角`）；但再叠上频率层 `p=0.75`、同族 8 手冷却、跨局族计数（`seen>=3` 后只有「走错」才讲）且**没有任何复位手段**，真实运行库里 25 族就只有 1 族被触发过 1 次；`formed` / 后续手顺则必须对手配合走库线。`branch`（分岔）在真库上按既有设计不可达（`test_branch_still_reachable_when_prefix_is_three_moves` 用合成库覆盖），本次**不改语义**，只把可达性钉成测试。修法：回放工具 + 「每条库线一定触发得到」的测试 + **手工复现三步法** + 复位文档。

**Tech Stack:** Python 3 / LangChain `ChatOpenAI` / httpx `MockTransport`（测试不触网）/ `unittest` / Godot 4.6（本次无前端改动）。

**Spec:** `docs/problem.md`（问题清单）。根因证据见下节「证据」，实施者**不需要重新猜**；探针脚本在 `C:\Users\z2996\AppData\Local\Temp\opencode\`（`probe_fallback.py` / `probe_isolate.py` / `probe_fix_budget*.py` / `probe_joseki2.py`），可复跑。

> **范围调整（2026-10-20 拍板：A3 / B1 / C7）**
> - **A3** = 本计划 Task 1 + Task 2 + Task 3 全做（代码修 + 配置自救）。
> - **B1** = 不压推理、不加分阶段思考参数 ⇒ 本计划**不含**任何 `reasoning_effort` / `thinking` 之类改动；`REASON_HEADROOM_DEFAULT` 只解决「被截断」，不追求变快。
> - **C7** = 定式导学档位加「总是 100%」⇒ **原 Task 4 / Task 5（回放工具 + 教义测试）整段作废**，替换为下面的 **Task 4′**；原 Task 6 顺延为 **Task 5**。

## 证据（2026-10-20 实测，`deepseek-v4.1-flash` @ `https://opencode.ai/inference/openai/v1`）

问题②——同一份真实提示词、只改一个变量：

| 组合 | 结果 | `finish_reason` | `completion_tokens` |
| --- | --- | --- | --- |
| 不注入 `max_tokens` | ✅ 正常台词 | `stop` | 2119 |
| 注入 `max_tokens=300`（代码现状） | ❌ **content 为空** | `length` | 300（全被推理吃掉） |
| 注入无关字段 | ✅ 正常 | `stop` | 2047 |
| 短提示词 + `max_tokens=300` | ✅ 正常 | `stop` | 136 |

真实链路 9/9 全空（`chat()` 三处预算 × 各 3 次），每次 1.2~2.6s 返回 ⇒ **不是超时、不是限流、不是凭据问题**。
上限抬到 16384 后 `own_turn` 3/3 正常（597~3074 tokens，`stop`）⇒ 配置层可先自救，但推理超过上限时仍会空。

问题①——`JosekiRecognizer.scan` 逐手回放 25 条库线（150 个位置，纯本地）：

| kind | 次数 |
| --- | --- |
| `entered` | 25 |
| `formed` | 28 |
| `（空/中段沉默）` | 24 |
| `branch` | **0** |
| `deviated` | 0（需真偏离，本次未构造） |

运行库 `backend\data\joseki_progress.db` 全文只有一行：`('star_33', 1, 1792250332.66)` ⇒ 真实局里只触发过 1 次。

## 文件结构

- 改：`backend/app/llm.py` —— 输出预算（`_budget`）与空回复恢复（`chat`）；本计划全部代码改动的唯一生产文件。
- 改：`backend/tests/test_llm.py` —— 钉住「预算含推理余量」与「`length` 空回复重试」两条新契约。
- 建：`tools/joseki_replay.py` —— 纯本地回放工具（零引擎 / 零网络 / 零 LLM）。
- 建：`backend/tests/test_joseki_replay.py` —— 回放工具的纯函数门卫 + 「每条库线都触发得到」的回归断言。
- 改：`backend/tests/test_joseki.py` —— 一条 `branch` 可达性教义注释（不改断言）。
- 改：`README.md` —— 测试小节加回放命令与族计数复位说明；「更新」小节加 v3.9.1。
- 本地改、**不入库**：`game/config.json` —— `llm_extra_body` 自救值。

## Global Constraints

- 测试一律 `unittest`，**不用 pytest**；后端命令一律从 `backend/` 跑（`python -m unittest ...`）。
- 提交信息：Conventional + 中文描述；**版本号只写在最后一条提交**（`（v3.9.1）`），并同步 `README.md` 的「更新」小节。
- `game/config.json` 里 `llm_api_key` 是**真实凭据**：任何提交前 `git diff --cached game/config.json` 必须为空。
- `game/config.json` 的 `tts_enabled` 已是 `false`（上一轮修 TTS 卡顿），别改回去。
- 不动 `game/`（本次无前端改动）；不动 `busy` / `speech` / `reveal` / 去重窗口任何既有语义。
- 识别链路必须保持**纯本地、零引擎调用**（`joseki.py` 只做几十次比较）。
- 测试不得往 `backend/data/` 写运行产物（用 `tempfile.TemporaryDirectory`）。

## Review Focus

1. **两条降级不能混**：`_strip_on_reject` 是「服务端 400/422 拒绝参数」，本次是「服务端接受了参数但推理烧光预算」。恢复路径不同，别合并成一个标志位。
2. **两条既有契约不能破**：`no_think=True` 不加余量；用户在 `llm_extra_body` 里显式写死 `max_tokens` / `max_completion_tokens` 时注入值让位。
3. **兜底台词仍必须存在**：空内容（`finish_reason=stop`）与未闭合 think 段仍返回 `FAIL_TEXT`，不许出现「空台词发出去」。
4. **重试只许一次**：重试后仍空 ⇒ 落 `FAIL_TEXT`，不允许循环或指数重试。
5. **回放工具不得真的下棋**：只调 `JosekiRecognizer.scan` 与库常量，不许碰棋盘 / 引擎 / LLM / 事件总线。

---

### Task 1: 输出预算加入「推理余量」

**Files:**
- Modify: `backend/app/llm.py:29-30`（常量区）、`backend/app/llm.py:131-139`（`_budget`）
- Test: `backend/tests/test_llm.py:70-74`、`:179-190`、`:217-233`

**Interfaces:**
- Consumes: `LLMClient._budget(max_tokens: int, no_think: bool) -> int`；`_extra_for(budget) -> dict`
- Produces: 模块常量 `REASON_HEADROOM_DEFAULT = 4096`；`_budget` 在 `no_think=False` 且 `think_mode == 0` 时返回 `max_tokens + REASON_HEADROOM_DEFAULT`（`think_mode` 1/2 的逻辑一字不改）

- [ ] **Step 1: 先把三条既有断言改成新契约（这一步会红）**

`backend/tests/test_llm.py`：

```python
# test_plain_reply（原 73 行）
self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)

# test_budget_binding_keeps_extra_body_keys（原 190 行）
self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)

# test_400_on_max_tokens_recovers_without_budget（原 231 行）
self.assertEqual(seen[0]["max_tokens"], 100 + REASON_HEADROOM_DEFAULT)
```

并在文件顶部 `from app.llm import LLMClient` 旁补 `from app.llm import REASON_HEADROOM_DEFAULT`。
**不要动** `test_explicit_max_tokens_wins`（7）、`test_explicit_max_completion_tokens_wins`（无 `max_tokens`）、`test_...no_think...`（32）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_llm -v`
Expected: 3 个用例 FAIL，实际值 `100` 与期望值 `4196` 不等（`ImportError: REASON_HEADROOM_DEFAULT` 也算红）。

- [ ] **Step 3: 实现**

`backend/app/llm.py`:

```python
REASON_BUDGET_MED = 160         # 思考档位 1（中等）：推理段的追加预算
REASON_BUDGET_STRONG = 800      # 思考档位 2（强思考）：推理段的追加预算
# 推理型模型（推理 token 也计入 max_tokens）的**常态余量**：实测 deepseek-v4.1-flash 一次回答的
# 推理量在 500~4000 token 之间，远大于台词预算本身。没有余量时服务端在推理段就把预算烧光，
# 返回 finish_reason=length + 空 content（v3.9.1 修复）。长尾由 chat() 的无上限重试兜底。
REASON_HEADROOM_DEFAULT = 4096
```

`_budget` 最后一行改为：

```python
        return max_tokens + REASON_HEADROOM_DEFAULT
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_llm -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/llm.py backend/tests/test_llm.py
git commit -m "fix(llm): 输出预算为推理型模型留出余量，避免推理段吃光 max_tokens"
```

---

### Task 2: 空内容 + `finish_reason=length` ⇒ 去掉上限重试一次

**Files:**
- Modify: `backend/app/llm.py:177-214`（`_reason_of` 之后新增 `_finish_reason`；`chat()` 尾部）
- Test: `backend/tests/test_llm.py`（新增 2 个用例，放在 `test_empty_content_returns_fail_text` 之后）

**Interfaces:**
- Consumes: `content_of(ai) -> str`；`self._sync_chain()`（返回 `ChatOpenAI`，可 `.ainvoke(msgs)`）
- Produces: `LLMClient._finish_reason(ai) -> str`（读 `ai.response_metadata.get("finish_reason")`，取不到返 `""`）；`chat()` 在命中条件时**多发一次**不带 `extra_body` 的请求

- [ ] **Step 1: 写两个失败用例**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_llm -v`
Expected: `test_length_finish_with_empty_content_retries_without_budget` FAIL（只发了 1 次请求，返回兜底台词）。

- [ ] **Step 3: 实现**

`backend/app/llm.py`，在 `_reason_of` 之后加：

```python
    @staticmethod
    def _finish_reason(ai) -> str:
        """``AIMessage.response_metadata["finish_reason"]`` 的安全读取（取不到返空串）。"""
        md = getattr(ai, "response_metadata", None) or {}
        try:
            return str(md.get("finish_reason") or "")
        except Exception:                       # noqa: BLE001 —— 脏替身也不许把回合打挂
            return ""
```

`chat()` 尾部（原 `text = content_of(ai)` 起）改为：

```python
        text = content_of(ai)
        if text == "" and self._finish_reason(ai) == "length":
            # 推理型模型把推理 token 也计入 max_tokens：预算被推理段烧光时 content 恒为空。
            # 去掉注入的输出上限重试**一次**——这是恢复，不是重试风暴（max_retries 仍是 0）。
            print("[LLM] 输出预算被推理段吃光（finish_reason=length），去掉上限重试一次")
            try:
                ai = await self._sync_chain().ainvoke(messages)
            except Exception as e:              # noqa: BLE001
                print("[LLM] 重试失败：%s" % e)
                return "调用出错：" + self._reason_of(e)
            text = content_of(ai)
        if text == "":
            print("[LLM] 空回复（finish_reason=%s）⇒ 兜底台词" % (self._finish_reason(ai) or "?"))
            return FAIL_TEXT
        return text
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_llm -v`
Expected: 全部 PASS（含既有的 `test_empty_content_returns_fail_text`，它用 `finish_reason=stop`，不该触发重试）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/llm.py backend/tests/test_llm.py
git commit -m "fix(llm): 空内容且 finish_reason=length 时去掉输出上限重试一次"
```

---

### Task 3: 配置层自救（本地生效，**不提交**）

**Files:**
- Modify（仅本地）: `game/config.json:18` `"llm_extra_body"`

**Interfaces:**
- Consumes: `LLMClient._pins_budget()` —— 用户扩展字段里已有 `max_tokens` 时，注入的预算让位
- Produces: 运行中的后端每次调用都带 `max_tokens=16384`（实测 `own_turn` 3/3 正常）

- [ ] **Step 1: 改本地配置**

```jsonc
  "llm_extra_body": { "max_tokens": 16384 },
```

- [ ] **Step 2: 重启后端（改配置必须重启）**

Run: `backend\run_server.bat`
Expected: 启动日志出现 `[KataGo] 云端服务已就绪`；`GET http://127.0.0.1:8000/api/health` 返回 `llm_url` 为 opencode 端点。

- [ ] **Step 3: 实测三句台词不再是兜底**

玩两三个回合，或直接 `python C:\Users\z2996\AppData\Local\Temp\opencode\probe_fallback.py`（已存在，改 9 次调用应该 9/9 出正常台词而不是 `唔...我脑子有点转不过来了`）。
Expected: 气泡里的台词是正常点评（可含 `[[tone:...]]` 前缀，前端会剥），不是「思路有点断了」这类兜底句。

- [ ] **Step 4: 确认没有把凭据带进版本库**

Run: `git status --short game/config.json && git diff --cached game/config.json`
Expected: 第一条显示 ` M game/config.json`（本地修改），第二条**空输出**（没进暂存区）。

---

### Task 4: `tools/joseki_replay.py` —— 纯本地回放工具

**Files:**
- Create: `tools/joseki_replay.py`
- Test: `backend/tests/test_joseki_replay.py`

**Interfaces:**
- Consumes: `app.joseki.JosekiData.LIST`、`app.joseki.JosekiRecognizer.scan(moves)`、`app.joseki._denorm(corner, nx, ny)`、`app.coords.xy_to_gtp(x, y)`
- Produces:
  - `replay(corner: int = 0, first: str = "B") -> list[dict]`：每条库线逐手一行，字段 `{"name", "family", "corner", "n", "kind", "k", "candidates"}`；`kind` 取 `scan()` 的 `kind`，无结果时为空串 `""`
  - `summarize(rows: list[dict]) -> dict[str, int]`：`{"entered": n, "formed": n, "branch": n, "deviated": n, "": n}`（空串键＝中段沉默）
  - CLI：`python tools/joseki_replay.py [--corner 0..3] [--first B|W] [--only 名字子串]`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_joseki_replay.py`（导入写法照 `tests/test_joseki_extract.py:15-21`，把仓库根与 `backend/` 都加进 `sys.path`）：

```python
    def test_every_library_entry_reaches_a_structure_point(self):
        """每条库线在某一手一定能出 entered 或 formed —— 否则这条路真实对局里永远教不到。"""
        rows = joseki_replay.replay()
        by_name = {}
        for r in rows:
            by_name.setdefault(r["name"], set()).add(r["kind"])
        for name, kinds in by_name.items():
            self.assertTrue(
                kinds & {"entered", "formed"},
                "库线 %s 逐手回放没有任何结构点：%s" % (name, sorted(kinds)))

    def test_summary_counts_match_rows(self):
        rows = joseki_replay.replay()
        s = joseki_replay.summarize(rows)
        self.assertEqual(sum(s.values()), len(rows))
        self.assertGreater(s["entered"], 0)
        self.assertGreater(s["formed"], 0)

    def test_replay_is_pure_local(self):
        """只读库常量：不碰棋盘 / 引擎 / LLM / 事件总线，换来换去结果一致。"""
        self.assertEqual(joseki_replay.replay(), joseki_replay.replay())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki_replay -v`
Expected: `ModuleNotFoundError: No module named 'tools.joseki_replay'`。

- [ ] **Step 3: 实现工具**

`tools/joseki_replay.py`：主体就是本计划「证据」里用过的那段回放（库序列 → `_denorm` → `xy_to_gtp` → 前缀逐手 `scan`）。三条硬要求：① 只 import `app.joseki` / `app.coords`，不 import `board`/`engine`/`llm`；② `replay()` 不改 `JosekiData.LIST`（只读）；③ `main()` 打印每行 `名字 | 库长 | 角内第 n 手 | kind | k | 候选数`，末尾打印 `summarize()` 与一行提示「想重测同一族：删 backend/data/joseki_progress.db」。

- [ ] **Step 4: 跑测试与 CLI**

Run: `cd backend && python -m unittest tests.test_joseki_replay -v`
Expected: 3 个用例 PASS。

Run: `python tools/joseki_replay.py --corner 0`
Expected: 25 条库线逐手输出；末尾 `branch: 0`（这是**已知且有意**的，见 Task 5）。

- [ ] **Step 5: 提交**

```bash
git add tools/joseki_replay.py backend/tests/test_joseki_replay.py
git commit -m "feat(tools): 定式库回放工具（纯本地，逐手打印识别结果与结构点统计）"
```

---

### Task 5: 把「定式为什么难测」写死成测试与文档

**Files:**
- Modify: `backend/tests/test_joseki.py:222-227`（只改 docstring，**不改断言**）
- Modify: `README.md`（测试小节 + 定式小节各一行）

**Interfaces:**
- Consumes: Task 4 的 `replay()` / `summarize()`
- Produces: 无新接口；把「真库不出 `branch`」与「族计数可复位」两件事记在代码旁边

- [ ] **Step 1: 补一条教义用例（把「真库 branch=0」钉住）**

在 `backend/tests/test_joseki_replay.py` 追加：

```python
    def test_real_library_has_no_branch_point(self):
        """真库的分岔全落在 index 2（已由「进入」接手）：`branch` 只在合成库用例里可达。

        这条**不是**缺陷断言，而是防回归——哪天真库补出真分岔（设计 §七 扩库），
        这里会红，提醒同步 `agent_graph` 的讲解模板与 README 口径。
        """
        self.assertEqual(joseki_replay.summarize(joseki_replay.replay())["branch"], 0)
```

- [ ] **Step 2: 跑测试**

Run: `cd backend && python -m unittest tests.test_joseki_replay -v`
Expected: 4 个用例 PASS。

- [ ] **Step 3: 写文档**

`backend/tests/test_joseki.py:223` 的 docstring 补一句「真库逐手回放的 branch 计数由 `tests/test_joseki_replay.py` 钉住」。

`README.md` 测试小节（`godot --path game --headless --script res://tests/test_joseki_level.gd` 那一段附近）加：

```markdown
  - `python tools/joseki_replay.py` —— 定式库逐手回放（纯本地，零引擎零 LLM）：打印每条库线在角内第几手会出「进入 / 分岔 / 收尾」，以及四种结构点的总数。想重测同一族前先删 `backend\data\joseki_progress.db`（族计数是跨局持久化的运行产物，教过 3 次后只有「走错」才会再讲）。
  - **手工复现一次定式导学**（最短路径，已实测）：① 删 `backend\data\joseki_progress.db` 复位族计数；② `game/config.json` 的 `joseki_rate` 设 `1.0`（设置面板最高只有 0.75）；③ 开局黑下星位（`D16`），等她下到角外，再在角内小飞守角（`F17`）⇒ 必出 `entered / 星位·小飞守角` 与参考图卡片。
```

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_joseki.py backend/tests/test_joseki_replay.py README.md
git commit -m "docs(joseki): 钉住真库无分岔点的现状，补回放与族计数复位的测法"
```

---

### Task 6: 版本、全量回归与手工验收

**Files:**
- Modify: `README.md`（「更新」小节）

- [ ] **Step 1: 全量测试**

Run: `cd backend && python -m unittest discover -s tests`
Expected: `OK`，用例数 ≥ 现有基线，一个都不许减。

Run: `cd katago_service && python -m unittest discover -s tests`
Expected: `OK`（本次未改该服务，应原样通过）。

- [ ] **Step 2: 起真实环境手工验收（需要 KataGo 服务）**

Run: `katago_service\run_server.bat`，等 `GET http://127.0.0.1:8100/health` 的 `state == ready`；再 `backend\run_server.bat`。
Expected: 连下三手，每回合都出现**正常点评**，不再是兜底句；回合耗时在 10 秒量级（TTS 已关）。

- [ ] **Step 3: 按三步法手工验一次定式导学**

先停掉后端，再：删 `backend\data\joseki_progress.db`（族计数复位）→ 把 `game/config.json` 的 `joseki_rate` 临时设为 `1.0` → 重新起后端与游戏 → 开局黑 `D16` → 等她下到角外 → 黑 `F17`。
Expected: 聊天区出现「星位·小飞守角」的讲解（不是兜底句），`JosekiCard` 浮层画出参考图，该族计数 +1。验完把 `joseki_rate` 改回原值，并确认 `game/config.json` 没进暂存区。

- [ ] **Step 4: 更新 README「更新」小节**

```markdown
- **v3.9.1**：修复推理型模型（推理 token 计入 `max_tokens`）导致**每句台词都退化成兜底台词**——输出预算加推理余量，并在「空内容 + `finish_reason=length`」时去掉上限重试一次；新增定式库回放工具 `tools/joseki_replay.py` 与「每条库线都能触发结构点」的回归用例。
```

- [ ] **Step 5: 提交**

```bash
git add README.md
git commit -m "chore(release): 兜底台词与定式可测性修复（v3.9.1）"
```

---

## 尚未纳入本计划（需要你拍板后再排期）

1. **扩库补真分岔 / 修 `branch` 语义**：当前 `formed` 抢在 `candidates` 之前判定，所以「点三三第 4 手」这种**真分岔点**被讲成「收尾」。改判定顺序会让 `test_formed_and_points_unchanged` 等既有断言变红（U52/U53 是有意为之），属于产品语义决策，不在本次 bug 修复范围内。
2. **给开发用的强制卡片出口**（例如 `godot --path game -- --joseki=demo`）：只用于肉眼验收 `JosekiCard` 的渲染，不改变生产触发条件。仓库已有 `-- --setup` 这类调试开关的先例，可随时补。
3. **`joseki_lookup` 工具**：`tools_enabled=false`（默认关），玩家目前无法自助查库/制造结构点；要「在游戏里主动测定式」，打开这个工具是成本最低的一步。
