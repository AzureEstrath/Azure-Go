# Azure·围棋陪练 —— LangChain 后端化：决策记录与计划索引

- 日期：2026-10-14
- 基线：`main` @ `51c8d05`（v2.0.1），前后端分离已在 v2.0.0 落地（`backend/` + `game/scripts/net/NetClient.gd`）
- 本文是**决策记录（spec）**：所有实现计划都从本文出发，执行前请先读本文。

---

## 一、已拍板的决策

| # | 决策 | 影响 |
| --- | --- | --- |
| D1 | 后端（Python）是**唯一权威**，前端只做输入与呈现 | 规则、KataGo、发言、记忆、语音合成全在后端；相机/摸头/切窗口留前端 |
| D2 | 采用**方案 B**：工具化 Agent + 长期记忆（B 自带 A：先做 LangChain 地基） | 见计划 02/03/04 |
| D3 | **所有本地模型改为云端**（LLM / embedding / TTS），不再保留本地模型服务 | 删除 llama-server / CosyVoice 的本地编排（`backend/app/services.py`、`Main.gd` 的拉起逻辑、`config.json` 的 `llm_auto_start*`、`vlm_*`、`tts_auto_start`） |
| D4 | **KataGo 也走云端**：自建 KataGo 云服务；**不保留 local transport** | 见计划 01；`engine.py` 由子进程改为 HTTP 客户端 |
| D5 | **去除窥屏功能**（前后端都删） | 删 `POST /api/peer`、`peer_remark`、前端 `_peek_*`、`vlm_*` 配置、`peek/azure_peek.py` 约定 |
| D6 | **删除 local 模式**，`backend_mode` 只保留 remote | 删 Godot 内 `AzureAgent.gd / AzurePrompts.gd / KataGoEngine.gd / LLMClient.gd / GoBoard.gd / GameState.gd / JosekiData.gd / JosekiRecognizer.gd / EmotionModel.gd` 与 `game/tests/test_e2e.gd` |
| D7 | **TTS 合成搬后端放最后一轮**；本轮只把 TTS 配置切云端 `openai` 模式 | 见计划 05 |
| D8 | 分享版（双击 exe 免 Python）**不再维护** | 不再考虑 PyInstaller / 自带解释器 |
| D9 | 后端不可用时前端**直接报错提示**（不再做旧版那种全能力优雅降级） | 后端内部对云服务仍须优雅降级、不得刷屏 |

### 明确不做（非目标）

- 多用户 / 多会话隔离（仍是单人桌面场景，`busy` 是全局单例）。
- 不改 `ai_move` + `POST /api/reveal` 的音画同步机制。
- 不改棋盘规则、定式库、情绪模型的行为。
- 不引入 pytest（测试一律 `unittest`）。

### 不可回退的硬规则（每个计划都必须守住）

1. **音画同步**：`ai_move` 事件带坐标，前端在她“落子宣告”的语音开始播放时才 `POST /api/reveal` 显示棋子。
2. **AI 原话按最近 8 条窗口去重**（`Main.gd:AI_DEDUP_WINDOW`，后端侧在 B3 要等价保留）。
3. **模型推理段永不显示给玩家**（`llm.py:clean_content`）。
4. **落子宣告不参与复读抑制**；**走神搭话可整批作废**。
5. `prompts.context_block` 末尾的「铁律段」——尤其**方位词必须按坐标核对**（同列只能说上下、同行才说左右）——不得删改。
6. 一切外部能力（云 LLM / KataGo 服务）失败都不得崩、不得刷屏；`busy` 锁必须能释放。

---

## 二、目标架构

```
┌──────────────────────┐        REST 命令 + WebSocket 事件        ┌─────────────────────┐
│  Godot 前端（仅显示） │ ───────────────────────────────────────▶ │ Python 后端（权威）  │
│  输入：落子/相机/手势 │ ◀─────────────────────────────────────── │  规则·编排·记忆      │
│  输出：棋子/文字/语音 │                                          └──────────┬──────────┘
└──────────────────────┘                                                     │
                                                                   ┌─────────┴──────────┐
                                                                   ▼                    ▼
                                                        KataGo 云服务(自建)     云 LLM / embedding / TTS
```

