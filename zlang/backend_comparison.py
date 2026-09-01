"""Reproducible Clash versus direct-SystemVerilog experiment."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
from time import perf_counter
from typing import Sequence

from zlang._version import __version__
from zlang.backend.systemverilog import emit_experimental
from zlang.backend_comparison_sources import BACKEND_COMPARISON_SOURCES
from zlang.compiler import compile_source
from zlang.synthesis import YosysTarget
from zlang.toolchain import (
    clash_subprocess_environment,
    find_clash_executable,
    generate_verilog,
)


COMPARISON_SCHEMA = "zlang-backend-comparison-v1"


class BackendComparisonError(RuntimeError):
    """The comparison tools or one representative design failed."""


@dataclass(frozen=True)
class Benchmark:
    name: str
    category: str
    source: str
    behavior_checks: int


BENCHMARKS = (
    Benchmark("ALU", "datapath", "examples/alu.zl", 5),
    Benchmark("PipelinedMAC", "pipeline", "examples/pipelined_mac.zl", 3),
    Benchmark("RvPassthrough", "ready_valid", "examples/rv_passthrough.zl", 3),
    Benchmark("CreditSource", "credit", "examples/credit_source.zl", 6),
    Benchmark("ControlCsr", "csr", "examples/control_csr.zl", 5),
    Benchmark("RequestClient", "request_response", "examples/request_client.zl", 8),
    Benchmark("RuleCounter", "rules", "examples/rule_counter.zl", 4),
)


@dataclass(frozen=True)
class SynthesisMeasurement:
    lut_cells: int
    flip_flops: int
    total_cells: int
    logic_depth: int
    elapsed_ms: float


@dataclass(frozen=True)
class BackendMeasurement:
    backend: str
    backend_source_loc: int
    generated_rtl_loc: int
    codegen_ms: float
    lint_ms: float
    behavior_passed: bool
    synthesis: SynthesisMeasurement


@dataclass(frozen=True)
class BenchmarkResult:
    name: str
    category: str
    zlang_source: str
    zlang_source_loc: int
    frontend_ms: float
    behavior_checks: int
    clash: BackendMeasurement
    direct_systemverilog: BackendMeasurement


@dataclass(frozen=True)
class DiagnosticProbe:
    backend: str
    compiler: str
    elapsed_ms: float
    failed_as_expected: bool
    generated_artifact_line: int | None
    mentions_missing_symbol: bool
    zlang_source_location: bool


@dataclass(frozen=True)
class ComparisonReport:
    schema: str
    host: str
    python_version: str
    clash_version: str
    verilator_version: str
    yosys_version: str
    iverilog_version: str
    synthesis_target: str
    synthesis_constraints: tuple[tuple[str, str], ...]
    timing_repetitions: int
    benchmarks: tuple[BenchmarkResult, ...]
    diagnostics: tuple[DiagnosticProbe, ...]
    decision: str


def run_comparison(
    project_root: Path | None,
    artifact_directory: Path,
    *,
    repetitions: int = 1,
    clash_executable: str | None = None,
    verilator_executable: str | None = None,
    yosys_executable: str | None = None,
    iverilog_executable: str | None = None,
    vvp_executable: str | None = None,
) -> ComparisonReport:
    """Run all representative cases with identical synthesis constraints."""

    if repetitions < 1:
        raise BackendComparisonError("timing repetitions must be positive")
    clash = clash_executable or find_clash_executable()
    verilator = verilator_executable or shutil.which("verilator")
    yosys = yosys_executable or shutil.which("yosys")
    iverilog = iverilog_executable or shutil.which("iverilog")
    vvp = vvp_executable or shutil.which("vvp")
    missing = tuple(
        name
        for name, executable in (
            ("Clash", clash),
            ("Verilator", verilator),
            ("Yosys", yosys),
            ("Icarus Verilog", iverilog),
            ("vvp", vvp),
        )
        if executable is None
    )
    if missing:
        raise BackendComparisonError(
            "backend comparison requires: " + ", ".join(missing)
        )
    assert clash and verilator and yosys and iverilog and vvp
    versions = {
        "clash": _version(
            (clash, "--version"),
            environment=clash_subprocess_environment(clash),
        ),
        "verilator": _version((verilator, "--version")),
        "yosys": _version((yosys, "-V")),
        "iverilog": _version((iverilog, "-V")),
    }
    target = YosysTarget()
    comparison_constraints = (
        *target.constraints,
        ("formal_cells", "removed"),
        ("synth", "flatten"),
        ("timing_proxy", "longest_topological_path_without_ff"),
    )
    artifact_directory.mkdir(parents=True, exist_ok=True)
    results: list[BenchmarkResult] = []
    first_clash_source = ""
    first_direct_source = ""
    for benchmark in BENCHMARKS:
        source = load_benchmark_source(benchmark, project_root)
        frontend_times: list[float] = []
        compilation = None
        for _ in range(repetitions):
            start = perf_counter()
            compilation = compile_source(source)
            frontend_times.append(_elapsed_ms(start))
        assert compilation is not None
        start = perf_counter()
        direct_source = emit_experimental(compilation.ir)
        direct_codegen_ms = _elapsed_ms(start)
        case_directory = artifact_directory / benchmark.name
        case_directory.mkdir(parents=True, exist_ok=True)
        clash_source_path = case_directory / f"{benchmark.name}.hs"
        direct_path = case_directory / f"{benchmark.name}.sv"
        clash_source_path.write_text(compilation.clash)
        direct_path.write_text(direct_source)
        clash_codegen_times: list[float] = []
        clash_files: tuple[Path, ...] = ()
        for iteration in range(repetitions):
            rtl_directory = case_directory / f"clash-rtl-{iteration}"
            start = perf_counter()
            clash_files = generate_verilog(
                compilation.clash,
                benchmark.name,
                rtl_directory,
                clash,
            )
            clash_codegen_times.append(_elapsed_ms(start))
        clash_lint_ms = _lint(
            verilator,
            benchmark.name,
            clash_files,
            systemverilog=False,
        )
        direct_lint_ms = _lint(
            verilator,
            benchmark.name,
            (direct_path,),
            systemverilog=True,
        )
        testbench = _testbench(benchmark.name)
        testbench_path = case_directory / f"{benchmark.name}_tb.sv"
        testbench_path.write_text(testbench)
        _run_behavior(iverilog, vvp, clash_files, testbench_path, case_directory / "clash-sim")
        _run_behavior(iverilog, vvp, (direct_path,), testbench_path, case_directory / "direct-sim")
        clash_synthesis = _synthesize(
            yosys,
            benchmark.name,
            clash_files,
            systemverilog=False,
            working_directory=case_directory / "clash-yosys",
            target=target,
        )
        direct_synthesis = _synthesize(
            yosys,
            benchmark.name,
            (direct_path,),
            systemverilog=True,
            working_directory=case_directory / "direct-yosys",
            target=target,
        )
        results.append(
            BenchmarkResult(
                benchmark.name,
                benchmark.category,
                benchmark.source,
                _loc(source),
                round(statistics.median(frontend_times), 3),
                benchmark.behavior_checks,
                BackendMeasurement(
                    "clash",
                    _loc(compilation.clash),
                    sum(_loc(path.read_text()) for path in clash_files),
                    round(statistics.median(clash_codegen_times), 3),
                    clash_lint_ms,
                    True,
                    clash_synthesis,
                ),
                BackendMeasurement(
                    "direct_systemverilog",
                    _loc(direct_source),
                    _loc(direct_source),
                    round(direct_codegen_ms, 3),
                    direct_lint_ms,
                    True,
                    direct_synthesis,
                ),
            )
        )
        if benchmark.name == "ALU":
            first_clash_source = compilation.clash
            first_direct_source = direct_source
    diagnostics = _diagnostic_probes(
        artifact_directory,
        clash,
        verilator,
        first_clash_source,
        first_direct_source,
    )
    return ComparisonReport(
        COMPARISON_SCHEMA,
        platform.platform(),
        platform.python_version(),
        versions["clash"],
        versions["verilator"],
        versions["yosys"],
        versions["iverilog"],
        target.name,
        comparison_constraints,
        repetitions,
        tuple(results),
        diagnostics,
        (
            "retain_clash_default_keep_direct_systemverilog_experimental; "
            "revisit_region_partition_after_source_mapping_and_broader_backend_coverage"
        ),
    )


def render_json(report: ComparisonReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def render_markdown(report: ComparisonReport) -> str:
    lines = [
        "# Clash versus direct SystemVerilog measurements",
        "",
        f"Schema: `{report.schema}`. Timing repetitions: {report.timing_repetitions}.",
        "",
        "| Category | Design | ZLang LOC | Clash Haskell LOC | Clash Verilog LOC | Direct SV LOC | Clash LUT/FF/depth | Direct LUT/FF/depth | Clash codegen ms | Direct emit ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report.benchmarks:
        clash = item.clash
        direct = item.direct_systemverilog
        lines.append(
            f"| {item.category} | {item.name} | {item.zlang_source_loc} | "
            f"{clash.backend_source_loc} | {clash.generated_rtl_loc} | "
            f"{direct.generated_rtl_loc} | "
            f"{clash.synthesis.lut_cells}/{clash.synthesis.flip_flops}/"
            f"{clash.synthesis.logic_depth} | "
            f"{direct.synthesis.lut_cells}/{direct.synthesis.flip_flops}/"
            f"{direct.synthesis.logic_depth} | {clash.codegen_ms:.3f} | "
            f"{direct.codegen_ms:.3f} |"
        )
    lines.extend(
        (
            "",
            "All cases passed the same behavioral testbench through both RTL paths.",
            f"Decision: `{report.decision}`.",
            "",
            "Tool versions:",
            "",
            f"- Clash: `{report.clash_version}`",
            f"- Verilator: `{report.verilator_version}`",
            f"- Yosys: `{report.yosys_version}`",
            f"- Icarus Verilog: `{report.iverilog_version}`",
            "",
        )
    )
    return "\n".join(lines)


def load_benchmark_source(
    benchmark: Benchmark,
    project_root: Path | None = None,
) -> str:
    """Load an explicit checkout source or the compiler-shipped corpus copy."""

    if project_root is None:
        try:
            return BACKEND_COMPARISON_SOURCES[benchmark.source]
        except KeyError as error:
            raise BackendComparisonError(
                f"packaged backend-comparison source is missing: {benchmark.source}"
            ) from error
    source_path = Path(project_root) / benchmark.source
    try:
        return source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise BackendComparisonError(
            f"could not read backend-comparison source '{source_path}': {error}"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zlang-compare-backends",
        description="Run the ZLang Clash/direct-SystemVerilog experiment"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help=(
            "read benchmark sources from a ZLang checkout instead of the "
            "compiler-shipped corpus"
        ),
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    arguments = parser.parse_args(argv)
    try:
        report = run_comparison(
            arguments.project_root,
            arguments.artifacts,
            repetitions=arguments.repetitions,
        )
    except BackendComparisonError as error:
        parser.error(str(error))
    arguments.json.parent.mkdir(parents=True, exist_ok=True)
    arguments.markdown.parent.mkdir(parents=True, exist_ok=True)
    arguments.json.write_text(render_json(report))
    arguments.markdown.write_text(render_markdown(report))
    return 0


def _lint(
    verilator: str,
    top: str,
    files: tuple[Path, ...],
    *,
    systemverilog: bool,
) -> float:
    command = [
        verilator,
        "--lint-only",
        "-Wno-WIDTHTRUNC",
        "-Wno-WIDTHEXPAND",
        "--top-module",
        top,
        *(str(path) for path in files),
    ]
    start = perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True)
    elapsed = _elapsed_ms(start)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackendComparisonError(
            f"Verilator rejected {top} ({'direct SV' if systemverilog else 'Clash'}): {detail}"
        )
    return round(elapsed, 3)


def _run_behavior(
    iverilog: str,
    vvp: str,
    rtl_files: tuple[Path, ...],
    testbench: Path,
    output: Path,
) -> None:
    completed = subprocess.run(
        (
            iverilog,
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(output),
            *(str(path) for path in rtl_files),
            str(testbench),
        ),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackendComparisonError(f"behavior compile failed: {detail}")
    completed = subprocess.run((vvp, str(output)), text=True, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackendComparisonError(f"behavior simulation failed: {detail}")


def _synthesize(
    yosys: str,
    top: str,
    files: tuple[Path, ...],
    *,
    systemverilog: bool,
    working_directory: Path,
    target: YosysTarget,
) -> SynthesisMeasurement:
    working_directory.mkdir(parents=True, exist_ok=True)
    stats_path = working_directory / "stats.json"
    depth_path = working_directory / "depth.txt"
    read_mode = "read_verilog -sv" if systemverilog else "read_verilog"
    read_files = " ".join(_quote(path) for path in files)
    script = "; ".join(
        (
            f"{read_mode} {read_files}",
            f"hierarchy -check -top {top}",
            "chformal -remove",
            f"synth -top {top} -flatten",
            f"abc -lut {target.lut_inputs}",
            "clean",
            f"tee -o {_quote(stats_path)} stat -json",
            f"tee -o {_quote(depth_path)} ltp -noff",
        )
    )
    start = perf_counter()
    completed = subprocess.run(
        (yosys, "-q", "-p", script),
        text=True,
        capture_output=True,
    )
    elapsed = _elapsed_ms(start)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackendComparisonError(f"Yosys failed for {top}: {detail}")
    try:
        design = json.loads(stats_path.read_text())["design"]
        cells = design["num_cells_by_type"]
        depth_text = depth_path.read_text()
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise BackendComparisonError(f"invalid Yosys result for {top}: {error}") from error
    match = re.search(r"Longest topological path.*\(length=(\d+)\)", depth_text)
    return SynthesisMeasurement(
        int(cells.get("$lut", 0)),
        sum(int(value) for key, value in cells.items() if "DFF" in key.upper()),
        int(design["num_cells"]),
        int(match.group(1)) if match is not None else 0,
        round(elapsed, 3),
    )


def _diagnostic_probes(
    directory: Path,
    clash: str,
    verilator: str,
    clash_source: str,
    direct_source: str,
) -> tuple[DiagnosticProbe, ...]:
    probe = directory / "diagnostics"
    probe.mkdir(parents=True, exist_ok=True)
    clash_line = clash_source.index("topEntity ::")
    broken_clash = clash_source[:clash_line] + (
        "zlangInjectedError = zlang_missing_symbol\n\n"
    ) + clash_source[clash_line:]
    clash_path = probe / "ALU.hs"
    clash_path.write_text(broken_clash)
    start = perf_counter()
    clash_result = subprocess.run(
        (clash, "--verilog", str(clash_path), "-outputdir", str(probe / "clash")),
        text=True,
        capture_output=True,
        env=clash_subprocess_environment(clash),
    )
    clash_ms = _elapsed_ms(start)
    direct_line = direct_source.index("endmodule")
    broken_direct = direct_source[:direct_line] + (
        "  wire zlang_injected_error = zlang_missing_symbol;\n"
    ) + direct_source[direct_line:]
    direct_path = probe / "ALU.sv"
    direct_path.write_text(broken_direct)
    start = perf_counter()
    direct_result = subprocess.run(
        (verilator, "--lint-only", "--top-module", "ALU", str(direct_path)),
        text=True,
        capture_output=True,
    )
    direct_ms = _elapsed_ms(start)
    return (
        _probe_result("clash", "GHC/Clash", clash_path, clash_result, clash_ms),
        _probe_result(
            "direct_systemverilog",
            "Verilator",
            direct_path,
            direct_result,
            direct_ms,
        ),
    )


def _probe_result(
    backend: str,
    compiler: str,
    path: Path,
    completed: subprocess.CompletedProcess[str],
    elapsed_ms: float,
) -> DiagnosticProbe:
    text = completed.stderr + completed.stdout
    match = re.search(re.escape(str(path)) + r":(\d+)", text)
    return DiagnosticProbe(
        backend,
        compiler,
        round(elapsed_ms, 3),
        completed.returncode != 0,
        int(match.group(1)) if match is not None else None,
        "zlang_missing_symbol" in text,
        False,
    )


def _loc(text: str) -> int:
    return sum(
        bool(line.strip()) and not line.lstrip().startswith(("//", "--", "/*", "**"))
        for line in text.splitlines()
    )


def _version(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise BackendComparisonError(f"could not query version for {command[0]}")
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0]


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000.0


def _quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _testbench(module: str) -> str:
    try:
        return _TESTBENCHES[module]
    except KeyError as error:
        raise BackendComparisonError(f"no behavior testbench for {module}") from error


_TESTBENCHES = {
    "ALU": """module tb;
  logic [31:0] a, b; logic [2:0] op; wire [31:0] y;
  ALU dut(.a(a), .b(b), .op(op), .y(y));
  initial begin
    a=32'hffffffff; b=1; op=0; #1; if (y !== 0) $fatal(1);
    a=7; b=5; op=1; #1; if (y !== 2) $fatal(1);
    a=32'hf0; b=32'hcc; op=2; #1; if (y !== 32'hc0) $fatal(1);
    op=3; #1; if (y !== 32'hfc) $fatal(1);
    op=7; #1; if (y !== 0) $fatal(1); $finish;
  end
