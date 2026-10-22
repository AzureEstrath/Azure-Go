# Azure-Go 定式教学 + 体验与可靠性 总计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先让 Azure 的定式教学从「等她走完才说一句」变成「主动识别、在结构点开口、棋盘上浮一张参考图卡片、并跨局记住教过你几次」（v3.6.0）；再收拾六件日常体验与可靠性的事，其中最重要的是给 `busy` 加一道不会放开并发的保险（v3.7.0）。

**Architecture:** 后端是唯一权威：定式识别、族计数、复盘局面重放都在 `backend/`；前端（Godot 4）只做渲染、输入、播放。机制先行——第一阶段的机制用**现有 7 条库**就能完整验收，扩库是随后独立的内容工作流。第二阶段是六个彼此独立的小改动，只共享「不动既有语义」这一条纪律。

**Tech Stack:** Python 3 / FastAPI / LangChain + LangGraph（`backend/`）；Godot 4.6 工程、用 4.7.1 本机验证（`game/`，GDScript）；KataGo 由独立的 `katago_service/` 提供；测试用标准库 `unittest`（后端）与 Godot headless 脚本（前端）。

**Spec:**
- [`specs/2026-10-17-joseki-active-teaching-design.md`](specs/2026-10-17-joseki-active-teaching-design.md)（阶段一）
- [`specs/2026-10-17-ux-and-reliability-batch-design.md`](specs/2026-10-17-ux-and-reliability-batch-design.md)（阶段二）
- [`specs/2026-10-17-strategy-lines-and-event-log-design.md`](specs/2026-10-17-strategy-lines-and-event-log-design.md)（阶段三）
- [`specs/2026-10-17-player-profile-and-time-awareness-design.md`](specs/2026-10-17-player-profile-and-time-awareness-design.md)（阶段四）

四个阶段**各自独立可发布**：阶段一 = v3.6.0（定式教学），阶段二 = v3.7.0（体验与可靠性），阶段三 = v3.8.0（策略线与事件表），阶段四 = v3.9.0（棋风画像与时间感知）。**不要**为了「一起做完」而让先做的验收拖着后做的。

---

## 决策摘要（一页看懂）

- **做什么**：阶段一（v3.6.0）把定式教学从「走完才说一句」改成「主动识别 + 结构点开口 + 棋盘浮层参考图卡片」；阶段二（v3.7.0）收拾 6 件日常体验与可靠性的事。
- **核心新增（2026-10-17 追加）**：定式提示**不再每手都触发**，改成**可开关 + 频率可调**；实际提示概率 = 基础频率 ± （棋力画像 / 情绪，各 ±10%）；**没触发就只点评这一手、不讲定式**。见下一节。
- **不做什么**：不考你、不做回放器、不做双棋盘对照、不给大棋盘加手顺编号、不动语音链路；阶段二不合并两栏、不做 CI。
- **顺序**：先机制（含门控）、后扩库；**各阶段独立可发布**。
- **阶段三（v3.8.0，2026-10-17 追加）**：预测线 **A / B / C（三条，两两 ≥2~3 步不同）** + 情绪波动开关（A/B/C 阶梯）+ 事件表（带时刻）+ 提示词事件区块。**第一条红线：零额外引擎调用、默认零额外 LLM 调用**。**已实测（2026-10-17）：`moveInfos[].pv` 存在；200 visits 下 pv 6~10 手，三线都凑得出**，详见 spec 6.1。
- **阶段四（v3.9.0，2026-10-17 追加）**：**玩家棋风画像**（独立 `profile.db`、四维、指数衰减增量）+ **时间感知**（四项）+ 三个延伸（主动约战 / 画像面板 / 局末进步报告）。**第一条纪律：写入只在 `summarize()`（手动结束）里、回合内零新增调用**。

---

## 核心机制：定式提示门控（可开关 · 频率可调）

> 用户诉求（2026-10-17 追加）：**不必每手都做定式提示**。要「可开关 + 可调频率」；例如提示概率 50% 时，实际是否提示**结合棋力画像与情绪上下浮动 ±10%**；**没触发提示时，就不讲定式，照常只点评这一手**。

**两层结构（全篇最重要的简化）**

| 层 | 决定什么 | 规则 |
| --- | --- | --- |
| **资格层** | 这条定式**该不该教** | 族计数 `< 3` → 有资格；`≥ 3` → 只有「走错」（真亏且偏离库线）时才有资格；总开关 `joseki_teach=false` → 一律无资格 |
| **频率层** | 这次**说不说** | 有资格时掷一次概率 `p`：命中 → 讲解 + 卡片；未命中 → **与「没识别到定式」逐字段同构**（只点评这一手） |

**概率公式（可调 + 画像/情绪波动）**

