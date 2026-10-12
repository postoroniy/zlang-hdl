"""Shared simulation diagnostics independent of any execution engine."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir.verification import VerificationGoal, VerificationScope
from zlang.source import SourceOrigin


class SimulationError(ValueError):
    """A simulation request does not match its typed hardware interface."""


class ProtocolViolation(SimulationError):
    """A protocol endpoint violated its cycle-to-cycle contract."""


def verification_origin_text(origin: SourceOrigin | None) -> str:
    if origin is None:
        return ""
    rendered = origin.render()
    if origin.source_unit is not None:
        rendered = f"{origin.source_unit}:{rendered}"
    return f" at {rendered}"


class VerificationAssertionError(SimulationError):
    """One sampled source ``assert`` or ``ensure`` goal evaluated false."""

    def __init__(
        self,
        scope: VerificationScope,
        goal: VerificationGoal,
        cycle: int,
    ) -> None:
        self.scope_id = scope.semantic_id
        self.scope_name = scope.name
        self.goal_id = goal.semantic_id
        self.goal_name = goal.name
        self.goal_kind = goal.kind
        self.cycle = cycle
        self.source_origin = goal.source_origin
        super().__init__(
            f"verification {goal.kind.value} '{scope.name}.{goal.name}' failed "
            f"at cycle {cycle}{verification_origin_text(goal.source_origin)}"
        )


@dataclass(frozen=True)
class VerificationRequirementViolation:
    """One environment precondition that did not hold at a sampled cycle."""

    scope_id: str
    scope_name: str
    requirement_id: str
    requirement_name: str
    cycle: int
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class VerificationCoverWitness:
    """The first sampled cycle at which one cover goal evaluated true."""

    scope_id: str
    scope_name: str
    goal_id: str
    goal_name: str
    cycle: int
    source_origin: SourceOrigin | None = None


__all__ = [
    "ProtocolViolation",
    "SimulationError",
    "VerificationAssertionError",
    "VerificationCoverWitness",
    "VerificationRequirementViolation",
]
