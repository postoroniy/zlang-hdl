from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.semantic.test_stdlib_coherence import WITNESSES
from zlang import sim
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.semantic import SemanticError
from zlang.semantic import limits as semantic_limits
from zlang.simulation_compare import compare_program


@pytest.mark.parametrize(
    'namespace,expression,declarations,expected,supplied',
    (
        ('dsp.halfband', 'halfband_exact<SW=4,SF=0,CW=4,CF=0,N=7,C=1>(s,c)',
         'in s,c:vec<7,fixed<4,0>> out y:fixed<11,0>', 3, 1),
        ('dsp.halfband', 'halfband_exact<SW=4,SF=0,CW=4,CF=0,N=7,Q=1>(s,c)',
         'in s,c:vec<7,fixed<4,0>> out y:fixed<11,0>', 2, 1),
        ('dsp.halfband', 'halfband_exact<SW=4,SF=0,CW=4,CF=0,N=7,AW=10>(s,c)',
         'in s,c:vec<7,fixed<4,0>> out y:fixed<10,0>', 11, 10),
        ('math.fp_format', 'fp_decode<E=8,F=23,W=33>(x).sign',
         'in x:bits<33> out y:bit', 32, 33),
        ('math.fp_format', 'fp_decode<E=8,F=23,X=14>(x).sign',
         'in x:bits<32> out y:bit', 13, 14),
        ('dsp.polyphase', 'fir_interpolation_batch<SW=4,SF=0,CW=4,CF=0,R=1,K=1,N=2>(s,c)[0]',
         'in s:vec<1,fixed<4,0>> in c:vec<2,fixed<4,0>> out y:fixed<8,0>', 1, 2),
    ),
    ids=('halfband-center', 'halfband-pairs', 'halfband-width', 'fp-width', 'fp-exponent', 'polyphase-count'),
)
def test_stdlib_rejects_inconsistent_derived_parameters(
    monkeypatch, namespace, expression, declarations, expected, supplied,
):
    monkeypatch.setattr(semantic_limits, 'TOTAL_GENERATED', 0)
    source = f'import std.{namespace}\nmodule Top {{ {declarations}\ny={expression} }}'
    with pytest.raises(SemanticError, match='requires equal packed widths') as error:
        compile_source(source)
    assert f'bits<{expected}> ({expected})' in str(error.value)
    assert f'bits<{supplied}> ({supplied})' in str(error.value)
    assert error.value.diagnostic.primary.construct == f'call {expression.split("<", 1)[0]}'
    assert error.value.diagnostic.primary.span.start_line == 3


