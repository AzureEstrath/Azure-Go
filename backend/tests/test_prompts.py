"""AzurePrompts 逐字移植回归测试：逐条比对 Godot 导出的黄金样本。

运行：在 d:\\KataGo\\azure-server 下执行
    python -m unittest discover -s tests -v

约定：全部提示词均为纯文本，逐字字符串全等（assertEqual）；
仅 to_black_perspective 返回浮点数用 1e-9 容差比较。
"""

import json
import os
import unittest

from app import prompts

_GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "prompts.json")


def _load_golden():
    with open(_GOLDEN, "r", encoding="utf-8") as f:
        return json.load(f)


# ===================== 黄金样本输入常量 =====================

MOVES_EMPTY = []
MOVES_SHORT = [["B", "Q16"], ["W", "D4"], ["B", "Q4"], ["W", "D16"]]
MOVES_LONG = [
    ["B", "Q16"], ["W", "D4"], ["B", "Q4"], ["W", "D16"],
    ["B", "K10"], ["W", "K9"], ["B", "C3"], ["W", "R16"],
    ["B", "C16"], ["W", "R4"], ["B", "F3"], ["W", "N16"],
    ["B", "D10"], ["W", "Q10"], ["B", "K3"], ["W", "K16"],
    ["B", "C10"], ["W", "R10"], ["B", "M4"], ["W", "L16"],
    ["B", "E9"], ["W", "O10"], ["B", "H6"], ["W", "J13"],
    ["B", "F13"], ["W", "N6"], ["B", "C6"], ["W", "R13"],
    ["B", "E13"], ["W", "M13"],
]

CHAT_EMPTY = []
CHAT_SAMPLE = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好呀~"},
    {"role": "user", "content": "这盘棋你觉得怎么样？"},
    {"role": "assistant", "content": "我觉得你布局下得不错。"},
    {"role": "user", "content": "那我该下哪里"},
    {"role": "assistant", "content": "可以先守角，别急着打进去。"},
    {"role": "user", "content": "好的"},
    {"role": "assistant", "content": "嗯，加油"},
    {"role": "user", "content": "谢谢你"},
    {"role": "assistant", "content": "不客气，随时问我。"},
]
CHAT_LONG = CHAT_SAMPLE + [
    {"role": "user", "content": "这是一条很长的消息，用来测试超过六十四字时是否会被截断并且加上省略号，后面的内容应当全部被丢弃。"},
]

MEMORY_EMPTY = []
MEMORY_SOME = [
    {"tag": "E", "move_no": 1, "text": "黑棋占角，方向不错"},
    {"tag": "A", "move_no": 2, "text": "我选择挂角回应，先取外势"},
]

SEQ_EMPTY = []
SEQ_SHORT = ["Q16", "D4", "Q4", "D16"]
SEQ_LONG = ["Q4", "D16", "K10", "K9", "C3", "R16"]

# 定式 name/key 取自 JosekiData.LIST[0] / LIST[3]
JOSEKI_BASIC_NAME = "点三三·基本型"
JOSEKI_BASIC_KEY = "对方点三三就还他角地：挡在这边守住外侧，之后可以先手转向别处——现在最常见的开局就是它。"
JOSEKI_KOMPA_NAME = "小飞挂·靠压定式"
JOSEKI_KOMPA_KEY = "对方小飞挂角时靠上去再长：把他压低、自己走厚，是很稳的应对。"

ANALYSIS_FULL = {"rootInfo": {"winrate": 0.62, "scoreLead": 3.5}, "ownership": [[0] * 19 for _ in range(19)]}
ANALYSIS_NO_OWNERSHIP = {"rootInfo": {"winrate": 0.62, "scoreLead": 3.5}}
ANALYSIS_EMPTY = {}


def _moves(tok):
    return {"empty": MOVES_EMPTY, "short": MOVES_SHORT, "long": MOVES_LONG}[tok]


def _chat(tok):
    return {"empty": CHAT_EMPTY, "sample": CHAT_SAMPLE, "long": CHAT_LONG}[tok]


def _memory(tok):
    return {"empty": MEMORY_EMPTY, "some": MEMORY_SOME}[tok]


def _seq(tok):
    return {"empty": SEQ_EMPTY, "short": SEQ_SHORT, "long": SEQ_LONG}[tok]


def _analysis(tok):
    return {"full+ownership": ANALYSIS_FULL, "no-ownership": ANALYSIS_NO_OWNERSHIP, "empty": ANALYSIS_EMPTY}[tok]


