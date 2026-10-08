"""校验规则冒烟测试：打印近两年识别出的信号。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.config import load_config
from finagent.datasource.eastmoney import EastMoneySource
from finagent.metrics import indicators as I
from finagent.trace import Trace
from finagent.validate.anomaly import run_rules


def main() -> int:
    cfg = load_config()
    src = EastMoneySource()
    trace = Trace()
    for code in ["002714.SZ", "000725.SZ"]:
        frames = src.frames(code)
        table, _ = I.compute_all(frames, trace)
        findings = run_rules(code, frames, table, cfg, trace, since_year=2025)
        print("=" * 74)
        print(f"{code}  识别到 {len(findings)} 条结论")
        for f in findings:
            print(f"  [{f.level}] {f.rule:<4} {f.category:<6} {f.period:<8} {f.title}")
            print(f"        事实：{f.statement}")
    print("=" * 74)
    print("trace:", trace.close())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
