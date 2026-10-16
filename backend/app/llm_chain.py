"""LangChain 模型构造与扩展字段（``ChatOpenAI`` 的唯一装配点）。

职责：
- ``build_chat_model``：把 ``AzureConfig`` 的云端 LLM 字段映射成 ``ChatOpenAI``；
- ``merge_extra_body``：``llm_extra_body`` 打底，思考档位开启时并入 ``llm_think_extra_body``；
- ``content_of``：只取 ``AIMessage.content`` 并剥离 think 段——**推理段永不显示给玩家**
  （DeepSeek-R1 类模型的 ``reasoning_content`` 只留在消息对象里，绝不进台词）。

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
_THINK_RE = re.compile(re.escape(_TAG_OPEN) + r".*?" + re.escape(_TAG_CLOSE), re.S)

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


def content_of(ai_message) -> str:
    """只取 ``AIMessage.content``（推理段永不进台词），再剥离 think 段后 strip。"""
    text = getattr(ai_message, "content", "")
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    out = _THINK_RE.sub("", text).strip()
    if _TAG_OPEN in out:                    # 思考段被截断（未闭合）
        return ""
    return out
