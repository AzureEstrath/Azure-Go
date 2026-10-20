"""语音文本清洗：规则必须与前端 TTSClient.gd 的既有行为一致。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.speech_text import chunk_for_synthesis, clean_for_speech, extract_tone, split_sentences


class CleanTest(unittest.TestCase):
    def test_strips_emoji_and_faces(self):
        out = clean_for_speech("📊 黑棋在左上形成势力 (｡･ω･｡) 【对局总结】\n这手不错~  ⏪")
        self.assertNotIn("📊", out)
        self.assertNotIn("｡･ω･｡", out)
        self.assertNotIn("【", out)
        self.assertIn("黑棋在左上形成势力", out)
        self.assertIn("这手不错", out)

    def test_keeps_chinese_punctuation(self):
        self.assertIn("，", clean_for_speech("你好，Azure。"))

    def test_collapses_whitespace(self):
        self.assertEqual(clean_for_speech("  甲   乙  "), "甲 乙")

    def test_empty(self):
        self.assertEqual(clean_for_speech(""), "")

    def test_halfwidth_bracket_keeps_content(self):
        # T8②：半角单方括号镜像全角规则——只换括号、内容保留
        out = clean_for_speech("答案是[1]和[2]")
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)
        self.assertIn("1", out)
        self.assertIn("2", out)

    def test_double_bracket_marker_dropped(self):
        # T8①：[[...]] 是语气标记形态，整段丢弃，绝不能念出来
        out = clean_for_speech("[[tone:sigh]]算了")
        self.assertNotIn("tone:", out)
        self.assertNotIn("[[", out)
        self.assertIn("算了", out)

    def test_single_bracket_tone_keeps_content(self):
        # 单方括号按全角规则：只换括号、保留内容（真正的语气标记由 extract_tone 先剥）
        out = clean_for_speech("[tone:sigh]算了")
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)
        self.assertIn("tone:sigh", out)
        self.assertIn("算了", out)

    def test_none_input(self):
        self.assertEqual(clean_for_speech(None), "")


class ToneTest(unittest.TestCase):
    def test_extract_first_only(self):
        tone, rest = extract_tone("[[tone:sigh]]算了。\n[[tone:cheer]]不过这手不错！")
        self.assertEqual(tone, "sigh")
        self.assertIn("不过这手不错", rest)          # 标记全部剥掉，正文保留
        self.assertNotIn("tone:", rest)

    def test_unknown_tone_ignored(self):
        tone, rest = extract_tone("[[tone:angry]]哼")
        self.assertEqual(tone, "")
        self.assertNotIn("tone:", rest)

    def test_no_tone(self):
        self.assertEqual(extract_tone("你好"), ("", "你好"))


class SplitTest(unittest.TestCase):
    def test_split_multiple(self):
        self.assertEqual(split_sentences("你好。我在这里！在吗？"), ["你好。", "我在这里！", "在吗？"])

    def test_split_drops_empty(self):
        self.assertEqual(split_sentences("嗯。。\n\n"), ["嗯。。"])

    def test_chunk_merges_short(self):
        chunks = chunk_for_synthesis("甲。乙。丙。", max_chars=100)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "甲。乙。丙。")

    def test_chunk_splits_long(self):
        chunks = chunk_for_synthesis("甲" * 250, max_chars=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertGreaterEqual(len(chunks), 3)


class BoundaryTest(unittest.TestCase):
    def test_none_input_across_api(self):
        self.assertEqual(extract_tone(None), ("", ""))
        self.assertEqual(split_sentences(None), [])
        self.assertEqual(chunk_for_synthesis(None), [""])

    def test_max_chars_one(self):
        chunks = chunk_for_synthesis("甲。乙。", max_chars=1)
        self.assertTrue(all(len(c) <= 1 for c in chunks))
        self.assertEqual("".join(chunks), "甲。乙。")

    def test_pure_punct_fragment_merges(self):
        # T9④：纯标点碎片并回上一句，不单独成句（纯标点半句会被云端 TTS 拒）
        self.assertEqual(split_sentences("。。。嗯"), ["嗯"])
        self.assertEqual(split_sentences("你好。！！！"), ["你好。！！！"])


if __name__ == "__main__":
    unittest.main()
