# -*- coding: utf-8 -*-
"""Azure 窥屏（游戏侧一次性进程）：前台窗口信息 → 截图 → OCR 文字 + Qwen2.5-VL 画面描述 → 写 JSON

由游戏（Main.gd 长时间无操作时）拉起：
    python azure_peek.py --out <result.json> --game-pid <游戏进程 pid> [--vlm-url http://127.0.0.1:8090/v1]

前台窗口就是游戏本身（pid 相同或标题含 azure）时立即返回 is_game=true，不跑 OCR/VLM。
OCR 用 RapidOCR（缺失时回退 PaddleOCR），VLM 优先走 GPU 常驻 llama-server（--vlm-url），
调用失败回退本地 llama_cpp（CPU）；模型与截图都在本机，不联网。只跑一次就退出。
结果 JSON：ok/is_game/fg_title/fg_pid/ocr/vlm/vlm_via/ms[/ocr_error/vlm_error/error]，
旁边会留一份 <result>.log 便于排查。
"""
import argparse
import ctypes
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

def _find_root(start: Path) -> Path:
    """从本脚本所在目录向上找含 models/ 的根（仓库内 models 通常在外层 D:\\KataGo，而非 AzureGo-Creative）。"""
    for p in [start, *start.parents]:
        if (p / "models").is_dir():
            return p
    return start.parent


ROOT = _find_root(Path(__file__).resolve().parent)     # 开发机 = D:\KataGo
OCR_DIR = ROOT / "models" / "OCR"
VLM_GGUF = ROOT / "models" / "Qwen2.5-VL-3B-Instruct-IQ4_NL.gguf"
MMPROJ_CANDIDATES = [
    ROOT / "models" / "mmproj-BF16.gguf",
    Path(r"E:\AI_Omni\models\mmproj-BF16.gguf"),
]


def _hide_console() -> None:
    """由游戏用 python.exe 拉起时，Windows 会给控制台程序分配一个可见窗口；
    第一行就把它藏掉（用 pythonw 时本来就没有窗口，这里是兜底）。
    """
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)      # SW_HIDE
    except Exception:                                      # noqa: BLE001
        pass


def _log(*args, **kwargs) -> None:
    """pythonw 下 sys.stdout 是 None，print 会抛；日志可有可无，绝不能因此崩。"""
    try:
        print(*args, **kwargs)
    except Exception:                                      # noqa: BLE001
        pass


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="结果 JSON 输出路径")
    ap.add_argument("--game-pid", type=int, default=0, help="游戏进程 pid：前台就是它则只回 is_game")
    ap.add_argument("--shot", default="", help="调试：把截到的窗口图存成 png")
    ap.add_argument("--max-side", type=int, default=768, help="送 VLM 的图最长边（越小越快）")
    ap.add_argument("--vlm-url", default="",
                    help="VLM 服务地址（OpenAI 兼容，形如 http://127.0.0.1:8090/v1）；"
                         "为空或调用失败时回退到本地 llama_cpp（CPU）")
    return ap.parse_args()


