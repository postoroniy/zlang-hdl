#!/usr/bin/env python3
"""Route the selected high-level target-aware FIR candidates in Vivado."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import time

from zlang.backend.systemverilog import emit_target
from zlang.compiler import compile_source
from zlang.target_planner import EVIDENCE_SCHEMA
try:
    from tools.vivado_routed_qor import routed_dsp48e1_tcl
except ModuleNotFoundError:  # Direct ``python tools/<script>.py`` execution.
    from vivado_routed_qor import routed_dsp48e1_tcl


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "symmetric_fixed_fir_auto.zhl"
CONFIGURATIONS = (
    "unregistered",
    "multiply_registered",
    "multiply_output_registered",
    "fully_pipelined",
)
BACKEND = "direct_systemverilog"
TOOL = "Vivado"
TOOL_VERSION = "2024.2"


def build_evidence_payload(
    rows: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    target_identity: str,
    target_part: str,
    clock_period_ns: float,
) -> dict[str, object]:
    """Translate exact routed auto-FIR graphs into planner evidence."""

    configuration_order = {
        name: index for index, name in enumerate(CONFIGURATIONS)
    }
    ordered = sorted(
        rows,
        key=lambda row: (
            1 if str(row["name"]) == "exact8" else 0,
            configuration_order.get(
                str(row["configuration"]), len(configuration_order)
            ),
            str(row["implementation_graph"]),
        ),
    )
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in ordered:
        graph_identity = str(row["implementation_graph"])
        if not graph_identity or graph_identity in seen:
            raise ValueError("auto-FIR routed rows require unique graph identities")
        seen.add(graph_identity)
        configuration = str(row["configuration"])
        pipeline_identity = str(row["pipeline_configuration"])
        if configuration not in configuration_order:
            raise ValueError(
                f"unknown auto-FIR pipeline configuration '{configuration}'"
            )
        if not pipeline_identity.endswith(f".{configuration}"):
            raise ValueError(
                "auto-FIR routed row has mismatched pipeline configuration"
            )
        records.append({
            "key": {
                "target_identity": target_identity,
                "target_part": target_part,
                "architecture_template_identity": str(
                    row["architecture_template_identity"]
                ),
                "implementation_graph_identity": graph_identity,
                "pipeline_configuration_identity": pipeline_identity,
                "backend": BACKEND,
                "tool": TOOL,
                "tool_version": TOOL_VERSION,
                "clock_period_ns": float(clock_period_ns),
            },
            "stage": "routed_measurement",
            "lut": int(row["lut"]),
            "ff": int(row["ff"]),
            "dsp": int(row["dsp"]),
            "bram": int(row.get("bram", 0)),
            "fmax_mhz": float(row["fmax_mhz"]),
            "wns_ns": float(row["wns_ns"]),
            "provenance": (
                "high-level exact-latency auto-pipeline routed validation"
                if str(row["name"]) == "exact8"
                else "target-aware FIR auto-candidate routed validation"
            ),
        })
    return {"schema": EVIDENCE_SCHEMA, "records": records}


def _route(name, top, module, graph, output, vivado, part, period):
    work = output / name
    work.mkdir(parents=True, exist_ok=True)
    rtl_text = emit_target(module, graph)
    rtl = work / f"{top}.sv"
    rtl.write_text(rtl_text)
    xdc = work / "timing.xdc"
    xdc.write_text(f"create_clock -name clk -period {period} [get_ports clk]\n")
    script = work / "run.tcl"
    script.write_text(routed_dsp48e1_tcl(
        rtl, xdc, work, top, part, period, include_bram=False,
    ))
    started = time.monotonic()
    run = subprocess.run(
        (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
        cwd=work, capture_output=True, text=True,
    )
    (work / "vivado.stdout.log").write_text(run.stdout)
    (work / "vivado.stderr.log").write_text(run.stderr)
    if run.returncode or not (work / "metrics.txt").exists():
        raise RuntimeError(f"Vivado failed for {name}; see {work}")
    raw = dict(line.split("=", 1) for line in (work / "metrics.txt").read_text().splitlines())
    return {
        "name": name,
        "top": top,
        "implementation_graph": graph.identity,
        "architecture_template_identity": graph.architecture_template_identity,
        "pipeline_configuration": graph.pipeline_configuration_identity,
        "configuration": graph.pipeline_configuration_identity.rsplit(".", 1)[-1],
        "active_sites": list(graph.active_pipeline_sites),
        "latency": graph.latency,
        "ii": graph.initiation_interval,
        "compensation_cycles": sum(
            item.cycles for item in (graph.timing_dag.compensation_delays if graph.timing_dag else ())
        ),
        "dsp": int(raw["dsp"]),
        "bram": 0,
        "lut": int(raw["lut"]),
        "ff": int(raw["ff"]),
        "wns_ns": float(raw["wns_ns"]),
        "fmax_mhz": float(raw["fmax_mhz"]),
        "dsp_configs": raw["dsp_configs"],
        "dsp_locations": raw["dsp_locations"],
        "compile_seconds": round(time.monotonic() - started, 3),
        "rtl_bytes": len(rtl_text.encode()),
        "rtl_lines": len(rtl_text.splitlines()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="write deterministic zlang-target-qor-v2 planner evidence",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = SOURCE.read_text()
    bounded = compile_source(source, top="SymmetricFixedFIRAuto", target=args.part)
    exact = compile_source(source, top="SymmetricFixedFIRAutoExact8", target=args.part)
    bounded_graphs = tuple(
        candidate.graph
        for candidate in bounded.target_planning_result.generated_candidates
        if not candidate.graph.is_generic
    )
    if tuple(
        graph.pipeline_configuration_identity.rsplit(".", 1)[-1]
        for graph in bounded_graphs
    ) != CONFIGURATIONS:
        raise RuntimeError("auto-FIR planner did not produce the exact bounded set")
    exact_graph = next(
        item.graph for item in exact.target_planning_result.generated_candidates
        if item.graph.pipeline_configuration_identity
        and item.graph.pipeline_configuration_identity.endswith("multiply_output_registered")
    )
    route_requests = tuple(
        (
            f"bounded_{graph.pipeline_configuration_identity.rsplit('.', 1)[-1]}",
            "SymmetricFixedFIRAuto", bounded.ir, graph,
        )
        for graph in bounded_graphs
    ) + (
        ("exact8", "SymmetricFixedFIRAutoExact8", exact.ir, exact_graph),
    )
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = [
            executor.submit(
                _route, name, top, module, graph, args.output,
                args.vivado, args.part, args.period_ns,
            )
            for name, top, module, graph in route_requests
        ]
        rows = tuple(future.result() for future in futures)
    payload = {
        "schema": "zlang-target-auto-fir-qor-v1",
        "tool": "Vivado 2024.2",
        "part": args.part,
        "period_ns": args.period_ns,
        "results": rows,
    }
    (args.output / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.evidence_output is not None:
        target_identity = bounded_graphs[0].target_identity
        evidence = build_evidence_payload(
            rows,
            target_identity=target_identity,
            target_part=args.part,
            clock_period_ns=args.period_ns,
        )
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
