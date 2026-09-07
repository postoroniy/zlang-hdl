from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.opt import lower, restore
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()


def _source(collision: str) -> str:
    return f"""
module ZtpuAsyncMemory {{
    clock clk reset rst
    in read_address:u2 in write_enable:bit
    in write_address:u2 in write_data:u16 in write_mask:bits<2>
    out read_data:u16
    memory table:mem<u16,4> {{
        read_latency 0
        collision {collision}
        reset {{ contents preserve read_data preserve }}
    }}
    table.read_address=read_address
    table.write_enable=write_enable
    table.write_address=write_address
    table.write_data=write_data
    table.write_mask=write_mask
    read_data=table.read_data
}}
"""


def _cycles() -> list[dict[str, int]]:
    return [
        {"read_address": 1, "write_enable": 0, "write_address": 0,
         "write_data": 0, "write_mask": 0},
        {"read_address": 1, "write_enable": 1, "write_address": 1,
         "write_data": 0x1234, "write_mask": 3},
        {"read_address": 1, "write_enable": 0, "write_address": 0,
         "write_data": 0, "write_mask": 0},
        {"read_address": 1, "write_enable": 1, "write_address": 1,
         "write_data": 0xABCD, "write_mask": 1},
        {"read_address": 1, "write_enable": 0, "write_address": 0,
         "write_data": 0, "write_mask": 0},
        # Reset must suppress this write while retaining the old word.
        {"read_address": 1, "write_enable": 1, "write_address": 1,
         "write_data": 0xFFFF, "write_mask": 3},
        {"read_address": 1, "write_enable": 0, "write_address": 0,
         "write_data": 0, "write_mask": 0},
    ]


def test_ztpu_async_memory_semantic_trace_and_canonical_round_trip() -> None:
    expected = {
        "read_first": [0, 0, 0x1234, 0x1234, 0x12CD, 0x12CD, 0x12CD],
        "write_first": [0, 0x1234, 0x1234, 0x12CD, 0x12CD, 0x12CD, 0x12CD],
    }
    for collision, trace in expected.items():
        module = compile_source(_source(collision), include_clash=False).ir
        restored = restore(lower(module))
        assert restored.memories == module.memories
        assert [
            item["read_data"]
            for item in simulate_cycles(
                restored,
                _cycles(),
                reset=[True, False, False, False, False, True, False],
            )
        ] == trace


def _harness(collision: str) -> str:
    bypass_first = "0x1234" if collision == "write_first" else "0"
    bypass_masked = "0x12cd" if collision == "write_first" else "0x1234"
    return f'''\
#include "VZtpuAsyncMemory.h"
static void edge(VZtpuAsyncMemory &d) {{
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}}
int main() {{
  VZtpuAsyncMemory d;
  d.clk=0; d.rst=1; d.read_address=1; d.write_enable=0;
  d.write_address=0; d.write_data=0; d.write_mask=0; d.eval();
  if (d.read_data != 0) return 1;
  edge(d); d.rst=0;
  d.write_enable=1; d.write_address=1; d.write_data=0x1234;
  d.write_mask=3; d.eval();
  if (d.read_data != {bypass_first}) return 2;
  edge(d); if (d.read_data != 0x1234) return 3;
  d.write_data=0xabcd; d.write_mask=1; d.eval();
  if (d.read_data != {bypass_masked}) return 4;
  edge(d); if (d.read_data != 0x12cd) return 5;
  d.rst=1; d.write_data=0xffff; d.write_mask=3; d.eval();
  if (d.read_data != 0x12cd) return 6;
  edge(d); if (d.read_data != 0x12cd) return 7;
  d.rst=0; d.write_enable=0; d.eval();
  return d.read_data == 0x12cd ? 0 : 8;
}}
'''


