# 计划 06：忙时聊天排队与右下角提示 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 她忙（后端 `busy`）的时候玩家发出的聊天不再被 `POST /api/chat` 静默挡掉——先本地排队，右下角显示「排队中 N 条 · 队头前几个字」，等她上一句说完、彻底空闲时**自动**一条接一条发出去。

**Architecture:** 新增 `game/scripts/net/ChatQueue.gd`（纯逻辑：FIFO + 上限 + 提示文案 + 「现在能不能发」判据，无副作用）；`NetClient` 持有它，负责「忙则入队 / 闲则发」与「发出去却仍被后端挡下 ⇒ 退回队列」；`Main.gd` 只留胶水：右下角一个小标签显示提示、每帧在她空闲时催一次续发、队列满时不吞玩家的话。

**Tech Stack:** Godot 4 GDScript（`RefCounted` / `Node` / `HTTPRequest` / `Label` / `LineEdit`），headless 门卫脚本（`extends SceneTree` + 断言）。

**Spec:** 本文件 §需求（与用户对话敲定；本改动无独立设计文档）

## 需求（对话敲定）

1. **排队条件**：只有后端正忙（`agent.busy`，即 `POST /api/chat` 会被挡下）时才排队；不忙时照旧直接发。
2. **排队期间**：玩家那条话照旧立刻进聊天栏（看得见自己说了什么），右下角出现提示。
3. **提示内容（用户原话）**：**仅**显示「排队中」+ 条数 + **队头**消息的前几个字。
4. **自动续发**：她上一句说完、彻底空闲的那一刻**自动**发下一条，不用玩家再动手；多条排队就一条接一条（每条必须等上一轮真正结束）。
5. **兜底**：发出去时后端仍回 `busy`（`busy=false` 事件还没到的竞态）⇒ 这条退回队列，不丢。

**本计划自己拿的主意（用户没明说，可随时改）**：

- 提示挂在**右侧栏聊天输入行正下方**（同属「右下角」），11 号字、灰色、队列空了就隐藏；聊天栏收起时本来也看不到聊天，语义一致。
- 预览长度 **8 个字符**（与项目日志口径一致），超出补 `…`。
- 队列上限 **5 条**；已满时不移交输入框内容、不在聊天栏伪造气泡，状态栏提示「排队已满」。
- 玩家那条消息「排队中」也立刻进聊天栏（否则玩家会以为没发出去）；是否真的发出、还剩几条看右下角提示。
- 断线/后端不可达时消息留在队列里，重连且她空闲后自动补发（不刷屏、不丢）。
- 版本 **v3.5.2**（上一版 v3.5.1 的补丁：修「忙时发的消息被吞」）。

## Global Constraints

- **只改前端**：后端 `POST /api/chat` 的行为不变（忙时仍回 `{"ok": false, "busy": true, "state": …}`），不新增/不修改任何后端接口与事件；`backend` 与 `katago_service` 的测试必须保持全绿。
- **不许回退既有硬规则**：`ai_move` 的揭示仍在「这条语音开始播放」时（v3.5.1 `SpeechPairing.should_wait_voice` 的行为一字不动）；`SpeechPairing` 的不变量（已登记台词最终一定显示一次）不受影响。
- **优雅降级**：断线/不可达 ⇒ 消息留在队列、状态栏照旧提示、不刷屏，重连后自动补发。
- **不吞玩家的话**：队列满时输入框内容保留 + 状态栏提示，聊天栏不进这条气泡。
- **文案与常量**：中文；提示文案固定 `排队中 %d 条 · %s`；预览 `PREVIEW_CHARS = 8`；队列上限 `CHAT_QUEUE_MAX = 5`；空/纯空白文本不入队。
- 提交格式 `type(scope): 中文描述（vX.Y.Z）`，本计划统一 **v3.5.2**，并写进 `README.md` 的「更新」小节（`AGENTS.md` 的版本口径同步）。

## Review Focus

