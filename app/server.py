"""FinAgent 桌面应用服务端（纯标准库实现，不引入任何第三方依赖）。

设计原则：本模块只是 `run.py` 的外壳。

    * 所有指标计算、规则判定、归因结论仍由 finagent 核心产生，界面不做任何计算；
    * 所有执行仍通过 `run.py` 命令行发起，因此全链路轨迹
      （`output/traces/*.jsonl`）完整保留，可核验、可追溯、可复现；
    * 服务端只做四件事：收文件、启动子进程、转发日志、读取产物渲染。

网络边界：服务**硬编码监听 127.0.0.1**，只在本机可访问。
这是有意的设计——每位使用者都在自己的机器上独立运行，
不需要处于同一局域网，也不需要任何人保持开机。
因此本模块不提供 `--host` 参数，也不存在"共享给队友"的分支：
一旦能改监听地址，就会引入"谁的机器在跑、数据传到哪去了"的不可控因素，
与赛题"封闭数据环境"的要求直接冲突。

用法：
    python app/server.py                  # 启动并自动打开浏览器
    python app/server.py --port 8899      # 指定端口
    python app/server.py --no-browser     # 不自动打开浏览器
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
sys.path.insert(0, ROOT)
# 让 pdfexport 无论从哪个工作目录启动都能被导入。
sys.path.insert(0, APP_DIR)

# 必须置于 sys.path 注入之后：否则在任意工作目录启动都会找不到 finagent 包。
from finagent.config import local_config_path  # noqa: E402

# 服务只监听回环地址。写死为常量而不是命令行参数，见模块文档的说明。
HOST = "127.0.0.1"

# 唯一允许的推理服务地址与模型清单。国产模型（DeepSeek）作为核心推理引擎。
BASE_URL = "https://api.deepseek.com/v1"
MODELS = ("deepseek-chat", "deepseek-reasoner")

# 允许通过接口读取的目录白名单（相对项目根）。防止任意路径读取。
ALLOWED_ROOTS = ("output", "data", "docs", "finagent")

# 单次上传的文件上限（一份年报通常 5~20MB；留足余量但不放任）。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_REQUEST_BYTES = 400 * 1024 * 1024

# 同一时刻只允许一次运行：两次分析同时写 output/ 会互相覆盖产物。
# 这里做非阻塞占用，冲突时明确告知而不是静默排队。
RUN_LOCK = threading.Lock()

# 当前上传批次。一次"上传 → 分析"对应一个 run_id 目录：
#   data/uploads/<run_id>/
# 用户清空重传时只切换会话指针，不删除已上传的原始文件——
# 原始文件是结论的唯一出处，删掉它，报告里的页码就再也无法核对。
SESSION_LOCK = threading.Lock()
SESSION: dict = {"run_id": None, "set": None}

# ------------------------------------------------------------------ 工具

def safe_path(rel):
    """把相对路径解析为绝对路径，且必须落在白名单目录内，否则返回 None。"""
    if not rel:
        return None
    rel = str(rel).replace("\\", "/").lstrip("/")
    cand = os.path.abspath(os.path.join(ROOT, rel))
    for root in ALLOWED_ROOTS:
        base = os.path.abspath(os.path.join(ROOT, root))
        if cand == base or cand.startswith(base + os.sep):
            return cand
    return None


def read_text(path, limit=8000000):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)


def load_config():
    from finagent.config import load_config as _load
    return _load(os.path.join(ROOT, "config.yaml"))


def _mask_key(value):
    """密钥打码：只保留首尾各 3 位，中间以星号替代。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:3] + "*" * (len(text) - 6) + text[-3:]


def find_table_for(report_path):
    """找到与报告对应的指标宽表 CSV。"""
    stem = os.path.basename(report_path)[:-3]
    table_dir = os.path.join(ROOT, "output", "tables")
    if not os.path.isdir(table_dir):
        return None
    if os.path.isfile(os.path.join(table_dir, stem + ".csv")):
        return os.path.join(table_dir, stem + ".csv")
    head = stem.split("_")[0]
    hits = [f for f in os.listdir(table_dir)
            if f.startswith(head + "_") and f.endswith(".csv")]
    if hits:
        return os.path.join(table_dir, sorted(hits)[-1])
    return None


