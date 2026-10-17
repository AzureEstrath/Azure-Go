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
> 当前版本 **v3.5.2**：**语音链路整体搬到后端**——说话文本的清洗 / 分句 / 语气标记剥离
> （`backend\app\speech_text.py`）与云端合成 + LRU 缓存 + 失败降级（`backend\app\tts.py`）都在后端完成；
> 后端在每条要念的台词之后**紧发一条 `speech` 事件**（`turn_id` + `seq` 配对，音频按 `id` 经
> `GET /api/tts/audio/{id}` 取回），客户端 `TTSClient` 退化为**播放器**，配对与兜底交给
> `SpeechPairing` 状态机（任何已登记的台词最终一定会被显示一次）；合成失败一律降级为
> **Windows 系统语音**，文字永不消失。新增 `tts_timeout_sec`（默认 30 秒）。
> v3.5.1 修掉「她还在念上一句时发聊天，回复被扣在『Azure 思考中……』里、要再发一条才显示」；
> v3.5.2 修掉「她忙时发的聊天被静默丢掉」——先排队、右下角显示条数与队头，她说完自动发出。
> 详见第五节「接语音」与「九、更新」。

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
│   │   ├─ services\        TTSClient.gd     语音播放器（按后端 speech 指令出声）
│   │   │                   SpeechPairing.gd 台词与语音的配对状态机（兜底不丢字）
│   │   ├─ core\            AzureConfig.gd   config.json 读写
│   │   │                   Coords.gd        坐标换算（棋盘坐标标签）
│   │   └─ net\             NetClient.gd     远程后端客户端（REST + WebSocket）
│   ├─ assets\voice\        预制语音：开场欢迎语 + 20 条语气词（即播零等待，与情绪/表情绑定）
│   ├─ tests\               语音测试脚本（test_tts.gd / test_speech_pairing.gd / test_config.gd）
│   ├─ config.json          客户端参数模板（见第三节）
│   └─ export_presets.cfg   Godot 导出预设（个人导出用）
├─ backend\                 远程后端（唯一权威：对局编排 / LLM / 情绪 / 定式；FastAPI + REST + WebSocket）
│   ├─ app\                 对局编排 / LangChain 链（LLM + 提示词） / 情感模型 / 发言侧工具 / 长期记忆 / 语音（清洗 + 合成）
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
│   └─ sherpa_voices.json   轻量音色清单（小雅 / 少女 / 女声）
└─ docs\                    后端化决策与实现计划
```

**模型与资产**（体积大，不随源码仓库分发，见 GitHub **Releases**）：
`katago.exe` + `*.dll`、`model-b18c384nbt.bin.gz`（KataGo 权重）——这两个归 **KataGo 云服务的部署侧**持有
（见第六节「KataGo 服务部署」），本仓库只含连它的客户端；
`Azure.vrm`（形象）、CosyVoice 权重（`pretrained_models` / `sherpa_models`）。

---

## 二、快速开始

最短路径（细节见下方 2.1~2.7）：

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
OpenAI 的音色，才能真正出声；**合成在后端**，云端不可用（连不上 / 超时 / 服务端报错）时后端会自动**整条降级**
为 **Windows 系统语音**，文字照常显示。
聊天/讲解需要按第四节接上一个大模型 API。

### 2.1 首次运行清单（一次性准备）

| # | 准备项 | 说明 |
| --- | --- | --- |
| ① | **Python 环境** | `pip install -r backend\requirements.txt`（`fastapi` / `uvicorn` / `websockets` / `httpx` / `langchain-core` / `langchain-openai` / `langgraph`）与 `pip install -r katago_service\requirements.txt`（`fastapi` / `uvicorn`，`httpx` 只给测试用） |
| ② | **KataGo 云服务部署侧** | 备好 `katago.exe` + 配套 `*.dll`、权重 `model-b18c384nbt.bin.gz`、analysis 配置文件（如 `analysis_example.cfg`）——**都不入库**（见第一节「模型与资产」与第六节「KataGo 服务部署」） |
| ③ | **客户端** | **Godot 4.6** 打开 `game\` 工程；`Azure.vrm` 形象（`vrm_path` 建议绝对路径，见第三节） |
| ④ | **可选（缺了也能跑）** | 云端 LLM 的地址 / 模型名 / Key（第四节）、云端 TTS 三件套 + Key（第五节）、embedding 三件套（长期记忆，第四节）、自建 CosyVoice（第五节） |

> `websockets` **不能省**：裸装 `uvicorn` 不带 WebSocket 实现，缺了它 `/ws` 会拒绝升级（404），
> 客户端只会一直显示「未连接后端」（见第六节）。

**最小可跑组合**：只保留 `backend_url` 的默认值 `http://127.0.0.1:8000`，按 2.2 把 **KataGo 云服务与后端**
起起来（服务侧要备好引擎与权重），就能对局——她照常算棋、应手、给提示。此时：
**讲解 / 闲聊退化为兜底台词**（没接 LLM，见第四节）；**语音走 Windows 系统语音**（云端 TTS 没配 Key 时后端
整条降级，见第五节；把 `tts_base_url` 留空则整条不出声、只出文字）。

### 2.2 启动顺序与就绪判定

按这个顺序起，前一步就绪了再起下一步：

1. **KataGo 云服务**：双击 `katago_service\run_server.bat`（脚本自己 `cd` 到仓库根目录，**必须原样跑**；
   等价命令与参数见第六节「KataGo 服务部署」）。默认监听 `127.0.0.1:8100`。
2. **等它就绪**：轮询 `curl http://127.0.0.1:8100/health`，直到 `"state": "ready"`。
   服务启动即后台预热（加载模型 / OpenCL 首次调优，可能几十秒到几分钟），预热期 `state` 是 `warming`
   （此时 `/analyze` 立刻回 503 可读错误，不会挂到客户端超时）。
