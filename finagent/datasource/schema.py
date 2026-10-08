"""字段口径映射表。

设计要点：
1. 一律不采用数据源自带的 *_YOY 字段，同比/环比由本系统自行计算，
   避免与第三方口径混淆（竞赛要求计算过程可复现）。
2. 每条规范化指标都保留 (报表, 源字段, 中文标签)，用于生成证据链。
"""

from __future__ import annotations

from typing import Dict, NamedTuple


class Field(NamedTuple):
    report: str            # income / cashflow / balance
    field: str             # 数据源字段名
    label: str             # 中文标签（报表原文用词）
    unit: str = "元"
    alt: tuple = ()        # 备选源字段名，按顺序回退

    def candidates(self) -> tuple:
        """返回取值顺序：首选字段 + 备选字段。

        存在的意义：A 股并非只有一套利润表格式。银行/保险/证券三类金融企业
        适用金融业报表格式，其"营业收入"列在 OPERATE_INCOME 而非
        TOTAL_OPERATE_INCOME，减值损失列在 CREDIT_IMPAIRMENT_LOSS 而非
        CREDIT_IMPAIRMENT_INCOME。若只认首选字段，金融企业会整表取空，
        系统看起来"能跑"，实际输出一片空白——这是最危险的静默失败。
        """
        return (self.field,) + tuple(self.alt)