def list_reports(cfg):
    report_dir = os.path.join(ROOT, cfg["output"]["report_dir"])
    out = []
    if not os.path.isdir(report_dir):
        return out
    for name in os.listdir(report_dir):
        if not name.endswith(".md"):
            continue
        full = os.path.join(report_dir, name)
        table = find_table_for(full)
        out.append({"name": name, "path": os.path.relpath(full, ROOT).replace("\\", "/"),
                    "mtime": os.path.getmtime(full), "bytes": os.path.getsize(full),
                    "table": os.path.relpath(table, ROOT).replace("\\", "/") if table else None})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def list_traces(cfg, limit=60):
    trace_dir = os.path.join(ROOT, cfg["output"]["trace_dir"])
    out = []
    if not os.path.isdir(trace_dir):
        return out
    for name in os.listdir(trace_dir):
        if not name.endswith(".jsonl"):
            continue
        full = os.path.join(trace_dir, name)
        out.append({"run": name[:-6], "mtime": os.path.getmtime(full),
                    "bytes": os.path.getsize(full)})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out[:limit]


# ------------------------------------------------------------------ 上传会话

def session_uploads(create: bool = False):
    """取当前上传批次。create=True 时若不存在则新建一个 run_id 目录。"""
    from finagent.ingest.dataset import UploadSet
    with SESSION_LOCK:
        if SESSION["set"] is None and create:
            from finagent.config import uploads_dir
            SESSION["set"] = UploadSet(root=uploads_dir(load_config()))
            SESSION["run_id"] = SESSION["set"].run_id
        return SESSION["set"]


def reset_session():
    """结束当前批次。已上传的文件保留在磁盘上，只是不再属于"当前这批"。"""
    with SESSION_LOCK:
        old = SESSION.get("run_id")
        SESSION["set"] = None
        SESSION["run_id"] = None
        return old


def entry_view(entry: dict) -> dict:
    """把一条上传结果整理成界面要的字段，并给出识别置信度。"""
    subject = entry.get("subject") or {}
    warnings = list(entry.get("warnings") or [])
    code = subject.get("code")
    date = subject.get("report_date")
    kind = subject.get("report_kind")
    issues = list(warnings)
    if not code:
        issues.append("未解析出股票代码（将按公司名称归组）")
    if not date:
        issues.append("未解析出报告期，该文件无法参与同比/环比计算")
    if not kind:
        issues.append("未解析出报告类型")
    level = "error" if (not date or not kind) else ("warn" if issues else "ok")
    verification = entry.get("verification") or {}
    return {
        "upload_id": entry.get("upload_id"),
        "filename": entry.get("filename"),
        "bytes": entry.get("size"),
        "pages": entry.get("pages"),
        "sha256": (entry.get("sha256") or "")[:16],
        "subject": subject.get("display") or entry.get("filename"),
        "short_name": subject.get("short_name"),
        "full_name": subject.get("full_name"),
        "code": code,
        "report_kind": kind,
        "report_kind_label": subject.get("report_kind_label"),
        "report_date": date,
        "compare_date": subject.get("compare_date"),
        "confidence": level,
        "issues": issues,
        "counts": entry.get("counts") or {},
        "fields_total": sum((entry.get("counts") or {}).values()),
        "checks_passed": verification.get("passed"),
        "checks_failed": verification.get("failed"),
        "suspect": sorted((verification.get("suspect") or {}).keys()),
        "stored_path": os.path.relpath(entry["stored_path"], ROOT).replace("\\", "/")
        if entry.get("stored_path") else None,
    }


def pending_payload() -> dict:
    uploads = session_uploads()
    if uploads is None:
        return {"run_id": None, "items": [], "groups": [], "notes": []}
    items = [entry_view(e) for e in uploads.entries]
    groups = []
    for key, members in uploads.groups().items():
        dates = sorted({(m.get("subject") or {}).get("report_date") or "" for m in members})
        groups.append({
            "key": str(key),
            "display": (members[0].get("subject") or {}).get("display") or str(key),
            "documents": len(members),
            "report_dates": [d for d in dates if d],
            "periods": len([d for d in dates if d]),
        })
    return {"run_id": uploads.run_id, "items": items, "groups": groups,
            "notes": _group_notes(uploads)}


def _group_notes(uploads) -> list:
    from finagent.ingest.group import notes as group_notes
    out = []
    for key, members in uploads.groups().items():
        for note in group_notes(members):
            out.append(f"{key}：{note}")
    return out


