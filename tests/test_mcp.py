"""MCP 服务端协议测试：以标准 MCP 客户端连接并调用工具。

竞赛要求提交 MCP 核心模块。这里用官方 mcp 客户端走一遍完整协议
（握手 → 列举工具 → 调用工具），确认外部宿主拿到的是与系统内部
完全相同的工具与结论，而不是另写一套。

需要先有一批上传材料（用 FINAGENT_RUN 指定，默认取 data/uploads 下最近一次）。
没有材料时跳过并说明原因。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _fixtures as F

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = 12


async def main() -> int:
    # 保证有一批材料可供 MCP 服务端绑定：MaterialBatch 与界面同源。
    us, key = F.uploads("002714")
    if us is None:
        print(F.missing_message("002714"))
        return 0

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "finagent.mcp_server"],
        cwd=str(ROOT),
        env=dict(os.environ, FINAGENT_RUN=F.TEST_RUN, PYTHONIOENCODING="utf-8"),
    )
    failures = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("=" * 74)
            print(f"服务端 {init.serverInfo.name} {init.serverInfo.version}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"暴露工具 {len(names)} 个：{names}")
            if len(names) != EXPECTED_TOOLS:
                failures += 1
                print(f"  [失败] 工具数应为 {EXPECTED_TOOLS}，实际 {len(names)}")
            else:
                print(f"  [通过] 工具数与内置编排一致（{EXPECTED_TOOLS} 个）")

            res = await session.call_tool("list_materials", {})
            payload = json.loads(res.content[0].text)
            count = payload.get("documents")
            ok = bool(count)
            print(f"  [{'通过' if ok else '失败'}] list_materials 返回 {count} 份材料")
            failures += 0 if ok else 1

            res = await session.call_tool("get_indicator", {
                "indicator": "revenue", "period": "2026Q2", "key": key})
            payload = json.loads(res.content[0].text)
            ok = payload.get("value") == 59410306384.59
            print(f"  [{'通过' if ok else '失败'}] get_indicator 营业总收入 "
                  f"{payload.get('value')} 证据 {payload.get('evidence_id')} "
                  f"第 {payload.get('page')} 页")
            failures += 0 if ok else 1

            res = await session.call_tool("search_disclosure", {
                "query": "非经常性损益", "topk": 1})
            payload = json.loads(res.content[0].text)
            hit = (payload.get("results") or [{}])[0]
            ok = bool(hit.get("citation"))
            print(f"  [{'通过' if ok else '失败'}] search_disclosure -> {hit.get('citation')}")
            failures += 0 if ok else 1

    print("=" * 74)
    print(f"MCP 测试失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
