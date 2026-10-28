"""台词里的 ``{行为}`` 标签（v3.11.5）：只做两件事——给 TTS 剥掉、给前端留干净的可用标签。

- **TTS 永远不念标签**：``strip_actions`` 把所有 ``{...}`` 段删掉（未知标签也删），语音文本因此干净；
- **前端只收到白名单标签**：``clean_actions`` 保留 ``ALLOWED`` 里的（最多 ``limit`` 个），未知标签删掉，
  免得模型自造一个前端不认识的动作、在气泡里留下花括号；
- 标签本身随 ``message`` 走，前端在**文字/语音真正出现的那一刻**解析并触发化身表情。

白名单刻意做得很小：程序化骨架只有表情、脸红、视线这些能演的动作，开太多名字只会让模型
写出前端做不出来的“动作”。
"""

import json
import re

# 标签（小写）→ 前端动作键（与 Main.gd 的 `_action_key` 一一对应）
ALLOWED = {
    "笑": "smile", "微笑": "smile", "轻笑": "smile", "smile": "smile",
    "大笑": "laugh", "笑出声": "laugh", "laugh": "laugh",
    "得意": "proud", "骄傲": "proud", "proud": "proud",
    "惊讶": "surprise", "吃惊": "surprise", "意外": "surprise", "surprise": "surprise",
    "担心": "worried", "担忧": "worried", "worried": "worried",
    "难过": "sad", "失落": "sad", "sad": "sad",
    "脸红": "blush", "害羞": "blush", "blush": "blush",
    "思考": "think", "托腮": "think", "think": "think",
}

# 提示词里给模型看的推荐标签（短、口语、都能演）
PROMPT_TAGS = "笑 / 得意 / 惊讶 / 担心 / 难过 / 脸红"

_TAG_RE = re.compile(r"\{\s*([^{}]{1,16})\s*\}")


def strip_actions(text) -> str:
    """删掉所有 ``{...}`` 标签（TTS 用；未知标签也不许念出来）。"""
    return _TAG_RE.sub("", str(text or "")).strip()


def clean_actions(text, limit: int = 2) -> str:
    """保留白名单标签（最多 ``limit`` 个）、删掉未知标签，返回台词原文。"""
    kept = [0]
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = 2

    def _repl(m):
        raw = m.group(1).strip()
        if not raw:
            return ""
        if ALLOWED.get(raw.lower()) and kept[0] < cap:
            kept[0] += 1
            return "{%s}" % raw
        return ""

    out = _TAG_RE.sub(_repl, str(text or ""))
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


# ================= 结构化误输出兜底 =================
#
# 聊天 / 走神偶尔会收到模型直接吐出的 JSON（典型是日程提取结果）。这样的内容：
# - 绝不能原样进气泡 / 聊天面板（用户看到 `[` / 一堆字段名，等同空回复）；
# - 也不能整个丢掉（里面确实有她「看到了什么」的信息）。
# 这里只做一件事：把**像 JSON 的整条回复**转成人话；普通台词原样返回，不碰任何正常路径。

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*", re.I)


def looks_structured(text) -> bool:
    """整条回复是否像 JSON（开头是 ``[`` / ``{``）。正常台词恒为 ``False``。

    唯一例外是句首的**短动作标签**（``{笑}``）：它没有引号/冒号、长度 ≤16，
    仍是正常台词，交给 `clean_actions` / 前端处理。
    """
    t = _FENCE_RE.sub("", str(text or "")).strip()
    if not t:
        return False
    if t[0] == "[":
        # JSON 数组的下一字符只可能是 ``{`` / ``"`` / ``]``；``[笑]`` 这种台词不是 JSON。
        nxt = t[1:].lstrip()[:1]
        return nxt in ("{", '"', "]")
    if t[0] == "{":
        # 短动作标签 ``{笑}`` 是台词；JSON 对象一定以 ``{`` + ``"`` / ``}`` 开头。
        if re.match(r"^\{\s*[^{}\"':\n]{1,16}\s*\}", t):
            return False
        nxt = t[1:].lstrip()[:1]
        return nxt in ('"', "}")
    return False


def _json_payload(text):
    """尽量把整条回复解析成 JSON；失败返回 ``None``（绝不上抛）。"""
    t = _FENCE_RE.sub("", str(text or "")).strip()
    t = re.sub(r"\s*```$", "", t)
    candidates = [t]
    a, b = t.find("["), t.rfind("]")
    if a >= 0 and b > a:
        candidates.append(t[a:b + 1])
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        candidates.append(t[a:b + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (TypeError, ValueError):
            continue
    return None


def _course_line(it):
    """一条日程 dict → 一行可读文本；不是日程就返回空串。"""
    title = str(it.get("title") or it.get("name") or it.get("summary") or "").strip()
    if title == "":
        return ""
    date = str(it.get("date") or "").strip()
    tm = str(it.get("time") or it.get("start_time") or "").strip()
    end = str(it.get("end_time") or "").strip()
    period = str(it.get("period") or "").strip()
    teacher = str(it.get("teacher") or "").strip()
    place = str(it.get("location") or "").strip()
    if place and not place.startswith("@"):
        place = "@" + place
    when = " ".join(x for x in (date, tm if tm else "") if x)
    if end and tm:
        when = "%s-%s" % (when, end) if when else "%s-%s" % (tm, end)
    bits = [when]
    if period:
        bits.append("第%s节" % period)
    bits.append(title)
    extra = " ".join(x for x in (teacher, place) if x)
    line = " ".join(x for x in bits if x)
    return line + ("（%s）" % extra if extra else "")


def naturalize(text) -> str:
    """JSON 形态的回复 → 可读中文；普通台词原样返回。

    日程数组 / ``{"items": [...]}`` 会转成「我把刚才看到的整理成了这些日程：」+ 最多 6 行；
    无法识别的 JSON 返回空串——调用方据此走兜底台词，**绝不把半截 JSON 塞给用户**。
    """
    raw = str(text or "")
    if not looks_structured(raw):
        return raw
    data = _json_payload(raw)
    if isinstance(data, dict):
        for key in ("items", "schedule", "events", "data", "list", "courses"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if isinstance(data, list):
        lines = []
        for it in data:
            if not isinstance(it, dict):
                continue
            line = _course_line(it)
            if line:
                lines.append("· " + line)
            if len(lines) >= 6:
                break
        if lines:
            head = ("我把刚才看到的整理成了这些日程：" if len(lines) > 1
                    else "我把刚才看到的整理成了这条日程：")
            more = "（还有更多，已经放进日程表）" if len(data) > len(lines) else ""
            return head + "\n" + "\n".join(lines) + more
    return ""
