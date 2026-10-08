"""Compiler-owned publication of immutable first-class verification bundles."""

from __future__ import annotations

from zlang.backend import systemverilog as systemverilog
from zlang.compilation_products import CompilationResult
from zlang.common import stable_digest
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


def _recursive_goal_design(
    result: CompilationResult,
    route: prepared_routes._PreparedFormalRoute,
    concrete: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool = False,
    supporting_assertions: tuple[object, ...] = (),
    target_requirements: tuple[object, ...] = (),
) -> tuple[
    ir_formal.FormalDesign | None,
    tuple[ir_formal.SignalBinding, ...],
    formal_planning.FormalSkipCode | None,
    str | None,
]:
    """Connect one existing recursive safety verification property to one backend only.

    Recursive semantic predicates already use concrete observation identities.
    This adapter resolves those identities exclusively through the v4 backend
    manifests and then reuses the ordinary structured safety verification harness emitter.  It
    never derives a locator from an RTL/instance name.
    """

    if route.connected.connected_artifact_hash is None:
        return (
            None,
            (),
            formal_planning.FormalSkipCode.ARTIFACT_UNAVAILABLE,
            route.connected.non_executable_reason
            or f"{route.backend} formal artifact is not connected",
        )
    artifact = route.artifact
    recursive_by_id = {
        item.semantic_binding_id: item
        for item in getattr(artifact, "recursive_bindings", ())
    }
    observation_by_id = {
        item.semantic_binding_id: item
        for item in getattr(artifact, "formal_observations", ())
        if item.physical_available and item.observation_token is not None
    }
    public = tuple(route.connected.dut_ports)
    public_by_id = {
        item.semantic_signal_id: item for item in public
    }
    public_clock = next(
        (item for item in public if item.semantic_signal_id == "clock"), None
    )
    public_reset = next(
        (item for item in public if item.semantic_signal_id == "reset"), None
    )
    if public_clock is None:
        return (
            None,
            (),
            formal_planning.FormalSkipCode.DOMAIN_UNSUPPORTED,
            f"{route.backend} recursive route has no explicit public clock",
        )

    target = (
        concrete
        if cover else recursive_requirements._guard_recursive_property(concrete, target_requirements)
    )
    predicates = (*assumptions, *supporting_assertions, target)
    required_ids = tuple(sorted({
        semantic_id
        for item in predicates
        for semantic_id in item.property.predicate.observation_ids()
        if item.property.predicate is not None
    }))
    if public_reset is None:
        return (
            None,
            (),
            formal_planning.FormalSkipCode.RESET_UNSUPPORTED,
            f"{route.backend} recursive route has no explicit public reset",
        )
    # Every clocked executable harness needs the exact physical reset contract,
    # even when a reset-epoch predicate observes a concrete child reset rather
    # than spelling ``reset_condition`` on the property itself.
    exact: list[ir_formal.SignalBinding] = [public_clock, public_reset]

    observation_ports: list[ir_formal.SignalBinding] = []
    for semantic_id in required_ids:
        root_physical = public_by_id.get(semantic_id)
        if root_physical is not None:
            if all(
                item.semantic_signal_id != semantic_id for item in exact
            ):
                exact.append(root_physical)
            continue
        binding = recursive_by_id.get(semantic_id)
        if binding is None:
            return (
                None,
                (),
                formal_planning.FormalSkipCode.BINDING_UNAVAILABLE,
                f"recursive semantic binding is unavailable: {semantic_id}",
            )
        local_id = binding.local_semantic_id
        if local_id == "clock":
            physical = ir_formal.SignalBinding(
                semantic_id,
                public_clock.rtl_module,
                public_clock.rtl_name,
                public_clock.width,
                "input",
                binding.clock_domain,
                binding.source_origin,
            )
        elif local_id == "reset":
            if public_reset is None:
                return (
                    None,
                    (),
                    formal_planning.FormalSkipCode.RESET_UNSUPPORTED,
                    f"recursive reset binding is unavailable: {semantic_id}",
                )
            physical = ir_formal.SignalBinding(
                semantic_id,
                public_reset.rtl_module,
                public_reset.rtl_name,
                public_reset.width,
                "input",
                binding.reset_domain or binding.clock_domain,
                binding.source_origin,
            )
        else:
            observation = observation_by_id.get(semantic_id)
            if observation is None or binding.rtl_module is None:
                return (
                    None,
                    (),
                    formal_planning.FormalSkipCode.OBSERVATION_UNAVAILABLE,
                    f"recursive formal observation is unavailable: {semantic_id}",
                )
            physical = ir_formal.SignalBinding(
                semantic_id,
                binding.rtl_module,
                observation.observation_token,
                binding.width,
                "output",
                binding.clock_domain,
                binding.source_origin,
            )
            observation_ports.append(physical)
        if all(item.semantic_signal_id != semantic_id for item in exact):
            exact.append(physical)

    for item in predicates:
        if item.property.predicate is None:
            code = (
                formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE
                if item in assumptions
                else formal_planning.FormalSkipCode.OBSERVATION_UNAVAILABLE
            )
            return (
                None,
                (),
                code,
                item.property.non_executable_reason
                or f"recursive property '{item.concrete_property_id}' has no "
                "structured predicate",
            )
        if item.property.non_executable_reason is not None:
            code = (
                formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE
                if item in assumptions
                else formal_planning.FormalSkipCode.OBSERVATION_UNAVAILABLE
            )
            return None, (), code, item.property.non_executable_reason

    observation_modules = {
        item.rtl_module for item in observation_ports
    }
    if len(observation_modules) > 1:
        return (
            None,
            (),
            formal_planning.FormalSkipCode.ARTIFACT_UNAVAILABLE,
            f"{route.backend} recursive observations span multiple RTL modules",
        )
    connected_module = next(
        iter(observation_modules), route.connected.connected_module
    )
    if connected_module is None:
        return (
            None,
            (),
            formal_planning.FormalSkipCode.ARTIFACT_UNAVAILABLE,
            f"{route.backend} recursive route has no validated formal top",
        )
    design = ir_formal.FormalDesign(
        module_name=(
            f"{result.ir.name}__recursive_"
            f"{stable_digest(concrete.concrete_property_id, length=12)}"
        ),
        properties=(
            tuple(item.property for item in assumptions)
            if cover
            else tuple(
                item.property
                for item in (*assumptions, *supporting_assertions, target)
            )
        ),
        bindings=tuple(exact),
        covers=(concrete.property,) if cover else (),
        connected_backend=route.backend,
        connected_artifact_hash=route.artifact_hash,
        connected_module=connected_module,
        implementation_text=route.implementation,
        dut_ports=tuple(dict.fromkeys((*public, *observation_ports))),
        clock_domains=tuple(result.ir.clock_domains),
    )
    return design, tuple(exact), None, None


