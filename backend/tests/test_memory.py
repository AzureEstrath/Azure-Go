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
        i = self.store.write("玩家喜欢在布局阶段点三三", kind="preference", tag="estarth")
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

    def test_chat_history_roundtrip_and_mood(self):
        self.store.append_chat("你好呀", "我在呢。", mode="pet", mood={"v": 0.7, "c": 0.5, "a": 0.4})
        self.store.append_chat("再来一句", "嗯哼。", mode="game", mood={"v": 0.6})
        rows = self.store.recent_chats(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["user_text"], "你好呀")
        self.assertEqual(rows[0]["mode"], "pet")
        self.assertAlmostEqual(rows[0]["mood"]["v"], 0.7)
        self.assertEqual(rows[1]["mode"], "game")

    def test_episode_rolls_over_and_writes_summary(self):
        # 6 轮内是同一段 episode；第 7 轮触发收束，并写一条可召回的 episode 摘要
        for i in range(6):
            self.store.append_episode_turn("pet", "第%d句" % i, "回应%d" % i)
        rows = self.store.recent_episodes(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["turns"]), 6)
        self.assertEqual(int(rows[0]["ended_ts"]), 0)
        self.store.append_episode_turn("pet", "第7句", "回应7")
        rows = self.store.recent_episodes(5)
        self.assertEqual(len(rows), 2)
        self.assertGreater(int(rows[0]["id"]), int(rows[1]["id"]))
        self.assertTrue(any("最近聊天摘要" in h["text"] for h in self.store.recall("最近聊天", k=3)))
        self.assertGreaterEqual(self.store.stats()["episodes"], 2)

    def test_episode_splits_on_emotion_valence_jump(self):
        """事件分割：同一段聊天里情绪效价大起大落时，另起一段而不是混成摘要。"""
        self.store.append_episode_turn("pet", "今天很开心", "那就好",
                                       mood={"v": 0.82, "c": 0.5, "a": 0.4})
        self.store.append_episode_turn("pet", "继续说说", "嗯，我在听",
                                       mood={"v": 0.80, "c": 0.5, "a": 0.4})
        self.assertEqual(len(self.store.recent_episodes(5)), 1)
        self.store.append_episode_turn("pet", "其实我有点难过", "怎么了",
                                       mood={"v": 0.30, "c": 0.6, "a": 0.3})
        rows = self.store.recent_episodes(5)
        self.assertEqual(len(rows), 2)
        self.assertGreaterEqual(int(rows[1]["ended_ts"]), int(rows[1]["started_ts"]))
        self.assertTrue(any("最近聊天摘要" in h["text"] for h in self.store.recall("最近聊天", k=3)))

    def test_close_episode_and_forget_all(self):
        self.store.append_episode_turn("pet", "在吗", "在呢")
        self.store.close_episode("pet")
        self.assertTrue(any("最近聊天摘要" in h["text"] for h in self.store.recall("最近聊天", k=3)))
        self.store.forget(all=True)
        self.assertEqual(self.store.recent_episodes(5), [])
        self.assertEqual(self.store.recall("最近聊天", k=3), [])

    def test_forget_all_clears_chat_history(self):
        self.store.append_chat("你好", "在呢", mode="pet")
        self.assertEqual(len(self.store.recent_chats(10)), 1)
        self.store.forget(all=True)
        self.assertEqual(self.store.recent_chats(10), [])

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
        store.write("玩家喜欢听棋谱讲解", kind="preference", tag="estarth")
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


