"""MCP 服务端：把 FinAgent 的工具集以 Model Context Protocol 对外暴露。

竞赛要求提交"MCP"核心模块。MCP 的意义在于把工具能力与智能体解耦：
任何支持 MCP 的宿主（编辑器、Agent 框架、自研前端）都能以统一协议
调用同一套经过勾稽校验的财务分析工具，而不必复制实现。

传输方式：stdio（单机、零配置，便于评审复现）。
启动：python -m finagent.mcp_server

工具作用于"某一次上传的财报材料"。用环境变量 FINAGENT_RUN 指定运行编号；
不指定时取 data/uploads 下最近的一次上传。

暴露的工具与内置编排循环使用的是同一份实现（finagent.agent.tools），
因此通过 MCP 外部调用得到的结果，与系统内部结论完全一致。
"""

from __future__ import annotations

import asyncio
import json

import mcp.types as types
from mcp.server import stdio
from mcp.server.lowlevel import Server

import os

from finagent import __version__
from finagent.agent.tools import build_tools
from finagent.config import load_config, uploads_dir
from finagent.ingest.dataset import UploadSet
from finagent.trace import Trace

SERVER_NAME = "finagent"


def create_server(cfg: dict | None = None, trace=None, uploads=None,
                  focus_key: str | None = None) -> Server:
    cfg = cfg or load_config()
    registry = build_tools(cfg, trace, uploads=uploads, focus_key=focus_key)
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


def open_uploads(cfg: dict, trace=None):
    """绑定某一次上传材料。默认取最近一次上传，使 MCP 外部调用与界面同源。"""
    root = uploads_dir(cfg)
    run_id = os.environ.get("FINAGENT_RUN") or None
    if not run_id and os.path.isdir(root):
        runs = [d for d in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, d)) and not d.startswith("_")]
        run_id = runs[-1] if runs else None
    if not run_id:
        return None
    return UploadSet(run_id=run_id, root=root, trace=trace)


async def _serve() -> None:
    cfg = load_config()
    trace = Trace(out_dir=cfg["output"]["trace_dir"])
    uploads = open_uploads(cfg, trace)
    server = create_server(cfg, trace, uploads=uploads)
    async with stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())
    trace.close()


def main() -> int:
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
