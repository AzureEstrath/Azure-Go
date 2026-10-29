"""SGF 导入 / 导出（v3.11.5）：只处理**标准 19 路围棋、单主分谱**。

设计口径：
- 导出写成 SGF FF[4] + UTF-8，着法用 ``;B[pd]`` / ``;W[dd]``，虚着写 ``B[]``；
- 逐手点评写在该手节点的 ``C`` 里（``你：…`` / ``Azure：…``），导入时还原成 ``move_notes``；
- 导入**只认主变化**（变体分支整棵跳过），并逐手用 ``GoBoard`` 重放校验：非法手 / 手序断裂 /
  让子摆子（AB/AW）/ 非 19 路一律可读拒绝，绝不把坏棋谱塞进棋谱库；
- 所有函数都**不抛到端点**：``parse_sgf`` 用 ``{"ok": False, "error": ...}`` 返回；
  ``to_sgf`` 只做纯文本拼装，脏记录最多丢掉一条着法，不抛。
"""

from . import coords
from .board import GoBoard

SIZE = 19
MAX_MOVES = 1000
MAX_TEXT = 200_000
APP_NAME = "AzureGo:3.12.0"


class SgfError(ValueError):
    """SGF 结构 / 坐标 / 规则错误（只在解析内部抛出，对外转成可读 error）。"""


# ================= 导出 =================

def _escape(text) -> str:
    return str(text or "").replace("\\", "\\\\").replace("]", "\\]")


def _move_parts(m):
    """着法统一成 ``(color, gtp)``；认 ``["black", "Q16"]`` 与 ``{"color", "gtp"}``。"""
    if isinstance(m, (list, tuple)) and len(m) >= 2:
        color, gtp = str(m[0]), str(m[1])
    elif isinstance(m, dict):
        color = str(m.get("color") or m.get("c") or "")
        gtp = str(m.get("gtp") or "")
    else:
        return "", ""
    c = color.strip().lower()
    if c in ("b", "black", "黑"):
        color = "black"
    elif c in ("w", "white", "白"):
        color = "white"
    else:
        return "", ""
    return color, gtp.strip()


def _to_sgf_point(gtp: str) -> str:
    if gtp == "" or gtp.lower() == "pass":
        return ""
    x, y = coords.gtp_to_xy(gtp)
    if x < 0:
        return ""
    return chr(97 + x) + chr(97 + y)


def _date_of(rec) -> str:
    d = str(rec.get("date") or "").strip()
    if d:
        return d[:10]
    ts = rec.get("ts")
    if ts is None:
        return ""
    try:
        import time
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _notes_of(rec) -> dict:
    """``move_no -> [行文本]``：点评 / 落子用意都写进 SGF 的 ``C``，观谱软件能直接看到。"""
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    notes = rec.get("notes")
    if not isinstance(notes, list):
        notes = meta.get("notes")
    out = {}
    if not isinstance(notes, list):
        return out
    for n in notes:
        if not isinstance(n, dict):
            continue
        try:
            move_no = int(n.get("move_no", 0))
        except (TypeError, ValueError):
            continue
        text = str(n.get("text") or "").strip()
        if move_no <= 0 or text == "":
            continue
        who = "你" if str(n.get("side", "")) == "E" else "Azure"
        out.setdefault(move_no, []).append("%s：%s" % (who, text))
    return out


def to_sgf(rec: dict) -> str:
    """把棋谱库记录写成 SGF 文本（纯拼装、不抛）。"""
    rec = rec if isinstance(rec, dict) else {}
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    moves = rec.get("moves") if isinstance(rec.get("moves"), list) else []
    black = str(meta.get("black") or meta.get("player") or rec.get("black") or "玩家")
    white = str(meta.get("white") or rec.get("white") or "Azure")
    result = str(rec.get("result") or "?").strip() or "?"
    summary = str(rec.get("summary") or "").strip()
    notes = _notes_of(rec)

    parts = [
        "(;GM[1]FF[4]CA[UTF-8]AP[%s]SZ[19]KM[7.5]DT[%s]PB[%s]PW[%s]RE[%s]"
        % (APP_NAME, _escape(_date_of(rec)), _escape(black), _escape(white), _escape(result))
    ]
    if summary:
        parts.append("GC[%s]" % _escape(summary))
    for i, m in enumerate(moves, 1):
        color, gtp = _move_parts(m)
        if color == "":
            continue
        tag = "B" if color == "black" else "W"
        node = ";%s[%s]" % (tag, _to_sgf_point(gtp))
        comment = "\n".join(notes.get(i, []))
        if comment:
            node += "C[%s]" % _escape(comment)
        parts.append(node)
    parts.append(")")
    return "".join(parts)


