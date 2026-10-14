# Azure · 围棋陪练

WARNING:这是一个由ai完善的项目！

ORIGINAL ASPIRATION:Azure是一个我设定的ai少女，最开始只有概念，后来根据自己学过的几年python知识以及一台比较好的设备在自己机器上跑了一下qwen2.5，后来又通过ai工具的使用逐渐做成了一个通过Napcat接入qq的机器人，后来得知了maibot这个工具并自己部署了qwen3.5但聊天效果太慢了，又恰好最近对围棋比较感兴趣（小时候也学过一点），遂有了此项目。

FUN WHERE:市场上有各种ai围棋陪练，但都是基于katago的直接应用，并没有指导的感觉，很枯燥，所以我希望AazureGo不仅仅可以让大家感到开心，并且也能激发大家在现代无法沉心环境背景下对于围棋学习的乐趣。

这是一个 3D 围棋陪练程序：**KataGo** 负责算棋（玩家执黑先行，Azure 执白应手），
**大模型** 负责讲解与闲聊，**语音合成** 让 Azure 开口说话，
**Godot 4** 场景里是一个会眨眼、歪头、注视你的青蓝色 AI 少女 Azure。

> 面向「**不想在本机部署大模型/语音**」的玩家：对局编排、KataGo 与模型调用都在
> **远程后端**（`backend\`）里，**LLM 与 TTS 都走可自定义的 OpenAI 兼容 API**（填地址 + API Key），
> **人设与提示词也能在 `config.json` 里直接改**。客户端（`game\`）只做渲染、输入与语音播放，
> 连不上后端时只在状态栏提示。
>
> 当前版本 **v3.0.0**：**只保留「连远程后端」一种运行方式**——客户端内的本地编排、
> 本地模型服务拉起、看屏幕搭话（peek）与 PCK 打包流程全部删除，所有模型改为云端。
> 详见「九、更新」。

---

## 一、仓库结构

```
.
├─ game\                    游戏本体（Godot 4.6 工程）
│   ├─ scenes\Main.tscn     主场景
│   ├─ scripts\             GDScript 源码（按职责分类）
│   │   ├─ ui\              Main.gd          主控：3D 棋盘 + 左右面板 + 接线
│   │   │                   Board3D.gd       3D 棋盘绘制与交互（含定式高亮轨迹）
│   │   │                   AzureAvatar.gd   VRM 形象、表情、视线、风场
│   │   │                   Ambience.gd      环境/风
│   │   ├─ services\        TTSClient.gd     语音客户端（system / openai）
│   │   ├─ core\            AzureConfig.gd   config.json 读写
│   │   │                   Coords.gd        坐标换算（棋盘坐标标签）
│   │   └─ net\             NetClient.gd     远程后端客户端（REST + WebSocket）
│   ├─ assets\voice\        预制语音：开场欢迎语 + 20 条语气词（即播零等待，与情绪/表情绑定）
│   ├─ tests\               语音测试脚本（test_tts.gd）
│   ├─ config.json          客户端参数模板（见第三节）
│   └─ export_presets.cfg   Godot 导出预设（个人导出用）
├─ backend\                 远程后端（唯一权威：对局编排 / KataGo / LLM / 情绪；FastAPI + REST + WebSocket）
│   ├─ app\                 对局编排 / KataGo·LLM 客户端 / 情感模型 / 提示词
│   ├─ tests\               后端单元测试（含情感模型黄金样本）
│   ├─ requirements.txt     后端依赖
│   └─ run_server.bat       一键启动后端（监听 127.0.0.1:8000）
├─ server\                  自建语音服务（CosyVoice 3，可选；**需自己启动**，游戏不再拉起）
│   ├─ azure_tts_server.py  FastAPI 服务：OpenAI 兼容 `/v1/audio/speech`、`/health`、`/voices`（另有 `/v1/models`）
│   ├─ run_hidden.vbs       隐藏窗口启动（手动启动自建服务时用）
│   ├─ start_tts_server*.bat 手动启动脚本
│   ├─ voices.json          克隆音色的参考音频与文本
│   └─ sherpa_voices.json   轻量音色清单（小雅 / 少女 / 女声）
└─ docs\                    后端化决策与实现计划
```

**模型与资产**（体积大，不随源码仓库分发，见 GitHub **Releases**）：
`katago.exe` + `*.dll`、`model-b18c384nbt.bin.gz`（KataGo 权重）、`Azure.vrm`（形象）、
CosyVoice 权重（`pretrained_models` / `sherpa_models`）。

---

## 二、快速开始

1. 用 **Godot 4.6** 打开 `game\` 工程，或：
   ```
   Godot_v4.6.3-stable_win64.exe --path game
   ```
2. 把 `Azure.vrm`（形象）放到可访问位置，并把 `vrm_path` 填成 **绝对路径**
   （用相对路径时文件必须能被 Godot 解析到，否则形象加载失败）。
   **KataGo 的 `katago.exe` / 权重 / `default_gtp.cfg` 由后端使用**，客户端不读这些路径。
3. 启动后端（`backend\run_server.bat`，见第六节），再启动游戏。
   > 后端在**启动时**读取 `config.json`（LLM / 提示词 / KataGo 等，见第三节「读取方」列），
   > 所以改这些字段、或在游戏设置面板里改完 LLM 地址/Key 后，都要**重启后端**才生效。
4. 玩家**执黑先行**：左键落子，右键拖拽旋转视角，滚轮缩放。

`config.json` 放在**可执行文件同目录**即可覆盖包内默认值（源码运行时读 `game\config.json`）。
语音默认走云端 `openai`（模板指向 OpenAI）：要填好 `tts_api_key`，并按第三节把 `tts_voice` 从 `中文女` 换成
OpenAI 的音色，才能真正出声；服务**连不上**时会自动临时降级为 **Windows 系统语音**。
聊天/讲解需要按第四节接上一个大模型 API。

---

## 三、`config.json` 字段

**这个文件前后端共用**：放在**可执行文件同目录**即可覆盖包内默认值（源码运行时读 `game\config.json`）。
下表「读取方」一列标出谁读它、什么时候读：

- **后端（启动时）**：`backend\run_server.bat` 启动时一次性读入，**改完必须重启后端才生效**
  （游戏内设置面板写回的 LLM 地址 / 模型名 / Key / 思考档位也属于这一类）。
- **后端（实时）**：经 `/api/config` 转发，改完立即生效。
- **客户端**：游戏进程自己读，改完（或下次启动）即时生效。

| 字段 | 读取方 | 含义 |
| --- | --- | --- |
| `backend_url` | 客户端 | **远程后端地址**：编排、KataGo 与 LLM 都在后端进程，游戏只做渲染与语音（WebSocket 地址由其推导） |
| `max_visits` | 后端（启动时） | KataGo 每手搜索次数，越大越强越慢（引擎在后端侧，客户端不跑 KataGo） |
| `joseki_teach` | 后端（实时） | **定式导学**：识别你与 Azure 走出的常见定式（讲解 + 棋盘高亮手顺）；右侧栏也能切；默认 `true`，关掉=不识别不打扰 |
| `llm_base_url` / `llm_model` | 后端（启动时） | **云端 LLM 的 OpenAI 兼容地址与模型名** |
| `llm_api_key` | 后端（启动时） | **云端 LLM 的 API Key**；非空则发送 `Authorization: Bearer <key>` |
| `llm_think_mode` | 后端（启动时） | **思考档位**：`0`=快速（直答，首字快）/ `1`=中等（推理限预算并催收尾，约 4~7 秒）/ `2`=强思考（自由推理，更慢更好）；默认 `0`；开局设置面板里也能改（旧键 `llm_think_strong` 自动迁移） |
| `prompt_system` | 后端（启动时） | **覆盖系统提示词（人设全文）**；留空用内置 Azure 人设 |
| `prompt_user_name` | 后端（启动时） | **玩家称呼**，替换提示词中的「Estarth」（默认 Estarth） |
| `prompt_phase_notes` | 后端（启动时） | **覆盖各阶段提示**，键：`observe` / `after_player` / `own_turn` / `just_moved` / `analyze` / `chat` / `summary` |
| `vrm_path` | 客户端 | 虚拟形象 `.vrm` 路径；**建议填绝对路径**（相对路径要能被 Godot 解析到，否则形象加载失败） |
| `tts_enabled` | 客户端 | 是否启用语音 |
| `tts_mode` | 客户端 | `openai`（云端 OpenAI 兼容 `/v1/audio/speech`，默认）/ `system`（Windows 系统语音，服务连不上时也会临时降级到它） |
| `tts_base_url` | 客户端 | **云端语音服务地址**（OpenAI 兼容，含 `/v1`，如 `https://api.openai.com/v1`） |
| `tts_model` / `tts_voice` | 客户端 | 云端语音模型名 / 音色 id（音色名随服务商而定，也可手填） |
| `tts_api_key` | 客户端 | **云端 TTS 的 API Key**；非空则发送 `Authorization: Bearer <key>` |
| `tts_speed` / `tts_pitch` / `tts_volume` | 客户端 | 语速 / 音调（播放层变调）/ 音量 |
| `tts_limit_len` / `tts_sync_speak` / `tts_batch_speak` / `tts_long_chunk` | 客户端 | 短回复 / 同步说话 / 整段合成 / 长句合并（立即生效） |