```
p = clamp( joseki_rate + skill_adj + mood_adj, 0, 1 )
  joseki_rate ∈ [0,1]            # 基础频率，用户可调（默认 0.5）
  skill_adj   ∈ [-0.10, +0.10]   # 棋力画像：生疏 / 没见过 → 正；熟练 → 负
  mood_adj    ∈ [-0.10, +0.10]   # 情绪：好奇 / 兴奋 → 正；疲惫 / 低落 → 负
```

**优先级（不许被概率压过）**

1. **走错**（真亏且偏离库线）→ **必讲**（纠错时刻，绕过频率层）。
2. 结构点（进入 / 分岔 / 收尾）→ 走频率层（这才是「可调」的部分）。
3. 中间推进 → 一律不讲（沿用既有）。

**边界（硬规则）**

- 门控**只决定文本与卡片**：不改棋子、**绝不改 `speech` 配对与 `busy`**。
- 未命中路径必须与「未识别」路径**逐字段同构**（同一出口、同一事件集合）——这是可测性的关键，也是「关掉定式＝纯点评」的验收口径。
- 掷概率用**可注入的 rng**（默认系统随机），便于单测复现。
- 画像 / 情绪取不到 → `adj = 0`，不抛错、不影响对局。
- **冷却**：同一族在最近 `N` 手（建议 `N=8`，与 `AI_DEDUP_WINDOW` 同口径）内已讲过 → 本次直接按「未命中」处理，避免连手讲同一个。

**新增配置与开关**

- `joseki_rate: float`（0~1，默认 0.5）。「定式导学」由单个 bool 升级为「**开关 + 频率**」两件，UI 用**一格**承载（下拉：关闭 / 低 25% / 中 50% / 高 75%，或滑条），**不新增右栏控件位**（守住反目标「界面更挤」）。
- 仍走 `POST /api/config` 透传（新增 `joseki_rate`），客户端 `NetClient.joseki_rate`。

**工作区（执行前先确认这一条）：** 唯一工作区是

```
G:\GodotProject\Azure-Go
```

分支 `main`，起点提交 `1f412d1`（v3.5.2）。下文的路径全部相对这个目录。

> ⚠️ **本机核对**：这台机器上 **`G:` 盘不存在**；本 `superpowers/` 文件夹是从别处拷来的设计稿。本机可运行的代码是 `AzureGo-v3`（**v3.3.0**，GitHub 线），与本文档的 v3.5.2 起点**不是同一条线**。执行前先确认工作区，别在本机照搬（见附录 A · O11）。

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
- **门控只改文本与卡片**：`joseki_gate` 的决策**不得**改动棋子、事件配对（`message`/`speech`）、`busy` 与状态机；**未命中 == 未识别**，两条路径必须逐字段同构。
- **概率必须可复现**：`joseki_gate` 一律接收**注入的 rng**（默认系统随机），单测不得依赖真随机。
- **门控不得新增持久化**：棋力画像只从**局内既有信号**（玩家 rank / 亏损手比例 / 族计数）推，不新建数据库（可推翻，见附录 A · O9）。
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
6. **门控的频率与浮动被玩家误读**：`joseki_rate` 设成 0 或 1、画像/情绪取不到、冷却期内同一族连出、走错与随机撞车 → 期望：`p=0` 必不说、`p=1` 必说、缺失值 `adj=0` 不抛、冷却按「未命中」处理、**走错永远优先**。测试在 **Task 5**。

---

# 阶段一 · 定式教学（v3.6.0）

机制先行（Task 1–9），扩库随后（Task 10–12）。

## 文件结构（先锁定职责）

| 文件 | 职责 |
| --- | --- |
| `backend/app/joseki.py`（改） | 定式库 + 识别器。库条目补 `family`；`scan()` 输出扩成统一结构（进入 / 分岔 / 收尾两种形态 / 偏离） |
| `backend/app/joseki_progress.py`（新） | 族计数的持久化读写（SQLite）。**只有这一处碰盘** |
| `backend/app/joseki_gate.py`（新） | **定式提示门控**（纯函数 + 可注入 rng）：开关 / 频率 / 画像·情绪 ±10% / 冷却 / 走错优先。**只有这一处决定「这次说不说」** |
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

### Task 5: 定式提示门控（开关 + 频率 + 画像/情绪 ±10% + 冷却 + 走错优先）

**Files:**
- Create: `backend/app/joseki_gate.py`（纯函数 + 可注入 rng）
- Modify: `backend/app/agent.py`（`_joseki_check` / `_joseki_emit` 与 `undo` 附近接门控）
- Modify: `backend/app/agent_graph.py`
- Modify: `backend/app/main.py`（装配 `JosekiProgress` + `/api/config` 透传 `joseki_rate`）
- Modify: `game/scripts/core/AzureConfig.gd`、`game/scripts/net/NetClient.gd`、`game/scripts/ui/Main.gd`（「开关 + 频率」合一格）
- Test: `backend/tests/test_joseki_gate.py`（新）、`backend/tests/test_joseki_progress.py`、`backend/tests/test_agent.py`、`game/tests/test_config.gd`

