"""Runtime Policy 校验；强副作用不能只依赖 Prompt 自律。"""

from __future__ import annotations

from collections.abc import Iterable

from anda.common.errors import DynamicInputError, DynamicPolicyError
from anda.dynamic.models import DEFAULT_RUNTIME_CAPABILITIES, RuntimeCapability


def normalize_runtime_capabilities(
    values: Iterable[str] | None,
) -> frozenset[RuntimeCapability]:
    if values is None:
        return DEFAULT_RUNTIME_CAPABILITIES
    normalized: set[RuntimeCapability] = set()
    invalid: list[str] = []
    for raw in values:
        try:
            normalized.add(RuntimeCapability(str(raw).strip().casefold()))
        except ValueError:
            invalid.append(str(raw))
    if invalid:
        allowed = ", ".join(item.value for item in RuntimeCapability)
        raise DynamicInputError(
            f"未知 Runtime Policy 能力：{', '.join(invalid)}；允许值：{allowed}"
        )
    if RuntimeCapability.OBSERVATION not in normalized:
        normalized.add(RuntimeCapability.OBSERVATION)
    return frozenset(normalized)


def require_capability(
    allowed: frozenset[RuntimeCapability], capability: RuntimeCapability
) -> None:
    if capability not in allowed:
        raise DynamicPolicyError(
            f"当前 Dynamic Session 未授权 {capability.value}；"
            "请关闭后使用明确的 runtime_policy 重新创建 Session"
        )
