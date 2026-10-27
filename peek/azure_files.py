"""资料文件扫描：把 assistant_files/inbox 里的文件读成 OCR / VLM / 文档文本。

由后端按需拉起（pythonw，隐藏窗口），跑完一次写 JSON 就退出：
    pythonw azure_files.py --inbox <dir> --out <result.json> [--vlm-url ...] [--vlm-wait 60]

支持：
- 图片：png / jpg / jpeg / webp / bmp / gif（PIL；OCR 用 RapidOCR/PaddleOCR，VLM 用本地服务）
- 文本：txt / md / csv / json / log / html / htm / xml / yaml / yml
- 文档：docx（zip+XML）、pdf（PyMuPDF 优先，pypdf 文本兜底）、xlsx（openpyxl，可选）、
        pptx（zip+XML 文本）
- 其他二进制：写 error，不参与日程提取

输出：{"ok":true,"inbox":...,"files":[{name,ext,kind,text,ocr,vlm,error,ms}],"ms":...}
所有失败都落成 error 字段，不影响其他文件。
"""
import argparse
import html
import io
import json
import os
import re
import sys
import time
import traceback
import zipfile
from pathlib import Path

try:
    import azure_peek
except Exception:  # noqa: BLE001
    azure_peek = None

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 6000
MAX_PDF_PAGES = 6

TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".ini", ".cfg",
             ".yaml", ".yml", ".xml", ".html", ".htm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
DOC_EXTS = {".docx", ".pdf", ".xlsx", ".pptx"}
SUPPORTED = TEXT_EXTS | IMAGE_EXTS | DOC_EXTS

FILE_VLM_PROMPT = (
    "请阅读这张资料图片，逐条提取其中出现的日期、时间、日程、会议、约定、待办与关键备注。"
    "保留原文中的数字与标题；没有明确日程时就回答「未发现明确日程」。"
    "不要编造，不要描述图片风格或人物。用简体中文，最多 8 条。"
)
FILE_VLM_SYSTEM = "你是资料整理助手，只提取图片中真实存在的文字与日程信息，始终用简体中文回答。"


