# Azure · 围棋陪练

WARNING:这是一个由ai完善的项目！

ORIGINAL ASPIRATION:Azure是一个我设定的ai少女，最开始只有概念，后来根据自己学过的几年python知识以及一台比较好的设备在本地跑了一下qwen2.5，后来又通过ai工具的使用逐渐做成了一个通过Napcat接入qq的机器人，后来得知了maibot这个工具并本地部署了qwen3.5但聊天效果太慢了，又恰好最近对围棋比较感兴趣（小时候也学过一点），遂有了此项目。

FUN WHERE:市场上有各种ai围棋陪练，但都是基于katago的直接应用，并没有指导的感觉，很枯燥，所以我希望AazureGo不仅仅可以让大家感到开心，并且也能激发大家在现代无法沉心环境背景下对于围棋学习的乐趣。

这是一个 3D 围棋陪练程序：**KataGo** 负责算棋（玩家执黑先行，Azure 执白应手），
**大模型** 负责讲解与闲聊，**语音合成** 让 Azure 开口说话，
**Godot 4** 场景里是一个会眨眼、歪头、注视你的青蓝色 AI 少女 Azure。

> 面向「**不想在本地部署大模型/语音**」的玩家：KataGo 随包本地运行，
> **LLM 与 TTS 都走可自定义的 OpenAI 兼容 API**（可填云端服务并带 API Key），
> **人设与提示词也能在 `config.json` 里直接改**。
>
> 当前版本 **v1.5.0**：**聊天防复读加强**——非相邻的完全重复句也会被抑制（最近 8 条窗口去重），
> 不再出现隔一句话的原样复读；**方位描述纠错**——点评里的「上下左右」必须按坐标核对
> （同列只说上下、同行才说左右，核对不上就换说法）；**代码整理**——scripts 按 core / ai / services / ui
> 分类，注释与死代码清理。上一版 **v1.4.0**：定式导学、语气与导演层、思考三档、本地服务看护。

---

## 一、仓库结构

```
.
├─ game\                    游戏本体（Godot 4.6 工程）
│   ├─ scenes\Main.tscn     主场景
│   ├─ scripts\             GDScript 源码（按职责分类）
│   │   ├─ ui\              Main.gd          主控：3D 棋盘 + 左右面板 + 本地服务起停 + 接线
│   │   │                   Board3D.gd       3D 棋盘绘制与交互（含定式高亮轨迹）
│   │   │                   AzureAvatar.gd   VRM 形象、表情、视线、风场
│   │   │                   Ambience.gd      环境/风
│   │   ├─ ai\              AzureAgent.gd    对局/聊天流程编排
│   │   │                   AzurePrompts.gd  人设与提示词（可被 config.json 覆盖）
│   │   ├─ services\        KataGoEngine.gd  KataGo 子进程封装（GTP）
│   │   │                   LLMClient.gd     LLM（OpenAI 兼容）客户端，支持 API Key
│   │   │                   TTSClient.gd     语音客户端，支持 system / openai / cosyvoice
│   │   └─ core\            GoBoard.gd / GameState.gd / Coords.gd   棋盘与对局核心
│   │                       JosekiData.gd / JosekiRecognizer.gd     定式库与识别（纯本地）
│   │                       AzureConfig.gd  config.json 读写
│   ├─ assets\voice\        预制语音：开场欢迎语 + 20 条语气词（即播零等待，与情绪/表情绑定）
│   ├─ tests\               端到端与语音测试脚本
│   ├─ config.json          全部参数模板（见第三节）
│   └─ export_presets.cfg   Godot 导出预设（重建 PCK 用）
├─ server\                  本地语音服务（CosyVoice 3，可选）
│   ├─ azure_tts_server.py  FastAPI 服务：/inference_sft、/health、/v1/voices
│   ├─ run_hidden.vbs       隐藏窗口启动（游戏自动拉起时调用，随包移动可用）
│   ├─ start_tts_server*.bat 手动启动脚本
│   ├─ voices.json          克隆音色的参考音频与文本
│   └─ sherpa_voices.json   轻量音色清单（小雅 / 少女 / 女声）
└─ tools\build_pck.ps1      重建游戏 PCK（分享版只需替换 .pck）
```

**模型与资产**（体积大，不随源码仓库分发，见 GitHub **Releases**）：
`katago.exe` + `*.dll`、`model-b18c384nbt.bin.gz`（KataGo 权重）、`Azure.vrm`（形象）、
CosyVoice 权重（`pretrained_models` / `sherpa_models`）、本地 LLM / VLM 的 gguf。

**本地服务启动脚本**（`start_llm_server.py`、`peek\start_vlm_server.py`）属于本机部署文件：
分享包根目录已附带 `start_llm_server.py`；游戏启动时用本机 python 拉起对应服务（锁 + 健康检查
防重复），退出时清理由游戏拉起的进程，缺环境自动跳过。

---

## 二、快速开始

### 方式 A：下载分享版（推荐给只想玩的玩家）

