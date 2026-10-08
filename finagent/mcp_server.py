"""MCP 服务端：把 FinAgent 的工具集以 Model Context Protocol 对外暴露。

竞赛要求提交"MCP"核心模块。MCP 的意义在于把工具能力与智能体解耦：
任何支持 MCP 的宿主（编辑器、Agent 框架、自研前端）都能以统一协议
调用同一套经过勾稽校验的财务分析工具，而不必复制实现。

传输方式：stdio（单机、零配置，便于评审复现）。
启动：python -m finagent.mcp_server

暴露的工具与内置编排循环使用的是同一份实现（finagent.agent.tools），
因此通过 MCP 外部调用得到的结果，与系统内部结论完全一致。
"""

from __future__ import annotations

import asyncio
import json

import mcp.types as types
from mcp.server import stdio
from mcp.server.lowlevel import Server

from finagent import __version__
from finagent.agent.tools import build_tools
from finagent.config import load_config
from finagent.trace import Trace

SERVER_NAME = "finagent"


def create_server(cfg: dict | None = None, trace=None) -> Server:
    cfg = cfg or load_config()
    registry = build_tools(cfg, trace)
    server = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list:
        tools = []
        for tool in registry.values():
            tools.append(types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.parameters,
            ))
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list:
        tool = registry.get(name)
        if tool is None:
            payload = {"error": f"不存在的工具: {name}",
                       "available": sorted(registry.keys())}
            return [types.TextContent(type="text",
                                      text=json.dumps(payload, ensure_ascii=False))]
        try:
            result = tool.handler(**(arguments or {}))
            ok = True
        except Exception as exc:
            result = {"error": f"工具执行失败: {exc!r}"}
            ok = False
        if trace is not None:
            trace.tool_call(f"mcp:{name}", arguments or {},
                            {"ok": ok}, ok=ok)
        return [types.TextContent(type="text",
                                  text=json.dumps(result, ensure_ascii=False, default=str))]

    return server


async def _serve() -> None:
    cfg = load_config()
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    server = create_server(cfg, trace)
    async with stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())
    trace.close()


def main() -> int:
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