- 前端：Godot 4.6 工程 `game/`，只留 `NetClient` + 渲染 + 音频播放。
- 后端：`backend/`（FastAPI），新增 LangChain 编排、工具层、长期记忆。
- KataGo 云服务：新建 `katago_service/`，独立部署在有算力的机器上（计划 01）。

## 三、目标架构 API 清单

**REST 命令**（既有 12 个，契约不变）
`/api/move` `/api/hint` `/api/analyze` `/api/chat` `/api/summarize` `/api/undo` `/api/reset` `/api/config` `/api/reveal` `/api/interaction` `/api/idle` `/api/mood/tick`
**REST 查询**：`GET /api/health`、`GET /api/state`
**REST 删除**：`POST /api/peer`（D5）
**REST 新增**：
| 端点 | 出自 | 说明 |
| --- | --- | --- |
| `POST /api/memory/query` | 计划 03 | `{query,k}` 检索长期记忆 |
| `POST /api/memory/write` | 计划 03 | `{tag,text}` 手动写一条 |
| `GET /api/memory/stats` | 计划 03 | 条数 / 库大小 |
| `POST /api/memory/forget` | 计划 03 | `{ids?}` / `{all:true}` |
| `POST /api/tts/speak` | 计划 05 | `{text,kind,tone}` → `audio/wav` |

**WebSocket `/ws`**（既有 7 类不变）：`hello` `message` `state` `info` `busy` `mood` `joseki`
**新增**：`tool`（工具调用开始/结束，计划 02）、`memory`（记忆写入/召回，计划 03）、`token`（流式，计划 04）、`speech`（语音就绪，计划 05）

**KataGo 云服务内部 API**（计划 01，非游戏契约）
`POST /analyze`（body 即 KataGo analysis 引擎请求原文）、`GET /health`

---

## 四、计划索引与执行顺序

| 顺序 | 计划文件 | 交付物 | 版本 |
| --- | --- | --- | --- |
| 0 | [`plans/00-delocalize-remote-only.md`](plans/00-delocalize-remote-only.md) | 前端不再拉起任何服务；只剩 remote 一条路径；窥屏与本地模型编排全部删除 | v3.0.0 |
| 1 | [`plans/01-katago-cloud-service.md`](plans/01-katago-cloud-service.md) | `katago_service/` 可独立部署；后端打 KataGo 走 HTTP | v3.1.0 |
| 2 | [`plans/02-langchain-foundation-and-tools.md`](plans/02-langchain-foundation-and-tools.md) | LangChain 地基（LCEL）+ 工具层（她自己决定要不要深算/查定式） | v3.2.0 |
| 3 | [`plans/03-long-term-memory.md`](plans/03-long-term-memory.md) | 跨局长记忆（云端 embedding）；开关默认关 | v3.3.0 |
| 4 | [`plans/04-langgraph-agent-loop.md`](plans/04-langgraph-agent-loop.md) | LangGraph 真 agent loop + `tool`/`token` 事件 | v3.4.0 |
| 5 | [`plans/05-tts-synthesis-to-backend.md`](plans/05-tts-synthesis-to-backend.md) | 语音合成搬后端，前端退化为播放器 | v3.5.0 |

**顺序不可调换的原因**：计划 0 与 01 都在改 `backend/app/config.py`、`backend/app/main.py` 的装配面与 `game/config.json` 的字段面；先让地基干净，02~04 才不用在“双模式 + 本地服务”的遗留分支上盖楼。

## 五、需要人准备的外部资源（执行前）

| 资源 | 用途 | 备注 |
| --- | --- | --- |
| 云 LLM（OpenAI 兼容）地址 / 模型名 / API Key | 计划 00 起就依赖 | 填进 `config.json` 的 `llm_base_url / llm_model / llm_api_key` |
| 云 embedding（OpenAI 兼容）地址 / 模型 / Key | 计划 03 | `embed_base_url / embed_model / embed_api_key` |
| 云 TTS（OpenAI 兼容 `/v1/audio/speech`） | 计划 00（配置切换）与 05 | `tts_base_url / tts_model / tts_voice / tts_api_key` |
| 一台能跑 KataGo 的机器（GPU 或强 CPU）+ `katago.exe` + 权重 + `default_gtp.cfg` | 计划 01 | 服务侧自持，不再分发给玩家 |
| 后端 Python 环境 | 全程 | `pip install -r backend/requirements.txt`；当前开发机 `E:\Python` 未装 fastapi |
