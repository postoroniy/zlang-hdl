"""Compiler-owned publication of immutable first-class verification bundles."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from zlang.backend import systemverilog as systemverilog
from zlang.compilation_products import CompilationResult
from zlang import verification_bundle_codec as bundle_codec
from zlang.common import stable_digest
from zlang import formal_orchestration as formal_orchestration
from zlang import formal as formal
from zlang.ir import formal as ir_formal
from zlang.ir import formal_predicates as formal_predicates
from zlang.ir.cdc import ClockDomain, clock_domain_contract_identity
from zlang.ir import formal_planning as formal_planning
import zlang.verification_prepared_routes as prepared_routes
from zlang.verification_publication_records import GoalPublication


@dataclass(frozen=True)
class _RouteFailure:
    code: formal_planning.FormalSkipCode
    message: str
    related: tuple[str, ...]
    backend: str


def _route_inputs(route: prepared_routes._PreparedFormalRoute) -> tuple[bundle_codec.VerificationBundleInput, ...]:
    companions = tuple(getattr(route.artifact, "companions", ()))
    return (
        bundle_codec.VerificationBundleInput(
            route.implementation_path,
            "implementation",
            route.implementation.encode("utf-8"),
        ),
        bundle_codec.VerificationBundleInput(
            route.source_map_path,
            "source_map",
            route.source_map.encode("utf-8"),
        ),
        *(
            bundle_codec.VerificationBundleInput(path, "companion", item.text.encode("ascii"))
            for path, item in zip(route.companion_paths, companions, strict=True)
        ),
    )


@dataclass
class _PublicationAccumulator:
    """Own deterministic bundle inputs, plans, bindings, and executable jobs."""

    jobs: list[bundle_codec.VerificationJob] = field(default_factory=list)
    goal_plans: list[formal_planning.FormalGoalPlan] = field(default_factory=list)
    inputs: dict[str, bundle_codec.VerificationBundleInput] = field(
        default_factory=dict
    )
    binding_sets: dict[str, dict[str, object]] = field(default_factory=dict)
    used_artifacts: set[tuple[str, str]] = field(default_factory=set)

    def add_input(self, item: bundle_codec.VerificationBundleInput) -> None:
        previous = self.inputs.get(item.logical_path)
        if previous is not None and previous != item:
            raise ir_formal.FormalError(
                "verification routes publish different contents for "
                f"'{item.logical_path}'"
            )
        self.inputs[item.logical_path] = item

    def record_skip(
        self,
        publication: GoalPublication,
        reason: formal_planning.FormalSkipReason,
        *,
        physical_domain_identity: str,
    ) -> None:
        self.goal_plans.append(publication.plan(
            skip_reason=reason,
            physical_domain_identity=physical_domain_identity,
        ))
        self.jobs.append(publication.job(
            executable=False,
            reason=f"{reason.code.value}: {reason.message}",
            physical_domain_identity=physical_domain_identity,
        ))

    def record_executable(
        self,
        publication: GoalPublication,
        route_owner: prepared_routes._PreparedFormalRoute,
        route: formal_planning.FormalExecutableRoute,
        *,
        harness_path: str,
        checker: str | bytes,
        bindings: tuple[object, ...],
        binding_identity: str,
        physical_domain_identity: str,
        binding_conflict: str | None = None,
    ) -> None:
        self.goal_plans.append(publication.plan(
            route=route,
            physical_domain_identity=physical_domain_identity,
        ))
        content = checker if isinstance(checker, bytes) else checker.encode("utf-8")
        self.add_input(bundle_codec.VerificationBundleInput(
            harness_path,
            "harness",
            content,
        ))
        artifact_key = (route_owner.backend, route_owner.artifact_hash)
        if artifact_key not in self.used_artifacts:
            for item in _route_inputs(route_owner):
                self.add_input(item)
            self.used_artifacts.add(artifact_key)
        binding_record = {
            "route": route.identity,
            "backend": route_owner.backend,
            "artifact_hash": route_owner.artifact_hash,
            "binding_identity": binding_identity,
            "bindings": [prepared_routes._binding_payload(item) for item in bindings],
        }
        previous = self.binding_sets.get(route.identity)
        if binding_conflict is not None and (
            previous is not None and previous != binding_record
        ):
            raise ir_formal.FormalError(binding_conflict)
        self.binding_sets[route.identity] = binding_record
        self.jobs.append(publication.job(
            source_files=(
                route_owner.implementation_path,
                harness_path,
                *route_owner.companion_paths,
            ),
            source_map_files=(route_owner.source_map_path,),
            route=route.identity,
            backend=route_owner.backend,
            artifact_hash=route_owner.artifact_hash,
            binding_identity=binding_identity,
            physical_domain_identity=physical_domain_identity,
        ))



def _unavailable_route_reason(
    failure: _RouteFailure | None,
    fallback: str,
    *,
    related_on_failure: tuple[str, ...] | None = None,
    fallback_related: tuple[str, ...] = (),
) -> formal_planning.FormalSkipReason:
    """Apply the one fail-closed route-selection policy for every goal kind."""

    if failure is None:
        return formal_planning.FormalSkipReason(
            formal_planning.FormalSkipCode.BACKEND_UNAVAILABLE,
            fallback,
            fallback_related,
        )
    code = failure.code
    related = (
        related_on_failure
        if related_on_failure is not None
        else failure.related
    )
    return formal_planning.FormalSkipReason(
        code,
        failure.message,
        related,
        failure.backend,
    )


def _route_property(
    connected: ir_formal.FormalDesign,
    property_id: str,
    *,
    cover: bool,
) -> object:
    values = connected.covers if cover else connected.properties
    matches = tuple(item for item in values if item.id == property_id)
    if len(matches) != 1:
        raise ir_formal.FormalError(
            f"connected formal route has {len(matches)} records for '{property_id}'"
        )
    return matches[0]


def _connect_exact_goal(
    route: prepared_routes._PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> ir_formal.FormalDesign:
    """Connect one goal/domain without sharing generic clock/reset bindings.

    ``connect_formal_design`` intentionally names the clock and reset
    observations ``clock`` and ``reset`` inside one harness.  Connecting a
    whole multi-domain source design would therefore collide two otherwise
    valid physical domains before per-goal planning.  Each executable harness
    owns exactly one goal and its scoped assumptions, so connect that exact
    singleton here and never weaken or combine backend routes.
    """

    source = ir_formal.FormalDesign(
        module_name=route.connected.module_name,
        properties=assumptions + (() if cover else (prop,)),
        bindings=(),
        covers=(prop,) if cover else (),
        clock_domains=route.connected.clock_domains,
    )
    return formal.connect_formal_design(source, route.artifact)


def _route_goal_reason(
    route: prepared_routes._PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> tuple[formal_planning.FormalSkipCode | None, str | None, tuple[str, ...]]:
    connected_design = _connect_exact_goal(
        route, prop=prop, assumptions=assumptions, cover=cover,
    )
    if connected_design.connected_artifact_hash is None:
        return (
            formal_planning.FormalSkipCode.ARTIFACT_UNAVAILABLE,
            connected_design.non_executable_reason
            or f"{route.backend} formal artifact is not connected",
            (),
        )
    connected_assumptions = {
        item.id: item
        for item in connected_design.properties
        if item.kind is ir_formal.PropertyKind.ASSUMPTION
    }
    unavailable_assumptions: list[tuple[str, str]] = []
    for source in assumptions:
        connected = connected_assumptions.get(source.id)
        reason = (
            "assumption is absent from the connected formal design"
            if connected is None
            else connected.non_executable_reason
            or (None if connected.predicate is not None else "structured predicate unavailable")
        )
        if reason is not None:
            unavailable_assumptions.append((source.id, reason))
    if unavailable_assumptions:
        ids = tuple(item[0] for item in unavailable_assumptions)
        detail = "; ".join(f"{item}: {reason}" for item, reason in unavailable_assumptions)
        return (
            formal_planning.FormalSkipCode.ASSUMPTION_UNAVAILABLE,
            f"{route.backend} route cannot bind required assumptions: {detail}",
            ids,
        )
    goal = _route_property(connected_design, prop.id, cover=cover)
    reason = goal.non_executable_reason or (
        None if goal.predicate is not None else "structured predicate unavailable"
    )
    if reason is not None:
        return (
            formal_planning.FormalSkipCode.OBSERVATION_UNAVAILABLE,
            f"{route.backend} route cannot bind goal '{prop.id}': {reason}",
            tuple(getattr(goal, "relevant_signals", ())),
        )
    return None, None, ()


def _checker_only(complete: str, implementation: str) -> str:
    prefix = implementation.rstrip()
    if not complete.startswith(prefix):
        raise ir_formal.FormalError("verification harness does not contain the connected implementation")
    checker = complete[len(prefix):].lstrip("\r\n")
    if not checker:
        raise ir_formal.FormalError("verification harness contains no checker module")
    return checker


def _publish_recursive_executable(
    accumulator: _PublicationAccumulator,
    publication: GoalPublication,
    route: prepared_routes._PreparedFormalRoute,
    design: ir_formal.FormalDesign,
    bindings: tuple[ir_formal.SignalBinding, ...],
    *,
    cover: bool = False,
) -> None:
    """Publish one connected recursive harness through the shared route schema."""

    top = (
        ir_formal.cover_harness_top(design, publication.identity)
        if cover
        else f"{design.module_name}__safety_verification_formal"
    )
    published_bindings = (
        *bindings,
        *prepared_routes._checker_reset_trace_bindings(design, top=top, cover=cover),
    )
    binding_identity = prepared_routes._binding_identity(published_bindings)
    artifact_ref = formal_planning.FormalBackendArtifactRef(
        route.backend,
        route.artifact_hash,
        binding_identity,
    )
    executable_route = formal_planning.FormalExecutableRoute(
        (
            formal_planning.FormalRouteKind.COVER_HARNESS
            if cover
            else formal_planning.FormalRouteKind.PROPERTY_HARNESS
        ),
        (artifact_ref,),
    )
    harness_path = (
        f"harness/{prepared_routes._token(f'{publication.identity}:{executable_route.identity}')}.sv"
    )
    complete = (
        formal.emit_cover_harness(design, cover_id=publication.identity)
        if cover
        else formal.emit_harness(design)
    )
    accumulator.record_executable(
        replace(publication, top=top),
        route,
        executable_route,
        harness_path=harness_path,
        checker=_checker_only(complete, route.implementation),
        bindings=published_bindings,
        binding_identity=binding_identity,
        physical_domain_identity=_route_physical_domain_identity(
            route,
            publication.clock_domain_contract,
        ),
        binding_conflict=(
            "recursive cover route identity resolved to different bindings"
            if cover
            else "recursive formal route identity resolved to different bindings"
        ),
    )


def _root_property_domain(
    result: CompilationResult,
    prop: ir_formal.FormalProperty | ir_formal.CoverProperty,
) -> tuple[str, str | None]:
    """Return the physical root domain independently of disable-iff policy.

    Some automatic reset/history properties deliberately have
    ``reset_condition=None`` so the checker observes the reset edge instead of
    being disabled by it.  That does not move the property into a different
    physical reset domain.
    """

    matches = tuple(
        item.reset for item in result.ir.clock_domains
        if item.clock == prop.clock
    )
    if len(matches) == 1:
        return prop.clock, matches[0]
    if result.ir.clock == prop.clock:
        return prop.clock, result.ir.reset
    return prop.clock, prop.reset_condition


def _exact_clock_domain_contract(
    result: CompilationResult,
    clock_domain: str | None,
    reset_domain: str | None,
) -> ClockDomain | None:
    """Resolve one goal's exact physical contract from typed root IR.

    Recursive component domains have already been mapped and validated against
    the root physical domain during elaboration.  Consequently the root names
    are the authoritative lookup key; manufacturing a default contract when no
    exact match exists would make a skipped goal look executable under the
    wrong reset semantics.
    """

    if clock_domain is None or reset_domain is None:
        return None
    matches = tuple(
        item for item in result.ir.clock_domains
        if item.clock == clock_domain and item.reset == reset_domain
    )
    if len(matches) > 1:
        raise ir_formal.FormalError(
            f"formal goal domain '{clock_domain}/{reset_domain}' is ambiguous"
        )
    return matches[0] if matches else None


def _route_physical_domain_identity(
    route: prepared_routes._PreparedFormalRoute,
    contract: ClockDomain | None,
) -> str | None:
    """Return the exact artifact-published domain identity when available."""

    if contract is None:
        return None
    expected = clock_domain_contract_identity(contract)
    matches = tuple(
        item for item in getattr(route.artifact, "physical_domains", ())
        if (
            getattr(item, "clock", None) == contract.clock
            and getattr(item, "reset", None) == contract.reset
            and getattr(item, "identity", None) == expected
        )
    )
    if len(matches) > 1:
        raise ir_formal.FormalError(
            "backend artifact publishes the formal goal physical domain more "
            "than once"
        )
    return expected if matches else None


def _root_assumptions_by_domain(
    result: CompilationResult,
) -> dict[tuple[str, str | None], tuple[ir_formal.FormalProperty, ...]]:
    """Return exact root-environment assumptions grouped by sampled domain.

    Automatic safety verification protocol assumptions live in ``FormalDesign`` rather than in
    the source verification overlay.  They still form part of the exact
    environment for every root goal in that domain and therefore need the same
    declared membership as source-authored module requirements.
    """

    grouped: dict[tuple[str, str | None], list[ir_formal.FormalProperty]] = {}
    for item in result.formal_design.properties:
        if item.kind is not ir_formal.PropertyKind.ASSUMPTION:
            continue
        grouped.setdefault(_root_property_domain(result, item), []).append(item)
    return {
        key: tuple(dict.fromkeys(values))
        for key, values in grouped.items()
    }


def _scope_payload(result: CompilationResult) -> list[dict[str, object]]:
    scopes = [
        {
            "id": scope.semantic_id,
            "name": scope.name,
            "clock": scope.clock,
            "reset": scope.reset,
            "requirements": [
                {
                    "id": item.semantic_id,
                    "name": item.name,
                    "source_origin": (
                        None if item.source_origin is None else item.source_origin.to_data()
                    ),
                }
                for item in scope.requirements
            ],
            "goals": [
                {
                    "id": item.semantic_id,
                    "kind": item.kind.value,
                    "name": item.name,
                    "source_origin": (
                        None if item.source_origin is None else item.source_origin.to_data()
                    ),
                }
                for item in scope.goals
            ],
            "source_origin": (
                None if scope.source_origin is None else scope.source_origin.to_data()
            ),
        }
        for scope in result.ir.verification_scopes
    ]

    # Source-authored module requirements are already present above.  Automatic
    # root safety verification assumptions are not overlay declarations, so publish them in one
    # compiler-owned module-global scope per exact domain.  If that scope already
    # exists, extend it without duplicating the source requirement record.
    module_scope_by_domain = {
        (str(item["clock"]), str(item["reset"])): item
        for item in scopes
        if item["name"] == "$module"
    }
    declared_requirement_ids = {
        str(requirement["id"])
        for item in scopes
        for requirement in item["requirements"]
    }
    for (clock, reset), assumptions in sorted(
        _root_assumptions_by_domain(result).items(),
        key=lambda item: (item[0][0], item[0][1] or ""),
    ):
        if reset is None:
            # The existing verification-bundle scope schema is reset-domain
            # based.  A clocked automatic assumption without a reset cannot be
            # silently represented as belonging to another domain.
            raise ir_formal.FormalError(
                f"root formal assumption '{assumptions[0].id}' has no reset domain"
            )
        additions = [
            {
                "id": item.id,
                "name": item.generated_from or item.id,
                "source_origin": prepared_routes._origin_payload(item.source_origin),
            }
            for item in assumptions
            if item.id not in declared_requirement_ids
        ]
        if not additions:
            continue
        scope = module_scope_by_domain.get((clock, reset))
        if scope is None:
            scope = {
                "id": "root-formal-scope:" + stable_digest({
                    "module": result.ir.name,
                    "clock": clock,
                    "reset": reset,
                }, length=24),
                "name": "$module",
                "clock": clock,
                "reset": reset,
                "requirements": [],
                "goals": [],
                "source_origin": None,
            }
            scopes.append(scope)
            module_scope_by_domain[(clock, reset)] = scope
        scope["requirements"].extend(additions)
        declared_requirement_ids.update(str(item["id"]) for item in additions)
    return scopes


def _goal_scope_metadata(
    result: CompilationResult,
) -> tuple[
    dict[tuple[str, str], tuple[str, ...]],
    dict[str, tuple[str | None, tuple[str, ...]]],
]:
    """Return domain-local globals and exact source-scope membership.

    A source file may contain more than one physical clock/reset domain.  A
    module-global requirement belongs only to the domain in which it is
    sampled; applying the first module scope to every goal would silently mix
    clocks in one harness.  Local contract requirements are retained in the
    plan even though their executable predicate is already folded into the
    source goal by the verification overlay lowering.
    """

    globals_by_domain = {
        (scope.clock, scope.reset): tuple(
            item.semantic_id for item in scope.requirements
        )
        for scope in result.ir.verification_scopes
        if scope.name == "$module"
    }
    for domain, assumptions in _root_assumptions_by_domain(result).items():
        globals_by_domain[domain] = tuple(dict.fromkeys((
            *globals_by_domain.get(domain, ()),
            *(item.id for item in assumptions),
        )))
    by_property: dict[str, tuple[str | None, tuple[str, ...]]] = {}
    for scope in result.ir.verification_scopes:
        global_requirements = globals_by_domain.get(
            (scope.clock, scope.reset), ()
        )
        local = tuple(item.semantic_id for item in scope.requirements)
        for goal in scope.goals:
            assumptions = tuple(dict.fromkeys((*global_requirements, *local)))
            by_property[goal.semantic_id] = (scope.semantic_id, assumptions)
        if local:
            feasibility = f"{scope.semantic_id}.requirements_feasible"
            by_property[feasibility] = (
                scope.semantic_id,
                tuple(dict.fromkeys((*global_requirements, *local))),
            )
    return globals_by_domain, by_property


def _feasibility_recipe(
    predicates: tuple[formal_predicates.FormalPredicate, ...],
    *,
    clock: str,
    reset: str,
    route: prepared_routes._PreparedFormalRoute,
) -> str:
    """Identify one exact requirements-feasibility query across scopes."""

    return stable_digest(
        {
            "requirements": [item.to_data() for item in predicates],
            "clock": clock,
            "reset": reset,
            "backend": route.backend,
            "artifact": route.artifact_hash,
        },
        length=24,
    )


def _exact_goal_bindings(
    connected: ir_formal.FormalDesign,
    goal: object,
    assumptions: tuple[object, ...],
) -> tuple[object, ...]:
    # Every executable clocked harness samples under one exact physical reset
    # contract, even when the predicate itself has no explicit reset_condition.
    required: set[str] = {"clock", "reset"}
    required.update(getattr(goal, "relevant_signals", ()))
    for item in assumptions:
        required.update(getattr(item, "relevant_signals", ()))
    by_id = {
        item.semantic_signal_id: item for item in connected.bindings
    }
    missing = sorted(required - set(by_id))
    if missing:
        raise ir_formal.FormalError(
            f"connected goal is missing binding '{missing[0]}'"
        )
    return tuple(by_id[item] for item in sorted(required))


def _safety_verification_goal_recipe(
    result: CompilationResult,
    route: prepared_routes._PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> dict[str, object]:
    return {
        "schema": "zlang-safety_verification-exact-goal-recipe-v2",
        "selected_ir_identity": result.selected_ir_identity,
        "route": prepared_routes._prepared_route_fingerprint(route),
        "goal": prepared_routes._formal_property_recipe_payload(prop),
        "assumptions": [
            prepared_routes._formal_property_recipe_payload(item) for item in assumptions
        ],
        "cover": cover,
        "harness_schema": "zlang-safety_verification-structured-harness-v2",
    }


def _prepare_safety_verification_goal(
    route: prepared_routes._PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> prepared_routes._PreparedFormalGoal:
    connected_design = _connect_exact_goal(
        route, prop, assumptions, cover=cover,
    )
    if connected_design.connected_artifact_hash is None:
        raise ir_formal.FormalError(
            connected_design.non_executable_reason
            or f"{route.backend} exact goal route is not connected"
        )
    connected_goal = _route_property(
        connected_design, prop.id, cover=cover,
    )
    connected_assumption_by_id = {
        item.id: item
        for item in connected_design.properties
        if item.kind is ir_formal.PropertyKind.ASSUMPTION
    }
    exact_assumptions = tuple(
        connected_assumption_by_id[item.id] for item in assumptions
    )
    exact_bindings = _exact_goal_bindings(
        connected_design,
        connected_goal,
        exact_assumptions,
    )
    selected = replace(
        connected_design,
        properties=exact_assumptions + (() if cover else (connected_goal,)),
        covers=(connected_goal,) if cover else (),
    )
    checker_top = (
        ir_formal.cover_harness_top(selected, prop.id)
        if cover else f"{selected.module_name}__safety_verification_formal"
    )
    published_bindings = (
        *exact_bindings,
        *prepared_routes._checker_reset_trace_bindings(
            selected,
            top=checker_top,
            cover=cover,
        ),
    )
    binding_identity = prepared_routes._binding_identity(published_bindings)
    artifact_ref = formal_planning.FormalBackendArtifactRef(
        route.backend,
        route.artifact_hash,
        binding_identity,
    )
    executable_route = formal_planning.FormalExecutableRoute(
        formal_planning.FormalRouteKind.COVER_HARNESS
        if cover else formal_planning.FormalRouteKind.PROPERTY_HARNESS,
        (artifact_ref,),
    )
    suffix = prepared_routes._token(f"{prop.id}:{executable_route.identity}")
    harness_path = f"harness/{suffix}.sv"
    complete = (
        formal.emit_cover_harness(selected, cover_id=prop.id)
        if cover else formal.emit_harness(selected)
    )
    return prepared_routes._PreparedFormalGoal(
        published_bindings,
        binding_identity,
        executable_route,
        harness_path,
        _checker_only(complete, route.implementation),
    )


def _safety_verification_goal_fingerprint(goal: prepared_routes._PreparedFormalGoal) -> dict[str, object]:
    return {
        "route": goal.route.identity,
        "binding_identity": goal.binding_identity,
        "bindings": [
            prepared_routes._binding_recipe_payload(item) for item in goal.bindings
        ],
        "harness_path": goal.harness_path,
        "checker_hash": hashlib.sha256(goal.checker.encode("utf-8")).hexdigest(),
    }
