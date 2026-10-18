"""AzurePrompts 逐字移植回归测试：逐条比对 Godot 导出的黄金样本。

运行：在 d:\\KataGo\\azure-server 下执行
    python -m unittest discover -s tests -v

约定：全部提示词均为纯文本，逐字字符串全等（assertEqual）；
仅 to_black_perspective 返回浮点数用 1e-9 容差比较。
"""

import asyncio
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import coords, prompts, time_ctx

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


# ===================== 计划 07 · Task 7：结构点开口（进入 / 分岔 / 收尾）=====================
#
# 三条新模板**只写素材 + 要求**（中文、一句话、用她自己的口吻、不要提胜率数值），不放可被照抄的
# 范文；它们**不写进 golden**（golden 只锁既有函数，见 `TestGoldenFrozen`）。


class TestJosekiActiveTemplates(_Base):
    """T7：`joseki_enter_prompt` / `joseki_branch_prompt` / `joseki_close_prompt` 的形状。"""

    NAME = JOSEKI_BASIC_NAME
    KEY = JOSEKI_BASIC_KEY
    CORNER = "左上"

    def _assert_common(self, out):
        """三条模板共有的要求句（U35）：一句话、用她自己的口吻、不要提胜率数值。"""
        self.assertIn("一句话", out)
        self.assertIn("你自己的口吻", out)
        self.assertIn("不要提胜率数值", out)
        for bad in ("%s", "%(", "（可选）", "例如"):
            self.assertNotIn(bad, out)

    def test_enter_prompt_is_material_plus_instruction(self):
        out = prompts.joseki_enter_prompt(self.NAME, self.KEY, self.CORNER)
        self._assert_common(out)
        # 素材：角 + 这一课的名字 + 要点
        self.assertIn(self.CORNER, out)
        self.assertIn(self.NAME, out)
        self.assertIn(self.KEY, out)
        # 换一份素材 ⇒ 换一份输出（不是写死的范文）
        other = prompts.joseki_enter_prompt(JOSEKI_KOMPA_NAME, JOSEKI_KOMPA_KEY, "右下")
        self.assertNotEqual(out, other)
        self.assertIn(JOSEKI_KOMPA_KEY, other)
        self.assertNotIn(self.KEY, other)

    def test_branch_prompt_gives_each_candidate_its_next_move(self):
        cands = [{"name": JOSEKI_BASIC_NAME, "family": "star_33", "next": (4, 3), "line": []},
                 {"name": JOSEKI_BASIC_NAME, "family": "star_33", "next": (5, 2), "line": []}]
        out = prompts.joseki_branch_prompt(self.NAME, self.KEY, cands)
        self._assert_common(out)
        self.assertIn(self.NAME, out)
        self.assertIn(self.KEY, out)
        # 候选**各自**的下一手坐标（0 基元组 → GTP）必须进素材，否则模型没法说清岔口
        self.assertIn(coords.xy_to_gtp(4, 3), out)
        self.assertIn(coords.xy_to_gtp(5, 2), out)

    def test_branch_prompt_without_candidates_has_no_stub_wording(self):
        out = prompts.joseki_branch_prompt(self.NAME, self.KEY, [])
        self._assert_common(out)
        self.assertIn(self.NAME, out)
        self.assertIn(self.KEY, out)
        self.assertNotIn("另有走法", out)             # 没候选就不留半截句式
        # …也不许反过来要求她说「别的走法」：没给她的东西，不能叫她说（U35：退回只说要点）
        self.assertNotIn("别的走法", out)
        self.assertNotIn("下一手在", out)

    def test_branch_prompt_drops_dirty_candidates_without_raising(self):
        """脏 `next`（缺值 / 非数字 / 出盘 / 不是元组）一律丢掉，**绝不抛**。

        这段跑在 `_prepare_after_player` 里：抛一次就是整条 `player_move` 丢台词
        （与 `agent.py` 的「缺键 / 脏值一律退化成 0，不抛」同一口径）。
        """
        dirty = [{"name": "点三三·定形", "next": (None, 3)},
                 {"name": "点三三·定形", "next": ("x", 3)},
                 {"name": "点三三·定形", "next": (99, 99)},      # 出盘（≥ 19）：不许变成假坐标
                 {"name": "点三三·定形", "next": (-1, 4)},
                 {"name": "点三三·定形", "next": "D17"},
                 {"name": "点三三·定形", "next": (4,)},
                 {"next": None},
                 "（连 dict 都不是）"]
        out = prompts.joseki_branch_prompt(self.NAME, self.KEY, dirty)
        self.assertIn(self.NAME, out)
        self.assertIn(self.KEY, out)
        self.assertNotIn("另有走法", out)             # 一个能用的都没有 ⇒ 只讲要点
        self.assertNotIn("下一手在", out)
        self.assertNotIn("别的走法", out)

        # 混一个合法的：只有它进文案；脏值不产生任何坐标
        good = {"name": "小飞挂·靠压定形", "next": (4, 2)}
        out2 = prompts.joseki_branch_prompt(self.NAME, self.KEY, dirty + [good])
        self.assertEqual(out2.count("下一手在"), 1)
        self.assertIn(coords.xy_to_gtp(4, 2), out2)
        self.assertIn("别的走法", out2)               # 有候选才要求她说别的走法
        self.assertNotIn("。。", out2)                # 要点本身以句号收尾时不许拼出「。。」

    def test_close_prompt_says_which_side_finished(self):
        player = prompts.joseki_close_prompt(self.NAME, self.KEY, self.CORNER, "player")
        azure = prompts.joseki_close_prompt(self.NAME, self.KEY, self.CORNER, "azure")
        self._assert_common(player)
        self._assert_common(azure)
        for out in (player, azure):
            self.assertIn(self.NAME, out)
            self.assertIn(self.KEY, out)
            self.assertIn(self.CORNER, out)
        # 谁走完的说清楚：玩家 / Azure 各一份，互不串味
        self.assertIn("Estarth", player)
        self.assertNotIn("Azure", player)
        self.assertIn("Azure", azure)
        self.assertNotIn("Estarth", azure)
        self.assertNotEqual(player, azure)


