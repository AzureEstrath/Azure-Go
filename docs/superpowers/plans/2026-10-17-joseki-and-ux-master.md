# Azure-Go 定式教学 + 体验与可靠性 总计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先让 Azure 的定式教学从「等她走完才说一句」变成「主动识别、在结构点开口、棋盘上浮一张参考图卡片、并跨局记住教过你几次」（v3.6.0）；再收拾六件日常体验与可靠性的事，其中最重要的是给 `busy` 加一道不会放开并发的保险（v3.7.0）。

**Architecture:** 后端是唯一权威：定式识别、族计数、复盘局面重放都在 `backend/`；前端（Godot 4）只做渲染、输入、播放。机制先行——第一阶段的机制用**现有 7 条库**就能完整验收，扩库是随后独立的内容工作流。第二阶段是六个彼此独立的小改动，只共享「不动既有语义」这一条纪律。

**Tech Stack:** Python 3 / FastAPI / LangChain + LangGraph（`backend/`）；Godot 4.6 工程、用 4.7.1 本机验证（`game/`，GDScript）；KataGo 由独立的 `katago_service/` 提供；测试用标准库 `unittest`（后端）与 Godot headless 脚本（前端）。

**Spec:**
- `docs/superpowers/specs/2026-10-17-joseki-active-teaching-design.md`（阶段一）
- `docs/superpowers/specs/2026-10-17-ux-and-reliability-batch-design.md`（阶段二）

两个阶段**各自独立可发布**：阶段一完成后是一个能跑、能测、能玩的 v3.6.0；阶段二在它之上做 v3.7.0。**不要**为了「一起做完」而让阶段一的验收拖着阶段二。

**工作区（执行前先确认这一条）：** 唯一工作区是

```
G:\GodotProject\Azure-Go
```

分支 `main`，起点提交 `1f412d1`（v3.5.2）。下文的路径全部相对这个目录。

**已废弃的两个位置，不要用、也不要试图还原：**

- `G:\CSProjects\Azure-Go` —— 旧主检出，工作树是空的（只有 `.git`），索引里还留着一次**半截的聊天排队回退**（15 个已暂存改动）。权限只作备份，不干活。
- `C:\Users\z2996\.local\share\opencode\worktree\6b7ec4\langchain-backend` —— opencode 早些时候开的临时 worktree，已清除。

**目标 clone 的 `origin/main` 与本仓库历史是分岔的**（那是 GitHub 上 PR #1 合并出来的另一条线，内容停在 v3.3.0）。`origin/main`（`6744c3b`）作为历史留着，**不要**拿它来合。要推远端时先想清楚推哪条线——本地比 `origin/main` 多 87 个提交。

---

## Global Constraints

每一条都来自两份 spec 与 `AGENTS.md`，**每个任务都隐含包含本节**。

- **中文**：注释、UI 文案、提交信息一律中文，与现有风格一致。
- **提交格式**：`type(scope): 中文描述（vX.Y.Z）`，版本号同时写进 `README.md` 的「更新」小节。
- **后端测试**：`cd backend && python -m unittest discover -s tests`。**不得引入 pytest。** 单测用 `python -m unittest tests.test_x.ClassName.test_name -v`。
- **KataGo 服务测试**：`cd katago_service && python -m unittest discover -s tests`（假引擎，不依赖真实 KataGo 与公网）。
- **前端门卫**：`godot --path game --headless --script res://tests/test_tts.gd` / `test_speech_pairing.gd` / `test_config.gd`。本机只有 Godot 4.7.1（`E:\Godot\Godot_v4.7.1-stable_win64_console.exe`）；**跑完必须 `git status`**，把 `game/project.godot` 的 `config/features`（保持 `4.6`）、`*.import`、`*.uid` 等任务意图之外的改动还原。
- **`busy` 必归位**：`agent_graph.py` 里 `_set_busy(False)` 的位置**按阶段编码、不许随意挪动**；`_set_busy` 自带去重。
- **音画同步不许回退**：`ai_move` 事件带落点坐标，客户端在落子宣告的**语音开始播放**时 `POST /api/reveal` 才揭示棋子。
- **一切外部能力优雅降级**：缺后端 / 缺 KataGo / 缺云端 LLM / 缺 TTS 都不得崩、不得报错刷屏；后端返回可读错误且 `busy` 必须归位（不归位＝棋盘永久锁死，本项目最严重的故障形态）。
- **推理段永不显示给玩家**；台词一律经 `clean_reply()` / `content_of()`。
- **后端是唯一权威**：`game/` 不得出现任何编排、定式识别、局面重放逻辑。
- **语音链路**：清洗 / 分句 / 合成在后端；每条要念的台词**紧接**一条 `speech` 事件（`turn_id` + `seq`）；`tts_enabled=false` 时**零请求、零 `speech` 事件**。
- **异步纪律**：同步的磁盘 / 数据库调用**不得直接放进 `async def`**，一律 `await asyncio.to_thread(...)`；任何存储故障只打日志、绝不影响对局。
- **`tests/golden/prompts.json` 锁定** `context_block`：`long_term` 为空串时输出**逐字符不变**；新内容只能插在铁律段之前，铁律段本身不许动。提示词里**不得**放可被模型照抄的示例句。
- **既有常量不许放宽**：`MAX_LLM_CALLS_PER_TURN = 4`、`AI_DEDUP_WINDOW = 8`。
- **配置读取顺序**：可执行文件同目录 `config.json` → `res://config.json`；CLI 覆盖记进 `cli_locked` 且只影响本次运行。
- **平台**：Windows 单平台。PowerShell 读写文本用 `[IO.File]::ReadAllLines/WriteAllLines` + `[Text.UTF8Encoding]::new($false)`。
- **`server/` 目录**：288/370 个文件是上游 CosyVoice 代码，**不要顺手重排 / 美化**。

---

## Review Focus

以下是两份 spec 隐含、但没有哪个任务的测试天然覆盖到的输入类与失败模式——最可能咬到真实使用的人，按可能性排序。每一条都在下面指定的任务里加了对应的测试。

