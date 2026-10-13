"""棋盘坐标换算：与 KataGo/GTP 一致，列 A..T 跳过 I，行 19..1。

逐方法移植自 AzureGoGodot/scripts/core/Coords.gd。
"""

SIZE = 19


def letter(i):
    """列下标 -> 字母；跳过 I（index >= 8 时后移一位）。"""
    return chr(65 + i + (1 if i >= 8 else 0))


def _to_int(s):
    """复刻 GDScript String.to_int：解析开头整数，非法返回 0。"""
    i = 0
    n = len(s)
    while i < n and s[i] in " \t\n\r":
        i += 1
    sign = 1
    if i < n and (s[i] == "-" or s[i] == "+"):
        if s[i] == "-":
            sign = -1
        i += 1
    num = 0
    started = False
    while i < n and "0" <= s[i] <= "9":
        num = num * 10 + (ord(s[i]) - 48)
        i += 1
        started = True
    return sign * num if started else 0


def gtp_to_xy(pos):
    """GTP 坐标 -> 内部 (x, y)；pass 或非法返回 (-1, -1)。"""
    p = pos.strip()
    if p == "" or p.lower() == "pass":
        return (-1, -1)
    col = ord(p[0].upper()) - 65
    if col >= 8:
        col -= 1
    row = _to_int(p[1:])
    if col < 0 or col >= SIZE or row < 1 or row > SIZE:
        return (-1, -1)
    return (col, SIZE - row)


def xy_to_gtp(x, y):
    return letter(x) + str(SIZE - y)


def valid(x, y):
    """坐标是否在棋盘范围内。"""
    return 0 <= x < SIZE and 0 <= y < SIZE
