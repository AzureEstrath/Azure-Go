class_name SfxPlayer
extends Node
## 对局音效（v3.11.5）：落子 / 提子 / 虚着 / 终局。
##
## 纯前端、零后端调用：音频资源全部预加载，4 个 AudioStreamPlayer 轮转，
## 落子与提子可以自然叠在一起（不会互相 cut）。所有音量只受 `volume` 控制。

const SFX_DIR := "res://assets/sfx"
const SOUND_FILES := {
	"stone": ["stone_1.wav", "stone_2.wav", "stone_3.wav"],
	"capture": ["capture.wav"],
	"pass": ["pass.wav"],
	"game_end": ["game_end.wav"],
}
## 各类音效的相对增益（0~1）：落子最响，虚着最轻；终局留一点余量避免压过台词
const GAIN := {"stone": 0.85, "capture": 0.75, "pass": 0.55, "game_end": 0.65}
const PLAYER_COUNT := 4

var enabled := true
var volume := 0.8

var _streams: Dictionary = {}          # name -> Array[AudioStream]
var _players: Array[AudioStreamPlayer] = []
var _next := 0
var _stone_last := -1

func _ready() -> void:
	for name in SOUND_FILES.keys():
		var list: Array = []
		for f in SOUND_FILES[name]:
			var st := load(SFX_DIR.path_join(str(f)))
			if st is AudioStream:
				list.append(st)
			else:
				push_warning("[Sfx] 音效缺失：%s" % f)
		if not list.is_empty():
			_streams[name] = list
	for i in PLAYER_COUNT:
		var p := AudioStreamPlayer.new()
		add_child(p)
		_players.append(p)

## 音效相对增益（纯函数；未知名字按 0.7）
static func gain_of(name: String) -> float:
	return float(GAIN.get(name, 0.7))

## 音量 → dB（纯函数；0 时压到极小值，避免 linear_to_db(0) = -inf）
static func db_for(vol: float, gain: float) -> float:
	return linear_to_db(clampf(vol * gain, 0.001, 1.0))

## 播放一个音效；不存在 / 关闭 / 没有播放器时静默返回。
func play(name: String) -> void:
	if not enabled or _players.is_empty() or not _streams.has(name):
		return
	var arr: Array = _streams[name]
	var idx := 0
	if arr.size() > 1:
		idx = randi() % arr.size()
		if name == "stone" and idx == _stone_last:
			idx = (idx + 1) % arr.size()      # 落子三连不重复同一音色
		if name == "stone":
			_stone_last = idx
	var p: AudioStreamPlayer = _players[_next]
	_next = (_next + 1) % _players.size()
	p.stop()
	p.stream = arr[idx]
	p.volume_db = db_for(volume, gain_of(name))
	p.play()

## 门卫用：某个音效名有几个可用资源；0 = 资源缺失
func sound_count(name: String) -> int:
	var arr = _streams.get(name, [])
	return arr.size() if typeof(arr) == TYPE_ARRAY else 0

func stop_all() -> void:
	for p in _players:
		p.stop()
