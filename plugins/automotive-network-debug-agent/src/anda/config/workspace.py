"""
文件用途：
- 管理可复用的 Config Workspace，并协调 ARXML 与源码索引。
- 对外提供 Config MCP 所需的确定性查询，不承载 Agent 调查策略。

Workspace 是当前 MCP 进程内的只读快照。重复加载同一组路径直接复用；工程发生变化时，
调用方可显式 ``force_reload=True`` 建立新快照，避免每次查询都递归扫描整个工程。
"""

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from anda.common.errors import (
    ConfigConflictError,
    ConfigInputError,
    ConfigNotFoundError,
)
from anda.config.arxml import normalize_identifier, parse_arxml_files, reference_name
from anda.config.inspection import (
    inspect_communication,
    inspect_ipdu_group,
    trace_signal_gateway,
)
from anda.config.models import ArxmlIndex, ConfigObject
from anda.config.routing import trace_message_route
from anda.config.source import SourceIndex

ARXML_EXTENSIONS = {".arxml", ".xml"}
SOURCE_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".inc"}
SUPPORTED_EXTENSIONS = ARXML_EXTENSIONS | SOURCE_EXTENSIONS
IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
MAX_SYMBOL_RESULTS = 50


@dataclass(slots=True)
class ConfigWorkspace:
    """一份工程配置的进程内只读快照。"""

    root: Path
    files: tuple[Path, ...]
    file_type_counts: dict[str, int]
    arxml: ArxmlIndex
    source: SourceIndex


