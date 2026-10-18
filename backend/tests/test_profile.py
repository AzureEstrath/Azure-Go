"""``app.profile``：四维特征提取（``extract_axes``）与指数衰减融合（``merge``）。

门卫（任务书 Q2 + spec §7）：EMA 正确性；**单局 α 上限**（首样本 1 / 第 2 次 0.5 /
第 4 次起 0.25）；**置信度随局数上升**；缺维 / 脏值当 0、不抛；``merge`` 对同一输入
幂等可复现、不改入参；``n_games`` 逐维累加正确；非数值维（``top_type`` / ``address_pref``）
按「最近一次非空覆盖」、不做 EMA。
"""

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/（app.*）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # tests/（helpers）

from app.profile import (ADDRESS_AXIS, ALPHA_CAP, BLUNDER_TYPES, CORE_AXES, HALF_LIFE,
                         NON_NUMERIC_AXES, TOP_TYPE_AXIS, TOPIC_PREFIX, alpha_of,
                         confidence_of, extract_axes, merge)
from app.profile_store import ProfileStore
from app import coords

STABLE_KEYS = set(CORE_AXES) | set(NON_NUMERIC_AXES)


def _seq(*items):
    """``["B","Q16"], …`` → ``moves`` 的 JSON 形状（``[["B","Q16"], …]``）。"""
    return [[c, g] for c, g in items]


class ExtractShapeTest(unittest.TestCase):
    """形状稳定 + 缺维 / 脏值当 0、绝不抛。"""

    def test_empty_rec_is_all_zero_with_stable_shape(self):
        for rec in ({}, None, "不是字典", 42, []):
            with self.subTest(rec=rec):
                got = extract_axes(rec)
                self.assertEqual(set(got), STABLE_KEYS, "核心维的形状必须稳定")
                for axis in CORE_AXES:
                    self.assertEqual(got[axis], 0.0, axis)
                self.assertEqual(got[TOP_TYPE_AXIS], "")
                self.assertEqual(got[ADDRESS_AXIS], "")
                self.assertFalse([k for k in got if k.startswith(TOPIC_PREFIX)])

    def test_dirty_rec_never_raises_and_stays_in_range(self):
        dirty = {"ts": object(), "move_count": "x", "final_black_wr": None, "final_score": [1],
                 "result": {"a": 1}, "moves": "根本不是 JSON",
                 "per_move_ms": "x", "joseki_families": object(),
                 "chat_topics": [None, {}, 7, ["", ""]], "summary": None}
        got = extract_axes(dirty)
        for axis in CORE_AXES:
            self.assertTrue(0.0 <= got[axis] <= 1.0, "%s=%r" % (axis, got[axis]))
        self.assertIn(got[TOP_TYPE_AXIS], ("",) + BLUNDER_TYPES)
        # 条目级脏值：坏条目跳过、好条目照算
        got2 = extract_axes({"moves": [None, 42, {"gtp": 5}, {"color": "B", "gtp": "Q16"}],
                             "per_move_ms": ["a", None, 1.0]})
        self.assertTrue(0.0 <= got2["style.aggression"] <= 1.0)
        self.assertTrue(0.0 <= got2["style.territory_vs_influence"] <= 1.0)


