from __future__ import annotations

from functools import reduce
import operator
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.structural.catalog import WITNESSES, WITNESS_BY_SLUG
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.simulate import simulate


def _compile(slug: str):
    witness = WITNESS_BY_SLUG[slug]
    return compile_source(witness.source_text("small"), top=witness.top).ir


def _or(values: list[int]) -> int:
    return reduce(operator.or_, values, 0)


def _xor(values: list[int]) -> int:
    return reduce(operator.xor, values, 0)


def _crc32_reflected(data: int, seed: int, width: int) -> int:
    crc = seed
    for index in range(width):
        mix = (crc ^ (data >> index)) & 1
        crc >>= 1
        if mix:
            crc ^= 0xEDB88320
    return crc


@pytest.mark.parametrize("witness", WITNESSES, ids=lambda item: item.slug)
def test_structural_witness_is_deterministic_single_module(witness) -> None:
    module = compile_source(witness.source_text("small"), top=witness.top).ir
    first = emit_experimental(module)
    second = emit_experimental(module)

    assert first == second
    assert len(first.encode()) < 2 * 1024 * 1024
    assert max(map(len, first.splitlines())) < 64 * 1024
    assert re.findall(r"(?m)^module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", first) == [
        witness.top
    ]
    assert "_zlang_core" not in first
    assert "zlang_top_core" not in first
    functions = re.finditer(
        r"function automatic\s+[^;]+?\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\((?P<parameters>.*?)\);(?P<body>.*?)endfunction",
        first,
        flags=re.DOTALL,
    )
    for function in functions:
        if function.group("parameters").strip():
            continue
        name = re.escape(function.group("name"))
        assert re.fullmatch(
            rf"\s*(?://[^\n]*\n\s*)?{name}\s*=\s*"
            r"\d+'[s]?[dhbo][0-9a-fA-F_xzXZ]+;\s*",
            function.group("body"),
        ) is None


@pytest.mark.parametrize(
    "slug",
    ("priority_encoder", "wide_arbiter", "fir"),
)
def test_medium_nested_functional_regions_reach_direct_sv(slug: str) -> None:
    witness = WITNESS_BY_SLUG[slug]
    module = compile_source(witness.source_text("medium"), top=witness.top).ir

    generated = emit_experimental(module)

    assert re.search(rf"(?m)^module\s+{witness.top}\s*\(", generated)
    assert "_zlang_core" not in generated


def test_generic_explosion_has_no_duplicate_specialization_identity() -> None:
    module = _compile("generic_explosion")
    identities = tuple(item.identity for item in module.generic_specializations)

    assert identities
    assert len(identities) == len(set(identities))


def test_one_hot_mux_preserves_the_one_hot_case_without_assuming_it() -> None:
    module = _compile("one_hot_mux")
    values = [1 << (index % 16) for index in range(16)]
    select = [int(index == 9) for index in range(16)]

    assert simulate(module, values=values, select=select) == {
        "selected_or": values[9],
        "any_selected": 1,
        "multiple_selected": 0,
    }


def test_parallel_crc_matches_the_bit_serial_polynomial_reference() -> None:
    module = _compile("crc_parallel")
    vectors = (
        (0x00000000, 0x00000000),
        (0xFFFFFFFF, 0xFFFFFFFF),
        (0xA55AA55A, 0x12345678),
        (0x01234567, 0x89ABCDEF),
        (0xDEADBEEF, 0x13579BDF),
    )

    for data, seed in vectors:
        assert simulate(module, data=data, seed=seed) == {
            "crc": _crc32_reflected(data, seed, 32)
        }


