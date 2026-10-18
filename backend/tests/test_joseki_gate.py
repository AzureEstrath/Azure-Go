"""定式提示门控（计划 07 · Task 5 / v3.7.0）：**资格层 + 频率层**。

11 个用例名逐字来自任务书 Step 1（``test_rate_zero_never_shows`` … ``test_undo_returns_the_count``）；
其余是接线用例（``player_ranks`` 局内信号、冷却经 agent 生效、``/api/config`` 透传）。

口径（全篇最重要的简化）：
- 资格层：族计数 ``< 3`` 有资格；``>= 3`` 只在「走错」（真亏且偏离库线）时有资格；``joseki_teach=false``
  一律无资格；
- 频率层：有资格时掷一次 ``p = clamp(rate + skill_adj + mood_adj, 0, 1)``；**未命中与「没识别到定式」
  逐字段同构**（只点评这一手）；
- 走错 → 必讲（绕过频率层与冷却）；结构点（进入 / 分岔 / 收尾）→ 走频率层；中间推进识别器自己沉默。

随机数一律**注入**（``joseki_gate`` 的纯函数拿 ``rng``；agent 侧持默认 rng，可注入替身），
所以本文件一条断言都不依赖真随机。
"""

import asyncio
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/（app.*）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # tests/（helpers）

from app import coords, events, joseki_gate, prompts
from app.agent import AzureAgent
from app.agent_graph import PHASE_AFTER_PLAYER
from app.joseki import JosekiRecognizer
from app.joseki_progress import JosekiProgress
from helpers import StubEngine, StubLLM, mk_agent


# ================= 替身 =================

class _FixedRng:
    """固定随机数的 rng 替身：注入它就等于「这一次掷出多少」完全可控。"""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def random(self):
        self.calls += 1
        return self.value


class _FakeProgress:
    """族计数替身：只记账、不碰磁盘（契约与 ``JosekiProgress`` 的四个方法一致）。"""

    def __init__(self, seen_counts=None):
        self.seen_counts = dict(seen_counts or {})
        self.bumps = []
        self.unbumps = []

    def seen(self, family):
        return int(self.seen_counts.get(family, 0))

    def bump(self, family):
        self.seen_counts[family] = self.seen(family) + 1
        self.bumps.append(family)
        return self.seen_counts[family]

    def unbump(self, family):
        self.seen_counts[family] = max(0, self.seen(family) - 1)
        self.unbumps.append(family)
        return self.seen_counts[family]

    def forget_all(self):
        n = len(self.seen_counts)
        self.seen_counts.clear()
        return n


def _seed(agent, seq):
    """把一串 ``[color, gtp]`` 直接铺进棋盘与棋谱（绕过编排，好造出确定的角部手顺）。"""
    for color, gtp in seq:
        x, y = coords.gtp_to_xy(gtp)
        agent.st.board.play(x, y, "black" if color == "B" else "white")
        agent.st.add_move(color, gtp)


def _agent(progress=None, **kw):
    return AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(),
                      joseki_progress=progress, **kw)


# ================= ① 纯函数层（注入 rng） =================