class ExtractFeatureTest(unittest.TestCase):
    """各维的提取规则（每条规则一个用例，输入都是手造的确定棋谱）。"""

    def test_opening_preferences_by_corner_stone(self):
        """每个角取玩家最早那一手：4-4 星 / 3-3 三三 / 3-4 小目 / 5-3 目外。"""
        rec = {"moves": _seq(("B", "Q16"), ("W", "Q4"), ("B", "D16"), ("W", "D4"),
                             ("B", "R3"), ("W", "C17"), ("B", "C4"), ("W", "C3"))}
        got = extract_axes(rec)
        # Q16=右上 4-4（星）、D16=左上 4-4（星）、R3=右下 3-3（三三）、C4=左下 3-4（小目）
        self.assertAlmostEqual(got["open.star"], 0.5)
        self.assertAlmostEqual(got["open.sansan"], 0.25)
        self.assertAlmostEqual(got["open.komoku"], 0.25)
        self.assertAlmostEqual(got["open.mokuhazushi"], 0.0)

    def test_mokuhazushi_is_the_5_3_point(self):
        got = extract_axes({"moves": _seq(("B", "E17"), ("W", "Q4"))})       # E17 = 左上 5-3
        self.assertAlmostEqual(got["open.mokuhazushi"], 1.0)
        self.assertAlmostEqual(got["open.star"], 0.0)

    def test_opening_window_ignores_late_corner_stones(self):
        """开局窗口＝玩家自己的前 12 手：第 13 手才去角上不算开局偏好。"""
        moves = [["B", coords.xy_to_gtp(10, 4 + i)] for i in range(12)]   # 前 12 手都在中腹一带
        moves.append(["B", coords.xy_to_gtp(3, 3)])                       # 第 13 手才去星位
        got = extract_axes({"moves": moves})
        self.assertEqual([got[a] for a in
                          ("open.star", "open.komoku", "open.sansan", "open.mokuhazushi")],
                         [0.0, 0.0, 0.0, 0.0])

    def test_favorite_kakari_from_joseki_families(self):
        rec = {"joseki_families": {"star_kakari_small": 3, "komoku_shimari": 1, "star_33": 5}}
        self.assertAlmostEqual(extract_axes(rec)["open.favorite_kakari"], 0.75)
        self.assertEqual(extract_axes({"joseki_families": {}})["open.favorite_kakari"], 0.0)
        self.assertEqual(extract_axes({})["open.favorite_kakari"], 0.0)
        # 列表形状也认
        self.assertAlmostEqual(
            extract_axes({"joseki_families": ["star_kakari_small", ["komoku_shimari", 3]]}
                         )["open.favorite_kakari"], 0.25)

    def test_aggression_and_tenuki(self):
        """接触战占比 / 脱先占比（与上一手切比雪夫 ≥4）。"""
        rec = {"moves": _seq(("B", "Q16"), ("W", "Q15"), ("B", "Q14"), ("W", "D4"),
                             ("B", "C3"))}
        got = extract_axes(rec)
        self.assertAlmostEqual(got["style.aggression"], 1 / 3)       # 只有 Q14 与 W 相邻
        self.assertAlmostEqual(got["style.tenuki"], 0.0)             # 两手都贴着上一手
        # 脱先：第二手跑到远处
        got2 = extract_axes({"moves": _seq(("B", "Q16"), ("W", "Q15"), ("B", "D4"))})
        self.assertAlmostEqual(got2["style.tenuki"], 1.0)
        # 首手没有上一手 ⇒ 不进分母
        got3 = extract_axes({"moves": _seq(("B", "Q16"))})
        self.assertAlmostEqual(got3["style.tenuki"], 0.0)
        self.assertAlmostEqual(got3["style.aggression"], 0.0)

    def test_territory_vs_influence_is_mean_line_height(self):
        """离最近边的线数 / 8 的均值：全在边上＝偏低（实地）、全在中腹＝偏高（势力）。"""
        rec = {"moves": _seq(("B", "Q16"), ("W", "Q15"), ("B", "Q14"), ("W", "D4"),
                             ("B", "C3"))}
        got = extract_axes(rec)
        # Q16/Q14 都靠右边（线数 3）、C3 靠左下（线数 2）⇒ (3+3+2)/3 再除以 8
        self.assertAlmostEqual(got["style.territory_vs_influence"], (3 + 3 + 2) / (3 * 18))
        low = extract_axes({"moves": _seq(("B", coords.xy_to_gtp(0, 9)), ("W", "K10"),
                                          ("B", coords.xy_to_gtp(1, 9)))})
        high = extract_axes({"moves": _seq(("B", coords.xy_to_gtp(9, 9)), ("W", "B1"),
                                           ("B", coords.xy_to_gtp(9, 10)))})
        self.assertLess(low["style.territory_vs_influence"],
                        high["style.territory_vs_influence"])
        self.assertAlmostEqual(low["style.territory_vs_influence"], (0 + 1) / (2 * 18))
        # 「线数」＝离四条边的**最小**距离：(9,9) 是 9、(9,10) 离下边只有 8
        self.assertAlmostEqual(high["style.territory_vs_influence"], (9 + 8) / (2 * 18))

    def test_blunder_rate_and_weak_corner_from_drop_data(self):
        """有亏手数据：rate＝真亏手 / 玩家手数；薄弱方位＝亏手按最近角分账。"""
        moves = [{"color": "B", "gtp": "D16", "drop": 0.30},        # 左上，亏得多
                 {"color": "W", "gtp": "Q4"},
                 {"color": "B", "gtp": "Q16", "drop": 0.02},        # 不算真亏
                 {"color": "W", "gtp": "D4"},
                 {"color": "B", "gtp": "C3"},
                 {"color": "W", "gtp": "C4"},
                 {"color": "B", "gtp": "K10"}]
        got = extract_axes({"moves": moves})
        self.assertAlmostEqual(got["blunder.rate"], 0.25)           # 1 / 4
        self.assertAlmostEqual(got["weak.corner_tl"], 1.0)
        self.assertAlmostEqual(got["weak.corner_tr"], 0.0)
        self.assertAlmostEqual(got["weak.corner_bl"], 0.0)
        self.assertAlmostEqual(got["weak.corner_br"], 0.0)
        self.assertIn(got[TOP_TYPE_AXIS], BLUNDER_TYPES)

    def test_blunder_type_contact_is_unreasonable(self):
        """与对方相邻的真亏手 → 「无理」；孤零零的 → 「方向」。"""
        contact = extract_axes({"moves": [{"color": "B", "gtp": "Q16"},
                                          {"color": "W", "gtp": "Q15"},
                                          {"color": "B", "gtp": "Q14", "drop": 0.4}]})
        self.assertEqual(contact[TOP_TYPE_AXIS], "无理")
        alone = extract_axes({"moves": [{"color": "B", "gtp": "D16", "drop": 0.4},
                                        {"color": "W", "gtp": "Q4"}]})
        self.assertEqual(alone[TOP_TYPE_AXIS], "方向")

    def test_blunder_from_optional_losses_key(self):
        """可选键 ``losses``：钉死的十个键之外、有就用。"""
        got = extract_axes({"moves": _seq(("B", "D16"), ("W", "Q4"), ("B", "C3"), ("W", "C4")),
                            "losses": [{"gtp": "D16", "drop": 0.5}]})
        self.assertAlmostEqual(got["blunder.rate"], 0.5)
        self.assertAlmostEqual(got["weak.corner_tl"], 1.0)

    def test_rush_proxy_when_no_drop_data(self):
        """没有亏手数据但有 5 手以上用时：退化为「随手」代理（用时 < 中位数 30%）。"""
        moves, ms = [], []
        for i in range(5):
            moves += [["B", "K%d" % (10 + i)], ["W", "L%d" % (10 + i)]]
            ms += [1000, 1000]
        ms[8] = 100                              # 第 5 手玩家落子只用 0.1 秒
        got = extract_axes({"moves": moves, "per_move_ms": ms})
        self.assertAlmostEqual(got["blunder.rate"], 0.2)
        self.assertEqual(got[TOP_TYPE_AXIS], "随手")
        # 样本不足 5 手：不做代理
        short = extract_axes({"moves": moves[:4], "per_move_ms": [1000, 1000, 100, 1000]})
        self.assertEqual(short["blunder.rate"], 0.0)
        self.assertEqual(short[TOP_TYPE_AXIS], "")
        # 中位数为 0（没有用时数据）：不做代理
        self.assertEqual(extract_axes({"moves": moves})["blunder.rate"], 0.0)

    def test_per_move_ms_accepts_player_only_and_dict_shapes(self):
        moves = []
        for i in range(6):
            moves += [["B", "K%d" % (10 + i)], ["W", "L%d" % (10 + i)]]
        player_only = [1000, 1000, 1000, 1000, 1000, 100]            # 只含玩家手
        got = extract_axes({"moves": moves, "per_move_ms": player_only})
        self.assertAlmostEqual(got["blunder.rate"], 1 / 6)
        got2 = extract_axes({"moves": moves, "per_move_ms": {"E": player_only, "A": [1] * 6}})
        self.assertAlmostEqual(got2["blunder.rate"], 1 / 6)

    def test_topic_axes_are_shares_of_this_game(self):
        rec = {"chat_topics": [["定式", "他说到定式"], ["定式", "又说了一次"],
                               ["闲聊", "他随口聊了几句"]]}
        got = extract_axes(rec)
        self.assertAlmostEqual(got[TOPIC_PREFIX + "定式"], 2 / 3)
        self.assertAlmostEqual(got[TOPIC_PREFIX + "闲聊"], 1 / 3)
        self.assertAlmostEqual(sum(v for k, v in got.items() if k.startswith(TOPIC_PREFIX)), 1.0)
        self.assertEqual([k for k in extract_axes({}) if k.startswith(TOPIC_PREFIX)], [])

    def test_address_pref_is_optional_and_empty_by_default(self):
        self.assertEqual(extract_axes({})[ADDRESS_AXIS], "")
        self.assertEqual(extract_axes({"address_pref": "  教练  "})[ADDRESS_AXIS], "教练")
        self.assertEqual(extract_axes({"address_pref": 42})[ADDRESS_AXIS], "")
        self.assertEqual(extract_axes({"address_pref": None})[ADDRESS_AXIS], "")

    def test_pretty_json_strings_are_accepted(self):
        """``profile_store`` 交回来的就是 JSON 文本——文本 / 已解析对象两种都要能跑。"""
        raw = '{"moves": [["B","Q16"],["W","Q4"]], "chat_topics": [["定式","x"]],' \
              ' "joseki_families": {"star_kakari_small": 1}}'
        parsed = {"moves": [["B", "Q16"], ["W", "Q4"]], "chat_topics": [["定式", "x"]],
                  "joseki_families": {"star_kakari_small": 1}}
        import json
        self.assertEqual(extract_axes(json.loads(raw)), extract_axes(parsed))


