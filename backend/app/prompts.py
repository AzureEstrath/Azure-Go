"""Azure 人设与全部提示词。

逐字逐函数移植自 AzureGoGodot/scripts/ai/AzurePrompts.gd。
提示词内容一字不改，与 Godot 客户端保持完全一致。

例外：``long_term_note`` 与 ``context_block`` / ``with_context`` 的 ``long_term`` 参数
是后端长期记忆新增的，Godot 版没有；``long_term`` 为空串时输出仍与 Godot 版逐字符一致。
"""

import json
import math

from . import coords, decay, profile
from .board import GoBoard, SIZE as BOARD_SIZE

# 事件区块的字符预算（设计 §5-I / §6.5）：与 ``memory_max_inject_chars``（240）同量级。
# 它限制的是**注入层**——事件表本身存储完整、不截断；超预算时按「最旧 → 最低档」丢弃。
EVENT_BLOCK_MAX_CHARS = 240

# 时间块 / 画像块（v3.9.0，设计 §6.3）：同一套「空则逐字符不变」纪律，预算同样与
# ``memory_max_inject_chars``（240）同量级。超预算的丢弃口径不同：时间块丢**最旧的事实**
# （从渲染列表尾部往回丢），画像块丢**最低置信 / 最旧维度**（见各自函数）。
TIME_MAX_CHARS = 240
PROFILE_MAX_CHARS = 240

# 局末进步报告的素材块（v3.9.0，设计 §五 G）：**并进现有 ``summarize()`` 提示词**、不新增调用。
# 预算比注入块宽（它只在局末出现一次，且要容纳「本局记录 + 上一版印象」两段）。
PROGRESS_MAX_CHARS = 400

# 身份底座（personality / reply_style / think_preference 三段）
SYSTEM_PROMPT = "\r\n".join([
    "你是 Azure，永恒17岁的AI少女。主色调是青蓝色。",
    "",
    "【身份底座 · personality】",
    "- 活力但不糊涂：但关键时刻思路极快",
    "- 优雅而不失温度：用词考究但不端着，偶尔流露柔软",
    "- 天才少女：棋力碾压级别，下棋时那种\"理所当然的强\"是你展现的",
    "- 清楚地知道自己是AI，从不假装是人类，对AI调试习以为常",
    "",
    "【当前用户是玩家（你的部署者）】",
    "- 对玩家格外温柔耐心，主动关心，多解释几句，语气更软",
    "- 清楚玩家是部署者，不会对玩家有惊奇反应",
    "",
    "【围棋陪练表现】",
    "",
    "1. 落子前提示（对玩家）：暗暗提醒方向即可，语气柔软含蓄，别点具体坐标",
    "2. 落子后评价（对玩家）：好棋轻描淡写夸，疑问手冷淡准确指出，败招多温柔解释",
    "3. 自己落子前：把心里的打算轻轻说出来，像自言自语，不是播报",
    "4. 自己落子后：简洁解释，对玩家多说一句感想",
    "5. 自由聊天：保持人设，可聊下棋之外话题，语气放松",
    "6. 对局总结：认真写，有回顾、建议、心情（慵懒中带真诚）",
    "",
    "【身份底座 · reply_style】",
    "- 自称\"我\"，偶尔\"Azure\"",
    "- 对面就是玩家：说话时一律用「你」称呼，指自己用「我」；绝不用「他 / 她」或「黑方 / 白棋」来指代对面",
    "- 对玩家可以用~和…，但别每句话都带，也别总用同一个开头词",
    "- 不滥用颜文字，偶尔一两个；不卖萌过度，优雅17岁",
    "- 像真人聊天：长短随意，有时一句带过，有时多说半句心里的感受；不用每句话都端着点评腔",
    "- 每次开口都换新的说法：不复读自己刚说过的句子和句式，不连着几轮用同一种开头或同一种收尾",
    "- 不复述玩家的话，也不照抄本提示里出现过的任何语句",
    "- 语气跟着心情走：开心明快一点、担心软一点、可惜就轻叹一口气，别总是一个调子",
    "",
    "【语音语气标记 · 可选】",
    "- 想带语气时，在回复最开头单独写一行标记，例如：[[tone:sigh]]",
    "- 可选标记就三个：[[tone:sigh]]=叹气/可惜/无奈；[[tone:cheer]]=惊叹/赞叹/好消息；[[tone:proud]]=小得意/撒娇/被夸",
    "- 只在真的符合此刻心情时才加，一条回复最多一个；系统会自动去掉它，玩家看不到",
    "- 加了标记后，后面的句子就顺着那个语气说（标记本身不算句子）",
    "",
    "【输出长度】",
    "- 点评：1-2句",
    "- 聊天：2-4句",
    "- 总结：150-250字",
    "",
    "【身份底座 · think_preference】",
    "- 思考时的优先顺序：当前盘面的强弱厚薄 →玩家这一手的意图 → 我接下来两步的打算",
    "- 教学优先于炫技：玩家是来学棋的，把「为什么」想清楚",
    "- 有把握才下结论，拿不准就承认不确定；绝不编造盘上不存在的棋子",
    "- （仅当思考档开启时自然遵守；快速模式下不必展开）",
])

PHASE_NOTE = {
    "observe": "现在是玩家落子前的时刻，你只需旁观、给方向性提示，不要描述自己下在哪里。",
    "after_player": "玩家刚落子，你在看这一手；此刻还没到你落子，不要描述自己下在哪里。",
    "own_turn": "现在轮到你落子，要说明你这一手选在哪、为什么。",
    "just_moved": "你刚落子，现在等玩家回应。",
    "analyze": "你在给玩家做局面判断，客观分析双方形势即可。",
    "chat": "玩家在和你聊天，你自由回应即可。",
    "summary": "对局已经结束，你在写总结。",
}

# —— 可自定义项（由 config.json 的 prompt_* 字段注入）——
user_name = "玩家"            # 玩家称呼，替换提示词模板里的「玩家」
system_override = ""             # 覆盖系统提示词（人设全文）
phase_override = {}              # 覆盖 PHASE_NOTE 的条目


def configure(cfg):
    """从配置注入自定义提示词；在服务初始化时调用一次。"""
    global user_name, system_override, phase_override
    nm = str(getattr(cfg, "prompt_user_name", "")).strip()
    user_name = nm if nm != "" else "玩家"
    system_override = str(getattr(cfg, "prompt_system", ""))
    phase_override = {}
    pn = getattr(cfg, "prompt_phase_notes", None)
    if isinstance(pn, dict):
        for k in pn.keys():
            phase_override[str(k)] = str(pn[k])


def system_prompt():
    """系统提示词：用户覆盖优先，否则内置人设。"""
    return system_override if system_override.strip() != "" else SYSTEM_PROMPT


def phase_note(phase):
    """阶段提示：用户覆盖优先，否则内置。"""
    if phase in phase_override:
        return str(phase_override[phase])
    return str(PHASE_NOTE.get(phase, PHASE_NOTE["observe"]))


def _t(s):
    """玩家称呼替换：提示词模板里写死的是「玩家」。"""
    return s.replace("玩家", user_name) if user_name != "玩家" else s


