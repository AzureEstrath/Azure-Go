"""阶段链：系统提示与用户消息的拼接必须与改造前逐字符一致。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prompt_chains, prompts
from app.agent import LEN_NOTE, LEN_TEACH_NOTE
from helpers import mk_agent


def _configure_prompts():
    class C:
        prompt_user_name = "Estarth"
        prompt_system = ""
        prompt_phase_notes = {}
    prompts.configure(C())


def _legacy_messages(len_note, mood_name, think_hint, context):
    """改造前 agent 的拼法，逐字符基准（``_sys_content`` 原样抄自 HEAD 的 agent.py）::

        s = prompts._t(prompts.system_prompt()) + prompts.mood_note(self.mood.region_name())
        if self.llm is not None and getattr(self.llm, "think_mode", 0) > 0:
            s += "\\n\\n【思考风格 · 随心情微调】" + self.mood.think_hint()
        return s + len_note

    调用契约同 ``AzureAgent._think_hint()``：思考档关闭时 ``think_hint`` 传空串。
    """
    s = prompts._t(prompts.system_prompt()) + prompts.mood_note(mood_name)
    if think_hint != "":
        s += "\n\n【思考风格 · 随心情微调】" + think_hint
    return [{"role": "system", "content": s + len_note},
            {"role": "user", "content": context}]


class PhaseMessagesTest(unittest.TestCase):
    def setUp(self):
        _configure_prompts()

    def test_phases_covered(self):
        self.assertEqual(set(prompt_chains.PHASES),
                         {"observe", "after_player", "own_turn", "just_moved", "analyze", "chat", "summary"})

    def test_system_message_prefix(self):
        msgs = prompt_chains.build_messages("observe", mood_name="平静", prompt="P")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertTrue(msgs[0]["content"].startswith(prompts.system_prompt()))
        self.assertIn(prompts.mood_note("平静"), msgs[0]["content"])

    def test_user_message_is_context_plus_prompt(self):
        moves = [["B", "Q16"], ["W", "D4"]]
        ctx = prompts.with_context(moves, [], [], "P", "observe", False, "", None)
        msgs = prompt_chains.build_messages("observe", context=ctx, prompt="P")
        self.assertEqual(msgs[-1], {"role": "user", "content": ctx})

    def test_history_inserted_between(self):
        hist = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的"}]
        msgs = prompt_chains.build_messages("chat", context="C", prompt="P", history=hist)
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])

    def test_len_note_appended(self):
        msgs = prompt_chains.build_messages("after_player", prompt="P", len_note="【短】")
        self.assertTrue(msgs[0]["content"].endswith("【短】"))


class ByteIdentityTest(unittest.TestCase):
    """现状路径（context 非空）必须与旧 ``_sys_msg() + _user_msg(with_context(...))`` 逐字符一致。"""

    def setUp(self):
        _configure_prompts()

    def _ctx(self, phase):
        moves = [["B", "Q16"], ["W", "D4"], ["B", "Q4"], ["W", "C3"]]
        memory = [{"tag": "E", "move_no": 1, "text": "开局还算正"}, {"tag": "A", "move_no": 2, "text": "我这样应"}]
        chat = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的"}]
        return prompts.with_context(moves, memory, chat, "第%s阶段的话" % phase, phase, False, "黑方胜率 55.0%", None)

    def test_all_phases_and_variants_match_legacy(self):
        for phase in prompt_chains.PHASES:
            ctx = self._ctx(phase)
            for len_note in ("", LEN_NOTE, LEN_TEACH_NOTE):
                for mood_name in ("", "平静", "开心"):
                    for think_hint in ("", "此刻心境平缓：思考放慢半拍，多确认一步再开口。"):
                        msgs = prompt_chains.build_messages(
                            phase, context=ctx, prompt="第%s阶段的话" % phase,
                            len_note=len_note, mood_name=mood_name, think_hint=think_hint)
                        self.assertEqual(msgs, _legacy_messages(len_note, mood_name, think_hint, ctx),
                                         "阶段 %s 的拼接与旧实现不一致" % phase)

    def test_history_matches_legacy_order(self):
        """历史消息夹在系统消息与最后一条 user 消息之间，内容原样透传。"""
        ctx = self._ctx("chat")
        hist = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的"}]
        msgs = prompt_chains.build_messages("chat", context=ctx, history=hist)
        legacy = _legacy_messages("", "", "", ctx)
        self.assertEqual(msgs, [legacy[0], *hist, legacy[1]])

    def test_custom_user_name_still_replaced(self):
        """系统提示词同样要过玩家称呼替换（旧 ``_sys_content`` 里是 ``prompts._t``）。"""
        class C:
            prompt_user_name = "小明"
            prompt_system = ""
            prompt_phase_notes = {}
        prompts.configure(C())
        ctx = prompts.with_context([], [], [], "P", "observe", False, "", None)
        msgs = prompt_chains.build_messages("observe", context=ctx)
        self.assertNotIn("Estarth", msgs[0]["content"])
        self.assertIn("小明", msgs[0]["content"])
        self.assertEqual(msgs, _legacy_messages("", "", "", ctx))


class FallbackTest(unittest.TestCase):
    """回退路径（无 context）：裸 prompt 作 user 消息，阶段提示补进系统消息。"""

    def setUp(self):
        _configure_prompts()

    def test_prompt_is_used_verbatim(self):
        msgs = prompt_chains.build_messages("own_turn", prompt="轮到你下了，说一句。")
        self.assertEqual(msgs[-1], {"role": "user", "content": "轮到你下了，说一句。"})

    def test_phase_note_added_to_system(self):
        msgs = prompt_chains.build_messages("own_turn", prompt="P")
        self.assertIn(prompts.phase_note("own_turn"), msgs[0]["content"])

    def test_no_prompt_keeps_system_plain(self):
        """``_sys_msg()`` 的用法：既没有 context 也没有 prompt 时，系统消息不带阶段提示。"""
        msgs = prompt_chains.build_messages("observe", mood_name="平静")
        self.assertEqual(msgs[0], _legacy_messages("", "平静", "", "")[0])

    def test_phase_chain_is_runnable(self):
        from langchain_core.runnables import Runnable
        for phase in prompt_chains.PHASES:
            self.assertIsInstance(prompt_chains.phase_chain(phase), Runnable)


class AgentSystemMessageTest(unittest.TestCase):
    """agent 的两个系统消息构造点：经链构造，且与旧 ``_sys_content`` 逐字符一致。"""

    def setUp(self):
        _configure_prompts()

    def test_sys_msg_matches_legacy(self):
        agent, _eng, _llm, _bus = mk_agent()
        self.assertEqual(agent._sys_msg(),
                         _legacy_messages("", agent.mood.region_name(), "", "")[0])

    def test_limit_len_uses_len_note(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.limit_len = True
        self.assertTrue(agent._sys_msg()["content"].endswith(LEN_NOTE))
        self.assertTrue(agent._sys_teach_msg()["content"].endswith(LEN_TEACH_NOTE))

    def test_think_hint_only_when_thinking(self):
        agent, _eng, llm, _bus = mk_agent()
        plain = agent._sys_msg()["content"]
        self.assertNotIn("【思考风格", plain)
        llm.think_mode = 2
        thinking = agent._sys_msg()["content"]
        self.assertEqual(thinking, plain + "\n\n【思考风格 · 随心情微调】" + agent.mood.think_hint())

    def test_agent_call_keeps_context_verbatim(self):
        """她实际发出的两条消息：系统消息经链构造，用户消息仍是 ``with_context`` 的原文。"""
        async def run():
            agent, _eng, llm, _bus = mk_agent()
            await agent.hint()
            return agent, llm

        agent, llm = asyncio.run(run())
        msgs = llm.calls[0][0]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0], agent._sys_msg())
        head = prompts.context_block(agent.st.moves, agent.st.memory_log, agent.st.chat_history,
                                     "observe", False, agent.st.last_katago, agent.st.board)
        self.assertTrue(msgs[1]["content"].startswith(head + "\n\n【现在】\n"))


if __name__ == "__main__":
    unittest.main()
