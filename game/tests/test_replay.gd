extends SceneTree
## 棋谱本地回放门卫（v3.11.5）：落子 / 提子 / 虚着 / 脏值 / 最后一手标记。
## 纯逻辑、不渲染。

var _total := 0
var _fail := 0

func _initialize() -> void:
	_run()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _find(stones: Array, x: int, y: int) -> Dictionary:
	for s in stones:
		if typeof(s) == TYPE_DICTIONARY and int(s["x"]) == x and int(s["y"]) == y:
			return s
	return {}

func _run() -> void:
	print("[replay] 棋谱本地回放自检")
	var moves := [["black", "Q16"], ["white", "D4"], ["black", "D16"], ["white", "Q4"]]
	_check("① 0 手：空盘", GameReplay.stones_at(moves, 0).is_empty())
	var two := GameReplay.stones_at(moves, 2)
	_check("② 前 2 手：两颗子、方向与 GTP 一致", two.size() == 2
		and not _find(two, 15, 3).is_empty() and not _find(two, 3, 15).is_empty())
	_check("② 最后一手带 last 标记（只在 D4）",
		bool(_find(two, 3, 15).get("last", false)) and not bool(_find(two, 15, 3).get("last", false)))
	_check("② 超出总手数自动夹取", GameReplay.stones_at(moves, 99).size() == 4)

	# 角上提子：B19、A19、A18 —— 第三手落定后白子无气被提
	var cap := [["black", "B19"], ["white", "A19"], ["black", "A18"]]
	var before := GameReplay.stones_at(cap, 2)
	_check("③ 提子前：角上白子还在", not _find(before, 0, 0).is_empty())
	var after := GameReplay.stones_at(cap, 3)
	_check("③ 提子后：白子消失、两颗黑子还在", _find(after, 0, 0).is_empty()
		and not _find(after, 1, 0).is_empty() and not _find(after, 0, 1).is_empty())
	_check("③ 提子后 last 标在被提后的最后一手", bool(_find(after, 0, 1).get("last", false)))

	# 虚着 / 脏值 / 字典形状
	var pass_moves := [["white", "D4"], ["black", "pass"], ["white", ""]]
	var passed := GameReplay.stones_at(pass_moves, 3)
	_check("④ 虚着不落子，盘面保持 1 颗", passed.size() == 1 and not _find(passed, 3, 15).is_empty())
	var dirty := [["x", "D4"], ["black", "ZZ"], {"color": "white", "gtp": "Q16"}]
	var dirty_stones := GameReplay.stones_at(dirty, 3)
	_check("⑤ 脏值跳过、字典形状认得",
		dirty_stones.size() == 1 and not _find(dirty_stones, 15, 3).is_empty())
	_check("⑥ 非数组入参安全返回空", GameReplay.stones_at("bad", 3).is_empty())

	print("[replay] %s：%d 项断言%s" % ["PASS" if _fail == 0 else "FAIL", _total,
		"全部通过" if _fail == 0 else "，%d 项失败" % _fail])
	quit(0 if _fail == 0 else 1)
