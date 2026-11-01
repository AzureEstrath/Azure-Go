"""知识查证 + 结构化记忆（v4.6.0）。

Azure 遇到自己不懂的知识时，先用**网页版 DeepSeek**查一次，把结果拆成
``topic / summary / facts / confidence``，写成 ``kind="knowledge"`` 的长期记忆；
下一轮相关话题再把「她查过的资料」作为软提示注入。

边界：
- 网页版需要用户自己的浏览器会话 token / cookie（``DEEPSEEK_WEB_TOKEN`` /
  ``DEEPSEEK_WEB_COOKIE``）；没有配置时 ``available=False``，**不发任何网络请求**；
- 查证是**后台任务**，不阻塞聊天回包，也不改变 ``busy``；
- 网页协议变化 / 超时 / 空结果都只返回 ``None``，绝不影响正常出话；
- 只存结构化摘要，不存原始网页 / 不存 token / cookie；置信度不足不写记忆。
"""

import asyncio
import json
import re
import threading
import time
import urllib.request

MAX_TOPIC = 48
MAX_SUMMARY = 320
MAX_FACTS = 4
MAX_FACT_CHARS = 90
MAX_QUERY = 120
# 失败后的冷却比成功短：一次超时 / 端点抖动不该把整句话题锁 10 分钟。
FAIL_COOLDOWN_SEC = 60.0

_QUESTION_RE = re.compile(
    r"(什么是|是什么|为什么|为何|怎么|如何|原理|区别|历史|介绍|解释|科普|有哪些|"
    r"能干什么|你知道吗|帮我查|查一下|查查|搜一下|求科普|讲讲)", re.I)
_UNCERTAIN_RE = re.compile(
    r"(不确定|不知道|没查到|不太了解|说不好|不清楚|没把握|记不清|不太懂|没学过)")
_BLOCK_RE = re.compile(
    r"(日程|提醒|安排|课表|几点|明天|今天|后天|下棋|这手|胜率|盘面|"
    r"落子|棋子|棋谱|复盘|作业|考试|活动|报名)", re.I)
# 「吃 / 睡」只在**私人日常**语境里挡（「你吃饭了吗」「我睡醒了」）；
# 「为什么人会睡觉」「睡眠为什么重要」这类科普问句要放行。
_PERSONAL_RE = re.compile(r"(吃|睡)(饭|觉)?(了|过|什么|啥|点|多久|没|不|好|着|吗|吧|得|醒)")
_WS_RE = re.compile(r"[\s\u3000]+")
_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def clip(text, limit):
	"""压成单行并截断；真的截了才加省略号。"""
	s = _WS_RE.sub(" ", str(text or "")).strip()
	if limit <= 0:
		return ""
	return s if len(s) <= limit else s[:limit - 1] + "…"


def query_of(text):
	"""把玩家原话收成一句查证 query（去掉称呼 / 客套 / 句末语气词）。"""
	s = _WS_RE.sub(" ", str(text or "")).strip()
	s = re.sub(r"^(请问|你知道|你知不知道|能不能|可以|帮我|麻烦|Azure|azure|小蓝)\s*", "", s)
	s = s.lstrip("，,。.:：;；!！?？ \t")
	# 称呼后面常跟「，帮我…」：剥掉称呼与标点后再剥一层客套。
	s = re.sub(r"^(请问|你知道|你知不知道|能不能|可以|帮我|麻烦)\s*", "", s)
	s = re.sub(r"[吗呢呀啊么？?]+$", "", s).strip()
	return clip(s, MAX_QUERY)


def need(text, reply=""):
	"""规则判断：这句话是否值得去查一次知识（纯函数，可单测）。

    排除日程 / 棋局 / 私人事实；明确的知识问句、或她上一句露怯（「我不太确定」）时触发。
    """
	s = str(text or "").strip()
	if len(s) < 6:
		return False
	if _BLOCK_RE.search(s) or _PERSONAL_RE.search(s):
		return False
	if _QUESTION_RE.search(s):
		return True
	if _UNCERTAIN_RE.search(str(reply or "")) and ("什么" in s or "为什么" in s or len(s) >= 10):
		return True
	return False


def _first_json(text):
	"""从网页回答里取第一个可解析的 JSON 对象 / 数组（允许外层有说明文字）。"""
	s = str(text or "").strip()
	for m in re.finditer(r"[\[{]", s):
		start = m.start()
		depth = 0
		in_str = False
		esc = False
		for i in range(start, len(s)):
			ch = s[i]
			if in_str:
				if esc:
					esc = False
				elif ch == "\\":
					esc = True
				elif ch == '"':
					in_str = False
				continue
			if ch == '"':
				in_str = True
			elif ch in "[{":
				depth += 1
			elif ch in "]}":
				depth -= 1
				if depth == 0:
					try:
						return json.loads(s[start:i + 1])
					except (TypeError, ValueError):
						break
	return None


