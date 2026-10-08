"""勾稽校验：用会计恒等式验证解析结果的正确性。

这是本系统"数据准确性"的第一道防线。
如果解析出来的数字通不过勾稽，后续一切分析都不可信。
"""

from __future__ import annotations

import pandas as pd

from finagent.datasource.eastmoney import evidence_id
from finagent.metrics.indicators import (
    _cum, _add, dep_amort, impairment, ebitda_proxy,
)


def check_balance_identity(frames: dict, secucode: str, tol_pct: float = 0.5) -> list[dict]:
    """资产 = 负债 + 所有者权益。"""
    df = frames.get("balance")
    if df is None or df.empty:
        return []
    results = []
    for _, row in df.iterrows():
        assets = row.get("total_assets")
        liab = row.get("total_liabilities")
        equity = row.get("total_equity")
        if any(pd.isna(x) for x in (assets, liab, equity)) or not assets:
            continue
        diff = assets - (liab + equity)
        rel_pct = abs(diff) / assets * 100.0
        results.append({
            "check": "资产=负债+所有者权益",
            "period": pd.Timestamp(row["report_date"]).strftime("%Y-%m-%d"),
            "lhs": float(assets),
            "rhs": float(liab + equity),
            "diff": float(diff),
            "rel_pct": round(rel_pct, 6),
            "passed": rel_pct <= tol_pct,
            "evidence_id": evidence_id(secucode, row["report_date"], "total_assets"),
        })
    return results


def reconcile_indirect_method(frames: dict, secucode: str) -> list[dict]:
    """间接法反推：净利润经非付现项目与营运资本变动调节后，应等于经营活动现金流量净额。

    这是对现金流量表解析质量最有力的检验。
    残差客观披露，不做人为抹平——残差本身也是信息。
    """
    net_profit = _cum(frames, "net_profit")
    if net_profit.empty:
        return []

    adj = {
        "资产减值准备": _cum(frames, "impairment_provision"),
        "固定资产折旧等": _cum(frames, "depr_fa"),
        "无形资产摊销": _cum(frames, "amort_ia"),
        "长期待摊费用摊销": _cum(frames, "amort_lpe"),
        "使用权资产摊销": _cum(frames, "amort_rou"),
        "非流动资产处置损失": _cum(frames, "disposal_longasset_loss"),
        "固定资产报废损失": _cum(frames, "fa_scrap_loss"),
        "财务费用(加回)": _cum(frames, "finance_expense"),
        "投资收益(扣除)": -_cum(frames, "invest_income"),
        "公允价值变动(扣除)": -_cum(frames, "fairvalue_change"),
        "递延所得税": _cum(frames, "defer_tax"),
        "存货的减少": _cum(frames, "inventory_reduce"),
        "经营性应收减少": _cum(frames, "operate_rece_reduce"),
        "经营性应付增加": _cum(frames, "operate_payable_add"),
    }
    total_adj = _add(*adj.values())
    reconstructed = net_profit.add(total_adj, fill_value=0.0)

    reported = _cum(frames, "netcash_operate")
    if reported.empty:
        return []

    # 仅在补充资料齐全的报告期校验（季报不披露间接法调节表）
    da = dep_amort(frames)
    periods = [k for k in da.index if k in reported.index and k in reconstructed.index]

    results = []
    for key in periods:
        year, q = key
        recon = reconstructed.get(key)
        rep = reported.get(key)
        if recon is None or pd.isna(recon) or rep is None or pd.isna(rep):
            continue
        residual = rep - recon
        scale = abs(rep) if abs(rep) > 0 else 1.0
        results.append({
            "check": "间接法反推经营活动现金流量净额",
            "period": f"{year}Q{q}",
            "reconstructed": float(recon),
            "reported": float(rep),
            "residual": float(residual),
            "residual_pct": round(residual / scale * 100.0, 2),
            "passed": abs(residual) / scale <= 0.10,
            "evidence_id": evidence_id(secucode, f"{year}Q{q}", "netcash_operate"),
        })
    return results


def run_articulation(frames: dict, secucode: str, cfg: dict, trace=None) -> list[dict]:
    tol = float(cfg["thresholds"].get("articulation_tol_pct", 0.5))
    out = check_balance_identity(frames, secucode, tol) + reconcile_indirect_method(frames, secucode)
    if trace:
        passed = sum(1 for r in out if r["passed"])
        trace.tool_call(
            "articulation_check",
            {"secucode": secucode, "tolerance_pct": tol},
            {"checks": len(out), "passed": passed, "failed": len(out) - passed},
        )
    return out
