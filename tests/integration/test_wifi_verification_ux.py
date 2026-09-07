"""First-class verification UX against the IEEE 802.11a real-design path."""

from __future__ import annotations

from dataclasses import replace
from functools import cache
import json
from pathlib import Path
import shutil
import tempfile

import pytest

from zlang.backend.systemverilog import (
    emit_artifact as emit_sv_artifact,
    emit_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_cover_harness,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalError, FormalStatus, PropertyKind
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import VerificationMonitor, simulate_cycles
from zlang.toolchain import lint_with_verilator
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)
from zlang.workspace import load_project_workspace


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "verification" / "wifi_verification.zhl"
PROJECT_SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "mapper.zhl"
)
SOURCE_UNIT = "tests/fixtures/verification/wifi_verification.zhl"
IFFT_TOP = "IeeeIFFTBoundaryVerification"
MAPPER_TOP = "IeeeMapper64Verification"
MAPPER_STATE_TOP = "IeeeMapperRateStateVerification"
MAPPER_DECODE_TOP = "VerificationMapperBoundaryOnly"
FORMAL_TOOLS = ("yosys", "sby", "yosys-smtbmc", "z3")
MINIMAL_HIERARCHICAL_RV = """
module RvIdentity {
    in input : rv<u8>
    out output : rv<u8>
    output.payload = input.payload
    output.valid = input.valid
    input.ready = output.ready
}

module RvHierarchy {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    child : RvIdentity
    input -> child.input
    child.output -> output
}
"""


@cache
def _workspace():
    workspace = load_project_workspace(PROJECT_SOURCE.resolve())
    assert workspace is not None
    return workspace


@cache
def _compile(top: str, source: str | None = None):
    workspace = _workspace()
    return compile_source(
        FIXTURE.read_text() if source is None else source,
        top=top,
        include_clash=False,
        source_unit=SOURCE_UNIT,
        module_resolver=workspace.resolver,
        dependency_closure=workspace.dependency_closure,
    )


def _connected(top: str):
    compilation = _compile(top)
    recursive = build_recursive_formal_design(compilation.ir)
    artifact = emit_formal_artifact(compilation.ir, recursive)
    return artifact, connect_formal_design(compilation.formal_design, artifact)


@cache
def _minimal_connected():
    compilation = compile_source(
        MINIMAL_HIERARCHICAL_RV,
        top="RvHierarchy",
        include_clash=False,
    )
    artifact = emit_formal_artifact(
        compilation.ir,
        build_recursive_formal_design(compilation.ir),
    )
    return connect_formal_design(compilation.formal_design, artifact)


@cache
def _published_mapper_bundle():
    temporary = tempfile.TemporaryDirectory()
    directory = Path(temporary.name) / "bundle"
    manifest = publish_compilation_verification_bundle(
        _compile(MAPPER_TOP),
        directory,
    )
    return temporary, directory, manifest


@cache
def _published_ifft_bundle():
    temporary = tempfile.TemporaryDirectory()
    directory = Path(temporary.name) / "bundle"
    manifest = publish_compilation_verification_bundle(
        _compile(IFFT_TOP),
        directory,
    )
    return temporary, directory, manifest


@cache
def _published_mapper_decode_bundle():
    source = FIXTURE.read_text() + """

module VerificationMapperBoundaryOnly {
    clock clk reset rst
    in input : rv<VerificationRawInterleavedWord>
    out output : rv<WifiInterleavedWord>
    decode : VerificationMapperInputBoundary
    input -> decode.input
    decode.output -> output
    contract boundary @ clk {
        assert transfer_identity {
            output.transfer == (output.valid & output.ready)
        }
    }
}
"""
    temporary = tempfile.TemporaryDirectory()
    directory = Path(temporary.name) / "bundle"
    manifest = publish_compilation_verification_bundle(
        _compile(MAPPER_DECODE_TOP, source),
        directory,
    )
    return temporary, directory, manifest


