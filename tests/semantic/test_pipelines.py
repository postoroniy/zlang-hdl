from pathlib import Path
import unittest

from zlang.ir.expressions import Pipeline
from zlang.ir.pipelines import MultiplierMapping, RegisterPlacement
from zlang.opt import lower, restore
from zlang.ir.types import SIntType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class PipelineSemanticTests(unittest.TestCase):
    def test_pipeline_is_typed_and_records_stage_count(self) -> None:
        module = analyze(parse((ROOT / "examples/pipelined_mac.zhl").read_text()))
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, Pipeline)
        self.assertEqual(expression.stages, 2)
        self.assertEqual(expression.type, UIntType(17))

    def test_pipeline_requires_clock_and_reset(self) -> None:
        source = "module Bad { in a:u8 out y:u8 y=pipeline(1){a} }"
        with self.assertRaisesRegex(SemanticError, "pipeline requires a module clock"):
            analyze(parse(source))

    def test_auto_pipeline_rejects_an_unsupported_shape(self) -> None:
        source = "module Auto { clock c reset r in a:u8 out y:u8 y=pipeline(auto){a} }"
        with self.assertRaisesRegex(
            SemanticError, "accepts only a sum of full-precision products"
        ):
            analyze(parse(source))

    def test_auto_pipeline_requires_a_power_of_two_search_shape(self) -> None:
        source = """
            module Auto {
                clock c reset r
                in a:u8 in b:u8 in c0:u8 in d:u8 in e:u8
                in f:u8 in g:u8 in h:u8 in i:u8 in j:u8
                out y:u20
                y=pipeline(auto){a*b + c0*d + e*f + g*h + i*j}
            }
        """
        with self.assertRaisesRegex(
            SemanticError, "requires a power-of-two product count"
        ):
            analyze(parse(source))

    def test_auto_pipeline_selects_a_legal_balanced_dsp_candidate(self) -> None:
        module = analyze(
            parse((ROOT / "examples/auto_pipeline_products.zhl").read_text())
        )
        exploration = module.pipeline_explorations[0]

        self.assertEqual(exploration.output, "y")
        self.assertEqual(exploration.search_bound, 5)
        self.assertEqual(exploration.selected, "balanced_levels_dsp")
        self.assertEqual(exploration.selected_candidate.latency, 3)
        self.assertEqual(exploration.selected_candidate.initiation_interval, 1)
        self.assertIs(
            exploration.selected_candidate.register_placement,
            RegisterPlacement.BALANCED_LEVELS,
        )
        self.assertIs(
            exploration.selected_candidate.multiplier_mapping,
            MultiplierMapping.DSP,
        )
        self.assertEqual(module.assignments[0].expression,
                         exploration.selected_candidate.expression)
        self.assertEqual(restore(lower(module)), module)

    def test_auto_pipeline_traverses_exact_representation_operands(self) -> None:
        module = analyze(parse("""
            module Auto {
                clock clk reset rst
                in raw : bits<8>
                in nested : vec<2,vec<2,u8>>
                in tail : vec<1,u8>
                in b : u8 in c : u8 in d : u8
                out y : u19

                raw_value = bitcast<u8>(concat(raw[7:4], raw[3:0]))
                lanes = concat(reshape<vec<4,u8>>(nested), tail)
                y = pipeline(
                    auto, latency<=3, throughput==1, dsp<=4, fmax>=400
                ) {
                      raw_value * b
                    + lanes[0] * c
                    + lanes[1] * d
                    + lanes[4] * b
                }
            }
        """))
        exploration = module.pipeline_explorations[0]

        self.assertEqual(
            [candidate.name for candidate in exploration.candidates],
            [
                "linear_output_logic",
                "balanced_output_logic",
                "balanced_levels_logic",
                "balanced_output_dsp",
                "balanced_levels_dsp",
            ],
        )
        self.assertEqual(
            exploration.selected_candidate.transformations,
            (
                "reassociate_balanced_tree",
                "map_products_to_dsp",
                "register_each_operator_level",
                "latency_balance",
            ),
        )
        rendered = repr(exploration.selected_candidate.expression)
        for node in ("Bitcast", "Concat", "Slice", "VectorConcat", "Reshape"):
            self.assertIn(node, rendered)

    def test_resource_constraint_selects_the_legal_logic_pipeline(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("dsp<=4, fmax>=400", "dsp<=0, fmax>=250")
        exploration = analyze(parse(source)).pipeline_explorations[0]

        self.assertEqual(exploration.selected, "balanced_levels_logic")
        self.assertEqual(exploration.selected_candidate.estimate.dsp, 0)

    def test_signed_product_reassociation_preserves_the_exact_type(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("u8", "s8").replace("u19", "s19")
        module = analyze(parse(source))
        exploration = module.pipeline_explorations[0]

        self.assertEqual(exploration.result_type, SIntType(19))
        self.assertTrue(
            all(
                candidate.expression.type == SIntType(19)
                for candidate in exploration.candidates
            )
        )

    def test_impossible_constraints_explain_every_candidate(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("fmax>=400", "fmax>=900")
        with self.assertRaisesRegex(
            SemanticError,
            r"no legal pipeline architecture.*linear_output_logic violates.*"
            r"balanced_levels_dsp violates",
        ):
            analyze(parse(source))

    def test_auto_pipeline_rejects_wrong_constraint_relation(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("fmax>=400", "fmax<=400")
        with self.assertRaisesRegex(
            SemanticError, "constraint 'fmax' requires '>='"
        ):
            analyze(parse(source))

    def test_auto_pipeline_rejects_duplicate_constraints(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("latency<=3", "latency<=3, latency<=4")
        with self.assertRaisesRegex(
            SemanticError, "repeats 'latency' constraint"
        ):
            analyze(parse(source))

    def test_auto_pipeline_rejects_non_unit_throughput(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        source = source.replace("throughput==1", "throughput==2")
        with self.assertRaisesRegex(
            SemanticError, "no legal pipeline architecture.*throughput=1 not ==2"
        ):
            analyze(parse(source))

    def test_auto_pipeline_rejects_state_dependencies(self) -> None:
        source = """
            module Bad {
                clock c reset rst
                in a:u8 in b:u8 in d:u8 in e:u8
                reg r:u8 = 0
                out y:u19
                y=pipeline(auto){r*a + b*d + a*e + b*e}
            }
        """
        with self.assertRaisesRegex(
            SemanticError, "products must be pure.*RegisterRef"
        ):
            analyze(parse(source))

    def test_auto_pipeline_must_be_a_complete_output_expression(self) -> None:
        source = """
            module Bad {
                clock c reset rst
                in a:u8 in b:u8 in d:u8 in e:u8
                out y:u20
                y=pipeline(auto){a*b + b*d + a*e + b*e} + 0
            }
        """
        with self.assertRaisesRegex(
            SemanticError, "allowed only as a complete wire-output assignment"
        ):
            analyze(parse(source))

    def test_fixed_pipeline_rejects_auto_constraints(self) -> None:
        source = (
            "module Bad { clock c reset r in a:u8 out y:u8 "
            "y=pipeline(1, latency<=2){a} }"
        )
        with self.assertRaisesRegex(
            SemanticError, "fixed pipeline stages do not accept"
        ):
            analyze(parse(source))

    def test_misaligned_binary_operands_are_rejected(self) -> None:
        source = """
            module Bad { clock c reset r in a:u8 in b:u8 out y:u9
                y = delay<1>(a) + b
            }
        """
        with self.assertRaisesRegex(
            SemanticError, "latency mismatch in .: operands have latencies 0, 1"
        ):
            analyze(parse(source))

    def test_misaligned_mux_is_rejected(self) -> None:
        source = """
            module Bad { clock c reset r in pick:bit in a:u8 in b:u8 out y:u8
                y = mux(pick, delay<1>(a), b)
            }
        """
        with self.assertRaisesRegex(SemanticError, "latency mismatch in mux"):
            analyze(parse(source))

    def test_constants_are_available_at_any_pipeline_latency(self) -> None:
        source = """
            module Valid { clock c reset r in a:u8 out y:u9
                y = delay<1>(a) + 1
            }
        """
        module = analyze(parse(source))
        self.assertEqual(module.assignments[0].expression.type, UIntType(9))


if __name__ == "__main__":
    unittest.main()
