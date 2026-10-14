# 计划 0：去本地化 · 去窥屏 · 只保留 remote —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Godot 前端收敛成“只连后端”的纯显示端，并删掉本地模型服务编排、窥屏功能与 local 模式，为 LangChain 化腾出干净地基。

**Architecture:** 后端（`backend/`）继续持有全部权威，但不再拉起任何本地进程；前端（`game/`）删除 `AzureAgent/AzurePrompts/KataGoEngine/LLMClient/GoBoard/GameState/Joseki*/EmotionModel` 与 `test_e2e.gd`，只留 `NetClient` + 渲染 + `TTSClient`。所有本地模型（LLM / 窥屏 VLM / CosyVoice）改为云端，通过 `config.json` 的地址 + API Key 接入。

**Tech Stack:** Python 3.10+ / FastAPI / httpx / `unittest`（不用 pytest）；Godot 4.6 / GDScript。

**Spec:** `docs/backend-langchain-plan.md`（决策 D3/D5/D6/D8/D9）

## Global Constraints

- 本机 Python 为 `E:\Python`，**未安装 fastapi**：跑后端任何测试前先 `pip install -r backend/requirements.txt`。
- 测试一律 `unittest`，命令 `cd backend && python -m unittest discover -s tests`；不得引入 pytest。
- 注释、UI 文案、提交信息一律中文；提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一版本 **v3.0.0**，并同步写进 `README.md` 的「更新」小节。
- 前端脚本是 `res://` 资源且带 `.gd.uid`：**删除 `.gd` 时必须同时删除同名 `.gd.uid`**，且 `.tscn` 里不得残留 `ext_resource`（本计划只涉及 `scenes/Main.tscn`，它只引用 `scripts/ui/Main.gd`）。
- `game/scripts/core/Coords.gd` **必须保留**：`Board3D.gd` 用它做棋盘坐标标签（`Coords.letter/valid`）。
- 后端对云服务失败仍须优雅降级（返回可读错误、`busy` 必须能释放、不得刷屏）；前端连不上后端时只显示状态栏提示。
- 不得改动 `prompts.context_block` 末尾的「铁律段」文本（含方位词核对规则）。

## Review Focus

- **旧配置残留**：玩家机器上带着 v2.x 的 `config.json`（`backend_mode: "local"`、`llm_auto_start`、`vlm_*`、`tts_auto_start`）升级后，必须照常启动并连后端，不得崩溃、不得静默黑屏、不得把旧键写回文件。
- **云端 LLM 失败路径**：断网 / 401 / 429 / 超时 → 她应回兜底台词且 `busy` 归位（`busy` 不归位＝棋盘永久锁死，是本项目最严重的故障形态）。
- **完全未配置云服务**：只配一个空 `llm_base_url` 启动 → 后端进程必须能起来、`GET /api/health` 与 `GET /api/state` 必须可用、下棋与落子宣告仍应工作（点评退化为兜底文案）。
- **删除窥屏后仍要能主动搭话**：`POST /api/idle` 与 `ai_idle` 事件（含“哼”一声的前置音效逻辑）必须照常；不得因删除 `_peek_hmph` 相关分支而报错。
- **`.uid` 悬挂**：删掉 `.gd` 却留着 `.gd.uid`（或反之）会让 Godot 导入报错；导入命令必须无 `ERROR` 输出。

---

### Task 1: 后端移除本地 LLM 服务编排

**Files:**
- Delete: `backend/app/services.py`
- Modify: `backend/app/config.py`（删 `llm_auto_start / llm_port / llm_exe / llm_model_path` 四个字段、`_apply` 里的读取、`_resolve_paths` 里对 `llm_exe/llm_model_path` 的解析）
- Modify: `backend/app/llm.py`（删 `/chat/completions` 之外的 `relaunch_cb` 入参、`watch_loop()`、`WATCH_INTERVAL/WATCH_TRIGGER/WATCH_COOLDOWN` 常量、`_local_target()` 中“看护”用途的注释；`_reachable/_try_connect/_resolve_addrs` 保留）
- Modify: `backend/app/main.py`（lifespan 里删 `functools.partial(services.ensure_local_llm, cfg)`、`services.ensure_local_llm(cfg)`、`watch_task` 相关分支与 `import functools`）
- Create: `backend/tests/test_config.py`