# 规范名 -> 字段口径
FIELD_MAP: Dict[str, Field] = {
    # ---------- 利润表 ----------
    "revenue":                    Field("income", "TOTAL_OPERATE_INCOME", "营业总收入",
                                          alt=("OPERATE_INCOME",)),
    "operating_cost":             Field("income", "OPERATE_COST", "营业成本",
                                          alt=("OPERATE_EXPENSE",)),
    "total_operate_cost":         Field("income", "TOTAL_OPERATE_COST", "营业总成本",
                                          alt=("OPERATE_EXPENSE",)),
    "sale_expense":               Field("income", "SALE_EXPENSE", "销售费用"),
    "manage_expense":             Field("income", "MANAGE_EXPENSE", "管理费用",
                                          alt=("BUSINESS_MANAGE_EXPENSE",)),
    "research_expense":           Field("income", "RESEARCH_EXPENSE", "研发费用"),
    "finance_expense":            Field("income", "FINANCE_EXPENSE", "财务费用"),
    "other_income":               Field("income", "OTHER_INCOME", "其他收益(政府补助等)"),
    "invest_income":              Field("income", "INVEST_INCOME", "投资收益"),
    "fairvalue_change":           Field("income", "FAIRVALUE_CHANGE_INCOME", "公允价值变动收益"),
    "credit_impairment":          Field("income", "CREDIT_IMPAIRMENT_INCOME", "信用减值损失",
                                          alt=("CREDIT_IMPAIRMENT_LOSS",)),
    "asset_impairment":           Field("income", "ASSET_IMPAIRMENT_INCOME", "资产减值损失",
                                          alt=("ASSET_IMPAIRMENT_LOSS",)),
    "asset_disposal":             Field("income", "ASSET_DISPOSAL_INCOME", "资产处置收益"),
    "operating_profit":           Field("income", "OPERATE_PROFIT", "营业利润"),
    "total_profit":               Field("income", "TOTAL_PROFIT", "利润总额"),
    "income_tax":                 Field("income", "INCOME_TAX", "所得税费用"),
    "net_profit":                 Field("income", "NETPROFIT", "净利润(含少数股东)"),
    "parent_net_profit":          Field("income", "PARENT_NETPROFIT", "归属于母公司股东的净利润"),
    "minority_interest":          Field("income", "MINORITY_INTEREST", "少数股东损益"),
    "deduct_parent_net_profit":   Field("income", "DEDUCT_PARENT_NETPROFIT", "扣除非经常性损益后归母净利润"),
    "basic_eps":                  Field("income", "BASIC_EPS", "基本每股收益", "元/股"),

    # ---------- 现金流量表 ----------
    "netcash_operate":            Field("cashflow", "NETCASH_OPERATE", "经营活动产生的现金流量净额"),
    "netcash_invest":             Field("cashflow", "NETCASH_INVEST", "投资活动产生的现金流量净额"),
    "netcash_finance":            Field("cashflow", "NETCASH_FINANCE", "筹资活动产生的现金流量净额"),
    "sales_services":             Field("cashflow", "SALES_SERVICES", "销售商品、提供劳务收到的现金"),
    "total_operate_inflow":       Field("cashflow", "TOTAL_OPERATE_INFLOW", "经营活动现金流入小计"),
    "total_operate_outflow":      Field("cashflow", "TOTAL_OPERATE_OUTFLOW", "经营活动现金流出小计"),
    "receive_tax_refund":         Field("cashflow", "RECEIVE_TAX_REFUND", "收到的税费返还"),
    "receive_other_operate":      Field("cashflow", "RECEIVE_OTHER_OPERATE", "收到其他与经营活动有关的现金"),
    # 补充资料（间接法调节项）
    "depr_fa":                    Field("cashflow", "FA_IR_DEPR", "固定资产折旧、油气资产折耗、生产性生物资产折旧",
                                          alt=("FIXED_ASSET_DEPR",)),
    "depr_biology":               Field("cashflow", "OILGAS_BIOLOGY_DEPR", "其中：生产性生物资产折旧"),
    "amort_ia":                   Field("cashflow", "IA_AMORTIZE", "无形资产摊销"),
    # 金融业现金流量表补充资料把无形资产摊销与长期待摊费用摊销合并为一列。
    # 单独设字段而不是让上面两项都回退到它，是为了避免同一笔金额被重复加总。
    "amort_ia_lpe":               Field("cashflow", "IA_LPE_AMORTIZE", "无形资产及长期待摊费用摊销(合并列示)"),
    "amort_lpe":                  Field("cashflow", "LPE_AMORTIZE", "长期待摊费用摊销"),
    "amort_rou":                  Field("cashflow", "USERIGHT_ASSET_AMORTIZE", "使用权资产摊销"),
    "amort_defer_income":         Field("cashflow", "DEFER_INCOME_AMORTIZE", "递延收益摊销"),
    "impairment_provision":       Field("cashflow", "ASSET_IMPAIRMENT", "资产减值准备(计提额)",
                                          alt=("CREDIT_IMPAIRMENT_INCOME",)),
    "inventory_reduce":           Field("cashflow", "INVENTORY_REDUCE", "存货的减少"),
    "operate_rece_reduce":        Field("cashflow", "OPERATE_RECE_REDUCE", "经营性应收项目的减少"),
    "operate_payable_add":        Field("cashflow", "OPERATE_PAYABLE_ADD", "经营性应付项目的增加"),
    "disposal_longasset_loss":    Field("cashflow", "DISPOSAL_LONGASSET_LOSS", "非流动资产处置损失"),
    "fa_scrap_loss":              Field("cashflow", "FA_SCRAP_LOSS", "固定资产报废损失"),
    "defer_tax":                  Field("cashflow", "DEFER_TAX", "递延所得税"),
    "construct_long_asset":       Field("cashflow", "CONSTRUCT_LONG_ASSET", "购建固定资产、无形资产和其他长期资产支付的现金"),

    # ---------- 资产负债表 ----------
    "total_assets":               Field("balance", "TOTAL_ASSETS", "资产总计"),
    "total_liabilities":          Field("balance", "TOTAL_LIABILITIES", "负债合计"),
    "total_equity":               Field("balance", "TOTAL_EQUITY", "所有者权益合计"),
    "total_parent_equity":        Field("balance", "TOTAL_PARENT_EQUITY", "归属于母公司股东权益合计"),
    "minority_equity":            Field("balance", "MINORITY_EQUITY", "少数股东权益"),
    "total_current_assets":       Field("balance", "TOTAL_CURRENT_ASSETS", "流动资产合计"),
    "total_current_liab":         Field("balance", "TOTAL_CURRENT_LIAB", "流动负债合计"),
    "monetaryfunds":              Field("balance", "MONETARYFUNDS", "货币资金"),
    "inventory_bs":               Field("balance", "INVENTORY", "存货"),
    "fixed_asset":                Field("balance", "FIXED_ASSET", "固定资产"),
    "cip":                        Field("balance", "CIP", "在建工程"),
    "goodwill":                   Field("balance", "GOODWILL", "商誉"),
    "short_loan":                 Field("balance", "SHORT_LOAN", "短期借款"),
    "long_loan":                  Field("balance", "LONG_LOAN", "长期借款"),
}


def by_report(report: str) -> Dict[str, Field]:
    return {k: v for k, v in FIELD_MAP.items() if v.report == report}
