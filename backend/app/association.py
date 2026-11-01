"""关联规则挖掘：FP-Growth + 少量日常词抽取（v4.7.0）。

用途（只作**软偏好**，绝不写死结论）：
- 日程：从历史里挖「哪些类别常在同一天一起出现」，给自动排班一个很小的加分；
- 记忆：从聊天流水里挖「哪些词常一起出现」（苹果 / 米饭 / 辣椒），
  作为「常见搭配」软提示，帮助联想但**不替用户下结论**。

设计边界：
- 纯标准库、纯函数；不读库、不发事件、不调 LLM。
- FP-Growth 只在调用方给的小批量事务上跑（日程 ≤300 行、聊天 ≤200 轮），
  数据量再大时也只截取最近一段，不让一次挖掘拖慢回合。
- 关联必须满足 **支持度 / 置信度 / 提升度** 三重门槛；单次出现不进入结果，
  所以不会因为一顿饭就把「苹果 + 辣椒」刻成固定印象。
- 输出只给「常一起出现」的提示；是否成立、今天是不是这样，由当前对话决定。
"""

import re

# 常见日常元素：中文没有空格，整串关键词 + 少量 ASCII 词足够覆盖「午餐吃了什么」这类记忆。
# 这不是分类器，只是把自由文本收成可共现的 token；不在词表里的内容不会进入关联。
LEXICON = (
    "苹果", "米饭", "辣椒", "牛奶", "鸡蛋", "面包", "面条", "咖啡", "酸奶", "果汁",
    "香蕉", "橙子", "葡萄", "西瓜", "草莓", "芒果", "牛肉", "鸡肉", "猪肉", "鱼",
    "虾", "豆腐", "土豆", "番茄", "黄瓜", "生菜", "汤", "粥", "饺子", "包子",
    "汉堡", "披萨", "火锅", "烧烤", "蛋糕", "可乐", "雪碧", "早餐", "午餐", "晚餐",
    "夜宵", "学习", "复习", "考试", "英语", "数学", "编程", "代码", "游戏", "电影",
    "音乐", "画画", "跑步", "健身", "散步", "睡眠", "熬夜", "工作", "上课", "作业",
    "项目", "会议", "看书", "写作", "咖啡店", "图书馆",
)

_TOKEN_ASCII_RE = re.compile(r"[a-z][a-z0-9_]{2,}", re.I)


def tokens_from_text(text, limit=20):
    """把一句话收成可共现的 token：词表命中 + ASCII 词；不足两个返回空列表。

    token 都做小写 / 去空白，重复去重；只保留词表里的中文元素，避免把整句的
    二元组噪声灌进关联库。
    """
    s = re.sub(r"[\s\u3000]+", " ", str(text or "")).strip().lower()
    if s == "":
        return []
    out = []
    for term in sorted(LEXICON, key=len, reverse=True):
        if term in s and term not in out:
            out.append(term)
    for m in _TOKEN_ASCII_RE.finditer(s):
        w = m.group(0).lower()
        if w not in out:
            out.append(w)
    out = out[:max(1, int(limit or 20))]
    return out if len(out) >= 2 else []


class _Node:
    """FP-tree 节点：item / 计数 / 父节点 / 子节点。"""

    __slots__ = ("item", "count", "parent", "children")

    def __init__(self, item, parent=None, count=0):
        self.item = item
        self.count = int(count)
        self.parent = parent
        self.children = {}


def _freq_items(transactions):
    counts = {}
    for t in transactions:
        for item in t:
            counts[item] = counts.get(item, 0) + 1
    return counts


def _build_tree(transactions, freq):
    root = _Node(None)
    header = {item: [] for item in freq}
    for t in transactions:
        items = sorted((x for x in t if x in freq), key=lambda x: (-freq[x], x))
        node = root
        for item in items:
            child = node.children.get(item)
            if child is None:
                child = _Node(item, node, 0)
                node.children[item] = child
                header[item].append(child)
            child.count += 1
            node = child
    return root, header


def _mine_tree(freq, header, min_support, prefix, out):
    """后序递归：先从小支持度项开始生成长度递增的频繁项集。"""
    for item in sorted(freq, key=lambda x: (freq[x], x)):
        support = int(freq[item])
        if support < min_support:
            continue
        new_set = tuple(sorted(tuple(prefix) + (item,)))
        out.append((new_set, support))
        # 条件模式基：从每个 node 往上走，收集父路径。
        paths = []
        for node in header.get(item, []):
            path = []
            parent = node.parent
            while parent is not None and parent.item is not None:
                path.append(parent.item)
                parent = parent.parent
            if path:
                paths.append((tuple(path), int(node.count)))
        if not paths:
            continue
        cond_counts = {}
        for path, count in paths:
            for x in set(path):
                cond_counts[x] = cond_counts.get(x, 0) + int(count)
        cond_freq = {x: c for x, c in cond_counts.items() if c >= min_support}
        if not cond_freq:
            continue
        root = _Node(None)
        cond_header = {x: [] for x in cond_freq}
        for path, count in paths:
            items = [x for x in path if x in cond_freq]
            node = root
            for x in sorted(items, key=lambda y: (-cond_freq[y], y)):
                child = node.children.get(x)
                if child is None:
                    child = _Node(x, node, 0)
                    node.children[x] = child
                    cond_header[x].append(child)
                child.count += int(count)
                node = child
        _mine_tree(cond_freq, cond_header, min_support, new_set, out)


