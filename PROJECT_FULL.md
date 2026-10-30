# Azure · 全量项目文档（v4.3.2）

> 本文件是**完整项目地图**：系统架构、人格化 / 日程 / 记忆三条主通路、代码量审计结果、
> 每个第一方文件的作用、配置与测试入口、维护约定。
> 快速上手看 [README.md](README.md)；深度实现细节看 [TECHNICAL.md](TECHNICAL.md)。

---

## 1. 系统总览

Azure 是「桌宠为主程序、围棋为独立窗口」的本地优先桌面 AI 助手。同一套人格、情绪、
时间线、记忆与日程在桌宠、围棋窗口、后端重启之间保持连续。

```text
Godot 客户端（桌宠 / 围棋）
   │  HTTP + WebSocket（/api/*、/ws）
   ▼
FastAPI 后端（backend/app，端口 8000）
   ├── KatagoEngine ────── HTTP ──────> KataGo 云服务（katago_service，端口 8100）
   ├── LLMClient ───────── OpenAI 兼容 ─> 本地 llama-server（端口 8080）或云端 LLM
   ├── MemoryService ───── llama.cpp ───> qwen3-Embedding.gguf（后端进程内，CPU）
   ├── TTS ─────────────── HTTP ───────> CosyVoice（端口 9880）/ 阿里云 / 系统语音
   └── 资料箱 / 窥屏 ────── Python ─────> PaddleOCR + Qwen2.5-VL（peek/、端口 8090）
```

### 1.1 运行进程

| 进程 | 启动方 | 作用 |
| --- | --- | --- |
| Godot 桌宠 / 围棋窗口 | `game/Launcher.tscn` | UI、棋盘、拖动、聊天面板、TTS 播放 |
| FastAPI 后端 | `dev_local.ps1` / `backend/run_server.bat` | 编排、LLM、记忆、日程、WS 事件 |
| llama-server | `tools/start_llm_server.py` | 本地大模型（Qwen3.6 GGUF） |
| KataGo 服务 | `dev_local.ps1` / `katago_service/run_server.bat` | 常驻分析、候选点、胜率 |
| CosyVoice | `dev_local.ps1` / `server/start_tts_server.bat` | 本地语音合成 |
| VLM / OCR | `peek/start_vlm_server.py` 或外部服务 | 窥屏与资料箱识别 |
| 本地 embedding | **后端进程内**（`llama_cpp`） | 长期记忆向量；默认 CPU，不抢主 LLM 显存 |

### 1.2 数据文件

| 文件 | 内容 |
| --- | --- |
| `backend/data/life.db` | 慢变量、情绪快照、生命事件、日程 `schedule_items`、类别耗时 `schedule_stats` |
| `backend/data/memory.db` | `chat_history`、`episodes`、`memories`、`vectors`、`games` 归档 |
| `backend/data/profile.db` | 棋风画像、每局记录、persona 表（关系 / 情绪余温 / 互动数） |
| `backend/data/games.db` | 棋谱库、逐手、总结、注记 |
| `backend/data/joseki_progress.db` | 定式族出现计数 |

以上均在 `.gitignore` 内，**不入库、不推送**。

---

## 2. 人格化逻辑通路

这是「情绪 → 记忆 → 语言 → 表现」的完整闭环，按时间顺序如下：

1. **事件进入情绪**
   - `agent._mood_event(ev, reason)` 查 `emotion.EVENT_DELTAS` 立即结算三维情绪
     （愉悦度 `v` / 好奇度 `c` / 唤醒度 `a`），20 秒粘性窗口防止下一帧弹回。
   - 事件来源：落子、好手 / 俗手、聊天、摸头、走神、窥屏、悔棋、局末、提醒等。
   - 慢变量（精力 / 无聊 / 亲密 / 信任 / 掌控感）由 `life.py` 持久化，
     `agent._life_fields()` 读取；启动时按真实流逝时间结转。
2. **情绪小调用（可选）**
   - `agent._mood_llm_refine()` 在后台小调用里让模型给三维增量；
     v4.2.8 起 6 秒超时、可被下一句聊天取消，绝不占用聊天生成槽位。
