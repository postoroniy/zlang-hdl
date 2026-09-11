"""Exact per-goal clock/reset applicability for frozen M35 execution."""

from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from zlang.backend.manifest import BackendArtifact, PhysicalDomainManifest
from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import (
    build_formal_design,
    build_recursive_formal_design,
    connect_formal_design,
)
from zlang.ir.cdc import (
    PowerUpPolicy,
    ResetMode,
    clock_domain_contract_identity,
)
from zlang.ir.formal_planning import FormalExecutionPlan
from zlang.verification_bundle import (
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle,
)
from zlang.verification_publication import publish_compilation_verification_bundle


MULTI_DOMAIN_GOALS = """
module DomainGoals {
    clock a
    reset ra @a
    clock b
    reset rb @b
    in x : bit @a
    out y : bit @a
    y = x
    assert on_a @a { y == x }
    cover on_b @b { 1 }
}
"""


MULTI_DOMAIN_STATE_GOAL = """
module DomainStateGoal {
    clock a
    reset ra @a
    clock b
    reset rb @b
    out y : u8 @b
    reg count : u8 = 0 @b
    count <- truncate<8>(count + 1)
    y = count
    assert count_is_representable @b { count <= 255 }
}
"""


COUNTER = """
module DomainCounter {
    clock clk
    reset rst
    out y : u8
    reg count : u8 = 0
    count <- truncate<8>(count + 1)
    y = count
}
"""


ASYNC_COUNTER = """
module AsyncDomainCounter {
    clock clk
    async reset arst @clk
    out y : u8
    reg count : u8 = 0
    count <- truncate<8>(count + 1)
    y = count
}
"""


def test_unrelated_async_domain_does_not_poison_legacy_goal() -> None:
    module = compile_source(MULTI_DOMAIN_GOALS).ir
    legacy, second = module.clock_domains
    mixed = replace(
        module,
        clock_domains=(
            legacy,
            replace(second, reset_mode=ResetMode.ASYNCHRONOUS),
        ),
    )

    design = build_formal_design(mixed)
    assert design.non_executable_reason is None
    assertion = next(item for item in design.properties if item.clock == "a")
    cover = next(item for item in design.covers if item.clock == "b")
    assert assertion.non_executable_reason is None
    assert "asynchronous reset goal in a multi-domain module" in (
        cover.non_executable_reason or ""
    )


def test_power_up_reset_remains_fail_closed_for_every_goal() -> None:
    module = compile_source(COUNTER).ir
    guarded = replace(
        module,
        clock_domains=(replace(
            module.clock_domains[0],
            power_up=PowerUpPolicy.RESET,
        ),),
    )

    design = build_formal_design(guarded)
    assert "power-up reset contract" in (design.non_executable_reason or "")
    assert design.properties
    assert all(
        "power-up reset contract" in (item.non_executable_reason or "")
        for item in design.properties
    )


def test_connector_checks_only_the_goal_physical_domain() -> None:
    compiled = compile_source(COUNTER)
    artifact = emit_formal_artifact(
        compiled.ir,
        build_recursive_formal_design(compiled.ir),
    )
    legacy = compiled.ir.clock_domains[0]
    unrelated_async = replace(
        legacy,
        clock="aux_clk",
        reset="aux_rst",
        reset_mode=ResetMode.ASYNCHRONOUS,
    )
    with_domains = replace(
        artifact,
        physical_domains=(
            PhysicalDomainManifest.publish(
                legacy,
                rtl_module=artifact.module,
                rtl_clock_path="clk",
                rtl_reset_path="rst",
            ),
            PhysicalDomainManifest.publish(
                unrelated_async,
                rtl_module=artifact.module,
                rtl_clock_path="aux_clk",
                rtl_reset_path="aux_rst",
            ),
        ),
    )

    connected = connect_formal_design(compiled.formal_design, with_domains)
    assert connected.connected_backend == "direct_systemverilog"
    assert connected.non_executable_reason is None
    assert all(
        item.non_executable_reason is None for item in connected.properties
    )


