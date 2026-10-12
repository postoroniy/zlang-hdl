"""Immutable backend-independent formal verification records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.cdc import ClockDomain
from zlang.ir.formal_predicates import (
    FormalPredicate,
    FormalPredicateError,
    require_predicate,
)
from zlang.source import SourceOrigin

class FormalError(ValueError):
    """A property or binding cannot be represented safely."""


class PropertyKind(str, Enum):
    ASSUMPTION = "assumption"
    ASSERTION = "assertion"


class TemporalForm(str, Enum):
    SAME_CYCLE = "same_cycle"
    NEXT_CYCLE = "next_cycle"
    STABLE_WHILE = "stable_while"
    BOUNDED_IMPLICATION = "bounded_implication"


class FormalPropertyClassification(str, Enum):
    """Truthful strength of one generated safety property.

    A range assertion over the complete representation of a fixed-width scalar
    is useful as a binding/width smoke check, but it cannot establish a
    behavioral invariant beyond the RTL representation itself.  Keeping that
    distinction in typed IR prevents reports from overstating the result while
    preserving the historical executable property and its stable identity.
    """

    BEHAVIORAL = "behavioral"
    REPRESENTATION_INVARIANT = "representation_invariant"


class Ownership(str, Enum):
    ENVIRONMENT = "environment"
    IMPLEMENTATION = "implementation"
    SOURCE_ENDPOINT = "source_endpoint"
    SINK_ENDPOINT = "sink_endpoint"
    SCHEDULER = "scheduler"


class FormalStatus(str, Enum):
    PROVEN = "proven"
    FAILED = "failed"
    BOUNDED_PASS = "bounded_pass"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class ProofMode(str, Enum):
    PROVE = "prove"
    BMC = "bmc"


class CoverStatus(str, Enum):
    """Outcome of one bounded reachability query.

    Cover results deliberately use a status vocabulary separate from safety
    proofs.  In particular, exhausting a bounded search without a witness is
    not a proof that the cover is unreachable.
    """

    WITNESSED = "witnessed"
    BOUNDED_UNREACHED = "bounded_unreached"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SignalBinding:
    """Stable semantic-to-RTL binding published by either backend."""

    semantic_signal_id: str
    rtl_module: str
    rtl_name: str
    width: int
    direction: str
    clock_domain: str | None = None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_signal_id or not self.rtl_module or not self.rtl_name:
            raise FormalError("signal bindings require semantic ID, module, and RTL name")
        if self.width < 1:
            raise FormalError("signal binding width must be positive")
        if self.direction not in {"input", "output", "internal"}:
            raise FormalError(f"unsupported binding direction: {self.direction}")


@dataclass(frozen=True)
class FormalProperty:
    id: str
    kind: PropertyKind
    clock: str
    reset_condition: str | None
    expression: str
    temporal_form: TemporalForm
    ownership: Ownership
    source_origin: SourceOrigin | None = None
    generated_from: str | None = None
    relevant_signals: tuple[str, ...] = ()
    antecedent: str | None = None
    consequent: str | None = None
    min_delay: int | None = None
    max_delay: int | None = None
    # ``expression`` and the optional antecedent/consequent strings are stable
    # report spellings retained for compatibility.  Executable meaning lives
    # exclusively in this structured predicate.
    predicate: FormalPredicate | None = None
    non_executable_reason: str | None = None
    classification: FormalPropertyClassification = (
        FormalPropertyClassification.BEHAVIORAL
    )

    def __post_init__(self) -> None:
        if not self.id or not self.clock or not self.expression:
            raise FormalError("formal properties require an id, clock, and expression")
        if not isinstance(self.classification, FormalPropertyClassification):
            raise FormalError(
                "formal property classification must be a "
                "FormalPropertyClassification"
            )
        if self.predicate is not None:
            try:
                require_predicate(self.predicate)
            except FormalPredicateError as error:
                raise FormalError(str(error)) from error
            observed = self.predicate.observation_ids()
            if self.relevant_signals and tuple(self.relevant_signals) != observed:
                raise FormalError(
                    f"property '{self.id}' relevant_signals do not match its "
                    "structured predicate observations"
                )
            if not self.relevant_signals:
                object.__setattr__(self, "relevant_signals", observed)
        if (
            self.temporal_form is TemporalForm.NEXT_CYCLE
            and not self.consequent
            and self.predicate is None
        ):
            raise FormalError("next_cycle properties require a consequent")
        if self.temporal_form is TemporalForm.BOUNDED_IMPLICATION:
            if self.antecedent is None or self.consequent is None:
                raise FormalError("bounded implication requires antecedent and consequent")
            if self.min_delay is None or self.max_delay is None or self.max_delay < self.min_delay:
                raise FormalError("bounded implication requires a valid delay range")


@dataclass(frozen=True)
class CoverProperty:
    """One backend-independent, bounded reachability goal.

    ``expression`` is a stable report spelling only.  As with
    :class:`FormalProperty`, executable meaning lives exclusively in the
    structured predicate and all observations are resolved through explicit
    backend-published bindings.
    """

    id: str
    clock: str
    reset_condition: str | None
    expression: str
    predicate: FormalPredicate | None
    source_origin: SourceOrigin | None = None
    generated_from: str | None = None
    relevant_signals: tuple[str, ...] = ()
    non_executable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.clock or not self.expression:
            raise FormalError("cover properties require an id, clock, and expression")
        if self.predicate is None:
            if self.non_executable_reason is None:
                raise FormalError(
                    f"cover property '{self.id}' requires a structured predicate"
                )
            return
        try:
            require_predicate(self.predicate)
        except FormalPredicateError as error:
            raise FormalError(str(error)) from error
        observed = self.predicate.observation_ids()
        if self.relevant_signals and tuple(self.relevant_signals) != observed:
            raise FormalError(
                f"cover property '{self.id}' relevant_signals do not match its "
                "structured predicate observations"
            )
        if not self.relevant_signals:
            object.__setattr__(self, "relevant_signals", observed)


@dataclass(frozen=True)
class FormalDesign:
    module_name: str
    properties: tuple[FormalProperty, ...]
    bindings: tuple[SignalBinding, ...]
    covers: tuple[CoverProperty, ...] = ()
    connected_backend: str | None = None
    connected_artifact_hash: str | None = None
    connected_module: str | None = None
    implementation_text: str | None = None
    dut_ports: tuple[SignalBinding, ...] = ()
    non_executable_reason: str | None = None
    # Exact source-side physical contracts used to interpret clock/reset
    # observations.  Kept after connection so harness rendering never has to
    # infer reset behavior from an RTL token or generated name.
    clock_domains: tuple[ClockDomain, ...] = ()

    def __post_init__(self) -> None:
        semantic_ids = tuple(item.semantic_signal_id for item in self.bindings)
        if len(semantic_ids) != len(set(semantic_ids)):
            duplicate = next(item for item in semantic_ids if semantic_ids.count(item) > 1)
            raise FormalError(f"duplicate semantic signal binding: {duplicate}")
        if (self.connected_backend is None) != (self.connected_artifact_hash is None):
            raise FormalError("connected formal design requires backend and artifact hash together")
        if self.connected_artifact_hash is not None:
            if not self.connected_module or self.implementation_text is None:
                raise FormalError(
                    "connected formal design requires an implementation module and RTL text"
                )
        domain_keys = tuple((item.clock, item.reset) for item in self.clock_domains)
        if len(domain_keys) != len(set(domain_keys)):
            raise FormalError("formal design contains duplicate clock/reset domains")


@dataclass(frozen=True)
class Counterexample:
    property_id: str
    cycle: int | None = None
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None


@dataclass(frozen=True)
class FormalResult:
    property_id: str
    status: FormalStatus
    mode: ProofMode
    engine: str | None
    solver: str | None
    depth: int | None
    counterexample: Counterexample | None = None
    source_origin: SourceOrigin | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is FormalStatus.BOUNDED_PASS and self.mode is not ProofMode.BMC:
            raise FormalError("bounded_pass is only valid for BMC mode")
        if self.status is FormalStatus.PROVEN and self.mode is not ProofMode.PROVE:
            raise FormalError("proven is only valid for prove mode")
        if (self.counterexample is not None) != (
            self.status is FormalStatus.FAILED
        ):
            raise FormalError(
                "failed formal results require exactly one counterexample"
            )


@dataclass(frozen=True)
class CoverWitness:
    property_id: str
    cycle: int
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None

    def __post_init__(self) -> None:
        if not self.property_id:
            raise FormalError("cover witness requires a property id")
        if self.cycle < 0:
            raise FormalError("cover witness cycle must be non-negative")


@dataclass(frozen=True)
class CoverResult:
    property_id: str
    status: CoverStatus
    engine: str | None
    solver: str | None
    depth: int | None
    witness: CoverWitness | None = None
    source_origin: SourceOrigin | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.property_id:
            raise FormalError("cover result requires a property id")
        if not isinstance(self.status, CoverStatus):
            raise FormalError("cover result status must be a CoverStatus")
        if self.depth is not None and self.depth < 1:
            raise FormalError("cover result depth must be positive")
        if self.status is CoverStatus.WITNESSED:
            if self.witness is None:
                raise FormalError("witnessed cover result requires witness metadata")
            if self.witness.property_id != self.property_id:
                raise FormalError("cover witness property id does not match its result")
            if self.depth is not None and self.witness.cycle > self.depth:
                raise FormalError("cover witness cycle exceeds the executed depth")
        elif self.witness is not None:
            raise FormalError("only witnessed cover results may carry witness metadata")
        if self.status is CoverStatus.BOUNDED_UNREACHED and self.depth is None:
            raise FormalError("bounded_unreached cover result requires a depth")
