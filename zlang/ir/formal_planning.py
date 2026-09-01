"""Backend-independent planning IR for existing M35--M39 formal work.

The objects in this module describe *how* an already typed property can be
executed.  They intentionally do not merge the distinct M35, M36, or M38
property/result types, and they contain no solver policy.  In particular, a
goal has either one completely specified route or one structured skip reason;
there is no partially executable plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Mapping

from zlang.common import stable_digest, stable_json
from zlang.ir.cdc import (
    ClockDomain,
    clock_domain_contract_identity,
    clock_domain_data,
    clock_domain_from_data,
)
from zlang.ir.comparison_window import ComparisonWindow, ComparisonWindowKind
from zlang.source import SourceOrigin


FORMAL_EXECUTION_PLAN_SCHEMA = 2


class FormalPlanningError(ValueError):
    """A formal execution plan is malformed or internally inconsistent."""


class FormalPlanGoalKind(str, Enum):
    """Existing formal products represented by the orchestration layer."""

    SAFETY = "safety"
    COVER = "cover"
    M36_EQUIVALENCE = "m36_equivalence"
    M38_EQUIVALENCE = "m38_equivalence"


class FormalRouteKind(str, Enum):
    """Shape of an executable route, independent of a particular backend."""

    PROPERTY_HARNESS = "property_harness"
    COVER_HARNESS = "cover_harness"
    SEMANTIC_EQUIVALENCE = "semantic_equivalence"
    CROSS_BACKEND_EQUIVALENCE = "cross_backend_equivalence"


class FormalSkipCode(str, Enum):
    """Stable reasons why a typed goal has no executable route."""

    ASSUMPTION_UNAVAILABLE = "assumption_unavailable"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    BINDING_UNAVAILABLE = "binding_unavailable"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    DOMAIN_UNSUPPORTED = "domain_unsupported"
    RESET_UNSUPPORTED = "reset_unsupported"
    CANDIDATE_UNSUPPORTED = "candidate_unsupported"
    COMPARISON_WINDOW_UNREACHED = "comparison_window_unreached"
    ROUTE_UNAVAILABLE = "route_unavailable"
    TOOL_UNAVAILABLE = "tool_unavailable"


def _require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise FormalPlanningError(f"{description} must be a non-empty string")
    return value


def _require_optional_string(value: object, description: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, description)


def _require_string_tuple(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FormalPlanningError(f"{description} must be an array")
    result = tuple(_require_string(item, f"{description} entry") for item in value)
    if len(result) != len(set(result)):
        raise FormalPlanningError(f"{description} must not contain duplicates")
    return result


def _require_exact_keys(
    data: Mapping[str, object], expected: set[str], description: str
) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise FormalPlanningError(f"{description} has " + "; ".join(details))


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FormalPlanningError(f"{description} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise FormalPlanningError(f"{description} keys must be strings")
    return value  # type: ignore[return-value]


def _origin_from_data(value: object) -> SourceOrigin | None:
    if value is None:
        return None
    data = _mapping(value, "formal-plan source origin")
    _require_exact_keys(
        data, {"construct", "digest", "source_unit", "span"},
        "formal-plan source origin",
    )
    span = _mapping(data["span"], "formal-plan source span")
    _require_exact_keys(
        span,
        {"start_line", "start_column", "end_line", "end_column"},
        "formal-plan source span",
    )
    try:
        return SourceOrigin.from_data(data)
    except ValueError as error:
        raise FormalPlanningError(str(error)) from error


def _window_from_data(value: object) -> ComparisonWindow:
    data = _mapping(value, "comparison window")
    _require_exact_keys(
        data,
        {
            "kind", "fill_cycles", "reset_release_cycles", "first_comparison_cycle",
            "minimum_bmc_depth",
        },
        "comparison window",
    )
    kind_value = data["kind"]
    fill_cycles = data["fill_cycles"]
    reset_release_cycles = data["reset_release_cycles"]
    if not isinstance(kind_value, str):
        raise FormalPlanningError("comparison-window kind must be a string")
    if isinstance(fill_cycles, bool) or not isinstance(fill_cycles, int):
        raise FormalPlanningError("comparison-window fill_cycles must be an integer")
    if (
        isinstance(reset_release_cycles, bool)
        or not isinstance(reset_release_cycles, int)
    ):
        raise FormalPlanningError(
            "comparison-window reset_release_cycles must be an integer"
        )
    try:
        window = ComparisonWindow(
            ComparisonWindowKind(kind_value),
            fill_cycles,
            reset_release_cycles,
        )
    except (ValueError, TypeError) as error:
        raise FormalPlanningError(str(error)) from error
    for field, expected in (
        ("first_comparison_cycle", window.first_comparison_cycle),
        ("minimum_bmc_depth", window.minimum_bmc_depth),
    ):
        actual = data[field]
        if isinstance(actual, bool) or not isinstance(actual, int):
            raise FormalPlanningError(f"comparison-window {field} must be an integer")
        if actual != expected:
            raise FormalPlanningError(
                f"comparison-window {field} does not match its typed window"
            )
    return window


def _clock_domain_from_plan_data(value: object) -> ClockDomain | None:
    try:
        return clock_domain_from_data(value)
    except ValueError as error:
        raise FormalPlanningError(str(error)) from error


@dataclass(frozen=True)
class FormalBackendArtifactRef:
    """One exact backend artifact and binding set used by a route."""

    backend: str
    artifact_identity: str
    binding_identity: str

    def __post_init__(self) -> None:
        _require_string(self.backend, "formal-route backend")
        _require_string(self.artifact_identity, "formal-route artifact identity")
        _require_string(self.binding_identity, "formal-route binding identity")

    def to_data(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "artifact_identity": self.artifact_identity,
            "binding_identity": self.binding_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "FormalBackendArtifactRef":
        data = _mapping(value, "formal backend artifact reference")
        _require_exact_keys(
            data, {"backend", "artifact_identity", "binding_identity"},
            "formal backend artifact reference",
        )
        return cls(
            _require_string(data["backend"], "formal-route backend"),
            _require_string(
                data["artifact_identity"], "formal-route artifact identity"
            ),
            _require_string(
                data["binding_identity"], "formal-route binding identity"
            ),
        )


@dataclass(frozen=True)
class FormalExecutableRoute:
    """A fully connected route for exactly one formal goal."""

    kind: FormalRouteKind
    artifacts: tuple[FormalBackendArtifactRef, ...]
    reference_identity: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", FormalRouteKind(self.kind))
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(item, FormalBackendArtifactRef) for item in self.artifacts
        ):
            raise FormalPlanningError(
                "formal-route artifacts must be typed artifact references"
            )
        if not self.artifacts:
            raise FormalPlanningError("an executable formal route requires an artifact")
        keys = tuple((item.backend, item.artifact_identity) for item in self.artifacts)
        if len(keys) != len(set(keys)):
            raise FormalPlanningError("formal-route backend artifacts must be unique")
        if self.kind is FormalRouteKind.CROSS_BACKEND_EQUIVALENCE:
            if len(self.artifacts) != 2:
                raise FormalPlanningError(
                    "cross-backend equivalence requires exactly two backend artifacts"
                )
            if len({item.backend for item in self.artifacts}) != 2:
                raise FormalPlanningError(
                    "cross-backend equivalence requires two distinct backends"
                )
            if self.reference_identity is not None:
                raise FormalPlanningError(
                    "cross-backend equivalence does not use a semantic reference identity"
                )
        elif len(self.artifacts) != 1:
            raise FormalPlanningError(
                "property, cover, and semantic-equivalence routes require one backend artifact"
            )
        if self.kind is FormalRouteKind.SEMANTIC_EQUIVALENCE:
            _require_optional_string(
                self.reference_identity, "semantic reference identity"
            )
            if self.reference_identity is None:
                raise FormalPlanningError(
                    "semantic equivalence requires a reference identity"
                )
        elif self.reference_identity is not None:
            raise FormalPlanningError(
                "only semantic equivalence may carry a reference identity"
            )

    @property
    def identity(self) -> str:
        return "formal-route:" + stable_digest(self.to_data())

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "artifacts": [item.to_data() for item in self.artifacts],
            "reference_identity": self.reference_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "FormalExecutableRoute":
        data = _mapping(value, "formal executable route")
        _require_exact_keys(
            data, {"kind", "artifacts", "reference_identity"},
            "formal executable route",
        )
        kind = _require_string(data["kind"], "formal-route kind")
        artifacts_data = data["artifacts"]
        if not isinstance(artifacts_data, list):
            raise FormalPlanningError("formal-route artifacts must be an array")
        try:
            route_kind = FormalRouteKind(kind)
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        return cls(
            route_kind,
            tuple(FormalBackendArtifactRef.from_data(item) for item in artifacts_data),
            _require_optional_string(
                data["reference_identity"], "semantic reference identity"
            ),
        )


@dataclass(frozen=True)
class FormalSkipReason:
    """A stable, machine-readable reason a goal is non-executable."""

    code: FormalSkipCode
    message: str
    related_ids: tuple[str, ...] = ()
    backend: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "code", FormalSkipCode(self.code))
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        _require_string(self.message, "formal skip message")
        if not isinstance(self.related_ids, tuple):
            raise FormalPlanningError("formal skip related IDs must be a tuple")
        if len(self.related_ids) != len(set(self.related_ids)):
            raise FormalPlanningError("formal skip related IDs must be unique")
        for item in self.related_ids:
            _require_string(item, "formal skip related ID")
        _require_optional_string(self.backend, "formal skip backend")

    def to_data(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "related_ids": list(self.related_ids),
            "backend": self.backend,
        }

    @classmethod
    def from_data(cls, value: object) -> "FormalSkipReason":
        data = _mapping(value, "formal skip reason")
        _require_exact_keys(
            data, {"code", "message", "related_ids", "backend"},
            "formal skip reason",
        )
        code = _require_string(data["code"], "formal skip code")
        try:
            skip_code = FormalSkipCode(code)
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        return cls(
            skip_code,
            _require_string(data["message"], "formal skip message"),
            _require_string_tuple(data["related_ids"], "formal skip related IDs"),
            _require_optional_string(data["backend"], "formal skip backend"),
        )


_ROUTE_FOR_GOAL = {
    FormalPlanGoalKind.SAFETY: FormalRouteKind.PROPERTY_HARNESS,
    FormalPlanGoalKind.COVER: FormalRouteKind.COVER_HARNESS,
    FormalPlanGoalKind.M36_EQUIVALENCE: FormalRouteKind.SEMANTIC_EQUIVALENCE,
    FormalPlanGoalKind.M38_EQUIVALENCE: FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
}


@dataclass(frozen=True)
class FormalGoalPlan:
    """One typed goal plus its complete route or explicit non-applicability."""

    goal_identity: str
    property_identity: str
    kind: FormalPlanGoalKind
    clock_domain: str | None
    reset_domain: str | None
    assumption_ids: tuple[str, ...]
    required_observations: tuple[str, ...]
    selected_ir_identity: str
    comparison_window: ComparisonWindow
    minimum_bmc_depth: int
    route: FormalExecutableRoute | None = None
    skip_reason: FormalSkipReason | None = None
    source_origin: SourceOrigin | None = None
    clock_domain_contract: ClockDomain | None = None
    physical_domain_identity: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.goal_identity, "formal goal identity")
        _require_string(self.property_identity, "formal property identity")
        _require_string(self.selected_ir_identity, "selected IR identity")
        try:
            object.__setattr__(self, "kind", FormalPlanGoalKind(self.kind))
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        _require_optional_string(self.clock_domain, "formal goal clock domain")
        _require_optional_string(self.reset_domain, "formal goal reset domain")
        if self.reset_domain is not None and self.clock_domain is None:
            raise FormalPlanningError("a formal reset domain requires a clock domain")
        if not isinstance(self.assumption_ids, tuple):
            raise FormalPlanningError("formal scoped assumption IDs must be a tuple")
        if not isinstance(self.required_observations, tuple):
            raise FormalPlanningError("formal required observations must be a tuple")
        if len(self.assumption_ids) != len(set(self.assumption_ids)):
            raise FormalPlanningError("formal scoped assumption IDs must be unique")
        if len(self.required_observations) != len(set(self.required_observations)):
            raise FormalPlanningError("formal required observations must be unique")
        for item in self.assumption_ids:
            _require_string(item, "formal scoped assumption ID")
        for item in self.required_observations:
            _require_string(item, "formal required observation")
        if not isinstance(self.comparison_window, ComparisonWindow):
            raise FormalPlanningError("formal goal comparison window must use typed IR")
        if isinstance(self.minimum_bmc_depth, bool) or not isinstance(
            self.minimum_bmc_depth, int
        ):
            raise FormalPlanningError("formal minimum BMC depth must be an integer")
        if self.minimum_bmc_depth < self.comparison_window.minimum_bmc_depth:
            raise FormalPlanningError(
                "formal minimum BMC depth does not reach the comparison window"
            )
        if self.route is not None and not isinstance(
            self.route, FormalExecutableRoute
        ):
            raise FormalPlanningError("formal goal route must use typed planning IR")
        if self.skip_reason is not None and not isinstance(
            self.skip_reason, FormalSkipReason
        ):
            raise FormalPlanningError("formal skip reason must use typed planning IR")
        if (self.route is None) == (self.skip_reason is None):
            raise FormalPlanningError(
                "a formal goal plan requires exactly one route or skip reason"
            )
        if self.route is not None and self.route.kind is not _ROUTE_FOR_GOAL[self.kind]:
            raise FormalPlanningError(
                f"formal goal kind '{self.kind.value}' cannot use route "
                f"'{self.route.kind.value}'"
            )
        if self.source_origin is not None and not isinstance(
            self.source_origin, SourceOrigin
        ):
            raise FormalPlanningError("formal goal source origin is malformed")
        if self.clock_domain_contract is not None:
            if not isinstance(self.clock_domain_contract, ClockDomain):
                raise FormalPlanningError(
                    "formal goal clock-domain contract must use typed IR"
                )
            try:
                self.clock_domain_contract.validate()
            except ValueError as error:
                raise FormalPlanningError(str(error)) from error
            if (
                self.clock_domain != self.clock_domain_contract.clock
                or self.reset_domain != self.clock_domain_contract.reset
            ):
                raise FormalPlanningError(
                    "formal goal clock/reset names do not match its exact "
                    "clock-domain contract"
                )
        if self.physical_domain_identity is not None:
            _require_string(
                self.physical_domain_identity,
                "formal goal physical-domain identity",
            )
            if self.clock_domain_contract is None:
                raise FormalPlanningError(
                    "formal goal physical-domain identity requires an exact "
                    "clock-domain contract"
                )
            expected_identity = clock_domain_contract_identity(
                self.clock_domain_contract
            )
            if self.physical_domain_identity != expected_identity:
                raise FormalPlanningError(
                    "formal goal physical-domain identity does not match its "
                    "exact clock-domain contract"
                )

    @property
    def backend_artifact_identities(self) -> tuple[str, ...]:
        if self.route is None:
            return ()
        return tuple(item.artifact_identity for item in self.route.artifacts)

    def identity_data(self) -> dict[str, object]:
        return {
            "goal_identity": self.goal_identity,
            "property_identity": self.property_identity,
            "kind": self.kind.value,
            "clock_domain": self.clock_domain,
            "reset_domain": self.reset_domain,
            "assumption_ids": list(self.assumption_ids),
            "required_observations": list(self.required_observations),
            "selected_ir_identity": self.selected_ir_identity,
            "comparison_window": self.comparison_window.to_data(),
            "minimum_bmc_depth": self.minimum_bmc_depth,
            "route": None if self.route is None else self.route.to_data(),
            "skip_reason": (
                None if self.skip_reason is None else self.skip_reason.to_data()
            ),
            "clock_domain_contract": clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
        }

    @property
    def plan_identity(self) -> str:
        return "formal-goal-plan:" + stable_digest(self.identity_data())

    def to_data(self) -> dict[str, object]:
        return {
            **self.identity_data(),
            "plan_identity": self.plan_identity,
            "source_origin": (
                None if self.source_origin is None else self.source_origin.to_data()
            ),
        }

    @classmethod
    def from_data(cls, value: object) -> "FormalGoalPlan":
        data = _mapping(value, "formal goal plan")
        _require_exact_keys(
            data,
            {
                "goal_identity", "property_identity", "kind", "clock_domain",
                "reset_domain", "assumption_ids", "required_observations",
                "selected_ir_identity", "comparison_window",
                "minimum_bmc_depth", "route", "skip_reason", "plan_identity",
                "source_origin", "clock_domain_contract",
                "physical_domain_identity",
            },
            "formal goal plan",
        )
        kind = _require_string(data["kind"], "formal goal kind")
        try:
            goal_kind = FormalPlanGoalKind(kind)
        except ValueError as error:
            raise FormalPlanningError(str(error)) from error
        depth = data["minimum_bmc_depth"]
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise FormalPlanningError("formal minimum BMC depth must be an integer")
        route_data = data["route"]
        skip_data = data["skip_reason"]
        restored = cls(
            _require_string(data["goal_identity"], "formal goal identity"),
            _require_string(data["property_identity"], "formal property identity"),
            goal_kind,
            _require_optional_string(data["clock_domain"], "formal goal clock domain"),
            _require_optional_string(data["reset_domain"], "formal goal reset domain"),
            _require_string_tuple(data["assumption_ids"], "formal scoped assumption IDs"),
            _require_string_tuple(
                data["required_observations"], "formal required observations"
            ),
            _require_string(data["selected_ir_identity"], "selected IR identity"),
            _window_from_data(data["comparison_window"]),
            depth,
            None if route_data is None else FormalExecutableRoute.from_data(route_data),
            None if skip_data is None else FormalSkipReason.from_data(skip_data),
            _origin_from_data(data["source_origin"]),
            _clock_domain_from_plan_data(data["clock_domain_contract"]),
            _require_optional_string(
                data["physical_domain_identity"],
                "formal goal physical-domain identity",
            ),
        )
        plan_identity = _require_string(data["plan_identity"], "formal goal plan identity")
        if plan_identity != restored.plan_identity:
            raise FormalPlanningError("formal goal plan identity does not match its contents")
        return restored


@dataclass(frozen=True)
class FormalExecutionPlan:
    """Deterministic collection of independently routed formal goals."""

    compilation_identity: str
    verification_identity: str
    goals: tuple[FormalGoalPlan, ...]
    schema_version: int = FORMAL_EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        _require_string(self.compilation_identity, "formal compilation identity")
        _require_string(self.verification_identity, "formal verification identity")
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise FormalPlanningError("formal execution-plan schema must be an integer")
        if self.schema_version != FORMAL_EXECUTION_PLAN_SCHEMA:
            raise FormalPlanningError(
                f"unsupported formal execution-plan schema {self.schema_version}"
            )
        if not isinstance(self.goals, tuple) or any(
            not isinstance(item, FormalGoalPlan) for item in self.goals
        ):
            raise FormalPlanningError("formal execution-plan goals must be typed goals")
        if not self.goals:
            raise FormalPlanningError("a formal execution plan requires at least one goal")
        ordered = tuple(sorted(self.goals, key=lambda item: item.goal_identity))
        object.__setattr__(self, "goals", ordered)
        goal_ids = tuple(item.goal_identity for item in ordered)
        if len(goal_ids) != len(set(goal_ids)):
            raise FormalPlanningError("formal execution-plan goal identities must be unique")
        mismatched = next((
            item
            for item in ordered
            if item.selected_ir_identity != self.compilation_identity
        ), None)
        if mismatched is not None:
            raise FormalPlanningError(
                "formal execution-plan goal "
                f"'{mismatched.goal_identity}' selected-IR identity differs "
                "from the compilation identity"
            )

    def identity_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "compilation_identity": self.compilation_identity,
            "verification_identity": self.verification_identity,
            "goals": [item.identity_data() for item in self.goals],
        }

    @property
    def plan_identity(self) -> str:
        return "formal-execution-plan:" + stable_digest(self.identity_data())

    def applicability_summary(self) -> dict[str, object]:
        """Summarize executable and skipped goals without adding plan state.

        The summary is a deterministic reporting view derived entirely from the
        typed goals.  It is deliberately absent from ``identity_data`` and the
        serialized plan: applicability continues to have exactly one source of
        truth, each goal's executable route or structured skip reason.
        """

        executable = tuple(item for item in self.goals if item.route is not None)
        skipped = tuple(item for item in self.goals if item.skip_reason is not None)

        def counts(values: tuple[str, ...]) -> dict[str, int]:
            return {
                value: values.count(value)
                for value in sorted(set(values))
            }

        return {
            "total": len(self.goals),
            "executable": len(executable),
            "skipped": len(skipped),
            "goal_kinds": counts(tuple(item.kind.value for item in self.goals)),
            "routes": counts(tuple(item.route.kind.value for item in executable)),
            "backends": counts(tuple(
                artifact.backend
                for item in executable
                for artifact in item.route.artifacts
            )),
            "skip_reasons": counts(tuple(
                item.skip_reason.code.value for item in skipped
            )),
        }

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "compilation_identity": self.compilation_identity,
            "verification_identity": self.verification_identity,
            "plan_identity": self.plan_identity,
            "goals": [item.to_data() for item in self.goals],
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, value: object) -> "FormalExecutionPlan":
        data = _mapping(value, "formal execution plan")
        _require_exact_keys(
            data,
            {
                "schema_version", "compilation_identity", "verification_identity",
                "plan_identity", "goals",
            },
            "formal execution plan",
        )
        schema = data["schema_version"]
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise FormalPlanningError("formal execution-plan schema must be an integer")
        goals_data = data["goals"]
        if not isinstance(goals_data, list):
            raise FormalPlanningError("formal execution-plan goals must be an array")
        restored = cls(
            _require_string(
                data["compilation_identity"], "formal compilation identity"
            ),
            _require_string(
                data["verification_identity"], "formal verification identity"
            ),
            tuple(FormalGoalPlan.from_data(item) for item in goals_data),
            schema,
        )
        plan_identity = _require_string(
            data["plan_identity"], "formal execution-plan identity"
        )
        if plan_identity != restored.plan_identity:
            raise FormalPlanningError(
                "formal execution-plan identity does not match its contents"
            )
        return restored

    @classmethod
    def from_json(cls, text: str) -> "FormalExecutionPlan":
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise FormalPlanningError("formal execution plan is not valid JSON") from error
        return cls.from_data(data)


__all__ = [
    "FORMAL_EXECUTION_PLAN_SCHEMA",
    "FormalBackendArtifactRef",
    "FormalExecutableRoute",
    "FormalExecutionPlan",
    "FormalGoalPlan",
    "FormalPlanGoalKind",
    "FormalPlanningError",
    "FormalRouteKind",
    "FormalSkipCode",
    "FormalSkipReason",
]
