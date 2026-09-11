import shutil
import subprocess
import tempfile
import unittest
import os
from pathlib import Path

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.ir import FixedOverflowPolicy, FixedType, UFixedType
from zlang.ir import expressions as ir_expr
from zlang.opt import lower, restore
from zlang.parser import ParseError, parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate


class FixedPointTests(unittest.TestCase):
    ROUNDING_SOURCE = r"""
module FixedRounds {
  in a:fixed<8,2>
  out toward:fixed<8,0>
  out down:fixed<8,0>
  out away:fixed<8,0>
  out even:fixed<8,0>
  toward=quantize<fixed<8,0>>(a){round toward_zero overflow wrap}
  down=quantize<fixed<8,0>>(a){round floor overflow wrap}
  away=quantize<fixed<8,0>>(a){round away_zero overflow wrap}
  even=quantize<fixed<8,0>>(a){round nearest_even overflow wrap}
}
"""

    @staticmethod
    def _quantize(value, source_fraction, target_width, target_fraction, nearest, saturate):
        shift = source_fraction - target_fraction
        magnitude = abs(value)
        quotient, remainder = divmod(magnitude, 1 << shift)
        if nearest:
            half = 1 << (shift - 1)
            if remainder > half or (remainder == half and quotient & 1):
                quotient += 1
        converted = -quotient if value < 0 else quotient
        minimum, maximum = -(1 << (target_width - 1)), (1 << (target_width - 1)) - 1
        if saturate:
            return min(max(converted, minimum), maximum)
        raw = converted & ((1 << target_width) - 1)
        return raw - (1 << target_width) if raw >= (1 << (target_width - 1)) else raw

    @staticmethod
    def _rounding_expected(raw, mode, divisor=4):
        quotient, remainder = divmod(abs(raw), divisor)
        if mode == "floor":
            return raw // divisor
        if mode == "away_zero" and remainder:
            quotient += 1
        if mode == "nearest_even" and (
            remainder * 2 > divisor
            or (remainder * 2 == divisor and quotient & 1)
        ):
            quotient += 1
        return -quotient if raw < 0 else quotient

    @classmethod
    def _rounding_bench(cls):
        checks = []
        for raw in range(-128, 128):
            values = tuple(
                cls._rounding_expected(raw, mode)
                for mode in ("toward_zero", "floor", "away_zero", "nearest_even")
            )
            checks.append(
                f"a={raw}; #1; if (toward!=={values[0]} || down!=={values[1]} "
                f"|| away!=={values[2]} || even!=={values[3]}) "
                f'$fatal(1,"rounding mismatch raw={raw}");'
            )
        return """
module tb;
  logic signed [7:0] a;
  wire signed [7:0] toward, down, away, even;
  FixedRounds dut(.a,.toward,.down,.away,.even);
  initial begin
""" + "\n".join(checks) + "\n$finish; end endmodule\n"

    @classmethod
    def _separate_rounding_bench(cls, module_names):
        body = cls._rounding_bench()
        body = body.replace(
            "FixedRounds dut(.a,.toward,.down,.away,.even);",
            "\n".join(
                f"  {name} dut{index}(.a,.y({wire}));"
                for index, (name, wire) in enumerate(
                    zip(module_names, ("toward", "down", "away", "even"), strict=True)
                )
            ),
        )
        return body

    @classmethod
    def _single_rounding_bench(cls, module_name, mode):
        checks = []
        for raw in range(-128, 128):
            expected = cls._rounding_expected(raw, mode)
            checks.append(
                f'a={raw}; #1; if (y!=={expected}) '
                f'$fatal(1,"{mode} mismatch raw={raw}");'
            )
        return f"""
module tb;
  logic signed [7:0] a; wire signed [7:0] y;
  {module_name} dut(.a,.y);
  initial begin
""" + "\n".join(checks) + "\n$finish; end endmodule\n"

    def test_types_and_exact_arithmetic(self):
        module = analyze(parse(
            "module FixedMath { in a:fixed<8,4> in b:fixed<8,4> "
            "out sum:fixed<9,4> out product:fixed<16,8> sum=a+b product=a*b }"
        ))
        self.assertEqual(module.ports[0].type, FixedType(8, 4))
        self.assertEqual(simulate(module, a=24, b=-8), {"sum": 16, "product": -192})

    def test_concise_types_normalize_to_canonical_fixed_types(self):
        module = analyze(parse(
            "type Sample=SF8.8 struct Pair { s:SF_Sat8.8 u:UF8.8 } "
            "module F { in a:Sample in p:Pair out y:UF_Sat8.8 y=p.u }"
        ))
        self.assertEqual(module.ports[0].type, FixedType(16, 8))
        self.assertEqual(
            module.structs[0].fields[0].type,
            FixedType(16, 8, FixedOverflowPolicy.SATURATE),
        )
        self.assertEqual(module.structs[0].fields[1].type, UFixedType(16, 8))
        self.assertEqual(
            module.ports[-1].type,
            UFixedType(16, 8, FixedOverflowPolicy.SATURATE),
        )
        self.assertEqual(
            analyze(parse("module F { in a:fixed<16,8> out y:fixed<16,8> y=a }")),
            analyze(parse("module F { in a:SF8.8 out y:SF8.8 y=a }")),
        )

    def test_parameterized_saturating_generic_types(self):
        module = analyze(parse(
            "module F<W=8,F=4> { in a:fixed_sat<W,F> in u:ufixed_sat<W,F> "
            "out y:fixed_sat<W,F> out v:ufixed_sat<W,F> y=a v=u }"
        ))
        self.assertEqual(
            module.ports[0].type,
            FixedType(8, 4, FixedOverflowPolicy.SATURATE),
        )
        self.assertEqual(
            module.ports[1].type,
            UFixedType(8, 4, FixedOverflowPolicy.SATURATE),
        )
        self.assertEqual(
            module.ports[3].type,
            UFixedType(8, 4, FixedOverflowPolicy.SATURATE),
        )

    def test_unsigned_fixed_type(self):
        module = analyze(parse("module U { in a:ufixed<6,2> out y:ufixed<7,2> y=a+1 }"))
        self.assertEqual(module.ports[0].type, UFixedType(6, 2))
        self.assertEqual(simulate(module, a=5), {"y": 9})
        parameterized = analyze(parse(
            "module Whole<W=8,F=0> { in a:fixed<W,F> out y:fixed<W,F> y=a }"
        ))
        self.assertEqual(parameterized.ports[0].type, FixedType(8, 0))

    def test_rounding_saturation_and_raw_round_trip(self):
        truncate = compile_source(
            "module F { in a:fixed<8,4> out y:fixed<6,2> y=fixed_truncate_wrap(a) }"
        ).ir
        nearest = compile_source(
            "module F { in a:fixed<8,4> out y:fixed<6,2> y=fixed_round_even_saturate(a) }"
        ).ir
        self.assertEqual(simulate(truncate, a=23)["y"], 5)
        self.assertEqual(simulate(nearest, a=22)["y"], 6)
        self.assertEqual(simulate(nearest, a=-22)["y"], -6)
        saturated = compile_source(
            "module F { in a:fixed<12,4> out y:fixed<6,2> y=fixed_truncate_saturate(a) }"
        ).ir
        self.assertEqual(simulate(saturated, a=500)["y"], 31)
        self.assertEqual(simulate(saturated, a=-500)["y"], -32)
        raw = compile_source(
            "module F { in a:sint<8> out y:fixed<8,4> y=fixed_raw(a) }"
        ).ir
        self.assertEqual(simulate(raw, a=-17)["y"], -17)

    def test_exhaustive_small_width_quantization(self):
        modes = (
            ("fixed_truncate_wrap", False, False),
            ("fixed_truncate_saturate", False, True),
            ("fixed_round_even_wrap", True, False),
            ("fixed_round_even_saturate", True, True),
        )
        for width in range(3, 6):
            for fraction in range(1, width - 1):
                for intrinsic, nearest, saturate in modes:
                    module = compile_source(
                        f"module F {{ in a:fixed<{width},{fraction}> "
                        f"out y:fixed<{width},{fraction-1}> y={intrinsic}(a) }}"
                    ).ir
                    for value in range(-(1 << (width - 1)), 1 << (width - 1)):
                        expected = self._quantize(
                            value, fraction, width, fraction - 1, nearest, saturate
                        )
                        self.assertEqual(simulate(module, a=value)["y"], expected)

    def test_exhaustive_same_scale_target_overflow_policy(self):
        for integer in range(1, 4):
            for fraction in range(0, 3):
                width = integer + fraction
                signed_minimum = -(1 << (width - 1))
                signed_maximum = (1 << (width - 1)) - 1
                for prefix, saturate in (("SF", False), ("SF_Sat", True)):
                    module = compile_source(
                        f"module F {{ in a:{prefix}{integer}.{fraction} "
                        f"in b:{prefix}{integer}.{fraction} "
                        f"out y:{prefix}{integer}.{fraction} y=a+b }}"
                    ).ir
                    for left in range(signed_minimum, signed_maximum + 1):
                        for right in range(signed_minimum, signed_maximum + 1):
                            total = left + right
                            if saturate:
                                expected = min(max(total, signed_minimum), signed_maximum)
                            else:
                                raw = total & ((1 << width) - 1)
                                expected = raw - (1 << width) if raw >= (1 << (width - 1)) else raw
                            self.assertEqual(simulate(module, a=left, b=right)["y"], expected)
                unsigned_maximum = (1 << width) - 1
                for prefix, saturate in (("UF", False), ("UF_Sat", True)):
                    module = compile_source(
                        f"module F {{ in a:{prefix}{integer}.{fraction} "
                        f"in b:{prefix}{integer}.{fraction} "
                        f"out y:{prefix}{integer}.{fraction} y=a+b }}"
                    ).ir
                    for left in range(unsigned_maximum + 1):
                        for right in range(unsigned_maximum + 1):
                            total = left + right
                            expected = min(total, unsigned_maximum) if saturate else total & unsigned_maximum
                            self.assertEqual(simulate(module, a=left, b=right)["y"], expected)

    def test_out_of_range_literals_require_explicit_quantize(self):
        expected = {
            "SF1.2": -4,
            "SF_Sat1.2": 3,
            "UF1.2": 4,
            "UF_Sat1.2": 7,
        }
        for type_name, raw in expected.items():
            with self.assertRaisesRegex(SemanticError, "use quantize"):
                compile_source(f"module F {{ out y:{type_name} y=3 }}")
            module = compile_source(
                f"module F {{ out y:{type_name} y=quantize(3,toward_zero) }}"
            ).ir
            self.assertEqual(simulate(module)["y"], raw)

    def test_exact_rational_literals_and_strict_diagnostics(self):
        exact = compile_source("module F { out y:SF8.8 y=0.5 }").ir
        negative = compile_source("module F { out y:SF8.8 y=-1.25 }").ir
        self.assertEqual(simulate(exact)["y"], 128)
        self.assertEqual(simulate(negative)["y"], -320)
        with self.assertRaisesRegex(SemanticError, "not exactly representable"):
            compile_source("module F { out y:fixed<8,4> y=0.1 }")
        with self.assertRaisesRegex(SemanticError, "outside the range"):
            compile_source("module F { out y:SF_Sat8.8 y=1000.0 }")

    def test_contextual_raw_literal_is_bit_exact_and_ignores_saturation(self):
        positive = compile_source(
            "module F { out y:SF_Sat8.8 y=fixed_raw(0x0C22) }"
        ).ir
        negative = compile_source(
            "module F { out y:SF_Sat8.8 y=fixed_raw(0xFFFF) }"
        ).ir
        self.assertEqual(simulate(positive)["y"], 0x0C22)
        self.assertEqual(simulate(negative)["y"], -1)
        with self.assertRaisesRegex(SemanticError, "does not fit 16 bits"):
            compile_source("module F { out y:SF8.8 y=fixed_raw(0x10000) }")
        with self.assertRaisesRegex(SemanticError, "integral raw bit pattern"):
            compile_source("module F { out y:SF8.8 y=fixed_raw(0.5) }")
        for type_name, value in (
            ("fixed<1,0>", -1),
            ("fixed<2,0>", -2),
            ("fixed<8,4>", -128),
        ):
            with self.subTest(type_name=type_name, value=value):
                with self.assertRaisesRegex(
                    SemanticError,
                    "non-negative integral raw bit pattern",
                ):
                    compile_source(
                        f"module F {{ out y:{type_name} y=fixed_raw({value}) }}"
                    )

    def test_all_rounding_modes_have_frozen_signed_behavior(self):
        expected = {
            "toward_zero": {6: 1, 10: 2, -6: -1, -10: -2},
            "floor": {6: 1, 10: 2, -6: -2, -10: -3},
            "away_zero": {6: 2, 10: 3, -6: -2, -10: -3},
            "nearest_even": {6: 2, 10: 2, -6: -2, -10: -2},
        }
        for mode, vectors in expected.items():
            module = compile_source(
                "module F { in a:fixed<8,2> out y:fixed<8,0> "
                f"y=quantize<fixed<8,0>>(a){{round {mode} overflow wrap}} }}"
            ).ir
            for raw, result in vectors.items():
                with self.subTest(mode=mode, raw=raw):
                    self.assertEqual(simulate(module, a=raw)["y"], result)

    def test_exhaustive_all_rounding_modes_cross_wrap_and_saturate(self):
        for source_width in range(3, 6):
            for source_fraction in range(1, source_width):
                target_fraction = source_fraction - 1
                target_width = max(2, source_width - 1)
                for signed, source_name, target_name in (
                    (True, "fixed", "fixed"),
                    (False, "ufixed", "ufixed"),
                ):
                    values = (
                        range(-(1 << (source_width - 1)), 1 << (source_width - 1))
                        if signed
                        else range(1 << source_width)
                    )
                    for mode in ("toward_zero", "floor", "away_zero", "nearest_even"):
                        for overflow in ("wrap", "saturate"):
                            module = compile_source(
                                f"module F {{ in a:{source_name}<{source_width},{source_fraction}> "
                                f"out y:{target_name}<{target_width},{target_fraction}> "
                                f"y=quantize<{target_name}<{target_width},{target_fraction}>>(a)"
                                f"{{round {mode} overflow {overflow}}} }}"
                            ).ir
                            for value in values:
                                rounded = self._rounding_expected(value, mode, 2)
                                if overflow == "saturate":
                                    minimum = -(1 << (target_width - 1)) if signed else 0
                                    maximum = (
                                        (1 << (target_width - 1)) - 1
                                        if signed
                                        else (1 << target_width) - 1
                                    )
                                    expected = min(max(rounded, minimum), maximum)
                                else:
                                    expected = rounded & ((1 << target_width) - 1)
                                    if signed and expected >= (1 << (target_width - 1)):
                                        expected -= 1 << target_width
                                self.assertEqual(
                                    simulate(module, a=value)["y"], expected,
                                    (source_width, source_fraction, signed, mode, overflow, value),
                                )

    def test_unsigned_subtraction_is_modular_at_max_operand_width(self):
        module = compile_source(
            "module F { in a:UF2.2 in b:UF3.2 out y:ufixed<5,2> y=a-b }"
        ).ir
        expression = module.assignments[0].expression
        self.assertEqual(expression.type, UFixedType(5, 2))
        self.assertEqual(simulate(module, a=0, b=1)["y"], 31)

    def test_quantized_rational_and_full_override_share_fixed_convert(self):
        contextual = compile_source(
            "module F { out y:SF8.8 y=quantize(0.1,nearest_even) }"
        ).ir
        explicit = compile_source(
            "module F { out y:SF8.8 "
            "y=quantize<SF8.8>(1000.0){round floor overflow saturate} }"
        ).ir
        left = contextual.assignments[0].expression
        right = explicit.assignments[0].expression
        self.assertIsInstance(left, ir_expr.FixedConvert)
        self.assertEqual(left.rational_denominator, 10)
        self.assertEqual(simulate(contextual)["y"], 26)
        self.assertEqual(simulate(explicit)["y"], 32767)
        self.assertEqual(restore(lower(contextual)), contextual)

    def test_dot_rounding_is_one_post_accumulation_conversion(self):
        exact = compile_source(
            "module F { in a:vec<2,SF2.2> in b:vec<2,SF2.2> "
            "out y:fixed<9,4> y=dot(a,b) }"
        ).ir.assignments[0].expression
        rounded = compile_source(
            "module F { in a:vec<2,SF2.2> in b:vec<2,SF2.2> "
            "out y:SF_Sat4.2 y=dot(a,b,floor) }"
        ).ir.assignments[0].expression
        self.assertIsInstance(exact, ir_expr.Reduce)
        self.assertIsInstance(rounded, ir_expr.FixedConvert)
        self.assertIsInstance(rounded.expression, ir_expr.Reduce)
        self.assertEqual(rounded.rounding, ir_expr.FixedRounding.FLOOR)
        with self.assertRaisesRegex(SemanticError, "only valid when"):
            compile_source(
                "fn f(a:vec<2,SF2.2>,b:vec<2,SF2.2>)->fixed<9,4> "
                "{ dot(a,b,floor) } module F { out y:bit y=0 }"
            )
        with self.assertRaisesRegex(SemanticError, "cannot assign"):
            compile_source(
                "module F { in a:vec<2,SF2.2> in b:vec<2,SF2.2> "
                "out y:SF4.2 y=dot(a,b) }"
            )

    def test_same_scale_widening_and_narrowing_are_explicit_in_ir(self):
        widened = compile_source(
            "module F { in a:SF2.2 out y:SF4.2 y=a }"
        ).ir.assignments[0].expression
        saturated = compile_source(
            "module F { in a:SF4.2 out y:SF_Sat2.2 y=a }"
        ).ir.assignments[0].expression
        self.assertEqual(widened.type, FixedType(6, 2))
        self.assertEqual(saturated.overflow.value, "saturate")

    def test_target_policy_is_shared_by_all_typed_semantic_boundaries(self):
        module = analyze(parse(r"""
struct Box { value:SF_Sat2.2 }
fn satadd(a:SF_Sat2.2,b:SF_Sat2.2)->SF_Sat2.2 { a+b }
module Child { in x:SF_Sat2.2 out y:SF_Sat2.2 y=x }
module Top {
  clock clk reset rst
  in go:bit in a:SF_Sat2.2 in b:SF_Sat2.2 in wide:SF4.2
  out y:SF_Sat2.2
  reg r:SF_Sat2.2=0
  local:SF_Sat2.2=a+b
  boxed:Box=Box{value=a+b}
  inst child:Child{x=wide}
  when go { r <- a+b }
  y=satadd(local,child.y)
}
"""))
        self.assertIsInstance(module.functions[0].body, ir_expr.FixedConvert)
        local = next(item for item in module.locals if item.name == "local")
        self.assertIsInstance(local.expression, ir_expr.FixedConvert)
        boxed = next(item for item in module.locals if item.name == "boxed")
        self.assertIsInstance(boxed.expression.fields[0][1], ir_expr.FixedConvert)
        self.assertIsInstance(module.rules[0].actions[0].expression, ir_expr.FixedConvert)
        self.assertIsInstance(module.instance_bindings[0].expression, ir_expr.FixedConvert)
        for conversion in (
            module.functions[0].body,
            local.expression,
            boxed.expression.fields[0][1],
            module.rules[0].actions[0].expression,
            module.instance_bindings[0].expression,
        ):
            self.assertEqual(conversion.overflow.value, "saturate")

    def test_invalid_scale_and_implicit_rescale_are_rejected(self):
        with self.assertRaisesRegex(SemanticError, "0 <= F < W"):
            analyze(parse("module F { in a:fixed<8,8> out y:fixed<8,8> y=a }"))
        with self.assertRaisesRegex(SemanticError, "identical fractional widths"):
            analyze(parse(
                "module F { in a:fixed<8,4> in b:fixed<8,3> out y:fixed<9,4> y=a+b }"
            ))
        with self.assertRaisesRegex(SemanticError, "to fixed_sat<6,2>"):
            analyze(parse(
                "module F { in a:SF4.4 out y:SF_Sat4.2 y=a }"
            ))
        for invalid in ("SF0.8", "UF_Sat0.1", "SF8.x"):
            with self.subTest(type=invalid), self.assertRaises(ParseError):
                parse(f"module F {{ in a:{invalid} out y:bit y=0 }}")
        with self.assertRaisesRegex(SemanticError, "0 <= F < W"):
            analyze(parse(
                "module F { in a:fixed_sat<8,8> out y:fixed_sat<8,8> y=a }"
            ))

    def test_canonical_round_trip_retains_type_and_conversion_modes(self):
        module = compile_source(
            "module F { in a:fixed<8,4> out y:fixed<6,2> y=fixed_round_even_saturate(a) }"
        ).ir
        restored = restore(lower(module))
        self.assertEqual(restored.assignments, module.assignments)
        saturating = compile_source(
            "module S { in a:SF4.2 out y:SF_Sat2.2 y=a }"
        ).ir
        self.assertEqual(restore(lower(saturating)), saturating)

    def test_manifest_distinguishes_wrap_and_saturating_types(self):
        wrap = compile_source("module F { in a:SF2.2 out y:SF2.2 y=a }").ir
        saturating = compile_source(
            "module F { in a:SF_Sat2.2 out y:SF_Sat2.2 y=a }"
        ).ir
        wrap_artifact = publish_artifact(
            wrap, "module F; endmodule", backend="systemverilog", selected_ir_identity="fixed-wrap",
            recursive_design=build_recursive_formal_design(wrap),
        )
        sat_artifact = publish_artifact(
            saturating, "module F; endmodule", backend="systemverilog", selected_ir_identity="fixed-sat",
            recursive_design=build_recursive_formal_design(saturating),
        )
        restored = BackendArtifact.from_json(sat_artifact.to_json())
        self.assertEqual(wrap_artifact.bindings[0].canonical_type, "fixed<4,2>")
        self.assertEqual(sat_artifact.bindings[0].canonical_type, "fixed_sat<4,2>")
        self.assertNotEqual(wrap_artifact.recursive_bindings[0].canonical_type,
                            sat_artifact.recursive_bindings[0].canonical_type)
        self.assertEqual(restored.recursive_bindings, sat_artifact.recursive_bindings)


    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_direct_sv_is_lint_clean(self):
        module = compile_source(
            "module F { in a:SF4.4 in b:SF4.4 out y:SF_Sat4.4 y=a+b }"
        ).ir
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "F.sv"
            path.write_text(emit_experimental(module))
            subprocess.run(("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(path)), check=True)

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_direct_sv_wrap_to_saturation_mutation_is_observable(self):
        wrap = compile_source(
            "module FixedWrap { in a:SF2.2 in b:SF2.2 out y:SF2.2 y=a+b }"
        ).ir
        saturating = compile_source(
            "module FixedSat { in a:SF_Sat2.2 in b:SF_Sat2.2 out y:SF_Sat2.2 y=a+b }"
        ).ir
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wrap.sv").write_text(emit_experimental(wrap))
            (root / "sat.sv").write_text(emit_experimental(saturating))
            (root / "tb.sv").write_text(r"""
module tb;
  logic signed [3:0] a, b; wire signed [3:0] wrap_y, sat_y;
  FixedWrap w(.a,.b,.y(wrap_y)); FixedSat s(.a,.b,.y(sat_y));
  initial begin
    a=7; b=7; #1;
    if (wrap_y !== -2 || sat_y !== 7) $fatal(1,"positive overflow policy mismatch");
    a=-8; b=-8; #1;
    if (wrap_y !== 0 || sat_y !== -8) $fatal(1,"negative overflow policy mismatch");
    $finish;
  end
endmodule
""")
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                ("verilator", "--binary", "--top-module", "tb", "-Wno-fatal",
                 str(root / "wrap.sv"), str(root / "sat.sv"), str(root / "tb.sv"),
                 "-Mdir", str(root / "obj")),
                check=True, capture_output=True, text=True, env=environment,
            )
            subprocess.run((str(root / "obj" / "Vtb"),), check=True,
                           capture_output=True, text=True)

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_direct_sv_all_rounding_modes_match_exhaustive_oracle(self):
        modes = ("toward_zero", "floor", "away_zero", "nearest_even")
        module_names = tuple(f"FixedRoundSv{index}" for index in range(4))
        sources = []
        for module_name, mode in zip(module_names, modes, strict=True):
            module = analyze(parse(
                f"module {module_name} {{ in a:fixed<8,2> out y:fixed<8,0> "
                f"y=quantize<fixed<8,0>>(a){{round {mode} overflow wrap}} }}"
            ))
            sources.append(emit_experimental(module))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl, bench = root / "FixedRounds.sv", root / "tb.sv"
            rtl.write_text("\n".join(sources))
            bench.write_text(self._separate_rounding_bench(module_names))
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                ("verilator", "--binary", "--top-module", "tb", "-Wno-fatal",
                 str(rtl), str(bench), "-Mdir", str(root / "obj")),
                check=True, capture_output=True, text=True, env=environment,
            )
            subprocess.run((str(root / "obj" / "Vtb"),), check=True,
                           capture_output=True, text=True)




if __name__ == "__main__":
    unittest.main()
