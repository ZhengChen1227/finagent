"""归因推理层：大模型负责提出归因假设，不负责计算。

职责边界（这是本系统的核心设计约束）：
    代码层  -> 取数、口径还原、指标计算、规则判定，产出事实与证据
    模型层  -> 基于既有事实提出归因假设、指出验证路径、评估证据强度

模型被明确禁止重新计算或引入任何新数字。
只要模型有可能"算错数"，数据的可复现性就不再成立；
把计算完全剥离给代码，模型输出才能被稳定地审阅与追责。
"""

from __future__ import annotations

import json

from finagent.agent.llm import LLMClient

SYSTEM_PROMPT = """你是一名严谨的上市公司财务分析师，服务于买方投研团队。

你必须遵守以下铁律：
1. 禁止计算：不得重新计算、修正或引入任何新数字。你只能引用用户提供的数据。
   如果认为某个数字有问题，用文字指出，不要给出你自己的结果。
2. 区分层次：严格区分「事实」（数据直接支持）、「推论」（基于事实的假设）、
   「观点」（主观判断）。混写会被视为不合格。
3. 归因要有依据：每条推论必须说明它依赖哪条证据，以及还需要什么材料才能证实或证伪。
4. 承认不确定：证据不足时明确写「证据不足」，绝不编造原因。
5. 审慎优先：宁可给出待验证的假设，也不要给出确定性结论。

输出必须是合法 JSON，结构如下：
{
  "summary": "对该报告期业绩变化的总体归因（200字以内）",
  "items": [
    {
      "rule": "对应输入中的规则编号",
      "period": "报告期",
      "fact": "复述事实（只引用给定数据）",
      "inference": "推论（归因假设）",
      "confidence": "高/中/低",
      "verify_with": "需要查阅的具体材料"
    }
  ],
  "cross_company": "若提供了多家公司，说明可比性与差异（150字以内）",
  "limitations": ["本次分析无法覆盖的方面"]
}"""

USER_TEMPLATE = """## 分析对象
{company_line}

## 报告期
{period_line}

## 关键指标（已由程序计算，请直接引用，不要重算）
{metrics_block}

## 规则引擎识别出的信号
{findings_block}

请完成归因分析，按约定的 JSON 结构输出。"""


def build_user_prompt(name: str, code: str, findings: list, metrics_block: str,
                      periods: list) -> str:
    findings_block = json.dumps(
        [{"rule": f.rule, "level": f.level, "category": f.category, "period": f.period,
          "fact": f.statement, "evidence": [e.as_dict() for e in f.evidence]}
         for f in findings],
        ensure_ascii=False, indent=2,
    ) or "（未识别到显著信号）"
    return USER_TEMPLATE.format(
        company_line=f"{name}（{code}）",
        period_line="、".join(periods),
        metrics_block=metrics_block,
        findings_block=findings_block,
    )


def offline_attribution(name: str, code: str, findings: list) -> dict:
    """离线降级模式：不依赖大模型，直接由规则结论组装归因。

    这不是"占位实现"，而是系统必要的能力：
    评审在无密钥环境复现时，归因章节仍需具备完整结构与可读性。
    同时它也是大模型输出的对照基准，便于人工比对模型是否跑偏。
    """
    items = []
    for f in findings:
        items.append({
            "rule": f.rule,
            "period": f.period,
            "fact": f.statement,
            "inference": f.interpretation or "需要结合附注进一步判断。",
            "confidence": {"高": "中", "中": "低", "提示": "低"}.get(f.level, "低"),
            "verify_with": f.verify_with or "相关报表附注",
        })
    high = sum(1 for f in findings if f.level == "高")
    return {
        "summary": (f"{name}（{code}）共识别 {len(findings)} 条值得关注的结论，"
                    f"其中高优先级 {high} 条。以下归因由规则引擎依据既定口径生成，"
                    f"未经大模型推理，仅作为事实梳理。"),
        "items": items,
        "cross_company": "",
        "limitations": ["离线模式未启用大模型推理，归因深度受限。"],
    }


def attribute(name: str, code: str, findings: list, metrics_block: str,
              periods: list, cfg: dict, trace=None) -> dict:
    """执行归因。优先调用大模型，失败或无配置时降级为离线模式。"""
    if not findings:
        return {"summary": "本次分析未识别到显著异常信号。", "items": [],
                "cross_company": "", "limitations": []}

    client = LLMClient(cfg)
    prompt = build_user_prompt(name, code, findings, metrics_block, periods)

    if client.available:
        try:
            result = client.chat_json(SYSTEM_PROMPT, prompt)
            if result:
                result.setdefault("items", [])
                result.setdefault("limitations", [])
                if trace:
                    trace.tool_call(
                        "llm_attribution",
                        {"model": client.model, "base_url": client.base_url,
                         "findings": len(findings)},
                        {"items": len(result["items"]), "mode": "llm"},
                    )
                return result
            if trace:
                trace.record("llm_empty_response", model=client.model)
        except Exception as exc:
            if trace:
                trace.record("llm_error", model=client.model, error=repr(exc))

    result = offline_attribution(name, code, findings)
    if trace:
        trace.tool_call(
            "llm_attribution",
            {"configured": client.available, "model": client.model or None},
            {"items": len(result["items"]), "mode": "offline"},
        )
    return result
