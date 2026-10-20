"""定式库扩库提取脚本（开发工具，阶段一 v3.6.0）。

用**已经在跑的** `katago_service`（`POST /analyze`）跑开局自对弈，把引擎给出的着法整理成
**草稿**库条目 JSON（`to_entry(...)` 的结果 + `note` 注记），**不改 `backend/app/joseki.py`**。

    用法：python tools/joseki_extract.py [service_url] [--out FILE] [--opening NAME]
                                         [--plies N] [--visits N] [--token T] [--timeout-sec S]
    例：  python tools/joseki_extract.py --out drafts.json --plies 12 --visits 400

三条硬约束：

1. **草稿必须逐条人工核对后才可入库**——本脚本只「提取 + 归一化 + 去重」，不生成棋理内容；
   条目里的 `key`（讲解词）一律留空，由人补写。未核对的草稿**不得**直接粘进 `JosekiData.LIST`。
2. **角部归一化口径来自 `backend/app/joseki.py`（`coords.gtp_to_xy` / `joseki._norm` /
   `joseki.REGION` / `joseki.CORNERS`），本脚本不另立一套**；只读该模块的常量与口径函数，
   **不 import、不改写 `JosekiData`**。
3. **引擎不是本脚本的参数**：引擎与权重由 `katago_service` 的部署侧持有（`--katago-exe/--model/
   --config` 或环境变量 `KATAGO_EXE/KATAGO_MODEL/KATAGO_CONFIG` 是**服务侧**的参数）。本脚本只连
   服务地址；连不上/服务报错时打一行可读错误后非 0 退出，不堆栈刷屏。

导入面（U8）：模块顶层只做「把 `<仓库根>/backend` 加进 `sys.path` → `from app import coords, joseki`」，
不联网、不起进程、不读环境变量、不 `parse_args()`——单测可以在没有引擎、没有网、没有环境变量的
机器上只 import 三个纯函数。
"""

import sys
from pathlib import Path

# `tools/` 在仓库根，而 `app` 在 `backend/`：导入口径函数前先把 backend 放上 sys.path（U14）。
# 顶层只做这一件事，其余（HTTP / argparse / 环境变量）全部惰性化。
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import coords, joseki              # noqa: E402  （口径来源：唯一，见文件头第 2 条）

DEFAULT_SERVICE_URL = "http://127.0.0.1:8100"
DEFAULT_PLIES = 12                # 每条线自对弈多少手
DEFAULT_VISITS = 400              # 每次分析的 maxVisits（与库内条目提取时同档）
DEFAULT_TIMEOUT_SEC = 60.0        # 单次 HTTP 超时

# 角号口径同 `joseki._norm`：0=左上 1=右上 2=左下 3=右下；这里是 family 后缀用的短名。
CORNER_SLUG = ("lt", "rt", "lb", "rb")

# 开局前缀：黑先、GTP 坐标（已按 coords 核对：Q16=右上星位 4-4、D4=左下星位、
# D17=左上小目 3-4、C17=左上三三 3-3）。之后由服务侧引擎自对弈续下。
OPENINGS = {
    "star":         {"cat": "star",    "label": "星位",       "moves": [["B", "Q16"]]},
    "star_star":    {"cat": "star",    "label": "星位对星位", "moves": [["B", "Q16"], ["W", "D4"]]},
    "star_komoku":  {"cat": "komoku",  "label": "星位对小目", "moves": [["B", "Q16"], ["W", "D17"]]},
    "star_san_san": {"cat": "san_san", "label": "星位对三三", "moves": [["B", "Q16"], ["W", "C17"]]},
}


class ExtractionError(Exception):
    """可读的提取失败（连不上服务 / 服务报错 / 响应不成形）：只报一句，不带堆栈。"""


# ================= 纯函数（单测目标；无 IO、无环境依赖） =================

