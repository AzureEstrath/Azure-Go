# Azure-Go 增量清单与实现文档

- 生成日期：2026-10-18
- 比较对象：`.superpowers/new_plan/`（新计划集） **vs** 现有内容（`docs/superpowers/` 已入库的计划与 spec、当前代码 HEAD、`.superpowers/sdd/` 两份 ledger）
- 工作区：`G:\GodotProject\Azure-Go`，分支 `main`，HEAD `90debd6`（阶段一已交付、阶段二 A–D 已完成）
- 本文档性质：**交接用实现文档**。它只描述「增量」，已完成部分只作为基线引用，不重复施工方案。

---

## 0 · 结论速览（一页）

### 0.1 文档侧增量

| # | 增量 | 载体 | 已入库？ |
| --- | --- | --- | --- |
| D1 | master plan 扩写 **+363 / -35 行**：新增「决策摘要」「核心机制：定式提示门控」「阶段三」「阶段四」「附录 A · O1–O25」「附录 B · 结构一览」；阶段一 Task 5 被**整段重写** | `plans/2026-10-17-joseki-and-ux-master.md` | ❌（`docs/` 里仍是旧版 49.7KB vs 新版 78.2KB） |
| D2 | 新增 spec：**策略线与情绪波动 + 事件表（v3.8.0）** | `specs/2026-10-17-strategy-lines-and-event-log-design.md` | ❌ 缺失 |
| D3 | 新增 spec：**玩家棋风画像 + 时间感知（v3.9.0）** | `specs/2026-10-17-player-profile-and-time-awareness-design.md` | ❌ 缺失 |
| D4 | 定式 spec 打补丁：新增 §6.1「定式提示频率可调」；决策表第 2、5 行改成「门控（资格层 + 频率层）、走错必说」口径 | `specs/2026-10-17-joseki-active-teaching-design.md` | ❌（`docs/` 里是旧口径） |
| D5 | UX spec | `specs/2026-10-17-ux-and-reliability-batch-design.md` | ✅ 逐字节一致，**无增量** |

### 0.2 代码侧增量（4 组）

| 组 | 内容 | 对应版本 | 现状 |
| --- | --- | --- | --- |
| **G1** | **阶段一回归补丁：定式提示门控**（`joseki_gate.py` + `joseki_rate` + 画像/情绪 ±10% + 冷却 + 走错优先 + UI 一格） | 原为 v3.6.0 | ❌ 未实现——现有 `AzureAgent.joseki_show_card()`（`agent.py:837`）仍是**旧规则**「前 3 次都上卡片、第 4 次起只在走错时上」（`586881d`） |
| **G2** | **阶段二剩余 5 项**：`E` watchdog+代际守卫、`F` 设置抽屉、`G` 视角/悬停手数、`H` 目差说人话、`I` 文档与版本 | v3.7.0 | ❌ 未开始（A–D 已完成：`f4951de`/`ed0a45d`/`4eba17c`/`fa03f4b`） |
| **G3** | **阶段三全部**：`pv_lines.py` / `event_log.py` / `decay.py` + 情绪波动 + 事件区块 | v3.8.0 | ❌ 全新（7 个新模块全不存在） |
| **G4** | **阶段四全部**：`profile_store.py` / `profile.py` / `time_ctx.py` + 画像·时间区块 + 三个延伸 | v3.9.0 | ❌ 全新 |

### 0.3 一句话

> 新计划集 = **旧计划（阶段一/二）** ＋ **阶段一 Task 5 的门控重写** ＋ **两份全新 spec（阶段三/四）** ＋ **附录 A/B**。
> 其中阶段一其余 11 个任务、阶段二 A–D **已经落地，不重做**；真正要动手的是 G1、G2（E/F/G/H/I）、G3、G4 四组。

---

## 1 · 比较方法与证据

| 比较对象 | 位置 | 手段 | 结果 |
| --- | --- | --- | --- |
| master plan | `docs/superpowers/plans/…` vs `.superpowers/new_plan/plans/…` | `git diff --no-index` | +363 / -35；差异集中在头部（决策摘要/核心机制/Global Constraints/文件结构）、Task 5 整段、Task 9 两行、收尾段、新增阶段三/四与附录 A/B |
| 定式 spec | `docs/superpowers/specs/…` vs `new_plan/specs/…` | `git diff --no-index` | +9 / -2：决策表第 2、5 行 + 新增 §6.1 |
| UX spec | 同上 | 哈希 | 逐字节相同 |
| 阶段三 spec | 仅存于 `new_plan/specs/` | 存在性 | 仓库内**不存在** |
| 阶段四 spec | 同上 | 存在性 | 仓库内**不存在** |
| 代码现状 | `backend/app/`、`game/scripts/`、`game/tests/` | 文件存在性 + 符号 grep | `joseki_gate.py`/`pv_lines.py`/`event_log.py`/`decay.py`/`profile_store.py`/`profile.py`/`time_ctx.py` **均不存在**；`joseki_rate`/`emotion_drift`/`chat_digest_llm`/`profile_enabled`/`time_aware_enabled` **均无任何引用** |
| 已实现基线 | `state.py`、`main.py` | grep | `move_notes`/`losses`/`joseki_counted`/`joseki_dropped` 已存在；`/api/review`、`/api/review/goto`、`GET /api/review/notes`、`review_gate()` 已存在 → 阶段二 A–D 确认完成 |
| 流程账本 | `.superpowers/sdd/07-…/progress.md`、`08-…/progress.md` | 尾部「剩余工作」 | 阶段二明确写：`E`（最高价值）、`F+G+H`（可合批）、`I`、阶段二终审 未做；README 版本号仍是 v3.6.0 |

