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
import json
import resource
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from functools import wraps
from importlib import import_module
from pathlib import Path
from time import perf_counter_ns
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    # This script is also invoked directly by CI and operators.  Prefer the
    # checkout under inspection over an unrelated installed zlang package.
    sys.path.insert(0, str(REPOSITORY_ROOT))


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


def _expression_nodes(value: Any) -> tuple[int, int]:
    """Return logical and unique nodes for one executable expression root."""

    try:
        from zlang.backend.expression_materialization import walk_expression
        from zlang.ir.expressions import Expression
    except ImportError:
        return (0, 0)
    if not isinstance(value, Expression):
        return (0, 0)
    nodes = tuple(walk_expression(value))
    return (len(nodes), len({id(item) for item in nodes}))


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
        elif attribute == "explore_intent_structures":
            statistics = value.stats
            for field in (
                "saturation_iterations",
                "eclasses",
                "enodes",
                "raw_extractions",
                "structurally_unique_extractions",
                "retained_structures",
            ):
                key = f"intent_egraph_{field}"
                timeline.counters[key] = (
                    timeline.counters.get(key, 0) + getattr(statistics, field)
                )
            timeline.counters["intent_egraph_sites"] = (
                timeline.counters.get("intent_egraph_sites", 0) + 1
            )
            timeline.counters["intent_egraph_limited_sites"] = (
                timeline.counters.get("intent_egraph_limited_sites", 0)
                + int(statistics.truncated)
            )
        elif attribute in {"_materialization_plan", "_build_materialization_plan"}:
            timeline.counters["sv_materialization_plan_builds"] = (
                timeline.counters.get("sv_materialization_plan_builds", 0) + 1
            )
            timeline.counters["sv_materialized_nodes_total"] = (
                timeline.counters.get("sv_materialized_nodes_total", 0) + len(value)
            )
            timeline.counters["sv_materialized_nodes_max"] = max(
                timeline.counters.get("sv_materialized_nodes_max", 0), len(value)
            )
        elif attribute == "build_direct_sv_dag_plan":
            from zlang.backend.expression_materialization import expression_children

            timeline.counters["sv_dag_plan_builds"] = (
                timeline.counters.get("sv_dag_plan_builds", 0) + 1
            )
            timeline.counters["sv_dag_nodes_total"] = (
                timeline.counters.get("sv_dag_nodes_total", 0) + len(value.nodes)
            )
            timeline.counters["sv_dag_nodes_max"] = max(
                timeline.counters.get("sv_dag_nodes_max", 0), len(value.nodes)
            )
            timeline.counters["sv_dag_edges_total"] = (
                timeline.counters.get("sv_dag_edges_total", 0)
                + sum(len(expression_children(item.expression)) for item in value.nodes)
            )
        return value

    setattr(owner, attribute, measured)


def _wrap_expression_counter(
    timeline: _Timeline,
    owner: Any,
    attribute: str,
    *,
    prefix: str,
    expression_argument: int = 0,
    timed_name: str | None = None,
) -> Any:
    """Count recursive expression work while timing only outermost calls."""

    original = getattr(owner, attribute)
    depth = 0
    unique_roots: set[int] = set()

    @wraps(original)
    def measured(*args: Any, **kwargs: Any) -> Any:
        nonlocal depth
        depth += 1
        is_outermost = depth == 1
        try:
            timeline.counters[f"{prefix}_calls"] = (
                timeline.counters.get(f"{prefix}_calls", 0) + 1
            )
            if len(args) > expression_argument:
                expression = args[expression_argument]
                if is_outermost:
                    nodes, unique_nodes = _expression_nodes(expression)
                    timeline.counters[f"{prefix}_logical_nodes"] = (
                        timeline.counters.get(f"{prefix}_logical_nodes", 0) + nodes
                    )
                    timeline.counters[f"{prefix}_unique_nodes"] = (
                        timeline.counters.get(f"{prefix}_unique_nodes", 0) + unique_nodes
                    )
                    if unique_nodes:
                        unique_roots.add(id(expression))
                        timeline.counters[f"{prefix}_unique_roots"] = len(unique_roots)
            if is_outermost and timed_name is not None:
                with timeline.span(timed_name):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)
        finally:
            depth -= 1

    setattr(owner, attribute, measured)
    return measured


