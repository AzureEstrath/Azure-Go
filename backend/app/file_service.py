"""资料箱与文件扫描：定位 inbox、列出文件、按需拉起 OCR/VLM 扫描器。

后端只做“进程编排 + 结果读取”，OCR/VLM 仍留在 `peek/.venv`（RapidOCR / Qwen2.5-VL），
避免把重型视觉依赖塞进 backend。所有故障都返回可读结果，不抛到 API 层。
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request

ASSISTANT_DIR = "assistant_files"
INBOX_SUBDIR = "inbox"
SCANNER_NAME = "azure_files.py"
SUPPORTED_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".ini", ".cfg",
    ".yaml", ".yml", ".xml", ".html", ".htm",
}

_VLM_LOCK = threading.Lock()
_VLM_LAST_START = 0.0


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def inbox_dir(cfg=None):
    """资料箱绝对路径：配置优先，默认 <repo>/assistant_files/inbox。"""
    raw = str(getattr(cfg, "assistant_inbox", "") or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(repo_root(), ASSISTANT_DIR, INBOX_SUBDIR)


def _peek_dirs():
    root = repo_root()
    return [os.path.join(root, "peek"), "D:/KataGo/peek"]


def list_files(cfg=None):
    """列出资料箱中的支持文件（不读内容）。"""
    inbox = inbox_dir(cfg)
    out = []
    try:
        if not os.path.isdir(inbox):
            os.makedirs(inbox, exist_ok=True)
        names = sorted(os.listdir(inbox), key=lambda n: os.path.getmtime(os.path.join(inbox, n)),
                       reverse=True)
    except Exception as e:                  # noqa: BLE001
        return {"ok": False, "folder": inbox, "files": [], "error": "资料箱不可读：%s" % e}
    for name in names:
        if name.startswith("."):
            continue
        p = os.path.join(inbox, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTS:
            continue
        try:
            st = os.stat(p)
            out.append({"name": name, "ext": ext, "size": int(st.st_size),
                        "modified_ts": float(st.st_mtime)})
        except OSError:
            continue
    return {"ok": True, "folder": inbox, "files": out}


def _find_scanner():
    for d in _peek_dirs():
        script = os.path.join(d, SCANNER_NAME)
        if not os.path.isfile(script):
            continue
        pys = [
            os.path.join(d, ".venv", "Scripts", "pythonw.exe"),
            os.path.join(d, ".venv", "Scripts", "python.exe"),
            r"D:/KataGo/peek/.venv/Scripts/pythonw.exe",
            r"D:/KataGo/peek/.venv/Scripts/python.exe",
        ]
        for py in pys:
            if os.path.isfile(py):
                return d, py, script
    return "", "", ""


def _vlm_ready(url):
    if not url:
        return False
    try:
        with urllib.request.urlopen(str(url).rstrip("/") + "/models", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:                      # noqa: BLE001
        return False


def _vlm_url(cfg):
    manual = str(getattr(cfg, "vlm_base_url", "") or "").strip()
    if manual:
        return manual
    port = int(getattr(cfg, "vlm_port", 8090) or 8090)
    return "http://127.0.0.1:%d/v1" % port


def ensure_vlm(cfg, wait=0.0):
    """按需拉起本机 VLM；返回 URL（不可用时 ``""``）。只拉起一次，失败不反复刷进程。"""
    global _VLM_LAST_START
    manual = str(getattr(cfg, "vlm_base_url", "") or "").strip()
    if manual:
        return manual                         # 外部/远端的 VLM 地址原样交给扫描器
    url = _vlm_url(cfg)
    if _vlm_ready(url):
        return url
    if not bool(getattr(cfg, "vlm_auto_start", True)):
        return ""
    with _VLM_LOCK:
        if time.time() - _VLM_LAST_START < 30.0:
            return url                        # 可能还在加载，交给扫描器等待
        for d in _peek_dirs():
            starter = os.path.join(d, "start_vlm_server.py")
            if not os.path.isfile(starter):
                continue
            py = next((p for p in (
                os.path.join(d, ".venv", "Scripts", "pythonw.exe"),
                os.path.join(d, ".venv", "Scripts", "python.exe"),
                r"D:/KataGo/peek/.venv/Scripts/pythonw.exe",
                r"D:/KataGo/peek/.venv/Scripts/python.exe",
            ) if os.path.isfile(p)), "")
            if py == "":
                continue
            argv = [py, starter, "--port", str(getattr(cfg, "vlm_port", 8090) or 8090)]
            if getattr(cfg, "vlm_exe", ""):
                argv += ["--exe", str(cfg.vlm_exe)]
            if getattr(cfg, "vlm_model", ""):
                argv += ["--model", str(cfg.vlm_model)]
            if getattr(cfg, "vlm_mmproj", ""):
                argv += ["--mmproj", str(cfg.vlm_mmproj)]
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(argv, cwd=d, creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _VLM_LAST_START = time.time()
                print("[Files] 已后台拉起 VLM：%s" % url)
                if wait > 0:
                    deadline = time.time() + float(wait)
                    while time.time() < deadline:
                        if _vlm_ready(url):
                            return url
                        time.sleep(1.5)
                return url
            except Exception as e:        # noqa: BLE001
                print("[Files] VLM 拉起失败（继续用 OCR / 文档文本）：%s" % e)
                break
    return ""


def scan_files(cfg, timeout=300.0):
    """跑一次资料扫描器，返回其 JSON（失败返回 ``ok=False`` + error）。"""
    inbox = inbox_dir(cfg)
    if not os.path.isdir(inbox):
        try:
            os.makedirs(inbox, exist_ok=True)
        except Exception as e:              # noqa: BLE001
            return {"ok": False, "folder": inbox, "files": [], "error": "资料箱不存在：%s" % e}
    dir_, py, script = _find_scanner()
    if not py:
        return {"ok": False, "folder": inbox, "files": [],
                "error": "找不到 peek/.venv 或 azure_files.py"}
    visual = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".pdf"}
    has_visual = any(str(f.get("ext", "")).lower() in visual
                     for f in list_files(cfg).get("files", []))
    url = ensure_vlm(cfg) if has_visual else ""
    fd, out = tempfile.mkstemp(prefix="azure_files_", suffix=".json")
    os.close(fd)
    try:
        os.remove(out)
    except OSError:
        pass
    argv = [py, script, "--inbox", inbox, "--out", out]
    if url:
        argv += ["--vlm-url", url, "--vlm-wait", "60"]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(argv, cwd=dir_, timeout=float(timeout),
                              capture_output=True, text=True, creationflags=flags)
        if not os.path.isfile(out):
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return {"ok": False, "folder": inbox, "files": [],
                    "error": "扫描器没有产出结果：%s" % (detail or "未知错误")}
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"ok": False, "folder": inbox, "files": [], "error": "扫描结果格式不正确"}
        data.setdefault("folder", inbox)
        return data
    except subprocess.TimeoutExpired:
        return {"ok": False, "folder": inbox, "files": [],
                "error": "资料处理超时（%d 秒）" % int(timeout)}
    except Exception as e:                 # noqa: BLE001
        return {"ok": False, "folder": inbox, "files": [], "error": "资料处理失败：%s" % e}
    finally:
        for p in (out, out + ".log"):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
