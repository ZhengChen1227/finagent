"""报告生成：把事实、证据与归因组装为结构化报告。

报告的每条结论都绑定证据 ID，读者可据此回溯到数据源字段与计算公式。
事实 / 推论 / 观点在版面上分区呈现，不做混写——这是竞赛明确要求的。
"""

from __future__ import annotations

import datetime as _dt

import pandas as pd

from finagent.metrics.periods import metric_table

YI = 100_000_000.0


def yi(value, digits: int = 2) -> str:
    """金额统一以亿元表述，避免长数字串影响阅读。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        return f"{value / YI:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def pct(value, digits: int = 2, signed: bool = True) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def ratio(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.{digits}f}"


def _table(header, rows) -> str:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _plabel(key) -> str:
    year, q = key
    return f"{year}Q{q}"


def _pct100(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v * 100.0


def build_metrics_block(table: pd.DataFrame, periods: int) -> str:
    """供大模型引用的紧凑指标文本。"""
    keys = ["revenue", "parent_net_profit", "deduct_parent_net_profit", "netcash_operate",
            "gross_margin", "net_margin", "collect_ratio", "dep_to_revenue",
            "cash_conversion_naive", "cash_conversion_adjusted", "effective_tax_rate"]
    cols = [k for k in keys if k in table.columns]
    if not cols:
        return "（无可用指标）"
    return table[cols].tail(periods).round(4).to_string()


def build_report(*, company: dict, frames: dict, table: pd.DataFrame, findings: list,
                 attribution: dict, articulation: list, trace, cfg: dict,
                 periods_shown: int = 6, agent_result: dict | None = None) -> str:
    name = company.get("name", company.get("code"))
    code = company.get("code")
    industry = company.get("industry", "未分类")
    family_label = company.get("report_family_label", "")
    org_name = company.get("org_name", "")
    listing_date = company.get("listing_date", "")
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L: list = []
    add = L.append

    add(f"# {name}（{code}）财务报告分析")
    add("")
    add(f"> 所属行业：{industry}　|　生成时间：{now}　|　运行编号：`{trace.run_id}`")
    if org_name or listing_date:
        add(f"> 公司全称：{org_name or '—'}　|　上市日期：{listing_date or '—'}")
    if family_label:
        _fin = family_label in ("银行", "保险", "证券")
        add(f"> 报表格式：{family_label}"
            f"（{'金融业专用格式' if _fin else '一般工商业格式'}）"
            f"　|　本报告全部指标按该格式的科目口径计算")
    if not company.get("corpus_docs"):
        add(">")
        add("> **证据强度提示**：本地尚未抓取该公司的公告原文，"
            "本报告的归因结论仅基于结构化财务数据与规则引擎推断，"
            "暂无法回溯至公告原文页面。如需原文级证据，请先运行 "
            "`python run.py fetch --code " + str(code).split(".")[0] + "`。")
    add(">")
    add("> 数据来源：巨潮资讯网定期报告（权威出处）、东方财富数据接口（第三方核验源）")
    add(">")
    add("> 计算口径：全部同比、环比与衍生指标均由程序计算并逐条绑定证据 ID，"
        "可凭证据 ID 回溯至数据源字段与计算公式。大语言模型仅参与归因推理，不参与任何计算。")
    add("")

    add("## 执行摘要")
    add("")
    add(attribution.get("summary") or "（无）")
    add("")
    by_cat: dict = {}
    for f in findings:
        by_cat.setdefault(f.category, []).append(f)
    if by_cat:
        rows = [[cat, len(items), "、".join(sorted({i.rule for i in items}))]
                for cat, items in sorted(by_cat.items())]
        add(_table(["结论类别", "条数", "触发规则"], rows))
    else:
        add("本次分析未识别到显著信号。")
    add("")

    add("## 一、关键财务指标")
    add("")
    add("金额单位：亿元；比率类指标按百分数或倍数列示。")
    add("")
    header = ["报告期", "营业总收入", "归母净利润", "扣非归母净利润", "经营现金流净额",
              "毛利率", "归母净利率", "收现比", "折旧摊销/收入", "现金含量(朴素)", "现金含量(调整后)"]
    rows = []
    for key, row in table.tail(periods_shown).iterrows():
        rows.append([
            _plabel(key),
            yi(row.get("revenue")), yi(row.get("parent_net_profit")),
            yi(row.get("deduct_parent_net_profit")), yi(row.get("netcash_operate")),
            pct(_pct100(row.get("gross_margin"))), pct(_pct100(row.get("net_margin"))),
            ratio(row.get("collect_ratio"), 3),
            pct(_pct100(row.get("dep_to_revenue")), 1),
            ratio(row.get("cash_conversion_naive")),
            ratio(row.get("cash_conversion_adjusted")),
        ])
    add(_table(header, rows))
    add("")

    add("## 二、同比与环比（含单季度还原）")
    add("")
    add("A股定期报告披露的是年初至今累计数。下表同时给出累计值与还原后的单季度值；"
        "环比一律基于单季度数计算——用累计数直接相除会得出错误结论。")
    add("")
    for metric, label in (("revenue", "营业总收入"), ("parent_net_profit", "归母净利润")):
        df = frames.get("income")
        if df is None:
            continue
        t = metric_table(df, metric, label)
        if t.empty:
            continue
        add(f"**{label}**（亿元）")
        add("")
        rows = [[r["quarter"], yi(r["cumulative"]), yi(r["single_quarter"]),
                 pct(r["yoy_cum_pct"]), pct(r["yoy_single_pct"]),
                 pct(r["qoq_single_pct"]), r.get("note") or ""]
                for _, r in t.tail(periods_shown).iterrows()]
        add(_table(["报告期", "累计值", "单季度值", "累计同比", "单季度同比", "单季度环比", "口径提示"], rows))
        add("")

    add("## 三、异常信号与结构性特征")
    add("")
    add("本节把结论分为三类：**异常信号**（数据偏离常态，需要解释）、"
        "**结构性特征**（看似异常，实为资产或行业结构使然）、"
        "**口径提示**（计算口径本身存在限制）。三者混为一谈是财务分析中最典型的误判。")
    add("")
    if not findings:
        add("未识别到显著信号。")
    for f in findings:
        add(f"### [{f.level}] {f.title}　规则 `{f.rule}`　报告期 {f.period}")
        add("")
        add(f"- **类别**：{f.category}")
        add(f"- **事实**：{f.statement}")
        if f.interpretation:
            add(f"- **推论**：{f.interpretation}")
        if f.verify_with:
            add(f"- **验证路径**：{f.verify_with}")
        for e in f.evidence:
            d = e.as_dict() if hasattr(e, "as_dict") else e
            add(f"- **证据**：`{d.get('evidence_id', '')}` {d.get('label', '')} = "
                f"{d.get('value', '')}（{d.get('period', '')}）｜口径：{d.get('formula', '')}")
        add("")

    add("## 四、归因分析")
    add("")
    items = attribution.get("items") or []
    if not items:
        add("（无）")
    for item in items:
        add(f"**{item.get('rule', '')}　{item.get('period', '')}"
            f"　置信度：{item.get('confidence', '—')}**")
        add("")
        add(f"- 事实：{item.get('fact', '')}")
        add(f"- 推论：{item.get('inference', '')}")
        if item.get("verify_with"):
            add(f"- 验证路径：{item['verify_with']}")
        add("")
    if attribution.get("cross_company"):
        add("**跨公司可比性**")
        add("")
        add(attribution["cross_company"])
        add("")

    add("## 五、勾稽校验")
    add("")
    add("勾稽校验是数据准确性的第一道防线；若恒等式不成立，后续分析全部不可信。")
    add("")
    if articulation:
        balance = [c for c in articulation if "资产=" in c["check"]]
        indirect = [c for c in articulation if "间接法" in c["check"]]
        if balance:
            passed = sum(1 for c in balance if c["passed"])
            worst = max(c["rel_pct"] for c in balance)
            add(f"- **资产负债表恒等式**（资产 = 负债 + 所有者权益）："
                f"{passed}/{len(balance)} 期通过，最大相对偏差 {worst:.6f}%。")
        if indirect:
            add("")
            add("间接法反推：净利润经非付现项目与营运资本变动调节后，应等于经营活动现金流量净额。")
            add("")
            rows = [[c["period"], f"{c['reconstructed']:,.0f}", f"{c['reported']:,.0f}",
                     f"{c['residual']:,.0f}", pct(c["residual_pct"]), "通过" if c["passed"] else "未通过"]
                    for c in indirect]
            add(_table(["报告期", "反推值", "报表值", "残差", "残差占比", "结论"], rows))
            add("")
            add("残差未作人为抹平。残差偏大说明该项目尚有未纳入的调节项（如其他调整、"
                "生物资产变动等），需回到附注逐项核对——这本身即是后续核查线索。")
    add("")

    add("## 六、事实与推论的划分")
    add("")
    add("| 层次 | 内容 | 依据 |")
    add("|---|---|---|")
    for f in findings:
        add(f"| 事实 | {f.statement.replace('|', '／')} | 数据源字段 + 程序计算 |")
    for item in items:
        inf = str(item.get("inference", "")).replace("|", "／")
        add(f"| 推论 | {inf} | 规则 {item.get('rule', '')}，置信度 {item.get('confidence', '—')} |")
    add("")
    add("本报告不含主观观点性判断；投资结论需在补充估值与行业分析后另行形成。")
    add("")

    add("## 七、证据索引")
    add("")
    rows = []
    seen = set()
    for f in findings:
        for e in f.evidence:
            d = e.as_dict() if hasattr(e, "as_dict") else e
            eid = d.get("evidence_id", "")
            if eid in seen:
                continue
            seen.add(eid)
            rows.append([eid, d.get("label", ""), d.get("period", ""),
                         d.get("formula", ""), d.get("source", "")])
    add(_table(["证据 ID", "指标", "报告期", "口径 / 公式", "来源"], rows) if rows else "（无）")
    add("")

    # ---------------- 八、智能体执行轨迹 ----------------
    if agent_result:
        add("## 八、智能体执行轨迹")
        add("")
        add(f"推理引擎：{'大语言模型 ' + (agent_result.get('model') or '') if agent_result.get('mode') == 'llm' else '确定性离线模式'}"
            f"　|　终止原因：`{agent_result.get('stop_reason')}`")
        add("")
        add("下表是智能体在本次任务中自主发起的工具调用序列。"
            "执行路径由模型现场决定，而非预先写死——这是智能体与脚本的分界。")
        add("")
        rows = [[e["step"], e["tool"], str(e.get("args", ""))[:60],
                 "成功" if e.get("ok") else "失败"]
                for e in agent_result.get("transcript", []) if e.get("type") == "tool"]
        add(_table(["步骤", "工具", "参数", "结果"], rows) if rows else "（未调用工具）")
        add("")
        rows = [["提示词", k, v] for k, v in sorted((agent_result.get("prompt_hashes") or {}).items())]
        rows += [["技能", k, v] for k, v in sorted((agent_result.get("skill_hashes") or {}).items())]
        if rows:
            add("提示词与技能的版本哈希。写入后可用于验证「这份输出由哪一版提示词与技能产生」，"
                "使方法论本身的演进也可追溯：")
            add("")
            add(_table(["类别", "名称", "SHA-256 前缀"], rows))
            add("")

    add("## 九、局限性与后续验证")
    add("")
    add("- 季报不披露现金流量表补充资料，折旧摊销不可得，调整后现金含量仅在中报与年报可得；"
        "本系统在这些报告期留空而非用近似值替代。")
    add("- 数据源为第三方接口，最终以巨潮资讯网定期报告原文为准；关键结论在提交前需逐条回原文核对。")
    add("- 非经常性损益、减值计提依据、会计政策变更等需查阅报表附注，本报告已标注验证路径。")
    for item in (attribution.get("limitations") or []):
        add(f"- {item}")
    add("")
    add("---")
    add("")
    add(f"*本报告由 FinAgent 财务分析智能体生成。运行编号 `{trace.run_id}`，"
        f"完整执行轨迹见 `{trace.path}`，可逐条重放。*")
    return "\n".join(L)
