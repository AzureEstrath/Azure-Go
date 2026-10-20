"""玩家棋风画像的四维特征提取 + 指数衰减融合（**纯函数**：零 LLM、零引擎、零磁盘）。

用途：``summarize()``（手动结束）时从一条对局记录（``profile_store`` 的 ``games`` 行）里
抠出**本局**的四维特征 ``extract_axes(rec)``，再与上一版画像 ``merge(prev, x, n_games)``
融合成新画像，最后经 ``ProfileStore.upsert_axes`` 落盘（写入点与落盘都在调用方，本模块不碰盘）。

**控制者钉死的融合公式**（spec §五 A 的实现口径）：

- 融合：``v ← v*(1-α) + x*α``，**每个维度各自维护 ``n_games``**（该维已有的样本数）；
- **单局 α**（评审前修正定稿）：``n = 0 ⇒ α = 1``（首样本＝**初始化**，此时没有既有画像
  可被爆冷改写）；``n ≥ 1 ⇒ α = min(alpha_cap, max(1/(n+1), 1 - 0.5**(1/half_life)))``
  ——**上限是真上限**（``n ≥ 1`` 时 α 恒 ≤ ``alpha_cap=0.25``），且 ``1/(n+1)`` 衰减到
  **半衰期稳态步长** ``1 - 0.5**(1/half_life)``（``half_life=20`` ⇒ ≈0.0341）后**不再变小**
  （＝真正的指数遗忘，有效窗口 ≈ 半衰期；见 ``alpha_of``）；
- **置信度**：``confidence = 1 - 0.5 ** (n_games / half_life)``（随局数上升、渐近 1；
  ``n_games`` 取**融合后**的样本数）；
- 数值维的值**归一化到 ``[0,1]``**（写进提示词的也是这个刻度）。

**非数值维**（``blunder.top_type`` / ``persona.address_pref``）：值是**自然字符串**，
``merge`` **不做 EMA**——按「最近一次非空覆盖」，且**空 / 缺＝未知＝跳过不更新**
（不会用一局没数据的棋把学到的值擦掉）。其余维都是 ``[0,1]`` 的数值维。

**缺维一律「原样带过」、脏值当 0、绝不抛**：``extract_axes`` 对空 rec / 坏 JSON / 脏类型都能跑；
``merge`` 对**缺键**（``x`` 里没有这一维）**原样带过**——不衰减、不涨 ``n_games``、不改 ``confidence``
（「本局没量到」≠「本局量到 0」；口径与 ``blunder.*`` / ``weak.*`` / ``persona.topic.*`` 统一），
只有 ``x`` 里**有**的键才参与 EMA，脏值（非数字）当 0 参与、同样不抛。
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

# —— 维度键（spec §6.2） ——
OPEN_AXES = ("open.star", "open.komoku", "open.sansan", "open.mokuhazushi")
STYLE_AXES = ("style.aggression", "style.tenuki", "style.territory_vs_influence")
WEAK_AXES = ("weak.corner_tl", "weak.corner_tr", "weak.corner_bl", "weak.corner_br")
KAKARI_AXIS = "open.favorite_kakari"
BLUNDER_AXIS = "blunder.rate"
TOP_TYPE_AXIS = "blunder.top_type"
ADDRESS_AXIS = "persona.address_pref"
TOPIC_PREFIX = "persona.topic."

# 有信号时才产出的维（``extract_axes`` 的产出形状见那里；**缺键 ≠ 值为 0**）：
# ``ALWAYS_AXES`` 是「有资格产出的数值维」全集（没占到角 / 没识别出族 / 玩家不足 2 手时**不产出**），
# ``merge`` 对**任何缺键**一律原样带过（不衰减、不涨样本数）。
ALWAYS_AXES = OPEN_AXES + (KAKARI_AXIS,) + STYLE_AXES
# 只在**有逐手 ``drop`` 信号**时才产出的维（评审前修正：缺信号＝**不产出该键**，
# 不把「不知道」写成 0）：失误率 + 薄弱方位 + 失误手型。
DROP_AXES = (BLUNDER_AXIS,) + WEAK_AXES + (TOP_TYPE_AXIS,)
# 数值维全集（ALWAYS + DROP 里的数值维；``TOP_TYPE_AXIS`` 的**值是字符串**，只是也归在失误组）
CORE_AXES = ALWAYS_AXES + DROP_AXES
# 非数值维（值＝字符串，不做 EMA）
NON_NUMERIC_AXES = (TOP_TYPE_AXIS, ADDRESS_AXIS)

# 失误手型（spec §二「常亏手型（深入 / 无理 / 方向 / 随手）」）：本批口径下**只支持 3 类**
# ——「随手」需要**时间信号源**（每手用时对比），而「失误与薄弱方位」这一组的唯一信号源
# 是逐手 ``drop``，故词表里不留永远不出现的枚举值（免得 Q6 / Q7 以为它存在）。
BLUNDER_TYPES = ("深入", "无理", "方向")

# 四个角的键（顺序＝ tl / tr / bl / br）——用于把真亏手归到「薄弱方位」的象限
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


def _items_with_colors(rec) -> list:
    """``moves`` 的原始条目 + 解析出来的 ``(颜色, (x, y))``：``[(item, color, (x, y))]``（坏条目跳过）。

    ``_moves_of`` 与 ``_drops_of`` 共用这一处解析：**颜色推定只此一份**——没有颜色信息的
    条目（``["Q16", …]`` / 无 ``color`` 的 dict）按「黑先、黑白交替」推；坏坐标的条目直接丢掉。
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
        out.append((it, color, pos))
    return out


