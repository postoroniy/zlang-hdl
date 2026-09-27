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
