"""Reset/fill comparison-window semantics for M36."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ComparisonWindowError(ValueError):
    """A timed equivalence comparison window is malformed."""


class ComparisonWindowKind(str, Enum):
    SAME_CYCLE = "same_cycle"
    RESET_FILL = "reset_fill"


@dataclass(frozen=True)
class ComparisonWindow:
    """The exact cycle window in which an equivalence assertion can fire.

    Timed M36 miters assume reset in the initial state, clear their valid
    history on the reset edge, and sample assertions on subsequent active
    clock edges.  A synchronized asynchronous reset additionally keeps the
    checker in the reset epoch for its exact release interval.  Release cycles
    are deliberately separate from ``fill_cycles``: they delay the first
    comparison but do not change implementation latency or counterexample
    sample attribution.

    With the current bounded-trace convention, a reset/fill window therefore
    needs ``reset_release_cycles + fill_cycles + 4`` states before one
    comparison can be observed.  Keeping this calculation in IR prevents a
    shallow, vacuous BMC pass from being reported as useful equivalence
    evidence.
    """

    kind: ComparisonWindowKind
    fill_cycles: int = 0
    reset_release_cycles: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ComparisonWindowKind(self.kind))
        if (
            isinstance(self.reset_release_cycles, bool)
            or not isinstance(self.reset_release_cycles, int)
            or self.reset_release_cycles < 0
        ):
            raise ComparisonWindowError(
                "comparison reset-release cycles must be a non-negative integer"
            )
        if self.kind is ComparisonWindowKind.SAME_CYCLE:
            if self.fill_cycles != 0 or self.reset_release_cycles != 0:
                raise ComparisonWindowError(
                    "same-cycle comparison cannot have reset/fill or release cycles"
                )
        elif self.fill_cycles < 1:
            raise ComparisonWindowError(
                "reset/fill comparison requires at least one fill cycle"
            )

    @classmethod
    def same_cycle(cls) -> "ComparisonWindow":
        return cls(ComparisonWindowKind.SAME_CYCLE, 0, 0)

    @classmethod
    def reset_fill(
        cls,
        fill_cycles: int,
        *,
        reset_release_cycles: int = 0,
    ) -> "ComparisonWindow":
        return cls(
            ComparisonWindowKind.RESET_FILL,
            fill_cycles,
            reset_release_cycles,
        )

    @property
    def first_comparison_cycle(self) -> int:
        return (
            0
            if self.kind is ComparisonWindowKind.SAME_CYCLE
            else self.reset_release_cycles + self.fill_cycles + 2
        )

    @property
    def minimum_bmc_depth(self) -> int:
        return (
            1
            if self.kind is ComparisonWindowKind.SAME_CYCLE
            else self.reset_release_cycles + self.fill_cycles + 4
        )

    def bmc_depth_reaches_comparison(self, depth: int) -> bool:
        return depth >= self.minimum_bmc_depth

    def bmc_unreached_reason(self, depth: int) -> str:
        return (
            "comparison_window_unreached: requested BMC depth "
            f"{depth} is below minimum {self.minimum_bmc_depth} for "
            f"{self.fill_cycles} fill cycle(s) after "
            f"{self.reset_release_cycles} reset-release cycle(s)"
        )

    def to_data(self) -> dict[str, int | str]:
        return {
            "kind": self.kind.value,
            "fill_cycles": self.fill_cycles,
            "reset_release_cycles": self.reset_release_cycles,
            "first_comparison_cycle": self.first_comparison_cycle,
            "minimum_bmc_depth": self.minimum_bmc_depth,
        }