3. **后端**：双击 `backend\run_server.bat`（内部 `cd` 到 `backend\` 再 `python -m app.main`），
   监听 `127.0.0.1:8000`。控制台会打印「Azure 后端启动...（KataGo 走云服务，LLM 走云端）」。
4. **游戏**：`Godot_v4.6.3-stable_win64.exe --path game`。

两个「看起来像坏了」的现象：

- **预热期起后端 ⇒ 后端最多 180 秒不监听任何端口**：后端在 lifespan 里等 KataGo 就绪
  （代码常量 `READY_TIMEOUT_MS`＝180 秒，不可配置）才绑端口。这段时间 `/api/state`、`/ws` 都不可达，游戏只显示
  「未连接后端（…），N 秒后重试…」——看起来像后端根本没起来，其实它在等预热；预热完成、后端绑上端口后
  客户端会自动重连。
- **服务侧没配引擎路径 ⇒ 服务以可读中文提示 + 退出码 2 结束**：控制台打印
  「`[KataGo服务] 启动失败：create_app 的 cfg 缺少引擎路径：…`」与「请用 `--katago-exe/--model/--config`
  或环境变量 `KATAGO_EXE/KATAGO_MODEL/KATAGO_CONFIG` 指定引擎」，然后进程退出。若路径给了但引擎起不来
  （路径错 / 缺 dll / 显存不足…），服务仍在跑，但 `/health` 的 `state` 是 `dead`（`katago_error` 给原因），
  `/analyze` 回 503。

### 2.3 界面与操作

**视角与鼠标**（左侧栏默认**收起**，右侧栏默认展开；两侧各有一个贴边按钮收起 / 展开对应面板，
按钮文案在 `▶` / `◀` 间随状态切换）：

- **左键**：落在交叉点上＝落子（她思考中或还有话没说完时棋盘锁着，点不动）；落在她头部附近＝**摸摸头**
  （开心 + 微微脸红 + 一声语气词，状态栏「你摸了摸 Azure 的头…」）。
- **右键拖拽**：旋转视角（松开后她先看你约 1 秒）。
- **滚轮**：缩放（上推拉近 / 下拉拉远）。

**右侧栏**（标题「♟️ Azure · 围棋陪练」，副标题「青蓝色 · 永恒17岁 · 天才少女 · 右键拖拽旋转视角 · 滚轮缩放」）：

| 控件 | 行为 |
| --- | --- |
| `手数: N` / `黑胜率: xx.x%` / `目差: ±x.x` | 顶部信息栏；未算出时为 `--`。手数与俗手列表随棋盘快照刷新，胜率 / 目差在她落子后与「局面分析」时刷新 |
| `允许 Azure 虚着`（复选框） | 勾上＝这局她可以虚着；即时生效（`POST /api/config`） |
| `定式导学`（复选框） | 识别你与她走出的定式（讲解 + 棋盘高亮手顺）；关掉＝不识别不打扰。写回 `config.json` 并对当前对局即时生效 |
| `短回复` | 限制她每次只说 1~2 句（点评 1 句 / 聊天最多 2 句）→ 经 `/api/config` 转发后端，**立即生效** |
| `同步说话` | 开启后她**这一轮还在跑**的朗读台词，文字等这条语音**开始播放**后再显示（落子宣告的揭示也跟着这一刻走）；**回合已经结束**的台词（聊天 / 闲置搭话 / 非法手提示）**当场显示**——她上一句可能还在念，傻等它就会让回复一直挂在「Azure 思考中……」里，要再发一条才冒出来（v3.5.1 修）；**取音频失败 / 合成超时 / 超过 45 秒配对窗口**时同样兜底立即显示（不卡字、不丢字）→ **立即生效** |
| `整段合成` | 她连发的多条台词合并成一段连续朗读 → **立即生效** |
| `长句合并` | 后端单次合成字数上限 `100` → `160`（关＝首句更快）→ **重启后端后生效** |
| 输入框 `和Azure聊天...` + `发送` | 自由聊天（最多 200 字，回车与按钮同效）；她的消息逐字显示，等待时先出现「Azure 思考中……」占位气泡。**她忙时（正在想 / 还在说话）你发的这句先排队**：输入框下面那行显示「排队中 N 条 · 队头前几个字」，等她上一句说完、语音也播完就**自动**发出去（不用你再发一条）；最多排 `5` 条，满了会保留你输入框里的原文并提示，绝不吞话 |
| `💡 提示` | 方向性提示，不直接说坐标（`POST /api/hint`） |
| `📊 局面分析` | 以她的视角解读当前局势（`POST /api/analyze`） |
| `🔄 重来` | 弹确认框（可填备注）→ 清空棋局与俗手记录（`POST /api/reset`） |
| `🏁 结束` | 弹总结框（可填备注、可留空直接确定）→ 对局总结（`POST /api/summarize`） |
| `⏪ 俗手回档（点一下回到那手之前）` | 列出最近最多 3 手俗手（按钮文案 `第N手 <坐标>`，悬停显示原因）；点一下回档到那一手之前（连带撤掉她的应手）；没有记录时显示「暂无俗手记录」 |
| 底部状态栏 | 见下方状态栏文案表 |

> 她忙时（思考 / 说话）`💡 提示` / `📊 局面分析` / `🔄 重来` / `🏁 结束` 四个按钮置灰，棋盘也锁着；
> 俗手回档按钮不禁用，但点击时若她正忙则不生效。

**左侧栏「🔊 语音朗读」**（默认收起）：

- `朗读 Azure 的话`：语音总开关（写回 `config.json`；关掉即刻停止播放与取音频，**后端重启后**才真正零请求、零 `speech` 事件；文字照常）。
- `朗读方式` 下拉：`系统语音` / `CosyVoice OpenAI 接口`（对应 `system` / `openai`；**重启后端后生效**）。
- `服务地址（上面第 2 项才需要）` / `模型` / `音色（可下拉选）`；`重填音色候选` 按钮 + 可手填音色 id 的输入框。
  下拉候选是**本地静态列表**：`alloy` / `echo` / `fable` / `nova` / `onyx` / `shimmer` / `中文女` / `粤语女` / `英文女`，
  也可直接手填服务商的音色 id。
- `语速`（0.5~2.0）/ `音调（播放层变调，会略带动语速）`（0.8~1.3）/ `音量`（0.0~1.5）三个滑块。
- `▶ 试听`：`POST /api/tts/speak` 合成一句话再播（**不看**朗读开关是否打开；系统语音模式下会回「当前语音方式不产生音频」）/
  `■ 停止`。
- 左侧栏底部另有一行语音状态（如「朗读方式：…（合成在后端，重启后端后生效）」）；只有朗读开始 / 完成、试听与出错这类状态会**同时镜像**到底部状态栏。

**开场「⚙️ 出发前的准备」面板**（无窗口模式与截图 / `--demo` 模式会跳过；用 `-- --setup` 可只把面板画出来）：

- `思考模式（只影响 Qwen3 这类思考型模型）` 下拉：`快速：不思考，首字 1~2 秒（推荐）` / `中等：先想几句再答，约 3~6 秒` /
  `强思考：先推理再回答，更慢、质量更好`（对应 `llm_think_mode` 的 `0` / `1` / `2`）。
- 可折叠分节 `LLM 对话 API（点评 / 聊天）`：`服务地址` / `模型名` / `API Key（只有云端需要）`。
- 可折叠分节 `语音 TTS API`：`服务地址` / `音色`。
- `▶ 开始对局`：确认后**写回 `config.json`**（地址 / 模型名 / 音色留空时保留原值，Key 可以清空），
  随后才按新配置起各服务。面板里**没有**的字段（如 `tts_model` / `tts_api_key`）保持原样——要改它们得手动编辑
  `config.json` 或用左侧栏。

**状态栏文案**（底部那一行，左侧栏收起时也能看到）：

| 文案 | 含义 |
| --- | --- |
| `后端模式：编排与引擎在 <地址>（本地不启动 KataGo/LLM）` | 启动后的默认文案 |
| `未连接后端（<地址>），N 秒后重试…` | WS 连不上（后端没起 / 端口不对 / 缺 `websockets` / 后端在等 KataGo 预热），退避重试 |
| `后端已连接：<地址>` | WS 已连上 |
| `她正在查资料…` | 发言侧工具在跑（仅 `tools_enabled` 开启时），工具结束恢复原文案 |
| `语音：<…>` | 语音状态与错误（如「这段语音的音频取回失败（…），改为只显示文字」） |
| `定式导学已开启 / 关闭`、`语音：短回复已开启` | 开关反馈（底栏文案）；带生效范围的那条在**左栏**：`短回复：开（立即生效）` / `长句合并：开（重启后端后生效）` |
| `你摸了摸 Azure 的头…` | 摸头反馈 |

### 2.4 一手棋的完整流程（含音画同步）

1. **你落子**（左键）：客户端先乐观画一颗黑子，同时 `POST /api/move`。
   > 落子不成立（超出棋盘 / 此处已有棋子 / 打劫禁着点 / 自杀禁着点）时，后端**先**推一份权威快照让界面撤销那颗子，再说一句
   > 「这手不成立：<原因>」。这条提示**不走 `busy`**、也**带语音**（见第五节「已知取舍」的错配窗口说明）。
2. **后端算两个局面**：你落子**前**（缓存复用，用于给你这手排名）与落子**后**（用于选她的应手）；
   定式识别（纯本地计算、零引擎调用）与情绪结算也在这时完成。
3. **她的点评**（事件 `ai_comment`）：按你这手的排名给评价；若这手让某个入门定式**成型**，点评直接换成
   **定式讲解**（不额外增加 LLM 调用）。
4. **她的用意 + 落子宣告是同一条台词**（事件 `ai_move`，带落点坐标）：这句话说清她准备下在哪、想达成什么。
   她的**落点由后端算棋决定**（取引擎候选里第一个合法点；不允许虚着时绝不虚着）。
   开了发言侧工具（`tools_enabled`）时，模型在工具轮里报出的落点可当候选，但要过三条边界——候选点必须过棋盘合法性、
   `pass` 只在允许虚着时可用、候选不合法就回退后端算棋的默认落点；**任何情况下都会产出可用落点**
   （见第四节「工具（发言侧）」）。
5. **先落到内部棋谱**：她这一手在发宣告时就已落进后端棋谱，但**棋盘上的可见更新延后**。
6. **音画同步**：客户端等这条「落子宣告」的**语音开始播放**时才把棋子揭示到棋盘（`POST /api/reveal`）。
   语音不可用 / 没播时，回合结束会兜底揭示一次，不会一直不显示。
7. **落子后感想**（事件 `ai_comment`）+ 右侧栏形势栏刷新。

几条配套行为：

- **俗手记录**：你这手**不在她算的前 5**，或**胜率下滑超过 10%**，会被记为俗手（右侧栏「俗手回档」按钮，
  最多保留最近 3 手）。点按钮回档到那一手之前——连同她的应手、俗手记录、定式去重记录一起回滚，
  所以悔棋重走那手可以重新讲解。
- **定式识别**：成型时棋盘按手顺点亮高亮轨迹；你偏离已吻合的定式时只点「常规一手」那个位置，并轻轻提一句。
- **总结写长期记忆**：`🏁 结束` 出的**真总结**（不是兜底台词）会把棋谱 + 结果 + 总结写进长期记忆
  （`memory_enabled=true` 时；见第四节「长期记忆（跨局记得你）」）。
- **锁**：她思考 / 说话期间棋盘锁着，`busy` 必归位是设计保证（见 2.6 最后一条）。

### 2.5 语音与情绪

- **合成在后端、前端只播放**（见第五节）：后端每发一条要念的台词都**紧接**一条 `speech` 事件
  （`turn_id` + `seq` 配对）。`mode=openai` 时前端按 `id` 去 `GET /api/tts/audio/{id}` 取音频播放；
  `mode=system` 时把后端**已清洗好**的 `text` 交给 Windows 系统语音。
- **两种模式**：`openai`（云端 OpenAI 兼容 `/v1/audio/speech`，默认）/ `system`（Windows 系统语音，零配置即可发声）。
- **失败整条降级**：连不上 / 超时 / 服务端拒绝 / 响应解析不了 / 单条音频超过缓存预算，后端把这一条**整条**改发
  `system` 指令（`speech.mode` 变 `system`），**文字永不消失**、回合不卡；下一句仍会重试云端。
  后端控制台留一行 `[TTS] …` 便于排查（合成失败 / 单条超预算）。
- **缓存**：条数 **32** / **8 MiB** 双向 LRU。被淘汰或后端重启后取不到音频 ⇒ 这一条只显示文字（不阻塞）。
- **语气词**：`assets\voice\` 下的预制语气词与情绪 / 表情绑定（如 `sigh` / `cheer` / `proud` / `think` / `pat`）；
  `think` 在每次玩家互动后的额度是 **1 次**，同一情绪 **30 秒**内延续不重复出声；只在对局互动中插，
  开场问候与闲置搭话不插。
- **开场欢迎语**：`assets\voice\welcome_<名字>.wav`（名字取 `prompt_user_name` 的小写；模板里是
  `welcome_estarth.wav`），有就零等待直播、没有就退回普通流程（纯文字）。
- **`[[tone:…]]` 由后端剥净**：语气标记（`sigh` / `cheer` / `proud`）只在后端解析，并**整段**从朗读文本里删掉，
  **不会被念出来**；未知标记照删但不出声。
- **情绪区域怎么影响她**：情绪区域（愉悦度 × 好奇度 × 唤醒度 → 27 区）决定**表情形变**（持续层 + 一次性强调）、
  **区域对应的语气词**与**本句音高微调**（`speech.pitch`，叠加在播放音高上，不改你的配置）。

已知取舍（4 条短提示只有文字没有语音、`busy` 会被合成占住、两栏生效范围不同、两条前端固有的错配窗口）
见第五节「已知取舍」，这里不重复。

### 2.6 常见问题排查

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 游戏一直显示「未连接后端（…），N 秒后重试…」 | ① 后端没起；② `backend_url` 地址 / 端口不对；③ 后端缺 `websockets`（`/ws` 升级被拒 404）；④ 后端还在等 KataGo 预热（最长 180 秒不监听端口） | 起后端（`backend\run_server.bat`）；核对 `backend_url`（默认 `http://127.0.0.1:8000`）；`pip install -r backend\requirements.txt` 补 `websockets`；若后端控制台停在等 KataGo，先 `curl http://127.0.0.1:8100/health` 确认 `state == "ready"`（见 2.2） |
| 她有话，但没声音 | ① `tts_enabled=false`；② `tts_mode` 与三件套不配套（如用 `中文女` 调 OpenAI 会被 HTTP 400 拒）；③ `tts_api_key` 空；④ 合成超时（`tts_timeout_sec`）；⑤ 音频被 LRU 淘汰或后端重启过（`GET /api/tts/audio/{id}` 404）；⑥ 走了系统语音（`speech.mode=system`） | 看底部状态栏「语音：…」与后端控制台 `[TTS] …` 行；按第三节把三件套配成**同一家**服务商的取值；`system` 模式本来就是系统语音出声，检查 Windows 音量与音色 |
| 完全没有声音，系统语音也没声 | PowerShell / SAPI 被策略拦掉（前端用 `OS.create_process("powershell")` 调系统语音，起不来就念不出） | 状态栏会提示「系统语音不可用（无法启动 PowerShell），这条改为只显示文字」；放行 PowerShell，或改用云端 `openai` 模式 |
| 形象不显示 | `Azure.vrm` 缺失，或 `vrm_path` 是相对路径、Godot 解析不到 | 按 2.1 ③ 放好 `.vrm`，并把 `vrm_path` 写成**绝对路径**（见第三节） |
| 她算不清，提示说「嗯...我这边一时算不清，稍等再试吧~」 | KataGo 云服务不可用（没起 / 地址或令牌不对 / 引擎 `dead`） | 读 `GET http://127.0.0.1:8000/api/health` 的 `katago_dead` + `katago_error`，**成对读**（见第六节）：`dead=true`＝引擎（或到它的链路）真没了；`dead=false` + 新的非空 `error`＝服务在线但这次没答好。再去看服务侧 `GET http://127.0.0.1:8100/health` 的 `state` |
| 讲解 / 闲聊变成「唔…我脑子有点转不过来…」这类兜底台词 | LLM 没接 / 401 / 429 / 超时；或请求体扩展字段被服务商拒（HTTP 400/422） | 核对 `llm_base_url` / `llm_model` / `llm_api_key`（第四节）。扩展字段被拒时后端会自动去掉重试一次、且本次会话不再发送（设计好的降级）；要彻底避免就把 `llm_extra_body` / `llm_think_extra_body` 留空 |
| 长期记忆没生效 | `memory_enabled=false`；或改了字段没重启后端 | 置 `memory_enabled=true` 后**重启后端**；`GET /api/health` 的 `memory_rows` 一眼看出库里有没有东西（见第四节） |
| 开了 `stream_tokens` 却看不到逐字效果 | 它默认 `false`，且后端只在**启动时**读 | 置 `stream_tokens=true` 后**重启后端**（第四节「流式输出」） |
| 棋盘像被锁住、点不动 | 她正在思考 / 说话（`busy`，或被语音占住） | 等一会儿；`busy` 必归位是设计保证（后端 `try/finally` + 前端每帧清锁），不会永久锁死（第五节「已知取舍」） |

