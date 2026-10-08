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
> 当前版本 **v1.2.0**：开场第一句为预制语音（启动即开口）；本地服务（LLM / CosyVoice / 窥屏 VLM）
> 随游戏自动起停，退出不留后台进程。

---

## 一、仓库结构

```
.
├─ game\                    游戏本体（Godot 4.6 工程）
│   ├─ scenes\Main.tscn     主场景
│   ├─ scripts\             GDScript 源码
│   │   ├─ Main.gd          主控：3D 棋盘 + 左右面板 + 本地服务起停 + 接线
│   │   ├─ AzureAgent.gd    对局/聊天流程编排
│   │   ├─ AzurePrompts.gd  人设与提示词（可被 config.json 覆盖）
│   │   ├─ AzureConfig.gd   config.json 读写
│   │   ├─ KataGoEngine.gd  KataGo 子进程封装（GTP）
│   │   ├─ LLMClient.gd     LLM（OpenAI 兼容）客户端，支持 API Key
│   │   ├─ TTSClient.gd     语音客户端，支持 system / openai / cosyvoice
│   │   ├─ Board3D.gd       3D 棋盘绘制与交互
│   │   ├─ AzureAvatar.gd   VRM 形象、表情、视线、风场
│   │   └─ Ambience.gd      环境/风
│   ├─ assets\voice\        预制/兜底语音（开场欢迎语 welcome_<玩家名>.wav，即播零等待）
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
即可下棋（KataGo 与权重已随包）。开场第一句是 Azure 预制欢迎语音，启动即开口；
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

### v1.2.0（当前）
- **开场秒开口**：第一句欢迎语改为**预制语音**（`assets\voice\welcome_<玩家名>.wav`），启动即播、零等待；缺文件自动回退普通合成。
- **本地服务随游戏自动起停**：启动时一并拉起本地 LLM / CosyVoice / 窥屏 VLM 服务，退出时清理由游戏拉起的进程（缺环境自动跳过，不影响使用）。
- 新增 `llm_auto_start` / `llm_exe` / `llm_model_path` / `llm_port` 与 `vlm_*` 配置项。
- 提速：LLM 前缀缓存复用；思考型模型默认关思考（云端不认自动回退）；CosyVoice 首块短切、边播边合成。

### v1.1.0
- 长闲置**窥屏搭话**：空闲时截屏 + OCR + 本地 VLM 看图，Azure 主动找你说话（缺环境自动降级为普通搭话）。
- 点评跨回合去重；落子宣告不复读；走神搭话语音可整句作废。

本人技术有限，但想象力绝对够，后续版本更新取决于ai以及我的计算机&人工智能学习深度(:-])
本地部署采用 CosyVoice 3，Qwen3.6 35B（`Qwen3.6-35B-A3B-UD-Q4_K_XL`）。
