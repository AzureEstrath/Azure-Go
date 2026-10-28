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
import datetime
import html
import io
import json
import math
import os
import re
import statistics
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
    "请把这张资料图片里的文字从上到下、从左到右逐行读出来，保留标题、日期、时间、课程名、"
    "教师、地点与备注；课程表要尽量读出每个格子，不要只读一格，也不要总结成一两句。"
    "没有文字就回答「未发现文字」。不要编造，不要描述图片风格或人物。用简体中文。"
)
FILE_VLM_SYSTEM = "你是资料整理助手，只提取图片中真实存在的文字与日程信息，始终用简体中文回答。"

# —— 课程表 / 周课表还原（纯坐标启发式，不依赖 VLM）——
_DATE_RE = re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})$")
_TIME_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{2})$")
_WEEKDAY_RE = re.compile(r"^周[一二三四五六日天]$")
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 教师名一般 2-3 个汉字；4 个汉字的「工程图学 / 高等数学」是课程名，不能当人名。
_CJK_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,3}$")


def _mid(v):
    try:
        return float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _median(values, default=0.0):
    vals = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return float(statistics.median(vals)) if vals else float(default)


def _mmdd_date(text, ref):
    """``10-26`` → 最接近 ``ref`` 的真实日期（跨年时自动选去年 / 明年）；失败返回 ``None``。"""
    m = _DATE_RE.match(str(text or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    best = None
    for delta in (0, -1, 1):
        try:
            d = datetime.date(ref.year + delta, month, day)
        except ValueError:
            continue
        score = abs((d - ref).days)
        if best is None or score < best[0]:
            best = (score, d)
    if best is None:
        return None
    # 课表通常就在这两天附近；离得太远（>200 天）说明不是本学期的表，仍按最近年取。
    return best[1]


def _join_lines(lines):
    """把同一单元格里被换行切开的文本拼回去（中文直接相连，英文/数字之间留一个空格）。"""
    out = ""
    for line in lines:
        line = str(line or "").strip()
        if not line:
            continue
        if out == "":
            out = line
        elif re.match(r"^[\x00-\x7f]", out[-1]) and re.match(r"^[\x00-\x7f]", line[0]):
            out += " " + line
        else:
            out += line
    return out


def _cell_parts(lines):
    """一个课程单元格的若干行 → (title, teacher, location, notes)。

    顺序就是 OCR 的从上到下：课程名（可能换行）→ 教师 → ``@地点``（可能换行，含教室号）→ 周次。
    """
    lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if not lines:
        return "", "", "", ""
    loc_idx = None
    for i, line in enumerate(lines):
        if line.startswith("@") or line.startswith("＠"):
            loc_idx = i
            break
    if loc_idx is None:
        loc_idx = len(lines)
        head = lines
        tail = []
    else:
        head = lines[:loc_idx]
        tail = lines[loc_idx:]
    teacher = ""
    # 只有「课程名 + 人名」两行以上时，最后一行才当教师；单独一行 2-3 字仍是课程名。
    if len(head) >= 2 and _CJK_NAME_RE.match(head[-1]):
        teacher = head[-1]
        head = head[:-1]
    title = _join_lines(head).strip()
    location = ""
    notes = ""
    if tail:
        loc = "".join(tail).lstrip("@＠").strip()
        # 末尾单独的一两位数字通常是「第几周」，三位以上是教室号（如 210）。
        m = re.search(r"(?:^|\s)(\d{1,2})$", loc)
        if m and len(loc) > len(m.group(1)):
            notes = "第%s周" % m.group(1)
            loc = loc[:m.start(1)].strip()
        location = loc
    return title, teacher, location, notes


def timetable_from_ocr(items, ref=None):
    """OCR 文本框 → 周课表条目（星期 / 节次 / 课程 / 教师 / 地点）。

    只做**几何还原**：表头日期当列、左侧时间当行、单元格文本按最近行列归位。
    识别不出表头（少于 3 个日期框）时返回 ``[]``，调用方继续走普通文本提取。
    """
    ref = ref or datetime.date.today()
    if not isinstance(items, list) or len(items) < 8:
        return []
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "") or "").strip()
        if not text:
            continue
        x, y = _mid(it.get("x")), _mid(it.get("y"))
        if x <= 0 and y <= 0:
            continue                    # 没有坐标（PaddleOCR 兜底路径）：无法还原表格
        clean.append({"text": text, "x": x, "y": y,
                      "x0": _mid(it.get("x0")), "x1": _mid(it.get("x1")),
                      "y0": _mid(it.get("y0")), "y1": _mid(it.get("y1")),
                      "score": _mid(it.get("score"))})
    dates = [it for it in clean if _DATE_RE.match(it["text"])]
    # 去掉重复日期框（同一列可能被识别两次），保留 x 最小/最完整的那一个。
    uniq = {}
    for it in dates:
        uniq.setdefault(it["text"], it)
    dates = sorted(uniq.values(), key=lambda d: d["x"])
    if len(dates) < 3:
        return []
    col_x = [d["x"] for d in dates]
    gaps = [col_x[i + 1] - col_x[i] for i in range(len(col_x) - 1)]
    col_w = _median(gaps, 100.0)
    if col_w <= 0:
        return []
    header_y = max(d["y1"] for d in dates)
    left_cut = col_x[0] - col_w * 0.5

    times = [it for it in clean
             if _TIME_RE.match(it["text"]) and it["x"] < left_cut and it["y"] > header_y]
    # 按 y 去重（同一条时间偶尔被识别成两框）。
    times.sort(key=lambda d: d["y"])
    dedup = []
    for it in times:
        if dedup and abs(it["y"] - dedup[-1]["y"]) < 8:
            continue
        dedup.append(it)
    times = dedup
    if len(times) < 2:
        return []
    periods = []
    i = 0
    while i < len(times):
        a = times[i]
        b = times[i + 1] if i + 1 < len(times) and times[i + 1]["y"] - a["y"] < 40 else None
        periods.append({
            "period": len(periods) + 1,
            "start": a["text"].replace("：", ":"),
            "end": b["text"].replace("：", ":") if b else "",
            "y": a["y"], "y1": b["y"] if b else a["y"],
        })
        i += 2 if b else 1
    if not periods:
        return []
    grid_top = header_y + 8
    grid_bottom = periods[-1]["y1"] + 80

    body = []
    for it in clean:
        if it["y"] <= grid_top or it["y"] >= grid_bottom:
            continue
        if it["x"] < left_cut:
            continue                    # 左侧节次 / 时间列
        if _DATE_RE.match(it["text"]) or _WEEKDAY_RE.match(it["text"]):
            continue
        if _TIME_RE.match(it["text"]):
            continue
        if re.match(r"^(第\d+周|全部|10月)$", it["text"]):
            continue
        col = min(range(len(col_x)), key=lambda k: abs(col_x[k] - it["x"]))
        if abs(col_x[col] - it["x"]) > col_w * 0.62:
            continue
        it = dict(it)
        it["col"] = col
        body.append(it)

    out = []
    for col in range(len(col_x)):
        cell = sorted([it for it in body if it["col"] == col], key=lambda d: d["y"])
        if not cell:
            continue
        groups = []
        for it in cell:
            # 同一格内「课程名 → 教师 → 地点」的行距约 25-50px；不同课程块之间至少隔 120px。
            if groups and it["y"] - groups[-1][-1]["y"] <= 80:
                groups[-1].append(it)
            else:
                groups.append([it])
        for gi, group in enumerate(groups):
            y0 = min(it["y0"] for it in group)
            y1 = max(it["y1"] for it in group)
            start = None
            for p in periods:
                if p["y"] >= y0 - 25:
                    start = p["period"]
                    break
            if start is None:
                continue
            next_start = None
            if gi + 1 < len(groups):
                ny = groups[gi + 1][0]["y"]
                for p in periods:
                    if p["y"] >= ny - 25:
                        next_start = p["period"]
                        break
            room = (next_start - start) if next_start is not None else 99
            span = 2 if room >= 2 else 1
            if room >= 3 and (y1 - y0) > 140:
                span = 3
            span = max(1, min(span, 4, len(periods) - start + 1))
            end = start + span - 1
            p0 = periods[start - 1]
            p1 = periods[end - 1]
            title, teacher, location, notes = _cell_parts([it["text"] for it in group])
            if title == "":
                continue
            date_text = ""
            weekday = -1
            d = _mmdd_date(dates[col]["text"], ref)
            if d is not None:
                date_text = d.isoformat()
                weekday = d.weekday()
            out.append({
                "kind": "course", "title": title, "teacher": teacher, "location": location,
                "notes": notes, "date": date_text, "weekday": weekday,
                "period": ("%d-%d" % (start, end)) if end > start else str(start),
                "period_start": start, "period_end": end,
                "start_time": p0["start"], "end_time": p1["end"],
                "source": "",
            })
    return out


