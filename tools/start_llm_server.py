# -*- coding: utf-8 -*-
"""拉起 Azure 陪练用的本地 LLM 服务（llama-server，ROCm/GPU 常驻）。

由游戏侧（Main.gd）在启动时调用；服务本身是常驻的（加载一次，之后每次点评/对话
首字 1~2 秒）。锁文件 .llm_server.lock 记录 llama-server 的 pid，用「锁 + 进程存活」
判断，避免重复拉起（与 TTS/VLM 服务的 .tts_server.lock 同一套思路）。

自愈逻辑（修「服务起不来 → 游戏一直提示无法连接」）：
  1) 锁里的进程还活着但没就绪：先等它加载完；一直既不响应也不退出（疑似卡死）才结束它重来；
  2) 拉起后盯到就绪为止：进程中途崩溃（偶发 ROCm 启动报错）就自动重试，至多 MAX_ATTEMPTS 次；
  3) 游戏已退出（父进程没了）就不再重试，避免退出后凭空又拉起一个常驻服务。

用法：
    python start_llm_server.py [--exe ...] [--model ...] [--port 8080] [--threads 16]
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 本脚本所在目录（开发机 = D:\KataGo\tools；分享包 = 根目录）
_CAND_ROOTS = [HERE, HERE.parent, HERE.parent.parent]        # 逐级上溯：分享包=tools 同级即根；开发机=tools/ 或 AzureGo-legacy/tools/ 之上
ROOT = next((c for c in _CAND_ROOTS if (c / "models").is_dir()), HERE.parent)   # 工作区/分享包根目录：llamacpp-rocm、models 与锁文件都在这里
LOCK = ROOT / ".llm_server.lock"
LOGF = ROOT / ".llm_server.log"
LAUNCHING = ROOT / ".llm_server.launching"      # 「有启动器正在重试」的标记：防止并发拉起多份服务

EXE_CANDIDATES = [
    Path(r"E:\AI.2\llama ROCm\llama-server.exe"),
    ROOT / "llamacpp-rocm" / "llama-server.exe",
    Path(r"E:\AI.2\llama\llama-server.exe"),
]
MODEL_CANDIDATES = [
    ROOT / "models" / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
]

WAIT_READY = 120        # 每次拉起后等「就绪」的上限（秒）；模型加载实测 15~30 秒
# 偶发 GPU 驱动级卡死：llama-server 会在设备初始化时秒崩（ROCm unknown error /
# current device: -1），通常几分钟内自行恢复。重试窗口必须盖过这段自愈期，
# 否则用户开局时正好撞上卡死期就会一直提示「无法连接」。
MAX_ATTEMPTS = 10       # 拉起失败自动重试的次数
RETRY_PAUSE = 30        # 重试前的间隔（秒）
RETRY_WINDOW = 360      # 自动重试的总时长上限（秒），约 5 分钟


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


def _image_of(pid: int) -> str:
    """pid 对应进程的镜像路径（拿不到返回空串）——避免误杀复用了同一 pid 的无关进程"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)             # Windows 上等价 TerminateProcess
    except Exception:                            # noqa: BLE001
        pass


def _read_lock() -> int:
    try:
        if LOCK.exists():
            pid = int(LOCK.read_text(encoding="utf-8").strip())
            return pid if pid > 0 else 0
    except Exception:                            # noqa: BLE001
        pass
    return 0


def _clear_lock() -> None:
    try:
        LOCK.unlink(missing_ok=True)
    except Exception:                            # noqa: BLE001
        pass


def _launching_other() -> bool:
    """是否已有另一个启动器正在重试窗口内工作（标记新鲜且其进程存活）"""
    try:
        parts = LAUNCHING.read_text(encoding="utf-8").split()
        pid, ts = int(parts[0]), float(parts[1])
    except Exception:                            # noqa: BLE001
        return False
    if time.time() - ts > RETRY_WINDOW + 120:
        return False
    return pid != os.getpid() and _alive(pid)


