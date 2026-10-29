"""发言侧工具：她自己决定要不要复算、查定式、核对盘面。

工具只影响**她怎么说**，不影响**她下哪里**——落点仍由 ``agent._pick_ai_move`` 决定。

硬规则（与 ``agent._tool_loop`` 配合）：
- 工具**绝不抛异常**：引擎异常 / 服务不可用 / 查不到都转成一句可读中文；
- 失败文本统一以 ``FAIL_PREFIX`` 开头：工具自己吞掉了异常，tool loop 只有这个信号能判 ``ok=False``；
- 工具输出**永不直接当台词**：它只回到模型手里，玩家看到的是模型据此重写的自然语言；
- ``engine`` 这类进程内对象由 ``agent._tool_args`` 注入（事件里只带模型自己给的那份参数）。

三个工具的名称即模型可见名（``@tool`` 用函数名），描述即「什么时候用它」。
"""

from typing import Annotated, Any

from langchain_core.tools import tool

from . import prompts
from .joseki import JosekiData

FAIL_PREFIX = "工具执行失败："

# 复算给模型看的候选点数量：Top5 足够，别拿长列表挤占上下文
TOP_MOVES = 5


def failed(text) -> bool:
    """这份工具输出是不是失败文本（tool loop 据此把 ``ok`` 报成 ``False``）。"""
    return str(text or "").startswith(FAIL_PREFIX)


@tool
async def katago_analyze(
    engine: Annotated[Any, "引擎客户端，由系统注入，模型不用给"] = None,
    moves: Annotated[list, "棋谱 [[\"B\",\"Q16\"],…]；留空＝按当前局面算"] = None,
) -> str:
    """用 KataGo 重新算一遍局面：返回黑方胜率、目差与引擎推荐的候选点。

    什么时候用：想确认形势、怀疑自己或对方的判断、或说方位/厚薄前想再核对一遍时。
    """
    ms = list(moves or [])
    try:
        analysis = await engine.analyze(ms)
    except Exception as e:                      # noqa: BLE001
        return FAIL_PREFIX + "KataGo 调用异常（%s），这次先不参考引擎数据。" % e
    if not isinstance(analysis, dict) or "error" in analysis:
        why = analysis.get("error", "") if isinstance(analysis, dict) else analysis
        return FAIL_PREFIX + "KataGo 服务不可用（%s），这次先不参考引擎数据。" % why
    conv = prompts.to_black_perspective(analysis, len(ms))
    tops = []
    for m in (analysis.get("moveInfos") or [])[:TOP_MOVES]:
        mv = str((m or {}).get("move", ""))
        if mv != "":
            tops.append(mv)
    return "KataGo 复算：黑方（玩家）胜率 %.1f%%，目差 %+.1f 目（正数＝黑领先）；候选点：%s。" % (
        float(conv[0]) * 100.0, float(conv[1]), "、".join(tops) if tops else "（没有候选点）")


@tool
def joseki_lookup(name: Annotated[str, "定式名，例如「点三三·基本型」；留空＝列出库里有哪些"] = "") -> str:
    """查入门定式库：按名称返回这条定式的要点（布局方向、之后的次序）。

    什么时候用：想讲某个角上的形状、或拿不准某条定式的后续变化时。
    """
    key = str(name or "").strip()
    names = "、".join(sorted({str(e["name"]) for e in JosekiData.LIST}))
    if key == "":
        return "请给一个定式名。库里现有：%s。" % names
    for e in JosekiData.LIST:                   # 先精确匹配
        if str(e["name"]) == key:
            return "【%s】要点：%s" % (e["name"], e["key"])
    for e in JosekiData.LIST:                   # 再宽松匹配（模型常把名字记个大概）
        if key in str(e["name"]) or str(e["name"]) in key:
            return "【%s】要点：%s" % (e["name"], e["key"])
    return "库里没有这个定式（%s）。现有：%s。" % (key, names)


@tool
def board_query(moves: Annotated[list, "棋谱 [[\"B\",\"Q16\"],…]；留空＝按当前局面画"] = None) -> str:
    """把局面画成紧凑盘面图：每行每列的棋子、双方现有棋子坐标清单、现在轮到谁。

    什么时候用：想核对某个点有没有子、某片区域是谁的、开口说方位之前想确认坐标时。
    """
    ms = list(moves or [])
    return prompts.board_map(prompts.board_from_moves(ms), len(ms))


TOOLS = [katago_analyze, joseki_lookup, board_query]
