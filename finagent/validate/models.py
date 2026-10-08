"""校验层数据模型。

关键设计：把"事实"与"推论"在数据结构层面强制分离。
竞赛要求"明确区分事实、推论与观点"，因此在代码层面就不允许两者混写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """单条证据：可回溯到具体出处与计算公式。"""
    metric: str
    label: str
    value: Any
    period: str
    formula: str = ""
    source: str = ""
    evidence_id: str = ""

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "label": self.label,
            "value": self.value,
            "period": self.period,
            "formula": self.formula,
            "source": self.source,
            "evidence_id": self.evidence_id,
        }


@dataclass
class Finding:
    """一条校验结论。

    level    严重程度：高 / 中 / 提示
    category 分类：异常信号 / 结构性特征 / 口径提示 / 勾稽校验
    statement     事实陈述（只描述数据，禁止推断）
    interpretation 推论（基于事实的归因假设，需进一步验证）
    """
    rule: str
    title: str
    level: str
    category: str
    period: str
    statement: str
    interpretation: str = ""
    evidence: list = field(default_factory=list)
    verify_with: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "title": self.title,
            "level": self.level,
            "category": self.category,
            "period": self.period,
            "fact": self.statement,
            "inference": self.interpretation,
            "verify_with": self.verify_with,
            "evidence": [e.as_dict() if isinstance(e, Evidence) else e for e in self.evidence],
        }
