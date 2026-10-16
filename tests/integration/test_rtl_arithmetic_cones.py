# SPDX-License-Identifier: Apache-2.0
"""Check the RTL proof reader independently against exhaustive Icarus values."""

from itertools import product
import json
import subprocess

import z3

from zlang.formal_rtl_cones import RtlArithmeticCones


def test_yosys_arithmetic_encoding_matches_exhaustive_rtl(tmp_path):
    cases = tuple(
        product((1, 3), (2, 3), (2, 6), (False, True), (False, True), ("+", "-", "*"))
    )
    outputs, assignments, wires, connections = [], [], [], [".a(a)", ".b(b)"]
    for index, (aw, bw, width, a_signed, b_signed, operation) in enumerate(cases):
        name = f"y{index}"
        outputs.append(f"output wire [{width - 1}:0] {name}")
        a = f"{'$signed' if a_signed else '$unsigned'}(a[{aw - 1}:0])"
        b = f"{'$signed' if b_signed else '$unsigned'}(b[{bw - 1}:0])"
        assignments.append(f"assign {name} = {a} {operation} {b};")
        wires.append(f"wire [{width - 1}:0] {name};")
        connections.append(f".{name}({name})")
    source = (
        "module DUT(input wire [2:0] a,b, "
        + ",".join(outputs)
        + ");\n"
        + "\n".join(assignments)
        + "\nendmodule\n"
    )
    (tmp_path / "dut.sv").write_text(source)
    lowered = subprocess.run(
        [
            "yosys",
            "-Q",
            "-T",
            "-p",
            "read_verilog -sv dut.sv; hierarchy -top DUT; proc; opt_clean; check -assert; write_json dut.json",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert lowered.returncode == 0, lowered.stdout + lowered.stderr
    module = json.loads((tmp_path / "dut.json").read_text())["modules"]["DUT"]
    reader = RtlArithmeticCones(module)
    expressions = [
        reader.vector(tuple(module["ports"][f"y{index}"]["bits"]))
        for index in range(len(cases))
    ]
    ports = {tuple(module["ports"][name]["bits"]): name for name in ("a", "b")}
    names = ",".join(f"y{index}" for index in range(len(cases)))
    tb = "\n".join(
        (
            "module tb; reg [2:0] a,b;",
            *wires,
            "DUT dut(" + ",".join(connections) + ");",
            "initial begin for (integer i=0;i<8;i=i+1) begin for (integer j=0;j<8;j=j+1) begin",
            'a=i; b=j; #1; $display("sample %0d %0d '
            + " ".join("%h" for _ in cases)
            + '",i,j,'
            + names
            + ");",
            "end end $finish; end endmodule",
        )
    )
    (tmp_path / "tb.sv").write_text(tb)
    compiled = subprocess.run(
        ["iverilog", "-g2012", "-s", "tb", "-o", "run.vvp", "dut.sv", "tb.sv"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    observed = subprocess.run(
        ["vvp", "run.vvp"], cwd=tmp_path, text=True, capture_output=True, timeout=20
    )
    assert observed.returncode == 0, observed.stderr
    rows = [line for line in observed.stdout.splitlines() if line.startswith("sample ")]
    assert len(rows) == 64
    for row in rows:
        _, a, b, *values = row.split()
        inputs = {"a": int(a), "b": int(b)}
        bindings = tuple(
            (
                leaf.expression,
                z3.BitVecVal(
                    inputs[ports[leaf.bits]], len(leaf.bits), ctx=reader.context
                ),
            )
            for leaf in reader.leaves
        )
        expected = [
            z3.simplify(z3.substitute(expression, *bindings)).as_long()
            for expression in expressions
        ]
        assert expected == [int(value, 16) for value in values]
