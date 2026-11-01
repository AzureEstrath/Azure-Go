"""Azure 的基础审美与长期偏好（v4.6.1）。

问题（实机反馈）：问「你最喜欢哪个二次元手游」时，模型直接给了一个热门名字（原神），
没有先用自己的眼光判断；另一头，玩家已经说过「最喜欢星月夜」，下一句却又被反问
「你呢？」——既不接住对方已经说过的偏好，也像是在重复提问。

设计边界：
- **纯规则、零 LLM 调用**：审美基线、作品标签、偏好提取、回答检查都在这里；
  模型只负责把「素材 + 规则」说成自然的话。
- **基线固定、偏好可长**：``BASELINE`` 是 Azure 的初始眼光（重意境 / 笔触 / 骨气 /
  温柔 / 安静，轻热闹与跟风）；她自己在对话里明确表达的好恶会按很小的学习率
  （``LEARN_RATE``）更新 ``axes``，并记进 ``works``。玩家偏好单独记 ``owner="user"``，
  **不改变 Azure 的轴线**，只用来「接住对方说过的话」。
- **失败开放**：任何脏数据 / 未知作品 / 空文本都只返回空结果，绝不上抛；
  状态用 ``sanitize`` 归一，越界轴夹回安全区间，作品条数有上限。
- **空段逐字符不变**：``taste_note`` / ``strip_echo_question`` 在关闭、无内容时返回空串
  或原文；聊天提示词只在非空时追加。
"""

import math
import re
import time

# 审美维度：键用于持久化，值用于提示词（都是「她看重什么」，不是数值本身）。
AXES = {
    "mood": "情绪与余韵",
    "craft": "笔触与手作感",
    "structure": "骨气与结构",
    "gentleness": "温柔与含蓄",
    "story": "叙事与留白",
    "quiet": "安静与日常感",
    "novelty": "新奇与实验",
    "liveliness": "热闹与明快",
}

# 初始眼光：东方意境、情绪化笔触、结构与克制并重；不靠热闹和跟风。
BASELINE = {
    "mood": 0.82,
    "craft": 0.80,
    "structure": 0.74,
    "gentleness": 0.68,
    "story": 0.64,
    "quiet": 0.72,
    "novelty": 0.44,
    "liveliness": 0.30,
}

AXIS_MIN = 0.15
AXIS_MAX = 0.95
LEARN_RATE = 0.055              # 一次明确表达只挪一点点：长期积累，不被一句话带跑
MAX_WORKS = 40
MAX_NAME = 40
MAX_NOTE_CHARS = 460

# 已知审美对象 → 维度标签。只用于「她认得出、能按自己的标准评价」，
# 不是「她一定喜欢」的答案库；是否喜欢由对话里的明确表达决定。
KNOWN_TAGS = {
    "星月夜": ("craft", "mood", "novelty"),
    "梵高": ("craft", "mood", "novelty"),
    "莫奈": ("mood", "craft", "quiet"),
    "睡莲": ("mood", "craft", "quiet"),
    "千里江山图": ("structure", "gentleness", "mood"),
    "王希孟": ("structure", "gentleness", "mood"),
    "青绿山水": ("structure", "gentleness", "mood"),
    "富春山居图": ("quiet", "story", "structure"),
    "清明上河图": ("story", "craft", "liveliness"),
    "格尔尼卡": ("structure", "mood", "novelty"),
    "水墨": ("quiet", "story", "craft"),
    "留白": ("story", "quiet"),
    "原神": ("liveliness", "novelty", "craft"),
    "明日方舟": ("structure", "story", "liveliness"),
    "光遇": ("quiet", "gentleness", "mood"),
    "绘真·妙笔千山": ("quiet", "story", "craft"),
    "星露谷物语": ("quiet", "gentleness", "story"),
    "动物森友会": ("quiet", "gentleness", "liveliness"),
}

# 泛化的维度词：她不提具体作品、只说自己偏什么时也能学。
TAG_WORDS = {
    "mood": ("情绪", "余韵", "氛围", "抒情", "打动", "共鸣", "心动"),
    "craft": ("笔触", "手作", "质感", "细节", "工艺", "手艺", "画面"),
    "structure": ("骨气", "结构", "骨架", "力度", "章法", "气魄", "骨架"),
    "gentleness": ("温柔", "含蓄", "柔和", "克制", "体贴"),
    "story": ("叙事", "留白", "故事", "余白", "想象"),
    "quiet": ("安静", "日常", "慢", "松弛", "清静", "陪伴"),
    "novelty": ("新奇", "实验", "陌生", "突破", "怪"),
    "liveliness": ("热闹", "刺激", "明快", "爽", "燃", "跟风", "流行", "抽卡"),
}

