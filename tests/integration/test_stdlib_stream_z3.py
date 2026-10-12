"""Real Z3 proof and mutation for an imported ready/valid stdlib slice."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

import pytest

from tests.semantic.test_stdlib_coherence import WITNESSES
from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus, ProofMode


FORMAL_TOOLS = all(
    shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
)
STDLIB_STREAM = Path(__file__).resolve().parents[2] / "stdlib/stream/core.zhl"


MUX_CONTRACT = """
import std.stream.core
module Top {
    clock clk reset rst
    in select : bit
    in input0 : rv<u8>
    in input1 : rv<u8>
    out output : rv<u8>
    inst route : RvMux2<T=u8>
    route.select = select
    connect input0 -> route.input0
    connect input1 -> route.input1
    connect route.output -> output
    assert mux_payload @ clk {
        (~select & (output.payload == input0.payload)) |
        (select & (output.payload == input1.payload))
    }
    assert mux_valid @ clk {
        output.valid == ((~select & input0.valid) | (select & input1.valid))
    }
    assert mux_ready0 @ clk { input0.ready == (~select & output.ready) }
    assert mux_ready1 @ clk { input1.ready == (select & output.ready) }
}
"""


DEMUX_CONTRACT = """
import std.stream.core
module Top {
    clock clk reset rst
    in select : bit
    in input : rv<u8>
    out output0 : rv<u8>
    out output1 : rv<u8>
    inst route : RvDemux2<T=u8>
    route.select = select
    connect input -> route.input
    connect route.output0 -> output0
    connect route.output1 -> output1
    assert demux_payload0 @ clk { output0.payload == input.payload }
    assert demux_payload1 @ clk { output1.payload == input.payload }
    assert demux_valid0 @ clk { output0.valid == (~select & input.valid) }
    assert demux_valid1 @ clk { output1.valid == (select & input.valid) }
    assert demux_ready @ clk {
        input.ready == ((~select & output0.ready) | (select & output1.ready))
    }
}
"""


def _record_source_identity(record_property) -> None:
    record_property("stdlib_source", "stdlib/stream/core.zhl")
    record_property(
        "stdlib_source_sha256", hashlib.sha256(STDLIB_STREAM.read_bytes()).hexdigest()
    )


def _run(source: str, *, mode: ProofMode, depth: int, work_directory):
    compiled = compile_source(source, top="Top")
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    design = connect_formal_design(compiled.formal_design, artifact)
    assert {
        item.generated_from for item in design.properties
    } == {"ready_valid:input", "ready_valid:output"}
    assert all(item.non_executable_reason is None for item in design.properties)
    return run_verilog_formal(
        emit_harness(design, mode=mode, depth=depth),
        top="Top__safety_verification_formal",
        property_id="stdlib.stream.register-slice.ready-valid",
        mode=mode,
        depth=depth,
        systemverilog=True,
        timeout_seconds=30,
        work_directory=work_directory,
    )


def _run_routing_contract(
    source: str,
    *,
    assertion: str,
    mode: ProofMode,
    depth: int,
    work_directory,
):
    compiled = compile_source(source, top="Top")
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    design = connect_formal_design(compiled.formal_design, artifact)
    assert any(
        item.generated_from == f"verification-assert:$module:{assertion}"
        for item in design.properties
    )
    assert all(item.non_executable_reason is None for item in design.properties)
    return run_verilog_formal(
        emit_harness(design, mode=mode, depth=depth),
        top="Top__safety_verification_formal",
        property_id=f"stdlib.stream.{assertion}",
        mode=mode,
        depth=depth,
        systemverilog=True,
        timeout_seconds=30,
        work_directory=work_directory,
    )


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
@pytest.mark.parametrize(
    ("source", "assertion"),
    ((MUX_CONTRACT, "mux_payload"), (DEMUX_CONTRACT, "demux_ready")),
    ids=("mux", "demux"),
)
def test_combinational_router_contract_and_mutation_with_z3(
    tmp_path, record_property, source: str, assertion: str,
) -> None:
    _record_source_identity(record_property)
    passed = _run_routing_contract(
        source,
        assertion=assertion,
        mode=ProofMode.BMC,
        depth=3,
        work_directory=tmp_path / "proof",
    )
    assert passed.status is FormalStatus.BOUNDED_PASS, passed.reason

    mutant = source.replace(
        "route.select = select", "route.select = ~select", 1
    )
    assert mutant != source
    failed = _run_routing_contract(
        mutant,
        assertion=assertion,
        mode=ProofMode.BMC,
        depth=3,
        work_directory=tmp_path / "mutation",
    )
    assert failed.status is FormalStatus.FAILED, failed.reason
    assert failed.counterexample is not None


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
def test_register_slice_ready_valid_proof_and_mutation(
    tmp_path, record_property,
) -> None:
    _record_source_identity(record_property)
    source = WITNESSES["stream_register_slice"]
    passed = _run(
        source, mode=ProofMode.PROVE, depth=4, work_directory=tmp_path / "proof"
    )
    assert passed.status is FormalStatus.PROVEN, passed.reason

    # Keep the stdlib slice instantiated, but bypass its held output payload.
    # A stalled transfer must then produce a real counterexample.
    mutant = source.replace(
        "connect slice.output -> output",
        "output.valid = slice.output.valid\n"
        "            output.payload = input.payload\n"
        "            slice.output.ready = output.ready",
        1,
    )
    assert mutant != source
    failed = _run(
        mutant, mode=ProofMode.BMC, depth=5,
        work_directory=tmp_path / "mutation",
    )
    assert failed.status is FormalStatus.FAILED, failed.reason
    assert failed.counterexample is not None


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
@pytest.mark.parametrize(
    ("fixture", "instance"),
    (("stream_skid_buffer", "skid"), ("stream_core", "queue")),
)
def test_bounded_fifo_ready_valid_and_mutation(
    tmp_path, record_property, fixture: str, instance: str,
) -> None:
    _record_source_identity(record_property)
    source = WITNESSES[fixture]
    passed = _run(
        source, mode=ProofMode.BMC, depth=6, work_directory=tmp_path / "pass"
    )
    assert passed.status is FormalStatus.BOUNDED_PASS, passed.reason
    mutant = source.replace(
        f"connect {instance}.output -> output",
        f"output.valid = {instance}.output.valid\n"
        "            output.payload = input.payload\n"
        f"            {instance}.output.ready = output.ready",
        1,
    )
    assert mutant != source
    failed = _run(
        mutant, mode=ProofMode.BMC, depth=6,
        work_directory=tmp_path / "mutation",
    )
    assert failed.status is FormalStatus.FAILED, failed.reason
    assert failed.counterexample is not None