**Interfaces:**
- Consumes: Task 2 的 `scan()`、Task 3 的 `is_joseki_mistake`、Task 4 的 `JosekiProgress`
- Produces（`joseki_gate.py`，纯函数 / 无副作用 / rng 注入）：
  - `eligible(seen: int, kind: str, mistake: bool, teach_on: bool) -> bool` —— 资格层
  - `probability(rate: float, skill_adj: float, mood_adj: float) -> float` —— `clamp(rate+skill_adj+mood_adj, 0, 1)`
  - `should_show(*, rng, rate, skill_adj, mood_adj, seen, kind, mistake, teach_on, last_taught_move, move_no, cooldown=8) -> bool` —— 最终决策（含冷却、走错优先）
  - `skill_adj(ranks: list[int], seen_count: int) -> float`、`mood_adj(mood: dict) -> float` —— 各自截断到 ±0.10；缺失 → 0.0
- 原有接口保留：`AzureAgent.joseki_progress: JosekiProgress | None`；`GameState.joseki_counted` / `joseki_dropped`（随快照回滚，供 `undo` 退回计数）

- [ ] **Step 1: 写失败测试**

```python
def test_rate_zero_never_shows(self):            # p=0 → 必不说（走错除外）
def test_rate_one_always_shows(self):            # p=1 → 必说
def test_adj_clamped_to_plus_minus_10pct(self):  # skill_adj / mood_adj 各自截断到 ±0.10
def test_rng_is_reproducible(self):              # 注入固定 rng → 决策可复现
def test_miss_is_identical_to_no_match(self):    # 未命中与「未识别」事件集合逐项相等
def test_teach_off_is_silent(self):              # joseki_teach=false → 无 joseki / 讲解 / 卡片
def test_cooldown_suppresses_repeat(self):       # 同族 N 手内不重复讲
def test_mistake_bypasses_frequency(self):       # 走错 → 必讲
def test_missing_profile_or_mood_degrades(self): # 画像 / 情绪缺失 → adj=0，不抛
def test_same_family_counts_once_per_game(self): # 同一局第二次进同族，计数不再 +1
def test_undo_returns_the_count(self):           # bump 后 undo → seen 回原值
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_agent -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.joseki_gate'`

- [ ] **Step 3: 实现**

- **资格层**：族计数 `< 3` 有资格；`≥ 3` 只在「走错」时有资格——原 Task 5 的「前 3 次 / 第 4 次起」并入资格层，**取消那两条独立规则**。
- **频率层**：`should_show(...)` 掷 `p`；未命中 → 走**与未识别完全相同的出口**（不 emit `joseki`、不换讲解模板、不上卡片）。
- **走错优先**：`mistake=True` → 直接 True，不进概率。
- **冷却**：维护「族 → 最近讲过的手数」（`last_taught_move`），窗口内按未命中处理。
- **画像 / 情绪**：`skill_adj(ranks, seen_count)` 用局内最近若干手的 `player_rank` + 该族计数；`mood_adj(mood)` 用现有 `mood` 区域（好奇 / 兴奋 ↑，疲惫 / 低落 ↓）。取不到 → `0.0`。
- `bump` 只在**本局首次讲解该族**时执行一次（`joseki_counted` 去重；**计数＝讲解次数**——频率层未命中不消耗「前 3 次」教学预算，与 `tests/test_joseki_gate.py` 锁定的口径一致），且必须 `await asyncio.to_thread(...)`；`joseki_counted` / `joseki_dropped` 加入 `make_snapshot()` 与 `_rollback`，悔棋回档时对被回滚的族 `unbump`。
- 决策留**一行日志**（rate / adj / roll / 命中），便于调参与事后归因。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add backend/app/joseki_gate.py backend/app/agent.py backend/app/agent_graph.py backend/app/main.py backend/tests/ game/scripts/core/AzureConfig.gd game/scripts/net/NetClient.gd game/scripts/ui/Main.gd
git commit -m "feat(joseki,teach): 定式提示门控（开关+频率+画像/情绪±10%+冷却），走错优先、未命中只点评（v3.6.0）"
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

- 「九、更新」加 v3.6.0 一段（主动识别 / 结构点开口 / 浮层卡片 / **提示门控：开关 + 频率 + 画像·情绪 ±10%、未命中只点评** / 扩库）。
- 第三节字段表补 `joseki_rate`（0~1，默认 0.5）与「定式导学 = 开关 + 频率」；2.3 的界面表格补上卡片的位置、避让行为与「📖 复盘」之外**不新增**的内容。
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