1. **次数记录读不出 / 写不进**（库文件被删、被占用、所在目录只读、首次运行目录不存在）→ 期望：当成「第 1 次」，照样上卡片、照样能下完这盘棋，只在日志留一行。测试在 **Task 4**。
2. **同一个角同时匹配多条**（镜像 / 换色歧义、同前缀分岔、扩库后条目变多）→ 期望：给出一致的候选集合，既不会把好手说成走错，也不会画出前后矛盾的参考图。测试在 **Task 2**、**Task 11a**。
3. **复盘模式下的并发与边界**：复盘进行中收到落子 / 聊天 / 闲置，或前端断开重连（`hello` 同步）→ 期望：一律被拒且状态自洽；退出复盘后回到进入前的局面。测试在 **Task C**、**Task D**。
4. **兜底超时的误伤与半写**：正常偏慢的一回合（KataGo 超时后重试 + 思考档位 + 两轮工具轮）**不得**被打断；真正卡死的回合超时后**不得**留下半写状态。测试在 **Task E**。
5. **前端在旧后端 / 事件缺字段下不崩**：`joseki` 事件缺字段、`bad_moves` 为空、`move_seq` 里含 `pass`、`score` 为 `null` → 期望：忽略未知字段、`pass` 不画子、不抛错。测试在 **Task 8**、**Task D**、**Task H**。

---

# 阶段一 · 定式教学（v3.6.0）

机制先行（Task 1–9），扩库随后（Task 10–12）。

## 文件结构（先锁定职责）

| 文件 | 职责 |
| --- | --- |
| `backend/app/joseki.py`（改） | 定式库 + 识别器。库条目补 `family`；`scan()` 输出扩成统一结构（进入 / 分岔 / 收尾两种形态 / 偏离） |
| `backend/app/joseki_progress.py`（新） | 族计数的持久化读写（SQLite）。**只有这一处碰盘** |
| `backend/app/agent_graph.py`（改） | 结构点开口的分流、并进既有发言位、走错判定、计数接线、悔棋退回 |
| `backend/app/agent.py`（改） | `_joseki_check` / `_joseki_emit` 适配新结构；持有 `joseki_progress` |
| `backend/app/events.py`（改） | `joseki` 事件扩字段（**旧字段不动**） |
| `backend/app/prompts.py`（改） | 进入 / 分岔 / 收尾三条讲解模板 |
| `backend/app/main.py`（改） | 装配 `joseki_progress`（可选：一个「忘掉教学记忆」的管理端点） |
| `game/scripts/ui/JosekiCard.gd`（新） | 浮层参考图卡片：小棋盘 + 进度 + 标错 + 拖动 / 避让 / 淡出 |
| `game/scripts/ui/Main.gd`（改） | 卡片接线 |
| `game/tests/test_joseki_card.gd`（新） | 卡片状态机的 headless 门卫（纯逻辑） |
| `tools/joseki_extract.py`（新） | 扩库用的自对弈提取脚本（开发工具，不入运行时路径） |

---

### Task 1: 定式库补 `family`

**Files:**
- Modify: `backend/app/joseki.py`
- Test: `backend/tests/test_joseki.py`（在既有文件里加用例）

**Interfaces:**
- Produces: `JosekiData.LIST` 的每个条目新增键 `"family": str`。族 id 用 ASCII 小写下划线，现有七条分别是 `star_33`（点三三三条）、`star_kakari_small`（小飞挂靠压两条）、`star_double_kakari`（双飞燕两条）。

- [ ] **Step 1: 写失败测试**

```python
def test_every_entry_has_family(self):
    for e in JosekiData.LIST:
        self.assertIn("family", e)
        self.assertRegex(e["family"], r"^[a-z0-9_]+$")

def test_variants_of_one_lesson_share_family(self):
    fams = [e["family"] for e in JosekiData.LIST if e["name"].startswith("点三三")]
    self.assertEqual(len(set(fams)), 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki -v`
Expected: FAIL —— `KeyError` / `AssertionError: 'family' not found`

- [ ] **Step 3: 逐条补 `family`**

给 `JosekiData.LIST` 的七个条目各加一个 `"family"` 键，取值按上面的划分。不要改 `name` / `seq` / `key`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿（既有 joseki 用例不得被破坏）

- [ ] **Step 5: 提交**

```bash
git add backend/app/joseki.py backend/tests/test_joseki.py
git commit -m "feat(joseki,data): 定式库补族 id（v3.6.0）"
```

---

### Task 2: 识别器输出扩成统一结构

**Files:**
- Modify: `backend/app/joseki.py`
- Test: `backend/tests/test_joseki.py`

**Interfaces:**
- Produces: `JosekiRecognizer.scan(moves) -> dict`（未命中时返回 `{}`）。命中时**保证**含这些键：
  - `kind: str` —— `"entered"` | `"branch"` | `"formed"` | `"deviated"`
  - `family: str`
  - `name: str`、`cat: str`、`corner: str`（沿用现有取值）
  - `k: int` —— 已吻合的手数
  - `line: list[dict]` —— **库里这一条**的全部着子落到真实棋盘坐标，元素 `{"x": int, "y": int, "c": "B"|"W"}`，长度等于该条目 `len(seq)`
  - `points: list[tuple[int, int]]` —— 已吻合前缀的实际棋盘点（**保持现有语义**，前端高亮轨迹在用）
  - `candidates: list[dict]` —— 以已吻合前缀为共同前缀、且下一步**不同**的其余线条，元素 `{"name": str, "family": str, "next": tuple[int, int], "line": list[dict]}`
- Consumes: Task 1 的 `family`
- 既有键 `points` / `key` / `expect` / `side` **不删**（前端 `Board3D.highlight_points` 与其余调用点仍在用）。

- [ ] **Step 1: 写失败测试**

```python
def test_entered_on_first_library_move(self):
    r = JosekiRecognizer.scan([["B", "Q16"], ["W", "D16"]])   # 星位 + 点三三
    self.assertEqual(r["kind"], "entered")
    self.assertEqual(r["family"], "star_33")
    self.assertEqual(r["k"], 2)
    self.assertEqual(len(r["line"]), 4)                        # 基本型共 4 手
    self.assertEqual(r["line"][0]["c"], "B")

def test_line_maps_mirrored_corner(self):
    r = JosekiRecognizer.scan([["B", "D4"], ["W", "Q4"]])      # 左下角，对角镜像
    self.assertEqual(r["kind"], "entered")
    self.assertTrue(all(0 <= p["x"] < 19 and 0 <= p["y"] < 19 for p in r["line"]))

def test_branch_when_two_lines_share_prefix(self):
    r = JosekiRecognizer.scan([["B", "Q16"], ["W", "D16"], ["B", "C16"]])
    self.assertIn(r["kind"], ("entered", "branch"))
    # 点三三两条定形在此分岔：候选里必须出现下一步不同的线
    self.assertGreaterEqual(len(r["candidates"]), 1)

def test_formed_and_points_unchanged(self):
    r = JosekiRecognizer.scan([["B", "Q16"], ["W", "D16"], ["B", "C16"], ["W", "D17"]])
    self.assertEqual(r["kind"], "formed")
    self.assertEqual(r["points"], [(3, 3), (2, 2), (2, 3), (3, 2)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki -v`