def _log(*args):
    try:
        print(*args, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _clip(s, n=MAX_TEXT_CHARS):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _read_text_file(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() in (".html", ".htm", ".xml"):
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    return _clip(text)


def _read_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = xml.replace("</w:p>", "\n").replace("</w:tr>", "\n")
    text = re.sub(r"<[^>]+>", " ", xml)
    return _clip(html.unescape(text))


def _read_pptx(path):
    texts = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if re.match(r"ppt/slides/slide\d+\.xml$", name):
                xml = z.read(name).decode("utf-8", errors="replace")
                texts.append(re.sub(r"<[^>]+>", " ", html.unescape(xml)))
    return _clip("\n".join(texts))


def _read_pdf(path):
    """PDF：PyMuPDF 有文本就抽文本；扫描件渲染前几页交给 OCR/VLM；pypdf 只做文本兜底。"""
    try:
        import fitz  # type: ignore
    except Exception:  # noqa: BLE001
        fitz = None
    if fitz is not None:
        texts = []
        images = []
        try:
            with fitz.open(str(path)) as doc:
                for i in range(min(len(doc), MAX_PDF_PAGES)):
                    page = doc.load_page(i)
                    t = str(page.get_text("text") or "").strip()
                    if t:
                        texts.append(t)
                    elif i < 3:
                        try:
                            from PIL import Image
                            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                            images.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("PDF 解析失败：%s" % e)
        return _clip("\n".join(texts)), images
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("缺少 PDF 解析依赖（PyMuPDF / pypdf）")
    reader = PdfReader(str(path))
    texts = []
    for i in range(min(len(reader.pages), MAX_PDF_PAGES)):
        try:
            texts.append(str(reader.pages[i].extract_text() or ""))
        except Exception:  # noqa: BLE001
            pass
    return _clip("\n".join(texts)), []


def _read_xlsx(path):
    try:
        import openpyxl  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("缺少 xlsx 解析依赖 openpyxl")
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    texts = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if vals:
                    texts.append(" | ".join(vals))
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return _clip("\n".join(texts))


def _wait_vlm(url, seconds):
    if not url or seconds <= 0:
        return False
    import urllib.request
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
    return False


def _image_text(img, args, ocr_engine):
    out = {"ocr": "", "vlm": ""}
    if azure_peek is None:
        out["error"] = "azure_peek 导入失败"
        return out
    if azure_peek.OCR_DIR.exists():
        try:
            out["ocr"] = azure_peek.run_ocr(ocr_engine, img)
        except Exception as e:  # noqa: BLE001
            out["ocr_error"] = repr(e)
    if args.vlm_url:
        try:
            out["vlm"] = azure_peek.run_vlm_api(
                img, args.max_side, args.vlm_url, timeout=args.vlm_timeout,
                prompt=FILE_VLM_PROMPT, max_tokens=500, system=FILE_VLM_SYSTEM)
        except Exception as e:  # noqa: BLE001
            out["vlm_error"] = repr(e)
            # 只有本地权重存在时才回退 CPU，避免每条都卡在导入/加载
            if azure_peek.VLM_GGUF.exists():
                try:
                    out["vlm"] = azure_peek.run_vlm_cpu(
                        img, args.max_side, prompt=FILE_VLM_PROMPT, max_tokens=500)
                except Exception as e2:  # noqa: BLE001
                    out["vlm_error"] = repr(e2)
    return out


def _process(path, args, ocr_engine):
    t0 = time.time()
    ext = path.suffix.lower()
    item = {"name": path.name, "ext": ext, "kind": "", "text": "", "ocr": "", "vlm": ""}
    if ext not in SUPPORTED:
        item["error"] = "不支持的文件类型"
        return item
    if path.stat().st_size > MAX_FILE_BYTES:
        item["error"] = "文件超过 25MB，已跳过"
        return item
    try:
        if ext in TEXT_EXTS:
            item["kind"] = "text"
            item["text"] = _read_text_file(path)
        elif ext == ".docx":
            item["kind"] = "docx"
            item["text"] = _read_docx(path)
        elif ext == ".pptx":
            item["kind"] = "pptx"
            item["text"] = _read_pptx(path)
        elif ext == ".xlsx":
            item["kind"] = "xlsx"
            item["text"] = _read_xlsx(path)
        elif ext == ".pdf":
            item["kind"] = "pdf"
            text, images = _read_pdf(path)
            item["text"] = text
            parts = []
            for img in images:
                got = _image_text(img, args, ocr_engine)
                if got.get("ocr"):
                    parts.append("OCR：%s" % got["ocr"])
                if got.get("vlm"):
                    parts.append("VLM：%s" % got["vlm"])
            if parts:
                item["text"] = _clip("\n".join([item["text"]] + parts))
        elif ext in IMAGE_EXTS:
            item["kind"] = "image"
            from PIL import Image
            img = Image.open(path)
            try:
                img.seek(0)
            except Exception:  # noqa: BLE001
                pass
            got = _image_text(img.convert("RGB"), args, ocr_engine)
            item["ocr"] = got.get("ocr", "")
            item["vlm"] = got.get("vlm", "")
            if got.get("error"):
                item["error"] = got["error"]
            if got.get("ocr_error"):
                item["ocr_error"] = got["ocr_error"]
            if got.get("vlm_error"):
                item["vlm_error"] = got["vlm_error"]
    except Exception as e:  # noqa: BLE001
        item["error"] = repr(e)
        _log("[files] %s 失败：%r" % (path.name, e))
        traceback.print_exc()
    item["ms"] = int((time.time() - t0) * 1000)
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--vlm-url", default="")
    ap.add_argument("--vlm-wait", type=float, default=60.0)
    ap.add_argument("--vlm-timeout", type=float, default=120.0)
    ap.add_argument("--max-side", type=int, default=1280)
    ap.add_argument("--max-files", type=int, default=16)
    args = ap.parse_args()
    if azure_peek is not None:
        try:
            azure_peek._hide_console()
        except Exception:  # noqa: BLE001
            pass
    t0 = time.time()
    inbox = Path(args.inbox)
    result = {"ok": True, "inbox": str(inbox), "files": [], "ms": {}}
    logf = open(str(args.out) + ".log", "w", encoding="utf-8")

    def log(msg):
        line = "[%.1fs] %s" % (time.time() - t0, msg)
        _log(line)
        try:
            logf.write(line + "\n")
            logf.flush()
        except Exception:  # noqa: BLE001
            pass

    try:
        if not inbox.is_dir():
            raise RuntimeError("资料箱不存在：%s" % inbox)
        files = []
        for p in sorted(inbox.iterdir(), key=lambda x: (x.stat().st_mtime, x.name)):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() not in SUPPORTED:
                continue
            files.append(p)
        files = files[:max(0, args.max_files)]
        log("待处理 %d 个文件" % len(files))
        ocr_engine = None
        if azure_peek is not None and azure_peek.OCR_DIR.exists() and any(
                p.suffix.lower() in IMAGE_EXTS | {".pdf"} for p in files):
            try:
                log("加载 OCR…")
                ocr_engine = azure_peek.load_ocr()
                log("OCR 就绪")
            except Exception as e:  # noqa: BLE001
                log("OCR 加载失败（继续用文档文本 / VLM）：%r" % e)
        if args.vlm_url:
            ready = _wait_vlm(args.vlm_url, args.vlm_wait)
            log("VLM 服务%s：%s" % ("就绪" if ready else "未就绪", args.vlm_url))
            if not ready:
                # 没就绪也继续：图片可能仍有 OCR / 文档文本，不整批失败
                pass
        for i, p in enumerate(files, 1):
            log("(%d/%d) %s" % (i, len(files), p.name))
            result["files"].append(_process(p, args, ocr_engine))
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["error"] = repr(e)
        log("整体失败：%r" % e)
        traceback.print_exc()
    finally:
        result["ms"]["total"] = int((time.time() - t0) * 1000)
        tmp = str(args.out) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, args.out)
        log("完成：%d 个文件" % len(result.get("files", [])))
        logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
