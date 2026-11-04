# SPDX-License-Identifier: Apache-2.0
import pytest

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit
from zlang.semantic.errors import SemanticError


def design(body, extra=""):
    return f"module T {{clock clk reset rst in a:u8 in enable:bit out y:u8 y=a {extra} simulation io @clk {{{body}}}}}"


@pytest.mark.parametrize(
    "body,message",
    [
        ("read src {x -> y} else {finish}", "environment-owned"),
        ("read src {x -> rst} else {finish}", "environment-owned"),
        ("read src {x -> a} else {finish} drive a=1", "overlapping"),
        ("read src {x -> a x -> a} else {finish}", "duplicate"),
        ("read src {x -> a} else {read other {x -> a} else {finish}}", "EOF"),
        ("read src {x -> a} else {finish} write src {x=y}", "distinct"),
        ("write dst {x=y} write dst {x=enable}", "inconsistent"),
        ("when a==0 {read src {x -> a} else {finish}}", "feedback"),
        ("when y==0 {read src {x -> a} else {finish}}", "feedback"),
        (
            "read src {x -> a} else {finish} read other {x -> a} else {finish}",
            "one input channel",
        ),
        ("drive enable=a", "bit"),
    ],
)
def test_type_ownership_schema_and_feedback_rejections(body, message):
    with pytest.raises(SemanticError, match=message):
        compile_source(design(body))


def test_inactive_block_preserves_selected_hardware_rtl_and_port_abi():
    from zlang.ir.hierarchy import specialization_fingerprint

    baseline = compile_source(
        "module T {clock clk reset rst in a:u8 in enable:bit out y:u8 y=a}"
    ).ir
    added = compile_source(
        design(
            "when ~rst & enable {read src {x -> a} else {drive a=0}} write dst {x=y}"
        )
    ).ir
    assert emit(added) == emit(baseline)
    assert added.top_physical_abi == baseline.top_physical_abi
    assert specialization_fingerprint(added) == specialization_fingerprint(baseline)


def test_activated_observation_metadata_is_not_hardware_specialization_content(tmp_path):
    from dataclasses import replace

    from zlang import sim
    from zlang.ir.hierarchy import specialization_fingerprint
    from zlang.ir.verification import verification_module_identity
    from zlang.target_mapping_selection import generic_implementation_graph

    path = tmp_path / "dut.zhl"
    path.write_text(design("write dst {x=y}"))
    program = sim.compile(path)
    assert program.simulation_blocks
    annotated = replace(program.module, simulation_blocks=program.simulation_blocks)
    assert specialization_fingerprint(annotated) == specialization_fingerprint(program.module)
    assert verification_module_identity(annotated) == verification_module_identity(program.module)
    assert generic_implementation_graph(annotated) == generic_implementation_graph(program.module)


def test_read_and_eof_drive_are_exclusive_owners():
    compile_source(design("when enable {read src {x -> a} else {drive a=0}}"))


def test_unknown_clock_and_duplicate_block_rejected():
    with pytest.raises(SemanticError, match="unknown clock"):
        compile_source(design("").replace("io @clk", "io @missing"))
    with pytest.raises(SemanticError, match="duplicate simulation"):
        compile_source(design("", extra="simulation io @clk {finish}"))


def test_immutable_wire_alias_cannot_be_used_as_an_input_target():
    with pytest.raises(SemanticError, match="top input"):
        compile_source(design("read src {x -> alias} else {finish}", extra="alias=a"))


def test_transfer_guard_cannot_hide_input_feedback():
    source = """module T {clock clk reset rst in rx:rv<u8> out tx:rv<u8> connect rx -> tx
simulation io @clk {when rx.transfer {read src {v->rx.payload active->rx.valid} else {finish}}}}"""
    with pytest.raises(SemanticError, match="feedback"):
        compile_source(source)


def test_local_alias_cannot_hide_read_feedback():
    with pytest.raises(SemanticError, match="feedback"):
        compile_source(
            design("when alias==0 {read src {x->a} else {finish}}", extra="alias=a")
        )
