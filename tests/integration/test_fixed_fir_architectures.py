import shutil
import subprocess
import tempfile
import os
from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.fixed_point import quantize_rational
from zlang.ir import expressions as expr
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/fixed_fir_architectures.zhl").read_text()
LATENCY = {
    "FixedFIRLinear": 1,
    "FixedFIRBalanced": 1,
    "FixedFIRPipelinedTree": 3,
    "FixedFIRDspOriented": 2,
}


def _oracle(samples: tuple[int, ...], coefficients: tuple[int, ...]) -> int:
    return quantize_rational(
        sum(left * right for left, right in zip(samples, coefficients, strict=True)),
        1 << 6,
        fraction=0,
        width=16,
        signed=True,
        rounding=expr.FixedRounding.NEAREST_EVEN,
        overflow=expr.FixedOverflow.SATURATE,
    )


def _vectors() -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    return [
        ((0,) * 8, (0,) * 8),
        ((1024,) * 8, (1024,) * 8),
        ((-1024, 512, -256, 128, 64, -32, 16, -8), (256,) * 8),
        ((2047,) * 8, (2047,) * 8),
        ((-2048,) * 8, (2047,) * 8),
        ((3, 5, 7, 9, -3, -5, -7, -9), (11, -13, 17, -19, 23, -29, 31, -37)),
    ]


def _packed(values: tuple[int, ...]) -> str:
    return "'{ " + ", ".join(f"12'h{value & 0xfff:03x}" for value in values) + " }"


def _signed_literal(value: int) -> str:
    return f"-16'sd{-value}" if value < 0 else f"16'sd{value}"


def _bench(top: str, latency: int) -> str:
    vectors = _vectors()
    expected = [_oracle(*item) for item in vectors]
    lines = []
    all_vectors = vectors + [((0,) * 8, (0,) * 8)] * (latency - 1)
    for cycle, (samples, coefficients) in enumerate(all_vectors):
        lines.append(f"samples={_packed(samples)}; coefficients={_packed(coefficients)}; tick;")
        index = cycle - latency + 1
        if 0 <= index < len(expected):
            lines.append(
                f"if ($signed(result) !== {_signed_literal(expected[index])}) "
                f'$fatal(1,"{top} mismatch vector {index}: %0d",$signed(result));'
            )
    return f"""
module tb;
  logic clk=0, rst=1;
  logic signed [11:0] samples [0:7];
  logic signed [11:0] coefficients [0:7];
  wire signed [15:0] result;
  {top} dut(.clk,.rst,.samples,.coefficients,.result);
  task tick; begin #1 clk=1; #1; clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    {''.join(lines)}
    $finish;
  end
endmodule
"""


def _run_verilator(top: str, rtl_files: list[Path], bench: str, root: Path) -> None:
    tb = root / "tb.sv"
    tb.write_text(bench)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--top-module", "tb", "-Wno-fatal",
         *(str(path) for path in rtl_files), str(tb), "-Mdir", str(root / "obj")),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(root / "obj" / "Vtb"),), check=True, capture_output=True, text=True)


def test_all_variants_share_one_post_accumulation_quantization_contract() -> None:
    for top in LATENCY:
        module = compile_source(SOURCE, top=top).ir
        conversions = []

        def visit(value: expr.Expression) -> None:
            if isinstance(value, expr.FixedConvert):
                conversions.append(value)
            for field in getattr(value, "__dataclass_fields__", {}).values():
                child = getattr(value, field.name)
                if isinstance(child, expr.Expression):
                    visit(child)
                elif isinstance(child, tuple):
                    for item in child:
                        if isinstance(item, expr.Expression):
                            visit(item)

        for assignment in (*module.assignments, *module.next_assignments):
            visit(assignment.expression)
        quantizations = [
            item for item in conversions
            if item.type.width == 16 and item.type.fraction == 14
        ]
        assert len(quantizations) == 1, top
        final = quantizations[0]
        assert final.rounding is expr.FixedRounding.NEAREST_EVEN
        assert final.overflow is expr.FixedOverflow.SATURATE
        assert not any(
            isinstance(item, expr.FixedConvert)
            for item in getattr(final.expression, "operands", ())
        )


def test_direct_sv_materializes_large_quantize_operand_once_and_deterministically() -> None:
    module = compile_source(SOURCE, top="FixedFIRBalanced").ir
    first = emit_experimental(module)
    second = emit_experimental(module)
    assert first == second
    assert "logic signed [26:0] zlang_expr_0;" in first
    assert first.count("assign zlang_expr_0 =") == 1
    assert first.count("samples[95:84]") == 1
    assert "$signed(zlang_expr_0)" in first
    core = first.split("module FixedFIRBalanced (", 1)[0]
    assert len(core.encode()) < 4_000
    artifact = emit_sv_artifact(module, selected_ir_identity="fixed-fir-balanced")
    assert any(item.semantic_signal_id == "port:result" for item in artifact.bindings)
    assert all("zlang_expr" not in item.rtl_path for item in artifact.bindings)


def test_combinational_quantize_materializes_but_trivial_conversion_does_not() -> None:
    large = compile_source(
        "module F { in a:vec<8,SF2.10> in b:vec<8,SF2.10> out y:fixed<16,14> "
        "y=quantize<fixed<16,14>>(dot(a,b)){round nearest_even overflow saturate} }"
    ).ir
    small = compile_source(
        "module S { in a:fixed<12,10> out y:fixed<16,14> "
        "y=quantize<fixed<16,14>>(a){round nearest_even overflow saturate} }"
    ).ir
    large_sv = emit_experimental(large)
    small_sv = emit_experimental(small)
    assert "logic signed [26:0] zlang_expr_0;" in large_sv
    assert large_sv.count("a[95:84]") == 1
    assert "zlang_expr_" not in small_sv


def test_variants_are_bit_exact_after_declared_latency() -> None:
    vectors = _vectors()
    padding = [((0,) * 8, (0,) * 8)] * 4
    cycles = [
        {"samples": samples, "coefficients": coefficients}
        for samples, coefficients in (*vectors, *padding)
    ]
    expected = [_oracle(*item) for item in vectors]
    for top, latency in LATENCY.items():
        module = compile_source(SOURCE, top=top).ir
        outputs = simulate_cycles(module, cycles)
        observed = [item["result"] for item in outputs]
        assert observed[latency:latency + len(expected)] == expected, top


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_all_direct_sv_variants_lint() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for top in LATENCY:
            module = compile_source(SOURCE, top=top).ir
            path = root / f"{top}.sv"
            path.write_text(emit_sv_artifact(module, selected_ir_identity=top).text)
            subprocess.run(
                ("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(path)),
                check=True, capture_output=True, text=True,
            )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("top", tuple(LATENCY))
def test_direct_sv_variants_are_bit_exact_in_verilator(top: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        module = compile_source(SOURCE, top=top).ir
        rtl = root / f"{top}.sv"
        rtl.write_text(emit_sv_artifact(module, selected_ir_identity=top).text)
        _run_verilator(top, [rtl], _bench(top, LATENCY[top]), root)