class _RecursiveGoalPublisher:
    """Publish descendant safety goals and their feasibility dependencies."""

    def __init__(
        self,
        result: CompilationResult,
        route: prepared_routes._PreparedFormalRoute | None,
        route_failure: str | None,
        accumulator: routing._PublicationAccumulator,
        requirements: recursive_requirements._RecursiveRequirementPlanner,
    ) -> None:
        self._result = result
        self._route = route
        self._route_failure = route_failure
        self._published = accumulator
        self._requirements = requirements
        self.cover_properties: dict[str, recursive_requirements._RecursiveCoverGoal] = {}
        self._cover_by_recipe: dict[str, str] = {}
        self.vacuity_dependencies: dict[str, str] = {}

    def publish(self, concrete: object) -> None:
        """Publish one already-existing descendant safety verification property.

        This is an orchestration adapter only: the property and all semantic
        observation identities were produced by the frozen recursive safety verification IR.
        Each candidate backend must connect the complete set independently.
        """

        node = self._requirements.nodes.get(concrete.instance_identity)
        if node is None:
            raise ir_formal.FormalError(
                f"recursive goal '{concrete.concrete_property_id}' has no "
                "physical instance node"
            )
        clock_domain = (
            getattr(node, "clock_domain", None)
            or concrete.property.clock
        )
        reset_domain = (
            getattr(node, "reset_domain", None)
            or concrete.property.reset_condition
        )
        clock_domain_contract = routing._exact_clock_domain_contract(
            self._result, clock_domain, reset_domain
        )
        available_physical_domain_identity = (
            routing._route_physical_domain_identity(self._route, clock_domain_contract)
            if self._route is not None else None
        )
        (
            scope_id,
            assumptions,
            assumption_ids,
            local_requirement_predicate,
            ownership_blocker,
            supporting_assertions,
            target_requirements,
        ) = self._requirements.scope_context(
            concrete,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
        )
        scope_publication = self._requirements.scopes[scope_id]
        scope_publication.goals[concrete.concrete_property_id] = {
            "id": concrete.concrete_property_id,
            "kind": "assert",
            "name": concrete.source_property_id,
            "source_origin": prepared_routes._origin_payload(concrete.property.source_origin),
        }
        required = {
            semantic_id
            for item in (
                *assumptions,
                *supporting_assertions,
                recursive_requirements._guard_recursive_property(concrete, target_requirements),
            )
            for semantic_id in item.property.relevant_signals
        }
        required_observations = tuple(sorted(required))
        fallback_top = (
            f"{self._result.ir.name}__recursive_"
            f"{concrete.concrete_property_id[:12]}__safety_verification_formal"
        )
        goal_publication = GoalPublication(
            concrete.concrete_property_id,
            formal_planning.FormalPlanGoalKind.SAFETY,
            "safety",
            fallback_top,
            clock_domain,
            reset_domain,
            assumption_ids,
            required_observations,
            self._result.selected_ir_identity,
            concrete.property.source_origin,
            scope_id,
            concrete.physical_instance_path,
            clock_domain_contract,
        )
        if ownership_blocker is not None:
            message = (
                "recursive scoped assumption ownership is unavailable: "
                + ownership_blocker
            )
            skip = formal_planning.FormalSkipReason(
                formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE,
                message,
                assumption_ids,
            )
            self._published.record_skip(
                goal_publication,
                skip,
                physical_domain_identity=available_physical_domain_identity,
            )
            return

        chosen = self._route
        connected_design: ir_formal.FormalDesign | None = None
        exact_bindings: tuple[ir_formal.SignalBinding, ...] = ()
        failure: routing._RouteFailure | None = None
        if chosen is not None:
            design, bindings, code, reason = _recursive_goal_design(
                self._result,
                chosen,
                concrete,
                assumptions,
                supporting_assertions=supporting_assertions,
                target_requirements=target_requirements,
            )
            if design is not None:
                connected_design = design
                exact_bindings = bindings
            else:
                assert code is not None and reason is not None
                failure = routing._RouteFailure(
                    code, reason, (), chosen.backend
                )
                chosen = None

        if chosen is None or connected_design is None:
            skip = routing._unavailable_route_reason(
                failure,
                self._route_failure or (
                    "direct-SystemVerilog cannot publish a connected recursive "
                    "formal artifact"
                ),
                related_on_failure=required_observations,
                fallback_related=required_observations,
            )
            self._published.record_skip(
                goal_publication,
                skip,
                physical_domain_identity=available_physical_domain_identity,
            )
            return

        routing._publish_recursive_executable(
            self._published,
            goal_publication,
            chosen,
            connected_design,
            exact_bindings,
        )

        self._publish_feasibility(
            concrete,
            scope_id=scope_id,
            scope_publication=scope_publication,
            assumptions=assumptions,
            assumption_ids=assumption_ids,
            local_requirement_predicate=local_requirement_predicate,
            route=chosen,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            clock_domain_contract=clock_domain_contract,
        )

    def _publish_feasibility(
        self,
        concrete: object,
        *,
        scope_id: str,
        scope_publication: recursive_requirements._RecursiveScopePublication,
        assumptions: tuple[object, ...],
        assumption_ids: tuple[str, ...],
        local_requirement_predicate: formal_predicates.FormalPredicate | None,
        route: prepared_routes._PreparedFormalRoute,
        clock_domain: str,
        reset_domain: str,
        clock_domain_contract: ClockDomain | None,
    ) -> None:
        if not assumption_ids:
            return
        feasibility_parts = tuple(
            item.property.predicate
            for item in assumptions
            if item.property.predicate is not None
        ) + (
            (() if local_requirement_predicate is None
             else (local_requirement_predicate,))
        )
        if not feasibility_parts:
            # This can only occur for a non-executable assumption; keep the
            # safety result dependent on an explicit skipped cover rather than
            # treating absence of a predicate as feasibility.
            feasibility_predicate = None
        else:
            feasibility_predicate = recursive_requirements._conjoin_predicates(feasibility_parts)
        # Equivalent root-owned requirement predicates share one feasibility
        # query even when several descendant scopes depend on the same physical
        # input contract.
        feasibility_recipe = routing._feasibility_recipe(
            feasibility_parts,
            clock=clock_domain,
            reset=reset_domain,
            route=route,
        )
        existing_cover_id = self._cover_by_recipe.get(feasibility_recipe)
        if existing_cover_id is not None:
            self.vacuity_dependencies[
                concrete.concrete_property_id
            ] = existing_cover_id
            return
        cover_id = f"{scope_id}.requirements_feasible.{feasibility_recipe}"
        self._cover_by_recipe[feasibility_recipe] = cover_id
        cover = ir_formal.CoverProperty(
            cover_id,
            clock_domain,
            reset_domain,
            (
                feasibility_predicate.render()
                if feasibility_predicate is not None
                else "recursive requirements unavailable"
            ),
            feasibility_predicate,
            source_origin=concrete.property.source_origin,
            generated_from=f"verification-feasibility:{scope_id}",
            non_executable_reason=(
                None
                if feasibility_predicate is not None
                else "recursive requirements have no structured predicate"
            ),
        )
        cover_goal = recursive_requirements._RecursiveCoverGoal(
            cover_id,
            concrete.instance_identity,
            concrete.physical_instance_path,
            cover,
        )
        self.cover_properties[cover_id] = cover_goal
        self.vacuity_dependencies[
            concrete.concrete_property_id
        ] = cover_id
        scope_publication.goals[cover_id] = {
            "id": cover_id,
            "kind": "cover",
            "name": "requirements_feasible",
            "source_origin": prepared_routes._origin_payload(cover.source_origin),
        }
        cover_required = tuple(sorted(cover.relevant_signals))
        (
            cover_design,
            cover_bindings,
            cover_code,
            cover_reason,
        ) = _recursive_goal_design(
            self._result,
            route,
            cover_goal,
            (),
            cover=True,
        )
        physical_domain_identity = routing._route_physical_domain_identity(
            route, clock_domain_contract
        )
        cover_publication = GoalPublication(
            cover_id,
            formal_planning.FormalPlanGoalKind.COVER,
            "cover",
            ir_formal.cover_harness_top(
                ir_formal.FormalDesign(self._result.ir.name, (), ()), cover_id
            ),
            clock_domain,
            reset_domain,
            assumption_ids,
            cover_required,
            self._result.selected_ir_identity,
            cover.source_origin,
            scope_id,
            concrete.physical_instance_path,
            clock_domain_contract,
        )
        if cover_design is None:
            code = cover_code or formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE
            message = cover_reason or "recursive feasibility cover is unavailable"
            self._published.record_skip(
                cover_publication,
                formal_planning.FormalSkipReason(
                    code,
                    message,
                    assumption_ids,
                    route.backend,
                ),
                physical_domain_identity=physical_domain_identity,
            )
            return

        routing._publish_recursive_executable(
            self._published,
            cover_publication,
            route,
            cover_design,
            cover_bindings,
            cover=True,
        )
