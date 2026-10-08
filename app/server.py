"""FinAgent 桌面应用服务端（纯标准库实现，不引入任何第三方依赖）。

设计原则：本模块只是 `run.py` 的外壳。

    * 所有指标计算、规则判定、归因结论仍由 finagent 核心产生，界面不做任何计算；
    * 所有执行仍通过 `run.py` 命令行发起，因此全链路轨迹
      （`output/traces/*.jsonl`）完整保留，可核验、可追溯、可复现；
    * 服务端只做三件事：启动子进程、转发日志、读取产物渲染。

这保证了「有界面」这件事不削弱竞赛要求的可复现性。

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
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
sys.path.insert(0, ROOT)

# 必须置于 sys.path 注入之后：否则在任意工作目录启动都会找不到 finagent 包。
from finagent.config import local_config_path  # noqa: E402

# 允许通过接口读取的目录白名单（相对项目根）。防止任意路径读取。
ALLOWED_ROOTS = ("output", "data", "docs", "finagent")

# 分析对象既可以是代码（600519 / 600519.SH），也可以是公司简称（贵州茅台）。
# 简称由 run.py 通过全市场名录解析，服务端只做字符层面的安全校验：
# 命令行以参数列表方式传递（不经 shell），此处仍拒绝空白与特殊字符。
CODES_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fa5.\-]{1,20}$")

# 覆盖度对照样本：每个上市地与报表族各取一例，用于一键验证"全市场可分析"。
COVERAGE_SAMPLES = [
    {"code": "600519.SH", "name": "贵州茅台", "segment": "沪市主板"},
    {"code": "000725.SZ", "name": "京东方A", "segment": "深市主板"},
    {"code": "300750.SZ", "name": "宁德时代", "segment": "创业板"},
    {"code": "688981.SH", "name": "中芯国际", "segment": "科创板"},
    {"code": "601398.SH", "name": "工商银行", "segment": "银行报表族"},
    {"code": "601318.SH", "name": "中国平安", "segment": "保险报表族"},
    {"code": "600030.SH", "name": "中信证券", "segment": "证券报表族"},
    {"code": "920002.BJ", "name": "万达轴承", "segment": "北交所"},
    {"code": "900948.SH", "name": "伊泰B股", "segment": "沪市B股"},
]

# 同一时刻只允许一次运行：局域网共享给队友时，两条 run.py 同时写
# output/ 会互相覆盖产物。这里做非阻塞占用，冲突时明确告知而不是静默排队。
RUN_LOCK = threading.Lock()


def local_addresses():
    """本机可被局域网访问的 IPv4 地址，默认出口网卡的地址排在首位。

    排除回环与 169.254.*（链路本地地址，无法跨机访问）。
    虚拟网卡（VPN、虚拟机网桥等）的地址无法可靠识别，因此一并列出，
    但排在默认出口网卡之后，避免队友连错地址。
    """
    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # 不实际发包，只为取默认出口网卡
            primary = s.getsockname()[0]
    except OSError:
        pass

    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass

    def usable(addr):
        return not (addr.startswith("127.") or addr.startswith("169.254."))

    ordered = []
    if primary and usable(primary):
        ordered.append(primary)
    for addr in sorted(found):
        if usable(addr) and addr not in ordered:
            ordered.append(addr)
    return ordered


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


def list_corpus(cfg):
    """已落盘的公告原文（封闭数据环境的实际内容）。"""
    corpus_dir = os.path.join(ROOT, cfg["data"]["corpus_dir"])
    out = []
    if not os.path.isdir(corpus_dir):
        return out
    for code in sorted(os.listdir(corpus_dir)):
        mpath = os.path.join(corpus_dir, code, "manifest.json")
        if not os.path.isfile(mpath):
            continue
        try:
            manifest = json.loads(read_text(mpath))
        except Exception:
            manifest = []
        total = 0
        for entry in manifest:
            pdf = entry.get("path")
            if pdf and os.path.isfile(pdf):
                total += os.path.getsize(pdf)
        out.append({"code": code, "documents": len(manifest), "bytes": total,
                    "kinds": sorted({e.get("kind", "") for e in manifest})})
    return out


def find_table_for(report_path):
    """找到与报告对应的指标宽表 CSV。"""
    stem = os.path.basename(report_path)[:-3]
    table_dir = os.path.join(ROOT, "output", "tables")
    if not os.path.isdir(table_dir):
        return None
    if os.path.isfile(os.path.join(table_dir, stem + ".csv")):
        return os.path.join(table_dir, stem + ".csv")
    code = stem.split("_")[0]
    hits = [f for f in os.listdir(table_dir) if f.startswith(code + "_") and f.endswith(".csv")]
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


_UNIVERSE_CACHE = {"total": 0, "loaded": False}


def _mask_key(value):
    """密钥打码：只保留首尾各 3 位，中间以星号替代。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:3] + "*" * (len(text) - 6) + text[-3:]