class MergeFormulaTest(unittest.TestCase):
    """α 与 confidence 的公式逐条钉死（控制者给的算式）。"""

    def test_alpha_of_matches_pinned_formula(self):
        """任务书逐字给出的那串数：1 / 0.5 / 1/3 / 0.25…（见 ``alpha_of`` 的取舍注释）。"""
        self.assertEqual(alpha_of(0), 1.0)                  # 首样本：α=1（直接取 x）
        self.assertEqual(alpha_of(1), 0.5)                  # 第 2 次：0.5
        self.assertAlmostEqual(alpha_of(2), 1 / 3)          # 第 3 次：1/3
        self.assertEqual(alpha_of(3), ALPHA_CAP)            # 第 4 次起：稳在 0.25
        self.assertEqual(alpha_of(10), ALPHA_CAP)
        self.assertEqual(alpha_of(1000), ALPHA_CAP)
        self.assertEqual(alpha_of(-5), 1.0)                 # 脏输入当 0

    def test_single_game_alpha_is_capped_in_steady_state(self):
        """门卫「单局 α 上限」：有 3 局以上样本后，一局最多挪 25%（防一局爆冷）。"""
        for n in (3, 4, 10, 50, 1000):
            with self.subTest(n=n):
                self.assertLessEqual(alpha_of(n), ALPHA_CAP)
                prev = {"open.star": {"value": 0.0, "n_games": n}}
                got = merge(prev, {"open.star": 1.0}, {})
                self.assertLessEqual(got["open.star"]["value"], ALPHA_CAP + 1e-9)

    def test_alpha_cap_is_never_exceeded_in_merge(self):
        """防一局爆冷：不管 x 多极端、样本多少，单局最多挪 α_cap。"""
        prev = {"open.star": {"value": 0.0, "n_games": 50}}
        got = merge(prev, {"open.star": 1.0}, {})
        self.assertAlmostEqual(got["open.star"]["value"], ALPHA_CAP)
        got2 = merge({"open.star": 0.0}, {"open.star": 1.0}, 50)
        self.assertAlmostEqual(got2["open.star"]["value"], ALPHA_CAP)

    def test_confidence_of_matches_pinned_formula(self):
        self.assertEqual(confidence_of(0), 0.0)
        self.assertAlmostEqual(confidence_of(1), 1 - 0.5 ** (1 / 20))
        self.assertAlmostEqual(confidence_of(HALF_LIFE), 0.5)
        self.assertAlmostEqual(confidence_of(10, half_life=10), 0.5)
        self.assertEqual(confidence_of(5, half_life=0), 1.0)        # 脏 half_life：不炸
        self.assertEqual(confidence_of("x"), 0.0)

    def test_confidence_rises_with_games(self):
        vals = [confidence_of(n) for n in (1, 3, 10, 20, 40, 100, 500)]
        self.assertEqual(vals, sorted(vals))
        self.assertTrue(all(0.0 <= v < 1.0 for v in vals))

    def test_merge_follows_ema_step_by_step(self):
        """v ← v*(1-α) + x*α：首样本取 x、第 2 次 0.5、第 3 次 1/3、第 4 次起 0.25。"""
        out = merge({}, {"open.star": 1.0}, 0)
        self.assertAlmostEqual(out["open.star"]["value"], 1.0)
        self.assertEqual(out["open.star"]["n_games"], 1)
        out = merge(out, {"open.star": 0.0}, {})
        self.assertAlmostEqual(out["open.star"]["value"], 0.5)      # 1*0.5 + 0*0.5
        self.assertEqual(out["open.star"]["n_games"], 2)
        out = merge(out, {"open.star": 0.0}, {})
        self.assertAlmostEqual(out["open.star"]["value"], 1 / 3)    # 0.5*(1-1/3)
        self.assertEqual(out["open.star"]["n_games"], 3)
        out = merge(out, {"open.star": 1.0}, {})
        self.assertAlmostEqual(out["open.star"]["value"], (1 / 3) * 0.75 + 0.25)
        self.assertEqual(out["open.star"]["n_games"], 4)
        self.assertAlmostEqual(out["open.star"]["confidence"], confidence_of(4))

    def test_n_games_accumulates_per_axis(self):
        """每个维度各自维护 n_games：互不串味、只在自己被更新时 +1。"""
        prev = {"open.star": {"value": 0.2, "n_games": 7},
                "style.tenuki": {"value": 0.4, "n_games": 2}}
        got = merge(prev, {"open.star": 1.0}, {})
        self.assertEqual(got["open.star"]["n_games"], 8)
        self.assertAlmostEqual(got["open.star"]["value"], 0.2 * 0.75 + 0.25)   # α=0.25
        self.assertEqual(got["style.tenuki"]["n_games"], 3, "缺键的核心维＝当 0 也计一次")
        self.assertAlmostEqual(got["style.tenuki"]["value"], 0.4 * (1 - 1 / 3))  # α=1/3
        # 按维给 n_games：优先于 prev 里记的
        got2 = merge(prev, {"open.star": 1.0}, {"open.star": 9})
        self.assertAlmostEqual(got2["open.star"]["value"], 0.2 * 0.75 + 0.25)
        self.assertEqual(got2["open.star"]["n_games"], 10)

    def test_missing_or_dirty_numeric_axis_counts_as_zero(self):
        """任务书：缺维 / 脏值 → 当 0、不抛。"""
        got = merge({"open.star": {"value": 1.0, "n_games": 1}}, {}, {})
        self.assertAlmostEqual(got["open.star"]["value"], 0.5)
        got2 = merge({"open.star": {"value": 1.0, "n_games": 1}}, {"open.star": "abc"}, {})
        self.assertAlmostEqual(got2["open.star"]["value"], 0.5)
        got3 = merge({"open.star": {"value": 1.0, "n_games": 1}}, {"open.star": None}, {})
        self.assertAlmostEqual(got3["open.star"]["value"], 0.5)

    def test_values_are_clamped_to_unit_interval(self):
        self.assertAlmostEqual(merge({}, {"open.star": 5.0}, 0)["open.star"]["value"], 1.0)
        self.assertAlmostEqual(merge({}, {"open.star": -3}, 0)["open.star"]["value"], 0.0)
        self.assertAlmostEqual(
            merge({"open.star": {"value": 9.0, "n_games": 1}}, {"open.star": 1.0}, {}
                  )["open.star"]["value"], 1.0)

    def test_optional_axis_is_carried_over_not_decayed(self):
        """``persona.topic.*`` 这类「结构上可缺」的维：本局没聊到就原样带过、不衰减。"""
        prev = {TOPIC_PREFIX + "定式": {"value": 0.9, "n_games": 3}}
        got = merge(prev, {}, {})
        self.assertAlmostEqual(got[TOPIC_PREFIX + "定式"]["value"], 0.9)
        self.assertEqual(got[TOPIC_PREFIX + "定式"]["n_games"], 3)
        self.assertAlmostEqual(got[TOPIC_PREFIX + "定式"]["confidence"], confidence_of(3))
        # 聊到了：照常 EMA + 计数
        got2 = merge(prev, {TOPIC_PREFIX + "定式": 0.1}, {})
        self.assertAlmostEqual(got2[TOPIC_PREFIX + "定式"]["value"], 0.9 * 0.75 + 0.1 * 0.25)
        self.assertEqual(got2[TOPIC_PREFIX + "定式"]["n_games"], 4)

    def test_merge_accepts_flat_prev_and_none_x(self):
        got = merge({"open.star": 0.5}, None, 1)
        self.assertAlmostEqual(got["open.star"]["value"], 0.5 * 0.5)     # α=0.5，x 缺＝0
        self.assertEqual(got["open.star"]["n_games"], 2)
        self.assertEqual(merge({}, {}, 0), {})
        self.assertEqual(merge(None, None, None), {})
        self.assertEqual(merge("脏", {"open.star": 1.0}, {})["open.star"]["value"], 1.0)