3. **记忆写入**
   - 聊天 / 事件 / 日程 / 对局总结写入 `memory_service`。
   - `memory.py` 把三维情绪压成**二维情绪矩阵**（`emo_v` × `emo_a`）与九格标签
     （低落 / 不安 / 焦躁 / 淡然 / 平静 / 振奋 / 惬意 / 开心 / 兴奋）。
   - 向量由 `memory_service.EmbedProvider` 计算，带模型指纹与维度写入 `vectors`。
4. **提示词装配**
   - `agent._sys_msg()` / `_phase_messages()` → `prompt_chains.build_messages()`：
     人设 + `mood_note` + `persona_extra` + 长度约束。
   - `persona_extra` 依次是：关系阶段 / 最近胜负 / 夜間节奏 / 今天的小心思 /
     情绪语言节奏（`persona.emotion_language_note`）/ 慢变量语气（`agent.life_note`）。
   - **基线附近情绪语言节奏为空**，提示词逐字符不变；明显偏离时只给句长、问句、
     快慢的软约束，并保留「仍是 Azure、克制不卖萌」的人设边界。
5. **语言参数**
   - `agent.chat_budget()` → 340 / 400 / 440 / 460 tokens；由 `agent_graph._prepare_chat`
     放进 `data["chat_budget"]`，`_speak_chat` 使用。
   - 不直接改人设文本，只改长度与节奏；棋评仍走全局 `llm_think_mode`。
6. **记忆召回**
   - `agent._long_term()` → `MemoryService.recall_for(query, mood=当前情绪)`。
   - 排序 = 语义余弦 + 情绪相似度 × 动态权重（0.25 → 0.40，随偏离基线放大）；
     加性偏置保证语义差距 > 0.40 时语义仍主导。
   - `memory_min_score=0.45` 过滤语义无关向量；关键词查询过滤“玩家 / 最近”等通用词，
     多词查询至少命中两个不同词，避免每句话都翻出全部旧事。
7. **主动回忆链**
   - 走神：`agent.idle_memory_hint()`，概率门 `idle_recall_chance()`；
   - 聊天：`agent.chat_recall_hint()`，只有明确「上次 / 还记得 / 之前」才开链；
   - 链状态在 `recall_chain.py`：1~3 步、20 分钟窗口、10 分钟链间冷却、
     聊天步间 90 秒、情绪跳变 / 换话题 / 到上限即收束；
   - 提示词里只是「此刻想起的旧事 · 可以提，也可以不提」，不强制提。
8. **表现层**
   - `message` 事件带文本；`speech` 事件带 `pitch`（当前情绪区域音高）；
   - 前端 `Main.gd` / `PetShell.gd` 播放 TTS 或预制语气音，`PetView.gd` 显示聊天气泡；
   - 头像 morphs / burst 由客户端按 `mood` 事件渲染。

---

## 3. 日程逻辑通路

### 3.1 资料箱 → 课表 / 日程

1. `main._scan_files` → `file_service` 扫描资料箱；
2. 图片 / PDF 走 `peek/azure_files.py`：PaddleOCR + VLM；课表由 OCR 坐标还原；
3. `schedule.normalize_items`：
   - LLM 只负责抽取；**绝对日期由 Python 规则落地**：
     `relative_date`（明天 / 下周三 / M月D日）优先，LLM 的 `date` 只作兜底；
   - `day_part`（早上 / 中午 / 下午 / 晚上）补正「8:00 → 08:00 或 20:00」；
   - 课程保留星期 / 节次 / 教师 / 地点；同日期 + 时刻 + 标题去重；
4. `LifeStore.upsert_schedule` 写 `life.db`；
5. `auto_plan` 按优先级、EWMA 类别时长、James-Stein 收缩、机会约束、缓冲与锁定窗排班；
6. 每条例程写一条 `tag='schedule'` 长期记忆（meta 带 `schedule_id / title / start_ts`）。

### 3.2 聊天 → 日程 / 取消

1. `/api/chat` 回复完成后，`main._capture_chat_schedule` 起后台任务；
2. 先跑 `schedule_chat.extract_cancellations`（规则 + 必要时 LLM）：
   - 课程等固定项不取消；疑问句不取消；规则命中不调 LLM；
3. 取消：`LifeStore.set_schedule_status(..., "cancelled")`；
   `MemoryService.supersede_schedule_item` 让旧日程记忆失效，
   并写一条 `schedule:cancelled` 审计记忆；