def _wrap_dependency_ordering(timeline: _Timeline, owner: Any) -> None:
    """Measure the exact selected graphs presented to dependency ordering."""

    original = owner.dependency_ordered_materialization

    @wraps(original)
    def measured(*args: Any, **kwargs: Any) -> Any:
        with timeline.span("dependency_ordered_materialization"):
            value = original(*args, **kwargs)
        timeline.counters["materialization_dependency_order_calls"] = (
            timeline.counters.get("materialization_dependency_order_calls", 0) + 1
        )
        roots = tuple(item.expression for item in value)
        logical_nodes = 0
        unique_nodes: set[int] = set()
        for root in roots:
            nodes, _ = _expression_nodes(root)
            logical_nodes += nodes
            from zlang.backend.expression_materialization import walk_expression

            unique_nodes.update(id(item) for item in walk_expression(root))
        timeline.counters["materialization_dependency_roots"] = (
            timeline.counters.get("materialization_dependency_roots", 0) + len(roots)
        )
        timeline.counters["materialization_dependency_logical_nodes"] = (
            timeline.counters.get("materialization_dependency_logical_nodes", 0)
            + logical_nodes
        )
        timeline.counters["materialization_dependency_unique_nodes"] = (
            timeline.counters.get("materialization_dependency_unique_nodes", 0)
            + len(unique_nodes)
        )
        return value

    owner.dependency_ordered_materialization = measured


