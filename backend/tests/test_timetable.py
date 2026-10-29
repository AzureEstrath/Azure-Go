"""课程表几何还原：OCR 文本框 → 星期 / 节次 / 课程 / 教师 / 地点（纯函数，不跑 OCR）。"""

import datetime
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "peek"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import azure_files  # noqa: E402


def _box(text, x, y, w=70, h=18):
    return {"text": text, "x": x, "y": y, "x0": x - w / 2, "x1": x + w / 2,
            "y0": y - h / 2, "y1": y + h / 2, "score": 0.9}


class TimetableTest(unittest.TestCase):
    def _items(self):
        return [
            # 表头：3 个星期（10-26 周一 / 10-27 周二 / 10-28 周三）
            _box("周一", 150, 60), _box("周二", 256, 60), _box("周三", 362, 60),
            _box("10-26", 150, 90), _box("10-27", 256, 90), _box("10-28", 362, 90),
            # 左侧两节课的时间（x 在日期列左侧）
            _box("08:00", 50, 200), _box("08:45", 50, 220),
            _box("08:50", 50, 300), _box("09:35", 50, 320),
            # 周一 1-2 节：课程名 / 教师 / 地点
            _box("高等数学", 150, 160), _box("张老师", 150, 185), _box("@综合楼101", 150, 210),
            # 周二 1-2 节
            _box("无机化学", 256, 160), _box("李老师", 256, 185), _box("@综合楼103", 256, 210),
        ]

    def test_restores_weekday_period_teacher_and_location(self):
        got = azure_files.timetable_from_ocr(self._items(), ref=datetime.date(2026, 10, 28))
        self.assertEqual(len(got), 2)
        by = {x["title"]: x for x in got}
        math = by["高等数学"]
        self.assertEqual(math["weekday"], 0)
        self.assertEqual(math["date"], "2026-10-26")
        self.assertEqual(math["period"], "1-2")
        self.assertEqual(math["start_time"], "08:00")
        self.assertEqual(math["end_time"], "09:35")
        self.assertEqual(math["teacher"], "张老师")
        self.assertEqual(math["location"], "综合楼101")
        self.assertEqual(by["无机化学"]["weekday"], 1)

    def test_text_render_mentions_week_and_teacher(self):
        got = azure_files.timetable_from_ocr(self._items(), ref=datetime.date(2026, 10, 28))
        text = azure_files.timetable_text(got)
        self.assertIn("周一", text)
        self.assertIn("第1-2节", text)
        self.assertIn("教师：张老师", text)
        self.assertIn("地点：综合楼101", text)

    def test_no_date_header_returns_empty(self):
        self.assertEqual(azure_files.timetable_from_ocr([
            _box("08:00", 50, 200), _box("08:45", 50, 220), _box("随便什么", 150, 160),
        ]), [])


if __name__ == "__main__":
    unittest.main()
