# SPDX-License-Identifier: Apache-2.0
"""Check the RTL proof reader independently against exhaustive Icarus values."""

from itertools import product
import json
import subprocess
import time

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


def test_proof_naming_changes_only_arithmetic_metadata(tmp_path, monkeypatch):
    from hashlib import sha256
    import zlang.formal_rtl_arithmetic as owner
    from zlang.formal_joint_value import RTL_ARITHMETIC_PROOF_SCHEMA

    source = """module miter(input clk,rst,input signed [15:0] a,b,c);
 wire signed [31:0] p=a*b,q=a*c;
 wire signed [32:0] sum=$signed(p)+$signed(q);
 reg signed [39:0] value;
 reg started=0;
 always @(posedge clk) begin
   started<=1;
   if(!started) assume(rst);
   if(rst) value<=0; else value<=sum;
   if(started && $past(rst)) assert(value==0);
 end
 endmodule
"""
    scripts = []
    execute = owner._yosys

    def observed(script, *args):
        scripts.append(script)
        return execute(script, *args)

    monkeypatch.setattr(owner, "_yosys", observed)
    prepared = owner.normalize_rtl_arithmetic(
        source, top="miter", work_directory=tmp_path,
        deadline=time.monotonic()+30,
    )
    root = tmp_path / "rtl_arithmetic"
    proof = json.loads((root / "proof.json").read_text())
    before = json.loads((root / "original.json").read_text())["modules"]["miter"]
    after = json.loads((root / "normalized.json").read_text())["modules"]["miter"]
    roots = {record["cell"] for record in proof["roots"]}
    assert roots and proof["assumptions_added"] == 0
    assert proof["schema"] == RTL_ARITHMETIC_PROOF_SCHEMA
    assert proof["source_hash"] == sha256(source.encode()).hexdigest()
    assert all(after["cells"][key] == cell for key, cell in before["cells"].items() if key not in roots)
    assert before["ports"] == after["ports"]
    assert any(cell["type"] == "$dff" for cell in before["cells"].values())
    assert any(cell["type"] in ("$check", "$assert", "$assume") for cell in before["cells"].values())
    final_script = scripts[-1]
    names = final_script.split("autoname ", 1)[1].split(";", 1)[0].split()
    assert set(names) == {f"t:{kind}" for kind in ("$add", "$sub", "$mul", "$neg", "$pos", "$eq", "$ne")}
    assert prepared.source == (root / "normalized.v").read_text()


def test_unused_empty_contract_is_pruned_but_connected_blackbox_fails_closed(tmp_path):
    from zlang.formal_rtl_arithmetic import run_rtl_arithmetic_formal
    from zlang.ir.formal import ProofMode, FormalStatus

    for connected in (False, True):
        definition = "module Contract(input x,output y); endmodule\n" if connected else "module Contract(); endmodule\n"
        binding = "wire opaque; Contract unknown(a[0],opaque);" if connected else ""
        assertion = "opaque==a[0]" if connected else "sum==wide_a+wide_b"
        source = definition+f"""module miter(input clk,input [2:0] a,b);
 wire [3:0] wide_a=a,wide_b=b;
 wire [3:0] sum=a+b;
 {binding}
 always @(posedge clk) assert({assertion}); endmodule
"""
        result = run_rtl_arithmetic_formal(source, top="miter", property_id="empty.contract",
            mode=ProofMode.PROVE, depth=8, timeout_seconds=30, solver="z3",
            systemverilog=True, flatten=True, work_directory=tmp_path/str(connected))
        assert result.status is (FormalStatus.UNKNOWN if connected else FormalStatus.PROVEN), result.reason
        if not connected:
            design = json.loads((tmp_path/str(connected)/"rtl_arithmetic/original.json").read_text())
            assert set(design["modules"]) == {"miter"}
