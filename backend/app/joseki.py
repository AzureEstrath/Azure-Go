"""定式库与定式识别器。

逐方法移植自 AzureGoGodot/scripts/core/JosekiData.gd + JosekiRecognizer.gd。

定式识别器：纯本地计算、零引擎调用（每手几十次比较，不在热路径上）
做法：取「最新一手所在角」的全部着子序列做角部归一化，
     再对定式库做 4 组变换（2 朝向 × 2 换色）下的最长前缀游走。
识别结果：
  formed   —— 序列恰好走完某条定式（刚成型）：points = 该定式实际棋盘点（供棋盘高亮轨迹）
  deviated —— 玩家在角内落子偏离了已吻合 ≥MIN_PREFIX 手的定式：expect = 库中下一手的棋盘点
"""

from . import board
from . import coords


class JosekiData:
    """入门定式库（角部归一化序列）。

    编码：每条 seq 为 [[col, nx, ny], ...]
      - col：0 = 该角先落子的一方，1 = 另一方（识别时两种换色都会尝试）
      - nx/ny：角部归一化坐标（0 基，距角越近越小；星位=(3,3) 小目=(2,3) 三三=(2,2)）
      - 只写一种朝向；对角镜像由识别器自动覆盖；pass 与角外的着手在识别时自动跳过
    数据来源：本地 KataGo（b18c384nbt，400 visits）自对弈实测提取（2026-10）
    条目分工：核心（约 4 手）负责「成型讲解」；延伸条目用于继续跟线、避免把正常次序误判为偏离
    """

    LIST = [
        # —— 星位（4-4）· 点三三 ——
        {
            "name": "点三三·基本型", "cat": "star", "family": "star_33",
            "seq": [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2]],
            "key": "对方点三三就还他角地：挡在这边守住外侧，之后可以先手转向别处——现在最常见的开局就是它。",
        },
        {
            "name": "点三三·定形", "cat": "star", "family": "star_33",
            "seq": [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2], [0, 5, 2], [1, 4, 2], [0, 4, 3], [1, 5, 1]],
            "key": "点三三之后的定形：先小飞取外势，再顺势压过来，对方爬边做活。走完角上告一段落。",
        },
        {
            "name": "点三三·定形", "cat": "star", "family": "star_33",
            "seq": [[0, 3, 3], [1, 2, 2], [0, 2, 3], [1, 3, 2], [0, 4, 3], [1, 5, 2]],
            "key": "点三三之后的另一个次序：先压再飞，效果一样——外势取到、角地给对方，之后转大场。",
        },
        # —— 星位 · 小飞挂 ——
        {
            "name": "小飞挂·靠压定式", "cat": "star", "family": "star_kakari_small",
            "seq": [[0, 3, 3], [1, 5, 2], [0, 4, 2], [1, 5, 3]],
            "key": "对方小飞挂角时靠上去再长：把他压低、自己走厚，是很稳的应对。",
        },
        {
            "name": "小飞挂·靠压定形", "cat": "star", "family": "star_kakari_small",
            "seq": [[0, 3, 3], [1, 5, 2], [0, 4, 2], [1, 5, 3], [0, 3, 5]],
            "key": "靠压之后拆二安定：这块棋已经站稳，可以放心脱先去下别处。",
        },
        # —— 星位 · 双飞燕 ——
        {
            "name": "双飞燕·靠压一边", "cat": "star", "family": "star_double_kakari",
            "seq": [[0, 3, 3], [1, 2, 5], [1, 5, 2], [0, 3, 5]],
            "key": "对方两边小飞挂（双飞燕）时先靠住一边：别两边都想要，处理完一边再回头。",
        },
        {
            "name": "双飞燕·靠压一边", "cat": "star", "family": "star_double_kakari",
            "seq": [[0, 3, 3], [1, 2, 5], [1, 5, 2], [0, 3, 5], [1, 3, 6], [0, 4, 5], [1, 2, 3]],
            "key": "双飞燕继续：靠完之后顺势连长，把外势往中央走，将来再经营另一边。",
        },
    ]


REGION = 8         # 距角 8 路以内视作该角
MIN_PREFIX = 4     # 偏离提醒的最低吻合手数（不足不打扰）
CORNERS = ["左上", "右上", "左下", "右下"]


def _norm(c, x, y):
    """角部归一化：0=左上 1=右上 2=左下 3=右下（y=0 为顶边，向下递增）。"""
    if c == 0:
        return (x, y)
    if c == 1:
        return (board.SIZE - 1 - x, y)
    if c == 2:
        return (x, board.SIZE - 1 - y)
    if c == 3:
        return (board.SIZE - 1 - x, board.SIZE - 1 - y)
    return (-1, -1)


