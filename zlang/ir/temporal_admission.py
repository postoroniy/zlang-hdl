"""Admission timing for bounded non-interleaved temporal implementations.

This module owns only transaction admission.  It deliberately knows nothing
about arithmetic, lowering, resource cost, or formal proof construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TemporalAdmissionPolicy(str, Enum):
    """Capacity-one admission behaviour after an output transaction."""

    BLOCK_UNTIL_IDLE = "block_until_idle"
    RETIRE_AND_RELOAD = "retire_and_reload"


@dataclass(frozen=True)
class TemporalAdmission:
    """Timing derived from one completed non-interleaved schedule."""

    policy: TemporalAdmissionPolicy
    latency: int
    initiation_interval: int
    capacity: int = 1

    def __post_init__(self) -> None:
        if self.capacity != 1:
            raise ValueError("bounded temporal admission currently requires capacity=1")
        if self.latency < 1 or self.initiation_interval < self.latency:
            raise ValueError("temporal admission timing is invalid")
        expected = (
            self.latency
            if self.policy is TemporalAdmissionPolicy.RETIRE_AND_RELOAD
            else self.latency + 1
        )
        if self.initiation_interval != expected:
            raise ValueError("temporal admission II must be derived from its policy")


def derive_noninterleaved_admission(
    *,
    final_result_cycle: int,
    policy: TemporalAdmissionPolicy,
) -> TemporalAdmission:
    """Derive capacity-one latency and II from the scheduled result edge.

    The source transfer is at edge zero.  A result registered by the final
    scheduled operation becomes externally valid on the following edge.
    """

    if final_result_cycle < 0:
        raise ValueError("temporal schedule result cycle must be non-negative")
    latency = final_result_cycle + 1
    return TemporalAdmission(
        policy=policy,
        latency=latency,
        initiation_interval=(
            latency
            if policy is TemporalAdmissionPolicy.RETIRE_AND_RELOAD
            else latency + 1
        ),
    )


__all__ = [
    "TemporalAdmission",
    "TemporalAdmissionPolicy",
    "derive_noninterleaved_admission",
]