def _moves_of(rec) -> list:
    """把 ``rec["moves"]`` 解析成 ``[(颜色, x, y)]``（pass / 认不出来的条目**丢掉**）。

    接受四种形状（``profile_store`` 存的是 JSON 文本，调用方也可能给已解析对象）：
    ``[["B","Q16"],…]`` / ``[{"color":"B","gtp":"Q16","drop":0.2},…]`` /
    ``["Q16","D4",…]``（无颜色信息 ⇒ 按「黑先、黑白交替」推）。
    """
    return [(color, int(pos[0]), int(pos[1])) for _it, color, pos in _items_with_colors(rec)]


def _drops_of(rec) -> list:
    """本局**玩家真亏手**的 ``[(x, y, drop)]``（``drop > BLUNDER_DROP``）。

    唯一来源（评审前修正定稿）：``moves`` 里每条可选的 ``drop`` 字段
    （``{"color":"B","gtp":"Q16","drop":0.32}``，缺省 0）。**Q7 负责在写记录时把
    ``st.losses`` 的真亏手填进 ``moves`` 的条目**——本模块只读、不碰 ``agent.py``。

    **只认玩家（黑）的 ``drop``**（修复轮 1：与分母同源）——Azure 的条目即使带了 ``drop``
    也一律不计，免得契约一变就把她的亏手算成「玩家的失误」。
    """
    out = []
    for it, color, pos in _items_with_colors(rec):
        if color != "B" or not (isinstance(it, dict) and "drop" in it):
            continue
        try:
            drop = float(it["drop"])
        except Exception:               # noqa: BLE001
            continue
        if drop > BLUNDER_DROP:
            out.append((int(pos[0]), int(pos[1]), drop))
    return out


def _has_drop_data(rec) -> bool:
    """本局是否带了**逐手 ``drop`` 信号**（没有就整组「失误与薄弱方位」不产出）。"""
    if not isinstance(rec, dict):
        return False
    for it in _as_list(_load_json(rec.get("moves"), [])):
        if isinstance(it, dict) and "drop" in it:
            return True
    return False


# ================= 四维特征提取 =================

