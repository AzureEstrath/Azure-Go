"""结构化误输出兜底：模型吐 JSON 时，气泡 / 聊天历史拿到的必须是人话。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import actions


class NaturalizeTest(unittest.TestCase):
    def test_plain_line_is_untouched(self):
        line = "嗯，我在。{笑}"
        self.assertFalse(actions.looks_structured(line))
        self.assertEqual(actions.naturalize(line), line)

    def test_schedule_json_becomes_short_ack(self):
        out = actions.naturalize(
            '[{"date":"2026-10-28","time":"08:00","title":"无机化学实验"}]')
        self.assertEqual(out, "嗯，记下了。")
        self.assertNotIn('"date"', out)
        self.assertNotIn("[{", out)

    def test_course_json_is_not_read_aloud(self):
        raw = ('```json\n[{"weekday":"周三","period":"1-2","title":"高等数学",'
               '"teacher":"张老师","location":"综合楼101"}]\n```')
        self.assertEqual(actions.naturalize(raw), "嗯，记下了。")

    def test_unknown_json_is_empty_not_garbage(self):
        self.assertEqual(actions.naturalize('{"foo": 1}'), "")
        self.assertEqual(actions.naturalize("[{"), "")
        self.assertEqual(actions.clean_actions("好。{笑}"), "好。{笑}")

    def test_bracketed_normal_line_is_not_json(self):
        """句首的 `[笑]` / 长动作标签是正常台词，不能被当成 JSON 清空。"""
        for line in ("[笑] 好呀", "{轻轻叹了口气}", "【不是 JSON】"):
            self.assertFalse(actions.looks_structured(line))
            self.assertEqual(actions.naturalize(line), line)


if __name__ == "__main__":
    unittest.main()