4. 新增：`schedule_chat.extract` → `schedule.normalize_items` → `LifeStore.upsert_schedule`
   → 按需 `auto_plan` → 写 memory → `ai_schedule` 只作面板刷新信号，不单独播报；
5. 同一标题 + 同一天的新日程会让旧记忆 `supersede`（不删除，保留审计）。

### 3.3 聊天时的日程事实

1. `agent_graph._prepare_chat` 调 `schedule_facts.select(rows, 用户原话)`：
   - `time_ref` 认今天 / 明天 / 后天 / 周X / 下周X / M月D日 / 时段 / 整点（含中文数字）；
   - 有明确时间窗就只给该窗口条目；窗口为空明确给「没有安排」；
   - 认不出才退回未来 48 小时；
2. 注入 `prompts.life_context_block` 的【近期真实日程】段；
3. 出话后 `schedule_facts.conflict(reply, rows)`：
   - 只对「不用 / 没有 / 没事 / 有空」等否定式日程判断触发；
   - 「没有课」只与课程类条目比较，避免把晚上的电影误判成课；
   - 冲突时给模型一次按事实重说的机会，仍失败用确定性事实句收口。

---

## 4. 记忆逻辑通路

### 4.1 写入

| 来源 | kind / tag | 情绪 |
| --- | --- | --- |
| 聊天 | `chat` / `chat:<mode>` | 当前三维情绪 → 二维矩阵 + 标签 |
| 走神 / 窥屏 | `event` | 当前情绪 |
| 日程 | `fact` / `schedule` | 无（日程事实不伪造情绪） |
| 取消日程 | `fact` / `schedule:cancelled` | 无 |
| 对局总结 | `game` | 无 |

- 单条上限 200 字，同 tag + 同 text 去重；重复写入复用同一行并更新 meta / 情绪标签。
- episode：6 轮 / 620 字 / 15 分钟空档 / 效价跳变 0.30 自动收束成摘要。

### 4.2 向量与召回

- 本地 `qwen3-Embedding.gguf`（1024 维）默认 CPU 编码；L2 归一化、LRU 缓存、批量编码。
- `EmbedProvider` 三态：`local` / `http` / `off`；本地失败可降级 HTTP，仍失败退回关键词。
- `vectors` 表存 `model` 指纹 + `dim`；召回只比较同模型、同维度的向量。
- 启动后台 `backfill_vectors()` 补齐当前模型的向量；没有向量时关键词召回。
- 排序：`语义分 + 情绪相似 × w`；`w = 0.25 + 0.20 × 平均偏离`，上限 0.40。
- 门槛：`memory_min_score=0.45`（向量）；关键词查询过滤通用词并要求多词命中。

### 4.3 supersede（事实失效）

- `status='active' / 'superseded'`；失效不删除，保留审计。
- 同一天 + 同标题新日程 → 旧日程记忆失效；
- 日程取消 → 按 `schedule_id` 失效；旧库无 meta 时按标题 + 时刻文本兜底；
- 重新写回同一条事实 → 自动重新激活，不会永远停在失效状态。

---

## 5. 代码量与结构审计（v4.3.2）

### 5.1 体量最大的文件

| 行数 | 文件 | 说明 |
| --- | --- | --- |
| ~5892 | `game/scripts/ui/Main.gd` | 围棋窗口 UI / 设置 / 服务状态 / 窥屏 / TTS 播放 |
| ~3061 | `backend/app/agent.py` | 公开动作、情绪、记忆、工具、看板、TTS 桥接 |
| ~2468 | `backend/tests/test_agent.py` | Agent 全链路回归 |
| ~2229 | `game/scripts/app/PetView.gd` | 桌宠视图、聊天面板、月历 / 周表 |
| ~1630 | `backend/app/agent_graph.py` | LangGraph 六节点编排与全部提示词装配 |
| ~1532 | `backend/app/main.py` | FastAPI 生命周期 / REST / WS / 日程后台任务 |
| ~1198 | `backend/app/life.py` | 生命时间线 / 慢变量 / 日程持久化 |
| ~1051 | `game/scripts/app/PetShell.gd` | 桌宠外壳、服务拉起、聊天 / 窥屏 / 情绪同步 |

### 5.2 已做的收敛

