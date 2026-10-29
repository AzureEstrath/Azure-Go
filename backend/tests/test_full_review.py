"""全盘 AI 复盘（v3.11.5）：分段规划 / 分段分析 / 强思考点评 / 行为标签 / 兜底。"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import actions, full_review, prompts
from app.agent_graph import _emit_speech
from app.events import RecordingBus
from helpers import ErrLLM, StubLLM

MOVES = [["black", "Q16"], ["white", "D4"], ["black", "D16"],
         ["white", "Q4"], ["black", "R14"], ["white", "F17"]]


class StepEngine:
    """假引擎：黑每落一手，黑胜率掉 5 个百分点；白落子不改变黑胜率。"""

    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    async def analyze(self, moves, max_visits=None):
        self.calls.append(len(moves))
        if self.fail_after is not None and len(moves) >= self.fail_after:
            return {"error": "引擎炸了"}
        black = sum(1 for m in moves if str(m[0]).lower() in ("b", "black"))
        target_black = 0.5 - 0.05 * black
        # rootInfo 是**行棋方视角**：黑方手数后轮到白，回 1 - 黑胜率
        side_wr = target_black if len(moves) % 2 == 0 else 1.0 - target_black
        return {"rootInfo": {"winrate": side_wr,
                             "scoreLead": 0.0,
                             "moveInfos": [], "ownership": [0.0] * 361}}


class FakeBusAgent:
    """够 `_emit_line` 用的小壳子：记录事件、不接 TTS。"""

    def __init__(self, engine, llm=None, tts=None):
        self.katago = engine
        self.llm = llm
        self.tts = tts
        self.player_name = "玩家"
        self.bus = RecordingBus()
        self.turn_id = ""
        self._speech_seq = 0

    def _emit(self, event):
        self.bus.publish(event)

    def _new_turn(self):
        self.turn_id = "t1"
        self._speech_seq = 0
        return self.turn_id

    def _next_speech_seq(self):
        self._speech_seq += 1
        return self._speech_seq

    def speech_pitch(self):
        return 0.0

    def _sys_content(self, len_note):
        return "你是Azure，一个围棋陪练。"

    def clean_reply(self, text):
        return text


class FakeTTS:
    def __init__(self):
        self.spoken = []

    async def speak(self, text):
        self.spoken.append(text)
        return {"mode": "system", "text": text}


class PlanTest(unittest.TestCase):
    def test_plan_covers_all_and_respects_bounds(self):
        self.assertEqual(full_review.plan_segments(0), [])
        self.assertEqual(full_review.plan_segments(5), [[0, 5]])
        self.assertEqual(full_review.plan_segments(24), [[0, 12], [12, 24]])
        for n in (1, 12, 13, 50, 100, 200, 400):
            segs = full_review.plan_segments(n)
            self.assertEqual(segs[0][0], 0)
            self.assertEqual(segs[-1][1], n)
            for a, b in zip(segs, segs[1:]):
                self.assertEqual(a[1], b[0])          # 无缝、无重叠
            for start, end in segs:
                self.assertGreater(end, start)
                self.assertLessEqual(end - start, full_review.MAX_SPAN)
            self.assertLessEqual(len(segs), max(full_review.MAX_SEGMENTS,
                                                -(-n // full_review.MAX_SPAN)))


class AnalyzeSegmentTest(unittest.TestCase):
    def test_rows_and_key_moves(self):
        eng = StepEngine()
        seg = asyncio.run(full_review.analyze_segment(eng, MOVES, 0, 4))
        self.assertTrue(seg["engine_ok"])
        self.assertEqual(len(seg["rows"]), 4)
        self.assertEqual([r["move_no"] for r in seg["key_moves"]], [1, 3])
        self.assertEqual(seg["rows"][0]["color"], "black")
        self.assertAlmostEqual(seg["rows"][0]["loss"], 0.05)
        self.assertAlmostEqual(seg["rows"][1]["loss"], 0.0)     # 白落子：黑胜率不变
        self.assertAlmostEqual(seg["wr_start"], 0.5)
        self.assertAlmostEqual(seg["wr_end"], 0.4)
        self.assertEqual(eng.calls, [0, 1, 2, 3, 4])            # 段首..段末各算一次

    def test_engine_failure_degrades(self):
        seg = asyncio.run(full_review.analyze_segment(StepEngine(fail_after=0), MOVES, 0, 4))
        self.assertFalse(seg["engine_ok"])
        self.assertEqual(seg["rows"], [])
        self.assertIn("引擎炸了", seg["error"])


class CommentSegmentTest(unittest.TestCase):
    def test_strong_thinking_and_action_tags(self):
        eng = StepEngine()
        llm = StubLLM("我{笑}觉得这一段你走得很稳。{摸头}")
        agent = FakeBusAgent(eng, llm)
        out = asyncio.run(full_review.comment_segment(
            agent, {"moves": MOVES, "meta": {"notes": []}}, 0, 4, index=0, total=2))
        self.assertEqual(out["text"], "我{笑}觉得这一段你走得很稳。")   # 未知标签被清掉
        self.assertEqual(llm.think_modes, [2])                      # 强制强思考
        msgs = llm.calls[0][0]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("行为标签", msgs[1]["content"])
        messages = agent.bus.of_type("message")
        self.assertEqual(len(messages), 1)
        self.assertIn("{笑}", messages[0]["text"])

    def test_llm_failure_falls_back(self):
        agent = FakeBusAgent(StepEngine(), ErrLLM())
        out = asyncio.run(full_review.comment_segment(
            agent, {"moves": MOVES}, 0, 4, index=0, total=2))
        self.assertTrue(out["text"])
        self.assertNotIn("调用出错", out["text"])
        self.assertEqual(len(agent.bus.of_type("message")), 1)

    def test_records_notes_are_in_prompt(self):
        agent = FakeBusAgent(StepEngine(), StubLLM("这一段还行。"))
        rec = {"moves": MOVES, "meta": {"notes": [
            {"move_no": 2, "kind": "review", "side": "E", "text": "这手有点急"}]}}
        asyncio.run(full_review.comment_segment(agent, rec, 0, 4, index=0, total=2))
        prompt = agent.llm.calls[0][0][1]["content"]
        self.assertIn("这手有点急", prompt)


class ActionTagTest(unittest.TestCase):
    def test_strip_and_clean(self):
        self.assertEqual(actions.strip_actions("你{笑}好{摸头}"), "你好")
        self.assertEqual(actions.clean_actions("你{笑}好{摸头}{得意}"), "你{笑}好{得意}")
        self.assertEqual(actions.clean_actions("你{笑}{得意}{脸红}", limit=1), "你{笑}")
        self.assertEqual(actions.clean_actions("没有标签"), "没有标签")

    def test_tts_never_speaks_tags(self):
        tts = FakeTTS()
        agent = FakeBusAgent(StepEngine(), StubLLM(), tts)
        agent._new_turn()
        asyncio.run(_emit_speech(agent, "我{笑}觉得这手不错"))
        self.assertEqual(tts.spoken, ["我觉得这手不错"])
        speech = agent.bus.of_type("speech")
        self.assertEqual(len(speech), 1)
        self.assertEqual(speech[0]["text"], "我觉得这手不错")


if __name__ == "__main__":
    unittest.main()