- **断线 / 后端不可达时排队**：消息不许丢、不许刷屏；重连且她空闲后自动补发。
- **队列满（第 6 条）**：不许吞话——输入框保留原文 + 状态栏提示，聊天栏不出现这条。
- **一条接一条**：第二条必须等第一条那一轮真的结束（后端 `busy=false`）才发，绝不允许并发两条（并发会被后端挡下 ⇒ 顺序错乱）。
- **她还在朗读（音频没播完）**：不发；「上一句完毕后」才发（哪怕后端已经不忙）。
- **空文本 / 纯空白**：不入队、不发（`Main._send_chat` 已有守卫，`ChatQueue` 也必须忽略，别让空串占住队头）。

---

### Task 1: `ChatQueue` —— FIFO + 上限 + 提示文案 + 能不能发（纯逻辑）

**Files:**
- Create: `game/scripts/net/ChatQueue.gd`
- Create: `game/tests/test_chat_queue.gd`

**Interfaces:**
- Consumes: 无
- Produces（`class_name ChatQueue extends RefCounted`）：
  - `const CHAT_QUEUE_MAX := 5`、`const PREVIEW_CHARS := 8`
  - `func enqueue(text: String) -> bool` —— 空/纯空白或已满 ⇒ `false` 且入队不变；成功 ⇒ `true`
  - `func peek() -> String` / `func pop() -> String` —— 空时返回 `""`
  - `func size() -> int` / `func is_empty() -> bool` / `func clear() -> void`
  - `func hint_text() -> String` —— 空 ⇒ `""`；否则 `"排队中 %d 条 · %s"`（`%s` 为队头前 `PREVIEW_CHARS` 个字符，被截断时补 `…`）
  - `func should_flush(env: Dictionary) -> bool` —— `not is_empty() and not bool(env.get("busy", false)) and not bool(env.get("speaking", false))`

- [ ] **Step 1: 写失败测试**（`game/tests/test_chat_queue.gd`，`extends SceneTree`，仿 `test_speech_pairing.gd` 的 `_check(name, ok)` 写法；结尾 `if _fail == 0: print("[chatq] PASS：", _total, " 项断言全部通过") else: print("[chatq] FAIL：…")` + `quit(0 if _fail == 0 else 1)`）

断言（逐条对应一个 `_check`；每个用例用一台干净队列）：

```gdscript
var q := ChatQueue.new()
_check("空队列：is_empty / size=0 / 提示为空串", q.is_empty() and q.size() == 0 and q.hint_text() == "")
_check("空文本与纯空白不入队", not q.enqueue("") and not q.enqueue("   ") and q.is_empty())
_check("入队成功返回 true", q.enqueue("第一条") and q.enqueue("第二条"))
_check("hint_text：条数 + 队头前 8 字", q.hint_text() == "排队中 2 条 · 第一条")
_check("pop 出队即 FIFO", q.pop() == "第一条" and q.size() == 1 and q.peek() == "第二条")
_check("出到空：peek/pop 回空串、提示回空串",
    q.pop() == "第二条" and q.is_empty() and q.peek() == "" and q.pop() == "" and q.hint_text() == "")

var q2 := ChatQueue.new()
q2.enqueue("一二三四五六七八九十")
_check("超过 8 字截断并补省略号", q2.hint_text() == "排队中 1 条 · 一二三四五六七八…")

var q3 := ChatQueue.new()
q3.enqueue("1"); q3.enqueue("2"); q3.enqueue("3"); q3.enqueue("4"); q3.enqueue("5")
_check("上限 5 条：第 6 条被拒、队列不变",
    not q3.enqueue("6") and q3.size() == 5 and q3.peek() == "1" and q3.hint_text() == "排队中 5 条 · 1")

var q4 := ChatQueue.new()
_check("空队列不 flush（哪怕不忙不朗读）", not q4.should_flush({"busy": false, "speaking": false}))
q4.enqueue("甲")
_check("should_flush：非空 + 不忙 + 不朗读三条一起才算真",
    not q4.should_flush({"busy": true, "speaking": false})
    and not q4.should_flush({"busy": false, "speaking": true})
    and q4.should_flush({"busy": false, "speaking": false}))
```