- v4.3.2 删除了情绪语言提示里与 `life_note` 重复的「精力不足」段（一处提示只由
  一个模块负责），并同步简化 `persona.emotion_language_note` 参数。
- 记忆关键词检索增加查询侧停用词与多词命中门槛，减少无效召回，减少注入长度。
- 重复记忆写入合并为一条 UPDATE 路径，减少两次写库与 meta 漂移。
- 日程提取把「日期算术」从 LLM 移到规则层，减少重试与错误纠正成本。

### 5.3 观察到但**未做**的大重构

- `Main.gd` 与 `PetShell.gd` 有约 10 个同名服务生命周期函数（VLM 拉起、窥屏 tick、
  TTS 状态、服务构建等）。可抽成共享 `ServiceTasks.gd`，但两者状态机与 UI 回调不同，
  属高风险 UI 重构；本版只审计、不合并，避免回归。
- `agent.py` 的公开动作与内部工具方法很长，但边界已被测试锁定；拆文件收益低、风险高。
- `server/` 是 CosyVoice 上游代码，保持原样，不参与本项目的代码量优化。

原则：**先保证行为与测试稳定，再做可验证的收敛**；不为行数牺牲可读性与回归安全。

---

## 6. 文件清单（第一方）

### 6.1 根目录

| 文件 | 作用 |
| --- | --- |
| `README.md` | 产品说明、快速开始、配置表、测试与发布 |
| `TECHNICAL.md` | 深度技术文档、模块契约、测试与设计边界 |
| `PROJECT_FULL.md` | 本文件：全量项目地图与逐文件说明 |
| `LICENSE` | Apache License 2.0 |
| `AGENTS.md` | 给编码 Agent 的项目约定与工作方式 |
| `dev_local.ps1` | 一键拉起 LLM / TTS / KataGo / 后端 / 前端 |
| `run_test_env.bat` | 后端 + KataGo + 前端全量测试入口 |
| `Azure.vrm` | Azure 人物模型（已入库） |
| `assistant_files/README.md` | 资料箱使用说明 |
| `assistant_files/inbox/.gitkeep` | 资料箱空目录占位；实际文件被 gitignore |
| `game/config.json` | 唯一配置模板（前端 + 后端共用） |
| `game/project.godot` | Godot 项目配置 |
| `game/export_presets.cfg` | Windows 导出预设 |
| `game/scenes/Launcher.tscn` | 启动场景（桌宠 / 围棋分流） |
| `game/scenes/Main.tscn` | 围棋主窗口场景 |