---

## 2 · 增量清单（逐条）

### 2.1 全局约束层的新增（3 条，适用于 G1）

来自 master plan 的 Global Constraints：

1. **门控只改文本与卡片**：`joseki_gate` 的决策**不得**改动棋子、事件配对（`message`/`speech`）、`busy` 与状态机；**未命中 == 未识别**，两条路径逐字段同构。
2. **概率必须可复现**：`joseki_gate` 一律接收**注入的 rng**（默认系统随机），单测不得依赖真随机。
3. **门控不得新增持久化**：棋力画像只从**局内既有信号**（玩家 rank / 亏损手比例 / 族计数）推，**不新建数据库**。

另加 Review Focus 第 6 条（门控边界回归）：`rate=0/1`、画像/情绪取不到、冷却期同族连出、走错与随机撞车。

### 2.2 文件结构层的新增

| 阶段 | 新增文件 | 修改文件 |
| --- | --- | --- |
| G1（阶段一补丁） | `backend/app/joseki_gate.py`、`backend/tests/test_joseki_gate.py` | `agent.py`、`agent_graph.py`、`main.py`、`AzureConfig.gd`、`NetClient.gd`、`Main.gd`、`test_config.gd` |
| G2-E | — | `agent.py`、`agent_graph.py`、`tests/test_agent_graph.py` |
| G2-F/G/H | `game/tests/test_view_helpers.gd`、`game/tests/test_score_phrase.gd` | `Main.gd`、`Board3D.gd`、`test_config.gd` |
| G3（v3.8.0） | `backend/app/pv_lines.py`、`event_log.py`、`decay.py` + `tests/test_pv_lines.py`、`test_event_log.py`、`test_decay.py` | `agent.py`、`prompts.py`、`prompt_chains.py`、`main.py`、`AzureConfig.gd`、`NetClient.gd`、`Main.gd` |
| G4（v3.9.0） | `backend/app/profile_store.py`、`profile.py`、`time_ctx.py` + `tests/test_profile_store.py`、`test_profile.py`、`test_time_ctx.py` | `prompts.py`、`prompt_chains.py`、`agent.py`、`main.py`、`AzureConfig.gd`、`NetClient.gd`、`Main.gd`、`test_config.gd` |

### 2.3 未被新计划覆盖、但属于「已知缺口」的增量项（来自 ledger，供分诊）

| # | 缺口 | 来源 | 影响的增量项 |
| --- | --- | --- | --- |
| X1 | `R-1`：4 个 2 手族从 `formed` 改成 `entered` 后，`agent_graph.py` 三处仍按 `formed` 门控（`:236` 情绪、`:294` `_len_note`、`:716` 玩家侧 LLM 失败兜底） | 阶段一 ledger R-1（Low） | G1（同一函数族，建议顺手修） |
| X2 | `R-2`：golden 的 `scan` 用例名未跟改（标签漂移，不影响断言） | 阶段一 ledger R-2 | G1 可选 |
| X3 | `R-3`：卡片单槽交互——同回合两侧都发 `joseki` 时后者会 `clear()` 前者 | 阶段一 ledger R-3 | G1（门控降低触发频率后概率变小但仍存在） |
| X4 | `R-4`：`joseki_dropped` 只写不读；`docs/superpowers/` 两份设计文档仍是 U52 之前的旧口径 | 阶段一 ledger R-4 | **本文档 §5 批次 0** |
| X5 | 扩库缺口：Task 11c 的**目外/高目**未做（`57e24fd` 标为「可砍项」）；「飞压/夹击」两条无法从自对弈提取 | 阶段一提交记录 | 独立小项，非阻塞 |

---

## 3 · 实现文档（按组）

> 通用纪律（全组适用，来自项目既有 `AGENTS.md` 与两份 ledger）：
> 1. 唯一工作区 `G:\GodotProject\Azure-Go`；动手前 `git log --oneline -3` + `git status`。
> 2. **禁止任何 `git stash` 操作**（`stash@{0}` 是迁移前存档，不许动）。
> 3. 同步磁盘/DB 调用一律 `await asyncio.to_thread(...)`；任何存储故障只打日志、绝不影响对局。
> 4. 既有常量不放宽：`MAX_LLM_CALLS_PER_TURN = 4`、`AI_DEDUP_WINDOW = 8`。
> 5. `tests/golden/prompts.json` 锁定 `context_block`：`long_term` 为空串时**逐字符不变**；新内容**只能插在铁律段之前**。
> 6. 前端 `Main.gd` **没有 `class_name`** ⇒ 门卫只能 `preload(...)` 调**静态函数**；`Board3D` 的纯逻辑要抽成静态/可注入函数。
> 7. 本机只有 Godot **4.7.1**：跑完把 `game/project.godot` 的 `config/features` 还原为 `4.6`，还原任务外的 `*.import`；新增 `*.gd.uid` 要入库。
> 8. 提交格式：`type(scope): 中文描述（vX.Y.Z）`；版本号由各阶段最后一个「文档与版本」任务一次写入。

---

### 3.1 G1 · 定式提示门控（阶段一回溯补丁）

> 原计划 Task 5「前 3 次都上卡片」被**整段替换**：资格层（族计数 < 3 或走错）在前，频率层（`joseki_rate` ± 画像/情绪 ±10%、冷却、走错优先）在后。**当前代码是旧规则，需要改造。**

