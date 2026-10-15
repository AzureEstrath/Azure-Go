"""长期记忆：写入/检索/降级/上限，全部用假 embedding，不触网。"""

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.memory import MAX_TEXT_LEN, MemoryStore, cosine, inject_text


def fake_embed(text):
    """极简确定性 embedding：按字符哈希出 8 维向量，保证同词相近。"""
    v = [0.0] * 8
    for ch in text:
        v[ord(ch) % 8] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.dir.name, "m.db"), embed_fn=fake_embed)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_write_then_recall(self):
        i = self.store.write("Estarth 喜欢在布局阶段点三三", kind="preference", tag="estarth")
        self.assertIsInstance(i, int)
        hits = self.store.recall("布局怎么下三三", k=3)
        self.assertEqual(hits[0]["id"], i)
        self.assertIn("点三三", hits[0]["text"])
        self.assertIsInstance(hits[0]["score"], float)

    def test_duplicate_not_stored_twice(self):
        self.store.write("同一句话", kind="fact", tag="t")
        self.store.write("同一句话", kind="fact", tag="t")
        self.assertEqual(self.store.stats()["rows"], 1)

    def test_text_truncated_to_limit(self):
        self.store.write("长" * 500)
        text = self.store.recall("长", k=1)[0]["text"]
        self.assertLessEqual(len(text), MAX_TEXT_LEN)

    def test_row_cap_evicts_oldest(self):
        store = MemoryStore(os.path.join(self.dir.name, "cap.db"), embed_fn=fake_embed)
        for i in range(5):
            store.write("第%d条" % i, tag="t%d" % i)
        store._max_rows = 3                      # 便于测试：临时收紧上限
        store.write("第四条", tag="t9")
        self.assertEqual(store.stats()["rows"], 3)
        store.close()

    def test_forget_all(self):
        self.store.write("a", tag="t")
        self.store.write("b", tag="t")
        self.assertEqual(self.store.forget(all=True), 2)
        self.assertEqual(self.store.stats()["rows"], 0)

    def test_save_game(self):
        self.store.save_game([["B", "Q16"], ["W", "D4"]], result="B+2.5", summary="布局平稳")
        self.assertEqual(self.store.stats()["games"], 1)

    def test_no_embed_fn_degrades_to_keyword(self):
        store = MemoryStore(os.path.join(self.dir.name, "kw.db"), embed_fn=None)
        store.write("Estarth 喜欢听棋谱讲解", kind="preference", tag="estarth")
        hits = store.recall("讲解", k=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["score"], 0.0)      # 关键词命中：不给伪相似度
        store.close()

    def test_embed_fn_failure_is_swallowed(self):
        def boom(_t):
            raise RuntimeError("云端 embedding 401")

        store = MemoryStore(os.path.join(self.dir.name, "boom.db"), embed_fn=boom)
        self.assertIsInstance(store.write("还是能存进去", tag="t"), int)
        self.assertEqual(len(store.recall("存进去", k=1)), 1)
        store.close()


class MathTest(unittest.TestCase):
    def test_cosine_identity_and_orthogonal(self):
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_cosine_zero_vector_is_safe(self):
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)


class InjectTest(unittest.TestCase):
    def test_items_truncated_and_joined(self):
        items = [{"text": "甲" * 200}, {"text": "乙"}]
        out = inject_text(items, max_chars=240)
        self.assertLessEqual(len(out), 240 + 8)      # 允许分隔符开销
        self.assertIn("…", out)

    def test_empty(self):
        self.assertEqual(inject_text([], 240), "")


if __name__ == "__main__":
    unittest.main()