**Interfaces:**
- Consumes: 现有 `AzureConfig.load()`、`LLMClient(base_url, model, api_key, timeout_sec, think_mode)`、`create_app(cfg, engine, llm, agent, autostart)`
- Produces: `LLMClient.__init__(self, base_url="http://localhost:8080/v1", model="qwen3.5", api_key="", timeout_sec=120.0, think_mode=0)`（**不再有 `relaunch_cb`**）；`AzureConfig` 不再有本地服务字段；`create_app(...)` 签名不变

- [ ] **Step 1: 写失败测试** — `backend/tests/test_config.py`

```python
"""去本地化后的配置面与 LLM 客户端形状（对应决策 D3）。"""

import importlib.util
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AzureConfig
from app.llm import LLMClient

_REMOVED_CONFIG_FIELDS = ["llm_auto_start", "llm_port", "llm_exe", "llm_model_path"]


class DelocalizedConfigTest(unittest.TestCase):
    def test_local_service_fields_removed(self):
        c = AzureConfig()
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), "配置项应已删除：%s" % name)

    def test_legacy_config_keys_are_ignored(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"llm_exe": "D:/x/llama-server.exe", "llm_port": 8080,
                           "llm_base_url": "https://api.example.com/v1"}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.llm_base_url, "https://api.example.com/v1")
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), name)


class DelocalizedLLMTest(unittest.TestCase):
    def test_no_service_watch(self):
        self.assertNotIn("relaunch_cb", inspect.signature(LLMClient.__init__).parameters)
        self.assertFalse(hasattr(LLMClient(), "watch_loop"))

    def test_services_module_removed(self):
        self.assertIsNone(importlib.util.find_spec("app.services"))


if __name__ == "__main__":
    unittest.main()
```

追加到 `backend/tests/test_agent.py`（Review Focus 前两条的守卫）

```python
class FailSafeTest(unittest.TestCase):
    """云端 LLM 失败 / 未配置时：busy 必须归位，健康面必须可用。"""

    class BoomLLM:
        think_mode = 0
        base_url = "https://api.example.com/v1"

        async def chat(self, messages, max_tokens=300, no_think=False):
            raise RuntimeError("云端 500")

    def test_busy_released_when_llm_raises(self):
        agent, eng, _llm, bus = _mk()
        agent.llm = self.BoomLLM()

        async def run():
            await agent.player_move(3, 3)

        asyncio.run(run())
        self.assertFalse(agent.busy)
        self.assertFalse(bus.of_type("busy")[-1]["busy"])


if __name__ == "__main__":
    unittest.main()
```

> 注：`test_busy_released_when_llm_raises` 现在**预期失败**（`agent.player_move` 未加 try/finally）——Step 3 必须在 `player_move`/`_ai_turn` 的 shell 外加上 `try/finally` 释放 `busy`。这是本计划唯一的**行为修复**（不是重构），也是 Review Focus 第一条的守卫。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_config tests.test_agent.FailSafeTest -v`
Expected: FAIL（`hasattr` 断言失败 / `find_spec("app.services")` 不为 None / `busy` 未归位）

- [ ] **Step 3: 实施删除与 busy 兜底**
  删掉 `backend/app/services.py` 整个文件；按 `Files` 列出的三处逐项删除；`main.py` 的 lifespan 收敛为「建 agent → 启动引擎 → 后台探模型」三步，删除 `watch_task` 与 `functools` 依赖。另外给 `player_move` 与 `_ai_turn` 的编排外壳加 `try/finally`（`finally` 里 `self._set_busy(False)`），修掉「LLM 抛异常 → `busy` 永久为真 → 棋盘永久锁死」这个既有隐患。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest tests.test_config tests.test_llm tests.test_agent tests.test_main -v`
Expected: 全 PASS（`test_llm` 7 条阶梯用例必须仍全绿——它们注入假 `_post`，是本次改动不碰行为边界的证据）

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/app/llm.py backend/app/main.py backend/tests/test_config.py
git rm backend/app/services.py
git commit -m "refactor(backend): 移除本地 LLM 服务编排，全部模型改走云端（v3.0.0）"
```

---

### Task 2: 后端移除窥屏（`peer_remark` 与 `POST /api/peer`）

**Files:**
- Modify: `backend/app/agent.py`（删 `peer_remark()` 方法与其 docstring）
- Modify: `backend/app/main.py`（删 `POST /api/peer` 端点；**保留** `/api/idle`）
- Modify: `backend/tests/test_main.py`（追加 404 断言）、`backend/tests/test_agent.py`（追加属性断言）

**Interfaces:**
- Consumes: 无
- Produces: `AzureAgent` 不再有 `peer_remark`；HTTP 面不再有 `/api/peer`

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_main.py` 的 `RestTest`