def universe_total():
    """全市场名录条数。用于向评审直观展示覆盖范围（沪深北及 B 股）。"""
    if _UNIVERSE_CACHE["loaded"]:
        return _UNIVERSE_CACHE["total"]
    try:
        from finagent.datasource.universe import load_universe
        total = len(load_universe())
    except Exception:
        total = 0
    _UNIVERSE_CACHE.update(total=total, loaded=bool(total))
    return total


def search_universe(keyword, limit=12):
    """在工作线程中检索名录，返回供界面下拉框直接使用的结果。"""
    try:
        from finagent.datasource.universe import search
        hits = search(keyword, limit=limit)
    except Exception:
        return []
    return [{"code": h["code"], "secucode": h["secucode"], "name": h["name"],
             "market": h["market"], "org_id": h.get("org_id")} for h in hits]


def status_payload():
    cfg = load_config()
    chunks = os.path.join(ROOT, cfg["data"]["index_dir"], "chunks.jsonl")
    index_chunks = 0
    if os.path.isfile(chunks):
        with open(chunks, "r", encoding="utf-8", errors="replace") as fh:
            for _ in fh:
                index_chunks += 1
    text_dir = os.path.join(ROOT, cfg["data"]["index_dir"], "_text")
    return {
        "python": sys.version.split()[0],
        "root": ROOT,
        "model": cfg["llm"]["model"],
        "base_url": cfg["llm"]["base_url"],
        "api_key_set": bool(cfg["llm"]["api_key"]),
        "max_steps": cfg["agent"]["max_steps"],
        "companies": cfg.get("companies", []),
        "corpus": list_corpus(cfg),
        "index": {"chunks": index_chunks,
                  "texts": len(os.listdir(text_dir)) if os.path.isdir(text_dir) else 0},
        "busy": RUN_LOCK.locked(),
        "reports": list_reports(cfg),
        "traces": list_traces(cfg),
        "thresholds": cfg.get("thresholds", {}),
        "universe_total": universe_total(),
        "coverage_samples": COVERAGE_SAMPLES,
    }


