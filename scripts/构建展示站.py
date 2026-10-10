# -*- coding: utf-8 -*-
"""构建 Cloudflare Pages 静态展示站。

为什么做成静态站：
    竞赛作品的完整运行需要 Python 与本地封闭语料，跑在服务器上才有意义。
    但"给评委和队友一个能直接打开的网址"这件事，不应该依赖于谁来开这台电脑。
    因此这里把已经跑出来的真实产物（报告、指标宽表、执行轨迹、PDF）
    固化成静态文件，部署到 Cloudflare Pages —— 免费、长期在线、不需要任何服务进程。

    展示站只读：能浏览全部结论与证据链，不能发起新分析。
    这是刻意的：静态站上没有任何模型密钥，也不会假装自己算得出现场的新问题。

用法：
    python scripts/构建展示站.py [--out dist/cloudflare-pages] [--zip]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

import pdfexport  # noqa: E402

STATIC_DIR = os.path.join(ROOT, "app", "static")

# 序号 -> 文件名。中文名在 URL 里容易被转义得很难看，落盘一律用代码。
SKIP_REPORT_PREFIX = ()


def slug_of(name: str) -> str:
    """把报告文件名压成纯 ASCII 的短标识，避免中文文件名在各环节被转义。"""
    stem = name[:-3] if name.endswith(".md") else name
    head = stem.split("_")[0]
    if head.isdigit():
        return head
    return "compare" if "对照" in stem else "report"


def read_jsonl(path: str) -> list:
    out = []
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# 报告列表的人工排序：把最有说服力的公司报告放前面。
# 「跨公司对照分析」固定放最后——它是无密钥时的降级产物（表里会标 offline），
# 存在的意义是演示不配模型也能产出完整结构，不适合当访客看到的第一份报告。
REPORT_PRIORITY = ["002714", "000725", "601398", "600519", "830964", "600217", "000001"]


def _ordered_reports(entries: list) -> list:
    """按人工优先级排序；未列入的排在已知公司之后，对照报告排最末。

    sorted 是稳定排序，因此同一档次内仍保持按修改时间倒序的原顺序。
    """
    def rank(entry):
        name = str(entry.get("name") or "")
        if name.startswith("跨公司"):
            return (2, 0)
        for idx, code in enumerate(REPORT_PRIORITY):
            if name.startswith(code):
                return (0, idx)
        return (1, 0)

    return sorted(entries, key=rank)


def build(out_dir: str, make_zip: bool, with_pdf: bool) -> int:
    from app.server import load_config, status_payload, read_text  # noqa: E402

    cfg = load_config()
    status = status_payload()
    report_dir = os.path.join(ROOT, cfg["output"]["report_dir"])
    trace_dir = os.path.join(ROOT, cfg["output"]["trace_dir"])

    if os.path.isdir(out_dir):
        _rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "demo", "reports"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "demo", "traces"), exist_ok=True)

    # ---------------- 静态资源
    # 界面里引用的是 /static/xxx 这样的绝对路径（本地服务端就是这么挂载的），
    # 因此展示站也必须把资源放在 static/ 子目录下，否则会 404。
    os.makedirs(os.path.join(out_dir, "static"), exist_ok=True)
    for asset in ("index.html", "style.css", "app.js", "md.js"):
        src = os.path.join(STATIC_DIR, asset)
        dst = os.path.join(out_dir, "static" if asset != "index.html" else out_dir, asset)
        if asset == "index.html":
            html = io.open(src, encoding="utf-8").read()
            # 打上静态模式标记，前端据此切换到只读数据源
            html = html.replace("<html lang=\"zh-CN\" data-theme=\"light\">",
                                "<html lang=\"zh-CN\" data-theme=\"light\" data-static>")
            html = html.replace("智金工坊 · FinAgent —— 上市公司财务报告分析智能体",
                                "智金工坊 · FinAgent —— 上市公司财务报告分析智能体（展示站）")
            # 资源引用改成相对路径。本地服务端把资源挂在 /static/ 下，所以源码里是绝对路径；
            # 但托管平台不一定把站点放在域名根目录（GitHub Pages 就是 /finagent/ 这种子路径），
            # 绝对路径在子路径下会 404，相对路径两种情况都能用。
            html = html.replace('href="/static/', 'href="static/')
            html = html.replace('src="/static/', 'src="static/')
            io.open(dst, "w", encoding="utf-8", newline="").write(html)
        else:
            shutil.copy2(src, dst)

    # ---------------- 报告
    reports_meta = []
    existing = _ordered_reports(status.get("reports", []))
    for entry in existing:
        name = entry["name"]
        src = os.path.join(report_dir, name)
        if not os.path.isfile(src):
            continue
        slug = slug_of(name)
        markdown = read_text(src)
        io.open(os.path.join(out_dir, "demo", "reports", slug + ".md"),
                "w", encoding="utf-8", newline="").write(markdown)

        item = {
            "slug": slug, "name": name, "bytes": entry.get("bytes") or len(markdown),
            "mtime": entry.get("mtime"), "table": None, "csv": None, "pdf": None,
            "facts": _count(markdown, "**证据**："),
        }

        table = entry.get("table")
        if table:
            tsrc = os.path.join(ROOT, table)
            if os.path.isfile(tsrc):
                # 两份都留：CSV 供直接下载留存，JSON 供界面渲染。
                # 界面不解析 CSV 是为了不把"逗号在字段里"这类边界问题带进前端。
                shutil.copy2(tsrc, os.path.join(out_dir, "demo", "reports", slug + ".csv"))
                item["csv"] = "demo/reports/%s.csv" % slug
                rows = _read_csv(tsrc)
                if rows:
                    payload = {"ok": True, "header": rows[0], "rows": rows[1:400],
                               "total": max(len(rows) - 1, 0),
                               "truncated": len(rows) - 1 > 400,
                               "file": os.path.basename(tsrc)}
                    io.open(os.path.join(out_dir, "demo", "reports", slug + ".json"),
                            "w", encoding="utf-8", newline="").write(
                        json.dumps(payload, ensure_ascii=False))
                    item["table"] = "demo/reports/%s.json" % slug

        if with_pdf:
            title, body, meta = pdfexport.split_report(markdown)
            doc = pdfexport.build_document(
                title, pdfexport.format_meta(meta) or ["<b>生成工具</b> FinAgent"],
                pdfexport.markdown_to_html(body),
                footer_note="<b>文件</b> %s　|　<b>导出时间</b> %s<br>"
                            % (name, time.strftime("%Y-%m-%d %H:%M:%S")))
            data = pdfexport.html_to_pdf(doc)
            if data:
                with open(os.path.join(out_dir, "demo", "reports", slug + ".pdf"), "wb") as fh:
                    fh.write(data)
                item["pdf"] = "demo/reports/%s.pdf" % slug
                print("  PDF  %-44s %6.0f KB" % (name, len(data) / 1024))
            else:
                print("  PDF  跳过（未找到浏览器）%s" % name)

        reports_meta.append(item)

    # ---------------- 轨迹
    traces_meta = []
    # 直接扫目录，而不是用 status["traces"]：状态接口为了界面性能只返回最新 60 条，
    # 而展示站需要全部历史运行——工商银行等早期高价值样本正好落在 60 条之外，
    # 用状态接口会让它们静默落选回放。
    runs = []
    for name in os.listdir(trace_dir):
        if not name.endswith(".jsonl"):
            continue
        full = os.path.join(trace_dir, name)
        runs.append({"run": name[:-6], "mtime": os.path.getmtime(full),
                     "bytes": os.path.getsize(full)})
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    demo_seeds = []
    for entry in runs:
        run = entry["run"]
        src = os.path.join(trace_dir, run + ".jsonl")
        if not os.path.isfile(src):
            continue
        events = read_jsonl(src)
        if len(events) < 5:
            continue
        rel = "demo/traces/%s.json" % run
        io.open(os.path.join(out_dir, rel), "w", encoding="utf-8", newline="").write(
            json.dumps(events, ensure_ascii=False))
        result = next((e for e in events if e.get("event") == "result"), None)
        agent_end = next((e for e in events if e.get("event") == "agent_end"), None)
        targets = [e for e in events if e.get("event") == "target"]
        target = targets[0] if targets else None
        # 推理模式早期只在 agent_end 里记，后来才补进 result，
        # 因此两处都读，否则旧轨迹会被误判成离线运行而落选回放。
        mode = (result or {}).get("agent_mode") or (agent_end or {}).get("mode")
        traces_meta.append({
            "run": run, "mtime": entry.get("mtime"), "bytes": entry.get("bytes"),
            "events": len(events), "file": rel,
            "name": (target or {}).get("name") or "",
            "secucode": (target or {}).get("secucode") or "",
            "findings": (result or {}).get("findings"),
            "report": os.path.basename((result or {}).get("report") or ""),
        })
        # 只有满足三个条件的运行才配当回放样本：
        #   1. 只分析了一家公司——跨公司对照的结论摊薄，不适合当第一印象；
        #   2. 走的是大模型推理而不是离线归因——回放要展示的正是模型的判断过程；
        #   3. 确实产出了结论。
        if (result and len(targets) == 1 and target.get("name")
                and mode == "llm" and (result.get("findings") or 0) > 0):
            code = (target.get("secucode") or "").split(".")[0]
            demo_seeds.append({
                "run": run, "name": target["name"], "secucode": target["secucode"],
                "code": code, "findings": result.get("findings") or 0,
                "high": result.get("high") or 0,
                "report": os.path.basename(result.get("report") or ""),
            })

    # ---------------- 演示回放：每个公司挑一次结论最丰富的运行
    best = {}
    for seed in demo_seeds:
        cur = best.get(seed["code"])
        score = (seed["findings"], seed["high"])
        if cur is None or score > (cur["findings"], cur["high"]):
            best[seed["code"]] = seed

    # 提问用真实用户会说的话，而不是"分析某公司"这种模板句——
    # 展示站的第一屏就是产品给评委的第一印象。
    QUESTIONS = {
        "002714": "分析牧原股份 2026 年中报为什么亏损",
        "000725": "京东方账面利润和经营现金流为什么差这么多",
        "601398": "工商银行这个半年报怎么样",
        "600519": "分析贵州茅台最近几期的关键财务指标",
        "830964": "润农节水最近几期有什么异常信号",
        "600217": "中再资环的利润质量怎么样",
        "000001": "平安银行的现金流和利润匹配吗",
    }
    # 排序刻意人工指定：先放"规则引擎发现、模型回原文证伪"这类最有讲头的案例。
    PRIORITY = ["002714", "000725", "601398", "600519", "830964", "600217", "000001"]
    order = {code: i for i, code in enumerate(PRIORITY)}

    demos = []
    for code, seed in sorted(best.items(), key=lambda kv: order.get(kv[0], 99)):
        slug = None
        for item in reports_meta:
            if item["slug"] == code:
                slug = item["slug"]
                break
        demos.append({
            "question": QUESTIONS.get(code, "分析%s最近的业绩变化" % seed["name"]),
            "name": seed["name"], "secucode": seed["secucode"], "run": seed["run"],
            "findings": seed["findings"], "high": seed["high"],
            "reportSlug": slug,
        })
    demos = demos[:6]

    # ---------------- 清单
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": "FinAgent 静态展示站",
        "note": "本站为只读展示。全部内容由 run.py 真实运行产生，可在本地复现。",
        "status": {
            "model": status.get("model"),
            "api_key_set": False,
            "universe_total": status.get("universe_total"),
            "coverage_samples": status.get("coverage_samples", []),
            "thresholds": status.get("thresholds", {}),
            "corpus": status.get("corpus", []),
            "index": status.get("index", {}),
            "companies": status.get("companies", []),
        },
        "reports": reports_meta,
        "traces": traces_meta,
        "demos": demos,
    }
    io.open(os.path.join(out_dir, "demo", "manifest.json"), "w",
            encoding="utf-8", newline="").write(
        json.dumps(manifest, ensure_ascii=False, indent=1))

    # ---------------- Pages 配置与说明
    io.open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8", newline="").write(
        "/*\n  X-Content-Type-Options: nosniff\n\n"
        "/demo/*.pdf\n  Content-Type: application/pdf\n  Content-Disposition: attachment\n")
    io.open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8").write("")
    _write_readme(out_dir, manifest)

    print("\n报告 %d 份（含 PDF %d 份）｜ 轨迹 %d 条 ｜ 回放样例 %d 个"
          % (len(reports_meta), sum(1 for r in reports_meta if r["pdf"]),
             len(traces_meta), len(demos)))
    print("输出目录 %s" % out_dir)
    if make_zip:
        _zip(out_dir)
    return 0


def _read_csv(path: str) -> list:
    import csv
    with io.open(path, encoding="utf-8", newline="") as fh:
        return [row for row in csv.reader(fh) if row]


def _count(text: str, needle: str) -> int:
    return text.count(needle)


def _write_readme(out_dir: str, manifest: dict) -> None:
    io.open(os.path.join(out_dir, "部署说明.md"), "w", encoding="utf-8", newline="").write(
        "# FinAgent 展示站 · 部署说明\n\n"
        "这个目录已经是可直接上传的静态站点：不需要 Node、不需要安装任何 CLI，\n"
        "也不需要内网穿透客户端（cloudflared / ngrok 等）——展示站是纯静态的，\n"
        "没有常驻进程，也不会把你的本机服务暴露到公网。\n\n"
        "## 方案 A：Cloudflare Pages（推荐，全程在浏览器里完成）\n\n"
        "1. 打开 https://dash.cloudflare.com/ 并登录（免费账号即可）。\n"
        "2. 左侧选 **Workers 与 Pages** → **创建** → **Pages** → **上传资产**。\n"
        "3. 项目名填 `finagent`，把本目录（或 `FinAgent-展示站.zip`）拖进去，点部署。\n\n"
        "几十秒后会得到一个 `https://finagent.pages.dev` 这样的网址，\n"
        "把它发给队友、评委，或写进项目计划书即可。\n\n"
        "## 方案 B：GitHub Pages（不用登录第三方）\n\n"
        "把本目录的内容推到仓库的 `gh-pages` 分支，或放进 `main` 分支的 `/docs` 目录，\n"
        "然后在仓库 **Settings → Pages** 里把来源指向它；网址形如\n"
        "`https://<用户名>.github.io/finagent/`。页面里的资源引用用的是相对路径，\n"
        "因此放在子目录下也能正常打开。\n\n"
        "## 更新内容\n\n"
        "重新运行 `python scripts/构建展示站.py --zip`，再在同一个项目里\n"
        "选择 **创建新部署** 并重新上传即可，网址不变。\n\n"
        "## 不能在站上做什么\n\n"
        "展示站是只读的：可以浏览全部报告、指标宽表、执行轨迹与 PDF，\n"
        "但不能在上面发起新分析。这是有意的——完整分析需要读公告原文、\n"
        "跑规则引擎并调用大模型，而且会消耗用户自己的密钥。\n\n"
        "## 边界说明\n\n"
        "展示站是只读的：可以浏览全部报告、指标宽表、执行轨迹与 PDF，\n"
        "但不能在上面发起新分析。站点里没有任何模型密钥，也不连接任何后台服务。\n"
        "需要发起新分析时，在本地运行 `启动FinAgent应用.bat`。\n\n"
        "生成时间：%s\n" % manifest["generated_at"])


def _zip(out_dir: str) -> None:
    dist = os.path.dirname(out_dir)
    zip_path = os.path.join(dist, "FinAgent-展示站.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(out_dir):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, out_dir))
    print("打包 %s（%.1f MB）" % (zip_path, os.path.getsize(zip_path) / 1e6))


def _rmtree(path: str) -> None:
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 Cloudflare Pages 静态展示站")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "cloudflare-pages"))
    ap.add_argument("--zip", action="store_true", help="同时打包成 zip")
    ap.add_argument("--no-pdf", action="store_true", help="跳过 PDF 生成（更快）")
    args = ap.parse_args()
    print("构建展示站 -> %s" % args.out)
    return build(args.out, args.zip, not args.no_pdf)


if __name__ == "__main__":
    raise SystemExit(main())