def status_payload():
    cfg = load_config()
    return {
        "python": sys.version.split()[0],
        "root": ROOT,
        "host": HOST,
        "base_url": BASE_URL,
        "models": list(MODELS),
        "model": cfg["llm"]["model"] if cfg["llm"]["model"] in MODELS else MODELS[0],
        "api_key_set": bool(cfg["llm"]["api_key"]),
        "max_steps": cfg["agent"]["max_steps"],
        "busy": RUN_LOCK.locked(),
        "pending": pending_payload(),
        "reports": list_reports(cfg),
        "traces": list_traces(cfg),
        "thresholds": cfg.get("thresholds", {}),
    }


# ------------------------------------------------------------------ 命令行构造

def build_command(action, params):
    """把界面参数翻译成 run.py 命令行。参数在此集中校验，避免注入。"""
    cmd = [sys.executable, "-u", os.path.join(ROOT, "run.py")]

    if action == "analyze":
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            uploads = session_uploads()
            run_id = uploads.run_id if uploads else ""
        if not re.match(r"^[0-9A-Za-z][0-9A-Za-z\-]{5,63}$", run_id):
            raise ValueError("没有可分析的财报材料，请先上传财报 PDF")
        cmd += ["analyze", "--run", run_id]
        since = params.get("since_year")
        if since:
            cmd += ["--since-year", str(int(since))]
        periods = params.get("periods")
        if periods:
            periods = int(periods)
            if not 1 <= periods <= 20:
                raise ValueError("报告期数需在 1~20 之间")
            cmd += ["--periods", str(periods)]
        if params.get("quiet"):
            cmd += ["--quiet"]
        # 用户在界面上直接敲的问题，原样交给智能体作为定向追问。
        # 只做长度与换行校验：内容本身不解释、不改写，保持"用户问什么就问什么"。
        question = str(params.get("question") or "").strip()
        if question:
            if len(question) > 500:
                raise ValueError("追问内容请控制在 500 字以内")
            if "\x00" in question:
                raise ValueError("追问内容包含非法字符")
            cmd += ["--question", question]
        return cmd

    raise ValueError("不支持的动作：" + str(action))


def snapshot_reports():
    """记录现有报告的时间戳，用于运行后判断新增了哪些产物。"""
    cfg = load_config()
    report_dir = os.path.join(ROOT, cfg["output"]["report_dir"])
    snap = {}
    if os.path.isdir(report_dir):
        for name in os.listdir(report_dir):
            full = os.path.join(report_dir, name)
            if os.path.isfile(full):
                snap[name] = os.path.getmtime(full)
    return snap


