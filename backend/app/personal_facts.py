"""玩家私人事实守卫（v4.3.3）：防止模型替玩家编造「刚吃完晚饭」这类没有依据的经历。

**为什么需要这一层**：聊天上下文里有日程、记忆、画像，但没有「玩家今天吃过晚饭」这件事。
模型很容易把「吃饭」「饿了」「外卖」这些相近词拼成一个肯定句，听起来贴心，实际是无中生有。
只靠提示词写「不要编造」并不稳，所以在出话后做一次**逻辑核对**：

- **只认玩家亲口说过的话**（当前这句 + 最近聊天流水里的 ``user_text``），助手自己的旧回复
  不算证据——避免「她上次说过，于是这次接着编」；
- **时间只用来加权，不用来证明**：15:59 说「刚吃完晚饭」明显不合理，但即便在晚饭点，
  没有玩家原话也不能当成已知；
- **反向线索优先**：玩家刚说「饿了 / 不吃了 / 外卖退掉了」时，任何「你吃过了」都是错的；
- 核对失败先给模型一次按事实重说的机会；仍编造就用确定性兜底句收口，绝不把假事实写进
  聊天历史与长期记忆。

本模块是**纯逻辑**：不读库、不调模型、不依赖全局状态；时间与玩家原话都由调用方注入。
"""

import datetime
import math
import re

# 餐次词 → 槽位。只用于判断「说的是哪一顿」，不推断玩家吃没吃。
MEAL_SLOTS = {
    "早饭": "breakfast", "早餐": "breakfast", "早点": "breakfast",
    "午饭": "lunch", "午餐": "lunch", "中饭": "lunch",
    "晚饭": "dinner", "晚餐": "dinner",
    "夜宵": "late", "宵夜": "late",
}

# 完成态：吃 / 喝 + 了 / 过 / 完 / 好 / 饱。问句与否定句会先被排除。
_ATE_RE = re.compile(r"(吃|喝)(了|过|完|好|饱)")
# 否定 / 反向线索：这些词出现时，「刚吃过」不成立，或玩家明确在说没吃。
_MEAL_NEG_RE = re.compile(
    r"(饿了|没吃|还没吃|吃不下|没胃口|不饿|不吃|不吃了|退掉|退了|"
    r"取消.{0,8}(外卖|晚饭|饭)|(外卖|晚饭).{0,8}(取消|退掉))")
_QUESTION_RE = re.compile(r"(吗|么|？|\?|要不要|是不是|有没有|是否|该不该|想吃啥|吃什么)")
_SUBJECT_RE = re.compile(r"(你|您|玩家)")
_SELF_RE = re.compile(r"我(?!们)")
_WORK_RE = re.compile(r"(下班|上班|忙完|到家|回家|出门)")
_SLEEP_RE = re.compile(r"(睡醒|起床|熬夜|睡了|没睡|睡好)")
_HEALTH_RE = re.compile(r"(感冒|发烧|生病|不舒服|受伤|头疼|嗓子疼|肚子疼)")
# 明确取消的是「饭 / 外卖」而不是会议等别的安排；兜底句据此决定要不要提“取消”。
_MEAL_CANCEL_RE = re.compile(
    r"(取消|退掉|退了).{0,8}(外卖|晚饭|饭|餐|吃)|(外卖|晚饭|饭|餐|吃).{0,8}(取消|退掉|退了)")
# 反向线索只看最近几轮玩家发言；几周前说过「饿了」不能证明现在也没吃。
_NEG_WINDOW = 8

_MEAL_WINDOWS = {
    "breakfast": (5, 11),
    "lunch": (10, 15),
    "dinner": (16, 23),
    "late": (21, 24),
    "meal": (5, 23),
}


def _hour(now=None) -> int:
    """本地小时（0~23）；``now`` 可注入时间戳，脏值退回当前时间。"""
    try:
        ts = float(now) if now is not None else datetime.datetime.now().timestamp()
        if not math.isfinite(ts):
            raise ValueError
        return datetime.datetime.fromtimestamp(ts).hour
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.datetime.now().hour


def _clock(now=None) -> str:
    try:
        ts = float(now) if now is not None else datetime.datetime.now().timestamp()
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.datetime.now().strftime("%H:%M")


def _sentences(text) -> list:
    """按中文句末标点 / 省略号切句；保留原句（去掉首尾空白），空句丢掉。"""
    parts = re.split(r"[。！!？?；;\n…]+", str(text or ""))
    return [" ".join(p.split()) for p in parts if str(p).strip() != ""]


def _slot_of(sentence) -> str:
    for word, slot in MEAL_SLOTS.items():
        if word in sentence:
            return slot
    return "meal" if ("饭" in sentence or "吃" in sentence or "喝" in sentence) else ""


