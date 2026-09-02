"""Root receiver-credit execution through the typed Clash formal ABI."""

from __future__ import annotations

import json
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import (
    emit_artifact,
    emit_formal_artifact,
    finalize_formal_verilog_artifact,
)
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_formal_artifact as emit_sv_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, PropertyKind
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)
from zlang.verification_bundle import (
    VerificationRunConfig,
    run_verification_bundle,
)
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)


SOURCE = """
module FormalCreditReceiver {
    clock clk reset rst
    in rx : credit<u8,2>
    out observed : u8

    rx.return = 0
    observed = rx.payload
}
"""
ROOT = Path(__file__).resolve().parents[2]
CREDIT_TO_RV = ROOT / "examples" / "credit_to_rv.zhl"


def _compilation():
    return compile_source(
        SOURCE,
        include_clash=False,
        source_unit="tests/fixtures/formal_credit_receiver.zhl",
    )


def test_receiver_credit_formal_source_is_separate_and_typed() -> None:
    compilation = _compilation()
    production = emit_artifact(compilation.ir)
    repeated = emit_artifact(compilation.ir)
    formal = emit_formal_artifact(
        compilation.ir, compilation.recursive_formal_design,
    )

    assert production.text == repeated.text
    assert production.artifact_hash == repeated.artifact_hash
    assert 't_name = "FormalCreditReceiver"' in production.text
    assert 't_name = "FormalCreditReceiver_formal"' in formal.text
    assert "rx_occupancy" in formal.text
    assert "rx_send" in formal.text
    assert "rx_return" in formal.text
    assert formal.text.count('PortName "zlang_formal_obs_') == 3
    assert not any(
        item.physical_available
        for item in formal.recursive_bindings
        if item.local_semantic_id == "port:rx.occupancy"
    )

    # Receiver support remains intentionally absent from direct SV so the
    # compiler-owned per-goal router must select the Clash route.
    with pytest.raises(
        SystemVerilogEmissionError,
        match="supports one sender endpoint",
    ):
        emit_sv_formal_artifact(
            compilation.ir, compilation.recursive_formal_design,
        )


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash and Verilator are required",
)
def test_receiver_credit_observations_are_validated_after_real_rtl(
    tmp_path: Path,
) -> None:
    compilation = compile_source(
        CREDIT_TO_RV.read_text(),
        include_clash=False,
        source_unit=str(CREDIT_TO_RV),
    )
    artifact = emit_formal_artifact(
        compilation.ir, compilation.recursive_formal_design,
    )
    files = generate_verilog(
        artifact.text,
        compilation.ir.name,
        tmp_path / "clash",
        find_clash_executable(),
        companions=artifact.companions,
    )
    lint_with_verilator(files, "CreditToRv_formal")
    finalized = finalize_formal_verilog_artifact(artifact, files)

    by_local = {
        item.local_semantic_id: item
        for item in finalized.recursive_bindings
        if item.local_semantic_id.startswith("port:rx.")
    }
    assert by_local["port:rx.occupancy"].width == 2
    assert by_local["port:rx.occupancy"].formal_observation_token.startswith(
        "zlang_formal_obs_"
    )
    assert by_local["port:rx.send"].formal_observation_token.startswith(
        "zlang_formal_obs_"
    )
    assert by_local["port:rx.return"].formal_observation_token.startswith(
        "zlang_formal_obs_"
    )
    assert all(item.physical_available for item in by_local.values())

    # Cycle-identical ordered trace for the repaired full pop+push boundary.
    harness = tmp_path / "credit_to_rv.cpp"
    harness.write_text(r'''
#include "VCreditToRv_formal.h"
static void tick(VCreditToRv_formal& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VCreditToRv_formal d;
  d.rst=1; d.rx_send=0; d.rx_payload=0; d.tx_ready=0; tick(d);
  d.rst=0; d.rx_send=1; d.rx_payload=9; tick(d);
  if (!d.tx_valid || d.tx_payload != 9 || d.rx_return) return 1;
  d.rx_payload=10; tick(d);
  if (!d.tx_valid || d.tx_payload != 9 || d.rx_return) return 2;
  d.rx_payload=11; d.tx_ready=1; d.eval();
  if (!d.rx_return) return 3;
  tick(d);
  if (!d.tx_valid || d.tx_payload != 10) return 4;
  d.rx_send=0; d.eval(); if (!d.rx_return) return 5; tick(d);
  if (!d.tx_valid || d.tx_payload != 11 || !d.rx_return) return 6;
  tick(d);
  return d.tx_valid ? 7 : 0;
}
''')
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "--top-module", "CreditToRv_formal",
            "--Mdir", str(obj),
            *(str(path) for path in files),
            str(harness),
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        (str(obj / "VCreditToRv_formal"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr

    top = next(
        path for path in files
        if path.name == "CreditToRv_formal.v"
    )
    occupancy = by_local["port:rx.occupancy"].formal_observation_token
    assert occupancy is not None
    original = top.read_text()
    wrong = original.replace(
        f"output wire [1:0] {occupancy}",
        f"output wire {occupancy}",
        1,
    )
    assert wrong != original
    top.write_text(wrong)
    rejected = finalize_formal_verilog_artifact(artifact, files)
    rejected_occupancy = next(
        item for item in rejected.recursive_bindings
        if item.local_semantic_id == "port:rx.occupancy"
    )
    assert not rejected_occupancy.physical_available
    assert rejected_occupancy.formal_observation_token is None


@pytest.mark.skipif(
    find_clash_executable() is None
    or not all(
        shutil.which(tool)
        for tool in ("verilator", "yosys", "sby", "yosys-smtbmc", "z3")
    ),
    reason="real Clash/Verilator/SBY/Yosys/Z3 toolchain is required",
)
def test_receiver_credit_routes_to_clash_and_m35_detects_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = compile_source(
        CREDIT_TO_RV.read_text(),
        include_clash=False,
        source_unit=str(CREDIT_TO_RV),
    )
    # The shared tree may independently acquire a direct adapter-count
    # projection. Exercise the exact per-goal fallback boundary by withholding
    # only that one direct observation; the Clash route must supply it without
    # weakening the receiver assumption or mixing backend signals.
    direct = emit_sv_formal_artifact(
        compilation.ir, compilation.recursive_formal_design,
    )
    occupancy_ids = {
        item.semantic_binding_id
        for item in direct.recursive_bindings
        if item.local_semantic_id == "port:rx.occupancy"
    }
    assert occupancy_ids
    partial_direct = replace(
        direct,
        formal_observations=tuple(
            item for item in direct.formal_observations
            if item.semantic_binding_id not in occupancy_ids
        ),
    )
    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        lambda *_args, **_kwargs: partial_direct,
    )
    bundle = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, bundle)

    assert manifest.jobs
    assert {item.backend for item in manifest.jobs} == {"clash"}
    assert all(item.executable for item in manifest.jobs)
    send_capacity = next(
        item for item in compilation.formal_design.properties
        if item.kind is PropertyKind.ASSUMPTION
        and "rx.occupancy < 2" in item.expression
    )
    assert all(
        item.assumption_ids == (send_capacity.id,)
        for item in manifest.jobs
    )
    assert send_capacity.id not in {item.property_id for item in manifest.jobs}

    report = run_verification_bundle(
        bundle,
        config=VerificationRunConfig(depth=8, timeout_seconds=30),
        work_directory=tmp_path / "work",
    )
    assert report.exit_code == 0
    assertion_ids = {
        item.id for item in compilation.formal_design.properties
        if item.kind is PropertyKind.ASSERTION
    }
    assertion_results = tuple(
        item for item in report.results if item.property_id in assertion_ids
    )
    feasibility_results = tuple(
        item for item in report.results if item.property_id not in assertion_ids
    )
    assert len(assertion_results) == 5
    assert {item.status for item in assertion_results} == {"bounded_pass"}
    assert feasibility_results
    assert {item.status for item in feasibility_results} == {"witnessed"}

    conservation = next(
        item for item in compilation.formal_design.properties
        if "previous(rx.occupancy +" in item.expression
    )
    job = next(
        item for item in manifest.jobs
        if item.property_id == conservation.id
    )
    implementation = (bundle / job.source_files[0]).read_text()
    checker = (bundle / job.source_files[1]).read_text()
    needle = "rx_tx_buffer_count <= c$rx_tx_buffer_count_app_arg;"
    mutated = implementation.replace(
        needle, "rx_tx_buffer_count <= rx_tx_buffer_count;", 1,
    )
    assert mutated != implementation
    failed = run_verilog_formal(
        mutated + "\n" + checker,
        top=job.top,
        property_id=conservation.id,
        depth=8,
        systemverilog=True,
        source_origin=conservation.source_origin,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace

    payload = json.loads((bundle / "verification-ir.json").read_text())[
        "payload"
    ]
    bindings = payload["binding_sets"][0]["bindings"]
    receiver = {
        item["semantic_signal_id"]: item
        for item in bindings
        if item["semantic_signal_id"].startswith("port:rx.")
    }
    assert set(receiver) == {
        "port:rx.occupancy", "port:rx.return", "port:rx.send",
    }
    assert all(item["rtl_module"] == "CreditToRv_formal"
               for item in receiver.values())