# ===================== 阶段三 · Task P5：提示词事件区块（v3.8.0）=====================
#
# 事件区块是**给模型看的素材**：插在长期记忆段之后、铁律段之前，空则逐字符不变；
# 事件原文**永不进台词**。四个门卫用例名逐字照用（task-P5-brief）。


def _ev(move_no, kind="chat", text="他和我聊过天", detail=None):
    return {"move_no": move_no, "ts": 0.0, "kind": kind, "text": text, "detail": detail or {}}


class TestEventBlock(_Base):
    """`prompts.event_block` 的形状 / 预算 / 插入点，以及「原文不进台词」。"""

    def test_empty_block_keeps_golden_identical(self):
        """空 / 全部超出衰减窗口 → 空串；`context_block` 输出与 **golden** 逐字符一致。"""
        self.assertEqual(prompts.event_block([], 10), "")
        self.assertEqual(prompts.event_block(None, 10), "")
        # 全部超出衰减窗口（> DECAY_DROP_AT = 12 手）→ 不进提示词
        self.assertEqual(prompts.event_block([_ev(1, text="很早的事")], 20), "")
        self.assertEqual(prompts.event_block([{"move_no": "脏", "kind": "chat"}], 0), "")
        self.assertEqual(prompts.event_block(["不是 dict"], 0), "")

        # 真的读 golden（`tests/golden/prompts.json` 的 context_block 样本）：事件为空时逐字符相等
        for c in self.data["context_block"]:
            board = None if c["board"] == "null" else _board(c["board"])
            self.eq(prompts.context_block(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                c["skip_last_chat"], c["katago"], board, events=[]),
                c["result"], "events=[] vs golden board=%s phase=%s" % (c["board"], c["phase"]))

        base = prompts.context_block(MOVES_SHORT, [], [], "observe")
        self.eq(prompts.context_block(MOVES_SHORT, [], [], "observe", events=[]), base,
                "events=[] 时 context_block 逐字符不变")
        self.eq(prompts.context_block(MOVES_SHORT, [], [], "observe", False, "", None, "", [], None), base,
                "events/now_move_no 缺省与显式空值都逐字符不变")
        # 空区块不产生任何表头
        self.assertNotIn("本局记事", base)

    def test_block_sits_before_ironclad(self):
        """只能插在**长期记忆段之后、铁律段之前**。"""
        ev = _ev(4, "ai_bad",
                 "我在第4手自主偏航：没走最优的Q16，选了D4（与最优的胜率差约3%）",
                 {"line": "b", "gtp": "D4", "best": "Q16", "drop": 0.03, "mood": "平静"})
        out = prompts.context_block(MOVES_SHORT, [], [], "observe",
                                    long_term="他喜欢点三三", events=[ev])
        self.assertIn("本局记事", out)
        self.assertIn("自主偏航", out)              # P2 留白的「自主偏航」标注已接上
        self.assertIn("走的是B线", out)             # 偏航选的线号（detail.line）也进区块
        self.assertIn("D4", out)
        self.assertIn("Q16", out)
        self.assertLess(out.index("长期记忆"), out.index("本局记事"))
        self.assertLess(out.index("本局记事"), out.index("铁律"))
        self.assertIn("A→T 从左到右", out)          # 铁律段正文没被动过

        # `with_context` 走同一条路（事件区块在【现在】之前）
        full = prompts.with_context(MOVES_SHORT, [], [], "继续下吧", "observe",
                                    long_term="他喜欢点三三", events=[ev])
        self.assertLess(full.index("本局记事"), full.index("【现在】"))

    def test_over_budget_drops_oldest_and_lowest(self):
        """超 `max_chars` → 丢【最旧 → 最低档】：最新的留住，最旧的先走。"""
        evs = [_ev(i, "player_good", "他在第%d手下的这步棋方向不错、形状也好、值得记一笔" % i)
               for i in range(1, 9)]
        out = prompts.event_block(evs, 12, max_chars=80)
        self.assertNotEqual(out, "")
        nums = [int(m) for m in re.findall(r"第(\d+)手", out)]
        self.assertIn(8, nums, "最新的一条必须留住")
        self.assertNotIn(1, nums, "最旧的一条先被丢")
        self.assertEqual(nums, sorted(nums, reverse=True), "渲染顺序：新 → 旧")
        self.assertEqual(len(nums) < len(evs), True, "确实发生了丢弃")
        # 丢掉的都是「更旧 ⇒ 档位更低」的那些：留住的手数全部大于被丢的手数
        self.assertGreater(min(nums), 1)

        # 预算连表头都放不下 → 空串（不留半截区块）
        self.assertEqual(prompts.event_block(evs, 12, max_chars=5), "")

    def test_injected_events_do_not_enter_reply(self):
        """事件原文只进提示词（user 消息），**永不进台词**、不产生 `speech`。"""
        from helpers import mk_agent

        async def run():
            agent, _eng, llm, bus = mk_agent()
            agent.event_log.add("chat", 0, "他和我聊了关于「定式」的话题", {"topic": "定式"})
            await agent.chat("这手定式不错")
            return llm, bus

        llm, bus = asyncio.run(run())
        prompt_text = " ".join(str(m.get("content", "")) for m in llm.calls[0][0])
        self.assertIn("本局记事", prompt_text)      # 素材进了提示词
        self.assertIn("定式", prompt_text)
        joined = " ".join(str(e.get("text", "")) for e in bus.messages())
        self.assertNotIn("本局记事", joined)        # 但没进任何台词
        self.assertNotIn("他和我聊了关于", joined)
        self.assertEqual([e for e in bus.of_type("speech")], [], "未接 TTS：一条 speech 都不发")


