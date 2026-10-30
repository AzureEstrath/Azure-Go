"""人格化状态的纯逻辑（无 IO、无时钟依赖、无随机副作用）。

本模块只负责「她怎么想、怎么称呼、怎么回忆」，持久化与注入由 ``ProfileStore`` /
``AzureAgent`` / ``agent_graph`` 负责。所有函数都是纯函数：时间、随机源、状态由调用方注入，
方便单测，也保证关掉人格化时整条链路逐字符不变。

四块内容：
- 关系阶段：按一起下过的局数（+ 少量互动）分档，给出一句**语气指令**，不直接念给玩家听；
- 情绪余温：上一局结束时的三维情绪按时间向基线回落，新局带一点回来（有逻辑、不硬编）；
- 小目标：每局开始小概率选一个「今天的小心思」，只影响语气与话题，不承诺做不到的事；
- 回忆文案：棋谱库某局的短回忆、复盘开场白、难度放轻时的半句提示。
"""

import math
import re
import time

from .emotion import BASELINE as MOOD_BASELINE

# ================= 关系阶段 =================

# (门槛分, key, 名称, 语气指令)。门槛分 = 一起下完的局数 + 互动加成（见 stage_of）。
# 语气指令里**不写死任何昵称**：称呼由 `relationship_note` 按配置的 address_name 生成。
STAGES = (
    (0, "first", "初识", "你们刚认识，保持礼貌和一点距离。"),
    (3, "familiar", "熟悉", "已经一起下过几盘，语气可以放松一点。"),
    (10, "close", "熟络", "下过不少盘了，可以更亲近一点，带一点玩笑。"),
    (30, "partner", "老搭档", "已经很熟了，语气可以很随意，偶尔撒娇或吐槽，但别腻。"),
)

# 互动（摸头等）对关系分的加成上限：不让狂点互动瞬间刷满关系
INTERACTION_CAP = 6


