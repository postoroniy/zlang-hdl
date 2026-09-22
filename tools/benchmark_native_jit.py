"""Record the post-boundary native-simulation performance baseline.

This runner deliberately changes no compiler or runtime policy. It measures
the semantic oracle, open primitive-plan reference executor, native JIT and
Direct-SV/Verilator paths at a stable set of compiler witnesses. Timing fields
are observations; identities, case definitions and support outcomes are the
reproducible part of the report.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import create_file_compilation_session
from zlang.sim import Program
from zlang.simulation_reference import compile_reference_plan
from zlang.simulate import simulate, simulate_cycles, simulate_multiclock_steps
from zlang.simulation_plan import JitUnsupportedFeatureError, build_simulation_plan


ROOT = Path(__file__).resolve().parents[1]
REPORT_SCHEMA = "zlang-native-simulation-performance-baseline-v3"
BASELINE = "native-runtime-boundary-v1"
CaseKind = Literal["combinational", "single_clock", "multi_clock"]


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    source: Path
    top: str
    kind: CaseKind
    inputs: dict[str, object]
    clocks: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityProbe:
    name: str
    source: Path
    top: str


CASES = (
    BenchmarkCase(
        "counter",
        ROOT / "examples/counter.zhl",
        "Counter",
        "single_clock",
        {},
        ("clk",),
    ),
    BenchmarkCase(
        "fsm_rules",
        ROOT / "benchmarks/native_jit/fsm_rules.zhl",
        "NativeJitFsmRulesBenchmark",
        "single_clock",
        {"start": 1, "advance": 1, "cancel": 0, "finish": 0},
        ("clk",),
    ),
    BenchmarkCase(
        "large_combinational_dag",
        ROOT / "benchmarks/native_jit/large_combinational.zhl",
        "NativeJitLargeCombinationalBenchmark",
        "combinational",
        {"seed": 0x1234_5678},
    ),
    BenchmarkCase(
        "fixed_pipeline",
        ROOT / "examples/general_expression_pipeline.zhl",
        "GeneralExpressionPipeline",
        "single_clock",
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6},
        ("clk",),
    ),
    BenchmarkCase(
        "sync_memory",
        ROOT / "examples/sync_memory.zhl",
        "SyncMemory",
        "single_clock",
        {
            "read_address": 0,
            "write_enable": 1,
            "write_address": 0,
            "write_data": 1,
        },
        ("clk",),
    ),
    BenchmarkCase(
        "multi_clock",
        ROOT / "examples/multi_clock_stateful.zhl",
        "MultiClockStateful",
        "multi_clock",
        {"control_enable": 1},
        ("control_clk", "datapath_clk"),
    ),
)

PROBES = (
    CapabilityProbe(
        "fft512",
        ROOT / "examples/fft/sdf_stage_numeric.zhl",
        "FFT512SDFReference",
    ),
    CapabilityProbe("simple_dma", ROOT / "examples/simple_dma.zhl", "SimpleDMA"),
    CapabilityProbe(
        "80211a_tx",
        ROOT / "examples/projects/80211a_transmitter/src/transmitter.zhl",
        "Ieee80211aTransmitter",
    ),
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool_version(command: str, *arguments: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    completed = subprocess.run(
        (executable, *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else None


def _median(samples: list[float]) -> float:
    return round(statistics.median(samples), 9)


def _measure(function, samples: int) -> tuple[float, object]:
    elapsed = []
    result = None
    for _ in range(samples):
        started = perf_counter()
        result = function()
        elapsed.append(perf_counter() - started)
    return _median(elapsed), result


def _set_inputs(simulator, inputs: dict[str, object]) -> None:
    for name, value in inputs.items():
        simulator.set(name, value)


def _semantic_reference_once(
    case: BenchmarkCase, module: object, count: int
) -> object:
    if case.kind == "combinational":
        result = None
        for _ in range(count):
            result = simulate(module, **case.inputs)
        return result
    if case.kind == "single_clock":
        # The historical semantic oracle reports the pre-edge output for each
        # supplied cycle.  Add one observation cycle so its final value is the
        # same post-edge state returned by the persistent plan executors after
        # ``count`` edges.
        return simulate_cycles(module, [case.inputs] * (count + 1))[-1]
    return simulate_multiclock_steps(
        module,
        [case.inputs] * (count + 1),
        [set(case.clocks)] * (count + 1),
    )[-1]


def _plan_executor_once(
    case: BenchmarkCase, program: Program, count: int
) -> dict[str, object]:
    with program.create() as simulator:
        _set_inputs(simulator, case.inputs)
        if case.kind == "combinational":
            simulator._native.run_events(  # noqa: SLF001 - benchmark the VM boundary
                [([], [], [])] * count
            )
        elif case.kind == "single_clock":
            simulator._native.run_cycles(case.clocks[0], count)  # noqa: SLF001
        else:
            simulator._native.run_events(  # noqa: SLF001 - benchmark the VM boundary
                [([], [], list(case.clocks))] * count
            )
        return simulator.outputs()


def _cpp_value(value: object) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) and 0 <= value < 1 << 64:
        return str(value)
    return None


def _verilator_harness(case: BenchmarkCase, module: object) -> str:
    top = case.top
    input_assignments = []
    for name, value in sorted(case.inputs.items()):
        encoded = _cpp_value(value)
        if encoded is not None:
            input_assignments.append(f"  design.{name} = {encoded};")
    reset_levels: dict[str, tuple[int, int]] = {}
    for domain in module.clock_domains:
        if domain.reset is None:
            continue
        active_low = domain.reset_polarity.value == "active_low"
        levels = (0, 1) if active_low else (1, 0)
        previous = reset_levels.setdefault(domain.reset, levels)
        if previous != levels:
            raise ValueError(
                f"reset {domain.reset!r} has inconsistent polarity across domains"
            )
    reset_assert = [
        f"  design.{name} = {levels[0]};"
        for name, levels in sorted(reset_levels.items())
    ]
    reset_release = [
        f"  design.{name} = {levels[1]};"
        for name, levels in sorted(reset_levels.items())
    ]
    clocks_low = [f"    design.{name} = 0;" for name in case.clocks]
    clocks_high = [f"    design.{name} = 1;" for name in case.clocks]
    if case.kind == "combinational":
        body = ["    design.eval();"]
        reset_cycle: list[str] = []
    else:
        body = [*clocks_low, "    design.eval();", *clocks_high, "    design.eval();"]
        reset_cycle = [
            *[line.replace("    ", "  ", 1) for line in clocks_low],
            "  design.eval();",
            *[line.replace("    ", "  ", 1) for line in clocks_high],
            "  design.eval();",
        ]
    output_lines = [
        f'  std::cout << "output {port.name} " << '
        f'static_cast<unsigned long long>(design.{port.name}) << "\\n";'
        for port in module.outputs
        if port.type.width <= 64
    ]
    lines = [
        f'#include "V{top}.h"',
        "#include <chrono>",
        "#include <cstdlib>",
        "#include <iostream>",
        "#include <verilated.h>",
        "int main(int argc, char** argv) {",
        "  Verilated::commandArgs(argc, argv);",
        "  Verilated::randReset(0);",
        "  const unsigned long long count = std::strtoull(argv[1], nullptr, 10);",
        f"  V{top} design;",
        *input_assignments,
        *reset_assert,
        *reset_cycle,
        *reset_release,
        "  const auto started = std::chrono::steady_clock::now();",
        "  for (unsigned long long index = 0; index < count; ++index) {",
        *body,
        "  }",
        "  const auto finished = std::chrono::steady_clock::now();",
        "  const std::chrono::duration<double> elapsed = finished - started;",
        '  std::cout << "seconds " << elapsed.count() << "\\n";',
        *output_lines,
        "  return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _emit_direct_sv_worker(
    module: object,
    output: Path,
    error_output: Path,
    memory_limit_mib: int,
) -> None:
    """Emit RTL in a bounded child so a QoR probe cannot exhaust the host."""

    import resource

    memory_bytes = memory_limit_mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    try:
        output.write_text(emit_experimental(module), encoding="utf-8")
    except BaseException as error:  # pragma: no cover - child failure transport
        error_output.write_text(
            f"{type(error).__name__}: {error}", encoding="utf-8"
        )
        raise


def _emit_direct_sv_bounded(
    module: object,
    output: Path,
    *,
    timeout_seconds: int,
    memory_limit_mib: int,
) -> dict[str, object]:
    context = multiprocessing.get_context("fork")
    error_output = output.with_suffix(".error.txt")
    started = perf_counter()
    process = context.Process(
        target=_emit_direct_sv_worker,
        args=(module, output, error_output, memory_limit_mib),
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join()
        return {
            "status": "timeout",
            "seconds": round(perf_counter() - started, 9),
            "reason": f"direct-SV emission exceeded {timeout_seconds} seconds",
        }
    if process.exitcode != 0 or not output.is_file():
        detail = (
            error_output.read_text(encoding="utf-8")
            if error_output.is_file()
            else f"emitter child exited with status {process.exitcode}"
        )
        return {
            "status": "failed",
            "seconds": round(perf_counter() - started, 9),
            "reason": detail,
        }
    return {
        "status": "ready",
        "seconds": round(perf_counter() - started, 9),
    }


def _build_verilator(
    case: BenchmarkCase,
    module: object,
    directory: Path,
    *,
    timeout_seconds: int,
    emission_timeout_seconds: int,
    emission_memory_limit_mib: int,
) -> dict[str, object]:
    if shutil.which("verilator") is None:
        return {"status": "unavailable", "reason": "verilator not found"}
    rtl = directory / f"{case.top}.sv"
    harness = directory / "benchmark.cpp"
    object_dir = directory / "obj"
    emission = _emit_direct_sv_bounded(
        module,
        rtl,
        timeout_seconds=emission_timeout_seconds,
        memory_limit_mib=emission_memory_limit_mib,
    )
    if emission["status"] != "ready":
        return {
            "status": f"emission_{emission['status']}",
            "reason": emission["reason"],
            "rtl_emission_seconds": emission["seconds"],
            "rtl_emission_memory_limit_mib": emission_memory_limit_mib,
        }
    harness.write_text(_verilator_harness(case, module), encoding="utf-8")
    rtl_text = rtl.read_text(encoding="utf-8")
    structural_metrics = {
        "rtl_bytes": len(rtl_text.encode("utf-8")),
        "rtl_max_line_bytes": max(
            (len(line.encode("utf-8")) for line in rtl_text.splitlines()),
            default=0,
        ),
        "rtl_emission_seconds": emission["seconds"],
        "rtl_emission_memory_limit_mib": emission_memory_limit_mib,
    }
    environment = {**os.environ, "CCACHE_DISABLE": "1"}
    started = perf_counter()
    process = subprocess.Popen(
        (
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "--top-module",
            case.top,
            "--Mdir",
            str(object_dir),
            "-o",
            "benchmark",
            "-CFLAGS",
            "-O3",
            str(rtl),
            str(harness),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return {
            "status": "timeout",
            "reason": f"Verilator build exceeded {timeout_seconds} seconds",
            "compile_seconds": round(perf_counter() - started, 9),
            **structural_metrics,
        }
    if process.returncode != 0:
        return {
            "status": "failed",
            "reason": (stderr or stdout)[-2000:],
            "compile_seconds": round(perf_counter() - started, 9),
            **structural_metrics,
        }
    return {
        "status": "ready",
        "compile_seconds": round(perf_counter() - started, 9),
        "executable": str(object_dir / "benchmark"),
        **structural_metrics,
    }


def _run_verilator(executable: Path, count: int) -> dict[str, object]:
    completed = subprocess.run(
        (str(executable), str(count)),
        check=True,
        capture_output=True,
        text=True,
    )
    fields: dict[str, object] = {}
    seconds = None
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "seconds":
            seconds = float(parts[1])
        elif len(parts) == 3 and parts[0] == "output":
            fields[parts[1]] = int(parts[2])
    if seconds is None or seconds <= 0:
        raise RuntimeError("Verilator benchmark did not report positive elapsed time")
    return {
        "seconds": round(seconds, 9),
        "items_per_second": round(count / seconds, 3),
        "final_outputs": fields,
    }


def _benchmark_case(
    case: BenchmarkCase,
    *,
    batch_sizes: tuple[int, ...],
    samples: int,
    engines: frozenset[str],
    semantic_reference_max_items: int,
    plan_reference_max_items: int,
    verilator_build_timeout: int,
    rtl_emission_timeout: int,
    rtl_emission_memory_limit_mib: int,
) -> dict[str, object]:
    session_started = perf_counter()
    session = create_file_compilation_session(case.source, top=case.top)
    module = session.planning.module
    planning_seconds = perf_counter() - session_started

    lowering_seconds, plan = _measure(lambda: build_simulation_plan(module), samples)
    assert plan is not None
    serialization_seconds, serialized = _measure(
        lambda: _canonical_json(plan.payload), samples
    )
    assert serialized == plan.to_bytes()

    result: dict[str, object] = {
        "name": case.name,
        "status": "measured",
        "source": str(case.source.relative_to(ROOT)),
        "source_sha256": _sha256(case.source),
        "top": case.top,
        "kind": case.kind,
        "clocks": list(case.clocks),
        "plan_identity": plan.identity,
        "plan_bytes": len(plan.to_bytes()),
        "primitive_nodes": len(plan.payload["nodes"]),
        "planning_seconds": round(planning_seconds, 9),
        "plan_lowering_seconds_median": lowering_seconds,
        "serialization_seconds_median": serialization_seconds,
        "measurements": {},
    }
    measurements = result["measurements"]
    assert isinstance(measurements, dict)

    if "semantic_reference" in engines:
        semantic_reference = {}
        for count in batch_sizes:
            if count > semantic_reference_max_items:
                semantic_reference[str(count)] = {
                    "status": "omitted",
                    "reason": (
                        "count exceeds semantic-reference control-workload limit "
                        f"{semantic_reference_max_items}"
                    ),
                }
                continue
            seconds, final = _measure(
                lambda count=count: _semantic_reference_once(case, module, count),
                samples,
            )
            semantic_reference[str(count)] = {
                "seconds_median": seconds,
                "items_per_second": round(count / seconds, 3),
                "final_outputs": final,
            }
        measurements["semantic_reference"] = semantic_reference

    if "plan_reference" in engines:
        reference_compile_seconds, reference = _measure(
            lambda: compile_reference_plan(plan), samples
        )
        assert reference is not None
        reference_program = Program(plan=plan, module=module, _native=reference)
        reference_instance_seconds, reference_instance = _measure(
            reference_program.create, samples
        )
        assert reference_instance is not None
        reference_instance.close()
        plan_reference = {
            "compile_seconds_median": reference_compile_seconds,
            "instance_create_seconds_median": reference_instance_seconds,
            "measurements": {},
        }
        for count in batch_sizes:
            if count > plan_reference_max_items:
                plan_reference["measurements"][str(count)] = {
                    "status": "omitted",
                    "reason": (
                        "count exceeds plan-reference workload limit "
                        f"{plan_reference_max_items}"
                    ),
                }
                continue
            seconds, final = _measure(
                lambda count=count: _plan_executor_once(
                    case, reference_program, count
                ),
                samples,
            )
            plan_reference["measurements"][str(count)] = {
                "seconds_median": seconds,
                "items_per_second": round(count / seconds, 3),
                "final_outputs": final,
            }
        measurements["plan_reference"] = plan_reference

    if "native" in engines:
        import _zlang_native_sim

        compile_seconds, native = _measure(
            lambda: _zlang_native_sim.compile_plan_bytes(plan.to_bytes()), samples
        )
        assert native is not None
        program = Program(plan=plan, module=module, _native=native)
        instance_seconds, instance = _measure(program.create, samples)
        assert instance is not None
        with instance:
            _set_inputs(instance, case.inputs)
            first_started = perf_counter()
            if case.kind == "combinational":
                instance._native.eval()  # noqa: SLF001
            elif case.kind == "single_clock":
                instance._native.run_cycles(case.clocks[0], 1)  # noqa: SLF001
            else:
                instance._native.edge_many(list(case.clocks))  # noqa: SLF001
            first_event_seconds = perf_counter() - first_started
        native_measurement = {
            "cranelift_compile_seconds_median": compile_seconds,
            "instance_create_seconds_median": instance_seconds,
            "first_event_seconds": round(first_event_seconds, 9),
            "measurements": {},
        }
        for count in batch_sizes:
            seconds, final = _measure(
                lambda count=count: _plan_executor_once(case, program, count),
                samples,
            )
            native_measurement["measurements"][str(count)] = {
                "seconds_median": seconds,
                "items_per_second": round(count / seconds, 3),
                "final_outputs": final,
                "mode": (
                    "native_run_cycles"
                    if case.kind == "single_clock"
                    else "native_run_events"
                ),
            }
        measurements["native"] = native_measurement

    if "verilator" in engines:
        with TemporaryDirectory(prefix=f"zlang-jit-p0-{case.name}-") as temporary:
            verilator = _build_verilator(
                case,
                module,
                Path(temporary),
                timeout_seconds=verilator_build_timeout,
                emission_timeout_seconds=rtl_emission_timeout,
                emission_memory_limit_mib=rtl_emission_memory_limit_mib,
            )
            if verilator["status"] == "ready":
                executable = Path(str(verilator.pop("executable")))
                verilator["measurements"] = {
                    str(count): _run_verilator(executable, count)
                    for count in batch_sizes
                }
            measurements["verilator"] = verilator
    return result


def _probe(probe: CapabilityProbe) -> dict[str, object]:
    started = perf_counter()
    try:
        session = create_file_compilation_session(probe.source, top=probe.top)
        plan = session.simulation_plan
    except JitUnsupportedFeatureError as error:
        return {
            "name": probe.name,
            "status": "unsupported",
            "source": str(probe.source.relative_to(ROOT)),
            "top": probe.top,
            "seconds": round(perf_counter() - started, 9),
            "reason": str(error),
        }
    return {
        "name": probe.name,
        "status": "supported",
        "source": str(probe.source.relative_to(ROOT)),
        "top": probe.top,
        "seconds": round(perf_counter() - started, 9),
        "plan_identity": plan.identity,
    }


def _environment() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "zlang_hdl": importlib.metadata.version("zlang-hdl"),
        "rustc": _tool_version("rustc", "--version"),
        "verilator": _tool_version("verilator", "--version"),
    }


def _parse_positive(values: list[str]) -> tuple[int, ...]:
    parsed = tuple(int(value) for value in values)
    if (
        not parsed
        or any(value < 1 for value in parsed)
        or len(set(parsed)) != len(parsed)
    ):
        raise argparse.ArgumentTypeError("batch sizes must be unique positive integers")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="*", choices=[case.name for case in CASES])
    parser.add_argument(
        "--batch-sizes", nargs="+", default=["100", "1000", "100000", "1000000"]
    )
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--semantic-reference-max-items", type=int, default=100)
    parser.add_argument("--plan-reference-max-items", type=int, default=100000)
    parser.add_argument("--verilator-build-timeout", type=int, default=120)
    parser.add_argument("--rtl-emission-timeout", type=int, default=120)
    parser.add_argument("--rtl-emission-memory-limit-mib", type=int, default=2048)
    parser.add_argument(
        "--engines",
        nargs="+",
        choices=(
            "semantic_reference",
            "plan_reference",
            "native",
            "verilator",
        ),
        default=(
            "semantic_reference",
            "plan_reference",
            "native",
            "verilator",
        ),
    )
    parser.add_argument("--skip-real-design-probes", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.samples < 1:
        parser.error("--samples must be positive")
    if arguments.semantic_reference_max_items < 1:
        parser.error("--semantic-reference-max-items must be positive")
    if arguments.plan_reference_max_items < 1:
        parser.error("--plan-reference-max-items must be positive")
    if arguments.verilator_build_timeout < 1:
        parser.error("--verilator-build-timeout must be positive")
    if arguments.rtl_emission_timeout < 1:
        parser.error("--rtl-emission-timeout must be positive")
    if arguments.rtl_emission_memory_limit_mib < 256:
        parser.error("--rtl-emission-memory-limit-mib must be at least 256")
    try:
        batch_sizes = _parse_positive(arguments.batch_sizes)
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    selected = [
        case for case in CASES if not arguments.cases or case.name in arguments.cases
    ]
    report = {
        "schema": REPORT_SCHEMA,
        "baseline": BASELINE,
        "simulation_plan_schema": "zlang-simulation-plan-v8",
        "runtime_abi": "zlang-native-simulation-abi-v8",
        "samples": arguments.samples,
        "batch_sizes": list(batch_sizes),
        "semantic_reference_max_items": arguments.semantic_reference_max_items,
        "plan_reference_max_items": arguments.plan_reference_max_items,
        "verilator_build_timeout": arguments.verilator_build_timeout,
        "rtl_emission_timeout": arguments.rtl_emission_timeout,
        "rtl_emission_memory_limit_mib": arguments.rtl_emission_memory_limit_mib,
        "engines": list(arguments.engines),
        "environment": _environment(),
        "cases": [
            _benchmark_case(
                case,
                batch_sizes=batch_sizes,
                samples=arguments.samples,
                engines=frozenset(arguments.engines),
                semantic_reference_max_items=(
                    arguments.semantic_reference_max_items
                ),
                plan_reference_max_items=arguments.plan_reference_max_items,
                verilator_build_timeout=arguments.verilator_build_timeout,
                rtl_emission_timeout=arguments.rtl_emission_timeout,
                rtl_emission_memory_limit_mib=(
                    arguments.rtl_emission_memory_limit_mib
                ),
            )
            for case in selected
        ],
        "capability_probes": (
            []
            if arguments.skip_real_design_probes
            else [_probe(probe) for probe in PROBES]
        ),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = arguments.output.with_suffix(arguments.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(arguments.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