def test_connector_preserves_whole_design_guard_when_only_goal_is_async() -> None:
    compiled = compile_source(COUNTER)
    artifact = emit_formal_artifact(
        compiled.ir,
        build_recursive_formal_design(compiled.ir),
    )
    async_root = replace(
        compiled.ir.clock_domains[0],
        reset_mode=ResetMode.ASYNCHRONOUS,
    )
    async_artifact = replace(
        artifact,
        physical_domains=(PhysicalDomainManifest.publish(
            async_root,
            rtl_module=artifact.module,
            rtl_clock_path="clk",
            rtl_reset_path="rst",
        ),),
    )

    connected = connect_formal_design(compiled.formal_design, async_artifact)
    assert connected.connected_backend is None
    assert connected.connected_artifact_hash is None
    assert "unsupported or unresolved physical domains" in (
        connected.non_executable_reason or ""
    )
    assert connected.properties
    assert all(
        "source and backend physical reset contracts do not match"
        in (item.non_executable_reason or "")
        for item in connected.properties
    )


def test_bundle_connects_each_supported_domain_independently(
    tmp_path: Path,
) -> None:
    compiled = compile_source(MULTI_DOMAIN_GOALS)
    bundle_path = tmp_path / "bundle"
    publish_compilation_verification_bundle(compiled, bundle_path)
    bundle = load_verification_bundle(bundle_path)

    assert len(bundle.manifest.jobs) == 2
    assert all(item.executable for item in bundle.manifest.jobs)
    assert {
        (item.clock_domain, item.reset_domain)
        for item in bundle.manifest.jobs
    } == {("a", "ra"), ("b", "rb")}
    plan = FormalExecutionPlan.from_data(
        bundle.verification_ir["payload"]["execution_plan"]
    )
    plans = {item.property_identity: item for item in plan.goals}
    domains = {
        (item.clock, item.reset): item for item in compiled.ir.clock_domains
    }
    for job in bundle.manifest.jobs:
        domain = domains[(job.clock_domain, job.reset_domain)]
        expected_identity = clock_domain_contract_identity(domain)
        assert job.clock_domain_contract == domain
        assert job.physical_domain_identity == expected_identity
        assert plans[job.property_id].clock_domain_contract == domain
        assert plans[job.property_id].physical_domain_identity == expected_identity
    routes = {item.route for item in bundle.manifest.jobs}
    assert None not in routes and len(routes) == 2

    binding_sets = bundle.verification_ir["payload"]["binding_sets"]
    clocks_and_resets = {
        item["route"]: {
            binding["semantic_signal_id"]: binding["rtl_name"]
            for binding in item["bindings"]
            if binding["semantic_signal_id"] in {"clock", "reset"}
        }
        for item in binding_sets
    }
    assert set(clocks_and_resets) == routes
    assert set(map(tuple, (
        sorted(item.items()) for item in clocks_and_resets.values()
    ))) == {
        (("clock", "a"), ("reset", "ra")),
        (("clock", "b"), ("reset", "rb")),
    }


