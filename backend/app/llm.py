"""LLM（OpenAI 兼容 /v1/chat/completions）调用。

剥离思考段落；思考档位只影响「她怎么想」，推理段永不显示给玩家。
用 httpx.AsyncClient，串行化与重试阶梯沿用旧客户端的实现。

对外接口：``chat(messages, max_tokens, no_think) -> str``（失败返回可读错误说明）。
"""

import asyncio
import re
import socket
import time

import httpx

REASON_BUDGET_MED = 160
REASON_STOP_MSG = "思考到此为止，请立即用中文直接给出最终回答，不要再继续分析。"

_TAG_OPEN = "<" + "think" + ">"     # 运行时拼接，和 Godot 版一样避免被外部工具当标记
_TAG_CLOSE = "</" + "think" + ">"
_THINK_RE = re.compile(re.escape(_TAG_OPEN) + r".*?" + re.escape(_TAG_CLOSE), re.S)
_URL_RE = re.compile(r"^(https?)://([^:/]+)(?::(\d+))?")

FAIL_TEXT = "唔...我脑子有点转不过来了"


class LLMClient:
    # 默认值是云端示例（与 game/config.json 模板、backend/app/config.py 一致）；实际运行时由 main.py 传入 cfg 的值
    def __init__(self, base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="", timeout_sec=120.0,
                 think_mode=0):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.think_mode = think_mode

        self._client = None
        self._lock = asyncio.Lock()
        self._base_ready = False
        self._dns_cache = {}
        self._think_params_unsupported = False

    # ================= 客户端 =================

    async def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_sec)
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_base(self):
        if self._base_ready:
            return
        self._base_ready = True
        if "://localhost" in self.base_url:
            self.base_url = self.base_url.replace("://localhost", "://127.0.0.1")
            print("[LLM] localhost → 127.0.0.1（避免解析到 IPv6 ::1 导致连接卡顿）")

    def _local_target(self) -> bool:
        return ("127.0.0.1" in self.base_url) or ("localhost" in self.base_url) or ("::1" in self.base_url)

    def _endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        key = self.api_key.strip()
        if key != "":
            h["Authorization"] = "Bearer " + key
        return h

    # ================= 连接预检 =================

    def _resolve_addrs(self, host):
        if host in ("127.0.0.1", "localhost", "::1"):
            return ["127.0.0.1", "::1"]         # 先 IPv4，再 IPv6
        if host in self._dns_cache:
            return self._dns_cache[host]
        out = []
        try:
            for info in socket.getaddrinfo(host, None):
                ip = info[4][0]
                if ip not in out:
                    out.append(ip)
        except Exception:
            pass
        if not out:
            out = [host]
        self._dns_cache[host] = out
        return out

    async def _reachable(self) -> bool:
        m = _URL_RE.search(self.base_url)
        if m is None:
            return True
        host = m.group(2)
        port = int(m.group(3)) if m.group(3) else (443 if m.group(1) == "https" else 80)
        addrs = self._resolve_addrs(host)
        t0 = time.monotonic()
        for a in addrs:
            if await self._try_connect(a, port):
                if a == "::1" and "::1" not in self.base_url:
                    print("[LLM] 服务只监听 IPv6：请在 config.json 里把地址写成 http://[::1]:端口/v1")
                return True
            if time.monotonic() - t0 > 3.0:
                break
        return False

    async def _try_connect(self, host, port) -> bool:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.7)
        except Exception:
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True

    # ================= 文本清洗 =================

    def clean_content(self, text) -> str:
        if not text:
            return ""
        out = _THINK_RE.sub("", text).strip()
        if _TAG_OPEN in out:                    # 思考段被截断（未闭合）
            return ""
        return out

    # ================= 主调用 =================

    async def chat(self, messages, max_tokens=300, no_think=False) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.9,
            # 降复读（OpenAI 兼容参数，支持它的服务端更稳）：
            # 让她少用重复的口头禅和句式，发言更像人
            "frequency_penalty": 0.4,
            "presence_penalty": 0.2,
            "cache_prompt": True,               # 复用上轮 KV 前缀：点评首字更快
        }
        with_think = not self._think_params_unsupported
        if with_think:
            body["chat_template_kwargs"] = {"enable_thinking": self.think_mode > 0 and not no_think}
            if self.think_mode == 1 and not no_think:
                body["reasoning_budget_tokens"] = REASON_BUDGET_MED
                body["reasoning_budget_message"] = REASON_STOP_MSG
        if not no_think:
            if self.think_mode == 2:
                body["max_tokens"] = max_tokens + 800
            elif self.think_mode == 1:
                body["max_tokens"] = max_tokens + REASON_BUDGET_MED

        r = await self._post(body)
        err_txt = str(r.get("_error", ""))
        if err_txt != "" and with_think and (err_txt.startswith("HTTP 400") or err_txt.startswith("HTTP 422")):
            # 只有服务端「明确拒绝参数」（400/422）才去掉重试；连接失败等不许乱扣帽子
            self._think_params_unsupported = True
            print("[LLM] 服务端不支持思考参数（多为云端 API），本次去掉重试")
            body.pop("chat_template_kwargs", None)
            body.pop("reasoning_budget_tokens", None)
            body.pop("reasoning_budget_message", None)
            r = await self._post(body)
            with_think = False
        if "_error" in r:
            return "调用出错：" + str(r["_error"])

        content = self._content_of(r)
        if content == "" and with_think and self.think_mode == 0:
            body.pop("chat_template_kwargs", None)
            r = await self._post(body)
            if "_error" not in r:
                content = self._content_of(r)
        if content == "" and with_think and self.think_mode > 0:
            # 推理段把预算全耗光、正文还没开口：改用快速模式立刻再要一次正式回答
            body["chat_template_kwargs"] = {"enable_thinking": False}
            body.pop("reasoning_budget_tokens", None)
            body.pop("reasoning_budget_message", None)
            body["max_tokens"] = max_tokens
            r = await self._post(body)
            if "_error" not in r:
                content = self._content_of(r)
        return content if content != "" else FAIL_TEXT

    def _content_of(self, r) -> str:
        choices = r.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        msg = first.get("message")
        if not isinstance(msg, dict):
            return ""
        return self.clean_content(str(msg.get("content", "")))

    async def _post(self, body):
        """串行入口：同一时刻只有一个请求在飞（闲置搭话可能与落子分析几乎同时发起）。"""
        async with self._lock:
            return await self._post_locked(body)

    async def _post_locked(self, body):
        self._ensure_base()
        if not await self._reachable():
            if self._local_target():
                return {"_error": "本地 LLM 服务暂未就绪（%s 正在加载或自动重试，最长几分钟），稍等再试" % self.base_url}
            return {"_error": "无法连接 LLM（%s），请确认服务已启动" % self.base_url}
        client = await self._http()
        try:
            resp = await client.post(self._endpoint(), headers=self._headers(), json=body)
        except Exception as e:
            print("[LLM] 连接中断：%s" % e)
            return {"_error": "LLM 连接被中断（%s），可能服务刚重启，稍等再试" % self.base_url}
        code = resp.status_code
        if code != 200:
            text = resp.text[:300]
            print("[LLM] HTTP %d: %s" % (code, text))
            if code == 503:
                if self._local_target():
                    return {"_error": "本地 LLM 正在加载模型（十几秒到半分钟），稍等一小会儿再和我说话吧"}
                return {"_error": "服务端暂时不可用（HTTP 503），稍后再试"}
            return {"_error": "HTTP %d（检查模型名是否为 %s）" % (code, self.model)}
        try:
            parsed = resp.json()
        except Exception:
            return {"_error": "响应不是合法 JSON"}
        if not isinstance(parsed, dict):
            return {"_error": "响应不是合法 JSON"}
        return parsed

    # ================= 探测（可选） =================

    async def probe_models(self):
        self._ensure_base()
        if not await self._reachable():
            if self._local_target():
                print("[LLM] 本地服务 %s 暂未就绪（正在加载或自动重试，稍后会自行恢复，不影响下棋）" % self.base_url)
            else:
                print("[LLM] 无法连接 %s（聊天功能将不可用），请确认服务已启动" % self.base_url)
            return
        try:
            client = await self._http()
            resp = await client.get(self.base_url.rstrip("/") + "/models", headers=self._headers(), timeout=5.0)
            parsed = resp.json()
            ids = [str(m.get("id", "")) for m in parsed.get("data", [])]
            print("[LLM] 可用模型：", ids, " 当前使用：", self.model)
        except Exception:
            pass

