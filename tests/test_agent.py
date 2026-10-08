"""智能体编排循环测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.agent.loop import AgentLoop
from finagent.config import load_config
from finagent.trace import Trace


def main() -> int:
    cfg = load_config()
    trace = Trace()
    loop = AgentLoop(cfg, trace)

    objective = (
        "请对牧原股份（002714）和京东方A（000725）最新报告期的业绩变化进行归因分析。"
        "重点关注利润与现金流的关系、减值计提、所得税异常，"
        "并对识别出的异常信号回到公告原文核实。"
    )
    result = loop.run(objective)

    print()
    print("运行模式:", result["mode"])
    print("终止原因:", result["stop_reason"])
    print("轨迹步数:", len(result["transcript"]))
    print("提示词版本:", result["prompt_hashes"])
    print()
    print("工具调用序列:")
    for entry in result["transcript"]:
        if entry["type"] == "tool":
            print(f"   步骤{entry['step']:>2}  {entry['tool']:<22} {entry['summary']}")
    print()
    print("轨迹文件:", trace.close())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