- [ ] **Step 2: 跑测试看它失败**

Run: `E:\Godot\Godot_v4.7.1-stable_win64_console.exe --headless --path game --script res://tests/test_chat_queue.gd`
Expected: FAIL / 解析错误（`ChatQueue` 还不存在）

- [ ] **Step 3: 实现 `ChatQueue`**（`game/scripts/net/ChatQueue.gd`：`_items: Array[String]`；`pop()` 用 `pop_front()`；`hint_text()` 用 `substr(0, PREVIEW_CHARS)` + `text.length() > PREVIEW_CHARS` 时补 `…`）

- [ ] **Step 4: 跑测试看它通过**

Run: 同 Step 2 —— Expected: `[chatq] PASS`

- [ ] **Step 5: 提交**

```bash
git add game/scripts/net/ChatQueue.gd game/tests/test_chat_queue.gd
git commit -m "feat(game,chat): 聊天排队（FIFO/上限/提示文案）与门卫（v3.5.2）"
```

---

### Task 2: `NetClient` —— 忙则入队、闲则发、被挡下退回队列

**Files:**
- Modify: `game/scripts/net/NetClient.gd`
- Create: `game/tests/test_net_chat.gd`

**Interfaces:**
- Consumes: `ChatQueue`（Task 1：`enqueue` / `peek` / `pop` / `size` / `is_empty` / `hint_text` / `CHAT_QUEUE_MAX`）
- Produces（`NetClient`）：
  - `signal chat_queue_changed()` —— 入队/出队/退回时发一次（`Main` 据此刷提示）
  - `func chat(text: String) -> bool` —— 忙（或 `busy` 期间）⇒ 入队并返回是否入队成功；闲 ⇒ 立即发（`true`）；队列满 ⇒ `false`
  - `func has_queued_chat() -> bool` / `func queued_chat_count() -> int` / `func queued_chat_hint() -> String`
  - `func clear_queued_chat() -> void` —— 清空队列（测试用；清空后发一次 `chat_queue_changed`）
  - `func flush_queued_chat(env: Dictionary) -> void` —— 先过 `ChatQueue.should_flush(env)` 门禁（`env` 由调用方给 `{"busy": …, "speaking": …}`），再查连接与节流，最后取队头发出去；任一条不满足 ⇒ **原样留队**
  - `func _send_chat_now(text: String) -> void` —— 用 `fetch_bytes("/api/chat", …)` 发；回调里读到 `busy == true` ⇒ 退回队头（`ChatQueue` 只支持尾部入队，退回需要 `push_front`，见 Step 3 备注）

**备注（Step 3 会用到）**：退回队头要保序 ⇒ `ChatQueue` 需要 `func push_front(text: String) -> void`（本任务加，Task 1 的门卫补一条断言：「push_front 保序」）。

**两条护栏（Step 3 必须实现，防刷屏）**：
- `flush_queued_chat()` 只在 **WS 已连上**（`_ws != null and _ws.get_ready_state() == STATE_OPEN`）时才真发；未连接 ⇒ 原样留队、**不发任何请求**（后端没起时 Main 每帧催一次也不会变成请求风暴）。
- 发送失败（含被 `busy` 挡下）后置 `_chat_retry_ms = Time.get_ticks_msec() + CHAT_RETRY_GAP_MS`（`CHAT_RETRY_GAP_MS := 2000`），窗口内不再重发同一条 —— 一次意外失败最多每 2 秒重试一次。
- **已知取舍**：请求其实到了后端、只是回包丢了的情况下，退回队列会让这条**重复发一次**（她可能回两遍）。宁可极少数情况重复一次，也不吞玩家的话。

- [ ] **Step 1: 写失败测试**（`game/tests/test_net_chat.gd`）：**不把 `NetClient` 加进场景树**（避免 `_ready` 去连 WS），只测决策面：

