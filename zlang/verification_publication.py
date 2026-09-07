"""Compiler-owned publication of immutable first-class verification bundles."""

from __future__ import annotations

from zlang.backend.naming import RTL_NAMING_SCHEMA

from dataclasses import dataclass, replace
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory

from zlang.backend.clash import (
    ClashEmissionError,
    emit_formal_artifact as emit_clash_formal_artifact,
    finalize_formal_verilog_artifact,
)
from zlang.backend.companions import CompanionArtifactError
from zlang.backend.manifest import BackendArtifact
from zlang.backend.source_map import GeneratedSourceMap, build_generated_source_map
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_formal_artifact,
)
from zlang.compilation_products import CompilationResult
from zlang.common import stable_digest, stable_json
from zlang.candidate_equivalence import PreparedCandidateEquivalenceSite
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
)
from zlang.formal_orchestration import (
    CompilerFormalExecutionPlan,
    FormalOrchestrationError,
    build_compiler_formal_execution_plan,
)
from zlang.formal import (
    connect_formal_design,
    emit_cover_harness,
    emit_harness,
)
from zlang.ir.formal import (
    CoverProperty,
    FormalDesign,
    FormalError,
    FormalProperty,
    PropertyKind,
    SignalBinding,
    cover_harness_top,
    formal_harness_domain_rendering,
    generate_properties,
)
from zlang.ir.formal_ownership import (
    RecursiveAssumptionDisposition,
    resolve_recursive_assumption_ownership,
)
from zlang.ir.formal_observations import recursive_observation_id
from zlang.ir.formal_predicates import (
    FORMAL_PREDICATE_SCHEMA,
    Binary as PredicateBinary,
    FormalBinaryOperator,
    FormalPredicate,
    FormalSignedness,
    ObservationRef,
    map_observations,
)
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.cdc import ClockDomain, clock_domain_contract_identity
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
from zlang.ir.module import dependency_context_identity
from zlang.ir.types import StructType, TupleType, VecType
from zlang.ir.verification import verification_identity as overlay_verification_identity
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.toolchain import (
    ToolchainError,
    clash_subprocess_environment,
    find_clash_executable,
    generate_verilog,
)
from zlang.verification_bundle import (
    VerificationBundleInput,
    VerificationBundleManifest,
    VerificationJob,
    publish_verification_bundle,
    verification_identity_for,
)


_PATH_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class _PreparedFormalRoute:
    """One immutable backend artifact connected independently per property."""

    artifact: object
    connected: FormalDesign
    backend: str
    artifact_hash: str
    implementation: str
    implementation_path: str
    source_map_path: str
    source_map: str
    companion_paths: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedFormalGoal:
    """One exact binding set and checker emitted from one prepared route."""

    bindings: tuple[object, ...]
    binding_identity: str
    route: FormalExecutableRoute
    harness_path: str
    checker: str


@dataclass(frozen=True)
class _RecursiveRequirementClosure:
    """Exact implementation-owned RV dependency cone for one child scope.

    ``external`` requirements remain ordinary harness assumptions.  Each
    ``dependency`` pairs an internal automatic requirement with the existing
    source-endpoint guarantee that must be checked in the same root harness.
    Anything that cannot be expressed by that narrow relationship remains a
    fail-closed blocker.
    """

    external: tuple[object, ...]
    blockers: tuple[tuple[object, str], ...]
    dependencies: tuple[tuple[object, object], ...]


@dataclass(frozen=True)
class _RecursiveCoverGoal:
    """One existing feasibility predicate instantiated at a physical path."""

    concrete_property_id: str
    instance_identity: str
    physical_instance_path: tuple[str, ...]
    property: CoverProperty


@dataclass
class _RecursiveScopePublication:
    """Mutable publication accumulator; emitted deterministically at the end."""

    scope_id: str
    name: str
    clock: str
    reset: str
    physical_instance_path: tuple[str, ...]
    requirements: dict[str, dict[str, object]]
    goals: dict[str, dict[str, object]]
    source_origin: object | None


class _FormalRouteUnavailable(RuntimeError):
    """An environment-dependent route preparation failed without caching."""


def _binding_payload(binding: object) -> dict[str, object]:
    return {
        "semantic_signal_id": getattr(binding, "semantic_signal_id"),
        "rtl_module": getattr(binding, "rtl_module"),
        "rtl_name": getattr(binding, "rtl_name"),
        "width": getattr(binding, "width"),
        "direction": getattr(binding, "direction"),
    }


def _binding_recipe_payload(binding: object) -> dict[str, object]:
    return {
        **_binding_payload(binding),
        "clock_domain": getattr(binding, "clock_domain", None),
    }


def _binding_identity(bindings: tuple[object, ...]) -> str:
    return "bindings:" + stable_digest([
        _binding_payload(item)
        for item in sorted(
            bindings,
            key=lambda value: getattr(value, "semantic_signal_id"),
        )
    ])


def _checker_reset_trace_bindings(
    design: FormalDesign,
    *,
    top: str,
    cover: bool = False,
) -> tuple[SignalBinding, ...]:
    """Publish normalized and raw reset leaves from the exact checker plan."""

    by_id = {item.semantic_signal_id: item for item in design.bindings}
    raw_reset = by_id.get("reset")
    if raw_reset is None:
        raise FormalError("connected formal checker has no physical reset binding")
    rendered = formal_harness_domain_rendering(
        design,
        top=top,
        cover=cover,
    )
    return (
        SignalBinding(
            "trace:reset",
            top,
            rendered.reset_active,
            1,
            "internal" if rendered.reset_active != raw_reset.rtl_name else "input",
            raw_reset.clock_domain,
            raw_reset.source_origin,
        ),
        SignalBinding(
            "physical_reset",
            raw_reset.rtl_module,
            raw_reset.rtl_name,
            1,
            "input",
            raw_reset.clock_domain,
            raw_reset.source_origin,
        ),
    )


def _origin_payload(value: object | None) -> object | None:
    if value is None:
        return None
    to_data = getattr(value, "to_data", None)
    return to_data() if callable(to_data) else str(value)


def _formal_property_recipe_payload(value: object) -> dict[str, object]:
    def enum_value(name: str) -> object | None:
        item = getattr(value, name, None)
        return getattr(item, "value", item)

    predicate = getattr(value, "predicate", None)
    return {
        "id": getattr(value, "id"),
        "kind": enum_value("kind"),
        "clock": getattr(value, "clock"),
        "reset_condition": getattr(value, "reset_condition", None),
        "expression": getattr(value, "expression"),
        "temporal_form": enum_value("temporal_form"),
        "ownership": enum_value("ownership"),
        "classification": enum_value("classification"),
        "predicate": None if predicate is None else predicate.to_data(),
        "generated_from": getattr(value, "generated_from", None),
        "relevant_signals": list(getattr(value, "relevant_signals", ())),
        "antecedent": getattr(value, "antecedent", None),
        "consequent": getattr(value, "consequent", None),
        "min_delay": getattr(value, "min_delay", None),
        "max_delay": getattr(value, "max_delay", None),
        "non_executable_reason": getattr(value, "non_executable_reason", None),
        "source_origin": _origin_payload(getattr(value, "source_origin", None)),
    }


def _formal_design_recipe_payload(design: FormalDesign) -> dict[str, object]:
    return {
        "module": design.module_name,
        "properties": [
            _formal_property_recipe_payload(item) for item in design.properties
        ],
        "covers": [
            _formal_property_recipe_payload(item) for item in design.covers
        ],
        "bindings": [
            _binding_recipe_payload(item) for item in design.bindings
        ],
        "clock_domains": [
            {
                "clock": item.clock,
                "reset": item.reset,
                "edge": item.edge.value,
                "reset_mode": item.reset_mode.value,
                "reset_polarity": item.reset_polarity.value,
                "reset_release_mode": item.reset_release_mode.value,
                "reset_release_cycles": item.reset_release_cycles,
                "power_up": item.power_up.value,
            }
            for item in design.clock_domains
        ],
    }


def _recursive_formal_recipe_payload(result: CompilationResult) -> object | None:
    design = result.recursive_formal_design
    if design is None:
        return None
    to_dict = getattr(design, "to_dict", None)
    if not callable(to_dict):
        raise FormalError(
            "recursive formal design has no deterministic recipe serialization"
        )
    return to_dict()


def _prepared_route_recipe(
    result: CompilationResult,
    backend: str,
    *,
    tool_route: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": "zlang-verification-prepared-route-recipe-v1",
        "backend": backend,
        "selected_ir_identity": result.selected_ir_identity,
        "source_context": {
            "source_identity": result.ir.source_identity or result.ir.name,
            "source_hash": result.ir.source_hash,
            "dependency_identity": dependency_context_identity(result.ir),
        },
        "formal_design": _formal_design_recipe_payload(result.formal_design),
        "recursive_formal_design": _recursive_formal_recipe_payload(result),
        "compiler": {
            "version": _compiler_version(),
            "canonical_ir": CANONICAL_IR_IDENTITY_SCHEMA,
            "formal_predicate": FORMAL_PREDICATE_SCHEMA,
            # v7 binds rule-fire projection to the exact effective reset.
            # Older prepared artifacts can otherwise retain the raw active-high
            # predicate even when the production reset contract is different.
            "verification_publication": 7,
            "rtl_naming": RTL_NAMING_SCHEMA,
        },
        "tool_route": tool_route,
    }


def _prepared_route_fingerprint(route: _PreparedFormalRoute) -> dict[str, object]:
    companions = tuple(getattr(route.artifact, "companions", ()))
    artifact_manifest = json.loads(route.artifact.to_json())
    return {
        "backend": route.backend,
        "artifact_hash": route.artifact_hash,
        # RTL text can remain byte-identical while a formal-only physical ABI
        # correction changes its top module, public-domain ownership, or
        # observation locators.  Bind exact-goal caches to that complete
        # canonical metadata rather than to text and a partial binding list.
        "artifact_manifest_identity": stable_digest(artifact_manifest),
        "artifact_module": getattr(route.artifact, "module"),
        "bindings": [
            _binding_recipe_payload(item)
            for item in sorted(
                route.connected.bindings,
                key=lambda value: getattr(value, "semantic_signal_id"),
            )
        ],
        "implementation_hash": hashlib.sha256(
            route.implementation.encode("utf-8")
        ).hexdigest(),
        "source_map_hash": hashlib.sha256(
            route.source_map.encode("utf-8")
        ).hexdigest(),
        "companions": [
            {
                "logical_path": item.logical_path,
                "file_hash": item.file_hash,
                "text_hash": hashlib.sha256(item.text.encode("ascii")).hexdigest(),
            }
            for item in companions
        ],
    }


_PREPARED_ROUTE_CACHE_SCHEMA = "zlang-prepared-formal-route-v1"


