"""Connected M35 property/harness execution through a real backend artifact."""

from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalError, FormalStatus


COUNTER = """
module ConnectedCounter {
    clock clk
    reset rst
    out y : u8
    reg count : u8 = 0
    count <- truncate<8>(count + 1)
    y = count
}
"""

CREDIT_SENDER = """
module ConnectedCreditSender {
    clock clk
    reset rst
    in data : u8
    in issue : bit
    out tx : credit<u8,2>
    tx.payload = data
    tx.send = issue
}
"""

READY_VALID_BUFFER = """
module ConnectedReadyValid {
    clock clk
    reset rst
    in rx : rv<u8>
    out tx : rv<u8>
    connect rx -> tx { buffer 1 }
}
"""

SIMPLE_DMA = (
    Path(__file__).resolve().parents[2] / "examples/simple_dma_m40.zhl"
).read_text()


def connected_counter():
    module = compile_source(COUNTER).ir
    recursive = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, recursive)
    return module, artifact, connect_formal_design(
        compile_source(COUNTER).formal_design,
        artifact,
    )


def test_connected_harness_uses_only_published_observation_ports() -> None:
    _, artifact, design = connected_counter()
    harness = emit_harness(design, depth=6)
    assert "non-executable property report" not in harness
    assert "ConnectedCounter__formal dut" in harness
    assert "zlang_formal_obs_" in harness
    assert "dut.count" not in harness
    assert artifact.formal_observations
    assert all(
        binding.semantic_signal_id in {"clock", "reset"}
        or binding.rtl_name.startswith("zlang_formal_obs_")
        for binding in design.bindings
    )


def test_missing_formal_observation_fails_only_affected_property_closed() -> None:
    module, artifact, _ = connected_counter()
    count = next(
        item for item in artifact.formal_observations
        if item.semantic_binding_id.endswith(":register:count")
    )
    observations = tuple(
        replace(item, observation_token=None, physical_available=False)
        if item == count else item
        for item in artifact.formal_observations
    )
    broken = replace(artifact, formal_observations=observations)
    connected = connect_formal_design(
        compile_source(COUNTER).formal_design,
        broken,
    )
    count_properties = [
        item for item in connected.properties
        if "register:count" in item.relevant_signals
    ]
    assert count_properties
    assert all(
        item.non_executable_reason
        and "formal observation unavailable" in item.non_executable_reason
        for item in count_properties
    )


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_real_connected_pass_and_reset_mutation_counterexample() -> None:
    _, _, design = connected_counter()
    harness = emit_harness(design, depth=6)
    passed = run_verilog_formal(
        harness,
        top="ConnectedCounter__m35_formal",
        property_id="m35.connected.counter",
        depth=6,
        systemverilog=True,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    mutated = harness.replace("count <= 8'd0;", "count <= 8'd1;")
    assert mutated != harness
    failed = run_verilog_formal(
        mutated,
        top="ConnectedCounter__m35_formal",
        property_id="m35.connected.counter.reset-mutation",
        depth=6,
        systemverilog=True,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace


def test_cli_sby_requires_and_references_connected_harness(tmp_path: Path) -> None:
    source = tmp_path / "counter.zhl"
    source.write_text(COUNTER)
    rtl = tmp_path / "counter.sv"
    harness = tmp_path / "proof" / "counter_formal.sv"
    config = tmp_path / "run" / "counter.sby"
    assert main([
        str(source), "--systemverilog", str(rtl),
        "--formal-harness", str(harness),
        "--formal-sby", str(config), "--formal-depth", "6",
    ]) == 0
    assert "non-executable property report" not in harness.read_text()
    relative = str(Path("..") / "proof" / "counter_formal.sv")
    assert f"read_verilog -sv -formal {relative}" in config.read_text()


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_connected_credit_sender_projections_are_driven_and_prove() -> None:
    compiled = compile_source(CREDIT_SENDER)
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    # Protocol bases are semantic aggregate identities and must never become a
    # fabricated packed signal such as ``tx``.  The executable leaves are
    # explicit, driven projection ports.
    assert " = tx;" not in artifact.text
    assert " = tx_credits;" in artifact.text
    assert " = tx_send;" in artifact.text
    assert " = tx_return;" in artifact.text

    design = connect_formal_design(compiled.formal_design, artifact)
    assert design.properties
    assert all(item.non_executable_reason is None for item in design.properties)
    result = run_verilog_formal(
        emit_harness(design, depth=6),
        top="ConnectedCreditSender__m35_formal",
        property_id="m35.connected.credit-sender",
        depth=6,
        systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_connected_ready_valid_uses_leaves_not_protocol_base_signals() -> None:
    compiled = compile_source(READY_VALID_BUFFER)
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    assert " = rx;" not in artifact.text
    assert " = tx;" not in artifact.text
    design = connect_formal_design(compiled.formal_design, artifact)
    assert len(design.properties) == 2
    assert all(item.non_executable_reason is None for item in design.properties)
    result = run_verilog_formal(
        emit_harness(design, depth=6),
        top="ConnectedReadyValid__m35_formal",
        property_id="m35.connected.ready-valid",
        depth=6,
        systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_simple_dma_all_request_response_observations_prove_and_mutate() -> None:
    compiled = compile_source(
        SIMPLE_DMA, top="SimpleDMA"
    )
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    design = connect_formal_design(compiled.formal_design, artifact)
    properties = tuple(
        item for item in design.properties
        if item.generated_from.startswith("request_response:")
    )
    assert len(properties) == 5
    assert all(item.non_executable_reason is None for item in properties)
    assert not any(
        item.semantic_signal_id.startswith("rr:")
        for item in design.dut_ports
    )

    harness = emit_harness(design, depth=8)
    passed = run_verilog_formal(
        harness,
        top="SimpleDMA__m35_formal",
        property_id="m35.connected.simple-dma-request-response",
        depth=8,
        systemverilog=True,
        source_origin=properties[0].source_origin,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    # Corrupt only the response-buffer observation ABI.  The implementation's
    # response occupancy is now reported one too high, which must violate the
    # existing response-accounting/reset-epoch properties with a real trace.
    needle = "  assign formal_count = count;"
    first = harness.find(needle)
    second = harness.find(needle, first + 1)
    assert first >= 0 and second >= 0
    assert harness.find(needle, second + 1) < 0
    mutated = (
        harness[:second]
        + harness[second:].replace(
            needle, "  assign formal_count = count + 1'b1;", 1
        )
    )
    failed = run_verilog_formal(
        mutated,
        top="SimpleDMA__m35_formal",
        property_id="m35.connected.simple-dma-response-occupancy-mutation",
        depth=8,
        systemverilog=True,
        source_origin=properties[0].source_origin,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.source_origin == properties[0].source_origin
    assert failed.counterexample is not None
    assert failed.counterexample.cycle is not None
    assert failed.counterexample.raw_trace
