"""局内事件表（带时刻、只增不减、去重）。

对局中发生的事（她下错 / 玩家下坏 / 玩家下好 / 聊过天 / 定式）按**时刻**（``move_no``，1 基手数）
写进一张**只增不减**的表；主键是「对局时刻」，墙钟 ``ts`` 只为可读性 / 调试。

- **局内内存**：不新建持久化、不落盘；``reset()`` 清空（新一局重来）。
- **不进 ``GameState`` 快照、不随悔棋回退**：「发生过就是发生过」（悔棋本身也是一次事件）。
- **去重键 ＝ ``kind`` + 归一化 ``text``**（命中则**不写**，返回 ``None``）：治「不要反复写入同一内容」。
  归一化只做「折叠空白 + 首尾去空」（同一句话多打几个空格 / 换行不该被当成两件事）。
- ``kind`` 的取值见 ``EVENT_KINDS``；本表**不因 kind 陌生而丢事件**（只增不减优先于校验），
  陌生 kind 照样写入、照样参与去重。
- 事件原文是**给模型看的素材**，永不进台词、不产生 ``speech``（由消费方保证）。
"""

import re
import time

# 五个已知类别（设计 §6.3）：她下错 / 玩家下坏 / 玩家下好 / 聊过天 / 定式
EVENT_KINDS = ("ai_bad", "player_bad", "player_good", "chat", "joseki")

_WS_RE = re.compile(r"\s+")


def _norm(text):
    """归一化：折叠连续空白、首尾去空（``None`` → 空串）。"""
    return _WS_RE.sub(" ", str(text if text is not None else "")).strip()


def _as_move_no(v):
    """手数：转不出 int 时退回 0（脏值不抛，也不丢事件）。"""
    if isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class EventLog:
    """局内事件表：``add`` 只追加、``all()`` 取全量、``reset()`` 清空。"""

    def __init__(self):
        self._events = []
        self._seen = set()          # (kind, 归一化 text)：去重键

    def add(self, kind, move_no, text, detail):
        """写一条事件；**同 kind + 同归一化 text 已存在时什么都不做并返回 ``None``**。

        返回写入的那条事件（``{"move_no", "ts", "kind", "text", "detail"}``），便于调用方 / 门卫断言。
        """
        kind = str(kind if kind is not None else "")
        text = str(text if text is not None else "")
        key = (kind, _norm(text))
        if key in self._seen:
            return None
        ev = {
            "move_no": _as_move_no(move_no),
            "ts": time.time(),
            "kind": kind,
            "text": text,
            "detail": dict(detail) if isinstance(detail, dict) else {},
        }
        self._events.append(ev)
        self._seen.add(key)
        return ev

    def all(self):
        """**从开局到当前**的全部事件（按写入顺序；返回列表的副本，事件本身仍只读式使用）。"""
        return list(self._events)

    def reset(self):
        """清空（新一局 / 显式重来）：事件与去重集合一并清掉。"""
        self._events = []
        self._seen = set()

    def __len__(self):
        return len(self._events)
