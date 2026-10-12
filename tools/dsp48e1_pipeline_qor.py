#!/usr/bin/env python3
"""Manual DSP48E1 PipelineConfiguration routed validation; no exploration."""

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
try:
    from tools.vivado_routed_qor import routed_dsp48e1_tcl
except ModuleNotFoundError:  # Direct ``python tools/<script>.py`` execution.
    from vivado_routed_qor import routed_dsp48e1_tcl


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples" / "symmetric_fixed_fir.zhl"
REGISTERED = ROOT / "examples" / "symmetric_fixed_fir_dsp_pipelines.zhl"
VARIANTS = (
    ("unregistered", BASE, "SymmetricFixedFIR", "Xilinx7SymmetricDSPCascade", 1),
    ("multiply_registered", REGISTERED, "SymmetricFixedFIRMREG",
     "Xilinx7SymmetricDSPCascadeMREG", 2),
    ("multiply_output_registered", REGISTERED, "SymmetricFixedFIRMREGPREG",
     "Xilinx7SymmetricDSPCascadeMREGPREG", 3),
    ("fully_pipelined", REGISTERED, "SymmetricFixedFIRFullyPipelined",
     "Xilinx7SymmetricDSPCascadeFullyPipelined", 4),
)


def _run(variant, output: Path, vivado: str, part: str, period: float):
    name, source_path, top, architecture, latency = variant
    compilation = compile_source(
        source_path.read_text(), top=top, target=part,
        architecture=architecture, architecture_mode="required",
    )
    graph = compilation.implementation_graph
    if graph.latency != latency:
        raise ValueError(f"{name}: graph latency {graph.latency} != {latency}")
    work = output / name
    work.mkdir(parents=True, exist_ok=True)
    rtl_text = emit_target(compilation.ir, graph)
    rtl = work / f"{top}.sv"
    rtl.write_text(rtl_text)
    xdc = work / "timing.xdc"
    xdc.write_text(f"create_clock -name clk -period {period} [get_ports clk]\n")
    script = work / "run.tcl"
    script.write_text(routed_dsp48e1_tcl(rtl, xdc, work, top, part, period))
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
        raise RuntimeError(f"Vivado failed for {name}; see {work}")
    raw = dict(line.split("=", 1) for line in metrics_path.read_text().splitlines())
    return {
        "configuration": name, "pipeline_configuration": graph.pipeline_configuration_identity,
        "active_sites": graph.active_pipeline_sites, "latency": latency, "ii": 1,
        "dsp": int(raw["dsp"]), "lut": int(raw["lut"]), "ff": int(raw["ff"]),
        "bram": int(raw["bram"]), "wns_ns": float(raw["wns_ns"]),
        "fmax_mhz": float(raw["fmax_mhz"]), "dsp_configs": raw["dsp_configs"],
        "dsp_locations": raw["dsp_locations"], "compile_seconds": round(elapsed, 3),
        "rtl_bytes": rtl.stat().st_size, "rtl_lines": len(rtl_text.splitlines()),
        "implementation_graph": graph.identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = [executor.submit(
            _run, variant, args.output, args.vivado, args.part, args.period_ns,
        ) for variant in VARIANTS]
        rows = [future.result() for future in futures]
    payload = {"tool": "Vivado 2024.2", "part": args.part,
               "period_ns": args.period_ns, "results": rows}
    (args.output / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