Expected: FAIL —— `KeyError: 'family'` / `KeyError: 'line'`

- [ ] **Step 3: 实现**

在现有 `_match_prefix` 游走的基础上，把「选中的那一条」与「同前缀的其余线条」都算出来：

- `line`：遍历该条目 `seq` 全部元素，用现有的 `_denorm(c, nx, ny)` 落回真实坐标，颜色由 `item[0]` 与 `same` 推出（`0`＝该角先落子方）。
- `candidates`：对每条条目取「与已吻合前缀相同」的最长共同长度，收集那些**第 k 手坐标不同**的线条。
- `kind`：`k == len(seq) == len(条目)` → `formed`；命中原条目但不是「进入」且存在候选 → `branch`；`k == 2`（星位 + 第一手）且无候选 → `entered`；玩家偏离 → 沿用 `deviated`。
  > 判定的确切阈值写在代码里并**由 Step 1 的四个用例钉住**——不要在这里发明新的分类学，用例就是定义。
- `points` 的计算**一行不改**。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿（含既有 joseki 用例与 `test_agent_graph.py`）

- [ ] **Step 5: 提交**

```bash
git add backend/app/joseki.py backend/tests/test_joseki.py
git commit -m "feat(joseki,core): 识别器输出统一结构（进入/分岔/收尾/偏离 + 完整线条与候选）（v3.6.0）"
```

---

### Task 3: 「走错」= 真亏且不在库线上

**Files:**
- Modify: `backend/app/agent_graph.py`（`after_player` 里的俗手判定附近，约 152–165 行）
- Test: `backend/tests/test_agent_graph.py`

**Interfaces:**
- Consumes: Task 2 的 `scan()` 结果
- Produces: 模块级函数 `def is_joseki_mistake(jk: dict, player_on_line: bool, drop: float, rank_ok: bool) -> bool`
  - `player_on_line=True`（这手正好是库里那一手）→ **恒为 `False`**（护栏）
  - `jk["kind"] != "deviated"` → `False`
  - 否则返回 `(not rank_ok) or (drop > 0.10)`，与既有俗手判定的口径一致

- [ ] **Step 1: 写失败测试**

```python
def test_on_library_line_is_never_a_mistake(self):
    self.assertFalse(is_joseki_mistake({"kind": "deviated"}, True, 0.5, False))

def test_off_line_without_loss_is_not_a_mistake(self):
    self.assertFalse(is_joseki_mistake({"kind": "deviated"}, False, 0.01, True))

def test_off_line_and_losing_is_a_mistake(self):
    self.assertTrue(is_joseki_mistake({"kind": "deviated"}, False, 0.12, True))

def test_formed_is_never_a_mistake(self):
    self.assertFalse(is_joseki_mistake({"kind": "formed"}, False, 0.5, False))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent_graph -v`
Expected: FAIL —— `NameError: is_joseki_mistake is not defined`

- [ ] **Step 3: 实现并在编排里接上**

实现上面那个纯函数（无副作用，便于测），然后在 `after_player` 里：把「是否正好走在库线上」算出来（把玩家这手的 GTP 与该角 `line` 中第 `k` 手比对），喂给它决定要不要走「走错」这条教学分支。**既有俗手记录的行为不变**（它仍按原判定写 `bad_moves`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent_graph.py backend/tests/test_agent_graph.py
git commit -m "feat(joseki,teach): 走错改为真亏且偏离库线，库线那一手不判错（v3.6.0）"
```

---

### Task 4: 族计数的持久化（含降级）

**Files:**
- Create: `backend/app/joseki_progress.py`
- Modify: `backend/app/main.py`（装配）
- Test: `backend/tests/test_joseki_progress.py`（新）

**Interfaces:**
- Produces: `class JosekiProgress`，构造 `JosekiProgress(db_path: str)`：
  - `seen(family: str) -> int` —— 该族**历史累计**出现次数；库不可用时返回 `0`
  - `bump(family: str) -> int` —— 加一并返回新值；失败返回旧值且不抛
  - `unbump(family: str) -> int` —— 减一（**不得低于 0**）；失败返回旧值且不抛
  - `forget_all() -> int` —— 清空，返回清掉的条数
- 所有方法都是**同步阻塞**的纯文件操作；调用方必须 `await asyncio.to_thread(...)`。
- 表结构：`joseki_seen(family TEXT PRIMARY KEY, count INTEGER NOT NULL, last_ts REAL NOT NULL)`。
- 库文件路径默认 `backend/data/joseki_progress.db`（与 `memory.db` 同在 `data/`，目录已 gitignore）。

- [ ] **Step 1: 写失败测试**

```python
def test_first_time_is_zero_then_one(self):
    p = JosekiProgress(self.db)
    self.assertEqual(p.seen("star_33"), 0)
    self.assertEqual(p.bump("star_33"), 1)
    self.assertEqual(p.seen("star_33"), 1)

def test_persists_across_instances(self):
    JosekiProgress(self.db).bump("star_33")
    self.assertEqual(JosekiProgress(self.db).seen("star_33"), 1)

def test_unbump_never_goes_negative(self):
    p = JosekiProgress(self.db)
    p.bump("star_33")
    self.assertEqual(p.unbump("star_33"), 0)
    self.assertEqual(p.unbump("star_33"), 0)