def _instrument(timeline: _Timeline) -> None:
    # Some package facades intentionally re-export names that also name a
    # submodule. Resolve owners explicitly so profiling never instruments a
    # facade function by accident.
    backend_naming = import_module("zlang.backend.naming")
    constant_folding = import_module("zlang.backend.expression_constant_folding")
    compilation_selection = import_module("zlang.compilation_selection")
    costs = import_module("zlang.costs")
    emitter = import_module("zlang.backend.systemverilog.emitter")
    sv_composed = import_module("zlang.backend.systemverilog.composed")
    sv_materialized = import_module("zlang.backend.systemverilog.materialized")
    implementation_plans = import_module("zlang.implementation_plans")
    implementation_policy = import_module("zlang.implementation_policy")
    exploration = import_module("zlang.exploration")
    structural_exploration = import_module("zlang.intent_structural_exploration")
    serialization = import_module("zlang.common.serialization")
    session = import_module("zlang.compilation_session")
    signed_reductions = import_module("zlang.ir.signed_reductions")
    normalization = import_module("zlang.ir.normalization")
    opt_identity = import_module("zlang.opt.identity")
    opt_render = import_module("zlang.opt.render")
    scheduling = import_module("zlang.pipeline_scheduling")
    simulation_plan_build = import_module("zlang.simulation_plan_build")
    simulation_plan_codec = import_module("zlang.simulation_plan_codec")
    workspace = import_module("zlang.workspace")
    module_resolver = import_module("zlang.module_resolver")
    parser_module = import_module("zlang.parser.parser")
    materialization = import_module("zlang.backend.expression_materialization")

    for name in ("formal", "documents", "reports", "target_instance"):
        timeline.counters[f"{name}_product_built"] = 0

    original_cache_lookup = (
        structural_exploration.IntentStructuralExplorationCache.get_or_compute
    )

    @wraps(original_cache_lookup)
    def cache_lookup(self: Any, key: Any, compute: Any) -> Any:
        before = self.info()
        value = original_cache_lookup(self, key, compute)
        after = self.info()
        for name in ("hits", "misses", "waits", "evictions"):
            counter = f"intent_egraph_cache_{name}"
            timeline.counters[counter] = (
                timeline.counters.get(counter, 0)
                + getattr(after, name)
                - getattr(before, name)
            )
        timeline.counters["intent_egraph_cache_entries"] = after.entries
        timeline.counters["intent_egraph_cache_enodes"] = after.total_enodes
        return value

    structural_exploration.IntentStructuralExplorationCache.get_or_compute = (
        cache_lookup
    )

    # Demand products record the real call graph, including eager products
    # which native simulation never requests. Individual inner passes expose
    # repeated canonicalization and analysis rather than hiding it in one sum.
    original_demand = session.CompilationSession._demand

    @wraps(original_demand)
    def demand(self: Any, key: Any, builder: Any) -> Any:
        was_computed = key.name in self.computed_products
        with timeline.span(f"product.{key.name}"):
            value = original_demand(self, key, builder)
        if not was_computed and key.name in {
            "formal", "documents", "reports", "target_instance"
        }:
            timeline.counters[f"{key.name}_product_built"] += 1
        return value

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
        "_build_target_instance",
        "_build_materialized",
    ):
        _wrap(timeline, session.CompilationSession, name, name.removeprefix("_build_"))
    for owner, names in (
        (session, ("analyze", "canonical_ir_identity")),
        (
            compilation_selection,
            ("inline_locals", "lower", "restore", "canonical_round_trip_matches"),
        ),
        (
            implementation_policy,
            ("normalize_implementation_policy", "apply_external_region_exploration"),
        ),
        (costs, ("extract_estimated_costs",)),
        (implementation_plans, ("plan_backend_implementations",)),
        (exploration, ("explore_intent_structures",)),
        (structural_exploration, ("saturate",)),
        (opt_render, ("render_identity",)),
        (scheduling, ("schedule_module_fixed_pipelines",)),
        (normalization, ("normalize_selected_values",)),
        (
            simulation_plan_build,
            ("_build_leaf_simulation_plan", "identity_bytes"),
        ),
        (simulation_plan_codec, ("_validate_plan_payload",)),
        (
            emitter,
            (
                "emit",
                "emit_artifact",
                "normalize_selected_values",
            ),
        ),
        (
            sv_materialized,
            (
                "_embedded_staging_emission",
                "_materialized_emission",
            ),
        ),
        (sv_composed, ("_emit_composed_component",)),
        (
            materialization,
            (
                "build_direct_sv_dag_plan",
                "plan_materialization",
                "dependency_ordered_materialization",
            ),
        ),
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
    for name in ("_build_roots", "_build_materialization_plan"):
        _wrap(
            timeline,
            sv_materialized.ModuleMaterializationOwner,
            name,
            name,
        )
    _wrap_expression_counter(
        timeline,
        sv_materialized.ModuleMaterializationOwner,
        "physicalize",
        prefix="instance_expression",
        expression_argument=1,
        timed_name="instance_expression",
    )
    _wrap_expression_counter(
        timeline,
        materialization,
        "replace_materialized",
        prefix="replace_materialized",
        timed_name="replace_materialized",
    )
    _wrap_expression_counter(
        timeline,
        constant_folding.BackendConstantFolder,
        "fold",
        prefix="constant_fold",
        expression_argument=1,
        timed_name="constant_fold",
    )
    _wrap_dependency_ordering(timeline, materialization)
    for owner, attribute, name in (
        (signed_reductions, "expression_merkle_identity", "identity.expression_merkle"),
        (signed_reductions, "expression_semantic_identity", "identity.expression_semantic"),
        (opt_identity, "canonical_ir_identity", "identity.canonical"),
        (opt_render, "render_identity", "identity.render"),
        (serialization, "stable_digest", "identity.stable_digest"),
        (backend_naming, "module_rtl_names", "naming.module_rtl_names"),
    ):
        _wrap(timeline, owner, attribute, name)
    # Local imports in product builders resolve module attributes at call time.
    _wrap(
        timeline,
        simulation_plan_build,
        "build_simulation_plan",
        "build_simulation_plan",
    )
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
    import zlang.backend.systemverilog as systemverilog
    import zlang.backend.systemverilog.emitter as emitter
    import zlang.cli as cli
    import zlang.cli_command as cli_command
    import zlang.compiler as compiler

    _wrap(
        timeline,
        compiler,
        "create_file_compilation_session_snapshot",
        "create_file_compilation_session_snapshot",
    )
    _wrap(
        timeline,
        cli_command,
        "_compile_artifact_snapshot",
        "compile_artifact_snapshot",
    )
    _wrap(
        timeline,
        systemverilog,
        "emit_artifact",
        "emit_systemverilog_artifact",
    )
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