def _mapper_cycle(*, valid: int = 0, ready: int = 1) -> dict[str, object]:
    return {
        "input": {
            "payload": {
                "data": 0,
                "meta": {
                    "rate": 1,
                    "valid_bytes": 3,
                    "tail_mask": 0,
                    "symbol_first": 1,
                    "symbol_last": 1,
                },
                "first": 1,
                "last": 1,
            },
            "valid": valid,
        },
        "output": {"ready": ready},
    }


def test_clocked_ifft_boundary_derives_directional_m35_rv_contracts() -> None:
    compilation = _compile(IFFT_TOP)
    assert [child.name for child in compilation.ir.children] == [
        "IeeeIFFTFramedInputBoundary",
        "IeeeIFFTFramedOutputBoundary",
    ]
    assert restore(
        lower(compilation.ir, stage=OptimizationStage.HIGH_LEVEL)
    ) == compilation.ir

    properties = compilation.formal_design.properties
    assert [(item.kind, item.generated_from) for item in properties] == [
        (PropertyKind.ASSUMPTION, "ready_valid:input"),
        (PropertyKind.ASSERTION, "ready_valid:output"),
    ]
    artifact, connected = _connected(IFFT_TOP)
    assert artifact.formal_observations
    assert all(item.non_executable_reason is None for item in connected.properties)

    # Aggregate public-port and endpoint identities intentionally alias the
    # same physical leaves.  The harness declares/connects each leaf once.
    harness = emit_harness(connected, depth=6)
    harness_only = harness[harness.index(f"module {IFFT_TOP}__m35_formal") :]
    assert harness_only.count(".input_valid(input_valid)") == 1
    assert harness_only.count(".input_ready(input_ready)") == 1
    assert harness_only.count(".output_valid(output_valid)") == 1
    assert harness_only.count(".output_ready(output_ready)") == 1


def test_non_wifi_hierarchical_rv_uses_one_canonical_physical_harness_port() -> None:
    connected = _minimal_connected()
    input_valid = tuple(
        item for item in connected.dut_ports if item.rtl_name == "input_valid"
    )
    assert len(input_valid) == 1
    assert input_valid[0].semantic_signal_id == "port:input.valid"
    assert len({item.rtl_name for item in connected.dut_ports}) == len(
        connected.dut_ports
    )

    harness = emit_harness(connected, depth=4)
    harness_only = harness[harness.index("module RvHierarchy__m35_formal") :]
    assert harness_only.count(".input_valid(input_valid)") == 1
    assert harness_only.count(".input_ready(input_ready)") == 1
    assert harness_only.count(".output_valid(output_valid)") == 1
    assert harness_only.count(".output_ready(output_ready)") == 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"width": 2}, "width"),
        ({"direction": "output"}, "direction"),
        ({"clock_domain": "other"}, "other"),
        ({"rtl_module": "OtherPhysicalModule"}, "OtherPhysicalModule"),
    ],
)
def test_formal_harness_rejects_incompatible_physical_port_aliases(
    change: dict[str, object], expected: str
) -> None:
    connected = _minimal_connected()
    original = next(
        item for item in connected.dut_ports if item.rtl_name == "input_valid"
    )
    conflicting = replace(
        original,
        semantic_signal_id="endpoint:conflicting.input.valid",
        **change,
    )
    malformed = replace(connected, dut_ports=(*connected.dut_ports, conflicting))

    with pytest.raises(
        FormalError,
        match="reuses RTL port token 'input_valid'.*incompatible",
    ) as caught:
        emit_harness(malformed, depth=4)
    assert expected in str(caught.value)


