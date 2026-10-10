"""异常信号规则测试：规则必须给出可核对的事实与证据，而不是结论性判断。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F
from finagent.metrics import indicators as I
from finagent.validate.anomaly import run_rules

LEVELS = {"高", "中", "低", "提示"}
CATEGORIES = {"异常信号", "结构性特征", "口径提示"}
PERIOD = re.compile(r"^\d{4}Q[1-4]$")


def run_one(code: str, family: str = "G") -> int:
    us, key = F.uploads(code)
    if us is None:
        print(F.missing_message(code))
        return 0
    cfg = F.load_cfg()
    merged = us.merged(key)
    table, _ = I.compute_all(merged["frames"])
    findings = run_rules(code, merged["frames"], table, cfg, since_year=2025)

    print("=" * 74)
    print(f"{code}  识别到 {len(findings)} 条结论")
    failures = 0
    if not findings:
        print("  [失败] 一条结论都没有：规则引擎等于没工作")
        return 1
    for f in findings:
        print(f"  [{f.level}] {f.rule:<4} {f.category:<6} {f.period:<8} {f.title}")
        print(f"        事实：{f.statement}")

    for f in findings:
        problems = []
        if f.level not in LEVELS:
            problems.append(f"等级 {f.level}")
        if f.category not in CATEGORIES:
            problems.append(f"类别 {f.category}")
        if not PERIOD.match(f.period or ""):
            problems.append(f"报告期 {f.period!r}")
        if not (f.statement or "").strip():
            problems.append("事实为空")
        if not f.evidence:
            problems.append("没有证据")
        if problems:
            failures += 1
            print(f"  [失败] {f.rule} {f.title}：{('、'.join(problems))}")
    if not failures:
        print(f"  [通过] {len(findings)} 条结论的等级／类别／报告期／事实／证据齐备")
    return failures


def main() -> int:
    failures = run_one("002714")
    # 京东方：折旧摊销巨大，现金含量必须被定性为"结构性特征"而不是异常，
    # 这正是赛题要求识别的"利润与现金流背离"，也是外行最容易误判的一类。
    us, key = F.uploads("000725")
    if us is not None:
        failures += run_one("000725")
        cfg = F.load_cfg()
        merged = us.merged(key)
        table, _ = I.compute_all(merged["frames"])
        findings = run_rules("000725", merged["frames"], table, cfg, since_year=2025)
        cash = [f for f in findings if f.rule.startswith("R2")]
        ok = bool(cash) and all(f.category in CATEGORIES for f in cash)
        print(f"  [{'通过' if ok else '失败'}] 京东方现金含量类结论 {len(cash)} 条，"
              f"类别 {sorted({f.category for f in cash})}")
        failures += 0 if ok else 1
    else:
        print(F.missing_message("000725"))
    print("=" * 74)
    print(f"规则引擎测试失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
