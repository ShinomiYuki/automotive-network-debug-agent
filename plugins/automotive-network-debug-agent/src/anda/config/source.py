"""
文件用途：
- 为汽车工程建立持久化、可增量失效的 SQLite 文件与源码标识符索引。
- 通过 byte offset 按需读取少量证据行，避免查询大型生成文件时再次整文件加载。
- 提供批量标识符搜索、可审计负向结果和问题驱动的候选文件规划。

索引只保存源码标识符、文件元数据、行偏移和有限分类信息，不复制完整源码正文。
缓存由绝对路径、文件大小和修改时间失效；原工程文件始终只读。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from anda.common.cache import default_cache_root
from anda.common.errors import ConfigInputError

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_TABLE_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^]]*\])?\s*=\s*(?:\{|$)"
)
_FIELD_RE = re.compile(
    r"/\*\s*([A-Za-z_][A-Za-z0-9_]*)\s*\*/\s*([^,}{]+)"
)
_REFERABLE_RE = re.compile(
    r"referable[-_ ]?key|referable key|short[-_ ]?name.*(?:path|map)",
    re.IGNORECASE,
)
_SCHEMA_VERSION = 2
_MAX_CONTEXT_LINE_LENGTH = 1000
_MAX_BATCH_IDENTIFIERS = 50
_MAX_RANGES = 100
MAX_SOURCE_RESULTS = 50
MAX_CONTEXT_LINES = 5

SOURCE_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".inc"}

ProgressCallback = Callable[[dict], None]


@dataclass(frozen=True, slots=True)
class SourceOccurrence:
    """源码中某个标识符的可定位证据；不在内存中长期保存正文。"""

    symbol: str
    file: Path
    relative_file: str
    line: int
    column: int
    byte_offset: int
    byte_length: int
    file_kind: str
    module: str
    role: str
    table: str | None

    def location_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "file": str(self.file),
            "relative_file": self.relative_file,
            "line": self.line,
            "column": self.column,
            "byte_offset": self.byte_offset,
            "byte_length": self.byte_length,
            "file_kind": self.file_kind,
            "module": self.module,
            "role": self.role,
            "table": self.table,
        }


class SourceIndex:
    """SQLite 支持的只读查询索引；构建阶段按文件身份增量更新。"""

    def __init__(
        self,
        root: Path,
        files: list[Path],
        *,
        inventory_files: list[Path] | None = None,
        cache_dir: str | Path | None = None,
        scope_identity: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.root = root.resolve()
        self.files = tuple(path.resolve() for path in files)
        self.inventory_files = tuple(
            path.resolve()
            for path in (inventory_files if inventory_files is not None else files)
        )
        cache_root = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else default_cache_root()
        )
        cache_root.mkdir(parents=True, exist_ok=True)
        identity = _path_key(scope_identity or str(self.root)).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:24]
        cache_directory = cache_root / "config"
        cache_directory.mkdir(parents=True, exist_ok=True)
        self._mutable_cache_path = cache_directory / f"{digest}.sqlite3"
        self._inventory_state = tuple(self._read_inventory_state())
        generation_payload = "\0".join(
            f"{_path_key(path)}\0{size}\0{mtime_ns}"
            for path, size, mtime_ns in self._inventory_state
        ).encode("utf-8")
        generation = hashlib.sha256(generation_payload).hexdigest()[:24]
        self._generation_cache_path = cache_directory / f"{digest}-{generation}.sqlite3"
        self.cache_path = self._mutable_cache_path
        self.reindexed_file_count = 0
        self.reused_file_count = 0
        self.removed_file_count = 0
        self.occurrence_count = 0
        self._build(progress_callback)

    def _read_inventory_state(self) -> list[tuple[Path, int, int]]:
        result = []
        for path in self.inventory_files:
            try:
                stat = path.stat()
            except OSError as error:
                raise ConfigInputError(f"无法读取工程文件状态：{path}：{error}") from error
            result.append((path, stat.st_size, stat.st_mtime_ns))
        return result

    @property
    def symbol_count(self) -> int:
        with self._connect() as database:
            row = database.execute(
                "SELECT COUNT(DISTINCT identifier_norm) FROM occurrences"
            ).fetchone()
        return int(row[0])

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.cache_path, timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        return database

    def _build(self, progress_callback: ProgressCallback | None) -> None:
        with self._connect() as database:
            self._initialize_schema(database)
            database.commit()
            # 同一项目缓存可被多个 MCP 进程共享。建索引前取得写锁，
            # 使后到进程在等待后重新读取最新文件快照。
            database.execute("BEGIN IMMEDIATE")
            existing = {
                row["absolute_path_key"]: row
                for row in database.execute(
                    "SELECT id, absolute_path, absolute_path_key, size, mtime_ns "
                    "FROM files"
                )
            }
            current_paths = {_path_key(path) for path in self.inventory_files}
            stale = [row for key, row in existing.items() if key not in current_paths]
            for row in stale:
                database.execute("DELETE FROM files WHERE id = ?", (row["id"],))
            self.removed_file_count = len(stale)

            total = len(self.inventory_files)
            for position, (path, size, mtime_ns) in enumerate(
                self._inventory_state, start=1
            ):
                key = _path_key(path)
                old = existing.get(key)
                unchanged = bool(
                    old
                    and int(old["size"]) == size
                    and int(old["mtime_ns"]) == mtime_ns
                )
                if unchanged:
                    self.reused_file_count += 1
                else:
                    self._index_file(database, path, size, mtime_ns, old)
                    self.reindexed_file_count += 1
                if progress_callback and (position == total or position % 50 == 0):
                    progress_callback(
                        {
                            "stage": "indexing_project_files",
                            "total_file_count": total,
                            "processed_file_count": position,
                            "reindexed_file_count": self.reindexed_file_count,
                            "reused_file_count": self.reused_file_count,
                        }
                    )
            self.occurrence_count = int(
                database.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            )
            if not self._valid_generation_snapshot():
                self._create_generation_snapshot(database)
            database.commit()
        self.cache_path = self._generation_cache_path

    @staticmethod
    def _initialize_schema(database: sqlite3.Connection) -> None:
        current_version = int(database.execute("PRAGMA user_version").fetchone()[0])
        if current_version not in {0, _SCHEMA_VERSION}:
            database.executescript(
                "DROP TABLE IF EXISTS occurrences; DROP TABLE IF EXISTS line_offsets; "
                "DROP TABLE IF EXISTS files;"
            )
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                absolute_path TEXT NOT NULL,
                absolute_path_key TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL,
                extension TEXT NOT NULL,
                module TEXT NOT NULL,
                file_kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                encoding TEXT,
                line_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS line_offsets (
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                line INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                PRIMARY KEY (file_id, line)
            );
            CREATE TABLE IF NOT EXISTS occurrences (
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                identifier_norm TEXT NOT NULL,
                identifier TEXT NOT NULL,
                line INTEGER NOT NULL,
                column_number INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                context_type TEXT NOT NULL,
                table_name TEXT,
                PRIMARY KEY (file_id, identifier_norm, line)
            );
            CREATE INDEX IF NOT EXISTS idx_occurrence_identifier
                ON occurrences(identifier_norm);
            CREATE INDEX IF NOT EXISTS idx_occurrence_file_line
                ON occurrences(file_id, line);
            CREATE INDEX IF NOT EXISTS idx_files_module ON files(module);
            """
        )
        database.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _valid_generation_snapshot(self) -> bool:
        if not self._generation_cache_path.is_file():
            return False
        try:
            with sqlite3.connect(self._generation_cache_path) as snapshot:
                version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
                snapshot.execute("SELECT 1 FROM files LIMIT 1").fetchone()
            if version == _SCHEMA_VERSION:
                return True
        except sqlite3.Error:
            pass
        try:
            self._generation_cache_path.unlink(missing_ok=True)
        except OSError as error:
            raise ConfigInputError(
                f"无法替换损坏的项目索引快照：{self._generation_cache_path}：{error}"
            ) from error
        return False

    def _create_generation_snapshot(self, database: sqlite3.Connection) -> None:
        """在基础索引的同一写事务中创建不可变 generation。"""
        database.execute(
            "ATTACH DATABASE ? AS generation", (str(self._generation_cache_path),)
        )
        try:
            database.execute(
                """CREATE TABLE generation.files (
                    id INTEGER PRIMARY KEY,
                    absolute_path TEXT NOT NULL,
                    absolute_path_key TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    module TEXT NOT NULL,
                    file_kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    encoding TEXT,
                    line_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            database.execute(
                """CREATE TABLE generation.line_offsets (
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    line INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL,
                    PRIMARY KEY (file_id, line)
                )"""
            )
            database.execute(
                """CREATE TABLE generation.occurrences (
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    identifier_norm TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    column_number INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL,
                    context_type TEXT NOT NULL,
                    table_name TEXT,
                    PRIMARY KEY (file_id, identifier_norm, line)
                )"""
            )
            database.execute("INSERT INTO generation.files SELECT * FROM main.files")
            database.execute(
                "INSERT INTO generation.line_offsets SELECT * FROM main.line_offsets"
            )
            database.execute(
                "INSERT INTO generation.occurrences SELECT * FROM main.occurrences"
            )
            database.execute(
                "CREATE INDEX generation.idx_occurrence_identifier "
                "ON occurrences(identifier_norm)"
            )
            database.execute(
                "CREATE INDEX generation.idx_occurrence_file_line "
                "ON occurrences(file_id, line)"
            )
            database.execute(
                "CREATE INDEX generation.idx_files_module ON files(module)"
            )
            database.execute(f"PRAGMA generation.user_version = {_SCHEMA_VERSION}")
            database.commit()
        finally:
            database.execute("DETACH DATABASE generation")

    def _index_file(
        self,
        database: sqlite3.Connection,
        path: Path,
        size: int,
        mtime_ns: int,
        old: sqlite3.Row | None,
    ) -> None:
        relative = _relative_path(path, self.root)
        extension = path.suffix.casefold()
        file_kind = _file_kind(path, self.root)
        module = _module_name(path, self.root)
        path_key = _path_key(path)
        if old:
            file_id = int(old["id"])
            database.execute("DELETE FROM occurrences WHERE file_id = ?", (file_id,))
            database.execute("DELETE FROM line_offsets WHERE file_id = ?", (file_id,))
            database.execute(
                """UPDATE files SET absolute_path=?, absolute_path_key=?, relative_path=?,
                   extension=?, module=?, file_kind=?, size=?, mtime_ns=?, encoding=NULL,
                   line_count=0 WHERE id=?""",
                (
                    str(path),
                    path_key,
                    relative,
                    extension,
                    module,
                    file_kind,
                    size,
                    mtime_ns,
                    file_id,
                ),
            )
        else:
            cursor = database.execute(
                """INSERT INTO files
                   (absolute_path, absolute_path_key, relative_path, extension, module,
                    file_kind, size, mtime_ns)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(path),
                    path_key,
                    relative,
                    extension,
                    module,
                    file_kind,
                    size,
                    mtime_ns,
                ),
            )
            file_id = int(cursor.lastrowid)

        if extension not in SOURCE_EXTENSIONS:
            return
        payload = _read_source_bytes(path)
        if b"\x00" in payload:
            return
        encoding = _detect_encoding(payload)
        offset = 0
        current_table: str | None = None
        line_count = 0
        for line_count, raw_line in enumerate(payload.splitlines(keepends=True), start=1):
            byte_length = len(raw_line)
            database.execute(
                "INSERT INTO line_offsets(file_id, line, byte_offset, byte_length) VALUES (?, ?, ?, ?)",
                (file_id, line_count, offset, byte_length),
            )
            line = _decode_line(raw_line, encoding)
            table_match = _TABLE_RE.search(line)
            if table_match:
                current_table = table_match.group(1)
            if not _is_referable_summary(line):
                first_columns: dict[str, tuple[str, int]] = {}
                for match in _IDENTIFIER_RE.finditer(line):
                    first_columns.setdefault(
                        match.group(0).casefold(), (match.group(0), match.start() + 1)
                    )
                for normalized, (symbol, column) in first_columns.items():
                    database.execute(
                        """INSERT INTO occurrences
                           (file_id, identifier_norm, identifier, line, column_number,
                            byte_offset, byte_length, context_type, table_name)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            file_id,
                            normalized,
                            symbol,
                            line_count,
                            column,
                            offset,
                            byte_length,
                            _classify_line_role(symbol, line),
                            current_table,
                        ),
                    )
            if current_table and "};" in line:
                current_table = None
            offset += byte_length
        database.execute(
            "UPDATE files SET encoding=?, line_count=? WHERE id=?",
            (encoding, line_count, file_id),
        )

    def occurrences(self, symbol: str) -> list[SourceOccurrence]:
        """按完整标识符查询位置；不做容易产生误关联的子串猜测。"""
        normalized = symbol.strip().casefold()
        if not normalized:
            return []
        with self._connect() as database:
            rows = database.execute(
                _OCCURRENCE_SELECT
                + " WHERE o.identifier_norm=? ORDER BY f.absolute_path, o.line",
                (normalized,),
            ).fetchall()
        return [_occurrence_from_row(row) for row in rows]

    def search_symbols(
        self,
        query: str,
        limit: int = 20,
        modules: list[str] | None = None,
    ) -> dict:
        """搜索源码标识符名称；查询 SQLite，不再重新读取工程文件。"""
        normalized = query.strip().casefold()
        if not normalized:
            raise ConfigInputError("query 不能为空")
        if limit < 1:
            raise ConfigInputError("limit 必须大于等于 1")
        applied_limit = min(limit, MAX_SOURCE_RESULTS)
        module_values = _normalize_modules(modules)
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        where = ["o.identifier_norm LIKE ? ESCAPE '\\'"]
        parameters: list[object] = [f"%{escaped}%"]
        if module_values:
            where.append(f"f.module IN ({','.join('?' for _ in module_values)})")
            parameters.extend(module_values)
        with self._connect() as database:
            total_count = int(
                database.execute(
                    "SELECT COUNT(DISTINCT o.identifier_norm) "
                    "FROM occurrences o JOIN files f ON f.id=o.file_id WHERE "
                    + " AND ".join(where),
                    parameters,
                ).fetchone()[0]
            )
            candidate_rows = database.execute(
                "SELECT o.identifier_norm, MIN(o.identifier) AS identifier, "
                "COUNT(*) AS occurrence_count "
                "FROM occurrences o JOIN files f ON f.id=o.file_id WHERE "
                + " AND ".join(where)
                + " GROUP BY o.identifier_norm "
                + "ORDER BY CASE WHEN o.identifier_norm=? THEN 0 "
                + "WHEN o.identifier_norm LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, "
                + "o.identifier_norm LIMIT ?",
                [*parameters, normalized, f"{escaped}%", applied_limit],
            ).fetchall()
            selected = [row["identifier_norm"] for row in candidate_rows]
            role_counts_by_identifier: dict[str, dict[str, int]] = defaultdict(dict)
            sample_rows_by_identifier: dict[str, list[sqlite3.Row]] = defaultdict(list)
            if selected:
                selected_placeholders = ",".join("?" for _ in selected)
                selected_where = [
                    f"o.identifier_norm IN ({selected_placeholders})"
                ]
                selected_parameters: list[object] = list(selected)
                if module_values:
                    selected_where.append(
                        f"f.module IN ({','.join('?' for _ in module_values)})"
                    )
                    selected_parameters.extend(module_values)
                role_rows = database.execute(
                    "SELECT o.identifier_norm, o.context_type, COUNT(*) AS count "
                    "FROM occurrences o JOIN files f ON f.id=o.file_id WHERE "
                    + " AND ".join(selected_where)
                    + " GROUP BY o.identifier_norm, o.context_type",
                    selected_parameters,
                ).fetchall()
                for row in role_rows:
                    role_counts_by_identifier[row["identifier_norm"]][
                        row["context_type"]
                    ] = int(row["count"])
                sample_rows = database.execute(
                    "SELECT * FROM (SELECT "
                    + _OCCURRENCE_COLUMNS
                    + ", ROW_NUMBER() OVER (PARTITION BY o.identifier_norm "
                    "ORDER BY f.absolute_path, o.line) AS sample_rank "
                    "FROM occurrences o JOIN files f ON f.id=o.file_id WHERE "
                    + " AND ".join(selected_where)
                    + ") WHERE sample_rank <= 3 ORDER BY identifier_norm, sample_rank",
                    selected_parameters,
                ).fetchall()
                for row in sample_rows:
                    sample_rows_by_identifier[row["identifier_norm"]].append(row)
        candidates = []
        for row in candidate_rows:
            key = row["identifier_norm"]
            candidates.append(
                {
                    "symbol": row["identifier"],
                    "matched_by": (
                        "exact"
                        if key == normalized
                        else "prefix"
                        if key.startswith(normalized)
                        else "substring"
                    ),
                    "occurrence_count": int(row["occurrence_count"]),
                    "role_counts": dict(
                        sorted(role_counts_by_identifier.get(key, {}).items())
                    ),
                    "sample_locations": [
                        _occurrence_from_row(item).location_dict()
                        for item in sample_rows_by_identifier.get(key, [])
                    ],
                }
            )
        return {
            "query": query.strip(),
            "modules": module_values or None,
            "total_count": total_count,
            "returned_count": len(candidates),
            "limit_applied": applied_limit,
            "matches": candidates[:applied_limit],
        }

    def search_occurrences(
        self,
        identifiers: list[str],
        *,
        modules: list[str] | None = None,
        include_generated: bool = True,
        include_source: bool = True,
        limit_per_identifier: int = 20,
        max_chars_per_match: int = 1000,
    ) -> dict:
        """一次查询多个完整标识符，返回有界片段和可审计未命中范围。"""
        normalized_pairs = _validate_identifiers(identifiers)
        if not include_generated and not include_source:
            raise ConfigInputError("include_generated 与 include_source 不能同时为 false")
        if limit_per_identifier < 1:
            raise ConfigInputError("limit_per_identifier 必须大于等于 1")
        if not 100 <= max_chars_per_match <= 2000:
            raise ConfigInputError("max_chars_per_match 必须在 100 到 2000 之间")
        applied_limit = min(limit_per_identifier, MAX_SOURCE_RESULTS)
        module_values = _normalize_modules(modules)
        placeholders = ",".join("?" for _ in normalized_pairs)
        where = [f"o.identifier_norm IN ({placeholders})"]
        parameters: list[object] = [item[0] for item in normalized_pairs]
        if module_values:
            where.append(f"f.module IN ({','.join('?' for _ in module_values)})")
            parameters.extend(module_values)
        kinds = []
        if include_generated:
            kinds.append("generated_code")
        if include_source:
            kinds.append("source_code")
        where.append(f"f.file_kind IN ({','.join('?' for _ in kinds)})")
        parameters.extend(kinds)
        with self._connect() as database:
            rows = database.execute(
                _OCCURRENCE_SELECT
                + " WHERE "
                + " AND ".join(where)
                + " ORDER BY o.identifier_norm, f.absolute_path, o.line",
                parameters,
            ).fetchall()
            scope_where = [
                "extension IN (" + ",".join("?" for _ in SOURCE_EXTENSIONS) + ")",
                "file_kind IN (" + ",".join("?" for _ in kinds) + ")",
            ]
            scope_parameters: list[object] = [*sorted(SOURCE_EXTENSIONS), *kinds]
            if module_values:
                scope_where.append(
                    f"module IN ({','.join('?' for _ in module_values)})"
                )
                scope_parameters.extend(module_values)
            scope_file_count = int(
                database.execute(
                    "SELECT COUNT(*) FROM files WHERE " + " AND ".join(scope_where),
                    scope_parameters,
                ).fetchone()[0]
            )

        grouped_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped_rows[row["identifier_norm"]].append(row)
        results = []
        not_found = []
        for normalized, original in normalized_pairs:
            identifier_rows = grouped_rows.get(normalized, [])
            if not identifier_rows:
                not_found.append(original)
            matches = []
            for row in identifier_rows[:applied_limit]:
                occurrence = _occurrence_from_row(row)
                text = self._read_indexed_line(occurrence.file, occurrence.line)
                snippet = _bounded_window(
                    text, occurrence.symbol, occurrence.column, max_chars_per_match
                )
                item = occurrence.location_dict()
                item.update(
                    {
                        "snippet": snippet,
                        "snippet_truncated": len(text) > len(snippet),
                        "fields": _extract_fields(snippet),
                        "generated_source": occurrence.file_kind == "generated_code",
                        "generated_from_arxml": None,
                        "generation_origin_status": "UNKNOWN",
                    }
                )
                matches.append(item)
            results.append(
                {
                    "identifier": original,
                    "total_count": len(identifier_rows),
                    "returned_count": len(matches),
                    "matches": matches,
                    "truncated": len(identifier_rows) > len(matches),
                }
            )
        return {
            "identifiers": [item[1] for item in normalized_pairs],
            "modules": module_values or None,
            "include_generated": include_generated,
            "include_source": include_source,
            "limit_per_identifier": applied_limit,
            "search_scope": {
                "root": str(self.root),
                "indexed_source_file_count": scope_file_count,
                "excluded_directories": [
                    ".git",
                    "Build",
                    "Debug",
                    "Release",
                    "obj",
                    "out",
                    "ThirdParty",
                    "BSW/vendor-doc",
                ],
                "matching_mode": "exact_identifier_from_sqlite_index",
            },
            "results": results,
            "not_found": not_found,
            "negative_result_is_scope_limited": bool(not_found),
        }

    def contexts(self, symbol: str, limit: int, context_lines: int) -> dict:
        """通过行偏移读取有限上下文，不把整个文件构造成字符串数组。"""
        _validate_context_request(symbol, limit, context_lines)
        applied_limit = min(limit, MAX_SOURCE_RESULTS)
        applied_context = min(context_lines, MAX_CONTEXT_LINES)
        occurrences = self.occurrences(symbol)
        matches = []
        for occurrence in occurrences[:applied_limit]:
            start = max(1, occurrence.line - applied_context)
            end = occurrence.line + applied_context
            context = self._read_range(occurrence.file, start, end, _MAX_CONTEXT_LINE_LENGTH)
            item = occurrence.location_dict()
            item["context"] = context
            matches.append(item)
        return {
            "query": symbol,
            "total_count": len(occurrences),
            "returned_count": len(matches),
            "limit_applied": applied_limit,
            "context_lines_applied": applied_context,
            "role_counts": _role_counts(occurrences),
            "matches": matches,
        }

    def inspect_symbol(self, symbol: str, limit: int, context_lines: int) -> dict:
        result = self.contexts(symbol, limit, context_lines)
        result["inspection_basis"] = "exact_c_identifier"
        result["semantic_inference_performed"] = False
        return result

    def locations_for_symbols(self, symbols: list[str], limit: int = 20) -> list[dict]:
        result: list[dict] = []
        seen: set[tuple[str, int, str]] = set()
        for symbol in symbols:
            for occurrence in self.occurrences(symbol):
                key = (str(occurrence.file).casefold(), occurrence.line, symbol.casefold())
                if key in seen:
                    continue
                seen.add(key)
                result.append(occurrence.location_dict())
                if len(result) >= limit:
                    return result
        return result

    def read_lines(
        self,
        path: str,
        ranges: list[list[int]],
        max_chars_per_line: int = 1000,
    ) -> dict:
        """一次读取同一已索引文件的多个明确行范围。"""
        if not ranges or len(ranges) > _MAX_RANGES:
            raise ConfigInputError(f"ranges 必须包含 1 到 {_MAX_RANGES} 个范围")
        if not 100 <= max_chars_per_line <= 5000:
            raise ConfigInputError("max_chars_per_line 必须在 100 到 5000 之间")
        target = Path(path).expanduser().resolve()
        with self._connect() as database:
            file_row = database.execute(
                "SELECT absolute_path FROM files WHERE absolute_path_key=?",
                (_path_key(target),),
            ).fetchone()
        if file_row is None:
            raise ConfigInputError(f"文件不在当前项目索引中：{target}")
        requested_lines: set[int] = set()
        normalized_ranges = []
        for value in ranges:
            if len(value) != 2:
                raise ConfigInputError("每个 range 必须是 [start_line, end_line]")
            start, end = value
            if start < 1 or end < start:
                raise ConfigInputError("行范围必须满足 1 <= start_line <= end_line")
            if end - start > 200:
                raise ConfigInputError("单个行范围最多读取 201 行")
            normalized_ranges.append([start, end])
            requested_lines.update(range(start, end + 1))
        lines = self._read_specific_lines(target, sorted(requested_lines), max_chars_per_line)
        return {
            "file": str(target),
            "ranges": normalized_ranges,
            "max_chars_per_line": max_chars_per_line,
            "returned_line_count": len(lines),
            "lines": lines,
        }

    def plan_search(self, question_type: str, identifiers: list[str]) -> dict:
        """按问题类型先筛模块和文件名，不执行内容搜索。"""
        normalized_type = question_type.strip().casefold()
        if not normalized_type:
            raise ConfigInputError("question_type 不能为空")
        _validate_identifiers(identifiers)
        module_plan = _QUESTION_MODULES.get(normalized_type)
        if module_plan is None:
            raise ConfigInputError(
                "question_type 仅支持 can_to_lin_timeout、message_route、signal_gateway、"
                "ipdu_group、runtime_control"
            )
        staged_modules = [list(stage) for stage in module_plan]
        all_modules = list(dict.fromkeys(module for stage in staged_modules for module in stage))
        with self._connect() as database:
            rows = database.execute(
                "SELECT absolute_path, relative_path, extension, module, file_kind, size, mtime_ns "
                f"FROM files WHERE module IN ({','.join('?' for _ in all_modules)}) "
                "ORDER BY module, relative_path",
                all_modules,
            ).fetchall()
        files_by_module: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            files_by_module[row["module"]].append(dict(row))
        stages = []
        for index, modules in enumerate(staged_modules, start=1):
            candidate_files = [
                file for module in modules for file in files_by_module.get(module, [])
            ]
            stages.append(
                {
                    "stage": index,
                    "modules": modules,
                    "candidate_file_count": len(candidate_files),
                    "candidate_files": candidate_files[:100],
                    "candidate_files_truncated": len(candidate_files) > 100,
                }
            )
        return {
            "question_type": normalized_type,
            "identifiers": identifiers,
            "strategy": "file_inventory_then_module_filter_then_batched_identifier_query",
            "stages": stages,
        }

    def cache_status(self) -> dict:
        return {
            "backend": "sqlite",
            "cache_file": str(self.cache_path),
            "mutable_base_cache_file": str(self._mutable_cache_path),
            "workspace_generation_pinned": True,
            "cache_file_size_bytes": self.cache_path.stat().st_size,
            "reindexed_file_count": self.reindexed_file_count,
            "reused_file_count": self.reused_file_count,
            "removed_file_count": self.removed_file_count,
            "invalidation_key": ["absolute_path", "file_size", "last_modified_time_ns"],
        }

    def _read_indexed_line(self, path: Path, line: int) -> str:
        lines = self._read_specific_lines(path, [line], 2_000_000)
        return lines[0]["text"] if lines else ""

    def _read_range(
        self, path: Path, start: int, end: int, max_chars: int
    ) -> list[dict]:
        return self._read_specific_lines(path, list(range(start, end + 1)), max_chars)

    def _read_specific_lines(
        self, path: Path, line_numbers: list[int], max_chars: int
    ) -> list[dict]:
        if not line_numbers:
            return []
        placeholders = ",".join("?" for _ in line_numbers)
        with self._connect() as database:
            rows = database.execute(
                """SELECT l.line, l.byte_offset, l.byte_length, f.encoding
                   FROM line_offsets l JOIN files f ON f.id=l.file_id
                   WHERE f.absolute_path_key=? AND l.line IN ("""
                + placeholders
                + ") ORDER BY l.line",
                [_path_key(path), *line_numbers],
            ).fetchall()
        result = []
        try:
            with path.open("rb") as source:
                for row in rows:
                    source.seek(int(row["byte_offset"]))
                    raw = source.read(int(row["byte_length"]))
                    text = _decode_line(raw, row["encoding"] or "utf-8-sig")
                    result.append(
                        {
                            "line": int(row["line"]),
                            "text": text[:max_chars],
                            "original_char_count": len(text),
                            "truncated": len(text) > max_chars,
                        }
                    )
        except OSError as error:
            raise ConfigInputError(f"无法读取源码文件：{path}：{error}") from error
        return result


_OCCURRENCE_COLUMNS = """o.identifier_norm, o.identifier, o.line,
       o.column_number, o.byte_offset,
       o.byte_length, o.context_type, o.table_name, f.absolute_path,
       f.relative_path, f.file_kind, f.module"""

_OCCURRENCE_SELECT = (
    "SELECT "
    + _OCCURRENCE_COLUMNS
    + " FROM occurrences o JOIN files f ON f.id=o.file_id"
)

_QUESTION_MODULES = {
    "can_to_lin_timeout": (
        ("CanIf", "Com", "PduR", "LinIf", "LDF"),
        ("OS/RTE",),
        ("BswM", "ComM", "CDD/Application"),
    ),
    "message_route": (("CanIf", "PduR", "Com"),),
    "signal_gateway": (("Com", "PduR"), ("OS/RTE",)),
    "ipdu_group": (("Com", "BswM", "ComM"),),
    "runtime_control": (("BswM", "ComM", "OS/RTE", "CDD/Application"),),
}


def _occurrence_from_row(row: sqlite3.Row) -> SourceOccurrence:
    return SourceOccurrence(
        symbol=row["identifier"],
        file=Path(row["absolute_path"]),
        relative_file=row["relative_path"],
        line=int(row["line"]),
        column=int(row["column_number"]),
        byte_offset=int(row["byte_offset"]),
        byte_length=int(row["byte_length"]),
        file_kind=row["file_kind"],
        module=row["module"],
        role=row["context_type"],
        table=row["table_name"],
    )


def _validate_identifiers(identifiers: list[str]) -> list[tuple[str, str]]:
    if not identifiers or len(identifiers) > _MAX_BATCH_IDENTIFIERS:
        raise ConfigInputError(
            f"identifiers 必须包含 1 到 {_MAX_BATCH_IDENTIFIERS} 个完整标识符"
        )
    result = []
    seen = set()
    for value in identifiers:
        symbol = value.strip()
        if not _IDENTIFIER_RE.fullmatch(symbol):
            raise ConfigInputError(f"不是完整 C/C++ 标识符：{value}")
        normalized = symbol.casefold()
        if normalized not in seen:
            result.append((normalized, symbol))
            seen.add(normalized)
    return result


def _normalize_modules(modules: list[str] | None) -> list[str]:
    if not modules:
        return []
    known = {
        module for stages in _QUESTION_MODULES.values() for stage in stages for module in stage
    }
    aliases = {value.casefold(): value for value in known | {"Other"}}
    result = []
    for module in modules:
        normalized = aliases.get(module.strip().casefold())
        if normalized is None:
            raise ConfigInputError(f"未知模块筛选：{module}")
        if normalized not in result:
            result.append(normalized)
    return result


def _validate_context_request(symbol: str, limit: int, context_lines: int) -> None:
    if not symbol.strip():
        raise ConfigInputError("symbol 不能为空")
    if not _IDENTIFIER_RE.fullmatch(symbol.strip()):
        raise ConfigInputError("symbol 必须是完整的 C/C++ 标识符")
    if limit < 1:
        raise ConfigInputError("limit 必须大于等于 1")
    if context_lines < 0:
        raise ConfigInputError("context_lines 必须大于等于 0")


def _read_source_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ConfigInputError(f"无法读取源码文件：{path}：{error}") from error


def _detect_encoding(payload: bytes) -> str:
    try:
        payload.decode("utf-8-sig")
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "gb18030"


def _decode_line(raw: bytes, encoding: str) -> str:
    return raw.decode(encoding, errors="replace").rstrip("\r\n")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _path_key(value: str | Path) -> str:
    """按主机文件系统语义建立路径键；Windows 不区分大小写。"""
    text = str(value)
    return text.casefold() if os.name == "nt" else text


def _file_kind(path: Path, root: Path) -> str:
    generated_markers = {
        "generated",
        "gen",
        "gendata",
        "generated_config",
        "cfg",
    }
    relative_parts = {
        part.casefold() for part in _relative_path(path, root).split("/")[:-1]
    }
    return "generated_code" if relative_parts & generated_markers else "source_code"


def _module_name(path: Path, root: Path) -> str:
    name = path.name.casefold()
    parts = {part.casefold() for part in _relative_path(path, root).split("/")}
    suffix = path.suffix.casefold()
    if suffix == ".ldf":
        return "LDF"
    if "canif" in name or "canif" in parts:
        return "CanIf"
    if "pdur" in name or "pdur" in parts:
        return "PduR"
    if "linif" in name or "linif" in parts:
        return "LinIf"
    if "bswm" in name or "bswm" in parts:
        return "BswM"
    if "comm" in name or "comm" in parts:
        return "ComM"
    if name.startswith("com_") or "com" in parts:
        return "Com"
    if name.startswith(("rte_", "os_")) or {"rte", "os"} & parts:
        return "OS/RTE"
    if "cdd" in name or "cdd" in parts or "appl" in parts or "application" in parts:
        return "CDD/Application"
    return "Other"


def _classify_line_role(symbol: str, line: str) -> str:
    escaped = re.escape(symbol)
    if re.search(rf"^\s*#\s*define\s+{escaped}\b", line):
        return "macro_definition"
    if re.search(rf"\btypedef\b[^;]*\b{escaped}\b", line):
        return "type_definition"
    if re.search(rf"\bextern\b[^;]*\b{escaped}\b", line):
        return "declaration"
    if re.search(rf"\b{escaped}\s*\([^;]*\)\s*(?:\{{|$)", line):
        return "function_declaration_or_definition"
    if re.search(rf"\b{escaped}\b\s*(?:\[[^]]*\]\s*)?=", line):
        return "object_definition"
    return "reference"


def _role_counts(occurrences: Iterable[SourceOccurrence]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for occurrence in occurrences:
        result[occurrence.role] += 1
    return dict(sorted(result.items()))


def _is_referable_summary(line: str) -> bool:
    if _REFERABLE_RE.search(line):
        return True
    # 某些生成器把几百个 AUTOSAR key 汇总进文件末尾的一行注释；这种行只会
    # 污染符号查询。保留行偏移供显式读取，但默认不把它当成代码引用。
    return len(line) > 4000 and line.lstrip().startswith(("/*", "//")) and len(
        _IDENTIFIER_RE.findall(line)
    ) > 64


def _bounded_window(text: str, symbol: str, column: int, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    start = max(0, column - 1 - max_chars // 2)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _extract_fields(text: str) -> dict[str, str]:
    result = {}
    for name, value in _FIELD_RE.findall(text):
        result.setdefault(name, value.strip()[:160])
        if len(result) >= 20:
            break
    return result
