"""EmotionModel 行为回归测试：逐条比对 Godot 导出的黄金样本。

运行：在 d:\\KataGo\\azure-server 下执行
    python -m unittest discover -s tests -v

约定：region / name / bands / hint / ok 必须完全相等；
delta 分量与 v/c/a/pitch/morphs 用 abs(a-b) <= 1e-5 容差比较。
"""

import json
import os
import time
import unittest

from app.emotion import EmotionModel

_TOL = 1e-5
_GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "emotion.json")


def _load_golden():
    with open(_GOLDEN, "r", encoding="utf-8") as f:
        return json.load(f)


class _Base(unittest.TestCase):
    def assertClose(self, actual, expected, msg=""):
        self.assertLessEqual(
            abs(actual - expected), _TOL,
            f"{msg} 期望={expected!r} 实际={actual!r}",
        )

    def assertMorphsClose(self, actual, expected, msg=""):
        self.assertEqual(set(actual.keys()), set(expected.keys()), f"{msg} morphs 键不一致 期望={expected} 实际={actual}")
        for k, ev in expected.items():
            self.assertClose(actual[k], ev, f"{msg} morphs[{k}]")


class TestGridSamples(_Base):
    """216 组三维格点 → 区域/名称（每组新建实例并置 _t_ms，dt≈0）。"""

    def test_grid_samples(self):
        golden = _load_golden()
        samples = golden["grid_samples"]
        self.assertEqual(len(samples), 216)
        for s in samples:
            m = EmotionModel()
            m._t_ms = time.monotonic() * 1000.0
            m.v, m.c, m.a = s["v"], s["c"], s["a"]
            region = m.region()
            tag = f"v={s['v']} c={s['c']} a={s['a']}"
            self.assertEqual(region, s["region"], f"{tag} region 期望={s['region']} 实际={region}")
            self.assertEqual(m.region_name(), s["name"], f"{tag} name 期望={s['name']} 实际={m.region_name()}")
            self.assertClose(m.v, s["v"], f"{tag} v")
            self.assertClose(m.c, s["c"], f"{tag} c")
            self.assertClose(m.a, s["a"], f"{tag} a")


class TestHysteresisSteps(_Base):
    """15 步 apply_delta，每步前重置 _t_ms（dt=0），验证分带滞回与懒衰减。"""

    def test_hysteresis_steps(self):
        golden = _load_golden()
        steps = golden["hysteresis_steps"]
        self.assertEqual(len(steps), 15)
        m = EmotionModel()
        for st in steps:
            m._t_ms = time.monotonic() * 1000.0
            m.apply_delta(*st["delta"])
            tag = f"step={st['i']} delta={st['delta']}"
            self.assertClose(m.v, st["v"], f"{tag} v")
            self.assertClose(m.c, st["c"], f"{tag} c")
            self.assertClose(m.a, st["a"], f"{tag} a")
            self.assertEqual(m._region, st["region"], f"{tag} region 期望={st['region']} 实际={m._region}")
            self.assertEqual(m.region_name(), st["name"], f"{tag} name 期望={st['name']} 实际={m.region_name()}")
            self.assertEqual(list(m._bands), list(st["bands"]), f"{tag} bands 期望={st['bands']} 实际={m._bands}")


class TestParseDelta(_Base):
    """parse_delta：9 组样本（全角、前后缀、超界、无标记、空串、非法小数）。"""

    def test_parse_delta(self):
        golden = _load_golden()
        samples = golden["parse_delta"]
        self.assertEqual(len(samples), 9)
        for s in samples:
            ok, delta = EmotionModel.parse_delta(s["text"])
            tag = f"text={s['text']!r}"
            self.assertEqual(ok, s["ok"], f"{tag} ok 期望={s['ok']} 实际={ok}")
            dv, dc, da = delta
            self.assertClose(dv, s["delta"][0], f"{tag} dv")
            self.assertClose(dc, s["delta"][1], f"{tag} dc")
            self.assertClose(da, s["delta"][2], f"{tag} da")


class TestThinkHint(unittest.TestCase):
    """think_hint：9 组 bands → 提示词。"""

    def test_think_hint(self):
        golden = _load_golden()
        samples = golden["think_hint"]
        self.assertEqual(len(samples), 9)
        for s in samples:
            m = EmotionModel()
            m._bands = list(s["bands"])
            hint = m.think_hint()
            self.assertEqual(
                hint, s["hint"],
                f"bands={s['bands']} 期望={s['hint']!r} 实际={hint!r}",
            )


class TestInfo(_Base):
    """info：idx 0..26 的 name/filler/pitch/burst/morphs。"""

    def test_info(self):
        golden = _load_golden()
        infos = golden["info"]
        self.assertEqual(len(infos), 27)
        m = EmotionModel()
        for row in infos:
            got = m.info(row["idx"])
            tag = f"idx={row['idx']}"
            self.assertEqual(got["name"], row["name"], f"{tag} name")
            self.assertEqual(got["filler"], row["filler"], f"{tag} filler")
            self.assertEqual(got["burst"], row["burst"], f"{tag} burst")
            self.assertClose(got["pitch"], row["pitch"], f"{tag} pitch")
            self.assertMorphsClose(got["morphs"], row["morphs"], tag)


class TestParsersAndTables(unittest.TestCase):
    """常量表与解析附属：REGIONS 顺序、event_delta/desc 完整性。"""

    def test_regions_order(self):
        names = [r["name"] for r in EmotionModel.REGIONS]
        self.assertEqual(len(names), 27)
        self.assertEqual(names[0], "落寞")
        self.assertEqual(names[12], "平静")
        self.assertEqual(names[26], "惊喜")

    def test_event_desc(self):
        self.assertEqual(EmotionModel.event_desc("player_good"), "对面下出了一手好棋")
        self.assertEqual(EmotionModel.event_desc("__unknown__"), "")


if __name__ == "__main__":
    unittest.main()
