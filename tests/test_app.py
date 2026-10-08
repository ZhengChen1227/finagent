"""应用层测试：启动 app/server.py，校验界面接口、参数校验与流式执行。

不依赖浏览器，也不访问外网；只用标准库。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = [], [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "通过" if ok else "失败"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


def skip(name: str, reason: str) -> None:
    """前置产物尚未构建时明确跳过，而不是报失败。

    公告原文与全文索引体积大、不入版本库，刚克隆下来的仓库本来就没有它们。
    此时报"失败"会让评审误以为功能坏了；报"跳过"并给出构建命令才是准确的描述。
    一旦环境已构建，下面的断言依然严格执行。
    """
    SKIP.append(name)
    print(f"  [跳过] {name}  {reason}")


def has_files(rel_dir: str) -> bool:
    path = ROOT / rel_dir
    return path.is_dir() and any(f.is_file() for f in path.rglob("*"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(base: str, path: str, timeout: float = 30.0):
    """返回 (状态码, 解析后的 JSON 或原始文本)。"""
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code = exc.code
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


def post_sse(base: str, payload: dict, timeout: float = 600.0) -> list:
    """发送 POST 并收集全部 SSE 事件。"""
    req = urllib.request.Request(
        base + "/api/run",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return [{"kind": "http_error", "code": exc.code,
                 "body": exc.read().decode("utf-8", "replace")}]
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "app/server.py", "--port", str(port), "--no-browser"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    try:
        # ---- 等待服务就绪
        ready = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.5)
        check("服务启动", ready, f"端口 {port}")
        if not ready:
            print(proc.stdout.read()[-2000:] if proc.stdout else "")
            return 1

        # ---- 静态资源
        code, html = get(base, "/")
        check("首页返回 HTML", code == 200 and "FinAgent" in html, f"HTTP {code}")
        for asset in ("style.css", "app.js", "md.js"):
            code, _ = get(base, "/static/" + asset)
            check(f"静态资源 {asset}", code == 200, f"HTTP {code}")

        # ---- 环境自检
        code, body = get(base, "/api/status")
        st = body.get("status", {}) if isinstance(body, dict) else {}
        check("状态接口", code == 200 and body.get("ok") is True)
        corpus_built = has_files("data/corpus")
        if corpus_built:
            check("语料已落盘", len(st.get("corpus", [])) > 0,
                  f"{len(st.get('corpus', []))} 家公司")
            check("索引已建立", st.get("index", {}).get("chunks", 0) > 0,
                  f"{st.get('index', {}).get('chunks')} 文本块")
        else:
            skip("语料已落盘", "尚未构建封闭数据环境：python run.py fetch")
            skip("索引已建立", "同上，随后：python run.py index")
        check("报告可枚举", len(st.get("reports", [])) > 0,
              f"{len(st.get('reports', []))} 份")
        if has_files("output/traces"):
            check("轨迹可枚举", len(st.get("traces", [])) > 0,
                  f"{len(st.get('traces', []))} 次")
        else:
            skip("轨迹可枚举", "尚无运行记录（本测试程序后续会产生一条）")

        # ---- 报告正文与指标宽表
        reports = st.get("reports", [])
        if reports:
            # 取内容最完整的一份作为样本：报告目录里还有篇幅较短的《跨公司对照分析》，
            # 按修改时间取最新会随机命中它，使断言不稳定。
            rep = max(reports, key=lambda r: r.get("bytes", 0))
            code, body = get(base, "/api/report?path=" + urllib.parse.quote(rep["path"]))
            check("读取报告正文", code == 200 and len(body.get("markdown", "")) > 1000,
                  f"{len(body.get('markdown', ''))} 字符")
            if rep.get("table"):
                code, body = get(base, "/api/table?path=" + urllib.parse.quote(rep["table"]))
                ok = code == 200 and len(body.get("header", [])) > 10 and body.get("total", 0) > 0
                check("读取指标宽表", ok,
                      f"{len(body.get('header', []))} 列 / {body.get('total')} 行")

        # ---- 公告原文清单
        if corpus_built:
            code, body = get(base, "/api/corpus?code=002714")
            check("公告原文清单", code == 200 and len(body.get("documents", [])) > 0,
                  f"{len(body.get('documents', []))} 份")
        else:
            skip("公告原文清单", "本地无公告原文，无法枚举")

        # ---- 轨迹可回放
        runs = st.get("traces", [])
        if runs:
            # 同理，取事件最多的一次运行，避免命中只有两三条事件的索引轨迹
            run = max(runs, key=lambda r: r.get("bytes", 0))
            code, body = get(base, "/api/trace?run=" + urllib.parse.quote(run["run"]))
            events = body.get("events", [])
            seqs = [e.get("seq") for e in events]
            check("轨迹可回放", code == 200 and len(events) > 5, f"{len(events)} 条事件")
            check("轨迹 seq 单调递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))

        # ---- 参数校验（不应启动子进程）
        # 说明：分析对象现在既可以是代码也可以是公司简称，由 run.py 的名录解析，
        # 因此这里只校验字符层面的安全性（拒绝空白与特殊字符），不再限定纯数字。
        for payload, expect in (
            ({"action": "fetch", "code": "600519;rm -rf /"}, "代码或公司简称"),
            ({"action": "analyze", "codes": ["600519;calc"]}, "格式不正确"),
            ({"action": "analyze", "codes": ["600519.SH"], "periods": 99}, "报告期数"),
            ({"action": "unknown"}, "不支持的动作"),
        ):
            events = post_sse(base, payload)
            first = events[0] if events else {}
            ok = (first.get("kind") == "http_error" and first.get("code") == 400
                  and expect in first.get("body", ""))
            check(f"参数校验：{expect}", ok, f"HTTP {first.get('code')}")

        # ---- 简称与代码在命令构造层均应放行（不启动子进程、不调用大模型）
        # 交到界面接口会真的触发一次完整分析，因此这里直接校验命令构造结果。
        sys.path.insert(0, str(ROOT))
        from app.server import build_command
        for good in ("贵州茅台", "600519", "600519.SH", "920002.BJ"):
            try:
                cmd = build_command("analyze", {"codes": [good]})
                ok = cmd[-1] == good
                detail = ""
            except ValueError as exc:
                ok, detail = False, str(exc)
            check(f"命令构造放行：{good}", ok, detail)

        # ---- 路径穿越防护
        for bad in ("../../../Windows/win.ini", "..%2f..%2fconfig.yaml"):
            code, _ = get(base, "/api/report?path=" + bad)
            check("越权路径被拒绝", code in (400, 404), f"HTTP {code}")

        # ---- 流式执行（离线动作，不调用大模型）
        events = post_sse(base, {"action": "index"})
        kinds = [e.get("kind") for e in events]
        check("流式执行：start 事件", kinds[:1] == ["start"], str(kinds[:1]))
        check("流式执行：stdout 转发", "line" in kinds)
        check("流式执行：轨迹实时跟随", "trace" in kinds,
              f"{kinds.count('trace')} 条")
        done = next((e for e in events if e.get("kind") == "done"), {})
        check("流式执行：正常结束", done.get("code") == 0)
        check("流式执行：回填运行编号", bool(done.get("run_id")), str(done.get("run_id")))

        # ---- 命令构造（纯函数，无需网络）
        from app.server import build_command
        cmd = build_command("analyze", {"codes": ["002714.SZ", "000725.SZ"],
                                        "since_year": 2025, "periods": 6})
        check("命令构造：多公司", cmd.count("--code") == 2, " ".join(cmd[-8:]))
        cmd = build_command("fetch", {"code": "002714", "limit": 5})
        check("命令构造：抓取参数", "--limit" in cmd and "5" in cmd)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
    for name in FAIL:
        print("   失败:", name)
    for name in SKIP:
        print("   跳过:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