### 2.7 开发者：跑测试与调试

**测试**（后端与 KataGo 服务都用标准库 `unittest`，不用 pytest）：

```bash
cd backend && python -m unittest discover -s tests          # 后端全量
cd katago_service && python -m unittest discover -s tests   # KataGo 云服务全量（假引擎，不依赖真实 KataGo 与公网）
godot --path game --headless --script res://tests/test_tts.gd              # 语音播放器
godot --path game --headless --script res://tests/test_speech_pairing.gd   # 语音配对状态机（纯逻辑）
godot --path game --headless --script res://tests/test_config.gd           # 客户端配置读写
```

> 后端跑前先 `pip install -r backend\requirements.txt`。`test_tts.gd` 会真的拉起一次系统语音进程（音量已压到最低）；
> 另外两条前端门卫纯逻辑、无外部依赖。

**调试参数**（走 `OS.get_cmdline_user_args()`，**必须放在 `--` 之后**）：

```bash
Godot_v4.6.3-stable_win64.exe --path game -- --shot=out.png    # 渲染数帧后截图退出
Godot_v4.6.3-stable_win64.exe --path game -- --demo            # 摆几颗示例棋子（纯视觉核对，不走引擎）
Godot_v4.6.3-stable_win64.exe --path game -- --setup           # 只画设置面板供截图
```

