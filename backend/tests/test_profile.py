"""``app.profile``：四维特征提取（``extract_axes``）与指数衰减融合（``merge``）。

门卫（任务书 Q2 + spec §7 + **评审前修正**）：
- **α（裁决 1 的 5 条）**：① ``n=0 ⇒ α=1``（初始化）；② ``n≥1 ⇒ α ≤ alpha_cap``（上限是真上限）；
  ③ 随 ``n`` **单调不增**且收敛到**半衰期稳态** ``1-0.5**(1/half_life)``（不是继续变小、也不是卡在 0.25）；
  ④ 半衰期越大 ⇒ 稳态 ``α`` 越小；⑤ 置信度随局数单调上升；
- **`blunder` / `weak.corner_*`（裁决 2）**：有逐手 ``drop`` ⇒ 算出这两组键；
  **缺 ``drop`` ⇒ 不产出这组键**（不把「不知道」写成 0）；
- **`persona.address_pref`（裁决 3）**：推导不出来就**不产出该键**；
- 通用：缺维 / 脏值当 0、不抛；``merge`` 对同一输入幂等可复现、不改入参；``n_games`` 逐维累加。
"""

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/（app.*）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # tests/（helpers）

from app import coords
from app.profile import (ADDRESS_AXIS, ALWAYS_AXES, ALPHA_CAP, BLUNDER_AXIS, BLUNDER_TYPES,
                         CORE_AXES, DROP_AXES, HALF_LIFE, KAKARI_AXIS, NON_NUMERIC_AXES,
                         TOP_TYPE_AXIS, TOPIC_PREFIX, WEAK_AXES, alpha_of, confidence_of,
                         extract_axes, merge, steady_alpha)
from app.profile_store import ProfileStore

STABLE_KEYS = set(ALWAYS_AXES)


def _seq(*items):
    """``["B","Q16"], …`` → ``moves`` 的 JSON 形状（``[["B","Q16"], …]``，**无 drop 信号**）。"""
    return [[c, g] for c, g in items]


def _mv(color, gtp, drop=None):
    """一条带（可选）``drop`` 的 ``moves`` 条目——裁决 2 定稿的形状。"""
    it = {"color": color, "gtp": gtp}
    if drop is not None:
        it["drop"] = drop
    return it


