from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from zlang.backend.clash import (
    emit_artifact as emit_clash_artifact,
    emit_artifact_with_source_map as emit_clash_bundle,
)
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
        include_clash=False,
        source_unit="tests/fixtures/generated_source_map.zhl",
    ).ir


@pytest.mark.parametrize(
    ("emit_plain", "emit_bundle", "statement"),
    (
        (emit_sv_artifact, emit_sv_bundle, "assign y ="),
        (emit_clash_artifact, emit_clash_bundle, "topEntity a b ="),
    ),
)
def test_artifact_bundle_maps_only_the_exact_top_assignment(
    emit_plain, emit_bundle, statement
):
    module = _module()
    plain = emit_plain(module, selected_ir_identity="selected:test:mapped-add")
    artifact, source_map = emit_bundle(
        module, selected_ir_identity="selected:test:mapped-add"
    )

    # Publishing a sidecar must not alter generated hardware text or identity.
    assert artifact.text == plain.text
    assert artifact.artifact_hash == plain.artifact_hash
    assert source_map.artifact_hash == artifact.artifact_hash
    assert source_map.selected_ir_identity == artifact.selected_ir_identity
    assert len(source_map.entries) == 1
    entry = source_map.entries[0]
    assert entry.semantic_identity == "port:y"
    assert entry.source_origin.construct == "operator +"
    assert entry.source_origin.source_unit == "tests/fixtures/generated_source_map.zhl"
    assert entry.source_origin.digest == hashlib.sha256(SOURCE.encode()).hexdigest()
    generated_line = artifact.text.splitlines()[entry.generated.start_line - 1]
    assert statement in generated_line
    assert source_map.entries_for_line(entry.generated.start_line) == (entry,)
    assert source_map.entries_for_line(1) == ()


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


def test_clash_sequential_wrapper_line_is_not_misattributed():
    module = compile_source(
        """
        module Counter {
            clock clk
            reset rst
            in enable : bit
            out value : u8
            reg count : u8 = 0
            when enable { count <- truncate<8>(count + 1) }
            value = count
        }
        """,
        include_clash=False,
    ).ir
    clash = emit_clash_artifact(module)
    assert build_generated_source_map(module, clash).entries == ()

    # Direct SV has an explicit, unique assignment for this output and can map
    # it without attributing an enclosing state-machine wrapper.
    direct = emit_sv_artifact(module)
    direct_map = build_generated_source_map(module, direct)
    assert tuple(item.semantic_identity for item in direct_map.entries) == ("port:value",)


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
            "clash", "M", "selected:M", "a" * 64, (), version=99
        )
    with pytest.raises(ValueError, match="SHA-256"):
        GeneratedSourceMap("clash", "M", "selected:M", "not-a-hash")
    with pytest.raises(ValueError, match="positive"):
        GeneratedSourceMap(
            "clash", "M", "selected:M", "a" * 64
        ).entries_for_line(0)


def test_external_tool_diagnostic_uses_only_hash_verified_exact_mapping():
    module = compile_source(
        SOURCE,
        include_clash=False,
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
        include_clash=False,
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
