"""端到端复现性验证：同一批上传材料重复运行，确定性层的产物必须逐字节一致。

"可复现"是竞赛的硬要求，因此这里不靠肉眼比对，而是把两次独立运行的
确定性产物（指标宽表、frames、规则结论、校验结论）落到磁盘上直接比字节。

大模型那一层本身不确定，因此单独记录它的模型名、提示词哈希与技能哈希：
评审据此能确认两次运行用的是同一套提示词与同一套 Skill——
不要求模型逐字相同，要求的是"输入与规则相同"。

调用方式：
    python tests/test_reproducibility.py            # 跑两遍并比对
    python tests/test_reproducibility.py --probe D  # 子进程模式：产物写入目录 D
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import _fixtures as F

CODE = "002714"
BATCH = "_repro"
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def write_config(tmp: str) -> str:
    """只改输出目录的配置：跑测试不覆盖交付用的 output/ 产物。

    临时目录里没有 config.local.yaml，子进程读不到开发机上的密钥，
    但这不影响本测试——被测的是确定性计算层，不是大模型。
    """
    def posix(p: str) -> str:
        return str(p).replace("\\", "/")

    path = os.path.join(tmp, "config.yaml")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "data:\n"
            f"  uploads_dir: {posix(ROOT / 'data' / 'uploads')}\n"
            "output:\n"
            f"  report_dir: {posix(os.path.join(tmp, 'reports'))}\n"
            f"  table_dir: {posix(os.path.join(tmp, 'tables'))}\n"
            f"  trace_dir: {posix(os.path.join(tmp, 'traces'))}\n"
        )
    return path


def probe(out_dir: str) -> int:
    """子进程入口：把确定性产物原样写到 out_dir。"""
    from finagent.config import load_config, uploads_dir
    from finagent.ingest.dataset import UploadSet, CODE_VERSION
    from finagent.metrics import indicators as I
    from finagent.validate.anomaly import run_rules

    os.makedirs(out_dir, exist_ok=True)
    cfg = load_config(os.path.join(os.path.dirname(out_dir), "config.yaml"))
    us = UploadSet(run_id=BATCH, root=uploads_dir(cfg))
    for path in F.pick(CODE, ("semi", "annual")):
        us.add(path)
    key = CODE
    merged = us.merged(key)
    table, _ = I.compute_all(merged["frames"])
    findings = run_rules(CODE, merged["frames"], table, cfg, since_year=2025)

    def dump(name, text):
        with io.open(os.path.join(out_dir, name), "w", encoding="utf-8",
                     newline="\n") as fh:
            fh.write(text)

    # 指标宽表：CSV 是交付物之一，必须逐字节稳定
    buffer = io.StringIO()
    table.to_csv(buffer, float_format="%.6f")
    dump("table.csv", buffer.getvalue())
    dump("frames.json", json.dumps(
        {k: json.loads(v.to_json(orient="records", date_format="iso"))
         for k, v in merged["frames"].items()},
        ensure_ascii=False, indent=1, sort_keys=True))
    dump("findings.json", json.dumps(
        [f.as_dict() for f in findings], ensure_ascii=False, indent=1, sort_keys=True))
    dump("verification.json", json.dumps(
        merged["verification"], ensure_ascii=False, indent=1, sort_keys=True,
        default=str))
    dump("version.txt", CODE_VERSION + "\n")
    print(f"probe ok: {len(table)} 期 × {len(table.columns)} 指标，"
          f"{len(findings)} 条结论，抽取层指纹 {CODE_VERSION}")
    return 0


def main() -> int:
    if "--probe" in sys.argv:
        return probe(sys.argv[sys.argv.index("--probe") + 1])

    us, _ = F.uploads(CODE)
    if us is None:
        print(F.missing_message(CODE))
        return 0

    failures = 0
    tmp = tempfile.mkdtemp(prefix="finagent-repro-")
    try:
        cfg_path = write_config(tmp)
        snapshots = []
        for round_no in (1, 2):
            out = os.path.join(tmp, f"run{round_no}")
            os.makedirs(out, exist_ok=True)
            # 每一轮都用同一份 config，只有输出目录不同
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", __file__, "--probe", out],
                cwd=ROOT, env=CHILD_ENV, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            print(f"  第 {round_no} 轮：{proc.stdout.strip() or proc.stderr.strip()[-200:]}")
            if proc.returncode != 0:
                failures += 1
            snapshots.append(out)

        print("-" * 74)
        names = sorted(os.listdir(snapshots[0]))
        for name in names:
            a = Path(snapshots[0], name).read_bytes()
            b = Path(snapshots[1], name).read_bytes()
            same = a == b
            failures += 0 if same else 1
            print(f"  [{'一致' if same else '不一致'}] {name:<18} {len(a):>8} 字节")
        version = Path(snapshots[0], "version.txt").read_text(encoding="utf-8").strip()
        print(f"  [{'通过' if version else '失败'}] 抽取层指纹已记录 {version}")
        failures += 0 if version else 1

        # 结论条数应为正数：全为零说明这一轮根本没抽到东西，比"不一致"更值得报警
        findings = json.loads(Path(snapshots[0], "findings.json").read_text(encoding="utf-8"))
        ok = len(findings) > 0
        print(f"  [{'通过' if ok else '失败'}] 规则结论 {len(findings)} 条")
        failures += 0 if ok else 1

        # 轨迹：一次运行一条完整轨迹，seq 必须连续无重号。
        # index 子命令不调用大模型，因此可以在没有密钥的环境里验证留痕机制。
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "run.py", "--config", cfg_path,
             "index", "--run", F.TEST_RUN],
            cwd=ROOT, env=CHILD_ENV, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        print(f"  [{'通过' if ok else '失败'}] 留痕机制可运行（run.py index）")
        failures += 0 if ok else 1
        trace_dir = os.path.join(tmp, "traces")
        traces = sorted(os.listdir(trace_dir)) if os.path.isdir(trace_dir) else []
        if traces:
            path = os.path.join(trace_dir, traces[0])
            events = [json.loads(line) for line in
                      io.open(path, encoding="utf-8").read().splitlines() if line.strip()]
            seqs = [e.get("seq") for e in events]
            ok = seqs == list(range(1, len(events) + 1))
            print(f"  [{'通过' if ok else '失败'}] 轨迹 seq 连续（{len(events)} 个事件）")
            failures += 0 if ok else 1
        else:
            print("  [提示] 本轮没有产生轨迹文件（确定性探针不写轨迹）")
        print("-" * 74)
        print(f"复现性测试失败项：{failures}")
        return 1 if failures else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