def corner_normalize(moves, corner):
    """整盘手顺里落在 `corner` 号角内的着手 → 归一化序列 `[[col, nx, ny], ...]`。

    - `moves` 与 `joseki.JosekiRecognizer.scan` 的入参同形：`[["B","Q16"],["W","pass"], ...]`。
    - 口径**复用** `app.joseki`：坐标换算 `coords.gtp_to_xy`、角部映射 `joseki._norm`、
      角内半径 `joseki.REGION`——本脚本不另立（与 `scan` 内部构造 `seq` 同口径）。
    - 按手顺收集，跳过 `pass`（解析为 (-1,-1)）与角外着手；
      `col` 取 `0`（该角**先落子**的一方）/ `1`（另一方）。
    - 角内着子不足 2 手返回 `[]`。
    """
    if corner not in (0, 1, 2, 3):
        return []
    seq = []
    first = ""
    for mv in moves:
        xy = coords.gtp_to_xy(str(mv[1]))
        if xy[0] < 0:                                   # pass / 非法坐标
            continue
        n = joseki._norm(corner, xy[0], xy[1])
        if max(n[0], n[1]) >= joseki.REGION:            # 角外
            continue
        col = str(mv[0])
        if not first:
            first = col
        seq.append([0 if col == first else 1, int(n[0]), int(n[1])])
    if len(seq) < 2:
        return []
    return seq


def _is_prefix(short, long_):
    """`short` 是否为 `long_` 的前缀（含完全相等）。"""
    if len(short) > len(long_):
        return False
    return all(tuple(short[i]) == tuple(long_[i]) for i in range(len(short)))


def dedupe_lines(lines):
    """互为前缀（含完全相等）的线条只留**最长**的那条，其余丢弃；互不构成的都保留。

    输出顺序稳定：按各保留线**首次出现**的位置（被更长的线顶替时仍占原位置）。
    """
    keep = []
    for ln in lines:
        cur = [[int(v) for v in step] for step in ln]
        related = False
        for i, ex in enumerate(keep):
            if _is_prefix(ex, cur) or _is_prefix(cur, ex):
                if len(cur) > len(ex):
                    keep[i] = cur                       # 更长的留下，位置不变
                related = True
                break
        if not related:
            keep.append(cur)
    return keep


def to_entry(family, name, cat, seq, key):
    """组装一条与 `JosekiData.LIST` 同形的库条目。

    键集合**精确**为 `family` / `name` / `cat` / `seq` / `key` 五个（与
    `backend/app/joseki.py` 的现有条目一致），`seq` 为 `[[col, nx, ny], ...]`。
    """
    return {
        "family": str(family),
        "name": str(name),
        "cat": str(cat),
        "seq": [[int(a), int(b), int(c)] for a, b, c in seq],
        "key": str(key),
    }


# ================= 提取（联网，仅 CLI 走这里） =================

def _seq_key(seq):
    return tuple(tuple(int(v) for v in step) for step in seq)


def _moves_text(moves):
    """手顺文本：`B Q16、W D4`。"""
    return "、".join("%s %s" % (str(mv[0]), str(mv[1])) for mv in moves)


def _service_hint():
    """连不上服务时的提示：引擎路径是**服务侧**参数，本脚本只认服务地址。"""
    import os

    hint = ("请确认 katago_service 已在运行（katago_service\\run_server.bat，"
            "默认 127.0.0.1:8100）；引擎路径属服务侧参数，本脚本不接受")
    seen = [k for k in ("KATAGO_EXE", "KATAGO_MODEL", "KATAGO_CONFIG") if os.environ.get(k)]
    if seen:
        hint += "（检测到服务侧环境变量：%s）" % "、".join(seen)
    return hint


def _request_json(service_url, path, payload, token, timeout_s):
    """请求一个 JSON 端点；任何失败一律转成 `ExtractionError`（不往外抛堆栈）。"""
    import json
    import urllib.error
    import urllib.request

    url = service_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8"))
            if isinstance(body, dict):
                detail = str(body.get("error", ""))
        except Exception:
            detail = ""
        raise ExtractionError("服务返回 HTTP %d%s" % (e.code, "：%s" % detail if detail else ""))
    except urllib.error.URLError as e:
        # 系统给的原因串自带句号，去掉后由调用方（main）接提示语，避免「。；」连着出现
        reason = str(getattr(e, "reason", e)).strip().rstrip("。")
        raise ExtractionError("连不上 KataGo 云服务 %s：%s" % (service_url, reason))
    except ExtractionError:
        raise
    except Exception as e:                              # 超时 / 响应不是 JSON 等
        raise ExtractionError("请求 %s 失败：%s" % (url, e))


