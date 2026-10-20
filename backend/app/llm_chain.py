"""LangChain 模型构造与扩展字段（``ChatOpenAI`` 的唯一装配点）。

职责：
- ``build_chat_model``：把 ``AzureConfig`` 的云端 LLM 字段映射成 ``ChatOpenAI``；
- ``merge_extra_body``：``llm_extra_body`` 打底，思考档位开启时并入 ``llm_think_extra_body``；
- ``content_of`` / ``content_text_of``：只取 ``AIMessage.content`` 并剥离 think 段——**推理段永不显示给玩家**
  （DeepSeek-R1 类模型的 ``reasoning_content`` 只留在消息对象里，绝不进台词；把思维链写进 content 通道
  的模型靠 ``<think>…</think>`` 或收尾标记 ``<｜end▁of▁thinking｜>`` 划界）。
- ``ThinkStream``：**流式**版的同一套剥离——流式 chunk 会把标签与推理段切到不同块，逐块剥离拦不住，
  所以逐块只取原文、由它跨块维护「是否还在推理段」的状态，delta 与最终台词都取自剥离后的文本。

httpx 客户端由调用方注入（``http_client`` / ``http_async_client``）：langchain-openai 1.x
的异步路径只认后者，两个都注入才能让测试全程不触网。
"""

import re

from langchain_openai import ChatOpenAI

FAIL_TEXT = "唔...我脑子有点转不过来了"

# 云端模型存放推理段的键名（DeepSeek-R1 等）：只用于识别，永不进入台词
REASON_KEYS = ("reasoning_content", "reasoning")

_TAG_OPEN = "<" + "think" + ">"     # 运行时拼接，和 Godot 版一样避免被外部工具当标记
_TAG_CLOSE = "</" + "think" + ">"
# 推理段的开始 / 收尾标记（有的模型把思维链写在 content 通道里，用它们圈出区间）
_TAG_BEGIN = "<" + "｜begin▁of▁thinking｜" + ">"
_TAG_END = "<" + "｜end▁of▁thinking｜" + ">"
_THINK_RE = re.compile(re.escape(_TAG_OPEN) + r".*?" + re.escape(_TAG_CLOSE), re.S)
# 成对的 begin…end 区间（含两个标记）：区间两侧的正文都留
_BEGIN_END_RE = re.compile(re.escape(_TAG_BEGIN) + r".*?" + re.escape(_TAG_END), re.S)

# 需要「尾部滞留」的标记：任何一个的前半截都可能被切到下一块，不能当正文发出去
_THINK_TOKENS = (_TAG_OPEN, _TAG_CLOSE, _TAG_BEGIN, _TAG_END)

_EMPTY_API_KEY = "EMPTY"        # 空 key 的哨兵：把「缺凭据」从构造期推迟到请求期
DEFAULT_TIMEOUT_SEC = 120.0     # 与旧 LLMClient 的 timeout_sec 默认值一致


def merge_extra_body(cfg) -> dict:
    """``llm_extra_body`` 打底；``think_mode > 0`` 时并入 ``llm_think_extra_body``。"""
    merged = dict(cfg.llm_extra_body or {})
    if cfg.llm_think_mode > 0:
        merged.update(cfg.llm_think_extra_body or {})
    return merged


def build_chat_model(cfg, *, http_client=None, http_async_client=None, timeout=None,
                     temperature=0.7, top_p=0.9, frequency_penalty=0.4,
                     presence_penalty=0.2) -> ChatOpenAI:
    """按配置构造 ChatOpenAI；扩展字段为空时不带 ``extra_body``（不改请求体形状）。

    空 ``llm_api_key``（模板现状）与空 ``llm_base_url`` 都不得在构造期抛错：
    缺凭据时用哨兵值顶上，失败推迟到请求期，由上层映射成可读错误走兜底台词。

    ``timeout``（秒）缺省时用 ``DEFAULT_TIMEOUT_SEC``：调用方（``LLMClient.timeout_sec``）
    传什么就是什么，不能只留一个不起作用的常量。

    ``max_retries`` 固定 0：SDK 默认重试 2 次，配合超时最坏能拖住 ``busy`` 几分钟——失败走
    可读错误 + 兜底台词才是设计好的降级（要重试由上层自己决定）。
    """
    extra = merge_extra_body(cfg)
    return ChatOpenAI(
        model=cfg.llm_model,
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key or _EMPTY_API_KEY,
        temperature=temperature,
        top_p=top_p,
        frequency_penalty=frequency_penalty,     # 降复读：少用重复的口头禅和句式
        presence_penalty=presence_penalty,
        timeout=DEFAULT_TIMEOUT_SEC if timeout is None else timeout,
        max_retries=0,
        http_client=http_client,
        http_async_client=http_async_client,
        **({"extra_body": extra} if extra else {}),
    )


def _strip_think(text: str) -> str:
    """剥离推理段（**不 strip**）：标记区间（含标记自身）与成对 ``<think>…</think>`` 之内的都不是正文。

    对**整条**原文与**流式累加**文本是同一套规则（``content_text_of`` 与 ``ThinkStream`` 都走这里），
    所以「逐块拼起来的台词」与「对整条原文调 ``content_of``」同构。三条规则：

    1. ``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`` 整段（含两个标记）去掉，区间两侧的正文都留；
    2. 只有开始标记、还没收尾：它之后的先不算正文；
    3. 只有收尾标记（开始标记被网关吞掉）：它之前的全是推理——**这一条只能事后判定**，
       标记到达之前已经流出去的内容收不回来（见 ``ThinkStream`` 的保证边界）。
    """
    text = _BEGIN_END_RE.sub("", text)      # ① 成对的 begin…end 区间（含两个标记）
    i = text.find(_TAG_BEGIN)
    if i >= 0:
        text = text[:i]                     # ② 开始标记之后还没收尾：之后的先不算正文
    elif _TAG_END in text:
        text = text.rsplit(_TAG_END, 1)[1]  # ③ 只剩收尾标记：它之前全是推理（事后判定）
    return _THINK_RE.sub("", text)          # ④ 成对 <think>…</think>


