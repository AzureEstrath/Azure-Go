"""完整围棋规则棋盘：提子、打劫、禁着点、自杀禁止。

逐方法移植自 AzureGoGodot/scripts/core/GoBoard.gd。
"""

from . import coords

SIZE = 19
EMPTY = ""
KO_NONE = (-1, -1)
DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


class GoBoard:
    def __init__(self):
        # 列优先：grid[x][y] = "" / "black" / "white"
        self.grid = [[EMPTY] * SIZE for _ in range(SIZE)]
        self.ko_point = KO_NONE

    @staticmethod
    def opposite(color):
        return "white" if color == "black" else "black"

    def at(self, x, y):
        if not coords.valid(x, y):
            return EMPTY
        return self.grid[x][y]

    def _neighbors(self, x, y):
        out = []
        for dx, dy in DIRS:
            nx = x + dx
            ny = y + dy
            if 0 <= nx < SIZE and 0 <= ny < SIZE:
                out.append((nx, ny))
        return out

    def _group_and_liberties(self, x, y):
        """返回 (group, libs)。"""
        color = self.grid[x][y]
        stack = [(x, y)]
        seen = {(x, y): True}
        libs = 0
        group = []
        while stack:
            p = stack.pop()
            group.append(p)
            for n in self._neighbors(p[0], p[1]):
                v = self.grid[n[0]][n[1]]
                if v == EMPTY:
                    libs += 1
                elif v == color and n not in seen:
                    seen[n] = True
                    stack.append(n)
        return group, libs

    def play(self, x, y, color):
        """落子并返回 {ok, captured, err}；会修改棋盘。"""
        if not coords.valid(x, y):
            return {"ok": False, "captured": [], "err": "超出棋盘"}
        if self.grid[x][y] != EMPTY:
            return {"ok": False, "captured": [], "err": "此处已有棋子"}
        if self.ko_point == (x, y):
            return {"ok": False, "captured": [], "err": "打劫禁着点"}
        opp = self.opposite(color)
        self.grid[x][y] = color
        captured = []
        for n in self._neighbors(x, y):
            if self.grid[n[0]][n[1]] == opp:
                g, libs = self._group_and_liberties(n[0], n[1])
                if libs == 0:
                    captured.extend(g)
        for p in captured:
            self.grid[p[0]][p[1]] = EMPTY
        own_group, own_libs = self._group_and_liberties(x, y)
        if own_libs == 0 and len(captured) == 0:
            self.grid[x][y] = EMPTY
            return {"ok": False, "captured": [], "err": "自杀禁着点"}
        if len(captured) == 1 and len(own_group) == 1 and own_libs == 1:
            self.ko_point = captured[0]
        else:
            self.ko_point = KO_NONE
        return {"ok": True, "captured": captured, "err": ""}

    def is_legal(self, x, y, color):
        """只检查不落子。"""
        tmp = self.clone()
        return tmp.play(x, y, color)

    def pass_move(self):
        self.ko_point = KO_NONE

    def clone(self):
        b = GoBoard()
        for x in range(SIZE):
            b.grid[x] = list(self.grid[x])
        b.ko_point = self.ko_point
        return b

    def stones_list(self, last_point=KO_NONE):
        """棋子列表；last_point 上的那颗带 last=true，遍历顺序 y 外 x 内。"""
        out = []
        for y in range(SIZE):
            for x in range(SIZE):
                c = self.grid[x][y]
                if c != EMPTY:
                    d = {"x": x, "y": y, "c": c}
                    if (x, y) == last_point:
                        d["last"] = True
                    out.append(d)
        return out

    def count(self):
        """统计双方棋子数。"""
        b = 0
        w = 0
        for x in range(SIZE):
            for y in range(SIZE):
                if self.grid[x][y] == "black":
                    b += 1
                elif self.grid[x][y] == "white":
                    w += 1
        return {"black": b, "white": w}
