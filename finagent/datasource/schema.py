"""报表科目口径表：canonical 字段 ↔ 财报原文科目名。

设计要点：

1. 一律不采用报告自带的同比/环比数值，同比与环比由本系统自行计算，
   避免与披露口径混淆（竞赛要求计算过程可复现）。报告披露的同比只用于
   交叉校验——两者对不上，本身就是"会计口径变化"的第一手线索。

2. 匹配采用"归一化之后的全等比较"，**不做模糊匹配**。模糊匹配会把
   "其中：对联营企业和合营企业的投资收益"错认成"投资收益"，
   于是明细行被当成合计行，产出一个偏小的数字。这类错误不会抛异常，
   只会静静写进报告，是最危险的一种错。

3. 归一化会剥掉序号前缀（一、／（一）／1.／加：／减：／其中：）与
   括号内容。之所以敢大范围剥括号：中文财报里括号内的内容几乎都是
   填表说明（"（损失以"－"号填列）"）或等价表述（"（或股东权益）"），
   科目名的实质部分一定在括号之外。
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, NamedTuple


class Field(NamedTuple):
    report: str                    # income / cashflow / balance
    label: str                     # 中文标签（用于报告与工具返回）
    synonyms: tuple = ()           # 财报原文中可接受的写法（归一化后比较）
    unit: str = "元"
    area: str = "main"             # main=主表；supplement=现金流量表补充资料

    def candidates(self) -> tuple:
        """可接受的原文写法（含标签本身）。"""
        return (self.label,) + tuple(self.synonyms)


FIELD_MAP: Dict[str, Field] = {
    # ---------- 合并利润表 ----------
    "revenue": Field("income", "营业总收入", ("营业收入",)),
    "operating_cost": Field("income", "营业成本"),
    # 银行与保险的利润表没有"营业总成本"这一行，对应的是"营业支出"。
    # 不收这个写法，银行/保险的营业利润率就永远算不出来。
    "total_operate_cost": Field("income", "营业总成本", ("营业支出",)),
    "sale_expense": Field("income", "销售费用"),
    "manage_expense": Field("income", "管理费用"),
    "research_expense": Field("income", "研发费用"),
    "finance_expense": Field("income", "财务费用"),
    "other_income": Field("income", "其他收益"),
    "invest_income": Field("income", "投资收益"),
    "fairvalue_change": Field("income", "公允价值变动收益"),
    "credit_impairment": Field("income", "信用减值损失"),
    "asset_impairment": Field("income", "资产减值损失"),
    "asset_disposal": Field("income", "资产处置收益"),
    "operating_profit": Field("income", "营业利润"),
    "total_profit": Field("income", "利润总额"),
    "income_tax": Field("income", "所得税费用"),
    "net_profit": Field("income", "净利润"),
    "parent_net_profit": Field("income", "归属于母公司股东的净利润",
                               ("归属于母公司所有者的净利润",)),
    "minority_interest": Field("income", "少数股东损益"),
    "deduct_parent_net_profit": Field("income", "扣非归母净利润", (
        "扣除非经常性损益后归属于母公司股东的净利润",
        "归属于母公司股东的扣除非经常性损益的净利润",
        "归属于母公司所有者的扣除非经常性损益的净利润",
        "扣除非经常性损益后的归属于母公司股东的净利润",
    )),
    "basic_eps": Field("income", "基本每股收益", unit="元/股"),

    # ---------- 合并现金流量表（主表）----------
    "netcash_operate": Field("cashflow", "经营活动产生的现金流量净额"),
    "netcash_invest": Field("cashflow", "投资活动产生的现金流量净额"),
    "netcash_finance": Field("cashflow", "筹资活动产生的现金流量净额"),
    "sales_services": Field("cashflow", "销售商品、提供劳务收到的现金"),
    "total_operate_inflow": Field("cashflow", "经营活动现金流入小计"),
    "total_operate_outflow": Field("cashflow", "经营活动现金流出小计"),
    "receive_tax_refund": Field("cashflow", "收到的税费返还"),
    "receive_other_operate": Field("cashflow", "收到其他与经营活动有关的现金"),
    "construct_long_asset": Field(
        "cashflow", "购建固定资产、无形资产和其他长期资产支付的现金"),

    # ---------- 现金流量表补充资料（间接法调节项）----------
    # 这组科目是拆解"利润与现金流差额"的关键：缺了它们，
    # 就只能看到利润与现金流背离，却说不出背离由什么构成。
    "depr_fa": Field("cashflow", "固定资产折旧、油气资产折耗、生产性生物资产折旧",
                     ("固定资产折旧",), area="supplement"),
    "depr_biology": Field("cashflow", "生产性生物资产折旧", area="supplement"),
    "amort_ia": Field("cashflow", "无形资产摊销", area="supplement"),
    "amort_lpe": Field("cashflow", "长期待摊费用摊销", area="supplement"),
    "amort_ia_lpe": Field("cashflow", "无形资产及长期待摊费用摊销",
                          ("无形资产和长期待摊费用摊销",), area="supplement"),
    # 同一笔摊销在不同公司叫法不同：有的写"使用权资产摊销"，有的写"使用权资产折旧"。
    # 少收一个写法，调整后现金含量的分母就会偏小，指标随之系统性失真。
    "amort_rou": Field("cashflow", "使用权资产摊销", ("使用权资产折旧",), area="supplement"),
    "amort_defer_income": Field("cashflow", "递延收益摊销", area="supplement"),
    "impairment_provision": Field("cashflow", "资产减值准备", area="supplement"),
    "inventory_reduce": Field("cashflow", "存货的减少", area="supplement"),
    "operate_rece_reduce": Field("cashflow", "经营性应收项目的减少", area="supplement"),
    "operate_payable_add": Field("cashflow", "经营性应付项目的增加", area="supplement"),
    "disposal_longasset_loss": Field("cashflow", "处置固定资产、无形资产和其他长期资产的损失",
                                     area="supplement"),
    "fa_scrap_loss": Field("cashflow", "固定资产报废损失", area="supplement"),
    "defer_tax": Field("cashflow", "递延所得税",
                       ("递延所得税资产减少", "递延所得税负债增加"), area="supplement"),

    # ---------- 合并资产负债表 ----------
    "total_assets": Field("balance", "资产总计", ("资产合计",)),
    "total_liabilities": Field("balance", "负债合计"),
    "total_equity": Field("balance", "所有者权益合计", ("股东权益合计",)),
    "total_parent_equity": Field("balance", "归属于母公司所有者权益合计",
                                 ("归属于母公司股东权益合计",)),
    "minority_equity": Field("balance", "少数股东权益"),
    "total_current_assets": Field("balance", "流动资产合计"),
    "total_current_liab": Field("balance", "流动负债合计"),
    # 银行的"货币资金"写作"现金及存放中央银行款项"，保险与证券亦然。
    # 这一项喂给现金类指标，缺了它，金融业企业的现金含量会算成 0。
    "monetaryfunds": Field("balance", "货币资金",
                           ("现金及存放中央银行款项", "库存现金")),
    "inventory_bs": Field("balance", "存货"),
    "fixed_asset": Field("balance", "固定资产"),
    "cip": Field("balance", "在建工程"),
    "goodwill": Field("balance", "商誉"),
    "short_loan": Field("balance", "短期借款"),
    "long_loan": Field("balance", "长期借款"),
}

# 同一科目在财报里被拆成多行时如何合并。
# 递延所得税在间接法里分为"资产减少"与"负债增加"两行，
# 二者之和才是应加回的递延所得税净额；只取一行会低估调节项。
COMBINE = {"defer_tax": "sum"}

# 括号内容一律剥除：中文财报里括号里装的是填表说明或等价表述，
# 科目名的实质部分一定在括号之外。
_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]?")
_PREFIX_RE = re.compile(
    r"^(?:[0-9]+[、.．]|[一二三四五六七八九十]+、|[（(][0-9一二三四五六七八九十]+[）)]"
    r"|加：|减：|其中：|其他：)+"
)
_SPACE_RE = re.compile(r"[\s\u3000]+")


def normalize(label: str) -> str:
    """把财报原文科目名归一化成可比对的键。"""
    text = _SPACE_RE.sub("", label or "")
    text = _PREFIX_RE.sub("", text)
    text = _PAREN_RE.sub("", text)
    return text.strip("　 　:：;；,，。.")


def _index() -> Dict[str, str]:
    """归一化写法 -> canonical 字段。重复写法视为配置错误并直接暴露。"""
    out: Dict[str, str] = {}
    for key, spec in FIELD_MAP.items():
        for text in spec.candidates():
            norm = normalize(text)
            if not norm:
                continue
            if norm in out and out[norm] != key:
                raise ValueError(f"科目名冲突：{norm} 同时指向 {out[norm]} 与 {key}")
            out[norm] = key
    return out


INDEX: Dict[str, str] = _index()


def match(label: str) -> "str | None":
    """把原文科目名解析为 canonical 字段名；解析不出返回 None。"""
    return INDEX.get(normalize(label))


def match_any(candidates) -> "str | None":
    """按可信度顺序尝试多个候选写法，返回首个命中。"""
    for text in candidates:
        hit = match(text)
        if hit:
            return hit
    return None


def by_report(report: str) -> Dict[str, Field]:
    return {k: v for k, v in FIELD_MAP.items() if v.report == report}


def evidence_id(secucode: str, report_date, canonical: str) -> str:
    """确定性证据编号：同一 (主体, 报告期, 科目) 恒定。

    编号前缀用 PDF 而非数据源缩写：本系统的证据一律指向用户上传的财报原文，
    前缀要能直接说明这一点，否则读者会去别处找来源。
    """
    spec = FIELD_MAP.get(canonical)
    token = "|".join([
        secucode, str(report_date),
        spec.report if spec else "-", spec.label if spec else canonical,
    ])
    return "PDF-" + hashlib.sha1(token.encode("utf-8")).hexdigest()[:10].upper()
