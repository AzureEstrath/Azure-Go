"""日程事实（v4.3.0，E）：时间指代、局部选条、出话冲突核对。纯函数，不碰真实库。"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import schedule_facts as sf

NOW = datetime.datetime(2026, 10, 29, 20, 0).timestamp()      # 周四晚八点
BASE_DATE = datetime.date(2026, 10, 29)


def _row(title, day_offset, hour, minute=0, start_time=None):
    d = datetime.datetime.combine(
        BASE_DATE + datetime.timedelta(days=day_offset),
        datetime.time(hour, minute)).timestamp()
    return {"id": abs(hash((title, day_offset, hour))) % 9999, "title": title,
            "status": "planned", "start_ts": d,
            "start_time": start_time or "%02d:%02d" % (hour, minute)}


# 相对“今天”构造，避免测试写死日期与运行日不一致
ROWS = [
    _row("有机化学", 1, 8),
    _row("高等数学(基础)", 1, 9, 50),
    _row("体育足球", 1, 13, 30),
    _row("数据科学导论", 1, 19, 0),
    _row("讲座", 7, 13, 30),
]


class TimeRefTest(unittest.TestCase):
    def test_tomorrow_morning_and_hour(self):
        ref = sf.time_ref("明天早上八点", NOW)
        self.assertEqual(ref["period"], "早上")
        self.assertEqual(ref["hour"], 8)
        start = datetime.datetime.fromtimestamp(ref["start"])
        self.assertEqual((start.hour, start.minute), (8, 0))
        end = datetime.datetime.fromtimestamp(ref["end"])
        self.assertEqual((end.hour, end.minute), (9, 0))

    def test_weekday_means_nearest_future(self):
        # 测试当天是周四 → “周五”就是明天
        ref = sf.time_ref("周五要不要早起", NOW)
        self.assertEqual(ref["day"], (BASE_DATE + datetime.timedelta(days=1)).isoformat())

    def test_next_week_wednesday(self):
        ref = sf.time_ref("下周三有事吗", NOW)
        d = datetime.date.fromisoformat(ref["day"])
        self.assertEqual(d.weekday(), 2)
        self.assertGreaterEqual((d - BASE_DATE).days, 4)

    def test_period_without_day_is_not_guessed(self):
        self.assertIsNone(sf.time_ref("晚上吃什么", NOW))
        self.assertIsNone(sf.time_ref("有点累", NOW))
        self.assertIsNotNone(sf.time_ref("今晚", NOW))

    def test_vague_yidian_is_not_an_hour(self):
        ref = sf.time_ref("明天有一点累", NOW)
        self.assertIsNotNone(ref)
        self.assertIsNone(ref["hour"], "「一点」是程度词，不是 1 点")

    def test_explicit_date(self):
        d = BASE_DATE + datetime.timedelta(days=10)
        ref = sf.time_ref("%d月%d日" % (d.month, d.day), NOW)
        self.assertEqual(ref["day"], d.isoformat())


class SelectTest(unittest.TestCase):
    def test_tomorrow_only(self):
        got = sf.select(ROWS, "明天要不要吃早餐", now=NOW)
        self.assertEqual([r["title"] for r in got["rows"]],
                         ["有机化学", "高等数学(基础)", "体育足球", "数据科学导论"])
        self.assertIn("明天", got["text"])

    def test_morning_narrows(self):
        got = sf.select(ROWS, "明天早上要早起吗", now=NOW)
        self.assertEqual([r["title"] for r in got["rows"]], ["有机化学", "高等数学(基础)"])
        self.assertNotIn("体育足球", got["text"])

    def test_evening_narrows(self):
        got = sf.select(ROWS, "明天晚上有空吗", now=NOW)
        self.assertEqual([r["title"] for r in got["rows"]], ["数据科学导论"])

    def test_empty_window_says_no_plan(self):
        got = sf.select(ROWS, "后天有空吗", now=NOW)
        self.assertEqual(got["rows"], [])
        self.assertIn("没有安排", got["text"])

    def test_no_ref_keeps_48h_window(self):
        got = sf.select(ROWS, "你最近怎么样", now=NOW, default_hours=48)
        titles = [r["title"] for r in got["rows"]]
        self.assertIn("有机化学", titles)
        self.assertNotIn("讲座", titles)          # 7 天后超出 48 小时


class ConflictTest(unittest.TestCase):
    def test_wrong_morning_claim_is_caught(self):
        hit = sf.conflict("明天是周五，不用赶早八，可以慢悠悠地吃早餐~", ROWS)
        self.assertEqual(hit["title"], "有机化学")
        self.assertIn("08:00", sf.fact_line([hit]))

    def test_afternoon_claim(self):
        hit = sf.conflict("明天下午没事，可以睡个午觉。", ROWS)
        self.assertEqual(hit["title"], "体育足球")

    def test_no_class_claim(self):
        hit = sf.conflict("明天没有课，可以睡懒觉。", ROWS)
        self.assertIsNotNone(hit)

    def test_plain_chat_is_not_conflict(self):
        self.assertIsNone(sf.conflict("今天有点累，想早点睡了。", ROWS))
        self.assertIsNone(sf.conflict("我不太喜欢吃早餐。", ROWS))
        self.assertIsNone(sf.conflict("明天天气不错呀。", ROWS))
        self.assertIsNone(sf.conflict("明天要早起吗？", ROWS))

    def test_period_mismatch_is_not_conflict(self):
        # 明早没课、只有晚上的课：说“不用赶早八”不算冲突
        only_evening = [ROWS[3]]
        self.assertIsNone(sf.conflict("明天不用赶早八。", only_evening))

    def test_correction_sentence_contains_fact(self):
        hit = sf.conflict("明天不用赶早八。", ROWS)
        line = sf.correction_sentence(hit)
        self.assertIn("有机化学", line)
        self.assertIn("08:00", line)


if __name__ == "__main__":
    unittest.main()
