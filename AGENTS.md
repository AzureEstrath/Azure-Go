# AGENTS.md

面向在本仓库工作的 OpenCode 会话。Windows 单平台；注释、UI 文案、提交信息一律中文，与现有风格保持一致。

## 仓库边界

3D 围棋陪练：`game/`（Godot 4.6，GDScript）只做渲染 / 输入 / 语音播放；`backend/`（FastAPI，`127.0.0.1:8000`）是对局编排的**唯一权威**；`katago_service/`（FastAPI，`127.0.0.1:8100`）包着常驻的 `katago analysis` 子进程；`server/` 是可选的 CosyVoice 语音服务（不随游戏启动）。只有 LLM（OpenAI 兼容）与语音合成走可配的外部地址，编排与 KataGo 都在本机。

- 前端没有 local 模式：`Main.gd` → `NetClient`（REST 命令 + WS 事件）是唯一装配路径，前端不拉起任何模型服务。
- `game/scenes/Main.tscn` 只挂 `scripts/ui/Main.gd`，其余节点与 UI 全在代码里构建；`game/scripts/` 分 `core`（配置 / 坐标）、`net`、`services`（语音播放 / 配对）、`ui`（渲染与交互）。
- `peek/` 是 v3.x 起**未接线**的遗留窥屏脚本；`tools/` 只剩 `joseki_extract.py`。不要顺手接回去或删掉。
- 根目录没有 CI / lint / formatter / 包管理器；只有 `main` 一条分支。`server/` 里 288/370 个文件是上游 CosyVoice 代码（`server/cosyvoice/`、`server/examples/`、`server/runtime/`、`server/.github/`、`server/README.md`、`server/third_party/Matcha-TTS` 子模块），本项目自己写的只有 `server/azure_tts_server.py` / `clone_voice.py` / `voices.json` / `sherpa_voices.json` / `run_hidden.vbs` / `start_tts_server*.bat`。不要重排上游目录。

## 命令

- **一键**：`run_test_env.bat` 依次起 KataGo + 后端 + 游戏；`backend` 只起服务；`tests` 跑后端 + 云服务 + 全部前端 headless + 自检，并自动还原 `project.godot`；`stop` 停进程；`restore` 还原 `game/project.godot`。脚本自己探测 `KATAGO_EXE/MODEL/CONFIG` 与 `GODOT_EXE`。
- **手动启动顺序**：`katago_service\run_server.bat`（**必须先起，并等 `/health` 的 `state=ready`**）→ `backend\run_server.bat` → Godot 开 `game/`。云服务预热期不监听端口，后端在 lifespan 里等它就绪（最长 180s），看起来像「后端没起来」通常就是这一条。
- 后端测试：`cd backend && python -m unittest discover -s tests`（标准库 `unittest`，不用 pytest；先 `pip install -r backend/requirements.txt`）。单个用例要**在 `backend/tests` 里跑**：`cd backend/tests && python -m unittest test_agent_graph.BusyWatchdogTest`（测试模块用顶层 `from helpers import ...`，写成 `tests.test_...` 会 `ModuleNotFoundError`）。
- 云服务测试：`cd katago_service && python -m unittest discover -s tests`（假引擎，不依赖真实 KataGo / 公网）。
- 前端测试：`godot --path game --headless --script res://tests/test_xxx.gd`，`game/tests/test_*.gd` 各脚本自跑自判；不必逐个记，`run_test_env.bat tests` 会跑全部。
- 局部改动先跑对应门卫（全量见 `tests`）：提示词 / 记忆 / 画像 → `test_prompts*.py`、`test_memory.py`、`test_profile*.py`（golden 在 `backend/tests/golden/prompts.json`）；编排 / busy → `test_agent*.py`；点目 / 形势 → `test_estimate.py`、`test_agent_graph.py::AnalyzeEstimateTest`、`test_estimate.gd`；新局入口 → `test_new_game.gd`、`test_title_screen.gd`；定式 → `test_joseki*.py` + `test_joseki_card.gd` / `test_joseki_level.gd`；复盘 → `test_main.py` + `test_review_mode.gd`；语音 → `test_tts*.py`、`test_speech_text.py`、`test_tts.gd`、`test_speech_pairing.gd`；前端配置 → `test_config.gd`。
- 新增 / 移动资源后先 `godot --headless --path game --import`。
- 调试参数必须放在 `--` 之后：`-- --shot=out.png`（截图退出）、`--demo`、`--setup`、`--title`。
- 本机若只有 Godot 4.7.1：跑完 `git status`，把 `game/project.godot` 的 `config/features`（工程按 4.6 配置）、`*.import`、`*.uid` 等任务外的改动还原。`restore` 只还原 `project.godot`，`*.import` / `*.uid` 要手动看。

