"""玩家棋风画像的四维特征提取 + 指数衰减融合（**纯函数**：零 LLM、零引擎、零磁盘）。

用途：``summarize()``（手动结束）时从一条对局记录（``profile_store`` 的 ``games`` 行）里
抠出**本局**的四维特征 ``extract_axes(rec)``，再与上一版画像 ``merge(prev, x, n_games)``
融合成新画像，最后经 ``ProfileStore.upsert_axes`` 落盘（写入点与落盘都在调用方，本模块不碰盘）。

**控制者钉死的融合公式**（spec §五 A 的实现口径）：

- 融合：``v ← v*(1-α) + x*α``，**每个维度各自维护 ``n_games``**（该维已有的样本数）；
- **单局 α**：``α = max(alpha_cap, 1/(n_games + 1))``
  ⇒ 首样本 α=1、第 2 次 0.5、第 3 次 1/3、**第 4 次起稳在 ``alpha_cap=0.25``**
  （任务书逐字给出的那串数：1 / 0.5 / 1/3 / 0.25…；见 ``alpha_of`` 的注释——任务书写的是
  ``min``，但 ``min`` 与「首样本 α=1、第 2 次 0.5」矛盾且恒等于 0.25，故按那串数实现成 ``max``）；
- **置信度**：``confidence = 1 - 0.5 ** (n_games / half_life)``（随局数上升、渐近 1；
  ``n_games`` 取**融合后**的样本数）；
- 数值维的值**归一化到 ``[0,1]``**（写进提示词的也是这个刻度）。

**非数值维**（``blunder.top_type`` / ``persona.address_pref``）：值是**自然字符串**，
``merge`` **不做 EMA**——按「最近一次非空覆盖」，且**空 / 缺＝未知＝跳过不更新**
（不会用一局没数据的棋把学到的值擦掉）。其余维都是 ``[0,1]`` 的数值维。

**缺维 / 脏值一律当 0、绝不抛**：``extract_axes`` 对空 rec / 坏 JSON / 脏类型都能跑；
``merge`` 对缺键（数值维当 0 参与融合）与脏值（非数字当 0）同样不抛。
"""

import json
import math

from . import coords

# —— 融合参数（控制者钉死；改口径必须同步改本模块 docstring 与测试） ——
HALF_LIFE = 20          # 置信度半衰期（局）：n_games=20 时 confidence=0.5
ALPHA_CAP = 0.25        # 单局 α 上限（防一局爆冷改写画像）

# —— 提取参数（spec §八「留给建造者的」，都在这里集中标出） ——
OPENING_MOVES = 12      # 「开局」＝玩家自己的前 12 手（此后的角上棋子不算开局偏好）
CORNER_BOX = 6          # 角部区域＝每个角 6×6 的方框（够盖住星 / 小目 / 三三 / 目外）
TENUKI_DIST = 4         # 与上一手的切比雪夫距离 ≥ 4 记一次「脱先」（就近应的手不会这么远）
BLUNDER_DROP = 0.10     # 「真亏手」门槛：掉胜率 > 10%（与 ``state.record_loss`` 同口径）
RUSH_RATIO = 0.30       # 「随手」代理：用时 < 本局中位数的 30%
RUSH_MIN_SAMPLES = 5    # 少于 5 手样本不做时间代理（样本太少，中位数不稳）

# —— 维度键（spec §6.2） ——
OPEN_AXES = ("open.star", "open.komoku", "open.sansan", "open.mokuhazushi")
STYLE_AXES = ("style.aggression", "style.tenuki", "style.territory_vs_influence")
WEAK_AXES = ("weak.corner_tl", "weak.corner_tr", "weak.corner_bl", "weak.corner_br")
KAKARI_AXIS = "open.favorite_kakari"
BLUNDER_AXIS = "blunder.rate"
TOP_TYPE_AXIS = "blunder.top_type"
ADDRESS_AXIS = "persona.address_pref"
TOPIC_PREFIX = "persona.topic."