### 6.2 `backend/app/` 逐文件

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 包标记与版本元信息 |
| `actions.py` | 模型输出自然化：从 JSON / 日程结构转回人话 |
| `adaptive.py` | 1~5 档自适应难度、局内冷却与基线约束 |
| `agent.py` | `AzureAgent`：公开动作、情绪结算、记忆、工具、TTS 桥、看门狗、消息窗口 |
| `agent_graph.py` | LangGraph 六节点编排：prepare → decide → tools → decide → speak → finalize |
| `auto_plan.py` | 自动排班：优先级、James-Stein、EWMA、机会约束、缓冲、锁定窗、最小扰动 |
| `board.py` | 围棋棋盘、落子 / 提子 / 气 / 复制 / 文本棋盘 |
| `config.py` | 后端配置读取与夹取（LLM、embedding、记忆、日程、TTS、提示词） |
| `coords.py` | GTP 坐标 ↔ x/y、列字母、方位词 |
| `decay.py` | 事件衰减、坏手短语、档位与方位表达 |
| `emotion.py` | 三维情绪模型、27 区表情 / filler / pitch、事件增量 |
| `engine.py` | KataGo analysis HTTP 客户端（超时、错误、候选 / ownership） |
| `estimate.py` | 点目与形势估算 |
| `event_log.py` | 局内事件表（好手 / 坏手 / 偏航 / 定式），供情绪与提示词 |
| `events.py` | WebSocket 事件构造与录制总线 |
| `file_service.py` | 资料箱扫描、VLM 拉起、文件列表编排 |
| `full_review.py` | 全盘复盘：分段、逐段讲解、语音出口 |
| `game_store.py` | 棋谱库：归档、查询、导入导出、逐手 / 总结 / 注记 |
| `joseki.py` | 定式识别、分支、族命名与讲解素材 |
| `joseki_gate.py` | 定式提示门控：技能 / 情绪 / 冷却 / 概率 |
| `joseki_progress.py` | 定式族出现计数与复习优先级 |
| `life.py` | 生命时间线：慢变量、情绪快照、事件、日程、类别耗时、统计 |
| `llm.py` | `LLMClient`：LangChain 链、按调用思考档 / `no_think`、超时、失败映射 |
| `llm_chain.py` | ChatOpenAI 装配、`extra_body` 合并、流式、content / think 剥离 |
| `main.py` | FastAPI：生命周期、REST、WebSocket、聊天排程、提醒循环、记忆 API |
| `memory.py` | 长期记忆存储：聊天流水 / episode / 记忆 / 向量 / supersede / 回填 |
| `memory_semantics.py` | 记忆二维情绪矩阵、九格标签、情绪相似度、动态权重、变更线索 |
| `memory_service.py` | 记忆门面：`MemoryStore` + `EmbedProvider`（本地 GGUF / HTTP）+ 回填 |
| `persona.py` | 人格化纯逻辑：关系阶段、情绪余温、小目标、旧局回忆、情绪语言节奏、聊天预算 |
| `profile.py` | 棋风画像计算与文本化（布局 / 战斗 / 官子 / 薄弱方位） |
| `profile_store.py` | 画像库与 persona 表持久化 |
| `prompt_chains.py` | 七阶段提示链（ChatPromptTemplate + RunnableBranch） |
| `prompts.py` | 全部提示词：人设、上下文、长期记忆、回忆、日程、画像、时间、定式 |
| `pv_lines.py` | 预测线 A / B / C 提取与胜率差 |
| `recall_chain.py` | 主动回忆链：开始 / 延续 / 终止条件、话题与情绪门 |
| `schedule.py` | 日程 / 课表抽取、确定性日期归一化、去重、任务分类、记忆文本 / meta |
| `schedule_chat.py` | 聊天日程判断、取消规则 + LLM fallback、JSON 解析 |
| `schedule_facts.py` | 日程事实：时间指代、窗口选条、格式化、出话冲突核对 |
| `schedule_learning.py` | 类别 EWMA 时长、变更点、偏好、反馈与复盘 |
| `sgf.py` | SGF 导入 / 导出 |
| `speech_text.py` | `[[tone:...]]` 语气标记提取 / 剥离 |
| `state.py` | 对局状态、棋谱、记忆窗口、逐手注记、快照回滚 |
| `time_ctx.py` | 绝对 / 相对 / 昼夜时间事实 |
| `tools.py` | 发言侧工具定义（查盘 / 复算 / 定式 / 资料） |
| `tts.py` | TTS 后端：本地 / 云端 / 系统降级、缓存、批量 |
| `tts_aliyun.py` | 阿里云 Voice Studio WebSocket 流式合成 |

**backend 根文件**：

| 文件 | 作用 |
| --- | --- |
| `requirements.txt` | 后端依赖；`llama-cpp-python` 为可选本地 embedding 依赖 |
| `run_server.bat` | Windows 后端启动脚本 |
| `data/` | 运行产物（SQLite / WAL / 备份，gitignore） |
| `logs/` | 后端运行日志（gitignore） |

### 6.3 `backend/tests/`

