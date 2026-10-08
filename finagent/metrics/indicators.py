"""衍生财务指标（纯函数式计算，不依赖大模型，保证可复现）。

重点指标：adjusted_cash_conversion（调整后现金含量）
    朴素做法 netcash_operate / net_profit 对重资产企业会严重失真：
    折旧摊销一年数百亿，会把比值推到 8 倍以上，容易被误读成"异常信号"。
    本系统改用 netcash_operate / (net_profit + 折旧摊销 + 减值计提)，
    即 EBITDA 近似口径，重资产与轻资产企业才具备横向可比性。
    这是本模块区分"结构性正常"与"真实异常"的核心设计。

另一条口径约定：
    现金含量类指标的分母一律采用"净利润(含少数股东)"而非"归母净利润"，
    因为经营活动现金流量净额覆盖的是合并报表整体，与归母口径不匹配。
    分母非正时比率不适用，统一置空并打标，绝不输出误导性数字。
"""

from __future__ import annotations

from typing import Callable, NamedTuple

import pandas as pd

from finagent.metrics.periods import cumulative_series

REPORT_ORDER = ("income", "cashflow", "balance")


def _cum(frames: dict, metric: str) -> pd.Series:
    """在全部报表中定位指标并返回累计值序列。"""
    for name in REPORT_ORDER:
        df = frames.get(name)
        if df is not None and metric in df.columns:
            return cumulative_series(df, metric)
    return pd.Series(dtype="float64")


def _add(*series: pd.Series) -> pd.Series:
    """多个序列按报告期相加；某期全为空则留空，不臆造为 0。"""
    items = [s for s in series if s is not None and not s.empty]
    if not items:
        return pd.Series(dtype="float64")
    return pd.concat(items, axis=1).sum(axis=1, min_count=1).sort_index()


def _ratio(numerator: pd.Series, denominator: pd.Series):
    """安全比率：分母非正时置空并打标，避免输出无经济含义的数字。"""
    if numerator.empty or denominator.empty:
        return pd.Series(dtype="float64"), {}
    aligned = pd.concat([numerator.rename("n"), denominator.rename("d")], axis=1).dropna()
    values, flags = {}, {}
    for key, row in aligned.iterrows():
        if row["d"] <= 0:
            values[key] = None
            flags[key] = "分母非正，比率不适用"
        else:
            values[key] = row["n"] / row["d"]
    return pd.Series(values).sort_index(), flags


# ---------------- 构成项 ----------------

def dep_amort(frames: dict) -> pd.Series:
    """折旧与摊销合计：固定资产折旧 + 无形资产摊销 + 长期待摊摊销 + 使用权资产摊销。

    金融业补充资料把"无形资产摊销"与"长期待摊费用摊销"合并为一列，
    此时逐期回退到合并列。绝不能把合并列同时填进两个分项——
    那会把同一笔金额加总两次，直接污染调整后现金含量的分母。
    """
    split = _add(_cum(frames, "amort_ia"), _cum(frames, "amort_lpe"))
    combined = _cum(frames, "amort_ia_lpe")
    if split.empty:
        amort = combined
    elif combined.empty:
        amort = split
    else:
        amort = split.combine_first(combined)
    return _add(_cum(frames, "depr_fa"), amort, _cum(frames, "amort_rou"))


def impairment(frames: dict) -> pd.Series:
    """资产减值准备计提额（取自现金流量表补充资料，非利润表损失额）。"""
    return _cum(frames, "impairment_provision")


def ebitda_proxy(frames: dict) -> pd.Series:
    """EBITDA 近似值 = 净利润 + 折旧摊销 + 减值计提。"""
    return _add(_cum(frames, "net_profit"), dep_amort(frames), impairment(frames))


def nonrecurring(frames: dict) -> pd.Series:
    """非经常性损益 = 归母净利润 - 扣非归母净利润。"""
    return _cum(frames, "parent_net_profit") - _cum(frames, "deduct_parent_net_profit")


# ---------------- 指标实现（均返回 (序列, 标记字典)） ----------------

def _cash_conversion_naive(frames):
    return _ratio(_cum(frames, "netcash_operate"), _cum(frames, "net_profit"))


def _cash_conversion_adjusted(frames):
    """调整后现金含量。仅在折旧摊销可得的报告期计算。

    重要：季报不披露现金流量表补充资料（间接法调节表），折旧摊销不可得。
    若此时仍按公式计算，分母会静默退化为"仅净利润"，指标摇身变成朴素口径
    并给出一个看起来正常的假值——这是隐蔽且危险的计算错误。
    本实现选择在这些报告期直接留空，宁可缺失也不输出不可比数字。
    """
    da = dep_amort(frames)
    if da.empty:
        return pd.Series(dtype="float64"), {}
    denom = _add(_cum(frames, "net_profit"), da, impairment(frames)).reindex(da.index)
    num = _cum(frames, "netcash_operate").reindex(da.index)
    return _ratio(num, denom)


def _gross_margin(frames):
    return _ratio(_cum(frames, "revenue") - _cum(frames, "operating_cost"), _cum(frames, "revenue"))


def _net_margin(frames):
    return _ratio(_cum(frames, "parent_net_profit"), _cum(frames, "revenue"))


def _deduct_net_margin(frames):
    return _ratio(_cum(frames, "deduct_parent_net_profit"), _cum(frames, "revenue"))