# 数值维里「``extract_axes`` 一定会发」的那批：``merge`` 对它们**缺键＝当 0 参与融合**；
# 其余键（``persona.topic.*`` 等）缺键＝**原样带过、不衰减**。
CORE_AXES = OPEN_AXES + (KAKARI_AXIS,) + STYLE_AXES + (BLUNDER_AXIS,) + WEAK_AXES
# 非数值维（值＝字符串，不做 EMA）
NON_NUMERIC_AXES = (TOP_TYPE_AXIS, ADDRESS_AXIS)

# 失误手型（spec §二「常亏手型（深入 / 无理 / 方向 / 随手）」）
BLUNDER_TYPES = ("深入", "无理", "方向", "随手")

# 四个角的中心（切比雪夫距离最近者胜）——用于把真亏手归到「薄弱方位」
_CORNER_CENTERS = ((3, 3), (15, 3), (3, 15), (15, 15))     # tl / tr / bl / br
_CORNER_KEYS = ("weak.corner_tl", "weak.corner_tr", "weak.corner_bl", "weak.corner_br")


# ================= 通用小工具（全部不抛） =================

def _clamp01(v) -> float:
    """数值维归一化到 ``[0,1]``；非数字当 0。"""
    try:
        f = float(v)
    except Exception:                   # noqa: BLE001 —— 脏值当 0
        return 0.0
    if math.isnan(f):
        return 0.0
    return 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)


