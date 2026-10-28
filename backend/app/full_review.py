"""全盘 AI 复盘（v3.11.5）：把棋谱库里的整盘棋**分段**交给 KataGo + Azure 复盘。

设计口径（与项目既有创新点结合）：
- **分段拉取**：``plan_segments`` 把 N 手切成最多 8 段（长棋谱按 48 手封顶），前端在前一段的
  文字/语音都播完之后才请求下一段——这就是「分几段，防止 TTS 忙不过来」的实现；
- **强思考**：每段固定用 ``think_mode=2``（``LLMClient.chat`` 的按调用覆盖）写 2~4 句点评；
- **``{行为}`` 标签**：提示词只给白名单（``actions.PROMPT_TAGS``），输出经 ``actions.clean_actions``
  清理；标签随 ``message`` 到前端，在文字/语音真正出现时触发表情，TTS 侧由 ``agent_graph`` 剥掉；
- **零新引擎**：只用现有 ``KataGoEngine.analyze``，每段对「段首…段末」的每个前缀各算一次
  （每段 N+1 次），深度压到 ``FAST_VISITS``，不碰当前对局；
- **失败可读**：引擎 / LLM 任一失败都退回**基于棋谱与已有记录**的兜底点评，绝不让复盘卡死。
"""

import math

from . import actions, events, prompts
from .agent_graph import _emit_line

MAX_SEGMENTS = 8
MIN_SPAN = 12
MAX_SPAN = 48
FAST_VISITS = 120
COMMENT_TOKENS = 500
THINK_MODE = 2


def plan_segments(total) -> list:
    """把 ``total`` 手切成 ``[start, end)`` 段：尽量 8 段以内、每段至少 12 手、最长 48 手。"""
    try:
        n = max(0, int(total))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return []
    count = max(1, min(MAX_SEGMENTS, math.ceil(n / MIN_SPAN)))
    count = max(count, math.ceil(n / MAX_SPAN))
    base, extra = divmod(n, count)
    out = []
    start = 0
    for i in range(count):
        span = base + (1 if i < extra else 0)
        out.append([start, start + span])
        start += span
    return out


def _fmt_wr(v) -> str:
    try:
        return "%.1f%%" % (float(v) * 100.0)
    except (TypeError, ValueError):
        return "—"


def _visits(agent) -> int:
    try:
        configured = int(getattr(agent.katago, "max_visits", FAST_VISITS))
    except (TypeError, ValueError):
        configured = FAST_VISITS
    return max(20, min(configured, FAST_VISITS))


async def analyze_segment(engine, moves, start, end, visits=None) -> dict:
    """分析 ``[start, end)`` 这一段：前缀 start..end 各算一次，复用相邻结果。

    返回 ``{engine_ok, rows, key_moves, good_moves, wr_start, wr_end, error}``。
    引擎在中途失败时保留已算出的前缀，``engine_ok=False``，不抛。
    """
    total = len(moves) if isinstance(moves, list) else 0
    start = max(0, min(int(start), total))
    end = max(start, min(int(end), total))
    points = {}
    engine_ok = True
    error = ""
    n_visits = visits if visits is not None else FAST_VISITS
    for prefix in range(start, end + 1):
        try:
            res = await engine.analyze(moves[:prefix], max_visits=n_visits)
        except Exception as e:                  # noqa: BLE001 —— 单段失败退兜底，不拖垮整盘
            res = {"error": str(e)}
        if not isinstance(res, dict) or "error" in res:
            engine_ok = False
            error = str(res.get("error")) if isinstance(res, dict) else "引擎返回异常"
            break
        conv = prompts.to_black_perspective(res, prefix)
        points[prefix] = {"wr": float(conv[0]), "score": float(conv[1])}

    rows = []
    for i in range(start + 1, end + 1):
        if (i - 1) not in points or i not in points:
            break
        before, after = points[i - 1], points[i]
        color = str(moves[i - 1][0]).lower() if isinstance(moves[i - 1], (list, tuple)) else ""
        color = "white" if color in ("w", "white") else "black"
        gtp = str(moves[i - 1][1]) if isinstance(moves[i - 1], (list, tuple)) and len(moves[i - 1]) > 1 else ""
        loss = (before["wr"] - after["wr"]) if color == "black" else (after["wr"] - before["wr"])
        rows.append({
            "move_no": i,
            "color": color,
            "gtp": gtp,
            "wr_before": round(before["wr"], 4),
            "wr_after": round(after["wr"], 4),
            "score_after": round(after["score"], 2),
            "loss": round(loss, 4),
        })

    key_moves = sorted([r for r in rows if r["loss"] >= 0.03],
                       key=lambda r: r["loss"], reverse=True)[:2]
    good_moves = sorted([r for r in rows if r["loss"] <= -0.05],
                        key=lambda r: r["loss"])[:1]
    wr_start = points[start]["wr"] if start in points else None
    wr_end = points[end]["wr"] if end in points else (
        points[start + len(rows)]["wr"] if (start + len(rows)) in points else None)
    return {
        "engine_ok": engine_ok,
        "error": error,
        "rows": rows,
        "key_moves": key_moves,
        "good_moves": good_moves,
        "wr_start": wr_start,
        "wr_end": wr_end,
    }