> - `tts_base_url` / `tts_model` / `tts_voice` 要**配套**：模板的三件套是 `https://api.openai.com/v1` + `tts-1` + `中文女`，
>   其中 `中文女` **不是** OpenAI 的音色 id——用 OpenAI 的话要把音色一并改成 `alloy` / `nova` / `onyx` 之类的
>   OpenAI 音色（音色 id 也可手填）；换其他服务商时地址、模型名、音色 id 都按服务商的取值改。
> - **两种失败表现不一样**：
>   ① 服务**连不上**（断网 / 地址写错 / 服务没启动）→ **自动临时降级**为 Windows 系统语音继续朗读，
>   服务恢复后自动切回云端（不报错、不改配置）；
>   ② 服务**能连上但拒绝了这次请求**（音色 / 模型名不被接受，例如用 `中文女` 调 OpenAI 会返回 HTTP 400）
>   → **不出声**：左侧语音栏与底部状态行显示「TTS 无响应（HTTP 400）…」，Azure 的文字照常显示，
>   冷却 6 秒后继续尝试队列里的下一句。**音色 / 模型配错不会自动降级**，只会没声音 + 一行报错。
> - `katago_bin` / `katago_model` / `katago_config` **不在上表**：它们由**后端**读取（当前后端仍自行拉起
>   KataGo 进程，计划 01 会改成独立的 KataGo 服务 + `katago_url`）。客户端**保留但不读**这些键——
>   游戏内保存设置时会**原样写回**，不会把它们抹掉（`game\config.json` 就是后端的配置文件，见第六节）。

