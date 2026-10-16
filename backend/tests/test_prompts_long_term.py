"""注入段必须可选，且默认输出与改造前逐字符一致。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prompts

MOVES = [["B", "Q16"], ["W", "D4"]]


class LongTermNoteTest(unittest.TestCase):
    def test_empty_means_no_section(self):
        base = prompts.context_block(MOVES, [], [], "observe")
        with_empty = prompts.context_block(MOVES, [], [], "observe", long_term="")
        self.assertEqual(base, with_empty)
        self.assertNotIn("长期记忆", base)

    def test_section_inserted_before_iron_rules(self):
        out = prompts.context_block(MOVES, [], [], "observe", long_term="Estarth 喜欢点三三")
        self.assertIn("长期记忆", out)
        self.assertIn("Estarth 喜欢点三三", out)
        self.assertLess(out.index("长期记忆"), out.index("铁律"))
        self.assertIn("A→T 从左到右", out)        # 铁律段正文没被动过

    def test_note_wording_carries_reference_only_clause(self):
        note = prompts.long_term_note("他喜欢简明")
        self.assertIn("仅供参考", note)
        self.assertIn("别照抄", note)


class MemoryServiceTest(unittest.TestCase):
    def _cfg(self, **kw):
        class C:
            memory_enabled = True
            memory_db_path = ""
            embed_base_url = ""
            embed_model = ""
            embed_api_key = ""
            memory_top_k = 3
            memory_max_inject_chars = 240
        c = C()
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_disabled_returns_empty_and_no_db(self):
        import tempfile
        from app.memory_service import MemoryService
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "nope.db")
            svc = MemoryService(self._cfg(memory_enabled=False, memory_db_path=db))
            self.assertFalse(svc.available)
            self.assertEqual(svc.recall_for("任意"), "")
            self.assertFalse(os.path.exists(db))
            svc.close()

    def test_enabled_without_embedding_still_works_by_keyword(self):
        import tempfile
        from app.memory_service import MemoryService
        with tempfile.TemporaryDirectory() as d:
            svc = MemoryService(self._cfg(memory_db_path=os.path.join(d, "m.db")))
            self.assertTrue(svc.available)
            svc.remember_fact("Estarth 喜欢听定式讲解", tag="estarth")
            self.assertIn("定式讲解", svc.recall_for("讲讲定式"))
            svc.close()

    def test_build_embed_fn_requires_full_config(self):
        from app.memory_service import build_embed_fn
        self.assertIsNone(build_embed_fn(self._cfg()))
        self.assertIsNotNone(build_embed_fn(self._cfg(embed_base_url="https://e.example.com/v1",
                                                      embed_model="emb", embed_api_key="k")))


class EmbedTimeoutTest(unittest.TestCase):
    """单次 embedding 超时按 ``embed_timeout`` 装配（默认 10 秒，运维可收紧）。"""

    def _cfg(self, **kw):
        class C:
            embed_base_url = "https://e.example.com/v1"
            embed_model = "emb"
            embed_api_key = "k"
            embed_timeout = 2.5
        c = C()
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def _build(self, cfg):
        from unittest import mock
        from app import memory_service
        seen = {}

        class FakeEmbeddings:
            def __init__(self, **kw):
                seen.update(kw)

            def embed_query(self, text):
                return [0.0]

        with mock.patch.object(memory_service, "OpenAIEmbeddings", FakeEmbeddings):
            fn = memory_service.build_embed_fn(cfg)
        return fn, seen

    def test_configured_timeout_reaches_client(self):
        fn, seen = self._build(self._cfg())
        self.assertIsNotNone(fn)
        self.assertEqual(seen["timeout"], 2.5)

    def test_missing_or_bad_timeout_falls_back_to_default(self):
        from app.memory_service import EMBED_TIMEOUT_SEC

        class Legacy:                        # 旧配置对象（没有这个字段）
            embed_base_url = "https://e.example.com/v1"
            embed_model = "emb"
            embed_api_key = "k"

        cases = (("缺字段", Legacy()),
                 ("0", self._cfg(embed_timeout=0)),
                 ("负数", self._cfg(embed_timeout=-1)),
                 ("脏值", self._cfg(embed_timeout="abc")))
        for label, cfg in cases:
            fn, seen = self._build(cfg)
            self.assertIsNotNone(fn, label)
            self.assertEqual(seen["timeout"], EMBED_TIMEOUT_SEC, label)


if __name__ == "__main__":
    unittest.main()
