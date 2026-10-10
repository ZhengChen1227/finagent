"""编排循环测试：无密钥时硬失败；有密钥时有界调用真实模型。

这里刻意分两段：
1. **无密钥必须抛错**。系统的任何一层都不允许在缺少推理引擎时"照常出报告"——
   那会让人误以为分析已经完成。因此把这条行为写成断言。
2. 有密钥时跑一次有界（3 步）的真实编排，验证 Function Calling 链路通。
   没有密钥时跳过并说明，而不是伪装通过。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F
from finagent.agent.loop import AgentLoop
from finagent.trace import Trace

OBJECTIVE = ("请对上传材料中最新报告期的业绩变化做归因，重点看利润与现金流的关系、"
             "减值计提、所得税，并回到原文核对。")


def main() -> int:
    us, key = F.uploads("002714")
    if us is None:
        print(F.missing_message("002714"))
        return 0
    cfg = F.load_cfg()
    failures = 0

    # ---- 1) 无密钥：必须硬失败 ----
    blank = dict(cfg)
    blank["llm"] = dict(cfg["llm"], api_key="")
    os.environ.pop("FINAGENT_API_KEY", None)
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    loop = AgentLoop(blank, trace, max_steps=2, verbose=False,
                     focus_key=key, uploads=us)
    try:
        loop.run(OBJECTIVE)
        print("  [失败] 无密钥时竟然跑通了——静默降级会让人以为分析已完成")
        failures += 1
    except RuntimeError as exc:
        ok = "DeepSeek" in str(exc) or "密钥" in str(exc)
        print(f"  [{'通过' if ok else '失败'}] 无密钥硬失败：{exc}")
        failures += 0 if ok else 1
    trace.close()

    # ---- 2) 有密钥：有界真实推理 ----
    if not (cfg["llm"].get("api_key") or os.environ.get("FINAGENT_API_KEY")):
        print("  跳过：未配置 DeepSeek API Key，无法验证真实编排链路。")
        print(f"编排循环测试失败项：{failures}")
        return 1 if failures else 0

    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    loop = AgentLoop(cfg, trace, max_steps=3, verbose=True,
                     focus_key=key, uploads=us)
    start = time.time()
    result = loop.run(OBJECTIVE)
    print(f"  模式 {result['mode']}  终止 {result['stop_reason']}  "
          f"耗时 {time.time() - start:.1f}s  调用 {result['llm_calls']} 次")
    tool_steps = [e for e in result["transcript"] if e.get("type") == "tool"]
    print(f"  工具调用 {len(tool_steps)} 次：")
    for entry in tool_steps[:12]:
        print(f"     步骤{entry['step']:>2}  {entry.get('tool'):<20} {entry.get('summary')}")
    checks = [
        ("推理模式为 llm", result["mode"] == "llm"),
        ("确实调用了模型", result["llm_calls"] > 0),
        ("记录提示词版本", bool(result["prompt_hashes"])),
    ]
    for title, ok in checks:
        print(f"  [{'通过' if ok else '失败'}] {title}")
        failures += 0 if ok else 1
    trace.close()
    print(f"编排循环测试失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
