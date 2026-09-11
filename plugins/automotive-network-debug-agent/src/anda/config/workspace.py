"""
文件用途：
- 管理可复用的 Config Workspace，并协调 ARXML 与源码索引。
- 对外提供 Config MCP 所需的确定性查询，不承载 Agent 调查策略。

Workspace 是当前 MCP 进程内的只读快照。重复加载同一组路径直接复用；工程发生变化时，
调用方可显式 ``force_reload=True`` 建立新快照，避免每次查询都递归扫描整个工程。
"""

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Semaphore, Thread
from time import perf_counter
from uuid import uuid4

from anda.common.errors import (
    ConfigConflictError,
    ConfigInputError,
    ConfigNotFoundError,
    ConfigNotReadyError,
)
from anda.config.arxml import normalize_identifier, parse_arxml_files, reference_name
from anda.config.inspection import (
    inspect_communication,
    inspect_ipdu_group,
    trace_signal_gateway,
)
from anda.config.models import ArxmlIndex, ConfigObject
from anda.config.routing import trace_message_route
from anda.config.runtime_chain import trace_autosar_runtime_chain
from anda.config.source import SourceIndex

ARXML_EXTENSIONS = {".arxml", ".xml"}
SOURCE_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".inc"}
INVENTORY_EXTENSIONS = {".dbc", ".ldf", ".html", ".htm", ".blf"}
LOADABLE_EXTENSIONS = ARXML_EXTENSIONS | SOURCE_EXTENSIONS
SUPPORTED_EXTENSIONS = LOADABLE_EXTENSIONS | INVENTORY_EXTENSIONS
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
    "build",
    "debug",
    "release",
    "obj",
    "out",
    "thirdparty",
    "vendor-doc",
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


@dataclass(slots=True)
class ConfigLoadTask:
    """一项快速返回 workspace_id 的后台索引任务。"""

    workspace_id: str
    root_key: str
    root_path: str
    arxml_paths: list[str] | None
    force_reload: bool
    cache_directory: str | None
    status: str = "queued"
    stage: str = "queued"
    elapsed_seconds: float = 0.0
    total_file_count: int = 0
    processed_file_count: int = 0
    reindexed_file_count: int = 0
    reused_file_count: int = 0
    error: str | None = None
    done: Event = field(default_factory=Event)