def _string_list(v):
	if not isinstance(v, list):
		return []
	out = []
	for x in v:
		s = clip(x, MAX_FACT_CHARS)
		if s and s not in out:
			out.append(s)
	return out[:MAX_FACTS]


def parse_structured(text, query, source, min_confidence=0.55, max_chars=MAX_SUMMARY):
	"""网页 / 模型回答 → 结构化知识条目；不可用或置信度不足返回 ``None``。"""
	raw = str(text or "").strip()
	if raw == "":
		return None
	obj = _first_json(raw)
	topic = clip(query, MAX_TOPIC)
	summary = ""
	facts = []
	conf = 0.55
	if isinstance(obj, dict):
		topic = clip(obj.get("topic") or obj.get("title") or query, MAX_TOPIC)
		summary = clip(obj.get("summary") or obj.get("answer") or "", max_chars)
		facts = _string_list(obj.get("facts") or obj.get("points") or [])
		try:
			conf = float(obj.get("confidence", 0.55))
		except (TypeError, ValueError):
			conf = 0.55
		if summary == "":
			return None                     # 有 JSON 但没正文：当成格式错误，不存
	else:
		# 网页没有按 JSON 回答：保留正文，但只当**低置信**摘要（默认阈值下不写记忆）。
		clean = re.sub(r"\s+", " ", raw).strip()
		clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean).strip()
		summary = clip(clean, max_chars)
		conf = 0.5
	if summary == "" or topic == "":
		return None
	conf = max(0.0, min(1.0, conf))
	if conf < float(min_confidence):
		return None
	return {
		"topic": topic,
		"summary": summary,
		"facts": facts,
		"confidence": conf,
		"source": str(source or ""),
		"query": clip(query, MAX_QUERY),
	}


def memory_text(item):
	"""拟人化记忆正文：写成「她查过什么」，不是网页摘录。"""
	topic = clip(item.get("topic", ""), MAX_TOPIC)
	summary = clip(item.get("summary", ""), MAX_SUMMARY)
	facts = _string_list(item.get("facts") or [])
	parts = ["她查过「%s」：%s" % (topic, summary)]
	if facts:
		parts.append("要点：" + "；".join(facts))
	return clip(" ".join(parts), MAX_SUMMARY + 260)


def topic_tag(topic):
	s = _SLUG_RE.sub("", str(topic or "").lower())
	return "knowledge:" + (s[:36] or "general")


def _note_key(topic):
	return _SLUG_RE.sub("", str(topic or "").lower())


def _grams(text):
	s = _SLUG_RE.sub("", str(text or "").lower())
	return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def render_note(rows, query, limit=2, max_chars=440):
	"""从 ``kind=knowledge`` 记忆里挑与当前 query 相关的条目，拼成注入正文。"""
	q = str(query or "").strip()
	if q == "":
		return ""
	qgrams = _grams(q)
	if not qgrams:
		return ""
	scored = []
	for row in rows or []:
		if not isinstance(row, dict):
			continue
		meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
		topic = str(meta.get("topic") or "")
		summary = str(meta.get("summary") or "")
		if not topic or not summary:
			continue
		overlap = len(qgrams & (_grams(topic) | _grams(summary)))
		if overlap <= 0:
			continue
		scored.append((overlap, float(row.get("ts", 0) or 0), topic, summary,
		                   _string_list(meta.get("facts") or [])))
	if not scored:
		return ""
	scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
	lines = []
	used = 0
	for _overlap, _ts, topic, summary, facts in scored[:max(1, int(limit))]:
		line = "「%s」：%s" % (clip(topic, MAX_TOPIC), clip(summary, 200))
		if facts:
			line += "（要点：%s）" % "；".join(clip(f, 60) for f in facts[:2])
		if used + len(line) > max_chars and lines:
			break
		lines.append(line)
		used += len(line)
	return "\n".join("- " + x for x in lines)


def _collect_strings(obj, out):
	"""递归收集网页响应里的文本字段（SSE / 普通 JSON 共用）。"""
	if isinstance(obj, dict):
		for k, v in obj.items():
			if k in ("content", "text", "answer", "response", "result", "message") and isinstance(v, str):
				if v.strip():
					out.append(v.strip())
			else:
				_collect_strings(v, out)
	elif isinstance(obj, list):
		for x in obj:
			_collect_strings(x, out)