## 改哪儿

- `backend/app/main.py`：REST + WS 端点与启动路径；`agent.py`：对外 API / 情绪 / 台词 / 看门狗；`agent_graph.py`：LangGraph 编排（文件的头注释就是这份契约）；`prompts.py` + `prompt_chains.py`：提示词装配；`tools.py`：发言侧工具；`memory*.py` / `joseki*.py` / `profile*.py` / `time_ctx.py` / `pv_lines.py` / `event_log.py` / `decay.py` / `tts*.py` / `speech_text.py` / `state.py` / `board.py` / `engine.py` 各管一层。
- 状态图拓扑：`prepare → decide →（条件边 tools → decide，可反复）→ speak → finalize`。六个需要 LLM 编排的公开方法（`player_move` / `hint` / `analyze_position` / `chat` / `idle_remark` / `summarize`）只做「构造状态 → 跑图」；`player_move` 一次公开调用＝三次 `ainvoke`（`after_player → own_turn → just_moved`），跨阶段数据经 `TurnState["data"]` 透传。其余六个同步方法（`undo` / `reset_game` / `set_allow_pass` / `reveal_move` / `tick_mood` / `player_interaction`）直接发事件。
- LLM 走 LangChain：`llm_chain.py` 是 `ChatOpenAI` 的唯一装配点，`llm.py` 只是保留 `chat()` 契约的薄适配器。`llm_extra_body` / `llm_think_extra_body` 是透传给服务商的扩展字段（后者只在 `llm_think_mode > 0` 时并入）；输出上限按线上键 `max_tokens` 发送，预算＝台词预算＋推理余量 `REASON_HEADROOM_DEFAULT`（推理 token 也计入，没余量时服务端在推理段就烧光上限 ⇒ 空 content + `finish_reason=length`）。空内容且被截断、或服务商 400/422 拒参时，去掉扩展字段重试一次——这两种降级是设计好的，不是故障。

## 不能回退的规则

### busy / 超时 / 代际

- `busy` 由调用图的公开方法用 `try/finally` 拥有；节点里每一个 `_set_busy(False)` 的位置**按阶段编码，一个都不许挪**（挪了会让 `busy_seq` 或「chat 先解锁再发话」回退）。改图必守 `agent_graph.py` 文件头的约定。
- 超时只由 `agent._turn_guard`（`WATCHDOG_SEC`）负责：解锁 + `begin_epoch()` 前推一代 + 一条可读提示，**不取消**在途任务（取消是协作式的）。`agent_graph` 里每个写 `st.*` / 发事件的位置之前都过 `_alive()`，旧代的棋谱 / 快照 / 逐手存档 / 事件 / `message`+`speech` / 定式计数全部作废。`undo` / `reset_game` 开头各开新一代。
- 任何外部能力（LLM / TTS / KataGo / 记忆）失败都不得让 `busy` 卡死——不归位＝棋盘永久锁死，这是本项目最严重的故障形态。
- 逐手存档 `st.move_notes`（她的用意 `kind="aim"`、点评与感想 `kind="review"`）只 `append`，不滚动 / 不截断 / 不发事件；唯一收缩口是 `undo`（按 `move_no` 裁剪，与快照回滚同义）。

### 发言与语音