```python
    def test_peer_endpoint_gone(self):
        with _client() as client:
            self.assertEqual(client.post("/api/peer", json={"fg_title": "x"}).status_code, 404)
```

追加到 `backend/tests/test_agent.py` 的 `FlowTest`

```python
    def test_peek_removed(self):
        agent, _eng, _llm, _bus = _mk()
        self.assertFalse(hasattr(agent, "peer_remark"))

    def test_idle_remark_still_works(self):
        """删掉窥屏后，闲置主动搭话必须照常（Review Focus 第四条）。"""

        async def run():
            agent, _eng, _llm, bus = _mk()
            await agent.idle_remark()
            return bus

        bus = asyncio.run(run())
        self.assertEqual(len(bus.messages(events.AI_IDLE)), 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m unittest tests.test_main tests.test_agent -v`
Expected: FAIL（`/api/peer` 返回 200；`hasattr(agent, "peer_remark")` 为 True）

- [ ] **Step 3: 实施删除** — 删 `peer_remark` 与端点。注意 `agent.py` 里 `IDLE_LINES` 等闲置逻辑**不动**（D5 只去掉窥屏）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent.py backend/app/main.py backend/tests/test_main.py backend/tests/test_agent.py
git commit -m "refactor(backend): 去除窥屏搭话（/api/peer 与 peer_remark）（v3.0.0）"
```

---

### Task 3: 前端收敛为 remote-only，删除本地编排实现

**Files:**
- Delete: `game/scripts/ai/AzureAgent.gd(+.uid)`、`game/scripts/ai/AzurePrompts.gd(+.uid)`、`game/scripts/services/KataGoEngine.gd(+.uid)`、`game/scripts/services/LLMClient.gd(+.uid)`、`game/scripts/core/GoBoard.gd(+.uid)`、`game/scripts/core/GameState.gd(+.uid)`、`game/scripts/core/JosekiData.gd(+.uid)`、`game/scripts/core/JosekiRecognizer.gd(+.uid)`、`game/scripts/core/EmotionModel.gd(+.uid)`（`ai/` 目录随之删空）
- Delete: `game/tests/test_e2e.gd(+.uid)`（断言依赖 `GoBoard.EMPTY` 与本地模式）
- Modify: `game/scripts/ui/Main.gd`（`var katago: KataGoEngine` / `var llm: LLMClient` 删除；`_build_services()` 里的 local 分支整段删除、函数体只留现在 `_build_services_remote()` 的内容并合并为 `_build_services()`；删 `_warm_up_engine()`、`AzurePrompts.configure(cfg)` 调用、`_apply_cli_overrides` 里的 `--backend=` 分支）
- Modify: `game/scripts/core/AzureConfig.gd`（删 `backend_mode`、`katago_bin/katago_model/katago_config`、`llm_auto_start/llm_exe/llm_model_path/llm_port`、`tts_auto_start`、`tts_bf16/tts_short_prompt`、`vlm_*`；删 `_resolve()` 及其调用；`save()` 里对上述键改为 `d.erase(...)`）

**Interfaces:**
- Consumes: `NetClient`（`game/scripts/net/NetClient.gd`，与旧 `AzureAgent` 同信号/方法面）
- Produces: `Main.gd` 只有一条装配路径；`AzureConfig` 字段集 = `max_visits / joseki_teach / backend_url / llm_base_url / llm_model / llm_api_key / llm_think_mode / vrm_path / prompt_* / tts_enabled / tts_mode / tts_base_url / tts_model / tts_api_key / tts_voice / tts_speed / tts_pitch / tts_volume / tts_limit_len / tts_sync_speak / tts_batch_speak / tts_long_chunk`

- [ ] **Step 1: 先删文件、再看编译面** — 按上方 `Delete` 列表执行 `git rm`（含 `.uid`），然后用引用检查确认没有悬挂引用：

Run: `rg -n "AzureAgent|AzurePrompts|KataGoEngine|LLMClient|GoBoard|GameState|JosekiData|JosekiRecognizer|EmotionModel" game`
Expected: 仅剩 `game/scripts/net/NetClient.gd` 注释里对 `AzureAgent` 的历史说明（Task 6 会改注释）

- [ ] **Step 2: 改 `Main.gd`** — 删 local 分支与上述函数；`agent` 仍是 `Var`（`NetClient`，`_wire_agent()` 不动）。
- [ ] **Step 3: 改 `AzureConfig.gd`** — 按 `Produces` 的字段集收敛；`save()` 中对已删键写 `d.erase("llm_auto_start")` 等，让旧文件自我清理。
- [ ] **Step 4: 导入与冒烟验证**

Run: `godot --headless --path game --import`
Expected: 无 `SCRIPT ERROR` / `Parse Error` / `Failed loading resource`

Run: `godot --headless --path game --quit`
Expected: 进程正常退出；输出中无 `SCRIPT ERROR`，只有 `[Net] 未连接后端（http://127.0.0.1:8000）…` 这类状态提示

