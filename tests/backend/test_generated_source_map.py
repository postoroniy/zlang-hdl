from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from zlang.backend.source_map import (
    GENERATED_SOURCE_MAP_VERSION,
    GeneratedLineRange,
    GeneratedSourceMap,
    GeneratedSourceMapEntry,
    build_generated_source_map,
)
from zlang.backend.systemverilog import (
    emit_artifact as emit_sv_artifact,
    emit_artifact_with_source_map as emit_sv_bundle,
)
from zlang.compiler import compile_source
from zlang.source import SourceOrigin, SourceSpan
from zlang.toolchain import (
    GeneratedDiagnosticContext,
    attribute_combined_generated_diagnostic,
    attribute_generated_diagnostic,
)


SOURCE = """
module MappedAdd {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""


def _module():
    return compile_source(
        SOURCE,
        source_unit="tests/fixtures/generated_source_map.zhl",
    ).ir




def test_source_map_is_deterministic_round_trippable_and_writable(tmp_path):
    module = _module()
    first_artifact, first = emit_sv_bundle(module)
    second_artifact, second = emit_sv_bundle(module)
    assert first_artifact.artifact_hash == second_artifact.artifact_hash
    assert first.to_json() == second.to_json()

    restored = GeneratedSourceMap.from_json(first.to_json())
    assert restored == first
    payload = json.loads(first.to_json())
    assert payload["version"] == GENERATED_SOURCE_MAP_VERSION
    assert set(payload["entries"][0]["source_origin"]) == {
        "source_unit", "digest", "span", "construct"
    }

    sidecar = tmp_path / "MappedAdd.sv.zmap.json"
    assert first.write_sidecar(sidecar) == sidecar
    assert sidecar.read_text(encoding="utf-8") == first.to_json()


def test_source_map_granularity_is_exact_assignment_lines_only():
    module = _module()
    artifact, source_map = emit_sv_bundle(module)
    assert len(source_map.entries) == 1

    assignment_line = source_map.entries[0].generated.start_line
    lines = artifact.text.splitlines()
    module_line = next(
        index for index, line in enumerate(lines, 1) if line.startswith("module ")
    )
    input_line = next(
        index for index, line in enumerate(lines, 1) if "input wire logic" in line
    )
    helper_comment_line = next(
        index
        for index, line in enumerate(lines, 1)
        if "Generated from backend-independent typed ZLang IR" in line
    )

    assert source_map.entries_for_line(assignment_line) == source_map.entries
    assert source_map.entries_for_line(module_line) == ()
    assert source_map.entries_for_line(input_line) == ()
    assert source_map.entries_for_line(helper_comment_line) == ()


def test_recursive_manifest_bindings_do_not_imply_generated_line_mappings():
    source = """
module Child {
    in a : u8
    out y : u8
    y = a
}