def test_mapper_contract_lowers_and_simulator_records_first_frame_cover() -> None:
    compilation = _compile(MAPPER_TOP)
    assert [child.name for child in compilation.ir.children] == [
        "VerificationMapperInputBoundary",
        "IeeeMapper64",
        "VerificationMapperFrameSink",
    ]
    assert restore(
        lower(compilation.ir, stage=OptimizationStage.HIGH_LEVEL)
    ) == compilation.ir

    (scope,) = compilation.ir.verification_scopes
    assert scope.name == "mapper_behavior"
    assert [(goal.kind.value, goal.name) for goal in scope.goals] == [
        ("assert", "accepted_transfer_legal"),
        ("cover", "emitted_frame"),
    ]
    contract_properties = [
        item
        for item in compilation.formal_design.properties
        if item.generated_from.startswith("verification-")
    ]
    assert len(contract_properties) == 1
    assert contract_properties[0].generated_from == (
        "verification-assert:mapper_behavior:accepted_transfer_legal"
    )
    (cover,) = compilation.formal_design.covers
    assert cover.expression == "(port:output.valid && port:output.ready)"

    stimuli = [
        _mapper_cycle(),
        _mapper_cycle(valid=1),
        _mapper_cycle(),
        _mapper_cycle(),
    ]
    trace = simulate_cycles(
        compilation.ir,
        stimuli,
        reset=(True, False, False, False),
    )
    monitor = VerificationMonitor(
        compilation.ir.verification_scopes,
        compilation.ir.functions,
    )
    for cycle, (stimulus, result) in enumerate(zip(stimuli, trace, strict=True)):
        monitor.sample(
            {
                "input.valid": stimulus["input"]["valid"],
                "input.ready": result["input"]["ready"],
                "output.valid": result["output"]["valid"],
                "output.ready": stimulus["output"]["ready"],
            },
            cycle,
            reset_active=cycle == 0,
        )
    (witness,) = monitor.cover_witnesses
    assert witness.scope_name == "mapper_behavior"
    assert witness.goal_name == "emitted_frame"
    assert witness.cycle == 2

    artifact, connected = _connected(MAPPER_TOP)
    assert artifact.formal_observations
    assert all(item.non_executable_reason is None for item in connected.properties)
    assert connected.covers[0].non_executable_reason is None
    cover_harness = emit_cover_harness(
        connected,
        cover_id=connected.covers[0].id,
        depth=4,
    )
    assert "cover (" in cover_harness
    assert connected.covers[0].id in cover_harness


def test_mapper_verification_overlay_does_not_change_production_rtl() -> None:
    source = FIXTURE.read_text()
    contract = """    contract mapper_behavior @ clk {
        assert accepted_transfer_legal {
            input.transfer == (input.valid & input.ready)
        }

        cover emitted_frame {
            output.transfer
        }
    }
"""
    assert source.count(contract) == 1
    without_verification = source.replace(contract, "")

    with_artifact = emit_sv_artifact(_compile(MAPPER_TOP).ir)
    without_artifact = emit_sv_artifact(
        _compile(MAPPER_TOP, without_verification).ir
    )
    assert with_artifact.text == without_artifact.text
    assert with_artifact.artifact_hash == without_artifact.artifact_hash


