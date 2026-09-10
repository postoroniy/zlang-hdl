#!/usr/bin/env python3
"""Run bounded compiler-owned equivalence and isolated RTL mutation checks.

This tutorial driver uses the existing CLI, immutable candidate replay inputs,
and M36 executor. It does not add assumptions, alter selection, or modify the
published bundle. Mutation files are deliberately broken, separate copies of
the direct-SV implementation; the reference, miter and four-cycle contract stay
unchanged. Bounded success is not an unbounded proof or FPGA timing evidence.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path
import re
import time

from zlang.cli import main as compiler_main
from zlang.compiler_verification_report import CompilerVerificationReport
from zlang.equivalence import artifact_hash, run_equivalence_formal
from zlang.equivalence_result_codec import equivalence_result_to_data
from zlang.ir.equivalence import EquivalenceMode
from zlang.verification_bundle import (
    load_candidate_equivalence_replay,
    load_verification_bundle,
)


DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "examples/verification/math_exploration.zhl"


def mutate_output(rtl: str, output: str, width: int, mutation: str) -> str:
    """Mutate exactly one published output assignment, failing on changed RTL.

    Internal stage names are read from this generated artifact, not used as an
    assumed backend ABI. The missing-stage mutation bypasses the final register
    by substituting its exact next-state expression in the output assignment.
    """
    assignment = re.compile(rf"(?m)^(\s*assign {re.escape(output)} = )([^;]+)(;)$")
    matches = list(assignment.finditer(rtl))
    if len(matches) != 1:
        raise ValueError("expected exactly one continuous published output assignment")
    match = matches[0]
    expression = match.group(2)
    if mutation == "output_bit_flip":
        replacement = f"({expression}) ^ {width}'d1"
    elif mutation == "missing_final_stage":
        stages = set(re.findall(r"\bpipeline_[0-9]+_s[0-9]+\b", expression))
        if len(stages) != 1:
            raise ValueError("expected one final pipeline register in the output")
        stage = stages.pop()
        assignments = re.findall(rf"(?m)^\s*{re.escape(stage)} <= ([^;]+);$", rtl)
        if len(assignments) != 2 or assignments[0] != "'0":
            raise ValueError("expected the final register's reset and next-state assignments")
        replacement = re.sub(rf"\b{re.escape(stage)}\b", f"({assignments[1]})", expression)
    else:
        raise ValueError(f"unknown mutation: {mutation}")
    return rtl[:match.start(2)] + replacement + rtl[match.end(2):]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(output: Path, *, source: Path = DEFAULT_SOURCE, depth: int = 10, timeout: int = 120) -> dict:
    if depth < 8:
        raise ValueError("the four-cycle example requires depth >= 8 for a non-vacuous comparison")
    if timeout < 1:
        raise ValueError("timeout must be positive")
    if output.exists():
        raise ValueError("output directory must not already exist; retained evidence is never overwritten")
    source_hash = artifact_hash(source.read_text(encoding="utf-8"))
    output.mkdir(parents=True)
    started = time.monotonic()
    arguments = [
        str(source), "--top", "MathExplore",
        "--systemverilog", str(output / "MathExplore.sv"),
        "--verify", "--formal-policy", "required_bmc",
        "--formal-depth", str(depth), "--formal-timeout", str(timeout),
        "--formal-max-candidates", "1", "--formal-cache", str(output / "cache"),
        "--verification-bundle", str(output / "bundle"),
        "--verification-report", str(output / "verification.json"),
        "--verification-format", "json",
        "--verification-work-dir", str(output / "work"),
        "--evidence-report", str(output / "evidence.json"),
        "--build-manifest", str(output / "build.json"),
        "--exploration-report", str(output / "exploration.txt"),
    ]
    with (output / "compiler.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                status = compiler_main(arguments)
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 2
    if status != 0:
        raise RuntimeError(f"compiler verification exited {status}; see {output / 'compiler.log'}")
    report = json.loads((output / "verification.json").read_text(encoding="utf-8"))
    sites = load_candidate_equivalence_replay(load_verification_bundle(output / "bundle"))
    if len(sites) != 1 or sites[0].direct_systemverilog is None:
        raise RuntimeError("expected one frozen direct-SV candidate-equivalence site")
    prepared = sites[0].direct_systemverilog
    artifact = prepared.implementation_artifact
    if artifact is None:
        raise RuntimeError("missing retained implementation artifact")
    bindings = [item for item in artifact.bindings if item.semantic_signal_id == prepared.property.implementation_output]
    if len(bindings) != 1:
        raise RuntimeError("missing unambiguous implementation output binding")
    binding = bindings[0]
    checks = {}
    for name in ("shallow_window", "output_bit_flip", "missing_final_stage"):
        directory = output / name
        directory.mkdir()
        implementation = artifact.text
        source_text = prepared.source
        check_depth = prepared.property.comparison_window.minimum_bmc_depth - 1 if name == "shallow_window" else depth
        if name != "shallow_window":
            implementation = mutate_output(implementation, binding.rtl_path, binding.width, name)
            if source_text.count(artifact.text) != 1:
                raise RuntimeError("prepared source does not contain exactly one implementation artifact")
            source_text = source_text.replace(artifact.text, implementation, 1)
        (directory / "implementation.sv").write_text(implementation, encoding="utf-8")
        (directory / "miter.sv").write_text(source_text, encoding="utf-8")
        result = run_equivalence_formal(
            prepared.property, source_text, top=prepared.top,
            backend="direct_systemverilog", mode=EquivalenceMode.BMC,
            depth=check_depth, solver="z3", timeout_seconds=timeout,
            reference_hash=prepared.reference_artifact_hash,
            implementation_hash=artifact_hash(implementation),
            work_directory=directory / "work", trace_metadata=prepared.trace_metadata,
        )
        _write_json(directory / "result.json", equivalence_result_to_data(result))
        expected = "unknown" if name == "shallow_window" else "failed"
        checks[name] = {
            "status": result.status.value, "expected": expected,
            "depth": check_depth, "reason": result.reason,
            "minimum_bmc_depth": prepared.property.comparison_window.minimum_bmc_depth,
            "counterexample": result.counterexample is not None,
            "reference_hash": prepared.reference_artifact_hash,
            "implementation_hash": artifact_hash(implementation),
            "harness_hash": prepared.harness_hash,
            "property_identity": prepared.property_identity,
            "result": str(directory / "result.json"),
        }
    typed_report = CompilerVerificationReport.from_data(report)
    evidence = typed_report.candidate_equivalence[0].evidence_records
    candidate_statuses = [item.status for item in evidence]
    accepted = (
        len(candidate_statuses) == 3
        and all(item == "bounded_pass" for item in candidate_statuses)
        and all(item["status"] == item["expected"] for item in checks.values())
        and all(checks[name]["counterexample"] for name in ("output_bit_flip", "missing_final_stage"))
        and not checks["shallow_window"]["counterexample"]
    )
    if artifact_hash(source.read_text(encoding="utf-8")) != source_hash:
        raise RuntimeError("source changed during verification; retain this run for diagnosis and rerun")
    summary = {
        "schema": "zlang-math-exploration-formal-demo-v1",
        "accepted": accepted, "compiler_exit_code": status,
        "source": str(source.resolve()), "source_sha256": source_hash,
        "top": "MathExplore", "depth": depth, "timeout_seconds": timeout,
        "candidate_statuses": candidate_statuses, "checks": checks,
        "seconds": time.monotonic() - started,
        "claim": "bounded equivalence only; not unbounded proof or measured FPGA timing",
        "verification_report": str(output / "verification.json"),
    }
    _write_json(output / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory (must not exist)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=120)
    arguments = parser.parse_args(argv)
    try:
        summary = run(arguments.output.resolve(), source=arguments.source, depth=arguments.depth, timeout=arguments.timeout)
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