- 音画同步：`ai_move` 带坐标，前端等「落子宣告」的语音**开始播放**时才 `POST /api/reveal` 揭示棋子；`reveal` 时机不许改。
- 后端每发一条要念的台词都紧接一条 `speech`（`turn_id` + 同回合从 1 递增的 `seq`），音频经 `GET /api/tts/audio/{id}` 取回；清洗 / 分句 / 合成 / LRU 缓存 / 降级全在后端（`speech_text.py` / `tts.py` / `tts_aliyun.py`），前端 `TTSClient.gd` 只是播放器，配对交给 `SpeechPairing.gd`。
- `SpeechPairing` 的不变量：任何已登记、且不属于 `tomb` / `forget_idle` 的台词最终一定显示一次。玩家落子打断语音时，`TTSClient.stop()` 与在途取音频（`_speech_fetch`）两条路径都必须 `finish + display`；留下 `got && !played` 就会锁死棋盘。
- 前端棋盘锁 `Main.review_lock_state` 只看三件事：`busy`、`move_pending`、`pending_reveal()`。`_speech_hold` 只挡四个功能按钮与复盘入口，**不许加回 `locked`**。
- 台词一律经 `clean_reply()` / `content_of()`，推理段永不显示；`AI_DEDUP_WINDOW` 去重闸门在 `finalize`，落子宣告不参与复读抑制，走神搭话可整批作废；提示词里不放可被模型照抄的示例句。`tts_enabled=false` ⇒ 零请求、零 `speech`，文字照常。

### 提示词

- `context_block` 顺序：长期记忆 → 事件区块 → 时间块 → 画像块 → 铁律段。新内容只能插在**铁律段之前**，铁律段本身不许动。
- 新增区块为空时必须**逐字符**回到原样：`backend/tests/golden/prompts.json` 锁定 `context_block`；画像 / 时间两个开关都关 ⇒ 与改动前逐字符一致。
- `MAX_LLM_CALLS_PER_TURN = 4`、`AI_DEDUP_WINDOW = 8` 不许放宽；新功能并进现有提示词，不在回合内新增调用。

### 点目 / 形势（v3.12.0）

- `estimate` 事件是**棋盘注记**：在 `_prepare_analyze` 里**复用那一次**分析发出（LLM 之前，所以与她的思考同步），**不得新增引擎调用**；不发 `speech`、不进提示词 / 事件表、不碰 `busy`，引擎失败一条都不发。
- 过滤只在后端 `app/estimate.py` 一处：`|ownership| ≥ 0.5` 才标、`index = y*19+x`（`y=0` 顶行，不翻转）、**活子跳过、被点死的对手子保留**；前端 `Board3D.set_estimate` 只负责画 `cells`，脏格子跳过。
- 收起由前端负责（玩家下一手 / 手数变了 / 回封面 / 进复盘 / 点「关闭形势」）；后端**不补发**「收起」事件。

### 定式

- 识别 `joseki.py` 纯本地、零引擎调用；只在**进入**（`k == len(seq) == 2`）、**分岔**、**收尾**出声，中段推进返回 `{}`（沉默，走普通点评）。
- 台词与卡片同走 `joseki_gate.py` 门控（资格层 + 频率层），未命中与「没识别到定式」逐字段同构；走错绕过频率层与冷却、必说；门控只决定文本 / 卡片 / 情绪，不碰棋子，也不碰 `message`+`speech` 的配对与 `busy`。
- 库线那一手不判走错（`is_joseki_mistake` 的护栏：真亏 **且** 偏离库线）；收尾并进 `just_moved` 的发言位（不是落子宣告）；族计数只在真的讲了时 `bump`，`joseki_progress.py` 是唯一碰盘点，同步调用一律 `asyncio.to_thread`，存储故障只打日志。

### 复盘

- `review` 是后端独立模式（`agent.review` + `/api/review`、`/api/review/goto`、`/api/review/notes`）。落子 / 聊天 / 闲置 / 提示 / 分析五个入口先问 `review_gate()`；拒绝形状与 `busy` **刻意不同**（`{"ok":false,"error":"复盘模式中"}` vs `{"ok":false,"busy":true}`），两种拒绝都不碰 `busy`。
- 复盘期间静音（情绪不衰减、不发 `mood`、走神搭话作废）；`goto` 是只读重放，只推 `state`，不改 `st`、不碰 `st.finished`；后端退出不推 `state`，前端自己拉回终局。进入前先过 `Main.review_can_enter`。