def _board(tok):
    return {"empty": prompts.board_from_moves([]),
            "short": prompts.board_from_moves(MOVES_SHORT),
            "long": prompts.board_from_moves(MOVES_LONG),
            "given": prompts.board_from_moves(MOVES_SHORT)}[tok]


class _Cfg:
    """鸭子类型的最小配置对象（对应 AzureConfig 的 prompt_* 字段）。"""

    def __init__(self, user_name="", system="", notes=None):
        self.prompt_user_name = user_name
        self.prompt_system = system
        self.prompt_phase_notes = notes if notes is not None else {}


DEFAULT_CFG = _Cfg()


# ===================== 基类 =====================

class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load_golden()

    def setUp(self):
        prompts.configure(DEFAULT_CFG)

    def eq(self, actual, expected, tag=""):
        """字符串逐字全等；失败打印首个差异位置与上下文。"""
        if actual == expected:
            return
        i = 0
        n = min(len(actual), len(expected))
        while i < n and actual[i] == expected[i]:
            i += 1
        ctx = 24
        self.fail(
            "%s 字符串不一致（首个差异 @%d，期望长 %d 实际长 %d）\n"
            "  期望[%d:%d] = %r\n  实际[%d:%d] = %r"
            % (tag, i, len(expected), len(actual),
               max(0, i - ctx), i + ctx, expected[max(0, i - ctx):i + ctx],
               max(0, i - ctx), i + ctx, actual[max(0, i - ctx):i + ctx])
        )


class TestSmallHelpers(_Base):
    def test_t(self):
        for c in self.data["_t"]:
            self.eq(prompts._t(c["s"]), c["result"], "s=%r" % c["s"])

    def test_color_cn(self):
        for c in self.data["color_cn"]:
            self.eq(prompts.color_cn(c["c"]), c["result"], "c=%r" % c["c"])

    def test_system_prompt(self):
        self.eq(prompts.system_prompt(), self.data["system_prompt"], "system_prompt")

    def test_phase_note(self):
        for c in self.data["phase_note"]:
            self.eq(prompts.phase_note(c["phase"]), c["result"], "phase=%r" % c["phase"])

    def test_mood_note(self):
        for c in self.data["mood_note"]:
            self.eq(prompts.mood_note(c["name"]), c["result"], "name=%r" % c["name"])

    def test_mood_delta_prompt(self):
        for c in self.data["mood_delta_prompt"]:
            self.eq(prompts.mood_delta_prompt(c["cur"], c["region"], c["event"]),
                    c["result"], "event=%r" % c["event"])

    def test_stage_note(self):
        for c in self.data["stage_note"]:
            moves = [["B", "Q16"]] * c["n"]
            self.eq(prompts.stage_note(moves), c["result"], "n=%d" % c["n"])


class TestMoves(_Base):
    def test_game_narrative(self):
        for c in self.data["game_narrative"]:
            self.eq(prompts.game_narrative(_moves(c["moves"]), c["last_n"]),
                    c["result"], "moves=%s last_n=%d" % (c["moves"], c["last_n"]))

    def test_compact_move_list(self):
        for c in self.data["compact_move_list"]:
            self.eq(prompts.compact_move_list(_moves(c["moves"]), c["head"], c["tail"]),
                    c["result"], "moves=%s head=%d tail=%d" % (c["moves"], c["head"], c["tail"]))

    def test_board_summary(self):
        for c in self.data["board_summary"]:
            self.eq(prompts.board_summary(_seq(c["move_seq"]), c["last_n"]),
                    c["result"], "move_seq=%s last_n=%d" % (c["move_seq"], c["last_n"]))

    def test_board_from_moves(self):
        for c in self.data["board_from_moves"]:
            board = prompts.board_from_moves(_moves(c["moves"]))
            self.assertEqual(board.grid, c["result"]["grid"], "moves=%s grid 不一致" % c["moves"])

    def test_board_map(self):
        for c in self.data["board_map"]:
            self.eq(prompts.board_map(_board(c["board"]), c["num_moves"]),
                    c["result"], "board=%s num_moves=%d" % (c["board"], c["num_moves"]))