def relationship_stage(games_total=0, interactions_total=0) -> dict:
    """按局数与互动数返回关系阶段 ``{key,name,note,score,games}``（脏值按 0）。"""
    games = _as_int(games_total)
    inter = _as_int(interactions_total)
    score = games + min(INTERACTION_CAP, max(0, inter) // 8)
    picked = STAGES[0]
    for row in STAGES:
        if score >= row[0]:
            picked = row
    return {"key": picked[1], "name": picked[2], "note": picked[3],
            "score": score, "games": games}


def relationship_note(stage, user_name="玩家", address_name="") -> str:
    """关系阶段的提示词片段；初识且没下过棋 ⇒ 空串（不改变任何提示词）。

    ``address_name`` 为空 / 关系还没到熟络 ⇒ **始终用 ``user_name``**（默认玩家不变）；
    填了昵称且阶段到熟络 / 老搭档，才允许她偶尔改用这个称呼。昵称由玩家在设置里选。
    """
    if not isinstance(stage, dict):
        return ""
    try:
        games = int(stage.get("games", 0))
    except (TypeError, ValueError):
        games = 0
    if games <= 0:
        return ""
    name = str(stage.get("name") or "").strip()
    note = str(stage.get("note") or "").strip()
    if name == "" or note == "":
        return ""
    key = str(stage.get("key") or "")
    nick = str(address_name or "").strip()
    if key in ("close", "partner") and nick != "":
        if key == "close":
            addr = "偶尔可以叫%s「%s」，带一点亲近的玩笑。" % (user_name, nick)
        else:
            addr = "「%s」和「%s」可以自然切换。" % (user_name, nick)
    elif key == "first":
        addr = "称呼%s用「%s」，别太随意。" % (user_name, user_name)
    else:
        addr = "称呼仍用「%s」，偶尔可以直接说「你」。" % user_name
    return "你和%s的关系：%s（一起下过 %d 局）。%s%s" % (
        user_name, name, games, note, addr)


# ================= 情绪余温 =================

# 情绪向基线回落的时间常数（小时）：一天后约剩 40%，三天后基本平静
MOOD_HALF_LIFE_H = 30.0


def mood_after_break(mood, last_ts, now=None, half_life_h=MOOD_HALF_LIFE_H) -> dict:
    """上一局结束时的情绪 → 现在还剩多少（向基线回落）。取不到有效值 ⇒ ``{}``。

    ``mood`` 是 ``{"v","c","a"}``；``last_ts`` 是上一局结束的墙钟。保留系数
    ``keep = 0.5 ** (elapsed_hours / half_life)``：刚结束几乎原样，隔一天剩约四成，
    隔三天基本回到平静。**这不是随机波动**，只是「过了一段时间，情绪自然淡了」。
    """
    src = mood if isinstance(mood, dict) else {}
    try:
        v, c, a = float(src.get("v")), float(src.get("c")), float(src.get("a"))
    except (TypeError, ValueError):
        return {}
    if not all(math.isfinite(x) for x in (v, c, a)):
        return {}
    try:
        ts = float(last_ts)
    except (TypeError, ValueError):
        return {}
    if ts <= 0.0:
        return {}
    ref = time.time() if now is None else float(now)
    hours = max(0.0, (ref - ts) / 3600.0)
    keep = 0.5 ** (hours / max(1.0, float(half_life_h)))
    base = MOOD_BASELINE                           # 与 EmotionModel.BASELINE 同一份数字
    return {"v": _clamp01(base[0] + (v - base[0]) * keep),
            "c": _clamp01(base[1] + (c - base[1]) * keep),
            "a": _clamp01(base[2] + (a - base[2]) * keep)}


def memory_note(streak=0) -> str:
    """最近连续胜负的语气提示；不足两局 ⇒ 空串（不硬提）。"""
    n = _as_int(streak)
    if n >= 2:
        return "最近两局都是你赢；心里有点小得意，但别挂在嘴上，也别因此轻敌。"
    if n <= -2:
        return "最近两局你都输了；你有点在意，语气软一点，别反复提输赢，也别刻意安慰。"
    return ""


# ================= 情绪 → 语言节奏（v4.3.0，C） =================
#
# 只把**偏离基线较多**的情绪翻译成「句子长短 / 问句多少 / 语气快慢」，
# 基线附近一律空串：不改变提示词，也不让每句话都像在演状态。
# 措辞始终是 Azure 自己的口吻——这是语气指令，不是另一套人格。

def emotion_language_note(mood) -> str:
    """当前情绪 → 一句语言节奏提示；没有明显偏离 ⇒ ``""``（提示词逐字符不变）。

    ``mood`` 认 ``v`` / ``c`` / ``a``。规则刻意保守：低落 / 明亮、活跃 / 安静、好奇
    三组各取最多两条，避免语气指令互相打架。体力（energy）由 ``AzureAgent.life_note``
    单独负责，这里不重复写“累了”的提示。
    """
    src = mood if isinstance(mood, dict) else {}
    try:
        v, c, a = float(src.get("v")), float(src.get("c")), float(src.get("a"))
    except (TypeError, ValueError):
        return ""
    if not all(math.isfinite(x) for x in (v, c, a)):
        return ""

    parts = []
    if v <= 0.38:
        parts.append("心里有点沉：句子短一点，先接住对方；不必急着逗笑，也别连着追问。")
    elif v >= 0.70:
        parts.append("心情亮一些：语气可以轻快半拍，允许一个短感叹或小玩笑，但别连着抛。")
    if a >= 0.68:
        parts.append("精神比较活跃：可以多说半句自己的感受，问句最多一个。")
    elif a <= 0.24:
        parts.append("整个人偏静：语速和句子都放慢半拍，不用为了热络硬撑。")
    if c >= 0.68 and v >= 0.55:
        parts.append("好奇心正盛：可以顺着对方的话追问一句「后来呢」，但只问这一件。")
    if not parts:
        return ""
    return ("【此刻说话的节奏 · 只影响语气与长短，不要逐条念出来】\n"
            + "\n".join("· " + p for p in parts[:3])
            + "\n（你仍是平时的 Azure：克制、有分寸，不卖萌也不端着；别把情绪名或数值说出口。）")


def chat_budget(mood, life_fields=None) -> int:
    """聊天输出预算（tokens）：情绪低落 / 没精力时收一点，明亮活跃时给多一点。

    默认仍返回 400（与 v4.2.x 的聊天预算一致）；只在明显偏离时变到 340 / 440 / 460，
    让语言长度真的跟着情绪走，而不是只写在提示词里。
    """
    src = mood if isinstance(mood, dict) else {}
    try:
        v, c, a = float(src.get("v")), float(src.get("c")), float(src.get("a"))
    except (TypeError, ValueError):
        return 400
    if not all(math.isfinite(x) for x in (v, c, a)):
        return 400
    f = life_fields if isinstance(life_fields, dict) else {}
    try:
        energy = float(f.get("energy", 1.0))
    except (TypeError, ValueError):
        energy = 1.0
    if v <= 0.38 or a <= 0.24 or energy <= 0.28:
        return 340
    if v >= 0.70 and a >= 0.55:
        return 460
    if c >= 0.68:
        return 440
    return 400


# ================= 小目标 =================

INTENTS = (
    {"key": "quiet", "text": "今天想安静一点：句子短些、语气轻些，多听玩家说话。"},
    {"key": "curious", "text": "今天对玩家最近在忙什么有点好奇；合适时问一句，别追问。"},
    {"key": "focused", "text": "今天想认真看看玩家的布局；点评可以更具体一点，但别变成上课。"},
    {"key": "playful", "text": "今天心情轻快；偶尔可以开个小玩笑，别过火。"},
)


def pick_intent(rng, chance=0.25):
    """按概率挑一个「今天的小心思」；未命中 ⇒ ``None``（大多数局没有，避免每局都刻意）。"""
    try:
        p = float(chance)
    except (TypeError, ValueError):
        p = 0.25
    if p <= 0.0:
        return None
    try:
        if float(rng.random()) >= min(1.0, p):
            return None
    except Exception:                              # noqa: BLE001 —— 随机源坏了就不选，绝不抛
        return None
    try:
        item = INTENTS[int(rng.random() * len(INTENTS)) % len(INTENTS)]
    except Exception:                              # noqa: BLE001
        item = INTENTS[0]
    return {"key": item["key"], "text": item["text"]}


def intent_note(intent) -> str:
    """小目标 → 提示词片段；空 / 脏值 ⇒ 空串。"""
    if not isinstance(intent, dict):
        return ""
    text = str(intent.get("text") or "").strip()
    if text == "":
        return ""
    return "今天的小心思（只影响语气，不要直接宣布）：" + text


def intent_idle_hint(intent) -> str:
    """把「今天的小心思」落到**主动搭话**这一格；没有小目标 ⇒ 空串。"""
    if not isinstance(intent, dict):
        return ""
    key = str(intent.get("key") or "")
    return {
        "quiet": "如果开口，短一点，别追着聊；玩家没接话就收住。",
        "curious": "可以顺口问一句玩家最近在忙什么；玩家不想说就收住，别追问。",
        "focused": "可以提一句最近总在留意玩家的布局；别把问候变成上课。",
        "playful": "可以用一句轻松的小玩笑开场，别过火，也别硬逗。",
    }.get(key, "")


# ================= 时间 / 夜里 =================


def night_note(hour) -> str:
    """深夜 / 清晨的语气提示；白天 ⇒ 空串（不改变提示词）。"""
    h = _as_int(hour)
    if h >= 23 or h < 5:
        return "现在是深夜，你有点困：句子短一点、语气更轻；别每句都劝对方睡，只在合适时提一次。"
    if 5 <= h < 7:
        return "天还没大亮，你还没完全醒：反应可以慢半拍，语气软一点，别太兴奋。"
    return ""


# ================= 难度放轻的半句 =================


def adaptive_soft_note(old_level, new_level) -> str:
    """难度被放轻时给下一回合的一句「心里话」；升高 / 没变 ⇒ 空串（不炫耀、不刻意）。"""
    try:
        old, new = int(old_level), int(new_level)
    except (TypeError, ValueError):
        return ""
    if new >= old:
        return ""
    return ("（心里）刚才把难度放轻了一点。别直接宣布，也别解释机制；"
            "如果上下文合适，用半句带过，比如「这几手我慢一点」。下一回合之后就不要再提。")


# ================= 棋谱库回忆 / 复盘开场 =================

_WHEN = (
    (1.0, "今天"),
    (2.0, "昨天"),
    (7.0, "前几天"),
    (30.0, "上个月"),
    (365.0, "去年"),
)


def when_phrase(ts, now=None) -> str:
    """时间戳 → 「今天 / 昨天 / 前几天 / 上个月 / 去年 / 更早」；脏值 ⇒ 「之前」。"""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "之前"
    if t <= 0.0:
        return "之前"
    ref = time.time() if now is None else float(now)
    days = max(0.0, (ref - t) / 86400.0)
    for limit, word in _WHEN:
        if days < limit:
            return word
    return "更早以前"


def _first_sentence(text, limit=44) -> str:
    """摘要取第一句（中文句号 / 换行），压平空白并截断；空 ⇒ 空串。"""
    s = " ".join(str(text or "").split())
    if s == "":
        return ""
    for mark in ("。", "！", "？", "\n"):
        i = s.find(mark)
        if i > 0:
            s = s[: i + 1]
            break
    return s[:limit].strip()


def library_remark(rec, now=None) -> str:
    """棋谱库某局的短回忆（1~2 句，纯本地、不调模型）。

    只用记录里真实存在的字段（日期 / 手数 / 黑方胜率 / 摘要 / 来源），**不编造**对局内容。
    """
    r = rec if isinstance(rec, dict) else {}
    when = when_phrase(r.get("ts"), now)
    meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
    source = str(meta.get("source") or "")
    summary = _first_sentence(r.get("summary"))
    moves = _as_int(r.get("move_count"))
    wr = r.get("final_black_wr")

    if source == "import":
        line = "%s导进来的这盘棋谱，我还没陪你走过。" % when
        if moves > 0:
            line += "一共 %d 手，" % moves
        line += "要不要挑个时候一起看看？"
        return line

    bits = []
    if moves > 0:
        bits.append("%d 手" % moves)
    try:
        w = float(wr)
    except (TypeError, ValueError):
        w = None
    if w is not None:
        if w >= 0.62:
            bits.append("你那局下得挺顺")
        elif w >= 0.45:
            bits.append("那局咬得很紧")
        else:
            bits.append("那局你有点可惜")
    head = "%s那盘" % when
    if bits:
        head += "，" + "，".join(bits)
    head += "。"
    if summary != "":
        head += "我那时说过：" + summary
    else:
        head += "没留下总结，只记得盘面。"
    return head


def review_intro(move_count, losses, now_move_no=None) -> str:
    """普通复盘的开场白（纯本地、1~2 句）；无棋谱 ⇒ 温和提示。

    ``losses`` 是 ``st.losses`` 的形状（``[{"move_no","drop",...}]``）；只挑**真亏最多**的一手，
    说它在第几手、大约掉了多少，不编造具体棋形。
    """
    n = _as_int(move_count)
    if n <= 0:
        return "棋盘还空着，先落几手，我们再回头看。"
    worst = None
    for item in (losses if isinstance(losses, (list, tuple)) else []):
        if not isinstance(item, dict):
            continue
        try:
            mn = int(item.get("move_no"))
            drop = float(item.get("drop"))
        except (TypeError, ValueError):
            continue
        if mn <= 0 or drop <= 0.0:
            continue
        if worst is None or drop > worst[1]:
            worst = (mn, drop)
    if worst is None:
        return "来，我陪你从头看。这局没留下明显的亏损手，整体挺稳的。"
    pct = max(1, int(round(worst[1] * 100.0)))
    return "来，我陪你从头看。第 %d 手那下掉得最多（约 %d%%），先看那里？" % (worst[0], pct)


# ================= 窥屏隐私（语义过滤在 agent_graph，这里只给共用词表） =================

# 命中任一标记 ⇒ 整屏视为私密：不带标题、不带 OCR、不带画面描述，只留一句「没细看」
PRIVATE_MARKERS = (
    "密码", "password", "passwd", "验证码", "身份证", "银行卡", "信用卡", "支付", "付款",
    "转账", "余额", "账单", "工资", "病历", "诊断", "体检", "聊天记录", "私聊", "微信",
    "wechat", "qq", "邮箱", "邮件", "合同", "手机号", "住址", "收货地址", "账号",
)

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_LONG_ID_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def is_private_text(*parts) -> bool:
    """一段或多段文字里是否含私密标记（含手机号 / 邮箱 / 长数字串）。"""
    blob = " ".join(str(p or "") for p in parts).lower()
    if blob.strip() == "":
        return False
    if any(str(m).lower() in blob for m in PRIVATE_MARKERS):
        return True
    return bool(_PHONE_RE.search(blob) or _EMAIL_RE.search(blob) or _LONG_ID_RE.search(blob))


# ================= 内部 =================


def _as_int(v) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _clamp01(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))