endmodule
""",
    "PipelinedMAC": """module tb;
  logic clk=0, rst; logic [7:0] a,b; logic [15:0] c; wire [16:0] y;
  PipelinedMAC dut(.*); always #1 clk=~clk;
  task tick; begin @(posedge clk); #1; end endtask
  initial begin
    rst=1; a=0; b=0; c=0; tick(); if(y!==0)$fatal(1);
    rst=0; a=2; b=3; c=4; tick(); if(y!==0)$fatal(1);
    a=5; b=6; c=7; tick(); if(y!==10)$fatal(1); $finish;
  end
endmodule
""",
    "RvPassthrough": """module tb;
  logic [7:0] rx_payload; logic rx_valid,tx_ready;
  wire rx_ready; wire [7:0] tx_payload; wire tx_valid;
  RvPassthrough dut(.*);
  initial begin
    rx_payload=8'h5a; rx_valid=0; tx_ready=0; #1;
    if(tx_payload!==8'h5a || tx_valid!==0 || rx_ready!==0)$fatal(1);
    rx_valid=1; #1; if(tx_valid!==1 || rx_ready!==0)$fatal(1);
    tx_ready=1; #1; if(!tx_valid || !rx_ready || tx_payload!==8'h5a)$fatal(1);
    $finish;
  end
endmodule
""",
    "CreditSource": """module tb;
  logic clk=0,rst; logic [7:0] payload_data; logic request,tx_return;
  wire [7:0] tx_payload; wire tx_send; CreditSource dut(.*); always #1 clk=~clk;
  task tick; begin @(posedge clk); #1; end endtask
  initial begin
    rst=1;payload_data=8'h55;request=1;tx_return=0;tick();if(tx_send)$fatal(1);
    rst=0;#0;if(!tx_send||tx_payload!==8'h55)$fatal(1);tick();if(!tx_send)$fatal(1);
    tick();if(tx_send)$fatal(1);request=0;tx_return=1;tick();
    tx_return=0;request=1;#1;if(!tx_send)$fatal(1);
    request=0;tx_return=1;tick();if(tx_send)$fatal(1);$finish;
  end
endmodule
""",
    "ControlCsr": """module tb;
  logic clk=0,rst; logic [31:0] addr,wdata; logic write,read;
  wire [31:0] rdata; wire ready; ControlCsr dut(.*); always #1 clk=~clk;
  task tick; begin @(posedge clk); #1; end endtask
  initial begin
    rst=1;addr=0;wdata=0;write=0;read=0;tick();rst=0;
    addr=32'h40000004;read=1;#1;if(!ready||rdata!==3)$fatal(1);
    read=0;write=1;addr=32'h40000000;wdata=32'hbb;tick();
    write=0;read=1;#1;if(rdata!==32'hb)$fatal(1);
    read=0;write=1;addr=32'h40000004;wdata=2;tick();
    write=0;read=1;#1;if(rdata!==1)$fatal(1);
    addr=32'h40000008;#1;if(ready||rdata!==0)$fatal(1);
    read=0;#1;if(ready)$fatal(1);$finish;
  end
endmodule
""",
    "RequestClient": """module tb;
  logic clk=0,rst;
  logic [1:0] request_payload_id,mem_response_payload_id;
  logic [7:0] request_payload_data,mem_response_payload_data;
  logic issue,accept_response,mem_request_ready,mem_response_valid;
  wire [1:0] response_payload_id,mem_request_payload_id;
  wire [7:0] response_payload_data,mem_request_payload_data;
  wire mem_request_valid,mem_response_ready; RequestClient dut(.*); always #1 clk=~clk;
  task tick; begin @(posedge clk); #1; end endtask
  initial begin
    rst=1;request_payload_id=2'd1;request_payload_data=8'h11;issue=1;accept_response=0;
    mem_request_ready=1;mem_response_payload_id=0;mem_response_payload_data=0;mem_response_valid=0;
    tick();if(mem_request_valid)$fatal(1);rst=0;#0;if(!mem_request_valid)$fatal(1);
    tick();mem_request_ready=0;#1;if(!mem_request_valid)$fatal(1);
    mem_request_ready=1;#1;if(mem_request_valid)$fatal(1);
    issue=0;accept_response=1;mem_response_payload_id=2'd2;mem_response_payload_data=8'hbb;
    mem_response_valid=0;#1;if(!mem_response_ready)$fatal(1);
    mem_response_valid=1;#1;if(mem_response_ready)$fatal(1);
    mem_response_payload_id=2'd1;mem_response_payload_data=8'haa;#1;
    if(!mem_response_ready||response_payload_id!==2'd1||response_payload_data!==8'haa)$fatal(1);tick();
    mem_response_payload_id=2'd2;mem_response_payload_data=8'hbb;#1;
    if(mem_response_ready)$fatal(1);$finish;
  end
endmodule
""",
    "RuleCounter": """module tb;
  logic clk=0,rst,increment,clear; wire [7:0] count_out;
  RuleCounter dut(.*); always #1 clk=~clk;
  task tick; begin @(posedge clk); #1; end endtask
  initial begin
    rst=1;increment=0;clear=0;tick();if(count_out!==0)$fatal(1);rst=0;
    increment=1;tick();if(count_out!==1)$fatal(1);
    increment=1;clear=1;tick();if(count_out!==0)$fatal(1);
    increment=0;clear=0;tick();if(count_out!==0)$fatal(1);$finish;
  end
endmodule
""",
}


if __name__ == "__main__":
    raise SystemExit(main())
