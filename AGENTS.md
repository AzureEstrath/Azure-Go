# AGENTS.md

面向在本仓库工作的 OpenCode 会话。Windows 单平台项目；注释、UI 文案、提交信息一律用中文，与现有风格保持一致。

## 是什么

3D 围棋陪练：Godot 4 前端 + **本机后端**（Python / FastAPI，`127.0.0.1:8000`）+ KataGo 云服务（Python / FastAPI）+ 云端模型（**只有 LLM 走云端**，OpenAI 兼容；语音 TTS 的合成本身在**后端进程**里做，`tts_base_url` 指向哪儿就在哪儿，不必然是远端）。

- `game/` —— 游戏本体（GDScript）。`scenes/Main.tscn` 只挂了 `scripts/ui/Main.gd`，其余节点与 UI 全在代码里构建；`scripts/` 按 `core`（配置/坐标）、`net`（后端客户端）、`services`（语音播放器 / 配对状态机）、`ui`（渲染与交互）分类。
- `backend/` —— **唯一权威**：对局编排、定式识别、情绪模型、提示词都在这里，通过 REST + WebSocket 对外；KataGo 只是它连的一个 HTTP 服务（`katago_url`），`game/` 只连它的 `backend_url`，自身不含任何编排实现。
- `katago_service/` —— **KataGo 云服务**（独立部署，可先起、也可单独重启）：FastAPI + 常驻 `katago analysis` 子进程，对外 `POST /analyze`（请求/响应都是引擎原文）与 `GET /health`（`warming`/`ready`/`dead`），可选 Bearer 鉴权，启动即后台预热。后端 `app/engine.py` 只是它的客户端；引擎本体与权重归**部署侧**持有，不入库。
- `server/` —— CosyVoice 3 语音服务（可选自建，**不再由游戏拉起**，要自己跑 `start_tts_server*.bat`）。**288/370 个入库文件是上游 CosyVoice 代码**（`server/cosyvoice/`、`examples/`、`runtime/`、`.github/`、根 `README.md` 都是上游内容），本项目自己写的只有 `azure_tts_server.py`、`clone_voice.py`、`voices.json`、`sherpa_voices.json`、`run_hidden.vbs`、`start_tts_server*.bat`。不要顺手重排/美化上游目录。`server/third_party/Matcha-TTS` 是 git submodule。
- 根目录没有 CI / lint / formatter / 包管理器配置，只有 `main` 一条分支；`server/.github/workflows/` 是上游 CosyVoice 的，与本项目无关。

## 架构（v3.0.0 起）

