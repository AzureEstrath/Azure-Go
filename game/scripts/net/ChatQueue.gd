class_name ChatQueue
extends RefCounted
## 玩家聊天在「她忙」时的本地排队（计划 06）。
##
## 后端 `POST /api/chat` 在她忙（`busy`）时会直接挡下（回 `{"ok": false, "busy": true}`），
## 玩家那条话原来就此消失：聊天栏里看得见、她永远不回。这里把话先排起来——`NetClient` 负责
## 「忙则入队 / 闲则发 / 被挡下退回队头」，`Main` 只用 `hint_text()` 在右下角显示
## 「排队中 N 条 · 队头前几个字」，并在她上一句说完后催 `NetClient` 续发。
##
## 本类是**纯逻辑**（无副作用、不发请求）：顺序、上限、提示文案与「现在能不能发」的判据都在这里，
## 由 `game/tests/test_chat_queue.gd` 钉住。

const CHAT_QUEUE_MAX := 5          # 队列上限：满了不收（调用方保留输入框原文并提示，绝不吞玩家的话）
const PREVIEW_CHARS := 8           # 提示里队头消息的预览字数（与项目日志口径一致）

var _items: Array[String] = []

## 入队（尾部）。空/纯空白或已满 ⇒ `false`，且队列不变。
func enqueue(text: String) -> bool:
	if text.strip_edges() == "" or _items.size() >= CHAT_QUEUE_MAX:
		return false
	_items.append(text)
	return true

## 插到队头：给「发出去了却被后端挡下（busy）」的那条退回用——它本来是最该发的一条，
## 不许排到别人后面（退回也要保序）。
func push_front(text: String) -> void:
	_items.push_front(text)

## 队头（下一条该发的）；空队列回空串。
func peek() -> String:
	if _items.is_empty():
		return ""
	return _items[0]

## 取出队头（发出去了）；空队列回空串。
func pop() -> String:
	if _items.is_empty():
		return ""
	return _items.pop_front()

func size() -> int:
	return _items.size()

func is_empty() -> bool:
	return _items.is_empty()

func clear() -> void:
	_items.clear()

## 右下角那行提示：空队列 ⇒ `""`；否则 `排队中 N 条 · 队头前 8 字`（**被截断才**补省略号）。
func hint_text() -> String:
	if _items.is_empty():
		return ""
	var head := _items[0]
	var preview := head.substr(0, PREVIEW_CHARS)
	if head.length() > PREVIEW_CHARS:
		preview += "…"
	return "排队中 %d 条 · %s" % [_items.size(), preview]

## 现在能不能发：队列非空、她这一轮结束了（`busy` 假）、她的语音也播完了（`speaking` 假）——
## 玩家要的是「上一句完毕后立即发送下一句」。缺键按「不忙 / 没在朗读」处理（只认显式 `true`），
## 调用方少传一个键不会把队列卡死。
func should_flush(env: Dictionary) -> bool:
	return not is_empty() and not bool(env.get("busy", false)) and not bool(env.get("speaking", false))
