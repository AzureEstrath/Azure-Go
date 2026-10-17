"""JosekiProgress：族计数跨局持久化、「任何存储故障只当第 1 次」的降级，
以及 T5 的入围规则接线（前 3 次都上卡片、第 4 次起只在走错时上；同局同族只计一次、悔棋退回）。
"""

import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/（app.*）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # tests/（helpers）

from fastapi.testclient import TestClient

from app import coords, events
from app.agent import AzureAgent
from app.joseki import JosekiRecognizer
from app.joseki_progress import DEFAULT_DB_PATH, JosekiProgress
from helpers import StubEngine, StubLLM, mk_agent


class JosekiProgressTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # 库路径本身先落成一个**文件**：下面「不可建库的路径」要拿它当父目录，
        # 父目录是个文件，`os.makedirs` 与 `sqlite3.connect` 才会真的失败。
        self.db = os.path.join(self.dir.name, "joseki_progress.db")
        open(self.db, "w").close()

    def tearDown(self):
        self.dir.cleanup()

    def test_first_time_is_zero_then_one(self):
        p = JosekiProgress(self.db)
        self.assertEqual(p.seen("star_33"), 0)
        self.assertEqual(p.bump("star_33"), 1)
        self.assertEqual(p.seen("star_33"), 1)

    def test_persists_across_instances(self):
        JosekiProgress(self.db).bump("star_33")
        self.assertEqual(JosekiProgress(self.db).seen("star_33"), 1)

    def test_unbump_never_goes_negative(self):
        p = JosekiProgress(self.db)
        p.bump("star_33")
        self.assertEqual(p.unbump("star_33"), 0)
        self.assertEqual(p.unbump("star_33"), 0)

    def test_readonly_dir_degrades_silently(self):
        p = JosekiProgress(os.path.join(self.db, "nope", "x.db"))   # 不可建库的路径
        self.assertEqual(p.seen("star_33"), 0)
        self.assertEqual(p.bump("star_33"), 0)

    # —— 额外边界用例（计划建议的 1~2 条） ——

    def test_unbump_without_row_stays_zero(self):
        """没有记录时 ``unbump`` 返回 0，且**不落 0 值行**（只动 count，不新建行）。"""
        p = JosekiProgress(self.db)
        self.assertEqual(p.unbump("never_seen"), 0)
        self.assertEqual(p.seen("never_seen"), 0)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT count FROM joseki_seen WHERE family = ?", ("never_seen",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_forget_all_counts_clears_and_degrades(self):
        p = JosekiProgress(self.db)
        p.bump("star_33")
        p.bump("star_33")
        p.bump("san_san")
        self.assertEqual(p.forget_all(), 2)                  # 清掉 2 个族（不是 3 次出现）
        self.assertEqual(p.seen("star_33"), 0)
        broken = JosekiProgress(os.path.join(self.db, "nope", "x.db"))
        self.assertEqual(broken.forget_all(), 0)             # 存储故障：0 且不抛

    def test_degradation_prints_once_per_reason_per_instance(self):
        """库一直坏着也**不得刷屏**：同类原因每个实例只打一行，返回值照旧每次降级。"""
        bad = os.path.join(self.db, "nope", "x.db")
        p = JosekiProgress(bad)                              # 第一实例：三种原因各允许一行
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            values = [p.seen("star_33"), p.seen("star_33"), p.seen("star_33")]
            values += [p.bump("star_33"), p.bump("star_33")]
            values.append(p.forget_all())
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(values, [0, 0, 0, 0, 0, 0])         # 每次调用都照降级值返回，不因节流而变
        self.assertEqual(len(lines), 3, "读取 / 写入 / 清空 各只许一行：%r" % lines)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            p.seen("star_33")                                # 同实例再来：静默
            JosekiProgress(bad).seen("star_33")              # 新实例：重新允许一行
        lines2 = [ln for ln in buf2.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines2), 1, "新实例应各有一行额度：%r" % lines2)


# ================= T5：卡片门禁（前 3 次都上卡片，第 4 次起只在走错时上） =================

class FakeProgress:
    """假的族计数替身：实现 ``seen`` / ``bump`` / ``unbump`` / ``forget_all`` 四个契约方法。

    只记账、不碰磁盘（真库那一条端到端在 ``RealCountTest`` 里用临时库跑）。
    """

    def __init__(self, seen_counts=None):
        self.seen_counts = dict(seen_counts or {})
        self.bumps = []
        self.unbumps = []

    def seen(self, family):
        return int(self.seen_counts.get(family, 0))

    def bump(self, family):
        # 顺序与 unbump 一致：**先改计数、最后**才记「这次调用发生了」——`bumps` / `unbumps` 是
        # 「已经改完」的信号，观测者（用例轮询）一旦看到它，状态必须已经是最终值（R2）。
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