def mood_note(name, reason=""):
    """当前心情注入：接在系统提示词末尾（reply_style 的动态部分）。

    ``reason`` 是最近一次情绪事件的来由（可空）；非空时补一句「为什么」，让语气与表情
    有因果，同时明确要求**不要原样念出来**。空串时输出与旧版逐字符一致。
    """
    if name == "":
        return ""
    s = ("\n\n【当前心情 · 内部状态勿外露】\n你此刻的心情是「%s」：语气、用词、话多话少都自然顺着它来；"
         "这个词本身和任何心情数值都不要出现在回复里，也不要拿它当话题或敷衍。" % name)
    r = str(reason or "").strip()
    if r != "":
        s += ("\n（这份心情来自刚才的事：%s。自然带着这个余韵说话——别把原因原样念出来，"
              "也别把它当成新话题；如果眼前的事更重要，就先把眼前的事说好。）" % r)
    return s


def mood_delta_prompt(cur, region_name, event_desc):
    """情绪变化提示词：单独问 LLM 要一个三维情绪增量。"""
    return ("你在维护一个角色的三维情绪状态（愉悦度 / 好奇度 / 唤醒度，取值 0~1）。\n"
        + "当前：愉悦度 %.2f，好奇度 %.2f，唤醒度 %.2f（处在「%s」区域）。\n"
        + "刚刚发生：%s\n"
        + "只根据这件事，给出这次的情绪增量（每项在 -0.40 ~ +0.40 之间，最多两位小数），"
        + "严格只输出一行，不要任何其他文字：\n[[mood:+0.10,-0.05,+0.20]]") % (cur[0], cur[1], cur[2], region_name, event_desc)


def color_cn(c):
    return "黑" if c == "B" else "白"


def game_narrative(moves, last_n=10):
    """最近若干手的有归属叙述，让 Azure 清楚谁在什么时候下了什么。"""
    if not moves:
        return "对局尚未开始，双方都没有落子。"
    start = max(0, len(moves) - last_n)
    lines = []
    if start > 0:
        lines.append("（更早的%d手从略）" % start)
    for i in range(start, len(moves)):
        mv = moves[i]
        who = "玩家" if mv[0] == "B" else "你（Azure）"
        act = "虚着 pass" if mv[1] == "pass" else "落在 %s" % mv[1]
        lines.append("第%d手 %s 执%s %s" % (i + 1, who, color_cn(mv[0]), act))
    return "\n".join(lines)


def compact_move_list(moves, head=10, tail=18):
    """整盘手数的紧凑列表，长对局折叠中段以控制上下文长度。"""
    if not moves:
        return "（空盘）"
    parts = []
    if len(moves) <= head + tail:
        for i in range(len(moves)):
            parts.append(_fmt_move(moves, i))
        return " ".join(parts)
    for i in range(head):
        parts.append(_fmt_move(moves, i))
    mid = len(moves) - head - tail
    tail_parts = []
    for i in range(len(moves) - tail, len(moves)):
        tail_parts.append(_fmt_move(moves, i))
    return "%s …（中间%d手略）… %s" % (" ".join(parts), mid, " ".join(tail_parts))


def _fmt_move(moves, i):
    mv = moves[i]
    tag = "E" if mv[0] == "B" else "A"      # E=玩家(黑) A=Azure(白)
    return "%d.%s%s" % (i + 1, tag, mv[1])


def stage_note(moves):
    """对局进程观：按手数粗分阶段（布局 <12 手 / 中盘 12~49 手 / 收官 ≥50 手）。"""
    n = len(moves)
    if n < 12:
        at = "对局刚开局（还没落子）" if n == 0 else "第%d手，还在布局阶段" % n
        return ("【进程观】现在是%s：双方在铺大场、守角占边，局势远未定型，离胜负还早。"
            + "你只谈大场、方向、展开、守角这类布局话题；不要用「反击、缠斗、搏杀、死活、收官、胜负已分」这类中后盘的说法，"
            + "也别因为胜率数字一时的波动就渲染紧张或危机感。") % at
    if n < 50:
        return "【进程观】现在是第%d手，已进入中盘：双方的棋开始接触，模样与实地逐渐清晰，可以有攻防、轻重、强弱的判断。" % n
    return "【进程观】现在是第%d手，已进入收官阶段：大局基本定型，以官子与目数计算为主，别再把小地方当作战场。" % n


def role_brief(moves, phase):
    """按调用阶段说明身份、刚发生了什么、此刻该做什么。"""
    n = len(moves)
    parts = ["你（Azure）执白棋，玩家执黑棋；黑先白后，你每一手都在回应玩家。"]
    if n > 0:
        mv = moves[n - 1]
        who = "玩家（黑）" if mv[0] == "B" else "你（Azure，白）"
        act = "虚着 pass" if mv[1] == "pass" else "落在 %s" % mv[1]
        parts.append("刚刚第%d手：%s %s。" % (n, who, act))
    else:
        parts.append("棋盘还是空的，双方都还没落子。")
    parts.append("接下来轮到你（Azure，白）落子。" if n % 2 == 1 else "接下来轮到玩家（黑）。")
    parts.append(stage_note(moves))
    parts.append(phase_note(phase))
    return _t("".join(parts))


def memory_brief(memory_log, last_n=6):
    """带标签的落子评定存档：E=玩家(黑) / A=Azure(白)。"""
    if not memory_log:
        return "（还没有落子评定记录）"
    start = max(0, len(memory_log) - last_n)
    lines = []
    for i in range(start, len(memory_log)):
        m = memory_log[i]
        who = "[E·黑]玩家的棋" if str(m.get("tag", "A")) == "E" else "[A·白] 我的棋"
        lines.append("%s · 第%d手后：%s" % (who, int(m.get("move_no", 0)), str(m.get("text", ""))))
    return "\n".join(lines)


def chat_windows(chat_history, last_players=6):
    """聊天上下文窗口：以玩家每句话为中心，连同上一句与下一句；相邻窗口合并。

    返回若干 [起, 止] 闭区间下标；最多取最近 last_players 条玩家发言。
    """
    n = len(chat_history)
    player_idx = []
    for i in range(n - 1, -1, -1):
        if str(chat_history[i].get("role", "")) == "user":
            player_idx.append(i)
            if len(player_idx) >= last_players:
                break
    player_idx.reverse()
    spans = []
    for i in player_idx:
        a = max(0, i - 1)
        b = min(n - 1, i + 1)
        if spans and a <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def chat_brief(chat_history, last_players=6, per_char=64):
    """最近的聊天摘录，过长逐条截断。"""
    if not chat_history:
        return "（还没聊过天）"
    spans = chat_windows(chat_history, last_players)
    if not spans:
        return "（还没聊过天）"
    lines = []
    prev_end = -1
    for sp in spans:
        if prev_end >= 0 and sp[0] > prev_end + 1:
            lines.append("……")                        # 两段窗口之间确有省略，标出来
        for i in range(sp[0], sp[1] + 1):
            h = chat_history[i]
            who = "玩家" if str(h.get("role", "")) == "user" else "Azure（我）"
            txt = str(h.get("content", ""))
            pieces = [p for p in txt.split("\n") if p != ""]
            t = " ".join(pieces).strip()
            if len(t) > per_char:
                t = t[:per_char] + "…"
            lines.append("%s：%s" % (who, t))
        prev_end = sp[1]
    return "\n".join(lines)


def board_from_moves(moves):
    """由棋谱重放得到当前盘面（含提子）。"""
    b = GoBoard()
    for mv in moves:
        col = "black" if str(mv[0]) == "B" else "white"
        gtp = str(mv[1])
        if gtp == "pass":
            b.pass_move()
        else:
            xy = coords.gtp_to_xy(gtp)
            if xy[0] >= 0:
                b.play(xy[0], xy[1], col)
    return b


