"""预测线 A/B/C 提取（纯函数，零额外引擎调用）。

输入是**既有的一次**分析结果（``moveInfos``），只读每条候选的 ``move`` / ``pv`` / ``winrate`` /
``scoreLead`` 四个字段——不新开任何分析、不持有任何客户端、不做 IO。复杂度 O(候选数)。

- 线 A ＝ ``move_infos[0]`` 的首手 + 其 ``pv`` 的前 ``pv_len-1`` 手；
- 线 B / C ＝ 在**前 ``candidates`` 个候选**里**依次**取「与**已选的每一条线**不同手数 ≥
  ``diverge_min``」且「与 A 的胜率差 ≤ ``max_drop``」的候选；凑不齐就**有多少用多少**
  （只有 A/B，或只剩 A），**绝不抛**。
- ``pv`` 长度由引擎「该候选被访问的次数」决定，不保证够 ``pv_len``：不足时**用可用长度**，
  只做做得到的线（``pv`` 缺失 ⇒ 该线只剩首手）；输入为空 / 认不出首手 ⇒ ``a`` 也是 ``None``。

两个口径（写在这里，供下游与门卫对齐）：

1. **引擎惯例：``pv`` 的首元素就是该候选本身**（``pv[0] == move``）。本模块先剥掉这个重复首手
   再取前 ``pv_len-1`` 手——两种约定（含 / 不含首手）下结果一致，也不会把同一个点在同一行里
   写两遍。
2. **「不同手数」只逐手比到较短那条的长度为止**（不把长度差算成差异）：否则一条只有 1 手的
   候选会靠「长度差」凑够 ``diverge_min`` 混进 B——那正好是「凑不齐就该缺线」的场合。

返回形状（superset，见设计 §6.1）：``{"a": {moves, winrate, score}, "b": {…, diff_from_a} | None,
"c": {…, diff_from_a, diff_from_b} | None, "drop": float}``；``drop`` ＝ A 与 B 的胜率差
（取绝对值；``b`` 为 ``None`` 时 ``0.0``）。
"""


def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _line(info, pv_len):
    """把一条候选渲染成 GTP 序列 ``[首手, …]``；认不出首手时返回 ``None``。"""
    move = str(info.get("move") or "")
    if move == "":
        return None
    rest = []
    pv = info.get("pv")
    if isinstance(pv, (list, tuple)):
        rest = [str(p) for p in pv if isinstance(p, str) and p != ""]
    if rest and rest[0] == move:            # 引擎惯例：pv 首元素＝该候选本身
        rest = rest[1:]
    return [move] + rest[:max(0, pv_len - 1)]


def _diff(x, y):
    """两条线「不同手数」：逐手比较（GTP 坐标），只比到较短那条的长度为止。"""
    return sum(1 for i in range(min(len(x), len(y))) if x[i] != y[i])


def extract_lines(move_infos, *, pv_len=5, diverge_min=2, max_drop=0.05, candidates=10) -> dict:
    """从**既有的一次**分析结果里抠出预测线 A / B / C（纯读，零引擎调用）。"""
    infos = [m for m in (move_infos or []) if isinstance(m, dict)]
    out = {"a": None, "b": None, "c": None, "drop": 0.0}
    if not infos:
        return out
    pv_len = _as_int(pv_len, 5)
    diverge_min = _as_int(diverge_min, 2)
    max_drop = _as_float(max_drop, 0.05)
    pool = _as_int(candidates, 10)

    a_line = _line(infos[0], pv_len)
    if a_line is None:                      # 连首手都认不出：三条线都做不了（不抛）
        return out
    a_wr = _as_float(infos[0].get("winrate"), 0.5)
    out["a"] = {"moves": a_line, "winrate": a_wr,
                "score": _as_float(infos[0].get("scoreLead"), 0.0)}

    picked = [a_line]                       # 已选线（A 起）：新候选要与**每一条**都够不同
    rest = []                               # 依次凑出来的 B、C
    for i, info in enumerate(infos[:max(0, pool)]):
        if i == 0 or len(rest) >= 2:
            continue
        line = _line(info, pv_len)
        if line is None or line in picked:
            continue
        if any(_diff(line, p) < diverge_min for p in picked):
            continue
        wr = _as_float(info.get("winrate"), 0.5)
        if abs(wr - a_wr) > max_drop:       # 与 A 的胜率差超限：宁缺毋滥（可降级回 A）
            continue
        picked.append(line)
        rest.append({"moves": line, "winrate": wr,
                     "score": _as_float(info.get("scoreLead"), 0.0)})

    if rest:
        b = rest[0]
        b["diff_from_a"] = _diff(b["moves"], a_line)
        out["b"] = b
        out["drop"] = abs(a_wr - b["winrate"])
    if len(rest) >= 2:
        c = rest[1]
        c["diff_from_a"] = _diff(c["moves"], a_line)
        c["diff_from_b"] = _diff(c["moves"], rest[0]["moves"])
        out["c"] = c
    return out