- 前端只有一条装配路径：`Main.gd` → `NetClient`（REST 命令 + WebSocket 事件）。**没有 local 模式**（说的是前端不内嵌编排，**不是**后端在别的机器上——后端也在这台机器上，`backend/`，`127.0.0.1:8000`），前端不启动任何模型服务（`OS.create_process` 只剩系统语音那条）；只有 LLM 走可自定义的 OpenAI 兼容 API。
- LLM 与语音合成都走可自定义的 OpenAI 兼容 API（**不内置模型**）：LLM（`llm_base_url` / `llm_model` / `llm_api_key` / `llm_think_mode`）与语音（`tts_mode=openai` + `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`，地址指向自建 CosyVoice 时就在本机合成）；`tts_mode=system` 是 Windows 系统语音兜底。
- 后端 LLM 走 **LangChain**：`app/llm_chain.py` 是 `ChatOpenAI` 的唯一装配点（`llm.py` 只是保留 `chat()` 契约的薄适配器），七阶段提示经 `app/prompt_chains.py` 的 `ChatPromptTemplate` 链构造。`llm_extra_body` / `llm_think_extra_body` 是**透传给服务商的请求体扩展字段**（前者始终并入，后者只在 `llm_think_mode > 0` 时并入）；输出上限按**线上键 `max_tokens`** 发送，**预算 = 台词预算 + 推理余量 `REASON_HEADROOM_DEFAULT = 4096`**（推理型模型的推理 token 也计入 `max_tokens`，没有余量时服务端会在推理段就把上限烧光，返回 `finish_reason=length` + 空 content ⇒ 台词退化成兜底句）；空内容且 `finish_reason=length` 时**去掉全部扩展字段重试一次**。服务商拒绝参数（400/422）时去掉扩展字段重试一次（`_extra_body_unsupported`）——这两种降级都是设计好的，不是故障。
- **回合编排在一张状态图里**（`app/agent_graph.py`，LangGraph）：拓扑 `prepare → decide →（条件边 tools → decide，可反复）→ speak → finalize`。六个需要 LLM 编排的公开方法（`player_move` / `hint` / `analyze_position` / `chat` / `idle_remark` / `summarize`）在 `agent.py` 里只做「构造状态 → 跑图」（`player_move` 一次公开调用＝三次 `ainvoke`：`after_player → own_turn → just_moved`，中间数据经 `TurnState["data"]` 透传），其余六个同步方法（`undo` / `reset_game` / `set_allow_pass` / `reveal_move` / `tick_mood` / `player_interaction`）仍是同步方法 + 直接发事件。**改图时必须守住 5 条硬规则与 `busy` 的 `try/finally`**：`busy` 由调用图的公开方法拥有，节点里的 `_set_busy(False)` 位置**按阶段编码、不许随意挪动**（挪了会让 `busy_seq` 或「chat 先解锁再发话」回退）；`decide` 内**总 LLM 调用次数**上限 `MAX_LLM_CALLS_PER_TURN = 4`（含工具轮）。`speak` 阶段还有一条**可选**的 `token` 流式通道（`stream_tokens`，默认关闭，关闭时事件序列与没有它时逐字一致）；去重闸门只对 `finalize` 里发出的台词类 `message` 生效。
- **发言侧工具**（`app/tools.py`：`katago_analyze` / `joseki_lookup` / `board_query`）：`tools_enabled` 默认 `false`（关闭时行为与没有工具完全一致），只在 `hint()`（observe）与落子宣告（own_turn）两处启用——工具请求走**条件边工具轮**（`agent_graph._decide_tool_round` + `tools` 节点）这条**主实现**，`agent._tool_loop` 现仅在「工具开但链不可用」的降级路径被调用（此时等价于 `_reply`）；工具次数有硬上限（`max_tool_calls`，配置只能收紧）、单工具 10 秒超时；工具输出只回给模型，`tool` WS 事件只喂前端状态栏，**永不进台词**。**本计划有意放宽**：工具/模型**可以参与落点候选**（`_candidate_from_text` + `_resolve_ai_move`），三条边界——候选点必须过 `board.is_legal(...)`、`pass` 只在 `allow_pass` 时可用、**任何情况下都产出可用落点**。
- **长期记忆**（`app/memory.py` + `app/memory_service.py`）**默认关闭**（`memory_enabled=false`）：关闭时**零开销**——不建库、不调 embedding，且提示词与没有这个功能时**逐字符一致**。开启后**自动**写入点只有 `summarize()` 成功出总结之后（只写总结，兜底台词不写），召回点在 `chat()` 与她自己落子前（`own_turn` 阶段）两处；`embed_*` 任一为空 / 401 / 超时一律降级为关键词检索，任何记忆故障都只打日志、绝不影响对局（`busy` 照常归位）。管理面是 `/api/memory/query|write|stats|forget`（关闭时查询面可用、写入被拒），写 / 查推 `memory` WS 事件——**纯元数据**，前端只记一行日志；agent 自己的召回 / 写入**不发**事件。两条不能回退的硬规则：**同步记忆调用不得直接放进 `async def`**——`_long_term` / `_remember_game` 与两个管理端点都必须 `await asyncio.to_thread(...)`（单 uvicorn 事件循环会被慢 embedding 冻住，单次上限 `embed_timeout`，默认 10 秒）；**召回是两级**——向量命中不足 `memory_top_k` 条时用关键词补齐未命中的行（无向量的老行 / 写入时 embedding 失败的行不得被有向量的行压掉），`memory_top_k=0` 与显式 `k≤0` 都表示不召回（管理面返回空列表，不兜底成 3）。
- **定式教学（v3.6.0 起主动跟课，v3.7.0 起提示门控）**：识别器 `app/joseki.py` 的 `scan()` 只在**进入**（`k == len(seq) == 2`，含 2 手条目——**优先级高于收尾与分岔**：这一刻就是「进入」，`formed` 只对 `len(条目) ≥ 3` 的完整命中成立，库里的候选照旧填进 `candidates`）、以及**分岔 / 收尾**（分岔＝角内全部吻合到 k 手、第 k+1 手存在两种走法，现实库里落在 index ≥ 3，index 2 的那次由「进入」接手；收尾＝库线走完）出声，`k == len(seq) > 2` 的**中段推进返回 `{}`**（不发事件、不提定式，走普通点评）；`app/agent.py` 的 `joseki_show_card` / `joseki_count_entry` 只决定**卡片**（**与台词同走门控**：`app/joseki_gate.py` 的资格层 + 频率层），族计数落 `app/joseki_progress.py` 的 SQLite（`backend/data/joseki_progress.db`，运行产物、已 gitignore、**没有开关**）；前端卡片 `game/scripts/ui/JosekiCard.gd` 只渲染事件字段，`Main.gd` 只接线。**七条不能回退的硬规则**：① **识别必须纯本地、零引擎调用**——`joseki.py` 只做每手几十次比较、不在热路径上，**不许**为了让识别更准去调 KataGo；② **族计数只在 `app/joseki_progress.py` 一处碰盘**——任何存储故障只打一行日志、绝不影响对局，同步调用一律 `await asyncio.to_thread(...)`（悔棋退回那步走 `run_in_executor`，不阻塞调用方）；③ **库线那一手不判走错**——`is_joseki_mistake` 的护栏，「走错」＝**真亏且偏离库线**，玩家正好走在显示线 `line[k]` 或任一候选线的下一步时恒为 `False`（治「照书下还被骂」）；④ **台词与卡片同走门控**（v3.7.0 口径，取代 v3.6.0 的「台词只按 `kind` 分流」）——结构点（进入 / 分岔 / 收尾）先过**频率层** `p = clamp(rate + 棋力画像 + 情绪, 0, 1)`（画像 / 情绪各 ±10%），**未命中与「没识别到定式」逐字段同构**（不发 `joseki`、不换讲解模板、不上卡片，只点评这一手）；**走错绕过频率层与冷却、必说**；**中段推进仍然不说**（识别器自己沉默，压根走不到门控）；⑤ **收尾并进她那一步的发言位**（`just_moved` 的感想），**不是落子宣告**——落子宣告在 `speak` 阶段就发出、早于 `finalize`，把识别提前到那里会动到「落子宣告 + `speech` 配对」的硬规则链；⑥ **门控只决定文本 / 卡片 / 情绪与事件表文案**——不改棋子、不改 `message` / `speech` 的配对与顺序、**不碰 `busy`**（`jk_teach` 进 `player_good` 判定与事件表文案是「未命中 == 未识别逐字段同构」的必然结果）；⑦ **族计数＝教过几次**——只在**真的讲了**时 `bump`（走错必讲照旧计数），不是「遇到几次」；同族同一局最多计 1 次、悔棋退回那一次；门控的棋力浮动**只读局内信号**（本局玩家每手的 rank + 该族计数），**不读任何持久化画像**。另：`joseki_enter_prompt` 的主语**必须是中性的**（进入那一手可能是她自己下的）。改这一侧要跑的门卫：后端 `tests/test_joseki.py` / `tests/test_joseki_progress.py` / `tests/test_joseki_gate.py`（编排用例在 `tests/test_agent.py` / `tests/test_agent_graph.py`），前端 `tests/test_joseki_card.gd` / `tests/test_joseki_level.gd`。
- **局后只读复盘（v3.7.0）**：后端的**独立模式**（`agent.review` + `main.py` 三个端点 `POST /api/review`（进 / 出）/ `POST /api/review/goto` / `GET /api/review/notes`）。**四条不能回退的硬规则**：① **复盘先于 `busy` 判定**——落子 / 聊天 / 闲置 / 提示 / 分析五个入口都先问 `review_gate()`，命中就回 `{"ok":false,"error":"复盘模式中"}`；**与 `busy` 的拒绝形状刻意不同**（`busy` 回 `{"ok":false,"busy":true,…}`），前端据 `error` / `busy` 分别提示；② **两种拒绝都不碰 `busy`**——复盘期间不得解锁、不得加锁、不得改在途回合（`set_review` 只把进入那一刻的 `busy` 记进 `_review_busy_before` 备查、**不消费**）；③ **静音**——`tick_mood` / 情绪增量在 `review` 时直接返回（不衰减、不发 `mood`），语气词与走神搭话一并作废（前端 `_cancel_idle_speech`）；④ **`goto` 是只读重放**——`review_snapshot(n)` 只重放一份临时盘面并推 `state`，**一个字都不改 `st`**、不碰 `st.finished`（「🏁 结束之后还能继续下」的既有语义不变），所以复盘**可进可出**、退出回到进入前的局面；后端退出**不推** `state`，前端自己 `GET /api/state` 拉回终局。前端冻结范围在纯函数 `Main.review_lock_state` 一处定死、退出逐项恢复；进复盘前先过 `Main.review_can_enter`（在途回合 / 语音未播完时先不进）。门卫：后端 `tests/test_main.py`，前端 `tests/test_review_mode.gd`。
- **超时后必须作废旧代的一切写入（不许只解锁）**：`agent.py` 的 `_turn_guard` 是最外层看门狗（`WATCHDOG_SEC = 1380`，算式写在注释里），超时**只解锁 + 前推一代（`begin_epoch`）+ 一条可读提示**，**不取消**在途任务（取消是协作式的，卡在不可取消等待里的任务照样留半截状态）；`agent_graph` 里**每一个写 `st.*` / 发事件的位置之前**都有一道代数核对 `_alive(agent, data)`（节点入口 + `await` 之后的站点两处），旧那一代的棋谱 / 快照 / 逐手存档 / 事件 / `message`+`speech` / `joseki` 计数全部作废。**不能回退的硬规则**：**既有 `_set_busy(False)` 的位置按阶段编码、一个都不许挪**（挪了会让 `busy_seq` 或「chat 先解锁再发话」回退）；`undo` / `reset_game` 开头各开新一代（玩家新动作把在途旧代一并作废）。门卫：后端 `tests/test_agent_graph.py` 的 `BusyWatchdogTest`。
- **本局逐手存档的两条写入点（用意 / 感想）都必须是纯追加**：`st.move_notes`（她的**用意** `kind="aim"`、玩家点评与她自己的**感想** `kind="review"`）由 `state.note_move()` 只 `append`——**不滚动、不截断、不发事件、不参与去重**（与旁边那份滚动 18 条 / 截 80 字的 `memory_log` 各写各的、互不影响）。写入点：她的用意在 `_speak_own_turn` 一带（`kind="aim"`），玩家点评与她的感想在 `_finalize_after_player` / `_finalize_just_moved`（`kind="review"`）。**悔棋是唯一的收缩口**（`undo` 按 `move_no <= keep` 裁剪 `move_notes` / `losses`，与快照回滚同义）——**不许**把「滚动 / 截断」塞进写入点。门卫：后端 `tests/test_state.py` / `tests/test_main.py`。
- **预测线 / 情绪波动 / 事件表（v3.8.0）**：`app/pv_lines.py` 从**既有那一次** `after` 分析的 `moveInfos`（每条候选自带 `pv` / `winrate` / `scoreLead`）里抠出三条五步预测线 A/B/C（A＝最优候选的首手 + `pv` 前 4 手；B/C 在**前 10 个候选**里**依次**取「与**已选各线**不同手数 ≥ `pv_diverge_min`（默认 2）」且「与 A 胜率差 ≤ `pv_max_drop`（默认 5%）」的候选，凑不齐降级，绝不抛）；`app/agent.py` 的 `_maybe_drift` 在 `emotion_drift=true` 时按 **A 常态 → B 小概率 → C 更小概率** 的阶梯选线（`p_drift = clamp(drift_base + emotion_bias, 0, drift_max)`，**B/C 缺失 / 胜率差超限 / 首手非法逐级回落** `c→b→a`，最终必是 A），走 B/C 记一条 `ai_bad` 事件（**落点定案并提交之后**才写，工具轮重定案 / 回落 A / 回合被作废都不留事件）；`app/event_log.py` 是局内事件表（带时刻 `move_no` + 墙钟 `ts`、只增不减、去重、`reset` 清空），`app/decay.py` 把事件按 `DECAY_SPAN=[1,3,6]` 分四档、超 `DECAY_DROP_AT=12` 不进提示词，`app/prompts.py` 的 `event_block()` 渲染成**事件区块**（插长期记忆段之后、铁律段之前）。**三条不能回退的硬规则**：① **线 A/B/C 只吃既有 `moveInfos`（含 `pv`）、不得新增任何引擎分析**——`pv_lines.extract_lines` 是**零 import 的纯函数**；`engine.analyze(moves, max_visits=None)` 的深值**只给「玩家落子后局面」那一次**调用（取 `max(pv_max_visits, max_visits)`，**只增不减**，绝不把这一次砍浅）；② **事件表存储完整、注入有界**——存储层只增不减、去重、**不随悔棋回退**（`undo` 一字不动；悔棋本身**不写事件**，只裁 `move_notes` / `losses`）；注入层受「衰减窗口 + 字符预算 `EVENT_BLOCK_MAX_CHARS`」双重限制，超限丢【最旧 → 最低档】；③ **事件区块为空必须逐字符不变**（`tests/golden/prompts.json` 锁定 `context_block`，新内容只能插在铁律段之前、铁律段本身不许动）、**事件原文永不进台词**（区块是给模型看的素材，**不得产生 `speech`、不得进 `message`**）。聊天摘要默认走**规则式**（纯函数分段 + 模板摘要、**零额外 LLM 调用**），只有 `chat_digest_llm=true` 才在**局末 `summarize()` / 空闲 `idle_remark()`** 用 LLM 补做（落子 / 聊天回合内零额外调用；失败文案整段与单行两道闸门都挡住）。改这一侧要跑的门卫：后端 `tests/test_pv_lines.py` / `tests/test_event_log.py` / `tests/test_decay.py`（编排用例在 `tests/test_agent.py` / `tests/test_agent_graph.py`），前端 `tests/test_config.gd`。
- **玩家棋风画像 / 时间感知（v3.9.0）**：`app/profile.py`（四维特征 + 指数衰减 EMA 融合，纯函数）+
  `app/profile_store.py`（独立库 `backend/data/profile.db`，运行产物、已 gitignore）+ `app/time_ctx.py`
  （时间事实，纯函数、时钟可注入）；提示词侧 `prompts.time_note()` / `profile_note()` 渲染**时间块 + 画像块**，
  插入顺序 **长期记忆段 → 事件区块 → 时间块 → 画像块 → 铁律段**。**五条不能回退的硬规则**：
  ① **写入只在 `summarize()` 里**——对局记录 + 画像融合的唯一写点；一律 `await asyncio.to_thread(...)`；
  **回合内零新增 LLM / 引擎调用**（`MAX_LLM_CALLS_PER_TURN = 4` 不放宽，进步报告**并进现有总结提示词**、
  不新增调用）；**任何故障只打日志、`busy` 必归位**；② **两块空则 `context_block` 逐字符不变**
  （`tests/golden/prompts.json` 锁定；铁律段不许动；**两个开关都关 ⇒ 与改动前逐字符一致**），超限丢
  「最低置信 / 最旧维度」（画像）与「最旧事实优先」（时间），字符预算 `PROFILE_MAX_CHARS` / `TIME_MAX_CHARS` = 240；
  ③ **时间与画像只在相关时提、禁止把数字当口头禅复述**（「她变话多」反目标，写进块内的说明句）；
  ④ **`profile.db` 只是运行产物**（`backend/data/` 已 gitignore；不要提交、不要当测试夹具；**与 `memory.db`
  解耦**——memory 关着画像照常）；⑤ **无信号的画像维不产出**（不把「不知道」写成 0）；`profile_enabled` /
  `time_aware_enabled` **由客户端在连上时推送**（与既有开关同形）。
  面板的「来源局」＝**全局最近 N 局记录**、**不是每维各自的来源**（`profile` 表没有局 id 列）。
  另：**主动约战提醒**不受 `time_aware_enabled` 门控，但与 `profile_enabled` **隐式耦合**——它只读
  `profile_store.last_game_ts()`（**不检查开关**，store 无条件装配）⇒「**从未开启 / 库里没有历史**
  时」关掉画像 ⇒ 不再写记录 ⇒ 没有历史可判 ⇒ 不会提醒；**若库里已有历史，提醒仍会发**（只与
  「有没有记录」耦合，不与开关本身耦合）。门卫：后端
  `tests/test_profile_store.py` / `tests/test_profile.py` / `tests/test_time_ctx.py`（编排用例在
  `tests/test_agent.py` / `tests/test_main.py`），前端 `tests/test_config.gd`。