class ExtractShapeTest(unittest.TestCase):
    """形状稳定 + 缺维 / 脏值当 0、绝不抛。"""

    def test_empty_rec_produces_no_axes_at_all(self):
        """空 rec：**一个键都不产出**（修复轮 1 / I-2）——没有信号就没有维，不是「全 0」。

        结构性 0.0 会被渲染侧（Q3 的画像块）念成观测到的特征（「他爱先守角」「很少脱先」），
        所以「不知道」一律**不发这个键**（与 ``blunder.*`` / ``weak.*`` 的 ``_has_drop_data`` 同一做法）。
        """
        for rec in ({}, None, "不是字典", 42, []):
            with self.subTest(rec=rec):
                got = extract_axes(rec)
                self.assertEqual(got, {}, "空 rec 没有任何信号 ⇒ 不产出任何维")
                for axis in ALWAYS_AXES:
                    self.assertNotIn(axis, got, "没有信号 ⇒ 不产出 %s" % axis)
                for axis in DROP_AXES:
                    self.assertNotIn(axis, got, "没有 drop 信号 ⇒ 不产出 %s" % axis)
                self.assertNotIn(ADDRESS_AXIS, got, "没有信号源 ⇒ 不产出 address_pref")
                self.assertEqual([k for k in got if k.startswith(TOPIC_PREFIX)], [])

    def test_axes_that_do_have_signal_stay_in_range(self):
        """有信号时该出的维都要出、且都在 ``[0,1]``（防 I-2 的「不产出」改过头）。"""
        got = extract_axes({"moves": _seq(("B", "Q16"), ("W", "Q15"), ("B", "D4")),
                            "joseki_families": {"star_kakari_small": 1}})
        for axis in ALWAYS_AXES:
            self.assertIn(axis, got, "%s 有信号（角 + 两手棋 + 挂角族）⇒ 必须产出" % axis)
            self.assertTrue(0.0 <= got[axis] <= 1.0, "%s=%r" % (axis, got[axis]))
        self.assertTrue(STABLE_KEYS <= set(got), "ALWAYS 组全在：缺 %r" % (STABLE_KEYS - set(got)))

    def test_dirty_rec_never_raises_and_stays_in_range(self):
        dirty = {"ts": object(), "move_count": "x", "final_black_wr": None, "final_score": [1],
                 "result": {"a": 1}, "moves": "根本不是 JSON",
                 "per_move_ms": "x", "joseki_families": object(),
                 "chat_topics": [None, {}, 7, ["", ""]], "summary": None}
        got = extract_axes(dirty)
        # 全是脏值 ⇒ 没有信号、也不产出任何**结构性**维（`chat_topics` 里的脏条目会成主题，
        # 那是既有的 `persona.topic.*` 口径，Minor #8 已裁决不改）
        for axis in ALWAYS_AXES:
            self.assertNotIn(axis, got, "脏 moves ⇒ %s 没有信号、不产出" % axis)
        # 条目级脏值：坏条目跳过、好条目照算；脏 drop 当「没亏」但**信号仍在**
        got2 = extract_axes({"moves": [None, 42, {"gtp": 5}, {"color": "B", "gtp": "Q16"},
                                       {"color": "B", "gtp": "D16", "drop": "abc"}]})
        self.assertTrue(0.0 <= got2["style.aggression"] <= 1.0)
        self.assertTrue(0.0 <= got2["style.territory_vs_influence"] <= 1.0)
        self.assertIn(BLUNDER_AXIS, got2, "有条目带 drop 键（哪怕是脏值）就算「有信号」")
        self.assertEqual(got2[BLUNDER_AXIS], 0.0)
        self.assertNotIn(TOP_TYPE_AXIS, got2, "没有真亏手 ⇒ 分不出手型、不产出")
        # DROP 组（有信号时）也要在 [0,1] 内
        for axis in (BLUNDER_AXIS,) + WEAK_AXES:
            self.assertTrue(0.0 <= got2[axis] <= 1.0, "%s=%r" % (axis, got2[axis]))
        # 脏值但**数值**的 drop：照样落进 [0,1]
        got3 = extract_axes({"moves": [_mv("B", "D16", 5.0), _mv("B", "Q16", -1),
                                       _mv("W", "Q4", 0.9)]})
        self.assertTrue(0.0 <= got3[BLUNDER_AXIS] <= 1.0)
        self.assertTrue(all(0.0 <= got3[a] <= 1.0 for a in WEAK_AXES))


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
        """开局窗口＝玩家自己的前 12 手：第 13 手才去角上 ⇒ 角上**没有信号**（不是 star=1.0）。"""
        moves = [["B", coords.xy_to_gtp(10, 4 + i)] for i in range(12)]   # 前 12 手都在中腹一带
        moves.append(["B", coords.xy_to_gtp(3, 3)])                       # 第 13 手才去星位
        got = extract_axes({"moves": moves})
        # 窗口内没占到角 ⇒ 四类都没有信号（修复轮 1 / I-2：不产出，而不是「全 0」）
        for axis in ("open.star", "open.komoku", "open.sansan", "open.mokuhazushi"):
            self.assertNotIn(axis, got, "窗口外的角不算开局偏好 ⇒ %s 无信号、不产出" % axis)

    def test_favorite_kakari_from_joseki_families(self):
        rec = {"joseki_families": {"star_kakari_small": 3, "komoku_shimari": 1, "star_33": 5}}
        self.assertAlmostEqual(extract_axes(rec)["open.favorite_kakari"], 0.75)
        # 修复轮 1 / I-2：没识别出挂角 / 守角族 ⇒ **不产出**（0.0 会被念成「他爱先守角」）
        self.assertNotIn(KAKARI_AXIS, extract_axes({"joseki_families": {}}))
        self.assertNotIn(KAKARI_AXIS, extract_axes({}))
        # 只有挂角、没有守角：0.0 是**真的量到 0**（有信号）⇒ 照样产出
        self.assertEqual(extract_axes({"joseki_families": {"star_kakari_small": 2}})[KAKARI_AXIS], 1.0)
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
        # 首手没有上一手 ⇒ 脱先**没有信号**（不产出；修复轮 1 / I-2），不是「很少脱先」
        got3 = extract_axes({"moves": _seq(("B", "Q16"))})
        self.assertNotIn("style.tenuki", got3, "只落 1 手 ⇒ 脱先无信号、不产出")
        self.assertAlmostEqual(got3["style.aggression"], 0.0)

    def test_territory_vs_influence_is_mean_line_height(self):
        """离最近边的线数 / 18 的均值：全在边上＝偏低（实地）、全在中腹＝偏高（势力）。"""
        rec = {"moves": _seq(("B", "Q16"), ("W", "Q15"), ("B", "Q14"), ("W", "D4"),
                             ("B", "C3"))}
        got = extract_axes(rec)
        # Q16/Q14 都靠右边（线数 3）、C3 靠左下（线数 2）⇒ (3+3+2)/3 再除以 18
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

    # —— 裁决 2：blunder.rate / weak.corner_* 读逐手 drop ——

    def test_drop_signal_produces_blunder_rate_and_weak_corners(self):
        """有 drop：rate＝真亏手数 / **玩家手数**；weak＝该象限真亏数 / **玩家**在该象限落子数。"""
        moves = [_mv("B", "D16", 0.30),      # 左上，真亏（>0.10）
                 _mv("W", "Q4"),             # 右下
                 _mv("B", "Q16", 0.02),      # 右上，不算真亏
                 _mv("W", "D4"),             # 左下
                 _mv("B", "C3"),             # 左下（无 drop 键 ⇒ 当 0）
                 _mv("W", "C4"),             # 左下
                 _mv("B", "K10")]            # 左上（(9,9)：x≤9 且 y≤9）
        got = extract_axes({"moves": moves})
        self.assertAlmostEqual(got[BLUNDER_AXIS], 1 / 4)                 # 1 条真亏 / 4 手玩家手
        self.assertAlmostEqual(got["weak.corner_tl"], 0.5)               # 左上：1 亏 / 2 手玩家手
        self.assertAlmostEqual(got["weak.corner_tr"], 0.0)               # 右上：0 / 1
        self.assertAlmostEqual(got["weak.corner_bl"], 0.0)               # 左下：0 / 1（玩家只落了 C3）
        self.assertAlmostEqual(got["weak.corner_br"], 0.0)               # 右下：玩家没落子 ⇒ 0
        self.assertIn(got[TOP_TYPE_AXIS], BLUNDER_TYPES)

    def test_blunder_denominators_count_the_player_only(self):
        """I-1 / I-2 的口径钉死：**同一象限里 Azure 也落了子**时，分母仍只算玩家。

        左上角玩家只落 1 手（且真亏）、Azure 落 3 手 ⇒ 玩家口径 rate=1/1、weak_tl=1/1；
        若分母混进 Azure 的落子会得到 1/5 与 1/4（这条用例就是为抓那个回归而加的）。
        """
        moves = [_mv("B", "D16", 0.40),      # 左上，真亏
                 _mv("W", "C17"), _mv("W", "D17"), _mv("W", "C16"),   # 左上：Azure 落 3 手
                 _mv("W", "Q4")]             # 右下
        got = extract_axes({"moves": moves})
        self.assertAlmostEqual(got[BLUNDER_AXIS], 1.0)                   # 1 真亏 / 1 手玩家手
        self.assertAlmostEqual(got["weak.corner_tl"], 1.0)               # 1 真亏 / 1 手玩家手
        self.assertAlmostEqual(got["weak.corner_tr"], 0.0)
        self.assertAlmostEqual(got["weak.corner_bl"], 0.0)
        self.assertAlmostEqual(got["weak.corner_br"], 0.0)               # 玩家没去过右下

    def test_azure_drop_is_not_counted_as_the_player_blunder(self):
        """第三个面：``_drops_of`` **只认玩家（黑）的 drop**——Azure 条目带 drop 也不计。"""
        got = extract_axes({"moves": [_mv("B", "D16"), _mv("W", "Q4", 0.9)]})
        self.assertIn(BLUNDER_AXIS, got, "有 drop 键就算「有信号」")
        self.assertEqual(got[BLUNDER_AXIS], 0.0, "Azure 的亏手不算玩家的")
        self.assertNotIn(TOP_TYPE_AXIS, got)
        self.assertEqual([got[a] for a in WEAK_AXES], [0.0, 0.0, 0.0, 0.0])

    def test_no_drop_signal_means_no_blunder_keys(self):
        """裁决 2：**缺 drop ⇒ 这一组键一个都不产出**（不把「不知道」写成 0）。"""
        got = extract_axes({"moves": _seq(("B", "D16"), ("W", "Q4"), ("B", "C3"))})
        for axis in DROP_AXES:
            self.assertNotIn(axis, got, axis)
        # 只有别的手带 drop 键也算「有信号」（该手自己的 drop 缺省 0）
        got2 = extract_axes({"moves": [_mv("B", "D16"), _mv("W", "Q4", 0.0)]})
        self.assertIn(BLUNDER_AXIS, got2)
        self.assertEqual(got2[BLUNDER_AXIS], 0.0)

    def test_drop_signal_without_real_blunder_emits_zero(self):
        """有信号但没有真亏手：rate / weak 照产出（值 0），**手型不产出**（分不出来）。"""
        got = extract_axes({"moves": [_mv("B", "D16", 0.10), _mv("W", "Q4", 0.0)]})
        self.assertEqual(got[BLUNDER_AXIS], 0.0)                 # 0.10 不算真亏（要 > 0.10）
        self.assertEqual([got[a] for a in WEAK_AXES], [0.0, 0.0, 0.0, 0.0])
        self.assertNotIn(TOP_TYPE_AXIS, got)

    def test_blunder_type_contact_is_unreasonable(self):
        """与对方相邻的真亏手 → 「无理」；孤零零的 → 「方向」。"""
        contact = extract_axes({"moves": [_mv("B", "Q16"), _mv("W", "Q15"),
                                          _mv("B", "Q14", 0.4)]})
        self.assertEqual(contact[TOP_TYPE_AXIS], "无理")
        alone = extract_axes({"moves": [_mv("B", "D16", 0.4), _mv("W", "Q4")]})
        self.assertEqual(alone[TOP_TYPE_AXIS], "方向")

    def test_weak_corner_zero_when_quadrant_has_no_moves(self):
        """「无落子则 0」：该象限没落子时那个键**照样产出**、值为 0。"""
        got = extract_axes({"moves": [_mv("B", "D16", 0.4), _mv("W", "D4")]})
        self.assertAlmostEqual(got["weak.corner_tl"], 1.0)       # 1 亏 / 1 手
        self.assertAlmostEqual(got["weak.corner_tr"], 0.0)       # 右上没人落子
        self.assertAlmostEqual(got["weak.corner_br"], 0.0)
        self.assertEqual(set(WEAK_AXES) - set(got), set())

    # —— 性格与话题 ——

    def test_topic_axes_are_shares_of_this_game(self):
        rec = {"chat_topics": [["定式", "他说到定式"], ["定式", "又说了一次"],
                               ["闲聊", "他随口聊了几句"]]}
        got = extract_axes(rec)
        self.assertAlmostEqual(got[TOPIC_PREFIX + "定式"], 2 / 3)
        self.assertAlmostEqual(got[TOPIC_PREFIX + "闲聊"], 1 / 3)
        self.assertAlmostEqual(sum(v for k, v in got.items() if k.startswith(TOPIC_PREFIX)), 1.0)
        self.assertEqual([k for k in extract_axes({}) if k.startswith(TOPIC_PREFIX)], [])

    def test_address_pref_is_not_produced_without_signal(self):
        """裁决 3：推导不出来就**不产出该键**（不恒发空串占位）；给了显式信号才带出来。"""
        for rec in ({}, {"address_pref": ""}, {"address_pref": "   "},
                    {"address_pref": None}, {"address_pref": 42}):
            with self.subTest(rec=rec):
                self.assertNotIn(ADDRESS_AXIS, extract_axes(rec))
        self.assertEqual(extract_axes({"address_pref": "  教练  "})[ADDRESS_AXIS], "教练")

    def test_pretty_json_strings_are_accepted(self):
        """``profile_store`` 交回来的就是 JSON 文本——文本 / 已解析对象两种都要能跑。"""
        raw = '{"moves": [{"color":"B","gtp":"Q16","drop":0.4},["W","Q4"]],' \
              ' "chat_topics": [["定式","x"]], "joseki_families": {"star_kakari_small": 1}}'
        parsed = {"moves": [{"color": "B", "gtp": "Q16", "drop": 0.4}, ["W", "Q4"]],
                  "chat_topics": [["定式", "x"]], "joseki_families": {"star_kakari_small": 1}}
        self.assertEqual(extract_axes(json.loads(raw)), extract_axes(parsed))
        self.assertAlmostEqual(extract_axes(parsed)[BLUNDER_AXIS], 1.0)   # 1 真亏 / 1 手玩家手


