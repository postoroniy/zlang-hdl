"""Signed-to-unsigned saturation must clamp before losing the source sign."""

from fractions import Fraction

import pytest

from tests.simulation.differential import run_differential
from zlang.formal import run_verilog_formal
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode
from zlang import sim


def run(rtl, contract, directory, mode):
    """Execute the compiler oracle without depending on any IP test support."""
    return run_verilog_formal(
        rtl + contract, top="contract", property_id="compiler.saturation",
        mode=mode, depth=8, timeout_seconds=30,
        route="abc-pdr" if mode is ProofMode.PROVE else "smtbmc",
        solver="pdr" if mode is ProofMode.PROVE else "z3",
        systemverilog=True, flatten=True, work_directory=directory,
    )


def source(fraction=0, rounding="nearest_even", source_signed=True, target_signed=False):
    source_type = "fixed" if source_signed else "ufixed"
    target_type = "fixed" if target_signed else "ufixed"
    source_raw = "s8" if source_signed else "u8"
    target_raw = "s4" if target_signed else "u4"
    return f"""module Top {{
 in raw:{source_raw} out result:{target_raw}
 value=bitcast<{source_type}<8,2>>(raw)
 result=bitcast<{target_raw}>(quantize<{target_type}<4,{fraction}>>(value) {{
   round {rounding} overflow saturate
 }})
}}"""


def expected(raw, fraction, mode, target_signed=False):
    value = Fraction(raw) * Fraction(2) ** (fraction-2)
    if mode == "nearest_even":
        rounded = round(value)  # Fraction implements exact ties-to-even.
    elif mode == "floor":
        rounded = value.numerator // value.denominator
    elif mode == "toward_zero":
        rounded = int(value)
    else:
        rounded = -(-abs(value.numerator) // value.denominator)
        rounded *= -1 if value < 0 else 1
    low, high = (-8, 7) if target_signed else (0, 15)
    return min(max(rounded, low), high)


def proof(artifact, fraction, mode, source_signed, target_signed, directory):
    values = range(-128, 128) if source_signed else range(256)
    table = "\n".join(
        f"8'd{v & 255}:ref_value=4'd{expected(v,fraction,mode,target_signed) & 15};"
        for v in values
    )
    contract = f"""module contract(input clk,input [7:0] raw);
 wire [3:0] result; reg [3:0] ref_value;
 Top dut(.*);
 always @* begin
   case(raw) {table} default:ref_value=0; endcase
   assert(result == ref_value);
 end
 endmodule"""
    return run(artifact, contract, directory, ProofMode.PROVE)


@pytest.mark.parametrize("fraction", (0, 2, 3))
@pytest.mark.parametrize("mode", ("nearest_even", "floor", "toward_zero", "away_zero"))
@pytest.mark.parametrize("source_signed,target_signed", ((True, False), (True, True),
                                                       (False, True), (False, False)))
def test_saturate_actual_rtl_all_inputs(tmp_path, fraction, mode, source_signed, target_signed):
    text = source(fraction, mode, source_signed, target_signed)
    path = tmp_path / "top.zhl"
    path.write_text(text)
    for logic_state in (False, True):
        with sim.compile(path, logic_state=logic_state).create() as machine:
            values = range(-128, 128) if source_signed else range(256)
            for value in values:
                machine.set("raw", value)
                machine.eval()
                assert machine.get("result") == expected(value, fraction, mode, target_signed)
    artifact = emit_artifact(compile_source(text, top="Top").ir)
    result = proof(artifact.text, fraction, mode, source_signed, target_signed, tmp_path)
    assert result.status is FormalStatus.PROVEN, result.reason


@pytest.mark.parametrize("source_signed,target_signed", ((True, False), (True, True),
                                                       (False, True), (False, False)))
def test_saturate_native_verilator(tmp_path, source_signed, target_signed):
    path = tmp_path / "top.zhl"
    path.write_text(source(source_signed=source_signed, target_signed=target_signed))
    values = range(-128, 128) if source_signed else range(256)
    rows = [{"set": {"raw": value}} for value in values]
    trace = run_differential(path, top="Top", directory=tmp_path, events=rows)
    assert trace.native == trace.direct_sv


def test_saturate_sign_mutation_has_actual_rtl_counterexample(tmp_path):
    artifact = emit_artifact(compile_source(source(2), top="Top").ir)
    needle = "< 9'sd0"
    assert needle in artifact.text
    # Reintroduce the original lost-sign lower comparison on actual DUT RTL.
    mutated = artifact.text.replace(needle, "< 9'd0")
    result = proof(mutated, 2, "nearest_even", True, False, tmp_path)
    assert result.status is FormalStatus.FAILED, result.reason


@pytest.mark.parametrize("sw,sf,tw,tf", ((8, 2, 16, 3), (129, 65, 67, 3)))
def test_signed_unsigned_widening_and_multi_limb_actual_rtl(tmp_path, sw, sf, tw, tf):
    text = f"""module Top {{
 in raw:sint<{sw}> out result:uint<{tw}>
 result=bitcast<uint<{tw}>>(quantize<ufixed<{tw},{tf}>>(
   bitcast<fixed<{sw},{sf}>>(raw)
 ) {{ round nearest_even overflow saturate }})
}}"""
    artifact = emit_artifact(compile_source(text, top="Top").ir)
    shift = sf-tf
    rw = max(sw+max(-shift, 0)+2, tw+2)
    if shift > 0:
        rounding = f"""rounded = value >>> {shift};
 if ((value & {rw}'d{(1 << shift)-1}) > {rw}'d{1 << (shift-1)} ||
     ((value & {rw}'d{(1 << shift)-1}) == {rw}'d{1 << (shift-1)} && rounded[0]))
   rounded = rounded + 1;"""
    else:
        rounding = f"rounded = value <<< {-shift};"
    contract = f"""module contract(input clk,input signed [{sw-1}:0] raw);
 wire [{tw-1}:0] result; reg [{tw-1}:0] ref_value;
 wire signed [{rw-1}:0] value = $signed(raw);
 reg signed [{rw-1}:0] rounded;
 Top dut(.*);
 always @* begin
   {rounding}
   if(rounded < {rw}'sd0) ref_value=0;
   else if(rounded > {rw}'sd{(1 << tw)-1}) ref_value={tw}'d{(1 << tw)-1};
   else ref_value=rounded[{tw-1}:0];
   assert(result == ref_value);
 end
 endmodule"""
    result = run(artifact.text, contract, tmp_path, ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason
