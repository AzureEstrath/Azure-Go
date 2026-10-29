# Azure · 桌宠围棋助手 — 详细技术文档

> 适用分支：`personal`（自用版）；本地工作目录 `AzureGo-Creative`。
> 本文描述当前代码的真实实现，配置默认值以 `game/config.json` 与 `backend/app/config.py` 为准。
> **v4.0.0 起：桌宠是主程序，围棋是它打开的独立窗口。** 本文主体仍侧重围棋后端 / 客户端细节；
> 桌宠外壳、共享生命时间线、双速情绪与主动回忆见 `README.md`、`game/scripts/app/` 与 `backend/app/life.py`。
> **v4.1.0：资料箱 OCR 周课表、自动排班（优先级 + 正部 James-Stein 收缩 + best-fit 空闲区间）、
> 到点事务提醒（LLM 文本 / 离线模板气泡）、归档与清理、课表格子悬停展开。**
> **v4.1.1：类别 EWMA 时长学习、变更点检测、事件驱动滚动重排（1~3 天 + 15 分钟冷却 + 12 小时锁定窗）、
> 每日集中缓冲、机会约束 `mean + z·σ`、完成 / 推迟反馈与按类别复盘。**
> **v4.1.2：日程自动分级（固定课程 / 已约定 / 锁定 / 截止 / 可调 / 待排）、任务整点半点对齐、
> 课程与约定前后通勤留白、旧自动安排优先保位。**
> **v4.1.3：资料 / 日程面板紧凑化（544×520）与响应式操作区；窄窗口折叠按钮、聊天面板随视口收窄，
> 关闭 / 收起按钮始终可达。**
> **v4.1.4：资料扫描稳健化——课程表整表不再喂 LLM；聊天活动公告由 LLM + 规则补抽双保险；
> 只有星期没有节次的条目按普通约定处理。**
> **v4.1.5：周表窄窗口优先完整显示 7 天；有明确时刻的约定按开始时间画进对应节次；
> 日程 / 约定列表移到周表上方，避免被长表埋住。**
> **v4.1.6：日程悬停只保留一张自绘卡（移除默认黑底 tooltip）；自动安排带可解释理由；
> 连续推迟 ≥ 2 次优先大段空闲。**
> **v4.1.7：重扫去重（课程按日期 + 节次 + 标题，启动清理历史重复）；群公告事件不再误判成课程；
> 进行中的约定保持「已约定」；已结束的星期型事件标为「已结束」；同一格多个约定合并显示。**
> **v4.2.0：聊天 → 日程（规则闸门 + LLM 判断 + 后台落库 / 排班 / 记忆 + `ai_schedule` 补充）；
> 资料面板默认月历（7×6、年月翻页、今天高亮），点日期进入该周，周视图可前后翻周 / 回本月 / 回月历。**
> **v4.2.1：周表只显示所属周的课程（不再每周套同一张基底课表）；已结束的约定仍留在本周周表。**
> **v4.2.2：聊天历史/记忆只存可读台词（`[[tone:...]]` 不落库、不显示）；窄窗口周表宽度预留滚动条；
> 切月 / 切周后回到顶部，周标题与表头不会被滚动位置藏住。**
> **v4.2.3：完成的任务保留在日历/周表（`/api/schedule` 对展示面 include_done=True，标「已完成」）；
> 聊天默认思考档位改为中等（`llm_think_mode=1`），思考扩展字段随档位启用。**
> 最后更新：2026-10-29。

---

## 目录

