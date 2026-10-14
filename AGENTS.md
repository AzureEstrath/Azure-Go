# AGENTS.md

面向在本仓库工作的 OpenCode 会话。Windows 单平台项目；注释、UI 文案、提交信息一律用中文，与现有风格保持一致。

## 是什么

3D 围棋陪练：Godot 4 前端 + 本地 KataGo 算棋 + OpenAI 兼容的 LLM（讲解/闲聊）与 TTS（语音）。

- `game/` —— 游戏本体（GDScript）。`scenes/Main.tscn` 只挂了 `scripts/ui/Main.gd`，其余节点与 UI 全在代码里构建；`scripts/` 按 `core`（棋盘/对局/配置/定式）、`ai`（AzureAgent 流程编排、AzurePrompts 提示词）、`services`（KataGo/LLM/TTS 客户端）、`ui` 分类。
- `server/` —— CosyVoice 3 语音服务。**288/370 个入库文件是上游 CosyVoice 代码**（`server/cosyvoice/`、`examples/`、`runtime/`、`.github/`、根 `README.md` 都是上游内容），本项目自己写的只有 `azure_tts_server.py`、`clone_voice.py`、`voices.json`、`sherpa_voices.json`、`run_hidden.vbs`、`start_tts_server*.bat`。不要顺手重排/美化上游目录。`server/third_party/Matcha-TTS` 是 git submodule。
- 根目录没有 CI / lint / formatter / 包管理器配置，只有 `main` 一条分支；`server/.github/workflows/` 是上游 CosyVoice 的，与本项目无关。

## 命令

- 运行游戏：`Godot_v4.6.3-stable_win64.exe --path game`（工程按 Godot 4.6 配置）。
- `tools/build_pck.ps1` 重建分享版 PCK：`powershell -ExecutionPolicy Bypass -File tools\build_pck.ps1`
  - 默认 Godot 路径是开发机上的 `D:\Godot\Godot_v4.6.3-stable_win64_console.exe`，默认输出**仓库外**的 `..\..\AzureGoGodot-share\AzureGoGodot.pck`；两者都要用 `-Godot` / `-OutPck` 覆盖。
  - 分享版的 exe 只是 Godot 引擎本体，游戏逻辑全在 `.pck`。
- 测试（都是 `SceneTree` 脚本，跑完自己 `quit()`）：
  - `godot --path game --headless --script res://tests/test_tts.gd` —— 文本清洗 / 服务不可用降级 / WAV 解析，不需要外部服务（会真的拉起一次系统语音进程，音量已压到最低）。
  - `godot --path game --headless --script res://tests/test_e2e.gd` —— 端到端，**需要真实 katago.exe + 权重 + 能连通的 LLM**；LLM 不可用时会因输出 `调用出错` 判 FAIL。结果看 `[e2e] RESULT=PASS|FAIL`，退出码即结果。
- 新增/移动资源后先 `godot --headless --path game --import` 重新生成导入信息（`build_pck.ps1` 的第一步就是它）。
- 调试参数走 `OS.get_cmdline_user_args()`，**必须放在 `--` 之后**：`-- --shot=out.png`（渲染数帧后截图退出）、`-- --demo`、`-- --setup`（只画设置面板供截图）；`tests/test_e2e.gd` 用自己的 `--tshot= / --tyaw= / --tpitch=`，不要写成 `--shot`。

## 约束与坑

- **仓库 clone 下来跑不起来是正常的**：`katago.exe`/`*.dll`、`model.bin`、`Azure.vrm`、CosyVoice 权重、本地 gguf 都不入库（见 `.gitignore` 与 README「模型与资产」，走 GitHub Releases）。不要试图补齐、生成或提交这些大文件。
- 配置读取顺序：**可执行文件同目录 `config.json` → `res://config.json`**；`game/config.json` 只是随包模板。相对路径依次在 exe 目录 → 项目目录 → `D:/KataGo` 下查找（`AzureConfig._resolve`）。游戏内设置面板会**写回** config.json，而 CLI `--llm/--model/--key` 被记进 `cli_locked`、只影响本次运行——改配置逻辑时别破坏这个区分。
- 不要拿 `game/config.json` 当字段默认值来源：它与 `AzureConfig.gd` 的默认值并不一致（模板 `tts_mode=system`/`tts_enabled=true`，代码默认 `tts_enabled=false`、`max_visits` 不同）。以 `AzureConfig.gd` 为准，字段清单看 README 第三节。
- 场景引用带 `uid://`（`Main.tscn` 的 ext_resource），`*.gd.uid` 与 `*.import` 已入库：移动/重命名脚本或资源时同步更新它们，不要手写 uid。
- **一切外部能力必须优雅降级**（缺 KataGo、LLM、TTS、VLM、窥屏环境都不得崩、不得报错刷屏），这是全项目一贯约定；服务不可用时语音可临时降级为系统语音但不改配置。
- 本地服务由游戏按需拉起（`llm_auto_start` / `tts_auto_start` / `vlm_auto_start`），靠锁文件 + 健康检查防重复，退出时清理自己拉起的进程。相关脚本不在仓库里，约定放在 exe 同级目录或 `D:/KataGo/`：`start_llm_server.py`、`peek/azure_peek.py`、`peek/start_vlm_server.py`。改启动逻辑时保持「找不到就静默跳过」。
- 语音服务由 `run_hidden.vbs` 以**隐藏窗口的 python.exe** 启动（用 pythonw 会静默崩溃），日志固定写 `server_run.log`——排查服务问题先看它。
- 坐标语义：内部 `Vector2i(x, y)` 中 x=列、y=行，`Coords.gd` 与 GTP 一致（A..T 跳过 I，行号上大下小）。给玩家写方位词前必须按坐标核对：同列只能说「上下」，同行才能说「左右」，核不上就不说方位（README v1.5.0 的硬性要求）。
- 发言链路有几条不能回退的硬规则：AI 原话按最近 8 条窗口去重（`Main.gd` 的 `AI_DEDUP_WINDOW`）、模型推理段永不显示给玩家、落子宣告不参与复读抑制、走神搭话可整批作废。改 `AzurePrompts.gd` 时保留这些前提，也别在提示词里放可被模型照抄的示例句。

## 提交

沿用现有 Conventional 风格 + 中文描述 + 版本号结尾，例如：

```
fix(chat,ai): 非相邻原样复读窗口去重 + 方位词按坐标核对（v1.5.0）
```

版本号同时写进 `README.md` 的「更新」小节（当前 v2.0.1）。