def extract_axes(game_rec) -> dict:
    """从一条对局记录里抠出**本局**的四维特征（spec §6.2 的键）；空 / 脏 rec 不抛（缺信号＝不产出该键）。

    返回 ``{axis: 值}``：数值维是 ``[0,1]`` 的 ``float``，``blunder.top_type`` /
    ``persona.address_pref`` 是**字符串**。产出形状分三类：

    - ``ALWAYS_AXES``（开局四类 + 挂角 + 行棋风格三项）**只在有信号时**产出（修复轮 1 / I-2）：
      「没占到任何角」⇒ 开局四类不产出、「没识别出挂角 / 守角族」⇒ ``open.favorite_kakari``
      不产出、「玩家不足 2 手」⇒ ``style.tenuki`` 不产出、「一手都没有」⇒ 三项风格与
      ``blunder.rate`` 都不产出——**缺键 ≠ 值为 0**（结构性 0.0 会被渲染侧念成「他爱先守角」
      「很少脱先」这种凭印象编的话）；
    - ``DROP_AXES``（``blunder.rate`` / ``weak.corner_*`` / ``blunder.top_type``）
      **只在 ``moves`` 带逐手 ``drop`` 信号时**才产出——缺信号＝**不发这些键**
      （不把「不知道」写成 0；Q7 负责写记录时把 ``st.losses`` 填进 ``moves``）；
    - ``persona.topic.*`` 只发本局出现过的主题；``persona.address_pref`` 目前**没有信号源**
      （钉死的十个键里没有称呼信息），故默认**不发这个键**，等后续给源。

    提取规则（**只读 spec §6.1 的那十个列** —— ``ts / move_count / final_black_wr /
    final_score / result / moves / per_move_ms / joseki_families / chat_topics / summary``
    —— **外加一个可选键 ``address_pref``**：``games`` 表里没有这一列，所以真实链路下
    ``persona.address_pref`` **永远不产出**，留着只是给后续的信号源一个入口；详见报告）：

    - ``open.star|komoku|sansan|mokuhazushi``：把每个角 6×6 方框里**玩家最早的那一手**
      按离角点的偏移分类（``(3,3)`` 星 / ``(2,3)|(3,2)`` 小目 / ``(2,2)`` 三三 /
      ``(2,4)|(4,2)`` 目外），只数玩家自己的**前 12 手**；值＝该类角数 / 有棋子的角数
      （「其它点」进分母不进分子，故四类之和 ≤ 1）。
    - ``open.favorite_kakari``：``joseki_families`` 里**挂角族**（族名含 ``kakari``）占
      「挂角 + 守角」（族名含 ``kakari`` / ``shimari``）的比例；没有族数据＝0。
    - ``style.aggression``：玩家落子时**与对方棋子正交相邻**（接触战）的手数占比。
    - ``style.tenuki``：玩家落子与**上一手**（任一方的最近一手有坐标的手）切比雪夫距离
      ≥ 4 的手数占比（首手没有上一手 ⇒ 不计）。
    - ``style.territory_vs_influence``：玩家每手「离四条边的**最小**距离 **/ 18**」的均值
      （0＝全在一路（实地）、天元 (9,9) ≈ 0.5（势力）；**值域实际约 ``[0, 0.5]``**——
      19 路的天元离边 9 格、除以 ``coords.SIZE - 1 = 18``，这就是 Q6 写提示词要用的刻度）。
    - ``blunder.rate``：``drop > 0.10`` 的**真亏手数 / 玩家手数**（分子分母同源：都只算玩家）。
    - ``blunder.top_type``：按**亏得最多那一手**的几何分型（与对方相邻→``无理``；
      两格内有两个以上对方子→``深入``；否则→``方向``）；**没有真亏手就不发这个键**。
    - ``weak.corner_tl|tr|bl|br``：真亏手按**所在象限**（19 路中心线分四块）计频率：
      值＝**该象限真亏数 / 玩家在该象限的落子数**（玩家在该象限没有落子＝0）。
    - ``persona.topic.*``：``chat_topics`` 里每个主题的段数占比（键名＝``persona.topic.<主题>``）。
    - ``persona.address_pref``：可选键 ``rec["address_pref"]`` 的非空字符串；没有就不发。
    """
    rec = game_rec if isinstance(game_rec, dict) else {}
    moves = _moves_of(rec)
    player = [(x, y) for c, x, y in moves if c == "B"]

    out = {axis: 0.0 for axis in ALWAYS_AXES}      # 一定产出的维（没数据＝0.0）

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

    # —— 失误与薄弱方位（评审前修正：**只在有逐手 drop 信号时**才产出这一组键） ——
    # 口径收口（修复轮 1）：这一组的**分子与分母同源、都只算玩家**——真亏手来自玩家的
    # ``drop``，那么分母也必须是**玩家自己的手数 / 玩家在该象限的落子数**；混进 Azure 的
    # 落子会把玩家的失误率系统性压低（这个值会经 Q6 进提示词给模型看）。
    if _has_drop_data(rec):
        blunders = _drops_of(rec)
        if n_player:
            out[BLUNDER_AXIS] = _clamp01(len(blunders) / float(n_player))
        if blunders:
            out[TOP_TYPE_AXIS] = _blunder_type(occ, blunders)
        for i, axis in enumerate(_CORNER_KEYS):               # 该象限真亏数 / **玩家**在该象限落子数
            placed = sum(1 for x, y in player if _corner_index(x, y) == i)
            hit = sum(1 for x, y, _d in blunders if _corner_index(x, y) == i)
            out[axis] = _clamp01(hit / float(placed)) if placed else 0.0

    # —— 结构性 0.0 的维**不产出**（修复轮 1 / I-2）——
    # `ALWAYS_AXES` 上面被无条件初始化成 0.0，但「**没有信号**」与「真的量到 0」在存储里长成同一个
    # 0.0，渲染侧（Q3 的画像块）就会把前者念成后者：绝大多数对局识别不出任何挂角 / 守角族 ⇒ 从第二局
    # 起每条提示词都断言「他爱先守角」；只落 1 手就被总结的玩家 ⇒ 被断言「很少脱先」。**缺键 ≠ 值为 0**
    # ——与 `blunder.*` / `weak.*` 的 `_has_drop_data` 同一做法（`merge` 对**任何缺键**都原样带过、
    # 不衰减也不涨样本数；`tests/golden/prompts.json` 与既有输出不受影响）。
    if not known:                                   # 玩家前 12 手没占到任何角：开局四类都没有信号
        for axis in OPEN_AXES:
            out.pop(axis, None)
    if kakari + shimari <= 0:                       # 没识别出任何挂角 / 守角族
        out.pop(KAKARI_AXIS, None)
    if not n_player:                                # 一手都没有：三项风格都没有信号
        for axis in STYLE_AXES:
            out.pop(axis, None)
    elif not tenuki_flags:                          # 玩家不足 2 手：脱先没有信号（首手无前一手）
        out.pop("style.tenuki", None)
    if not n_player:                                # 有 drop 信号但没有玩家手：失误率没有信号
        out.pop(BLUNDER_AXIS, None)

    # —— 性格与话题（``address_pref`` 现在没有信号源：推导不出来就**不产出该键**） ——
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


