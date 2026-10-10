"""智能体编排循环（ReAct + Function Calling）。

竞赛要求"大语言模型作为核心推理引擎""复杂任务的组织执行能力"。
本模块实现工具调用循环：模型自主决定下一步查什么、算什么，
直到证据充分或触发终止条件。

与"固定流水线末端挂模型"的本质区别在于决策权归属：
流水线的执行路径由代码写死，本循环的执行路径由模型在每一步现场决定。
前者只能叫自动化脚本，后者才构成智能体。

三重终止条件（全部记录在轨迹中，可审计）：
    1. 模型主动收尾（输出最终 JSON）
    2. 达到最大步数上限
    3. 连续两轮无新增信息（防止无效空转）

本模块**没有离线降级路径**：没有可用的大模型密钥时直接报错，
而不是退化成脚本。理由是"大语言模型作为核心推理引擎"是赛题的硬性要求，
一个能绕过模型的降级分支会让"智能体"退化成固定流水线，
也让评审无法判断系统究竟是模型在决策还是代码在决策。
"""

from __future__ import annotations

import json

from finagent.agent import prompts
from finagent import skills as skill_lib
from finagent.agent.llm import LLMClient, parse_json_loose
from finagent.agent.tools import build_tools, tool_schemas

MAX_OBSERVATION_CHARS = 4000