def test_stdlib_explicit_derived_parameters_match_native_and_rtl(tmp_path: Path):
    source = tmp_path / 'derived.zhl'
    source.write_text('''import std.dsp.halfband
import std.dsp.polyphase
import std.math.fp_format
module Top {
    in a,b,c,d,e,f,g:fixed<4,0>
    in value:bits<32>
    out halfband,halfband_default:fixed<11,0>
    out phase0,phase1,phase2,phase0_default,phase1_default,phase2_default:fixed<9,0>
    out sign,sign_default,subnormal,subnormal_default:bit
    out exponent,exponent_default:sint<13>
    out significand,significand_default:bits<24>
    taps:vec<7,fixed<4,0>> = [1,0,-2,3,-2,0,1]
    coeffs:vec<6,fixed<4,0>> = [1,-2,3,4,-1,-2]
    halfband=halfband_exact<SW=4,SF=0,CW=4,CF=0,N=7,C=3,Q=2,AW=11>([a,b,c,d,e,f,g],taps)
    halfband_default=halfband_exact<SW=4,SF=0,CW=4,CF=0,N=7>([a,b,c,d,e,f,g],taps)
    phases=fir_interpolation_batch<SW=4,SF=0,CW=4,CF=0,R=3,K=2,N=6>([a,b],coeffs)
    phases_default=fir_interpolation_batch<SW=4,SF=0,CW=4,CF=0,R=3,K=2>([a,b],coeffs)
    phase0=phases[0] phase1=phases[1] phase2=phases[2]
    phase0_default=phases_default[0] phase1_default=phases_default[1] phase2_default=phases_default[2]
    decoded=fp_decode<E=8,F=23,W=32,X=13>(value)
    decoded_default=fp_decode<E=8,F=23>(value)
    sign=decoded.sign sign_default=decoded_default.sign
    exponent=decoded.exponent exponent_default=decoded_default.exponent
    significand=decoded.significand significand_default=decoded_default.significand
    subnormal=decoded.subnormal subnormal_default=decoded_default.subnormal
}''')
    # IEEE binary32: -1, +6, and the smallest negative subnormal.
    cases = (
        ((1, 0, 0, 1, 0, 0, 2), 0xbf800000, (1, 0, 1 << 23, 0)),
        ((-8, 7, -1, 3, 2, -2, 5), 0x40c00000, (0, 2, 3 << 22, 0)),
        ((7, -8, 3, -5, -1, 0, -8), 0x80000001, (1, -149, 1 << 23, 1)),
    )
    events = []
    expected = []
    for samples, value, (sign, exponent, significand, subnormal) in cases:
        a, b, c, d, e, _, g = samples
        events.append({'set': dict(zip('abcdefg', samples, strict=True), value=value)})
        # The comparison API records raw packed output bits.
        reference = {
            'halfband': (a - 2*c + 3*d - 2*e + g) & ((1 << 11) - 1),
            'phase0': (a + 4*b) & ((1 << 9) - 1),
            'phase1': (-2*a - b) & ((1 << 9) - 1),
            'phase2': (3*a - 2*b) & ((1 << 9) - 1),
            'sign': sign,
            'exponent': exponent & ((1 << 13) - 1),
            'significand': significand,
            'subnormal': subnormal,
        }
        expected.append(reference | {f'{port}_default': data for port, data in reference.items()})
    compared = compare_program(
        sim.compile(source), events, simulator='iverilog',
        artifact_directory=tmp_path / 'comparison', timeout=30, build_timeout=30,
    )
    assert compared.native_trace == tuple(expected)
    assert compared.rtl_trace == tuple(expected)


@pytest.mark.parametrize("width", (128, 256))
def test_fp_round_pack_keeps_wide_highest_bit_nonnegative(tmp_path: Path, width: int):
    source = tmp_path / "fp_round_pack.zhl"
    source.write_text(f"""import std.math.fp_round
module Top {{
    in sign:bit
    in magnitude:bits<{width}>
    out value:bits<64> out flags:bits<5> out illegal:bit
    packed=fp_round_pack<E=8,F=23,M={width}>(sign,magnitude,-{width-1},0)
    value=packed.value flags=packed.flags illegal=packed.illegal
}}""")
    # With scale=1-width, the two leading powers of two are exactly 0.5 and 1.
    cases = (
        (0, 1 << (width - 2), 0x3F000000),
        (0, 1 << (width - 1), 0x3F800000),
        (0, 0, 0),
        (1, 0, 0x80000000),
    )
    events = [{'set': {'sign': sign, 'magnitude': magnitude}} for sign, magnitude, _ in cases]
    expected = tuple({'value': value, 'flags': 0, 'illegal': 0} for _, _, value in cases)
    compared = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == expected
    assert compared.rtl_trace == expected


