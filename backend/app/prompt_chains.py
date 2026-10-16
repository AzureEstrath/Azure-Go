"""七阶段提示链（``ChatPromptTemplate`` + ``RunnableBranch``）。

把「系统消息 + 历史 + 最后一条 user 消息」的拼接收拢到一处；文本仍全部来自 ``prompts.py``：

- 现状路径：``context``（``prompts.with_context(...)`` 的完整产出）原样作为最后一条 user 消息，
  系统消息与旧 ``AzureAgent._sys_content()`` 逐字符一致（由 ``tests/test_prompt_chains.py`` 锁定）；
- 回退路径（没有 ``context``）：裸 ``prompt`` 作为 user 消息，并把该阶段的 ``prompts.phase_note``
  补进系统消息——回退路径没有 context 块带出阶段提示，供计划 04 的图节点复用；
- ``phase_chain(phase)``：某阶段的 LCEL 链，便于 tracing 与替换。

本模块只拼提示词：台词清洗（推理段剥离、舞台提示）仍全在 ``llm_chain.content_of`` 与
``AzureAgent.clean_reply``，不在这里。
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableBranch

from . import prompts

PHASES = ("observe", "after_player", "own_turn", "just_moved", "analyze", "chat", "summary")

# 思考风格段的标签（与旧 _sys_content 逐字符一致）
_THINK_LABEL = "\n\n【思考风格 · 随心情微调】"

# 系统消息：人设 +（回退路径的）阶段提示 + 本次长度约束
_SYSTEM_TMPL = "{persona}{phase_note}{len_note}"

# 现状路径模板：阶段提示已由 context 里的 role_brief 带出，这里必须留空
_WITH_CONTEXT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM_TMPL),
    MessagesPlaceholder("history", optional=True),
    ("user", "{context}"),
]).partial(phase_note="")

# 消息类型 → OpenAI 角色名（history 里的旧消息要原样还原成 role/content）
_ROLE_OF = {"system": "system", "human": "user", "ai": "assistant"}


def _persona_block(mood_name, think_hint) -> str:
    """人设 + 心情 +（思考档开启时）思考风格：与旧 ``_sys_content()`` 的前半段逐字符一致。

    系统提示词同样要过玩家称呼替换（``prompts._t``，旧实现也在这里调它）。
    """
    s = prompts._t(prompts.system_prompt()) + prompts.mood_note(mood_name)
    if think_hint != "":
        s += _THINK_LABEL + think_hint
    return s


def _fallback_template(phase) -> ChatPromptTemplate:
    """回退路径模板：裸 ``prompt`` 作 user 消息，阶段提示补进系统消息。"""
    return ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_TMPL),
        MessagesPlaceholder("history", optional=True),
        ("user", "{prompt}"),
    ]).partial(phase_note="\n\n" + prompts.phase_note(phase))


def _is_fallback(x) -> bool:
    """回退判据：没有 ``context`` 块、但有要回答的 ``prompt``。"""
    return str(x.get("context", "")) == "" and str(x.get("prompt", "")) != ""


def phase_chain(phase):
    """某阶段的链：输入模板变量 → 提示词（``PromptValue``，可直接 ``|`` 接模型）。

    ``build_messages()`` 在它后面接 ``to_messages()``，产出与旧拼接顺序一致（system → history → user）。
    """
    return RunnableBranch((_is_fallback, _fallback_template(phase)), _WITH_CONTEXT)


def build_messages(phase, *, context="", prompt="", len_note="", mood_name="", think_hint="",
                   history=None) -> list[dict]:
    """按阶段拼消息，与旧 ``_sys_msg() + _user_msg(with_context(...))`` 逐字符一致。

    逐字符一致是硬要求：模型要看到的字必须与本次重构前完全相同，一个标点都不能变（测试锁定）。

    - ``context``：``prompts.with_context(...)`` 的完整产出（已含【现在】与 prompt）——非空时原样
      作为最后一条 user 消息；**今天的调用点一律走这条路**（见 ``AzureAgent._phase_messages``）；
    - ``prompt``：没有 ``context`` 时的回退——作为 user 消息原样发出，并把该阶段的
      ``prompts.phase_note(phase)`` 补进系统消息；这是**计划 04 的图节点入口，今天没有调用点用它**
      （``len_note``/``mood_name``/``think_hint`` 两条路径共用）；
    - ``history``：夹在系统消息与最后一条 user 消息之间的历史消息（原样透传）。
    """
    msgs = phase_chain(phase).invoke({
        "persona": _persona_block(mood_name, think_hint),
        "len_note": len_note,
        "history": list(history or []),
        "context": context,
        "prompt": prompt,
    }).to_messages()          # 与 ChatPromptTemplate.format_messages 的产出等价
    return [{"role": _ROLE_OF.get(str(m.type), str(m.type)), "content": m.content} for m in msgs]
