"""大模型路径测试：连通性、Function Calling、有界编排循环。

这是系统中唯一依赖外部服务的部分，因此单独验证：网络不通时应当**明确报错**，
而不是悄悄换成别的数据来源。未配置密钥时跳过（退出码 0）。

配置方式（三选一）：
    set FINAGENT_API_KEY=sk-xxx
    set FINAGENT_BASE_URL=https://api.deepseek.com/v1
    set FINAGENT_MODEL=deepseek-chat
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F
from finagent.agent.llm import LLMClient
from finagent.agent.loop import AgentLoop
from finagent.config import ALLOWED_HOST
from finagent.trace import Trace


def main() -> int:
    cfg = F.load_cfg()
    client = LLMClient(cfg)
    print(f"接口 {cfg['llm']['base_url']}  模型 {cfg['llm']['model']}")

    # 网络边界：本项目唯一允许的外部服务就是 DeepSeek。
    host = cfg["llm"]["base_url"].split("//")[-1].split("/")[0]
    if host != ALLOWED_HOST:
        print(f"  [失败] 接口主机 {host} 不在允许清单（{ALLOWED_HOST}）内")
        return 1
    print(f"  [通过] 接口主机 {host} 属于唯一允许的外部服务")

    if not client.available:
        print("跳过：未配置大模型密钥。")
        print("  设置 FINAGENT_API_KEY 后重试；或直接在界面「设置」里填入。")
        return 0

    failures = 0

    # 1. 连通性
    t0 = time.time()
    try:
        reply = client.chat("你是财务分析师，回答务必简短。", "用一句话说明毛利率的定义。")
    except Exception as exc:
        print(f"  [失败] 连通性  {exc!r}")
        return 1
    print(f"  [通过] 连通性 ({time.time() - t0:.1f}s)  {reply.strip()[:50]}")

    # 2. Function Calling：工具名与参数结构必须能被模型正确使用
    try:
        msg = client.chat_messages(
            [{"role": "user", "content": "查一下本次上传材料里营业总收入最新报告期的数值。"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_indicator",
                    "description": "读取财务指标",
                    "parameters": {
                        "type": "object",
                        "properties": {"indicator": {"type": "string"},
                                       "period": {"type": "string"}},
                        "required": ["indicator", "period"],
                    },
                },
            }],
        )
        calls = msg.get("tool_calls") or []
        if calls:
            args = calls[0].get("function", {}).get("arguments", "{}")
            print(f"  [通过] Function Calling 触发 {calls[0]['function']['name']} {args[:60]}")
        else:
            print("  [提示] 模型未触发工具调用（直接作答属可接受行为）")
    except Exception as exc:
        print(f"  [失败] Function Calling  {exc!r}")
        failures += 1

    # 3. 有界编排循环
    us, key = F.uploads("002714")
    if us is None:
        print(F.missing_message("002714"))
        print(f"大模型路径测试失败项：{failures}")
        return 1 if failures else 0
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    loop = AgentLoop(cfg, trace, max_steps=3, verbose=True,
                     focus_key=key, uploads=us)
    t0 = time.time()
    result = loop.run("简述本次上传材料中归母净利润的最新情况，不要展开。")
    print(f"  模式 {result['mode']}  终止 {result['stop_reason']}  "
          f"耗时 {time.time() - t0:.1f}s  调用 {result['llm_calls']} 次  "
          f"token {result['llm_usage']}")
    ok = result["mode"] == "llm" and result["llm_calls"] > 0
    print(f"  [{'通过' if ok else '失败'}] 编排循环")
    failures += 0 if ok else 1
    trace.close()
    print(f"大模型路径测试失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