---

## 四、接大模型（LLM，OpenAI 兼容）

改 `config.json` 三行即可，**不内置任何模型**：

```json
"llm_base_url": "https://api.deepseek.com/v1",
"llm_model": "deepseek-chat",
"llm_api_key": "sk-你的密钥"
```

- **也可在游戏里改**：开局前的「出发前的准备」面板能直接填地址 / 模型名 / API Key（确认后写回 `config.json`），不用手动编辑。
  ⚠️ 后端只在**启动时**读取这几个字段（游戏面板不会实时通知后端），所以改完要**重启后端**才生效。
- **云端**：DeepSeek / OpenAI / 通义 / 硅基流动 等 OpenAI 兼容服务，填地址、模型名与 Key。
- **自建服务**：vLLM / LM Studio / llama.cpp server 等，`llm_api_key` 留空即可——游戏只按地址请求，不负责启动它。
- 命令行临时指定（不写回配置）：`AzureGoGodot.exe --llm=https://api.deepseek.com/v1 --model=deepseek-chat --key=你的密钥`（指向自建服务时把地址换成它的，如 `http://127.0.0.1:8080/v1`）
- 没接 LLM 时：**对局功能照常可用**，只是讲解/闲聊退化为兜底台词。

---

## 五、接语音（TTS，可选）

- **云端 `openai`（默认）**：任何 OpenAI 兼容 `/v1/audio/speech` 服务（如 OpenAI、部分云厂商），
  填 `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`。
- **`system` 兜底**：Windows 系统语音，零配置即可发声；云端服务连不上时游戏会**自动临时降级为它**
  （不报错、不改配置），服务恢复后再切回。
