# -*- coding: utf-8 -*-
"""Azure 陪练的本地 TTS 服务：CosyVoice 3 / 2 的薄封装。

- 全程本地推理（CPU 或 GPU），返回标准 16bit PCM WAV，供 Godot 端直接播放
- 开机时按 voices.json 注册「命名音色」（zero-shot 参考音频），可用 instruct 控制语气
- 同时兼容两种接口：
    POST /inference_sft      （表单 tts_text, spk_id）      —— 与 CosyVoice 官方 fastapi 一致
    POST /v1/audio/speech    （JSON input, voice, speed）   —— OpenAI 兼容
    GET  /health                                        —— 查看模型/音色状态

用法：
    python azure_tts_server.py --port 9880 --model_dir pretrained_models/Fun-CosyVoice3-0.5B
"""
import argparse
import io
import json
import os
import sys
import tempfile
import threading
import time
import wave

# 让 torch/OpenMP 空闲时不要自旋抢 CPU（否则会一直吃满核心）
os.environ.setdefault('OMP_WAIT_POLICY', 'PASSIVE')
os.environ.setdefault('KMP_BLOCKTIME', '0')
os.environ.setdefault('MKL_NUM_THREADS', os.environ.get('TTS_THREADS', '8'))
# AMD 核显(ROCm/HIP)：MIOpen 需用默认的完整搜索才能给 gfx1151 选出可用卷积内核
# （FAST 启发式会挑到不可用实现，报 miopenStatusInternalError）。搜索结果缓存在 ~/.miopen，
# 且服务端启动时有预热，所以只在首次遇到新形状时慢。
os.environ.setdefault('MIOPEN_FIND_MODE', 'NORMAL')
# 逃生开关：设 AZURE_TTS_CPU=1 强制走 CPU（GPU 万一出问题时一行环境变量即可回退）
if os.environ.get('AZURE_TTS_CPU') == '1':
    os.environ['CUDA_VISIBLE_DEVICES'] = ''

import numpy as np
import torch
import uvicorn

# ROCm(Windows) 轮子不带 torch.distributed(c10d)，而 transformers 4.51 会无条件 import torch.distributed.tensor：
# 用空模块顶替（我们只跑前向推理，不用分布式）
import types

try:
    import torch.distributed.tensor                  # noqa: F401
except Exception:                                    # noqa: BLE001
    for _n in ('torch.distributed.tensor', 'torch.distributed.tensor.parallel'):
        sys.modules.setdefault(_n, types.ModuleType(_n))

# torch>=2.6 起 torch.load 默认 weights_only=True，读不了 CosyVoice/OpenVoice 的历史检查点：统一放行
_orig_torch_load = torch.load


def _torch_load_compat(*args, **kwargs):
    kwargs.setdefault('weights_only', False)
    return _orig_torch_load(*args, **kwargs)


torch.load = _torch_load_compat
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(ROOT)
sys.path.append(os.path.join(ROOT, 'third_party', 'Matcha-TTS'))

# 日志始终落盘到 server_run.log：VBS 以隐藏窗口启动时看不到控制台，出问题只能靠它排查。
# 用 Tee 同时保留原 stdout/stderr（pythonw 下它们为空，则由文件兜底）。
class _Tee:
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None and hasattr(s, 'write')]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:                            # noqa: BLE001
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:                            # noqa: BLE001
                pass

    def isatty(self):
        return False


try:
    _lf = open(os.path.join(ROOT, 'server_run.log'), 'a', encoding='utf-8', buffering=1)
    sys.stdout = _Tee(sys.stdout, _lf)
    sys.stderr = _Tee(sys.stderr, _lf)
    print('[tts] ===== 启动 %s  pid=%d =====' % (time.strftime('%Y-%m-%d %H:%M:%S'), os.getpid()))
except Exception:                                        # noqa: BLE001
    pass

# CosyVoice 相关导入放在真正需要时再做（轻量模式下不加载 torch，启动更省资源）
AutoModel = None
load_wav = None