def test_readonly_dir_degrades_silently(self):
    p = JosekiProgress(os.path.join(self.db, "nope", "x.db"))   # 不可建库的路径
    self.assertEqual(p.seen("star_33"), 0)
    self.assertEqual(p.bump("star_33"), 0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki_progress -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.joseki_progress'`

- [ ] **Step 3: 实现**

用 `sqlite3`，`CREATE TABLE IF NOT EXISTS`，每个方法在 `try/except Exception` 里执行并 `logging.warning` 一行后降级返回。**不要**在模块导入期建库（要能 import 成功而不产生文件）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/joseki_progress.py backend/tests/test_joseki_progress.py
git commit -m "feat(joseki,memory): 族计数持久化，任何存储故障只降级不影响对局（v3.6.0）"
```

---

### Task 5: 入围规则接线（前 3 次都上卡片，第 4 次起只在走错时上）

**Files:**
- Modify: `backend/app/agent.py`（`_joseki_check` / `_joseki_emit` 与 `undo` 附近）
- Modify: `backend/app/agent_graph.py`
- Modify: `backend/app/main.py`（把 `JosekiProgress` 注进 agent）
- Test: `backend/tests/test_joseki_progress.py`、`backend/tests/test_agent.py`

**Interfaces:**
- Consumes: Task 2 的 `scan()`、Task 3 的 `is_joseki_mistake`、Task 4 的 `JosekiProgress`
- Produces:
  - `AzureAgent.joseki_progress: JosekiProgress | None`
  - `AzureAgent.joseki_show_card(jk: dict) -> bool` —— 是否该上卡片：
    - 进入时：`await asyncio.to_thread(self.joseki_progress.seen, family) < 3` → True
    - 第 4 次起：只有 `jk["kind"] == "deviated"` 且 `is_joseki_mistake(...)` → True
    - `joseki_progress is None` → 一律 True（降级＝每次都教）
  - `GameState` 新增 `joseki_counted: list[str]` —— **本局已计过数的族**（随快照回滚）
  - `GameState` 新增 `joseki_dropped: list[str]` —— 本局已被退回计数的族（供 `undo` 用）

- [ ] **Step 1: 写失败测试**

```python
def test_first_three_show_always(self):
    # count 0/1/2 → 进入即上卡片
def test_fourth_onward_only_on_mistake(self):
    # count 3 → entered 不上卡片；deviated+亏 才上
def test_same_family_counts_once_per_game(self):
    # 同一局里第二次进入同族，计数不再 +1
def test_undo_returns_the_count(self):
    # bump 后 undo 到那一手之前 → seen 回到原值
def test_missing_progress_degrades_to_always_show(self):
    # joseki_progress is None → True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: FAIL —— `AttributeError: 'AzureAgent' object has no attribute 'joseki_show_card'`

- [ ] **Step 3: 实现**

- `bump` 只在**本局首次进入该族**时执行一次（`joseki_counted` 去重），且必须 `await asyncio.to_thread(...)`。
- `joseki_counted` / `joseki_dropped` 加入 `GameState.make_snapshot()` 与 `_rollback`（与现有 `joseki_log` 同一处），这样悔棋回档会连计数一起退回；回档时对**被回滚掉**的族调 `unbump`。
- `undo` 是**同步方法**（无法 `await`）→ 退回计数走 `asyncio.to_thread` 的同步包装，或把退回放进一个不阻塞事件循环的线程调用里；**不得**让磁盘 IO 直接跑在事件循环上。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent.py backend/app/agent_graph.py backend/app/main.py backend/tests/
git commit -m "feat(joseki,teach): 前 3 次都上卡片、第 4 次起只在走错时上，同局同族只计一次、悔棋退回（v3.6.0）"
```

---

### Task 6: `joseki` 事件扩字段

**Files:**
- Modify: `backend/app/events.py`
- Modify: `backend/app/agent.py`（`_joseki_emit`）
- Test: `backend/tests/test_events.py`、`backend/tests/test_agent.py`

**Interfaces:**
- Consumes: Task 2 的 `scan()` 结果、Task 5 的 `joseki_show_card`
- Produces: `joseki` 事件**保留**现有 `type` / `kind` / `corner` / `points`，并**新增**：
  - `family: str`、`name: str`、`k: int`
  - `line: list[{"x": int, "y": int, "c": str}]`
  - `candidates: list[{"name": str, "next": [int, int]}]`
  - `show_card: bool`
  - `mistake: {"move_no": int, "x": int, "y": int, "drop": float} | None`

- [ ] **Step 1: 写失败测试**

```python
def test_joseki_event_keeps_legacy_fields(self):
    e = events.joseki({"kind": "formed", "corner": "左上", "points": [(3, 3)]})
    self.assertEqual(e["kind"], "formed")
    self.assertEqual(e["points"], [(3, 3)])

def test_joseki_event_carries_card_payload(self):
    e = events.joseki({"kind": "entered", "family": "star_33", "line": [...],
                       "candidates": [...], "show_card": True})
    self.assertEqual(e["family"], "star_33")
    self.assertTrue(e["show_card"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_events -v`
Expected: FAIL —— `KeyError: 'family'`

- [ ] **Step 3: 实现**

`events.joseki(payload)` 用 `.get()` 取新字段并给**安全缺省**（`family=""`、`line=[]`、`candidates=[]`、`show_card=False`、`mistake=None`），旧字段的取值路径**一行不改**。`_joseki_emit` 补齐这些字段。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/events.py backend/app/agent.py backend/tests/
git commit -m "feat(joseki,api): joseki 事件扩出卡片载荷，旧字段不动（v3.6.0）"
```

---

### Task 7: 结构点开口（进入 / 分岔 / 收尾）并进既有发言位

**Files:**
- Modify: `backend/app/prompts.py`（新增三条模板）
- Modify: `backend/app/agent_graph.py`（`after_player` / `own_turn` 的分流）
- Test: `backend/tests/test_prompts.py`、`backend/tests/test_agent_graph.py`

**Interfaces:**
- Consumes: Task 2、Task 6
- Produces（`prompts.py`）：
  - `joseki_enter_prompt(name: str, key: str, corner: str) -> str`
  - `joseki_branch_prompt(name: str, key: str, candidates: list[dict]) -> str`
  - `joseki_close_prompt(name: str, key: str, corner: str, side: str) -> str`
- 行为要求：
  - **进入**那一手：讲解**顶替**这一手的普通点评（同一时刻只出一条气泡）
  - **分岔 / 走错**：并进这一手的发言位
  - **收尾**：定式是玩家走完的 → 并进玩家这手的点评；是她走完的 → 并进她的落子宣告
  - **中间吻合的推进**：走**普通点评**，一个字都不提定式

- [ ] **Step 1: 写失败测试**

```python
def test_enter_replaces_the_comment(self):        # 进入那一手只发一条 message
def test_middle_moves_do_not_mention_joseki(self): # 吻合推进的 prompt 里不含定式名
def test_close_follows_the_side_that_finished(self):  # 她走完 → 并进 ai_move 那条
def test_context_block_golden_unchanged(self):     # tests/golden/prompts.json 逐字符一致
def test_llm_calls_stay_within_budget(self):       # 不超 MAX_LLM_CALLS_PER_TURN
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_prompts -v`
Expected: FAIL —— `AttributeError: module 'app.prompts' has no attribute 'joseki_enter_prompt'`

- [ ] **Step 3: 实现**

三条模板都用「给素材（定式名 + 库里的 `key`）+ 要求她用自己口吻说」的写法，**不要**放可被照抄的示例句。分流写在 `agent_graph`：进入 / 收尾两个分支各**复用**那一步原本就要发的那条台词位置，不新增发言点。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/prompts.py backend/app/agent_graph.py backend/tests/
git commit -m "feat(joseki,teach): 进入/分岔/收尾三处开口，并进既有发言位不增台词（v3.6.0）"
```

---

### Task 8: 前端浮层参考图卡片

**Files:**
- Create: `game/scripts/ui/JosekiCard.gd`
- Modify: `game/scripts/ui/Main.gd`
- Create: `game/tests/test_joseki_card.gd`
- Create: `game/tests/test_joseki_card.gd.uid`（由 Godot 导入生成，不要手写）

**Interfaces:**
- Consumes: `joseki` 事件的 `line` / `k` / `candidates` / `show_card` / `mistake`
- Produces: `class_name JosekiCard extends Control`，公开方法：
  - `apply_event(e: Dictionary) -> void` —— 缺字段时**忽略未知键并安全缺省**，不得抛错
  - `set_panel_overlap(rects: Array[Rect2]) -> void` —— 避让：与给定矩形（棋盘落点热区、鼠标附近）重叠时淡出
  - `clear() -> void` —— 收尾时收起
  - `state() -> String` —— `"hidden" | "shown" | "faded"`（供门卫断言，纯逻辑）
  - `stone_count() -> int` —— 当前画出来的合法棋子数（非法坐标不计，供门卫断言）
- 视觉要求：小棋盘上画 `line` 的全部着子（黑 `B` / 白 `W`），第 `k` 手高亮；`candidates` 在各自 `next` 位置画不同记号；`mistake` 那一手标红。默认停在棋盘空白角落、半透明、可拖动。

- [ ] **Step 1: 写失败测试（纯逻辑，headless）**

```gdscript
func test_missing_fields_do_not_crash() -> void:
	var c := JosekiCard.new()
	c.apply_event({"type": "joseki", "kind": "entered"})     # 缺 family/line/...
	assert_eq(c.state(), "shown")

func test_pass_and_unknown_fields_are_ignored() -> void:
	var c := JosekiCard.new()
	c.apply_event({"type": "joseki", "line": [{"x": 3, "y": 3, "c": "B"}, {"x": -1, "y": -1, "c": "?"}],
	               "k": 1, "junk": 5})
	assert_eq(c.stone_count(), 1)                            # 非法坐标不画

func test_clear_hides_it() -> void:
	var c := JosekiCard.new()
	c.apply_event({"type": "joseki", "show_card": true, "line": [], "k": 0})
	c.clear()
	assert_eq(c.state(), "hidden")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `godot --path game --headless --script res://tests/test_joseki_card.gd`
Expected: FAIL —— `JosekiCard` 未定义 / 断言失败

- [ ] **Step 3: 实现**

用 `Control` + 自绘（`_draw`）画小棋盘与棋子；拖动与淡出用 `_gui_input` 与 `Tween`。`Main.gd` 里接 `joseki` 事件、把棋盘与鼠标附近的矩形喂给 `set_panel_overlap`。**卡片不参与任何语音与台词**。

- [ ] **Step 4: 跑测试确认通过**

Run: `godot --path game --headless --script res://tests/test_joseki_card.gd`
Expected: 全部 PASS
再跑既有门卫：`godot --path game --headless --script res://tests/test_config.gd`（24/24）、`--headless --path game --quit`（无 `SCRIPT ERROR`）

- [ ] **Step 5: 提交**

```bash
git add game/scripts/ui/JosekiCard.gd game/scripts/ui/Main.gd game/tests/
git commit -m "feat(game,joseki): 棋盘浮层参考图卡片，跟对局同步并带避让（v3.6.0）"
```

---

### Task 9: 文档与版本（v3.6.0）

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `game/config.json`（若扩库引入新字段才动；否则跳过）

**Interfaces:** 无代码接口。

- [ ] **Step 1: 改 `README.md`**

- 「九、更新」加 v3.6.0 一段（主动识别 / 结构点开口 / 浮层卡片 / 前 3 次规则 / 扩库）。
- 第三节与 2.3 的界面表格补上卡片的位置、避让行为与「📖 复盘」之外**不新增**的内容。
- **顺手更正「远程后端」的表述**（设计文档 `2026-10-17-joseki-active-teaching-design.md` 第九节末表）：导语与「读取方」口径改为「后端是本机进程；只有 LLM 走可自定义的 OpenAI 兼容 API」。

- [ ] **Step 2: 改 `AGENTS.md`**

- 「是什么」：`远程后端` → `本机后端（Python / FastAPI）；只有 LLM 走云端`。
- 架构一节：把「前端没有 local 模式」与「后端就在本机」并列说清，避免读成「后端在别的机器上」。
- 新增定式教学的硬规则：**识别必须纯本地、零引擎调用**；**族计数只在 `joseki_progress.py` 一处碰盘**；**库线那一手不判走错**。

- [ ] **Step 3: 核对文档与代码一致**

逐条对照 README 里的字段表与开关生效范围，确认没有一条因为本阶段改动而失真。

- [ ] **Step 4: 全量门卫**

Run: `cd backend && python -m unittest discover -s tests`；`cd katago_service && python -m unittest discover -s tests`；三个前端 headless 脚本；`godot --path game --headless --quit`
Expected: 全绿；`git status` 除本任务的文件外无改动

- [ ] **Step 5: 提交**

```bash
git add README.md AGENTS.md
git commit -m "docs(readme,agents): 定式教学用法与后端本机口径（v3.6.0）"
```

---

### Task 10: 扩库提取脚本

**Files:**
- Create: `tools/joseki_extract.py`
- Test: `backend/tests/test_joseki_extract.py`（只测纯函数，不联网、不真跑引擎）

**Interfaces:**
- Produces（脚本内可被单测导入的纯函数，放在 `tools/` 下面的模块级）：
  - `corner_normalize(moves: list[list[str]], corner: int) -> list[list[int]]` —— 与 `joseki.py` 同一个归一化口径
  - `dedupe_lines(lines: list[list[list[int]]]) -> list[list[list[int]]]` —— 去掉互为前缀的重复，保留最长
  - `to_entry(family: str, name: str, cat: str, seq: list[list[int]], key: str) -> dict`
- 脚本本体：用 `katago_service` 的 HTTP 接口跑自对弈，产出**草稿 JSON**到 stdout 或文件，**不直接改 `joseki.py`**。

- [ ] **Step 1: 写失败测试**

```python
def test_dedupe_keeps_longest_prefix(self):
    lines = [[[0,3,3],[1,2,2]], [[0,3,3],[1,2,2],[0,2,3]]]
    self.assertEqual(dedupe_lines(lines), [[[0,3,3],[1,2,2],[0,2,3]]])

def test_to_entry_shape_matches_library(self):
    e = to_entry("star_33", "点三三·测试", "star", [[0,3,3],[1,2,2]], "要点")
    self.assertEqual(set(e), {"family", "name", "cat", "seq", "key"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki_extract -v`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 实现**

自对弈部分直接复用 `katago_service` 的 `POST /analyze`（引擎原文请求/响应），按「每手取 top-1 候选、连续 N 手都唯一」的规则切出一条线。**引擎路径从环境变量或命令行参数取**（`KATAGO_EXE` / `KATAGO_MODEL` / `KATAGO_CONFIG`），不写死。

> 本机可用的引擎（2026-10-17 核实）：`G:\GodotProject\Azure-Go\katago_service\engine\`（`katago.exe` + `model.bin` + `OpenCL.dll`，且 `opencltuning` 已调优），analysis 配置：`D:\下载\KataGo\analysis_example.cfg`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_joseki_extract -v`
Expected: PASS（纯函数用例）

- [ ] **Step 5: 提交**

```bash
git add tools/joseki_extract.py backend/tests/test_joseki_extract.py
git commit -m "feat(tools,joseki): 定式库自对弈提取脚本（v3.6.0）"
```

---

### Task 11a: 扩库 · 星位补齐（含分岔）

**Files:**
- Modify: `backend/app/joseki.py`（`JosekiData.LIST`）
- Test: `backend/tests/test_joseki.py`

**Interfaces:**
- Consumes: Task 10 的脚本产出、Task 1 的 `family`
- Produces: `star_33` 族补出**分岔**（挡这边 / 挡那边两条替代线）；新增族 `star_shimari`（守角）、`star_kakari_big`（大飞挂）、`star_kakari_one`（一间挂）、以及 `star_kakari_small` 的飞压 / 夹击两条。

- [ ] **Step 1: 写库校验测试**

```python
def test_every_family_has_at_least_two_lines_or_a_documented_exception(self):
    # 每条核心族至少两条线（否则「分岔」永远触发不了）
def test_no_two_entries_share_identical_seq(self):
def test_all_coords_inside_corner_region(self):     # max(nx,ny) < REGION
def test_no_entry_is_a_prefix_of_another_within_the_same_family(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_joseki -v`
Expected: FAIL（新族尚不存在）

- [ ] **Step 3: 逐组补条目**

用 Task 10 脚本产出的手顺草稿，**逐条人工核对**后才入库；每条写一句 `key` 要点文案（中文、一句话、说清「这形状谁得了什么、接下来该干嘛」）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿；并跑一次机制侧的黄金样本（Task 2 的用例）确认新条目没把旧判定带歪

- [ ] **Step 5: 提交**

```bash
git add backend/app/joseki.py backend/tests/test_joseki.py
git commit -m "feat(joseki,data): 星位补齐（守角/大飞挂/一间挂/飞压/夹击）与点三三分岔（v3.6.0）"
```

---

### Task 11b: 扩库 · 小目

**Files:** 同 Task 11a（只加 `komoku_*` 族）

**Interfaces:**
- Produces: `komoku_low_kakari`（一间低挂：飞 / 靠 / 夹）、`komoku_small_kakari`（小飞挂）、`komoku_shimari`（无忧角 / 小飞守角）、`komoku_tsuke`（挂角后尖顶）

- [ ] **Step 1–5:** 与 Task 11a 同形（写库校验测试 → 确认失败 → 用提取脚本的草稿逐条入库 + 写 `key` → 全量测试 → 提交 `feat(joseki,data): 小目开局四条（v3.6.0）`）

---

### Task 11c: 扩库 · 三三与目外 / 高目

**Files:** 同 Task 11a

**Interfaces:**
- Produces: `sansan_kakari`（三三被挂角）、`mokuhazushi_kakari` 与 `takamoku_kakari`（目外 / 高目被挂角）

> 这一组是设计文档里标着「**可砍**」的。若砍掉，**只做三三、`cat` 分别标 `san` / `moku` / `taka`**，并在提交信息里说明砍了什么。

- [ ] **Step 1–5:** 与 Task 11a 同形

---

### Task 12: 扩库后的文档与验收

**Files:**
- Modify: `README.md`（「定式导学」一节：库覆盖范围、「前 3 次」规则、卡片出现时机）

- [ ] **Step 1: 补文档**：写清库里现在覆盖哪些开局；卡片什么时候出现、什么时候收起。
- [ ] **Step 2: 全量门卫 + 联动冒烟**：后端全量、`katago_service` 全量、三个前端 script、`--headless --quit`；起后端 + 起 KataGo 服务（缺引擎也行）+ 前端 headless 连上 WS。
- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs(readme): 定式库覆盖范围与卡片出现时机（v3.6.0）"
```

---

# 阶段二 · 体验与可靠性批（v3.7.0）

六个互相独立的小项目。**顺序按依赖**：先做后端数据（Task A / B），再做复盘（C / D），然后 E（它碰编排写入点，放在复盘之后以免两处改同一片代码），最后是三个纯前端小项（F / G / H）。

### Task A: 本局逐手存档（完整、不截断）

**Files:**
- Modify: `backend/app/state.py`
- Modify: `backend/app/agent.py`（`just_moved` 路径）、`backend/app/agent_graph.py`（`_speak_own_turn` 的落子宣告路径）
- Test: `backend/tests/test_state.py`

**Interfaces:**
- Produces:
  - `GameState.move_notes: list[dict]` —— 元素 `{"move_no": int, "kind": "aim" | "review", "side": "E" | "A", "text": str}`
    - `kind="aim"` ＝ **落子宣告**（她这一手的用意）
    - `kind="review"` ＝ **落子后感想**（现有的 `A` 标签语义）
  - `GameState.note_move(move_no: int, kind: str, side: str, text: str) -> None` —— **纯追加，不滚动、不截断**
- 现有的 `memory_log`（滚动 18 条 / 截 80 字）**一行不动**，`note_move` 是它旁边的一份副本。

- [ ] **Step 1: 写失败测试**

```python
def test_notes_are_not_scrolled_or_truncated(self):
    st = GameState()
    for i in range(40):
        st.note_move(i, "review", "A", "x" * 300)
    self.assertEqual(len(st.move_notes), 40)
    self.assertEqual(len(st.move_notes[0]["text"]), 300)

def test_both_kinds_are_recorded(self):
    # aim（落子宣告）与 review（感想）各一条，move_no 相同 kind 不同

def test_reset_clears_notes(self):
```

- [ ] **Step 2: 跑测试确认失败** → `python -m unittest tests.test_state -v`，FAIL `AttributeError: move_notes`

- [ ] **Step 3: 实现**

`note_move` 只做 append（无长度上限、无截断）。**写入点必须纯追加**：在落子宣告那条路径上新增一次调用，**不得**改变它「直发、不走去重闸门、不产生额外事件」的既有行为——落子宣告的 `speech` 配对是硬规则。

- [ ] **Step 4: 跑测试确认通过** → `python -m unittest discover -s tests` 全绿

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,state): 本局逐手存档（用意 + 感想），不滚动不截断（v3.7.0）"
```

---

### Task B: 本局全部亏损手 + 亏损量

**Files:**
- Modify: `backend/app/state.py`
- Modify: `backend/app/agent_graph.py`（俗手判定处，约 158–165 行）
- Test: `backend/tests/test_state.py`

**Interfaces:**
- Produces:
  - `GameState.losses: list[dict]` —— 元素 `{"move_no": int, "gtp": str, "drop": float, "reason": str}`，**本局全部**，不截断
  - `GameState.record_loss(gtp: str, drop: float, reason: str) -> None` —— **只有 `drop > 0.10` 才记**
- `bad_moves` 与 `record_bad_move` 的判定、容量（3 条）、以及右侧栏「俗手回档」按钮的行为**全部不变**。

- [ ] **Step 1: 写失败测试**

```python
def test_bad_moves_still_capped_at_three(self):
    # 连记 5 条俗手 → len(st.bad_moves) == 3

def test_losses_are_not_capped(self):
    # 连记 5 条真亏 → len(st.losses) == 5

def test_rank_only_bad_move_is_not_a_loss(self):
    st.record_loss("D4", 0.0, "不在前 5")     # 没掉胜率
    self.assertEqual(st.losses, [])
```

- [ ] **Step 2–4:** 同上（失败 → 实现 → 全量绿）

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,state): 本局全部亏损手带亏损量，俗手回档行为不变（v3.7.0）"
```

---

### Task C: 复盘模式（后端）

**Files:**
- Modify: `backend/app/main.py`（两个端点 + 门禁）
- Modify: `backend/app/agent.py`（复盘开关与冻结）
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: Task A 的 `move_notes`、Task B 的 `losses`、现有 `payload()`
- Produces:
  - `POST /api/review` —— body `{"on": bool}`；进入 / 退出复盘模式；返回 `{"ok": true, "on": bool}`
  - `POST /api/review/goto` —— body `{"n": int}`；把第 `n` 手（含）之后的棋谱状态推一份 `state` 事件（复用现有 `payload()`）
  - `GET /api/review/notes` —— 返回 `{"ok": true, "notes": [...], "losses": [...]}`
  - 复盘模式期间：`/api/move`、`/api/chat`、`/api/idle`、`/api/hint`、`/api/analyze` 一律返回可读错误（`{"ok": false, "error": "复盘模式中"}`），**不得**改动 `busy`
- **不改动**「`finished` 之后仍可继续落子」这个既有语义；复盘是**可进可出**的独立模式，退出后回到进入前的状态。

- [ ] **Step 1: 写失败测试**

```python
def test_review_blocks_move_chat_idle(self):
    # 进复盘 → 三个端点都被拒且返回可读错误

def test_exit_review_restores_normal_play(self):
    # 退出 → /api/move 重新可用

def test_review_does_not_touch_finished_semantics(self):
    # 复盘进出前后 st.finished 不变

def test_goto_pushes_state_for_that_move(self):
    # goto(4) → 收到 state，且 move_count == 4

def test_reconnect_during_review_still_blocks(self):
    # 复盘中断开重连（hello 同步）后仍处于复盘模式
```

- [ ] **Step 2: 跑测试确认失败** → `python -m unittest tests.test_main -v`

- [ ] **Step 3: 实现**

复盘开关放在 agent 上（一个布尔 + 一个「进入前的 `busy`」快照）；`goto` **不重放**到一半就不管——它按 `move_seq` 的前 `n` 手重建一份只读盘面（**不要**去改 `st.board`，重建一份临时的用于推送）。情绪心跳与语气词在复盘期间冻结。

- [ ] **Step 4: 跑测试确认通过** → 全量绿

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(api,review): 后端复盘模式：只读重放 + 门禁 + 静音（v3.7.0）"
```

---

### Task D: 复盘模式（前端）

**Files:**
- Modify: `game/scripts/ui/Main.gd`、`game/scripts/ui/Board3D.gd`（只读开关）
- Create: `game/tests/test_review_mode.gd`

**Interfaces:**
- Consumes: Task C 的三个端点与 `state` 事件
- Produces: `Main.gd` 的 `_on_review_toggle()`、`_review_step(delta: int) -> void`，以及棋盘只读标志 `Board3D.set_readonly(v: bool) -> void`
- 入口「📖 复盘」只在**对局结束后**出现；上一手 / 下一手；退出回到进入前的局面。

- [ ] **Step 1: 写失败测试（纯逻辑）**

```gdscript
func test_step_clamps_at_both_ends() -> void:
	# _review_step(-1) 在 0 手时不动；_review_step(+1) 在末手时不动

func test_readonly_blocks_placement() -> void:
	# set_readonly(true) 后点击交叉点不产生落子意图
```

- [ ] **Step 2: 跑测试确认失败**

Run: `godot --path game --headless --script res://tests/test_review_mode.gd`

- [ ] **Step 3: 实现**

复盘期间：棋盘只读（**不接受落子与摸头**）、四个按钮置灰、聊天框禁用；退出时恢复。

- [ ] **Step 4: 跑测试确认通过** + 既有三个前端门卫

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,review): 局后只读复盘界面（v3.7.0）"
```

---

### Task E: `busy` 看门狗 + 代际守卫

**Files:**
- Modify: `backend/app/agent.py`（代数 + `_set_busy` 附近）
- Modify: `backend/app/agent_graph.py`（每个写入点前核对代数）
- Test: `backend/tests/test_agent_graph.py`

**Interfaces:**
- Produces:
  - `AzureAgent.epoch: int` —— 自增代数
  - `AzureAgent.begin_epoch() -> int` —— 返回并记住当前代数
  - `AzureAgent.epoch_ok(token: int) -> bool` —— 在途编排是否还属于自己那一代
  - `AzureAgent.WATCHDOG_SEC = <最坏预算 × 1.5~2>` —— 常量，值由最坏路径算出并**写在注释里**
- 超时触发时：`_set_busy(False)` + 前推 `epoch` + 一行日志 + 一条可读提示。

- [ ] **Step 1: 写失败测试**

```python
def test_normal_slow_turn_is_not_interrupted(self):
    # 三次 ainvoke + 重试的引擎调用 + 工具轮，全在 WATCHDOG_SEC 内 → 不触发

def test_watchdog_releases_busy_on_a_hung_turn(self):
    # 卡死 → busy 归位、只发一条可读提示

def test_late_writes_after_timeout_are_discarded(self):
    # 超时后旧那一代的所有写入全部作废（st.moves / bad_moves / move_notes 不变）

def test_no_double_busy_event(self):
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（无 `epoch` / `WATCHDOG_SEC`）

- [ ] **Step 3: 实现**

代数核对必须放在**每一个写 `st.*` 或发事件的位置之前**（棋谱提交、快照、`bad_moves`、`move_notes`、`_reveal_pending`、台词事件）。**不要挪动任何现有的 `_set_busy(False)`**；看门狗在最外层包一层。

- [ ] **Step 4: 跑测试确认通过** → 全量绿

- [ ] **Step 5: 提交**

```bash
git commit -m "fix(agent,core): busy 兜底超时 + 代际守卫，超时不再放开并发（v3.7.0）"
```

---

### Task F: 右栏收纳（⚙ 设置抽屉）

**Files:**
- Modify: `game/scripts/ui/Main.gd`（`_build_right_panel` 一带）
- Test: `game/tests/test_config.gd`（保持 24/24）

**Interfaces:** 无新接口；**只移动控件位置**，不改任何开关的绑定与生效范围。

- [ ] **Step 1: 记录改动前的行为基线**：把右栏每个开关现在的生效范围（立即 / 重启后端）抄成一份清单，改动后逐条核对。
- [ ] **Step 2: 实现抽屉**：`同步说话` / `整段合成` / `长句合并` 三个进抽屉（默认收起）；`允许虚着` / `定式导学` / `短回复` 与四个按钮、聊天框、状态栏留在外面。**左侧栏一行不动。**
- [ ] **Step 3: 逐条核对清单**（含 `config.json` 写回：抽屉里的开关改动后仍要写回原字段，`test_config.gd` 的「未知键原样保留」不得破）
- [ ] **Step 4: 跑门卫**：`test_config.gd` 24/24；`--headless --path game --quit` 无 `SCRIPT ERROR`
- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,ui): 一次性设置收进设置抽屉，右栏只留对局常用项（v3.7.0）"
```

---

### Task G: 视角便捷 + 悬停坐标残留

**Files:**
- Modify: `game/scripts/ui/Board3D.gd`、`game/scripts/ui/Main.gd`
- Create: `game/tests/test_view_helpers.gd`

**Interfaces:**
- Produces:
  - `Board3D.reset_view() -> void` —— 回初始视角（启动时捕获的 yaw / pitch / dist / tilt）
  - `Board3D.look_at_corner(corner: int) -> void` —— 四个角轮转
  - `Board3D.tip_text(x: int, y: int) -> String` —— 悬停文案；**空点**给「`D4 · 点击落子`」（现有措辞不许改），**已有棋子**给「`D4 · 第 3 手`」
- 键位挑不冲突、不易误触的；操作写进 README。

- [ ] **Step 1: 写失败测试（纯逻辑）**

```gdscript
func test_tip_text_for_empty_point() -> void:
func test_tip_text_for_occupied_point_includes_move_number() -> void:
func test_tip_text_outside_board_is_empty() -> void:
```

- [ ] **Step 2: 跑测试确认失败** → `godot --path game --headless --script res://tests/test_view_helpers.gd`
- [ ] **Step 3: 实现**（含悬停提示对已有棋子也给坐标 + 手数；手数由 `move_seq` 推）
- [ ] **Step 4: 跑测试确认通过** + 既有三个前端门卫
- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,ui): 一键回视角/看角，悬停已有棋子显示坐标与手数（v3.7.0）"
```

---

### Task H: 目差说人话

**Files:**
- Modify: `game/scripts/ui/Main.gd`（`_lbl_score` 一带）
- Create: `game/tests/test_score_phrase.gd`

**Interfaces:**
- Produces: `Main.gd` 的 `score_phrase(score, winrate) -> String` —— 把目差映射成量级人话（如「差不多」「半个角」「一个角的实空」「已经很难追了」），**原始数字仍然显示**（并列或悬停）。
- 注意：目差与胜率偶尔会打架（胜率 51% 但目差 -2.5 是正常的）——两者一起看，措辞不得让人以为矛盾。

- [ ] **Step 1: 写失败测试**

```gdscript
func test_near_even() -> void:
	assert_eq(score_phrase(0.5, 0.51), "差不多")
