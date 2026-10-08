"""异常信号规则引擎。

设计哲学：
1. 规则只描述数据事实，归因留给智能体层，两者在数据结构上分离。
2. 明确区分三类结论：
   - 异常信号：数据本身偏离常态，需要解释
   - 结构性特征：看似异常，实为行业/资产结构使然，必须说明而非报警
   - 口径提示：计算口径本身存在限制，提醒读者不要误读
   这一区分是本模块最重要的设计，直接对应竞赛考察的"识别能力"。
   把结构性特征误报为异常，是财务分析中最典型的外行错误。
"""

from __future__ import annotations

import pandas as pd

from finagent.datasource.eastmoney import evidence_id
from finagent.metrics.periods import cumulative_series, metric_table
from finagent.validate.models import Evidence, Finding


# 金融业适用专用报表格式，且其现金流量表结构与工商业不同：
# 客户存款增减、同业拆入、卖出回购等"类筹资"项目计入经营活动现金流，
# 金额动辄数千亿，与净利润完全不在同一量纲上，因此很多工商业适用的
# 指标在金融业身上既不可比也不可解释。这个集合决定这些指标该如何定性。
FINANCIAL_FAMILIES = ("B", "I", "S")


def _eid(code: str, year: int, q: int, metric: str) -> str:
    return evidence_id(code, f"{year}Q{q}", metric)


def _g(series: pd.Series, year: int, q: int):
    if series is None or series.empty:
        return None
    val = series.get((year, q))
    return None if val is None or pd.isna(val) else float(val)


def _abs_growth(cur, base):
    """按绝对额计算的增幅，用于损失类科目（如减值损失）。"""
    if cur is None or base is None:
        return None
    if base == 0:
        return None
    return (abs(cur) - abs(base)) / abs(base) * 100.0


# ---------------------------------------------------------------- 规则 R1

