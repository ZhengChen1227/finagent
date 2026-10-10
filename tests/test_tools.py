"""工具层测试：模拟智能体将要发起的调用序列。

工具层是"结论能否被核对"的关键：每一个数字都要能说清来自哪份文件的第几页。
因此这里除了验证取值正确，还要验证证据链（文件名 + 页码）确实存在。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F
from finagent.agent.tools import build_tools
from finagent.trace import Trace

RESULTS = []


def check(title: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((title, bool(ok)))
    print(f"  [{'通过' if ok else '失败'}] {title}" + (f"  {detail}" if detail else ""))


def show(title, payload, limit=320):
    text = json.dumps(payload, ensure_ascii=False, default=str)
    print(f"  · {title}: {text[:limit]}{'…' if len(text) > limit else ''}")


def main() -> int:
    us, key = F.uploads("002714")
    if us is None:
        print(F.missing_message("002714"))
        return 0
    cfg = F.load_cfg()
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    reg = build_tools(cfg, trace, uploads=us, focus_key=key)
    missing = [n for n in ("list_materials", "list_periods", "get_indicator",
                           "compute_growth", "run_anomaly_rules", "check_articulation",
                           "list_verification", "search_disclosure", "read_page",
                           "list_skills", "load_skill", "compare_subjects")
               if n not in reg]
    check("工具齐备（12 个）", not missing, str(missing))

    # --- 材料清单：界面上的"我到底在分析什么" ---
    materials = reg["list_materials"].handler(None)
    show("list_materials", {k: v for k, v in materials.items() if k != "items"})
    doc = (materials.get("items") or [{}])[0]
    check("材料清单含文件名／主体／报告期／页数",
          bool(doc.get("file")) and bool(doc.get("subject"))
          and bool(doc.get("report_date")) and bool(doc.get("pages")), str(doc.get("file")))
    check("材料清单声明不联网取数",
          "不联网" in (materials.get("note") or ""), str(materials.get("note"))[:60])

    periods = reg["list_periods"].handler(key)
    check("报告期清单（累计口径）", "2026Q2" in periods.get("periods", []),
          str(periods.get("periods")))

    # --- 取数与同比 ---
    got = reg["get_indicator"].handler("revenue", "2026Q2", key)
    show("get_indicator 营业总收入 2026Q2", got)
    check("营业总收入取值", got.get("value") == 59410306384.59, str(got.get("value")))
    check("取值带证据编号与页码",
          bool(got.get("evidence_id")) and got.get("page") is not None,
          f"{got.get('evidence_id')} 第{got.get('page')}页")

    growth = reg["compute_growth"].handler("revenue", "2026Q2", key)
    show("compute_growth 营业总收入 2026Q2", growth)
    check("同比与披露一致（-22.30%）",
          abs((growth.get("yoy_cum_pct") or 0) + 22.30) < 0.05,
          str(growth.get("yoy_cum_pct")))
    check("环比不可计算时逐条说明缺什么，不臆造",
          growth.get("single_quarter") is None and bool(growth.get("unavailable")),
          str(growth.get("unavailable"))[:110])

    # --- 规则层 ---
    findings = reg["run_anomaly_rules"].handler(2025, key)
    show("run_anomaly_rules", {k: v for k, v in findings.items() if k != "findings"})
    check("规则结论非空", (findings.get("count") or 0) > 0, str(findings.get("count")))

    artic = reg["check_articulation"].handler(key)
    show("check_articulation", artic)
    ident = artic.get("balance_identity") or {}
    check("资产=负债+权益逐期通过",
          ident.get("periods") and ident.get("passed") == ident.get("periods"),
          str(ident))
    check("间接法反推有结论", bool(artic.get("indirect_method")),
          str(len(artic.get("indirect_method") or [])) + " 期")

    ver = reg["list_verification"].handler(key)
    show("list_verification", {k: v for k, v in ver.items() if k != "checks"})
    check("交叉校验闸门结论可读", (ver.get("total") or 0) > 0, str(ver.get("total")))

    # --- 原文检索与整页回读 ---
    hits = reg["search_disclosure"].handler("存货跌价准备", None, 2)
    show("search_disclosure 存货跌价准备", {k: v for k, v in hits.items() if k != "results"})
    top = (hits.get("results") or [{}])[0]
    check("检索命中带引用（文件名＋页码）",
          bool(top.get("citation")) and bool(top.get("page")), str(top.get("citation")))
    check("检索声明只在本批材料内",
          "上传" in (hits.get("note") or ""), str(hits.get("note"))[:60])

    if top.get("doc_id"):
        page = reg["read_page"].handler(top["doc_id"], top["page"])
        check("整页回读成功", bool(page.get("text")), f"{len(page.get('text') or '')} 字")

    # --- Skill（分析方法论）---
    skills = reg["list_skills"].handler()
    names = [s.get("name") for s in skills.get("skills") or []]
    check("Skill 清单非空", bool(names), str(names))
    if names:
        body = reg["load_skill"].handler(names[0])
        check("Skill 正文可加载", len(body.get("content") or "") > 200,
              f"{names[0]} {len(body.get('content') or '')} 字")

    print("-" * 74)
    bad = [t for t, ok in RESULTS if not ok]
    print(f"工具层测试：{len(RESULTS) - len(bad)}/{len(RESULTS)} 项通过")
    for title in bad:
        print("  未通过：" + title)
    trace.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