def timetable_text(courses):
    """还原出的课程 → 给 LLM / 日志看的紧凑文本。"""
    if not courses:
        return ""
    lines = ["课程表（已按行列还原）："]
    for it in courses:
        wd = it.get("weekday", -1)
        name = _WEEKDAY_NAMES[wd] if isinstance(wd, int) and 0 <= wd <= 6 else "周?"
        bits = [name]
        if it.get("date"):
            bits.append(str(it["date"])[5:])
        bits.append("第%s节" % it.get("period", ""))
        times = " ".join(x for x in (it.get("start_time", ""), it.get("end_time", "")) if x)
        if times:
            bits.append(times.replace(" ", "-"))
        bits.append(str(it.get("title", "")))
        if it.get("teacher"):
            bits.append("教师：%s" % it["teacher"])
        if it.get("location"):
            bits.append("地点：%s" % it["location"])
        lines.append(" ".join(bits))
    return "\n".join(lines)


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
    """图片 → ``{"ocr","vlm","table"}``；课程表会额外给出按行列还原的 ``table``。"""
    out = {"ocr": "", "vlm": "", "table": []}
    if azure_peek is None:
        out["error"] = "azure_peek 导入失败"
        return out
    if azure_peek.OCR_DIR.exists():
        try:
            # 资料图要完整读，不能沿用窥屏的 400 字截断；长边放到 2600 保小字。
            items = azure_peek.run_ocr_items(ocr_engine, img, max_side=2600)
            items_sorted = sorted(items, key=lambda d: (round(_mid(d.get("y")) / 12.0), _mid(d.get("x"))))
            out["ocr"] = " | ".join(str(it.get("text", "")).strip()
                                    for it in items_sorted if str(it.get("text", "")).strip())[:MAX_TEXT_CHARS]
            out["table"] = timetable_from_ocr(items)
        except Exception as e:  # noqa: BLE001
            out["ocr_error"] = repr(e)
    # 已经还原出课程表时不再调 VLM：小 VLM 往往只读到一格，反而把噪声带进提取。
    if args.vlm_url and not out["table"]:
        try:
            out["vlm"] = azure_peek.run_vlm_api(
                img, max(1280, int(args.max_side)), args.vlm_url, timeout=args.vlm_timeout,
                prompt=FILE_VLM_PROMPT, max_tokens=900, system=FILE_VLM_SYSTEM)
        except Exception as e:  # noqa: BLE001
            out["vlm_error"] = repr(e)
            # 只有本地权重存在时才回退 CPU，避免每条都卡在导入/加载
            if azure_peek.VLM_GGUF.exists():
                try:
                    out["vlm"] = azure_peek.run_vlm_cpu(
                        img, max(1280, int(args.max_side)), prompt=FILE_VLM_PROMPT, max_tokens=900)
                except Exception as e2:  # noqa: BLE001
                    out["vlm_error"] = repr(e2)
    return out


def _process(path, args, ocr_engine):
    t0 = time.time()
    ext = path.suffix.lower()
    item = {"name": path.name, "ext": ext, "kind": "", "text": "", "ocr": "", "vlm": "", "table": []}
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
                item["table"].extend(got.get("table", []) or [])
            if item["table"]:
                parts.append(timetable_text(item["table"]))
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
            item["table"] = got.get("table", []) or []
            if item["table"]:
                # 还原文本也塞进 text 字段：即使调用方不认 table，LLM 仍能按行列读到课程。
                item["text"] = _clip(timetable_text(item["table"]))
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
