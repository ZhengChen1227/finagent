"""报告期口径还原与同比/环比计算。

关键口径问题：
A股定期报告披露的是"年初至今累计数"——一季报=Q1，中报=H1，三季报=9M，年报=FY。
因此"环比"绝不能拿累计数直接相除，必须先把累计数还原为单季度数。
这里是同比/环比准确性的关键所在，也是本模块存在的理由。

另外两条严谨性约定：
1. 同比基数为负时，比率失去经济含义，本模块仍给出数值但强制打标，交由报告层披露。
2. 单季度还原只在前一期累计数存在时进行，缺失时留空而不臆造。
"""

from __future__ import annotations

import pandas as pd

# 报告期 -> (累计期标签, 年内序号)
CUM_PERIODS = {
    "03-31": ("Q1", 1),
    "06-30": ("H1", 2),
    "09-30": ("9M", 3),
    "12-31": ("FY", 4),
}
Q_LABEL = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}
Q_MONTH_DAY = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def period_index(report_date):
    """报告期 -> (年, 年内序号, 累计期标签)。"""
    ts = pd.Timestamp(report_date)
    key = ts.strftime("%m-%d")
    if key not in CUM_PERIODS:
        raise ValueError(f"非标准报告期: {report_date}")
    label, idx = CUM_PERIODS[key]
    return ts.year, idx, label


def cumulative_series(df: pd.DataFrame, metric: str) -> pd.Series:
    """抽取某指标的累计值序列，索引为 (年, 年内序号)。"""
    if metric not in df.columns:
        return pd.Series(dtype="float64")
    sub = df[["report_date", metric]].dropna()
    if sub.empty:
        return pd.Series(dtype="float64")
    keys = [period_index(d)[:2] for d in sub["report_date"]]
    series = pd.Series(sub[metric].to_numpy(),
                       index=pd.MultiIndex.from_tuples(keys, names=["year", "q"]))
    return series.sort_index()


def single_quarter(cum: pd.Series) -> pd.Series:
    """累计数 -> 单季度数。Q1 等于累计值；Qn = 本期累计 - 上期累计。"""
    if cum.empty:
        return pd.Series(dtype="float64")
    out = {}
    for (year, q), value in cum.items():
        if q == 1:
            out[(year, 1)] = value
        elif (year, q - 1) in cum.index:
            out[(year, q)] = value - cum[(year, q - 1)]
    return pd.Series(out).sort_index() if out else pd.Series(dtype="float64")


def _pct(current, base):
    """比率计算，返回 (数值, 备注)。基数为负或缺失时按约定处理。"""
    if current is None or base is None or pd.isna(current) or pd.isna(base):
        return None, "数据缺失"
    if base == 0:
        return None, "基数为零"
    if base < 0:
        return (current - base) / abs(base) * 100.0, "基数为负，比率仅供参考"
    return (current - base) / base * 100.0, None


def metric_table(df: pd.DataFrame, metric: str, label: str | None = None) -> pd.DataFrame:
    """生成单指标的完整口径表：累计值、单季度值、同比、环比。

    每一行对应一个报告期，列含义：
      cumulative      累计值（报告原文口径）
      single_quarter  还原后的单季度值
      yoy_cum_pct     累计同比 %
      yoy_single_pct  单季度同比 %
      qoq_single_pct  单季度环比 %
      note            口径提示
    """
    cum = cumulative_series(df, metric)
    single = single_quarter(cum)
    rows = []
    for (year, q), value in cum.items():
        base_cum = cum.get((year - 1, q))
        base_single = single.get((year - 1, q))
        prev_single = single.get((year, q - 1))

        yoy_cum, _ = _pct(value, base_cum)
        yoy_single, _ = _pct(single.get((year, q)), base_single)
        qoq_single, note_c = _pct(single.get((year, q)), prev_single)

        # 口径提示只保留有决策价值的说明。首季度天然没有环比口径，
        # 不作为"数据缺失"上报，否则噪声会淹没真正需要注意的提示。
        notes = []
        if base_cum is None:
            notes.append("上年同期缺失")
        elif base_cum < 0:
            notes.append("同比基数为负，比率仅供参考")
        if q > 1 and note_c:
            notes.append(note_c)
        rows.append({
            "year": year,
            "q": q,
            "period": f"{year}{CUM_PERIODS[Q_MONTH_DAY[q]][0]}",
            "quarter": f"{year}{Q_LABEL[q]}",
            "cumulative": value,
            "single_quarter": single.get((year, q)),
            "yoy_cum_pct": yoy_cum,
            "yoy_single_pct": yoy_single,
            "qoq_single_pct": qoq_single,
            "note": "；".join(notes) if notes else "",
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["metric"] = metric
        out["label"] = label or metric
    return out


def latest_two(table: pd.DataFrame):
    """取出最近两个可比的报告期行，便于生成"本期 vs 上期"叙述。"""
    if table.empty:
        return None, None
    valid = table[table["cumulative"].notna()]
    if len(valid) == 0:
        return None, None
    if len(valid) == 1:
        return valid.iloc[-1], None
    return valid.iloc[-1], valid.iloc[-2]