```gdscript
var nc := NetClient.new()
var changed := 0
nc.chat_queue_changed.connect(func(): changed += 1)
nc.busy = true
_check("忙时 chat() 入队并返回 true", nc.chat("甲") and nc.has_queued_chat() and nc.queued_chat_count() == 1)
nc.chat("乙")
_check("提示文案只带队头前 8 字", nc.queued_chat_hint() == "排队中 2 条 · 甲")
_check("入队会发 chat_queue_changed", changed == 2)
nc.flush_queued_chat({"busy": true, "speaking": false})
nc.flush_queued_chat({"busy": false, "speaking": true})
_check("忙 / 还在朗读时不 flush（两条都原样留队）",
    nc.queued_chat_count() == 2 and nc.queued_chat_hint() == "排队中 2 条 · 甲")
nc.flush_queued_chat({"busy": false, "speaking": false})
_check("空闲但未连接：不丢消息（原样留队）",
    nc.queued_chat_count() == 2 and nc.queued_chat_hint() == "排队中 2 条 · 甲")
nc.chat("丙"); nc.chat("丁"); nc.chat("戊")
_check("队列满：第 6 条返回 false 且不吞", not nc.chat("己") and nc.queued_chat_count() == 5)
nc.clear_queued_chat()
_check("清空后提示为空、信号也发过", not nc.has_queued_chat() and nc.queued_chat_hint() == "" and changed >= 6)
_check("空文本连队都不进", not nc.chat("   ") and not nc.has_queued_chat())
```

- [ ] **Step 2: 跑测试看它失败**

Run: `… --script res://tests/test_net_chat.gd` —— Expected: FAIL（`chat()` 签名还是旧的无返回值版本）

- [ ] **Step 3: 实现**（要点）
  - `var _chat_queue := ChatQueue.new()`；`chat()` 按 `busy` 分流；入队/出队/退队后 `chat_queue_changed.emit()`。
  - `_post("/api/chat", …)` 换成 `fetch_bytes`（要读回包判 `busy`）；**保持 `POST /api/chat` 的请求体形状不变**（`{"text": text}`，`Content-Type: application/json`）。
  - 回调：`ok == false`（网络层失败/非 2xx）或回包 `busy == true` ⇒ 把这条 `push_front` 回队列（不丢、不刷屏）；成功 ⇒ 什么都不做。
  - `flush_queued_chat()`：未连接（`_ws == null` 或未 `STATE_OPEN`）⇒ 直接返回（消息留队）；否则 `pop()` 一条、`_send_chat_now()`。
  - 顺带保留既有公开方法签名（`player_move` / `hint` / … 全不动）。

- [ ] **Step 4: 跑测试看它通过**（同 Step 2）

- [ ] **Step 5: 提交**

```bash
git add game/scripts/net/ChatQueue.gd game/scripts/net/NetClient.gd game/tests/test_net_chat.gd
git commit -m "feat(game,net): 忙时聊天入队、忙回包退回、空闲可续发（v3.5.2）"
```

---

### Task 3: 右侧栏提示 + 每帧续发 + 不吞话 + 文档

**Files:**
- Modify: `game/scripts/ui/Main.gd`
- Modify: `README.md`（使用说明「输入框」一行 + 新增 `### v3.5.2`）
- Modify: `AGENTS.md`（版本口径一行）

**Interfaces:**
- Consumes: `NetClient.chat()` / `has_queued_chat()` / `queued_chat_hint()` / `flush_queued_chat()` / `chat_queue_changed`；`ChatQueue.CHAT_QUEUE_MAX`
- Produces: `Main._lbl_queue: Label`、`Main._refresh_chat_queue_hint()`

- [ ] **Step 1: 加提示标签**：在聊天输入行 `inp_row` 之后、功能按钮之前插入 `_lbl_queue`（11 号字、`Color(0.55, 0.55, 0.55)`、默认 `visible = false`、`autowrap_mode = AUTOWRAP_WORD_SMART`）

- [ ] **Step 2: 接线**：`_wire_agent()` 里 `agent.chat_queue_changed.connect(func(): _refresh_chat_queue_hint())`；`_refresh_chat_queue_hint()` 设 `text = agent.queued_chat_hint()`、`visible = agent.has_queued_chat()`

