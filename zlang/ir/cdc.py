"""Clock-domain and explicit crossing concepts in typed IR."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from zlang.common import stable_digest


class CrossingKind(str, Enum):
    SYNC_LEVEL = "sync_level"
    PULSE_TOGGLE = "pulse_toggle"
    HANDSHAKE = "handshake"
    ASYNC_FIFO = "async_fifo"


class ClockEdge(str, Enum):
    """Physical active edge of one clock input."""

    RISING = "rising"
    FALLING = "falling"


class ResetMode(str, Enum):
    """Physical reset sampling contract."""

    SYNCHRONOUS = "synchronous"
    ASYNCHRONOUS = "asynchronous"


class ResetPolarity(str, Enum):
    """Physical assertion level of one reset input."""

    ACTIVE_HIGH = "active_high"
    ACTIVE_LOW = "active_low"


class ResetReleaseMode(str, Enum):
    """How an asserted physical reset is released into its clock domain."""

    NATIVE = "native"
    SYNCHRONIZED = "synchronized"


class PowerUpPolicy(str, Enum):
    """Power-up contract independent from ordinary reset behavior."""

    UNSPECIFIED = "unspecified"
    RESET = "reset"


@dataclass(frozen=True)
class Crossing:
    kind: CrossingKind
    depth: int | None = None


@dataclass(frozen=True)
class ClockDomain:
    clock: str
    reset: str
    edge: ClockEdge = ClockEdge.RISING
    reset_mode: ResetMode = ResetMode.SYNCHRONOUS
    reset_polarity: ResetPolarity = ResetPolarity.ACTIVE_HIGH
    power_up: PowerUpPolicy = PowerUpPolicy.UNSPECIFIED
    source_origin: Any | None = field(default=None, compare=False)
    reset_release_mode: ResetReleaseMode = ResetReleaseMode.NATIVE
    reset_release_cycles: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate the complete physical reset contract.

        Canonical restoration calls this method again so malformed serialized
        or programmatically corrupted metadata cannot bypass the constructor
        boundary of this frozen dataclass.
        """

        for value, label in ((self.clock, "clock"), (self.reset, "reset")):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"clock-domain {label} must be a non-empty string"
                )
        if self.clock == self.reset:
            raise ValueError(
                "clock-domain clock and reset must be distinct signals"
            )
        enum_fields = (
            (self.edge, ClockEdge, "clock edge"),
            (self.reset_mode, ResetMode, "reset mode"),
            (self.reset_polarity, ResetPolarity, "reset polarity"),
            (self.power_up, PowerUpPolicy, "power-up policy"),
            (self.reset_release_mode, ResetReleaseMode, "reset release mode"),
        )
        for value, expected_type, label in enum_fields:
            if not isinstance(value, expected_type):
                raise ValueError(f"{label} is not recognized")
        if (
            isinstance(self.reset_release_cycles, bool)
            or not isinstance(self.reset_release_cycles, int)
        ):
            raise ValueError("reset release cycles must be an integer")
        if self.reset_release_cycles < 0:
            raise ValueError("reset release cycles cannot be negative")
        if self.reset_release_mode is ResetReleaseMode.NATIVE:
            if self.reset_release_cycles != 0:
                raise ValueError("native reset release requires zero release cycles")
            return
        if self.reset_mode is not ResetMode.ASYNCHRONOUS:
            raise ValueError(
                "synchronized reset release requires asynchronous assertion"
            )
        if self.reset_release_cycles != 2:
            raise ValueError(
                "synchronized reset release currently requires exactly two cycles"
            )

    @property
    def is_legacy_default(self) -> bool:
        return (
            self.edge is ClockEdge.RISING
            and self.reset_mode is ResetMode.SYNCHRONOUS
            and self.reset_polarity is ResetPolarity.ACTIVE_HIGH
            and self.power_up is PowerUpPolicy.UNSPECIFIED
            and self.reset_release_mode is ResetReleaseMode.NATIVE
            and self.reset_release_cycles == 0
        )


def clock_domain_data(domain: ClockDomain | None) -> object:
    """Serialize one exact physical contract without source provenance."""

    if domain is None:
        return None
    domain.validate()
    return {
        "clock": domain.clock,
        "reset": domain.reset,
        "edge": domain.edge.value,
        "reset_mode": domain.reset_mode.value,
        "reset_polarity": domain.reset_polarity.value,
        "power_up": domain.power_up.value,
        "reset_release_mode": domain.reset_release_mode.value,
        "reset_release_cycles": domain.reset_release_cycles,
    }


def clock_domain_contract_identity(domain: ClockDomain) -> str:
    """Return the backend-independent physical-domain contract identity.

    The versioned payload is shared with :class:`PhysicalDomainManifest` so
    formal planning can retain and validate the exact domain identity without
    introducing an IR dependency on a particular backend manifest.
    """

    domain.validate()
    return stable_digest({
        "schema": "zlang-physical-domain-contract-v1",
        "clock": domain.clock,
        "reset": domain.reset,
        "clock_edge": domain.edge.value,
        "reset_mode": domain.reset_mode.value,
        "reset_polarity": domain.reset_polarity.value,
        "reset_release_mode": domain.reset_release_mode.value,
        "reset_release_cycles": domain.reset_release_cycles,
        "power_up": domain.power_up.value,
    })


def clock_domain_from_data(value: object) -> ClockDomain | None:
    """Strictly restore one contract emitted by :func:`clock_domain_data`."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("clock-domain contract must be an object")
    expected = {
        "clock", "reset", "edge", "reset_mode", "reset_polarity",
        "power_up", "reset_release_mode", "reset_release_cycles",
    }
    if set(value) != expected:
        raise ValueError("clock-domain contract fields are invalid")
    clock = value["clock"]
    reset = value["reset"]
    cycles = value["reset_release_cycles"]
    if not isinstance(clock, str) or not clock:
        raise ValueError("clock-domain clock must be a non-empty string")
    if not isinstance(reset, str) or not reset:
        raise ValueError("clock-domain reset must be a non-empty string")
    if isinstance(cycles, bool) or not isinstance(cycles, int):
        raise ValueError("clock-domain reset-release cycles must be an integer")
    try:
        return ClockDomain(
            clock,
            reset,
            ClockEdge(value["edge"]),
            ResetMode(value["reset_mode"]),
            ResetPolarity(value["reset_polarity"]),
            PowerUpPolicy(value["power_up"]),
            reset_release_mode=ResetReleaseMode(value["reset_release_mode"]),
            reset_release_cycles=cycles,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"clock-domain contract is invalid: {error}") from error