def _encode_prepared_route(route: _PreparedFormalRoute) -> dict[str, object]:
    """Serialize every immutable input needed to replay one prepared route."""

    if not isinstance(route.artifact, BackendArtifact):
        raise FormalError("prepared formal route requires a BackendArtifact")
    if hashlib.sha256(route.artifact.text.encode("utf-8")).hexdigest() != (
        route.artifact.artifact_hash
    ):
        raise FormalError("prepared formal artifact text does not match its hash")
    # Parsing here validates the sidecar before it can be persisted.
    source_map = GeneratedSourceMap.from_json(route.source_map)
    if source_map.artifact_hash != route.artifact.artifact_hash:
        raise FormalError("prepared formal source map does not match its artifact")
    companions = tuple(route.artifact.companions)
    if len(route.companion_paths) != len(companions):
        raise FormalError("prepared formal route companion paths are incomplete")
    return {
        "schema": _PREPARED_ROUTE_CACHE_SCHEMA,
        "backend": route.backend,
        "artifact_hash": route.artifact_hash,
        "artifact_manifest": json.loads(route.artifact.to_json()),
        "artifact_text": route.artifact.text,
        "implementation": route.implementation,
        "implementation_path": route.implementation_path,
        "source_map_path": route.source_map_path,
        "source_map": json.loads(route.source_map),
        "companion_paths": list(route.companion_paths),
        "companions": [
            {
                "logical_path": item.logical_path,
                "file_hash": item.file_hash,
                "text": item.text,
            }
            for item in companions
        ],
    }