# 阶段三 · 策略线与事件表（v3.8.0）

> **Spec**：[`specs/2026-10-17-strategy-lines-and-event-log-design.md`](specs/2026-10-17-strategy-lines-and-event-log-design.md)。
> 本阶段独立于阶段一 / 二，可单独实施与发布。
> **本阶段第一条纪律：零额外引擎调用、默认零额外 LLM 调用**；**提示词事件区块为空时必须逐字符不变**。
> **实测已完成（2026-10-17）**：`pv` 存在、200 visits 够五步、三线可凑；代价是**那一次已有分析要更深**（`maxVisits` 100→200，单线程 ≈9.8s），且部署侧**必须显式设 `analysisPVLen`**。

## 文件结构（先锁定职责）

| 文件 | 职责 |
| --- | --- |
| `backend/app/pv_lines.py`（新） | 从既有 `moveInfos` 提取**线 A / B / C**（纯函数，**零引擎调用**） |
| `backend/app/event_log.py`（新） | **事件表**：带时刻、只增不减、去重（局内内存） |
| `backend/app/decay.py`（新） | **遗忘权重 → 4 档淡化**（纯函数） |
| `backend/app/agent.py`（改） | 接线：偏航决策、事件写入（我下错 / 玩家好评·坏评 / 聊天） |
| `backend/app/prompts.py`（改） | `event_block()`；**插在铁律段之前** |
| `backend/app/prompt_chains.py`（改） | 区块接线（区块表见 spec 6.5） |
| `backend/app/main.py`（改） | `/api/config` 透传 `emotion_drift` / `chat_digest_llm` |
| `game/scripts/core/AzureConfig.gd`、`net/NetClient.gd`、`ui/Main.gd`（改） | 设置抽屉里**一格**开关 |
| `backend/tests/test_pv_lines.py`、`test_event_log.py`、`test_decay.py`（新） | 门卫 |

---

### Task P1: 预测线 A / B / C（纯函数，零引擎调用）

**Files:** Create `backend/app/pv_lines.py`；Test `backend/tests/test_pv_lines.py`
**Interfaces:** `extract_lines(move_infos, *, pv_len=5, diverge_min=2, max_drop=0.05, candidates=10) -> dict`——返回 `a` / `b` / `c` 三条线（形状见 spec 6.1）
- [ ] **Step 1: 写失败测试**
```python
def test_a_is_top_candidate_pv(self):        # A = moveInfos[0] 的首手 + pv 前 4 手
def test_short_pv_degrades(self):            # pv 不足 5 手 → 用可用长度，不抛
def test_b_differs_from_a(self):             # B 与 A 不同手数 ≥ diverge_min
def test_c_differs_from_a_and_b(self):       # C 与 A、B 都 ≥ diverge_min；凑不齐 → c=None
def test_candidates_rejected_when_too_weak(self):  # 胜率差 > max_drop 的候选不入选
def test_no_engine_call(self):               # 断言只消费传入的 move_infos（不触 engine）
```
- [ ] **Step 2: 跑测试确认失败** → `python -m unittest tests.test_pv_lines -v`，FAIL `ModuleNotFoundError`
- [ ] **Step 3: 实现** —— 只读 `move_infos` 的 `move` / `pv` / `winrate` / `scoreLead`，**不新增任何 `katago.analyze`**；**依次**凑 B、C（spec 第五节 B 条）；`pv` 不足五步时用可用长度、只做做得到的线
- [ ] **Step 4: 跑测试确认通过** → `python -m unittest discover -s tests` 全绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,pv): 预测线 A/B/C 提取，零额外引擎调用（v3.8.0）"`

---

### Task P2: 情绪波动开关 + 走偏优解

**Files:** Modify `backend/app/agent.py`、`backend/app/main.py`、`game/scripts/core/AzureConfig.gd`、`game/scripts/net/NetClient.gd`、`game/scripts/ui/Main.gd`；Test `backend/tests/test_agent.py`
**Interfaces:** `AzureAgent._maybe_drift(lines, mood, rng) -> str`（返回 `"a"` / `"b"` / `"c"`）；配置 `emotion_drift`(bool，默认 **false**)、`drift_base`(0.03)、`drift_max`(0.08)、`pv_max_drop`(0.05)
- [ ] **Step 1: 写失败测试**
```python
def test_drift_off_always_a(self):           # emotion_drift=false → 恒走 A（与现状同构）
def test_drift_probability_clamped(self):    # p_drift 截断到 [0, drift_max]
def test_drift_rng_reproducible(self):       # 注入 rng → 决策可复现
def test_ladder_prefers_a_then_b_then_c(self):  # P(走A) > P(走B) > P(走C)
def test_illegal_first_move_falls_back(self):   # C→B→A 逐级回落
def test_weak_line_never_taken(self):        # drop > pv_max_drop → 该线不用
```
- [ ] **Step 2: 跑测试确认失败** → FAIL（无 `_maybe_drift`）
- [ ] **Step 3: 实现** —— 决策插在**选出 A 之后、真正落子之前**；阶梯概率选线、首手**复用既有合法性检查**、**C→B→A 逐级回落**；走 B/C 时**记一条事件**（Task P3）
- [ ] **Step 4: 跑测试确认通过** → 全绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,emotion): 情绪波动开关，小概率走偏优解（默认关）（v3.8.0）"`

