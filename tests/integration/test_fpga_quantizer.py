# SPDX-License-Identifier: Apache-2.0
"""Source-bound quantizer lemmas reject corrupted RTL and observation facts."""

from copy import deepcopy
from hashlib import sha256
import json
import time

import pytest
import z3

from zlang.backend.systemverilog.fpga_dsp import _fir_round_sat
from zlang.compiler import compile_source
from zlang.formal_bitvector_rewrite import ArithmeticProofUnavailable
from zlang.formal_fpga_quantizer import checked_fpga_quantizers
from zlang.formal_rtl_arithmetic import _yosys


def actual_design(root, mutation=None):
    product = compile_source("""module Reference {
        in raw:fixed<33,20> out result:fixed<16,14>
        result=quantize<fixed<16,14>>(raw) { round nearest_even overflow saturate }
    }""")
    value = product.ir.assignments[0].expression
    lines = []
    rhs = _fir_round_sat(value, value.expression, lambda name, identity: name, lines)
    source = (
        "module top(input [32:0] raw, output [15:0] result);\n"
        + "\n".join(lines)
        + f"\nassign result={rhs};\nendmodule\n"
    )
    if mutation == "rounding":
        assert "raw[5] &&" in source
        source = source.replace("raw[5] &&", "!raw[5] &&", 1)
    (root / "actual.v").write_text(source)
    _yosys(
        "read_verilog -sv actual.v; proc; opt_clean; check -assert; write_json -noscopeinfo actual.json",
        root,
        time.monotonic() + 30,
        "actual",
    )
    return json.loads((root / "actual.json").read_text())


def test_checked_quantizer_keeps_connected_ports_and_replayable_query(tmp_path):
    design = actual_design(tmp_path)
    module = design["modules"]["top"]
    before = deepcopy(module["ports"])
    records = checked_fpga_quantizers(
        design, "top", tmp_path, time.monotonic() + 30, _yosys
    )
    assert len(records) == 1 and module["ports"] == before
    record = records[0]
    assert record["assumptions_added"] == 0 and record["result"] == "unsat"
    query = (tmp_path / "quantizer-0.smt2").read_text()
    assert sha256(query.encode()).hexdigest() == record["query_sha256"]
    solver = z3.SolverFor("QF_BV")
    solver.set(timeout=3000)
    solver.add(z3.parse_smt2_string(query))
    assert solver.check() == z3.unsat
    cell = module["cells"][record["cell"]]
    assert cell["connections"] == {
        "raw": list(record["input_bits"]),
        "result": record["output_bits"],
    }


@pytest.mark.parametrize(
    "mutation", ("rounding", "input", "width", "unknown-cell", "extra-state")
)
def test_corrupted_quantizer_is_not_replaced(mutation, tmp_path):
    design = actual_design(tmp_path, mutation)
    module = design["modules"]["top"]
    marked = next(
        net
        for net in module["netnames"].values()
        if "zlang_fir_quantizer" in net.get("attributes", {})
    )
    if mutation == "input":
        marked["attributes"]["zlang_fir_quantizer_input"] = "nonexistent"
    elif mutation == "width":
        marked["attributes"]["zlang_fir_quantizer"] = "32:20:16:14"
    elif mutation in ("unknown-cell", "extra-state"):
        root = next(
            cell
            for cell in module["cells"].values()
            if cell["connections"].get("Y") == marked["bits"]
        )
        if mutation == "unknown-cell":
            root["type"] = "opaque_unqualified_converter"
        else:
            module["ports"]["state"] = {"direction": "input", "bits": [99999]}
            root["connections"]["S"] = [99999]
    snapshot = deepcopy(design)
    with pytest.raises(ArithmeticProofUnavailable):
        checked_fpga_quantizers(design, "top", tmp_path, time.monotonic() + 30, _yosys)
    assert design == snapshot
    if mutation == "rounding":
        solver = z3.SolverFor("QF_BV")
        solver.set(timeout=3000)
        solver.add(z3.parse_smt2_string((tmp_path / "quantizer-0.smt2").read_text()))
        assert solver.check() == z3.sat


def test_checked_quantizer_source_inventory_changes_cache_policy(monkeypatch):
    from pathlib import Path
    from zlang.compiler import compile_file
    from zlang.formal_joint_value import rtl_arithmetic_policy

    root = Path(__file__).resolve().parents[2]
    product = compile_file(
        root / "examples/fpga/architectures/arithmetic.zhl",
        top="SymmetricFirAuto1",
        target="lfe5u_25f_8mg285c",
        architecture="Ecp5SymmetricFirFabric",
        architecture_mode="required",
    )
    before = rtl_arithmetic_policy(product.implementation_graph, "z3")
    checker = root / "zlang/formal_fpga_quantizer.py"
    original = Path.read_bytes
    assert (
        before["checker_sources"][checker.name] == sha256(original(checker)).hexdigest()
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: original(path)
        + (b"\n# changed checker\n" if path == checker else b""),
    )
    after = rtl_arithmetic_policy(product.implementation_graph, "z3")
    assert before != after
    assert (
        before["checker_sources"][checker.name]
        != after["checker_sources"][checker.name]
    )
    assert {k: v for k, v in before.items() if k != "checker_sources"} == {
        k: v for k, v in after.items() if k != "checker_sources"
    }
