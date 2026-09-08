"""
文件用途：
- 定义最终诊断结果的数据模型。
- 供 Debug Agent / API / 报告输出复用。

当前状态：
- TODO 草案。
"""

from dataclasses import dataclass, field

from anda.models.evidence import Evidence


@dataclass(slots=True)
class DiagnosisResult:
    finding: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