def test_round_robin_protocol_arbiter_is_a_single_deterministic_module(
    tmp_path: Path,
) -> None:
    source = WITNESS_BY_SLUG["wide_arbiter"].source.read_text(encoding="utf-8")
    module = compile_source(source, top="RoundRobinProtocolWitness").ir
    first = emit_experimental(module)
    second = emit_experimental(module)

    assert first == second
    assert re.findall(r"(?m)^module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", first) == [
        "RoundRobinProtocolWitness"
    ]
    assert "_zlang_core" not in first
    assert "zlang_top_core" not in first

    rtl = tmp_path / "RoundRobinProtocolWitness.sv"
    rtl.write_text(first, encoding="utf-8")
    if shutil.which("verilator") is not None:
        completed = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "--timing",
                "-Wall",
                "-Wno-fatal",
                "--top-module",
                "RoundRobinProtocolWitness",
                str(rtl),
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    if shutil.which("yosys") is not None:
        completed = subprocess.run(
            (
                "yosys",
                "-q",
                "-p",
                f"read_verilog -sv {rtl}; "
                "hierarchy -check -top RoundRobinProtocolWitness; "
                "proc; opt_expr; opt_clean; check",
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("witness", WITNESSES, ids=lambda item: item.slug)
@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_structural_witness_lints_with_verilator(witness, tmp_path: Path) -> None:
    rtl = tmp_path / f"{witness.top}.sv"
    rtl.write_text(
        emit_experimental(
            compile_source(witness.source_text("small"), top=witness.top).ir
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "--timing",
            "-Wall",
            "-Wno-fatal",
            "--top-module",
            witness.top,
            str(rtl),
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("witness", WITNESSES, ids=lambda item: item.slug)
@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_structural_witness_crosses_meaningful_yosys_stages(
    witness,
    tmp_path: Path,
) -> None:
    rtl = tmp_path / f"{witness.top}.sv"
    rtl.write_text(
        emit_experimental(
            compile_source(witness.source_text("small"), top=witness.top).ir
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        (
            "yosys",
            "-q",
            "-p",
            f"read_verilog -sv {rtl}; hierarchy -check -top {witness.top}; "
            "proc; opt_expr; opt_clean; check",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("slug", tuple(item.slug for item in WITNESSES))
def test_structural_witness_semantics(slug: str) -> None:
    module = _compile(slug)

    if slug == "barrel_shifter":
        inputs = {"data": 0x9235, "shift": 3}
        expected = {
            "shifted_left": (inputs["data"] << 3) & 0xFFFF,
            "shifted_right": inputs["data"] >> 3,
        }
    elif slug == "dynamic_permute":
        data = [11, 22, 33, 44, 55, 66, 77, 88]
        index = [7, 0, 6, 1, 5, 2, 4, 3]
        inputs = {"data": data, "index": index}
        expected = {"result": [data[item] for item in index]}
    elif slug in {"priority_encoder", "wide_arbiter"}:
        requests = [0] * 16
        requests[3] = requests[11] = 1
        inputs = {"requests": requests}
        expected = (
            {"valid": 1, "selected": 3}
            if slug == "priority_encoder"
            else {
                "grants": [int(index == 3) for index in range(16)],
                "any_grant": 1,
                "selected": 3,
            }
        )
    elif slug == "packet_compactor":
        data = [10, 20, 30, 40, 50, 60, 70, 80]
        valid = [1, 0, 1, 1, 0, 0, 1, 0]
        compact = [item for item, keep in zip(data, valid, strict=True) if keep]
        inputs = {"data": data, "valid": valid}
        expected = {"packed": compact + [0] * (8 - len(compact)), "count": 4}
    elif slug == "scatter_gather":
        values = [1, 2, 4, 8, 16, 32, 64, 128]
        index = [1, 1, 2, 7, 0, 3, 4, 5]
        enable = [1, 1, 1, 1, 0, 1, 1, 1]
        scattered = [0] * 8
        for value, destination, active in zip(values, index, enable, strict=True):
            if active:
                scattered[destination] |= value
        inputs = {"input": values, "index": index, "enable": enable}
        expected = {
            "gathered": [values[item] for item in index],
            "scattered_or": scattered,
        }
    elif slug == "variable_slice":
        source = [index * 0x01010101 for index in range(16)]
        inputs = {"source": source, "index": 9}
        expected = {"field": source[9]}
    elif slug == "crossbar":
        values = [0x1000 + index for index in range(8)]
        select = [3, 2, 1, 0, 7, 6, 5, 4]
        inputs = {"inputs": values, "select": select}
        expected = {"outputs": [values[item] for item in select]}
    elif slug == "cam":
        keys = [3, 7, 11, 7, 19, 23, 29, 31]
        values = [100 + index for index in range(8)]
        inputs = {"search_key": 7, "keys": keys, "values": values}
        expected = {"hit": 1, "match_index": 1, "match_value": values[1]}
    elif slug == "reduction_tree":
        values = [index * 7 & 0xFF for index in range(32)]
        groups = [_xor(values[start : start + 8]) for start in range(0, 32, 8)]
        inputs = {"values": values}
        expected = {
            "or_value": _or(values),
            "and_value": reduce(operator.and_, values, 0xFF),
            "xor_value": _xor(values),
            "sum_value": sum(values) & 0xFFFF,
            "grouped_xor": _xor(groups),
        }
    elif slug == "generic_explosion":
        runtime = [1000 + index for index in range(8)]
        constants = [
            [[(a * 17 + b * 5 + c) & 0xFFFF for c in range(2)] for b in range(4)]
            for a in range(8)
        ]
        inputs = {"runtime": runtime}
        expected = {
            "constants": constants,
            "partials": [
                [[(runtime[a] + constants[a][b][c]) & 0xFFFF for c in range(2)] for b in range(4)]
                for a in range(8)
            ],
            "indexed": [
                [[(runtime[a] + constants[a][b][c]) & 0xFFFF for c in range(2)] for b in range(4)]
                for a in range(8)
            ],
        }
    elif slug == "fir":
        samples = [index + 1 for index in range(16)]
        inputs = {"samples": samples}
        expected = {
            "result": sum(value * (index * 3 + 1) for index, value in enumerate(samples))
            & 0xFFFFFFFF
        }
    elif slug == "crc_parallel":
        data = 0xA55AA55A
        seed = 0x12345678
        inputs = {"data": data, "seed": seed}
        expected = {"crc": _crc32_reflected(data, seed, 32)}
    elif slug == "matrix_transpose":
        matrix = [[row * 16 + column for column in range(8)] for row in range(8)]
        inputs = {"matrix": matrix}
        expected = {
            "transposed": [[matrix[column][row] for column in range(8)] for row in range(8)]
        }
    elif slug == "byte_lane_aligner":
        source = list(range(64))
        inputs = {"source": source, "offset": 5}
        expected = {"aligned": source[5:13]}
    elif slug == "one_hot_mux":
        values = [1 << (index % 16) for index in range(16)]
        select = [int(index in (2, 5)) for index in range(16)]
        inputs = {"values": values, "select": select}
        expected = {
            "selected_or": values[2] | values[5],
            "any_selected": 1,
            "multiple_selected": 1,
        }
    elif slug == "prefix_network":
        flags = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1]
        inputs = {"flags": flags}
        expected = {
            "prefix_or": [int(any(flags[: index + 1])) for index in range(16)],
            "prefix_count": [sum(flags[: index + 1]) for index in range(16)],
        }
    elif slug == "multidimensional_index":
        values = [
            [[lane * 16 + group * 4 + byte for byte in range(4)] for group in range(4)]
            for lane in range(12)
        ]
        inputs = {"values": values, "lane": 5, "group": 1, "byte_index": 3}
        expected = {
            "static_static": values[3][2],
            "static_runtime": values[3][1][3],
            "runtime_static": values[5][2][1],
            "runtime_runtime": values[5][1][3],
        }
    else:  # pragma: no cover - catalog/test exhaustiveness guard
        raise AssertionError(f"missing semantic oracle for {slug}")

    assert simulate(module, **inputs) == expected


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus unavailable",
)
def test_barrel_shifter_executes_in_icarus(tmp_path: Path) -> None:
    witness = WITNESS_BY_SLUG["barrel_shifter"]
    rtl = tmp_path / "BarrelShifterWitness.sv"
    rtl.write_text(emit_experimental(_compile(witness.slug)), encoding="utf-8")
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic [15:0] data;
  logic [8:0] shift;
  logic [15:0] shifted_left, shifted_right;
  BarrelShifterWitness dut(.*);
  initial begin
    data = 16'h9235; shift = 9'd3; #1;
    if (shifted_left !== 16'h91a8) $fatal(1, "left");
    if (shifted_right !== 16'h1246) $fatal(1, "right");
    $finish;
  end
endmodule
""",
        encoding="utf-8",
    )
    executable = tmp_path / "barrel.vvp"
    compiled = subprocess.run(
        ("iverilog", "-g2012", "-s", "tb", "-o", str(executable), str(rtl), str(bench)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = subprocess.run(
        ("vvp", str(executable)), capture_output=True, text=True, timeout=30
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus unavailable",
)
@pytest.mark.parametrize(
    ("slug", "bench"),
    (
        (
            "dynamic_permute",
            """
module tb;
  logic [7:0][7:0] data;
  logic [7:0][2:0] index;
  logic [7:0][7:0] result;
  DynamicPermuteWitness dut(.*);
  initial begin
    for (integer i = 0; i < 8; i = i + 1) begin
      data[i] = 8'(i + 11);
      index[i] = 3'(7 - i);
    end
    #1;
    for (integer i = 0; i < 8; i = i + 1)
      if (result[i] !== data[7-i]) $fatal(1, "dynamic permutation");
    $finish;
  end
endmodule
""",
        ),
        (
            "matrix_transpose",
            """
module tb;
  logic [7:0][7:0][7:0] matrix;
  logic [7:0][7:0][7:0] transposed;
  MatrixTransposeWitness dut(.*);
  initial begin
    matrix = 512'h77767574737271706766656463626160575655545352515047464544434241403736353433323130272625242322212017161514131211100706050403020100;
    #1;
    if (transposed !== 512'h77675747372717077666564636261606756555453525150574645444342414047363534333231303726252423222120271615141312111017060504030201000)
      $fatal(1, "static transpose");
    $finish;
  end
endmodule
""",
        ),
    ),
)
def test_static_and_runtime_permutations_execute_in_icarus(
    slug: str,
    bench: str,
    tmp_path: Path,
) -> None:
    witness = WITNESS_BY_SLUG[slug]
    rtl = tmp_path / f"{witness.top}.sv"
    rtl.write_text(emit_experimental(_compile(slug)), encoding="utf-8")
    testbench = tmp_path / f"{slug}_tb.sv"
    testbench.write_text(bench, encoding="utf-8")
    executable = tmp_path / f"{slug}.vvp"
    compiled = subprocess.run(
        (
            "iverilog",
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(executable),
            str(rtl),
            str(testbench),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = subprocess.run(
        ("vvp", str(executable)), capture_output=True, text=True, timeout=30
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