@pytest.mark.parametrize("e,f", ((8, 23), (11, 52)))
def test_fp_round_pack_checks_exponent_before_narrowing(tmp_path: Path, e: int, f: int):
    source = tmp_path / "fp_exponent.zhl"
    source.write_text(f"""import std.math.fp_round
module Top {{
    in sign:bit
    in mag:bits<8>
    in scale:sint<{e+5}>
    in rounding:u3
    out y:bits<64> out flags:bits<5> out illegal:bit
    p=fp_round_pack<E={e},F={f},M=8>(sign,mag,scale,rounding)
    y=p.value flags=p.flags illegal=p.illegal
}}""")
    exponent_max = (1 << (e - 1)) - 1
    scale_min = -(1 << (e + 4))
    scale_max = -scale_min - 1
    min_subnormal_exponent = 1 - exponent_max - f
    infinity = ((1 << e) - 1) << f
    max_binade = infinity - (1 << f)
    sign_bit = 1 << (e + f)
    cases = (
        (0, 1, scale_max, 0, infinity, 5),
        (0, 2, scale_max, 0, infinity, 5),
        (1, 2, scale_max, 1, sign_bit | (infinity - 1), 5),
        (0, 1, exponent_max, 0, max_binade, 0),
        (0, 2, exponent_max, 0, infinity, 5),
        (0, 1, min_subnormal_exponent, 0, 1, 0),
        (0, 1, min_subnormal_exponent - 1, 0, 0, 3),
        (0, 1, scale_min, 0, 0, 3),
        (0, 1, scale_min, 3, 1, 3),
        (1, 1, scale_min, 2, sign_bit | 1, 3),
        (1, 0, scale_min, 0, sign_bit, 0),
    )
    events = [
        {"set": dict(sign=sign, mag=mag, scale=scale, rounding=rounding)}
        for sign, mag, scale, rounding, _, _ in cases
    ]
    expected = tuple({"y": y, "flags": flags, "illegal": 0} for *_, y, flags in cases)
    compared = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == expected
    assert compared.rtl_trace == expected


@pytest.mark.parametrize("e,f", ((8, 23), (11, 52)))
def test_fp_round_pack_classifies_carry_after_rounding(tmp_path: Path, e: int, f: int):
    source = tmp_path / "fp_round_carry.zhl"
    source.write_text(f"""import std.math.fp_round
module Top {{
    in mag:bits<{f+2}>
    in scale:sint<{e+5}>
    out y:bits<64> out flags:bits<5>
    p=fp_round_pack<E={e},F={f},M={f+2}>(0,mag,scale,0)
    y=p.value flags=p.flags
}}""")
    exponent_max = (1 << (e - 1)) - 1
    infinity = ((1 << e) - 1) << f
    # RNE carries the upper midpoint into overflow and the lower midpoint
    # into the smallest normal. Tininess is determined after rounding.
    cases = (
        ((1 << (f + 2)) - 2, exponent_max - f - 1, infinity - 1, 0),
        ((1 << (f + 2)) - 1, exponent_max - f - 1, infinity, 5),
        ((1 << (f + 1)) - 2, -exponent_max - f, (1 << f) - 1, 0),
        ((1 << (f + 1)) - 1, -exponent_max - f, 1 << f, 1),
    )
    events = [{"set": dict(mag=mag, scale=scale)} for mag, scale, _, _ in cases]
    expected = tuple({"y": y, "flags": flags} for _, _, y, flags in cases)
    compared = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == expected
    assert compared.rtl_trace == expected


def _assert_fp_helper_trace(source, events, expected):
    compared = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=source.parent / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == expected
    assert compared.rtl_trace == expected
    with sim.compile(source, logic_state=True).create() as machine:
        assert tuple(
            {name: value.require_binary() for name, value in row.items()}
            for row in machine.run_events(events)
        ) == expected


def test_fp_sum_preserves_same_sign_carry_and_flags(tmp_path: Path):
    source = tmp_path / "sum_carry.zhl"
    source.write_text("""import std.math.fp_round
module Top {
    in a,b:bits<27> in sa,sb:bit in rounding:u3
    out narrow,wide:bits<64> out narrow_flags,wide_flags:bits<5>
    out limit:bits<64> out limit_flags:bits<5>
    n=fp_sum<E=8,F=23,M=27,K=26>(a,b,0,0,sa,sb,rounding)
    w=fp_sum<E=8,F=23,M=28,K=26>(extend<28>(a),extend<28>(b),0,0,sa,sb,rounding)
    l=fp_sum<E=3,F=2,M=128,K=127>(extend<128>(a) << 101,extend<128>(b) << 101,0,0,sa,sb,rounding)
    narrow=n.value narrow_flags=n.flags wide=w.value wide_flags=w.flags
    limit=l.value limit_flags=l.flags
}""")
    one = 1 << 26
    cases = (
        (one, one, 0, 0, 0, 0x40000000, 0x10),
        (one, one, 1, 1, 0, 0xc0000000, 0x30),
        (0, one, 0, 0, 0, 0x3f800000, 0x0c),
        (one, one >> 1, 0, 1, 0, 0x3f000000, 0x08),
        (one, one, 0, 1, 2, 0x80000000, 0x20),
        (0, 0, 0, 0, 0, 0, 0),
    )
    events = [
        {"set": dict(a=a, b=b, sa=sa, sb=sb, rounding=rounding)}
        for a, b, sa, sb, rounding, _, _ in cases
    ]
    expected = tuple(
        dict(narrow=value, wide=value, limit=limit, narrow_flags=0, wide_flags=0, limit_flags=0)
        for *_, value, limit in cases
    )
    _assert_fp_helper_trace(source, events, expected)