class GatePureTest(unittest.TestCase):
    """``joseki_gate`` 的纯函数：资格层 / 频率层 / 冷却 / 画像 / 情绪。"""

    def test_rate_zero_never_shows(self):
        """p=0 → 必不说（走错除外）。"""
        rng = _FixedRng(0.0)                     # 就算掷出 0.0，p=0 也不该讲
        for _ in range(10):
            self.assertFalse(joseki_gate.should_show(
                rng=rng, rate=0.0, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
                mistake=False, teach_on=True, last_taught_move=None, move_no=3))
        self.assertEqual(rng.calls, 0, "p=0 连掷都不该掷（确定性）")
        # 走错例外：p=0 也必讲
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=0.0, skill_adj=0.0, mood_adj=0.0, seen=0, kind="deviated",
            mistake=True, teach_on=True, last_taught_move=None, move_no=3))

    def test_rate_one_always_shows(self):
        """p=1 → 必说（不问 rng）。"""
        rng = _FixedRng(0.999)                   # 掷得再大也压不过 p=1
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=1.0, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
            mistake=False, teach_on=True, last_taught_move=None, move_no=5))
        # 族计数已到上限（只有走错才有资格）：走错照样必说
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=1.0, skill_adj=0.0, mood_adj=0.0, seen=9, kind="deviated",
            mistake=True, teach_on=True, last_taught_move=None, move_no=5))
        self.assertEqual(rng.calls, 0, "p=1 连掷都不该掷（确定性）")

    def test_adj_clamped_to_plus_minus_10pct(self):
        """skill_adj / mood_adj 各自截断到 ±0.10（原始值可以超界，返回的必须不超）。"""
        self.assertEqual(joseki_gate.skill_adj([-1, -1, -1, -1], 0), 0.10)    # 生疏 + 没见过
        self.assertEqual(joseki_gate.skill_adj([0, 1, 2, 3], 9), -0.10)       # 熟练 + 见得多
        self.assertEqual(joseki_gate.mood_adj({"name": "兴奋"}), 0.10)        # 积极 · 好奇
        self.assertEqual(joseki_gate.mood_adj({"name": "难过"}), -0.10)       # 消极 · 平常
        for ranks in ([0, 1, -1], [-1], [3, 4], [], None, "oops", [None, "x"]):
            with self.subTest(ranks=ranks):
                self.assertLessEqual(abs(joseki_gate.skill_adj(ranks, 2)), 0.10 + 1e-9)
        for mood in ({"name": "好奇"}, {"name": "惊慌"}, {"name": "平静"}, {}, None, "x", 5):
            with self.subTest(mood=mood):
                self.assertLessEqual(abs(joseki_gate.mood_adj(mood)), 0.10 + 1e-9)

    def test_rng_is_reproducible(self):
        """注入固定 rng → 决策可复现（同种子同结果；同一次掷出的值决定这一次的结果）。"""
        def run(seed):
            rng = random.Random(seed)
            return [joseki_gate.should_show(
                rng=rng, rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
                mistake=False, teach_on=True, last_taught_move=None, move_no=i + 1)
                for i in range(16)]

        self.assertEqual(run(2026), run(2026), "同种子必须给出同一串决策")
        # rng 真的参与决策：0.1 < 0.5 命中、0.9 > 0.5 未命中（与种子无关，逐条确定）
        low = joseki_gate.should_show(
            rng=_FixedRng(0.1), rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
            mistake=False, teach_on=True, last_taught_move=None, move_no=1)
        high = joseki_gate.should_show(
            rng=_FixedRng(0.9), rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
            mistake=False, teach_on=True, last_taught_move=None, move_no=1)
        self.assertTrue(low)
        self.assertFalse(high)

    def test_cooldown_suppresses_repeat(self):
        """同族 N 手内不重复讲（窗口外恢复；走错绕过冷却）。"""
        rng = _FixedRng(0.0)                     # p=0.5 下掷 0.0 ⇒ 必命中
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
            mistake=False, teach_on=True, last_taught_move=None, move_no=2))
        for move_no in range(2, 10):             # 第 2 手讲过：第 2~9 手都在窗口内
            with self.subTest(move_no=move_no):
                self.assertFalse(joseki_gate.should_show(
                    rng=rng, rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
                    mistake=False, teach_on=True, last_taught_move=2, move_no=move_no))
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="entered",
            mistake=False, teach_on=True, last_taught_move=2, move_no=10), "窗口外恢复")
        # 走错绕过冷却
        self.assertTrue(joseki_gate.should_show(
            rng=rng, rate=0.5, skill_adj=0.0, mood_adj=0.0, seen=0, kind="deviated",
            mistake=True, teach_on=True, last_taught_move=9, move_no=9))

    def test_mistake_bypasses_frequency(self):
        """走错 → 必讲：rate=0 + 冷却窗口内 + 族计数超上限，照样讲。"""
        self.assertTrue(joseki_gate.should_show(
            rng=_FixedRng(0.99), rate=0.0, skill_adj=0.0, mood_adj=0.0, seen=99,
            kind="deviated", mistake=True, teach_on=True, last_taught_move=5, move_no=5))
        self.assertFalse(joseki_gate.should_show(
            rng=_FixedRng(0.99), rate=0.0, skill_adj=0.0, mood_adj=0.0, seen=99,
            kind="deviated", mistake=False, teach_on=True, last_taught_move=5, move_no=5))

    def test_missing_profile_or_mood_degrades(self):
        """画像 / 情绪缺失（或脏）→ adj=0，不抛；概率算式也照样出得来。"""
        self.assertEqual(joseki_gate.skill_adj([], 3), 0.0)
        self.assertEqual(joseki_gate.skill_adj(None, 0), 0.0)
        self.assertEqual(joseki_gate.mood_adj({}), 0.0)
        self.assertEqual(joseki_gate.mood_adj(None), 0.0)
        for bad in ("oops", 5, [None, "x"]):
            self.assertEqual(joseki_gate.skill_adj(bad, 1), 0.0)
        for bad in ("oops", 5, {"name": None}, {"v": "x"}, [1, 2]):
            self.assertEqual(joseki_gate.mood_adj(bad), 0.0)
        self.assertEqual(joseki_gate.probability("x", 0.0, 0.0), 0.0)
        self.assertEqual(joseki_gate.probability(-1.0, 0.0, 0.0), 0.0)
        self.assertEqual(joseki_gate.probability(2.0, 0.0, 0.0), 1.0)
        self.assertEqual(joseki_gate.probability(0.5, 0.10, -0.10), 0.5)
        self.assertEqual(joseki_gate.probability(0.45, 0.10, 0.10), 0.65)

    def test_teach_off_is_silent(self):
        """``joseki_teach=false`` → 资格层一律不放行（也无 joseki / 讲解 / 卡片）。"""
        for kind in ("entered", "branch", "formed", "deviated"):
            with self.subTest(kind=kind):
                self.assertFalse(joseki_gate.eligible(0, kind, True, False))
                self.assertFalse(joseki_gate.should_show(
                    rng=_FixedRng(0.0), rate=1.0, skill_adj=0.0, mood_adj=0.0, seen=0,
                    kind=kind, mistake=True, teach_on=False, last_taught_move=None, move_no=1))

        agent, _eng, llm, bus = mk_agent()
        agent.joseki_teach = False
        _seed(agent, [["W", "D17"]])
        agent._new_turn()
        state = asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": 5, "y": 2}))

        self.assertEqual(state["data"]["jk"], {}, "总开关关掉：识别器自己不出结果")
        self.assertNotIn("jk_show_card", state["data"], "门控连问都不问")
        self.assertEqual(bus.of_type("joseki"), [], "不发 joseki 事件")
        self.assertEqual(len(bus.messages()), 1, "仍有一条普通点评")
        self.assertIn("用你自己的口吻评", _phase_text(llm), "走的是普通点评，不是讲解模板")


