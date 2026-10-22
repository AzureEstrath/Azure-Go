"""台词里的 ``{行为}`` 标签（v3.11.5）：只做两件事——给 TTS 剥掉、给前端留干净的可用标签。

- **TTS 永远不念标签**：``strip_actions`` 把所有 ``{...}`` 段删掉（未知标签也删），语音文本因此干净；
- **前端只收到白名单标签**：``clean_actions`` 保留 ``ALLOWED`` 里的（最多 ``limit`` 个），未知标签删掉，
  免得模型自造一个前端不认识的动作、在气泡里留下花括号；
- 标签本身随 ``message`` 走，前端在**文字/语音真正出现的那一刻**解析并触发化身表情。

白名单刻意做得很小：程序化骨架只有表情、脸红、视线这些能演的动作，开太多名字只会让模型
写出前端做不出来的“动作”。
"""

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
