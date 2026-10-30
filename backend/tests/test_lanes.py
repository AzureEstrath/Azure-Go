"""任务车道（v4.4.0）：纯统计，不改变执行顺序。"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import lanes


class LaneTest(unittest.TestCase):
    def setUp(self):
        lanes.reset()

    def tearDown(self):
        lanes.reset()

    def test_track_records_count_ms_and_active(self):
        with lanes.track(lanes.LANE_FOREGROUND):
            time.sleep(0.005)
        snap = lanes.snapshot()
        row = snap[lanes.LANE_FOREGROUND]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["active"], 0)
        self.assertGreater(row["last_ms"], 0.0)
        self.assertGreaterEqual(row["max_ms"], row["last_ms"])

    def test_unknown_lane_is_ignored(self):
        started = lanes.begin("nope")
        lanes.end("nope", started)
        self.assertEqual(set(lanes.snapshot()), set(lanes.LANES))

    def test_reset_clears_stats(self):
        with lanes.track(lanes.LANE_BACKGROUND):
            pass
        lanes.reset()
        self.assertEqual(lanes.snapshot()[lanes.LANE_BACKGROUND]["count"], 0)


if __name__ == "__main__":
    unittest.main()