def test_fp_sum_keeps_scale_below_signed_exponent_minimum(tmp_path: Path):
    source = tmp_path / "sum_scale.zhl"
    source.write_text("""import std.math.fp_round
module Top {
    in e:s13 in small:s8 in sign:bit in rounding:u3
    out y,boundary:bits<64> out flags,boundary_flags:bits<5>
    r=fp_sum<E=8,F=23,M=8,K=1>(bitcast<bits<8>>(extend<8>(2)),zeros<8>,e,0,sign,sign,rounding)
    // M hits the existing signed leading-index bound for the tiny format.
    b=fp_sum<E=3,F=2,M=128,K=1>(bitcast<bits<128>>(extend<128>(2)),zeros<128>,small,0,sign,sign,rounding)
    y=r.value flags=r.flags boundary=b.value boundary_flags=b.flags
}""")
    cases = (
        (-4096, -128, 0, 0, 0, 0, 3),
        (-4095, -127, 0, 0, 0, 0, 3),
        (-4096, -128, 1, 0, 0x80000000, 0x20, 3),
        (-4096, -128, 1, 2, 0x80000001, 0x21, 3),
        (-4096, -128, 0, 3, 1, 1, 3),
        (0, 0, 0, 0, 0x3f800000, 0x0c, 0),
    )
    events = [
        {"set": dict(e=e, small=small, sign=sign, rounding=mode)}
        for e, small, sign, mode, *_ in cases
    ]
    expected = tuple(
        dict(y=y, boundary=boundary, flags=flags, boundary_flags=flags)
        for *_, y, boundary, flags in cases
    )
    _assert_fp_helper_trace(source, events, expected)


# Each raw magnitude represents a*2**(ea-K) before its sign is applied.
# Keep independent expected binary32 values and IEEE flags beside the inputs.
_FP_SUM_ALIGNMENT_CASES = (
    (8, 7, (
        (128, 128, 0, -8, 0, 0, 0, 0x3f808000, 0),
        (128, 128, 0, -7, 0, 0, 0, 0x3f810000, 0),
        (128, 128, 0, 0, 0, 0, 0, 0x40000000, 0),
        (128, 128, 0, -8, 0, 1, 0, 0x3f7f0000, 0),
        (128, 128, 0, -24, 0, 0, 0, 0x3f800000, 1),
        (128, 128, 0, -24, 0, 0, 3, 0x3f800001, 1),
        (128, 128, 0, -25, 0, 1, 0, 0x3f800000, 1),
        (128, 128, 0, -25, 0, 1, 2, 0x3f7fffff, 1),
        (128, 128, 0, -4096, 0, 0, 3, 0x3f800001, 1),
    )),
    (32, 0, (
        (1, 0xffffffff, 0, -32, 0, 1, 0, 0x2f800000, 0),
        (1, 0xffffffff, 0, -32, 1, 0, 0, 0xaf800000, 0),
        (1, 1, 0, 0, 0, 1, 2, 0x80000000, 0),
    )),
)


