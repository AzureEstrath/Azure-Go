"""任务车道（v4.4.0）：给前台聊天、人格化小调用与后台任务一个轻量的可观测标签。

参考 AgentRM / Agent.xpu 的 multi-level feedback queue 思路，但**不改变执行顺序**：
当前本地单槽 LLM 仍靠现有的取消 / 超时保护，这里只记录每条车道的次数与耗时，
为未来接入第二个程序前的路由与容量判断预留入口。

纯标准库、线程安全、失败安全：统计本身永远不影响主流程。
"""

import threading
import time
from contextlib import contextmanager

LANE_FOREGROUND = "foreground"      # 玩家正在等的那一句话
LANE_PERSONA = "persona"            # 情绪精修 / 人格化小调用
LANE_BACKGROUND = "background"      # embedding 回填等不阻塞聊天的工作
LANES = (LANE_FOREGROUND, LANE_PERSONA, LANE_BACKGROUND)

_LOCK = threading.Lock()
_STATS = {lane: {"active": 0, "count": 0, "total_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0}
          for lane in LANES}


def _valid(lane) -> bool:
    return str(lane) in _STATS


def begin(lane) -> float:
    """记录一次任务开始（返回单调时钟）；未知车道返回 0.0，不计数。"""
    if not _valid(lane):
        return 0.0
    with _LOCK:
        _STATS[str(lane)]["active"] += 1
    return time.monotonic()


def end(lane, started) -> None:
    """记录一次任务结束；耗时按毫秒累计，异常 / 脏起点安全忽略。"""
    if not _valid(lane):
        return
    try:
        elapsed = max(0.0, (time.monotonic() - float(started)) * 1000.0)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    with _LOCK:
        row = _STATS[str(lane)]
        row["active"] = max(0, int(row["active"]) - 1)
        row["count"] = int(row["count"]) + 1
        row["total_ms"] = round(float(row["total_ms"]) + elapsed, 2)
        row["max_ms"] = round(max(float(row["max_ms"]), elapsed), 2)
        row["last_ms"] = round(elapsed, 2)


@contextmanager
def track(lane):
    """``with lanes.track(LANE_BACKGROUND): ...``；退出时自动记一次耗时。"""
    started = begin(lane)
    try:
        yield
    finally:
        end(lane, started)


def snapshot() -> dict:
    """当前各车道计数 / 总耗时 / 最大耗时 / 最近耗时（深拷贝，调用方可安全序列化）。"""
    with _LOCK:
        return {lane: dict(row) for lane, row in _STATS.items()}


def reset() -> None:
    """清空统计（测试用；不影响执行）。"""
    with _LOCK:
        for lane in LANES:
            _STATS[lane] = {"active": 0, "count": 0, "total_ms": 0.0,
                            "max_ms": 0.0, "last_ms": 0.0}