**核心机制（两层）**

| 层 | 决定什么 | 规则 |
| --- | --- | --- |
| 资格层 | 这条定式该不该教 | 族计数 `< 3` → 有资格；`≥ 3` → 只在「走错」（真亏且偏离库线）时有资格；`joseki_teach=false` → 一律无资格 |
| 频率层 | 这次说不说 | 有资格时掷 `p`：命中 → 讲解 + 卡片；未命中 → **与「未识别定式」逐字段同构**（只点评这一手）|

```
p = clamp( joseki_rate + skill_adj + mood_adj, 0, 1 )
  joseki_rate ∈ [0,1]           # 默认 0.5，用户可调
  skill_adj   ∈ [-0.10, +0.10]  # 棋力画像：生疏/没见过 → 正；熟练 → 负
  mood_adj    ∈ [-0.10, +0.10]  # 情绪：好奇/兴奋 → 正；疲惫/低落 → 负
```

**优先级（不被概率压过）**：① 走错 → 必讲（绕过频率层）② 结构点（进入/分岔/收尾）→ 走频率层 ③ 中间推进 → 一律不讲。

**实现要点**

| 项 | 落点 | 说明 |
| --- | --- | --- |
| 新模块 | `backend/app/joseki_gate.py` | **纯函数 + 可注入 rng**，无副作用 |
| 接口 | `eligible(seen, kind, mistake, teach_on) -> bool` | 资格层 |
| | `probability(rate, skill_adj, mood_adj) -> float` | `clamp(rate+skill_adj+mood_adj, 0, 1)` |
| | `should_show(*, rng, rate, skill_adj, mood_adj, seen, kind, mistake, teach_on, last_taught_move, move_no, cooldown=8) -> bool` | 最终决策（含冷却、走错优先） |
| | `skill_adj(ranks, seen_count) -> float`、`mood_adj(mood: dict) -> float` | 各自截断到 ±0.10；**取不到 → 0.0，不抛** |
| 接线 | `agent.py` `joseki_show_card()`（:837）/ `joseki_emit`（:790）/ `joseki_count_entry()`（:879） | **在既有函数上接频率层**，不要重写资格层与族计数（`joseki_progress.py`、`joseki_counted`/`joseki_dropped` 已在位） |
| 冷却 | `agent` 内「族 → 最近讲过的手数」 | 窗口 `N=8`（与 `AI_DEDUP_WINDOW` 同口径）；窗口内按**未命中**处理 |
| 配置 | `config.py`：`joseki_rate: float = 0.5`；`/api/config` 透传；`AzureConfig.gd`、`NetClient.gd` | 「定式导学」由单 bool 升级为「开关 + 频率」，**UI 用一格**承载（下拉：关闭 / 低 25% / 中 50% / 高 75%，或滑条），**不新增右栏控件位** |
| UI 落点 | `Main.gd` `_chk_joseki`（:373）→ 改为「一格」复合控件 | 与 G2-F 的设置抽屉合并处理（见 §5 批次顺序） |
| 日志 | 决策留**一行**（rate / adj / roll / 命中） | 便于调参、事后归因 |
| 硬约束 | 门控**只决定文本与卡片** | 不改棋子、不改 `speech` 配对、不改 `busy`；画像/情绪取不到 → `adj=0` |

**门卫 `backend/tests/test_joseki_gate.py`（11 例）**

```python
def test_rate_zero_never_shows(self):             # p=0 → 必不说（走错除外）
def test_rate_one_always_shows(self):             # p=1 → 必说
def test_adj_clamped_to_plus_minus_10pct(self):   # 两个 adj 各自截断 ±0.10
def test_rng_is_reproducible(self):               # 注入固定 rng → 可复现
def test_miss_is_identical_to_no_match(self):     # 未命中与未识别事件集合逐项相等
def test_teach_off_is_silent(self):               # joseki_teach=false → 无 joseki/讲解/卡片
def test_cooldown_suppresses_repeat(self):        # 同族 N 手内不重复讲
def test_mistake_bypasses_frequency(self):        # 走错 → 必讲
def test_missing_profile_or_mood_degrades(self):  # 缺失 → adj=0，不抛
def test_same_family_counts_once_per_game(self):  # 同局第二次进同族不再 +1
def test_undo_returns_the_count(self):            # bump 后 undo → seen 回原值
```

**验收**：`cd backend && python -m unittest discover -s tests` 全绿（基线 483 OK，只许增不许减）；`godot --path game --headless --script res://tests/test_config.gd` 24/24（新字段读写 + 未知键保留）。

**提交（可拆两次）**

```
feat(joseki,teach): 定式提示门控（开关+频率+画像/情绪±10%+冷却），走错优先、未命中只点评（v3.6.x）
docs(readme,agents): 定式导学＝开关+频率；未命中==未识别（v3.6.x）
```

> **版本落点待裁决**：该补丁原属 v3.6.0（已交付），现落在 v3.7.0 之上。建议**并入 v3.7.0**（阶段二发布时一并写文档），避免为它单独发一版。

---

### 3.2 G2 · 阶段二剩余（v3.7.0）

#### Task E · `busy` 看门狗 + 代际守卫（**最高价值**）