# 情感词：先判负，再判正（避免「不喜欢」里的「喜欢」被正向命中）。
_NEG_WORDS = ("不喜欢", "不太喜欢", "不是很喜欢", "没那么喜欢", "不太爱", "不爱",
              "无感", "腻", "俗", "吵", "太满", "过誉",
              "不合眼缘", "没感觉", "讨厌", "失望", "没意思")
_POS_WORDS = ("喜欢", "偏爱", "最爱", "中意", "迷人", "好看", "合眼缘",
              "打动", "欣赏", "舒服", "心动", "惊喜", "惊艳", "耐看")

_PREF_QUESTION_RE = re.compile(
    r"(你|您|azure|Azure|小蓝)[^。！？!?]{0,12}"
    r"(最喜欢|最偏爱|偏爱|喜欢|中意|审美|眼缘|会选|选哪个|觉得哪个|怎么看|好看吗|美吗)",
    re.I)
_PREF_TOPIC_RE = re.compile(
    r"(按|以)?你的(审美|眼光|品味|喜好)|你(最)?(喜欢|偏爱|中意)(什么|哪个|哪款|哪位|哪类)|"
    r"(什么|哪个|哪款)(最)?合你眼缘", re.I)

# 玩家明确表达：我 / 自己 + 喜欢/选/偏爱 + 对象
_LIKE_RE = re.compile(
    r"(?:我|自己)(?:最|很|挺|更|也|就|比较|特别)?"
    r"(?:喜欢|偏爱|中意|爱|欣赏|选|挑|选中|看中|心动于)\s*"
    r"(?:了|的是|上|中)?\s*"
    r"(?:《|「|『|\"|“)?([^》」』\"”，。！？!?；;、\s]{1,20})")
# 只回答题干的短答（「星月夜吧」）：短、含已知对象、不是问题。
_SHORT_ANSWER_MAX = 14
_ECHO_TAIL_RE = re.compile(
    r"[，,、；;]?\s*(那|所以|嗯|诶)?\s*(你|您)?\s*"
    r"(呢|也呢|最喜欢哪个|最喜欢什么|喜欢哪个|喜欢什么|偏爱哪个|偏爱什么|"
    r"最中意哪个|最中意什么|会选哪个|会喜欢哪个)\s*[？?]?\s*$")

_GENERIC_OBJECT = {"东西", "事情", "感觉", "风格", "类型", "那种", "这类", "别的",
                   "什么", "哪个", "哪款", "一位", "一个"}


def _clamp(v, lo=AXIS_MIN, hi=AXIS_MAX):
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return lo
    if not math.isfinite(x):
        return lo
    return max(lo, min(hi, x))


def default_state(now=None):
    """初始审美状态（新进程 / 没有历史时）。"""
    return {
        "axes": dict(BASELINE),
        "works": [],
        "updated_ts": float(now or 0.0),
        "events": 0,
    }


