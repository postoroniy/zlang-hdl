#!/usr/bin/env python3
"""Reproducible Clash/direct-SV QoR evidence for the canonical 802.11a top.

This runner deliberately compiles one ZLang semantic module once and emits both
backends from that identical IR.  It publishes the complete ROM companion
bundle, passes every generated Clash Verilog file to downstream tools, and
records failures/timeouts as evidence instead of silently dropping a backend.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Any

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.ir.module import dependency_context_identity
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "transmitter.zl"
)
DEFAULT_TOP = "Ieee80211aTransmitter"
DEFAULT_PART = "xc7z030ffg676-1"


@dataclass(frozen=True)
class ProcessEvidence:
    status: str
    returncode: int | None
    elapsed_seconds: float
    command: tuple[str, ...]
    stdout_log: str
    stderr_log: str


@dataclass(frozen=True)
class RtlEvidence:
    backend: str
    generation_seconds: float
    artifact_hash: str
    files: tuple[str, ...]
    rtl_bytes: int
    rtl_lines: int
    companion_count: int
    companion_hashes: tuple[tuple[str, str], ...]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _version(command: tuple[str, ...]) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else "unknown"


def _run_bounded(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
    stdout_log: Path,
    stderr_log: Path,
) -> ProcessEvidence:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    status = "completed"
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        status = "timeout"
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    elapsed = time.monotonic() - started
    stdout_log.write_text(stdout)
    stderr_log.write_text(stderr)
    if status == "completed" and process.returncode != 0:
        status = "failed"
    return ProcessEvidence(
        status=status,
        returncode=process.returncode,
        elapsed_seconds=round(elapsed, 3),
        command=command,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )


def _rtl_evidence(
    backend: str,
    generation_seconds: float,
    artifact_hash: str,
    files: tuple[Path, ...],
    companions: tuple[object, ...],
    output: Path,
) -> RtlEvidence:
    return RtlEvidence(
        backend=backend,
        generation_seconds=round(generation_seconds, 3),
        artifact_hash=artifact_hash,
        files=tuple(str(path.relative_to(output)) for path in files),
        rtl_bytes=sum(path.stat().st_size for path in files),
        rtl_lines=sum(len(path.read_text().splitlines()) for path in files),
        companion_count=len(companions),
        companion_hashes=tuple(
            sorted((item.logical_path, item.file_hash) for item in companions)
        ),
    )


def _yosys_script(
    files: tuple[Path, ...], top: str, generic_stats: Path, mapped_stats: Path
) -> str:
    # Yosys command files are not Tcl: brace quoting becomes part of the file
    # name.  Generated evidence paths are controlled and contain no whitespace.
    sources = " ".join(str(path) for path in files)
    return "\n".join(
        (
            f"read_verilog -sv {sources}",
            f"hierarchy -check -top {top}",
            # Gather the bounded generic snapshot on a disposable design copy;
            # mapped evidence must retain the ordinary synth_xilinx pass order.
            "design -save zlang_unlowered",
            "proc; opt; memory_collect",
            f"tee -o {generic_stats} stat -json -top {top}",
            "design -load zlang_unlowered",
            f"synth_xilinx -family xc7 -top {top}",
            f"tee -o {mapped_stats} stat -json -top {top}",
        )
    )


def _primitive_cell_counts(
    statistics: dict[str, Any], top: str
) -> dict[str, int]:
    modules = {
        str(name).lstrip("\\"): value
        for name, value in statistics.get("modules", {}).items()
    }
    primitive_cells: dict[str, int] = {}

    def collect(module_name: str, multiplier: int, active: tuple[str, ...]) -> None:
        if module_name in active:
            raise ValueError(
                "recursive synthesized hierarchy: "
                + " -> ".join((*active, module_name))
            )
        module = modules.get(module_name)
        if module is None:
            primitive_cells[module_name] = (
                primitive_cells.get(module_name, 0) + multiplier
            )
            return
        for cell_type, count in module.get("num_cells_by_type", {}).items():
            normalized = str(cell_type).lstrip("\\")
            instances = multiplier * int(count)
            if normalized in modules:
                collect(normalized, instances, (*active, module_name))
            else:
                primitive_cells[normalized] = (
                    primitive_cells.get(normalized, 0) + instances
                )

    if top not in modules:
        return {}
    collect(top, 1, ())
    return primitive_cells


def _cell_metrics(statistics: dict[str, Any], top: str) -> dict[str, int]:
    primitive_cells = _primitive_cell_counts(statistics, top)

    def count(prefixes: tuple[str, ...]) -> int:
        return sum(
            int(value)
            for name, value in primitive_cells.items()
            if name.startswith(prefixes)
        )

    return {
        "lut": count(("LUT",)),
        "ff": count(("FD",)),
        "dsp": count(("DSP",)),
        "bram": count(("RAMB",)),
        "total_cells": sum(primitive_cells.values()),
    }


def _generic_metrics(statistics: dict[str, Any], top: str) -> dict[str, Any]:
    cells = _primitive_cell_counts(statistics, top)

    def exact(*names: str) -> int:
        return sum(cells.get(name, 0) for name in names)

    return {
        "total_cells": sum(cells.values()),
        "multiply_cells": exact("$mul", "$macc"),
        "add_sub_cells": exact("$add", "$sub", "$alu"),
        "register_cells": sum(
            value
            for name, value in cells.items()
            if name.startswith(
                ("$dff", "$sdff", "$adff", "$aldff", "$ff", "$dlatch")
            )
        ),
        "memory_cells": sum(
            value for name, value in cells.items() if name.startswith("$mem")
        ),
        "cell_types": dict(sorted(cells.items())),
    }


def _run_yosys(
    backend: str,
    rtl: tuple[Path, ...],
    top: str,
    output: Path,
    executable: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    work = output / "yosys" / backend
    work.mkdir(parents=True, exist_ok=True)
    generic_stats = work / "statistics.generic.json"
    mapped_stats = work / "statistics.mapped.json"
    script = work / "run.ys"
    script.write_text(_yosys_script(rtl, top, generic_stats, mapped_stats))
    run = _run_bounded(
        (executable, "-s", str(script)),
        cwd=work,
        timeout_seconds=timeout_seconds,
        stdout_log=work / "stdout.log",
        stderr_log=work / "stderr.log",
    )
    result: dict[str, Any] = {
        "process": asdict(run),
        "generic_metrics": None,
        "mapped_metrics": None,
    }
    if generic_stats.is_file():
        try:
            parsed = json.loads(generic_stats.read_text())
            result["generic_metrics"] = _generic_metrics(parsed, top)
            result["generic_statistics"] = str(generic_stats)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
            result["generic_statistics_error"] = str(error)
    if mapped_stats.is_file():
        try:
            parsed = json.loads(mapped_stats.read_text())
            result["mapped_metrics"] = _cell_metrics(parsed, top)
            result["mapped_statistics"] = str(mapped_stats)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
            result["mapped_statistics_error"] = str(error)
    _write_json(work / "result.json", result)
    return result


def _vivado_tcl(
    files: tuple[Path, ...], top: str, part: str, period: float, work: Path
) -> str:
    reads = "\n".join(
        f"read_verilog {'-sv ' if path.suffix == '.sv' else ''}{{{path}}}"
        for path in files
    )
    return f"""