def test_mapper_recursive_requirements_are_owned_by_the_physical_source_path() -> None:
    _temporary, directory, manifest = _published_mapper_bundle()
    payload = json.loads(
        (directory / "verification-ir.json").read_text(encoding="utf-8")
    )["payload"]
    decode_path = (MAPPER_TOP, "decode")
    mapper_path = (MAPPER_TOP, "mapper")
    decode_safety = tuple(
        item for item in manifest.jobs
        if item.physical_instance_path == decode_path and item.kind == "safety"
    )
    assert len(decode_safety) == 1
    assert decode_safety[0].executable
    assert decode_safety[0].backend == "direct_systemverilog"
    assert len(tuple(
        item for item in manifest.jobs
        if len(item.physical_instance_path) > 1 and item.kind == "cover"
    )) == 1

    mapper_safety = tuple(
        item for item in manifest.jobs
        if item.physical_instance_path == mapper_path and item.kind == "safety"
    )
    assert mapper_safety
    assert all(item.executable for item in mapper_safety)
    assert all(item.backend == "direct_systemverilog" for item in mapper_safety)
    assert all(len(item.assumption_ids) == 1 for item in mapper_safety)
    plans = {
        item["property_identity"]: item
        for item in payload["execution_plan"]["goals"]
    }
    assert all(
        plans[item.property_id]["route"] is not None
        and plans[item.property_id]["skip_reason"] is None
        for item in mapper_safety
    )
    # Every executable route names one backend artifact.  Recursive sink goals
    # may use another exact route, but one harness never mixes their bindings.
    binding_sets = {
        item["route"]: item for item in payload["binding_sets"]
    }
    assert {
        binding_sets[decode_safety[0].route]["backend"]
    } == {decode_safety[0].backend}
    root_feasibility = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path == (MAPPER_TOP,)
        and item.kind == "cover"
        and ".requirements_feasible." in item.property_id
    )
    assert len(root_feasibility) == 1
    vacuity_dependencies = payload["vacuity_dependencies"]
    root_safety = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path == (MAPPER_TOP,)
        and item.kind == "safety"
    )
    assert root_safety
    assert all(
        vacuity_dependencies[item.property_id]
        == root_feasibility[0].property_id
        for item in root_safety
    )
    assert len({item.property_id for item in manifest.jobs}) == len(manifest.jobs)
    assert all(item.executable for item in manifest.jobs)