class ConfigWorkspaceManager:
    """加载并查询 Config Workspace。"""

    def __init__(self) -> None:
        self._workspaces: dict[str, ConfigWorkspace] = {}
        self._root_to_id: dict[str, str] = {}
        self._load_lock = Lock()

    def load_config_workspace(
        self,
        root_path: str,
        arxml_paths: list[str] | None = None,
        force_reload: bool = False,
    ) -> dict:
        scope = Path(root_path).expanduser().resolve()
        if scope.is_dir():
            source_root = scope
            initial_files: list[Path] | None = None
        elif scope.is_file() and scope.suffix.casefold() in SUPPORTED_EXTENSIONS:
            source_root = scope.parent
            initial_files = [scope]
        else:
            raise ConfigInputError(f"工程路径不存在，或不是受支持的目录/文件：{scope}")
        explicit_arxml_files = _resolve_explicit_arxml_paths(arxml_paths)
        root_key = _workspace_cache_key(scope, explicit_arxml_files)

        with self._load_lock:
            existing_id = self._root_to_id.get(root_key)
            if existing_id is not None and not force_reload:
                return self._load_result(
                    existing_id, self._workspaces[existing_id], reused=True
                )

            files = initial_files or _scan_supported_files(scope)
            if arxml_paths:
                # 显式指定 ARXML 时，以用户选择为准，不混入工程目录中可能属于其他
                # 项目或历史版本的 ARXML；工程目录中的 C/H 仍用于生成代码定位。
                files = [
                    path
                    for path in files
                    if path.suffix.casefold() in SOURCE_EXTENSIONS
                ]
                files.extend(explicit_arxml_files)
                files = list(dict.fromkeys(files))
            if not files:
                supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
                raise ConfigInputError(
                    f"工程路径中没有支持的配置或源码文件：{scope}；支持：{supported}"
                )
            arxml_files = [
                path for path in files if path.suffix.casefold() in ARXML_EXTENSIONS
            ]
            source_files = [
                path for path in files if path.suffix.casefold() in SOURCE_EXTENSIONS
            ]
            arxml_index = parse_arxml_files(arxml_files)
            source_index = SourceIndex(source_root, source_files)
            workspace = ConfigWorkspace(
                root=scope,
                files=tuple(files),
                file_type_counts=dict(
                    Counter(path.suffix.casefold() for path in files)
                ),
                arxml=arxml_index,
                source=source_index,
            )
            workspace_id = str(uuid4())
            self._workspaces[workspace_id] = workspace
            self._root_to_id[root_key] = workspace_id
            if existing_id is not None:
                self._workspaces.pop(existing_id, None)
            return self._load_result(workspace_id, workspace, reused=False)

    @staticmethod
    def _load_result(
        workspace_id: str, workspace: ConfigWorkspace, reused: bool
    ) -> dict:
        source_file_count = sum(
            count
            for extension, count in workspace.file_type_counts.items()
            if extension in SOURCE_EXTENSIONS
        )
        arxml_file_count = sum(
            count
            for extension, count in workspace.file_type_counts.items()
            if extension in ARXML_EXTENSIONS
        )
        return {
            "workspace_id": workspace_id,
            "root_path": str(workspace.root),
            "reused": reused,
            "supported_file_count": len(workspace.files),
            "file_type_counts": workspace.file_type_counts,
            "arxml_file_count": arxml_file_count,
            "source_file_count": source_file_count,
            "arxml_object_count": len(workspace.arxml.objects),
            "message_count": len(workspace.arxml.messages),
            "routing_path_count": len(workspace.arxml.routes),
            "signal_gateway_count": len(workspace.arxml.signal_gateways),
            "source_symbol_count": workspace.source.symbol_count,
            "source_occurrence_count": workspace.source.occurrence_count,
            "workspace_mode": (
                "source_plus_arxml"
                if source_file_count and arxml_file_count
                else "source_only"
                if source_file_count
                else "arxml_only"
            ),
            "arxml_enrichment_enabled": bool(workspace.arxml.objects),
            "index_ready": True,
        }

    def search_config_symbol(
        self, workspace_id: str, query: str, limit: int = 20
    ) -> dict:
        normalized_query = query.strip()
        if not normalized_query:
            raise ConfigInputError("query 不能为空")
        if limit < 1:
            raise ConfigInputError("limit 必须大于等于 1")
        workspace = self._get(workspace_id)
        applied_limit = min(limit, MAX_SYMBOL_RESULTS)
        results = _search_arxml(workspace.arxml, normalized_query)

        source_matches = workspace.source.search_symbols(
            normalized_query, limit=MAX_SYMBOL_RESULTS
        )
        for candidate in source_matches["matches"]:
            occurrence = candidate["sample_locations"][0]
            results.append(
                {
                    "match_type": occurrence["file_kind"],
                    "name": candidate["symbol"],
                    "matched_by": f"source_symbol_{candidate['matched_by']}",
                    "occurrence_count": candidate["occurrence_count"],
                    "role_counts": candidate["role_counts"],
                    "file": occurrence["file"],
                    "line": occurrence["line"],
                    "evidence": occurrence,
                }
            )

        results = _deduplicate_results(results)
        results.sort(
            key=lambda item: (
                0 if item["matched_by"].endswith("exact") else 1,
                item["match_type"],
                item["name"].casefold(),
                item["file"].casefold(),
                item["line"],
            )
        )
        return {
            "workspace_id": workspace_id,
            "query": normalized_query,
            "total_count": len(results),
            "returned_count": min(len(results), applied_limit),
            "limit_applied": applied_limit,
            "matches": results[:applied_limit],
        }

    def trace_message_route(
        self,
        workspace_id: str,
        arbitration_id: int | None = None,
        message_name: str | None = None,
        source_network: str | None = None,
        destination_network: str | None = None,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = trace_message_route(
            index=workspace.arxml,
            source_index=workspace.source,
            arbitration_id=arbitration_id,
            message_name=message_name,
            source_network=source_network,
            destination_network=destination_network,
        )
        result["workspace_id"] = workspace_id
        return result

    def search_source_symbol(
        self, workspace_id: str, query: str, limit: int = 20
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.search_symbols(query, limit)
        result["workspace_id"] = workspace_id
        return result

    def inspect_source_symbol(
        self,
        workspace_id: str,
        symbol: str,
        limit: int = 20,
        context_lines: int = 2,
    ) -> dict:
        """以源码符号为入口，返回源码事实及同名 ARXML 配置证据。"""
        workspace = self._get(workspace_id)
        normalized_symbol = symbol.strip()
        result = workspace.source.inspect_symbol(
            normalized_symbol, limit, context_lines
        )
        if result["total_count"] == 0:
            raise ConfigNotFoundError(f"工程源码中未找到标识符：{normalized_symbol}")
        config_matches = [
            item
            for item in _search_arxml(workspace.arxml, normalized_symbol)
            if item["matched_by"]
            in {
                "name_exact",
                "reference_exact",
            }
        ]
        result.update(
            {
                "workspace_id": workspace_id,
                "config_link_basis": "exact_same_identifier",
                "config_match_count": len(config_matches),
                "config_matches": config_matches[:MAX_SYMBOL_RESULTS],
                "config_matches_truncated": len(config_matches) > MAX_SYMBOL_RESULTS,
            }
        )
        return result

    def inspect_communication(
        self,
        workspace_id: str,
        arbitration_id: int | None = None,
        message_name: str | None = None,
        pdu_name: str | None = None,
        network: str | None = None,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = inspect_communication(
            workspace.arxml,
            workspace.source,
            arbitration_id,
            message_name,
            pdu_name,
            network,
        )
        result["workspace_id"] = workspace_id
        return result

    def trace_signal_gateway(
        self, workspace_id: str, signal_name: str, limit: int = 20
    ) -> dict:
        workspace = self._get(workspace_id)
        result = trace_signal_gateway(
            workspace.arxml, workspace.source, signal_name, limit
        )
        result["workspace_id"] = workspace_id
        return result

    def inspect_ipdu_group(self, workspace_id: str, group_name: str) -> dict:
        workspace = self._get(workspace_id)
        result = inspect_ipdu_group(workspace.arxml, workspace.source, group_name)
        result["workspace_id"] = workspace_id
        return result

    def inspect_pdu(self, workspace_id: str, pdu_name: str) -> dict:
        normalized_name = pdu_name.strip()
        if not normalized_name:
            raise ConfigInputError("pdu_name 不能为空")
        workspace = self._get(workspace_id)
        objects = _matching_pdu_objects(workspace.arxml, normalized_name)
        known_references = _known_pdu_references(workspace.arxml, normalized_name)
        if not objects and not known_references:
            raise ConfigNotFoundError(f"工程配置中未找到 PDU：{normalized_name}")
        if len(objects) > 1:
            candidates = ", ".join(item.reference_path for item in objects[:10])
            raise ConfigConflictError(
                f"PDU 名称存在多个定义，请使用完整引用路径：{candidates}"
            )
        if not objects and len(known_references) > 1:
            candidates = ", ".join(sorted(known_references)[:10])
            raise ConfigConflictError(
                f"PDU 短名对应多个引用，请使用完整引用路径：{candidates}"
            )

        reference = objects[0].reference_path if objects else min(known_references)
        networks = _networks(workspace.arxml, reference)
        message_mappings = [
            {
                "message": message.name,
                "arbitration_id": message.arbitration_id,
                "arbitration_id_hex": f"0x{message.arbitration_id:X}",
                "network": message.network,
                "evidence": [item.as_dict() for item in message.evidence],
            }
            for message in workspace.arxml.messages
            if any(_same_pdu(reference, item) for item in message.pdu_references)
        ]
        as_source = [
            route
            for route in workspace.arxml.routes
            if route.source and _same_pdu(reference, route.source.pdu_reference)
        ]
        as_destination = [
            route
            for route in workspace.arxml.routes
            if any(
                _same_pdu(reference, endpoint.pdu_reference)
                for endpoint in route.destinations
            )
        ]
        return {
            "workspace_id": workspace_id,
            "pdu_name": reference_name(reference),
            "reference": reference,
            "type": objects[0].kind if objects else "referenced_pdu",
            "networks": networks,
            "defined_at": objects[0].evidence.as_dict() if objects else None,
            "message_mappings": message_mappings[:20],
            "routing_as_source": [_route_summary(route) for route in as_source[:20]],
            "routing_as_destination": [
                _route_summary(route) for route in as_destination[:20]
            ],
            "generated_locations": workspace.source.locations_for_symbols(
                [reference_name(reference)], limit=20
            ),
        }

    def find_source_context(
        self,
        workspace_id: str,
        symbol: str,
        limit: int = 20,
        context_lines: int = 2,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.contexts(symbol.strip(), limit, context_lines)
        result["workspace_id"] = workspace_id
        return result

    def _get(self, workspace_id: str) -> ConfigWorkspace:
        try:
            return self._workspaces[workspace_id]
        except KeyError as error:
            raise ConfigNotFoundError(f"未知 workspace_id：{workspace_id}") from error


def _scan_supported_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.casefold() not in IGNORED_DIRECTORIES
        )
        for file_name in sorted(file_names):
            path = Path(current_root, file_name)
            if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue
            # 不通过目录内符号链接读取用户未放入 Workspace 范围的其他工程。
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                files.append(resolved)
    return files


def _resolve_explicit_arxml_paths(paths: list[str] | None) -> list[Path]:
    """展开用户明确选择的 ARXML 文件/目录，并拒绝含糊或不支持的路径。"""
    if not paths:
        return []
    result: list[Path] = []
    for raw_path in paths:
        if not raw_path.strip():
            raise ConfigInputError("arxml_paths 不能包含空路径")
        path = Path(raw_path).expanduser().resolve()
        if path.is_file() and path.suffix.casefold() in ARXML_EXTENSIONS:
            result.append(path)
            continue
        if path.is_dir():
            result.extend(
                candidate
                for candidate in _scan_supported_files(path)
                if candidate.suffix.casefold() in ARXML_EXTENSIONS
            )
            continue
        raise ConfigInputError(f"ARXML 路径不存在或格式不受支持：{path}")
    if not result:
        raise ConfigInputError("arxml_paths 中没有找到 .arxml 或 .xml 文件")
    return list(dict.fromkeys(result))


def _workspace_cache_key(scope: Path, arxml_files: list[Path]) -> str:
    # 显式 ARXML 的输入顺序不改变 Workspace 内容，不应因此重复建立索引。
    parts = [
        str(scope).casefold(),
        *(sorted(str(path).casefold() for path in arxml_files)),
    ]
    return "\0".join(parts)


def _search_arxml(index: ArxmlIndex, query: str) -> list[dict]:
    result: list[dict] = []
    normalized = query.casefold()
    parsed_id = _parse_can_id(query)
    for message in index.messages:
        name_match = message.name.casefold() == normalized
        partial_match = normalized in message.name.casefold()
        id_match = parsed_id is not None and message.arbitration_id == parsed_id
        if not (name_match or partial_match or id_match):
            continue
        evidence = message.evidence[0].as_dict()
        result.append(
            {
                "match_type": "message",
                "name": message.name,
                "matched_by": (
                    "can_id_exact"
                    if id_match
                    else "name_exact"
                    if name_match
                    else "name_partial"
                ),
                "arbitration_id": message.arbitration_id,
                "arbitration_id_hex": f"0x{message.arbitration_id:X}",
                "network": message.network,
                "pdu_references": list(message.pdu_references),
                "file": evidence["file"],
                "line": evidence["line"],
                "evidence": evidence,
            }
        )

    for item in index.objects:
        name_match = item.name.casefold() == normalized
        partial_match = normalized in item.name.casefold()
        long_name_match = bool(
            item.long_name and normalized in item.long_name.casefold()
        )
        reference_match = any(
            reference_name(reference).casefold() == normalized
            for reference in item.references
        )
        if not (name_match or partial_match or long_name_match or reference_match):
            continue
        result.append(
            {
                "match_type": _object_match_type(item),
                "name": item.name,
                "matched_by": (
                    "name_exact"
                    if name_match
                    else "reference_exact"
                    if reference_match
                    else "long_name_partial"
                    if long_name_match
                    else "name_partial"
                ),
                "long_name": item.long_name,
                "object_type": item.kind,
                "reference_path": item.reference_path,
                "file": item.evidence.file,
                "line": item.evidence.line,
                "evidence": item.evidence.as_dict(),
            }
        )
    return result


def _object_match_type(item: ConfigObject) -> str:
    signature = normalize_identifier(
        f"{item.kind} {item.name} {item.definition_ref or ''}"
    )
    if "PDURROUTINGPATH" in signature:
        return "routing_path"
    if (
        "PDU" in item.kind
        and "MAPPING" not in item.kind
        and "TRIGGERING" not in item.kind
    ):
        return "pdu"
    if "SIGNAL" in item.kind:
        return "signal"
    return "config_object"


def _parse_can_id(query: str) -> int | None:
    value = query.strip()
    try:
        if value.casefold().startswith("0x") or value.isdecimal():
            parsed = int(value, 0)
            return parsed if 0 <= parsed <= 0x1FFFFFFF else None
    except ValueError:
        return None
    return None


def _deduplicate_results(results: list[dict]) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for item in results:
        key = (
            item["match_type"],
            item["name"].casefold(),
            item["file"].casefold(),
            item["line"],
        )
        unique[key] = item
    return list(unique.values())


def _matching_pdu_objects(index: ArxmlIndex, value: str) -> list[ConfigObject]:
    normalized = value.casefold()
    short_name = reference_name(value).casefold()
    if value.startswith("/"):
        return [
            item
            for item in index.objects
            if _is_pdu_object(item) and item.reference_path.casefold() == normalized
        ]
    return [
        item
        for item in index.objects
        if _is_pdu_object(item) and item.name.casefold() == short_name
    ]


def _is_pdu_object(item: ConfigObject) -> bool:
    return (
        "PDU" in item.kind
        and "MAPPING" not in item.kind
        and "TRIGGERING" not in item.kind
    )


def _known_pdu_references(index: ArxmlIndex, value: str) -> set[str]:
    result: set[str] = set()
    for message in index.messages:
        result.update(
            reference
            for reference in message.pdu_references
            if _same_pdu(value, reference)
        )
    for route in index.routes:
        if route.source and _same_pdu(value, route.source.pdu_reference):
            result.add(route.source.pdu_reference)
        result.update(
            endpoint.pdu_reference
            for endpoint in route.destinations
            if _same_pdu(value, endpoint.pdu_reference)
        )
    return result


def _same_pdu(first: str, second: str) -> bool:
    if first.casefold() == second.casefold():
        return True
    if first.startswith("/") and second.startswith("/"):
        return False
    return reference_name(first).casefold() == reference_name(second).casefold()


def _networks(index: ArxmlIndex, reference: str) -> list[str]:
    result = set(index.pdu_networks.get(reference.casefold(), ()))
    if not result:
        result.update(index.pdu_networks.get(reference_name(reference).casefold(), ()))
    return sorted(result, key=str.casefold)


def _route_summary(route) -> dict:
    return {
        "name": route.name,
        "source_pdu": reference_name(route.source.pdu_reference)
        if route.source
        else None,
        "destination_pdus": [
            reference_name(item.pdu_reference) for item in route.destinations
        ],
        "evidence": route.evidence.as_dict(),
    }