def _corner_index(x, y) -> int:
    """点落在哪个**象限**（19 路按中心线分四块：``x ≤ 9`` 为左、``y ≤ 9`` 为上）。

    返回下标对应 ``_CORNER_KEYS``：0=tl / 1=tr / 2=bl / 3=br（中心那一格 (9,9) 归 tl）。
    """
    half = (coords.SIZE - 1) // 2
    return (0 if y <= half else 2) + (0 if x <= half else 1)


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


def steady_alpha(half_life: float = HALF_LIFE) -> float:
    """半衰期对应的**稳态单局步长** ``1 - 0.5**(1/half_life)``（``half_life=20`` ⇒ ≈0.0341）。

    含义：样本多到 ``1/(n+1)`` 已经小于它之后，α 就**不再变小**——每局固定挪这么多，
    效果就是「有效窗口 ≈ 半衰期」的指数遗忘（而不是越挪越小的滑动平均）。
    ``half_life <= 0``（脏值）⇒ 稳态步长按 1.0（＝每局都按 ``alpha_cap`` 挪，无遗忘）。
    """
    try:
        hl = float(half_life)
    except Exception:                   # noqa: BLE001
        hl = HALF_LIFE
    if hl <= 0:
        return 1.0
    return _clamp01(1.0 - 0.5 ** (1.0 / hl))