def mine(transactions, min_support=3, max_len=3, max_items=20):
    """返回频繁项集列表：``{items, support}``；数据不足 / 脏输入返回 ``[]``。

    事务是「一次对话 / 同一天」的集合；空事务、单元素事务会被忽略（没有共现）。
    """
    try:
        ms = max(1, int(min_support))
        ml = max(1, int(max_len))
        mi = max(2, int(max_items))
    except (TypeError, ValueError):
        ms, ml, mi = 3, 3, 20
    rows = []
    for t in transactions or []:
        try:
            items = {str(x) for x in t if str(x).strip()}
        except TypeError:
            continue
        if len(items) >= 2:
            rows.append(items)
    if len(rows) < ms:
        return []
    freq = _freq_items(rows)
    freq = {k: v for k, v in freq.items() if v >= ms}
    if not freq:
        return []
    # 限制参与挖掘的 token 数，避免一个超长聊天把树撑大。
    if len(freq) > mi:
        keep = sorted(freq, key=lambda x: (-freq[x], x))[:mi]
        freq = {k: freq[k] for k in keep}
        rows = [{x for x in t if x in freq} for t in rows]
    root, header = _build_tree(rows, freq)
    out = []
    _mine_tree(freq, header, ms, (), out)
    seen = set()
    result = []
    for items, support in out:
        if len(items) < 2 or len(items) > ml:
            continue
        if items in seen:
            continue
        seen.add(items)
        result.append({"items": list(items), "support": int(support)})
    result.sort(key=lambda x: (-x["support"], -len(x["items"]), x["items"]))
    return result


def _support_count(transactions, subset):
    return sum(1 for t in transactions if set(subset) <= set(t))


def patterns(transactions, min_support=3, min_confidence=0.65,
             min_lift=1.05, max_len=3, limit=12):
    """频繁项集 → 带置信度 / 提升度的关联；单次出现 / 低于门槛不进入结果。"""
    rows = []
    for t in transactions or []:
        try:
            items = {str(x) for x in t if str(x).strip()}
        except TypeError:
            continue
        if len(items) >= 2:
            rows.append(items)
    if not rows:
        return []
    base = mine(rows, min_support=min_support, max_len=max_len)
    if not base:
        return []
    n = float(len(rows))
    singles = {}
    for t in rows:
        for x in t:
            singles[x] = singles.get(x, 0) + 1
    out = []
    for row in base:
        items = tuple(row["items"])
        support = int(row["support"])
        if support < min_support:
            continue
        # 取「去掉一个元素作前件」里置信度最高的一条；这是关联规则最常用的读法。
        best_conf = 0.0
        consequent = ""
        for x in items:
            ante = tuple(y for y in items if y != x)
            s_ante = singles.get(x, 0) if not ante else _support_count(rows, ante)
            if s_ante <= 0:
                continue
            conf = support / float(s_ante)
            if conf > best_conf:
                best_conf = conf
                consequent = x
        if best_conf < min_confidence or consequent == "":
            continue
        c_support = singles.get(consequent, 0)
        lift = best_conf / (c_support / n) if c_support > 0 else 0.0
        if lift < min_lift:
            continue
        out.append({"items": list(items), "support": support,
                    "confidence": round(best_conf, 4),
                    "lift": round(lift, 4), "consequent": consequent})
    out.sort(key=lambda x: (-x["support"], -x["confidence"], x["items"]))
    return out[:max(1, int(limit or 12))]


def category_bonus(itemsets, gain=0.08):
    """关联 → 每个类别的很小加分（用于自动排班同优先级排序）。"""
    out = {}
    try:
        g = max(0.0, min(1.5, float(gain)))
    except (TypeError, ValueError):
        g = 0.08
    for row in itemsets or []:
        if not isinstance(row, dict):
            continue
        items = [str(x) for x in (row.get("items") or []) if str(x).strip()]
        try:
            conf = max(0.0, min(1.0, float(row.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        for item in items:
            out[item] = max(out.get(item, 0.0), g * conf)
    return out
