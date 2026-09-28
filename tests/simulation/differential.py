"""Test adapter for the production native-versus-Direct-SV comparator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from zlang import sim
from zlang.simulation_compare import compare_program


@dataclass(frozen=True)
class DifferentialTrace:
    plan_identity: str
    native: tuple[dict[str, int], ...]
    direct_sv: tuple[dict[str, int], ...]


def run_differential(
    source: Path,
    *,
    top: str,
    events: Sequence[Mapping[str, object]],
    directory: Path,
    timeout: int = 120,
) -> DifferentialTrace:
    """Execute exact events through production native and Verilator paths."""

    result = compare_program(
        sim.compile(source, top=top),
        events,
        simulator="verilator",
        artifact_directory=directory / "simulation-comparison",
        timeout=timeout,
    )
    return DifferentialTrace(
        result.plan_identity,
        result.native_trace,
        result.rtl_trace,
    )


__all__ = ["DifferentialTrace", "run_differential"]