class MergeNonNumericTest(unittest.TestCase):
    """非数值维：字符串、**不做 EMA**、最近一次非空覆盖、空＝未知＝跳过。"""

    def test_top_type_last_non_empty_wins(self):
        out = merge({}, {TOP_TYPE_AXIS: "随手"}, 0)
        self.assertEqual(out[TOP_TYPE_AXIS]["value"], "随手")
        self.assertEqual(out[TOP_TYPE_AXIS]["n_games"], 1)
        out = merge(out, {TOP_TYPE_AXIS: "无理"}, {})
        self.assertEqual(out[TOP_TYPE_AXIS]["value"], "无理")
        self.assertEqual(out[TOP_TYPE_AXIS]["n_games"], 2)
        self.assertAlmostEqual(out[TOP_TYPE_AXIS]["confidence"], confidence_of(2))

    def test_empty_or_missing_text_does_not_clobber(self):
        prev = {TOP_TYPE_AXIS: {"value": "深入", "n_games": 4},
                ADDRESS_AXIS: {"value": "教练", "n_games": 2}}
        for x in ({}, {TOP_TYPE_AXIS: ""}, {TOP_TYPE_AXIS: None}, {TOP_TYPE_AXIS: "   "}):
            with self.subTest(x=x):
                got = merge(prev, x, {})
                self.assertEqual(got[TOP_TYPE_AXIS]["value"], "深入")
                self.assertEqual(got[TOP_TYPE_AXIS]["n_games"], 4, "未知不涨样本数")
                self.assertEqual(got[ADDRESS_AXIS]["value"], "教练")
        # 首样本就是空：不发这个键（＝「无画像」，不是「空画像」）
        self.assertEqual(merge({}, {TOP_TYPE_AXIS: ""}, 0), {})

    def test_address_pref_round_trip(self):
        got = merge({}, {ADDRESS_AXIS: "先生"}, 0)
        self.assertEqual(got[ADDRESS_AXIS]["value"], "先生")
        self.assertIn(ADDRESS_AXIS, NON_NUMERIC_AXES)


