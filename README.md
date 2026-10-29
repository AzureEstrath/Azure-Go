# Azure · 桌宠围棋助手

> **桌宠是主程序，围棋是它打开的独立窗口。**
> 同一套 Azure 人格、情绪、时间线与记忆，在桌宠、围棋窗口和重启后的下一次见面之间保持连续。

**版本：v4.2.6（Release）**

Azure 是一个本地优先的桌面 AI 助手：她可以待在桌面上陪你说话、看你在忙什么，也可以打开一盘围棋和你下棋。她不是“围棋程序里的桌宠模式”，而是一个有自己的时间、精力、心情与记忆的桌面角色。

---

## 目录

- [1. 核心特性](#1-核心特性)
- [2. 产品形态](#2-产品形态)
- [3. 架构](#3-架构)
- [4. 快速开始](#4-快速开始)
- [5. 操作方式](#5-操作方式)
- [6. 配置](#6-配置)
- [7. 情绪、时间与记忆](#7-情绪时间与记忆)
- [8. 数据与接口](#8-数据与接口)
- [9. 测试](#9-测试)
- [10. 导出与发布](#10-导出与发布)
- [11. 目录结构](#11-目录结构)
- [12. 已知边界](#12-已知边界)
- [13. 致谢与许可](#13-致谢与许可)

---

## 1. 核心特性

### 桌宠助手

- 完整半身 PNG 贴图，不做圆形裁切。
- 尺寸四档：**120 / 160 / 200 / 240 px**，右键菜单即时调整并持久化。
- 透明、无边框、置顶小窗；悬浮球式拖动，跟手并带轻微惯性。
- 右键菜单：聊天、围棋陪练、大小、退出桌宠。
- 聊天面板输入；Azure 的回复只进面板，不在桌面冒泡。
- 桌宠启动时会自动拉起本地服务链（LLM / TTS / KataGo / 后端）。

### 拟人化节奏

- 主动开口**不是固定定时器**：由基础分布、时段、心情、精力、无聊、亲密、信任、掌控感、连续未回应次数共同决定。
- 偶尔会有短追问，也偶尔会有长停顿；连着说话但没人接时会自然拉长间隔。
- **时不时主动回忆**：按情绪挑选回忆主题（开心想温暖的事、低落想被陪伴的事、好奇想知道你最近在忙什么），
  用本地向量 / 关键词联想召回一条具体旧事，再自然接进一句很短的话里；同一条 30 分钟内不重复，也不会编造。
  桌宠走神与围棋闲聊共用同一份回忆，只是各自按当前语气开口。
- 初次连接约 **11–18 秒**开口；稳定后通常约 **20–150 秒**一句，且始终保留“助手存在感”，不会长时间装死。
- 待机、走神、窥屏的气泡只显示文本，并播放对应情绪的**预制语气音**；不调用系统 TTS。

### 共享情绪与思维连续性

- 情绪使用三维 **愉悦度 / 好奇度 / 唤醒度**（现有 27 区表情映射）并补充 PAD 的**掌控感**。
- **双速衰减**：唤醒度约 150 秒回落；愉悦度 / 好奇度约 30 分钟回落。
- 每次情绪事件后有 **20 秒粘性窗口**，不会下一帧就弹回基线。
- 慢变量持久化：**精力、无聊、亲密、信任、掌控感**；聊天/摸头/下棋/走神都会留下痕迹。
- 情绪与慢变量跨进程、跨围棋窗口、跨重启恢复；提示词只拿到“此刻的身体与心绪”约束，不念数值。

### 共享记忆

- `chat_history`：桌宠与围棋共用的最近聊天原文流水。
- `episodes`：按 **6 轮 / 620 字 / 15 分钟空档 / 情绪效价跳变 0.30** 自动收束的聊天片段。
- `memories`：可召回的长期记忆（聊天摘要、事件、对局总结、手动写入）。
- `LifeTimeline`：只追加的生命事件与相对时间事实；桌宠启动时能知道“上一回见到你是什么时候”。
- 共享后端：桌宠、围棋窗口、重启后的 Azure 看到的是同一份情绪与记忆。

### 资料、日程与提醒（v4.2.0）

- **聊天里 Azure 自己判断要不要排（v4.2.0）**：聊天回复完成后，后端先用规则闸门过滤掉
  明显没有时间 / 事项线索的闲聊（不浪费模型调用），再由 LLM 判断「这句话是不是玩家自己的
  约定 / 任务 / 截止 / 提醒」，是则抽取、写入共享日程、按需自动排班并写入长期记忆。
  排完**不单独播报**（不插「日程记好了」这类生硬消息），聊天照常继续；`ai_schedule` 事件
  只作为数据刷新信号，资料面板开着时重拉日程，让新条目立刻出现。
  模型误把日程 JSON 当聊天回复时，也只回一句「嗯，记下了。」，不再逐条念日程。
  可用 `schedule_chat_enabled=false` 关闭，聊天行为与旧版一致。
- **年月历 → 周视图（v4.2.0）**：资料 / 日程面板默认显示**7×6 的月历**（年月标题、今天高亮、
  有课的日期直接显示课程 / 约定小条）；点某一天打开那一周，周视图可前后翻周、一键回本周 / 月历。
  基底课表只显示它所属的那一周（先只有本周，不再每周套同一张）；定时约定也只显示在所属周。
  月历上方可翻年 / 翻月，下方按「本月日程 / 待安排」列出条目。
  **v4.2.1 修复**：已经结束的比赛 / 约定仍留在本周周表（灰色「已结束」），不再出现
  「月历里有、周表里没有」；课程不再跨周重复。
  **v4.2.2 修复**：切月 / 切周后自动回到顶部；窄窗口周表不再出现横向滚动条；
  聊天历史里的 `[[tone:...]]` / `{动作}` 舞台标记会在显示前剥掉，旧记忆也不会再露出原始标记；
  新回复入库只存可读台词，语气标记不再被下一轮模型复读。
  **v4.2.3**：完成的任务不再从周表 / 月历删除，保留并标「已完成」；聊天默认 LLM 思考档位改为中等。
  **v4.2.4**：聊天排程不再弹生硬播报，只静默写库 / 刷新面板；日程 JSON 误输出只回一句「嗯，记下了」；
  清除聊天流水与聊天类长期记忆后重启（保留日程事实）。
  **v4.2.5**：聊天不再只能回「记下了」——可以自然接住你说的事，也可以按情绪 / 共同记忆换个话题；
  模型误吐日程 JSON 时先用快速档重答一次，仍失败才兜底；聊天回复超过 60 秒也不会丢，
  迟到的那句会直接出现在面板里；桌宠聊天默认只播预制情绪音（`pet_chat_tts_prebuilt=true`），
  CosyVoice 服务照常启动，聊天面板里可随时切回整句合成。
  **v4.2.6**：退出 / 重开桌宠不再清空聊天面板——后端历史只与当前会话合并，空响应绝不清空；
  默认思考档位改为快速（`llm_think_mode=0`），聊天出字更快；与课程同格的约定不再被周表忽略，
  改为在课程块底部画窄条，满课日的约定在月历日格也优先显示。
- **资料箱 → 周课表**：图片 / PDF / 文档经 OCR + VLM 读取；课程表按 OCR 坐标还原成
  7 天 × 12 节的周课表（星期、节次、教师、地点、日期），不再压成一串文字。
- **重扫不再重复 / 事件不再误判（v4.1.7）**：课程按「日期 + 节次 + 标题」去重，
  启动时自动清理历史重复并合并来源；群公告里的比赛 / 活动按普通约定处理，
  进行中的约定显示「已约定」，已结束的标为「已结束」且不会第二天再排一遍。
- **周表不再漏项（v4.1.5）**：窄窗口也优先完整显示 7 天；有明确时刻的约定
  （如周四 12:00 篮球赛）会画进对应节次行；日程 / 约定列表放在周表上方，课程块悬停仍可看完整信息。
  同一格有多个约定时合并为一个「N 项约定」块，悬停卡列出全部；
  与课程同格的约定不再跳过（v4.2.6），改为在课程块底部画一条窄条，既看得见也不遮住课程。
- **可解释排班（v4.1.6）**：悬停只保留一张自绘卡，不再出现 Godot 默认黑底白字 tooltip；
  卡片会给出「排班理由」（截止优先 / 优先级高 / 连续推迟后放大段空闲 / 风险缓冲）。
  连续推迟 ≥ 2 次的任务会优先放进至少两倍时长的连续空档。
- **聊天公告也能入日程（v4.1.4）**：课程表不再整表喂给 LLM（避免挤掉同批资料）；
  聊天 / 群公告里明确的「日期 + 时间 + 活动 / 地点」由 LLM 提取，并由规则补抽兜底；
  只有星期、没有节次的事件按普通约定处理，不会误判成课程。
- **紧凑响应式面板（v4.1.3）**：资料 / 日程窗口从 700×560 收到 544×520；
  窄窗口下操作按钮自动折成两列、说明文字让位，关闭 / 收起按钮始终在视口内；聊天面板也会随视口收窄。
- **日程自动分级（v4.1.2）**：每条日程按「能不能动」自动分成
  **固定课程 / 已约定 / 锁定中 / 截止驱动 / 可调整 / 待安排** 六级。
  课程与用户已定时间的约定不会被自动排班移动；只有待定任务与锁定窗外的旧自动安排参与重排。
  面板的日程行与悬停卡会显示分级标签。
- **自动排班**：待定任务按**优先级 + 截止时间 + 空闲区间**自动落位；时长 / 优先级先做
  **正部 James-Stein 收缩估计**（向历史均值收缩，`m < 3` 不收缩），再用优先级密度排序、
  best-fit 打包空闲段。**72 小时内到期**的任务优先走 EDF；更远的截止任务不抢占今天。
  固定课程与已定时间不被移动。
- **动态排程学习（v4.1.1）**：
  - 每类任务用 **EWMA** 维护真实耗时；样本少时向类别先验收缩，样本多时向类别均值收缩，
    再向全局均值收缩。连续两次偏差超过 2.5σ 视为**变更点**，重置该类别的时长先验。
  - **事件驱动滚动重排**：新待定 / 关键任务逾期 / 未来容量不足才触发；两次重排至少隔 15 分钟，
    只动未来 1~3 天；已自动安排且落在 12 小时锁定窗内的任务不动（最小扰动）。
  - **集中缓冲**：每天工作窗口末尾预留 45 分钟弹性块，而不是给每个任务加安全时间。
  - **机会约束**：按 `均值 + z·σ`（默认 z=0.84 ≈ 80% 完成概率）预留时长，上限 1.35 倍。
  - **偏好与推迟**：完成 / 推迟次数形成很轻的类别偏好分；点「推迟」后任务优先排到明天。
  - **真实日程手感（v4.1.2）**：普通任务对齐到整点 / 半点；课程与约定前后留 10 分钟
    通勤 / 切换时间；之前自动排过的任务原时间仍可行时优先不移动，减少无谓抖动。
- **执行反馈与复盘**：日程行可直接「完成 / 推迟」；完成后更新类别 EWMA，
  「复盘」按类别显示计划 / 实际耗时偏差与完成、推迟、跳过计数。
- **到点事务提醒**：后端每 15 秒检查一次，到点把事务喂给 LLM 生成一句短提醒；
  LLM 不可用时自动退回模板气泡。提醒走 `ai_remind`，**不发语音**；面板开着时显示在面板上方的提醒卡。
- **留档与清理**：过期日程可归档保留（不删除）；超过保留期（默认 30 天）的归档可一键清理；
  后台每小时自动留档 7 天前条目、每天自动清理过期归档。
- **课表格子悬停展开**：鼠标停在放不下的课程块上，浮层显示完整课程名 / 教师 / 地点 / 时间 / 备注。

### 围棋陪练

- 从桌宠打开独立围棋窗口（1280×800），桌宠最小化；关闭围棋窗口后桌宠恢复。
- **关闭围棋窗口不会清空棋局**，下次打开显示“继续对局”。
- KataGo 算棋，Azure 执白应手；大模型负责讲解、闲聊、复盘；TTS 合成在后端。
- 保留棋谱库 / 复盘 / 定式导学 / 情绪棋风 / 自适应难度 / 领地热力图 / 战报等既有功能。

### 窥屏

- 不提供手动入口，按心情与无聊度**不定时自动触发**。
- 从桌宠身后的前台窗口截图 → 本地 OCR → 本机 VLM 描述 → 后端 `/api/peer` 组织一句主动搭话。
- 缺 Python / VLM / 服务时自动退化为普通待机搭话。

### 资料箱 → 日程

- 把图片 / PDF / DOCX / XLSX / PPTX / TXT / Markdown / CSV / HTML 等资料放进 `assistant_files/inbox/`。
- 右键桌宠 → **资料与日程** → **开始处理**：
  - 图片与扫描件走本机 **OCR + VLM**；
  - 文档抽取文本；
  - 再交给 LLM 提取明确的日期 / 时间 / 约定 / 待办；
- 整理结果写入共享日程（`backend/data/life.db` 的 `schedule_items`）和长期记忆；
- 后续提示词的时间块会带上“下一条日程”，桌宠与围棋都能自然提到；
- 同一份资料重复处理按“来源 + 时间 + 标题”去重；资料原文不会回传给前端。

---

## 2. 产品形态

```text
启动
 └─ Launcher（主场景）
     ├─ 桌宠模式（默认）：PetShell + PetView
     │    ├─ 透明置顶小窗
     │    ├─ 右键聊天 / 调整大小 / 退出
     │    └─ 打开围棋 → 另起独立进程 --game --from-pet
     └─ 围棋模式：Main（独立窗口，1280×800）
```

- 项目主场景是 `game/scenes/Launcher.tscn`。
- 默认加载 `PetShell`；`--game` / `--no-pet` / 截图调试参数 / 无头模式加载围棋窗口。
- 围棋窗口由桌宠使用 `OS.create_process` 另起进程打开；关闭后由 `PetShell` 检测进程退出并恢复桌宠。
- 两个窗口共用 `backend` 的同一份 Agent 状态、情绪、时间线与记忆库。

---

## 3. 架构

```text
Godot 客户端（game/）
  Launcher.tscn
  ├─ PetShell.gd / PetView.gd     桌宠主程序：透明窗、聊天、TTS、窥屏排程、围棋进程
  └─ Main.tscn / Main.gd          独立围棋窗口：3D 棋盘、VRM、聊天、复盘、棋谱库
        │
        │ REST + WebSocket（127.0.0.1:8000）
        ▼
FastAPI 后端（backend/）
  ├─ agent.py / agent_graph.py    回合编排、情绪结算、聊天、走神、窥屏、总结
  ├─ emotion.py                   三维情绪 + 双速衰减 + 粘性
  ├─ life.py                      生命时间线、慢变量、跨重启恢复
  ├─ memory.py / memory_service.py 共享聊天流水、episode、长期记忆
  ├─ prompts.py / prompt_chains.py 时间块、画像块、生命状态、人格化提示词
  ├─ main.py                       REST / WebSocket / 生命周期
  └─ tts*.py                      系统 / 本地 / OpenAI 兼容 / 阿里云语音
        │
        ├─ KataGo 服务（katago_service，127.0.0.1:8100）
        ├─ 本地 LLM（llama.cpp 等，127.0.0.1:8080）
        ├─ 本地 TTS（CosyVoice / MeloTTS 等，127.0.0.1:9880）
        └─ 可选 VLM（peek/，127.0.0.1:8090）
```

**客户端只做渲染、输入与语音播放。** 对局编排、情绪、记忆、提示词与合成都在后端；LLM 与 KataGo 通过可配置的 OpenAI 兼容 / HTTP 接口接入。

---

## 4. 快速开始

### 4.1 环境要求

| 组件 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11（透明置顶窗口与逐像素透明） |
| Godot | 4.6.3（项目使用 GL Compatibility） |
| Python | 3.10+（后端与本地服务） |
| KataGo | `katago.exe` + 权重 + analysis 配置，或已部署的 KataGo 服务 |
| LLM | 任意 OpenAI 兼容服务；自用版默认本地 llama.cpp |
| TTS | 系统语音 / 本地 CosyVoice 或 MeloTTS / OpenAI 兼容 / 阿里云，任选 |
| 窥屏（可选） | RapidOCR + Qwen2.5-VL；缺省自动退化 |

安装后端依赖：

```powershell
pip install -r backend\requirements.txt
pip install -r katago_service\requirements.txt
```

### 4.2 一键开发启动

```powershell
# 启动本地 LLM / TTS / KataGo / 后端，然后打开游戏
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev_local.ps1

# 只起服务，不打开游戏（默认会等语音预热）
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev_local.ps1 -NoGame

# 快速起后端：语音模型后台加载，不再让后端排队等 TTS
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev_local.ps1 -NoGame -NoWaitTts

# 停止本次启动的服务
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev_local.ps1 -Stop
```

`dev_local.ps1` 会按当前 `game\config.json` 的语音配置选择本地语音引擎，并拉起：

- LLM `http://127.0.0.1:8080/v1`
- TTS `http://127.0.0.1:9880`
- KataGo 服务 `http://127.0.0.1:8100`
- 后端 `http://127.0.0.1:8000`

> 桌宠自身也会在 `auto_start_services=true` 且 `backend_url` 为本机地址时，通过 `tools/run_hidden_dev_local.vbs` 拉起服务；`AZURE_SERVICES_MANAGED=1` 用于避免重复启动。自动拉起会传 `-NoWaitTts`：**后端先就绪，CosyVoice / MeloTTS 在后台继续加载**，避免桌宠长时间显示离线。

### 4.3 手动启动

1. 启动 KataGo 服务：`katago_service\run_server.bat`，等 `/health` 的 `state` 为 `ready`。
2. 启动后端：`backend\run_server.bat`。
3. 启动客户端：

```powershell
Godot_v4.6.3-stable_win64.exe --path game
```

默认进入桌宠；也可以直接进入围棋窗口：

```powershell
Godot_v4.6.3-stable_win64.exe --path game -- --game
```

### 4.4 配置文件

- 源码运行：`game\config.json`。
- 发布包：把 `config.json` 放在可执行文件同目录即可覆盖包内默认值。
- 后端在启动时读取大部分配置；LLM / KataGo / 服务地址等改动后需重启后端。

---

## 5. 操作方式

| 操作 | 效果 |
| --- | --- |
| 左键点击桌宠 | 摸头反应、气泡、预制语气音、情绪/亲密积累 |
| 按住左键拖动 | 跟手拖动，松手带轻微惯性并夹在屏幕内 |
| 右键桌宠 | 打开菜单：聊天 / 资料与日程 / 围棋陪练 / 大小 / 退出 |
| 右键 → 聊天 | 打开聊天面板；输入后 Azure 的回复只显示在面板并用 TTS 播放 |
| 右键 → 资料与日程 | 选择「开始处理」：OCR / VLM 读取 `assistant_files/inbox/`，整理成日程 |
| 右键 → 围棋陪练 | 桌宠最小化，另开独立围棋窗口 |
| 关闭围棋窗口 | 桌宠恢复；棋局留在后端，下次显示“继续对局” |
| 右键 → 大小 | 120 / 160 / 200 / 240 px 四档 |
| 右键 → 退出桌宠 | 保存位置并退出客户端 |

围棋窗口内：玩家执黑先行，左键落子，右键拖拽旋转视角，滚轮缩放。具体棋子点评、提示、分析、复盘、棋谱库等操作沿用窗口内 UI。

---

## 6. 配置

关键字段（`game\config.json`）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `pet_enabled` | `true` | 启动进入桌宠；`--game` / `--no-pet` 可强制围棋 |
| `pet_height` | `160` | 桌宠显示高度（96–360，菜单四档） |
| `auto_start_services` | `true` | 本机后端且源码目录存在 `dev_local.ps1` 时自动拉起服务 |
| `backend_url` | `http://127.0.0.1:8000` | 后端地址 |
| `katago_url` | `http://127.0.0.1:8100` | KataGo 服务地址 |
| `llm_base_url` / `llm_model` / `llm_api_key` | 本地 | 任意 OpenAI 兼容 LLM |
| `llm_think_mode` | `0` | 思考档位：0 快速（默认）/ 1 中等 / 2 强思考 |
| `time_aware_enabled` | `true` | 注入绝对 / 相对 / 昼夜时间事实 |
| `memory_enabled` | `true` | 共享聊天流水、episode、长期记忆 |
| `assistant_inbox` | 空 | 资料箱路径；留空＝`<repo>/assistant_files/inbox` |
| `auto_plan_enabled` | `true` | 扫描后自动给待定任务排班 |
| `auto_plan_days` | `7` | 自动排班的未来天数（1–30） |
| `schedule_reminder_enabled` | `true` | 到点事务提醒（LLM 生成，失败用模板） |
| `schedule_archive_days` | `7` | 自动留档 / 手动「归档过期」的天数 |
| `schedule_retention_days` | `30` | 归档保留天数，超过后自动清理 |
| `schedule_buffer_min` | `45` | 每天工作窗口末尾预留的弹性时间（分钟） |
| `schedule_lock_hours` | `12` | 自动安排在未来多少小时内不再移动 |
| `schedule_risk_z` | `0.84` | 机会约束的 z 值（0.84 ≈ 80% 完成概率） |
| `schedule_chat_enabled` | `true` | 聊天里由 Azure 自己判断是否排入日程（关闭后聊天行为与旧版一致） |
| `schedule_ewma_alpha` | `0.35` | 真实耗时 EWMA 的更新速度 |
| `schedule_align_min` | `30` | 普通任务开始时间的对齐粒度（分钟） |
| `schedule_transition_min` | `10` | 课程 / 约定前后的通勤与切换留白（分钟） |
| `vlm_enabled` | `true` | 自动窥屏 / 资料图片读取；缺环境时退化 |
| `tts_enabled` | `true` | 语音总开关 |
| `tts_mode` | `local` | `system` / `local` / `openai` / `aliyun` |
| `tts_local_base_url` | `http://127.0.0.1:9880/v1` | 本地 TTS |
| `tts_local_voice` | `azure` | 本地音色（如 `azure` / `xiaoya`） |
| `pet_chat_tts_prebuilt` | `true` | 桌宠聊天只用预制情绪音（不合成整句；CosyVoice 照常启动，聊天面板可切回） |
| `ui_theme` | `mist` | 客户端主题 |
| `prompt_user_name` | `朋友` | 对玩家的称呼 |

完整字段与后端读取方见 `TECHNICAL.md`。部分设置可在围棋窗口设置面板中即时修改并写回配置；服务地址类配置通常需要重启后端。

---

## 7. 情绪、时间与记忆

### 7.1 情绪

- 现有展示区仍使用 **愉悦度 / 好奇度 / 唤醒度** 的 27 区表情映射，以保持既有立绘、语气词与语音音高。
- 内部增加 **掌控感（PAD-D）** 作为辅助轴，不强行改动 27 区映射。
- 双速指数衰减 + 20 秒事件粘性；情绪不会瞬移回中性。
- 情绪事件来自下棋、聊天、摸头、走神、窥屏、悔棋、局末等；LLM 只能通过限频情绪小调用给三维增量，不能直接改标签。

### 7.2 慢变量

| 变量 | 含义 | 行为 |
| --- | --- | --- |
| 精力 | 体力 / 反应速度 | 无互动缓慢下降，聊天与摸头回升；低精力只放慢节奏，不装死 |
| 无聊 | 想不想找人说话 | 无互动累积；高无聊缩短主动开口间隔 |
| 亲密 | 熟悉与放松程度 | 聊天/摸头/下棋缓慢累积，长时间不互动慢慢回落 |
| 信任 | 分寸与安全感 | 互动缓慢累积；低信任时保持距离 |
| 掌控感 | 底气 / 判断自信 | 成功交流与对局缓慢累积；长时空回落 |

### 7.3 时间线

`backend/data/life.db` 保存：

- 生命状态：时间戳、情绪快照、精力 / 无聊 / 亲密 / 信任 / 掌控感；
- 只追加事件：聊天、互动、走神、窥屏、开局、落子、局末；
- 相对时间事实：距上次互动 / 主动开口 / 聊天 / 落子，以及昼夜相位；
- **共享日程**：`schedule_items`（资料处理后的日期 / 时间 / 标题 / 备注 / 来源）。

进程不在时时间仍在走：启动时按真实流逝时间把慢变量结转到“现在”。

### 7.4 记忆

`backend/data/memory.db`：

- `chat_history`：桌宠与围棋共用的最近聊天原文；
- `episodes`：聊天片段；满 6 轮 / 620 字 / 15 分钟空档 / 效价跳变 0.30 自动收束；
- `memories`：可召回长期记忆；
- `games`：对局归档。

记忆是本地 SQLite，不上传云端；embedding 可选，没有配置时退化为关键词检索。

---

## 8. 数据与接口

### 8.1 REST（节选）

| 端点 | 作用 |
| --- | --- |
| `POST /api/chat` | 聊天；`mode=pet/game` 共享记忆。回复后由后端后台判断是否排入日程，排完推 `ai_schedule` |
| `POST /api/idle` | 走神搭话 |
| `POST /api/peer` | 窥屏搭话 |
| `POST /api/interaction` | 摸头等互动 |
| `GET /api/mood` | 当前情绪 + 慢变量快照 |
| `POST /api/mood/tick` | 情绪/时间心跳 |
| `GET /api/life` | 生命时间线快照（状态 / 相对时间 / 最近事件 / 日程） |
| `GET /api/files` | 资料箱文件列表 |
| `POST /api/files/scan` | OCR / VLM / 文档读取 → LLM 提取日程 → 写共享日程与记忆 |
| `GET /api/schedule` | 共享日程列表（含 `grade` / `grade_label` 自动分级；`include_archived=1` 看留档） |
| `POST /api/schedule/auto_plan` | 自动排班（分层收缩 + 机会约束 + 每日缓冲 + best-fit） |
| `POST /api/schedule/feedback` | 记录完成 / 推迟 / 跳过；更新 EWMA，推迟后立即重排 |
| `GET /api/schedule/review?days=7` | 按类别复盘计划 / 实际耗时偏差与完成、推迟计数 |
| `POST /api/schedule/archive` | 归档过期 / 指定日程（不删除） |
| `POST /api/schedule/cleanup` | 清理超过保留期的归档 |
| `POST /api/schedule/remind` | 手动触发一次事务提醒（LLM 文本 / 模板） |
| `GET /api/chat/history` | 最近聊天流水 |
| `GET /api/state` | 围棋棋盘权威快照 |
| `GET /api/health` | 引擎 / LLM / 记忆状态 |

### 8.2 WebSocket `/ws`

连接后先收到 `hello`（棋盘、情绪、设置同步）。主要事件：

`message / state / info / estimate / ownership / busy / mood / joseki / tool / token / speech / memory / difficulty`

- `speech` 与对应 `message` 同 `turn_id`、`seq` 递增配对；
- `mode=openai` 时前端按 `id` 取音频；`mode=system` 时带文本；
- 桌宠聊天默认**只播预制情绪音**（v4.2.5，`pet_chat_tts_prebuilt=true`）：出字不再等 CosyVoice
  合成，聊天面板里的「聊天只用预制音」可切回整句 TTS；待机 / 窥屏始终只用预制语气音，不退系统语音。
- `message.kind=ai_remind`（到点提醒）与 `ai_schedule`（聊天排程数据刷新）**都不发 speech**；
  `ai_schedule` 不再显示为聊天消息，只在资料面板打开时触发日程重拉，不抢聊天 busy、不占语音配对位。

---

## 9. 测试

### 后端

```powershell
cd backend
python -m unittest discover -s tests -q
```

当前全量：**1063 tests OK**。

### 前端门卫

```powershell
godot --headless --path game --script res://tests/test_pet_shell.gd
godot --headless --path game --script res://tests/test_pet_mode.gd
godot --headless --path game --script res://tests/test_config.gd
godot --headless --path game --script res://tests/test_tts.gd
```

完整清单：

```powershell
Get-ChildItem game\tests\test_*.gd | ForEach-Object {
  godot --headless --path game --script ("res://tests/{0}.gd" -f $_.BaseName)
}
```

当前 20 个前端脚本全部通过；`test_pet_shell.gd` 118 项断言（含月历 / 周视图 / 完成保留 / 聊天排程与历史清洗）。

### 启动自检

```powershell
godot --headless --path game --quit
```

退出码 0 即通过。

### 一键测试

```powershell
run_test_env.bat tests
```

---

## 10. 导出与发布

`game\export_presets.cfg` 提供 Windows Desktop 导出预设（**PCK 已嵌入 exe**，并排除 `tests/`），可直接用命令行导出：

```powershell
godot --headless --path game --export-release "Windows Desktop" "build\Azure.exe"
```

导出后把 `game\config.json` 与仓库根目录的 `Azure.vrm` 复制到 `build\`（与 exe 同目录），双击 `build\Azure.exe` 即可运行。`build/` 是本地产物，已 gitignore。

发布前建议检查：

1. 后端全量与前端 20 个脚本全部通过；
2. `godot --headless --path game --quit` 退出码 0；
3. 桌宠透明、置顶、拖动与右键菜单在目标机器上正常；
4. `assets/azure_pet.png`、`assets/voice/`、`Azure.vrm` 在导出包中可用；
5. `config.json` 随包放置，服务地址与本机实际部署一致；
6. 本地服务（LLM / TTS / KataGo / 后端）能被 `dev_local.ps1` 或被部署环境正常拉起。

> 仓库本身不包含大体积模型权重、KataGo 引擎与第三方语音权重；这些按各自上游许可单独分发。

---

## 11. 目录结构

```text
.
├─ game\                          Godot 4.6.3 客户端
│  ├─ scenes\Launcher.tscn        主场景：桌宠 / 围棋选择
│  ├─ scenes\Main.tscn            独立围棋窗口
│  ├─ scripts\app\                Launcher / PetShell / PetView
│  ├─ scripts\ui\                 Main / Board3D / AzureAvatar / TitleScreen...
│  ├─ scripts\net\NetClient.gd     REST + WebSocket 客户端
│  ├─ scripts\services\           TTSClient / SpeechPairing
│  ├─ assets\azure_pet.png        桌宠完整半身图
│  ├─ assets\voice\               预制语气音 / 欢迎语
│  ├─ tests\                      前端 headless 门卫
│  └─ config.json                 客户端配置
├─ backend\                       FastAPI 后端
│  ├─ app\agent.py                回合编排 / 情绪 / 事件
│  ├─ app\agent_graph.py          LangGraph 阶段
│  ├─ app\emotion.py              PAD / 双速 / 粘性
│  ├─ app\life.py                 生命时间线 / 慢变量 / 共享日程
│  ├─ app\memory.py               共享记忆 / episode
│  ├─ app\file_service.py         资料箱 / 扫描器编排 / VLM 拉起
│  ├─ app\schedule.py             资料文本 → 日程提取与归一化
│  ├─ app\auto_plan.py            自动排班（分层 James-Stein + 触发闸门 + best-fit）
│  ├─ app\schedule_learning.py    EWMA 时长 / 变更点 / 偏好 / 复盘
│  ├─ app\main.py                 REST / WebSocket
│  ├─ tests\                      后端测试
│  └─ data\                       运行产物（gitignore）
├─ assistant_files\
│  ├─ inbox\                      资料箱入口（图片 / PDF / 文档）
│  └─ README.md                   使用说明
├─ katago_service\                KataGo 常驻分析服务
├─ peek\                          截图 + OCR + VLM 窥屏
│  ├─ azure_peek.py               窥屏脚本
│  └─ azure_files.py              资料箱扫描 / OCR / VLM / 文档文本
├─ tools\                         隐藏启动 dev_local 的 VBS / LLM 启动器
├─ dev_local.ps1                  本地一键启动 / 停止
├─ run_test_env.bat               后端 + KataGo + 前端全量测试
├─ TECHNICAL.md                   详细技术文档
└─ LICENSE                        Apache License 2.0
```

---

## 12. 已知边界

- 透明置顶窗口以 **Windows** 为主要目标；其他平台未做逐像素透明验证。
- 桌宠是客户端主程序，但情绪 / 记忆 / LLM / TTS / 棋局编排仍依赖本机后端；后端不可达时只能显示离线气泡与本地反应。
- 窥屏依赖本机 Python、OCR 与 VLM；缺环境时自动退化为普通走神，不影响聊天与围棋。
- 资料箱扫描依赖 `peek/.venv` 的 OCR / VLM 环境；文本与 DOCX 可独立处理，PDF / 图片在缺少依赖时只会记录错误，不影响其他文件。
- 事件分割目前先落“时间 / 轮数 / 字数 / 情绪效价跳变”四类边界；话题 embedding 级分割、双时态记忆、记忆衰减/重新巩固仍在后续优化清单中。
- 发布包不内置大模型与引擎权重，首次部署仍需按第 4 节准备本机服务。

---

## 13. 致谢与许可

- **KataGo**：围棋引擎与棋力分析。
- **Godot Engine**：客户端渲染与场景。
- **MaiBot / A_Memorix**：episode 与长期记忆组织的思路启发。
- **Sentipolis / ES-Mem / Chronos / Pragma / AgenticTime** 等公开工作：情绪持久化、事件分割、时间感知与记忆连续性的设计参考。
- 本项目源码采用 **Apache License 2.0**，见 [LICENSE](LICENSE)。
- `server/` 下 CosyVoice 及其第三方组件、KataGo 引擎与权重、各模型权重分别遵循其上游许可。

---

**一句话：** Azure 不是“接了大模型的围棋程序”，而是一个有时间、有精力、会记得也会忘、会在你安静一会儿后轻轻开口的桌面助手；围棋只是她邀请你坐下来的一盘棋。
