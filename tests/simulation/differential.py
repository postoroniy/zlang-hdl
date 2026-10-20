"""Test adapter for the production native-versus-Direct-SV comparator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from zlang import sim
from zlang.ir.cdc import ClockDomain
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
    build_timeout: int = 120,
    root_domain: ClockDomain | None = None,
) -> DifferentialTrace:
    """Compare exact events; execution and RTL/C++ build have separate bounds.

    A short event trace does not imply a fast Verilator/C++ build on a loaded
    runner.  Keep the standard 120-second build bound when a test tightens the
    execution timeout.  Neither deadline can turn a mismatch into success.
    """

    result = compare_program(
        sim.compile(source, top=top, root_domain=root_domain),
        events,
        simulator="verilator",
        artifact_directory=directory / "simulation-comparison",
        timeout=timeout,
        build_timeout=build_timeout,
    )
    return DifferentialTrace(
        result.plan_identity,
        result.native_trace,
        result.rtl_trace,
    )


__all__ = ["DifferentialTrace", "run_differential"]