### 记忆 / 画像 / 事件表

- 长期记忆默认关，关时零开销且提示词逐字符不变。同步记忆调用**不得直接放进 `async def`**，必须 `await asyncio.to_thread(...)`（单 uvicorn 事件循环会被慢 embedding 冻住）。召回两级：向量命中不足时用关键词补齐。
- 画像 / 时间默认关；唯一写点是 `summarize()`，一律 `await asyncio.to_thread(...)`，任何故障只打日志、`busy` 必归位。无信号的画像维不产出。主动约战提醒只读 `profile_store.last_game_ts()`，不受 `time_aware_enabled` 门控，但与「有没有历史记录」隐式耦合。
- 预测线 A/B/C 只吃**既有那一次** `after` 分析的 `moveInfos`（含 `pv`），**不得新增任何引擎分析**；B/C 缺失 / 超限时逐级回落 `c → b → a`，最终必是 A，走 B/C 才在落点定案后记 `ai_bad`。
- 事件表存储完整（只增不减、去重、不随悔棋回退），注入有界（衰减窗口 + 字符预算，超限丢【最旧 → 最低档】）；事件原文**永不进台词**（不进 `message`、不产生 `speech`）。事件区块为空必须逐字符不变。
- `backend/data/` 下的 `memory.db` / `profile.db` / `joseki_progress.db` 都是运行产物（已 gitignore）：不要提交、不要当测试夹具。

### 主界面 / 运镜

- 封面与运镜期间必须 `review_lock_state(..., menu_open=true)`（棋盘 / 四个按钮 / 聊天 / 回档一起冻），`_process` 的 hold 与主动搭话条件都要算上 `_title_open`；运镜中 `_transition=true` 防重入，**入场结束才**把 `_title_open` 置假。进出场只在 `_play_enter_game` / `_play_exit_to_menu` 两处编排，退场严格反向。
- **每次进对局都是新的一局**：首次启动由 `_start_launch_game()` 静默 reset（后端是独立进程，可能还留着上次运行的对局）；从主界面回来由 `_on_title_primary` 在运镜前调 `_begin_new_game_on_entry()`（本地清棋盘 / 手数 / 胜率 / 目差 / 俗手 / 复盘入口 → `POST /api/reset {"silent": true}`），主按钮是「开始新对局」。`/api/reset` **忙时会被后端拒绝**，所以忙就先记 `_restart_when_idle`、等 `_on_busy_changed(false)` 补发——只发一次会让「她思考时回主界面再进来」静默保留上一局。
- `TitleScreen._ready` 用 `set_anchors_and_offsets_preset`（`set_anchors_preset` 会把纸幕算成 0×0）；主界面首帧用 `board.snap_panels(false, false)`；设置面板与备注框的遮罩节点必须叫 `ModalVeil`（`_modal_open()` 据此让 Esc 不抢键盘）。
- 无窗口 / `--shot` / `--demo` / `--setup` 一律不等封面（只有 `--title` 强制画），否则 headless 会卡死。

## 坑