# ===================== 阶段四 · Task Q3：时间块 / 画像块（v3.9.0）=====================
#
# 两块都插在**事件区块之后、铁律段之前**（时间在上、画像在下），空则逐字符不变；
# 它们只喂给模型（user 消息），**永不进台词**。两个开关（`profile_enabled` /
# `time_aware_enabled`）由调用方把关（关着就传空 dict ⇒ 与「没有这个功能」逐字符同构）。


def _facts(**over):
    """一份「有全部事实」的时间事实（形状＝``time_ctx.facts()`` 的返回）。"""
    base = {"now": 1792414980.0, "date": "2026-10-19", "weekday": "周一", "clock": "21:03",
            "session_sec": 7380.0, "avg_move_sec": 42.0, "last_gap_days": 3.0, "streak_days": 4}
    base.update(over)
    return base


_PROFILE = {
    "open.star": {"value": 0.8, "confidence": 0.9, "updated_ts": 5},
    "style.aggression": {"value": 0.6, "confidence": 0.8, "updated_ts": 4},
    "persona.topic.定式": {"value": 0.5, "confidence": 0.7, "updated_ts": 1},
    "blunder.rate": {"value": 0.30, "confidence": 0.3, "updated_ts": 3},
    "weak.corner_tl": {"value": 0.40, "confidence": 0.25, "updated_ts": 3},
    "blunder.top_type": {"value": "无理", "confidence": 0.2, "updated_ts": 2},
    "persona.address_pref": {"value": "主人", "confidence": 1.0, "updated_ts": 9},
}


