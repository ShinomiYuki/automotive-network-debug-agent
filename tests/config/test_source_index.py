"""验证 SQLite 项目索引、增量失效、长行切片与批量负向搜索。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from anda.config.source import SourceIndex


def _project_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_source_index_batches_long_generated_lines_and_reads_by_offset(tmp_path):
    root = tmp_path / "project"
    generated = root / "Appl" / "GenData" / "Com_Lcfg.c"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        "\n".join(
            [
                "const int Com_GwSigMapping[] = {",
                "  { /* RxAccessInfoIdx */ 1184U, AmbLigBriAdj, "
                + "X" * 2000
                + ", /* TxSigInfoIdx */ 91U },",
                "};",
                "/* referable-key OnlyInReferableSummary " + "Y" * 5000 + " */",
            ]
        ),
        encoding="utf-8",
    )
    cdd = root / "Appl" / "CDD" / "Gateway.c"
    cdd.parent.mkdir(parents=True)
    cdd.write_text("void Observe(void) { Use(ICC_TDL_24); }\n", encoding="utf-8")
    wildcard = root / "Appl" / "CDD" / "Wildcard.c"
    wildcard.write_text("int A_B; int AXB;\n", encoding="utf-8")
    cache = tmp_path / "cache"

    index = SourceIndex(
        root,
        [generated, cdd],
        inventory_files=_project_files(root),
        cache_dir=cache,
    )
    result = index.search_occurrences(
        ["AmbLigBriAdj", "DefinitelyMissing"],
        modules=["Com"],
        max_chars_per_match=500,
    )
    match = result["results"][0]["matches"][0]

    assert index.reindexed_file_count == 3
    assert match["file"] == str(generated.resolve())
    assert match["line"] == 2
    assert match["table"] == "Com_GwSigMapping"
    assert len(match["snippet"]) <= 502
    assert match["snippet_truncated"] is True
    assert result["not_found"] == ["DefinitelyMissing"]
    assert result["negative_result_is_scope_limited"] is True
    assert result["search_scope"]["indexed_source_file_count"] == 1
    assert index.occurrences("OnlyInReferableSummary") == []
    assert [item["symbol"] for item in index.search_symbols("A_B")["matches"]] == [
        "A_B"
    ]

    lines = index.read_lines(str(generated), [[2, 2]], max_chars_per_line=300)
    assert lines["returned_line_count"] == 1
    assert lines["lines"][0]["truncated"] is True
    assert len(lines["lines"][0]["text"]) == 300


def test_source_index_reuses_unchanged_files_and_reindexes_only_changes(tmp_path):
    root = tmp_path / "project"
    first = root / "Appl" / "GenData" / "Com_Lcfg.c"
    second = root / "Appl" / "CDD" / "Gateway.c"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("const int AmbLigBriAdj = 1;\n", encoding="utf-8")
    second.write_text("void Gateway(void) {}\n", encoding="utf-8")
    cache = tmp_path / "cache"
    files = _project_files(root)

    initial = SourceIndex(root, files, cache_dir=cache)
    reused = SourceIndex(root, files, cache_dir=cache)
    second.write_text("void Gateway(void) { Observe(); }\n", encoding="utf-8")
    changed = SourceIndex(root, files, cache_dir=cache)

    assert initial.reindexed_file_count == 2
    assert reused.reindexed_file_count == 0
    assert reused.reused_file_count == 2
    assert changed.reindexed_file_count == 1
    assert changed.reused_file_count == 1
    assert changed.occurrences("Observe")[0].line == 1
    assert initial.occurrences("Observe") == []
    assert reused.occurrences("Observe") == []
    assert changed.cache_status()["workspace_generation_pinned"] is True


def test_source_search_plan_uses_file_inventory_before_content(tmp_path):
    root = tmp_path / "project"
    paths = [
        root / "Appl" / "GenData" / "CanIf_Lcfg.c",
        root / "Appl" / "GenData" / "Com_Lcfg.c",
        root / "Appl" / "GenData" / "PduR_Lcfg.c",
        root / "Input" / "LIN" / "body.ldf",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/* synthetic */\n", encoding="utf-8")
    source_files = [path for path in paths if path.suffix == ".c"]
    index = SourceIndex(
        root,
        source_files,
        inventory_files=paths,
        cache_dir=tmp_path / "cache",
    )

    plan = index.plan_search("can_to_lin_timeout", ["AmbLigBriAdj"])

    assert plan["strategy"] == (
        "file_inventory_then_module_filter_then_batched_identifier_query"
    )
    first_stage = plan["stages"][0]
    assert first_stage["candidate_file_count"] == 4
    assert {item["module"] for item in first_stage["candidate_files"]} == {
        "CanIf",
        "Com",
        "PduR",
        "LDF",
    }


def test_source_symbol_search_bounds_samples_for_frequent_identifier(tmp_path):
    root = tmp_path / "project"
    source = root / "Appl" / "GenData" / "Com_Lcfg.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(f"int ComFrequent = {index};" for index in range(200)),
        encoding="utf-8",
    )
    index = SourceIndex(root, [source], cache_dir=tmp_path / "cache")

    result = index.search_symbols("Com", limit=1)

    assert result["total_count"] == 1
    assert result["returned_count"] == 1
    assert result["matches"][0]["occurrence_count"] == 200
    assert len(result["matches"][0]["sample_locations"]) == 3


def test_source_index_serializes_shared_cache_rebuilds(tmp_path):
    root = tmp_path / "project"
    source = root / "Appl" / "GenData" / "Com_Lcfg.c"
    source.parent.mkdir(parents=True)
    source.write_text("const int SharedSymbol = 1;\n", encoding="utf-8")
    cache = tmp_path / "cache"

    def build() -> SourceIndex:
        return SourceIndex(root, [source], cache_dir=cache)

    with ThreadPoolExecutor(max_workers=2) as executor:
        indexes = list(executor.map(lambda _: build(), range(2)))

    assert sum(item.reindexed_file_count for item in indexes) == 1
    assert sum(item.reused_file_count for item in indexes) == 1
    assert all(item.occurrences("SharedSymbol") for item in indexes)