- [ ] **Step 3: 每帧续发**：`_process` 里现成的空闲分支（`if _azure_hold_active(): … else: …`）追加：
  `if agent != null and agent.has_queued_chat() and not _azure_hold_active(): agent.flush_queued_chat({"busy": agent.busy, "speaking": tts != null and tts.is_speaking()})`
  —— `not _azure_hold_active()` 已涵盖「她的回合结束了」+「她的音频播完了」+「还有没结清的语音/字」（`hold_active` 含 `busy(env)`，`busy(env)` 含 `playing`）；`env` 再过一遍状态机那层门禁（冗余但有门卫钉着）。不忙但没连上后端时 `flush_queued_chat` 自己会拒发，不会变成请求风暴。

- [ ] **Step 4: 不吞话**：`_send_chat()` 先调 `agent.chat(t)`；返回 `false`（队列满）⇒ 保留 `_chat_input.text`、`_set_status("排队已满（最多 %d 条），等她说完了再发" % ChatQueue.CHAT_QUEUE_MAX)`、直接 `return`（不进聊天栏）；返回 `true` 才照旧 `_allow_tts_fillers()` + `_add_msg("user", t)` + 清空输入框

- [ ] **Step 5: 冒烟（照项目口径全跑）**

```bash
cd backend && python -m unittest discover -s tests        # 354 tests OK（只改前端，须仍全绿）
cd katago_service && python -m unittest discover -s tests  # 36 tests OK
E:\Godot\Godot_v4.7.1-stable_win64_console.exe --headless --path game --quit                        # 无 SCRIPT ERROR
… --headless --path game --script res://tests/test_chat_queue.gd         # PASS
… --headless --path game --script res://tests/test_net_chat.gd           # PASS
… --headless --path game --script res://tests/test_speech_pairing.gd     # 87 项全 PASS（不许回退）
… --headless --path game --script res://tests/test_config.gd             # 24 项全 PASS
… --headless --path game --script res://tests/test_tts.gd                # 50 项全 PASS
```

端到端（临时脚本放 `C:\Users\z2996\AppData\Local\Temp\opencode`，不入库；假后端跑 8899 端口 + `STUB_LLM_DELAY=2` 让她「忙」得够久）：

1. 她忙时发一条 ⇒ 聊天栏有这条 + 右下角「排队中 1 条 · …」+ 后端**收到**这条（假后端留 `[stub] chat` 日志）。
2. 她这一轮结束、音频播完 ⇒ **自动**发出（没有第二次手动发送）、她的回复进来、右下角提示消失。
3. 连发两条 ⇒ 一条接一条依次发出（假后端日志顺序 = 发送顺序），第二条在第一条那一轮结束前不出现。

- [ ] **Step 6: 文档**：`README.md`「输入框」一行补「她忙时先排队（右下角显示条数与队头），她说完自动发出」；「九、更新」新增 `### v3.5.2（当前）`（并把 v3.5.1 的「（当前）」去掉）；`AGENTS.md` 版本口径改 v3.5.2

- [ ] **Step 7: 提交**

```bash
git add game/scripts/ui/Main.gd README.md AGENTS.md docs/plans/06-chat-queue-when-busy.md
git commit -m "feat(game,ui): 忙时聊天排队提示与自动续发，补齐文档（v3.5.2）"
```

---

## 验收（照本计划做完后逐条核）

- [ ] 忙时发聊天：消息进聊天栏、右下角出现提示、后端真的收到了（不丢）。
- [ ] 她上一句说完 ⇒ 自动发出、回复到达、提示消失；不需要玩家再发一条。
- [ ] 连发多条 ⇒ 依次（一条一轮）发出，顺序不乱。
- [ ] 队列满 ⇒ 输入框保留、状态栏提示、聊天栏不伪造气泡。
- [ ] 断线 ⇒ 提示仍在、消息仍在队列；重连后空闲时自动补发。
- [ ] `test_chat_queue.gd` / `test_net_chat.gd` / `test_speech_pairing.gd` / `test_config.gd` / `test_tts.gd` / `--quit` / 后端 354 / katago 36 全绿。
