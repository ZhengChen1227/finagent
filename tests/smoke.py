"""主链路冒烟测试：上传财报 → 抽取 → 指标 → 勾稽 → 口径还原。

这是"从 PDF 到数字"最短的一条链路，任何一环断掉都会在这里立刻暴露。
需要本机 data/corpus/ 下有真实财报；没有时**跳过并说明原因**，
而不是伪装通过——伪装通过会让"可复现"变成一句空话。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F
from finagent.metrics import indicators as I
from finagent.metrics.periods import metric_table
from finagent.validate.articulation import run_articulation


def main() -> int:
    us, key = F.uploads("002714", ("semi", "annual"))
    if us is None:
        print(F.missing_message("002714"))
        return 0

    cfg = F.load_cfg()
    merged = us.merged(key)
    frames = merged["frames"]
    failures = 0

    income = frames["income"]
    row = income[income["report_date"] == "2026-06-30"].iloc[0]
    print("=" * 74)
    print(f"主体 {key}  材料 {len(merged['members'])} 份  "
          f"报告期 {[str(d.date()) for d in income['report_date']]}")

    # --- 与巨潮原文逐字核对过的基准数字 ---
    # 现金流量表的科目只出现在 cashflow 那一张表里：三大表各自成帧，
    # 跨表取值会取到 None，而 None 与"数字错了"是两种完全不同的问题。
    cash = frames["cashflow"]
    cash_row = cash[cash["report_date"] == "2026-06-30"].iloc[0]
    for field, want in F.MUYUAN_2026H1.items():
        source = cash_row if field == "netcash_operate" else row
        got = source.get(field)
        ok = got is not None and abs(float(got) - want) < 0.01
        failures += 0 if ok else 1
        print(f"  [{'通过' if ok else '失败'}] {field:<24} {got!r}  期望 {want!r}")

    # --- 勾稽校验 ---
    checks = run_articulation(frames, "002714", cfg)
    balance = [c for c in checks if "资产=" in c["check"]]
    bad = [c for c in balance if not c["passed"]]
    failures += len(bad)
    print(f"  [{'通过' if not bad else '失败'}] 资产负债表恒等式 "
          f"{len(balance) - len(bad)}/{len(balance)} 期通过")
    for c in checks:
        if "间接法" in c["check"]:
            print(f"       间接法反推 {c['period']:<10} 残差 {c['residual']:>16,.0f} "
                  f"({c['residual_pct']:>7.2f}%)")

    # --- 指标层 ---
    table, flags = I.compute_all(frames)
    cols = ["cash_conversion_naive", "cash_conversion_adjusted", "net_margin"]
    missing = [c for c in cols if c not in table.columns]
    failures += len(missing)
    print(f"  [{'通过' if not missing else '失败'}] 指标层列齐备 {cols}")
    print(table[cols].tail(4).round(3).to_string().replace("\n", "\n    "))

    # --- 口径还原：同比优先用报告自带的比较列 ---
    t = metric_table(frames["income"], "parent_net_profit", "归母净利润")
    row2 = t.dropna(subset=["yoy_cum_pct"]).iloc[-1]
    yoy = float(row2["yoy_cum_pct"])
    ok = abs(yoy - (-157.72)) < 0.05
    failures += 0 if ok else 1
    print(f"  [{'通过' if ok else '失败'}] 归母净利润同比 {yoy:.2f}%  期望 -157.72%")

    # --- 交叉校验闸门：这份材料的校验结论必须全部可解释 ---
    ver = merged["verification"]
    print(f"  [{'通过' if not ver['failed'] else '失败'}] 校验闸门 "
          f"通过 {ver['passed']} 未过 {ver['failed']} "
          f"可疑 {sorted(ver['suspect'])}")
    failures += 1 if ver["failed"] else 0

    print("=" * 74)
    print(f"冒烟测试失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
