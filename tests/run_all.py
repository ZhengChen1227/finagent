"""一键运行全部测试并汇总结果。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 子进程 stdout 在没有控制台时按系统本地编码（中文 Windows 为 GBK）写出，
# 若一律用 UTF-8 解码会抛 UnicodeDecodeError，并把失败详情整段吞掉。
# 统一要求子进程以 UTF-8 输出，并允许替换非法字节作为兜底。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

SUITES = [
    ("环境自检", "tests/check_env.py"),
    ("抽取层(合成版面)", "tests/test_ingest.py"),
    ("主链路冒烟", "tests/smoke.py"),
    ("规则引擎", "tests/test_rules.py"),
    ("工具层", "tests/test_tools.py"),
    ("编排循环", "tests/test_agent.py"),
    ("MCP 协议", "tests/test_mcp.py"),
    ("大模型路径", "tests/test_llm.py"),
    ("端到端复现", "tests/test_reproducibility.py"),
    ("应用界面与接口", "tests/test_app.py"),
    ("网络边界", "tests/test_network.py"),
]


def main() -> int:
    failed = []
    for name, script in SUITES:
        proc = subprocess.run([sys.executable, script], cwd=ROOT, env=CHILD_ENV,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        status = "通过" if proc.returncode == 0 else f"失败({proc.returncode})"
        if proc.returncode != 0:
            failed.append((name, script))
        print(f"  {name:<18} {status}")

    print()
    if failed:
        print("失败详情：")
        for name, script in failed:
            proc = subprocess.run([sys.executable, script], cwd=ROOT, env=CHILD_ENV,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
            print(f"--- {name} ({script}) ---")
            print((proc.stdout or "")[-1500:])
            print((proc.stderr or "")[-1500:])
    else:
        print("全部测试通过。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