> 另有 `--yaw=` / `--pitch=` / `--dist=` / `--tilt=` 调相机与形象，`--panels=both|left|right` 调面板开合；
> `--shot` / `--demo` 会跳过启动设置面板（配 `--setup` 则强制画出）。

**日志位置**：

| 位置 | 内容 |
| --- | --- |
| 后端控制台 | `[KataGo]` / `[TTS]` / `[Agent]` / `[Memory]` / `[WS]` 前缀的运行日志与降级原因 |
| Godot 控制台（客户端） | `[Setup]` 启动设置已保存 / `[TTS]` 取音频失败与配对清出 / `[chat]` 防复读 |
| `katago_service\logs\katago\` | 引擎自己的日志（服务用 `-override-config logDir=…` 指到这里） |
| `server\server_run.log` | 自建 CosyVoice 语音服务的日志（VBS 隐藏窗口启动，只能看文件） |

**改 `config.json` 后要不要重启后端**：见第三节的「读取方」列——标「后端（启动时）」的字段
（LLM / 提示词 / `katago_*` / `tts_timeout_sec` / `tools_enabled` / `stream_tokens` / `memory_*` /
`embed_*` / `tts_long_chunk` 等）改完都要**重启后端**；标「客户端 + 后端（启动时）」的
`tts_enabled` / `tts_mode` / `tts_voice` / `tts_speed` **也要**重启后端（前端那半边立刻生效，后端那半边不会）；
标「实时」的（`joseki_teach`、`tts_limit_len`）不用。**纯客户端**字段（`backend_url` / `vrm_path` /
`tts_pitch` / `tts_volume` / `tts_sync_speak` / `tts_batch_speak`）与后端无关。`tools_enabled` 另可经
`POST /api/config` 实时切换（第四节；客户端界面暂未提供入口）。

---

## 三、`config.json` 字段

**这个文件前后端共用**：放在**可执行文件同目录**即可覆盖包内默认值（源码运行时读 `game\config.json`）。
下表「读取方」一列标出谁读它、什么时候读：

- **后端（启动时）**：`backend\run_server.bat` 启动时一次性读入，**改完必须重启后端才生效**
  （游戏内设置面板写回的 LLM 地址 / 模型名 / Key / 思考档位也属于这一类）。
- **后端（实时）**：经 `/api/config` 转发，改完立即生效。
- **客户端**：游戏进程自己读，改完（或下次启动）即时生效。
- **后端专用（客户端不读）**：`katago_url` / `katago_token` / `katago_timeout_ms`、
  `llm_extra_body` / `llm_think_extra_body` / `stream_tokens` / `tools_enabled` / `max_tool_calls` 与
  `memory_enabled` / `memory_db_path` / `embed_base_url` / `embed_model` / `embed_api_key` /
  `embed_timeout` / `memory_top_k` / `memory_max_inject_chars` 只由后端消费；
  语音侧的 `tts_base_url` / `tts_model` / `tts_api_key` / `tts_timeout_sec` 也只由后端消费
  （`tts_long_chunk` 客户端只留一个勾选框、分块本身在后端）；这些键客户端不参与计算，
  但**写回配置时会原样保留**（游戏内保存设置不会把它们弄丢）。

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
| `stream_tokens` | 后端（启动时） | **输出流式**开关，默认 `false`：开启后她的台词逐段推送（WS `token` 事件），首字更快；**气泡最终内容仍以完整 `message` 为准**（见第四节「流式输出」） |
| `tools_enabled` | 后端（启动时） | **发言侧工具**总开关，默认 `false`：开启后她在「提示」与「落子宣告」两个回合可自主复算局面 / 查定式 / 核对盘面；工具轮里模型报出的落点也能**参与落点候选**（见第四节「工具（发言侧）」） |
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
| `tts_enabled` | 客户端 + 后端（启动时） | 是否启用语音；后端据此决定要不要合成（关闭时零请求、不发 `speech` 事件，文字照常） |
| `tts_mode` | 后端（启动时）+ 客户端 | `openai`（云端 OpenAI 兼容 `/v1/audio/speech`，默认）/ `system`（Windows 系统语音）；后端按它决定合成路由，客户端只作本地镜像（两边都把未知值回落 `openai`） |
| `tts_base_url` | 后端（启动时） | **云端语音服务地址**（OpenAI 兼容，含 `/v1`，如 `https://api.openai.com/v1`）；`openai` 模式留空＝视为未启用（只出文字） |
| `tts_model` | 后端（启动时） | 云端语音模型名 |
| `tts_voice` | 后端（启动时）+ 客户端 | 音色 id（随服务商而定，也可手填）；后端用它合成，客户端系统语音兜底时也拿它选音色 |
| `tts_api_key` | 后端（启动时） | **云端 TTS 的 API Key**；非空则发送 `Authorization: Bearer <key>` |
| `tts_speed` | 后端（启动时）+ 客户端 | 语速：后端合成时传给服务商，客户端系统语音兜底也用它 |
| `tts_pitch` / `tts_volume` | 客户端 | 音调（播放层变调，会略带动语速）/ 音量 |
| `tts_timeout_sec` | 后端（启动时） | **单次合成超时**（秒，默认 `30`）；`≤0` **钳到 1 秒**、脏值退回默认；超时一律**整条降级**为系统语音，回合不卡死 |
| `tts_limit_len` | 客户端 + 后端（实时） | **短回复**（限制她每次只说 1~2 句）：客户端勾选后经 `/api/config` 转发后端，**立即生效** |
| `tts_sync_speak` | 客户端 | **同步说话**：开启后她的**朗读台词**（含聊天与闲置搭话）文字等这条语音**开始播放**后再显示；**取音频失败 / 合成超时 / 超过 45 秒配对窗口**时会兜底立即显示文字（不卡字、不丢字）。聊天与闲置搭话看起来接近即时，是因为它们的回合在发话前就已解锁、前端会立刻放字——不是因为音频已就绪；**立即生效** |
| `tts_batch_speak` | 客户端 | **整段排队播报**：连发的多条台词合并成一段连续朗读（**立即生效**）；语义变化见下方说明 |
| `tts_long_chunk` | 后端（启动时） | **长句合并**：后端单块字数上限 `100` → `160`（关 = 100，首句更快）；改完要**重启后端** |