# ================= ② 接线层（agent + 图） =================

def _phase_text(llm):
    """模型收到的**阶段提示词**那一份（经 ``with_context`` 装配的才带「【现在】」）。"""
    for msgs, _mx, _nt in llm.calls:
        for m in msgs:
            if str(m.get("role", "")) == "user" and "【现在】" in str(m.get("content", "")):
                return str(m["content"])
    return ""


class GateWiringTest(unittest.TestCase):
    """门控接进 ``after_player``：命中才讲 / 上卡片 / 发事件；未命中＝没识别到定式。"""

    # 角内先铺 1 手（白小目 D17），玩家再落 F17(5,2) ⇒ ``entered``（小目一间低挂只有一条 3 手线）
    ENTER_SEED = [["W", "D17"]]

    def _after_player(self, agent, x=5, y=2):
        agent._new_turn()
        return asyncio.run(agent._turn(PHASE_AFTER_PLAYER, {"x": x, "y": y}))

    def test_miss_is_identical_to_no_match(self):
        """未命中（门控不放行）与「识别器没出声」：**事件集合逐项相等**（同一出口）。"""
        def run(gate_miss):
            agent, _eng, llm, bus = mk_agent()
            # 0.0 ⇒ 必未命中；1.0 ⇒ 必命中。rng 也固定（画像 / 情绪给的正负浮动都压不过确定性）
            agent.joseki_rate = 0.0 if gate_miss else 1.0
            agent.joseki_rng = _FixedRng(0.999)
            _seed(agent, self.ENTER_SEED)
            if gate_miss:                                     # 门控未命中：识别结果在，只是不讲
                state = self._after_player(agent)
            else:                                             # 识别器沉默：压根没有识别结果
                with mock.patch.object(JosekiRecognizer, "scan",
                                       staticmethod(lambda moves: {})):
                    state = self._after_player(agent)
            return agent, llm, bus, state

        a_agent, a_llm, a_bus, a_state = run(True)
        b_agent, b_llm, b_bus, b_state = run(False)

        self.assertEqual(str(a_state["data"]["jk"]["kind"]), "entered",
                         "门控未命中：识别结果仍在 data 里，只是不讲")
        self.assertIs(a_state["data"]["jk_show_card"], False)
        self.assertEqual(b_state["data"]["jk"], {})

        self.assertEqual([str(e["type"]) for e in a_bus.events],
                         [str(e["type"]) for e in b_bus.events], "事件种类逐项相等")
        self.assertEqual(a_bus.of_type("joseki"), [])
        self.assertEqual(b_bus.of_type("joseki"), [])
        self.assertEqual([e for e in a_bus.messages()], [e for e in b_bus.messages()],
                         "台词逐字段相等（同一出口、同一模板）")
        self.assertEqual(_phase_text(a_llm), _phase_text(b_llm), "提示词也同构")

    def test_hit_teaches_emits_and_shows_the_card(self):
        """命中（未接线 ⇒ 有资格就讲）：讲解模板 + 卡片 + ``joseki`` 事件，一条不多一条不少。"""
        agent, _eng, llm, bus = mk_agent()
        _seed(agent, self.ENTER_SEED)
        state = self._after_player(agent)
        jk = state["data"]["jk"]
        self.assertEqual(str(jk["kind"]), "entered")
        self.assertIs(state["data"]["jk_show_card"], True)
        self.assertEqual(len(bus.of_type("joseki")), 1)
        self.assertIs(bus.of_type("joseki")[-1]["show_card"], True)
        self.assertIn(prompts.joseki_enter_prompt(str(jk["name"]), str(jk["key"]), str(jk["corner"])),
                      _phase_text(llm))
        self.assertNotIn("用你自己的口吻评", _phase_text(llm), "顶替：普通点评一个字都不在")

    def test_player_ranks_are_recorded_in_game_and_roll_back(self):
        """裁决 1：棋力画像只用**局内信号**——``player_ranks`` 每手纯追加、随快照回滚、不进 payload。"""
        agent, _eng, _llm, _bus = mk_agent()
        _seed(agent, self.ENTER_SEED)
        self.assertNotIn("player_ranks", agent.st.payload(), "纯局内信号：不进对外快照 payload")
        self.assertEqual(agent.st.make_snapshot()["player_ranks"], [], "快照带上它（供回滚）")
        self._after_player(agent)
        self.assertEqual(len(agent.st.player_ranks), 1, "本局玩家每一手记一个 rank")
        self.assertIn(agent.st.player_ranks[0], (-1, 0, 1, 2, 3, 4))
        agent.undo(0)
        self.assertEqual(agent.st.player_ranks, [], "悔棋：随快照回到当时的样子")

    def test_gate_records_the_cooldown_after_a_hit(self):
        """命中要记「这一族最近讲过的手数」：窗口内同族不再重复讲，走出窗口恢复。"""
        agent, _eng, _llm, _bus = mk_agent()
        agent.joseki_rate = 1.0                              # 接上频率层（未接线时没有冷却）
        agent.joseki_rng = _FixedRng(0.0)                    # 画像 / 情绪的浮动也压不过确定性
        jk = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
        self.assertTrue(asyncio.run(agent.joseki_gate_show(jk, None)))
        agent.st.add_move("B", "pass")
        agent.st.add_move("W", "pass")
        self.assertFalse(asyncio.run(agent.joseki_gate_show(jk, None)), "冷却窗口内：按未命中处理")
        for _ in range(8):
            agent.st.add_move("B", "pass")
        self.assertTrue(asyncio.run(agent.joseki_gate_show(jk, None)), "走出窗口：恢复")

    def test_unwired_rate_keeps_the_old_behaviour(self):
        """未接线 ``joseki_rate``（直接构造 agent / 单测）⇒ 没有频率层：有资格就讲、也不进冷却。"""
        agent, _eng, _llm, _bus = mk_agent()
        self.assertIsNone(agent.joseki_rate, "未接线：保持 None（不是 0.5）")
        jk = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
        for _ in range(3):
            self.assertTrue(asyncio.run(agent.joseki_gate_show(jk, None)), "连着问也照样讲")
        self.assertEqual(agent._joseki_taught, {}, "未接线不记冷却起点")
        # 资格层照常把关：这一族已教过 3 次（走错除外）
        agent.joseki_progress = _FakeProgress({"star_33": 3})
        self.assertFalse(asyncio.run(agent.joseki_gate_show(jk, None)))

    def test_gate_only_decides_text_and_card(self):
        """门控不改 ``busy``、不改事件配对、不改状态机：未命中时棋子照落、谱照走。"""
        agent, _eng, _llm, bus = mk_agent()
        agent.joseki_rate = 0.0
        agent.joseki_rng = _FixedRng(0.999)      # 画像给的 +10% 浮动也压不过确定性
        _seed(agent, self.ENTER_SEED)
        before = len(agent.st.moves)
        state = self._after_player(agent)
        self.assertEqual(len(agent.st.moves), before + 1, "玩家那一手照常入谱")
        self.assertEqual(agent.st.board.grid[5][2], "black", "棋子照常落到棋盘上")
        self.assertIs(agent.busy, False, "门控不碰 busy")
        self.assertEqual([e for e in bus.of_type("speech")], [], "未接线 TTS：一条 speech 都不发")
        self.assertEqual(state["data"]["jk_mistake"]["gtp"], "F17", "走错判定照常算出来")