def build_command(action, params):
    """把界面参数翻译成 run.py 命令行。参数在此集中校验，避免注入。"""
    cmd = [sys.executable, "-u", os.path.join(ROOT, "run.py")]

    if action == "fetch":
        code = str(params.get("code", "")).strip()
        if not CODES_RE.match(code):
            raise ValueError("抓取对象需为代码或公司简称（如 002714 或 牧原股份）")
        limit = int(params.get("limit") or 3)
        if not 1 <= limit <= 30:
            raise ValueError("每类报告份数需在 1~30 之间")
        return cmd + ["fetch", "--code", code, "--limit", str(limit)]

    if action == "index":
        return cmd + (["index", "--force"] if params.get("force") else ["index"])

    if action == "analyze":
        codes = params.get("codes") or []
        if isinstance(codes, str):
            codes = [c for c in re.split(r"[,\s]+", codes) if c]
        if not codes:
            raise ValueError("请至少填写一个股票代码")
        cmd += ["analyze"]
        for code in codes:
            code = str(code).strip()
            if not CODES_RE.match(code):
                raise ValueError("股票代码格式不正确：" + code)
            cmd += ["--code", code]
        since = params.get("since_year")
        if since:
            cmd += ["--since-year", str(int(since))]
        periods = params.get("periods")
        if periods:
            periods = int(periods)
            if not 1 <= periods <= 20:
                raise ValueError("报告期数需在 1~20 之间")
            cmd += ["--periods", str(periods)]
        if params.get("refresh"):
            cmd += ["--refresh"]
        if params.get("quiet"):
            cmd += ["--quiet"]
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

    只读操作：不改动轨迹内容，因此不影响力「可追溯、可复现」的要求。
    """

    def __init__(self, trace_dir, existing):
        self.dir = trace_dir
        self.existing = set(existing)
        self.path = None
        self.offset = 0
        self._bound = None

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


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FinAgentUI/1.0"
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
        if parsed.path == "/api/report":
            return self.api_report(query)
        if parsed.path == "/api/table":
            return self.api_table(query)
        if parsed.path == "/api/trace":
            return self.api_trace(query)
        if parsed.path == "/api/corpus":
            return self.api_corpus(query)
        if parsed.path == "/api/settings":
            return self.api_settings()
        if parsed.path == "/api/universe":
            keyword = (query.get("q") or [""])[0]
            try:
                limit = min(int((query.get("limit") or ["12"])[0]), 50)
            except ValueError:
                limit = 12
            return self.send_json({"ok": True, "query": keyword,
                                   "total": universe_total(),
                                   "items": search_universe(keyword, limit)})
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

    def api_corpus(self, query):
        """返回某公司的公告原文清单，供界面展示证据出处。"""
        code = (query.get("code") or [""])[0]
        if not re.match(r"^\d{6}$", code):
            return self.send_error_json("需要 6 位代码")
        cfg = load_config()
        mpath = os.path.join(ROOT, cfg["data"]["corpus_dir"], code, "manifest.json")
        if not os.path.isfile(mpath):
            return self.send_json({"ok": True, "code": code, "documents": []})
        manifest = json.loads(read_text(mpath))
        docs = []
        for e in manifest:
            pdf = e.get("path")
            docs.append({
                "date": e.get("date"), "kind": e.get("kind"), "title": e.get("title"),
                "url": e.get("url"),
                "size_mb": round(os.path.getsize(pdf) / 1e6, 2)
                if pdf and os.path.isfile(pdf) else None,
            })
        docs.sort(key=lambda d: (d.get("date") or ""), reverse=True)
        return self.send_json({"ok": True, "code": code, "documents": docs})

    def api_settings(self):
        """读取 / 写入推理引擎配置。

        密钥只写入本地 config.local.yaml（.gitignore 已排除），
        接口返回时始终打码，避免密钥经由浏览器或日志泄露。
        """
        cfg = load_config()
        self.send_json({"ok": True, "llm": {
            "base_url": cfg["llm"]["base_url"],
            "model": cfg["llm"]["model"],
            "api_key_set": bool(cfg["llm"]["api_key"]),
            "api_key_hint": _mask_key(cfg["llm"]["api_key"]),
            "local_config": os.path.relpath(
                local_config_path(os.path.join(ROOT, "config.yaml")), ROOT).replace("\\", "/"),
        }})

    def save_settings(self, params):
        if RUN_LOCK.locked():
            return self.send_error_json("正在运行，请等本次运行结束后再保存配置", 409)
        try:
            from finagent.config import local_config_path
        except Exception as exc:
            return self.send_error_json("无法加载配置模块：" + str(exc), 500)

        path = local_config_path(os.path.join(ROOT, "config.yaml"))
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                data = {}
        llm = data.setdefault("llm", {})

        base_url = str(params.get("base_url") or "").strip()
        model = str(params.get("model") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        if base_url:
            if not base_url.startswith(("http://", "https://")):
                return self.send_error_json("接口地址需以 http:// 或 https:// 开头")
            llm["base_url"] = base_url
        if model:
            llm["model"] = model
        if api_key:
            llm["api_key"] = api_key
        if not llm:
            return self.send_error_json("没有需要保存的内容")

        try:
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            return self.send_error_json("写入失败：" + str(exc), 500)
        self.send_json({"ok": True, "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                        "api_key_set": bool(llm.get("api_key")),
                        "api_key_hint": _mask_key(llm.get("api_key"))})

    # ---------------- 执行动作（SSE 流式日志）

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1000000:
            return self.send_error_json("请求过大", 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_error_json("请求体不是合法 JSON")

        if parsed.path == "/api/settings":
            return self.save_settings(body)
        if parsed.path != "/api/run":
            return self.send_error_json("未知接口", 404)

        params = body
        action = str(params.get("action", ""))

        try:
            cmd = build_command(action, params)
        except ValueError as exc:
            return self.send_error_json(str(exc))

        if not RUN_LOCK.acquire(blocking=False):
            return self.send_error_json(
                "已有一次运行正在进行（可能是队友在跑），请等它结束后再试", 409)
        try:
            self._stream_run(action, cmd)
        finally:
            RUN_LOCK.release()

    def _stream_run(self, action, cmd):
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
        # fetch / index 不打印运行编号，改从轨迹文件名回填
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器（也可用环境变量 FINAGENT_NO_BROWSER=1）")
    args = parser.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    local_url = "http://127.0.0.1:%d/" % args.port
    shared = args.host not in ("127.0.0.1", "localhost", "::1")
    cfg = load_config()
    print("=" * 62)
    print("  FinAgent —— 上市公司财务报告分析智能体")
    print("=" * 62)
    if shared:
        print("  本机打开   " + local_url)
        peers = local_addresses()
        for index, addr in enumerate(peers):
            if index == 0:
                print("  队友打开   http://%s:%d/   ← 推荐发给队友" % (addr, args.port))
            else:
                print("  其它地址   http://%s:%d/   ← 虚拟网卡，一般不用" % (addr, args.port))
        if not peers:
            print("  队友打开   http://本机局域网IP:%d/" % args.port)
        print("  共享模式   局域网内可访问；首次请在弹出的防火墙提示中允许「专用网络」")
    else:
        print("  界面地址   " + url)
    print("  项目目录   " + ROOT)
    print("  解释器     " + sys.executable)
    print("  推理模型   %s @ %s" % (cfg["llm"]["model"], cfg["llm"]["base_url"]))
    print("  密钥状态   " + ("已配置" if cfg["llm"]["api_key"] else "未配置（分析将回落到离线归因）"))
    print("  停止服务   在本窗口按 Ctrl+C")
    print("=" * 62)

    quiet_browser = args.no_browser or bool(os.environ.get("FINAGENT_NO_BROWSER"))
    if not quiet_browser:
        threading.Timer(1.0, lambda: webbrowser.open(local_url if shared else url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