> - `tts_base_url` / `tts_model` / `tts_voice` 要**配套**：模板的三件套是 `https://api.openai.com/v1` + `tts-1` + `中文女`，
>   其中 `中文女` **不是** OpenAI 的音色 id——用 OpenAI 的话要把音色一并改成 `alloy` / `nova` / `onyx` 之类的
>   OpenAI 音色（音色 id 也可手填）；换其他服务商时地址、模型名、音色 id 都按服务商的取值改。
> - **合成在后端，失败一律降级、不出错刷屏**：服务**连不上**（断网 / 地址写错 / 服务没启动）、**超时**、
>   服务端**拒绝了这次请求**（音色 / 模型名不被接受，例如用 `中文女` 调 OpenAI 返回 HTTP 400）或响应解析不了，
>   后端都把这一条**整条降级**为「用 Windows 系统语音念」的 `system` 指令（`speech` 事件的 `mode` 变成 `system`），
>   文字照常显示、回合不卡；下一句仍会重新尝试云端合成，服务恢复后自动回到云端（不报错、不改配置）。
>   后端控制台会留一行 `[TTS] …` 的降级日志（合成失败 / 单条音频超过缓存预算）便于排查。
> - **`tts_long_chunk` 现在是后端的分块开关**：它决定后端单次合成的字数上限（关 = 100 / 开 = 160），
>   改完要重启后端；`tts_batch_speak` 的语义也变了——合成改成「一行一条」之后，它从「一次请求合成一整段」
>   变成**整段排队连续播报**（仍影响观感与时延，但不再减少合成请求数），仍在前端即时生效。
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