def _prepared_route_path(value: object, *, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise FormalError(f"cached prepared route {field} must be non-empty")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise FormalError(f"cached prepared route {field} is unsafe")
    if path.parts[0] != prefix:
        raise FormalError(
            f"cached prepared route {field} must be below '{prefix}/'"
        )
    return value


def _decode_prepared_route(
    result: CompilationResult,
    value: object,
) -> _PreparedFormalRoute:
    """Strictly restore a complete route without invoking either backend."""

    if not isinstance(value, dict):
        raise FormalError("cached prepared formal route must be an object")
    expected_fields = {
        "schema", "backend", "artifact_hash", "artifact_manifest",
        "artifact_text", "implementation", "implementation_path",
        "source_map_path", "source_map", "companion_paths", "companions",
    }
    if set(value) != expected_fields:
        raise FormalError("cached prepared formal route fields are invalid")
    if value.get("schema") != _PREPARED_ROUTE_CACHE_SCHEMA:
        raise FormalError("cached prepared formal route schema is unsupported")
    manifest = value.get("artifact_manifest")
    if not isinstance(manifest, dict):
        raise FormalError("cached prepared formal artifact manifest must be an object")
    artifact_text = value.get("artifact_text")
    implementation = value.get("implementation")
    source_map_data = value.get("source_map")
    if not isinstance(artifact_text, str) or not isinstance(implementation, str):
        raise FormalError("cached prepared formal route text must be UTF-8 text")
    if not isinstance(source_map_data, dict):
        raise FormalError("cached prepared formal source map must be an object")
    artifact = BackendArtifact.from_json(manifest)
    if hashlib.sha256(artifact_text.encode("utf-8")).hexdigest() != artifact.artifact_hash:
        raise FormalError("cached prepared formal artifact text hash does not match")

    raw_companions = value.get("companions")
    if not isinstance(raw_companions, list):
        raise FormalError("cached prepared formal companions must be an array")
    by_path: dict[str, dict[str, object]] = {}
    for item in raw_companions:
        if not isinstance(item, dict) or set(item) != {
            "logical_path", "file_hash", "text"
        }:
            raise FormalError("cached prepared formal companion fields are invalid")
        logical_path = item.get("logical_path")
        file_hash = item.get("file_hash")
        text = item.get("text")
        if (
            not isinstance(logical_path, str)
            or not isinstance(file_hash, str)
            or not isinstance(text, str)
        ):
            raise FormalError("cached prepared formal companion values are invalid")
        if logical_path in by_path:
            raise FormalError("cached prepared formal companion path is duplicated")
        by_path[logical_path] = item
    restored_companions = []
    for companion in artifact.companions:
        item = by_path.pop(companion.logical_path, None)
        if item is None or item["file_hash"] != companion.file_hash:
            raise FormalError(
                f"cached prepared companion '{companion.logical_path}' metadata differs"
            )
        try:
            restored_companions.append(replace(companion, text=item["text"]))
        except CompanionArtifactError as error:
            raise FormalError(str(error)) from error
    if by_path:
        raise FormalError("cached prepared formal route has an unknown companion")
    artifact = replace(
        artifact,
        text=artifact_text,
        companions=tuple(restored_companions),
    )
    if json.loads(artifact.to_json()) != manifest:
        raise FormalError("cached prepared formal artifact manifest does not round-trip")

    backend = value.get("backend")
    artifact_hash = value.get("artifact_hash")
    if backend != artifact.backend or artifact_hash != artifact.artifact_hash:
        raise FormalError("cached prepared formal route artifact identity differs")
    implementation_path = _prepared_route_path(
        value.get("implementation_path"),
        prefix="implementation",
        field="implementation path",
    )
    source_map_path = _prepared_route_path(
        value.get("source_map_path"),
        prefix="source-map",
        field="source-map path",
    )
    raw_paths = value.get("companion_paths")
    if not isinstance(raw_paths, list) or any(
        not isinstance(item, str) for item in raw_paths
    ):
        raise FormalError("cached prepared companion paths must be strings")
    companion_paths = tuple(
        _prepared_route_path(
            item,
            prefix="implementation",
            field="companion path",
        )
        for item in raw_paths
    )
    source_map = GeneratedSourceMap.from_json(source_map_data)
    if (
        source_map.backend != artifact.backend
        or source_map.module != artifact.module
        or source_map.selected_ir_identity != artifact.selected_ir_identity
        or source_map.artifact_hash != artifact.artifact_hash
    ):
        raise FormalError("cached prepared formal source map identity differs")

    # Reuse the same whole-design compatibility adapter as a fresh route.
    # A valid multi-domain artifact cannot be connected through the historical
    # shared ``clock``/``reset`` IDs until each exact goal is isolated; calling
    # ``connect_formal_design`` directly here made enabling the disk cache turn
    # otherwise executable per-goal routes into backend-unavailable failures.
    expected = _prepare_route(result, artifact)
    restored = _PreparedFormalRoute(
        artifact,
        expected.connected,
        artifact.backend,
        artifact.artifact_hash,
        implementation,
        implementation_path,
        source_map_path,
        source_map.to_json(),
        companion_paths,
    )
    if (
        restored.backend != expected.backend
        or restored.artifact_hash != expected.artifact_hash
        or restored.implementation != expected.implementation
        or restored.implementation_path != expected.implementation_path
        or restored.source_map_path != expected.source_map_path
        or restored.source_map != expected.source_map
        or restored.companion_paths != expected.companion_paths
    ):
        raise FormalError("cached prepared formal route is not canonical")
    return restored


def _prepare_route(result: CompilationResult, artifact: object) -> _PreparedFormalRoute:
    backend = str(getattr(artifact, "backend"))
    artifact_hash = str(
        getattr(artifact, "formal_artifact_hash", None)
        or getattr(artifact, "artifact_hash")
    )
    route_token = _token(f"{backend}:{artifact_hash}")
    implementation = str(getattr(artifact, "text"))
    try:
        connected = connect_formal_design(result.formal_design, artifact)
    except FormalError:
        # Generic harness-local IDs ``clock``/``reset`` are intentionally
        # reused by each independent goal.  A whole-design connection can
        # therefore collide for a valid multi-domain module; exact connection
        # happens later in ``_connect_exact_goal``.  Retain only immutable
        # artifact metadata here.  Single-domain/recursive routes keep the
        # richer connected view when it is available.
        connected = replace(
            result.formal_design,
            bindings=(),
            connected_backend=backend,
            connected_artifact_hash=artifact_hash,
            connected_module=str(getattr(artifact, "module")),
            implementation_text=implementation,
            dut_ports=(),
            non_executable_reason=None,
        )
    implementation = connected.implementation_text or implementation
    return _PreparedFormalRoute(
        artifact,
        connected,
        backend,
        artifact_hash,
        implementation,
        f"implementation/{route_token}/formal.sv",
        f"source-map/{route_token}.json",
        build_generated_source_map(result.ir, artifact).to_json(),
        tuple(
            f"implementation/companions/{route_token}/{item.logical_path}"
            for item in getattr(artifact, "companions", ())
        ),
    )


def _route_property(
    connected: FormalDesign,
    property_id: str,
    *,
    cover: bool,
) -> object:
    values = connected.covers if cover else connected.properties
    matches = tuple(item for item in values if item.id == property_id)
    if len(matches) != 1:
        raise FormalError(
            f"connected formal route has {len(matches)} records for '{property_id}'"
        )
    return matches[0]


def _connect_exact_goal(
    route: _PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> FormalDesign:
    """Connect one goal/domain without sharing generic clock/reset bindings.

    ``connect_formal_design`` intentionally names the clock and reset
    observations ``clock`` and ``reset`` inside one harness.  Connecting a
    whole multi-domain source design would therefore collide two otherwise
    valid physical domains before per-goal planning.  Each executable harness
    owns exactly one goal and its scoped assumptions, so connect that exact
    singleton here and never weaken or combine backend routes.
    """

    source = FormalDesign(
        module_name=route.connected.module_name,
        properties=assumptions + (() if cover else (prop,)),
        bindings=(),
        covers=(prop,) if cover else (),
        clock_domains=route.connected.clock_domains,
    )
    return connect_formal_design(source, route.artifact)


def _route_goal_reason(
    route: _PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> tuple[FormalSkipCode | None, str | None, tuple[str, ...]]:
    connected_design = _connect_exact_goal(
        route, prop=prop, assumptions=assumptions, cover=cover,
    )
    if connected_design.connected_artifact_hash is None:
        return (
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
            connected_design.non_executable_reason
            or f"{route.backend} formal artifact is not connected",
            (),
        )
    connected_assumptions = {
        item.id: item
        for item in connected_design.properties
        if item.kind is PropertyKind.ASSUMPTION
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
            FormalSkipCode.ASSUMPTION_UNAVAILABLE,
            f"{route.backend} route cannot bind required assumptions: {detail}",
            ids,
        )
    goal = _route_property(connected_design, prop.id, cover=cover)
    reason = goal.non_executable_reason or (
        None if goal.predicate is not None else "structured predicate unavailable"
    )
    if reason is not None:
        return (
            FormalSkipCode.OBSERVATION_UNAVAILABLE,
            f"{route.backend} route cannot bind goal '{prop.id}': {reason}",
            tuple(getattr(goal, "relevant_signals", ())),
        )
    return None, None, ()


def _token(value: str) -> str:
    readable = _PATH_TOKEN.sub("_", value).strip("._") or "goal"
    return f"{readable[:72]}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def _checker_only(complete: str, implementation: str) -> str:
    prefix = implementation.rstrip()
    if not complete.startswith(prefix):
        raise FormalError("verification harness does not contain the connected implementation")
    checker = complete[len(prefix):].lstrip("\r\n")
    if not checker:
        raise FormalError("verification harness contains no checker module")
    return checker


def _root_property_domain(
    result: CompilationResult,
    prop: FormalProperty | CoverProperty,
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
        raise FormalError(
            f"formal goal domain '{clock_domain}/{reset_domain}' is ambiguous"
        )
    return matches[0] if matches else None


def _route_physical_domain_identity(
    route: _PreparedFormalRoute,
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
        raise FormalError(
            "backend artifact publishes the formal goal physical domain more "
            "than once"
        )
    return expected if matches else None


def _available_physical_domain_identity(
    routes: tuple[_PreparedFormalRoute, ...],
    contract: ClockDomain | None,
) -> str | None:
    identities = {
        identity
        for item in routes
        if (identity := _route_physical_domain_identity(item, contract)) is not None
    }
    if len(identities) > 1:
        raise FormalError(
            "formal backend artifacts disagree on the physical-domain identity"
        )
    return next(iter(identities), None)


def _root_assumptions_by_domain(
    result: CompilationResult,
) -> dict[tuple[str, str | None], tuple[FormalProperty, ...]]:
    """Return exact root-environment assumptions grouped by sampled domain.

    Automatic M35 protocol assumptions live in ``FormalDesign`` rather than in
    the source verification overlay.  They still form part of the exact
    environment for every root goal in that domain and therefore need the same
    declared membership as source-authored module requirements.
    """

    grouped: dict[tuple[str, str | None], list[FormalProperty]] = {}
    for item in result.formal_design.properties:
        if item.kind is not PropertyKind.ASSUMPTION:
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
    # root M35 assumptions are not overlay declarations, so publish them in one
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
            raise FormalError(
                f"root formal assumption '{assumptions[0].id}' has no reset domain"
            )
        additions = [
            {
                "id": item.id,
                "name": item.generated_from or item.id,
                "source_origin": _origin_payload(item.source_origin),
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


def _recursive_scope_payload(result: CompilationResult) -> list[dict[str, object]]:
    """Describe descendant-instance M35 scopes without inventing properties."""

    design = result.recursive_formal_design
    if design is None:
        return []
    root_path = (result.ir.name,)
    descendants = tuple(
        item for item in design.properties
        if item.physical_instance_path != root_path
    )
    by_instance: dict[str, list[object]] = {}
    for item in descendants:
        by_instance.setdefault(item.instance_identity, []).append(item)
    nodes = {item.instance_identity: item for item in design.instances}
    scopes: list[dict[str, object]] = []
    for instance_identity in sorted(by_instance):
        node = nodes[instance_identity]
        properties = tuple(sorted(
            by_instance[instance_identity],
            key=lambda item: item.concrete_property_id,
        ))
        requirements = tuple(
            item for item in properties
            if item.property.kind is PropertyKind.ASSUMPTION
        )
        goals = tuple(
            item for item in properties
            if item.property.kind is PropertyKind.ASSERTION
        )
        if not goals:
            continue
        scope_id = f"recursive-scope:{instance_identity}"
        scopes.append({
            "id": scope_id,
            "name": "$recursive:" + ".".join(node.physical_instance_path),
            "clock": node.clock_domain or result.ir.clock or "clock",
            "reset": node.reset_domain or result.ir.reset or "reset",
            "requirements": [
                {
                    "id": item.concrete_property_id,
                    "name": item.source_property_id,
                    "source_origin": (
                        None if item.property.source_origin is None
                        else item.property.source_origin.to_data()
                    ),
                }
                for item in requirements
            ],
            "goals": [
                {
                    "id": item.concrete_property_id,
                    "kind": "assert",
                    "name": item.source_property_id,
                    "source_origin": (
                        None if item.property.source_origin is None
                        else item.property.source_origin.to_data()
                    ),
                }
                for item in goals
            ],
            "source_origin": (
                None if node.source_origin is None else node.source_origin.to_data()
            ),
        })
    return scopes


def _clash_version_for_recipe(executable: str) -> str:
    """Freeze only the Clash tool evidence relevant to route preparation."""

    try:
        completed = subprocess.run(
            (executable, "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=clash_subprocess_environment(executable),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"unavailable:{type(error).__name__}"
    output = (completed.stdout or completed.stderr).strip()
    return f"exit={completed.returncode}:{output}"


def _clash_recipe_context(
    result: CompilationResult,
) -> tuple[dict[str, object], str | None]:
    resolver = result.formal_tool_resolver
    resolve = getattr(resolver, "clash_context", None)
    if callable(resolve):
        context = resolve()
        return context.recipe_data, context.executable
    executable = find_clash_executable()
    return (
        {
            "executable": (
                None if executable is None else str(Path(executable).resolve())
            ),
            "version": (
                None
                if executable is None
                else _clash_version_for_recipe(executable)
            ),
        },
        executable,
    )


_DISCOVER_CLASH_EXECUTABLE = object()


def _try_clash_formal_fallback(
    result: CompilationResult,
    *,
    direct_reason: str,
    executable: str | None | object = _DISCOVER_CLASH_EXECUTABLE,
) -> tuple[object | None, str | None]:
    """Return an immutable validated Clash formal artifact or an exact reason.

    A verification bundle may publish only an immutable *Verilog* artifact
    whose public ports and formal observations have all been validated against
    that exact text/hash.  The structured Clash route is deliberately two
    phase: Haskell first, followed by real RTL generation and exact port
    validation.  ``finalize_formal_verilog_artifact`` republishes only that
    validated bounded subset; unsupported Clash shapes remain fail-closed.
    """

    if executable is _DISCOVER_CLASH_EXECUTABLE:
        executable = find_clash_executable()
    if executable is None:
        return None, (
            f"{direct_reason}; Clash formal fallback is unavailable because "
            "the Clash executable was not found; set ZLANG_CLASH or "
            "ZLANG_CLASH_ROOT"
        )
    assert isinstance(executable, str)
    try:
        candidate = emit_clash_formal_artifact(
            result.ir,
            result.recursive_formal_design,
            selected_ir_identity=result.selected_ir_identity,
        )
        with TemporaryDirectory(prefix="zlang-verification-clash-") as temporary:
            files = generate_verilog(
                candidate.text,
                result.ir.name,
                Path(temporary),
                executable,
                companions=candidate.companions,
            )
            finalized = finalize_formal_verilog_artifact(candidate, files)
    except (ClashEmissionError, ToolchainError, OSError, ValueError) as error:
        return None, (
            f"{direct_reason}; Clash formal fallback generated no fully "
            f"validated common artifact: {error}"
        )
    return finalized, None


def _clash_fallback_unavailable_reason(
    result: CompilationResult,
    *,
    direct_reason: str,
) -> str:
    """Compatibility helper returning only the fail-closed reason."""

    artifact, reason = _try_clash_formal_fallback(
        result,
        direct_reason=direct_reason,
    )
    return reason or (
        f"{direct_reason}; Clash formal fallback is available as a validated "
        f"{getattr(artifact, 'backend', 'Clash')} artifact"
    )


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


def _conjoin_predicates(values: tuple[FormalPredicate, ...]) -> FormalPredicate:
    if not values:
        raise FormalError("a recursive feasibility cover requires a predicate")
    result = values[0]
    for value in values[1:]:
        result = PredicateBinary(
            FormalBinaryOperator.LOGICAL_AND,
            result,
            value,
            1,
            FormalSignedness.BIT,
        )
    return result


def _guard_recursive_property(
    concrete: object,
    requirements: tuple[object, ...],
) -> object:
    """Guard one recursive assertion without turning requirements into assumes."""

    if not requirements:
        return concrete
    predicate = concrete.property.predicate
    requirement_predicates = tuple(
        item.property.predicate for item in requirements
        if item.property.predicate is not None
    )
    if predicate is None or len(requirement_predicates) != len(requirements):
        return concrete
    guard = _conjoin_predicates(requirement_predicates)
    guarded = PredicateBinary(
        FormalBinaryOperator.IMPLIES,
        guard,
        predicate,
        1,
        FormalSignedness.BIT,
    )
    return replace(
        concrete,
        property=replace(
            concrete.property,
            expression=f"({guard.render()}) -> ({predicate.render()})",
            predicate=guarded,
            relevant_signals=guarded.observation_ids(),
        ),
    )


def _controlled_assumption_ids(concrete: object, module: object) -> tuple[str, ...]:
    """Return only the leaves whose driver an assumption may constrain.

    Automatic protocol predicates legitimately *observe* implementation-owned
    condition signals (for example ready) while constraining only the source's
    valid/payload behavior.  User-authored assumptions have already passed the
    semantic ownership check, so every non-clock/reset observation is a
    controlled leaf and must reach a real root environment input.
    """

    generated = concrete.property.generated_from or ""
    refs = tuple(
        item.local_semantic_id
        for item in concrete.object_refs
        if item.local_semantic_id not in {"clock", "reset"}
    )
    if generated.startswith("ready_valid:"):
        endpoint = generated.removeprefix("ready_valid:")
        return tuple(
            item for item in refs
            if item in {
                f"port:{endpoint}.{ReadyValidSignal.VALID.value}",
                f"port:{endpoint}.{ReadyValidSignal.PAYLOAD.value}",
            }
        )
    if generated.startswith("credit:"):
        endpoint = generated.split(":", 2)[1]
        port = next(
            (item for item in module.ports if item.name == endpoint), None
        )
        if port is None:
            return refs
        controlled = (
            {"payload", "send"}
            if port.direction.value == "input"
            else {"return"}
        )
        return tuple(
            item for item in refs
            if item.removeprefix(f"port:{endpoint}.") in controlled
        )
    return refs


def _exact_goal_bindings(
    connected: FormalDesign,
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
        raise FormalError(
            f"connected goal is missing binding '{missing[0]}'"
        )
    return tuple(by_id[item] for item in sorted(required))


def _m35_goal_recipe(
    result: CompilationResult,
    route: _PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> dict[str, object]:
    return {
        "schema": "zlang-m35-exact-goal-recipe-v2",
        "selected_ir_identity": result.selected_ir_identity,
        "route": _prepared_route_fingerprint(route),
        "goal": _formal_property_recipe_payload(prop),
        "assumptions": [
            _formal_property_recipe_payload(item) for item in assumptions
        ],
        "cover": cover,
        "harness_schema": "zlang-m35-structured-harness-v2",
    }


def _prepare_m35_goal(
    route: _PreparedFormalRoute,
    prop: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool,
) -> _PreparedFormalGoal:
    connected_design = _connect_exact_goal(
        route, prop, assumptions, cover=cover,
    )
    if connected_design.connected_artifact_hash is None:
        raise FormalError(
            connected_design.non_executable_reason
            or f"{route.backend} exact goal route is not connected"
        )
    connected_goal = _route_property(
        connected_design, prop.id, cover=cover,
    )
    connected_assumption_by_id = {
        item.id: item
        for item in connected_design.properties
        if item.kind is PropertyKind.ASSUMPTION
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
        cover_harness_top(selected, prop.id)
        if cover else f"{selected.module_name}__m35_formal"
    )
    published_bindings = (
        *exact_bindings,
        *_checker_reset_trace_bindings(
            selected,
            top=checker_top,
            cover=cover,
        ),
    )
    binding_identity = _binding_identity(published_bindings)
    artifact_ref = FormalBackendArtifactRef(
        route.backend,
        route.artifact_hash,
        binding_identity,
    )
    executable_route = FormalExecutableRoute(
        FormalRouteKind.COVER_HARNESS
        if cover else FormalRouteKind.PROPERTY_HARNESS,
        (artifact_ref,),
    )
    suffix = _token(f"{prop.id}:{executable_route.identity}")
    harness_path = f"harness/{suffix}.sv"
    complete = (
        emit_cover_harness(selected, cover_id=prop.id)
        if cover else emit_harness(selected)
    )
    return _PreparedFormalGoal(
        published_bindings,
        binding_identity,
        executable_route,
        harness_path,
        _checker_only(complete, route.implementation),
    )


def _m35_goal_fingerprint(goal: _PreparedFormalGoal) -> dict[str, object]:
    return {
        "route": goal.route.identity,
        "binding_identity": goal.binding_identity,
        "bindings": [
            _binding_recipe_payload(item) for item in goal.bindings
        ],
        "harness_path": goal.harness_path,
        "checker_hash": hashlib.sha256(goal.checker.encode("utf-8")).hexdigest(),
    }


def _route_inputs(route: _PreparedFormalRoute) -> tuple[VerificationBundleInput, ...]:
    companions = tuple(getattr(route.artifact, "companions", ()))
    return (
        VerificationBundleInput(
            route.implementation_path,
            "implementation",
            route.implementation.encode("utf-8"),
        ),
        VerificationBundleInput(
            route.source_map_path,
            "source_map",
            route.source_map.encode("utf-8"),
        ),
        *(
            VerificationBundleInput(path, "companion", item.text.encode("ascii"))
            for path, item in zip(route.companion_paths, companions, strict=True)
        ),
    )


def _recursive_goal_design(
    result: CompilationResult,
    route: _PreparedFormalRoute,
    concrete: object,
    assumptions: tuple[object, ...],
    *,
    cover: bool = False,
    supporting_assertions: tuple[object, ...] = (),
    target_requirements: tuple[object, ...] = (),
) -> tuple[
    FormalDesign | None,
    tuple[SignalBinding, ...],
    FormalSkipCode | None,
    str | None,
]:
    """Connect one existing recursive M35 property to one backend only.

    Recursive semantic predicates already use concrete observation identities.
    This adapter resolves those identities exclusively through the v4 backend
    manifests and then reuses the ordinary structured M35 harness emitter.  It
    never derives a locator from an RTL/instance name.
    """

    if route.connected.connected_artifact_hash is None:
        return (
            None,
            (),
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
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
            FormalSkipCode.DOMAIN_UNSUPPORTED,
            f"{route.backend} recursive route has no explicit public clock",
        )

    target = (
        concrete
        if cover else _guard_recursive_property(concrete, target_requirements)
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
            FormalSkipCode.RESET_UNSUPPORTED,
            f"{route.backend} recursive route has no explicit public reset",
        )
    # Every clocked executable harness needs the exact physical reset contract,
    # even when a reset-epoch predicate observes a concrete child reset rather
    # than spelling ``reset_condition`` on the property itself.
    exact: list[SignalBinding] = [public_clock, public_reset]

    observation_ports: list[SignalBinding] = []
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
                FormalSkipCode.BINDING_UNAVAILABLE,
                f"recursive semantic binding is unavailable: {semantic_id}",
            )
        local_id = binding.local_semantic_id
        if local_id == "clock":
            physical = SignalBinding(
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
                    FormalSkipCode.RESET_UNSUPPORTED,
                    f"recursive reset binding is unavailable: {semantic_id}",
                )
            physical = SignalBinding(
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
                    FormalSkipCode.OBSERVATION_UNAVAILABLE,
                    f"recursive formal observation is unavailable: {semantic_id}",
                )
            physical = SignalBinding(
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
                FormalSkipCode.ASSUMPTION_UNAVAILABLE
                if item in assumptions
                else FormalSkipCode.OBSERVATION_UNAVAILABLE
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
                FormalSkipCode.ASSUMPTION_UNAVAILABLE
                if item in assumptions
                else FormalSkipCode.OBSERVATION_UNAVAILABLE
            )
            return None, (), code, item.property.non_executable_reason

    observation_modules = {
        item.rtl_module for item in observation_ports
    }
    if len(observation_modules) > 1:
        return (
            None,
            (),
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
            f"{route.backend} recursive observations span multiple RTL modules",
        )
    connected_module = next(
        iter(observation_modules), route.connected.connected_module
    )
    if connected_module is None:
        return (
            None,
            (),
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
            f"{route.backend} recursive route has no validated formal top",
        )
    design = FormalDesign(
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


def publish_compilation_verification_bundle(
    result: CompilationResult,
    directory: Path,
    *,
    compiler_execution_plan: CompilerFormalExecutionPlan | None = None,
    prepared_candidate_equivalence: tuple[
        PreparedCandidateEquivalenceSite, ...
    ] = (),
) -> VerificationBundleManifest:
    """Plan every current M35/source goal and publish immutable routes.

    A goal is connected to direct SystemVerilog first.  Clash is prepared
    lazily for only the goals whose complete observation/assumption set is not
    available in the direct artifact.  Each harness contains one implementation
    and one exact binding set; backend signals are never mixed.
    """

    source_design = result.formal_design
    source_assumptions = tuple(
        item for item in source_design.properties
        if item.kind is PropertyKind.ASSUMPTION
    )
    source_safety = tuple(
        item for item in source_design.properties
        if item.kind is PropertyKind.ASSERTION
    )
    source_covers = source_design.covers
    recursive_design = result.recursive_formal_design
    root_path = (result.ir.name,)
    recursive_properties = tuple(
        item for item in getattr(recursive_design, "properties", ())
        if item.physical_instance_path != root_path
    )
    recursive_safety = tuple(
        item for item in recursive_properties
        if item.property.kind is PropertyKind.ASSERTION
    )
    recursive_assumptions_by_instance: dict[str, tuple[object, ...]] = {}
    for instance_identity in sorted({
        item.instance_identity for item in recursive_properties
    }):
        recursive_assumptions_by_instance[instance_identity] = tuple(
            item for item in recursive_properties
            if item.instance_identity == instance_identity
            and item.property.kind is PropertyKind.ASSUMPTION
        )
    recursive_nodes = {
        item.instance_identity: item
        for item in getattr(recursive_design, "instances", ())
    }
    hierarchy = build_hierarchy_index(result.ir)
    local_scope_cache: dict[
        tuple[tuple[str, ...], str],
        tuple[tuple[object, ...], FormalPredicate | None, str | None],
    ] = {}

    def recursive_local_requirements(
        concrete: object,
    ) -> tuple[tuple[object, ...], FormalPredicate | None, str | None]:
        generated = concrete.property.generated_from or ""
        parts = generated.split(":")
        if (
            len(parts) < 3
            or parts[0] not in {"verification-assert", "verification-ensure"}
            or parts[1] == "$module"
        ):
            return (), None, None
        scope_name = parts[1]
        key = (concrete.physical_instance_path, scope_name)
        cached = local_scope_cache.get(key)
        if cached is not None:
            return cached
        entry = hierarchy.at(concrete.physical_instance_path)
        scope = next(
            (
                item for item in entry.module.verification_scopes
                if item.name == scope_name
            ),
            None,
        )
        if scope is None or not scope.requirements:
            result_ = ((), None, None)
            local_scope_cache[key] = result_
            return result_
        source_cover = next(
            (
                item for item in generate_properties(entry.module).covers
                if item.generated_from == f"verification-feasibility:{scope_name}"
            ),
            None,
        )
        if source_cover is None or source_cover.predicate is None:
            result_ = (
                tuple(scope.requirements),
                None,
                "recursive contract requirements have no structured feasibility predicate",
            )
            local_scope_cache[key] = result_
            return result_
        controlled = tuple(
            item for item in source_cover.predicate.observation_ids()
            if item not in {"clock", "reset"}
        )
        blocker: str | None = None
        if controlled:
            ownership = resolve_recursive_assumption_ownership(
                result.ir,
                concrete.physical_instance_path,
                controlled,
                automatic_protocol=False,
                hierarchy=hierarchy,
            )
            if ownership.disposition is not RecursiveAssumptionDisposition.EXTERNAL:
                blocker = (
                    "user-authored recursive requirement is not controlled by "
                    "the root environment: "
                    + (ownership.reason or "ownership is unresolved")
                )
        mapped = map_observations(
            source_cover.predicate,
            lambda observation: ObservationRef(
                recursive_observation_id(
                    concrete.instance_identity,
                    observation.semantic_signal_id,
                ),
                observation.width,
                observation.signedness,
                observation.cycle,
            ),
        )
        result_ = (tuple(scope.requirements), mapped, blocker)
        local_scope_cache[key] = result_
        return result_

    def recursive_scope_context(
        concrete: object,
        *,
        clock_domain: str,
        reset_domain: str,
    ) -> tuple[
        str,
        tuple[object, ...],
        tuple[str, ...],
        FormalPredicate | None,
        str | None,
        tuple[object, ...],
        tuple[object, ...],
    ]:
        closure = recursive_requirement_closure(concrete.instance_identity)
        external = closure.external
        blockers = closure.blockers
        dependencies = closure.dependencies
        immediate_requirements = tuple(
            item
            for item, _ in recursive_discharged_by_instance.get(
                concrete.instance_identity, ()
            )
        )
        supporting_assertions = recursive_supporting_assertions(closure)
        local_requirements, local_predicate, local_blocker = (
            recursive_local_requirements(concrete)
        )
        scope_key = {
            "path": list(concrete.physical_instance_path),
            "external": [item.concrete_property_id for item in external],
            "blocked": [item.concrete_property_id for item, _ in blockers],
            "internal_dependencies": [
                [
                    requirement.concrete_property_id,
                    guarantee.concrete_property_id,
                ]
                for requirement, guarantee in dependencies
            ],
            "local": [item.semantic_id for item in local_requirements],
            "clock": clock_domain,
            "reset": reset_domain,
        }
        scope_id = (
            f"recursive-scope:{concrete.instance_identity}"
            if (
                not external
                and not blockers
                and not dependencies
                and not local_requirements
            )
            else "recursive-scope:" + stable_digest(scope_key, length=24)
        )

        def scoped_requirement_id(source_id: str) -> str:
            return (
                f"{scope_id}.requirement."
                + stable_digest(source_id, length=16)
            )

        scoped_external_items: list[object] = []
        for item in external:
            entry = hierarchy.at(item.physical_instance_path)
            automatic = (item.property.generated_from or "").startswith(
                ("ready_valid:", "credit:")
            )
            root_by_recursive_id: dict[str, str] = {}
            for local_id in _controlled_assumption_ids(item, entry.module):
                leaf = resolve_recursive_assumption_ownership(
                    result.ir,
                    item.physical_instance_path,
                    (local_id,),
                    automatic_protocol=automatic,
                    hierarchy=hierarchy,
                )
                if (
                    leaf.disposition
                    is not RecursiveAssumptionDisposition.EXTERNAL
                    or len(leaf.root_leaves) != 1
                ):
                    raise FormalError(
                        "recursive external assumption ownership changed while "
                        f"planning '{item.concrete_property_id}'"
                    )
                root_by_recursive_id[recursive_observation_id(
                    item.instance_identity,
                    local_id,
                )] = leaf.root_leaves[0]
                # Aggregate payloads are flattened into physical leaves at the
                # root ABI.  The existing predicate observes the exact packed
                # payload and the recursive formal artifact already publishes
                # that value.  Keep that backend-published observation rather
                # than inventing a new aggregate root port or rebuilding a pack
                # expression in the orchestration layer.
                resolved_port = next(
                    (
                        port for port in entry.module.ports
                        if local_id.startswith(f"port:{port.name}.")
                    ),
                    None,
                )
                if (
                    resolved_port is not None
                    and local_id.endswith(".payload")
                    and isinstance(
                        resolved_port.type,
                        (StructType, TupleType, VecType),
                    )
                ):
                    root_by_recursive_id.pop(recursive_observation_id(
                        item.instance_identity,
                        local_id,
                    ))
            predicate = item.property.predicate
            if predicate is not None and root_by_recursive_id:
                predicate = map_observations(
                    predicate,
                    lambda observation: ObservationRef(
                        root_by_recursive_id.get(
                            observation.semantic_signal_id,
                            observation.semantic_signal_id,
                        ),
                        observation.width,
                        observation.signedness,
                        observation.cycle,
                    ),
                )
            requirement_id = scoped_requirement_id(item.concrete_property_id)
            scoped_external_items.append(replace(
                item,
                concrete_property_id=requirement_id,
                property=replace(
                    item.property,
                    id=requirement_id,
                    predicate=predicate,
                    relevant_signals=(
                        () if predicate is not None else item.property.relevant_signals
                    ),
                ),
            ))
        scoped_external = tuple(scoped_external_items)
        blocker_ids = tuple(
            scoped_requirement_id(item.concrete_property_id)
            for item, _ in blockers
        )
        local_ids = tuple(
            scoped_requirement_id(item.semantic_id)
            for item in local_requirements
        )
        assumption_ids = tuple(
            item.concrete_property_id for item in scoped_external
        ) + blocker_ids + local_ids
        node = recursive_nodes[concrete.instance_identity]
        publication = recursive_scopes.get(scope_id)
        if publication is None:
            publication = _RecursiveScopePublication(
                scope_id,
                "$recursive:" + ".".join(concrete.physical_instance_path),
                clock_domain,
                reset_domain,
                concrete.physical_instance_path,
                {},
                {},
                node.source_origin,
            )
            recursive_scopes[scope_id] = publication
        for source, scoped in zip(external, scoped_external, strict=True):
            publication.requirements[scoped.concrete_property_id] = {
                "id": scoped.concrete_property_id,
                "name": source.source_property_id,
                "source_origin": (
                    None
                    if source.property.source_origin is None
                    else source.property.source_origin.to_data()
                ),
            }
        for (source, _), requirement_id in zip(blockers, blocker_ids, strict=True):
            publication.requirements[requirement_id] = {
                "id": requirement_id,
                "name": source.source_property_id,
                "source_origin": (
                    None
                    if source.property.source_origin is None
                    else source.property.source_origin.to_data()
                ),
            }
        for source, requirement_id in zip(
            local_requirements, local_ids, strict=True
        ):
            publication.requirements[requirement_id] = {
                "id": requirement_id,
                "name": source.name,
                "source_origin": (
                    None
                    if source.source_origin is None
                    else source.source_origin.to_data()
                ),
            }
        blocker_reasons = tuple(reason for _, reason in blockers)
        if local_blocker is not None:
            blocker_reasons = (*blocker_reasons, local_blocker)
        return (
            scope_id,
            scoped_external,
            assumption_ids,
            local_predicate,
            "; ".join(blocker_reasons) if blocker_reasons else None,
            supporting_assertions,
            immediate_requirements,
        )
    guarantee_by_endpoint: dict[tuple[tuple[str, ...], str], str] = {}
    for item in source_safety:
        generated = item.generated_from or ""
        if generated.startswith("ready_valid:"):
            guarantee_by_endpoint[
                (root_path, generated.removeprefix("ready_valid:"))
            ] = item.id
    for item in recursive_safety:
        generated = item.property.generated_from or ""
        if generated.startswith("ready_valid:"):
            guarantee_by_endpoint[
                (
                    item.physical_instance_path,
                    generated.removeprefix("ready_valid:"),
                )
            ] = item.concrete_property_id

    recursive_external_by_instance: dict[str, tuple[object, ...]] = {}
    recursive_blockers_by_instance: dict[str, tuple[tuple[object, str], ...]] = {}
    recursive_discharged_by_instance: dict[str, tuple[tuple[object, str], ...]] = {}
    for instance_identity, assumptions in recursive_assumptions_by_instance.items():
        node = recursive_nodes[instance_identity]
        entry = hierarchy.at(node.physical_instance_path)
        external: list[object] = []
        blockers: list[tuple[object, str]] = []
        discharged: list[tuple[object, str]] = []
        for assumption in assumptions:
            controlled = _controlled_assumption_ids(assumption, entry.module)
            # A compile-time true requirement has no physical ownership.  A
            # compile-time false requirement was rejected during semantic
            # analysis, so retaining this record as an external assumption is
            # conservative and allows the ordinary feasibility query to be the
            # executable source of truth.
            if not controlled:
                external.append(assumption)
                continue
            automatic = (assumption.property.generated_from or "").startswith(
                ("ready_valid:", "credit:")
            )
            ownership = resolve_recursive_assumption_ownership(
                result.ir,
                assumption.physical_instance_path,
                controlled,
                automatic_protocol=automatic,
                hierarchy=hierarchy,
            )
            if ownership.disposition is RecursiveAssumptionDisposition.EXTERNAL:
                external.append(assumption)
                continue
            if ownership.disposition is RecursiveAssumptionDisposition.INTERNAL_GUARANTEED:
                guarantee_id = guarantee_by_endpoint.get((
                    ownership.guarantee_path or (),
                    ownership.guarantee_port or "",
                ))
                if guarantee_id is not None:
                    discharged.append((assumption, guarantee_id))
                    continue
                blockers.append((
                    assumption,
                    "internally driven automatic requirement has no published "
                    "exact M35 source-endpoint guarantee",
                ))
                continue
            blockers.append((
                assumption,
                ownership.reason
                or "recursive assumption ownership is unresolved",
            ))
        recursive_external_by_instance[instance_identity] = tuple(external)
        recursive_blockers_by_instance[instance_identity] = tuple(blockers)
        recursive_discharged_by_instance[instance_identity] = tuple(discharged)

    recursive_property_by_id = {
        item.concrete_property_id: item for item in recursive_properties
    }
    dependency_cache: dict[str, _RecursiveRequirementClosure] = {}

    def recursive_requirement_closure(
        instance_identity: str,
        stack: tuple[str, ...] = (),
    ) -> _RecursiveRequirementClosure:
        cached = dependency_cache.get(instance_identity)
        if cached is not None:
            return cached
        if instance_identity in stack:
            assumption = next(
                iter(recursive_assumptions_by_instance.get(instance_identity, ())),
                None,
            )
            if assumption is None:
                raise FormalError(
                    "recursive ready/valid requirement dependency cycle has no "
                    "source assumption"
                )
            return _RecursiveRequirementClosure(
                (),
                ((assumption, "recursive ready/valid requirement dependency cycle"),),
                (),
            )

        external = list(recursive_external_by_instance.get(instance_identity, ()))
        blockers = list(recursive_blockers_by_instance.get(instance_identity, ()))
        dependencies: list[tuple[object, object]] = []
        for requirement, guarantee_id in recursive_discharged_by_instance.get(
            instance_identity, ()
        ):
            if (
                requirement.property.predicate is None
                or requirement.property.non_executable_reason is not None
            ):
                blockers.append((
                    requirement,
                    requirement.property.non_executable_reason
                    or "internally driven automatic ready/valid requirement "
                    "has no structured predicate",
                ))
                continue
            guarantee = recursive_property_by_id.get(guarantee_id)
            if guarantee is None:
                blockers.append((
                    requirement,
                    "internally driven automatic requirement references an "
                    f"unavailable source guarantee '{guarantee_id}'",
                ))
                continue
            if (
                guarantee.property.predicate is None
                or guarantee.property.non_executable_reason is not None
            ):
                blockers.append((
                    requirement,
                    guarantee.property.non_executable_reason
                    or "source-endpoint ready/valid guarantee has no structured "
                    "predicate",
                ))
                continue
            requirement_node = recursive_nodes[instance_identity]
            guarantee_node = recursive_nodes[guarantee.instance_identity]
            if (
                requirement_node.clock_domain != guarantee_node.clock_domain
                or requirement_node.reset_domain != guarantee_node.reset_domain
            ):
                blockers.append((
                    requirement,
                    "internally driven automatic ready/valid requirement crosses "
                    "a clock/reset domain",
                ))
                continue
            upstream = recursive_requirement_closure(
                guarantee.instance_identity,
                (*stack, instance_identity),
            )
            external.extend(upstream.external)
            blockers.extend(upstream.blockers)
            dependencies.extend(upstream.dependencies)
            dependencies.append((requirement, guarantee))

        def unique(items: list[object]) -> tuple[object, ...]:
            by_id: dict[str, object] = {}
            for item in items:
                by_id.setdefault(item.concrete_property_id, item)
            return tuple(by_id[key] for key in sorted(by_id))

        unique_blockers: dict[str, tuple[object, str]] = {}
        for item, reason in blockers:
            unique_blockers.setdefault(item.concrete_property_id, (item, reason))
        unique_dependencies: dict[str, tuple[object, object]] = {}
        for requirement, guarantee in dependencies:
            unique_dependencies.setdefault(
                requirement.concrete_property_id,
                (requirement, guarantee),
            )
        closure = _RecursiveRequirementClosure(
            unique(external),
            tuple(unique_blockers[key] for key in sorted(unique_blockers)),
            tuple(
                unique_dependencies[key]
                for key in sorted(unique_dependencies)
            ),
        )
        dependency_cache[instance_identity] = closure
        return closure

    def recursive_supporting_assertions(
        closure: _RecursiveRequirementClosure,
    ) -> tuple[object, ...]:
        result_: list[object] = []
        seen: set[str] = set()
        for _, guarantee in closure.dependencies:
            if guarantee.concrete_property_id in seen:
                continue
            immediate_requirements = tuple(
                item
                for item, _ in recursive_discharged_by_instance.get(
                    guarantee.instance_identity, ()
                )
            )
            result_.append(_guard_recursive_property(
                guarantee,
                immediate_requirements,
            ))
            seen.add(guarantee.concrete_property_id)
        return tuple(result_)

    if not source_safety and not source_covers and not recursive_safety:
        raise FormalError("verification bundle requires at least one safety or cover goal")

    provider = getattr(result, "formal_artifact_provider", None)
    if not isinstance(provider, FormalArtifactProvider):
        provider = FormalArtifactProvider()

    globals_by_domain, scope_metadata = _goal_scope_metadata(result)
    assumption_by_id = {item.id: item for item in source_assumptions}
    # First-class contract requirements are already lowered into each local
    # goal predicate and its feasibility cover.  Legacy module-global
    # ``assume`` declarations remain separate FormalProperty records and must
    # never be treated as folded merely because their overlay metadata exists.
    folded_requirement_ids = {
        item.semantic_id
        for scope in result.ir.verification_scopes
        if scope.name != "$module"
        for item in scope.requirements
    }

    def scoped_assumption_membership(
        prop: object,
    ) -> tuple[str | None, tuple[str, ...]]:
        clock_domain, reset_domain = _root_property_domain(result, prop)
        default_ids = globals_by_domain.get(
            (clock_domain, reset_domain), ()
        )
        return scope_metadata.get(prop.id, (None, default_ids))

    def resolved_scoped_assumptions(
        prop: object,
    ) -> tuple[str | None, tuple[str, ...], tuple[object, ...], tuple[str, ...]]:
        scope_id, assumption_ids = scoped_assumption_membership(prop)
        missing = tuple(
            item for item in assumption_ids
            if item not in assumption_by_id and item not in folded_requirement_ids
        )
        assumptions = tuple(
            assumption_by_id[item]
            for item in assumption_ids
            if item in assumption_by_id
        )
        return scope_id, assumption_ids, assumptions, missing

    direct_route: _PreparedFormalRoute | None = None
    direct_failure: str | None = None
    try:
        direct_route = provider.get_or_prepare(
            FormalArtifactNamespace.PREPARED,
            "direct-systemverilog-formal-route-v1",
            _prepared_route_recipe(result, "direct_systemverilog"),
            lambda: _prepare_route(
                result,
                emit_formal_artifact(
                    result.ir,
                    result.recursive_formal_design,
                    selected_ir_identity=result.selected_ir_identity,
                ),
            ),
            encode=_encode_prepared_route,
            decode=lambda value: _decode_prepared_route(result, value),
            fingerprint=_prepared_route_fingerprint,
        )
    except (SystemVerilogEmissionError, FormalError, ValueError) as error:
        direct_failure = f"direct-SystemVerilog formal route is unavailable: {error}"

    def direct_unresolved(prop: object, *, cover: bool) -> bool:
        _, _, assumptions, missing = resolved_scoped_assumptions(prop)
        # No backend can repair a missing semantic assumption.  ``add_goal``
        # publishes the exact fail-closed skip without needlessly probing
        # Clash for this goal.
        if missing:
            return False
        if direct_route is None:
            return True
        return _route_goal_reason(
            direct_route,
            prop,
            assumptions,
            cover=cover,
        )[0] is not None

    needs_clash = any(
        direct_unresolved(item, cover=False) for item in source_safety
    ) or any(
        direct_unresolved(item, cover=True) for item in source_covers
    )
    if direct_route is None:
        needs_clash = needs_clash or bool(recursive_safety)
    else:
        for item in recursive_safety:
            closure = recursive_requirement_closure(item.instance_identity)
            if closure.blockers:
                continue
            supporting = recursive_supporting_assertions(closure)
            immediate = tuple(
                requirement
                for requirement, _ in recursive_discharged_by_instance.get(
                    item.instance_identity, ()
                )
            )
            if _recursive_goal_design(
                result,
                direct_route,
                item,
                closure.external,
                supporting_assertions=supporting,
                target_requirements=immediate,
            )[0] is None:
                needs_clash = True
                break
    clash_route: _PreparedFormalRoute | None = None
    clash_failure: str | None = None
    if needs_clash:
        fallback_reason = direct_failure or (
            "direct-SystemVerilog formal route lacks one or more required "
            "goal/assumption observations"
        )
        clash_tool_route, clash_executable = _clash_recipe_context(result)

        def prepare_clash_route() -> _PreparedFormalRoute:
            artifact, reason = _try_clash_formal_fallback(
                result,
                direct_reason=fallback_reason,
                executable=clash_executable,
            )
            if artifact is None:
                raise _FormalRouteUnavailable(
                    reason or f"{fallback_reason}; Clash formal route is unavailable"
                )
            return _prepare_route(result, artifact)

        try:
            clash_route = provider.get_or_prepare(
                FormalArtifactNamespace.PREPARED,
                "clash-formal-route-v1",
                _prepared_route_recipe(
                    result,
                    "clash",
                    tool_route=clash_tool_route,
                ),
                prepare_clash_route,
                encode=_encode_prepared_route,
                decode=lambda value: _decode_prepared_route(result, value),
                fingerprint=_prepared_route_fingerprint,
            )
        except _FormalRouteUnavailable as error:
            clash_failure = str(error)
        except (FormalError, ValueError) as error:
            clash_failure = (
                f"{fallback_reason}; Clash formal route is unavailable: {error}"
            )

    jobs: list[VerificationJob] = []
    goal_plans: list[FormalGoalPlan] = []
    input_by_path: dict[str, VerificationBundleInput] = {}
    binding_sets: dict[str, dict[str, object]] = {}
    used_artifacts: set[tuple[str, str]] = set()
    recursive_scopes: dict[str, _RecursiveScopePublication] = {}
    recursive_cover_properties: dict[str, _RecursiveCoverGoal] = {}
    recursive_cover_by_recipe: dict[str, str] = {}
    recursive_vacuity_dependencies: dict[str, str] = {}
    root_feasibility_properties: dict[str, CoverProperty] = {}
    root_feasibility_by_recipe: dict[str, str] = {}
    root_vacuity_dependencies: dict[str, str] = {}

    def add_input(item: VerificationBundleInput) -> None:
        previous = input_by_path.get(item.logical_path)
        if previous is not None and previous != item:
            raise FormalError(
                f"verification routes publish different contents for '{item.logical_path}'"
            )
        input_by_path[item.logical_path] = item

    def add_goal(prop: object, *, cover: bool) -> None:
        clock_domain, reset_domain = _root_property_domain(result, prop)
        clock_domain_contract = _exact_clock_domain_contract(
            result, clock_domain, reset_domain
        )
        candidate_routes = tuple(
            item for item in (direct_route, clash_route) if item is not None
        )
        available_physical_domain_identity = (
            _available_physical_domain_identity(
                candidate_routes, clock_domain_contract
            )
        )
        (
            scope_id,
            scoped_ids,
            harness_assumptions,
            missing_assumptions,
        ) = resolved_scoped_assumptions(prop)
        kind = FormalPlanGoalKind.COVER if cover else FormalPlanGoalKind.SAFETY
        required = set(getattr(prop, "relevant_signals", ()))
        for assumption in harness_assumptions:
            required.update(assumption.relevant_signals)
        required_observations = tuple(sorted(required))
        top = (
            cover_harness_top(source_design, prop.id)
            if cover else f"{source_design.module_name}__m35_formal"
        )
        if missing_assumptions:
            message = (
                "verification scope references unavailable assumption(s): "
                + ", ".join(missing_assumptions)
            )
            skip = FormalSkipReason(
                FormalSkipCode.ASSUMPTION_UNAVAILABLE,
                message,
                missing_assumptions,
            )
            goal_plans.append(FormalGoalPlan(
                prop.id,
                prop.id,
                kind,
                clock_domain,
                reset_domain,
                scoped_ids,
                required_observations,
                result.selected_ir_identity,
                ComparisonWindow.same_cycle(),
                1,
                skip_reason=skip,
                source_origin=prop.source_origin,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            jobs.append(VerificationJob(
                prop.id,
                "cover" if cover else "safety",
                top,
                executable=False,
                reason=(
                    f"{FormalSkipCode.ASSUMPTION_UNAVAILABLE.value}: {message}"
                ),
                source_origin=prop.source_origin,
                selected_ir_identity=result.selected_ir_identity,
                scope_id=scope_id,
                assumption_ids=scoped_ids,
                clock_domain=clock_domain,
                reset_domain=reset_domain,
                physical_instance_path=(result.ir.name,),
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            return
        chosen: _PreparedFormalRoute | None = None
        failures: list[tuple[FormalSkipCode, str, tuple[str, ...], str]] = []
        for route in candidate_routes:
            code, reason, related = _route_goal_reason(
                route,
                prop,
                harness_assumptions,
                cover=cover,
            )
            if code is None:
                chosen = route
                break
            assert reason is not None
            failures.append((code, reason, related, route.backend))

        if chosen is None:
            if failures:
                code = (
                    FormalSkipCode.ASSUMPTION_UNAVAILABLE
                    if any(item[0] is FormalSkipCode.ASSUMPTION_UNAVAILABLE
                           for item in failures)
                    else failures[-1][0]
                )
                message = "; ".join(item[1] for item in failures)
                related = tuple(dict.fromkeys(
                    value for item in failures for value in item[2]
                ))
                backend = "+".join(item[3] for item in failures)
            else:
                code = FormalSkipCode.BACKEND_UNAVAILABLE
                message = "; ".join(
                    item for item in (direct_failure, clash_failure) if item
                ) or "no backend can publish a connected formal artifact"
                related = ()
                backend = None
            skip = FormalSkipReason(code, message, related, backend)
            goal_plans.append(FormalGoalPlan(
                prop.id,
                prop.id,
                kind,
                clock_domain,
                reset_domain,
                scoped_ids,
                required_observations,
                result.selected_ir_identity,
                ComparisonWindow.same_cycle(),
                1,
                skip_reason=skip,
                source_origin=prop.source_origin,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            jobs.append(VerificationJob(
                prop.id,
                "cover" if cover else "safety",
                top,
                executable=False,
                reason=f"{code.value}: {message}",
                source_origin=prop.source_origin,
                selected_ir_identity=result.selected_ir_identity,
                scope_id=scope_id,
                assumption_ids=scoped_ids,
                clock_domain=clock_domain,
                reset_domain=reset_domain,
                physical_instance_path=(result.ir.name,),
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            return

        prepared_goal = provider.get_or_prepare(
            FormalArtifactNamespace.M35,
            "exact-property-harness-v1",
            _m35_goal_recipe(
                result,
                chosen,
                prop,
                harness_assumptions,
                cover=cover,
            ),
            lambda: _prepare_m35_goal(
                chosen,
                prop,
                harness_assumptions,
                cover=cover,
            ),
            fingerprint=_m35_goal_fingerprint,
        )
        goal_plans.append(FormalGoalPlan(
            prop.id,
            prop.id,
            kind,
            clock_domain,
            reset_domain,
            scoped_ids,
            required_observations,
            result.selected_ir_identity,
            ComparisonWindow.same_cycle(),
            1,
            route=prepared_goal.route,
            source_origin=prop.source_origin,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))

        add_input(VerificationBundleInput(
            prepared_goal.harness_path,
            "harness",
            prepared_goal.checker.encode("utf-8"),
        ))
        artifact_key = (chosen.backend, chosen.artifact_hash)
        if artifact_key not in used_artifacts:
            for item in _route_inputs(chosen):
                add_input(item)
            used_artifacts.add(artifact_key)
        binding_sets[prepared_goal.route.identity] = {
            "route": prepared_goal.route.identity,
            "backend": chosen.backend,
            "artifact_hash": chosen.artifact_hash,
            "binding_identity": prepared_goal.binding_identity,
            "bindings": [
                _binding_payload(item) for item in prepared_goal.bindings
            ],
        }
        jobs.append(VerificationJob(
            prop.id,
            "cover" if cover else "safety",
            top,
            (
                chosen.implementation_path,
                prepared_goal.harness_path,
                *chosen.companion_paths,
            ),
            source_map_files=(chosen.source_map_path,),
            source_origin=prop.source_origin,
            route=prepared_goal.route.identity,
            backend=chosen.backend,
            artifact_hash=chosen.artifact_hash,
            binding_identity=prepared_goal.binding_identity,
            selected_ir_identity=result.selected_ir_identity,
            scope_id=scope_id,
            assumption_ids=scoped_ids,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            physical_instance_path=(result.ir.name,),
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))

        # Automatic M35 environment assumptions are ordinary root-domain
        # requirements.  Like first-class and recursive requirements, every
        # non-empty exact set needs one independent feasibility query so a
        # bounded safety pass cannot be reported through a vacuous harness.
        # The cover intentionally contains no assumptions of its own.
        if (
            cover
            or not harness_assumptions
            or not any(item.id.startswith("m35.") for item in harness_assumptions)
        ):
            return
        if any(item.predicate is None for item in harness_assumptions):
            # ``_route_goal_reason`` should already have rejected this route;
            # keep this branch fail-closed if a malformed caller bypasses it.
            raise FormalError(
                "root automatic assumptions have no structured feasibility "
                "predicate"
            )
        feasibility_predicate = _conjoin_predicates(tuple(
            item.predicate for item in harness_assumptions
        ))
        feasibility_recipe = stable_digest({
            "requirements": [
                item.predicate.to_data() for item in harness_assumptions
            ],
            "clock": clock_domain,
            "reset": reset_domain,
            "backend": chosen.backend,
            "artifact": chosen.artifact_hash,
        }, length=24)
        existing_cover_id = root_feasibility_by_recipe.get(feasibility_recipe)
        if existing_cover_id is not None:
            root_vacuity_dependencies[prop.id] = existing_cover_id
            return
        feasibility_scope = scope_id or "$module"
        cover_id = (
            f"{feasibility_scope}.requirements_feasible."
            f"{feasibility_recipe}"
        )
        feasibility = CoverProperty(
            cover_id,
            clock_domain,
            reset_domain,
            feasibility_predicate.render(),
            feasibility_predicate,
            source_origin=prop.source_origin,
            generated_from=f"verification-feasibility:{feasibility_scope}",
        )
        root_feasibility_by_recipe[feasibility_recipe] = cover_id
        root_feasibility_properties[cover_id] = feasibility
        root_vacuity_dependencies[prop.id] = cover_id
        prepared_cover = provider.get_or_prepare(
            FormalArtifactNamespace.M35,
            "exact-property-harness-v1",
            _m35_goal_recipe(
                result, chosen, feasibility, (), cover=True
            ),
            lambda: _prepare_m35_goal(
                chosen, feasibility, (), cover=True
            ),
            fingerprint=_m35_goal_fingerprint,
        )
        goal_plans.append(FormalGoalPlan(
            cover_id,
            cover_id,
            FormalPlanGoalKind.COVER,
            clock_domain,
            reset_domain,
            # ``assumption_ids`` records declared scope membership throughout
            # the existing bundle schema (recursive feasibility covers use
            # the same convention).  The executable cover is nevertheless
            # prepared with ``()`` above and therefore assumes none of them.
            scoped_ids,
            tuple(sorted(feasibility.relevant_signals)),
            result.selected_ir_identity,
            ComparisonWindow.same_cycle(),
            1,
            route=prepared_cover.route,
            source_origin=prop.source_origin,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))
        add_input(VerificationBundleInput(
            prepared_cover.harness_path,
            "harness",
            prepared_cover.checker.encode("utf-8"),
        ))
        binding_sets[prepared_cover.route.identity] = {
            "route": prepared_cover.route.identity,
            "backend": chosen.backend,
            "artifact_hash": chosen.artifact_hash,
            "binding_identity": prepared_cover.binding_identity,
            "bindings": [
                _binding_payload(item) for item in prepared_cover.bindings
            ],
        }
        jobs.append(VerificationJob(
            cover_id,
            "cover",
            cover_harness_top(source_design, cover_id),
            (
                chosen.implementation_path,
                prepared_cover.harness_path,
                *chosen.companion_paths,
            ),
            source_map_files=(chosen.source_map_path,),
            source_origin=prop.source_origin,
            route=prepared_cover.route.identity,
            backend=chosen.backend,
            artifact_hash=chosen.artifact_hash,
            binding_identity=prepared_cover.binding_identity,
            selected_ir_identity=result.selected_ir_identity,
            scope_id=scope_id,
            assumption_ids=scoped_ids,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            physical_instance_path=(result.ir.name,),
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))

    def add_recursive_goal(concrete: object) -> None:
        """Publish one already-existing descendant M35 property.

        This is an orchestration adapter only: the property and all semantic
        observation identities were produced by the frozen recursive M35 IR.
        Each candidate backend must connect the complete set independently.
        """

        node = recursive_nodes.get(concrete.instance_identity)
        if node is None:
            raise FormalError(
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
        clock_domain_contract = _exact_clock_domain_contract(
            result, clock_domain, reset_domain
        )
        candidate_routes = tuple(
            item for item in (direct_route, clash_route) if item is not None
        )
        available_physical_domain_identity = (
            _available_physical_domain_identity(
                candidate_routes, clock_domain_contract
            )
        )
        (
            scope_id,
            assumptions,
            assumption_ids,
            local_requirement_predicate,
            ownership_blocker,
            supporting_assertions,
            target_requirements,
        ) = recursive_scope_context(
            concrete,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
        )
        publication = recursive_scopes[scope_id]
        publication.goals[concrete.concrete_property_id] = {
            "id": concrete.concrete_property_id,
            "kind": "assert",
            "name": concrete.source_property_id,
            "source_origin": (
                None
                if concrete.property.source_origin is None
                else concrete.property.source_origin.to_data()
            ),
        }
        required = {
            semantic_id
            for item in (
                *assumptions,
                *supporting_assertions,
                _guard_recursive_property(concrete, target_requirements),
            )
            for semantic_id in item.property.relevant_signals
        }
        required_observations = tuple(sorted(required))
        fallback_top = (
            f"{result.ir.name}__recursive_"
            f"{concrete.concrete_property_id[:12]}__m35_formal"
        )
        if ownership_blocker is not None:
            message = (
                "recursive scoped assumption ownership is unavailable: "
                + ownership_blocker
            )
            skip = FormalSkipReason(
                FormalSkipCode.ASSUMPTION_UNAVAILABLE,
                message,
                assumption_ids,
            )
            goal_plans.append(FormalGoalPlan(
                concrete.concrete_property_id,
                concrete.concrete_property_id,
                FormalPlanGoalKind.SAFETY,
                clock_domain,
                reset_domain,
                assumption_ids,
                required_observations,
                result.selected_ir_identity,
                ComparisonWindow.same_cycle(),
                1,
                skip_reason=skip,
                source_origin=concrete.property.source_origin,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            jobs.append(VerificationJob(
                concrete.concrete_property_id,
                "safety",
                fallback_top,
                executable=False,
                reason=f"{FormalSkipCode.ASSUMPTION_UNAVAILABLE.value}: {message}",
                source_origin=concrete.property.source_origin,
                selected_ir_identity=result.selected_ir_identity,
                scope_id=scope_id,
                assumption_ids=assumption_ids,
                clock_domain=clock_domain,
                reset_domain=reset_domain,
                physical_instance_path=concrete.physical_instance_path,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            return

        chosen: _PreparedFormalRoute | None = None
        connected_design: FormalDesign | None = None
        exact_bindings: tuple[SignalBinding, ...] = ()
        failures: list[tuple[FormalSkipCode, str, str]] = []
        for route in candidate_routes:
            design, bindings, code, reason = _recursive_goal_design(
                result,
                route,
                concrete,
                assumptions,
                supporting_assertions=supporting_assertions,
                target_requirements=target_requirements,
            )
            if design is not None:
                chosen = route
                connected_design = design
                exact_bindings = bindings
                break
            assert code is not None and reason is not None
            failures.append((code, reason, route.backend))

        if chosen is None or connected_design is None:
            if failures:
                code = (
                    FormalSkipCode.ASSUMPTION_UNAVAILABLE
                    if any(
                        item[0] is FormalSkipCode.ASSUMPTION_UNAVAILABLE
                        for item in failures
                    )
                    else failures[-1][0]
                )
                message = "; ".join(item[1] for item in failures)
                backend = "+".join(item[2] for item in failures)
            else:
                code = FormalSkipCode.BACKEND_UNAVAILABLE
                message = "; ".join(
                    item for item in (direct_failure, clash_failure) if item
                ) or "no backend can publish a connected recursive formal artifact"
                backend = None
            skip = FormalSkipReason(
                code,
                message,
                required_observations,
                backend,
            )
            goal_plans.append(FormalGoalPlan(
                concrete.concrete_property_id,
                concrete.concrete_property_id,
                FormalPlanGoalKind.SAFETY,
                clock_domain,
                reset_domain,
                assumption_ids,
                required_observations,
                result.selected_ir_identity,
                ComparisonWindow.same_cycle(),
                1,
                skip_reason=skip,
                source_origin=concrete.property.source_origin,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            jobs.append(VerificationJob(
                concrete.concrete_property_id,
                "safety",
                fallback_top,
                executable=False,
                reason=f"{code.value}: {message}",
                source_origin=concrete.property.source_origin,
                selected_ir_identity=result.selected_ir_identity,
                scope_id=scope_id,
                assumption_ids=assumption_ids,
                clock_domain=clock_domain,
                reset_domain=reset_domain,
                physical_instance_path=concrete.physical_instance_path,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=available_physical_domain_identity,
            ))
            return

        trace_bindings = _checker_reset_trace_bindings(
            connected_design,
            top=f"{connected_design.module_name}__m35_formal",
        )
        published_bindings = (*exact_bindings, *trace_bindings)
        binding_identity = _binding_identity(published_bindings)
        artifact_ref = FormalBackendArtifactRef(
            chosen.backend,
            chosen.artifact_hash,
            binding_identity,
        )
        executable_route = FormalExecutableRoute(
            FormalRouteKind.PROPERTY_HARNESS,
            (artifact_ref,),
        )
        goal_plans.append(FormalGoalPlan(
            concrete.concrete_property_id,
            concrete.concrete_property_id,
            FormalPlanGoalKind.SAFETY,
            clock_domain,
            reset_domain,
            assumption_ids,
            required_observations,
            result.selected_ir_identity,
            ComparisonWindow.same_cycle(),
            1,
            route=executable_route,
            source_origin=concrete.property.source_origin,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))
        suffix = _token(
            f"{concrete.concrete_property_id}:{executable_route.identity}"
        )
        harness_path = f"harness/{suffix}.sv"
        complete = emit_harness(connected_design)
        add_input(VerificationBundleInput(
            harness_path,
            "harness",
            _checker_only(complete, chosen.implementation).encode("utf-8"),
        ))
        artifact_key = (chosen.backend, chosen.artifact_hash)
        if artifact_key not in used_artifacts:
            for item in _route_inputs(chosen):
                add_input(item)
            used_artifacts.add(artifact_key)
        binding_record = {
            "route": executable_route.identity,
            "backend": chosen.backend,
            "artifact_hash": chosen.artifact_hash,
            "binding_identity": binding_identity,
            "bindings": [
                _binding_payload(item) for item in published_bindings
            ],
        }
        previous = binding_sets.get(executable_route.identity)
        if previous is not None and previous != binding_record:
            raise FormalError(
                "recursive formal route identity resolved to different bindings"
            )
        binding_sets[executable_route.identity] = binding_record
        jobs.append(VerificationJob(
            concrete.concrete_property_id,
            "safety",
            f"{connected_design.module_name}__m35_formal",
            (
                chosen.implementation_path,
                harness_path,
                *chosen.companion_paths,
            ),
            source_map_files=(chosen.source_map_path,),
            source_origin=concrete.property.source_origin,
            route=executable_route.identity,
            backend=chosen.backend,
            artifact_hash=chosen.artifact_hash,
            binding_identity=binding_identity,
            selected_ir_identity=result.selected_ir_identity,
            scope_id=scope_id,
            assumption_ids=assumption_ids,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            physical_instance_path=concrete.physical_instance_path,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))

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
            feasibility_predicate = _conjoin_predicates(feasibility_parts)
        feasibility_recipe = stable_digest({
            # Equivalent root-owned requirement predicates share one
            # feasibility query even when several descendant scopes depend on
            # the same physical input contract.
            "requirements": [item.to_data() for item in feasibility_parts],
            "clock": clock_domain,
            "reset": reset_domain,
            "backend": chosen.backend,
            "artifact": chosen.artifact_hash,
        }, length=24)
        existing_cover_id = recursive_cover_by_recipe.get(feasibility_recipe)
        if existing_cover_id is not None:
            recursive_vacuity_dependencies[
                concrete.concrete_property_id
            ] = existing_cover_id
            return
        cover_id = f"{scope_id}.requirements_feasible.{feasibility_recipe}"
        recursive_cover_by_recipe[feasibility_recipe] = cover_id
        cover = CoverProperty(
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
        cover_goal = _RecursiveCoverGoal(
            cover_id,
            concrete.instance_identity,
            concrete.physical_instance_path,
            cover,
        )
        recursive_cover_properties[cover_id] = cover_goal
        recursive_vacuity_dependencies[
            concrete.concrete_property_id
        ] = cover_id
        publication.goals[cover_id] = {
            "id": cover_id,
            "kind": "cover",
            "name": "requirements_feasible",
            "source_origin": (
                None
                if cover.source_origin is None
                else cover.source_origin.to_data()
            ),
        }
        cover_required = tuple(sorted(cover.relevant_signals))
        (
            cover_design,
            cover_bindings,
            cover_code,
            cover_reason,
        ) = _recursive_goal_design(
            result,
            chosen,
            cover_goal,
            (),
            cover=True,
        )
        if cover_design is None:
            code = cover_code or FormalSkipCode.ASSUMPTION_UNAVAILABLE
            message = cover_reason or "recursive feasibility cover is unavailable"
            goal_plans.append(FormalGoalPlan(
                cover_id,
                cover_id,
                FormalPlanGoalKind.COVER,
                clock_domain,
                reset_domain,
                assumption_ids,
                cover_required,
                result.selected_ir_identity,
                ComparisonWindow.same_cycle(),
                1,
                skip_reason=FormalSkipReason(
                    code,
                    message,
                    assumption_ids,
                    chosen.backend,
                ),
                source_origin=cover.source_origin,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=_route_physical_domain_identity(
                    chosen, clock_domain_contract
                ),
            ))
            jobs.append(VerificationJob(
                cover_id,
                "cover",
                cover_harness_top(
                    FormalDesign(result.ir.name, (), ()), cover_id
                ),
                executable=False,
                reason=f"{code.value}: {message}",
                source_origin=cover.source_origin,
                selected_ir_identity=result.selected_ir_identity,
                scope_id=scope_id,
                assumption_ids=assumption_ids,
                clock_domain=clock_domain,
                reset_domain=reset_domain,
                physical_instance_path=concrete.physical_instance_path,
                clock_domain_contract=clock_domain_contract,
                physical_domain_identity=_route_physical_domain_identity(
                    chosen, clock_domain_contract
                ),
            ))
            return

        cover_top = cover_harness_top(cover_design, cover_id)
        cover_trace_bindings = _checker_reset_trace_bindings(
            cover_design,
            top=cover_top,
            cover=True,
        )
        published_cover_bindings = (*cover_bindings, *cover_trace_bindings)
        cover_binding_identity = _binding_identity(published_cover_bindings)
        cover_artifact_ref = FormalBackendArtifactRef(
            chosen.backend,
            chosen.artifact_hash,
            cover_binding_identity,
        )
        cover_route = FormalExecutableRoute(
            FormalRouteKind.COVER_HARNESS,
            (cover_artifact_ref,),
        )
        goal_plans.append(FormalGoalPlan(
            cover_id,
            cover_id,
            FormalPlanGoalKind.COVER,
            clock_domain,
            reset_domain,
            assumption_ids,
            cover_required,
            result.selected_ir_identity,
            ComparisonWindow.same_cycle(),
            1,
            route=cover_route,
            source_origin=cover.source_origin,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))
        cover_harness_path = f"harness/{_token(f'{cover_id}:{cover_route.identity}')}.sv"
        add_input(VerificationBundleInput(
            cover_harness_path,
            "harness",
            _checker_only(
                emit_cover_harness(cover_design, cover_id=cover_id),
                chosen.implementation,
            ).encode("utf-8"),
        ))
        cover_binding_record = {
            "route": cover_route.identity,
            "backend": chosen.backend,
            "artifact_hash": chosen.artifact_hash,
            "binding_identity": cover_binding_identity,
            "bindings": [
                _binding_payload(item) for item in published_cover_bindings
            ],
        }
        previous_cover = binding_sets.get(cover_route.identity)
        if previous_cover is not None and previous_cover != cover_binding_record:
            raise FormalError(
                "recursive cover route identity resolved to different bindings"
            )
        binding_sets[cover_route.identity] = cover_binding_record
        jobs.append(VerificationJob(
            cover_id,
            "cover",
            cover_top,
            (
                chosen.implementation_path,
                cover_harness_path,
                *chosen.companion_paths,
            ),
            source_map_files=(chosen.source_map_path,),
            source_origin=cover.source_origin,
            route=cover_route.identity,
            backend=chosen.backend,
            artifact_hash=chosen.artifact_hash,
            binding_identity=cover_binding_identity,
            selected_ir_identity=result.selected_ir_identity,
            scope_id=scope_id,
            assumption_ids=assumption_ids,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            physical_instance_path=concrete.physical_instance_path,
            clock_domain_contract=clock_domain_contract,
            physical_domain_identity=_route_physical_domain_identity(
                chosen, clock_domain_contract
            ),
        ))

    for prop in source_safety:
        add_goal(prop, cover=False)
    for prop in source_covers:
        add_goal(prop, cover=True)
    for concrete in sorted(
        recursive_safety,
        key=lambda item: item.concrete_property_id,
    ):
        add_recursive_goal(concrete)

    # Preserve the historical single-route source-map view.  Mixed-route
    # bundles deliberately omit it because selecting one backend would be
    # ambiguous; each executable job already references its exact map.
    if len(used_artifacts) == 1:
        only_key = next(iter(used_artifacts))
        only_route = next(
            item for item in (direct_route, clash_route)
            if item is not None
            and (item.backend, item.artifact_hash) == only_key
        )
        input_by_path.pop(only_route.source_map_path, None)
        add_input(VerificationBundleInput(
            "source-map/formal.json",
            "source_map",
            only_route.source_map.encode("utf-8"),
        ))
        jobs = [
            replace(
                item,
                source_map_files=("source-map/formal.json",),
            )
            if item.executable and item.source_map_files == (only_route.source_map_path,)
            else item
            for item in jobs
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
    vacuity_dependencies.update(root_vacuity_dependencies)
    vacuity_dependencies.update(recursive_vacuity_dependencies)

    execution_plan = FormalExecutionPlan(
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
            ] + sorted(root_feasibility_properties)
            + sorted(recursive_cover_properties),
        }),
        tuple(goal_plans),
    )
    base_compiler_execution_plan, _ = build_compiler_formal_execution_plan(
        result,
        execution_plan,
    )
    if compiler_execution_plan is None:
        compiler_execution_plan = base_compiler_execution_plan
    else:
        if not isinstance(compiler_execution_plan, CompilerFormalExecutionPlan):
            raise FormalOrchestrationError(
                "verification publication requires a typed compiler formal plan"
            )
        if compiler_execution_plan.verification_plan != execution_plan:
            raise FormalOrchestrationError(
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
            or compiler_execution_plan.m39_attempts
            != base_compiler_execution_plan.m39_attempts
        ):
            raise FormalOrchestrationError(
                "published compiler formal plan differs from this compilation"
            )
    if tuple(item.plan for item in prepared_candidate_equivalence) != (
        compiler_execution_plan.candidate_equivalence_plans
    ):
        raise FormalOrchestrationError(
            "published candidate replay inputs differ from compiler plans"
        )
    candidate_records: list[dict[str, object]] = []
    for prepared in prepared_candidate_equivalence:
        frozen = prepared.freeze()
        content = (stable_json(frozen.to_data(), indent=2) + "\n").encode("utf-8")
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
        add_input(VerificationBundleInput(path, "companion", content))
        candidate_records.append({
            "site_identity": frozen.plan.site_identity,
            "candidate_identity": frozen.plan.candidate_identity,
            "plan_identity": frozen.plan.plan_identity,
            "replay_identity": frozen.replay_identity,
            "logical_path": path,
            "content_hash": content_hash,
        })
    legacy_bindings = (
        next(iter(binding_sets.values()))["bindings"]
        if len(binding_sets) == 1 else []
    )
    payload: dict[str, object] = {
        "formal_ir_version": 4 if candidate_records else 3,
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
                "version": _compiler_version(),
                "canonical_ir": CANONICAL_IR_IDENTITY_SCHEMA,
                "formal_predicate": FORMAL_PREDICATE_SCHEMA,
                "verification_publication": 4 if candidate_records else 3,
            }),
        },
        "hardware": {
            "high_level_ir_identity": result.high_level_ir_identity,
            "selected_ir_identity": result.selected_ir_identity,
        },
        "scopes": _scope_payload(result) + [
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
                "source_origin": _origin_payload(item.source_origin),
            }
            for item in (
                recursive_scopes[key] for key in sorted(recursive_scopes)
            )
        ],
        "properties": [
            {
                "id": item.id,
                "kind": "safety",
                "classification": item.classification.value,
                "generated_from": item.generated_from,
                "predicate": None if item.predicate is None else item.predicate.to_data(),
                "source_origin": (
                    None if item.source_origin is None else item.source_origin.to_data()
                ),
            }
            for item in source_safety
        ] + [
            {
                "id": item.id,
                "kind": "cover",
                "classification": "bounded_reachability",
                "generated_from": item.generated_from,
                "predicate": None if item.predicate is None else item.predicate.to_data(),
                "source_origin": (
                    None if item.source_origin is None else item.source_origin.to_data()
                ),
            }
            for item in source_covers
        ] + [
            {
                "id": item.id,
                "kind": "cover",
                "classification": "bounded_reachability",
                "generated_from": item.generated_from,
                "predicate": (
                    None if item.predicate is None else item.predicate.to_data()
                ),
                "source_origin": (
                    None
                    if item.source_origin is None
                    else item.source_origin.to_data()
                ),
            }
            for item in (
                root_feasibility_properties[key]
                for key in sorted(root_feasibility_properties)
            )
        ] + [
            {
                "id": item.concrete_property_id,
                "kind": "safety",
                "classification": item.property.classification.value,
                "generated_from": item.property.generated_from,
                "predicate": (
                    None
                    if item.property.predicate is None
                    else item.property.predicate.to_data()
                ),
                "source_origin": (
                    None
                    if item.property.source_origin is None
                    else item.property.source_origin.to_data()
                ),
            }
            for item in sorted(
                recursive_safety,
                key=lambda value: value.concrete_property_id,
            )
        ] + [
            {
                "id": item.concrete_property_id,
                "kind": "cover",
                "classification": "bounded_reachability",
                "generated_from": item.property.generated_from,
                "predicate": (
                    None
                    if item.property.predicate is None
                    else item.property.predicate.to_data()
                ),
                "source_origin": (
                    None
                    if item.property.source_origin is None
                    else item.property.source_origin.to_data()
                ),
            }
            for item in (
                recursive_cover_properties[key]
                for key in sorted(recursive_cover_properties)
            )
        ],
        "bindings": legacy_bindings,
        "binding_sets": [binding_sets[item] for item in sorted(binding_sets)],
        "execution_plan": execution_plan.to_data(),
        "compiler_execution_plan": compiler_execution_plan.to_data(),
        "vacuity_dependencies": vacuity_dependencies,
    }
    if candidate_records:
        payload["candidate_equivalence_records"] = candidate_records
    property_ids = tuple(item.property_id for item in jobs)
    verification_identity = verification_identity_for(
        top=result.ir.name,
        hardware_identity=result.selected_ir_identity,
        property_ids=property_ids,
        payload=payload,
    )
    return publish_verification_bundle(
        directory,
        top=result.ir.name,
        hardware_identity=result.selected_ir_identity,
        verification_identity=verification_identity,
        property_ids=property_ids,
        verification_ir=payload,
        files=tuple(input_by_path[item] for item in sorted(input_by_path)),
        jobs=jobs,
    )


def _compiler_version() -> str:
    try:
        return version("zlang-hdl")
    except PackageNotFoundError:
        return "source-checkout"


__all__ = ["publish_compilation_verification_bundle"]
