"""Current-schema verification bundle fixtures shared by bundle tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from zlang.candidate_sites import CandidateSiteLedger
from zlang.common import stable_digest
from zlang.formal_exploration import FormalPolicy
from zlang.formal_orchestration import CompilerFormalExecutionPlan
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.formal_planning import (
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalExecutionPlan,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.verification_bundle import VerificationJob


@dataclass(frozen=True)
class CurrentVerificationFixture:
    payload: dict[str, object]
    jobs: tuple[VerificationJob, ...]


def current_verification_fixture(
    *,
    hardware_identity: str,
    jobs: tuple[VerificationJob, ...],
    source_identity: str,
    dependency_identity: str,
    compiler_identity: str,
    vacuity_dependencies: Mapping[str, str] | None = None,
) -> CurrentVerificationFixture:
    """Build the exact current Formal IR and route records for synthetic jobs."""

    dependencies = dict(vacuity_dependencies or {})
    feasibility = set(dependencies.values())
    binding_records = [{
        "semantic_signal_id": "port:result",
        "rtl_module": "Counter",
        "rtl_name": "result",
        "width": 1,
        "direction": "output",
    }]
    binding_identity = "bindings:" + stable_digest(binding_records)
    normalized_jobs: list[VerificationJob] = []
    goals: list[FormalGoalPlan] = []
    binding_sets: dict[str, dict[str, object]] = {}
    for job in jobs:
        kind = FormalPlanGoalKind(job.kind)
        route = None
        skip_reason = None
        if job.executable:
            route = FormalExecutableRoute(
                FormalRouteKind.COVER_HARNESS
                if kind is FormalPlanGoalKind.COVER
                else FormalRouteKind.PROPERTY_HARNESS,
                (FormalBackendArtifactRef(
                    "direct_systemverilog",
                    hardware_identity,
                    binding_identity,
                ),),
            )
            binding_sets[route.identity] = {
                "route": route.identity,
                "backend": "direct_systemverilog",
                "artifact_hash": hardware_identity,
                "binding_identity": binding_identity,
                "bindings": binding_records,
            }
        else:
            skip_reason = FormalSkipReason(
                FormalSkipCode.ROUTE_UNAVAILABLE,
                job.reason or "formal route is unavailable",
            )
        goal = FormalGoalPlan(
            goal_identity="formal-goal:" + stable_digest({
                "property": job.property_id,
                "kind": job.kind,
            }),
            property_identity=job.property_id,
            kind=kind,
            clock_domain=job.clock_domain,
            reset_domain=job.reset_domain,
            assumption_ids=job.assumption_ids,
            required_observations=(),
            selected_ir_identity=hardware_identity,
            comparison_window=ComparisonWindow.same_cycle(),
            minimum_bmc_depth=ComparisonWindow.same_cycle().minimum_bmc_depth,
            route=route,
            skip_reason=skip_reason,
            source_origin=job.source_origin,
            clock_domain_contract=job.clock_domain_contract,
            physical_domain_identity=job.physical_domain_identity,
        )
        goals.append(goal)
        normalized_jobs.append(replace(
            job,
            route=None if route is None else route.identity,
            backend=None if route is None else "direct_systemverilog",
            artifact_hash=None if route is None else hardware_identity,
            binding_identity=None if route is None else binding_identity,
            selected_ir_identity=hardware_identity,
        ))

    execution_plan = FormalExecutionPlan(
        hardware_identity,
        "verification:" + stable_digest({
            "hardware": hardware_identity,
            "properties": sorted(job.property_id for job in jobs),
        }),
        tuple(goals),
    )
    compiler_plan = CompilerFormalExecutionPlan(
        hardware_identity,
        execution_plan,
        CandidateSiteLedger(()),
        FormalPolicy.OFF,
    )
    payload: dict[str, object] = {
        "formal_ir_version": 4,
        "identities": {
            "source": source_identity,
            "dependency": dependency_identity,
            "compiler": compiler_identity,
        },
        "hardware": {
            "high_level_ir_identity": hardware_identity,
            "selected_ir_identity": hardware_identity,
        },
        "scopes": [],
        "properties": [{
            "id": job.property_id,
            "kind": job.kind,
            "classification": (
                "bounded_reachability" if job.kind == "cover" else "behavioral"
            ),
            "generated_from": (
                "verification-feasibility:test"
                if job.property_id in feasibility else None
            ),
            "predicate": {"kind": "constant", "value": 1},
            "source_origin": (
                None if job.source_origin is None else job.source_origin.to_data()
            ),
        } for job in sorted(jobs, key=lambda item: item.property_id)],
        "vacuity_dependencies": dependencies,
        "binding_sets": [binding_sets[key] for key in sorted(binding_sets)],
        "execution_plan": execution_plan.to_data(),
        "compiler_execution_plan": compiler_plan.to_data(),
        "candidate_equivalence_records": [],
    }
    return CurrentVerificationFixture(payload, tuple(normalized_jobs))


__all__ = ["CurrentVerificationFixture", "current_verification_fixture"]
