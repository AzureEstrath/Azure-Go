"""本地服务编排（对应 Main.gd 的「服务拉起/看护」职责）。

前后端分离后，对局编排与 KataGo/LLM 都在后端进程；后端启动时负责：
  - 拉起本地 LLM（llama-server）：复用 ``tools/start_llm_server.py``（锁 + 健康检查 + 崩溃重试）
  - 对局中看护：本地 LLM 持续不可达时重新拉起（由 ``LLMClient.watch_loop`` 触发本模块的 ensure_local_llm）

KataGo 的启动在 ``main.py``（``engine.start()``）；TTS / 窥屏 VLM 归客户端
（渲染与交互在本进程），后端不涉及，故不在此模块内。

配置来源与 Godot 版一致：地址指向本机且 ``llm_auto_start=true`` 才拉起。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

_URL_RE = re.compile(r"^(https?)://([^:/]+)")

# 启动器候选：后端目录（分享包）→ 开发机 tools 目录
_STARTER_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "start_llm_server.py",
    Path(r"D:\KataGo\tools\start_llm_server.py"),
]


def is_local_url(url: str) -> bool:
    """是否为指向本机的地址（与 Main.gd 的 _is_local_url 一致）。"""
    m = _URL_RE.search((url or "").strip())
    if m is None:
        return False
    return m.group(2).lower() in ("127.0.0.1", "localhost", "::1")


def _find_starter():
    for p in _STARTER_CANDIDATES:
        if p.is_file():
            return p
    return None


def ensure_local_llm(cfg) -> bool:
    """确保本地 LLM 服务在跑（幂等：启动器自带锁 + 健康检查，已在跑则立即返回）。

    返回是否成功发起了启动器（不代表服务已就绪，模型加载在后台进行）。
    与 ``Main.gd`` 的 ``_ensure_llm_url`` 行为一致：外部地址或关掉自动拉起则不碰对方。
    """
    url = str(getattr(cfg, "llm_base_url", "")).strip()
    if not is_local_url(url) or not bool(getattr(cfg, "llm_auto_start", True)):
        return False
    starter = _find_starter()
    if starter is None:
        print("[LLM] 未找到 start_llm_server.py，跳过自动拉起（服务需自行启动）")
        return False

    port = int(getattr(cfg, "llm_port", 8080))
    args = [sys.executable, str(starter),
            "--port", str(port),
            "--game-pid", str(os.getpid())]     # 后端退出后，启动器不再续拉服务
    exe = str(getattr(cfg, "llm_exe", "") or "")
    model = str(getattr(cfg, "llm_model_path", "") or "")
    if exe:
        args += ["--exe", exe]
    if model:
        args += ["--model", model]
    try:
        p = _spawn_detached(args, str(starter.parent))
    except Exception as e:                      # noqa: BLE001
        print("[LLM] 本地 LLM 服务启动器拉起失败：%s（服务需自行启动）" % e)
        return False
    print("[LLM] 已后台拉起本地 LLM 服务（启动器 pid=%d，端口 %d），加载模型需要一段时间" % (p.pid, port))
    return True


def _spawn_detached(args, cwd):
    """隐藏窗口、脱离父进程：后端退出后启动器仍能自行完成加载/重试。"""
    flags = 0
    si = None
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    return subprocess.Popen(args, cwd=cwd, creationflags=flags, startupinfo=si, close_fds=True)
