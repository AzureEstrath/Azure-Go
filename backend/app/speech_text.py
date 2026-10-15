"""语音文本清洗与分句：按前端 ``game/scripts/services/TTSClient.gd`` 的既有行为移植。

计划 05 把「她说什么、怎么切句、什么语气」收归后端，前端只负责取回音频并播放；
本模块是这条链路最底层的纯函数：不触网、只用标准库，emoji / 颜文字判定走 Unicode
区间 + 已知符号表，不引入第三方依赖。

行为基准（行号可逐条对照）：
- ``clean_for_speech``    ↔ ``TTSClient.gd:323-336``（``_DROP_RE`` 见 ``:24``、``MAX_CHARS`` 见 ``:14``）
- ``split_sentences``     ↔ ``TTSClient.gd:339-365``（``_split_sentences``）
- ``chunk_for_synthesis`` ↔ ``TTSClient.gd:377-393``（``_chunk_for_slow``，另按 T1 补超长句硬切）
- ``extract_tone``        ↔ ``Main.gd:796-806``（``_strip_tone``）
"""

import re

# 清洗后朗读文本的字数上限；超出截断并补省略号（前端 TTSClient.gd:14）。
MAX_CHARS = 300

# 语气标记：``[[tone:xxx]]``；方括号可少一个、冒号可全角、标记内前后可有空白（前端 Main.gd:798）。
TONE_RE = re.compile(r"\[\[?\s*tone\s*[:：]\s*([a-zA-Z]+)\s*\]\]?")

# 需剔除的符号：emoji、箭头/杂项符号、变体选择符、半角片假名、中点、不可见空白/控制符、希腊字母。
# Unicode 区间逐段沿用前端 ``TTSClient.gd:24`` 的 ``_DROP_RE``。
SYMBOL_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji / 象形符号
    "\u2190-\u2BFF"           # 箭头、数学与杂项符号
    "\uFE00-\uFE0F"           # 变体选择符（emoji 修饰）
    "\u2600-\u27BF"           # 杂项符号与装饰
    "\uFF61-\uFF9F"           # 半角片假名（颜文字常用）
    "\u30FB\uFF65"            # 片假名中点 / 半角中点
    "\u2000-\u200F"           # 各类空格与方向控制符
    "\u0370-\u03FF"           # 希腊字母（ω 等颜文字）
    "]"
)

# 半角 ``[方括号]`` 标记（计划 Interfaces 有意加严；前端只把全角【】转成逗号）。
# 先匹配双中括号再匹配单中括号，顺带兜住可能泄漏的 ``[[mood:...]]`` 之类标记。
_BRACKET_RE = re.compile(r"\[\[[^\[\]]*\]\]|\[[^\[\]]*\]")

# 超长句软切用的断点集合：标点 / 空格 / 换行（句末终止符与 Interfaces 清单一致，另含前端
# TTSClient.gd:348 里的 ``…``，并补充逗号以便把长句切得更自然）。
_SOFT_BREAKS = "，,、。！？；…!?;. \n"

# 在句末终止符处切分；连续终止符（如「。。」）不切开，避免产生纯标点碎片。
_SPLIT_RE = re.compile(r"(?<=[。！？；…!?;.\n])(?![。！？；…!?;.\n])")


def clean_for_speech(text: str) -> str:
    """文本 → 适合朗读的句子：去装饰/表情/方括号标记，合并空白，限长。"""
    if not text:
        return ""
    s = text.replace("**", "").replace("📊", "").replace("📋", "")
    s = SYMBOL_RE.sub("", s)
    s = _BRACKET_RE.sub("", s)                        # 半角方括号标记（加严）
    s = s.replace("【", "，").replace("】", "，")      # 全角括号标题 → 逗号
    s = s.replace("()", "").replace("（）", "")       # 颜文字被清空后剩下的空括号
    s = " ".join(part for part in s.split("\n") if part)
    while "  " in s:
        s = s.replace("  ", " ")
    s = s.strip()
    if len(s) > MAX_CHARS:
        s = s[:MAX_CHARS] + "…"
    return s


def extract_tone(text: str) -> tuple[str, str]:
    """剥离 ``[[tone:xxx]]`` → ``(tone, rest)``。

    只取**第一个**标记作为 tone；不在 ``sigh/cheer/proud`` 之内一律返回空串；但**所有**
    标记都要从正文里剥掉（未知标记也照删，防止被系统语音念出来）。
    """
    if not text:
        return "", text if text is not None else ""
    m = TONE_RE.search(text)
    tone = m.group(1).lower() if m else ""
    rest = TONE_RE.sub("", text).strip()
    if tone not in ("sigh", "cheer", "proud"):
        tone = ""
    return tone, rest


def split_sentences(text: str) -> list[str]:
    """按句末终止符切分并过滤空句；连续终止符不切碎（如「。。」留在同一句）。"""
    out: list[str] = []
    for part in _SPLIT_RE.split(text or ""):
        s = part.strip()
        if s:
            out.append(s)
    return out


def chunk_for_synthesis(text: str, max_chars: int = 100) -> list[str]:
    """把文本合并成适合单次合成的块：相邻短句拼到 ``max_chars`` 为止。

    超过 ``max_chars`` 的长句先按标点软切、再按长度硬切，保证**任何输入下**
    ``len(chunk) <= max_chars``（T1）。
    """
    limit = int(max_chars)
    if limit < 1:
        limit = 1
    sents = split_sentences(text)
    if not sents and text:
        sents = [text]                    # 全是空白/纯空句：按原文兜底，但仍守住长度上限
    chunks: list[str] = []
    cur = ""
    for sent in sents:
        pieces = _hard_split(sent, limit) if len(sent) > limit else [sent]
        for p in pieces:
            if cur == "":
                cur = p
            elif len(cur) + len(p) <= limit:
                cur += p
            else:
                chunks.append(cur)
                cur = p
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks.append("")
    return chunks


def _last_break(buf: str):
    """返回 ``buf`` 中最后一个标点之后的位置（标点归左段）；找不到返回 ``None``。"""
    for i in range(len(buf) - 1, -1, -1):
        if buf[i] in _SOFT_BREAKS:
            return i + 1
    return None


def _hard_split(s: str, limit: int) -> list[str]:
    """把超过 ``limit`` 的长句切成每段 <= ``limit``：优先在标点处断，断不了就硬断。"""
    out: list[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if len(buf) >= limit:
            cut = _last_break(buf)
            if cut is None:
                cut = len(buf)
            out.append(buf[:cut])
            buf = buf[cut:]
    if buf:
        out.append(buf)
    return out
