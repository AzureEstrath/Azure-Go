"""定式提示门控（计划 07 · Task 5 / v3.7.0）：两层决策 —— **资格层 + 频率层**。

**本模块只做决策**：纯函数、无副作用，不碰棋盘 / 事件 / ``speech`` 配对 / ``busy`` / 状态机；
随机数一律由调用方**注入**（``should_show(..., rng=...)``），所以单测一条断言都不依赖真随机。

| 层 | 决定什么 | 规则 |
| --- | --- | --- |
| 资格层 ``eligible`` | 这条定式**该不该教** | 族计数 ``< FREE_TEACH_TIMES`` → 有资格；``>=`` → 只有「走错」才有资格；``teach_on=False`` → 一律无资格 |
| 频率层 ``should_show`` | 这次**说不说** | 有资格时掷一次 ``p = clamp(rate + skill_adj + mood_adj, 0, 1)``；未命中与「没识别到定式」同一条出口 |

优先级（不许被概率压过）：

1. **走错**（真亏且偏离库线）→ **必讲**（纠错时刻，绕过频率层与冷却）；
2. 结构点（进入 / 分岔 / 收尾）→ 走频率层（这才是「可调」的那部分）；
3. 中段推进 → 一律不讲——识别器（``joseki.scan``）对中段自己沉默，压根走不到这里。

冷却：同一族在最近 ``COOLDOWN_MOVES`` 手内已讲过 → 本次直接按「未命中」处理（与后端
``AI_DEDUP_WINDOW`` 同口径，防连手讲同一个）。

画像 / 情绪：``skill_adj`` 只吃**局内既有信号**（本局玩家每手的 rank + 该族计数，不新建任何持久化），
``mood_adj`` 吃现有情绪区域快照（好奇 / 兴奋 ↑，疲惫 / 低落 ↓）；两者各自截断到 ``±ADJ_LIMIT``，
取不到 → ``0.0``，**任何输入都不许抛**（降级只影响「讲不讲」，绝不影响对局）。
"""

# 同一族最近 N 手内已讲过 ⇒ 本次按「未命中」处理（与 ``agent.AI_DEDUP_WINDOW`` 同口径）
COOLDOWN_MOVES = 8

# ``skill_adj`` / ``mood_adj`` 各自的截断幅度（「结合棋力画像与情绪上下浮动 ±10%」）
ADJ_LIMIT = 0.10

# 族计数 < 该值 ⇒ 有资格；>= 该值 ⇒ 只有走错才有资格（原「前 3 次 / 第 4 次起」两条规则并进这一层）
FREE_TEACH_TIMES = 3

# 结构点（进入 / 分岔 / 收尾）：走频率层
STRUCTURE_KINDS = ("entered", "branch", "formed")

# 偏离库线（走错只在它上面成立；「库线那一手」由 ``agent_graph.is_joseki_mistake`` 的护栏拦住）
DEVIATED = "deviated"