class EmotionTagTest(unittest.TestCase):
    """v4.3.0（A）：记忆带二维情绪矩阵 + 情绪标签，召回按当前情绪轻微偏置。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.dir.name, "emo.db"), embed_fn=None)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_write_stores_two_dim_matrix_and_label(self):
        mid = self.store.write("玩家喜欢雨天摆棋", tag="t", mood={"v": 0.82, "c": 0.6, "a": 0.78})
        hits = self.store.recall("雨天", k=1, mood={"v": 0.82, "a": 0.78})
        self.assertEqual(hits[0]["id"], mid)
        self.assertEqual(hits[0]["emo_tag"], "兴奋")
        self.assertAlmostEqual(hits[0]["emo_v"], 0.82)
        self.assertAlmostEqual(hits[0]["emo_a"], 0.78)
        self.assertAlmostEqual(hits[0]["emo_affinity"], 1.0)

    def test_unknown_mood_is_not_faked(self):
        self.store.write("没有情绪的一条", tag="t")
        hit = self.store.recall("情绪", k=1)[0]
        self.assertEqual(hit["emo_tag"], "")
        self.assertEqual(hit["emo_v"], -1.0)

    def test_recall_prefers_mood_matching_memory_on_keyword_ties(self):
        self.store.write("一起喝茶的那个下午", tag="t", mood={"v": 0.80, "a": 0.75})
        self.store.write("一起散步的雨夜", tag="t2", mood={"v": 0.25, "a": 0.30})
        bright = self.store.recall("一起", k=2, mood={"v": 0.85, "a": 0.80})
        self.assertIn("喝茶", bright[0]["text"])
        sad = self.store.recall("一起", k=2, mood={"v": 0.20, "a": 0.25})
        self.assertIn("散步", sad[0]["text"])

    def test_meta_mood_is_used_too(self):
        self.store.write("聊天里很开心", tag="t", meta={"mood": {"v": 0.85, "c": 0.5, "a": 0.6}})
        hit = self.store.recall("开心", k=1)[0]
        self.assertEqual(hit["emo_tag"], "开心")


class SupersedeTest(unittest.TestCase):
    """v4.3.0（F）：旧事实失效但不删除；召回只认 active。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.dir.name, "sup.db"), embed_fn=None)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_superseded_rows_are_not_recalled(self):
        old = self.store.write("明天下午三点开会", tag="t")
        new = self.store.write("明天下午四点开会", tag="t2")
        self.assertEqual(self.store.supersede([old], by_id=new, reason="updated"), 1)
        hits = self.store.recall("开会", k=5)
        self.assertEqual([h["id"] for h in hits], [new])
        self.assertEqual(self.store.stats()["rows"], 2, "失效不删除，保留审计")

    def test_supersede_is_idempotent(self):
        mid = self.store.write("旧安排", tag="t")
        self.assertEqual(self.store.supersede([mid]), 1)
        self.assertEqual(self.store.supersede([mid]), 0)

    def test_rewriting_same_fact_reactivates_it(self):
        """同一条事实重新写回（取消后重新安排）应重新生效，而不是永远停在 superseded。"""
        mid = self.store.write("周五 08:00 有机化学", tag="t")
        self.assertEqual(self.store.supersede([mid]), 1)
        again = self.store.write("周五 08:00 有机化学", tag="t")
        self.assertEqual(again, mid)
        self.assertEqual([h["id"] for h in self.store.recall("有机化学", k=5)], [mid])

    def test_schedule_update_supersedes_same_day_same_title(self):
        import datetime
        day = datetime.datetime(2026, 10, 30, 8, 0).timestamp()
        m1 = {"schedule_id": 1, "title": "有机化学", "start_ts": day}
        i1 = self.store.write("日程：周五 08:00 有机化学", tag="schedule", meta=m1)
        m2 = {"schedule_id": 2, "title": "有机化学", "start_ts": day + 2 * 3600}
        i2 = self.store.write("日程：周五 10:00 有机化学", tag="schedule", meta=m2)
        got = self.store.supersede_schedule_conflicts(m2, keep_id=i2)
        self.assertEqual(got, 1)
        hits = self.store.recall("有机化学", k=5)
        self.assertEqual([h["id"] for h in hits if h["id"] == i2], [i2])
        self.assertNotIn(i1, [h["id"] for h in hits])

    def test_schedule_different_day_is_not_superseded(self):
        import datetime
        d1 = datetime.datetime(2026, 10, 30, 8, 0).timestamp()
        d2 = datetime.datetime(2026, 11, 6, 8, 0).timestamp()
        self.store.write("日程：周五 08:00 有机化学", tag="schedule",
                         meta={"schedule_id": 1, "title": "有机化学", "start_ts": d1})
        i2 = self.store.write("日程：下周五 08:00 有机化学", tag="schedule",
                              meta={"schedule_id": 2, "title": "有机化学", "start_ts": d2})
        self.assertEqual(self.store.supersede_schedule_conflicts(
            {"title": "有机化学", "start_ts": d2}, keep_id=i2), 0)

    def test_schedule_cancel_supersedes_by_id_and_falls_back_to_text(self):
        import datetime
        day = datetime.datetime(2026, 10, 30, 12, 0).timestamp()
        i1 = self.store.write("日程：周五 12:00 外卖", tag="schedule",
                              meta={"schedule_id": 7, "title": "外卖", "start_ts": day})
        legacy = self.store.write("日程：周五 12:00 外卖（旧记录）", tag="schedule")
        got = self.store.supersede_schedule_item({"id": 7, "title": "外卖", "start_time": "12:00"})
        self.assertGreaterEqual(got, 1)
        hits = self.store.recall("外卖", k=5)
        self.assertNotIn(i1, [h["id"] for h in hits])
        self.assertNotIn(legacy, [h["id"] for h in hits], "旧库无 meta 时按标题+时刻兜底")


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