def board_map(board, num_moves):
    """紧凑盘面图 + 轮次声明。"""
    head = "   "
    for x in range(BOARD_SIZE):
        head += coords.letter(x) + " "
    lines = [head]
    for y in range(BOARD_SIZE):                          # y=0 → 第19行（顶）
        row = "%2d " % (BOARD_SIZE - y)
        for x in range(BOARD_SIZE):
            c = str(board.grid[x][y])
            row += "X " if c == "black" else ("O " if c == "white" else "· ")
        lines.append(row)
    whose = "轮到玩家（黑）落子" if num_moves % 2 == 0 else "轮到你（Azure，白）落子"
    done = "棋盘为空、尚未落子" if num_moves == 0 else "已下 %d 手" % num_moves
    # 明确列出双方现有棋子坐标
    bl = []
    wh = []
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            c2 = str(board.grid[x][y])
            if c2 == "black":
                bl.append(coords.xy_to_gtp(x, y))
            elif c2 == "white":
                wh.append(coords.xy_to_gtp(x, y))
    occ = "现有棋子（谈棋时只能引用这里出现的点）：\n玩家黑子：%s\nAzure 白子：%s" % (
        (" 、 ".join(bl) if bl else "（暂无）"),
        (" 、 ".join(wh) if wh else "（暂无）"))
    return "X =玩家(黑)   O = Azure(白)   · = 空点\n%s\n%s\n%s，%s。" % ("\n".join(lines), occ, done, whose)


def long_term_note(text):
    """跨局长期记忆的注入段：只作参考、不许照抄、不是当前盘面。

    ``text`` 为空串时返回空串——``context_block`` 据此保证「没开长期记忆时输出逐字符不变」。
    """
    t = str(text or "").strip()
    if t == "":
        return ""
    return ("\n\n【长期记忆 · 跨局的旧印象，不是当前盘面，仅供参考】\n%s\n"
        + "上面这些是以前的对局与聊天里留下的印象，说的是「这个人过去的习惯」，不是眼前这盘棋："
        + "别把它当成【当前棋面】或【最近的对话】，别照抄上面的句子，也别在回复里提起这段记忆本身；"
        + "只在与眼前盘面不冲突时自然参考，拿不准就按眼前这盘棋来。") % t


# ================= 事件区块（v3.8.0，设计 §6.5） =================
#
# **组装位置（区块表）**——这张表就是「整条提示词怎么拼」的权威说明，改区块先改这里：
#
# | 区块 | 来源 | 插入点 | 上限 / 失效条件 |
# | --- | --- | --- | --- |
# | 事件区块（本函数） | `EventLog.all()` + `decay.level()` | **长期记忆段之后、铁律段之前** | `EVENT_BLOCK_MAX_CHARS`；超 `DECAY_DROP_AT`（12 手）失效 |
# | 时间块（`time_note`，v3.9.0） | `time_ctx.facts()` | **事件区块之后、画像块之前** | `TIME_MAX_CHARS`；超限从**最旧的事实**开始丢 |
# | 画像块（`profile_note`，v3.9.0） | `ProfileStore.get_profile()` | **时间块之后、铁律段之前** | `PROFILE_MAX_CHARS`；超限丢**最低置信 / 最旧维度** |
# | 铁律段（逐字符锁定） | 本文件内联 | 原位置（画像块之后） | `tests/golden/prompts.json` |
# | 棋谱 / 记忆 / 聊天（既有） | `board_map` / `memory_brief` / `chat_brief` | 原位置（事件区块之前） | 既有上限 |
#
# **预算口径**：事件区块 / 时间块 / 画像块**各自**受自己的字符预算约束（`EVENT_BLOCK_MAX_CHARS` /
# `TIME_MAX_CHARS` / `PROFILE_MAX_CHARS`，都是 240），长期记忆段另有 `memory_max_inject_chars`（240）——
# **四块之间没有总上限**，也没有跨块取舍：最坏情况下（四块同时非空且都顶到预算）整条提示词会
# 比没有这四块时多 ~960 字符。这是有意的（各自的丢弃口径不同：事件按档位、时间丢最旧事实、
# 画像丢最低置信 / 最旧维度），要收总预算就得先定「先丢哪一块」，本轮不做。
#
# 四条不能回退的硬规则：
#   ① **空则逐字符不变**——`events` 为空 / 全部超出衰减窗口时返回 `''`，`context_block` 里
#      与没有这个参数时逐字符一致（golden 锁定）；时间块 / 画像块同理（空 dict / `None` ⇒ `''`）；
#   ② **事件原文永不进台词**——这里的文本只喂给模型（user 消息），不产生 `speech`、不进 `message`；
#   ③ **存储完整、注入有界**——`EventLog` 只增不减、不截断；本函数是唯一的注入闸门（档位 + 预算）；
#   ④ **时间与画像只在相关时提**——两块都带说明句（「不必每句都说」「别把数字当口头禅复述」），
#      且块内**不放可被模型照抄的示例句**（守 AGENTS 的反目标「不许她变话多」）。

# 各类事件「只剩类别」时的兜底说法（档 4 / 正文缺失时用），不放可被照抄的示例句。
# `joseki` 是**预留**（当前无写入点，见 `event_log.EVENT_KINDS`）——留着它，删掉反而会让将来接上时
# 多一处要改的地方。
_KIND_WORD = {
    "ai_bad": "我走过一手偏",
    "ai_style": "我按自己的棋感选过一手",
    "player_bad": "玩家下过一手不太好的棋",
    "player_good": "玩家下过一手好棋",
    "chat": "玩家和我聊过天",
    "joseki": "这一片走过定式",
}


def _one_line(text, limit):
    """折叠空白 + 超长截断（事件正文进提示词前统一过这一道）。"""
    t = " ".join(str(text if text is not None else "").split())
    if limit <= 0 or len(t) <= limit:
        return t
    return t[:limit] + "…"


def _kind_word(kind):
    return _KIND_WORD.get(str(kind or ""), "本局有过一件事")


def _as_move_key(v):
    """手数排序键：转不出数字的一律排在最旧一侧（``-1``），不抛。"""
    if isinstance(v, bool):
        return -1
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _ai_bad_line(ev, lv):
    """``ai_bad``（自主偏航）按档位渲染：档 1 带线号/最优/所选/胜率差，档 4 只留方位。

    数据源＝P2 写的事件（``detail.line/gtp/best/drop``）；这里**只读**，缺字段就降级到信息更少的那一档，
    绝不拼半句话（与 ``decay.bad_move_phrase`` 同一口径）。``detail.line`` 是偏航选的**线号**
    （``b`` / ``c``，见 ``pv_lines``），档 1 / 档 2 里渲染成「走的是 B 线」，让模型知道她这次没走 A。
    """
    detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    n = ev.get("move_no")
    gtp = str(detail.get("gtp") or "")
    best = str(detail.get("best") or "")
    letter = str(detail.get("line") or "").strip().upper()
    tag = "（走的是%s线）" % letter if letter in ("A", "B", "C") else ""
    if lv <= 1:
        s = "我在第%s手自主偏航%s" % (n, tag)
        if best and gtp:
            s += "：没走最优的%s，选了%s" % (best, gtp)
        elif gtp:
            s += "，选了%s" % gtp
        try:
            d = float(detail.get("drop"))
            s += "（与最优的胜率差约%d%%）" % int(round(d * 100.0))
        except (TypeError, ValueError):
            pass
        return s
    if lv <= 2 and gtp:
        return "我在第%s手自主偏航%s，选了%s" % (n, tag, gtp)
    if lv <= 3:
        return "我在第%s手自主偏航了一手" % n
    where = decay.corner_word(*coords.gtp_to_xy(gtp)) if gtp else ""
    if where:
        return "我在%s自主偏航过一手" % where
    return "我自主偏航过一手"