class CountEntryTest(unittest.TestCase):
    """族计数的两条硬口径（与 ``test_joseki_progress`` 同源，这里按任务书用例名各钉一条）。"""

    def test_same_family_counts_once_per_game(self):
        """同一局第二次进同族，计数不再 +1。"""
        fake = _FakeProgress()
        agent = _agent(fake)
        jk = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
        asyncio.run(agent.joseki_count_entry(jk))
        asyncio.run(agent.joseki_count_entry(jk))
        self.assertEqual(fake.bumps, ["star_33"])
        self.assertEqual(fake.seen("star_33"), 1)
        self.assertEqual(agent.st.joseki_counted, ["star_33"])

    def test_undo_returns_the_count(self):
        """bump 后 undo → seen 回原值（快照之前的那一次不动）。"""
        fake = _FakeProgress()
        agent = _agent(fake)
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "star_33"}))
        self.assertEqual(fake.seen("star_33"), 1)
        agent.st.snapshots.append(agent.st.make_snapshot())     # 快照＝这一手之前
        agent.st.add_move("B", "D16")
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "san_san"}))
        self.assertEqual(fake.seen("san_san"), 1)

        agent.undo(0)
        self.assertEqual(fake.seen("san_san"), 0, "这一手计的那一次退回")
        self.assertEqual(fake.seen("star_33"), 1, "快照之前的族不动")
        self.assertEqual(agent.st.joseki_dropped, ["san_san"])