app = FastAPI()
model = None                    # 主模型：Fun-CosyVoice3-0.5B（克隆 / instruct）
model_sft = None                # 预设音色模型：CosyVoice-300M-SFT（中文女 等）
sample_rate = 24000
model_dir = ''
model_dir_sft = ''
voices = {}                     # name -> {"prompt_wav":..., "prompt_text":..., "instruct":...}
zero_shot_registered = set()    # 已注册到主模型的克隆音色名（推理直接用缓存特征，快很多）
# 快速克隆（melo 合成 + OpenVoice 音色转换）：惰性加载
OV_DIR = os.path.join(ROOT, 'third_party', 'OpenVoice')
ov_converter = None
ov_src_se = None
ov_tgt_se = {}                  # 基础音色名 -> 目标 SE
ov_tau = 0.3                    # OpenVoice 音色转换噪声系数（越小越干净；--ov_tau 可调）
_lock = threading.Lock()        # 串行推理，避免多请求互抢 CPU
sherpa = None                   # 轻量 VITS（sherpa-onnx）：CPU 上快得多
sherpa_sr = 44100
sherpa_extra = {}               # 额外轻量音色：id -> {'engine':OfflineTts,'sid':int,'label':str}
_cache = {}                     # (text, voice, speed) -> (wav bytes, 时间戳)
CACHE_TTL = 3600.0              # 相同文本的朗读结果缓存 1 小时：口头禅/问候语等重复句子直接秒回


def read_voices() -> dict:
    path = os.path.join(ROOT, 'voices.json')
    if not os.path.exists(path):
        default = {
            "azure": {
                "prompt_wav": "asset/zero_shot_prompt.wav",
                "prompt_text": "希望你以后能够做的比我还好呦。",
                "instruct": "用慵懒温柔、带一点笑意的少女语气说话<|endofprompt|>",
            }
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print('[tts] 已生成默认 voices.json（音色 azure）')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def to_wav(audio: torch.Tensor, sr: int) -> bytes:
    pcm = (audio.squeeze(0).clamp(-1.0, 1.0).detach().cpu().numpy() * 32767.0).astype('<i2').tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def synth_sherpa(text: str, speed: float = 1.0, engine=None, sid: int = 0) -> bytes:
    """轻量 VITS（sherpa-onnx）合成：CPU 上 RTF 通常 < 0.3"""
    eng = engine if engine is not None else sherpa
    audio = eng.generate(text, sid=sid, speed=speed)
    samples = np.array(audio.samples, dtype=np.float32)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def load_sherpa_engine(sd: str, threads: int, provider: str):
    """按目录加载一个 sherpa-onnx VITS 引擎；目录里没有可用 onnx 时返回 None。"""
    import glob as _glob                                  # noqa: PLC0415
    cands = [p for p in _glob.glob(os.path.join(sd, '*.onnx'))
             if 'int8' not in os.path.basename(p)]
    if not cands:
        return None
    import sherpa_onnx                                    # noqa: PLC0415
    fsts = [os.path.join(sd, f) for f in ('date.fst', 'number.fst', 'phone.fst', 'new_heteronym.fst')]
    fsts = [f for f in fsts if os.path.exists(f)]
    lex = os.path.join(sd, 'lexicon.txt')
    dic = os.path.join(sd, 'dict')
    es = os.path.join(sd, 'espeak-ng-data')
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=sorted(cands)[0],
                lexicon=lex if os.path.exists(lex) else '',
                tokens=os.path.join(sd, 'tokens.txt'),
                dict_dir=dic if os.path.isdir(dic) else '',
                data_dir=es if os.path.isdir(es) else ''),
            num_threads=threads, provider=provider),
        rule_fsts=','.join(fsts),
        max_num_sentences=1))


# ---------- 快速克隆：melo 合成 + OpenVoice 音色转换（CPU 上比 CosyVoice 快一个数量级） ----------

