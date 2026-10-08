"""端到端复现性验证：同一输入重复运行，结论必须逐条一致。"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "002714_牧原股份"


def snapshot():
    data = json.load(open(ROOT / f"output/reports/{PKG}_结论.json", encoding="utf-8"))
    findings = [(f["rule"], f["period"], f["fact"]) for f in data["findings"]]
    artic = [(c["period"], c["check"], round(c.get("residual", c.get("rel_pct", 0)), 4))
             if "residual" in c else (c["period"], c["check"], c["rel_pct"])
             for c in data["articulation"]]
    return findings, artic, data["run_id"]


def main():
    # 本测试验证的是确定性计算层的可复现性——规则结论、勾稽结果必须逐条一致。
    # 因此强制离线运行：一是排除大模型非确定性对本测试的干扰，
    # 二是避免每次跑两遍完整分析带来的数分钟开销。
    env = dict(os.environ)
    env["FINAGENT_API_KEY"] = ""
    subprocess.run([sys.executable, "run.py", "analyze", "--quiet"],
                   cwd=ROOT, check=True, capture_output=True, env=env)
    f1, a1, r1 = snapshot()
    subprocess.run([sys.executable, "run.py", "analyze", "--quiet"],
                   cwd=ROOT, check=True, capture_output=True, env=env)
    f2, a2, r2 = snapshot()

    print(f"结论条数           {len(f1)} vs {len(f2)}")
    print(f"结论逐条一致       {f1 == f2}")
    print(f"勾稽结果一致       {a1 == a2}")
    print(f"运行编号可区分批次 {r1 != r2}")
    print()

    trace = sorted(glob.glob(str(ROOT / "output/traces/*.jsonl")))[-1]
    events = [json.loads(line) for line in open(trace, encoding="utf-8")]
    seqs = [e["seq"] for e in events]
    kinds = {}
    for e in events:
        kinds[e["event"]] = kinds.get(e["event"], 0) + 1
    print(f"轨迹文件 {Path(trace).name}")
    print(f"seq 连续           {seqs == list(range(1, len(events) + 1))}")
    print(f"事件总数           {len(events)}")
    print(f"事件分布           {kinds}")
    has_probe = [e for e in events if e["event"] == "agent_end"]
    if has_probe:
        print(f"智能体模式         {has_probe[0].get('mode')} / 终止原因 {has_probe[0].get('stop_reason')}")
        print(f"提示词版本         {[e for e in events if e['event']=='agent_start'][0].get('prompt_hashes')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