@pytest.mark.parametrize("width,point,cases", _FP_SUM_ALIGNMENT_CASES, ids=("narrow", "wide-cancellation"))
def test_fp_sum_aligns_before_its_only_rounding(tmp_path: Path, width: int, point: int, cases):
    source = tmp_path / "fp_alignment.zhl"
    source.write_text(f"""import std.math.fp_round
module Top {{
    in a,b:bits<{width}> in ea,eb:s13 in sa,sb:bit in rounding:u3
    out y,swapped:bits<64> out flags,swapped_flags:bits<5>
    r=fp_sum<E=8,F=23,M={width},K={point}>(a,b,ea,eb,sa,sb,rounding)
    s=fp_sum<E=8,F=23,M={width},K={point}>(b,a,eb,ea,sb,sa,rounding)
    y=r.value flags=r.flags swapped=s.value swapped_flags=s.flags
}}""")
    events = [
        {"set": dict(a=a, b=b, ea=ea, eb=eb, sa=sa, sb=sb, rounding=rounding)}
        for a, b, ea, eb, sa, sb, rounding, _, _ in cases
    ]
    expected = tuple(
        dict(y=value, swapped=value, flags=flags, swapped_flags=flags)
        for *_, value, flags in cases
    )
    _assert_fp_helper_trace(source, events, expected)


def test_fp_convert_preserves_source_scale_through_range_classification(tmp_path: Path):
    source = tmp_path / "convert_scale.zhl"
    source.write_text("""import std.math.fp_convert
module Top {
    in raw:bits<64> in rounding:u3
    out half,single:bits<64> out half_flags,single_flags:bits<5>
    h=fp_convert<SE=11,SF=52,DE=5,DF=10>(raw,rounding)
    s=fp_convert<SE=11,SF=52,DE=8,DF=23>(raw,rounding)
    half=h.value half_flags=h.flags single=s.value single_flags=s.flags
}""")
    high, low, sign = 0x7fe0000000000000, 0x0010000000000000, 1 << 63
    cases = (
        (high, 0, 0x7c00, 0x7f800000, 5),
        (high, 1, 0x7bff, 0x7f7fffff, 5),
        (high | sign, 2, 0xfc00, 0xff800000, 5),
        (high | sign, 3, 0xfbff, 0xff7fffff, 5),
        (low, 0, 0, 0, 3),
        (low, 3, 1, 1, 3),
        (low | sign, 0, 0x8000, 0x80000000, 3),
        (low | sign, 2, 0x8001, 0x80000001, 3),
        (0x3ff0000000000000, 0, 0x3c00, 0x3f800000, 0),
        (0x7ff0000000000000, 0, 0x7c00, 0x7f800000, 0),
    )
    events = [{"set": dict(raw=raw, rounding=rounding)} for raw, rounding, *_ in cases]
    expected = tuple(
        dict(half=half, single=single, half_flags=flags, single_flags=flags)
        for _, _, half, single, flags in cases
    )
    _assert_fp_helper_trace(source, events, expected)


@pytest.mark.parametrize("fraction_width", (23, 128))
def test_fp_decode_subnormal_uses_unsigned_normalization_distance(
    tmp_path: Path, fraction_width: int,
):
    source = tmp_path / "decode_shift.zhl"
    source.write_text(f"""import std.math.fp_format
module Top {{
    in raw:bits<{9+fraction_width}>
    out exponent:s13 out subnormal,is_zero:bit
    out significand:bits<{fraction_width+1}>
    d=fp_decode<E=8,F={fraction_width}>(raw)
    exponent=d.exponent subnormal=d.subnormal
    is_zero=d.is_zero significand=d.significand
}}""")
    cases = (
        (2, -126 - fraction_width + 1, 1, 0, 1 << fraction_width),
        (1, -126 - fraction_width, 1, 0, 1 << fraction_width),
        (0, -126, 0, 1, 0),
        (127 << fraction_width, 0, 0, 0, 1 << fraction_width),
    )
    events = [{"set": {"raw": raw}} for raw, *_ in cases]
    expected = tuple(
        dict(exponent=exponent & 0x1fff, subnormal=tiny, is_zero=zero, significand=sig)
        for _, exponent, tiny, zero, sig in cases
    )
    _assert_fp_helper_trace(source, events, expected)


def test_fp_bit_length_rejects_a_count_that_would_overflow_u8(monkeypatch):
    monkeypatch.setattr(semantic_limits, "TOTAL_GENERATED", 0)
    source = """import std.math.fp_format
module Top { in value:bits<256> out length:u8
    length=fp_bit_length<M=256>(value)
}"""
    with pytest.raises(SemanticError, match=r"cannot extend bits<9> to width 8"):
        compile_source(source)