---

### Task P3: 事件表（带时刻、只增不减、去重）

**Files:** Create `backend/app/event_log.py`；Modify `backend/app/agent.py`（四个写入点）；Test `backend/tests/test_event_log.py`
**Interfaces:** `EventLog.add(kind, move_no, text, detail)` / `all()` / `reset()`；`kind ∈ {ai_bad, player_bad, player_good, chat, joseki}`；**去重键 = `kind` + 归一化 `text`**；形状见 spec 6.3
- [ ] **Step 1: 写失败测试**（重复不写；`all()` 覆盖从开局到当前；`reset` 清空；**不进快照**、悔棋不回退）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(state,events): 局内事件表（带时刻、只增不减、去重）（v3.8.0）"`

---

### Task P4: 遗忘权重 → 4 档淡化（纯函数）

**Files:** Create `backend/app/decay.py`；Test `backend/tests/test_decay.py`
**Interfaces:** `DECAY_SPAN = [1, 3, 6]`、`DECAY_DROP_AT = 12`、`level(event_move_no, now_move_no) -> int`（1..4）；文案模板见 spec 6.4
- [ ] **Step 1: 写失败测试**（`[1,3,6]` 边界；`DECAY_DROP_AT` 之后失效；方位词按坐标核对，核不上不说）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(prompts,decay): 事件遗忘权重与 4 档淡化（v3.8.0）"`

---

### Task P5: 提示词事件区块（增量翻新）

**Files:** Modify `backend/app/prompts.py`、`backend/app/prompt_chains.py`；Test `backend/tests/test_prompts.py`
**Interfaces:** `prompts.event_block(events: list[dict], now_move_no: int, max_chars: int = EVENT_BLOCK_MAX_CHARS) -> str`
- [ ] **Step 1: 写失败测试**
```python
def test_empty_block_keeps_golden_identical(self):  # events 为空 → ""，prompts.json 逐字符一致
def test_block_sits_before_ironclad(self):          # 只能插在铁律段之前
def test_over_budget_drops_oldest_and_lowest(self): # 超 max_chars → 丢【最旧 → 最低档】
def test_injected_events_do_not_enter_reply(self):  # 事件原文永不进台词
```
- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现** —— 组织成**区块表**（来源 / 插入点 / 上限 / 失效条件，见 spec 6.5）
- [ ] **Step 4: 跑测试确认通过** → 全绿（含 `tests/golden/prompts.json`）
- [ ] **Step 5: 提交** → `git commit -m "feat(prompts,events): 事件区块（插铁律段之前，空则逐字符不变）（v3.8.0）"`

---

### Task P6: 聊天事件写入（主题裁切 + 去重，默认零 LLM）

**Files:** Modify `backend/app/agent.py`（`chat()` 之后；`chat_digest_llm=true` 时在 `summarize()` / `idle` 补做）；Test `backend/tests/test_agent.py`
**Interfaces:** 规则式 `chat_topics(history) -> list[(主题, 摘要)]`；可选 LLM 版只在**局末 / 空闲**跑
- [ ] **Step 1: 写失败测试**（默认**零 LLM**：用假 LLM 断言调用数不变；重复主题不写；覆盖从开局起）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,chat): 聊天按主题分段入事件表，默认零额外 LLM（v3.8.0）"`

---

### Task P7: 文档与版本（v3.8.0）

**Files:** Modify `README.md`、`AGENTS.md`
- [ ] **Step 1: 改 `README.md`** —— 第三节字段表补 `emotion_drift` / `chat_digest_llm` 与常量（`pv_diverge_min` / `pv_max_drop` / `DECAY_SPAN`）；**部署说明补两条硬要求**：引擎配置**必须显式设 `analysisPVLen`**、**三线需要更高 `maxVisits`（默认 100 → 建议 200）**；界面说明补开关位置；「九、更新」加 v3.8.0。
- [ ] **Step 2: 改 `AGENTS.md`** —— 补三条硬规则：**线 A/B/C 只用既有 `moveInfos`、不得新增引擎调用**；**事件表存储完整、注入有界**；**事件区块为空必须逐字符不变、事件原文永不进台词**。
- [ ] **Step 3: 全量门卫 + 联动冒烟**（后端全量 + `katago_service` 全量 + 三个前端 headless 脚本 + `--headless --quit`）。
- [ ] **Step 4: 提交** → `git commit -m "docs(readme,agents): 策略线、情绪波动与事件表说明（v3.8.0）"`

