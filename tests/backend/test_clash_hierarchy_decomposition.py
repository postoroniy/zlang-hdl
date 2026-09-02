"""Byte-locked regression tests for the Clash hierarchy extraction."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shutil

import pytest

from zlang.backend.clash import emitter
from zlang.backend.clash.emitter import (
    ClashEmissionError,
    emit,
    emit_artifact,
)
from zlang.backend.clash.hierarchy import (
    ProtocolApplicationKey,
    ProtocolComponentOwner,
    protocol_child_name,
    recursive_protocol_components,
)
from zlang.backend.clash.syntax import apply_argument
from zlang.compiler import compile_source
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
TOOLS = bool(find_clash_executable() and shutil.which("verilator"))

NESTED_READY_VALID_SOURCE = """
module Leaf {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>
    input.ready = output.ready
    output.payload = input.payload
    output.valid = input.valid
}
module Wrapper {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>
    inst leaf : Leaf
    connect input -> leaf.input
    connect leaf.output -> output
}
module Top {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>
    inst wrapper : Wrapper
    connect input -> wrapper.input
    connect wrapper.output -> output
}
"""

REPEATED_SPECIALIZATION_SOURCE = """
module Leaf {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    input.ready = output.ready
    output.payload = input.payload
    output.valid = input.valid
}
module Wrapper {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    inst leaf : Leaf
    connect input -> leaf.input
    connect leaf.output -> output
}
module Top {
    clock clk reset rst
    in a : rv<u8> out ao : rv<u8>
    in b : rv<u8> out bo : rv<u8>
    inst left : Wrapper
    inst right : Wrapper
    connect a -> left.input
    connect left.output -> ao
    connect b -> right.input
    connect right.output -> bo
}
"""

# Captured after the always-leaf public-top ABI and the compositional
# CSR/request-response correctness repairs. JSON hashes lock the typed public
# leaf bindings and source origins in addition to generated source.  The
# protocol fixture includes the legal full-buffer simultaneous pop/push ready
# path.  The FFT fixture also locks the v2 exact callable-specialization
# identities used by its generic numerical helpers.
CASES = {
    "protocol_top": (
        ROOT / "examples" / "hierarchical_protocol_m40.zhl",
        None,
        "602d99a3baebe14ee7a7aec2e02e3ce06cba7da7c82d099c5bb0aad8f051e60d",
        "9029fbe69da27d10dfcd57300b693139223976920ad9d73f9e6f9a62fe5cd460",
    ),
    "simple_dma": (
        ROOT / "examples" / "simple_dma_m40.zhl",
        "SimpleDMA",
        "a8b8069f0afe94f7ce898d20867784bfedde6087f6e7b09ae0ba456356c072bf",
        "b9b345a1382ad1fcef61d9d8854c3a2a859794c02036d9afc0058df72b8efc56",
    ),
    "fft_specializations": (
        ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl",
        "FFT4SDFReference",
        # ROM companion names now derive from exact typed contents/layout,
        # rather than dependency/source provenance.  The generated logic is
        # otherwise byte-identical and equivalent source spellings share this
        # physical filename and RTL hash.
        "1667cef81edf2f19770d8927f78fc5f5b10fa2cc0be4acc083ae4f55096f3e26",
        "b4132b156016135db66a97319b823f54f3700065f3be1498496d33d54427e8e6",
    ),
    "aggregate_csr": (
        ROOT / "examples" / "axi_csr_top.zhl",
        "AxiCsrTop",
        "f24baa0be06165ccb4bdf2d6038a2ade2f362423867889b84e25f8de823156c1",
        "706b0e2b994d50295da5eaa43e566776924d7c3004dd023070cd1d2275a2e9f7",
    ),
}


def _nested_module():
    return compile_source(
        NESTED_READY_VALID_SOURCE,
        top="Top",
        include_clash=False,
    ).ir


def test_emitter_uses_extracted_component_planner() -> None:
    assert emitter._protocol_child_name is protocol_child_name


def test_shared_clash_argument_rendering_preserves_task_entry_behavior() -> None:
    assert apply_argument("foo.bar") == "foo.bar"
    assert apply_argument("foo bar") == "(foo bar)"


@pytest.mark.parametrize("case", tuple(CASES))
def test_extraction_preserves_example_source_and_artifact_bytes(
    case: str,
) -> None:
    source_path, top, source_hash, json_hash = CASES[case]
    module = compile_source(
        source_path.read_text(),
        top=top,
        include_clash=False,
    ).ir
    text = emit(module)
    artifact = emit_artifact(module)

    assert hashlib.sha256(text.encode()).hexdigest() == source_hash
    assert artifact.text == text
    assert artifact.artifact_hash == source_hash
    encoded = artifact.to_json()
    assert hashlib.sha256(encoded.encode()).hexdigest() == json_hash
    restored = type(artifact).from_json(encoded)
    assert restored.to_json() == encoded


def test_extraction_preserves_nested_source_and_artifact_bytes() -> None:
    module = _nested_module()
    text = emit(module)
    artifact = emit_artifact(module)

    expected_source = (
        "8231d730d2cc612908fed658465cad3ae1325c8bdd9743aa2e74e63901993aaf"
    )
    assert hashlib.sha256(text.encode()).hexdigest() == expected_source
    assert artifact.artifact_hash == expected_source
    assert hashlib.sha256(artifact.to_json().encode()).hexdigest() == (
        "41732e57ff3326c6383d72677332214373418838d966646169aff7e0d8819ce0"
    )


def _catalog_signature(module: object) -> tuple[object, ...]:
    catalog = recursive_protocol_components(
        module,
        error=ClashEmissionError,
    )
    return (
        catalog.root_owner,
        tuple(
            (
                component.specialization_identity,
                component.component_name,
                component.owner,
                component.representative_path,
            )
            for component in catalog.components
        ),
        catalog.applications,
    )


def test_recursive_catalog_is_stable_across_independent_compilations() -> None:
    first_catalog = recursive_protocol_components(
        _nested_module(),
        error=ClashEmissionError,
    )
    second_catalog = recursive_protocol_components(
        _nested_module(),
        error=ClashEmissionError,
    )
    assert first_catalog == second_catalog
    assert repr(first_catalog) == repr(second_catalog)

    first = _catalog_signature(_nested_module())
    second = _catalog_signature(_nested_module())

    assert first == second
    root_owner, _components, applications = first
    assert isinstance(root_owner, ProtocolComponentOwner)
    assert root_owner.kind == "root"
    assert all(
        isinstance(key, ProtocolApplicationKey)
        and isinstance(key.parent, ProtocolComponentOwner)
        and isinstance(key.instance_identity, str)
        for key, _name in applications
    )
    assert not any(
        isinstance(value, int)
        for key, _name in applications
        for value in (
            key.parent.kind,
            key.parent.module_name,
            key.parent.specialization_identity,
            key.instance_identity,
        )
    )


def test_recursive_catalog_uses_representative_physical_paths_for_nested_calls(
) -> None:
    module = _nested_module()
    catalog = recursive_protocol_components(
        module,
        error=ClashEmissionError,
    )

    wrapper = next(
        item for item in catalog.components if item.child.name == "Wrapper"
    )
    leaf = catalog.child(wrapper.representative_path, "leaf")
    assert wrapper.owner is not None
    assert wrapper.owner.kind == "specialization"
    assert leaf.physical_path == ("Top", "wrapper", "leaf")
    assert leaf.instance_identity


def test_repeated_parent_specialization_uses_parent_scoped_catalog_key() -> None:
    module = compile_source(
        REPEATED_SPECIALIZATION_SOURCE,
        top="Top",
        include_clash=False,
    ).ir
    catalog = recursive_protocol_components(
        module,
        error=ClashEmissionError,
    )
    text = emit(module)

    assert [item.child.name for item in catalog.components] == [
        "Wrapper",
        "Leaf",
    ]
    assert text.count("protocol_wrapper ::") == 1
    assert text.count("protocol_leaf ::") == 1
    assert text.count("leaf_result = protocol_leaf input output_backward") == 1
    assert "left_result = protocol_wrapper parent_a ao_backward" in text
    assert "right_result = protocol_wrapper parent_b bo_backward" in text
    root_applications = tuple(
        key for key, _name in catalog.applications
        if key.parent == catalog.root_owner
    )
    nested_applications = tuple(
        key for key, _name in catalog.applications
        if key.parent.kind == "specialization"
    )
    assert len(root_applications) == 2
    assert len({item.instance_identity for item in root_applications}) == 2
    assert len(nested_applications) == 1
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "63732b7616fd3ee6e9268edc765e786fa77c7e1a7bd925b1c1ba1f44b2f6c800"
    )


def test_extracted_planner_preserves_malformed_elaboration_diagnostic() -> None:
    module = _nested_module()
    malformed = replace(module, children=())

    with pytest.raises(
        ClashEmissionError,
        match=(
            "hierarchical child 'Top' has incomplete elaborated instance "
            "metadata"
        ),
    ):
        emit(malformed)


@pytest.mark.skipif(not TOOLS, reason="real Clash and Verilator unavailable")
def test_extracted_nested_component_reaches_verilator(tmp_path: Path) -> None:
    module = _nested_module()
    files = generate_verilog(
        emit(module), "Top", tmp_path, find_clash_executable()
    )
    lint_with_verilator(files, "Top")


@pytest.mark.skipif(not TOOLS, reason="real Clash and Verilator unavailable")
def test_repeated_specialization_component_reaches_verilator(
    tmp_path: Path,
) -> None:
    module = compile_source(
        REPEATED_SPECIALIZATION_SOURCE,
        top="Top",
        include_clash=False,
    ).ir
    files = generate_verilog(
        emit(module), "Top", tmp_path, find_clash_executable()
    )
    lint_with_verilator(files, "Top")
