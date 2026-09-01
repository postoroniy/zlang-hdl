#!/usr/bin/env python3
"""Benchmark immutable verification-bundle execution strategies.

This is evidence tooling, not an alternative verification result authority.
The ordinary per-goal bundle executor remains the oracle.  Aggregate and
shared-Yosys runs consume one already-published immutable bundle and are
accepted only when their job statuses agree with that oracle.  In particular,
this tool never republishes a bundle, changes a property, or infers a passing
result from solver prose.

The aggregate experiment merges the compiler-generated one-goal checker
modules only after their public ABI, DUT module, domain, assumptions, and
physical scope have matched exactly.  It uses one shared DUT and the union of
the unchanged assertions.  A non-passing conjunction remains ineligible until
the ordinary one-goal executor can recover exact status and attribution; this
tool deliberately does not publish aggregate proof evidence.

The shared-Yosys experiment is deliberately advisory.  It lowers the common
implementation once, then prepares one SMT2 model per unchanged checker.  Its
subprocess status is compared with the authoritative bundle result but is not
used to publish proof evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Iterable

from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.verification_bundle import (
    LoadedVerificationBundle,
    VerificationJob,
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle,
)


_MODULE_DECLARATION = re.compile(
    r"(?m)^module (?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<header>\([^;]*\))?;"
)
_INPUT_DECLARATION = re.compile(
    r"input wire(?P<packed> \[[^]]+\])? "
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_WIRE_DECLARATION = re.compile(
    r"^  wire(?P<packed> \[[^]]+\])? "
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*);$"
)
_DUT_INSTANTIATION = re.compile(
    r"^  (?P<module>[A-Za-z_][A-Za-z0-9_]*) dut "
    r"\((?P<connections>.*)\);$"
)
_DUT_CONNECTION = re.compile(
    r"\.(?P<port>[A-Za-z_][A-Za-z0-9_]*)"
    r"\((?P<expression>[A-Za-z_][A-Za-z0-9_]*)\)"
)


@dataclass(frozen=True)
class ProcessEvidence:
    command: tuple[str, ...]
    elapsed_seconds: float
    returncode: int | None
    status: str
    stdout_log: str
    stderr_log: str


@dataclass(frozen=True)
class StrategyEvidence:
    name: str
    elapsed_seconds: float
    invocations: int
    statuses: tuple[tuple[str, str], ...]
    exact_status_match: bool
    candidate_eligible: bool
    reason: str | None = None
    process_evidence: tuple[ProcessEvidence, ...] = ()


@dataclass(frozen=True)
class BenchmarkEvidence:
    bundle_identity: str
    bundle_manifest_hash: str
    top: str
    depth: int
    timeout_seconds: int
    planned_jobs: int
    executable_safety_jobs: int
    executable_cover_jobs: int
    skipped_jobs: int
    strategies: tuple[StrategyEvidence, ...]


@dataclass(frozen=True)
class _Checker:
    job: VerificationJob
    implementation_path: str
    checker_path: str
    checker_text: str
    input_ports: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _CheckerFragment:
    header: str
    wires: tuple[tuple[str, str], ...]
    dut_module: str
    connections: tuple[tuple[str, str], ...]
    setup: tuple[str, ...]
    event: str
    statements: tuple[str, ...]


def _round_seconds(value: float) -> float:
    return round(value, 6)


def _manifest_hash(bundle: LoadedVerificationBundle) -> str:
    return hashlib.sha256(
        (bundle.directory / "manifest.json").read_bytes()
    ).hexdigest()


def _source_records(bundle: LoadedVerificationBundle) -> dict[str, str]:
    return {
        item.logical_path: item.kind for item in bundle.manifest.files
    }


def _checker(bundle: LoadedVerificationBundle, job: VerificationJob) -> _Checker:
    records = _source_records(bundle)
    implementation = tuple(
        item for item in job.source_files
        if records.get(item) == "implementation"
    )
    checker = tuple(
        item for item in job.source_files
        if records.get(item) == "harness"
    )
    if len(implementation) != 1 or len(checker) != 1:
        raise ValueError(
            f"benchmark job '{job.property_id}' does not have one immutable "
            "implementation and one checker"
        )
    text = bundle.read_bytes(checker[0]).decode("utf-8")
    declarations = tuple(_MODULE_DECLARATION.finditer(text))
    match = next(
        (item for item in declarations if item.group("name") == job.top),
        None,
    )
    if match is None:
        raise ValueError(
            f"benchmark checker for '{job.property_id}' does not declare "
            f"top '{job.top}'"
        )
    header = match.group("header") or ""
    ports = tuple(
        ((item.group("packed") or ""), item.group("name"))
        for item in _INPUT_DECLARATION.finditer(header)
    )
    return _Checker(job, implementation[0], checker[0], text, ports)


def _group_key(checker: _Checker) -> tuple[object, ...]:
    job = checker.job
    # Assumptions and physical scope are part of semantics, not a heuristic.
    # Different routes or scopes are never conjoined.
    return (
        checker.implementation_path,
        job.backend,
        job.artifact_hash,
        job.assumption_ids,
        job.clock_domain,
        job.reset_domain,
        job.physical_instance_path,
        job.systemverilog,
    )


def aggregate_groups(
    bundle: LoadedVerificationBundle,
) -> tuple[tuple[_Checker, ...], ...]:
    """Return deterministic, semantics-compatible safety groups."""

    grouped: dict[tuple[object, ...], list[_Checker]] = {}
    for job in bundle.manifest.jobs:
        if not job.executable or job.kind != "safety":
            continue
        checker = _checker(bundle, job)
        grouped.setdefault(_group_key(checker), []).append(checker)
    return tuple(
        tuple(sorted(values, key=lambda item: item.job.property_id))
        for _, values in sorted(grouped.items(), key=lambda item: repr(item[0]))
    )


def _checker_fragment(checker: _Checker) -> _CheckerFragment:
    """Parse only the strict checker shape emitted by the compiler.

    This is benchmark tooling, not a general SystemVerilog parser.  Any shape
    drift fails closed instead of guessing how to merge executable semantics.
    """

    lines = checker.checker_text.splitlines()
    if len(lines) < 8 or lines[0] != "`default_nettype none":
        raise ValueError("aggregate benchmark requires a compiler checker preamble")
    if lines[-1] != "`default_nettype wire" or lines[-2] != "endmodule":
        raise ValueError("aggregate benchmark requires a compiler checker trailer")
    module_prefix = f"module {checker.job.top}"
    if not lines[1].startswith(module_prefix) or not lines[1].endswith(";"):
        raise ValueError("aggregate benchmark checker header is malformed")
    header = lines[1][len(module_prefix):]
    cursor = 2
    wires: list[tuple[str, str]] = []
    while cursor < len(lines):
        match = _WIRE_DECLARATION.fullmatch(lines[cursor])
        if match is None:
            break
        wires.append((match.group("name"), lines[cursor]))
        cursor += 1
    if cursor >= len(lines):
        raise ValueError("aggregate benchmark checker has no DUT")
    dut = _DUT_INSTANTIATION.fullmatch(lines[cursor])
    if dut is None:
        raise ValueError("aggregate benchmark checker DUT is not canonical")
    connection_text = dut.group("connections")
    matches = tuple(_DUT_CONNECTION.finditer(connection_text))
    if ", ".join(item.group(0) for item in matches) != connection_text:
        raise ValueError("aggregate benchmark checker DUT connections are not direct")
    connections = tuple(
        (item.group("port"), item.group("expression")) for item in matches
    )
    cursor += 1
    setup: list[str] = []
    while cursor < len(lines) and not lines[cursor].startswith("  always @("):
        setup.append(lines[cursor])
        cursor += 1
    if cursor >= len(lines) or not lines[cursor].endswith(" begin"):
        raise ValueError("aggregate benchmark checker has no canonical clocked block")
    event = lines[cursor]
    cursor += 1
    statements: list[str] = []
    while cursor < len(lines) and lines[cursor] != "  end":
        if not lines[cursor].startswith("    "):
            raise ValueError("aggregate benchmark checker block is not flat")
        statements.append(lines[cursor])
        cursor += 1
    if cursor != len(lines) - 3:
        raise ValueError("aggregate benchmark checker contains unsupported trailing logic")
    return _CheckerFragment(
        header,
        tuple(wires),
        dut.group("module"),
        connections,
        tuple(setup),
        event,
        tuple(statements),
    )


def _merge_unique(
    values: Iterable[tuple[str, str]], *, description: str
) -> tuple[str, ...]:
    merged: dict[str, str] = {}
    for identity, text in values:
        previous = merged.setdefault(identity, text)
        if previous != text:
            raise ValueError(
                f"aggregate benchmark {description} '{identity}' disagrees"
            )
    return tuple(merged[key] for key in sorted(merged))


def _aggregate_source(
    bundle: LoadedVerificationBundle,
    checkers: tuple[_Checker, ...],
) -> tuple[str, str]:
    if not checkers:
        raise ValueError("aggregate benchmark requires at least one checker")
    implementations = {item.implementation_path for item in checkers}
    if len(implementations) != 1:
        raise ValueError("aggregate benchmark group mixes implementation artifacts")
    implementation = bundle.read_bytes(next(iter(implementations))).decode("utf-8")
    token = hashlib.sha256(
        "\0".join(item.job.property_id for item in checkers).encode()
    ).hexdigest()[:16]
    top = f"zlang_formal_benchmark_{token}"
    fragments = tuple(_checker_fragment(item) for item in checkers)
    first = fragments[0]
    if any(item.header != first.header for item in fragments[1:]):
        raise ValueError("aggregate benchmark checker public ABIs disagree")
    if any(item.dut_module != first.dut_module for item in fragments[1:]):
        raise ValueError("aggregate benchmark checker DUT modules disagree")
    if any(item.event != first.event for item in fragments[1:]):
        raise ValueError("aggregate benchmark checker clock events disagree")
    wires = _merge_unique(
        (item for fragment in fragments for item in fragment.wires),
        description="wire",
    )
    connections = _merge_unique(
        (
            (port, f".{port}({expression})")
            for fragment in fragments
            for port, expression in fragment.connections
        ),
        description="DUT port",
    )
    setup = tuple(dict.fromkeys(
        line for fragment in fragments for line in fragment.setup
    ))
    statements = tuple(dict.fromkeys(
        line for fragment in fragments for line in fragment.statements
    ))
    checker_lines = [
        "`default_nettype none",
        f"module {top}{first.header}",
        *wires,
        f"  {first.dut_module} dut ({', '.join(connections)});",
        *setup,
        first.event,
        *statements,
        "  end",
        "endmodule",
        "`default_nettype wire",
        "",
    ]
    return "\n".join((implementation, "\n".join(checker_lines))), top


def _auxiliary_files(
    bundle: LoadedVerificationBundle,
    jobs: Iterable[VerificationJob],
) -> dict[str, bytes]:
    records = _source_records(bundle)
    result: dict[str, bytes] = {}
    for job in jobs:
        for path in job.source_files:
            if records.get(path) != "companion":
                continue
            name = Path(path).name
            value = bundle.read_bytes(path)
            previous = result.setdefault(name, value)
            if previous != value:
                raise ValueError(
                    f"aggregate benchmark has conflicting companion '{name}'"
                )
    return result


def _status_map(report: object) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.property_id, item.status)
        for item in getattr(report, "results")
    )


def _run_cover_subset(
    bundle: LoadedVerificationBundle,
    *,
    config: VerificationRunConfig,
    work_root: Path,
) -> tuple[dict[str, str], int]:
    """Execute advisory cover jobs instead of borrowing oracle outcomes.

    Aggregate safety and shared-front-end strategies do not alter cover
    semantics.  Their wall-time comparison must nevertheless include those
    jobs; copying oracle results would make a mixed safety/cover bundle appear
    faster without doing equivalent work.
    """

    covers = tuple(
        item for item in bundle.manifest.jobs
        if item.executable and item.kind == "cover"
    )
    if not covers:
        return {}, 0
    report = run_verification_bundle(
        bundle,
        config=config,
        work_directory=work_root,
        job_kinds=frozenset({"cover"}),
    )
    return dict(_status_map(report)), len(covers)


def _run_aggregate_group(
    bundle: LoadedVerificationBundle,
    checkers: tuple[_Checker, ...],
    *,
    config: VerificationRunConfig,
    work_root: Path,
    ordinal: int,
) -> tuple[str, float]:
    source, top = _aggregate_source(bundle, checkers)
    started = time.monotonic()
    result = run_verilog_formal(
        source,
        top=top,
        property_id=f"benchmark.aggregate.{ordinal}",
        mode=config.mode,
        depth=config.depth,
        solver=config.solver,
        engine=config.engine,
        systemverilog=all(item.job.systemverilog for item in checkers),
        timeout_seconds=config.timeout_seconds,
        work_directory=work_root / f"aggregate-{ordinal:04d}",
        auxiliary_files=_auxiliary_files(
            bundle, (item.job for item in checkers)
        ),
    )
    return result.status.value, time.monotonic() - started


def benchmark_aggregate(
    bundle: LoadedVerificationBundle,
    oracle: tuple[tuple[str, str], ...],
    *,
    config: VerificationRunConfig,
    work_root: Path,
) -> StrategyEvidence:
    """Benchmark passing conjunctions; retain the current executor as oracle.

    A failed/unknown conjunction intentionally makes this candidate ineligible.
    Production adoption additionally requires deterministic failure splitting
    and counterexample parity, which this measurement-only runner does not
    claim.
    """

    groups = aggregate_groups(bundle)
    started = time.monotonic()
    statuses: dict[str, str] = {
        item.property_id: FormalStatus.SKIPPED.value
        for item in bundle.manifest.jobs
        if not item.executable
    }
    cover_statuses, invocations = _run_cover_subset(
        bundle,
        config=config,
        work_root=work_root / "covers",
    )
    statuses.update(cover_statuses)
    reason: str | None = None
    for ordinal, group in enumerate(groups):
        status, _ = _run_aggregate_group(
            bundle,
            group,
            config=config,
            work_root=work_root,
            ordinal=ordinal,
        )
        invocations += 1
        if status not in {
            FormalStatus.BOUNDED_PASS.value,
            FormalStatus.PROVEN.value,
        }:
            reason = (
                "aggregate result requires deterministic failure splitting; "
                f"group {ordinal} returned {status}"
            )
            for item in group:
                statuses[item.job.property_id] = status
            continue
        for item in group:
            statuses[item.job.property_id] = status
    ordered = tuple(
        (job.property_id, statuses[job.property_id])
        for job in bundle.manifest.jobs
    )
    exact = ordered == oracle
    if any(len(group) > 1 for group in groups):
        reason = reason or (
            "passing-path timing only; failure-split counterexample parity "
            "has not been established"
        )
    else:
        reason = reason or "no aggregate group contains more than one safety job"
    return StrategyEvidence(
        "aggregate_passing_path",
        _round_seconds(time.monotonic() - started),
        invocations,
        ordered,
        exact,
        False,
        reason,
    )


def _run_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
    stem: str,
) -> ProcessEvidence:
    stdout_path = cwd / f"{stem}.stdout.log"
    stderr_path = cwd / f"{stem}.stderr.log"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        return ProcessEvidence(
            command,
            _round_seconds(time.monotonic() - started),
            None,
            "timeout",
            str(stdout_path),
            str(stderr_path),
        )
    stdout_path.write_text(completed.stdout)
    stderr_path.write_text(completed.stderr)
    status = "passed" if completed.returncode == 0 else "failed"
    return ProcessEvidence(
        command,
        _round_seconds(time.monotonic() - started),
        completed.returncode,
        status,
        str(stdout_path),
        str(stderr_path),
    )


def benchmark_shared_yosys(
    bundle: LoadedVerificationBundle,
    oracle: tuple[tuple[str, str], ...],
    *,
    config: VerificationRunConfig,
    work_root: Path,
) -> StrategyEvidence:
    """Measure one shared Yosys front-end with unchanged per-goal SMT checks."""

    if config.mode is not ProofMode.BMC:
        return StrategyEvidence(
            "shared_yosys_frontend",
            0.0,
            0,
            (),
            False,
            False,
            "shared-Yosys benchmark currently measures BMC only",
        )
    groups = aggregate_groups(bundle)
    if any(item.job.source_files[2:] for group in groups for item in group):
        # Companion publication is supported by the ordinary executor.  This
        # experiment deliberately remains smaller than a second bundle runner.
        return StrategyEvidence(
            "shared_yosys_frontend",
            0.0,
            0,
            (),
            False,
            False,
            "shared-Yosys benchmark does not stage companion files",
        )
    work_root.mkdir(parents=True, exist_ok=True)
    statuses = {
        item.property_id: FormalStatus.SKIPPED.value
        for item in bundle.manifest.jobs
        if not item.executable
    }
    evidence: list[ProcessEvidence] = []
    started = time.monotonic()
    cover_statuses, invocations = _run_cover_subset(
        bundle,
        config=config,
        work_root=work_root / "covers",
    )
    statuses.update(cover_statuses)
    for group_ordinal, group in enumerate(groups):
        group_root = work_root / f"group-{group_ordinal:04d}"
        group_root.mkdir(parents=True, exist_ok=True)
        implementation = bundle.read_bytes(group[0].implementation_path).decode(
            "utf-8"
        )
        implementation_path = group_root / "implementation.sv"
        implementation_path.write_text(implementation)
        checker_paths: list[Path] = []
        for ordinal, checker in enumerate(group):
            path = group_root / f"checker-{ordinal:04d}.sv"
            path.write_text(checker.checker_text)
            checker_paths.append(path)
        implementation_module = re.search(
            r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s+dut\s*\(",
            group[0].checker_text,
        )
        if implementation_module is None:
            return StrategyEvidence(
                "shared_yosys_frontend",
                _round_seconds(time.monotonic() - started),
                invocations,
                tuple(sorted(statuses.items())),
                False,
                False,
                "cannot identify the compiler-generated DUT instance",
                tuple(evidence),
            )
        script = [
            f"read_verilog -sv -formal {implementation_path.name}",
            "proc; opt_clean",
            "design -save zlang_implementation",
        ]
        for ordinal, (checker, path) in enumerate(zip(group, checker_paths, strict=True)):
            script.extend((
                "design -reset",
                "design -load zlang_implementation",
                f"read_verilog -sv -formal {path.name}",
                f"prep -top {checker.job.top}",
                "scc -select; simplemap; select -clear",
                "memory_nordff",
                "async2sync",
                "chformal -assume -early",
                "opt_clean",
                "formalff -setundef -clk2ff -ff2anyinit -hierarchy",
                "chformal -live -fair -cover -remove",
                "opt_clean",
                "check",
                "setundef -undriven -anyseq",
                "opt -fast",
                "rename -witness",
                "opt_clean",
                "hierarchy -smtcheck",
                "delete */t:$print",
                "formalff -assume",
                "dffunmap",
                f"write_smt2 -wires goal-{ordinal:04d}.smt2",
            ))
        script_path = group_root / "shared-yosys.ys"
        script_path.write_text("\n".join(script) + "\n")
        process = _run_process(
            ("yosys", "-ql", "shared-yosys.log", script_path.name),
            cwd=group_root,
            timeout_seconds=config.timeout_seconds,
            stem="shared-yosys",
        )
        evidence.append(process)
        invocations += 1
        if process.status != "passed":
            return StrategyEvidence(
                "shared_yosys_frontend",
                _round_seconds(time.monotonic() - started),
                invocations,
                tuple(sorted(statuses.items())),
                False,
                False,
                "shared Yosys preparation failed or timed out",
                tuple(evidence),
            )
        for ordinal, checker in enumerate(group):
            process = _run_process(
                (
                    "yosys-smtbmc",
                    "-s",
                    config.solver,
                    "-t",
                    str(config.depth),
                    "-m",
                    checker.job.top,
                    f"goal-{ordinal:04d}.smt2",
                ),
                cwd=group_root,
                timeout_seconds=config.timeout_seconds,
                stem=f"goal-{ordinal:04d}",
            )
            evidence.append(process)
            invocations += 1
            if process.status == "timeout":
                statuses[checker.job.property_id] = FormalStatus.UNKNOWN.value
            elif process.returncode == 0:
                statuses[checker.job.property_id] = FormalStatus.BOUNDED_PASS.value
            elif process.returncode == 1:
                statuses[checker.job.property_id] = FormalStatus.FAILED.value
            else:
                statuses[checker.job.property_id] = FormalStatus.UNKNOWN.value
    ordered = tuple(
        (job.property_id, statuses[job.property_id])
        for job in bundle.manifest.jobs
    )
    exact = ordered == oracle
    return StrategyEvidence(
        "shared_yosys_frontend",
        _round_seconds(time.monotonic() - started),
        invocations,
        ordered,
        exact,
        False,
        (
            "advisory subprocess classification has no SBY status artifact or "
            "counterexample/source-attribution parity"
        ),
        tuple(evidence),
    )


def benchmark(
    bundle_path: Path,
    *,
    output: Path,
    depth: int,
    timeout_seconds: int,
) -> BenchmarkEvidence:
    bundle = load_verification_bundle(bundle_path)
    output.mkdir(parents=True, exist_ok=True)
    config = VerificationRunConfig(
        mode=ProofMode.BMC,
        depth=depth,
        timeout_seconds=timeout_seconds,
        jobs=1,
    )
    started = time.monotonic()
    oracle_report = run_verification_bundle(
        bundle,
        config=config,
        work_directory=output / "per-goal-work",
    )
    oracle_elapsed = time.monotonic() - started
    oracle = _status_map(oracle_report)
    per_goal = StrategyEvidence(
        "per_goal_oracle",
        _round_seconds(oracle_elapsed),
        sum(item.executable for item in bundle.manifest.jobs),
        oracle,
        True,
        True,
    )
    aggregate = benchmark_aggregate(
        bundle,
        oracle,
        config=config,
        work_root=output / "aggregate-work",
    )
    shared = benchmark_shared_yosys(
        bundle,
        oracle,
        config=config,
        work_root=output / "shared-yosys-work",
    )
    return BenchmarkEvidence(
        bundle.manifest.bundle_identity or bundle.manifest.computed_identity,
        _manifest_hash(bundle),
        bundle.manifest.top,
        depth,
        timeout_seconds,
        len(bundle.manifest.jobs),
        sum(item.executable and item.kind == "safety" for item in bundle.manifest.jobs),
        sum(item.executable and item.kind == "cover" for item in bundle.manifest.jobs),
        sum(not item.executable for item in bundle.manifest.jobs),
        (per_goal, aggregate, shared),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark one immutable ZLang verification bundle"
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120)
    arguments = parser.parse_args()
    if arguments.depth < 1 or arguments.timeout < 1:
        parser.error("--depth and --timeout must be positive")
    if shutil.which("yosys") is None or shutil.which("yosys-smtbmc") is None:
        parser.error("Yosys and yosys-smtbmc are required")
    evidence = benchmark(
        arguments.bundle,
        output=arguments.output,
        depth=arguments.depth,
        timeout_seconds=arguments.timeout,
    )
    payload = asdict(evidence)
    destination = arguments.output / "formal-execution-scalability.json"
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
