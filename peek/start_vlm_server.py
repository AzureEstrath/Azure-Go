# -*- coding: utf-8 -*-
"""拉起 Azure 窥屏用的 VLM 服务（llama-server，ROCm/GPU 常驻，一次性退出）。

由游戏侧（Main.gd）在长时间闲置窥屏前调用；服务本身是常驻的（加载一次，之后每次
窥屏推理 ~1-2 秒）。已在运行则直接返回。锁文件 .vlm_server.lock 记录 llama-server 的 pid，
游戏侧用「锁 + 进程存活」判断，避免重复拉起（与 TTS 服务的 .tts_server.lock 同一套思路）。

用法：
    python start_vlm_server.py [--exe ...] [--model ...] [--mmproj ...] [--port 8090]
"""
import argparse
import ctypes
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 开发机 = repo/peek


def _hide_console() -> None:
    """由游戏用 python.exe 拉起时，Windows 会给控制台程序分配一个可见窗口；
    第一行就把它藏掉（用 pythonw 时本来就没有窗口，这里是兜底）。
    """
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)      # SW_HIDE
    except Exception:                                      # noqa: BLE001
        pass


def _log(*args) -> None:
    """pythonw 下 sys.stdout 是 None，print 会抛；日志可有可无，绝不能因此崩。"""
    try:
        print(*args)
    except Exception:                                      # noqa: BLE001
        pass


def _find_root(start: Path) -> Path:
    """从本脚本所在目录向上找含 models/ 的根（仓库内 models 通常在外层 D:\\KataGo，而非 AzureGo-Creative）。"""
    for p in [start, *start.parents]:
        if (p / "models").is_dir():
            return p
    return start.parent


ROOT = _find_root(HERE)
LOCK = HERE / ".vlm_server.lock"
LOGF = HERE / ".vlm_server.log"

EXE_CANDIDATES = [
    ROOT / "llamacpp-rocm" / "llama-server.exe",
    HERE.parent / "llamacpp-rocm" / "llama-server.exe",
    Path(r"E:\AI.2\llama ROCm\llama-server.exe"),
    HERE / "llama-server.exe",
]
MODEL_CANDIDATES = [
    ROOT / "models" / "Qwen2.5-VL-3B-Instruct-IQ4_NL.gguf",
    HERE.parent / "models" / "Qwen2.5-VL-3B-Instruct-IQ4_NL.gguf",
]
MMPROJ_CANDIDATES = [
    ROOT / "models" / "mmproj-BF16.gguf",
    HERE.parent / "models" / "mmproj-BF16.gguf",
    Path(r"E:\AI_Omni\models\mmproj-BF16.gguf"),
]


def _alive(pid: int) -> bool:
    """Windows：进程是否还活着（不发送信号，避免误杀）"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:                                # noqa: BLE001
        return False


def _first(paths):
    return next((p for p in paths if Path(p).exists()), None)


def main() -> int:
    _hide_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--mmproj", default="")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()

    # 已在跑（锁有效，或端口已有服务响应）：不重复拉起
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text(encoding="utf-8").strip())
            if pid > 0 and _alive(pid):
                _log("[vlm] 服务已在运行（pid=%d），跳过" % pid)
                return 0
        except Exception:                            # noqa: BLE001
            pass
    if _health_ok(a.port):
        _log("[vlm] 端口 %d 已有 VLM 服务响应，跳过拉起" % a.port)
        return 0

    exe = a.exe or _first(EXE_CANDIDATES)
    model = a.model or _first(MODEL_CANDIDATES)
    mmproj = a.mmproj or _first(MMPROJ_CANDIDATES)
    missing = [n for n, v in (("llama-server", exe), ("模型", model), ("mmproj", mmproj)) if not v]
    if missing:
        _log("[vlm] 缺少 %s，无法拉起服务" % "/".join(missing))
        return 2

    args = [str(exe), "-m", str(model), "--mmproj", str(mmproj),
            "-ngl", "99", "-fa", "on", "-c", "4096",
            "--host", "127.0.0.1", "--port", str(a.port),
            "--no-webui", "-t", str(a.threads)]
    logf = open(LOGF, "ab")
    logf.write(("\n=== start %s ===\n" % " ".join(args)).encode("utf-8"))
    logf.flush()

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    p = subprocess.Popen(args, cwd=str(Path(exe).parent), stdout=logf, stderr=logf,
                         creationflags=flags, startupinfo=si, close_fds=True)
    LOCK.write_text(str(p.pid), encoding="utf-8")
    _log("[vlm] 已启动 llama-server（pid=%d，端口 %d），日志：%s" % (p.pid, a.port, LOGF))
    return 0


if __name__ == "__main__":
    sys.exit(main())