class DeepSeekWebClient:
	"""网页版 DeepSeek 的最小客户端：session → completion → SSE / JSON 文本。

    说明：网页协议不是公开 API，端点与体可能随站点更新；失败一律返回空串，
    不影响本地聊天。token / cookie 只从配置或环境变量读取，不写日志。
    """

	def __init__(self, cfg):
		self.url = str(getattr(cfg, "deepseek_web_url", "") or "").strip()
		self.create_url = str(getattr(cfg, "deepseek_web_create_url", "") or "").strip()
		self.token = str(getattr(cfg, "deepseek_web_token", "") or "").strip()
		self.cookie = str(getattr(cfg, "deepseek_web_cookie", "") or "").strip()
		self.session_id = str(getattr(cfg, "deepseek_web_session_id", "") or "").strip()
		try:
			self.timeout = max(5.0, float(getattr(cfg, "deepseek_web_timeout_sec", 30.0) or 30.0))
		except (TypeError, ValueError):
			self.timeout = 30.0
		self.last_error = ""
		self._lock = threading.Lock()

	@property
	def configured(self):
		return bool(self.url and (self.token or self.cookie))

	def _headers(self):
		h = {
			"Content-Type": "application/json",
			"Accept": "application/json, text/event-stream",
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Azure/4.6",
			"Referer": "https://chat.deepseek.com/",
			"Origin": "https://chat.deepseek.com",
		}
		if self.token:
			h["Authorization"] = self.token if self.token.lower().startswith("bearer ") \
				else "Bearer " + self.token
		if self.cookie:
			h["Cookie"] = self.cookie
		return h

	def _post(self, url, payload):
		body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
		req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
		with urllib.request.urlopen(req, timeout=self.timeout) as resp:
			raw = resp.read()
			ctype = str(resp.headers.get("Content-Type", ""))
		return raw.decode("utf-8", "replace"), ctype

	def _ensure_session(self):
		if self.session_id:
			return self.session_id
		if not self.create_url:
			return ""
		try:
			text, _ctype = self._post(self.create_url, {"character_id": None})
			obj = _first_json(text) or {}
			# 站点可能把 id 藏在 data / biz_data / chat_session 里；递归找第一个 id 字符串。
			found = []

			def walk(v):
				if isinstance(v, dict):
					for k, x in v.items():
						if k in ("id", "chat_session_id", "session_id") and isinstance(x, str):
							found.append(x)
						else:
							walk(x)
				elif isinstance(v, list):
					for x in v:
						walk(x)
			walk(obj)
			self.session_id = found[0] if found else ""
		except Exception as e:              # noqa: BLE001
			self.last_error = "create: %r" % e
			self.session_id = ""
		return self.session_id

	def ask(self, query):
		"""同步查一次，返回网页原文；失败返回 ``""`` 并写 ``last_error``。"""
		if not self.configured:
			self.last_error = "not configured"
			return ""
		with self._lock:                    # 网页 session 串行，避免并发写乱上下文
			prompt = (
				"你是知识查证助手。只回答客观事实，不寒暄、不编造，也不要提你的身份。\n"
				"用简体中文严格输出 JSON（不要 markdown 代码块）：\n"
				'{"topic":"不超过24字","summary":"不超过160字","facts":["最多4条，每条40字内"],'
				'"confidence":0.0}\n'
				"confidence 表示你对事实的把握；不确定就给低分。\n\n问题：%s" % clip(query, MAX_QUERY)
			)
			sid = self._ensure_session()
			body = {
				"chat_session_id": sid,
				"parent_message_id": None,
				"prompt": prompt,
				"ref_file_ids": [],
				"thinking_enabled": False,
				"search_enabled": True,
			}
			try:
				text, ctype = self._post(self.url, body)
			except Exception as e:          # noqa: BLE001
				self.last_error = repr(e)
				return ""
			try:
				return _extract_web_text(text, ctype)
			except Exception as e:          # noqa: BLE001
				self.last_error = "parse: %r" % e
				return ""


