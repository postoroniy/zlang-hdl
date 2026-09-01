"""Backend-independent selected-architecture equivalence IR (M36)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib

from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.physical_types import physical_signedness
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin


class EquivalenceError(ValueError):
    """An equivalence relation or binding map is not legal."""


class EquivalenceRelation(str, Enum):
    SAME_CYCLE_VALUE = "same_cycle_value"
    FIXED_LATENCY_VALUE = "fixed_latency_value"


class EquivalenceStatus(str, Enum):
    PROVEN = "proven"
    FAILED = "failed"
    BOUNDED_PASS = "bounded_pass"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class EquivalenceMode(str, Enum):
    PROVE = "prove"
    BMC = "bmc"


class BindingSide(str, Enum):
    REFERENCE = "reference"
    IMPLEMENTATION = "implementation"


class SignalRole(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    CLOCK = "clock"
    RESET = "reset"


@dataclass(frozen=True)
class EquivalenceBinding:
    map_version: int
    side: BindingSide
    semantic_signal_id: str
    selected_ir_identity: str
    rtl_module: str
    rtl_path: str
    width: int
    signedness: str
    role: SignalRole
    clock_domain: str | None
    reset_domain: str | None
    backend: str
    artifact_hash: str
    source_origin: SourceOrigin | None = None
    aggregate_endpoint_id: str | None = None
    protocol_specialization_id: str | None = None
    protocol_role: str | None = None
    member_path: tuple[str, ...] = ()
    ownership: str | None = None
    signal_kind: str | None = None
    physical_available: bool = True
    canonical_type: str | None = None

    def __post_init__(self) -> None:
        if self.map_version < 2:
            raise EquivalenceError("M36 binding maps require version 2 or newer")
        if not self.semantic_signal_id or not self.selected_ir_identity:
            raise EquivalenceError("equivalence bindings require semantic and selected identities")
        if self.width < 1:
            raise EquivalenceError("equivalence binding width must be positive")
        if self.signedness not in {"unsigned", "signed", "bit", "bits"}:
            raise EquivalenceError(f"unsupported binding signedness: {self.signedness}")
        if not self.backend or not self.artifact_hash:
            raise EquivalenceError("equivalence bindings require backend and artifact hash")


@dataclass(frozen=True)
class BindingMap:
    entries: tuple[EquivalenceBinding, ...]
    map_version: int = 2

    def validate(self, *, required: tuple[tuple[BindingSide, str], ...] = ()) -> None:
        if self.map_version < 2:
            raise EquivalenceError("binding map version must be at least 2")
        keys = [(item.side, item.semantic_signal_id) for item in self.entries]
        if len(keys) != len(set(keys)):
            duplicate = next(key for key in keys if keys.count(key) > 1)
            raise EquivalenceError(f"duplicate or ambiguous binding: {duplicate[0].value}:{duplicate[1]}")
        by_key = {(item.side, item.semantic_signal_id): item for item in self.entries}
        for key in required:
            if key not in by_key:
                raise EquivalenceError(f"missing required binding: {key[0].value}:{key[1]}")
        for semantic_id in {item.semantic_signal_id for item in self.entries}:
            sides = [item for item in self.entries if item.semantic_signal_id == semantic_id]
            if len(sides) == 2:
                left, right = sides
                if (left.width, left.signedness, left.role) != (right.width, right.signedness, right.role):
                    raise EquivalenceError(f"binding type/role mismatch for {semantic_id}")
                if (
                    left.canonical_type is not None
                    and right.canonical_type is not None
                    and left.canonical_type != right.canonical_type
                ):
                    raise EquivalenceError(
                        f"binding canonical type mismatch for {semantic_id}"
                    )
                if left.artifact_hash == right.artifact_hash and left.side is not right.side:
                    raise EquivalenceError(f"reference/implementation artifact mismatch for {semantic_id}")
                if left.selected_ir_identity != right.selected_ir_identity:
                    raise EquivalenceError(f"selected IR identity mismatch for {semantic_id}")

    def for_side(self, side: BindingSide) -> tuple[EquivalenceBinding, ...]:
        return tuple(item for item in self.entries if item.side is side)


@dataclass(frozen=True)
class EquivalenceProperty:
    id: str
    relation_kind: EquivalenceRelation
    reference_root: str
    implementation_root: str
    canonical_type: HardwareType
    inputs: tuple[str, ...]
    reference_output: str
    implementation_output: str
    reference_latency: int
    implementation_latency: int
    reference_ii: int
    implementation_ii: int
    reference_clock: str | None
    implementation_clock: str | None
    reference_reset: str | None
    implementation_reset: str | None
    latency_delta: int
    comparison_window: ComparisonWindow
    source_origin: SourceOrigin | None = None
    selected_origin: SourceOrigin | None = None
    candidate_class: str = "value"
    clock_domain_contract: ClockDomain | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.reference_root or not self.implementation_root:
            raise EquivalenceError("equivalence property requires stable root identities")
        if self.reference_latency < 0 or self.implementation_latency < 0:
            raise EquivalenceError("equivalence latency cannot be negative")
        if self.reference_ii != 1 or self.implementation_ii != 1:
            raise EquivalenceError("M36 equivalence requires II=1")
        if self.latency_delta != self.implementation_latency - self.reference_latency:
            raise EquivalenceError("latency delta does not match timing records")
        if self.relation_kind is EquivalenceRelation.SAME_CYCLE_VALUE:
            if self.latency_delta != 0:
                raise EquivalenceError("same-cycle equivalence requires zero latency delta")
            if self.reference_clock != self.implementation_clock or self.reference_reset != self.implementation_reset:
                raise EquivalenceError("same-cycle clock/reset domains must match")
        else:
            if self.latency_delta < 0:
                raise EquivalenceError("fixed-latency implementation cannot precede reference")
            if not self.reference_clock or not self.implementation_clock:
                raise EquivalenceError("fixed-latency equivalence requires a clock")
            if self.reference_clock != self.implementation_clock or self.reference_reset != self.implementation_reset:
                raise EquivalenceError("fixed-latency clock/reset domains must match")
        if not isinstance(self.comparison_window, ComparisonWindow):
            raise EquivalenceError("equivalence comparison window must use typed IR")
        contract = self.clock_domain_contract
        if contract is not None:
            if not isinstance(contract, ClockDomain):
                raise EquivalenceError(
                    "equivalence clock domain must use exact typed IR"
                )
            try:
                contract.validate()
            except ValueError as error:
                raise EquivalenceError(str(error)) from error
            if contract.power_up is not PowerUpPolicy.UNSPECIFIED:
                raise EquivalenceError(
                    "M36 executable equivalence does not support power_up reset"
                )
            named_clocks = {
                item
                for item in (self.reference_clock, self.implementation_clock)
                if item is not None
            }
            named_resets = {
                item
                for item in (self.reference_reset, self.implementation_reset)
                if item is not None
            }
            if named_clocks and named_clocks != {contract.clock}:
                raise EquivalenceError(
                    "equivalence physical clock contract does not match timing"
                )
            if named_resets and named_resets != {contract.reset}:
                raise EquivalenceError(
                    "equivalence physical reset contract does not match timing"
                )
        release_cycles = (
            0 if contract is None else contract.reset_release_cycles
        )
        expected_window = (
            ComparisonWindow.same_cycle()
            if self.relation_kind is EquivalenceRelation.SAME_CYCLE_VALUE
            else ComparisonWindow.reset_fill(
                self.latency_delta,
                reset_release_cycles=release_cycles,
            )
        )
        if self.comparison_window != expected_window:
            raise EquivalenceError("equivalence comparison window does not match timing")


@dataclass(frozen=True)
class EquivalenceCounterexample:
    property_id: str
    failure_cycle: int | None = None
    sample_cycle: int | None = None
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None


@dataclass(frozen=True)
class EquivalenceResult:
    property_id: str
    status: EquivalenceStatus
    mode: EquivalenceMode
    engine: str | None
    solver: str | None
    depth: int | None
    relation_kind: EquivalenceRelation
    latency_delta: int
    backend: str
    reference_hash: str
    implementation_hash: str
    binding_map_version: int
    candidate_identity: str
    source_origin: SourceOrigin | None = None
    selected_origin: SourceOrigin | None = None
    counterexample: EquivalenceCounterexample | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status in {
            EquivalenceStatus.BOUNDED_PASS,
            EquivalenceStatus.PROVEN,
            EquivalenceStatus.FAILED,
        } and (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or self.depth < 1
        ):
            raise EquivalenceError(
                "decisive equivalence results require a positive depth"
            )
        if self.status is EquivalenceStatus.BOUNDED_PASS and self.mode is not EquivalenceMode.BMC:
            raise EquivalenceError("bounded_pass is valid only for BMC")
        if self.status is EquivalenceStatus.PROVEN and self.mode is not EquivalenceMode.PROVE:
            raise EquivalenceError("proven is valid only for prove mode")
        if (self.counterexample is not None) != (
            self.status is EquivalenceStatus.FAILED
        ):
            raise EquivalenceError(
                "failed equivalence results require exactly one counterexample"
            )


def stable_equivalence_id(reference_root: str, implementation_root: str, candidate_class: str) -> str:
    digest = hashlib.sha256(f"{reference_root}|{implementation_root}|{candidate_class}".encode()).hexdigest()[:12]
    return f"m36.equiv.{candidate_class}.{digest}"


def signedness(type_: HardwareType) -> str:
    return physical_signedness(type_).value


def classify_equivalence(*, property_id: str, mode: EquivalenceMode, outcome: str,
                         relation_kind: EquivalenceRelation, latency_delta: int,
                         backend: str, reference_hash: str, implementation_hash: str,
                         binding_map_version: int, candidate_identity: str,
                         engine: str | None = None, solver: str | None = None,
                         depth: int | None = None, source_origin: SourceOrigin | None = None,
                         selected_origin: SourceOrigin | None = None,
                         reason: str | None = None) -> EquivalenceResult:
    value = outcome.strip().lower()
    statuses = {"proven": EquivalenceStatus.PROVEN, "failed": EquivalenceStatus.FAILED,
                "unknown": EquivalenceStatus.UNKNOWN, "skipped": EquivalenceStatus.SKIPPED,
                "pass": EquivalenceStatus.BOUNDED_PASS if mode is EquivalenceMode.BMC else EquivalenceStatus.PROVEN}
    if value not in statuses:
        raise EquivalenceError(f"unknown equivalence outcome: {outcome}")
    if value == "failed":
        raise EquivalenceError(
            "failed outcomes require typed counterexample metadata; construct "
            "EquivalenceResult with an EquivalenceCounterexample"
        )
    return EquivalenceResult(property_id, statuses[value], mode, engine, solver, depth,
                             relation_kind, latency_delta, backend, reference_hash,
                             implementation_hash, binding_map_version, candidate_identity,
                             source_origin, selected_origin, reason=reason)