def _ensure_openvoice():
    """惰性加载 OpenVoice 音色转换器；源音色 SE 优先从真实 melo 合成里提取"""
    global ov_converter, ov_src_se
    if ov_converter is not None:
        return
    if not os.path.isdir(OV_DIR):
        raise RuntimeError('未找到 OpenVoice（%s）' % OV_DIR)
    sys.path.append(OV_DIR)
    from openvoice.api import OpenVoiceBaseClass, ToneColorConverter      # noqa: PLC0415

    class _FastConverter(ToneColorConverter):
        """绕过上游构造器 kwargs 透传的 bug，同时不加载水印模型"""

        def __init__(self, config_path, device='cpu'):
            OpenVoiceBaseClass.__init__(self, config_path, device=device)
            self.watermark_model = None
            self.version = getattr(self.hps, '_version_', 'v1')

    ckpt = os.path.join(OV_DIR, 'checkpoints_v2')
    t0 = time.time()
    conv = _FastConverter(os.path.join(ckpt, 'converter', 'config.json'), device='cpu')
    conv.load_ckpt(os.path.join(ckpt, 'converter', 'checkpoint.pth'))
    try:
        probe = os.path.join(tempfile.gettempdir(), 'azure_tts_ov_probe.wav')
        with open(probe, 'wb') as f:
            f.write(synth_sherpa('大家好，我是Azure。', 1.0))
        src_se = conv.extract_se([probe])
    except Exception as exc:                          # noqa: BLE001
        print('[tts] melo 源音色提取失败（改用官方 zh 基座 SE）：%s' % exc)
        src_se = torch.load(os.path.join(ckpt, 'base_speakers', 'ses', 'zh.pth'))
    ov_converter = conv
    ov_src_se = src_se
    print('[tts] OpenVoice 音色转换器就绪，用时 %.1fs' % (time.time() - t0))


def synth_fast_clone(text: str, base: str, speed: float = 1.0) -> bytes:
    """melo 合成 → 音色转换为 base（如 azure）→ WAV 字节"""
    if sherpa is None:
        raise RuntimeError('快速克隆音色需要 melo 轻量引擎（请以 --cosyvoice 1 启动）')
    prof = voices.get(base)
    if not prof:
        raise RuntimeError('快速克隆音色缺少基础音色 %s（见 voices.json）' % base)
    with _lock:
        _ensure_openvoice()
        t0 = time.time()
        raw = synth_sherpa(text, speed)
        t1 = time.time()
        if base not in ov_tgt_se:
            # 目标音色优先用「长参考」（多位语料拼出的干净参考，音色更稳），没有才退回单段 prompt
            ref = prof.get('prompt_long_wav') or prof['prompt_wav']
            ov_tgt_se[base] = ov_converter.extract_se([os.path.join(ROOT, ref)])
            print('[tts] 目标音色 SE 参考：%s' % ref)
        src = os.path.join(tempfile.gettempdir(), 'azure_tts_ov_src.wav')
        dst = os.path.join(tempfile.gettempdir(), 'azure_tts_ov_dst.wav')
        with open(src, 'wb') as f:
            f.write(raw)
        ov_converter.convert(src, ov_src_se, ov_tgt_se[base], output_path=dst, tau=ov_tau)
        with open(dst, 'rb') as f:
            out = f.read()
        t2 = time.time()
    with wave.open(io.BytesIO(out)) as w:
        dur = w.getnframes() / float(w.getframerate())
    print('[tts] %-10s 音频 %.1fs / melo %.2fs + 转换 %.2fs (RTF %.2f)'
          % (base + '-fast', dur, t1 - t0, t2 - t1, (t2 - t0) / max(dur, 1e-6)))
    return out