def _row_line(r: dict) -> str:
    return "第%d手 %s %s：黑胜率 %s → %s" % (
        int(r["move_no"]), "你" if r["color"] == "black" else "我", str(r["gtp"]),
        _fmt_wr(r.get("wr_before")), _fmt_wr(r.get("wr_after")))


def _quality(r: dict) -> str:
    loss = float(r.get("loss", 0.0))
    if loss >= 0.08:
        return "这一手明显亏了"
    if loss >= 0.03:
        return "这一手有点亏"
    if loss <= -0.05:
        return "这一手是好棋"
    return "这一手还稳"


def _notes_in_range(record, start, end):
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    notes = record.get("notes")
    if not isinstance(notes, list):
        notes = meta.get("notes")
    out = []
    if isinstance(notes, list):
        for n in notes:
            if not isinstance(n, dict):
                continue
            try:
                move_no = int(n.get("move_no", 0))
            except (TypeError, ValueError):
                continue
            text = str(n.get("text") or "").strip()
            if start < move_no <= end and text:
                who = "你" if str(n.get("side", "")) == "E" else "我"
                out.append("第%d手 %s：%s" % (move_no, who, text))
            if len(out) >= 4:
                break
    losses = record.get("losses")
    if not isinstance(losses, list):
        losses = meta.get("losses")
    if isinstance(losses, list):
        for ls in losses:
            if not isinstance(ls, dict):
                continue
            try:
                move_no = int(ls.get("move_no", 0))
            except (TypeError, ValueError):
                continue
            if start < move_no <= end:
                out.append("第%d手 %s：%s" % (move_no, str(ls.get("gtp", "")),
                                              str(ls.get("reason", ""))))
            if len(out) >= 6:
                break
    return out