def rule_impairment_jump(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """减值计提突增。损失类科目按绝对额比较，避免负基号导致的比率失真。"""
    thr = float(cfg["thresholds"]["impairment_yoy_pct"])
    min_amt = float(cfg["thresholds"]["impairment_min_amount"])
    cum = cumulative_series(frames["income"], "asset_impairment")
    if cum.empty:
        return []
    findings = []
    for (year, q) in cum.index:
        cur = _g(cum, year, q)
        base = _g(cum, year - 1, q)
        growth = _abs_growth(cur, base)
        if growth is None or cur is None:
            continue
        if growth >= thr and abs(cur) >= min_amt:
            findings.append(Finding(
                rule="R1",
                title="资产减值损失同比大幅增加",
                level="高",
                category="异常信号",
                period=f"{year}Q{q}",
                statement=(f"{year}Q{q} 资产减值损失 {cur:,.0f} 元，"
                           f"上年同期 {base:,.0f} 元，按绝对额增幅 {growth:,.1f}%。"),
                interpretation=("减值计提通常与资产可收回金额下修有关。"
                                "需核对附注确认减值标的（存货跌价、商誉、长期资产等）"
                                "及其计提依据，判断是一次性出清还是持续恶化的起点。"),
                verify_with="半年报/年报附注「资产减值损失」明细及存货跌价准备计提说明",
                evidence=[Evidence("asset_impairment", "资产减值损失", cur, f"{year}Q{q}",
                                   "利润表 ASSET_IMPAIRMENT_INCOME",
                                   "第三方核验源(东方财富)", _eid(code, year, q, "asset_impairment"))],
            ))
    return findings


# ---------------------------------------------------------------- 规则 R2

def rule_cash_conversion(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """利润与现金流关系。

    这里刻意把"结构性正常"与"真实异常"分开处理：
    重资产企业折旧摊销巨大，朴素现金含量必然虚高，这不是异常。
    只有调整后（EBITDA 口径）仍显著偏离 1，才构成需要解释的信号。

    金融业是第三种情况：银行/保险/证券的经营活动现金流包含客户存款、
    同业拆入、卖出回购等"类筹资"项目，与净利润不存在可比关系。
    此时无论比值多高都推不出利润质量问题，只能作为口径提示披露——
    把它当异常信号报警，是对金融企业报表结构不了解的典型误判。
    """
    low = float(cfg["thresholds"]["adjusted_cash_conv_low"])
    high = float(cfg["thresholds"]["adjusted_cash_conv_high"])
    dep_hi = float(cfg["thresholds"]["depr_to_revenue_high_pct"])
    if family in FINANCIAL_FAMILIES:
        return _financial_cash_caliber(code, table, low, high)
    findings = []
    for (year, q) in table.index:
        naive = _g(table["cash_conversion_naive"] if "cash_conversion_naive" in table else None, year, q)
        adj = _g(table["cash_conversion_adjusted"] if "cash_conversion_adjusted" in table else None, year, q)
        dep_r = _g(table["dep_to_revenue"] if "dep_to_revenue" in table else None, year, q)
        period = f"{year}Q{q}"

        # 结构性特征：朴素口径虚高，但调整后回归正常
        if naive is not None and adj is not None and naive >= 3.0 and (low + 0.2) <= adj <= (high - 0.5):
            findings.append(Finding(
                rule="R2a",
                title="现金含量虚高系折旧摊销所致（结构性特征，非异常）",
                level="提示",
                category="结构性特征",
                period=period,
                statement=(f"{period} 经营活动现金流量净额 / 净利润 = {naive:.2f} 倍；"
                           f"折旧摊销占营业总收入 {(dep_r or 0) * 100:.1f}%；"
                           f"加回折旧摊销与减值后，该比值为 {adj:.2f} 倍。"),
                interpretation=("比值从数倍回落至接近 1，说明差额主要由非付现成本（折旧摊销）构成，"
                                "属于重资产行业的结构性特征，不构成利润质量异常的证据。"
                                "若仅凭朴素口径判断，会得出错误结论。"),
                verify_with="现金流量表补充资料与固定资产折旧政策附注",
                evidence=[Evidence("cash_conversion_adjusted", "现金含量(调整后)", adj, period,
                                   "经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提)",
                                   "本系统计算", _eid(code, year, q, "cash_conversion_adjusted"))],
            ))

        # 真实异常：调整后仍显著偏离
        if adj is not None and (adj < low or adj > high):
            findings.append(Finding(
                rule="R2b",
                title="调整后现金含量显著偏离 1",
                level="中",
                category="异常信号",
                period=period,
                statement=(f"{period} 经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提) "
                           f"= {adj:.2f}，偏离正常区间 [{low}, {high}]。"),
                interpretation=("即使在 EBITDA 近似口径下仍不匹配，"
                                "可能来自营运资本剧烈变动、收入确认与收现时点错配，"
                                "或存在跨期确认问题，需要结合应收/应付与存货变动进一步定位。"),
                verify_with="现金流量表补充资料中存货、经营性应收应付项目的变动额",
                evidence=[Evidence("cash_conversion_adjusted", "现金含量(调整后)", adj, period,
                                   "经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提)",
                                   "本系统计算", _eid(code, year, q, "cash_conversion_adjusted"))],
            ))
    return findings


def _financial_cash_caliber(code: str, table: pd.DataFrame,
                            low: float, high: float) -> list[Finding]:
    """金融业现金含量的口径提示，取代工商业的异常判定。

    只取偏离最明显的一个报告期作代表，避免同一口径问题刷屏。
    """
    if "cash_conversion_adjusted" not in table:
        return []
    series = table["cash_conversion_adjusted"].dropna()
    if series.empty:
        return []
    period, value = series.index[-1], float(series.iloc[-1])
    naive = None
    if "cash_conversion_naive" in table:
        naive_series = table["cash_conversion_naive"].dropna()
        if len(naive_series):
            naive = float(naive_series.iloc[-1])
    if low <= value <= high and (naive is None or naive < 3.0):
        return []                       # 落在正常区间，无需提示
    year, q = period
    return [Finding(
        rule="R2c",
        title="金融业现金含量不具可比性（口径提示，非异常）",
        level="提示",
        category="口径提示",
        period=f"{year}Q{q}",
        statement=(f"{year}Q{q} 经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提) "
                   f"= {value:.2f} 倍"
                   + (f"（朴素口径 {naive:.2f} 倍）" if naive is not None else "")
                   + f"，偏离工商业参考区间 [{low}, {high}]。"),
        interpretation=("银行、保险、证券的经营活动现金流包含客户存款增减、同业拆入、"
                        "卖出回购等具有筹资性质的科目，金额与净利润不在同一量纲，"
                        "因此该比值对金融企业不构成利润质量证据。"
                        "本系统将其定性为口径提示而非异常信号；"
                        "判断金融企业利润质量应改用拨备覆盖率、不良率、"
                        "净息差、信用减值损失等专用指标。"),
        verify_with="现金流量表「经营活动产生的现金流量」明细；年报「风险管理」与「拨备」章节",
        evidence=[Evidence("cash_conversion_adjusted", "现金含量(调整后)", value,
                           f"{year}Q{q}",
                           "经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提)",
                           "本系统计算", _eid(code, year, q, "cash_conversion_adjusted"))],
    )]


# ---------------------------------------------------------------- 规则 R3

def rule_tax_anomaly(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """所得税异常：亏损年度仍确认大额所得税费用，或有效税率显著抬升。"""
    findings = []
    total_profit = cumulative_series(frames["income"], "total_profit")
    income_tax = cumulative_series(frames["income"], "income_tax")
    tax_rate = table["effective_tax_rate"] if "effective_tax_rate" in table else None
    high = float(cfg["thresholds"]["tax_rate_high_pct"]) / 100.0

    for (year, q) in cumulative_series(frames["income"], "total_profit").index:
        tp = _g(total_profit, year, q)
        tax = _g(income_tax, year, q)
        if tp is None or tax is None:
            continue
        period = f"{year}Q{q}"
        if tp < 0 and tax > 0:
            findings.append(Finding(
                rule="R3a",
                title="亏损期间仍确认所得税费用",
                level="中",
                category="异常信号",
                period=period,
                statement=f"{period} 利润总额 {tp:,.0f} 元（亏损），同期所得税费用 {tax:,.0f} 元。",
                interpretation=("亏损且当期无应纳税所得额时，通常应确认递延所得税资产并形成所得税收益。"
                                "仍确认费用，可能意味着：未确认递延所得税资产（对未来盈利预期审慎）、"
                                "或母子公司盈亏结构差异导致集团层面无法弥补。"
                                "该信号对判断管理层盈利预期具有参考价值。"),
                verify_with="附注「所得税费用」中递延所得税与未确认递延所得税资产部分",
                evidence=[Evidence("income_tax", "所得税费用", tax, period,
                                   "利润表 INCOME_TAX", "第三方核验源(东方财富)",
                                   _eid(code, year, q, "income_tax"))],
            ))

    if tax_rate is not None:
        for (year, q) in tax_rate.index:
            cur = _g(tax_rate, year, q)
            base = _g(tax_rate, year - 1, q)
            if cur is None or base is None:
                continue
            if cur - base > 0.05 and cur > high:
                findings.append(Finding(
                    rule="R3b",
                    title="有效税率同比抬升",
                    level="提示",
                    category="异常信号",
                    period=f"{year}Q{q}",
                    statement=(f"{year}Q{q} 有效税率 {cur * 100:.2f}%，"
                               f"上年同期 {base * 100:.2f}%，上升 {(cur - base) * 100:.2f} 个百分点。"),
                    interpretation="需区分是盈利结构变化、税收优惠到期，还是不可抵扣项目增加。",
                    verify_with="附注「所得税费用」中的税率调节表",
                    evidence=[Evidence("effective_tax_rate", "有效税率", cur, f"{year}Q{q}",
                                       "所得税费用 / 利润总额", "本系统计算",
                                       _eid(code, year, q, "effective_tax_rate"))],
                ))
    return findings


# ---------------------------------------------------------------- 规则 R4

def rule_nonrecurring(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """非经常性损益占归母净利润比重偏高，提示利润质量。"""
    thr = float(cfg["thresholds"]["nonrecurring_ratio_pct"]) / 100.0
    ratio = table["nonrecurring_ratio"] if "nonrecurring_ratio" in table else None
    if ratio is None:
        return []
    parent = cumulative_series(frames["income"], "parent_net_profit")
    findings = []
    for (year, q) in ratio.index:
        val = _g(ratio, year, q)
        pnp = _g(parent, year, q)
        if val is None or pnp is None or pnp <= 0:
            continue
        if abs(val) >= thr:
            findings.append(Finding(
                rule="R4",
                title="非经常性损益占归母净利润比重偏高",
                level="中" if abs(val) >= 0.4 else "提示",
                category="异常信号",
                period=f"{year}Q{q}",
                statement=(f"{year}Q{q} 非经常性损益 {val * pnp:,.0f} 元，"
                           f"占归母净利润 {val * 100:.1f}%。"),
                interpretation=("扣非后利润与报表利润差距较大，说明当期业绩对非经常项目的依赖较高。"
                                "需拆分政府补助、资产处置、公允价值变动等构成，评估可持续性。"),
                verify_with="年报「非经常性损益明细表」及「其他收益」附注",
                evidence=[Evidence("nonrecurring_ratio", "非经常性损益占归母净利比重", val,
                                   f"{year}Q{q}", "(归母净利润 - 扣非归母净利润) / 归母净利润",
                                   "本系统计算", _eid(code, year, q, "nonrecurring_ratio"))],
            ))
    return findings


# ---------------------------------------------------------------- 规则 R5

def rule_single_quarter_swing(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """单季度环比剧烈波动。必须基于还原后的单季度数，而非累计数。"""
    thr = float(cfg["thresholds"]["qoq_swing_pct"])
    findings = []
    for metric, label in (("parent_net_profit", "归母净利润"), ("revenue", "营业总收入")):
        t = metric_table(frames["income"], metric, label)
        for _, row in t.iterrows():
            qoq = row["qoq_single_pct"]
            if pd.isna(qoq) or abs(qoq) < thr:
                continue
            findings.append(Finding(
                rule="R5",
                title=f"{label}单季度环比剧烈波动",
                level="中" if abs(qoq) >= 100 else "提示",
                category="异常信号",
                period=row["quarter"],
                statement=(f"{row['quarter']} 单季度{label} {row['single_quarter']:,.0f} 元，"
                           f"环比 {qoq:+.2f}%。"),
                interpretation=("环比大幅波动需结合季节性因素判断。"
                                "周期性行业（如生猪养殖）的季节性规律会主导环比变化，"
                                "此时同比比环比更具解释力。"),
                verify_with="季度经营数据公告、行业价格走势",
                evidence=[Evidence(metric, label, float(row["single_quarter"]), row["quarter"],
                                   "累计数差分还原单季度", "本系统计算",
                                   _eid(code, row["year"], row["q"], metric))],
            ))
    return findings


# ---------------------------------------------------------------- 规则 R6

def rule_collect_ratio(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """收现比偏低：收入未同步转化为现金流入。"""
    thr = float(cfg["thresholds"]["collect_ratio_low"])
    ratio = table["collect_ratio"] if "collect_ratio" in table else None
    if ratio is None:
        return []
    findings = []
    for (year, q) in ratio.index:
        val = _g(ratio, year, q)
        if val is None or val >= thr:
            continue
        findings.append(Finding(
            rule="R6",
            title="收现比低于 0.9",
            level="中",
            category="异常信号",
            period=f"{year}Q{q}",
            statement=f"{year}Q{q} 收现比（销售收现 / 营业总收入）= {val:.3f}。",
            interpretation="收入未充分转化为现金，可能源于应收增加或预收减少，需结合应收账款附注核实。",
            verify_with="应收账款账龄附注、现金流量表经营性应收项目变动",
            evidence=[Evidence("collect_ratio", "收现比", val, f"{year}Q{q}",
                               "销售商品提供劳务收到的现金 / 营业总收入", "本系统计算",
                               _eid(code, year, q, "collect_ratio"))],
        ))
    return findings


# ---------------------------------------------------------------- 规则 R7

def rule_dep_intensity(code, frames, cfg, table, family: str = "G") -> list[Finding]:
    """折旧强度提示。这是"结构性特征"，其价值在于指导口径选择。"""
    thr = float(cfg["thresholds"]["depr_to_revenue_high_pct"]) / 100.0
    dep = table["dep_to_revenue"] if "dep_to_revenue" in table else None
    if dep is None:
        return []
    findings = []
    for (year, q) in dep.index:
        val = _g(dep, year, q)
        if val is None or val < thr:
            continue
        findings.append(Finding(
            rule="R7",
            title="折旧摊销占收入比重高（重资产结构）",
            level="提示",
            category="口径提示",
            period=f"{year}Q{q}",
            statement=f"{year}Q{q} 折旧摊销占营业总收入 {val * 100:.1f}%，超过 {thr * 100:.0f}%。",
            interpretation=("重资产结构下，净利润与经营现金流天然存在大额非付现成本缺口。"
                            "分析现金含量、自由现金流时须采用调整后口径，"
                            "直接使用净利润会系统性高估现金转化能力。"),
            verify_with="固定资产折旧政策与折旧年限附注；关注折旧年限变更",
            evidence=[Evidence("dep_to_revenue", "折旧摊销占收入比重", val, f"{year}Q{q}",
                               "(折旧 + 摊销) / 营业总收入", "本系统计算",
                               _eid(code, year, q, "dep_to_revenue"))],
        ))
    return findings


def rule_financial_indicator_caliber(code, frames, cfg, table,
                                     family: str = "G") -> list[Finding]:
    """金融业专用口径提示：若干工商业指标在金融业不具备可比口径。

    这些指标的计算本身没有错，错的是把结果当作工商业语境下的结论来读。
    与其让读者自行困惑"银行为什么没有毛利率"，不如由系统主动声明。
    """
    if family not in FINANCIAL_FAMILIES:
        return []
    missing = []
    if "gross_margin" in table and table["gross_margin"].notna().any():
        missing.append("毛利率（金融业按营业总收入与营业总成本近似计算，"
                       "与工商业「售价-成本」含义不同）")
    if "collect_ratio" not in table or not table["collect_ratio"].notna().any():
        missing.append("收现比（金融业不列示「销售商品、提供劳务收到的现金」）")
    if not missing:
        return []
    # 报告期取表中最新一期：年份过滤器按报告期年份工作，
    # 若这里写"全报告期"会被年份过滤静默丢掉——口径提示恰恰是最不能丢的一类结论。
    year, q = table.index[-1] if len(table.index) else (0, 0)
    period = f"{year}Q{q}" if year else "全报告期"
    return [Finding(
        rule="R8",
        title="金融业指标口径适配说明",
        level="提示",
        category="口径提示",
        period=period,
        statement="下列指标在金融业报表结构下不具备工商业语境下的可比含义：" + "；".join(missing) + "。",
        interpretation=("金融企业适用专用报表格式，科目分类与工商业不同。"
                        "本系统仍按统一公式计算并展示数值，但明确标注其口径限制，"
                        "避免读者将数值直接套用工商业经验。"),
        verify_with="年报「重要会计政策及会计估计」章节中的报表格式说明",
        evidence=[],
    )]


RULES = [
    rule_impairment_jump,
    rule_cash_conversion,
    rule_tax_anomaly,
    rule_nonrecurring,
    rule_single_quarter_swing,
    rule_collect_ratio,
    rule_dep_intensity,
    rule_financial_indicator_caliber,
]


def run_rules(code: str, frames: dict, table: pd.DataFrame, cfg: dict, trace=None,
              since_year: int | None = None, family: str = "G") -> list[Finding]:
    """执行全部规则，可按年份过滤以聚焦近期报告期。

    family 决定若干指标的定性方式（异常信号 / 结构性特征 / 口径提示）：
    同一套公式在工商业与金融业下含义不同，规则层必须知道自己在分析哪一类主体。
    """
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(code, frames, cfg, table, family))
        except Exception as exc:  # 单条规则异常不影响整体
            if trace:
                trace.record("rule_error", rule=rule.__name__, error=repr(exc))
    if since_year is not None:
        # 年份无法解析的结论一律保留：宁可多留一条，也不要静默丢掉
        # 口径提示这类最容易被读者忽略、也最不该被隐藏的结论。
        findings = [f for f in findings
                    if (_year_of(f.period) or since_year) >= since_year]
    findings.sort(key=lambda f: (_LEVEL_ORDER.get(f.level, 9), f.period), reverse=False)
    if trace:
        trace.tool_call(
            "anomaly_rules", {"secucode": code, "rules": len(RULES),
                              "since_year": since_year, "report_family": family},
            {"findings": len(findings),
             "by_category": {c: sum(1 for f in findings if f.category == c)
                             for c in sorted({f.category for f in findings})}},
        )
    return findings


_LEVEL_ORDER = {"高": 0, "中": 1, "提示": 2}


def _year_of(period: str):
    """从报告期字符串取年份；无法解析时返回 None（表示"未知"而非"第 0 年"）。"""
    try:
        return int(str(period)[:4])
    except (ValueError, TypeError):
        return None
