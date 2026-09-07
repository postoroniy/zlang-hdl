"""Regression coverage for typed Clash protocol-child specializations.

One parent may instantiate the same source module at more than one concrete
specialization.  Clash helper identity and recursive-formal traversal must use
the elaborated specialization/instance relation, not collapse children by the
source module name.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from zlang.backend.clash import ClashEmissionError, emit
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
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


def _compile(top: str):
    return compile_source(SOURCE.read_text(), top=top).ir


def test_clash_names_and_calls_each_fft_stage_specialization_exactly_once() -> None:
    module = _compile("FFT4SDFReference")
    by_instance = {
        elaborated.instance.name: elaborated
        for elaborated in module.elaborated_instances
    }
    assert set(by_instance) == {"stage_d2", "stage_d1"}

    d2_identity = by_instance["stage_d2"].specialization_identity
    d1_identity = by_instance["stage_d1"].specialization_identity
    assert d2_identity and d1_identity and d2_identity != d1_identity
    d2_helper = f"protocol_fFTSDFStageNumeric_s{d2_identity[:8]}"
    d1_helper = f"protocol_fFTSDFStageNumeric_s{d1_identity[:8]}"

    clash = emit(module)
    assert clash.count(f"{d2_helper} ::") == 1
    assert clash.count(f"{d1_helper} ::") == 1
    assert (
        f"stage_d2_result = {d2_helper} parent_input stage_d1_input_ready"
        in clash
    )
    assert (
        f"stage_d1_result = {d1_helper} stage_d2_output output_backward"
        in clash
    )


def test_clash_uses_exact_depth_one_rom_and_pow2_depth_two_rom() -> None:
    module = _compile("FFT4SDFReference")
    by_instance = {
        elaborated.instance.name: elaborated
        for elaborated in module.elaborated_instances
    }
    d2_helper = (
        "protocol_fFTSDFStageNumeric_s"
        + by_instance["stage_d2"].specialization_identity[:8]
    )
    d1_helper = (
        "protocol_fFTSDFStageNumeric_s"
        + by_instance["stage_d1"].specialization_identity[:8]
    )
    clash = emit(module)

    d2_region = clash[
        clash.index(f"{d2_helper}_raw ::") : clash.index(f"{d2_helper} ::")
    ]
    d1_region = clash[
        clash.index(f"{d1_helper}_raw ::") : clash.index(f"{d1_helper} ::")
    ]
    assert re.search(
        r'twiddles_read_raw = romFilePow2 @1 @32 "[^"]+\.mem"',
        d2_region,
    )
    assert "romFile (SNat @1)" not in d2_region
    assert re.search(
        r'twiddles_read_raw = romFile \(SNat @1\) "[^"]+\.mem"',
        d1_region,
    )
    assert "romFilePow2" not in d1_region


def test_recursive_formal_keeps_specialized_phase_widths_and_physical_paths() -> None:
    module = _compile("FFT4SDFReference")
    expected_specializations = {
        ("FFT4SDFReference", elaborated.instance.name): (
            elaborated.specialization_identity
        )
        for elaborated in module.elaborated_instances
    }
    design = build_recursive_formal_design(module)
    phase_bindings = {
        binding.physical_instance_path: binding
        for binding in design.bindings
        if binding.ref.local_semantic_id == "register:phase"
    }

    d2_path = ("FFT4SDFReference", "stage_d2")
    d1_path = ("FFT4SDFReference", "stage_d1")
    assert set(phase_bindings) == {d2_path, d1_path}
    assert phase_bindings[d2_path].width == 2
    assert phase_bindings[d1_path].width == 1
    assert (
        phase_bindings[d2_path].specialization_identity
        == expected_specializations[d2_path]
    )
    assert (
        phase_bindings[d1_path].specialization_identity
        == expected_specializations[d1_path]
    )
    assert (
        phase_bindings[d2_path].ref.instance_identity
        != phase_bindings[d1_path].ref.instance_identity
    )


def test_single_specialization_gets_a_stable_compact_clash_helper() -> None:
    module = _compile("FFTSDFStageNumericD4")
    helper = f"protocol_fFTSDFStageNumeric_s{module.elaborated_instances[0].specialization_identity[:8]}"
    clash = emit(module)
    assert clash.count(f"{helper} ::") == 1
    assert (
        f"stage_result = {helper} parent_input output_backward"
        in clash
    )
    assert not re.search(r"protocol_fFTSDFStageNumeric_[0-9a-f]{24} ::", clash)


def test_storage_child_wrapper_uses_mangled_reserved_protocol_argument() -> None:
    source = """
module QueueChild {
    clock clk
    reset rst
    in input : rv<u8>
    out data : rv<u8>
    fifo queue : fifo<u8,2>