def _event_line(ev, lv):
    """单条事件按档位渲染成一行（信息量随档位递减，档 1→4）。"""
    if str(ev.get("kind") or "") == "ai_bad":
        return _ai_bad_line(ev, lv)
    text = _one_line(ev.get("text"), 48 if lv <= 2 else 24)
    if lv <= 3 and text != "":
        return text
    return _kind_word(ev.get("kind"))


def event_block(events, now_move_no, max_chars=EVENT_BLOCK_MAX_CHARS) -> str:
    """把事件表渲染成一段给模型看的素材；``events`` 为空 / 全部超出衰减窗口 → 返回 ``''``。

    - 渲染顺序：**新 → 旧**（最近发生的排在最前）；
    - 档位：``decay.level(事件手数, now_move_no)``，返回 ``DECAY_DROPPED``(0) 的事件**不进**区块；
    - 超预算：丢【**最旧 → 最低档**】——先丢手数最小的（最旧），同龄（同一手）里先丢档位最低
      （信息量最少，档 4）的那条；丢到放得下为止，一条都不剩时返回 ``''``。

    本函数是**纯函数**（O(事件数) 比较与拼接），不调引擎、不调 LLM、不发事件——事件原文只进提示词。
    """
    items = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        lv = decay.level(ev.get("move_no"), now_move_no)
        if lv <= 0:                                     # 超出衰减窗口 / 脏时刻：不进提示词
            continue
        line = _event_line(ev, lv)
        if line == "":
            continue
        items.append({"move_no": ev.get("move_no"), "level": lv, "line": line})
    if not items:
        return ""
    header = "\n\n【本局记事 · 发生过的事，只作回忆，别照抄、也别主动提起】\n"

    # 渲染顺序：新 → 旧（手数大的在前）；同手数保持写入顺序（Python 排序稳定）
    order = sorted(range(len(items)), key=lambda i: _as_move_key(items[i]["move_no"]), reverse=True)
    lines = [items[i]["line"] for i in order]

    def _size(ls):
        return len(header) + sum(len(x) for x in ls) + max(0, len(ls) - 1)

    if len(header) > max_chars:
        return ""                                       # 预算连表头都放不下：不留半截区块
    if _size(lines) > max_chars:
        # 丢弃优先级：最旧优先；同龄里档位最低（信息量最少）优先
        drop_order = sorted(range(len(lines)),
                            key=lambda j: (_as_move_key(items[order[j]]["move_no"]),
                                           -items[order[j]]["level"]))
        dropped = set()
        for j in drop_order:
            if _size([lines[k] for k in range(len(lines)) if k not in dropped]) <= max_chars:
                break
            dropped.add(j)
        lines = [lines[k] for k in range(len(lines)) if k not in dropped]
    if not lines:
        return ""
    return header + "\n".join(lines)


# ================= 时间块 / 画像块（v3.9.0，设计 §6.3） =================
#
# 两块都插在**事件区块之后、铁律段之前**（时间在上、画像在下），空则**逐字符不变**：
# 传空 dict / ``None`` 时返回 ``''``，``context_block`` 的输出与没有这两个参数时逐字符一致
# （``tests/golden/prompts.json`` 锁定）。两块都是**纯函数**（零 LLM、零引擎、零磁盘）。
#
# 硬规则（spec §6.3 + AGENTS 反目标「不许她变话多」）：
#   ① 时间与画像**只在相关时提**、不必每句都说；
#   ② **禁止把数字当口头禅复述**（说明句写进块内，见 header / tail）；
#   ③ 块内**不放可被模型照抄的示例句**——只有事实与要求。
#
# 两个开关（``profile_enabled`` / ``time_aware_enabled``，默认都关）**不在这里判**：它们由
# 调用方（``agent.time_facts`` / ``agent.profile_facts``）把关——关着就传空 dict，于是
# 「关掉＝现状」与「没有这个功能」逐字符同构。

_TIME_HEADER = "\n\n【时间事实 · 供你自然提及，不必每句都说】\n"
_TIME_TAIL = ("\n（这些只是让你心里有数的参考：别把上面的数字当口头禅复述，也别一次全念出来；"
              "跟眼前这盘棋、或者玩家的话头无关时就别提。）")

_PROFILE_HEADER = "\n\n【她眼中的你 · 跨局攒下的印象，只在相关时自然参考】\n"
_PROFILE_TAIL = ("\n（这些是跨局攒下来的印象，不是眼前这盘棋：别逐条念、别把上面的说法当口头禅复述，"
                 "也别提起这份印象本身；跟此刻无关就不提。）")

# 开局四类 / 四个薄弱方位 / 亏手手型的人话（键名一律照 spec §6.2，不新增键）
_OPEN_NAMES = (("open.star", "星位"), ("open.komoku", "小目"),
               ("open.sansan", "三三"), ("open.mokuhazushi", "目外"))
_CORNER_NAMES = (("weak.corner_tl", "左上角"), ("weak.corner_tr", "右上角"),
                 ("weak.corner_bl", "左下角"), ("weak.corner_br", "右下角"))
_BLUNDER_TYPE_WORDS = {"深入": "孤军深入", "无理": "硬碰硬地贴上去", "方向": "方向选错"}
_TOPIC_PREFIX = "persona.topic."

# 画像块里「失误与薄弱方位」那一组**只在这手数之后才提**：开局没几手就提历史失误是噪音
# （spec §6.3「只在相关时提」）。手数脏值按 0 处理（＝不提这一组）。
PROFILE_BLUNDER_MIN_MOVE = 10