class TestChatMemory(_Base):
    def test_chat_windows(self):
        for c in self.data["chat_windows"]:
            self.assertEqual(prompts.chat_windows(_chat(c["chat"]), c["last_players"]), c["result"],
                             "chat=%s last_players=%d" % (c["chat"], c["last_players"]))

    def test_chat_brief(self):
        for c in self.data["chat_brief"]:
            self.eq(prompts.chat_brief(_chat(c["chat"]), c["last_players"], c["per_char"]),
                    c["result"], "chat=%s per_char=%d" % (c["chat"], c["per_char"]))

    def test_memory_brief(self):
        for c in self.data["memory_brief"]:
            self.eq(prompts.memory_brief(_memory(c["memory"]), c["last_n"]),
                    c["result"], "memory=%s" % c["memory"])


class TestContext(_Base):
    def _board_arg(self, tok):
        return None if tok == "null" else _board(tok)

    def test_context_block(self):
        for c in self.data["context_block"]:
            got = prompts.context_block(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                c["skip_last_chat"], c["katago"], self._board_arg(c["board"]))
            self.eq(got, c["result"], "board=%s chat=%s phase=%s skip=%s" % (c["board"], c["chat"], c["phase"], c["skip_last_chat"]))

    def test_with_context(self):
        for c in self.data["with_context"]:
            got = prompts.with_context(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["prompt"], c["phase"],
                c["skip_last_chat"], c["katago"], self._board_arg(c["board"]))
            self.eq(got, c["result"], "board=%s chat=%s phase=%s" % (c["board"], c["chat"], c["phase"]))

    def test_role_brief(self):
        for c in self.data["role_brief"]:
            self.eq(prompts.role_brief(_moves(c["moves"]), c["phase"]),
                    c["result"], "moves=%s phase=%s" % (c["moves"], c["phase"]))


class TestJoseki(_Base):
    def test_joseki_teach_prompt(self):
        for c in self.data["joseki_teach_prompt"]:
            key = JOSEKI_BASIC_KEY if c["name"] == JOSEKI_BASIC_NAME else JOSEKI_KOMPA_KEY
            self.eq(prompts.joseki_teach_prompt(c["name"], key, c["corner"]),
                    c["result"], "name=%s corner=%s" % (c["name"], c["corner"]))

    def test_joseki_short_prompt(self):
        for c in self.data["joseki_short_prompt"]:
            self.eq(prompts.joseki_short_prompt(c["name"], JOSEKI_BASIC_KEY, c["gtp"]),
                    c["result"], "gtp=%s" % c["gtp"])

    def test_joseki_hint_clause(self):
        for c in self.data["joseki_hint_clause"]:
            self.eq(prompts.joseki_hint_clause(c["name"], c["expect_gtp"]),
                    c["result"], "name=%s" % c["name"])

    def test_joseki_fallback(self):
        for c in self.data["joseki_fallback"]:
            self.eq(prompts.joseki_fallback(c["name"], JOSEKI_BASIC_KEY),
                    c["result"], "name=%s" % c["name"])


class TestToBlackPerspective(_Base):
    def test_to_black_perspective(self):
        for c in self.data["to_black_perspective"]:
            got = prompts.to_black_perspective(_analysis(c["analysis"]), c["num_moves"])
            exp = c["result"]
            tag = "analysis=%s num_moves=%d" % (c["analysis"], c["num_moves"])
            self.assertEqual(len(got), len(exp), "%s 长度不一致" % tag)
            for a, e in zip(got, exp):
                self.assertLessEqual(abs(a - e), 1e-9, "%s 期望=%r 实际=%r" % (tag, exp, got))


class TestConfigureOverride(_Base):
    def test_configure_override(self):
        g = self.data["configure_override"]
        prompts.configure(_Cfg("小明", "【自定义人设】你是测试角色。", {"observe": "【自定义】观察阶段。"}))
        try:
            self.eq(prompts.system_prompt(), g["system_prompt"], "configure system_prompt")
            self.eq(prompts.phase_note("observe"), g["phase_note_observe"], "configure phase_note observe")
            self.eq(prompts.phase_note("chat"), g["phase_note_chat"], "configure phase_note chat")
            self.eq(prompts.role_brief(MOVES_SHORT, "observe"), g["role_brief"], "configure role_brief")
            self.eq(prompts._t("Estarth 你好"), g["_t"], "configure _t")
        finally:
            prompts.configure(DEFAULT_CFG)   # 复原默认配置
        # 复原校验：默认态不含覆盖
        self.assertEqual(prompts.system_prompt(), self.data["system_prompt"], "复原后 system_prompt 应回到内置")


if __name__ == "__main__":
    unittest.main()