def ensure_dpi():
    """先声明 DPI 感知，否则截图坐标会被系统缩放错位（进程内只有首次有效，重复调用无害）"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                              # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                          # noqa: BLE001
            pass


def foreground_window():
    """返回 (标题, pid, (l, t, r, b))；失败时抛异常"""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("GetForegroundWindow 返回空")
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return buf.value, int(pid.value), (rect.left, rect.top, rect.right, rect.bottom)


def grab_window(rect):
    """按窗口矩形截图（覆盖全屏时就是整屏）；矩形不可用时退回整屏"""
    from PIL import ImageGrab

    l, t, r, b = rect
    if r - l < 120 or b - t < 120:
        img = ImageGrab.grab(all_screens=True)
    else:
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return img.convert("RGB")


def load_ocr():
    """优先 RapidOCR（onnxruntime，实测比 PaddleOCR 快 ~4 倍、质量相当）；缺失则回退 PaddleOCR。"""
    try:
        from rapidocr_onnxruntime import RapidOCR

        t0 = time.time()
        engine = RapidOCR()
        _log("[peek] OCR 引擎：RapidOCR（初始化 %.2fs）" % (time.time() - t0), flush=True)
        return ("rapid", engine)
    except Exception as e:                         # noqa: BLE001
        _log("[peek] RapidOCR 不可用（%r），回退 PaddleOCR" % e, flush=True)
    return ("paddle", load_paddle_ocr())


def load_paddle_ocr():
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    # Paddle 3.3.1 的 PIR 新执行器 + oneDNN 在这几个 OCR 模型上会抛
    # NotImplementedError(... onednn_instruction.cc:118)；而 PaddleX 默认给它们选的就是 mkldnn 模式。
    # 这里把 run_mode 强制成 "paddle"（纯 CPU kernel）绕开该 bug。
    import paddlex.inference.models.runners.paddle_static.runner as R

    _orig = R.resolve_paddle_static_engine_config

    def _patched(model_name, engine_config):
        cfg = _orig(model_name, engine_config)
        cfg["run_mode"] = "paddle"
        return cfg

    R.resolve_paddle_static_engine_config = _patched
    from paddleocr import PaddleOCR

    return PaddleOCR(
        text_detection_model_name="PP-OCRv6_small_det",
        text_detection_model_dir=str(OCR_DIR / "det" / "PP-OCRv6_small_det_infer"),
        text_recognition_model_name="PP-OCRv6_tiny_rec",
        text_recognition_model_dir=str(OCR_DIR / "rec" / "PP-OCRv6_tiny_rec_infer"),
        textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
        textline_orientation_model_dir=str(OCR_DIR / "cls" / "PP-LCNet_x1_0_textline_ori_infer"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )


def run_ocr(engine, img):
    """截图 → 识别出的文字（一行行拼起来，限制长度）；engine 为 load_ocr() 的 (kind, ocr)"""
    import numpy as np

    if max(img.size) > 2048:
        s = 2048 / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)))
    arr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])   # PIL RGB → BGR
    kind, ocr = engine
    raw = []
    if kind == "rapid":
        res, _ = ocr(arr)
        raw = [str(r[1]) for r in (res or [])]
    else:
        for r in ocr.predict(arr):
            d = r if isinstance(r, dict) else {}
            raw.extend(str(t) for t in (d.get("rec_texts") or []))
    texts = []
    for t in raw:
        t = t.strip()
        if t and (not texts or texts[-1] != t):
            texts.append(t)
    return " | ".join(texts)[:400]


VLM_PROMPT = ("这是一张电脑屏幕截图。用一两句简短的中文描述屏幕上正在做什么"
              "（什么软件、显示的是什么内容）。不要描述聊天内容、账号、密码、金额、姓名等隐私信息；"
              "如果画面以聊天 / 支付 / 账号为主，只说「他在处理私人的事情」。"
              "别猜人物身份，不超过50字。")


def _img_to_b64(img, max_side):
    """缩到 max_side 长边并按生产参数编码 JPEG，返回 (base64, 缩放后图)"""
    import base64

    if img.width > max_side:
        img = img.resize((max_side, max(1, int(img.height * max_side / img.width))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode(), img


def run_vlm_api(img, max_side, url, timeout=120.0):
    """截图 → llama-server（OpenAI 兼容）的中文画面描述（GPU 常驻服务）"""
    import urllib.request

    b64, _ = _img_to_b64(img, max_side)
    payload = {
        "model": "vlm",
        "messages": [
            {"role": "system", "content": "你是一个简短的画面描述助手，始终用简体中文回答。"},
            {"role": "user", "content": [
                {"type": "text", "text": VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]},
        ],
        "max_tokens": 80,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"]).strip()


def run_vlm_cpu(img, max_side):
    """截图 → Qwen2.5-VL 的中文画面描述（本地 llama_cpp，CPU，兜底路径）"""
    b64, _ = _img_to_b64(img, max_side)
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import Qwen25VLChatHandler

    mmproj = next((p for p in MMPROJ_CANDIDATES if p.exists()), None)
    if mmproj is None:
        raise RuntimeError("找不到 mmproj（视觉编码器）")
    handler = Qwen25VLChatHandler(clip_model_path=str(mmproj), verbose=False)
    llm = Llama(model_path=str(VLM_GGUF), chat_handler=handler, n_ctx=4096, n_threads=12, verbose=False)
    out = llm.create_chat_completion(
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }],
        max_tokens=100,
        temperature=0.3,
    )
    return out["choices"][0]["message"]["content"].strip()


def main() -> int:
    _hide_console()
    args = parse_args()
    t0 = time.time()
    logf = open(str(args.out) + ".log", "w", encoding="utf-8")

    def log(msg):
        line = "[%.1fs] %s" % (time.time() - t0, msg)
        _log(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    ensure_dpi()
    result = {"ok": False, "is_game": False, "fg_title": "", "fg_pid": 0,
              "ocr": "", "vlm": "", "ms": {}}
    try:
        title, pid, rect = foreground_window()
        is_game = (args.game_pid > 0 and pid == args.game_pid) or "azure" in title.lower()
        result.update(fg_title=title, fg_pid=pid, is_game=is_game, ok=True)
        log("前台窗口：%r pid=%d rect=%s is_game=%s" % (title, pid, rect, is_game))
        if not is_game:
            t = time.time()
            img = grab_window(rect)
            result["ms"]["grab"] = int((time.time() - t) * 1000)
            log("截图 %dx%d" % img.size)
            if args.shot:
                img.save(args.shot)
            if OCR_DIR.exists():
                t = time.time()
                try:
                    result["ocr"] = run_ocr(load_ocr(), img)
                    result["ms"]["ocr"] = int((time.time() - t) * 1000)
                    log("OCR(%dms)：%s" % (result["ms"]["ocr"], result["ocr"][:120]))
                except Exception as e:             # noqa: BLE001
                    result["ocr_error"] = repr(e)
                    log("OCR 失败：%r" % e)
                    traceback.print_exc()
            else:
                log("OCR 模型目录缺失：%s" % OCR_DIR)
            if VLM_GGUF.exists() or args.vlm_url:
                t = time.time()
                try:
                    if args.vlm_url:
                        try:
                            text = run_vlm_api(img, args.max_side, args.vlm_url)
                            via = "api"
                        except Exception as e:     # noqa: BLE001
                            log("[peek] VLM API 调用失败（%r），回退 CPU 本地推理" % e)
                            text = run_vlm_cpu(img, args.max_side)
                            via = "cpu"
                    else:
                        text = run_vlm_cpu(img, args.max_side)
                        via = "cpu"
                    result["vlm"] = text
                    result["vlm_via"] = via
                    result["ms"]["vlm"] = int((time.time() - t) * 1000)
                    log("VLM(%dms, %s)：%s" % (result["ms"]["vlm"], via, text))
                except Exception as e:             # noqa: BLE001
                    result["vlm_error"] = repr(e)
                    result["ms"]["vlm"] = int((time.time() - t) * 1000)
                    log("VLM 失败：%r" % e)
                    traceback.print_exc()
            else:
                log("VLM 模型缺失：%s" % VLM_GGUF)
    except Exception as e:                         # noqa: BLE001
        result["ok"] = False
        result["error"] = repr(e)
        log("整体失败：%r" % e)
        traceback.print_exc()
    finally:
        result["ms"]["total"] = int((time.time() - t0) * 1000)
        tmp = str(args.out) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, args.out)                  # 原子落盘：游戏侧读到的永远是完整 JSON
        log("完成：%s" % json.dumps(result, ensure_ascii=False)[:300])
        logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())