def synth(text: str, voice: str, speed: float = 1.0) -> bytes:
    """文本 → WAV 字节；voice 为 sherpa 音色名时走轻量引擎，否则走 CosyVoice"""
    text = (text or '').strip()
    if text == '':
        raise ValueError('empty text')
    key = (text, voice, round(float(speed), 2))
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[1] < CACHE_TTL:
        return hit[0]
    if voice in sherpa_extra:
        ent = sherpa_extra[voice]
        t0 = time.time()
        wav = synth_sherpa(text, speed, ent['engine'], ent['sid'])
        dur = max(0.0, (len(wav) - 44) / 2.0 / ent['engine'].sample_rate)
        el = time.time() - t0
        print('[tts] %-10s 音频 %.1fs / 用时 %.2fs (RTF %.2f)' % (voice, dur, el, el / max(dur, 1e-6)))
        _cache[key] = (wav, time.time())
        return wav
    if voice.endswith('-fast'):
        wav = synth_fast_clone(text, voice[:-5], speed)
        _cache[key] = (wav, time.time())
        return wav
    if sherpa is not None and (voice in ('melo', 'zh_en', 'sherpa') or voice.startswith('melo')):
        t0 = time.time()
        wav = synth_sherpa(text, speed)
        dur = max(0.0, (len(wav) - 44) / 2.0 / sherpa_sr)
        el = time.time() - t0
        print('[tts] sherpa  音频 %.1fs / 用时 %.2fs (RTF %.2f)' % (dur, el, el / max(dur, 1e-6)))
        _cache[key] = (wav, time.time())
        return wav
    prof = voices.get(voice) or {}
    if model is None and model_sft is None:
        raise RuntimeError('CosyVoice 未加载（服务以轻量模式启动）：请把音色设为 melo，'
                           '或以 --cosyvoice 1 启动服务')
    # 路由：克隆/zero-shot 音色走主模型（0.5B）；预设音色走 SFT 模型
    use_sft = model_sft is not None and not prof
    mdl = model_sft if use_sft else model
    if mdl is None:
        raise RuntimeError('音色 %s 对应的模型未加载' % voice)
    t0 = time.time()
    with _lock:
        if use_sft:
            gen = mdl.inference_sft(text, voice, speed=speed)
        elif prof.get('instruct') and hasattr(mdl, 'inference_instruct2'):
            # 注意：这版接口收「wav 路径」，内部自己读音频
            gen = mdl.inference_instruct2(text, prof['instruct'], os.path.join(ROOT, prof['prompt_wav']), speed=speed)
        elif prof.get('prompt_text') and hasattr(mdl, 'inference_zero_shot'):
            # 克隆音色走 zero-shot：参考音频的音色 + 其转写文本（注册过则直接用缓存特征）
            zid = voice if voice in zero_shot_registered else ''
            gen = mdl.inference_zero_shot(text, prof['prompt_text'], os.path.join(ROOT, prof['prompt_wav']),
                                          zero_shot_spk_id=zid, speed=speed)
        else:
            gen = mdl.inference_sft(text, voice, speed=speed)
        chunks = [out['tts_speech'] for out in gen]
    if not chunks:
        raise RuntimeError('no audio produced')
    audio = torch.concat(chunks, dim=1)
    sr = int(getattr(mdl, 'sample_rate', sample_rate))
    wav = to_wav(audio, sr)
    dur = audio.shape[1] / sr
    el = time.time() - t0
    print('[tts] %-8s 音频 %.1fs / 用时 %.1fs (RTF %.2f)' % (voice, dur, el, el / max(dur, 1e-6)))
    _cache[key] = (wav, time.time())
    return wav


@app.get('/health')
def health():
    return {
        'ok': model is not None or model_sft is not None,
        'model_dir': model_dir,
        'model_dir_sft': model_dir_sft,
        'openvoice': ov_converter is not None,
        'extra_voices': list(sherpa_extra.keys()),
        'sample_rate': sample_rate,
        'voices': list(voices.keys()),
        'preset_spks': ((model_sft.list_available_spks() if model_sft is not None else [])
                        + (model.list_available_spks() if model is not None else [])),
    }


@app.post('/inference_sft')
@app.post('/v1/inference_sft')          # 与「基址带不带 /v1」都兼容
def inference_sft(tts_text: str = Form(), spk_id: str = Form(), speed: float = Form(1.0)):
    return Response(content=synth(tts_text, spk_id, speed), media_type='audio/wav')


