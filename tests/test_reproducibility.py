"""端到端复现性验证：同一输入重复运行，结论必须逐条一致。

本测试刻意把产物写进临时目录，而不是项目里的 output/。原因是它要跑完整的
analyze 流程，而 analyze 会按配置覆盖 output/reports/ 下的同名报告——那些报告
是真实运行（含大模型归因）的产物，要交给评审看，不能被测试悄悄改写成离线版。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "002714_牧原股份"


def snapshot(report_dir: str):
    data = json.load(open(os.path.join(report_dir, f"{PKG}_结论.json"), encoding="utf-8"))
    findings = [(f["rule"], f["period"], f["fact"]) for f in data["findings"]]
    artic = [(c["period"], c["check"], round(c.get("residual", c.get("rel_pct", 0)), 4))
             if "residual" in c else (c["period"], c["check"], c["rel_pct"])
             for c in data["articulation"]]
    return findings, artic, data["run_id"]


def _write_config(tmp: str) -> str:
    """在临时目录里写一份只改输出路径的配置。

    只需要写 output 一节：其余项由 finagent/config.py 的 DEFAULTS 兜底，
    其中就包含本测试要用的 002714 与 000725 两家公司。
    临时目录里没有 config.local.yaml，因此不会读到开发机上的密钥，
    运行必然走确定性离线路径——这正是本测试要验证的那一层。
    """
    path = os.path.join(tmp, "config.yaml")
    def posix(p: str) -> str:
        return p.replace("\\", "/")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "output:\n"
            "  report_dir: %s\n"
            "  table_dir: %s\n"
            "  trace_dir: %s\n"
            % (posix(os.path.join(tmp, "reports")),
               posix(os.path.join(tmp, "tables")),
               posix(os.path.join(tmp, "traces")))
        )
    return path


def main():
    # 本测试验证的是确定性计算层的可复现性——规则结论、勾稽结果必须逐条一致。
    # 因此强制离线运行：一是排除大模型非确定性对本测试的干扰，
    # 二是避免每次跑两遍完整分析带来的数分钟开销。
    tmp = tempfile.mkdtemp(prefix="finagent-repro-")
    try:
        report_dir = os.path.join(tmp, "reports")
        trace_dir = os.path.join(tmp, "traces")
        cfg_path = _write_config(tmp)

        env = dict(os.environ)
        env["FINAGENT_API_KEY"] = ""

        def run_once():
            # --config 定义在主 parser 上，必须放在子命令之前
            subprocess.run([sys.executable, "run.py", "--config", cfg_path,
                            "analyze", "--quiet"],
                           cwd=ROOT, check=True, capture_output=True, env=env)

        run_once()
        f1, a1, r1 = snapshot(report_dir)
        run_once()
        f2, a2, r2 = snapshot(report_dir)

        print(f"结论条数           {len(f1)} vs {len(f2)}")
        print(f"结论逐条一致       {f1 == f2}")
        print(f"勾稽结果一致       {a1 == a2}")
        print(f"运行编号可区分批次 {r1 != r2}")
        print()

        trace = sorted(glob.glob(os.path.join(trace_dir, "*.jsonl")))[-1]
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
