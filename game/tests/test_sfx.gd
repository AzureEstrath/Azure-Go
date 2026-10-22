extends SceneTree
## 对局音效门卫（v3.11.5）：资源是否齐全、音量换算是否安全、播放是否不抛。
## 纯逻辑为主；音频在 headless 下用 dummy 驱动，不会真的发声。

var _total := 0
var _fail := 0

func _initialize() -> void:
	_go.call_deferred()

func _check(name: String, ok: bool) -> void:
	_total += 1
	if ok:
		print("  PASS  ", name)
	else:
		_fail += 1
		print("  FAIL  ", name)

func _go() -> void:
	var s := SfxPlayer.new()
	root.add_child(s)
	await process_frame
	_check("① 落子三音色齐全", s.sound_count("stone") == 3)
	_check("① 提子 / 虚着 / 终局各一条", s.sound_count("capture") == 1
			and s.sound_count("pass") == 1 and s.sound_count("game_end") == 1)
	_check("② 相对增益：落子 > 提子 > 虚着", SfxPlayer.gain_of("stone") > SfxPlayer.gain_of("capture")
			and SfxPlayer.gain_of("capture") > SfxPlayer.gain_of("pass"))
	_check("② 50% 音量 ≈ -6 dB", absf(SfxPlayer.db_for(0.5, 1.0) - (-6.0206)) < 0.01)
	_check("② 0 音量不产生 -inf（压到 -60 dB 以内）", is_finite(SfxPlayer.db_for(0.0, 1.0))
			and SfxPlayer.db_for(0.0, 1.0) > -100.0)
	_check("② 超范围音量被夹到 0..1", absf(SfxPlayer.db_for(9.0, 1.0) - SfxPlayer.db_for(1.0, 1.0)) < 0.001)
	s.volume = 0.5
	s.play("stone")
	s.play("capture")
	s.play("pass")
	s.play("game_end")
	s.play("missing")                 # 未知名字：静默，不抛
	_check("③ 四种音效播放不报错（含未知名字）", true)
	s.enabled = false
	s.play("stone")
	_check("③ 关闭后仍安全（静默返回）", true)
	s.stop_all()
	print("[sfx] %s：%d 项断言%s" % ["PASS" if _fail == 0 else "FAIL", _total,
			"全部通过" if _fail == 0 else "，%d 项失败" % _fail])
	quit(0 if _fail == 0 else 1)