- [ ] **Step 5: 提交**

```bash
git add -A game
git commit -m "refactor(game): 只保留 remote 模式，删除本地编排与旧脚本（v3.0.0）"
```

---

### Task 4: 前端删除窥屏与本地语音拉起/加速开关

**Files:**
- Modify: `game/scripts/ui/Main.gd`
  - 删窥屏：`PEEK_COOLDOWN_MS/PEEK_TIMEOUT_MS`、`_peek_*`、`_vlm_starter`、`_peek_available()`、`_ensure_vlm_url()`、`_start_peek()`、`_tick_peek()`、`_process()` 里的窥屏分支、`_deliver_msg` 里 `kind == "ai_idle"` 的 `_peek_hmph` 分支、`_cancel_idle_speech`/`_bump_activity` 里对 `_peek_hmph`/`_peek_*` 的引用
  - 删本地语音拉起：`_tts_dir`、`_find_tts_dir()`、`_autostart_tts()`、`_spawn_tts_server()`、`_restart_tts_server()`、`_health_url()`、`_service_started_or_starting()`、`_tts_health_once()`、`_wait_tts_health()`、`_on_acc_toggled()`、`_kill_lock()`、`_read_lock_pid()`、`_exit_tree()` 里对它的调用
  - 删 `_ensure_llm_url()`、`_llm_starter`、`_llm_py`、`_is_local_url()`、`_url_port()`
  - 左栏：删「bf16 加速」「短参考」两个 `_acc_check`（原 323~325 行附近）与 `tts_auto_start` 勾选框（原 482~483 行附近）
  - 设置面板：删「窥屏 VLM API」整段（原 1443~1446 行附近），`_apply_setup()` 去掉 `vlm_url/vlm_port` 形参
  - `_refresh_voices()`/`_fill_voice_items()`：不再向本地服务 `/voices` 探测，改用静态音色列表 + 允许手填（`COSYVOICE_PRESETS/CLONE_*` 常量随之删除）
- Modify: `game/scripts/services/TTSClient.gd`：删除 `cosyvoice` 专属的本地服务探测/加速分支（`_probe_service` 中本地地址特判、`is_slow_engine` 的本地快慢判定可保留为纯配置判断），保留 `system`/`openai` 两种模式与播放队列
- Modify: `game/tests/test_tts.gd`：删掉对本地服务路径的注释与引用（行为断言不变）

**Interfaces:**
- Consumes: 云 TTS：`tts_mode="openai"`、`tts_base_url/tts_model/tts_voice/tts_api_key`
- Produces: `Main.gd` 中不再出现任何 `OS.create_process` 调用；`TTSClient` 只认 `system` / `openai`

- [ ] **Step 1: 删除代码** — 按 `Files` 逐项删除；`_process()` 的闲置逻辑保留「到点调 `agent.idle_remark()`」这一步。
- [ ] **Step 2: 确认前端不再拉起进程**

Run: `rg -n "create_process|wscript|\.bat|wscript\.exe|azure_peek|start_llm_server|start_vlm_server" game`
Expected: 无匹配

- [ ] **Step 3: 冒烟验证**

Run: `godot --headless --path game --import`
Expected: 无 `SCRIPT ERROR`

Run: `godot --headless --path game --quit`
Expected: 正常退出、无 `SCRIPT ERROR`

