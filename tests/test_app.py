"""应用界面与接口测试：上传 → 待分析清单 → 运行 → 产物，全部走真实 HTTP。

这里坚持"起真服务、发真请求"，而不是直接调用内部函数：界面与后端的契约
一旦错位（字段名改了、路径变了），只有走 HTTP 才能发现。
不调用大模型（那属于端到端测试，见 FINAGENT_E2E）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import _fixtures as F

RESULTS = []


def check(title: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((title, bool(ok)))
    print(f"  [{'通过' if ok else '失败'}] {title}" + (f"  {detail}" if detail else ""))


def request(port: int, path: str, method: str = "GET", body=None, ctype=None):
    """发一个请求，返回 (状态码, 解析后的 JSON 或原始文本)。"""
    url = f"http://127.0.0.1:{port}{path}"
    data = body
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = exc.code
    text = raw.decode("utf-8", "replace")
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def multipart(files: list) -> tuple:
    """构造 multipart/form-data 请求体：files 是 (文件名, 字节) 列表。"""
    boundary = "----finagent" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, payload in files:
        out.write(f"--{boundary}\r\n".encode())
        out.write(b'Content-Disposition: form-data; name="files"; filename="'
                  + name.encode("utf-8") + b'"\r\n')
        out.write(b"Content-Type: application/pdf\r\n\r\n")
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def blank_pdf() -> bytes:
    """一张没有任何文字的合法 PDF：用来验证服务端会明确拒绝扫描件。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main() -> int:
    from app.server import BASE_URL, Handler, HOST
    from finagent.config import ALLOWED_HOST, load_config

    httpd = ThreadingHTTPServer((HOST, 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    print(f"服务已启动 http://127.0.0.1:{port}/")
    try:
        # --- 静态页面 ---
        code, html = request(port, "/")
        check("首页可访问", code == 200 and "FinAgent" in html, f"{code} {len(html)} 字节")
        check("首页含上传区与四个视图",
              all(w in html for w in ("上传", "dropzone", "执行轨迹", "分析报告")))

        # --- 状态与配置 ---
        code, payload = request(port, "/api/status")
        status = payload.get("status") or {}
        check("/api/status 正常", code == 200 and status.get("host") == "127.0.0.1",
              f"host={status.get('host')}")
        check("状态里含待分析清单与产物列表",
              "pending" in status and "reports" in status)

        code, payload = request(port, "/api/settings")
        llm = payload.get("llm") or {}
        key = (load_config()["llm"]["api_key"] or "")
        check("base_url 固定为 DeepSeek（不允许改成别的服务）",
              llm.get("base_url") == BASE_URL and ALLOWED_HOST in BASE_URL,
              str(llm.get("base_url")))
        check("密钥始终打码、绝不明文回传",
              bool(key) and key not in json.dumps(payload, ensure_ascii=False),
              str(llm.get("api_key_hint")))
        check("模型限定为两个 DeepSeek 模型",
              tuple(llm.get("models") or ()) == ("deepseek-chat", "deepseek-reasoner"),
              str(llm.get("models")))

        # --- 没有材料时不得启动运行 ---
        code, payload = request(port, "/api/run", "POST",
                                json.dumps({"action": "analyze"}).encode(),
                                "application/json")
        check("没有材料时拒绝启动分析", code == 400 and not payload.get("ok"),
              str(payload.get("error"))[:60])

        # --- 上传：只收 PDF ---
        body, ctype = multipart([("说明.txt", b"not a pdf")])
        code, payload = request(port, "/api/upload", "POST", body, ctype)
        rejected = payload.get("rejected") or []
        check("非 PDF 文件被拒绝",
              code == 200 and rejected and "PDF" in rejected[0].get("error", ""),
              str(rejected[:1]))

        body, ctype = multipart([("扫描件.pdf", blank_pdf())])
        code, payload = request(port, "/api/upload", "POST", body, ctype)
        rejected = payload.get("rejected") or []
        check("无文字的 PDF 被明确拒绝并说明原因",
              code == 200 and rejected and ("文本" in rejected[0].get("error", "")
                                            or "扫描" in rejected[0].get("error", "")),
              str(rejected[:1])[:100])

        # --- 上传真实财报（本机有语料时）---
        pdfs = F.pick("002714", ("semi",))
        if pdfs:
            payload_bytes = Path(pdfs[0]).read_bytes()
            # 用中文文件名上传：浏览器发的就是这种名字，
            # 名字若变成乱码，"这个数字来自哪个文件"就无法核对。
            body, ctype = multipart([("牧原股份2026年半年报.pdf", payload_bytes)])
            code, payload = request(port, "/api/upload", "POST", body, ctype)
            added = payload.get("added") or []
            check("上传财报成功并返回识别结果",
                  code == 200 and added and "牧原" in (added[0].get("subject") or ""),
                  str((added[0] or {}).get("subject") if added else payload)[:80])
            if added:
                item = added[0]
                check("中文文件名原样保留",
                      item.get("filename") == "牧原股份2026年半年报.pdf",
                      str(item.get("filename")))
                check("识别结果含报告期／报告类型／科目数／校验数",
                      item.get("report_date") == "2026-06-30"
                      and item.get("report_kind") == "semi"
                      and (item.get("fields_total") or 0) > 20
                      and item.get("checks_passed") is not None,
                      f"{item.get('report_date')} {item.get('report_kind')} "
                      f"{item.get('fields_total')} 项 校验{item.get('checks_passed')}")
                check("识别置信度给出档位", item.get("confidence") in ("ok", "warn", "error"),
                      str(item.get("confidence")))
                stored = item.get("stored_path") or ""
                check("PDF 原件已留存（可回溯）",
                      stored and (ROOT / stored).is_file(), stored)
                run_id = payload.get("run_id") or ""
                code2, pending = request(port, "/api/pending")
                ok = (code2 == 200 and pending.get("run_id") == run_id
                      and any(i.get("filename") == item.get("filename")
                              for i in pending.get("items") or []))
                check("待分析清单反映本次上传", ok, str(pending.get("run_id")))
                check("清单带主体归组信息", bool(pending.get("groups")),
                      str(pending.get("groups"))[:80])

            code, payload = request(port, "/api/upload/clear", "POST", b"{}",
                                    "application/json")
            check("可清空当前批次", code == 200 and payload.get("ok"), str(payload)[:60])
        else:
            print("  [跳过] 上传真实财报：本机没有 data/corpus/002714/ 下的 PDF。")

        # --- 其它接口边界 ---
        code, _ = request(port, "/api/nope")
        check("未知 GET 路径返回 404", code == 404)
        code, _ = request(port, "/api/nope", "POST", b"{}", "application/json")
        check("未知 POST 路径返回 404", code == 404)
        code, payload = request(port, "/api/run", "POST", b"{not json",
                                "application/json")
        check("非法 JSON 被拒绝", code == 400, str(payload)[:60])
        code, payload = request(port, "/api/run", "POST",
                                json.dumps({"action": "delete_all"}).encode(),
                                "application/json")
        check("未知动作被拒绝", code in (400, 404) and not payload.get("ok"),
              str(payload.get("error"))[:60])
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("-" * 74)
    bad = [t for t, ok in RESULTS if not ok]
    print(f"界面与接口测试：{len(RESULTS) - len(bad)}/{len(RESULTS)} 项通过")
    for title in bad:
        print("  未通过：" + title)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
