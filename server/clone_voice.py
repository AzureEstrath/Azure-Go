# -*- coding: utf-8 -*-
"""从录音里裁剪出 CosyVoice 克隆用的参考音频（prompt wav + 转写文本）。

用法：
    python clone_voice.py 1.mp3 2.mp3 3.mp3
产物：
    asset/azure_clone.wav      16kHz 单声道 16bit 的参考音频（选段+归一化）
    asset/azure_clone.txt      Whisper 转写的对应文本（填进 voices.json 的 prompt_text）
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SR = 16000
HOP = 0.05          # VAD 帧长 0.05s


def speech_segments(y):
    """按能量找出连续说话段，合并小于 0.45s 的停顿 → [(起, 止)] 秒"""
    hop = int(SR * HOP)
    n = len(y) // hop
    if n == 0:
        return []
    rms = np.array([float(np.sqrt(np.mean(y[i * hop:(i + 1) * hop] ** 2))) for i in range(n)])
    thr = max(float(np.percentile(rms, 20)) * 2.0, float(rms.max()) * 0.05, 1e-4)
    act = rms > thr
    segs = []
    start = -1
    gap = 0
    for i in range(n):
        if act[i]:
            if start < 0:
                start = i
            gap = 0
        elif start >= 0:
            gap += 1
            if gap * HOP > 0.45:            # 停顿够长 → 断开
                segs.append((start * HOP, (i - gap + 1) * HOP))
                start = -1
                gap = 0
    if start >= 0:
        segs.append((start * HOP, n * HOP))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('--out', default=os.path.join(ROOT, 'asset', 'azure_clone.wav'))
    ap.add_argument('--whisper', default='small')
    ap.add_argument('--min-sec', type=float, default=4.0)
    ap.add_argument('--max-sec', type=float, default=10.0)
    args = ap.parse_args()

    import librosa
    import soundfile as sf

    cands = []
    for p in args.inputs:
        y, _ = librosa.load(p, sr=SR, mono=True)
        segs = speech_segments(y)
        print('[cut] %s 时长 %.2fs，检测到 %d 个说话段' % (p, len(y) / SR, len(segs)))
        for (a, b) in segs:
            seg = y[int(a * SR):int(b * SR)]
            d = len(seg) / SR
            if d < 1.2:
                continue
            r = float(np.sqrt(np.mean(seg ** 2)))
            bonus = 1.0 if args.min_sec <= d <= args.max_sec else 0.45
            cands.append({'score': bonus * min(d, args.max_sec) * r, 'seg': seg, 'dur': d, 'src': p})
    if not cands:
        print('[cut] 没找到可用说话段')
        sys.exit(1)

    cands.sort(key=lambda c: c['score'], reverse=True)
    best = cands[0]
    if best['dur'] >= args.min_sec:
        prompt = best['seg']
        used = [best]
    else:                                   # 太短 → 拼几段（之间留 0.2s 静音）
        prompt = np.zeros(0, dtype=np.float32)
        used = []
        total = 0.0
        for c in cands:
            if total >= args.min_sec or len(used) >= 3:
                break
            prompt = np.concatenate([prompt, c['seg'], np.zeros(int(0.2 * SR), dtype=np.float32)])
            used.append(c)
            total += c['dur'] + 0.2
    used_desc = ' + '.join('%s(%.2fs)' % (os.path.basename(c['src']), c['dur']) for c in used)
    print('[cut] 选用：%s → 共 %.2fs' % (used_desc, len(prompt) / SR))

    peak = float(np.abs(prompt).max())
    if peak > 1e-6:
        prompt = prompt * (0.9 / peak)
    prompt = np.concatenate([np.zeros(int(0.1 * SR), dtype=np.float32), prompt,
                             np.zeros(int(0.1 * SR), dtype=np.float32)])

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    sf.write(args.out, prompt, SR, subtype='PCM_16')
    print('[cut] 参考音频已写入：%s（%.2fs，16kHz）' % (args.out, len(prompt) / SR))

    import whisper
    wm = None
    for name in [args.whisper, 'base', 'tiny']:
        try:
            wm = whisper.load_model(name)
            print('[asr] 使用 Whisper 模型：%s' % name)
            break
        except Exception as exc:            # noqa: BLE001
            print('[asr] %s 载入失败：%s' % (name, exc))
    if wm is None:
        print('[asr] Whisper 不可用，跳过转写（可手动把文本填进 voices.json 的 prompt_text）')
        sys.exit(1)
    res = wm.transcribe(prompt.astype(np.float32), language='zh', fp16=False, verbose=False)
    text = str(res.get('text', '')).strip()
    txt_path = os.path.splitext(args.out)[0] + '.txt'
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(text)
    print('[asr] 转写：%s' % text)
    print('[asr] 已写入：%s' % txt_path)


if __name__ == '__main__':
    main()