- **clone 下来跑不起来是正常的**：`katago.exe` / `*.dll`、`model.bin`、`Azure.vrm`、CosyVoice 权重都不入库（见 `.gitignore` 与 README「模型与资产」，走 GitHub Releases）。不要补齐、生成或提交这些大文件。
- 配置读取顺序：可执行文件同目录 `config.json` → `res://config.json`。设置面板会写回 `config.json`；CLI `--llm/--model/--key/--backend-url` 记进 `cli_locked`、只影响本次运行，改配置逻辑别破坏这个区分。
- `game/config.json` 只是随包模板，**不要拿它当字段默认值来源**：以 `AzureConfig.gd` 的代码默认值为准，字段清单看 README 第三节。
- 后端配置走环境变量 `AZURE_CONFIG`；`backend\run_server.bat` 依次找 `..\AzureGoGodot\config.json`、`..\game\config.json`。`katago_*` 只有服务地址 / 令牌 / 超时，**不要**再把引擎路径解析加回来。
- KataGo 健康语义要成对读：`dead`＝引擎没了（启停探活失败或传输层失败），分析超时与 HTTP 状态响应只刷 `katago_error`。
- 一切外部能力必须优雅降级：缺后端 / 缺 KataGo / 缺 LLM / 缺 TTS 都不得崩、不得刷屏，后端错误必须可读且 `busy` 归位。
- 日志不得打挂回合：`main.py` 启动路径做一次带守卫的 `sys.stdout.reconfigure(errors="replace")`（只改 `errors`）；不要去改 `run_server.bat` 的 `PYTHONIOENCODING`。
- 坐标语义：内部 `Vector2i(x, y)` 中 x=列、y=行，`Coords.gd` 与 GTP 一致（A..T 跳过 I，行号上大下小）。给玩家写方位词前必须按坐标核对：同列只能说「上下」，同行才能说「左右」，核不上就不说方位。
- 场景引用带 `uid://`，`*.gd.uid` 与 `*.import` 已入库：移动 / 重命名脚本或资源时同步更新它们，不要手写 uid。
- `vrm_path` 绝对路径直接用；相对路径依次找 exe 同目录 → `game/` → 仓库根 → 工作目录（发布包把 `Azure.vrm` 放 exe 旁边即可）。
- 「说多少 / 想多久」是运行期设置：`reply_style`（0/1/2）与 `think_mode`（0/1/2）都能经 `/api/config` 即时改；`tts_limit_len` 只是 `reply_style == 0` 的旧键别名，不是独立字段。新增长度约束只能加在系统消息末尾（`prompt_chains` 的 `len_note`）。
- 思考中短句只走 `Main.parse_thinking_lines`（`JSON.new().parse()` 静默失败），文案只改 `game/data/thinking_lines.json`，不要在 `Main.gd` 里写死句子。
- 发言侧工具默认关（`tools_enabled=false`，关时行为与没有工具完全一致），只在 `hint()` 与落子宣告启用；工具次数有硬上限、单工具 10 秒超时；工具输出只回模型，`tool` WS 事件只喂前端状态栏，**永不进台词**；候选落点必须过 `board.is_legal(...)`，任何情况下都要产出可用落点。
- 运行游戏：`Godot_v4.6.3-stable_win64.exe --path game`（工程按 Godot 4.6 配置）。
  - 本机若只有 4.7（`E:\Godot\Godot_v4.7.1-stable_win64_console.exe`），跑完务必 `git status` 检查，把 `game/project.godot` 的 `config/features`、`*.import`、`*.uid` 等任务意图之外的改动还原。