@app.post('/v1/audio/speech')
@app.post('/audio/speech')
def openai_speech(body: dict):
    text = body.get('input') or body.get('text') or ''
    voice = body.get('voice') or 'azure'
    speed = float(body.get('speed', 1.0))
    return Response(content=synth(text, voice, speed), media_type='audio/wav')


@app.get('/voices')
def list_voices():
    """可供选择的音色清单（游戏左栏的「刷新音色」就是读这个）"""
    out = []
    if sherpa is not None:
        out.append({'id': 'melo', 'label': 'MeloTTS 中文女声 · 轻量快速', 'engine': 'sherpa'})
    for vid, ent in sherpa_extra.items():
        out.append({'id': vid, 'label': ent['label'], 'engine': 'sherpa'})
    for name in voices.keys():
        out.append({'id': name, 'label': 'CosyVoice 克隆音色 · ' + name, 'engine': 'cosyvoice'})
        out.append({'id': name + '-fast', 'label': '快速克隆音色（melo+转换）· ' + name, 'engine': 'openvoice'})
    seen = {v['id'] for v in out}
    for m2 in (model_sft, model):
        if m2 is None:
            continue
        try:
            for spk in m2.list_available_spks():
                if spk in seen:
                    continue
                out.append({'id': spk, 'label': 'CosyVoice 预设 · ' + spk, 'engine': 'cosyvoice'})
                seen.add(spk)
        except Exception:                                  # noqa: BLE001
            pass
    return {'voices': out}