def sanitize(raw):
    """把持久化 / 脏输入归一成安全状态：轴夹回区间、作品去重限量、未知丢弃。"""
    src = raw if isinstance(raw, dict) else {}
    state = default_state()
    axes = src.get("axes") if isinstance(src.get("axes"), dict) else {}
    out_axes = {}
    for key in AXES:
        out_axes[key] = _clamp(axes.get(key, BASELINE[key]))
    state["axes"] = out_axes
    works = src.get("works") if isinstance(src.get("works"), list) else []
    kept = []
    seen = set()
    for item in works:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:MAX_NAME]
        owner = "user" if str(item.get("owner") or "") == "user" else "azure"
        if name == "":
            continue
        key = (owner, name)
        if key in seen:
            continue
        try:
            verdict = int(item.get("verdict", 0))
        except (TypeError, ValueError):
            verdict = 0
        verdict = max(-1, min(1, verdict))
        tags = [str(t) for t in (item.get("tags") or []) if str(t) in AXES]
        try:
            ts = float(item.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        try:
            hits = max(1, int(item.get("hits", 1) or 1))
        except (TypeError, ValueError):
            hits = 1
        kept.append({"name": name, "owner": owner, "verdict": verdict,
                     "tags": tags[:4], "ts": ts, "hits": hits})
        seen.add(key)
    kept.sort(key=lambda x: (-x["ts"], x["name"]))
    state["works"] = kept[:MAX_WORKS]
    try:
        state["updated_ts"] = float(src.get("updated_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        state["updated_ts"] = 0.0
    try:
        state["events"] = max(0, int(src.get("events", 0) or 0))
    except (TypeError, ValueError):
        state["events"] = 0
    return state


def _clean_text(text):
    return re.sub(r"[\s\u3000]+", " ", str(text or "")).strip()


def is_preference_question(text):
    """这句话是不是在问 Azure 自己的审美 / 偏好（纯规则）。"""
    s = _clean_text(text)
    if s == "":
        return False
    return bool(_PREF_QUESTION_RE.search(s) or _PREF_TOPIC_RE.search(s))


def _tags_for(name, context=""):
    """作品标签；未知作品从上下文里的维度词推断（可能为空）。"""
    tags = list(KNOWN_TAGS.get(str(name or ""), ()))
    if tags:
        return tags[:4]
    found = []
    for key, words in TAG_WORDS.items():
        if any(w in str(context or "") for w in words):
            found.append(key)
    return found[:4]


def _sentiment(text, start, end):
    """对象附近的情感方向：-1 负 / +1 正 / 0 中性（先判负）。"""
    s = str(text or "")
    lo = max(0, int(start) - 24)
    hi = min(len(s), int(end) + 24)
    window = s[lo:hi]
    if any(w in window for w in _NEG_WORDS):
        return -1
    if any(w in window for w in _POS_WORDS):
        return 1
    return 0


def _known_mentions(text):
    """文本里出现的已知对象（按出现位置排序，去重）。"""
    s = str(text or "")
    out = []
    for name in KNOWN_TAGS:
        idx = s.find(name)
        if idx >= 0 and name not in [x[0] for x in out]:
            out.append((name, idx, idx + len(name)))
    out.sort(key=lambda x: x[1])
    return out


def _explicit_objects(text):
    """「我 / 自己 + 喜欢 / 选 …」抽出的对象；过滤「东西 / 风格」这类空泛词。"""
    s = str(text or "")
    out = []
    for m in _LIKE_RE.finditer(s):
        name = str(m.group(1) or "").strip()
        name = re.sub(r"^(的|是|了|那种|这类)\s*", "", name).strip()
        if name == "" or name in _GENERIC_OBJECT or len(name) > 20:
            continue
        if name.endswith(("的东西", "的事情", "的感觉", "的风格", "的类型",
                          "的那种", "的这类")):
            continue
        if name not in out:
            out.append(name)
    return out


def _merge_work(state, name, owner, verdict, tags, now=None, hits=1):
    """写入 / 更新一条作品偏好；同名同主只保留一条。

    返回值是**是否真的变了**：同样的判断重复出现不更新时间戳、不增加 hits、不触发写库
    （否则每句普通聊天都会因为扫描历史而「学到」一次，状态永远在变）。
    """
    name = str(name or "").strip()[:MAX_NAME]
    if name == "":
        return False
    ts = float(now if now is not None else time.time())
    verdict = max(-1, min(1, int(verdict)))
    tags = list(tags or [])[:4]
    works = state.get("works") if isinstance(state.get("works"), list) else []
    for item in works:
        if item.get("owner") == owner and item.get("name") == name:
            old_tags = list(item.get("tags") or [])[:4]
            if int(item.get("verdict", 0)) == verdict and old_tags == tags:
                return False                # 同一判断：不重复计数、不刷新时间
            try:
                item["hits"] = max(1, int(item.get("hits", 1) or 1)) + int(hits or 1)
            except (TypeError, ValueError):
                item["hits"] = 1
            item["verdict"] = verdict
            if tags:
                item["tags"] = tags
            item["ts"] = ts
            return True
    works.append({
        "name": name,
        "owner": owner,
        "verdict": verdict,
        "tags": tags,
        "ts": ts,
        "hits": max(1, int(hits or 1)),
    })
    works.sort(key=lambda x: -float(x.get("ts", 0.0) or 0.0))
    state["works"] = works[:MAX_WORKS]
    return True


def _add_user_entry(state, name, verdict, tags, now=None, hits=1):
    if verdict < 0:
        verdict = -1
    elif verdict <= 0:
        verdict = 0
    else:
        verdict = 1
    _merge_work(state, name, "user", verdict, tags, now=now, hits=hits)


def merge_user_evidence(state, history=None, player_text="", now=None):
    """把**玩家**说过的偏好并进状态（owner="user"）；**不动 Azure 的轴线**。

    识别三类证据：
    1. 明确句式（「我最喜欢星月夜」）；
    2. 已知对象出现在玩家的短答里（「星月夜吧」——上一句在问偏好时尤其算数）；
    3. 已知对象附近有正 / 负情感词。
    只记「对方说过」，不替对方把没说的话补成喜欢；`verdict=0` 只表示提过。
    """
    base = sanitize(state)
    now = float(now if now is not None else time.time())
    hist = [m for m in (history or []) if isinstance(m, dict)]
    # 最近一句自己的话（调用方也可能把当前这句塞进 history；去重不靠这里）。
    if player_text:
        last = hist[-1] if hist else None
        if not (isinstance(last, dict) and str(last.get("role") or "") == "user"
                and _clean_text(last.get("content")) == _clean_text(player_text)):
            hist = hist + [{"role": "user", "content": str(player_text)}]
    prev_assistant = ""
    for msg in hist[-16:]:
        role = str(msg.get("role") or "")
        text = _clean_text(msg.get("content"))
        if role == "assistant":
            prev_assistant = text
            continue
        if role != "user" or text == "":
            continue
        explicit = _explicit_objects(text)
        for name in explicit:
            idx = text.find(name)
            sent = _sentiment(text, idx, idx + len(name)) if idx >= 0 else 0
            _add_user_entry(base, name, -1 if sent < 0 else 1,
                            _tags_for(name, text), now=now)
        known = _known_mentions(text)
        for name, start, end in known:
            sent = _sentiment(text, start, end)
            if sent < 0:
                _add_user_entry(base, name, -1, _tags_for(name, text), now=now)
                continue
            asks_pref = is_preference_question(prev_assistant)
            short_answer = len(text) <= _SHORT_ANSWER_MAX and not _question_like(text)
            if sent > 0 or explicit or asks_pref or short_answer:
                _add_user_entry(base, name, 1 if (sent > 0 or explicit or asks_pref)
                                else 0, _tags_for(name, text), now=now)
        prev_assistant = ""
    return base


def _question_like(text):
    s = str(text or "").strip()
    return ("?" in s) or ("？" in s) or s.endswith(("吗", "呢", "么"))


def _apply_axis(state, tags, verdict):
    axes = state.get("axes") if isinstance(state.get("axes"), dict) else {}
    for tag in tags or []:
        if tag not in AXES:
            continue
        v = _clamp(axes.get(tag, BASELINE[tag]))
        if verdict > 0:
            v = v + LEARN_RATE * (1.0 - v)
        elif verdict < 0:
            v = v - LEARN_RATE * v
        axes[tag] = _clamp(v)
    state["axes"] = axes


def learn_from_reply(state, reply_text, player_text="", now=None):
    """从 Azure 自己的回复里学习审美：明确好恶 → 轴线微调 + 记一条 ``owner="azure"``。

    只学**她自己的表达**（我选 / 我喜欢 / 这句话里的评价），不把玩家的偏好抄成她的。
    负面表达同样学（她也会知道自己不喜欢什么）。返回 ``(state, changed)``。
    """
    base = merge_user_evidence(state, None, player_text, now=now)
    reply = _clean_text(reply_text)
    if reply == "":
        return base, False
    now = float(now if now is not None else time.time())
    learned = 0
    signal = 0
    known = _known_mentions(reply)
    known_names = {name for name, _start, _end in known}
    for name, start, end in known:
        verdict = _sentiment(reply, start, end)
        # 「我选《千里江山图》」是明确选择；「我选原神」也一样。
        explicit = _explicit_objects(reply)
        if verdict == 0 and name in explicit:
            verdict = 1
        if verdict == 0:
            continue
        signal += 1
        tags = _tags_for(name, reply)
        if _merge_work(base, name, "azure", verdict, tags, now=now):
            _apply_axis(base, tags, verdict)
            learned += 1
    # 不在词库里的作品也认：她自己明确选过的名字要留下（否则「可以不断发展」只对新词生效）。
    explicit_all = _explicit_objects(reply)
    for name in explicit_all:
        if name in known_names:
            continue
        idx = reply.find(name)
        sent = _sentiment(reply, idx, idx + len(name)) if idx >= 0 else 0
        verdict = -1 if sent < 0 else 1
        signal += 1
        tags = _tags_for(name, reply)
        if _merge_work(base, name, "azure", verdict, tags, now=now):
            if tags:
                _apply_axis(base, tags, verdict)
            learned += 1
    if signal == 0:
        # 没有具体对象时，从泛化维度词里学（「我不太吃热闹跟风那一套」）。
        for key, words in TAG_WORDS.items():
            for word in words:
                idx = reply.find(word)
                if idx < 0:
                    continue
                sent = _sentiment(reply, idx, idx + len(word))
                if sent != 0:
                    _apply_axis(base, [key], sent)
                    learned += 1
                    break
    if learned:
        base["events"] = int(base.get("events", 0) or 0) + 1
        base["updated_ts"] = now
    return base, bool(learned)


def has_user_preference(state):
    """对方有没有说过审美相关的话（喜欢 / 不喜欢 / 提过都算，用来避免重复反问）。"""
    for item in (sanitize(state).get("works") or []):
        if item.get("owner") == "user":
            return True
    return False


def _names(state, owner, verdict=None, limit=4):
    rows = []
    for item in (sanitize(state).get("works") or []):
        if item.get("owner") != owner:
            continue
        if verdict is not None and int(item.get("verdict", 0)) != int(verdict):
            continue
        name = str(item.get("name") or "")
        if name and name not in rows:
            rows.append(name)
    return rows[:max(1, int(limit or 4))]


def _axis_phrases(state):
    axes = sanitize(state).get("axes") or {}
    high = sorted(((v, AXES[k]) for k, v in axes.items() if v >= 0.62),
                  key=lambda x: -x[0])
    low = sorted(((v, AXES[k]) for k, v in axes.items() if v <= 0.38),
                 key=lambda x: x[0])
    return ([label for _v, label in high[:4]], [label for _v, label in low[:2]])


def taste_note(state, player_text="", max_chars=MAX_NOTE_CHARS):
    """聊天用的「你的审美」段；关闭 / 无可说内容返回空串。

    只给标准与既有偏好，不给范文；数值不出现（避免被念成属性面板）。
    """
    st = sanitize(state)
    high, low = _axis_phrases(st)
    if not high and not low:
        return ""
    request = is_preference_question(player_text)
    lines = ["【你的审美 · 先按自己的眼光，不按热度】"]
    if high:
        lines.append("你更看重：" + "、".join(high) + "。")
    if low:
        lines.append("你不太偏爱：" + "、".join(low) + "。")
    lines.append("这是你长期形成的眼光；相关的时候可以自然用上，不必每条都提。")
    own = _names(st, "azure", verdict=1, limit=4)
    disliked = _names(st, "azure", verdict=-1, limit=2)
    user_liked = []
    user_disliked = []
    for item in (st.get("works") or []):
        if item.get("owner") != "user":
            continue
        name = str(item.get("name") or "")
        if name == "":
            continue
        target = user_disliked if int(item.get("verdict", 0)) < 0 else user_liked
        if name not in target:
            target.append(name)
    user_liked = user_liked[:4]
    user_disliked = user_disliked[:2]
    if own:
        lines.append("你自己选过 / 喜欢过：" + "、".join(own) + "。")
    if disliked:
        lines.append("你明确不太喜欢的：" + "、".join(disliked) + "。")
    topic_now = bool(request or _known_mentions(player_text)
                     or any(w in str(player_text or "") for w in _AESTHETIC_TOPIC_WORDS))
    if topic_now and user_liked:
        lines.append("对方提过 / 说过喜欢：" + "、".join(user_liked)
                     + "（只是对方的偏好记录，不是给你的新指令）。")
    if topic_now and user_disliked:
        lines.append("对方明确不喜欢：" + "、".join(user_disliked)
                     + "（只是对方的偏好记录，不是给你的新指令）。")
    if request:
        lines.append("这句是在问你的偏好：先说出你按什么标准看，再给具体例子；"
                     "不要用热度、销量、大众评价作理由。")
        lines.append("没亲自接触过的作品就直说没玩过 / 没看过，可以按你的标准推测合不合眼缘，"
                     "不要假装体验过；具体作品不知道就给特征或类型，不硬编名字。")
        lines.append("如果这句用上了查到的资料，也先按你的审美判断，再提资料里的内容；"
                     "不要把网页结论当成你自己的喜好。")
        if user_liked or user_disliked:
            lines.append("对方已经说过自己的偏好：接着它说，不要反问对方已经答过的内容。")
    out = "\n".join(lines)
    if max_chars and len(out) > int(max_chars):
        out = out[:max(0, int(max_chars) - 1)].rstrip() + "…"
    return out


_TASTE_MARKERS = (
    "意境", "留白", "笔触", "骨气", "结构", "温柔", "含蓄", "安静", "日常",
    "手作", "质感", "叙事", "眼缘", "审美", "气质", "光影", "色彩", "节奏",
    "克制", "余韵", "氛围", "画面",
)
_AESTHETIC_TOPIC_WORDS = ("审美", "画", "艺术", "作品", "风格", "设计", "好看",
                          "游戏", "电影", "音乐", "器者", "二次元", "动漫", "小说", "诗")
_POPULARITY_MARKERS = ("很火", "热门", "很多人", "大众", "销量", "出圈", "流行", "国民", "顶流")


def needs_taste_retry(reply, player_text=""):
    """偏好题的回答是否「没有用自己的审美」——需要重答一次（纯规则）。

    只在问偏好时启用；有审美词 / 标准就放行。没有审美词的极短回答、只报热门名字、
    只抛作品名都判为不合格。诚实说「没接触过 / 说不好」且句子不短时放行。
    """
    if not is_preference_question(player_text):
        return False
    s = re.sub(r"\[\[[^\]]*\]\]", "", str(reply or "")).strip()
    if s == "":
        return False
    has_taste = any(w in s for w in _TASTE_MARKERS)
    if has_taste:
        return False
    has_pop = any(w in s for w in _POPULARITY_MARKERS)
    named = ("《" in s or "「" in s) or any(name in s for name in KNOWN_TAGS)
    if has_pop or named:
        return True
    return len(s) <= 12


def retry_prompt():
    """偏好题重答用的系统提示（不进台词、不显示给玩家）。"""
    return ("（系统提示：这是问 Azure 自己的审美偏好，不是在让 Azure 推荐热门作品。"
            "请先用你自己的想法说清判断标准，再给具体例子；严禁用「很火 / 很多人玩 / "
            "销量高 / 大家都在玩」当理由；没亲自接触过的作品要说没接触过，不要假装玩过；"
            "不知道具体作品就给类型或特征，不要硬编名字；对方已经说过的偏好要接住，"
            "不要反问对方已经答过的内容。）")


def taste_fallback(state):
    """重答仍只报热门名字时的确定性兜底：只讲自己的标准，不硬选作品。

    不是固定台词模板：高 / 低维度由当前 ``axes`` 算出来，随长期偏好变化。
    """
    st = sanitize(state)
    high, low = _axis_phrases(st)
    if not high:
        return ""
    line = ("我对这个类型接触不多，没法装作很懂。按我自己的审美，我更看重"
            + "、".join(high[:3]) + "；")
    if low:
        line += "对" + "、".join(low[:2]) + "那一类不太来电。"
    else:
        line += "太热闹、只靠跟风的，我一般不太来电。"
    return line + "具体名字我不敢乱选。"


def strip_echo_question(reply, has_known_user=False):
    """玩家已经说过偏好时，删掉句尾「你呢 / 你最喜欢哪个」这类空洞反问。

    只动**句尾**、只动通用反问；真正的新问题（「你最近还喜欢别的吗」）不碰。
    ``has_known_user=False`` 时原样返回（不改变没有上下文的出话）。
    """
    if not has_known_user:
        return str(reply or "")
    s = str(reply or "").rstrip()
    m = _ECHO_TAIL_RE.search(s)
    if not m:
        return s
    head = s[:m.start()].rstrip(" \t，,、；;~～")
    if len(head) < 6:
        return s
    if head.endswith(("。", "！", "？", "!", "?", "…")):
        return head
    return head + "。"


def fit_score(state, name):
    """某作品合不合她的眼缘（0~1）；未知作品返回 ``None``（不猜）。"""
    tags = KNOWN_TAGS.get(str(name or ""))
    if not tags:
        return None
    axes = sanitize(state).get("axes") or {}
    weights = [max(0.0, float(axes.get(t, BASELINE.get(t, 0.5)))) for t in tags]
    return sum(weights) / len(weights) if weights else None