class ConfigWorkspaceManager:
    """加载并查询 Config Workspace。"""

    def __init__(self) -> None:
        self._workspaces: dict[str, ConfigWorkspace] = {}
        self._root_to_id: dict[str, str] = {}
        self._load_lock = Lock()
        self._load_slot = Semaphore(1)
        self._task_lock = Lock()
        self._load_tasks: dict[str, ConfigLoadTask] = {}
        self._pending_keys: dict[str, str] = {}

    def start_load_config_workspace(
        self,
        root_path: str,
        arxml_paths: list[str] | None = None,
        force_reload: bool = False,
        cache_directory: str | None = None,
    ) -> dict:
        """快速返回 workspace_id，并在 daemon 线程中扫描与解析工程。"""
        scope = Path(root_path).expanduser().resolve()
        if not (
            scope.is_dir()
            or (scope.is_file() and scope.suffix.casefold() in LOADABLE_EXTENSIONS)
        ):
            raise ConfigInputError(f"工程路径不存在，或不是受支持的目录/文件：{scope}")
        explicit_arxml_files = _resolve_explicit_arxml_paths(arxml_paths)
        root_key = _workspace_cache_key(scope, explicit_arxml_files, cache_directory)

        with self._task_lock:
            pending_id = self._pending_keys.get(root_key)
            if pending_id is not None:
                return self._task_result(self._load_tasks[pending_id], reused=True)
        with self._load_lock:
            ready_id = self._root_to_id.get(root_key)
            if ready_id is not None and not force_reload:
                return self._load_result(
                    ready_id, self._workspaces[ready_id], reused=True
                )

        workspace_id = str(uuid4())
        task = ConfigLoadTask(
            workspace_id,
            root_key,
            str(scope),
            list(arxml_paths) if arxml_paths else None,
            force_reload,
            cache_directory,
        )
        with self._task_lock:
            existing_pending = self._pending_keys.get(root_key)
            if existing_pending is not None:
                return self._task_result(
                    self._load_tasks[existing_pending], reused=True
                )
            self._load_tasks[workspace_id] = task
            self._pending_keys[root_key] = workspace_id
        Thread(
            target=self._run_background_load,
            args=(task,),
            name=f"config-load-{workspace_id[:8]}",
            daemon=True,
        ).start()
        return self._task_result(task, reused=False)

    def get_load_status(self, workspace_id: str, wait_seconds: float = 0) -> dict:
        if not 0 <= wait_seconds <= 55:
            raise ConfigInputError("wait_seconds 必须在 0 到 55 之间")
        with self._task_lock:
            task = self._load_tasks.get(workspace_id)
        if task is None:
            with self._load_lock:
                workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                raise ConfigNotFoundError(f"未知 workspace_id：{workspace_id}")
            return self._load_result(workspace_id, workspace, reused=True)
        if wait_seconds:
            task.done.wait(wait_seconds)
        if task.status == "ready":
            with self._load_lock:
                workspace = self._workspaces[workspace_id]
            return self._load_result(workspace_id, workspace, reused=True)
        return self._task_result(task, reused=True)

    def _run_background_load(self, task: ConfigLoadTask) -> None:
        started = perf_counter()
        task.status = "loading"
        task.stage = "scanning_and_indexing_workspace"
        try:
            def progress(update: dict) -> None:
                self._update_task_progress(task, update)
                task.elapsed_seconds = perf_counter() - started

            result = self.load_config_workspace(
                task.root_path,
                task.arxml_paths,
                task.force_reload,
                task.cache_directory,
                progress_callback=progress,
            )
            built_id = result["workspace_id"]
            with self._load_lock:
                workspace = self._workspaces[built_id]
                self._workspaces[task.workspace_id] = workspace
                self._root_to_id[task.root_key] = task.workspace_id
            task.status = "ready"
            task.stage = "ready"
        # 后台边界必须捕获并序列化所有失败，否则调用方只会看到永久 loading。
        except Exception as exc:  # noqa: BLE001
            task.error = f"{type(exc).__name__}: {exc}"
            task.status = "failed"
            task.stage = "failed"
        finally:
            task.elapsed_seconds = perf_counter() - started
            with self._task_lock:
                self._pending_keys.pop(task.root_key, None)
            task.done.set()

    @staticmethod
    def _task_result(task: ConfigLoadTask, reused: bool) -> dict:
        progress = (
            min(100.0, round(task.processed_file_count * 100 / task.total_file_count, 2))
            if task.total_file_count
            else None
        )
        return {
            "workspace_id": task.workspace_id,
            "status": task.status,
            "index_ready": task.status == "ready",
            "stage": task.stage,
            "elapsed_seconds": round(task.elapsed_seconds, 3),
            "total_file_count": task.total_file_count,
            "processed_file_count": task.processed_file_count,
            "reindexed_file_count": task.reindexed_file_count,
            "reused_file_count": task.reused_file_count,
            "progress_percent": progress,
            "reused": reused,
            "error": task.error,
        }

    @staticmethod
    def _update_task_progress(task: ConfigLoadTask, update: dict) -> None:
        task.stage = update.get("stage", task.stage)
        task.total_file_count = update.get("total_file_count", task.total_file_count)
        task.processed_file_count = update.get(
            "processed_file_count", task.processed_file_count
        )
        task.reindexed_file_count = update.get(
            "reindexed_file_count", task.reindexed_file_count
        )
        task.reused_file_count = update.get(
            "reused_file_count", task.reused_file_count
        )

    def load_config_workspace(
        self,
        root_path: str,
        arxml_paths: list[str] | None = None,
        force_reload: bool = False,
        cache_directory: str | None = None,
        progress_callback=None,
    ) -> dict:
        scope = Path(root_path).expanduser().resolve()
        if scope.is_dir():
            source_root = scope
            initial_files: list[Path] | None = None
        elif scope.is_file() and scope.suffix.casefold() in LOADABLE_EXTENSIONS:
            source_root = scope.parent
            initial_files = [scope]
        else:
            raise ConfigInputError(f"工程路径不存在，或不是受支持的目录/文件：{scope}")
        explicit_arxml_files = _resolve_explicit_arxml_paths(arxml_paths)
        root_key = _workspace_cache_key(scope, explicit_arxml_files, cache_directory)

        with self._load_lock:
            existing_id = self._root_to_id.get(root_key)
            if existing_id is not None and not force_reload:
                return self._load_result(
                    existing_id, self._workspaces[existing_id], reused=True
                )

        # 扫描数千源码文件和解析大型 ARXML 不能占用状态锁；否则另一个 load Tool
        # 连“返回后台任务 ID”都会被当前索引阻塞。重型构建串行，状态查询保持可用。
        with self._load_slot:
            if progress_callback:
                progress_callback({"stage": "building_file_inventory"})
            files = initial_files or _scan_supported_files(scope)
            if arxml_paths:
                # 显式指定 ARXML 时，以用户选择为准，不混入工程目录中可能属于其他
                # 项目或历史版本的 ARXML；工程目录中的 C/H 仍用于生成代码定位。
                files = [
                    path
                    for path in files
                    if path.suffix.casefold() not in ARXML_EXTENSIONS
                ]
                files.extend(explicit_arxml_files)
                files = list(dict.fromkeys(files))
            loadable_files = [
                path for path in files if path.suffix.casefold() in LOADABLE_EXTENSIONS
            ]
            if not loadable_files:
                supported = ", ".join(sorted(LOADABLE_EXTENSIONS))
                raise ConfigInputError(
                    f"工程路径中没有支持的配置或源码文件：{scope}；支持：{supported}"
                )
            arxml_files = [
                path for path in files if path.suffix.casefold() in ARXML_EXTENSIONS
            ]
            source_files = [
                path for path in files if path.suffix.casefold() in SOURCE_EXTENSIONS
            ]
            if progress_callback:
                progress_callback(
                    {
                        "stage": "parsing_arxml",
                        "total_file_count": len(files),
                        "processed_file_count": 0,
                    }
                )
            arxml_index = parse_arxml_files(arxml_files)
            source_index = SourceIndex(
                source_root,
                source_files,
                inventory_files=[
                    path
                    for path in files
                    if path.suffix.casefold() not in ARXML_EXTENSIONS
                ],
                cache_dir=cache_directory,
                scope_identity=str(scope),
                progress_callback=progress_callback,
            )
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
        with self._load_lock:
            current_id = self._root_to_id.get(root_key)
            if current_id is not None and not force_reload:
                return self._load_result(
                    current_id, self._workspaces[current_id], reused=True
                )
            self._workspaces[workspace_id] = workspace
            self._root_to_id[root_key] = workspace_id
            if current_id is not None:
                self._workspaces.pop(current_id, None)
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
            "status": "ready",
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
            "project_index": workspace.source.cache_status(),
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
        self,
        workspace_id: str,
        query: str,
        limit: int = 20,
        modules: list[str] | None = None,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.search_symbols(query, limit, modules)
        result["workspace_id"] = workspace_id
        return result

    def search_source_evidence(
        self,
        workspace_id: str,
        identifiers: list[str],
        modules: list[str] | None = None,
        include_generated: bool = True,
        include_source: bool = True,
        limit_per_identifier: int = 20,
        max_chars_per_match: int = 1000,
    ) -> dict:
        """批量返回结构化源码证据；未命中结果同时携带明确搜索范围。"""
        workspace = self._get(workspace_id)
        result = workspace.source.search_occurrences(
            identifiers,
            modules=modules,
            include_generated=include_generated,
            include_source=include_source,
            limit_per_identifier=limit_per_identifier,
            max_chars_per_match=max_chars_per_match,
        )
        for identifier_result in result["results"]:
            config_matches = [
                item
                for item in _search_arxml(
                    workspace.arxml, identifier_result["identifier"]
                )
                if item["matched_by"] in {"name_exact", "reference_exact"}
            ]
            origins = [
                {
                    "object_name": item["name"],
                    "object_type": item.get("object_type"),
                    "reference_path": item.get("reference_path"),
                    "evidence": item["evidence"],
                }
                for item in config_matches[:20]
            ]
            for match in identifier_result["matches"]:
                if not match["generated_source"]:
                    continue
                match["generated_from_arxml"] = origins or None
                match["generation_origin_status"] = (
                    "EXACT_IDENTIFIER_MATCH" if origins else "UNKNOWN"
                )
        result["workspace_id"] = workspace_id
        return result

    def plan_source_search(
        self, workspace_id: str, question_type: str, identifiers: list[str]
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.plan_search(question_type, identifiers)
        result["workspace_id"] = workspace_id
        return result

    def read_source_lines(
        self,
        workspace_id: str,
        path: str,
        ranges: list[list[int]],
        max_chars_per_line: int = 1000,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.read_lines(path, ranges, max_chars_per_line)
        result["workspace_id"] = workspace_id
        return result

    def get_project_index_status(self, workspace_id: str) -> dict:
        workspace = self._get(workspace_id)
        return {
            "workspace_id": workspace_id,
            **workspace.source.cache_status(),
            "source_symbol_count": workspace.source.symbol_count,
            "source_occurrence_count": workspace.source.occurrence_count,
        }

    def inspect_source_symbol(
        self,
        workspace_id: str,
        symbol: str,
        limit: int = 20,
        context_lines: int = 0,
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

    def trace_autosar_runtime_chain(
        self,
        workspace_id: str,
        signal_name: str,
        additional_identifiers: list[str] | None = None,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = trace_autosar_runtime_chain(
            workspace.arxml,
            workspace.source,
            signal_name,
            additional_identifiers,
        )
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
        context_lines: int = 0,
    ) -> dict:
        workspace = self._get(workspace_id)
        result = workspace.source.contexts(symbol.strip(), limit, context_lines)
        result["workspace_id"] = workspace_id
        return result

    def _get(self, workspace_id: str) -> ConfigWorkspace:
        workspace = self._workspaces.get(workspace_id)
        if workspace is not None:
            return workspace
        task = self._load_tasks.get(workspace_id)
        if task is not None:
            if task.status == "failed":
                raise ConfigInputError(f"Config 索引失败：{task.error}")
            raise ConfigNotReadyError(
                f"Config 索引尚未完成（{task.stage}）；请调用 "
                "get_config_load_status 并复用同一 workspace_id，不要重复加载"
            )
        raise ConfigNotFoundError(f"未知 workspace_id：{workspace_id}")


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


def _workspace_cache_key(
    scope: Path, arxml_files: list[Path], cache_directory: str | None = None
) -> str:
    # 显式 ARXML 的输入顺序不改变 Workspace 内容，不应因此重复建立索引。
    parts = [
        _path_identity(scope),
        *(sorted(_path_identity(path) for path in arxml_files)),
        _path_identity(Path(cache_directory).expanduser().resolve())
        if cache_directory
        else "<default-cache>",
    ]
    return "\0".join(parts)


def _path_identity(path: Path) -> str:
    value = str(path)
    return value.casefold() if os.name == "nt" else value


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
