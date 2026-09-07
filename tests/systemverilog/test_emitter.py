from pathlib import Path
import shutil
import tempfile
import unittest

from zlang.backend.systemverilog import (
    emit_experimental,
)
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "ALU",
    "PipelinedMAC",
    "RvPassthrough",
    "CreditSource",
    "ControlCsr",
    "RequestClient",
    "RuleCounter",
)
SOURCES = {
    "ALU": "alu.zhl",
    "PipelinedMAC": "pipelined_mac.zhl",
    "RvPassthrough": "rv_passthrough.zhl",
    "CreditSource": "credit_source.zhl",
    "ControlCsr": "control_csr.zhl",
    "RequestClient": "request_client.zhl",
    "RuleCounter": "rule_counter.zhl",
}


class DirectSystemVerilogEmitterTests(unittest.TestCase):
    def test_every_representative_region_matches_its_golden(self) -> None:
        for module_name in CASES:
            with self.subTest(module=module_name):
                result = compile_source(
                    (ROOT / "examples" / SOURCES[module_name]).read_text()
                )
                self.assertEqual(
                    emit_experimental(result.ir),
                    (
                        ROOT
                        / "examples/generated"
                        / f"{module_name}.direct.sv"
                    ).read_text(),
                )

    def test_backend_consumes_typed_ir_and_marks_the_boundary(self) -> None:
        result = compile_source((ROOT / "examples/alu.zhl").read_text())
        generated = emit_experimental(result.ir)

        self.assertIn("Generated from backend-independent typed ZLang IR", generated)
        self.assertIn("module ALU", generated)
        self.assertNotIn("switch op", generated)

    def test_runtime_logical_not_lowers_without_unary_minus(self) -> None:
        result = compile_source(
            "module Invert { in x:bit out y:bit y = !x }",
            include_clash=False,
        )
        generated = emit_experimental(result.ir)
        self.assertIn("assign y = ((x) == (1'd0));", generated)

    def test_pure_typed_function_is_emitted_once_and_called(self) -> None:
        result = compile_source((ROOT / "examples/fir2.zhl").read_text())
        generated = emit_experimental(result.ir)
        self.assertIn("module FIR2", generated)
        self.assertEqual(generated.count("function automatic logic [15:0] tap("), 1)
        self.assertEqual(generated.count("tap = "), 1)
        self.assertEqual(generated.count("tap(samples["), 2)

    def test_nested_pure_function_helpers_are_emitted_once_and_lint(self) -> None:
        source = """
fn bump(x : u8) -> u8 {
    truncate<8>(x + 1)
}

fn choose_bump(select : u1, x : u8) -> u8 {
    switch select {
        0 => bump(x)
        else => x
    }
}

module NestedSwitchCall {
    in select : u1
    in value : u8
    out y : u8

    y = choose_bump(select, value)
}
"""
        result = compile_source(source, include_clash=False)
        generated = emit_experimental(result.ir)

        self.assertIn("module NestedSwitchCall", generated)
        self.assertEqual(generated.count("function automatic logic [7:0] bump("), 1)
        self.assertEqual(
            generated.count("function automatic logic [7:0] choose_bump("), 1
        )
        # Function arguments keep their source stem with a compact arg_ prefix
        # so ordinary source names cannot become SystemVerilog keywords.
        self.assertIn(
            "choose_bump = ((arg_select)", generated
        )
        self.assertIn("bump(arg_x)", generated)
        self.assertIn("assign y = choose_bump(select, value);", generated)
        self.assertIn(" + ", generated)
        self.assertIn(" ? ", generated)

        if shutil.which("verilator") is None:
            self.skipTest("Verilator is required for strict direct-SV lint")
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / "NestedSwitchCall.sv"
            rtl.write_text(generated)
            lint_with_verilator((rtl,), "NestedSwitchCall")

    def test_reused_expression_in_switch_arm_uses_materialized_temporary(self) -> None:
        result = compile_source(
            """
module SwitchShared {
    in choose : u1
    in a : u8
    in b : u8
    out y : u11

    heavy : u10 = extend<9>(a) + extend<9>(b)
    y = switch choose {
        0 => heavy + heavy
        else => 0
    }
}
""",
            include_clash=False,
        )
        generated = emit_experimental(result.ir)
        temporary = next(
            line.split()[1]
            for line in generated.splitlines()
            if line.startswith("  assign zlang_expr_")
            and " + " in line
        )
        output = next(
            line for line in generated.splitlines()
            if line.startswith("  assign y =")
        )
        self.assertIn(temporary, output)
        self.assertLess(len(output), 200)

    def test_negative_typed_constants_use_legal_sized_literal_order(self) -> None:
        result = compile_source(
            "module NegativeConstant { out y:fixed<16,14> "
            "y=quantize<fixed<16,14>>(-0.7071067811865475) { "
            "round nearest_even overflow saturate } }",
            include_clash=False,
        )
        generated = emit_experimental(result.ir)
        self.assertIn("-16'sd11585", generated)
        self.assertNotIn("16'sd-11585", generated)

        if shutil.which("verilator") is None:
            self.skipTest("Verilator is required for strict direct-SV lint")
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / "NegativeConstant.sv"
            rtl.write_text(generated)
            lint_with_verilator((rtl,), "NegativeConstant")

    def test_reserved_scalar_output_name_is_mangled_at_driver_and_port(self) -> None:
        result = compile_source(
            "module ReservedOutput { in input:s16 out output:s16 "
            "output=input }",
            include_clash=False,
        )
        generated = emit_experimental(result.ir)
        self.assertIn("input wire logic signed [15:0] zlang_input", generated)
        self.assertIn("output logic signed [15:0] zlang_output", generated)
        self.assertIn("assign zlang_output = zlang_input;", generated)
        self.assertNotIn("assign output =", generated)

        if shutil.which("verilator") is None:
            self.skipTest("Verilator is required for strict direct-SV lint")
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / "ReservedOutput.sv"
            rtl.write_text(generated)
            lint_with_verilator((rtl,), "ReservedOutput")


if __name__ == "__main__":
    unittest.main()
