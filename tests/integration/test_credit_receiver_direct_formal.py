"""Direct-SV M35 closure for the existing credit-to-ready/valid adapter."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_formal_artifact
from zlang.backend.systemverilog import emitter as sv_emitter
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus, PropertyKind
from zlang.ir.formal_predicates import Constant, FormalSignedness
from zlang.ir.interfaces import ConnectionAdapter
from zlang.verification_bundle import (
    VerificationRunConfig,
    run_verification_bundle,
)
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/credit_to_rv.zhl").read_text()
FORMAL_TOOLS = all(
    shutil.which(item) for item in ("yosys", "yosys-smtbmc", "sby", "z3")
)


def _compilation():
    return compile_source(
        SOURCE, top="CreditToRv"
    )


def _instrumented(compilation):
    recursive = build_recursive_formal_design(compilation.ir)
    observations = sorted(
        recursive.bindings, key=lambda item: item.semantic_binding_id
    )
    tokens = {
        item.semantic_binding_id: f"zlang_formal_obs_{index}"
        for index, item in enumerate(observations)
    }
    return sv_emitter._instrument_direct_formal_module(
        compilation.ir, recursive, tokens
    )


def test_receiver_credit_count_is_a_typed_formal_only_adapter_projection(
    tmp_path: Path,
) -> None:
    compilation = _compilation()
    recursive = build_recursive_formal_design(compilation.ir)
    production_before = emit_artifact(
        compilation.ir, recursive_design=recursive
    )
    formal = emit_formal_artifact(compilation.ir, recursive)
    production_after = emit_artifact(
        compilation.ir, recursive_design=recursive
    )

    assert production_after == production_before
    assert "ZLangRvFifoFormal" not in production_before.text
    assert "formal_count" not in production_before.text
    occupancy = next(
        item for item in formal.recursive_bindings
        if item.local_semantic_id == "port:rx.occupancy"
    )
    assert occupancy.width == 2
    assert occupancy.canonical_type == "uint<2>"
    assert occupancy.physical_available
    assert occupancy.formal_observation_token is not None
    assert formal.text.count("output logic [1:0] formal_count") == 1
    assert formal.text.count(".formal_count(") == 1
    assert (
        f"assign {occupancy.formal_observation_token} = "
        "zlang_formal_adapter_count_"
    ) in formal.text
    assert ".count" not in formal.text
    restored = BackendArtifact.from_json(formal.to_json())
    assert restored.to_json() == formal.to_json()
    assert restored.recursive_bindings == formal.recursive_bindings
    assert restored.formal_observations == formal.formal_observations

    if shutil.which("verilator"):
        rtl = tmp_path / "credit_to_rv_formal.sv"
        rtl.write_text(formal.text)
        completed = subprocess.run(
            (
                "verilator", "--lint-only", "-Wall",
                "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", str(rtl),
            ),
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout


def test_malformed_receiver_credit_count_descriptors_and_links_fail_closed() -> None:
    compilation = _compilation()
    formal_module, _, buffer_counts, adapter_counts = _instrumented(compilation)
    assert not buffer_counts
    assert len(adapter_counts) == 1
    projection = adapter_counts[0]

    malformed = (
        replace(projection, width=projection.width + 1),
        replace(projection, depth=projection.depth + 1),
        replace(projection, source_port="other"),
        replace(projection, destination_port="other"),
        replace(projection, adapter=ConnectionAdapter.READY_VALID_TO_CREDIT),
    )
    for item in malformed:
        with pytest.raises(
            sv_emitter.SystemVerilogEmissionError,
            match="projection disagrees with the typed",
        ):
            sv_emitter.emit(
                formal_module, _formal_adapter_counts=(item,)
            )
    with pytest.raises(
        sv_emitter.SystemVerilogEmissionError,
        match="duplicate formal count projections",
    ):
        sv_emitter.emit(
            formal_module,
            _formal_adapter_counts=(projection, projection),
        )
    broken_link = replace(
        formal_module,
        connections=(replace(
            formal_module.connections[0],
            buffer_depth=formal_module.connections[0].buffer_depth + 1,
        ),),
    )
    with pytest.raises(
        sv_emitter.SystemVerilogEmissionError,
        match="projection disagrees with the typed",
    ):
        sv_emitter.emit(
            broken_link, _formal_adapter_counts=(projection,)
        )
    with pytest.raises(
        sv_emitter.SystemVerilogEmissionError,
        match="closed clocked connection",
    ):
        sv_emitter.emit(formal_module)


@pytest.mark.skipif(
    not FORMAL_TOOLS,
    reason="Yosys/yosys-smtbmc/SBY/Z3 are required",
)
def test_receiver_credit_five_properties_prove_and_count_mutation_fails() -> None:
    compilation = _compilation()
    artifact = emit_formal_artifact(
        compilation.ir, build_recursive_formal_design(compilation.ir)
    )
    connected = connect_formal_design(compilation.formal_design, artifact)
    credit = tuple(
        item for item in connected.properties
        if item.generated_from.startswith("credit:rx")
    )
    assert len(credit) == 5
    assert sum(item.kind is PropertyKind.ASSUMPTION for item in credit) == 1
    assert all(item.non_executable_reason is None for item in credit)

    harness = emit_harness(connected, depth=8)
    passed = run_verilog_formal(
        harness,
        top="CreditToRv__m35_formal",
        property_id="m35.connected.credit-receiver",
        depth=8,
        systemverilog=True,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    needle = "  assign formal_count = count;"
    assert harness.count(needle) == 1
    mutated = harness.replace(
        needle, "  assign formal_count = count + 1'b1;", 1
    )
    failed = run_verilog_formal(
        mutated,
        top="CreditToRv__m35_formal",
        property_id="m35.connected.credit-receiver-count-mutation",
        depth=8,
        systemverilog=True,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.cycle is not None
    assert failed.counterexample.raw_trace


@pytest.mark.skipif(
    not FORMAL_TOOLS,
    reason="Yosys/yosys-smtbmc/SBY/Z3 are required",
)
def test_root_automatic_credit_assumption_has_one_unassumed_feasibility_cover(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "credit_bundle"
    manifest = publish_compilation_verification_bundle(
        _compilation(), directory
    )
    covers = tuple(item for item in manifest.jobs if item.kind == "cover")
    safety = tuple(item for item in manifest.jobs if item.kind == "safety")
    assert len(covers) == 1
    assert safety
    cover = covers[0]
    assumption_ids = safety[0].assumption_ids
    assert len(assumption_ids) == 1
    assert all(item.assumption_ids == assumption_ids for item in safety)
    # Bundle metadata records declared scope membership, as for recursive
    # feasibility covers.  The executable checker itself must be unassumed.
    assert cover.assumption_ids == assumption_ids
    cover_harness = next(
        directory / item
        for item in cover.source_files
        if item.startswith("harness/")
    ).read_text()
    assert "cover (" in cover_harness
    assert "assume (" not in cover_harness

    payload = json.loads((directory / "verification-ir.json").read_text())[
        "payload"
    ]
    assert set(payload["vacuity_dependencies"].values()) == {
        cover.property_id
    }
    report = run_verification_bundle(
        directory,
        config=VerificationRunConfig(
            depth=8, timeout_seconds=30, jobs=2
        ),
    )
    by_id = {item.property_id: item for item in report.results}
    assert report.exit_code == 0
    assert by_id[cover.property_id].status == "witnessed"
    assert all(by_id[item.property_id].status == "bounded_pass" for item in safety)


@pytest.mark.skipif(
    not FORMAL_TOOLS,
    reason="Yosys/yosys-smtbmc/SBY/Z3 are required",
)
def test_unreachable_root_automatic_assumption_marks_safety_vacuous(
    tmp_path: Path,
) -> None:
    compilation = _compilation()
    properties = tuple(
        replace(
            item,
            expression="0",
            predicate=Constant(0, 1, FormalSignedness.BIT),
            relevant_signals=(),
        )
        if item.kind is PropertyKind.ASSUMPTION
        and item.generated_from == "credit:rx"
        else item
        for item in compilation.formal_design.properties
    )
    unreachable = replace(
        compilation,
        formal_design=replace(
            compilation.formal_design, properties=properties
        ),
    )
    directory = tmp_path / "unreachable_bundle"
    manifest = publish_compilation_verification_bundle(
        unreachable, directory
    )
    report = run_verification_bundle(
        directory,
        config=VerificationRunConfig(
            depth=6, timeout_seconds=30, jobs=2
        ),
    )
    covers = tuple(item for item in report.results if item.kind == "cover")
    safety = tuple(item for item in report.results if item.kind == "safety")
    assert len(covers) == 1
    assert covers[0].status == "bounded_unreached"
    assert safety
    assert all(item.status == "unknown" for item in safety)
    assert all("vacuous" in (item.reason or "") for item in safety)
    assert report.exit_code == 2
    assert len(tuple(item for item in manifest.jobs if item.kind == "cover")) == 1
