"""LLM 调用：LangChain ``ChatOpenAI`` 的薄适配器（保留旧 ``chat()`` 契约）。

链的装配（模型参数、扩展字段合并）全在 ``llm_chain.py``；本模块只负责四件事：
- 把 ``LLMClient`` 的参数映射成链（``extra_body`` 被外部改写时重建）；
- 每次调用的输出上限（思考档位 1/2 各追加一段推理预算）：以线上键 ``max_tokens`` 顶层发出
  （按调用 ``bind(extra_body=...)`` 合并，用户配置打底、显式写死上限则让位）——``ChatOpenAI``
  自己的 ``max_tokens`` 会被改名成 ``max_completion_tokens``，而 DeepSeek 等目标服务端认的是前者；
- 把链抛出的异常映射成「调用出错：…」可读中文（绝不抛给上层）；
- 服务端拒绝参数（HTTP 400/422）时去掉扩展字段与注入的上限重试一次的安全网。

推理段永不显示给玩家：台词一律经 ``llm_chain.content_of``（只取 ``content``）。

对外接口：``chat(messages, max_tokens, no_think) -> str``（失败返回可读错误说明）；``chain``
（``ChatOpenAI``）供 ``bind_tools`` / ``astream`` / ``with_structured_output`` 复用——它**没有**
``chat()`` 的地址快拦（见 ``_url_ok``），直接用它发请求前必须自备同样的判断。
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
        # chain 只供 bind_tools / astream / with_structured_output 复用（Task 4/5）。一切真的发请求的
        # 台词路径必须走 chat()：它会先过 _url_ok() 快拦，而未配置 llm_base_url 时 openai 客户端会
        # 静默退回 api.openai.com，真连外网且单次卡几十秒。
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

    @staticmethod
    def _pins_budget(body) -> bool:
        """用户在 ``extra_body`` 里显式写死了输出上限：注入的预算必须让位（尊重配置）。"""
        return "max_tokens" in body or "max_completion_tokens" in body

    def _extra_for(self, budget) -> dict:
        """本次调用要绑定的扩展字段：用户配置打底，未被写死时补上输出上限。

        输出上限走 ``max_tokens``（``bind`` 到请求体顶层）而不是 ``ainvoke(max_tokens=...)``：
        后者会被 langchain-openai 改名成 ``max_completion_tokens``，目标服务端不认。
        """
        if self._extra_body_unsupported:
            return {}
        body = dict(self.extra_body or {})
        if not self._pins_budget(body):
            body["max_tokens"] = budget
        return body

    async def _call(self, messages, budget):
        """发一次请求：按调用把扩展字段与输出上限合并后 bind 到链上（不重建模型）。"""
        return await self._sync_chain().bind(extra_body=self._extra_for(budget)).ainvoke(messages)

    def _strip_on_reject(self, e) -> bool:
        """服务端明确拒绝参数（HTTP 400/422）时去掉扩展字段与注入的输出上限，重试一次。

        请求体里总有我们注入的 ``max_tokens``，所以 400/422 一律给一次「什么都不加」的机会
        （有的服务端只认 ``max_completion_tokens``，会直接拒绝 ``max_tokens``）；连接失败、超时
        这类传输层问题不算数，不许乱扣帽子。置位后永久不再加料。
        """
        if e.status_code not in (400, 422) or self._extra_body_unsupported:
            return False
        self._extra_body_unsupported = True
        self.extra_body = {}
        print("[LLM] 服务端拒绝参数（HTTP %d），去掉扩展字段与输出上限重试" % e.status_code)
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
            ai = await self._call(messages, budget)
        except openai.APIStatusError as e:
            print("[LLM] HTTP %d：%s" % (e.status_code, str(e)[:200]))
            if not self._strip_on_reject(e):
                return "调用出错：" + self._reason_of(e)
            try:
                ai = await self._call(messages, budget)
            except Exception as e2:                 # noqa: BLE001
                print("[LLM] 调用失败：%s" % e2)
                return "调用出错：" + self._reason_of(e2)
        except Exception as e:                      # noqa: BLE001
            print("[LLM] 调用失败：%s" % e)
            return "调用出错：" + self._reason_of(e)

        text = content_of(ai)
        return text if text != "" else FAIL_TEXT
