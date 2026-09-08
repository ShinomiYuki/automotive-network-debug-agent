"""
文件用途：
- 定义跨模块使用的“证据”结构。
- 后续主智能体不应接收大量原始数据，而应接收这类压缩证据。

当前状态：
- 初始模型草案。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Evidence:
    source: str
    summary: str
    details: dict[str, Any]
