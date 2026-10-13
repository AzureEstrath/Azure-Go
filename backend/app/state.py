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
        # 带标签的对局记忆：{tag, move_no, text}；E=Estarth(黑)的棋，A=Azure(白)的棋
        self.memory_log = []
        # 定式识别去重记录：{key, move_no}；随快照回滚（悔棋后可以重新讲解）
        self.joseki_log = []
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
        tag = "E"（对 Estarth 这手的分析/评价）或 "A"（Azure 自己的落子分析与思路）。
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

    def reset(self):
        self.moves = []
        self.move_seq = []
        self.board = GoBoard()
        self.snapshots = []
        self.bad_moves = []
        self.memory_log = []
        self.joseki_log = []
        self.chat_history = []
        self.pre_analysis = None
        self.last_katago = ""
        self.finished = False