def test_ifft_root_ready_valid_requirement_is_published_as_environment_owned() -> None:
    _temporary, directory, manifest = _published_ifft_bundle()
    payload = json.loads(
        (directory / "verification-ir.json").read_text(encoding="utf-8")
    )["payload"]
    (root_assumption,) = tuple(
        item for item in _compile(IFFT_TOP).formal_design.properties
        if item.kind is PropertyKind.ASSUMPTION
    )
    module_scope = next(
        item for item in payload["scopes"] if item["name"] == "$module"
    )
    assert [item["id"] for item in module_scope["requirements"]] == [
        root_assumption.id
    ]
    root_jobs = tuple(
        item for item in manifest.jobs
        if item.physical_instance_path == (IFFT_TOP,)
    )
    assert root_jobs
    assert all(item.assumption_ids == (root_assumption.id,) for item in root_jobs)
    assert all(item.executable for item in root_jobs)
    root_feasibility = tuple(
        item
        for item in root_jobs
        if item.kind == "cover" and ".requirements_feasible." in item.property_id
    )
    root_safety = tuple(item for item in root_jobs if item.kind == "safety")
    assert len(root_feasibility) == 1
    assert len(root_safety) == 1
    assert payload["vacuity_dependencies"][root_safety[0].property_id] == (
        root_feasibility[0].property_id
    )
    assert len({item.property_id for item in manifest.jobs}) == len(manifest.jobs)
    assert all(item.executable for item in manifest.jobs)


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in FORMAL_TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_mapper_upstream_stall_mutation_fails_source_endpoint_guarantee() -> None:
    _temporary, directory, manifest = _published_mapper_decode_bundle()
    job = next(
        item for item in manifest.jobs
        if item.physical_instance_path == (MAPPER_DECODE_TOP, "decode")
        and item.kind == "safety"
    )
    assert job.executable
    implementation_path = next(
        directory / item
        for item in job.source_files
        if item.startswith("implementation/") and item.endswith(".sv")
    )
    harness_path = next(
        directory / item
        for item in job.source_files
        if item.startswith("harness/")
    )
    implementation = implementation_path.read_text(encoding="utf-8")
    harness = harness_path.read_text(encoding="utf-8")
    source = implementation + "\n" + harness
    passed = run_verilog_formal(
        source,
        top=job.top,
        property_id=job.property_id,
        depth=6,
        systemverilog=True,
        source_origin=job.source_origin,
        timeout_seconds=60,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    component_start = implementation.index(
        "module VerificationMapperInputBoundary_s"
    )
    component_end = implementation.index("endmodule", component_start)
    component = implementation[component_start:component_end]
    assignment = next(
        line for line in component.splitlines()
        if line.strip().startswith("assign zlang_output_payload = ")
    )
    mutation = assignment[:-1] + " ^ {81{zlang_output_ready}};"
    mutated_implementation = (
        implementation[:component_start]
        + component.replace(assignment, mutation, 1)
        + implementation[component_end:]
    )
    assert mutated_implementation != implementation
    failed = run_verilog_formal(
        mutated_implementation + "\n" + harness,
        top=job.top,
        property_id=job.property_id,
        depth=6,
        systemverilog=True,
        source_origin=job.source_origin,
        timeout_seconds=60,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.source_origin == job.source_origin
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace


def test_wifi_verification_wrappers_emit_deterministic_strict_sv(
    tmp_path: Path,
) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    for top in (IFFT_TOP, MAPPER_TOP):
        compilation = _compile(top)
        first = emit_sv_artifact(compilation.ir)
        second = emit_sv_artifact(compilation.ir)
        assert first.text == second.text
        assert first.artifact_hash == second.artifact_hash
        rtl = tmp_path / f"{top}.sv"
        rtl.write_text(first.text)
        lint_with_verilator((rtl,), top)


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in FORMAL_TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_real_ifft_boundary_safety_and_payload_stability_mutation() -> None:
    _, connected = _connected(IFFT_TOP)
    harness = emit_harness(connected, depth=6)
    top = f"{IFFT_TOP}__m35_formal"
    passed = run_verilog_formal(
        harness,
        top=top,
        property_id="wifi.ifft.boundary",
        depth=6,
        systemverilog=True,
        timeout_seconds=60,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    component_start = harness.index("module IeeeIFFTFramedOutputBoundary_s")
    component_end = harness.index("endmodule", component_start)
    component = harness[component_start:component_end]
    assignment = next(
        line
        for line in component.splitlines()
        if line.strip().startswith("assign zlang_output_payload = ")
    )
    mutation = assignment[:-1] + " ^ {51{zlang_output_ready}};"
    mutated = (
        harness[:component_start]
        + component.replace(assignment, mutation, 1)
        + harness[component_end:]
    )
    assert mutated != harness
    failed = run_verilog_formal(
        mutated,
        top=top,
        property_id="wifi.ifft.boundary.payload-stability-mutation",
        depth=6,
        systemverilog=True,
        timeout_seconds=60,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in FORMAL_TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_real_mapper_state_mutation_has_goal_source_attribution() -> None:
    compilation = _compile(MAPPER_STATE_TOP)
    (scope,) = compilation.ir.verification_scopes
    goal = next(item for item in scope.goals if item.name == "current_rate_legal")
    assert goal.source_origin is not None
    assert goal.source_origin.source_unit == SOURCE_UNIT

    _, connected = _connected(MAPPER_STATE_TOP)
    prop = next(
        item
        for item in connected.properties
        if item.generated_from
        == "verification-ensure:mapper_rate_state:current_rate_legal"
    )
    assert prop.source_origin == goal.source_origin
    harness = emit_harness(connected, depth=5)
    top = f"{MAPPER_STATE_TOP}__m35_formal"

    passed = run_verilog_formal(
        harness,
        top=top,
        property_id=prop.id,
        depth=5,
        systemverilog=True,
        source_origin=prop.source_origin,
        timeout_seconds=60,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS
    assert passed.source_origin == goal.source_origin

    state_update = next(
        line
        for line in harness.splitlines()
        if line.lstrip().startswith("if (") and "current_rate <=" in line
    )
    assignment_start = state_update.rindex("current_rate <=")
    mutated = harness.replace(
        state_update,
        state_update[:assignment_start] + "current_rate <= 3'b111;",
        1,
    )
    assert mutated != harness

    failed = run_verilog_formal(
        mutated,
        top=top,
        property_id=prop.id,
        depth=5,
        systemverilog=True,
        source_origin=prop.source_origin,
        timeout_seconds=60,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.source_origin == goal.source_origin
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace
