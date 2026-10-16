"""长期记忆：写入/检索/降级/上限，全部用假 embedding，不触网。"""

import math
import os
import sqlite3
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

    def test_mixed_db_keeps_vectorless_rows_recallable(self):
        """向量行不得压掉关键词召回：没向量的行（写入时 embedding 失败 / 旧库遗留）也要找得到。"""
        calls = {"fail": False}

        def flaky(text):
            """写入这一条时 embedding 恰好失败（返回 None）——该行没向量，其余照常。"""
            if calls["fail"]:
                return None
            return fake_embed(text)

        store = MemoryStore(os.path.join(self.dir.name, "mixed.db"), embed_fn=flaky)
        try:
            calls["fail"] = True
            old_id = store.write("他喜欢乙乙乙的开局", tag="t")
            calls["fail"] = False
            new_id = store.write("布局喜欢点三三", tag="t2")
            self.assertEqual(store.stats()["has_vectors"], 1)     # 旧行确实没向量

            hits = store.recall("乙乙乙", k=3)                    # 向量命中不足 k 条 → 关键词补齐
            ids = [h["id"] for h in hits]
            self.assertIn(old_id, ids, "无向量的行照样关键词可达")
            self.assertIn(new_id, ids, "有向量的行也在（向量优先）")
            self.assertEqual([h["score"] for h in hits if h["id"] == old_id], [0.0])
        finally:
            store.close()

    def test_legacy_db_is_migrated_in_place(self):
        """旧库只有 ``memories(id, text)``：补表补列后写入 / 召回 / 统计 / 归档都得能用。"""
        path = os.path.join(self.dir.name, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
        conn.execute("INSERT INTO memories(text) VALUES('旧库里的一句话：喜欢点三三')")
        conn.commit()
        conn.close()

        store = MemoryStore(path, embed_fn=None)          # 关键词模式：旧行没有向量也能召回
        try:
            self.assertEqual((store.stats()["rows"], store.stats()["games"]), (1, 0))
            hits = store.recall("点三三", k=3)
            self.assertEqual([h["text"] for h in hits], ["旧库里的一句话：喜欢点三三"])
            self.assertIsInstance(store.write("新写的一条", tag="t"), int)
            self.assertEqual(store.stats()["rows"], 2)
            self.assertIsInstance(store.save_game([["B", "Q16"]], "B+2.5", "布局平稳"), int)
            self.assertEqual(store.stats()["games"], 1)
        finally:
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