### 流式输出（可选，默认关闭）

`stream_tokens: true` 后，她说话时逐段推送（WS `token` 事件，带 `turn_id` 与 `delta`），**首字更快**；
默认 `false` 时一个 `token` 都不发，事件序列与没有这个功能时**逐字一致**。

- **气泡最终内容以完整 `message` 为准**：`token` 只是过程；收口的那条 `message` 要过 `clean_reply()`
  与后端的 **8 条窗口去重闸门**（见「九、更新」v3.4.0）之后才发，整句原样复读时连 `token` 也不发，
  前端不会留下没收口的残余气泡。
- **推理段永不进 `token`**：增量只取模型回答的 content 通道，content 里的 think 段由后端在**累加文本**上
  跨块剥离。成对 `<think>…</think>` 与 `<｜begin…｜>…<｜end…｜>` 形态（含标记被切块）**保证增量与台词都干净**；
  「网关吞掉开始标记、只剩收尾标记」与「未闭合 `think`」这类**畸形**形态下，增量流可能短暂带出原文，
  但**最终 `message` 与语音恒为干净台词**。
- **流式本身失败（连接断开 / 服务端报错）绝不影响这一回合**：自动回落为整段出话，台词必定完整。
- 改完这个字段要**重启后端**才生效。

### 工具（发言侧）

**默认关闭**（`tools_enabled: false`，改成 `true` 即开启）：关闭时行为与没有工具时完全一致。开启后她**只在两个回合**可以自己查资料——
「提示」与「落子宣告」——工具请求由状态图的**条件边工具轮**驱动（模型要工具就先执行、把结果回灌给模型
再来一轮，直到没有请求或预算用尽）。

- **工具可参与落点候选**（本版有意放宽，v3.2.0 时是「绝不影响她下哪里」）：模型在这一轮里报出的落点
  （她这句话里出现的 GTP 坐标或虚着）会被后端认出来当候选，但有三条边界——① 候选点必须过棋盘合法性；
  ② `pass` 只在开了允许虚着时可用；③ **任何情况下都产出可用落点**（候选不合法就回退后端算棋的默认落点），
  **绝不会因为她报了个坏点而这一手不下**。未开启工具时落点仍完全由后端算棋决定。
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

**合成在后端、前端只播放**（v3.5.0 起）：她说什么、怎么切句、什么语气都由后端决定
（清洗 / 分句 / `[[tone:…]]` 剥离见 `backend\app\speech_text.py`，云端合成 + 缓存 + 降级见
`backend\app\tts.py`）。后端每发一条要念的台词，都会**紧接**补一条 WS `speech` 事件，用
`turn_id` + `seq` 与那条 `message` 一一配对；客户端 `TTSClient` 退化为播放器，只按指令出声：

- `speech.mode == "openai"`：按事件里的 `id` 去 `GET /api/tts/audio/{id}` 取回音频播放（后端把合成结果按
  **条数 32 / 8 MiB 双向 LRU** 缓存；被淘汰或后端重启后取不到，这种情况只显示文字、不阻塞）；
- `speech.mode == "system"`：用 `text`（后端已清洗好，**不再含**语气标记）交给 **Windows 系统语音**念；
- 语音关闭（`tts_enabled=false`）时后端**一条 `speech` 都不发**、也不发起任何合成请求，文字照常显示。

**两种模式**

- **云端 `openai`（默认）**：任何 OpenAI 兼容 `/v1/audio/speech` 服务（如 OpenAI、部分云厂商），
  填 `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`——这几个字段现在由**后端启动时**读取
  （改完要**重启后端**），加 `tts_timeout_sec`（默认 30 秒）控制单次合成超时。
- **`system` 兜底**：Windows 系统语音，零配置即可发声；**后端合成失败（连不上 / 超时 / 服务端拒绝 /
  响应解析不了）时后端把这一条整条改发 `system` 指令**（不报错、不改配置），下一句仍会重试云端。
  左侧栏第 1 项「系统语音」也可以主动把整条链路切到它。

**已知取舍**

- **有 4 条短提示只有文字、没有语音**：`undo` 的两条（回档点不存在 / 已回档）与 `reset_game`、
  `set_allow_pass`——它们是同步方法，无法 `await` 合成；文字照常显示。
- **`busy` 会被合成占住**：`speech` 事件要带上音频 id，就必须**先合成、后发事件**，所以后端在一行
  合成完成前一直 `busy`（`/api/undo`、`/api/reset` 等会据此被挡下，返回 `busy` 而不错乱棋局）——
  最坏每行数秒，`player_move` 这种一次要念数行的会叠加。这是「合成归后端」的固有设计（计划 05 · Task 3）。
- **两栏的生效范围不同**：朗读方式 / 服务地址 / 模型（左栏「语音设置」）与长句合并（右栏「语音开关」）
  现在只对**后端重启**生效（合成归后端，前端不再自行合成）；「同步说话」「整段合成」「短回复」仍是
  前端 / 请求级**即时生效**（`tts_limit_len` 经 `/api/config` 转发后端）。界面在这些项上会提示
  「重启后端后生效」。
- **`speech` 与 `message` 的配对**：后端保证 `speech` **紧随**对应 `message` 之后发出；`seq` 是
  **同一个 `turn_id` 内**从 1 开始的序号，**跨回合会重置**——客户端必须按 `turn_id + seq` 配对，
  不能只看 `seq`。
- **两条前端固有的错配窗口**（音频序列本身仍然正确，只是**归属 / 顺序**可能错一位）：
  ① 「这手不成立」是唯一一条 `busy=false` 且带语音的台词，玩家可以马上再落一手与它交错；
  ② 本地被去重的台词会留下一个占位（tomb）却永远收不到 `speech`，它可能「用掉」下一条的语音。
  两者都有自愈：被顶掉的那句仍会在回合结束时放出文字，**不锁死棋盘、不静音全局**。
  后续选项（暂未实现）：让这条提示留在 `busy` 窗口内，或给 `message` 也带上 `turn_id`。
- **流式的保证边界**见第四节「流式输出」（成对 think 区间保证 `token` 与台词都干净；「只剩收尾标记」与
  未闭合 `think` 的**畸形**形态只保证最终 `message` 与 TTS 干净）。

**两个语音端点**（对局中由 `speech` 事件驱动，这两个是**试听 / 手动**用的；失败只回可读错误，不 500）：