module Parent {
    in a : u8
    out y : u8
    child : Child
    child.a = a
    y = child.y
}
"""
    result = compile_source(source, top="Parent", source_unit="hierarchy.zhl")
    artifact, source_map = emit_sv_bundle(
        result.ir,
        recursive_design=result.recursive_formal_design,
    )

    assert len(artifact.components) == 2
    assert len(artifact.instances) == 2
    assert artifact.recursive_bindings
    assert all(item.source_origin is None for item in artifact.instances)
    assert all(item.source_origin is None for item in artifact.recursive_bindings)
    assert source_map.entries == ()


def test_source_origin_digest_records_the_snapshot_needed_for_stale_detection():
    module = compile_source(
        SOURCE,
        source_unit="tests/fixtures/generated_source_map.zhl",
    ).ir
    _, source_map = emit_sv_bundle(module)
    origin = source_map.entries[0].source_origin
    assert origin.digest == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert origin.digest != hashlib.sha256((SOURCE + "\n").encode()).hexdigest()


def test_reverse_line_lookup_preserves_all_proven_overlapping_entries():
    first_origin = SourceOrigin(
        SourceSpan(2, 1, 2, 2),
        "name a",
        "overlap.zhl",
        "a" * 64,
    )
    second_origin = SourceOrigin(
        SourceSpan(3, 1, 3, 2),
        "name b",
        "overlap.zhl",
        "a" * 64,
    )
    source_map = GeneratedSourceMap(
        "direct_systemverilog",
        "Overlap",
        "selected:test",
        "b" * 64,
        (
            GeneratedSourceMapEntry(
                GeneratedLineRange(7, 7), "value:a", first_origin
            ),
            GeneratedSourceMapEntry(
                GeneratedLineRange(7, 7), "value:b", second_origin
            ),
        ),
    )

    assert source_map.entries_for_line(7) == source_map.entries


def test_source_map_preserves_extended_origin_fields_when_available():
    origin = SourceOrigin(
        span=SourceSpan(3, 5, 3, 10),
        construct="operator +",
        source_unit="examples/mapped_add.zhl",
        digest="a" * 64,
    )
    source_map = GeneratedSourceMap(
        "direct_systemverilog",
        "MappedAdd",
        "selected:test",
        "b" * 64,
        (GeneratedSourceMapEntry(GeneratedLineRange(7, 7), "port:y", origin),),
    )
    restored = GeneratedSourceMap.from_json(source_map.to_json())
    assert restored == source_map


def test_ambiguous_or_unbound_generated_statement_is_not_guessed():
    module = _module()
    artifact = emit_sv_artifact(module)
    extra = "\n  assign y = a;\n"
    text = artifact.text + extra
    digest = hashlib.sha256(text.encode()).hexdigest()
    # Keep artifact and binding hashes coherent while deliberately making the
    # generated assignment location ambiguous.
    bindings = tuple(replace(item, artifact_hash=digest) for item in artifact.bindings)
    ambiguous = replace(artifact, text=text, artifact_hash=digest, bindings=bindings)
    assert build_generated_source_map(module, ambiguous).entries == ()

    unbound = replace(
        artifact,
        bindings=tuple(
            replace(item, source_origin=None)
            if item.semantic_signal_id == "port:y" else item
            for item in artifact.bindings
        ),
    )
    assert build_generated_source_map(module, unbound).entries == ()




def test_source_map_rejects_mismatched_artifact_text_and_module():
    module = _module()
    artifact = emit_sv_artifact(module)
    with pytest.raises(ValueError, match="artifact text"):
        build_generated_source_map(module, replace(artifact, text=artifact.text + "\n"))
    with pytest.raises(ValueError, match="module mismatch"):
        build_generated_source_map(module, replace(artifact, module="Other"))


def test_source_map_rejects_invalid_schema_data():
    with pytest.raises(ValueError, match="line range"):
        GeneratedLineRange(0, 1)
    with pytest.raises(ValueError, match="unsupported.*version"):
        GeneratedSourceMap(
            "direct_systemverilog", "M", "selected:M", "a" * 64, (), version=99
        )
    with pytest.raises(ValueError, match="SHA-256"):
        GeneratedSourceMap("direct_systemverilog", "M", "selected:M", "not-a-hash")
    with pytest.raises(ValueError, match="positive"):
        GeneratedSourceMap(
            "direct_systemverilog", "M", "selected:M", "a" * 64
        ).entries_for_line(0)


def test_external_tool_diagnostic_uses_only_hash_verified_exact_mapping():
    module = compile_source(
        SOURCE,
        source_unit="examples/mapped_add.zhl",
    ).ir
    artifact, source_map = emit_sv_bundle(module)
    line = source_map.entries[0].generated.start_line
    detail = f"%Error: MappedAdd.sv:{line}:7: deliberate failure"

    attributed = attribute_generated_diagnostic(
        detail, source_map, artifact.text
    )
    assert "ZLang origin: examples/mapped_add.zhl:" in attributed
    assert "(operator +)" in attributed
    assert attribute_generated_diagnostic(
        detail, source_map, artifact.text + "// changed\n"
    ) == detail
    assert attribute_generated_diagnostic(
        "%Error: MappedAdd.sv:1:1: header failure",
        source_map,
        artifact.text,
    ).count("ZLang origin:") == 0


def test_combined_formal_source_attribution_applies_exact_line_offset():
    module = compile_source(
        SOURCE,
        source_unit="examples/mapped_add.zhl",
    ).ir
    artifact, source_map = emit_sv_bundle(module)
    local_line = source_map.entries[0].generated.start_line
    prefix = "module unrelated;\nendmodule\n"
    offset = prefix.count("\n") + 1
    context = GeneratedDiagnosticContext(source_map, artifact.text, offset)
    detail = f"ERROR: formal.v:{local_line + offset}:9: deliberate failure"

    attributed = attribute_combined_generated_diagnostic(detail, (context,))
    assert "ZLang origin: examples/mapped_add.zhl:" in attributed
    assert "(operator +)" in attributed
    assert attribute_combined_generated_diagnostic(
        f"ERROR: formal.v:{local_line}:9: wrong source slice",
        (context,),
    ) == f"ERROR: formal.v:{local_line}:9: wrong source slice"


def test_combined_attribution_does_not_merge_distinct_source_snapshots():
    module = compile_source(
        SOURCE,
        source_unit="examples/first.zhl",
    ).ir
    artifact, source_map = emit_sv_bundle(module)
    entry = source_map.entries[0]
    other_origin = replace(
        entry.source_origin,
        source_unit="examples/second.zhl",
        digest="c" * 64,
    )
    other_map = replace(
        source_map,
        entries=(replace(entry, source_origin=other_origin),),
    )
    line = entry.generated.start_line
    detail = f"ERROR: combined.sv:{line}:9: ambiguous source snapshots"

    assert attribute_combined_generated_diagnostic(
        detail,
        (
            GeneratedDiagnosticContext(source_map, artifact.text),
            GeneratedDiagnosticContext(other_map, artifact.text),
        ),
    ) == detail