到 **Releases** 下载 `AzureGoGodot-share.zip`，解压后双击 `AzureGoGodot.exe`
即可下棋（KataGo 与权重已随包）。开局前会先弹出「出发前的准备」：选择思考模式、
需要的话直接改 LLM / 语音 / 窥屏的 API 配置（默认沿用上次，直接点「开始对局」即可）。
开场第一句是 Azure 预制欢迎语音，启动即开口；
语音默认走 **Windows 系统语音**，零配置；聊天/讲解需要按第四节接上一个大模型 API。
若本机配了本地服务（LLM / CosyVoice / 窥屏 VLM），游戏启动时会一并拉起、退出时清理。

### 方式 B：从源码运行（开发者）

1. 用 **Godot 4.6** 打开 `game\` 工程，或：
   ```
   Godot_v4.6.3-stable_win64.exe --path game
   ```
2. 把 `katago.exe`（含 `*.dll`）、`model-b18c384nbt.bin.gz`、`default_gtp.cfg`、`Azure.vrm`
   放到可访问位置，并在 `game\config.json` 里填好路径（相对路径会先在可执行目录、再在项目目录查找）。
3. 玩家**执黑先行**：左键落子，右键拖拽旋转视角，滚轮缩放。

---

## 三、`config.json` 字段

放在**可执行文件同目录**即可覆盖包内默认值。新增/关键项如下：

| 字段 | 含义 |
| --- | --- |
| `katago_bin` / `katago_model` / `katago_config` | KataGo 可执行文件、权重、GTP 配置 |
| `max_visits` | KataGo 每手搜索次数，越大越强越慢 |
| `llm_base_url` / `llm_model` | **LLM 的 OpenAI 兼容地址与模型名**（本地或云端） |
| `llm_api_key` | **云端 LLM 的 API Key**；非空则发送 `Authorization: Bearer <key>`，本地服务留空 |
| `llm_think_mode` | **思考档位**：`0`=快速（直答，首字快）/ `1`=中等（推理限预算并催收尾，约 4~7 秒）/ `2`=强思考（自由推理，更慢更好）；默认 `0`；开局设置面板里也能改（旧键 `llm_think_strong` 自动迁移） |
| `joseki_teach` | **定式导学**：识别你与 Azure 走出的常见定式（讲解 + 棋盘高亮手顺）；右侧栏也能切；默认 `true`，关掉=不识别不打扰 |
| `llm_auto_start` / `llm_port` | **本地 LLM 自动起停**：`llm_base_url` 指向本机时随游戏一并拉起（已在跑则跳过，退出游戏自动清理）；端口应与 `llm_base_url` 一致 |
| `llm_exe` / `llm_model_path` | 本地 `llama-server.exe` / 主模型 gguf 路径（留空由启动器自动探测） |
| `vlm_base_url` / `vlm_auto_start` / `vlm_port` | 窥屏搭话的 VLM 服务：留空=用本地服务（游戏负责拉起），填了视为外部服务、不碰本地 |
| `vlm_exe` / `vlm_model` / `vlm_mmproj` | 本地 `llama-server.exe` / VLM gguf / 视觉编码器 mmproj 路径（留空自动探测） |
| `prompt_system` | **覆盖系统提示词（人设全文）**；留空用内置 Azure 人设 |
| `prompt_user_name` | **玩家称呼**，替换提示词中的「Estarth」（默认 Estarth） |
| `prompt_phase_notes` | **覆盖各阶段提示**，键：`observe` / `after_player` / `own_turn` / `just_moved` / `analyze` / `chat` / `summary` |
| `vrm_path` | 虚拟形象 `.vrm` 路径 |
| `tts_enabled` | 是否启用语音 |
| `tts_mode` | `system`（Windows 系统语音，零配置兜底）/ `openai`（OpenAI 兼容 `/v1/audio/speech`，可云端）/ `cosyvoice`（本地 CosyVoice 表单接口） |
| `tts_base_url` | 语音服务地址，默认 `http://127.0.0.1:9880`（cosyvoice 模式**不带** `/v1`） |
| `tts_model` / `tts_voice` | 语音模型名 / 音色 id |
| `tts_api_key` | **云端 TTS 的 API Key**；非空则发送 `Authorization: Bearer <key>` |
| `tts_speed` / `tts_pitch` / `tts_volume` | 语速 / 音调（播放层变调）/ 音量 |
| `tts_auto_start` | 启动游戏时自动拉起本地 CosyVoice 服务（仅 `cosyvoice` 模式），退出时一并清理 |
| `tts_bf16` / `tts_short_prompt` | 本地 CosyVoice 加速开关（需重启语音服务） |
| `tts_limit_len` / `tts_sync_speak` / `tts_batch_speak` / `tts_long_chunk` | 短回复 / 同步说话 / 整段合成 / 长句合并（立即生效） |

---

## 四、接大模型（LLM，OpenAI 兼容）

改 `config.json` 三行即可，**不内置任何模型**：

```json
"llm_base_url": "https://api.deepseek.com/v1",
"llm_model": "deepseek-chat",
"llm_api_key": "sk-你的密钥"
```