def _health_state(service_url, token, timeout_s):
    """`/health` 的 `state`（`warming` / `ready` / `dead`）；只认 state，不认 ok。"""
    body = _request_json(service_url, "/health", None, token, timeout_s)
    if not isinstance(body, dict):
        raise ExtractionError("KataGo 云服务 /health 响应不是 JSON 对象")
    return str(body.get("state", ""))


def _analyze_once(service_url, moves, visits, token, timeout_s):
    """一次 `POST /analyze`；请求体字段与 `backend/app/engine.py` 的客户端一致，返回引擎原文。"""
    req = {
        "id": "joseki_extract",
        "moves": [[str(mv[0]), str(mv[1])] for mv in moves],
        "rules": "chinese",
        "komi": 7.5,
        "boardXSize": coords.SIZE,
        "boardYSize": coords.SIZE,
        "maxVisits": int(visits),
    }
    body = _request_json(service_url, "/analyze", req, token, timeout_s)
    if not isinstance(body, dict):
        raise ExtractionError("KataGo 云服务 /analyze 响应不是 JSON 对象")
    return body


def _next_color(moves):
    """黑先：已有手数为偶数时轮到黑。"""
    return "B" if len(moves) % 2 == 0 else "W"


def _visits_of(info):
    """候选的 visits：引擎理论上总给整数，缺字段 / 不成形一律按 0（外部响应不可信）。"""
    try:
        return int(info.get("visits", 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _play_line(service_url, opening, plies, visits, token, timeout_s):
    """从开局前缀续下：每手取 top-1，连续若干手都唯一才继续。

    top-1 不唯一（visits 并列）/ 引擎建议 pass / 引擎没给候选，都**截断在上一手**（不猜、不编造）；
    引擎报错直接抛 `ExtractionError`。返回 `(moves, stopped)`：`moves` 含开局前缀，`stopped` 是停线
    原因（正常下满为 `""`）。
    """
    moves = [list(mv) for mv in opening]
    for _ in range(plies):
        reply = _analyze_once(service_url, moves, visits, token, timeout_s)
        if "error" in reply:
            raise ExtractionError("引擎回答出错：%s" % reply.get("error"))
        infos = reply.get("moveInfos") or []
        if not infos:
            return moves, "引擎没给候选"
        top = infos[0]                                  # KataGo 按 visits 降序，第 0 条即 top-1
        if len(infos) > 1 and _visits_of(infos[1]) >= _visits_of(top):
            return moves, "第 %d 手 top-1 不唯一（visits 并列），已截断" % (len(moves) + 1)
        mv = str(top.get("move", ""))
        if not mv or mv.lower() == "pass":
            return moves, "第 %d 手引擎建议 pass，已截断" % (len(moves) + 1)
        moves.append([_next_color(moves), mv])
    return moves, ""


def _draft_note(service_url, draft, seq, visits):
    """草稿注记：写清这条线怎么切出来的，并显式标注「未人工核对」。"""
    note = ("草稿（未人工核对）：开局「%s」（%s），角=%s；起点 %s；引擎自对弈 %d 手，"
            "角内取到 %d 手"
            % (draft["label"], draft["opening"], joseki.CORNERS[draft["corner"]],
               _moves_text(draft["start"]),
               len(draft["moves"]) - len(draft["start"]), len(seq)))
    if draft["stopped"]:
        note += "（%s）" % draft["stopped"]
    note += ("；来源服务 %s，visits=%d。入库前必须逐手人工复核，并补写讲解词 key。"
             % (service_url, visits))
    return note


def extract(service_url, names, plies, visits, token, timeout_s):
    """逐开局跑自对弈、逐角归一化、跨开局去重，返回草稿条目列表（联网）。"""
    state = _health_state(service_url, token, timeout_s)
    if state != "ready":
        raise ExtractionError("KataGo 云服务不是 ready（/health state=%s）" % (state or "未知"))

    drafts = []
    for name in names:
        spec = OPENINGS[name]
        start = [list(mv) for mv in spec["moves"]]
        moves, stopped = _play_line(service_url, start, plies, visits, token, timeout_s)
        for c in range(4):
            seq = corner_normalize(moves, c)
            if len(seq) < 2:                            # 角内不足 2 手不成课
                continue
            drafts.append({"seq": seq, "opening": name, "label": spec["label"],
                           "cat": spec["cat"], "corner": c, "start": start,
                           "moves": moves, "stopped": stopped})

    by_seq = {}
    for d in drafts:
        by_seq.setdefault(_seq_key(d["seq"]), d)        # 同形线条取首次出现的那条
    out = []
    for seq in dedupe_lines([d["seq"] for d in drafts]):
        d = by_seq[_seq_key(seq)]
        entry = to_entry(
            family="%s_%s" % (d["opening"], CORNER_SLUG[d["corner"]]),
            name="%s·%s" % (d["label"], joseki.CORNERS[d["corner"]]),
            cat=d["cat"],
            seq=seq,
            key="",                                     # 讲解词留空：只提取，不编造
        )
        entry["note"] = _draft_note(service_url, d, seq, visits)
        out.append(entry)
    return out


# ================= CLI（惰性 import + 无 import-time 副作用） =================

def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python tools/joseki_extract.py",
        description="定式库扩库提取：连已经在跑的 kata_service 自对弈，产出草稿 JSON"
                    "（不改 joseki.py；草稿必须逐条人工核对后才可入库）。",
    )
    parser.add_argument("service_url", nargs="?", default=DEFAULT_SERVICE_URL,
                        help="katago_service 地址（默认 %s）" % DEFAULT_SERVICE_URL)
    parser.add_argument("--out", default="", help="草稿 JSON 输出文件；留空打到 stdout")
    parser.add_argument("--opening", action="append", default=[], choices=sorted(OPENINGS),
                        help="要提取的开局，可重复（默认全部：%s）" % "、".join(sorted(OPENINGS)))
    parser.add_argument("--plies", type=int, default=DEFAULT_PLIES,
                        help="每条线自对弈多少手（默认 %d）" % DEFAULT_PLIES)
    parser.add_argument("--visits", type=int, default=DEFAULT_VISITS,
                        help="每次分析的 maxVisits（默认 %d）" % DEFAULT_VISITS)
    parser.add_argument("--token", default="",
                        help="Bearer 令牌（服务侧配了 KATAGO_SERVICE_TOKEN 时才需要）")
    parser.add_argument("--timeout-sec", dest="timeout_sec", type=float,
                        default=DEFAULT_TIMEOUT_SEC,
                        help="单次 HTTP 超时秒数（默认 %s）" % DEFAULT_TIMEOUT_SEC)
    args = parser.parse_args(argv)

    if args.plies < 0:
        print("[joseki_extract] --plies 不能为负")
        return 2
    if args.visits <= 0:
        print("[joseki_extract] --visits 必须为正")
        return 2

    names = args.opening or sorted(OPENINGS)
    try:
        entries = extract(args.service_url, names, args.plies, args.visits,
                          args.token, args.timeout_sec)
    except ExtractionError as e:
        print("[joseki_extract] %s；%s" % (e, _service_hint()))
        return 2

    text = json.dumps(entries, ensure_ascii=False, indent=2)
    if not args.out:
        print(text)                                     # 默认：纯 JSON 打到 stdout，可直接重定向
        return 0
    try:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        print("[joseki_extract] 写文件失败（%s）：%s" % (args.out, e))
        return 2
    print("[joseki_extract] 已写出 %d 条草稿 → %s（未人工核对，入库前必须逐条复核）"
          % (len(entries), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