def _load_json(v, default):
    """``rec`` 里的 JSON 列可能是文本（表里的原样）也可能是已解析对象；解析不出来退回 ``default``。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:               # noqa: BLE001
            return default
    return default if v is None else v


def _as_list(v):
    """脏值兜底成列表（``dict`` 也当空，别拿键当条目）。"""
    return list(v) if isinstance(v, (list, tuple)) else []


def _color_of(v) -> str:
    """颜色归一化成 ``"B"`` / ``"W"``；认不出来返回空串。"""
    s = str(v or "").strip().lower()
    if s in ("b", "black", "1"):
        return "B"
    if s in ("w", "white", "2"):
        return "W"
    return ""


def _xy_of_item(it):
    """一个 ``moves`` 条目里的坐标（``{"x","y"}`` / ``{"gtp"}`` / ``["B","Q16"]`` / ``"Q16"``）；认不出来 ``(-1,-1)``。"""
    if isinstance(it, dict):
        if "x" in it and "y" in it:
            try:
                return (int(it["x"]), int(it["y"]))
            except Exception:           # noqa: BLE001
                return (-1, -1)
        return coords.gtp_to_xy(str(it.get("gtp", "")))
    if isinstance(it, (list, tuple)) and len(it) >= 2:
        return coords.gtp_to_xy(str(it[1]))
    return coords.gtp_to_xy(str(it))


def _moves_of(rec) -> list:
    """把 ``rec["moves"]`` 解析成 ``[(颜色, x, y)]``（pass / 认不出来的条目**丢掉**）。

    接受四种形状（``profile_store`` 存的是 JSON 文本，调用方也可能给已解析对象）：
    ``[["B","Q16"],…]`` / ``[{"color":"B","gtp":"Q16","drop":0.2},…]`` /
    ``["Q16","D4",…]``（无颜色信息 ⇒ 按「黑先、黑白交替」推）。
    """
    items = _load_json(rec.get("moves") if isinstance(rec, dict) else None, [])
    out = []
    auto = None                          # 无颜色信息时的推定色（黑先）
    for it in _as_list(items):
        pos = _xy_of_item(it)
        if pos == (-1, -1):
            continue
        color = _color_of(it.get("color")) if isinstance(it, dict) else ""
        if color == "" and isinstance(it, (list, tuple)) and len(it) >= 1:
            color = _color_of(it[0])
        if color == "":
            if auto is None:
                auto = "B"
            color = auto
            auto = "W" if auto == "B" else "B"
        out.append((color, int(pos[0]), int(pos[1])))
    return out


def _drops_of(rec) -> list:
    """本局**玩家真亏手**的 ``[(x, y, drop)]``（``drop > BLUNDER_DROP``）。

    两个来源（任一存在即视为「本局有亏手数据」）：
    1. ``moves`` 里带 ``drop`` 的条目（推荐：Q3 把真亏手账并进 ``moves`` 的 JSON）；
    2. 可选的 ``rec["losses"]``（``[{"gtp":"Q16","drop":0.3},…]``，不在钉死的键表里、
       有就用、没有就算）。
    """
    out = []
    for it in _as_list(_load_json(rec.get("moves") if isinstance(rec, dict) else None, [])):
        if not (isinstance(it, dict) and "drop" in it):
            continue
        x, y = _xy_of_item(it)
        if (x, y) == (-1, -1):
            continue
        try:
            drop = float(it["drop"])
        except Exception:               # noqa: BLE001
            continue
        if drop > BLUNDER_DROP:
            out.append((x, y, drop))
    for it in _as_list(_load_json(rec.get("losses") if isinstance(rec, dict) else None, [])):
        if not isinstance(it, dict):
            continue
        x, y = coords.gtp_to_xy(str(it.get("gtp", "")))
        if (x, y) == (-1, -1):
            continue
        try:
            drop = float(it.get("drop", 0))
        except Exception:               # noqa: BLE001
            continue
        if drop > BLUNDER_DROP:
            out.append((x, y, drop))
    return out


def _has_drop_data(rec) -> bool:
    """本局是否带了「亏手」数据（有的话就不必退到时间代理）。"""
    if not isinstance(rec, dict):
        return False
    if "losses" in rec:
        return True
    for it in _as_list(_load_json(rec.get("moves"), [])):
        if isinstance(it, dict) and "drop" in it:
            return True
    return False


def _player_ms_of(rec, moves) -> list:
    """玩家每手的用时（毫秒）；认不出来一律 ``[]``。

    接受：与 ``moves`` **等长**的数组（按颜色挑出玩家的那几手）、
    ``{"E":[…],"A":[…]}`（``E`` 就是玩家）、或**只含玩家手**的数组（原样用）。
    """
    raw = _load_json(rec.get("per_move_ms") if isinstance(rec, dict) else None, [])
    n_all = len(moves)
    n_player = sum(1 for c, _x, _y in moves if c == "B")
    if isinstance(raw, dict):
        raw = raw.get("E", raw.get("player", []))
    vals = []
    for v in _as_list(raw):
        try:
            vals.append(float(v))
        except Exception:               # noqa: BLE001
            vals.append(None)
    if n_all and len(vals) == n_all:
        vals = [v for v, (c, _x, _y) in zip(vals, moves) if c == "B"]
    if len(vals) > n_player:
        vals = vals[:n_player]
    return vals


def _median(vals) -> float:
    """纯标准库中位数（空列表 → 0.0）。"""
    xs = sorted(float(v) for v in vals if v is not None)
    if not xs:
        return 0.0
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


# ================= 四维特征提取 =================

def extract_axes(game_rec) -> dict:
    """从一条对局记录里抠出**本局**的四维特征（spec §6.2 的键）；空 / 脏 rec 当 0、不抛。

    返回 ``{axis: 值}``：数值维是 ``[0,1]`` 的 ``float``，``blunder.top_type`` /
    ``persona.address_pref`` 是**字符串**（不知道就是 ``""``）。``CORE_AXES`` 里的键
    **一定都在**（算不出来就是 0.0，形状稳定）；``persona.topic.*`` **只发本局出现过的主题**
    （没聊到 / 没记录就不发这个键，``merge`` 会把它原样带过、不衰减）。

    提取规则（全部只看 ``rec`` 里钉死的那十个键；详见报告）：

    - ``open.star|komoku|sansan|mokuhazushi``：把每个角 6×6 方框里**玩家最早的那一手**
      按离角点的偏移分类（``(3,3)`` 星 / ``(2,3)|(3,2)`` 小目 / ``(2,2)`` 三三 /
      ``(2,4)|(4,2)`` 目外），只数玩家自己的**前 12 手**；值＝该类角数 / 有棋子的角数
      （「其它点」进分母不进分子，故四类之和 ≤ 1）。
    - ``open.favorite_kakari``：``joseki_families`` 里**挂角族**（族名含 ``kakari``）占
      「挂角 + 守角」（族名含 ``kakari`` / ``shimari``）的比例；没有族数据＝0。
    - ``style.aggression``：玩家落子时**与对方棋子正交相邻**（接触战）的手数占比。
    - ``style.tenuki``：玩家落子与**上一手**（任一方的最近一手有坐标的手）切比雪夫距离
      ≥ 4 的手数占比（首手没有上一手 ⇒ 不计）。
    - ``style.territory_vs_influence``：玩家每手「离最近边的线数 / 8」的均值
      （0＝全在一路（实地）、1＝全在天元（势力））。
    - ``blunder.rate``：有亏手数据时＝``drop > 0.10`` 的手数 / 玩家手数；没有亏手数据但
      ``per_move_ms`` 样本 ≥ 5 时退化为「随手」代理（用时 < 中位数 30% 的手数占比）；
      两者都没有＝0。
    - ``blunder.top_type``：亏手数据下按**亏得最多那一手**的几何分型
      （与对方相邻→``无理``；两格内有两个以上对方子→``深入``；否则→``方向``）；
      时间代理命中时＝``随手``；都没有＝``""``。
    - ``weak.corner_tl|tr|bl|br``：把每条真亏手按切比雪夫距离归到最近的角，
      值＝该角亏手数 / 本局亏手总数（没有亏手数据＝全 0）。
    - ``persona.topic.*``：``chat_topics`` 里每个主题的段数占比（键名＝``persona.topic.<主题>``）。
    - ``persona.address_pref``：可选键 ``rec["address_pref"]`` 的非空字符串（钉死的十个键里
      **没有**称呼信号，故默认 ``""``＝未知）。
    """
    rec = game_rec if isinstance(game_rec, dict) else {}
    moves = _moves_of(rec)
    player = [(x, y) for c, x, y in moves if c == "B"]

    out = {axis: 0.0 for axis in CORE_AXES}
    out[TOP_TYPE_AXIS] = ""
    out[ADDRESS_AXIS] = ""

    # —— 逐手几何：接触 / 与上一手的距离 / 线数（占位法，**不处理提子**，只作近似） ——
    occ = {}
    contact_flags = []
    tenuki_flags = []
    lines = []
    prev_xy = None
    for color, x, y in moves:
        if color == "B":
            contact = any(occ.get((nx, ny)) == "W"
                          for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
            contact_flags.append(contact)
            if prev_xy is not None:
                tenuki_flags.append(max(abs(x - prev_xy[0]), abs(y - prev_xy[1])) >= TENUKI_DIST)
            lines.append(min(x, y, coords.SIZE - 1 - x, coords.SIZE - 1 - y))
        occ[(x, y)] = color
        prev_xy = (x, y)

    # —— 开局偏好（玩家前 OPENING_MOVES 手，每个角取最早那一手） ——
    corners = [None] * 4                                     # tl / tr / bl / br
    seen_player = 0
    for color, x, y in moves:
        if color != "B":
            continue
        seen_player += 1
        if seen_player > OPENING_MOVES:
            break
        if x < CORNER_BOX and y < CORNER_BOX:
            idx = 0
        elif x >= coords.SIZE - CORNER_BOX and y < CORNER_BOX:
            idx = 1
        elif x < CORNER_BOX and y >= coords.SIZE - CORNER_BOX:
            idx = 2
        elif x >= coords.SIZE - CORNER_BOX and y >= coords.SIZE - CORNER_BOX:
            idx = 3
        else:
            continue
        if corners[idx] is None:
            dx = x if idx in (0, 2) else coords.SIZE - 1 - x
            dy = y if idx in (0, 1) else coords.SIZE - 1 - y
            corners[idx] = (dx, dy)
    known = [c for c in corners if c is not None]
    if known:
        buckets = {"sansan": 0, "star": 0, "komoku": 0, "mokuhazushi": 0}
        for dx, dy in known:
            key = {(2, 2): "sansan", (3, 3): "star",
                   (2, 3): "komoku", (3, 2): "komoku",
                   (2, 4): "mokuhazushi", (4, 2): "mokuhazushi"}.get((dx, dy))
            if key:
                buckets[key] += 1
        total = float(len(known))
        out["open.sansan"] = _clamp01(buckets["sansan"] / total)
        out["open.star"] = _clamp01(buckets["star"] / total)
        out["open.komoku"] = _clamp01(buckets["komoku"] / total)
        out["open.mokuhazushi"] = _clamp01(buckets["mokuhazushi"] / total)

    # —— 常用挂角 / 守角 ——
    kakari = shimari = 0.0
    for fam, cnt in _family_counts(rec):
        name = fam.lower()
        try:
            n = float(cnt)
        except Exception:                   # noqa: BLE001
            n = 1.0
        if "kakari" in name:
            kakari += n
        elif "shimari" in name:
            shimari += n
    if kakari + shimari > 0:
        out[KAKARI_AXIS] = _clamp01(kakari / (kakari + shimari))

    # —— 行棋风格 ——
    n_player = len(player)
    if n_player:
        out["style.aggression"] = _clamp01(sum(1 for c in contact_flags if c) / n_player)
        if tenuki_flags:
            out["style.tenuki"] = _clamp01(sum(1 for t in tenuki_flags if t) / len(tenuki_flags))
        out["style.territory_vs_influence"] = _clamp01(
            sum(lines) / (len(lines) * float(coords.SIZE - 1)) if lines else 0.0)

    # —— 失误与薄弱方位 ——
    blunders = _drops_of(rec)
    has_drops = _has_drop_data(rec)
    if has_drops:
        if n_player:
            out[BLUNDER_AXIS] = _clamp01(len(blunders) / float(n_player))
        if blunders:
            out[TOP_TYPE_AXIS] = _blunder_type(occ, blunders)
            counts = [0.0] * 4
            for x, y, _drop in blunders:
                counts[_nearest_corner(x, y)] += 1.0
            for i, axis in enumerate(_CORNER_KEYS):
                out[axis] = _clamp01(counts[i] / float(len(blunders)))
    else:
        times = _player_ms_of(rec, moves)
        if len(times) >= RUSH_MIN_SAMPLES:
            med = _median(times)
            if med > 0:
                rush = sum(1 for v in times if v is not None and v < RUSH_RATIO * med)
                rate = _clamp01(rush / float(len(times)))
                out[BLUNDER_AXIS] = rate
                if rate > 0:
                    out[TOP_TYPE_AXIS] = "随手"

    # —— 性格与话题 ——
    out.update(_topic_axes(rec))
    pref = rec.get("address_pref")
    if isinstance(pref, str) and pref.strip() != "":
        out[ADDRESS_AXIS] = pref.strip()
    return out


def _family_counts(rec) -> list:
    """``joseki_families`` → ``[(族名, 次数)]``；三种形状都认（``{fam: n}`` / ``[fam,…]`` / ``[[fam,n],…]``）。"""
    raw = _load_json(rec.get("joseki_families") if isinstance(rec, dict) else None, [])
    out = []
    if isinstance(raw, dict):
        for fam, cnt in raw.items():
            out.append((str(fam), cnt))
    else:
        for it in _as_list(raw):
            if isinstance(it, (list, tuple)) and it:
                out.append((str(it[0]), it[1] if len(it) > 1 else 1))
            elif it is not None:
                out.append((str(it), 1))
    return out


def _nearest_corner(x, y) -> int:
    """离哪个角最近（切比雪夫距离，平手取靠前的角）。"""
    best, best_d = 0, None
    for i, (cx, cy) in enumerate(_CORNER_CENTERS):
        d = max(abs(x - cx), abs(y - cy))
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def _blunder_type(occ, blunders) -> str:
    """亏得最多那一手的**几何分型**（``BLUNDER_TYPES`` 里的一个，认不出来＝``""``）。

    只是「没有引擎也能落地的近似」：与对方棋子正交相邻 → ``无理``（硬碰）；
    两格内有两个以上对方子 → ``深入``（孤军深入）；否则 → ``方向``。
    """
    if not blunders:
        return ""
    x, y, _drop = max(blunders, key=lambda b: b[2])
    if any(occ.get((nx, ny)) == "W"
           for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))):
        return "无理"
    near = 0
    for nx in range(x - 2, x + 3):
        for ny in range(y - 2, y + 3):
            if occ.get((nx, ny)) == "W":
                near += 1
    return "深入" if near >= 2 else "方向"


def _topic_axes(rec) -> dict:
    """``chat_topics`` → ``{"persona.topic.<主题>": 段数占比}``（本局没聊到就不发键）。"""
    raw = _load_json(rec.get("chat_topics") if isinstance(rec, dict) else None, [])
    counts = {}
    if isinstance(raw, dict):
        for topic, val in raw.items():
            counts[str(topic)] = counts.get(str(topic), 0.0) + 1.0
    else:
        for it in _as_list(raw):
            if isinstance(it, (list, tuple)) and it:
                topic = str(it[0])
            elif isinstance(it, dict):
                topic = str(it.get("topic", ""))
            else:
                topic = str(it)
            if topic == "":
                continue
            counts[topic] = counts.get(topic, 0.0) + 1.0
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {TOPIC_PREFIX + t: _clamp01(n / total) for t, n in counts.items()}


# ================= 指数衰减融合 =================

def confidence_of(n_games, half_life: float = HALF_LIFE) -> float:
    """``confidence = 1 - 0.5 ** (n_games / half_life)``（随局数上升、渐近 1）。"""
    try:
        n = float(n_games)
    except Exception:                   # noqa: BLE001
        return 0.0
    if n <= 0:
        return 0.0
    try:
        hl = float(half_life)
    except Exception:                   # noqa: BLE001
        hl = HALF_LIFE
    if hl <= 0:
        return 1.0
    return _clamp01(1.0 - 0.5 ** (n / hl))


def alpha_of(n_games, alpha_cap: float = ALPHA_CAP) -> float:
    """单局权重：``α = 1/(n_games+1)`` 但**不低于** ``alpha_cap``（``n_games`` ＝该维已有样本数）。

    ⇒ 首样本 α=1（直接取 ``x``）、第 2 次 0.5、第 3 次 1/3、**第 4 次起稳在 ``alpha_cap=0.25``**
    （任务书里逐字给出的那串数：1 / 0.5 / 1/3 / 0.25…）。

    **注意（任务书里两句话打架，这里按「那串数」实现）**：任务书写的是
    ``α = min(alpha_cap, 1/(n_games + 1))``，但 ``min`` 与它紧接着写的
    「首样本 α=1、第 2 次 0.5」互相矛盾——``min(0.25, 1/(n+1))`` 对任何 ``n`` 都恒等于 0.25
    （第 1 局就取不到 ``x``，``1/(n+1)`` 这一项形同虚设，新画像永远只有真实值的 25%）。
    本实现取 ``max``：它**恰好**给出任务书列出的 1 / 0.5 / 1/3 / 0.25… 这串数，
    也守住了门卫「单局 α 上限」的稳态口径（``n ≥ 3`` 起单局最多挪 25%）。
    若控制者确认要 ``min``（首样本 0.25、此后恒 0.25），把下面的 ``max`` 改回 ``min`` 即可，
    门卫用例 ``test_alpha_of_matches_pinned_formula`` 同步改。
    """
    try:
        n = int(n_games)
    except Exception:                   # noqa: BLE001
        n = 0
    if n < 0:
        n = 0
    cap = _clamp01(alpha_cap)
    return max(cap, 1.0 / (n + 1))


def merge(prev, x, n_games, *, half_life: float = HALF_LIFE, alpha_cap: float = ALPHA_CAP) -> dict:
    """把本局特征 ``x`` 融进上一版画像 ``prev``，返回**新画像**（纯函数、不改入参、可复现）。

    - ``prev``：上一版画像，``{axis: {"value","n_games",…}}``（``ProfileStore.get_profile()``
      的形状）或 ``{axis: 值}``（只有值的扁平形状）都收；``None`` / ``{}`` ＝首次。
    - ``x``：``extract_axes(rec)`` 的返回（``None`` / ``{}`` 也可以）。
    - ``n_games``：该维**已有的样本数**——可以是一个 ``int``（对所有维生效），也可以是
      ``{axis: int}``（按维给，优先于 ``prev`` 里记的 ``n_games``）。

    返回 ``{axis: {"value", "confidence", "n_games"}}``，形状与 ``upsert_axes`` 的「记录形状」
    一致（可直接 ``store.upsert_axes(merge(...))``）。规则：

    - 数值维：``v ← v*(1-α) + x*α``（首样本 α=1 ⇒ 直接取 ``x``），α 见 ``alpha_of``；
      值一律夹到 ``[0,1]``；``CORE_AXES`` 里的键**缺键＝当 0 参与融合**（任务书「缺维当 0」），
      脏值同样当 0；其余键（``persona.topic.*``）**缺键＝原样带过、不衰减**。
    - 非数值维（``blunder.top_type`` / ``persona.address_pref``）：**最近一次非空覆盖**，
      不做 EMA；空 / 缺＝未知＝**跳过不更新**（连 ``n_games`` 都不涨）。
    - ``n_games`` 每融合一次 +1（只在真的更新了该维时）。
    """
    prev_rec = _prev_records(prev)
    x_map = x if isinstance(x, dict) else {}
    counts = n_games if isinstance(n_games, dict) else {}
    scalar = None if isinstance(n_games, dict) else n_games
    out = {}
    for axis in sorted(set(prev_rec) | set(x_map)):
        old = prev_rec.get(axis)
        old_val = old[0] if old else None
        old_n = old[1] if old else 0
        if axis in counts:                  # 按维显式给的样本数最优先
            base_n = counts[axis]
        elif old and old[1]:                # 其次：prev 记录里带的样本数
            base_n = old[1]
        else:                               # 再次：标量（扁平 prev 没有样本数）
            base_n = scalar if scalar is not None else 0
        try:
            base_n = max(0, int(base_n))
        except Exception:               # noqa: BLE001
            base_n = old_n
        present = axis in x_map
        if not present and axis not in CORE_AXES and axis not in NON_NUMERIC_AXES:
            if old:                     # 结构上可缺的维（persona.topic.*）：原样带过
                out[axis] = {"value": old_val, "confidence": confidence_of(old_n, half_life),
                             "n_games": old_n}
            continue
        if axis in NON_NUMERIC_AXES:
            raw = x_map.get(axis) if present else None
            text = raw.strip() if isinstance(raw, str) else ""
            if text == "":
                if old:                 # 未知：不更新（值 / 计数 / 置信度都不动）
                    out[axis] = {"value": old_val, "confidence": confidence_of(old_n, half_life),
                                 "n_games": old_n}
                continue
            new_n = base_n + 1
            out[axis] = {"value": text, "confidence": confidence_of(new_n, half_life),
                         "n_games": new_n}
            continue
        xv = _clamp01(x_map.get(axis))  # 缺维 / 脏值 ⇒ 0
        new_n = base_n + 1
        if old is None:
            value = xv
        else:
            alpha = alpha_of(base_n, alpha_cap)
            value = _clamp01(_clamp01(old_val) * (1.0 - alpha) + xv * alpha)
        out[axis] = {"value": value, "confidence": confidence_of(new_n, half_life),
                     "n_games": new_n}
    return out


def _prev_records(prev) -> dict:
    """把 ``prev`` 归一化成 ``{axis: (值, n_games)}``（扁平 / 记录两种形状都收；脏值不抛）。"""
    out = {}
    if not isinstance(prev, dict):
        return out
    for axis, val in prev.items():
        name = str(axis or "")
        if name == "":
            continue
        if isinstance(val, dict):
            value = val.get("value")
            try:
                n = max(0, int(val.get("n_games", 0) or 0))
            except Exception:           # noqa: BLE001
                n = 0
        else:
            value, n = val, 0
        if value is None:
            continue
        out[name] = (value, n)
    return out