---

# 阶段四 · 玩家棋风画像 + 时间感知（v3.9.0）

> **Spec**：[`specs/2026-10-17-player-profile-and-time-awareness-design.md`](specs/2026-10-17-player-profile-and-time-awareness-design.md)。
> 独立于阶段一 / 二 / 三，可单独实施与发布。
> **第一条纪律：写入只在 `summarize()`（手动结束）里、回合内零新增调用；画像块 / 时间块为空必须逐字符不变。**

## 文件结构（先锁定职责）

| 文件 | 职责 |
| --- | --- |
| `backend/app/profile_store.py`（新） | `profile.db` 读写（`games` / `profile` 两表）。**只有这一处碰盘** |
| `backend/app/profile.py`（新） | 四维特征提取 + **EMA 衰减融合**（纯函数，可注入系数） |
| `backend/app/time_ctx.py`（新） | 时间事实（now / 星期 / 本局时长 / 单手用时 / 上次间隔 / 连续天数），**时钟可注入** |
| `backend/app/prompts.py`（改） | `profile_note()` / `time_note()`；**插在铁律段之前** |
| `backend/app/prompt_chains.py`（改） | 区块接线 |
| `backend/app/agent.py`（改） | `summarize()` 后写记录 + 更新画像；单手用时打点 |
| `backend/app/main.py`（改） | 装配 + `GET /api/profile`（只读）+ `/api/config` 透传两个开关 |
| `game/scripts/core/AzureConfig.gd`、`net/NetClient.gd`、`ui/Main.gd`（改） | 面板一格 + 两个开关 |
| `backend/tests/test_profile_store.py`、`test_profile.py`、`test_time_ctx.py`（新） | 门卫 |

### Task Q1: 对局记录 + 存储层（`profile.db`）

**Files:** Create `backend/app/profile_store.py`；Test `backend/tests/test_profile_store.py`
**Interfaces:** `ProfileStore(db_path)`：`add_game(rec) -> int` / `last_game_ts() -> float|None` / `games(n=…) -> list` / `get_profile() -> dict` / `upsert_axes(dict)`；表结构见 spec 6.1
- [ ] **Step 1: 写失败测试**（库被删 / 只读 / 目录不存在 → **降级为「无画像」不抛**；`games` 只增不减；空库 `last_game_ts` → `None`）
- [ ] **Step 2-4:** 失败 → 实现（SQLite；只在 `summarize()` 调用、一律 `asyncio.to_thread`）→ 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(profile,store): 对局记录与 profile.db 存储层（v3.9.0）"`

### Task Q2: 画像四维 + EMA 融合

**Files:** Create `backend/app/profile.py`；Test `backend/tests/test_profile.py`
**Interfaces:** `extract_axes(game_rec) -> dict[str,float]`（四组维度键见 spec 6.2）；`merge(prev, x, n_games, *, half_life=20, alpha_cap=0.25) -> dict`
- [ ] **Step 1: 写失败测试**（EMA 正确；**单局 α 上限**；置信度随局数上升；缺维当 0、不抛）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(profile,core): 四维特征与指数衰减融合（v3.9.0）"`

### Task Q3: 画像 / 时间区块注入

**Files:** Modify `backend/app/prompts.py`、`backend/app/prompt_chains.py`；Test `backend/tests/test_prompts.py`
**Interfaces:** `prompts.profile_note(profile, now_move_no, max_chars=PROFILE_MAX_CHARS) -> str`、`prompts.time_note(t, max_chars=TIME_MAX_CHARS) -> str`
- [ ] **Step 1: 写失败测试**（**两块为空 → `''` 且 `golden/prompts.json` 逐字符不变**；**只能插在铁律段之前**；超预算丢「最低置信 / 最旧维度」）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(prompts,profile): 画像与时间区块（插铁律段之前，空则逐字符不变）（v3.9.0）"`

### Task Q4: 时间感知（采集 + 注入）

**Files:** Create `backend/app/time_ctx.py`；Modify `backend/app/agent.py`（单手用时打点）、`backend/app/main.py`；Test `backend/tests/test_time_ctx.py`
**Interfaces:** `time_ctx.facts(*, now, session_start, last_game_ts, move_ts_list) -> dict`（字段见 spec 6.3 的例子）
- [ ] **Step 1: 写失败测试**（**首手无前一手 → 单手用时不计**；跨日 / 跨时区边界；连续天数计算）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,time): 时间感知（当前时间 / 本局时长 / 单手用时 / 间隔 / 连续天数）（v3.9.0）"`

### Task Q5: 主动约战 / 提醒（连接时一次）

