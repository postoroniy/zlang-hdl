#!/usr/bin/env python3
"""Routed validation for the scalar signed-product FFT pipeline slice.

This is a measurement harness, not planner logic.  It materializes the two
typed FFT component expressions, maps each one through every source-published
DSP48E1 PipelineConfiguration, and records the routed vendor result.
"""

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
from zlang.targets import (
    load_architecture_templates,
    load_target,
    map_auto_signed_product_configuration,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "fft" / "complex_multiply_pipeline_auto.zhl"
CONFIGURATIONS = (
    "unregistered", "multiply_registered", "multiply_output_registered",
    "fully_pipelined",
)
MODES = ("real", "imag")
BACKEND = "direct_systemverilog"
TOOL = "Vivado"
TOOL_VERSION = "2024.2"


def build_evidence_payload(
    rows: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    target_identity: str,
    target_part: str,
    architecture_template_identity: str,
    clock_period_ns: float,
) -> dict[str, object]:
    """Translate routed rows into deterministic planner evidence.

    The implementation graph identity comes directly from the graph that was
    emitted and routed by :func:`_run`.  It is deliberately never reconstructed
    from a configuration name or copied from an older evidence file.
    """

    mode_order = {name: index for index, name in enumerate(MODES)}
    configuration_order = {
        name: index for index, name in enumerate(CONFIGURATIONS)
    }
    ordered = sorted(
        rows,
        key=lambda row: (
            mode_order.get(str(row["mode"]), len(mode_order)),
            configuration_order.get(
                str(row["configuration"]), len(configuration_order)
            ),
            str(row["implementation_graph"]),
        ),
    )
    seen: set[tuple[str, str]] = set()
    records: list[dict[str, object]] = []
    for row in ordered:
        mode = str(row["mode"])
        configuration = str(row["configuration"])
        if mode not in mode_order:
            raise ValueError(f"unknown signed-product mode '{mode}'")
        if configuration not in configuration_order:
            raise ValueError(
                f"unknown signed-product pipeline configuration '{configuration}'"
            )
        variant = (mode, configuration)
        if variant in seen:
            raise ValueError(
                f"duplicate signed-product routed row '{mode}/{configuration}'"
            )
        seen.add(variant)
        graph_identity = str(row["implementation_graph"])
        pipeline_identity = str(row["pipeline_configuration"])
        if not graph_identity:
            raise ValueError(
                f"signed-product routed row '{mode}/{configuration}' has no graph identity"
            )
        if not pipeline_identity.endswith(f".{configuration}"):
            raise ValueError(
                f"signed-product routed row '{mode}/{configuration}' has mismatched "
                f"pipeline identity '{pipeline_identity}'"
            )
        records.append({
            "key": {
                "target_identity": target_identity,
                "target_part": target_part,
                "architecture_template_identity": architecture_template_identity,
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
            "bram": int(row["bram"]),
            "fmax_mhz": float(row["fmax_mhz"]),
            "wns_ns": float(row["wns_ns"]),
            "provenance": (
                f"signed FFT {mode} scalar cascade, routed Vivado checkpoint"
            ),
        })
    return {"schema": EVIDENCE_SCHEMA, "records": records}


def _tcl(rtl: Path, xdc: Path, work: Path, top: str, part: str, period: float) -> str:
    return f"""
read_verilog -sv {{{rtl}}}
read_xdc {{{xdc}}}
synth_design -top {top} -part {part} -flatten_hierarchy none
set data_inputs [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_input_delay 0.0 -clock clk $data_inputs
set_output_delay 0.0 -clock clk [all_outputs]
opt_design
place_design
phys_opt_design
route_design
report_utilization -file {{{work / 'utilization.rpt'}}}
report_timing_summary -file {{{work / 'timing.rpt'}}}
set path [get_timing_paths -setup -max_paths 1]
set wns [get_property SLACK $path]
set critical [expr {{{period} - $wns}}]
set fmax [expr {{$critical > 0.0 ? 1000.0 / $critical : 0.0}}]
set lut [llength [get_cells -hier -filter {{REF_NAME =~ LUT*}}]]
set ff [llength [get_cells -hier -filter {{REF_NAME =~ FD*}}]]
set dsp_cells [lsort [get_cells -hier -filter {{REF_NAME == DSP48E1}}]]
set dsp [llength $dsp_cells]
set bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set configs {{}}
set locations {{}}
foreach cell $dsp_cells {{
  lappend configs "[get_property AREG $cell]/[get_property BREG $cell]/[get_property DREG $cell]/[get_property MREG $cell]/[get_property PREG $cell]"
  lappend locations [get_property LOC $cell]
}}
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
puts $output "bram=$bram"
puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
puts $output "dsp_configs=[join $configs ,]"
puts $output "dsp_locations=[join $locations ,]"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def _run(
    mode: str, configuration_name: str, output: Path,
    vivado: str, part: str, period: float,
) -> dict[str, object]:
    top = (
        "FFTComplexMultiplyRealAuto"
        if mode == "real" else "FFTComplexMultiplyImagAuto"
    )
    typed = compile_source(SOURCE.read_text(), top=top)
    target, family, resources = load_target(part)
    template = next(
        item for item in load_architecture_templates(operation="signed_product_reduction")
        if item.name == "Xilinx7SignedProductCascade"
    )
    resource = next(item for item in resources if item.name == "DSP48E1")
    configuration = resource.pipeline_configuration(configuration_name)
    graph = map_auto_signed_product_configuration(
        typed.ir, target, family, resources, template, configuration,
    )
    work = output / f"{mode}_{configuration_name}"
    work.mkdir(parents=True, exist_ok=True)
    rtl_text = emit_target(typed.ir, graph)
    rtl = work / f"{top}.sv"
    rtl.write_text(rtl_text)
    xdc = work / "timing.xdc"
    xdc.write_text(f"create_clock -name clk -period {period} [get_ports clk]\n")
    script = work / "run.tcl"
    script.write_text(_tcl(rtl, xdc, work, top, part, period))
    started = time.monotonic()
    completed = subprocess.run(
        (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
        cwd=work, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - started
    (work / "vivado.stdout.log").write_text(completed.stdout)
    (work / "vivado.stderr.log").write_text(completed.stderr)
    metrics_path = work / "metrics.txt"
    if completed.returncode or not metrics_path.exists():
        raise RuntimeError(f"Vivado failed for {mode}/{configuration_name}; see {work}")
    raw = dict(line.split("=", 1) for line in metrics_path.read_text().splitlines())
    return {
        "mode": mode,
        "configuration": configuration_name,
        "pipeline_configuration": graph.pipeline_configuration_identity,
        "active_sites": graph.active_pipeline_sites,
        "latency": graph.latency,
        "ii": graph.initiation_interval,
        "dsp": int(raw["dsp"]),
        "lut": int(raw["lut"]),
        "ff": int(raw["ff"]),
        "bram": int(raw["bram"]),
        "wns_ns": float(raw["wns_ns"]),
        "fmax_mhz": float(raw["fmax_mhz"]),
        "dsp_configs": raw["dsp_configs"],
        "dsp_locations": raw["dsp_locations"],
        "compile_seconds": round(elapsed, 3),
        "rtl_bytes": rtl.stat().st_size,
        "rtl_lines": len(rtl_text.splitlines()),
        "implementation_graph": graph.identity,
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
        help=(
            "write deterministic zlang-target-qor-v1 evidence for the routed "
            "implementation graphs"
        ),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    variants = tuple(
        (mode, configuration)
        for mode in MODES
        for configuration in CONFIGURATIONS
    )
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = [executor.submit(
            _run, mode, configuration, args.output, args.vivado,
            args.part, args.period_ns,
        ) for mode, configuration in variants]
        rows = [future.result() for future in futures]
    payload = {
        "tool": "Vivado 2024.2", "part": args.part,
        "period_ns": args.period_ns, "results": rows,
    }
    (args.output / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    if args.evidence_output is not None:
        target, _, _ = load_target(args.part)
        template = next(
            item for item in load_architecture_templates(
                operation="signed_product_reduction"
            )
            if item.name == "Xilinx7SignedProductCascade"
        )
        evidence = build_evidence_payload(
            rows,
            target_identity=target.identity,
            target_part=args.part,
            architecture_template_identity=template.identity,
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