def _hold_partial_tag(raw: str) -> str:
    """扣住尾部「可能是某个标记前半截」的最长后缀（``<thi`` / ``<｜beg`` / ``<｜end`` …）。

    丢弃这段的正文不发：下一块补齐了才知道它是标记还是普通文本；留着它就是「标签被切块」时
    标签/标记本身漏进增量。
    """
    keep = 0
    for tok in _THINK_TOKENS:
        for n in range(len(tok) - 1, 0, -1):
            if raw.endswith(tok[:n]):
                keep = max(keep, n)
                break
    return raw[:len(raw) - keep] if keep else raw


def content_text_of(text) -> str:
    """纯文本版的 ``content_of``：同一套剥离规则（含「未闭合 ``<think>`` → 空串」的兜底）。"""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    out = _strip_think(text).strip()
    if _TAG_OPEN in out:                    # 思考段被截断（未闭合，且没有收尾标记）
        return ""
    return out


def content_of(ai_message) -> str:
    """只取 ``AIMessage.content``（推理段永不进台词），再剥离 think 段后 strip。"""
    return content_text_of(getattr(ai_message, "content", ""))


def chunk_text_of(chunk) -> str:
    """流式 chunk → **本块的回答原文**（不做剥离：跨块剥离统一交给 ``ThinkStream``）。

    纯字符串（鸭子类型替身直接吐文本块）原样返回；消息对象只取 ``content``——
    ``additional_kwargs.reasoning_content`` / ``reasoning`` / ``response_metadata`` 一概不读。
    空内容返回空串，由调用方跳过（不发事件）。
    """
    if isinstance(chunk, str):
        return chunk
    text = getattr(chunk, "content", "")
    if not text:
        return ""
    return text if isinstance(text, str) else str(text)


class ThinkStream:
    """跨块剥离推理段：逐块喂**回答原文**，返回这一次可以安全外发的增量。

    逐块 ``content_of`` 挡不住「标签与推理段被切到不同块」：推理正文所在块可能根本没有标签
    （原样返回），开标签自己也可能被切成 ``<thi`` / ``nk>``（连标签都进了增量）。这里用同一套
    规则（``_strip_think``）作用在**累加文本**上，并额外扣住两种「现在还不能当正文发」的尾巴：

    1. 尾部是某个标记的前半截（``<thi`` / ``<｜beg`` / ``<｜end`` …）：等下一块补齐再判（``_hold_partial_tag``）；
    2. 已经出现**未闭合**的 ``<think>`` 或只有开始标记：从那之后先不发，等收尾标记/闭合标签。

    **保证边界**（与 ``content_text_of`` 逐条对应，别再往大里说）：

    - **保证不漏**（delta 与最终台词都不含推理段，标记自身也不外发）：成对 ``<think>…</think>``、
      ``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`` 区间——含各自的标签/标记被切块的情形
      （开始标记一到就扣留）；
    - **只保证最终台词干净**（token 流里可能短暂出现推理原文，气泡随后由 ``message`` 整条替换）：
      网关吞掉开始标记、只剩 ``CoT…<｜end▁of▁thinking｜>``（标记到达前无法判定它是推理），
      以及未闭合的 ``<think>``（整条被 ``content_text_of`` 兜底成空串 ⇒ 兜底台词）。

    于是 ``"".join(feed(块))`` 在上面的**第一类**形态下是「可见正文」的真前缀；第二类形态下只保证
    「发出的每一块都不是标记」且最终 ``message`` == ``content_text_of(整条原文)``。
    """

    def __init__(self):
        self._raw = ""
        self._out = ""

    @property
    def raw(self) -> str:
        """已吃进的原文（含推理段），定稿时交给 ``content_text_of``。"""
        return self._raw

    @property
    def visible(self) -> str:
        """到目前为止的可见正文（第一类形态下就是已发增量拼起来的它）。"""
        return self._out

    def feed(self, text) -> str:
        """吃进一块原文，返回本块新增的可见增量（可能为空串 = 这一块全是推理段/标记）。"""
        if not text:
            return ""
        self._raw += str(text)
        vis = self._visible_now()
        if vis == self._out or not vis.startswith(self._out):
            # 没长（推理段 / 标记 / 滞留尾巴）：不发。**降级形态**下可见文本会「缩」（已发出去的是
            # 事后才判定的推理原文）：这里只停发、不往回补——前面那块收不回来，但标记永不外发，
            # 且最终 message 与 TTS 恒为干净台词（气泡由 message 整条收口）。
            return ""
        delta = vis[len(self._out):]
        self._out = vis
        return delta

    def _visible_now(self) -> str:
        out = _strip_think(_hold_partial_tag(self._raw))
        i = out.find(_TAG_OPEN)             # 未闭合的开标签：它之后的正文一律先不发
        if i >= 0:
            out = out[:i]
        return out