class _ProbeList(list):
    """列表探针：被 ``append`` 的那一刻立刻执行回调（＝「轮询看到信号后的第一眼」）。"""

    def __init__(self, probe):
        super().__init__()
        self._probe = probe

    def append(self, item):
        super().append(item)
        self._probe()


def _agent(progress=None):
    """stub 装配的 agent（假引擎 / 假 LLM / 记录总线）。"""
    return AzureAgent(StubEngine(), StubLLM(), events.RecordingBus(), joseki_progress=progress)


def _show_card(agent, jk, mistake=None):
    return asyncio.run(agent.joseki_show_card(jk, mistake))


def _seed(agent, seq):
    """把一串 ``[color, gtp]`` 直接铺进棋盘与棋谱（绕过编排，好造出确定的角部手顺）。"""
    for color, gtp in seq:
        x, y = coords.gtp_to_xy(gtp)
        agent.st.board.play(x, y, "black" if color == "B" else "white")
        agent.st.add_move(color, gtp)


class CardGateTest(unittest.TestCase):
    """``joseki_show_card`` 的判定表（只读，绝不写状态）。"""

    _ENTERED = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
    _DEVIATED = {"kind": "deviated", "family": "star_33", "corner": "左上", "cat": "star"}
    _MISTAKE = {"is_mistake": True, "on_line": False, "gtp": "B15", "x": 1, "y": 4,
                "drop": 0.20, "rank_ok": True}

    def test_first_three_always_show_card(self):
        """意图 1：``seen`` 为 0 / 1 / 2 时，进入（entered）一律上卡片。"""
        for seen in (0, 1, 2):
            with self.subTest(seen=seen):
                agent = _agent(FakeProgress({"star_33": seen}))
                self.assertTrue(_show_card(agent, self._ENTERED))
                self.assertEqual(agent.st.joseki_counted, [], "判定不许写状态")

    def test_from_fourth_only_a_real_mistake_shows_card(self):
        """意图 2：``seen`` 到 3 之后——进入不上；偏离要「真亏」才上。"""
        agent = _agent(FakeProgress({"star_33": 3}))
        self.assertFalse(_show_card(agent, self._ENTERED))
        # 偏离 + 真亏（掉 >10% 或不在她算的前 5）⇒ 上卡片
        self.assertTrue(_show_card(agent, self._DEVIATED, self._MISTAKE))
        # 偏离但不亏（掉 1%、排名在前 5）⇒ 不上卡片
        self.assertFalse(_show_card(agent, self._DEVIATED,
                                    {"on_line": False, "drop": 0.01, "rank_ok": True}))
        # 照书下（库线那一手）永远不判错 ⇒ 不上卡片（``is_joseki_mistake`` 的第一条护栏）
        self.assertFalse(_show_card(agent, self._DEVIATED,
                                    {"on_line": True, "drop": 0.5, "rank_ok": False}))
        # 没有走错结论（缺 ``jk_mistake``）⇒ 不上卡片
        self.assertFalse(_show_card(agent, self._DEVIATED, None))
        # 成型 / 中段不是走错的形态
        self.assertFalse(_show_card(agent, {"kind": "formed", "family": "star_33"}))
        self.assertFalse(_show_card(agent, {"kind": "branch", "family": "star_33"}))

    def test_dirty_mistake_dict_degrades_to_no_mistake(self):
        """R3：``mistake`` 形状脏（``drop`` 不是数字）⇒ 不许抛，按「不算走错」保守处理 + 一行日志。"""
        agent = _agent(FakeProgress({"star_33": 3}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            card = _show_card(agent, self._DEVIATED,
                              {"on_line": False, "drop": "abc", "rank_ok": True})
        self.assertFalse(card)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "降级只留一行日志：%r" % lines)
        self.assertIn("[Joseki]", lines[0])

    def test_no_store_means_always_teach(self):
        """意图 5：``joseki_progress is None`` ⇒ 一律 True（降级＝每次都教）。"""
        agent = _agent(None)
        for kind in ("entered", "branch", "formed", "deviated"):
            with self.subTest(kind=kind):
                self.assertTrue(_show_card(agent, {"kind": kind, "family": "star_33"},
                                           self._MISTAKE))
        self.assertFalse(_show_card(agent, {}), "没识别结果：不上卡片")

    def test_missing_family_is_conservative(self):
        """族 id 缺失（旧库 / 异常）⇒ 保守地 True：宁可多教一次，也不漏教。"""
        agent = _agent(FakeProgress({"star_33": 9}))
        self.assertTrue(_show_card(agent, {"kind": "entered"}))

    def test_dirty_seen_value_counts_as_first_time(self):
        """替身 / 将来实现给了脏值（``None`` / 非数字）⇒ 当成第 1 次（fail-open）。"""

        class _DirtyProgress(FakeProgress):
            def seen(self, family):
                return None

        agent = _agent(_DirtyProgress({"star_33": 9}))
        self.assertTrue(_show_card(agent, self._DEVIATED, None))

    def test_broken_store_degrades_to_first_time(self):
        """真的去碰一个开不出来的库：读不出次数 ⇒ 当成第 1 次，照样上卡片、只留一行日志。"""
        with tempfile.TemporaryDirectory() as d:
            blocked = os.path.join(d, "afile")                    # 先做成一个**文件**
            open(blocked, "w").close()
            broken = JosekiProgress(os.path.join(blocked, "nope", "p.db"))
            agent = _agent(broken)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                card = _show_card(agent, self._DEVIATED, self._MISTAKE)
                again = _show_card(agent, self._DEVIATED, self._MISTAKE)
            self.assertTrue(card)
            self.assertTrue(again)
            self.assertEqual(broken.seen("star_33"), 0)
            self.assertEqual(len(buf.getvalue().splitlines()), 1, "降级日志每实例每原因只许一行")


class CountEntryTest(unittest.TestCase):
    """``joseki_count_entry`` 的写入口径（同局同族一次）与 ``undo`` 的退回路径。"""

    def test_same_family_counts_once_per_game(self):
        """意图 3：同一局里两次进入同族 ⇒ 只 ``bump`` 一次。"""
        fake = FakeProgress()
        agent = _agent(fake)
        jk = {"kind": "entered", "family": "star_33"}
        asyncio.run(agent.joseki_count_entry(jk))
        asyncio.run(agent.joseki_count_entry(jk))
        self.assertEqual(fake.bumps, ["star_33"])
        self.assertEqual(fake.seen("star_33"), 1)
        self.assertEqual(agent.st.joseki_counted, ["star_33"])

    def test_counting_skipped_without_store_family_or_result(self):
        fake = FakeProgress()
        agent = _agent(fake)
        asyncio.run(agent.joseki_count_entry({}))
        asyncio.run(agent.joseki_count_entry({"kind": "entered"}))       # 无 family
        self.assertEqual(fake.bumps, [])
        self.assertEqual(agent.st.joseki_counted, [])
        off = _agent(None)
        asyncio.run(off.joseki_count_entry({"kind": "entered", "family": "star_33"}))
        self.assertEqual(off.st.joseki_counted, [])

    def test_recording_again_clears_the_dropped_mark(self):
        """新的一集开始：重计某族时把它从 ``joseki_dropped`` 里摘掉。"""
        fake = FakeProgress()
        agent = _agent(fake)
        agent.st.joseki_dropped = ["star_33"]
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "star_33"}))
        self.assertEqual(agent.st.joseki_dropped, [])
        self.assertEqual(fake.bumps, ["star_33"])

    def test_undo_rolls_back_both_fields_and_the_count(self):
        """意图 4 + 7：快照之前的族不退，这一手计的那一次退回去并记档（无 loop ⇒ 就地同步）。"""
        fake = FakeProgress()
        agent = _agent(fake)
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "san_san"}))
        agent.st.snapshots.append(agent.st.make_snapshot())      # 快照＝这一手之前
        agent.st.add_move("B", "D16")
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "star_33"}))
        self.assertEqual(agent.st.joseki_counted, ["san_san", "star_33"])

        agent.undo(0)                                            # asyncio.run 之外：确定性退回
        self.assertEqual(agent.st.joseki_counted, ["san_san"], "应与快照一致")
        self.assertEqual(agent.st.joseki_dropped, ["star_33"], "被退回的族要记档")
        self.assertEqual(fake.unbumps, ["star_33"])
        self.assertEqual(fake.seen("star_33"), 0)
        self.assertEqual(fake.seen("san_san"), 1, "快照之前的族不该被退")

    def test_unbump_signal_comes_after_the_state_change(self):
        """R2：``unbumps`` 是「这一族已经退完」的信号——信号出现时计数必须**已经**是新值。

        旧替身「先追加信号、后改计数」，轮询一旦在那两条语句之间收手就读到半成品（用例时红时绿）；
        这里用探针把那个瞬间钉死，不必靠运气去撞竞态。
        """
        seen_when_signalled = []
        fake = FakeProgress()
        fake.bump("star_33")
        fake.unbumps = _ProbeList(lambda: seen_when_signalled.append(fake.seen("star_33")))
        fake.unbump("star_33")
        self.assertEqual(seen_when_signalled, [0], "信号出现时看到的必须是改完的计数")

    def test_undo_offloads_the_unbump_when_a_loop_is_running(self):
        """有运行中的 loop（真实 ``/api/undo`` 的形态）：退回落到线程，但**仍然会发生**（U27）。"""
        fake = FakeProgress()
        agent = _agent(fake)

        async def go():
            agent.st.snapshots.append(agent.st.make_snapshot())
            await agent.joseki_count_entry({"kind": "entered", "family": "star_33"})
            agent.undo(0)
            for _ in range(200):                 # 线程池里的退回是异步落地的，等它落地
                if fake.unbumps:
                    break
                await asyncio.sleep(0.01)

        asyncio.run(go())
        self.assertEqual(fake.unbumps, ["star_33"])
        self.assertEqual(fake.seen("star_33"), 0)