def build_messages(agent, record, seg, index, total, previous="") -> list:
    """拼这一段的提示词：系统人设 + 本段形势 / 棋谱 / 关键处 / 已有记录 / 上一段。"""
    moves = record.get("moves") if isinstance(record.get("moves"), list) else []
    start = int(seg.get("from", 0))
    end = int(seg.get("to", 0))
    player = str(getattr(agent, "player_name", "") or "Estarth")
    rows = seg.get("rows") or []
    parts = [
        "你正在和%s一起复盘棋谱库里的一盘棋（共%d手）。这是第%d/%d段，覆盖第%d~%d手。"
        % (player, len(moves), index + 1, total, start + 1, end),
    ]
    if seg.get("engine_ok") and seg.get("wr_start") is not None and seg.get("wr_end") is not None:
        parts.append("【本段形势】段首黑胜率 %s，段末黑胜率 %s。"
                     % (_fmt_wr(seg.get("wr_start")), _fmt_wr(seg.get("wr_end"))))
    else:
        parts.append("【本段形势】引擎这一段没算清；先按棋谱和你记得的点评讲，别编胜率。")
    if rows:
        parts.append("【本段棋谱与胜率】\n" + "\n".join(_row_line(r) for r in rows))
    else:
        lines = []
        for i in range(start, min(end, len(moves))):
            color = "黑" if str(moves[i][0]).lower() in ("b", "black") else "白"
            lines.append("第%d手 %s %s" % (i + 1, color, moves[i][1]))
        parts.append("【本段棋谱】\n" + ("\n".join(lines) if lines else "（空）"))
    keys = []
    for r in (seg.get("key_moves") or []):
        keys.append("%s（%s）" % (_row_line(r), _quality(r)))
    for r in (seg.get("good_moves") or []):
        keys.append("%s（%s）" % (_row_line(r), _quality(r)))
    if keys:
        parts.append("【值得点的地方】\n" + "\n".join(keys))
    notes = _notes_in_range(record, start, end)
    if notes:
        parts.append("【你之前对这盘的点评】\n" + "\n".join(notes))
    summary = str(record.get("summary") or "").strip()
    if summary:
        parts.append("【这盘的局后总结（参考，别照念）】" + summary[:240])
    prev = str(previous or "").strip()
    if prev:
        parts.append("【你上一段刚说过（别重复）】" + prev[:220])
    parts.append(
        "【要求】用Azure的口吻（我/你，别说「黑方/白棋」）说 2~4 句，控制在 110 字内："
        "先概括这一段的走势，再点 1~2 个关键处；关键处可以带坐标，但别逐手念；"
        "语气慵懒但专业，像陪练在旁边一起看棋。"
    )
    parts.append(
        "可以在句首或句中自然插入行为标签：%s（每次最多一个，不要输出别的花括号）。"
        % actions.PROMPT_TAGS
    )
    if index == total - 1:
        parts.append("这是最后一段，收个尾：给%s一句具体、轻一点的建议或肯定。" % player)
    else:
        parts.append("别抢后面段落的结论，给下一段留一点伏笔。")
    return [
        {"role": "system", "content": agent._sys_content("")},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def fallback_text(seg, index, total) -> str:
    """LLM / 引擎失败时的可读兜底：仍然给出这一段的走势与一个关键处，不空着。"""
    rows = seg.get("rows") or []
    if seg.get("engine_ok") and seg.get("wr_start") is not None and seg.get("wr_end") is not None:
        start_wr = float(seg["wr_start"])
        end_wr = float(seg["wr_end"])
        delta = (end_wr - start_wr) * 100.0
        if delta >= 3.0:
            trend = "这一段黑棋追回来 %.0f 个百分点" % delta
        elif delta <= -3.0:
            trend = "这一段黑棋掉了 %.0f 个百分点" % abs(delta)
        else:
            trend = "这一段形势差不多，双方都在铺"
        if rows:
            worst = max(rows, key=lambda r: float(r.get("loss", 0.0)))
            if float(worst.get("loss", 0.0)) >= 0.03:
                return "%s。第%d手%s%s那里%s，我们再往下看。" % (
                    trend, int(worst["move_no"]),
                    "你" if worst.get("color") == "black" else "我",
                    str(worst.get("gtp", "")), _quality(worst))
        return trend + "。我们接着看下一段。"
    if rows:
        return "这一段我这边没算清形势，先按棋谱记着；第%d手那一带我们可以回头再抠一下。" % int(rows[-1]["move_no"])
    if index == total - 1:
        return "这一段我看完了，棋谱先记着；有空我们再从头抠一遍。"
    return "这一段我先记着，接着看下一段。"


def _usable(text: str) -> bool:
    t = str(text or "").strip()
    return t != "" and not t.startswith("调用出错") and t != "调用失败"


async def comment_segment(agent, record, start, end, index=0, total=1, previous="") -> dict:
    """分析 + 生成点评 + 发 ``message``/``speech``；返回给 REST 的结构化结果。"""
    moves = record.get("moves") if isinstance(record.get("moves"), list) else []
    total_moves = len(moves)
    start = max(0, min(int(start), total_moves))
    end = max(start, min(int(end), total_moves))
    seg = await analyze_segment(agent.katago, moves, start, end, _visits(agent))
    text = ""
    if agent.llm is not None:
        try:
            msgs = build_messages(agent, record, {"from": start, "to": end, **seg},
                                  int(index), max(1, int(total)), previous)
            reply = await agent.llm.chat(msgs, max_tokens=COMMENT_TOKENS, think_mode=THINK_MODE)
            text = actions.naturalize(actions.clean_actions(agent.clean_reply(reply)))
        except Exception as e:                  # noqa: BLE001 —— 点评失败退兜底，不拖垮整盘
            print("[FullReview] 点评生成失败（用兜底）：%s" % e)
    if not _usable(text):
        text = fallback_text(seg, int(index), max(1, int(total)))
    if text:
        try:
            agent._new_turn()
            await _emit_line(agent, events.AI_REVIEW, text)
        except Exception as e:                  # noqa: BLE001 —— 说话/语音失败也不影响返回
            print("[FullReview] 发布点评失败（已忽略）：%s" % e)
    return {"from": start, "to": end, "text": text, **seg}
