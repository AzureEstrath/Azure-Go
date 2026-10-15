"""LLM 调用：LangChain ``ChatOpenAI`` 的薄适配器（保留旧 ``chat()`` 契约）。

链的装配（模型参数、扩展字段合并）全在 ``llm_chain.py``；本模块只负责四件事：
- 把 ``LLMClient`` 的参数映射成链（``extra_body`` 被外部改写时重建）；
- 每次调用的 ``max_tokens`` 预算（思考档位 1/2 各追加一段推理预算）；
- 把链抛出的异常映射成「调用出错：…」可读中文（绝不抛给上层）；
- 服务端拒绝扩展字段（HTTP 400/422）时去掉扩展字段重试一次的安全网。

推理段永不显示给玩家：台词一律经 ``llm_chain.content_of``（只取 ``content``）。

对外接口：``chat(messages, max_tokens, no_think) -> str``（失败返回可读错误说明）；
``chain``（``ChatOpenAI``）供 ``bind_tools`` / ``astream`` / ``with_structured_output`` 复用。
"""

from types import SimpleNamespace

import openai

from .llm_chain import FAIL_TEXT, build_chat_model, content_of

REASON_BUDGET_MED = 160         # 思考档位 1（中等）：推理段的追加预算
REASON_BUDGET_STRONG = 800      # 思考档位 2（强思考）：推理段的追加预算


def clean_content(text) -> str:
    """台词清洗（旧引用兼容）：唯一实现在 ``llm_chain.content_of``，这里只把纯文本包成消息。"""
    return content_of(SimpleNamespace(content=text))


class _ChainCfg:
    """``build_chat_model`` 需要的 cfg 形状：扩展字段已由调用方合并好，故 think 档位不再叠加。"""

    def __init__(self, cli, extra_body):
        self.llm_base_url = cli.base_url
        self.llm_model = cli.model
        self.llm_api_key = cli.api_key
        self.llm_think_mode = 0
        self.llm_extra_body = dict(extra_body or {})
        self.llm_think_extra_body = {}


class LLMClient:
    # 默认值是云端示例（与 game/config.json 模板、backend/app/config.py 一致）；实际运行时由 main.py 传入 cfg 的值
    def __init__(self, base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="", timeout_sec=120.0,
                 think_mode=0, http_client=None, http_async_client=None, extra_body=None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.think_mode = think_mode
        self.extra_body = dict(extra_body or {})    # 可写：被服务端拒绝后清空（见 _strip_on_reject）
        self._extra_body_unsupported = False
        self._http = http_client                    # 同步路径（invoke）：langchain-openai 1.x 只认它
        self._http_async = http_async_client        # 异步路径（ainvoke）：只认它，测试注入假客户端用
        self._built_extra_body = None
        self.chain = None
        self._sync_chain()

    # ================= 链 =================

    def _sync_chain(self):
        """按当前 ``extra_body`` 装配/重建链：外部改写 ``extra_body`` 后下次调用即生效。"""
        want = dict(self.extra_body or {})
        if self.chain is not None and self._built_extra_body == want:
            return self.chain
        self.chain = build_chat_model(_ChainCfg(self, want), http_client=self._http,
                                      http_async_client=self._http_async, timeout=self.timeout_sec)
        self._built_extra_body = want
        return self.chain

    async def close(self):
        """关闭链持有的异步连接（与旧实现的 ``close()`` 对应）；失败一律忽略。"""
        client = getattr(self.chain, "root_async_client", None)
        if client is None:
            return
        try:
            await client.close()
        except Exception:                           # noqa: BLE001
            pass

    # ================= 文本清洗 =================

    def clean_content(self, text) -> str:
        """历史 API 兼容（旧调用点与测试）：实现只有 ``llm_chain.content_of`` 一处。"""
        return clean_content(text)

    # ================= 主调用 =================

    def _url_ok(self) -> bool:
        """地址必须带 http(s)://：``base_url`` 为空时 openai 客户端会静默退回 api.openai.com，
        那样「没配地址」就会变成真连外网并卡住整个回合，必须在发请求前拦下。"""
        u = str(self.base_url or "").strip().lower()
        return u.startswith("http://") or u.startswith("https://")

    def _budget(self, max_tokens, no_think) -> int:
        """思考档位的输出上限：推理段与正文共用，按档位追加一段预算。"""
        if no_think:
            return max_tokens
        if self.think_mode == 2:
            return max_tokens + REASON_BUDGET_STRONG
        if self.think_mode == 1:
            return max_tokens + REASON_BUDGET_MED
        return max_tokens

    def _strip_on_reject(self, e) -> bool:
        """服务端明确拒绝扩展字段（HTTP 400/422）时清空 ``extra_body``（只做一次）。

        只有「确实带着扩展字段」的 400/422 才算数：连接失败等不许乱扣帽子。
        """
        if e.status_code not in (400, 422) or not self.extra_body or self._extra_body_unsupported:
            return False
        self._extra_body_unsupported = True
        self.extra_body = {}
        print("[LLM] 服务端拒绝扩展字段（HTTP %d），本次去掉重试" % e.status_code)
        return True

    def _reason_of(self, e) -> str:
        """把链抛出的异常映射成可读中文（不含任何凭据）。"""
        if isinstance(e, openai.APIStatusError):
            code = e.status_code
            if code in (401, 403):
                return "HTTP %d（检查 llm_api_key 是否正确）" % code
            if code == 429:
                return "HTTP 429（触发限流），稍等再试"
            if code == 503:
                return "服务端暂时不可用（HTTP 503），稍后再试"
            return "HTTP %d（检查模型名是否为 %s）" % (code, self.model)
        if isinstance(e, openai.APITimeoutError):
            return "请求超时（%.0f 秒），稍后再试" % self.timeout_sec
        if isinstance(e, openai.APIConnectionError):
            return "无法连接 LLM（%s），请确认服务地址与网络" % self.base_url
        return "LLM 调用失败（%s）" % type(e).__name__

    async def chat(self, messages, max_tokens=300, no_think=False) -> str:
        if not self._url_ok():
            return "调用出错：未配置 LLM 地址（llm_base_url），请在 config.json 里填写"
        budget = self._budget(max_tokens, no_think)
        try:
            ai = await self._sync_chain().ainvoke(messages, max_tokens=budget)
        except openai.APIStatusError as e:
            print("[LLM] HTTP %d：%s" % (e.status_code, str(e)[:200]))
            if not self._strip_on_reject(e):
                return "调用出错：" + self._reason_of(e)
            try:
                ai = await self._sync_chain().ainvoke(messages, max_tokens=budget)
            except Exception as e2:                 # noqa: BLE001
                print("[LLM] 调用失败：%s" % e2)
                return "调用出错：" + self._reason_of(e2)
        except Exception as e:                      # noqa: BLE001
            print("[LLM] 调用失败：%s" % e)
            return "调用出错：" + self._reason_of(e)

        text = content_of(ai)
        return text if text != "" else FAIL_TEXT