class RealCountTest(unittest.TestCase):
    """真 ``JosekiProgress`` + 临时库的端到端：同局同族一次、悔棋退回真的落在库上。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "joseki_progress.db")
        self.progress = JosekiProgress(self.db)

    def tearDown(self):
        self.dir.cleanup()

    def test_count_once_then_undo_returns_it(self):
        agent = _agent(self.progress)
        jk = {"kind": "entered", "family": "star_33"}
        asyncio.run(agent.joseki_count_entry(jk))
        asyncio.run(agent.joseki_count_entry(jk))                # 同局同族第二次：不再计数
        self.assertEqual(self.progress.seen("star_33"), 1)

        agent.st.snapshots.append(agent.st.make_snapshot())      # 快照＝这一手之前（这一族还没计过数）
        agent.st.add_move("B", "D16")
        asyncio.run(agent.joseki_count_entry({"kind": "entered", "family": "san_san"}))
        self.assertEqual(self.progress.seen("san_san"), 1)

        agent.undo(0)
        self.assertEqual(self.progress.seen("san_san"), 0, "这一手计的那一次必须退回")
        self.assertEqual(self.progress.seen("star_33"), 1, "快照之前的族不该被退")
        self.assertEqual(agent.st.joseki_counted, ["star_33"], "应与快照一致")
        self.assertEqual(agent.st.joseki_dropped, ["san_san"])

    def test_three_games_then_the_fourth_gets_no_card(self):
        """跨局口径：前 3 局进入都上卡片，第 4 局进入不上（库真落盘、跨实例存活）。"""
        jk = {"kind": "entered", "family": "star_33", "corner": "左上", "cat": "star"}
        for i in range(3):
            agent = _agent(JosekiProgress(self.db))
            with self.subTest(round=i + 1):
                self.assertTrue(_show_card(agent, jk))
                asyncio.run(agent.joseki_count_entry(jk))
        agent = _agent(JosekiProgress(self.db))
        self.assertEqual(JosekiProgress(self.db).seen("star_33"), 3)
        self.assertFalse(_show_card(agent, jk))


class DedupKeySplitTest(unittest.TestCase):
    """U26（T2 评审顺延的必办项）：``entered`` / ``branch`` 不许吃 ``deviated`` 的 ``D:`` 预算。"""

    def test_enter_or_branch_does_not_consume_the_deviation_budget(self):
        """先（进入/）分岔再真偏离：偏离仍须被识别（旧实现里 ``D:左上`` 已被非偏离事件吃掉 ⇒ 沉默）。

        点三三补了「挡另一边」之后（T11 修复轮 U49），角内恰好 2 手的这个局面判 ``branch``；
        本用例钉的不变量是「非偏离事件不吃 ``D:`` 额度」，故断言按分岔的键写。
        """
        agent = _agent(None)
        _seed(agent, [["B", "D16"], ["W", "C17"]])               # 角内恰好 2 手吻合、第 3 手有分岔 ⇒ branch
        first = agent._joseki_check()
        self.assertEqual(str(first.get("kind")), "branch")
        self.assertEqual([e["key"] for e in agent.st.joseki_log], ["B:左上:star_33"])

        _seed(agent, [["B", "C16"], ["W", "D17"]])               # 第 4 手走完核心手顺 ⇒ formed
        self.assertEqual(str(agent._joseki_check().get("kind")), "formed")
        agent.st.board.play(1, 4, "black")
        agent.st.add_move("B", "B15")                            # 第 5 手出线 ⇒ 真偏离
        got = agent._joseki_check()
        self.assertEqual(str(got.get("kind")), "deviated")
        self.assertEqual([e["key"] for e in agent.st.joseki_log],
                         ["B:左上:star_33", "F:左上:star", "D:左上"])

    def test_branch_has_its_own_key(self):
        """``branch`` 单列 ``B:<角>:<族>``（每角每族一次），不吃 ``D:`` 的额度。"""
        agent = _agent(None)
        # 真实库现在也有真分岔（见 test_joseki 的 fork 用例），这里仍按识别器形状合成一个结果，
        # 只钉「去重键怎么分家」这一条。
        result = {"kind": "branch", "family": "star_kakari_small", "corner": "左上", "cat": "star",
                  "name": "小飞挂·靠压定式", "key": "（合成）", "k": 2, "line": [], "points": [],
                  "candidates": [{"name": "小飞挂·靠压定形", "family": "star_kakari_small",
                                  "next": (4, 2), "line": []}]}
        with mock.patch.object(JosekiRecognizer, "scan", staticmethod(lambda moves: dict(result))):
            self.assertEqual(str(agent._joseki_check().get("kind")), "branch")
            self.assertEqual([e["key"] for e in agent.st.joseki_log], ["B:左上:star_kakari_small"])
            self.assertEqual(agent._joseki_check(), {}, "同角同族第二次：去重")

    def test_formed_key_still_dedups_per_corner_and_cat(self):
        """``formed`` 的 ``F:<角>:<cat>`` 与现状一致：同一角同一 cat 第二次成型仍被去重。"""
        agent = _agent(None)
        _seed(agent, [["B", "D16"], ["W", "C17"], ["B", "C16"], ["W", "D17"]])
        first = agent._joseki_check()
        self.assertEqual(str(first.get("kind")), "formed")
        self.assertEqual([e["key"] for e in agent.st.joseki_log], ["F:左上:star"])
        self.assertEqual(agent._joseki_check(), {})


class AssemblyTest(unittest.TestCase):
    """U22：``create_app`` 无条件装配 ``JosekiProgress``，且**构造期不碰磁盘**。"""

    def test_lifespan_wires_a_progress_that_touches_no_disk(self):
        from app.config import AzureConfig
        from app.main import create_app
        data_dir = os.path.dirname(DEFAULT_DB_PATH)
        # ``backend/data/`` 与 ``joseki_progress.db`` 都是**运行产物**（第一次真计数才创建），
        # 本机完全可能已经有了——所以这里断言的是「装配**没有变化**」，不是「绝对不存在」。
        # 用显式的默认 ``cfg`` 构造：本地 ``backend/config.json`` 若开了 ``memory_enabled``，
        # 那是 ``MemoryService``（与本任务无关）在 ``backend/data/`` 里建目录，不该由这条用例背。
        db_existed = os.path.exists(DEFAULT_DB_PATH)
        dir_existed = os.path.isdir(data_dir)
        app = create_app(cfg=AzureConfig(), engine=StubEngine(), llm=StubLLM(), autostart=False)
        with TestClient(app):
            self.assertIsInstance(app.state.agent.joseki_progress, JosekiProgress)
            self.assertIs(app.state.joseki_progress, app.state.agent.joseki_progress)
        # 装配（含构造 JosekiProgress）不得创建、也不得删除这两个东西
        self.assertEqual(os.path.exists(DEFAULT_DB_PATH), db_existed,
                         "装配不得创建 / 删除库文件")
        self.assertEqual(os.path.isdir(data_dir), dir_existed,
                         "装配不得创建 / 删除 backend/data/")

    def test_injected_agent_gets_the_same_progress(self):
        agent, _eng, _llm, _bus = mk_agent()
        from app.main import create_app
        app = create_app(agent=agent, autostart=False)
        with TestClient(app):
            self.assertIsInstance(app.state.agent.joseki_progress, JosekiProgress)
            self.assertIs(app.state.agent.joseki_progress, app.state.joseki_progress)


if __name__ == "__main__":
    unittest.main()
