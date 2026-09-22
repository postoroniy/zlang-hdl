#!/usr/bin/env python3
# ruff: noqa: E402
"""Opt-in normalization e-graph A/B experiment for structural witnesses.

This tool never participates in normal compilation.  It selects scalar pure
sub-DAGs by IR properties, applies the existing bounded/certified saturation
engine, and compares the source DAG with the cheapest admitted alternative.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass, replace
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.structural.catalog import WITNESS_BY_SLUG
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.normalization import normalize_selected_values
from zlang.ir.packing import unpack_runtime
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    expression_children,
)
from zlang.ir.types import HardwareType, StructType, TupleType, VecType
from zlang.opt import lower, render_term, saturate, term_to_expression
from zlang.simulate import simulate


SCHEMA = "zlang-structural-normalization-egraph-experiment-v1"
DEFAULT_WITNESSES = ("crc_parallel", "prefix_network", "generic_explosion")
MAX_GRAPH_NODES = 16_384
MAX_ITERATIONS = 8
MAX_TERMS = 256
MAX_ROOTS = 8


def _children(value: expr.Expression) -> tuple[expr.Expression, ...]:
    return expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )


def _metrics(value: expr.Expression) -> tuple[int, int, int]:
    seen: dict[int, expr.Expression] = {}
    incoming: dict[int, int] = {}
    depth_memo: dict[int, int] = {}

    def visit(item: expr.Expression) -> None:
        incoming[id(item)] = incoming.get(id(item), 0) + 1
        if seen.get(id(item)) is item:
            return
        seen[id(item)] = item
        for child in _children(item):
            visit(child)

    def depth(item: expr.Expression) -> int:
        cached = depth_memo.get(id(item))
        if cached is not None:
            return cached
        result = 1 + max((depth(child) for child in _children(item)), default=0)
        depth_memo[id(item)] = result
        return result

    visit(value)
    repeated = sum(count - 1 for count in incoming.values() if count > 1)
    return len(seen), depth(value), repeated


def _scalar_templates(module: Module) -> tuple[expr.Expression, ...]:
    """Select candidate roots by shape, never by witness or source name."""

    found: list[expr.Expression] = []
    visited: dict[int, object] = {}

    def visit(value: object) -> None:
        previous = visited.get(id(value))
        if previous is value:
            return
        if isinstance(value, (tuple, list)) or (
            is_dataclass(value) and not isinstance(value, type)
        ):
            visited[id(value)] = value
        if isinstance(value, expr.FunctionalRegion):
            if not isinstance(value.template.type, (VecType, StructType, TupleType)):
                found.append(value.template)
            visit(value.template)
            for table in value.tables:
                visit(table.values)
            for _, captured in value.captures:
                visit(captured)
            return
        if isinstance(value, expr.Generate):
            for element in value.elements:
                if not isinstance(element.type, (VecType, StructType, TupleType)):
                    found.append(element)
                visit(element)
            return
        if isinstance(value, expr.Expression):
            if not isinstance(value.type, (VecType, StructType, TupleType)):
                found.append(value)
            for child in _children(value):
                visit(child)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    for assignment in module.assignments:
        visit(assignment.expression)
    unique: dict[str, expr.Expression] = {}
    for value in found:
        unique.setdefault(expression_semantic_identity(value), value)
    ranked = sorted(
        unique.values(),
        key=lambda value: (*_metrics(value)[:2], expression_semantic_identity(value)),
        reverse=True,
    )
    return tuple(ranked[:MAX_ROOTS])


def _standalone_expression(
    value: expr.Expression,
) -> tuple[expr.Expression, tuple[Port, ...]]:
    """Close region-local symbolic leaves as independent scalar inputs."""

    memo: dict[int, tuple[expr.Expression, expr.Expression]] = {}

    def rewrite(item: object) -> object:
        if isinstance(item, expr.FunctionalCaptureRef):
            identity = hashlib.sha256(
                repr((item.identity, item.type)).encode()
            ).hexdigest()
            return expr.InputRef(
                f"capture_{identity[:12]}", item.type, origin=item.origin
            )
        if isinstance(item, expr.FunctionalValue):
            identity = hashlib.sha256(
                repr((item.expression, item.type)).encode()
            ).hexdigest()
            return expr.InputRef(
                f"binder_{identity[:12]}",
                item.type,
                origin=item.origin,
            )
        if isinstance(item, expr.FunctionalTableLookup):
            raise ValueError("functional table lookup is not a scalar free input")
        if isinstance(item, expr.FunctionalRegion):
            raise ValueError("nested region is not a scalar e-graph root")
        if isinstance(item, expr.Expression):
            cached = memo.get(id(item))
            if cached is not None and cached[0] is item:
                return cached[1]
            updates = {
                descriptor.name: rewrite(getattr(item, descriptor.name))
                for descriptor in fields(item)
                if descriptor.init
                and descriptor.name not in {"type", "origin", "source_origin"}
            }
            result = replace(item, **updates) if updates else item
            memo[id(item)] = (item, result)
            return result
        if isinstance(item, tuple):
            return tuple(rewrite(child) for child in item)
        return item

    rewritten = rewrite(value)
    assert isinstance(rewritten, expr.Expression)
    inputs: dict[str, HardwareType] = {}
    stack = [rewritten]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, expr.InputRef):
            previous = inputs.setdefault(current.name, current.type)
            if previous != current.type:
                raise ValueError("standalone input has conflicting exact types")
        stack.extend(_children(current))
    ports = tuple(
        Port(PortDirection.INPUT, name, type_)
        for name, type_ in sorted(inputs.items())
    )
    return rewritten, ports


def _module_for(
    value: expr.Expression,
    ports: tuple[Port, ...],
    owner: Module,
) -> Module:
    output = Port(PortDirection.OUTPUT, "result", value.type)
    return Module(
        "NormalizationEGraphProbe",
        (*ports, output),
        (Assignment(output, value),),
        functions=owner.functions,
        callable_definitions=owner.callable_definitions,
        equivalences=owner.equivalences,
    )


def _yosys(rtl: str) -> dict[str, object] | None:
    executable = shutil.which("yosys")
    if executable is None:
        return None
    with tempfile.TemporaryDirectory(prefix="zlang-egraph-") as directory:
        path = Path(directory) / "probe.sv"
        path.write_text(rtl, encoding="utf-8")
        started = time.perf_counter()
        result = subprocess.run(
            (
                executable,
                "-q",
                "-p",
                f"read_verilog -sv {path}; hierarchy -top "
                "NormalizationEGraphProbe; proc; opt_expr; opt_clean; check; stat",
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return {
        "returncode": result.returncode,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "output_sha256": hashlib.sha256(
            (result.stdout + result.stderr).encode()
        ).hexdigest(),
    }


def _differential(
    source: Module,
    selected: Module,
    ports: tuple[Port, ...],
) -> bool:
    generator = random.Random(0x5A17)
    for _ in range(64):
        inputs = {
            port.name: unpack_runtime(
                port.type,
                generator.randrange(1 << port.type.width),
            )
            for port in ports
        }
        if simulate(source, **inputs) != simulate(selected, **inputs):
            return False
    return True


def _experiment(slug: str, profile: str) -> dict[str, object]:
    witness = WITNESS_BY_SLUG[slug]
    module = normalize_selected_values(
        compile_source(witness.source_text(profile), top=witness.top).ir
    )
    records: list[dict[str, object]] = []
    for root in _scalar_templates(module):
        source_metrics = _metrics(root)
        if source_metrics[0] > MAX_GRAPH_NODES:
            records.append(
                {
                    "source_identity": expression_semantic_identity(root),
                    "status": "bounded_fallback",
                    "reason": "input graph exceeds 16384 nodes",
                }
            )
            continue
        try:
            standalone, ports = _standalone_expression(root)
            source_module = _module_for(standalone, ports, module)
            canonical = lower(source_module)
            if len(canonical.expressions) > MAX_GRAPH_NODES:
                raise ValueError("canonical input graph exceeds 16384 nodes")
            result = saturate(
                canonical,
                canonical.assignments[0].expression,
                max_iterations=MAX_ITERATIONS,
                max_terms=MAX_TERMS,
            )
            alternatives = tuple(
                term_to_expression(term) for term in result.alternatives
            )
            candidates = (standalone, *alternatives)
            selected = min(
                candidates,
                key=lambda value: (
                    *_metrics(value),
                    expression_semantic_identity(value),
                ),
            )
            selected_module = _module_for(selected, ports, module)
            source_rtl = emit_experimental(source_module)
            selected_rtl = emit_experimental(selected_module)
            records.append(
                {
                    "source_identity": expression_semantic_identity(standalone),
                    "selected_identity": expression_semantic_identity(selected),
                    "status": "accepted",
                    "saturated": result.saturated,
                    "truncated": result.truncated,
                    "iterations": result.iterations,
                    "eclasses": result.eclass_count,
                    "alternatives": len(alternatives),
                    "certificates": len(result.certificates),
                    "rejections": list(result.rejection_reasons),
                    "source_metrics": source_metrics,
                    "selected_metrics": _metrics(selected),
                    "source_rtl_bytes": len(source_rtl.encode()),
                    "selected_rtl_bytes": len(selected_rtl.encode()),
                    "source_yosys": _yosys(source_rtl),
                    "selected_yosys": _yosys(selected_rtl),
                    "differential_simulation": _differential(
                        source_module, selected_module, ports
                    ),
                    "selected_term": (
                        "source"
                        if selected is standalone
                        else next(
                            render_term(term)
                            for term, expression in zip(
                                result.alternatives,
                                alternatives,
                                strict=True,
                            )
                            if expression == selected
                        )
                    ),
                }
            )
        except Exception as error:
            records.append(
                {
                    "source_identity": expression_semantic_identity(root),
                    "status": "ineligible_fallback",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
    return {
        "witness": slug,
        "profile": profile,
        "roots": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("small", "medium"), default="medium")
    parser.add_argument(
        "--witness",
        action="append",
        choices=DEFAULT_WITNESSES,
        dest="witnesses",
    )
    parser.add_argument("--json", type=Path, required=True)
    arguments = parser.parse_args()
    selected = tuple(arguments.witnesses or DEFAULT_WITNESSES)
    report = {
        "schema": SCHEMA,
        "production_enabled": False,
        "bounds": {
            "graph_nodes": MAX_GRAPH_NODES,
            "iterations": MAX_ITERATIONS,
            "terms": MAX_TERMS,
            "roots_per_witness": MAX_ROOTS,
        },
        "records": [_experiment(slug, arguments.profile) for slug in selected],
    }
    arguments.json.parent.mkdir(parents=True, exist_ok=True)
    arguments.json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
