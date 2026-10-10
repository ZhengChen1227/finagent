"""FinAgent 命令行入口。

用法：
    python run.py fetch                       # 抓取公告原文，构建封闭数据环境
    python run.py index                       # 建立公告全文索引
    python run.py analyze                      # 执行完整财务分析（智能体驱动）
    python run.py analyze --code 002714.SZ     # 只分析指定公司
    python run.py analyze --quiet              # 不打印工具调用过程
    python run.py mcp                          # 以 MCP 协议暴露工具集

产物：
    output/reports/*.md      分析报告
    output/reports/*.json    结构化结论（便于程序化复核）
    output/tables/*.csv      指标宽表
    output/traces/*.jsonl    完整执行轨迹，可逐条重放
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from finagent.agent.attributor import offline_attribution
from finagent.agent.loop import AgentLoop
from finagent.config import company_of, load_config
from finagent.corpus.index import CorpusIndex
from finagent.datasource.cninfo import fetch_corpus, load_manifest
from finagent.datasource.codes import bare, market_of, normalize_code
from finagent.datasource.eastmoney import FAMILY_LABEL, EastMoneySource
from finagent.datasource.universe import load_universe
from finagent.datasource.universe import resolve as resolve_stock
from finagent.datasource.universe import search as search_universe
from finagent.metrics import indicators as I
from finagent.report.builder import build_metrics_block, build_report, pct, ratio, yi
from finagent.trace import Trace
from finagent.util import json_default
from finagent.validate.anomaly import run_rules
from finagent.validate.articulation import run_articulation


# ---------------------------------------------------------------- 目标解析

def resolve_target(text: str) -> dict:
    """把用户输入解析成唯一主体，支持简称、代码、带后缀代码三种写法。

    这是"任意上市公司都能分析"的入口：用户写"茅台"、"600519"或"600519.SH"
    都能定位到同一只证券。名录未命中时按代码规则归一化并显式提示，
    不由系统悄悄跳过——跳过会让用户以为这家公司已经被分析过了。
    """
    hit = resolve_stock(text)
    if hit:
        return {"code": hit["code"], "name": hit["name"], "secucode": hit["secucode"],
                "market": hit["market"], "org_id": hit.get("org_id"), "matched": True}
    return {"code": bare(text), "name": "", "secucode": normalize_code(text),
            "market": market_of(text), "org_id": None, "matched": False}


def resolve_inputs(values, cfg: dict) -> list:
    """解析命令行传入的分析对象；未指定则回落到配置中的公司。"""
    texts = list(values or [c["code"] for c in cfg.get("companies", [])])
    out, seen = [], set()
    for text in texts:
        target = resolve_target(text)
        if target["secucode"] in seen:
            continue
        seen.add(target["secucode"])
        if not target["matched"]:
            print(f"[提示] {text} 不在全市场名录中，按代码规则归一化为 "
                  f"{target['secucode']} 继续尝试。")
        out.append(target)
    return out


# ---------------------------------------------------------------- 任务构造

def build_objective(company: dict, cfg: dict, question: str = "") -> str:
    """构造交给智能体的任务描述。

    这段文字就是 Prompt 的一部分：它决定智能体关注什么。
    注意它只给目标与约束，不规定步骤——步骤由模型自己决定。

    question 是用户在界面上自己敲的问题。它被原样附加在任务描述里，
    使同一套工具链既能跑标准体检，也能回答"为什么二季度毛利率掉了"这类
    定向追问——追问内容本身也会进入技能匹配与轨迹，可被复核。
    """
    peers = [c["name"] for c in cfg.get("companies", [])
             if c["code"] != company["code"]]
    peer_line = (f"可对照公司：{'、'.join(peers)}。" if peers else "")
    family_label = company.get("report_family_label", "")
    family_line = (f"该主体适用{family_label}报表格式，"
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
        f"任务：分析 {company['name']}（{company['code']}，{company.get('industry', '')}）"
        f"的报告期业绩变化与异常信号。\n"
        f"{family_line}\n"
        f"要求：\n"
        f"1. 先了解本地有哪些公告原文与报告期可用。\n"
        f"2. 运行规则引擎识别信号，并运行勾稽校验确认数据自洽。\n"
        f"3. 对识别出的异常信号，必须回到公告原文核实原因，不得仅凭规则结论下判断。\n"
        f"4. 注意区分「异常信号」「结构性特征」与「口径提示」三类结论。\n"
        f"5. 重点关注利润与现金流的关系、减值计提、所得税异常、非经常性损益。\n"
        f"{peer_line}"
        f"{focus_line}\n"
        f"证据充分后请输出最终 JSON 结论。"
    )


def to_attribution(agent_result: dict, name: str, code: str, findings: list) -> dict:
    """把智能体的最终结论转换为报告层结构；无结论时回落到离线归因。"""
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
    if not items:
        return offline_attribution(name, code, findings)
    return {
        "summary": final.get("summary", ""),
        "items": items,
        "cross_company": final.get("cross_company", ""),
        "limitations": list(final.get("limitations") or []),
        "unresolved": list(final.get("unresolved") or []),
    }


# ---------------------------------------------------------------- 单公司分析

def analyze_one(code: str, cfg: dict, trace: Trace, since_year: int,
                periods_shown: int, refresh: bool, verbose: bool = True,
                question: str = "") -> dict | None:
    company = company_of(cfg, code)

    with trace.step("fetch_data", secucode=code):
        source = EastMoneySource(cache_dir=cfg["data"]["cache_dir"], trace=trace,
                                 refresh=refresh)
        secucode = source.secucode(code)
        family = source.family(secucode)
        frames = source.frames(code)
        # 配置外的公司（如决赛现场由评委提供的样例数据）没有预设名称与行业，
        # 一律从数据源解析，避免报告里出现"以代码充当公司名、以未分类充当行业"。
        profile = source.profile(code)
        # 名称解析三级回退：config.yaml 的登记 < 数据源主体画像 < 巨潮名录（离线可读）。
        # 后两级是为了让"配置外的主体"也能写出真实公司名，
        # 而不是把代码当成公司名印在报告标题上。
        unresolved = (not company.get("name")
                      or company["name"] in (code, bare(code), secucode))
        if unresolved:
            listing = resolve_stock(bare(secucode)) or {}
            resolved_name = profile.get("name") or listing.get("name")
            if resolved_name:
                company = dict(company, name=resolved_name)
        if not company.get("industry") or company.get("industry") == "未分类":
            if profile.get("industry"):
                company = dict(company, industry=profile["industry"])
        # 公告原文是结论的最终出处。若本地尚未抓取该公司的原文，
        # 必须在报告里说明"结论暂无可核验的原文支撑"，而不是让读者以为
        # 每条结论都能翻到对应公告页——这属于对证据强度的如实披露。
        corpus_docs = len(load_manifest(bare(secucode), cfg["data"]["corpus_dir"]))
        company = dict(company, secucode=secucode, report_family=family,
                       report_family_label=FAMILY_LABEL[family],
                       org_name=profile.get("org_name", ""),
                       listing_date=profile.get("listing_date", ""),
                       province=profile.get("province", ""),
                       corpus_docs=corpus_docs)
        if not corpus_docs:
            print(f"[提示] 本地尚无 {company['name']} 的公告原文，"
                  f"归因结论暂无法回溯到原文。可运行："
                  f"python run.py fetch --code {bare(secucode)}", file=sys.stderr)
            trace.record("corpus_missing", secucode=secucode,
                         hint=f"run.py fetch --code {bare(secucode)}")
    name = company["name"]

    # 三张表全空意味着这家公司确实没有可用数据。此时必须显式报错，
    # 而不是继续生成一份满是空格的"分析报告"——后者会被误读为已完成分析。
    if all(frames[k].empty for k in ("income", "cashflow", "balance")):
        print(f"[跳过] {code}：未取到任何报表数据。请确认代码是否正确"
              f"（当前按 {secucode} 取数，报表族 {FAMILY_LABEL[family]}）。",
              file=sys.stderr)
        trace.record("skip", secucode=secucode, reason="no_statement_data")
        return None

    with trace.step("compute_metrics", secucode=code):
        table, flags = I.compute_all(frames, trace)

    with trace.step("articulation_check", secucode=code):
        articulation = run_articulation(frames, code, cfg, trace)

    with trace.step("anomaly_rules", secucode=code):
        findings = run_rules(code, frames, table, cfg, trace, since_year=since_year,
                             family=family)

    with trace.step("agent_reasoning", secucode=code):
        loop = AgentLoop(cfg, trace,
                         max_steps=int(cfg.get("agent", {}).get("max_steps", 12)),
                         verbose=verbose, focus_code=secucode)
        trace.record("question", text=(question or '').strip() or None)
    agent_result = loop.run(build_objective(company, cfg, question),
                            focus_code=code)

    attribution = to_attribution(agent_result, name, code, findings)

    report_md = build_report(
        company=company, frames=frames, table=table, findings=findings,
        attribution=attribution, articulation=articulation, trace=trace,
        cfg=cfg, periods_shown=periods_shown, agent_result=agent_result,
    )

    report_dir = cfg["output"]["report_dir"]
    table_dir = cfg["output"]["table_dir"]
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(table_dir, exist_ok=True)

    stem = f"{code.split('.')[0]}_{name}"
    md_path = os.path.join(report_dir, f"{stem}_财务分析报告.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report_md)
    trace.file_access(md_path, "write", "分析报告")

    payload = {
        "run_id": trace.run_id,
        "company": company,
        "findings": [f.as_dict() for f in findings],
        "attribution": attribution,
        "articulation": articulation,
        "agent": {
            "mode": agent_result.get("mode"),
            "stop_reason": agent_result.get("stop_reason"),
            "llm_calls": agent_result.get("llm_calls"),
            "llm_usage": agent_result.get("llm_usage"),
            "prompt_hashes": agent_result.get("prompt_hashes"),
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

    trace.result(secucode=code, findings=len(findings),
                 high=sum(1 for f in findings if f.level == "高"),
                 agent_mode=agent_result.get("mode"),
                 report=md_path)
    return {"company": company, "findings": findings, "table": table,
            "attribution": attribution, "agent_result": agent_result,
            "report_path": md_path, "json_path": json_path, "csv_path": csv_path}


# ---------------------------------------------------------------- 跨公司对照

def _mul100(value):
    try:
        import pandas as pd
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return value * 100.0


def build_comparison(results: list, cfg: dict, trace: Trace) -> str:
    """跨公司对照：同一套指标口径与规则集，检验结论的可比性与差异。"""
    L = ["# 跨公司对照分析", "",
         f"> 运行编号：`{trace.run_id}`", "",
         "本对照使用完全相同的指标口径与规则集处理各家公司，"
         "目的是检验系统在结构差异极大的样本上是否保持稳定，"
         "以及不同资产结构如何导致不同结论。", ""]

    header = ["公司", "最新报告期", "营业总收入(亿)", "归母净利润(亿)", "经营现金流(亿)",
              "现金含量(朴素)", "现金含量(调整后)", "折旧摊销/收入"]
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
                     pct(_mul100(row.get("dep_to_revenue")), 1)])
    L.append("| " + " | ".join(header) + " |")
    L.append("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows:
        L.append("| " + " | ".join(row) + " |")
    L.append("")
    L.append("## 结论分布")
    L.append("")
    L.append("| 公司 | 异常信号 | 结构性特征 | 口径提示 | 高优先级 | 推理模式 |")
    L.append("|---|---|---|---|---|---|")
    for r in results:
        fs = r["findings"]
        L.append("| {} | {} | {} | {} | {} | {} |".format(
            r["company"]["name"],
            sum(1 for f in fs if f.category == "异常信号"),
            sum(1 for f in fs if f.category == "结构性特征"),
            sum(1 for f in fs if f.category == "口径提示"),
            sum(1 for f in fs if f.level == "高"),
            r["agent_result"].get("mode", "-")))
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- 子命令

def cmd_fetch(cfg: dict, args) -> int:
    """抓取公告原文。环境构建环节同样留下轨迹，使文件访问全程可核验。"""
    trace = Trace(out_dir=cfg["output"]["trace_dir"])

    with trace.step("load_universe"):
        stock_map = {i["code"]: i for i in load_universe()}
        trace.tool_call("load_universe", {}, {"securities": len(stock_map)})

    texts = args.code or [c["code"].split(".")[0] for c in cfg.get("companies", [])]
    for text in texts:
        target = resolve_target(text)
        code = target["code"]
        info = stock_map.get(code)
        if not info:
            print(f"未找到 {code} 在全市场名录中的登记信息，跳过")
            continue
        print(f"[抓取] {code} {info.get('name', '')} orgId={info['org_id']} ...")
        with trace.step("fetch_corpus", secucode=code):
            manifest = fetch_corpus(code, info["org_id"],
                                    corpus_dir=cfg["data"]["corpus_dir"], limit=args.limit)
            trace.tool_call("fetch_corpus",
                            {"code": code, "orgId": info["org_id"], "limit": args.limit},
                            {"documents": len(manifest),
                             "bytes": sum(e.get("size", 0) for e in manifest)})
        for entry in manifest:
            print(f"    {entry['date']}  {entry['kind']:<7} "
                  f"{entry['size'] / 1e6:6.2f}MB  {entry['title']}")
            trace.file_access(entry["path"], "write",
                              f"公告原文 {entry['kind']} {entry['date']}")
        if not manifest:
            print(f"    [警告] 未取到 {code} 的定期报告原文。"
                  f"请确认该代码在巨潮有定期报告披露，以及网络可达。")

    print()
    print(f"执行轨迹：{trace.close()}")
    print(f"运行编号：{trace.run_id}")
    return 0


def cmd_index(cfg: dict, args) -> int:
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    index = CorpusIndex(corpus_dir=cfg["data"]["corpus_dir"],
                        index_dir=cfg["data"]["index_dir"])
    count = index.build(force=args.force, trace=trace)
    print(f"[索引] 文本块 {count} 个")
    print()
    print(f"执行轨迹：{trace.close()}")
    print(f"运行编号：{trace.run_id}")
    return 0


def cmd_analyze(cfg: dict, args) -> int:
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    targets = resolve_inputs(args.code, cfg)
    if not targets:
        print("未指定分析对象。", file=sys.stderr)
        return 2

    results = []
    for target in targets:
        label = target["secucode"] + (f" {target['name']}" if target["name"] else "")
        print(f"[FinAgent] 分析 {label} ...")
        trace.record("target", input=target.get("code"), secucode=target["secucode"],
                     name=target["name"], matched=target["matched"])
        result = analyze_one(target["secucode"], cfg, trace, args.since_year,
                             args.periods, args.refresh, verbose=not args.quiet,
                             question=getattr(args, "question", "") or "")
        if result is not None:
            results.append(result)

    if not results:
        print("没有任何公司完成分析，请检查上面的提示。", file=sys.stderr)
        print(f"执行轨迹：{trace.close()}")
        return 1

    if len(results) > 1:
        path = os.path.join(cfg["output"]["report_dir"], "跨公司对照分析.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(build_comparison(results, cfg, trace))
        trace.file_access(path, "write", "跨公司对照报告")

    print()
    for r in results:
        fs = r["findings"]
        print(f"  {r['company']['name']:<8} 结论 {len(fs):>2} 条"
              f"（高 {sum(1 for f in fs if f.level == '高')}）"
              f"  推理模式 {r['agent_result'].get('mode', '-')}"
              f"  -> {r['report_path']}")
    print()
    print(f"执行轨迹：{trace.close()}")
    print(f"运行编号：{trace.run_id}")
    return 0


def cmd_search(cfg: dict, args) -> int:
    """检索全市场名录，确认公司代码与数据可得性。

    存在的意义：用户在动手分析之前，应该能低成本确认"这家公司系统认不认识"。
    """
    for text in args.keyword:
        hits = search_universe(text, limit=args.limit)
        if not hits:
            print(f"[{text}] 未在全市场名录中找到匹配主体。")
            continue
        print(f"[{text}] 命中 {len(hits)} 条：")
        for item in hits:
            print(f"    {item['secucode']:<12} {item['name']:<10} "
                  f"上市地={item['market']:<3} orgId={item.get('org_id')}")
    return 0


def cmd_mcp(cfg: dict, args) -> int:
    from finagent.mcp_server import main as mcp_main
    return mcp_main()


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run.py",
                                     description="FinAgent —— 上市公司财务报告分析智能体")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="抓取公告原文，构建封闭数据环境")
    p_fetch.add_argument("--code", action="append", default=None)
    p_fetch.add_argument("--limit", type=int, default=3, help="每类报告最多保留几份")

    p_index = sub.add_parser("index", help="建立公告全文索引")
    p_index.add_argument("--force", action="store_true", help="强制重新抽取文本")

    p_an = sub.add_parser("analyze", help="执行财务分析（智能体驱动）")
    p_an.add_argument("--code", action="append", default=None)
    p_an.add_argument("--since-year", type=int, default=2025)
    p_an.add_argument("--periods", type=int, default=6)
    p_an.add_argument("--refresh", action="store_true")
    p_an.add_argument("--quiet", action="store_true", help="不打印工具调用过程")
    p_an.add_argument("--question", default="",
                      help="用户的定向追问；会连同标准体检一起交给智能体")

    p_se = sub.add_parser("search", help="在全市场名录中检索公司代码")
    p_se.add_argument("keyword", nargs="+", help="公司简称或代码，可用空格分隔多个")
    p_se.add_argument("--limit", type=int, default=10)

    sub.add_parser("mcp", help="以 MCP 协议暴露工具集")

    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {"fetch": cmd_fetch, "index": cmd_index, "analyze": cmd_analyze,
                "search": cmd_search, "mcp": cmd_mcp}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