```bash
# 试听：直接返回 audio/wav；合成不出音频（语音关闭 / 系统语音模式 / 空文本 / 合成失败）→ 409
curl -X POST http://127.0.0.1:8000/api/tts/speak -H "Content-Type: application/json" \
  -d '{"text":"你好呀"}' -o preview.wav
# 取一条已合成的音频（speech 事件 mode=openai 时带的 id）；未命中 / 已被淘汰 / 后端重启过 → 404
curl -o line.wav http://127.0.0.1:8000/api/tts/audio/音频id
```

> `-d '…'` 的单引号写法在 **PowerShell 7 / Git Bash** 可用；**cmd.exe** 要改写成 `-d "{\"text\":\"…\"}"`。

**自建 CosyVoice 也走 `openai`**：`server\` 的 `azure_tts_server.py` 同样实现了 OpenAI 兼容的
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
    以及 `GET /api/state`、`GET /api/health`，长期记忆管理面 `/api/memory/query|write|stats|forget`
    （见第四节「长期记忆（跨局记得你）」），语音的试听 / 取音频两个端点 `POST /api/tts/speak`、
    `GET /api/tts/audio/{audio_id}`（见第五节「接语音」）。
  - WebSocket：`/ws` 推送 `hello / message / state / info / busy / mood / joseki / tool / token / speech / memory`
    （`tool` 是发言侧工具的开始 / 结束，只喂状态栏提示，不进入对话；`token` 是可选流式增量，仅
    `stream_tokens` 开启时才有、默认关闭，气泡最终内容仍以随后的 `message` 为准；`speech` 是每条台词的
    **语音指令**——`mode=openai` 时按 `id` 取上面的音频端点、`mode=system` 时用 `text` 交系统语音，与对应
    `message` 同 `turn_id`、`seq` 递增配对，语音关着时根本不发；`memory` 是记忆管理动作的元数据，
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
  后端在 lifespan 里会等它就绪（代码常量 `READY_TIMEOUT_MS`＝180 秒，不可配置）才绑端口，这段时间里 `/api/chat`、
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

### v3.5.2（当前）
- **修复：她忙的时候发的聊天被后端静默挡下（`POST /api/chat` 回 `{"ok": false, "busy": true}`），
  玩家的话进了聊天栏却永远等不到回复。** 现在改成**先本地排队**：输入框下面那行（右下角）显示
  「排队中 N 条 · 队头前 8 字」，她上一句说完、语音也播完的那一刻**自动**一条接一条发出去——
  不用玩家再发一条来「催」；最多排 `5` 条，满了时**不吞话**（原文留在输入框 + 状态栏提示）。
- **实现**：新增纯逻辑 `game\scripts\net\ChatQueue.gd`（FIFO / 上限 / 提示文案 / 「能不能发」判据，
  门卫 `tests\test_chat_queue.gd` **17 项**）；`NetClient.chat()` 改成吃回包——忙 ⇒ 入队，发出后
  **只有明确 `ok: true` 才算收下**（忙 / 非 2xx / 读不懂的回包一律退回队头，另压 `2` 秒重试闸），
  新增 `chat_queue_changed` 信号与 `flush_queued_chat(env)`（三道门禁：空闲判据 / WS 未连接不发 /
  失败节流），门卫 `tests\test_net_chat.gd` **12 项**；`Main.gd` 只留胶水（提示标签 + 每帧续发 + 不吞话）。
- **已知取舍**：请求其实到了后端、只是回包丢了时，这条会重复发一次（她可能回两遍）——
  宁可极少数情况重复一次，也不吞玩家的话。
- **冒烟**：五支前端门卫全 PASS（`17 / 12 / 87 / 24 / 50`）、`--quit` 无 SCRIPT ERROR、后端 **354 OK**、
  `katago_service` **36 OK**；另跑了「她忙时连发两条」的无头端到端（假后端 + 真 `Main.tscn`）：
  排队 → 右下角提示 → 她说完自动依次发出、提示消失、顺序正确、全程不用手动再发。
- **文档**：`README` 使用说明「输入框」一栏补排队与提示的说明；新增本计划
  `docs\plans\06-chat-queue-when-busy.md`。

### v3.5.1
- **修复：在她还在念上一句时发聊天，回复会被扣住——聊天栏一直挂着「Azure 思考中……」，要等再发一条
  消息才把上一条回复放出来。** 根因：逐句模式下的「同步说话」原来一律让文字等**这条台词自己的语音开播**
  （`arm(rec, cfg.tts_sync_speak)`），而唯一的提前放行发生在 `Main._deliver_msg` 末尾的
  `release_stale()`——它只要看到「还有音频在途」（她上一句还在播 / 音频还在取）就**不放**。于是玩家发的
  聊天回复一直压在配对表里：占位气泡不消失、回复不出现；玩家**再发一条消息**时 `_deliver_msg` 对玩家消息
  的「先放积压」把上一条回复放了出来，看起来就是「上一条要等下一次发送」。
- **修法（判断收进状态机，可测）**：新增 `SpeechPairing.should_wait_voice(sync, env, reveal)`，三种情况
  才等语音——**她这一轮还没结束**（对局中的同步说话与落子同步照旧）、**这条要揭示棋子**（`ai_move`，
  音画同步硬规则：子仍在这手宣告的语音**开始播放**时揭示）、**前面还有台词在等它的语音**（不许插队，
  聊天栏顺序不乱）；其余（回合已结束、不用揭示、前面没欠账：聊天 / 闲置搭话 / 非法手提示）**当场显示**。
  整段合成（`tts_batch_speak`）那条路本来就只在「已经在响了」时才等，不受此问题影响。
- **门卫**：`game\tests\test_speech_pairing.gd` 新增 ⑪ / ⑪c / ⑪d 三组断言（回合结束即显示、不许插队、
  `reveal` 与回合中的台词照旧等语音）；README「同步说话」一栏改写为上述规则。
- **端到端复核**：无头跑真 `Main.tscn` + 假后端（本地 12 秒长音频）——修复前：玩家在她朗读时发聊天，
  `pending=true`、占位不变、10 秒后回复仍未出现，发下一条消息才显示；修复后：回复当场显示、占位随即被
  回复替换。落子场景逐帧对照：`ai_move` 文字与 `POST /api/reveal` 的时机与修复前**逐字一致**。

### v3.5.0
- **语音链路整体搬到后端**：新增 `backend\app\speech_text.py`（清洗 / 分句 / 分块 / `[[tone:…]]` 剥离，
  从 `TTSClient.gd` 与 `Main.gd:_strip_tone` 逐条移植）与 `backend\app\tts.py`（云端合成客户端：**条数 32 /
  8 MiB 双向 LRU**、单条超预算直接不插、多块按格式拼接、任一步失败**整条**降级）；后端每发一条要念的
  台词都**紧接**一条 `speech` 事件（`turn_id` + `seq` 配对），音频经 `GET /api/tts/audio/{id}` 取回；
  新增端点 `POST /api/tts/speak`（试听）。见第五节「接语音」。
- **前端退化为播放器**：`TTSClient.gd` 不再清洗 / 分句 / 合成，只有 `play_audio(bytes, seq)` 与
  `speak_system(text, seq)` 两个入口；新增 `SpeechPairing.gd`（可测的配对状态机）守住不变量
  「任何已登记、且不属于「有意不显示」两类的台词**最终一定会被显示一次**」——音频取不回 / HTTP 200 但
  body 为空 / 字节解析不了 / `speech` 没到 / 连接断开，都会**先显示文字再收账**，**绝不静默丢字、
  绝不锁死棋盘**。
- **降级与超时**：后端合成失败（连不上 / 超时 / 非 200 / 解析失败）一律改发 `mode:"system"` 指令，
  文字照常、回合不卡（fail-closed）；新增配置 `tts_timeout_sec`（单次合成超时，默认 `30` 秒，
  `≤0` **钳到 1 秒**、脏值退回默认）。**清洗规则不再在前端出现**（语气标记已由后端剥净，不会被系统语音念出来）。
- **`tts_batch_speak` 的语义变化**：合成改成「一行一条」之后，它从「一次请求合成一整段」变成
  **整段排队连续播报**（仍影响观感 / 时延，但不再减少合成请求数）；`tts_long_chunk` 现在是**后端**的
  分块上限开关（单块 `100` → `160` 字），改完要重启后端。
- **已知取舍**：4 条同步方法的短提示只有文字没有语音；朗读方式 / 服务地址 / 模型 / 长句合并只对
  **后端重启**生效；`speech` 与 `message` 按 `turn_id` + `seq` 配对（`seq` 同回合递增、**跨回合重置**）；
  两条前端固有的错配窗口（非法手提示与第二次落子交错、tomb 占位吃掉下一条语音）各自有兜底。
  详见第五节「接语音」。
- **文档与配置**：WS 事件清单由 9 类增至 **10 类**（新增 `speech`，连接建立时的 `hello` 另算）；
  `game\config.json` 模板新增 `tts_timeout_sec: 30`（**不删任何 `tts_*` 键**——`tts_limit_len` /
  `tts_batch_speak` / `tts_long_chunk` 都还有活的读取方）；README 字段表与 `AGENTS.md` 同步。
- **文档**：扩写「二、快速开始」为详细使用说明——首次运行清单、启动顺序与就绪判定、界面与操作逐项、
  一手棋的完整流程（含音画同步）、语音与情绪、常见问题排查表、开发者测试与调试命令（章节编号未动）。

### v3.4.0
- **回合编排显式成一张状态图**：新增 `backend\app\agent_graph.py`（LangGraph），拓扑
  `prepare → decide →（条件边 tools → decide，可反复）→ speak → finalize`；六个需要 LLM 编排的公开方法
  （`player_move` / `hint` / `analyze_position` / `chat` / `idle_remark` / `summarize`）在 `agent.py` 里只做
  「构造状态 → 跑图」，引擎调用 / 提示词 / 台词 / 事件 / 记忆标签的顺序全在四个节点里。
  `player_move` 一次公开调用＝**三次 `ainvoke`**（`after_player → own_turn → just_moved`，中间数据经
  `TurnState["data"]` 透传、`turn_id` 三阶段共用）；其余 `undo` / `reset_game` / `set_allow_pass` /
  `reveal_move` / `tick_mood` / `player_interaction` 仍是同步方法、直接发事件。**公开契约不变**：
  端点、事件形状与 `AzureAgent` 公开方法签名一行未动，客户端 `NetClient.gd` 仅新增 `token_event` 一条。
- **条件边工具轮与落点候选**：工具请求改由 `decide` 后的条件边驱动（`tools → decide`，回到模型再来一轮，
  直到没有请求或预算用尽）；`decide` 内**总 LLM 调用次数**上限 `MAX_LLM_CALLS_PER_TURN = 4`（含工具轮），
  工具预算仍是 `MAX_TOOL_CALLS = 2` / 单工具 10 秒超时。工具/模型现在**可以参与落点候选**
  （模型原话里的 GTP 坐标或虚着经 `_candidate_from_text` + `_resolve_ai_move` 认下），但有**三条边界**：
  ① 候选点必须过 `board.is_legal(...)`；② `pass` 只在允许虚着时可用；③ **任何情况下都产出可用落点**
  （候选不合法就回退后端算棋的默认落点）。`tools_enabled` 默认 `false`，关闭时行为与没有工具完全一致。
- **AI 原话去重下沉到后端**：`AI_DEDUP_WINDOW = 8`（`backend\app\agent.py`）与判据移进 `finalize` 阶段
  （`agent_graph._emit_deduped` / `_is_repeat` / `_is_repeat_prefix`）。只有**台词类 `message`** 过闸——
  `ai_move` 落子宣告永不判重，`state` / `info` / `busy` / `mood` / `joseki` / `tool` 不受影响；
  「这手不成立 / 空盘提示 / 回档 / 重开 / 虚着开关 / 引擎失败恒定句」等**错误与事件型提示有意不过闸**
  （连按两次必须照旧还给玩家）；被抑制的台词**照常**写 `memory_log` / `chat_history`。前端 `Main.gd` 的同名
  常量只作 **WS 重连乱序的兜底**（计划明示的双保险）。
- **可选流式输出（默认关闭）**：新增配置 `stream_tokens`（默认 `false`）；开启后 `speak` 阶段逐块推
  `{"type":"token","turn_id","delta"}`，**气泡最终内容仍以经 `clean_reply()` + 去重闸门的完整 `message` 为准**。
  增量只取模型回答的 content 通道（推理段永不进 `delta`）；成对 think 区间保证增量与台词都干净，
  「只剩收尾标记」与未闭合 `think` 的畸形形态只保证最终 `message` / 语音干净；流式失败自动回落非流式整段出话。
  关闭时一个 `token` 都不发，事件序列与没有这个功能时逐字一致。见第四节「流式输出」。
- **文档与配置**：`game\config.json` 模板新增 `stream_tokens: false`；WS 事件清单由 8 类增至 **9 类**
  （新增 `token`）；README 字段表与 `AGENTS.md` 同步（编排在状态图里、工具可参与落点候选、去重与 `busy` 的边界）。

### v3.3.0
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
