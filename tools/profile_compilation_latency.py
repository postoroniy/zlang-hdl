# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Opt-in, process-local compilation profiler; never changes compiler policy.

Examples:
    .venv/bin/python tools/profile_compilation_latency.py SOURCE --top Top --mode native
    .venv/bin/python tools/profile_compilation_latency.py SOURCE --top Top --mode sv

Set ZLANG_COMPILE_PROFILE=1 for the additional Rust/Cranelift JSON line on
stderr when using a locally built, instrumented native extension. Each run
uses a fresh process so neither Python's compiled-program cache nor an already
initialized JIT disguises cold cost. The generated RTL is temporary.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from functools import wraps
import json
from pathlib import Path
import resource
import tempfile
from time import perf_counter_ns
from typing import Any


@dataclass
class _Frame:
    name: str
    start: int
    child_ns: int = 0


class _Timeline:
    def __init__(self) -> None:
        self.stack: list[_Frame] = []
        self.events: list[dict[str, int | str]] = []
        self.counters: dict[str, int] = {}

    @contextmanager
    def span(self, name: str):
        frame = _Frame(name, perf_counter_ns())
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = perf_counter_ns() - frame.start
            self.stack.pop()
            if self.stack:
                self.stack[-1].child_ns += elapsed
            self.events.append(
                {
                    "name": name,
                    "parent": self.stack[-1].name if self.stack else "",
                    "inclusive_ns": elapsed,
                    "exclusive_ns": elapsed - frame.child_ns,
                }
            )

    def aggregate(self) -> dict[str, dict[str, float | int]]:
        groups: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"calls": 0, "inclusive_ms": 0.0, "exclusive_ms": 0.0}
        )
        for event in self.events:
            item = groups[str(event["name"])]
            item["calls"] += 1
            item["inclusive_ms"] += int(event["inclusive_ns"]) / 1e6
            item["exclusive_ms"] += int(event["exclusive_ns"]) / 1e6
        return dict(sorted(groups.items()))


def _ast_nodes(value: Any) -> int:
    """Count unique root AST objects, outside the timed parser span."""

    seen: set[int] = set()

    def visit(item: Any) -> int:
        if is_dataclass(item):
            if id(item) in seen:
                return 0
            seen.add(id(item))
            return 1 + sum(visit(getattr(item, field.name)) for field in fields(item))
        if isinstance(item, (tuple, list)):
            return sum(map(visit, item))
        return 0

    return visit(value)


def _wrap(timeline: _Timeline, owner: Any, attribute: str, name: str) -> None:
    original = getattr(owner, attribute)

    @wraps(original)
    def measured(*args: Any, **kwargs: Any) -> Any:
        with timeline.span(name):
            value = original(*args, **kwargs)
        if attribute == "_build_syntax":
            timeline.counters["ast_nodes"] = _ast_nodes(value)
        elif attribute == "_analyze":
            statistics = value.module.semantic_expression_arena_statistics
            if statistics is not None:
                for field in (
                    "requests",
                    "hits",
                    "unique_nodes",
                    "provenance_occurrences",
                ):
                    timeline.counters[f"arena_{field}"] = getattr(statistics, field)
        elif attribute == "_build_selection":
            timeline.counters["canonical_nodes"] = len(
                value.optimization_ir.expressions
            )
            timeline.counters["canonical_edges"] = sum(
                len(node.operands) for node in value.optimization_ir.expressions
            )
            timeline.counters["high_level_nodes"] = len(value.high_level_ir.expressions)
            timeline.counters["functional_regions"] = sum(
                "functional_region" in str(node.op).lower()
                for node in value.optimization_ir.expressions
            )
        elif attribute == "_build_simulation_plan":
            timeline.counters["plan_nodes"] = len(value.payload["nodes"])
            timeline.counters["plan_edges"] = sum(
                len(node["operands"]) for node in value.payload["nodes"]
            )
            timeline.counters["plan_bytes"] = len(value.canonical_bytes)
            timeline.counters["clock_domains"] = len(value.payload["domains"])
        elif attribute == "_build_planning":
            module = value.module
            for field in (
                "assignments",
                "rules",
                "registers",
                "memories",
                "fifos",
                "children",
                "functions",
                "callable_definitions",
                "generic_specializations",
                "pipelines",
            ):
                timeline.counters[field] = len(getattr(module, field, ()))
        elif attribute == "_materialization_plan":
            timeline.counters["sv_materialized_nodes"] = len(value)
        elif attribute == "build_direct_sv_dag_plan":
            timeline.counters["sv_dag_nodes"] = len(value.nodes)
        return value

    setattr(owner, attribute, measured)