- 启动后端：`backend\run_server.bat`（内部 `cd` 到 `backend\` 再 `python -m app.main`，监听 `127.0.0.1:8000`）；等价的手动命令是 `cd backend && python -m app.main`。
- 启动 KataGo 云服务：`katago_service\run_server.bat`（脚本自己 `cd` 到**仓库根目录**再 `python -m katago_service.server`，**必须原样跑**；监听 `127.0.0.1:8100`）。等价的手动命令在仓库根目录执行：`python -m katago_service.server --port 8100 --katago-exe <exe> --model <bin> --config <cfg> [--analysis-threads N]`；参数留空由环境变量兜底：`KATAGO_HOST` / `KATAGO_SERVICE_TOKEN` / `KATAGO_EXE` / `KATAGO_MODEL` / `KATAGO_CONFIG`（直接执行与经 `run_server.bat` 都生效）。后端要连它（`katago_url`，默认 `http://127.0.0.1:8100`），服务没起时后端不崩、只报「云服务不可用」。注意启动顺序：先起服务并等 `/health` 的 `state` 变 `ready` 再起后端——后端在 lifespan 里会等它就绪（最长 180s）才绑端口，预热期启动的后端在就绪前不监听任何端口（看起来像没起来）。
- 测试：
  - `cd backend && python -m unittest discover -s tests` —— 后端全量（标准库 `unittest`，不用 pytest；先 `pip install -r backend/requirements.txt`）。
  - `cd katago_service && python -m unittest discover -s tests` —— KataGo 云服务全量（同样 `unittest`，用假引擎，不依赖真实 KataGo 与公网）。
  - `godot --path game --headless --script res://tests/test_tts.gd` —— 语音播放器（WAV / MP3 解析、队列上限、系统语音指令），不需要外部服务（会真的拉起一次系统语音进程，音量已压到最低）。
  - `godot --path game --headless --script res://tests/test_speech_pairing.gd` —— 语音配对状态机（不变量 / TTL / 兜底），纯逻辑、无外部依赖。
  - `godot --path game --headless --script res://tests/test_config.gd` —— 客户端配置读写（含未知键原样保留、`joseki_rate` 口径）。
  - `godot --path game --headless --script res://tests/test_joseki_card.gd` —— 定式参考图卡片状态机（事件 / 避让 / 兜底 TTL / 合法棋子数），纯逻辑、不依赖场景树。
  - `godot --path game --headless --script res://tests/test_review_mode.gd` —— 复盘冻结范围 / 翻页夹取 / 逐手存档展示，纯逻辑。
  - `godot --path game --headless --script res://tests/test_view_helpers.gd` —— 视角便捷（回初始 / 四角）、悬停文案（坐标 + 手数三分支）与主界面运镜（`fly_ease` / 远看取景 / `fly_home`），纯逻辑。
  - `godot --path game --headless --script res://tests/test_score_phrase.gd` —— 目差量级人话（分档 / 两信号打架取保守档 / 原始数字保留），纯逻辑。
  - `godot --path game --headless --script res://tests/test_joseki_level.gd` —— 定式导学档位纯函数（关闭 + 四档频率 / 并列取中 / 越界与脏值不越界），纯逻辑。
  - `godot --path game --headless --script res://tests/test_net_chat.gd` / `res://tests/test_chat_queue.gd` —— 聊天回包判定 / 忙时排队 FIFO 与上限，纯逻辑。
  - `godot --path game --headless --script res://tests/test_thinking_lines.gd` —— 思考中短句（JSON 解析 / 20 句 / 轮换不连复 / 缺表回退）。
  - `godot --path game --headless --script res://tests/test_title_screen.gd` —— 主界面（纸幕挡点击 / 三键信号 / 开始⇄开始新对局 / 显隐）。
  - `godot --path game --headless --script res://tests/test_new_game.gd` —— 每次进对局都重开 / 空局侧栏文案，纯逻辑。
  - `godot --path game --headless --script res://tests/test_estimate.gd` —— 局面点目文案 / 脏值安全 / 棋盘标记层。
  - `godot --path game --headless --script res://tests/test_avatar_path.gd` —— 化身 VRM 相对路径候选与解析（不加载模型）。
- 新增/移动资源后先 `godot --headless --path game --import` 重新生成导入信息。
- 调试参数走 `OS.get_cmdline_user_args()`，**必须放在 `--` 之后**：`-- --shot=out.png`（渲染数帧后截图退出）、`-- --demo`、`-- --setup`（只画设置面板供截图）、`-- --title`（强制只画主界面纸幕封面供截图）。

## 约束与坑