class AlphaFormulaTest(unittest.TestCase):
    """裁决 1 的 5 条门卫（α 的上限是真上限 + 半衰期稳态收敛）。"""

    def test_first_sample_initializes_with_alpha_one(self):
        """① ``n=0 ⇒ α=1``：首样本＝初始化，直接取 x。"""
        self.assertEqual(alpha_of(0), 1.0)
        self.assertEqual(alpha_of(-3), 1.0)                 # 脏输入当 0
        got = merge({}, {"open.star": 0.8}, 0)
        self.assertAlmostEqual(got["open.star"]["value"], 0.8)
        self.assertEqual(got["open.star"]["n_games"], 1)

    def test_alpha_is_capped_for_every_nonzero_sample(self):
        """② ``n ≥ 1 ⇒ α ≤ alpha_cap``（上限是真上限，不是下限）。"""
        for n in range(1, 300):
            with self.subTest(n=n):
                self.assertLessEqual(alpha_of(n), ALPHA_CAP)
        prev = {"open.star": {"value": 0.0, "n_games": 1}}
        got = merge(prev, {"open.star": 1.0}, {})
        self.assertLessEqual(got["open.star"]["value"], ALPHA_CAP + 1e-12)

    def test_alpha_matches_the_pinned_self_check_table(self):
        """裁决 1 给的数值自查表（``alpha_cap=0.25``、``half_life=20``）。"""
        self.assertAlmostEqual(alpha_of(1), 0.25)
        self.assertAlmostEqual(alpha_of(2), 0.25)
        self.assertAlmostEqual(alpha_of(3), 0.25)
        self.assertAlmostEqual(alpha_of(4), 0.2)
        self.assertAlmostEqual(alpha_of(9), 0.1)
        self.assertAlmostEqual(alpha_of(28), 0.0345, places=4)     # 1/29 = 0.03448
        self.assertAlmostEqual(alpha_of(100), 0.0341, places=4)    # 半衰期稳态 0.03406

    def test_alpha_is_monotonic_and_converges_to_steady_state(self):
        """③ 随 n **单调不增**，且**收敛到半衰期稳态**（不是继续变小、也不是卡在 0.25）。"""
        vals = [alpha_of(n) for n in range(1, 400)]
        self.assertEqual(vals, sorted(vals, reverse=True), "α 必须单调不增")
        steady = steady_alpha(HALF_LIFE)
        self.assertAlmostEqual(steady, 1 - 0.5 ** (1 / 20), places=12)
        self.assertAlmostEqual(alpha_of(2000), steady, places=12)
        self.assertAlmostEqual(alpha_of(10000), steady, places=12)
        self.assertLess(alpha_of(10000), ALPHA_CAP, "大样本时不该卡在 alpha_cap")
        self.assertGreater(alpha_of(10000), 0.0)

    def test_larger_half_life_means_smaller_steady_alpha(self):
        """④ 半衰期越大 ⇒ 稳态 α 越小（遗忘越慢）。"""
        steadies = [steady_alpha(hl) for hl in (5, 10, 20, 50, 100)]
        self.assertEqual(steadies, sorted(steadies, reverse=True))
        self.assertAlmostEqual(steady_alpha(10), 1 - 0.5 ** 0.1, places=12)
        self.assertAlmostEqual(alpha_of(500, half_life=10), steady_alpha(10), places=12)
        self.assertGreater(alpha_of(500, half_life=10), alpha_of(500, half_life=50))
        self.assertEqual(alpha_of(0, half_life=10), 1.0, "首样本与半衰期无关")
        self.assertEqual(steady_alpha(0), 1.0)                     # 脏 half_life：不炸

    def test_confidence_rises_with_games(self):
        """⑤ 置信度随局数单调上升、渐近 1。"""
        vals = [confidence_of(n) for n in (1, 3, 10, 20, 40, 100, 500)]
        self.assertEqual(vals, sorted(vals))
        self.assertTrue(all(0.0 <= v < 1.0 for v in vals))
        self.assertEqual(confidence_of(0), 0.0)
        self.assertAlmostEqual(confidence_of(1), 1 - 0.5 ** (1 / 20))
        self.assertAlmostEqual(confidence_of(HALF_LIFE), 0.5)
        self.assertAlmostEqual(confidence_of(10, half_life=10), 0.5)
        self.assertEqual(confidence_of(5, half_life=0), 1.0)       # 脏 half_life：不炸
        self.assertEqual(confidence_of("x"), 0.0)