- **自建 CosyVoice 也走 `openai`**：`server\` 的 `azure_tts_server.py` 同样实现了 OpenAI 兼容的
  `/v1/audio/speech`，把 `tts_mode` 保持 `openai`、`tts_base_url` 指向 `http://127.0.0.1:9880/v1` 即可。
  1. 自备 CosyVoice 权重，放到 `server\pretrained_models\` 下；
  2. 建虚拟环境并安装依赖（`server\requirements_win.txt`、`requirements.txt`）；
  3. 双击 `server\start_tts_server.bat` 手动启动服务（游戏不会自动拉起），服务监听 `http://127.0.0.1:9880`。

---

## 六、远程后端（唯一的运行方式）

对局编排、KataGo 与 LLM 都在**后端进程**（`backend\`，FastAPI + REST + WebSocket）里；游戏客户端只做渲染、
输入与语音播放，**不启动任何模型服务**（没有 local 模式）。后端连不上时只在状态栏提示，不影响界面操作。

- **启动后端**：双击 `backend\run_server.bat`（等价的手动启动：`cd backend` 后 `python -m app.main`），
  监听 `http://127.0.0.1:8000`。依赖 `fastapi` / `uvicorn` / `httpx`（见 `backend\requirements.txt`）。
- **配置**：`config.json` 里
  ```json
  "backend_url": "http://127.0.0.1:8000"
  ```
  也可命令行临时指定：`AzureGoGodot.exe --backend-url=http://127.0.0.1:8000`（只影响本次运行，不写回配置）。
- **接口契约**：
  - REST：`/api/move|hint|analyze|chat|summarize|undo|reset|config|reveal|interaction|idle|mood/tick`，
    以及 `GET /api/state`、`GET /api/health`。
  - WebSocket：`/ws` 推送 `hello / message / state / info / busy / mood / joseki`。
- **后端配置**：后端进程用环境变量 `AZURE_CONFIG` 指定 `config.json`；未设置时依次找 `<当前工作目录>\config.json`
  → `backend\config.json`（`backend\app\config.py` 的 `load`）。`backend\run_server.bat` 会先把
  `..\AzureGoGodot\config.json` 写进 `AZURE_CONFIG`，该文件不存在时改用 `..\game\config.json`——两个都在时用前者。
- **注意**：后端依赖本机 Python 环境，需按上面的命令自行启动；后端不在时游戏只提示、不崩溃。

---

## 七、导出与分发（可选）