# ================= 解析 =================
#
# 只实现 SGF 的一个很小但正确的子集：属性用大写字母、值用 ``[...]``、变体用 ``(...)``。
# 不做通用 SGF 库（没有第三方依赖），但括号 / 转义 / 变体跳过都按规范处理。

def _read_value(s: str, i: int):
    """读到 ``]`` 为止；返回 ``(value, next_index)``。反斜杠转义、软换行都按 SGF 规范。"""
    n = len(s)
    i += 1                                   # 跳过 '['
    out = []
    while i < n:
        c = s[i]
        if c == "\\":
            i += 1
            if i < n:
                nxt = s[i]
                if nxt not in ("\r", "\n"):  # SGF 的「软换行」：反斜杠 + 换行直接吞掉
                    out.append(nxt)
                if nxt == "\r" and i + 1 < n and s[i + 1] == "\n":
                    i += 1
                i += 1
            continue
        if c == "]":
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise SgfError("SGF 属性值没有闭合")


def _skip_tree(s: str, i: int) -> int:
    """从 ``(`` 起跳过一整棵变体子树（属性值里的括号不算）。"""
    n = len(s)
    depth = 0
    while i < n:
        c = s[i]
        if c == "[":
            _, i = _read_value(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth <= 0:
                return i + 1
        i += 1
    raise SgfError("SGF 括号没有闭合")


def _walk(text: str):
    """返回 ``(nodes)``：每个节点是 ``{PROP: [value, ...]}``；变体分支整棵跳过。"""
    start = text.find("(")
    if start < 0:
        raise SgfError("这看起来不是 SGF（缺少主树）")
    i = start + 1
    n = len(text)
    nodes = []
    cur = None
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            i = _skip_tree(text, i)
            continue
        if c == ")":
            break
        if c == ";":
            cur = {}
            nodes.append(cur)
            i += 1
            continue
        if "A" <= c <= "Z":
            j = i
            while j < n and "A" <= text[j] <= "Z":
                j += 1
            key = text[i:j]
            vals = []
            while True:
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] == "[":
                    value, j = _read_value(text, j)
                    vals.append(value)
                else:
                    break
            if cur is None:
                raise SgfError("SGF 属性出现在第一个节点之前")
            cur.setdefault(key, []).extend(vals)
            i = j
            continue
        i += 1                               # 杂项字符（空白 / BOM 已剥）忽略
    if not nodes:
        raise SgfError("SGF 里没有节点")
    return nodes


def _first(node: dict, key: str, default: str = "") -> str:
    vals = node.get(key)
    if isinstance(vals, list) and vals:
        return str(vals[0])
    return default


def _sgf_point(value: str) -> str:
    """SGF 坐标 -> GTP；``""`` / ``tt`` 是虚着。"""
    v = str(value or "").strip()
    if v == "" or v.lower() == "tt":
        return "pass"
    if len(v) < 2:
        raise SgfError("坐标不完整：%s" % v)
    x = ord(v[0].lower()) - 97
    y = ord(v[1].lower()) - 97
    if not (0 <= x < SIZE and 0 <= y < SIZE):
        raise SgfError("坐标超出 19 路棋盘：%s" % v)
    return coords.xy_to_gtp(x, y)