def _run_verilator(files: tuple[Path, ...], root: Path, collision: str) -> None:
    harness = root / "harness.cpp"
    harness.write_text(_harness(collision))
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            "ZtpuAsyncMemory", "--Mdir", str(obj), "-o", "memory_sim",
            *map(str, files), str(harness),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run(
        (str(obj / "memory_sim"),),
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
@pytest.mark.parametrize("collision", ("read_first", "write_first"))
def test_ztpu_async_memory_direct_sv_lints_and_simulates(
    tmp_path: Path, collision: str,
) -> None:
    module = compile_source(_source(collision), include_clash=False).ir
    text = emit_experimental(module)
    assert emit_experimental(module) == text
    assert "initial begin" in text
    assert "Memory contents and read result hold across reset" in text
    rtl = tmp_path / "ZtpuAsyncMemory.sv"
    rtl.write_text(text)
    subprocess.run(
        ("verilator", "--lint-only", "-Wall", str(rtl)),
        check=True, capture_output=True, text=True,
    )
    _run_verilator((rtl,), tmp_path, collision)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
@pytest.mark.parametrize("collision", ("read_first", "write_first"))
def test_ztpu_async_memory_real_clash_is_cycle_identical(
    tmp_path: Path, collision: str,
) -> None:
    result = compile_source(_source(collision))
    files = generate_verilog(
        result.clash, "ZtpuAsyncMemory", tmp_path / "rtl", CLASH
    )
    _run_verilator(tuple(files), tmp_path, collision)


_RESET_PROFILE_MATRIX = (
    ("MemoryGlobalL0CC", 0, "clear", "clear", False),
    ("MemoryGlobalL0CP", 0, "clear", "preserve", False),
    ("MemoryGlobalL0PC", 0, "preserve", "clear", False),
    ("MemoryGlobalL0PP", 0, "preserve", "preserve", False),
    ("MemoryGlobalL1CC", 1, "clear", "clear", False),
    ("MemoryGlobalL1CP", 1, "clear", "preserve", False),
    ("MemoryGlobalL1PC", 1, "preserve", "clear", False),
    ("MemoryGlobalL1PP", 1, "preserve", "preserve", False),
    ("MemoryScheduledCP", 1, "clear", "preserve", True),
    ("MemoryScheduledPC", 1, "preserve", "clear", True),
)


def _reset_profile_source(
    name: str,
    latency: int,
    contents: str,
    read_data: str,
    scheduled: bool,
) -> str:
    read_port = "in read_enable:bit" if scheduled else ""
    controls = (
        "fetch: when read_enable { table.read(address) }\n"
        "    store: when write_enable { table.write(address,write_data) }"
        if scheduled
        else (
            "table.read_address=address\n"
            "    table.write_enable=write_enable\n"
            "    table.write_address=address\n"
            "    table.write_data=write_data"
        )
    )
    return f"""
module {name} {{
    clock clk reset rst
    in address:u2 {read_port} in write_enable:bit in write_data:u8
    out q:u8
    memory table:mem<u8,4> {{
        read_latency {latency}
        collision write_first
        reset {{ contents {contents} read_data {read_data} }}
    }}
    {controls}
    q=table.read_data
}}
"""


def _reset_profile_cycles(*, scheduled: bool) -> list[dict[str, int]]:
    cycles = [
        # Reset must suppress this attempted initial read/write action.
        {"address": 1, "read_enable": 1, "write_enable": 1,
         "write_data": 0x11},
        {"address": 1, "read_enable": 1, "write_enable": 1,
         "write_data": 0x5A},
        {"address": 1, "read_enable": 0, "write_enable": 0,
         "write_data": 0},
        {"address": 1, "read_enable": 0, "write_enable": 0,
         "write_data": 0},
        # A second reset distinguishes cell and registered-result policy and
        # must suppress another attempted read/write action.
        {"address": 1, "read_enable": 1, "write_enable": 1,
         "write_data": 0xA5},
        {"address": 1, "read_enable": 1, "write_enable": 0,
         "write_data": 0},
        {"address": 1, "read_enable": 0, "write_enable": 0,
         "write_data": 0},
    ]
    if scheduled:
        return cycles
    return [
        {key: value for key, value in cycle.items() if key != "read_enable"}
        for cycle in cycles
    ]


def _semantic_reset_profile_traces() -> dict[str, tuple[int, ...]]:
    expected_by_profile = {
        (0, "clear", "clear"): (0, 0x5A, 0x5A, 0x5A, 0, 0, 0),
        (0, "clear", "preserve"): (0, 0x5A, 0x5A, 0x5A, 0, 0, 0),
        (0, "preserve", "clear"): (0, 0x5A, 0x5A, 0x5A, 0, 0x5A, 0x5A),
        (0, "preserve", "preserve"): (
            0, 0x5A, 0x5A, 0x5A, 0x5A, 0x5A, 0x5A,
        ),
        (1, "clear", "clear"): (0, 0, 0x5A, 0x5A, 0, 0, 0),
        (1, "clear", "preserve"): (0, 0, 0x5A, 0x5A, 0x5A, 0x5A, 0),
        (1, "preserve", "clear"): (0, 0, 0x5A, 0x5A, 0, 0, 0x5A),
        (1, "preserve", "preserve"): (
            0, 0, 0x5A, 0x5A, 0x5A, 0x5A, 0x5A,
        ),
    }
    traces: dict[str, tuple[int, ...]] = {}
    reset = (True, False, False, False, True, False, False)
    for name, latency, contents, read_data, scheduled in _RESET_PROFILE_MATRIX:
        module = compile_source(
            _reset_profile_source(
                name, latency, contents, read_data, scheduled
            ),
            include_clash=False,
        ).ir
        trace = tuple(
            int(item["q"])
            for item in simulate_cycles(
                module,
                _reset_profile_cycles(scheduled=scheduled),
                reset=reset,
            )
        )
        assert trace == expected_by_profile[(latency, contents, read_data)]
        traces[name] = trace
    return traces


def _matrix_wrapper() -> str:
    output_ports = ",\n".join(
        f"  output logic [7:0] q{index}"
        for index in range(len(_RESET_PROFILE_MATRIX))
    )
    instances = []
    for index, (name, _, _, _, scheduled) in enumerate(_RESET_PROFILE_MATRIX):
        read_binding = ".read_enable(read_enable), " if scheduled else ""
        instances.append(
            f"  {name} profile_{index} ("
            ".clk(clk), .rst(rst), .address(address), "
            f"{read_binding}.write_enable(write_enable), "
            f".write_data(write_data), .q(q{index}));"
        )
    return "\n".join((
        "`default_nettype none",
        "module MemoryResetProfileMatrix (",
        "  input logic clk,",
        "  input logic rst,",
        "  input logic [1:0] address,",
        "  input logic read_enable,",
        "  input logic write_enable,",
        "  input logic [7:0] write_data,",
        output_ports,
        ");",
        *instances,
        "endmodule",
        "`default_nettype wire",
        "",
    ))


def _matrix_harness() -> str:
    outputs = " << ' ' << ".join(
        f"static_cast<unsigned>(d.q{index})"
        for index in range(len(_RESET_PROFILE_MATRIX))
    )
    stimuli = (
        (1, 1, 1, 0x11),
        (0, 1, 1, 0x5A),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        (1, 1, 1, 0xA5),
        (0, 1, 0, 0),
        (0, 0, 0, 0),
    )
    calls = "\n".join(
        f"  sample(d, {rst}, {read_enable}, {write_enable}, {write_data});"
        for rst, read_enable, write_enable, write_data in stimuli
    )
    return f'''\
#include "VMemoryResetProfileMatrix.h"
#include <iostream>

static void edge(VMemoryResetProfileMatrix &d) {{
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}}

static void sample(
    VMemoryResetProfileMatrix &d,
    int reset,
    int read_enable,
    int write_enable,
    int write_data) {{
  d.rst = reset;
  d.address = 1;
  d.read_enable = read_enable;
  d.write_enable = write_enable;
  d.write_data = write_data;
  // The semantic simulator presents reset state in the reset-marked cycle.
  // Sample after that active edge; ordinary cycles retain their pre-edge view.
  if (reset) edge(d); else d.eval();
  std::cout << {outputs} << '\\n';
  if (!reset) edge(d);
}}

int main() {{
  VMemoryResetProfileMatrix d;
  d.clk = 0;
{calls}
  return 0;
}}
'''


def _run_reset_profile_matrix(
    files: tuple[Path, ...], root: Path, *, strict_lint: bool,
) -> dict[str, tuple[int, ...]]:
    wrapper = root / "MemoryResetProfileMatrix.sv"
    wrapper.write_text(_matrix_wrapper())
    harness = root / "matrix_harness.cpp"
    harness.write_text(_matrix_harness())
    if strict_lint:
        subprocess.run(
            (
                "verilator", "--lint-only", "-Wall", "--top-module",
                "MemoryResetProfileMatrix", *map(str, files), str(wrapper),
            ),
            check=True, capture_output=True, text=True,
        )
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            "MemoryResetProfileMatrix", "--Mdir", str(obj),
            "-o", "memory_profile_matrix", *map(str, files),
            str(wrapper), str(harness),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    completed = subprocess.run(
        (str(obj / "memory_profile_matrix"),),
        check=True, capture_output=True, text=True,
    )
    rows = tuple(
        tuple(int(value) for value in line.split())
        for line in completed.stdout.splitlines()
        if line.strip()
    )
    assert len(rows) == 7
    return {
        name: tuple(row[index] for row in rows)
        for index, (name, *_rest) in enumerate(_RESET_PROFILE_MATRIX)
    }


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_reset_profile_matrix_direct_sv_matches_simulator(tmp_path: Path) -> None:
    expected = _semantic_reset_profile_traces()
    rtl_files = []
    for name, latency, contents, read_data, scheduled in _RESET_PROFILE_MATRIX:
        module = compile_source(
            _reset_profile_source(
                name, latency, contents, read_data, scheduled
            ),
            include_clash=False,
        ).ir
        rtl = tmp_path / f"{name}.sv"
        rtl.write_text(emit_experimental(module))
        rtl_files.append(rtl)
    assert _run_reset_profile_matrix(
        tuple(rtl_files), tmp_path, strict_lint=True
    ) == expected


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_reset_profile_matrix_clash_matches_simulator(tmp_path: Path) -> None:
    expected = _semantic_reset_profile_traces()
    rtl_files: list[Path] = []
    for name, latency, contents, read_data, scheduled in _RESET_PROFILE_MATRIX:
        result = compile_source(
            _reset_profile_source(
                name, latency, contents, read_data, scheduled
            )
        )
        rtl_files.extend(generate_verilog(
            result.clash, name, tmp_path / f"clash_{name}", CLASH
        ))
    assert _run_reset_profile_matrix(
        tuple(rtl_files), tmp_path, strict_lint=False
    ) == expected