`game\export_presets.cfg` 保留着导出预设，可以用 Godot 自带的「项目 → 导出」自行导出（PCK / 可执行文件）。
**原先的 PCK 打包脚本（`tools\` 下那个 PowerShell 脚本）已随 v3.0.0 删除**，那套「引擎本体 exe + 替换 PCK」
的分发流程不再维护。

---

## 八、许可

本项目源码采用 **Apache License 2.0**（见 [LICENSE](LICENSE)）。
`server\` 下的 CosyVoice 及其 `third_party\Matcha-TTS` 为各自上游项目，遵循其原始许可。
KataGo 引擎与权重遵循其上游许可。

---

## 九、更新

### v3.0.0（当前）
- **只保留远程后端**：客户端删除 `AzureAgent / AzurePrompts / KataGoEngine / LLMClient / GoBoard / GameState / Joseki* / EmotionModel` 与端到端测试脚本，装配路径收敛为 `NetClient`（REST 命令 + WebSocket 事件）；`config.json` 删掉运行方式键（`local` / `remote`）、`katago_*`（前端侧）、`llm_auto_start/exe/model_path/port`、`tts_auto_start/bf16/short_prompt`、`vlm_*` 等只服务本机部署的键，旧配置读入后会在下次写回时自我清理，不会崩溃、也不会把旧键写回文件。
- **模型全云端**：LLM 与语音 TTS 一律走 OpenAI 兼容 API（`llm_*` / `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`），`tts_mode` 默认改为 `openai`；`system` 只作连不上时的临时兜底（不写回配置）。后端不再拉起本地 LLM 服务（`services.py` 与看护逻辑删除），`backend/app/config.py` 的内置默认值改为云端示例。
- **看屏幕搭话（peek）整体移除**：前后端的 `peer_remark` / `POST /api/peer` / `_peek_*` / VLM 配置与情绪条目全部删除；闲置主动搭话仍走 `POST /api/idle` + `ai_idle`（随 peek 一起删掉的只有发言前那声「哼」，情绪区域爆发时仍会用同一段语气词）。
- **客户端不再拉起任何服务**：语音服务改为手动启动（`server\start_tts_server*.bat`），游戏只请求与播放；PCK 打包脚本删除，`export_presets.cfg` 保留供个人导出。
- **健壮性**：云端 LLM 失败（断网 / 401 / 429 / 超时）时所有流程都回兜底台词且 `busy` 必归位（此前会留下棋盘永久锁死的隐患）。

### v2.0.1
- **远程模式健壮性微调**：后端 `/api/move`、`/api/undo` 对畸形 / 越界参数改为优雅拒绝（不再返回 HTTP 500）；客户端 REST 请求加 30 秒超时，避免后端卡住时请求节点不释放（长时间运行不泄漏）；WS 连接建立时的 `hello` 事件附带忙闲状态，界面锁状态在连接瞬间即正确。另修正事件契约文档、补充 WS 异常日志。**当时默认的「客户端内编排」行为不变**（v3.0.0 已删除该模式）。

### v2.0.0
- **三维情感模型**：Azure 的情绪由「愉悦度 × 好奇度 × 唤醒度」三维状态驱动（各 0~1，分低/中/高三带 → 27 个情绪区域）；区域带滞回防抖、随时间向基线平缓回落。情绪经「落子 / 聊天 / 互动 / 闲置」等事件即时结算，表情（形变持续层 + 一次性强调）、语气词与语音音高随之变化，另有一次异步的 LLM「情绪变化」小调用做细腻微调（失败静默、绝不阻塞交互）。
- **前后端分离（可选）**：新增 Python 后端 `backend\`（FastAPI，REST + WebSocket），可把对局编排与 KataGo / LLM 移到独立进程，客户端只做渲染、语音与看屏幕搭话；`config.json` 用运行方式键（`local` / `remote`）与 `backend_url` 切换。**当时默认仍是 `local`**，全在客户端内，行为与旧版一致；分发包不随后端，玩家无需改动。**（v3.0.0 起只剩 remote，看屏幕搭话也已移除。）**

### v1.5.0
- **聊天防复读加强**：抑制「非相邻的原样复读」——此前只拦紧挨着的重复句，落子宣告（如「我在D16补一手」）夹在中间时，同一句点评仍会再冒一次；现在按最近 8 条窗口去重，隔一句话的一字不差复读也会被拦下（落子宣告本身不受影响）。
- **方位描述纠错**：点评里的「左/右/上/下」必须先按坐标核对——字母是列（从左到右）、数字是行（从上到下）；两子同列只能说「上下」、同行才算「左右」，核对不上就别说方位词（如「你这两颗黑子一左一右」说成实际同列一上一下的错误不再出现）。
- **代码整理**：`game\scripts\` 按职责重新分类为 `core` / `ai` / `services` / `ui` 四个子目录（引用与场景同步更新）；清理过期注释与只写不读的死变量，行为不变。

### v1.4.0
- **定式导学**：对局中走出常见定式（点三三 / 小飞挂 / 双飞燕…，入门定式库取自 KataGo 自对弈实测）时，Azure 讲解要点、棋盘按手顺点亮高亮轨迹；你偏离已吻合的定式时，她会轻轻提一句常规形状（点到即止）。识别纯本地计算、零引擎与额外 LLM 调用（成型讲解直接替换该手点评）；悔棋回滚后可重新讲解；右侧栏「定式导学」可关，关掉行为与旧版一致。
- **发言更像真人**：提示词删掉可被照抄的示例句、补「说话方式」小节；LLM 侧开启重复惩罚；完全相同的重复句直接抑制。新增导演层：重要点评前有犹豫节拍、聊天面板逐字打字（确定性本地规则，零额外开销）。
- **语气词与情绪**：预制语音（欢迎语 + 语气词）扩到 20 条——新增 加油 / 得意 / 傲娇 / 叹气 四类语气，并全部以新一轮高精度音色合成逐条择优替换，更接近 Azure 声线；新增 `[[tone:…]]` 情绪标记与去重（同一情绪 30 秒内只出声一次）、语气词不叠响、思考声每回合限一次。
- **思考三档**：快速 / 中等（推理限 token 预算并注入收尾提示）/ 强思考，设置面板与 `config.json` 均可选；旧 `llm_think_strong` 自动迁移。
- **本地服务更稳**：对局中看护本地 LLM 服务，意外退出（如 GPU 故障）自动重新拉起；llama-server 加载期 503 显示「正在加载」而不是报错。
- **语音加载期不再误用系统音色**：本地语音模型加载期间只出文字、绝不降级为 Windows 音色；TTS 地址指向外部 API 时不拉起 / 不等待本地服务。
- **对局进程观**：按手数识别布局 / 中盘 / 收官，让她的口吻与当前进程匹配（治刚开局就喊「反击」「缠斗」）。

### v1.3.1
- **修复强思考推理段泄漏**：强思考把回复预算耗在推理上时，模型原始推理过程曾被当成正文显示给玩家（一整段全英文内心独白）。现在推理段**永不显示**——预算耗尽时自动改用快速模式重答一次，内容干净、语气自然（代价：强思考下每次回答会先多等 20~30 秒，日常陪玩建议用「快速」模式）。
- **走神搭话先看屏幕**：玩家切到别的窗口（微信/浏览器…）时，走神搭话一律先看一眼屏幕、结合屏幕内容开口，不再只说默认的「还在吗」；游戏窗口在前台时直接普通搭话（不白起截图进程）。看屏幕冷却从 3 分钟缩短到 1 分钟。**（v3.0.0 已移除。）**

### v1.3.0
- **开局设置面板**：启动时先弹出「出发前的准备」——选择**快速 / 强思考**，并可直接修改 **LLM / 语音 TTS / 看屏幕 VLM** 的 API 配置（默认沿用上次的值，只有改过的才写回 `config.json`；命令行 `--llm/--model/--key` 的临时覆盖不会写回）。**（v3.0.0 起面板只剩 LLM 与语音 TTS。）**
- **强思考模式**：思考型模型（Qwen3 等）可选「先推理再回答」（更慢、质量更好）；本地服务自动放行推理段，云端 API 不支持时自动回退，不影响使用。
- 语音修正：取消「首块短切」分句合成——短消息整句一次合成、连续读完，不再开口碎句。
- 看屏幕修正：游戏窗口在前台（玩家就在游戏里）时不再白起 python 进程；超时不再残留孤儿进程、失败不再占用冷却；结果文件按次命名；VLM 服务未就绪时等待重试（不再白白回退 CPU）。
- 修复 `start_qwen35b.bat` 误杀看屏幕 VLM 的 llama-server（现在只结束占用 8080 的旧实例）。

### v1.2.0
- **开场秒开口**：第一句欢迎语改为**预制语音**（`assets\voice\welcome_<玩家名>.wav`），启动即播、零等待；缺文件自动回退普通合成。
- **本地服务随游戏自动起停**：启动时一并拉起本地 LLM / CosyVoice / 看屏幕 VLM 服务，退出时清理由游戏拉起的进程（缺环境自动跳过，不影响使用）。**（v3.0.0 已移除：游戏不再拉起任何服务。）**
- 新增 `llm_auto_start` / `llm_exe` / `llm_model_path` / `llm_port` 与 `vlm_*` 配置项。
- 提速：LLM 前缀缓存复用；思考型模型默认关思考（云端不认自动回退）；CosyVoice 边播边合成、长句合并（可开关）。

### v1.1.0
- 长闲置**看屏幕搭话**：空闲时截屏 + OCR + 本地 VLM 看图，Azure 主动找你说话（缺环境自动降级为普通搭话）。
- 点评跨回合去重；落子宣告不复读；走神搭话语音可整句作废。

本人技术有限，但想象力绝对够，后续版本更新取决于ai以及我的计算机&人工智能学习深度(:-])
自建部署采用 CosyVoice 3，Qwen3.6 35B（`Qwen3.6-35B-A3B-UD-Q4_K_XL`）。