def _about_user(user_text) -> bool:
    """当前这句是不是在问玩家自己的事（吃饭 / 睡觉 / 忙 / 我）。"""
    return bool(re.search(r"(我|咱|今天|几点|什么时候|要不要|该不该|吃|喝|睡|忙|下班)", str(user_text or "")))


def collect_evidence(history=None, recent_chats=None, current_text="", now=None) -> dict:
    """从聊天历史 / 聊天流水里收集**玩家亲口说过的话**（新 → 旧），给核对用。

    - ``history`` 是 ``agent.st.chat_history`` 形状：``[{"role","content"}]``，只取 ``user``；
    - ``recent_chats`` 是 ``MemoryStore.recent_chats`` 形状：``[{"user_text",...}]``；
    - 当前这句单独传入，避免历史尚未追加时漏掉；
    - 去重保序（最新的在前）；任何脏输入都跳过，不抛异常。
    """
    out = []
    seen = set()

    def add(text):
        t = " ".join(str(text or "").split())
        if t == "" or t in seen:
            return
        seen.add(t)
        out.append(t)

    for h in reversed(list(history or [])):
        if isinstance(h, dict) and str(h.get("role", "")) == "user":
            add(h.get("content"))
    for row in reversed(list(recent_chats or [])):
        if isinstance(row, dict):
            add(row.get("user_text"))
    add(current_text)
    return {"texts": out, "now": float(now) if now is not None else datetime.datetime.now().timestamp(),
            "hour": _hour(now), "clock": _clock(now)}


def _recent_text(evidence) -> str:
    """最近几轮玩家原话拼在一起（反向线索只在这个窗口里找）。"""
    return " ".join(list((evidence or {}).get("texts") or [])[:_NEG_WINDOW])


def _evidence_sentences(evidence):
    """把证据拆成陈述句：问句不算证据（「我吃过了吗」不是在说她吃过了）。"""
    for t in list((evidence or {}).get("texts") or []):
        for s in _sentences(t):
            if _QUESTION_RE.search(s):
                continue
            yield s


def _user_ate(evidence, slot) -> str:
    """玩家是否亲口说过自己吃过这一顿；返回那句原话，找不到返回空串。"""
    for s in _evidence_sentences(evidence):
        if _MEAL_NEG_RE.search(s):          # 明确说没吃 / 饿了：这句不能当证据
            continue
        if not _ATE_RE.search(s):
            continue
        if slot in ("dinner", "lunch", "breakfast", "late"):
            words = [w for w, v in MEAL_SLOTS.items() if v == slot]
            if any(w in s for w in words) or "饭" in s:
                return s
        elif "饭" in s or "吃" in s or "喝" in s:
            return s
    return ""


def _user_said(evidence, pattern) -> str:
    """玩家是否亲口说过某类事（工作 / 睡眠 / 身体）；返回原话或空串。"""
    for s in _evidence_sentences(evidence):
        if pattern.search(s) and ("我" in s or "咱" in s):
            return s
    return ""


def _claim_meal(sentence, user_text) -> dict | None:
    if _QUESTION_RE.search(sentence):
        return None
    if _MEAL_NEG_RE.search(sentence):       # 「还没吃 / 不吃了」不是「刚吃过」型断言
        return None
    m = _ATE_RE.search(sentence)
    if m is None:
        return None
    slot = _slot_of(sentence)
    # 主语只在**动词之前**找：「我刚吃完晚饭，你呢」的主语是「我」（Azure 自己），
    # 句末的「你」只是反问，不能把它当成对玩家的断言。
    before = sentence[:m.start()]
    explicit = bool(_SUBJECT_RE.search(before))
    if slot == "" or (slot == "meal" and not explicit):
        return None
    if not explicit:
        if _SELF_RE.search(before):         # 「我刚吃完」是 Azure 自己，不是玩家事实
            return None
        if not _about_user(user_text):
            return None
    return {"kind": "meal", "slot": slot, "sentence": sentence, "negative_cue": False}


def _claim_other(sentence) -> dict | None:
    if _QUESTION_RE.search(sentence):
        return None
    if not _SUBJECT_RE.search(sentence):
        return None
    for kind, pattern in (("work", _WORK_RE), ("sleep", _SLEEP_RE), ("health", _HEALTH_RE)):
        if pattern.search(sentence):
            return {"kind": kind, "slot": "", "sentence": sentence, "negative_cue": False}
    return None