| 项 | 内容 |
| --- | --- |
| Files | `backend/app/agent.py`（代数 + `_set_busy` 附近）、`backend/app/agent_graph.py`（每个写入点前核对代数）、`backend/tests/test_agent_graph.py` |
| 接口 | `AzureAgent.epoch: int`；`begin_epoch() -> int`；`epoch_ok(token) -> bool`；`WATCHDOG_SEC = <最坏预算 × 1.5~2>`（值由最坏路径算出并**写在注释里**） |
| 语义 | 超时触发：`_set_busy(False)` + 前推 `epoch` + 一行日志 + 一条可读提示。**超时必须作废旧代的一切写入**（只解锁不算完成） |
| 落点纪律 | 代际核对放在**每一个写 `st.*` / 发事件的位置之前**：棋谱提交、快照、`bad_moves`、`move_notes`、`_reveal_pending`、台词事件。**不要挪动任何现有的 `_set_busy(False)`**（当时有 5+ 处），看门狗在最外层包一层 |
| 门卫 | ① 正常偏慢回合不被打断 ② 卡死 → busy 归位且只发一条可读提示 ③ 超时后旧代写入全部作废 ④ 无重复 busy 事件 |

提交：`fix(agent,core): busy 兜底超时 + 代际守卫，超时不再放开并发（v3.7.0）`

#### Task F · 右栏收纳（⚙ 设置抽屉）

| 项 | 内容 |
| --- | --- |
| Files | `game/scripts/ui/Main.gd`（`_build_right_panel`，:317）、`game/tests/test_config.gd`（保持 24/24） |
| 现状 | 右栏控件：`_chk_pass`「允许 Azure 虚着」(:366)、`_chk_joseki`「定式导学」(:373)，以及语音组 `短回复 / 同步说话 / 整段合成 / 长句合并`（`_acc_check` :385–392） |
| 动作 | 把 **同步说话 / 整段合成 / 长句合并** 收进抽屉（默认收起）；**允许虚着 / 定式导学 / 短回复** 与四个按钮、聊天框、状态栏留在外面。**左侧栏一行不动** |
| 纪律 | 只移动控件位置，**不改任何开关的绑定与生效范围**；抽屉里的开关改动后仍要写回 `config.json` 原字段（`test_config.gd` 的「未知键原样保留」不得破） |
| 步骤 | ① 先把右栏每个开关现在的生效范围（立即 / 重启后端）抄成清单 ② 实现抽屉 ③ 逐条核对清单 ④ `test_config.gd` 24/24 + `--headless --quit` 无 `SCRIPT ERROR` |

提交：`feat(game,ui): 一次性设置收进设置抽屉，右栏只留对局常用项（v3.7.0）`

> G1 的「定式导学」一格（开关+频率）**并入本任务的抽屉改造**，一次改完。

#### Task G · 视角便捷 + 悬停坐标/手数

| 项 | 内容 |
| --- | --- |
| Files | `game/scripts/ui/Board3D.gd`、`Main.gd`；Create `game/tests/test_view_helpers.gd` |
| 接口 | `Board3D.reset_view() -> void`（回启动时捕获的 yaw/pitch/dist/tilt）；`Board3D.look_at_corner(corner: int)`（四角轮转）；`Board3D.tip_text(x, y) -> String`——**空点** `"D4 · 点击落子"`（既有措辞不许改），**已有棋子** `"D4 · 第 3 手"`（手数由 `move_seq` 推） |
| 纪律 | 键位不冲突、不易误触；操作写进 README；纯逻辑抽静态函数以便 headless 门卫 |
| 门卫 | 空点文案 / 已有棋子含手数 / 盘外返回空串 |

提交：`feat(game,ui): 一键回视角/看角，悬停已有棋子显示坐标与手数（v3.7.0）`

#### Task H · 目差说人话

| 项 | 内容 |
| --- | --- |
| Files | `game/scripts/ui/Main.gd`（`_lbl_score` 一带）；Create `game/tests/test_score_phrase.gd` |
| 接口 | `Main.gd` 的 `score_phrase(score, winrate) -> String`——映射成量级人话（「差不多」「半个角」「一个角的实空」「已经很难追了」），**原始数字仍然显示** |
| 纪律 | 目差与胜率偶尔打架（胜率 51% + 目差 -2.5 正常）：两者一起看，措辞**不得**让人以为矛盾；`null`/脏值安全缺省 → `"--"` |
| 门卫 | 近均势 / 胜率与目差不同调不矛盾 / `null` 安全 |

提交：`feat(game,ui): 目差翻译成人话，原始数字保留（v3.7.0）`

#### Task I · 文档与版本（v3.7.0）

- `README.md`：「九、更新」加 v3.7.0；补复盘模式用法与边界（**可进可出、不改变「结束还能继续下」**）；设置抽屉各开关的生效范围；新键位；目差人话。
- `AGENTS.md` 补硬规则：复盘门禁与静音；**超时后必须作废旧代数的一切写入**；逐手存档两条写入点必须纯追加；（若同时做 G1）**未命中 == 未识别**、门控只改文本与卡片。
- **README 版本号当前仍是 v3.6.0**，由本任务写成 v3.7.0。
- 全量门卫 + 联动冒烟（见 §6）。

提交：`docs(readme,agents): 复盘、设置抽屉、视角与目差说明（v3.7.0）`

---

### 3.3 G3 · 阶段三：策略线与事件表（v3.8.0）

