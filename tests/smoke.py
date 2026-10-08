"""冒烟测试：验证数据源、指标层、校验层的主链路可跑通。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.config import load_config
from finagent.datasource.eastmoney import EastMoneySource
from finagent.metrics import indicators as I
from finagent.metrics.periods import metric_table
from finagent.validate.articulation import run_articulation

CODES = ["002714.SZ", "000725.SZ"]


def main() -> int:
    cfg = load_config()
    src = EastMoneySource()
    failures = 0

    for code in CODES:
        frames = src.frames(code)
        print("=" * 74)
        print(code)

        # --- 勾稽校验 ---
        checks = run_articulation(frames, code, cfg)
        balance = [c for c in checks if "资产=" in c["check"]]
        indirect = [c for c in checks if "间接法" in c["check"]]
        passed_b = sum(1 for c in balance if c["passed"])
        print(f"  资产负债表恒等式   {passed_b}/{len(balance)} 期通过  "
              f"最大相对偏差 {max(c['rel_pct'] for c in balance):.6f}%")
        if passed_b != len(balance):
            failures += 1

        for c in indirect[-3:]:
            flag = "PASS" if c["passed"] else "FAIL"
            print(f"  间接法反推 {c['period']:<8} 残差 {c['residual']:>17,.0f} "
                  f"({c['residual_pct']:>7.2f}%)  {flag}")

        # --- 指标层 ---
        table, flags = I.compute_all(frames)
        cols = ["cash_conversion_naive", "cash_conversion_adjusted", "net_margin"]
        print("  关键指标：")
        print(table[cols].tail(4).round(3).to_string().replace("\n", "\n    "))

        # --- 口径还原 ---
        t = metric_table(frames["income"], "parent_net_profit", "归母净利润")
        row = t.dropna(subset=["yoy_cum_pct"]).iloc[-1]
        print(f"  最新可比期 {row['quarter']}  归母净利 {row['cumulative']:,.0f}  "
              f"同比 {row['yoy_cum_pct']:.2f}%")

    print("=" * 74)
    print("冒烟测试失败项：", failures)
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
