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
from zlang.backend.clash.syntax import apply_argument, or_signal_expressions
from zlang.compiler import compile_source
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
TOOLS = bool(find_clash_executable() and shutil.which("verilator"))


def test_shared_signal_or_renderer_preserves_applicative_grouping() -> None:
    assert or_signal_expressions([]) == "pure low"
    assert or_signal_expressions(["a"]) == "a"
    assert or_signal_expressions(["a", "b", "c"]) == (
        "((.|.) <$> (((.|.) <$> (a) <*> b)) <*> c)"
    )

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

# Captured after the always-leaf public-top ABI, compositional
# CSR/request-response correctness repairs and shared hierarchy-local naming.
# JSON hashes lock the typed public
# leaf bindings and source origins in addition to generated source.  The
# protocol fixture includes the legal full-buffer simultaneous pop/push ready
# path.  The FFT fixture also locks the v2 exact callable-specialization
# identities used by its generic numerical helpers.
CASES = {
    "protocol_top": (
        ROOT / "examples" / "hierarchical_protocol_m40.zhl",
        None,
        "43f670ee87d730e3f685d4766440d960fd4e6b301280259246c466f2c9468e44",
        "e13f746f7736161de76e6e2245a2f5b7cc86aa42163663b2daa2bec16633f742",
    ),
    "simple_dma": (
        ROOT / "examples" / "simple_dma_m40.zhl",
        "SimpleDMA",
        "86120838b1d701ec048e1e4a64e244c3b3c2e8242e433ae510c76e3d7e2f6a70",
        "95b8199ce1e69ecb8f41350947b017dadcf7d778f82b26ea1e8d074685ad3d19",
    ),
    "fft_specializations": (
        ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl",
        "FFT4SDFReference",
        # ROM companion names now derive from exact typed contents/layout,
        # rather than dependency/source provenance.  The generated logic is
        # otherwise byte-identical and equivalent source spellings share this
        # physical filename and RTL hash.
        "ca52c20e0a278aa1246dd4dd3794057d7bd8505e53a0b4295330a93767f9e3c1",
        "0f8acfac79956377b4d28c496eef75d943052f1003f2884971d68cb199630b05",
    ),
    "aggregate_csr": (
        ROOT / "examples" / "axi_csr_top.zhl",
        "AxiCsrTop",
        # Packed child ports no longer also reserve public leaf aliases.
        # Seven ready nets lose phantom collision suffixes; public bindings,
        # semantic identities and generated expressions remain unchanged.
        "e345f6936eca8783275c7b3af6a25bc79c0b68b4a161921b6687127129a32349",
        "c262030cdb048d7fd6aa286d4caf8013068d630c88e7854ae038b404e22dd288",
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
        "35787fe022de1c933cadd0847f437a6c25ec2545b17d087ec12eb4f0f687e42a"
    )
    assert hashlib.sha256(text.encode()).hexdigest() == expected_source
    assert artifact.artifact_hash == expected_source
    assert hashlib.sha256(artifact.to_json().encode()).hexdigest() == (
        "7bd2cff5b543e96d5376276efcd351c6c4f55c43e0dfe1165ef005df26c4dc79"
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
    wrapper_name, leaf_name = (
        item.component_name for item in catalog.components
    )
    assert text.count(f"{wrapper_name} ::") == 1
    assert text.count(f"{leaf_name} ::") == 1
    assert text.count(f"leaf_result = {leaf_name} input output_backward") == 1
    assert f"left_result = {wrapper_name} parent_a ao_backward" in text
    assert f"right_result = {wrapper_name} parent_b bo_backward" in text
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
        "2316711bb1de9853ab7dded743af9ca03a0017498511a6a04dadcab5562700db"
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
