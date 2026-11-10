# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed endpoint spelling preserves binary and pre-existing SV X semantics.

Native logic comparisons deliberately have more precise partial-unknown
behavior than SV relational operators. Their unchanged contract is tested in
test_comparison_endpoint_native; the four-state differential here compares
actual emitted RTL with the original SV relational expressions.
"""

from hashlib import sha256
from itertools import groupby, product
import json
import shutil
import subprocess

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


FAMILIES = ("u", "s", "uf", "sf")


def _bounds(width, family):
    signed = family in ("s", "sf")
    return (-(1 << (width - 1)) if signed else 0,
            (1 << (width - int(signed))) - 1)


def _type(width, family):
    if family in ("u", "s"):
        return ("uint" if family == "u" else "sint") + f"<{width}>"
    return ("ufixed" if family == "uf" else "fixed") + f"<{width},{min(2, width-1)}>"


def _literal(width, family, value):
    # Equal-width raw-bit constructors also exercise fixed endpoint constants,
    # without depending on decimal fractions or host floating-point rounding.
    bits = f"{value & ((1 << width)-1):0{width}b}"
    runs = [f"{'ones' if bit == '1' else 'zeros'}<{len(tuple(run))}>"
            for bit, run in groupby(bits)]
    raw = runs[0] if len(runs) == 1 else "concat(" + ",".join(runs) + ")"
    return f"bitcast<{_type(width, family)}>({raw})"


def _relations(value, minimum, maximum):
    return ((value, "<=", maximum), (value, ">=", minimum),
            (maximum, ">=", value), (minimum, "<=", value),
            (value, ">", maximum), (value, "<", minimum),
            (maximum, "<", value), (minimum, ">", value),
            (value, "<", maximum), (value, "<=", maximum - 1),
            (value, ">", minimum), (value, ">=", minimum + 1),
            (maximum, ">", value), (maximum - 1, ">=", value),
            (minimum, "<", value), (minimum + 1, "<=", value))


def endpoint_source(width, *, neighbors_only=False):
    declarations, assignments = [], []
    for family in FAMILIES:
        name = family + "_value"
        declarations.append(f"in {name}:{_type(width, family)} out {family}_flags:bits<{8 if neighbors_only else 16}>")
        bounds = _bounds(width, family)
        relations = _relations(name, *bounds)[8 if neighbors_only else 0:]
        def render(value):
            return value if isinstance(value, str) else _literal(width, family, value)
        comparisons = [f"({render(a)} {op} {render(b)})" for a, op, b in relations]
        assignments.append(f"{family}_flags=concat({','.join(comparisons)})")
    return "module ComparisonEndpoints {" + " ".join(declarations + assignments) + "}"


def _expected(width, raw):
    result = {}
    for family in FAMILIES:
        minimum, maximum = _bounds(width, family)
        value = raw - (1 << width) if minimum < 0 and raw & (1 << (width-1)) else raw
        comparisons = (value <= maximum, value >= minimum, maximum >= value, minimum <= value,
                       value > maximum, value < minimum, maximum < value, minimum > value,
                       value < maximum, value <= maximum-1, value > minimum, value >= minimum+1,
                       maximum > value, maximum-1 >= value, minimum < value, minimum+1 <= value)
        flags = 0
        for predicate in comparisons:
            flags = (flags << 1) | int(predicate)
        result[family + "_flags"] = flags
    return result


@pytest.mark.parametrize("width", (1, 5, 65))
@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_endpoint_binary_native_and_strict_actual_rtl_match_integer_oracle(
    tmp_path, width, logic_state, simulator,
):
    path = tmp_path / "endpoints.zhl"
    path.write_text(endpoint_source(width))
    mask = (1 << width) - 1
    samples = sorted({0, 1, mask, mask-1, 1 << (width-1), (1 << (width-1))-1,
                      *(item & mask for item in (0x5555555555555555, 0xaaaaaaaaaaaaaaaa))})
    events = [{"set": {
        family + "_value": raw - (1 << width)
        if family in ("s", "sf") and raw & (1 << (width-1)) else raw
        for family in FAMILIES}, "edges": []}
              for raw in samples]
    compared = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == tuple(_expected(width, raw) for raw in samples)


def _sv_reference(width, *, neighbors_only=False):
    lines = []
    for family in FAMILIES:
        cast = "$signed" if family in ("s", "sf") else "$unsigned"
        def render(value):
            if isinstance(value, str):
                return f"{cast}({value})"
            sign = "s" if family in ("s", "sf") else ""
            literal = f"{width}'{sign}d{abs(value)}"
            return f"{cast}({'-' if value < 0 else ''}{literal})"
        relations = _relations(family + "_value", *_bounds(width, family))[8 if neighbors_only else 0:]
        expressions = [f"({render(a)} {op} {render(b)})" for a, op, b in relations]
        lines.append(f"wire [{7 if neighbors_only else 15}:0] {family}_reference={{{','.join(expressions)}}};")
    return "\n".join(lines)


def _run(command, *, timeout=30):
    executable = shutil.which(command[0])
    assert executable, f"required tool missing: {command[0]}"
    return subprocess.run([executable, *command[1:]], capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("mutation", (None, "lost-unknown"))
def test_actual_rtl_preserves_original_sv_relational_four_state_table(tmp_path, mutation):
    width = 3
    compiled = compile_source(endpoint_source(width))
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    rtl = artifact.text
    if mutation:
        needle = "((^((u_value))) | ~(^((u_value))))"
        assert needle in rtl
        rtl = rtl.replace(needle, "1'b1", 1)
    checks = []
    # X and Z both exercise the pre-existing SV truth table; no assumption or
    # native U/X equality is inferred from the binary endpoint tautology.
    for digits in product("01xz", repeat=width):
        spelling = "".join(digits)
        checks.append(" ".join(f"{family}_value={width}'b{spelling};" for family in FAMILIES) + " #1;")
        for family in FAMILIES:
            checks.append(f'if({family}_flags !== {family}_reference) $fatal(1,"endpoint four-state mismatch {family} {spelling}");')
            if "x" in spelling or "z" in spelling:
                checks.append(f'if({family}_reference[15:8] !== 8\'bxxxxxxxx) $fatal(1,"old relational truth table changed");')
    harness = "module tb;\n" + "\n".join(
        f"reg [{width-1}:0] {family}_value; wire [15:0] {family}_flags;" for family in FAMILIES
    ) + f"\n{artifact.module} dut(.*);\n" + _sv_reference(width) + "\ninitial begin\n" + "\n".join(checks) + "\n$finish; end endmodule\n"
    path, binary = tmp_path / "four-state.sv", tmp_path / "four-state.vvp"
    path.write_text(rtl + "\n" + harness)
    built = _run(("iverilog", "-g2012", "-s", "tb", "-o", str(binary), str(path)))
    assert built.returncode == 0, built.stdout + built.stderr
    executed = _run(("vvp", str(binary)))
    (tmp_path / "result.log").write_text(executed.stdout + executed.stderr)
    if mutation:
        assert executed.returncode != 0
        assert "endpoint four-state mismatch" in executed.stdout + executed.stderr
    else:
        assert executed.returncode == 0, executed.stdout + executed.stderr


@pytest.mark.parametrize("width,mutation", ((1, None), (5, None), (65, None),
                                           (5, "wrong-polarity"), (5, "lost-sign"),
                                           (5, "wrong-neighbor")))
def test_endpoint_actual_rtl_is_proven_and_mutations_have_counterexamples(tmp_path, width, mutation):
    compiled = compile_source(endpoint_source(width))
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    rtl = artifact.text
    if mutation == "wrong-polarity":
        rtl = rtl.replace(" | ~", " & ~", 1)
    elif mutation == "lost-sign":
        rtl = rtl.replace("$signed((s_value)) <", "$unsigned((s_value)) <", 1)
    elif mutation == "wrong-neighbor":
        rtl = rtl.replace("5'd30", "5'd29", 1)
    if mutation:
        assert rtl != artifact.text, "mutation must change actual emitted RTL"
    ports = ",".join(f"input [{width-1}:0] {family}_value" for family in FAMILIES)
    harness = "module contract(" + ports + ");\n" + "\n".join(
        f"wire [15:0] {family}_flags;" for family in FAMILIES
    ) + f"\n{artifact.module} dut(.*);\n" + _sv_reference(width) + "\nalways @* begin\n" + "\n".join(
        f"assert({family}_flags == {family}_reference);" for family in FAMILIES
    ) + "\nend endmodule\n"
    assert harness.count("assert(") == 4
    assert "assume(" not in harness
    source = rtl + "\n" + harness
    (tmp_path / "actual-rtl-reference.sv").write_text(source)
    result = run_verilog_formal(
        source, top="contract", property_id=f"comparison-endpoints.{width}.{mutation or 'positive'}",
        mode=ProofMode.BMC if mutation else ProofMode.PROVE, depth=8,
        timeout_seconds=30, route="smtbmc", solver="z3", systemverilog=True,
        flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "receipt.json").write_text(json.dumps({
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash,
        "source_sha256": sha256(source.encode()).hexdigest(),
        "harness_sha256": sha256(harness.encode()).hexdigest(),
        "depth": 8, "timeout_seconds": 30, "assumptions": 0,
        "assertions": 4, "status": result.status.value, "reason": result.reason,
    }, indent=2) + "\n")
    assert result.status is (FormalStatus.FAILED if mutation else FormalStatus.PROVEN), (result.status, result.reason)
    if mutation:
        assert result.counterexample is not None


def test_neighbor_and_dynamic_comparisons_keep_typed_relational_spelling():
    artifact = emit_artifact(compile_source(endpoint_source(5, neighbors_only=True)).ir)
    assert "(^" not in artifact.text
    assert "$signed((s_value)) <= $signed((5'sd14))" in artifact.text
    assert "$unsigned((u_value)) >= $unsigned((5'd1))" in artifact.text
    dynamic = emit_artifact(compile_source(
        "module Dynamic {in a:s5 in b:s5 out y:bit out same:bit out different:bit "
        "y=a<=b same=a==b different=a!=b}"
    ).ir)
    assert "$signed((a)) <= $signed((b))" in dynamic.text
    assert "((a) == (b))" in dynamic.text
    assert "((a) != (b))" in dynamic.text