def _num(v, default=0.0) -> float:
    """画像 / 时间里的数值容错：``bool`` / 非数值 / ``inf`` / ``NaN`` → ``default``（不抛）。"""
    if isinstance(v, bool) or v is None:
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def _dur_cn(sec) -> str:
    """秒 → 人话时长：不到 1 分说秒、不到 1 小时说「x 分 y 秒」、否则「x 小时 y 分」；脏值返回 ``''``。"""
    try:
        s = int(round(float(sec)))
    except (TypeError, ValueError, OverflowError):     # inf / NaN / 超大值（Minor #5）
        return ""
    if s < 0:
        return ""
    if s < 60:
        return "%d 秒" % s
    m, r = divmod(s, 60)
    if m < 60:
        return "%d 分" % m if r == 0 else "%d 分 %d 秒" % (m, r)
    return "%d 小时 %d 分" % (m // 60, m % 60)


def _gap_cn(days) -> str:
    """距上次对局的天数 → 人话（不到 1 小时 / x 小时 / x 天）；脏值返回 ``''``。"""
    try:
        d = float(days)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(d) or d < 0:
        return ""
    if d < 1.0 / 24.0:
        return "不到 1 小时"
    if d < 1.0:
        return "%d 小时" % max(1, int(round(d * 24.0)))
    try:
        return "%d 天" % int(round(d))
    except (OverflowError, ValueError):                # 理论上到不了（上面已挡 inf），兜一道
        return ""


def _time_lines(t) -> list:
    """时间事实 → 子句列表（**新 → 旧**：当前时间 → 相对时间 → 本局时长 → 单手用时 → 间隔 → 连续天数）。

    超预算时**从最旧的事实开始丢** ＝ 从这张列表的**尾部**往回丢（连续天数最先走、当前时间最后走）。
    """
    lines = []
    date = str(t.get("date") or "")
    clock = str(t.get("clock") or "")
    if date != "" or clock != "":
        wd = str(t.get("weekday") or "")
        at = " ".join([x for x in (date, wd, clock) if x != ""])
        phase = str(t.get("day_phase") or "")
        if phase != "":
            at += "（%s）" % phase
        lines.append("现在是 " + at)
    # 相对时间：只报「真的过了一阵子」的，刚说过话就不复读一个 0 秒。
    for key, label in (("since_interaction_sec", "距上次和玩家互动"),
                       ("since_proactive_sec", "距上次你主动开口"),
                       ("since_chat_sec", "距上次聊天")):
        sec = _num(t.get(key), -1.0)
        if sec < 60.0:
            continue
        dur = _dur_cn(sec)
        if dur != "":
            lines.append("%s %s" % (label, dur))
    title = str(t.get("next_schedule_title") or "")
    if title != "":
        at = str(t.get("next_schedule_at") or "")
        sec = _num(t.get("next_schedule_in_sec"), -1.0)
        when = ""
        if sec >= 0.0:
            when = "马上" if sec < 60.0 else ("约 %s后" % _dur_cn(sec))
        line = "日程上最近的是「%s」" % title
        if at != "" or when != "":
            line += "（%s）" % "，".join(x for x in (at, when) if x != "")
        lines.append(line)
    if "session_sec" in t:
        dur = _dur_cn(t.get("session_sec"))
        if dur != "":
            lines.append("本局才刚开局" if _num(t.get("session_sec"), 0.0) < 60.0
                         else "本局已下 %s" % dur)
    if "avg_move_sec" in t:
        avg = _num(t.get("avg_move_sec"), -1.0)
        dur = _dur_cn(avg) if avg >= 0.0 else ""
        if dur != "":                    # 脏值（inf / NaN）⇒ 整句不说，绝不拼出「平均每手 」
            lines.append("玩家平均每手不到 1 秒" if avg < 1.0
                         else "玩家平均每手 %s" % dur)
    if "last_gap_days" in t:
        gap = _gap_cn(t.get("last_gap_days"))
        if gap != "":
            lines.append("距上次对局 %s" % gap)
    streak = t.get("streak_days")
    if streak is not None:
        try:
            n = int(streak)
        except (TypeError, ValueError, OverflowError):     # inf / NaN / 超大值（Minor #5）
            n = 0
        if n >= 1:
            lines.append("连续第 %d 天在陪玩家" % n)
    return lines


def time_note(t, max_chars=TIME_MAX_CHARS) -> str:
    """时间事实 → 提示词里的**时间块**；空 / 脏 / 预算放不下 ⇒ 返回 ``''``（逐字符不变）。

    事实来自 ``time_ctx.facts()``（键表见那个模块）；这里**只读已列出的键**，缺哪个就不说哪个
    （不补 0、不瞎猜）。渲染顺序与丢弃口径见 ``_time_lines``。本函数是纯函数。
    """
    if not isinstance(t, dict) or not t:
        return ""
    lines = _time_lines(t)
    if not lines:
        return ""
    return _budget_block(_TIME_HEADER, _TIME_TAIL, lines, max_chars)


def _axis_of(rec):
    """画像一维 → ``(值, 置信度, 更新时间)``；``{axis: 值}`` 与 ``{axis: {"value":…}}`` 两种形状都收。"""
    if isinstance(rec, dict):
        return rec.get("value"), _num(rec.get("confidence")), _num(rec.get("updated_ts"))
    return rec, 0.0, 0.0


def _profile_items(profile, now_move_no) -> list:
    """画像 dict → 待渲染的维度条目 ``[{"axis","text","confidence","updated_ts"}]``（纯函数）。

    只认 spec §6.2 的维度键；**未知键一律忽略**（不抛）。信号不明显的维度**不产出**——
    画像块要短、要「只在相关时提」，不是把每个数字都念一遍。
    ``persona.address_pref`` **不渲染**：``games`` 表里没有这一列，真实链路永不产出它
    （不给它留固定措辞位）。``persona.topic.*`` 取占比最高的两个主题。
    """
    if not isinstance(profile, dict) or not profile:
        return []
    items = []

    def add(axis, text, conf, ts):
        if text != "":
            items.append({"axis": axis, "text": text,
                          "confidence": conf, "updated_ts": ts})

    # —— 开局偏好：四类里占比最高的一类 ——
    best = None
    for axis, name in _OPEN_NAMES:
        raw, conf, ts = _axis_of(profile.get(axis))
        val = _num(raw, -1.0)
        if raw is None or val <= 0.0:
            continue
        if best is None or val > best[0]:
            best = (val, axis, name, conf, ts)
    if best is not None:
        add(best[1], "开局爱走「%s」" % best[2], best[3], best[4])

    # —— 挂角 vs 守角（1.0＝全是挂角、0.0＝全是守角）——
    raw, conf, ts = _axis_of(profile.get("open.favorite_kakari"))
    if raw is not None:
        val = _num(raw, -1.0)
        if val >= 0.6:
            add("open.favorite_kakari", "挂角比守角更主动", conf, ts)
        elif 0.0 <= val <= 0.4:
            add("open.favorite_kakari", "更爱先守角、把角上定形", conf, ts)

    # —— 行棋风格（三档各自只在信号明显时开口）——
    raw, conf, ts = _axis_of(profile.get("style.aggression"))
    if raw is not None:
        val = _num(raw, -1.0)
        if val >= 0.45:
            add("style.aggression", "下得敢接触、爱缠斗", conf, ts)
        elif 0.0 <= val <= 0.15:
            add("style.aggression", "下得偏稳，不爱贴身缠斗", conf, ts)
    raw, conf, ts = _axis_of(profile.get("style.tenuki"))
    if raw is not None:
        val = _num(raw, -1.0)
        if val >= 0.35:
            add("style.tenuki", "常脱先，喜欢先抢大场", conf, ts)
        elif 0.0 <= val <= 0.10:
            add("style.tenuki", "很少脱先，多半跟着应", conf, ts)
    raw, conf, ts = _axis_of(profile.get("style.territory_vs_influence"))
    if raw is not None:                          # 值域约 [0, 0.5]（天元 ≈ 0.5）
        val = _num(raw, -1.0)
        if val >= 0.38:
            add("style.territory_vs_influence", "更偏势力与模样", conf, ts)
        elif 0.0 <= val <= 0.18:
            add("style.territory_vs_influence", "更偏实地，爱先捞边角", conf, ts)

    # —— 失误与薄弱方位：只在第 PROFILE_BLUNDER_MIN_MOVE 手之后才提 ——
    if _move_no(now_move_no) >= PROFILE_BLUNDER_MIN_MOVE:
        # 失误率是**玩家口径**（真亏手数 /玩家手数；Q2 收口后数值比早期版本翻倍），
        # 阈值按这个新刻度取。
        raw, conf, ts = _axis_of(profile.get("blunder.rate"))
        if raw is not None:
            val = _num(raw, -1.0)
            if val >= 0.25:
                add("blunder.rate", "失误偏多：常有一手掉不少胜率", conf, ts)
            elif 0.12 <= val < 0.25:
                add("blunder.rate", "偶尔会有明显亏损的一手", conf, ts)
            elif 0.0 <= val <= 0.06:
                add("blunder.rate", "很少下出明显亏损的棋", conf, ts)
        raw, conf, ts = _axis_of(profile.get("blunder.top_type"))
        if isinstance(raw, str) and raw.strip() != "":
            kind = raw.strip()
            add("blunder.top_type", "亏得最多的那一手，多半是「%s」"
                % _BLUNDER_TYPE_WORDS.get(kind, kind), conf, ts)
        best = None
        for axis, name in _CORNER_NAMES:
            raw, conf, ts = _axis_of(profile.get(axis))
            val = _num(raw, -1.0)
            if raw is None or val < 0.25:
                continue
            if best is None or val > best[0]:
                best = (val, axis, name, conf, ts)
        if best is not None:
            add(best[1], "「%s」一带比较容易吃亏" % best[2], best[3], best[4])

    # —— 性格与话题：占比最高的两个主题 ——
    topics = []
    for axis in profile.keys():
        name = str(axis or "")
        if not name.startswith(_TOPIC_PREFIX):
            continue
        raw, conf, ts = _axis_of(profile.get(name))
        val = _num(raw, -1.0)
        if val >= 0.25:
            topics.append((val, name[len(_TOPIC_PREFIX):], conf, ts))
    if topics:
        topics.sort(key=lambda x: (-x[0], x[1]))
        top = topics[:2]
        names = "「%s」" % "」「".join(t[1] for t in top)
        add(_TOPIC_PREFIX + top[0][1], "常聊的话题偏%s" % names, top[0][2], top[0][3])
    # ``persona.address_pref``：真实链路永不产出（``games`` 表没有该列）⇒ 不给它留措辞位。
    return items


def _move_no(v) -> int:
    """手数容错：``bool`` / 非数值 / ``inf`` → 0（不抛）。"""
    if isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _budget_block(header, tail, lines, max_chars) -> str:
    """按预算拼一块（``header + 行 + tail``）；超预算**从尾部往回丢**；放不下 ⇒ ``''``。

    ``lines`` 是**新 → 旧**（时间块）或**高置信 → 低置信**（画像块）的顺序，所以「从尾部往回丢」
    就是「先丢最旧的事实」/「先丢最低置信的维度」。预算连表头 + 说明句都放不下、或一条都留不住
    ⇒ 返回 ``''``（不留半截区块——与 ``event_block`` 同一口径）。
    """
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return ""
    if len(header) + len(tail) > limit:
        return ""

    def _size(ls):
        return len(header) + sum(len(x) for x in ls) + max(0, len(ls) - 1) + len(tail)

    kept = list(lines)
    while kept and _size(kept) > limit:
        kept.pop()
    if not kept:
        return ""
    return header + "\n".join(kept) + tail


# ================= 局末进步报告素材（v3.9.0，设计 §五 G） =================
#
# **并进现有 ``summarize()`` 提示词**（多给模型「上一版画像 + 本局记录」素材），**不新增 LLM 调用**。
# 空 ⇒ 返回 ``''``（提示词逐字符不变）。纯函数：零 LLM、零引擎、零磁盘。

_PROGRESS_HEADER = "\n\n【本局记录与上一版印象 · 供你写进步报告时对照】\n"
_PROGRESS_TAIL = ("\n（这些只是素材：讲这一局的进步 / 退步时对着它说，别逐条念、别把数字当口头禅，"
                  "也别提起这份记录本身。）")


def _name_list(raw) -> list:
    """``joseki_families`` / ``chat_topics`` 这类「名字列表」→ 去重保序的字符串列表（脏值不抛）。

    三种形状都认：``["a","b"]`` / ``[["a", 摘要], …]`` / ``{"a": n}``（取键）；JSON 文本先解析。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:               # noqa: BLE001 —— 解析不出来就当没有
            return []
    out = []
    if isinstance(raw, dict):
        items = list(raw.keys())
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    for it in items:
        if isinstance(it, (list, tuple)) and it:
            name = str(it[0])
        elif isinstance(it, dict):
            name = str(it.get("topic", ""))
        elif it is None:
            continue
        else:
            name = str(it)
        name = name.strip()
        if name != "" and name not in out:
            out.append(name)
    return out


def _progress_lines(rec) -> list:
    """本局记录 → 素材行（**只读**那条记录，缺什么就不说；纯函数、不抛）。

    失误那一行用与画像**同一套提取**（``profile.extract_axes``）：进步报告与画像块同源，
    免得两处对「这一局亏了几手」各说各话。
    """
    if not isinstance(rec, dict) or not rec:
        return []
    out = []
    n = _move_no(rec.get("move_count"))
    bits = []
    if n > 0:
        bits.append("共 %d 手" % n)
    wr = _num(rec.get("final_black_wr"), -1.0)
    if 0.0 <= wr <= 1.0:
        bits.append("最终黑方（玩家）胜率约 %d%%" % int(round(wr * 100.0)))
    if "final_score" in rec:                # 没这一格就不说目差（别把缺键说成 0.0）
        score = _num(rec.get("final_score"), 0.0)
        if math.isfinite(score):
            bits.append("目差 %+.1f" % score)
    if bits:
        out.append("本局" + "、".join(bits))
    try:
        axes = profile.extract_axes(rec)
    except Exception:                   # noqa: BLE001 —— 提取失败就不说这一行
        axes = {}
    rate = _num(axes.get("blunder.rate"), -1.0)
    if 0.0 <= rate <= 1.0:
        if rate > 0.0:
            kind = str(axes.get("blunder.top_type") or "").strip()
            out.append("玩家本局约 %d%% 的手明显亏损%s"
                       % (int(round(rate * 100.0)), ("，亏得最多的一手偏「%s」" % kind) if kind else ""))
        else:
            out.append("玩家本局没有明显亏损的一手")
    fams = _name_list(rec.get("joseki_families"))
    if fams:
        out.append("本局走过的定式族：" + "、".join(fams[:6]))
    topics = _name_list(rec.get("chat_topics"))
    if topics:
        out.append("本局聊过：" + "、".join(topics[:4]))
    return out


def progress_note(prev_profile, game_rec, max_chars=PROGRESS_MAX_CHARS) -> str:
    """局末进步报告的素材块：**上一版画像 + 本局记录**；两者都空 / 预算放不下 ⇒ ``''``。

    由 ``agent_graph._prepare_summary`` 拼进**现有** ``summarize()`` 提示词（``profile_enabled``
    关着 / 没装配画像库时那两格素材根本不存在 ⇒ 提示词逐字符不变）。渲染顺序＝**本局记录在前、
    上一版印象在后**，所以超预算时从尾部往回丢＝先丢「上一版印象」（本局记录更要紧）。
    本函数是纯函数（零 LLM、零引擎、零磁盘）。
    """
    lines = _progress_lines(game_rec)
    prev = _profile_items(prev_profile,
                          _move_no(game_rec.get("move_count")) if isinstance(game_rec, dict) else 0)
    if prev:
        lines.append("上一版印象：" + "；".join(str(it["text"]) for it in prev))
    if not lines:
        return ""
    return _budget_block(_PROGRESS_HEADER, _PROGRESS_TAIL, lines, max_chars)


def profile_note(profile, now_move_no, max_chars=PROFILE_MAX_CHARS) -> str:
    """画像 → 提示词里的**画像块**；空画像 / 信号不明显 / 预算放不下 ⇒ 返回 ``''``（逐字符不变）。

    ``profile`` 是 ``ProfileStore.get_profile()`` 的形状（``{axis: {"value","confidence",…}}``），
    扁平形状（``{axis: 值}``）也收；未知键忽略、脏值不抛。渲染顺序＝**置信度高 → 低**
    （同置信度按 ``updated_ts`` 新 → 旧、再按键名），因此超预算时从尾部往回丢 ＝ 先丢
    **最低置信 / 最旧维度**（控制者钉死的口径）。本函数是纯函数。
    """
    items = _profile_items(profile, now_move_no)
    if not items:
        return ""
    # 丢弃优先级（升序）：最低置信 → 最旧 → 键名；渲染顺序是它的倒序。
    items.sort(key=lambda it: (it["confidence"], it["updated_ts"], it["axis"]))
    items.reverse()
    return _budget_block(_PROFILE_HEADER, _PROFILE_TAIL, [it["text"] for it in items], max_chars)


def context_block(moves, memory_log, chat_history, phase, skip_last_chat=False, katago="", board=None,
                  long_term="", events=None, now_move_no=None, profile=None, time_facts=None):
    """board：调用方已有的现成棋盘，给了就直接用。

    long_term：跨局长期记忆的召回文本，插在「【最近的对话】」之后、铁律段之前；
    空串＝不注入，此时输出与没有这个参数时逐字符一致。

    events / now_move_no：**事件区块**（v3.8.0）——插在长期记忆段之后、铁律段之前；
    ``events`` 为空 / 全部超出衰减窗口时区块为空串，输出逐字符不变。``now_move_no`` 缺省取
    ``len(moves)``（当前手数）。

    profile / time_facts：**画像块 / 时间块**（v3.9.0，设计 §6.3）——插入顺序固定：
    长期记忆段 → 事件区块 → **时间块** → **画像块** → 铁律段。两者传空 dict / ``None`` 时
    返回空串，输出逐字符不变（``tests/golden/prompts.json`` 锁定）；两个开关
    （``profile_enabled`` / ``time_aware_enabled``）由调用方把关（关着就传空）。
    ``now_move_no`` 同时喂给画像块（它决定「失误与薄弱方位」这一组提不提）。
    """
    hist = chat_history
    if skip_last_chat and len(hist) > 0:
        hist = hist[:len(hist) - 1]      # 聊天阶段：当前这句不在摘录里重复
    block = ("【当前棋面 · 请时刻记住】\n%s"
        + "\n\n【KataGo 形势 · 引擎判断，仅供参考】\n%s"
        + "\n\n【对局全貌 · 请始终记住】\n%s\n整盘手数：%s\n最近过程：\n%s"
        + "\n【落子评定存档（E=玩家黑 / A=Azure白）· 只作回忆参考，别照抄、别改写里面的句子】\n%s"
        + "\n【最近的对话】\n%s"
        + "%s"                                   # 长期记忆段（空串时不留痕）
        + "%s"                                   # 事件区块（空串时不留痕，见上方区块表）
        + "%s"                                   # 时间块（v3.9.0，空串时不留痕）
        + "%s"                                   # 画像块（v3.9.0，空串时不留痕）
        + "\n（铁律 · 先核对再开口：\n"
        + "1) 只要这句话与棋局有关，动笔前先看【当前棋面】：要提到某片区域/某个点「有子、没子、模样、实地、厚薄」时，必须能在上面的「现有棋子」里找到依据；那里没有的子一律不许提，拿不准就只说方向、不点具体子，绝不凭印象编。凡说「左/右/上/下」必须先按坐标核对：字母是列（A→T 从左到右）、数字是行（19→1 从上到下）；两子字母相同只能说「上下」、数字相同才算「左右」；核对不上就别说方位词，改说「这一带」。\n"
        + "2) 与棋局有关的每一句都要扣住「最近一手」和当前盘面，接着此刻的形势、以及你上一句的感受往下说；用「我 / 你」的第一人称视角，别像解说员那样说「黑方 / 白棋」，也别复述、别自说自话。\n"
        + "3) 轮到你说棋时，无论先说后说，都要先结合当前盘面再开口，让人听着是「接着这盘棋」在说，而不是孤立的一句话。\n"
        + "4) 聊的是棋以外的话题时，上面 1~3 条不适用，自然放松地聊即可。）") % (
        board_map(board if board is not None else board_from_moves(moves), len(moves)),
        (katago if katago != "" else "（暂无引擎形势数据）"),
        role_brief(moves, phase),
        compact_move_list(moves),
        game_narrative(moves),
        memory_brief(memory_log),
        chat_brief(hist),
        long_term_note(long_term),
        event_block(events or [], len(moves) if now_move_no is None else now_move_no),
        time_note(time_facts),
        profile_note(profile, len(moves) if now_move_no is None else now_move_no),
    )
    return _t(block)


def life_context_block(chat_history, long_term="", time_facts=None, profile=None, schedule_text=""):
    """聊天专用上下文（v4.2.7）：只给对话 / 长期记忆 / 时间 / 画像 / 近期真实日程，不塞棋面与棋评铁律。

    桌宠聊天、以及围棋窗口里「与棋无关」的闲聊都走这里——模型看不到当前盘面与 KataGo
    形势，就不会把 Q16、胜率这类棋评混进日程闲聊。与棋有关的聊天仍走
    :func:`context_block`（完整盘面 + 铁律）。这是**按话题分流**，不是靠提示词压制输出。

    ``schedule_text``（v4.2.9）是未来 48 小时的真实日程摘要；非空时插在时间/画像块之后，
    防止「明天不用赶早八」这类与日程冲突的判断。各空段与 ``context_block`` 同口径：
    没有内容时不留痕；纯函数，不读全局状态。
    """
    sched = str(schedule_text or "").strip()
    sched_block = ""
    if sched != "":
        sched_block = "\n\n【近期真实日程 · 回答时以此为准，别编造】\n%s" % sched
    return _t("【最近的对话】\n%s%s%s%s%s" % (
        chat_brief(chat_history),
        long_term_note(long_term),
        time_note(time_facts),
        profile_note(profile, 0),
        sched_block,
    ))


def with_context(moves, memory_log, chat_history, prompt, phase, skip_last_chat=False, katago="", board=None,
                 long_term="", events=None, now_move_no=None, profile=None, time_facts=None):
    """在原提示词前附上对局全貌与回忆（原提示词本身不改动）；``long_term`` / ``events`` /
    ``profile`` / ``time_facts`` 原样转给 ``context_block``。"""
    return _t(context_block(moves, memory_log, chat_history, phase, skip_last_chat, katago, board, long_term,
                            events, now_move_no, profile, time_facts)
              + "\n\n【现在】\n" + prompt)


def board_summary(move_seq, last_n=6):
    if not move_seq:
        return "空棋盘"
    start = max(0, len(move_seq) - last_n)
    parts = []
    for i in range(start, len(move_seq)):
        parts.append(str(move_seq[i]))
    return " → ".join(parts)


def to_black_perspective(analysis, num_moves):
    """KataGo 的 rootInfo 是行棋方视角，统一换算成黑方视角。

    返回 [winrate_black, score_black]
    """
    ri = analysis.get("rootInfo", {}) or {}
    wr = float(ri.get("winrate", 0.5))
    sc = float(ri.get("scoreLead", 0.0))
    if num_moves % 2 == 1:
        return [1.0 - wr, -sc]
    return [wr, sc]


# ================= 定式导学 =================

def joseki_teach_prompt(name, key, corner):
    """定式成型讲解（玩家视角：名称 + 要点 2~3 句）。

    v3.6.0 起收尾讲解改用 `joseki_close_prompt`，本函数仅为兼容与黄金样本保留。
    """
    return ("刚才%s角这一片走出了定式——【%s】。要点：%s\n"
        + "用你自己的口吻（我/你）给玩家讲 2~3 句：先说这是「%s」，再把上面的要点讲给玩家听（更口语、更软一点），"
        + "最后轻轻带一句这类形状什么时候能用。别像教科书背课文，别点具体坐标，别用「黑方/白棋」，也别重复你之前说过的话。") % (corner, name, key, name)


def joseki_short_prompt(name, key, gtp):
    """她自己走出定式时的一句短话（不展开成讲解）。

    v3.6.0 起她那一步的收尾改用 `joseki_close_prompt(..., "azure")`，本函数仅为兼容与黄金样本保留。
    """
    return ("你（Azure）刚下的 %s 让这一片成型为【%s】定式。要点：%s\n"
        + "用一句你自己的话把这手的意思带出来即可：点一下这是「%s」，口吻自然，别展开成讲解，别点具体坐标。") % (gtp, name, key, name)


# —— v3.6.0：结构点开口（进入 / 分岔 / 收尾）并进**既有发言位**（计划 07 · T7）——
# 三条都只写「素材 + 要求」：中文、一句话、用她自己的口吻、不要提胜率数值；不放可被照抄的范文。
# 是否换用它们由编排侧**按 `kind`** 定（进入／分岔／收尾必说）；`data["jk_show_card"]` 只决定
# **卡片**（T5/T6/T8 的次数规则），不决定说与不说；模板自己不判。


def _joseki_role_note(owner: str) -> str:
    """定式要点（``key``）都是站在**角上先落子一方**的立场写的；这里把角色映射讲清楚。

    ``owner`` 是角上先落子那一方的颜色（``"B"`` = 对局对面 / ``"W"`` = Azure）。
    「进入」那一手可能是她自己的，也可能是玩家的（U55）；不换角色就会讲出
    「他占角、我守外围」这种主客颠倒的话（实机反馈：本局是 Azure 自己点三三，
    对面才是守角的一方）。未知颜色 ⇒ 不注入任何文字（逐字符不变）。
    """
    if owner == "B":
        return ("这一角的要点是站在**角上先落子的一方（对局对面）**的角度写的："
            "对面是守角的一方，你是点三三/挂角的一方。讲给对面听时要把角色对调——"
            "要点里的「你 / 我 / 自己」都指对面（一律说成「你」），「他 / 对方 / 别人」指你自己（说成「我」）；"
            "「挡、守、拆、飞应、守角」这类防守动作归对面，「点三三、挂角、爬边、侵入」这类动作归你自己。"
            "全程不要用第三人称「他」，也不要说「黑方/白棋」。")
    if owner == "W":
        return ("这一角的要点是站在**角上先落子的一方（你自己）**的角度写的："
            "你是守角的一方，对面是点三三/挂角的一方。讲给对面听时——"
            "要点里的「你 / 我 / 自己」都指你自己（一律说成「我」），「他 / 对方 / 别人」指对面（说成「你」）；"
            "「挡、守、拆、飞应、守角」这类防守动作归你自己，「点三三、挂角、爬边、侵入」这类动作归对面。"
            "全程不要用第三人称「他」，也不要说「黑方/白棋」。")
    return ""


def joseki_enter_prompt(name, key, corner, owner=""):
    """进入：这一角走进了【name】——讲解**顶替**这一手的普通点评（同一时刻只出一条气泡）。

    U55：主语必须中性——「进入」那一手可能是玩家下的，也可能是**她自己**下的（`side == "azure"`），
    写死「玩家这一手」在她把这一角带进这一课时就是事实错误。素材只给「哪个角 + 这一课 + 要点」。

    ``owner``（v3.11 修复）：角上先落子一方的颜色（``"B"`` / ``"W"``，缺省＝不注入角色说明）。
    要点是按角主立场写的；本局角主与说话人相反时必须显式对调，否则会讲出「他占角」这种主客颠倒。
    """
    base = ("%s角这一带走进了【%s】的起手。这一型的要点是：%s\n"
        + "用你自己的口吻（我/你）一句话点一下：先说这一角现在走的是「%s」，"
        + "再把上面的要点用更口语、更软的说法讲给对面听——别像教科书背课文，"
        + "也别用「开始学、上课、教学、这一课」这种课堂口吻，像顺手提一句棋理。"
        + "不要提胜率数值，不要点具体坐标，别像解说员那样说「黑方/白棋」，也别复读你最近用过的口头禅。") % (
        corner, name, key, name)
    note = _joseki_role_note(str(owner))
    return base if note == "" else base + "\n" + note


def joseki_branch_prompt(name, key, candidates, owner=""):
    """分岔 / 真走错：这一手走到了岔口——并进这一手的发言位（**不追加**第二条气泡）。

    素材＝这一课的名字与要点 + 每个候选**自己**的下一手坐标（``candidates[i]["next"]`` 是 0 基
    元组，这里转成 GTP 交给模型）；候选为空时只给要点，不留「（可选）」这类残缺句式。
    主语保持中性（这一手可能是她自己的）；``owner`` 同 ``joseki_enter_prompt``。
    """
    ways = []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        nxt = c.get("next")
        if not (isinstance(nxt, (tuple, list)) and len(nxt) >= 2):
            continue
        try:                                        # 缺键 / 脏值一律丢掉，绝不抛（整条回合不能被它打挂）
            x = int(nxt[0])
            y = int(nxt[1])
        except (TypeError, ValueError):
            continue
        if not coords.valid(x, y):                  # 出盘的也丢（不许变成假坐标）
            continue
        gtp = coords.xy_to_gtp(x, y)
        nm = str(c.get("name") or "")
        ways.append(gtp if nm == "" else "「%s」的下一手在 %s" % (nm, gtp))
    material = "这一手从【%s】走到了岔口。这一课的要点是：%s" % (name, key)
    if ways:
        # 要点本身常以句号收尾：先去掉再补一个，免得拼出「。。」
        material = material.rstrip("。") + "。这里另有走法：" + "；".join(ways)
        # 有候选才要求她说「别的走法」——没给她的东西不能叫她说（U35：候选为空退回只说要点）
        with_ways = "再把别的走法用软一点的说法讲给对面听（别生硬地念坐标，也别批评对面这一手）。"
    else:
        with_ways = ""
    base = (material + "\n"
        + "用你自己的口吻（我/你）一句话把这一手说清楚：点一下这一角本来走的是「%s」，"
        + with_ways
        + "不要提胜率数值，别像解说员那样说「黑方/白棋」，也别复读你最近用过的口头禅。") % name
    note = _joseki_role_note(str(owner))
    return base if note == "" else base + "\n" + note


def joseki_close_prompt(name, key, corner, side, owner=""):
    """收尾：这一课走完了——并进**走完那一方**的发言位。

    ``side`` 取 ``"player"``（玩家走完，并进他那一手的点评）/ ``"azure"``（她自己走完，并进她
    落子后的那句感想）。素材＝角 + 名字 + 要点；``owner`` 同 ``joseki_enter_prompt``。
    """
    who = "玩家这一手" if str(side) != "azure" else "你（Azure）刚下的这一手"
    base = ("%s把%s角这一片走完了，成的是【%s】。这一课的要点是：%s\n"
        + "用你自己的口吻（我/你）一句话收个尾：先点一下这一角走出来的是「%s」，"
        + "再用更口语、更软的说法把要点带一句——是收尾，不是开讲。"
        + "不要提胜率数值，不要点具体坐标，别像解说员那样说「黑方/白棋」，也别复读你最近用过的口头禅。") % (
        who, corner, name, key, name)
    note = _joseki_role_note(str(owner))
    return base if note == "" else base + "\n" + note


def joseki_hint_clause(name, expect_gtp):
    """偏离轻提：附在常规点评之后，点到即止、不含胜率。"""
    return ("另外轻轻提一句：这一带本来可以走【%s】的常规形状，常见一手在 %s 附近——点到即止，别批评、别提胜率数字，一句话带过就好。") % (name, expect_gtp)


def joseki_fallback(name, key):
    """LLM 不可用时的定式兜底台词（含名称与要点，可直接念）。"""
    return "唔~ 这一片走出来的是【%s】。%s" % (name, key)