class TestProfileTimeBlocks(_Base):
    """`profile_note` / `time_note` 的空串、插入点、预算与「只在相关时提」。"""

    def test_empty_blocks_keep_golden_identical(self):
        """两块为空 ⇒ 空串，且 `context_block` 输出与 **golden**（三块都空的样本）逐字符一致。"""
        self.assertEqual(prompts.time_note({}), "")
        self.assertEqual(prompts.time_note(None), "")
        self.assertEqual(prompts.time_note("不是 dict"), "")
        self.assertEqual(prompts.profile_note({}, 40), "")
        self.assertEqual(prompts.profile_note(None, 40), "")
        self.assertEqual(prompts.profile_note("不是 dict", 40), "")

        # 新增的 golden 用例：同一份输入 + 两块都空 ⇒ 与既有 context_block 样本**逐字符相等**
        for c in self.data["context_block_profile_time_empty"]:
            board = None if c["board"] == "null" else _board(c["board"])
            base = prompts.context_block(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                c["skip_last_chat"], c["katago"], board)
            self.eq(base, c["result"], "三块都空 vs golden board=%s phase=%s" % (c["board"], c["phase"]))
            for kwargs in ({"profile": {}, "time_facts": {}}, {"profile": None, "time_facts": None}):
                got = prompts.context_block(
                    _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                    c["skip_last_chat"], c["katago"], board, **kwargs)
                self.eq(got, c["result"],
                        "%r 时 context_block 逐字符不变 board=%s" % (sorted(kwargs), c["board"]))
            self.assertNotIn("时间事实", base)
            self.assertNotIn("她眼中的你", base)

        # 既有 golden 条目（不带这两个参数）仍逐字符一致
        for c in self.data["context_block"]:
            board = None if c["board"] == "null" else _board(c["board"])
            self.eq(prompts.context_block(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                c["skip_last_chat"], c["katago"], board),
                c["result"], "既有 golden board=%s phase=%s" % (c["board"], c["phase"]))

    def test_blocks_sit_between_events_and_ironclad(self):
        """插入顺序固定：长期记忆 → 事件区块 → **时间块** → **画像块** → 铁律段。"""
        ev = _ev(4, "player_good", "他在第4手下的这步棋方向不错")
        out = prompts.context_block(MOVES_SHORT, [], [], "observe", long_term="他喜欢点三三",
                                    events=[ev], profile=_PROFILE, time_facts=_facts())
        self.assertLess(out.index("长期记忆"), out.index("本局记事"))
        self.assertLess(out.index("本局记事"), out.index("时间事实"))
        self.assertLess(out.index("时间事实"), out.index("她眼中的你"))
        self.assertLess(out.index("她眼中的你"), out.index("铁律"))
        self.assertIn("A→T 从左到右", out)          # 铁律段正文没被动过
        # 两块真的进了提示词
        self.assertIn("现在是 2026-10-19 周一 21:03", out)
        self.assertIn("开局爱走「星位」", out)

        # `with_context` 走同一条路（两块都在【现在】之前）
        full = prompts.with_context(MOVES_SHORT, [], [], "继续下吧", "observe",
                                    profile=_PROFILE, time_facts=_facts())
        self.assertLess(full.index("她眼中的你"), full.index("【现在】"))

    def test_time_note_drops_the_oldest_fact_first(self):
        """超预算：**从最旧的事实开始丢**（连续天数 → 间隔 → 单手用时 → 本局时长 → 当前时间）。"""
        full = prompts.time_note(_facts())
        self.assertIn("现在是 2026-10-19 周一 21:03", full)
        self.assertIn("本局已下 2 小时 3 分", full)
        self.assertIn("他平均每手 42 秒", full)
        self.assertIn("距上次对局 3 天", full)
        self.assertIn("连续第 4 天在陪 Estarth", full)

        # 只差「连续天数」那一行放不下 ⇒ 丢它，其余全留
        out = prompts.time_note(_facts(), max_chars=len(full) - len("连续第 4 天在陪 Estarth") - 1)
        self.assertEqual(out, full.replace("\n连续第 4 天在陪 Estarth", ""))
        # 再收紧一行 ⇒ 「距上次对局」也先走，当前时间留住
        out2 = prompts.time_note(_facts(), max_chars=len(out) - len("距上次对局 3 天") - 1)
        self.assertNotIn("距上次对局", out2)
        self.assertIn("现在是", out2)
        self.assertIn("本局已下", out2)
        # 预算连表头 + 说明句都放不下 ⇒ 空串（不留半截区块）
        self.assertEqual(prompts.time_note(_facts(), max_chars=5), "")

    def test_profile_note_drops_the_lowest_confidence_first(self):
        """超预算：丢**最低置信 / 最旧维度**（渲染顺序＝置信度高 → 低）。"""
        full = prompts.profile_note(_PROFILE, 40)
        # 渲染顺序：置信度高 → 低
        self.assertLess(full.index("开局爱走「星位」"), full.index("下得敢接触"))
        self.assertLess(full.index("下得敢接触"), full.index("常聊的话题"))
        self.assertLess(full.index("常聊的话题"), full.index("失误偏多"))
        self.assertLess(full.index("失误偏多"), full.index("「左上角」"))
        self.assertLess(full.index("「左上角」"), full.index("亏得最多的那一手"))

        # 只放得下最高置信的那一条 ⇒ 最低置信的（blunder.top_type）先走
        one = "开局爱走「星位」"
        out = prompts.profile_note(_PROFILE, 40, max_chars=len(prompts._PROFILE_HEADER)
                                   + len(prompts._PROFILE_TAIL) + len(one))
        self.assertEqual(out, prompts._PROFILE_HEADER + one + prompts._PROFILE_TAIL)
        self.assertNotIn("亏得最多的那一手", out)
        # 连表头 + 说明句都放不下 ⇒ 空串
        self.assertEqual(prompts.profile_note(_PROFILE, 40, max_chars=5), "")

    def test_profile_note_early_game_hides_the_blunder_group(self):
        """「失误与薄弱方位」只在这一局下够 ``PROFILE_BLUNDER_MIN_MOVE`` 手之后才提（只在相关时提）。"""
        early = prompts.profile_note(_PROFILE, prompts.PROFILE_BLUNDER_MIN_MOVE - 1)
        self.assertNotIn("失误", early)
        self.assertNotIn("亏得最多的那一手", early)
        self.assertNotIn("吃亏", early)
        self.assertIn("开局爱走「星位」", early)
        late = prompts.profile_note(_PROFILE, prompts.PROFILE_BLUNDER_MIN_MOVE)
        self.assertIn("失误偏多", late)
        self.assertIn("亏得最多的那一手", late)
        self.assertIn("「左上角」", late)
        # 手数脏值 ⇒ 当 0（不提这一组），不抛
        self.assertNotIn("失误", prompts.profile_note(_PROFILE, "脏"))

    def test_profile_note_ignores_unknown_keys_and_never_renders_address_pref(self):
        """未知键忽略；``persona.address_pref`` **不留措辞位**（真实链路永不产出）。"""
        self.assertEqual(prompts.profile_note({"not.an.axis": 0.9}, 40), "")
        self.assertEqual(prompts.profile_note({"persona.address_pref": {"value": "主人"}}, 40), "")
        self.assertNotIn("主人", prompts.profile_note(_PROFILE, 40))
        # 扁平形状（只给值）也收；脏值不抛
        flat = prompts.profile_note({"open.komoku": 0.5, "style.aggression": "脏"}, 40)
        self.assertIn("开局爱走「小目」", flat)
        self.assertNotIn("敢接触", flat)

    def test_profile_note_uses_the_current_scales(self):
        """Q2 收口后的刻度：``blunder.rate`` 是玩家口径（翻倍）、``territory_vs_influence`` ≈ [0, 0.5]。"""
        def note(axis, value):
            return prompts.profile_note({axis: value}, 40)

        self.assertIn("失误偏多", note("blunder.rate", 0.25))
        self.assertIn("偶尔会有明显亏损", note("blunder.rate", 0.15))
        self.assertIn("很少下出明显亏损", note("blunder.rate", 0.05))
        self.assertEqual(note("blunder.rate", 0.09), "", "中间地带不开口（只在相关时提）")

        self.assertIn("更偏势力与模样", note("style.territory_vs_influence", 0.5))
        self.assertIn("更偏实地", note("style.territory_vs_influence", 0.1))
        self.assertEqual(note("style.territory_vs_influence", 0.3), "")

        self.assertIn("敢接触", note("style.aggression", 0.5))
        self.assertIn("下得偏稳", note("style.aggression", 0.1))
        self.assertIn("常脱先", note("style.tenuki", 0.4))
        self.assertIn("很少脱先", note("style.tenuki", 0.05))
        self.assertIn("挂角比守角更主动", note("open.favorite_kakari", 0.7))
        self.assertIn("更爱先守角", note("open.favorite_kakari", 0.3))
        self.assertIn("孤军深入", note("blunder.top_type", "深入"))
        self.assertIn("硬碰硬", note("blunder.top_type", "无理"))

    def test_time_note_reads_only_the_keys_that_are_there(self):
        """缺哪个事实就不说哪个（不补 0）；空壳 / 只有 ``now`` ⇒ 空串。"""
        self.assertEqual(prompts.time_note({"now": 1.0}), "")
        only_now = prompts.time_note({"date": "2026-10-19", "weekday": "周一", "clock": "21:03"})
        self.assertIn("现在是 2026-10-19 周一 21:03", only_now)
        for gone in ("本局", "平均每手", "距上次对局", "连续第"):
            self.assertNotIn(gone, only_now)
        # 直接吃 `time_ctx.facts()` 的产出（两端字段名必须对得上）
        facts = time_ctx.facts(now=1792414980.0, session_start=1792414980.0 - 7380.0,
                               last_game_ts=1792414980.0 - 3 * 86400.0,
                               move_ts_list=[1792414980.0 - 84.0, 1792414980.0],
                               game_ts_list=[1792414980.0])
        out = prompts.time_note(facts)
        self.assertIn("本局已下 2 小时 3 分", out)
        self.assertIn("他平均每手 1 分 24 秒", out)
        self.assertIn("距上次对局 3 天", out)
        self.assertIn("连续第 1 天在陪 Estarth", out)

    def test_blocks_carry_the_instruction_and_no_copyable_example(self):
        """两块都必须带说明句（只在相关时提 / 别把数字当口头禅），且不放可照抄的示例句。"""
        t = prompts.time_note(_facts())
        p = prompts.profile_note(_PROFILE, 40)
        self.assertIn("不必每句都说", t)
        self.assertIn("口头禅", t)
        self.assertIn("只在相关时", p)
        self.assertIn("口头禅", p)
        for out in (t, p):
            for bad in ("例如", "%s", "%(", "（可选）"):
                self.assertNotIn(bad, out)


