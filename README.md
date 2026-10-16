# Azure · 围棋陪练

WARNING:这是一个由ai完善的项目！

ORIGINAL ASPIRATION:Azure是一个我设定的ai少女，最开始只有概念，后来根据自己学过的几年python知识以及一台比较好的设备在自己机器上跑了一下qwen2.5，后来又通过ai工具的使用逐渐做成了一个通过Napcat接入qq的机器人，后来得知了maibot这个工具并自己部署了qwen3.5但聊天效果太慢了，又恰好最近对围棋比较感兴趣（小时候也学过一点），遂有了此项目。

FUN WHERE:市场上有各种ai围棋陪练，但都是基于katago的直接应用，并没有指导的感觉，很枯燥，所以我希望AazureGo不仅仅可以让大家感到开心，并且也能激发大家在现代无法沉心环境背景下对于围棋学习的乐趣。

这是一个 3D 围棋陪练程序：**KataGo** 负责算棋（玩家执黑先行，Azure 执白应手），
**大模型** 负责讲解与闲聊，**语音合成** 让 Azure 开口说话，
**Godot 4** 场景里是一个会眨眼、歪头、注视你的青蓝色 AI 少女 Azure。

> 面向「**不想在本机部署大模型/语音**」的玩家：对局编排与模型调用都在
> **远程后端**（`backend\`）里，**KataGo 由独立部署的云服务**（`katago_service\`）提供，
> **LLM 与 TTS 都走可自定义的 OpenAI 兼容 API**（填地址 + API Key），
> **人设与提示词也能在 `config.json` 里直接改**。客户端（`game\`）只做渲染、输入与语音播放，
> 连不上后端时只在状态栏提示。
>
> 当前版本 **v3.3.0**：新增**长期记忆**——开启后她跨局记得你（对局总结写进本地 SQLite，
> 出话前按语义检索注入上下文，云端 embedding 不可用时自动降级为关键词匹配），**默认关闭**，
> 关闭时不建库、不调 embedding 接口、提示词逐字符不变；另有 `/api/memory/*` 管理端点可查看 / 手动写入 / 忘记。
> 详见第四节「长期记忆（跨局记得你）」与「九、更新」。

---

## 目录

- [分支说明](#分支说明)
- [一、仓库结构](#一仓库结构)
- [二、快速开始](#二快速开始)
- [三、`config.json` 字段](#三configjson-字段)
- [四、接大模型（LLM，OpenAI 兼容）](#四接大模型llmopenai-兼容)
- [五、接语音（TTS，可选）](#五接语音tts可选)
- [六、远程后端（唯一的运行方式）](#六远程后端唯一的运行方式)
- [七、导出与分发（可选）](#七导出与分发可选)
- [八、许可](#八许可)
- [九、更新](#九更新)
- [十、致谢](#十致谢)

---

## 分支说明

本仓库以 **发展主版本** 为主线，另保留一条 **历史分支** 完整承载旧版：

| 分支 | 定位 | 内容概要 | 链接 |
| --- | --- | --- | --- |
| **`main`** | 发展主版本（当前主线，v3.x） | 前后端分离：`game\` 只负责渲染、输入与语音播放，`backend\` 统一编排对局与模型调用，`katago_service\` 独立部署提供 KataGo；支持 LangChain 提示链、发言侧工具与长期记忆。 | [查看 main](https://github.com/AzureEstrath/Azure-Go) |
| **`legacy`** | 历史分支（发展副版本，v1.0–v2.0.1） | 旧版完整存档：本机运行 KataGo 与本地模型、三维情感模型（valence / curiosity / arousal）、前后端分离 v2.0.1，以及 `peek\` 窥屏脚本等。 | [查看 legacy](https://github.com/AzureEstrath/Azure-Go/tree/legacy) |

> `legacy` 为旧版的历史存档，后续功能开发集中在 `main`。

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
├─ backend\                 远程后端（唯一权威：对局编排 / LLM / 情绪 / 定式；FastAPI + REST + WebSocket）
│   ├─ app\                 对局编排 / LangChain 链（LLM + 提示词） / 情感模型 / 发言侧工具 / 长期记忆
│   ├─ tests\               后端单元测试（含情感模型黄金样本）
│   ├─ requirements.txt     后端依赖
│   ├─ run_server.bat       一键启动后端（监听 127.0.0.1:8000）
│   └─ data\                运行产物（已 gitignore）：长期记忆库 memory.db（见第四节）
├─ katago_service\          KataGo 云服务（独立部署：常驻 `katago analysis` 进程，对外 `/health` + `/analyze`）
│   ├─ server.py            FastAPI 应用：Bearer 鉴权、串行化、warming/ready/dead 状态与 /health
│   ├─ proc.py              常驻引擎子进程封装（stderr 持续排空 + 64MB 读行缓冲 + 启动预热）
│   ├─ tests\               服务单元测试（假引擎，不依赖真实 KataGo）
│   ├─ requirements.txt     服务依赖（fastapi / uvicorn）
│   └─ run_server.bat       一键启动服务（监听 127.0.0.1:8100，见第六节）
├─ server\                  自建语音服务（CosyVoice 3，可选；**需自己启动**，游戏不再拉起）
│   ├─ azure_tts_server.py  FastAPI 服务：OpenAI 兼容 `/v1/audio/speech`、`/health`、`/voices`（另有 `/v1/models`）
│   ├─ run_hidden.vbs       隐藏窗口启动（手动启动自建服务时用）
│   ├─ start_tts_server*.bat 手动启动脚本
│   ├─ voices.json          克隆音色的参考音频与文本
│   ├─ third_party\OpenVoice\ 快速克隆音色转换（源码；权重 .pth 另行获取）
│   └─ sherpa_voices.json   轻量音色清单（小雅 / 少女 / 女声）
├─ docs\                    后端化决策与实现计划
└─ peek\                    窥屏 VLM 脚本（azure_peek.py / start_vlm_server.py）；v3.x 未接线，保留供后续功能
```

**模型与资产**（体积大，不随源码仓库分发，见 GitHub **Releases**）：
`katago.exe` + `*.dll`、`model-b18c384nbt.bin.gz`（KataGo 权重）——这两个归 **KataGo 云服务的部署侧**持有
（见第六节「KataGo 服务部署」），本仓库只含连它的客户端；
`Azure.vrm`（形象）、CosyVoice 权重（`pretrained_models` / `sherpa_models`）、
OpenVoice 权重（`third_party/OpenVoice/checkpoints_v2` 的 `.pth`）、云端 LLM / VLM 的 gguf。

---

## 二、快速开始

1. 用 **Godot 4.6** 打开 `game\` 工程，或：
   ```
   Godot_v4.6.3-stable_win64.exe --path game
   ```
2. 把 `Azure.vrm`（形象）放到可访问位置，并把 `vrm_path` 填成 **绝对路径**
   （用相对路径时文件必须能被 Godot 解析到，否则形象加载失败）。
   **KataGo 的 `katago.exe` / 权重 / analysis 配置文件归 KataGo 云服务部署侧持有**，客户端不读这些路径。
3. 先启动 **KataGo 云服务**（`katago_service\run_server.bat`，见第六节「KataGo 服务部署」），
   再启动后端（`backend\run_server.bat`，见第六节），最后启动游戏。
   > 后端在**启动时**读取 `config.json`（LLM / 提示词 / KataGo 服务地址等，见第三节「读取方」列），
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
- **后端专用（客户端不读）**：`katago_url` / `katago_token` / `katago_timeout_ms`、
  `llm_extra_body` / `llm_think_extra_body` / `tools_enabled` / `max_tool_calls` 与
  `memory_enabled` / `memory_db_path` / `embed_base_url` / `embed_model` / `embed_api_key` /
  `embed_timeout` / `memory_top_k` / `memory_max_inject_chars` 只由后端消费；
  客户端不读它们，但**写回配置时会原样保留**（游戏内保存设置不会把这些键弄丢）。

| 字段 | 读取方 | 含义 |
| --- | --- | --- |
| `backend_url` | 客户端 | **远程后端地址**：编排与 LLM 都在后端进程，游戏只做渲染与语音（WebSocket 地址由其推导） |
| `max_visits` | 后端（启动时） | KataGo 每手搜索次数，越大越强越慢（由后端逐次分析请求传给云服务，客户端不跑 KataGo） |
| `katago_url` | 后端（启动时） | **KataGo 云服务地址**（默认 `http://127.0.0.1:8100`）；引擎在独立服务里，后端只发 HTTP 请求（见第六节「KataGo 服务部署」） |
| `katago_token` | 后端（启动时） | **KataGo 云服务的 Bearer 令牌**；与服务侧 `KATAGO_SERVICE_TOKEN` / `--token` 一致，服务未配令牌时留空（仅限本机回环调试） |
| `katago_timeout_ms` | 后端（启动时） | 单次分析超时（毫秒，默认 `30000`）；超时/连接失败会重试 1 次，仍失败才报「云服务不可用」 |
| `joseki_teach` | 后端（实时） | **定式导学**：识别你与 Azure 走出的常见定式（讲解 + 棋盘高亮手顺）；右侧栏也能切；默认 `true`，关掉=不识别不打扰 |
| `llm_base_url` / `llm_model` | 后端（启动时） | **云端 LLM 的 OpenAI 兼容地址与模型名** |
| `llm_api_key` | 后端（启动时） | **云端 LLM 的 API Key**；非空则发送 `Authorization: Bearer <key>` |
| `llm_think_mode` | 后端（启动时） | **思考档位**：`0`=快速（直答，首字快）/ `1`=中等（推理限预算并催收尾，约 4~7 秒）/ `2`=强思考（自由推理，更慢更好）；默认 `0`；开局设置面板里也能改（旧键 `llm_think_strong` 自动迁移） |
| `llm_extra_body` | 后端（启动时） | **透传给云端 LLM 的请求体扩展字段**（JSON 对象，如 `{"top_k": 40}`）：每次请求都并入。服务商不认这些字段（HTTP 400/422）时后端自动去掉扩展字段重试一次，且本次会话不再发送它们（设计好的降级，不是故障）。输出上限按线上键 `max_tokens` 发送；在这里显式写 `max_tokens` / `max_completion_tokens` 可覆盖后端注入的预算（服务商只认新键时的退路） |
| `llm_think_extra_body` | 后端（启动时） | 同上，但**只在思考档位开启（`llm_think_mode` > 0）时**并入（如 `{"reasoning_effort": "high"}`）；不开思考档位时完全不发 |
| `tools_enabled` | 后端（启动时） | **发言侧工具**总开关，默认 `false`：开启后她在「提示」与「落子宣告」两个回合可自主复算局面 / 查定式 / 核对盘面——**只影响她怎么说，不影响她下哪里**（见第四节「工具（发言侧）」） |
| `max_tool_calls` | 后端（启动时） | 一次发言里**工具执行次数**上限（默认 `2`；代码侧硬上限也是 `2`，配置只能收紧、不能放宽） |
| `memory_enabled` | 后端（启动时） | **长期记忆**总开关，默认 `false`：开启后她跨局记得你（对局总结与显式事实落 SQLite，出话前召回注入）；见第四节「长期记忆（跨局记得你）」 |
| `memory_db_path` | 后端（启动时） | 长期记忆库路径；留空＝`backend\data\memory.db`（**只在开启时创建**，属运行产物、已 gitignore） |
| `embed_base_url` / `embed_model` / `embed_api_key` | 后端（启动时） | **云端 embedding 的 OpenAI 兼容地址 / 模型名 / Key**（如 `https://api.openai.com/v1` + `text-embedding-3-small`）；三者**任一为空＝不配向量**，召回自动降级为关键词匹配（不报错、不影响出话） |
| `embed_timeout` | 后端（启动时） | **单次 embedding 请求超时**（秒，默认 `10`）；网络差或想让降级更快发生时可调小（≤0 / 脏值退回默认）。召回 / 写入已移出事件循环，这里只决定单次请求最长等多久 |
| `memory_top_k` | 后端（启动时） | 每次召回**注入条数**上限（默认 `3`；`0`＝记忆开着但不召回，管理面查询同样返回空列表） |
| `memory_max_inject_chars` | 后端（启动时） | 注入提示词的**总字数**上限（默认 `240`；单条另截到 120 字） |
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
> - **KataGo 引擎路径类的旧键（可执行文件 / 权重 / analysis 配置三件套）不在上表，也不再有任何读取方**：
>   它们是「后端自行拉起 KataGo 进程」时代的旧键（v3.0.0 及更早），v3.1.0 起 KataGo 由独立云服务提供，
>   后端只连 `katago_url`。游戏内保存设置时会**把这几个旧键一并清掉**（自我清理，见
>   `game\scripts\core\AzureConfig.gd` 的 `save()`），不影响对局；引擎本体与权重归服务部署侧持有，
>   不入本仓库（见第一节「模型与资产」）。

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
- 后端内部走 **LangChain**（`ChatOpenAI`）：上面三个字段照旧填，服务商专有参数用 `llm_extra_body` /
  `llm_think_extra_body` 透传（见第三节）；调用失败一律降级为可读提示 + 兜底台词，不会卡住棋盘。

### 工具（发言侧）

**默认关闭**（`tools_enabled: false`，改成 `true` 即开启）：行为与没有工具时完全一致。开启后她**只在两个回合**可以自己查资料——
「提示」与「落子宣告」——且**只影响她怎么说，绝不影响她下哪里**（落点仍由后端算棋决定）。

- 三个工具：`katago_analyze`（复算局面：黑方胜率 / 目差 / 候选点）、`joseki_lookup`（查入门定式库）、
  `board_query`（把局面画成紧凑盘面图，核对坐标与归属）。
- **代价**：这两个回合会多出几次模型往返（工具执行次数受 `max_tool_calls` 与代码硬上限约束、单工具 10 秒超时），
  回答会变慢；闲聊 / 闲置搭话 / 点评等路径**不启用**工具，时延不受影响。
- **工具输出永不直接给玩家看**：返回的盘面 / JSON 只回到模型手里，玩家看到的仍是她据此重写的自然语言；
  客户端只多一类 `tool` 事件（开始 / 结束），仅用于状态栏提示（「她正在查资料…」）。
- 改完 `tools_enabled` / `max_tool_calls` 要**重启后端**；`POST /api/config` 也能实时切换 `tools_enabled`
  （客户端界面暂未提供入口）。

### 长期记忆（跨局记得你）

**默认关闭**（`memory_enabled: false`）：关闭时**零开销**——不创建数据库文件、不调任何 embedding 接口，
提示词与没有这个功能时**逐字符一致**。开启后她跨局记得你这个人；与「这盘棋」的局内记忆
（落子评定存档，随对局重置）互不替代。

**存什么**

- **对局总结**：每局 `POST /api/summarize` 成功出总结后，后端把棋谱 + 结果 + 总结落一条归档，
  并把总结另存为一条可召回的记忆（`kind=game`）。LLM 失败退化为兜底台词时**不写**，防止污染。
- **显式事实 / 偏好**：经 `POST /api/memory/write` 手动写入（`kind=fact`，如「他喜欢简明，不喜欢复杂战斗」）。
- **不存**：她的台词、当前盘面描述、模型推理段——长期记忆只收总结与显式事实，越用越准、不会越用越失真。
- 写入硬上限：单条 200 字（超长截断并标「…」）、同 `tag` + 同 `text` 去重、库内最多 2000 条
  （超出按时间淘汰最旧）。

**怎么用**

- 两个召回点：**闲聊**（用你这句原话做查询）与**她自己落子前**（用最近一手 + 阶段做查询）；
  召回文本插在提示词的「【最近的对话】」之后、铁律段之前，措辞明确「不是当前盘面、仅供参考、别照抄」。
- 注入硬上限：每次最多 `memory_top_k` 条、单条 120 字、总计 `memory_max_inject_chars` 字。
- 配了 `embed_*` 就按**语义相似度**（云端 embedding + 余弦）排序，**向量命中不足 `memory_top_k` 条时用
  关键词从其余记忆里补齐**（没向量的老行 / 写入时 embedding 失败的行不会被有向量的行压掉）；
  **没配 / 401 / 超时**一律降级为**关键词匹配**（不伪造相似度分数）。记忆出任何问题都只打日志、自动降级，
  **绝不影响对局**（`busy` 照常归位）；召回 / 写入都在工作线程里执行（`asyncio.to_thread`），
  云端 embedding 卡住也不会冻住后端事件循环（单次最长等 `embed_timeout` 秒，默认 10）。
- 库是 SQLite：`memory_db_path` 留空时落在 `backend\data\memory.db`（运行产物，已 gitignore）；
  这几个键都在后端**启动时**读取，改完要**重启后端**。

**怎么忘掉 / 查看**

四个管理端点（`memory_enabled=false` 时查询面返回空结果、写入被拒，都不会 500）：

```bash
# 1) 看她记住了什么（query 必填；k 可省，缺省用 memory_top_k；memory_top_k=0 或显式 k≤0 ＝不召回，返回空列表）
curl -X POST http://127.0.0.1:8000/api/memory/query -H "Content-Type: application/json" \
  -d '{"query":"Estarth 的偏好","k":3}'
# → {"ok":true,"items":[{"id":1,"ts":1750000000.0,"kind":"fact","tag":"estarth","text":"…","score":0.83}]}

# 2) 手动记一条（tag 可省；关闭 / 空文本会被拒：{"ok":false,"id":null}）
curl -X POST http://127.0.0.1:8000/api/memory/write -H "Content-Type: application/json" \
  -d '{"tag":"estarth","text":"他喜欢简明，不喜欢复杂战斗"}'
# → {"ok":true,"id":1}

# 3) 看体量：rows=记忆条数、games=对局归档数、has_vectors=有向量的条数、db=库文件路径
curl http://127.0.0.1:8000/api/memory/stats
# → {"ok":true,"rows":1,"games":0,"has_vectors":1,"db":"…\\backend\\data\\memory.db"}

# 4) 忘掉指定几条，或一次清空
curl -X POST http://127.0.0.1:8000/api/memory/forget -H "Content-Type: application/json" -d '{"ids":[1]}'
curl -X POST http://127.0.0.1:8000/api/memory/forget -H "Content-Type: application/json" -d '{"all":true}'
# → {"ok":true,"removed":1}
```

> - `forget` 只清**记忆**（会被召回的部分），不动对局归档；停掉后端后直接删掉库文件（连同 `-wal` / `-shm`）
>   也能全部忘记。
> - 示例里 `-d '…'` 的单引号写法在 **PowerShell 7 / Git Bash** 可用；**cmd.exe** 要改写成 `-d "{\"query\":\"…\"}"`。
> - 管理端点的写 / 查会往 `/ws` 推 `memory` 事件（形状 `{type, op, tag, text, score}`；写一条、
>   查每条命中一条）——**纯元数据**：客户端只记一行日志，不进台词、不产生界面变化；
>   **对局中她自己的召回 / 写入不发事件**。
> - `GET /api/health` 的 `memory_rows` 是当前记忆条数（关闭 / 库不可用时为 `0`）。

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

对局编排、LLM 与定式识别都在**后端进程**（`backend\`，FastAPI + REST + WebSocket）里；KataGo 由**独立部署的
云服务**（`katago_service\`）提供，后端只连它（见下方「KataGo 服务部署」）。游戏客户端只做渲染、输入与语音播放，
**不启动任何模型服务**（没有 local 模式）。后端连不上时只在状态栏提示，不影响界面操作。

- **启动后端**：双击 `backend\run_server.bat`（等价的手动启动：`cd backend` 后 `python -m app.main`），
  监听 `http://127.0.0.1:8000`。依赖 `fastapi` / `uvicorn` / `websockets` / `httpx` /
  `langchain-core` / `langchain-openai`（见 `backend\requirements.txt`；LLM 调用走 LangChain）。
  > `websockets` 不能省：裸装 `uvicorn` 不带 WebSocket 实现，缺了它 `/ws` 会拒绝升级（404），
  > 客户端只会一直显示「未连接后端」。
- **配置**：`config.json` 里
  ```json
  "backend_url": "http://127.0.0.1:8000"
  ```
  也可命令行临时指定：`AzureGoGodot.exe --backend-url=http://127.0.0.1:8000`（只影响本次运行，不写回配置）。
- **接口契约**：
  - REST：`/api/move|hint|analyze|chat|summarize|undo|reset|config|reveal|interaction|idle|mood/tick`，
    以及 `GET /api/state`、`GET /api/health`，和长期记忆管理面 `/api/memory/query|write|stats|forget`
    （见第四节「长期记忆（跨局记得你）」）。
  - WebSocket：`/ws` 推送 `hello / message / state / info / busy / mood / joseki / tool / memory`
    （`tool` 是发言侧工具的开始 / 结束，只喂状态栏提示，不进入对话；`memory` 是记忆管理动作的元数据，
    只记一行日志，不进台词、不产生界面变化）。
  - `GET /api/health` 返回 `{ok, katago_dead, katago_error, katago_url, llm_url, memory_rows}`（字段都改为描述所连云服务：
    地址 / 引擎是否还在 / 最近错误）。其中 `katago_dead` 与 `katago_error` 必须**成对读**：
    - `katago_dead` 表示**引擎已经没了**：`start()` 探活失败、或分析时发生**传输层失败**（连接被拒 / 连接或
      读取错误，相当于旧子进程时代的「管道断了」）都会置位；一次**成功的分析会把它清掉**（云服务重启后可自愈）；
    - `katago_error` 则在**每次分析失败**时刷新（成功时清空）；
    - **分析**时的超时与 HTTP 状态响应（503 预热 / 401 / 500）**只刷新 `katago_error`、不置 `katago_dead`**
      ——服务还在、只是这一次没答好。所以「`katago_dead=false` + 新的非空 `katago_error`」＝服务**在线但这次
      没答好**，「`katago_dead=true`」＝引擎（或到它的链路）真的没了。运维自检以 `katago_error` 为准；
    - `katago_url` 是后端当前连接的 KataGo 服务地址，便于确认连的是哪台（留空时客户端回退默认回环地址
      `http://127.0.0.1:8100`）；
    - `memory_rows` 是长期记忆库里的记忆条数（未启用 / 库不可用时为 `0`）——一眼看出记忆库是否在用。
- **后端配置**：后端进程用环境变量 `AZURE_CONFIG` 指定 `config.json`；未设置时依次找 `<当前工作目录>\config.json`
  → `backend\config.json`（`backend\app\config.py` 的 `load`）。`backend\run_server.bat` 会先把
  `..\AzureGoGodot\config.json` 写进 `AZURE_CONFIG`，该文件不存在时改用 `..\game\config.json`——两个都在时用前者。
- **注意**：后端依赖本机 Python 环境，需按上面的命令自行启动；后端不在时游戏只提示、不崩溃。
  KataGo 服务没起或 `katago_url` 写错时，后端启动横幅会打印「`[KataGo] 云服务不可用：…`」并继续运行——
  对局时该手退化为可读提示、`busy` 必归位、棋盘不会锁死；把地址改对后**重启后端**即可。

### KataGo 服务部署

KataGo 不再由后端进程拉起，而是**独立部署的云服务**（`katago_service\`）：FastAPI 里常驻一个
`katago analysis` 子进程，启动即**后台预热**（OpenCL 首次调优挪到部署阶段），对外只暴露
`POST /analyze`（请求/响应都是引擎原文）与 `GET /health`。后端通过 `katago_url` 连它，因此服务可以
先起、也可以单独重启，对局侧只会拿到「云服务不可用」的可读提示。

- **机器要求**：能跑 KataGo 的机器（Windows + 显卡驱动；纯 CPU 也能跑，只是慢）。引擎本体
  `katago.exe` / `*.dll`、权重 `model-b18c384nbt.bin.gz`、analysis 配置文件（如 `analysis_example.cfg`）
  都归**这台机器**持有，不入本仓库（见第一节「模型与资产」）。
- **装依赖**：`pip install -r katago_service\requirements.txt`（`fastapi` / `uvicorn`；`httpx` 只给测试用）。
- **启动**：双击 `katago_service\run_server.bat`。脚本**必须原样跑**——它自己 `cd` 到仓库根目录，
  因为 `katago_service` 是包，`python -m katago_service.server` 要求父目录在 `sys.path` 上。
  等价的手动命令是**在仓库根目录**执行：
  ```
  python -m katago_service.server --port 8100 --token <令牌> --katago-exe <katago.exe> --model <权重> --config <analysis cfg> [--analysis-threads N]
  ```
  参数留空则由环境变量兜底：`KATAGO_HOST` / `KATAGO_SERVICE_TOKEN` / `KATAGO_EXE` / `KATAGO_MODEL` /
  `KATAGO_CONFIG`（对**直接执行**与**经 `run_server.bat`** 两种方式都生效）。默认监听 `127.0.0.1:8100`。
- **启动顺序（重要）**：先起 KataGo 服务，**轮询 `/health` 直到 `state == "ready"`**，再起后端。
  服务启动即后台预热（加载模型 / OpenCL 首次调优可能要几十秒到几分钟），预热期间**不要**启动后端：
  后端在 lifespan 里会等它就绪（最长 `ready_timeout_ms`＝180 秒）才绑端口，这段时间里 `/api/chat`、
  `/api/state` 与 `/ws` 都不可达，游戏只会显示「未连接后端」——看起来像后端根本没起来，
  其实它是在等 KataGo 预热（预热完成、后端绑上端口后会自动恢复）。
- **自检**：`curl http://127.0.0.1:8100/health` → `{"ok": true, "state": "ready", "katago_error": "", ...}`。
  模型加载期间 `state` 是 `warming`（此时 `/analyze` 立刻回 503 可读错误，不会挂到客户端超时，也不会
  让游戏卡住）；引擎起不来则是 `dead`，`katago_error` 给出原因。日志在 `katago_service\logs\katago\`。
- **鉴权与暴露面**：服务配了 token（`--token` / `KATAGO_SERVICE_TOKEN`）就必须带
  `Authorization: Bearer <token>`，后端读 `katago_token` 对应它；不配 token 只允许**本机回环调试**。
  跨机部署要显式 `--host 0.0.0.0` 或 `KATAGO_HOST=0.0.0.0`——此时服务启动会打印一条「对外监听且未配置
  token」的告警（**只是告警、不阻止启动**，回环调试仍可无 token）。
  **务必只在内网 / TLS 反代之后暴露，并配上 token。**
- **端口/地址要对上**：服务默认 8100、后端 `katago_url` 默认 `http://127.0.0.1:8100`；换机器或换端口时
  两边一起改（服务侧 `--port`，后端侧 `katago_url`）。`katago_url` / `katago_token` /
  `katago_timeout_ms` 都在后端**启动时**读取，改完重启后端。

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

### v3.3.0（当前）
- **长期记忆（默认关闭）**：新增 `backend\app\memory.py`（SQLite 存储 + 检索：有向量按余弦相似度、
  无向量按关键词）与 `backend\app\memory_service.py`（云端 embedding 装配，`embed_*` 任一为空就不装配
  embedding 客户端、自动降级为关键词检索）。对局总结成功后写入长期记忆，**闲聊**与**她自己落子前**
  两处召回注入（插在提示词「【最近的对话】」之后、铁律段之前，措辞强调「不是当前盘面、仅供参考、别照抄」）。
  **默认关闭时零开销**：不建库、不调 embedding，且提示词与没有这个功能时**逐字符一致**（黄金样本锁定）。
- **写入与注入都有硬上限**：单条 200 字、同 tag + 同 text 去重、库内最多 2000 条（超出按时间淘汰最旧）；
  每次最多注入 `memory_top_k`（默认 `3`）条、单条 120 字、总计 `memory_max_inject_chars`（默认 `240`）字。
  任何记忆故障（库不可写、embedding 401 / 超时）都只打日志、自动降级，**绝不影响对局**。
- **管理面与配置**：新增 `POST /api/memory/query|write|forget` 与 `GET /api/memory/stats`（查看 / 手动写入 /
  忘记），`GET /api/health` 增加 `memory_rows`；管理端点的写 / 查另推 `memory` WS 事件（纯元数据，前端只记日志）。
  新增配置 `memory_enabled` / `memory_db_path` / `embed_base_url` / `embed_model` / `embed_api_key` /
  `embed_timeout`（默认 10 秒） / `memory_top_k` / `memory_max_inject_chars`，库文件默认在 `backend\data\`
  （运行产物，已 gitignore）。见第四节「长期记忆（跨局记得你）」。
- **召回与阻塞**：向量命中不足 `memory_top_k` 条时用关键词从其余记忆里补齐（没向量的老行 / 写入时
  embedding 失败的行不会被压掉）；召回 / 写入（她的两处 + 管理端点）都走 `asyncio.to_thread`，
  云端 embedding 卡住也不会冻住后端事件循环。

### v3.2.0
- **后端 LLM 调用改走 LangChain**：新增 `backend\app\llm_chain.py`（`ChatOpenAI` 的唯一装配点）与
  `backend\app\prompt_chains.py`（七阶段提示的 `ChatPromptTemplate` 链）；`llm.py` 退化为薄适配器
  （对外 `chat()` 契约不变，台词仍必经 `clean_reply()` 与去 think 段的 `content_of()`）。依赖锁定
  `langchain-core>=1.0,<2.0` / `langchain-openai>=1.0,<2.0`（要求 1.0 及以上、限定在 1.x——测过的
  就是装到的；`backend\requirements.txt`）；旧的本地地址预检与 `probe_models` 探测随之删除。
- **可配置的请求体扩展字段**：新增 `llm_extra_body`（始终并入）与 `llm_think_extra_body`（仅思考档位开启时并入），
  透传服务商专有参数（`top_k` / `reasoning_effort` 等）；输出上限按线上键 `max_tokens` 发送，服务商拒绝
  （HTTP 400/422）时自动去掉扩展字段重试一次。**推理段永不显示给玩家**：只取 `AIMessage.content`，
  `reasoning_content` / `response_metadata` 一律不进台词。
- **情绪增量改为结构化输出**：情绪微调改用 `with_structured_output`（`function_calling` 方式——DeepSeek 等
  服务商不认 `response_format.json_schema`，工具调用则是公共能力），拿不到时静默回落到原有的正则解析，
  行为与旧版一致。
- **发言侧工具（默认关闭）**：新增 `backend\app\tools.py`（`katago_analyze` / `joseki_lookup` / `board_query`）
  与受限 tool loop——只在「提示」与「落子宣告」两个回合启用，工具次数有硬上限（默认 `2` 次、单工具 10 秒超时）、
  工具输出永不直接当台词；**只影响她怎么说，不影响她下哪里**（落点仍由后端算棋决定）。新增配置
  `tools_enabled` / `max_tool_calls`；客户端新增 `tool` 事件，只用于状态栏「她正在查资料…」提示。
  见第四节「工具（发言侧）」。

### v3.1.0
- **KataGo 独立成云服务**：新增 `katago_service\`（FastAPI + 常驻 `katago analysis` 子进程）：`POST /analyze`（请求/响应即引擎原文）、`GET /health`（`warming` / `ready` / `dead`）、可选 Bearer 鉴权、启动即后台预热（OpenCL 首次调优挪到部署阶段，端口不会被预热堵住），另有 `run_server.bat` 与 `--host/--port/--token/--katago-exe/--model/--config/--analysis-threads` 入口 + `KATAGO_*` 环境变量兜底。部署见第六节「KataGo 服务部署」。
- **后端改为 HTTP 客户端**：`backend\app\engine.py` 重写为连 `/health` + `/analyze` 的客户端（就绪判定只看 `state`，不认 `ok`；超时/连接失败重试 1 次后返回可读错误），不再持有引擎子进程——`agent.py` / `prompts.py` 一行未改。子进程时代的两条坑（stderr 必须持续排空、读行缓冲放大到 64MB）随引擎搬进 `katago_service\proc.py`。
- **配置换血**：后端与模板里的 KataGo **引擎路径三件套**（可执行文件 / 权重 / analysis 配置）换成 `katago_url`（默认 `http://127.0.0.1:8100`）/ `katago_token` / `katago_timeout_ms`；`game\config.json` 模板与 README 字段表同步，`AzureConfig.save()` 改为把这三个旧键**自我清理**（此前为兼容后端共用同一份配置而刻意保留）。后端不再解析引擎路径（`_resolve` 删除），`GET /api/health` 增加 `katago_url`。
- **服务不可用的表现不变**：KataGo 服务没起 / 地址写错 / 预热中，后端都给出可读中文提示且 `busy` 必归位，棋盘不会永久锁死；`katago_dead` 在**启动探活失败**或**分析时传输层失败**（连接被拒 / 连接或读取错误）时置位、一次成功分析会清除它，`katago_error` 每次失败刷新，两者成对读（见第六节）。

### v3.0.0
- **只保留远程后端**：客户端删除 `AzureAgent / AzurePrompts / KataGoEngine / LLMClient / GoBoard / GameState / Joseki* / EmotionModel` 与端到端测试脚本，装配路径收敛为 `NetClient`（REST 命令 + WebSocket 事件）；`config.json` 删掉运行方式键（`local` / `remote`）、`katago_*`（前端侧）、`llm_auto_start/exe/model_path/port`、`tts_auto_start/bf16/short_prompt`、`vlm_*` 等只服务本机部署的键，旧配置读入后会在下次写回时自我清理，不会崩溃、也不会把旧键写回文件。
- **补齐后端的 WebSocket 依赖**：`backend\requirements.txt` 增列 `websockets`。此前只有裸 `uvicorn`，不含任何 WS 实现，`/ws` 的升级请求会被拒成 404 —— 远程模式还是可选项时被掩盖，v3.0.0 让它成为唯一运行方式后就成了致命问题（客户端只会一直显示「未连接后端」）。
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

---

## 十、致谢

- **[@CarrotMissileVehicle](https://github.com/CarrotMissileVehicle)** —— 本版本的**前后端分离重构**由其贡献：
  通过 [PR #1](https://github.com/AzureEstrath/Azure-Go/pull/1) 让 `game\` 只负责渲染与输入、`backend\` 统一编排、
  `katago_service\` 独立部署，并引入 LangChain 提示链、发言侧工具与长期记忆。特此感谢。
- 感谢 **KataGo**、**CosyVoice**、**Godot**、**LangChain** 等开源项目，本项目建立在它们之上。
- 感谢每一位提交 issue / PR、以及下载试玩并反馈的使用者。