    input.ready = queue.ready
    data.payload = queue.front
    data.valid = queue.valid
    rule push when input.transfer { queue.push(input.payload) }
    rule pop when data.transfer { queue.pop() }
}

module QueueTop {
    clock clk
    reset rst
    in input : rv<u8>
    out data : rv<u8>
    inst queue : QueueChild
    connect input -> queue.input
    connect queue.data -> data
}
"""
    module = compile_source(source, top="QueueTop").ir
    clash = emit(module)
    helper = f"protocol_queueChild_s{module.elaborated_instances[0].specialization_identity[:8]}"
    assert (
        f"{helper} input data_zlang_backward = "
        f"{helper}_raw input data_zlang_backward"
        in clash
    )


def test_nested_protocol_child_emits_closed_recursive_component_abi() -> None:
    module = compile_source(
        NESTED_READY_VALID_SOURCE, top="Top", include_clash=False
    ).ir
    wrapper = module.children[0]
    leaf = wrapper.children[0]
    wrapper_instance = module.elaborated_instances[0]
    leaf_instance = wrapper.elaborated_instances[0]

    clash = emit(module)

    assert wrapper.name == "Wrapper"
    assert leaf.name == "Leaf"
    assert wrapper_instance.instance_identity
    assert wrapper_instance.specialization_identity
    assert leaf_instance.instance_identity
    assert leaf_instance.specialization_identity
    wrapper_name = f"protocol_wrapper_s{wrapper_instance.specialization_identity[:8]}"
    leaf_name = f"protocol_leaf_s{leaf_instance.specialization_identity[:8]}"
    assert clash.count(f"{wrapper_name} ::") == 1
    assert clash.count(f"{leaf_name} ::") == 1
    assert (
        f"{wrapper_name} input output_backward = "
        "(input_backward_result, output)"
        in clash
    )
    assert f"leaf_result = {leaf_name} input output_backward" in clash
    assert f"wrapper_result = {wrapper_name} parent_input output_backward" in clash
    assert "parent_input" not in clash[
        clash.index(f"{wrapper_name} ::") : clash.index(f"{leaf_name} ::")
    ]


def test_recursive_components_keep_every_nested_specialization_identity() -> None:
    source = """
module Leaf<W=8> {
    clock c reset r
    in input : rv<uint<W>>
    out output : rv<uint<W>>
    input.ready = output.ready
    output.payload = input.payload
    output.valid = input.valid
}
module Wrapper<W=8> {
    clock c reset r
    in input : rv<uint<W>>
    out output : rv<uint<W>>
    inst leaf : Leaf<W>
    connect input -> leaf.input
    connect leaf.output -> output
}
module Top {
    clock c reset r
    in a : rv<u8> out ao : rv<u8>
    in b : rv<u16> out bo : rv<u16>
    inst w8 : Wrapper<8>
    inst w16 : Wrapper<16>
    connect a -> w8.input
    connect w8.output -> ao
    connect b -> w16.input
    connect w16.output -> bo
}
"""
    module = compile_source(source, top="Top", include_clash=False).ir
    clash = emit(module)

    for wrapper, elaborated in zip(
        module.children, module.elaborated_instances, strict=True
    ):
        wrapper_helper = (
            f"protocol_wrapper_s{elaborated.specialization_identity[:8]}"
        )
        leaf_identity = wrapper.elaborated_instances[0].specialization_identity
        leaf_helper = f"protocol_leaf_s{leaf_identity[:8]}"
        assert clash.count(f"{wrapper_helper} ::") == 1
        assert clash.count(f"{leaf_helper} ::") == 1
        assert f"leaf_result = {leaf_helper} input output_backward" in clash
        assert f"{elaborated.instance.name}_result = {wrapper_helper}" in clash


@pytest.mark.skipif(not TOOLS, reason="real Clash and Verilator unavailable")
def test_real_clash_and_verilator_accept_nested_protocol_component(
    tmp_path: Path,
) -> None:
    module = compile_source(
        NESTED_READY_VALID_SOURCE, top="Top", include_clash=False
    ).ir
    files = generate_verilog(
        emit(module), "Top", tmp_path, find_clash_executable()
    )
    lint_with_verilator(files, "Top")


def test_nested_protocol_child_rejects_buffered_internal_edge_explicitly() -> None:
    source = NESTED_READY_VALID_SOURCE.replace(
        "connect input -> leaf.input",
        "connect input -> leaf.input { buffer 1 }",
    )
    module = compile_source(source, top="Top", include_clash=False).ir
    with pytest.raises(
        ClashEmissionError,
        match=(
            "recursive ready/valid child 'Wrapper' supports direct "
            "unbuffered same-domain connections only"
        ),
    ):
        emit(module)