class MergePurityTest(unittest.TestCase):
    """``merge`` 纯函数：同一输入幂等可复现、不改入参。"""

    def test_same_input_gives_same_output_and_no_mutation(self):
        prev = {"open.star": {"value": 0.4, "n_games": 3},
                TOP_TYPE_AXIS: {"value": "随手", "n_games": 1},
                TOPIC_PREFIX + "定式": {"value": 0.5, "n_games": 2}}
        x = {"open.star": 0.9, TOP_TYPE_AXIS: "方向", TOPIC_PREFIX + "定式": 0.2,
             "style.tenuki": 0.1}
        prev_before, x_before = copy.deepcopy(prev), copy.deepcopy(x)
        first = merge(prev, x, {})
        second = merge(prev, x, {})
        self.assertEqual(first, second)
        self.assertEqual(prev, prev_before, "不得改 prev")
        self.assertEqual(x, x_before, "不得改 x")
        # 输出键顺序确定（可复现的另一个侧面）
        self.assertEqual(list(first), sorted(first))

    def test_merge_is_associative_in_the_expected_way(self):
        """逐局融合的结果与「一局一局来」一致（增量式：同一条记录融两次＝按公式算两次）。"""
        x = {"open.star": 0.8}
        a = merge({}, x, 0)
        b = merge(a, x, {})
        self.assertAlmostEqual(b["open.star"]["value"], 0.8 * 0.5 + 0.8 * 0.5)
        self.assertEqual(b["open.star"]["n_games"], 2)


