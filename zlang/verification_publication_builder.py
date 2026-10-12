"""Compiler-owned publication of immutable first-class verification bundles."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from zlang.backend import systemverilog as systemverilog
from zlang.compilation_products import CompilationResult
from zlang.common import stable_digest, stable_pretty_json
from zlang.candidate_equivalence import PreparedCandidateEquivalenceSite
from zlang import formal_artifact_provider as artifact_provider
from zlang import formal_orchestration as formal_orchestration
from zlang import formal as formal
from zlang.ir import formal as ir_formal
from zlang.ir import formal_predicates as formal_predicates
from zlang.ir import formal_planning as formal_planning
from zlang.ir.module import dependency_context_identity
from zlang.ir.verification import verification_identity as overlay_verification_identity
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang import verification_bundle_codec as bundle_codec
from zlang import verification_bundle_io as bundle_io
import zlang.verification_prepared_routes as prepared_routes
import zlang.verification_recursive_requirements as recursive_requirements
import zlang.verification_goal_routing as routing
from zlang.verification_recursive_publication import _RecursiveGoalPublisher
from zlang import verification_root_publication as root_publication


def _published_properties_payload(
    source_safety: tuple[object, ...],
    source_covers: tuple[object, ...],
    root_goals: root_publication._RootGoalPublisher,
    recursive_safety: tuple[object, ...],
    recursive_goals: _RecursiveGoalPublisher,
) -> list[dict[str, object]]:
    """Collect all source and recursive properties in stable schema order."""

    groups = (
        (source_safety, "safety"),
        (source_covers, "cover"),
        (
            tuple(
                root_goals.feasibility_properties[key]
                for key in sorted(root_goals.feasibility_properties)
            ),
            "cover",
        ),
        (
            tuple(
                sorted(
                    recursive_safety,
                    key=lambda value: value.concrete_property_id,
                )
            ),
            "safety",
        ),
        (
            tuple(
                recursive_goals.cover_properties[key]
                for key in sorted(recursive_goals.cover_properties)
            ),
            "cover",
        ),
    )
    properties: list[dict[str, object]] = []
    for items, kind in groups:
        for item in items:
            prop = getattr(item, "property", item)
            properties.append({
                "id": getattr(item, "concrete_property_id", prop.id),
                "kind": kind,
                "classification": (
                    prop.classification.value
                    if kind == "safety"
                    else "bounded_reachability"
                ),
                "generated_from": prop.generated_from,
                "predicate": None if prop.predicate is None else prop.predicate.to_data(),
                "source_origin": prepared_routes._origin_payload(prop.source_origin),
            })
    return properties


def publish_compilation_verification_bundle(
    result: CompilationResult,
    directory: Path,
    *,
    compiler_execution_plan: formal_orchestration.CompilerFormalExecutionPlan | None = None,
    prepared_candidate_equivalence: tuple[
        PreparedCandidateEquivalenceSite, ...
    ] = (),
) -> bundle_codec.VerificationBundleManifest:
    """Plan every current safety verification/source goal and publish immutable routes.

    Each goal is connected to one direct-SystemVerilog artifact. Missing
    observations or assumptions produce an explicit non-executable plan.
    """

    source_design = result.formal_design
    source_assumptions = tuple(
        item for item in source_design.properties
        if item.kind is ir_formal.PropertyKind.ASSUMPTION
    )
    source_safety = tuple(
        item for item in source_design.properties
        if item.kind is ir_formal.PropertyKind.ASSERTION
    )
    source_covers = source_design.covers
    recursive_design = result.recursive_formal_design
    root_path = (result.ir.name,)
    recursive_properties = tuple(
        item for item in getattr(recursive_design, "properties", ())
        if item.physical_instance_path != root_path
    )
    requirement_planner = recursive_requirements._RecursiveRequirementPlanner(
        result,
        source_safety,
        recursive_properties,
    )
    recursive_safety = requirement_planner.safety
    recursive_scopes = requirement_planner.scopes
    if not source_safety and not source_covers and not recursive_safety:
        raise ir_formal.FormalError("verification bundle requires at least one safety or cover goal")

    provider = getattr(result, "formal_artifact_provider", None)
    if not isinstance(provider, artifact_provider.FormalArtifactProvider):
        provider = artifact_provider.FormalArtifactProvider()

    root_assumptions = root_publication._RootAssumptionPlanner(
        result, source_assumptions
    )

    direct_route: prepared_routes._PreparedFormalRoute | None = None
    direct_failure: str | None = None
    try:
        direct_route = provider.get_or_prepare(
            artifact_provider.FormalArtifactNamespace.PREPARED,
            "direct-systemverilog-formal-route-v1",
            prepared_routes._prepared_route_recipe(result, "direct_systemverilog"),
            lambda: prepared_routes._prepare_route(
                result,
                systemverilog.emit_formal_artifact(
                    result.ir,
                    result.recursive_formal_design,
                    selected_ir_identity=result.selected_ir_identity,
                ),
            ),
            encode=prepared_routes._encode_prepared_route,
            decode=lambda value: prepared_routes._decode_prepared_route(result, value),
            fingerprint=prepared_routes._prepared_route_fingerprint,
        )
    except (systemverilog.SystemVerilogEmissionError, ir_formal.FormalError, ValueError) as error:
        direct_failure = f"direct-SystemVerilog formal route is unavailable: {error}"

    published = routing._PublicationAccumulator()
    root_goals = root_publication._RootGoalPublisher(
        result,
        source_design,
        provider,
        direct_route,
        direct_failure,
        published,
        root_assumptions,
    )
    recursive_goals = _RecursiveGoalPublisher(
        result,
        direct_route,
        direct_failure,
        published,
        requirement_planner,
    )

    for prop in source_safety:
        root_goals.publish(prop, cover=False)
    for prop in source_covers:
        root_goals.publish(prop, cover=True)
    for concrete in sorted(
        recursive_safety,
        key=lambda item: item.concrete_property_id,
    ):
        recursive_goals.publish(concrete)

    # Preserve the historical single-route source-map view.  Mixed-route
    # bundles deliberately omit it because selecting one backend would be
    # ambiguous; each executable job already references its exact map.
    if len(published.used_artifacts) == 1:
        only_key = next(iter(published.used_artifacts))
        only_route = next(
            item for item in (direct_route,)
            if item is not None
            and (item.backend, item.artifact_hash) == only_key
        )
        published.inputs.pop(only_route.source_map_path, None)
        published.add_input(bundle_codec.VerificationBundleInput(
            "source-map/formal.json",
            "source_map",
            only_route.source_map.encode("utf-8"),
        ))
        published.jobs = [
            replace(
                item,
                source_map_files=("source-map/formal.json",),
            )
            if item.executable and item.source_map_files == (only_route.source_map_path,)
            else item
            for item in published.jobs
        ]

    feasibility_by_scope = {
        item.generated_from.removeprefix("verification-feasibility:"): item.id
        for item in source_covers
        if item.generated_from and item.generated_from.startswith("verification-feasibility:")
    }
    module_feasibility = feasibility_by_scope.get("$module")
    vacuity_dependencies: dict[str, str] = {}
    for item in source_safety:
        generated = item.generated_from or ""
        parts = generated.split(":")
        scope = parts[1] if len(parts) >= 3 and parts[0] in {
            "verification-assert", "verification-ensure"
        } else None
        feasibility = feasibility_by_scope.get(scope or "") or module_feasibility
        if feasibility is not None:
            vacuity_dependencies[item.id] = feasibility
    vacuity_dependencies.update(root_goals.vacuity_dependencies)
    vacuity_dependencies.update(recursive_goals.vacuity_dependencies)

    execution_plan = formal_planning.FormalExecutionPlan(
        result.selected_ir_identity,
        "verification:" + stable_digest({
            "overlay": overlay_verification_identity(
                result.ir.verification_scopes
            ),
            "recursive_goals": [
                item.concrete_property_id
                for item in sorted(
                    recursive_safety,
                    key=lambda value: value.concrete_property_id,
                )
            ] + sorted(root_goals.feasibility_properties)
            + sorted(recursive_goals.cover_properties),
        }),
        tuple(published.goal_plans),
    )
    base_compiler_execution_plan, _ = formal_orchestration.build_compiler_formal_execution_plan(
        result,
        execution_plan,
    )
    if compiler_execution_plan is None:
        compiler_execution_plan = base_compiler_execution_plan
    else:
        if not isinstance(compiler_execution_plan, formal_orchestration.CompilerFormalExecutionPlan):
            raise formal_orchestration.FormalOrchestrationError(
                "verification publication requires a typed compiler formal plan"
            )
        if compiler_execution_plan.verification_plan != execution_plan:
            raise formal_orchestration.FormalOrchestrationError(
                "published compiler formal plan references a different "
                "verification execution plan"
            )
        if (
            compiler_execution_plan.selected_ir_identity
            != base_compiler_execution_plan.selected_ir_identity
            or compiler_execution_plan.candidate_site_ledger
            != base_compiler_execution_plan.candidate_site_ledger
            or compiler_execution_plan.formal_policy
            is not base_compiler_execution_plan.formal_policy
            or compiler_execution_plan.formal_selection_attempts
            != base_compiler_execution_plan.formal_selection_attempts
        ):
            raise formal_orchestration.FormalOrchestrationError(
                "published compiler formal plan differs from this compilation"
            )
    if tuple(item.plan for item in prepared_candidate_equivalence) != (
        compiler_execution_plan.candidate_equivalence_plans
    ):
        raise formal_orchestration.FormalOrchestrationError(
            "published candidate replay inputs differ from compiler plans"
        )
    candidate_records: list[dict[str, object]] = []
    for prepared in prepared_candidate_equivalence:
        frozen = prepared.freeze()
        content = stable_pretty_json(frozen.to_data()).encode("utf-8")
        content_hash = hashlib.sha256(content).hexdigest()
        path = (
            "implementation/companions/candidate-equivalence/"
            + stable_digest({
                "site": frozen.plan.site_identity,
                "candidate": frozen.plan.candidate_identity,
                "plan": frozen.plan.plan_identity,
            })[:24]
            + ".json"
        )
        published.add_input(
            bundle_codec.VerificationBundleInput(path, "companion", content)
        )
        candidate_records.append({
            "site_identity": frozen.plan.site_identity,
            "candidate_identity": frozen.plan.candidate_identity,
            "plan_identity": frozen.plan.plan_identity,
            "replay_identity": frozen.replay_identity,
            "logical_path": path,
            "content_hash": content_hash,
        })
    payload: dict[str, object] = {
        "formal_ir_version": 4,
        "identities": {
            "source": "source:" + stable_digest({
                "logical_source": result.ir.source_identity or result.ir.name,
                "source_hash": result.ir.source_hash,
            }),
            "dependency": "dependency:" + stable_digest({
                "context": dependency_context_identity(result.ir),
            }),
            "compiler": "compiler:" + stable_digest({
                "package": "zlang-hdl",
                "version": prepared_routes._compiler_version(),
                "canonical_ir": CANONICAL_IR_IDENTITY_SCHEMA,
                "formal_predicate": formal_predicates.FORMAL_PREDICATE_SCHEMA,
                "verification_publication": 4,
            }),
        },
        "hardware": {
            "high_level_ir_identity": result.high_level_ir_identity,
            "selected_ir_identity": result.selected_ir_identity,
        },
        "scopes": routing._scope_payload(result) + [
            {
                "id": item.scope_id,
                "name": item.name,
                "clock": item.clock,
                "reset": item.reset,
                "requirements": [
                    item.requirements[key]
                    for key in sorted(item.requirements)
                ],
                "goals": [
                    item.goals[key] for key in sorted(item.goals)
                ],
                "source_origin": prepared_routes._origin_payload(item.source_origin),
            }
            for item in (
                recursive_scopes[key] for key in sorted(recursive_scopes)
            )
        ],
        "properties": _published_properties_payload(
            source_safety,
            source_covers,
            root_goals,
            recursive_safety,
            recursive_goals,
        ),
        "binding_sets": [
            published.binding_sets[item] for item in sorted(published.binding_sets)
        ],
        "execution_plan": execution_plan.to_data(),
        "compiler_execution_plan": compiler_execution_plan.to_data(),
        "candidate_equivalence_records": candidate_records,
        "vacuity_dependencies": vacuity_dependencies,
    }
    property_ids = tuple(item.property_id for item in published.jobs)
    verification_identity = bundle_codec.verification_identity_for(
        top=result.ir.name,
        hardware_identity=result.selected_ir_identity,
        property_ids=property_ids,
        payload=payload,
    )
    return bundle_io.publish_verification_bundle(
        directory,
        top=result.ir.name,
        hardware_identity=result.selected_ir_identity,
        verification_identity=verification_identity,
        property_ids=property_ids,
        verification_ir=payload,
        files=tuple(
            published.inputs[item] for item in sorted(published.inputs)
        ),
        jobs=published.jobs,
    )
