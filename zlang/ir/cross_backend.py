"""Backend-independent cross-backend equivalence IR (M38)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.comparison_window import ComparisonWindow
from zlang.source import SourceOrigin


class CrossBackendError(ValueError):
    """Cross-backend artifacts or observations are incompatible."""


class CrossBackendRelation(str, Enum):
    SAME_CYCLE_VALUE = "same_cycle_value"
    FIXED_LATENCY_VALUE = "fixed_latency_value"


class CrossBackendStatus(str, Enum):
    PROVEN = "proven"
    FAILED = "failed"
    BOUNDED_PASS = "bounded_pass"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class CrossBackendMode(str, Enum):
    PROVE = "prove"
    BMC = "bmc"


@dataclass(frozen=True)
class CrossBackendProperty:
    id: str
    relation: CrossBackendRelation
    selected_ir_identity: str
    observable_ids: tuple[str, ...]
    clock_domain: str | None
    reset_domain: str | None
    left_latency: int
    right_latency: int
    left_ii: int = 1
    right_ii: int = 1
    comparison_window: ComparisonWindow | None = None
    source_origin: SourceOrigin | None = None
    clock_domain_contract: ClockDomain | None = None

    @property
    def latency_delta(self) -> int:
        return self.right_latency - self.left_latency

    def __post_init__(self) -> None:
        if not self.id or not self.selected_ir_identity or not self.observable_ids:
            raise CrossBackendError("cross-backend properties require identity and observables")
        if len(set(self.observable_ids)) != len(self.observable_ids):
            raise CrossBackendError("cross-backend observables must be unique")
        if self.left_latency < 0 or self.right_latency < 0:
            raise CrossBackendError("cross-backend latency cannot be negative")
        if self.left_ii != 1 or self.right_ii != 1:
            raise CrossBackendError("cross-backend equivalence requires II=1")
        if self.relation is CrossBackendRelation.SAME_CYCLE_VALUE and self.left_latency != self.right_latency:
            raise CrossBackendError("same-cycle cross-backend equivalence requires equal latency")
        if (
            self.relation is CrossBackendRelation.FIXED_LATENCY_VALUE
            and self.clock_domain_contract is None
            and (not self.clock_domain or not self.reset_domain)
        ):
            raise CrossBackendError("fixed-latency cross-backend equivalence requires clock/reset domains")
        if self.relation is CrossBackendRelation.FIXED_LATENCY_VALUE and self.left_latency != self.right_latency:
            raise CrossBackendError("fixed-latency cross-backend equivalence requires equal backend latency")
        contract = self.clock_domain_contract
        if contract is not None:
            if not isinstance(contract, ClockDomain):
                raise CrossBackendError(
                    "cross-backend physical domain must use typed ClockDomain IR"
                )
            try:
                contract.validate()
            except ValueError as error:
                raise CrossBackendError(str(error)) from error
            if contract.power_up is not PowerUpPolicy.UNSPECIFIED:
                raise CrossBackendError(
                    "cross-backend equivalence does not support power_up reset"
                )
            if self.clock_domain not in {None, contract.clock}:
                raise CrossBackendError(
                    "cross-backend clock-domain name disagrees with its physical contract"
                )
            if self.reset_domain not in {None, contract.reset}:
                raise CrossBackendError(
                    "cross-backend reset-domain name disagrees with its physical contract"
                )
            if self.clock_domain is None:
                object.__setattr__(self, "clock_domain", contract.clock)
            if self.reset_domain is None:
                object.__setattr__(self, "reset_domain", contract.reset)
        elif self.relation is CrossBackendRelation.FIXED_LATENCY_VALUE:
            # Preserve the frozen positional M38 construction API while making
            # the legacy physical contract explicit in the property itself.
            assert self.clock_domain is not None and self.reset_domain is not None
            contract = ClockDomain(self.clock_domain, self.reset_domain)
            object.__setattr__(self, "clock_domain_contract", contract)
        expected_window = (
            ComparisonWindow.same_cycle()
            if self.relation is CrossBackendRelation.SAME_CYCLE_VALUE
            else ComparisonWindow.reset_fill(
                max(1, self.left_latency, self.right_latency),
                reset_release_cycles=(
                    0 if contract is None else contract.reset_release_cycles
                ),
            )
        )
        if self.comparison_window is None:
            object.__setattr__(self, "comparison_window", expected_window)
        elif not isinstance(self.comparison_window, ComparisonWindow):
            raise CrossBackendError(
                "cross-backend comparison window must use typed IR"
            )
        elif self.comparison_window != expected_window:
            raise CrossBackendError(
                "cross-backend comparison window does not match timing"
            )


@dataclass(frozen=True)
class CrossBackendCounterexample:
    property_id: str
    semantic_signal_id: str | None
    cycle: int | None
    sample_cycle: int | None
    left_backend: str
    right_backend: str
    left_artifact_hash: str
    right_artifact_hash: str
    left_rtl_path: str | None = None
    right_rtl_path: str | None = None
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class CrossBackendResult:
    property_id: str
    status: CrossBackendStatus
    mode: CrossBackendMode
    engine: str | None
    solver: str | None
    depth: int | None
    relation: CrossBackendRelation
    latency_delta: int
    selected_ir_identity: str
    left_backend: str
    right_backend: str
    left_artifact_hash: str
    right_artifact_hash: str
    manifest_version: int
    observable_signal_id: str | None = None
    source_origin: SourceOrigin | None = None
    counterexample: CrossBackendCounterexample | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status in {
            CrossBackendStatus.BOUNDED_PASS,
            CrossBackendStatus.PROVEN,
            CrossBackendStatus.FAILED,
        } and (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or self.depth < 1
        ):
            raise CrossBackendError(
                "decisive cross-backend results require a positive depth"
            )
        if self.status is CrossBackendStatus.BOUNDED_PASS and self.mode is not CrossBackendMode.BMC:
            raise CrossBackendError("bounded_pass is valid only for BMC")
        if self.status is CrossBackendStatus.PROVEN and self.mode is not CrossBackendMode.PROVE:
            raise CrossBackendError("proven is valid only for prove mode")
        if (self.counterexample is not None) != (
            self.status is CrossBackendStatus.FAILED
        ):
            raise CrossBackendError(
                "failed cross-backend results require exactly one counterexample"
            )