set status [open {{{work / 'status.txt'}}} w]
puts $status "stage=read"
flush $status
{reads}
if {{[catch {{synth_design -top {top} -part {part} -flatten_hierarchy none}} message]}} {{
  puts $status "stage=synth_failed"
  puts $status "message=$message"
  close $status
  exit 2
}}
puts $status "stage=synthesized"
flush $status
report_utilization -file {{{work / 'utilization_synth.rpt'}}}
set synth_lut [llength [get_cells -hier -filter {{REF_NAME =~ LUT*}}]]
set synth_ff [llength [get_cells -hier -filter {{REF_NAME =~ FD*}}]]
set synth_dsp [llength [get_cells -hier -filter {{REF_NAME =~ DSP*}}]]
set synth_bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set synth_metrics [open {{{work / 'metrics.synth.txt'}}} w]
puts $synth_metrics "lut=$synth_lut"
puts $synth_metrics "ff=$synth_ff"
puts $synth_metrics "dsp=$synth_dsp"
puts $synth_metrics "bram=$synth_bram"
close $synth_metrics
create_clock -name clk -period {period} [get_ports clk]
set data_inputs [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_input_delay 0.0 -clock clk $data_inputs
set_output_delay 0.0 -clock clk [all_outputs]
if {{[catch {{opt_design}} message]}} {{
  puts $status "stage=opt_failed"
  puts $status "message=$message"
  close $status
  exit 3
}}
if {{[catch {{place_design}} message]}} {{
  puts $status "stage=place_failed"
  puts $status "message=$message"
  close $status
  exit 3
}}
puts $status "stage=placed"
flush $status
if {{[catch {{phys_opt_design}} message]}} {{
  puts $status "stage=phys_opt_failed"
  puts $status "message=$message"
  close $status
  exit 4
}}
if {{[catch {{route_design}} message]}} {{
  puts $status "stage=route_failed"
  puts $status "message=$message"
  close $status
  exit 4
}}
puts $status "stage=routed"
flush $status
report_utilization -file {{{work / 'utilization.routed.rpt'}}}
report_timing_summary -file {{{work / 'timing.routed.rpt'}}}
set path [get_timing_paths -setup -max_paths 1]
set wns [get_property SLACK $path]
set critical [expr {{{period} - $wns}}]
set fmax [expr {{$critical > 0.0 ? 1000.0 / $critical : 0.0}}]
set lut [llength [get_cells -hier -filter {{REF_NAME =~ LUT*}}]]
set ff [llength [get_cells -hier -filter {{REF_NAME =~ FD*}}]]
set dsp [llength [get_cells -hier -filter {{REF_NAME =~ DSP*}}]]
set bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set metrics [open {{{work / 'metrics.txt'}}} w]
puts $metrics "lut=$lut"
puts $metrics "ff=$ff"
puts $metrics "dsp=$dsp"
puts $metrics "bram=$bram"
puts $metrics "wns_ns=$wns"
puts $metrics "critical_delay_ns=$critical"
puts $metrics "fmax_mhz=$fmax"
close $metrics
puts $status "stage=complete"
close $status
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def _key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def _run_vivado(
    backend: str,
    rtl: tuple[Path, ...],
    top: str,
    output: Path,
    executable: str,
    part: str,
    period: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    work = output / "vivado" / backend
    work.mkdir(parents=True, exist_ok=True)
    script = work / "run.tcl"
    script.write_text(_vivado_tcl(rtl, top, part, period, work))
    run = _run_bounded(
        (
            executable,
            "-mode",
            "batch",
            "-nojournal",
            "-nolog",
            "-source",
            str(script),
        ),
        cwd=work,
        timeout_seconds=timeout_seconds,
        stdout_log=work / "stdout.log",
        stderr_log=work / "stderr.log",
    )
    status_file = work / "status.txt"
    result: dict[str, Any] = {
        "process": asdict(run),
        "stage": _key_values(status_file).get("stage", "not_started")
        if status_file.is_file()
        else "not_started",
        "metrics": None,
        "synthesis_metrics": None,
    }
    synthesis_metrics = work / "metrics.synth.txt"
    if synthesis_metrics.is_file():
        raw = _key_values(synthesis_metrics)
        result["synthesis_metrics"] = {
            "lut": int(raw["lut"]),
            "ff": int(raw["ff"]),
            "dsp": int(raw["dsp"]),
            "bram": int(raw["bram"]),
        }
    metrics = work / "metrics.txt"
    if metrics.is_file():
        raw = _key_values(metrics)
        result["metrics"] = {
            "lut": int(raw["lut"]),
            "ff": int(raw["ff"]),
            "dsp": int(raw["dsp"]),
            "bram": int(raw["bram"]),
            "wns_ns": float(raw["wns_ns"]),
            "critical_delay_ns": float(raw["critical_delay_ns"]),
            "fmax_mhz": float(raw["fmax_mhz"]),
        }
    result["status_file"] = str(status_file) if status_file.is_file() else None
    _write_json(work / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--top", default=DEFAULT_TOP)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--part", default=DEFAULT_PART)
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument(
        "--backend",
        choices=("both", "direct_sv", "clash"),
        default="both",
        help="emit and measure both backends or one bounded rerun",
    )
    parser.add_argument("--clash", default=find_clash_executable())
    parser.add_argument("--yosys", default=shutil.which("yosys") or "yosys")
    parser.add_argument(
        "--vivado",
        default=os.environ.get("ZLANG_VIVADO") or shutil.which("vivado") or "vivado",
        help="Vivado executable (defaults to ZLANG_VIVADO or PATH)",
    )
    parser.add_argument("--yosys-timeout", type=int, default=900)
    parser.add_argument("--vivado-timeout", type=int, default=3600)
    parser.add_argument(
        "--skip-yosys", action="store_true", help="only generate RTL/Vivado evidence"
    )
    parser.add_argument(
        "--skip-vivado", action="store_true", help="only generate RTL/Yosys evidence"
    )
    args = parser.parse_args()
    enabled = (
        ("direct_sv", "clash")
        if args.backend == "both"
        else (args.backend,)
    )
    if "clash" in enabled and not args.clash:
        parser.error("Clash was not found; pass --clash")
    if args.period_ns <= 0:
        parser.error("--period-ns must be positive")
    args.output.mkdir(parents=True, exist_ok=True)

    source = args.source.resolve()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    frontend_started = time.monotonic()
    module = compile_file(source, top=args.top, include_clash=False).ir
    frontend_seconds = time.monotonic() - frontend_started

    rtl: dict[str, tuple[Path, ...]] = {}
    rtl_evidence: dict[str, Any] = {}
    if "direct_sv" in enabled:
        direct_started = time.monotonic()
        direct_artifact = emit_sv_artifact(module)
        direct_directory = args.output / "rtl" / "direct_sv"
        direct_directory.mkdir(parents=True, exist_ok=True)
        direct_file = direct_directory / f"{args.top}.sv"
        direct_file.write_text(direct_artifact.text)
        publish_companion_bundle(direct_artifact.companions, direct_directory)
        direct_seconds = time.monotonic() - direct_started
        rtl["direct_sv"] = (direct_file,)
        rtl_evidence["direct_sv"] = asdict(
            _rtl_evidence(
                "direct_sv",
                direct_seconds,
                direct_artifact.artifact_hash,
                rtl["direct_sv"],
                direct_artifact.companions,
                args.output,
            )
        )
    if "clash" in enabled:
        assert args.clash is not None
        clash_started = time.monotonic()
        clash_artifact = emit_clash_artifact(module)
        clash_directory = args.output / "rtl" / "clash"
        clash_files = generate_verilog(
            clash_artifact.text,
            args.top,
            clash_directory,
            args.clash,
            companions=clash_artifact.companions,
        )
        clash_seconds = time.monotonic() - clash_started
        rtl["clash"] = tuple(clash_files)
        rtl_evidence["clash"] = asdict(
            _rtl_evidence(
                "clash",
                clash_seconds,
                clash_artifact.artifact_hash,
                rtl["clash"],
                clash_artifact.companions,
                args.output,
            )
        )
    common = {
        "schema": "zlang-80211a-backend-qor-v1",
        "source": str(source),
        "source_sha256": source_hash,
        "dependency_identity": dependency_context_identity(module),
        "top": args.top,
        "part": args.part,
        "period_ns": args.period_ns,
        "requested_backend": args.backend,
        "frontend_seconds": round(frontend_seconds, 3),
        "tools": {
            "clash": _version((args.clash, "--version"))
            if "clash" in enabled
            else "not_requested",
            "yosys": _version((args.yosys, "-V")),
            "vivado": _version((args.vivado, "-version")),
        },
        "rtl": rtl_evidence,
        "yosys": {},
        "vivado": {},
    }
    _write_json(args.output / "results.json", common)

    if not args.skip_yosys:
        for backend, files in rtl.items():
            common["yosys"][backend] = _run_yosys(
                backend,
                files,
                args.top,
                args.output,
                args.yosys,
                args.yosys_timeout,
            )
            _write_json(args.output / "results.json", common)

    if not args.skip_vivado:
        for backend, files in rtl.items():
            common["vivado"][backend] = _run_vivado(
                backend,
                files,
                args.top,
                args.output,
                args.vivado,
                args.part,
                args.period_ns,
                args.vivado_timeout,
            )
            _write_json(args.output / "results.json", common)

    _write_json(args.output / "results.json", common)
    print(json.dumps(common, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