| 文件 | 覆盖 |
| --- | --- |
| `helpers.py` | 假引擎 / 假 LLM / 固定随机源 / Agent 工厂 |
| `golden/*.json` | 棋盘 / 坐标 / 情绪 / 定式 / 提示词 / 状态黄金样本 |
| `test_actions.py` | 输出自然化 |
| `test_adaptive.py` | 自适应难度 |
| `test_agent.py` | Agent 全链路：聊天 / 情绪 / 记忆 / 提醒 / 工具 / 去重 / 日程冲突 |
| `test_agent_graph.py` | LangGraph 阶段、工具轮、看门狗、流式、去重 |
| `test_auto_plan.py` | 排班算法与触发闸门 |
| `test_board.py` / `test_coords.py` | 棋盘与坐标 |
| `test_config.py` | 配置默认值 / 夹取 / embedding 字段 |
| `test_decay.py` / `test_emotion.py` | 衰减与三维情绪 |
| `test_engine.py` | KataGo 客户端 |
| `test_estimate.py` / `test_pv_lines.py` | 点目 / 预测线 |
| `test_event_log.py` / `test_events.py` | 事件表与 WS 事件 |
| `test_full_review.py` / `test_full_review_api.py` | 全盘复盘与 API |
| `test_game_store.py` / `test_games_api.py` / `test_sgf.py` | 棋谱库、API、SGF |
| `test_joseki*.py` | 定式识别 / 提取 / 门控 / 族计数 |
| `test_life.py` | 生命时间线与日程持久化 |
| `test_llm.py` / `test_llm_chain.py` | LLM 客户端、思考档、流式、降级 |
| `test_main.py` | FastAPI 端点 / 生命周期 / 运行期配置 / 记忆 API |
| `test_memory.py` / `test_memory_semantics.py` | 记忆存储、向量指纹、回填、阈值、情绪矩阵、supersede |
| `test_mood_delta.py` | 情绪小调用解析与限频 |
| `test_persona.py` / `test_profile*.py` | 人格化、画像、画像库 |
| `test_prompt_chains.py` / `test_prompts.py` / `test_prompts_long_term.py` | 提示链、黄金提示词、注入段 |
| `test_recall_chain.py` | 回忆链开始 / 延续 / 终止 |
| `test_schedule*.py` | 日程抽取、归一化、聊天排程、取消、API、学习 |
| `test_speech_text.py` | 语气标记 |
| `test_state.py` | 对局状态 |
| `test_time_ctx.py` | 时间事实 |
| `test_timetable.py` | 课表几何还原 |
| `test_tools.py` | 发言侧工具 |
| `test_tts*.py` | TTS 文本 / 缓存 / 阿里云 / 链路 |
| `test_agent_graph.py` / `test_agent.py` 中的 v4.3.x 用例 | 回忆链、日程事实、动态情绪权重、embedding |

### 6.4 `game/scripts/`

| 文件 | 作用 |
| --- | --- |
| `app/Launcher.gd` | 启动分流：桌宠 / 围棋 / 命令行覆盖 |
| `app/PetShell.gd` | 桌宠外壳：服务拉起、窗口拖动、聊天 / 窥屏 / 情绪 / TTS 状态 |
| `app/PetView.gd` | 桌宠视图：圆形头像、聊天气泡、思考档、资料 / 月历 / 周表 |
| `core/AzureConfig.gd` | 前端配置读写（保留未知后端键，`_raw` 原样写回） |
| `core/Coords.gd` | 前端坐标换算 |
| `core/GameReplay.gd` | 棋谱重放 |
| `net/ChatQueue.gd` | busy 时聊天排队 |
| `net/NetClient.gd` | REST / WS 客户端、设置推送、聊天 / 记忆 / 日程接口 |
| `services/SfxPlayer.gd` | 落子 / 提子 / 终局音效 |
| `services/SpeechPairing.gd` | `turn_id + seq` 语音与气泡配对 |
| `services/TTSClient.gd` | 前端 TTS 播放（系统 / 后端音频） |
| `ui/Ambience.gd` | 背景氛围 |
| `ui/AzureAvatar.gd` | VRM 头像、表情 morph、burst |
| `ui/Board3D.gd` | 3D 棋盘、落子、悬停、坐标 |
| `ui/GameLibrary.gd` | 棋谱库面板 |
| `ui/GameReport.gd` | 局末报告 / 复盘报告 |
| `ui/GufengTheme.gd` | 古风主题色 / 字体 / 控件样式 |
| `ui/JosekiCard.gd` | 定式教学卡片 |
| `ui/Main.gd` | 围棋主窗口：对局、聊天、设置、资料、日程、服务状态 |
| `ui/MiniBoard.gd` | 小棋盘预览 / 复盘缩略 |
| `ui/PetMode.gd` | 桌宠模式切换与独立窗口 |
| `ui/TitleScreen.gd` | 标题页 |
| `addons/azure_dev/plugin.gd` | 开发期 Godot 编辑器插件（一键自检 / 开发辅助） |

**资源与场景**：`game/assets/azure_pet.png`（桌宠贴图）、`AzureGo2.0.png`（标题图）、
`sfx/*.wav`（落子 / 提子 / 终局音效）、`voice/filler_*.wav`（情绪预制语气音）；
`game/scenes/Launcher.tscn` / `Main.tscn` 为启动与围棋主场景；`.import` / `.uid` 为 Godot
自动生成的资源索引，随源文件一起保留。

