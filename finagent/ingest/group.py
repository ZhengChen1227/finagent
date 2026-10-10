"""多份材料的归组、可比性判定与单季还原。

存在的意义：用户拖进来的可能是一季报加半年报，也可能是两家不同公司的财报。
在把这些材料叠成一条时间轴之前，必须先回答"它们能不能放在一起比"。

三条必须显式判定的可比性条件：

1. **主体一致**。两家公司的报表合并到一张表里，指标会静默变成
   "本期减上期"的跨公司相减，数字看着正常、含义完全错。
   归并键取股票代码（解析不到时才退回公司全称）。
2. **单位一致**。以万元列示的报表与以元列示的报表相加会差一万倍。
   这类错误不会抛异常，只会产出一个荒谬但完整的报告。
3. **报告期不重复**。同一期上传两次（例如先传季报又传了含同一期的年报），
   同一报告期会出现两行；本模块只保留一手来源那一行。

另外还会识别"金融业报表"并给出提示：银行与保险的科目体系与一般工商业
完全不同（没有"营业成本""存货"），本系统对这类主体只提供同义词兜底，
识别不出的科目会被如实列为缺失，而不是硬套工商业口径。
"""

from __future__ import annotations

import pandas as pd

from finagent.metrics.periods import CUM_PERIODS, single_quarter

# 报告期在一年内的先后顺序（借用报表口径表的定义，避免两处口径漂移）。
KIND_ORDER = {key: idx for idx, key in
              enumerate(["03-31", "06-30", "09-30", "12-31"], start=1)}

# 金融业报表的特征：一般工商业一定有"营业成本"与"存货"，
# 银行／保险／证券则一定没有。用"缺什么"来识别，比用"有什么"更稳。
GENERAL_ONLY = ("operating_cost", "inventory_bs")
# 金融业常见科目（本系统已配置同义词的）
FINANCIAL_HINTS = ("credit_impairment",)


def order_key(entry: dict) -> tuple:
    """排序键：(报告期, 报告类型序数)。让材料按时间轴自然排列。"""
    subject = entry.get("subject") or {}
    date = subject.get("report_date") or ""
    return date, KIND_ORDER.get(date[-5:], 0) if date else 0


def order(members: list) -> list:
    return sorted(members, key=order_key)


def notes(members: list) -> list:
    """列出这批材料的可比性提示。空列表表示无异常。"""
    out = []
    codes = {(m.get("subject") or {}).get("code") for m in members}
    names = {(m.get("subject") or {}).get("short_name")
             or (m.get("subject") or {}).get("full_name") for m in members}
    if len(codes - {None}) > 1 or (len(codes) == 1 and None in codes
                                   and len(names - {None}) > 1):
        out.append("本组材料的股票代码不一致，可能混入了不同公司的报告："
                   + "、".join(sorted(str(c) for c in codes)))
    dates = [(m.get("subject") or {}).get("report_date") for m in members]
    duplicated = {d for d in dates if dates.count(d) > 1}
    if duplicated:
        out.append("同一报告期有多份材料（" + "、".join(sorted(duplicated))
                   + "），合并时只保留本期口径的那一份，比较列不参与覆盖")
    scales = {}
    for member in members:
        for report, scale in (member.get("built_units") or {}).items():
            scales.setdefault(report, set()).add(round(float(scale), 6))
    mixed = {r: s for r, s in scales.items() if len(s) > 1}
    for report, values in mixed.items():
        out.append(f"{report} 的单位在组内不一致（{sorted(values)}），"
                   f"这些材料不会被相加")
    return out


def family_hint(frames: dict) -> str:
    """粗略判断报表族：G 一般工商业 / F 金融业（银行、保险、证券）。

    只用于提示"本系统对该主体只提供同义词兜底"，不参与任何计算——
    把金融业硬套成工商业口径去算毛利率，会得到一个没有意义的数字，
    但不会有任何报错，因此这里宁可谨慎。
    """
    income = frames.get("income")
    balance = frames.get("balance")
    missing_general = 0
    for report, keys in (("income", ("operating_cost",)),
                         ("balance", ("inventory_bs",))):
        df = frames.get(report)
        if df is None or df.empty:
            continue
        if not any(k in df.columns and df[k].notna().any() for k in keys):
            missing_general += 1
    if missing_general == 2:
        return "F"
    return "G"


def single_quarters(cumulative: pd.Series) -> pd.Series:
    """累计值 → 单季度值。直接复用指标层的实现，保证与同比/环比同源。"""
    return single_quarter(cumulative)


def period_label(report_date: str) -> str:
    return CUM_PERIODS.get(str(report_date)[-5:], ("", 0))[0]


def quarters_in(frames: dict, metric: str) -> pd.Series:
    """取某指标的单季度序列，用于环比与"单季还原 vs 年报分季度表"的互证。"""
    from finagent.metrics.periods import cumulative_series
    for report in ("income", "cashflow", "balance"):
        df = frames.get(report)
        if df is not None and not df.empty and metric in df.columns:
            return single_quarter(cumulative_series(df, metric))
    return pd.Series(dtype="float64")
