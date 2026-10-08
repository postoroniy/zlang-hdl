"""Compiler-owned publication of immutable first-class verification bundles."""

from __future__ import annotations

from zlang.backend import systemverilog as systemverilog
from zlang.compilation_products import CompilationResult
from zlang import formal_artifact_provider as artifact_provider
from zlang import formal_orchestration as formal_orchestration
from zlang import formal as formal
from zlang.ir import formal as ir_formal
from zlang.ir import formal_predicates as formal_predicates
from zlang.ir.cdc import ClockDomain
from zlang.ir import formal_planning as formal_planning
import zlang.verification_prepared_routes as prepared_routes
from zlang.verification_publication_records import GoalPublication
import zlang.verification_recursive_requirements as recursive_requirements
import zlang.verification_goal_routing as routing


class _RootAssumptionPlanner:
    """Resolve root-goal assumption membership from compiler-owned metadata."""

    def __init__(
        self,
        result: CompilationResult,
        source_assumptions: tuple[object, ...],
    ) -> None:
        self._result = result
        self._globals_by_domain, self._scope_metadata = routing._goal_scope_metadata(result)
        self._assumption_by_id = {item.id: item for item in source_assumptions}
        # First-class contract requirements are already folded into each local
        # goal predicate and feasibility cover.  Legacy module-global assumes
        # remain separate FormalProperty records.
        self._folded_requirement_ids = {
            item.semantic_id
            for scope in result.ir.verification_scopes
            if scope.name != "$module"
            for item in scope.requirements
        }

    def resolve(
        self,
        prop: object,
    ) -> tuple[str | None, tuple[str, ...], tuple[object, ...], tuple[str, ...]]:
        clock_domain, reset_domain = routing._root_property_domain(self._result, prop)
        default_ids = self._globals_by_domain.get((clock_domain, reset_domain), ())
        scope_id, assumption_ids = self._scope_metadata.get(
            prop.id,
            (None, default_ids),
        )
        missing = tuple(
            item
            for item in assumption_ids
            if item not in self._assumption_by_id
            and item not in self._folded_requirement_ids
        )
        assumptions = tuple(
            self._assumption_by_id[item]
            for item in assumption_ids
            if item in self._assumption_by_id
        )
        return scope_id, assumption_ids, assumptions, missing


