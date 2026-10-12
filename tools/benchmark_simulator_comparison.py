#!/usr/bin/env python3
"""Compare equivalent Amaranth and ZLang native simulation microbenchmarks.

This is a host-specific microbenchmark over a few selected steady-state
workloads, not a general claim that ZLang simulation is faster than Amaranth.
Construction is reported separately from execution.  Amaranth execution uses
``Simulator.run_until()`` and therefore includes its scheduler; ZLang execution
calls the native engine's batched ``run_cycles()`` directly and bypasses the
JSONL/CLI layers.  The benchmark is observational, not a release gate: absolute
wall time varies by machine and Python build.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import statistics
from time import perf_counter
from typing import Mapping

import zlang
from zlang.compilation_session import CompilationSession
from zlang.sim import Program


REPORT_SCHEMA = "zlang-amaranth-simulator-comparison-v1"


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    description: str
    zlang_source: str
    zlang_top: str
    amaranth_factory: Callable[[], object]
    inputs: Mapping[str, int]
    output_name: str
    expected: Callable[[int], int]


def _amaranth_counter():
    from amaranth import Elaboratable, Module, Signal

    class Counter(Elaboratable):
        def __init__(self) -> None:
            self.count = Signal(32)
            self.output = self.count

        def elaborate(self, platform):
            del platform
            module = Module()
            module.d.sync += self.count.eq(self.count + 1)
            return module

    return Counter()


def _amaranth_pipeline():
    from amaranth import Elaboratable, Module, Signal

    class Pipeline(Elaboratable):
        def __init__(self) -> None:
            self.a = Signal(8, init=3)
            self.b = Signal(8, init=5)
            self.c = Signal(8, init=7)
            self.d = Signal(8, init=11)
            self.e = Signal(8, init=13)
            self.f = Signal(25, init=17)
            self.product_sum = Signal(17)
            self.scaled = Signal(25)
            self.result = Signal(26)
            self.output = self.result

        def elaborate(self, platform):
            del platform
            module = Module()
            module.d.sync += [
                self.product_sum.eq(self.a * self.b + self.c * self.d),
                self.scaled.eq(self.product_sum * self.e),
                self.result.eq(self.scaled + self.f),
            ]
            return module

    return Pipeline()


def _amaranth_state_bank():
    from amaranth import Elaboratable, Module, Signal

    class StateBank(Elaboratable):
        def __init__(self) -> None:
            self.state = tuple(Signal(32, init=index) for index in range(16))
            self.result = Signal(32)
            self.output = self.result

        def elaborate(self, platform):
            del platform
            module = Module()
            for index, state in enumerate(self.state):
                shift = index % 7 + 1
                constant = 0x9E37_79B9 + index * 0x0101_0101
                module.d.sync += state.eq((state + constant) ^ (state >> shift))
            combined = self.state[0]
            for state in self.state[1:]:
                combined = combined ^ state
            module.d.comb += self.result.eq(combined)
            return module

    return StateBank()


def _state_bank_source() -> str:
    declarations = "\n".join(
        f"  reg state_{index:02d}:u32={index}"
        for index in range(16)
    )
    updates = "\n".join(
        "  state_{index:02d} <- truncate<32>((state_{index:02d} + "
        "{constant}) ^ (state_{index:02d} >> {shift}))".format(
            index=index,
            constant=0x9E37_79B9 + index * 0x0101_0101,
            shift=index % 7 + 1,
        )
        for index in range(16)
    )
    reduction = " ^ ".join(f"state_{index:02d}" for index in range(16))
    return (
        "module SimulatorStateBank {\n"
        "  clock clk reset rst\n"
        f"{declarations}\n"
        "  out result:u32\n"
        f"  result={reduction}\n"
        f"{updates}\n"
        "}\n"
    )


def _state_bank_expected(cycles: int) -> int:
    mask = (1 << 32) - 1
    state = list(range(16))
    for _ in range(cycles):
        state = [
            ((value + 0x9E37_79B9 + index * 0x0101_0101) & mask)
            ^ (value >> (index % 7 + 1))
            for index, value in enumerate(state)
        ]
    result = 0
    for value in state:
        result ^= value
    return result


CASES = (
    BenchmarkCase(
        "counter_u32",
        "one 32-bit register increment per cycle",
        """
        module SimulatorCounter {
          clock clk reset rst
          reg count:u32=0
          out y:u32=count
          count <- truncate<32>(count + 1)
        }
        """,
        "SimulatorCounter",
        _amaranth_counter,
        {},
        "y",
        lambda cycles: cycles & 0xFFFF_FFFF,
    ),
    BenchmarkCase(
        "pipeline_muladd",
        "three registered multiply/add stages",
        """
        module SimulatorPipeline {
          clock clk reset rst
          in a,b,c,d,e:u8 in f:u25
          out result:u26
          result = pipeline(3) { (a*b + c*d)*e + f }
        }
        """,
        "SimulatorPipeline",
        _amaranth_pipeline,
        {"a": 3, "b": 5, "c": 7, "d": 11, "e": 13, "f": 17},
        "result",
        lambda cycles: 1213 if cycles >= 3 else 0,
    ),
    BenchmarkCase(
        "state_bank_16",
        "sixteen 32-bit state updates and one XOR reduction",
        _state_bank_source(),
        "SimulatorStateBank",
        _amaranth_state_bank,
        {},
        "result",
        _state_bank_expected,
    ),
)


def _median(samples: list[float]) -> float:
    return statistics.median(samples)


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def _measure_amaranth(case: BenchmarkCase, cycles: int, samples: int) -> dict[str, float]:
    try:
        from amaranth.sim import Simulator
    except ImportError as error:
        raise RuntimeError(
            "Amaranth is unavailable; install the pinned comparison dependency "
            "with '.venv/bin/python -m pip install amaranth==0.5.10'"
        ) from error

    construction: list[float] = []
    execution: list[float] = []
    for _ in range(samples):
        started = perf_counter()
        simulator = Simulator(case.amaranth_factory())
        simulator.add_clock(1e-6)
        construction.append(perf_counter() - started)
        started = perf_counter()
        simulator.run_until(cycles * 1e-6)
        execution.append(perf_counter() - started)
    seconds = _median(execution)
    return {
        "construction_seconds_median": _median(construction),
        "execution_seconds_median": seconds,
        "cycles_per_second": cycles / seconds,
    }


def _build_zlang(case: BenchmarkCase) -> Program:
    import _zlang_native_sim

    session = CompilationSession(case.zlang_source, top=case.zlang_top)
    plan = session.simulation_plan
    native = _zlang_native_sim.compile_plan_bytes(plan.to_bytes())
    return Program(plan=plan, module=session.planning.module, _native=native)


def _measure_zlang(case: BenchmarkCase, cycles: int, samples: int) -> dict[str, float]:
    construction: list[float] = []
    for _ in range(samples):
        started = perf_counter()
        program = _build_zlang(case)
        construction.append(perf_counter() - started)

    program = _build_zlang(case)
    execution: list[float] = []
    for _ in range(samples):
        with program.create() as simulator:
            for name, value in case.inputs.items():
                simulator.set(name, value)
            started = perf_counter()
            simulator._native.run_cycles("clk", cycles)  # noqa: SLF001
            execution.append(perf_counter() - started)
    seconds = _median(execution)
    return {
        "construction_seconds_median": _median(construction),
        "execution_seconds_median": seconds,
        "cycles_per_second": cycles / seconds,
    }


def _validate(case: BenchmarkCase, cycles: int = 16) -> dict[str, int]:
    from amaranth.sim import Simulator

    design = case.amaranth_factory()
    amaranth_value: list[int] = []

    async def testbench(context) -> None:
        for _ in range(cycles):
            await context.tick()
        amaranth_value.append(context.get(design.output))

    simulator = Simulator(design)
    simulator.add_clock(1e-6)
    simulator.add_testbench(testbench)
    simulator.run()

    program = _build_zlang(case)
    with program.create() as instance:
        for name, value in case.inputs.items():
            instance.set(name, value)
        zlang_value = int(instance.run_cycles("clk", cycles)[case.output_name])
    expected = case.expected(cycles)
    if not amaranth_value or amaranth_value[0] != expected or zlang_value != expected:
        raise RuntimeError(
            f"{case.name} engines disagree after {cycles} cycles: "
            f"expected={expected}, amaranth={amaranth_value}, zlang={zlang_value}"
        )
    return {
        "cycles": cycles,
        "expected": expected,
        "amaranth": amaranth_value[0],
        "zlang_native": zlang_value,
    }


def benchmark(*, cycles: int, samples: int) -> dict[str, object]:
    cases = []
    for case in CASES:
        amaranth = _measure_amaranth(case, cycles, samples)
        zlang_native = _measure_zlang(case, cycles, samples)
        cases.append({
            "name": case.name,
            "description": case.description,
            "cycles": cycles,
            "samples": samples,
            "amaranth": amaranth,
            "zlang_native": zlang_native,
            "zlang_execution_speedup": (
                zlang_native["cycles_per_second"] / amaranth["cycles_per_second"]
            ),
            "validation": _validate(case),
        })
    return {
        "schema": REPORT_SCHEMA,
        "methodology": {
            "scope": "host-specific selected steady-state microbenchmarks",
            "release_gate": False,
            "amaranth_execution": "Simulator.run_until() including scheduler",
            "zlang_execution": "direct native run_cycles(), excluding CLI/JSONL",
            "construction_reported_separately": True,
        },
        "host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "versions": {
            "amaranth": _package_version("amaranth"),
            "zlang_source": zlang.__version__,
            "zlang_distribution_metadata": _package_version("zlang-hdl"),
            "zlang_native_sim": _package_version("zlang-native-sim"),
        },
        "cases": cases,
    }


def markdown(report: dict[str, object]) -> str:
    lines = [
        "This is a host-specific microbenchmark over selected steady-state workloads,",
        "not a general simulator-speed claim. Amaranth execution uses",
        "`Simulator.run_until()` (including scheduler overhead); ZLang uses",
        "the native engine's direct batched `run_cycles()` path and",
        "excludes CLI/JSONL overhead. Construction is reported separately.",
        "",
        "| Workload | Cycles | Amaranth Mcycles/s | ZLang Mcycles/s | ZLang speedup |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in report["cases"]:
        assert isinstance(item, dict)
        amaranth = item["amaranth"]
        zlang = item["zlang_native"]
        assert isinstance(amaranth, dict) and isinstance(zlang, dict)
        lines.append(
            f"| {item['name']} | {item['cycles']:,} | "
            f"{amaranth['cycles_per_second'] / 1_000_000:.3f} | "
            f"{zlang['cycles_per_second'] / 1_000_000:.3f} | "
            f"{item['zlang_execution_speedup']:.1f}× |"
        )
    lines.extend([
        "",
        "| Workload | Amaranth build ms | ZLang source→native ms |",
        "|---|---:|---:|",
    ])
    for item in report["cases"]:
        assert isinstance(item, dict)
        amaranth = item["amaranth"]
        zlang = item["zlang_native"]
        assert isinstance(amaranth, dict) and isinstance(zlang, dict)
        lines.append(
            f"| {item['name']} | "
            f"{amaranth['construction_seconds_median'] * 1000:.2f} | "
            f"{zlang['construction_seconds_median'] * 1000:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=100_000)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", action="store_true")
    arguments = parser.parse_args()
    if arguments.cycles < 1 or arguments.samples < 1:
        parser.error("cycles and samples must be positive")
    report = benchmark(cycles=arguments.cycles, samples=arguments.samples)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(markdown(report) if arguments.markdown else json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