@app.get('/v1/models')
def list_models():
    """有些客户端会先探测模型列表"""
    return {'object': 'list', 'data': [{'id': os.path.basename(model_dir.rstrip('/\\')) or 'cosyvoice', 'object': 'model'}]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=9880)
    parser.add_argument('--model_dir', type=str, default='pretrained_models/Fun-CosyVoice3-0.5B')
    parser.add_argument('--sft_dir', type=str, default='pretrained_models/CosyVoice-300M-SFT',
                        help='预设音色（中文女 等）的 SFT 模型目录；与主模型同时加载，留空则只用主模型')
    parser.add_argument('--sherpa_dir', type=str, default='sherpa_models/vits-melo-tts-zh_en',
                        help='轻量引擎（sherpa-onnx VITS）模型目录，留空则禁用')
    parser.add_argument('--threads', type=int, default=6,
                        help='推理线程数（默认 6，给游戏/LLM 留核；不要用满所有核心）')
    parser.add_argument('--low-priority', type=int, default=1, help='1=降低进程优先级，避免抢占游戏')
    parser.add_argument('--provider', type=str, default='cpu',
                        help='onnxruntime 执行后端：cpu / dml（DirectML，走 AMD 核显）/ cuda')
    parser.add_argument('--ov_tau', type=float, default=0.3,
                        help='OpenVoice 音色转换的噪声系数（越小越干净，默认 0.3；0.15~0.5 之间试听选择）')
    parser.add_argument('--cosyvoice', type=int, default=0,
                        help='1=同时加载 CosyVoice（音质更高但 CPU 很慢、加载 1-2 分钟）；0=只加载轻量引擎')
    parser.add_argument('--flow_steps', type=int, default=4,
                        help='CosyVoice flow(ODE) 采样步数：越少越快、音质略降（默认 4；6 更稳，10=原版）')
    parser.add_argument('--bf16', type=int, default=0,
                        help='1=把 CosyVoice 的 LLM 权重降到 bfloat16（CPU 更快、实测音质基本无损）；0=保持 fp32（默认）')
    parser.add_argument('--prompt', type=str, default='long', choices=['long', 'short'],
                        help='zero-shot 参考音频：long=完整 6.3s（默认，最像）；short=~2.7s（prefill 更少更快，音色略降）')
    args = parser.parse_args()

    # 实例锁：写本进程 pid。模型加载期间端口尚未监听（健康检查必然失败），
    # 游戏侧靠它判断「已有实例正在启动/运行」，避免重复拉起把 CPU 抢光。
    _BOOT_LOCK = os.path.join(ROOT, '.tts_server.lock')
    try:
        with open(_BOOT_LOCK, 'w', encoding='utf-8') as _f:
            _f.write(str(os.getpid()))
    except Exception:                                    # noqa: BLE001
        print('[tts] 写实例锁失败（不影响运行）：%s' % _BOOT_LOCK)
        _BOOT_LOCK = ''

    threads = max(1, args.threads)
    # flow 采样步数（在 cosyvoice/flow/flow.py 里按此环境变量取值）
    os.environ['COSY_FLOW_STEPS'] = str(max(1, args.flow_steps))
    globals()['ov_tau'] = float(args.ov_tau)
    torch.set_num_threads(threads)
    if args.low_priority:
        try:
            import psutil
            p = psutil.Process()
            if hasattr(psutil, 'BELOW_NORMAL_PRIORITY_CLASS'):
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            print('[tts] 进程优先级已降为 below-normal（不抢游戏/LLM 的核）')
        except Exception as exc:                       # noqa: BLE001
            print('[tts] 设置优先级失败：%s' % exc)
    _dev = ('cuda/HIP · ' + torch.cuda.get_device_name(0)) if torch.cuda.is_available() else 'cpu'
    print('[tts] 线程数=%d  CPU=%s  torch=%s  推理设备=%s  flow步数=%d'
          % (threads, os.cpu_count(), torch.__version__, _dev, args.flow_steps))

    # 轻量引擎：sherpa-onnx VITS（MeloTTS），CPU 上比 CosyVoice 快一个数量级
    sd = args.sherpa_dir
    sd = sd if os.path.isabs(sd) else os.path.join(ROOT, sd)
    if sd:
        try:
            t0 = time.time()
            sherpa = load_sherpa_engine(sd, threads, args.provider)
            if sherpa is not None:
                sherpa_sr = sherpa.sample_rate
                print('[tts] 轻量引擎就绪（%s，%.1fs，%dHz）—— 请求音色用 melo 即走它'
                      % (os.path.basename(sd.rstrip('/\\')), time.time() - t0, sherpa_sr))
            else:
                print('[tts] 未找到轻量引擎模型：%s' % sd)
        except Exception as exc:                       # noqa: BLE001
            print('[tts] 轻量引擎加载失败（将只用 CosyVoice）：%s' % exc)
    else:
        print('[tts] 未指定轻量引擎目录')

    # 额外轻量音色（sherpa_voices.json）：少女/元气等中文女声，可多模型并存
    vpath = os.path.join(ROOT, 'sherpa_voices.json')
    if sherpa is not None and os.path.exists(vpath):
        try:
            with open(vpath, 'r', encoding='utf-8') as f:
                extra = json.load(f)
            eng_cache = {}
            for item in extra:
                vid, rel = item.get('id'), item.get('dir')
                if not vid or not rel:
                    continue
                d = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
                if d not in eng_cache:
                    try:
                        eng_cache[d] = load_sherpa_engine(d, threads, args.provider)
                    except Exception as exc:           # noqa: BLE001
                        print('[tts] 额外音色模型加载失败 %s：%s' % (rel, exc))
                        eng_cache[d] = None
                eng = eng_cache.get(d)
                if eng is None:
                    print('[tts] 跳过额外音色 %s（模型不可用）' % vid)
                    continue
                sid = int(item.get('sid', 0))
                sherpa_extra[vid] = {'engine': eng, 'sid': sid, 'label': item.get('label', vid)}
                print('[tts] 额外音色就绪：%s <- %s (sid=%d) %s'
                      % (vid, os.path.basename(d), sid, item.get('label', '')))
        except Exception as exc:                       # noqa: BLE001
            print('[tts] 读取 sherpa_voices.json 失败：%s' % exc)

    voices = read_voices()
    if args.prompt == 'short':                     # 短参考：prefill 更少 → 更快（音色相似度略降）
        for _n, _p in voices.items():
            if _p.get('prompt_short_wav'):
                _p['prompt_wav'] = _p['prompt_short_wav']
                _p['prompt_text'] = _p.get('prompt_short_text', _p.get('prompt_text', ''))
                print('[tts] 音色 %s 使用短参考：%s' % (_n, _p['prompt_short_wav']))
    if args.cosyvoice:
        from cosyvoice.cli.cosyvoice import AutoModel as _AutoModel      # noqa: PLC0415
        from cosyvoice.utils.file_utils import load_wav as _load_wav    # noqa: PLC0415
        globals()['AutoModel'] = _AutoModel
        globals()['load_wav'] = _load_wav
        path = args.model_dir if os.path.isabs(args.model_dir) else os.path.join(ROOT, args.model_dir)
        print('[tts] 加载 CosyVoice：%s（首次加载 1-2 分钟）' % path)
        t0 = time.time()
        model = AutoModel(model_dir=path)
        sample_rate = model.sample_rate
        model_dir = path
        print('[tts] CosyVoice 就绪，用时 %.1fs，采样率 %d' % (time.time() - t0, sample_rate))
        if args.bf16:
            try:
                model.model.llm = model.model.llm.to(torch.bfloat16)
                print('[tts] LLM 权重已转 bfloat16（省内存带宽→更快；实测回读正确、音高一致）')
            except Exception as exc:                   # noqa: BLE001
                print('[tts] bf16 转换失败（保持 fp32）：%s' % exc)
        sp = args.sft_dir
        sp = sp if os.path.isabs(sp) else os.path.join(ROOT, sp)
        if sp and os.path.isdir(sp):
            print('[tts] 加载预设音色模型（SFT）：%s' % sp)
            t0 = time.time()
            try:
                model_sft = AutoModel(model_dir=sp)
                model_dir_sft = sp
                print('[tts] SFT 就绪，用时 %.1fs，采样率 %d' % (time.time() - t0, model_sft.sample_rate))
            except Exception as exc:                       # noqa: BLE001
                print('[tts] SFT 加载失败（中文女 等预设音色将不可用）：%s' % exc)
        else:
            print('[tts] 未找到预设音色模型目录：%s' % sp)
        for name, prof in voices.items():
            try:
                model.add_zero_shot_spk(prof.get('prompt_text', ''), os.path.join(ROOT, prof['prompt_wav']), name)
                zero_shot_registered.add(name)
                print('[tts] 注册音色：%s <- %s' % (name, prof['prompt_wav']))
            except Exception as exc:                   # noqa: BLE001
                print('[tts] 音色 %s 注册失败：%s' % (name, exc))
        if voices:                                     # 热启动：先预热一句，把惰性初始化/首次分配的成本挪到启动阶段
            try:
                t0 = time.time()
                synth('你好呀。', next(iter(voices)))
                print('[tts] CosyVoice 预热完成（%s），用时 %.1fs'
                      % ('GPU' if torch.cuda.is_available() else 'CPU', time.time() - t0))
            except Exception as exc:                   # noqa: BLE001
                print('[tts] CosyVoice 预热失败（不影响使用）：%s' % exc)
    else:
        print('[tts] 跳过 CosyVoice（--cosyvoice 0）：只有轻量引擎，启动快、占用低')

    print('[tts] 服务地址 http://127.0.0.1:%d  （/inference_sft 与 /v1/audio/speech 都可）' % args.port)
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port)
    except (OSError, SystemExit) as exc:
        print('[tts] 端口 %d 无法绑定（多半已经有一个语音服务在跑）：%s' % (args.port, exc))
        print('[tts] 直接使用正在运行的那个即可；要另开请改 --port（游戏里同步改地址）')
        sys.exit(1)
    finally:
        if _BOOT_LOCK:                                   # 进程退出（含绑定失败）：撤掉实例锁
            try:
                os.remove(_BOOT_LOCK)
            except Exception:                            # noqa: BLE001
                pass