### 6.5 `game/tests/`

20 个门卫脚本，覆盖：配置、聊天队列、网络聊天、桌宠模式 / 外壳、
头像路径、棋盘估算、定式卡片 / 等级、新局、复盘、报告、点目短语、
音效、语音配对、思考台词、标题页、TTS、视图 helper。

具体清单：`test_avatar_path.gd`、`test_chat_queue.gd`、`test_config.gd`、
`test_estimate.gd`、`test_joseki_card.gd`、`test_joseki_level.gd`、`test_net_chat.gd`、
`test_new_game.gd`、`test_pet_mode.gd`、`test_pet_shell.gd`、`test_replay.gd`、
`test_report.gd`、`test_review_mode.gd`、`test_score_phrase.gd`、`test_sfx.gd`、
`test_speech_pairing.gd`、`test_thinking_lines.gd`、`test_title_screen.gd`、
`test_tts.gd`、`test_view_helpers.gd`。
`test_pet_shell.gd` 也覆盖圆形遮罩、四档思考、气泡去重、月历 / 周表。

### 6.6 `katago_service/`

| 文件 | 作用 |
| --- | --- |
| `server.py` | 常驻 KataGo 分析 HTTP 服务（端口 8100） |
| `proc.py` | KataGo 子进程生命周期、健康检查、日志 |
| `run_server.bat` | Windows 启动脚本 |
| `requirements.txt` | 服务依赖 |
| `tests/` | 进程与服务测试 |

### 6.7 `peek/`

| 文件 | 作用 |
| --- | --- |
| `azure_files.py` | 资料箱图片 / PDF：OCR、VLM、课表几何还原 |
| `azure_peek.py` | 窥屏：截图、隐私过滤、OCR / VLM 描述 |
| `start_vlm_server.py` | 本地 llama-server VLM 拉起（Qwen2.5-VL） |

### 6.8 `tools/`

| 文件 | 作用 |
| --- | --- |
| `start_llm_server.py` | 本地 LLM llama-server 拉起 / 锁 / 自愈重试 |
| `joseki_extract.py` | 定式库提取工具 |
| `run_hidden_dev_local.vbs` | 无窗口启动开发服务 |

### 6.9 `docs/`

`docs/plans/` 与 `docs/superpowers/` 保存历史设计与实施计划（远程化、KataGo 云服务、
LangChain / LangGraph、长期记忆、TTS、聊天队列、定式 / 画像 / 事件 / UX）。
这些是**历史设计记录**，不是当前行为契约；当前契约以 README / TECHNICAL / 测试为准。

### 6.10 `server/`（CosyVoice，第三方）

这是 CosyVoice 上游代码与本地语音服务，**不作为第一方模块维护**：

| 子路径 | 作用 |
| --- | --- |
| `azure_tts_server.py` | Azure 本地 TTS HTTP 服务（`/v1/audio/speech`、健康检查、三级降级） |
| `cosyvoice/` | CosyVoice 模型 / 前端 / flow / hifigan / LLM / tokenizer |
| `runtime/` | FastAPI / gRPC / Triton / TensorRT-LLM 运行示例 |
| `third_party/` | Matcha-TTS、OpenVoice 等上游依赖 |
| `examples/` | 训练 / GRPO / 数据集示例 |
| `asset/`、`voices.json`、`sherpa_voices.json` | 音色与提示音频 |
| `start_tts_server*.bat`、`run_hidden.vbs` | Windows 启动 |
| `README.md`、`FAQ.md`、`LICENSE` | 上游说明与许可 |

---

## 7. 配置地图（关键字段）