class _RootGoalPublisher:
    """Publish root safety/cover goals and their feasibility dependencies."""

    def __init__(
        self,
        result: CompilationResult,
        source_design: ir_formal.FormalDesign,
        provider: artifact_provider.FormalArtifactProvider,
        route: prepared_routes._PreparedFormalRoute | None,
        route_failure: str | None,
        accumulator: routing._PublicationAccumulator,
        assumptions: _RootAssumptionPlanner,
    ) -> None:
        self._result = result
        self._source_design = source_design
        self._provider = provider
        self._route = route
        self._route_failure = route_failure
        self._published = accumulator
        self._assumptions = assumptions
        self.feasibility_properties: dict[str, ir_formal.CoverProperty] = {}
        self._feasibility_by_recipe: dict[str, str] = {}
        self.vacuity_dependencies: dict[str, str] = {}

    def publish(self, prop: object, *, cover: bool) -> None:
        result = self._result
        clock_domain, reset_domain = routing._root_property_domain(result, prop)
        clock_domain_contract = routing._exact_clock_domain_contract(
            result,
            clock_domain,
            reset_domain,
        )
        available_physical_domain_identity = (
            routing._route_physical_domain_identity(self._route, clock_domain_contract)
            if self._route is not None else None
        )
        (
            scope_id,
            scoped_ids,
            harness_assumptions,
            missing_assumptions,
        ) = self._assumptions.resolve(prop)
        kind = (
            formal_planning.FormalPlanGoalKind.COVER
            if cover
            else formal_planning.FormalPlanGoalKind.SAFETY
        )
        required = set(getattr(prop, "relevant_signals", ()))
        for assumption in harness_assumptions:
            required.update(assumption.relevant_signals)
        publication = GoalPublication(
            prop.id,
            kind,
            "cover" if cover else "safety",
            (
                ir_formal.cover_harness_top(self._source_design, prop.id)
                if cover
                else f"{self._source_design.module_name}__safety_verification_formal"
            ),
            clock_domain,
            reset_domain,
            scoped_ids,
            tuple(sorted(required)),
            result.selected_ir_identity,
            prop.source_origin,
            scope_id,
            (result.ir.name,),
            clock_domain_contract,
        )
        if missing_assumptions:
            self._published.record_skip(
                publication,
                formal_planning.FormalSkipReason(
                    formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE,
                    "verification scope references unavailable assumption(s): "
                    + ", ".join(missing_assumptions),
                    missing_assumptions,
                ),
                physical_domain_identity=available_physical_domain_identity,
            )
            return

        chosen = self._route
        failure: routing._RouteFailure | None = None
        if chosen is not None:
            code, reason, related = routing._route_goal_reason(
                chosen,
                prop,
                harness_assumptions,
                cover=cover,
            )
            if code is not None:
                assert reason is not None
                failure = routing._RouteFailure(
                    code, reason, related, chosen.backend
                )
                chosen = None
        if chosen is None:
            self._published.record_skip(
                publication,
                routing._unavailable_route_reason(
                    failure,
                    self._route_failure or (
                        "direct-SystemVerilog cannot publish a connected "
                        "formal artifact"
                    ),
                ),
                physical_domain_identity=available_physical_domain_identity,
            )
            return

        prepared_goal = self._provider.get_or_prepare(
            artifact_provider.FormalArtifactNamespace.SAFETY,
            "exact-property-harness-v1",
            routing._safety_verification_goal_recipe(
                result,
                chosen,
                prop,
                harness_assumptions,
                cover=cover,
            ),
            lambda: routing._prepare_safety_verification_goal(
                chosen,
                prop,
                harness_assumptions,
                cover=cover,
            ),
            fingerprint=routing._safety_verification_goal_fingerprint,
        )
        physical_domain_identity = routing._route_physical_domain_identity(
            chosen,
            clock_domain_contract,
        )
        self._published.record_executable(
            publication,
            chosen,
            prepared_goal.route,
            harness_path=prepared_goal.harness_path,
            checker=prepared_goal.checker,
            bindings=prepared_goal.bindings,
            binding_identity=prepared_goal.binding_identity,
            physical_domain_identity=physical_domain_identity,
        )
        self._publish_feasibility(
            prop,
            cover=cover,
            scope_id=scope_id,
            scoped_ids=scoped_ids,
            assumptions=harness_assumptions,
            route=chosen,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            clock_domain_contract=clock_domain_contract,
        )

    def _publish_feasibility(
        self,
        prop: object,
        *,
        cover: bool,
        scope_id: str | None,
        scoped_ids: tuple[str, ...],
        assumptions: tuple[object, ...],
        route: prepared_routes._PreparedFormalRoute,
        clock_domain: str,
        reset_domain: str,
        clock_domain_contract: ClockDomain | None,
    ) -> None:
        if (
            cover
            or not assumptions
            or not any(
                item.id.startswith("safety_verification.")
                for item in assumptions
            )
        ):
            return
        if any(item.predicate is None for item in assumptions):
            raise ir_formal.FormalError(
                "root automatic assumptions have no structured feasibility predicate"
            )
        predicate = recursive_requirements._conjoin_predicates(
            tuple(item.predicate for item in assumptions)
        )
        recipe = routing._feasibility_recipe(
            tuple(item.predicate for item in assumptions),
            clock=clock_domain,
            reset=reset_domain,
            route=route,
        )
        existing_cover_id = self._feasibility_by_recipe.get(recipe)
        if existing_cover_id is not None:
            self.vacuity_dependencies[prop.id] = existing_cover_id
            return
        feasibility_scope = scope_id or "$module"
        cover_id = f"{feasibility_scope}.requirements_feasible.{recipe}"
        feasibility = ir_formal.CoverProperty(
            cover_id,
            clock_domain,
            reset_domain,
            predicate.render(),
            predicate,
            source_origin=prop.source_origin,
            generated_from=f"verification-feasibility:{feasibility_scope}",
        )
        self._feasibility_by_recipe[recipe] = cover_id
        self.feasibility_properties[cover_id] = feasibility
        self.vacuity_dependencies[prop.id] = cover_id
        prepared = self._provider.get_or_prepare(
            artifact_provider.FormalArtifactNamespace.SAFETY,
            "exact-property-harness-v1",
            routing._safety_verification_goal_recipe(
                self._result,
                route,
                feasibility,
                (),
                cover=True,
            ),
            lambda: routing._prepare_safety_verification_goal(
                route,
                feasibility,
                (),
                cover=True,
            ),
            fingerprint=routing._safety_verification_goal_fingerprint,
        )
        publication = GoalPublication(
            cover_id,
            formal_planning.FormalPlanGoalKind.COVER,
            "cover",
            ir_formal.cover_harness_top(self._source_design, cover_id),
            clock_domain,
            reset_domain,
            scoped_ids,
            tuple(sorted(feasibility.relevant_signals)),
            self._result.selected_ir_identity,
            prop.source_origin,
            scope_id,
            (self._result.ir.name,),
            clock_domain_contract,
        )
        self._published.record_executable(
            publication,
            route,
            prepared.route,
            harness_path=prepared.harness_path,
            checker=prepared.checker,
            bindings=prepared.bindings,
            binding_identity=prepared.binding_identity,
            physical_domain_identity=routing._route_physical_domain_identity(
                route,
                clock_domain_contract,
            ),
        )