- **也可在游戏里改**：开局前的「出发前的准备」面板能直接填地址 / 模型名 / API Key（确认后写回 `config.json`），不用手动编辑。
- **云端**：DeepSeek / OpenAI / 通义 / 硅基流动 等 OpenAI 兼容服务，填地址、模型名与 Key。
- **本地**：vLLM / LM Studio / llama.cpp server 等，`llm_api_key` 留空。
- **本地想省事**：把 `start_llm_server.py`（分享包根目录附带）与 `llm_exe` / `llm_model_path`
  备好，游戏启动时自动拉起服务、退出时清理；服务已在跑会被识别并跳过。
- 命令行临时指定（不写回配置）：`AzureGoGodot.exe --llm=http://localhost:8080/v1 --model=你的模型名 --key=你的密钥`
- 没接 LLM 时：**对局功能照常可用**，只是讲解/闲聊不可用。

---

## 五、接语音（TTS，可选）

- **默认 `system`**：Windows 系统语音，零配置即可发声。
- **云端 `openai`**：任何 OpenAI 兼容 `/v1/audio/speech` 服务（如 OpenAI、部分云厂商），
  填 `tts_base_url` / `tts_model` / `tts_voice` / `tts_api_key`。
- **本地 `cosyvoice`**：用 `server\` 自建 CosyVoice（最像 Azure 原声）。
  1. 自备 CosyVoice 权重，放到 `server\pretrained_models\` 下；
  2. 建虚拟环境并安装依赖（`server\requirements_win.txt`、`requirements.txt`）；
  3. 双击 `server\start_tts_server.bat`，服务监听 `http://127.0.0.1:9880`；
  4. `config.json` 改 `tts_mode=cosyvoice`、`tts_voice=azure`。
  > 服务未启动时游戏会**自动临时降级为系统语音**，不报错、不改配置。

---

## 六、重建分享版

分享版的 `AzureGoGodot.exe` 只是 Godot 引擎本体，**游戏逻辑全在 `AzureGoGodot.pck`**。
改完 `game\` 源码后：

```
powershell -ExecutionPolicy Bypass -File tools\build_pck.ps1
```

然后把生成的 `AzureGoGodot.pck` 覆盖到分享包目录即可。

---

## 七、许可

本项目源码采用 **Apache License 2.0**（见 [LICENSE](LICENSE)）。
`server\` 下的 CosyVoice 及其 `third_party\Matcha-TTS` 为各自上游项目，遵循其原始许可。
KataGo 引擎与权重遵循其上游许可。

---

## 八、更新

### v1.5.0（当前）
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
- **走神搭话先窥屏**：玩家切到别的窗口（微信/浏览器…）时，走神搭话一律先窥屏、结合屏幕内容开口，不再只说默认的「还在吗」；游戏窗口在前台时直接普通搭话（不白起窥屏进程）。窥屏冷却从 3 分钟缩短到 1 分钟。

### v1.3.0
- **开局设置面板**：启动时先弹出「出发前的准备」——选择**快速 / 强思考**，并可直接修改 **LLM / 语音 TTS / 窥屏 VLM** 的 API 配置（默认沿用上次的值，只有改过的才写回 `config.json`；命令行 `--llm/--model/--key` 的临时覆盖不会写回）。
- **强思考模式**：思考型模型（Qwen3 等）可选「先推理再回答」（更慢、质量更好）；本地服务自动放行推理段，云端 API 不支持时自动回退，不影响使用。
- 语音修正：取消「首块短切」分句合成——短消息整句一次合成、连续读完，不再开口碎句。
- 窥屏修正：游戏窗口在前台（玩家就在游戏里）时不再白起 python 进程；超时不再残留孤儿进程、失败不再占用冷却；结果文件按次命名；VLM 服务未就绪时等待重试（不再白白回退 CPU）。
- 修复 `start_qwen35b.bat` 误杀窥屏 VLM 的 llama-server（现在只结束占用 8080 的旧实例）。

### v1.2.0
- **开场秒开口**：第一句欢迎语改为**预制语音**（`assets\voice\welcome_<玩家名>.wav`），启动即播、零等待；缺文件自动回退普通合成。
- **本地服务随游戏自动起停**：启动时一并拉起本地 LLM / CosyVoice / 窥屏 VLM 服务，退出时清理由游戏拉起的进程（缺环境自动跳过，不影响使用）。
- 新增 `llm_auto_start` / `llm_exe` / `llm_model_path` / `llm_port` 与 `vlm_*` 配置项。
- 提速：LLM 前缀缓存复用；思考型模型默认关思考（云端不认自动回退）；CosyVoice 边播边合成、长句合并（可开关）。

### v1.1.0
- 长闲置**窥屏搭话**：空闲时截屏 + OCR + 本地 VLM 看图，Azure 主动找你说话（缺环境自动降级为普通搭话）。
- 点评跨回合去重；落子宣告不复读；走神搭话语音可整句作废。

本人技术有限，但想象力绝对够，后续版本更新取决于ai以及我的计算机&人工智能学习深度(:-])
本地部署采用 CosyVoice 3，Qwen3.6 35B（`Qwen3.6-35B-A3B-UD-Q4_K_XL`）。
