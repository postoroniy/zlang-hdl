#!/usr/bin/env python3
"""Replay one immutable verification bundle through a fixed solver matrix."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from zlang.common import stable_pretty_json
from zlang.ir.formal import ProofMode
from zlang.verification_bundle import (
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle_staged,
)


@dataclass(frozen=True)
class SolverReplay:
    solver: str
    role: str
    outcome: str
    run_identity: str
    result_vector: tuple[tuple[str, str, str, str, int], ...]
    counterexample_vector: tuple[
        tuple[str, int | None, tuple[tuple[str, str], ...]], ...
    ]
    tool_versions: tuple[tuple[str, str], ...]

    def to_data(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "counterexample_vector": [
                [property_id, cycle, [list(value) for value in values]]
                for property_id, cycle, values in self.counterexample_vector
            ],
            "result_vector": [list(item) for item in self.result_vector],
            "role": self.role,
            "run_identity": self.run_identity,
            "solver": self.solver,
            "tool_versions": [list(item) for item in self.tool_versions],
        }


def _result_vector(report) -> tuple[tuple[str, str, str, str, int], ...]:
    return tuple(
        sorted(
            (
                item.property_id,
                item.kind,
                item.status,
                item.mode,
                item.depth,
            )
            for item in report.results
        )
    )


def _counterexample_vector(
    report,
) -> tuple[tuple[str, int | None, tuple[tuple[str, str], ...]], ...]:
    return tuple(
        sorted(
            (
                item.property_id,
                item.counterexample.cycle,
                tuple(item.counterexample.values),
            )
            for item in report.results
            if item.counterexample is not None
        )
    )


def replay_solver_matrix(
    bundle: Path,
    *,
    required_solvers: tuple[str, ...],
    corroborating_solvers: tuple[str, ...] = (),
    mode: ProofMode = ProofMode.BMC,
    depth: int = 20,
    timeout_seconds: int = 120,
    jobs: int = 1,
    work_root: Path,
) -> dict[str, object]:
    """Return independent reports and require exact status-vector agreement."""

    solvers = (*required_solvers, *corroborating_solvers)
    if not required_solvers:
        raise ValueError("formal solver matrix requires at least one required solver")
    if any(not solver or any(character.isspace() for character in solver) for solver in solvers):
        raise ValueError("formal solver names must be non-empty tokens")
    if len(set(solvers)) != len(solvers):
        raise ValueError("formal solver matrix entries must be unique")

    loaded = load_verification_bundle(bundle)
    bundle_identity = loaded.manifest.bundle_identity or loaded.manifest.computed_identity
    replays: list[SolverReplay] = []
    for solver in solvers:
        report = run_verification_bundle_staged(
            loaded,
            config=VerificationRunConfig(
                mode=mode,
                solver=solver,
                depth=depth,
                timeout_seconds=timeout_seconds,
                jobs=jobs,
                route="smtbmc",
            ),
            work_directory=work_root / solver,
        )
        replays.append(
            SolverReplay(
                solver,
                "required" if solver in required_solvers else "corroborating",
                report.outcome,
                report.run_identity,
                _result_vector(report),
                _counterexample_vector(report),
                report.tool_versions,
            )
        )

    reference = replays[0].result_vector
    reference_counterexamples = replays[0].counterexample_vector
    agreement = all(
        item.result_vector == reference
        and item.counterexample_vector == reference_counterexamples
        for item in replays[1:]
    )
    return {
        "agreement": agreement,
        "bundle_identity": bundle_identity,
        "depth": depth,
        "mode": mode.value,
        "replays": [item.to_data() for item in replays],
        "route": "smtbmc",
        "schema": "zlang-formal-solver-matrix-v1",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--solver", action="append", dest="required_solvers")
    parser.add_argument(
        "--corroborating-solver", action="append", dest="corroborating_solvers"
    )
    parser.add_argument("--mode", choices=("bmc", "prove"), default="bmc")
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120, dest="timeout_seconds")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--expect", choices=("passed", "failed"), default="passed")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    payload = replay_solver_matrix(
        arguments.bundle,
        required_solvers=tuple(
            arguments.required_solvers or ("z3", "boolector", "bitwuzla")
        ),
        corroborating_solvers=tuple(arguments.corroborating_solvers or ()),
        mode=ProofMode(arguments.mode),
        depth=arguments.depth,
        timeout_seconds=arguments.timeout_seconds,
        jobs=arguments.jobs,
        work_root=arguments.work_root,
    )
    rendered = stable_pretty_json(payload)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    outcomes = tuple(item["outcome"] for item in payload["replays"])
    if not payload["agreement"] or any(item != arguments.expect for item in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