def unsupported(reply, evidence) -> dict | None:
    """找出第一句「替玩家编造、但没有任何玩家原话支持」的私人事实；没有返回 ``None``。"""
    ev = evidence if isinstance(evidence, dict) else {}
    texts = list(ev.get("texts") or [])
    user_text = " ".join(texts[:1]) if texts else ""
    for sentence in _sentences(reply):
        claim = _claim_meal(sentence, user_text) or _claim_other(sentence)
        if claim is None:
            continue
        kind = claim["kind"]
        if kind == "meal":
            if _user_ate(ev, claim["slot"]):
                continue
        elif kind == "work":
            if _user_said(ev, _WORK_RE):
                continue
        elif kind == "sleep":
            if _user_said(ev, _SLEEP_RE):
                continue
        elif kind == "health":
            if _user_said(ev, _HEALTH_RE):
                continue
        # 玩家刚说饿了 / 取消了外卖：即使没有「吃过」原话，也算反向线索
        claim["negative_cue"] = bool(_MEAL_NEG_RE.search(_recent_text(ev))) if kind == "meal" else False
        claim["reason"] = _reason(claim, ev)
        return claim
    return None


def _reason(claim, evidence) -> str:
    hour = int((evidence or {}).get("hour", _hour()))
    if claim["kind"] != "meal":
        return "no_source"
    if claim.get("negative_cue"):
        return "negative"
    slot = claim.get("slot", "meal")
    start, end = _MEAL_WINDOWS.get(slot, (5, 23))
    if not (start <= hour < end):
        return "time"
    return "no_source"


def correction_prompt(claim, evidence) -> str:
    """给模型的一句事实核对说明：只给事实，不规定具体措辞。"""
    ev = evidence if isinstance(evidence, dict) else {}
    clock = str(ev.get("clock") or _clock())
    slot_words = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭",
                  "late": "夜宵", "meal": "饭"}
    slot = slot_words.get(claim.get("slot", "meal"), "饭") if claim.get("kind") == "meal" else ""
    if claim.get("kind") == "meal":
        if claim.get("reason") == "negative":
            fact = "玩家刚说过自己饿了、没吃或把外卖退掉了，不能说他刚吃过%s。" % slot
        elif claim.get("reason") == "time":
            fact = "现在是 %s，不是%s时间；玩家也没有说过自己吃过%s。" % (clock, slot, slot)
        else:
            fact = "玩家没有说过自己吃过%s。" % slot
    elif claim.get("kind") == "work":
        fact = "玩家没有说过自己刚下班 / 忙完 / 到家。"
    elif claim.get("kind") == "sleep":
        fact = "玩家没有说过自己的睡眠情况。"
    else:
        fact = "玩家没有说过自己身体不舒服。"
    return ("（事实核对：%s 请不要替玩家编造私人经历；保留你原来的语气，"
            "改成按时间给建议，或自然地问一句。不要提系统、记忆或“核对”。）" % fact)


def fallback_reply(claim, evidence) -> str:
    """重答仍编造 / 超时时的确定性兜底句：不编事实，仍回答玩家的问题。"""
    ev = evidence if isinstance(evidence, dict) else {}
    hour = int(ev.get("hour", _hour()))
    kind = str((claim or {}).get("kind", ""))
    if kind == "meal":
        slot = str((claim or {}).get("slot", "meal"))
        if (claim or {}).get("negative_cue"):
            if _MEAL_CANCEL_RE.search(_recent_text(ev)):
                return "你刚说外卖取消了，别空着肚子；晚饭还是吃点简单的，或者先垫一口。"
            return "你刚说还没吃或者不太想吃；别空着肚子太久，晚点想吃再吃一点。"
        if slot == "dinner":
            if hour < 17:
                return "现在离晚饭还早呢。六点半到七点吃比较合适；要是饿了，先垫一点水果或热牛奶。"
            if hour < 21:
                return "这个点差不多该吃晚饭了，别拖太晚；你吃过了吗？"
            return "这么晚了别吃太重，垫一点就好。"
        if slot == "lunch":
            if hour < 11:
                return "现在离午饭还早，十二点左右吃比较合适。"
            if hour < 15:
                return "这个点差不多该吃午饭了；你吃过了吗？"
            return "午饭错过了就晚点补一点，别空腹太久。"
        if slot == "breakfast":
            if hour < 6:
                return "现在还没到早饭点，先睡好；醒了再吃点热乎的。"
            if hour < 11:
                return "早饭吃点热乎的，别空着肚子。"
            return "早饭错过了就午饭补上，别空腹太久。"
        if slot == "late":
            return "这么晚别吃太重，垫一点就好。"
        return "这个我还真不知道你吃过没有；你想吃点什么？"
    if kind == "work":
        return "这个我还真不知道，你今天忙完了吗？"
    if kind == "sleep":
        return "这个我不清楚，你昨晚睡得怎么样？"
    if kind == "health":
        return "这个我不敢乱猜，你哪里不舒服吗？"
    return "这个我还真不知道，你说说看？"