class PipelineTest(unittest.TestCase):
    """端到端：记录 → 提取 → 融合 → 落盘 → 读回（Q3 的写入点就是这么用的）。"""

    def test_records_to_profile_db_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(os.path.join(d, "profile.db"))
            recs = [
                {"ts": 100.0, "moves": _seq(("B", "Q16"), ("W", "Q4"), ("B", "D16")),
                 "joseki_families": {"star_kakari_small": 1}, "chat_topics": [["定式", "x"]]},
                {"ts": 200.0, "moves": _seq(("B", "Q16"), ("W", "Q4"), ("B", "Q16")),
                 "chat_topics": [["定式", "y"], ["闲聊", "z"]]},
            ]
            for rec in recs:
                self.assertGreater(store.add_game(rec), 0)
            profile = store.get_profile()
            for rec in store.games():
                profile = merge(profile, extract_axes(rec), {})
            self.assertEqual(store.upsert_axes(profile), len(profile))
            back = store.get_profile()
            self.assertEqual(set(back), set(profile))
            self.assertEqual(back["open.star"]["n_games"], 2)
            self.assertAlmostEqual(back["open.star"]["value"], 1.0, places=6)
            self.assertAlmostEqual(back["persona.topic.定式"]["value"], 0.75)
            self.assertEqual(back["persona.topic.定式"]["n_games"], 2)
            # 幂等：同一批记录再融一次，值按公式再走一步（可复现），不是随机
            again = merge(back, extract_axes(store.games()[0]), {})
            self.assertAlmostEqual(again["open.star"]["n_games"], 3)


if __name__ == "__main__":
    unittest.main()