def test_fp_round_pack_rejects_an_unrepresentable_highest_bit(monkeypatch):
    monkeypatch.setattr(semantic_limits, "TOTAL_GENERATED", 0)
    source = """import std.math.fp_round
module Top { in magnitude:bits<129> out value:bits<64>
    value=fp_round_pack<E=3,F=2,M=129>(0,magnitude,0,0).value
}"""
    with pytest.raises(SemanticError, match=r"requires equal packed widths.*bits<2>.*bits<1>"):
        compile_source(source)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rv_register_slice_stalls_and_replaces_on_simultaneous_transfer(
    tmp_path: Path,
) -> None:
    result = compile_source(
        WITNESSES["stream_register_slice"],
        top="Top",
    )
    rtl = tmp_path / "Top.sv"
    rtl.write_text(
        emit_sv_artifact(result.ir, selected_ir_identity="stdlib:rv-slice").text
    )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        r"""
module tb;
  logic clk=0, rst=1;
  logic [7:0] input_payload=0;
  logic input_valid=0, output_ready=0;
  wire input_ready, output_valid;
  wire [7:0] output_payload;
  Top dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    input_payload=8'h11; input_valid=1; output_ready=0;
    #1;
    if (!input_ready) $fatal(1,"empty slice did not accept");
    tick;
    if (!output_valid || output_payload != 8'h11)
      $fatal(1,"accepted payload not held");
    input_payload=8'h22; output_ready=1;
    #1;
    if (!input_ready) $fatal(1,"full simultaneous replacement blocked");
    tick;
    if (!output_valid || output_payload != 8'h22)
      $fatal(1,"replacement payload mismatch");
    input_valid=0; tick;
    if (output_valid) $fatal(1,"slice did not drain");
    $finish;
  end
endmodule
"""
    )
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--top-module",
            "tb",
            "-Wno-fatal",
            str(rtl),
            str(bench),
            "-Mdir",
            str(tmp_path / "obj"),
        ),
        env={**os.environ, "CCACHE_DISABLE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj" / "Vtb"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_ping_pong_blocks_reordering_and_allows_atomic_retire_commit(
    tmp_path: Path,
) -> None:
    result = compile_source(
        WITNESSES["storage_ping_pong"],
        top="Top",
    )
    rtl = tmp_path / "Top.sv"
    rtl.write_text(
        emit_sv_artifact(result.ir, selected_ir_identity="stdlib:ping-pong").text
    )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        r"""
module tb;
  logic clk=0, rst=1, write=0, commit=0, retire=0;
  logic [1:0] write_index=0, read_index=0;
  logic [7:0] write_data=0;
  wire write_ready, commit_ready, read_valid;
  wire [7:0] read_data;
  Top dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    write=1; write_index=0; write_data=8'h11; tick;
    write_index=1; write_data=8'h22; tick;
    write=0; commit=1;
    if (!commit_ready) $fatal(1,"first bank could not commit");
    tick; commit=0;
    read_index=0; #1;
    if (!read_valid || read_data != 8'h11)
      $fatal(1,"first bank was not published");

    write=1; write_index=0; write_data=8'h33; tick; write=0;
    commit=1; #1;
    if (commit_ready) $fatal(1,"second bank reordered before retire");
    tick;
    if (!read_valid || read_data != 8'h11)
      $fatal(1,"blocked commit changed visible bank");

    retire=1; #1;
    if (!commit_ready) $fatal(1,"retire did not open atomic commit");
    tick; retire=0; commit=0;
    if (!read_valid || read_data != 8'h33)
      $fatal(1,"atomic retire/commit lost the second bank");
    retire=1; tick; retire=0;
    if (read_valid) $fatal(1,"retired bank remained valid");
    $finish;
  end
endmodule
"""
    )
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--top-module",
            "tb",
            "-Wno-fatal",
            str(rtl),
            str(bench),
            "-Mdir",
            str(tmp_path / "obj_ping_pong"),
        ),
        env={**os.environ, "CCACHE_DISABLE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj_ping_pong" / "Vtb"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