class AgentLoop:
    def __init__(self, cfg: dict, trace=None, max_steps: int = 12,
                 verbose: bool = True, focus_key: str | None = None,
                 uploads=None) -> None:
        self.cfg = cfg
        self.trace = trace
        self.max_steps = max_steps
        self.verbose = verbose
        self.focus_key = focus_key
        self.uploads = uploads
        self.registry = build_tools(cfg, trace, uploads=uploads, focus_key=focus_key)
        self.client = LLMClient(cfg)
        self._cache: dict = {}
        self._cache: dict = {}

    # ---------------- 工具执行 ----------------

    def _invoke(self, name: str, args: dict) -> dict:
        """执行工具调用。重复调用相同参数时直接返回缓存，避免无效空转。"""
        signature = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if signature in self._cache:
            cached = dict(self._cache[signature])
            cached["_note"] = "与之前完全相同的调用，结果一致，已从缓存返回"
            return cached

        tool = self.registry.get(name)
        if tool is None:
            return {"error": f"不存在的工具: {name}",
                    "available": sorted(self.registry.keys())}

        import time
        t0 = time.perf_counter()
        try:
            result = tool.handler(**args)
            ok = True
        except TypeError as exc:
            result = {"error": f"参数错误: {exc}"}
            ok = False
        except Exception as exc:
            result = {"error": f"工具执行失败: {exc!r}"}
            ok = False
        ms = round((time.perf_counter() - t0) * 1000, 1)

        self._cache[signature] = result
        if self.trace:
            self.trace.tool_call(name, args, _summarize(result), ok=ok, duration_ms=ms)
        return result

    # ---------------- 主循环 ----------------

    def run(self, objective: str, hint: str = "", focus_key: str | None = None) -> dict:
        if not self.client.available:
            # 没有密钥就不跑。这是有意的硬失败：
            # 静默产出"事实层结论"会让人以为分析已经完成，比直接报错危险得多。
            message = ("未配置大模型 API Key，无法启动推理。"
                       "请在界面「设置」中填入自己的 DeepSeek API Key 后重试。")
            if self.trace:
                self.trace.record("agent_start", objective=objective,
                                  mode="unavailable", error=message)
            raise RuntimeError(message)
        if self.trace:
            self.trace.record(
                "agent_start",
                objective=objective,
                mode="llm",
                model=self.client.model or None,
                max_steps=self.max_steps,
                prompt_hashes=prompts.manifest(),
                skill_hashes=skill_lib.manifest(),
                skills_matched=[x["name"] for x in skill_lib.match(objective)],
                tools=sorted(self.registry.keys()),
            )
        return self._llm_run(objective, hint)

    def _llm_run(self, objective: str, hint: str) -> dict:
        # 技能按任务关键词自动匹配并注入系统提示。
        # 这与"把所有规则都塞进提示词"不同：只加载本次任务真正相关的方法论，
        # 既节省上下文，也让模型的注意力集中在当前分析类型上。
        system_prompt = prompts.load("system_agent")
        matched = skill_lib.compose(objective)
        if matched:
            system_prompt = system_prompt + "\n\n" + matched
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": objective + (f"\n\n{hint}" if hint else "")},
        ]
        schemas = tool_schemas(self.registry)
        transcript: list = []
        stale_rounds = 0
        stop_reason = "max_steps"

        for step in range(1, self.max_steps + 1):
            try:
                message = self.client.chat_messages(messages, tools=schemas)
            except Exception as exc:
                if self.trace:
                    self.trace.record("agent_llm_error", step=step, error=repr(exc))
                stop_reason = "llm_error"
                break

            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content") or ""
                final = parse_json_loose(content)
                transcript.append({"step": step, "type": "final", "content": content[:2000]})
                stop_reason = "model_finished"
                return self._finish("llm", stop_reason, transcript, final, messages)

            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": calls})
            produced = False
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._invoke(name, args)
                produced = produced or "_note" not in result
                observation = json.dumps(result, ensure_ascii=False, default=str)
                if len(observation) > MAX_OBSERVATION_CHARS:
                    observation = observation[:MAX_OBSERVATION_CHARS] + "…（已截断）"
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": observation})
                transcript.append({"step": step, "type": "tool", "tool": name,
                                   "args": args, "ok": "error" not in result,
                                   "summary": _summarize(result)})
                if self.verbose:
                    print(f"   [{step}] {name}({_brief(args)}) -> {_oneline(result)}")

            stale_rounds = 0 if produced else stale_rounds + 1
            if stale_rounds >= 2:
                stop_reason = "no_new_information"
                break

        # 步数耗尽或空转：要求模型基于已有信息收尾
        messages.append({"role": "user", "content":
                         "已达探查上限。请立即基于已获得的信息输出最终 JSON 结论，不要再调用工具。"})
        try:
            message = self.client.chat_messages(messages)
            final = parse_json_loose(message.get("content") or "")
        except Exception as exc:
            if self.trace:
                self.trace.record("agent_llm_error", stage="finalize", error=repr(exc))
            final = {}
        transcript.append({"step": "final", "type": "finalize", "reason": stop_reason})
        return self._finish("llm", stop_reason, transcript, final, messages)

    # ---------------- 收尾 ----------------

    def _finish(self, mode: str, stop_reason: str, transcript: list,
                final: dict, messages) -> dict:
        if self.trace:
            self.trace.record(
                "agent_end", mode=mode, stop_reason=stop_reason,
                steps=len(transcript),
                tool_calls=self.client.calls if mode == "llm" else len(transcript),
                llm_usage=self.client.usage if mode == "llm" else None,
                conclusion_keys=sorted(final.keys()),
            )
        return {
            "mode": mode,
            "stop_reason": stop_reason,
            "transcript": transcript,
            "final": final,
            "llm_calls": self.client.calls,
            "llm_usage": self.client.usage,
            "prompt_hashes": prompts.manifest(),
            "skill_hashes": skill_lib.manifest(),
        }


# ---------------- 辅助 ----------------

def _summarize(result) -> dict:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    out = {}
    for key, value in result.items():
        if isinstance(value, list):
            out[key] = f"{len(value)} 项"
        elif isinstance(value, dict):
            out[key] = "…"
        elif isinstance(value, str):
            out[key] = value[:120]
        else:
            out[key] = value
    return out


def _oneline(result) -> str:
    if not isinstance(result, dict):
        return str(result)[:80]
    if "error" in result:
        return f"错误: {result['error']}"
    for key in ("count", "documents", "results", "comparison", "findings"):
        if key in result:
            value = result[key]
            n = len(value) if isinstance(value, list) else value
            return f"{key}={n}"
    if "value" in result:
        return f"{result.get('indicator')}={result['value']}"
    return "ok"


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])


def _verify_query(finding: dict) -> str:
    rule = finding.get("rule", "")
    return {
        "R1": "资产减值损失 存货跌价准备",
        "R3a": "所得税费用 递延所得税资产",
        "R3b": "所得税费用 递延所得税",
        "R4": "非经常性损益 政府补助",
        "R5": "营业收入 营业成本",
        "R6": "应收账款 账龄",
        "R7": "固定资产折旧 折旧年限",
    }.get(rule, "合并财务报表 主要会计政策")