class MergeFormulaTest(unittest.TestCase):
    """EMA 的步进值、逐维计数、缺维当 0、可选维带过。"""

    def test_merge_follows_ema_step_by_step(self):
        """``v ← v*(1-α) + x*α``：首样本取 x；``n≥1`` 起 α = 0.25/0.25/0.25/0.2…"""
        out = merge({}, {"open.star": 1.0}, 0)
        self.assertAlmostEqual(out["open.star"]["value"], 1.0)
        self.assertEqual(out["open.star"]["n_games"], 1)
        for base_n in (1, 2, 3):                        # α = alpha_cap = 0.25
            out = merge(out, {"open.star": 0.0}, {})
            self.assertAlmostEqual(out["open.star"]["value"], 1.0 * 0.75 ** base_n)
            self.assertEqual(out["open.star"]["n_games"], base_n + 1)
        out = merge(out, {"open.star": 1.0}, {})        # base_n = 4 ⇒ α = 0.2
        self.assertAlmostEqual(out["open.star"]["value"], 0.75 ** 3 * 0.8 + 0.2)
        self.assertEqual(out["open.star"]["n_games"], 5)
        self.assertAlmostEqual(out["open.star"]["confidence"], confidence_of(5))

    def test_n_games_accumulates_per_axis(self):
        """每个维度各自维护 n_games：互不串味、只在自己被更新时 +1（缺键的那一维原样带过）。"""
        prev = {"open.star": {"value": 0.2, "n_games": 7},
                "style.tenuki": {"value": 0.4, "n_games": 2}}
        got = merge(prev, {"open.star": 1.0}, {})
        self.assertEqual(got["open.star"]["n_games"], 8)
        self.assertAlmostEqual(got["open.star"]["value"], 0.2 * 0.875 + 0.125)   # α = 1/8
        # v3.9.0 口径收口：``x`` 里没有 ``style.tenuki`` ⇒ **原样带过**（不衰减、不涨计数）
        self.assertEqual(got["style.tenuki"]["n_games"], 2, "缺键不涨样本数")
        self.assertAlmostEqual(got["style.tenuki"]["value"], 0.4, "缺键不衰减")
        self.assertAlmostEqual(got["style.tenuki"]["confidence"], confidence_of(2))
        # 按维给 n_games：优先于 prev 里记的
        got2 = merge(prev, {"open.star": 1.0}, {"open.star": 9})
        self.assertAlmostEqual(got2["open.star"]["value"], 0.2 * 0.9 + 0.1)       # α = 1/10
        self.assertEqual(got2["open.star"]["n_games"], 10)

    def test_missing_key_is_carried_over_not_decayed(self):
        """v3.9.0 裁决：**任何**缺键都原样带过——不衰减、不涨 ``n_games``、不改 ``confidence``。

        理由：``extract_axes`` 已经不产出无信号的维（I-2），若 ``merge`` 再把缺键按 0 融合，
        某维一旦有过信号（如 ``open.favorite_kakari=1.0``）就会被其后几局「没识别出族」逐局拉向 0
        （约 5 局后 ≤ 0.4），渲染侧又会念出「更爱先守角」——I-2 的缺陷会经 ``merge`` 复发。
        """
        prev = {"open.favorite_kakari": {"value": 1.0, "confidence": 0.3, "n_games": 3}}
        for _ in range(6):                              # 连着几局都没识别出族
            prev = merge(prev, {}, {})
            self.assertAlmostEqual(prev["open.favorite_kakari"]["value"], 1.0, msg="缺键不衰减")
            self.assertEqual(prev["open.favorite_kakari"]["n_games"], 3, "缺键不涨样本数")
            self.assertAlmostEqual(prev["open.favorite_kakari"]["confidence"], confidence_of(3),
                                   msg="置信度只由 n_games 决定 ⇒ 缺键时原样不动")
        # 别的维有信号也不影响它；``x`` 里没有的键根本不会被产出
        got = merge(prev, {"style.tenuki": 0.5}, {})
        self.assertAlmostEqual(got["open.favorite_kakari"]["value"], 1.0)
        self.assertEqual(got["open.favorite_kakari"]["n_games"], 3)
        self.assertEqual(got["style.tenuki"]["n_games"], 1, "有信号的维照常 EMA + 计数")
        # 对照：真的有信号时才照常 EMA + 计数
        got = merge({"open.favorite_kakari": {"value": 1.0, "n_games": 3}},
                    {"open.favorite_kakari": 0.0}, {})
        self.assertAlmostEqual(got["open.favorite_kakari"]["value"], 1.0 * 0.75)
        self.assertEqual(got["open.favorite_kakari"]["n_games"], 4)

    def test_present_but_dirty_numeric_axis_counts_as_zero(self):
        """``x`` 里**有**这个键、但值是脏的 ⇒ 当 0 参与（缺键与脏值是两回事）。"""
        for x in ({"open.star": "abc"}, {"open.star": None}, {"open.star": []}):
            with self.subTest(x=x):
                got = merge({"open.star": {"value": 1.0, "n_games": 1}}, x, {})
                self.assertAlmostEqual(got["open.star"]["value"], 0.75)     # α = 0.25
                self.assertEqual(got["open.star"]["n_games"], 2)
        # 缺键的对照：原样带过（见上一条用例）
        got = merge({"open.star": {"value": 1.0, "n_games": 1}}, {}, {})
        self.assertAlmostEqual(got["open.star"]["value"], 1.0)
        self.assertEqual(got["open.star"]["n_games"], 1)

    def test_values_are_clamped_to_unit_interval(self):
        self.assertAlmostEqual(merge({}, {"open.star": 5.0}, 0)["open.star"]["value"], 1.0)
        self.assertAlmostEqual(merge({}, {"open.star": -3}, 0)["open.star"]["value"], 0.0)
        self.assertAlmostEqual(
            merge({"open.star": {"value": 9.0, "n_games": 1}}, {"open.star": 1.0}, {}
                  )["open.star"]["value"], 1.0)

    def test_drop_axes_are_carried_over_when_absent(self):
        """裁决 2 的下游：本局没有 drop 信号（x 里没有这几个键）⇒ **原样带过、不衰减、不涨计数**。"""
        prev = {BLUNDER_AXIS: {"value": 0.2, "n_games": 4},
                "weak.corner_tl": {"value": 0.5, "n_games": 4}}
        got = merge(prev, {"open.star": 1.0}, {})
        self.assertAlmostEqual(got[BLUNDER_AXIS]["value"], 0.2)
        self.assertEqual(got[BLUNDER_AXIS]["n_games"], 4)
        self.assertAlmostEqual(got["weak.corner_tl"]["value"], 0.5)
        self.assertEqual(got["weak.corner_tl"]["n_games"], 4)
        self.assertAlmostEqual(got[BLUNDER_AXIS]["confidence"], confidence_of(4))
        # 有信号（x 里带着）：照常 EMA + 计数
        got2 = merge(prev, {BLUNDER_AXIS: 1.0}, {})
        self.assertAlmostEqual(got2[BLUNDER_AXIS]["value"], 0.2 * 0.8 + 0.2)   # base_n=4 ⇒ α=0.2
        self.assertEqual(got2[BLUNDER_AXIS]["n_games"], 5)

    def test_optional_topic_axis_is_carried_over_not_decayed(self):
        """``persona.topic.*`` 这类「结构上可缺」的维：本局没聊到就原样带过、不衰减。"""
        prev = {TOPIC_PREFIX + "定式": {"value": 0.9, "n_games": 3}}
        got = merge(prev, {}, {})
        self.assertAlmostEqual(got[TOPIC_PREFIX + "定式"]["value"], 0.9)
        self.assertEqual(got[TOPIC_PREFIX + "定式"]["n_games"], 3)
        got2 = merge(prev, {TOPIC_PREFIX + "定式": 0.1}, {})
        self.assertAlmostEqual(got2[TOPIC_PREFIX + "定式"]["value"], 0.9 * 0.75 + 0.1 * 0.25)
        self.assertEqual(got2[TOPIC_PREFIX + "定式"]["n_games"], 4)

    def test_half_life_parameter_reaches_the_merge_step(self):
        """``half_life`` 越大 ⇒ 样本多时单局挪得越少（真正的指数遗忘）。"""
        prev = {"open.star": {"value": 0.0, "n_games": 500}}
        fast = merge(prev, {"open.star": 1.0}, {}, half_life=5)["open.star"]["value"]
        slow = merge(prev, {"open.star": 1.0}, {}, half_life=50)["open.star"]["value"]
        self.assertAlmostEqual(fast, steady_alpha(5))
        self.assertAlmostEqual(slow, steady_alpha(50))
        self.assertGreater(fast, slow)

    def test_merge_accepts_flat_prev_and_none_x(self):
        # v3.9.0 口径收口：``x`` 为 ``None``（＝一维都没有）⇒ 已有的维**原样带过**，不衰减
        got = merge({"open.star": {"value": 0.5, "n_games": 4}}, None, 1)
        self.assertAlmostEqual(got["open.star"]["value"], 0.5)
        self.assertEqual(got["open.star"]["n_games"], 4)
        # 扁平 prev（``{axis: 值}``，**没有**样本数）同样原样带过——计数保持 0（无从得知）
        flat = merge({"open.star": 0.5}, None, 1)
        self.assertAlmostEqual(flat["open.star"]["value"], 0.5)
        self.assertEqual(flat["open.star"]["n_games"], 0)
        self.assertEqual(merge({}, {}, 0), {})
        self.assertEqual(merge(None, None, None), {})
        self.assertEqual(merge("脏", {"open.star": 1.0}, {})["open.star"]["value"], 1.0)

    def test_axis_constants_are_consistent(self):
        self.assertEqual(set(ALWAYS_AXES) | set(DROP_AXES), set(CORE_AXES))
        self.assertTrue(set(ALWAYS_AXES).isdisjoint(set(DROP_AXES)))
        self.assertIn(BLUNDER_AXIS, DROP_AXES)
        self.assertTrue(set(WEAK_AXES) <= set(DROP_AXES))
        # M-2：本批口径下只支持 3 类手型（「随手」需要时间信号源，词表里不留死枚举）
        self.assertEqual(BLUNDER_TYPES, ("深入", "无理", "方向"))
        self.assertNotIn("随手", BLUNDER_TYPES)


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
                BLUNDER_AXIS: {"value": 0.2, "n_games": 2},
                TOPIC_PREFIX + "定式": {"value": 0.5, "n_games": 2}}
        x = {"open.star": 0.9, TOP_TYPE_AXIS: "方向", TOPIC_PREFIX + "定式": 0.2,
             "style.tenuki": 0.1}
        prev_before, x_before = copy.deepcopy(prev), copy.deepcopy(x)
        first = merge(prev, x, {})
        second = merge(prev, x, {})
        self.assertEqual(first, second)
        self.assertEqual(prev, prev_before, "不得改 prev")
        self.assertEqual(x, x_before, "不得改 x")
        self.assertEqual(list(first), sorted(first), "输出键顺序确定（可复现）")
        self.assertAlmostEqual(first[BLUNDER_AXIS]["value"], 0.2, msg="没信号就带过")

    def test_merge_is_associative_in_the_expected_way(self):
        """逐局融合的结果与「一局一局来」一致（同一条记录融两次＝按公式算两次）。"""
        x = {"open.star": 0.8}
        a = merge({}, x, 0)
        b = merge(a, x, {})
        self.assertAlmostEqual(b["open.star"]["value"], 0.8)
        self.assertEqual(b["open.star"]["n_games"], 2)