1. [项目定位](#1-项目定位)
2. [总体架构](#2-总体架构)
3. [技术栈](#3-技术栈)
4. [仓库结构](#4-仓库结构)
5. [客户端（Godot）](#5-客户端godot)
6. [后端（FastAPI + LangGraph）](#6-后端fastapi--langgraph)
7. [配置系统](#7-配置系统)
8. [数据与持久化](#8-数据与持久化)
9. [运行、部署与打包](#9-运行部署与打包)
10. [测试体系](#10-测试体系)
11. [关键设计取舍](#11-关键设计取舍)
12. [已知限制与注意事项](#12-已知限制与注意事项)
13. [扩展与维护指南](#13-扩展与维护指南)
14. [附录：REST / WS 速查](#14-附录rest--ws-速查)

---

## 1. 项目定位

Azure · 围棋陪练是一个 **本地优先的 AI 围棋陪练**：

- 棋力由 KataGo 提供，后端通过独立的 KataGo 云服务调用 analysis 协议；
- 对话、讲解、定式教学与复盘由 LLM 驱动，支持任意 OpenAI 兼容服务（云端 DeepSeek 或本机 llama.cpp）；
- 语音支持系统语音、本地 CosyVoice/MeloTTS、云端 OpenAI 兼容 TTS、阿里云 Voice Studio 四档；
- 客户端为 Godot 3D 界面：程序化 19×19 棋盘、VRM 化身、情绪表情、领地热力图、定式卡片、棋谱库、全盘复盘与战报；
- 前端只做渲染、输入与音频播放，**对局编排与所有模型调用都在后端进程**；本地服务链由 `dev_local.ps1` 代办，客户端启动时可自动触发。只要游戏会接管本地语音，客户端会在主界面 / 设置面板期间先用 `dev_local.ps1 -TtsOnly -QualityTts` 预热 CosyVoice（不写配置、不起其他服务），进对局时不再阻塞等待；加载期客户端压掉系统语音降级（只显示文字），就绪后自动换回 Azure 克隆音色。本地音色在左侧栏下拉里直接选（运行期只推 `/api/config` 改后端内存，不重启、不丢棋局）。

### 1.1 当前分支布局

| 分支 | 本地目录 | 用途 |
| --- | --- | --- |
| `main` | — | 对外发布快照，保持冻结，不接收日常提交 |
| `personal` | `AzureGo-Creative` | 当前开发线，本文档描述的对象 |
| `classic` | `AzureGo-classic` | 历史存档，只读为主 |

日常开发只在 `personal`。

### 1.2 代码规模

| 部分 | 规模 |
| --- | --- |
| 后端 `backend/app` | 约 11,565 行 Python |
| 前端 `game/scripts` | 约 9,554 行 GDScript |
| 后端测试 | 897 个用例（40 个测试文件） |
| KataGo 服务测试 | 36 个用例 |
| 前端 headless 测试 | 18 个脚本 |

---

## 2. 总体架构

```text
┌──────────────────────────────┐
│ Godot 客户端（game/）         │
│ 3D 棋盘 / UI / VRM / 音频播放 │
│ 只连后端；可代办拉起服务       │
└──────────────┬───────────────┘
               │ REST 命令 + WebSocket 事件
               │ http://127.0.0.1:8000
┌──────────────▼───────────────┐
│ FastAPI 后端（backend/）      │
│ 对局编排 / LangGraph / LLM   │
│ 情绪 / 定式 / 复盘 / TTS 合成 │
└───┬───────────┬───────────┬──┘
    │           │           │
    │ HTTP      │ OpenAI    │ HTTP / WebSocket
    │ /analyze  │ 兼容 API  │ /audio/speech、/health
┌───▼───────┐ ┌─▼─────────┐ ┌▼────────────────────┐
│ KataGo 云 │ │ LLM       │ │ TTS 服务            │
│ 服务 :8100│ │ 云端/本机  │ │ 本地 :9880 / 云端   │
│ analysis  │ │ :8080     │ │ 阿里云 Voice Studio │
└───────────┘ └───────────┘ └─────────────────────┘
```

### 2.1 进程与端口

| 进程 | 默认端口 | 启动方 | 职责 |
| --- | --- | --- | --- |
| Godot 客户端 | — | 玩家 / `dev_local.ps1` | 渲染、输入、播放语音与音效 |
| 后端 FastAPI | 8000 | `backend/run_server.bat` / `dev_local.ps1` | 唯一编排权威：REST + `/ws` |
| KataGo 云服务 | 8100 | `katago_service/run_server.bat` / `dev_local.ps1` | 常驻 `katago analysis`，暴露 `/health`、`/analyze` |
| LLM（本机） | 8080 | `tools/start_llm_server.py` | llama.cpp / OpenAI 兼容服务 |
| 本地 TTS | 9880 | `dev_local.ps1`（`server/azure_tts_server.py`） | CosyVoice/MeloTTS 语音合成 |
| 阿里云 TTS | 远端 WebSocket | 后端 `tts_aliyun.py` | 流式语音合成 |

### 2.2 一条完整回合

```text
玩家点击交叉点
  → 客户端乐观落子，POST /api/move
  → 后端校验并交给 Agent（LangGraph）
  → KataGo /analyze（局面与候选点）
  → LLM 决定应手 + 台词（可调用发言侧工具）
  → 生成 speech 事件（TTS 合成或系统语音指令）
  → WS 推送 message / state / info / busy / mood / joseki ...
  → 客户端在落子宣告语音开始时 POST /api/reveal，才真正显示 AI 那一手
```

棋谱、记忆、画像、定式计数都在局末或显式请求时写入 SQLite，落子回合内零数据库写入。

---

## 3. 技术栈

| 层 | 技术 |
| --- | --- |
| 客户端 | Godot 4.6.x（本机 4.6.3）、GDScript、Forward+、1280×800、MSAA 3D 2× |
| 后端 | Python、FastAPI、uvicorn、websockets、httpx、LangChain 1.x、LangGraph 1.x、SQLite |
| 棋力 | KataGo `analysis` 模式，独立 FastAPI 云服务封装 |
| LLM | OpenAI 兼容 Chat Completions；默认 DeepSeek 云端，可切本机 llama.cpp（Qwen 系列 gguf） |
| TTS | 系统语音 / CosyVoice 3·MeloTTS·sherpa 本地服务 / OpenAI 兼容 / 阿里云 Voice Studio WebSocket |
| 人物 | VRM（`Azure.vrm`），程序化表情、视线与风场 |
| 测试 | `unittest`（后端/KataGo 服务）+ Godot headless 脚本 |
| 开发脚本 | PowerShell 5.1、Windows 批处理 |

后端依赖（`backend/requirements.txt`）：`fastapi>=0.110`、`uvicorn>=0.27`、`websockets>=12`、`httpx>=0.27`、`langchain-core>=1.0,<2.0`、`langchain-openai>=1.0,<2.0`、`langgraph>=1.0,<2.0`。

KataGo 服务只依赖 `fastapi`、`uvicorn`（`httpx` 仅测试用）。

---

## 4. 仓库结构

```text
.
├─ Azure.vrm                    人物模型（约 14MB，随仓库提供）
├─ TECHNICAL.md                 本文档
├─ README.md                    使用说明、配置字段与接口契约
├─ AGENTS.md                    开发约定与测试清单
├─ game/                        Godot 工程
│  ├─ scenes/Main.tscn          主场景（仅一个 Node3D 根节点）
│  ├─ scripts/
│  │  ├─ ui/                    Main / Board3D / AzureAvatar / GufengTheme /
│  │  │                        TitleScreen / JosekiCard / GameLibrary /
│  │  │                        GameReport / MiniBoard / Ambience
│  │  ├─ services/              TTSClient / SpeechPairing / SfxPlayer
│  │  ├─ core/                  AzureConfig / Coords / GameReplay
│  │  └─ net/                   NetClient / ChatQueue
│  ├─ assets/                   voice（27 个 wav）、sfx（棋子/提子/终局/虚着）
│  ├─ data/thinking_lines.json  思考短句解析工具的数据（当前运行路径不轮播）
│  ├─ tests/                    18 个 headless 测试脚本
│  ├─ config.json               客户端配置模板（前后端共读）
│  └─ export_presets.cfg        Windows Desktop 导出预设
├─ backend/
│  ├─ app/                      FastAPI 应用与全部编排逻辑
│  ├─ tests/                    897 个用例
│  ├─ data/                     运行产物（gitignore）：memory/profile/games/joseki_progress.db
│  ├─ logs/                     dev_local 日志与 PID 文件（gitignore）
│  ├─ requirements.txt
│  └─ run_server.bat
├─ katago_service/              KataGo 云服务（server.py / proc.py / tests）
├─ server/                      自建语音服务（azure_tts_server.py、OpenVoice、sherpa）
├─ tools/                       start_llm_server.py、joseki_extract.py
├─ peek/                        VLM 窥屏脚本（azure_peek.py 截图/OCR/VLM、start_vlm_server.py 拉起服务）
├─ docs/                       设计文档与实施计划
├─ dev_local.ps1                本机一键开发启动/停止
└─ run_test_env.bat             全栈启动与全量测试入口
```

---

## 5. 客户端（Godot）

### 5.1 启动流程

`game/scenes/Main.tscn` 只有一个 `Node3D` 根节点，界面、棋盘、化身全部由 `Main.gd` 程序化构建。`Main._ready()` 的顺序：

1. `AzureConfig.load_config()` 读取配置；
2. `_apply_cli_overrides()` 处理命令行覆盖；
3. `GufengTheme.apply_preset(cfg.ui_theme)`：**先落主题，再建任何控件**；
4. 构建语音配对状态机、音效播放器、CJK 字体；
5. `_build_world()`：世界环境、方向光、风场、VRM 化身；
6. `_build_board()`：3D 棋盘与交互信号；
7. `_build_ui()`：左右栏、设置、定式卡片、点目面板等；
8. 读取思考台词，构建纸幕封面 `TitleScreen`；
9. 首次启动等待「开始对局」；
10. 可选显示启动设置面板；
11. 若 `auto_start_services=true` 且后端在本机，检查 CosyVoice：未就绪时弹窗询问是否等待；
    等待用 `-QualityTts` 拉起并轮询 `/health`，不等用 `-QualityTts -NoWaitTts` 让它在后台加载；
12. `_begin_new_game_on_entry()` 先立「等 reset」闸，避免上一局迟到的 `hello/state` 闪出；
13. `_build_services()` 装配 `NetClient`，`_start_launch_game()` 保证空局；
14. `_greet()` 播放入场欢迎语。

所有控件都是运行时 `new()` 出来并挂在 `CanvasLayer` 下，没有大量 `.tscn` 资源，因此改 UI 主要改 `Main.gd` 与 `GufengTheme.gd`。

### 5.2 界面与功能

| 模块 | 职责 |
| --- | --- |
| `TitleScreen` | 纸幕封面：开始对局 / 棋谱库 / 设置 / 退出 |
| 左侧栏 | 朗读方式（四档）、阿里云配置（仅 aliyun 模式显示）、对局开关、提示/分析按钮 |
| 右侧栏 | 胜率/目差、聊天、状态栏、棋谱库入口 |
| 设置面板 | LLM 地址/模型/Key、思考档位、TTS、界面主题、棋盘与音效开关 |
| `JosekiCard` | 定式名称、手顺、变化候选、走错提示 |
| 点目面板 | `estimate` 事件的目差与逐点归属统计 |
| `GameLibrary` | 棋谱列表、导入 SGF、删除、打开棋谱 |
| `GameReport` | 局末战报：手数、胜率曲线、提子、定式统计 |
| 复盘模式 | 上一手/下一手、只读重放、逐手注记、全盘 AI 复盘分段 |
| `MiniBoard` | 棋谱库/复盘中的缩略图 |

### 5.3 3D 棋盘（`Board3D.gd`）

- 19×19 交叉点、星位、坐标标签，全部程序化生成；
- `_world()` 把棋盘坐标映射到 3D 空间；相机根据侧栏展开状态偏移视点；
- 左键落子，右键拖拽旋转，滚轮缩放，Home 回正，C 键平滑转到下一个角（0.35s smoothstep；飞行期间视角输入让路）；
- 棋盘点击信号 `intersection_clicked`、摸头信号 `head_patted`、视角变化 `view_changed`；
- `ownership` 事件用 19×19 半透明贴图绘制领地热力图，可开关；
- `joseki` 事件绘制定式轨迹与高亮；
- 悬停显示坐标与手数；落子、提子、终局、虚着触发 `SfxPlayer` 音效；
- 当前客户端删除了山水背景板、圆形木地板与护栏，只保留主题底色与风场。

### 5.4 化身与环境

`AzureAvatar.gd` 加载 `Azure.vrm`，负责：

- 视线：默认看鼠标落点，旋转视角时看玩家；
- 表情与情绪：由 `mood` 事件驱动（愉悦度、好奇度、唤醒度）；
- 风场摆动：采样 `Ambience.wind()`；
- 摸头交互：点击头部附近触发回应；
- 语音口型/节拍由 `speech` 事件与播放进度驱动。

`Ambience.gd` 只剩风场计算，不再有地板、侧壁与环形护栏。

### 5.5 主题系统（`GufengTheme.gd`）

四套预设，均在运行时切换静态色板：

| id | 名称 | 风格 |
| --- | --- | --- |
| `warm` | 暖白简约 | 浅暖底色、低对比 |
| `classic` | 宣纸古风 | 米黄纸感、楷体 |
| `mist` | 雾蓝清透（默认） | 冷灰蓝、无衬线 |
| `dark` | 深色青蓝 | 深底、青蓝强调 |

- `PALETTES` 定义 paper / ink / accent / 字体等；`apply_preset()` 写入静态变量；
- `font()` 根据 `FONT_SERIF` 选楷体或无衬线；
- 面板、气泡、输入框、滑杆、按钮、标题、棋子提示均读取同一套主题色；
- 图标（滑杆 grabber、复选勾）改为程序化绘制，不再依赖 png；
- 主题写入 `config.json` 的 `ui_theme`，**切换后重启生效**；
- 命令行 `--theme=warm|classic|mist|dark` 可临时覆盖。

### 5.6 配置（`AzureConfig.gd`）

配置字段分组：

| 组 | 字段 |
| --- | --- |
| 后端 | `backend_url`、`auto_start_services` |
| LLM | `llm_base_url`、`llm_model`、`llm_api_key`、`llm_think_mode`、`extra_body` |
| 棋力 | `max_visits`、`katago_url`、`katago_token`、`katago_timeout_ms` |
| 教学 | `joseki_teach`、`joseki_rate`、`adaptive_difficulty` 系列 |
| 情绪 | `emotion_drift`、`drift_base`、`drift_max` |
| 记忆/画像 | `memory_enabled`、`memory_*`、`profile_enabled`、`time_aware_enabled` |
| 语音 | `tts_enabled`、`tts_mode`、`tts_local_*`、`tts_aliyun_*`、`tts_speed/pitch/volume`、`tts_sync_speak/batch_speak/long_chunk` |
| 视觉 | `ui_theme`、`vrm_path`、`ownership_enabled` |
| 音效 | `sfx_enabled`、`sfx_volume` |
| 提示词 | `prompt_system`、`prompt_user_name`、`prompt_phase_notes` |

命令行覆盖（只影响本次运行，不写回配置）：`--backend-url=`、`--llm=`、`--model=`、`--key=`、`--theme=`。

`save()` 只覆盖改动项，保留文件里的相对路径与其他字段。

### 5.7 网络层（`NetClient.gd`）

- 连接 `base_url`，HTTP 自动推导 `ws://` 或 `wss://` 的 `/ws`；
- 重连退避 1s → 8s；连接超时 8s；REST 超时 30s；
- 服务端事件按 `type` 分发为 GDScript 信号（见附录 B）；
- **命令走 REST，事件走 WS**：WS 是单向推送，客户端不通过 WS 发命令；
- 聊天在忙碌时进入 `ChatQueue`，空闲后由 `flush_queued_chat()` 续发；发送失败有 2s 重试闸；
- 设置类字段（定式、情绪、画像、自适应）采用批量写入，只推一次 `/api/config`；
- 落子采用乐观更新：先落子，后端拒绝时用 `move_rejected` 的权威快照回滚；
- `reset_game()` 返回 `bool`，供重开竞态时判断后端是否真的受理。

### 5.8 语音链路

```text
后端 message（带 turn_id）
  → 后端 speech 事件（同 turn_id + seq；mode=openai/local/aliyun 时带 audio id，system 时带 text）
  → NetClient 转发给 SpeechPairing
  → SpeechPairing 决策：文字是否等语音、是否超时、是否乱序、是否缺音频
  → TTSClient 取音频（GET /api/tts/audio/{id}）或调用系统语音
  → 音频播放与气泡显示同步
```

关键点：

- `ai_notice` 明确声明「这条没有语音」，不登记配对，避免挤掉后续真语音；
- 语音缺失/超时不会锁死棋盘，配对状态机有超时兜底；
- 预制开场欢迎语；情绪语气词只在「Azure 思考中…」占位出现的**思考节点**插入，候选音优先匹配上一句的 `[[tone]]` / `{行为}`，频率由当前情绪强度决定（语义匹配约 8%~50%，纯情绪约 2%~22%，另有 2 节点 + 6 秒连播闸）；思考、逐句 tone、落子打断都不插；摸头保留一声 `pat` 反应；
- `tts_sync_speak` 控制文字是否等语音；`tts_batch_speak` 控制多条消息是否合并朗读。

### 5.9 前端测试

`game/tests/` 下 18 个 headless 脚本，覆盖：配置读写、标题屏、新局、视角与悬停、定式卡片与档位、聊天队列、语音配对、TTS、口语化目差、复盘、战报、棋谱回放、点目、形象路径、音效、思考短句解析工具。运行方式见第 10 节。

---

## 6. 后端（FastAPI + LangGraph）

### 6.1 应用生命周期

`backend/app/main.py::create_app()`：

1. 读取 `AzureConfig`（环境变量 `AZURE_CONFIG` 优先；否则当前目录 `config.json` → `backend/config.json`）；
2. lifespan 内构造 `MemoryService`、TTS 客户端、`JosekiProgress`、`ProfileStore`、`GameStore`；
3. 构造 `KataGoEngine`、`LLMClient`、`AzureAgent`，把记忆、TTS、定式计数、画像库、棋谱库挂到 Agent；
4. `autostart=True` 时等待 KataGo 云服务就绪（最长 180s），失败只打印横幅，不阻止后端运行；
5. 退出时依次关闭记忆库、TTS、KataGo、LLM。

注意：**后端在等待 KataGo 预热期间不监听 8000 端口**，客户端会显示「未连接后端」，预热完成后自动重连。

### 6.2 REST API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 后端、KataGo、LLM、记忆库健康状态 |
| GET | `/api/state` | 当前棋局权威快照 |
| GET | `/api/profile` | 玩家棋风画像（只读，功能关时为空） |
| POST | `/api/move` | 玩家落子 |
| POST | `/api/hint` | 求提示 |
| POST | `/api/analyze` | 局面分析 + 点目注记 |
| POST | `/api/chat` | 聊天；回复后在后台判断是否排入日程，排完推 `ai_schedule` |
| POST | `/api/summarize` | 结束对局并总结、归档棋谱 |
| POST | `/api/undo` | 撤销到指定手 |
| POST | `/api/reset` | 开新局 |
| POST | `/api/config` | 运行时改设置（朗读详略、思考档位、定式、情绪、画像、自适应等） |
| POST | `/api/reveal` | 语音开始时揭示 AI 已下的一手 |
| POST | `/api/interaction` | 玩家互动（摸头等） |
| POST | `/api/idle` | 空闲搭话（普通走神） |
| POST | `/api/peer` | 窥屏搭话（前台窗口 + OCR + VLM 画面描述） |
| POST | `/api/mood/tick` | 情绪自然衰减/波动 |
| POST | `/api/review` | 进入/退出局后复盘 |
| POST | `/api/review/intro` | 复盘开场白（纯本地，只显示不发声） |
| POST | `/api/review/goto` | 复盘只读重放到第 N 手 |
| GET | `/api/review/notes` | 逐手注记 + 真亏手账 |
| POST | `/api/review/full/segment` | 全盘 AI 复盘（按段） |
| GET | `/api/games` | 棋谱库列表 |
| GET | `/api/games/{id}/remark` | 某局的「她的回忆」（纯本地短文案） |
| GET | `/api/games/{id}` | 单局详情 |
| GET | `/api/games/{id}/sgf` | 导出 SGF |
| POST | `/api/games/import` | 导入 SGF |
| DELETE | `/api/games/{id}` | 删除棋谱 |
| POST | `/api/tts/speak` | 试听合成 |
| GET | `/api/tts/audio/{audio_id}` | 取已合成音频 |
| POST | `/api/memory/query` | 查询长期记忆 |
| POST | `/api/memory/write` | 手动写入记忆 |
| GET | `/api/memory/stats` | 记忆库体量 |
| POST | `/api/memory/forget` | 删除记忆 |
| GET | `/api/schedule` | 共享日程列表 |
| POST | `/api/schedule/auto_plan` | 自动排班（分层收缩 + 机会约束 + 每日缓冲 + best-fit） |
| POST | `/api/schedule/feedback` | 完成 / 推迟 / 跳过反馈 → EWMA 学习；推迟后立即重排 |
| GET | `/api/schedule/review` | 按类别复盘计划 / 实际耗时偏差 |
| POST | `/api/schedule/archive` | 归档过期 / 指定日程 |
| POST | `/api/schedule/cleanup` | 清理超过保留期的归档 |
| POST | `/api/schedule/remind` | 手动触发一次事务提醒 |
| GET | `/api/files` | 资料箱文件列表 |
| POST | `/api/files/scan` | OCR / VLM / 文档读取 → 提取日程 → 自动排班 |
| WS | `/ws` | 服务端事件推送 |

### 6.3 WebSocket 事件协议

`/ws` 是**单向推送**：连接后先收 `hello`，之后持续接收事件。客户端命令全部走 REST。

| type | 关键字段 | 说明 |
| --- | --- | --- |
| `hello` | `state`、`mood`、`busy` | 连接同步 |
| `message` | `kind`、`text`、`move?` | `kind`：`ai` / `ai_notice` / `ai_comment` / `ai_move` / `ai_idle` / `ai_remind` / `ai_schedule` / `ai_review` / `difficulty` / `user` / `system` |
| `state` | 棋盘、手数、提子等 | 权威快照 |
| `info` | `winrate`、`score` | 右栏数字 |
| `estimate` | `komi`、`move_count`、`lead`、`black`、`white`、`cells` | 点目注记，不进 LLM |
| `ownership` | `values`、`move_count` | 361 点逐点归属 |
| `busy` | `busy` | 她是否正在应手 |
| `mood` | 三维情绪 | 表情、语气、音高 |
| `joseki` | `kind`、`corner`、`points`、`family`、`name`、`k`、`line`、`candidates`、`show_card`、`mistake` | 定式轨迹与卡片 |
| `tool` | `phase`、工具名 | 发言侧工具进度，只喂状态栏 |
| `token` | `turn_id`、`delta` | 可选流式增量，默认关闭 |
| `speech` | `turn_id`、`seq`、`mode`、`id`/`text` | 语音指令 |
| `memory` | `op` 等 | 记忆元数据，只记日志 |
| `difficulty` | 档位、基线、原因 | 自适应难度变化 |

`ai_move` 额外带 `move`；客户端在对应语音开始时才 `POST /api/reveal`，实现「先说话、后落子」的音画同步。

### 6.4 LangGraph 编排

图结构（`agent_graph.py::build_graph`）：

```text
START → prepare → decide ──(需要工具且未超预算)──→ tools → decide
                     └────(否则)────→ speak → finalize → END
```

`TurnState` 字段：`phase`、`moves`、`messages`、`reply`、`tool_calls`、`wants_tools`、`move`、`turn_id`、`data`。

- `prepare`：构造本回合上下文（棋局、分析、情绪、记忆、画像、时间、定式、复盘资料）；
- `decide`：调用 LLM，决定台词、是否用工具、是否落子；
- `tools`：执行发言侧工具（查盘、复算、查定式），结果只回模型，不进台词；
- `speak`：清洗台词、分块、TTS、发 `message`/`speech`；
- `finalize`：落子、更新状态、写日志、收尾。

入口方法：`player_move`、`hint`、`analyze_position`、`chat`、`summarize`、`greet_with_context`；`undo`、`reset_game` 为同步控制。

可靠性机制：

- `busy` 有看门狗，回合异常也会归位，避免棋盘锁死；
- `turn_id` / epoch 防止迟到的旧回合台词或事件回填；
- LLM/TTS 失败都有可读兜底，不把异常抛给上层；
- 玩家落子被拒绝时返回权威快照，客户端回滚乐观落子。

### 6.5 Agent 能力

| 能力 | 模块 | 说明 |
| --- | --- | --- |
| 对局 | `agent.py` / `agent_graph.py` | 应手、提示、分析、聊天、总结 |
| 棋力 | `engine.py` | KataGo analysis 客户端 |
| 情绪 | `emotion.py`、`decay.py` | 三维情绪、自然衰减、小概率偏航 |
| 定式 | `joseki.py`、`joseki_gate.py`、`joseki_progress.py` | 识别、族计数、提示门控、教学卡片 |
| 复盘 | `full_review.py`、`pv_lines.py`、`estimate.py` | 分段全盘复盘、预测线、点目 |
| 记忆 | `memory.py`、`memory_service.py` | 长期记忆，可选 embedding，默认关 |
| 画像 | `profile.py`、`profile_store.py` | 棋风画像、时间感知，默认关 |
| 人格化 | `persona.py`、`profile_store.py`（persona 表） | 关系阶段（默认关，可开关并自选称呼名）、情绪余温、今天的小心思、旧局回忆、夜間节奏；`persona_enabled=false` 时零变化 |
| 自适应 | `adaptive.py` | 1~5 档，局内 ±spread |
| 棋谱 | `game_store.py`、`sgf.py` | SQLite 棋谱库、SGF 导入导出 |
| 语音 | `tts.py`、`tts_aliyun.py`、`speech_text.py` | 四档 TTS、文本清洗与分块、缓存 |
| 提示词 | `prompts.py`、`prompt_chains.py` | 人设、阶段提示、长度约束 |

### 6.6 LLM 层

- `llm_chain.py`：用 LangChain `ChatOpenAI` 装配模型，合并 `extra_body`，解析内容；
- `llm.py`：`chat()` 薄适配器，负责输出预算、错误中文化、参数被拒后的降级重试；
- 思考档位：0 快速 / 1 中等（+160 token 推理预算）/ 2 强思考（+800），另有 4096 常态推理余量；
- 推理段永不显示给玩家，只取 `content`；
- 支持流式 `astream`（`stream_tokens` 默认关）；
- 情绪增量用结构化输出 `MoodDelta`，超界裁剪、NaN 归零；
- 网络失败、空回复、服务端拒绝都有兜底台词，不阻塞回合。

### 6.7 KataGo 接入

后端只做 HTTP 客户端，不再拉起子进程：

- `GET /health` → `state` 为 `warming` / `ready` / `dead`；就绪判定只认 `state`，不认 `ok`；
- `POST /analyze` → 引擎原文；请求含 `moves`、`rules`、`komi`、`boardXSize/YSize`、`maxVisits`、`includeOwnership`；
- 传输层失败才置 `katago_dead`；超时/503/401/500 只刷新 `katago_error`；
- 一次成功分析会清除 `dead`，服务重启后可自愈；
- `max_visits` 默认 500（代码），随包 `config.json` 为 100；玩家落子后的那次分析取 `max(pv_max_visits, max_visits)`，只增不减。

KataGo 服务（`katago_service/server.py`）：

- FastAPI lifespan 先置 `warming`，后台预热，完成后置 `ready`/`dead`；
- 常驻 `katago analysis` 子进程，`proc.py` 持续排空 stderr、64MB 读行缓冲；
- Bearer token 鉴权；未配 token 仅允许回环调试；
- `/analyze` 在非 ready 时立即回 503 可读错误，避免客户端超时。

### 6.8 TTS 后端

统一入口 `app/tts.py`，四档模式：

| 模式 | 行为 |
| --- | --- |
| `system` | 不请求合成，只发 `mode=system` 的文本指令，交给 Windows 语音 |
| `local` | 只试 `tts_local_base_url`（CosyVoice/MeloTTS），失败回落系统语音 |
| `openai` | 有本地地址时本地优先，再试云端 `tts_base_url`，最后系统语音 |
| `aliyun` | 由 `AliyunTTSClient` 走 Voice Studio WebSocket，失败整条降级 |

合成流程：

1. `speech_text`：提取语气、清洗文本、按 100/160 字分块；
2. 同步 `httpx.Client` 经 `asyncio.to_thread` 发出，避免冻住事件循环；
3. 单块原样缓存；多块 WAV 重建头，多块 MP3 直接拼接；
4. 缓存按条数（32）与字节数（8MB）双向 LRU 淘汰；
5. 任一环节失败**整条**降级为系统语音，绝不返回半截音频，也绝不抛异常卡死回合。

本地语音服务（`server/azure_tts_server.py`）兼容 CosyVoice 3/2、MeloTTS、sherpa，提供 `/inference_sft`、`/v1/audio/speech`、`/health`、`/voices`、`/v1/models`；`voices.json` 与 `sherpa_voices.json` 定义音色。

本地服务按 `tts_local_voice` 选择引擎：`melo` / `xiaoya` / `shaonv` / `nvsheng` 等走 sherpa 轻量 VITS；`azure`（及 SFT 预设音色）才走 CosyVoice。**即使 CosyVoice 已加载，`tts_local_voice=xiaoya` 仍会合成轻量声线**；要质量音色必须把 voice 设为 `azure`，并让后端在配置写入后重启。

CosyVoice3 的 zero-shot 对个别短句可能产出空音频（服务端 `no audio produced`）。本地服务内部按 `cross-lingual → SFT 预设 → sherpa 轻量` 三级兜底，保证本地始终有声音；只有服务不可达或三级都失败时，后端才按 fail-closed 整条降级为系统语音。

### 6.9 后端测试

`backend/tests` 共 **1063** 个用例，覆盖：

- 配置、事件、引擎、棋盘、坐标、状态；
- Agent 与 LangGraph 各阶段、工具循环、看门狗、忙闲；
- 提示词、LLM 链、流式、降级；
- 情绪、衰减、偏航、自适应；
- 定式识别、门控、族计数、教学卡片；
- 点目、预测线、全盘复盘、SGF；
- 棋谱库、记忆、画像、时间感知；
- TTS 文本、缓存、阿里云协议、语音链路；
- 动态日程：James-Stein / EWMA / 变更点 / 机会约束 / 缓冲 / 锁定窗口 / 触发闸门 / 反馈与复盘；
- 聊天排程：规则闸门 / JSON 解析 / 规范化 / 过去事件过滤 / 补充文案 / 开关 / `ai_schedule` 事件；
- FastAPI 端点与 WebSocket 事件。

KataGo 服务测试 36 个，用假引擎，不依赖真实 KataGo。

### 6.10 动态日程排程（v4.1.1 ~ v4.2.0）

`auto_plan.py`（纯逻辑）+ `schedule_learning.py`（纯学习）+ `life.py`（持久化）分工：

0. **日程自动分级（v4.1.2）**：`classify_item()` 是 UI 与排班共用的唯一判定：
   - `fixed`：课程（`kind=course`，或带节次）——自动排班永不移动；
   - `committed`：有明确开始时间且非自动排、**还没结束（含正在进行）**——用户定下的约定，不移动；
   - `locked`：自动安排且在未来 `schedule_lock_hours` 内——本次重排不移动；
   - `deadline`：没有固定时间但有截止时间——时间可动，截止不可动；
   - `tentative`：自动安排且锁定窗外——可重排，但原时间仍可行时优先保持原位；
   - `flexible`：完全待定；`closed`：已完成 / 取消 / 归档，或已结束的星期型事件（不再自动重排）。
   `/api/schedule` 返回时统一补 `grade` / `grade_label`；面板日程行与悬停卡显示分级。
1. **时长估计**：`schedule_stats` 按类别保存 EWMA 均值 / 方差 / 样本数。
   `update_stats()` 收到完成反馈后更新；连续两次偏差 > 2.5σ 且样本 ≥ 3 ⇒ 变更点，均值重置到本次实际值。
2. **分层收缩**：`plan()` 先按类别可信度 `n/(n+3)` 把类别均值与全局均值混合，类别无记录时
   用「类别先验 0.5 + 全局均值 0.5」；同类别 ≥ 3 条走 `shrink_toward()`（向各自目标的正部 JS），
   1~2 条按 `n/(n+2)` 温和收缩；类别没有完成样本时完全相信本次声明时长。
3. **机会约束**：`risk_min = mean + z·σ`（上限 1.35 倍），`z` 由 `schedule_risk_z` 配置
   （0.84 ≈ 80% 完成概率）。每日 `buffer_min` 在窗口末尾集中预留，不逐任务加安全时间。
4. **放置与排序**：**72 小时内到期**的任务走 EDF；更远的截止任务按优先级密度参与常规排序，
   避免下周才到期的小任务抢占今天；其余按综合分 / 时长（优先级密度）降序。
   best-fit 选「最早一天 + 剩余最小」的空闲段；推迟过的任务优先排到明天。
   连续推迟 ≥ 2 次的任务，优先放进至少两倍时长的连续空档，不再死磕碎片时间。
5. **真实手感（v4.1.2）**：普通任务按 `schedule_align_min`（默认 30 分钟）对齐到本地
   :00/:30；若对齐后放不进空档，才退回 5 分钟粒度。课程 / 约定 / 锁定项前后各加
   `schedule_transition_min`（默认 10 分钟）通勤与切换留白，避免任务紧贴上课时间。
6. **滚动重排**：`should_replan()` 只在新增待定 / 关键任务逾期 / 容量不足时返回 `trigger=True`；
   后台循环每 5 分钟检查、两次重排至少隔 15 分钟，只排未来 1~3 天；
   `lock_sec`（默认 12 小时）内的自动安排与窗口外的自动安排都视为已承诺，不移动。
7. **反馈闭环**：`/api/schedule/feedback` 写回状态与实际耗时 → 更新类别统计 →
   推迟时立即按 1~3 天重排；`/api/schedule/review` 汇总各类别计划 / 实际偏差与完成、推迟、跳过计数。
   偏好分只做同优先级平局加分，不覆盖用户明确给出的优先级。
8. **可解释性（v4.1.6）**：每条 assignment 带 `reason`（截止优先 / 优先级 / 大段空闲 /
   风险缓冲 / 保持原位）；前端悬停只显示一张自绘卡，并移除 Godot 默认黑底 tooltip。
9. **重扫去重与事件分级（v4.1.7）**：`upsert_schedule()` 的课程键改为
   「日期（或星期）+ 节次 + 标题」，不再带 `source`；命中后合并来源与缺失的教师 / 地点 / 备注。
   启动时 `_dedupe_schedule_locked()` 清理旧版留下的重复行。只有带节次的条目才是课程，
   群公告里的比赛 / 活动按普通约定处理；同一格多个约定在周表合并为「N 项约定」块。
10. **聊天排程（v4.2.0）**：`schedule_chat.py` 先用 `looks_schedulable()` 规则闸门过滤
   明显没有时间 / 事项线索的闲聊（不发起 LLM 调用），再让模型输出
   `{"schedule": bool, "items": [...]}`；`schedule.normalize_items()` 规范化后过滤已彻底过去的事件。
   `main.py` 在 `/api/chat` 回复完成后用**后台任务**（`app.state.chat_capture_tasks`，退出时取消）
   写库 → 按 `auto_plan_enabled` 自动排班 → 写长期记忆 → 推 `ai_schedule`。
   不 await 后台任务，避免长判断拖慢 HTTP 回包、被客户端误判为「没发出去」而重复排队。
   前端 `PetShell` 只在聊天面板打开时把 `ai_schedule` 追加为 assistant 文本；不碰 `_chat_busy`，
   `Main.message_needs_speech("ai_schedule")` 恒为 false。
11. **月历 / 周视图（v4.2.0，纯前端）**：`PetView` 保存一份 `_schedule_items`，按 `_schedule_view`
   重画。月历以周一为第一列、42 个日格；课程按 `date` 只落在它真实的那一天（没有日期的
   旧条目先只在本周显示），普通条目按 `date` / `start_ts` 落到具体日期；点日格
   `show_week_for_date()` 以该周周一为锚点进入周表。周表只收 `date` 落在选中周的课程与
   定时约定（周表不再每周套同一张基底课表）；无日期 / 无星期的条目进入「待安排」。
   前后端职责刻意分开：判断与存储全在后端，日历只消费 `/api/schedule`。

未采用（缺少数据源或收益不足）：数字孪生 / Pareto 预演、手机行为信号触发、夜间消息 / 邮件事实抽取。

---

## 7. 配置系统

只有一份 `config.json`：前端读视觉、语音、VRM、后端地址等；后端读引擎、LLM、提示词、记忆、画像、TTS 与教学开关。

读取优先级：

1. 环境变量 `AZURE_CONFIG` 指向的文件；
2. 当前工作目录 `config.json`；
3. `backend/config.json`（后端）或可执行文件同目录（客户端打包后）。

后端在**启动时**读取配置；改 LLM 地址/Key、KataGo 地址、TTS 模式等需要重启后端。前端设置面板里部分运行时字段（朗读详略、思考档位、定式、情绪、画像、自适应）通过 `/api/config` 即时生效。

### 7.1 关键环境变量

| 变量 | 作用 |
| --- | --- |
| `AZURE_CONFIG` | 指定配置文件 |
| `AZURE_TTS_DIR` | 本地 TTS 目录 |
| `KATAGO_EXE` / `KATAGO_MODEL` / `KATAGO_CONFIG` | KataGo 部署路径 |
| `KATAGO_HOST` / `KATAGO_SERVICE_TOKEN` | KataGo 服务监听地址与令牌 |
| `GODOT_EXE` | Godot 可执行文件 |

---

## 8. 数据与持久化

| 文件 | 位置 | 内容 | 写入时机 |
| --- | --- | --- | --- |
| `memory.db` | `backend/data/` | 长期记忆与可选向量 | 记忆写入/局末 |
| `profile.db` | `backend/data/` | 玩家棋风画像、每局记录、人格化状态（关系 / 情绪余温 / 互动数） | `summarize()`；画像总开关关着时不写画像，人格化仍按 `persona_enabled` 写 persona 表 |
| `games.db` | `backend/data/` | 棋谱库、逐手、总结、注记 | 局末归档 / 导入 |
| `joseki_progress.db` | `backend/data/` | 定式族出现计数 | 定式命中时 |
| `life.db` | `backend/data/` | 生命时间线、慢变量、共享日程（`schedule_items`）与类别耗时统计（`schedule_stats`） | 互动 / 日程写入 / 执行反馈 |
| `thinking_lines.json` | `game/data/` | 思考短句解析工具的数据（当前运行路径不轮播） | 只读 |
| `assets/voice` | `game/assets/` | 27 条预制语音 | 只读 |
| `backend/logs` | `backend/logs/` | 服务日志、PID 文件、TTS 探针 | 运行时 |
| 事件日志 | 内存 | 每局逐手事件、注记 | 对局中，随复盘读取 |

所有 SQLite 存储层都采用 **fail-open**：库被删、只读、损坏时只打印一行日志并返回空结果，不影响对局。

---

## 9. 运行、部署与打包

### 9.1 本机一键开发（`dev_local.ps1`）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File dev_local.ps1          # 起服务 + 游戏
powershell ... -File dev_local.ps1 -NoGame                                 # 只起服务
powershell ... -File dev_local.ps1 -Tests                                  # 跑全量测试
powershell ... -File dev_local.ps1 -Stop                                   # 停止
```

启动顺序：

1. 本机 LLM（8080，`tools/start_llm_server.py`）；
2. 本地 TTS（9880，若找到 `.venv` 与 `azure_tts_server.py`）：`.tts_server.lock` 防重复、轮询 `/health`、就绪后才把 `game/config.json` 的 `tts_mode` 写为 `local`，并重启后端使配置生效；`-QualityTts` 用 CosyVoice（`--cosyvoice 1`），`-NoWaitTts` 时先继续起后端、让 CosyVoice 在后台加载；
3. KataGo 云服务（8100），轮询 `/health` 到 `ready`；
4. 后端（8000），轮询 `/api/health`；
5. Godot 客户端。

客户端从 Godot 编辑器或 `dev_local.ps1` 启动时：若 `auto_start_services=true`、`backend_url` 与 `tts_local_base_url` 都是本机、且源码目录存在 `dev_local.ps1`，会先读一次 TTS `/health`：CosyVoice 已就绪则直接沿用；未就绪则弹窗询问是否等待。选择等待时用 `dev_local.ps1 -QualityTts -NoGame` 拉起并轮询 `/health`（最长 300 秒）；选择不等时用 `-QualityTts -NoWaitTts` 让 CosyVoice 后台加载，先用轻量 / 系统语音，起好后后端下一次合成自动接上。由 `dev_local.ps1` 拉起游戏时设置了 `AZURE_SERVICES_MANAGED=1`，客户端不重复拉起基础服务；但若当前仍是轻量引擎，仍会询问是否切换。无头测试、截图模式和导出包均跳过。

停止时按 PID 文件与端口清理进程。

### 9.2 手动启动

```text
katago_service\run_server.bat      → 127.0.0.1:8100
backend\run_server.bat             → 127.0.0.1:8000
Godot --path game                  → 客户端
```

`run_test_env.bat` 提供全栈、仅服务、全量测试、停止、还原 `project.godot` 五种模式。

### 9.3 模型与资产

仓库**不包含**：`katago.exe` / DLL、KataGo 权重、analysis 配置、CosyVoice 权重、OpenVoice `.pth`、LLM gguf。

仓库**包含**：`Azure.vrm`（约 14MB）、预制语音与音效、全部源码与测试。

本机默认路径（可用环境变量覆盖）：

- `D:\KataGo\bin\katago\katago.exe`
- `D:\KataGo\models\model-b18c384nbt.bin.gz`
- `D:\KataGo\bin\katago\analysis_example.cfg`
- `D:\KataGo\models\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`
- `D:\KataGo\cosyvoice`（CosyVoice 运行环境）

### 9.4 打包

`game/export_presets.cfg` 提供 Windows Desktop 预设，可用 Godot 编辑器导出 PCK 或可执行文件。`config.json` 放在可执行文件同目录即可覆盖包内默认值；`Azure.vrm` 与 exe 并列。

---

## 10. 测试体系

### 10.1 后端

```powershell
cd backend
python -m unittest discover -s tests        # 897 个用例
```

### 10.2 KataGo 服务

```powershell
cd katago_service
python -m unittest discover -s tests        # 36 个用例，假引擎
```

### 10.3 前端 headless

```powershell
godot --path game --headless --script res://tests/test_config.gd
```

或直接 `run_test_env.bat tests`，它会依次跑后端全量、KataGo 服务全量、18 个前端脚本，并做 `--headless --quit` 自检，最后还原 `game/project.godot`。

> 测试脚本可能使用 Godot 4.7.1 运行并改写 `project.godot` 的 `config/features`；`run_test_env.bat tests` 会自动还原，手动跑脚本后如发现工程版本被改，执行 `run_test_env.bat restore`。

---

## 11. 关键设计取舍

1. **后端是唯一编排权威**：前端不内嵌模型、不做对局逻辑，只渲染和播放；本地模型链由 `dev_local.ps1` 代办，客户端只负责触发启动器。这样换前端、做 Web 版或多端时不复制编排。
2. **REST 命令 + WS 事件单向推送**：命令有明确返回值与错误处理，事件只负责广播；避免双向 WS 的请求/响应配对复杂度。
3. **音画同步在客户端完成**：AI 已算好落点，但等「落子宣告」语音开始才 `reveal`，保证「先说话、后落子」。
4. **TTS fail-closed 到系统语音**：合成失败绝不给半截音频、绝不抛异常，防止 `busy` 卡死。
5. **乐观落子 + 权威回滚**：客户端立即显示玩家落子，后端拒绝时用 `move_rejected` 快照撤回幽灵子。
6. **SQLite fail-open**：记忆、画像、棋谱、定式计数任一库损坏都不影响对局。
7. **默认关闭重型功能**：记忆、画像、工具、流式、情绪偏航、自适应默认关，关闭时行为与旧版逐项一致，避免无意增加延迟与费用。
8. **本地优先、云端可选**：默认本地 LLM/TTS；所有云端地址都可替换为任意 OpenAI 兼容服务。
9. **程序化 UI**：只保留一个空场景，UI 全部代码生成，主题与布局集中，便于统一改版；代价是 `Main.gd` 很大。
10. **安全边界**：KataGo 服务默认只监听回环；跨机部署必须配 token 并放在内网/TLS 反代后。

---

## 12. 已知限制与注意事项

- 开发脚本（`dev_local.ps1`、`run_test_env.bat`）是 **Windows / PowerShell 5.1** 专用；其他平台需手动起服务。
- 后端启动时会等待 KataGo 就绪，最长 180 秒；预热期间 8000 端口不监听，属于正常现象。
- 后端配置在启动时读取；改 LLM、KataGo、TTS 端点后需重启后端。
- 界面主题切换需要重启客户端。
- 本地 TTS 冷启动较慢，首次合成可能数十秒；`dev_local.ps1` 会等待 `/health` 就绪。
- 阿里云 TTS 需要 Voice Studio 签发的 Key（`sk-…`），百炼原生 Key 会 401。
- 流式输出默认关闭；开启后仍以最终 `message` 为准。
- 长期记忆的 embedding 三件套任一为空时退化为关键词检索。
- `peek/` 的窥屏脚本由客户端在「长时间无操作且游戏窗口不在前台」时拉起：截图 + RapidOCR + 本机 VLM 后 POST `/api/peer`；缺 Python/模型/服务时自动退回普通走神搭话。
- 仓库没有 CI；测试需本地手动运行。
- `Main.gd`、`agent.py`、`agent_graph.py`、`prompts.py` 体积较大，修改时优先定位函数与阶段，避免全文件重排。
- 自动拉起本地服务只在源码目录、`backend_url` 与 `tts_local_base_url` 均为本机、`auto_start_services=true` 时生效；导出包没有 `dev_local.ps1` 时静默跳过。
- CosyVoice 询问只在上述条件且 CosyVoice 未就绪时出现；已就绪则不再询问。等待超时为 300 秒。
- 无头测试与截图模式不会拉起服务或弹窗。

---

## 13. 扩展与维护指南

### 13.1 新增一个后端能力

1. 在 `backend/app` 对应模块实现逻辑（纯函数优先，便于单测）；
2. 在 `main.py` 增加 REST 端点，保持返回 `{"ok": bool, ...}` 形状；
3. 如需推送，在 `events.py` 增加事件构造器与类型说明；
4. 在 `NetClient.gd` 增加信号与分发；
5. 在 `Main.gd` 接线到 UI；
6. 后端补 `backend/tests/test_*.py`，前端补 `game/tests/test_*.gd`。

### 13.2 新增一个 UI 主题

1. 在 `GufengTheme.PALETTES` 增加色板；
2. 在 `theme_options()` 增加显示名；
3. 在 `AzureConfig.UI_THEMES` 增加 id；
4. 检查面板、气泡、按钮、滑杆、棋盘标签、提示文字在浅色/深色下的对比度；
5. 补配置测试。

### 13.3 新增一个 WS 事件

1. `events.py` 写构造器与字段说明；
2. `main.py` 在合适阶段 `bus.emit`；
3. `NetClient.gd` 增加信号并在 `_handle_packet` 分发；
4. `Main.gd` 消费；
5. 更新本文附录 B 与 README 接口契约。

### 13.4 分支与提交

- 日常开发：`personal`（`AzureGo-Creative`）；
- 历史存档：`classic`（`AzureGo-classic`），只读为主；
- 对外发布：`main` 保持冻结，不直接提交；
- 提交信息建议：`feat(personal): …`、`fix(personal): …`、`docs(personal): …`。

---

## 14. 附录：REST / WS 速查

### 14.1 常用请求

```http
POST /api/move        {"x": 3, "y": 15}
POST /api/chat        {"text": "这手怎么样？"}
POST /api/hint        {}
POST /api/analyze     {}
POST /api/summarize   {"note": "…"}
POST /api/config      {"reply_style": 1, "think_mode": 2}
POST /api/reveal      {}
POST /api/reset       {"silent": true}
GET  /api/games
GET  /api/games/1/sgf
POST /api/review/full/segment  {"game_id": 1, "from_move": 0, "to_move": 50}
```

### 14.2 事件示例

```json
{"type":"message","kind":"ai_move","text":"我下这里。","move":{"x":3,"y":15,"gtp":"Q16","pass":false}}
{"type":"speech","turn_id":"t-12","seq":0,"mode":"openai","id":"a-…"}
{"type":"estimate","komi":7.5,"move_count":42,"lead":3.2,"black":38,"white":35,"cells":[[3,15,"black"]]}
{"type":"joseki","kind":"hit","corner":"upper_right","points":[[15,3]],"family":"…","name":"…","show_card":true}
{"type":"difficulty","level":3,"base":3,"reason":"player_streak"}
```

### 14.3 术语

| 术语 | 含义 |
| --- | --- |
| `busy` | Azure 正在思考/应手，玩家落子会被拒绝或排队 |
| `turn_id` | 一轮发言的标识，用于台词、语音、流式增量配对 |
| `epoch` | 对局代次，用于丢弃旧局迟到的事件 |
| `ownership` | KataGo 逐点归属，正值黑、负值白 |
| `pv` | 预测线（principal variation） |
| `joseki` | 定式 |
| `estimate` | 点目/形势判断 |
| `fail-open` | 存储故障时降级放行，不影响主流程 |
| `fail-closed` | 语音失败时整条降级为系统语音，不返回半截结果 |