class TraceTail:
    """运行期间实时跟随新增的轨迹文件，用于在界面上同步展示工具调用。

    只读操作：不改动轨迹内容，因此不影响「可追溯、可复现」的要求。
    """

    def __init__(self, trace_dir, existing):
        self.dir = trace_dir
        self.existing = set(existing)
        self.path = None
        self.offset = 0

    def _locate(self):
        if self.path or not os.path.isdir(self.dir):
            return
        fresh = [f for f in os.listdir(self.dir)
                 if f.endswith(".jsonl") and f not in self.existing]
        if not fresh:
            return
        fresh.sort(key=lambda f: os.path.getmtime(os.path.join(self.dir, f)))
        self.path = os.path.join(self.dir, fresh[-1])

    @property
    def run_id(self):
        return os.path.basename(self.path)[:-6] if self.path else None

    def poll(self):
        """返回自上次调用以来新增的轨迹事件（按 seq 顺序）。"""
        self._locate()
        if not self.path or not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return []
        if not data:
            return []
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        self.offset += cut + 1
        text = data[:cut + 1].decode("utf-8", "replace")
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def parse_multipart(body: bytes, boundary: str) -> list:
    """极简 multipart/form-data 解析。

    存在的意义：不引入任何第三方 Web 框架或解析库，
    保持"评审环境零配置即可复现"这一前提。只处理本项目需要的字段：
    一个文件名 + 一段二进制内容，其余一律忽略。
    """
    delim = b"--" + boundary.encode("latin-1")
    out = []
    for part in body.split(delim)[1:]:
        if part[:2] == b"--":
            break
        part = part.lstrip(b"\r\n")
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in head.split(b"\r\n"):
            key, _, value = line.partition(b":")
            headers[key.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
        disposition = headers.get("content-disposition", "")
        found = re.search(r'filename="([^"]*)"', disposition)
        out.append({"filename": _fix_filename(found.group(1) if found else ""),
                    "data": data})
    return out


def _fix_filename(name: str) -> str:
    """还原 multipart 里被按 latin-1 解出来的中文文件名。

    浏览器发送的文件名是 UTF-8 字节，而 HTTP 头按规定按 latin-1 解析，
    于是"牧原股份2026半年报.pdf"会变成"ç§å2026å¹´æ¥æ¥.pdf"。
    文件名会被写进上传目录、报告与证据链，乱码会让"这份数字来自哪个文件"
    这句话失去意义，因此这里必须还原。
    还原失败（文件名本来就是 latin-1）时原样返回：绝不因为名字读不出来而丢文件。
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FinAgentUI/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[app] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def send_error_json(self, message, code=400):
        self.send_json({"ok": False, "error": message}, code)

    # ---------------- 路由

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            return self.serve_static("index.html")
        if parsed.path.startswith("/static/"):
            return self.serve_static(parsed.path[len("/static/"):])
        if parsed.path == "/api/status":
            return self.send_json({"ok": True, "status": status_payload()})
        if parsed.path == "/api/pending":
            return self.send_json({"ok": True, **pending_payload()})
        if parsed.path == "/api/report":
            return self.api_report(query)
        if parsed.path == "/api/table":
            return self.api_table(query)
        if parsed.path == "/api/report.pdf":
            return self.api_report_pdf(query)
        if parsed.path == "/api/trace":
            return self.api_trace(query)
        if parsed.path == "/api/settings":
            return self.api_settings()
        return self.send_error_json("未知接口", 404)

    def serve_static(self, name):
        name = name.split("?")[0].lstrip("/")
        full = os.path.abspath(os.path.join(STATIC_DIR, name))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self.send_error_json("静态资源不存在", 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def api_report(self, query):
        path = safe_path((query.get("path") or [""])[0])
        if not path or not os.path.isfile(path) or not path.endswith(".md"):
            return self.send_error_json("报告不存在", 404)
        self.send_json({"ok": True, "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                        "markdown": read_text(path)})

    def api_report_pdf(self, query):
        """把一份 Markdown 报告导出为 PDF。

        浏览器可用时返回真正的 PDF；找不到浏览器时退化成"可直接打印的 HTML"，
        用户在浏览器里按 Ctrl+P 另存即可。无论哪条路径都不返回半成品文件。
        """
        path = safe_path((query.get("path") or [""])[0])
        if not path or not os.path.isfile(path) or not path.endswith(".md"):
            return self.send_error_json("报告不存在", 404)

        try:
            import pdfexport
        except Exception as exc:  # pragma: no cover - 仅在文件被破坏时发生
            return self.send_error_json("PDF 模块加载失败：" + str(exc), 500)

        markdown = read_text(path)
        title, body_md, meta = pdfexport.split_report(markdown)
        document = pdfexport.build_document(
            title,
            pdfexport.format_meta(meta) or ["<b>生成工具</b> FinAgent"],
            pdfexport.markdown_to_html(body_md),
            footer_note="<b>文件</b> %s　|　<b>导出时间</b> %s<br>"
                        % (os.path.basename(path), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        inline = (query.get("inline") or [""])[0] in ("1", "true", "yes")
        filename = os.path.basename(path)[:-3] + ".pdf"

        if pdfexport.find_browser() is None:
            # 无浏览器：给出打印页，并说明原因，避免用户以为"下载坏了"。
            return self._send(200, document.encode("utf-8"),
                              "text/html; charset=utf-8")

        data = pdfexport.html_to_pdf(document)
        if not data:
            return self.send_error_json("PDF 生成失败，请改用浏览器打印", 500)

        disposition = "inline" if inline else "attachment"
        ascii_name = re.sub(r"[^0-9A-Za-z._-]", "_", filename)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            '%s; filename="%s"; filename*=UTF-8\'\'%s'
            % (disposition, ascii_name, quote(filename)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def api_key_verify(self, params):
        """验证用户填写的密钥是否可用。

        只在内存里用一次，不落盘——密钥是用户自己的，服务端没有理由保存它。
        """
        api_key = str(params.get("api_key") or "").strip()
        if not api_key:
            return self.send_json({"ok": False, "error": "请先填写 API Key"})
        try:
            import requests
        except Exception:
            return self.send_json({"ok": False, "error": "服务端缺少 requests 依赖"})
        try:
            resp = requests.get(BASE_URL + "/models",
                                headers={"Authorization": "Bearer " + api_key},
                                timeout=20)
        except Exception as exc:
            return self.send_json({"ok": False, "error": "无法连接模型服务：" + str(exc)})
        if resp.status_code == 401:
            return self.send_json({"ok": False, "error": "密钥无效或已过期（401）"})
        if resp.status_code >= 400:
            return self.send_json({"ok": False,
                                   "error": "模型服务返回 %d：%s"
                                            % (resp.status_code, resp.text[:160])})
        try:
            models = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
        except Exception:
            models = []
        supported = [m for m in models if m in MODELS]
        return self.send_json({"ok": True, "models": supported or list(MODELS)})

    def api_table(self, query):
        path = safe_path((query.get("path") or [""])[0])
        if not path or not os.path.isfile(path) or not path.endswith(".csv"):
            return self.send_error_json("指标表不存在", 404)
        rows = list(csv.reader(io.StringIO(read_text(path))))
        if not rows:
            return self.send_json({"ok": True, "header": [], "rows": [], "total": 0})
        header, body = rows[0], rows[1:]
        limit = 400
        return self.send_json({"ok": True, "header": header, "rows": body[:limit],
                               "total": len(body), "truncated": len(body) > limit,
                               "file": os.path.basename(path)})

    def api_trace(self, query):
        run = (query.get("run") or [""])[0]
        if not re.match(r"^[0-9A-Za-z\-]+$", run):
            return self.send_error_json("运行编号不合法")
        cfg = load_config()
        path = os.path.join(ROOT, cfg["output"]["trace_dir"], run + ".jsonl")
        if not os.path.isfile(path):
            return self.send_error_json("轨迹不存在", 404)
        events = []
        for line in read_text(path).splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return self.send_json({"ok": True, "run": run, "events": events,
                               "file": os.path.relpath(path, ROOT).replace("\\", "/")})

    def api_settings(self):
        """读取推理引擎配置。

        密钥只写入本地 config.local.yaml（.gitignore 已排除），
        接口返回时始终打码，避免密钥经由浏览器或日志泄露。
        """
        cfg = load_config()
        self.send_json({"ok": True, "llm": {
            "base_url": BASE_URL,
            "models": list(MODELS),
            "model": cfg["llm"]["model"] if cfg["llm"]["model"] in MODELS else MODELS[0],
            "api_key_set": bool(cfg["llm"]["api_key"]),
            "api_key_hint": _mask_key(cfg["llm"]["api_key"]),
            "local_config": os.path.relpath(
                local_config_path(os.path.join(ROOT, "config.yaml")), ROOT).replace("\\", "/"),
        }})

    def save_settings(self, params):
        if RUN_LOCK.locked():
            return self.send_error_json("正在运行，请等本次运行结束后再保存配置", 409)
        path = local_config_path(os.path.join(ROOT, "config.yaml"))
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                data = {}
        llm = data.setdefault("llm", {})

        # 接口地址固定为 DeepSeek 官方地址：这是唯一允许联网的去处，
        # 允许用户改地址等于允许把财报正文发往任意服务器。
        llm["base_url"] = BASE_URL
        model = str(params.get("model") or "").strip()
        if model:
            if model not in MODELS:
                return self.send_error_json("只支持这两种模型：" + "、".join(MODELS))
            llm["model"] = model
        api_key = str(params.get("api_key") or "").strip()
        if api_key:
            llm["api_key"] = api_key

        try:
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            return self.send_error_json("写入失败：" + str(exc), 500)
        self.send_json({"ok": True, "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                        "api_key_set": bool(llm.get("api_key")),
                        "api_key_hint": _mask_key(llm.get("api_key"))})


    def api_upload(self):
        """接收用户上传的财报 PDF。

        每个文件都走 `UploadSet.add()`：落盘 → layout 抽取 → 装配 → 交叉校验。
        因此"上传完成"时识别结果与校验结果已经就绪，界面可以立刻显示
        "这是谁家的哪一期、抽到多少科目、有没有校验未过"。
        """
        ctype = self.headers.get("Content-Type") or ""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.send_error_json("没有收到文件内容")
        if length > MAX_REQUEST_BYTES:
            return self.send_error_json("单次上传过大，请分次上传", 413)
        try:
            body = self.rfile.read(length)
        except (BrokenPipeError, ConnectionResetError):
            return self.send_error_json("上传中断")

        parts = []
        if ctype.startswith("multipart/form-data"):
            found = re.search(r'boundary="?([^";,]+)"?', ctype)
            if not found:
                return self.send_error_json("multipart 缺少 boundary")
            for part in parse_multipart(body, found.group(1)):
                if part["data"]:
                    parts.append(part)
        else:
            name = _fix_filename(
                (parse_qs(urlparse(self.path).query).get("filename") or [""])[0])
            parts.append({"filename": name, "data": body})
        if not parts:
            return self.send_error_json("没有解析到文件内容")

        from finagent.ingest.dataset import UploadError
        uploads = session_uploads(create=True)
        incoming = os.path.join(uploads.dir, "_incoming")
        os.makedirs(incoming, exist_ok=True)

        added, rejected = [], []
        for part in parts:
            name = os.path.basename(part["filename"] or "upload.pdf")
            if not name.lower().endswith(".pdf"):
                rejected.append({"filename": name, "error": "只接受 PDF 文件"})
                continue
            if len(part["data"]) > MAX_UPLOAD_BYTES:
                rejected.append({"filename": name, "error": "单个文件超过 200MB"})
                continue
            tmp = os.path.join(incoming, uuid.uuid4().hex + ".pdf")
            with open(tmp, "wb") as fh:
                fh.write(part["data"])
            try:
                entry = uploads.add(tmp, filename=name)
                added.append(entry_view(entry))
            except (UploadError, ValueError) as exc:
                rejected.append({"filename": name, "error": str(exc)})
            except Exception as exc:  # 单个坏文件不应让整批上传失败
                rejected.append({"filename": name, "error": f"解析失败：{exc!r}"})
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        payload = pending_payload()
        return self.send_json({"ok": bool(added) or not rejected,
                               "added": added, "rejected": rejected, **payload})

    # ---------------- 执行动作（SSE 流式日志）

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            return self.api_upload()

        length = int(self.headers.get("Content-Length") or 0)
        if length > 1000000:
            return self.send_error_json("请求过大", 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_error_json("请求体不是合法 JSON")

        if parsed.path == "/api/settings":
            return self.save_settings(body)
        if parsed.path == "/api/key/verify":
            return self.api_key_verify(body)
        if parsed.path == "/api/upload/clear":
            return self.send_json({"ok": True, "cleared": reset_session()})
        if parsed.path != "/api/run":
            return self.send_error_json("未知接口", 404)

        params = body
        action = str(params.get("action", ""))

        # 没有密钥就不启动。这是硬拦截：让用户当场看见"缺什么"，
        # 而不是等一个跑了十几秒的报告告诉他"归因未启用"。
        cfg = load_config()
        if not (str(params.get("api_key") or "").strip() or cfg["llm"]["api_key"]):
            return self.send_error_json(
                "请先填写你自己的 DeepSeek API Key（界面右上角「设置」）", 400)

        current = session_uploads()
        if not (current and current.entries):
            return self.send_error_json("请先上传至少一份财报 PDF", 400)
        if not params.get("run_id"):
            params["run_id"] = current.run_id

        try:
            cmd = build_command(action, params)
        except ValueError as exc:
            return self.send_error_json(str(exc))

        if not RUN_LOCK.acquire(blocking=False):
            return self.send_error_json("已有一次运行正在进行，请等它结束后再试", 409)
        try:
            self._stream_run(action, cmd, params)
        finally:
            RUN_LOCK.release()

    def _stream_run(self, action, cmd, params=None):
        """执行一次动作，把子进程输出与轨迹事件以 SSE 推送给界面。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(kind, **payload):
            data = json.dumps({"kind": kind, **payload}, ensure_ascii=False, default=str)
            try:
                self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return False
            return True

        cfg = load_config()
        trace_dir = os.path.join(ROOT, cfg["output"]["trace_dir"])
        report_dir = os.path.join(ROOT, cfg["output"]["report_dir"])
        before = snapshot_reports()
        before_traces = os.listdir(trace_dir) if os.path.isdir(trace_dir) else []

        shown = " ".join(
            os.path.relpath(c, ROOT).replace("\\", "/") if c.startswith(ROOT) else c
            for c in cmd)
        emit("start", action=action, command=shown, cwd=ROOT)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # 界面传来的密钥只在本进程的环境变量里活一次，用完即弃，
        # 不写 config.local.yaml、不进日志、不随轨迹落盘。
        user_key = str((params or {}).get("api_key") or "").strip()
        if user_key:
            env["FINAGENT_API_KEY"] = user_key
            env["DEEPSEEK_API_KEY"] = user_key
            emit("notice", text="本次运行使用界面填写的 API Key（不落盘）")
        env["FINAGENT_BASE_URL"] = BASE_URL
        user_model = str((params or {}).get("model") or "").strip()
        if user_model:
            if user_model not in MODELS:
                emit("error", message="不支持的模型：" + user_model)
            else:
                env["FINAGENT_MODEL"] = user_model

        t0 = time.perf_counter()
        run_id = None
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            emit("error", message="无法启动子进程：" + str(exc))
            emit("done", code=-1, seconds=0, reports=[])
            return

        # 子进程输出与轨迹文件分别从两个来源汇入同一条 SSE 流。
        # stdout 走独立线程入队，主线程负责取队并轮询轨迹，避免并发写响应体。
        line_q = queue.Queue()

        def pump(stream):
            try:
                for raw in stream:
                    line_q.put(raw.rstrip("\r\n"))
            finally:
                line_q.put(None)

        threading.Thread(target=pump, args=(proc.stdout,), daemon=True).start()

        tail = TraceTail(trace_dir, before_traces)
        finished = False
        while not finished:
            try:
                line = line_q.get(timeout=0.3)
            except queue.Empty:
                line = ""
                idle = True
            else:
                idle = False
            if not idle:
                if line is None:
                    finished = True
                else:
                    found = re.search(r"运行编号：(\S+)", line)
                    if found:
                        run_id = found.group(1)
                    if not emit("line", text=line):
                        finished = True
            for event in tail.poll():
                emit("trace", event=event)

        proc.wait()
        seconds = round(time.perf_counter() - t0, 2)
        if run_id is None:
            run_id = tail.run_id

        new_reports = []
        if os.path.isdir(report_dir):
            for name in os.listdir(report_dir):
                full = os.path.join(report_dir, name)
                if not os.path.isfile(full):
                    continue
                if name not in before or os.path.getmtime(full) > before[name]:
                    new_reports.append({
                        "name": name,
                        "path": os.path.relpath(full, ROOT).replace("\\", "/"),
                        "mtime": os.path.getmtime(full),
                    })
        emit("done", code=proc.returncode, seconds=seconds,
             run_id=run_id, reports=new_reports)


# ------------------------------------------------------------------ 入口

def main(argv=None):
    parser = argparse.ArgumentParser(prog="FinAgent App", description="FinAgent 桌面应用")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器（也可用环境变量 FINAGENT_NO_BROWSER=1）")
    args = parser.parse_args(argv)

    try:
        httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 无法在 {HOST}:{args.port} 启动服务：{exc}")
        print("       端口可能已被占用，可换一个端口重试："
              f"python app\\server.py --port {args.port + 1}")
        return 1
    url = "http://%s:%d/" % (HOST, args.port)
    cfg = load_config()
    print("=" * 62)
    print("  FinAgent —— 上市公司财务报告分析智能体")
    print("=" * 62)
    print("  界面地址   " + url + "   （仅本机可访问）")
    print("  项目目录   " + ROOT)
    print("  解释器     " + sys.executable)
    print("  推理模型   %s @ %s" % (cfg["llm"]["model"], BASE_URL))
    print("  密钥状态   " + ("已配置" if cfg["llm"]["api_key"]
                             else "未配置（在界面右上角填入你自己的 DeepSeek API Key）"))
    print("  停止服务   在本窗口按 Ctrl+C")
    print("=" * 62)

    quiet_browser = args.no_browser or bool(os.environ.get("FINAGENT_NO_BROWSER"))
    if not quiet_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

