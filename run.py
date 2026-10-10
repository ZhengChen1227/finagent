"""FinAgent 命令行入口。

用法：
    python run.py analyze --pdf <文件...>      # 分析用户上传的财报 PDF（智能体驱动）
    python run.py analyze --pdf-dir <目录>     # 目录内所有 PDF 一次性上传并分析
    python run.py index [--run <run_id>]       # 为上传的材料建全文索引
    python run.py mcp                          # 以 MCP 协议暴露工具集

产物：
    output/reports/*.md      分析报告
    output/reports/*.json    结构化结论（便于程序化复核）
    output/tables/*.csv      指标宽表
    output/traces/*.jsonl    完整执行轨迹，可逐条重放

三条不可动摇的设计约束（详见 AGENTS.md）：

1. 输入只有一种——用户上传的财报文件。不存在按代码或公司名联网取数的入口。
2. 全部财务数字来自上传件正文的抽取与确定性计算，模型只做归因推理。
3. 每一份抽取结果都要过交叉校验闸门；未通过校验的科目降级为"需人工复核"，
   并随报告一起呈现，绝不静默输出可疑数字。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

from finagent.agent.loop import AgentLoop
from finagent.config import load_config, uploads_dir
from finagent.corpus.index import CorpusIndex
from finagent.ingest import assemble
from finagent.ingest.dataset import UploadError, UploadSet
from finagent.ingest.group import family_hint, notes as group_notes
from finagent.metrics import indicators as I
from finagent.metrics.periods import period_index
from finagent.report.builder import build_metrics_block, build_report, pct, ratio, yi
from finagent.trace import Trace
from finagent.util import json_default
from finagent.validate.anomaly import run_rules
from finagent.validate.articulation import run_articulation


# ---------------------------------------------------------------- 上传材料

def collect_pdfs(paths, pdf_dir: str | None) -> list:
    """把命令行参数整理成待上传的 PDF 清单。只接受 .pdf，且必须是文件。"""
    out: list = []
    flat: list = []
    for entry in list(paths or []):
        # --pdf 允许两种写法：--pdf a.pdf b.pdf（一次给多个）与
        # --pdf a.pdf --pdf b.pdf（重复给）。两种都常见，不该让人猜对哪一种。
        flat.extend(entry if isinstance(entry, (list, tuple)) else [entry])
    for item in flat:
        if os.path.isdir(item):
            out.extend(_pdfs_in(item))
        elif os.path.isfile(item) and item.lower().endswith(".pdf"):
            out.append(os.path.abspath(item))
        else:
            raise UploadError(f"不是可读取的 PDF 文件：{item}")
    if pdf_dir:
        if not os.path.isdir(pdf_dir):
            raise UploadError(f"目录不存在：{pdf_dir}")
        out.extend(_pdfs_in(pdf_dir))
    seen, unique = set(), []
    for path in out:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _pdfs_in(folder: str) -> list:
    names = sorted(os.listdir(folder))
    return [os.path.join(os.path.abspath(folder), n) for n in names
            if n.lower().endswith(".pdf")
            and os.path.isfile(os.path.join(folder, n))]


def latest_run(root: str) -> str | None:
    """上传根目录下最近的一次运行编号（按目录名排序，时间戳天然有序）。"""
    if not os.path.isdir(root):
        return None
    runs = [d for d in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, d))]
    return runs[-1] if runs else None


def ensure_index(uploads: UploadSet, trace: Trace) -> int:
    """为本次上传的材料建全文索引。

    这是"封闭数据环境"的落地：索引建成后，智能体的全部检索都只读本机文件。
    已建过则不重复抽取，直接复用（同一输入重复运行结果一致）。
    """
    index = CorpusIndex(corpus_dir=uploads.dir,
                        index_dir=os.path.join(uploads.dir, "_index"))
    chunks_path = os.path.join(index.index_dir, "chunks.jsonl")
    if os.path.exists(chunks_path):
        with open(chunks_path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    return index.build(trace=trace)


# ---------------------------------------------------------------- 任务构造

def subject_name(entry_subject: dict, filename: str = "") -> str:
    """主体展示名。代码与名称都能缺，缺了就退回文件名——但不猜。"""
    for key in ("short_name", "full_name"):
        if entry_subject.get(key):
            return entry_subject[key]
    return entry_subject.get("display") or filename


def build_objective(members: list, others: list, question: str = "",
                    family_label: str = "") -> str:
    """构造交给智能体的任务描述。

    这段文字就是 Prompt 的一部分：它决定智能体关注什么。
    注意它只给目标与约束，不规定步骤——步骤由模型自己决定。
    同时在开头写死"材料只有这些"，从源头堵住模型凭记忆补充事实的路径。

    question 是用户在界面上自己敲的问题。它被原样附加在任务描述里，
    使同一套工具链既能跑标准体检，也能回答"为什么二季度毛利率掉了"这类
    定向追问——追问内容本身也会进入技能匹配与轨迹，可被复核。
    """
    head = members[0].get("subject") or {}
    name = subject_name(head, members[0].get("filename", ""))
    code = head.get("code") or "（上传件未披露代码）"
    kinds = "、".join(sorted({(m.get("subject") or {}).get("report_kind_label") or "未知类型"
                             for m in members}))
    dates = "、".join(sorted({(m.get("subject") or {}).get("report_date") or "未知报告期"
                             for m in members}))
    files = "；".join(m.get("filename", "") for m in members)
    peer_line = ""
    if others:
        peer_line = ("本次还上传了其他主体：" + "、".join(others)
                     + "。如需横向对照，用 compare_subjects 工具。\n")
    family_line = (f"该主体的报表格式按科目判定为{family_label}，"
                   f"请勿套用其它行业的经验口径。\n" if family_label else "")
    question = (question or "").strip()
    focus_line = ""
    if question:
        focus_line = (
            f"\n用户本次的定向追问（优先级高于常规体检，但同样必须落到证据上）：\n"
            f"  「{question}」\n"
            f"请优先围绕该问题取证与作答，并在结论中单独给出针对该问题的回答；"
            f"若原文证据不足以回答，须明说证据不足，不得臆测。\n")
    return (
        f"任务：分析 {name}（{code}）的报告期业绩变化与异常信号。\n"
        f"本次可用的全部材料就是用户上传的这 {len(members)} 份财报：{files}。\n"
        f"报告类型：{kinds}；报告期：{dates}。\n"
        f"**除这些文件之外没有任何数据来源**，系统运行期不联网；"
        f"本批材料里没有的信息，一律写\"本批材料不含该信息\"。\n"
        f"{family_line}{peer_line}\n"
        f"要求：\n"
        f"1. 先用 list_materials 与 list_periods 确认这批材料是谁的哪几期、抽到了哪些科目。\n"
        f"2. 运行规则引擎识别信号，运行勾稽校验确认数据自洽，"
        f"并查看 list_verification 的交叉校验结论。\n"
        f"3. 对识别出的异常信号，必须回到上传的财报原文核实原因，"
        f"不得仅凭规则结论下判断。\n"
        f"4. 注意区分「异常信号」「结构性特征」与「口径提示」三类结论。\n"
        f"5. 重点关注利润与现金流的关系、减值计提、所得税异常、非经常性损益、"
        f"以及同比与环比的准确性。\n"
        f"{focus_line}\n"
        f"证据充分后请输出最终 JSON 结论。"
    )


def to_attribution(agent_result: dict, name: str, findings: list) -> dict:
    """把智能体的最终结论转换为报告层结构。

    模型没有给出结论时，本函数返回"未完成归因"的显式说明，而不是回落到
    某段程序写死的归因文本——降级归因会让读者分不清哪句话是模型说的、
    哪句话是代码写的，这正是"必须使用大语言模型作为核心推理引擎"要避免的。
    """
    final = (agent_result or {}).get("final") or {}
    items = []
    for item in (final.get("findings") or []):
        items.append({
            "rule": item.get("title", ""),
            "period": item.get("period", ""),
            "fact": item.get("fact", ""),
            "inference": item.get("inference", ""),
            "confidence": item.get("confidence", ""),
            "verify_with": item.get("verify_with", ""),
        })
    if not items and not final.get("summary"):
        unresolved = list(final.get("unresolved") or [])
        unresolved.append(f"模型未返回可解析的归因结论（{name}），"
                          f"规则层已命中 {len(findings)} 条信号，请重试或人工复核。")
        return {"summary": "", "items": [], "cross_company": "",
                "limitations": list(final.get("limitations") or []),
                "unresolved": unresolved}
    return {
        "summary": final.get("summary", ""),
        "items": items,
        "cross_company": final.get("cross_company", ""),
        "limitations": list(final.get("limitations") or []),
        "unresolved": list(final.get("unresolved") or []),
    }


# ---------------------------------------------------------------- 单主体分析

def analyze_one(uploads: UploadSet, key: str, cfg: dict, trace: Trace,
                since_year: int | None, periods_shown: int, verbose: bool = True,
                question: str = "", others: list | None = None) -> dict | None:
    """分析一个主体：合并材料 → 指标 → 校验 → 规则 → 智能体归因 → 四类产物。"""
    try:
        merged = uploads.merged(key)
    except UploadError as exc:
        print(f"[跳过] {key}：{exc}", file=sys.stderr)
        return None

    frames, table, flags = merged["frames"], merged["table"], merged["flags"]
    members = merged["members"]
    head = members[0].get("subject") or {}
    name = subject_name(head, members[0].get("filename", ""))
    code = head.get("code") or ""

    if all(frames[k].empty for k in ("income", "cashflow", "balance")):
        print(f"[跳过] {name}：上传的财报中未抽到任何报表数据，"
              f"请确认上传的是含三大报表的定期报告。", file=sys.stderr)
        trace.record("skip", subject=key, reason="no_statement_data")
        return None

    family = family_hint(frames)
    family_label = "金融业科目体系" if family == "F" else "一般工商业科目体系"

    with trace.step("compute_metrics", subject=key):
        trace.tool_call("compute_metrics", {"subject": key},
                        {"periods": len(table), "columns": len(table.columns)})

    with trace.step("articulation_check", subject=key):
        articulation = run_articulation(frames, code or key, cfg, trace)

    with trace.step("anomaly_rules", subject=key):
        findings = run_rules(code or key, frames, table, cfg, trace,
                             since_year=since_year, family=family)

    enrich_evidence(findings, merged, uploads)

    for check in (merged.get("verification") or {}).get("checks") or []:
        if check.get("level") == "fail":
            trace.finding("verification_failed", subject=key,
                          name=check.get("name"), detail=check.get("detail"))

    with trace.step("agent_reasoning", subject=key):
        loop = AgentLoop(cfg, trace,
                         max_steps=int(cfg.get("agent", {}).get("max_steps", 12)),
                         verbose=verbose, focus_key=key, uploads=uploads)
        trace.record("question", text=(question or '').strip() or None)
        trace.record("objective", text=build_objective(
            members, others or [], question, family_label))
        trace.record("metrics_block", text=build_metrics_block(table, periods_shown))
    agent_result = loop.run(build_objective(members, others or [], question,
                                            family_label))

    attribution = to_attribution(agent_result, name, findings)

    company = {
        "code": code or key,
        "name": name,
        "industry": family_label,
        "secucode": code,
        "org_name": head.get("full_name") or "",
        "short_name": head.get("short_name") or "",
        "report_family": family,
        # 让报告层知道"证据链完整"：本报告的每条归因都能翻回上传的 PDF。
        "corpus_docs": len(members),
        "uploads": [m.get("filename") for m in members],
    }

    report_md = build_report(
        company=company, frames=frames, table=table, findings=findings,
        attribution=attribution, articulation=articulation, trace=trace,
        cfg=cfg, periods_shown=periods_shown, agent_result=agent_result,
        verification=merged.get("verification") or {},
    )

    report_dir = cfg["output"]["report_dir"]
    table_dir = cfg["output"]["table_dir"]
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(table_dir, exist_ok=True)

    stem = f"{code or 'UPLOAD'}_{name}"
    md_path = os.path.join(report_dir, f"{stem}_财务分析报告.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report_md)
    trace.file_access(md_path, "write", "分析报告")

    verification = merged.get("verification") or {}
    payload = {
        "run_id": trace.run_id,
        "company": company,
        "subject": head,
        "materials": [{
            "file": m.get("filename"), "sha256": m.get("sha256"),
            "pages": m.get("pages"), "report_date": (m.get("subject") or {}).get("report_date"),
            "report_kind": (m.get("subject") or {}).get("report_kind"),
        } for m in members],
        "excluded": [m.get("filename") for m in (merged.get("excluded") or [])],
        "notes": merged.get("notes") or [],
        "verification": {
            "total": len(verification.get("checks") or []),
            "passed": verification.get("passed"),
            "failed": verification.get("failed"),
            "suspect": verification.get("suspect") or {},
            "checks": verification.get("checks") or [],
        },
        "findings": [f.as_dict() for f in findings],
        "attribution": attribution,
        "articulation": articulation,
        "agent": {
            "mode": agent_result.get("mode"),
            "stop_reason": agent_result.get("stop_reason"),
            "llm_calls": agent_result.get("llm_calls"),
            "llm_usage": agent_result.get("llm_usage"),
            "prompt_hashes": agent_result.get("prompt_hashes"),
            "skill_hashes": agent_result.get("skill_hashes"),
            "transcript": agent_result.get("transcript"),
            "final": agent_result.get("final"),
        },
        "flags": {f"{y}Q{q}": v for (y, q), v in flags.items()},
    }
    json_path = os.path.join(report_dir, f"{stem}_结论.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=json_default)
    trace.file_access(json_path, "write", "结构化结论")

    csv_path = os.path.join(table_dir, f"{stem}_指标宽表.csv")
    table.to_csv(csv_path, encoding="utf-8-sig")
    trace.file_access(csv_path, "write", "指标宽表")

    trace.result(subject=key, findings=len(findings),
                 high=sum(1 for f in findings if f.level == "高"),
                 agent_mode=agent_result.get("mode"),
                 report=md_path)
    return {"company": company, "findings": findings, "table": table,
            "attribution": attribution, "agent_result": agent_result,
            "merged": merged,
            "report_path": md_path, "json_path": json_path, "csv_path": csv_path}


def enrich_evidence(findings: list, merged: dict, uploads: UploadSet) -> int:
    """给规则层的证据补上"来自哪份上传文件的第几页"。

    存在的意义：规则层只知道科目与报告期，页码只有在装配层才拿得到。
    补上页码之后，报告里的每一条证据都能被评审直接翻到原文核对，
    这是"数据来源可核验"从口号变成事实的一步。补不上页码的条目保持原样，
    绝不填一个猜出来的页码。
    """
    page_map = {}
    for entry in merged["members"]:
        built = uploads.load_built(entry)
        for item in built.get("evidence") or []:
            try:
                year, q, _ = period_index(item["report_date"])
            except ValueError:
                continue
            page = item.get("page")
            if page is None:
                continue
            page_map.setdefault((item["field"], f"{year}Q{q}"), (
                page, os.path.basename(item.get("source_pdf") or "")))
    filled = 0
    for finding in findings:
        for evidence in finding.evidence:
            if not hasattr(evidence, "source"):
                continue
            hit = page_map.get((evidence.metric, evidence.period))
            if not hit:
                continue
            page, pdf = hit
            if f"第 {page} 页" in (evidence.source or ""):
                continue
            evidence.source = f"{evidence.source}｜{pdf} 第 {page} 页"
            filled += 1
    return filled


# ---------------------------------------------------------------- 跨主体对照

def _mul100(value):
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return value * 100.0


def build_comparison(results: list, cfg: dict, trace: Trace) -> str:
    """跨主体对照：同一套指标口径与规则集，检验结论的可比性与差异。

    对照组是本题最直接的加分点：把两家资产负债结构完全不同的公司放进
    同一套公式里，才能看出哪些"异常"其实是行业属性、哪些才是真问题。
    """
    L = ["# 跨主体对照分析", "",
         f"> 运行编号：`{trace.run_id}`", "",
         "本对照使用完全相同的指标口径与规则集处理每个主体。"
         "这些主体来自用户一次上传的全部财报材料，"
         "因此横向差异只反映经营与资产结构的不同，不掺杂口径差异。", ""]

    header = ["主体", "最新报告期", "营业总收入(亿)", "归母净利润(亿)", "经营现金流(亿)",
              "现金含量(朴素)", "现金含量(调整后)", "折旧摊销/收入", "非经常性损益占比"]
    rows = []
    for r in results:
        t = r["table"]
        if t.empty:
            continue
        y, q = t.index[-1]
        row = t.loc[(y, q)]
        rows.append([r["company"]["name"], f"{y}Q{q}",
                     yi(row.get("revenue")), yi(row.get("parent_net_profit")),
                     yi(row.get("netcash_operate")),
                     ratio(row.get("cash_conversion_naive")),
                     ratio(row.get("cash_conversion_adjusted")),
                     pct(_mul100(row.get("dep_to_revenue")), 1),
                     pct(_mul100(row.get("nonrecurring_ratio")), 1)])
    L.append("| " + " | ".join(header) + " |")
    L.append("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows:
        L.append("| " + " | ".join(row) + " |")
    L.append("")
    L.append("## 结论分布")
    L.append("")
    L.append("| 主体 | 异常信号 | 结构性特征 | 口径提示 | 高优先级 | 推理模式 | 校验未过 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        fs = r["findings"]
        verification = r["merged"].get("verification") or {}
        L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["company"]["name"],
            sum(1 for f in fs if f.category == "异常信号"),
            sum(1 for f in fs if f.category == "结构性特征"),
            sum(1 for f in fs if f.category == "口径提示"),
            sum(1 for f in fs if f.level == "高"),
            r["agent_result"].get("mode", "-"),
            verification.get("failed", "-")))
    L.append("")
    L.append("> 校验未过：交叉校验闸门判定未能通过的检查项数量。"
             "未通过的科目已在各自报告中降级为「需人工复核」，"
             "本对照表保留该计数，以免读者高估数据质量。")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- 子命令

def cmd_index(cfg: dict, args) -> int:
    """为某一次上传的材料建全文索引（检索功能的入口）。"""
    root = uploads_dir(cfg)
    run_id = args.run or latest_run(root)
    if not run_id:
        print(f"[提示] {root} 下还没有任何上传材料。"
              f"请先运行：python run.py analyze --pdf <财报路径>", file=sys.stderr)
        return 2
    # 轨迹编号每次运行都新生成，材料批次编号由 --run 指定。
    # 两者分开是因为"同一批材料可以被反复分析"——把同一批的分析结果
    # 追加进同一个轨迹文件，会让 seq 重号、两次运行混在一起，
    # 反而破坏了"一次运行一条完整轨迹"这条可追溯性约定。
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    uploads = UploadSet(run_id=run_id, root=root, trace=trace)
    if args.force:
        chunks = os.path.join(uploads.dir, "_index", "chunks.jsonl")
        if os.path.exists(chunks):
            os.remove(chunks)
    count = ensure_index(uploads, trace)
    print(f"[索引] 材料批次 {run_id} 文本块 {count} 个")
    print()
    print(f"执行轨迹：{trace.close()}")
    return 0


def cmd_analyze(cfg: dict, args) -> int:
    try:
        files = collect_pdfs(args.pdf, args.pdf_dir)
    except UploadError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    root = uploads_dir(cfg)
    run_id = args.run or None
    if not files and not run_id:
        print("未指定任何财报文件。用法：python run.py analyze --pdf <财报路径...>\n"
              "说明：本系统只分析你上传的财报，不接受股票代码或公司名称。",
              file=sys.stderr)
        return 2

    # 轨迹编号与材料批次编号分开：轨迹编号标识"这一次运行"，
    # 材料批次编号标识"这一批文件"，后者在界面上是稳定的（同一批材料可反复分析）。
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    uploads = UploadSet(run_id=run_id or trace.run_id, root=root, trace=trace)
    trace.record("uploads_open", run_id=uploads.run_id, directory=uploads.dir)

    failed = []
    for path in files:
        try:
            entry = uploads.add(path)
        except (UploadError, ValueError) as exc:
            failed.append((path, str(exc)))
            print(f"[跳过] {os.path.basename(path)}：{exc}", file=sys.stderr)
            continue
        subject = entry.get("subject") or {}
        print(f"[上传] {entry['filename']}  -> {subject.get('display')} "
              f"{subject.get('report_date')} {subject.get('report_kind_label')} "
              f"（{entry['pages']} 页，抽到科目 "
              f"{sum((entry.get('counts') or {}).values())} 项）")
        for warning in entry.get("warnings") or []:
            print(f"        [注意] {warning}", file=sys.stderr)

    if not uploads.entries:
        print("没有一份文件被成功解析，无法分析。", file=sys.stderr)
        for path, message in failed:
            print(f"  - {os.path.basename(path)}：{message}", file=sys.stderr)
        print(f"执行轨迹：{trace.close()}")
        return 1

    chunks = ensure_index(uploads, trace)
    print(f"[索引] 文本块 {chunks} 个（仅对本次上传材料，全程离线检索）")
    print()

    groups = uploads.groups()
    print(f"[归组] 识别到 {len(groups)} 个主体："
          + "、".join(str(k) for k in groups))
    for key, members in groups.items():
        for note in group_notes(members):
            print(f"        [注意] {key}：{note}", file=sys.stderr)
    print()

    results = []
    for key in groups:
        members = groups[key]
        head = members[0].get("subject") or {}
        others = [str(k) for k in groups if k != key]
        label = f"{subject_name(head, members[0].get('filename',''))}"
        if head.get("code"):
            label += f"（{head['code']}）"
        print(f"[FinAgent] 分析 {label}（{len(members)} 份材料）…")
        trace.record("target", key=key, name=label,
                     report_dates=sorted({(m.get('subject') or {}).get('report_date') or ''
                                          for m in members}))
        since = args.since_year
        if since is None:
            since = _default_since_year(members)
        result = analyze_one(uploads, key, cfg, trace, since, args.periods,
                             verbose=not args.quiet,
                             question=getattr(args, "question", "") or "",
                             others=others)
        if result is not None:
            results.append(result)

    if not results:
        print("没有任何主体完成分析，请检查上面的提示。", file=sys.stderr)
        print(f"执行轨迹：{trace.close()}")
        return 1

    if len(results) > 1:
        path = os.path.join(cfg["output"]["report_dir"], "跨主体对照分析.md")
        os.makedirs(cfg["output"]["report_dir"], exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(build_comparison(results, cfg, trace))
        trace.file_access(path, "write", "跨主体对照报告")

    print()
    for r in results:
        fs = r["findings"]
        print(f"  {r['company']['name']:<10} 结论 {len(fs):>2} 条"
              f"（高 {sum(1 for f in fs if f.level == '高')}）"
              f"  校验未过 {(r['merged'].get('verification') or {}).get('failed', '-')}"
              f"  推理模式 {r['agent_result'].get('mode', '-')}"
              f"  -> {r['report_path']}")
    print()
    print(f"材料批次：{uploads.run_id}（目录 {uploads.dir}）")
    print(f"执行轨迹：{trace.close()}")
    print(f"运行编号：{trace.run_id}")
    return 0


def _default_since_year(members: list) -> int | None:
    """默认聚焦最近两个年度：上传了新报告就不该被陈年数据淹没。"""
    years = []
    for entry in members:
        date = (entry.get("subject") or {}).get("report_date")
        if date:
            try:
                years.append(int(str(date)[:4]))
            except ValueError:
                continue
    return max(years) - 1 if years else None


def cmd_mcp(cfg: dict, args) -> int:
    from finagent.mcp_server import main as mcp_main
    return mcp_main()


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description="FinAgent —— 上市公司财务报告分析智能体")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="为上传的材料建全文索引")
    p_index.add_argument("--run", default=None, help="运行编号；默认取最近一次上传")
    p_index.add_argument("--force", action="store_true", help="强制重新抽取文本")

    p_an = sub.add_parser("analyze", help="分析用户上传的财报（智能体驱动）")
    p_an.add_argument("--pdf", action="append", nargs="+", default=None,
                      help="财报 PDF 路径，可一次给多个或重复传入")
    p_an.add_argument("--pdf-dir", default=None, help="目录内的全部 PDF")
    p_an.add_argument("--run", default=None, help="复用某次上传目录（含其已解析结果）")
    p_an.add_argument("--since-year", type=int, default=None,
                      help="只保留该年份及之后的规则结论；默认聚焦最近两年")
    p_an.add_argument("--periods", type=int, default=6)
    p_an.add_argument("--quiet", action="store_true", help="不打印工具调用过程")
    p_an.add_argument("--question", default="",
                      help="用户的定向追问；会连同标准体检一起交给智能体")

    sub.add_parser("mcp", help="以 MCP 协议暴露工具集")

    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {"index": cmd_index, "analyze": cmd_analyze, "mcp": cmd_mcp}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