**Files:** Modify `backend/app/agent.py`、`backend/app/main.py`；Test `backend/tests/test_agent.py`
**Interfaces:** `AzureAgent.greet_with_context()` —— 玩家连上 / 首次交互时读「距上次对局」决定是否邀请；**模板句，不新增 LLM 调用**
- [ ] **Step 1: 写失败测试**（**只在连接 / 首次交互触发一次**；假 LLM **断言调用数不变**）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,time): 主动约战提醒（连接时一次，零新增调用）（v3.9.0）"`

### Task Q6: 画像可视化面板（只读）

**Files:** Modify `backend/app/main.py`（`GET /api/profile`）、`game/scripts/net/NetClient.gd`、`game/scripts/ui/Main.gd`；Test `game/tests/test_config.gd`（保持 24/24）
- [ ] **Step 1: 写失败测试**（`GET /api/profile` 返回各维 + 置信度 + **来源局**；无画像 → 空结果、**不 500**）
- [ ] **Step 2-4:** 实现 + 前端一格（只读、无副作用）→ 前端门卫全绿
- [ ] **Step 5: 提交** → `git commit -m "feat(api,ui): 画像可视化面板（只读）（v3.9.0）"`

### Task Q7: 局末进步报告（并进现有总结）

**Files:** Modify `backend/app/agent.py`（`summarize()` 提示词）、`backend/app/prompts.py`；Test `backend/tests/test_agent.py`
- [ ] **Step 1: 写失败测试**（把「上一版画像 + 本局记录」喂进**现有** summarize 提示词；**LLM 调用数与改动前一致**）
- [ ] **Step 2-4:** 失败 → 实现 → 全量绿
- [ ] **Step 5: 提交** → `git commit -m "feat(agent,summary): 局末进步报告并进现有总结，不新增调用（v3.9.0）"`

### Task Q8: 文档与版本（v3.9.0）

**Files:** Modify `README.md`、`AGENTS.md`
- [ ] **Step 1: 改 `README.md`** —— 第三节字段表补 `profile_enabled` / `time_aware_enabled`；说明画像四维、面板入口、「只在相关时提」的用法；「九、更新」加 v3.9.0。
- [ ] **Step 2: 改 `AGENTS.md`** —— 补硬规则：**写入只在 `summarize()`**；**画像 / 时间块空则逐字符不变**；**时间与画像只在相关时提、禁止把数字当口头禅复述**；**`profile.db` 只是运行产物**。
- [ ] **Step 3: 全量门卫 + 联动冒烟**。
- [ ] **Step 4: 提交** → `git commit -m "docs(readme,agents): 棋风画像与时间感知说明（v3.9.0）"`

---

## 收尾（各阶段完成后）

1. **一并验证**：后端全量 + `katago_service` 全量 + 三个前端 headless 脚本 + `--headless --quit`；整体联动冒烟（起后端 + 起 KataGo 服务（缺引擎也行）+ 前端 headless 连上 WS）。
2. **四份 spec 的验收 grep**：四份设计文档里每一条「必须 / 不许 / 不得回退」都要能指到一处代码或一个测试。
3. **parked Minor 清单**：把评审留下的次要项汇成一张表交用户分诊。
4. **合入决策**：① 本地合回 `main` ② 推送开 PR ③ 继续保留分支——问用户。注意 `docs/`、`AGENTS.md` 与全部计划只在分支上，合并时务必带上。

---

## 附录 A · 优化点（本轮追加，供分诊）

