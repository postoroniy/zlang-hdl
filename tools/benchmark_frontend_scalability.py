#!/usr/bin/env python3
"""Measure the bounded shared-DAG frontend regression family.

Each size runs in a fresh process so peak RSS is attributable to that one
compilation.  The witness is a natural straight-line integer mixer; changing
the number of rounds changes only its semantic DAG depth.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

from zlang.compiler import create_file_compilation_session


SCHEMA = "zlang-frontend-shared-dag-scalability-v1"
DEFAULT_STEPS = (12, 16, 24, 32, 48, 64)
_CONSTANTS = (
    0x9E3779B9,
    0x7F4A7C15,
    0x94D049BB,
    0xED5AD4BB,
    0xAC4C1B51,
    0x31848BAB,
    0x4CF5AD43,
    0x1B873593,
    0x85EBCA6B,
    0xC2B2AE35,
    0x27D4EB2F,
    0x165667B1,
    0xD3A2646C,
    0xFD7046C5,
    0xB55A4F09,
    0x6C8E9CF5,
)
_SHIFTS = ((">>", 7), ("<<", 9), (">>", 11), ("<<", 5))


def mixer_source(steps: int) -> str:
    """Return the exact scalable family used by the historical 64-step case."""

    if steps < 1:
        raise ValueError("step count must be positive")
    lines = [
        "module FrontendSharedDagScalability {",
        "    in seed:u32",
        "    out result:u32",
        "    state_00:u32=seed",
    ]
    for step in range(1, steps + 1):
        operator, shift = _SHIFTS[(step - 1) % len(_SHIFTS)]
        constant = _CONSTANTS[(step - 1) % len(_CONSTANTS)]
        lines.append(
            f"    state_{step:02d}:u32=truncate<32>((state_{step - 1:02d} "
            f"^ (state_{step - 1:02d} {operator} {shift})) + 0x{constant:08x})"
        )
    lines.extend((f"    result=state_{steps:02d}", "}"))
    return "\n".join(lines) + "\n"


def _stats_payload(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        name: int(getattr(value, name))
        for name in (
            "requests",
            "hits",
            "unique_nodes",
            "provenance_occurrences",
            "expression_requests",
            "expression_cache_hits",
            "unique_expression_visits",
            "unique_object_visits",
        )
        if hasattr(value, name)
    }


def _worker(steps: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"zlang-front-{steps}-") as directory:
        source = Path(directory) / "witness.zhl"
        source.write_text(mixer_source(steps), encoding="utf-8")
        stages: dict[str, float] = {}
        started = time.perf_counter()
        session = create_file_compilation_session(
            source, top="FrontendSharedDagScalability"
        )
        stages["session"] = time.perf_counter() - started
        started = time.perf_counter()
        semantic = session.semantic_ir
        stages["semantic"] = time.perf_counter() - started
        started = time.perf_counter()
        planning = session.planning
        stages["planning"] = time.perf_counter() - started
        started = time.perf_counter()
        plan = session.simulation_plan
        stages["simulation_plan"] = time.perf_counter() - started
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "steps": steps,
            "stage_seconds": {
                name: round(seconds, 9) for name, seconds in stages.items()
            },
            "peak_rss_kib": int(usage.ru_maxrss),
            "semantic_arena": _stats_payload(
                semantic.semantic_expression_arena_statistics
            ),
            "normalization": _stats_payload(
                planning.module.selected_value_normalization_statistics
            ),
            "canonical_nodes": len(session.high_level_ir.expressions),
            "selected_canonical_nodes": len(session.optimization_ir.expressions),
            "primitive_nodes": len(plan.payload["nodes"]),
            "plan_bytes": len(plan.to_bytes()),
        }


def _run_fresh_process(steps: int) -> dict[str, object]:
    completed = subprocess.run(
        (sys.executable, __file__, "--worker", str(steps)),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", nargs="+", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=int)
    arguments = parser.parse_args(argv)
    if arguments.worker is not None:
        print(json.dumps(_worker(arguments.worker), sort_keys=True, separators=(",", ":")))
        return 0
    if any(step < 1 for step in arguments.steps):
        parser.error("steps must be positive")
    report = {
        "schema": SCHEMA,
        "cases": [_run_fresh_process(step) for step in arguments.steps],
    }
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if arguments.output is None:
        print(encoded, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