- **语音链路（v3.5.0 起）整体在后端**：清洗 / 分句 / 分块 / `[[tone:…]]` 剥离在 `app/speech_text.py`，云端合成 + LRU 缓存（**条数 32 / 8 MiB 双向淘汰**，单条超预算直接不插）+ fail-closed 降级在 `app/tts.py`；后端每发一条要念的台词都**紧接**一条 `speech` 事件（`turn_id` + `seq`，**同回合从 1 递增、跨回合重置**），音频经 `GET /api/tts/audio/{id}` 取回（试听是 `POST /api/tts/speak`）。前端 `TTSClient.gd` 退化为播放器（`play_audio` / `speak_system`），配对与兜底交给 `SpeechPairing.gd`（`Main.gd` 只留胶水）。**四条不能回退的硬规则**：① **清洗规则只在后端一处**——前端 `TTSClient.gd` 不得再出现清洗 / 分句 / 合成；② **`reveal` 时机不许改**——仍在 `ai_move` 那条语音**开始播放**时由前端 `POST /api/reveal`；③ **任何异常都不得让 `busy` 卡死**——合成失败整条降级 `mode:"system"`（文字照常、回合不卡），配对侧不得留下悬空记录（`got && !played` 会恒真 → 棋盘永久锁死）；④ 语音关闭（`tts_enabled=false`）时**零请求、零 `speech` 事件**，文字照常。`SpeechPairing.gd` 的状态机守一条不变量：**任何已登记、且不属于「有意不显示」的两类（`tomb` / `forget_idle`）的台词，最终一定会被显示一次**——无论 `speech` 到没到、配对位是否过 TTL、那一轮是否 `busy`、音频端点回什么。改这两侧时要跑的门卫：后端 `tests/test_tts_backend.py` / `tests/test_speech_text.py` / `tests/test_main.py`，前端 `tests/test_tts.gd` / `tests/test_speech_pairing.gd` / `tests/test_config.gd`。
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
  - `godot --path game --headless --script res://tests/test_tts.gd` —— 语音播放器（WAV / MP3 解析、队列上限、系统语音指令），不需要外部服务（会真的拉起一次系统语音进程，音量已压到最低）。
  - `godot --path game --headless --script res://tests/test_speech_pairing.gd` —— 语音配对状态机（不变量 / TTL / 兜底），纯逻辑、无外部依赖。
  - `godot --path game --headless --script res://tests/test_config.gd` —— 客户端配置读写（含未知键原样保留、`joseki_rate` 口径）。
  - `godot --path game --headless --script res://tests/test_joseki_card.gd` —— 定式参考图卡片状态机（事件 / 避让 / 兜底 TTL / 合法棋子数），纯逻辑、不依赖场景树。
  - `godot --path game --headless --script res://tests/test_review_mode.gd` —— 复盘冻结范围 / 翻页夹取 / 逐手存档展示，纯逻辑。
  - `godot --path game --headless --script res://tests/test_view_helpers.gd` —— 视角便捷（回初始 / 四角）与悬停文案（坐标 + 手数三分支），纯逻辑。
  - `godot --path game --headless --script res://tests/test_score_phrase.gd` —— 目差量级人话（分档 / 两信号打架取保守档 / 原始数字保留），纯逻辑。
  - `godot --path game --headless --script res://tests/test_joseki_level.gd` —— 定式导学档位纯函数（关闭 + 四档频率 / 并列取中 / 越界与脏值不越界），纯逻辑。
  - `godot --path game --headless --script res://tests/test_net_chat.gd` / `res://tests/test_chat_queue.gd` —— 聊天回包判定 / 忙时排队 FIFO 与上限，纯逻辑。
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
- **日志不得把整回合打挂**：`backend/app/main.py` 的启动路径做一次**带守卫**的 `sys.stdout.reconfigure(errors="replace")`（`try/except` + `hasattr`；**只改 `errors`、不改编码**）——中文 Windows 控制台 / 重定向文件是 cp936（strict），而 `llm.py` 打服务商错误体、`profile_store.py` / `joseki_progress.py` / `memory.py` 的 `print("…%s" % e)` 都可能在回合链路上抛 `UnicodeEncodeError`，降级成 `?` 好过打挂回合（也覆盖去重命中时打台词原文那条）。不要去改 `run_server.bat` 的 `PYTHONIOENCODING`。
- **长期记忆库是运行产物**：默认落在 `backend/data/memory.db`（`backend/data/` 已 gitignore，不要提交、不要拿它当测试夹具），且**只在 `memory_enabled=true` 时**才会被创建。改 `prompts.context_block` 时必须保持「`long_term` 为空串时输出**逐字符不变**」（`tests/golden/prompts.json` 锁定）：新内容只能插在铁律段**之前**，铁律段本身不许动。
- 坐标语义：内部 `Vector2i(x, y)` 中 x=列、y=行，`Coords.gd` 与 GTP 一致（A..T 跳过 I，行号上大下小）。给玩家写方位词前必须按坐标核对：同列只能说「上下」，同行才能说「左右」，核不上就不说方位（README v1.5.0 的硬性要求）。
- 发言链路有几条不能回退的硬规则：**音画同步**（`ai_move` 事件带坐标，客户端等「落子宣告」的语音开始播放才揭示棋子，见 `backend/app/agent.py` 文件头与 `POST /api/reveal`）、AI 原话按最近 8 条窗口去重（后端常量 `AI_DEDUP_WINDOW = 8`，见 `backend/app/agent.py`，闸门在 `agent_graph` 的 `finalize`；前端 `Main.gd` 的同名常量只兜 WS 重连乱序）、模型推理段永不显示给玩家、落子宣告不参与复读抑制、走神搭话可整批作废。改 `backend/app/agent.py` / `agent_graph.py` / `prompts.py` 时保留这些前提（台词一律经 `clean_reply()` / `content_of()`），也别在提示词里放可被模型照抄的示例句。

## 提交

沿用现有 Conventional 风格 + 中文描述 + 版本号结尾，例如：

```
fix(chat,ai): 非相邻原样复读窗口去重 + 方位词按坐标核对（v1.5.0）
```

版本号同时写进 `README.md` 的「更新」小节（当前 v3.9.0）。