def _instrument(timeline: _Timeline) -> None:
    import zlang.backend.systemverilog.emitter as emitter
    import zlang.compilation_session as session
    import zlang.ir.normalization as normalization
    import zlang.pipeline_scheduling as scheduling
    import zlang.simulation_plan as simulation_plan
    import zlang.workspace as workspace
    import zlang.module_resolver as module_resolver
    import zlang.parser.parser as parser_module
    import zlang.backend.expression_materialization as materialization

    # Demand products record the real call graph, including eager products
    # which native simulation never requests. Individual inner passes expose
    # repeated canonicalization and analysis rather than hiding it in one sum.
    original_demand = session.CompilationSession._demand

    @wraps(original_demand)
    def demand(self: Any, key: Any, builder: Any) -> Any:
        with timeline.span(f"product.{key.name}"):
            return original_demand(self, key, builder)

    session.CompilationSession._demand = demand
    for name in (
        "_build_syntax",
        "_analyze",
        "_build_selection",
        "_build_planning",
        "_build_simulation_plan",
        "_build_formal",
        "_build_documents",
        "_build_reports",
        "_build_materialized",
    ):
        _wrap(timeline, session.CompilationSession, name, name.removeprefix("_build_"))
    for owner, names in (
        (
            session,
            (
                "analyze",
                "inline_locals",
                "normalize_implementation_policy",
                "apply_external_region_exploration",
                "extract_estimated_costs",
                "lower",
                "restore",
                "render_canonical_identity",
                "plan_backend_implementations",
                "_canonical_round_trip_matches",
            ),
        ),
        (scheduling, ("schedule_module_fixed_pipelines",)),
        (normalization, ("normalize_selected_values",)),
        (
            simulation_plan,
            (
                "_build_leaf_simulation_plan",
                "_identity_bytes",
                "_validate_plan_payload",
            ),
        ),
        (
            emitter,
            (
                "emit",
                "_materialization_plan",
                "_materialized_emission",
                "plan_materialization",
                "dependency_ordered_materialization",
                "normalize_selected_values",
                "reachable_module_callables",
            ),
        ),
        (materialization, ("build_direct_sv_dag_plan",)),
        (
            workspace,
            (
                "discover_project_manifest",
                "_validate_external_mappings",
                "_validate_graph",
                "_index_package",
                "_validate_module_graph",
            ),
        ),
    ):
        for name in names:
            _wrap(timeline, owner, name, name)
    # Local imports in product builders resolve module attributes at call time.
    _wrap(timeline, simulation_plan, "build_simulation_plan", "build_simulation_plan")
    _wrap(
        timeline,
        workspace.ProjectWorkspace,
        "compilation_closure_for",
        "compilation_closure_for",
    )
    original_load = workspace.load_indexed_module

    @wraps(original_load)
    def load_indexed(logical_path: str, **kwargs: Any) -> Any:
        with timeline.span(f"index.{logical_path}"):
            return original_load(logical_path, **kwargs)

    workspace.load_indexed_module = load_indexed
    _wrap(timeline, module_resolver, "parse", "index.parse")
    _wrap(timeline, parser_module, "_get_parser", "lark.initialize_or_reuse")


def _native(
    timeline: _Timeline,
    source: Path,
    project: Path | None,
    top: str,
    clock: str | None,
) -> None:
    import zlang.sim as simulation

    _wrap(
        timeline, simulation, "create_file_compilation_session", "create_file_session"
    )
    _wrap(timeline, simulation, "_native_runtime", "load_native_runtime")
    original_runtime = simulation._native_runtime

    class MeasuredRuntime:
        def __init__(self, runtime: Any) -> None:
            self.runtime = runtime

        def compile_plan_bytes(self, data: bytes) -> Any:
            with timeline.span("native.compile_plan_bytes"):
                return self.runtime.compile_plan_bytes(data)

    simulation._native_runtime = lambda: MeasuredRuntime(original_runtime())
    _wrap(timeline, simulation.SimulationPlan, "to_bytes", "plan.serialize")
    _wrap(timeline, simulation.Program, "create", "simulator.create")
    with timeline.span("native.prepare"):
        program = simulation.compile(source, top=top, project=project, engine="native")
        sim = program.create()
    with timeline.span("native.first_eval"):
        sim.eval()
    domains = program.plan.payload["domains"]
    if domains:
        selected_clock = clock or domains[0]["clock"]
        with timeline.span("native.first_edge"):
            sim.edge(selected_clock)
    sim.close()


def _sv(timeline: _Timeline, source: Path, project: Path | None, top: str) -> None:
    import zlang.cli as cli
    import zlang.backend.systemverilog.emitter as emitter

    _wrap(timeline, cli, "compile_file_snapshot", "compile_file_snapshot")
    _wrap(timeline, cli, "emit_systemverilog_artifact", "emit_systemverilog_artifact")
    _wrap(timeline, emitter, "emit_artifact", "emit_artifact")
    with tempfile.TemporaryDirectory(prefix="zlang-compile-profile-") as directory:
        output = Path(directory) / "profile.sv"
        args = [str(source), "--top", top, "--systemverilog", str(output)]
        if project is not None:
            args += ["--project", str(project)]
        with timeline.span("sv.cli_total"):
            status = cli.main(args)
        if status != 0:
            raise RuntimeError(f"zlang CLI returned {status}")
        timeline.counters["rtl_bytes"] = output.stat().st_size
        rtl = output.read_text()
        timeline.counters["rtl_max_line"] = max(map(len, rtl.splitlines()), default=0)
        timeline.counters["rtl_modules"] = rtl.count("endmodule")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--mode", choices=("native", "sv"), required=True)
    parser.add_argument(
        "--clock", help="edge to time; default is the first plan domain"
    )
    parser.add_argument(
        "--warm-parser",
        action="store_true",
        help="exclude Lark initialization as a controlled warm-process comparison",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="copy source outside project for control",
    )
    args = parser.parse_args()
    if args.isolated and args.project is not None:
        parser.error("temporary standalone source cannot use the original project lock")
    timeline = _Timeline()
    with tempfile.TemporaryDirectory(prefix="zlang-compile-profile-") as directory:
        source = args.source
        top = args.top
        if args.isolated:
            source = Path(directory) / "isolated_source.zhl"
            source.write_bytes(args.source.read_bytes())
        timeline.counters["source_lines"] = len(source.read_text().splitlines())
        if args.warm_parser:
            from zlang.parser.parser import _get_parser

            _get_parser()
        _instrument(timeline)
        if args.mode == "native":
            _native(timeline, source, args.project, top, args.clock)
        else:
            _sv(timeline, source, args.project, top)
    print(
        json.dumps(
            {
                "mode": args.mode,
                "source": str(args.source),
                "top": args.top,
                "counters": timeline.counters,
                "timings": timeline.aggregate(),
                "events": timeline.events,
                "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
