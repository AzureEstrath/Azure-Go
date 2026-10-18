"""预测线 A/B/C 提取（纯函数）：零引擎调用，只吃既有的一次分析结果。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _info(move, pv, winrate=0.5, score=0.0):
    """一条候选：只带本模块会读的四个字段（**不挂任何引擎对象**）。"""
    return {"move": move, "pv": list(pv), "winrate": winrate, "scoreLead": score}


class ExtractLinesTest(unittest.TestCase):
    def test_a_is_top_candidate_pv(self):
        """A ＝ moveInfos[0] 的首手 + 其 pv 前 4 手（``pv[0]`` 是候选本身，先剥掉重复首手）。"""
        from app.pv_lines import extract_lines
        got = extract_lines([
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3", "R14"], winrate=0.61, score=3.5),
            _info("D4", ["D4", "Q4", "D16", "C3", "Q16"], winrate=0.60, score=3.4),
        ])
        a = got["a"]
        self.assertEqual(a["moves"], ["Q16", "D4", "Q4", "D16", "C3"])
        self.assertEqual(a["winrate"], 0.61)
        self.assertEqual(a["score"], 3.5)

    def test_short_pv_degrades(self):
        """pv 不足 5 手 → 用可用长度；缺 pv / 空输入 → 不抛、有多少做多少。"""
        from app.pv_lines import extract_lines
        # pv 只有 1 手（＝首手本身）⇒ A 只留首手
        got = extract_lines([_info("Q16", ["Q16"])])
        self.assertEqual(got["a"]["moves"], ["Q16"])
        self.assertIsNone(got["b"])
        self.assertIsNone(got["c"])
        self.assertEqual(got["drop"], 0.0)
        # pv 2 / 3 手 ⇒ 用可用长度
        self.assertEqual(extract_lines([_info("Q16", ["Q16", "D4"])])["a"]["moves"],
                         ["Q16", "D4"])
        self.assertEqual(extract_lines([_info("Q16", ["Q16", "D4", "Q4"])])["a"]["moves"],
                         ["Q16", "D4", "Q4"])
        # 引擎的另一种约定（pv 不含首手）：同样得到「首手 + pv 前 4 手」
        self.assertEqual(extract_lines([_info("Q16", ["D4", "Q4", "D16"])])["a"]["moves"],
                         ["Q16", "D4", "Q4", "D16"])
        # pv 字段缺失 ⇒ 只剩首手
        self.assertEqual(extract_lines([{"move": "Q16"}])["a"]["moves"], ["Q16"])
        # 空输入 / None ⇒ 三条线全 None，不抛
        self.assertEqual(extract_lines([]), {"a": None, "b": None, "c": None, "drop": 0.0})
        self.assertIsNone(extract_lines(None)["a"])
        self.assertIsNone(extract_lines([{"pv": ["Q16"]}])["a"], "认不出首手就不做线")

    def test_b_differs_from_a(self):
        """B 与 A 不同手数 ≥ diverge_min，且带 diff_from_a 与 A/B 胜率差。"""
        from app.pv_lines import extract_lines
        got = extract_lines([
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3"], winrate=0.60),
            _info("D4", ["D4", "Q4", "D16", "C3", "Q16"], winrate=0.58),
        ], diverge_min=2)
        b = got["b"]
        self.assertIsNotNone(b)
        self.assertEqual(b["moves"], ["D4", "Q4", "D16", "C3", "Q16"])
        self.assertGreaterEqual(b["diff_from_a"], 2)
        self.assertAlmostEqual(got["drop"], 0.02)

    def test_c_differs_from_a_and_b(self):
        """C 与 A、B **都** ≥ diverge_min；凑不齐 ⇒ c 为 None（不报错）。"""
        from app.pv_lines import extract_lines
        infos = [
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3"], winrate=0.60),
            _info("D4", ["D4", "Q4", "D16", "C3", "Q16"], winrate=0.59),
            _info("Q4", ["Q4", "D16", "C3", "Q16", "D4"], winrate=0.58),
        ]
        got = extract_lines(infos)
        c = got["c"]
        self.assertIsNotNone(c)
        self.assertEqual(c["moves"], ["Q4", "D16", "C3", "Q16", "D4"])
        self.assertGreaterEqual(c["diff_from_a"], 2)
        self.assertGreaterEqual(c["diff_from_b"], 2)
        # 只有两条线可选 ⇒ 只有 A/B
        got2 = extract_lines(infos[:2])
        self.assertIsNotNone(got2["b"])
        self.assertIsNone(got2["c"])
        # 只剩 A
        got3 = extract_lines(infos[:1])
        self.assertIsNone(got3["b"])
        self.assertIsNone(got3["c"])

    def test_candidates_rejected_when_too_weak(self):
        """胜率差 > max_drop 的候选不入选；放宽后同一条候选就能进。"""
        from app.pv_lines import extract_lines
        weak = [
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3"], winrate=0.60),
            _info("D4", ["D4", "Q4", "D16", "C3", "Q16"], winrate=0.40),      # 差 0.20 > 0.05
        ]
        got = extract_lines(weak)
        self.assertIsNone(got["b"])
        self.assertIsNone(got["c"])
        self.assertEqual(got["drop"], 0.0)
        self.assertIsNotNone(extract_lines(weak, max_drop=0.25)["b"])

    def test_short_pv_candidate_cannot_qualify(self):
        """短 pv 的候选不会靠「长度差」凑成够格：只有 1 手的线进不了 B。"""
        from app.pv_lines import extract_lines
        got = extract_lines([
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3"], winrate=0.60),
            _info("D4", ["D4"], winrate=0.59),                                # 只差 1 手 ⇒ 不够
            _info("Q4", ["Q4", "D16", "C3", "Q16", "D4"], winrate=0.58),
        ])
        self.assertEqual(got["b"]["moves"], ["Q4", "D16", "C3", "Q16", "D4"])

    def test_candidates_cap_and_pv_len(self):
        """只看前 ``candidates`` 个候选；``pv_len`` 决定每条线保留几手。"""
        from app.pv_lines import extract_lines
        infos = [
            _info("Q16", ["Q16", "D4", "Q4", "D16", "C3"], winrate=0.60),
            _info("D4", ["D4", "Q4", "D16", "C3", "Q16"], winrate=0.59),
            _info("Q4", ["Q4", "D16", "C3", "Q16", "D4"], winrate=0.58),
        ]
        got = extract_lines(infos, candidates=2)      # 池子里只剩第 2 个候选 ⇒ 只有 A/B
        self.assertIsNotNone(got["b"])
        self.assertIsNone(got["c"])
        self.assertEqual(extract_lines(infos, pv_len=3)["a"]["moves"], ["Q16", "D4", "Q4"])

    def test_dirty_params_do_not_raise(self):
        """参数是脏值时退回默认，绝不抛。"""
        from app.pv_lines import extract_lines
        got = extract_lines([_info("Q16", ["Q16", "D4"])], pv_len="x", diverge_min=None,
                            max_drop="x", candidates="x")
        self.assertEqual(got["a"]["moves"], ["Q16", "D4"])

    def test_no_engine_call(self):
        """零引擎调用：模块**顶层没有任何 import**（结构性证据，不靠源码子串——注释里出现
        引擎名或 ``analyze`` 这类词都不该让它红）。"""
        import ast
        import inspect
        from app import pv_lines
        tree = ast.parse(inspect.getsource(pv_lines))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], "pv_lines 不该 import 任何东西（尤其不 import 引擎 / HTTP 客户端）")
        # 只带四个字段的裸 dict（没有任何引擎对象参与）也能出线
        got = pv_lines.extract_lines([_info("Q16", ["Q16", "D4"])])
        self.assertEqual(got["a"]["moves"], ["Q16", "D4"])


if __name__ == "__main__":
    unittest.main()
