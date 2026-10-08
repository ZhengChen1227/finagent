"""MCP 服务端协议测试：以标准 MCP 客户端连接并调用工具。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "finagent.mcp_server"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("服务端:", init.serverInfo.name, init.serverInfo.version)

            tools = await session.list_tools()
            print(f"暴露工具 {len(tools.tools)} 个:")
            for tool in tools.tools:
                print(f"   - {tool.name}")

            print()
            res = await session.call_tool("get_indicator",
                                          {"code": "002714", "indicator": "parent_net_profit",
                                           "period": "2026Q2"})
            payload = json.loads(res.content[0].text)
            print("调用 get_indicator ->",
                  payload.get("label"), payload.get("period"), payload.get("value"),
                  "证据:", payload.get("evidence_id"))

            res = await session.call_tool("search_disclosure",
                                          {"query": "非经常性损益", "code": "000725", "topk": 1})
            payload = json.loads(res.content[0].text)
            hit = (payload.get("results") or [{}])[0]
            print("调用 search_disclosure ->", hit.get("citation"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