> **第一条纪律：零额外引擎调用、默认零额外 LLM 调用**；**提示词事件区块为空时必须逐字符不变**。
> **实测前提（2026-10-17）**：`moveInfos[].pv` 存在；30 visits → pv 只有 1~7 手（不够五步），**200 visits → pv 6~10 手**，前 5 个候选两两差异 ≥2 手，三条线凑得出。
> ⚠ **深度口径已裁决（见 §7.1）**：**不改全局 `max_visits`**；新增 `analyze(moves, max_visits=None)` + `pv_max_visits`（默认 150），**只在 `agent_graph.py:156` 那一次**传深值；`katago_timeout_ms` 建议 45000；`analysisPVLen` 作为部署护栏写入 README，不写成「必须」。

| Task | 文件 | 接口 / 要点 |
| --- | --- | --- |
| **P1** 预测线 A/B/C | Create `backend/app/pv_lines.py`；`tests/test_pv_lines.py` | `extract_lines(move_infos, *, pv_len=5, diverge_min=2, max_drop=0.05, candidates=10) -> dict`，返回 `a`/`b`/`c` + `drop`。只读 `move`/`pv`/`winrate`/`scoreLead`，**绝不新增 `katago.analyze`**；**依次**凑 B、C；`diff ≥ diverge_min` 且 `胜率差 ≤ max_drop`；凑不齐 → `c=None`、A/B 降级、不抛 |
| **P2** 情绪波动开关 | `agent.py`、`main.py`、`AzureConfig.gd`、`NetClient.gd`、`Main.gd`；`tests/test_agent.py` | `_maybe_drift(lines, mood, rng) -> "a"|"b"|"c"`；`p_drift = clamp(drift_base + emotion_bias(mood), 0, drift_max)`，`drift_base=0.03`、`drift_max=0.08`、`pv_max_drop=0.05`、`emotion_drift` **默认 false**；阶梯 `P(A) > P(B) > P(C)`，**C→B→A 逐级回落**；插在**选出 A 之后、真正落子之前**；首手复用既有合法性检查 |
| **P3** 事件表 | Create `backend/app/event_log.py`；`agent.py` 四写入点；`tests/test_event_log.py` | `add(kind, move_no, text, detail)` / `all()` / `reset()`；`kind ∈ {ai_bad, player_bad, player_good, chat, joseki}`；**去重键 = kind + 归一化 text**；**不进 `GameState` 快照**（悔棋不回退）|
| **P4** 遗忘权重→4 档 | Create `backend/app/decay.py`；`tests/test_decay.py` | `DECAY_SPAN = [1,3,6]`、`DECAY_DROP_AT = 12`、`level(event_move_no, now_move_no) -> 1..4`；四档文案信息量递减（原因/后果 → 事实 → 方位）；**方位按坐标核，核不上就不说** |
| **P5** 事件区块 | `prompts.py`、`prompt_chains.py`；`tests/test_prompts.py` | `event_block(events, now_move_no, max_chars=EVENT_BLOCK_MAX_CHARS) -> str`；**插在长期记忆段（#6）与铁律段（#7）之间**；空 → `''` 逐字符不变；超预算丢【最旧 → 最低档】；**事件原文永不进台词** |
| **P6** 聊天事件写入 | `agent.py`（`chat()` 之后）；`tests/test_agent.py` | 规则式 `chat_topics(history)`（时间窗 + 关键词/相似度 + 模板摘要）→ **默认零 LLM**；`chat_digest_llm=true` 时**只在局末 `summarize()` / `idle` 补做**；主题+归一化摘要去重 |
| **P7** 文档与版本 | `README.md`、`AGENTS.md` | 字段表补 `emotion_drift`/`chat_digest_llm` 与常量；**部署硬要求两条**：① 引擎配置**必须显式设 `analysisPVLen`** ② 三线需要更高 `maxVisits`；AGENTS 补：线 A/B/C 只用既有 `moveInfos`、事件表存储完整/注入有界、事件区块空则逐字符不变 |

**UI**：开关与参数只在设置抽屉里**一格**（不新增右栏控件位）。
**关键接线点**：`agent._ai_turn` 内**已有的** `after_infos`（`engine.analyze` 的 `moveInfos`，`engine.py:159` 传 `maxVisits`）。
**门卫**：`pv_lines`（A 正常/B、C 依次判据/C 凑不齐不报错）、情绪波动（关→恒 A、p 截断、注入 rng 可复现、非法点回落）、`event_log`（去重/all 覆盖/reset）、`decay`（边界取值）、事件区块（空 → 逐字符不变 + 超预算丢弃）、聊天写入（假 LLM 断言调用数不变）。

**提交序列**：P1→P7 逐个提交，版本号在 P7 一次写入。

---

### 3.4 G4 · 阶段四：玩家棋风画像 + 时间感知（v3.9.0）

> **第一条纪律：写入只在 `summarize()`（手动结束）里、回合内零新增调用；画像块 / 时间块为空必须逐字符不变。**

**存储（`backend/data/profile.db`，`backend/data/` 已被 `.gitignore` 覆盖 ✅）**

```sql
CREATE TABLE IF NOT EXISTS games(
  id INTEGER PRIMARY KEY, ts REAL, move_count INTEGER,
  final_black_wr REAL, final_score REAL, result TEXT,
  moves TEXT, per_move_ms TEXT, joseki_families TEXT, chat_topics TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS profile(
  axis TEXT PRIMARY KEY, value REAL, confidence REAL, n_games INTEGER, updated_ts REAL);
```

**四组维度键**：`open.star|komoku|sansan|mokuhazushi|favorite_kakari`、`style.aggression|tenuki|territory_vs_influence`、`blunder.rate|top_type`、`weak.corner_tl|tr|bl|br`、`persona.topic.*|address_pref`。

