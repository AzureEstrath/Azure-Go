# AGENTS.md

面向在本仓库工作的 OpenCode 会话。Windows 单平台项目；注释、UI 文案、提交信息一律用中文，与现有风格保持一致。

## 是什么

3D 围棋陪练：Godot 4 前端 + 远程后端（Python / FastAPI）+ KataGo 云服务（Python / FastAPI）+ 云端模型（OpenAI 兼容的 LLM 与语音 TTS）。

- `game/` —— 游戏本体（GDScript）。`scenes/Main.tscn` 只挂了 `scripts/ui/Main.gd`，其余节点与 UI 全在代码里构建；`scripts/` 按 `core`（配置/坐标）、`net`（后端客户端）、`services`（TTS 客户端）、`ui`（渲染与交互）分类。
- `backend/` —— **唯一权威**：对局编排、定式识别、情绪模型、提示词都在这里，通过 REST + WebSocket 对外；KataGo 只是它连的一个 HTTP 服务（`katago_url`），`game/` 只连它的 `backend_url`，自身不含任何编排实现。
- `katago_service/` —— **KataGo 云服务**（独立部署，可先起、也可单独重启）：FastAPI + 常驻 `katago analysis` 子进程，对外 `POST /analyze`（请求/响应都是引擎原文）与 `GET /health`（`warming`/`ready`/`dead`），可选 Bearer 鉴权，启动即后台预热。后端 `app/engine.py` 只是它的客户端；引擎本体与权重归**部署侧**持有，不入库。
- `server/` —— CosyVoice 3 语音服务（可选自建，**不再由游戏拉起**，要自己跑 `start_tts_server*.bat`）。**288/370 个入库文件是上游 CosyVoice 代码**（`server/cosyvoice/`、`examples/`、`runtime/`、`.github/`、根 `README.md` 都是上游内容），本项目自己写的只有 `azure_tts_server.py`、`clone_voice.py`、`voices.json`、`sherpa_voices.json`、`run_hidden.vbs`、`start_tts_server*.bat`。不要顺手重排/美化上游目录。`server/third_party/Matcha-TTS` 是 git submodule。
- 根目录没有 CI / lint / formatter / 包管理器配置，只有 `main` 一条分支；`server/.github/workflows/` 是上游 CosyVoice 的，与本项目无关。

## 架构（v3.0.0 起）

- 前端只有一条装配路径：`Main.gd` → `NetClient`（REST 命令 + WebSocket 事件）。**没有 local 模式**，前端不启动任何模型服务（`OS.create_process` 只剩系统语音那条）。
- 模型全在云端：LLM（`llm_base_url` / `llm_model` / `llm_api_key` / `llm_think_mode`）与语音（`tts_mode=openai` + `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`）；`tts_mode=system` 是 Windows 系统语音兜底。
- 后端 LLM 走 **LangChain**：`app/llm_chain.py` 是 `ChatOpenAI` 的唯一装配点（`llm.py` 只是保留 `chat()` 契约的薄适配器），七阶段提示经 `app/prompt_chains.py` 的 `ChatPromptTemplate` 链构造。`llm_extra_body` / `llm_think_extra_body` 是**透传给服务商的请求体扩展字段**（前者始终并入，后者只在 `llm_think_mode > 0` 时并入）；输出上限按**线上键 `max_tokens`** 发送，服务商拒绝（400/422）时去掉扩展字段重试一次（`_extra_body_unsupported`）——这是设计好的降级，不是故障。
- **发言侧工具**（`app/tools.py`：`katago_analyze` / `joseki_lookup` / `board_query`）**只影响她怎么说，不影响她下哪里**——落点仍由 `agent._pick_ai_move` 决定；`tools_enabled` 默认 `false`（关闭时行为与没有工具完全一致），只在 `hint()` 与 `_ai_turn()` 的落子宣告两处启用，工具次数有硬上限（`max_tool_calls`，配置只能收紧）、单工具 10 秒超时；工具输出只回给模型，`tool` WS 事件只喂前端状态栏，**永不进台词**。
- KataGo 是**独立部署的云服务**（`katago_service/`），后端只连 `katago_url` / `katago_token` / `katago_timeout_ms`，**仓库里只有客户端**：`backend/` 里**不得**再引入拉起 `katago` 子进程的代码——`katago.exe` / 权重 / analysis cfg 由服务侧的 `--katago-exe/--model/--config` 或 `KATAGO_EXE/KATAGO_MODEL/KATAGO_CONFIG` 提供，`game/` 连这几个字段都不读。
- peek（看屏幕搭话）已整体移除（前端 `_peek_*`、`POST /api/peer`、`peer_remark`、VLM 配置都没了）；闲置主动搭话走 `POST /api/idle` + `ai_idle` 事件。
- 分发：`tools/` 下的 PCK 打包脚本已删除，PCK 打包流程不再维护；`game/export_presets.cfg` 保留，可用 Godot 自带导出做个人分发。

## 命令

- 运行游戏：`Godot_v4.6.3-stable_win64.exe --path game`（工程按 Godot 4.6 配置）。
  - 本机若只有 4.7（`E:\Godot\Godot_v4.7.1-stable_win64_console.exe`），跑完务必 `git status` 检查，把 `game/project.godot` 的 `config/features`、`*.import`、`*.uid` 等任务意图之外的改动还原。