class TestGoldenFrozen(_Base):
    """T7 门卫：本任务只**新增**三条模板，golden 锁定的既有输出一字未改。"""

    def test_context_block_golden_unchanged(self):
        for c in self.data["context_block"]:
            board = None if c["board"] == "null" else _board(c["board"])
            self.eq(prompts.context_block(
                _moves(c["moves"]), _memory(c["memory"]), _chat(c["chat"]), c["phase"],
                c["skip_last_chat"], c["katago"], board),
                c["result"], "context_block board=%s phase=%s" % (c["board"], c["phase"]))

    def test_existing_joseki_helpers_output_unchanged(self):
        for c in self.data["joseki_teach_prompt"]:
            key = JOSEKI_BASIC_KEY if c["name"] == JOSEKI_BASIC_NAME else JOSEKI_KOMPA_KEY
            self.eq(prompts.joseki_teach_prompt(c["name"], key, c["corner"]),
                    c["result"], "teach name=%s" % c["name"])
        for c in self.data["joseki_short_prompt"]:
            self.eq(prompts.joseki_short_prompt(c["name"], JOSEKI_BASIC_KEY, c["gtp"]),
                    c["result"], "short gtp=%s" % c["gtp"])
        for c in self.data["joseki_hint_clause"]:
            self.eq(prompts.joseki_hint_clause(c["name"], c["expect_gtp"]),
                    c["result"], "hint name=%s" % c["name"])
        for c in self.data["joseki_fallback"]:
            self.eq(prompts.joseki_fallback(c["name"], JOSEKI_BASIC_KEY),
                    c["result"], "fallback name=%s" % c["name"])

    def test_new_templates_are_not_written_into_golden(self):
        for name in ("joseki_enter_prompt", "joseki_branch_prompt", "joseki_close_prompt"):
            self.assertNotIn(name, self.data, "新模板不写进 golden（golden 只锁既有函数）")


if __name__ == "__main__":
    unittest.main()