- [ ] **Step 4: 音频通道自检（TTS 仍在客户端）**

Run: `godot --path game --headless --script res://tests/test_tts.gd`
Expected: 打印「清洗」「WAV 解析」「系统语音进程 pid=…」「降级验证完成」，不退化为报错

- [ ] **Step 5: 提交**

```bash
git add -A game
git commit -m "refactor(game): 去除窥屏与本地语音服务拉起，语音改用云端 openai 模式（v3.0.0）"
```

---

### Task 5: 配置面与模板清理

**Files:**
- Modify: `game/config.json`（模板：删除 `katago_*`、`llm_auto_start/exe/model_path/port`、`tts_auto_start/bf16/short_prompt`、`vlm_*`、`backend_mode`；`tts_mode` 改 `"openai"`；补 `backend_url`、`llm_*`、`tts_*` 云端字段；`_说明` 改写为“云端模型 + 远程后端”）
- Modify: `backend/app/config.py`（**本任务不动 `katago_*`**：它属计划 01；这里只确认删掉本地服务字段后没有别处引用）
- Modify: `README.md`（第三节 `config.json` 字段表：删本地服务相关行、标注云端字段；第六节「远程后端」：改为唯一模式，去掉“默认 local”“分享包默认仍是 local”等表述）

**Interfaces:**
- Consumes: 无
- Produces: 一份与代码一致的 `config.json` 模板；字段表与 `AzureConfig.gd` 字段集一一对应

- [ ] **Step 1: 改模板与文档** — 按 `Files` 执行。
- [ ] **Step 2: 校验模板能被两边读入**

Run: `cd backend && python -c "from app.config import AzureConfig as C; c=C.load('../game/config.json'); print(c.llm_base_url, c.max_visits)"`
Expected: 正常打印，无异常

Run: `godot --headless --path game --quit`
Expected: 无 `SCRIPT ERROR`

- [ ] **Step 3: 提交**

```bash
git add game/config.json backend/app/config.py README.md
git commit -m "chore(config,docs): config.json 与字段表改为云端模型 + 远程后端（v3.0.0）"
```

---

### Task 6: 仓库卫生、文档与版本

**Files:**
- Modify: `AGENTS.md`（**必须改**：现有内容仍在描述 local 模式、分享版、`peek/azure_peek.py`、`start_llm_server.py`、本地服务锁、“缺服务优雅降级”，这些前提已全部失效；改为 remote-only + 云端模型 + `katago_service/` 待建）
- Modify: `game/scripts/net/NetClient.gd`（注释里“本地模式（AzureAgent）行为不变”等历史说明改为现状）
- Modify: `game/scripts/ui/Main.gd`（顶部注释“调试：`-- --shot=…`”保留；删掉任何指向已删脚本的说明）
- Modify: `README.md` 第九节（新增 `### v3.0.0` 条目）
- Modify: `.gitignore`（补 `backend/data/`、`backend/logs/`，为计划 03 的 SQLite 与引擎日志预留；`gtp_logs/`、`shots/`、`server_run.log` 里已无用的条目可留可删）
- **分享版遗留（决策 D8，需显式处理）**：删除 `tools/build_pck.ps1`（分享版 PCK 打包流程不再维护）；`game/export_presets.cfg` **保留**（个人本地导出仍可用），并在 README 注明“分享版流程已废弃”。`git rm tools/build_pck.ps1`。

**Interfaces:**
- Consumes: 无
- Produces: 与实际代码一致的 `AGENTS.md` / `README.md`

- [ ] **Step 1: 改文档** — 按 `Files` 执行；`AGENTS.md` 里凡是提到本地模式、分享包、窥屏、`start_*_server.py` 的段落删掉，换成：后端是唯一权威、前端只连 `backend_url`、KataGo 由 `katago_service/` 提供（计划 01）、模型全云端。
- [ ] **Step 2: 全量回归**

Run: `cd backend && python -m unittest discover -s tests`
Expected: 全 PASS

Run: `rg -n "分享版|backend_mode|窥屏|azure_peek|start_llm_server|build_pck" README.md AGENTS.md`
Expected: 无匹配

- [ ] **Step 3: 提交**

```bash
git add AGENTS.md README.md .gitignore game
git rm tools/build_pck.ps1
git commit -m "docs: 同步 remote-only 与云端模型架构说明，README 记录 v3.0.0（v3.0.0）"
```