func test_winrate_and_score_disagree_does_not_contradict() -> void:
	# 胜率 51% + 目差 -2.5 → 措辞不得说「你领先」也不得说「已经很难追」
func test_null_score_is_safe() -> void:
	assert_eq(score_phrase(null, null), "--")
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（分档函数；`null` / 脏值安全缺省）
- [ ] **Step 4: 跑测试确认通过** + 既有门卫
- [ ] **Step 5: 提交**

```bash
git commit -m "feat(game,ui): 目差翻译成人话，原始数字保留（v3.7.0）"
```

---

### Task I: 文档与版本（v3.7.0）

**Files:**
- Modify: `README.md`、`AGENTS.md`

- [ ] **Step 1: 补文档**：复盘模式的用法与边界（可进可出、不改变「结束还能继续下」）；设置抽屉里各开关的生效范围（核对第三节表格）；新键位；目差的人话说明。
- [ ] **Step 2: `AGENTS.md` 补硬规则**：复盘模式的门禁与静音；**超时后必须作废旧代数的一切写入**（不许只解锁）；逐手存档的两条写入点（用意 / 感想）都必须是纯追加。
- [ ] **Step 3: 全量门卫 + 联动冒烟**（同 Task 12 的 Step 2）
- [ ] **Step 4: 提交**

```bash
git commit -m "docs(readme,agents): 复盘、设置抽屉、视角与目差说明（v3.7.0）"
```

---

## 收尾（两个阶段都完成后）

1. **一并验证**：后端全量 + `katago_service` 全量 + 三个前端 headless 脚本 + `--headless --quit`；整体联动冒烟（起后端 + 起 KataGo 服务（缺引擎也行）+ 前端 headless 连上 WS）。
2. **两份 spec 的验收 grep**：两份设计文档里每一条「必须 / 不许 / 不得回退」都要能指到一处代码或一个测试。
3. **parked Minor 清单**：把评审留下的次要项汇成一张表交用户分诊。
4. **合入决策**：① 本地合回 `main` ② 推送开 PR ③ 继续保留分支——问用户。注意 `docs/`、`AGENTS.md` 与全部计划只在分支上，合并时务必带上。
