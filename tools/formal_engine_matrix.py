#!/usr/bin/env python3
"""Qualify independent SBY engines against one immutable verification bundle."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from zlang.common import stable_pretty_json
from zlang.formal_routes import formal_engine_route
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.verification_bundle import (
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle,
)


@dataclass(frozen=True)
class EngineQualification:
    route: str
    solver: str
    mode: ProofMode
    required: bool = True


@dataclass(frozen=True)
class EngineReplay:
    route: str
    solver: str
    mode: str
    required: bool
    outcome: str
    run_identity: str
    property_statuses: tuple[tuple[str, str], ...]
    counterexamples: tuple[
        tuple[str, int | None, tuple[tuple[str, str], ...]], ...
    ]
    tool_versions: tuple[tuple[str, str], ...]

    def to_data(self) -> dict[str, object]:
        return {
            "counterexamples": [
                [property_id, cycle, [list(value) for value in values]]
                for property_id, cycle, values in self.counterexamples
            ],
            "mode": self.mode,
            "outcome": self.outcome,
            "property_statuses": [list(item) for item in self.property_statuses],
            "route": self.route,
            "required": self.required,
            "run_identity": self.run_identity,
            "solver": self.solver,
            "tool_versions": [list(item) for item in self.tool_versions],
        }


QUALIFICATION_ROUTES = (
    EngineQualification("smtbmc", "z3", ProofMode.PROVE),
    EngineQualification("abc-pdr", "pdr", ProofMode.PROVE),
    # The pinned Avy build in OSS CAD Suite 2026-09-30 segfaults on one generated
    # property in the positive witness.  Keep its evidence visible without
    # weakening the required independent-engine gate.
    EngineQualification("aiger-avy", "avy", ProofMode.PROVE, required=False),
    EngineQualification("btor-pono", "pono", ProofMode.BMC),
    EngineQualification("btor-btormc", "btormc", ProofMode.BMC),
)


def _replay(report, qualification: EngineQualification) -> EngineReplay:
    return EngineReplay(
        route=qualification.route,
        solver=qualification.solver,
        mode=qualification.mode.value,
        required=qualification.required,
        outcome=report.outcome,
        run_identity=report.run_identity,
        property_statuses=tuple(
            sorted((item.property_id, item.status) for item in report.results)
        ),
        counterexamples=tuple(
            sorted(
                (
                    item.property_id,
                    item.counterexample.cycle,
                    tuple(item.counterexample.values),
                )
                for item in report.results
                if item.counterexample is not None
            )
        ),
        tool_versions=report.tool_versions,
    )


def qualify_formal_engines(
    bundle: Path,
    *,
    expected: str,
    depth: int,
    timeout_seconds: int,
    jobs: int,
    work_root: Path,
    qualifications: tuple[EngineQualification, ...] = QUALIFICATION_ROUTES,
) -> dict[str, object]:
    """Replay fixed qualification routes without promoting their proof status."""

    if expected not in {"passed", "failed"}:
        raise ValueError("formal engine qualification expects 'passed' or 'failed'")
    if not qualifications:
        raise ValueError("formal engine qualification requires at least one route")
    if len({item.route for item in qualifications}) != len(qualifications):
        raise ValueError("formal engine qualification routes must be unique")

    loaded = load_verification_bundle(bundle)
    bundle_identity = loaded.manifest.bundle_identity or loaded.manifest.computed_identity
    replays: list[EngineReplay] = []
    for qualification in qualifications:
        route = formal_engine_route(qualification.route)
        if qualification.route != "smtbmc" and not route.qualification_only:
            raise ValueError(
                f"formal route '{qualification.route}' is not qualification-only"
            )
        report = run_verification_bundle(
            loaded,
            config=VerificationRunConfig(
                mode=qualification.mode,
                solver=qualification.solver,
                route=qualification.route,
                depth=depth,
                timeout_seconds=timeout_seconds,
                jobs=jobs,
            ),
            work_directory=work_root / qualification.route,
            job_kinds=frozenset({"safety"}),
        )
        replays.append(_replay(report, qualification))

    required_replays = [replay for replay in replays if replay.required]
    if not required_replays:
        raise ValueError("formal engine qualification requires a required route")
    property_ids = tuple(
        property_id for property_id, _ in required_replays[0].property_statuses
    )
    same_properties = all(
        tuple(property_id for property_id, _ in replay.property_statuses) == property_ids
        for replay in required_replays[1:]
    )
    expected_statuses = {
        ProofMode.BMC.value: FormalStatus.BOUNDED_PASS.value,
        ProofMode.PROVE.value: FormalStatus.PROVEN.value,
    }
    if expected == "passed":
        consistent = same_properties and all(
            replay.outcome == "passed"
            and all(
                status == expected_statuses[replay.mode]
                for _, status in replay.property_statuses
            )
            for replay in required_replays
        )
    else:
        failing_properties = tuple(
            property_id
            for property_id, status in required_replays[0].property_statuses
            if status == FormalStatus.FAILED.value
        )
        consistent = (
            same_properties
            and bool(failing_properties)
            and all(replay.outcome == "failed" for replay in required_replays)
            and all(
                tuple(
                    property_id
                    for property_id, status in replay.property_statuses
                    if status == FormalStatus.FAILED.value
                ) == failing_properties
                for replay in required_replays[1:]
            )
            and all(
                all(cycle is not None and bool(values) for _, cycle, values in replay.counterexamples)
                and tuple(item[0] for item in replay.counterexamples)
                == failing_properties
                for replay in required_replays
            )
        )

    return {
        "bundle_identity": bundle_identity,
        "consistent": consistent,
        "depth": depth,
        "expected": expected,
        "required_routes": [
            replay.route for replay in required_replays
        ],
        "replays": [replay.to_data() for replay in replays],
        "schema": "zlang-formal-engine-qualification-v1",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expect", choices=("passed", "failed"), required=True)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120, dest="timeout_seconds")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    payload = qualify_formal_engines(
        arguments.bundle,
        expected=arguments.expect,
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
    return 0 if payload["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
