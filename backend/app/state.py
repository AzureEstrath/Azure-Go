"""一局棋的全部状态（含文档中提到的「对局记忆」）。

逐方法移植自 AzureGoGodot/scripts/core/GameState.gd。
"""

import copy

from . import coords
from .board import GoBoard


class GameState:
    def __init__(self):
        self.moves = []            # 传给 KataGo 的配对序列 [["B","Q16"],["W","D4"],...]
        self.move_seq = []         # 展示/总结用的纯坐标序列 ["Q16","D4",...]
        self.board = GoBoard()     # 对应 GoBoard.gd
        self.snapshots = []        # 每个玩家手之前的完整状态，供俗手回档
        self.bad_moves = []        # 最近三次俗手
        # 本局逐手存档（v3.7.0）：{move_no, kind, side, text}；kind="aim"＝落子宣告（她这一手的用意），
        # kind="review"＝点评/落子后感想；side：E=玩家，A=Azure。**纯追加：不滚动、不截断**
        # （memory_log 是它旁边那份滚动 18 条 / 截 80 字的副本，两者互不影响）
        self.move_notes = []
        # 本局全部真亏手（v3.7.0）：{move_no, gtp, drop, reason}；只有 drop > 0.10 才入账，不截断。
        # bad_moves 语义不变（最近 3 条俗手，供右侧栏「俗手回档」），这是另开的一份完整账
        self.losses = []
        # 带标签的对局记忆：{tag, move_no, text}；E=玩家(黑)的棋，A=Azure(白)的棋
        self.memory_log = []
        # 定式识别去重记录：{key, move_no}；随快照回滚（悔棋后可以重新讲解）
        self.joseki_log = []
        # 定式族计数（T5）：本局已计过数的族，以及本局已被退回计数的族（供 undo 用）；
        # 两者都随快照回滚（悔棋重走那一族时，退回的那一次可以重新计入）
        self.joseki_counted = []
        self.joseki_dropped = []
        # 本局玩家每手的 rank（v3.7.0 定式提示门控的棋力画像）：-1 = 不在她算的前 5。
        # **纯局内信号**——不落盘、不发事件、不进 payload；随快照回滚（悔棋后那一手不再计入画像）。
        self.player_ranks = []
        self.chat_history = []
        self.allow_pass = False    # 是否允许 Azure 虚着
        self.pre_analysis = None   # {n, a} 落子前局面分析缓存
        self.last_katago = ""      # 最近一次 KataGo 形势的文字摘要（胜率/目差/实地估算）
        self.finished = False

    def add_move(self, color, gtp):
        self.moves.append([color, gtp])
        self.move_seq.append(gtp)

    def make_snapshot(self):
        return {
            "board": self.board.clone(),
            "moves": copy.deepcopy(self.moves),
            "move_seq": list(self.move_seq),
            "memory_log": copy.deepcopy(self.memory_log),
            "joseki_log": copy.deepcopy(self.joseki_log),
            "joseki_counted": copy.deepcopy(self.joseki_counted),
            "joseki_dropped": copy.deepcopy(self.joseki_dropped),
            "player_ranks": copy.deepcopy(self.player_ranks),
        }

    def last_played_xy(self):
        """最近一手实际落子的交叉点（跳过 pass）。"""
        for i in range(len(self.move_seq) - 1, -1, -1):
            if self.move_seq[i] != "pass":
                return coords.gtp_to_xy(str(self.move_seq[i]))
        return (-1, -1)

    def payload(self):
        return {
            "stones": self.board.stones_list(self.last_played_xy()),
            "move_count": len(self.moves),
            "move_seq": list(self.move_seq),
            "bad_moves": copy.deepcopy(self.bad_moves),
            "allow_pass": self.allow_pass,
        }

    def log_memory(self, tag, text, move_no):
        """记录一条带标签的对局记忆（思考前回忆用）：
        tag = "E"（对玩家这手的分析/评价）或 "A"（Azure 自己的落子分析与思路）。
        """
        t = " ".join(s for s in text.split("\n") if s != "").strip()
        if t == "":
            return
        self.memory_log.append({"tag": tag, "move_no": move_no, "text": t[:80]})
        while len(self.memory_log) > 18:
            self.memory_log.pop(0)

    def record_bad_move(self, turn_index, gtp, reason):
        self.bad_moves.append({
            "turn_index": turn_index,
            "move_no": len(self.move_seq),
            "gtp": gtp,
            "reason": reason,
        })
        while len(self.bad_moves) > 3:
            self.bad_moves.pop(0)

    def note_move(self, move_no, kind, side, text):
        """逐手存档（v3.7.0）：**纯追加，不滚动、不截断**（与 memory_log 互不影响）。

        kind = "aim"（她这一手的用意，落子宣告的原文）或 "review"（点评 / 落子后感想）；
        side = "E"（玩家）或 "A"（Azure）。存的是**未截断的原文**。
        """
        self.move_notes.append({
            "move_no": move_no,
            "kind": kind,
            "side": side,
            "text": text,
        })

    def record_loss(self, gtp, drop, reason):
        """记一条真亏手（v3.7.0）：本局全部、不截断；**只有 ``drop > 0.10`` 才入账**。

        ``drop`` 与俗手判定同一个量（winrate_before - after_black_wr）；「只掉名次不掉胜率」
        （drop == 0）不入 ``losses``。俗手 ``bad_moves`` 的判定与 3 条上限不在这里、也不受影响。
        """
        if drop > 0.10:
            self.losses.append({
                "move_no": len(self.move_seq),
                "gtp": gtp,
                "drop": drop,
                "reason": reason,
            })

    def reset(self):
        self.moves = []
        self.move_seq = []
        self.board = GoBoard()
        self.snapshots = []
        self.bad_moves = []
        self.move_notes = []
        self.losses = []
        self.memory_log = []
        self.joseki_log = []
        self.joseki_counted = []
        self.joseki_dropped = []
        self.player_ranks = []
        self.chat_history = []
        self.pre_analysis = None
        self.last_katago = ""
        self.finished = False
