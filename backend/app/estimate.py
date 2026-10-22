"""局面点目（形势判断）的纯函数：把 KataGo 的 ``ownership`` 收敛成棋盘标记与面板数字。

只在 ``analyze_position()`` 的 ``prepare`` 阶段调用，**复用既有的那一次分析**——不新增任何
引擎调用、不产生台词、不进任何 LLM 上下文（它只是给玩家看的棋盘注记）。

三条约定：

* **纯函数、零 IO**：输入是引擎响应原文 + 当前棋子列表，输出是可直接序列化的 dict；
  任何脏值 / 缺字段都退化成「不显示」（返回 ``None``），绝不抛。
* **只标「定了的点」**：``|ownership| >= SHOW_THRESHOLD`` 才算归属明确（演示图里的稀疏小方块
  就是这个口径），中间大片模糊地带不标。
* **活子不标、死子才标**：归属与棋子同色 ⇒ 那颗子本来就在盘上，不用再画方块；归属与棋子
  相反 ⇒ 对手的子被点死，方块画在棋子上（前端据 ``is_occupied`` 抬高一层）。

``ownership`` 是 KataGo 的逐点归属（+ = 黑）：**扁平数组、``index = y * 19 + x``、``y = 0`` 是最上面
一行**，与内部坐标（``y = 0`` 顶行、GTP 行号 19..1）一致，无需翻转。
"""

from . import prompts

BOARD_N = 19
KOMI = 7.5                      # 与 engine.py 的请求体一致（chinese 规则、黑贴 7.5）
SHOW_THRESHOLD = 0.5            # |ownership| ≥ 0.5 才算这一目定了

SIDE_BLACK = "black"
SIDE_WHITE = "white"


def _flat(ownership):
    """把引擎的 ``ownership`` 收敛成 361 个 ``float``；形状不对 ⇒ ``None``。

    引擎给的是扁平数组，但测试 / 旧版本可能给嵌套行（19×19）：两种都收。单个元素非数值时按
    ``0.0`` 处理（＝不标），不让一个脏值把整张点目图打没。
    """
    if not isinstance(ownership, (list, tuple)):
        return None
    vals = []
    for item in ownership:
        if isinstance(item, (list, tuple)):
            vals.extend(item)
        else:
            vals.append(item)
    if len(vals) != BOARD_N * BOARD_N:
        return None
    out = []
    for v in vals:
        try:
            out.append(max(-1.0, min(1.0, float(v))))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _stones_map(stones):
    """``stones_list()`` → ``{(x, y): "black"|"white"}``；脏值 / 越界一律跳过。"""
    occ = {}
    if not isinstance(stones, (list, tuple)):
        return occ
    for s in stones:
        if not isinstance(s, dict):
            continue
        try:
            x = int(s.get("x"))
            y = int(s.get("y"))
        except (TypeError, ValueError):
            continue
        if not (0 <= x < BOARD_N and 0 <= y < BOARD_N):
            continue
        c = str(s.get("c") or "")
        if c in (SIDE_BLACK, SIDE_WHITE):
            occ[(x, y)] = c
    return occ


def build(analysis, move_count, stones=None):
    """构造 ``estimate`` 事件的载荷；没有可用 ``ownership`` ⇒ ``None``（前端什么都不显示）。

    ``lead`` 与右栏「目差」同源（``prompts.to_black_perspective``，正数＝黑领先），保证两个
    数字不会互相打架；``black`` / ``white`` 是**标出来的点数**（空地 + 被点死的对手子）。
    """
    if not isinstance(analysis, dict) or "error" in analysis:
        return None
    vals = _flat(analysis.get("ownership"))
    if vals is None:
        return None
    occ = _stones_map(stones)
    cells = []
    black = 0
    white = 0
    for i, v in enumerate(vals):
        if abs(v) < SHOW_THRESHOLD:
            continue
        x = i % BOARD_N
        y = i // BOARD_N
        side = SIDE_BLACK if v > 0.0 else SIDE_WHITE
        if occ.get((x, y)) == side:      # 活子：子本身就在那儿，不必再叠方块
            continue
        cells.append([x, y, side])
        if side == SIDE_BLACK:
            black += 1
        else:
            white += 1
    try:
        n = int(move_count)
    except (TypeError, ValueError):
        n = 0
    try:
        lead = float(prompts.to_black_perspective(analysis, n)[1])
    except (TypeError, ValueError):
        lead = None                      # 目差脏值：标记照常显示，面板那一行给「--」
    return {
        "komi": KOMI,
        "move_count": n,
        "lead": lead,
        "black": black,
        "white": white,
        "cells": cells,
    }
