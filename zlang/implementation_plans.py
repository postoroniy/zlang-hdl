"""Deterministic implementation planning for the production SV backend."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable

from zlang.costs import SourcePolicy
from zlang.diagnostics import DiagnosticError
from zlang.ir.module import Module
from zlang.ir.target import ImplementationGraph
from zlang.target_planner import (
    QoREvidence,
    TargetPlanningResult,
    plan_target_pipeline,
)
from zlang.targets import (
    ArchitectureSelectionMode,
    TargetArchitectureError,
    generic_implementation_graph,
    load_target,
    select_implementation_graph,
)


PLAN_SCHEMA = "zlang-backend-implementation-plan-v1"
_BACKEND_ORDER = ("systemverilog",)


class BackendPlanStatus(str, Enum):
    """Outcome of planning one complete backend route."""

    SELECTED = "selected"
    GENERIC_FALLBACK = "generic_fallback"
    UNSUPPORTED = "unsupported"
    NOT_REQUESTED = "not_requested"


class BackendPlanRequirement(str, Enum):
    """Whether absence of a requested backend route is fatal."""

    PREFERRED = "preferred"
    REQUIRED = "required"
    NOT_REQUESTED = "not_requested"


class BackendImplementationPlanningError(DiagnosticError):
    """One or more required backend routes could not be planned."""

    default_code = "ZL-IMPL-BACKEND"

    def __init__(self, message: str, result: "BackendImplementationPlanningResult"):
        super().__init__(message, notes=(result.report.rstrip(),))
        self.result = result


@dataclass(frozen=True)
class BackendImplementationPlan:
    """One backend's independent implementation decision."""

    backend: str
    requirement: BackendPlanRequirement
    status: BackendPlanStatus
    graph: ImplementationGraph | None = None
    reason: str = ""
    target: str | None = None
    architecture: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in _BACKEND_ORDER:
            raise ValueError(f"unsupported implementation backend '{self.backend}'")
        if self.status in {
            BackendPlanStatus.SELECTED,
            BackendPlanStatus.GENERIC_FALLBACK,
        } and self.graph is None:
            raise ValueError(f"backend plan status '{self.status.value}' requires a graph")
        if self.status in {
            BackendPlanStatus.UNSUPPORTED,
            BackendPlanStatus.NOT_REQUESTED,
        } and self.graph is not None:
            raise ValueError(f"backend plan status '{self.status.value}' cannot carry a graph")
        if self.status is BackendPlanStatus.GENERIC_FALLBACK and not self.graph.is_generic:
            raise ValueError("generic_fallback must carry a generic implementation graph")

    @property
    def identity(self) -> str:
        return sha256(_canonical_json(self.to_data()).encode()).hexdigest()

    def to_data(self) -> dict[str, object]:
        return {
            "schema": PLAN_SCHEMA,
            "backend": self.backend,
            "requirement": self.requirement.value,
            "status": self.status.value,
            "target": self.target,
            "architecture": self.architecture,
            "implementation_graph_identity": self.graph.identity if self.graph else None,
            "realization_backend": self.graph.realization_backend if self.graph else None,
            "latency_knowledge": self.graph.latency_knowledge if self.graph else None,
            "latency": self.graph.latency if self.graph else None,
            "ii": self.graph.initiation_interval if self.graph else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BackendImplementationPlanningResult:
    """Stable collection containing the sole production backend plan."""

    plans: tuple[BackendImplementationPlan, ...]
    target_planning_result: TargetPlanningResult | None = None

    def __post_init__(self) -> None:
        expected = tuple(item for item in _BACKEND_ORDER)
        actual = tuple(item.backend for item in self.plans)
        if actual != expected:
            raise ValueError(
                "backend implementation plans must be complete and ordered as "
                + ", ".join(expected)
            )

    @property
    def identity(self) -> str:
        return sha256(_canonical_json(self.to_data()).encode()).hexdigest()

    @property
    def report(self) -> str:
        return render_backend_implementation_report(self)

    def plan_for(self, backend: object) -> BackendImplementationPlan:
        name = _backend_name(backend)
        return next(item for item in self.plans if item.backend == name)

    def to_data(self) -> dict[str, object]:
        return {
            "schema": PLAN_SCHEMA,
            "plans": [item.to_data() for item in self.plans],
        }


def plan_backend_implementations(
    module: Module,
    *,
    backend_requests: Iterable[object] = (),
    target: str | None = None,
    architecture: str | None = None,
    architecture_mode: ArchitectureSelectionMode | str = ArchitectureSelectionMode.GENERIC,
    source_policy: SourcePolicy | str = SourcePolicy.MEASURED_PREFERRED,
    evidence: Iterable[QoREvidence] | None = None,
    evidence_path=None,
    tool: str = "Vivado",
    tool_version: str = "2024.2",
    clock_period_ns: float = 10.0,
    strict_target_planning: bool = False,
) -> BackendImplementationPlanningResult:
    """Plan direct SystemVerilog using existing target semantics.

    ``backend_requests`` accepts ``(backend, requirement)`` pairs as well as
    objects carrying ``kind``/``backend`` and ``mode``/``requirement`` fields.
    This keeps the planner independent from the project-profile data model.
    A missing request is retained as ``not_requested`` so report identity does
    not depend on call-site branching.
    """

    requests = _normalize_requests(backend_requests)
    mode = ArchitectureSelectionMode(architecture_mode)
    generic = _generic_graph(module, target)
    physical_result: TargetPlanningResult | None = None
    planning_error: str | None = None
    selected = generic
    physical_intent = architecture is not None

    # The bounded elastic implementation has one compiler-owned global clock
    # enable.  Existing physical resource schemas do not advertise a compatible
    # stall/clock-enable input, so no DSP/site graph may be assumed stallable.
    # Preferred physical requests fall back through the normal plan machinery;
    # required requests remain explicit failures.
    elastic_physical_error = None
    if module.elastic_pipeline_regions and (
        architecture is not None or mode is not ArchitectureSelectionMode.GENERIC
    ):
        physical_intent = True
        elastic_physical_error = (
            "elastic pipeline(auto) physical mapping requires every selected "
            "resource site to advertise one compatible clock-enable/stall input; "
            "the current target resource schema provides no such capability"
        )

    # The target-aware planner produces physical graphs for direct SystemVerilog.
    try:
        if elastic_physical_error is not None:
            raise TargetArchitectureError(elastic_physical_error)
        physical_result = plan_target_pipeline(
            module,
            target=target,
            source_policy=source_policy,
            evidence=evidence,
            evidence_path=evidence_path,
            backend="direct_systemverilog",
            tool=tool,
            tool_version=tool_version,
            clock_period_ns=clock_period_ns,
            architecture=architecture,
            required_architecture=mode is ArchitectureSelectionMode.REQUIRED,
        )
        if physical_result is not None:
            physical_intent = True
            selected = physical_result.selected_graph
        elif architecture is not None or mode is not ArchitectureSelectionMode.GENERIC:
            selected = select_implementation_graph(
                module,
                target=target,
                architecture=architecture,
                mode=mode,
            )
            physical_intent = architecture is not None
        _validate_exact_timing(module, selected)
    except (TargetArchitectureError, ValueError) as error:
        if strict_target_planning:
            raise
        planning_error = str(error)

    plans: list[BackendImplementationPlan] = []
    for backend in _BACKEND_ORDER:
        requirement = requests.get(backend, BackendPlanRequirement.NOT_REQUESTED)
        plans.append(_plan_backend(
            backend,
            requirement,
            generic=generic,
            selected=selected,
            target=target,
            architecture=architecture,
            architecture_mode=mode,
            physical_intent=physical_intent,
            planning_error=planning_error,
        ))
    result = BackendImplementationPlanningResult(tuple(plans), physical_result)
    failed = tuple(
        item for item in result.plans
        if item.requirement is BackendPlanRequirement.REQUIRED
        and item.status is BackendPlanStatus.UNSUPPORTED
    )
    if failed:
        detail = "; ".join(f"{item.backend}: {item.reason}" for item in failed)
        raise BackendImplementationPlanningError(
            f"required backend implementation is unavailable: {detail}", result,
        )
    return result


def _plan_backend(
    backend: str,
    requirement: BackendPlanRequirement,
    *,
    generic: ImplementationGraph,
    selected: ImplementationGraph,
    target: str | None,
    architecture: str | None,
    architecture_mode: ArchitectureSelectionMode,
    physical_intent: bool,
    planning_error: str | None,
) -> BackendImplementationPlan:
    common = dict(
        backend=backend,
        requirement=requirement,
        target=target,
        architecture=architecture,
    )
    if requirement is BackendPlanRequirement.NOT_REQUESTED:
        return BackendImplementationPlan(
            **common,
            status=BackendPlanStatus.NOT_REQUESTED,
            reason="backend was not requested",
        )
    if planning_error is not None:
        if (
            requirement is BackendPlanRequirement.PREFERRED
            and architecture_mode is not ArchitectureSelectionMode.REQUIRED
        ):
            return BackendImplementationPlan(
                **common,
                status=BackendPlanStatus.GENERIC_FALLBACK,
                graph=generic,
                reason=f"preferred physical route rejected: {planning_error}",
            )
        return BackendImplementationPlan(
            **common,
            status=BackendPlanStatus.UNSUPPORTED,
            reason=planning_error,
        )

    if backend == "systemverilog":
        if selected.realization_backend == "direct_systemverilog":
            return BackendImplementationPlan(
                **common,
                status=BackendPlanStatus.SELECTED,
                graph=selected,
                reason="selected target graph is realized by direct SystemVerilog",
            )
        if selected.is_generic:
            status = (
                BackendPlanStatus.GENERIC_FALLBACK
                if physical_intent else BackendPlanStatus.SELECTED
            )
            return BackendImplementationPlan(
                **common,
                status=status,
                graph=generic,
                reason=_generic_selection_reason(selected, physical_intent),
            )
        return BackendImplementationPlan(
            **common,
            status=BackendPlanStatus.UNSUPPORTED,
            reason=(
                f"selected graph is realized by '{selected.realization_backend}', "
                "not direct SystemVerilog"
            ),
        )

    raise AssertionError(f"unreachable backend plan '{backend}'")


def render_backend_implementation_report(
    result: BackendImplementationPlanningResult,
) -> str:
    lines = [f"Backend implementation plans ({PLAN_SCHEMA})"]
    for plan in result.plans:
        line = (
            f"backend {plan.backend}: {plan.status.value}; "
            f"requirement={plan.requirement.value}"
        )
        if plan.graph is not None:
            line += (
                f"; realization={plan.graph.realization_backend}"
                f"; timing={plan.graph.latency_knowledge}"
                f"; latency={plan.graph.latency}; ii={plan.graph.initiation_interval}"
                f"; graph={plan.graph.identity}"
            )
        lines.append(line)
        lines.append(f"  reason: {plan.reason}")
    lines.append(f"plan identity: {result.identity}")
    return "\n".join(lines) + "\n"


def _generic_graph(module: Module, target: str | None) -> ImplementationGraph:
    target_instance = None
    if target not in {None, "generic"}:
        target_instance = load_target(target)[0]
    return generic_implementation_graph(module, target_instance)


def _validate_exact_timing(module: Module, graph: ImplementationGraph) -> None:
    contract = getattr(module, "timing_contract", None)
    if contract is None:
        return
    if graph.latency_knowledge != "known":
        raise TargetArchitectureError(
            "selected implementation has unknown latency but the module declares "
            f"exact latency {contract.latency}"
        )
    if graph.latency != contract.latency or graph.initiation_interval != contract.initiation_interval:
        raise TargetArchitectureError(
            "selected implementation timing "
            f"latency={graph.latency}, ii={graph.initiation_interval} conflicts with "
            f"exact module timing latency={contract.latency}, ii={contract.initiation_interval}"
        )


def _generic_selection_reason(
    selected: ImplementationGraph, physical_intent: bool,
) -> str:
    rejected = tuple(
        item for item in selected.legality_evidence
        if "rejected:" in item
    )
    if rejected:
        return rejected[-1]
    if physical_intent:
        return "target route selected the technology-independent generic implementation"
    return "technology-independent generic implementation selected"


def _normalize_requests(
    requests: Iterable[object],
) -> dict[str, BackendPlanRequirement]:
    result: dict[str, BackendPlanRequirement] = {}
    for raw in requests:
        if isinstance(raw, tuple) and len(raw) == 2:
            backend, requirement = raw
        else:
            backend = getattr(raw, "kind", getattr(raw, "backend", None))
            requirement = getattr(raw, "mode", getattr(raw, "requirement", None))
        name = _backend_name(backend)
        if name in result:
            raise ValueError(f"backend '{name}' is requested more than once")
        result[name] = _requirement(requirement)
    return result


def _backend_name(value: object) -> str:
    raw = getattr(value, "value", value)
    aliases = {
        "systemverilog": "systemverilog",
        "direct_systemverilog": "systemverilog",
        "direct-sv": "systemverilog",
    }
    try:
        return aliases[str(raw)]
    except KeyError as error:
        raise ValueError(f"unsupported implementation backend '{raw}'") from error


def _requirement(value: object) -> BackendPlanRequirement:
    raw = getattr(value, "value", value)
    if raw is None:
        return BackendPlanRequirement.PREFERRED
    return BackendPlanRequirement(str(raw))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "PLAN_SCHEMA",
    "BackendImplementationPlan",
    "BackendImplementationPlanningError",
    "BackendImplementationPlanningResult",
    "BackendPlanRequirement",
    "BackendPlanStatus",
    "plan_backend_implementations",
    "render_backend_implementation_report",
]