# 情绪区域名 → 原始调整量（正 = 更好奇 / 更兴奋，更愿意多讲；负 = 低落 / 疲惫，少打扰）。
# 名字取自 ``emotion.REGIONS``；中性区域不在表里（→ 0.0）。原始值刻意有超界项，由 ``_clamp`` 收到 ±0.10。
_MOOD_RAW = {
    # 积极 · 好奇 / 兴奋
    "好奇": 0.12, "兴致": 0.14, "激动": 0.16, "期待": 0.12, "跃跃欲试": 0.16, "惊喜": 0.18,
    "兴奋": 0.18,
    # 积极 · 其余
    "振奋": 0.12, "开心": 0.10, "欢喜": 0.12, "欢腾": 0.14, "愉悦": 0.08, "惬意": 0.06,
    # 消极（疲惫 / 低落一类）
    "落寞": -0.14, "烦闷": -0.12, "烦躁": -0.10, "失落": -0.14, "难过": -0.16, "委屈": -0.12,
    "忧心": -0.12, "不安": -0.14, "惊慌": -0.16,
    # 中性 · 略带好奇
    "跳脱": 0.02, "专注": 0.02,
}


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _f(x, d=0.0):
    """脏值安全的 float：转不出来（``None`` / 字符串 / 脏替身）一律退回 ``d``。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _roll(rng) -> float:
    """掷一次 ``[0, 1)``；rng 取不到（未注入 / 脏替身）时返回 ``1.0``（＝不命中，保守、不抛）。"""
    try:
        return float(rng.random())
    except Exception:              # noqa: BLE001 —— 没有可用的 rng：宁可不讲，也不许把回合打挂
        return 1.0


def _in_cooldown(last_taught_move, move_no, cooldown) -> bool:
    """同一族是否还在冷却窗口内（``last`` 起 ``cooldown`` 手之内）；取不到值 ⇒ 不在冷却。"""
    if last_taught_move is None:
        return False
    try:
        last = int(last_taught_move)
        cur = int(move_no)
        win = int(cooldown)
    except (TypeError, ValueError):
        return False
    if last < 0 or win <= 0:
        return False
    return 0 <= cur - last < win


# ================= 资格层 =================

def eligible(seen: int, kind: str, mistake: bool, teach_on: bool) -> bool:
    """资格层：这条定式**该不该教**（族计数 / 走错 / 总开关；纯判定，无副作用）。

    - ``teach_on=False``（``joseki_teach=false``）⇒ 一律无资格；
    - 走错（真亏且偏离库线）⇒ **有资格**（纠错时刻必讲，绕过频率层）；
    - 其余：族计数 ``< FREE_TEACH_TIMES`` 才有资格（「前 3 次」并进这一层，不再有独立的次数规则）；
    - 未知 / 非教学 ``kind``（中段推进一类）⇒ 无资格。

    次数读不出来（脏值）按「第 1 次」处理（fail-open，与 ``JosekiProgress.seen`` 的降级一致）。
    """
    if not teach_on:
        return False
    k = str(kind or "")
    if k not in STRUCTURE_KINDS and k != DEVIATED:
        return False
    if bool(mistake):
        return True
    return _f(seen, 0.0) < FREE_TEACH_TIMES


# ================= 频率层 =================

def probability(rate: float, skill_adj: float, mood_adj: float) -> float:
    """``p = clamp(rate + skill_adj + mood_adj, 0, 1)``；脏值按 ``0`` 计（绝不抛）。"""
    return _clamp(_f(rate) + _f(skill_adj) + _f(mood_adj), 0.0, 1.0)


def decision(*, rng, rate, skill_adj, mood_adj, seen, kind, mistake, teach_on,
             last_taught_move, move_no, cooldown=COOLDOWN_MOVES) -> dict:
    """完整决策（``should_show`` 的布尔投影）：多带出 ``p`` / ``roll`` 供调用方留一行日志。

    返回 ``{"eligible", "p", "roll", "cooldown", "show"}``；``roll`` 只在**真的掷了**时非 ``None``
    （走错 / 未命中资格 / 冷却 / ``p`` 到边界时都不掷，决策因此与 rng 状态无关、可复现）。
    """
    ok = eligible(seen, kind, mistake, teach_on)
    p = probability(rate, skill_adj, mood_adj)
    out = {"eligible": ok, "p": p, "roll": None, "cooldown": False, "show": False}
    if not ok:
        return out
    if bool(mistake):                           # 走错：绕过频率层与冷却
        out["show"] = True
        return out
    if _in_cooldown(last_taught_move, move_no, cooldown):
        out["cooldown"] = True
        return out
    if p <= 0.0:
        return out
    if p >= 1.0:
        out["show"] = True
        return out
    roll = _roll(rng)
    out["roll"] = roll
    out["show"] = roll < p
    return out


def should_show(*, rng, rate, skill_adj, mood_adj, seen, kind, mistake, teach_on,
                last_taught_move, move_no, cooldown=COOLDOWN_MOVES) -> bool:
    """最终决策：资格层 → 走错优先 → 冷却 → 掷一次 ``p``。

    调用方必须**注入** ``rng``（生产用 ``random.Random()``，单测用固定种子 / 固定值替身）。
    """
    return bool(decision(
        rng=rng, rate=rate, skill_adj=skill_adj, mood_adj=mood_adj, seen=seen, kind=kind,
        mistake=mistake, teach_on=teach_on, last_taught_move=last_taught_move,
        move_no=move_no, cooldown=cooldown)["show"])


# ================= 画像 / 情绪（各自截断 ±ADJ_LIMIT） =================

def skill_adj(ranks: list[int], seen_count: int) -> float:
    """棋力画像调整：生疏 / 没见过 → 正，熟练 / 见得多 → 负；截断 ``±ADJ_LIMIT``，取不到 → ``0.0``。

    只吃**局内既有信号**（门控不得新增持久化）：``ranks`` ＝本局玩家每手的 rank（``-1`` = 不在她
    算的前 5；不在前 5 的占比越高＝越生疏），``seen_count`` ＝该族历史出现次数（没见过 → 正）。
    """
    try:
        rs = [int(r) for r in (ranks or [])]
    except (TypeError, ValueError):        # 不是可迭代的整数序列（含 ``None`` / 脏值）：取不到
        return 0.0
    if not rs:
        return 0.0
    off = sum(1 for r in rs if r < 0) / float(len(rs))     # 不在前 5 的占比
    n = _f(seen_count, 0.0)
    raw = (off - 0.5) * 0.16                                # 全生疏 +0.08、全熟练 -0.08
    raw += 0.04 if n <= 0 else (0.0 if n < FREE_TEACH_TIMES else -0.04)
    return _clamp(raw, -ADJ_LIMIT, ADJ_LIMIT)


def mood_adj(mood: dict) -> float:
    """情绪调整：好奇 / 兴奋 → 正，低落 / 疲惫 → 负；截断 ``±ADJ_LIMIT``，取不到 → ``0.0``。

    吃 ``agent.mood_payload()`` 的形状（至少有 ``name``）；不是 dict / 名字认不出来（中性区域 / 脏值）
    一律 ``0.0``，**绝不抛**。
    """
    try:
        if not isinstance(mood, dict):
            return 0.0
        return _clamp(_MOOD_RAW.get(str(mood.get("name") or ""), 0.0), -ADJ_LIMIT, ADJ_LIMIT)
    except Exception:              # noqa: BLE001 —— 情绪取不到一律 0，绝不影响对局
        return 0.0