def _nonrecurring_ratio(frames):
    return _ratio(nonrecurring(frames), _cum(frames, "parent_net_profit"))


def _effective_tax_rate(frames):
    return _ratio(_cum(frames, "income_tax"), _cum(frames, "total_profit"))


def _collect_ratio(frames):
    return _ratio(_cum(frames, "sales_services"), _cum(frames, "revenue"))


def _dep_to_revenue(frames):
    return _ratio(dep_amort(frames), _cum(frames, "revenue"))


def _fcf(frames):
    return _cum(frames, "netcash_operate") - _cum(frames, "construct_long_asset")


def _minority_ratio(frames):
    return _ratio(_cum(frames, "minority_interest"), _cum(frames, "net_profit"))


# 需要与衍生指标同表呈现的基础科目（报表原始口径的累计值）。
# 报告层直接消费宽表，避免二次取数造成口径不一致。
BASE_METRICS = [
    # 利润表
    "revenue", "operating_cost", "total_operate_cost",
    "parent_net_profit", "deduct_parent_net_profit", "net_profit",
    "total_profit", "income_tax", "operating_profit",
    "sale_expense", "manage_expense", "research_expense", "finance_expense",
    "asset_impairment", "credit_impairment", "other_income", "invest_income",
    "fairvalue_change", "asset_disposal", "minority_interest",
    # 现金流量表主表
    "netcash_operate", "netcash_invest", "netcash_finance", "sales_services",
    "total_operate_inflow", "total_operate_outflow", "construct_long_asset",
    "receive_tax_refund", "receive_other_operate",
    # 现金流量表补充资料（间接法调节项）——这组科目是判断非付现成本的关键，
    # 缺了它们，模型就无法自行拆解利润与现金流的差额。
    "depr_fa", "depr_biology", "amort_ia", "amort_lpe", "amort_ia_lpe", "amort_rou",
    "amort_defer_income", "impairment_provision", "inventory_reduce",
    "operate_rece_reduce", "operate_payable_add", "disposal_longasset_loss",
    "fa_scrap_loss", "defer_tax",
    # 资产负债表
    "total_assets", "total_liabilities", "total_equity", "total_parent_equity",
    "minority_equity", "fixed_asset", "inventory_bs", "monetaryfunds",
    "cip", "goodwill", "short_loan", "long_loan",
]


class Indicator(NamedTuple):
    key: str
    label: str
    formula: str
    fn: Callable


INDICATORS = [
    Indicator("cash_conversion_naive", "现金含量(朴素)",
              "经营活动现金流量净额 / 净利润(含少数股东)", _cash_conversion_naive),
    Indicator("cash_conversion_adjusted", "现金含量(调整后/EBITDA口径)",
              "经营活动现金流量净额 / (净利润 + 折旧摊销 + 减值计提)", _cash_conversion_adjusted),
    Indicator("gross_margin", "毛利率", "(营业总收入 - 营业成本) / 营业总收入", _gross_margin),
    Indicator("net_margin", "归母净利率", "归母净利润 / 营业总收入", _net_margin),
    Indicator("deduct_net_margin", "扣非归母净利率", "扣非归母净利润 / 营业总收入", _deduct_net_margin),
    Indicator("nonrecurring_ratio", "非经常性损益占归母净利比重",
              "(归母净利润 - 扣非归母净利润) / 归母净利润", _nonrecurring_ratio),
    Indicator("effective_tax_rate", "有效税率", "所得税费用 / 利润总额", _effective_tax_rate),
    Indicator("collect_ratio", "收现比", "销售商品提供劳务收到的现金 / 营业总收入", _collect_ratio),
    Indicator("dep_to_revenue", "折旧摊销占收入比重", "(折旧+摊销) / 营业总收入", _dep_to_revenue),
    Indicator("free_cash_flow", "自由现金流(近似)",
              "经营活动现金流量净额 - 购建长期资产支付的现金", _fcf),
    Indicator("minority_ratio", "少数股东损益占比", "少数股东损益 / 净利润(含少数股东)", _minority_ratio),
]

LABELS = {ind.key: ind.label for ind in INDICATORS}
FORMULAS = {ind.key: ind.formula for ind in INDICATORS}


def compute_all(frames: dict, trace=None) -> tuple[pd.DataFrame, dict]:
    """计算全部指标，返回 (指标宽表, 标记字典)。

    宽表索引为 (年, 年内序号)，列名为指标 key。
    """
    values: dict = {}
    flags: dict = {}
    for metric in BASE_METRICS:
        series = _cum(frames, metric)
        if not series.empty:
            values[metric] = series
    for ind in INDICATORS:
        result = ind.fn(frames)
        series, flag_map = result if isinstance(result, tuple) else (result, {})
        if series is None or series.empty:
            continue
        values[ind.key] = series
        for key, note in (flag_map or {}).items():
            flags.setdefault(key, {})[ind.key] = note
        if trace is not None:
            latest = series.dropna()
            trace.compute(
                ind.key, ind.formula,
                inputs={"periods": len(series)},
                output={"latest_period": _fmt_key(latest.index[-1]) if len(latest) else None,
                        "latest_value": float(latest.iloc[-1]) if len(latest) else None},
            )
    table = pd.DataFrame(values).sort_index()
    return table, flags


def _fmt_key(key) -> str:
    year, q = key
    return f"{year}Q{q}"
