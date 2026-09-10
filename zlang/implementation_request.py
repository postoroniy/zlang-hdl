"""Normalized external implementation policy.

This module is deliberately an adapter and data-model layer.  It does not run
exploration, select a backend, or assign new semantics to legacy source forms.
Profiles, explicit API/CLI options, and source policy are represented as typed
contributions and merged before the existing M28/M34/M39 machinery consumes
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from zlang.common.serialization import stable_digest
from zlang.costs import SourcePolicy, UnifiedConstraint
from zlang.diagnostics import DiagnosticError
from zlang.exploration import TransformFamily
from zlang.formal_exploration import FormalPolicy
from zlang.ir.expressions import CostMetric
from zlang.ir.timing import ModuleTimingContract
from zlang.project import ProjectManifest
from zlang.source import SourceOrigin
from zlang.targets import ArchitectureSelectionMode


IMPLEMENTATION_REQUEST_SCHEMA = "zlang-implementation-request-v1"

_REGION_ID = re.compile(r"[0-9a-f]{64}\Z")


class ImplementationRequestError(DiagnosticError):
    """A selected profile or normalized policy contribution is invalid."""

    default_code = "ZL-IMPL-001"


class BackendKind(str, Enum):
    CLASH = "clash"
    SYSTEMVERILOG = "systemverilog"


class RequirementMode(str, Enum):
    PREFERRED = "preferred"
    REQUIRED = "required"


class ConstraintRelation(str, Enum):
    MAXIMUM = "maximum"
    MINIMUM = "minimum"
    EXACT = "exact"


class ObjectiveDirection(str, Enum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


_METRIC_NAMES = {
    "lut": CostMetric.LUT,
    "ff": CostMetric.FF,
    "dsp": CostMetric.DSP,
    "bram": CostMetric.BRAM,
    "latency": CostMetric.LATENCY,
    "ii": CostMetric.INITIATION_INTERVAL,
    "fmax": CostMetric.FMAX_EST,
    "fmax_est": CostMetric.FMAX_EST,
}
_METRIC_SPELLING = {
    value: ("fmax" if value is CostMetric.FMAX_EST else value.value)
    for value in CostMetric
}
_DEFAULT_RELATION = {
    CostMetric.LUT: ConstraintRelation.MAXIMUM,
    CostMetric.FF: ConstraintRelation.MAXIMUM,
    CostMetric.DSP: ConstraintRelation.MAXIMUM,
    CostMetric.BRAM: ConstraintRelation.MAXIMUM,
    CostMetric.LATENCY: ConstraintRelation.MAXIMUM,
    CostMetric.INITIATION_INTERVAL: ConstraintRelation.EXACT,
    CostMetric.FMAX_EST: ConstraintRelation.MINIMUM,
}
MINIMIZABLE_METRICS = frozenset(
    {
        CostMetric.LUT,
        CostMetric.FF,
        CostMetric.DSP,
        CostMetric.BRAM,
        CostMetric.LATENCY,
    }
)
MAXIMIZABLE_METRICS = frozenset({CostMetric.FMAX_EST})


@dataclass(frozen=True)
class PolicyOrigin:
    """Human-facing policy provenance; never part of request identity."""

    label: str
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("policy origin label must not be empty")

    def render(self) -> str:
        if self.source_origin is None:
            return self.label
        return f"{self.label} ({self.source_origin.render()})"


@dataclass(frozen=True)
class BackendRequest:
    kind: BackendKind
    mode: RequirementMode = RequirementMode.PREFERRED

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", BackendKind(self.kind))
        object.__setattr__(self, "mode", RequirementMode(self.mode))

    def to_data(self) -> dict[str, str]:
        return {"kind": self.kind.value, "mode": self.mode.value}


@dataclass(frozen=True)
class TransformPolicy:
    allowed: tuple[TransformFamily, ...] = ()
    avoided: tuple[TransformFamily, ...] = ()

    def __post_init__(self) -> None:
        allowed = _normalized_enums(self.allowed, TransformFamily, "allowed transform")
        avoided = _normalized_enums(self.avoided, TransformFamily, "avoided transform")
        overlap = set(allowed) & set(avoided)
        if overlap:
            names = ", ".join(sorted(item.value for item in overlap))
            raise ValueError(f"transforms are both allowed and avoided: {names}")
        object.__setattr__(self, "allowed", allowed)
        object.__setattr__(self, "avoided", avoided)

    def to_data(self) -> dict[str, list[str]]:
        return {
            "allowed": [item.value for item in self.allowed],
            "avoided": [item.value for item in self.avoided],
        }


@dataclass(frozen=True)
class ImplementationConstraint:
    metric: CostMetric
    relation: ConstraintRelation
    value: int | float

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", CostMetric(self.metric))
        object.__setattr__(self, "relation", ConstraintRelation(self.relation))
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("implementation constraint value must be numeric")
        if self.metric is CostMetric.INITIATION_INTERVAL:
            if self.value != int(self.value) or self.value < 1:
                raise ValueError("implementation II constraint must be a positive integer")
        elif self.metric is CostMetric.FMAX_EST:
            if self.value <= 0:
                raise ValueError("implementation Fmax constraint must be positive")
        elif self.value < 0 or self.value != int(self.value):
            raise ValueError(
                f"implementation {_metric_name(self.metric)} constraint must be "
                "a non-negative integer"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "metric": _metric_name(self.metric),
            "relation": self.relation.value,
            "value": self.value,
        }

    def accepts(self, value: int | float) -> bool:
        if self.relation is ConstraintRelation.MAXIMUM:
            return value <= self.value
        if self.relation is ConstraintRelation.MINIMUM:
            return value >= self.value
        return value == self.value

    def as_unified(self) -> UnifiedConstraint:
        if self.relation is ConstraintRelation.MAXIMUM:
            return UnifiedConstraint(self.metric, maximum=self.value)
        if self.relation is ConstraintRelation.MINIMUM:
            return UnifiedConstraint(self.metric, minimum=self.value)
        return UnifiedConstraint(self.metric, minimum=self.value, maximum=self.value)


@dataclass(frozen=True)
class ImplementationObjective:
    direction: ObjectiveDirection = ObjectiveDirection.MINIMIZE
    metric: CostMetric = CostMetric.LUT

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", ObjectiveDirection(self.direction))
        object.__setattr__(self, "metric", CostMetric(self.metric))
        if (
            self.direction is ObjectiveDirection.MAXIMIZE
            and self.metric not in MAXIMIZABLE_METRICS
        ):
            raise ValueError("the bounded implementation profile only maximizes Fmax")
        if (
            self.direction is ObjectiveDirection.MINIMIZE
            and self.metric not in MINIMIZABLE_METRICS
        ):
            raise ValueError(
                "the bounded implementation profile minimizes only LUT, FF, "
                "DSP, BRAM, or latency"
            )

    def to_data(self) -> dict[str, str]:
        return {"direction": self.direction.value, "metric": _metric_name(self.metric)}


@dataclass(frozen=True)
class ArchitectureRequest:
    identity: str | None = None
    mode: ArchitectureSelectionMode = ArchitectureSelectionMode.GENERIC

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ArchitectureSelectionMode(self.mode))
        if self.identity is not None and (not isinstance(self.identity, str) or not self.identity.strip()):
            raise ValueError("architecture identity must be a non-empty string")
        if self.identity is None and self.mode is not ArchitectureSelectionMode.GENERIC:
            raise ValueError("preferred/required architecture mode requires an architecture")
        if self.identity is not None and self.mode is ArchitectureSelectionMode.GENERIC:
            raise ValueError("a named architecture must be preferred or required")

    def to_data(self) -> dict[str, object]:
        return {"identity": self.identity, "mode": self.mode.value}


@dataclass(frozen=True)
class ExactTiming:
    latency: int
    initiation_interval: int

    def __post_init__(self) -> None:
        if isinstance(self.latency, bool) or not isinstance(self.latency, int) or self.latency < 0:
            raise ValueError("exact timing latency must be a non-negative integer")
        if (
            isinstance(self.initiation_interval, bool)
            or not isinstance(self.initiation_interval, int)
            or self.initiation_interval < 1
        ):
            raise ValueError("exact timing II must be a positive integer")

    def to_data(self) -> dict[str, int]:
        return {"latency": self.latency, "ii": self.initiation_interval}


@dataclass(frozen=True)
class SemanticRegionIdentity:
    """Stable region key based only on already-canonical semantic identities."""

    module_identity: str
    specialization_identity: str
    output_binding: str
    expression_identity: str
    canonical_result_type: str

    def __post_init__(self) -> None:
        for name in (
            "module_identity", "specialization_identity", "output_binding",
            "expression_identity", "canonical_result_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"semantic region {name.replace('_', ' ')} must not be empty")

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    def to_data(self) -> dict[str, str]:
        return {
            "module": self.module_identity,
            "specialization": self.specialization_identity,
            "output": self.output_binding,
            "expression": self.expression_identity,
            "result_type": self.canonical_result_type,
        }


@dataclass(frozen=True)
class ImplementationContribution:
    """One partial policy supplied by source, profile, CLI, or an API caller."""

    origin: PolicyOrigin
    backend: BackendRequest | None = None
    target: str | None = None
    transforms: TransformPolicy | None = None
    constraints: tuple[ImplementationConstraint, ...] | None = None
    objective: ImplementationObjective | None = None
    architecture: ArchitectureRequest | None = None
    evidence_policy: SourcePolicy | None = None
    formal_policy: FormalPolicy | None = None
    regions: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.origin, PolicyOrigin):
            raise ValueError("implementation contribution requires typed provenance")
        if self.target is not None and (not isinstance(self.target, str) or not self.target.strip()):
            raise ValueError("target identity must be a non-empty string")
        if self.constraints is not None:
            object.__setattr__(self, "constraints", _normalize_constraints(self.constraints))
        if self.evidence_policy is not None:
            object.__setattr__(self, "evidence_policy", SourcePolicy(self.evidence_policy))
        if self.formal_policy is not None:
            object.__setattr__(self, "formal_policy", FormalPolicy(self.formal_policy))
        if self.regions is not None:
            object.__setattr__(self, "regions", _normalize_regions(self.regions))


@dataclass(frozen=True)
class ImplementationRequest:
    """One deterministic policy after compatible contributions are merged."""

    backend: BackendRequest | None = None
    target: str | None = None
    transforms: TransformPolicy = field(default_factory=TransformPolicy)
    constraints: tuple[ImplementationConstraint, ...] = ()
    objective: ImplementationObjective = field(default_factory=ImplementationObjective)
    architecture: ArchitectureRequest = field(default_factory=ArchitectureRequest)
    evidence_policy: SourcePolicy = SourcePolicy.MEASURED_PREFERRED
    formal_policy: FormalPolicy = FormalPolicy.OFF
    exact_timing: ExactTiming | None = None
    regions: tuple[str, ...] = ()
    schema: str = field(default=IMPLEMENTATION_REQUEST_SCHEMA, init=False)
    contributions: tuple[PolicyOrigin, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.target is not None and (not isinstance(self.target, str) or not self.target.strip()):
            raise ValueError("target identity must be a non-empty string")
        object.__setattr__(self, "constraints", _normalize_constraints(self.constraints))
        object.__setattr__(self, "evidence_policy", SourcePolicy(self.evidence_policy))
        object.__setattr__(self, "formal_policy", FormalPolicy(self.formal_policy))
        object.__setattr__(self, "regions", _normalize_regions(self.regions))

    @property
    def identity(self) -> str:
        return stable_digest(self.to_identity_data())

    def to_identity_data(self) -> dict[str, object]:
        return {
            "architecture": self.architecture.to_data(),
            "backend": None if self.backend is None else self.backend.to_data(),
            "constraints": [item.to_data() for item in self.constraints],
            "evidence_policy": self.evidence_policy.value,
            "exact_timing": None if self.exact_timing is None else self.exact_timing.to_data(),
            "formal_policy": self.formal_policy.value,
            "objective": self.objective.to_data(),
            "regions": list(self.regions),
            "schema": self.schema,
            "target": self.target,
            "transforms": self.transforms.to_data(),
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self.to_identity_data(),
            "contributions": [item.render() for item in self.contributions],
        }

    def unified_constraints(self) -> tuple[UnifiedConstraint, ...]:
        return tuple(item.as_unified() for item in self.constraints)


def _parse_selected_profile_unchecked(
    manifest: ProjectManifest,
    profile_name: str,
) -> ImplementationContribution:
    """Strictly parse one selected profile, ignoring every unselected entry."""

    if not profile_name:
        raise ImplementationRequestError("profile name must not be empty")
    if profile_name not in manifest.profiles:
        raise ImplementationRequestError(
            f"unknown implementation profile '{profile_name}'",
            fixes=("select a profile declared under [profiles] in zlang.toml",),
        )
    raw = _mapping(manifest.profiles[profile_name], f"profile '{profile_name}'")
    allowed_keys = {
        "backend", "backend-mode", "target", "allowed-transforms",
        "avoided-transforms", "constraints", "objective", "architecture",
        "architecture-mode", "evidence-policy", "formal-policy", "regions",
        # Physical clock publication is parsed separately from implementation
        # policy so it cannot affect semantic legality or candidate selection.
        "platform",
        # Physical external HDL selection is validated and resolved separately;
        # it cannot affect semantic/architecture policy identity.
        "external-mappings",
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        key = sorted(unknown)[0]
        raise ImplementationRequestError(
            f"unknown implementation profile key '{key}' in profile '{profile_name}'"
        )
    if "external-mappings" in raw:
        _string_array(raw["external-mappings"], "external-mappings")

    backend = None
    if "backend" in raw:
        backend = BackendRequest(
            _enum(BackendKind, raw["backend"], "backend"),
            _enum(
                RequirementMode,
                raw.get("backend-mode", RequirementMode.PREFERRED.value),
                "backend mode",
            ),
        )
    elif "backend-mode" in raw:
        raise ImplementationRequestError("profile backend-mode requires backend")

    transforms = None
    if "allowed-transforms" in raw or "avoided-transforms" in raw:
        transforms = TransformPolicy(
            tuple(
                _enum(TransformFamily, item, "allowed transform")
                for item in _string_array(raw.get("allowed-transforms", []), "allowed-transforms")
            ),
            tuple(
                _enum(TransformFamily, item, "avoided transform")
                for item in _string_array(raw.get("avoided-transforms", []), "avoided-transforms")
            ),
        )

    architecture = None
    if "architecture" in raw or "architecture-mode" in raw:
        identity = _optional_string(raw.get("architecture"), "architecture")
        mode = _enum(
            ArchitectureSelectionMode,
            raw.get("architecture-mode", ArchitectureSelectionMode.PREFERRED.value),
            "architecture mode",
        )
        try:
            architecture = ArchitectureRequest(identity, mode)
        except ValueError as error:
            raise ImplementationRequestError(str(error)) from error

    constraints = None
    if "constraints" in raw:
        constraints = _parse_constraints(_mapping(raw["constraints"], "profile constraints"))

    origin = PolicyOrigin(f"profile '{profile_name}' in {manifest.path.name}")
    try:
        return ImplementationContribution(
            origin=origin,
            backend=backend,
            target=_optional_string(raw.get("target"), "target"),
            transforms=transforms,
            constraints=constraints,
            objective=(
                None if "objective" not in raw else _parse_objective(raw["objective"])
            ),
            architecture=architecture,
            evidence_policy=(
                None if "evidence-policy" not in raw
                else _enum(SourcePolicy, raw["evidence-policy"], "evidence policy")
            ),
            formal_policy=(
                None if "formal-policy" not in raw
                else _enum(FormalPolicy, raw["formal-policy"], "formal policy")
            ),
            regions=(
                None if "regions" not in raw
                else _string_array(raw["regions"], "regions")
            ),
        )
    except ValueError as error:
        if isinstance(error, ImplementationRequestError):
            raise
        raise ImplementationRequestError(str(error)) from error


def parse_selected_profile(
    manifest: ProjectManifest,
    profile_name: str,
) -> ImplementationContribution:
    """Strictly parse one selected profile into a typed partial contribution."""

    try:
        contribution = _parse_selected_profile_unchecked(manifest, profile_name)
        # Validate the orthogonal physical-publication table without merging it
        # into ImplementationRequest semantics.
        from zlang.platform_constraints import parse_platform_profile
        parse_platform_profile(manifest, profile_name)
        return contribution
    except ImplementationRequestError:
        raise
    except ValueError as error:
        raise ImplementationRequestError(str(error)) from error


# Descriptive compatibility spelling for callers that treat the selected
# profile as one contribution among source/CLI/API policy.
parse_implementation_profile = parse_selected_profile


def merge_implementation_contributions(
    *contributions: ImplementationContribution,
) -> ImplementationRequest:
    """Merge compatible partial policies; duplicate equal values are harmless."""

    selected: dict[str, tuple[object, PolicyOrigin]] = {}
    constraints: dict[CostMetric, tuple[tuple[ImplementationConstraint, ...], PolicyOrigin]] = {}
    for contribution in contributions:
        if not isinstance(contribution, ImplementationContribution):
            raise TypeError("implementation contribution must be typed")
        for field_name in (
            "backend", "target", "transforms", "objective", "architecture",
            "evidence_policy", "formal_policy", "regions",
        ):
            value = getattr(contribution, field_name)
            if value is None:
                continue
            _merge_field(selected, field_name, value, contribution.origin)
        if contribution.constraints is not None:
            for metric, values in _constraints_by_metric(contribution.constraints).items():
                previous = constraints.get(metric)
                if previous is None:
                    constraints[metric] = (values, contribution.origin)
                elif previous[0] != values:
                    _raise_conflict(
                        f"constraint.{_metric_name(metric)}",
                        previous[0], previous[1], values, contribution.origin,
                    )
    all_constraints = tuple(
        item
        for metric in sorted(constraints, key=lambda value: value.value)
        for item in constraints[metric][0]
    )
    return ImplementationRequest(
        backend=selected.get("backend", (None, None))[0],
        target=selected.get("target", (None, None))[0],
        transforms=selected.get("transforms", (TransformPolicy(), None))[0],
        constraints=all_constraints,
        objective=selected.get("objective", (ImplementationObjective(), None))[0],
        architecture=selected.get("architecture", (ArchitectureRequest(), None))[0],
        evidence_policy=selected.get(
            "evidence_policy", (SourcePolicy.MEASURED_PREFERRED, None)
        )[0],
        formal_policy=selected.get("formal_policy", (FormalPolicy.OFF, None))[0],
        regions=selected.get("regions", ((), None))[0],
        contributions=tuple(contribution.origin for contribution in contributions),
    )


def apply_exact_timing_contract(
    request: ImplementationRequest,
    contract: ModuleTimingContract | ExactTiming,
    *,
    origin: PolicyOrigin | None = None,
) -> ImplementationRequest:
    """Attach immutable public timing after proving all hard bounds accept it."""

    exact = (
        contract if isinstance(contract, ExactTiming)
        else ExactTiming(contract.latency, contract.initiation_interval)
    )
    if request.exact_timing is not None and request.exact_timing != exact:
        previous = PolicyOrigin("existing exact timing contract")
        _raise_conflict(
            "exact_timing", request.exact_timing, previous, exact,
            origin or PolicyOrigin("module timing contract"),
        )
    for item in request.constraints:
        if item.metric is CostMetric.LATENCY:
            value = exact.latency
        elif item.metric is CostMetric.INITIATION_INTERVAL:
            value = exact.initiation_interval
        else:
            continue
        if not item.accepts(value):
            raise ImplementationRequestError(
                f"implementation {_metric_name(item.metric)} constraint "
                f"{item.relation.value} {item.value} conflicts with exact module "
                f"timing value {value}",
                notes=(
                    (origin or PolicyOrigin("module timing contract")).render(),
                    "an exact public timing contract cannot be weakened or changed",
                ),
            )
    return ImplementationRequest(
        backend=request.backend,
        target=request.target,
        transforms=request.transforms,
        constraints=request.constraints,
        objective=request.objective,
        architecture=request.architecture,
        evidence_policy=request.evidence_policy,
        formal_policy=request.formal_policy,
        exact_timing=exact,
        regions=request.regions,
        contributions=request.contributions + ((origin,) if origin is not None else ()),
    )


def _parse_constraints(raw: Mapping[str, object]) -> tuple[ImplementationConstraint, ...]:
    unknown = set(raw) - set(_METRIC_NAMES)
    if unknown:
        raise ImplementationRequestError(
            f"unknown implementation constraint '{sorted(unknown)[0]}'"
        )
    values: list[ImplementationConstraint] = []
    for name in sorted(raw):
        metric = _METRIC_NAMES[name]
        specification = raw[name]
        if isinstance(specification, Mapping):
            keys = set(specification)
            unknown_relations = keys - {item.value for item in ConstraintRelation}
            if unknown_relations:
                raise ImplementationRequestError(
                    f"unknown relation '{sorted(unknown_relations)[0]}' for constraint '{name}'"
                )
            if ConstraintRelation.EXACT.value in keys and len(keys) != 1:
                raise ImplementationRequestError(
                    f"constraint '{name}' exact cannot be combined with minimum/maximum"
                )
            if not keys:
                raise ImplementationRequestError(f"constraint '{name}' must not be empty")
            for relation in ConstraintRelation:
                if relation.value in specification:
                    values.append(ImplementationConstraint(
                        metric, relation,
                        _number(specification[relation.value], f"constraint '{name}'"),
                    ))
        else:
            values.append(ImplementationConstraint(
                metric, _DEFAULT_RELATION[metric], _number(specification, f"constraint '{name}'")
            ))
    return _normalize_constraints(values)


def _parse_objective(raw: object) -> ImplementationObjective:
    if isinstance(raw, str):
        words = raw.strip().lower().replace("_", " ").split()
        if len(words) != 2:
            raise ImplementationRequestError(
                "objective must be 'minimize METRIC' or 'maximize fmax'"
            )
        direction, metric = words
    elif isinstance(raw, Mapping):
        unknown = set(raw) - {"direction", "metric"}
        missing = {"direction", "metric"} - set(raw)
        if unknown or missing:
            name = sorted(unknown or missing)[0]
            qualifier = "unknown" if unknown else "missing"
            raise ImplementationRequestError(f"objective has {qualifier} key '{name}'")
        direction = raw["direction"]
        metric = raw["metric"]
    else:
        raise ImplementationRequestError("objective must be a string or table")
    try:
        return ImplementationObjective(
            _enum(ObjectiveDirection, direction, "objective direction"),
            _metric(metric, "objective metric"),
        )
    except ValueError as error:
        raise ImplementationRequestError(str(error)) from error


def _merge_field(
    selected: dict[str, tuple[object, PolicyOrigin]],
    name: str,
    value: object,
    origin: PolicyOrigin,
) -> None:
    previous = selected.get(name)
    if previous is None:
        selected[name] = (value, origin)
    elif previous[0] != value:
        _raise_conflict(name, previous[0], previous[1], value, origin)


def _raise_conflict(
    name: str,
    first: object,
    first_origin: PolicyOrigin,
    second: object,
    second_origin: PolicyOrigin,
) -> None:
    raise ImplementationRequestError(
        f"conflicting implementation policy for '{name}'",
        notes=(
            f"{first_origin.render()}: {_render_value(first)}",
            f"{second_origin.render()}: {_render_value(second)}",
        ),
        fixes=("remove one policy or make both normalized values identical",),
    )


def _normalize_constraints(
    values: Sequence[ImplementationConstraint],
) -> tuple[ImplementationConstraint, ...]:
    typed = tuple(
        item if isinstance(item, ImplementationConstraint)
        else ImplementationConstraint(*item)  # type: ignore[arg-type]
        for item in values
    )
    unique = set(typed)
    return tuple(sorted(
        unique,
        key=lambda item: (item.metric.value, item.relation.value, item.value),
    ))


def _constraints_by_metric(
    values: Sequence[ImplementationConstraint],
) -> dict[CostMetric, tuple[ImplementationConstraint, ...]]:
    grouped: dict[CostMetric, list[ImplementationConstraint]] = {}
    for item in values:
        grouped.setdefault(item.metric, []).append(item)
    return {
        metric: _normalize_constraints(items)
        for metric, items in grouped.items()
    }


def _normalized_enums(values: Sequence[object], enum: type[Enum], description: str):
    try:
        converted = tuple(enum(item) for item in values)
    except ValueError as error:
        raise ValueError(f"unknown {description} '{error.args[0]}'") from error
    if len(converted) != len(set(converted)):
        raise ValueError(f"duplicate {description}")
    return tuple(sorted(converted, key=lambda item: item.value))


def _normalize_regions(values: Sequence[str]) -> tuple[str, ...]:
    converted = tuple(values)
    for value in converted:
        if not isinstance(value, str) or _REGION_ID.fullmatch(value) is None:
            raise ValueError(
                "implementation region selector must be a 64-character "
                "lowercase semantic identity"
            )
    if len(converted) != len(set(converted)):
        raise ValueError("duplicate implementation region selector")
    return tuple(sorted(converted))


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ImplementationRequestError(f"{description} must be a table")
    if any(not isinstance(key, str) for key in value):
        raise ImplementationRequestError(f"{description} keys must be strings")
    return value


def _string_array(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ImplementationRequestError(f"{description} must be an array of strings")
    return tuple(value)


def _optional_string(value: object, description: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ImplementationRequestError(f"{description} must be a non-empty string")
    return value


def _number(value: object, description: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImplementationRequestError(f"{description} must be numeric")
    return value


def _enum(enum: type[Enum], value: object, description: str):
    if not isinstance(value, str):
        raise ImplementationRequestError(f"{description} must be a string")
    try:
        return enum(value)
    except ValueError as error:
        choices = ", ".join(item.value for item in enum)
        raise ImplementationRequestError(
            f"unknown {description} '{value}'; expected one of {choices}"
        ) from error


def _metric(value: object, description: str) -> CostMetric:
    if not isinstance(value, str) or value not in _METRIC_NAMES:
        choices = ", ".join(sorted(set(_METRIC_NAMES) - {"fmax_est"}))
        raise ImplementationRequestError(
            f"unknown {description} '{value}'; expected one of {choices}"
        )
    return _METRIC_NAMES[value]


def _metric_name(value: CostMetric) -> str:
    return _METRIC_SPELLING[value]


def _render_value(value: object) -> str:
    if hasattr(value, "to_data"):
        return str(value.to_data())
    if isinstance(value, Enum):
        return value.value
    return str(value)


__all__ = [
    "IMPLEMENTATION_REQUEST_SCHEMA",
    "ArchitectureRequest",
    "BackendKind",
    "BackendRequest",
    "ConstraintRelation",
    "ExactTiming",
    "ImplementationConstraint",
    "ImplementationContribution",
    "ImplementationObjective",
    "ImplementationRequest",
    "ImplementationRequestError",
    "MAXIMIZABLE_METRICS",
    "MINIMIZABLE_METRICS",
    "ObjectiveDirection",
    "PolicyOrigin",
    "RequirementMode",
    "SemanticRegionIdentity",
    "TransformPolicy",
    "apply_exact_timing_contract",
    "merge_implementation_contributions",
    "parse_implementation_profile",
    "parse_selected_profile",
]