- **仓库 clone 下来跑不起来是正常的**：`katago.exe`/`*.dll`、`model*.bin(.gz)`（这两类归 **KataGo 云服务部署侧**持有）、CosyVoice / OpenVoice 权重与云端 LLM / VLM 模型都不入库（见 `.gitignore` 与 README「模型与资产」，走 GitHub Releases）。不要试图补齐、生成或提交这些大文件。**例外**：人物模型 `Azure.vrm`（约 14MB）自 v3.12.2 起入库（仓库根），不要删除或忽略。
- 配置读取顺序：**可执行文件同目录 `config.json` → `res://config.json`**；`game/config.json` 只是随包模板。游戏内设置面板会**写回** config.json，而 CLI `--llm/--model/--key/--backend-url` 被记进 `cli_locked`、只影响本次运行——改配置逻辑时别破坏这个区分。
- 后端配置走环境变量 `AZURE_CONFIG`；未设置时 `backend/app/config.py` 的 `load` 依次找 `<当前工作目录>/config.json` → `backend/config.json`（`backend\run_server.bat` 会先把 `..\AzureGoGodot\config.json` 写进 `AZURE_CONFIG`，该文件不存在时才用 `..\game\config.json`）。`katago_*` 只存**服务地址 / 令牌 / 超时**，引擎路径的解析逻辑（`_resolve`）已随服务化删除，不要再加回来。
- KataGo 服务的健康语义：`/api/health` 的 `katago_dead` 反映**引擎已经没了**——`start()` 探活失败、或分析时**传输层失败**（连接被拒 / 连接或读取错误）都置位，一次成功分析会清除它；**分析**超时与 HTTP 状态响应（503 预热 / 401 / 500）只刷新 `katago_error`、**不**置 `katago_dead`——两者要成对读。
- 不要拿 `game/config.json` 当字段默认值来源：模板（随包样例：`max_visits=100`、`tts_enabled=true`、`tts_pitch=1.02`、`tts_volume=0.65`）与 `AzureConfig.gd` 的代码默认值并不一致，这是既有且有意的。以 `AzureConfig.gd` 为准，字段清单看 README 第三节。
- `vrm_path` 的相对值（默认 `Azure.vrm`）会依次在 **exe 同目录 → `game/` → 仓库根 → 工作目录** 找，取第一个存在的文件；发布包把 `Azure.vrm` 与 exe 放一起即可。
- 场景引用带 `uid://`（`Main.tscn` 的 ext_resource），`*.gd.uid` 与 `*.import` 已入库：移动/重命名脚本或资源时同步更新它们，不要手写 uid。
- **一切外部能力必须优雅降级**（缺后端、缺 KataGo、缺云端 LLM / TTS 都不得崩、不得报错刷屏）：后端返回可读错误且 `busy` 必须归位（不归位＝棋盘永久锁死，本项目最严重的故障形态）；前端连不上后端只在状态栏提示；语音服务不可用时临时降级为系统语音但不改配置。
- **日志不得把整回合打挂**：`backend/app/main.py` 的启动路径做一次**带守卫**的 `sys.stdout.reconfigure(errors="replace")`（`try/except` + `hasattr`；**只改 `errors`、不改编码**）——中文 Windows 控制台 / 重定向文件是 cp936（strict），而 `llm.py` 打服务商错误体、`profile_store.py` / `joseki_progress.py` / `memory.py` 的 `print("…%s" % e)` 都可能在回合链路上抛 `UnicodeEncodeError`，降级成 `?` 好过打挂回合（也覆盖去重命中时打台词原文那条）。不要去改 `run_server.bat` 的 `PYTHONIOENCODING`。
- **长期记忆库是运行产物**：默认落在 `backend/data/memory.db`（`backend/data/` 已 gitignore，不要提交、不要拿它当测试夹具），且**只在 `memory_enabled=true` 时**才会被创建。改 `prompts.context_block` 时必须保持「`long_term` 为空串时输出**逐字符不变**」（`tests/golden/prompts.json` 锁定）：新内容只能插在铁律段**之前**，铁律段本身不许动。
- 坐标语义：内部 `Vector2i(x, y)` 中 x=列、y=行，`Coords.gd` 与 GTP 一致（A..T 跳过 I，行号上大下小）。给玩家写方位词前必须按坐标核对：同列只能说「上下」，同行才能说「左右」，核不上就不说方位（README v1.5.0 的硬性要求）。
- 发言链路有几条不能回退的硬规则：**音画同步**（`ai_move` 事件带坐标，客户端等「落子宣告」的语音开始播放才揭示棋子，见 `backend/app/agent.py` 文件头与 `POST /api/reveal`）、AI 原话按最近 8 条窗口去重（后端常量 `AI_DEDUP_WINDOW = 8`，见 `backend/app/agent.py`，闸门在 `agent_graph` 的 `finalize`；前端 `Main.gd` 的同名常量只兜 WS 重连乱序）、模型推理段永不显示给玩家、落子宣告不参与复读抑制、走神搭话可整批作废。改 `backend/app/agent.py` / `agent_graph.py` / `prompts.py` 时保留这些前提（台词一律经 `clean_reply()` / `content_of()`），也别在提示词里放可被模型照抄的示例句。
- **「说多少 / 想多久」是运行期设置**（v3.11.1）：`reply_style`（0 精简 / 1 适中 / 2 详细，走提示词末尾的长度约束）与 `think_mode`（0/1/2，走推理预算 + `llm_think_extra_body`）都能经 `/api/config` 即时改；`tts_limit_len` 只是 `reply_style == 0` 的**旧键别名**，`agent.limit_len` 也是属性（读写映射到 0 / 1），别再把它写成独立字段。新增长度约束只能加在**系统消息末尾**（`prompt_chains` 的 `len_note`），人设段与铁律段不许动。
- **棋盘锁（前端 `review_lock_state`）只看三件事**：`busy`（后端这一轮在跑）、`move_pending`（点下就锁）、`pending_reveal()`（她的落子还没揭示）。**绝不能把 `_speech_hold` 加回 `locked`**——那会让玩家必须听完她整段话才能落子（v3.11.1 明确修掉）；`_speech_hold` 只挡四个功能按钮与复盘入口。玩家落子时 `_interrupt_speech_for_move()` 会停播并**收账**：`TTSClient.stop()` 负责在播 / 排队的，`_speech_fetch` + `_speech_fetch_current` 负责在途取音频的——两条路径都必须 `finish + display`，否则留下 `got && !played` 就会锁死棋盘。
- **主界面（`title_screen` / `TitleScreen.gd`，v3.11.2 / 运镜 v3.11.3）**：启动先过封面再进设置面板 / 起服务（`_ready` 里 `await _maybe_show_title()`），对局中 `Esc` 经 `_menu_keys()` 开合。**入场**＝封面淡出 → `board.fly_home()` → 右侧面板滑入，**退场严格反向**；两个方向只在 `_play_enter_game` / `_play_exit_to_menu` 两处编排。**五条不能回退的规则**：① 封面 / 运镜期间必须 `review_lock_state(..., menu_open=true)`（棋盘 / 四个按钮 / 聊天 / 回档与开关一起冻），`_process` 的 `hold` 与主动搭话条件都要算上 `_title_open`；② 运镜期间 `_transition=true`（Esc 不叠加、按钮不重入），**入场结束才**把 `_title_open` 置假（棋盘全程锁着）；③ `TitleScreen._ready` 用 `set_anchors_and_offsets_preset`，**不要**改回 `set_anchors_preset`（后者会把纸幕算成 0×0）；④ 无窗口 / `--shot` / `--demo` / `--setup` 一律不等待封面（`--title` 才强制画出来），否则 headless 会卡死；⑤ 主界面首帧用 `board.snap_panels(false, false)`（封面期间没有侧栏，棋盘要正对镜头），**不要**留着右侧让位让棋盘偏在一边。`Board3D.set_panels` 的让位现在是平滑过渡（`_step_panel_bias`），封面期间 `board.input_blocked`、飞行中 `is_flying()` 都让 `_unhandled_input` 整体让路（视角键 / 滚轮 / 拖拽不许动开场取景）。设置面板与备注框的遮罩节点必须叫 `ModalVeil`（`_modal_open()` 据此让 Esc 不抢键盘）。
- **思考中短句（`game/data/thinking_lines.json`，v3.11.2）**：气泡里每秒换一句，随机且不连复；解析只走 `Main.parse_thinking_lines`（`JSON.new().parse()` 静默失败，**不要**换回会喷错误的 `JSON.parse_string`），文件缺失 / 写坏时回退旧的「Azure 思考中…」省略号。改文案只动 JSON，不要在 `Main.gd` 里再写死句子。

## 提交

沿用 Conventional 风格 + 中文描述 + 版本号结尾，例如：

```
fix(chat,ai): 非相邻原样复读窗口去重 + 方位词按坐标核对（v1.5.0）
```

版本号同时写进 `README.md` 的「九、更新」（当前 v3.12.2）。

## 参考

- `README.md`：配置字段（第三节）、语音链路（第五节）、更新日志（第九节）。
- `docs/plans/`：远端化 / KataGo 服务化 / LangChain / 记忆 / 状态图 / 语音后端化等架构决策；`docs/superpowers/specs/`：定式导学、画像与时间、预测线与事件表的设计约束。