- 启动后端：`backend\run_server.bat`（内部 `cd` 到 `backend\` 再 `python -m app.main`，监听 `127.0.0.1:8000`）；等价的手动命令是 `cd backend && python -m app.main`。
- 启动 KataGo 云服务：`katago_service\run_server.bat`（脚本自己 `cd` 到**仓库根目录**再 `python -m katago_service.server`，**必须原样跑**；监听 `127.0.0.1:8100`）。等价的手动命令在仓库根目录执行：`python -m katago_service.server --port 8100 --katago-exe <exe> --model <bin> --config <cfg> [--analysis-threads N]`；参数留空由环境变量兜底：`KATAGO_HOST` / `KATAGO_SERVICE_TOKEN` / `KATAGO_EXE` / `KATAGO_MODEL` / `KATAGO_CONFIG`（直接执行与经 `run_server.bat` 都生效）。后端要连它（`katago_url`，默认 `http://127.0.0.1:8100`），服务没起时后端不崩、只报「云服务不可用」。注意启动顺序：先起服务并等 `/health` 的 `state` 变 `ready` 再起后端——后端在 lifespan 里会等它就绪（最长 180s）才绑端口，预热期启动的后端在就绪前不监听任何端口（看起来像没起来）。
- 测试：
  - `cd backend && python -m unittest discover -s tests` —— 后端全量（标准库 `unittest`，不用 pytest；先 `pip install -r backend/requirements.txt`）。
  - `cd katago_service && python -m unittest discover -s tests` —— KataGo 云服务全量（同样 `unittest`，用假引擎，不依赖真实 KataGo 与公网）。
  - `godot --path game --headless --script res://tests/test_tts.gd` —— 文本清洗 / 服务不可用降级 / WAV 解析，不需要外部服务（会真的拉起一次系统语音进程，音量已压到最低）。
- 新增/移动资源后先 `godot --headless --path game --import` 重新生成导入信息。
- 调试参数走 `OS.get_cmdline_user_args()`，**必须放在 `--` 之后**：`-- --shot=out.png`（渲染数帧后截图退出）、`-- --demo`、`-- --setup`（只画设置面板供截图）。

## 约束与坑

- **仓库 clone 下来跑不起来是正常的**：`katago.exe`/`*.dll`、`model.bin`（这两类归 **KataGo 云服务部署侧**持有）、`Azure.vrm`、CosyVoice 权重都不入库（见 `.gitignore` 与 README「模型与资产」，走 GitHub Releases）。不要试图补齐、生成或提交这些大文件。
- 配置读取顺序：**可执行文件同目录 `config.json` → `res://config.json`**；`game/config.json` 只是随包模板。游戏内设置面板会**写回** config.json，而 CLI `--llm/--model/--key/--backend-url` 被记进 `cli_locked`、只影响本次运行——改配置逻辑时别破坏这个区分。
- 后端配置走环境变量 `AZURE_CONFIG`；未设置时 `backend/app/config.py` 的 `load` 依次找 `<当前工作目录>/config.json` → `backend/config.json`（`backend\run_server.bat` 会先把 `..\AzureGoGodot\config.json` 写进 `AZURE_CONFIG`，该文件不存在时才用 `..\game\config.json`）。`katago_*` 只存**服务地址 / 令牌 / 超时**，引擎路径的解析逻辑（`_resolve`）已随服务化删除，不要再加回来。
- KataGo 服务的健康语义：`/api/health` 的 `katago_dead` 反映**引擎已经没了**——`start()` 探活失败、或分析时**传输层失败**（连接被拒 / 连接或读取错误）都置位，一次成功分析会清除它；**分析**超时与 HTTP 状态响应（503 预热 / 401 / 500）只刷新 `katago_error`、**不**置 `katago_dead`——两者要成对读。
- 不要拿 `game/config.json` 当字段默认值来源：模板（随包样例：`max_visits=100`、`tts_enabled=true`、`tts_pitch=1.02`、`tts_volume=0.65`、`vrm_path` 用相对值）与 `AzureConfig.gd` 的代码默认值并不一致，这是既有且有意的。以 `AzureConfig.gd` 为准，字段清单看 README 第三节。
- `vrm_path` 建议写**绝对路径**（相对路径要能被 Godot 解析到，否则形象加载失败）。
- 场景引用带 `uid://`（`Main.tscn` 的 ext_resource），`*.gd.uid` 与 `*.import` 已入库：移动/重命名脚本或资源时同步更新它们，不要手写 uid。
- **一切外部能力必须优雅降级**（缺后端、缺 KataGo、缺云端 LLM / TTS 都不得崩、不得报错刷屏）：后端返回可读错误且 `busy` 必须归位（不归位＝棋盘永久锁死，本项目最严重的故障形态）；前端连不上后端只在状态栏提示；语音服务不可用时临时降级为系统语音但不改配置。
- 坐标语义：内部 `Vector2i(x, y)` 中 x=列、y=行，`Coords.gd` 与 GTP 一致（A..T 跳过 I，行号上大下小）。给玩家写方位词前必须按坐标核对：同列只能说「上下」，同行才能说「左右」，核不上就不说方位（README v1.5.0 的硬性要求）。
- 发言链路有几条不能回退的硬规则：**音画同步**（`ai_move` 事件带坐标，客户端等「落子宣告」的语音开始播放才揭示棋子，见 `backend/app/agent.py` 文件头与 `POST /api/reveal`）、AI 原话按最近 8 条窗口去重（`Main.gd` 的 `AI_DEDUP_WINDOW`）、模型推理段永不显示给玩家、落子宣告不参与复读抑制、走神搭话可整批作废。改 `backend/app/agent.py` / `prompts.py` 时保留这些前提（台词一律经 `clean_reply()` / `content_of()`），也别在提示词里放可被模型照抄的示例句。

## 提交

沿用现有 Conventional 风格 + 中文描述 + 版本号结尾，例如：

```
fix(chat,ai): 非相邻原样复读窗口去重 + 方位词按坐标核对（v1.5.0）
```

版本号同时写进 `README.md` 的「更新」小节（当前 v3.2.0）。
