#!/usr/bin/env python3
# ruff: noqa: E402
"""Measure ZLang structural witnesses without changing compiler policy.

The normal regression gate uses ``--profile small``. Larger profiles are
explicit opt-in probes of the same parameterized source algorithms.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
import hashlib
import json
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.structural.catalog import WITNESSES, WITNESS_BY_SLUG
SCHEMA = "zlang-structural-synthesis-baseline-v2"
YOSYS_STAGES = (
    ("read_verilog", "read_verilog -sv {rtl}; stat"),
    (
        "hierarchy",
        "read_verilog -sv {rtl}; hierarchy -check -top {top}; stat",
    ),
    (
        "proc",
        "read_verilog -sv {rtl}; hierarchy -check -top {top}; proc; stat",
    ),
    (
        "opt_expr",
        "read_verilog -sv {rtl}; hierarchy -check -top {top}; "
        "proc; opt_expr; stat",
    ),
    (
        "opt_clean_check",
        "read_verilog -sv {rtl}; hierarchy -check -top {top}; "
        "proc; opt_expr; opt_clean; check; stat",
    ),
)


def _count_dataclass_nodes(value: object) -> int:
    seen: set[int] = set()

    def visit(item: object) -> int:
        if item is None or isinstance(item, (str, int, float, bool, bytes)):
            return 0
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        if isinstance(item, (tuple, list, set, frozenset)):
            return sum(visit(child) for child in item)
        if isinstance(item, dict):
            return sum(visit(key) + visit(child) for key, child in item.items())
        if is_dataclass(item) and not isinstance(item, type):
            return 1 + sum(visit(getattr(item, field.name)) for field in fields(item))
        return 0

    return visit(value)


def _expression_graph_metrics(value: object) -> tuple[int, tuple[object, ...]]:
    """Count expanded logical paths without materializing them.

    The v1 runner appended every occurrence to a Python list.  That made the
    benchmark itself consume hundreds of MiB for compact DAGs such as CAM and
    PacketCompactor.  Keep the historical logical occurrence metric, but
    compute subtree multiplicities once per expression object.
    """

    from typing import get_args

    from zlang.ir import expressions as expression_ir
    from zlang.ir.traversal import expression_children

    expression_types = tuple(get_args(expression_ir.Expression))
    roots: list[object] = []
    visited_objects: set[int] = set()

    def visit(item: object) -> None:
        if item is None or isinstance(item, (str, int, float, bool, bytes)):
            return
        if isinstance(item, expression_types):
            roots.append(item)
            return
        identity = id(item)
        if identity in visited_objects:
            return
        visited_objects.add(identity)
        if isinstance(item, (tuple, list, set, frozenset)):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key)
                visit(child)
        elif is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                visit(getattr(item, field.name))

    visit(value)

    unique: list[object] = []
    seen_expressions: set[int] = set()
    subtree_occurrences: dict[int, int] = {}

    def expanded_size(expression: object) -> int:
        identity = id(expression)
        cached = subtree_occurrences.get(identity)
        if cached is not None:
            return cached
        if identity not in seen_expressions:
            seen_expressions.add(identity)
            unique.append(expression)
        result = 1 + sum(
            expanded_size(child) for child in expression_children(expression)
        )
        subtree_occurrences[identity] = result
        return result

    return sum(expanded_size(root) for root in roots), tuple(unique)


def _memory_snapshot() -> dict[str, float]:
    """Return current and cumulative resident memory for this worker."""

    current_kib = 0
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        match = re.search(r"(?m)^VmRSS:\s+(\d+)\s+kB$", status)
        if match is not None:
            current_kib = int(match.group(1))
    except OSError:
        pass
    return {
        "rss_mib": round(current_kib / 1024, 3),
        "peak_rss_mib": round(peak_kib / 1024, 3),
    }


def _timed_stage(name: str, action, stages: list[dict[str, object]]):
    started = time.perf_counter()
    result = action()
    stages.append(
        {
            "stage": name,
            "wall_seconds": round(time.perf_counter() - started, 6),
            **_memory_snapshot(),
        }
    )
    return result


def _parse_yosys_stats(output: str) -> dict[str, int | float | None]:
    def last_integer(*patterns: str) -> int | None:
        for pattern in patterns:
            matches = re.findall(pattern, output)
            if matches:
                return int(matches[-1])
        return None

    memory = re.findall(r"MEM:\s+([0-9.]+)\s+MB peak", output)

    wires = last_integer(r"Number of wires:\s+(\d+)", r"(?m)^\s*(\d+) wires$")
    cells = last_integer(r"Number of cells:\s+(\d+)", r"(?m)^\s*(\d+) cells$")
    return {
        "wires": wires,
        "wire_bits": last_integer(
            r"Number of wire bits:\s+(\d+)", r"(?m)^\s*(\d+) wire bits$"
        ),
        "cells": 0 if cells is None and wires is not None else cells,
        "peak_rss_mib": float(memory[-1]) if memory else None,
    }


def _run_yosys(
    rtl: Path,
    top: str,
    *,
    all_stages: bool,
) -> tuple[list[dict[str, object]], str | None]:
    executable = shutil.which("yosys")
    if executable is None:
        return [], "yosys unavailable"
    selected = YOSYS_STAGES if all_stages else (YOSYS_STAGES[-1],)
    results: list[dict[str, object]] = []
    for name, recipe in selected:
        command = recipe.format(rtl=rtl, top=top)
        started = time.perf_counter()
        completed = subprocess.run(
            (executable, "-p", command),
            capture_output=True,
            text=True,
            timeout=180,
        )
        elapsed = time.perf_counter() - started
        combined = completed.stdout + completed.stderr
        result: dict[str, object] = {
            "stage": name,
            "wall_seconds": round(elapsed, 6),
            "returncode": completed.returncode,
            **_parse_yosys_stats(combined),
        }
        results.append(result)
        if completed.returncode:
            return results, combined[-4000:]
    return results, None


def _measure_one(slug: str, profile: str, *, rtl_output: Path) -> dict[str, object]:
    from zlang.backend.systemverilog import emit_experimental
    from zlang.compilation_session import CompilationSession
    from zlang.ir.expressions import FunctionalRegion
    from zlang.ir.functional_regions import FunctionalSpecializationCertificate
    from zlang.ir.normalization import normalize_selected_values
    from zlang.ir.signed_reductions import expression_semantic_identity

    witness = WITNESS_BY_SLUG[slug]
    source = witness.source_text(profile)
    started = time.perf_counter()
    stages: list[dict[str, object]] = []
    session = CompilationSession(source, top=witness.top)
    _timed_stage("parse", lambda: session.syntax, stages)
    semantic_module = _timed_stage("semantic", lambda: session.semantic_ir, stages)
    _timed_stage("canonical_selection", lambda: session.optimization_ir, stages)
    _timed_stage("implementation_planning", lambda: session.planning, stages)
    compilation = _timed_stage("materialize", session.materialize, stages)
    metrics_started = time.perf_counter()
    semantic_occurrences, expressions = _expression_graph_metrics(semantic_module)
    stages.append(
        {
            "stage": "semantic_metrics",
            "wall_seconds": round(time.perf_counter() - metrics_started, 6),
            **_memory_snapshot(),
        }
    )
    normalized = _timed_stage(
        "normalization", lambda: normalize_selected_values(compilation.ir), stages
    )
    normalized_metrics_started = time.perf_counter()
    normalized_occurrences, normalized_expressions = _expression_graph_metrics(
        normalized
    )
    stages.append(
        {
            "stage": "normalized_metrics",
            "wall_seconds": round(
                time.perf_counter() - normalized_metrics_started, 6
            ),
            **_memory_snapshot(),
        }
    )
    unique_normalized = {
        expression_semantic_identity(expression)
        for expression in normalized_expressions
    }
    regions = [item for item in expressions if isinstance(item, FunctionalRegion)]
    certificates = [
        certificate
        for region in regions
        for certificate in region.certificates
        if isinstance(certificate, FunctionalSpecializationCertificate)
    ]
    specializations = compilation.ir.generic_specializations
    specialization_identities = tuple(item.identity for item in specializations)
    common: dict[str, object] = {
        "schema": SCHEMA,
        "witness": slug,
        "profile": profile,
        "top": witness.top,
        "source": witness.source.relative_to(witness.source.parents[2]).as_posix(),
        "purpose": witness.purpose,
        "source_lines": len(source.splitlines()),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "ast_nodes": _count_dataclass_nodes(compilation.ast),
        "semantic_expression_occurrences": semantic_occurrences,
        "semantic_unique_objects": len(expressions),
        "semantic_expression_arena": (
            None
            if semantic_module.semantic_expression_arena_statistics is None
            else {
                "requests": semantic_module.semantic_expression_arena_statistics.requests,
                "hits": semantic_module.semantic_expression_arena_statistics.hits,
                "unique_nodes": semantic_module.semantic_expression_arena_statistics.unique_nodes,
                "provenance_occurrences": semantic_module.semantic_expression_arena_statistics.provenance_occurrences,
            }
        ),
        "selected_value_normalization": (
            None
            if normalized.selected_value_normalization_statistics is None
            else {
                "expression_requests": (
                    normalized.selected_value_normalization_statistics.expression_requests
                ),
                "expression_cache_hits": (
                    normalized.selected_value_normalization_statistics.expression_cache_hits
                ),
                "unique_expression_visits": (
                    normalized.selected_value_normalization_statistics.unique_expression_visits
                ),
                "unique_object_visits": (
                    normalized.selected_value_normalization_statistics.unique_object_visits
                ),
            }
        ),
        "generic_specializations": len(specializations),
        "duplicate_specializations": len(specializations)
        - len(set(specialization_identities)),
        "functional_regions": len(regions),
        "virtual_elements": sum(region.type.length for region in regions),
        "functional_certificates": len(certificates),
        "certificate_virtual_instances": sum(
            certificate.virtual_instances for certificate in certificates
        ),
        "canonical_nodes": len(compilation.high_level_ir.expressions),
        "selected_canonical_nodes": len(compilation.optimization_ir.expressions),
        "normalized_expression_occurrences": normalized_occurrences,
        "normalized_unique_expressions": len(unique_normalized),
        "egraph_observation": witness.egraph_observation,
    }
    try:
        rtl = _timed_stage(
            "rtl_emission", lambda: emit_experimental(compilation.ir), stages
        )
    except Exception as error:  # baseline must retain the first failing phase
        return {
            **common,
            "status": "failed",
            "failure_phase": "rtl_emission",
            "failure_type": type(error).__name__,
            "failure_message": str(error),
            "zlang_wall_seconds": round(time.perf_counter() - started, 6),
            "zlang_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "zlang_stages": stages,
            "rtl_bytes": None,
            "max_rtl_line": None,
            "generated_modules": None,
            "yosys": [],
            "yosys_error": None,
        }
    zlang_seconds = time.perf_counter() - started
    module_count = len(re.findall(r"(?m)^module\s+", rtl))
    rtl_output.write_text(rtl, encoding="utf-8")
    lines = rtl.splitlines()
    return {
        **common,
        "status": "ok",
        "failure_phase": None,
        "rtl_bytes": len(rtl.encode()),
        "max_rtl_line": max(map(len, lines), default=0),
        "generated_modules": module_count,
        "zlang_wall_seconds": round(zlang_seconds, 6),
        "zlang_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "zlang_stages": stages,
        "yosys": [],
        "yosys_error": None,
    }


def _markdown(records: list[dict[str, object]]) -> str:
    lines = [
        "# Structural and synthesis baseline",
        "",
        f"Schema: `{SCHEMA}`.",
        "",
        "Measurements are observations, not pass/fail QoR promises. The normal",
        "test gate owns semantic correctness, deterministic emission and lint.",
        "",
        "| Witness | ZLang s | RSS MiB | Semantic nodes | Canonical nodes | "
        "RTL bytes | Max line | Yosys s | Yosys RSS MiB | Yosys cells |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        yosys = record.get("yosys", [])
        final = yosys[-1] if isinstance(yosys, list) and yosys else {}
        failed = record.get("status") == "failed"
        lines.append(
            "| {witness} | {zlang_wall_seconds:.3f} | {rss:.1f} | "
            "{semantic_expression_occurrences} | {selected_canonical_nodes} | "
            "{rtl_bytes} | {max_rtl_line} | {ys} | {yrss} | {cells} |".format(
                **{
                    **record,
                    "rtl_bytes": "FAIL" if failed else record.get("rtl_bytes", "—"),
                    "max_rtl_line": "—" if failed else record.get("max_rtl_line", "—"),
                },
                rss=int(record["zlang_peak_rss_kib"]) / 1024,
                ys=(
                    f"{float(final['wall_seconds']):.3f}"
                    if final.get("wall_seconds") is not None
                    else "—"
                ),
                yrss=final.get("peak_rss_mib", "—"),
                cells=final.get("cells", "—"),
            )
        )
    lines.extend(("", "Use `--json` to retain the corresponding raw measurements."))
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("small", "medium", "large", "stress"), default="small")
    parser.add_argument("--witness", action="append", choices=tuple(WITNESS_BY_SLUG))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--yosys-stages", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rtl-output", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    selected = arguments.witness or [item.slug for item in WITNESSES]
    if arguments.worker:
        if len(selected) != 1:
            raise SystemExit("worker requires exactly one --witness")
        if arguments.rtl_output is None:
            raise SystemExit("worker requires --rtl-output")
        print(
            json.dumps(
                _measure_one(
                    selected[0],
                    arguments.profile,
                    rtl_output=arguments.rtl_output,
                ),
                sort_keys=True,
            )
        )
        return 0

    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="zlang-structural-baseline-") as directory:
        temporary_root = Path(directory)
        for slug in selected:
            rtl_path = temporary_root / f"{slug}.sv"
            command = (
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--profile",
                arguments.profile,
                "--witness",
                slug,
                "--rtl-output",
                str(rtl_path),
            )
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if completed.returncode:
                records.append({
                    "schema": SCHEMA,
                    "witness": slug,
                    "profile": arguments.profile,
                    "error": (completed.stdout + completed.stderr)[-8000:],
                })
                continue
            record = json.loads(completed.stdout.splitlines()[-1])
            if record.get("status") == "ok":
                yosys, yosys_error = _run_yosys(
                    rtl_path,
                    str(record["top"]),
                    all_stages=arguments.yosys_stages,
                )
                record["yosys"] = yosys
                record["yosys_error"] = yosys_error
                if yosys_error is not None:
                    record["status"] = "failed"
                    record["failure_phase"] = "yosys"
            records.append(record)

    payload = {"schema": SCHEMA, "profile": arguments.profile, "records": records}
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if arguments.json:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if arguments.markdown:
        arguments.markdown.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown.write_text(_markdown(records), encoding="utf-8")
    return int(any("error" in record for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