class PipelineTest(unittest.TestCase):
    """端到端：记录 → 提取 → 融合 → 落盘 → 读回（Q3 的写入点就是这么用的）。"""

    def test_records_to_profile_db_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(os.path.join(d, "profile.db"))
            recs = [
                {"ts": 100.0, "moves": [_mv("B", "Q16"), _mv("W", "Q4"), _mv("B", "D16", 0.4)],
                 "joseki_families": {"star_kakari_small": 1}, "chat_topics": [["定式", "x"]]},
                {"ts": 200.0, "moves": _seq(("B", "Q16"), ("W", "Q4"), ("B", "R17")),
                 "chat_topics": [["定式", "y"], ["闲聊", "z"]]},
            ]
            for rec in recs:
                self.assertGreater(store.add_game(rec), 0)
            profile = store.get_profile()
            # ``games()`` 是**最新在前**；融合要按时间正序（旧 → 新），所以调用方 ``reversed()``
            for rec in reversed(store.games()):
                profile = merge(profile, extract_axes(rec), {})
            self.assertEqual(store.upsert_axes(profile), len(profile))
            back = store.get_profile()
            self.assertEqual(set(back), set(profile))
            self.assertEqual(back["open.star"]["n_games"], 2)
            self.assertAlmostEqual(back["open.star"]["value"], 1.0, places=6)
            self.assertAlmostEqual(back["persona.topic.定式"]["value"], 0.875)
            self.assertEqual(back["persona.topic.定式"]["n_games"], 2)
            # 第一局带 drop、第二局没有：失误维**只被第一局更新过一次**（第二局原样带过）
            self.assertAlmostEqual(back[BLUNDER_AXIS]["value"], 1 / 2)   # 1 真亏 / 2 手玩家手
            self.assertEqual(back[BLUNDER_AXIS]["n_games"], 1)
            self.assertEqual(back[TOP_TYPE_AXIS]["value"], "方向")
            self.assertAlmostEqual(back["weak.corner_tl"]["value"], 1.0)
            # 幂等：同一批记录再融一次，值按公式再走一步（可复现），不是随机
            again = merge(back, extract_axes(store.games()[0]), {})
            self.assertAlmostEqual(again["open.star"]["n_games"], 3)


if __name__ == "__main__":
    unittest.main()