| Task | 文件 | 接口 / 要点 |
| --- | --- | --- |
| **Q1** 记录 + 存储层 | Create `profile_store.py`；`tests/test_profile_store.py` | `ProfileStore(db_path)`：`add_game(rec)->int` / `last_game_ts()->float|None` / `games(n)` / `get_profile()` / `upsert_axes(dict)`；**库被删 / 只读 / 目录不存在 → 降级为「无画像」不抛**；`games` 只增不减；SQLite 一律 `asyncio.to_thread` |
| **Q2** 四维 + EMA | Create `profile.py`；`tests/test_profile.py` | `extract_axes(game_rec) -> dict[str,float]`；`merge(prev, x, n_games, *, half_life=20, alpha_cap=0.25) -> dict`；`v ← v*(1-α)+x*α`，**单局 α 有上限**；置信度随局数上升；缺维当 0 |
| **Q3** 画像/时间区块 | `prompts.py`、`prompt_chains.py`；`tests/test_prompts.py` | `profile_note(profile, now_move_no, max_chars=PROFILE_MAX_CHARS)`、`time_note(t, max_chars=TIME_MAX_CHARS)`；**两块为空 → `''` 且 golden 逐字符不变**；超预算丢「最低置信 / 最旧维度」；**时间在上、画像在下**，都插**铁律段之前** |
| **Q4** 时间感知 | Create `time_ctx.py`；`agent.py`（单手用时打点）、`main.py`；`tests/test_time_ctx.py` | `facts(*, now, session_start, last_game_ts, move_ts_list) -> dict`；后端在 `POST /api/move` 到达时记时间戳、下一手结算；**首手无前一手 → 不计**；跨日/跨时区/连续天数边界 |
| **Q5** 主动约战 | `agent.py`、`main.py`；`tests/test_agent.py` | `greet_with_context()`——玩家连上 / 首次交互时读「距上次对局」决定是否邀请；**模板句，零新增 LLM 调用**；**只触发一次** |
| **Q6** 画像面板 | `main.py`（`GET /api/profile`）、`NetClient.gd`、`Main.gd`；`test_config.gd` 保持 24/24 | 只读：各维 + 置信度 + **来源局**；无画像 → 空结果、**不 500** |
| **Q7** 局末进步报告 | `agent.py`（`summarize()` 提示词）、`prompts.py`；`tests/test_agent.py` | 把「上一版画像 + 本局记录」喂进**现有** summarize 提示词；**LLM 调用数与改动前一致** |
| **Q8** 文档与版本 | `README.md`、`AGENTS.md` | 字段表补 `profile_enabled`/`time_aware_enabled`；说明四维、面板入口、「只在相关时提」；AGENTS 补：写入只在 `summarize()`；空块逐字符不变；**禁止把数字当口头禅复述**；`profile.db` 只是运行产物 |

**解耦**：`profile.db` 与 `memory.db` 互不依赖（memory 关闭时画像照常）。

---

## 4 · 继承的硬约束（不可回退，实现时逐条自检）

| # | 约束 | 落点 |
| --- | --- | --- |
| C1 | `MAX_LLM_CALLS_PER_TURN = 4`、`AI_DEDUP_WINDOW = 8` 不许放宽 | 全部新功能 |
| C2 | `tests/golden/prompts.json`：`long_term` 为空时 `context_block` **逐字符不变**；新内容**只能插铁律段之前** | P5、Q3 |
| C3 | 提示词里不得放可被模型照抄的示例句 | P5、Q3 |
| C4 | 同步磁盘/DB 调用一律 `await asyncio.to_thread(...)`；故障只降级 | Q1、G1 计数 |
| C5 | `message`/`speech` 配对、`busy` 必归位、音画同步（`ai_move` 带坐标、语音开始才 `reveal`） | G1、G2-E、G3 |
| C6 | `server/`（288/370 文件是上游 CosyVoice）不要顺手重排/美化 | 全部 |
| C7 | 前端 `Main.gd` 无 `class_name` → 门卫只能 `preload` 静态函数 | G2-F/G/H |
| C8 | Godot 本机 4.7.1：跑完还原 `project.godot` 的 `features = 4.6`、还原任务外 `*.import`；新增 `*.gd.uid` 入库 | G2-F/G/H |
| C9 | 配置读取顺序：可执行文件同目录 `config.json` → `res://config.json`；CLI 覆盖记进 `cli_locked` | 新增配置字段 |
| C10 | 禁止任何 `git stash` 操作 | 全部 |

---

## 5 · 建议实施顺序（批次）

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| **0** | **把 `new_plan` 落库**：copy 两份新 spec 进 `docs/superpowers/specs/`，用新版覆盖 master plan 与定式 spec | 否则仓库文档与执行依据不一致（X4：`docs/` 仍是旧口径） |
| **1** | **G2-E** watchdog + 代际守卫 | ledger 标注**最高价值**；纯后端、不动 UI |
| **2** | **G2-F + G1 的 UI 一格**（设置抽屉 + 定式导学＝开关+频率） | 两者改同一片 `Main.gd`，合批只做一次 UI 评审 |
| **3** | **G1 后端**（`joseki_gate.py` + 接线） | 依赖批次 2 的 `joseki_rate` 控件；顺手修 X1（R-1 的 `formed` 耦合） |
| **4** | **G2-G + G2-H**（纯前端小项，可合批） | 都只改 `Main.gd`/`Board3D.gd` |
| **5** | **G2-I**（v3.7.0 文档与版本）→ 阶段二整支终审 → **唯一一轮修复波** | 阶段二收口 |
| **6** | **G3：P1 → P2 → P3 → P4 → P5 → P6 → P7** | 依赖顺序：线 → 波动 → 事件表 → 淡化 → 区块 → 聊天写入 → 文档 |
| **7** | **G4：Q1 → Q2 → Q3 → Q4 → Q5 → Q6 → Q7 → Q8** | 存储 → 融合 → 注入 → 采集 → 约战 → 面板 → 报告 → 文档 |