# ================= ③ 真库端到端 + 配置透传 =================

class RealStoreGateTest(unittest.TestCase):
    """真 ``JosekiProgress``：资格层的跨局口径（前 3 次有资格、第 4 次起只看走错）。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.progress = JosekiProgress(os.path.join(self.dir.name, "joseki_progress.db"))

    def tearDown(self):
        self.dir.cleanup()

    def test_fourth_entry_is_not_eligible_without_a_mistake(self):
        jk = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
        for i in range(3):
            agent = _agent(self.progress)
            with self.subTest(round=i + 1):
                self.assertTrue(asyncio.run(agent.joseki_show_card(jk)), "前 3 次有资格")
                asyncio.run(agent.joseki_count_entry(jk))
        self.assertEqual(self.progress.seen("star_33"), 3)
        agent = _agent(self.progress)
        self.assertFalse(asyncio.run(agent.joseki_show_card(jk)), "第 4 次进入：资格层不放行")
        self.assertFalse(asyncio.run(agent.joseki_gate_show(jk, None)), "连频率层都到不了")


class ConfigPassthroughTest(unittest.TestCase):
    """``/api/config`` 透传 ``joseki_rate``（与 ``joseki_teach`` 同一条通道）。"""

    def test_api_config_sets_joseki_rate(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        agent, _eng, _llm, _bus = mk_agent()
        app = create_app(agent=agent, autostart=False)
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/config", json={"joseki_rate": 0.25}).status_code, 200)
        self.assertAlmostEqual(agent.joseki_rate, 0.25)

    def test_dirty_or_out_of_range_rate_is_clamped(self):
        from app.config import AzureConfig
        cfg = AzureConfig()
        self.assertEqual(cfg.joseki_rate, 0.5, "字段默认值 0.5")
        cfg._apply({"joseki_rate": 7})
        self.assertEqual(cfg.joseki_rate, 1.0)
        cfg._apply({"joseki_rate": -3})
        self.assertEqual(cfg.joseki_rate, 0.0)
        cfg._apply({"joseki_rate": "oops"})
        self.assertEqual(cfg.joseki_rate, 0.5, "脏值退回默认：不因一个旋钮把启动搞崩")


if __name__ == "__main__":
    unittest.main()