def _wait_ready(pid: int, port: int, seconds: int) -> bool:
    """等 /health 就绪；进程中途退出立即返回 False"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _alive(pid):
            return False
        if _health_ok(port):
            return True
        time.sleep(1)
    return _health_ok(port)


def _game_alive(game_pid: int) -> bool:
    """游戏是否还在：游戏退出后不再续拉，避免凭空又拉起一个常驻服务。
    优先用游戏传入的 --game-pid（可靠）；没传时退回父进程判断（够用，但受解释器包装链影响）"""
    try:
        if game_pid > 0:
            return _alive(game_pid)
        ppid = os.getppid()
        return ppid > 0 and _alive(ppid)
    except Exception:                            # noqa: BLE001
        return True


def _first(paths):
    return next((p for p in paths if Path(p).exists()), None)


def _spawn(exe: Path, model: Path, port: int, threads: int):
    # 用户 bat 已验证的稳定参数：全量 offload + 关 mmap（ROCm 上更省内存）+ q8_0 KV 缓存
    args = [str(exe), "-m", str(model),
            "-ngl", "99", "--no-mmap", "-c", "8192", "-np", "1", "-fa", "on",
            "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--host", "127.0.0.1", "--port", str(port),
            "--threads", str(threads)]
    logf = open(LOGF, "ab")
    logf.write(("\n=== start %s ===\n" % " ".join(args)).encode("utf-8"))
    logf.flush()
    env = os.environ.copy()
    env["HSA_XNACK"] = "1"                           # ROCm：允许统一内存缺页回退（缺了会直接崩）
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return subprocess.Popen(args, cwd=str(Path(exe).parent), stdout=logf, stderr=logf,
                            creationflags=flags, startupinfo=si, close_fds=True, env=env)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--game-pid", type=int, default=0)   # 游戏进程 pid：它没了就停止续拉
    a = ap.parse_args()

    # 1) 锁里的服务还活着？
    pid = _read_lock()
    if pid and _alive(pid):
        if _health_ok(a.port):
            print("[llm] 服务已在运行（pid=%d），跳过" % pid)
            return 0
        print("[llm] 服务进程 %d 存活但尚未就绪，等它加载…" % pid)
        if _wait_ready(pid, a.port, WAIT_READY):
            print("[llm] 服务已就绪（pid=%d），跳过" % pid)
            return 0
        if _alive(pid) and "llama-server" in _image_of(pid).lower():
            print("[llm] 服务 %d 卡死无响应，结束它后重新拉起" % pid)
            _kill(pid)
            time.sleep(2)
        _clear_lock()
    elif pid:
        _clear_lock()                                # 锁里的进程早没了：清掉旧锁

    # 2) 端口上已有别的服务在响应（不是本锁拉起的）：直接用它，不重复拉起
    if _health_ok(a.port):
        print("[llm] 端口 %d 已有 LLM 服务响应，跳过拉起" % a.port)
        return 0

    exe = a.exe or _first(EXE_CANDIDATES)
    model = a.model or _first(MODEL_CANDIDATES)
    missing = [n for n, v in (("llama-server", exe), ("模型", model)) if not v]
    if missing:
        print("[llm] 缺少 %s，无法拉起服务" % "/".join(missing))
        return 2

    # 3) 拉起 + 盯「就绪」；中途崩溃（偶发 GPU 卡死）持续重试，直到超过总时长上限。
    #    同一时刻只允许一个启动器在拉：游戏内看护会重复请求拉起，用标记去重，避免起出多份服务
    if _launching_other():
        print("[llm] 已有另一个启动器正在重试，跳过本次")
        return 0
    LAUNCHING.write_text("%d %.0f" % (os.getpid(), time.time()), encoding="utf-8")
    try:
        t0 = time.time()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # 首次总是尝试拉起（旧版先看父进程存活，包装链误判时会一次都不拉）；
            # 重试前才检查游戏是否还在：退出了就收手
            if attempt > 1 and not _game_alive(a.game_pid):
                print("[llm] 游戏已退出，停止重试")
                return 0
            if _health_ok(a.port):                   # 别人已起好：直接用
                print("[llm] 端口 %d 已有 LLM 服务响应，跳过拉起" % a.port)
                return 0
            p = _spawn(exe, model, a.port, a.threads)
            LOCK.write_text(str(p.pid), encoding="utf-8")
            print("[llm] 已拉起 llama-server（pid=%d，端口 %d），等模型加载就绪…" % (p.pid, a.port))
            if _wait_ready(p.pid, a.port, WAIT_READY):
                if a.game_pid > 0 and not _alive(a.game_pid):
                    # 游戏在加载期间退出：模型已经加载完了就留着常驻（LLM 不再随游戏退出而结束，
                    # 下次开局直接秒用；杀掉它等于把这次加载白费，下次又得冷加载十几秒到半分钟）
                    print("[llm] 服务已就绪但游戏已退出：保留常驻（下次开局可直接使用）")
                print("[llm] 服务就绪：http://127.0.0.1:%d（pid=%d）" % (a.port, p.pid))
                return 0
            why = "进程已退出（崩溃详情见日志 %s）" % LOGF if not _alive(p.pid) else "超时未就绪"
            print("[llm] 第 %d/%d 次拉起失败：%s" % (attempt, MAX_ATTEMPTS, why))
            if _alive(p.pid):
                _kill(p.pid)
                time.sleep(1)
            _clear_lock()
            if attempt < MAX_ATTEMPTS:
                if time.time() - t0 >= RETRY_WINDOW:
                    print("[llm] 已达自动重试总时长上限（%d 秒），停止重试" % RETRY_WINDOW)
                    break
                time.sleep(RETRY_PAUSE)
        print("[llm] 自动重试约 %d 分钟后仍失败（GPU 可能需更久恢复）：请查看日志 %s"
              % (RETRY_WINDOW // 60, LOGF))
        return 3
    finally:
        try:
            LAUNCHING.unlink(missing_ok=True)
        except Exception:                        # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())