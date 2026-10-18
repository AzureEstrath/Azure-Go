"""测试替身（stub）与工厂：不触网、不起子进程。

单独成文件，是为了不让 `test_main.py` 从 `test_agent.py` 里 import `TestCase` 子类——
那样 unittest 会把它当成 `test_main` 的模块属性再收集一次（`FailSafeTest` 的 6 个用例重复）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import events
from app.agent import AzureAgent


class StubEngine:
    """固定返回的假引擎：候选点覆盖常见位置，便于断言确定性的应手。

    ``analyze`` 收 ``max_visits``（v3.8.0 的按调用深度）：既有的 ``calls`` 语义一字不变，
    另外把每次的 ``max_visits`` 记进 ``visits``（与 ``calls`` 同序），供「深搜只给哪一次」的门卫用。
    """

    CAND = ["Q16", "D4", "Q4", "D16", "K10", "C3", "R14", "F16", "J3", "O16", "C16", "R4"]

    def __init__(self):
        self.calls = []
        self.visits = []
        self.dead = False

    async def analyze(self, moves, max_visits=None):
        self.calls.append([list(m) for m in moves])
        self.visits.append(max_visits)
        return {
            "rootInfo": {"winrate": 0.5, "scoreLead": 0.0},
            "moveInfos": [{"move": m, "visits": 1} for m in self.CAND],
            "ownership": [0.0] * 361,
        }

    def is_dead(self):
        return self.dead

    def last_error(self):
        return ""


class StubLLM:
    def __init__(self, text="嗯~ 这一步我看着挺顺。"):
        self.think_mode = 0
        self.text = text
        self.calls = []
        self.base_url = "http://localhost:8080/v1"

    async def chat(self, messages, max_tokens=300, no_think=False):
        self.calls.append((list(messages), max_tokens, no_think))
        return self.text


class BoomLLM:
    """chat 直接抛异常（云端 500 / 断连一类）。"""

    think_mode = 0
    base_url = "https://api.example.com/v1"

    async def chat(self, messages, max_tokens=300, no_think=False):
        raise RuntimeError("云端 500")


class ErrLLM:
    """真实失败形态：断网 / 401 / 429 / 超时在 LLMClient 里是「调用出错：…」文案（不抛异常）。"""

    think_mode = 0
    base_url = "https://api.example.com/v1"

    async def chat(self, messages, max_tokens=300, no_think=False):
        return "调用出错：HTTP 401（检查模型名是否为 qwen3.5）"


class ZeroRng:
    """固定随机源：``random()`` 恒为 ``0.0`` ⇒ 只要 ``p > 0`` 就一定命中。

    测试装配「必定讲」用它而不是靠 ``joseki_rate=1.0`` 单独兜底：画像 / 情绪的浮动是**负**的时候
    （熟练 / 见得多 / 低落）``p = clamp(1.0 + adj, 0, 1)`` 会小于 1，仍然会掷一次；把 rng 钉成 0.0
    才是真正的确定（不依赖真随机，也不受 adj 正负影响）。无状态，可多实例共用。
    """

    def random(self):
        return 0.0


def mk_agent():
    """造一套 stub 装配：返回 (agent, engine, llm, bus)。

    ``joseki_rate=1.0`` + ``ZeroRng()`` 是**测试装配的约定**：既有门卫要的是「有资格就必定讲」的
    确定性场景；定式提示门控本身的频率层行为（0.5 / 冷却 / 画像 / 情绪 / 走错绕过）由
    ``test_joseki_gate.py`` 显式设 rate 与注入 rng 覆盖。
    """
    eng = StubEngine()
    llm = StubLLM()
    bus = events.RecordingBus()
    return AzureAgent(eng, llm, bus, joseki_rate=1.0, joseki_rng=ZeroRng()), eng, llm, bus
