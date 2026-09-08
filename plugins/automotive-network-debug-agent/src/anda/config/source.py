"""
文件用途：
- 在 Workspace 加载阶段为 C/H 源码建立轻量标识符位置索引。
- 查询时只读取命中文件附近的少量行，避免把整份生成代码放进 Agent 上下文。
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from anda.common.errors import ConfigInputError

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_MAX_CONTEXT_LINE_LENGTH = 240
MAX_SOURCE_RESULTS = 50
MAX_CONTEXT_LINES = 5


@dataclass(frozen=True, slots=True)
class SourceOccurrence:
    """源码中某个标识符出现的一行；不保存该行正文。"""

    symbol: str
    file: Path
    relative_file: str
    line: int
    file_kind: str
    role: str

    def location_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "file": str(self.file),
            "relative_file": self.relative_file,
            "line": self.line,
            "file_kind": self.file_kind,
            "role": self.role,
        }


class SourceIndex:
    """以标识符 casefold 为键的只读源码位置索引。"""

    def __init__(self, root: Path, files: list[Path]) -> None:
        self.root = root
        self.files = tuple(files)
        self._by_symbol: dict[str, list[SourceOccurrence]] = defaultdict(list)
        self.occurrence_count = 0
        self._build()

    @property
    def symbol_count(self) -> int:
        return len(self._by_symbol)

    def _build(self) -> None:
        for path in self.files:
            lines = _read_source_lines(path)
            relative_file = path.relative_to(self.root).as_posix()
            file_kind = _file_kind(path, self.root)
            for line_number, line in enumerate(lines, start=1):
                # 同一标识符在同一行出现多次，对“源码位置”没有新增信息，只记录一次。
                symbols = dict.fromkeys(_IDENTIFIER_RE.findall(line))
                for symbol in symbols:
                    self._by_symbol[symbol.casefold()].append(
                        SourceOccurrence(
                            symbol=symbol,
                            file=path,
                            relative_file=relative_file,
                            line=line_number,
                            file_kind=file_kind,
                            role=_classify_line_role(symbol, line),
                        )
                    )
                    self.occurrence_count += 1

    def occurrences(self, symbol: str) -> list[SourceOccurrence]:
        """按完整标识符查询位置；不做容易产生误关联的子串猜测。"""
        return list(self._by_symbol.get(symbol.casefold(), ()))

    def search_symbols(self, query: str, limit: int = 20) -> dict:
        """按完整名、前缀或子串搜索源码标识符，不返回源码正文。"""
        normalized = query.strip().casefold()
        if not normalized:
            raise ConfigInputError("query 不能为空")
        if limit < 1:
            raise ConfigInputError("limit 必须大于等于 1")
        applied_limit = min(limit, MAX_SOURCE_RESULTS)
        candidates = []
        for key, occurrences in self._by_symbol.items():
            if normalized not in key:
                continue
            roles: dict[str, int] = defaultdict(int)
            for occurrence in occurrences:
                roles[occurrence.role] += 1
            candidates.append(
                {
                    "symbol": occurrences[0].symbol,
                    "matched_by": (
                        "exact"
                        if key == normalized
                        else "prefix"
                        if key.startswith(normalized)
                        else "substring"
                    ),
                    "occurrence_count": len(occurrences),
                    "role_counts": dict(sorted(roles.items())),
                    "sample_locations": [
                        occurrence.location_dict() for occurrence in occurrences[:3]
                    ],
                }
            )
        candidates.sort(
            key=lambda item: (
                {"exact": 0, "prefix": 1, "substring": 2}[item["matched_by"]],
                item["symbol"].casefold(),
            )
        )
        return {
            "query": query.strip(),
            "total_count": len(candidates),
            "returned_count": min(len(candidates), applied_limit),
            "limit_applied": applied_limit,
            "matches": candidates[:applied_limit],
        }

    def contexts(self, symbol: str, limit: int, context_lines: int) -> dict:
        """返回受限源码上下文，正文只在此调用期间读取。"""
        _validate_context_request(symbol, limit, context_lines)
        applied_limit = min(limit, MAX_SOURCE_RESULTS)
        applied_context = min(context_lines, MAX_CONTEXT_LINES)
        occurrences = self.occurrences(symbol)
        line_cache: dict[Path, list[str]] = {}
        matches = []

        for occurrence in occurrences[:applied_limit]:
            lines = line_cache.get(occurrence.file)
            if lines is None:
                lines = _read_source_lines(occurrence.file)
                line_cache[occurrence.file] = lines
            start = max(1, occurrence.line - applied_context)
            end = min(len(lines), occurrence.line + applied_context)
            context = [
                {
                    "line": line_number,
                    "text": lines[line_number - 1][:_MAX_CONTEXT_LINE_LENGTH],
                }
                for line_number in range(start, end + 1)
            ]
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
        """检查一个完整源码标识符；只报告词法位置、角色和有限上下文。"""
        result = self.contexts(symbol, limit, context_lines)
        result["inspection_basis"] = "exact_c_identifier"
        result["semantic_inference_performed"] = False
        return result

    def locations_for_symbols(self, symbols: list[str], limit: int = 20) -> list[dict]:
        """为路由对象附加生成代码位置，不返回源码正文。"""
        result: list[dict] = []
        seen: set[tuple[str, int, str]] = set()
        for symbol in symbols:
            for occurrence in self.occurrences(symbol):
                key = (
                    str(occurrence.file).casefold(),
                    occurrence.line,
                    symbol.casefold(),
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(occurrence.location_dict())
                if len(result) >= limit:
                    return result
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


def _read_source_lines(path: Path) -> list[str]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ConfigInputError(f"无法读取源码文件：{path}：{error}") from error
    if b"\x00" in payload:
        return []
    try:
        return payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        # 国内汽车工程的历史生成代码常见 GBK/GB18030。这里仅为文本定位做兼容，
        # 不据此推断源文件语义或修改原始编码。
        return payload.decode("gb18030", errors="replace").splitlines()


def _file_kind(path: Path, root: Path) -> str:
    generated_markers = {"generated", "gen", "generated_config", "cfg"}
    relative_parts = {part.casefold() for part in path.relative_to(root).parts[:-1]}
    return "generated_code" if relative_parts & generated_markers else "source_code"


def _classify_line_role(symbol: str, line: str) -> str:
    """基于单行语法给出保守角色标签，不宣称完成 C/C++ AST 解析。"""
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


def _role_counts(occurrences: list[SourceOccurrence]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for occurrence in occurrences:
        result[occurrence.role] += 1
    return dict(sorted(result.items()))