def alpha_of(n_games, alpha_cap: float = ALPHA_CAP, half_life: float = HALF_LIFE) -> float:
    """单局权重（**评审前修正定稿**）：``n = 0 ⇒ 1.0``（首样本＝初始化，没有既有画像可被爆冷改写）；

    ``n ≥ 1 ⇒ α = min(alpha_cap, max(1/(n_games + 1), 1 - 0.5**(1/half_life)))``。

    - **上限是真上限**：``n ≥ 1`` 时 ``α ≤ alpha_cap``（默认 0.25）＝「防一局爆冷改写画像」；
    - **随样本增多单调不增**，且 ``1/(n+1)`` 衰减到**半衰期稳态步长** ``steady_alpha``
      后不再变小 ⇒ 真正的指数遗忘（``half_life=20``、``alpha_cap=0.25`` 时：
      ``n=1/2/3→0.25``、``n=4→0.2``、``n=9→0.1``、``n=28→≈0.0345``、``n=100→≈0.0341``）。
    """
    try:
        n = int(n_games)
    except Exception:                   # noqa: BLE001
        n = 0
    if n <= 0:
        return 1.0
    return min(_clamp01(alpha_cap), max(1.0 / (n + 1), steady_alpha(half_life)))


def merge(prev, x, n_games, *, half_life: float = HALF_LIFE, alpha_cap: float = ALPHA_CAP) -> dict:
    """把本局特征 ``x`` 融进上一版画像 ``prev``，返回**新画像**（纯函数、不改入参、可复现）。

    - ``prev``：上一版画像，``{axis: {"value","n_games",…}}``（``ProfileStore.get_profile()``
      的形状）或 ``{axis: 值}``（只有值的扁平形状）都收；``None`` / ``{}`` ＝首次。
    - ``x``：``extract_axes(rec)`` 的返回（``None`` / ``{}`` 也可以）。
    - ``n_games``：该维**已有的样本数**——可以是一个 ``int``（对所有维生效），也可以是
      ``{axis: int}``（按维给，优先于 ``prev`` 里记的 ``n_games``）。

    返回 ``{axis: {"value", "confidence", "n_games"}}``，形状与 ``upsert_axes`` 的「记录形状」
    一致（可直接 ``store.upsert_axes(merge(...))``）。规则：

    - 数值维：``v ← v*(1-α) + x*α``（首样本 α=1 ⇒ 直接取 ``x``），α 见 ``alpha_of``
      （``n=0⇒1``；``n≥1`` 时 ``min(alpha_cap, max(1/(n+1), 半衰期稳态))``）；
      值一律夹到 ``[0,1]``；
    - **缺键一律「原样带过」**（控制者裁决，v3.9.0 收口）：``x`` 里没有这一维 ⇒ 不衰减、
      不涨 ``n_games``、不改 ``confidence``，照原值带过；``x`` 里**有**的键才参与 EMA，
      脏值（非数字 / ``None``）当 0 参与。理由：「本局没量到」≠「本局量到 0」——
      ``extract_axes`` 已经不产出无信号的维（I-2），若这里再把缺键按 0 融合，
      某维一旦有过信号（如 ``open.favorite_kakari=1.0``）就会被其后几局「没识别出族」逐局拉向 0，
      渲染侧又会念出「更爱先守角」——I-2 的缺陷会经 ``merge`` 复发；
    - 非数值维（``blunder.top_type`` / ``persona.address_pref``）：**最近一次非空覆盖**，
      不做 EMA；空 / 缺＝未知＝**跳过不更新**（连 ``n_games`` 都不涨）。
    - ``n_games`` 每融合一次 +1（只在 ``x`` 里**有**这一维、真的更新了它时）。
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
        if not present:                     # 缺键：原样带过（不衰减、不涨计数、不改置信度）
            if old:
                out[axis] = {"value": old_val, "confidence": confidence_of(old_n, half_life),
                             "n_games": old_n}
            continue
        if axis in NON_NUMERIC_AXES:
            raw = x_map.get(axis)
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
        xv = _clamp01(x_map.get(axis))  # 脏值 ⇒ 0（缺键在上面就带过、走不到这里）
        new_n = base_n + 1
        if old is None:
            value = xv
        else:
            alpha = alpha_of(base_n, alpha_cap, half_life)
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