def test_multidomain_exact_goal_routes_survive_formal_disk_cache(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "formal-cache"
    compiled = compile_source(
        MULTI_DOMAIN_GOALS,
        formal_cache=cache,
    )

    first_path = tmp_path / "first"
    publish_compilation_verification_bundle(compiled, first_path)
    first = load_verification_bundle(first_path)
    assert len(first.manifest.jobs) == 2
    assert all(item.executable for item in first.manifest.jobs)
    assert compiled.formal_artifact_provider.stats.publications >= 1

    compiled.formal_artifact_provider.clear_memory()
    second_path = tmp_path / "second"
    publish_compilation_verification_bundle(compiled, second_path)
    second = load_verification_bundle(second_path)
    assert len(second.manifest.jobs) == 2
    assert all(item.executable for item in second.manifest.jobs)
    assert compiled.formal_artifact_provider.stats.disk_hits >= 1


def test_source_goal_keeps_multidomain_state_binding_and_fails_closed(
    tmp_path: Path,
) -> None:
    compiled = compile_source(MULTI_DOMAIN_STATE_GOAL)
    source_goal = next(
        item for item in compiled.formal_design.properties
        if item.generated_from.startswith("verification-assert:")
    )
    assert source_goal.clock == "b"
    assert source_goal.reset_condition == "rb"
    assert "register:count" in source_goal.relevant_signals
    assert any(
        item.semantic_signal_id == "register:count"
        for item in compiled.formal_design.bindings
    )

    bundle_path = tmp_path / "state-bundle"
    publish_compilation_verification_bundle(compiled, bundle_path)
    bundle = load_verification_bundle(bundle_path)
    job = next(
        item for item in bundle.manifest.jobs
        if item.property_id == source_goal.id
    )
    assert not job.executable
    assert "backend_unavailable" in (job.reason or "")
    assert "no signal binding for 'register:count'" not in (job.reason or "")
    assert (job.clock_domain, job.reset_domain) == ("b", "rb")
    expected_domain = next(
        item for item in compiled.ir.clock_domains
        if (item.clock, item.reset) == ("b", "rb")
    )
    assert job.clock_domain_contract == expected_domain
    # No backend artifact was emitted for this unsupported mixed state shape,
    # so the exact typed contract remains available while the manifest link is
    # truthfully absent.
    assert job.physical_domain_identity is None


def test_skipped_async_goal_retains_available_artifact_domain_identity(
    tmp_path: Path,
) -> None:
    compiled = compile_source(ASYNC_COUNTER)
    source_goal = next(
        item for item in compiled.formal_design.properties
        if item.kind.value == "assertion"
    )
    guarded_design = replace(
        compiled.formal_design,
        properties=tuple(
            replace(item, non_executable_reason="forced observation gap")
            if item.id == source_goal.id else item
            for item in compiled.formal_design.properties
        ),
    )
    compiled = replace(compiled, formal_design=guarded_design)

    bundle_path = tmp_path / "skipped-async"
    publish_compilation_verification_bundle(compiled, bundle_path)
    bundle = load_verification_bundle(bundle_path)
    job = next(
        item for item in bundle.manifest.jobs
        if item.property_id == source_goal.id
    )
    plan = FormalExecutionPlan.from_data(
        bundle.verification_ir["payload"]["execution_plan"]
    )
    goal = next(
        item for item in plan.goals if item.property_identity == source_goal.id
    )
    domain = compiled.ir.clock_domains[0]
    expected_identity = clock_domain_contract_identity(domain)

    assert not job.executable
    assert job.clock_domain_contract == goal.clock_domain_contract == domain
    assert job.physical_domain_identity == expected_identity
    assert goal.physical_domain_identity == expected_identity


TOOLS = ("yosys", "sby", "yosys-smtbmc", "z3")


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_multi_domain_constant_cover_executes_against_closed_formal_top(
    tmp_path: Path,
) -> None:
    compiled = compile_source(MULTI_DOMAIN_GOALS)
    artifact = emit_formal_artifact(
        compiled.ir,
        compiled.recursive_formal_design,
        selected_ir_identity=compiled.selected_ir_identity,
    )
    assert artifact.module == "DomainGoals__formal"
    assert {item.rtl_module for item in artifact.bindings} == {
        "DomainGoals__formal"
    }
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.module == artifact.module
    assert restored.bindings == artifact.bindings

    bundle_path = tmp_path / "bundle"
    publish_compilation_verification_bundle(compiled, bundle_path)
    bundle = load_verification_bundle(bundle_path)
    cover = next(item for item in bundle.manifest.jobs if item.kind == "cover")
    checker = bundle.read_bytes(cover.source_files[1]).decode("utf-8")
    assert "DomainGoals__formal dut" in checker
    assert "DomainGoals dut" not in checker

    report = run_verification_bundle(
        bundle,
        config=VerificationRunConfig(depth=4),
        work_directory=tmp_path / "work",
    )
    by_kind = {item.kind: item for item in report.results}
    assert by_kind["safety"].status == "bounded_pass"
    assert by_kind["cover"].status == "witnessed"