**每个阶段末统一收尾**（沿用阶段一/二流程）：全量门卫 + 联动冒烟 → 四份 spec 的「必须/不许/不得回退」逐条 grep 到代码或测试 → parked Minor 汇总表交用户分诊 → **唯一一轮修复波** + 定向复审 → 合入决策问用户。

---

## 6 · 验收门卫与基线

| 门卫 | 命令 | 当前基线（HEAD `90debd6`） |
| --- | --- | --- |
| 后端全量 | `cd backend && python -m unittest discover -s tests` | **483 OK**（2026-10-18 实跑确认） |
| KataGo 服务 | `cd katago_service && python -m unittest discover -s tests` | **36 OK**（2026-10-18 实跑确认） |
| 前端：定式卡片 | `godot --path game --headless --script res://tests/test_joseki_card.gd` | 51 PASS |
| 前端：复盘 | `… res://tests/test_review_mode.gd` | 31 PASS |
| 前端：配置 | `… res://tests/test_config.gd` | 24 PASS |
| 前端：TTS | `… res://tests/test_tts.gd` | 20 PASS |
| 前端：音画配对 | `… res://tests/test_speech_pairing.gd` | 77 PASS |
| 启动无脚本错误 | `godot --path game --headless --quit` | 无 `SCRIPT ERROR` |
| 联动冒烟 | 起 `katago_service`（真引擎）→ 起后端 → `GET /api/health` → `POST /api/move` 两手 → `POST /api/reset`；前端 headless 连 WS | 阶段一已跑通 |

**新增门卫归属**：`test_joseki_gate.py`（G1）、`test_view_helpers.gd` + `test_score_phrase.gd`（G2-G/H）、`test_pv_lines.py`/`test_event_log.py`/`test_decay.py`（G3）、`test_profile_store.py`/`test_profile.py`/`test_time_ctx.py`（G4）。

---

## 7 · 已裁决事项（2026-10-18 用户确认）

### 7.1 · D1 引擎深度与部署前提 → **改为「按调用可调」，不做全局加深**

**事实依据**

| 事实 | 证据 |
| --- | --- |
| 代码默认 `max_visits = 500`，**部署值 100** | `backend/app/config.py:19`；`game/config.json` = `100`（生效的是 100） |
| `maxVisits` 逐次请求下发，服务端可被请求覆盖 | `backend/app/engine.py:159`；`katago_service/proc.py:39`、`server.py:108` 只是默认 |
| `analysisPVLen` 在本仓**零引用** → 是引擎侧 cfg 键 | 全仓 grep 无命中（本机 `D:\下载\KataGo\analysis_example.cfg`） |
| **稳态每回合 2 次引擎分析** | `_prepare_after_player` 的 `analyze(:138)`（通常命中 `pre_analysis` 缓存）+ `analyze(:156)`；`_prepare_just_moved` 的 `analyze(:346)`，`:357` 缓存给下一手 → 稳态 2 次/回合 |
| 三线数据源＝`:156` 那一次的 `moveInfos` | `agent_graph.py:163` `after.get("moveInfos")` |

**裁决**：计划的「那一次已有分析变慢」在**全局提 `max_visits`** 的读法下不成立（`:346` 的 `post` 也会变慢，每回合 ≈10s → ≈20s，逼近 `katago_timeout_ms=30000` 并触发重试）。

**落地动作（P1/P2 必须照此实现）**

1. `engine.py`：`analyze(self, moves, max_visits=None)`，`req["maxVisits"] = int(max_visits or self.max_visits)`——**默认语义不变**。
2. 新增配置 `pv_max_visits`（**默认 150**）；**只在 `agent_graph.py:156`** 传；`:346`/`:394`/`:415`/`:481` 与 `tools.py` 一律不传。
3. `katago_timeout_ms` 建议默认提至 **45000**；README/AGENTS 写明「三线是**单次分析更深**的代价，不是多一次调用」。
4. `analysisPVLen` 写成**部署护栏**（防引擎默认值漂移），不得写成「必须」而误导——`pv` 长度的真实决定因素是 visits。
5. P1 门卫必含**降级用例**：`pv` 不足 → 只出 A（或 A/B），不抛不报错。

**验收判据**：`pv_max_visits=150` 下打印 `extract_lines()` 的三线长度与 `drop`（临时目录脚本，不入库）；稳态回合不触发 `katago_timeout_ms` 重试。

### 7.2 · D2 O9 与阶段四 `profile.db` → **不冲突；G1 严格只用局内信号**

**事实依据**

| 事实 | 证据 |
| --- | --- |
| O9 的三个信号今天全在局内现成 | `player_rank`：`agent_graph.py:149-153`；亏损率：`st.losses`（`state.py:112`）；族计数：`joseki_progress.seen`（`agent.py:837`） |
| 「玩家 rank」＝她前 5 候选里的名次，**不是段位** | `agent_graph.py:121` `(not rank_ok) or (drop > 0.10)` |
| **阶段四四维喂不出 `skill_adj`** | spec §6.2 只有 `open.*` / `style.*` / `blunder.*|weak.*` / `persona.*`，**无「定式族熟悉度」** |