def _denorm(c, nx, ny):
    if c == 0:
        return (nx, ny)
    if c == 1:
        return (board.SIZE - 1 - nx, ny)
    if c == 2:
        return (nx, board.SIZE - 1 - ny)
    if c == 3:
        return (board.SIZE - 1 - nx, board.SIZE - 1 - ny)
    return (-1, -1)


def _opp(c):
    return "W" if c == "B" else "B"


def _match_prefix(seq, pat, first_col, tr, same):
    """前缀吻合长度：颜色与坐标均一致才算吻合。"""
    k = 0
    n = min(len(seq), len(pat))
    for i in range(n):
        item = pat[i]
        want = first_col if (item[0] == 0) == same else _opp(first_col)
        s = seq[i]
        if s["col"] != want:
            return k
        px = item[2] if tr else item[1]
        py = item[1] if tr else item[2]
        if s["nx"] != px or s["ny"] != py:
            return k
        k += 1
    return k


def _better(a, b):
    """评分比较：完整匹配优先 → 吻合手数多优先 → 线条更长优先。"""
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    return a[2] > b[2]


class JosekiRecognizer:
    @staticmethod
    def scan(moves):
        """moves: GameState.moves 原样（[["B","Q16"],["W","pass"], ...]）。"""
        # 1) 最新一手落在哪个角（归一化后取离角最近的那个）
        last = (-1, -1)
        for i in range(len(moves) - 1, -1, -1):
            gt = str(moves[i][1])
            if gt == "pass":
                continue
            last = coords.gtp_to_xy(gt)
            break
        if last[0] < 0:
            return {}
        c = -1
        best_d = 999
        for ci in range(4):
            n0 = _norm(ci, last[0], last[1])
            d = max(n0[0], n0[1])
            if d < best_d:
                best_d = d
                c = ci
        if c < 0 or best_d >= REGION:
            return {}
        # 2) 该角内的全部着子（按手顺，跳过 pass 与角外）
        seq = []
        for mv in moves:
            gt = str(mv[1])
            if gt == "pass":
                continue
            xy = coords.gtp_to_xy(gt)
            if xy[0] < 0:
                continue
            n = _norm(c, xy[0], xy[1])
            if max(n[0], n[1]) < REGION:
                seq.append({"col": str(mv[0]), "nx": n[0], "ny": n[1], "gx": xy[0], "gy": xy[1]})
        if len(seq) < 2:
            return {}
        # 3) 定式库最长前缀游走（2 朝向：原样/对角镜像；2 换色：先落子方同色/异色）
        first_col = str(seq[0]["col"])
        best = {}
        for e in JosekiData.LIST:
            pat = e["seq"]
            if len(pat) < 2:
                continue
            for tr in (False, True):
                for same in (True, False):
                    k = _match_prefix(seq, pat, first_col, tr, same)
                    if k <= 0:
                        continue
                    complete = k == len(pat) and k == len(seq)
                    score = [1 if complete else 0, k, len(pat)]
                    if not best or _better(score, best["score"]):
                        best = {"e": e, "k": k, "tr": tr, "same": same, "score": score, "complete": complete}
        if not best:
            return {}
        k = best["k"]
        e = best["e"]
        pat = e["seq"]
        # side：以「角内最后一手」是谁下的为准（成型时即 k-1，偏离时即刚落的第 k+1 手）
        side = "player" if str(seq[-1]["col"]) == "B" else "azure"
        base = {
            "name": str(e["name"]), "cat": str(e["cat"]),
            "corner": CORNERS[c], "side": side, "k": k,
        }
        if best["complete"]:
            pts = []
            for i in range(k):
                pts.append((int(seq[i]["gx"]), int(seq[i]["gy"])))
            base["kind"] = "formed"
            base["key"] = str(e["key"])
            base["points"] = pts
            return base
        # 4) 偏离：玩家刚在角内落子、且与某个已吻合 4+ 手的定式不同（只对玩家开口，避免打扰）
        if k >= MIN_PREFIX and len(pat) > k and len(seq) == k + 1 and side == "player":
            item = pat[k]
            want_col = first_col if (item[0] == 0) == best["same"] else _opp(first_col)
            if str(seq[k]["col"]) == want_col:
                ex = int(item[2]) if best["tr"] else int(item[1])
                ey = int(item[1]) if best["tr"] else int(item[2])
                base["kind"] = "deviated"
                base["key"] = str(e["key"])
                base["expect"] = _denorm(c, ex, ey)
                return base
        return {}