| 组 | 字段 |
| --- | --- |
| 后端 / 服务 | `backend_url`、`auto_start_services`、`katago_url`、`katago_token`、`katago_timeout_ms` |
| LLM | `llm_base_url`、`llm_model`、`llm_api_key`、`llm_think_mode`、`llm_timeout_sec`、`pet_chat_think_level` |
| Embedding | `embed_mode`、`embed_local_model`、`embed_local_gpu_layers`、`embed_local_threads`、`embed_local_ctx`、`embed_batch_size`、`embed_cache_size`、`embed_backfill`、`embed_query_prefix`、`embed_base_url/model/api_key`、`memory_min_score` |
| 记忆 / 画像 | `memory_enabled`、`memory_top_k`、`memory_max_inject_chars`、`profile_enabled`、`time_aware_enabled` |
| 日程 | `auto_plan_enabled`、`auto_plan_days`、`schedule_chat_enabled`、`schedule_reminder_enabled`、`schedule_*` 算法参数 |
| 语音 | `tts_enabled`、`tts_mode`、`tts_local_*`、`tts_aliyun_*`、`pet_chat_tts_prebuilt` |
| 视觉 | `vlm_enabled`、`vlm_auto_start`、`vlm_base_url`、`vlm_port` |
| 人格化 | `persona_enabled`、`persona_relationship_enabled`、`persona_address_name`、`emotion_style` |
| 界面 | `pet_enabled`、`pet_height`、`ui_theme`、`reply_style` |

完整读取与夹取规则见 `backend/app/config.py` 与 `game/scripts/core/AzureConfig.gd`。

---

## 8. 测试与运行

```powershell
# 后端全量（当前 1163 tests OK）
cd backend
python -m unittest discover -s tests -q

# 前端全量（20 个脚本）
Get-ChildItem game\tests\test_*.gd | ForEach-Object {
  & 'D:\Godot\Godot_v4.6.3-stable_win64_console.exe' --headless --path game `
    --script ("res://tests/{0}.gd" -f $_.BaseName)
}

# 一键：后端 + KataGo + 前端
run_test_env.bat tests
```

前端门卫重点：

- `test_pet_shell.gd`：圆形遮罩、四档思考、气泡去重、迟到回复、预制音、月历 / 周表。
- `test_config.gd`：配置默认值 / 夹取 / 保存 / 设置推送。
- `test_net_chat.gd` / `test_chat_queue.gd`：WS 聊天、busy 排队。
- `test_speech_pairing.gd`：`turn_id + seq` 语音配对。
- `test_pet_mode.gd` / `test_avatar_path.gd`：桌宠切换与头像。

---

## 9. 维护约定 / 扩展点

1. **空则逐字符不变**：所有提示词注入段（记忆 / 事件 / 时间 / 画像 / 日程 / 回忆）
   为空时不得改变原有输出；黄金测试守着这条。
2. **LLM 只做语义，规则做算术**：日期、时长、权限、阈值、状态机由 Python 决定；
   LLM 输出不直接写库。
3. **失败开放，不失败关闭**：记忆 / 画像 / 日程 / embedding / TTS 任一故障都只降级，
   不得打挂聊天或对局；`busy` 必须有兜底归位。
4. **单槽位生成**：后台情绪 / 日程判断不得长时间占用 `_gen_lock`；超时 / 取消必须释放。
5. **隐私**：窥屏只给短描述，不念窗口标题 / 聊天内容；资料箱本地处理，不上传；
   `backend/data/`、`assistant_files/inbox/*`、`build/`、模型权重不推送。
6. **新增记忆字段**：先改 `memory.py` 的 `_SCHEMA` / `_COLUMNS`（旧库自动迁移），
   再改 `memory_service` / `memory_semantics`，最后补测试与文档。
7. **新增日程逻辑**：纯规则放 `schedule.py` / `schedule_facts.py`；LLM 判断放
   `schedule_chat.py`；写库 / 排班 / 记忆联动放 `main.py`，不要把三件事混在一个函数里。
8. **新增人格化提示**：只写「语气 / 节奏 / 分寸」，不写数值、不写状态名；
   基线附近返回空串，避免每句话都在演状态。

---

## 10. 数据、隐私与推送

- 不推送：`backend/data/`（SQLite / WAL / 备份）、`backend/logs/`、`assistant_files/inbox/*`、
  `build/`、`dist/`、模型权重（`*.gguf` 等）、`peek/*.log`、锁文件、`.env`。
- `game/config.json` 只含服务地址与参数模板，不含个人密钥 / 本地资料；
  实际个人配置放同目录 `config.json` 或环境变量 `AZURE_CONFIG`。
- 提交前用 `git status` 与 `git ls-files` 复核，确认没有个人图片 / 数据库 / 日志进入索引。
