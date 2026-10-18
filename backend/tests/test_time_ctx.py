"""``app.time_ctx``：时间事实（当前时间 / 本局时长 / 单手用时 / 间隔 / 连续天数）+ 采集点。

门卫口径（task-Q4-brief + 设计 §5-H / §6.3）：

- **时钟可注入**：``now`` 必给，跨日 / 跨时区边界按**本地时区**的日历日切；
- **首手无前一手 ⇒ 单手用时不计**（有效落子时刻少于 2 个就不产出该字段）；
- **缺什么就不产出什么**、脏值（``now=None`` / 非数值 / 未来时间戳）**不抛**；
- 采集点＝``POST /api/move`` 到达时打点（``agent.mark_move_time``），``reset_game`` 清空。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app import time_ctx
from app.main import create_app
from helpers import StubEngine, StubLLM, mk_agent


def _local(y, mo, d, h=0, mi=0, s=0):
    """本地时区的一个墙钟秒：测试用 ``mktime`` 构造，不依赖机器的时区偏移。"""
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


# 2026-10-19 是周一（本文件所有固定时刻都以它为准）
NOW = _local(2026, 10, 19, 21, 3)


class FactsShapeTest(unittest.TestCase):
    """字段产出 / 不产出的边界（含脏值不抛）。"""

    def test_now_is_required_and_dirty_now_yields_nothing(self):
        """``now`` 脏 ⇒ 整个 dict 为空（当前时间是一切的锚点）。"""
        for bad in (None, "abc", 0, -1, float("nan"), True):
            self.assertEqual(time_ctx.facts(now=bad), {}, "now=%r" % (bad,))

    def test_basic_fields_are_local(self):
        got = time_ctx.facts(now=NOW)
        self.assertEqual(got["date"], "2026-10-19")
        self.assertEqual(got["weekday"], "周一")
        self.assertEqual(got["clock"], "21:03")
        self.assertAlmostEqual(got["now"], NOW, places=3)
        # 只给 now：其余四项一个都不产出（不瞎猜 0）
        for k in ("session_sec", "avg_move_sec", "last_gap_days", "streak_days"):
            self.assertNotIn(k, got)

    def test_session_duration_needs_a_valid_start(self):
        got = time_ctx.facts(now=NOW, session_start=NOW - 750.0)
        self.assertAlmostEqual(got["session_sec"], 750.0, places=3)
        # 缺 / 脏 / 晚于现在：不产出
        self.assertNotIn("session_sec", time_ctx.facts(now=NOW, session_start=None))
        self.assertNotIn("session_sec", time_ctx.facts(now=NOW, session_start="abc"))
        self.assertNotIn("session_sec", time_ctx.facts(now=NOW, session_start=NOW + 10.0))

    def test_first_move_has_no_avg_and_two_moves_do(self):
        """**首手无前一手 ⇒ 不计**：只有一个落子时刻时不产出 ``avg_move_sec``。"""
        one = time_ctx.facts(now=NOW, move_ts_list=[NOW - 300.0])
        self.assertNotIn("avg_move_sec", one)
        self.assertNotIn("avg_move_sec", time_ctx.facts(now=NOW, move_ts_list=[]))
        self.assertNotIn("avg_move_sec", time_ctx.facts(now=NOW, move_ts_list=None))

        two = time_ctx.facts(now=NOW, move_ts_list=[NOW - 300.0, NOW - 260.0])
        self.assertAlmostEqual(two["avg_move_sec"], 40.0, places=3)
        three = time_ctx.facts(now=NOW, move_ts_list=[NOW - 300.0, NOW - 260.0, NOW - 180.0])
        self.assertAlmostEqual(three["avg_move_sec"], (40.0 + 80.0) / 2.0, places=3)

    def test_dirty_and_future_move_timestamps_are_ignored_one_by_one(self):
        """一条脏时刻不丢掉整份均值；未来时刻忽略；差值非正的那对丢掉。"""
        got = time_ctx.facts(now=NOW, move_ts_list=[NOW - 300.0, "脏", None, NOW - 260.0, NOW + 60.0])
        self.assertAlmostEqual(got["avg_move_sec"], 40.0, places=3)
        # 乱序（差值为负）⇒ 那一对丢掉；一对都不剩 ⇒ 不产出
        self.assertNotIn("avg_move_sec", time_ctx.facts(now=NOW, move_ts_list=[NOW - 10.0, NOW - 300.0]))
        # 不是列表 ⇒ 不产出、不抛
        self.assertNotIn("avg_move_sec", time_ctx.facts(now=NOW, move_ts_list="abc"))

    def test_last_gap_days(self):
        got = time_ctx.facts(now=NOW, last_game_ts=NOW - 3.0 * 86400.0)
        self.assertAlmostEqual(got["last_gap_days"], 3.0, places=6)
        # 缺 / 脏 / 未来：不产出
        self.assertNotIn("last_gap_days", time_ctx.facts(now=NOW, last_game_ts=None))
        self.assertNotIn("last_gap_days", time_ctx.facts(now=NOW, last_game_ts="昨天"))
        self.assertNotIn("last_gap_days", time_ctx.facts(now=NOW, last_game_ts=NOW + 86400.0))

    def test_facts_never_raise_on_dirty_input(self):
        """一整套脏值灌进去也不抛（对应字段不产出即可）。"""
        got = time_ctx.facts(now=NOW, session_start="x", last_game_ts={}, move_ts_list={"a": 1},
                             game_ts_list="nope")
        self.assertEqual(got["date"], "2026-10-19")
        for k in ("session_sec", "avg_move_sec", "last_gap_days", "streak_days"):
            self.assertNotIn(k, got)


class StreakTest(unittest.TestCase):
    """连续下棋天数：从今天往回数（含今天；**今天还没下完不算断**）。"""

    def test_counts_back_from_today(self):
        games = [_local(2026, 10, 19, 10, 0), _local(2026, 10, 18, 22, 0),
                 _local(2026, 10, 17, 9, 0)]
        got = time_ctx.facts(now=NOW, game_ts_list=games)
        self.assertEqual(got["streak_days"], 3)
        # 同一天里多局只算一天
        again = games + [_local(2026, 10, 19, 12, 0)]
        self.assertEqual(time_ctx.facts(now=NOW, game_ts_list=again)["streak_days"], 3)

    def test_today_not_finished_does_not_break_the_streak(self):
        """今天还没下完（还没有已结束的对局）不算断：锚点退到昨天。"""
        games = [_local(2026, 10, 18, 22, 0), _local(2026, 10, 17, 9, 0)]
        self.assertEqual(time_ctx.facts(now=NOW, game_ts_list=games)["streak_days"], 2)

    def test_broken_when_the_last_game_is_older_than_yesterday(self):
        games = [_local(2026, 10, 17, 9, 0)]
        self.assertEqual(time_ctx.facts(now=NOW, game_ts_list=games)["streak_days"], 0)

    def test_missing_list_means_no_field(self):
        """``game_ts_list`` 缺省 ⇒ **不产出**（不瞎猜 0）；给了空列表 ⇒ 0；全是脏值 ⇒ 不产出。"""
        self.assertNotIn("streak_days", time_ctx.facts(now=NOW))
        self.assertNotIn("streak_days", time_ctx.facts(now=NOW, game_ts_list=None))
        self.assertEqual(time_ctx.facts(now=NOW, game_ts_list=[])["streak_days"], 0)
        self.assertNotIn("streak_days", time_ctx.facts(now=NOW, game_ts_list=["脏", None, -1]))

    def test_future_game_timestamps_are_ignored(self):
        got = time_ctx.facts(now=NOW, game_ts_list=[_local(2026, 10, 18, 22, 0), NOW + 3600.0])
        self.assertEqual(got["streak_days"], 1)

    def test_cross_midnight_boundary_is_two_days(self):
        """本地 23:59 与 00:01 落在**两个日历日**：连续天数把它们算成两天。"""
        prev = _local(2026, 10, 18, 23, 59)
        now = _local(2026, 10, 19, 0, 1)
        self.assertNotEqual(time_ctx._local_day(prev), time_ctx._local_day(now))
        got = time_ctx.facts(now=now, last_game_ts=prev, game_ts_list=[prev, now])
        self.assertAlmostEqual(got["last_gap_days"], 2.0 / 1440.0, places=6)
        self.assertEqual(got["streak_days"], 2)

    def test_cross_timezone_uses_the_local_calendar_day(self):
        """跨时区：**本地同一天**的两个时刻（哪怕 UTC 日不同）只算一天。"""
        early = _local(2026, 10, 19, 0, 30)
        late = _local(2026, 10, 19, 23, 30)
        self.assertEqual(time_ctx._local_day(early), time_ctx._local_day(late))
        got = time_ctx.facts(now=late, game_ts_list=[early])
        self.assertEqual(got["streak_days"], 1)


class MarkMoveTimeTest(unittest.TestCase):
    """采集点本身：脏值静默忽略、首手同时置位 ``session_start``。"""

    def test_first_call_sets_session_start_then_only_appends(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.mark_move_time(1000.0)
        self.assertEqual(agent.session_start, 1000.0)
        agent.mark_move_time(1040.0)
        self.assertEqual(agent.session_start, 1000.0)
        self.assertEqual(agent.move_ts_list, [1000.0, 1040.0])

    def test_dirty_values_are_silently_ignored(self):
        agent, _eng, _llm, _bus = mk_agent()
        for bad in ("abc", 0, -5.0, float("nan"), True):
            agent.mark_move_time(bad)
        self.assertIsNone(agent.session_start)
        self.assertEqual(agent.move_ts_list, [])


class MoveEndpointPunctuationTest(unittest.TestCase):
    """``POST /api/move`` 到达时打点（设计 §5-H：**不要求前端配合**）；``reset`` 清空。"""

    def _app(self):
        return create_app(engine=StubEngine(), llm=StubLLM(), autostart=False)

    def test_two_moves_record_two_timestamps_then_reset_clears(self):
        app = self._app()
        with TestClient(app) as client:
            s = app.state.agent
            self.assertIsNone(s.session_start)
            self.assertEqual(s.move_ts_list, [])

            self.assertTrue(client.post("/api/move", json={"x": 3, "y": 3}).json()["ok"])
            self.assertIsNotNone(s.session_start)
            self.assertEqual(len(s.move_ts_list), 1)

            self.assertTrue(client.post("/api/move", json={"x": 15, "y": 15}).json()["ok"])
            self.assertEqual(len(s.move_ts_list), 2)

            got = time_ctx.facts(now=time.time(), session_start=s.session_start,
                                 move_ts_list=s.move_ts_list)
            self.assertIn("session_sec", got)
            self.assertIn("avg_move_sec", got)      # 两手 ⇒ 有均值（首手不计）

            client.post("/api/reset")
            self.assertIsNone(s.session_start)
            self.assertEqual(s.move_ts_list, [])

    def test_rejected_moves_are_not_recorded(self):
        """畸形坐标 / 复盘拒绝：**不打点**（不是一次真实落子到达）。"""
        app = self._app()
        with TestClient(app) as client:
            s = app.state.agent
            for body in ({"x": "abc", "y": 3}, {}, {"x": 99, "y": 3}, {"x": 3, "y": -5}):
                self.assertFalse(client.post("/api/move", json=body).json()["ok"])
            self.assertEqual(s.move_ts_list, [])
            self.assertIsNone(s.session_start)


class TimeFactsAccessorTest(unittest.TestCase):
    """``agent.time_facts`` / ``agent.profile_facts``：两个开关是「注不注入」的唯一闸门。"""

    def test_switches_off_mean_empty_facts(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.mark_move_time(1000.0)
        self.assertFalse(agent.profile_enabled)
        self.assertFalse(agent.time_aware_enabled)
        self.assertEqual(agent.time_facts(now=2000.0), {}, "关着 ⇒ 空 dict（＝不注入）")
        self.assertEqual(agent.profile_facts(), {}, "关着 ⇒ 空 dict（＝不注入）")

    def test_time_facts_on_reads_the_collected_timestamps(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.time_aware_enabled = True
        base = 1_700_000_000.0
        agent.mark_move_time(base)
        agent.mark_move_time(base + 40.0)
        got = agent.time_facts(now=base + 100.0, last_game_ts=base - 2 * 86400.0,
                               game_ts_list=[base])
        self.assertAlmostEqual(got["session_sec"], 100.0, places=3)
        self.assertAlmostEqual(got["avg_move_sec"], 40.0, places=3)
        self.assertAlmostEqual(got["last_gap_days"], 2.0 + 100.0 / 86400.0, places=6)
        self.assertEqual(got["streak_days"], 1)

    def test_profile_facts_needs_an_assembled_store(self):
        agent, _eng, _llm, _bus = mk_agent()
        agent.profile_enabled = True
        self.assertEqual(agent.profile_facts(), {}, "没装配画像库 ⇒ 无画像（不抛）")

        class Boom:
            def get_profile(self):
                raise RuntimeError("库坏了")

        agent.profile_store = Boom()
        self.assertEqual(agent.profile_facts(), {}, "读画像抛异常 ⇒ 按「无画像」降级")

        agent.profile_store = type("S", (), {"get_profile": lambda self: {"open.star": 0.5}})()
        self.assertEqual(agent.profile_facts(), {"open.star": 0.5})


if __name__ == "__main__":
    unittest.main()