**裁决**：两者是不同尺度的东西（局内棋力 vs 跨局棋风），**不是冲突**。G1 按 O9 只用局内信号。

**落地动作**

1. `should_show(*, rng, rate, skill_adj, mood_adj, ...)` 中 `skill_adj` 保持**入参**，由调用方算（将来的扩展点是调用点一行，不是门控重写）。
2. AGENTS.md 补硬规则：**门控的棋力浮动只读局内信号，不读任何持久化画像**（对应门卫 `test_missing_profile_or_mood_degrades`）。
3. 「让 `skill_adj` 读 `profile.db`」列入**未来候选**，前置条件：`profile.db` 先加 `joseki.familiarity.<family>` 维（v3.9.0 之后，不在本次范围）。
4. 解耦纪律：`memory.db` / `profile.db` / 门控三者互不依赖，任何一方故障不牵连另两方。

### 7.3 · D3 G1 版本落点 → **并入 v3.7.0，先 F 后 G1**

**事实依据**

| 事实 | 证据 |
| --- | --- |
| 阶段一按**旧规则**交付 | `586881d`；`agent_graph.py:229` 注释仍是「前 3 次…第 4 次起」 |
| PR **#2 正文把旧规则写成已交付特性** | `.superpowers/PR-v3.6.0-v3.7.0.md`（同一正文也列了 E/F/G/H/I 未完成） |
| v3.7.0 **尚未发布**、README 仍是 `### v3.6.0（当前）` | `README.md:716`；阶段二 ledger「剩余工作」 |
| 版本号只由各阶段「文档与版本」任务写一次 | ledger V12 |

**裁决**：并入 v3.7.0（不单独发 v3.6.1，也不保留旧规则）。

**落地动作**

1. **顺序**：批次 2 先做 F（设置抽屉），再做 G1 的 UI 一格 + G1 后端——两者都改 `Main.gd`（`_chk_joseki` :373、`_acc_check` :385–392），**合批一次前端评审**。
2. README v3.7.0 段写两件事：①「定式导学」升级为「开关 + 频率」，未命中只点评；②**明写这是对 v3.6.0 口径的修订**；v3.6.0 段内容不动，只加一句交叉引用（「※ 本条已在 v3.7.0 修订为提示门控」）。
3. PR #2：G1 落地后**追加一条 PR 评论**说明「阶段一的『前 3 次』规则已由提示门控取代」。
4. AGENTS.md 补：`未命中 == 未识别（逐字段同构）`；`门控只改文本与卡片，不改棋子 / speech 配对 / busy`。

---

## 7.4 · 其余风险（未变）

| # | 事项 | 说明 / 建议 |
| --- | --- | --- |
| R4 | **已知残留（按 ledger 停车，等分诊）** | X1 `R-1`（`agent_graph.py:239` 起的三处 `formed` 耦合，Low，建议在批次 3 顺手修）、X2 `R-2`、X3 `R-3`、X5 目外/高目未做 |
| R5 | **协作与远端** | 阶段一末尾曾有第二个会话并发写同一工作树 → 每次动手前确认 HEAD/干净度；远端已推 `feat/joseki-teaching-and-ux-batch`（PR **#2**，base `main`），本地 `main` 领先 `localrepo/main` 28 个提交且未推；`origin/main` 是另一条停靠 v3.3.0 的分岔线，**不要拿它做基线** |
| R6 | **文档漂移** | `docs/superpowers/` 目前落后于 `new_plan/`（缺两份 spec、两份文件是旧版）→ 由批次 0 修正 |

---

## 8 · 增量核对矩阵

| 增量项 | 计划出处 | 是否已实现 | 证据 |
| --- | --- | --- | --- |
| 定式门控（资格层 + 频率层 + 冷却 + 走错优先） | master plan「核心机制」+ Task 5（新版） | ❌ | `joseki_gate.py` 不存在；`joseki_rate` 零引用；`agent.py:837 joseki_show_card` 为旧「前 3 次」规则 |
| 定式 spec §6.1 / 决策表口径 | 定式 spec diff | ❌ | `docs/superpowers/specs/…joseki…` 仍是旧口径 |
| 阶段二 E | master plan Task E | ❌ | 无 `epoch` / `WATCHDOG_SEC` |
| 阶段二 F/G/H | master plan Task F/G/H | ❌ | 无抽屉；无 `reset_view`/`look_at_corner`/`tip_text`；无 `score_phrase` |
| 阶段二 I | master plan Task I | ❌ | `README.md` 版本号仍 v3.6.0 |
| 阶段三 P1–P7 | master plan 阶段三 + 新 spec | ❌ | 7 个新模块 / 5 个新测试文件均不存在；`emotion_drift`/`chat_digest_llm` 零引用 |
| 阶段四 Q1–Q8 | master plan 阶段四 + 新 spec | ❌ | 同上；`profile_enabled`/`time_aware_enabled` 零引用；`GET /api/profile` 不存在 |
| 阶段一 12 个任务（除门控） | 旧计划 | ✅ | `1f412d1..6465966`；ledger `07-…/progress.md` |
| 阶段二 A–D | 旧计划 | ✅ | `f4951de`/`ed0a45d`/`4eba17c`/`fa03f4b`；ledger `08-…/progress.md` |
