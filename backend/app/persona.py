"""人格化状态的纯逻辑（无 IO、无时钟依赖、无随机副作用）。

本模块只负责「她怎么想、怎么称呼、怎么回忆」，持久化与注入由 ``ProfileStore`` /
``AzureAgent`` / ``agent_graph`` 负责。所有函数都是纯函数：时间、随机源、状态由调用方注入，
方便单测，也保证关掉人格化时整条链路逐字符不变。

五块内容：
- 关系阶段：按一起下过的局数（+ 少量互动）分档，给出一句**语气指令**，不直接念给玩家听；
- 情绪余温：上一局结束时的三维情绪按时间向基线回落，新局带一点回来（有逻辑、不硬编）；
- 情绪 → 语言节奏 / 回复方向：`v/c/a` 既决定「怎么说」，也决定「这句往哪儿说」，
  并维护「问过但没被回答的问题」——可以再问一次，绝不反复追问；
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


# ================= 情绪 → 回复方向 + 未答问题（v4.5.0，E） =================
#
# 不写「好奇高就说某句」这类硬分流：把 v / c / a 相对基线的偏离标准化后，作为**对话动作**
# 的对数几率（好奇→问、愉悦→分享、低落→接住、活跃→推进、安静→收），softmax 成分布；
# 只有概率高于「不特意做什么」的中性动作，才算这句的**方向**。未答问题也用压力值
# （pressure，按情绪分布给初值、每轮指数衰减、再问一次就减半）决定「能不能带回来」，
# 而不是写死「最多问两次」——压力落到下限以下就自然收住。基线且无积压 ⇒ 空串（提示词不变）。

# 情绪尺度的调参（不是判定阈值）：三轴「一个标准差」的感受幅度；增益控制分布的峰度。
_POLICY_SIGMA = {"v": 0.20, "c": 0.18, "a": 0.22}
_POLICY_GAIN = 1.0
# 方向的门槛来自分布本身：动作概率要比「不特意做什么」高出这么多归一化概率质量才算方向，
# 基线附近（情绪还没形成倾向）就什么都不写。
_POLICY_MARGIN = 0.08
# 问句预算上限：一次最多两件事（一件新问题 + 一件没答过的旧问题）。
_ASK_MAX = 2
# 追问压力：问过一次后每轮按「基础衰减 + 好奇加成」衰减；再问一次只留一半压力。
_REASK_DECAY_BASE = 0.45
_REASK_DECAY_GAIN = 0.35
_REASK_SATIATION = 0.5
_REASK_FLOOR = 0.18

# 话题片段（中文二元组）里不能当「主题」用的虚词 / 疑问词 / 对话动词：
# 只含这些字的二元组一律丢掉，否则「关于什么的呢」会被误当成一个新话题
# （它其实是在追问上一句，属于同一条未答问题）。
_Q_STOP_CHARS = set(
    "什么哪咋怎吗呢吧嘛呀哦啊嗯诶欸这那哪个些多少于关和与对把被让给从向在"
    "是有会能可要想说告诉还就都也才又很太更最没不别我你他她们问答应聊")

# 「卖什么 / 卖的是什么 / 去哪」这类疑问词前的内容字就是话题焦点
# （中间允许夹「的 / 是 / 了」这类虚词；单字也足以判断答没答）。
_FOCUS_RE = re.compile(r"(.)(?:的|是|了|着|过){0,2}\s*(?:什么|啥|哪些|哪个|哪样|怎么样|如何)")

# 明确的回避 / 不想说：算「已经回应过」，之后不再追问（也别道歉或解释）。
_REFUSE_RE = re.compile(
    r"(不想说|不想讲|不告诉你|不说了|别问|保密|关你.*事|没什么|不关你事|"
    r"以后再说|改天再说|再说吧|算了|不方便说|不想聊)")

# 「呢」既可以是疑问语气，也可以是「这个可是要好好上呢」这种陈述收尾：
# 只有句中出现疑问词时才把「…呢」当问句（问号仍然无条件算问句）。
_Q_WORD_RE = re.compile(
    r"(什么|啥|哪|怎么|为什么|为何|多少|几时|几点|是不是|有没有|要不要|会不会|"
    r"能不能|好不好|行不行|对吗|对吧)")

# 是非问句 + 很短的肯定 / 否定回答 ⇒ 算回答过（不再要求提到同一个话题词）。
_YES_NO_RE = re.compile(r"(吗|么|是不是|有没有|要不要|会不会|行不行|好不好)")
_SHORT_ANSWER_RE = re.compile(
    r"^(嗯|恩|嗯嗯|是|是的|对|对啊|好|好呀|行|可以|有|有的|有啊|没有|没|不是|不|不用)$")


def question_fragments(text, limit=2) -> list:
    """从一段话里取出**带具体话题**的问句（去掉「欸？」这类纯感叹）。

    只保留能当「未答问题」追踪的句子：有问号，或以「吗 / 么 / 嘛 / 呢」收尾且句中带
    疑问词（「这个可是要好好上呢」是陈述、不是问句）。像「关于什么的呢」「后来呢」
    这种没有新话题的追问会被跳过——它们本来就是上一句的续问，单独追踪只会重复追问。
    """
    raw = str(text or "")
    if raw.strip() == "":
        return []
    out = []
    for frag in re.findall(r"[^。！？!?\n]+[。！？!?]?", raw):
        f = frag.strip()
        if f == "":
            continue
        body = re.sub(r"[。！？!?；;，,、\s]+$", "", f).strip()
        if body == "":
            continue
        has_mark = ("？" in f) or ("?" in f)
        tail_q = (body.endswith(("吗", "么", "嘛"))
                  or (body.endswith("呢") and bool(_Q_WORD_RE.search(body))))
        if not has_mark and not tail_q:
            continue
        if not (_content_grams(body) or _focus_chars(body)):
            continue
        if len(body) < 3:
            continue
        if any(same_question(body, q) for q in out):
            continue
        out.append(body)
        if len(out) >= max(1, _as_int(limit) or 1):
            break
    return out


def answers_question(player_text, question_text) -> bool:
    """玩家这句话**是否算回答了**那个问题（纯规则，不调模型）。

    命中任一条即算答过：① 提到问题里的话题片段 / 疑问焦点；② 明确回避
    （「不想说」）；③ 是非问句收到很短的肯定 / 否定回答。
    """
    ans = str(player_text or "").strip()
    q = str(question_text or "").strip()
    if ans == "" or q == "":
        return False
    if _REFUSE_RE.search(ans):
        return True
    hay = ans.lower()
    if any(g in hay for g in _content_grams(q)):
        return True
    if any(ch in hay for ch in _focus_chars(q)):
        return True
    if _YES_NO_RE.search(q) and len(ans) <= 6 and _SHORT_ANSWER_RE.match(
            re.sub(r"[。！？!?，,、\s]+", "", ans)):
        return True
    return False


def same_question(a, b) -> bool:
    """两句话是不是**同一个问题**（用来合并重复追问，而不是又记一条）。"""
    ga, gb = _content_grams(a), _content_grams(b)
    if ga and gb:
        overlap = ga & gb
        if len(overlap) >= 2:
            return True
        if len(overlap) >= 1 and min(len(ga), len(gb)) <= 2:
            return True
    if _focus_chars(a) & _focus_chars(b):
        return True
    return False


def emotion_policy(mood) -> dict:
    """情绪三维 → 对话动作分布（纯算法，不是 if-else 硬分流）。

    v / c / a 相对基线 ``MOOD_BASELINE`` 的偏离先按各自的感受尺度标准化，作为五个动作的
    对数几率：``ask``（好奇）＝ +z_c、``share``（愉悦）＝ +z_v、``hold``（低落）＝ -z_v、
    ``push``（活跃）＝ +z_a、``quiet``（安静）＝ -z_a，另有一个 ``neutral``（不特意做什么）
    恒为 0。softmax 之后，只有概率**高于 neutral** 的动作才算这句的方向；两者之差归一化成
    ``excess``，同时决定问句预算（越好奇越多，最多两件）。数据脏 / 缺维 ⇒ ``{}``。
    """
    src = mood if isinstance(mood, dict) else {}
    try:
        v, c, a = float(src.get("v")), float(src.get("c")), float(src.get("a"))
    except (TypeError, ValueError):
        return {}
    if not all(math.isfinite(x) for x in (v, c, a)):
        return {}
    try:
        v0, c0, a0 = (float(x) for x in MOOD_BASELINE[:3])
    except (TypeError, ValueError):
        return {}

    def _z(x, zero, key):
        return (x - zero) / _POLICY_SIGMA[key]

    logits = {
        "ask": _z(c, c0, "c"),
        "share": _z(v, v0, "v"),
        "hold": -_z(v, v0, "v"),
        "push": _z(a, a0, "a"),
        "quiet": -_z(a, a0, "a"),
        "neutral": 0.0,
    }
    try:
        raw = {k: math.exp(_POLICY_GAIN * x) for k, x in logits.items()}
    except OverflowError:                   # 极端脏值（理论上到不了）也不抛
        raw = {k: (float("inf") if x > 0 else 0.0) for k, x in logits.items()}
    total = sum(raw.values())
    if not (total > 0.0) or not math.isfinite(total):
        return {}
    probs = {k: x / total for k, x in raw.items()}
    neutral = probs["neutral"]
    scale = max(1e-6, 1.0 - neutral)
    excess = {k: max(0.0, (p - neutral) / scale) for k, p in probs.items() if k != "neutral"}
    acts = sorted((k for k in excess if excess[k] >= _POLICY_MARGIN), key=lambda k: -excess[k])
    if "hold" in acts:
        # 情绪低落时先接住对方：再好奇也不在这句开新问题（同一个分布内部的冲突消解）。
        acts = [k for k in acts if k != "ask"]
    if "ask" in acts:
        ask_budget = min(_ASK_MAX, int(math.ceil(_ASK_MAX * excess["ask"])))
    else:
        ask_budget = 0
    return {"probs": probs, "excess": excess, "acts": acts[:2],
            "ask": float(excess.get("ask", 0.0)), "ask_budget": ask_budget}


def update_open_questions(pending, player_text, reply_text, mood=None) -> list:
    """推进「未答问题」状态机：答过 / 回避的移出，压力衰减到下限以下就收住。

    - 玩家回答了（或明确说不想说）⇒ 直接移出；
    - 没答 ⇒ ``pressure *= 0.45 + 0.35 * ask``（越好奇衰减越慢），低于 ``_REASK_FLOOR`` 移出；
    - 这次回复里换了说法又问同一个问题 ⇒ 在**已衰减**的压力上再减半（问过就满足了一部分），
      不重复记一条、也不会因为「又问了一次」把压力刷新回满；
    - 新问题 ⇒ 压力 = 当前好奇质量（不刻意问时压力本来就很低，下一轮自然消失）；
    - 最多留两条，旧的在前：先解决旧问题，下一轮自然轮到「卖东西」这类旧话题。
    """
    policy = emotion_policy(mood)
    ask = float(policy.get("ask", 0.0)) if policy else 0.0
    decay = _REASK_DECAY_BASE + _REASK_DECAY_GAIN * ask
    player = str(player_text or "")
    rows = []
    for p in pending or []:
        if not isinstance(p, dict):
            continue
        q = str(p.get("q") or "").strip()
        if q == "" or answers_question(player, q):
            continue
        try:
            pressure = _clamp01(p.get("pressure", 0.0))
        except (TypeError, ValueError):
            pressure = 0.0
        rows.append({"q": q, "pressure": pressure * decay})
    for q in question_fragments(reply_text, limit=2):
        merged = None
        for row in rows:
            if same_question(q, row["q"]):
                merged = row
                break
        if merged is not None:
            merged["q"] = q                 # 用最新说法，下次更容易自然带回来
            merged["pressure"] = min(merged["pressure"], ask) * _REASK_SATIATION
            continue
        if ask >= _REASK_FLOOR:            # 没有好奇驱动时，问过的问题不进状态机
            rows.append({"q": q, "pressure": ask})
    return [r for r in rows if r["pressure"] >= _REASK_FLOOR][-2:]


def reply_direction_note(mood, pending=None) -> str:
    """情绪分布 + 未答问题的压力 → 一段**内容方向**提示；没有方向 ⇒ ``""``。

    与 ``emotion_language_note``（语气节奏）分开：这里只说「这句往哪儿聊」，
    所有措辞都由 ``emotion_policy`` / 压力值算出来的档位决定，不写任何示例台词，
    也绝不把情绪名或数值念出来。
    """
    policy = emotion_policy(mood)
    if not policy:
        return ""
    acts = list(policy.get("acts") or [])
    parts = []

    rows = [p for p in (pending or []) if isinstance(p, dict) and str(p.get("q") or "").strip()]
    reask = None
    for p in rows:
        try:
            pressure = _clamp01(p.get("pressure", 0.0))
        except (TypeError, ValueError):
            pressure = 0.0
        if pressure >= _REASK_FLOOR:
            reask = (str(p.get("q") or "").strip(), pressure)
            break
    budget = int(policy.get("ask_budget", 0) or 0)
    if reask is not None and budget > 0:
        budget -= 1                         # 带回来的旧问题占掉一个问句额度

    if "ask" in acts and budget > 0:
        parts.append("好奇心把这句话的重心推向「了解对方」：最多问 %d 件对方刚提到的事"
                     "（细节 / 后来 / 具体是哪一件），不猜自己的答案。" % budget)
    if reask is not None and "ask" in acts:
        parts.append("上一句问过「%s」，对方还没回答：可以换个说法自然带回来一次，"
                     "问的是同一件事；对方再不接就收住，不重复第二遍。" % reask[0])
    if "share" in acts:
        parts.append("心情把这句话推向「说一点自己」：可以带半句自己的感受或近况，"
                     "再顺着对方的话接一句，别只顾着问。")
    if "hold" in acts:
        parts.append("低落把这句话推向「先接住对方」：只回应这一句，不开新话题、不加问句。")
    if "push" in acts:
        parts.append("活跃把这句话推向「往前推」：给一个具体的下一步或小提议，别停在附和。")
    if "quiet" in acts:
        parts.append("安静把这句话收短：不主动推进话题，回应到位就停。")

    if not parts:
        return ""
    return ("【这句往哪儿说 · 按此刻情绪算出的方向，不要逐条念出来】\n"
            + "\n".join("· " + p for p in parts[:3])
            + "\n（仍用 Azure 平时干脆、有分寸的口吻；问句件数按上面的数，对方不接就收住。）")


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


def _content_grams(text) -> set:
    """可当话题用的片段：中文二元组（滤掉虚词 / 疑问词）+ 3 字母以上的英文词。"""
    t = str(text or "").strip().lower()
    grams = set()
    if t == "":
        return grams
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g.strip() == "" or any(ch in _Q_STOP_CHARS for ch in g):
            continue
        if re.match(r"^[\u4e00-\u9fff]{2}$", g):
            grams.add(g)
    for word in re.findall(r"[a-z]{3,}", t):
        grams.add(word)
    return grams


def _focus_chars(text) -> set:
    """疑问词前的那个内容字（「卖什么」→ 卖）：单字也足以判断答没答。"""
    out = set()
    for ch in _FOCUS_RE.findall(str(text or "")):
        if re.match(r"^[\u4e00-\u9fff]$", ch) and ch not in _Q_STOP_CHARS:
            out.add(ch)
    return out


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