def _comment_note(text: str, color: str):
    """SGF 注释 → ``(side, text)``：认导出时写的前缀（``你：`` / ``Azure：``），否则按落子方。"""
    s = str(text or "").strip()
    side = "E" if color == "black" else "A"
    for prefix, val in (("Azure：", "A"), ("Azure:", "A"), ("你：", "E"), ("你:", "E")):
        if s.startswith(prefix):
            side = val
            s = s[len(prefix):].strip()
            break
    return side, s


def parse_sgf(text) -> dict:
    """解析 SGF → ``{ok, moves, notes, summary, result, black, white, date}``。

    ``moves`` 形状与后端 ``GameState.moves`` 完全一致：``[["black", "Q16"], ...]``（虚着是 ``"pass"``）。
    """
    raw = str(text or "")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    if raw.strip() == "":
        return {"ok": False, "error": "空棋谱"}
    if len(raw) > MAX_TEXT:
        return {"ok": False, "error": "棋谱太大（超过 %d 字符）" % MAX_TEXT}
    try:
        nodes = _walk(raw)
    except SgfError as e:
        return {"ok": False, "error": str(e)}
    root = nodes[0]

    gm = _first(root, "GM")
    if gm not in ("", "1"):
        return {"ok": False, "error": "这不是围棋棋谱（GM[%s]）" % gm}
    sz = _first(root, "SZ", "19").strip()
    if ":" in sz:                             # SZ[19:19] 这种方形写法
        sz = sz.split(":", 1)[0].strip()
    if sz not in ("", "19"):
        return {"ok": False, "error": "只支持 19 路棋谱（SZ[%s]）" % sz}
    for setup in ("AB", "AW", "AE"):
        if root.get(setup):
            return {"ok": False, "error": "暂不支持带摆子 / 让子的棋谱（%s）" % setup}

    notes = []
    moves = []
    expected = ""
    for node in nodes[1:]:
        color = ""
        if node.get("B"):
            color = "black"
        elif node.get("W"):
            color = "white"
        if color:
            if expected and color != expected:
                return {"ok": False, "error": "第 %d 手黑白手序不连续" % (len(moves) + 1)}
            expected = "white" if color == "black" else "black"
            try:
                gtp = _sgf_point(_first(node, "B" if color == "black" else "W"))
            except SgfError as e:
                return {"ok": False, "error": "第 %d 手：%s" % (len(moves) + 1, e)}
            moves.append([color, gtp])
            if len(moves) > MAX_MOVES:
                return {"ok": False, "error": "着法太多（超过 %d 手）" % MAX_MOVES}
            comment = " ".join(v for v in node.get("C", []) if str(v).strip())
            if comment:
                side, note_text = _comment_note(comment, color)
                if note_text:
                    notes.append({"move_no": len(moves), "kind": "review",
                                  "side": side, "text": note_text})
        else:
            # 没有着法的节点：注释挂到上一手（部分软件会把点评单独放一个节点）
            comment = " ".join(v for v in node.get("C", []) if str(v).strip())
            if comment and moves:
                side, note_text = _comment_note(comment, moves[-1][0])
                if note_text:
                    notes.append({"move_no": len(moves), "kind": "review",
                                  "side": side, "text": note_text})

    if not moves:
        return {"ok": False, "error": "棋谱里没有落子"}

    # 规则校验：逐手重放（提子 / 打劫 / 自杀 / 占用点）
    board = GoBoard()
    for i, (color, gtp) in enumerate(moves, 1):
        if gtp == "pass":
            board.pass_move()
            continue
        x, y = coords.gtp_to_xy(gtp)
        res = board.play(x, y, color)
        if not res.get("ok"):
            return {"ok": False, "error": "第 %d 手 %s 不合法：%s" % (i, gtp, res.get("err", ""))}

    summary = _first(root, "GC") or _first(root, "C")
    return {
        "ok": True,
        "moves": moves,
        "notes": notes,
        "summary": summary.strip(),
        "result": _first(root, "RE").strip(),
        "black": _first(root, "PB").strip(),
        "white": _first(root, "PW").strip(),
        "date": _first(root, "DT").strip(),
    }
