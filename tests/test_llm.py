"""大模型路径测试：连通性、Function Calling、有界编排循环。

这是系统中唯一依赖外部服务的部分，因此需要单独验证。
未配置密钥时自动跳过并以退出码 0 结束，不影响整体测试套件。

配置方式（三选一）：
    set FINAGENT_API_KEY=sk-xxx
    set FINAGENT_BASE_URL=https://api.deepseek.com/v1
    set FINAGENT_MODEL=deepseek-flash
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.agent.llm import LLMClient
from finagent.agent.loop import AgentLoop
from finagent.config import load_config
from finagent.trace import Trace


def main() -> int:
    cfg = load_config()
    client = LLMClient(cfg)

    if not client.available:
        print("跳过：未配置大模型。")
        print("  设置 FINAGENT_API_KEY / FINAGENT_BASE_URL / FINAGENT_MODEL 后重试。")
        return 0

    print(f"模型 {client.model}  接口 {client.base_url}")
    print("-" * 72)

    # 1. 连通性
    t0 = time.time()
    try:
        reply = client.chat("你是财务分析师，回答务必简短。", "用一句话说明毛利率的定义。")
    except Exception as exc:
        print(f"连通性测试     失败  {exc!r}")
        return 1
    print(f"连通性测试     通过  ({time.time() - t0:.1f}s)  {reply.strip()[:60]}")

    # 2. Function Calling
    try:
        msg = client.chat_messages(
            [{"role": "user", "content": "查一下牧原股份2026Q2的归母净利润。"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_indicator",
                    "description": "读取财务指标",
                    "parameters": {
                        "type": "object",
                        "properties": {"code": {"type": "string"},
                                       "indicator": {"type": "string"},
                                       "period": {"type": "string"}},
                        "required": ["code", "indicator", "period"],
                    },
                },
            }],
        )
        calls = msg.get("tool_calls") or []
        if calls:
            args = calls[0].get("function", {}).get("arguments", "{}")
            print(f"Function Calling 通过  触发 {calls[0]['function']['name']} 参数 {args[:70]}")
        else:
            print("Function Calling 未触发工具调用（模型可能直接作答，属可接受行为）")
    except Exception as exc:
        print(f"Function Calling 失败  {exc!r}")
        return 1

    # 3. 有界编排循环
    print("-" * 72)
    print("编排循环（限制3步，仅验证链路，不做完整分析）")
    trace = Trace()
    loop = AgentLoop(cfg, trace, max_steps=3, verbose=True)
    t0 = time.time()
    result = loop.run("简述牧原股份2026年中报的净利润情况，不要展开。",
                      focus_code="002714.SZ")
    print("-" * 72)
    print(f"模式={result['mode']}  终止={result['stop_reason']}  "
          f"耗时={time.time() - t0:.1f}s  调用={result['llm_calls']}次")
    print(f"token 用量 {result['llm_usage']}")
    ok = result["mode"] == "llm" and result["llm_calls"] > 0
    print("编排循环测试   ", "通过" if ok else "失败")
    trace.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