def _extract_web_text(text, ctype=""):
	"""网页返回可能是 SSE 增量，也可能是普通 JSON；取最长完整文本，避免重复。"""
	body = str(text or "")
	if "event-stream" in str(ctype).lower() or "data:" in body[:200]:
		acc = ""
		for line in body.splitlines():
			s = line.strip()
			if not s.startswith("data:"):
				continue
			data = s[5:].strip()
			if data == "[DONE]" or data == "":
				continue
			try:
				obj = json.loads(data)
			except (TypeError, ValueError):
				continue
			got = []
			_collect_strings(obj, got)
			if not got:
				continue
			piece = max(got, key=len)
			# 网页可能发增量片段，也可能发累积全文：两种都按「不重复」拼。
			if acc and piece.startswith(acc):
				acc = piece
			elif piece not in acc:
				acc += piece
		return acc.strip()
	obj = _first_json(body)
	if obj is None:
		return body.strip()
	got = []
	_collect_strings(obj, got)
	if not got:
		return ""
	# 普通 JSON 里常同时有增量与最终全文；取最长那条。
	return max(got, key=len).strip()


class KnowledgeService:
	"""查证 → 结构化 → 记忆 的门面；关闭 / 无配置时零请求。"""

	def __init__(self, cfg, memory=None, llm=None, web=None):
		self.cfg = cfg
		self.memory = memory
		self.llm = llm
		self.provider = str(getattr(cfg, "knowledge_provider", "deepseek_web") or "").lower()
		self.enabled = bool(getattr(cfg, "knowledge_enabled", False)) \
			and self.provider != "off"
		self.web = web if web is not None else DeepSeekWebClient(cfg)
		try:
			self.min_conf = max(0.0, min(1.0, float(
				getattr(cfg, "knowledge_confidence_min", 0.55) or 0.55)))
			self.max_chars = max(120, int(getattr(cfg, "knowledge_max_chars", 420) or 420))
			self.cooldown = max(60.0, float(
				getattr(cfg, "knowledge_lookup_cooldown_sec", 600) or 600))
		except (TypeError, ValueError):
			self.min_conf, self.max_chars, self.cooldown = 0.55, 420, 600.0
		self._last = {}
		self._lock = threading.Lock()

	@property
	def available(self):
		if not self.enabled:
			return False
		if self.provider in ("deepseek_web", "auto"):
			return bool(getattr(self.web, "configured", False))
		if self.provider == "llm":
			return self.llm is not None
		return False

	def _cool(self, query):
		"""是否还在冷却里；**只读**，不因为一次失败就把整句话题锁很久。"""
		key = query_of(query).lower()
		now = time.monotonic()
		with self._lock:
			got = self._last.get(key)
			if not got:
				return False
			ts, ok = got if isinstance(got, tuple) else (float(got), True)
			window = self.cooldown if ok else min(self.cooldown, FAIL_COOLDOWN_SEC)
			return now - ts < window

	def _mark(self, query, ok):
		"""记一次查证结果的时间：成功用整段冷却，失败只用短冷却。"""
		key = query_of(query).lower()
		with self._lock:
			self._last[key] = (time.monotonic(), bool(ok))

	def _finish(self, query, raw, source):
		"""收尾：先记失败冷却，解析成功再升级成成功冷却。"""
		self._mark(query, False)
		if raw == "":
			return None
		item = parse_structured(raw, query, source, self.min_conf, self.max_chars)
		if item is not None:
			self._mark(query, True)
		return item

	@staticmethod
	def _llm_msgs(query):
		return [{"role": "system", "content": "你是知识查证助手，只输出结构化 JSON，不寒暄。"},
		        {"role": "user", "content": (
					'请回答并用 JSON 输出：{"topic":"...","summary":"...",'
					'"facts":["..."],"confidence":0.0}\n问题：%s' % clip(query, MAX_QUERY))}]

	def _ask_llm(self, query):
		"""同步兜底（只给同步 lookup / 测试用）；事件循环里请走 ``_ask_llm_async``。"""
		if self.llm is None:
			return ""
		try:
			out = asyncio.run(self.llm.chat(self._llm_msgs(query), 260, no_think=True, think_mode=0))
			return str(out or "")
		except Exception as e:              # noqa: BLE001
			print("[Knowledge] 本地兜底失败（已忽略）：%s" % e)
			return ""

	async def _ask_llm_async(self, query):
		"""异步兜底：LLM 的生成锁绑在当前事件循环上，不能在别的线程里 ``asyncio.run``。"""
		if self.llm is None:
			return ""
		try:
			out = await self.llm.chat(self._llm_msgs(query), 260, no_think=True, think_mode=0)
			return str(out or "")
		except Exception as e:              # noqa: BLE001
			print("[Knowledge] 本地兜底失败（已忽略）：%s" % e)
			return ""

	def lookup(self, query):
		"""同步查一次并返回结构化条目（不写记忆）；不可用 / 冷却 / 低置信返回 None。

		同步版只覆盖网页版（测试与脚本接缝）；本地 LLM 兜底在事件循环里必须走
		:meth:`lookup_async`。
		"""
		q = query_of(query)
		if not self.available or q == "" or self._cool(q):
			return None
		raw = ""
		source = ""
		if self.provider in ("deepseek_web", "auto"):
			try:
				raw = str(self.web.ask(q) or "")
			except Exception as e:          # noqa: BLE001 —— 网页协议变化 / 网络错误只当没查到
				print("[Knowledge] 网页查证失败（已忽略）：%s" % e)
				raw = ""
			source = "DeepSeek 网页版"
		if raw == "" and self.provider == "llm":
			raw = self._ask_llm(q)
			source = "本地模型兜底"
		return self._finish(q, raw, source)

	async def lookup_async(self, query):
		"""异步查一次（生产用）：网页请求线程化，LLM 兜底在当前循环里 await。"""
		q = query_of(query)
		if not self.available or q == "" or self._cool(q):
			return None
		raw = ""
		source = ""
		if self.provider in ("deepseek_web", "auto"):
			try:
				raw = str(await asyncio.to_thread(self.web.ask, q) or "")
			except Exception as e:          # noqa: BLE001 —— 网页协议变化 / 网络错误只当没查到
				print("[Knowledge] 网页查证失败（已忽略）：%s" % e)
				raw = ""
			source = "DeepSeek 网页版"
		if raw == "" and (self.provider == "llm" or bool(
				getattr(self.cfg, "knowledge_llm_fallback", False))):
			raw = await self._ask_llm_async(q)
			source = "本地模型兜底"
		return self._finish(q, raw, source)

	def remember(self, item):
		"""把结构化条目写成 ``kind=knowledge`` 记忆；同一话题旧条目 supersede。"""
		if self.memory is None or not isinstance(item, dict):
			return None
		text = memory_text(item)
		tag = topic_tag(item.get("topic", ""))
		meta = dict(item)
		meta["learned_at"] = time.time()
		mid = self.memory.remember_knowledge(text, tag, meta)
		if mid:
			try:
				self.memory.supersede_knowledge(tag, keep_id=int(mid))
			except Exception as e:          # noqa: BLE001
				print("[Knowledge] 旧知识失效失败（已忽略）：%s" % e)
		return mid

	def lookup_and_remember(self, query):
		item = self.lookup(query)
		if item is None:
			return None
		return self._remember_item(item)

	async def lookup_and_remember_async(self, query):
		"""生产路径：异步查证 + 写记忆（写库走线程，不冻事件循环）。"""
		item = await self.lookup_async(query)
		if item is None:
			return None
		return await asyncio.to_thread(self._remember_item, item)

	def _remember_item(self, item):
		mid = self.remember(item)
		if mid:
			print("[Knowledge] 已记住「%s」（%s，置信度 %.2f）"
				% (clip(item.get("topic", ""), 24), item.get("source", ""),
				   float(item.get("confidence", 0.0))))
		return item

	def recent_rows(self, limit=20):
		"""最近的结构化知识记忆（管理面 / 调试）；未启用 / 失败返回 ``[]``。"""
		if self.memory is None:
			return []
		try:
			rows = list(self.memory.knowledge_rows(limit) or [])
		except Exception as e:              # noqa: BLE001
			print("[Knowledge] 读取知识记忆失败（已忽略）：%s" % e)
			return []
		out = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
			out.append({
				"id": row.get("id"),
				"ts": row.get("ts"),
				"tag": row.get("tag", ""),
				"topic": str(meta.get("topic") or ""),
				"summary": str(meta.get("summary") or ""),
				"facts": _string_list(meta.get("facts") or []),
				"confidence": meta.get("confidence", 0.0),
				"source": str(meta.get("source") or ""),
				"query": str(meta.get("query") or ""),
			})
		return out

	def note_for(self, query):
		"""下一轮相关话题的注入正文（不含标题）；关闭 / 失败 / 无命中返回空串。"""
		if not self.enabled or self.memory is None:
			return ""
		try:
			rows = self.memory.knowledge_rows(60)
		except Exception as e:              # noqa: BLE001
			print("[Knowledge] 读取知识记忆失败（已忽略）：%s" % e)
			return ""
		try:
			return render_note(rows, query)
		except Exception as e:              # noqa: BLE001
			print("[Knowledge] 知识注入失败（已忽略）：%s" % e)
			return ""