| # | 优化点 | 为什么 | 落点 |
| --- | --- | --- | --- |
| O1 | 门控做成**纯函数 + 注入 rng** | 概率逻辑最难测；注入 rng 才能复现 | Task 5 |
| O2 | 用**频率层统一**「前 3 次」与「第 4 次起」 | 两条规则合成「资格层 + 频率层」，规则更少、更一致 | Task 5（取代旧 Task 5） |
| O3 | **走错绕过概率、永远优先** | 纠错时刻不该被随机吞掉 | Task 5 用例 |
| O4 | **冷却窗口**（同族 N 手内不重复讲） | 防连手讲同一个，守住「她变话多」反目标 | Task 5 |
| O5 | 门控**只改文本 / 卡片**，事件配对与 `busy` 不碰 | 守住音画同步与 busy 硬规则 | Task 5 + Global Constraints |
| O6 | **未命中 == 未识别**同一条出口 | 少一条分支、少一类 bug；「关掉定式＝纯点评」可直接验收 | Task 5 |
| O7 | UI 用**一格**（开关 + 频率合一）放 v3.7 设置抽屉 | 守住「界面更挤」反目标，不新增控件位 | Task F（D1）+ Task 5 |
| O8 | 门控决策留**一行日志**（rate / adj / roll / 命中） | 便于调参、事后归因 | Task 5 |
| O9 | 画像**复用局内既有信号**，不新建持久化 | 减复杂度、少一个运行产物 | Task 5（可推翻） |
| O10 | `joseki_rate` 进 `config.json` 字段表 + `/api/config` 透传 | 与既有开关同一条通道，不新造 | Task 9 |
| O11 | **文档链接与本机工作区核对**：Spec 链接改为相对本文件夹；`G:\GodotProject\Azure-Go` 本机不存在 | 计划自洽、执行时能找到入口 | 本文档头部 + 附录 B |
| O12 | **口径统一**：定式 spec 里其余「必说 / 开口」表述（第二节目标 1 / 2、第八节 8.2）与第五节第 5 条（频率可调）冲突，需加同一限定词「结构点走频率层，走错必说」 | 免得实现时两处口径打架（第五节第 2 条已修） | 定式 spec 第二 / 八节 |
| O13 | **线 A/B/C 只吃既有 `moveInfos`**（含 `pv`），**不新增任何引擎分析** | 这是阶段三能否「不拖慢对局」的胜负手 | Task P1 |
| O14 | **事件表「存储完整 / 注入有界」分离** | 「包含每个事件」与「提示词预算」本是对立；分离两者才同时成立 | Task P3 + P5 |
| O15 | 聊天摘要**默认规则式**，LLM 版只在**局末 / 空闲**补做 | 守住「落子回合内零额外 LLM 调用」 | Task P6 |
| O16 | 情绪波动**默认关 + 概率/胜率差双上限 + 注入 rng** | 关掉＝现状；开启也不下烂棋；可复现可测 | Task P2 |
| O17 | **三线共用同一次分析**，代价是**更深的搜索**（`maxVisits` 100→200）；部署侧**必须显式设 `analysisPVLen`** | 实测结论：三线可行，但 `pv` 长度由 visits 决定；不增加调用次数 | Task P1 + P7 |
| O18 | 画像用**指数衰减 EMA + 单局 α 上限** | 「微增量」的字面实现；防一局爆冷改写画像 | Task Q2 |
| O19 | **画像块 / 时间块空则逐字符不变** + 字符预算 | 守住 `golden/prompts.json` 与提示词预算 | Task Q3 |
| O20 | 每维**可追溯到来源局**（`games` 表只增不减） | 面板与进步报告都要「凭什么这么说」 | Task Q1 + Q6 |
| O21 | 库被删 / 只读 / 目录不存在 → **当「无画像」**、绝不影响对局 | 沿用长期记忆那套降级纪律 | Task Q1 |
| O22 | 时间与画像**只在相关时提**、**禁止把数字当口头禅** | 守住「她变话多」反目标 | Task Q3 + Q8 |
| O23 | 写入走 `asyncio.to_thread`；**只在 `summarize()`** | 异步纪律；回合内**零新增调用** | Task Q1 + Q7 |
| O24 | 进步报告**并进现有 `summarize()` 提示词** | 不新增 LLM 调用、不碰 `MAX_LLM_CALLS_PER_TURN` | Task Q7 |
| O25 | `profile.db` 与 `memory.db` **解耦**（memory 关闭时画像照常） | 两个功能互不牵连 | Task Q1 |

---

## 附录 B · 结构一览（逻辑主线）

```
定式识别（Task 2：scan 统一结构）
  └─ 资格层（Task 4 族计数 + Task 3 走错判定）        ← 该不该教
       └─ 频率层（Task 5 门控：rate ± 画像/情绪 ±10%，冷却，走错优先）  ← 这次说不说
            ├─ 命中 → 结构点开口（Task 7）+ 浮层卡片（Task 8）+ 事件扩字段（Task 6）
            └─ 未命中 → 与「未识别」同一条出口：只做普通点评

阶段三（v3.8.0）：
既有 after_infos ──> 线 A / B / C（P1，零引擎调用）
                     └─ 情绪波动开关（P2）→ A/B/C 阶梯，越偏越少；C→B→A 逐级回落
对局事件 ──────────> 事件表（P3：带时刻、只增不减、去重）
                     └─ 遗忘权重 → 4 档淡化（P4）→ 事件区块（P5：插铁律段之前，空则逐字符不变）
聊天回复 ──────────> 按主题裁切入表（P6：默认零额外 LLM）

阶段四（v3.9.0）：
手动结束（summarize）──> 对局记录（Q1：games 表）──> 四维特征 + EMA 融合（Q2）──> profile
提示词 ──────────────> 时间块 + 画像块（Q3 / Q4：插铁律段之前，空则逐字符不变）
玩家连上 ────────────> 主动约战提醒（Q5：一次、零新增调用）
面板 / 总结 ─────────> GET /api/profile（Q6，只读）· 进步报告并进 summarize（Q7）
```
